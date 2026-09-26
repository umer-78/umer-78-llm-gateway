"""What went wrong, in terms the router can act on.

Each kind answers two questions: does it say something about the provider's
health, and would another provider plausibly succeed?
"""

# kind: (counts against the provider, worth trying the next provider)
KINDS = {
    "rate_limited": (True, True),
    "timeout": (True, True),
    "server_error": (True, True),
    # A bad key is this provider's problem, not the request's.
    "auth": (True, True),
    # The request's problem: another provider would refuse or reject it too.
    "content_filter": (False, False),
    "bad_request": (False, False),
}


class ProviderError(Exception):
    def __init__(self, kind: str, message: str = "", status: int | None = None):
        if kind not in KINDS:
            raise ValueError(f"unknown error kind {kind!r}")
        super().__init__(message or kind)
        self.kind = kind
        self.status = status


def counts_against_provider(kind: str) -> bool:
    return KINDS[kind][0]


def worth_failover(kind: str) -> bool:
    return KINDS[kind][1]


def classify_http(status: int, body: str = "") -> str:
    """Map an OpenAI-compatible HTTP error to a kind."""
    text = body.lower()
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if status in (408, 504):
        return "timeout"
    if status >= 500:
        return "server_error"
    if status == 400 and ("content_filter" in text or "content policy" in text or "safety" in text):
        return "content_filter"
    return "bad_request"
