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
