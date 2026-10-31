"""批 EF-14（src/shared/ui，13 条 Suggestion）修复验证。

覆盖：
- AUD-37612：横向/compact 电平条渐变在 _update_layout 缓存，paint 不再每帧新建
- AUD-37805：PresetDialog 读取/保存失败不再静默（用户可见错误 + 留痕）
- AUD-37819：频响曲线按 (EQ 参数, 采样率) 缓存，重绘不重算 scipy 频响
- AUD-38007：PlaybackHUDModel 样本列表有界（deque maxlen），_history 被写入并封顶
- AUD-38009：模块 docstring 示例使用 PlaybackHUDModel（面板 .model）
- AUD-38100：VideoControlPanel 文档信号契约与 pyqtSignal 声明一致
- AUD-38201：CIE 光谱轨迹/色温曲线 xy 域折线预计算缓存
- AUD-38209：ColorWarper._padding 构造即初始化（35），命中测试与绘制一致
- AUD-38219：CurvesWidget 绘制采样按通道缓存，未脏通道不重算
- AUD-38309：lut_gallery 顶层不再 import cv2
- AUD-38408：热图静态基底按 (h, w) 缓存复用
- AUD-38412：FalseColor LUT/图例条按 (mode, transfer) 缓存
- AUD-38417：程序化 setChecked 经状态锁辅助不再重发 mask_toggled/split_toggled
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox


# ---------------------------------------------------------------------------
# AUD-37612: AudioLevelMeter 渐变缓存
# ---------------------------------------------------------------------------

class TestAud37612GradientCache:
    def test_horizontal_gradients_cached_on_layout(self, qapp):
        """横向模式下 _update_layout 预构建 hot/warm 两支渐变。"""
        from src.shared.ui.audio_mixer.audio_level_meter import (
            AudioLevelMeter,
        )
        meter = AudioLevelMeter(orientation=Qt.Orientation.Horizontal)
        meter.resize(300, 80)
        meter._update_layout()

        assert set(meter._cached_horizontal_gradients) == {'hot', 'warm'}
        # 端点覆盖整条轨道（与 A8 垂直缓存同语义）
        for g in meter._cached_horizontal_gradients.values():
            assert g.stops()  # 已设置色标

    def test_compact_gradient_cached(self, qapp):
        """compact 渐变在布局时缓存且色标齐备。"""
        from src.shared.ui.audio_mixer.audio_level_meter import (
            AudioLevelMeter,
        )
        meter = AudioLevelMeter(compact_mode=True)
        meter.resize(120, 24)
        meter._update_layout()
        assert meter._cached_compact_gradient is not None
        assert len(meter._cached_compact_gradient.stops()) == 4

    def test_vertical_mode_clears_horizontal_cache(self, qapp):
        """垂直模式下横向缓存被清空（不残留旧布局对象）。"""
        from src.shared.ui.audio_mixer.audio_level_meter import (
            AudioLevelMeter,
        )
        meter = AudioLevelMeter(orientation=Qt.Orientation.Horizontal)
        meter.resize(300, 80)
        meter._update_layout()
        meter._orientation = Qt.Orientation.Vertical
        meter._update_layout()
        assert meter._cached_horizontal_gradients == {}


# ---------------------------------------------------------------------------
# AUD-37805: PresetDialog 静默失败修复
# ---------------------------------------------------------------------------

class TestAud37805PresetDialogErrors:
    def test_load_failure_shows_warning_not_crash(self, qapp, monkeypatch):
        """预设文件读取失败 → 列表降级为空 + 警告，不抛异常。"""
        from src.shared.ui.audio_mixer.preset_dialog import PresetDialog
        import src.shared.ui.audio_mixer.preset_dialog as pd_mod

        monkeypatch.setattr(
            pd_mod, 'load_presets',
            lambda: (_ for _ in ()).throw(OSError('disk error')))
        warned = []
        # QMessageBox.warning 是模态调用，测试中记录调用而非弹窗
        monkeypatch.setattr(
            pd_mod.QMessageBox, 'warning',
            lambda *a, **k: warned.append(a))
        dlg = PresetDialog('t1', 'Track 1')
        dlg._refresh_list()  # 不应抛出
        assert warned, '读取失败必须给出用户可见警告'
        assert dlg._list.count() == 0

    def test_save_failure_shows_critical_and_keeps_name(self, qapp, monkeypatch):
        """保存落盘失败 → 用户可见错误，输入与列表不清空。"""
        from src.shared.ui.audio_mixer.preset_dialog import PresetDialog
        import src.shared.ui.audio_mixer.preset_dialog as pd_mod

        from src.features.edit.core.mixer_preset import ChannelStripPreset

        source = ChannelStripPreset(name='src')
        dlg = PresetDialog('t1', 'Track 1', current_preset_source=source)
        dlg._name_edit.setText('my preset')

        calls = []

        def fail_add(preset):
            calls.append(preset)
            raise OSError('permission denied')

        warned = []
        monkeypatch.setattr(pd_mod, 'add_preset', fail_add)
        monkeypatch.setattr(
            pd_mod.QMessageBox, 'critical',
            lambda *a, **k: warned.append(a))

        dlg._on_save_clicked()

        assert warned, '保存失败必须给出用户可见错误'
        assert dlg._name_edit.text() == 'my preset'  # 失败不清空输入

    def test_save_success_clears_name(self, qapp, monkeypatch):
        """保存成功路径保持原行为：清空输入并刷新列表。"""
        from src.shared.ui.audio_mixer.preset_dialog import PresetDialog
        import src.shared.ui.audio_mixer.preset_dialog as pd_mod

        from src.features.edit.core.mixer_preset import ChannelStripPreset

        source = ChannelStripPreset(name='src')
        dlg = PresetDialog('t1', 'Track 1', current_preset_source=source)
        dlg._name_edit.setText('ok preset')

        monkeypatch.setattr(pd_mod, 'add_preset', lambda p: None)
        monkeypatch.setattr(pd_mod.PresetDialog, '_refresh_list', lambda self: None)
        dlg._on_save_clicked()
        assert dlg._name_edit.text() == ''


# ---------------------------------------------------------------------------
# AUD-37819: 频响曲线缓存
# ---------------------------------------------------------------------------

class TestAud37819EqResponseCache:
    def _widget(self, qapp):
        from src.shared.ui.audio_mixer.strip_eq_dialog import (
            FrequencyResponseWidget,
        )
        from src.features.edit.core.timeline_v2 import StripEQParams
        w = FrequencyResponseWidget()
        w.resize(400, 180)
        params = StripEQParams(mode='3-band', bands=(1.0, -2.0, 3.0), gain=0.0)
        return w, params

    def test_paint_reuses_cached_response(self, qapp):
        """参数不变时连续两次重绘只计算一次频响。"""
        import src.shared.ui.audio_mixer.strip_eq_dialog as eq_mod
        w, params = self._widget(qapp)
        w.set_params(params, 48000.0)

        calls = []
        real = eq_mod.compute_eq_response

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with patch.object(eq_mod, 'compute_eq_response', counting):
            w._params = params  # set_params 已作废缓存
            w._resp_cache_key = None
            w.paintEvent(type('E', (), {'rect': lambda self: w.rect()})())
            assert len(calls) == 1
            w.paintEvent(type('E', (), {'rect': lambda self: w.rect()})())
            assert len(calls) == 1, '第二次重绘不应重算频响'

    def test_param_change_recomputes(self, qapp):
        """参数变化后重算。"""
        import src.shared.ui.audio_mixer.strip_eq_dialog as eq_mod
        from src.features.edit.core.timeline_v2 import StripEQParams
        w, params = self._widget(qapp)
        w.set_params(params, 48000.0)

        calls = []
        real = eq_mod.compute_eq_response

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with patch.object(eq_mod, 'compute_eq_response', counting):
            w.paintEvent(type('E', (), {'rect': lambda self: w.rect()})())
            new_params = StripEQParams(
                mode='3-band', bands=(5.0, -2.0, 3.0), gain=0.0)
            w.set_params(new_params, 48000.0)
            w.paintEvent(type('E', (), {'rect': lambda self: w.rect()})())
            assert len(calls) == 2, '参数变化必须重算'


# ---------------------------------------------------------------------------
# AUD-38007 / AUD-38009: PlaybackHUDModel
# ---------------------------------------------------------------------------

class TestAud38007HudModelBounds:
    def test_metric_lists_capped(self, qapp):
        """样本超过 _max_history 后自动丢弃最旧值。"""
        from src.shared.ui.playback_hud import PlaybackHUDModel
        model = PlaybackHUDModel()
        cap = model._max_history
        for i in range(cap + 200):
            model.record_sample(decode_ms=float(i))
        assert len(model._decode_times) == cap
        assert model._decode_times[-1] == float(cap + 199)
        assert model._decode_times[0] == float(200), '最旧样本应被丢弃'

    def test_history_populated_and_capped(self, qapp, tmp_path):
        """record_sample 写入 _history（导出用）并封顶。"""
        import json as _json
        from src.shared.ui.playback_hud import PlaybackHUDModel
        model = PlaybackHUDModel()
        cap = model._max_history
        for i in range(cap + 100):
            model.record_sample(render_ms=float(i), queue_depth=i)
        assert len(model._history) == cap
        last = model._history[-1]
        assert last['render_ms'] == float(cap + 99)
        assert last['queue_depth'] == cap + 99
        assert 'timestamp' in last
        # 导出日志包含最近 100 条历史（export_log 挂接 _history）
        out = tmp_path / 'hud_export.json'
        path = model.export_log(str(out))
        assert path
        data = _json.loads(out.read_text(encoding='utf-8'))
        assert len(data['history']) == 100


class TestAud38009DocstringExample:
    def test_module_docstring_uses_model_correctly(self):
        import src.shared.ui.playback_hud as m
        doc = m.__doc__ or ''
        assert 'PlaybackHUDPanel(hud_panel)' not in doc
        assert 'hud_panel.model' in doc


# ---------------------------------------------------------------------------
# AUD-38100: VideoControlPanel 信号契约文档
# ---------------------------------------------------------------------------

class TestAud38100SignalDocs:
    def test_docstring_matches_declared_signals(self):
        from src.shared.ui.video_control_panel import VideoControlPanel
        doc = VideoControlPanel.__doc__ or ''
        # 文档不再声称不存在的 frame_changed
        assert 'frame_changed' not in doc
        # 实际声明全部见于文档
        for sig in ('seek_requested', 'play_toggled', 'next_frame',
                    'prev_frame', 'first_frame', 'last_frame'):
            assert sig in doc

    def test_frame_changed_signal_absent(self):
        from src.shared.ui.video_control_panel import VideoControlPanel
        assert not hasattr(VideoControlPanel, 'frame_changed')


# ---------------------------------------------------------------------------
# AUD-38201: CIE 静态几何缓存
# ---------------------------------------------------------------------------

class TestAud38201CiePolylines:
    def test_locus_polyline_cached(self, qapp):
        """光谱轨迹折线只计算一次，重复调用返回同一对象。"""
        from src.shared.ui.widgets.cie_scope_widget import CieGpuScopeWidget
        w = CieGpuScopeWidget()
        w.resize(300, 300)
        first = w._get_locus_polyline()
        assert first, '折线不应为空'
        assert w._get_locus_polyline() is first

    def test_ct_polyline_cached_per_kind(self, qapp):
        """色温曲线按类型缓存，切换类型后重算。"""
        from src.shared.ui.widgets.cie_scope_widget import CieGpuScopeWidget
        w = CieGpuScopeWidget()
        w.resize(300, 300)
        day = w._get_ct_polyline('daylight')
        assert day
        assert w._get_ct_polyline('daylight') is day
        bb = w._get_ct_polyline('blackbody')
        assert bb is not day
        assert w._get_ct_polyline('daylight') is day

    def test_locus_covers_all_segments(self, qapp):
        """折线样本数 = 段数 × 每段采样数（与原实现一致的密度）。"""
        from src.shared.ui.widgets.cie_scope_widget import CieGpuScopeWidget
        from src.shared.ui.widgets.vectorscope_widget import CIE_SPECTRAL_LOCUS
        w = CieGpuScopeWidget()
        n_seg = len(CIE_SPECTRAL_LOCUS) - 1
        polyline = w._get_locus_polyline()
        assert len(polyline) == n_seg * 12


# ---------------------------------------------------------------------------
# AUD-38209: ColorWarper padding 一致性
# ---------------------------------------------------------------------------

class TestAud38209Padding:
    def test_padding_initialized_in_constructor(self, qapp):
        from src.shared.ui.widgets.color_warper import ColorWarper
        w = ColorWarper()
        assert w._padding == 35
        # 首帧前命中测试即可用（此前 fallback 30 与绘制 35 不一致）
        from PyQt6.QtCore import QPointF
        assert w._get_point_at(QPointF(-1000, -1000)) is None

    def test_no_stale_fallback(self):
        src = Path('src/shared/ui/widgets/color_warper.py').read_text(
            encoding='utf-8')
        assert "getattr(self, '_padding', 30)" not in src


# ---------------------------------------------------------------------------
# AUD-38219: CurvesWidget 通道曲线缓存
# ---------------------------------------------------------------------------

class TestAud38219CurveCache:
    def test_unchanged_points_reuse_cached_values(self, qapp):
        from src.shared.ui.widgets.curves_widget import CurvesWidget
        w = CurvesWidget()
        pts = w.curves['M']
        v1 = w._cached_curve_values('M', pts)
        v2 = w._cached_curve_values('M', pts)
        assert v1 is v2

    def test_moved_point_recomputes(self, qapp):
        from src.shared.ui.widgets.curves_widget import CurvesWidget
        from PyQt6.QtCore import QPointF
        w = CurvesWidget()
        pts = list(w.curves['M'])
        v1 = w._cached_curve_values('M', pts)
        pts[1] = QPointF(pts[1].x(), pts[1].y() + 40)
        v2 = w._cached_curve_values('M', pts)
        assert v1 is not v2
        assert not np.allclose(v1, v2)


# ---------------------------------------------------------------------------
# AUD-38309: lut_gallery 不再顶层导入 cv2
# ---------------------------------------------------------------------------

class TestAud38309NoCv2Import:
    def test_no_top_level_cv2(self):
        src = Path('src/shared/ui/widgets/lut_gallery.py').read_text(
            encoding='utf-8')
        for line in src.splitlines():
            assert line.strip() != 'import cv2', 'lut_gallery 顶层不应导入 cv2'


# ---------------------------------------------------------------------------
# AUD-38408: 热图静态基底缓存
# ---------------------------------------------------------------------------

class TestAud38408HeatmapBaseCache:
    def _density(self, h=800, w=800):
        rng = np.random.default_rng(7)
        d = rng.integers(0, 50, size=(h, w)).astype(np.float64)
        d[0, 0] = 0
        return d

    def test_base_cache_reused_for_same_shape(self, qapp):
        from src.shared.ui.widgets.vectorscope_widget import VectorscopeWidget
        w = VectorscopeWidget()
        d = self._density()
        img1 = w._create_heatmap_qimage(d)
        cache1 = w._heatmap_base_cache
        assert cache1 is not None
        img2 = w._create_heatmap_qimage(d)
        assert w._heatmap_base_cache is cache1, '同尺寸应复用基底缓存'
        assert not img1.isNull() and not img2.isNull()

    def test_base_cache_invalidated_for_new_shape(self, qapp):
        from src.shared.ui.widgets.vectorscope_widget import VectorscopeWidget
        w = VectorscopeWidget()
        w._create_heatmap_qimage(self._density(800, 800))
        w._create_heatmap_qimage(self._density(400, 400))
        assert w._heatmap_base_cache[0] == (400, 400)


# ---------------------------------------------------------------------------
# AUD-38412: FalseColor LUT / 图例缓存
# ---------------------------------------------------------------------------

class TestAud38412FalseColorCache:
    def test_lut_cached_per_mode_transfer(self, qapp):
        from src.shared.ui.widgets.vectorscope_widget import FalseColorWidget
        w = FalseColorWidget()
        lut1 = w._get_false_color_lut()
        assert w._get_false_color_lut() is lut1
        w.set_mode(FalseColorWidget.MODE_HDR)
        lut2 = w._get_false_color_lut()
        assert lut2 is not lut1

    def test_transfer_change_invalidates(self, qapp):
        from src.shared.ui.widgets.vectorscope_widget import FalseColorWidget
        w = FalseColorWidget()
        lut1 = w._get_false_color_lut()
        w.set_color_state('hlg')
        assert w._fc_lut_cache is None, 'transfer 变化必须作废缓存'
        lut2 = w._get_false_color_lut()
        assert lut2 is not lut1

    def test_legend_strip_cached(self, qapp):
        from src.shared.ui.widgets.vectorscope_widget import FalseColorWidget
        from PyQt6.QtGui import QImage
        w = FalseColorWidget()
        w._display_image = QImage(16, 16, QImage.Format.Format_RGB888)
        w.resize(320, 240)
        w.paintEvent(type('E', (), {'rect': lambda self: w.rect()})())
        strip1 = w._fc_legend_strip
        assert strip1 is not None
        w.paintEvent(type('E', (), {'rect': lambda self: w.rect()})())
        assert w._fc_legend_strip is strip1


# ---------------------------------------------------------------------------
# AUD-38417: 工具栏程序化切换守卫
# ---------------------------------------------------------------------------

class _SpyToolbar:
    """收集 mask_toggled/split_toggled 发射的探针。"""


class TestAud38417ProgrammaticToggle:
    def _make_toolbar(self, qapp):
        from src.shared.ui.widgets.viewport_toolbar_base import (
            ViewportToolBarBase,
        )
        tb = ViewportToolBarBase('ef14TestBar')
        # 工厂方法按需创建按钮
        tb._make_mask_btn()
        tb._make_split_btn()
        return tb

    def test_programmatic_set_does_not_reemit(self, qapp):
        tb = self._make_toolbar(qapp)
        emitted = []
        tb.mask_toggled.connect(lambda checked: emitted.append(checked))

        tb._set_checked_programmatic(tb.mask_btn, True)
        assert tb.mask_btn.isChecked()
        assert emitted == [], '程序化切换不得重发 mask_toggled'

    def test_user_toggle_still_emits(self, qapp):
        tb = self._make_toolbar(qapp)
        emitted = []
        tb.mask_toggled.connect(lambda checked: emitted.append(checked))

        tb.mask_btn.setChecked(True)  # 用户路径（无状态锁）
        assert emitted == [True]

    def test_split_programmatic_set_does_not_reemit(self, qapp):
        tb = self._make_toolbar(qapp)
        emitted = []
        tb.split_toggled.connect(lambda checked: emitted.append(checked))

        tb._set_checked_programmatic(tb.split_btn, True)
        tb._set_checked_programmatic(tb.split_btn, True)  # 幂等：已选中直接返回
        assert emitted == []
        assert tb.split_btn.isChecked()

    def test_programmatic_toggle_lock_released(self, qapp):
        tb = self._make_toolbar(qapp)
        tb._set_checked_programmatic(tb.mask_btn, True)
        assert tb._programmatic_toggle is False, '状态锁必须复位'
