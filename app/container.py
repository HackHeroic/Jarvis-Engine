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
from app.core.settings import Settings
from app.domain.anticipation import ConsentPolicy
from app.domain.events import (
    DraftAccepted,
    EventBus,
    LLMCallCompleted,
    PatternDetected,
    TaskCompleted,
    TaskMissed,
    TaskSkipped,
)
from app.domain.ports import DraftPlanner, EnergyEstimator, Sweep, TextEmbedder
from app.infrastructure.embedding import MiniLMEmbedder
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import set_chain
from app.infrastructure.persistence.factory import Repositories, RepositoryFactory
from app.modules.planning_replan import PlanningGraphReplanner
from app.orchestrator.hooks import ActionHooks
from app.services.anticipation.actions import build_action_factory
from app.services.anticipation.service import ActionFactory, AnticipationService
from app.services.anticipation.triggers import (
    AnticipationListener,
    MorningBriefTrigger,
    PatternRuleTrigger,
)
from app.services.analytical.rule_check_ins import RuleCheckIns
from app.services.analytical.workspace_builder import invalidate_workspaces
from app.services.documents.linker import DocumentLinker, DocumentLinkListener
from app.services.documents.service import DocumentService
from app.services.draft_service import DraftService
from app.services.energy import HeuristicEnergyEstimator, HistoryEnergyEstimator
from app.services.memory.conversation_memory import (
    ConversationMemoryService,
    SessionSummarySweep,
)
from app.services.memory.memory_service import MemoryDecaySweep, MemoryService
from app.services.memory.patterns import (
    DeadlineCrunchDetector,
    PatternDetector,
    PatternObserver,
    PatternService,
    PeakHourDetector,
    RepeatedSkipDetector,
    SkipTimeWindowDetector,
)
from app.services.memory.stated_rules import StatedRules
from app.services.planning.services import PlanningServices
from app.services.task_service import SlotPassedSweep, TaskService
from app.services.usage_ledger import UsageLedger
from app.services.user_model_service import UserModelService

logger = logging.getLogger(__name__)


def default_pattern_detectors() -> list[PatternDetector]:
    """The ways PEARL reads the outcome history: the one place they are chosen."""
    return [
        SkipTimeWindowDetector(),
        PeakHourDetector(),
        DeadlineCrunchDetector(),
        RepeatedSkipDetector(),
    ]


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
    patterns: PatternService
    pattern_observer: PatternObserver
    consent: ConsentPolicy
    planning: PlanningServices
    planner: DraftPlanner
    actions: ActionFactory
    anticipation: AnticipationService
    anticipation_listener: AnticipationListener
    user_model_service: UserModelService
    conversation_memory: ConversationMemoryService
    stated_rules: StatedRules
    rule_check_ins: RuleCheckIns
    documents: DocumentService
    document_linker: DocumentLinker
    settings: Settings

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
        keeps answering but persists nothing. ``cfg`` is the config module: it becomes
        each service's settings here, once (``Settings.from_config``);
        ``hooks`` gates every cloud send (PreCloudLLM). ``embedder`` turns memories
        into vectors; ``None`` means the local sentence model (``MiniLMEmbedder``).
        ``install_chain=False`` builds the chain without making it the process-wide
        one (a test that scripts its own model).
        """
        settings = Settings.from_config(cfg)
        if supabase_client is not None:
            repos = RepositoryFactory.supabase(
                supabase_client,
                document_bucket=settings.documents.bucket,
                max_file_bytes=settings.documents.max_file_bytes,
            )
        else:
            logger.warning("running on in-memory repositories — nothing is persisted")
            repos = RepositoryFactory.in_memory()

        events = EventBus()
        usage_ledger = UsageLedger(repos.usage)
        events.subscribe(LLMCallCompleted, usage_ledger)

        llm = build_chain(cfg, hooks, events)
        if install_chain:
            set_chain(llm)

        task_service = TaskService(
            repos.tasks, repos.task_events, events, settings=settings.tasks
        )
        embedder = embedder if embedder is not None else MiniLMEmbedder()
        memory_service = MemoryService(repos.memories, embedder, settings=settings.memory)
        patterns = PatternService(
            repos.task_events, repos.memories, repos.profiles, events,
            detectors=default_pattern_detectors(), settings=settings.focus,
        )
        observer = PatternObserver(patterns)  # PEARL looks again after every outcome
        for outcome in (TaskCompleted, TaskSkipped, TaskMissed):
            events.subscribe(outcome, observer)
        # The focus profile: the solver leans on it, the You page shows it.
        energy = HistoryEnergyEstimator(
            repos.task_events,
            HeuristicEnergyEstimator(),
            window_days=settings.focus.window_days,
            prior_strength=settings.focus.prior_strength,
            min_observations=settings.focus.min_observations,
        )
        draft_service = DraftService(
            repos.drafts, events, task_events=repos.task_events, tasks=repos.tasks
        )
        # Jarvis checks in on every rule it learned, spaced out (SM-2) while it holds:
        # each way a rule is stored starts its check-ins, and deleting one ends them.
        rule_check_ins = RuleCheckIns(repos.habit_trackers, repos.constraints, repos.profiles)
        # Documents and tasks find each other by meaning, both ways: a new document is
        # linked to the pending tasks it fits when it is kept, and an accepted plan's tasks
        # to the documents they fit once the accept has committed (DraftAccepted). A
        # change to a task's documents drops its cached workspace.
        document_linker = DocumentLinker(
            repos.documents, repos.tasks, embedder,
            threshold=settings.documents.link_similarity,
            on_links_changed=invalidate_workspaces,
        )
        events.subscribe(DraftAccepted, DocumentLinkListener(document_linker))
        # The first move. Each action kind is registered with the factory, so an
        # accepted suggestion can be rebuilt from its payload; the triggers with the
        # service. A skip window PEARL finds is offered as a rule right away.
        consent = ConsentPolicy.default()
        # What the planning sub-graph works with: a planning turn reaches it through the
        # turn's state, the replanner through its constructor.
        planning = PlanningServices(
            drafts=draft_service,
            tasks=task_service,
            task_repo=repos.tasks,
            constraints=repos.constraints,
            profiles=repos.profiles,
            memory=memory_service,
            energy=energy,
        )
        planner = PlanningGraphReplanner(planning)
        actions = build_action_factory(
            drafts=draft_service,
            constraints=repos.constraints,
            memory=memory_service,
            planner=planner,
            check_ins=rule_check_ins,
        )
        anticipation = AnticipationService(
            {}, actions, consent, repos.suggestions, repos.profiles, repos.constraints,
            settings=settings.anticipation,
        )
        anticipation.register_trigger(
            MorningBriefTrigger(
                repos.tasks, repos.task_events, planner, energy, patterns, actions,
                suggestions=repos.suggestions, settings=settings.anticipation,
            )
        )
        anticipation.register_trigger(
            PatternRuleTrigger(patterns, repos.suggestions, actions, repos.constraints)
        )
        anticipation_listener = AnticipationListener(anticipation, patterns)
        events.subscribe(PatternDetected, anticipation_listener)
        # Conversations that remember: the window, its summaries, "since we last spoke".
        conversation_memory = ConversationMemoryService(
            repos.conversations,
            repos.task_events,
            memory=memory_service,
            suggestions=repos.suggestions,
            settings=settings.conversation,
        )

        return cls(
            repos=repos,
            events=events,
            llm=llm,
            draft_service=draft_service,
            usage_ledger=usage_ledger,
            task_service=task_service,
            sweeps=[
                SlotPassedSweep(task_service),
                MemoryDecaySweep(memory_service),
                SessionSummarySweep(conversation_memory),
            ],
            energy=energy,
            embedder=embedder,
            memory_service=memory_service,
            patterns=patterns,
            pattern_observer=observer,
            consent=consent,
            planning=planning,
            planner=planner,
            actions=actions,
            anticipation=anticipation,
            anticipation_listener=anticipation_listener,
            user_model_service=UserModelService(
                repos.profiles, memory_service, patterns, repos.constraints, energy, usage_ledger,
                repos.task_events, settings=settings.focus,
            ),
            conversation_memory=conversation_memory,
            # A stated rule and the memory of stating it: letting go of one lets go of both.
            stated_rules=StatedRules(repos.constraints, memory_service, check_ins=rule_check_ins),
            rule_check_ins=rule_check_ins,
            # The user's documents across their record, links and file.
            documents=DocumentService(
                repos.documents, repos.document_files, repos.tasks,
                linker=document_linker, on_links_changed=invalidate_workspaces,
            ),
            document_linker=document_linker,
            settings=settings,
        )

    async def shutdown(self) -> None:
        """Stop the background work the container owns: PEARL runs, the pattern
        proposals they start and conversation compactions get a short grace to finish,
        then are cancelled."""
        await self.pattern_observer.shutdown()
        await self.anticipation_listener.shutdown()
        await self.conversation_memory.shutdown()
