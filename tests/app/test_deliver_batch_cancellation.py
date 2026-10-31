"""Batch export cancellation reaches an in-flight color full-res request."""

from unittest.mock import MagicMock

from src.app.controllers.deliver_controller import DeliverControllerMixin


class _Harness(DeliverControllerMixin):
    def __init__(self):
        self._batch_cancelled = False
        self.engine = MagicMock()
        self.status_bar = MagicMock()


def test_cancel_batch_export_forwards_to_full_resolution_renderer():
    harness = _Harness()

    harness._cancel_batch_export()

    assert harness._batch_cancelled is True
    harness.engine.cancel_full_resolution.assert_called_once_with()
    harness.status_bar.showMessage.assert_called_once()


def test_full_resolution_progress_updates_global_batch_bar():
    harness = _Harness()
    harness._batch_file_processing = True
    harness._batch_files = ["/tmp/one.png", "/tmp/two.png"]
    harness._batch_index = 1
    harness.progress_bar = MagicMock()

    harness._on_full_resolution_progress("render-2", 42, "render")

    harness.progress_bar.setRange.assert_called_once_with(0, 200)
    harness.progress_bar.setValue.assert_called_once_with(142)
    harness.progress_bar.show.assert_called_once_with()
    assert "2/2" in harness.status_bar.showMessage.call_args.args[0]
