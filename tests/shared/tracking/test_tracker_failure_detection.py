"""阶段 2 验收：失败检测与遮挡标记（不允许无解释的失败）。"""

import numpy as np
import pytest

from src.shared.tracking.tracker_2d import Tracker2D, TrackDirection
from src.shared.tracking.types import (
    FailureReason,
    TrackingMode,
    TrackStatus,
)

from tests.shared.tracking import tracker_support as ts

pytestmark = pytest.mark.skipif(
    not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用"
)


@pytest.fixture(scope="module")
def fixture_dir() -> str:
    return ts.fixture_dir()


@pytest.fixture(scope="module")
def occlusion_results(fixture_dir):
    """前向 + 后向跟踪遮挡素材。"""
    tracker = Tracker2D()
    forward = tracker.track(
        ts.build_request(fixture_dir, "occlusion", TrackingMode.TRANSLATION,
                         TrackDirection.FORWARD)
    )
    backward = tracker.track(
        ts.build_request(fixture_dir, "occlusion", TrackingMode.TRANSLATION,
                         TrackDirection.BACKWARD)
    )
    manifest = ts.load_manifest(fixture_dir, "occlusion")
    return forward, backward, manifest


class TestFailureDetection:
    def test_occluded_frames_marked_failed(self, occlusion_results):
        """被遮挡的帧必须标记失败（不能输出假有效变换）。"""
        forward, backward, manifest = occlusion_results
        occluded_frames = [
            f for f, meta in enumerate(manifest["frames"]) if meta["occluded"]
        ]
        assert len(occluded_frames) > 0
        transforms = {**backward.transforms, **forward.transforms}
        failed_in_occlusion = 0
        for f in occluded_frames:
            t = transforms.get(f)
            if t is not None and t.status == TrackStatus.FAILED:
                failed_in_occlusion += 1
        # 遮挡帧绝大多数应失败（允许重捕获边缘帧）
        assert failed_in_occlusion >= len(occluded_frames) * 0.7

    def test_every_failure_has_reason(self, occlusion_results):
        """每个失败帧必须给出原因枚举。"""
        forward, backward, _ = occlusion_results
        for result in (forward, backward):
            for f, t in result.transforms.items():
                if t.status == TrackStatus.FAILED:
                    assert t.failure_reason != FailureReason.NONE, f"帧 {f} 失败无原因"
                    assert t.failure_reason != FailureReason.CANCELLED

    def test_failure_reasons_in_expected_set(self, occlusion_results):
        """遮挡素材的失败原因应落在遮挡/越界/模糊集合内。"""
        forward, backward, _ = occlusion_results
        allowed = {
            FailureReason.OCCLUSION,
            FailureReason.OUT_OF_BOUNDS,
            FailureReason.BLUR,
            FailureReason.SOLVE_FAILED,
            FailureReason.INSUFFICIENT_FEATURES,
        }
        for result in (forward, backward):
            for t in result.transforms.values():
                if t.status == TrackStatus.FAILED:
                    assert t.failure_reason in allowed

    def test_visible_frames_still_accurate(self, occlusion_results):
        """可见且未遮挡的帧误差仍须达标（失败处理不污染有效段）。"""
        forward, backward, manifest = occlusion_results
        ref_corners = ts.reference_corners(manifest)
        transforms = {**backward.transforms, **forward.transforms}
        errors = []
        for f, meta in enumerate(manifest["frames"]):
            if meta["occluded"] or not meta["visible"]:
                continue
            t = transforms.get(f)
            if t is None or t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            errors.append(ts.corner_error(t.matrix_np, H_gt, ref_corners))
        errors = np.array(errors)
        assert len(errors) >= 50, "有效帧过少"
        assert errors.mean() < 1.5
        assert errors.max() < 3.0

    def test_no_fake_valid_after_occlusion(self, occlusion_results):
        """长遮挡之后的帧：要么真正重捕获（误差达标），要么诚实标记失败。
        不允许携带错误矩阵的假有效（该断言在阶段 7 重捕获实现后仍成立）。"""
        forward, backward, manifest = occlusion_results
        occluded_frames = [
            f for f, meta in enumerate(manifest["frames"]) if meta["occluded"]
        ]
        occlusion_start = min(occluded_frames)
        ref_corners = ts.reference_corners(manifest)
        for f in range(0, occlusion_start):
            t = backward.transforms.get(f)
            if t is None or t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(
                manifest["frames"][f]["homography"], dtype=np.float64
            ).reshape(3, 3)
            err = ts.corner_error(t.matrix_np, H_gt, ref_corners)
            assert err < 3.0, (
                f"帧 {f} 遮挡后假有效（四角误差 {err:.1f}px）"
            )
        # 允许全部失败（阶段 2 尚未实现长遮挡重捕获），但若有非失败帧必须准确

    def test_out_of_frames_failure_classified(self, occlusion_results):
        """卡片完全出画的帧应标记失败（越界或相关原因）。"""
        forward, backward, manifest = occlusion_results
        invisible_frames = [
            f for f, meta in enumerate(manifest["frames"]) if not meta["visible"]
        ]
        assert len(invisible_frames) > 0
        transforms = {**backward.transforms, **forward.transforms}
        failed = sum(
            1 for f in invisible_frames
            if f in transforms and transforms[f].status == TrackStatus.FAILED
        )
        assert failed >= len(invisible_frames) * 0.6


class TestFailureNeverFakesValid:
    def test_failed_transform_is_identity(self, occlusion_results):
        forward, backward, _ = occlusion_results
        for result in (forward, backward):
            for t in result.transforms.values():
                if t.status == TrackStatus.FAILED:
                    assert np.allclose(t.matrix_np, np.eye(3)), (
                        f"失败帧 {t.frame} 不应携带变换矩阵"
                    )
                    assert t.confidence == 0.0

    def test_no_transform_for_untracked_frames(self, fixture_dir):
        """帧范围之外的帧不应出现在结果中。"""
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        result = tracker.track(req)
        for f in result.transforms:
            assert req.frame_start <= f <= req.frame_end
