"""D3 cooperative decoder-context worker budget tests."""
from __future__ import annotations

import pytest

from src.shared.codec import decode_worker_budget
from src.shared.codec.decode_worker_budget import DecodeWorkerBudget


def test_budget_rebalances_and_restores() -> None:
    budget = DecodeWorkerBudget(capacity=8)
    applied: dict[str, int] = {}

    a = budget.acquire("a", role="playback", apply=lambda n: applied.__setitem__("a", n))
    assert a.assigned == 8
    assert applied["a"] == 8

    b = budget.acquire("b", role="export", apply=lambda n: applied.__setitem__("b", n))
    assert a.assigned == 4
    assert b.assigned == 4
    assert applied["a"] == applied["b"] == 4
    assert budget.describe()["assigned_total"] == 8

    b.release()
    assert a.assigned == 8
    assert applied["a"] == 8
    a.release()
    assert budget.describe()["active"] == 0


def test_budget_honors_caps_and_reports_oversubscription() -> None:
    budget = DecodeWorkerBudget(capacity=2)
    a = budget.acquire("a", requested=1)
    b = budget.acquire("b", requested=1)
    c = budget.acquire("c", requested=1)
    state = budget.describe()
    assert [entry["assigned"] for entry in state["leases"]] == [1, 1, 1]
    assert state["oversubscribed"] is True
    c.release()
    b.release()
    a.release()


def test_wide_windows_playback_budget_respects_cpu_affinity_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(decode_worker_budget.sys, "platform", "win32")
    monkeypatch.delenv("TOPOS_DECODE_WORKER_BUDGET", raising=False)
    monkeypatch.setattr(psutil.Process, "cpu_affinity", lambda self: list(range(28)), raising=False)
    assert decode_worker_budget._playback_worker_limit() == 16

    monkeypatch.setattr(psutil.Process, "cpu_affinity", lambda self: list(range(32)), raising=False)
    assert decode_worker_budget._playback_worker_limit() == 24
    budget = DecodeWorkerBudget(capacity=24, max_workers=24)
    with budget.acquire("export", role="export", requested=16) as export:
        assert export.assigned == 16
        with budget.acquire("export-2", role="export", requested=16) as second:
            assert (export.assigned, second.assigned) == (8, 8)
            with budget.acquire("playback") as playback:
                assert (export.assigned, second.assigned, playback.assigned) == (8, 8, 8)
            assert (export.assigned, second.assigned) == (8, 8)
        with budget.acquire("playback") as playback:
            assert (playback.assigned, export.assigned) == (12, 12)
    with budget.acquire("playback") as playback:
        assert playback.assigned == 24

    monkeypatch.setenv("TOPOS_DECODE_WORKER_BUDGET", "12")
    assert decode_worker_budget._playback_worker_limit() == 16


def test_bind_publishes_share_changed_during_context_creation() -> None:
    budget = DecodeWorkerBudget(capacity=24, max_workers=24)
    applied: list[int] = []
    with budget.acquire("playback") as playback:
        assert playback.assigned == 24
        with budget.acquire("export", role="export"):
            budget.bind(playback.key, applied.append)
            assert applied == [12]
        assert applied == [12, 24]

