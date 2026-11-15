"""Settings for the services the composition root builds: one frozen record per service.

``Settings.from_config(cfg)`` is the one place config values become settings; the
composition root (``AppContainer.build``) calls it once and hands each service its own
record. A service built without one (a test, a script) takes ``default_settings()``, the
same values read from ``app.core.config``. No service reads the config module itself.
"""

from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class TaskSettings:
    """``TaskService``: when a slot with no outcome has passed."""

    slot_grace_minutes: int  # a slot has passed this long after its end
    sweep_lookback_days: int  # slots that started up to this long ago are swept


@dataclass(frozen=True)
class MemorySettings:
    """``MemoryService``: when a memory is new, close, kept and recalled."""

    dedupe_similarity: float  # this close to an active or faded memory: reinforce it
    profile_tokens: int  # the standing profile's share of the context note
    recall_min_similarity: float  # a memory counts as relevant from here up
    recall_tokens: int  # the relevant memories' share of the context note
    retention_floor: float  # below this strength a memory leaves recall
    suppress_similarity: float  # this close to a let-go memory: never stored again
    summary_suppress_similarity: float  # a summary sentence this close to one is dropped


@dataclass(frozen=True)
class ConversationSettings:
    """``ConversationMemoryService``: the window, its compaction and its summaries."""

    compact_after_messages: int
    compact_keep_recent: int
    summary_tokens: int
    history_max_chars_per_message: int
    history_max_messages: int
    idle_minutes: int
    summary_lookback_days: int
    summary_rescan_hours: int
    summary_input_max_chars: int
    summary_page_messages: int
    summary_retry_minutes: int


@dataclass(frozen=True)
class FocusSettings:
    """The learned focus profile: PatternService and UserModelService read
    ``window_days``; HistoryEnergyEstimator takes all three."""

    window_days: int
    prior_strength: float
    min_observations: int


@dataclass(frozen=True)
class AnticipationSettings:
    """The anticipation loop, the manual run's cooldown and the morning brief's window."""

    tick_seconds: float
    manual_cooldown_seconds: float
    morning_brief_window_hours: int


@dataclass(frozen=True)
class DocumentSettings:
    """The documents: where an uploaded file is kept, how big it may be, and how close a
    document must be to a task to be linked to it."""

    bucket: str  # the private Supabase Storage bucket
    max_file_bytes: int  # the largest upload ingestion accepts; the bucket's limit
    link_similarity: float  # a document phrase this close to a task title links them


@dataclass(frozen=True)
class Settings:
    tasks: TaskSettings
    memory: MemorySettings
    conversation: ConversationSettings
    focus: FocusSettings
    anticipation: AnticipationSettings
    documents: DocumentSettings

    @classmethod
    def from_config(cls, cfg: ModuleType) -> "Settings":
        """The one place config values become settings."""
        return cls(
            tasks=TaskSettings(
                slot_grace_minutes=cfg.SLOT_GRACE_MINUTES,
                sweep_lookback_days=cfg.SLOT_SWEEP_LOOKBACK_DAYS,
            ),
            memory=MemorySettings(
                dedupe_similarity=cfg.MEMORY_DEDUPE_SIMILARITY,
                profile_tokens=cfg.MEMORY_PROFILE_TOKENS,
                recall_min_similarity=cfg.MEMORY_RECALL_MIN_SIMILARITY,
                recall_tokens=cfg.MEMORY_RECALL_TOKENS,
                retention_floor=cfg.MEMORY_RETENTION_FLOOR,
                suppress_similarity=cfg.MEMORY_SUPPRESS_SIMILARITY,
                summary_suppress_similarity=cfg.SUMMARY_SUPPRESS_SIMILARITY,
            ),
            conversation=ConversationSettings(
                compact_after_messages=cfg.COMPACT_AFTER_MESSAGES,
                compact_keep_recent=cfg.COMPACT_KEEP_RECENT,
                summary_tokens=cfg.CONVERSATION_SUMMARY_TOKENS,
                history_max_chars_per_message=cfg.HISTORY_MAX_CHARS_PER_MESSAGE,
                history_max_messages=cfg.HISTORY_MAX_MESSAGES,
                idle_minutes=cfg.SESSION_IDLE_MINUTES,
                summary_lookback_days=cfg.SESSION_SUMMARY_LOOKBACK_DAYS,
                summary_rescan_hours=cfg.SESSION_SUMMARY_RESCAN_HOURS,
                summary_input_max_chars=cfg.SUMMARY_INPUT_MAX_CHARS,
                summary_page_messages=cfg.SUMMARY_PAGE_MESSAGES,
                summary_retry_minutes=cfg.SUMMARY_RETRY_MINUTES,
            ),
            focus=FocusSettings(
                window_days=cfg.FOCUS_WINDOW_DAYS,
                prior_strength=cfg.FOCUS_PRIOR_STRENGTH,
                min_observations=cfg.MIN_FOCUS_OBSERVATIONS,
            ),
            anticipation=AnticipationSettings(
                tick_seconds=cfg.ANTICIPATION_TICK_SECONDS,
                manual_cooldown_seconds=cfg.ANTICIPATION_MANUAL_COOLDOWN_SECONDS,
                morning_brief_window_hours=cfg.MORNING_BRIEF_WINDOW_HOURS,
            ),
            documents=DocumentSettings(
                bucket=cfg.DOCUMENT_BUCKET,
                max_file_bytes=cfg.MAX_CHAT_UPLOAD_BYTES,
                link_similarity=cfg.DOCUMENT_LINK_SIMILARITY,
            ),
        )


def default_settings() -> Settings:
    """``Settings.from_config(app.core.config)``: what a service built without the
    composition root (a test, a script) runs on."""
    import app.core.config as config

    return Settings.from_config(config)
