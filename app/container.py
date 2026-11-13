"""The composition root: the one place that chooses concrete classes.

Services and the orchestrator depend on ports (``app.domain.ports``) and on the
event bus, and receive their collaborators. ``AppContainer.build`` decides which
repository family stores the data, creates the bus and subscribes its listeners,
and builds the language-model chain ``route_llm_call`` uses — metered onto that
bus, so every call reaches the usage ledger.
"""

import logging
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from app.core.model_router import build_chain
from app.domain.events import EventBus, LLMCallCompleted
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import set_chain
from app.infrastructure.persistence.factory import Repositories, RepositoryFactory
from app.orchestrator.hooks import ActionHooks
from app.services.draft_service import DraftService
from app.services.usage_ledger import UsageLedger

logger = logging.getLogger(__name__)


@dataclass
class AppContainer:
    """The application's long-lived collaborators, wired once at startup."""

    repos: Repositories
    events: EventBus
    llm: FallbackChainProvider
    draft_service: DraftService
    usage_ledger: UsageLedger

    @classmethod
    def build(
        cls, *, supabase_client: Any | None, cfg: ModuleType, hooks: ActionHooks
    ) -> "AppContainer":
        """Wire the application and install its chain as the process-wide one.

        ``supabase_client`` selects the Supabase repositories; ``None`` (Supabase
        unreachable at startup, or a test) selects the in-memory ones, so the app
        keeps answering but persists nothing. ``cfg`` is the config module;
        ``hooks`` gates every cloud send (PreCloudLLM).
        """
        if supabase_client is not None:
            repos = RepositoryFactory.supabase(supabase_client)
        else:
            logger.warning("running on in-memory repositories — nothing is persisted")
            repos = RepositoryFactory.in_memory()

        events = EventBus()
        usage_ledger = UsageLedger(repos.usage)
        events.subscribe(LLMCallCompleted, usage_ledger)

        llm = build_chain(cfg, hooks, events)
        set_chain(llm)

        return cls(
            repos=repos,
            events=events,
            llm=llm,
            draft_service=DraftService(repos.drafts, events),
            usage_ledger=usage_ledger,
        )
