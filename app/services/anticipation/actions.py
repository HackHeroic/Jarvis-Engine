"""What accepting a proactive suggestion does: the actions behind the first move.

``ApplyDraftAction`` puts a drafted plan in place (the morning brief).
``AdoptRuleAction`` turns a habit Jarvis noticed into a standing rule, then drafts a
plan that keeps it (a pattern proposal). ``ShareInsightAction`` changes nothing: it is
something Jarvis noticed. ``build_action_factory`` registers each kind, so an accepted
suggestion is rebuilt from its stored payload.
"""

import logging
from typing import Any, ClassVar
from uuid import uuid4

from app.domain import errors
from app.domain.anticipation import ActionResult, ActionRisk, ProactiveAction
from app.domain.entities import HOURS_PER_DAY, BehavioralConstraint, DraftStatus
from app.domain.ports import ConstraintRepository, DraftPlanner
from app.schemas.context import SemanticTimeSlot
from app.services.anticipation.service import ActionFactory
from app.services.draft_service import DraftService
from app.services.memory.memory_service import MemoryService
from app.services.memory.patterns import clock_hour
from app.services.memory.stated_rules import RULE_MEMORY_KEY
from app.services.planning.rule_windows import to_horizon

logger = logging.getLogger(__name__)


REFRESHED = "I refreshed the plan first — it's in place now."
NOTHING_LEFT = "Nothing left to place today."
COULDNT_FIT_REST = "I couldn't fit what's left into today — want to look at it together?"


class ApplyDraftAction(ProactiveAction):
    """Accept a drafted plan: its tasks are written, each with its slot.

    Only while the plan still holds. A slot that has begun, or a task finished or
    skipped since it was drafted, means it no longer does: the user's open tasks are
    drafted again from now (through the ``DraftPlanner``) and that plan is put in
    place instead, and the result says so. Nothing left to place, the stale draft is
    expired and the action is done. Accepting slots that have gone by would hand the
    slot sweep tasks to set aside as if the user had.
    """

    kind: ClassVar[str] = "apply_plan"
    risk: ClassVar[ActionRisk] = ActionRisk.SCHEDULE_CHANGE

    def __init__(self, draft_id: str, *, drafts: DraftService, planner: DraftPlanner) -> None:
        self._draft_id = draft_id
        self._drafts = drafts
        self._planner = planner

    def describe(self) -> str:
        return "Put today's plan in place"

    def dedupe_key(self) -> str:
        return f"apply_plan:{self._draft_id}"

    def to_payload(self) -> dict:
        return {"kind": self.kind, "dedupe_key": self.dedupe_key(), "draft_id": self._draft_id}

    async def execute(self, user_id: str) -> ActionResult:
        """Put the plan in place, refreshed first if it no longer holds.

        InvalidStateTransition when the draft closed meanwhile (a newer plan replaced
        it); NotFoundError when it is gone; LLMUnavailableError when a refresh could
        not read the user's habits (a later try can).
        """
        draft = await self._drafts.get(user_id, self._draft_id)
        draft.transition(DraftStatus.ACCEPTED)  # closed meanwhile: out of date
        currency = await self._drafts.currency(user_id, draft)
        if currency.current:
            written = await self._drafts.accept(user_id, self._draft_id)
            return ActionResult(
                kind=self.kind,
                message="Done — today's plan is in place.",
                tasks_written=written,
                draft_id=self._draft_id,
            )
        try:
            fresh = await self._planner.replan(
                user_id, reslot_task_ids=sorted(currency.set_aside)
            )
        except errors.ScheduleInfeasibleError:
            await self._drafts.expire(user_id, self._draft_id)
            return ActionResult(kind=self.kind, message=COULDNT_FIT_REST)
        if fresh is None:
            await self._drafts.expire(user_id, self._draft_id)
            return ActionResult(kind=self.kind, message=NOTHING_LEFT)
        written = await self._drafts.accept(user_id, fresh.id)
        return ActionResult(
            kind=self.kind, message=REFRESHED, tasks_written=written, draft_id=fresh.id
        )


# A pattern the user let go of since it was proposed is never made a rule, by status.
_PATTERN_LET_GO = {
    "dismissed": "You told me this pattern doesn't fit, so I haven't made it a rule.",
    "forgotten": "You asked me to forget this pattern, so I haven't made it a rule.",
}


class AdoptRuleAction(ProactiveAction):
    """Keep an hour the user keeps setting aside clear of all work, from now on.

    The rule is a hard block on that hour; the proposal's words say so ("keep that
    hour clear"). A learned pattern alone only softens the hour (light work may still
    go there); accepting the rule is what clears it.

    It carries the pattern's numbers as they were when it was proposed (``confidence``
    and ``occurrences``), for the suggestion to show.
    """

    kind: ClassVar[str] = "adopt_rule"
    risk: ClassVar[ActionRisk] = ActionRisk.SCHEDULE_CHANGE

    def __init__(
        self,
        pattern_key: str,
        hour: int,
        memory_id: str,
        *,
        confidence: float,
        occurrences: int,
        constraints: ConstraintRepository,
        memory: MemoryService,
        planner: DraftPlanner,
    ) -> None:
        if not 0 <= hour < HOURS_PER_DAY:
            raise errors.ValidationError("An hour runs from 0 to 23.", details={"hour": hour})
        self._pattern_key = pattern_key
        self._hour = hour
        self._memory_id = memory_id
        self._confidence = float(confidence)
        self._occurrences = occurrences
        self._constraints = constraints
        self._memory = memory
        self._planner = planner

    def describe(self) -> str:
        return f"Keep {clock_hour(self._hour)} clear"

    def dedupe_key(self) -> str:
        return f"pattern_rule:{self._pattern_key}"

    def blocked_hours(self) -> tuple[int, int] | None:
        return (self._hour, self._hour + 1)

    def to_payload(self) -> dict:
        return {
            "kind": self.kind,
            "dedupe_key": self.dedupe_key(),
            "pattern_key": self._pattern_key,
            "hour": self._hour,
            "memory_id": self._memory_id,
            "confidence": self._confidence,
            "occurrences": self._occurrences,
        }

    async def execute(self, user_id: str) -> ActionResult:
        """Confirm the pattern, store the rule (once, however often this runs), redraft.

        A pattern the user dismissed or forgot since it was proposed is a ConflictError,
        before anything is stored: no rule, no draft.

        The rule carries its blocked slot already worked out, so planning never spends a
        model call translating it. Nothing to plan, or nothing that fits, is no draft:
        the rule still holds for every plan after it. Habits that can't be read right
        now mean no draft yet, and the message says the redraft waits for them.
        """
        try:
            await self._memory.confirm(user_id, self._memory_id)
        except errors.ConflictError as exc:  # dismissed or forgotten since it was proposed
            status = (exc.details or {}).get("status")
            raise errors.ConflictError(
                _PATTERN_LET_GO.get(status, _PATTERN_LET_GO["dismissed"]),
                details={"memory_id": self._memory_id, "status": status},
            ) from exc
        if not await self._rule_stored(user_id):
            await self._constraints.add(user_id, self._rule(user_id))
        h12 = clock_hour(self._hour)
        saved = f"Saved. Every plan from now on keeps {h12} clear"
        try:
            draft = await self._planner.replan(user_id, reslot_task_ids=[])
        except errors.ScheduleInfeasibleError:
            draft = None
        except errors.LLMUnavailableError:  # the habits could not be read: no draft yet
            return ActionResult(
                kind=self.kind,
                message=(
                    f"{saved} — I'll draft today's around it as soon as I can read your"
                    " habits again."
                ),
            )
        if draft is None:
            return ActionResult(kind=self.kind, message=f"{saved}.")
        return ActionResult(
            kind=self.kind,
            message=(
                f"Saved. I've drafted a plan that keeps {h12} clear"
                " — accept it when it suits you."
            ),
            draft_id=draft.id,
        )

    async def _rule_stored(self, user_id: str) -> bool:
        """Whether this pattern's rule is already stored (a retried accept)."""
        for constraint in await self._constraints.list_active(user_id):
            semantics = constraint.structured_semantics or {}
            if semantics.get("pattern_key") == self._pattern_key:
                return True
        return False

    def _rule(self, user_id: str) -> BehavioralConstraint:
        h = self._hour
        start = to_horizon(h * 60)
        slot = SemanticTimeSlot(
            name=f"learned_rule_{h:02d}",
            start_min=start,
            end_min=start + 60,
            availability="blocked",
            recurrence="daily",
        )
        return BehavioralConstraint(
            id=uuid4().hex,
            user_id=user_id,
            raw_text=f"No work between {h:02d}:00 and {h + 1:02d}:00",
            constraint_type="habit",
            recurrence="daily",
            source="inferred",
            structured_semantics={
                "pattern_key": self._pattern_key,
                "semantic_slots": [slot.model_dump(mode="json")],
                # Linked to the pattern it was adopted from, as a stated rule is to the
                # memory of saying it: letting go of one lets go of the other.
                RULE_MEMORY_KEY: self._memory_id,
            },
        )


class ShareInsightAction(ProactiveAction):
    """Something Jarvis noticed. Accepting it changes nothing.

    ``key`` is what it is about, so a dismissal holds for exactly that: a pattern's key
    (dismissed once, never shared again, whatever its numbers become), or one day's
    brief (``brief:{local day}``: tomorrow's is a new note).
    """

    kind: ClassVar[str] = "share_insight"
    risk: ClassVar[ActionRisk] = ActionRisk.READ_ONLY

    def __init__(self, insight: str, *, key: str) -> None:
        self._insight = insight
        self._key = key

    def describe(self) -> str:
        return self._insight

    def dedupe_key(self) -> str:
        return self.key_for(self._key)

    @staticmethod
    def key_for(key: str) -> str:
        """The dedupe_key of a note about ``key`` (a pattern's key, or one day's brief)."""
        return f"share_insight:{key}"

    def to_payload(self) -> dict:
        return {
            "kind": self.kind,
            "dedupe_key": self.dedupe_key(),
            "insight": self._insight,
            "key": self._key,
        }

    async def execute(self, user_id: str) -> ActionResult:
        return ActionResult(kind=self.kind, message="Noted.")


def _field(payload: dict, name: str, kind: type | tuple[type, ...]) -> Any:
    """``payload[name]`` if it is a ``kind``; else the suggestion can't be carried out."""
    value = payload.get(name)
    if not isinstance(value, kind) or isinstance(value, bool):
        raise errors.ValidationError(
            "This suggestion can't be carried out.",
            details={"kind": payload.get("kind"), "field": name},
        )
    return value


def build_action_factory(
    *,
    drafts: DraftService,
    constraints: ConstraintRepository,
    memory: MemoryService,
    planner: DraftPlanner,
) -> ActionFactory:
    """An ``ActionFactory`` that rebuilds every action the triggers propose."""
    factory = ActionFactory()
    factory.register(
        ApplyDraftAction.kind,
        lambda p: ApplyDraftAction(_field(p, "draft_id", str), drafts=drafts, planner=planner),
    )
    factory.register(
        AdoptRuleAction.kind,
        lambda p: AdoptRuleAction(
            _field(p, "pattern_key", str),
            _field(p, "hour", int),
            _field(p, "memory_id", str),
            confidence=_field(p, "confidence", (int, float)),
            occurrences=_field(p, "occurrences", int),
            constraints=constraints,
            memory=memory,
            planner=planner,
        ),
    )
    factory.register(
        ShareInsightAction.kind,
        lambda p: ShareInsightAction(_field(p, "insight", str), key=_field(p, "key", str)),
    )
    return factory
