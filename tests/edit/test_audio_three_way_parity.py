"""P0-01 验收：固定 PCM fixture 下三方一致（实时 / 导出 / Stem）。

2026-08-27 剪辑页审计 P0-01：「实时播放、离线导出、Stem、响度分析共用
同一节点处理器」。导出与 Stem 已复用 AudioGraphRenderer；实时路径由
AudioMixer 实现并经 parity 测试锁定。本测试用同一份确定性 PCM fixture
把三条路径的最终 master PCM 拉通对比，按 DSP 类型定义容差：

- volume/pan：1e-6
- 轨道 EQ（IIR 状态跨块演化）：1e-5
- Bus 路由 + master：1e-6
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.stem_export import StemSpec, StemType, render_stem_pcm
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    StripEQParams,
    TimelineSequence,
)
from src.features.edit.core.clip_params import SpeedParams

SR = 48000
FPS = 24.0
SPF = int(SR / FPS)  # 2000 samples/frame
FRAMES = 6


def _pcm_for_frame(frame_idx: int, n: int, seed: int) -> np.ndarray:
    """确定性 PCM：跨帧连续相位的正弦 + 固定噪声（按 seed 区分轨道）。"""
    t = (frame_idx * n + np.arange(n)) / SR
    freq = 220.0 + seed * 55.0
    left = 0.4 * np.sin(2 * np.pi * freq * t)
    right = 0.35 * np.sin(2 * np.pi * freq * 1.5 * t + 0.3)
    rng = np.random.default_rng(seed * 1000 + frame_idx)
    noise = 0.02 * rng.standard_normal(n)
    return np.stack([
        (left + noise).astype(np.float32),
        (right + noise).astype(np.float32),
    ])


class _FixedDecoder:
    """实时路径假 decoder：返回与 fixture 相同的 PCM。"""

    def __init__(self, seed: int):
        self._seed = seed

    def decode_samples_for_frame(self, frame, fps, num_samples):
        return _pcm_for_frame(int(frame), int(num_samples), self._seed)

    def close(self):
        pass


def _sequence(with_eq: bool, with_bus: bool) -> TimelineSequence:
    clip1 = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="t1",
        timeline_start=0, timeline_duration=FRAMES,
        source_start=0, source_duration=FRAMES, speed=SpeedParams(speed=1.0),
    )
    clip2 = ClipInstance(
        clip_id="c2", asset_id="a2", track_id="t2",
        timeline_start=0, timeline_duration=FRAMES,
        source_start=0, source_duration=FRAMES, speed=SpeedParams(speed=1.0),
    )
    eq = StripEQParams(
        enabled=True, mode="3-band", bands=(3.0, -2.0, 4.0), gain=2.0,
        low_cut_enable=False, low_cut=20.0, high_cut_enable=False, high_cut=20000.0,
    ) if with_eq else None
    t1 = EditTrack(
        track_id="t1", name="A1", track_type="audio", index=0,
        clips=(clip1,), volume=0.8, pan=0.25,
        output_bus_id="bus1" if with_bus else None,
    )
    t2 = EditTrack(
        track_id="t2", name="A2", track_type="audio", index=1,
        clips=(clip2,), volume=0.6, pan=-0.3, strip_eq=eq,
    )
    tracks = [t1, t2]
    if with_bus:
        tracks.append(EditTrack(
            track_id="bus1", name="Bus1", track_type="audio", index=2,
            is_bus=True, volume=0.9,
        ))
    seq = TimelineSequence(
        timeline_id="seq", name="ThreeWay", audio_tracks=tuple(tracks),
        fps=FPS,
    )
    return seq


def _render_realtime(seq: TimelineSequence, with_eq: bool, with_bus: bool) -> np.ndarray:
    """实时路径：AudioMixer（memory sink）逐帧 mix_samples。"""
    mixer = AudioMixer()
    mixer.add_track("t1", _FixedDecoder(1), AudioTrackState(
        track_id="t1", volume=0.8, pan=0.25,
    ))
    mixer.add_track("t2", _FixedDecoder(2), AudioTrackState(
        track_id="t2", volume=0.6, pan=-0.3,
    ))
    mixer.set_track_routing("t1", "bus1" if with_bus else None, [], is_bus=False)
    if with_bus:
        mixer.register_bus_track(
            "bus1", AudioTrackState(track_id="bus1", volume=0.9)
        )
        mixer.set_track_routing("bus1", None, [], is_bus=True)
    t2 = next(t for t in seq.audio_tracks if t.track_id == "t2")
    if with_eq and t2.strip_eq is not None:
        mixer.set_track_strip_eq("t2", t2.strip_eq)

    chunks = []
    for f in range(FRAMES):
        out, _ = mixer.mix_samples(f, FPS, SPF, 2)
        chunks.append(out)
    return np.concatenate(chunks, axis=1)


def _render_export(seq: TimelineSequence, with_eq: bool, with_bus: bool) -> np.ndarray:
    """导出路径：AudioGraphRenderer（timeline_export 同款驱动循环）。"""
    renderer = AudioGraphRenderer(float(SR))
    renderer.configure_from_sequence(seq)
    output = np.zeros((2, FRAMES * SPF), dtype=np.float32)
    bus_inputs: dict = {}
    for f in range(FRAMES):
        for track_id, seed in (("t1", 1), ("t2", 2)):
            samples = _pcm_for_frame(f, SPF, seed)
            processed = renderer.render_track_block(track_id, samples, f)
            if processed is None:
                continue
            node = renderer.get_node(track_id)
            target = node.output_bus_id if node else None
            if target is not None:
                bus_inputs.setdefault(
                    target, np.zeros((2, FRAMES * SPF), dtype=np.float32)
                )
                bus_inputs[target][:, f * SPF:(f + 1) * SPF] += processed
            else:
                output[:, f * SPF:(f + 1) * SPF] += processed
    renderer.render_bus_stage(bus_inputs, output, has_solo=False)
    return renderer.apply_master(output)


def _render_stem(seq: TimelineSequence) -> np.ndarray:
    """Stem 路径：render_stem_pcm MASTER（含 master stage）。"""
    stem = StemSpec(
        stem_type=StemType.MASTER, name="Master",
        track_ids=(), include_master=True,
    )

    def decode(track, clip, frame_idx, fps, n_samples):
        seed = 1 if track.track_id == "t1" else 2
        return _pcm_for_frame(int(frame_idx), int(n_samples), seed)

    pcm = render_stem_pcm(seq, stem, sample_rate=SR, channels=2,
                          decode_clip_fn=decode)
    assert pcm is not None
    return pcm


class TestThreeWayParity:
    """实时 memory sink / 离线导出 / Stem 的 master PCM 三方一致。"""

    @pytest.mark.parametrize("with_eq,with_bus,atol", [
        (False, False, 1e-6),   # volume/pan
        (True, False, 1e-5),    # 轨道 EQ（IIR 跨块状态）
        (False, True, 1e-6),    # Bus 路由
        (True, True, 1e-5),     # EQ + Bus 全链
    ])
    def test_three_way_master_pcm(self, with_eq, with_bus, atol):
        seq = _sequence(with_eq, with_bus)
        rt = _render_realtime(seq, with_eq, with_bus)
        ex = _render_export(seq, with_eq, with_bus)
        st = _render_stem(seq)

        assert rt.shape == ex.shape == st.shape == (2, FRAMES * SPF)
        assert np.allclose(rt, ex, atol=atol), \
            f"realtime vs export: max diff {np.max(np.abs(rt - ex)):.2e}"
        assert np.allclose(rt, st, atol=atol), \
            f"realtime vs stem: max diff {np.max(np.abs(rt - st)):.2e}"
        assert np.allclose(ex, st, atol=atol), \
            f"export vs stem: max diff {np.max(np.abs(ex - st)):.2e}"
