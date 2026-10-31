from __future__ import annotations

import logging
import time

import pytest

from src.shared.control_surfaces.tangent import hub_client
from src.shared.control_surfaces.tangent.hub_client import TangentHubClient


def _wait_for(qapp, condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


def test_failure_streak_reports_error_and_unavailable_once(qapp):
    client = TangentHubClient("Test", system_path=".")
    errors: list[str] = []
    unavailable: list[int] = []
    client.error.connect(errors.append)
    client.unavailable.connect(lambda: unavailable.append(1))

    for _ in range(client.UNAVAILABLE_AFTER_FAILURES * 2):
        client._report_connection_failure("timed out")

    # A missing Hub must not spam: one error for the transient notice and
    # exactly one unavailable escalation per failure streak.
    assert errors == ["timed out"]
    assert len(unavailable) == 1
    client.stop()


def test_wake_resets_the_failure_streak(qapp):
    client = TangentHubClient("Test", system_path=".")
    unavailable: list[int] = []
    client.unavailable.connect(lambda: unavailable.append(1))

    for _ in range(client.UNAVAILABLE_AFTER_FAILURES):
        client._report_connection_failure("timed out")
    assert len(unavailable) == 1

    # An explicit user request (preferences Apply / wake) restarts the grace
    # period, so the next streak can escalate again.
    client.wake()
    for _ in range(client.UNAVAILABLE_AFTER_FAILURES):
        client._report_connection_failure("timed out")

    assert len(unavailable) == 2
    client.stop()


def test_unreachable_hub_goes_dormant_without_error_spam(qapp, monkeypatch):
    attempts: list[int] = []

    def fake_create_connection(address, timeout=None):
        attempts.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(
        hub_client.socket,
        "create_connection",
        fake_create_connection,
    )

    client = TangentHubClient("Test", system_path=".")
    client.UNAVAILABLE_AFTER_FAILURES = 3
    client.UNAVAILABLE_PROBE_DELAY = 0.05
    client._reconnect_min_delay = 0.01
    client._reconnect_max_delay = 0.02

    errors: list[str] = []
    unavailable: list[int] = []
    client.error.connect(errors.append)
    client.unavailable.connect(lambda: unavailable.append(1))

    client.start()
    try:
        assert _wait_for(qapp, lambda: len(attempts) >= 6)
    finally:
        client.stop()

    assert len(errors) == 1
    assert len(unavailable) == 1
    assert not client.is_running


def test_successful_connect_resets_the_streak(qapp):
    client = TangentHubClient("Test", system_path=".")
    errors: list[str] = []
    unavailable: list[int] = []
    client.error.connect(errors.append)
    client.unavailable.connect(lambda: unavailable.append(1))

    # A running Hub restart triggers this exact reset in the worker before
    # its next failure, so the new streak must start from attempt one.
    client._consecutive_failures = client.UNAVAILABLE_AFTER_FAILURES
    client._consecutive_failures = 0
    client._report_connection_failure("timed out")

    assert errors == ["timed out"]
    assert unavailable == []
    client.stop()


@pytest.mark.parametrize(
    ("failures", "expected_level"),
    [
        (1, logging.INFO),
        (2, logging.DEBUG),
        (TangentHubClient.UNAVAILABLE_AFTER_FAILURES, logging.INFO),
        (TangentHubClient.UNAVAILABLE_AFTER_FAILURES + 1, logging.DEBUG),
    ],
)
def test_failure_logging_quiets_down_after_the_streak(
    qapp,
    caplog,
    failures,
    expected_level,
):
    client = TangentHubClient("Test", system_path=".")
    logger_name = "src.shared.control_surfaces.tangent.hub_client"
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        for _ in range(failures):
            client._report_connection_failure("timed out")

    assert caplog.records[-1].levelno == expected_level
    client.stop()
