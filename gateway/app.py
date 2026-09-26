"""HTTP surface: an OpenAI-compatible endpoint, jobs, metrics, and admin/chaos controls."""
import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis

from .breaker import STATE_CODE, Breakers
from .clock import Clock
from .config import Config
from .health import HealthTracker
from .metrics import Metrics
from .providers import Chaos, MockProvider, OpenAICompatProvider
from .queue import JobQueue
from .router import Outcome, Router

log = logging.getLogger("gateway")


def build_providers(cfg: Config, clock: Clock, seed: int = 0) -> dict[str, Chaos]:
    out = {}
    for i, p in enumerate(cfg.providers):
        common = {"model": p.get("model", p["name"]), "price_in": p.get("price_in", 0.0), "price_out": p.get("price_out", 0.0)}
        kind = p.get("kind", "mock")
        if kind == "mock":
            inner = MockProvider(p["name"], median_latency_s=p.get("median_latency_s", 0.8),
                                 latency_sigma=p.get("latency_sigma", 0.4), error_rate=p.get("error_rate", 0.0),
                                 seed=seed + i, clock=clock, **common)
        elif kind == "openai_compat":
            inner = OpenAICompatProvider(p["name"], base_url=p["base_url"], api_key_env=p.get("api_key_env"), **common)
        else:
            raise ValueError(f"provider {p['name']!r}: unknown kind {kind!r}")
        out[p["name"]] = Chaos(inner, clock, seed=seed + 100 + i)
    return out


def error(status: int, message: str, type_: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": type_, **extra}}, status_code=status, headers=headers)


def attempts_json(out: Outcome) -> list[dict]:
    return [{"provider": a.provider, "ok": a.ok, "error": a.kind, "latency_s": round(a.latency_s, 3),
             "cost_usd": round(a.cost_usd, 8), "probe": a.probe, "hedge": a.hedge} for a in out.attempts]


def create_app(cfg: Config, *, redis=None, clock: Clock | None = None, providers: dict | None = None,
               seed: int | None = None, admin_token: str | None = None, start_worker: bool = True) -> FastAPI:
    clock = clock or Clock()
    redis = redis or Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    admin_token = admin_token if admin_token is not None else os.environ.get("GATEWAY_ADMIN_TOKEN", "")
    rng = random.Random(seed)
    metrics = Metrics()
    providers = providers or build_providers(cfg, clock, seed or 0)
    events: list[dict] = []

    def on_change(provider, old, new, reason):
        metrics.breaker_changes.labels(provider, new).inc()
        metrics.breaker_state.labels(provider).set(STATE_CODE[new])
        events.append({"t": clock.now(), "provider": provider, "from": old, "to": new, "reason": reason})
        del events[:-500]
        log.warning("breaker %s: %s -> %s (%s)", provider, old, new, reason)

    health = HealthTracker(redis, clock, cfg.window_s, cfg.redis_prefix)
    budgets = {p["name"]: p["latency_budget_s"] for p in cfg.providers if "latency_budget_s" in p}
    breakers = Breakers(redis, clock, health, cfg.breaker, cfg.redis_prefix, on_change, budgets)
    router = Router(providers, cfg.classes, breakers, health, metrics, clock, cfg.resilience, rng)

    def attribute(meta: dict, out: Outcome) -> None:
        """Every attempt's spend, hedge losers included, lands on the tenant and feature that caused it."""
        for a in out.attempts:
            if a.cost_usd:
                metrics.cost.labels(meta["tenant"], meta["feature"], a.provider).inc(a.cost_usd)
        if out.completion:
            c = out.completion
            metrics.tokens.labels(meta["tenant"], meta["feature"], out.provider, "input").inc(c.input_tokens)
            metrics.tokens.labels(meta["tenant"], meta["feature"], out.provider, "output").inc(c.output_tokens)

    queue = JobQueue(redis, clock, router, cfg.queue, cfg.redis_prefix, rng, on_done=lambda job, out: attribute(job["meta"], out))

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        worker = asyncio.create_task(queue.run(stop)) if start_worker and cfg.resilience.queue else None
        yield
        stop.set()
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    app = FastAPI(title="llm-gateway", lifespan=lifespan)
    app.state.gw = SimpleNamespace(cfg=cfg, redis=redis, clock=clock, metrics=metrics, providers=providers,
                                   health=health, breakers=breakers, router=router, queue=queue, events=events)

    @app.post("/v1/chat/completions")
    async def chat(request: Request,
                   x_tenant: str | None = Header(None), x_feature: str | None = Header(None),
                   x_request_id: str | None = Header(None), x_request_class: str | None = Header(None),
                   idempotency_key: str | None = Header(None)):
        missing = [h for h, v in (("X-Tenant", x_tenant), ("X-Feature", x_feature), ("X-Request-Id", x_request_id)) if not v]
        if missing:
            return error(400, f"missing header {', '.join(missing)}: every call is attributed to a tenant and a feature",
                         "invalid_request_error")
        class_name = x_request_class or cfg.default_class
        if class_name not in cfg.classes:
            return error(400, f"unknown request class {class_name!r}; configured: {', '.join(cfg.classes)}", "invalid_request_error")
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return error(400, "the body must be JSON", "invalid_request_error")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
            return error(400, "messages must be a non-empty list", "invalid_request_error")
        if body.get("stream"):
            return error(400, "streaming is not supported by this gateway yet", "invalid_request_error")
        meta = {"tenant": x_tenant, "feature": x_feature, "request_id": x_request_id}

        idem = f"{cfg.redis_prefix}:idem:{x_tenant}:{idempotency_key}" if idempotency_key else None
        if idem:
            if not await redis.set(idem, json.dumps({"state": "in_progress"}), nx=True, ex=cfg.idempotency_ttl_s):
                prev = json.loads(await redis.get(idem) or "{}")
                if prev.get("state") != "done":
                    return error(409, "a request with this Idempotency-Key is still in progress", "conflict")
                return JSONResponse(prev["body"], status_code=prev["status"], headers={**prev["headers"], "Idempotent-Replay": "true"})

        out = await router.route(body, class_name)
        attribute(meta, out)
        headers = {"X-Gateway-Attempts": str(len(out.attempts)), "X-Gateway-Cost-USD": f"{out.cost_usd:.8f}"}
        if out.completion:
            c = out.completion
            headers["X-Gateway-Provider"] = out.provider
            status, payload = 200, {
                "id": f"chatcmpl-{x_request_id}", "object": "chat.completion", "created": int(time.time()), "model": c.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": c.text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": c.input_tokens, "completion_tokens": c.output_tokens, "total_tokens": c.input_tokens + c.output_tokens},
                "gateway": {"provider": out.provider, "request_class": class_name, "cost_usd": round(out.cost_usd, 8), "attempts": attempts_json(out)},
            }
        elif out.error_kind in ("content_filter", "bad_request"):
            status, payload = 400, {"error": {"message": f"the provider rejected the request ({out.error_kind})", "type": out.error_kind,
                                              "attempts": attempts_json(out)}}
        elif cfg.classes[class_name].deferrable and cfg.resilience.queue:
            job_id = idempotency_key or x_request_id
            await queue.enqueue(job_id, body, class_name, meta)
            headers["Location"] = f"/v1/jobs/{job_id}"
            status, payload = 202, {"id": job_id, "object": "gateway.job", "status": "queued", "poll": f"/v1/jobs/{job_id}",
                                    "reason": out.error_kind}
        else:
            status, payload = 503, {"error": {"message": "every provider for this request class failed or is unavailable",
                                              "type": "gateway_unavailable", "reason": out.error_kind, "attempts": attempts_json(out)}}
        metrics.responses.labels(class_name, str(status)).inc()
        if idem:
            if status == 503:  # nothing happened, so a retry with the same key should run again
                await redis.delete(idem)
            else:
                await redis.set(idem, json.dumps({"state": "done", "status": status, "body": payload, "headers": headers}), ex=cfg.idempotency_ttl_s)
        return JSONResponse(payload, status_code=status, headers=headers)

    @app.get("/v1/jobs/{job_id}")
    async def job(job_id: str):
        j = await queue.get(job_id)
        if not j:
            return error(404, f"no job {job_id!r}", "not_found")
        j.pop("request", None)
        return j

    @app.get("/metrics")
    async def prom():
        metrics.queue_depth.set(await queue.depth())
        for name in providers:
            metrics.breaker_state.labels(name).set(STATE_CODE[(await breakers.state(name))["state"]])
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    def admin_denied(authorization: str | None):
        if not admin_token:
            return error(403, "admin endpoints are off: set GATEWAY_ADMIN_TOKEN to enable them", "forbidden")
        if authorization != f"Bearer {admin_token}":
            return error(401, "admin token required", "unauthorized")
        return None

    @app.get("/admin/providers")
    async def admin_providers(authorization: str | None = Header(None)):
        if (denied := admin_denied(authorization)):
            return denied
        result = {}
        for name, p in providers.items():
            b = await breakers.state(name)
            h = await health.snapshot(name, since=b["since"])
            result[name] = {"breaker": b, "health": h.__dict__, "chaos": p.active(), "model": p.model}
        return result

    @app.get("/admin/events")
    async def admin_events(authorization: str | None = Header(None)):
        return admin_denied(authorization) or {"events": events[-200:]}

    @app.post("/admin/chaos/{name}")
    async def chaos_set(name: str, request: Request, authorization: str | None = Header(None)):
        if (denied := admin_denied(authorization)):
            return denied
        if name not in providers:
            return error(404, f"no provider {name!r}", "not_found")
        try:
            rule = providers[name].set(**(await request.json()))
        except (TypeError, ValueError) as e:
            return error(400, str(e), "invalid_request_error")
        return {"provider": name, "chaos": rule}

    @app.delete("/admin/chaos/{name}")
    async def chaos_clear(name: str, authorization: str | None = Header(None)):
        if (denied := admin_denied(authorization)):
            return denied
        if name not in providers:
            return error(404, f"no provider {name!r}", "not_found")
        providers[name].clear()
        return {"provider": name, "chaos": None}

    return app
