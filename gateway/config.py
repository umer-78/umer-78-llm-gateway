"""Gateway configuration, loaded from YAML (see config.yaml)."""
from dataclasses import dataclass, field

import yaml

from .breaker import BreakerConfig
from .limiter import RateLimitConfig


@dataclass
class ClassConfig:
    """How one kind of request is served."""
    preference: list[str]  # providers in the order to try them
    timeout_s: float = 10.0
    hedge_after_s: float | None = None  # latency-sensitive: race a second provider after this
    deferrable: bool = False  # queue instead of failing when every provider is down


@dataclass
class Resilience:
    """Switches for each mechanism, so the benchmark can measure what each one buys."""
    breakers: bool = True
    failover: bool = True
    hedging: bool = True
    retries: int = 0  # extra attempts on the same provider before moving on
    queue: bool = True


@dataclass
class QueueConfig:
    max_attempts: int = 8
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0


@dataclass
class Config:
    providers: list[dict]
    classes: dict[str, ClassConfig]
    default_class: str = "interactive"
    window_s: float = 30.0
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    resilience: Resilience = field(default_factory=Resilience)
    queue: QueueConfig = field(default_factory=QueueConfig)
    idempotency_ttl_s: int = 86400
    redis_prefix: str = "gw"
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)

    def __post_init__(self):
        names = {p["name"] for p in self.providers}
        for cname, c in self.classes.items():
            unknown = [p for p in c.preference if p not in names]
            if unknown:
                raise ValueError(f"class {cname!r} prefers unknown providers {unknown}")
        if self.default_class not in self.classes:
            raise ValueError(f"default_class {self.default_class!r} is not a configured class")


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config(
        providers=raw["providers"],
        classes={k: ClassConfig(**v) for k, v in raw["classes"].items()},
        default_class=raw.get("default_class", "interactive"),
        window_s=raw.get("window_s", 30.0),
        breaker=BreakerConfig(**raw.get("breaker", {})),
        resilience=Resilience(**raw.get("resilience", {})),
        queue=QueueConfig(**raw.get("queue", {})),
        idempotency_ttl_s=raw.get("idempotency_ttl_s", 86400),
        redis_prefix=raw.get("redis_prefix", "gw"),
        rate_limit=RateLimitConfig(**raw.get("rate_limit", {})),
    )
