"""阶段 8 验收：单应性稳定性检查（退化四边形/接近侧面停止并标记）。"""

import math

import numpy as np
import pytest

from src.shared.tracking.homography_stability import (
    HomographyDiagnostics,
    check_homography_stability,
    summarize_diagnostics,
    transform_quad,
)
from src.shared.tracking.tracker_2d import Tracker2D, TrackerConfig
from src.shared.tracking.types import (
    FailureReason,
    TrackStatus,
    TrackingMode,
)

from tests.shared.tracking import tracker_support as ts

RECT = (480.0, 250.0, 320.0, 220.0)


class TestStabilityPure:
    """纯函数稳定性判定：各类不稳定必须给出明确原因。"""

    def test_identity_is_stable(self):
        d = check_homography_stability(np.eye(3), RECT)
        assert d.stable
        assert d.convex
        assert not d.side_view
        assert d.area_ratio == pytest.approx(1.0, rel=1e-9)
        assert math.isfinite(d.condition_number)

    def test_translation_is_stable(self):
        H = np.array([[1, 0, 40], [0, 1, -18], [0, 0, 1]], dtype=np.float64)
        d = check_homography_stability(H, RECT)
        assert d.stable
        assert d.area_ratio == pytest.approx(1.0, rel=1e-9)

    def test_non_finite_matrix_rejected(self):
        H = np.array([[1, 0, 0], [0, np.nan, 0], [0, 0, 1]])
        d = check_homography_stability(H, RECT)
        assert not d.stable
        assert "非有限" in d.reason

    def test_area_explosion_rejected(self):
        """爆炸矩阵：放大 100 倍 → 面积比 1e4 超上限，不输出。"""
        H = np.diag([100.0, 100.0, 1.0])
        d = check_homography_stability(H, RECT)
        assert not d.stable
        assert d.area_ratio == pytest.approx(1e4, rel=1e-6)
        assert "爆炸" in d.reason

    def test_side_view_rejected(self):
        """接近侧面：面积比低于下限，标记 side_view。"""
        H = np.diag([0.05, 0.8, 1.0])
        d = check_homography_stability(H, RECT)
        assert not d.stable
        assert d.side_view
        assert d.area_ratio < 0.10
        assert "侧面" in d.reason

    def test_flipped_vertex_order_rejected(self):
        """顶点顺序反转（镜像）→ 退化四边形。"""
        H = np.diag([1.0, -1.0, 1.0])
        d = check_homography_stability(H, RECT)
        assert not d.stable
        assert not d.convex
        assert "退化" in d.reason

    def test_huge_condition_number_rejected(self):
        """剪切保持面积但条件数爆炸 → 数值不稳定被拒绝。"""
        H = np.array([[1, 1e6, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64)
        d = check_homography_stability(H, RECT)
        assert not d.stable
        assert d.condition_number > 1e4
        assert "条件数" in d.reason

    def test_small_pattern_rejected(self):
        d = check_homography_stability(np.eye(3), (10.0, 10.0, 0.5, 0.5))
        assert not d.stable
        assert "过小" in d.reason

    def test_transform_quad_division_by_zero(self):
        # 齐次除零（w 全为 0）→ None，不输出爆炸坐标
        H = np.array([[0, 0, 1], [0, 0, 1], [0, 0, 0]], dtype=np.float64)
        assert transform_quad(H, RECT) is None

    def test_thresholds_configurable(self):
        H = np.diag([100.0, 100.0, 1.0])
        d = check_homography_stability(H, RECT, max_area_ratio=1e6)
        assert d.stable


class TestDiagnosticsSummary:
    def test_summary_keys_and_values(self):
        class FakeDiag:
            def __init__(self, inlier, err, conf, cond):
                self.inlier_count = inlier
                self.reproj_error = err
                self.confidence = conf
                self.condition_number = cond

        diags = {
            0: FakeDiag(10, 0.5, 0.9, 20.0),
            1: FakeDiag(12, 1.5, 0.8, 40.0),
        }
        s = summarize_diagnostics(diags)
        assert s["frame_count"] == 2
        assert s["avg_inlier_count"] == pytest.approx(11.0)
        assert s["min_inlier_count"] == 10
        assert s["max_reproj_error"] == pytest.approx(1.5)
        assert s["max_condition_number"] == pytest.approx(40.0)

    def test_empty_summary(self):
        s = summarize_diagnostics({})
        assert s["frame_count"] == 0


@pytest.mark.skipif(not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用")
class TestHomographyOnPerspectiveFixture:
    """透视素材：HOMOGRAPHY 模式四角误差达标，诊断携带条件数。"""

    def test_perspective_corner_accuracy(self):
        fixture_dir = ts.fixture_dir()
        manifest = ts.load_manifest(fixture_dir, "perspective")
        tracker = Tracker2D()
        result = tracker.track(
            ts.build_request(
                fixture_dir, "perspective",
                TrackingMode.HOMOGRAPHY,
            )
        )
        ref_corners = ts.reference_corners(manifest)
        errors = []
        for frame, t in result.transforms.items():
            if t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(
                manifest["frames"][frame]["homography"], dtype=np.float64
            ).reshape(3, 3)
            errors.append(ts.corner_error(t.matrix_np, H_gt, ref_corners))
        assert errors, "透视素材应有可用帧"
        # 验收：透视素材四角平均误差达标（项目阈值）
        assert np.mean(errors) < 2.0
        assert np.max(errors) < 5.0

    def test_diagnostics_carry_condition_number(self):
        fixture_dir = ts.fixture_dir()
        tracker = Tracker2D()
        result = tracker.track(
            ts.build_request(
                fixture_dir, "perspective", TrackingMode.HOMOGRAPHY
            )
        )
        conds = [
            d.condition_number for d in result.diagnostics.values()
            if d.condition_number > 0.0
        ]
        assert conds, "HOMOGRAPHY 模式诊断应记录条件数"
        assert all(math.isfinite(c) for c in conds)

    def test_degenerate_marked_not_exploding(self):
        """极端阈值下所有帧判为不稳定 → 失败且原因 = DEGENERATE，
        绝不输出爆炸矩阵（不假有效）。
        参考帧本身也过稳定性检查，不允许例外。"""
        fixture_dir = ts.fixture_dir()
        cfg = TrackerConfig(homography_min_area_ratio=2.0)
        tracker = Tracker2D(config=cfg)
        result = tracker.track(
            ts.build_request(
                fixture_dir, "perspective", TrackingMode.HOMOGRAPHY
            )
        )
        assert result.transforms, "应逐帧产出结果"
        for frame, t in result.transforms.items():
            assert t.status == TrackStatus.FAILED, (
                f"帧 {frame} 应为失败（含参考帧）"
            )
            assert t.failure_reason == FailureReason.DEGENERATE
            assert np.all(np.isfinite(t.matrix_np))
