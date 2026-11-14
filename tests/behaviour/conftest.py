"""Harness for the behaviour suites.

Each suite talks to Jarvis the way a person does — a message into the real
``POST /chat/v2/stream`` handler, the reply read back off its SSE stream — and
checks what that person would see: the reply, the draft, the rows in their task
list, what Jarvis remembers next time.

Only the boundaries are faked:

* the database is ``FakeSupabase`` (real filtering, so user isolation has teeth);
* every language model is one ``ScriptedLLM``, installed as the process-wide
  provider chain with ``set_chain`` — every LLM call reaches it through
  ``route_llm_call``. An LLM call the test did not script is recorded and fails
  the test;
* ChromaDB returns no documents, and text embeddings are a deterministic
  bag-of-words (the real model would be downloaded, which the socket guard forbids).

Everything else is real: the LangGraph orchestrator and its SQLite checkpointer,
the planning sub-graph, CP-SAT, Pydantic parsing, the memory store and its decay,
PEARL, the constraint bridge and the draft service.
"""

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel

from app.domain.anti_guilt import BLAME_PHRASES, blame_phrases_in  # noqa: F401  (re-export)
from app.domain.entities import LLMLocation
from app.domain.errors import LLMUnavailableError
from app.domain.ports import LLMProvider, LLMRequest
from app.infrastructure.llm.chain import FallbackChainProvider
from app.infrastructure.llm.factory import set_chain
from app.infrastructure.llm.transport import strip_fences
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase, bag_of_words_embedding

# --- the scripted language model ---------------------------------------------

Reply = str | dict | BaseModel | Callable[[LLMRequest], Any]


class ScriptedLLM(LLMProvider):
    """Answers each LLM task from a script and records every call, in order.

    ``calls`` holds one ``LLMRequest`` per call, in the order the provider chain
    (``route_llm_call``) made them. Structured replies are parsed into the request's
    schema with real Pydantic, from JSON text the way a provider parses a model's
    output.
    """

    name = "scripted"
    location = LLMLocation.CLOUD

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []
        self.unscripted: list[str] = []
        self._replies: dict[str, Callable[[LLMRequest], Any]] = {}
        # A turn with nothing to say still reaches these; their defaults are dull on purpose.
        # No separate memory extraction: memories come back from brain_dump_extraction,
        # so a memory_extraction call is unscripted and fails the test.
        self.script("brain_dump_extraction", "{}")
        self.script("voice_of_jarvis", "Very good, sir.")
        self.script("voice_of_jarvis_synthesis", "Done, sir.")

    def script(self, task: str, reply: Reply) -> None:
        """Answer every later call for ``task`` with ``reply`` (or ``reply(request)``)."""
        self._replies[task] = reply if callable(reply) else (lambda _req, value=reply: value)

    def tasks(self, since: int = 0) -> list[str]:
        """The task names of the calls made so far, from call number ``since`` on."""
        return [c.task for c in self.calls[since:]]

    def calls_for(self, task: str) -> list[LLMRequest]:
        return [c for c in self.calls if c.task == task]

    def answer(self, req: LLMRequest) -> Any:
        self.calls.append(req)
        reply = self._replies.get(req.task)
        if reply is None:
            self.unscripted.append(req.task)
            raise LLMUnavailableError(f"ScriptedLLM has no reply for task {req.task!r}")
        out = reply(req)
        schema = req.response_schema
        if schema is None or isinstance(out, schema):
            return out
        if isinstance(out, dict):
            return schema.model_validate(out)
        return schema.model_validate_json(strip_fences(str(out)))

    # LLMProvider
    def is_available(self) -> bool:
        return True

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        return self.answer(req)

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        for token in _tokens(str(self.answer(req))):
            yield ("content", token)

    def should_retry(self, error: Exception, attempt: int) -> bool:
        return False

    def delay(self, attempt: int) -> float:
        return 0.0


def _tokens(text: str) -> list[str]:
    """Split a reply into small streamed chunks, mid-word on purpose."""
    return [text[i : i + 5] for i in range(0, len(text), 5)] or [""]


@pytest.fixture
def embeddings(monkeypatch) -> None:
    """Memory similarity uses the deterministic bag-of-words embedder."""
    monkeypatch.setattr("app.services.memory.store.embed_text", bag_of_words_embedding)


@pytest.fixture
def llm(monkeypatch, embeddings) -> ScriptedLLM:
    """The only language model the test can reach. Fails the test on any unscripted call."""
    scripted = ScriptedLLM()
    set_chain(FallbackChainProvider([scripted]))  # conftest's _fresh_llm_chain resets it
    monkeypatch.setattr("app.utils.chroma_client.query_knowledge", lambda *a, **k: [])

    yield scripted

    assert not scripted.unscripted, f"unscripted LLM calls: {scripted.unscripted}"


# --- realistic model output ----------------------------------------------------


def brain_dump(**fields: Any) -> str:
    """A ``BrainDumpExtraction`` as the model returns it: JSON in a markdown fence."""
    return f"```json\n{json.dumps(fields)}\n```"


def said(req: LLMRequest) -> str:
    """What the user said, out of a brain_dump_extraction prompt.

    The prompt is the message itself, or, when the user has memories, the known
    memories (``[m1] (type) content`` lines) followed by ``Message:`` and the message.
    """
    known, header, message = req.prompt.partition("\n\nMessage:\n")
    return message if header else known


def decomposition(
    titles: list[str],
    *,
    duration: int = 25,
    difficulty: float = 0.5,
    deadline: str | list[str | None] | None = None,
    intrinsic_load: float = 0.5,
    with_intentions: bool = True,
    chained: bool = False,
    objective: str = "",
) -> str:
    """An ``ExecutionGraph`` as the socratic chunker returns it, positional ids and all.

    ``deadline`` is one deadline hint for every task, or a list with one per task.
    """
    deadlines = deadline if isinstance(deadline, list) else [deadline] * len(titles)
    tasks = []
    for i, (title, task_deadline) in enumerate(zip(titles, deadlines, strict=True), start=1):
        task: dict[str, Any] = {
            "task_id": f"task_{i}",
            "title": title,
            "duration_minutes": duration,
            "difficulty_weight": difficulty,
            "dependencies": [f"task_{i - 1}"] if chained and i > 1 else [],
            "completion_criteria": f"Explain {title.lower()} from memory in two minutes",
            "deadline_hint": task_deadline,
        }
        if with_intentions:
            task["implementation_intention"] = {
                "obstacle_trigger": f"If I stall on {title.lower()}",
                "behavioral_response": "then I write down the one step I am stuck on and do it",
            }
        tasks.append(task)
    graph = {
        "goal_metadata": {"objective": objective},
        "decomposition": tasks,
        "cognitive_load_estimate": {"intrinsic_load": intrinsic_load},
    }
    return json.dumps(graph)


# --- talking to Jarvis --------------------------------------------------------


@dataclass
class Turn:
    """One exchange, as the person (and their browser) saw it."""

    reply: str
    payload: dict
    events: list[tuple[str, Any]] = field(default_factory=list)

    @property
    def intent(self) -> str | None:
        return self.payload.get("intent")

    @property
    def draft_id(self) -> str | None:
        return self.payload.get("draft_id")

    @property
    def conversation_id(self) -> str | None:
        return self.payload.get("conversation_id")


def _parse_sse(raw: str) -> list[tuple[str, Any]]:
    frames = []
    for block in raw.split("\n\n"):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if event is None:
            continue
        try:
            frames.append((event, json.loads(data) if data else None))
        except json.JSONDecodeError:
            frames.append((event, data))
    return frames


async def settle_background_tasks(timeout: float = 10.0) -> None:
    """Wait for every task a turn left running to finish."""
    current = asyncio.current_task()
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        remaining = deadline - asyncio.get_running_loop().time()
        if not pending or remaining <= 0:
            return
        await asyncio.wait(pending, timeout=remaining)


class Jarvis:
    """The app as one user's browser sees it: the real v2 chat handler over fakes."""

    def __init__(self, graph: Any, supabase: FakeSupabase) -> None:
        from app.services.draft_store import DraftStore
        from app.services.memory.store import MemoryStore

        self.db = supabase
        self.memory_store = MemoryStore(supabase_client=supabase)
        self.draft_store = DraftStore(supabase_client=supabase)
        self.state = SimpleNamespace(
            jarvis_graph=graph,
            db_client=FakeDBClient(supabase),
            memory_store=self.memory_store,
            draft_store=self.draft_store,
        )

    async def say(self, text: str, *, user: str = "alice", session: str | None = None) -> Turn:
        """Send one message and read the whole SSE reply, then let background work finish."""
        from app.api.v1.endpoints.chat import ChatRequest, chat_stream_v2

        request = SimpleNamespace(app=SimpleNamespace(state=self.state))
        response = await chat_stream_v2(
            ChatRequest(user_prompt=text, user_id=user, conversation_id=session), request
        )
        raw = ""
        async for chunk in response.body_iterator:
            raw += chunk if isinstance(chunk, str) else chunk.decode()
        await response.background()
        await settle_background_tasks()

        events = _parse_sse(raw)
        errors = [data for event, data in events if event == "error"]
        assert not errors, f"the turn failed: {errors}"
        payload = next(data for event, data in events if event == "complete")
        return Turn(reply=payload["message"], payload=payload, events=events)

    def rows(self, table: str, **where: Any) -> list[dict]:
        """Copies of the matching rows: a snapshot, not a live view of the table."""
        return [
            copy.deepcopy(r) for r in self.db.rows.get(table, [])
            if all(r.get(k) == v for k, v in where.items())
        ]

    def draft(self, draft_id: str) -> dict:
        (row,) = self.rows("draft_schedules", id=draft_id)
        return row


@pytest_asyncio.fixture
async def jarvis(tmp_path) -> Jarvis:
    """A fresh Jarvis: empty database, empty checkpoint file, real orchestrator."""
    import app.core.config as config
    from app.container import AppContainer
    from app.core.runtime import set_container, set_shared_clients
    from app.orchestrator.checkpoint import open_checkpointer
    from app.orchestrator.graph import build_jarvis_graph
    from app.orchestrator.hooks import get_hooks
    # Like the lifespan, no legacy PEARL detectors: the composition root is built below.
    supabase = FakeSupabase()
    async with open_checkpointer(str(tmp_path / "checkpoints.sqlite")) as saver:
        app = Jarvis(build_jarvis_graph(checkpointer=saver), supabase)
        # What the lifespan registers, for turns resumed from a checkpoint.
        set_shared_clients(db=app.state.db_client, memory_store=app.memory_store)
        # The composition root, over the same database. The chain stays the test's:
        # the ``llm`` fixture installs the scripted model.
        container = AppContainer.build(
            supabase_client=supabase,
            cfg=config,
            hooks=get_hooks(),
            embedder=BagOfWordsEmbedder(),
            install_chain=False,
        )
        set_container(container)
        app.state.container = container
        yield app
        await settle_background_tasks()


# --- anti-guilt ----------------------------------------------------------------
# BLAME_PHRASES and blame_phrases_in come from app.domain.anti_guilt (imported above):
# the one list, shared with the production code that vets model-written text.
