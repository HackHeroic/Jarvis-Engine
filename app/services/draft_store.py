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
