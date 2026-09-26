from gateway.config import ClassConfig, Resilience
from gateway.breaker import OPEN

from conftest import CHAT, CLASSES, Scripted, make_router


async def test_a_failed_provider_fails_over_to_the_next(redis, fast_clock):
    alpha = Scripted("alpha", fast_clock, ["server_error"])
    beta = Scripted("beta", fast_clock)
    router, _, metrics = make_router([alpha, beta, Scripted("gamma", fast_clock)], CLASSES, redis, fast_clock)
    out = await router.route(CHAT, "interactive")
    assert out.provider == "beta"
    assert [(a.provider, a.kind) for a in out.attempts] == [("alpha", "server_error"), ("beta", None)]
    assert metrics.failovers.labels("alpha", "beta", "interactive")._value.get() == 1


async def test_the_requests_own_errors_do_not_fail_over(redis, fast_clock):
    alpha = Scripted("alpha", fast_clock, ["content_filter"])
    beta = Scripted("beta", fast_clock)
    router, _, _ = make_router([alpha, beta, Scripted("gamma", fast_clock)], CLASSES, redis, fast_clock)
    out = await router.route(CHAT, "interactive")
    assert out.completion is None and out.error_kind == "content_filter"
    assert beta.calls == 0


async def test_an_open_breaker_is_skipped_without_a_call(redis, fast_clock):
    alpha, beta = Scripted("alpha", fast_clock), Scripted("beta", fast_clock)
    router, breakers, _ = make_router([alpha, beta, Scripted("gamma", fast_clock)], CLASSES, redis, fast_clock)
    await breakers._set("alpha", "closed", OPEN, "test", opened_at=fast_clock.now())
    out = await router.route(CHAT, "interactive")
    assert out.provider == "beta" and alpha.calls == 0


async def test_a_slow_call_is_hedged_and_the_loser_is_cancelled_and_billed(redis, fast_clock):
    alpha = Scripted("alpha", fast_clock, [("ok", 5.0)])
    beta = Scripted("beta", fast_clock, [("ok", 0.3)])
    router, _, metrics = make_router([alpha, beta], CLASSES, redis, fast_clock)
    t0 = fast_clock.now()
    out = await router.route(CHAT, "hedged")
    assert out.provider == "beta"
    assert fast_clock.now() - t0 < 3.0, "the caller waited for the slow provider"
    kinds = {a.provider: a.kind for a in out.attempts}
    assert kinds == {"alpha": "cancelled", "beta": None}
    cancelled = next(a for a in out.attempts if a.kind == "cancelled")
    assert cancelled.cost_usd > 0, "a hedge that lost still costs money"
    assert metrics.hedges.labels("hedged", "hedge")._value.get() == 1


async def test_a_fast_answer_is_not_hedged(redis, fast_clock):
    alpha = Scripted("alpha", fast_clock, [("ok", 0.2)])
    beta = Scripted("beta", fast_clock)
    router, _, _ = make_router([alpha, beta], CLASSES, redis, fast_clock)
    out = await router.route(CHAT, "hedged")
    assert out.provider == "alpha" and beta.calls == 0


async def test_timeouts_count_as_failures_and_fail_over(redis, fast_clock):
    classes = {"interactive": ClassConfig(preference=["alpha", "beta"], timeout_s=2)}
    alpha = Scripted("alpha", fast_clock, [("ok", 9.0)])
    beta = Scripted("beta", fast_clock)
    router, _, _ = make_router([alpha, beta], classes, redis, fast_clock)
    out = await router.route(CHAT, "interactive")
    assert [a.kind for a in out.attempts] == ["timeout", None]


async def test_retries_stay_on_one_provider_when_failover_is_off(redis, fast_clock):
    alpha = Scripted("alpha", fast_clock, ["rate_limited", "rate_limited", "ok"])
    router, _, _ = make_router([alpha, Scripted("beta", fast_clock), Scripted("gamma", fast_clock)], CLASSES, redis,
                               fast_clock, resilience=Resilience(failover=False, breakers=False, hedging=False, retries=2))
    out = await router.route(CHAT, "interactive")
    assert out.provider == "alpha" and alpha.calls == 3


async def test_nothing_allowed_reports_unavailable(redis, fast_clock):
    providers = [Scripted(n, fast_clock) for n in ("alpha", "beta", "gamma")]
    router, breakers, _ = make_router(providers, CLASSES, redis, fast_clock)
    for n in ("alpha", "beta", "gamma"):
        await breakers._set(n, "closed", OPEN, "test", opened_at=fast_clock.now())
    out = await router.route(CHAT, "interactive")
    assert out.completion is None and out.error_kind == "unavailable" and out.attempts == []


async def test_a_provider_that_always_loses_its_hedge_still_trips_on_latency(redis, fast_clock):
    """Before this, cancelled legs recorded nothing, so a slow provider looked healthy forever."""
    from gateway.breaker import BreakerConfig
    alpha = Scripted("alpha", fast_clock, [("ok", 6.0)] * 12)
    beta = Scripted("beta", fast_clock, latency=0.3)
    router, breakers, _ = make_router([alpha, beta], CLASSES, redis, fast_clock,
                                      breaker_cfg=BreakerConfig(min_requests=10, p95_budget_s=1.2))
    for _ in range(12):
        out = await router.route(CHAT, "hedged")
        assert out.provider == "beta"
    assert (await breakers.state("alpha"))["state"] == OPEN
    out = await router.route(CHAT, "hedged")
    assert [a.provider for a in out.attempts] == ["beta"], "once open, alpha is not even tried"


async def test_a_cancelled_probe_does_not_close_the_breaker(redis, fast_clock):
    from gateway.breaker import BreakerConfig
    alpha = Scripted("alpha", fast_clock, [("ok", 6.0)])
    beta = Scripted("beta", fast_clock, latency=0.3)
    router, breakers, _ = make_router([alpha, beta], CLASSES, redis, fast_clock,
                                      breaker_cfg=BreakerConfig(cooldown_s=0.0, probe_fraction=1.0, probes_to_close=1))
    await breakers._set("alpha", "closed", OPEN, "test", opened_at=fast_clock.now() - 1)
    out = await router.route(CHAT, "hedged")
    assert out.provider == "beta"
    assert (await breakers.state("alpha"))["state"] == OPEN, "a slow probe that lost its hedge is a failed probe"
