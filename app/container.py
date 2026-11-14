"""The composition root: the one place that chooses concrete classes.

Services and the orchestrator depend on ports (``app.domain.ports``) and on the
event bus, and receive their collaborators. ``AppContainer.build`` decides which
repository family stores the data, creates the bus and subscribes its listeners,
and builds the language-model chain ``route_llm_call`` uses — metered onto that
bus, so every call reaches the usage ledger. It also lists the background sweeps
each subsystem owns, for the scheduler to run.
"""

import logging
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from app.core.model_router import build_chain
from app.domain.anticipation import ConsentPolicy
from app.domain.events import EventBus, LLMCallCompleted
from app.domain.ports import EnergyEstimator, Sweep, TextEmbedder
from app.infrastructure.embedding import MiniLMEmbedder
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import set_chain
from app.infrastructure.persistence.factory import Repositories, RepositoryFactory
from app.orchestrator.hooks import ActionHooks
from app.services.anticipation.service import ActionFactory, AnticipationService
from app.services.draft_service import DraftService
from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator
from app.services.memory.memory_service import MemoryDecaySweep, MemoryService
from app.services.task_service import SlotPassedSweep, TaskService
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
    task_service: TaskService
    sweeps: list[Sweep]
    energy: EnergyEstimator
    embedder: TextEmbedder
    memory_service: MemoryService
    consent: ConsentPolicy
    actions: ActionFactory
    anticipation: AnticipationService

    @classmethod
    def build(
        cls,
        *,
        supabase_client: Any | None,
        cfg: ModuleType,
        hooks: ActionHooks,
        embedder: TextEmbedder | None = None,
        install_chain: bool = True,
    ) -> "AppContainer":
        """Wire the application and install its chain as the process-wide one.

        ``supabase_client`` selects the Supabase repositories; ``None`` (Supabase
        unreachable at startup, or a test) selects the in-memory ones, so the app
        keeps answering but persists nothing. ``cfg`` is the config module;
        ``hooks`` gates every cloud send (PreCloudLLM). ``embedder`` turns memories
        into vectors; ``None`` means the local sentence model (``MiniLMEmbedder``).
        ``install_chain=False`` builds the chain without making it the process-wide
        one (a test that scripts its own model).
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
        if install_chain:
            set_chain(llm)

        task_service = TaskService(repos.tasks, repos.task_events, events)
        embedder = embedder if embedder is not None else MiniLMEmbedder()
        memory_service = MemoryService(repos.memories, embedder)
        # Triggers register themselves with the service; each one's action kind with
        # the factory, so an accepted suggestion can be rebuilt from its payload.
        consent = ConsentPolicy.default()
        actions = ActionFactory()

        return cls(
            repos=repos,
            events=events,
            llm=llm,
            draft_service=DraftService(repos.drafts, events, task_events=repos.task_events),
            usage_ledger=usage_ledger,
            task_service=task_service,
            sweeps=[SlotPassedSweep(task_service), MemoryDecaySweep(memory_service)],
            energy=HistoryEnergyEstimator(repos.task_events, HeuristicEnergyEstimator()),
            embedder=embedder,
            memory_service=memory_service,
            consent=consent,
            actions=actions,
            anticipation=AnticipationService(
                {}, actions, consent, repos.suggestions, repos.profiles, repos.constraints
            ),
        )
