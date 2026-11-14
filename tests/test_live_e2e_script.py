"""The live end-to-end script waits for background work by polling, never a fixed sleep.

The script itself needs a running server and real credentials, so it is never run
here; only its waiting helper is.
"""

import pytest

import scripts.live_e2e as live_e2e


class _FakeTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time(monkeypatch) -> _FakeTime:
    clock = _FakeTime()
    monkeypatch.setattr(live_e2e, "time", clock)
    return clock


def test_poll_until__work_lands_on_the_third_look__returns_it_without_waiting_longer(fake_time):
    answers = iter([None, [], ["the rule"]])

    found = live_e2e.poll_until(lambda: next(answers))

    assert found == ["the rule"]
    assert fake_time.now == 2 * live_e2e.POLL_INTERVAL_S


def test_poll_until__work_never_lands__gives_up_at_the_deadline_with_the_last_value(fake_time):
    found = live_e2e.poll_until(lambda: [])

    assert found == []
    assert live_e2e.BACKGROUND_DEADLINE_S <= fake_time.now <= 20.0 + live_e2e.POLL_INTERVAL_S
    assert set(fake_time.sleeps) == {live_e2e.POLL_INTERVAL_S}
