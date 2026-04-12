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
