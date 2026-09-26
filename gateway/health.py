"""Rolling health per provider, kept in Redis so a restart does not wipe it.

Each call is one member of a sorted set scored by time; the window is whatever
sits inside the last `window_s` seconds. A few hundred members per provider is
cheap to read back and summarise, and it lets p95 come from real samples rather
than from a pre-bucketed histogram.
"""
import uuid
from dataclasses import dataclass, field

from .clock import Clock


@dataclass
class Health:
    requests: int = 0
    errors: int = 0
    error_rate: float = 0.0
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None
    by_kind: dict = field(default_factory=dict)


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest rank: the smallest value with at least q of the samples at or below it."""
    if not sorted_values:
        return None
    rank = max(1, -(-len(sorted_values) * q // 1))  # ceil(n * q)
    return sorted_values[int(rank) - 1]


def summarise(rows: list[tuple[bool, str | None, float]]) -> Health:
    if not rows:
        return Health()
    latencies = sorted(r[2] for r in rows)
    errors = [r for r in rows if not r[0]]
    by_kind: dict[str, int] = {}
    for _, kind, _ in errors:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return Health(
        requests=len(rows), errors=len(errors), error_rate=len(errors) / len(rows),
        p50=percentile(latencies, 0.50), p95=percentile(latencies, 0.95), p99=percentile(latencies, 0.99),
        by_kind=by_kind,
    )


class HealthTracker:
    def __init__(self, redis, clock: Clock, window_s: float = 30.0, prefix: str = "gw"):
        self.redis, self.clock, self.window_s, self.prefix = redis, clock, window_s, prefix

    def _key(self, provider: str) -> str:
        return f"{self.prefix}:health:{provider}"

    async def record(self, provider: str, ok: bool, latency_s: float, kind: str | None = None) -> None:
        now = self.clock.now()
        member = f"{now:.6f}|{uuid.uuid4().hex[:8]}|{int(ok)}|{kind or '-'}|{latency_s:.4f}"
        key = self._key(provider)
        async with self.redis.pipeline(transaction=False) as p:
            p.zadd(key, {member: now})
            p.zremrangebyscore(key, "-inf", now - self.window_s)
            await p.execute()

    async def snapshot(self, provider: str, since: float | None = None) -> Health:
        """Health over the window, ignoring anything before `since` (the breaker's last reset)."""
        lo = self.clock.now() - self.window_s
        if since is not None:
            lo = max(lo, since)
        rows = []
        for m in await self.redis.zrangebyscore(self._key(provider), lo, "+inf"):
            _, _, ok, kind, latency = (m.decode() if isinstance(m, bytes) else m).split("|")
            rows.append((ok == "1", None if kind == "-" else kind, float(latency)))
        return summarise(rows)
