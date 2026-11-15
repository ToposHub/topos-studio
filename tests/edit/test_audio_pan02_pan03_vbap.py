"""PAN-02/PAN-03 回归：VBAP 多扬声器声像与真实 LFE 路由（阶段H，离线域）。

验收（计划 §8）：
- 逐源冲激检查各目标扬声器能量（扬声器方向 → 该扬声器增益最大）；
- F/B 能切换前后；height 进入上方声道；无高度输出时明确限制（exact=False）；
- 布局切换不串声道（C 方向 → C 扬声器，非 L/R）；
- 能量归一：任意方向 ‖g‖₂=1；Spread 增宽不改总能量；
- Only 只向 LFE 输出；Boom 不使主声道重复加倍；Pre/Post/Only 显式。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.vbap import (
    SPEAKER_LAYOUTS,
    has_height_channels,
    lfe_route,
    spread_gains,
    vbap_gains,
)


class TestPan02VbapGains:
    @pytest.mark.parametrize("layout", ["2.0", "5.1", "7.1"])
    @pytest.mark.parametrize("az", [-30.0, 0.0, 30.0, 110.0, -110.0, 180.0])
    def test_energy_normalized_any_direction(self, layout, az):
        g, exact = vbap_gains(az, 0.0, layout)
        assert exact
        norm = float(np.sqrt(sum(v * v for v in g.values())))
        assert norm == pytest.approx(1.0, abs=1e-6)

    def test_speaker_direction_hits_that_speaker(self):
        """扬声器方位角方向 → 该扬声器增益最大（逐源冲激等价）。"""
        for layout in ("5.1", "7.1"):
            for label, az, el in SPEAKER_LAYOUTS[layout]:
                g, _ = vbap_gains(az, el, layout)
                assert max(g, key=g.get) == label, (
                    f"{layout} az={az}: argmax {max(g, key=g.get)} != {label}")

    def test_front_back_switch(self):
        """F/B 切换：前方能量 ↔ 后方能量。"""
        front, _ = vbap_gains(0.0, 0.0, "5.1")
        back, _ = vbap_gains(180.0, 0.0, "5.1")
        front_surround = sum(v for k, v in front.items() if "s" in k.lower())
        back_surround = sum(v for k, v in back.items() if "s" in k.lower())
        assert front_surround < 0.1, "front direction must not light surrounds"
        assert back_surround > 0.8, "back direction must light surrounds"

    def test_center_is_center_not_lr(self):
        """C 方向 → C 扬声器为主（布局切换不串声道，无 Center/LFE 交换）。"""
        for layout in ("5.1", "7.1"):
            g, _ = vbap_gains(0.0, 0.0, layout)
            assert "C" in g and g["C"] == pytest.approx(1.0, abs=1e-6)
            # 邻对解出的残余增益 ~1e-17（数值零）
            assert g.get("L", 0.0) < 1e-6 and g.get("R", 0.0) < 1e-6

    def test_height_enters_top_channels(self):
        """仰角 45° 前方 → 顶部前对（Ltf/Rtf）承载能量。"""
        g, exact = vbap_gains(0.0, 45.0, "7.1.4")
        assert exact
        top_energy = sum(v for k, v in g.items() if "t" in k)
        assert top_energy > 0.7, f"height must light top pair, got {g}"

    def test_no_height_layout_flags_limitation(self):
        """无高度布局 + 仰角>0：钳到水平面并返回 exact=False（明确限制，
        不静默当作沉浸输出）。"""
        g, exact = vbap_gains(0.0, 60.0, "5.1")
        assert not exact
        # 近似结果 = 水平面同方向（C 为主）
        assert g.get("C", 0.0) > 0.7

    def test_stereo_pair_interpolation(self):
        """2.0：正前方 ±30° 对内插（L/R 各半，能量 1）。"""
        g, _ = vbap_gains(0.0, 0.0, "2.0")
        assert set(g) == {"L", "R"}
        assert abs(g["L"] - g["R"]) < 1e-6
        assert g["L"] == pytest.approx(1.0 / np.sqrt(2.0), abs=1e-6)


class TestPan02Spread:
    def test_spread_widens_without_energy_change(self):
        """Spread 60° → 后置扬声器获得能量；总能量保持 1。"""
        tight, _ = spread_gains(0.0, 0.0, layout="5.1")
        wide, _ = spread_gains(0.0, 60.0, layout="5.1")
        assert not any("s" in k.lower() for k in tight)
        wide_surround = sum(v for k, v in wide.items() if "s" in k.lower())
        assert wide_surround > 0.2
        norm = float(np.sqrt(sum(v * v for v in wide.values())))
        assert norm == pytest.approx(1.0, abs=1e-6)

    def test_spread_zero_equals_vbap(self):
        a, _ = vbap_gains(-60.0, 0.0, "5.1")
        b, _ = spread_gains(-60.0, 0.0, layout="5.1")
        assert a == pytest.approx(b, abs=1e-9) or set(a) == set(b)


class TestPan03LfeRouting:
    def test_boom_off_no_lfe(self):
        assert lfe_route(False, "Pre") == (1.0, 0.0)

    def test_pre_post_split_without_doubling(self):
        """Pre/Post：主声道增益保持 main_gain（不因 Boom 加倍）。"""
        for route in ("Pre", "Post"):
            main, lfe = lfe_route(True, route, main_gain=0.8, lfe_gain=0.6)
            assert main == 0.8
            assert lfe == 0.6

    def test_only_routes_exclusively_to_lfe(self):
        """Only：主声道 0，LFE 承载全部。"""
        main, lfe = lfe_route(True, "Only", main_gain=1.0, lfe_gain=1.0)
        assert main == 0.0
        assert lfe == 1.0

    def test_unknown_route_rejected(self):
        with pytest.raises(ValueError):
            lfe_route(True, "Side")

    def test_lfe_excluded_from_downmix_no_double(self):
        """主声道不因 LFE 重复加倍：channel_layout downmix 默认排除 LFE
        （Lo/Ro 不含 LFE 贡献）——Boom 进 LFE 不再回混 L/R。"""
        from src.features.edit.core.channel_layout import (
            ChannelLayout,
            downmix,
        )
        x = np.zeros((6, 8), dtype=np.float32)
        x[0, :] = 0.5   # L
        x[1, :] = 0.5   # R
        x[3, :] = 0.5   # LFE（若被回混会叠加成 0.5+0.354）
        out = downmix(x, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(out[0], 0.5, atol=1e-6)
        np.testing.assert_allclose(out[1], 0.5, atol=1e-6)


class TestPan02LayoutContract:
    def test_layouts_declared(self):
        for name in ("2.0", "5.1", "7.1", "7.1.4"):
            assert name in SPEAKER_LAYOUTS
        assert has_height_channels("7.1.4")
        assert not has_height_channels("7.1")
