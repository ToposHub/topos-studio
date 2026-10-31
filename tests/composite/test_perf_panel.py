"""Perf trace retention and JSON export tests."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.features.composite.ui.perf_panel import PerfPanel  # noqa: E402
from src.features.composite.utils.perf_monitor import (  # noqa: E402
    PerfMonitor,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_perf_panel_exports_bounded_trace_json(qapp, tmp_path):
    monitor = PerfMonitor()
    monitor.begin_frame()
    monitor.record_node_eval("n1", "blur", 3.0, False, True)
    monitor.record_frame(
        request_id=4,
        total_eval_ms=3.0,
        cache_hits=0,
        cache_misses=1,
        cache_evictions=0,
        active_textures=2,
        vram_mb=32.0,
    )
    panel = PerfPanel()
    panel.set_history_provider(monitor.get_history)
    panel.update_from_snapshot(monitor.get_snapshot())

    output = tmp_path / "perf.json"
    assert panel.export_report(str(output)) is True
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["sample_count"] == 1
    assert payload["hot_nodes"][0]["node_id"] == "n1"
