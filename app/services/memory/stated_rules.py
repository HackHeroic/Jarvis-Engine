"""A rule the user states, and the memory of saying it: one thing, two views.

A turn that states a rule ("never schedule before 11 AM") stores it twice: a rule
(a behavioural constraint, which planning reads and the You page lists) and a
constraint memory (which recall gives the model and the Memory panel lists). The turn
links them as it writes the memory: the rule's ``structured_semantics`` carries the
memory's id under RULE_MEMORY_KEY. That link is the only way the two are matched,
never their words.

Planning plans from the rule alone and skips the memory a rule covers
(``memory_slots(covered=...)``). Letting go of either lets go of both: forgetting the
memory deletes every rule linked to it, and deleting a rule forgets its memory (and so
every copy of the rule said again). Each move is user-scoped and safe to repeat.
"""

import logging
from collections.abc import Iterable

from app.domain import errors
from app.domain.entities import BehavioralConstraint
from app.domain.ports import ConstraintRepository
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


def covered_memory_ids(rules: Iterable[BehavioralConstraint | dict]) -> frozenset[str]:
    """The memories these rules are linked to: planning has them from the rules already."""
    return frozenset(filter(None, map(rule_memory_id, rules)))


class StatedRules:
    """Keeps a stated rule and the memory of stating it together."""

    def __init__(self, rules: ConstraintRepository, memory: MemoryService) -> None:
        self._rules = rules
        self._memory = memory

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

    async def forget_memory(self, user_id: str, memory_id: str) -> None:
        """Forget the memory (``MemoryService.forget``) and delete every rule linked to it.

        Another user's memory is NotFoundError, and nothing is deleted. Forgetting it
        again deletes whatever linked rule is left, which is nothing.
        """
        await self._memory.forget(user_id, memory_id)
        await self._delete_linked(user_id, memory_id, await self._rules.list_active(user_id))

    async def delete_rule(self, user_id: str, rule_id: str) -> bool:
        """Delete the user's rule and forget the memory it is linked to, if any, with
        every other rule linked to that memory (the same rule said again).

        The memory goes first, so a retry after a failure in between still finds the
        rule and finishes. Returns whether the rule was there to delete (another user's
        is not).
        """
        rules = await self._rules.list_active(user_id)
        memory_id = rule_memory_id(next((r for r in rules if r.id == rule_id), None))
        if memory_id is not None:
            try:
                await self._memory.forget(user_id, memory_id)
            except errors.NotFoundError:  # the memory is gone already
                logger.info("A deleted rule's memory was already gone")
        deleted = await self._rules.delete(user_id, rule_id)
        if memory_id is not None:
            await self._delete_linked(
                user_id, memory_id, [r for r in rules if r.id != rule_id]
            )
        return deleted

    async def _find(self, user_id: str, rule_id: str) -> BehavioralConstraint | None:
        return next(
            (r for r in await self._rules.list_active(user_id) if r.id == rule_id), None
        )

    async def _delete_linked(
        self, user_id: str, memory_id: str, rules: list[BehavioralConstraint]
    ) -> None:
        for rule in rules:
            if rule_memory_id(rule) == memory_id:
                await self._rules.delete(user_id, rule.id)
