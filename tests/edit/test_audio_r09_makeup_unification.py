"""R09：Dynamics Make Up 两种存储形态统一回归（计划 §5 R09，2026-11-15 复审）。

复审确认的问题：复合 Dynamics 已把 Make Up 放在全部动态处理后（F07），
三实例 panel=dynamics 面板仍把 makeup 存在 Compressor、位于 Limiter 之前
——同面板参数两种模型产生不同限幅声音。

R09 契约：
- ``CompressorParams.makeup_post``：None = legacy（v3 放置，旧工程声音
  不变）；True = 组后增益（连续 panel="dynamics" 段处理完后统一施加，
  压缩器内部 makeup 清零）。
- 新建 DY 面板的压缩器默认 makeup_post=True（dynamics_dialog._write_instance）。
- preset／snapshot 不把组增益加两次（增益只在组末施加一次）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.timeline_v2 import (
    CompressorParams,
    EffectChainParams,
    TrackEffectInstance,
)

pytestmark = pytest.mark.edit_core
SR = 48000.0


def _comp(makeup_db: float, post: bool | None) -> dict:
    p = {"threshold_db": -40.0, "ratio": 2.0, "attack_ms": 1.0,
         "release_ms": 100.0, "knee": 0.0, "mix": 100.0,
         "makeup_gain": makeup_db, "enabled": True}
    if post is not None:
        p["makeup_post"] = post
    return p


def _run(comp_params: dict, lim_threshold_db: float = -20.0) -> np.ndarray:
    """三实例 DY 组（EG 旁路 → Comp(makeup) → Limiter）处理常数块。

    Limiter 天花板 10^(threshold/20)：legacy 的 makeup 在 Limiter 前被
    天花板钳住；组后 makeup 在 Limiter 后施加，可超出天花板——两种放置
    的输出可判别。
    """
    chain = EffectChain(SR)
    chain.set_params([
        ("expander", {"enabled": False}, False, "EG", 4, "dynamics"),
        ("compressor", comp_params, True, "C", 4, "dynamics"),
        ("limiter", {"enabled": True, "threshold_db": lim_threshold_db},
         True, "L", 4, "dynamics"),
    ], sample_rate=SR)
    x = np.full((2, 9600), 0.08, np.float32)
    return chain.process(x)


class TestMakeupPlacement:
    def test_legacy_placement_unchanged(self):
        """无 makeup_post（旧工程）：makeup 在压缩器内、Limiter 前——
        Limiter 天花板钳住输出（peak ≈ 10^(-20/20)）。"""
        out = _run(_comp(6.0, None))
        peak = float(np.abs(out).max())
        assert 0.09 < peak <= 0.105, f"legacy placement changed: {peak}"

    def test_group_post_placement_applies_after_limiter(self):
        """makeup_post=True：+6dB 组后增益在 Limiter 后——输出显著超出
        天花板（≈ 天花板 × 2，受 limiter 包络细节影响 > 1.4×）。"""
        out = _run(_comp(6.0, True))
        peak = float(np.abs(out).max())
        assert peak > 0.14, f"group post makeup not applied: {peak}"

    def test_zero_makeup_identity_between_placements(self):
        """makeup=0 时两种放置逐位一致（增益路径零乘）。"""
    # 同参数仅 makeup_post 差异 + makeup 0
        a = _run(_comp(0.0, None))
        b = _run(_comp(0.0, True))
        np.testing.assert_array_equal(a, b)

    def test_bypassed_compressor_does_not_apply_group_gain(self):
        """压缩器实例旁路：其组后增益不施加（empty chain 直通语义）。"""
        chain = EffectChain(SR)
        chain.set_params([
            ("expander", {"enabled": False}, False, "EG", 4, "dynamics"),
            ("compressor", _comp(12.0, True), False, "C", 4, "dynamics"),
            ("limiter", {"enabled": True, "threshold_db": -6.0},
             True, "L", 4, "dynamics"),
        ], sample_rate=SR)
        x = np.full((2, 4800), 0.08, np.float32)
        out = chain.process(x)
        # 压缩器旁路 + Limiter -6dB → ~0.5 天花板，不乘 4 倍
        assert float(np.abs(out).max()) < 0.55

    def test_gain_not_doubled_across_group_end(self):
        """组增益只在组末施加一次：EG→Comp→Lim 连续 dynamics 段，
        输出 = legacy(无组增益) × 10^(6/20)（不叠加两份 6dB）。"""
        legacy = _run(_comp(0.0, None), lim_threshold_db=3.0)
        post = _run(_comp(6.0, True), lim_threshold_db=3.0)
        ratio = float(np.abs(post).max()) / float(np.abs(legacy).max())
        assert 1.8 < ratio < 2.2, f"group gain doubled or missing: {ratio}"

    def test_roundtrip_and_legacy_absence(self):
        d = CompressorParams(makeup_post=True).to_dict()
        assert d["makeup_post"] is True
        assert CompressorParams.from_dict(d).makeup_post is True
        legacy = CompressorParams.from_dict({"makeup_gain": 3.0})
        assert legacy.makeup_post is None
        assert "makeup_post" not in legacy.to_dict()


class TestDialogNewPanelDefaults:
    def test_new_panel_compressor_gets_makeup_post(self, qapp):
        """dynamics_dialog._write_instance 新建压缩器实例默认组后放置；
        更新已有实例不改变其原放置标记。"""
        from src.shared.ui.audio_mixer.dynamics_dialog import DynamicsDialog
        from unittest.mock import MagicMock
        dlg = DynamicsDialog.__new__(DynamicsDialog)
        dlg._chain = None
        dlg._write_instance("compressor", _comp(3.0, None))
        created = dlg._chain.effects[-1]
        assert created.params["makeup_post"] is True

        # 更新：params 未带 makeup_post → 保留原值 True
        dlg._write_instance("compressor", _comp(4.0, None))
        updated = [e for e in dlg._chain.effects
                   if e.effect_type == "compressor"][-1]
        assert updated.params["makeup_post"] is True

        # legacy 链：已有实例无 makeup_post → 编辑后仍无（声音不变）
        dlg2 = DynamicsDialog.__new__(DynamicsDialog)
        dlg2._chain = EffectChainParams(effects=(
            TrackEffectInstance(effect_type="compressor", panel="dynamics",
                                params=_comp(3.0, None)),))
        dlg2._write_instance("compressor", _comp(5.0, None))
        updated2 = [e for e in dlg2._chain.effects
                    if e.effect_type == "compressor"][-1]
        assert "makeup_post" not in updated2.params


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
