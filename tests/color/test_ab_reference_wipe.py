"""P1-COLOR-05：A/B 比较 / 参考 wipe 监看（显示层状态机）。

验收核心：
- 抓参考图 → reference 纹理上传 + 自动开 wipe + reference_is_still 置位
- 清除 → CLEAR_REFERENCE 命令 + 状态回退
- A/B toggle → split 位置 0/1 翻转；拖回中间 → 退出 A/B 态
- 全程不触碰工程状态（layers/grading 不变）
"""

import os
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest

from PyQt6.QtWidgets import QApplication

from src.shared.video.render_commands import RenderCommand


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeRenderer:
    """记录 submit 调用的假 renderer（不启动 GL 线程）。"""

    def __init__(self):
        self.commands = []

    def submit(self, cmd, data=None, **kwargs):
        self.commands.append((cmd, data))


def _make_engine():
    from src.features.color.core.image_engine import ImageEngine
    engine = ImageEngine.__new__(ImageEngine)  # 跳过重量级 __init__
    engine.renderer = _FakeRenderer()
    engine.split_view_active = False
    engine.split_position = 0.5
    engine.reference_is_still = False
    engine._ab_showing_reference = False
    engine.viewport_size = (100, 100)
    engine.render_pipeline = MagicMock()
    return engine


class TestClearReferenceCommand:
    def test_command_registered(self):
        assert RenderCommand.CLEAR_REFERENCE == "clear_reference"

    def test_renderer_handler_exists(self):
        from src.features.color.core.async_renderer import AsyncRenderer
        assert hasattr(AsyncRenderer, '_do_clear_reference')


class TestReferenceStillStateMachine:
    def test_capture_reference(self, qapp):
        engine = _make_engine()
        fake_img = np.full((4, 4, 3), 128, dtype=np.uint8)
        engine.grab_viewport_image = MagicMock(return_value=fake_img)
        assert engine.capture_reference_still() is True
        # reference 纹理上传命令
        uploads = [c for c in engine.renderer.commands
                   if c[0] == RenderCommand.UPLOAD_REFERENCE]
        assert len(uploads) == 1
        # 自动开启 wipe
        assert engine.split_view_active is True
        assert engine.reference_is_still is True
        assert engine._ab_showing_reference is False

    def test_capture_failure_returns_false(self, qapp):
        engine = _make_engine()
        engine.grab_viewport_image = MagicMock(return_value=None)
        assert engine.capture_reference_still() is False
        assert engine.reference_is_still is False

    def test_clear_reference(self, qapp):
        engine = _make_engine()
        engine.reference_is_still = True
        engine.clear_reference_still()
        clears = [c for c in engine.renderer.commands
                  if c[0] == RenderCommand.CLEAR_REFERENCE]
        assert len(clears) == 1
        assert engine.reference_is_still is False

    def test_capture_does_not_touch_grade_state(self, qapp):
        """验收：比较模式不改变工程状态。"""
        engine = _make_engine()
        engine.grab_viewport_image = MagicMock(
            return_value=np.zeros((4, 4, 3), dtype=np.uint8))
        engine.layers = [MagicMock(name='layer')]
        layers_before = list(engine.layers)
        engine.capture_reference_still()
        assert engine.layers == layers_before


class TestAbToggle:
    def test_toggle_flips_split_position(self, qapp):
        engine = _make_engine()
        engine.split_view_active = True
        # B（参考/原图）→ 全屏
        assert engine.toggle_ab_view() is True
        assert engine.split_position == 1.0
        # 回到当前调色全屏
        assert engine.toggle_ab_view() is False
        assert engine.split_position == 0.0

    def test_toggle_enables_split_if_off(self, qapp):
        engine = _make_engine()
        engine.split_view_active = False
        engine.toggle_ab_view()
        assert engine.split_view_active is True

    def test_dragging_to_middle_exits_ab(self, qapp):
        engine = _make_engine()
        engine.split_view_active = True
        engine.toggle_ab_view()
        assert engine._ab_showing_reference is True
        # 用户拖动分割线到中间
        engine.set_split_position(0.4)
        assert engine._ab_showing_reference is False
        assert engine.split_position == 0.4

    def test_ab_edges_do_not_reset(self, qapp):
        """set_split_position 直接设 0/1 边界不清 A/B 状态。"""
        engine = _make_engine()
        engine.split_view_active = True
        engine.toggle_ab_view()
        engine.set_split_position(1.0)
        assert engine._ab_showing_reference is True


class TestViewportMenuIntegration:
    def test_menu_bar_signals_and_actions(self, qapp, qtbot):
        from src.features.color.ui.canvas import ViewportMenuBar
        bar = ViewportMenuBar()
        qtbot.addWidget(bar)
        for sig in ('ab_toggle_requested', 'capture_reference_requested',
                    'clear_reference_requested'):
            assert hasattr(bar, sig)
        for act in ('ab_toggle_act', 'capture_reference_act',
                    'clear_reference_act'):
            assert hasattr(bar, act)

    def test_canvas_wires_signals_to_engine(self, qapp):
        from src.features.color.ui.canvas import Canvas
        c = Canvas()
        # Canvas 的方法存在且被连接（engine 为 None 时安全 no-op）
        assert hasattr(c, 'toggle_ab_view')
        assert hasattr(c, 'capture_reference_wipe')
        assert hasattr(c, 'clear_reference_wipe')
        c.toggle_ab_view()          # engine None → 不抛
        c.capture_reference_wipe()
        c.clear_reference_wipe()


class TestCompareModeStateMachine:
    """F4.3 · difference 对比模式状态机（wipe 参考通路复用）。"""

    def _engine(self):
        from src.features.color.core.image_engine import ImageEngine
        eng = ImageEngine.__new__(ImageEngine)
        eng.split_view_active = False
        eng.compare_mode = "split"
        eng.viewport_size = (0, 0)
        eng.split_position = 0.5
        eng._ab_showing_reference = False
        calls = []
        eng.render_pipeline = lambda: calls.append(1)
        eng.renderer = None
        return eng, calls

    def test_difference_mode_reuses_reference_path(self):
        eng, calls = self._engine()
        eng.set_compare_mode("difference")
        assert eng.compare_mode == "difference"
        assert eng.split_view_active is True  # 差异模式复用参考纹理
        assert calls  # 触发重绘

    def test_split_mode_is_default_and_normalizes_unknown(self):
        eng, _ = self._engine()
        eng.set_compare_mode("bogus")
        assert eng.compare_mode == "split"
