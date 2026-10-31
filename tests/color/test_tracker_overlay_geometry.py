"""阶段 4 测试：画布 Tracker Overlay 几何、图层顺序、命中与拖动提交。

覆盖计划 §4.1-4.4 的可测试项：
- 包含性校验（search 必须包住 pattern，否则禁止跟踪）
- 图层顺序常量（底→顶单调递增，位于蒙版之上、蒙版手柄之下）
- 路径抽样（>2000 点显示抽样但原始数据不丢，首尾保留）
- 坐标命中（当前帧变换区域/失败标记；pattern/search 区域框已
  随「跟踪复用 Mask 窗口遮罩」改造移除，区域命中必须穿透）
- 拖动三阶段（按下+拖动只更新 overlay，释放才提交一次；
  仅保留当前帧手动修正的拖动）
"""

import math

import numpy as np
import pytest

pytestmark = pytest.mark.usefixtures("qapp")

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsScene

from src.features.color.ui.tracker_overlay import (
    HANDLE_SIZE,
    HIT_TOLERANCE,
    LAYER_ORDER,
    LAYER_Z,
    PATH_MAX_DISPLAY,
    Z_HOVER,
    Z_MASK_TOP,
    Z_SEARCH,
    FeaturePointStatus,
    OverlayState,
    TrackerOverlay,
    clamp_region,
    sample_path,
    search_contains_pattern,
    transform_point,
    transform_region_corners,
)

FRAME_W, FRAME_H = 1000.0, 500.0
PATTERN = (0.4, 0.4, 0.2, 0.2)     # 场景矩形 (400,200,200,100)
SEARCH = (0.3, 0.3, 0.4, 0.4)


class FakeMouseEvent:
    """最小鼠标事件替身：仅提供 overlay 用到的接口。"""

    def __init__(self, scene_pos, button=Qt.MouseButton.LeftButton):
        self._pos = QPointF(*scene_pos)
        self._button = button
        self._accepted = False

    def scenePos(self):
        return self._pos

    def button(self):
        return self._button

    def accept(self):
        self._accepted = True

    def ignore(self):
        self._accepted = False

    def isAccepted(self):
        return self._accepted


def make_overlay(active: bool = True) -> TrackerOverlay:
    overlay = TrackerOverlay()
    scene = QGraphicsScene()
    scene.addItem(overlay)
    overlay._test_scene = scene  # 保活：防止 scene 被 GC 后 C++ 侧析构子项
    overlay.set_frame_size(FRAME_W, FRAME_H)
    overlay.set_pattern_region(PATTERN)
    overlay.set_search_region(SEARCH)
    overlay.set_overlay_active(active)
    return overlay


# ================================================================
# 纯函数：包含性 / 抽样 / 夹取 / 变换
# ================================================================

class TestPureGeometry:
    def test_search_contains_pattern_true(self):
        assert search_contains_pattern(SEARCH, PATTERN)

    def test_search_contains_pattern_false(self):
        too_small = (0.45, 0.45, 0.05, 0.05)
        assert not search_contains_pattern(too_small, PATTERN)
        # 右侧越界
        right_out = (0.3, 0.3, 0.25, 0.4)
        assert not search_contains_pattern(right_out, PATTERN)

    def test_search_contains_pattern_boundary_eps(self):
        exact = PATTERN  # 完全相等视为包含（容差内）
        assert search_contains_pattern(exact, PATTERN)
        assert not search_contains_pattern(exact, PATTERN, eps=0.0) \
            or search_contains_pattern(exact, PATTERN, eps=1e-6)

    def test_sample_path_no_change_under_limit(self):
        pts = [(float(i), 0.0) for i in range(100)]
        assert sample_path(pts) == pts

    def test_sample_path_over_limit_keeps_endpoints(self):
        pts = [(float(i), float(i) / 10.0) for i in range(PATH_MAX_DISPLAY * 3)]
        sampled = sample_path(pts)
        assert len(sampled) <= PATH_MAX_DISPLAY + 1
        assert sampled[0] == pts[0]
        assert sampled[-1] == pts[-1]

    def test_clamp_region(self):
        assert clamp_region((-0.2, -0.1, 0.5, 0.5)) == pytest.approx(
            (0.0, 0.0, 0.3, 0.4))
        assert clamp_region((0.8, 0.9, 0.5, 0.5)) == pytest.approx(
            (0.8, 0.9, 0.2, 0.1))
        assert clamp_region((-2.0, -2.0, 0.5, 0.5)) == (0.0, 0.0, 0.0, 0.0)

    def test_transform_point_translation(self):
        m = np.array([[1, 0, 0.1], [0, 1, -0.05], [0, 0, 1]], dtype=np.float64)
        x, y = transform_point(m, 0.5, 0.5)
        assert math.isclose(x, 0.6, abs_tol=1e-9)
        assert math.isclose(y, 0.45, abs_tol=1e-9)

    def test_transform_region_corners_translation(self):
        m = np.array([[1, 0, 0.1], [0, 1, 0.1], [0, 0, 1]], dtype=np.float64)
        corners = transform_region_corners(m, PATTERN)
        assert len(corners) == 4
        assert math.isclose(corners[0][0], 0.5, abs_tol=1e-9)
        assert math.isclose(corners[0][1], 0.5, abs_tol=1e-9)
        assert math.isclose(corners[2][0], 0.7, abs_tol=1e-9)
        assert math.isclose(corners[2][1], 0.7, abs_tol=1e-9)


# ================================================================
# 图层顺序与样式常量
# ================================================================

class TestLayerOrder:
    def test_layer_z_monotonic(self):
        zs = [LAYER_Z[name] for name in LAYER_ORDER]
        assert zs == sorted(zs)
        assert len(set(zs)) == len(zs)

    def test_overlay_above_mask_below_handles(self):
        # 蒙版形状顶层 z=25，蒙版手柄 z=30/31（canvas.py）
        assert Z_SEARCH > Z_MASK_TOP
        assert Z_HOVER < 30.0

    def test_overlay_zvalue(self, qapp):
        overlay = make_overlay()
        assert overlay.zValue() == Z_SEARCH

    def test_handles_ignore_transformations_and_size(self, qapp):
        overlay = make_overlay()
        flag = QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        for handle in overlay._handles:
            assert handle.flags() & flag
            rect = handle.rect()
            assert rect.width() == HANDLE_SIZE
            assert 7.0 <= HANDLE_SIZE <= 11.0

    def test_handles_hidden_region_frame_removed(self, qapp):
        # pattern/search 手柄不再显示：区域框已随「跟踪复用 Mask
        # 窗口遮罩」改造移除，跟踪区域由蒙版图层自身承担
        overlay = make_overlay(active=True)
        assert not any(h.isVisible() for h in overlay._handles)
        overlay.set_overlay_active(False)
        assert not any(h.isVisible() for h in overlay._handles)


# ================================================================
# 包含性与状态机
# ================================================================

class TestContainment:
    def test_can_start_tracking_ok(self, qapp):
        overlay = make_overlay()
        assert overlay.can_start_tracking

    def test_can_start_tracking_broken(self, qapp):
        overlay = make_overlay()
        overlay.set_search_region((0.45, 0.45, 0.05, 0.05))
        assert not overlay.can_start_tracking

    def test_can_start_tracking_missing_regions(self, qapp):
        overlay = TrackerOverlay()
        assert not overlay.can_start_tracking
        overlay.set_pattern_region(PATTERN)
        assert not overlay.can_start_tracking

    def test_state_changed_signal(self, qapp):
        overlay = make_overlay()
        seen = []
        overlay.state_changed.connect(seen.append)
        overlay.set_state(OverlayState.TRACKER_READY)
        overlay.set_state(OverlayState.TRACKER_READY)  # 重复不发射
        assert seen == [OverlayState.TRACKER_READY]


# ================================================================
# 路径数据完整性
# ================================================================

class TestPathData:
    def test_full_data_kept_display_sampled(self, qapp):
        overlay = make_overlay()
        pts = [(0.5, 0.5 + i * 1e-6) for i in range(PATH_MAX_DISPLAY * 2 + 7)]
        overlay.set_path(pts)
        assert len(overlay.path_points()) == len(pts)
        display = overlay.display_path_points()
        assert len(display) <= PATH_MAX_DISPLAY + 1
        assert display[0] == pts[0]
        assert display[-1] == pts[-1]


# ================================================================
# 命中判定
# ================================================================

class TestHitTesting:
    def test_region_frame_hits_removed(self, qapp):
        # pattern/search 区域框已移除：角点/内部一律不命中（None），
        # 鼠标事件穿透到蒙版图层，跟踪时仍可直接拖动窗口遮罩
        overlay = make_overlay()
        for pos in [(400, 200), (600, 300), (500, 250), (350, 175), (300, 150)]:
            assert overlay._hit_part(QPointF(*pos)) is None, pos

    def test_hit_current_transformed_region(self, qapp):
        overlay = make_overlay()
        m = np.array([[1, 0, 0.2], [0, 1, 0.1], [0, 0, 1]], dtype=np.float64)
        overlay.set_current_transform(m)
        # 变换后 pattern 中心 (0.7, 0.6) → 场景 (700, 300)
        assert overlay._hit_part(QPointF(700, 300)) == ("current", -1)
        # 原 pattern 区域不再命中（穿透）
        assert overlay._hit_part(QPointF(500, 250)) is None

    def test_no_hit_outside(self, qapp):
        overlay = make_overlay()
        assert overlay._hit_part(QPointF(10, 10)) is None

    def test_failed_marker_hit_and_signal(self, qapp):
        overlay = make_overlay()
        overlay.set_failed_markers({77: (0.5, 0.5)})
        assert overlay._hit_failed_marker(QPointF(500, 250)) == 77
        assert overlay._hit_failed_marker(QPointF(100, 100)) is None
        clicked = []
        overlay.failed_marker_clicked.connect(clicked.append)
        overlay.mousePressEvent(FakeMouseEvent((500, 250)))
        assert clicked == [77]


# ================================================================
# 拖动三阶段：释放才提交
# ================================================================

class TestDragCommit:
    def _collect(self, overlay):
        events = {"pattern": [], "search": [], "current": []}
        overlay.pattern_region_committed.connect(events["pattern"].append)
        overlay.search_region_committed.connect(events["search"].append)
        overlay.current_region_committed.connect(events["current"].append)
        return events

    def test_region_drag_removed_passthrough(self, qapp):
        # pattern/search 区域拖拽已随区域框移除：按下不接受事件（穿透
        # 到蒙版图层）、不改变状态、不修改区域数据、不发射提交信号
        overlay = make_overlay()
        overlay.set_state(OverlayState.TRACKER_READY)
        events = self._collect(overlay)
        press = FakeMouseEvent((500, 250))
        overlay.mousePressEvent(press)
        assert not press.isAccepted()
        assert overlay.state == OverlayState.TRACKER_READY
        overlay.mouseMoveEvent(FakeMouseEvent((550, 275)))
        overlay.mouseReleaseEvent(FakeMouseEvent((550, 275)))
        assert events["pattern"] == [] and events["search"] == []
        assert overlay._pattern == PATTERN and overlay._search == SEARCH

    def test_manual_correction_commits_current(self, qapp):
        overlay = make_overlay()
        m = np.array([[1, 0, 0.2], [0, 1, 0.1], [0, 0, 1]], dtype=np.float64)
        overlay.set_current_transform(m)
        overlay.set_state(OverlayState.TRACKED)
        events = self._collect(overlay)
        overlay.mousePressEvent(FakeMouseEvent((700, 300)))
        assert overlay.state == OverlayState.MANUAL_CORRECTION
        overlay.mouseMoveEvent(FakeMouseEvent((710, 305)))
        assert events["current"] == []
        overlay.mouseReleaseEvent(FakeMouseEvent((710, 305)))
        assert len(events["current"]) == 1
        assert overlay.state == OverlayState.TRACKED

    def test_miss_press_does_not_start_drag(self, qapp):
        overlay = make_overlay()
        events = self._collect(overlay)
        press = FakeMouseEvent((10, 10))
        overlay.mousePressEvent(press)
        assert not press.isAccepted()
        overlay.mouseMoveEvent(FakeMouseEvent((100, 100)))
        overlay.mouseReleaseEvent(FakeMouseEvent((100, 100)))
        assert events["pattern"] == [] and events["search"] == [] \
            and events["current"] == []

    def test_right_button_does_not_start_drag(self, qapp):
        overlay = make_overlay()
        press = FakeMouseEvent((500, 250), button=Qt.MouseButton.RightButton)
        overlay.mousePressEvent(press)
        assert not press.isAccepted()
        assert overlay.state != OverlayState.PATTERN_EDITING


# ================================================================
# 特征点数据
# ================================================================

class TestFeatures:
    def test_set_features_roundtrip(self, qapp):
        overlay = make_overlay()
        feats = [
            (0.1, 0.2, FeaturePointStatus.VALID),
            (0.3, 0.4, FeaturePointStatus.WEAK),
            (0.5, 0.6, FeaturePointStatus.FAILED),
        ]
        overlay.set_features(feats)
        assert overlay._features == feats
