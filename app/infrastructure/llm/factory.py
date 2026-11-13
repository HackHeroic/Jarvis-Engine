"""Assembles the provider chain and holds the process-wide instance (Factory)."""

from types import ModuleType

from app.domain.events import EventBus
from app.domain.ports import LLMProvider
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.claude import ClaudeProvider
from app.infrastructure.llm.decorators import (
    MeteredProvider,
    RedactingProvider,
    Redactor,
    RetryingProvider,
    TimeoutProvider,
)
from app.infrastructure.llm.gemini import GeminiProvider
from app.infrastructure.llm.local import LocalLMStudioProvider
from app.infrastructure.llm.retry import ExponentialBackoff


def _wrap_cloud(provider: LLMProvider, redactor: Redactor, bus: EventBus) -> LLMProvider:
    # A provider may name its model per role for the usage ledger; else its name stands in.
    model_for_role = getattr(provider, "model_for_role", None) or (
        lambda _role, name=provider.name: name
    )
    metered = MeteredProvider(TimeoutProvider(provider), bus, model_for_role)
    return RedactingProvider(RetryingProvider(metered, ExponentialBackoff()), redactor)


class LLMProviderFactory:
    """Builds the chain every language-model call goes through."""

    @staticmethod
    def build(
        cfg: ModuleType,
        redactor: Redactor,
        bus: EventBus,
        extra_cloud: list[LLMProvider] | None = None,
    ) -> FallbackChainProvider:
        """Return ``[local, claude, *extra_cloud, gemini]``, each wrapped for its location.

        Claude is left out when no Anthropic credential is configured. Cloud
        providers, outermost first: Redacting → Retrying → Metered → Timeout. Local:
        Metered → Timeout (no redaction — the prompt never leaves the machine — and no
        retry — a dead LM Studio should hand over to the cloud at once). Metering sits
        outside the timeout so a slow ledger write cannot turn an answer into a retry.
        """
        local = LocalLMStudioProvider(cfg)
        providers: list[LLMProvider] = [
            MeteredProvider(TimeoutProvider(local), bus, local.model_for_role)
        ]
        claude = ClaudeProvider(cfg)
        first_cloud = [claude] if claude.is_available() else []
        for cloud in [*first_cloud, *(extra_cloud or []), GeminiProvider(cfg)]:
            providers.append(_wrap_cloud(cloud, redactor, bus))
        return FallbackChainProvider(providers)


_chain: FallbackChainProvider | None = None


def current_chain() -> FallbackChainProvider | None:
    """Return the process-wide chain, or None if none has been set yet.

    ``app.core.model_router.get_chain`` builds the default one on first use.
    """
    return _chain


def set_chain(chain: FallbackChainProvider) -> None:
    """Replace the process-wide chain (the composition root, or a test's fake)."""
    global _chain
    _chain = chain


def reset_chain() -> None:
    """Forget the process-wide chain; the next ``model_router.get_chain`` builds a fresh one."""
    global _chain
    _chain = None
