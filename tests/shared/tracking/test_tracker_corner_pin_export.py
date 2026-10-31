"""阶段 8 验收：corner pin 导出（透视素材四角误差阈值 + 导出结构）。"""

import json

import numpy as np
import pytest

from src.shared.tracking.corner_pin import (
    CORNER_ORDER,
    build_corner_pin,
    corner_pin_error,
    corner_positions,
    export_corner_pin_json,
    normalized_corners,
)
from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.tracker_2d import Tracker2D
from src.shared.tracking.types import (
    FailureReason,
    TrackStatus,
    TrackTransform,
    TrackingMode,
)

from tests.shared.tracking import tracker_support as ts

RECT = (480.0, 250.0, 320.0, 220.0)


def translation(dx: float, dy: float = 0.0) -> np.ndarray:
    return np.array([[1, 0, dx], [0, 1, dy], [0, 0, 1]], dtype=np.float64)


class TestCornerPositions:
    def test_identity_returns_rect_corners(self):
        corners = corner_positions(np.eye(3), RECT)
        x, y, w, h = RECT
        assert corners == [
            (x, y), (x + w, y), (x + w, y + h), (x, y + h),
        ]

    def test_translation_offsets(self):
        corners = corner_positions(translation(10.0, -5.0), RECT)
        x, y, w, h = RECT
        assert corners[0] == pytest.approx((x + 10.0, y - 5.0))
        assert corners[2] == pytest.approx((x + w + 10.0, y + h - 5.0))

    def test_division_by_zero_returns_none(self):
        H = np.array([[0, 0, 1], [0, 0, 1], [0, 0, 0]], dtype=np.float64)
        assert corner_positions(H, RECT) is None

    def test_normalized_corners(self):
        corners = [(100.0, 50.0), (200.0, 50.0)]
        norm = normalized_corners(corners, 1000, 500)
        assert norm == [(0.1, 0.1), (0.2, 0.1)]
        assert normalized_corners(corners, 0, 500) is None


def _make_binding(reference_frame: int = 10) -> TrackingBinding:
    return TrackingBinding(
        binding_id="b1", clip_id="c1", shape_id="s1",
        source_frame_start=8, source_frame_end=14,
        reference_frame=reference_frame,
        mode=TrackingMode.HOMOGRAPHY,
    )


class TestBuildCornerPin:
    def test_failed_frames_skipped_not_faked(self):
        binding = _make_binding()
        binding.apply_result(
            TrackTransform.from_matrix(
                10, np.eye(3), 0.95, TrackStatus.VALID))
        binding.apply_result(
            TrackTransform.failed(11, FailureReason.OCCLUSION))
        binding.apply_result(
            TrackTransform.from_matrix(
                12, translation(4.0), 0.9, TrackStatus.VALID))
        data = build_corner_pin(binding, RECT)
        assert set(data["frames"]) == {10, 12}
        assert 11 in data["skipped"]
        # 导出四角与手动求值一致（不造假数据）
        expected = corner_positions(translation(4.0), RECT)
        assert data["frames"][12] == expected

    def test_manual_correction_takes_priority(self):
        binding = _make_binding()
        binding.apply_result(
            TrackTransform.from_matrix(
                10, translation(4.0), 0.9, TrackStatus.VALID))
        binding.set_manual_correction(
            TrackTransform.from_matrix(
                10, translation(99.0), 1.0, TrackStatus.MANUAL))
        data = build_corner_pin(binding, RECT)
        assert data["frames"][10] == corner_positions(translation(99.0), RECT)

    def test_empty_binding(self):
        data = build_corner_pin(_make_binding(), RECT)
        assert data["frames"] == {}
        assert data["skipped"] == []
        assert data["reference_frame"] == 10

    def test_explicit_frame_range_holes_recorded(self):
        binding = _make_binding()
        binding.apply_result(
            TrackTransform.from_matrix(
                10, np.eye(3), 0.9, TrackStatus.VALID))
        binding.apply_result(
            TrackTransform.from_matrix(
                13, translation(2.0), 0.9, TrackStatus.VALID))
        data = build_corner_pin(binding, RECT, frame_start=10, frame_end=13)
        # 11/12 无结果 → 跳过（不用插值假装有效）
        assert sorted(data["skipped"]) == [11, 12]


class TestExportJson:
    def test_json_structure(self, tmp_path):
        binding = _make_binding()
        binding.apply_result(
            TrackTransform.from_matrix(
                10, np.eye(3), 0.95, TrackStatus.VALID))
        binding.apply_result(
            TrackTransform.failed(11, FailureReason.OCCLUSION))
        path = tmp_path / "pin.json"
        payload = export_corner_pin_json(
            binding, RECT, path, frame_size=(1280, 720))
        assert path.exists()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == payload
        assert saved["format"] == "corner_pin"
        assert saved["version"] == 1
        assert saved["corner_order"] == list(CORNER_ORDER)
        assert saved["frame_size"] == [1280, 720]
        assert saved["reference_frame"] == 10
        assert saved["pattern_rect"] == list(RECT)
        assert "10" in saved["frames"]
        assert saved["skipped"] == [11]
        assert len(saved["frames"]["10"]) == 4
        for point in saved["frames"]["10"]:
            assert len(point) == 2


@pytest.mark.skipif(not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用")
class TestCornerPinOnPerspectiveFixture:
    """透视素材跟踪 → corner pin 导出四角与真值对比（验收阈值）。"""

    def test_perspective_corner_pin_accuracy(self):
        fixture_dir = ts.fixture_dir()
        manifest = ts.load_manifest(fixture_dir, "perspective")
        tracker = Tracker2D()
        request = ts.build_request(
            fixture_dir, "perspective", TrackingMode.HOMOGRAPHY)
        result = tracker.track(request)

        binding = _make_binding(reference_frame=manifest["reference_frame"])
        for t in result.transforms.values():
            binding.apply_result(t)

        px, py, pw, ph = manifest["pattern_rect_reference"]
        rect = (px, py, pw, ph)
        data = build_corner_pin(binding, rect)
        # 真值四角用 GT homography 变换 pattern 四角得到（与跟踪输出同基准；
        # manifest 的 ``corners`` 字段为渲染坐标系，两者存在系统性偏移）
        truth = {}
        for frame, meta in enumerate(manifest["frames"]):
            if not meta.get("visible", True):
                continue
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            truth[frame] = corner_positions(H_gt, rect)
        errors = corner_pin_error(data["frames"], truth)
        assert errors, "透视素材应有可对比帧"
        # 验收：透视测试素材四角平均误差达到项目设定阈值
        assert np.mean(list(errors.values())) < 2.0
        assert np.max(list(errors.values())) < 5.0

    def test_export_roundtrip(self, tmp_path):
        fixture_dir = ts.fixture_dir()
        manifest = ts.load_manifest(fixture_dir, "perspective")
        tracker = Tracker2D()
        result = tracker.track(
            ts.build_request(
                fixture_dir, "perspective", TrackingMode.HOMOGRAPHY))
        binding = _make_binding(reference_frame=manifest["reference_frame"])
        for t in result.transforms.values():
            binding.apply_result(t)
        px, py, pw, ph = manifest["pattern_rect_reference"]
        path = tmp_path / "perspective_pin.json"
        payload = export_corner_pin_json(
            binding, (px, py, pw, ph), path,
            frame_size=(manifest["width"], manifest["height"]),
        )
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["corner_order"] == list(CORNER_ORDER)
        assert len(saved["frames"]) == len(payload["frames"])
        assert payload["skipped"] == saved["skipped"]
