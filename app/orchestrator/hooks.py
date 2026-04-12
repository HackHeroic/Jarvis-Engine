"""Action hooks — synchronous blocking gates for consent, PII, cost.

7 hook events total. This file creates the ActionHooks class and the
PreCloudLLM (PII filter) handler. Remaining 6 handlers added in Layer 5.
"""

import re
from enum import Enum
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


class HookDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    MODIFY = "modify"


@dataclass
class HookResult:
    decision: HookDecision
    modified_input: Optional[dict] = None
    reason: Optional[str] = None


HookHandler = Callable[..., Awaitable[HookResult]]


class ActionHooks:
    """Lightweight hook pipeline. 7 events, simple registry."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[HookHandler]] = {}

    def register(self, event: str, handler: HookHandler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def execute(self, event: str, **context: Any) -> HookResult:
        """Run all handlers for an event. First DENY or ASK wins."""
        for handler in self._handlers.get(event, []):
            result = await handler(**context)
            if result.decision in (HookDecision.DENY, HookDecision.ASK, HookDecision.MODIFY):
                return result
        return HookResult(decision=HookDecision.ALLOW)


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")


async def pii_filter_hook(prompt: str, **kwargs: Any) -> HookResult:
    """Strip PII before sending to cloud LLM. Phase 1: regex-based."""
    filtered = prompt
    modified = False

    email_matches = _EMAIL_RE.findall(filtered)
    if email_matches:
        for email in email_matches:
            filtered = filtered.replace(email, "[EMAIL]")
        modified = True

    phone_matches = _PHONE_RE.findall(filtered)
    if phone_matches:
        for phone in phone_matches:
            filtered = filtered.replace(phone, "[PHONE]")
        modified = True

    if modified:
        return HookResult(
            decision=HookDecision.MODIFY,
            modified_input={"prompt": filtered},
            reason=f"Stripped {len(email_matches)} emails, {len(phone_matches)} phones",
        )
    return HookResult(decision=HookDecision.ALLOW)


async def consent_gate_module(initiated_by: str = "user", module: str = "", **kwargs: Any) -> HookResult:
    """Gate module execution. User-initiated always allowed; system-initiated requires consent."""
    if initiated_by == "user":
        return HookResult(decision=HookDecision.ALLOW)
    return HookResult(decision=HookDecision.ASK, reason=f"I noticed something and want to run {module} — OK?")


async def consent_gate_schedule(task_count: int = 0, goal_count: int = 1, **kwargs: Any) -> HookResult:
    """Gate schedule modifications. Always asks for consent."""
    return HookResult(
        decision=HookDecision.ASK,
        reason=f"I'd like to schedule {task_count} tasks across {goal_count} goals. OK to proceed?"
    )


async def post_module_telemetry(module: str = "", **kwargs: Any) -> HookResult:
    """Log telemetry after module execution."""
    return HookResult(decision=HookDecision.ALLOW)


async def memory_write_gate(**kwargs: Any) -> HookResult:
    """Gate writes to persistent memory (Strategy Hub)."""
    return HookResult(decision=HookDecision.ALLOW)


async def cost_threshold_check(token_count: int = 0, threshold: int = 5_000_000, **kwargs: Any) -> HookResult:
    """Check token usage against cost threshold. Asks user if exceeded."""
    if token_count > threshold:
        return HookResult(
            decision=HookDecision.ASK,
            reason=f"This session has used {token_count:,} tokens. Continue?"
        )
    return HookResult(decision=HookDecision.ALLOW)


async def proactive_suggestion_gate(**kwargs: Any) -> HookResult:
    """Gate proactive suggestions (e.g., optimizations, new insights)."""
    return HookResult(decision=HookDecision.ALLOW)


def register_all_hooks(hooks: ActionHooks) -> None:
    """Register all 7 hook handlers to the ActionHooks instance."""
    hooks.register("PreCloudLLM", pii_filter_hook)
    hooks.register("PreModuleExecution", consent_gate_module)
    hooks.register("PreScheduleModify", consent_gate_schedule)
    hooks.register("PostModuleExecution", post_module_telemetry)
    hooks.register("PreMemoryWrite", memory_write_gate)
    hooks.register("CostThreshold", cost_threshold_check)
    hooks.register("ProactiveSuggestion", proactive_suggestion_gate)
