#!/usr/bin/env python3
"""Live end-to-end check of the core loop and the vision core against a running backend.

Drives the real server (real Supabase, the real LLM chain) the way the frontend
does, as a throwaway user, and prints a pass/fail/skip table:

    1. brain dump  -> a draft is proposed and nothing is written to user_tasks
    2. accept      -> POST /drafts/{id}/accept writes the tasks
    3. double accept -> the same accept again answers 409 and writes nothing
    4. "looks good" in chat afterwards -> "already locked in", still no duplicates
    5. teach a rule ("never before 11 AM") -> stored as a rule or a memory
    6. re-plan     -> the new draft keeps every task out of 08:00-11:00
    7. new session -> the remembered rule is loaded into the new conversation
    8. forget      -> DELETE /memories/{id} removes it from the Memory panel, and the
                      rule stored with it goes too (a rule and the memory of stating it
                      are linked: letting go of one lets go of both)
    9. usage receipt -> the user's ledger shows cloud calls
  10a. a stated fact -> its memory_extracted frame arrives before the complete frame
  10b. forget sticks -> forgotten, then said again in a new conversation: no frame,
                       not listed, no active row in user_memories
   11. new session  -> complete.since_last_spoke starts "Since we last spoke"
  11a. cached prompt -> the second reply of a conversation read cached prompt tokens
  11b. You page     -> focus is the prior, the rule whose memory step 8 forgot is no
                       longer under rules (a re-plan may use the morning again), and the
                       privacy receipt counts cloud calls over a stated window
   12. morning brief on demand -> a morning_brief suggestion holding a draft
   13. accept the brief -> its tasks are written in its slots; a second accept is 409
   14. task outcomes -> Complete and Skip each write one task event
   15. cleanup      -> every row of the throwaway user is deleted, and checked gone

A step prints PASS, FAIL or SKIP with one line of detail. SKIP means the run could
not say anything on that point (a step it needs did not pass, or the live stack
legitimately had nothing to show), never that something is wrong.

Cost: ten chat turns, roughly 25-30 model calls, plus the brief's re-plan (at most
one habit-translation call). Times are on the user's clock, as the scheduler's
are (minute 0 = 08:00 in the user's zone: their profile's timezone, else
DEFAULT_TIMEZONE). Step 11a tells the second turn's ledger rows by this machine's
clock, so run it against a backend on the same host (the default --base-url).

Usage, from the Jarvis-Engine directory, with the server running:
    .venv/bin/python scripts/live_e2e.py [--base-url http://localhost:8000]

Exit status: 0 when no step failed (skips allowed), 1 otherwise. Credentials come
from .env, loaded the way the app loads them, and are never printed. Rows are
deleted even when a step fails, only for an "e2e-" user; only the server's local
checkpoint file keeps this user's conversation threads.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import app.core.config as cfg  # loads .env the way the app does

PLAN_PROMPT = (
    "I have a data structures exam on Friday. Plan my revision: graphs, dynamic "
    "programming and trees, with some mock problems."
)
RULE_PROMPT = "From now on never schedule any study before 11 AM, I'm useless in the mornings."
RULE_TEXT = re.compile(r"\b11\b|morning", re.IGNORECASE)
FACT_PROMPT = "Something about me: I'm a night owl and do my best work after 9 pm."
PROGRESS_PROMPT = "I finished the graphs revision today"
# However the model words the stated fact when it stores it.
NIGHT_OWL = re.compile(
    r"night[\s-]*owl"
    r"|after\s+(?:9|nine)(?::00)?\s*(?:p\.?\s*m\b|o'?clock\b)"
    r"|after\s+21(?::?00)?\b"
    r"|\b(?:late\s+(?:at\s+)?night|at\s+night)\b",
    re.IGNORECASE,
)
# The calls that write a chat reply, as chat.py names them (_REPLY_TASKS).
REPLY_TASKS = frozenset({"voice_of_jarvis", "voice_of_jarvis_synthesis"})
TURN_TIMEOUT_S = 300.0  # a plan turn is several model calls plus a CP-SAT solve
# How long to wait for work a turn leaves running in the background (memory
# extraction, ledger writes) before calling it missing.
BACKGROUND_DEADLINE_S = 20.0
# How long a memory write that outlived its turn may take to land (the turn waits
# 5 s for it, then lets it finish unannounced): a forgotten fact must stay absent.
ABSENCE_WINDOW_S = 6.0
POLL_INTERVAL_S = 0.5

# Only throwaway users are ever deleted.
E2E_PREFIX = "e2e-"
# Every table holding a user's rows, children before parents. llm_usage_events goes
# first: deleting the profile only nulls its user (ON DELETE SET NULL). user_profiles,
# the root every other table cascades from, goes last.
USER_TABLES = (
    "llm_usage_events",
    "task_events",
    "proactive_suggestions",
    "user_memories",  # tombstones included: a forgotten memory is still a row
    "chat_messages",
    "chat_sessions",  # Task 9's summaries are columns here
    "task_completion_signals",
    "task_workspaces",
    "task_materials",
    "ingested_documents",
    "user_tasks",
    "draft_schedules",
    "user_plan_updates",
    "behavioral_constraints",
    "habit_trackers",
    "pending_calendar_updates",
    "user_preferences",
    "goals",
    "user_profiles",
)
# Tables that may not exist on this database: checked only where they do.
OPTIONAL_USER_TABLES = ("session_summaries",)
# What PostgREST answers for a table it does not have.
_MISSING_TABLE_CODES = frozenset({"PGRST205", "42P01"})


def poll_until(
    probe: Callable[[], Any],
    deadline_s: float = BACKGROUND_DEADLINE_S,
    interval_s: float = POLL_INTERVAL_S,
) -> Any:
    """Call ``probe`` until it returns something truthy or ``deadline_s`` has passed.

    Returns as soon as background work lands, instead of sleeping a fixed time that
    is either wasted or too short. Returns the last value probed, falsy if the
    deadline passed, so the caller's assertion reports what was (not) found.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        value = probe()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval_s)


def linked_memory_id(rule_row: dict) -> str | None:
    """The memory a behavioral_constraints row is linked to (the memory of stating it)."""
    semantics = rule_row.get("structured_semantics")
    linked = semantics.get("memory_id") if isinstance(semantics, dict) else None
    return linked if isinstance(linked, str) and linked else None


def rule_memory(memories: list[dict], rule_rows: list[dict]) -> dict | None:
    """The Memory panel entry that is the taught rule: the memory a rule reading as the
    rule is linked to, else the first memory that reads as the rule; None if neither."""
    linked = {
        linked_memory_id(row) for row in rule_rows if RULE_TEXT.search(row.get("raw_text") or "")
    }
    by_link = [m for m in memories if m["memory_id"] in linked]
    by_text = [m for m in memories if RULE_TEXT.search(m["content"])]
    return (by_link or by_text or [None])[0]


def rules_linked_to(rule_rows: list[dict], memory_id: str) -> list[str]:
    """The ids of the rules linked to ``memory_id``: forgetting it deletes them."""
    return [str(row["id"]) for row in rule_rows if linked_memory_id(row) == memory_id]


def forgotten_rules(forget_body: dict, linked_before: list[str]) -> list[str]:
    """The rule ids ``DELETE /memories/{id}`` says went with the memory: at least one (the
    taught rule was stored linked to it), and exactly the rules linked before."""
    gone = [str(rule_id) for rule_id in forget_body.get("forgotten_rule_ids") or []]
    assert gone, f"no rule went with the rule's memory (linked before: {linked_before})"
    assert sorted(gone) == sorted(linked_before), (
        f"forgot rules {sorted(gone)}, but {sorted(linked_before)} were linked"
    )
    return gone


def matches_fact(text: str | None) -> bool:
    """Whether ``text`` states the night-owl fact of FACT_PROMPT, in any wording."""
    return bool(text and NIGHT_OWL.search(text))


# --- reporting -----------------------------------------------------------------------


class Skip(Exception):
    """A step that has nothing to say on this run raises this with the reason; not a failure."""


def _one_line(text: str) -> str:
    return " ".join(text.split())


@dataclass
class Step:
    name: str
    status: str = "SKIP"  # PASS | FAIL | SKIP
    detail: str = "not run"


class Run:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def check(self, name: str, body: Callable[[], str], *, needs: tuple[str, ...] = ()) -> bool:
        """Run one step and print its line.

        ``body`` returns a detail line, raises AssertionError (FAIL) or ``Skip``
        (SKIP). A step whose ``needs`` did not all pass is a SKIP that names them.
        """
        statuses = {s.name: s.status for s in self.steps}
        step = Step(name)
        self.steps.append(step)
        blocked = [need for need in needs if statuses.get(need) != "PASS"]
        if blocked:
            step.detail = "blocked by: " + ", ".join(
                f"{need} ({statuses.get(need, 'not run')})" for need in blocked
            )
        else:
            try:
                step.detail = body() or ""
                step.status = "PASS"
            except Skip as exc:
                step.status, step.detail = "SKIP", str(exc) or "skipped"
            except AssertionError as exc:
                step.status, step.detail = "FAIL", str(exc) or "assertion failed"
            except Exception as exc:  # a live run reports every failure, it does not stop
                step.status, step.detail = "FAIL", f"{type(exc).__name__}: {exc}"
        step.detail = _one_line(step.detail)
        print(f"  [{step.status}] {name}: {step.detail}", flush=True)
        return step.status == "PASS"

    def table(self) -> str:
        width = max(len(s.name) for s in self.steps)
        lines = [f"{'step'.ljust(width)}  result  detail", f"{'-' * width}  ------  ------"]
        lines += [f"{s.name.ljust(width)}  {s.status.ljust(6)}  {s.detail}" for s in self.steps]
        return "\n".join(lines)

    @property
    def ok(self) -> bool:
        """No step failed; skips are allowed."""
        return not any(s.status == "FAIL" for s in self.steps)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    @property
    def verdict(self) -> str:
        if not self.ok:
            return "FAILED"
        skipped = sum(s.status == "SKIP" for s in self.steps)
        return f"PASSED, {skipped} skipped" if skipped else "ALL PASSED"


# --- talking to the server -------------------------------------------------------------


def parse_sse(lines: list[str]) -> list[tuple[str, Any]]:
    frames, event, data = [], None, []
    for line in [*lines, ""]:
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data.append(line[len("data: "):])
        elif not line and event is not None:
            raw = "\n".join(data)
            try:
                frames.append((event, json.loads(raw)))
            except json.JSONDecodeError:
                frames.append((event, raw))
            event, data = None, []
    return frames


def frame_index(
    frames: list[tuple[str, Any]], event: str, where: Callable[[Any], bool] | None = None
) -> int | None:
    """Position of the first frame named ``event`` (whose data passes ``where``), if any."""
    for index, (name, data) in enumerate(frames):
        if name == event and (where is None or (isinstance(data, dict) and where(data))):
            return index
    return None


def arrives_before_complete(
    frames: list[tuple[str, Any]], event: str, where: Callable[[Any], bool] | None = None
) -> bool:
    """Whether a frame named ``event`` (passing ``where``) arrived before the complete frame."""
    first, done = frame_index(frames, event, where), frame_index(frames, "complete")
    return first is not None and done is not None and first < done


def frame_order(frames: list[tuple[str, Any]]) -> str:
    """The frames' names in arrival order, a run of one name collapsed: "message x40"."""
    runs: list[list] = []
    for name, _ in frames:
        if runs and runs[-1][0] == name:
            runs[-1][1] += 1
        else:
            runs.append([name, 1])
    return " > ".join(name if n == 1 else f"{name} x{n}" for name, n in runs)


def said(response: httpx.Response) -> str:
    """A response's status and what it said, for a failure line (the error envelope if any)."""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"{response.status_code} {error.get('code')}: {error.get('message')}"
    return f"{response.status_code}: {response.text[:200]}"


class Client:
    def __init__(self, base_url: str, user_id: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=TURN_TIMEOUT_S)
        self.user_id = user_id
        self.me = {"user_id": user_id}

    def turn(self, text: str, session: str | None = None) -> tuple[list[tuple[str, Any]], dict]:
        """One chat turn over the SSE stream; returns every frame, in order, and ``complete``."""
        body = {"user_prompt": text, "user_id": self.user_id, "conversation_id": session}
        with self.http.stream("POST", "/api/v1/chat/v2/stream", json=body) as response:
            assert response.status_code == 200, f"chat answered {response.status_code}"
            frames = parse_sse(list(response.iter_lines()))
        errors = [data for event, data in frames if event == "error"]
        assert not errors, f"stream error: {errors}"
        complete = [data for event, data in frames if event == "complete"]
        assert complete, "the stream ended without a complete event"
        return frames, complete[-1]

    def say(self, text: str, session: str | None = None) -> dict:
        """One chat turn; returns the ``complete`` payload."""
        return self.turn(text, session)[1]

    def accept(self, draft_id: str) -> httpx.Response:
        return self.http.post(f"/api/v1/drafts/{draft_id}/accept", json=self.me)

    def memories(self) -> list[dict]:
        response = self.http.get("/api/v1/memories/", params=self.me)
        assert response.status_code == 200, f"memories: {said(response)}"
        return response.json()["memories"]

    def forget(self, memory_id: str) -> httpx.Response:
        return self.http.delete(f"/api/v1/memories/{memory_id}", params=self.me)

    def you(self) -> httpx.Response:
        return self.http.get("/api/v1/you", params=self.me)

    def run_trigger(self, trigger: str) -> httpx.Response:
        return self.http.post("/api/v1/anticipation/run", params=self.me, json={"trigger": trigger})

    def suggestions(self, status: str = "pending") -> list[dict]:
        response = self.http.get(
            "/api/v1/anticipation/suggestions", params={**self.me, "status": status}
        )
        assert response.status_code == 200, f"suggestions: {said(response)}"
        return response.json()["suggestions"]

    def accept_suggestion(self, suggestion_id: str) -> httpx.Response:
        return self.http.post(
            f"/api/v1/anticipation/suggestions/{suggestion_id}/accept", params=self.me
        )

    def complete_task(self, task_id: str) -> httpx.Response:
        return self.http.post(f"/api/v1/tasks/{task_id}/complete", json={**self.me, "quality": 4})

    def skip_task(self, task_id: str) -> httpx.Response:
        return self.http.post(f"/api/v1/tasks/{task_id}/skip", json=self.me)


def rows(db: Any, table: str, user_id: str, columns: str = "*") -> list[dict]:
    return db.table(table).select(columns).eq("user_id", user_id).execute().data or []


def user_zone_of(db: Any, user_id: str) -> Any:
    """The zone the backend plans this user's day in: the same rule, the same profile row."""
    from app.domain.entities import UserProfile
    from app.services.planning.local_time import user_zone

    found = rows(db, "user_profiles", user_id, "user_id, timezone")
    return user_zone(UserProfile.model_validate(found[0]) if found else None)


def slots_in_window(payload: dict, from_hour: int, to_hour: int, zone: Any) -> list[str]:
    """Titles of scheduled tasks that overlap [from_hour, to_hour) in ``zone`` on any day."""
    schedule = payload.get("schedule") or {}
    start = datetime.fromisoformat(schedule["horizon_start"])
    hits = []
    for task in schedule["schedule"].values():
        begin = (start + timedelta(minutes=task["start_min"])).astimezone(zone)
        end = (start + timedelta(minutes=task["end_min"])).astimezone(zone)
        window = begin.replace(hour=from_hour, minute=0, second=0, microsecond=0)
        if begin < window + timedelta(hours=to_hour - from_hour) and end > window:
            hits.append(f"{task.get('title')} @ {begin:%a %H:%M}")
    return hits


def draft_slots(draft: dict) -> dict[str, datetime]:
    """When each slotted task of a stored draft row starts: horizon_start + its start_min.

    The schedule is stored flat (task_id -> slot) or as a whole schedule payload,
    with the map one level down under "schedule". A slot's start is "start_min"
    (planner) or "start" (solver), read as accepting a draft reads it.
    """
    schedule = draft.get("schedule")
    if isinstance(schedule, dict) and isinstance(schedule.get("schedule"), dict):
        schedule = schedule["schedule"]
    origin = datetime.fromisoformat(draft["horizon_start"])
    planned = {}
    for task_id, slot in (schedule or {}).items():
        if not isinstance(slot, dict):
            continue
        start = slot.get("start_min") if slot.get("start_min") is not None else slot.get("start")
        if start is not None:
            planned[task_id] = origin + timedelta(minutes=int(start))
    return planned


def slot_mismatches(planned: dict[str, datetime], tasks: list[dict]) -> list[str]:
    """Planned tasks not written to ``tasks`` (user_tasks rows) at their planned start."""
    written = {task["task_id"]: task for task in tasks}
    wrong = []
    for task_id, start in sorted(planned.items()):
        task = written.get(task_id)
        at = task.get("scheduled_start") if task else None
        if task is None:
            wrong.append(f"{task_id}: not written")
        elif not at or datetime.fromisoformat(at) != start:
            wrong.append(f"{task_id}: {at} != {start.isoformat()}")
    return wrong


def task_events_of(db: Any, user_id: str, task_id: str, event_type: str) -> list[dict]:
    return (
        db.table("task_events").select("id, event_type, occurred_at")
        .eq("user_id", user_id).eq("task_id", task_id).eq("event_type", event_type)
        .execute().data or []
    )


def reply_calls_since(db: Any, user_id: str, session_id: str, since: datetime) -> list[dict]:
    """The ledger's rows for the reply calls of ``session_id`` made since ``since``."""
    found = (
        db.table("llm_usage_events")
        .select("task, provider, model, location, cache_read_tokens, created_at")
        .eq("user_id", user_id).eq("session_id", session_id)
        .gte("created_at", since.isoformat()).order("created_at")
        .execute().data or []
    )
    return [row for row in found if row["task"] in REPLY_TASKS]


# --- the run ----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set in .env; nothing to check against.")
        return 1
    from app.db.supabase_py import build_supabase_client

    db = build_supabase_client(cfg.SUPABASE_URL, cfg.SUPABASE_SERVICE_KEY)
    user_id = f"{E2E_PREFIX}{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(2)}"
    client = Client(args.base_url, user_id)
    run = Run()
    state: dict[str, Any] = {}
    print(f"live e2e against {args.base_url} as {user_id}", flush=True)

    try:
        _core_loop(run, client, db, user_id, state)
        _vision_core(run, client, db, user_id, state)
    finally:
        run.check("15 cleanup", lambda: cleanup(db, user_id))
        client.http.close()

    print()
    print(run.table())
    print()
    print(run.verdict)
    return run.exit_code


def _core_loop(run: Run, client: Client, db: Any, user_id: str, state: dict) -> None:
    def brain_dump() -> str:
        payload = client.say(PLAN_PROMPT)
        state.update(plan=payload, session=payload["conversation_id"])
        assert payload["intent"] == "PLAN_DAY", f"intent was {payload['intent']}: {payload['message']!r}"
        assert payload.get("draft_id"), f"no draft proposed: {payload['message']!r}"
        assert payload.get("schedule"), "no schedule in the reply"
        written = rows(db, "user_tasks", user_id, "task_id")
        assert written == [], f"{len(written)} tasks written before acceptance"
        state["draft_id"] = payload["draft_id"]
        n = len(payload["schedule"]["schedule"])
        return f"draft {payload['draft_id'][:8]} with {n} tasks; user_tasks empty"

    def accept() -> str:
        response = client.accept(state["draft_id"])
        assert response.status_code == 200, said(response)
        written = rows(db, "user_tasks", user_id, "task_id")
        body = response.json()
        assert body.get("status") == "accepted" and body.get("task_count") == len(written) > 0, body
        state["task_ids"] = sorted(r["task_id"] for r in written)
        return f"{len(written)} tasks in user_tasks"

    def double_accept() -> str:
        response = client.accept(state["draft_id"])
        assert response.status_code == 409, f"expected 409, got {response.status_code}"
        now = sorted(r["task_id"] for r in rows(db, "user_tasks", user_id, "task_id"))
        assert now == state["task_ids"], "the second accept changed user_tasks"
        return "409, user_tasks unchanged"

    def looks_good_again() -> str:
        payload = client.say("looks good", session=state["session"])
        assert "already locked in" in payload["message"], repr(payload["message"])
        now = sorted(r["task_id"] for r in rows(db, "user_tasks", user_id, "task_id"))
        assert now == state["task_ids"], "chat accept duplicated tasks"
        return repr(payload["message"])

    def teach_rule() -> str:
        payload = client.say(RULE_PROMPT)

        def stored() -> tuple[list[str], list[str]] | None:
            # Memory extraction finishes in the background, after the reply.
            habits = [r["raw_text"] for r in rows(db, "behavioral_constraints", user_id, "raw_text")]
            memories = [m["content"] for m in client.memories() if RULE_TEXT.search(m["content"])]
            return (habits, memories) if habits or memories else None

        found = poll_until(stored)
        assert found, (
            f"rule not stored within {BACKGROUND_DEADLINE_S:.0f} s; reply was {payload['message']!r}"
        )
        habits, memories = found
        return f"habits={habits} memories={memories}"

    def replan() -> str:
        zone = user_zone_of(db, user_id)
        before = slots_in_window(state["plan"], 8, 11, zone)
        payload = client.say(PLAN_PROMPT)  # a new conversation
        assert payload.get("schedule"), f"no schedule: {payload['message']!r}"
        after = slots_in_window(payload, 8, 11, zone)
        assert not after, f"still in 08:00-11:00 {zone.key}: {after}"
        return f"08:00-11:00 {zone.key} tasks: {len(before)} before, 0 after"

    def _rule_rows() -> list[dict]:
        return rows(db, "behavioral_constraints", user_id, "id,raw_text,structured_semantics")

    def recall() -> str:
        payload = client.say("Good morning")  # a new conversation: one model call
        loaded = [m["content"] for m in payload.get("memories") or [] if RULE_TEXT.search(m["content"])]
        assert loaded, "the rule was not loaded into the new conversation"
        state["rule_memory"] = rule_memory(client.memories(), _rule_rows())
        assert state["rule_memory"], "the rule is not in the Memory panel"
        return f"loaded {loaded}; reply {payload['message'][:60]!r}"

    def forget() -> str:
        memory = state["rule_memory"]
        linked = rules_linked_to(_rule_rows(), memory["memory_id"])
        response = client.forget(memory["memory_id"])
        assert response.status_code == 200, said(response)
        state["forgotten_rule_ids"] = forgotten_rules(response.json(), linked)
        assert memory["memory_id"] not in [m["memory_id"] for m in client.memories()], "still in the panel"
        left = [r["id"] for r in _rule_rows() if str(r["id"]) in state["forgotten_rule_ids"]]
        assert not left, f"the rule stored with the forgotten memory is still a rule: {left}"
        return f"forgot {memory['content']!r} and rule(s) {state['forgotten_rule_ids']} with it"

    def usage() -> str:
        last: dict = {}

        def recorded() -> dict | None:
            # The ledger writes in the background, after each call.
            last.update(asyncio.run(_receipt(db, user_id)))
            return dict(last) if last["cloud_calls"] > 0 else None

        receipt = poll_until(recorded)
        assert receipt, f"no cloud calls recorded within {BACKGROUND_DEADLINE_S:.0f} s: {last}"
        return json.dumps(receipt)

    run.check("1 brain dump -> draft", brain_dump)
    run.check("2 accept", accept, needs=("1 brain dump -> draft",))
    run.check("3 double accept -> 409", double_accept, needs=("2 accept",))
    run.check("4 'looks good' -> already locked in", looks_good_again, needs=("2 accept",))
    run.check("5 teach rule", teach_rule)
    run.check("6 re-plan respects the rule", replan, needs=("1 brain dump -> draft", "5 teach rule"))
    run.check("7 memory recalled in new session", recall, needs=("5 teach rule",))
    run.check("8 forget", forget, needs=("7 memory recalled in new session",))
    run.check("9 usage receipt shows cloud calls", usage)


def _vision_core(run: Run, client: Client, db: Any, user_id: str, state: dict) -> None:
    def fact_frame(data: dict) -> bool:
        return matches_fact(data.get("content"))

    def fact_memory() -> dict | None:
        return next((m for m in client.memories() if matches_fact(m["content"])), None)

    def learning_moment() -> str:
        frames, _ = client.turn(FACT_PROMPT)  # a new conversation
        state["fact_said"] = True
        order = frame_order(frames)
        assert arrives_before_complete(frames, "memory_extracted", where=fact_frame), (
            f"no night-owl memory_extracted frame before complete: {order}"
        )
        frame = frames[frame_index(frames, "memory_extracted", where=fact_frame)][1]
        return f"{frame.get('action')} {frame.get('content')!r} before complete: {order}"

    def forget_sticks() -> str:
        if not state.get("fact_said"):
            raise Skip("blocked: the fact turn of 10a did not complete")
        memory = poll_until(fact_memory)
        assert memory, f"the night-owl fact was not stored within {BACKGROUND_DEADLINE_S:.0f} s"
        response = client.forget(memory["memory_id"])
        assert response.status_code == 200, said(response)

        frames, _ = client.turn(FACT_PROMPT)  # said again, in another new conversation
        told = [d.get("content") for e, d in frames
                if e == "memory_extracted" and isinstance(d, dict) and fact_frame(d)]
        assert not told, f"a learning frame for the forgotten fact: {told}"
        listed = [m["content"] for m in client.memories() if matches_fact(m["content"])]
        assert not listed, f"the forgotten fact is listed again: {listed}"

        def active_rows() -> list[dict]:
            return [r for r in rows(db, "user_memories", user_id, "content, status")
                    if r["status"] == "active" and matches_fact(r.get("content"))]

        # A write that outlived the turn could still land: the fact must stay absent.
        stored = poll_until(active_rows, deadline_s=ABSENCE_WINDOW_S)
        assert not stored, f"an active row holds the forgotten fact: {stored}"
        tombstones = sum(r["status"] == "forgotten"
                         for r in rows(db, "user_memories", user_id, "status"))
        return (
            f"forgot {memory['content']!r}; said again: no frame, not listed, no active row"
            f" ({tombstones} tombstone(s))"
        )

    def since_last_spoke() -> str:
        first = client.say(PROGRESS_PROMPT)  # a new conversation
        session = first["conversation_id"]
        started = datetime.now(timezone.utc)
        client.say("thanks", session=session)
        state.update(cache_session=session, cache_turn_started=started)

        payload = client.say("Hi again")  # another new conversation
        if "since_last_spoke" not in payload:
            raise Skip("complete has no since_last_spoke field: Task 9 is not deployed")
        line = payload["since_last_spoke"]
        assert line, "since_last_spoke is null in a new conversation after an earlier one"
        assert line.startswith("Since we last spoke"), repr(line)
        return repr(line)

    def cached_prompt() -> str:
        session, started = state.get("cache_session"), state.get("cache_turn_started")
        if session is None:
            raise Skip("cache-read tokens: n/a (the two turns of step 11 did not both complete)")
        calls: list[dict] = []

        def landed() -> bool:
            calls[:] = reply_calls_since(db, user_id, session, started)
            return any(c["provider"] == "claude" for c in calls)

        try:
            poll_until(landed)
        except Exception as exc:
            if getattr(exc, "code", None) == "42703":  # the ledger has no such column
                raise Skip("cache-read tokens: n/a (the usage ledger does not record them)") from exc
            raise
        assert calls, (
            f"no reply call of the second turn reached the ledger within {BACKGROUND_DEADLINE_S:.0f} s"
        )
        claude = [c for c in calls if c["provider"] == "claude"]
        if not claude:
            served = ", ".join(sorted({f"{c['provider']} ({c['location']})" for c in calls}))
            raise Skip(f"cache-read tokens: n/a (the second reply ran on {served}; "
                       "only Claude reports them)")
        best = max(claude, key=lambda c: c["cache_read_tokens"])
        assert best["cache_read_tokens"] > 0, (
            f"cache_read_tokens=0 on the second reply ({best['model']}, {len(claude)} call(s))"
        )
        return f"cache_read_tokens={best['cache_read_tokens']} on the second reply ({best['model']})"

    def you_page() -> str:
        view: dict = {}

        def read() -> bool:
            response = client.you()
            assert response.status_code == 200, f"GET /you: {said(response)}"
            view.update(response.json())
            return view["receipt"]["cloud_calls"] > 0

        poll_until(read)
        focus, rules, receipt = view["focus"], view["rules"], view["receipt"]
        assert focus["source"] == "prior", (
            f"focus is {focus['source']} from {focus['observations']} outcomes; "
            "a new user has the prior"
        )
        # Step 8 forgot the rule's memory, which took the rule with it (step 8 checked at
        # least one went): plans stop applying it, and the page stops listing that id.
        gone = state.get("forgotten_rule_ids") or []
        if gone:
            still = [r["text"] for r in rules if str(r["id"]) in gone]
            assert not still, f"step 8 forgot rules {gone}, yet they are under rules: {still}"
        assert receipt["cloud_calls"] > 0, f"no cloud calls in the receipt: {receipt}"
        now = datetime.now(timezone.utc)
        since = datetime.fromisoformat(receipt["since"])
        assert since < now, f"the receipt's window opens in the future: {receipt['since']}"
        days = round((now - since).total_seconds() / 86400)
        return (
            f"focus prior ({focus['observations']} outcomes); {len(gone)} forgotten rule(s)"
            f" not listed; receipt {receipt['cloud_calls']} cloud /"
            f" {receipt['on_device_calls']} on-device calls over {days} days"
            f" since {since:%Y-%m-%d}"
        )

    def morning_brief() -> str:
        response = client.run_trigger("morning_brief")
        assert response.status_code < 500, f"POST /anticipation/run: {said(response)}"
        assert response.status_code == 200, f"POST /anticipation/run: {said(response)}"
        body = response.json()
        status = body["status"]
        if status in ("nothing_to_say", "denied"):
            why = (body.get("consent") or {}).get("message") or "no pending or set-aside work"
            raise Skip(f"status {status}: {why}")
        assert status in ("created", "already_done"), f"status {status}: {body}"
        suggestion, found = body.get("suggestion"), ""
        if suggestion is None and status == "already_done":
            # Before the brief fix, already_done came without the day's suggestion.
            suggestion = next(
                (s for s in client.suggestions() if s["trigger"] == "morning_brief"), None
            )
            found = " (found in the pending list)"
        assert suggestion, f"{status} without a suggestion"
        assert suggestion["kind"] == "morning_brief", f"kind {suggestion['kind']}"
        draft_id = (suggestion.get("details") or {}).get("draft_id")
        assert draft_id, (
            f"no draft in the brief: {suggestion['title']!r}: {suggestion['body']!r}"
        )
        state["brief"] = suggestion
        return (
            f"{status}{found}: {suggestion['title']!r}, {suggestion['status']}, draft"
            f" {draft_id[:8]}: {suggestion['body']!r}"
        )

    def accept_brief() -> str:
        suggestion = state["brief"]
        if suggestion["status"] != "pending":
            raise Skip(f"the day's brief is already {suggestion['status']}: nothing to accept")
        draft_id = suggestion["details"]["draft_id"]
        response = client.accept_suggestion(suggestion["id"])
        assert response.status_code == 200, f"accept: {said(response)}"
        result = response.json()["result"]
        assert (result.get("tasks_written") or 0) > 0, f"no tasks written: {result}"

        draft = (
            db.table("draft_schedules").select("id, status, schedule, horizon_start")
            .eq("user_id", user_id).eq("id", draft_id).execute().data or []
        )
        assert draft, f"the brief's draft {draft_id[:8]} is not stored"
        assert draft[0]["status"] == "accepted", f"the brief's draft is {draft[0]['status']}"
        assert draft[0].get("horizon_start"), "the brief's draft has no horizon_start"
        planned = draft_slots(draft[0])
        assert planned, "the brief's draft has no slots"
        tasks = rows(db, "user_tasks", user_id, "task_id, scheduled_start")
        wrong = slot_mismatches(planned, tasks)
        assert not wrong, f"user_tasks does not carry the brief's plan: {wrong}"

        again = client.accept_suggestion(suggestion["id"])
        assert again.status_code == 409, f"second accept: expected 409, got {said(again)}"
        return (
            f"{result['tasks_written']} tasks written, {len(planned)} in the brief's slots;"
            f" second accept 409: {result['message']!r}"
        )

    def task_outcomes() -> str:
        pending = sorted(
            (r for r in rows(db, "user_tasks", user_id, "task_id, status, scheduled_start")
             if r["status"] == "pending"),
            key=lambda r: (r.get("scheduled_start") or "~", r["task_id"]),
        )
        assert len(pending) >= 2, f"{len(pending)} pending task(s); two are needed"
        done_id, skip_id = pending[0]["task_id"], pending[1]["task_id"]

        done = client.complete_task(done_id)
        assert done.status_code == 200, f"complete: {said(done)}"
        skipped = client.skip_task(skip_id)
        assert skipped.status_code == 200, f"skip: {said(skipped)}"
        assert skipped.json()["status"] == "skipped", f"skip: {skipped.json()}"

        completed_events = task_events_of(db, user_id, done_id, "completed")
        assert len(completed_events) == 1, f"{len(completed_events)} 'completed' events for {done_id}"
        skipped_events = task_events_of(db, user_id, skip_id, "skipped")
        assert len(skipped_events) == 1, f"{len(skipped_events)} 'skipped' events for {skip_id}"
        status = {r["task_id"]: r["status"] for r in rows(db, "user_tasks", user_id, "task_id, status")}
        assert status.get(skip_id) == "skipped", f"user_tasks.status of {skip_id} is {status.get(skip_id)}"
        assert status.get(done_id) == "completed", f"user_tasks.status of {done_id} is {status.get(done_id)}"
        return f"{done_id}: one 'completed' event; {skip_id}: one 'skipped' event, status skipped"

    run.check("10a learning moment before complete", learning_moment)
    run.check("10b forget sticks across re-extraction", forget_sticks)
    run.check("11 new session knows what happened", since_last_spoke)
    run.check("11a cached prompt on the second reply", cached_prompt)
    run.check("11b You page", you_page, needs=("5 teach rule",))
    run.check("12 morning brief on demand", morning_brief, needs=("2 accept",))
    run.check("13 accept the brief -> tasks written", accept_brief,
              needs=("12 morning brief on demand",))
    run.check("14 task outcome -> event", task_outcomes, needs=("2 accept",))


async def _receipt(db: Any, user_id: str) -> dict:
    from app.infrastructure.persistence.supabase_repos import SupabaseUsageRepository
    from app.services.usage_ledger import UsageLedger

    return await UsageLedger(SupabaseUsageRepository(db)).receipt(user_id)


def _missing_table(exc: Exception) -> bool:
    return getattr(exc, "code", None) in _MISSING_TABLE_CODES


def _delete_user_rows(db: Any, user_id: str) -> list[str]:
    """Delete the user's rows from every table; return the tables that exist."""
    present = []
    for table in (*OPTIONAL_USER_TABLES, *USER_TABLES):
        try:
            db.table(table).delete().eq("user_id", user_id).execute()
        except Exception as exc:
            if table in OPTIONAL_USER_TABLES and _missing_table(exc):
                continue
            raise
        present.append(table)
    return present


def _rows_left(db: Any, user_id: str, tables: list[str]) -> dict[str, int]:
    counts = {table: len(rows(db, table, user_id, "user_id")) for table in tables}
    return {table: n for table, n in counts.items() if n}


def cleanup(db: Any, user_id: str) -> str:
    """Delete every row of the throwaway ``user_id``, then check each table holds none.

    Refuses, before touching anything, any id without the "e2e-" prefix. Work the
    backend still has running (a ledger write, a scheduler tick) may write a row
    during the first pass, so one more pass is made if anything is left; the detail
    line says what it removed.
    """
    if not user_id.startswith(E2E_PREFIX):
        raise ValueError(
            f"refusing to delete the rows of {user_id!r}: cleanup only removes "
            f"throwaway '{E2E_PREFIX}' users"
        )
    tables = _delete_user_rows(db, user_id)
    left = _rows_left(db, user_id, tables)
    if not left:
        return f"all rows deleted; {len(tables)} tables checked empty"
    _delete_user_rows(db, user_id)
    still = _rows_left(db, user_id, tables)
    assert not still, f"rows left behind: {still}"
    return (
        f"all rows deleted; {len(tables)} tables checked empty (a second pass removed"
        f" rows written during cleanup: {left})"
    )


if __name__ == "__main__":
    sys.exit(main())
