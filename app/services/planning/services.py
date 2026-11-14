"""What the planning sub-graph works with, handed to it as one record."""

from dataclasses import dataclass

from app.domain.ports import (
    ConstraintRepository,
    EnergyEstimator,
    ProfileRepository,
    TaskRepository,
)
from app.services.draft_service import DraftService
from app.services.memory.memory_service import MemoryService
from app.services.task_service import TaskService


@dataclass(frozen=True)
class PlanningServices:
    """The ports and services the planning sub-graph uses, and only those (ISP).

    The composition root builds it once (``AppContainer.planning``). A planning turn
    reaches it through the turn's state (``planning_state_in`` copies it from the
    container); the replanner holds it from its constructor. Never looked up.
    """

    drafts: DraftService  # proposes the plan for review; accepting it is the only task write
    tasks: TaskService  # every task status change
    task_repo: TaskRepository  # the user's pending and set-aside tasks
    constraints: ConstraintRepository  # the user's stored rules
    profiles: ProfileRepository  # the user's timezone
    memory: MemoryService  # stated rules and learned patterns, as memories
    energy: EnergyEstimator  # the learned focus profile the solver leans on
