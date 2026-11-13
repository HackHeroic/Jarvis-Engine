"""Try language-model providers in order until one answers (Chain of Responsibility)."""

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

from pydantic import BaseModel

from app.domain.entities import LLMLocation
from app.domain.errors import LLMError, LLMUnavailableError
from app.domain.ports import LLMProvider, LLMRequest

logger = logging.getLogger(__name__)

_NOTHING_REACHABLE = "No language model is reachable right now."


class FallbackChainProvider(LLMProvider):
    """Asks each available provider in turn; an ``LLMError`` moves on to the next.

    Any other exception is a bug and propagates at once — the next provider is not
    tried. ``skip_local`` (chain-only) leaves out on-device providers for this call.
    """

    name = "fallback_chain"

    def __init__(self, providers: list[LLMProvider]) -> None:
        self._providers = tuple(providers)

    @property
    def providers(self) -> tuple[LLMProvider, ...]:
        """The providers in the order they are tried."""
        return self._providers

    @property
    def location(self) -> LLMLocation:
        """Location of the provider that would be tried first (cloud if none is available)."""
        first = next(self._candidates(skip_local=False), None)
        return first.location if first is not None else LLMLocation.CLOUD

    def is_available(self) -> bool:
        """True when any provider is available."""
        return any(p.is_available() for p in self._providers)

    def has_available(self, location: LLMLocation) -> bool:
        """True when any provider at ``location`` is available."""
        return any(p.location == location and p.is_available() for p in self._providers)

    def _candidates(self, skip_local: bool) -> Iterator[LLMProvider]:
        # Availability is checked as each provider's turn comes, so one that switched
        # itself off during an earlier attempt is not tried.
        for provider in self._providers:
            if skip_local and provider.location == LLMLocation.LOCAL:
                continue
            if provider.is_available():
                yield provider

    @staticmethod
    def _fall_over(provider: LLMProvider, req: LLMRequest, error: LLMError) -> None:
        logger.warning(
            "LLM provider %s failed for %s (%s); trying the next one",
            provider.name,
            req.task,
            error.code,
        )

    async def complete(self, req: LLMRequest, skip_local: bool = False) -> str | BaseModel:
        last_error: LLMError | None = None
        for provider in self._candidates(skip_local):
            try:
                return await provider.complete(req)
            except LLMError as exc:
                self._fall_over(provider, req, exc)
                last_error = exc
        raise LLMUnavailableError(_NOTHING_REACHABLE) from last_error

    async def stream(
        self, req: LLMRequest, skip_local: bool = False
    ) -> AsyncIterator[tuple[str, str]]:
        last_error: LLMError | None = None
        for provider in self._candidates(skip_local):
            started = False
            try:
                async with aclosing(provider.stream(req)) as tokens:
                    async for item in tokens:
                        started = True
                        yield item
                return
            except LLMError as exc:
                # After the first token a fall-over would restart the reply mid-sentence.
                if started:
                    raise
                self._fall_over(provider, req, exc)
                last_error = exc
        raise LLMUnavailableError(_NOTHING_REACHABLE) from last_error
