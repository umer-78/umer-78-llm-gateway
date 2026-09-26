"""Picks a provider for each request and recovers when it fails.

For a request class the router walks the class's preference list, skipping
providers whose breaker is open (and sending only a probe's share of traffic to
half-open ones). Retryable failures move to the next provider; failures that
belong to the request (a content filter, a malformed request) stop the walk.
Latency-sensitive classes race a second provider once the first has taken
longer than `hedge_after_s`, keep whichever answers first and cancel the other.
"""
import asyncio
import random
from dataclasses import dataclass, field

from .breaker import Breakers
from .clock import Clock
from .config import ClassConfig, Resilience
from .errors import ProviderError, counts_against_provider, worth_failover
from .health import HealthTracker
from .metrics import Metrics
from .providers import Completion, Provider, estimate_tokens, prompt_text


@dataclass
class Attempt:
    provider: str
    ok: bool
    kind: str | None  # error kind, or "cancelled" for a hedge that lost
    latency_s: float
    cost_usd: float = 0.0
    probe: bool = False
    hedge: bool = False  # this call was the second leg of a hedge


@dataclass
class Outcome:
    completion: Completion | None
    provider: str | None
    attempts: list[Attempt] = field(default_factory=list)
    error_kind: str | None = None  # why nothing succeeded; "unavailable" when no provider was allowed

    @property
    def cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.attempts)


class Router:
    def __init__(self, providers: dict[str, Provider], classes: dict[str, ClassConfig], breakers: Breakers,
                 health: HealthTracker, metrics: Metrics, clock: Clock, resilience: Resilience,
                 rng: random.Random | None = None):
        self.providers, self.classes = providers, classes
        self.breakers, self.health, self.metrics, self.clock = breakers, health, metrics, clock
        self.res = resilience
        self.rng = rng or random.Random()

    async def route(self, request: dict, class_name: str) -> Outcome:
        cls = self.classes[class_name]
        order = cls.preference if self.res.failover else cls.preference[:1]
        allowed: list[tuple[str, bool]] = []
        for name in order:
            ok, probe = (await self.breakers.allow(name, self.rng)) if self.res.breakers else (True, False)
            if ok:
                allowed.append((name, probe))
        out = Outcome(completion=None, provider=None)
        if not allowed:
            out.error_kind = "unavailable"
            return out

        i = 0
        while i < len(allowed):
            if self.res.hedging and cls.hedge_after_s is not None and i + 1 < len(allowed):
                done = await self._hedged(allowed[i], allowed[i + 1], request, class_name, cls, out)
                i += 2
            else:
                done = await self._with_retries(allowed[i], request, class_name, cls, out)
                i += 1
            if done:
                return out
            last = out.attempts[-1]
            if last.kind and last.kind != "cancelled" and not worth_failover(last.kind):
                break
            if i < len(allowed):
                self.metrics.failovers.labels(last.provider, allowed[i][0], class_name).inc()
        out.error_kind = next((a.kind for a in reversed(out.attempts) if a.kind != "cancelled"), "unavailable")
        return out

    async def _with_retries(self, target, request, class_name, cls, out: Outcome) -> bool:
        name, probe = target
        for k in range(1 + self.res.retries):
            if await self._call(name, probe, request, class_name, cls, out):
                return True
            if not worth_failover(out.attempts[-1].kind):
                return False
            if k < self.res.retries:
                await self.clock.sleep(0.5 * 2 ** k * self.rng.uniform(0.5, 1.0))
        return False

    async def _hedged(self, first, second, request, class_name, cls, out: Outcome) -> bool:
        a = asyncio.ensure_future(self._call(*first, request, class_name, cls, out))
        done, _ = await asyncio.wait({a}, timeout=self.clock.to_real(cls.hedge_after_s))
        if a in done:
            # The first answered (or failed) in time, so there is nothing to race.
            if a.result():
                return True
            return await self._with_retries(second, request, class_name, cls, out)
        b = asyncio.ensure_future(self._call(*second, request, class_name, cls, out, hedge=True))
        pending = {a, b}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(t.result() for t in done):
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                self.metrics.hedges.labels(class_name, "hedge" if out.provider == second[0] else "first").inc()
                return True
        return False

    async def _call(self, name, probe, request, class_name, cls, out: Outcome, hedge=False) -> bool:
        provider = self.providers[name]
        t0 = self.clock.now()
        kind, completion = None, None
        try:
            completion = await asyncio.wait_for(provider.complete(request, cls.timeout_s), self.clock.to_real(cls.timeout_s))
        except asyncio.TimeoutError:
            kind = "timeout"
        except ProviderError as e:
            kind = e.kind
        except asyncio.CancelledError:
            # Lost a hedge. Assume it is billed as if it had answered: the
            # conservative way to count what hedging costs.
            elapsed = self.clock.now() - t0
            tokens_in = estimate_tokens(prompt_text(request))
            winner_out = out.completion.output_tokens if out.completion else 0
            out.attempts.append(Attempt(name, False, "cancelled", elapsed,
                                        provider.cost(tokens_in, winner_out), probe, hedge))
            # It ran for at least `elapsed`. Dropping that sample hid a slow
            # provider from its breaker: every call was hedged and cancelled,
            # so none ever finished slowly. A cancelled probe is not a success.
            await self.health.record(name, True, elapsed)
            if self.res.breakers:
                await self.breakers.after_call(name, not probe, probe)
            raise
        except Exception:  # a bug in an adapter must not take the gateway down with it
            kind = "server_error"
        latency = self.clock.now() - t0
        ok = completion is not None
        if ok or counts_against_provider(kind):
            await self.health.record(name, ok, latency, kind)
            if self.res.breakers:
                await self.breakers.after_call(name, ok, probe)
        self.metrics.requests.labels(name, class_name, "ok" if ok else kind).inc()
        self.metrics.latency.labels(name).observe(latency)
        cost = provider.cost(completion.input_tokens, completion.output_tokens) if ok else 0.0
        out.attempts.append(Attempt(name, ok, kind, latency, cost, probe, hedge))
        if ok and out.completion is None:
            out.completion, out.provider = completion, name
        return ok
