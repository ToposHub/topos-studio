"""P1-COLOR-22 / P2-COLOR-10：色彩监看状态栏。

验收核心：
- 画面上始终显示 source / working / display / output 链路
- 硬件/依赖不可用时显式提示（provider 降级黄色警示 + tooltip 诊断）
- 源 tag 缺失时探测一次并写回 asset.metadata 缓存（切镜不重复探测）
"""

import os
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from PyQt6.QtWidgets import QApplication

from src.features.color.ui.color_status_bar import (
    ColorMonitorStatusBar,
    format_provider_badge,
    format_source_state,
)


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class TestFormatSourceState:
    def test_hdr_tags(self):
        assert format_source_state('smpte2084', 'bt2020', 'limited') == \
            'BT.2020 PQ lim'

    def test_sdr_full(self):
        assert format_source_state('bt709', 'bt709', 'full') == \
            'BT.709 γ709 full'

    def test_defaults_rec709(self):
        assert format_source_state(None, None, None) == 'BT.709 γ709'

    def test_hlg_p3(self):
        assert format_source_state('arib-std-b67', 'smpte431', 'mpeg') == \
            'BT.P3 HLG lim'


class TestFormatProviderBadge:
    def test_ocio_ok(self):
        text, warn, tip = format_provider_badge({
            'active_provider': 'ocio', 'ocio_config_loaded': True,
            'requested_engine': 'ocio', 'fallback_reason': None})
        assert text == 'OCIO' and warn is False
        assert 'provider: ocio' in tip

    def test_builtin_fallback_warns(self):
        text, warn, tip = format_provider_badge({
            'active_provider': 'builtin', 'requested_engine': 'ocio',
            'fallback_reason': 'PyOpenColorIO 未安装',
            'ocio_config_loaded': False})
        assert text == 'builtin' and warn is True
        assert 'fallback' in tip and 'PyOpenColorIO' in tip

    def test_builtin_when_builtin_requested_no_warn(self):
        text, warn, _ = format_provider_badge({
            'active_provider': 'builtin', 'requested_engine': 'builtin',
            'fallback_reason': None, 'ocio_config_loaded': False})
        assert text == 'builtin' and warn is False

    def test_none_diag(self):
        text, warn, _ = format_provider_badge(None)
        assert warn is True


class TestStatusBarWidget:
    def test_segments_displayed(self, qtbot):
        bar = ColorMonitorStatusBar()
        qtbot.addWidget(bar)
        bar.set_source_state('smpte2084', 'bt2020', 'limited')
        bar.set_working_space('scene-linear')
        bar.set_display_space('sRGB')
        assert bar._src_label.text() == 'SRC BT.2020 PQ lim'
        assert bar._work_label.text() == 'WORK scene-linear'
        assert bar._out_label.text() == 'OUT sRGB'

    def test_provider_warning_visibility_state(self, qtbot):
        bar = ColorMonitorStatusBar()
        qtbot.addWidget(bar)
        bar.set_provider_diagnostics({
            'active_provider': 'builtin', 'requested_engine': 'ocio',
            'fallback_reason': 'x', 'ocio_config_loaded': False})
        assert 'builtin' in bar._provider_label.text()
        assert '255, 190, 80' in bar._provider_label.styleSheet()  # 黄色警示
        bar.set_provider_diagnostics({
            'active_provider': 'ocio', 'ocio_config_loaded': True,
            'requested_engine': 'ocio', 'fallback_reason': None})
        assert '130, 220, 150' in bar._provider_label.styleSheet()  # 正常绿

    def test_partial_source_update(self, qtbot):
        bar = ColorMonitorStatusBar()
        qtbot.addWidget(bar)
        bar.set_source_state('smpte2084', 'bt2020', 'limited')
        bar.set_source_state(primaries='smpte170m')
        assert bar._src_label.text() == 'SRC BT.601 PQ lim'


class TestCanvasIntegration:
    def test_update_color_monitor_shows_bar(self, qtbot):
        from src.features.color.ui.canvas import Canvas
        canvas = Canvas()
        qtbot.addWidget(canvas)
        canvas.resize(900, 500)
        assert canvas.color_status_bar.isHidden()
        canvas.update_color_monitor(
            source={'transfer': 'pq', 'primaries': 'bt2020', 'range': 'limited'})
        assert not canvas.color_status_bar.isHidden()
        assert canvas.color_status_bar._src_label.text() == 'SRC BT.2020 PQ lim'


class TestControllerHook:
    def _make(self, asset_meta=None, probe_result=None):
        """构造最小 PageController 替身（仅 hook 所需属性）。"""
        import types
        ctrl = types.SimpleNamespace()
        ctrl.canvas = MagicMock()
        ctrl.canvas.update_color_monitor = MagicMock()
        ctrl.config_manager = None
        from src.app.controllers.page_controller import PageManagementMixin
        hook = PageManagementMixin._update_color_monitor_for_asset.__get__(ctrl)
        return ctrl, hook

    def test_cached_tags_used_without_probe(self, monkeypatch):
        ctrl, hook = self._make()
        asset = types_stub_asset({'color_transfer': 'smpte2084',
                                  'color_primaries': 'bt2020',
                                  'color_range': 'limited'})
        called = []
        import src.shared.media.factory as factory_mod
        monkeypatch.setattr(
            factory_mod.MediaSourceFactory, 'probe_video_metadata',
            staticmethod(lambda p: called.append(p) or None))
        hook(asset, dict(asset.metadata))
        kwargs = ctrl.canvas.update_color_monitor.call_args.kwargs
        assert kwargs['source']['transfer'] == 'smpte2084'
        assert not called, "已有缓存 tag 不应再探测"

    def test_probe_fallback_and_cache_writeback(self, monkeypatch):
        ctrl, hook = self._make()
        asset = types_stub_asset({})
        meta = MagicMock(color_transfer='arib-std-b67',
                         color_primaries='bt2020', color_range='limited')
        import src.shared.media.factory as factory_mod
        monkeypatch.setattr(
            factory_mod.MediaSourceFactory, 'probe_video_metadata',
            staticmethod(lambda p: meta))
        hook(asset, dict(asset.metadata))
        kwargs = ctrl.canvas.update_color_monitor.call_args.kwargs
        assert kwargs['source']['transfer'] == 'arib-std-b67'
        # 写回缓存
        assert asset.metadata.get('color_transfer') == 'arib-std-b67'

    def test_missing_canvas_is_safe(self):
        import types
        from src.app.controllers.page_controller import PageManagementMixin
        ctrl = types.SimpleNamespace(canvas=None)
        PageManagementMixin._update_color_monitor_for_asset.__get__(ctrl)(
            types_stub_asset({}), {})
        # 不抛即通过


def types_stub_asset(metadata: dict):
    stub = MagicMock()
    stub.metadata = dict(metadata)
    stub.path = '/fake/video.mov'
    return stub
