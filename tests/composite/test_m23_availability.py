"""M23.2 · AI matte 可用性探测回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.composite_core

from src.features.composite.core import ai_matte_availability as avail


class TestFaceDetectorStatus:
    def test_no_detector_needs_detector(self):
        # 默认进程未注入
        status = avail.face_detector_status()
        assert status["status"] == avail.STATUS_NEEDS_DETECTOR
        assert "NEEDS_DETECTOR" in status["detail"]
        assert "不冒充" in status["detail"]

    def test_injected_detector_available(self):
        from src.features.composite.nodes import face_track_node as ftn

        class FakeDetector:
            def __call__(self, frame, image):
                return None

        ftn.set_face_detector(FakeDetector())
        try:
            status = avail.face_detector_status()
            assert status["status"] == avail.STATUS_AVAILABLE
            assert status["engine"] == "FakeDetector"
        finally:
            ftn.set_face_detector(None)


class TestModelFileStatus:
    def test_none_path_missing(self):
        s = avail.model_file_status(None)
        assert s["status"] == avail.STATUS_MODEL_MISSING

    def test_missing_file(self, tmp_path):
        s = avail.model_file_status(str(tmp_path / "gone.onnx"))
        assert s["status"] == avail.STATUS_MODEL_MISSING
        assert "缺失" in s["detail"]

    def test_existing_file(self, tmp_path):
        p = tmp_path / "model.onnx"
        p.write_bytes(b"x" * 128)
        s = avail.model_file_status(str(p), label="RIFE")
        assert s["status"] == avail.STATUS_AVAILABLE
        assert "RIFE" in s["detail"]
