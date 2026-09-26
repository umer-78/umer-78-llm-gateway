import random

import httpx
import pytest

from gateway.app import create_app
from gateway.config import ClassConfig, Config, QueueConfig, Resilience
from gateway.providers import Chaos
from gateway.queue import JobQueue

from conftest import CHAT, Scripted, make_router

HEADERS = {"X-Tenant": "acme", "X-Feature": "chat", "X-Request-Id": "r1"}


def config(**kw):
    return Config(
        providers=[{"name": n} for n in ("alpha", "beta")],
        classes={"interactive": ClassConfig(preference=["alpha", "beta"], timeout_s=5),
                 "batch": ClassConfig(preference=["beta", "alpha"], timeout_s=5, deferrable=True)},
        **kw,
    )


@pytest.fixture
def setup(redis, fast_clock):
    alpha = Scripted("alpha", fast_clock, latency=0.1, price_in=3.0, price_out=15.0)
    beta = Scripted("beta", fast_clock, latency=0.1)
    providers = {p.name: Chaos(p, fast_clock) for p in (alpha, beta)}
    app = create_app(config(), redis=redis, clock=fast_clock, providers=providers, seed=1,
                     admin_token="secret", start_worker=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    return app, client, alpha, beta


async def test_every_call_must_say_who_it_is_for(setup):
    _, client, _, _ = setup
    r = await client.post("/v1/chat/completions", json=CHAT, headers={"X-Tenant": "acme"})
    assert r.status_code == 400
    assert "X-Feature" in r.json()["error"]["message"] and "X-Request-Id" in r.json()["error"]["message"]


async def test_openai_shaped_answer_with_cost_attributed_to_tenant_and_feature(setup):
    app, client, _, _ = setup
    r = await client.post("/v1/chat/completions", json=CHAT, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "alpha answered"
    assert body["usage"] == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    assert r.headers["X-Gateway-Provider"] == "alpha"
    cost = (10 * 3.0 + 20 * 15.0) / 1e6
    assert float(r.headers["X-Gateway-Cost-USD"]) == pytest.approx(cost)
    metrics = (await client.get("/metrics")).text
    assert f'llm_gateway_cost_usd_total{{feature="chat",provider="alpha",tenant="acme"}} {cost}' in metrics


async def test_an_idempotency_key_replays_instead_of_calling_again(setup):
    _, client, alpha, _ = setup
    h = {**HEADERS, "Idempotency-Key": "k-1"}
    first = await client.post("/v1/chat/completions", json=CHAT, headers=h)
    second = await client.post("/v1/chat/completions", json=CHAT, headers=h)
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replay"] == "true"
    assert alpha.calls == 1


async def test_interactive_fails_fast_when_every_provider_is_down(setup):
    _, client, alpha, beta = setup
    alpha.script, beta.script = ["server_error"], ["server_error"]
    r = await client.post("/v1/chat/completions", json=CHAT, headers=HEADERS)
    assert r.status_code == 503
    assert [a["provider"] for a in r.json()["error"]["attempts"]] == ["alpha", "beta"]


async def test_deferrable_work_is_queued_through_an_outage_and_finishes_after(setup):
    app, client, alpha, beta = setup
    alpha.script, beta.script = ["server_error"], ["server_error"]
    h = {**HEADERS, "X-Request-Class": "batch", "X-Feature": "summarize", "Idempotency-Key": "job-7"}
    r = await client.post("/v1/chat/completions", json=CHAT, headers=h)
    assert r.status_code == 202 and r.json()["id"] == "job-7"
    # the same key again does not queue a second copy
    assert (await client.post("/v1/chat/completions", json=CHAT, headers=h)).status_code == 202
    assert await app.state.gw.queue.depth() == 1
    assert await app.state.gw.queue.work_once()  # providers have recovered
    job = (await client.get("/v1/jobs/job-7")).json()
    assert job["status"] == "done" and job["provider"] == "beta" and job["attempts"] == 1
    metrics = (await client.get("/metrics")).text
    assert 'llm_gateway_cost_usd_total{feature="summarize",provider="beta",tenant="acme"}' in metrics


async def test_chaos_needs_the_admin_token_and_moves_traffic(setup):
    _, client, _, _ = setup
    assert (await client.post("/admin/chaos/alpha", json={"error_rate": 1.0})).status_code == 401
    auth = {"Authorization": "Bearer secret"}
    r = await client.post("/admin/chaos/alpha", json={"error_rate": 1.0, "error_kind": "rate_limited"}, headers=auth)
    assert r.status_code == 200 and r.json()["chaos"]["error_kind"] == "rate_limited"
    ans = await client.post("/v1/chat/completions", json=CHAT, headers=HEADERS)
    assert ans.headers["X-Gateway-Provider"] == "beta"
    assert (await client.delete("/admin/chaos/alpha", headers=auth)).json()["chaos"] is None
    bad = await client.post("/admin/chaos/alpha", json={"error_kind": "nonsense"}, headers=auth)
    assert bad.status_code == 400


async def test_queue_backs_off_with_jitter_and_gives_up_after_max_attempts(redis, manual_clock):
    beta = Scripted("beta", manual_clock, ["server_error"] * 10, latency=0.0)
    classes = {"batch": ClassConfig(preference=["beta"], timeout_s=5, deferrable=True)}
    router, _, _ = make_router([beta], classes, redis, manual_clock, resilience=Resilience(breakers=False))
    q = JobQueue(redis, manual_clock, router, QueueConfig(max_attempts=3, base_delay_s=2, max_delay_s=60), rng=random.Random(3))
    meta = {"tenant": "t", "feature": "f", "request_id": "j"}
    assert await q.enqueue("j", CHAT, "batch", meta)
    assert not await q.enqueue("j", CHAT, "batch", meta), "the same id is queued once"
    delays, last = [], manual_clock.now()
    assert await q.work_once()
    job = await q.get("j")
    while job["status"] == "retrying":
        delays.append(job["next_try_at"] - last)
        assert not await q.work_once(), "not due yet"
        manual_clock.t = last = job["next_try_at"]
        assert await q.work_once()
        job = await q.get("j")
    assert job["status"] == "failed" and job["attempts"] == 3
    # exponential with jitter: attempt n waits between half and all of 2 * 2^(n-1) seconds
    assert len(delays) == 2 and 1.0 <= delays[0] <= 2.0 and 2.0 <= delays[1] <= 4.0
