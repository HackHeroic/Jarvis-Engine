# Workspace & Document Intelligence (P1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix workspace data flow (criteria/WOOP from backend, not localStorage), implement real document type handlers (practice_problems, syllabus, assignment), and build the Intent Discovery Engine.

**Architecture:** Workspace currently reads completion_criteria and WOOP from localStorage. We move this to the backend by: (1) persisting criteria in `task_completion_criteria` table during task persistence, (2) returning them from the workspace endpoint, (3) implementing document handlers that enrich tasks with extracted problems and criteria. The Intent Discovery Engine tracks unmatched intents, clusters them, and generates IntentBlueprints.

**Tech Stack:** Next.js (frontend), FastAPI + Pydantic (backend), Supabase (DB), ChromaDB (RAG), Gemini 2.5 Flash / Qwen-4B (LLM)

**Depends on:** Plan A (Core Loop Fixes) must be completed first — Task 2 adds the `completion_criteria` and `implementation_intention` columns to `user_tasks`.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `Jarvis-Engine/app/services/analytical/control_policy.py` | Modify | Persist completion_criteria to `task_completion_criteria` table during `_persist_fused_tasks()` |
| `Jarvis-Engine/app/services/analytical/workspace_builder.py` | Modify | Return completion_criteria + WOOP from DB instead of title-only primary_objective |
| `Jarvis-Engine/app/schemas/workspace.py` | Modify | Add `completion_criteria` and `implementation_intention` fields to `TaskWorkspace` |
| `Jarvis-Engine/app/services/documents/registry.py` | Modify | Replace stub handlers with real extraction logic |
| `Jarvis-Engine/app/services/documents/handlers/` | Create (dir) | Separate handler files for each document type |
| `Jarvis-Engine/app/services/intent_discovery.py` | Create | Intent Discovery Engine: gap detection, clustering, blueprint generation |
| `jarvis-frontend/app/(app)/workspace/[taskId]/page.tsx` | Modify | Read criteria/WOOP from API response instead of localStorage |

---

### Task 1: Persist Completion Criteria to Backend During Task Creation

**Files:**
- Modify: `Jarvis-Engine/app/services/analytical/control_policy.py`

- [ ] **Step 1: After `_persist_fused_tasks()` inserts rows, also insert completion_criteria**

Add this function to `control_policy.py` after `_persist_fused_tasks`:

```python
def _persist_completion_criteria(
    user_id: str,
    chunks: list,
    supabase_client: Any,
) -> None:
    """Store each task's completion_criteria in the task_completion_criteria table."""
    if not supabase_client or not chunks:
        return
    try:
        rows = []
        for chunk in chunks:
            tid = chunk.task_id if hasattr(chunk, "task_id") else chunk.get("task_id", "")
            cc = chunk.completion_criteria if hasattr(chunk, "completion_criteria") else chunk.get("completion_criteria", "")
            if not cc:
                continue
            # Split compound criteria by semicolons or newlines
            criteria_parts = [c.strip() for c in re.split(r"[;\n]", cc) if c.strip()]
            for part in criteria_parts:
                rows.append({
                    "user_id": user_id,
                    "task_id": tid,
                    "criteria_text": part,
                    "source": "decomposition",
                    "is_required": True,
                    "is_completed": False,
                })
        if rows:
            # Clear existing decomposition criteria for these tasks
            task_ids = list(set(r["task_id"] for r in rows))
            for tid in task_ids:
                supabase_client.table("task_completion_criteria").delete().eq(
                    "user_id", user_id
                ).eq("task_id", tid).eq("source", "decomposition").execute()
            supabase_client.table("task_completion_criteria").insert(rows).execute()
    except Exception as e:
        print(f"[Control Policy] Persist completion criteria failed: {e}")
```

- [ ] **Step 2: Call `_persist_completion_criteria` from `_persist_fused_tasks`**

At the end of `_persist_fused_tasks`, before the except block, add:

```python
        _persist_completion_criteria(user_id, chunks, supabase_client)
```

- [ ] **Step 3: Commit**

```bash
git add app/services/analytical/control_policy.py
git commit -m "feat: persist completion criteria to task_completion_criteria table"
```

---

### Task 2: Update Workspace Endpoint to Return Criteria + WOOP from Backend

**Files:**
- Modify: `Jarvis-Engine/app/schemas/workspace.py`
- Modify: `Jarvis-Engine/app/services/analytical/workspace_builder.py`

- [ ] **Step 1: Add fields to TaskWorkspace schema**

In `Jarvis-Engine/app/schemas/workspace.py`, add to the `TaskWorkspace` class:

```python
class TaskWorkspace(BaseModel):
    task_id: str = Field(description="Task identifier")
    task_title: str = Field(description="Display title of the task")
    primary_objective: str = Field(
        description="Derived from title + topic_keywords; main learning goal",
    )
    surfaced_assets: List[StudyAsset] = Field(
        default_factory=list,
        description="RAG chunks, curated links, and generated practice assets",
    )
    completion_criteria: List[dict] = Field(
        default_factory=list,
        description="List of {id, text, is_completed, source} from task_completion_criteria",
    )
    implementation_intention: Optional[dict] = Field(
        default=None,
        description="WOOP: {obstacle_trigger, behavioral_response}",
    )
```

- [ ] **Step 2: Fetch criteria + WOOP in workspace_builder.py**

In `build_task_workspace()`, after fetching task metadata (around line 253-259), add:

```python
        # Fetch completion criteria from task_completion_criteria table
        criteria_list = []
        woop_data = None
        try:
            cr_result = (
                supabase.table("task_completion_criteria")
                .select("id, criteria_text, is_completed, source")
                .eq("user_id", user_id)
                .eq("task_id", task_id)
                .execute()
            )
            criteria_list = [
                {"id": c["id"], "text": c["criteria_text"],
                 "is_completed": c.get("is_completed", False),
                 "source": c.get("source", "decomposition")}
                for c in (cr_result.data or [])
            ]
        except Exception:
            pass

        # Fetch WOOP from user_tasks
        if task_result.data and len(task_result.data) > 0:
            woop_data = task_result.data[0].get("implementation_intention")
```

Then update the return to include these fields:

```python
    return TaskWorkspace(
        task_id=task_id,
        task_title=task_title,
        primary_objective=primary_objective,
        surfaced_assets=surfaced,
        completion_criteria=criteria_list,
        implementation_intention=woop_data,
    )
```

- [ ] **Step 3: Update the `user_tasks` select query to also fetch `implementation_intention`**

In `build_task_workspace()` line 246, change:

```python
.select("title, topic_keywords")
```

to:

```python
.select("title, topic_keywords, implementation_intention")
```

- [ ] **Step 4: Commit**

```bash
git add app/schemas/workspace.py app/services/analytical/workspace_builder.py
git commit -m "feat: workspace returns completion_criteria + WOOP from backend"
```

---

### Task 3: Update Frontend Workspace to Read from API Instead of localStorage

**Files:**
- Modify: `jarvis-frontend/app/(app)/workspace/[taskId]/page.tsx`

- [ ] **Step 1: Use API response for criteria instead of localStorage**

In the workspace page, after the `getWorkspace()` call succeeds, use the response's `completion_criteria` and `implementation_intention` fields instead of parsing localStorage:

```typescript
// After workspace data is fetched:
if (ws.completion_criteria && ws.completion_criteria.length > 0) {
  setCriteria(ws.completion_criteria.map((c: any) => ({
    id: c.id,
    text: c.text,
    done: c.is_completed || false,
  })));
}

if (ws.implementation_intention) {
  setWoop(ws.implementation_intention);
}
```

Keep the localStorage fallback for backwards compatibility, but prefer API data.

- [ ] **Step 2: Build and verify**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend && npx next build --no-lint
```

- [ ] **Step 3: Commit**

```bash
git add app/\(app\)/workspace/\[taskId\]/page.tsx
git commit -m "feat: workspace reads criteria + WOOP from API, localStorage fallback"
```

---

### Task 4: Implement Real Document Type Handlers

**Files:**
- Modify: `Jarvis-Engine/app/services/documents/registry.py`

- [ ] **Step 1: Implement `handle_practice_problems` — extract problems and link to tasks**

Replace the stub in `registry.py` (lines 31-43):

```python
async def handle_practice_problems(user_id: str, extraction: dict, source_id: str):
    """Extract individual problems from practice document, match to tasks."""
    from app.models.brain.litellm_conf import hybrid_route_query
    from app.schemas.document import ProblemSetExtraction
    import json, re

    classification = extraction.get("classification", {})
    topics = classification.get("topics_covered", []) if hasattr(classification, "get") else getattr(classification, "topics_covered", [])
    text = extraction.get("text", "")

    if not text:
        return {"handler": "practice_problems", "status": "no_text"}

    # Extract problems via LLM
    try:
        raw = await hybrid_route_query(
            user_prompt=(
                "Extract all individual problems/questions from this document. "
                "Return JSON matching ProblemSetExtraction schema:\n"
                '{"problems": [{"problem_number": 1, "problem_text": "...", '
                '"topic_tags": ["..."], "difficulty_estimate": 0.5, '
                '"expected_time_minutes": 10, "has_solution": false}], '
                '"overall_topics": ["..."]}\n\n'
                f"Document text:\n{text[:6000]}"
            ),
            system_prompt="Extract structured problems from academic documents. Return ONLY valid JSON.",
            response_schema=ProblemSetExtraction,
        )
        if isinstance(raw, str):
            raw = re.sub(r"```json|```", "", raw).strip()
            result = ProblemSetExtraction.model_validate_json(raw)
        else:
            result = raw if isinstance(raw, ProblemSetExtraction) else ProblemSetExtraction.model_validate(raw)
    except Exception as e:
        logger.warning("Practice problem extraction failed: %s", e)
        return {"handler": "practice_problems", "status": "extraction_failed", "error": str(e)}

    # Store extracted problems in Supabase
    from app.db.supabase_py import DatabaseClient
    db = DatabaseClient()
    supabase = db.supabase if db else None

    if supabase and result.problems:
        rows = []
        for p in result.problems:
            rows.append({
                "user_id": user_id,
                "source_id": source_id,
                "problem_number": p.problem_number,
                "problem_text": p.problem_text,
                "topic_tags": p.topic_tags,
                "difficulty": p.difficulty_estimate,
                "expected_time": p.expected_time_minutes,
                "has_solution": p.has_solution,
                "solution_text": p.solution_text,
                "status": "pending",
            })
        try:
            supabase.table("extracted_problems").insert(rows).execute()
        except Exception as e:
            logger.warning("Failed to store extracted problems: %s", e)

    return {
        "handler": "practice_problems",
        "status": "extracted",
        "problem_count": len(result.problems),
        "topics": result.overall_topics,
    }
```

- [ ] **Step 2: Implement `handle_syllabus` — extract topics + deadlines, propose tasks**

Replace the stub (lines 60-72):

```python
async def handle_syllabus(user_id: str, extraction: dict, source_id: str):
    """Extract topics + deadlines from syllabus, store as plan updates."""
    classification = extraction.get("classification", {})
    topics = classification.get("topics_covered", []) if hasattr(classification, "get") else getattr(classification, "topics_covered", [])
    deadline = classification.get("deadline_detected") if hasattr(classification, "get") else getattr(classification, "deadline_detected", None)

    from app.db.supabase_py import DatabaseClient
    db = DatabaseClient()
    supabase = db.supabase if db else None

    # Store deadline as plan update if detected
    if supabase and deadline:
        try:
            supabase.table("user_plan_updates").upsert({
                "user_id": user_id,
                "goal_id": f"syllabus_{source_id[:8]}",
                "source": "ingestion",
                "deadline_date": deadline,
                "deadline_raw": deadline,
                "context_snippet": f"Syllabus topics: {', '.join(topics[:5])}",
            }, on_conflict="user_id,goal_id").execute()
        except Exception as e:
            logger.warning("Failed to store syllabus deadline: %s", e)

    logger.info("[DocumentHandler] syllabus: user=%s, topics=%s, deadline=%s", user_id, topics, deadline)
    return {"handler": "syllabus", "status": "processed", "topics": topics, "deadline": deadline}
```

- [ ] **Step 3: Implement `handle_assignment` — extract requirements as completion criteria**

Replace the stub (lines 75-87):

```python
async def handle_assignment(user_id: str, extraction: dict, source_id: str):
    """Extract assignment requirements and add as task completion criteria."""
    from app.models.brain.litellm_conf import hybrid_route_query
    import json, re

    classification = extraction.get("classification", {})
    topics = classification.get("topics_covered", []) if hasattr(classification, "get") else getattr(classification, "topics_covered", [])
    text = extraction.get("text", "")

    if not text:
        return {"handler": "assignment", "status": "no_text"}

    # Extract requirements via LLM
    try:
        raw = await hybrid_route_query(
            user_prompt=(
                "Extract the assignment requirements/deliverables as a checklist. "
                "Return JSON: {\"requirements\": [\"requirement 1\", \"requirement 2\", ...], "
                "\"deadline\": \"ISO date or null\"}\n\n"
                f"Assignment text:\n{text[:6000]}"
            ),
            system_prompt="Extract structured requirements. Return ONLY valid JSON.",
            prefer_local=False,
        )
        text_clean = re.sub(r"```json|```", "", raw.strip() if isinstance(raw, str) else "").strip()
        parsed = json.loads(text_clean) if isinstance(raw, str) else raw
    except Exception:
        return {"handler": "assignment", "status": "extraction_failed"}

    requirements = parsed.get("requirements", []) if isinstance(parsed, dict) else []

    # Find matching tasks by topic similarity and add as criteria
    from app.db.supabase_py import DatabaseClient
    db = DatabaseClient()
    supabase = db.supabase if db else None

    if supabase and requirements:
        # Find tasks that match assignment topics
        try:
            task_result = supabase.table("user_tasks").select("task_id, title").eq(
                "user_id", user_id
            ).eq("status", "pending").execute()
            tasks = task_result.data or []
        except Exception:
            tasks = []

        # Simple keyword matching — link to first matching task
        matched_task_id = None
        for t in tasks:
            title_lower = (t.get("title") or "").lower()
            if any(topic.lower() in title_lower for topic in topics[:3]):
                matched_task_id = t["task_id"]
                break

        if matched_task_id:
            rows = [
                {
                    "user_id": user_id,
                    "task_id": matched_task_id,
                    "criteria_text": req,
                    "source": "uploaded_document",
                    "source_id": source_id,
                    "is_required": True,
                    "is_completed": False,
                }
                for req in requirements
            ]
            try:
                supabase.table("task_completion_criteria").insert(rows).execute()
            except Exception as e:
                logger.warning("Failed to store assignment criteria: %s", e)

    return {"handler": "assignment", "status": "processed", "requirements_count": len(requirements)}
```

- [ ] **Step 4: Update `document_intelligence_pipeline` to pass extracted text to handlers**

In `pipeline.py` line 95-96, change:

```python
        extraction = {"classification": classification}
        await entry.handler(user_id, extraction, source_id)
```

to:

```python
        extraction = {"classification": classification, "text": extracted_text}
        await entry.handler(user_id, extraction, source_id)
```

- [ ] **Step 5: Commit**

```bash
git add app/services/documents/registry.py app/services/documents/pipeline.py
git commit -m "feat: implement real document handlers (practice_problems, syllabus, assignment)"
```

---

### Task 5: Build Intent Discovery Engine

**Files:**
- Create: `Jarvis-Engine/app/services/intent_discovery.py`
- Modify: `Jarvis-Engine/app/services/intent_registry.py`

- [ ] **Step 1: Create the Intent Discovery Engine**

Create `Jarvis-Engine/app/services/intent_discovery.py`:

```python
"""Intent Discovery Engine — detects embedding gaps, clusters CHAT fallbacks,
generates IntentBlueprint proposals when patterns emerge.

Phase 1D: Rule-based gap detection + frequency counting.
Clustering and blueprint generation via Gemini when threshold hit.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Thresholds
MIN_GAP_COUNT = 3       # Need 3+ unmatched queries before proposing
SIMILARITY_THRESHOLD = 0.65  # Below this = "embedding gap"


@dataclass
class IntentGap:
    """A single unmatched user query that fell back to CHAT."""
    query: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class IntentBlueprint:
    """A proposed new intent, generated from clustered gaps."""
    name: str
    description: str
    examples: list[str]
    handler_steps: list[str]  # High-level steps the handler should perform
    confidence: float = 0.0


class IntentDiscoveryEngine:
    """Tracks CHAT fallbacks, clusters them, proposes new intents."""

    def __init__(self):
        self._gaps: list[IntentGap] = []
        self._proposed: list[IntentBlueprint] = []

    def record_gap(self, query: str, max_similarity: float) -> None:
        """Record a query that fell back to CHAT due to low similarity."""
        if max_similarity < SIMILARITY_THRESHOLD:
            self._gaps.append(IntentGap(query=query))
            logger.info(
                "[IntentDiscovery] Gap recorded (sim=%.2f): %s",
                max_similarity, query[:80],
            )

    @property
    def gap_count(self) -> int:
        return len(self._gaps)

    @property
    def proposals(self) -> list[IntentBlueprint]:
        return list(self._proposed)

    async def check_and_propose(self, memory_store: Any = None) -> Optional[IntentBlueprint]:
        """If enough gaps accumulated, cluster and propose a new intent.

        Called periodically (e.g., after every 5th CHAT fallback).
        """
        if len(self._gaps) < MIN_GAP_COUNT:
            return None

        # Cluster gaps by semantic similarity
        queries = [g.query for g in self._gaps]

        try:
            from app.models.brain.litellm_conf import hybrid_route_query
            import json
            import re

            cluster_prompt = (
                "Analyze these user queries that the system couldn't understand. "
                "If they share a common intent pattern, propose a new intent.\n\n"
                "Queries:\n" + "\n".join(f"- {q}" for q in queries[-10:]) + "\n\n"
                "Return JSON:\n"
                '{"has_pattern": true/false, '
                '"intent_name": "SNAKE_CASE_NAME", '
                '"description": "What this intent does", '
                '"examples": ["example 1", "example 2", "example 3"], '
                '"handler_steps": ["step 1", "step 2"]}\n'
                "If no clear pattern, set has_pattern to false."
            )

            raw = await hybrid_route_query(
                user_prompt=cluster_prompt,
                system_prompt="You analyze user query patterns to discover new intents. Return ONLY valid JSON.",
                prefer_local=False,  # Use Gemini for quality
            )
            text = re.sub(r"```json|```", "", raw.strip() if isinstance(raw, str) else "").strip()
            result = json.loads(text) if isinstance(raw, str) else raw

            if result.get("has_pattern"):
                blueprint = IntentBlueprint(
                    name=result["intent_name"],
                    description=result["description"],
                    examples=result.get("examples", []),
                    handler_steps=result.get("handler_steps", []),
                    confidence=len(self._gaps) / 10.0,  # Higher with more evidence
                )
                self._proposed.append(blueprint)
                # Clear processed gaps
                self._gaps.clear()
                logger.info(
                    "[IntentDiscovery] Proposed new intent: %s (%s)",
                    blueprint.name, blueprint.description,
                )
                return blueprint
        except Exception as e:
            logger.warning("[IntentDiscovery] Clustering failed: %s", e)

        return None

    def clear_gaps(self) -> None:
        """Reset gap tracking."""
        self._gaps.clear()


# Singleton instance
intent_discovery = IntentDiscoveryEngine()
```

- [ ] **Step 2: Wire Intent Discovery into the intent classification flow**

In `Jarvis-Engine/app/services/intent_registry.py`, find the `_fallback_single_intent` function (or wherever CHAT fallback happens). After the intent is classified, if the result is CHAT, record the gap:

```python
# At the top of intent_registry.py, add:
from app.services.intent_discovery import intent_discovery

# In the CHAT fallback handler or classification logic:
async def _handle_chat(ctx: Any) -> dict:
    """General conversation fallback — also records gap for Intent Discovery."""
    # Record this as a potential gap
    intent_discovery.record_gap(ctx.user_prompt, max_similarity=0.0)

    # Check if we should propose a new intent
    if intent_discovery.gap_count >= 3:
        proposal = await intent_discovery.check_and_propose()
        if proposal:
            # Store as a memory for the user
            if ctx.memory_store:
                ctx.memory_store.store_memory(ctx.user_id, {
                    "type": "feedback",
                    "content": f"Jarvis discovered a potential new intent: {proposal.name} — {proposal.description}",
                    "source": "behavior",
                })

    # Original CHAT handler logic
    from app.services.analytical.voice_of_jarvis import synthesize_jarvis_response
    message, thinking_process = await synthesize_jarvis_response(
        {"chat": True, "user_request": ctx.user_prompt}
    )
    return {
        "intent": "CHAT",
        "message": message,
        "thinking_process": thinking_process,
    }
```

- [ ] **Step 3: Commit**

```bash
git add app/services/intent_discovery.py app/services/intent_registry.py
git commit -m "feat: Intent Discovery Engine — gap detection + clustering + blueprint proposals"
```

---

### Task 6: End-to-End Verification

- [ ] **Step 1: Test workspace with criteria from backend**

1. Plan a day in chat → accept the draft
2. Navigate to /schedule → click a task → verify workspace shows completion criteria from backend (not empty)
3. Verify WOOP section shows obstacle_trigger / behavioral_response

- [ ] **Step 2: Test document handler — upload practice problems PDF**

1. Go to /documents → upload a practice problems PDF
2. Check backend logs for `[DocumentHandler] practice_problems: extracted`
3. Check Supabase `extracted_problems` table for rows

- [ ] **Step 3: Test Intent Discovery**

1. Send 3+ queries that don't match any intent (e.g., "what's the weather", "tell me a joke", "play music")
2. Check backend logs for `[IntentDiscovery] Gap recorded` and eventually `[IntentDiscovery] Proposed new intent`

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "test: verify workspace + document intelligence + intent discovery"
```
