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

import app.api.v1.endpoints.schedule as schedule_endpoint
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
from tests.fakes import planning_services

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
    clock.freeze(MONDAY_8AM, schedule_endpoint, utc_clock)
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
    from app.services.memory.constraint_bridge import to_horizon

    clock.freeze(MONDAY_8AM, schedule_endpoint, utc_clock)
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


@pytest.mark.asyncio
async def test_metered_web_search__bus_passed__usage_published_and_no_bus_nothing():
    from app.core.model_router import attribute_llm_calls, metered_web_search
    from app.domain.events import EventBus, EventListener, LLMCallCompleted

    recorded = []

    class _Listener(EventListener):
        async def handle(self, event) -> None:
            recorded.append(event.usage)

    bus = EventBus()
    bus.subscribe(LLMCallCompleted, _Listener())

    async def search() -> str:
        return "https://docs.example.org/graphs | Graphs"

    end = attribute_llm_calls(U, "sess-1")
    try:
        answered = await metered_web_search(search, bus=bus, pii_redactions=1)
        unmetered = await metered_web_search(search, bus=None)
    finally:
        end()

    assert answered == unmetered == "https://docs.example.org/graphs | Graphs"
    (usage,) = recorded
    assert (usage.provider, usage.task, usage.user_id, usage.session_id) == (
        "gemini", "web_search", U, "sess-1"
    )
    assert usage.pii_redactions == 1
    assert usage.created_at.tzinfo is timezone.utc


# --- ingestion reads the user's tasks and stores a goal's deadline through ports ----------


async def _accepted(repos, rows: list[dict]) -> None:
    seed = await repos.drafts.create(U, tasks=[], horizon_start=None, goal_id=None, schedule=None)
    await repos.drafts.accept_atomic(U, seed.id, [
        {"status": "pending", "duration_minutes": 25, "difficulty_weight": 0.5, **row}
        for row in rows
    ])


@pytest.mark.asyncio
async def test_link_document_to_tasks__reads_the_users_tasks_through_the_task_repository(
    monkeypatch,
):
    from unittest.mock import MagicMock

    import app.services.extraction.task_material_linker as linker

    repos = RepositoryFactory.in_memory()
    await _accepted(repos, [
        {"task_id": "graphs_t1", "title": "graphs", "goal_id": "dsa"},
        {"task_id": "essay_t1", "title": "essay", "goal_id": "english"},
    ])
    links = MagicMock()  # task_materials has no repository yet: the link rows go here
    monkeypatch.setattr(  # one dimension per word: "graphs" matches only the graphs task
        linker, "_get_embedding_function",
        lambda: lambda texts: [[float("graphs" in t), float("essay" in t)] for t in texts],
    )

    matched = await linker.link_document_to_tasks(
        U, ["graphs"], "doc-1", tasks=repos.tasks, supabase_client=links
    )

    assert matched == ["graphs_t1"]
    links.table.assert_called_once_with("task_materials")  # never user_tasks


@pytest.mark.asyncio
async def test_process_ingestion__document_names_a_deadline__stored_for_the_matched_tasks_goal(
    monkeypatch,
):
    import app.services.extraction.orchestrator as orchestrator
    from app.schemas.context import IntentType
    from app.services.extraction.knowledge_ingester import KnowledgeIngestionResult

    repos = RepositoryFactory.in_memory()
    await _accepted(repos, [{"task_id": "graphs_t1", "title": "graphs", "goal_id": "dsa"}])
    seen_tasks = []

    async def ingest(**_kwargs):
        return KnowledgeIngestionResult(
            stored_chunk_count=3, suggested_actions=[], metadata={},
            action_items=["Finish the graphs problem set"], document_topics=["graphs"],
            deadlines=["2099-01-15"],
        )

    async def link(*, tasks, **_kwargs):
        seen_tasks.append(tasks)
        return ["graphs_t1"]

    monkeypatch.setattr(orchestrator, "ingest_knowledge", ingest)
    monkeypatch.setattr(orchestrator, "link_document_to_tasks", link)

    await orchestrator.process_ingestion(
        payload="Graphs problem set, due 15 January 2099", user_id=U,
        intent_override=IntentType.KNOWLEDGE_INGESTION, tasks=repos.tasks, goals=repos.goals,
    )

    assert seen_tasks == [repos.tasks]
    assert await repos.goals.latest_context(U, "dsa") == "Finish the graphs problem set"
