"""P2 · 动态处理器分段遥测（FX-03 · 2026-11-21 计划）。

验收：E/G、C、L 三根表各自取子模块实际非负 GR（不用最大值填满）；
detector_dbfs 为压缩器检测电平；limiter_overshoot_dbfs 为限幅后块峰；
表计口径 = 块末状态；旁路/关闭全部清零（含子模块旧读数）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import DynamicsEffect

RATE = 48000


def _effect() -> DynamicsEffect:
    fx = DynamicsEffect(RATE)
    fx.set_params({
        "enabled": True, "makeup": 3.0,
        "eg": {"threshold_db": -30.0, "range_db": 12.0, "ratio": 2.0,
               "enabled": True},
        "comp": {"threshold_db": -16.0, "ratio": 3.0, "knee": 0.0,
                 "mix": 100.0, "enabled": True},
        "lim": {"threshold_db": -1.0, "enabled": True},
    })
    return fx


class TestStageTelemetry:
    def test_stages_report_own_gr(self) -> None:
        """安静段（EG 触发）与响段（Comp 触发）：三根表各自独立读数。"""
        t = np.arange(RATE) / RATE
        quiet = (0.02 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        loud = (0.95 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        fx = _effect()
        fx.process(quiet.copy())
        tel_quiet = fx.collect_telemetry()
        assert tel_quiet["eg_gr_db"] > 0.0       # 低于门限 → EG 衰减
        assert tel_quiet["comp_gr_db"] == 0.0    # 检测未超阈值
        fx.process(loud.copy())
        tel_loud = fx.collect_telemetry()
        assert tel_loud["comp_gr_db"] > 0.0
        assert not (tel_loud["eg_gr_db"] == tel_loud["comp_gr_db"]
                    == tel_loud["lim_gr_db"])  # 不用最大值填满三根表
        assert tel_loud["detector_dbfs"] > -3.0   # 响段检测电平接近满幅

    def test_limiter_overshoot_is_block_peak(self) -> None:
        """限幅过冲 = 限幅后（Make Up 前）块峰（makeup 3 dB 前读数）。"""
        t = np.arange(RATE) / RATE
        loud = (0.95 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        fx = _effect()
        out = fx.process(loud.copy())
        tel = fx.collect_telemetry()
        # 过冲读数应低于最终输出峰（makeup +3 dB 在过冲读数之后）。
        out_peak_db = 20.0 * np.log10(float(np.max(np.abs(out))))
        assert tel["limiter_overshoot_dbfs"] <= out_peak_db + 1e-6
        assert tel["makeup_db"] == 3.0

    def test_bypass_clears_all_meters(self) -> None:
        """旁路/关闭：三根表 + 检测 + 过冲全部清零（含子模块旧读数）。"""
        t = np.arange(RATE) / RATE
        loud = (0.95 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        fx = _effect()
        fx.process(loud.copy())
        fx.set_params({"enabled": False})
        fx.process(loud.copy())
        tel = fx.collect_telemetry()
        assert tel["eg_gr_db"] == 0.0
        assert tel["comp_gr_db"] == 0.0
        assert tel["lim_gr_db"] == 0.0
        assert tel["limiter_overshoot_dbfs"] == -120.0

    def test_reset_state_clears_meters(self) -> None:
        t = np.arange(RATE) / RATE
        loud = (0.95 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        fx = _effect()
        fx.process(loud.copy())
        fx.reset_state()
        tel = fx.collect_telemetry()
        assert tel["comp_gr_db"] == 0.0
        assert tel["lim_gr_db"] == 0.0
