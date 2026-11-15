#!/usr/bin/env python3
"""Live check of the memory loop against a running backend: said, found, corrected, forgotten.

Drives the real server (real Supabase, the real LLM chain) the way the frontend does, as
a throwaway user (``memcheck_<8 hex>``, never ``demo``), and prints one PASS/FAIL line
per step:

    1. two facts      -> one message states a weekday gym rule and a Thursday quiz fact:
                         the You page lists both memories and one gym rule
    2. two-part ask   -> a new conversation asks about both at once: both memories were
                         recalled (complete.memories, the loading_context frame's count)
    3. correction     -> "I moved my gym to 7 AM": one gym rule remains, it reads 7 AM,
                         and the 6 PM memory is no longer listed
    4. ask again      -> a new conversation: the 7 AM memory and the quiz fact recalled,
                         the 6 PM memory not
    5. forget         -> DELETE /memories/{id} on the quiz fact: no longer listed
    6. ask again      -> a new conversation: the quiz fact not recalled
    7. cleanup        -> every row of the throwaway user is deleted, and checked gone

Each step reads stored state (``GET /api/v1/you``) and what the turn recalled (the
``complete`` frame's memories and the ``loading_context`` frame's memory count), never
the wording of a reply. A step whose earlier step failed fails too, saying so.

Cost: five chat turns, roughly ten to fifteen model calls.

Usage, from the Jarvis-Engine directory, with the server running:
    .venv/bin/python scripts/memory_check.py [--base-url http://localhost:8000]

Exit status: 0 when every step passed, 1 otherwise. Credentials come from .env, loaded the
way the app loads them, and are never printed. Rows are deleted even when a step fails,
and only for a ``memcheck_`` user.
"""

import argparse
import re
import secrets
import sys
from typing import Any

from live_e2e import (  # scripts/ is on the path when this file is run
    Client,
    Run,
    _delete_user_rows,
    _rows_left,
    cfg,
    frame_index,
    said,
)

MEMCHECK_PREFIX = "memcheck_"
TWO_FACTS = (
    "I go to the gym every weekday at 6 PM, and my Computer Networks professor Dr. Rao "
    "gives surprise quizzes on Thursdays."
)
TWO_PART_QUESTION = "When is my gym, and what should I know about Thursdays?"
CORRECTION = "Actually I moved my gym to 7 AM on weekdays, not 6 PM anymore."
# However the model words each memory when it stores it.
GYM = re.compile(r"\bgym\b", re.IGNORECASE)
SIX_PM = re.compile(r"\b(6|six)(:00)?\s*(p\.?\s*m\b|in the evening)|\b18:00\b", re.IGNORECASE)
SEVEN_AM = re.compile(r"\b(7|seven)(:00)?\s*(a\.?\s*m\b|in the morning)|\b07:00\b", re.IGNORECASE)
QUIZ = re.compile(r"\bRao\b|\bquiz", re.IGNORECASE)


class Check:
    """The run's client and what each step found, for the steps after it."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.ids: dict[str, str] = {}

    def you(self) -> dict:
        response = self.client.you()
        assert response.status_code == 200, f"/you: {said(response)}"
        return response.json()

    def ask(self, text: str) -> tuple[set[str], int]:
        """Ask in a new conversation; return the ids of the memories the turn recalled and
        the loading_context frame's memory count."""
        frames, complete = self.client.turn(text)
        recalled = {m["memory_id"] for m in complete.get("memories") or []}
        at = frame_index(
            frames, "phase", lambda data: data.get("phase") == "loading_context"
        )
        detail = (frames[at][1].get("detail") or {}) if at is not None else {}
        return recalled, int(detail.get("memories_count", -1))


def _one(memories: list[dict], *patterns: re.Pattern) -> dict:
    found = [m for m in memories if all(p.search(m["content"]) for p in patterns)]
    assert len(found) == 1, f"expected one memory matching {[p.pattern for p in patterns]}: " + (
        "; ".join(m["content"] for m in memories) or "none listed"
    )
    return found[0]


def _gym_rules(you: dict) -> list[str]:
    return [rule["text"] for rule in you["rules"] if GYM.search(rule["text"])]


def two_facts(check: Check) -> str:
    check.client.say(TWO_FACTS)
    you = check.you()
    check.ids["gym_6pm"] = _one(you["memories"], GYM, SIX_PM)["memory_id"]
    check.ids["quiz"] = _one(you["memories"], QUIZ)["memory_id"]
    rules = _gym_rules(you)
    assert len(rules) == 1, f"expected one gym rule, found {rules}"
    return f"two memories and one gym rule ({rules[0]!r})"


def two_part_ask(check: Check) -> str:
    recalled, count = check.ask(TWO_PART_QUESTION)
    missing = [name for name in ("gym_6pm", "quiz") if check.ids[name] not in recalled]
    assert not missing, f"not recalled: {missing} (loading_context count {count})"
    assert count >= 2, f"loading_context counted {count} memories"
    return f"both recalled; loading_context counted {count}"


def correction(check: Check) -> str:
    check.client.say(CORRECTION)
    you = check.you()
    rules = _gym_rules(you)
    assert len(rules) == 1, f"expected one gym rule, found {rules}"
    assert SEVEN_AM.search(rules[0]), f"the rule reads {rules[0]!r}"
    listed = {m["memory_id"] for m in you["memories"]}
    assert check.ids["gym_6pm"] not in listed, "the 6 PM memory is still listed"
    check.ids["gym_7am"] = _one(you["memories"], GYM, SEVEN_AM)["memory_id"]
    return f"one gym rule ({rules[0]!r}); the 6 PM memory replaced"


def ask_after_correction(check: Check) -> str:
    recalled, count = check.ask(TWO_PART_QUESTION)
    assert check.ids["gym_6pm"] not in recalled, "the 6 PM memory was recalled"
    missing = [name for name in ("gym_7am", "quiz") if check.ids[name] not in recalled]
    assert not missing, f"not recalled: {missing} (loading_context count {count})"
    return f"7 AM and the quiz fact recalled, 6 PM not; loading_context counted {count}"


def forget(check: Check) -> str:
    response = check.client.forget(check.ids["quiz"])
    assert response.status_code == 200, f"forget: {said(response)}"
    listed = {m["memory_id"] for m in check.you()["memories"]}
    assert check.ids["quiz"] not in listed, "the forgotten memory is still listed"
    return "the quiz fact is no longer listed"


def ask_after_forget(check: Check) -> str:
    recalled, count = check.ask(TWO_PART_QUESTION)
    assert check.ids["quiz"] not in recalled, "the forgotten memory was recalled"
    return f"the quiz fact not recalled; loading_context counted {count}"


def cleanup(db: Any, user_id: str) -> str:
    """Delete every row of the throwaway ``user_id``, then check none is left. Refuses,
    before touching anything, any id without the ``memcheck_`` prefix."""
    if not user_id.startswith(MEMCHECK_PREFIX):
        raise ValueError(f"refusing to delete the rows of {user_id!r}: not a memcheck user")
    tables = _delete_user_rows(db, user_id)
    if _rows_left(db, user_id, tables):  # written while the first pass ran
        _delete_user_rows(db, user_id)
    left = _rows_left(db, user_id, tables)
    assert not left, f"rows left behind: {left}"
    return f"all rows deleted; {len(tables)} tables checked empty"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set in .env; nothing to check against.")
        return 1
    from app.db.supabase_py import build_supabase_client

    db = build_supabase_client(cfg.SUPABASE_URL, cfg.SUPABASE_SERVICE_KEY)
    user_id = f"{MEMCHECK_PREFIX}{secrets.token_hex(4)}"
    check = Check(Client(args.base_url, user_id))
    run = Run()
    print(f"memory check against {args.base_url} as {user_id}", flush=True)

    # (name, body, the steps it needs): a step reads the ids an earlier step found.
    steps = (
        ("1 two facts", two_facts, ()),
        ("2 two-part ask", two_part_ask, ("1 two facts",)),
        ("3 correction", correction, ("1 two facts",)),
        ("4 ask again", ask_after_correction, ("3 correction",)),
        ("5 forget", forget, ("1 two facts",)),
        ("6 ask again", ask_after_forget, ("5 forget",)),
    )
    try:
        for name, body, needs in steps:
            run.check(name, lambda body=body: body(check), needs=needs)
    finally:
        run.check("7 cleanup", lambda: cleanup(db, user_id))
        check.client.http.close()

    print()
    print(run.table())
    print()
    # A step blocked by an earlier failure is a failure of the loop, not a skip.
    failed = [s.name for s in run.steps if s.status != "PASS"]
    print("FAILED: " + ", ".join(failed) if failed else "ALL PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
