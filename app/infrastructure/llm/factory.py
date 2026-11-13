"""Assembles the provider chain and holds the process-wide instance (Factory)."""

from types import ModuleType

from app.domain.events import EventBus, NullEventBus
from app.domain.ports import LLMProvider
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.claude import ClaudeProvider
from app.infrastructure.llm.decorators import (
    MeteredProvider,
    RedactingProvider,
    RetryingProvider,
    TimeoutProvider,
)
from app.infrastructure.llm.gemini import GeminiProvider
from app.infrastructure.llm.local import LocalLMStudioProvider
from app.infrastructure.llm.retry import ExponentialBackoff
from app.orchestrator.hooks import ActionHooks


def _wrap_cloud(provider: LLMProvider, hooks: ActionHooks, bus: EventBus) -> LLMProvider:
    # A provider may name its model per role for the usage ledger; else its name stands in.
    model_for_role = getattr(provider, "model_for_role", None) or (
        lambda _role, name=provider.name: name
    )
    metered = MeteredProvider(provider, bus, model_for_role)
    return RedactingProvider(RetryingProvider(TimeoutProvider(metered), ExponentialBackoff()), hooks)


class LLMProviderFactory:
    """Builds the chain every language-model call goes through."""

    @staticmethod
    def build(
        cfg: ModuleType,
        hooks: ActionHooks,
        bus: EventBus,
        extra_cloud: list[LLMProvider] | None = None,
    ) -> FallbackChainProvider:
        """Return ``[local, claude, *extra_cloud, gemini]``, each wrapped for its location.

        Claude is left out when no Anthropic credential is configured. Cloud
        providers: Redacting → Retrying → Timeout → Metered. Local: Timeout →
        Metered (no redaction — the prompt never leaves the machine — and no retry —
        a dead LM Studio should hand over to the cloud at once).
        """
        local = LocalLMStudioProvider(cfg)
        providers: list[LLMProvider] = [
            TimeoutProvider(MeteredProvider(local, bus, local.model_for_role))
        ]
        claude = ClaudeProvider(cfg)
        first_cloud = [claude] if claude.is_available() else []
        for cloud in [*first_cloud, *(extra_cloud or []), GeminiProvider(cfg)]:
            providers.append(_wrap_cloud(cloud, hooks, bus))
        return FallbackChainProvider(providers)


_chain: FallbackChainProvider | None = None


def get_chain() -> FallbackChainProvider:
    """Return the process-wide chain, building a default one on first use."""
    global _chain
    if _chain is None:
        import app.core.config as cfg
        from app.orchestrator.hooks import get_hooks

        _chain = LLMProviderFactory.build(cfg, get_hooks(), NullEventBus())
    return _chain


def set_chain(chain: FallbackChainProvider) -> None:
    """Replace the process-wide chain (the composition root, or a test's fake)."""
    global _chain
    _chain = chain


def reset_chain() -> None:
    """Forget the process-wide chain; the next ``get_chain`` builds a fresh default."""
    global _chain
    _chain = None
