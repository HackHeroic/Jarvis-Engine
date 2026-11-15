"""Sleep windows, precedence, earliest starts, and buffer logic."""

from __future__ import annotations

from typing import Iterable

from ortools.sat.python import cp_model


def merge_time_windows(windows: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Coalesce ``(start, end)`` minute windows into disjoint, ascending windows.

    Blocked time is a *union*: a 09:00-11:00 lecture nested inside a "no work
    before 11am" rule is one blocked stretch, not two things competing for the
    user's single body. Both callers need that union — one puts the windows in a
    CP-SAT ``add_no_overlap`` set (where two overlapping *constant* intervals are
    unsatisfiable on their own), the other sums blocked minutes per day (where
    overlap is double counted).

    Degenerate windows (``end <= start``) block nothing and are dropped. Touching
    windows (``a.end == b.start``) are merged, since the result is contiguous.
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(w for w in windows if w[1] > w[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def add_earliest_starts(model: cp_model.CpModel, tasks: dict[str, dict]) -> None:
    """Hold every task with an ``earliest_start`` from starting before it.

    ``earliest_start`` is a minute from the horizon's start (the task's "not before");
    0 or absent is no constraint. One beyond the latest start the horizon allows
    (``earliest_start + duration > horizon``) leaves the model INFEASIBLE, which the
    caller answers by widening the horizon.
    """
    for task in tasks.values():
        earliest = task.get("earliest_start", 0)
        if earliest > 0:
            model.add(task["start"] >= earliest)


def energy_objective_terms(
    model: cp_model.CpModel,
    tasks: dict[str, dict],
    penalty_by_hour: list[int],
    day_start_hour: int,
    horizon: int,
) -> list[cp_model.LinearExpr]:
    """Objective terms that lean each weighted task toward the user's high-focus hours.

    For every task with ``energy_weight > 0`` the hour it starts in is read off its
    start variable, ``h = start // 60``, and turned into the local hour
    ``(h + day_start_hour) % 24`` (minute 0 is ``day_start_hour`` local). The term is
    ``energy_weight * penalty_by_hour[local]``. ``penalty_by_hour`` holds 24 integers,
    0 at the user's best hours and 10 at their worst. ``horizon`` is the plan's length
    in minutes, the latest any task can start.

    These are soft terms only. They never relax a hard block, a soft block or a
    dependency; they only choose between placements those constraints allow.
    """
    terms: list[cp_model.LinearExpr] = []
    for task_id, task in tasks.items():
        weight = task.get("energy_weight", 0)
        if weight <= 0:
            continue
        start = task["start"]
        hour = model.new_int_var(0, horizon // 60, f"start_hour_{task_id}")
        model.add_division_equality(hour, start, 60)
        local_hour = model.new_int_var(0, 23, f"local_hour_{task_id}")
        model.add_modulo_equality(local_hour, hour + day_start_hour, 24)
        penalty = model.new_int_var(
            min(penalty_by_hour), max(penalty_by_hour), f"focus_penalty_{task_id}"
        )
        model.add_element(local_hour, penalty_by_hour, penalty)
        terms.append(weight * penalty)
    return terms
