"""M21.2b · 逐版本 QC 批量回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.delivery_qc_batch import (
    measure_version_lufs,
    subtitle_qc_summary,
)


class TestSubtitleQcSummary:
    def test_all_ok_and_blocking(self):
        s = subtitle_qc_summary([
            {"blocking": 0, "warning": 2},
            {"blocking": 3, "warning": 0},
        ])
        assert not s["all_ok"]
        assert s["total_blocking"] == 3
        assert s["versions"][0]["ok"] is True
        assert s["versions"][1]["ok"] is False

    def test_empty(self):
        s = subtitle_qc_summary([])
        assert s["all_ok"] and s["total_blocking"] == 0


class TestMeasureVersionLufs:
    def test_missing_file_error_not_pass(self, tmp_path):
        report = measure_version_lufs([str(tmp_path / "gone.mp4")])
        entry = report[str(tmp_path / "gone.mp4")]
        assert entry["input"] is None
        assert entry["error"]  # 失败 ≠ 通过

    def test_real_file_measured(self, tmp_path):
        import av
        import numpy as np
        out = tmp_path / "v.mp4"
        container = av.open(str(out), mode="w")
        vstream = container.add_stream("libx264", rate=25)
        vstream.width, vstream.height = 64, 48
        vstream.pix_fmt = "yuv420p"
        for i in range(6):
            frame = av.VideoFrame.from_ndarray(
                np.full((48, 64, 3), 40 + i * 20, dtype=np.uint8),
                format="rgb24")
            for packet in vstream.encode(frame):
                container.mux(packet)
        for packet in vstream.encode():
            container.mux(packet)
        container.close()
        report = measure_version_lufs([str(out)])
        entry = report[str(out)]
        assert entry["input"] is not None
        assert entry["error"] == ""
        # 无音轨：audio_present=False（如实），LUFS 未测（None，不冒充）
        assert entry["input"].audio_present is False
        assert "integrated_lufs" not in entry
