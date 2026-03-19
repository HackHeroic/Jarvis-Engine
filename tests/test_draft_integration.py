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
