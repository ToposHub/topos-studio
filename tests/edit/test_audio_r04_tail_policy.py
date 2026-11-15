"""R04：尾音、延迟与时间线结束策略回归（计划 §5 R04，2026-11-15 复审）。

验收条款：
- 严格选区（默认 include_fx_tail=False）：输出长度 = 选区帧数（逐字节
  不变），Master/Bus 尾音在选区外截断。
- 含尾模式（include_fx_tail=True）：输出按 Bus/Master 链尾音预算延长，
  音频长于视频末帧；选区外区域出现真实的 FX 尾音（非零）。
- clip FX 尾音：clip 链尾音在 clip 结束后以零块推进，回声跨间隙出现。
"""

from __future__ import annotations

import wave
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_export import (
    TimelineExportConfig,
    TimelineExporter,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    EffectChainParams,
    TimelineSequence,
    TrackEffectInstance,
)
from src.shared.media.metadata import Rational

SR = 48000
FPS = 25


def delay_chain(iid: str = "D", delay_ms: float = 200.0) -> EffectChainParams:
    return EffectChainParams(effects=(TrackEffectInstance(
        effect_type="delay", instance_id=iid, params={
            "enabled": True, "left_delay": delay_ms, "right_delay": delay_ms,
            "left_feedback": 0.0, "right_feedback": 0.0,
            "dry_wet": 100.0, "level_db": 0.0, "low_cut": 20.0,
            "high_cut": 20000.0, "high_ratio": 0.0, "stereo_blend": 0.0,
        }),))


def write_pcm(path: Path, signal: np.ndarray) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(signal.shape[0])
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(np.rint(signal.T * 32767.0).astype(np.int16).tobytes())


def _export(project, sequence, root: Path, include_fx_tail: bool = False):
    return TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 4,
        TimelineExportConfig(output_path=str(root / "out.mov"),
                              include_fx_tail=include_fx_tail),
        encoder=Mock())


@pytest.fixture()
def impulse_project(tmp_path):
    signal = np.zeros((2, SR), np.float32)
    signal[:, 0] = 0.5
    path = tmp_path / "impulse.wav"
    write_pcm(path, signal)
    project = EditProject("p", "P", assets={
        "a": MediaAsset("a", str(path), "audio", "I", {})})
    track = EditTrack("T", "T", "audio", 0,
                      clips=(ClipInstance("c1", "a", "T", 0, 2, 0, 2),))
    return project, track


class TestStrictVsTailModes:
    def test_strict_mode_output_length_matches_selection(self, tmp_path,
                                                         impulse_project):
        project, track = impulse_project
        seq = TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(track,),
                               master_effect_chain=delay_chain())
        out = _export(project, seq, tmp_path, include_fx_tail=False)
        assert out.shape[-1] == 4 * SR // FPS  # 严格 = 选区长度

    def test_tail_mode_extends_output_with_master_echo(self, tmp_path,
                                                       impulse_project):
        """Master 200ms Delay：impulse 在帧 0，回声 200ms（帧 5）落在选区
        （0..4）之外——含尾模式必须延长输出并包含该回声。"""
        project, track = impulse_project
        seq = TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(track,),
                               master_effect_chain=delay_chain())
        out = _export(project, seq, tmp_path, include_fx_tail=True)
        strict_len = 4 * SR // FPS
        assert out.shape[-1] > strict_len, "tail mode did not extend output"
        # 延长量 ≥ 200ms（帧 5 的回声位置）
        assert out.shape[-1] >= strict_len + 5 * SR // FPS - strict_len + 1
        # 选区外出现真实尾音（回声 @200ms）
        tail_region = out[:, strict_len:]
        assert float(np.max(np.abs(tail_region))) > 1e-4, \
            "no master tail energy beyond selection"
        # 回声时刻验证：全输出首个能量峰在 200ms
        idx = np.flatnonzero(np.abs(out[0]) > 1e-4)
        assert idx.size and abs(idx[0] * 1000.0 / SR - 200.0) <= 1000.0 / SR

    def test_tail_mode_bus_tail_extends_output(self, tmp_path,
                                               impulse_project):
        """Bus 上的 Delay 尾音同样延长输出（并联汇合取 max 预算）。"""
        project, track = impulse_project
        routed = track.with_output_bus("B") if hasattr(track, "with_output_bus") \
            else EditTrack("T", "T", "audio", 0,
                           clips=track.clips, output_bus_id="B")
        bus = EditTrack("B", "Bus", "audio", 1, is_bus=True,
                        effect_chain=delay_chain("BD"))
        seq = TimelineSequence(fps=Rational(FPS, 1),
                               audio_tracks=(routed, bus))
        out = _export(project, seq, tmp_path, include_fx_tail=True)
        strict_len = 4 * SR // FPS
        assert out.shape[-1] > strict_len
        assert float(np.max(np.abs(out[:, strict_len:]))) > 1e-4


class TestClipFxTail:
    def test_clip_fx_echo_continues_after_clip_end(self, tmp_path):
        """clip 级 Delay：clip 0..2 + 200ms 链尾音 → 回声（200ms，帧 5）
        应出现在选区内（选区 0..10），不再于 clip 结束处截断。"""
        from src.features.edit.core.effect_instance import EffectInstance

        signal = np.zeros((2, SR), np.float32)
        signal[:, 0] = 0.5
        path = tmp_path / "impulse.wav"
        write_pcm(path, signal)
        project = EditProject("p", "P", assets={
            "a": MediaAsset("a", str(path), "audio", "I", {})})
        clip_fx = EffectInstance(
            effect_id="cf-dly", effect_type="delay", order=0, is_enabled=True,
            params={
                "enabled": True, "left_delay": 200.0, "right_delay": 200.0,
                "left_feedback": 0.0, "right_feedback": 0.0,
                "dry_wet": 100.0, "level_db": 0.0, "low_cut": 20.0,
                "high_cut": 20000.0, "high_ratio": 0.0,
                "stereo_blend": 0.0},
        )
        clip = ClipInstance("c1", "a", "T", 0, 2, 0, 2,
                            effects=(clip_fx,))
        track = EditTrack("T", "T", "audio", 0, clips=(clip,))
        seq = TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(track,))
        out = TimelineExporter(project, seq, Mock())._render_audio_master(
            0, 10, TimelineExportConfig(output_path=str(tmp_path / "o.mov")),
            encoder=Mock())
        assert out is not None
        idx = np.flatnonzero(np.abs(out[0]) > 1e-4)
        assert idx.size, "clip FX tail echo missing entirely"
        echo_ms = float(idx[0]) * 1000.0 / SR
        assert abs(echo_ms - 200.0) <= 1000.0 / SR, \
            f"clip fx echo at {echo_ms}ms (expected 200ms)"


class TestRendererTailEstimates:
    def test_master_and_bus_tail_accessors(self, impulse_project, tmp_path):
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        project, track = impulse_project
        bus = EditTrack("B", "Bus", "audio", 1, is_bus=True,
                        effect_chain=delay_chain("BD"))
        seq = TimelineSequence(fps=Rational(FPS, 1),
                               audio_tracks=(track, bus),
                               master_effect_chain=delay_chain("MD"))
        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(seq)
        # Delay 200ms wet100 fb0 → tail 预算 ≥ 200ms（样本）
        assert r.master_tail_estimate_samples() >= int(0.2 * SR) - 1
        assert r.bus_tail_estimate_samples() >= int(0.2 * SR) - 1
        # 无链 → 0
        r2 = AudioGraphRenderer(SR)
        r2.configure_from_sequence(TimelineSequence(
            fps=Rational(FPS, 1), audio_tracks=(track,)))
        assert r2.master_tail_estimate_samples() == 0
        assert r2.bus_tail_estimate_samples() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
