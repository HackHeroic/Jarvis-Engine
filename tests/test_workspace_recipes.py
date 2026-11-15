"""Each kind of task gets the workspace that fits it.

The web search (``_search_web``) is replaced by a recorder, the model is scripted, and
the task's material is whatever the test hands ``fetch_chunks_for_task``.
"""

import json

import pytest

import app.services.analytical.workspace_builder as wb
from app.domain.events import NullEventBus
from app.schemas.workspace import TaskOutline
from app.services.analytical.workspace.materials import MaterialChunk
from tests.fakes import FakeSupabase, install_scripted_model

U = "alice"
SYLLABUS = "DBMS-Syllabus-CS304.pdf"
MATERIAL = [MaterialChunk("Unit 3: 1NF, 2NF, 3NF and BCNF.", "dbms", SYLLABUS)]
PROBLEM_SITES = ("leetcode.com", "codeforces.com")

OUTLINE = {
    "title": "README outline",
    "sections": [
        {"heading": "What it does", "note": "One paragraph on the project."},
        {"heading": "How to run it", "note": "Install and start commands."},
        {"heading": "How it works", "note": "The main parts."},
    ],
    "checklist": ["Every command was run once", "Screenshots are current"],
}
STEPS = {
    "title": "Page replacement simulator",
    "sections": [],
    "checklist": [
        "Write a function that replays a page reference string with FIFO",
        "Add LRU",
        "Add Optimal",
        "Print the page faults of each",
    ],
}
READY = {
    "title": "Before you submit",
    "sections": [],
    "checklist": ["The answer sheet as a PDF", "Your roll number", "The portal login"],
}
QUIZ = json.dumps({"assets": [{"title": "Normal forms", "questions": [
    {"question": f"Q{i}?", "answer": f"A{i}.", "hint": ""} for i in range(3)
]}]})


class _Env:
    """A workspace build with a recorded web search and scripted material."""

    def __init__(self, monkeypatch) -> None:
        self.searches: list[str] = []
        self.fetches = 0
        self.answer = "https://example.org/guide | A guide"
        self.material: list[MaterialChunk] = []
        self._db = FakeSupabase()

        async def search(user_prompt: str, system_prompt: str, *, bus) -> str:
            self.searches.append(user_prompt)
            if not self.answer:
                raise wb.WebSearchUnavailableError(wb.WEB_RESULTS_UNAVAILABLE)
            return self.answer

        async def fetch(*_args, **_kwargs):
            self.fetches += 1
            return self.material

        async def reader(*_args, **_kwargs):
            return "reader"

        monkeypatch.setattr(wb, "_search_web", search)
        monkeypatch.setattr(wb, "fetch_chunks_for_task", fetch)
        monkeypatch.setattr(wb, "get_learning_style", reader)
        monkeypatch.setattr(wb, "_get_supabase", lambda: self._db)

    async def build(self, title: str, keywords: list[str] | None = None, goal_id: str | None = None):
        self._db.rows["user_tasks"] = [{
            "user_id": U, "task_id": "t1", "title": title, "topic_keywords": keywords or [],
            "goal_id": goal_id, "created_at": "2026-11-16T08:00:00+00:00",
        }]
        return await wb.build_task_workspace(U, "t1", bus=NullEventBus())


@pytest.fixture
def env(monkeypatch) -> _Env:
    return _Env(monkeypatch)


def _types(ws) -> list[str]:
    return [a.asset_type for a in ws.surfaced_assets]


def _urls(ws) -> list[str]:
    return [a.content_or_url for a in ws.surfaced_assets if a.content_or_url.startswith("http")]


# --- the plan's cases --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace__write_task__no_quiz_and_no_problem_links(env):
    env.material = MATERIAL
    env.answer = (
        "https://leetcode.com/problems/two-sum | Two Sum\n"
        "https://www.makeareadme.com | Make a README"
    )
    model = install_scripted_model(workspace_outline=OUTLINE)

    ws = await env.build("Course project: write the README")

    assert ws.task_kind == "write"
    assert "generated_quiz" not in _types(ws)
    assert not any(site in url for url in _urls(ws) for site in PROBLEM_SITES)
    assert not {"leetcode_link", "codeforces_link"} & set(_types(ws))
    assert _urls(ws) == ["https://www.makeareadme.com"]  # one guide
    (outline,) = [a for a in ws.surfaced_assets if a.asset_type == "generated_content"]
    assert "How to run it" in outline.content_or_url
    assert "Screenshots are current" in outline.content_or_url
    assert model.tasks == ["workspace_outline"]  # no quiz call
    assert len(env.searches) == 1


@pytest.mark.asyncio
async def test_workspace__admin_task__no_web_search_is_made(env):
    env.material = MATERIAL
    install_scripted_model(workspace_outline=READY)

    ws = await env.build("Submit the quiz answers")

    assert ws.task_kind == "admin"
    assert env.searches == []  # _search_web was not called
    assert env.fetches == 0  # and no document was read for it
    assert _types(ws) == ["generated_content"]
    assert "Your roll number" in ws.surfaced_assets[0].content_or_url
    assert ws.notices == []


@pytest.mark.asyncio
async def test_workspace__practice_task_not_about_programming__no_leetcode_links(env):
    env.answer = (
        "https://leetcode.com/problems/two-sum | Two Sum\n"
        "https://codeforces.com/problemset/problem/1/A | Theatre Square\n"
        "https://www.masterorganicchemistry.com/quizzes | Mechanism practice\n"
        "https://www.youtube.com/watch?v=mech | Arrow pushing, explained"
    )
    install_scripted_model()

    ws = await env.build("Practice organic chemistry reaction mechanisms")

    assert ws.task_kind == "practice"
    assert not any(site in url for url in _urls(ws) for site in PROBLEM_SITES)
    assert _urls(ws) == [
        "https://www.masterorganicchemistry.com/quizzes", "https://www.youtube.com/watch?v=mech",
    ]
    assert "leetcode" not in env.searches[0].lower()


@pytest.mark.asyncio
async def test_workspace__practice_task_about_programming__problem_links(env):
    env.answer = (
        "https://leetcode.com/problems/partition-equal-subset-sum | Partition Equal Subset Sum\n"
        "https://codeforces.com/problemset/problem/577/B | Modulo Sum\n"
        "https://www.youtube.com/watch?v=knap | 0/1 knapsack"
    )
    install_scripted_model()

    ws = await env.build("DP practice: knapsack and its variants")

    assert ws.task_kind == "practice"
    assert _types(ws) == ["leetcode_link", "codeforces_link", "youtube_link"]
    assert "leetcode" in env.searches[0].lower() and len(env.searches) == 1


# --- the other kinds ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace__build_task__smallest_first_step_then_the_rest_and_one_reference(env):
    env.answer = "https://en.wikipedia.org/wiki/Page_replacement_algorithm | Page replacement"
    model = install_scripted_model(workspace_outline=STEPS)

    ws = await env.build("Page replacement simulator", ["operating systems"])

    assert ws.task_kind == "build"
    (steps,) = [a for a in ws.surfaced_assets if a.asset_type == "generated_content"]
    first = steps.content_or_url.index("replays a page reference string")
    assert first < steps.content_or_url.index("Add LRU")
    assert "First step" in steps.content_or_url
    assert _urls(ws) == ["https://en.wikipedia.org/wiki/Page_replacement_algorithm"]
    assert (model.tasks, len(env.searches)) == (["workspace_outline"], 1)


@pytest.mark.asyncio
async def test_workspace__study_task__excerpts_quiz_and_one_learning_style_link(env):
    env.material = MATERIAL
    model = install_scripted_model(practice_generation=QUIZ)

    ws = await env.build("Revise DBMS normalisation", ["normalisation"])

    assert ws.task_kind == "study"
    assert _types(ws) == ["pdf_chunk", "generated_quiz", "article_link"]
    assert (model.tasks, len(env.searches)) == (["practice_generation"], 1)


@pytest.mark.asyncio
async def test_workspace__study_task_without_material__no_quiz_call(env):
    model = install_scripted_model(practice_generation=QUIZ)

    ws = await env.build("Revise DBMS normalisation")

    assert "generated_quiz" not in _types(ws) and model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("title, searches_allowed", [
    ("Course project: write the README", 1),
    ("Page replacement simulator", 1),
    ("Submit the quiz answers", 0),
    ("DP practice: knapsack and its variants", 1),
    ("Revise DBMS normalisation", 1),
])
async def test_workspace__any_kind__one_generation_call_at_most_and_its_search_budget(
    env, title, searches_allowed
):
    env.material = MATERIAL
    model = install_scripted_model(
        workspace_outline=READY, practice_generation=QUIZ
    )

    await env.build(title)

    assert len(model.calls) <= 1
    assert len(env.searches) <= searches_allowed


@pytest.mark.asyncio
async def test_workspace__write_task_search_down__outline_kept_with_the_notice(env):
    env.answer = ""  # the search is unavailable
    install_scripted_model(workspace_outline=OUTLINE)

    ws = await env.build("Course project: write the README")

    assert ws.notices == [wb.WEB_RESULTS_UNAVAILABLE]
    assert _types(ws) == ["generated_content"]


# --- the generated outline ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_outline__structured_call__primary_role_with_its_response_schema(env):
    model = install_scripted_model(workspace_outline=OUTLINE)

    await env.build("Course project: write the README")

    (call,) = model.calls
    assert (call.role, call.response_schema) == ("primary", TaskOutline)


@pytest.mark.asyncio
async def test_outline__reply_not_an_outline__no_asset_and_no_raw_text(env):
    install_scripted_model(workspace_outline="Sure! First, explain what the project does.")

    ws = await env.build("Course project: write the README")

    assert "generated_content" not in _types(ws)
    assert not any("Sure!" in a.content_or_url for a in ws.surfaced_assets)


@pytest.mark.asyncio
async def test_outline__admin_checklist_of_two__no_asset(env):
    install_scripted_model(workspace_outline={**READY, "checklist": READY["checklist"][:2]})

    ws = await env.build("Submit the quiz answers")

    assert ws.surfaced_assets == []


@pytest.mark.asyncio
async def test_outline__write_task_with_material__prompt_carries_it_and_criteria_hint(env):
    env.material = MATERIAL
    model = install_scripted_model(workspace_outline=OUTLINE)

    await env.build("Course project: write the README")

    assert f"[From {SYLLABUS}]" in model.calls[0].prompt
