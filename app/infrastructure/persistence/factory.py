"""Builds a whole repository family at once (Abstract Factory).

Callers ask for a family and receive ports; they never name a concrete class, so
the in-memory and Supabase families are interchangeable.
"""

from dataclasses import dataclass
from typing import Any

from app.domain.ports import (
    ConstraintRepository,
    ConversationRepository,
    DocumentFileStore,
    DocumentRepository,
    DraftRepository,
    GoalRepository,
    HabitTrackerRepository,
    MemoryRepository,
    ProfileRepository,
    SuggestionRepository,
    TaskEventRepository,
    TaskRepository,
    UsageRepository,
)
from app.infrastructure.persistence.in_memory import (
    InMemoryConstraintRepository,
    InMemoryConversationRepository,
    InMemoryDocumentFileStore,
    InMemoryDocumentRepository,
    InMemoryDraftRepository,
    InMemoryGoalRepository,
    InMemoryHabitTrackerRepository,
    InMemoryMemoryRepository,
    InMemoryProfileRepository,
    InMemoryStore,
    InMemorySuggestionRepository,
    InMemoryTaskEventRepository,
    InMemoryTaskRepository,
    InMemoryUsageRepository,
)
from app.infrastructure.persistence.supabase_repos import (
    SupabaseConstraintRepository,
    SupabaseConversationRepository,
    SupabaseDocumentRepository,
    SupabaseDraftRepository,
    SupabaseGoalRepository,
    SupabaseHabitTrackerRepository,
    SupabaseMemoryRepository,
    SupabaseProfileRepository,
    SupabaseSuggestionRepository,
    SupabaseTaskEventRepository,
    SupabaseTaskRepository,
    SupabaseUsageRepository,
)
from app.infrastructure.persistence.supabase_storage import (
    DOCUMENT_BUCKET,
    SupabaseDocumentFileStore,
)


@dataclass(frozen=True)
class Repositories:
    """One repository per port, all from the same family over the same storage."""

    tasks: TaskRepository
    task_events: TaskEventRepository
    drafts: DraftRepository
    constraints: ConstraintRepository
    profiles: ProfileRepository
    suggestions: SuggestionRepository
    usage: UsageRepository
    memories: MemoryRepository
    conversations: ConversationRepository
    goals: GoalRepository
    habit_trackers: HabitTrackerRepository
    documents: DocumentRepository
    document_files: DocumentFileStore


class RepositoryFactory:
    """Builds the Supabase family or the in-memory family."""

    @staticmethod
    def supabase(
        client: Any, *, document_bucket: str = DOCUMENT_BUCKET, max_file_bytes: int | None = None
    ) -> Repositories:
        """Build the Supabase family over a supabase-py ``Client`` (or a stand-in for one).

        Uploaded files go to Supabase Storage over the same client: the bucket
        ``document_bucket``, limited to ``max_file_bytes`` (None: the project's limit).
        """
        return Repositories(
            tasks=SupabaseTaskRepository(client),
            task_events=SupabaseTaskEventRepository(client),
            drafts=SupabaseDraftRepository(client),
            constraints=SupabaseConstraintRepository(client),
            profiles=SupabaseProfileRepository(client),
            suggestions=SupabaseSuggestionRepository(client),
            usage=SupabaseUsageRepository(client),
            memories=SupabaseMemoryRepository(client),
            conversations=SupabaseConversationRepository(client),
            goals=SupabaseGoalRepository(client),
            habit_trackers=SupabaseHabitTrackerRepository(client),
            documents=SupabaseDocumentRepository(client),
            document_files=SupabaseDocumentFileStore(
                client, bucket=document_bucket, max_file_bytes=max_file_bytes
            ),
        )

    @staticmethod
    def in_memory(store: InMemoryStore | None = None) -> Repositories:
        """Build the in-memory family over ``store``, or over a new empty one."""
        store = store if store is not None else InMemoryStore()
        return Repositories(
            tasks=InMemoryTaskRepository(store),
            task_events=InMemoryTaskEventRepository(store),
            drafts=InMemoryDraftRepository(store),
            constraints=InMemoryConstraintRepository(store),
            profiles=InMemoryProfileRepository(store),
            suggestions=InMemorySuggestionRepository(store),
            usage=InMemoryUsageRepository(store),
            memories=InMemoryMemoryRepository(store),
            conversations=InMemoryConversationRepository(store),
            goals=InMemoryGoalRepository(store),
            habit_trackers=InMemoryHabitTrackerRepository(store),
            documents=InMemoryDocumentRepository(store),
            document_files=InMemoryDocumentFileStore(store),
        )
