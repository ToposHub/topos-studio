"""Host video exporter post-export hard-gate tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core


class _SuccessfulExporter:
    """P0-05：export_timeline 返回 ExportOutcome（__bool__=success）。"""

    def export_timeline(self, _config):
        from src.features.edit.core.timeline_export import ExportOutcome
        return ExportOutcome.ok()


def test_video_worker_emits_finished_only_after_postcheck(monkeypatch, qapp) -> None:
    from src.features.edit.core import timeline_export
    from src.shared.export.video_export_worker import VideoExportWorker

    monkeypatch.setattr(
        timeline_export,
        "create_timeline_exporter",
        lambda **_kwargs: _SuccessfulExporter(),
    )
    worker = VideoExportWorker()
    events: list[tuple[str, str]] = []
    worker.finished.connect(lambda path: events.append(("finished", path)))
    worker.error.connect(lambda message: events.append(("error", message)))
    worker._post_export = lambda path: (True, "")

    worker.run(None, None, SimpleNamespace(output_path="verified.mp4"))

    assert events == [("finished", "verified.mp4")]


def test_video_worker_blocks_success_when_postcheck_fails(monkeypatch, qapp) -> None:
    from src.features.edit.core import timeline_export
    from src.shared.export.video_export_worker import VideoExportWorker

    monkeypatch.setattr(
        timeline_export,
        "create_timeline_exporter",
        lambda **_kwargs: _SuccessfulExporter(),
    )
    worker = VideoExportWorker()
    events: list[tuple[str, str]] = []
    worker.finished.connect(lambda path: events.append(("finished", path)))
    worker.error.connect(lambda message: events.append(("error", message)))
    worker._post_export = lambda path: (False, "EXPORT_QUALITY_GATE_FAILED")

    worker.run(None, None, SimpleNamespace(output_path="rejected.mp4"))

    assert events == [("error", "EXPORT_QUALITY_GATE_FAILED")]
