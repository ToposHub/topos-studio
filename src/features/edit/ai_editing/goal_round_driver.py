"""Serialized, observable admission for asynchronous goal continuations."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable


@dataclass(frozen=True)
class GoalRoundReservation:
    reservation_id: str
    goal_id: str
    run_id: str
    expected_revision: int
    round_number: int
    created_at: float


class GoalRoundDriver:
    """Allow at most one live continuation round per goal.

    The driver owns admission and lifecycle only.  The host supplies the
    actual operation, which keeps Qt/editor state on its existing thread while
    still preventing duplicate callbacks from starting competing rounds.
    """

    def __init__(
        self,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._event_sink = event_sink
        self._clock = clock
        self._active: dict[str, GoalRoundReservation] = {}
        self._lock = RLock()

    def reserve(
        self,
        goal_id: str,
        *,
        run_id: str = "",
        expected_revision: int = 0,
        round_number: int = 1,
    ) -> GoalRoundReservation | None:
        goal_id = str(goal_id or "").strip()
        if not goal_id:
            return None
        with self._lock:
            active = self._active.get(goal_id)
            if active is not None:
                self._emit("goal.round.deduplicated", {
                    "goal_id": goal_id,
                    "run_id": str(run_id),
                    "active_reservation_id": active.reservation_id,
                    "active_round": active.round_number,
                })
                return None
            reservation = GoalRoundReservation(
                reservation_id=uuid.uuid4().hex[:24],
                goal_id=goal_id,
                run_id=str(run_id or ""),
                expected_revision=max(0, int(expected_revision)),
                round_number=max(1, int(round_number)),
                created_at=self._clock(),
            )
            self._active[goal_id] = reservation
        self._emit("goal.round.reserved", self._payload(reservation))
        return reservation

    def start(self, reservation: GoalRoundReservation) -> bool:
        with self._lock:
            current = self._active.get(reservation.goal_id)
            allowed = current is not None and current.reservation_id == reservation.reservation_id
        self._emit(
            "goal.round.started" if allowed else "goal.round.start_rejected",
            self._payload(reservation),
        )
        return allowed

    def execute(
        self,
        reservation: GoalRoundReservation,
        operation: Callable[[], Any],
    ) -> Any:
        """Run one admitted operation and always release its reservation."""

        if not self.start(reservation):
            return None
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001 - release before propagating
            self.complete(reservation, success=False, reason=type(exc).__name__)
            raise
        self.complete(reservation, success=bool(result))
        return result

    def complete(
        self,
        reservation: GoalRoundReservation,
        *,
        success: bool,
        reason: str = "",
    ) -> bool:
        with self._lock:
            current = self._active.get(reservation.goal_id)
            if current is None or current.reservation_id != reservation.reservation_id:
                return False
            self._active.pop(reservation.goal_id, None)
        self._emit(
            "goal.round.completed" if success else "goal.round.failed",
            {**self._payload(reservation), "reason": str(reason or "")[:160]},
        )
        return True

    def cancel(self, goal_id: str, *, reason: str = "cancelled") -> bool:
        with self._lock:
            reservation = self._active.pop(str(goal_id or ""), None)
        if reservation is None:
            return False
        self._emit(
            "goal.round.cancelled",
            {**self._payload(reservation), "reason": str(reason)[:160]},
        )
        return True

    def active(self, goal_id: str) -> GoalRoundReservation | None:
        with self._lock:
            return self._active.get(str(goal_id or ""))

    def active_reservations(self) -> tuple[GoalRoundReservation, ...]:
        with self._lock:
            return tuple(self._active.values())

    def clear(self) -> None:
        with self._lock:
            self._active.clear()

    @staticmethod
    def _payload(reservation: GoalRoundReservation) -> dict[str, Any]:
        return {
            "reservation_id": reservation.reservation_id,
            "goal_id": reservation.goal_id,
            "run_id": reservation.run_id,
            "expected_revision": reservation.expected_revision,
            "round_number": reservation.round_number,
        }

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(kind, payload)
        except Exception:
            # Diagnostics must never become a continuation failure path.
            return


__all__ = ["GoalRoundDriver", "GoalRoundReservation"]
