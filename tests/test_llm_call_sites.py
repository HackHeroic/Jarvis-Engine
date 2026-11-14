"""Every live LLM call site reaches a model through ``route_llm_call`` and keeps its contract.

Each test installs a scripted provider as the whole provider chain (``set_chain``).
A site that still called the LiteLLM transport directly would never reach it, so
"the scripted chain saw a request for task X" is the proof the call went through
the chain — and with it the PII redaction, the cloud fallback and the usage ledger.
Each site's return shape and failure fallback are pinned as its callers rely on them.

Web search is the exception: it needs Gemini's search grounding, which no other
provider offers, so it stays on ``gemini_web_search``. Its tests pin that a failed
search says so plainly and never surfaces a link.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException

import app.core.config as _cfg
from app.domain.entities import LLMLocation
from app.domain.errors import LLMUnavailableError
from app.domain.ports import LLMProvider, LLMRequest
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import set_chain
from app.infrastructure.llm.transport import strip_fences, translated_errors
from tests.fakes import FakeDBClient, FakeSupabase


class ScriptedModel(LLMProvider):
    """A model that answers each task from a script and records every request.

    A reply is a value (text, a dict or a schema instance), an exception to raise,
    or a list of those consumed one per call. A task without a reply is unavailable.
    """

    name = "scripted"

    def __init__(self, replies: dict[str, Any], location: LLMLocation = LLMLocation.CLOUD) -> None:
        self.location = location
        self.calls: list[LLMRequest] = []
        self._replies = {task: list(r) if isinstance(r, list) else r for task, r in replies.items()}

    @property
    def tasks(self) -> list[str]:
        return [c.task for c in self.calls]

    def is_available(self) -> bool:
        return True

    def _answer(self, req: LLMRequest) -> Any:
        self.calls.append(req)
        reply = self._replies.get(req.task)
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else None
        if reply is None:
            raise LLMUnavailableError(f"no scripted reply for {req.task!r}")
        if isinstance(reply, BaseException):
            raise reply
        schema = req.response_schema
        if schema is None or isinstance(reply, schema):
            return reply
        with translated_errors():  # unparseable output is a model failure, as in the adapters
            if isinstance(reply, dict):
                return schema.model_validate(reply)
            return schema.model_validate_json(strip_fences(reply))

    async def complete(self, req: LLMRequest) -> Any:
        return self._answer(req)

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        text = self._answer(req)
        for i in range(0, len(text), 4):  # mid-word chunks, as a real stream arrives
            yield ("content", text[i : i + 4])


@pytest.fixture
def llm():
    """Install ``ScriptedModel(replies)`` as the only provider; returns it for inspection."""

    def install(**replies: Any) -> ScriptedModel:
        model = ScriptedModel(replies)
        set_chain(FallbackChainProvider([model]))  # conftest's _fresh_llm_chain resets it
        return model

    return install


def _down() -> LLMUnavailableError:
    """A fresh model outage (an exception instance is not shared between raises)."""
    return LLMUnavailableError("scripted outage")


# --- memory extraction --------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_extraction__routes_through_the_chain__returns_memory_dicts(llm):
    from unittest.mock import MagicMock

    from app.services.memory.extractor import extract_memories_from_turn

    model = llm(memory_extraction={"memories": [{"type": "fact", "content": "Studies CS", "confidence": 0.8}]})
    store = MagicMock()
    store.get_active_memories.return_value = []
    store.find_similar_memory.return_value = None

    result = await extract_memories_from_turn("u1", "I study CS", "Noted, sir.", store)

    assert model.tasks == ["memory_extraction"]
    assert model.calls[0].role == "fast"
    assert result == [
        {"type": "fact", "content": "Studies CS", "confidence": 0.8, "contradicts": None, "expires_at": None}
    ]


# --- Voice of Jarvis synthesis ------------------------------------------------------


@pytest.mark.asyncio
async def test_voj_synthesis__routes_through_the_chain__splits_thinking_from_message(llm):
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

    model = llm(
        voice_of_jarvis_synthesis="<think>Solved six tasks with CP-SAT.</think>Your schedule is set, sir."
    )
    history = [{"role": "user", "content": "plan my thursday"}]

    message, thinking = await synthesize_jarvis_response(
        {"schedule_generated": True}, conversation_history=history
    )

    assert model.tasks == ["voice_of_jarvis_synthesis"]
    assert model.calls[0].role == "fast"
    assert model.calls[0].conversation_history == history
    assert (message, thinking) == ("Your schedule is set, sir.", "Solved six tasks with CP-SAT.")


@pytest.mark.asyncio
async def test_voj_synthesis__model_down__returns_the_deterministic_reply(llm):
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

    llm(voice_of_jarvis_synthesis=_down())

    message, thinking = await synthesize_jarvis_response({"schedule_generated": True})

    # A proposed schedule is a draft under review, not a plan already set.
    assert message == "A proposed schedule is ready for your review, sir."
    assert thinking is None  # no model reasoning exists, so none is invented


@pytest.mark.asyncio
async def test_voj_synthesis__reply_without_think_block__full_message_and_no_thinking(llm):
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

    llm(voice_of_jarvis_synthesis="Your draft is ready, sir. Six tasks, none before nine.")

    message, thinking = await synthesize_jarvis_response({"schedule_generated": True})

    assert message == "Your draft is ready, sir. Six tasks, none before nine."
    assert thinking is None


@pytest.mark.asyncio
async def test_voj_stream__routes_through_the_chain__yields_thinking_then_message(llm):
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response_stream

    model = llm(voice_of_jarvis_synthesis="<think>Planned six tasks.</think>Done, sir.")
    history = [{"role": "user", "content": "plan my thursday"}]

    events = [
        e
        async for e in synthesize_jarvis_response_stream(
            {"schedule_generated": True}, conversation_history=history
        )
    ]

    assert model.tasks == ["voice_of_jarvis_synthesis"]
    assert model.calls[0].conversation_history == history
    assert {kind for kind, _ in events} == {"thinking", "message"}
    assert "".join(t for kind, t in events if kind == "thinking") == "Planned six tasks."
    assert "".join(t for kind, t in events if kind == "message") == "Done, sir."


@pytest.mark.asyncio
async def test_voj_stream__model_down__yields_the_fallback_events(llm):
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response_stream

    llm(voice_of_jarvis_synthesis=_down())

    events = [e async for e in synthesize_jarvis_response_stream({"schedule_generated": True})]

    assert events == [("message", "A proposed schedule is ready for your review, sir.")]


@pytest.mark.asyncio
async def test_voj_stream__model_down_and_no_schedule__claims_nothing_was_done(llm):
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response_stream

    llm(voice_of_jarvis_synthesis=_down())

    events = [
        e async for e in synthesize_jarvis_response_stream({"search_done": True, "schedule_generated": False})
    ]

    (kind, reply), = events
    assert kind == "message"
    assert "schedule" not in reply.lower() and "plan" not in reply.lower()
    assert "sorted" not in reply.lower()  # the old fallback claimed everything was done
    assert reply.endswith("sir. Shall we try again shortly?")


@pytest.mark.asyncio
async def test_voj_stream__reply_without_think_block__message_only(llm):
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response_stream

    llm(voice_of_jarvis_synthesis="Your draft is ready, sir.")

    events = [e async for e in synthesize_jarvis_response_stream({"schedule_generated": True})]

    assert {kind for kind, _ in events} == {"message"}
    assert "".join(t for _, t in events) == "Your draft is ready, sir."


def test_prompts__every_prompt_constant__never_asks_the_model_to_write_out_its_reasoning():
    """Asking a model to emit its reasoning in tags is declined by Claude's safeguards
    (the live reply synthesis came back refused), so no prompt may ask for it. A local
    model that emits a think block anyway is still parsed (tests above)."""
    import importlib
    import pkgutil

    import app

    prompts: dict[str, str] = {}
    for info in pkgutil.walk_packages(app.__path__, "app."):
        if info.name.startswith(("app.models.", "app.main")):
            continue  # ML stubs with heavy optional imports; no prompts live there
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue  # an optional dependency (docling, chromadb...) is not installed
        for attr, value in vars(module).items():
            if "PROMPT" in attr and isinstance(value, str):
                prompts[f"{info.name}.{attr}"] = value

    assert "app.services.analytical.voice_of_jarvis.VOICE_OF_JARVIS_PROMPT" in prompts
    assert "app.modules.conversation.JARVIS_CHAT_PROMPT" in prompts
    for name, text in prompts.items():
        assert "<think" not in text.lower() and "think>" not in text.lower(), name
    voice = prompts["app.services.analytical.voice_of_jarvis.VOICE_OF_JARVIS_PROMPT"]
    for asks_for_reasoning in ("thinking", "reasoning", "internally"):
        assert asks_for_reasoning not in voice.lower(), asks_for_reasoning


# --- habit translation ----------------------------------------------------------------

_SWIM = {"name": "swim", "start_min": 600, "end_min": 660, "availability": "blocked"}


@pytest.fixture
def habit_translator(monkeypatch):
    import app.services.analytical.habit_translator as module

    monkeypatch.setattr(module, "_habit_cache", {})
    return module


@pytest.mark.asyncio
async def test_habit_translation__routes_through_the_chain__returns_semantic_slots(llm, habit_translator):
    model = llm(habit_translation={"semantic_slots": [_SWIM]})

    slots = await habit_translator.translate_habits_to_slots("I swim on Saturdays")

    assert model.tasks == ["habit_translation"]
    assert model.calls[0].role == "primary"
    assert [s.name for s in slots] == ["swim"]


@pytest.mark.asyncio
async def test_habit_translation__empty_answer_with_time_anchor__strict_retry_on_the_chain(
    llm, habit_translator, monkeypatch
):
    monkeypatch.setattr(habit_translator, "_python_fallback_for_time_anchor", lambda _text: None)
    model = llm(habit_translation=[{"semantic_slots": []}, {"semantic_slots": [_SWIM]}])

    slots = await habit_translator.translate_habits_to_slots("No deep work after 9 pm")

    assert model.tasks == ["habit_translation", "habit_translation"]
    assert [s.name for s in slots] == ["swim"]


@pytest.mark.asyncio
async def test_habit_translation__model_down__raises_to_the_caller(llm, habit_translator):
    llm(habit_translation=_down())

    with pytest.raises(LLMUnavailableError):
        await habit_translator.translate_habits_to_slots("I swim on Saturdays")


# --- edit / reorder parsing -----------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_modification__routes_through_the_chain__returns_the_modification(llm):
    from app.services.analytical.schedule_modifier import (
        ScheduleModification,
        parse_modification_request,
    )

    model = llm(task_edit_parsing={
        "action": "change_duration", "target_task_title": "Graphs", "new_duration_minutes": 15,
    })

    result = await parse_modification_request("make graphs 15 minutes", [{"title": "Graphs"}])

    assert model.tasks == ["task_edit_parsing"]
    assert model.calls[0].role == "fast"
    assert isinstance(result, ScheduleModification)
    assert (result.action, result.new_duration_minutes) == ("change_duration", 15)


@pytest.mark.asyncio
async def test_schedule_modification__model_down__returns_none(llm):
    from app.services.analytical.schedule_modifier import parse_modification_request

    llm(task_edit_parsing=_down())

    assert await parse_modification_request("make graphs 15 minutes", [{"title": "Graphs"}]) is None


def _pending_tasks() -> FakeSupabase:
    return FakeSupabase({"user_tasks": [
        {"id": "r1", "user_id": "alice", "task_id": "t1", "title": "Graphs", "status": "pending",
         "duration_minutes": 25, "scheduled_start": "2026-11-14T09:00:00+00:00"},
        {"id": "r2", "user_id": "alice", "task_id": "t2", "title": "Heaps", "status": "pending",
         "duration_minutes": 25, "scheduled_start": "2026-11-14T10:00:00+00:00"},
    ]})


@pytest.mark.asyncio
async def test_task_edit__routes_through_the_chain__applies_the_edit(llm):
    from app.services.analytical.task_editor import handle_edit_task

    model = llm(task_edit_parsing='{"task_keyword": "graphs", "field": "duration_minutes", "new_value": 15}')
    db = _pending_tasks()

    out = await handle_edit_task("alice", "make graphs 15 minutes", None, FakeDBClient(db), None)

    assert model.tasks == ["task_edit_parsing"]
    assert out["intent"] == "EDIT_TASK"
    assert next(r for r in db.rows["user_tasks"] if r["task_id"] == "t1")["duration_minutes"] == 15


@pytest.mark.asyncio
async def test_task_edit__model_down__asks_to_rephrase(llm):
    from app.services.analytical.task_editor import handle_edit_task

    llm(task_edit_parsing=_down())

    out = await handle_edit_task("alice", "make graphs 15 minutes", None, FakeDBClient(_pending_tasks()), None)

    assert out == {
        "intent": "EDIT_TASK",
        "message": "Sorry, I couldn't understand which task to edit. Could you rephrase?",
    }


@pytest.mark.asyncio
async def test_task_rearrange__routes_through_the_chain__swaps_the_slots(llm):
    from app.services.analytical.task_rearranger import handle_rearrange

    model = llm(task_edit_parsing="[2, 1]")
    db = _pending_tasks()

    out = await handle_rearrange("alice", "do heaps first", None, FakeDBClient(db), None)

    assert model.tasks == ["task_edit_parsing"]
    assert out["intent"] == "REARRANGE"
    starts = {r["task_id"]: r["scheduled_start"] for r in db.rows["user_tasks"]}
    assert starts == {"t1": "2026-11-14T10:00:00+00:00", "t2": "2026-11-14T09:00:00+00:00"}


@pytest.mark.asyncio
async def test_task_rearrange__model_down__asks_to_rephrase(llm):
    from app.services.analytical.task_rearranger import handle_rearrange

    llm(task_edit_parsing=_down())

    out = await handle_rearrange("alice", "do heaps first", None, FakeDBClient(_pending_tasks()), None)

    assert out == {
        "intent": "REARRANGE",
        "message": "Sorry, I couldn't understand your rearrangement request. Could you rephrase?",
    }


# --- workspace practice quiz -----------------------------------------------------------


@pytest.mark.asyncio
async def test_practice_quiz__routes_through_the_chain__returns_the_quiz_assets(llm):
    from app.services.analytical.workspace_builder import generate_practice_assets

    quiz = {
        "asset_type": "generated_quiz", "title": "Quiz: Dijkstra",
        "content_or_url": "1. Why a heap?", "rationale": "From your syllabus",
    }
    model = llm(practice_generation=json.dumps({"assets": [quiz]}))

    assets = await generate_practice_assets(["Dijkstra uses a min-heap."], "Dijkstra", [])

    assert model.tasks == ["practice_generation"]
    assert model.calls[0].role == "primary"
    assert [a.model_dump(exclude={"metadata"}) for a in assets] == [quiz]


@pytest.mark.asyncio
async def test_practice_quiz__model_down__returns_no_assets(llm):
    from app.services.analytical.workspace_builder import generate_practice_assets

    llm(practice_generation=_down())

    assert await generate_practice_assets(["Dijkstra uses a min-heap."], "Dijkstra", []) == []


# --- documents and ingestion ------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_classification__routes_through_the_chain__returns_the_classification(llm):
    from app.schemas.document import DocumentClassification
    from app.services.documents.pipeline import classify_document

    model = llm(document_understanding={"document_type": "syllabus", "confidence": 0.9})

    result = await classify_document("Week 1: graphs", "Types: syllabus, reference")

    assert model.tasks == ["document_understanding"]
    assert model.calls[0].role == "primary"
    assert isinstance(result, DocumentClassification) and result.document_type == "syllabus"


@pytest.mark.asyncio
async def test_action_item_proposal__routes_through_the_chain__returns_the_proposal(llm):
    from app.services.extraction.action_item_handler import propose_action_item

    model = llm(action_item_extraction={
        "title": "Apply to Acme", "summary": "Internship",
        "deadline_mentioned": True, "deadline_date": "2026-11-20",
    })

    proposal = await propose_action_item("Apply to the Acme internship by Nov 20")

    assert model.tasks == ["action_item_extraction"]
    assert model.calls[0].role == "fast"
    assert (proposal.title, proposal.deadline_date) == ("Apply to Acme", "2026-11-20")
    assert proposal.id and proposal.created_at


@pytest.mark.asyncio
async def test_calendar_extraction__routes_through_the_chain__on_the_primary_model(llm):
    from app.schemas.context import ExtractedTimeSlots
    from app.services.extraction.calendar_extractor import extract_calendar_slots

    model = llm(calendar_parsing={
        "slots": [{"name": "OS Lec", "start_min": 60, "end_min": 120, "availability": "blocked"}],
        "source_summary": "Mon",
    })

    result = await extract_calendar_slots("Mon 9:00-10:00 OS Lec")

    assert model.tasks == ["calendar_parsing"]
    # Heavy structured generation (minute arithmetic, day offsets), and a wrong slot
    # becomes a hard block in the scheduler: the heavy model, not the fast one.
    assert model.calls[0].role == "primary"
    assert isinstance(result, ExtractedTimeSlots) and [s.name for s in result.slots] == ["OS Lec"]


@pytest.mark.asyncio
async def test_proactive_extraction__routes_through_the_chain__returns_items_and_topics(llm):
    from app.services.extraction.knowledge_ingester import _run_proactive_extraction

    model = llm(action_item_extraction={
        "action_items": ["Solve DPP 4"], "deadlines": ["2026-11-20"], "document_topics": ["Graphs"],
    })

    result = await _run_proactive_extraction("Daily Practice Problems, chapter 4: graphs. Due 20 November.")

    assert model.tasks == ["action_item_extraction"]
    assert (result.action_items, result.document_topics) == (["Solve DPP 4"], ["Graphs"])


@pytest.mark.asyncio
async def test_proactive_extraction__model_down__returns_empty_lists(llm):
    from app.services.extraction.knowledge_ingester import ProactiveExtractionSchema, _run_proactive_extraction

    llm(action_item_extraction=_down())

    result = await _run_proactive_extraction("Daily Practice Problems, chapter 4: graphs. Due 20 November.")

    assert result == ProactiveExtractionSchema()


@pytest.mark.asyncio
async def test_ingestion_classifier__routes_through_the_chain__returns_the_intent(llm):
    from app.schemas.context import IntentType
    from app.services.extraction.orchestrator import _classify

    model = llm(intent_classification={"intent": "ACTION_ITEM", "confidence": 0.8, "summary": "An application"})

    result = await _classify("Apply to the Acme internship by Nov 20")

    assert model.tasks == ["intent_classification"]
    assert model.calls[0].role == "fast"
    assert result.intent == IntentType.ACTION_ITEM


@pytest.mark.asyncio
async def test_ingest_endpoint__routes_through_the_chain__returns_the_classification(llm):
    from app.api.v1.endpoints.ingestion import IngestionRequest, ingest

    model = llm(intent_classification={"intent": "CALENDAR_SYNC", "confidence": 0.9, "summary": "Timetable"})

    result = await ingest(IngestionRequest(payload="Mon 9-10 OS Lec"))

    assert model.tasks == ["intent_classification"]
    assert result.summary == "Timetable"


@pytest.mark.asyncio
async def test_ingest_endpoint__model_down__answers_502(llm):
    from app.api.v1.endpoints.ingestion import IngestionRequest, ingest

    llm(intent_classification=_down())

    with pytest.raises(HTTPException) as caught:
        await ingest(IngestionRequest(payload="Mon 9-10 OS Lec"))
    assert caught.value.status_code == 502


# --- the reasoning endpoint ------------------------------------------------------------


def _graph(n: int) -> dict:
    return {
        "goal_metadata": {"objective": "Learn graphs"},
        "decomposition": [
            {"task_id": f"task_{i}", "title": f"Step {i}", "duration_minutes": 20,
             "difficulty_weight": 0.5, "completion_criteria": f"Explain step {i}"}
            for i in range(1, n + 1)
        ],
        "cognitive_load_estimate": {"intrinsic_load": 0.5},
    }


@pytest.fixture
def local_then_cloud(monkeypatch):
    """A chain of [local, cloud] scripted models, local tried first."""
    import contextvars

    monkeypatch.setattr(_cfg, "GEMINI_PRIMARY", False)
    monkeypatch.setattr(_cfg, "CLAUDE_PRIMARY", False)
    monkeypatch.setattr(
        "app.core.model_router.force_cloud_var", contextvars.ContextVar("fc", default=False)
    )

    def install(local_reply: Any, cloud_reply: Any) -> tuple[ScriptedModel, ScriptedModel]:
        local = ScriptedModel({"socratic_chunker": local_reply}, LLMLocation.LOCAL)
        cloud = ScriptedModel({"socratic_chunker": cloud_reply})
        set_chain(FallbackChainProvider([local, cloud]))
        return local, cloud

    return install


@pytest.mark.asyncio
async def test_decompose_goal__routes_through_the_chain__returns_the_graph(local_then_cloud):
    from app.api.v1.endpoints.reasoning import decompose_goal

    local, cloud = local_then_cloud(_graph(5), None)

    graph = await decompose_goal(user_prompt="Learn graphs")

    assert local.tasks == ["socratic_chunker"] and cloud.tasks == []
    assert local.calls[0].role == "primary"
    assert len(graph.decomposition) == 5


@pytest.mark.asyncio
async def test_decompose_goal__undersized_local_graph__retried_on_the_cloud(local_then_cloud):
    from app.api.v1.endpoints.reasoning import decompose_goal

    local, cloud = local_then_cloud(_graph(2), _graph(6))

    graph = await decompose_goal(user_prompt="Learn graphs")

    assert (local.tasks, cloud.tasks) == (["socratic_chunker"], ["socratic_chunker"])
    assert len(graph.decomposition) == 6


@pytest.mark.asyncio
async def test_decompose_goal__undersized_graph_and_no_cloud_model__answers_502(local_then_cloud):
    from app.api.v1.endpoints.reasoning import decompose_goal

    local, _ = local_then_cloud(_graph(2), None)
    set_chain(FallbackChainProvider([local]))  # no cloud provider configured

    with pytest.raises(HTTPException) as caught:
        await decompose_goal(user_prompt="Learn graphs")
    assert caught.value.status_code == 502
    assert "insufficient tasks" in caught.value.detail
    assert local.tasks == ["socratic_chunker"]


@pytest.mark.asyncio
async def test_decompose_goal__no_model_answers_parseably__answers_502(local_then_cloud):
    from app.api.v1.endpoints.reasoning import decompose_goal

    local_then_cloud("not json", "still not json")

    with pytest.raises(HTTPException) as caught:
        await decompose_goal(user_prompt="Learn graphs")
    assert caught.value.status_code == 502
    assert "could not be parsed" in caught.value.detail


# --- still Gemini-only: web search ------------------------------------------------------


@pytest.fixture
def web_search_down(monkeypatch):
    """Gemini's grounded search fails at the transport, as on a 402 or an outage."""
    import app.models.brain.litellm_conf as _llm

    async def failing_completion(**_kwargs):
        raise RuntimeError("HTTP 402: prepaid credits depleted")

    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(_llm.litellm, "acompletion", failing_completion)


@pytest.fixture
def offline_workspace(monkeypatch):
    """A workspace build with no database and no ingested materials."""
    import app.services.analytical.workspace_builder as wb

    async def no_chunks(*_args, **_kwargs):
        return []

    async def watcher(*_args, **_kwargs):
        return "watcher"

    monkeypatch.setattr(wb, "_get_supabase", lambda: None)
    monkeypatch.setattr(wb, "fetch_chunks_for_task", no_chunks)
    monkeypatch.setattr(wb, "get_learning_style", watcher)
    return wb


@pytest.mark.asyncio
async def test_workspace__web_search_fails__says_web_results_are_unavailable_and_shows_no_links(
    web_search_down, offline_workspace
):
    ws = await offline_workspace.build_task_workspace("alice", "graphs_task_1")

    assert ws.notices == [offline_workspace.WEB_RESULTS_UNAVAILABLE]
    assert "web results are unavailable" in ws.notices[0].lower()
    assert [a for a in ws.surfaced_assets if "http" in a.content_or_url] == []


@pytest.mark.asyncio
async def test_workspace__web_search_fails__degraded_workspace_is_not_cached(
    web_search_down, offline_workspace, monkeypatch
):
    written: list = []

    async def no_cache(*_args):
        return None

    async def record_write(*args):
        written.append(args)

    monkeypatch.setattr(offline_workspace, "_read_cached_workspace", no_cache)
    monkeypatch.setattr(offline_workspace, "_write_cached_workspace", record_write)

    ws = await offline_workspace.get_or_build_task_workspace("alice", "graphs_task_1")

    assert ws.notices and written == []


@pytest.mark.asyncio
async def test_workspace__web_search_answers__links_surface_without_a_notice(offline_workspace, monkeypatch):
    async def search(**_kwargs):
        return "https://www.youtube.com/watch?v=abc | Graphs in 10 minutes"

    monkeypatch.setattr(offline_workspace, "gemini_web_search", search)

    ws = await offline_workspace.build_task_workspace("alice", "graphs_task_1")

    assert ws.notices == []
    assert "https://www.youtube.com/watch?v=abc" in [a.content_or_url for a in ws.surfaced_assets]


@pytest.mark.asyncio
async def test_research__web_search_fails__result_says_web_results_are_unavailable(web_search_down):
    from app.modules.research_graph import execute_search, needs_more
    from app.services.analytical.workspace_builder import WEB_RESULTS_UNAVAILABLE

    state = {"query": "graph algorithms", "search_results": [], "iteration_count": 0, "max_iterations": 3}

    out = await execute_search(state)

    assert out["error"] == WEB_RESULTS_UNAVAILABLE
    assert "search_results" not in out  # nothing invented, nothing kept
    assert needs_more({**state, **out}) is False  # one honest failure, not three silent retries


@pytest.fixture
def web_search_recorded(monkeypatch):
    """Gemini's grounded search answers one link, and every request it got is kept."""
    import app.models.brain.litellm_conf as _llm

    sent: list[dict] = []

    class _Message:
        content = "https://docs.example.org/graphs | Graphs, explained"

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    async def recording_completion(**kwargs):
        sent.append(kwargs)
        return _Response()

    monkeypatch.setattr(_cfg, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(_llm.litellm, "acompletion", recording_completion)
    return sent


@pytest.mark.asyncio
async def test_web_search__email_in_the_query__redacted_before_it_leaves(web_search_recorded):
    from app.services.analytical.workspace_builder import perform_learning_style_search

    assets = await perform_learning_style_search(
        "graphs, and mail the notes to madhav@example.com", "reader"
    )

    (request,) = web_search_recorded
    sent_text = json.dumps(request["messages"])
    assert "madhav@example.com" not in sent_text
    assert "[EMAIL]" in sent_text
    assert [a.content_or_url for a in assets] == ["https://docs.example.org/graphs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["deny", "ask"])
async def test_web_search__privacy_gate_refuses__no_search_and_says_results_are_unavailable(
    web_search_recorded, monkeypatch, decision
):
    import app.services.analytical.workspace_builder as wb
    from app.orchestrator.hooks import ActionHooks, HookDecision, HookResult

    async def refuse(prompt: str, **_kwargs) -> HookResult:
        return HookResult(decision=HookDecision(decision), reason="needs consent")

    hooks = ActionHooks()
    hooks.register("PreCloudLLM", refuse)
    monkeypatch.setattr(wb, "get_hooks", lambda: hooks)

    with pytest.raises(wb.WebSearchUnavailableError) as raised:
        await wb.perform_learning_style_search("graphs for madhav@example.com", "reader")

    assert raised.value.message == wb.WEB_RESULTS_UNAVAILABLE
    assert web_search_recorded == []  # nothing left the machine


@pytest.mark.asyncio
async def test_research__user_message_with_an_email__redacted_before_the_search(web_search_recorded):
    from app.modules.research_graph import execute_search

    out = await execute_search(
        {"query": "graph algorithms; reply to madhav@example.com", "search_results": [], "iteration_count": 0}
    )

    assert out["search_results"]
    assert "madhav@example.com" not in json.dumps(web_search_recorded)

