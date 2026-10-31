"""P1-COLOR-07：Scope 输入源选择（graded / source / reference）。

验收：
- 用户能明确知道 scope 分析的是哪一阶段（In: 标签 + 菜单勾选）
- 切换输入源不显示旧帧（在途帧丢弃 + 无缓存时清空显示）
- 有缓存时切回立即显示
- source/reference 模式不消费 GPU 帧
"""

import os
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest

from PyQt6.QtWidgets import QApplication

from src.features.color.ui.scope_panel import ScopePanel


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _img(seed: float = 0.5) -> np.ndarray:
    rng = np.random.default_rng(int(seed * 1000))
    return rng.random((32, 32, 3)).astype(np.float32)


class TestScopeSourceStateMachine:
    def test_switch_clears_stale_display(self, qtbot):
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.resize(500, 400)
        panel.show()
        panel.set_mode('waveform')
        panel.update_scope(_img(1))
        panel._process_pending_image()
        assert panel.waveform_widget.q_scope_image is not None

        panel.set_scope_source('source')
        assert panel.scope_source == 'source'
        assert panel.waveform_widget.q_scope_image is None, \
            "无缓存的源切换必须清空显示"

    def test_inflight_frames_from_other_source_dropped(self, qtbot):
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.set_scope_source('source')
        panel.update_scope(_img(2), source='graded')  # 在途旧源帧
        assert panel._pending_image is None

    def test_matching_source_accepted(self, qtbot):
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.set_scope_source('source')
        panel.update_scope(_img(3), source='source')
        assert panel._pending_image is not None

    def test_switch_back_uses_cache(self, qtbot):
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.resize(500, 400)
        panel.show()
        panel.set_mode('waveform')
        panel.update_scope(_img(4))
        panel._process_pending_image()
        panel.set_scope_source('source')
        panel.update_scope(_img(5), source='source')
        panel._process_pending_image()
        assert panel._source_images['source'] is not None

        panel.set_scope_source('graded')
        assert panel._pending_image is not None, "切回应立即用缓存帧"
        panel._process_pending_image()
        assert panel.waveform_widget.q_scope_image is not None

    def test_label_tracks_source(self, qtbot):
        panel = ScopePanel()
        qtbot.addWidget(panel)
        assert 'Graded' in panel.source_label.text()
        panel.set_scope_source('reference')
        assert 'Reference' in panel.source_label.text()
        panel.set_scope_source('source')
        assert 'Source' in panel.source_label.text()

    def test_invalid_source_rejected(self, qtbot):
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.set_scope_source('bogus')
        assert panel.scope_source == 'graded'

    def test_cpu_page_forced_for_non_graded(self, qtbot):
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel._gpu_display_active = True
        panel.set_scope_source('reference')
        assert panel._gpu_display_active is False
        assert panel.display_stack.currentIndex() == 0

    def test_gpu_frames_not_consumed_for_non_graded(self, qtbot):
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.set_scope_source('source')
        frame = MagicMock()
        frame.revision = panel.gpu_demand_revision
        # mode 不在 CPU-only，source 守卫应拦截
        panel._current_mode = 'waveform'
        panel.update_gpu_scope(frame)
        # _present_gpu_frame 未被调用（presenter 仍为空/未收到）
        presenter = getattr(panel, '_gpu_wf_presenter', None)
        if presenter is not None:
            assert presenter._frame_tex_id is None


class TestEngineReferenceCpuCopy:
    def test_reference_getter(self):
        from src.features.color.core.image_engine import ImageEngine
        engine = ImageEngine.__new__(ImageEngine)
        engine._reference_still_cpu = _img(6)
        assert engine.get_reference_still_cpu() is not None
        engine._reference_still_cpu = None
        assert engine.get_reference_still_cpu() is None


class TestControllerDispatch:
    def _make(self):
        from src.app.controllers.layer_controller import LayerOpsMixin
        ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
        ctrl.engine = MagicMock()
        ctrl.scope_panel = MagicMock()
        ctrl.scope_panel.scope_source = 'graded'
        return ctrl

    def test_source_mode_feeds_original(self):
        ctrl = self._make()
        ctrl.scope_panel.scope_source = 'source'
        orig = (np.random.rand(32, 32, 3) * 255).astype(np.uint8)
        ctrl.engine.img_full_original = orig
        ctrl._update_scope_panel(_img(7))
        args = ctrl.scope_panel.update_scope.call_args
        assert args.kwargs.get('source') == 'source'
        fed = args.args[0]
        assert fed.dtype == np.float32 and fed.max() <= 1.0

    def test_reference_mode_feeds_reference(self):
        ctrl = self._make()
        ctrl.scope_panel.scope_source = 'reference'
        ref = _img(8)
        ctrl.engine.get_reference_still_cpu.return_value = ref
        ctrl._update_scope_panel(_img(9))
        kwargs = ctrl.scope_panel.update_scope.call_args.kwargs
        assert kwargs.get('source') == 'reference'
        assert kwargs.get('mask') is None

    def test_reference_mode_without_still_skips(self):
        ctrl = self._make()
        ctrl.scope_panel.scope_source = 'reference'
        ctrl.engine.get_reference_still_cpu.return_value = None
        ctrl._update_scope_panel(_img(10))
        ctrl.scope_panel.update_scope.assert_not_called()

    def test_graded_mode_unchanged(self):
        ctrl = self._make()
        img = _img(11)
        ctrl.engine.get_active_layer.return_value = None
        ctrl._update_scope_panel(img)
        call = ctrl.scope_panel.update_scope.call_args
        assert call.args[0] is img
