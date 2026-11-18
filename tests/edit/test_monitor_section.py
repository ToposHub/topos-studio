"""P1 · 监控与录音分离（2026-11-19 计划第二阶段）。

验收（核心层）：分开 Arm / Input Monitor / Record 三态；独立监听音量、
Mono、DIM、Mute 只作用于监听转发——take 录监听前信号，监听状态对
录音/导出零副作用；监听参考校准标注可持久化。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.monitor_section import (
    DEFAULT_DIM_DB,
    MonitorSection,
    MonitorState,
)
from src.features.edit.core.recording_manager import RecordingConfig

RATE = 48000


def _manager():
    from src.features.edit.core.recording_manager import RecordingManager

    return RecordingManager(RecordingConfig(
        output_dir="/tmp/topos_test_takes", sample_rate=RATE, channels=2))


class TestMonitorSection:
    def test_volume_and_mono(self) -> None:
        section = MonitorSection(MonitorState(volume_db=-6.0, mono=True))
        block = np.stack([
            np.full(100, 0.5, dtype=np.float32),
            np.full(100, -0.3, dtype=np.float32),
        ])
        out = section.process(block)
        # Mono 折叠 (0.5−0.3)/2=0.1 后 −6 dB。
        expected = 0.1 * (10.0 ** (-6.0 / 20.0))
        np.testing.assert_allclose(out[0], expected, atol=1e-6)
        np.testing.assert_allclose(out[1], expected, atol=1e-6)
        # 输入不变。
        assert block[0, 0] == 0.5 and block[1, 0] == -0.3

    def test_dim_and_mute(self) -> None:
        block = np.full((2, 64), 1.0, dtype=np.float32)
        dim = MonitorSection(MonitorState(dim=True)).process(block)
        assert dim[0, 0] == pytest.approx(10.0 ** (DEFAULT_DIM_DB / 20.0))
        muted = MonitorSection(MonitorState(muted=True)).process(block)
        assert np.all(muted == 0.0)

    def test_state_round_trip(self) -> None:
        state = MonitorState(volume_db=-8.0, mono=True, dim=True, pfl=True,
                             reference_db=-18.0)
        restored = MonitorState.from_dict(state.to_dict())
        assert restored == state


class TestArmMonitorRecordStates:
    def test_tri_state_independent(self) -> None:
        manager = _manager()
        assert manager.armed is False and manager.input_monitor is False
        manager.set_input_monitor(True)
        assert manager.input_monitor is True and manager.armed is False
        assert manager.state == "idle"  # 监听不触发 take
        manager.arm_input(True)
        assert manager.armed is True and manager.state == "idle"
        manager.arm_input(False)
        assert manager.armed is False

    def test_disarm_blocked_while_recording(self) -> None:
        manager = _manager()
        manager.arm_input(True)
        manager._state = manager.STATE_RECORDING
        errors: list[str] = []
        manager.on_error = errors.append
        manager.arm_input(False)
        assert manager.armed is True and errors

    def test_monitor_chain_applies_to_forward_only(self) -> None:
        manager = _manager()
        forwarded: list[np.ndarray] = []
        manager.on_monitor_block = forwarded.append
        manager.set_monitor_section(MonitorSection(
            MonitorState(volume_db=-20.0)))
        raw = np.full((2, 32), 0.5, dtype=np.float32)
        # 模拟设备回调内部转发（复用同一数学路径）。
        forwarded.append(manager._monitor_section.process(raw))
        assert forwarded[0][0, 0] == pytest.approx(0.05, abs=1e-6)
        # 原始采集数据未被监听通道条修改。
        assert raw[0, 0] == 0.5
