"""The You page API: one read of what Jarvis knows about the user, and their settings.

The composition root runs on the Supabase repositories over ``FakeSupabase``, with
the bag-of-words embedder. Every error is the one envelope, and an outage is a 503,
never an empty page.
"""

from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.domain.entities import (
    BehavioralConstraint,
    LLMLocation,
    LLMUsageEvent,
    MemorySource,
    TaskEvent,
    TaskEventType,
)
from app.services.memory.memory_service import MemoryCandidate
from tests.behaviour.conftest import blame_phrases_in
from tests.fakes import BagOfWordsEmbedder, FakeDBClient, FakeSupabase

SECRET = "postgres://admin:hunter2@db.internal"
MARATHON = "Training for the Mumbai marathon in March"

_TOP_LEVEL = {"profile", "patterns", "rules", "memories", "focus", "receipt", "streak_days"}
_PROFILE = {"display_name", "timezone", "wake_time", "autonomy_mode"}
_PATTERN = {
    "memory_id", "pattern_key", "detector", "description", "confidence", "occurrences",
    "evidence", "confirmed",
}
_RULE = {"id", "text", "provenance", "created_at"}
_MEMORY = {
    "memory_id", "memory_type", "content", "confidence", "provenance", "freshness",
    "noted_at", "last_confirmed_at", "observation_count", "pattern_key",
}
_FOCUS = {"source", "observations", "hours"}
_HOUR = {"hour", "score", "done", "not_done"}
_RECEIPT = {
    "since", "truncated", "on_device_calls", "cloud_calls", "pii_redactions", "input_tokens",
    "output_tokens",
}


def _container(supabase):
    import app.core.config as cfg
    from app.container import AppContainer
    from app.orchestrator.hooks import ActionHooks

    return AppContainer.build(
        supabase_client=supabase,
        cfg=cfg,
        hooks=ActionHooks(),
        embedder=BagOfWordsEmbedder(),
        install_chain=False,
    )


@asynccontextmanager
async def _api(monkeypatch, container, supabase):
    """The app over ``container``, its database client over ``supabase`` (None: no database)."""
    from app.main import app

    db_client = FakeDBClient(supabase) if supabase is not None else SimpleNamespace(supabase=None)
    monkeypatch.setattr(app.state, "container", container, raising=False)
    monkeypatch.setattr(app.state, "db_client", db_client, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _skips_at_nine(container, user_id: str = "alice") -> None:
    """Three slots at 09:00 IST set aside on the last three days: a learnable habit."""
    now = datetime.now(timezone.utc)
    for days_ago in (3, 2, 1):
        day = (now - timedelta(days=days_ago)).date()
        slot = datetime(day.year, day.month, day.day, 3, 30, tzinfo=timezone.utc)  # 09:00 IST
        await container.repos.task_events.append(user_id, TaskEvent(
            user_id=user_id,
            task_id=f"t{days_ago}",
            event_type=TaskEventType.SKIPPED if days_ago != 1 else TaskEventType.MISSED,
            occurred_at=slot + timedelta(minutes=25),
            scheduled_start=slot,
            scheduled_end=slot + timedelta(minutes=25),
            metadata={"title": "Morning task", "goal_id": "g1"},
        ))


@pytest.mark.asyncio
async def test_you_api__get__matches_response_model(monkeypatch, recording_llm):
    from app.api.v1.endpoints.you import YouResponse

    supabase = FakeSupabase()
    container = _container(supabase)
    await _skips_at_nine(container)
    await container.memory_service.remember(
        "alice", MemoryCandidate(memory_type="goal", content=MARATHON, confidence=0.8)
    )
    await container.repos.constraints.add("alice", BehavioralConstraint(
        id="00000000-0000-4000-8000-0000000a11ce", user_id="alice",
        raw_text="No work after 10 PM", constraint_type="sleep", source="said",
    ))
    await container.repos.usage.record(LLMUsageEvent(
        user_id="alice", task="voice_of_jarvis", role="fast", provider="local", model="gemma",
        location=LLMLocation.LOCAL, input_tokens=120, output_tokens=30, latency_ms=40,
        created_at=datetime.now(timezone.utc),
    ))

    async with _api(monkeypatch, container, supabase) as api:
        resp = await api.get("/api/v1/you", params={"user_id": "alice"})

    assert resp.status_code == 200
    body = resp.json()
    YouResponse.model_validate(body)
    assert set(body) == _TOP_LEVEL
    assert set(body["profile"]) == _PROFILE
    assert body["profile"] == {
        "display_name": None, "timezone": "Asia/Kolkata", "wake_time": "07:00",
        "autonomy_mode": "ask_first",
    }
    (pattern,) = body["patterns"]
    assert set(pattern) == _PATTERN
    assert pattern["pattern_key"] == "skip_time_window:9"
    assert pattern["memory_id"]  # remembered by the observe the read runs first
    assert [line.split(" ", 2)[:2] for line in pattern["evidence"]] == [["set", "aside"]] * 3
    for line in [pattern["description"], *pattern["evidence"]]:
        assert "missed" not in line and "skipped" not in line
        assert blame_phrases_in(line) == []
    (rule,) = body["rules"]
    assert set(rule) == _RULE
    assert (rule["text"], rule["provenance"]) == ("No work after 10 PM", "said")
    assert datetime.fromisoformat(rule["created_at"]).tzinfo is not None
    (memory,) = body["memories"]  # the pattern's own memory shows under "patterns" only
    assert set(memory) == _MEMORY
    assert (memory["content"], memory["provenance"]) == (MARATHON, MemorySource.SAID.value)
    assert memory["freshness"] == pytest.approx(1.0, abs=1e-3)
    assert set(body["focus"]) == _FOCUS
    assert body["focus"]["source"] == "prior"
    assert [h["hour"] for h in body["focus"]["hours"]] == list(range(24))
    assert set(body["focus"]["hours"][9]) == _HOUR
    assert (body["focus"]["hours"][9]["done"], body["focus"]["hours"][9]["not_done"]) == (0, 3)
    receipt = body["receipt"]
    assert set(receipt) == _RECEIPT
    assert {k: v for k, v in receipt.items() if k != "since"} == {
        "truncated": False, "on_device_calls": 1, "cloud_calls": 0, "pii_redactions": 0,
        "input_tokens": 120, "output_tokens": 30,
    }
    window = datetime.now(timezone.utc) - datetime.fromisoformat(receipt["since"])
    assert timedelta(days=30) <= window < timedelta(days=30, minutes=5)
    assert recording_llm.calls == []  # one read of what Jarvis knows: no model is asked


@pytest.mark.asyncio
async def test_you_api__streak_days__the_coachs_streak_on_the_users_days(monkeypatch):
    """The streak the coach counts (completions, on the user's clock) is on the page the
    dashboard already reads, so the frontend need not count it itself."""
    from zoneinfo import ZoneInfo

    from app.services.analytical.mastery_tracker import streak_as_of

    ist = ZoneInfo("Asia/Kolkata")
    supabase = FakeSupabase()
    container = _container(supabase)
    today = datetime.now(ist).date()
    # 00:30 IST: still the previous day in UTC, so only a local count sees three in a row.
    for days_ago in (0, 1, 2, 4):  # a run of three ending today, then a gap
        at = datetime.combine(today - timedelta(days=days_ago), time(0, 30), ist)
        await container.repos.task_events.append("alice", TaskEvent(
            user_id="alice", task_id=f"t{days_ago}", event_type=TaskEventType.COMPLETED,
            occurred_at=at, scheduled_start=at, scheduled_end=at + timedelta(minutes=25),
        ))

    async with _api(monkeypatch, container, supabase) as api:
        alice = await api.get("/api/v1/you", params={"user_id": "alice"})
        bob = await api.get("/api/v1/you", params={"user_id": "bob"})

    assert alice.status_code == bob.status_code == 200
    assert alice.json()["streak_days"] == 3
    assert bob.json()["streak_days"] == 0
    # The coach's own count over the same history agrees.
    events = await container.repos.task_events.list_for_user("alice")
    assert streak_as_of(events, ist, datetime.now(timezone.utc)) == 3


@pytest.mark.asyncio
async def test_you_api__bad_timezone__422_envelope(monkeypatch):
    supabase = FakeSupabase()
    container = _container(supabase)
    params = {"user_id": "alice"}
    await container.repos.profiles.update(  # settings chosen earlier, all unlike the request
        "alice", timezone="Asia/Tokyo", wake_time=time(5, 45), autonomy_mode="auto_low_risk"
    )
    (prior,) = [dict(r) for r in supabase.rows["user_profiles"]]

    async with _api(monkeypatch, container, supabase) as api:
        unknown = await api.patch(
            "/api/v1/you/settings", params=params,
            json={"timezone": "Mars/Olympus_Mons", "wake_time": "06:30",
                  "autonomy_mode": "suggest_only"},
        )
        too_long = await api.patch(
            "/api/v1/you/settings", params=params, json={"timezone": "A" * 65}
        )
        not_a_time = await api.patch(
            "/api/v1/you/settings", params=params, json={"wake_time": "25:99"}
        )
        unknown_key = await api.patch(
            "/api/v1/you/settings", params=params, json={"wake_up": "06:30"}
        )
        (after_422s,) = [dict(r) for r in supabase.rows["user_profiles"]]
        fine = await api.patch(
            "/api/v1/you/settings", params=params,
            json={"autonomy_mode": "suggest_only", "wake_time": "06:30",
                  "timezone": "Europe/London"},
        )

    for resp in (unknown, too_long, not_a_time, unknown_key):
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert set(error) == {"code", "message", "details", "request_id"}
        assert error["code"] == "validation_error"
    assert unknown.json()["error"]["details"] == {"fields": ["timezone"]}
    assert after_422s == prior  # a rejected change changes nothing, not even the valid parts
    assert fine.status_code == 200
    assert fine.json() == {
        "display_name": None, "timezone": "Europe/London", "wake_time": "06:30",
        "autonomy_mode": "suggest_only",
    }
    (row,) = supabase.rows["user_profiles"]
    assert (row["timezone"], row["wake_time"]) == ("Europe/London", "06:30:00")


class _BrokenTable:
    """A table whose every query fails with a planted secret."""

    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise RuntimeError(f"connection to {SECRET} refused")

        return fail


class _EventsDown(FakeSupabase):
    def table(self, name: str):
        return _BrokenTable() if name == "task_events" else super().table(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", ["store_raises", "no_database"])
async def test_you_api__store_down__503_envelope(monkeypatch, outage):
    if outage == "store_raises":
        supabase = _EventsDown()
        container = _container(supabase)
    else:  # what the lifespan builds with Supabase unreachable: in-memory repositories
        supabase, container = None, _container(None)

    async with _api(monkeypatch, container, supabase) as api:
        responses = [
            await api.get("/api/v1/you", params={"user_id": "alice"}),
            *([await api.patch(
                "/api/v1/you/settings", params={"user_id": "alice"},
                json={"autonomy_mode": "suggest_only"},
            )] if outage == "no_database" else []),
        ]

    for resp in responses:
        assert resp.status_code == 503
        error = resp.json()["error"]
        assert set(error) == {"code", "message", "details", "request_id"}
        assert error["code"] == "persistence_unavailable"
        assert SECRET not in resp.text and "refused" not in resp.text


# --- several page loads at once (React's double mount sends two) --------------------


def _delayed(monkeypatch, obj: object, name: str, seconds: float) -> None:
    """Each call of ``obj.name`` waits ``seconds`` first, so concurrent requests overlap."""
    import asyncio

    fast = getattr(obj, name)

    async def slow(*args, **kwargs):
        await asyncio.sleep(seconds)
        return await fast(*args, **kwargs)

    monkeypatch.setattr(obj, name, slow)


@pytest.mark.asyncio
async def test_you_api__three_loads_at_once__all_200_same_content_and_one_look(monkeypatch):
    import asyncio

    supabase = FakeSupabase()
    container = _container(supabase)
    await _skips_at_nine(container)
    patterns = container.patterns
    looks: list[str] = []
    look_now = patterns._look_now

    async def counted(user_id, **kwargs):
        looks.append(user_id)
        return await look_now(user_id, **kwargs)

    monkeypatch.setattr(patterns, "_look_now", counted)
    _delayed(monkeypatch, container.repos.task_events, "list_for_user", 0.05)

    async with _api(monkeypatch, container, supabase) as api:
        responses = await asyncio.gather(
            *(api.get("/api/v1/you", params={"user_id": "alice"}) for _ in range(3))
        )

    assert [r.status_code for r in responses] == [200, 200, 200]
    bodies = [r.json() for r in responses]
    assert all(b["patterns"] == bodies[0]["patterns"] for b in bodies)
    assert all(b["rules"] == bodies[0]["rules"] and b["focus"] == bodies[0]["focus"] for b in bodies)
    (pattern,) = bodies[0]["patterns"]
    assert pattern["pattern_key"] == "skip_time_window:9" and pattern["memory_id"]
    assert looks == ["alice"], "one look at the history for all three loads"
    assert [r["pattern_key"] for r in supabase.rows["user_memories"]] == ["skip_time_window:9"]


@pytest.mark.asyncio
async def test_you_api__pattern_write_lost_to_another_process__200_not_503(monkeypatch):
    """Another server stored the same pattern a moment earlier: someone else did it."""
    from app.domain import errors

    supabase = FakeSupabase()
    container = _container(supabase)
    await _skips_at_nine(container)

    async def taken(user_id, memory, **kwargs):
        raise errors.ConflictError(
            "That pattern is already remembered.", details={"pattern_key": memory.pattern_key}
        )

    monkeypatch.setattr(container.repos.memories, "add", taken)

    async with _api(monkeypatch, container, supabase) as api:
        resp = await api.get("/api/v1/you", params={"user_id": "alice"})

    assert resp.status_code == 200
    (pattern,) = resp.json()["patterns"]
    assert pattern["pattern_key"] == "skip_time_window:9"


@pytest.mark.asyncio
async def test_you_api__steady_page_load__each_table_read_once(monkeypatch):
    """The profile and the 28-day history are read once, and shared by PEARL's look and
    the focus profile; the patterns' memories in one read."""
    from collections import Counter

    from tests.behaviour.conftest import settle_background_tasks

    supabase = FakeSupabase()
    container = _container(supabase)
    await _skips_at_nine(container)

    async with _api(monkeypatch, container, supabase) as api:
        first = await api.get("/api/v1/you", params={"user_id": "alice"})
        await settle_background_tasks()  # what the first sight set going (a rule proposal)
        supabase.selects.clear()
        again = await api.get("/api/v1/you", params={"user_id": "alice"})

    assert first.status_code == again.status_code == 200
    assert again.json()["patterns"] == first.json()["patterns"]
    reads = Counter(table for table, _ in supabase.selects)
    assert reads == {
        "user_profiles": 1,
        "task_events": 1,
        "user_memories": 2,  # the patterns' memories, and the memories in use
        "behavioral_constraints": 1,
        "llm_usage_events": 1,
    }
