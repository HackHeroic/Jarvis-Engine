"""A rule the user states, and the memory of saying it: one thing, two views.

A turn that states a rule ("never schedule before 11 AM") stores it twice: a rule
(a behavioural constraint, which planning reads and the You page lists) and a
constraint memory (which recall gives the model and the Memory panel lists). The turn
links them as it writes the memory: the rule's ``structured_semantics`` carries the
memory's id under RULE_MEMORY_KEY. That link is the only way the two are matched,
never their words.

Planning plans from the rule alone and skips the memory a rule covers
(``memory_slots(covered=...)``). Letting go of either lets go of both: forgetting the
memory deletes every rule linked to it (it says them all), and deleting a rule deletes
the copies of it said again in the same words and forgets its memory, unless a
differently worded rule still rests on that memory. Deleting one rule never removes
another. Correcting the statement (a newer memory supersedes the memory) deletes the
rules linked to the old memory; the corrected rule rests on the new one. Each move is
user-scoped and safe to repeat. A rule's check-ins (``RuleCheckIns``) end just before
the rule is deleted, by whichever move.
"""

import logging
import re
from collections.abc import Iterable

from app.domain import errors
from app.domain.entities import BehavioralConstraint, Memory
from app.domain.ports import ConstraintRepository
from app.services.analytical.rule_check_ins import RuleCheckIns
from app.services.memory.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Where a rule's structured_semantics names the memory of the user stating it.
RULE_MEMORY_KEY = "memory_id"


def rule_memory_id(rule: BehavioralConstraint | dict | None) -> str | None:
    """The id of the memory ``rule`` (an entity, or a behavioral_constraints row) is
    linked to; None for a rule with no link."""
    if rule is None:
        return None
    semantics = (
        rule.structured_semantics
        if isinstance(rule, BehavioralConstraint)
        else rule.get("structured_semantics")
    )
    linked = semantics.get(RULE_MEMORY_KEY) if isinstance(semantics, dict) else None
    return linked if isinstance(linked, str) and linked else None


def _normalised(text: str | None) -> str:
    """A rule's words for comparing two rules: case, spacing and punctuation aside."""
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower()))


def _adopted(rule: BehavioralConstraint) -> bool:
    """Whether the rule was adopted from a learned pattern (it names the pattern's key)."""
    return bool((rule.structured_semantics or {}).get("pattern_key"))


def covered_memory_ids(rules: Iterable[BehavioralConstraint | dict]) -> frozenset[str]:
    """The memories these rules are linked to: planning has them from the rules already."""
    return frozenset(filter(None, map(rule_memory_id, rules)))


class StatedRules:
    """Keeps a stated rule and the memory of stating it together."""

    def __init__(
        self,
        rules: ConstraintRepository,
        memory: MemoryService,
        *,
        check_ins: RuleCheckIns | None = None,
    ) -> None:
        """``check_ins`` ends a deleted rule's check-ins; None (tests, scripts): there
        are none to end, and a read of the check-ins never lists a deleted rule anyway."""
        self._rules = rules
        self._memory = memory
        self._check_ins = check_ins

    async def link(self, user_id: str, rule_id: str, memory_id: str) -> bool:
        """Link the user's rule to the memory of stating it; False when there is no such
        rule. The rule's other semantics are kept."""
        rule = await self._find(user_id, rule_id)
        if rule is None:
            return False
        if rule_memory_id(rule) == memory_id:
            return True
        semantics = {**(rule.structured_semantics or {}), RULE_MEMORY_KEY: memory_id}
        return await self._rules.set_semantics(user_id, rule_id, semantics) is not None

    async def forget_memory(self, user_id: str, memory_id: str) -> list[str]:
        """Forget the memory (``MemoryService.forget``) and delete every rule linked to it;
        return the ids of the rules deleted.

        Letting go of the memory lets go of everything it says: a memory of saying two
        rules at once takes both. Another user's memory is NotFoundError, and nothing is
        deleted. Forgetting it again deletes whatever linked rule is left (none).
        """
        await self._memory.forget(user_id, memory_id)
        return await self._delete_linked(user_id, memory_id)

    async def replace_for_superseded(self, user_id: str, old_memory_id: str) -> list[str]:
        """The user corrected the memory ``old_memory_id`` (a newer memory supersedes it):
        delete every rule linked to it, with their check-ins, as ``forget_memory`` does,
        and leave the memory as it is. Return the ids of the rules deleted.

        The corrected rule is stored and linked to the new memory by the same turn, so
        only what the old statement said goes. A memory no rule rests on (a plain fact),
        or another user's, deletes nothing. Safe to repeat.
        """
        return await self._delete_linked(user_id, old_memory_id)

    async def _delete_linked(self, user_id: str, memory_id: str) -> list[str]:
        linked = [
            r for r in await self._rules.list_active(user_id) if rule_memory_id(r) == memory_id
        ]
        for rule in linked:
            await self._delete(user_id, rule.id)
        return [rule.id for rule in linked]

    async def dismiss_memory(self, user_id: str, memory_id: str) -> tuple[Memory, list[str]]:
        """Dismiss the memory (``MemoryService.dismiss``: the user says it does not hold)
        and delete every rule linked to it; return the memory and the ids of the rules
        deleted.

        A rule adopted from a learned pattern goes with the pattern. A forgotten memory is
        InvalidStateTransition (409), and nothing is deleted.
        """
        dismissed = await self._memory.dismiss(user_id, memory_id)
        return dismissed, await self._delete_linked(user_id, memory_id)

    async def delete_rule(self, user_id: str, rule_id: str) -> bool:
        """Delete the user's rule, with any copy of it said again in the same words; never
        a differently worded rule.

        The memory of stating it is forgotten too, unless a differently worded rule still
        rests on it (one memory of saying two rules at once, or a later rule reinforced
        into the same memory): then the memory stays for that rule. A rule adopted from
        a learned pattern dismisses the pattern instead (kept, never proposed again).
        The memory goes first, so a retry after a failure in between still finds the
        rule and finishes. Returns whether the rule was there to delete (another
        user's is not).
        """
        rules = await self._rules.list_active(user_id)
        rule = next((r for r in rules if r.id == rule_id), None)
        memory_id = rule_memory_id(rule)
        if rule is None or memory_id is None:
            return await self._delete(user_id, rule_id)
        words = _normalised(rule.raw_text)
        linked = [r for r in rules if rule_memory_id(r) == memory_id]
        copies = [r for r in linked if r.id != rule_id and _normalised(r.raw_text) == words]
        if all(_normalised(r.raw_text) == words for r in linked):
            await self._let_go_of(user_id, memory_id, learned=_adopted(rule))
        deleted = await self._delete(user_id, rule_id)
        for copy in copies:
            await self._delete(user_id, copy.id)
        return deleted

    async def _delete(self, user_id: str, rule_id: str) -> bool:
        """Delete one of the user's rules, its check-ins first (the database's key would
        only clear their link to it). Returns whether the rule was there."""
        if self._check_ins is not None:
            await self._check_ins.stop(user_id, rule_id)
        return await self._rules.delete(user_id, rule_id)

    async def _let_go_of(self, user_id: str, memory_id: str, *, learned: bool) -> None:
        """Forget the memory of saying a rule; dismiss a learned pattern a rule came from.
        One already let go of (or gone) stays as it is."""
        try:
            if learned:
                await self._memory.dismiss(user_id, memory_id)
            else:
                await self._memory.forget(user_id, memory_id)
        except (errors.NotFoundError, errors.InvalidStateTransition):
            logger.info("A deleted rule's memory was already gone or let go of")

    async def _find(self, user_id: str, rule_id: str) -> BehavioralConstraint | None:
        return next(
            (r for r in await self._rules.list_active(user_id) if r.id == rule_id), None
        )
