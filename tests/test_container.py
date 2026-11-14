"""AppContainer: the composition root picks every concrete class, in one place."""

import logging
from datetime import datetime, timezone

import pytest

import app.core.config as _cfg
import app.models.brain.litellm_conf as _llm
from app.container import AppContainer
from app.core import runtime
from app.core.model_router import attribute_llm_calls, route_llm_call
from app.domain.entities import LLMLocation, LLMUsageEvent
from app.domain.events import DraftAccepted, DomainEvent, EventListener, LLMCallCompleted
from app.infrastructure.llm.factory import current_chain
from app.infrastructure.llm.transport import report_token_usage
from app.infrastructure.persistence.in_memory import (
    InMemoryDraftRepository,
    InMemoryTaskRepository,
    InMemoryUsageRepository,
)
from app.infrastructure.persistence.supabase_repos import (
    SupabaseDraftRepository,
    SupabaseTaskRepository,
    SupabaseUsageRepository,
)
from app.orchestrator.hooks import ActionHooks
from tests.fakes import FakeSupabase

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _build(supabase_client=None) -> AppContainer:
    return AppContainer.build(supabase_client=supabase_client, cfg=_cfg, hooks=ActionHooks())


class _Recorder(EventListener):
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def handle(self, event: DomainEvent) -> None:
        self.events.append(event)


def test_container__no_supabase__uses_in_memory_family(caplog):
    with caplog.at_level(logging.WARNING, logger="app.container"):
        container = _build(supabase_client=None)

    assert isinstance(container.repos.tasks, InMemoryTaskRepository)
    assert isinstance(container.repos.drafts, InMemoryDraftRepository)
    assert isinstance(container.repos.usage, InMemoryUsageRepository)
    assert [r.getMessage() for r in caplog.records if r.name == "app.container"] == [
        "running on in-memory repositories — nothing is persisted"
    ]


def test_container__with_fake_supabase__uses_supabase_family(caplog):
    with caplog.at_level(logging.WARNING, logger="app.container"):
        container = _build(supabase_client=FakeSupabase())

    assert isinstance(container.repos.tasks, SupabaseTaskRepository)
    assert isinstance(container.repos.drafts, SupabaseDraftRepository)
    assert isinstance(container.repos.usage, SupabaseUsageRepository)
    assert [r for r in caplog.records if r.name == "app.container"] == []


@pytest.mark.asyncio
async def test_container__wires_usage_ledger_to_bus():
    container = _build()
    usage = LLMUsageEvent(
        user_id="u1",
        session_id="s1",
        task="voice_of_jarvis",
        role="fast",
        provider="local",
        model="gemma",
        location=LLMLocation.LOCAL,
        input_tokens=120,
        output_tokens=30,
        latency_ms=40,
        created_at=NOW,
    )

    await container.events.publish(LLMCallCompleted(occurred_at=NOW, user_id="u1", usage=usage))
    await container.usage_ledger.drain()

    summary = await container.repos.usage.summary("u1")
    assert (summary["local_calls"], summary["input_tokens"], summary["output_tokens"]) == (1, 120, 30)


def test_container__installs_its_chain_for_route_llm_call():
    container = _build()

    assert current_chain() is container.llm


def test_container__memory_service_on_the_given_embedder__decay_sweep_listed_chain_left_alone():
    from app.infrastructure.embedding import MiniLMEmbedder
    from app.services.memory.conversation_memory import (
        ConversationMemoryService,
        SessionSummarySweep,
    )
    from app.services.memory.memory_service import MemoryDecaySweep, MemoryService
    from tests.fakes import BagOfWordsEmbedder

    installed = current_chain()
    embedder = BagOfWordsEmbedder()

    container = AppContainer.build(
        supabase_client=None, cfg=_cfg, hooks=ActionHooks(), embedder=embedder, install_chain=False
    )

    assert isinstance(container.memory_service, MemoryService)
    assert container.embedder is embedder
    assert [sweep.name for sweep in container.sweeps] == [
        "slot_passed", "memory_decay", "session_summary",
    ]
    assert isinstance(container.sweeps[1], MemoryDecaySweep)
    assert isinstance(container.sweeps[2], SessionSummarySweep)
    assert isinstance(container.conversation_memory, ConversationMemoryService)
    assert current_chain() is installed  # not replaced by container.llm
    assert isinstance(_build().embedder, MiniLMEmbedder)  # the default: the local model


@pytest.mark.asyncio
async def test_container__route_llm_call__usage_recorded_for_the_attributed_user(monkeypatch):
    """End to end: the chain route_llm_call uses meters onto the container's bus."""
    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", False)
    monkeypatch.setattr(_cfg, "CLAUDE_PRIMARY", False)
    monkeypatch.setattr(_cfg, "GEMMA_FAST_MODEL", "openai/google/gemma-4-e4b")

    async def transport(**kwargs):
        report_token_usage(42, 7)
        return "Good evening."

    monkeypatch.setattr(_llm, "hybrid_route_query", transport)
    container = _build()

    end_attribution = attribute_llm_calls("u1", "sess-1")
    try:
        assert await route_llm_call(task="voice_of_jarvis", prompt="hi") == "Good evening."
    finally:
        end_attribution()
    await container.usage_ledger.drain()

    assert await container.usage_ledger.receipt("u1") == {
        "on_device_calls": 1,
        "cloud_calls": 0,
        "pii_redactions": 0,
        "input_tokens": 42,
        "output_tokens": 7,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_container__draft_service__publishes_draft_accepted_on_its_bus():
    container = _build()
    recorder = _Recorder()
    container.events.subscribe(DraftAccepted, recorder)
    draft = await container.repos.drafts.create(
        "u1",
        tasks=[{"task_id": "g_t1", "title": "Read ch1", "duration_minutes": 25}],
        horizon_start="2026-11-14T08:00:00+00:00",
        goal_id="g",
        schedule=None,
    )

    assert await container.draft_service.accept("u1", draft.id) == 1

    (event,) = recorder.events
    assert (event.user_id, event.draft_id, event.task_count) == ("u1", draft.id, 1)


@pytest.mark.asyncio
async def test_accept_draft_and_persist__the_containers_draft_service__draft_accepted_on_its_bus():
    """The orchestrator's draft_action node goes through this helper with the turn's
    container: DraftAccepted must reach the container's bus."""
    from app.services.draft_actions import accept_draft_and_persist

    container = _build()
    recorder = _Recorder()
    container.events.subscribe(DraftAccepted, recorder)
    draft = await container.repos.drafts.create(
        "u1",
        tasks=[{"task_id": "g_t1", "title": "Read ch1", "duration_minutes": 25}],
        horizon_start="2026-11-14T08:00:00+00:00",
        goal_id="g",
        schedule=None,
    )

    written = await accept_draft_and_persist(
        container.draft_service, container.planner, "u1", draft
    )

    assert written == 1
    assert [type(e) for e in recorder.events] == [DraftAccepted]
    assert len(await container.repos.tasks.list_pending("u1")) == 1


def test_runtime__reset_shared_clients__forgets_the_container():
    runtime.set_container(_build())

    runtime.reset_shared_clients()

    assert runtime.registered_container() is None
    # One name per meaning: get_container is the request-scoped dependency in app.api.deps.
    assert not hasattr(runtime, "get_container")


def test_container__pearl_detectors__chosen_here_and_only_here():
    """The composition root picks the strategies PEARL reads the history with; the
    service has no list of its own (a new detector is a class and one line here)."""
    import inspect

    from app.container import default_pattern_detectors
    from app.services.memory.patterns import PatternService

    container = _build()

    assert [type(d).__name__ for d in container.patterns._detectors] == [
        type(d).__name__ for d in default_pattern_detectors()
    ] == [
        "SkipTimeWindowDetector", "PeakHourDetector", "DeadlineCrunchDetector",
        "RepeatedSkipDetector",
    ]
    detectors = inspect.signature(PatternService).parameters["detectors"]
    assert detectors.default is inspect.Parameter.empty
