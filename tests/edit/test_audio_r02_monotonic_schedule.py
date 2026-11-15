"""R02：单调调度与有界侧链 tap 回归（计划 §5 R02，2026-11-15 复审）。

原失败案例（docs/audit/2026-11-15-audio-mixer-review/reproduce_remaining_issues.py
的 probe_export_cursor / probe_sidechain_cache）转为 pytest：
- 两片段 0..2 与 20..22、200ms 全湿 Delay：旧实现 render_track_block 帧序列
  为 [0,1,0,1,2,…]（early flush 后 final drain 从 start_frame 重扫 + 尾音预算
  未清零，旧帧二次喂入有状态节点），首回声提前到 120ms。
- SC 帧缓存 + 结束前全长拼接 = 每源双份全长拷贝；改为单份直写缓冲，
  Bus/Master 消费完立即释放。
"""

from __future__ import annotations

import gc
import sys
import tracemalloc
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
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


def delay_chain(*, wet: float = 100.0, iid: str = "D") -> EffectChainParams:
    return EffectChainParams(effects=(TrackEffectInstance(
        effect_type="delay", instance_id=iid, params={
            "enabled": True, "left_delay": 200.0, "right_delay": 200.0,
            "left_feedback": 0.0, "right_feedback": 0.0,
            "dry_wet": wet, "level_db": 0.0, "low_cut": 20.0,
            "high_cut": 20000.0, "high_ratio": 0.0, "stereo_blend": 0.0,
        },
    ),))


def write_pcm(path: Path, signal: np.ndarray) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(signal.shape[0])
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(np.rint(signal.T * 32767.0).astype(np.int16).tobytes())


def _impulse_project(root: Path, clip_layout) -> tuple:
    signal = np.zeros((2, SR), np.float32)
    signal[:, 0] = 0.5
    path = root / "impulse.wav"
    write_pcm(path, signal)
    project = EditProject("p", "Probe", assets={
        "a": MediaAsset("a", str(path), "audio", "Impulse", {}),
    })
    track = EditTrack("T", "Track", "audio", 0, clips=clip_layout,
                      effect_chain=delay_chain())
    sequence = TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(track,))
    return project, sequence


def _export(project, sequence, root: Path) -> np.ndarray:
    return TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 30, TimelineExportConfig(output_path=str(root / "out.mov")),
        encoder=Mock())


class TestMonotonicSchedule:
    def test_frame_sequence_strictly_monotonic_and_echo_on_time(self, tmp_path):
        """复现 probe_export_cursor：帧序列严格递增，首回声 200ms（±1 采样）。"""
        project, sequence = _impulse_project(
            tmp_path, (ClipInstance("c1", "a", "T", 0, 2, 0, 2),
                       ClipInstance("c2", "a", "T", 20, 2, 10, 2)))
        calls: list[int] = []
        original = AudioGraphRenderer.render_track_block

        def observe(renderer, tid, samples, current_frame, *a, **kw):
            calls.append(int(current_frame))
            return original(renderer, tid, samples, current_frame, *a, **kw)

        with patch.object(AudioGraphRenderer, "render_track_block", observe):
            output = _export(project, sequence, tmp_path)
        assert calls == sorted(calls), f"non-monotonic: {calls}"
        assert len(calls) == len(set(calls)), f"repeated frames: {calls}"
        indices = np.flatnonzero(np.abs(output[0]) > 1e-4)
        assert indices.size, "no echo rendered"
        echo_ms = float(indices[0]) * 1000.0 / SR
        assert abs(echo_ms - 200.0) <= 1000.0 / SR

    def test_clip_storage_order_does_not_change_output(self, tmp_path):
        """clip 列表乱序存储输出一致（suffix-min 提前 flush + 单调游标）。"""
        clips = (ClipInstance("c1", "a", "T", 0, 2, 0, 2),
                 ClipInstance("c2", "a", "T", 20, 2, 10, 2),
                 ClipInstance("c3", "a", "T", 8, 2, 4, 2))
        project_a, seq_a = _impulse_project(tmp_path, clips)
        project_b, seq_b = _impulse_project(
            tmp_path, (clips[2], clips[0], clips[1]))  # 乱序
        out_a = _export(project_a, seq_a, tmp_path)
        out_b = _export(project_b, seq_b, tmp_path)
        np.testing.assert_allclose(out_a, out_b, atol=1e-5)

    def test_two_impulse_gaps_keep_echo_positions(self, tmp_path):
        """间隙帧不再把旧帧二次喂入：首回声仍在 200ms，第二脉冲回声在
        20 帧 + 200ms 处（旧行为会提前 ~80ms）。"""
        project, sequence = _impulse_project(
            tmp_path, (ClipInstance("c1", "a", "T", 0, 2, 0, 2),
                       ClipInstance("c2", "a", "T", 20, 2, 0, 2)))  # 第二个也是脉冲
        output = _export(project, sequence, tmp_path)
        idx = np.flatnonzero(np.abs(output[0]) > 1e-4)
        assert idx.size >= 2
        echo1_ms = idx[0] * 1000.0 / SR
        # 第二个回声簇：跳过第一回声的衰减尾（>100ms 之后的新能量起点）
        second = idx[idx > idx[0] + int(0.1 * SR)]
        assert second.size, "second echo cluster missing"
        impulse2_sample = 20 * SR // FPS
        echo2_ms = (second[0] - impulse2_sample) * 1000.0 / SR
        assert abs(echo1_ms - 200.0) <= 1000.0 / SR
        assert abs(echo2_ms - 200.0) <= 1000.0 / SR


class TestSidechainSingleCopy:
    def _sc_project(self, root: Path):
        assets = {}
        tracks = []
        for name in ("programme", "key"):
            path = root / f"{name}.wav"
            sig = np.stack([0.1 * np.sin(2 * np.pi * 440.0 * np.arange(SR) / SR)] * 2)
            write_pcm(path, sig.astype(np.float32))
            assets[name] = MediaAsset(name, str(path), "audio", name, {})
            tracks.append(EditTrack(name, name, "audio", len(tracks), clips=(
                ClipInstance(f"{name}-clip", name, name, 0, 25, 0, 25),)))
        chain = EffectChainParams(effects=(TrackEffectInstance(
            effect_type="compressor", params={"enabled": True, "sc_on": True,
                                              "sc_source": "key"},
            instance_id="C"),))
        sequence = TimelineSequence(fps=Rational(FPS, 1), audio_tracks=tuple(tracks),
                                    master_effect_chain=chain)
        return EditProject("sc", "SC", assets=assets), sequence

    def _plain_project(self, root: Path):
        return _impulse_project(root, (ClipInstance("c1", "a", "T", 0, 25, 0, 25),))

    @pytest.mark.slow
    def test_sc_export_memory_delta_bounded_to_single_copy(self, tmp_path):
        """1 秒双轨工程：开启外部 SC（1 源）相对内部检出的峰值内存增量
        ≤ 单份全长缓冲 × 1.6（旧实现 ≈ 2 份：帧缓存 + 拼接）。
        基线与实验用同一素材/轨数（解码器开销对齐），进程级 tracemalloc。"""
        def _peak(fn) -> int:
            gc.collect()
            tracemalloc.start()
            try:
                fn()
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
                gc.collect()

        sc_project, sc_sequence = self._sc_project(tmp_path)
        # 基线：同一双轨工程，压缩器用内部检出（无 SC 缓冲）
        base_sequence = TimelineSequence(
            fps=Rational(FPS, 1), audio_tracks=sc_sequence.audio_tracks,
            master_effect_chain=EffectChainParams(effects=(TrackEffectInstance(
                effect_type="compressor",
                params={"enabled": True, "sc_on": True, "sc_source": ""},
                instance_id="C"),)))
        peak_base = _peak(lambda: _export(sc_project, base_sequence, tmp_path))
        peak_sc = _peak(lambda: _export(sc_project, sc_sequence, tmp_path))
        single_copy = 2 * SR * 4  # (channels=2, samples, float32)
        assert peak_sc - peak_base < single_copy * 1.6, (
            f"SC peak delta {peak_sc - peak_base} bytes ≥ 1.6 copies "
            f"({single_copy * 1.6:.0f}); frame-cache double-copy regression?")

    def test_bus_stage_receives_full_length_sc_tap(self, tmp_path):
        """Bus/Master 消费到的 tap 为检源轨完整输出（PCM oracle 对齐）。"""
        project, sequence = self._sc_project(tmp_path)
        captured: dict = {}
        original = AudioGraphRenderer.apply_master

        def observe(renderer, output, *a, **kw):
            taps = kw.get("sidechain_taps")
            # 快照（导出方消费后在函数内 del 释放引用，不保留 dict 对象）
            captured["taps"] = dict(taps) if taps else taps
            return original(renderer, output, *a, **kw)

        with patch.object(AudioGraphRenderer, "apply_master", observe):
            _export(project, sequence, tmp_path)
        taps = captured.get("taps") or {}
        assert "key" in taps, f"master chain external key tap missing: {taps.keys()}"
        key = taps["key"]
        total = 30 * SR // FPS  # _export 渲染 0..30 帧
        assert key.shape == (2, total)
        # 检源轨直接渲染的 oracle：0.1 幅度 440Hz 正弦（轨参数为默认 unity），
        # 经 int16 WAV 量化（±1 LSB = 3.05e-5）；clip 只覆盖前 25 帧
        oracle = np.rint(0.1 * np.sin(2 * np.pi * 440.0 * np.arange(SR) / SR)
                         * 32767.0) / 32767.0
        np.testing.assert_allclose(key[0, :SR], oracle.astype(np.float32),
                                   atol=3.1e-5)
        np.testing.assert_allclose(key[:, 25 * SR // FPS:], 0.0, atol=1e-6)


class TestExportPreflightDiagnostics:
    def test_unknown_fx_logged_in_preflight(self, tmp_path, caplog):
        project, sequence = _impulse_project(
            tmp_path, (ClipInstance("c1", "a", "T", 0, 2, 0, 2),))
        track = sequence.audio_tracks[0]
        chain = EffectChainParams(effects=(
            TrackEffectInstance.from_dict({
                "effect_type": "future_fx", "params": {"x": 1.0},
                "instance_id": "F9"}),
        ))
        sequence = sequence.with_track_updated(
            track.with_effect_chain(chain))
        import logging
        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.timeline_export"):
            _export(project, sequence, tmp_path)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "audio export preflight" in joined
        assert "future_fx" in joined


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
