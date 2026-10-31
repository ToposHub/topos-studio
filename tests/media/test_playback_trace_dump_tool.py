"""Tests for the playback trace dump CLI (P0.4)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
    reset_trace_collector,
)
from tools import playback_trace_dump as dump

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "playback_trace_dump.py"


@pytest.fixture(autouse=True)
def _clean_global_collector():
    reset_trace_collector()
    yield
    reset_trace_collector()


def _make_collector() -> FrameTraceCollector:
    c = FrameTraceCollector()
    c.start_session(page="unit")
    c.record(
        EditPlaybackFrameTrace(
            frame_index=1,
            decode_path="macos_vt_iosurface_zero_copy",  # AUD-39315 后的真实 decode 词表
            render_path="gpu_planar_upload",
            decode_ms=2.0,
            render_ms=1.0,
            total_ms=3.0,
        )
    )
    c.record(
        EditPlaybackFrameTrace(
            frame_index=2,
            decode_path="cpu_packed_rgb",
            render_path="cpu_qimage_upload",
            fallback_reason="vt_unavailable",
            sync_seek_count=1,
            qimage_copy=True,
            decode_ms=12.0,
            render_ms=8.0,
            total_ms=20.0,
        )
    )
    return c


def test_build_report_summarises_slice():
    c = _make_collector()
    report = dump.build_report(c, last_seconds=None)
    assert report["summary"]["reported_frames"] == 2
    assert report["summary"]["gpu_frames"] == 1
    assert report["summary"]["cpu_fallback_frames"] == 1
    assert report["summary"]["sync_seek_frames"] == 1
    assert len(report["traces"]) == 2


def test_markdown_contains_problem_counters_and_paths():
    c = _make_collector()
    md = dump.format_markdown(dump.build_report(c, last_seconds=None))
    assert "# Playback Trace Dump" in md
    assert "sync_seek_frames" in md
    assert "stale_frame_emitted" in md
    assert "macos_vt_iosurface_zero_copy" in md
    assert "cpu_qimage_upload" in md
    assert "vt_unavailable" in md


def test_dump_report_json_round_trip(tmp_path):
    c = _make_collector()
    out = tmp_path / "report.json"
    text = dump.dump_report(last_seconds=None, fmt="json", collector=c, out=out)
    loaded = json.loads(text)
    assert loaded["summary"]["reported_frames"] == 2
    assert out.exists()


def test_filter_last_seconds_drops_old_frames(monkeypatch):
    # Backdate the first trace so it falls outside a 1s window.
    c = _make_collector()
    c._traces[0].timestamp = c._traces[1].timestamp - 5.0  # noqa: SLF001
    kept = dump._filter_last_seconds(c.get_traces(), 1.0)  # noqa: SLF001
    assert len(kept) == 1
    assert kept[0].frame_index == 2


def test_self_test_cli_runs():
    result = subprocess.run(
        [sys.executable, str(TOOL), "--self-test"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "# Playback Trace Dump" in result.stdout
    assert "macos_vt_iosurface_zero_copy" in result.stdout


def test_from_file_reformats_exported_json(tmp_path):
    c = _make_collector()
    js = tmp_path / "trace.json"
    js.write_text(dump.dump_report(last_seconds=None, fmt="json", collector=c), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(TOOL), "--from-file", str(js), "--format", "markdown"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "macos_vt_iosurface_zero_copy" in result.stdout
