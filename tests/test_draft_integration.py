# tests/test_draft_integration.py
"""Integration tests for the draft review flow, against a fake Supabase.

These originally exercised the in-memory *component* API (`create`,
`add_component`, `accept_component`, ...). That API was removed in 3674f6d
when DraftStore migrated to Supabase persistence, and its method names now
survive only as no-op shims (see the "Legacy alias" block in draft_store.py,
whose no-op contract `tests/test_draft_store.py` pins). The tests below keep
the original intent — full lifecycle, user isolation, pending selection — but
target the API the store actually implements.
"""

import pytest

from app.services.draft_store import DraftStore
from tests.fakes import FakeSupabase


HORIZON = "2026-08-08T08:00:00Z"


def _tasks():
    return [
        {"task_id": "goal1_task_1", "title": "Read chapter 1", "duration_minutes": 25},
        {"task_id": "goal1_task_2", "title": "Practice problems", "duration_minutes": 20},
    ]


@pytest.fixture
def store():
    return DraftStore(supabase_client=FakeSupabase(), ttl_seconds=300)


def test_full_draft_lifecycle(store):
    """create → read back → edit a task → accept."""
    created = store.create_draft(
        user_id="user_1", tasks=_tasks(), horizon_start=HORIZON, goal_id="goal1"
    )
    assert created is not None
    draft_id = created["id"]

    draft = store.get_draft(draft_id, "user_1")
    assert draft["status"] == "pending"
    assert draft["horizon_start"] == HORIZON
    assert [t["task_id"] for t in draft["tasks"]] == ["goal1_task_1", "goal1_task_2"]

    # User shortens a task — edit must persist and flip status to "modified".
    edited = store.edit_task_in_draft(
        draft_id, "user_1", "goal1_task_1", {"duration_minutes": 15}
    )
    assert edited is not None
    assert edited["status"] == "modified"
    task_1 = next(t for t in edited["tasks"] if t["task_id"] == "goal1_task_1")
    assert task_1["duration_minutes"] == 15
    # The untouched task must be left alone.
    task_2 = next(t for t in edited["tasks"] if t["task_id"] == "goal1_task_2")
    assert task_2["duration_minutes"] == 20

    assert store.accept_draft(draft_id, "user_1") is True
    assert store.get_draft(draft_id, "user_1")["status"] == "accepted"


def test_edit_unknown_task_leaves_draft_untouched(store):
    created = store.create_draft(
        user_id="user_1", tasks=_tasks(), horizon_start=HORIZON
    )
    draft_id = created["id"]

    assert store.edit_task_in_draft(draft_id, "user_1", "no_such_task", {"title": "x"}) is None
    assert store.get_draft(draft_id, "user_1")["status"] == "pending"


def test_draft_user_isolation(store):
    """User B must not be able to read or mutate user A's draft."""
    draft_a = store.create_draft(
        user_id="user_a", tasks=_tasks(), horizon_start=HORIZON
    )
    store.create_draft(user_id="user_b", tasks=_tasks(), horizon_start=HORIZON)
    draft_a_id = draft_a["id"]

    assert store.get_draft(draft_a_id, "user_b") is None
    assert store.accept_draft(draft_a_id, "user_b") is False
    assert store.reject_draft(draft_a_id, "user_b", reason="nope") is False
    assert store.edit_task_in_draft(draft_a_id, "user_b", "goal1_task_1", {"title": "x"}) is None

    # delete_draft returns True unconditionally, so assert on the data instead:
    # the user_id filter must keep user A's row alive.
    store.delete_draft(draft_a_id, "user_b")

    # None of user B's attempts may have changed user A's draft.
    mine = store.get_draft(draft_a_id, "user_a")
    assert mine is not None
    assert mine["status"] == "pending"

    # user_b's own pending draft must not be user_a's.
    assert store.get_pending_draft("user_b")["user_id"] == "user_b"


def test_get_pending_draft_ignores_resolved_drafts(store):
    """Only drafts still awaiting review are returned as pending."""
    first = store.create_draft(user_id="user_1", tasks=_tasks(), horizon_start=HORIZON)
    assert store.get_pending_draft("user_1")["id"] == first["id"]

    store.reject_draft(first["id"], "user_1", reason="too cramped")
    assert store.get_pending_draft("user_1") is None
    assert store.get_draft(first["id"], "user_1")["rejection_reason"] == "too cramped"

    second = store.create_draft(user_id="user_1", tasks=_tasks(), horizon_start=HORIZON)
    assert store.get_pending_draft("user_1")["id"] == second["id"]

    store.accept_draft(second["id"], "user_1")
    assert store.get_pending_draft("user_1") is None


def test_delete_draft_removes_it(store):
    created = store.create_draft(user_id="user_1", tasks=_tasks(), horizon_start=HORIZON)
    draft_id = created["id"]

    assert store.delete_draft(draft_id, "user_1") is True
    assert store.get_draft(draft_id, "user_1") is None


def test_clientless_store_returns_empty_defaults_without_network():
    """Task 1 contract: an explicit None client degrades, never dials out."""
    store = DraftStore(supabase_client=None, ttl_seconds=300)

    assert store.create_draft("user_1", _tasks(), HORIZON) is None
    assert store.get_draft("d1", "user_1") is None
    assert store.get_pending_draft("user_1") is None
    assert store.accept_draft("d1", "user_1") is False
    assert store.reject_draft("d1", "user_1") is False
    assert store.delete_draft("d1", "user_1") is False
