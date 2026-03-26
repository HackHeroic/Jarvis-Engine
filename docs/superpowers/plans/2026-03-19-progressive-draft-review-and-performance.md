# Progressive Draft Review & Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce perceived response time from 125s to <5s (progressive rendering) and ensure ALL pipeline outputs (habits, tasks, schedule, materials) go through a user review/accept/reject flow before being persisted.

**Architecture:** Introduce an in-memory `DraftStore` that holds the complete pipeline output keyed by `draft_id`. SSE phase events are enhanced to carry structured data (not just progress text), enabling the frontend to render incrementally. Nothing persists to Supabase until the user explicitly accepts via `POST /api/v1/drafts/{draft_id}/accept`. Voice of Jarvis synthesis is made optional for PLAN_DAY (frontend renders structured schedule directly, saving one LLM call).

**Tech Stack:** FastAPI (async), Pydantic v2, existing OR-Tools solver, existing SSE streaming, existing LiteLLM routing. No new dependencies.

---

## Current State & Problem Summary

### Performance (125.8s response, 23.3s TTFT)
- **4-5 sequential LLM calls** per PLAN_DAY: brain dump extraction (4B) → habit translation (27B, ~30-60s) → decomposition (27B, ~60-90s) → Voice of Jarvis (4B, ~5-10s)
- User stares at blank screen for full duration
- SSE streaming exists (`POST /chat/stream`) but only streams VoJ tokens at the end — structured data (tasks, schedule) arrives in a single `complete` event

### Review UX (inconsistent)
- **Schedule**: Draft/accept flow exists and works (`schedule_status="draft"`, `POST /chat/accept-schedule`)
- **Calendar**: Pending/approve/reject flow exists (`pending_calendar_updates` table)
- **Habits**: Auto-committed immediately to `behavioral_constraints` — NO review
- **Action Items**: Proposed in ChatResponse but NEVER persisted — no accept/reject
- **Material Links**: Auto-committed to `task_materials` — NO review

### What Already Works (keep and extend)
- `POST /chat/stream` — SSE with `phase` events + `complete` event
- `POST /chat/confirm-schedule` — Phase 2 scheduling from user-edited tasks
- `POST /chat/accept-schedule` — Persist draft tasks to Supabase
- `POST /chat/modify-schedule` — Natural language schedule edits
- `confirm_before_schedule` flag in ChatRequest
- `awaiting_task_confirmation` and `schedule_status` in ChatResponse
- `progress_callback` mechanism in control_policy.py
- Decomposition cache (SHA256, 1hr TTL)
- Habit translation cache (SHA256, 1hr TTL)
- Python-first regex patterns in habit_translator.py

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `app/services/draft_store.py` | In-memory draft store with TTL. Holds pipeline output keyed by draft_id. |
| `app/schemas/draft.py` | Draft, DraftComponent, DraftAcceptRequest schemas |
| `app/api/v1/endpoints/drafts.py` | CRUD endpoints for draft review/accept/reject |
| `tests/test_draft_store.py` | Unit tests for draft store |
| `tests/test_draft_endpoints.py` | Integration tests for draft API |

### Modified Files
| File | Changes |
|------|---------|
| `app/schemas/context.py:211-254` | Add `draft_id` field to ChatResponse |
| `app/services/analytical/control_policy.py:1129-1139` | Defer habit persistence — store in draft, not DB |
| `app/services/analytical/control_policy.py:532-875` | Return draft_id in _run_plan_day_flow, skip VoJ when structured |
| `app/api/v1/endpoints/chat.py:148-200` | Emit structured data in SSE phase events |
| `app/api/v1/router.py` | Mount drafts router |
| `app/services/extraction/behavioral_store.py` | Add `stage_habit()` (returns proposal, no DB write) |
| `app/core/config.py` | Add DRAFT_TTL_SECONDS constant |

---

## Task 1: Draft Store Service

**Files:**
- Create: `app/services/draft_store.py`
- Create: `tests/test_draft_store.py`

- [ ] **Step 1: Write failing tests for draft store**

```python
# tests/test_draft_store.py
import time
import pytest
from app.services.draft_store import DraftStore, Draft, DraftComponent


def test_create_and_get_draft():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    assert draft.draft_id
    assert draft.user_id == "user_123"
    assert draft.components == {}

    retrieved = store.get(draft.draft_id, "user_123")
    assert retrieved is not None
    assert retrieved.draft_id == draft.draft_id


def test_get_draft_wrong_user():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    assert store.get(draft.draft_id, "user_456") is None


def test_add_component():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    store.add_component(
        draft.draft_id,
        "user_123",
        "habits",
        DraftComponent(
            component_type="habits",
            data=[{"raw_text": "no work before 11 AM"}],
            status="pending",
        ),
    )
    updated = store.get(draft.draft_id, "user_123")
    assert "habits" in updated.components
    assert updated.components["habits"].status == "pending"


def test_accept_component():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    store.add_component(
        draft.draft_id,
        "user_123",
        "habits",
        DraftComponent(component_type="habits", data=[], status="pending"),
    )
    store.accept_component(draft.draft_id, "user_123", "habits")
    updated = store.get(draft.draft_id, "user_123")
    assert updated.components["habits"].status == "accepted"


def test_reject_component():
    store = DraftStore(ttl_seconds=60)
    draft = store.create("user_123")
    store.add_component(
        draft.draft_id,
        "user_123",
        "habits",
        DraftComponent(component_type="habits", data=[], status="pending"),
    )
    store.reject_component(draft.draft_id, "user_123", "habits")
    updated = store.get(draft.draft_id, "user_123")
    assert updated.components["habits"].status == "rejected"


def test_draft_expires():
    store = DraftStore(ttl_seconds=0)  # Immediate expiry
    draft = store.create("user_123")
    time.sleep(0.01)
    assert store.get(draft.draft_id, "user_123") is None


def test_cleanup_expired():
    store = DraftStore(ttl_seconds=0)
    store.create("user_123")
    store.create("user_123")
    time.sleep(0.01)
    removed = store.cleanup_expired()
    assert removed >= 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_draft_store.py -v`
Expected: FAIL with ImportError (module doesn't exist yet)

- [ ] **Step 3: Implement DraftStore**

```python
# app/services/draft_store.py
"""In-memory draft store with TTL for pipeline output review."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DraftComponent:
    """A single reviewable component within a draft."""

    component_type: str  # "habits", "tasks", "schedule", "action_items", "materials"
    data: Any  # Component-specific data (list of habits, list of tasks, etc.)
    status: str = "pending"  # "pending", "accepted", "rejected", "modified"


@dataclass
class Draft:
    """Complete draft holding all pipeline output for user review."""

    draft_id: str
    user_id: str
    created_at: float  # time.time()
    components: dict[str, DraftComponent] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class DraftStore:
    """Asyncio-safe in-memory store for drafts with TTL-based expiry.

    Safe under single-threaded asyncio event loop (no locking needed).
    Drafts are keyed by draft_id. Every access checks user_id to
    prevent cross-user data leaks (IDOR protection).
    """

    def __init__(self, ttl_seconds: int = 1800):
        self._store: dict[str, Draft] = {}
        self._ttl = ttl_seconds

    def create(self, user_id: str, metadata: dict | None = None) -> Draft:
        draft_id = str(uuid.uuid4())
        draft = Draft(
            draft_id=draft_id,
            user_id=user_id,
            created_at=time.time(),
            metadata=metadata or {},
        )
        self._store[draft_id] = draft
        return draft

    def get(self, draft_id: str, user_id: str) -> Optional[Draft]:
        draft = self._store.get(draft_id)
        if draft is None:
            return None
        if draft.user_id != user_id:
            return None
        if time.time() - draft.created_at > self._ttl:
            del self._store[draft_id]
            return None
        return draft

    def add_component(
        self,
        draft_id: str,
        user_id: str,
        component_key: str,
        component: DraftComponent,
    ) -> bool:
        draft = self.get(draft_id, user_id)
        if draft is None:
            return False
        draft.components[component_key] = component
        return True

    def update_component_data(
        self,
        draft_id: str,
        user_id: str,
        component_key: str,
        data: Any,
    ) -> bool:
        draft = self.get(draft_id, user_id)
        if draft is None or component_key not in draft.components:
            return False
        draft.components[component_key].data = data
        draft.components[component_key].status = "modified"
        return True

    def accept_component(
        self, draft_id: str, user_id: str, component_key: str
    ) -> bool:
        draft = self.get(draft_id, user_id)
        if draft is None or component_key not in draft.components:
            return False
        draft.components[component_key].status = "accepted"
        return True

    def reject_component(
        self, draft_id: str, user_id: str, component_key: str
    ) -> bool:
        draft = self.get(draft_id, user_id)
        if draft is None or component_key not in draft.components:
            return False
        draft.components[component_key].status = "rejected"
        return True

    def accept_all(self, draft_id: str, user_id: str) -> bool:
        draft = self.get(draft_id, user_id)
        if draft is None:
            return False
        for comp in draft.components.values():
            if comp.status == "pending":
                comp.status = "accepted"
        return True

    def delete(self, draft_id: str, user_id: str) -> bool:
        draft = self._store.get(draft_id)
        if draft is None or draft.user_id != user_id:
            return False
        del self._store[draft_id]
        return True

    def cleanup_expired(self) -> int:
        now = time.time()
        expired = [
            did for did, d in self._store.items()
            if now - d.created_at > self._ttl
        ]
        for did in expired:
            del self._store[did]
        return len(expired)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_draft_store.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/draft_store.py tests/test_draft_store.py
git commit -m "feat: add in-memory DraftStore for pipeline output review"
```

---

## Task 2: Draft Schemas & ChatResponse Update

**Files:**
- Create: `app/schemas/draft.py`
- Modify: `app/schemas/context.py:211-254`
- Modify: `app/core/config.py`

- [ ] **Step 1: Create draft schemas**

```python
# app/schemas/draft.py
"""Schemas for draft review/accept/reject API."""

from typing import Any, List, Optional
from pydantic import BaseModel, Field


class DraftComponentResponse(BaseModel):
    """A single reviewable component in a draft."""

    component_type: str = Field(description="habits, tasks, schedule, action_items, materials")
    data: Any = Field(description="Component-specific data")
    status: str = Field(description="pending, accepted, rejected, modified")


class DraftResponse(BaseModel):
    """Full draft state returned to frontend."""

    draft_id: str
    user_id: str
    components: dict[str, DraftComponentResponse] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DraftAcceptRequest(BaseModel):
    """Accept specific components of a draft."""

    user_id: str = Field(..., description="User identifier")
    components: Optional[List[str]] = Field(
        default=None,
        description="Component keys to accept. None = accept all pending.",
    )


class DraftRejectRequest(BaseModel):
    """Reject specific components of a draft."""

    user_id: str = Field(..., description="User identifier")
    components: List[str] = Field(..., description="Component keys to reject")


class DraftModifyRequest(BaseModel):
    """Modify a specific component's data."""

    user_id: str = Field(..., description="User identifier")
    component: str = Field(..., description="Component key to modify")
    data: Any = Field(..., description="Updated component data")
```

- [ ] **Step 2: Add draft_id to ChatResponse**

In `app/schemas/context.py`, add `draft_id` field to `ChatResponse`:

```python
# Add after schedule_status field (line 253):
    draft_id: Optional[str] = Field(
        default=None,
        description="Draft identifier for review/accept/reject flow. Present when pipeline output is staged.",
    )
```

- [ ] **Step 3: Add DRAFT_TTL_SECONDS to config**

In `app/core/config.py`, add:

```python
DRAFT_TTL_SECONDS = 1800  # 30 minutes
```

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/schemas/draft.py app/schemas/context.py app/core/config.py
git commit -m "feat: add draft schemas and draft_id to ChatResponse"
```

---

## Task 3: Draft Store Singleton on App State

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Mount DraftStore on app.state during lifespan**

Find the lifespan function in `app/main.py` and add:

```python
from app.services.draft_store import DraftStore
from app.core.config import DRAFT_TTL_SECONDS

# Inside lifespan (after db_client setup):
app.state.draft_store = DraftStore(ttl_seconds=DRAFT_TTL_SECONDS)
```

- [ ] **Step 2: Verify server starts**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.main import app; print('OK')"`
Expected: "OK" (no import errors)

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/main.py
git commit -m "feat: mount DraftStore singleton on app.state"
```

---

## Task 4: Defer Habit Persistence (Stage Instead of Auto-Commit)

**Files:**
- Modify: `app/services/extraction/behavioral_store.py`
- Modify: `app/services/analytical/control_policy.py:1129-1139`

- [ ] **Step 1: Add stage_habit function to behavioral_store.py**

```python
# Add to app/services/extraction/behavioral_store.py:

def stage_habit(raw_text: str, user_id: str) -> dict:
    """Create a habit proposal without persisting to DB.

    Returns a dict suitable for DraftComponent.data.
    """
    return {
        "raw_text": raw_text.strip(),
        "user_id": user_id,
        "constraint_type": "behavioral",
        "status": "staged",
    }
```

- [ ] **Step 2: Modify control_policy.py to stage habits instead of auto-committing**

In `app/services/analytical/control_policy.py`, replace the habit-saving block (around lines 1129-1139).

Old code:
```python
    if extraction.inline_habits:
        log_step("3_HABITS", "Saving inline habits", {"count": len(extraction.inline_habits)})
        for h in extraction.inline_habits:
            if h and h.strip():
                await store_behavioral_constraint(
                    raw_text=h.strip(),
                    user_id=user_id,
                    supabase_client=supabase,
                )
                print(f"[Memory] Inline habit saved: {h.strip()}")
        execution_summary["habits_saved"] = extraction.inline_habits
```

New code:
```python
    if extraction.inline_habits:
        log_step("3_HABITS", "Staging inline habits for review", {"count": len(extraction.inline_habits)})
        from app.services.extraction.behavioral_store import stage_habit
        staged_habits = []
        for h in extraction.inline_habits:
            if h and h.strip():
                staged_habits.append(stage_habit(h.strip(), user_id))
                print(f"[Memory] Inline habit staged: {h.strip()}")
        execution_summary["habits_staged"] = staged_habits
```

- [ ] **Step 2.5: Verify no other callers of _extract_and_save_inline_habits**

Run: `grep -rn "_extract_and_save_inline_habits" app/`
Expected: Only `control_policy.py:132` (definition) and `control_policy.py:559` (single caller). Both will be updated in this task. If other callers exist, update them too.

- [ ] **Step 3: Rename and rewrite _extract_and_save_inline_habits to stage (lines 132-157)**

Replace the body of `_extract_and_save_inline_habits` to use `stage_habit` instead of `store_behavioral_constraint`. Return the staged list so the caller can add it to the draft.

```python
async def _extract_and_stage_inline_habits(
    text: str, user_id: str,
) -> list[dict]:
    """Extract and stage habits from text. Returns staged habit dicts."""
    from app.services.extraction.behavioral_store import stage_habit
    try:
        extracted = await hybrid_route_query(
            user_prompt=text,
            system_prompt=INLINE_HABIT_EXTRACTION_PROMPT,
            model_override=SLM_ROUTER_MODEL,
        )
        if not extracted:
            return []
        raw = extracted if isinstance(extracted, str) else str(extracted)
        raw = raw.strip()
        if "NONE" in raw.upper() or len(raw) <= 5:
            return []
        return [stage_habit(raw, user_id)]
    except Exception as e:
        print(f"[Memory] Inline extraction failed: {e}")
        return []
```

- [ ] **Step 4: Update _run_plan_day_flow to use staged habits**

In `_run_plan_day_flow` (around line 558), replace:
```python
    if not inline_habits_already_saved:
        await _extract_and_save_inline_habits(planning_goal, user_id, supabase)
```
With:
```python
    staged_from_extraction: list[dict] = []
    if not inline_habits_already_saved:
        staged_from_extraction = await _extract_and_stage_inline_habits(planning_goal, user_id)
    if execution_summary is None:
        execution_summary = {}
    if staged_from_extraction:
        existing = execution_summary.get("habits_staged", [])
        execution_summary["habits_staged"] = existing + staged_from_extraction
```

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/extraction/behavioral_store.py app/services/analytical/control_policy.py
git commit -m "feat: stage habits for review instead of auto-committing"
```

---

## Task 5: Integrate DraftStore into Control Policy

**Files:**
- Modify: `app/services/analytical/control_policy.py`
- Modify: `app/api/v1/endpoints/chat.py`

- [ ] **Step 1: Add `draft_store` parameter to `execute_agentic_flow` and create draft**

In `app/services/analytical/control_policy.py`, add `draft_store` as an explicit optional parameter to the function signature (line 961):

```python
async def execute_agentic_flow(
    user_prompt: str,
    user_id: str,
    db_client: Any,
    day_start_hour_override: Optional[int] = None,
    deadline_override: Optional[str] = None,
    file_base64: Optional[str] = None,
    media_type: Optional[str] = None,
    max_daily_deep_work_minutes: Optional[int] = None,
    min_daily_deep_work_minutes: Optional[int] = None,
    max_task_duration_minutes: Optional[int] = None,
    min_task_duration_minutes: Optional[int] = None,
    progress_callback: ProgressCallback = None,
    model_mode: str = "auto",
    skip_scheduling: bool = False,
    file_name: Optional[str] = None,
    draft_schedule: Optional[dict] = None,
    draft_store: Optional[Any] = None,  # DraftStore instance from app.state
) -> ChatResponse:
```

Then after brain dump extraction and intent classification (~line 1096, after the `_parts` logging block), create the draft:

```python
    # Create draft to hold pipeline output for review
    draft = None
    if draft_store is not None:
        draft = draft_store.create(user_id, metadata={
            "prompt": user_prompt[:200],
            "intent": _intent,
        })
```

- [ ] **Step 2: After each component extraction, add to draft**

After habits are staged (the new code from Task 4):
```python
    if extraction.inline_habits and draft and draft_store:
        from app.services.draft_store import DraftComponent
        draft_store.add_component(
            draft.draft_id, user_id, "habits",
            DraftComponent(
                component_type="habits",
                data=execution_summary.get("habits_staged", []),
                status="pending",
            ),
        )
```

After action proposals are collected (around line 1227):
```python
    if action_proposals and draft and draft_store:
        from app.services.draft_store import DraftComponent
        draft_store.add_component(
            draft.draft_id, user_id, "action_items",
            DraftComponent(
                component_type="action_items",
                data=action_proposals,
                status="pending",
            ),
        )
```

- [ ] **Step 3: In _run_plan_day_flow, add tasks and schedule to draft**

After schedule_response is computed (around line 807), add tasks + schedule to draft:
```python
    # Add tasks component to draft
    if draft and draft_store:
        from app.services.draft_store import DraftComponent
        draft_store.add_component(
            draft.draft_id, user_id, "tasks",
            DraftComponent(
                component_type="tasks",
                data=[c.model_dump() for c in master_chunk_list],
                status="pending",
            ),
        )
    if schedule_response is not None and draft and draft_store:
        draft_store.add_component(
            draft.draft_id, user_id, "schedule",
            DraftComponent(
                component_type="schedule",
                data=schedule_response.model_dump(mode='json'),
                status="pending",
            ),
        )
```

- [ ] **Step 4: Return draft_id in ChatResponse**

In every ChatResponse construction within `_run_plan_day_flow` and `execute_agentic_flow`, add:
```python
    draft_id=draft.draft_id if draft else None,
```

- [ ] **Step 5: Pass draft_store explicitly from chat endpoint to control policy**

In `app/api/v1/endpoints/chat.py`, get draft_store from app state and pass it as an explicit parameter to `execute_agentic_flow`:
```python
    draft_store = getattr(http_request.app.state, "draft_store", None)
    # Pass draft_store explicitly in the execute_agentic_flow call:
    response = await execute_agentic_flow(
        ...existing args...,
        draft_store=draft_store,
    )
```
Update every call site of `execute_agentic_flow` in `chat.py` to include `draft_store=draft_store`.

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py app/api/v1/endpoints/chat.py
git commit -m "feat: integrate DraftStore into pipeline, return draft_id in ChatResponse"
```

---

## Task 6: Draft API Endpoints

**Files:**
- Create: `app/api/v1/endpoints/drafts.py`
- Create: `tests/test_draft_endpoints.py`
- Modify: `app/api/v1/router.py`

- [ ] **Step 1: Write failing tests for draft endpoints**

```python
# tests/test_draft_endpoints.py
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client with draft store."""
    from app.main import app
    from app.services.draft_store import DraftStore, Draft, DraftComponent

    # Seed a draft for testing
    store = DraftStore(ttl_seconds=300)
    draft = store.create("test_user")
    store.add_component(
        draft.draft_id, "test_user", "habits",
        DraftComponent(component_type="habits", data=[{"raw_text": "no work before 11 AM"}], status="pending"),
    )
    store.add_component(
        draft.draft_id, "test_user", "tasks",
        DraftComponent(component_type="tasks", data=[{"task_id": "t1", "title": "Read ch1"}], status="pending"),
    )
    app.state.draft_store = store
    app.state._test_draft_id = draft.draft_id
    return TestClient(app)


def test_get_draft(client):
    draft_id = client.app.state._test_draft_id
    resp = client.get(f"/api/v1/drafts/{draft_id}?user_id=test_user")
    assert resp.status_code == 200
    data = resp.json()
    assert data["draft_id"] == draft_id
    assert "habits" in data["components"]
    assert data["components"]["habits"]["status"] == "pending"


def test_get_draft_wrong_user(client):
    draft_id = client.app.state._test_draft_id
    resp = client.get(f"/api/v1/drafts/{draft_id}?user_id=wrong_user")
    assert resp.status_code == 404


def test_accept_component(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/accept",
        json={"user_id": "test_user", "components": ["habits"]},
    )
    assert resp.status_code == 200
    # Verify habit was persisted (mock would be needed for full integration)
    assert resp.json()["accepted"] == ["habits"]


def test_reject_component(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/reject",
        json={"user_id": "test_user", "components": ["tasks"]},
    )
    assert resp.status_code == 200
    assert resp.json()["rejected"] == ["tasks"]


def test_accept_all(client):
    draft_id = client.app.state._test_draft_id
    resp = client.post(
        f"/api/v1/drafts/{draft_id}/accept",
        json={"user_id": "test_user"},
    )
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_draft_endpoints.py -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Implement draft endpoints**

```python
# app/api/v1/endpoints/drafts.py
"""Draft review/accept/reject endpoints."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from app.schemas.draft import (
    DraftAcceptRequest,
    DraftModifyRequest,
    DraftRejectRequest,
    DraftResponse,
    DraftComponentResponse,
)

router = APIRouter()


def _get_draft_store(request: Request):
    store = getattr(request.app.state, "draft_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Draft store not available")
    return store


@router.get(
    "/{draft_id}",
    response_model=DraftResponse,
    summary="Get draft state",
)
async def get_draft(draft_id: str, user_id: str, http_request: Request):
    """Retrieve current state of a draft for user review."""
    store = _get_draft_store(http_request)
    draft = store.get(draft_id, user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found or expired")
    return DraftResponse(
        draft_id=draft.draft_id,
        user_id=draft.user_id,
        components={
            k: DraftComponentResponse(
                component_type=v.component_type,
                data=v.data,
                status=v.status,
            )
            for k, v in draft.components.items()
        },
        metadata=draft.metadata,
    )


@router.post(
    "/{draft_id}/accept",
    summary="Accept draft components",
)
async def accept_draft(
    draft_id: str, request: DraftAcceptRequest, http_request: Request
):
    """Accept specific components (or all) and persist to database."""
    store = _get_draft_store(http_request)
    draft = store.get(draft_id, request.user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found or expired")

    db_client = getattr(http_request.app.state, "db_client", None)
    supabase = db_client.supabase if db_client and hasattr(db_client, "supabase") else None

    components_to_accept = request.components
    if components_to_accept is None:
        components_to_accept = [
            k for k, v in draft.components.items() if v.status == "pending"
        ]

    accepted = []
    for key in components_to_accept:
        if key not in draft.components:
            continue
        comp = draft.components[key]
        if comp.status in ("rejected",):
            continue

        # Persist based on component type
        if key == "habits" and supabase:
            from app.services.extraction.behavioral_store import store_behavioral_constraint
            for habit in comp.data:
                if isinstance(habit, dict) and habit.get("raw_text"):
                    await store_behavioral_constraint(
                        raw_text=habit["raw_text"],
                        user_id=request.user_id,
                        supabase_client=supabase,
                    )

        elif key == "tasks" and supabase:
            import asyncio
            from app.api.v1.endpoints.reasoning import TaskChunk
            from app.services.analytical.control_policy import _persist_fused_tasks
            task_chunks = [TaskChunk(**t) for t in comp.data]
            # _persist_fused_tasks is sync — offload to thread to avoid blocking event loop
            await asyncio.to_thread(_persist_fused_tasks, request.user_id, task_chunks, supabase)

        elif key == "action_items" and supabase:
            # Store accepted action items to pending_action_items
            for item in comp.data:
                try:
                    supabase.table("pending_action_items").insert({
                        "user_id": request.user_id,
                        "title": item.get("title", ""),
                        "summary": item.get("summary", ""),
                        "status": "accepted",
                    }).execute()
                except Exception as e:
                    print(f"[Drafts] Action item persist failed: {e}")

        store.accept_component(draft_id, request.user_id, key)
        accepted.append(key)

    return {"status": "ok", "accepted": accepted, "draft_id": draft_id}


@router.post(
    "/{draft_id}/reject",
    summary="Reject draft components",
)
async def reject_draft(
    draft_id: str, request: DraftRejectRequest, http_request: Request
):
    """Reject specific components (they won't be persisted)."""
    store = _get_draft_store(http_request)
    draft = store.get(draft_id, request.user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found or expired")

    rejected = []
    for key in request.components:
        if store.reject_component(draft_id, request.user_id, key):
            rejected.append(key)

    return {"status": "ok", "rejected": rejected, "draft_id": draft_id}


@router.post(
    "/{draft_id}/modify",
    summary="Modify a draft component",
)
async def modify_draft(
    draft_id: str, request: DraftModifyRequest, http_request: Request
):
    """Update a specific component's data (e.g., edit a task, change a habit)."""
    store = _get_draft_store(http_request)
    if not store.update_component_data(
        draft_id, request.user_id, request.component, request.data
    ):
        raise HTTPException(status_code=404, detail="Draft or component not found")

    return {"status": "modified", "component": request.component, "draft_id": draft_id}


@router.delete(
    "/{draft_id}",
    summary="Discard a draft",
)
async def delete_draft(draft_id: str, user_id: str, http_request: Request):
    """Discard an entire draft (user decided not to proceed)."""
    store = _get_draft_store(http_request)
    if not store.delete(draft_id, user_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"status": "deleted", "draft_id": draft_id}
```

- [ ] **Step 4: Mount drafts router**

In `app/api/v1/router.py`, add:
```python
from app.api.v1.endpoints.drafts import router as drafts_router
api_router.include_router(drafts_router, prefix="/drafts", tags=["drafts"])
```

- [ ] **Step 5: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_draft_endpoints.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/api/v1/endpoints/drafts.py app/api/v1/router.py tests/test_draft_endpoints.py
git commit -m "feat: add draft CRUD endpoints for review/accept/reject"
```

---

## Task 7: Enhanced SSE Phase Events with Structured Data

**Files:**
- Modify: `app/api/v1/endpoints/chat.py:148-200`
- Modify: `app/services/analytical/control_policy.py` (progress_callback calls)

- [ ] **Step 1: Enhance progress_callback to emit component data**

In `control_policy.py`, update the progress_callback calls to include actual data, not just metadata.

After habits are staged:
```python
    if progress_callback:
        await progress_callback("habits_staged", {
            "count": len(staged_habits),
            "habits": staged_habits,  # Full data, not just count
            "phase_summary": f"Found {len(staged_habits)} habit(s) for your review",
        })
```

After decomposition completes (around line 674):
```python
    if progress_callback:
        _task_data = [t.model_dump() for t in graph.decomposition]
        await progress_callback("decomposition_done", {
            "task_count": len(graph.decomposition),
            "total_minutes": sum(t.duration_minutes for t in graph.decomposition),
            "duration_ms": _decompose_dur,
            "tasks": _task_data,  # Full task data for progressive render
            "phase_summary": f"Created {len(graph.decomposition)} tasks",
        })
```

After schedule is solved (around line 811):
```python
    if progress_callback and schedule_response is not None:
        await progress_callback("schedule_done", {
            "status": "OPTIMAL",
            "schedule": schedule_response.model_dump(mode='json'),  # Full schedule data
            "horizon_hours": round(used_horizon_minutes / 60, 1),
            "duration_ms": _schedule_dur,
            "phase_summary": f"Scheduled {_sched_task_count} tasks",
        })
```

- [ ] **Step 2: Include draft_id in SSE complete event**

In `chat.py` event_stream, the `complete` event already includes the full ChatResponse. The `draft_id` field added in Task 2 will automatically be included since it's part of ChatResponse.

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py app/api/v1/endpoints/chat.py
git commit -m "feat: emit structured data in SSE phase events for progressive rendering"
```

---

## Task 8: Skip Voice of Jarvis for Structured Responses

**Files:**
- Modify: `app/services/analytical/control_policy.py:836-842`
- Modify: `app/api/v1/endpoints/chat.py:296-356`

The key insight: when the pipeline produces a schedule + tasks, the frontend renders them directly. Voice of Jarvis synthesis is redundant and costs 5-10s. We should skip it and let the frontend display the structured data.

- [ ] **Step 1: In _run_plan_day_flow, always skip VoJ when schedule is present**

Replace the voice synthesis block (around lines 836-842):

Old:
```python
        if use_voice_synthesis:
            message, thinking_process = await synthesize_jarvis_response(summary)
        else:
            message = "Here's your schedule."
            thinking_process = _build_thinking_fallback(summary)
```

New:
```python
        # Skip VoJ when frontend will render structured schedule data directly.
        # VoJ was adding 5-10s for a message the user doesn't read when they can
        # see the visual schedule. Use deterministic fallback instead.
        message = "Here's your schedule."
        thinking_process = _build_thinking_fallback(summary)
```

- [ ] **Step 2: In chat.py stream, skip VoJ streaming when schedule is present**

In `app/api/v1/endpoints/chat.py`, insert the following block **after line 294** (after the GREETING early-return `return` statement) and **before line 296** (`# Step 2b: For pipeline intents` comment). This is right after the greeting handler and before VoJ streaming begins:

```python
        # Skip VoJ for PLAN_DAY when schedule data exists — frontend renders it directly
        if partial.schedule and partial.intent == "PLAN_DAY":
            partial_dict = partial.model_dump()
            partial_dict["message"] = partial.message or "Here's your schedule."
            partial_dict["thinking_process"] = partial.thinking_process or _build_thinking_fallback(captured_summary)
            partial_dict["generation_metrics"] = {
                "total_tokens": 0, "total_time_s": 0, "tok_per_sec": 0, "ttft_ms": None,
                "model": "none (structured response)",
            }
            yield f"event: complete\ndata: {json.dumps(partial_dict)}\n\n"
            return
```

This saves one full LLM call (~5-10s) for every PLAN_DAY response.

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py app/api/v1/endpoints/chat.py
git commit -m "perf: skip Voice of Jarvis synthesis for PLAN_DAY with schedule data"
```

---

## Task 9: Improve Cache TTL & Hit Rate

**Files:**
- Modify: `app/services/analytical/control_policy.py:57` (decomposition cache TTL)
- Modify: `app/services/analytical/habit_translator.py:20` (habit cache TTL)

- [ ] **Step 1: Extend habit translation cache from 1hr to 24hr**

Habits rarely change within a day. In `app/services/analytical/habit_translator.py`, change:
```python
_CACHE_TTL_S = 86400  # 24 hours (was 3600)
```

- [ ] **Step 2: Extend decomposition cache from 1hr to 4hr**

Goals are rephrased but the same goal within a session should reuse. In `app/services/analytical/control_policy.py`, change:
```python
_DECOMPOSE_CACHE_TTL_S = 14400  # 4 hours (was 3600)
```

- [ ] **Step 3: Invalidate habit translation cache when habits change**

In `app/services/extraction/behavioral_store.py`, after `store_behavioral_constraint` succeeds, also clear the habit translation cache:

```python
# After invalidate_habit_cache() in store_behavioral_constraint:
from app.services.analytical.habit_translator import invalidate_translation_cache
invalidate_translation_cache()
```

Add `invalidate_translation_cache` to `habit_translator.py`:
```python
def invalidate_translation_cache() -> None:
    """Clear habit translation cache when habits are modified."""
    global _translate_cache
    _translate_cache.clear()
```

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/analytical/control_policy.py app/services/analytical/habit_translator.py app/services/extraction/behavioral_store.py
git commit -m "perf: extend cache TTLs (24h habits, 4h decomposition) with invalidation"
```

---

## Task 10: Integration Tests

**Files:**
- Create: `tests/test_draft_integration.py`

- [ ] **Step 1: Write integration test for full draft flow**

```python
# tests/test_draft_integration.py
"""Integration tests for the progressive draft review flow."""

import pytest
from app.services.draft_store import DraftStore, DraftComponent


def test_full_draft_lifecycle():
    """Test: create → add components → modify → accept some → reject some."""
    store = DraftStore(ttl_seconds=300)

    # Create draft
    draft = store.create("user_1", metadata={"prompt": "Plan my day"})
    assert draft.draft_id

    # Add habits component
    store.add_component(
        draft.draft_id, "user_1", "habits",
        DraftComponent(
            component_type="habits",
            data=[
                {"raw_text": "no work before 11 AM", "status": "staged"},
                {"raw_text": "gym from 5 to 6 PM", "status": "staged"},
            ],
            status="pending",
        ),
    )

    # Add tasks component
    store.add_component(
        draft.draft_id, "user_1", "tasks",
        DraftComponent(
            component_type="tasks",
            data=[
                {"task_id": "goal1_task_1", "title": "Read chapter 1", "duration_minutes": 25},
                {"task_id": "goal1_task_2", "title": "Practice problems", "duration_minutes": 20},
            ],
            status="pending",
        ),
    )

    # Add schedule component
    store.add_component(
        draft.draft_id, "user_1", "schedule",
        DraftComponent(
            component_type="schedule",
            data={"status": "OPTIMAL", "schedule": {"goal1_task_1": {"start": 180}}},
            status="pending",
        ),
    )

    # Verify all components present
    draft = store.get(draft.draft_id, "user_1")
    assert len(draft.components) == 3
    assert all(c.status == "pending" for c in draft.components.values())

    # Modify tasks (user edits a task duration)
    updated_tasks = draft.components["tasks"].data.copy()
    updated_tasks[0]["duration_minutes"] = 15  # User shortened it
    store.update_component_data(draft.draft_id, "user_1", "tasks", updated_tasks)
    draft = store.get(draft.draft_id, "user_1")
    assert draft.components["tasks"].status == "modified"
    assert draft.components["tasks"].data[0]["duration_minutes"] == 15

    # Accept habits and tasks
    store.accept_component(draft.draft_id, "user_1", "habits")
    store.accept_component(draft.draft_id, "user_1", "tasks")

    # Reject schedule (user wants to reschedule)
    store.reject_component(draft.draft_id, "user_1", "schedule")

    draft = store.get(draft.draft_id, "user_1")
    assert draft.components["habits"].status == "accepted"
    assert draft.components["tasks"].status == "accepted"
    assert draft.components["schedule"].status == "rejected"


def test_draft_user_isolation():
    """Test: user A cannot access user B's draft."""
    store = DraftStore(ttl_seconds=300)
    draft_a = store.create("user_a")
    draft_b = store.create("user_b")

    store.add_component(
        draft_a.draft_id, "user_a", "habits",
        DraftComponent(component_type="habits", data=[{"raw_text": "secret"}], status="pending"),
    )

    # User B cannot read user A's draft
    assert store.get(draft_a.draft_id, "user_b") is None

    # User B cannot accept user A's component
    assert not store.accept_component(draft_a.draft_id, "user_b", "habits")

    # User A can access their own
    assert store.get(draft_a.draft_id, "user_a") is not None


def test_accept_all_pending():
    """Test: accept_all only touches pending components."""
    store = DraftStore(ttl_seconds=300)
    draft = store.create("user_1")

    store.add_component(
        draft.draft_id, "user_1", "habits",
        DraftComponent(component_type="habits", data=[], status="pending"),
    )
    store.add_component(
        draft.draft_id, "user_1", "tasks",
        DraftComponent(component_type="tasks", data=[], status="pending"),
    )

    # Reject habits first
    store.reject_component(draft.draft_id, "user_1", "habits")

    # Accept all — should only accept tasks (habits already rejected)
    store.accept_all(draft.draft_id, "user_1")
    draft = store.get(draft.draft_id, "user_1")
    assert draft.components["habits"].status == "rejected"  # Unchanged
    assert draft.components["tasks"].status == "accepted"
```

- [ ] **Step 2: Run all tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_draft_store.py tests/test_draft_integration.py tests/test_draft_endpoints.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add tests/test_draft_integration.py
git commit -m "test: add integration tests for progressive draft review flow"
```

---

## Summary of Changes

### Performance Impact
| Optimization | Time Saved | How |
|-------------|-----------|-----|
| Skip VoJ for PLAN_DAY | ~5-10s | Frontend renders structured schedule directly |
| Progressive SSE with data | Perceived: ~120s → ~3s | User sees each phase as it completes |
| 24h habit cache | ~30-60s on repeat requests | Avoids redundant 27B translation |
| 4h decomposition cache | ~60-90s on same goal | Avoids redundant 27B decomposition |

### Review UX Impact
| Component | Before | After |
|-----------|--------|-------|
| Habits | Auto-committed | Staged in draft → accept/reject |
| Tasks | Auto-committed (or draft) | Always in draft → accept/reject |
| Schedule | Draft (existed) | Draft with draft_id linkage |
| Action Items | Never persisted | In draft → accept persists |
| Materials | Auto-committed | Future task (not in this plan) |

### New API Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/drafts/{id}` | GET | Retrieve draft state |
| `/api/v1/drafts/{id}/accept` | POST | Accept components, persist to DB |
| `/api/v1/drafts/{id}/reject` | POST | Reject components |
| `/api/v1/drafts/{id}/modify` | POST | Edit component data |
| `/api/v1/drafts/{id}` | DELETE | Discard draft |

### SSE Event Enhancements
| Event | Before | After |
|-------|--------|-------|
| `phase:habits_staged` | Count only | Full habit data for UI render |
| `phase:decomposition_done` | Task titles | Full task objects for interactive list |
| `phase:schedule_done` | Status only | Full schedule data for calendar render |
| `complete` | Full ChatResponse | + draft_id for review flow |
