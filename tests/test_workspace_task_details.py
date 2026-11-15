"""A task's workspace carries the task's own details, read once through TaskRepository.

Tasks are written the only way they ever are, by accepting a draft, into both
repository families (in-memory, and Supabase over ``FakeSupabase``). The web search is
a recorder and the model is scripted.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import app.services.analytical.workspace_builder as wb
from app.domain import errors
from app.domain.entities import GoalDeadlineUpdate
from app.domain.events import NullEventBus
from app.infrastructure.persistence.factory import RepositoryFactory
from tests.fakes import FakeSupabase, accept_tasks, build_test_container, install_scripted_model

U, OTHER = "alice", "bob"
CRITERIA = "Normalise one schema to 3NF from memory"
INTENTION = {
    "obstacle_trigger": "Urge to check the phone when a dependency gets confusing",
    "behavioral_response": "If that urge comes, then write the dependency down on paper first",
}


def _row(task_id: str, title: str, **extra) -> dict:
    return {"task_id": task_id, "title": title, **extra}


_accept = accept_tasks


@pytest.fixture(params=["in_memory", "supabase_fake"])
def repos(request):
    if request.param == "in_memory":
        return RepositoryFactory.in_memory()
    return RepositoryFactory.supabase(FakeSupabase())


@pytest.fixture
def offline(monkeypatch) -> list[str]:
    """No database client, a reader, a web search that answers one link (recorded)."""
    searches: list[str] = []

    async def search(user_prompt: str, system_prompt: str, *, bus) -> str:
        searches.append(user_prompt)
        return "https://example.org/normal-forms | Normal forms, explained"

    async def reader(*_args, **_kwargs):
        return "reader"

    async def no_material(*_args, **_kwargs):
        return []

    monkeypatch.setattr(wb, "_get_supabase", lambda: None)
    monkeypatch.setattr(wb, "_search_web", search)
    monkeypatch.setattr(wb, "get_learning_style", reader)
    monkeypatch.setattr(wb, "fetch_chunks_for_task", no_material)
    install_scripted_model()
    return searches


async def _build(repos, task_id: str = "t1", user_id: str = U):
    return await wb.build_task_workspace(
        user_id, task_id, tasks=repos.tasks, materials=repos.documents, goals=repos.goals,
        bus=NullEventBus(),
    )


# --- the task's own details ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace__task_with_criteria__criteria_returned(repos, offline):
    await _accept(repos, U, [_row(
        "t1", "Revise DBMS normalisation", completion_criteria=CRITERIA,
        deadline_hint="2026-11-20", topic_keywords=["normalisation", "3NF"],
    )])

    ws = await _build(repos)

    assert ws.task_title == "Revise DBMS normalisation"
    assert ws.completion_criteria == CRITERIA
    assert (ws.duration_minutes, ws.deadline_hint) == (25, "2026-11-20")
    assert ws.primary_objective == "Revise DBMS normalisation normalisation 3NF"


@pytest.mark.asyncio
async def test_workspace__criteria_only_restating_the_title__left_blank(repos, offline):
    await _accept(repos, U, [_row(
        "t1", "Revise DBMS normalisation", completion_criteria="Finish: Revise DBMS normalisation",
    )])

    assert (await _build(repos)).completion_criteria == ""


@pytest.mark.asyncio
async def test_workspace__task_without_criteria__empty_not_the_title(repos, offline):
    await _accept(repos, U, [_row("t1", "Revise DBMS normalisation")])

    ws = await _build(repos)

    assert ws.completion_criteria == "" and ws.deadline_hint is None


@pytest.mark.asyncio
async def test_workspace__goal_id_and_keywords_of_the_task__decide_its_kind(repos, offline):
    await _accept(repos, U, [_row("t1", "Chapter 2", goal_id="write_my_thesis")])

    assert (await _build(repos)).task_kind == "write"


@pytest.mark.asyncio
async def test_workspace__woop_card__from_the_tasks_intention_and_its_goal(repos, offline):
    await _accept(repos, U, [_row(
        "t1", "Revise DBMS normalisation", goal_id="dbms", implementation_intention=INTENTION,
    )])
    await repos.goals.add_deadline_update(U, GoalDeadlineUpdate(
        user_id=U, goal_id="dbms", source="planning",
        context_snippet=json.dumps({
            "planning_goal": "Ace the DBMS mid-sem",
            "goal_metadata": {"outcome_visualization": "Walking out of the exam calm"},
        }),
    ))

    card = (await _build(repos)).woop_card

    assert card is not None
    assert (card.wish, card.outcome) == ("Ace the DBMS mid-sem", "Walking out of the exam calm")
    assert (card.obstacle, card.plan) == (
        INTENTION["obstacle_trigger"], INTENTION["behavioral_response"],
    )


@pytest.mark.asyncio
async def test_workspace__intention_without_a_plan__no_woop_card(repos, offline):
    await _accept(repos, U, [_row(
        "t1", "Revise DBMS normalisation",
        implementation_intention={"obstacle_trigger": "Phone", "behavioral_response": ""},
    )])

    assert (await _build(repos)).woop_card is None


# --- whose task, and when the store fails ------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace__another_users_task_id__not_found_not_their_title(repos, offline):
    await _accept(repos, OTHER, [_row("t1", "Bob's private task")])

    with pytest.raises(errors.NotFoundError):
        await _build(repos, user_id=U)


@pytest.mark.asyncio
async def test_workspace_route__another_users_task_id__404_without_their_title(monkeypatch, offline):
    container = build_test_container()
    await _accept(container.repos, OTHER, [_row("t1", "Bob's private task")])

    response = await _get(monkeypatch, container, "t1")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "Bob's private task" not in response.text


@pytest.mark.asyncio
async def test_workspace__repository_fails__typed_error_not_500(monkeypatch, offline):
    container = build_test_container()

    async def unreachable(*_args):
        raise errors.PersistenceError("Could not reach the database.")

    monkeypatch.setattr(container.repos.tasks, "get", unreachable)

    response = await _get(monkeypatch, container, "t1")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "persistence_unavailable"


@pytest.mark.asyncio
async def test_workspace__no_task_store__persistence_error_not_a_made_up_title(offline):
    with pytest.raises(errors.PersistenceError):
        await wb.build_task_workspace(U, "t1", tasks=None, bus=NullEventBus())


@pytest.mark.asyncio
async def test_workspace_route__task_with_criteria__criteria_on_the_wire(monkeypatch, offline):
    container = build_test_container()
    await _accept(container.repos, U, [_row(
        "t1", "Revise DBMS normalisation", completion_criteria=CRITERIA, deadline_hint="Friday",
    )])

    body = (await _get(monkeypatch, container, "t1")).json()

    assert (body["completion_criteria"], body["duration_minutes"], body["deadline_hint"]) == (
        CRITERIA, 25, "Friday",
    )
    assert body["task_kind"] == "study"


async def _get(monkeypatch, container, task_id: str) -> httpx.Response:
    from app.main import app

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(wb, "_read_cached_workspace", nothing)
    monkeypatch.setattr(wb, "_write_cached_workspace", nothing)
    monkeypatch.setattr(app.state, "container", container, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/api/v1/tasks/{task_id}/workspace", params={"user_id": U})


# --- the cache --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_cache__row_built_before_these_fields__still_loads(monkeypatch):
    """A workspace cached before task_kind, metadata.quiz and the task's details existed."""
    old = {
        "task_id": "t1",
        "task_title": "Revise DBMS normalisation",
        "primary_objective": "Revise DBMS normalisation",
        "surfaced_assets": [
            {"asset_type": "pdf_chunk", "title": "Material excerpt 1",
             "content_or_url": "Unit 3: normal forms", "rationale": "From your ingested materials"},
            {"asset_type": "generated_quiz", "title": "Quiz: DBMS",
             "content_or_url": "1. What is 3NF?", "rationale": "From your syllabus"},
        ],
        "woop_card": None,
        "notices": [],
    }
    expires = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    db = FakeSupabase({"task_workspaces": [
        {"user_id": U, "task_id": "t1", "workspace_json": old, "expires_at": expires},
    ]})
    monkeypatch.setattr(wb, "_get_supabase", lambda: db)

    ws = await wb._read_cached_workspace(U, "t1")

    assert ws is not None
    assert (ws.task_kind, ws.completion_criteria, ws.duration_minutes, ws.deadline_hint) == (
        "study", "", None, None,
    )
    assert [a.title for a in ws.surfaced_assets] == ["Material excerpt 1", "Quiz: DBMS"]
    assert ws.surfaced_assets[1].metadata is None  # the page falls back to its markdown


# --- the builder keeps to its layer -------------------------------------------------------------


def test_workspace_builder__source__no_raw_task_or_material_reads_and_no_print():
    root = Path(wb.__file__).parent
    sources = [Path(wb.__file__), *sorted((root / "workspace").glob("*.py"))]

    for source in sources:
        text = source.read_text()
        assert 'table("user_tasks")' not in text, source
        assert 'table("task_materials")' not in text, source
        assert "print(" not in text, source


# --- what a task row reads as -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_task__intention_stored_as_json_text__reads_as_a_dict_and_junk_as_none():
    from app.infrastructure.persistence.supabase_repos import SupabaseTaskRepository

    base = {"user_id": U, "status": "pending", "duration_minutes": 25, "difficulty_weight": 0.5}
    db = FakeSupabase({"user_tasks": [
        {**base, "task_id": "t1", "title": "A", "implementation_intention": json.dumps(INTENTION),
         "topic_keywords": None},
        {**base, "task_id": "t2", "title": "B", "implementation_intention": "not json"},
    ]})
    tasks = SupabaseTaskRepository(db)

    first, second = await tasks.get(U, "t1"), await tasks.get(U, "t2")

    assert first.implementation_intention == INTENTION and first.topic_keywords == []
    assert second.implementation_intention is None
