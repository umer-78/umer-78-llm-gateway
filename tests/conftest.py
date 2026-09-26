import random

import fakeredis.aioredis
import pytest

from gateway.breaker import BreakerConfig, Breakers
from gateway.clock import Clock, ScaledClock
from gateway.config import ClassConfig, Resilience
from gateway.errors import ProviderError
from gateway.health import HealthTracker
from gateway.metrics import Metrics
from gateway.providers import Completion, Provider
from gateway.router import Router


class ManualClock(Clock):
    """Time that only moves when a test moves it (or something sleeps)."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def to_real(self, seconds: float) -> float:
        return seconds  # nothing waits in real time: every sleep here is instant

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class Scripted(Provider):
    """Answers from a script of outcomes: "ok" or an error kind, optionally with a latency."""

    def __init__(self, name, clock, script=(), latency=0.1, price_in=1.0, price_out=2.0):
        self.name, self.model = name, f"{name}-model"
        self.price_in, self.price_out = price_in, price_out
        self.clock, self.script, self.latency = clock, list(script), latency
        self.calls = 0

    async def complete(self, request, timeout):
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        kind, latency = step if isinstance(step, tuple) else (step, self.latency)
        await self.clock.sleep(latency)
        if kind != "ok":
            raise ProviderError(kind)
        return Completion(text=f"{self.name} answered", input_tokens=10, output_tokens=20, model=self.model)


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def manual_clock():
    return ManualClock()


@pytest.fixture
def fast_clock():
    # 1 simulated second = 5 ms, so races resolve on real asyncio timing in milliseconds
    return ScaledClock(0.005, start=1000.0)


def make_router(providers, classes, redis, clock, resilience=None, breaker_cfg=None, events=None):
    health = HealthTracker(redis, clock, 30)
    on_change = (lambda *e: events.append(e)) if events is not None else None
    breakers = Breakers(redis, clock, health, breaker_cfg or BreakerConfig(), on_change=on_change)
    metrics = Metrics()
    router = Router({p.name: p for p in providers}, classes, breakers, health, metrics, clock,
                    resilience or Resilience(), random.Random(1))
    return router, breakers, metrics


CHAT = {"messages": [{"role": "user", "content": "Where is my order?"}]}
CLASSES = {
    "interactive": ClassConfig(preference=["alpha", "beta", "gamma"], timeout_s=10),
    "hedged": ClassConfig(preference=["alpha", "beta"], timeout_s=10, hedge_after_s=1.0),
}
