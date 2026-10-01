"""Per-tenant rate limiting.

A token bucket per scope (a tenant, or a tenant-and-feature pair), kept in Redis
so every gateway replica shares one limit. Each scope refills at a steady rate
and may burst up to a cap; a request that cannot pay a token is refused with a
429 and a Retry-After telling the caller when a token will be free.

The bucket is read-modify-written inside a WATCH transaction, so two replicas
spending from the same bucket at once cannot overspend: whoever writes second
retries against the fresh value.
"""
from __future__ import annotations

from dataclasses import dataclass

from redis.exceptions import WatchError

from .clock import Clock


@dataclass
class RateLimitConfig:
    """Off by default: the gateway rate-limits only when a store opts in."""
    enabled: bool = False
    rate_per_s: float = 0.0   # tokens added per second (the sustained rate)
    burst: int = 0            # bucket capacity (the most that can arrive at once)
    per_feature: bool = False  # limit each tenant+feature separately, not the whole tenant

    def __post_init__(self):
        if self.enabled and (self.rate_per_s <= 0 or self.burst <= 0):
            raise ValueError("rate_limit needs rate_per_s > 0 and burst > 0 when enabled")


class RateLimiter:
    def __init__(self, redis, clock: Clock, cfg: RateLimitConfig, prefix: str = "gw"):
        self.redis, self.clock, self.cfg, self.prefix = redis, clock, cfg, prefix
        # a bucket that has been full and idle this long is forgotten; it refills to full anyway
        self._ttl_ms = int((cfg.burst / cfg.rate_per_s) * 1000) + 1000 if cfg.enabled else 1000

    def scope(self, tenant: str, feature: str | None) -> str:
        return f"{tenant}:{feature}" if (self.cfg.per_feature and feature) else tenant

    async def check(self, tenant: str, feature: str | None = None, cost: float = 1.0) -> tuple[bool, float]:
        """Try to spend `cost` tokens for this scope. Returns (allowed, retry_after_seconds)."""
        if not self.cfg.enabled:
            return True, 0.0
        key = f"{self.prefix}:rl:{self.scope(tenant, feature)}"
        rate, burst, now = self.cfg.rate_per_s, float(self.cfg.burst), self.clock.now()
        async with self.redis.pipeline() as pipe:
            for _ in range(8):
                try:
                    await pipe.watch(key)
                    raw = await pipe.hmget(key, "tokens", "ts")
                    tokens = float(raw[0]) if raw[0] is not None else burst
                    ts = float(raw[1]) if raw[1] is not None else now
                    tokens = min(burst, tokens + max(0.0, now - ts) * rate)
                    allowed = tokens >= cost
                    if allowed:
                        tokens -= cost
                    pipe.multi()
                    pipe.hset(key, mapping={"tokens": tokens, "ts": now})
                    pipe.pexpire(key, self._ttl_ms)
                    await pipe.execute()
                    retry = 0.0 if allowed else round((cost - tokens) / rate, 3)
                    return allowed, retry
                except WatchError:
                    continue  # another replica wrote first; re-read and try again
        # the bucket is too contended to settle; fail open rather than block a real request
        return True, 0.0
