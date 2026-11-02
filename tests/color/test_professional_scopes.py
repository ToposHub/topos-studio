"""P1-COLOR-06：专业 Scope 扩展验收（RGB Parade / Luma Waveform / Histogram）。

数值正确性直接读回 widget 生成的 QImage 密度数据断言：
- Parade：纯色图的迹线只出现在对应通道段
- Luma：BT.709 加权亮度的垂直位置（0%→底、100%→顶）
- Histogram：峰值 bin 位置（纯色 → 255 bin）
- ScopePanel：模式注册表/切换/CPU 分发
"""

import numpy as np
import pytest

from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

from src.shared.ui.widgets.vectorscope_widget import (
    HistogramWidget,
    LumaWaveformWidget,
    ParadeWidget,
)
from src.features.color.ui.scope_panel import MODE_ORDER, SCOPE_MODES


def _solid_bgr(h=64, w=64, b=0.0, g=0.0, r=0.0) -> np.ndarray:
    img = np.zeros((h, w, 3), dtype=np.float32)
    img[:, :, 0] = b
    img[:, :, 1] = g
    img[:, :, 2] = r
    return img


def _qimage_to_array(img: QImage) -> np.ndarray:
    """QImage(Format_RGB888) → (H, W, 3) uint8。"""
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())  # sip.voidptr 需显式给尺寸
    arr = np.frombuffer(ptr, dtype=np.uint8)
    arr = arr.reshape(img.height(), img.bytesPerLine())
    return arr[:, :img.width() * 3].reshape(img.height(), img.width(), 3)


class TestParadeWidget:
    def test_red_image_trace_only_in_r_segment(self, qtbot):
        """纯红图：迹线只在左 1/3（R 段），G/B 段为空。"""
        widget = ParadeWidget()
        qtbot.addWidget(widget)
        widget.update_parade(_solid_bgr(r=1.0))
        assert widget.q_scope_image is not None
        arr = _qimage_to_array(widget.q_scope_image)
        h, w, _ = arr.shape
        third = w // 3
        r_seg = arr[:, :third]
        g_seg = arr[:, third:2 * third]
        b_seg = arr[:, 2 * third:]
        assert r_seg[:, :, 2].max() > 50, "R 段应有迹线（红基色）"
        # 0 电平是合法信号：G/B 通道全 0 只在底部 0% 线处有迹线，
        # 其余区域（>20% 高度）必须为空
        assert g_seg[:int(h * 0.85)].max() == 0, "G 段 0% 线以上应为空"
        assert b_seg[:int(h * 0.85)].max() == 0, "B 段 0% 线以上应为空"

    def test_gray_image_all_segments(self, qtbot):
        """灰图：三段都有迹线（R/G/B 电平相同）。"""
        widget = ParadeWidget()
        qtbot.addWidget(widget)
        widget.update_parade(_solid_bgr(b=0.5, g=0.5, r=0.5))
        arr = _qimage_to_array(widget.q_scope_image)
        w = arr.shape[1]
        third = w // 3
        assert arr[:, :third, 2].max() > 50
        assert arr[:, third:2 * third, 1].max() > 50
        assert arr[:, 2 * third:, 0].max() > 50

    def test_vertical_position_midgray(self, qtbot):
        """中灰（50%）迹线位置在垂直中部附近（0%=底部，100%=顶部）。"""
        widget = ParadeWidget()
        qtbot.addWidget(widget)
        widget.update_parade(_solid_bgr(b=0.5, g=0.5, r=0.5))
        arr = _qimage_to_array(widget.q_scope_image)
        h, w, _ = arr.shape
        third = w // 3
        col = arr[:, :third, 2]
        rows = np.where(col.max(axis=1) > 50)[0]
        assert len(rows) > 0
        # -10%..120% 域：50% 值在 (1.2-0.5)/1.3 ≈ 0.538 高度处
        expected = h * (1.2 - 0.5) / 1.3
        assert abs(rows.mean() - expected) < h * 0.08


class TestLumaWaveformWidget:
    def test_white_at_top_black_at_bottom(self, qtbot):
        """纯白 → 顶部迹线；纯黑 → 底部迹线。"""
        widget = LumaWaveformWidget()
        qtbot.addWidget(widget)

        widget.update_luma_waveform(_solid_bgr(b=1, g=1, r=1))
        arr = _qimage_to_array(widget.q_scope_image)
        rows = np.where(arr[:, :, 0].max(axis=1) > 50)[0]
        assert rows.max() < arr.shape[0] * 0.2, "白应在顶部"

        widget.update_luma_waveform(_solid_bgr())
        arr = _qimage_to_array(widget.q_scope_image)
        rows = np.where(arr[:, :, 0].max(axis=1) > 50)[0]
        assert rows.min() > arr.shape[0] * 0.85, "黑应在底部"

    def test_luma_weighting_bt709(self, qtbot):
        """纯绿亮度高于纯红：绿色迹线位置更高（BT.709 G 权重 0.7152）。"""
        widget = LumaWaveformWidget()
        qtbot.addWidget(widget)

        widget.update_luma_waveform(_solid_bgr(g=1.0))
        g_rows = np.where(_qimage_to_array(widget.q_scope_image)[:, :, 0]
                          .max(axis=1) > 50)[0].mean()
        widget.update_luma_waveform(_solid_bgr(r=1.0))
        r_rows = np.where(_qimage_to_array(widget.q_scope_image)[:, :, 0]
                          .max(axis=1) > 50)[0].mean()
        assert g_rows < r_rows, "纯绿 Y(0.7152) 应高于纯红 Y(0.2126)"


class TestHistogramWidget:
    def test_solid_color_peak_bin(self, qtbot):
        """纯红图：R 通道在 bin 255 峰值，G/B 峰值在 bin 0。"""
        widget = HistogramWidget()
        qtbot.addWidget(widget)
        widget.update_histogram(_solid_bgr(r=1.0))
        assert widget._hist_rgb is not None
        r_hist, g_hist, b_hist = widget._hist_rgb
        assert int(np.argmax(r_hist)) == 255
        assert int(np.argmax(g_hist)) == 0
        assert int(np.argmax(b_hist)) == 0

    def test_gray_uniform_channels(self, qtbot):
        """灰图：三通道分布相同。"""
        widget = HistogramWidget()
        qtbot.addWidget(widget)
        widget.update_histogram(_solid_bgr(b=0.3, g=0.3, r=0.3))
        r_hist, g_hist, b_hist = widget._hist_rgb
        np.testing.assert_allclose(r_hist, g_hist, atol=1e-6)
        np.testing.assert_allclose(r_hist, b_hist, atol=1e-6)
        assert int(np.argmax(r_hist)) == int(round(0.3 * 255))

    def test_gradient_spread(self, qtbot):
        """横向渐变：计数分布覆盖多个 bin（非退化）。"""
        widget = HistogramWidget()
        qtbot.addWidget(widget)
        ramp = np.tile(np.linspace(0, 255, 256, dtype=np.float32) / 255.0,
                       (64, 1))
        img = np.stack([ramp] * 3, axis=-1)
        widget.update_histogram(img)
        nonzero = np.count_nonzero(widget._hist_rgb[0] > 0.01)
        assert nonzero > 100, "渐变应产生宽分布"


class TestScopePanelModes:
    def test_mode_registry_complete(self):
        """Keep single-mode indices stable while adding multi-view layouts."""
        assert MODE_ORDER == ['waveform', 'vectorscope', 'split',
                              'parade', 'luma', 'histogram', 'cie',
                              'falsecolor', 'quad', 'all']
        for mode in MODE_ORDER:
            assert mode in SCOPE_MODES

    def test_panel_switch_and_dispatch(self, qtbot):
        """面板切换到 parade/luma/histogram 时数据分发到对应 widget。"""
        from src.features.color.ui.scope_panel import ScopePanel

        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.show()

        panel.set_mode('parade')
        assert panel.current_mode == 'parade'
        panel.update_scope(_solid_bgr(r=0.8, g=0.4, b=0.2))
        panel._process_pending_image()
        assert panel.parade_widget.q_scope_image is not None

        panel.set_mode('luma')
        panel.update_scope(_solid_bgr(b=0.3, g=0.6, r=0.9))
        panel._process_pending_image()
        assert panel.luma_widget.q_scope_image is not None

        panel.set_mode('histogram')
        panel.update_scope(_solid_bgr(b=0.3, g=0.6, r=0.9))
        panel._process_pending_image()
        assert panel.histogram_widget._hist_rgb is not None


# ============================================================
# P1-COLOR-06（第二批）：CIE 色度图 / False Color / Zebra /
# HDR nits 标尺 / 10-bit code values
# ============================================================

from src.shared.ui.widgets.vectorscope_widget import (  # noqa: E402
    CieChromaticityWidget,
    FalseColorWidget,
    GAMUT_XY,
    build_false_color_lut,
    code_value_to_10bit,
    hlg_code_to_display_nits,
    hlg_code_to_scene,
    hlg_nits_to_code,
    linear_rgb_to_xy,
    pq_code_to_nits,
    pq_nits_to_code,
    waveform_scale_marks,
    zebra_overlay_mask,
)
from src.shared.color.color_transform_provider import (  # noqa: E402
    _linear_to_pq_np,
    _pq_to_linear_np,
)


class TestTransferHelpers:
    """scope 侧传输函数与 color_transform_provider 的 parity 契约。"""

    def test_pq_roundtrip_and_anchor(self):
        # 100 nits ≈ code 0.508（HDR 参考白锚点）
        code_100 = float(pq_nits_to_code(100.0))
        assert abs(code_100 - 0.508) < 2e-3
        back = float(pq_code_to_nits(code_100))
        assert abs(back - 100.0) < 1e-6
        # 端点：0 → 0，10000 → 1.0
        assert float(pq_nits_to_code(10000.0)) == pytest.approx(1.0, abs=1e-9)
        assert float(pq_code_to_nits(0.0)) == pytest.approx(0.0, abs=1e-9)

    def test_pq_matches_provider(self):
        """与 provider 的 numpy 实现逐点一致（防止两边漂移）。"""
        codes = np.linspace(0.0, 1.0, 257, dtype=np.float64)
        mine = pq_code_to_nits(codes)
        provider = _pq_to_linear_np(
            np.stack([codes, codes, codes, np.ones_like(codes)], axis=-1)
        )[..., 0]
        assert np.allclose(mine, provider, rtol=1e-9, atol=1e-6)
        mine_c = pq_nits_to_code(mine)
        provider_c = _linear_to_pq_np(
            np.stack([mine, mine, mine, np.ones_like(mine)], axis=-1)
        )[..., 0]
        assert np.allclose(mine_c, provider_c, rtol=1e-9, atol=1e-9)

    def test_hlg_scene_monotonic_and_anchors(self):
        codes = np.linspace(0.0, 1.0, 101)
        scene = hlg_code_to_scene(codes)
        assert np.all(np.diff(scene) >= 0), "HLG 场景光必须单调"
        # 75% → 203 nits 参考白（BT.2408 锚定显示域）
        assert abs(float(hlg_code_to_display_nits(0.75)) - 203.0) < 1e-6
        assert abs(float(hlg_nits_to_code(203.0)) - 0.75) < 1e-6
        assert float(hlg_code_to_display_nits(1.0)) == pytest.approx(1000.0)

    def test_code10_mapping(self):
        assert code_value_to_10bit(0.0) == 64
        assert code_value_to_10bit(1.0) == 940
        assert code_value_to_10bit(0.5) == 502


class TestWaveformScales:
    def test_nits_marks_pq_order(self):
        marks = waveform_scale_marks('nits', 'pq')
        values = [v for v, _ in marks]
        labels = [l for _, l in marks]
        assert values == sorted(values), "PQ nits 标尺必须自下而上递增"
        assert labels == ["100", "203", "1k", "4k", "10k nits"]
        # 100 nits 标尺位置 ≈ 0.508
        assert values[0] == pytest.approx(0.508, abs=2e-3)

    def test_nits_marks_hlg(self):
        values = [v for v, _ in waveform_scale_marks('nits', 'hlg')]
        assert values == sorted(values)
        assert 0.74 < values[1] < 0.76, "203 nits 应在 HLG 75% 处"

    def test_code10_marks(self):
        marks = waveform_scale_marks('code10')
        assert [l for _, l in marks] == ["64", "283", "502", "721", "940"]
        assert [v for v, _ in marks] == pytest.approx(
            [0.0, 0.25, 0.5, 0.75, 1.0])

    def test_percent_empty(self):
        assert waveform_scale_marks('percent') == []


class TestCieChromaticity:
    def test_srgb_primaries_xy(self):
        """线性化后的 sRGB 三基色落在标称色域顶点 ±0.005。"""
        for channel, name in ((0, 'R'), (1, 'G'), (2, 'B')):
            rgb = np.zeros((1, 1, 3), dtype=np.float32)
            rgb[0, 0, channel] = 1.0
            xy = linear_rgb_to_xy(rgb, 'bt709')
            gx, gy = GAMUT_XY['bt709'][name]
            assert abs(float(xy[0, 0, 0]) - gx) < 0.005
            assert abs(float(xy[0, 0, 1]) - gy) < 0.005

    def test_bt2020_primaries_xy(self):
        rgb = np.array([[[1.0, 0.0, 0.0]]], dtype=np.float32)
        xy = linear_rgb_to_xy(rgb, 'bt2020')
        gx, gy = GAMUT_XY['bt2020']['R']
        assert abs(float(xy[0, 0, 0]) - gx) < 0.005
        assert abs(float(xy[0, 0, 1]) - gy) < 0.005

    def test_d65_white(self):
        """等量白 → D65 白点。"""
        rgb = np.ones((1, 1, 3), dtype=np.float32)
        xy = linear_rgb_to_xy(rgb, 'bt709')
        assert abs(float(xy[0, 0, 0]) - 0.3127) < 0.005
        assert abs(float(xy[0, 0, 1]) - 0.3290) < 0.005

    def test_bt2020_gamut_contains_709(self):
        """BT.2020 三角形必须包含 BT.709 三个顶点（面积更大）。"""
        def _in_triangle(pt, tri):
            (ax, ay), (bx, by), (cx, cy) = tri
            px, py = pt
            d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
            d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
            d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
            has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
            has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
            return not (has_neg and has_pos)

        tri2020 = tuple(GAMUT_XY['bt2020'][k] for k in 'RGB')
        for k in 'RGB':
            assert _in_triangle(GAMUT_XY['bt709'][k], tri2020)

    def test_widget_density_and_mapping(self, qtbot):
        """纯红 sRGB 图 → 密度峰落在 R 顶点 xy 附近。"""
        widget = CieChromaticityWidget()
        qtbot.addWidget(widget)
        widget.resize(400, 420)
        img = np.zeros((32, 32, 3), dtype=np.float32)
        img[:, :, 0] = 1.0  # RGB 红
        widget.update_cie(img)
        widget._process_image()
        assert widget._density is not None
        peak_idx = int(np.argmax(widget._density))
        iy, ix = np.unravel_index(peak_idx, widget._density.shape)
        x = (ix + 0.5) / widget._BINS_X * 0.8
        y = 0.9 - (iy + 0.5) / widget._BINS_Y * 0.9
        assert abs(x - GAMUT_XY['bt709']['R'][0]) < 0.02
        assert abs(y - GAMUT_XY['bt709']['R'][1]) < 0.02

    def test_mask_limits_samples(self, qtbot):
        """mask 只保留限定区域的样本（左半红 / 右半绿，mask 选绿）。"""
        widget = CieChromaticityWidget()
        qtbot.addWidget(widget)
        widget.resize(400, 420)
        img = np.zeros((32, 64, 3), dtype=np.float32)
        img[:, :32, 0] = 1.0   # 左：红
        img[:, 32:, 1] = 1.0   # 右：绿
        mask = np.zeros((32, 64), dtype=np.float32)
        mask[:, 32:] = 1.0
        widget.update_cie(img, mask)
        widget._process_image()
        peak_idx = int(np.argmax(widget._density))
        iy, ix = np.unravel_index(peak_idx, widget._density.shape)
        x = (ix + 0.5) / widget._BINS_X * 0.8
        y = 0.9 - (iy + 0.5) / widget._BINS_Y * 0.9
        assert abs(x - GAMUT_XY['bt709']['G'][0]) < 0.02
        assert abs(y - GAMUT_XY['bt709']['G'][1]) < 0.02


class TestFalseColor:
    def test_sdr_lut_bands(self):
        lut = build_false_color_lut('sdr')
        assert lut.shape == (1024, 3) and lut.dtype == np.uint8
        # 50% IRE → 绿档 (0,255,0)（索引 512 处于 0.5..0.6 档）
        assert tuple(lut[512]) == (0, 255, 0)
        # 5% → 蓝系（G=R=0，B 高）
        assert lut[51, 0] == 45 and lut[51, 2] == 140
        # 过曝（>100%）→ 白
        assert tuple(lut[1023]) == (255, 255, 255)

    def test_hdr_lut_uses_pq_levels(self):
        lut_pq = build_false_color_lut('hdr', 'pq')

        def _band(nits):
            code = float(pq_nits_to_code(nits))
            return tuple(lut_pq[int(round(code * 1023))])

        # 档位中部取值（避免边界量化抖动）：
        # 150 nits → 绿（100-200），600 → 青（400-1000），
        # 1500 → 黄（1000-2000），3000 → 橙（2000-4000），6000 → 红
        assert _band(150.0) == (0, 255, 0)
        assert _band(600.0) == (0, 255, 255)
        assert _band(1500.0) == (255, 255, 0)
        assert _band(3000.0) == (255, 140, 0)
        assert _band(6000.0) == (255, 0, 0)
        # 满码值（10000 nits 峰值以上）→ 白
        assert tuple(lut_pq[1023]) == (255, 255, 255)
        # 100 nits 以下：70 nits → 蓝（50 是档位边界，量化会抖动）
        assert _band(70.0) == (0, 0, 255)

    def test_false_color_widget_sdr_render(self, qtbot):
        widget = FalseColorWidget()
        qtbot.addWidget(widget)
        img = np.full((16, 16, 3), 0.55, dtype=np.float32)  # 55% 灰 → 绿档
        widget.update_false_color(img)
        arr = _qimage_to_array(widget._display_image)
        # 中心像素应为绿色伪彩
        c = arr[8, 8]
        assert c[1] > 200 and c[0] < 30 and c[2] < 30

    def test_zebra_mode(self, qtbot):
        widget = FalseColorWidget()
        qtbot.addWidget(widget)
        widget.set_mode(FalseColorWidget.MODE_ZEBRA)
        widget.set_zebra_threshold(1.0)
        img = np.zeros((16, 16, 3), dtype=np.float32)
        img[:, :8] = 0.5    # 左半：中灰（不触发）
        img[:, 8:] = 1.05   # 右半：过曝（触发）
        widget.update_false_color(img)
        arr = _qimage_to_array(widget._display_image)
        # 左半没有任何纯白条纹（灰度值 < 255）
        assert arr[:, :8].max() < 255 or arr[:, :8, :].max() < 250
        # 右半存在纯白条纹（255,255,255）与暗背景交替
        right = arr[:, 8:]
        assert right.reshape(-1, 3).min() < 200, "条纹应有暗部"
        assert right.reshape(-1, 3).max() == 255, "条纹应有纯白"

    def test_zebra_mask_logic(self):
        # 阈值以下永不触发
        m_off = zebra_overlay_mask(np.full((8, 8), 0.5, dtype=np.float32), 1.0)
        assert m_off.shape == (8, 8) and m_off.dtype == bool
        assert not m_off.any()
        # 全部过曝时：条纹有明有暗（棋盘），行内相邻块交替
        m = zebra_overlay_mask(np.full((16, 16), 1.05, dtype=np.float32),
                               1.0, tile=4)
        assert m.any() and not m.all(), "条纹必须是交替图案"
        assert bool(m[0, 0]) != bool(m[0, 5]), "同一行相邻 tile 应交替"
        assert bool(m[0, 0]) == bool(m[1, 0]), "同一 tile 列内应同色"
        # 部分过曝：只有 over 区域出现条纹
        luma = np.full((16, 16), 0.4, dtype=np.float32)
        luma[:, 8:] = 1.1
        m_part = zebra_overlay_mask(luma, 1.0, tile=4)
        assert not m_part[:, :8].any()
        assert m_part[:, 8:].any()


class TestScopePanelColorState:
    def test_color_state_propagates(self, qtbot):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.resize(500, 400)
        panel.set_color_state('pq', 'bt2020')
        assert panel._scope_transfer == 'pq'
        assert panel.cie_widget._transfer == 'pq'
        assert panel.cie_widget._primaries == 'bt2020'
        assert panel.falsecolor_widget._transfer == 'pq'
        assert panel.waveform_widget._transfer == 'pq'

    def test_waveform_scale_switch(self, qtbot):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel._set_waveform_scale('nits', 'pq')
        assert panel.waveform_widget._scale_mode == 'nits'
        assert panel._scope_transfer == 'pq'
        panel._set_waveform_scale('code10')
        assert panel.waveform_widget._scale_mode == 'code10'
        assert panel.split_waveform._scale_mode == 'code10'

    def test_invalid_inputs_rejected(self, qtbot):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.set_color_state('weird', 'nope')
        assert panel._scope_transfer == 'srgb'
        assert panel._scope_primaries == 'bt709'
        panel._set_waveform_scale('bogus')
        assert panel._waveform_scale == 'percent'

    def test_new_modes_registered(self):
        assert 'cie' in MODE_ORDER and 'falsecolor' in MODE_ORDER
        assert SCOPE_MODES['cie']['widget_class'] is CieChromaticityWidget
        assert SCOPE_MODES['falsecolor']['widget_class'] is FalseColorWidget


class TestGpuPresenterScaleModes:
    """GPU 波形 presenter 的标尺模式状态机（绘制本身需要真实 GL）。"""

    def _make(self, qtbot):
        from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget
        widget = GpuScopeWidget(preserve_aspect=False, flip_y=False)
        qtbot.addWidget(widget)
        return widget

    def test_scale_mode_state(self, qtbot):
        widget = self._make(qtbot)
        assert widget._scale_mode == 'code10'
        widget.set_scale_mode('nits', 'pq')
        assert widget._scale_mode == 'nits' and widget._transfer == 'pq'
        widget.set_scale_mode('code10_video')
        assert widget._scale_mode == 'code10_video'
        # 非法模式被拒绝
        widget.set_scale_mode('bogus')
        assert widget._scale_mode == 'code10_video'
        # 重复设置不变（不触发重绘）
        widget.set_scale_mode('code10_video')
        assert widget._scale_mode == 'code10_video'


# ================================================================
# M19.3b · 测量域角标（域/采样/单位三要素 + tooltip 完整声明）
# ================================================================

class TestMeasurementDomainBadge:
    def test_badge_default_fullframe_display_domain(self, qtbot):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel()
        qtbot.addWidget(panel)
        text = panel.domain_badge.text()
        assert "显示域" in text and "全帧" in text and "0–1" in text
        # tooltip 携带完整声明（差一层显示变换/采样通路/几何）
        tip = panel.domain_badge.toolTip()
        assert "显示变换" in tip and "全帧回读" in tip and "极坐标" in tip

    def test_badge_reflects_downsampling_bounds(self, qtbot):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel.set_cpu_analysis_bounds(1920, 1080)
        text = panel.domain_badge.text()
        assert "下采样" in text and "1920" in text
        panel.set_cpu_analysis_bounds(None, None)
        assert "全帧" in panel.domain_badge.text()

    def test_badge_unit_follows_waveform_scale(self, qtbot):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel()
        qtbot.addWidget(panel)
        panel._set_waveform_scale("code10", "srgb")
        assert "64–940" in panel.domain_badge.text()
        panel._set_waveform_scale("percent", "srgb")
        assert "0–1" in panel.domain_badge.text()
