"""Chaos benchmark: identical traffic through three setups while providers fail on a schedule.

    python -m bench.chaos                # against a local Redis (REDIS_URL, default localhost:6379)
    python -m bench.chaos --fake-redis   # no Redis needed

It runs the real gateway app in-process over ASGI against simulated providers,
on a clock where one simulated second lasts --scale real seconds, and prints a
results table. Nothing here is estimated: every number is counted from the
responses the gateway returned.
"""
import argparse
import asyncio
import json
import pathlib
import random
import statistics
import uuid
from dataclasses import replace

import httpx

from gateway.app import build_providers, create_app
from gateway.clock import ScaledClock
from gateway.config import Resilience, load_config
from gateway.health import percentile

DURATION = 240  # simulated seconds of traffic
DRAIN = 180  # time allowed afterwards for deferred jobs to finish
RATE = {"interactive": 5, "batch": 1}  # requests per simulated second
SCENARIO = [  # (start, end, provider, chaos rule)
    (60, 120, "alpha", {"error_rate": 1.0, "error_kind": "server_error"}),
    (100, 115, "beta", {"error_rate": 1.0, "error_kind": "server_error"}),
    (100, 115, "gamma", {"error_rate": 1.0, "error_kind": "rate_limited"}),
    (150, 210, "alpha", {"extra_latency_ms": 6000}),
]
STRATEGIES = {
    "direct": Resilience(breakers=False, failover=False, hedging=False, retries=0, queue=False),
    "client retries": Resilience(breakers=False, failover=False, hedging=False, retries=3, queue=False),
    "gateway": Resilience(),
}
TENANTS = [("acme", 0.7), ("globex", 0.3)]


def arrivals(seed: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for s in range(DURATION):
        for cls, rate in RATE.items():
            for k in range(rate):
                tenant = "acme" if rng.random() < TENANTS[0][1] else "globex"
                feature = "summarize" if cls == "batch" else rng.choice(["chat", "search"])
                out.append({"t": s + (k + rng.random()) / rate, "class": cls, "tenant": tenant, "feature": feature,
                            "id": f"{cls[0]}-{s}-{k}", "prompt": f"{feature} request {s}-{k}: " + "x" * rng.randint(200, 1200)})
    return sorted(out, key=lambda r: r["t"])


async def chaos_driver(client, clock):
    auth = {"Authorization": "Bearer bench"}
    changes = sorted([(start, p, rule) for start, _, p, rule in SCENARIO] + [(end, p, None) for _, end, p, _ in SCENARIO],
                     key=lambda c: c[0])
    for t, provider, rule in changes:
        await asyncio.sleep(max(0.0, clock.to_real(t - clock.now())))
        if rule:
            await client.post(f"/admin/chaos/{provider}", json=rule, headers=auth)
        else:
            await client.delete(f"/admin/chaos/{provider}", headers=auth)


async def send(client, clock, rec):
    headers = {"X-Tenant": rec["tenant"], "X-Feature": rec["feature"], "X-Request-Id": rec["id"],
               "X-Request-Class": rec["class"], "Idempotency-Key": rec["id"]}
    body = {"messages": [{"role": "user", "content": rec["prompt"]}], "max_tokens": 120}
    t0 = clock.now()
    r = await client.post("/v1/chat/completions", json=body, headers=headers)
    rec["latency"] = clock.now() - t0
    rec["status"] = r.status_code
    rec["cost"] = float(r.headers.get("X-Gateway-Cost-USD", 0))
    data = r.json()
    rec["attempts"] = (data.get("gateway") or data.get("error") or {}).get("attempts", [])
    rec["provider"] = r.headers.get("X-Gateway-Provider")
    if r.status_code == 202:
        rec["job"] = data["id"]


async def run(name, resilience, cfg, redis, scale, seed):
    clock = ScaledClock(scale)
    cfg = replace(cfg, resilience=resilience, redis_prefix=f"bench:{name}:{uuid.uuid4().hex[:6]}")
    providers = build_providers(cfg, clock, seed)
    app = create_app(cfg, redis=redis, clock=clock, providers=providers, seed=seed, admin_token="bench", start_worker=False)
    gw = app.state.gw
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=None) as client:
        stop = asyncio.Event()
        worker = asyncio.create_task(gw.queue.run(stop, idle_s=0.25))
        chaos = asyncio.create_task(chaos_driver(client, clock))
        recs, tasks = arrivals(seed), []
        for rec in recs:
            await asyncio.sleep(max(0.0, clock.to_real(rec["t"] - clock.now())))
            tasks.append(asyncio.create_task(send(client, clock, rec)))
        await asyncio.gather(*tasks)
        while await gw.queue.depth() and clock.now() < DURATION + DRAIN:
            await clock.sleep(1)
        stop.set()
        await asyncio.gather(worker, chaos, return_exceptions=True)
        for rec in recs:
            if rec.get("job"):
                job = await gw.queue.get(rec["job"])
                rec["job_status"] = job["status"]
                rec["cost"] += job.get("cost_usd", 0.0)
                if job["status"] == "done":
                    rec["done_after"] = job["finished_at"] - rec["t"]
    return summarise(name, recs, gw.events)


def pct(values, q):
    return percentile(sorted(values), q)


def summarise(name, recs, events):
    inter = [r for r in recs if r["class"] == "interactive"]
    batch = [r for r in recs if r["class"] == "batch"]
    ok = [r for r in inter if r["status"] == 200]
    lat = [r["latency"] for r in ok]
    batch_done = [r for r in batch if r["status"] == 200 or r.get("job_status") == "done"]
    queued = [r for r in batch if r.get("job")]
    hedged = [r for r in inter if any(a["hedge"] for a in r["attempts"])]
    cancelled_cost = sum(a["cost_usd"] for r in recs for a in r["attempts"] if a["error"] == "cancelled")
    total_cost = sum(r["cost"] for r in recs)
    by_second = {}
    for r in inter:
        b = by_second.setdefault(int(r["t"]) // 5 * 5, [0, 0])
        b[0] += r["status"] == 200
        b[1] += 1
    return {
        "strategy": name,
        "interactive": {"requests": len(inter), "succeeded": len(ok), "availability": len(ok) / len(inter),
                        "p50_s": pct(lat, 0.5), "p95_s": pct(lat, 0.95), "hedged": len(hedged)},
        "batch": {"requests": len(batch), "completed": len(batch_done), "completion": len(batch_done) / len(batch),
                  "queued": len(queued), "queued_median_wait_s": statistics.median([r["done_after"] for r in queued if "done_after" in r]) if any("done_after" in r for r in queued) else None},
        "cost_usd": {"total": total_cost, "hedge_losers": cancelled_cost,
                     "by_tenant": {t: sum(r["cost"] for r in recs if r["tenant"] == t) for t, _ in TENANTS},
                     "by_feature": {f: sum(r["cost"] for r in recs if r["feature"] == f) for f in ("chat", "search", "summarize")}},
        "success_by_5s": {k: v[0] / v[1] for k, v in sorted(by_second.items())},
        "breaker_events": [{**e, "t": round(e["t"], 1)} for e in events],
    }


def detection(events, provider, to, after):
    e = next((e for e in events if e["provider"] == provider and e["to"] == to and e["t"] >= after), None)
    return round(e["t"] - after, 1) if e else None


def table(results) -> str:
    rows = ["| Setup | Interactive availability | p50 | p95 | Batch completed | Spend |",
            "|---|---:|---:|---:|---:|---:|"]
    for r in results:
        i, b = r["interactive"], r["batch"]
        rows.append(f"| {r['strategy']} | **{i['availability']:.1%}** ({i['succeeded']}/{i['requests']}) | {i['p50_s']:.2f} s | "
                    f"{i['p95_s']:.2f} s | {b['completion']:.1%} ({b['completed']}/{b['requests']}) | ${r['cost_usd']['total']:.2f} |")
    return "\n".join(rows)


def timeline_svg(results) -> str:
    """Success rate per 5 s for each setup, with the outages shaded and alpha's breaker underneath."""
    W, H, L, R, T, B = 900, 380, 60, 20, 30, 110
    pw, ph = W - L - R, H - T - B
    x = lambda t: L + pw * t / DURATION
    y = lambda v: T + ph * (1 - v)
    colours = {"direct": "#94a3b8", "client retries": "#f59e0b", "gateway": "#2563eb"}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui, sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>']
    for start, end, provider, rule in SCENARIO:
        label = f"{provider} {'down' if rule.get('error_rate') else 'slow'}"
        out.append(f'<rect x="{x(start):.1f}" y="{T}" width="{x(end) - x(start):.1f}" height="{ph}" fill="#fee2e2" fill-opacity="0.55"/>')
        out.append(f'<text x="{x(start) + 3:.1f}" y="{T + 12 + 13 * ["alpha", "beta", "gamma"].index(provider)}" fill="#b91c1c">{label}</text>')
    for v in (0, 0.25, 0.5, 0.75, 1):
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#e5e7eb"/>')
        out.append(f'<text x="{L - 8}" y="{y(v) + 4:.1f}" text-anchor="end" fill="#6b7280">{v:.0%}</text>')
    for t in range(0, DURATION + 1, 30):
        out.append(f'<text x="{x(t):.1f}" y="{T + ph + 16}" text-anchor="middle" fill="#6b7280">{t}s</text>')
    for r in results:
        pts = " ".join(f"{x(int(k) + 2.5):.1f},{y(v):.1f}" for k, v in r["success_by_5s"].items())
        out.append(f'<polyline points="{pts}" fill="none" stroke="{colours[r["strategy"]]}" stroke-width="2.5"/>')
    lx = L
    for name, c in colours.items():
        out.append(f'<rect x="{lx}" y="{H - 62}" width="14" height="4" fill="{c}"/><text x="{lx + 20}" y="{H - 56}" fill="#111827">{name}</text>')
        lx += 150
    out.append(f'<text x="{L}" y="{T - 10}" fill="#111827" font-weight="600">Interactive requests answered, per 5 s</text>')
    band_y, state, since = H - 36, "closed", 0.0
    fill = {"closed": "#16a34a", "half_open": "#eab308", "open": "#dc2626"}
    changes = [e for e in results[-1]["breaker_events"] if e["provider"] == "alpha"] + [{"t": DURATION, "to": None}]
    for e in changes:
        t = min(e["t"], DURATION)
        if t > since:
            out.append(f'<rect x="{x(since):.1f}" y="{band_y}" width="{x(t) - x(since):.1f}" height="12" fill="{fill[state]}"/>')
        state, since = e["to"] or state, t
    out.append(f'<text x="{L - 8}" y="{band_y + 10}" text-anchor="end" fill="#111827">alpha</text>')
    out.append(f'<text x="{L}" y="{H - 8}" fill="#6b7280">gateway breaker for alpha: green closed · yellow half open · red open</text>')
    out.append("</svg>")
    return "\n".join(out)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--scale", type=float, default=0.05, help="real seconds per simulated second")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fake-redis", action="store_true")
    ap.add_argument("--out", default="bench/out")
    ap.add_argument("--check", action="store_true", help="exit 1 unless the gateway beats a direct call (for CI)")
    args = ap.parse_args()
    if args.fake_redis:
        import fakeredis.aioredis
        redis = fakeredis.aioredis.FakeRedis()
    else:
        import os

        from redis.asyncio import Redis
        redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    cfg = load_config(args.config)
    results = [await run(name, res, cfg, redis, args.scale, args.seed) for name, res in STRATEGIES.items()]
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=2))
    (out / "timeline.svg").write_text(timeline_svg(results))
    print(table(results))
    ev = results[-1]["breaker_events"]
    print("\nGateway breaker events:")
    for e in ev:
        print(f"  t={e['t']:>6}s  {e['provider']:<6} {e['from']:>9} -> {e['to']:<9} {e['reason']}")
    print(f"\nalpha outage at 60 s: opened after {detection(ev, 'alpha', 'open', 60)} s; "
          f"closed {detection(ev, 'alpha', 'closed', 120)} s after it recovered at 120 s")
    print(f"alpha slow from 150 s: opened after {detection(ev, 'alpha', 'open', 150)} s")
    g = results[-1]
    print(f"hedged interactive requests: {g['interactive']['hedged']}; spend on cancelled hedge legs "
          f"${g['cost_usd']['hedge_losers']:.2f} of ${g['cost_usd']['total']:.2f}")
    if args.check:
        direct = results[0]
        ok = (g["interactive"]["availability"] > direct["interactive"]["availability"] + 0.05
              and g["batch"]["completion"] == 1.0 and detection(ev, "alpha", "open", 60) is not None)
        print("CHECK", "passed" if ok else "FAILED")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
