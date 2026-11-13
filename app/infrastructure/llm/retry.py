"""Retry policies for language-model calls (Strategy)."""

import random

from app.domain.errors import LLMTimeoutError, LLMUnavailableError
from app.domain.ports import RetryPolicy


class ExponentialBackoff(RetryPolicy):
    """Retry transient failures with doubling, capped, jittered delays.

    Only timeouts and unavailability are transient. A parse error or a rejected
    request would fail the same way again, so it is never retried.
    """

    def __init__(
        self, base: float = 0.5, cap: float = 8.0, jitter: float = 0.25, max_attempts: int = 3
    ) -> None:
        self._base = base
        self._cap = cap
        self._jitter = jitter
        self._max_attempts = max_attempts

    def should_retry(self, error: Exception, attempt: int) -> bool:
        """Retry a timeout or unavailability while attempts remain."""
        transient = isinstance(error, (LLMTimeoutError, LLMUnavailableError))
        return transient and attempt < self._max_attempts

    def delay(self, attempt: int) -> float:
        """Return ``min(cap, base * 2**(attempt-1))`` stretched by up to ``jitter``."""
        backoff = min(self._cap, self._base * 2 ** (attempt - 1))
        return backoff * (1 + random.uniform(0, self._jitter))


class NoRetry(RetryPolicy):
    """Never retry."""

    def should_retry(self, error: Exception, attempt: int) -> bool:
        """Always False."""
        return False

    def delay(self, attempt: int) -> float:
        """Zero; never consulted."""
        return 0.0
