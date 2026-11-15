"""The workspace the model composes for a task, and the fixed recipe it falls back to.

The model is scripted, the web search is a recorder, and the task's material is whatever
the test hands ``fetch_chunks_for_task``.
"""

import pytest

import app.services.analytical.workspace_builder as wb
from app.domain.entities import MemorySource
from app.domain.events import NullEventBus
from app.infrastructure.persistence.factory import RepositoryFactory
from app.schemas.workspace import TaskWorkspace, WorkspaceBlock, WorkspaceItem
from app.services.analytical.workspace.composer import (
    NOT_FROM_DOCUMENTS,
    ComposerTask,
    block_asset,
    block_markdown,
    composer_prompt,
)
from app.services.analytical.workspace.materials import MaterialChunk
from app.services.memory.memory_service import MemoryCandidate, MemoryService
from tests.fakes import BagOfWordsEmbedder, accept_tasks, install_scripted_model

pytestmark = pytest.mark.usefixtures("compose_workspaces")

U = "alice"
SYLLABUS = "DBMS-Syllabus-CS304.pdf"
MATERIAL = [MaterialChunk("Unit 3: 1NF, 2NF, 3NF and BCNF.", "dbms", SYLLABUS)]


def _questions(n: int = 3, type_: str = "questions") -> dict:
    return {
        "type": type_, "title": "Normal forms", "why": "Checks you can apply each form",
        "items": [{"text": f"Q{i}?", "answer": f"A{i}.", "hint": ""} for i in range(n)],
    }


def _composition(**changes) -> dict:
    base = {
        "kind": "study",
        "approach": "Spend ten minutes recalling the forms, then test yourself.",
        "material_is_relevant": True,
        "blocks": [_questions()],
        "search": None,
    }
    return {**base, **changes}


class _Env:
    def __init__(self, monkeypatch) -> None:
        self.searches: list[str] = []
        self.answer = "https://example.org/guide | A guide"
        self.material: list[MaterialChunk] = []
        self.repos = RepositoryFactory.in_memory()
        self.memory: MemoryService | None = None

        async def search(user_prompt: str, system_prompt: str, *, bus) -> str:
            self.searches.append(user_prompt)
            if not self.answer:
                raise wb.WebSearchUnavailableError(wb.WEB_RESULTS_UNAVAILABLE)
            return self.answer

        async def fetch(*_args, **_kwargs):
            return self.material

        async def reader(*_args, **_kwargs):
            return "reader"

        monkeypatch.setattr(wb, "_search_web", search)
        monkeypatch.setattr(wb, "fetch_chunks_for_task", fetch)
        monkeypatch.setattr(wb, "get_learning_style", reader)
        monkeypatch.setattr(wb, "_get_supabase", lambda: None)

    async def build(self, title: str, **task) -> TaskWorkspace:
        await accept_tasks(self.repos, U, [{"task_id": "t1", "title": title, **task}])
        return await wb.build_task_workspace(
            U, "t1", tasks=self.repos.tasks, memory=self.memory, bus=NullEventBus()
        )


@pytest.fixture
def env(monkeypatch) -> _Env:
    return _Env(monkeypatch)


def _types(ws) -> list[str]:
    return [a.asset_type for a in ws.surfaced_assets]


# --- what the model composes is what the workspace holds ---------------------------------


@pytest.mark.asyncio
async def test_composed__blocks_in_the_models_order__with_its_kind_and_approach(env):
    env.material = MATERIAL
    model = install_scripted_model(workspace_composition=_composition(
        kind="write",
        blocks=[
            {"type": "outline", "title": "README outline", "why": "Gives you the sections",
             "items": [{"text": "What it does", "answer": "One paragraph."},
                       {"text": "How to run it", "answer": ""}]},
            {"type": "checklist", "title": "Before it is done", "why": "So a classmate can follow it",
             "items": [{"text": "Every command was run once"}]},
        ],
    ))

    ws = await env.build("Course project: write the README")

    assert ws.task_kind == "write"
    assert ws.approach.startswith("Spend ten minutes")
    assert _types(ws) == ["pdf_chunk", "generated_content", "generated_content"]
    outline, checklist = ws.surfaced_assets[1:]
    assert outline.content_or_url == "1. **What it does**: One paragraph.\n2. **How to run it**"
    assert checklist.content_or_url == "- [ ] Every command was run once"
    assert outline.metadata == {"block": "outline"}
    assert model.tasks == ["workspace_composition"]  # one model call, no recipe behind it
    assert env.searches == []  # the model asked for no search


@pytest.mark.asyncio
async def test_composed__questions_block__a_structured_quiz_named_after_its_source(env):
    env.material = MATERIAL
    install_scripted_model(workspace_composition=_composition())

    ws = await env.build("Revise DBMS normalisation")

    (quiz,) = [a for a in ws.surfaced_assets if a.asset_type == "generated_quiz"]
    assert len(quiz.metadata["quiz"]["questions"]) == 3
    assert quiz.rationale == f"Checks you can apply each form · from {SYLLABUS}"
    assert "**1. Q0?**" in quiz.content_or_url


@pytest.mark.asyncio
async def test_composed__no_material__blocks_say_they_are_not_from_documents(env):
    install_scripted_model(workspace_composition=_composition(kind="practice"))

    ws = await env.build("DP practice: knapsack and its variants")

    assert _types(ws) == ["generated_quiz"]
    assert ws.surfaced_assets[0].rationale.endswith(NOT_FROM_DOCUMENTS)


@pytest.mark.asyncio
async def test_composed__model_finds_the_material_unrelated__no_excerpt_and_not_credited(env):
    env.material = [MaterialChunk("Semester 5 marks: DBMS 8, OS 9", "t", "Marks-Transcript.pdf")]
    install_scripted_model(workspace_composition=_composition(material_is_relevant=False))

    ws = await env.build("Course project: write the README")

    assert "pdf_chunk" not in _types(ws)
    assert all("Marks-Transcript" not in a.rationale for a in ws.surfaced_assets)


@pytest.mark.asyncio
async def test_composed__too_few_questions__shown_as_text_not_a_quiz(env):
    install_scripted_model(workspace_composition=_composition(blocks=[_questions(2)]))

    ws = await env.build("Revise DBMS normalisation")

    assert _types(ws) == ["generated_content"]


# --- the one search the model may ask for ------------------------------------------------


@pytest.mark.asyncio
async def test_composed__search_asked_for__one_search_with_the_models_query(env):
    install_scripted_model(workspace_composition=_composition(
        search={"query": "third normal form worked example", "want": "article"},
    ))

    ws = await env.build("Revise DBMS normalisation")

    assert len(env.searches) == 1 and "third normal form worked example" in env.searches[0]
    assert ws.surfaced_assets[-1].content_or_url == "https://example.org/guide"


@pytest.mark.asyncio
async def test_composed__problem_sites__only_for_programming_practice(env):
    env.answer = (
        "https://leetcode.com/problems/coin-change | Coin Change\n"
        "https://example.org/essay-practice | Practice prompts"
    )
    wanted = {"query": "practice", "want": "practice_problems"}
    install_scripted_model(workspace_composition=[
        _composition(kind="practice", search=wanted), _composition(kind="practice", search=wanted),
    ])

    essay = await env.build("Practise essay introductions")
    await accept_tasks(env.repos, U, [{"task_id": "t2", "title": "DP practice: knapsack"}])
    coding = await wb.build_task_workspace(U, "t2", tasks=env.repos.tasks, bus=NullEventBus())

    assert not any("leetcode.com" in a.content_or_url for a in essay.surfaced_assets)
    assert any("leetcode.com" in a.content_or_url for a in coding.surfaced_assets)


@pytest.mark.asyncio
async def test_composed__search_cannot_run__blocks_kept_and_a_notice(env):
    env.answer = ""
    install_scripted_model(workspace_composition=_composition(
        search={"query": "normal forms", "want": "video"},
    ))

    ws = await env.build("Revise DBMS normalisation")

    assert _types(ws) == ["generated_quiz"]
    assert ws.notices == [wb.WEB_RESULTS_UNAVAILABLE]


# --- what the model is told --------------------------------------------------------------


@pytest.mark.asyncio
async def test_composed__prompt__carries_the_task_its_criteria_and_what_jarvis_remembers(env):
    env.material = MATERIAL
    env.memory = MemoryService(env.repos.memories, BagOfWordsEmbedder())
    await env.memory.remember(
        U,
        MemoryCandidate(
            memory_type="goal",
            content="Preparing for placement interviews, so DSA matters most",
            confidence=0.9,
        ),
        provenance=MemorySource.SAID,
    )
    model = install_scripted_model(workspace_composition=_composition())

    await env.build(
        "Revise DBMS normalisation",
        completion_criteria="Normalise one schema to 3NF from memory",
        duration_minutes=25,
    )

    prompt = model.calls[0].prompt
    assert 'Task: "Revise DBMS normalisation"' in prompt
    assert "Done when: Normalise one schema to 3NF from memory" in prompt
    assert "Time planned: 25 minutes" in prompt
    assert "Preparing for placement interviews" in prompt
    assert f"[From {SYLLABUS}]" in prompt


def test_composer_prompt__no_material__says_so():
    prompt = composer_prompt(ComposerTask(title="Submit the form"), [])

    assert "There is no material" in prompt and "[From" not in prompt


@pytest.mark.asyncio
async def test_composed__memory_cannot_answer__still_composed(env):
    class Down:
        async def recall(self, *_a, **_k):
            raise RuntimeError("down")

    env.memory = Down()
    install_scripted_model(workspace_composition=_composition())

    ws = await env.build("Revise DBMS normalisation")

    assert _types(ws) == ["generated_quiz"]


# --- the fallback: the fixed recipe of the task's kind ------------------------------------


@pytest.mark.asyncio
async def test_fallback__model_cannot_compose__the_kinds_recipe_and_no_approach(env):
    model = install_scripted_model(workspace_outline={
        "title": "README outline",
        "sections": [{"heading": h, "note": ""} for h in ("What", "Run", "How")],
        "checklist": ["Commands run", "Screenshots current"],
    })

    ws = await env.build("Course project: write the README")

    assert model.tasks == ["workspace_composition", "workspace_outline"]
    assert ws.task_kind == "write" and ws.approach == ""
    assert "generated_content" in _types(ws)


@pytest.mark.asyncio
async def test_fallback__composition_with_nothing_to_show__the_kinds_recipe(env):
    model = install_scripted_model(workspace_composition=_composition(
        blocks=[{"type": "checklist", "title": "Empty", "why": "", "items": []}],
    ))

    ws = await env.build("Revise DBMS normalisation")

    assert model.tasks[0] == "workspace_composition" and ws.approach == ""


@pytest.mark.asyncio
async def test_fallback__composer_switched_off__no_composition_call(env, monkeypatch):
    import app.core.config as config

    monkeypatch.setattr(config, "WORKSPACE_COMPOSER", False)
    model = install_scripted_model(workspace_composition=_composition())

    await env.build("Submit the quiz answers")

    assert "workspace_composition" not in model.tasks


def test_cached_workspace__built_before_the_composer__still_loads():
    old = {"task_id": "t", "task_title": "T", "primary_objective": "T", "surfaced_assets": []}

    assert TaskWorkspace.model_validate(old).approach == ""


# --- one block as markdown ---------------------------------------------------------------


def _block(type_: str, texts: list[str], body: str = "") -> WorkspaceBlock:
    return WorkspaceBlock(
        type=type_, title="T", why="W", items=[WorkspaceItem(text=t) for t in texts], body=body
    )


@pytest.mark.parametrize("type_, texts, body, expected", [
    ("checklist", ["Roll number", "Portal login"], "", "- [ ] Roll number\n- [ ] Portal login"),
    ("steps", ["Read the trace", "Add FIFO"], "",
     "**First step:** Read the trace\n\n### Then\n\n- [ ] Add FIFO"),
    ("key_points", ["3NF"], "", "- **3NF**"),
    ("worked_example", [], "Take R(A, B, C)...", "Take R(A, B, C)..."),
    ("note", [], "", ""),
])
def test_block_markdown__each_type(type_, texts, body, expected):
    assert block_markdown(_block(type_, texts, body)) == expected


def test_block_asset__nothing_to_show__none():
    assert block_asset(_block("checklist", ["  "]), [], False) is None
