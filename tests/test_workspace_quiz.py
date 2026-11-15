"""A workspace quiz is data: questions, answers and hints, parsed through Pydantic.

The model is scripted (``install_scripted_model``); its replies are realistic model
output: plain JSON, JSON in a markdown fence, prose, a quiz too short.
"""

import json
import logging

import pytest

from app.schemas.workspace import Quiz, TaskWorkspace
from app.services.analytical.workspace import quiz as quiz_module
from app.services.analytical.workspace.materials import MaterialChunk
from tests.fakes import install_scripted_model

SYLLABUS = "DBMS-Syllabus-CS304.pdf"
MATERIAL = [
    MaterialChunk("Unit 3: 1NF, 2NF, 3NF and BCNF; functional dependencies.", "dbms", SYLLABUS),
]


def _questions(n: int) -> list[dict]:
    return [
        {"question": f"Question {i}?", "answer": f"Answer {i}.", "hint": f"Hint {i}"}
        for i in range(1, n + 1)
    ]


def _reply(n: int = 3, title: str = "Normal forms") -> str:
    return json.dumps({"assets": [{"title": title, "questions": _questions(n)}]})


async def _quiz(reply) -> list:
    install_scripted_model(practice_generation=reply)
    return await quiz_module.generate_quiz(MATERIAL, "Revise DBMS normalisation")


@pytest.mark.asyncio
async def test_quiz__valid_reply__asset_carries_structured_questions_and_markdown():
    model = install_scripted_model(practice_generation=_reply(3))

    (asset,) = await quiz_module.generate_quiz(MATERIAL, "Revise DBMS normalisation")

    assert model.tasks == ["practice_generation"] and model.calls[0].role == "primary"
    assert asset.asset_type == "generated_quiz"
    assert asset.metadata == {"quiz": {"title": "Normal forms", "questions": _questions(3)}}
    assert Quiz.model_validate(asset.metadata["quiz"]).questions[0].hint == "Hint 1"
    for q in _questions(3):  # the same quiz, as markdown, for an older page or a cached row
        assert q["question"] in asset.content_or_url and q["answer"] in asset.content_or_url
    assert asset.title == "Quiz: Normal forms"


@pytest.mark.asyncio
async def test_quiz__rationale_names_the_source_document_never_your_syllabus():
    (asset,) = await _quiz(_reply(4))

    assert asset.rationale == f"From {SYLLABUS}"
    assert "syllabus" not in asset.rationale.lower().replace(SYLLABUS.lower(), "")


@pytest.mark.asyncio
async def test_quiz__prompt__names_each_chunks_document_and_allows_no_quiz():
    model = install_scripted_model(practice_generation=_reply(3))

    await quiz_module.generate_quiz(MATERIAL, "Revise DBMS normalisation")

    prompt = model.calls[0].prompt
    assert f"[From {SYLLABUS}]" in prompt
    assert '{"assets": []}' in prompt


@pytest.mark.asyncio
async def test_quiz__reply_in_markdown_fences__parsed():
    (asset,) = await _quiz(f"```json\n{_reply(5)}\n```\n")

    assert len(asset.metadata["quiz"]["questions"]) == 5


@pytest.mark.asyncio
async def test_quiz__bare_quiz_object_without_the_assets_wrapper__parsed():
    (asset,) = await _quiz(json.dumps({"title": "Normal forms", "questions": _questions(3)}))

    assert asset.metadata["quiz"]["title"] == "Normal forms"


@pytest.mark.asyncio
async def test_quiz__hint_left_null__read_as_no_hint():
    questions = [{**q, "hint": None} for q in _questions(3)]

    (asset,) = await _quiz(json.dumps({"assets": [{"title": "T", "questions": questions}]}))

    assert [q["hint"] for q in asset.metadata["quiz"]["questions"]] == ["", "", ""]


@pytest.mark.asyncio
async def test_quiz__malformed_reply__no_asset_and_no_raw_text(caplog):
    raw = "Sure! Here is your quiz:\n1. What is 3NF?\nAnswer: no transitive dependencies."

    with caplog.at_level(logging.WARNING):
        assets = await _quiz(raw)

    assert assets == []
    assert "What is 3NF" not in caplog.text  # the exception's type is logged, not the reply


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 6])
async def test_quiz__two_questions_only__rejected(count):
    assert await _quiz(_reply(count)) == []


@pytest.mark.asyncio
async def test_quiz__model_says_material_is_unrelated__empty_assets__no_quiz():
    assert await _quiz('{"assets": []}') == []


@pytest.mark.asyncio
async def test_quiz__no_material__no_model_call():
    model = install_scripted_model(practice_generation=_reply(3))

    assert await quiz_module.generate_quiz([], "Revise DBMS normalisation") == []
    assert model.calls == []


@pytest.mark.asyncio
async def test_quiz__model_down__no_asset():
    from app.domain.errors import LLMUnavailableError

    assert await _quiz(LLMUnavailableError("scripted outage")) == []


def test_quiz__workspace_round_trip__quiz_metadata_survives_the_cache_json():
    from app.schemas.workspace import StudyAsset

    ws = TaskWorkspace(
        task_id="t1", task_title="T", primary_objective="T",
        surfaced_assets=[StudyAsset(
            asset_type="generated_quiz", title="Quiz: T", content_or_url="**1. Q?**",
            metadata={"quiz": {"title": "T", "questions": _questions(3)}},
        )],
    )

    again = TaskWorkspace.model_validate(json.loads(ws.model_dump_json()))

    assert again.surfaced_assets[0].metadata["quiz"]["questions"] == _questions(3)


# --- on the wire ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_route__quiz__metadata_quiz_questions_is_a_list(monkeypatch):
    import httpx

    import app.services.analytical.workspace_builder as wb
    from app.main import app
    from tests.fakes import accept_tasks, build_test_container

    async def nothing(*_args, **_kwargs):
        return None

    async def material(*_args, **_kwargs):
        return MATERIAL

    async def reader(*_args, **_kwargs):
        return "reader"

    async def one_link(*, user_prompt: str, system_prompt: str) -> str:
        return "https://example.org/normal-forms | Normal forms, explained"

    monkeypatch.setattr(wb, "_get_supabase", lambda: None)
    monkeypatch.setattr(wb, "_read_cached_workspace", nothing)
    monkeypatch.setattr(wb, "_write_cached_workspace", nothing)
    monkeypatch.setattr(wb, "fetch_chunks_for_task", material)
    monkeypatch.setattr(wb, "get_learning_style", reader)
    monkeypatch.setattr(wb, "gemini_web_search", one_link)
    container = build_test_container()
    await accept_tasks(container.repos, "alice", [
        {"task_id": "dbms_t1", "title": "Revise DBMS normalisation"},
    ])
    monkeypatch.setattr(app.state, "container", container, raising=False)
    install_scripted_model(practice_generation=_reply(3))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/tasks/dbms_t1/workspace", params={"user_id": "alice"})

    assert response.status_code == 200
    (quiz,) = [a for a in response.json()["surfaced_assets"] if a["asset_type"] == "generated_quiz"]
    assert isinstance(quiz["metadata"]["quiz"]["questions"], list)
    assert len(quiz["metadata"]["quiz"]["questions"]) == 3
