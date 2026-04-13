# Psychology Framework Completion — Design Spec

**Date:** 2026-04-13
**Author:** Madhav + Claude (Opus 4.6)
**Status:** Draft
**Research Sources:** `Jarvis AI Blueprint.pdf`, `Jarvis AI Day 1 Behavior Architecture.pdf`, `Implementation of Psychological Factors.pdf`

> **Core thesis:** Jarvis is not a planner — it's a "proactive preparation engine" that synthesizes behavioral science with deterministic execution. The psychology layer is the competitive moat: TMT drives prioritization, WOOP bridges intention to action, CLT caps cognitive load, self-efficacy fuels motivation, and anti-guilt prevents tool abandonment.

---

## Problem Statement

Five psychological frameworks are designed into Jarvis's architecture but incompletely implemented:

| Framework | Schema/Prompt | Runtime Logic | Frontend Visibility |
|---|---|---|---|
| TMT | ✅ Formula exists | ❌ Wrong denominator, hardcoded 24h delay | ❌ No visibility |
| WOOP | ✅ Fields in TaskChunk | ❌ Never surfaced at runtime | ❌ No UI |
| CLT | ✅ 25-min cap, intrinsic_load | ✅ Works | ❌ No visibility |
| Self-Efficacy | ❌ No mastery tracking | ❌ Coach is a stub | ❌ No mastery UI |
| Anti-Guilt | ✅ INFEASIBLE handling | ❌ No slippery deadlines | ❌ "Overdue" language |

The LLM fills psychology fields (WOOP intentions, completion criteria, difficulty weights) but the backend never acts on them. This spec closes that gap.

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| TMT denominator | `1 + I × D` (canonical Steel & König 2006) | Prevents division by zero, softens short-delay discounting |
| TMT delay source | Parse `deadline_hint` → real hours | Current 24h default makes all tasks equally urgent |
| TMT Value | `mastery_value` if available, else `1 - difficulty_weight + 0.1` | Blueprint uses mastery_value (1-5); fallback inverts cognitive load to reward |
| Mastery tracker | Statistical (quality + SR + reschedule penalty) | Sits between task counts and DKT; same output interface for future DKT swap |
| Mastery target | Progress toward `mastery_level_target` (1-5) from goal_metadata | Not absolute score — relative to user's stated ambition |
| SR computation | Single `_calculate_sr()` shared by TMT and mastery tracker | Blueprint defines this once; no duplication |
| WOOP display | Full 4-stage MCII card (Wish + Outcome + Obstacle + Plan) | Research shows 60% more practice completion with all 4 stages vs if-then alone |
| Coach persona | Mastery-oriented only; explicitly forbid peer comparison | Mastery goals predict 51% of Flow State variance; performance orientation → learned helplessness |
| Vicarious Experience | "Mastery paths" not peer stats | "Students who followed this pattern mastered it in ~3 sessions" OK; "Other users completed 8 tasks" NOT OK |
| Energy decay | 0.98 per miss (not Blueprint's 0.95) | 0.95 too punishing in testing |
| Reschedule threshold | 3 pushes → memory: "needs re-decomposition" | Repeated avoidance signals topic needs smaller chunks |
| Task miss language | "Adjusted" / "Rescheduled" / "Life happened" | Never "Overdue" / "Failed" / "Late" — prevents amygdala shame response |
| Dev verification | Seed script + dev mode trace + phase enrichment | Prove it works in one session without waiting for real user data |

---

## Components

### 1. TMT Fix (`app/api/v1/endpoints/schedule.py`, `app/services/analytical/mastery_tracker.py`)

**Current code (broken):**
```python
def _compute_tmt_priority(difficulty_weight, delay_hours=DEFAULT_DELAY_HOURS):
    value = difficulty_weight
    motivation = (EXPECTANCY * value) / (IMPULSIVENESS * delay_hours)
    # Bug 1: no +1 in denominator — division by zero when delay=0
    # Bug 2: delay_hours always 24 — ignores deadline_hint
    # Bug 3: EXPECTANCY is constant 1.0 — doesn't use success_rate
```

**Fixed implementation:**
```python
def _compute_tmt_priority(
    difficulty_weight: float,
    delay_hours: float,          # real hours until deadline
    success_rate: float = 1.0,   # from mastery tracker SR
    self_efficacy_proxy: float = 0.8,
    mastery_value: float | None = None,
) -> tuple[float, int]:
    """Canonical TMT (Steel & König 2006).
    
    Motivation = (Expectancy × Value) / (1 + Impulsiveness × Delay)
    """
    expectancy = success_rate * self_efficacy_proxy
    value = mastery_value if mastery_value is not None else (1.0 - difficulty_weight + 0.1)
    impulsiveness = 1.5
    delay = max(delay_hours, 0.1)  # floor prevents zero
    
    motivation = (expectancy * value) / (1.0 + impulsiveness * delay)
    priority_score = max(1, int(motivation * 100))
    return motivation, priority_score
```

**Delay computation:**
```python
def _compute_delay_hours(chunk: TaskChunk, horizon_end: datetime) -> float:
    """Parse deadline_hint into hours-until-deadline."""
    if chunk.deadline_hint:
        deadline = parse_deadline(chunk.deadline_hint)  # existing deadline_parser.py
        if deadline:
            return max((deadline - datetime.now(timezone.utc)).total_seconds() / 3600, 0.1)
    # Fallback: hours until horizon end
    return max((horizon_end - datetime.now(timezone.utc)).total_seconds() / 3600, 0.1)
```

**"Deadline Dopamine" effect:** Each micro-task chunk inherits the parent goal's deadline. If parent goal deadline is Friday, all 7 chunks have `delay = hours_until_friday`. This keeps Delay small across the entire project, maintaining motivation per TMT.

**Dev mode trace output (emitted as tool_use event):**
```
TMT: E=0.85 × V=7 / (1 + 1.5 × 2.3h) = 1.28 → priority=8
```

**Frontend indicator:** Tasks within 24h of deadline get `border-left: 3px solid` with color interpolated from `sage` (>48h) → `gold` (24h) → `terra` (< 6h). Applied to schedule task cards.

---

### 2. Lightweight Mastery Tracker (`app/services/analytical/mastery_tracker.py`)

**New service file.** Computes mastery per topic/goal without DKT.

**Shared SR computation (single source of truth):**
```python
def _calculate_sr(user_id: str, goal_id: str | None, db) -> float:
    """Success Ratio: completed / total for a goal or all tasks.
    
    Feeds into: TMT Expectancy, Mastery score, Coach messages.
    Same as Blueprint's JarvisCoreEngine._calculate_sr().
    """
    if goal_id:
        tasks = db.table("user_tasks").select("status").eq("user_id", user_id).eq("goal_id", goal_id).execute()
    else:
        tasks = db.table("user_tasks").select("status").eq("user_id", user_id).execute()
    total = len(tasks.data) if tasks.data else 0
    completed = sum(1 for t in (tasks.data or []) if t["status"] == "completed")
    return completed / total if total > 0 else 1.0
```

**Mastery computation (3 signals):**
```python
def compute_mastery(user_id: str, topic_or_goal_id: str, db) -> float:
    """Lightweight mastery score (0.0 to 1.0).
    
    3 data sources:
    1. Quality scores (SM-2 input 0-5) → normalized to 0-1, weight 0.5
    2. Success ratio (completed / total) → weight 0.3
    3. Reschedule penalty (-0.1 per push, capped at -0.3) → weight 0.2
    
    Returns progress toward mastery_level_target if available.
    When DKT arrives, this function's interface stays the same — only internals change.
    """
    avg_quality = _get_avg_quality(user_id, topic_or_goal_id, db)  # 0-5 → 0-1
    sr = _calculate_sr(user_id, topic_or_goal_id, db)
    reschedule_count = _get_reschedule_count(user_id, topic_or_goal_id, db)
    
    raw_mastery = (
        (avg_quality / 5.0) * 0.5
        + sr * 0.3
        + max(0.0, 1.0 - reschedule_count * 0.1) * 0.2
    )
    
    # Scale to mastery_level_target if available
    target = _get_mastery_target(user_id, topic_or_goal_id, db)  # 1-5 from goal_metadata
    if target:
        return min(raw_mastery / (target / 5.0), 1.0)  # progress toward target
    return raw_mastery
```

**`get_mastery_summary(user_id)` → `dict[str, float]`:** Returns `{topic_or_goal: mastery_score}` for all active topics. Used by coach module and frontend.

**Wiring `task_completion_criteria` table:**
- On decomposition: each chunk's `completion_criteria` string → insert row with `source='decomposition'`
- On task completion: mark row `is_completed=True`, record `completed_at`
- Future (Sub-project 2): document enrichment adds rows with `source='uploaded_document'`

**Dev mode trace:**
```
Mastery: DSA → 0.62 (quality=3.5/5, SR=0.8, reschedule=0×, target=4/5)
```

**Frontend indicator:** Small circular progress ring per topic on schedule view cards. Shows "DSA: 62%" in `sage` color. Ring fills proportionally.

---

### 3. WOOP Runtime (`app/services/analytical/workspace_builder.py`, frontend)

**Backend:** Extend workspace response with full MCII card.

```python
# In build_task_workspace(), after fetching task metadata:
woop_card = None
if task.get("implementation_intention"):
    intention = task["implementation_intention"]
    goal_meta = _get_goal_metadata(task.get("goal_id"), user_id, db)
    woop_card = {
        "wish": goal_meta.get("objective", task.get("title", "")),          # Wish stage
        "outcome": goal_meta.get("outcome_visualization", ""),               # Outcome stage
        "obstacle": intention.get("obstacle_trigger", ""),                   # Obstacle stage
        "plan": intention.get("behavioral_response", ""),                    # Plan stage
    }

# Include in workspace response:
workspace = TaskWorkspace(
    ...,
    woop_card=woop_card,
)
```

**Frontend component: `WoopCard.tsx`**
```
┌─────────────────────────────────────────────────┐
│ 💡 Master Dynamic Programming                   │
│ 🎯 "I'll ace the contest and feel confident"    │
│ ⚠️  If: you feel overwhelmed by the recursion...│
│ → Then: open practice set, solve just problem #1│
└─────────────────────────────────────────────────┘
```

- Shown at top of task workspace page
- Collapsible — collapsed after first view per task (localStorage: `woop-dismissed-{taskId}`)
- Only rendered when `woop_card` has non-empty `obstacle` + `plan`
- Uses `dusk` color left border

**Observation loop integration:** When task with WOOP intention is completed, extract a memory: "User completed [task] despite obstacle [obstacle_trigger]" → reinforces self-efficacy.

**Dev mode trace:**
```
WOOP: wish="Master DP" · outcome="ace contest" · obstacle="overwhelmed" → plan="solve problem #1"
```

---

### 4. Coach Module Rewrite (`app/modules/coach.py`)

**Replace stub with Bandura's 4 Sources of Self-Efficacy:**

```python
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
"""

async def run_coaching_response(state: dict) -> dict:
    user_model = state.get("user_model")
    if not user_model:
        return {"response_message": "I need to learn more about you first. Tell me about your goals."}
    
    from app.services.analytical.mastery_tracker import get_mastery_summary, _calculate_sr
    
    mastery = await asyncio.to_thread(get_mastery_summary, user_model.user_id, db)
    sr = await asyncio.to_thread(_calculate_sr, user_model.user_id, None, db)
    all_tasks = await user_model.get_all_tasks()
    
    completed = [t for t in all_tasks if t.get("status") == "completed"]
    pushed = [t for t in all_tasks if t.get("status") == "pacing_pushed"]
    pending = [t for t in all_tasks if t.get("status") == "pending"]
    
    # Quality trend: compare last 5 completions to previous 5
    quality_trend = _compute_quality_trend(completed)  # "improving" | "stable" | "declining"
    
    # Streak: consecutive days with at least 1 completion
    streak = _compute_streak(completed)
    
    # Energy pattern from PEARL
    energy = await user_model.get_estimated_energy()
    
    coaching_context = f"""User mastery data:
- Topics: {json.dumps(mastery)}
- Success ratio: {sr:.0%}
- Quality trend: {quality_trend}
- Completion streak: {streak} days
- Tasks completed: {len(completed)}, pending: {len(pending)}, rescheduled: {len(pushed)}
- Current energy estimate: {energy:.1f}/1.0
- Time of day: {datetime.now().strftime('%I %p')}
"""
    
    response = await route_llm_call(
        task="voice_of_jarvis",
        prompt=f"Generate a coaching message for this user:\n{coaching_context}",
        system_prompt=MASTERY_COACHING_SYSTEM_PROMPT,
    )
    
    return {
        "response_message": response,
        "coaching_data": {
            "mastery": mastery,
            "sr": sr,
            "quality_trend": quality_trend,
            "streak": streak,
            "bandura_sources": _identify_sources_used(mastery, quality_trend, streak, energy),
        },
    }
```

**Triggers (when coach activates):**
- `CHECK_PROGRESS` intent → full coaching response
- INFEASIBLE error → anti-guilt coaching (scope vs time mismatch)
- After task completion → inline micro-coaching (shorter prompt)
- PEARL detects declining quality trend → proactive coaching

**Dev mode trace:**
```
Coach: mastery={DSA: 0.62, Calc: 0.45} · SR=0.78 · streak=3 · trend=improving → Bandura: accomplishment+persuasion
```

**Frontend:** Coach messages render inline in chat (existing). After task completion, an optional inline card:
```
📈 DSA mastery: 40% → 62% this week. ~3 more focused sessions to your target.
```

---

### 5. Slippery Deadlines (`app/services/analytical/control_policy.py`, `app/api/v1/endpoints/tasks.py`)

**From Blueprint's `handle_missed_deadline()`:**

**New task status: `pacing_pushed`**
```python
# When task is skipped or missed:
target_task["status"] = "pacing_pushed"  # NOT "overdue" or "failed"
target_task["reschedule_count"] = (target_task.get("reschedule_count", 0)) + 1
```

**Energy decay + SR update:**
```python
async def handle_missed_task(user_id: str, task_id: str, db, user_model) -> dict:
    """Anti-guilt rescheduling logic (Blueprint: handle_missed_deadline)."""
    
    # 1. Update energy profile (0.98 decay per miss)
    energy_decay = user_model._cache.get("energy_decay", 1.0) * 0.98
    user_model._cache["energy_decay"] = energy_decay
    
    # 2. Recalculate success ratio
    sr = _calculate_sr(user_id, task.get("goal_id"), db)
    
    # 3. Mark task as pacing_pushed
    task = await db.table("user_tasks").update({
        "status": "pacing_pushed",
        "reschedule_count": task["reschedule_count"] + 1,
    }).eq("task_id", task_id).eq("user_id", user_id).execute()
    
    # 4. Buffer reallocation (Blueprint: buffer_ratio = 0.25)
    daily_cap = compute_adaptive_daily_cap(...)
    buffer_minutes = daily_cap * 0.25  # 25% anti-guilt reserve
    
    if task["duration_minutes"] <= buffer_minutes:
        reassignment = "today_buffer"
    else:
        reassignment = "tomorrow_morning_peak"
    
    # 5. Reschedule count >= 3 → memory: needs re-decomposition
    if task["reschedule_count"] >= 3:
        memory_store = await user_model.get_memory_store()
        if memory_store:
            topic = task.get("title", "this topic")
            memory_store.store_memory(
                user_id=user_id,
                content=f"User struggles with '{topic}' — needs re-decomposition into smaller chunks",
                memory_type="behavioral_pattern",
                source="pacing_pushed",
                confidence=0.8,
            )
    
    # 6. Trigger background replan
    asyncio.create_task(trigger_replan(user_id, db, f"pacing_pushed:{task_id}"))
    
    return {
        "status": "pacing_pushed",
        "reassignment": reassignment,
        "message": f"Life happened — I've adjusted your plan. {task['title']} moves to {reassignment.replace('_', ' ')}.",
        "energy_decay": energy_decay,
        "sr": sr,
    }
```

**`reschedule_count` column:** Add to `user_tasks` if not present. Default 0. Incremented on each push.

**Frontend language rules:**

| Current (BAD) | New (Anti-Guilt) |
|---|---|
| "Overdue" | "Adjusted" |
| "Missed deadline" | "Rescheduled — no stress" |
| "Failed to complete" | "Life happened" |
| "You're behind" | "Plan updated" |
| Red/terra color on missed | Sage/muted color on adjusted |

**Task card when `pacing_pushed`:**
```
↻ Adjusted — no stress
   Moved to tomorrow morning
```

**Dev mode trace:**
```
Slippery: task="Dynamics Ch3" · reschedule=1 · energy_decay=0.98 · buffer=45min → reassign=tomorrow_morning_peak
```

---

## Phase Trace Enrichment

The IntelligentTrace (from the Intelligent Phase Progress plan) shows psychology decisions in both default and dev mode:

**Default mode (Option B):**
```
✓ Crunching schedule... 0.1s
  → TMT applied: 3 tasks deadline-boosted

✓ Absorbing learnings... 0.2s
  → 🧠 Mastery: DSA improved 40% → 62%
```

**Dev mode (Option C):**
```
✓ Crunching schedule... 0.1s
  → TMT applied: 3 tasks boosted
  ⚙ TMT: E=0.85 × V=7 / (1 + 1.5 × 2.3h) = 1.28 → priority=8
  ⚙ TMT: E=0.85 × V=3 / (1 + 1.5 × 48h) = 0.03 → priority=1
  ⚙ Mastery: DSA=0.62 (q=3.5, SR=0.8, push=0), Calc=0.45 (q=2.8, SR=0.6, push=1)

✓ Checking in... 0.3s
  → Coach: accomplishment + persuasion (mastery improving)
  ⚙ Bandura: sources=[accomplishment, persuasion] · streak=3 · trend=improving
```

**SSE events for psychology (via progress_queue):**
```json
{"_event_type": "tool_use", "module": "planning_module", "tool": "tmt_priority",
 "status": "done", "detail": {"tasks_boosted": 3, "formula": "canonical_steel_konig"}}

{"_event_type": "tool_use", "module": "coach_module", "tool": "mastery_check",
 "status": "done", "detail": {"topics": {"DSA": 0.62}, "trend": "improving"}}
```

---

## Seed Data & Dev Verification

**Script: `scripts/seed_psychology_demo.py`**

Creates a complete test scenario in one command:

```python
# 1. Demo user with behavioral constraints
user_id = "psych-demo-001"
# Sleep: midnight-8am, Study: 9am-12pm full_focus, Meetings: 2-3pm blocked

# 2. Two goals with mastery_level_target
# Goal A: "Master Dynamic Programming" — target=4/5, deadline=Friday
# Goal B: "Learn Calculus basics" — target=3/5, deadline=next Wednesday

# 3. 10 completed tasks with quality scores
# DSA tasks: quality [4, 5, 3, 4, 5] → avg 4.2 → high mastery
# Calculus tasks: quality [2, 3, 2, 3, 3] → avg 2.6 → low mastery

# 4. 3 skipped/pushed tasks
# 2 calculus tasks pushed (reschedule_count=1 each)
# 1 DSA task pushed once

# 5. Deadline hints on 5 tasks
# 2 tasks due in 6 hours (high TMT urgency)
# 2 tasks due in 48 hours (moderate)
# 1 task due in 7 days (low)

# 6. WOOP intentions on 3 tasks
# obstacle: "overwhelmed by recursion" → response: "solve just problem #1"

# 7. Completion criteria rows in task_completion_criteria table

# 8. A ChromaDB document (Deep Learning sample paper)
# Linked to DSA goal via task_materials → validates workspace + WOOP pipeline

# 9. Behavioral patterns (PEARL memories)
# "User skips tasks during 14:00-15:00" (confidence 0.75)
# "User completes morning tasks faster" (confidence 0.8)
```

**Verification curl:**
```bash
# After running seed script:
python scripts/seed_psychology_demo.py

# Test TMT + mastery + coach:
curl -N -X POST http://localhost:8000/api/v1/chat/v2/stream \
  -H "Content-Type: application/json" \
  -d '{"user_prompt": "how am I doing with my studies?", "user_id": "psych-demo-001"}'
# Expected: CHECK_PROGRESS intent → coach module with real mastery data

# Test WOOP in workspace:
curl http://localhost:8000/api/v1/tasks/dsa_task_1/workspace?user_id=psych-demo-001
# Expected: woop_card with all 4 MCII stages + RAG chunks from uploaded document

# Test slippery deadline:
curl -X POST http://localhost:8000/api/v1/tasks/calc_task_3/skip \
  -H "Content-Type: application/json" \
  -d '{"user_id": "psych-demo-001", "quality": 0}'
# Expected: status=pacing_pushed, message="Life happened...", no "overdue" language
```

---

## Files to Create/Modify

### New files
| File | Responsibility |
|---|---|
| `app/services/analytical/mastery_tracker.py` | Mastery computation (quality + SR + reschedule penalty) |
| `scripts/seed_psychology_demo.py` | Seed data for dev verification |
| `jarvis-frontend/components/app/WoopCard.tsx` | Full MCII card component |

### Modified files (backend)
| File | What Changes |
|---|---|
| `app/api/v1/endpoints/schedule.py` | Fix TMT formula, wire real delay + SR |
| `app/modules/coach.py` | Full rewrite: Bandura's 4 sources, mastery data, mastery-only prompt |
| `app/api/v1/endpoints/tasks.py` | `skip_task` → `handle_missed_task` with pacing_pushed status |
| `app/services/analytical/workspace_builder.py` | Add `woop_card` to workspace response |
| `app/modules/planning_graph.py` | Emit TMT detail in tool_use events |
| `app/core/observation.py` | WOOP completion → memory extraction |
| `app/schemas/context.py` | Add `woop_card` to workspace schema, `pacing_pushed` status |

### Modified files (frontend)
| File | What Changes |
|---|---|
| `jarvis-frontend/components/app/IntelligentTrace.tsx` | Show TMT + mastery in phase trace |
| `jarvis-frontend/app/(app)/schedule/page.tsx` | Mastery progress rings, deadline urgency colors |
| `jarvis-frontend/app/(app)/workspace/[taskId]/page.tsx` | Render WoopCard at top |
| `jarvis-frontend/lib/constants.ts` | Anti-guilt language (replace "Overdue" → "Adjusted") |

### Database
| Change | Table |
|---|---|
| Add `reschedule_count INT DEFAULT 0` | `user_tasks` (if not present) |
| Populate `task_completion_criteria` on decomposition | `task_completion_criteria` |

---

## Out of Scope

- **Cold Start onboarding** — separate spec (needs frontend onboarding flow, preference elicitation quizzes)
- **DKT/RL** — replaces mastery tracker later, same interface
- **Document → task enrichment** — Sub-project 2 (completion criteria from documents)
- **Custom Interactive UI** — Sub-project 5 (workspace persistence, Claude-style artifacts)
- **Germane/extraneous load tracking** — CLT intrinsic_load works; fine-grained load types are Phase 2
- **Population priors for cold start** — requires training on anonymized data; future work
