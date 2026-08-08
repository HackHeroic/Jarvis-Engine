"""Phase 2 — Deterministic Scheduler (OR-Tools CP-SAT).

Implements the Anti-Guilt Architecture: the OR-Tools solver prevents the LLM
from hallucinating overlapping tasks by treating the human as a biological
constraint (Theory of Constraints). All temporal math is deterministic;
the solver returns 100% constraint-satisfying schedules or INFEASIBLE,
triggering Socratic recalibration rather than user guilt.

Uses the modern Python OR-Tools API (snake_case). Do NOT use legacy C++
CamelCase (NewIntVar, etc.) — they raise AttributeError.
"""

from __future__ import annotations

import math
from typing import Optional

from ortools.sat.python import cp_model

MINUTES_PER_DAY = 1440


class JarvisScheduler:
    """CP-SAT scheduler for deterministic task placement.

    Consumes task decomposition from Phase 1 (Socratic Chunker), enforces
    hard blocks (sleep), precedence (dependencies), and single-tasking
    (AddNoOverlap). The TMT-weighted objective schedules high-priority
    tasks earlier while minimizing makespan.
    """

    def __init__(
        self,
        horizon_minutes: int = 2880,
        max_daily_deep_work_minutes: Optional[int] = None,
        slack_ratio: float = 1.0,
    ) -> None:
        """Initialize the constraint model and storage.

        Args:
            horizon_minutes: Planning window in minutes (default 48 hours).
            max_daily_deep_work_minutes: Cap on scheduled work per day; None to skip.
            slack_ratio: horizon_minutes / total_task_minutes; higher = favor spread.
        """
        self.model = cp_model.CpModel()
        self.horizon = horizon_minutes
        self.max_daily_deep_work_minutes = max_daily_deep_work_minutes
        self.slack_ratio = slack_ratio
        self.tasks: dict[str, dict] = {}
        self.hard_blocks: list = []
        self.soft_blocks: list[tuple] = []  # (interval_var, max_duration, max_difficulty)
        self._load_d_vars: list = []  # for spread objective

    def add_hard_block(self, start_min: int, end_min: int, name: str) -> None:
        """Add a non-negotiable block (e.g., sleep 23:00–07:00).

        Uses model.new_interval_var with integer constants. Do NOT use
        NewFixedIntervalVar — it does not exist in the Python OR-Tools API.

        Args:
            start_min: Block start time in minutes from horizon zero.
            end_min: Block end time in minutes from horizon zero.
            name: Identifier for the block.
        """
        duration = end_min - start_min
        iv = self.model.new_interval_var(
            start_min, duration, end_min, f"hard_{name}"
        )
        self.hard_blocks.append(iv)

    def add_soft_block(
        self,
        start_min: int,
        end_min: int,
        name: str,
        max_task_duration: int = 15,
        max_difficulty: float = 0.4,
    ) -> None:
        """Add a soft block (e.g., back-bench lecture) — only short, low-difficulty tasks may overlap.

        Args:
            start_min: Block start time in minutes from horizon zero.
            end_min: Block end time in minutes from horizon zero.
            name: Identifier for the block.
            max_task_duration: Max task duration in minutes to allow overlap.
            max_difficulty: Max difficulty_weight (0–1) to allow overlap.
        """
        duration = end_min - start_min
        iv = self.model.new_interval_var(
            start_min, duration, end_min, f"soft_{name}"
        )
        self.soft_blocks.append((iv, max_task_duration, max_difficulty))

    def add_task(
        self,
        task_id: str,
        duration: int,
        priority_score: int,
        dependencies: list[str],
        difficulty_weight: float = 1.0,
    ) -> None:
        """Add a schedulable task with variable start/end.

        Args:
            task_id: Unique identifier.
            duration: Task duration in minutes.
            priority_score: TMT-derived integer for objective weighting.
            dependencies: List of task_ids that must finish before this one.
            difficulty_weight: Cognitive load 0–1; used for soft-block qualification.
        """
        start_var = self.model.new_int_var(
            0, self.horizon, f"start_{task_id}"
        )
        end_var = self.model.new_int_var(0, self.horizon, f"end_{task_id}")
        interval_var = self.model.new_interval_var(
            start_var, duration, end_var, f"itv_{task_id}"
        )
        self.tasks[task_id] = {
            "interval": interval_var,
            "start": start_var,
            "end": end_var,
            "priority": priority_score,
            "dependencies": dependencies,
            "duration": duration,
            "difficulty_weight": difficulty_weight,
        }

    def build_dependencies(self) -> None:
        """Enforce precedence: Task B cannot start until Task A ends.

        For each task B and each dependency A in B.dependencies, adds
        start_B >= end_A. Skips invalid references (A not in tasks).
        """
        for task_id, data in self.tasks.items():
            for dep_id in data["dependencies"]:
                if dep_id in self.tasks:
                    self.model.add(
                        data["start"] >= self.tasks[dep_id]["end"]
                    )

    def _build_daily_load_constraints(self) -> None:
        """Enforce max_daily_deep_work_minutes per day. Skip if horizon <= 1 day."""
        cap = self.max_daily_deep_work_minutes
        if cap is None or self.horizon <= MINUTES_PER_DAY:
            return
        num_days = math.ceil(self.horizon / MINUTES_PER_DAY)
        self._load_d_vars = []
        # contribs_by_task[t] = list of contrib vars for task t across days
        contribs_by_task: dict[str, list] = {tid: [] for tid in self.tasks}
        for d in range(num_days):
            day_start = d * MINUTES_PER_DAY
            day_end = (d + 1) * MINUTES_PER_DAY
            if day_end > self.horizon:
                break
            contribs = []
            for task_id, t in self.tasks.items():
                start_var = t["start"]
                duration = t["duration"]
                b = self.model.new_bool_var(f"on_day_{task_id}_d{d}")
                self.model.add(start_var >= day_start).only_enforce_if(b)
                self.model.add(start_var < day_end).only_enforce_if(b)
                contrib = self.model.new_int_var(0, duration, f"contrib_{task_id}_d{d}")
                self.model.add(contrib == duration).only_enforce_if(b)
                self.model.add(contrib == 0).only_enforce_if(b.Not())
                contribs.append(contrib)
                contribs_by_task[task_id].append(contrib)
            load_d = self.model.new_int_var(0, cap, f"load_d{d}")
            self.model.add(load_d == sum(contribs))
            self.model.add(load_d <= cap)
            self._load_d_vars.append(load_d)
        # Each task's contrib sums to its duration (ensures exact one day gets it)
        for task_id, t in self.tasks.items():
            self.model.add(sum(contribs_by_task[task_id]) == t["duration"])

    def solve(
        self,
    ) -> tuple[dict[str, dict[str, int]], str] | tuple[str, str]:
        """Solve the constraint model and return the schedule.

        Applies AddNoOverlap to all intervals (tasks + hard blocks),
        builds dependencies, and minimizes a weighted objective:
        makespan + Σ(priority_i × start_i) so high-priority tasks
        are scheduled earlier.

        The search is capped at ``SOLVER_MAX_TIME_SECONDS``; see config for why.
        An expired cap yields FEASIBLE (best schedule found so far, returned as
        normal) or UNKNOWN, which joins the INFEASIBLE branch — "I ran out of
        time" and "no such schedule exists" leave the caller the same two moves,
        widen the horizon or cut scope, and both are already handled there.

        Returns:
            Dict mapping task_id to {"start": start_min, "end": end_min}
            if FEASIBLE or OPTIMAL; otherwise "INFEASIBLE".
        """
        from app.core.config import SOLVER_MAX_TIME_SECONDS

        # Collect all interval variables (Anti-Guilt: user does one thing at a time)
        all_intervals = (
            [t["interval"] for t in self.tasks.values()] + self.hard_blocks
        )
        self.model.add_no_overlap(all_intervals)

        # Soft blocks: non-qualifying tasks cannot overlap them
        for soft_iv, max_dur, max_diff in self.soft_blocks:
            for task_id, t in self.tasks.items():
                duration = t["duration"]
                diff = t["difficulty_weight"]
                if duration > max_dur or diff > max_diff:
                    self.model.add_no_overlap([t["interval"], soft_iv])

        self.build_dependencies()
        self._build_daily_load_constraints()

        # TMT-weighted objective: minimize makespan AND early start for high-priority
        weight_makespan = 15  # Balance: makespan ~500–2880, sum(pri*start) ~5k–50k
        obj_var = self.model.new_int_var(0, self.horizon, "makespan")
        self.model.add_max_equality(
            obj_var, [t["end"] for t in self.tasks.values()]
        )

        priority_weighted_starts = [
            t["priority"] * t["start"] for t in self.tasks.values()
        ]
        obj_expr = weight_makespan * obj_var + sum(priority_weighted_starts)

        # Spread preference: minimize peak daily load when cap is enabled
        # Adaptive weight: high slack -> favor spread; low slack -> makespan dominates
        if self._load_d_vars:
            weight_spread = 5 + 45 * min(1.0, max(0.0, (self.slack_ratio - 2) / 8))
            max_load = self.model.new_int_var(0, self.horizon, "max_load")
            for load_d in self._load_d_vars:
                self.model.add(max_load >= load_d)
            obj_expr += weight_spread * max_load

        self.model.minimize(obj_expr)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = SOLVER_MAX_TIME_SECONDS
        status = solver.solve(self.model)

        if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            schedule = {
                tid: {
                    "start": int(solver.value(t["start"])),
                    "end": int(solver.value(t["end"])),
                }
                for tid, t in self.tasks.items()
            }
            return (schedule, "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE")

        if status == cp_model.UNKNOWN:
            # Distinguishable in the logs even though the return value is not:
            # a plan that keeps hitting this needs a bigger cap, not less scope.
            print(
                f"[Scheduler] CP-SAT returned UNKNOWN after the "
                f"{SOLVER_MAX_TIME_SECONDS}s cap ({len(self.tasks)} tasks, "
                f"horizon {self.horizon} min) — reporting INFEASIBLE"
            )
        return ("INFEASIBLE", "")
