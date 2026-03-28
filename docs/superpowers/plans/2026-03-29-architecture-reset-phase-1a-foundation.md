# Architecture Reset Phase 1A: Foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the core loop reliable — swap LLM routing to Gemini primary, add the BaseRegistry framework, migrate drafts to Supabase, wire up draft negotiation endpoints, and add integration tests for the full brain dump → schedule → draft pipeline.

**Architecture:** Invert the LLM routing (Gemini 2.5 Flash primary, Qwen-4B fallback) for schema-critical tasks. Replace the hardcoded intent routing in `control_policy.py` with a `BaseRegistry` framework. Migrate the in-memory `DraftStore` to Supabase persistence. Add the missing draft negotiation endpoints (edit task, rearrange, chat-modify). All changes are backward-compatible — existing `/chat` endpoint continues to work.

**Tech Stack:** FastAPI, Pydantic v2, Supabase (PostgreSQL), OR-Tools CP-SAT, LiteLLM, Gemini 2.5 Flash, Qwen-4B (LM Studio)

**Spec:** `docs/superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md`

**Prerequisite:** None (this is Phase 1A)

**Produces:** Working `/chat` endpoint with Gemini-primary routing, registry-based intent dispatch, Supabase-persisted drafts, and negotiation endpoints. All covered by integration tests.

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `app/core/registry.py` | BaseRegistry generic class — shared by all registries |
| Create | `app/services/intent_registry.py` | Intent registry instance + default intent registrations |
| Create | `tests/test_registry.py` | Unit tests for BaseRegistry |
| Create | `tests/test_intent_routing.py` | Integration tests for intent classification + dispatch |
| Create | `tests/test_draft_negotiation.py` | Integration tests for draft accept/edit/reject/chat-modify |
| Create | `tests/test_core_pipeline.py` | End-to-end: brain dump → schedule → draft |
| Modify | `app/models/brain/litellm_conf.py` | Invert routing: Gemini primary, Qwen fallback |
| Modify | `app/core/config.py` | Update GEMINI_MODEL default, add GEMINI_PRIMARY flag |
| Modify | `app/schemas/context.py` | Add `source` field to TimeSlot |
| Modify | `app/schemas/draft.py` | Add DraftSchedule, DraftTask, DraftAction schemas |
| Modify | `app/services/draft_store.py` | Migrate from in-memory to Supabase persistence |
| Modify | `app/api/v1/endpoints/drafts.py` | Add edit-task, rearrange, chat-modify endpoints |
| Modify | `app/services/analytical/control_policy.py` | Replace hardcoded intent routing with registry dispatch |
| Modify | `app/main.py` | Register default intents at startup |
| Modify | `tests/conftest.py` | Add shared fixtures (mock DB, mock LLM) |

---

### Task 1: BaseRegistry Framework

**Files:**
- Create: `app/core/registry.py`
- Create: `tests/test_registry.py`

- [ ] **Step 1: Write the failing test for BaseRegistry**

```python
# tests/test_registry.py
import pytest
from app.core.registry import BaseRegistry, RegistryEntry


async def _dummy_handler(**kwargs):
    return {"handled": True}


def test_register_and_get():
    registry = BaseRegistry(name="test", fallback_key="DEFAULT")
    entry = RegistryEntry(
        name="GREET",
        description="Handle greetings",
        handler=_dummy_handler,
        examples=["hello", "hi"],
    )
    registry.register(entry)
    result = registry.get("GREET")
    assert result is not None
    assert result.name == "GREET"
    assert result.handler is _dummy_handler


def test_get_returns_none_for_unknown():
    registry = BaseRegistry(name="test")
    assert registry.get("NONEXISTENT") is None


def test_get_or_fallback_returns_fallback():
    registry = BaseRegistry(name="test", fallback_key="DEFAULT")
    default_entry = RegistryEntry(
        name="DEFAULT",
        description="Default handler",
        handler=_dummy_handler,
    )
    registry.register(default_entry)
    result = registry.get_or_fallback("NONEXISTENT")
    assert result.name == "DEFAULT"


def test_get_or_fallback_raises_without_fallback():
    registry = BaseRegistry(name="test")
    with pytest.raises(KeyError, match="No entry"):
        registry.get_or_fallback("NONEXISTENT")


def test_classification_prompt_includes_all_entries():
    registry = BaseRegistry(name="test")
    registry.register(RegistryEntry(
        name="A", description="Do A", handler=_dummy_handler, examples=["do a"],
    ))
    registry.register(RegistryEntry(
        name="B", description="Do B", handler=_dummy_handler, examples=["do b"],
    ))
    prompt = registry.classification_prompt()
    assert "A: Do A" in prompt
    assert "B: Do B" in prompt
    assert "do a" in prompt
    assert "do b" in prompt


def test_registered_names():
    registry = BaseRegistry(name="test")
    registry.register(RegistryEntry(name="X", description="x", handler=_dummy_handler))
    registry.register(RegistryEntry(name="Y", description="y", handler=_dummy_handler))
    names = registry.registered_names()
    assert set(names) == {"X", "Y"}


def test_register_overwrites_existing():
    registry = BaseRegistry(name="test")
    handler_a = _dummy_handler

    async def handler_b(**kwargs):
        return {"new": True}

    registry.register(RegistryEntry(name="X", description="old", handler=handler_a))
    registry.register(RegistryEntry(name="X", description="new", handler=handler_b))
    assert registry.get("X").description == "new"
    assert registry.get("X").handler is handler_b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.registry'`

- [ ] **Step 3: Implement BaseRegistry**

```python
# app/core/registry.py
"""
Generic registry framework for Jarvis.

All extensible subsystems (intents, document types, memory types,
PEARL patterns) inherit from BaseRegistry. Adding a new capability
to ANY subsystem = defining a handler + registering it.

Inspired by Django's app registry and FastAPI's dependency injection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass
class RegistryEntry(Generic[T]):
    """A single entry in any registry."""

    name: str
    description: str
    handler: Callable[..., Awaitable[Any]]
    examples: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseRegistry(Generic[T]):
    """
    Generic registry. Provides:
    - Registration with validation
    - LLM classification prompt generation (auto-discovers registered types)
    - Handler lookup with fallback
    - Introspection
    """

    def __init__(self, name: str, fallback_key: str | None = None):
        self._name = name
        self._entries: dict[str, RegistryEntry[T]] = {}
        self._fallback_key = fallback_key

    def register(self, entry: RegistryEntry[T]) -> None:
        """Register a new entry. Re-registering overwrites."""
        if not entry.name or not entry.handler:
            raise ValueError("Registry entry must have name and handler")
        self._entries[entry.name] = entry

    def get(self, name: str) -> RegistryEntry[T] | None:
        """Look up an entry by name."""
        return self._entries.get(name)

    def get_or_fallback(self, name: str) -> RegistryEntry[T]:
        """Look up entry, fall back to default if not found."""
        entry = self._entries.get(name)
        if entry:
            return entry
        if self._fallback_key and self._fallback_key in self._entries:
            return self._entries[self._fallback_key]
        raise KeyError(
            f"No entry '{name}' in {self._name} registry and no fallback"
        )

    def classification_prompt(self) -> str:
        """Generate a classification prompt from all registered entries.

        The LLM sees this to decide which handler to route to.
        When you register a new type, the LLM automatically learns
        to classify it — no retraining needed.
        """
        lines = [f"Classify into one of these {self._name} types:\n"]
        for name, entry in self._entries.items():
            examples = ", ".join(entry.examples[:3]) if entry.examples else "N/A"
            lines.append(f"- {name}: {entry.description} (e.g., {examples})")
        if self._fallback_key:
            lines.append(f"\nIf none match clearly, use: {self._fallback_key}")
        return "\n".join(lines)

    def all_entries(self) -> dict[str, RegistryEntry[T]]:
        """List all registered entries."""
        return dict(self._entries)

    def registered_names(self) -> list[str]:
        """List all registered type names."""
        return list(self._entries.keys())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_registry.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/core/registry.py tests/test_registry.py
git commit -m "feat(core): add BaseRegistry framework for extensible subsystems"
```

---

### Task 2: Invert LLM Routing — Gemini Primary

**Files:**
- Modify: `app/core/config.py:28-30`
- Modify: `app/models/brain/litellm_conf.py:74-242`

- [ ] **Step 1: Update config defaults**

In `app/core/config.py`, change the GEMINI_MODEL default and add a routing flag:

```python
# app/core/config.py — replace lines 28-30 with:
GEMINI_API_KEY: str | None = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash")
GEMINI_PRIMARY: bool = os.getenv("GEMINI_PRIMARY", "true").lower() == "true"
```

- [ ] **Step 2: Update hybrid_route_query to use Gemini as primary**

In `app/models/brain/litellm_conf.py`, modify the `hybrid_route_query` function. The key change is: when `GEMINI_PRIMARY` is `True` and `force_cloud` is not explicitly `False`, use Gemini for schema-critical tasks and fall back to local on failure.

Add at the top of `hybrid_route_query` (after the existing function signature on line 74), add a `prefer_local` parameter:

```python
# In hybrid_route_query signature (line 74), add parameter:
# prefer_local: bool = False,

# After the existing CLOUD_KEYWORDS check (around line 100), add:
    # Gemini-primary routing: use cloud for schema-critical tasks
    if (
        GEMINI_PRIMARY
        and not prefer_local
        and not force_cloud
        and GEMINI_API_KEY
        and response_schema is not None  # Schema-critical = needs reliable JSON
    ):
        force_cloud = True  # Route to Gemini for structured output
```

Add rate limit fallback after the existing Gemini call (around line 180, inside the cloud response handling):

```python
    # After the existing cloud call, wrap in try/except for rate limits:
    except Exception as cloud_err:
        if "rate" in str(cloud_err).lower() or "429" in str(cloud_err):
            logger.warning("Gemini rate limit hit — falling back to local")
        else:
            logger.warning(f"Gemini error: {cloud_err} — falling back to local")
        # Fall through to local model below
```

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/ -v`
Expected: Existing tests still pass (scheduler tests don't touch LLM routing)

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/core/config.py app/models/brain/litellm_conf.py
git commit -m "feat(llm): invert routing — Gemini 2.5 Flash primary, Qwen-4B fallback"
```

---

### Task 3: Add `source` Field to TimeSlot Schema

**Files:**
- Modify: `app/schemas/context.py:119-137`

- [ ] **Step 1: Add source field to TimeSlot**

In `app/schemas/context.py`, add the `source` field to the `TimeSlot` class (around line 137, before the closing of the class):

```python
# app/schemas/context.py — add to TimeSlot class (after max_difficulty field):
    source: str = Field(
        default="user",
        description="Origin of this constraint: user, habit, pearl_inferred, calendar",
    )
```

- [ ] **Step 2: Run existing tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/ -v`
Expected: PASS — default value makes this backward-compatible

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/schemas/context.py
git commit -m "feat(schema): add source field to TimeSlot for constraint provenance"
```

---

### Task 4: Draft Schemas — DraftSchedule, DraftTask, DraftAction

**Files:**
- Modify: `app/schemas/draft.py`

- [ ] **Step 1: Add new draft schemas**

Replace the contents of `app/schemas/draft.py` with the existing schemas PLUS the new ones:

```python
# app/schemas/draft.py
"""Draft schemas for schedule negotiation UX."""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


# ── Existing schemas (keep these) ──────────────────────────

class DraftComponentResponse(BaseModel):
    component_type: str = Field(description="Type of component")
    data: Any = Field(description="Component data")
    status: str = Field(default="pending", description="pending|accepted|rejected")


class DraftResponse(BaseModel):
    draft_id: str
    user_id: str
    components: dict[str, DraftComponentResponse] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DraftAcceptRequest(BaseModel):
    user_id: str
    components: Optional[List[str]] = Field(
        default=None, description="Component keys to accept. None = accept all."
    )


class DraftRejectRequest(BaseModel):
    user_id: str
    components: List[str] = Field(description="Component keys to reject")


class DraftModifyRequest(BaseModel):
    user_id: str
    component: str = Field(description="Component key to modify")
    data: Any = Field(description="New component data")


# ── New schemas for draft negotiation ──────────────────────

class DraftTask(BaseModel):
    """A single task within a draft schedule."""

    task_id: str
    title: str
    start_min: int = Field(description="Start time in minutes from horizon_start")
    duration_minutes: int = Field(le=25, description="Max 25 min per task chunk")
    difficulty_weight: float = Field(ge=0, le=1)
    completion_criteria: str
    goal_id: str | None = None
    dependencies: list[str] = Field(default_factory=list)


class DraftSchedule(BaseModel):
    """A proposed schedule awaiting user review."""

    draft_id: str
    user_id: str
    goal_id: str | None = None
    tasks: list[DraftTask]
    horizon_start: datetime
    status: Literal["pending", "accepted", "rejected", "modified", "expired"] = "pending"
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DraftTaskEdit(BaseModel):
    """User edit to a single task in a draft."""

    title: str | None = None
    duration_minutes: int | None = Field(default=None, le=60)
    difficulty_weight: float | None = Field(default=None, ge=0, le=1)
    start_min: int | None = None


class DraftAction(BaseModel):
    """User action on a draft."""

    action: Literal["accept_all", "reject", "edit_task", "rearrange", "chat_modify"]
    draft_id: str
    task_id: str | None = None
    edits: DraftTaskEdit | None = None
    reason: str | None = None
    modification_request: str | None = None
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.schemas.draft import DraftSchedule, DraftTask, DraftAction; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/schemas/draft.py
git commit -m "feat(schema): add DraftSchedule, DraftTask, DraftAction for negotiation UX"
```

---

### Task 5: Migrate DraftStore from In-Memory to Supabase

**Files:**
- Modify: `app/services/draft_store.py`
- Create: `tests/test_draft_store.py`

- [ ] **Step 1: Write failing test for Supabase-based DraftStore**

```python
# tests/test_draft_store.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.draft_store import DraftStore
from app.schemas.draft import DraftTask


@pytest.fixture
def mock_supabase():
    """Mock Supabase client for draft store tests."""
    client = MagicMock()
    # Chain mock: client.table("draft_schedules").insert(...).execute()
    table = MagicMock()
    client.table.return_value = table
    execute_result = MagicMock()
    execute_result.data = [{"id": "test-draft-id"}]
    table.insert.return_value.execute.return_value = execute_result
    table.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = execute_result
    table.update.return_value.eq.return_value.execute.return_value = execute_result
    return client


@pytest.fixture
def store(mock_supabase):
    return DraftStore(supabase_client=mock_supabase)


def test_create_draft(store, mock_supabase):
    tasks = [
        DraftTask(
            task_id="t1",
            title="Study CNNs",
            start_min=480,
            duration_minutes=25,
            difficulty_weight=0.5,
            completion_criteria="Explain convolution operation",
        )
    ]
    draft = store.create_draft(
        user_id="user-1",
        tasks=tasks,
        horizon_start="2026-03-29T08:00:00Z",
    )
    assert draft is not None
    mock_supabase.table.assert_called_with("draft_schedules")


def test_get_draft(store, mock_supabase):
    mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {
            "id": "d1",
            "user_id": "user-1",
            "tasks": [{"task_id": "t1", "title": "X", "start_min": 0, "duration_minutes": 25, "difficulty_weight": 0.5, "completion_criteria": "Y"}],
            "horizon_start": "2026-03-29T08:00:00Z",
            "status": "pending",
        }
    ]
    result = store.get_draft("d1", "user-1")
    assert result is not None
    assert result["status"] == "pending"


def test_accept_draft(store, mock_supabase):
    store.accept_draft("d1", "user-1")
    mock_supabase.table.return_value.update.assert_called()


def test_reject_draft(store, mock_supabase):
    store.reject_draft("d1", "user-1", reason="Too cramped")
    mock_supabase.table.return_value.update.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_draft_store.py -v`
Expected: FAIL — DraftStore doesn't accept `supabase_client` parameter yet

- [ ] **Step 3: Rewrite DraftStore with Supabase persistence**

```python
# app/services/draft_store.py
"""Draft persistence layer — Supabase-backed, survives server restarts."""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import SUPABASE_SERVICE_KEY, SUPABASE_URL


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


class DraftStore:
    """Persists draft schedules in Supabase.

    Lifecycle:
    - create_draft() → status='pending'
    - accept_draft() → status='accepted', tasks persisted to user_tasks
    - reject_draft() → status='rejected', reason stored
    - edit_task_in_draft() → modifies task in JSONB, status='modified'
    - Drafts expire after 24 hours (cleanup on access)
    """

    def __init__(self, supabase_client=None):
        self._supabase = supabase_client or _get_supabase()

    def create_draft(
        self,
        user_id: str,
        tasks: list,
        horizon_start: str,
        goal_id: str | None = None,
    ) -> dict | None:
        """Create a new draft schedule. Returns the draft record."""
        if not self._supabase:
            return None

        draft_id = str(uuid.uuid4())
        tasks_json = [t.model_dump() if hasattr(t, "model_dump") else t for t in tasks]

        result = self._supabase.table("draft_schedules").insert({
            "id": draft_id,
            "user_id": user_id,
            "goal_id": goal_id,
            "tasks": tasks_json,
            "horizon_start": horizon_start,
            "status": "pending",
        }).execute()

        if result.data:
            return result.data[0]
        return None

    def get_draft(self, draft_id: str, user_id: str) -> dict | None:
        """Get a draft by ID. Returns None if not found or wrong user."""
        if not self._supabase:
            return None

        result = (
            self._supabase.table("draft_schedules")
            .select("*")
            .eq("id", draft_id)
            .eq("user_id", user_id)
            .execute()
        )
        if result.data:
            return result.data[0]
        return None

    def get_pending_draft(self, user_id: str) -> dict | None:
        """Get the most recent pending draft for a user."""
        if not self._supabase:
            return None

        result = (
            self._supabase.table("draft_schedules")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]
        return None

    def accept_draft(self, draft_id: str, user_id: str) -> bool:
        """Accept a draft — marks as accepted."""
        if not self._supabase:
            return False

        self._supabase.table("draft_schedules").update({
            "status": "accepted",
        }).eq("id", draft_id).eq("user_id", user_id).execute()
        return True

    def reject_draft(
        self, draft_id: str, user_id: str, reason: str | None = None
    ) -> bool:
        """Reject a draft — marks as rejected with optional reason."""
        if not self._supabase:
            return False

        self._supabase.table("draft_schedules").update({
            "status": "rejected",
            "rejection_reason": reason,
        }).eq("id", draft_id).eq("user_id", user_id).execute()
        return True

    def edit_task_in_draft(
        self, draft_id: str, user_id: str, task_id: str, edits: dict
    ) -> dict | None:
        """Edit a single task in a draft's JSONB tasks array.

        Returns the updated draft or None on failure.
        """
        draft = self.get_draft(draft_id, user_id)
        if not draft:
            return None

        tasks = draft.get("tasks", [])
        updated = False
        for task in tasks:
            if task.get("task_id") == task_id:
                for key, value in edits.items():
                    if value is not None:
                        task[key] = value
                updated = True
                break

        if not updated:
            return None

        self._supabase.table("draft_schedules").update({
            "tasks": tasks,
            "status": "modified",
        }).eq("id", draft_id).eq("user_id", user_id).execute()

        return self.get_draft(draft_id, user_id)

    def delete_draft(self, draft_id: str, user_id: str) -> bool:
        """Delete a draft."""
        if not self._supabase:
            return False

        self._supabase.table("draft_schedules").delete().eq(
            "id", draft_id
        ).eq("user_id", user_id).execute()
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_draft_store.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/draft_store.py tests/test_draft_store.py
git commit -m "feat(drafts): migrate DraftStore from in-memory to Supabase persistence"
```

---

### Task 6: Intent Registry — Register Default Intents

**Files:**
- Create: `app/services/intent_registry.py`
- Create: `tests/test_intent_routing.py`

- [ ] **Step 1: Write failing test for intent registry**

```python
# tests/test_intent_routing.py
import pytest
from app.services.intent_registry import intent_registry, register_default_intents


def test_default_intents_registered():
    register_default_intents()
    names = intent_registry.registered_names()
    assert "PLAN_DAY" in names
    assert "EDIT_TASK" in names
    assert "CHAT" in names
    assert "ACCEPT_DRAFT" in names
    assert "REJECT_DRAFT" in names
    assert "INGEST_DOCUMENT" in names
    assert "CHECK_PROGRESS" in names
    assert "ADD_CONSTRAINT" in names
    assert "REARRANGE" in names


def test_chat_is_fallback():
    register_default_intents()
    result = intent_registry.get_or_fallback("NONEXISTENT_INTENT")
    assert result.name == "CHAT"


def test_classification_prompt_generated():
    register_default_intents()
    prompt = intent_registry.classification_prompt()
    assert "PLAN_DAY" in prompt
    assert "plan my day" in prompt.lower() or "schedule" in prompt.lower()
    assert "CHAT" in prompt


def test_plan_day_has_handler():
    register_default_intents()
    entry = intent_registry.get("PLAN_DAY")
    assert entry is not None
    assert callable(entry.handler)
    assert entry.metadata.get("triggers_replan") is False


def test_edit_task_triggers_replan():
    register_default_intents()
    entry = intent_registry.get("EDIT_TASK")
    assert entry is not None
    assert entry.metadata.get("triggers_replan") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_intent_routing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.intent_registry'`

- [ ] **Step 3: Implement intent registry with default registrations**

```python
# app/services/intent_registry.py
"""Intent registry — extensible intent routing for /chat endpoint.

Uses BaseRegistry from app/core/registry.py. Adding a new intent
requires only a handler function + a register() call.
"""

from app.core.registry import BaseRegistry, RegistryEntry

# The global intent registry instance
intent_registry = BaseRegistry[dict](
    name="intent",
    fallback_key="CHAT",
)


# ── Placeholder handlers (will be wired to real logic in control_policy) ──

async def _handle_plan_day(user_id: str, message: str, context: dict) -> dict:
    """Placeholder — wired to _run_plan_day_flow in control_policy.py."""
    return {"intent": "PLAN_DAY", "status": "not_wired"}


async def _handle_edit_task(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "EDIT_TASK", "status": "not_wired"}


async def _handle_rearrange(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "REARRANGE", "status": "not_wired"}


async def _handle_add_constraint(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "ADD_CONSTRAINT", "status": "not_wired"}


async def _handle_accept_draft(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "ACCEPT_DRAFT", "status": "not_wired"}


async def _handle_reject_draft(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "REJECT_DRAFT", "status": "not_wired"}


async def _handle_ingest_document(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "INGEST_DOCUMENT", "status": "not_wired"}


async def _handle_check_progress(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "CHECK_PROGRESS", "status": "not_wired"}


async def _handle_chat(user_id: str, message: str, context: dict) -> dict:
    return {"intent": "CHAT", "status": "not_wired"}


def register_default_intents() -> None:
    """Register all built-in intents. Called during app lifespan startup."""

    intent_registry.register(RegistryEntry(
        name="PLAN_DAY",
        description="User wants to plan their day, schedule tasks, or organize their workload",
        handler=_handle_plan_day,
        examples=["plan my day", "schedule my week", "I need to study for 3 exams"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))

    intent_registry.register(RegistryEntry(
        name="EDIT_TASK",
        description="User wants to modify an existing task (duration, title, deadline, priority)",
        handler=_handle_edit_task,
        examples=["change that to 15 minutes", "rename the first task", "make it easier"],
        metadata={"requires_draft": False, "triggers_replan": True},
    ))

    intent_registry.register(RegistryEntry(
        name="REARRANGE",
        description="User wants to swap task order or move a task to a different time",
        handler=_handle_rearrange,
        examples=["move DSA to afternoon", "swap tasks 2 and 3", "do math first"],
        metadata={"requires_draft": True, "triggers_replan": True},
    ))

    intent_registry.register(RegistryEntry(
        name="ADD_CONSTRAINT",
        description="User is adding a scheduling constraint, habit, or time block",
        handler=_handle_add_constraint,
        examples=["no work after 6 PM", "I have a meeting at 2", "I sleep at midnight"],
        metadata={"requires_draft": False, "triggers_replan": True},
    ))

    intent_registry.register(RegistryEntry(
        name="ACCEPT_DRAFT",
        description="User accepts the proposed schedule or draft",
        handler=_handle_accept_draft,
        examples=["looks good", "accept", "yes go with this", "perfect"],
        metadata={"requires_draft": True, "triggers_replan": False},
    ))

    intent_registry.register(RegistryEntry(
        name="REJECT_DRAFT",
        description="User rejects the proposed schedule and wants something different",
        handler=_handle_reject_draft,
        examples=["no that doesn't work", "reject", "start over", "this is wrong"],
        metadata={"requires_draft": True, "triggers_replan": False},
    ))

    intent_registry.register(RegistryEntry(
        name="INGEST_DOCUMENT",
        description="User is uploading a document, PDF, or file for processing",
        handler=_handle_ingest_document,
        examples=["process this PDF", "here's my syllabus", "upload this document"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))

    intent_registry.register(RegistryEntry(
        name="CHECK_PROGRESS",
        description="User wants to check their progress, completed tasks, or status",
        handler=_handle_check_progress,
        examples=["how am I doing", "what did I finish", "show my progress"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))

    intent_registry.register(RegistryEntry(
        name="CHAT",
        description="General conversation, questions, or anything that doesn't fit other intents",
        handler=_handle_chat,
        examples=["hello", "what can you do", "tell me about yourself"],
        metadata={"requires_draft": False, "triggers_replan": False},
    ))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_intent_routing.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/intent_registry.py tests/test_intent_routing.py
git commit -m "feat(intents): add intent registry with 9 default intent registrations"
```

---

### Task 7: Wire Intent Registry into App Startup

**Files:**
- Modify: `app/main.py:22-109`

- [ ] **Step 1: Add intent registration to lifespan**

In `app/main.py`, inside the `lifespan` function (after the DraftStore initialization, around line 50), add:

```python
    # Register default intents
    from app.services.intent_registry import register_default_intents
    register_default_intents()
    logger.info("Intent registry: %d intents registered", len(intent_registry.registered_names()))
```

Also add the import at the top of main.py:

```python
from app.services.intent_registry import intent_registry
```

- [ ] **Step 2: Verify app starts**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.main import app; print('App created OK')"`
Expected: `App created OK` (may show warnings about missing env vars, that's fine)

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/main.py
git commit -m "feat(startup): register default intents during app lifespan"
```

---

### Task 8: Draft Negotiation Endpoints — Edit Task and Rearrange

**Files:**
- Modify: `app/api/v1/endpoints/drafts.py`

- [ ] **Step 1: Add edit-task endpoint**

Add to `app/api/v1/endpoints/drafts.py` after the existing `modify_draft` endpoint:

```python
from fastapi import Query
from app.schemas.draft import DraftTaskEdit


@router.patch("/{draft_id}/tasks/{task_id}")
async def edit_draft_task(
    draft_id: str,
    task_id: str,
    edits: DraftTaskEdit,
    user_id: str = Query(..., description="User ID for authorization"),
    request: Request = None,
):
    """Edit a single task in a draft. Re-solve is NOT automatic — caller triggers replan."""
    draft_store = _get_draft_store(request)
    if not draft_store:
        raise HTTPException(status_code=503, detail="Draft store unavailable")

    edits_dict = edits.model_dump(exclude_none=True)
    if not edits_dict:
        raise HTTPException(status_code=400, detail="No edits provided")

    result = draft_store.edit_task_in_draft(draft_id, user_id, task_id, edits_dict)
    if result is None:
        raise HTTPException(status_code=404, detail="Draft or task not found")

    return {"status": "modified", "draft_id": draft_id, "task_id": task_id, "updated_draft": result}
```

- [ ] **Step 2: Update _get_draft_store to use new DraftStore**

Verify that `_get_draft_store` returns the Supabase-backed store. The existing code at lines 19-23 gets it from `app.state.draft_store` — this should work as long as `main.py` lifespan creates the new `DraftStore`. Update `main.py` lifespan to use the new constructor:

In `app/main.py`, find where `DraftStore` is instantiated (around line 40-45) and ensure it passes the Supabase client:

```python
    # Replace existing DraftStore creation with:
    from app.services.draft_store import DraftStore
    draft_store = DraftStore(supabase_client=db_client.supabase if hasattr(db_client, 'supabase') else None)
    app.state.draft_store = draft_store
```

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/api/v1/endpoints/drafts.py app/main.py
git commit -m "feat(drafts): add edit-task endpoint, wire Supabase-backed DraftStore"
```

---

### Task 9: Test Fixtures — Mock DB and Mock LLM

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add shared test fixtures**

```python
# tests/conftest.py
"""Shared test fixtures for Jarvis Engine tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_supabase():
    """Mock Supabase client that returns empty results by default."""
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table

    # Default: empty query results
    empty_result = MagicMock()
    empty_result.data = []

    # Chain all query methods to return empty by default
    for method in ["select", "insert", "update", "delete", "upsert"]:
        chained = getattr(table, method).return_value
        chained.execute.return_value = empty_result
        chained.eq.return_value = chained
        chained.neq.return_value = chained
        chained.gt.return_value = chained
        chained.lt.return_value = chained
        chained.is_.return_value = chained
        chained.order.return_value = chained
        chained.limit.return_value = chained

    return client


@pytest.fixture
def mock_llm_response():
    """Factory for mocking hybrid_route_query responses."""
    def _make(response_data):
        async def _mock_route(*args, **kwargs):
            schema = kwargs.get("response_schema")
            if schema and isinstance(response_data, dict):
                return schema.model_validate(response_data)
            return response_data
        return _mock_route
    return _make


@pytest.fixture
def sample_tasks():
    """Sample TaskChunk data for testing."""
    return [
        {
            "task_id": "goal1_t1",
            "title": "Study CNNs - convolution layers",
            "duration_minutes": 25,
            "difficulty_weight": 0.5,
            "dependencies": [],
            "completion_criteria": "Explain convolution operation",
            "deadline_hint": None,
        },
        {
            "task_id": "goal1_t2",
            "title": "Study backpropagation math",
            "duration_minutes": 25,
            "difficulty_weight": 0.7,
            "dependencies": ["goal1_t1"],
            "completion_criteria": "Derive backprop gradient",
            "deadline_hint": None,
        },
        {
            "task_id": "goal1_t3",
            "title": "Practice: implement basic neural network",
            "duration_minutes": 25,
            "difficulty_weight": 0.6,
            "dependencies": ["goal1_t2"],
            "completion_criteria": "Working forward pass in Python",
            "deadline_hint": None,
        },
    ]
```

- [ ] **Step 2: Verify fixtures load**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/conftest.py --collect-only`
Expected: Shows fixture collection without errors

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add tests/conftest.py
git commit -m "test: add shared fixtures — mock Supabase, mock LLM, sample tasks"
```

---

### Task 10: Integration Test — Core Pipeline

**Files:**
- Create: `tests/test_core_pipeline.py`

- [ ] **Step 1: Write integration tests**

```python
# tests/test_core_pipeline.py
"""Integration tests for the core pipeline: brain dump → schedule → draft."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.core.registry import BaseRegistry, RegistryEntry
from app.services.intent_registry import intent_registry, register_default_intents


class TestIntentClassification:
    """Test that the intent registry generates valid classification prompts."""

    def setup_method(self):
        register_default_intents()

    def test_classification_prompt_is_valid(self):
        prompt = intent_registry.classification_prompt()
        assert len(prompt) > 100
        assert "PLAN_DAY" in prompt
        assert "CHAT" in prompt

    def test_all_intents_have_handlers(self):
        for name in intent_registry.registered_names():
            entry = intent_registry.get(name)
            assert entry is not None, f"Missing entry for {name}"
            assert callable(entry.handler), f"Handler for {name} is not callable"

    def test_all_intents_have_examples(self):
        for name in intent_registry.registered_names():
            entry = intent_registry.get(name)
            assert len(entry.examples) > 0, f"No examples for {name}"

    def test_fallback_to_chat(self):
        result = intent_registry.get_or_fallback("BANANA")
        assert result.name == "CHAT"


class TestDraftStore:
    """Test DraftStore with mock Supabase."""

    def test_create_and_get_draft(self, mock_supabase):
        from app.services.draft_store import DraftStore
        from app.schemas.draft import DraftTask

        mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [
            {"id": "d1", "user_id": "u1", "status": "pending"}
        ]

        store = DraftStore(supabase_client=mock_supabase)
        tasks = [DraftTask(
            task_id="t1", title="Test", start_min=0,
            duration_minutes=25, difficulty_weight=0.5,
            completion_criteria="Done",
        )]
        result = store.create_draft("u1", tasks, "2026-03-29T08:00:00Z")
        assert result is not None

    def test_edit_task_in_draft(self, mock_supabase):
        from app.services.draft_store import DraftStore

        # Setup: get_draft returns a draft with tasks
        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {
                "id": "d1",
                "user_id": "u1",
                "tasks": [
                    {"task_id": "t1", "title": "Original", "start_min": 0,
                     "duration_minutes": 25, "difficulty_weight": 0.5,
                     "completion_criteria": "Done"},
                ],
                "status": "pending",
            }
        ]

        store = DraftStore(supabase_client=mock_supabase)
        result = store.edit_task_in_draft("d1", "u1", "t1", {"title": "Edited"})
        assert result is not None

    def test_reject_draft(self, mock_supabase):
        from app.services.draft_store import DraftStore

        store = DraftStore(supabase_client=mock_supabase)
        result = store.reject_draft("d1", "u1", reason="Too much work")
        assert result is True


class TestTimeSlotSchema:
    """Test that TimeSlot now has source field."""

    def test_timeslot_has_source_default(self):
        from app.schemas.context import TimeSlot
        slot = TimeSlot(name="test", start_min=0, end_min=60, availability="blocked")
        assert slot.source == "user"

    def test_timeslot_accepts_pearl_source(self):
        from app.schemas.context import TimeSlot
        slot = TimeSlot(
            name="inferred", start_min=0, end_min=60,
            availability="minimal_work", source="pearl_inferred"
        )
        assert slot.source == "pearl_inferred"
```

- [ ] **Step 2: Run integration tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_core_pipeline.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/ -v`
Expected: All tests PASS (including existing scheduler and chunker tests)

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add tests/test_core_pipeline.py
git commit -m "test: add integration tests for core pipeline — registry, drafts, schemas"
```

---

## Phase 1A Complete Checklist

After completing all 10 tasks, verify:

- [ ] `BaseRegistry` framework works with full test coverage
- [ ] LLM routing defaults to Gemini 2.5 Flash for schema-critical tasks
- [ ] `TimeSlot` has `source` field (backward-compatible)
- [ ] Draft schemas support negotiation (DraftSchedule, DraftTask, DraftAction)
- [ ] `DraftStore` persists to Supabase (not in-memory)
- [ ] Intent registry has 9 default intents with placeholder handlers
- [ ] Intents are registered at app startup
- [ ] Draft edit-task endpoint works
- [ ] Test fixtures exist for mock Supabase and mock LLM
- [ ] All tests pass: `python -m pytest tests/ -v`

**Next phase:** Phase 1B (Memory & Context) — depends on the database tables and registry framework built here.
