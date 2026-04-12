# Jarvis Architecture v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-rolled `execute_agentic_flow()` orchestrator with a LangGraph StateGraph, add User Model facade, Observation Loop, 5 cognitive modules, action hooks, and Gemma 4 model routing — wrapping existing code, not rewriting it.

**Architecture:** Cognitive Architecture with User Model at center. LangGraph StateGraph orchestrates 5 modules (Planning, Research, Coach, Knowledge, Conversation). Observation Loop runs post-turn for PEARL behavioral inference. Action hooks provide consent gates and PII filtering. See `docs/superpowers/specs/2026-04-12-jarvis-architecture-v2-design.md`.

**Tech Stack:** Python 3.11+, FastAPI, LangGraph, langchain-core, Supabase, OR-Tools, ChromaDB, Gemma 4 (26B A4B + E4B via LM Studio), Gemini 2.5 Flash (cloud fallback)

---

## File Structure

### New files to create

```
app/
├── orchestrator/
│   ├── __init__.py
│   ├── state.py          # JarvisState TypedDict, ConversationPhase, NegotiationPhase, enums
│   ├── graph.py           # Main LangGraph StateGraph — the orchestrator
│   ├── routing.py         # route_to_module(), check_negotiation_shortcut, check_needs_followup
│   └── hooks.py           # ActionHooks class, HookDecision, HookResult, 7 hook handlers
├── modules/
│   ├── __init__.py
│   ├── planning_graph.py  # Planning sub-graph (wraps existing functions)
│   ├── research_graph.py  # Research agent sub-graph
│   ├── knowledge_graph.py # Knowledge module sub-graph
│   ├── coach.py           # Coach module function
│   └── conversation.py    # Conversation module + synthesize_response
├── core/
│   ├── user_model.py      # UserModel lazy facade class
│   ├── model_router.py    # route_llm_call() — task-based model routing
│   └── observation.py     # Observation Loop (memory extract + PEARL + bridge)
tests/
├── test_user_model.py
├── test_orchestrator.py
├── test_planning_graph.py
├── test_hooks.py
├── test_model_router.py
├── test_observation.py
└── test_modules.py
```

### Existing files to modify

```
app/main.py                        # Add LangGraph initialization in lifespan
app/api/v1/endpoints/chat.py       # SSE generator calls jarvis_graph.astream()
app/schemas/context.py             # Add RESEARCH, CHECK_PROGRESS, EDIT_TASK to IntentType
app/core/config.py                 # Add Gemma 4 model constants
```

---

## Layer 1: User Model (Tasks 1-3)

### Task 1: State Types and Enums

**Files:**
- Create: `app/orchestrator/__init__.py`
- Create: `app/orchestrator/state.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Create orchestrator package**

```bash
mkdir -p app/orchestrator app/modules
touch app/orchestrator/__init__.py app/modules/__init__.py
```

- [ ] **Step 2: Write the failing test for state types**

```python
# tests/test_orchestrator.py
from app.orchestrator.state import (
    ConversationPhase,
    NegotiationPhase,
    JarvisState,
)


def test_conversation_phase_values():
    assert ConversationPhase.GREETING == "greeting"
    assert ConversationPhase.PLANNING == "planning"
    assert ConversationPhase.NEGOTIATION == "negotiation"
    assert ConversationPhase.REVIEW == "review"
    assert ConversationPhase.CHAT == "chat"


def test_negotiation_phase_values():
    assert NegotiationPhase.NONE == "none"
    assert NegotiationPhase.PROPOSED == "proposed"
    assert NegotiationPhase.REVIEWING == "reviewing"
    assert NegotiationPhase.EDITING == "editing"
    assert NegotiationPhase.ACCEPTED == "accepted"


def test_jarvis_state_is_typed_dict():
    """JarvisState should be a TypedDict usable by LangGraph."""
    state: JarvisState = {
        "user_model": None,
        "user_message": "plan my day",
        "brain_dump": None,
        "intent": None,
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "response_message": None,
        "conversation_phase": ConversationPhase.GREETING,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": [],
        "needs_followup": False,
        "error": None,
    }
    assert state["user_message"] == "plan my day"
    assert state["conversation_phase"] == ConversationPhase.GREETING
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.orchestrator.state'`

- [ ] **Step 4: Implement state types**

```python
# app/orchestrator/state.py
"""Jarvis orchestrator state types for LangGraph."""

from enum import Enum
from typing import Any, Optional, TypedDict

from app.schemas.context import (
    BrainDumpExtraction,
    ExecutionGraph,
    IntentType,
)


class ConversationPhase(str, Enum):
    GREETING = "greeting"
    PLANNING = "planning"
    NEGOTIATION = "negotiation"
    REVIEW = "review"
    CHAT = "chat"


class NegotiationPhase(str, Enum):
    NONE = "none"
    PROPOSED = "proposed"
    REVIEWING = "reviewing"
    EDITING = "editing"
    ACCEPTED = "accepted"


class JarvisState(TypedDict):
    """State that flows through the LangGraph orchestrator.

    Working Memory tier — exists only per-turn, in-memory.
    """

    # User Model (lazy facade, loaded once per session)
    user_model: Any  # UserModel — Any to avoid circular import

    # Current turn
    user_message: str
    brain_dump: Optional[BrainDumpExtraction]
    intent: Optional[IntentType]
    initiated_by: str  # "user" | "system" | "pearl"

    # Module outputs
    execution_graph: Optional[ExecutionGraph]
    schedule: Optional[dict]
    draft_response: Optional[dict]
    research_results: Optional[list[dict]]
    ingestion_result: Optional[dict]
    clarification_request: Optional[str]

    # Response
    thinking_process: Optional[str]
    response_message: Optional[str]

    # Orchestrator control
    conversation_phase: ConversationPhase
    negotiation_state: NegotiationPhase
    modules_invoked: list[str]
    needs_followup: bool
    error: Optional[str]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add app/orchestrator/ app/modules/ tests/test_orchestrator.py
git commit -m "feat: add orchestrator state types (JarvisState, ConversationPhase, NegotiationPhase)"
```

---

### Task 2: UserModel Lazy Facade

**Files:**
- Create: `app/core/user_model.py`
- Test: `tests/test_user_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_model.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.user_model import UserModel


@pytest.fixture
def mock_db():
    db = MagicMock()
    # Chain: db.supabase.table("x").select("*").eq("user_id", "u1").execute()
    table_mock = MagicMock()
    select_mock = MagicMock()
    eq_mock = MagicMock()
    execute_mock = MagicMock()
    execute_mock.data = [{"id": "c1", "raw_text": "no work before 11am", "constraint_type": "preference"}]

    db.supabase.table.return_value = table_mock
    table_mock.select.return_value = select_mock
    select_mock.eq.return_value = eq_mock
    eq_mock.execute.return_value = execute_mock
    return db


@pytest.mark.asyncio
async def test_get_behavioral_constraints_lazy_loads(mock_db):
    um = UserModel(user_id="u1", db=mock_db)
    constraints = await um.get_behavioral_constraints()
    assert len(constraints) == 1
    assert constraints[0]["raw_text"] == "no work before 11am"
    # Second call should use cache, not query again
    mock_db.supabase.table.reset_mock()
    constraints2 = await um.get_behavioral_constraints()
    assert constraints2 == constraints
    mock_db.supabase.table.assert_not_called()


@pytest.mark.asyncio
async def test_invalidate_clears_cache(mock_db):
    um = UserModel(user_id="u1", db=mock_db)
    await um.get_behavioral_constraints()
    um.invalidate("constraints")
    # After invalidation, next call should query again
    await um.get_behavioral_constraints()
    assert mock_db.supabase.table.call_count == 2


@pytest.mark.asyncio
async def test_get_estimated_energy_returns_float(mock_db):
    um = UserModel(user_id="u1", db=mock_db)
    energy = await um.get_estimated_energy()
    assert isinstance(energy, float)
    assert 0.0 <= energy <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_user_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.user_model'`

- [ ] **Step 3: Implement UserModel**

```python
# app/core/user_model.py
"""UserModel — lazy facade over Supabase tables.

The Soul of Jarvis. Every module reads from and writes to this.
Queries on first access, caches per-session, invalidates on writes.
"""

import asyncio
from datetime import datetime
from typing import Any, Optional

from app.core.config import SUPABASE_URL, SUPABASE_SERVICE_KEY


class UserModel:
    """Lazy facade over Supabase tables. Queries on first access, caches."""

    def __init__(self, user_id: str, db: Any) -> None:
        self._user_id = user_id
        self._db = db
        self._cache: dict[str, Any] = {}

    @property
    def user_id(self) -> str:
        return self._user_id

    # ── Recall Memory (SM-2 scored memories) ──

    async def get_memory_store(self) -> Any:
        """Return the MemoryStore handle (from app.state, passed through)."""
        return self._cache.get("memory_store")

    def set_memory_store(self, store: Any) -> None:
        """Set the MemoryStore handle (injected from app.state at session start)."""
        self._cache["memory_store"] = store

    # ── Archival Memory (ChromaDB) ──

    async def get_semantic_store(self) -> Any:
        """Return the ChromaDB handle (from app.state, passed through)."""
        return self._cache.get("semantic_store")

    def set_semantic_store(self, store: Any) -> None:
        self._cache["semantic_store"] = store

    # ── Behavioral Constraints ──

    async def get_behavioral_constraints(self) -> list[dict]:
        """From behavioral_constraints table. Lazy-loaded, cached."""
        if "constraints" not in self._cache:
            result = await asyncio.to_thread(
                lambda: self._db.supabase.table("behavioral_constraints")
                .select("*")
                .eq("user_id", self._user_id)
                .execute()
            )
            self._cache["constraints"] = result.data
        return self._cache["constraints"]

    # ── Active State ──

    async def get_pending_tasks(self) -> list[dict]:
        """Pending tasks from user_tasks table."""
        if "pending_tasks" not in self._cache:
            result = await asyncio.to_thread(
                lambda: self._db.supabase.table("user_tasks")
                .select("*")
                .eq("user_id", self._user_id)
                .eq("status", "pending")
                .execute()
            )
            self._cache["pending_tasks"] = result.data
        return self._cache["pending_tasks"]

    async def get_active_goals(self) -> list[dict]:
        """Active goals from user_plan_updates table."""
        if "active_goals" not in self._cache:
            result = await asyncio.to_thread(
                lambda: self._db.supabase.table("user_plan_updates")
                .select("*")
                .eq("user_id", self._user_id)
                .execute()
            )
            self._cache["active_goals"] = result.data
        return self._cache["active_goals"]

    async def get_active_draft(self) -> Optional[dict]:
        """Active draft schedule from draft_store."""
        return self._cache.get("active_draft")

    def set_active_draft(self, draft: Optional[dict]) -> None:
        self._cache["active_draft"] = draft

    # ── Behavioral Profile (PEARL) ──

    async def get_pearl_patterns(self) -> list[dict]:
        """PEARL-detected behavioral patterns."""
        if "pearl_patterns" not in self._cache:
            self._cache["pearl_patterns"] = []
        return self._cache["pearl_patterns"]

    def set_pearl_patterns(self, patterns: list[dict]) -> None:
        self._cache["pearl_patterns"] = patterns

    # ── Cognitive State ──

    async def get_estimated_energy(self) -> float:
        """Circadian energy estimate based on time of day.

        Simple heuristic — promoted from pacing.py inline logic.
        Future: SARIMAX model replaces this.
        """
        hour = datetime.now().hour
        # Circadian curve: peak at 10-12 and 15-17, low at 13-14 and after 21
        if 9 <= hour <= 12:
            return 0.9
        elif 15 <= hour <= 17:
            return 0.85
        elif 13 <= hour <= 14:
            return 0.5  # post-lunch dip
        elif 7 <= hour <= 9:
            return 0.7  # warming up
        elif 17 < hour <= 21:
            return 0.6  # winding down
        else:
            return 0.3  # late night / early morning

    # ── Cache Management ──

    def invalidate(self, key: str) -> None:
        """Called after a module writes to this data."""
        self._cache.pop(key, None)

    async def upsert_behavioral_constraint(self, constraint: dict) -> None:
        """Write a constraint and invalidate cache."""
        await asyncio.to_thread(
            lambda: self._db.supabase.table("behavioral_constraints")
            .upsert(constraint)
            .execute()
        )
        self.invalidate("constraints")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_user_model.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/core/user_model.py tests/test_user_model.py
git commit -m "feat: add UserModel lazy facade over Supabase tables"
```

---

### Task 3: Extend IntentType Enum

**Files:**
- Modify: `app/schemas/context.py`
- Test: `tests/test_orchestrator.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py`:

```python
from app.schemas.context import IntentType


def test_new_intent_types_exist():
    assert IntentType.EDIT_TASK == "EDIT_TASK"
    assert IntentType.REARRANGE == "REARRANGE"
    assert IntentType.ACCEPT_DRAFT == "ACCEPT_DRAFT"
    assert IntentType.REJECT_DRAFT == "REJECT_DRAFT"
    assert IntentType.ADD_CONSTRAINT == "ADD_CONSTRAINT"
    assert IntentType.CHECK_PROGRESS == "CHECK_PROGRESS"
    assert IntentType.RESEARCH == "RESEARCH"
    assert IntentType.CHAT == "CHAT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py::test_new_intent_types_exist -v`
Expected: FAIL — some enum values don't exist yet

- [ ] **Step 3: Add missing intent types to IntentType enum**

In `app/schemas/context.py`, find the `IntentType` enum and add any missing values:

```python
class IntentType(str, Enum):
    GREETING = "GREETING"
    GENERAL_QA = "GENERAL_QA"
    CALENDAR_SYNC = "CALENDAR_SYNC"
    KNOWLEDGE_INGESTION = "KNOWLEDGE_INGESTION"
    BEHAVIORAL_CONSTRAINT = "BEHAVIORAL_CONSTRAINT"
    ACTION_ITEM = "ACTION_ITEM"
    PLAN_DAY = "PLAN_DAY"
    # New intents for v2 orchestrator
    EDIT_TASK = "EDIT_TASK"
    REARRANGE = "REARRANGE"
    ACCEPT_DRAFT = "ACCEPT_DRAFT"
    REJECT_DRAFT = "REJECT_DRAFT"
    ADD_CONSTRAINT = "ADD_CONSTRAINT"
    CHECK_PROGRESS = "CHECK_PROGRESS"
    RESEARCH = "RESEARCH"
    CHAT = "CHAT"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/schemas/context.py tests/test_orchestrator.py
git commit -m "feat: add EDIT_TASK, REARRANGE, ACCEPT_DRAFT, REJECT_DRAFT, ADD_CONSTRAINT, CHECK_PROGRESS, RESEARCH, CHAT to IntentType"
```

---

## Layer 2: LangGraph Orchestrator + Hooks (Tasks 4-8)

### Task 4: Install LangGraph + Action Hooks Class

**Files:**
- Modify: `pyproject.toml` or `requirements.txt`
- Create: `app/orchestrator/hooks.py`
- Test: `tests/test_hooks.py`

- [ ] **Step 1: Install dependencies**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
pip install langgraph langchain-core
```

- [ ] **Step 2: Add to requirements.txt**

Add these lines to `requirements.txt`:

```
langgraph>=0.4.0
langchain-core>=0.3.0
```

- [ ] **Step 3: Write the failing test for ActionHooks**

```python
# tests/test_hooks.py
import pytest

from app.orchestrator.hooks import ActionHooks, HookDecision, HookResult


@pytest.mark.asyncio
async def test_hooks_allow_by_default():
    hooks = ActionHooks()
    result = await hooks.execute("PreModuleExecution", module="planning")
    assert result.decision == HookDecision.ALLOW


@pytest.mark.asyncio
async def test_hooks_deny_stops_execution():
    hooks = ActionHooks()

    async def deny_handler(**ctx):
        return HookResult(decision=HookDecision.DENY, reason="blocked")

    hooks.register("PreModuleExecution", deny_handler)
    result = await hooks.execute("PreModuleExecution", module="planning")
    assert result.decision == HookDecision.DENY
    assert result.reason == "blocked"


@pytest.mark.asyncio
async def test_hooks_ask_stops_execution():
    hooks = ActionHooks()

    async def ask_handler(**ctx):
        return HookResult(decision=HookDecision.ASK, reason="need consent")

    hooks.register("PreModuleExecution", ask_handler)
    result = await hooks.execute("PreModuleExecution", module="planning")
    assert result.decision == HookDecision.ASK


@pytest.mark.asyncio
async def test_hooks_modify_returns_modified_input():
    hooks = ActionHooks()

    async def pii_handler(**ctx):
        return HookResult(
            decision=HookDecision.MODIFY,
            modified_input={"prompt": "REDACTED"},
        )

    hooks.register("PreCloudLLM", pii_handler)
    result = await hooks.execute("PreCloudLLM", prompt="my name is John")
    assert result.decision == HookDecision.MODIFY
    assert result.modified_input["prompt"] == "REDACTED"


@pytest.mark.asyncio
async def test_hooks_first_deny_wins():
    hooks = ActionHooks()

    async def allow_handler(**ctx):
        return HookResult(decision=HookDecision.ALLOW)

    async def deny_handler(**ctx):
        return HookResult(decision=HookDecision.DENY, reason="denied")

    hooks.register("PreModuleExecution", allow_handler)
    hooks.register("PreModuleExecution", deny_handler)
    result = await hooks.execute("PreModuleExecution", module="planning")
    # allow passes through, deny wins
    assert result.decision == HookDecision.DENY


@pytest.mark.asyncio
async def test_pii_filter_hook_strips_email():
    from app.orchestrator.hooks import pii_filter_hook

    result = await pii_filter_hook(prompt="Contact john@example.com for details")
    assert result.decision == HookDecision.MODIFY
    assert "john@example.com" not in result.modified_input["prompt"]
    assert "[EMAIL]" in result.modified_input["prompt"]
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_hooks.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 5: Implement ActionHooks + PII filter**

```python
# app/orchestrator/hooks.py
"""Action hooks — synchronous blocking gates for consent, PII, cost.

7 hook events total. This file creates the ActionHooks class and the
PreCloudLLM (PII filter) handler. Remaining 6 handlers added in Layer 5.
"""

import re
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


class HookDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    MODIFY = "modify"


@dataclass
class HookResult:
    decision: HookDecision
    modified_input: Optional[dict] = None
    reason: Optional[str] = None


HookHandler = Callable[..., Awaitable[HookResult]]


class ActionHooks:
    """Lightweight hook pipeline. 7 events, simple registry.

    Events: PreModuleExecution, PostModuleExecution, PreScheduleModify,
    PreCloudLLM, PreMemoryWrite, CostThreshold, ProactiveSuggestion
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[HookHandler]] = {}

    def register(self, event: str, handler: HookHandler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def execute(self, event: str, **context: Any) -> HookResult:
        """Run all handlers for an event. First DENY or ASK wins."""
        for handler in self._handlers.get(event, []):
            result = await handler(**context)
            if result.decision in (HookDecision.DENY, HookDecision.ASK, HookDecision.MODIFY):
                return result
        return HookResult(decision=HookDecision.ALLOW)


# ── PreCloudLLM: PII Filter (Layer 2 — needed by model router) ──

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")


async def pii_filter_hook(prompt: str, **kwargs: Any) -> HookResult:
    """Strip PII before sending to cloud LLM. Phase 1: regex-based."""
    filtered = prompt
    modified = False

    email_matches = _EMAIL_RE.findall(filtered)
    if email_matches:
        for email in email_matches:
            filtered = filtered.replace(email, "[EMAIL]")
        modified = True

    phone_matches = _PHONE_RE.findall(filtered)
    if phone_matches:
        for phone in phone_matches:
            filtered = filtered.replace(phone, "[PHONE]")
        modified = True

    if modified:
        return HookResult(
            decision=HookDecision.MODIFY,
            modified_input={"prompt": filtered},
            reason=f"Stripped {len(email_matches)} emails, {len(phone_matches)} phones",
        )
    return HookResult(decision=HookDecision.ALLOW)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_hooks.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Commit**

```bash
git add requirements.txt app/orchestrator/hooks.py tests/test_hooks.py
git commit -m "feat: add ActionHooks class with PII filter hook, install langgraph"
```

---

### Task 5: Orchestrator Routing Functions

**Files:**
- Create: `app/orchestrator/routing.py`
- Test: `tests/test_orchestrator.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py`:

```python
from app.orchestrator.routing import (
    route_to_module,
    check_negotiation_shortcut,
    check_needs_followup,
    INTENT_TO_MODULE,
)
from app.orchestrator.state import ConversationPhase, NegotiationPhase, JarvisState


def _make_state(**overrides) -> JarvisState:
    base: JarvisState = {
        "user_model": None,
        "user_message": "test",
        "brain_dump": None,
        "intent": "PLAN_DAY",
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "response_message": None,
        "conversation_phase": ConversationPhase.CHAT,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": [],
        "needs_followup": False,
        "error": None,
    }
    base.update(overrides)
    return base


def test_route_plan_day():
    state = _make_state(intent="PLAN_DAY")
    assert route_to_module(state) == "planning_module"


def test_route_chat_fallback():
    state = _make_state(intent="UNKNOWN_INTENT")
    assert route_to_module(state) == "conversation_module"


def test_route_negotiation_overrides_intent():
    state = _make_state(
        intent="CHAT",
        conversation_phase=ConversationPhase.NEGOTIATION,
    )
    assert route_to_module(state) == "planning_module"


def test_route_infeasible_fallback_to_coach():
    state = _make_state(
        intent="PLAN_DAY",
        modules_invoked=["planning_module"],
        error="INFEASIBLE",
    )
    assert route_to_module(state) == "coach_module"


def test_negotiation_shortcut_active():
    state = _make_state(negotiation_state=NegotiationPhase.REVIEWING)
    assert check_negotiation_shortcut(state) == "negotiation_active"


def test_negotiation_shortcut_normal():
    state = _make_state(negotiation_state=NegotiationPhase.NONE)
    assert check_negotiation_shortcut(state) == "normal"


def test_needs_followup_false():
    state = _make_state(needs_followup=False)
    assert check_needs_followup(state) is False


def test_needs_followup_true():
    state = _make_state(needs_followup=True)
    assert check_needs_followup(state) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py -v -k "route or negotiation or followup"`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement routing functions**

```python
# app/orchestrator/routing.py
"""State-aware routing for the Jarvis orchestrator.

Not a dumb router — understands conversation phase, negotiation state,
and module history to make intelligent routing decisions.
"""

from app.orchestrator.state import ConversationPhase, JarvisState, NegotiationPhase

INTENT_TO_MODULE: dict[str, str] = {
    "PLAN_DAY": "planning_module",
    "EDIT_TASK": "planning_module",
    "REARRANGE": "planning_module",
    "ACCEPT_DRAFT": "planning_module",
    "REJECT_DRAFT": "planning_module",
    "ADD_CONSTRAINT": "planning_module",
    "INGEST_DOCUMENT": "knowledge_module",
    "CALENDAR_SYNC": "knowledge_module",
    "KNOWLEDGE_INGESTION": "knowledge_module",
    "CHECK_PROGRESS": "coach_module",
    "RESEARCH": "research_agent",
    "CHAT": "conversation_module",
    "GREETING": "conversation_module",
    "GENERAL_QA": "conversation_module",
    "BEHAVIORAL_CONSTRAINT": "planning_module",
    "ACTION_ITEM": "knowledge_module",
}


def route_to_module(state: JarvisState) -> str:
    """State-aware intent routing."""
    intent = state.get("intent", "CHAT")
    phase = state.get("conversation_phase", ConversationPhase.CHAT)
    invoked = state.get("modules_invoked", [])
    error = state.get("error")

    # Negotiation overrides intent classification
    if phase == ConversationPhase.NEGOTIATION:
        return "planning_module"

    # Anti-guilt: if planning failed, route to coach
    if "planning_module" in invoked and error:
        return "coach_module"

    return INTENT_TO_MODULE.get(intent, "conversation_module")


def check_negotiation_shortcut(state: JarvisState) -> str:
    """Skip extraction + classification if negotiation is active."""
    neg = state.get("negotiation_state", NegotiationPhase.NONE)
    if neg not in (NegotiationPhase.NONE, NegotiationPhase.ACCEPTED):
        return "negotiation_active"
    return "normal"


def check_needs_followup(state: JarvisState) -> bool:
    """Check if the orchestrator should loop back for more processing."""
    return state.get("needs_followup", False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/orchestrator/routing.py tests/test_orchestrator.py
git commit -m "feat: add state-aware routing (negotiation shortcut, anti-guilt fallback)"
```

---

### Task 6: Orchestrator Graph Skeleton

**Files:**
- Create: `app/orchestrator/graph.py`
- Test: `tests/test_orchestrator.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py`:

```python
import pytest
from app.orchestrator.graph import build_jarvis_graph


@pytest.mark.asyncio
async def test_graph_compiles():
    """The graph should compile without errors."""
    graph = build_jarvis_graph()
    assert graph is not None


@pytest.mark.asyncio
async def test_graph_has_expected_nodes():
    graph = build_jarvis_graph()
    node_names = set(graph.nodes.keys())
    expected = {
        "load_context",
        "extract_brain_dump",
        "classify_intent",
        "planning_module",
        "research_agent",
        "coach_module",
        "knowledge_module",
        "conversation_module",
        "synthesize_response",
        "observation_loop",
    }
    assert expected.issubset(node_names), f"Missing nodes: {expected - node_names}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py::test_graph_compiles -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement graph skeleton with stub nodes**

```python
# app/orchestrator/graph.py
"""Main LangGraph StateGraph — the Jarvis orchestrator.

Replaces execute_agentic_flow() in control_policy.py.
Each node is a stub that will be replaced by real module implementations
in Tasks 9-18.
"""

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from app.orchestrator.state import JarvisState
from app.orchestrator.routing import (
    check_needs_followup,
    check_negotiation_shortcut,
    route_to_module,
)


# ── Stub node functions (replaced by real modules in later tasks) ──

async def _stub_load_context(state: JarvisState) -> dict:
    return {}


async def _stub_extract_brain_dump(state: JarvisState) -> dict:
    return {"brain_dump": None}


async def _stub_classify_intent(state: JarvisState) -> dict:
    return {"intent": "CHAT"}


async def _stub_planning(state: JarvisState) -> dict:
    return {"modules_invoked": state.get("modules_invoked", []) + ["planning_module"]}


async def _stub_research(state: JarvisState) -> dict:
    return {"modules_invoked": state.get("modules_invoked", []) + ["research_agent"]}


async def _stub_coach(state: JarvisState) -> dict:
    return {"modules_invoked": state.get("modules_invoked", []) + ["coach_module"]}


async def _stub_knowledge(state: JarvisState) -> dict:
    return {"modules_invoked": state.get("modules_invoked", []) + ["knowledge_module"]}


async def _stub_conversation(state: JarvisState) -> dict:
    return {
        "response_message": "Hello! I'm Jarvis.",
        "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
    }


async def _stub_synthesize(state: JarvisState) -> dict:
    return {"response_message": state.get("response_message", "Synthesized response.")}


async def _stub_observation(state: JarvisState) -> dict:
    return {"needs_followup": False}


def build_jarvis_graph(checkpointer=None):
    """Build and compile the Jarvis orchestrator graph.

    Returns a compiled LangGraph that can be invoked with .ainvoke()
    or streamed with .astream().
    """
    graph = StateGraph(JarvisState)

    # Register nodes
    graph.add_node("load_context", _stub_load_context)
    graph.add_node("extract_brain_dump", _stub_extract_brain_dump)
    graph.add_node("classify_intent", _stub_classify_intent)
    graph.add_node("planning_module", _stub_planning)
    graph.add_node("research_agent", _stub_research)
    graph.add_node("coach_module", _stub_coach)
    graph.add_node("knowledge_module", _stub_knowledge)
    graph.add_node("conversation_module", _stub_conversation)
    graph.add_node("synthesize_response", _stub_synthesize)
    graph.add_node("observation_loop", _stub_observation)

    # Entry point
    graph.set_entry_point("load_context")

    # Negotiation short-circuit
    graph.add_conditional_edges(
        "load_context",
        check_negotiation_shortcut,
        {
            "negotiation_active": "planning_module",
            "normal": "extract_brain_dump",
        },
    )

    graph.add_edge("extract_brain_dump", "classify_intent")

    # Intent routing
    graph.add_conditional_edges(
        "classify_intent",
        route_to_module,
        {
            "planning_module": "planning_module",
            "research_agent": "research_agent",
            "coach_module": "coach_module",
            "knowledge_module": "knowledge_module",
            "conversation_module": "conversation_module",
        },
    )

    # Non-CHAT modules → synthesize → observe
    for module in ["planning_module", "research_agent", "coach_module", "knowledge_module"]:
        graph.add_edge(module, "synthesize_response")
    graph.add_edge("synthesize_response", "observation_loop")

    # CHAT → observe directly (it IS the synthesis)
    graph.add_edge("conversation_module", "observation_loop")

    # Observation → done or multi-turn loop
    graph.add_conditional_edges(
        "observation_loop",
        check_needs_followup,
        {
            True: "classify_intent",
            False: END,
        },
    )

    cp = checkpointer or MemorySaver()
    return graph.compile(checkpointer=cp)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the graph end-to-end with a stub invocation**

Add to `tests/test_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_graph_runs_chat_end_to_end():
    """A CHAT message should flow: load → extract → classify → conversation → observe → END."""
    graph = build_jarvis_graph()
    initial_state: JarvisState = _make_state(user_message="hello")
    result = await graph.ainvoke(initial_state)
    assert result["response_message"] == "Hello! I'm Jarvis."
    assert "conversation_module" in result["modules_invoked"]
    assert result["needs_followup"] is False
```

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py::test_graph_runs_chat_end_to_end -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/orchestrator/graph.py tests/test_orchestrator.py
git commit -m "feat: add LangGraph orchestrator skeleton with stub nodes and conditional edges"
```

---

### Task 7: Wire SSE Endpoint to LangGraph

**Files:**
- Modify: `app/api/v1/endpoints/chat.py`
- Modify: `app/main.py`

This task is complex — it modifies critical production code. Read both files carefully before editing.

- [ ] **Step 1: Read current chat.py SSE endpoint**

Read `app/api/v1/endpoints/chat.py` to understand the current SSE generator structure. Identify the `chat_stream` or equivalent function that yields SSE events.

- [ ] **Step 2: Read current main.py lifespan**

Read `app/main.py` to understand how `app.state` is initialized.

- [ ] **Step 3: Add graph initialization to main.py lifespan**

In `app/main.py`, after the existing `app.state.memory_store = MemoryStore(...)` line, add:

```python
    from app.orchestrator.graph import build_jarvis_graph
    app.state.jarvis_graph = build_jarvis_graph()
```

- [ ] **Step 4: Add NODE_TO_PHASE mapping to chat.py**

At the top of `app/api/v1/endpoints/chat.py`, add:

```python
NODE_TO_PHASE = {
    "load_context": "loading_context",
    "extract_brain_dump": "brain_dump_extraction",
    "classify_intent": "intent_classified",
    "planning_module": "planning",
    "research_agent": "researching",
    "coach_module": "coaching",
    "knowledge_module": "ingesting",
    "conversation_module": "responding",
    "synthesize_response": "synthesizing",
    "observation_loop": "learning",
}
```

- [ ] **Step 5: Add a `/chat/v2/stream` endpoint that uses LangGraph**

Do NOT modify the existing `/chat/stream` endpoint yet. Add a NEW endpoint so both old and new can run in parallel during migration:

```python
@router.post("/v2/stream")
async def chat_stream_v2(request: ChatRequest, http_request: Request):
    """SSE endpoint using LangGraph orchestrator. Same SSE contract as /chat/stream."""
    from app.core.user_model import UserModel
    from app.orchestrator.state import ConversationPhase, NegotiationPhase

    jarvis_graph = http_request.app.state.jarvis_graph
    db_client = http_request.app.state.db_client
    user_model = UserModel(user_id=request.user_id, db=db_client)

    if hasattr(http_request.app.state, "memory_store"):
        user_model.set_memory_store(http_request.app.state.memory_store)

    initial_state = {
        "user_model": user_model,
        "user_message": request.user_prompt,
        "brain_dump": None,
        "intent": None,
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "response_message": None,
        "conversation_phase": ConversationPhase.GREETING,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": [],
        "needs_followup": False,
        "error": None,
    }

    config = {"configurable": {"thread_id": request.user_id}}

    async def event_gen():
        try:
            async for event in jarvis_graph.astream(initial_state, config):
                node_name = list(event.keys())[0]
                node_state = event[node_name]

                phase = NODE_TO_PHASE.get(node_name)
                if phase:
                    yield f"event: phase\ndata: {json.dumps({'phase': phase})}\n\n"

                if node_name == "classify_intent" and node_state.get("intent"):
                    yield f"event: step\ndata: {json.dumps({'intent': node_state['intent'], 'stage': 'intent_classified'})}\n\n"

            final = jarvis_graph.get_state(config).values
            yield f"event: step\ndata: {json.dumps({'intent': str(final.get('intent', 'CHAT')), 'stage': 'pipeline_done', 'model_mode': 'gemma', 'synthesis_model': 'gemma-4-e4b'})}\n\n"
            yield f"event: complete\ndata: {json.dumps({'intent': str(final.get('intent', 'CHAT')), 'message': final.get('response_message', ''), 'thinking_process': final.get('thinking_process')})}\n\n"
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    from starlette.responses import StreamingResponse
    return StreamingResponse(event_gen(), media_type="text/event-stream")
```

- [ ] **Step 6: Test manually**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
uvicorn app.main:app --reload --port 8000 &
sleep 3
curl -N -X POST http://localhost:8000/api/v1/chat/v2/stream \
  -H "Content-Type: application/json" \
  -d '{"user_prompt": "hello", "user_id": "demo"}'
```

Expected: SSE events with phase names flowing through, ending with `event: complete`.

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/api/v1/endpoints/chat.py
git commit -m "feat: add /chat/v2/stream SSE endpoint using LangGraph orchestrator"
```

---

### Task 8: Model Router

**Files:**
- Create: `app/core/model_router.py`
- Modify: `app/core/config.py`
- Test: `tests/test_model_router.py`

- [ ] **Step 1: Add Gemma 4 model constants to config.py**

In `app/core/config.py`, add:

```python
# Gemma 4 models (replacing Qwen)
GEMMA_PRIMARY_MODEL: str = os.getenv("GEMMA_PRIMARY_MODEL", "openai/google/gemma-4-26b-a4b")
GEMMA_FAST_MODEL: str = os.getenv("GEMMA_FAST_MODEL", "openai/google/gemma-4-e4b")
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_model_router.py
import pytest
from unittest.mock import AsyncMock, patch

from app.core.model_router import ModelRole, MODEL_ROUTING, route_llm_call
from app.orchestrator.hooks import ActionHooks, pii_filter_hook


def test_routing_table_completeness():
    expected_tasks = [
        "socratic_chunker", "habit_translation", "document_understanding",
        "research_summarization", "intent_classification", "brain_dump_extraction",
        "memory_extraction", "voice_of_jarvis", "calendar_parsing",
        "goal_validation", "web_search", "real_time_research",
    ]
    for task in expected_tasks:
        assert task in MODEL_ROUTING, f"Missing routing for {task}"


def test_primary_tasks_use_26b():
    assert MODEL_ROUTING["socratic_chunker"] == ModelRole.PRIMARY
    assert MODEL_ROUTING["habit_translation"] == ModelRole.PRIMARY


def test_fast_tasks_use_e4b():
    assert MODEL_ROUTING["intent_classification"] == ModelRole.FAST
    assert MODEL_ROUTING["voice_of_jarvis"] == ModelRole.FAST


def test_cloud_tasks_use_gemini():
    assert MODEL_ROUTING["web_search"] == ModelRole.CLOUD
    assert MODEL_ROUTING["real_time_research"] == ModelRole.CLOUD
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_model_router.py -v`
Expected: FAIL

- [ ] **Step 4: Implement model router**

```python
# app/core/model_router.py
"""Task-based model routing. Replaces hybrid_route_query().

Same interface (prompt + response_schema → parsed model), different internals.
Local-first always. Gemini fallback only for web research or validation failure.
PII filter hook runs exactly once before any cloud call.
"""

import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from app.core.config import (
    GEMMA_FAST_MODEL,
    GEMMA_PRIMARY_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LOCAL_LLM_URL,
)
from app.core.jarvis_logger import logger
from app.orchestrator.hooks import ActionHooks, HookDecision


class ModelRole(str, Enum):
    PRIMARY = "primary"    # Gemma 4 26B A4B
    FAST = "fast"          # Gemma 4 E4B
    CLOUD = "cloud"        # Gemini 2.5 Flash


MODEL_ROUTING: dict[str, ModelRole] = {
    # PRIMARY (26B) — sequential, never concurrent
    "socratic_chunker": ModelRole.PRIMARY,
    "habit_translation": ModelRole.PRIMARY,
    "document_understanding": ModelRole.PRIMARY,
    "research_summarization": ModelRole.PRIMARY,
    # FAST (E4B) — can run alongside 26B
    "intent_classification": ModelRole.FAST,
    "brain_dump_extraction": ModelRole.FAST,
    "memory_extraction": ModelRole.FAST,
    "voice_of_jarvis": ModelRole.FAST,
    "calendar_parsing": ModelRole.FAST,
    "goal_validation": ModelRole.FAST,
    # CLOUD (Gemini) — web research, fallback
    "web_search": ModelRole.CLOUD,
    "real_time_research": ModelRole.CLOUD,
}

_ROLE_TO_MODEL = {
    ModelRole.PRIMARY: GEMMA_PRIMARY_MODEL,
    ModelRole.FAST: GEMMA_FAST_MODEL,
    ModelRole.CLOUD: GEMINI_MODEL,
}

_FENCE_RE = re.compile(r"```json|```")


def strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


async def route_llm_call(
    task: str,
    prompt: str,
    system_prompt: str = "",
    response_schema: Optional[type[BaseModel]] = None,
    hooks: Optional[ActionHooks] = None,
    conversation_history: Optional[list[dict]] = None,
) -> str | BaseModel:
    """Route LLM call with fallback chain. Local-first always.

    Replaces hybrid_route_query() from litellm_conf.py.
    """
    from app.models.brain.litellm_conf import hybrid_route_query

    role = MODEL_ROUTING.get(task, ModelRole.FAST)
    model = _ROLE_TO_MODEL.get(role, GEMMA_FAST_MODEL)

    # Try local first (unless cloud-only task)
    if role in (ModelRole.PRIMARY, ModelRole.FAST):
        try:
            result = await hybrid_route_query(
                user_prompt=prompt,
                system_prompt=system_prompt,
                response_schema=response_schema,
                model_override=model,
                conversation_history=conversation_history,
            )
            if response_schema and isinstance(result, str):
                return response_schema.model_validate_json(strip_fences(result))
            return result
        except (ValidationError, Exception) as e:
            logger.warning(f"Local {model} failed for {task}: {e}")

    # Cloud path — PII filter exactly once here
    if hooks:
        pii_result = await hooks.execute("PreCloudLLM", prompt=prompt)
        if pii_result.decision == HookDecision.MODIFY:
            prompt = pii_result.modified_input["prompt"]

    if not GEMINI_API_KEY:
        raise RuntimeError(f"Local LLM failed for {task} and no GEMINI_API_KEY set")

    from app.models.brain.litellm_conf import gemini_primary_route

    result = await gemini_primary_route(
        user_prompt=prompt,
        system_prompt=system_prompt,
        response_schema=response_schema,
        conversation_history=conversation_history,
    )
    if response_schema and isinstance(result, str):
        return response_schema.model_validate_json(strip_fences(result))
    return result
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_model_router.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/core/model_router.py app/core/config.py tests/test_model_router.py
git commit -m "feat: add task-based model router (Gemma 26B + E4B + Gemini fallback)"
```

---

## Layer 3: Planning Module (Tasks 9-10)

### Task 9: Planning Sub-Graph

**Files:**
- Create: `app/modules/planning_graph.py`
- Test: `tests/test_planning_graph.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_planning_graph.py
import pytest
from app.modules.planning_graph import build_planning_graph


def test_planning_graph_compiles():
    graph = build_planning_graph()
    assert graph is not None


def test_planning_graph_has_expected_nodes():
    graph = build_planning_graph()
    node_names = set(graph.nodes.keys())
    expected = {
        "fetch_constraints",
        "translate_habits",
        "memory_to_constraints",
        "validate_goal",
        "decompose_goal",
        "fuse_tasks",
        "solve_schedule",
        "handle_infeasible",
    }
    assert expected.issubset(node_names), f"Missing: {expected - node_names}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_planning_graph.py -v`
Expected: FAIL

- [ ] **Step 3: Implement planning sub-graph wrapping existing functions**

```python
# app/modules/planning_graph.py
"""Planning sub-graph — wraps existing pipeline functions as LangGraph nodes.

Existing code in control_policy.py, habit_translator.py, solver.py etc.
is called FROM these nodes, not rewritten.
"""

from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.core.jarvis_logger import logger


class PlanningState(TypedDict):
    """State flowing through the planning sub-graph."""

    user_id: str
    user_model: Any
    planning_goal: Optional[str]
    habits_text: str
    semantic_slots: list
    time_slots: list
    constraints: list
    task_chunks: list
    pending_tasks: list
    schedule: Optional[dict]
    horizon_minutes: int
    retry_count: int
    clarification_request: Optional[str]
    error: Optional[str]
    progress_callback: Any


# ── Node functions: wrap existing code ──


async def fetch_constraints(state: PlanningState) -> dict:
    """Fetch behavioral constraints from User Model."""
    cb = state.get("progress_callback")
    if cb:
        cb("habits_fetched")

    user_model = state["user_model"]
    constraints = await user_model.get_behavioral_constraints()
    habits_text = "\n".join(c.get("raw_text", "") for c in constraints if c.get("constraint_type") == "habit")
    return {"constraints": constraints, "habits_text": habits_text}


async def translate_habits(state: PlanningState) -> dict:
    """Translate habits text to semantic time slots. Wraps habit_translator.py."""
    cb = state.get("progress_callback")
    if cb:
        cb("translating")

    habits_text = state.get("habits_text", "")
    if not habits_text.strip():
        return {"semantic_slots": []}

    from app.services.analytical.habit_translator import translate_habits_to_slots

    slots = await translate_habits_to_slots(habits_text)
    return {"semantic_slots": [s.model_dump() for s in slots] if slots else []}


async def memory_to_constraints(state: PlanningState) -> dict:
    """Bridge PEARL patterns to OR-Tools constraints."""
    user_model = state["user_model"]
    patterns = await user_model.get_pearl_patterns()
    # PEARL patterns are already applied as behavioral_constraints
    # This node is where future pattern → constraint bridging will live
    return {}


async def validate_goal(state: PlanningState) -> dict:
    """Check if the planning goal is clear enough to decompose."""
    goal = state.get("planning_goal", "")
    if not goal or len(goal.strip()) < 5:
        return {"clarification_request": "Could you tell me more about what you'd like to plan?"}
    return {"clarification_request": None}


def is_goal_clear(state: PlanningState) -> bool:
    return state.get("clarification_request") is None


async def decompose_goal(state: PlanningState) -> dict:
    """Socratic chunker — decompose goal into TaskChunks. Wraps existing code."""
    cb = state.get("progress_callback")
    if cb:
        cb("decomposing")

    from app.models.brain.litellm_conf import hybrid_route_query
    from app.schemas.context import ExecutionGraph

    goal = state.get("planning_goal", "")
    system_prompt = (
        "You are a task decomposition expert. Break the user's goal into 5-8 concrete, "
        "actionable micro-tasks of 15-25 minutes each. Each task must have clear completion criteria."
    )

    try:
        result = await hybrid_route_query(
            user_prompt=goal,
            system_prompt=system_prompt,
            response_schema=ExecutionGraph,
        )
        if isinstance(result, str):
            import json
            import re

            clean = re.sub(r"```json|```", "", result).strip()
            data = json.loads(clean)
            graph = ExecutionGraph.model_validate(data)
        else:
            graph = result if isinstance(result, ExecutionGraph) else ExecutionGraph.model_validate(result)

        return {"task_chunks": [tc.model_dump() for tc in graph.decomposition]}
    except Exception as e:
        logger.error(f"Decomposition failed: {e}")
        return {"error": f"Decomposition failed: {e}", "task_chunks": []}


async def fuse_tasks(state: PlanningState) -> dict:
    """Fuse new chunks with existing pending tasks."""
    cb = state.get("progress_callback")
    if cb:
        cb("scheduling")

    user_model = state["user_model"]
    pending = await user_model.get_pending_tasks()
    new_chunks = state.get("task_chunks", [])
    return {"pending_tasks": pending, "task_chunks": new_chunks}


async def solve_schedule(state: PlanningState) -> dict:
    """Run OR-Tools CP-SAT solver. Wraps solver.py."""
    from app.core.or_tools.solver import JarvisScheduler

    chunks = state.get("task_chunks", [])
    horizon = state.get("horizon_minutes", 2880)

    if not chunks:
        return {"error": "No tasks to schedule", "schedule": None}

    scheduler = JarvisScheduler(horizon_minutes=horizon)

    # Apply time slot blocks from constraints
    for slot in state.get("time_slots", []):
        if slot.get("availability") == "blocked":
            scheduler.add_hard_block(slot["start_min"], slot["end_min"], slot.get("name", "block"))
        elif slot.get("availability") == "minimal_work":
            scheduler.add_soft_block(
                slot["start_min"],
                slot["end_min"],
                slot.get("name", "soft"),
                max_task_duration=slot.get("max_task_duration", 15),
                max_difficulty=slot.get("max_difficulty", 0.4),
            )

    # Add tasks
    for i, chunk in enumerate(chunks):
        scheduler.add_task(
            task_id=chunk.get("task_id", f"t{i}"),
            duration=chunk.get("duration_minutes", 25),
            priority_score=len(chunks) - i,
            dependencies=chunk.get("dependencies", []),
            difficulty_weight=chunk.get("difficulty_weight", 0.5),
        )

    scheduler.build_dependencies()
    result, status = scheduler.solve()

    if status == "INFEASIBLE":
        return {"schedule": None, "error": "INFEASIBLE"}

    return {"schedule": result, "error": None}


async def handle_infeasible(state: PlanningState) -> dict:
    """Widen horizon and retry, or give up with anti-guilt message."""
    retry_count = state.get("retry_count", 0)
    current_horizon = state.get("horizon_minutes", 2880)

    HORIZON_RETRY_SEQUENCE = [4320, 7200]  # 72h, 5 days

    if retry_count < len(HORIZON_RETRY_SEQUENCE):
        new_horizon = HORIZON_RETRY_SEQUENCE[retry_count]
        return {
            "horizon_minutes": new_horizon,
            "retry_count": retry_count + 1,
            "error": None,
        }
    else:
        return {
            "error": "INFEASIBLE_EXHAUSTED",
            "clarification_request": (
                "I couldn't fit everything in even with a 5-day window. "
                "This is a scope problem, not a you problem. "
                "Want to reduce scope or extend the deadline?"
            ),
        }


def check_feasibility(state: PlanningState) -> str:
    if state.get("error") == "INFEASIBLE":
        return "INFEASIBLE"
    return "OPTIMAL"


def can_retry(state: PlanningState) -> str:
    if state.get("error") == "INFEASIBLE_EXHAUSTED":
        return "exhausted"
    return "retry"


def build_planning_graph():
    """Build and compile the Planning sub-graph."""
    graph = StateGraph(PlanningState)

    graph.add_node("fetch_constraints", fetch_constraints)
    graph.add_node("translate_habits", translate_habits)
    graph.add_node("memory_to_constraints", memory_to_constraints)
    graph.add_node("validate_goal", validate_goal)
    graph.add_node("decompose_goal", decompose_goal)
    graph.add_node("fuse_tasks", fuse_tasks)
    graph.add_node("solve_schedule", solve_schedule)
    graph.add_node("handle_infeasible", handle_infeasible)

    graph.set_entry_point("fetch_constraints")
    graph.add_edge("fetch_constraints", "translate_habits")
    graph.add_edge("translate_habits", "memory_to_constraints")
    graph.add_edge("memory_to_constraints", "validate_goal")
    graph.add_conditional_edges("validate_goal", is_goal_clear, {
        True: "decompose_goal",
        False: END,
    })
    graph.add_edge("decompose_goal", "fuse_tasks")
    graph.add_edge("fuse_tasks", "solve_schedule")
    graph.add_conditional_edges("solve_schedule", check_feasibility, {
        "OPTIMAL": END,
        "INFEASIBLE": "handle_infeasible",
    })
    graph.add_conditional_edges("handle_infeasible", can_retry, {
        "retry": "solve_schedule",
        "exhausted": END,
    })

    return graph.compile()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_planning_graph.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/modules/planning_graph.py tests/test_planning_graph.py
git commit -m "feat: add Planning sub-graph wrapping existing pipeline functions"
```

---

### Task 10: Wire Planning Module into Orchestrator

**Files:**
- Modify: `app/orchestrator/graph.py`

- [ ] **Step 1: Replace the planning stub with the real sub-graph**

In `app/orchestrator/graph.py`, replace `_stub_planning` with:

```python
from app.modules.planning_graph import build_planning_graph

# In build_jarvis_graph():
planning_compiled = build_planning_graph()

async def planning_module_node(state: JarvisState) -> dict:
    """Wrap the planning sub-graph as an orchestrator node."""
    user_model = state.get("user_model")
    brain_dump = state.get("brain_dump")

    planning_state = {
        "user_id": user_model.user_id if user_model else "demo",
        "user_model": user_model,
        "planning_goal": brain_dump.planning_goal if brain_dump else state.get("user_message", ""),
        "habits_text": "",
        "semantic_slots": [],
        "time_slots": [],
        "constraints": [],
        "task_chunks": [],
        "pending_tasks": [],
        "schedule": None,
        "horizon_minutes": 2880,
        "retry_count": 0,
        "clarification_request": None,
        "error": None,
        "progress_callback": None,
    }

    result = await planning_compiled.ainvoke(planning_state)
    return {
        "schedule": result.get("schedule"),
        "execution_graph": {"decomposition": result.get("task_chunks", [])},
        "clarification_request": result.get("clarification_request"),
        "error": result.get("error"),
        "modules_invoked": state.get("modules_invoked", []) + ["planning_module"],
    }

# Replace the stub registration:
graph.add_node("planning_module", planning_module_node)
```

- [ ] **Step 2: Run orchestrator tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py tests/test_planning_graph.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/orchestrator/graph.py
git commit -m "feat: wire Planning sub-graph into orchestrator (replaces stub)"
```

---

## Layer 4: Observation Loop (Tasks 11-12)

### Task 11: Observation Loop Implementation

**Files:**
- Create: `app/core/observation.py`
- Test: `tests/test_observation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_observation.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.observation import run_observation_loop
from app.orchestrator.state import ConversationPhase, NegotiationPhase


def _make_state(**overrides):
    base = {
        "user_model": MagicMock(),
        "user_message": "plan my DSA study",
        "response_message": "Here's your schedule...",
        "brain_dump": None,
        "intent": "PLAN_DAY",
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "conversation_phase": ConversationPhase.PLANNING,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": ["planning_module"],
        "needs_followup": False,
        "error": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_observation_loop_returns_state():
    state = _make_state()
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)

    result = await run_observation_loop(state)
    assert result is not None
    assert result.get("needs_followup") is False


@pytest.mark.asyncio
async def test_observation_loop_calls_pearl():
    state = _make_state()
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)

    await run_observation_loop(state)
    state["user_model"].get_pearl_patterns.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_observation.py -v`
Expected: FAIL

- [ ] **Step 3: Implement observation loop**

```python
# app/core/observation.py
"""Observation Loop — post-turn behavioral intelligence.

Runs after every interaction (~200-500ms, blocking).
1. Extract memories (E4B)
2. Detect PEARL patterns (stats)
3. Update cognitive state (math)
4. Bridge patterns → constraints

This is what makes Jarvis get smarter over time.
"""

from typing import Any

from app.core.jarvis_logger import logger


async def extract_and_store_memories(user_model: Any, user_message: str, response_message: str) -> None:
    """Extract facts worth remembering from this turn.

    Uses E4B (~300ms). Not every turn produces memories.
    """
    memory_store = await user_model.get_memory_store()
    if not memory_store:
        return

    # Future: call E4B to extract memories from conversation
    # For now, this is a placeholder that will be implemented with the model router
    pass


async def detect_pearl_patterns(user_model: Any) -> list[dict]:
    """Detect recurring behavioral patterns from episodic data.

    Pure statistical analysis — no LLM needed (~50ms).
    """
    patterns = await user_model.get_pearl_patterns()
    # Future: run PEARL detectors on recent task completions
    # Current patterns are loaded from behavioral_constraints
    return patterns


async def update_cognitive_state(user_model: Any) -> None:
    """Update energy estimate from time + behavioral signals (~10ms)."""
    energy = await user_model.get_estimated_energy()
    # Future: adjust by recent skip rate, mood signals
    pass


async def bridge_patterns_to_constraints(user_model: Any, patterns: list[dict]) -> None:
    """Convert PEARL observations into OR-Tools constraints (~50ms).

    THE MOAT: memories that change the scheduling math.
    Only acts on patterns with confidence ≥ 0.7.
    """
    for pattern in patterns:
        confidence = pattern.get("confidence", 0.0)
        if confidence < 0.7:
            continue

        pattern_type = pattern.get("type", "")
        # Future: generate constraint from pattern and upsert
        # e.g., skip_pattern → soft block on morning hours
        logger.debug(f"PEARL pattern {pattern_type} (conf={confidence}) ready for bridging")


async def run_observation_loop(state: dict) -> dict:
    """Post-turn behavioral intelligence. Blocking but fast (~200-500ms).

    Must be blocking because check_needs_followup reads results
    to decide whether to loop back.
    """
    user_model = state.get("user_model")
    if not user_model:
        return {"needs_followup": False}

    user_message = state.get("user_message", "")
    response_message = state.get("response_message", "")

    # Sequential — total under 500ms
    await extract_and_store_memories(user_model, user_message, response_message)
    patterns = await detect_pearl_patterns(user_model)
    await update_cognitive_state(user_model)
    await bridge_patterns_to_constraints(user_model, patterns)

    return {"needs_followup": False}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_observation.py -v`
Expected: PASS

- [ ] **Step 5: Wire into orchestrator graph**

In `app/orchestrator/graph.py`, replace `_stub_observation` with:

```python
from app.core.observation import run_observation_loop

# Replace: graph.add_node("observation_loop", _stub_observation)
# With:    graph.add_node("observation_loop", run_observation_loop)
```

- [ ] **Step 6: Run all tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_orchestrator.py tests/test_observation.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/core/observation.py tests/test_observation.py app/orchestrator/graph.py
git commit -m "feat: add Observation Loop (memory extract + PEARL + constraint bridge)"
```

---

## Layer 5: Remaining Modules + Hooks (Tasks 12-16)

### Task 12: Conversation Module + synthesize_response

**Files:**
- Create: `app/modules/conversation.py`
- Test: `tests/test_modules.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_modules.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.modules.conversation import run_general_chat, voice_of_jarvis_synthesis


@pytest.mark.asyncio
async def test_conversation_module_returns_message():
    state = {
        "user_model": MagicMock(),
        "user_message": "hello",
        "modules_invoked": [],
    }
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)

    result = await run_general_chat(state)
    assert "response_message" in result
    assert "conversation_module" in result["modules_invoked"]


@pytest.mark.asyncio
async def test_synthesize_response_wraps_module_output():
    state = {
        "user_model": MagicMock(),
        "schedule": {"t1": {"start": 480, "end": 510}},
        "execution_graph": {"decomposition": [{"title": "Study DSA"}]},
        "response_message": None,
        "conversation_phase": "planning",
    }
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.8)

    result = await voice_of_jarvis_synthesis(state)
    assert "response_message" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_modules.py -v`
Expected: FAIL

- [ ] **Step 3: Implement conversation module**

```python
# app/modules/conversation.py
"""Conversation module (CHAT-only) + synthesize_response (orchestrator step).

Conversation handles general chat. synthesize_response wraps
other modules' output in Voice of Jarvis personality.
"""

from typing import Any

from app.core.jarvis_logger import logger


async def run_general_chat(state: dict) -> dict:
    """Handle CHAT intent — general conversation."""
    user_message = state.get("user_message", "")

    # Use existing Voice of Jarvis for general conversation
    try:
        from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

        execution_summary = {
            "intent": "CHAT",
            "user_prompt": user_message,
        }
        message, thinking = await synthesize_jarvis_response(execution_summary)
        return {
            "response_message": message,
            "thinking_process": thinking,
            "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
        }
    except Exception as e:
        logger.error(f"Conversation module error: {e}")
        return {
            "response_message": "I'm here to help! Could you tell me more?",
            "modules_invoked": state.get("modules_invoked", []) + ["conversation_module"],
        }


async def voice_of_jarvis_synthesis(state: dict) -> dict:
    """Orchestrator step — wrap module output in Voice of Jarvis personality.

    Runs after Planning, Research, Coach, Knowledge modules.
    NOT a module — an orchestrator step.
    """
    try:
        from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response

        execution_summary = {
            "intent": state.get("intent", "CHAT"),
            "schedule": state.get("schedule"),
            "execution_graph": state.get("execution_graph"),
            "research_results": state.get("research_results"),
            "ingestion_result": state.get("ingestion_result"),
            "clarification_request": state.get("clarification_request"),
            "error": state.get("error"),
            "user_prompt": state.get("user_message", ""),
        }
        message, thinking = await synthesize_jarvis_response(execution_summary)
        return {
            "response_message": message,
            "thinking_process": thinking,
        }
    except Exception as e:
        logger.error(f"Voice of Jarvis synthesis error: {e}")
        return {
            "response_message": state.get("clarification_request", "Here's what I've got for you."),
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_modules.py -v`
Expected: PASS

- [ ] **Step 5: Wire into orchestrator**

In `app/orchestrator/graph.py`, replace the conversation and synthesize stubs:

```python
from app.modules.conversation import run_general_chat, voice_of_jarvis_synthesis

# Replace stubs:
graph.add_node("conversation_module", run_general_chat)
graph.add_node("synthesize_response", voice_of_jarvis_synthesis)
```

- [ ] **Step 6: Commit**

```bash
git add app/modules/conversation.py tests/test_modules.py app/orchestrator/graph.py
git commit -m "feat: add Conversation module + Voice of Jarvis synthesis orchestrator step"
```

---

### Task 13: Coach Module

**Files:**
- Create: `app/modules/coach.py`
- Test: `tests/test_modules.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_modules.py`:

```python
from app.modules.coach import run_coaching_response


@pytest.mark.asyncio
async def test_coach_module_returns_message():
    state = {
        "user_model": MagicMock(),
        "user_message": "how am I doing?",
        "modules_invoked": [],
        "error": None,
    }
    state["user_model"].get_pending_tasks = AsyncMock(return_value=[
        {"status": "completed", "title": "Study DSA"},
        {"status": "pending", "title": "Read chapter 5"},
    ])
    state["user_model"].get_pearl_patterns = AsyncMock(return_value=[])
    state["user_model"].get_estimated_energy = AsyncMock(return_value=0.7)

    result = await run_coaching_response(state)
    assert "response_message" in result
    assert "coach_module" in result["modules_invoked"]
```

- [ ] **Step 2: Implement coach module**

```python
# app/modules/coach.py
"""Coach module — anti-guilt, progress tracking, WOOP, mastery orientation.

Fires on: CHECK_PROGRESS intent, INFEASIBLE fallback, PEARL stress pre-step.
Single node (no sub-graph needed).
"""

from typing import Any

from app.core.jarvis_logger import logger


async def run_coaching_response(state: dict) -> dict:
    """Provide coaching, progress feedback, or anti-guilt support."""
    user_model = state.get("user_model")
    error = state.get("error")

    # Anti-guilt: if planning failed (INFEASIBLE)
    if error and "INFEASIBLE" in str(error):
        return {
            "response_message": (
                "This is a scope problem, not a you problem. "
                "You've got more on your plate than fits in the time available. "
                "Want to reduce scope, extend the deadline, or adjust your daily capacity?"
            ),
            "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
        }

    # CHECK_PROGRESS: show stats
    try:
        if user_model:
            tasks = await user_model.get_pending_tasks()
            completed = sum(1 for t in tasks if t.get("status") == "completed")
            pending = sum(1 for t in tasks if t.get("status") == "pending")
            total = len(tasks)

            return {
                "response_message": (
                    f"Here's where you stand: {completed} tasks completed, "
                    f"{pending} still pending out of {total} total. "
                    "Every task you finish is progress — keep it going!"
                ),
                "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
            }
    except Exception as e:
        logger.error(f"Coach module error: {e}")

    return {
        "response_message": "You're doing great — keep going!",
        "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
    }
```

- [ ] **Step 3: Run test and verify pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_modules.py -v`
Expected: PASS

- [ ] **Step 4: Wire into orchestrator + commit**

```bash
# In graph.py: graph.add_node("coach_module", run_coaching_response)
git add app/modules/coach.py tests/test_modules.py app/orchestrator/graph.py
git commit -m "feat: add Coach module (anti-guilt, progress tracking)"
```

---

### Task 14: Knowledge Module Sub-Graph

**Files:**
- Create: `app/modules/knowledge_graph.py`
- Test: `tests/test_modules.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_modules.py`:

```python
from app.modules.knowledge_graph import build_knowledge_graph


def test_knowledge_graph_compiles():
    graph = build_knowledge_graph()
    assert graph is not None


def test_knowledge_graph_has_expected_nodes():
    graph = build_knowledge_graph()
    node_names = set(graph.nodes.keys())
    expected = {"classify_content", "extract_calendar", "ingest_document", "link_to_tasks", "file_operations", "propose_actions"}
    assert expected.issubset(node_names)
```

- [ ] **Step 2: Implement knowledge sub-graph**

```python
# app/modules/knowledge_graph.py
"""Knowledge module sub-graph — document ingestion, file ops, ChromaDB, task linking.

Wraps existing extraction pipeline code.
"""

from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.core.jarvis_logger import logger


class KnowledgeState(TypedDict):
    user_id: str
    user_model: Any
    content: Optional[str]
    file_bytes: Optional[bytes]
    media_type: Optional[str]
    file_name: Optional[str]
    content_type: Optional[str]  # calendar | document | file_op
    ingestion_result: Optional[dict]
    calendar_result: Optional[dict]
    linked_tasks: list
    action_proposals: list
    error: Optional[str]


async def classify_content(state: KnowledgeState) -> dict:
    media = state.get("media_type", "")
    file_name = state.get("file_name", "")
    if "calendar" in (state.get("content", "") or "").lower() or "timetable" in file_name.lower():
        return {"content_type": "calendar"}
    elif state.get("file_bytes") or media:
        return {"content_type": "document"}
    else:
        return {"content_type": "file_op"}


def content_type_router(state: KnowledgeState) -> str:
    return state.get("content_type", "document")


async def extract_calendar(state: KnowledgeState) -> dict:
    try:
        from app.services.extraction.calendar_extractor import extract_calendar_from_text
        # Wrap existing calendar extraction
        return {"calendar_result": {"status": "pending_approval"}}
    except Exception as e:
        return {"error": str(e)}


async def ingest_document(state: KnowledgeState) -> dict:
    try:
        from app.services.extraction.orchestrator import process_ingestion
        result = await process_ingestion(
            payload=state.get("content"),
            file_bytes=state.get("file_bytes"),
            media_type=state.get("media_type"),
            user_id=state.get("user_id"),
            file_name=state.get("file_name"),
        )
        return {"ingestion_result": result.model_dump() if hasattr(result, "model_dump") else {"status": "ok"}}
    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        return {"error": str(e)}


async def link_to_tasks(state: KnowledgeState) -> dict:
    return {"linked_tasks": []}


async def file_operations(state: KnowledgeState) -> dict:
    return {}


async def propose_actions(state: KnowledgeState) -> dict:
    return {"action_proposals": []}


def build_knowledge_graph():
    graph = StateGraph(KnowledgeState)
    graph.add_node("classify_content", classify_content)
    graph.add_node("extract_calendar", extract_calendar)
    graph.add_node("ingest_document", ingest_document)
    graph.add_node("link_to_tasks", link_to_tasks)
    graph.add_node("file_operations", file_operations)
    graph.add_node("propose_actions", propose_actions)

    graph.set_entry_point("classify_content")
    graph.add_conditional_edges("classify_content", content_type_router, {
        "calendar": "extract_calendar",
        "document": "ingest_document",
        "file_op": "file_operations",
    })
    graph.add_edge("ingest_document", "link_to_tasks")
    graph.add_edge("link_to_tasks", "propose_actions")
    graph.add_edge("propose_actions", END)
    graph.add_edge("extract_calendar", END)
    graph.add_edge("file_operations", END)

    return graph.compile()
```

- [ ] **Step 3: Run tests, wire into orchestrator, commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
python -m pytest tests/test_modules.py -v
# Wire: graph.add_node("knowledge_module", knowledge_module_node)
git add app/modules/knowledge_graph.py tests/test_modules.py app/orchestrator/graph.py
git commit -m "feat: add Knowledge module sub-graph (ingestion, calendar, file ops)"
```

---

### Task 15: Research Agent Sub-Graph

**Files:**
- Create: `app/modules/research_graph.py`
- Test: `tests/test_modules.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_modules.py`:

```python
from app.modules.research_graph import build_research_graph


def test_research_graph_compiles():
    graph = build_research_graph()
    assert graph is not None


def test_research_graph_has_expected_nodes():
    graph = build_research_graph()
    node_names = set(graph.nodes.keys())
    expected = {"plan_research", "execute_search", "evaluate_results", "summarize", "link_to_tasks"}
    assert expected.issubset(node_names)
```

- [ ] **Step 2: Implement research sub-graph**

```python
# app/modules/research_graph.py
"""Research agent sub-graph — autonomous, can iterate.

Wraps existing workspace_builder.py web search + RAG.
"""

from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.core.jarvis_logger import logger


class ResearchState(TypedDict):
    user_id: str
    user_model: Any
    query: str
    search_results: list
    iteration_count: int
    max_iterations: int
    summary: Optional[str]
    linked_tasks: list
    error: Optional[str]


async def plan_research(state: ResearchState) -> dict:
    return {"search_results": [], "iteration_count": 0, "max_iterations": 3}


async def execute_search(state: ResearchState) -> dict:
    query = state.get("query", "")
    try:
        from app.services.analytical.workspace_builder import perform_learning_style_search
        results = await perform_learning_style_search(query, "reader")
        return {
            "search_results": state.get("search_results", []) + [r.model_dump() for r in results],
            "iteration_count": state.get("iteration_count", 0) + 1,
        }
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return {"error": str(e), "iteration_count": state.get("iteration_count", 0) + 1}


def needs_more(state: ResearchState) -> bool:
    results = state.get("search_results", [])
    iterations = state.get("iteration_count", 0)
    max_iter = state.get("max_iterations", 3)
    return len(results) < 3 and iterations < max_iter


async def summarize(state: ResearchState) -> dict:
    results = state.get("search_results", [])
    summary = f"Found {len(results)} results for your query."
    return {"summary": summary}


async def link_to_tasks(state: ResearchState) -> dict:
    return {"linked_tasks": []}


def build_research_graph():
    graph = StateGraph(ResearchState)
    graph.add_node("plan_research", plan_research)
    graph.add_node("execute_search", execute_search)
    graph.add_node("evaluate_results", lambda s: {})
    graph.add_node("summarize", summarize)
    graph.add_node("link_to_tasks", link_to_tasks)

    graph.set_entry_point("plan_research")
    graph.add_edge("plan_research", "execute_search")
    graph.add_edge("execute_search", "evaluate_results")
    graph.add_conditional_edges("evaluate_results", needs_more, {
        True: "execute_search",
        False: "summarize",
    })
    graph.add_edge("summarize", "link_to_tasks")
    graph.add_edge("link_to_tasks", END)

    return graph.compile()
```

- [ ] **Step 3: Run tests, wire, commit**

```bash
python -m pytest tests/test_modules.py -v
git add app/modules/research_graph.py tests/test_modules.py app/orchestrator/graph.py
git commit -m "feat: add Research agent sub-graph (autonomous, iterates)"
```

---

### Task 16: Register Remaining 6 Hook Handlers

**Files:**
- Modify: `app/orchestrator/hooks.py`
- Test: `tests/test_hooks.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hooks.py`:

```python
from app.orchestrator.hooks import (
    consent_gate_module,
    consent_gate_schedule,
)


@pytest.mark.asyncio
async def test_consent_gate_allows_user_initiated():
    result = await consent_gate_module(initiated_by="user", module="planning_module")
    assert result.decision == HookDecision.ALLOW


@pytest.mark.asyncio
async def test_consent_gate_asks_for_system_initiated():
    result = await consent_gate_module(initiated_by="system", module="planning_module")
    assert result.decision == HookDecision.ASK


@pytest.mark.asyncio
async def test_schedule_consent_always_asks():
    result = await consent_gate_schedule(task_count=6, goal_count=2)
    assert result.decision == HookDecision.ASK
```

- [ ] **Step 2: Implement remaining hook handlers**

Add to `app/orchestrator/hooks.py`:

```python
# ── PreModuleExecution: Consent Gate (Layer 5) ──

async def consent_gate_module(initiated_by: str = "user", module: str = "", **kwargs: Any) -> HookResult:
    """Only gate system/PEARL-initiated actions. User-initiated auto-allows."""
    if initiated_by == "user":
        return HookResult(decision=HookDecision.ALLOW)
    return HookResult(
        decision=HookDecision.ASK,
        reason=f"I noticed something and want to run {module} — OK?",
    )


# ── PreScheduleModify: Draft Negotiation Gate ──

async def consent_gate_schedule(task_count: int = 0, goal_count: int = 1, **kwargs: Any) -> HookResult:
    """Always ask before modifying the schedule (this IS the negotiation UX)."""
    return HookResult(
        decision=HookDecision.ASK,
        reason=f"I'd like to schedule {task_count} tasks across {goal_count} goals. OK to proceed?",
    )


# ── PostModuleExecution: Telemetry (observe only) ──

async def post_module_telemetry(module: str = "", **kwargs: Any) -> HookResult:
    """Log module execution for telemetry. Never blocks."""
    return HookResult(decision=HookDecision.ALLOW)


# ── PreMemoryWrite: Optional review ──

async def memory_write_gate(**kwargs: Any) -> HookResult:
    """Optional — let user review what Jarvis remembers."""
    return HookResult(decision=HookDecision.ALLOW)


# ── CostThreshold: Token tracking ──

async def cost_threshold_check(token_count: int = 0, threshold: int = 5_000_000, **kwargs: Any) -> HookResult:
    """Notify if token usage exceeds threshold."""
    if token_count > threshold:
        return HookResult(
            decision=HookDecision.ASK,
            reason=f"This session has used {token_count:,} tokens. Continue?",
        )
    return HookResult(decision=HookDecision.ALLOW)


# ── ProactiveSuggestion: Phase 2+ ──

async def proactive_suggestion_gate(**kwargs: Any) -> HookResult:
    """Gate unsolicited advice. Phase 2+ implementation."""
    return HookResult(decision=HookDecision.ALLOW)


def register_all_hooks(hooks: ActionHooks) -> None:
    """Register all 7 hook handlers."""
    hooks.register("PreCloudLLM", pii_filter_hook)
    hooks.register("PreModuleExecution", consent_gate_module)
    hooks.register("PreScheduleModify", consent_gate_schedule)
    hooks.register("PostModuleExecution", post_module_telemetry)
    hooks.register("PreMemoryWrite", memory_write_gate)
    hooks.register("CostThreshold", cost_threshold_check)
    hooks.register("ProactiveSuggestion", proactive_suggestion_gate)
```

- [ ] **Step 3: Run tests, commit**

```bash
python -m pytest tests/test_hooks.py -v
git add app/orchestrator/hooks.py tests/test_hooks.py
git commit -m "feat: register all 7 hook handlers (consent, PII, cost, memory, proactive)"
```

---

### Task 17: Final Integration — Replace All Stubs + End-to-End Test

**Files:**
- Modify: `app/orchestrator/graph.py`
- Create: `tests/test_integration.py`

- [ ] **Step 1: Replace all remaining stubs in graph.py**

Update `app/orchestrator/graph.py` to import and use all real modules:

```python
from app.modules.conversation import run_general_chat, voice_of_jarvis_synthesis
from app.modules.coach import run_coaching_response
from app.modules.planning_graph import build_planning_graph
from app.modules.knowledge_graph import build_knowledge_graph
from app.modules.research_graph import build_research_graph
from app.core.observation import run_observation_loop
```

Replace all `_stub_*` registrations with the real functions. Keep `_stub_load_context`, `_stub_extract_brain_dump`, and `_stub_classify_intent` as they need the model router wired in (which depends on LM Studio running).

- [ ] **Step 2: Write integration test**

```python
# tests/test_integration.py
"""End-to-end integration tests for the LangGraph orchestrator."""
import pytest
from app.orchestrator.graph import build_jarvis_graph
from app.orchestrator.state import ConversationPhase, NegotiationPhase


def _make_initial_state(message: str = "hello"):
    return {
        "user_model": None,
        "user_message": message,
        "brain_dump": None,
        "intent": None,
        "initiated_by": "user",
        "execution_graph": None,
        "schedule": None,
        "draft_response": None,
        "research_results": None,
        "ingestion_result": None,
        "clarification_request": None,
        "thinking_process": None,
        "response_message": None,
        "conversation_phase": ConversationPhase.GREETING,
        "negotiation_state": NegotiationPhase.NONE,
        "modules_invoked": [],
        "needs_followup": False,
        "error": None,
    }


@pytest.mark.asyncio
async def test_chat_flow_end_to_end():
    graph = build_jarvis_graph()
    state = _make_initial_state("hello")
    result = await graph.ainvoke(state)
    assert result.get("response_message") is not None
    assert "conversation_module" in result.get("modules_invoked", [])


@pytest.mark.asyncio
async def test_graph_streams_events():
    graph = build_jarvis_graph()
    state = _make_initial_state("hello")
    config = {"configurable": {"thread_id": "test"}}
    events = []
    async for event in graph.astream(state, config):
        events.append(event)
    assert len(events) > 0
    # Should have at least: load_context, extract, classify, conversation, observe
    node_names = [list(e.keys())[0] for e in events]
    assert "observation_loop" in node_names
```

- [ ] **Step 3: Run integration tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 4: Run ALL tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/ -v --ignore=tests/test_scheduler.py --ignore=tests/test_chunker.py`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/orchestrator/graph.py tests/test_integration.py
git commit -m "feat: complete LangGraph orchestrator — all modules wired, stubs replaced"
```

---

## Summary

| Layer | Tasks | What's built | Est. time |
|---|---|---|---|
| **L1: User Model** | 1-3 | State types, UserModel facade, IntentType extensions | ~1 day |
| **L2: Orchestrator + Hooks** | 4-8 | LangGraph graph, routing, hooks, SSE endpoint, model router | ~2-3 days |
| **L3: Planning Module** | 9-10 | Planning sub-graph wrapping existing pipeline | ~1 day |
| **L4: Observation Loop** | 11 | Memory extraction + PEARL + constraint bridge | ~0.5 day |
| **L5: Remaining Modules** | 12-17 | Conversation, Coach, Knowledge, Research, all hooks, integration | ~2-3 days |

**Total: 17 tasks, ~7-8 days focused work.**

After completion:
- `/api/v1/chat/v2/stream` runs the LangGraph orchestrator (new)
- `/api/v1/chat/stream` still works (old, untouched)
- Switch over by pointing frontend to `/v2/stream` when ready
- Delete old endpoint + `control_policy.py` after validation
