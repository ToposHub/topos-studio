"""The AI preferences page must not destroy a running probe thread.

``_ConnectionTestWorker.completed`` is emitted from inside ``run()``, before
the thread has returned.  The page used to drop its reference to the worker
in the ``completed`` handler; when that was the last reference, sip destroyed
a QThread that was still running and Qt aborted the whole process
("QThread: Destroyed while thread is still running" - observed as a crash of
the full test suite, and reachable from the "test connection" button).

The page now releases the worker on ``finished``.  The worker below stays
alive for a moment after emitting ``completed``, which makes the race
deterministic.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from PyQt6 import sip

pytest.importorskip("PyQt6.QtWidgets")

from src.features.edit.ui import ai_preferences_page
from src.features.edit.ui.ai_preferences_page import AiEditingPreferencePage
from src.shared.ai_models.generation.provider_profiles import (
    AiProviderProfile,
    FakeCredentialStore,
    ProfileRegistry,
)


class _LingeringProbeWorker(ai_preferences_page._ConnectionTestWorker):
    """Emits ``completed`` and then keeps its thread alive for a moment."""

    instances: list["_LingeringProbeWorker"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # An outside reference keeps the thread alive whatever the page does,
        # so a regression fails an assertion instead of aborting the process.
        _LingeringProbeWorker.instances.append(self)

    def run(self) -> None:
        self.completed.emit("ok", "")
        time.sleep(0.3)                                 # still running afterwards


@pytest.fixture()
def lingering_worker(monkeypatch: pytest.MonkeyPatch):
    _LingeringProbeWorker.instances = []
    monkeypatch.setattr(ai_preferences_page, "_ConnectionTestWorker", _LingeringProbeWorker)
    yield _LingeringProbeWorker
    for worker in _LingeringProbeWorker.instances:
        # deleteLater() may already have destroyed the C++ object.
        if not sip.isdeleted(worker):
            worker.wait(5000)
    _LingeringProbeWorker.instances = []


def test_probe_worker_is_released_only_after_its_thread_finished(
    qtbot, lingering_worker,
) -> None:
    registry = ProfileRegistry()
    registry.add(AiProviderProfile(
        "lan", "LAN", endpoint="http://127.0.0.1:9/v1", model_id="m",
        capabilities=("language",),
    ))
    page = AiEditingPreferencePage(registry, FakeCredentialStore())
    page._pending_api_key = "test-key"
    widget = page.create_widget()
    widget.show()
    handled: list = []
    original = page._probe_finished
    page._probe_finished = (  # type: ignore[method-assign]
        lambda *args, **kwargs: (handled.append(1), original(*args, **kwargs))
    )

    page._start_probe(registry.get("lan"), "models")

    # ``completed`` was handled while the thread is still sleeping.
    qtbot.waitUntil(lambda: bool(handled), timeout=3000)
    worker = lingering_worker.instances[-1]
    assert worker.isRunning(), "the test relies on the thread outliving ``completed``"
    # The page still owns it: destroying it now would abort the process.
    assert page._test_worker is worker

    # ``finished`` follows within milliseconds: now, and only now, the page
    # lets go (the worker's own deleteLater then destroys it).
    qtbot.waitUntil(lambda: page._test_worker is None, timeout=5000)
    widget.close()
