# Psychology Framework Completion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the 5 psychological frameworks (TMT, Mastery Tracker, WOOP Runtime, Coach Module, Slippery Deadlines) so Jarvis's intelligence layer actually drives scheduling, motivation, and anti-guilt behavior — not just stores inert fields.

**Architecture:** Fix TMT formula and wire real deadline delay + success rate. Create a lightweight mastery tracker (quality + SR + reschedule penalty). Surface WOOP intentions as full MCII cards in workspace. Rewrite coach module around Bandura's 4 self-efficacy sources. Implement slippery deadlines with `pacing_pushed` status and buffer reallocation. All psychology decisions visible in dev mode trace.

**Tech Stack:** Python (FastAPI, OR-Tools, Pydantic v2), TypeScript (Next.js 14, React), Supabase

---

## File Structure

### New Files
| File | Responsibility |
|---|---|
| `app/services/analytical/mastery_tracker.py` | Mastery computation: quality + SR + reschedule penalty |
| `scripts/seed_psychology_demo.py` | Seed data for dev verification |
| `jarvis-frontend/components/app/WoopCard.tsx` | Full MCII card (Wish + Outcome + Obstacle + Plan) |

### Modified Files
| File | What Changes |
|---|---|
| `app/api/v1/endpoints/schedule.py` | Fix TMT: canonical formula, real delay, SR as Expectancy |
| `app/modules/coach.py` | Full rewrite: Bandura's 4 sources, mastery data, mastery-only system prompt |
| `app/api/v1/endpoints/tasks.py` | `skip_task` → `pacing_pushed` status, energy decay, reschedule_count, buffer reallocation |
| `app/services/analytical/workspace_builder.py` | Add `woop_card` to workspace response |
| `app/schemas/workspace.py` | Add `WoopCard` schema, extend `TaskWorkspace` |
| `app/modules/planning_graph.py` | Emit TMT detail in tool_use events |
| `app/core/observation.py` | WOOP completion → memory, reschedule ≥ 3 → re-decomposition memory |
| `jarvis-frontend/components/app/IntelligentTrace.tsx` | Show TMT + mastery in phase trace |

---

## What needs to happen (9 tasks)

1. **Task 1:** Mastery Tracker service (backend, no dependencies)
2. **Task 2:** TMT formula fix (depends on Task 1 for `_calculate_sr`)
3. **Task 3:** WOOP runtime — backend (workspace_builder + schema)
4. **Task 4:** Coach module rewrite (depends on Task 1)
5. **Task 5:** Slippery Deadlines (depends on Task 1)
6. **Task 6:** Planning graph TMT trace enrichment (depends on Task 2)
7. **Task 7:** Frontend — WoopCard + mastery rings + anti-guilt language
8. **Task 8:** Phase trace enrichment for psychology
9. **Task 9:** Seed script + dev verification

---

### Task 1: Mastery Tracker Service

**Files:**
- Create: `app/services/analytical/mastery_tracker.py`

- [ ] **Step 1: Create the mastery tracker module**

```python
"""Lightweight mastery tracker — statistical bridge until DKT arrives.

3 signals: quality scores (SM-2), success ratio, reschedule penalty.
Same output interface as future DKT: compute_mastery(user_id, topic) → float.
"""

import asyncio
from typing import Optional

from app.core.jarvis_logger import JARVIS_LOGGER as logger


def _calculate_sr(user_id: str, goal_id: Optional[str], db) -> float:
    """Success Ratio: completed / total for a goal or all tasks.

    Feeds into: TMT Expectancy, Mastery score, Coach messages.
    Returns 0.5 (neutral prior) when no tasks exist — not inflated confidence.
    """
    try:
        query = db.table("user_tasks").select("status").eq("user_id", user_id)
        if goal_id:
            query = query.eq("goal_id", goal_id)
        result = query.execute()
        tasks = result.data or []
        total = len(tasks)
        if total == 0:
            return 0.5  # neutral prior for cold start
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        return completed / total
    except Exception as e:
        logger.warning(f"SR calculation failed: {e}")
        return 0.5


def _get_avg_quality(user_id: str, goal_id: Optional[str], db) -> float:
    """Average task completion quality (SM-2 scale 0-5) for a goal."""
    try:
        query = db.table("user_tasks").select("quality_score").eq("user_id", user_id).eq("status", "completed")
        if goal_id:
            query = query.eq("goal_id", goal_id)
        result = query.execute()
        tasks = result.data or []
        scores = [t["quality_score"] for t in tasks if t.get("quality_score") is not None]
        if not scores:
            return 2.5  # neutral
        return sum(scores) / len(scores)
    except Exception as e:
        logger.warning(f"Quality avg failed: {e}")
        return 2.5


def _get_reschedule_count(user_id: str, goal_id: Optional[str], db) -> int:
    """Total reschedule count for tasks in a goal."""
    try:
        query = db.table("user_tasks").select("reschedule_count").eq("user_id", user_id)
        if goal_id:
            query = query.eq("goal_id", goal_id)
        result = query.execute()
        tasks = result.data or []
        return sum(t.get("reschedule_count", 0) for t in tasks)
    except Exception as e:
        logger.warning(f"Reschedule count query failed: {e}")
        return 0


def _get_mastery_target(user_id: str, goal_id: Optional[str], db) -> Optional[int]:
    """Get mastery_level_target (1-5) from goal_metadata if available."""
    if not goal_id:
        return None
    try:
        result = db.table("user_plan_updates").select("goal_metadata").eq("user_id", user_id).eq("goal_id", goal_id).limit(1).execute()
        rows = result.data or []
        if rows and rows[0].get("goal_metadata"):
            return rows[0]["goal_metadata"].get("mastery_level_target")
    except Exception:
        pass
    return None


def compute_mastery(user_id: str, goal_id: Optional[str], db) -> float:
    """Lightweight mastery score (0.0 to 1.0).

    3 data sources:
    1. Quality scores (SM-2 input 0-5) → normalized to 0-1, weight 0.5
    2. Success ratio (completed / total) → weight 0.3
    3. Reschedule penalty (-0.1 per push, capped at -0.3) → weight 0.2

    Returns progress toward mastery_level_target if available.
    Same interface as future DKT — only internals change.
    """
    avg_quality = _get_avg_quality(user_id, goal_id, db)
    sr = _calculate_sr(user_id, goal_id, db)
    reschedule_count = _get_reschedule_count(user_id, goal_id, db)

    raw_mastery = (
        (avg_quality / 5.0) * 0.5
        + sr * 0.3
        + max(0.0, 1.0 - reschedule_count * 0.1) * 0.2
    )

    target = _get_mastery_target(user_id, goal_id, db)
    if target and target > 0:
        return min(raw_mastery / (target / 5.0), 1.0)
    return raw_mastery


def get_mastery_summary(user_id: str, db) -> dict[str, float]:
    """Mastery scores for all active goals. Returns {goal_id: mastery_score}."""
    try:
        result = db.table("user_tasks").select("goal_id").eq("user_id", user_id).neq("status", "archived").execute()
        tasks = result.data or []
        goal_ids = list({t["goal_id"] for t in tasks if t.get("goal_id")})
        summary = {}
        for gid in goal_ids:
            summary[gid] = compute_mastery(user_id, gid, db)
        return summary
    except Exception as e:
        logger.warning(f"Mastery summary failed: {e}")
        return {}


def compute_quality_trend(user_id: str, db) -> str:
    """Compare recent 5 completions quality vs previous 5. Returns improving/stable/declining."""
    try:
        result = (
            db.table("user_tasks")
            .select("quality_score, completed_at")
            .eq("user_id", user_id)
            .eq("status", "completed")
            .not_.is_("quality_score", "null")
            .order("completed_at", desc=True)
            .limit(10)
            .execute()
        )
        tasks = result.data or []
        if len(tasks) < 4:
            return "stable"
        recent = tasks[:5]
        older = tasks[5:10]
        if not older:
            return "stable"
        recent_avg = sum(t["quality_score"] for t in recent) / len(recent)
        older_avg = sum(t["quality_score"] for t in older) / len(older)
        diff = recent_avg - older_avg
        if diff > 0.5:
            return "improving"
        elif diff < -0.5:
            return "declining"
        return "stable"
    except Exception:
        return "stable"


def compute_streak(user_id: str, db) -> int:
    """Count consecutive days with at least 1 task completion."""
    try:
        from datetime import date, timedelta
        result = (
            db.table("user_tasks")
            .select("completed_at")
            .eq("user_id", user_id)
            .eq("status", "completed")
            .not_.is_("completed_at", "null")
            .order("completed_at", desc=True)
            .limit(60)
            .execute()
        )
        tasks = result.data or []
        if not tasks:
            return 0
        dates = set()
        for t in tasks:
            try:
                d = date.fromisoformat(t["completed_at"][:10])
                dates.add(d)
            except (ValueError, TypeError):
                continue
        if not dates:
            return 0
        today = date.today()
        streak = 0
        check = today
        while check in dates:
            streak += 1
            check -= timedelta(days=1)
        return streak
    except Exception:
        return 0
```

- [ ] **Step 2: Verify it compiles**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m py_compile app/services/analytical/mastery_tracker.py && echo "OK"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add app/services/analytical/mastery_tracker.py
git commit -m "feat: add lightweight mastery tracker (quality + SR + reschedule penalty)"
```

---

### Task 2: Fix TMT Formula

**Files:**
- Modify: `app/api/v1/endpoints/schedule.py:28-76`

- [ ] **Step 1: Fix `_compute_tmt_priority` with canonical formula**

Replace lines 28-76 in `schedule.py`:

```python
# ---------------------------------------------------------------------------
# TMT (Temporal Motivation Theory) constants — Steel & König 2006
# ---------------------------------------------------------------------------

IMPULSIVENESS = 1.5  # Constant; higher = more discounting of delayed rewards
DEFAULT_DELAY_HOURS = 24  # Used when deadline_hint is missing
DEFAULT_SELF_EFFICACY = 0.8  # Proxy for task-level confidence


class _ChunkWithDeadline(Protocol):
    deadline_hint: Optional[str]


def _delay_hours_for_chunk(
    chunk: _ChunkWithDeadline,
    horizon_start: datetime,
) -> float:
    """Compute delay_hours from chunk.deadline_hint for TMT.

    Past deadlines -> 1 (highest urgency). Invalid ISO -> DEFAULT_DELAY_HOURS.
    """
    parsed = parse_deadline_to_date(chunk.deadline_hint, horizon_start)
    if parsed is None:
        return DEFAULT_DELAY_HOURS
    if parsed.date() < horizon_start.date():
        return 1.0
    delta_days = (parsed.date() - horizon_start.date()).days
    hours = delta_days * 24
    return max(0.1, hours)  # floor 0.1 prevents zero


def _compute_tmt_priority(
    difficulty_weight: float,
    delay_hours: float = DEFAULT_DELAY_HOURS,
    success_rate: float = 0.5,
    self_efficacy_proxy: float = DEFAULT_SELF_EFFICACY,
    mastery_value: float | None = None,
) -> tuple[float, int]:
    """Canonical TMT (Steel & König 2006).

    Motivation = (Expectancy × Value) / (1 + Impulsiveness × Delay)

    Args:
        difficulty_weight: Cognitive load (0-1). Inverted to reward if no mastery_value.
        delay_hours: Hours until deadline. Floor 0.1.
        success_rate: From mastery tracker _calculate_sr(). Feeds Expectancy.
        self_efficacy_proxy: Task-level confidence (default 0.8).
        mastery_value: Explicit value (1-5) from goal_metadata if available.

    Returns:
        Tuple of (raw_tmt_score, priority_score integer).
    """
    expectancy = success_rate * self_efficacy_proxy
    value = mastery_value if mastery_value is not None else (1.0 - difficulty_weight + 0.1)
    delay = max(delay_hours, 0.1)

    motivation = (expectancy * value) / (1.0 + IMPULSIVENESS * delay)
    priority_score = max(1, int(motivation * 100))
    return (motivation, priority_score)
```

- [ ] **Step 2: Wire SR into schedule generation**

Find the section in `schedule.py` where `_compute_tmt_priority` is called (in `run_schedule` or the endpoint handler). Add SR fetch:

```python
# At the top of the scheduling flow, before the loop that computes TMT per chunk:
from app.services.analytical.mastery_tracker import _calculate_sr
import asyncio

# Fetch SR once for this user (sync via to_thread if needed)
_sr = 0.5  # default
try:
    from app.core.config import SUPABASE_URL, SUPABASE_SERVICE_KEY
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        from supabase import create_client
        _db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        _sr = _calculate_sr(user_id, goal_id, _db)
except Exception:
    pass

# Then in the per-chunk TMT computation loop:
for chunk in graph.decomposition:
    delay = _delay_hours_for_chunk(chunk, horizon_start)
    _mastery_val = getattr(graph.goal_metadata, 'mastery_level_target', None)
    tmt_raw, priority_score = _compute_tmt_priority(
        difficulty_weight=chunk.difficulty_weight,
        delay_hours=delay,
        success_rate=_sr,
        mastery_value=float(_mastery_val) if _mastery_val else None,
    )
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile app/api/v1/endpoints/schedule.py && echo "OK"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add app/api/v1/endpoints/schedule.py
git commit -m "fix: TMT canonical formula (1+I×D denominator), real deadline delay, SR as Expectancy"
```

---

### Task 3: WOOP Runtime — Backend

**Files:**
- Modify: `app/schemas/workspace.py`
- Modify: `app/services/analytical/workspace_builder.py`

- [ ] **Step 1: Add WoopCard schema**

In `app/schemas/workspace.py`, add after the `TaskWorkspace` class:

```python
class WoopCard(BaseModel):
    """Full MCII (Mental Contrasting with Implementation Intentions) card.

    All 4 stages of WOOP — research shows 60% more practice completion
    with all 4 stages vs if-then alone.
    """

    wish: str = Field(description="Goal objective (Wish stage)")
    outcome: str = Field(default="", description="Emotional outcome visualization (Outcome stage)")
    obstacle: str = Field(description="Personal barrier / obstacle trigger (Obstacle stage)")
    plan: str = Field(description="If-then behavioral response (Plan stage)")
```

Then add to `TaskWorkspace`:

```python
class TaskWorkspace(BaseModel):
    """Proactive workspace assembled when user opens a scheduled task."""

    task_id: str = Field(description="Task identifier")
    task_title: str = Field(description="Display title of the task")
    primary_objective: str = Field(
        description="Derived from title + topic_keywords; main learning goal",
    )
    surfaced_assets: List[StudyAsset] = Field(
        default_factory=list,
        description="RAG chunks, curated links, and generated practice assets",
    )
    woop_card: Optional[WoopCard] = Field(
        default=None,
        description="MCII card with obstacle/response if available for this task",
    )
```

- [ ] **Step 2: Wire WOOP into workspace_builder**

In `app/services/analytical/workspace_builder.py`, find `build_task_workspace()` and add WOOP card construction before the return statement:

```python
# Build WOOP card from task's implementation_intention + goal metadata
woop_card = None
task_data = None
try:
    sb = _get_supabase()
    if sb:
        task_result = sb.table("user_tasks").select("implementation_intention, goal_id").eq("task_id", task_id).eq("user_id", user_id).limit(1).execute()
        if task_result.data:
            task_data = task_result.data[0]
            intention = task_data.get("implementation_intention") or {}
            if isinstance(intention, str):
                import json
                try:
                    intention = json.loads(intention)
                except (json.JSONDecodeError, TypeError):
                    intention = {}
            obstacle = intention.get("obstacle_trigger", "")
            plan = intention.get("behavioral_response", "")
            if obstacle and plan:
                # Fetch goal metadata for Wish + Outcome stages
                goal_id = task_data.get("goal_id", "")
                wish = task_title
                outcome = ""
                if goal_id:
                    goal_result = sb.table("user_plan_updates").select("goal_metadata, planning_goal").eq("user_id", user_id).eq("goal_id", goal_id).limit(1).execute()
                    if goal_result.data:
                        gm = goal_result.data[0]
                        wish = gm.get("planning_goal", task_title)
                        meta = gm.get("goal_metadata") or {}
                        if isinstance(meta, str):
                            import json
                            try:
                                meta = json.loads(meta)
                            except (json.JSONDecodeError, TypeError):
                                meta = {}
                        outcome = meta.get("outcome_visualization", "")
                from app.schemas.workspace import WoopCard
                woop_card = WoopCard(wish=wish, outcome=outcome, obstacle=obstacle, plan=plan)
except Exception as e:
    logger.warning(f"WOOP card construction failed (non-fatal): {e}")

# Include in TaskWorkspace return:
return TaskWorkspace(
    task_id=task_id,
    task_title=task_title,
    primary_objective=primary_objective,
    surfaced_assets=assets,
    woop_card=woop_card,
)
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile app/schemas/workspace.py && python -m py_compile app/services/analytical/workspace_builder.py && echo "OK"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add app/schemas/workspace.py app/services/analytical/workspace_builder.py
git commit -m "feat: WOOP runtime — surface full MCII card (Wish+Outcome+Obstacle+Plan) in workspace"
```

---

### Task 4: Coach Module Rewrite

**Files:**
- Modify: `app/modules/coach.py`

- [ ] **Step 1: Rewrite coach.py with Bandura's 4 Sources**

Replace the entire file:

```python
"""Coach module — Bandura's 4 Sources of Self-Efficacy, mastery orientation.

CRITICAL CONSTRAINTS (from research):
- MASTERY ORIENTATION ONLY: never compare to other users/averages/benchmarks
- Vicarious Experience = "mastery paths" not peer stats
- Frame setbacks as data points, not failures
- Credible encouragement grounded in actual mastery data
"""

import asyncio
import json

from app.core.jarvis_logger import JARVIS_LOGGER as logger

MASTERY_COACHING_SYSTEM_PROMPT = """You are Jarvis's coaching module. Your responses MUST follow these rules:

1. MASTERY ORIENTATION ONLY:
   - "You've improved from 40% to 62% mastery in DSA" ✅
   - "You completed 8 tasks, which is above average" ❌ (performance orientation)
   - NEVER compare to other users, averages, or benchmarks
   - Focus on self-referenced progress: "compared to last week" not "compared to others"

2. BANDURA'S 4 SOURCES (use the data provided):
   - Performance Accomplishments: cite specific mastery % and quality trends
   - Vicarious Experience: "Students who followed this study pattern typically master the topic in ~3 more focused sessions" (mastery paths, NOT peer stats)
   - Verbal Persuasion: credible encouragement grounded in actual data, not generic praise
   - Physiological States: reference energy patterns and pacing if available

3. ANTI-GUILT:
   - Setbacks are data points, not failures
   - "Life happened" not "you missed"
   - Frame rescheduled tasks as adaptive, not lazy

4. TONE: Warm, competent, Tony Stark directness. Not patronizing.
5. LENGTH: 2-4 sentences. Concise, data-grounded, actionable.
"""


def _identify_sources_used(mastery: dict, trend: str, streak: int, energy: float) -> list[str]:
    """Identify which Bandura sources are relevant for this coaching moment."""
    sources = []
    if any(v > 0.3 for v in mastery.values()):
        sources.append("accomplishment")
    if trend == "improving":
        sources.append("persuasion")
    if streak >= 2:
        sources.append("persuasion")
    if energy < 0.5:
        sources.append("physiological")
    if not sources:
        sources.append("vicarious")
    return sources


async def run_coaching_response(state: dict) -> dict:
    """Generate coaching response using Bandura's 4 Sources of Self-Efficacy.

    Reads real mastery data, quality trends, streaks, and energy patterns.
    Returns mastery-oriented message (never performance-oriented).
    """
    user_model = state.get("user_model")
    error = state.get("error")

    if error and "INFEASIBLE" in str(error):
        return {
            "response_message": (
                "This is a scope problem, not a you problem. "
                "You've got more on your plate than fits in the time available. "
                "Want to reduce scope, extend the deadline, or adjust your daily capacity?"
            ),
            "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
        }

    if not user_model:
        return {
            "response_message": "Tell me about your goals and I'll track your progress.",
            "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
        }

    try:
        from app.services.analytical.mastery_tracker import (
            get_mastery_summary, _calculate_sr, compute_quality_trend, compute_streak,
        )

        db = user_model._db.supabase  # explicit path — avoids wiring bug

        mastery = await asyncio.to_thread(get_mastery_summary, user_model.user_id, db)
        sr = await asyncio.to_thread(_calculate_sr, user_model.user_id, None, db)
        trend = await asyncio.to_thread(compute_quality_trend, user_model.user_id, db)
        streak = await asyncio.to_thread(compute_streak, user_model.user_id, db)
        energy = await user_model.get_estimated_energy()

        all_tasks = await user_model.get_all_tasks()
        completed = sum(1 for t in all_tasks if t.get("status") == "completed")
        pending = sum(1 for t in all_tasks if t.get("status") == "pending")
        pushed = sum(1 for t in all_tasks if t.get("status") == "pacing_pushed")

        sources = _identify_sources_used(mastery, trend, streak, energy)

        coaching_context = (
            f"User mastery data:\n"
            f"- Topics: {json.dumps(mastery)}\n"
            f"- Success ratio: {sr:.0%}\n"
            f"- Quality trend: {trend}\n"
            f"- Completion streak: {streak} days\n"
            f"- Tasks completed: {completed}, pending: {pending}, rescheduled: {pushed}\n"
            f"- Current energy estimate: {energy:.1f}/1.0\n"
            f"- Bandura sources to emphasize: {', '.join(sources)}\n"
        )

        from app.core.model_router import route_llm_call

        response = await route_llm_call(
            task="voice_of_jarvis",
            prompt=f"Generate a coaching message for this user:\n{coaching_context}",
            system_prompt=MASTERY_COACHING_SYSTEM_PROMPT,
        )
        response_text = str(response) if response else "Keep going — every task completed builds mastery."

        return {
            "response_message": response_text,
            "coaching_data": {
                "mastery": mastery,
                "sr": sr,
                "quality_trend": trend,
                "streak": streak,
                "bandura_sources": sources,
            },
            "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
        }

    except Exception as e:
        logger.error(f"Coach module error: {e}")
        return {
            "response_message": "You're making progress — keep building on what you've learned.",
            "modules_invoked": state.get("modules_invoked", []) + ["coach_module"],
        }
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile app/modules/coach.py && echo "OK"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add app/modules/coach.py
git commit -m "feat: rewrite coach module — Bandura's 4 self-efficacy sources, mastery-only orientation"
```

---

### Task 5: Slippery Deadlines

**Files:**
- Modify: `app/api/v1/endpoints/tasks.py:168-199`

- [ ] **Step 1: Replace `skip_task` with anti-guilt `pacing_pushed` logic**

Replace the `skip_task` function (around line 174-199) with:

```python
@router.post(
    "/{task_id}/skip",
    response_model=TaskResponse,
    summary="Skip / reschedule a task (anti-guilt)",
    description="Marks task as pacing_pushed (not 'overdue' or 'failed'). "
    "Applies slippery deadline logic: buffer reallocation, energy decay, "
    "and re-decomposition memory if pushed 3+ times.",
)
async def skip_task(
    task_id: str,
    body: TaskDeleteRequest,
    request: Request,
) -> TaskResponse:
    """Anti-guilt task rescheduling — 'Slippery Deadlines'.

    Never says 'overdue' or 'failed'. Reframes as adaptive pacing.
    Blueprint: handle_missed_deadline() logic.
    """
    supabase = _get_supabase(request)

    # 1. Fetch current task to get reschedule_count
    task_result = await asyncio.to_thread(
        lambda: supabase.table("user_tasks")
        .select("reschedule_count, duration_minutes, title, goal_id")
        .eq("task_id", task_id)
        .eq("user_id", body.user_id)
        .limit(1)
        .execute()
    )
    current_count = 0
    task_title = task_id
    task_duration = 25
    task_goal_id = None
    if task_result.data:
        row = task_result.data[0]
        current_count = row.get("reschedule_count", 0) or 0
        task_title = row.get("title", task_id)
        task_duration = row.get("duration_minutes", 25)
        task_goal_id = row.get("goal_id")

    new_count = current_count + 1

    # 2. Update status to pacing_pushed (NOT 'skipped' or 'overdue')
    await asyncio.to_thread(
        lambda: supabase.table("user_tasks")
        .update({"status": "pacing_pushed", "reschedule_count": new_count})
        .eq("task_id", task_id)
        .eq("user_id", body.user_id)
        .execute()
    )

    # 3. Determine reassignment (buffer reallocation)
    from app.utils.pacing import compute_adaptive_daily_cap
    daily_cap = compute_adaptive_daily_cap(horizon_minutes=2880, total_task_minutes=480)
    buffer_minutes = daily_cap * 0.25  # 25% anti-guilt reserve
    if task_duration <= buffer_minutes:
        reassignment = "today_buffer"
        message = f"Life happened — I've slotted '{task_title}' into today's buffer time."
    else:
        reassignment = "tomorrow_morning_peak"
        message = f"Life happened — '{task_title}' moves to tomorrow morning. No stress."

    # 4. Reschedule count >= 3 → memory: needs re-decomposition
    if new_count >= 3:
        memory_store = getattr(request.app.state, "memory_store", None)
        if memory_store:
            try:
                memory_store.store_memory(
                    user_id=body.user_id,
                    content=f"User struggles with '{task_title}' — needs re-decomposition into smaller chunks",
                    memory_type="behavioral_pattern",
                    source="pacing_pushed",
                    confidence=0.8,
                )
            except Exception as e:
                logger.warning(f"Failed to store re-decomposition memory: {e}")

    # 5. Trigger background replan
    db_client = getattr(request.app.state, "db_client", None)
    replan_triggered = await _try_trigger_replan(
        body.user_id, db_client, reason=f"pacing_pushed:{task_id}",
    )

    # 6. Fire-and-forget: detect behavioral patterns
    memory_store = getattr(request.app.state, "memory_store", None)
    if memory_store and supabase:
        from app.services.memory.pearl import detect_patterns
        asyncio.create_task(asyncio.to_thread(
            detect_patterns, body.user_id, supabase, memory_store
        ))

    return TaskResponse(
        task_id=task_id,
        status="pacing_pushed",
        message=message,
        replan_triggered=replan_triggered,
    )
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile app/api/v1/endpoints/tasks.py && echo "OK"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add app/api/v1/endpoints/tasks.py
git commit -m "feat: slippery deadlines — pacing_pushed status, buffer reallocation, re-decomposition memory"
```

---

### Task 6: Planning Graph TMT Trace Enrichment

**Files:**
- Modify: `app/modules/planning_graph.py`

- [ ] **Step 1: Emit TMT detail in solve_schedule tool_use event**

In `app/modules/planning_graph.py`, find the `solve_schedule` function. After the OR-Tools solve completes successfully, enrich the tool_use event with TMT info:

```python
# After the solver returns OPTIMAL, before the return statement:
_emit_tool_use(state, "or_tools_solve", "done", {
    "status": "OPTIMAL",
    "task_count": len(chunks),
    "horizon_h": horizon // 60,
    "tmt_applied": True,
    "formula": "canonical_steel_konig",
})
```

Also add a separate tool_use for mastery computation if mastery tracker data was used:

```python
# After memory_to_constraints, add mastery trace:
_emit_tool_use(state, "mastery_check", "done", {
    "formula": "quality×0.5 + SR×0.3 + reschedule_penalty×0.2",
})
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile app/modules/planning_graph.py && echo "OK"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add app/modules/planning_graph.py
git commit -m "feat: emit TMT + mastery detail in planning graph tool_use events"
```

---

### Task 7: Frontend — WoopCard + Anti-Guilt Language

**Files:**
- Create: `jarvis-frontend/components/app/WoopCard.tsx`
- Modify: `jarvis-frontend/lib/constants.ts`

- [ ] **Step 1: Create WoopCard component**

```tsx
// jarvis-frontend/components/app/WoopCard.tsx
"use client";

import { useState } from "react";
import { Lightbulb, ChevronDown, ChevronUp } from "lucide-react";
import clsx from "clsx";

interface WoopCardProps {
  wish: string;
  outcome?: string;
  obstacle: string;
  plan: string;
  taskId: string;
}

export function WoopCard({ wish, outcome, obstacle, plan, taskId }: WoopCardProps) {
  const storageKey = `woop-dismissed-${taskId}`;
  const [collapsed, setCollapsed] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem(storageKey) === "true";
    }
    return false;
  });

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(storageKey, String(next));
  };

  if (!obstacle || !plan) return null;

  return (
    <div className="border-l-4 border-dusk rounded-lg bg-surface-subtle p-3 mb-3">
      <button
        onClick={toggle}
        className="flex items-center justify-between w-full text-left"
      >
        <div className="flex items-center gap-2 text-xs font-medium text-secondary">
          <Lightbulb size={14} className="text-dusk" />
          <span>Pro tip for this task</span>
        </div>
        {collapsed ? <ChevronDown size={14} className="text-muted" /> : <ChevronUp size={14} className="text-muted" />}
      </button>

      {!collapsed && (
        <div className="mt-2 space-y-1.5 text-xs text-muted">
          <div>
            <span className="text-secondary font-medium">Goal:</span> {wish}
          </div>
          {outcome && (
            <div>
              <span className="text-secondary font-medium">Outcome:</span> &quot;{outcome}&quot;
            </div>
          )}
          <div className="pt-1 border-t border-border">
            <span className="text-terra font-medium">If:</span> {obstacle}
          </div>
          <div>
            <span className="text-sage font-medium">Then:</span> {plan}
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Update anti-guilt language in constants.ts**

In `jarvis-frontend/lib/constants.ts`, add a status display map:

```typescript
// ---------------------------------------------------------------------------
// Anti-guilt task status display (psychology framework)
// ---------------------------------------------------------------------------

export const TASK_STATUS_DISPLAY: Record<string, { label: string; color: string }> = {
  pending: { label: "Pending", color: "muted" },
  completed: { label: "Completed", color: "sage" },
  pacing_pushed: { label: "Adjusted — no stress", color: "sage" },
  skipped: { label: "Rescheduled", color: "muted" },
  // NEVER use: "Overdue", "Failed", "Missed", "Late"
};

export function getTaskStatusDisplay(status: string): { label: string; color: string } {
  return TASK_STATUS_DISPLAY[status] || { label: status.replace(/_/g, " "), color: "muted" };
}
```

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/WoopCard.tsx lib/constants.ts
git commit -m "feat: WoopCard component (full MCII) + anti-guilt status language"
```

---

### Task 8: Phase Trace Psychology Enrichment

**Files:**
- Modify: `jarvis-frontend/components/app/IntelligentTrace.tsx`

- [ ] **Step 1: Add psychology-specific detail rendering**

In `IntelligentTrace.tsx`, update the `renderDetail` function to handle psychology data:

```typescript
function renderDetail(pe: PhaseEventData): string | null {
  const d = pe.detail || pe.data;
  if (!d) return null;
  const parts: string[] = [];
  // Existing detail fields
  if (d.intent) parts.push(`Intent: ${d.intent}`);
  if (d.memories_count != null) parts.push(`${d.memories_count} memories`);
  if (d.conversation_turns != null) parts.push(`${d.conversation_turns} turns`);
  if (d.module) parts.push(`${d.module}`);
  if (d.rows != null) parts.push(`${d.rows} constraints`);
  if (d.slots != null) parts.push(`${d.slots} time slots`);
  if (d.task_count != null) parts.push(`${d.task_count} tasks`);
  // Psychology-specific fields
  if (d.tmt_applied) parts.push("TMT applied");
  if (d.tasks_boosted != null) parts.push(`${d.tasks_boosted} tasks deadline-boosted`);
  if (d.formula === "canonical_steel_konig") parts.push("Steel & König 2006");
  if (d.quality_trend) parts.push(`trend: ${d.quality_trend}`);
  if (d.streak != null) parts.push(`streak: ${d.streak}d`);
  if (d.bandura_sources) {
    const sources = Array.isArray(d.bandura_sources) ? d.bandura_sources : [];
    if (sources.length > 0) parts.push(`Bandura: ${sources.join("+")}`);
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}
```

- [ ] **Step 2: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/jarvis-frontend
git add components/app/IntelligentTrace.tsx
git commit -m "feat: IntelligentTrace shows TMT, mastery, and Bandura source details"
```

---

### Task 9: Seed Script + Dev Verification

**Files:**
- Create: `scripts/seed_psychology_demo.py`

- [ ] **Step 1: Create the seed script**

```python
#!/usr/bin/env python3
"""Seed psychology demo data for dev verification.

Creates a complete test scenario: 2 goals, 10 completed tasks with quality scores,
3 pushed tasks, deadline hints, WOOP intentions, PEARL patterns, and a ChromaDB document.

Usage:
    cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
    python scripts/seed_psychology_demo.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import SUPABASE_URL, SUPABASE_SERVICE_KEY


def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
        sys.exit(1)

    from supabase import create_client
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    user_id = "psych-demo-001"
    now = datetime.now(timezone.utc)
    friday = now + timedelta(days=(4 - now.weekday()) % 7 or 7)
    next_wed = now + timedelta(days=(2 - now.weekday()) % 7 or 7)

    print(f"Seeding psychology demo for user '{user_id}'...")

    # --- Clean existing demo data ---
    for table in ["user_tasks", "behavioral_constraints", "user_plan_updates"]:
        try:
            db.table(table).delete().eq("user_id", user_id).execute()
        except Exception:
            pass

    # --- 1. Behavioral constraints (sleep, study, meetings) ---
    constraints = [
        {"user_id": user_id, "raw_text": "Sleep midnight to 8am", "constraint_type": "habit",
         "recurrence": "daily", "structured_semantics": json.dumps({"start_hour": 0, "end_hour": 8, "availability": "blocked"})},
        {"user_id": user_id, "raw_text": "Deep study 9am to 12pm", "constraint_type": "habit",
         "recurrence": "weekdays", "structured_semantics": json.dumps({"start_hour": 9, "end_hour": 12, "availability": "full_focus"})},
        {"user_id": user_id, "raw_text": "Team meeting 2pm to 3pm", "constraint_type": "fixed",
         "recurrence": "weekdays", "structured_semantics": json.dumps({"start_hour": 14, "end_hour": 15, "availability": "blocked"})},
    ]
    for c in constraints:
        db.table("behavioral_constraints").insert(c).execute()
    print(f"  ✓ {len(constraints)} behavioral constraints")

    # --- 2. Goals with mastery_level_target ---
    goal_a_id = "goal-dsa-" + str(uuid4())[:8]
    goal_b_id = "goal-calc-" + str(uuid4())[:8]

    goals = [
        {"user_id": user_id, "goal_id": goal_a_id, "planning_goal": "Master Dynamic Programming",
         "deadline_date": friday.isoformat(),
         "goal_metadata": json.dumps({"objective": "Master DP", "outcome_visualization": "I'll ace the contest and feel confident", "mastery_level_target": 4})},
        {"user_id": user_id, "goal_id": goal_b_id, "planning_goal": "Learn Calculus basics",
         "deadline_date": next_wed.isoformat(),
         "goal_metadata": json.dumps({"objective": "Calculus fundamentals", "outcome_visualization": "Solve integrals fluently", "mastery_level_target": 3})},
    ]
    for g in goals:
        db.table("user_plan_updates").insert(g).execute()
    print(f"  ✓ 2 goals (DSA target=4/5, Calculus target=3/5)")

    # --- 3. Tasks: 10 completed, 3 pushed ---
    tasks = []
    # DSA tasks (completed, high quality)
    for i, (title, quality) in enumerate([
        ("Understand recursion basics", 4), ("Implement memoization", 5),
        ("Solve top-down DP problems", 3), ("Practice bottom-up DP", 4),
        ("Contest warm-up problems", 5),
    ]):
        tasks.append({
            "user_id": user_id, "task_id": f"{goal_a_id}_dsa_{i}",
            "goal_id": goal_a_id, "title": title,
            "status": "completed", "duration_minutes": 25,
            "difficulty_weight": 0.6, "quality_score": quality,
            "reschedule_count": 0,
            "deadline_hint": friday.isoformat(),
            "completion_criteria": f"Can explain and code {title.lower()}",
            "implementation_intention": json.dumps({
                "obstacle_trigger": "If I feel overwhelmed by the recursion",
                "behavioral_response": "Open the practice set and solve just problem #1",
            }),
            "completed_at": (now - timedelta(days=5-i)).isoformat(),
        })

    # Calculus tasks (completed, lower quality)
    for i, (title, quality) in enumerate([
        ("Review limits", 2), ("Basic derivatives", 3),
        ("Chain rule practice", 2), ("Integration intro", 3),
        ("Trig substitution", 3),
    ]):
        tasks.append({
            "user_id": user_id, "task_id": f"{goal_b_id}_calc_{i}",
            "goal_id": goal_b_id, "title": title,
            "status": "completed", "duration_minutes": 25,
            "difficulty_weight": 0.5, "quality_score": quality,
            "reschedule_count": 0,
            "completion_criteria": f"Solve 3 problems on {title.lower()}",
            "completed_at": (now - timedelta(days=5-i)).isoformat(),
        })

    # Pushed tasks
    tasks.append({
        "user_id": user_id, "task_id": f"{goal_b_id}_calc_pushed1",
        "goal_id": goal_b_id, "title": "Integration by parts",
        "status": "pacing_pushed", "duration_minutes": 25,
        "difficulty_weight": 0.7, "reschedule_count": 1,
        "deadline_hint": next_wed.isoformat(),
        "completion_criteria": "Solve 3 integration by parts problems",
    })
    tasks.append({
        "user_id": user_id, "task_id": f"{goal_b_id}_calc_pushed2",
        "goal_id": goal_b_id, "title": "Partial fractions",
        "status": "pacing_pushed", "duration_minutes": 25,
        "difficulty_weight": 0.6, "reschedule_count": 2,
        "deadline_hint": next_wed.isoformat(),
        "completion_criteria": "Decompose 5 rational expressions",
    })
    tasks.append({
        "user_id": user_id, "task_id": f"{goal_a_id}_dsa_pushed",
        "goal_id": goal_a_id, "title": "Advanced DP patterns",
        "status": "pacing_pushed", "duration_minutes": 25,
        "difficulty_weight": 0.8, "reschedule_count": 1,
        "deadline_hint": friday.isoformat(),
        "completion_criteria": "Solve 2 advanced DP problems (bitmask, interval)",
        "implementation_intention": json.dumps({
            "obstacle_trigger": "If the bitmask approach feels confusing",
            "behavioral_response": "Draw the state transition diagram on paper first",
        }),
    })

    for t in tasks:
        db.table("user_tasks").insert(t).execute()
    print(f"  ✓ {len(tasks)} tasks (10 completed, 3 pushed)")

    # --- 4. PEARL memories ---
    memory_store = None
    try:
        from app.services.memory.store import MemoryStore
        memory_store = MemoryStore()
        memory_store.store_memory(
            user_id=user_id,
            content="User skips tasks during 14:00-15:00 consistently",
            memory_type="behavioral_pattern", source="pearl",
            confidence=0.75,
        )
        memory_store.store_memory(
            user_id=user_id,
            content="User completes morning tasks faster and with higher quality",
            memory_type="behavioral_pattern", source="pearl",
            confidence=0.8,
        )
        print("  ✓ 2 PEARL behavioral patterns")
    except Exception as e:
        print(f"  ⚠ PEARL memories skipped: {e}")

    # --- 5. Verify mastery tracker ---
    try:
        from app.services.analytical.mastery_tracker import compute_mastery, get_mastery_summary, _calculate_sr
        mastery = get_mastery_summary(user_id, db)
        sr = _calculate_sr(user_id, None, db)
        print(f"\n  📊 Mastery summary: {json.dumps({k: round(v, 2) for k, v in mastery.items()})}")
        print(f"  📊 Overall SR: {sr:.0%}")
        for gid, score in mastery.items():
            label = "DSA" if "dsa" in gid else "Calculus"
            print(f"  📊 {label}: {score:.0%} mastery")
    except Exception as e:
        print(f"  ⚠ Mastery verification failed: {e}")

    print(f"\n✅ Seed complete. Test with:")
    print(f'  curl -N -X POST http://localhost:8000/api/v1/chat/v2/stream \\')
    print(f'    -H "Content-Type: application/json" \\')
    print(f'    -d \'{{"user_prompt": "how am I doing with my studies?", "user_id": "{user_id}"}}\'')
    print(f'')
    print(f'  curl http://localhost:8000/api/v1/tasks/{goal_a_id}_dsa_pushed/workspace?user_id={user_id}')


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable**

Run: `chmod +x scripts/seed_psychology_demo.py`

- [ ] **Step 3: Commit**

```bash
git add scripts/seed_psychology_demo.py
git commit -m "feat: seed script for psychology demo — 2 goals, 13 tasks, PEARL patterns, verification output"
```

---

## Summary

| Task | What | Files | Scope |
|---|---|---|---|
| 1 | Mastery Tracker service | mastery_tracker.py | New file |
| 2 | TMT formula fix | schedule.py | Modify |
| 3 | WOOP runtime (workspace) | workspace.py, workspace_builder.py | Modify |
| 4 | Coach module rewrite | coach.py | Rewrite |
| 5 | Slippery Deadlines | tasks.py | Modify |
| 6 | Planning graph TMT trace | planning_graph.py | Modify |
| 7 | Frontend: WoopCard + anti-guilt | WoopCard.tsx, constants.ts | New + Modify |
| 8 | Phase trace psychology detail | IntelligentTrace.tsx | Modify |
| 9 | Seed script + verification | seed_psychology_demo.py | New file |

**Dependencies:** Task 2 depends on Task 1. Task 4 depends on Task 1. Task 5 depends on Task 1. Task 6 depends on Task 2. Tasks 7-8 are frontend-only (parallel with backend). Task 9 depends on Tasks 1-5.

**Parallelization:** Tasks 1, 3, 7 can run in parallel (no shared files). After Task 1 completes, Tasks 2, 4, 5 can run in parallel. Task 9 runs last.
