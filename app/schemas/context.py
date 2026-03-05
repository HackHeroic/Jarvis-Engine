"""Universal Semantic Router and timetable schemas for dynamic scheduling."""

from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class IntentType(str, Enum):
    """Universal intent classification for unstructured data."""

    CALENDAR_SYNC = "CALENDAR_SYNC"  # Timetables, meetings, flights, deep-work blocks
    KNOWLEDGE_INGESTION = "KNOWLEDGE_INGESTION"  # Syllabi, business plans, PDFs
    BEHAVIORAL_CONSTRAINT = "BEHAVIORAL_CONSTRAINT"  # Work preferences, back-bench modes
    ACTION_ITEM = "ACTION_ITEM"  # Direct goals or tasks to be scheduled
    PLAN_DAY = "PLAN_DAY"  # User wants to plan day, schedule tasks, break down a goal


class IntentClassification(BaseModel):
    """Universal Semantic Router classification for unstructured data."""

    intent: IntentType = Field(
        description="The universally classified intent of the incoming data."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(
        description="A brief, persona-agnostic summary of the payload."
    )


class BrainDumpExtraction(BaseModel):
    """Extracted components from a brain-dump prompt."""

    planning_goal: Optional[str] = Field(
        default=None,
        description="Plan my day to do X, Y, Z -> use for decompose",
    )
    inline_habits: List[str] = Field(
        default_factory=list,
        description="Verbatim long-term constraints including time anchors (e.g. 'never schedule work before 11 AM'). Preserve exact wording.",
    )
    state_updates: List[str] = Field(
        default_factory=list,
        description="Temporary today-only mood e.g. I'm feeling really tired today",
    )
    action_items: List[str] = Field(
        default_factory=list,
        description="Reminders, tasks e.g. remind me to call my mom",
    )
    search_queries: List[str] = Field(
        default_factory=list,
        description="Look up, search e.g. latest updates on SpaceX launch",
    )
    has_calendar: bool = Field(default=False, description="Timetables, meeting schedules")
    calendar_text: Optional[str] = Field(
        default=None,
        description="If has_calendar, pass to calendar extractor",
    )
    has_knowledge: bool = Field(
        default=False,
        description="Stub for future PDF/syllabus inline refs",
    )


class Availability(str, Enum):
    """Availability level for a temporal slot."""

    BLOCKED = "blocked"  # Hard block: no work (sleep, meetings)
    MINIMAL_WORK = "minimal_work"  # Soft block: back-bench, passive listening
    FULL_FOCUS = "full_focus"  # Available for scheduling deep tasks


RecurrenceType = Literal[
    "daily", "weekdays", "weekends", "weekly", "monthly", "yearly", "once"
]


class SemanticTimeSlot(BaseModel):
    """LLM output: semantic representation before horizon expansion."""

    name: str = Field(description="Name of the event")
    start_min: int = Field(ge=0, le=1440, description="Intra-day start (0 = 8 AM)")
    end_min: int = Field(ge=0, le=1440, description="Intra-day end")
    availability: Availability
    recurrence: RecurrenceType = "daily"
    weekday: Optional[int] = Field(
        default=None,
        ge=0,
        le=6,
        description="0-6 for weekly/monthly; e.g. 'every Monday' -> 0",
    )
    valid_from: Optional[str] = Field(
        default=None,
        description="ISO-8601 or null = now",
    )
    valid_until: Optional[str] = Field(
        default=None,
        description="ISO-8601 or null = indefinite",
    )
    max_task_duration: Optional[int] = Field(
        default=None,
        description="Max task minutes if minimal_work",
    )
    max_difficulty: Optional[float] = Field(
        default=None,
        description="Max difficulty weight if minimal_work",
    )


class TimeSlot(BaseModel):
    """Represents a dynamically ingested temporal constraint."""

    name: str = Field(description="Name of the event")
    start_min: int = Field(description="Start time in minutes from day start")
    end_min: int = Field(description="End time in minutes from day start")
    availability: Availability
    recurring: bool = Field(
        default=False,
        description="Flag for horizon expansion; backward compatibility",
    )
    max_task_duration: Optional[int] = Field(
        default=None,
        description="Max task minutes if minimal_work",
    )
    max_difficulty: Optional[float] = Field(
        default=None,
        description="Max difficulty weight if minimal_work",
    )


class TimeSlotsResponse(BaseModel):
    """LLM output wrapper for habit translator (legacy)."""

    slots: List[TimeSlot] = Field(default_factory=list)


class SemanticTimeSlotsResponse(BaseModel):
    """LLM output wrapper for habit translator (semantic slots)."""

    semantic_slots: List[SemanticTimeSlot] = Field(default_factory=list)


class ExtractedTimeSlots(BaseModel):
    """Output of calendar extraction pipeline."""

    slots: List[TimeSlot] = Field(default_factory=list)
    source_summary: str = Field(description="e.g. Sem 6 Mon-Fri")
    behavioral_overrides_applied: List[str] = Field(
        default_factory=list,
        description="e.g. OS Lec -> minimal_work (back bench)",
    )
    valid_until: Optional[str] = Field(
        default=None,
        description="ISO-8601 semester/exam end date if found",
    )
    needs_end_date: bool = Field(
        default=False,
        description="True if semester schedule but no end date mentioned",
    )


class PendingCalendarUpdate(BaseModel):
    """Pending calendar update awaiting user approval."""

    id: str
    extracted_slots: List[TimeSlot] = Field(default_factory=list)
    source_summary: str = ""
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: str = Field(description="ISO-8601 timestamp")


class ActionItemProposal(BaseModel):
    """Proposal for an action item requiring user choice."""

    id: str
    title: str = Field(description="Short title of the action")
    summary: str = Field(description="Brief description")
    suggested_actions: List[str] = Field(
        default_factory=list,
        description="e.g. remind_after_days, add_to_evening_schedule",
    )
    deadline_mentioned: bool = False
    created_at: str = Field(description="ISO-8601 timestamp")


class IngestionPipelineResult(BaseModel):
    """Unified result from the autonomous extraction pipeline."""

    intent: IntentType
    classification_summary: str = ""
    calendar_result: Optional[ExtractedTimeSlots] = None
    pending_calendar_id: Optional[str] = None
    knowledge_result: Optional[dict] = None
    behavioral_result: Optional[dict] = None
    action_proposal: Optional[ActionItemProposal] = None


class ChatResponse(BaseModel):
    """Unified response from the Control Policy for the /chat endpoint."""

    intent: str = Field(
        description="Classified intent (e.g. PLAN_DAY, BEHAVIORAL_CONSTRAINT)",
    )
    message: str = Field(description="Friendly agentic response for the user")
    schedule: Optional[dict] = Field(
        default=None,
        description="OR-Tools output: status, schedule, goal_metadata",
    )
    execution_graph: Optional[dict] = Field(
        default=None,
        description="ExecutionGraph from reasoning",
    )
    ingestion_result: Optional[dict] = Field(
        default=None,
        description="IngestionPipelineResult when intent is ingestion",
    )
    action_proposals: Optional[List[dict]] = Field(
        default=None,
        description="ActionItemProposal list when we extracted action items from brain dump",
    )
    search_result: Optional[dict] = Field(
        default=None,
        description="When search_queries executed: queries and summaries",
    )
    suggested_action: Optional[str] = Field(
        default=None,
        description="Frontend hint: e.g. 'replan' when user saved a habit and may want to refresh schedule",
    )
    thinking_process: Optional[str] = Field(
        default=None,
        description="The extracted internal think monologue from the LLM.",
    )
