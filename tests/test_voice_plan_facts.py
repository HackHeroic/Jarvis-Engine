"""The reply's facts about a drafted plan: real counts and the work just asked for."""

from app.services.analytical.voice_of_jarvis import VOICE_OF_JARVIS_PROMPT, _build_voj_parts


def _summary(new_titles: list[str], slots: int) -> dict:
    return {
        "schedule_generated": True,
        "schedule": {"schedule": {f"t{i}": {"start_min": i, "end_min": i + 1} for i in range(slots)}},
        "execution_graph": {"decomposition": [{"task_id": f"n{i}", "title": t}
                                              for i, t in enumerate(new_titles)]},
    }


def test_voice_parts__plan_drafted__carries_real_counts_and_names_the_new_work():
    parts = _build_voj_parts(_summary(["List ML quiz topics", "Outline the compiler assignment"], 14))

    assert "tasks_in_draft: 14" in parts
    assert "new_tasks: 2" in parts
    assert "new_work: List ML quiz topics; Outline the compiler assignment" in parts
    assert "already_on_the_plate_and_given_a_slot: 12" in parts


def test_voice_parts__many_new_tasks__names_a_few_and_counts_the_rest():
    parts = _build_voj_parts(_summary([f"Task {i}" for i in range(7)], 7))

    assert "new_work: Task 0; Task 1; Task 2; Task 3; and 3 more" in parts
    assert not any(p.startswith("already_on_the_plate") for p in parts)


def test_voice_parts__no_schedule__no_plan_facts():
    parts = _build_voj_parts({"schedule_generated": False, "execution_graph": {
        "decomposition": [{"title": "X"}]}})

    assert not any(p.startswith(("tasks_in_draft", "new_")) for p in parts)


def test_voice_prompt__examples_carry_no_task_count_to_copy():
    """An example that says "six tasks" gets recited for a plan of any size."""
    assert "six tasks" not in VOICE_OF_JARVIS_PROMPT
