import httpx
import pytest

from gateway.app import create_app
from gateway.config import ClassConfig, Config
from gateway.limiter import RateLimitConfig, RateLimiter
from gateway.providers import Chaos

from conftest import CHAT, Scripted

HEADERS = {"X-Tenant": "acme", "X-Feature": "chat", "X-Request-Id": "r1"}


def test_disabled_config_rejects_bad_values_only_when_on():
    RateLimitConfig()  # off: no numbers required
    RateLimitConfig(enabled=True, rate_per_s=1, burst=5)
    with pytest.raises(ValueError):
        RateLimitConfig(enabled=True, rate_per_s=0, burst=5)


async def test_disabled_limiter_always_allows(redis, manual_clock):
    lim = RateLimiter(redis, manual_clock, RateLimitConfig(enabled=False))
    for _ in range(100):
        ok, retry = await lim.check("acme", "chat")
        assert ok and retry == 0.0


async def test_burst_then_block_then_refill(redis, manual_clock):
    lim = RateLimiter(redis, manual_clock, RateLimitConfig(enabled=True, rate_per_s=1.0, burst=3))
    # three tokens are available at once
    assert [await lim.check("acme") for _ in range(3)] == [(True, 0.0)] * 3
    # the fourth is refused, and tells the caller how long until a token frees up
    ok, retry = await lim.check("acme")
    assert ok is False and 0 < retry <= 1.0
    # after two seconds, two tokens have refilled
    manual_clock.t += 2.0
    assert (await lim.check("acme"))[0] is True
    assert (await lim.check("acme"))[0] is True
    assert (await lim.check("acme"))[0] is False  # only two had refilled


async def test_tenants_have_separate_buckets(redis, manual_clock):
    lim = RateLimiter(redis, manual_clock, RateLimitConfig(enabled=True, rate_per_s=1.0, burst=2))
    assert (await lim.check("acme"))[0] and (await lim.check("acme"))[0]
    assert (await lim.check("acme"))[0] is False      # acme is spent
    assert (await lim.check("globex"))[0] is True      # globex is untouched


async def test_per_feature_splits_the_bucket(redis, manual_clock):
    lim = RateLimiter(redis, manual_clock, RateLimitConfig(enabled=True, rate_per_s=1.0, burst=1, per_feature=True))
    assert (await lim.check("acme", "chat"))[0] is True
    assert (await lim.check("acme", "chat"))[0] is False   # chat is spent
    assert (await lim.check("acme", "search"))[0] is True   # search is its own bucket


async def test_endpoint_returns_429_with_retry_after(redis, manual_clock):
    cfg = Config(
        providers=[{"name": "alpha"}],
        classes={"interactive": ClassConfig(preference=["alpha"], timeout_s=5)},
        rate_limit=RateLimitConfig(enabled=True, rate_per_s=1.0, burst=2),
    )
    providers = {"alpha": Chaos(Scripted("alpha", manual_clock, latency=0.01), manual_clock)}
    app = create_app(cfg, redis=redis, clock=manual_clock, providers=providers, seed=1, start_worker=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    ok = [await client.post("/v1/chat/completions", json=CHAT, headers={**HEADERS, "X-Request-Id": f"r{i}"}) for i in range(2)]
    assert all(r.status_code == 200 for r in ok)
    blocked = await client.post("/v1/chat/completions", json=CHAT, headers={**HEADERS, "X-Request-Id": "r3"})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["type"] == "rate_limit"
    assert int(blocked.headers["Retry-After"]) >= 1
    await client.aclose()
