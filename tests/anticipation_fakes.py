"""Test doubles for the anticipation framework: actions, a trigger and a model that record.

Shared by the trigger-template, service, scheduler and API suites. Nothing here
touches the network.
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from app.domain.anticipation import (
    ActionResult,
    ActionRisk,
    ProactiveAction,
    SuggestionDraft,
    Trigger,
    TriggerContext,
)
from app.domain.entities import LLMLocation
from app.domain.ports import LLMProvider, LLMRequest


@dataclass
class ActionLog:
    """What every ``FakeAction`` sharing this log did.

    ``started`` counts every ``execute`` begun. While ``hold`` is set to an unset
    event, ``execute`` waits on it before anything else. ``errors`` are raised one
    per ``execute``, in order, before anything runs.
    """

    runs: list[str] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)
    started: int = 0
    hold: asyncio.Event | None = None


async def until_started(log: ActionLog, timeout: float = 2.0) -> None:
    """Wait until an action sharing ``log`` has begun to run; TimeoutError if none does."""

    async def wait() -> None:
        while log.started == 0:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout)


class FakeAction(ProactiveAction):
    """A schedule change that records each run in its ``ActionLog``."""

    kind: ClassVar[str] = "fake_action"
    risk: ClassVar[ActionRisk] = ActionRisk.SCHEDULE_CHANGE

    def __init__(
        self,
        log: ActionLog | None = None,
        *,
        key: str | None = None,
        hours: tuple[int, int] | None = None,
    ) -> None:
        self.log = log if log is not None else ActionLog()
        self._key = key
        self._hours = hours

    def describe(self) -> str:
        return "Move a task to a better hour"

    def to_payload(self) -> dict:
        return {"kind": self.kind, "dedupe_key": self.dedupe_key(), "hours": self._hours}

    async def execute(self, user_id: str) -> ActionResult:
        self.log.started += 1
        if self.log.hold is not None:
            await self.log.hold.wait()
        await asyncio.sleep(0)  # a real action awaits storage: let a racing request in
        if self.log.errors:
            raise self.log.errors.pop(0)
        self.log.runs.append(user_id)
        return ActionResult(kind=self.kind, message="It's on your plan now.", draft_id="draft-1")

    def dedupe_key(self) -> str:
        return self._key or self.kind

    def blocked_hours(self) -> tuple[int, int] | None:
        return self._hours

    @classmethod
    def builder(cls, log: ActionLog):
        """An ``ActionFactory`` builder: rebuilds the action from its payload, on ``log``."""

        def build(payload: dict) -> "FakeAction":
            hours = payload.get("hours")
            return cls(log, key=payload.get("dedupe_key"), hours=tuple(hours) if hours else None)

        return build


class FakeLowRiskAction(FakeAction):
    kind: ClassVar[str] = "fake_low_risk"
    risk: ClassVar[ActionRisk] = ActionRisk.LOW_RISK


class FakeReadOnlyAction(FakeAction):
    kind: ClassVar[str] = "fake_read_only"
    risk: ClassVar[ActionRisk] = ActionRisk.READ_ONLY


class CountingTrigger(Trigger):
    """A trigger that counts every step the template asks of it.

    ``gathered=None`` means it has nothing to say. ``fail_for`` users make
    ``gather`` raise. ``ask_model`` makes ``build_suggestion`` phrase the body
    through ``route_llm_call``, as a model-backed trigger would. ``gather`` yields
    to the event loop once, as a real read of storage does.
    """

    name: ClassVar[str] = "counting"

    def __init__(
        self,
        *,
        due: bool = True,
        gathered: Any = "Two hard tasks sit in your low-focus hours.",
        action: ProactiveAction | None = None,
        fail_for: frozenset[str] = frozenset(),
        ask_model: bool = False,
        expires_at: datetime | None = None,
    ) -> None:
        self.due = due
        self.gathered = gathered
        self.action = action if action is not None else FakeAction()
        self.fail_for = fail_for
        self.ask_model = ask_model
        self.expires_at = expires_at
        self.calls: Counter[str] = Counter()

    async def should_fire(self, ctx: TriggerContext) -> bool:
        self.calls["should_fire"] += 1
        return self.due

    async def gather(self, ctx: TriggerContext) -> Any | None:
        self.calls["gather"] += 1
        await asyncio.sleep(0)
        if ctx.user_id in self.fail_for:
            raise RuntimeError("secret connection string in the driver text")
        return self.gathered

    async def build_suggestion(self, ctx: TriggerContext, gathered: Any) -> SuggestionDraft:
        self.calls["build_suggestion"] += 1
        body = str(gathered)
        if self.ask_model:
            from app.core.model_router import route_llm_call

            body = await route_llm_call(task="voice_of_jarvis", prompt=f"Say kindly: {gathered}")
        return SuggestionDraft(
            kind="nudge",
            title="A thought for today",
            body=body,
            action=self.action,
            expires_at=self.expires_at,
        )


class RecordingModel(LLMProvider):
    """A cloud model that answers every call with one reply and records each request."""

    name = "recording"
    location = LLMLocation.CLOUD

    def __init__(self, reply: str = "Shall I move it to the morning?") -> None:
        self.reply = reply
        self.calls: list[LLMRequest] = []

    def is_available(self) -> bool:
        return True

    async def complete(self, req: LLMRequest) -> str:
        self.calls.append(req)
        return self.reply

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        self.calls.append(req)
        yield ("content", self.reply)
