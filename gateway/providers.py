"""Model providers behind one interface.

OpenAI, Groq, Ollama and vLLM all
accept the same /v1/chat/completions request, so one small HTTP adapter covers
all of them. MockProvider stands in for a model in tests and in the chaos
benchmark; Chaos wraps any provider to inject failures and latency on demand.
"""
import math
import os
import random
from dataclasses import dataclass

import httpx

from .clock import Clock
from .errors import ProviderError, classify_http


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


def estimate_tokens(text: str) -> int:
    """Rough count (about four characters a token) for when a provider reports no usage."""
    return max(1, len(text) // 4)


def prompt_text(request: dict) -> str:
    parts = []
    for m in request.get("messages", []):
        c = m.get("content", "")
        parts.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict)))
    return "\n".join(parts)


class Provider:
    name: str
    model: str
    price_in: float  # USD per million input tokens
    price_out: float  # USD per million output tokens

    async def complete(self, request: dict, timeout: float) -> Completion:
        raise NotImplementedError

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.price_in + output_tokens * self.price_out) / 1e6


class MockProvider(Provider):
    """A stand-in model with seeded, lognormal latency and a base error rate."""

    def __init__(self, name, *, model="mock", price_in=1.0, price_out=4.0, median_latency_s=0.8,
                 latency_sigma=0.4, error_rate=0.0, seed=0, clock: Clock | None = None):
        self.name, self.model = name, model
        self.price_in, self.price_out = price_in, price_out
        self.median_latency_s, self.latency_sigma, self.error_rate = median_latency_s, latency_sigma, error_rate
        self.rng = random.Random(seed)
        self.clock = clock or Clock()

    async def complete(self, request: dict, timeout: float) -> Completion:
        latency = self.median_latency_s * math.exp(self.latency_sigma * self.rng.gauss(0, 1))
        if self.rng.random() < self.error_rate:
            await self.clock.sleep(min(latency, 0.2))
            raise ProviderError("server_error", f"{self.name}: simulated 503")
        await self.clock.sleep(latency)
        prompt = prompt_text(request)
        out = min(int(request.get("max_tokens") or 120), 60 + len(prompt) % 60)
        return Completion(text=f"[{self.name}] answer to: {prompt[:60]}", input_tokens=estimate_tokens(prompt),
                          output_tokens=out, model=self.model)


class OpenAICompatProvider(Provider):
    """Any endpoint that speaks POST {base_url}/chat/completions."""

    def __init__(self, name, *, base_url, model, api_key_env=None, price_in=0.0, price_out=0.0,
                 client: httpx.AsyncClient | None = None):
        self.name, self.model = name, model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.price_in, self.price_out = price_in, price_out
        self.client = client or httpx.AsyncClient()

    async def complete(self, request: dict, timeout: float) -> Completion:
        headers = {}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ProviderError("auth", f"{self.name}: {self.api_key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        payload = {k: v for k, v in request.items() if k not in ("model", "stream")}
        payload.update(model=self.model, stream=False)
        try:
            r = await self.client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", f"{self.name}: {e}") from e
        except httpx.TransportError as e:
            raise ProviderError("server_error", f"{self.name}: {e}") from e
        if r.status_code != 200:
            raise ProviderError(classify_http(r.status_code, r.text), f"{self.name}: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        data = r.json()
        usage = data.get("usage") or {}
        text = data["choices"][0]["message"].get("content") or ""
        return Completion(
            text=text,
            input_tokens=usage.get("prompt_tokens") or estimate_tokens(prompt_text(request)),
            output_tokens=usage.get("completion_tokens") or estimate_tokens(text),
            model=data.get("model", self.model),
        )


class Chaos(Provider):
    """Wraps a provider and injects errors or latency until the rule expires or is cleared."""

    def __init__(self, inner: Provider, clock: Clock, seed: int = 0):
        self.inner, self.clock = inner, clock
        self.rng = random.Random(seed)
        self.rule: dict | None = None

    name = property(lambda self: self.inner.name)
    model = property(lambda self: self.inner.model)
    price_in = property(lambda self: self.inner.price_in)
    price_out = property(lambda self: self.inner.price_out)

    def set(self, *, error_rate=0.0, error_kind="server_error", extra_latency_ms=0, duration_s=None):
        ProviderError(error_kind)  # validates the kind
        until = self.clock.now() + duration_s if duration_s else None
        self.rule = {"error_rate": float(error_rate), "error_kind": error_kind,
                     "extra_latency_ms": float(extra_latency_ms), "until": until}
        return self.active()

    def clear(self):
        self.rule = None

    def active(self) -> dict | None:
        if self.rule and self.rule["until"] is not None and self.clock.now() >= self.rule["until"]:
            self.rule = None
        return self.rule

    async def complete(self, request: dict, timeout: float) -> Completion:
        rule = self.active()
        if rule and rule["extra_latency_ms"]:
            await self.clock.sleep(rule["extra_latency_ms"] / 1000)
        if rule and self.rng.random() < rule["error_rate"]:
            raise ProviderError(rule["error_kind"], f"{self.name}: injected by chaos")
        return await self.inner.complete(request, timeout)
