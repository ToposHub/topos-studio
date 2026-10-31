"""阶段 7 验收：前向+后向结果在参考帧合并、冲突提示与轨迹分段。"""

import numpy as np
import pytest

from src.shared.tracking.merge import (
    MergeConflict,
    TrackSegment,
    merge_bidirectional,
    merge_into_binding,
    pattern_corner_deviation,
    trajectory_segments,
)
from src.shared.tracking.tracker_2d import (
    Tracker2D,
    TrackDirection,
    TrackResult,
)
from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.types import (
    FailureReason,
    TrackStatus,
    TrackTransform,
    TrackingMode,
)

from tests.shared.tracking import tracker_support as ts

PATTERN_RECT = (480.0, 250.0, 320.0, 220.0)
REF = 50


def translation(dx: float, dy: float = 0.0) -> np.ndarray:
    return np.array([[1, 0, dx], [0, 1, dy], [0, 0, 1]], dtype=np.float64)


def valid(frame: int, dx: float, conf: float = 0.9) -> TrackTransform:
    return TrackTransform.from_matrix(
        frame, translation(dx), conf, TrackStatus.VALID
    )


def failed(frame: int) -> TrackTransform:
    return TrackTransform.failed(frame, FailureReason.OCCLUSION)


def make_result(frames: dict, generation: int = 0) -> TrackResult:
    return TrackResult(
        generation=generation,
        cancelled=False,
        transforms=frames,
        observations={},
        diagnostics={},
    )


class TestMergeAtReference:
    def test_reference_frame_is_identity(self):
        fwd = make_result({REF: valid(REF, 0.0), 51: valid(51, 4.0)})
        bwd = make_result({REF: valid(REF, 0.0), 49: valid(49, -4.0)})
        merged = merge_bidirectional(fwd, bwd, REF, PATTERN_RECT)
        ref_t = merged.transforms[REF]
        assert np.allclose(ref_t.matrix_np, np.eye(3))
        assert ref_t.status == TrackStatus.VALID
        assert ref_t.confidence == 1.0

    def test_forward_backward_partition(self):
        fwd = make_result(
            {REF: valid(REF, 0.0), 51: valid(51, 4.0), 52: valid(52, 8.0)}
        )
        bwd = make_result(
            {REF: valid(REF, 0.0), 49: valid(49, -4.0), 48: valid(48, -8.0)}
        )
        merged = merge_bidirectional(fwd, bwd, REF, PATTERN_RECT)
        assert set(merged.transforms) == {48, 49, REF, 51, 52}
        assert merged.transforms[52].translation == (8.0, 0.0)
        assert merged.transforms[48].translation == (-8.0, 0.0)
        assert merged.conflicts == ()

    def test_overlap_conflict_detected(self):
        """双方都覆盖的帧偏差超阈值 → 记录冲突，保留高置信度一方。"""
        fwd = make_result({REF: valid(REF, 0.0), 55: valid(55, 10.0, 0.9)})
        bwd = make_result({REF: valid(REF, 0.0), 55: valid(55, 40.0, 0.7)})
        merged = merge_bidirectional(fwd, bwd, REF, PATTERN_RECT)
        assert len(merged.conflicts) == 1
        conflict = merged.conflicts[0]
        assert conflict.frame == 55
        assert conflict.deviation_px == pytest.approx(30.0, abs=1e-6)
        # 保留置信度高的一方（前向 0.9）
        assert merged.transforms[55].translation == (10.0, 0.0)

    def test_overlap_within_threshold_no_conflict(self):
        fwd = make_result({REF: valid(REF, 0.0), 55: valid(55, 10.0, 0.9)})
        bwd = make_result({REF: valid(REF, 0.0), 55: valid(55, 12.0, 0.8)})
        merged = merge_bidirectional(fwd, bwd, REF, PATTERN_RECT)
        assert merged.conflicts == ()
        assert merged.transforms[55].translation == (10.0, 0.0)

    def test_corner_deviation_metric(self):
        dev = pattern_corner_deviation(
            translation(0.0), translation(3.0, 4.0), PATTERN_RECT
        )
        assert dev == pytest.approx(5.0, abs=1e-9)


class TestTrajectorySegments:
    def test_segments_split_by_failure(self):
        transforms = {
            10: valid(10, 0), 11: valid(11, 1), 12: valid(12, 2),
            13: failed(13), 14: failed(14),
            15: valid(15, 5), 16: valid(16, 6),
        }
        segments = trajectory_segments(transforms)
        assert segments == (
            TrackSegment(10, 12, True),
            TrackSegment(13, 14, False),
            TrackSegment(15, 16, True),
        )

    def test_segments_break_on_missing_frames(self):
        """无结果帧断段（不连线假装连续）。"""
        transforms = {10: valid(10, 0), 11: valid(11, 1), 14: valid(14, 4)}
        segments = trajectory_segments(transforms)
        assert segments == (
            TrackSegment(10, 11, True),
            TrackSegment(14, 14, True),
        )

    def test_weak_frames_are_usable(self):
        weak = TrackTransform.from_matrix(
            11, translation(1.0), 0.3, TrackStatus.WEAK
        )
        transforms = {10: valid(10, 0), 11: weak}
        segments = trajectory_segments(transforms)
        assert segments == (TrackSegment(10, 11, True),)

    def test_empty(self):
        assert trajectory_segments({}) == ()


class TestMergeIntoBinding:
    def test_apply_merged_and_flags(self):
        binding = TrackingBinding(
            binding_id="b1", clip_id="c1", shape_id="s1",
            source_frame_start=40, source_frame_end=60, reference_frame=REF,
        )
        fwd = make_result({REF: valid(REF, 0.0), 51: valid(51, 4.0)})
        bwd = make_result({REF: valid(REF, 0.0), 49: valid(49, -4.0)})
        merged = merge_bidirectional(fwd, bwd, REF, PATTERN_RECT)
        conflict_count = merge_into_binding(binding, merged)
        assert conflict_count == 0
        assert binding.has_forward and binding.has_backward
        assert binding.transform_at(51).translation == (4.0, 0.0)
        assert binding.transform_at(49).translation == (-4.0, 0.0)

    def test_manual_corrections_not_overwritten(self):
        binding = TrackingBinding(
            binding_id="b1", clip_id="c1", shape_id="s1", reference_frame=REF,
        )
        manual = TrackTransform.from_matrix(
            51, translation(99.0), 1.0, TrackStatus.MANUAL
        )
        binding.set_manual_correction(manual)
        fwd = make_result({REF: valid(REF, 0.0), 51: valid(51, 4.0)})
        bwd = make_result({REF: valid(REF, 0.0)})
        merged = merge_bidirectional(fwd, bwd, REF, PATTERN_RECT)
        merge_into_binding(binding, merged)
        assert binding.transform_at(51).translation == (99.0, 0.0)
        assert binding.transform_at(51).status == TrackStatus.MANUAL


@pytest.mark.skipif(not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用")
class TestRealFixtureMerge:
    """遮挡素材：双向合并 → 失败段落在遮挡区间，可用段误差达标。"""

    def test_occlusion_fixture_merge(self):
        fixture_dir = ts.fixture_dir()
        manifest = ts.load_manifest(fixture_dir, "occlusion")
        tracker = Tracker2D()
        forward = tracker.track(
            ts.build_request(fixture_dir, "occlusion",
                             TrackingMode.TRANSLATION, TrackDirection.FORWARD)
        )
        backward = tracker.track(
            ts.build_request(fixture_dir, "occlusion",
                             TrackingMode.TRANSLATION, TrackDirection.BACKWARD)
        )
        W, H = manifest["width"], manifest["height"]
        px, py, pw, ph = manifest["pattern_rect_reference"]
        merged = merge_bidirectional(
            forward, backward, manifest["reference_frame"],
            (px, py, pw, ph),
        )

        # 参考帧恒等
        assert np.allclose(
            merged.transforms[manifest["reference_frame"]].matrix_np,
            np.eye(3),
        )
        # 双向各自覆盖的帧不产生冲突（无重叠覆盖则必无冲突）
        assert merged.conflicts == ()

        # 失败分段存在且覆盖遮挡帧（失败段不画假有效）
        failed_segments = [s for s in merged.segments if not s.usable]
        assert failed_segments, "遮挡素材合并后应有失败段"
        occluded = {
            f for f, m in enumerate(manifest["frames"]) if m["occluded"]
        }
        covered = set()
        for seg in failed_segments:
            covered.update(range(seg.frame_start, seg.frame_end + 1))
        assert occluded & covered, "失败段应覆盖遮挡区间"

        # 可用段帧误差达标（不假有效）
        ref_corners = ts.reference_corners(manifest)
        usable_errors = []
        for frame, t in merged.transforms.items():
            if t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(
                manifest["frames"][frame]["homography"], dtype=np.float64
            ).reshape(3, 3)
            usable_errors.append(
                ts.corner_error(t.matrix_np, H_gt, ref_corners)
            )
        assert usable_errors, "合并后应有可用帧"
        assert np.mean(usable_errors) < 1.5
        assert np.max(usable_errors) < 3.0
        assert W > 0 and H > 0
