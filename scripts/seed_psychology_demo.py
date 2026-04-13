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

    # Also clean user_memories for this demo user
    try:
        db.table("user_memories").delete().eq("user_id", user_id).execute()
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

    # user_plan_updates columns: id, user_id, goal_id, source, deadline_date, deadline_raw, context_snippet, created_at
    # Store planning_goal + goal_metadata inside context_snippet (JSONB-serialized)
    goals = [
        {"user_id": user_id, "goal_id": goal_a_id, "source": "chat",
         "deadline_date": friday.date().isoformat(),
         "deadline_raw": f"Friday {friday.strftime('%Y-%m-%d')}",
         "context_snippet": json.dumps({
             "planning_goal": "Master Dynamic Programming",
             "goal_metadata": {"objective": "Master DP", "outcome_visualization": "I'll ace the contest and feel confident", "mastery_level_target": 4},
         })},
        {"user_id": user_id, "goal_id": goal_b_id, "source": "chat",
         "deadline_date": next_wed.date().isoformat(),
         "deadline_raw": f"Wednesday {next_wed.strftime('%Y-%m-%d')}",
         "context_snippet": json.dumps({
             "planning_goal": "Learn Calculus basics",
             "goal_metadata": {"objective": "Calculus fundamentals", "outcome_visualization": "Solve integrals fluently", "mastery_level_target": 3},
         })},
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
        memory_store = MemoryStore(supabase_client=db)
        memory_store.store_memory(user_id, {
            "type": "behavioral_pattern",
            "content": "User skips tasks during 14:00-15:00 consistently",
            "source": "pearl",
            "confidence": 0.75,
        })
        memory_store.store_memory(user_id, {
            "type": "behavioral_pattern",
            "content": "User completes morning tasks faster and with higher quality",
            "source": "pearl",
            "confidence": 0.8,
        })
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
