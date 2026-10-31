"""Serialized goal continuation admission tests."""

from __future__ import annotations

from src.features.edit.ai_editing.goal_round_driver import GoalRoundDriver


def test_one_active_round_per_goal_and_release_after_completion() -> None:
    events: list[tuple[str, dict]] = []
    driver = GoalRoundDriver(event_sink=lambda kind, payload: events.append((kind, payload)))
    first = driver.reserve("goal-1", run_id="run-a", expected_revision=2, round_number=3)
    assert first is not None
    assert driver.reserve("goal-1", run_id="run-b") is None
    assert any(kind == "goal.round.deduplicated" for kind, _ in events)

    assert driver.execute(first, lambda: True) is True
    second = driver.reserve("goal-1", run_id="run-b", expected_revision=3, round_number=4)
    assert second is not None
    assert driver.execute(second, lambda: False) is False
    assert driver.active_reservations() == ()
    assert any(kind == "goal.round.completed" for kind, _ in events)
    assert any(kind == "goal.round.failed" for kind, _ in events)


def test_failed_operation_releases_reservation() -> None:
    driver = GoalRoundDriver()
    reservation = driver.reserve("goal-2")
    assert reservation is not None
    try:
        driver.execute(reservation, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    except RuntimeError:
        pass
    assert driver.active("goal-2") is None
