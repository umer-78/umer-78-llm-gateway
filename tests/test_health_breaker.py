import random

from gateway.breaker import CLOSED, HALF_OPEN, OPEN, BreakerConfig, Breakers
from gateway.health import HealthTracker, percentile


def test_percentile_is_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 0.50) == 50
    assert percentile(values, 0.95) == 95
    assert percentile(values, 0.99) == 99
    assert percentile([7.0], 0.95) == 7
    assert percentile([], 0.95) is None


async def test_window_forgets_old_calls_and_respects_since(redis, manual_clock):
    h = HealthTracker(redis, manual_clock, window_s=30)
    for _ in range(4):
        await h.record("alpha", False, 0.2, "server_error")
    await h.record("alpha", True, 1.0)
    snap = await h.snapshot("alpha")
    assert (snap.requests, snap.errors, snap.by_kind) == (5, 4, {"server_error": 4})
    assert snap.error_rate == 0.8
    manual_clock.t += 31
    assert (await h.snapshot("alpha")).requests == 0
    await h.record("alpha", True, 0.5)
    assert (await h.snapshot("alpha", since=manual_clock.t + 1)).requests == 0


def breakers(redis, clock, events, **cfg):
    health = HealthTracker(redis, clock, window_s=30)
    return health, Breakers(redis, clock, health, BreakerConfig(**cfg), on_change=lambda *e: events.append(e))


async def test_trips_on_error_rate_but_not_on_too_few_calls(redis, manual_clock):
    events = []
    health, b = breakers(redis, manual_clock, events, min_requests=10, error_rate=0.5)
    # two failures, one success: never five in a row, so only the error-rate rule can fire
    pattern = [False, False, True] * 3
    for ok in pattern:
        await health.record("alpha", ok, 0.1, None if ok else "server_error")
        await b.after_call("alpha", ok, False)
    assert (await b.state("alpha"))["state"] == CLOSED, "nine calls is too few to judge"
    await health.record("alpha", False, 0.1, "server_error")
    await b.after_call("alpha", False, False)
    s = await b.state("alpha")
    assert s["state"] == OPEN and "error rate" in s["reason"]
    assert events[-1][:3] == ("alpha", CLOSED, OPEN)
    assert await b.allow("alpha", random.Random(0)) == (False, False)


async def test_a_run_of_failures_trips_even_when_the_window_is_full_of_old_successes(redis, manual_clock):
    """The window rule alone took 15 s to notice a hard outage behind 150 healthy calls."""
    health, b = breakers(redis, manual_clock, [], min_requests=10, error_rate=0.5, consecutive_failures=5)
    for _ in range(150):
        await health.record("alpha", True, 0.8)
        await b.after_call("alpha", True, False)
    for i in range(5):
        await health.record("alpha", False, 0.1, "server_error")
        await b.after_call("alpha", False, False)
    s = await b.state("alpha")
    assert s["state"] == OPEN and s["reason"] == "5 failures in a row"


async def test_a_success_resets_the_run(redis, manual_clock):
    health, b = breakers(redis, manual_clock, [], min_requests=100, consecutive_failures=5)
    for ok in [False] * 4 + [True] + [False] * 4:
        await health.record("alpha", ok, 0.1, None if ok else "timeout")
        await b.after_call("alpha", ok, False)
    assert (await b.state("alpha"))["state"] == CLOSED


async def test_each_provider_has_its_own_latency_budget(redis, manual_clock):
    health = HealthTracker(redis, manual_clock, window_s=30)
    b = Breakers(redis, manual_clock, health, BreakerConfig(min_requests=10, p95_budget_s=3.0), budgets={"gamma": 5.0})
    for name in ("alpha", "gamma"):
        for _ in range(20):
            await health.record(name, True, 4.0)
            await b.after_call(name, True, False)
    assert (await b.state("alpha"))["state"] == OPEN, "4 s is over the default 3 s budget"
    assert (await b.state("gamma"))["state"] == CLOSED, "but within the slow model's own 5 s"


async def test_trips_on_slow_p95_even_when_every_call_succeeds(redis, manual_clock):
    health, b = breakers(redis, manual_clock, [], min_requests=10, p95_budget_s=3.0)
    for i in range(20):
        await health.record("alpha", True, 6.0 if i % 4 == 0 else 1.0)
        await b.after_call("alpha", True, False)
    s = await b.state("alpha")
    assert s["state"] == OPEN and "p95" in s["reason"]


async def test_half_open_probes_heal_it_and_old_failures_do_not_retrip(redis, manual_clock):
    events = []
    health, b = breakers(redis, manual_clock, events, min_requests=10, cooldown_s=15, probe_fraction=1.0, probes_to_close=3)
    for _ in range(10):
        await health.record("alpha", False, 0.1, "server_error")
        await b.after_call("alpha", False, False)
    manual_clock.t += 16
    for _ in range(3):
        allowed, probe = await b.allow("alpha", random.Random(0))
        assert allowed and probe
        await health.record("alpha", True, 0.5)
        await b.after_call("alpha", True, True)
    assert (await b.state("alpha"))["state"] == CLOSED
    # the ten failures are still inside the 30 s window; they must not count again
    await health.record("alpha", True, 0.5)
    await b.after_call("alpha", True, False)
    assert (await b.state("alpha"))["state"] == CLOSED
    assert [e[2] for e in events] == [OPEN, HALF_OPEN, CLOSED]


async def test_a_failed_probe_reopens_straight_away(redis, manual_clock):
    health, b = breakers(redis, manual_clock, [], min_requests=10, cooldown_s=15, probe_fraction=1.0)
    for _ in range(10):
        await health.record("alpha", False, 0.1, "timeout")
        await b.after_call("alpha", False, False)
    manual_clock.t += 16
    assert await b.allow("alpha", random.Random(0)) == (True, True)
    await b.after_call("alpha", False, True)
    s = await b.state("alpha")
    assert s["state"] == OPEN and s["opened_at"] == manual_clock.t


async def test_half_open_sends_only_a_share_of_traffic(redis, manual_clock):
    health, b = breakers(redis, manual_clock, [], min_requests=10, cooldown_s=15, probe_fraction=0.1)
    for _ in range(10):
        await health.record("alpha", False, 0.1, "server_error")
        await b.after_call("alpha", False, False)
    manual_clock.t += 16
    rng = random.Random(42)
    allowed = [(await b.allow("alpha", rng))[0] for _ in range(1000)]
    assert 60 < sum(allowed) < 140


async def test_concurrent_requests_make_a_transition_once(redis, manual_clock):
    import asyncio
    events = []
    health, b = breakers(redis, manual_clock, events, min_requests=10, consecutive_failures=5)
    for _ in range(4):
        await health.record("alpha", False, 0.1, "server_error")
        await b.after_call("alpha", False, False)
    # two failures land at once: both see five in a row, only one may trip it
    await asyncio.gather(b.after_call("alpha", False, False), b.after_call("alpha", False, False))
    assert [e[2] for e in events] == [OPEN]
    manual_clock.t += 16
    await asyncio.gather(*(b.allow("alpha", random.Random(i)) for i in range(5)))
    assert [e[2] for e in events] == [OPEN, HALF_OPEN]
