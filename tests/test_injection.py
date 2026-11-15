"""Injection: collaborators come from the composition root, never from a process-wide lookup.

The lifespan builds one ``AppContainer``. A chat turn carries it in its state
(``container``), the planning sub-graph reads the ``PlanningServices`` it holds
(``planning``), and a service gets what it needs through its constructor. Without a
container a feature is off: a node returns its empty result and builds nothing itself.
"""

import ast
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

import app.services.planning.scheduling as scheduling
import app.domain.clock as utc_clock
from app.domain.entities import BehavioralConstraint, TaskEvent, TaskEventType
from app.infrastructure.persistence.factory import RepositoryFactory
from tests.behaviour.conftest import (  # noqa: F401 - jarvis and llm are fixtures
    brain_dump,
    decomposition,
    jarvis,
    llm,
    said,
)
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, planning_services

ROOT = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)
U = "alice"

# --- the registry is gone ---------------------------------------------------------------

_LOOKUP_NAMES = frozenset(
    {"registered_container", "set_container", "set_shared_clients", "get_db", "get_memory_store"}
)


def _lookups_in(path: Path) -> list[str]:
    """Each import of ``app.core.runtime`` and each identifier naming a global lookup."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "app.core.runtime" or (
                module == "app.core" and any(a.name == "runtime" for a in node.names)
            ):
                found.append(f"imports {module}")
            found += [f"imports {a.name}" for a in node.names if a.name in _LOOKUP_NAMES]
        elif isinstance(node, ast.Import):
            found += [f"imports {a.name}" for a in node.names if a.name == "app.core.runtime"]
        elif isinstance(node, ast.Name) and node.id in _LOOKUP_NAMES:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _LOOKUP_NAMES:
            found.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _LOOKUP_NAMES:
            found.append(f"defines {node.name}")
    return found


def test_runtime_registry__gone_and_imported_nowhere():
    assert find_spec("app.core.runtime") is None
    files = sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "tests").rglob("*.py"),
                    *(ROOT / "scripts").rglob("*.py")])

    offenders = {
        str(path.relative_to(ROOT)): found for path in files if (found := _lookups_in(path))
    }

    assert offenders == {}


def test_lookup_check__an_import_or_a_call__is_caught(tmp_path):
    planted = tmp_path / "node.py"
    planted.write_text(
        "from app.core.runtime import registered_container\n"
        "from app.core import runtime\n"
        "c = runtime.set_container(x)\n"
        "name = 'registered_container'\n"  # a string names nothing
    )

    assert _lookups_in(planted) == [
        "imports app.core.runtime", "imports registered_container", "imports app.core",
        "set_container",
    ]


# --- a whole turn, with the container only in the turn's state --------------------------


def _script(llm) -> None:
    def extract(req) -> str:
        message = said(req)
        if "before 11" in message:
            rule = {"type": "constraint", "content": "No work before 11 AM", "confidence": 0.9}
            return brain_dump(memories=[rule])
        if "marathon" in message:
            fact = {"type": "goal", "content": "Training for the Mumbai marathon",
                    "confidence": 0.8}
            return brain_dump(memories=[fact])
        return brain_dump(planning_goal="Revise DSA") if "plan" in message.lower() else "{}"

    llm.script("brain_dump_extraction", extract)
    llm.script("socratic_chunker", decomposition(["Graphs", "DP", "Trees", "Heaps", "Mocks"]))


def _local_slots(turn) -> list[tuple[datetime, datetime]]:
    schedule = turn.payload["schedule"]
    start = datetime.fromisoformat(schedule["horizon_start"])
    return sorted(
        ((start + timedelta(minutes=t["start_min"])).astimezone(IST),
         (start + timedelta(minutes=t["end_min"])).astimezone(IST))
        for t in schedule["schedule"].values()
    )


def _before_11(slot: tuple[datetime, datetime]) -> bool:
    begin, _end = slot
    return begin.hour < 11


@pytest.mark.asyncio
async def test_turn__container_only_in_turn_state__rule_memory_and_pattern_reach_the_turn(
    jarvis, llm, clock
):
    """The harness holds its container on ``app.state`` and nowhere else."""
    clock.freeze(MONDAY_8AM, scheduling, utc_clock)
    _script(llm)

    # A taught rule reaches the planner (planning.memory) and every day keeps it.
    await jarvis.say("Never put any work before 11 AM, please")
    plan = await jarvis.say("Plan my DSA revision")
    assert _local_slots(plan) and not [s for s in _local_slots(plan) if _before_11(s)]

    # A fact said in one conversation is in the next one's context note.
    first = await jarvis.say("I'm training for the Mumbai marathon")
    second = await jarvis.say("What should I focus on this week?")
    assert second.conversation_id != first.conversation_id
    assert "Training for the Mumbai marathon" in [m["content"] for m in second.payload["memories"]]

    # Three skips at 09:00: PEARL's learning moment arrives before complete.
    events = []
    for day in range(3):
        slot = datetime(2026, 11, 5 + day, 9, 0, tzinfo=IST)
        events.append(TaskEvent(
            user_id=U, task_id=f"past_{day}", event_type=TaskEventType.SKIPPED,
            occurred_at=slot + timedelta(minutes=25), scheduled_start=slot,
            scheduled_end=slot + timedelta(minutes=25),
            metadata={"title": f"Morning task {day}", "goal_id": "past", "deadline_hint": None,
                      "reason": None},
        ))
    await jarvis.state.container.repos.task_events.append_many(U, events)
    turn = await jarvis.say("Anything I should know about my week?")
    names = [event for event, _data in turn.events]
    assert "pattern_detected" in names
    assert names.index("pattern_detected") < names.index("complete")


@pytest.mark.asyncio
async def test_chat_plan__calendar_rule_expired__no_longer_constrains_the_plan(jarvis, llm, clock):
    """Chat planning reads the rules that apply now (``ConstraintRepository.list_active``),
    as the morning brief and a late accept do: an expired calendar block frees its hours."""
    from app.services.planning.rule_windows import to_horizon

    clock.freeze(MONDAY_8AM, scheduling, utc_clock)
    _script(llm)
    start = to_horizon(8 * 60)
    morning_block = {"semantic_slots": [{
        "name": "lectures", "start_min": start, "end_min": start + 180,
        "availability": "blocked", "recurrence": "daily",
    }]}
    for valid_until in ("2026-11-10T00:00:00+00:00", None):
        jarvis.db.table("behavioral_constraints").insert({
            "id": str(uuid4()), "user_id": U, "raw_text": "Lectures 8 to 11",
            "constraint_type": "habit", "recurrence": "daily", "source": "inferred",
            "structured_semantics": morning_block, "valid_until": valid_until,
            "created_at": "2026-11-01T00:00:00+00:00",
        }).execute()
        plan = await jarvis.say("Plan my DSA revision")
        mornings_used = [s for s in _local_slots(plan) if _before_11(s)]
        if valid_until:  # the term's timetable has ended: the mornings are free again
            assert mornings_used
            jarvis.db.rows["behavioral_constraints"].clear()
        else:  # a block still in force keeps every morning clear
            assert not mornings_used


# --- the planning nodes read their collaborators from the state --------------------------


@pytest.mark.asyncio
async def test_planning_nodes__read_planning_services_from_state__none_means_no_slots_no_focus_no_scan(
    clock,
):
    from app.domain.entities import TaskStatus
    from app.modules.planning_graph import _learned_focus, _memory_slots, mark_missed
    from app.services.memory.memory_service import MemoryCandidate

    clock.freeze(MONDAY_8AM, utc_clock)
    # No planning services: no memory slots, no focus, no deadline scan.
    assert await _memory_slots({"user_id": U, "planning": None}) == []
    assert await _learned_focus(None, U, IST) is None
    assert await mark_missed({"user_id": U, "planning": None, "user_message": "plan"}) == {
        "missed_deadlines": [], "anti_guilt_message": None,
    }

    repos = RepositoryFactory.in_memory()
    planning = planning_services(repos)
    await planning.memory.remember(U, MemoryCandidate(
        memory_type="constraint", content="No work before 11 AM", confidence=0.9,
    ))
    await repos.drafts.accept_atomic(U, (await repos.drafts.create(
        U, tasks=[], horizon_start=None, goal_id=None, schedule=None
    )).id, [{"task_id": "essay", "title": "Essay", "status": "pending", "duration_minutes": 25,
             "difficulty_weight": 0.5, "deadline_hint": "2026-11-10"}])

    slots = await _memory_slots({"user_id": U, "planning": planning})
    swept = await mark_missed({"user_id": U, "planning": planning, "user_message": "plan"})

    assert slots  # the stated rule, through planning.memory
    assert [m["task_id"] for m in swept["missed_deadlines"]] == ["essay"]  # planning.tasks
    assert (await repos.tasks.get(U, "essay")).status is TaskStatus.MISSED


# --- UserModel is a facade over ports -----------------------------------------------------


@pytest.mark.asyncio
async def test_user_model__over_ports__no_db_attribute():
    from app.core.user_model import UserModel

    repos = RepositoryFactory.in_memory()
    await repos.constraints.add(U, BehavioralConstraint(
        id=uuid4().hex, user_id=U, raw_text="No study before 11", constraint_type="habit",
    ))
    await repos.profiles.update(U, timezone="Europe/London")
    seed = await repos.drafts.create(U, tasks=[], horizon_start=None, goal_id=None, schedule=None)
    await repos.drafts.accept_atomic(U, seed.id, [
        {"task_id": "graphs", "title": "Graphs", "status": "pending", "duration_minutes": 25,
         "difficulty_weight": 0.5},
        {"task_id": "dp", "title": "DP", "status": "missed", "duration_minutes": 25,
         "difficulty_weight": 0.5},
    ])

    user = UserModel.for_planning(U, planning_services(repos))

    assert not hasattr(user, "_db")
    assert user.user_id == U
    assert (await user.get_profile()).timezone == "Europe/London"
    assert [c["raw_text"] for c in await user.get_behavioral_constraints()] == ["No study before 11"]
    assert [t["task_id"] for t in await user.get_pending_tasks()] == ["graphs"]
    assert [t["task_id"] for t in await user.get_missed_tasks()] == ["dp"]
    assert {t["task_id"] for t in await user.get_all_tasks()} == {"graphs", "dp"}


@pytest.mark.asyncio
async def test_user_model__profile_unreadable__none_and_the_default_zone():
    from app.core.user_model import UserModel
    from app.domain.errors import PersistenceError

    class _Down:
        async def get_or_create(self, user_id):
            raise PersistenceError("Could not reach the database.")

    repos = RepositoryFactory.in_memory()
    user = UserModel(U, profiles=_Down(), constraints=repos.constraints, tasks=repos.tasks)

    assert await user.get_profile() is None


# --- web search is metered onto the bus it is handed --------------------------------------


class _UsageRecorder:
    """Records every LLMCallCompleted published on the bus it listens to."""

    def __init__(self, bus) -> None:
        from app.domain.events import EventListener, LLMCallCompleted

        recorded = self.recorded = []

        class _Listener(EventListener):
            async def handle(self, event) -> None:
                recorded.append(event.usage)

        bus.subscribe(LLMCallCompleted, _Listener())


_SEARCH_ANSWER = "https://docs.example.org/graphs | Graphs"


@pytest.mark.asyncio
async def test_metered_web_search__bus_passed__usage_published_and_no_bus__refused_before_searching():
    """No bus is a programming error, never a silent unmetered cloud send: the ledger
    (the You page's privacy receipt) would under-count. Refused before the search runs."""
    from app.core.model_router import attribute_llm_calls, metered_web_search
    from app.domain.events import EventBus

    bus = EventBus()
    usage_seen = _UsageRecorder(bus)
    searches = []

    async def search() -> str:
        searches.append(1)
        return _SEARCH_ANSWER

    end = attribute_llm_calls(U, "sess-1")
    try:
        answered = await metered_web_search(search, bus=bus, pii_redactions=1)
        with pytest.raises(TypeError, match="bus"):
            await metered_web_search(search, bus=None)
    finally:
        end()

    assert answered == _SEARCH_ANSWER
    assert len(searches) == 1  # the refused call never searched
    (usage,) = usage_seen.recorded
    assert (usage.provider, usage.task, usage.user_id, usage.session_id) == (
        "gemini", "web_search", U, "sess-1"
    )
    assert usage.pii_redactions == 1
    assert usage.created_at.tzinfo is timezone.utc


def _stub_web_search(monkeypatch) -> list[str]:
    """Gemini's grounded search answers one link; returns the queries it was sent."""
    import app.services.analytical.workspace_builder as wb

    queries = []

    async def search(*, user_prompt: str, system_prompt: str) -> str:
        queries.append(user_prompt)
        return _SEARCH_ANSWER

    monkeypatch.setattr(wb, "gemini_web_search", search)
    return queries


@pytest.mark.asyncio
async def test_workspace_route__web_search_answered__one_web_search_usage_on_the_containers_bus(
    monkeypatch,
):
    """The route hands the composition root's bus to the search: the usage ledger counts
    the cloud send. Dropping the hand-off would under-count the privacy receipt."""
    import httpx

    import app.services.analytical.workspace_builder as wb
    from app.main import app
    from tests.fakes import accept_tasks, build_test_container

    container = build_test_container()
    await accept_tasks(container.repos, U, [{"task_id": "graphs_t1", "title": "Graph algorithms"}])
    usage_seen = _UsageRecorder(container.events)
    queries = _stub_web_search(monkeypatch)

    async def nothing(*_args, **_kwargs):
        return None

    async def no_chunks(*_args, **_kwargs):
        return []

    async def reader(*_args, **_kwargs):
        return "reader"

    monkeypatch.setattr(wb, "_get_supabase", lambda: None)
    monkeypatch.setattr(wb, "_read_cached_workspace", nothing)
    monkeypatch.setattr(wb, "_write_cached_workspace", nothing)
    monkeypatch.setattr(wb, "fetch_chunks_for_task", no_chunks)
    monkeypatch.setattr(wb, "get_learning_style", reader)
    monkeypatch.setattr(app.state, "container", container, raising=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/tasks/graphs_t1/workspace", params={"user_id": U})

    assert response.status_code == 200
    assert len(queries) == 1
    (usage,) = usage_seen.recorded
    assert (usage.provider, usage.task) == ("gemini", "web_search")


def _turn_state(container, db_client=None, **request) -> dict:
    """The orchestrator's input exactly as ``ChatTurnService`` builds it for a turn."""
    import asyncio

    from app.schemas.chat_stream import ChatStreamRequest
    from app.services.chat_turn.context import initial_state

    queue: asyncio.Queue = asyncio.Queue()
    return initial_state(
        ChatStreamRequest(user_id=U, **request),
        session_id="sess-1",
        user_model=None,
        progress_callback=lambda phase, **detail: None,
        progress_queue=queue,
        container=container,
        db_client=db_client,
        force_cloud_request=False,
    )


async def _run_module_in_turn(module_name: str, state: dict) -> dict:
    """``module_name``'s orchestrator node, run on a graph over the turn's state type
    (``JarvisState``): a key the state type does not declare never reaches the module."""
    from langgraph.graph import END, START, StateGraph

    from app.modules import module_registry
    from app.orchestrator.module_wrapper import create_module_wrapper
    from app.orchestrator.state import JarvisState

    graph = StateGraph(JarvisState)
    graph.add_node(module_name, create_module_wrapper(module_name, module_registry))
    graph.add_edge(START, module_name)
    graph.add_edge(module_name, END)
    return await graph.compile().ainvoke(state)


@pytest.mark.asyncio
async def test_research_module__in_a_turn__each_web_search_metered_on_the_containers_bus(
    monkeypatch,
):
    from tests.fakes import build_test_container

    container = build_test_container()
    usage_seen = _UsageRecorder(container.events)
    queries = _stub_web_search(monkeypatch)

    result = await _run_module_in_turn(
        "research_agent", _turn_state(container, user_prompt="latest on CP-SAT")
    )

    assert "research_agent" in result["modules_invoked"]
    assert result["error"] is None
    assert queries  # it searched
    assert [(u.provider, u.task) for u in usage_seen.recorded] == (
        [("gemini", "web_search")] * len(queries)
    )


# --- the turn's state hands the knowledge and research modules their collaborators -------


def test_state_in__real_turn_state__knowledge_and_research_carry_the_container_and_database():
    from app.modules.knowledge_graph import knowledge_state_in
    from app.modules.research_graph import research_state_in
    from tests.fakes import FakeDBClient, build_test_container

    container, db_client = build_test_container(), FakeDBClient()
    state = _turn_state(container, db_client, user_prompt="here are my notes")

    knowledge = knowledge_state_in(state)
    research = research_state_in(state)

    assert knowledge["container"] is container
    assert knowledge["db_client"] is db_client
    assert research["container"] is container


@pytest.mark.asyncio
async def test_knowledge_module__upload_in_a_turn__ingestion_gets_the_turns_database_and_repositories(
    monkeypatch,
):
    """A document upload in chat: the document row needs the database, the user's rules
    and tasks the container's repositories. Lose either and the document is not kept."""
    import base64

    from app.schemas.context import IngestionPipelineResult, IntentType
    from tests.fakes import FakeDBClient, build_test_container

    container, db_client = build_test_container(), FakeDBClient()
    seen = []

    async def ingest(**kwargs):
        seen.append(kwargs)
        return IngestionPipelineResult(
            intent=IntentType.KNOWLEDGE_INGESTION, classification_summary="lecture notes"
        )

    monkeypatch.setattr("app.services.extraction.orchestrator.process_ingestion", ingest)

    result = await _run_module_in_turn("knowledge_module", _turn_state(
        container, db_client, user_prompt="here are my notes",
        file_base64=base64.b64encode(b"lecture notes").decode(), media_type="pdf",
        file_name="notes.pdf",
    ))

    assert "knowledge_module" in result["modules_invoked"]
    (call,) = seen
    assert call["db_client"] is db_client
    assert call["user_id"] == U
    assert call["constraints"] is container.repos.constraints
    assert call["tasks"] is container.repos.tasks
    assert call["goals"] is container.repos.goals
    assert result["ingestion_result"]["intent"] == "KNOWLEDGE_INGESTION"


@pytest.mark.asyncio
async def test_process_ingestion__a_users_document_and_no_database__persistence_error_nothing_ingested(
    monkeypatch,
):
    """No database means no document row: refused before anything is read or stored,
    never a reply that says the document was kept."""
    import app.services.extraction.orchestrator as orchestrator
    from app.domain.errors import PersistenceError
    from app.schemas.context import IntentType

    ingested = []

    async def ingest(**kwargs):
        ingested.append(kwargs)

    monkeypatch.setattr(orchestrator, "ingest_knowledge", ingest)

    with pytest.raises(PersistenceError) as raised:
        await orchestrator.process_ingestion(
            payload="Graphs problem set", user_id=U,
            intent_override=IntentType.KNOWLEDGE_INGESTION, db_client=None,
        )

    assert raised.value.message == orchestrator.DOCUMENT_NOT_SAVED
    assert ingested == []


@pytest.mark.asyncio
async def test_knowledge_module__no_database_in_the_turn__honest_couldnt_save_reply(
    monkeypatch, caplog
):
    import base64

    import app.services.extraction.orchestrator as orchestrator
    from app.schemas.context import IntentClassification, IntentType
    from tests.fakes import build_test_container

    async def classified(_text):
        return IntentClassification(
            intent=IntentType.KNOWLEDGE_INGESTION, confidence=0.9, summary="notes"
        )

    monkeypatch.setattr(orchestrator, "_classify", classified)
    async def extracted(*_a):
        return "lecture notes", None

    monkeypatch.setattr(orchestrator, "_extract_text", extracted)

    result = await _run_module_in_turn("knowledge_module", _turn_state(
        build_test_container(), None, user_prompt="here are my notes",
        file_base64=base64.b64encode(b"lecture notes").decode(), media_type="pdf",
    ))

    assert result["error"] == orchestrator.DOCUMENT_NOT_SAVED
    assert "no database" in caplog.text


# --- ingestion reads the user's tasks and stores a goal's deadline through ports ----------


async def _accepted(repos, rows: list[dict]) -> None:
    seed = await repos.drafts.create(U, tasks=[], horizon_start=None, goal_id=None, schedule=None)
    await repos.drafts.accept_atomic(U, seed.id, [
        {"status": "pending", "duration_minutes": 25, "difficulty_weight": 0.5, **row}
        for row in rows
    ])


@pytest.mark.asyncio
async def test_document_linker__reads_the_users_tasks_and_writes_links_through_the_ports():
    from app.domain.entities import IngestedDocument
    from app.services.documents.linker import DocumentLinker

    repos = RepositoryFactory.in_memory()
    await _accepted(repos, [
        {"task_id": "graphs_t1", "title": "graphs", "goal_id": "dsa"},
        {"task_id": "essay_t1", "title": "essay", "goal_id": "english"},
    ])
    linker = DocumentLinker(repos.documents, repos.tasks, BagOfWordsEmbedder(), threshold=0.5)
    document = IngestedDocument(user_id=U, source_id="doc-1", document_topics=["graphs"])
    await repos.documents.save(U, document)

    matched = await linker.link_document(U, document)

    assert matched == ["graphs_t1"]
    assert [l.task_id for l in await repos.documents.links_for_documents(U, ["doc-1"])] == [
        "graphs_t1"
    ]


@pytest.mark.asyncio
async def test_process_ingestion__document_names_a_deadline__stored_for_the_matched_tasks_goal(
    monkeypatch,
):
    import app.services.extraction.orchestrator as orchestrator
    from app.schemas.context import IntentType
    from app.services.documents.linker import DocumentLinker
    from app.services.documents.service import DocumentService
    from app.services.extraction.knowledge_ingester import KnowledgeIngestionResult

    repos = RepositoryFactory.in_memory()
    await _accepted(repos, [{"task_id": "graphs_t1", "title": "graphs", "goal_id": "dsa"}])
    documents = DocumentService(
        repos.documents, repos.document_files, repos.tasks,
        linker=DocumentLinker(repos.documents, repos.tasks, BagOfWordsEmbedder(), threshold=0.5),
    )

    async def ingest(**_kwargs):
        return KnowledgeIngestionResult(
            stored_chunk_count=3, suggested_actions=[], metadata={},
            action_items=["Finish the graphs problem set"], document_topics=["graphs"],
            deadlines=["2099-01-15"],
        )

    monkeypatch.setattr(orchestrator, "ingest_knowledge", ingest)

    result = await orchestrator.process_ingestion(
        payload="Graphs problem set, due 15 January 2099", user_id=U,
        intent_override=IntentType.KNOWLEDGE_INGESTION, tasks=repos.tasks, goals=repos.goals,
        db_client=FakeDBClient(),  # the document row is kept in the database
        documents=documents,
    )

    assert result.knowledge_result["linked_task_ids"] == ["graphs_t1"]
    assert await repos.goals.latest_context(U, "dsa") == "Finish the graphs problem set"


# --- settings come from the composition root ----------------------------------------------

_CONTAINER_BUILT_SERVICES = (
    "app/services/task_service.py",
    "app/services/memory/memory_service.py",
    "app/services/memory/conversation_memory.py",
    "app/services/memory/patterns.py",
    "app/services/user_model_service.py",
    "app/services/anticipation/scheduler.py",
    "app/services/anticipation/service.py",
    "app/services/anticipation/triggers.py",
    "app/services/documents/service.py",
    "app/services/documents/linker.py",
    "app/services/analytical/rule_check_ins.py",
)


@pytest.mark.asyncio
async def test_container__services_get_settings_from_the_composition_root():
    """A config with a 5-minute grace gives the container's task service a 5-minute grace:
    a slot that ended 10 minutes ago has passed (the default 30 would keep it)."""
    from types import ModuleType

    import app.core.config as config
    from app.container import AppContainer
    from app.domain.entities import TaskStatus
    from app.orchestrator.hooks import ActionHooks
    from tests.fakes import BagOfWordsEmbedder

    cfg = ModuleType("cfg_stub")
    cfg.__dict__.update({k: v for k, v in vars(config).items() if k.isupper()})
    cfg.SLOT_GRACE_MINUTES = 5
    container = AppContainer.build(
        supabase_client=None, cfg=cfg, hooks=ActionHooks(), embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )
    now = datetime(2026, 11, 16, 10, 0, tzinfo=IST)
    start = now - timedelta(minutes=35)
    await _accepted(container.repos, [{
        "task_id": "graphs", "title": "Graphs", "scheduled_start": start.isoformat(),
        "scheduled_end": (start + timedelta(minutes=25)).isoformat(),
    }])

    await container.task_service.sweep_passed_slots(U, now)

    assert container.task_service._settings.slot_grace_minutes == 5
    assert (await container.repos.tasks.get(U, "graphs")).status is TaskStatus.MISSED


def test_container_built_services__import_no_config_module():
    importing = []
    for relative in _CONTAINER_BUILT_SERVICES:
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            if any(n == "app.core.config" or n.startswith("app.core.config.") for n in names):
                importing.append(relative)
    assert importing == []
