#!/usr/bin/env python3
"""Live end-to-end check of the core loop against a running backend.

Drives the real server (real Supabase, the real LLM chain) the way the frontend
does, as a throwaway user, and prints a pass/fail table:

    1. brain dump  -> a draft is proposed and nothing is written to user_tasks
    2. accept      -> POST /drafts/{id}/accept writes the tasks
    3. double accept -> the same accept again answers 409 and writes nothing
    4. "looks good" in chat afterwards -> "already locked in", still no duplicates
    5. teach a rule ("never before 11 AM") -> stored as a rule or a memory
    6. re-plan     -> the new draft keeps every task out of 08:00-11:00
    7. new session -> the remembered rule is loaded into the new conversation
    8. forget      -> DELETE /memories/{id} removes it from the Memory panel
    9. usage receipt -> the user's ledger shows cloud calls
   10. cleanup     -> every row of the throwaway user is deleted

Cost: five chat turns, roughly 12-14 model calls (about half through the metered
chain - brain-dump extraction, decomposition, the chat reply - and half through
modules still on the old transport: reply synthesis, memory extraction, habit
translation). Times are UTC, as the scheduler's are (minute 0 = 08:00 UTC).

Usage, from the Jarvis-Engine directory, with the server running:
    .venv/bin/python scripts/live_e2e.py [--base-url http://localhost:8000]

Exit status: 0 when every step passed, 1 otherwise. Credentials come from .env,
loaded the way the app loads them, and are never printed. Rows are deleted even
when a step fails; only the server's local checkpoint file keeps this user's
conversation threads.
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
TURN_TIMEOUT_S = 300.0  # a plan turn is several model calls plus a CP-SAT solve
# How long to wait for work a turn leaves running in the background (memory
# extraction, ledger writes) before calling it missing.
BACKGROUND_DEADLINE_S = 20.0
POLL_INTERVAL_S = 0.5


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


# --- reporting -----------------------------------------------------------------------


@dataclass
class Step:
    name: str
    status: str = "SKIP"  # PASS | FAIL | SKIP
    detail: str = "not run"


class Run:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def check(self, name: str, body: Callable[[], str], *, needs: tuple[str, ...] = ()) -> bool:
        """Run one step; ``body`` returns a detail line or raises AssertionError."""
        step = Step(name)
        self.steps.append(step)
        passed = {s.name for s in self.steps if s.status == "PASS"}
        blocked = [name for name in needs if name not in passed]
        if blocked:
            step.detail = f"blocked by: {', '.join(blocked)}"
            return False
        try:
            step.detail = body() or ""
            step.status = "PASS"
        except AssertionError as exc:
            step.status, step.detail = "FAIL", str(exc) or "assertion failed"
        except Exception as exc:  # a live run reports every failure, it does not stop
            step.status, step.detail = "FAIL", f"{type(exc).__name__}: {exc}"
        print(f"  [{step.status}] {name}: {step.detail}", flush=True)
        return step.status == "PASS"

    def table(self) -> str:
        width = max(len(s.name) for s in self.steps)
        lines = [f"{'step'.ljust(width)}  result  detail", f"{'-' * width}  ------  ------"]
        lines += [f"{s.name.ljust(width)}  {s.status.ljust(6)}  {s.detail}" for s in self.steps]
        return "\n".join(lines)

    @property
    def ok(self) -> bool:
        return all(s.status == "PASS" for s in self.steps)


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


class Client:
    def __init__(self, base_url: str, user_id: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=TURN_TIMEOUT_S)
        self.user_id = user_id

    def say(self, text: str, session: str | None = None) -> dict:
        """One chat turn over the SSE stream; returns the ``complete`` payload."""
        body = {"user_prompt": text, "user_id": self.user_id, "conversation_id": session}
        with self.http.stream("POST", "/api/v1/chat/v2/stream", json=body) as response:
            assert response.status_code == 200, f"chat answered {response.status_code}"
            frames = parse_sse(list(response.iter_lines()))
        errors = [data for event, data in frames if event == "error"]
        assert not errors, f"stream error: {errors}"
        complete = [data for event, data in frames if event == "complete"]
        assert complete, "the stream ended without a complete event"
        return complete[-1]

    def accept(self, draft_id: str) -> httpx.Response:
        return self.http.post(f"/api/v1/drafts/{draft_id}/accept", json={"user_id": self.user_id})

    def memories(self) -> list[dict]:
        response = self.http.get("/api/v1/memories/", params={"user_id": self.user_id})
        assert response.status_code == 200, f"memories answered {response.status_code}"
        return response.json()["memories"]

    def forget(self, memory_id: str) -> httpx.Response:
        return self.http.delete(f"/api/v1/memories/{memory_id}", params={"user_id": self.user_id})


def rows(db: Any, table: str, user_id: str, columns: str = "*") -> list[dict]:
    return db.table(table).select(columns).eq("user_id", user_id).execute().data or []


def slots_in_window(payload: dict, from_hour: int, to_hour: int) -> list[str]:
    """Titles of scheduled tasks that overlap [from_hour, to_hour) UTC on any day."""
    schedule = payload.get("schedule") or {}
    start = datetime.fromisoformat(schedule["horizon_start"])
    hits = []
    for task in schedule["schedule"].values():
        begin = start + timedelta(minutes=task["start_min"])
        end = start + timedelta(minutes=task["end_min"])
        window = begin.replace(hour=from_hour, minute=0, second=0, microsecond=0)
        if begin < window + timedelta(hours=to_hour - from_hour) and end > window:
            hits.append(f"{task.get('title')} @ {begin:%a %H:%M}")
    return hits


# --- the run ----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set in .env; nothing to check against.")
        return 1
    from supabase import create_client

    db = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_SERVICE_KEY)
    user_id = f"e2e-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(2)}"
    client = Client(args.base_url, user_id)
    run = Run()
    state: dict[str, Any] = {}
    print(f"live e2e against {args.base_url} as {user_id}", flush=True)

    try:
        _core_loop(run, client, db, user_id, state)
    finally:
        run.check("10 cleanup", lambda: _cleanup(db, user_id))
        client.http.close()

    print()
    print(run.table())
    print()
    print("ALL PASSED" if run.ok else "FAILED")
    return 0 if run.ok else 1


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
        assert response.status_code == 200, f"{response.status_code}: {response.text[:200]}"
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
        before = slots_in_window(state["plan"], 8, 11)
        payload = client.say(PLAN_PROMPT)  # a new conversation
        assert payload.get("schedule"), f"no schedule: {payload['message']!r}"
        after = slots_in_window(payload, 8, 11)
        assert not after, f"still in 08:00-11:00 UTC: {after}"
        return f"08:00-11:00 tasks: {len(before)} before, 0 after"

    def recall() -> str:
        payload = client.say("Good morning")  # a new conversation: one model call
        loaded = [m["content"] for m in payload.get("memories") or [] if RULE_TEXT.search(m["content"])]
        assert loaded, "the rule was not loaded into the new conversation"
        state["rule_memory"] = next(m for m in client.memories() if RULE_TEXT.search(m["content"]))
        return f"loaded {loaded}; reply {payload['message'][:60]!r}"

    def forget() -> str:
        memory = state["rule_memory"]
        response = client.forget(memory["id"])
        assert response.status_code == 200, f"{response.status_code}: {response.text[:200]}"
        assert memory["id"] not in [m["id"] for m in client.memories()], "still in the panel"
        return f"forgot {memory['content']!r}"

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


async def _receipt(db: Any, user_id: str) -> dict:
    from app.infrastructure.persistence.supabase_repos import SupabaseUsageRepository
    from app.services.usage_ledger import UsageLedger

    return await UsageLedger(SupabaseUsageRepository(db)).receipt(user_id)


def _cleanup(db: Any, user_id: str) -> str:
    # Usage rows only lose their user on profile deletion (ON DELETE SET NULL): go first.
    db.table("llm_usage_events").delete().eq("user_id", user_id).execute()
    db.table("user_profiles").delete().eq("user_id", user_id).execute()  # cascades
    left = {
        table: len(rows(db, table, user_id, "user_id"))
        for table in ("user_tasks", "draft_schedules", "user_memories", "chat_sessions",
                      "chat_messages", "behavioral_constraints", "llm_usage_events")
    }
    assert not any(left.values()), f"rows left behind: {left}"
    return "all rows deleted"


if __name__ == "__main__":
    sys.exit(main())
