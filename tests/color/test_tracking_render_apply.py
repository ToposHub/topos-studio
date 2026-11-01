"""跟踪结果应用（阶段 6 集成）回归测试。

覆盖本次"最后一公里"接线的渲染端核心逻辑：
- ImageEngine.get_tracked_geometry / _tracked_shape_view / refresh_tracking_masks
- _render_mask_items_recursive 对跟踪几何的消费（蒙版随帧移动）
- ColorControllerMixin._tracker_render_context 帧号回退链
- ColorControllerMixin._on_tracker_pattern_committed 写回窗口几何
"""
import numpy as np
import pytest
from weakref import WeakKeyDictionary

from src.features.color.core.image_engine import ImageEngine
from src.features.color.core.layer import Layer, MaskShape
from src.features.color.tracking.track_data import TrackingBinding
from src.features.color.tracking.tracking_evaluator import capture_base_geometry
from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.tracking.types import TrackStatus, TrackTransform, TrackingMode
from src.app.controllers.color_controller import ColorControllerMixin

pytestmark = pytest.mark.usefixtures("qapp")


def make_engine_skeleton() -> ImageEngine:
    """绕过 __init__ 的重依赖，构造只含跟踪求值所需字段的最小实例。"""
    engine = ImageEngine.__new__(ImageEngine)
    engine.tracking_store = TrackingStore()
    engine._tracking_shape_views = WeakKeyDictionary()
    from src.features.color.tracking.tracking_evaluator import TrackingEvaluator
    engine.tracking_evaluator = TrackingEvaluator(engine.tracking_store)
    engine.tracking_context_provider = None
    engine.img_full_original = np.zeros((2160, 3840, 3), dtype=np.float32)
    engine.layers = [Layer("L1")]
    engine.renderer = None
    return engine


def add_translation_binding(engine: ImageEngine, shape_id: str,
                            matrix_by_frame: dict) -> TrackingBinding:
    binding = TrackingBinding(
        binding_id="b1", clip_id="clip1", shape_id=shape_id,
        source_frame_start=0, source_frame_end=100, reference_frame=0,
        mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
        pattern_region=(0.4, 0.4, 0.2, 0.2), search_region=(0.3, 0.3, 0.4, 0.4),
    )
    engine.tracking_store.add_binding(binding)
    binding.base_geometry = None  # 由调用方按需填充
    for frame, dx in matrix_by_frame.items():
        binding.apply_result(TrackTransform(
            frame=frame,
            matrix=[1.0, 0.0, float(dx), 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            translation=(float(dx), 0.0), rotation=0.0, scale=(1.0, 1.0),
            shear=(0.0, 0.0),
            confidence=0.9, status=TrackStatus.VALID,
        ))
    return binding


class TestGetTrackedGeometry:
    def test_none_without_provider(self):
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        assert engine.get_tracked_geometry(shape) is None

    def test_identity_at_reference_frame(self):
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        shape.center = [0.5, 0.5]
        shape.size = [0.2, 0.3]
        binding = add_translation_binding(engine, shape.id, {0: 0.0, 50: 200.0})
        binding.base_geometry = capture_base_geometry(shape)
        engine.tracking_context_provider = lambda: ("clip1", 0)
        geo = engine.get_tracked_geometry(shape)
        assert geo is not None
        assert geo["center"] == pytest.approx([0.5, 0.5])

    def test_translated_at_tracked_frame(self):
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        shape.center = [0.5, 0.5]
        shape.size = [0.2, 0.3]
        binding = add_translation_binding(engine, shape.id, {0: 0.0, 50: 200.0})
        binding.base_geometry = capture_base_geometry(shape)
        engine.tracking_context_provider = lambda: ("clip1", 50)
        geo = engine.get_tracked_geometry(shape)
        # 200px / 3840px 宽度
        assert geo["center"][0] == pytest.approx(0.5 + 200.0 / 3840.0, abs=1e-6)
        assert geo["center"][1] == pytest.approx(0.5, abs=1e-6)


class TestTrackedShapeView:
    def test_proxy_isolated_from_original(self):
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        shape.center = [0.3, 0.3]
        binding = add_translation_binding(engine, shape.id, {0: 0.0, 10: 768.0})
        binding.base_geometry = capture_base_geometry(shape)
        engine.tracking_context_provider = lambda: ("clip1", 10)

        proxy = engine._tracked_shape_view(shape)
        assert proxy is not shape
        assert proxy.center[0] == pytest.approx(0.3 + 768.0 / 3840.0, abs=1e-6)
        # 原始形状不被修改
        assert shape.center == [0.3, 0.3]
        # 缓存字段清空
        assert proxy._render_cache is None and proxy._shape_cache_key is None
        # 非几何参数共享
        assert proxy.feather == shape.feather and proxy.type == shape.type

    def test_no_binding_returns_original(self):
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        engine.tracking_context_provider = lambda: ("clip1", 0)
        assert engine._tracked_shape_view(shape) is shape


class TestRenderMaskConsumesTracking:
    def test_mask_centroid_moves_with_track(self):
        """同一形状在两个跟踪帧渲染出的蒙版质心必须不同。"""
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        shape.center = [0.5, 0.5]
        shape.size = [0.1, 0.1]
        shape.feather = 0
        binding = add_translation_binding(
            engine, shape.id, {0: 0.0, 50: 768.0, 100: 1536.0})
        binding.base_geometry = capture_base_geometry(shape)

        def centroid_at(frame):
            engine.tracking_context_provider = lambda: ("clip1", frame)
            mask = engine._render_mask_items_recursive([shape], 512, 288, 3840)
            ys, xs = np.nonzero(mask > 0.5)
            assert len(xs) > 0, f"帧 {frame} 蒙版为空"
            return xs.mean() / 512.0

        c0 = centroid_at(0)
        c50 = centroid_at(50)
        c100 = centroid_at(100)
        assert c50 > c0, "帧 50 蒙版质心必须右移"
        assert c100 > c50, "帧 100 蒙版质心必须继续右移"
        # 位移比例接近 768/3840=0.2、1536/3840=0.4
        assert (c100 - c0) == pytest.approx(1536.0 / 3840.0, abs=0.02)


class TestRefreshTrackingMasks:
    def test_marks_layer_dirty_on_tracked_frame(self):
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        engine.layers[0].mask_shapes.append(shape)
        add_translation_binding(engine, shape.id, {0: 0.0, 50: 100.0})
        engine.tracking_context_provider = lambda: ("clip1", 50)
        assert engine.refresh_tracking_masks() is True
        assert engine.layers[0].mask_dirty is True

    def test_noop_without_bindings(self):
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        engine.layers[0].mask_shapes.append(shape)
        engine.tracking_context_provider = lambda: ("clip1", 50)
        assert engine.refresh_tracking_masks() is False

    def test_noop_outside_binding_range(self):
        engine = make_engine_skeleton()
        shape = MaskShape(MaskShape.CIRCLE)
        engine.layers[0].mask_shapes.append(shape)
        add_translation_binding(engine, shape.id, {0: 0.0, 50: 100.0})
        engine.tracking_context_provider = lambda: ("clip1", 200)
        assert engine.refresh_tracking_masks() is False


class TestTrackerRenderContext:
    def _controller(self, session_frame, video_frame, last_frame):
        ctrl = ColorControllerMixin.__new__(ColorControllerMixin)

        class Session:
            clip_id = "clip1"
            source_frame = session_frame

        class Manager:
            def get_current_session(self):
                return Session()

        class Video:
            current_frame = video_frame

        ctrl._color_session_manager = Manager()
        ctrl.video_controller = Video()
        ctrl._last_color_source_frame = last_frame
        return ctrl

    def test_prefers_live_video_frame(self):
        ctrl = self._controller(session_frame=5, video_frame=42, last_frame=40)
        assert ctrl._tracker_render_context() == ("clip1", 42)

    def test_falls_back_to_last_frame(self):
        ctrl = self._controller(session_frame=5, video_frame=None, last_frame=40)
        assert ctrl._tracker_render_context() == ("clip1", 40)

    def test_falls_back_to_session_frame(self):
        ctrl = self._controller(session_frame=5, video_frame=None, last_frame=None)
        assert ctrl._tracker_render_context() == ("clip1", 5)


class TestEnsureTrackerBinding:
    """回归：_ensure_tracker_binding 曾引用未定义的 params（NameError），
    导致 Tracker 面板点击跟踪必然失败。"""

    def _controller(self, shape):
        from src.features.color.tracking.tracking_store import TrackingStore

        ctrl = ColorControllerMixin.__new__(ColorControllerMixin)
        ctrl._tracker_shape_id = shape.id

        class Engine:
            tracking_store = TrackingStore()

        class Panel:
            def __init__(self):
                from unittest.mock import MagicMock
                self.graph = MagicMock()
                self.params = {
                    "mode": "translation", "quality": "standard",
                    "feature_count": 12, "search_multiplier": 2.0,
                    "min_confidence": 0.35, "failure_policy": "stop",
                }
                self.binding = None
                self.frame = None

            def get_params(self):
                return dict(self.params)

            def set_tracking_binding(self, binding):
                self.binding = binding

            def set_current_frame(self, frame):
                self.frame = frame

        ctrl.engine = Engine()
        ctrl.tracker_panel = Panel()
        ctrl._tracker_frame_context = lambda: (
            "clip1", "/tmp/media.mov", 5, 0, 100)
        return ctrl

    def test_creates_binding_with_panel_mode(self):
        shape = MaskShape(MaskShape.CIRCLE)
        shape.center = [0.5, 0.5]
        shape.size = [0.2, 0.2]
        ctrl = self._controller(shape)

        binding, context = ctrl._ensure_tracker_binding(shape)
        from src.features.color.tracking.types import TrackingMode
        assert binding.mode is TrackingMode.TRANSLATION
        assert context == ("clip1", shape.id, "/tmp/media.mov", 5, 0, 100)
        assert ctrl.tracker_panel.binding is binding
        assert ctrl.tracker_panel.frame == 5
        # Circle model sizes are radii, matching Canvas and the renderer.
        assert binding.pattern_region == (0.3, 0.3, 0.4, 0.4)

    def test_missing_clip_context_raises_valueerror(self):
        shape = MaskShape(MaskShape.CIRCLE)
        ctrl = self._controller(shape)
        ctrl._tracker_frame_context = lambda: ("", "", 0, 0, 0)
        with pytest.raises(ValueError):
            ctrl._ensure_tracker_binding(shape)


class TestPatternCommit:
    def test_commit_writes_back_shape(self, qapp):
        from unittest.mock import MagicMock
        from types import SimpleNamespace

        ctrl = ColorControllerMixin()
        shape = MaskShape(MaskShape.CIRCLE)
        layer = Layer("Pattern")
        layer.mask_shapes = [shape]
        ctrl.engine = SimpleNamespace(
            active_layer_index=0, layers=[layer], history=MagicMock(),
            img_full_original=np.zeros((720, 1280, 3), np.uint8),
            get_active_layer=lambda: layer, get_tracked_geometry=lambda _shape: None,
            tracking_store=TrackingStore())
        ctrl.canvas = MagicMock()
        ctrl.canvas.active_shape_id = shape.id
        ctrl.canvas.logical_size = None
        ctrl.mask_panel = MagicMock()
        ctrl._tracker_shape_id = shape.id
        ctrl._tracker_render_context = lambda: ("clip", 0)
        ctrl._sync_tracker_overlay = MagicMock()
        ctrl.mask_update_timer = MagicMock()
        ctrl.pending_mask_updates = {}
        ctrl.request_render = MagicMock()
        ctrl._trigger_autosave = MagicMock()

        ctrl._on_tracker_pattern_committed((0.2, 0.3, 0.1, 0.2))
        assert shape.center == pytest.approx([0.25, 0.4])
        assert shape.size == pytest.approx([0.05, 0.1])
        assert layer.mask_dirty is True
        ctrl.request_render.assert_called()
        ctrl._trigger_autosave.assert_called()

    def test_commit_ignores_group(self):
        from src.features.color.core.layer import MaskGroup
        ctrl = ColorControllerMixin.__new__(ColorControllerMixin)
        group = MaskGroup("G")

        class Layer:
            def get_mask_item_by_id(self, sid):
                return group

        class Engine:
            def get_active_layer(self):
                return Layer()

        ctrl.engine = Engine()
        ctrl._tracker_shape_id = group.id
        # 不应抛异常；MaskGroup 无 center 属性，不应被写入
        ctrl._on_tracker_pattern_committed((0.2, 0.3, 0.1, 0.2))
        assert not hasattr(group, "center")
