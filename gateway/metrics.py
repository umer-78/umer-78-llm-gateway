"""Prometheus metrics. One registry per app, so tests can build several apps."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self):
        r = self.registry = CollectorRegistry()
        self.requests = Counter("llm_gateway_requests", "Provider calls by outcome",
                                ["provider", "request_class", "outcome"], registry=r)
        self.latency = Histogram("llm_gateway_provider_latency_seconds", "Provider call latency",
                                 ["provider"], registry=r,
                                 buckets=(0.25, 0.5, 1, 1.5, 2, 3, 5, 8, 12, 20, 30))
        self.breaker_state = Gauge("llm_gateway_breaker_state", "0 closed, 1 half open, 2 open",
                                   ["provider"], registry=r)
        self.breaker_changes = Counter("llm_gateway_breaker_transitions", "Breaker state changes",
                                       ["provider", "to_state"], registry=r)
        self.failovers = Counter("llm_gateway_failovers", "Requests moved to the next provider",
                                 ["from_provider", "to_provider", "request_class"], registry=r)
        self.hedges = Counter("llm_gateway_hedges", "Hedged requests and which side won",
                              ["request_class", "winner"], registry=r)
        self.responses = Counter("llm_gateway_responses", "Responses returned to callers",
                                 ["request_class", "status"], registry=r)
        self.queue_depth = Gauge("llm_gateway_queue_depth", "Deferred requests waiting", registry=r)
        self.cost = Counter("llm_gateway_cost_usd", "Spend attributed by tenant and feature",
                            ["tenant", "feature", "provider"], registry=r)
        self.tokens = Counter("llm_gateway_tokens", "Tokens by tenant, feature and direction",
                              ["tenant", "feature", "provider", "direction"], registry=r)
