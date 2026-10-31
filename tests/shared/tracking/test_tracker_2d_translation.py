"""阶段 2 验收：纯平移素材跟踪（无旋转/缩放漂移）。"""

import numpy as np
import pytest

from src.shared.tracking.tracker_2d import Tracker2D
from src.shared.tracking.types import TrackingMode, TrackStatus

from tests.shared.tracking import tracker_support as ts

pytestmark = pytest.mark.skipif(
    not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用"
)


@pytest.fixture(scope="module")
def fixture_dir() -> str:
    return ts.fixture_dir()


@pytest.fixture(scope="module")
def translation_result(fixture_dir):
    """前向 + 后向跟踪平移素材（模块级缓存，避免重复解码）。"""
    tracker = Tracker2D()
    transforms = ts.track_both_directions(
        fixture_dir, "translation", TrackingMode.TRANSLATION, tracker
    )
    manifest = ts.load_manifest(fixture_dir, "translation")
    return transforms, manifest


class TestTranslationAccuracy:
    def test_all_frames_valid(self, translation_result):
        transforms, manifest = translation_result
        n = len(manifest["frames"])
        assert len(transforms) == n
        for f, t in transforms.items():
            assert t.status == TrackStatus.VALID, (
                f"帧 {f} 应为 VALID，实际 {t.status}({t.failure_reason})"
            )

    def test_corner_error_within_threshold(self, translation_result):
        transforms, manifest = translation_result
        ref_corners = ts.reference_corners(manifest)
        errors = []
        for f, meta in enumerate(manifest["frames"]):
            t = transforms[f]
            if t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            errors.append(ts.corner_error(t.matrix_np, H_gt, ref_corners))
        errors = np.array(errors)
        assert errors.mean() < 1.5, f"平均四角误差过大: {errors.mean():.2f}px"
        assert errors.max() < 3.0, f"最大四角误差过大: {errors.max():.2f}px"

    def test_no_rotation_or_scale_drift(self, translation_result):
        """纯平移素材：估计结果不得出现明显旋转/缩放分量。"""
        transforms, _ = translation_result
        for f, t in transforms.items():
            if t.status == TrackStatus.FAILED:
                continue
            assert abs(t.rotation) < 0.2, f"帧 {f} 旋转漂移 {t.rotation:.3f}°"
            assert abs(t.scale[0] - 1.0) < 0.01, f"帧 {f} sx 漂移 {t.scale[0]:.4f}"
            assert abs(t.scale[1] - 1.0) < 0.01, f"帧 {f} sy 漂移 {t.scale[1]:.4f}"

    def test_no_cumulative_drift(self, translation_result):
        """远离参考帧的误差不应系统性增长（无二次/线性漂移）。"""
        transforms, manifest = translation_result
        ref_corners = ts.reference_corners(manifest)
        ref = manifest["reference_frame"]
        near, far = [], []
        for f, meta in enumerate(manifest["frames"]):
            t = transforms[f]
            if t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            err = ts.corner_error(t.matrix_np, H_gt, ref_corners)
            (near if abs(f - ref) < 15 else far).append(err)
        assert near and far
        # 远端平均误差不应显著超过近端（允许 5 倍宽松上限）
        assert np.mean(far) < max(1.0, 5 * np.mean(near)), (
            f"误差随距离增长: near={np.mean(near):.3f} far={np.mean(far):.3f}"
        )

    def test_reference_frame_is_identity(self, fixture_dir):
        """参考帧输出必须为单位矩阵、置信度 1.0。"""
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        result = tracker.track(req)
        t = result.transforms[req.reference_frame]
        assert np.allclose(t.matrix_np, np.eye(3), atol=1e-9)
        assert t.confidence == 1.0
        assert t.status == TrackStatus.VALID

    def test_translation_matches_ground_truth(self, translation_result):
        """估计平移量与 GT 一致（允许分析分辨率量化误差）。"""
        transforms, manifest = translation_result
        for f, meta in enumerate(manifest["frames"]):
            t = transforms[f]
            if t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            assert abs(t.translation[0] - H_gt[0, 2]) < 1.5, f"帧 {f} tx 偏差过大"
            assert abs(t.translation[1] - H_gt[1, 2]) < 1.5, f"帧 {f} ty 偏差过大"


class TestTranslationInsufficientFeatures:
    def test_tiny_pattern_fails_with_reason(self, fixture_dir):
        """特征不足时必须整体标记失败并给出明确原因（不允许无解释失败）。"""
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        # 极小 pattern：检测不到足够特征点
        req = type(req)(
            **{**req.__dict__, "pattern_region": (0.01, 0.01, 0.005, 0.005)}
        )
        result = tracker.track(req)
        for f, t in result.transforms.items():
            if f == req.reference_frame:
                continue
            assert t.status == TrackStatus.FAILED
            assert t.failure_reason.value == "insufficient_features"
