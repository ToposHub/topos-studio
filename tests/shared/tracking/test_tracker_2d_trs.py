"""阶段 2 验收：TRS（平移+旋转+缩放）素材跟踪。"""

import numpy as np
import pytest

from src.shared.tracking.types import TrackingMode, TrackStatus

from tests.shared.tracking import tracker_support as ts

pytestmark = pytest.mark.skipif(
    not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用"
)


@pytest.fixture(scope="module")
def fixture_dir() -> str:
    return ts.fixture_dir()


@pytest.fixture(scope="module")
def trs_result(fixture_dir):
    transforms = ts.track_both_directions(
        fixture_dir, "trs", TrackingMode.TRANSLATION_ROTATION_SCALE
    )
    manifest = ts.load_manifest(fixture_dir, "trs")
    return transforms, manifest


class TestTrsAccuracy:
    def test_corner_error_within_threshold(self, trs_result):
        transforms, manifest = trs_result
        ref_corners = ts.reference_corners(manifest)
        errors = []
        for f, meta in enumerate(manifest["frames"]):
            t = transforms.get(f)
            if t is None or t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            errors.append(ts.corner_error(t.matrix_np, H_gt, ref_corners))
        errors = np.array(errors)
        assert len(errors) >= len(manifest["frames"]) * 0.95, "过多帧失败"
        assert errors.mean() < 1.5, f"平均四角误差过大: {errors.mean():.2f}px"
        assert errors.max() < 3.0, f"最大四角误差过大: {errors.max():.2f}px"

    def test_rotation_and_scale_recovered(self, trs_result):
        """估计的旋转/缩放应与 GT 分解一致。"""
        transforms, manifest = trs_result
        for f, meta in enumerate(manifest["frames"]):
            t = transforms.get(f)
            if t is None or t.status == TrackStatus.FAILED:
                continue
            assert abs(t.rotation - meta["rotation_deg"]) < 0.5, (
                f"帧 {f} 旋转偏差 {t.rotation - meta['rotation_deg']:.3f}°"
            )
            assert abs(t.scale[0] - meta["scale"]) < 0.02, (
                f"帧 {f} 缩放偏差 {t.scale[0] - meta['scale']:.4f}"
            )

    def test_translation_mode_cannot_fit_rotation(self, fixture_dir):
        """用 TRANSLATION 模式跟踪 TRS 素材：旋转帧误差应显著大于 TRS 模式，
        或置信度/状态体现不适配（模式能力边界检查）。"""
        transforms = ts.track_both_directions(
            fixture_dir, "trs", TrackingMode.TRANSLATION
        )
        manifest = ts.load_manifest(fixture_dir, "trs")
        ref_corners = ts.reference_corners(manifest)
        errors = []
        for f, meta in enumerate(manifest["frames"]):
            t = transforms.get(f)
            if t is None or t.status == TrackStatus.FAILED:
                continue
            if abs(meta["rotation_deg"]) < 1.0:
                continue
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            errors.append(ts.corner_error(t.matrix_np, H_gt, ref_corners))
        if errors:
            # 纯平移模型无法表达旋转：大旋转帧四角误差必然超过阈值
            assert max(errors) > 3.0, "TRANSLATION 模式不应能精确拟合旋转运动"


class TestAffineAndHomographyOnTrs:
    def test_affine_mode_tracks_trs(self, fixture_dir):
        transforms = ts.track_both_directions(
            fixture_dir, "trs", TrackingMode.AFFINE
        )
        manifest = ts.load_manifest(fixture_dir, "trs")
        ref_corners = ts.reference_corners(manifest)
        errors = []
        for f, meta in enumerate(manifest["frames"]):
            t = transforms.get(f)
            if t is None or t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            errors.append(ts.corner_error(t.matrix_np, H_gt, ref_corners))
        errors = np.array(errors)
        assert len(errors) >= 100
        assert errors.mean() < 2.0
