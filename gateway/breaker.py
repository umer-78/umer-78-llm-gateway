"""A circuit breaker per provider: closed, open, half open.

Closed: traffic flows, and the breaker trips when the window shows too many
errors or a p95 above budget. Open: no traffic until the cooldown passes.
Half open: a small share of traffic probes the provider; a run of successes
closes the breaker, any failure opens it again. That last state is what makes
the gateway heal on its own instead of waiting for a human.

State lives in Redis, so every gateway replica and every restart agree on it.
"""
import random
from dataclasses import dataclass

from redis.exceptions import WatchError

from .clock import Clock
from .health import HealthTracker

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
STATE_CODE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


@dataclass
class BreakerConfig:
    error_rate: float = 0.5  # trip when at least this share of calls in the window failed
    consecutive_failures: int = 5  # or on this many failures in a row, which the window dilutes
    p95_budget_s: float = 5.0  # or when p95 latency is above this (default; providers can set their own)
    min_requests: int = 10  # never judge a provider on fewer calls than this
    cooldown_s: float = 15.0  # time open before the first probe
    probe_fraction: float = 0.1  # share of traffic sent to a half-open provider
    probes_to_close: int = 3  # consecutive probe successes that close it


class Breakers:
    def __init__(self, redis, clock: Clock, health: HealthTracker, cfg: BreakerConfig,
                 prefix: str = "gw", on_change=None, budgets: dict[str, float] | None = None):
        self.redis, self.clock, self.health, self.cfg, self.prefix = redis, clock, health, cfg, prefix
        self.on_change = on_change  # callback(provider, old, new, reason)
        # A slow cheap model and a fast frontier model cannot share one latency budget.
        self.budgets = budgets or {}

    def _key(self, provider: str) -> str:
        return f"{self.prefix}:breaker:{provider}"

    async def state(self, provider: str) -> dict:
        raw = await self.redis.hgetall(self._key(provider))
        s = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
        return {
            "state": s.get("state", CLOSED),
            "since": float(s.get("since", 0.0)),  # health before this moment is ignored
            "opened_at": float(s.get("opened_at", 0.0)),
            "probe_ok": int(s.get("probe_ok", 0)),
            "fails": int(s.get("fails", 0)),
            "reason": s.get("reason", ""),
        }

    async def _set(self, provider: str, old: str, new: str, reason: str, **fields) -> bool:
        """Move old -> new only if the breaker is still in `old`. Concurrent requests
        that see the same trip race to make it; exactly one wins and reports it."""
        fields.setdefault("fails", 0)
        key = self._key(provider)
        async with self.redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                current = await pipe.hget(key, "state")
                current = current.decode() if isinstance(current, bytes) else (current or CLOSED)
                if current != old:
                    return False
                pipe.multi()
                pipe.hset(key, mapping={"state": new, "reason": reason, **{k: str(v) for k, v in fields.items()}})
                await pipe.execute()
            except WatchError:
                return False
        if self.on_change and old != new:
            self.on_change(provider, old, new, reason)
        return True

    async def allow(self, provider: str, rng: random.Random) -> tuple[bool, bool]:
        """(may this request use the provider, is it a probe)."""
        s = await self.state(provider)
        now = self.clock.now()
        if s["state"] == OPEN:
            if now - s["opened_at"] < self.cfg.cooldown_s:
                return False, False
            await self._set(provider, OPEN, HALF_OPEN, "cooldown over", since=now, probe_ok=0)
            s = await self.state(provider)
            if s["state"] == OPEN:  # another request re-opened it in between
                return False, False
        if s["state"] == HALF_OPEN:
            return rng.random() < self.cfg.probe_fraction, True
        return True, False

    async def after_call(self, provider: str, ok: bool, probe: bool) -> None:
        s = await self.state(provider)
        now = self.clock.now()
        if s["state"] == HALF_OPEN:
            if not probe:
                return
            if not ok:
                await self._set(provider, HALF_OPEN, OPEN, "probe failed", opened_at=now, probe_ok=0)
            elif s["probe_ok"] + 1 >= self.cfg.probes_to_close:
                # `since` restarts the window: the failures that opened the breaker
                # must not trip it again the moment it closes.
                await self._set(provider, HALF_OPEN, CLOSED, f"{self.cfg.probes_to_close} probes succeeded", since=now, probe_ok=0)
            else:
                await self.redis.hincrby(self._key(provider), "probe_ok", 1)
            return
        if s["state"] != CLOSED:
            return
        if not ok:
            fails = await self.redis.hincrby(self._key(provider), "fails", 1)
            if fails >= self.cfg.consecutive_failures:
                await self._set(provider, CLOSED, OPEN, f"{fails} failures in a row", opened_at=now)
                return
        elif s["fails"]:
            await self.redis.hset(self._key(provider), "fails", 0)
        h = await self.health.snapshot(provider, since=s["since"])
        if h.requests < self.cfg.min_requests:
            return
        budget = self.budgets.get(provider, self.cfg.p95_budget_s)
        if h.error_rate >= self.cfg.error_rate:
            await self._set(provider, CLOSED, OPEN, f"error rate {h.error_rate:.0%} over {h.requests} calls", opened_at=now)
        elif h.p95 is not None and h.p95 > budget:
            await self._set(provider, CLOSED, OPEN, f"p95 {h.p95:.2f}s over its {budget:.2f}s budget", opened_at=now)
