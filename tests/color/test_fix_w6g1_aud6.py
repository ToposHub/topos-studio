"""W6-G1（批 B）修复回归测试：plan-02 发现的五条缺陷。

对应证据文档 docs/testcoverage/2026-09-28/BUGS-agent-A{1,2,3}.md：

- BUG-A2-01：undo/redo 恢复翻转状态（image_engine._restore_state 消费
  flip 字段 + HistoryManager.undo/redo 对称交换记录翻转）
- BUG-A2-04：_render_mask_items_recursive 的未知类型防御分支可达
- BUG-A1-02：RESET_VIDEO_DENOISE_HISTORY 分支用 __dict__.get 守卫，
  与 async_renderer 自身的 __new__ 测试桩惯例一致
- BUG-A3-02：perform_quick_export 补 uint16→uint8 降位
- BUG-A3-03：sRGB 回退编码负值先钳 0（正值高光不钳）

同批已在既有套件翻正的锁定用例见：
- tests/color/test_image_engine_coverage.py::TestUndoRestoreFlipState（原 xfail）
- tests/app/test_deliver_controller_coverage.py::TestEncodeCompositeLinearToSrgb
  / TestPerformQuickExport（原"锁定旧行为"断言）
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.app.controllers.deliver_controller import DeliverControllerMixin
from src.features.color.core.history import HistoryManager
from src.features.color.core.image_engine import ImageEngine
from src.features.color.core.layer import Layer, MaskGroup, MaskShape
from src.shared.video.render_commands import RenderCommand


def _small_image(h: int = 4, w: int = 6) -> np.ndarray:
    """确定性的小 BGR uint8 测试图。"""
    return np.arange(h * w * 3, dtype=np.uint8).reshape(h, w, 3)


def _make_circle(size: float = 0.6) -> MaskShape:
    shape = MaskShape(MaskShape.CIRCLE)
    shape.center = [0.5, 0.5]
    shape.size = [size, size]
    shape.feather = 0
    return shape


# ---------------------------------------------------------------------------
# BUG-A2-01：undo/redo 恢复翻转状态
# ---------------------------------------------------------------------------

class TestFixA201FlipUndoRedo:
    def test_undo_restores_flip_flags(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine.img_full_original = engine.img_source_full.copy()
        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.flip_image(horizontal=True)
        assert engine._flip_h is True
        assert engine.undo() is True
        assert engine._flip_h is False
        assert engine._flip_v is False

    def test_redo_restores_flip_flags(self, image_engine: ImageEngine) -> None:
        """undo 记录的当前翻转态必须进重做栈，redo 换回来。"""
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine.img_full_original = engine.img_source_full.copy()
        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.flip_image(horizontal=True)
        assert engine.undo() is True
        assert engine._flip_h is False
        assert engine.redo() is True
        assert engine._flip_h is True

    def test_undo_double_flip_restores_both(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine.img_full_original = engine.img_source_full.copy()
        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.flip_image(horizontal=True)
            engine.flip_image(horizontal=False)  # vertical
        assert (engine._flip_h, engine._flip_v) == (True, True)
        assert engine.undo() is True
        # 回到第二次翻转前的状态：H 已翻、V 未翻
        assert (engine._flip_h, engine._flip_v) == (True, False)

    def test_get_flip_state_after_undo(self, image_engine: ImageEngine) -> None:
        """项目序列化读到的翻转态随撤销回滚（不再写入错误翻转态）。"""
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine.img_full_original = engine.img_source_full.copy()
        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.flip_image(horizontal=False)
        assert engine.get_flip_state() == {"flip_h": False, "flip_v": True}
        engine.undo()
        assert engine.get_flip_state() == {"flip_h": False, "flip_v": False}

    def test_non_flip_snapshot_keeps_current_flags(self, image_engine: ImageEngine) -> None:
        """未记录翻转的快照（crop/resize 等旧调用方）恢复时保持现行标志。

        回归守卫：翻转→裁切→撤销裁切，像素与标志必须一致（此前若把
        缺失字段当 False 回写会造成标志/像素错位）。
        """
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine.img_full_original = engine.img_source_full.copy()
        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.flip_image(horizontal=True)
        # 模拟 crop 类压栈：不带 flip 参数（默认 None = 不跟踪）
        engine.history.push_state(engine.img_full_original, engine.layers, 0)
        engine._flip_h = True
        assert engine.undo() is True
        assert engine._flip_h is True  # 保持，不重置


class TestFixA201HistoryManager:
    """HistoryManager 层：undo/redo 对称交换记录翻转字段。"""

    def _layers(self) -> list:
        return [Layer("L1")]

    def test_undo_records_current_flip_into_redo_stack(self) -> None:
        history = HistoryManager()
        img = _small_image(2, 2)
        history.push_state(img, self._layers(), 0, flip_h=False, flip_v=False)
        # 当前（翻转后）状态换回历史状态
        state = history.undo(img, self._layers(), 0,
                             current_flip_h=True, current_flip_v=True)
        assert state is not None
        assert state["flip_h"] is False and state["flip_v"] is False
        redo_state = history.redo_stack[0]
        assert redo_state["flip_h"] is True and redo_state["flip_v"] is True

    def test_redo_records_current_flip_into_undo_stack(self) -> None:
        history = HistoryManager()
        img = _small_image(2, 2)
        history.push_state(img, self._layers(), 0, flip_h=True)
        history.undo(img, self._layers(), 0, current_flip_h=True)
        state = history.redo(img, self._layers(), 0,
                             current_flip_h=False, current_flip_v=False)
        assert state is not None
        assert state["flip_h"] is True
        assert history.undo_stack[-1]["flip_h"] is False

    def test_push_state_flip_defaults_to_untracked(self) -> None:
        """不带 flip 参数的压栈记录 None（未跟踪），而非 False。"""
        history = HistoryManager()
        history.push_state(_small_image(2, 2), self._layers(), 0)
        assert history.undo_stack[0]["flip_h"] is None
        assert history.undo_stack[0]["flip_v"] is None


# ---------------------------------------------------------------------------
# BUG-A2-04：未知条目类型防御分支可达
# ---------------------------------------------------------------------------

class TestFixA204UnknownMaskItem:
    def test_unknown_item_without_visible_attr_skipped(
        self, image_engine: ImageEngine
    ) -> None:
        """无 visible 属性的杂项对象：旧实现在 isinstance 判定前解引用
        item.visible 抛 AttributeError，防御分支不可达。"""
        junk = SimpleNamespace()  # 故意不带 visible
        mask = image_engine._render_mask_items_recursive(
            [junk], 8, 8, 8)  # type: ignore[list-item]
        assert mask.shape == (8, 8)
        assert mask.max() == 0.0

    def test_unknown_item_with_visible_attr_skipped(
        self, image_engine: ImageEngine
    ) -> None:
        junk = SimpleNamespace(visible=True)
        mask = image_engine._render_mask_items_recursive(
            [junk], 8, 8, 8)  # type: ignore[list-item]
        assert mask.max() == 0.0

    def test_unknown_item_does_not_break_following_shapes(
        self, image_engine: ImageEngine
    ) -> None:
        """杂项条目被跳过后，同列表中的合法形状正常渲染。"""
        junk = SimpleNamespace()
        mask = image_engine._render_mask_items_recursive(
            [junk, _make_circle(0.6)], 32, 32, 32)  # type: ignore[list-item]
        assert mask[16, 16] == pytest.approx(1.0, abs=0.01)

    def test_known_types_render_unchanged(self, image_engine: ImageEngine) -> None:
        """类型过滤前移不改变 MaskShape/MaskGroup 行为。"""
        shape = _make_circle(0.6)
        shape.visible = False
        assert image_engine._render_mask_items_recursive(
            [shape], 16, 16, 16).max() == 0.0

        group = MaskGroup("G")
        group.children = [_make_circle(0.6)]
        mask = image_engine._render_mask_items_recursive([group], 32, 32, 32)
        assert mask[16, 16] == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# BUG-A1-02：__new__ 测试桩可安全分发 RESET_VIDEO_DENOISE_HISTORY
# ---------------------------------------------------------------------------

class TestFixA102StubFriendlyDispatch:
    def test_reset_denoise_history_on_bare_stub_does_not_raise(self) -> None:
        """不带 video_denoise_pipeline 属性的 __new__ 桩直接分发该命令。

        旧实现普通 getattr(self, x, default) 在未跑 QObject.__init__ 的
        实例上抛 RuntimeError（默认值兜不住），与文件自身 __dict__.get
        守卫惯例不一致。
        """
        from src.features.color.core.async_renderer import AsyncRenderer

        renderer = AsyncRenderer.__new__(AsyncRenderer)
        assert "video_denoise_pipeline" not in renderer.__dict__
        renderer._handle_command(RenderCommand.RESET_VIDEO_DENOISE_HISTORY, None)

    def test_reset_denoise_history_still_calls_pipeline(self) -> None:
        from src.features.color.core.async_renderer import AsyncRenderer

        pipeline = MagicMock()
        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.__dict__["video_denoise_pipeline"] = pipeline
        renderer._handle_command(RenderCommand.RESET_VIDEO_DENOISE_HISTORY, None)
        pipeline.reset_temporal_history.assert_called_once()


# ---------------------------------------------------------------------------
# BUG-A3-02 / BUG-A3-03：deliver_controller（mixin 直接实例化，Qt offscreen）
# ---------------------------------------------------------------------------

class _QuickExportHarness(DeliverControllerMixin):
    """仅暴露 perform_quick_export 读取的协作对象（全部 mock，无 GL / 磁盘）。

    复用 tests/app/test_deliver_controller_coverage.py 的 mixin-harness 模式。
    """

    def __init__(self) -> None:
        self.status_bar = MagicMock()
        self.engine = MagicMock()
        self.crop_panel = MagicMock()
        self.crop_panel.is_crop_active.return_value = False
        self.color_manager = MagicMock()
        self.color_manager.convert_for_export.side_effect = (
            lambda img, source_space=None: img
        )
        self.config_manager = MagicMock()
        self._config: Dict[str, Any] = {}
        self.config_manager.get.side_effect = (
            lambda key, default=None: self._config.get(key, default)
        )
        self._open_export_folder = MagicMock()


class TestFixA302QuickExportUint16:
    def test_uint16_downconverted_for_8bit_formats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """8 位显示格式：uint16 源必须 /256 降位（与 panel/batch 一致）。"""
        from src.app.controllers import deliver_controller as dc

        file_io = MagicMock()
        file_io.save_image.return_value = True
        monkeypatch.setattr(dc, "FileIO", file_io)
        monkeypatch.setattr(dc, "QMessageBox", MagicMock())

        harness = _QuickExportHarness()
        harness._export_color = MagicMock(
            return_value=np.full((2, 2, 3), 512, dtype=np.uint16))
        harness.perform_quick_export({"path": "/tmp/w6g1-out.png"})

        saved = file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint8
        np.testing.assert_array_equal(saved, np.full((2, 2, 3), 2, dtype=np.uint8))

    def test_uint16_kept_for_16bit_tiff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """16 位格式（.tif）：uint16 不做降位。"""
        from src.app.controllers import deliver_controller as dc

        file_io = MagicMock()
        file_io.save_image.return_value = True
        monkeypatch.setattr(dc, "FileIO", file_io)
        monkeypatch.setattr(dc, "QMessageBox", MagicMock())

        harness = _QuickExportHarness()
        harness._export_color = MagicMock(
            return_value=np.full((2, 2, 3), 512, dtype=np.uint16))
        harness.perform_quick_export({"path": "/tmp/w6g1-out.tif"})

        saved = file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint16


class TestFixA303SrgbFallbackNegatives:
    @staticmethod
    def _force_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
        import src.shared.color.color_transform_provider as provider

        def _boom(*args: Any, **kwargs: Any) -> np.ndarray:
            raise RuntimeError("transform unavailable")

        monkeypatch.setattr(provider, "transform_color", _boom)

    def test_negative_pixels_clamp_to_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.app.controllers.deliver_controller import (
            _encode_composite_linear_to_srgb,
        )

        self._force_fallback(monkeypatch)
        img = np.array([[[-0.5, -0.001, 0.0]]], dtype=np.float32)
        out = _encode_composite_linear_to_srgb(img)
        np.testing.assert_allclose(out[0, 0], [0.0, 0.0, 0.0], atol=1e-7)

    def test_positive_highlights_not_clamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """正值高光走幂律 OETF 上滚，不钳到 1（HDR 契约）。"""
        from src.app.controllers.deliver_controller import (
            _encode_composite_linear_to_srgb,
        )

        self._force_fallback(monkeypatch)
        img = np.array([[[2.0, 4.0, 1.0]]], dtype=np.float32)
        out = _encode_composite_linear_to_srgb(img)
        expected = 1.055 * float(np.power(2.0, 1.0 / 2.4)) - 0.055
        assert out[0, 0, 0] == pytest.approx(expected, rel=1e-5)
        assert out[0, 0, 0] > 1.0
        assert out[0, 0, 1] > out[0, 0, 0]  # 单调，不封顶

    def test_alpha_passes_through_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.app.controllers.deliver_controller import (
            _encode_composite_linear_to_srgb,
        )

        self._force_fallback(monkeypatch)
        img = np.array([[[-0.25, 0.25, 0.75, 0.6]]], dtype=np.float32)
        out = _encode_composite_linear_to_srgb(img)
        assert out.shape == (1, 1, 4)
        assert out[0, 0, 3] == pytest.approx(0.6)
        assert out is not img
