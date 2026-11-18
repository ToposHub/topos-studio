"""P0 · PDC（插件延迟补偿）与 Stem 求和语义（2026-11-19 计划第一阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- 不同块长下同一毫秒的脉冲落点一致；
- 声明算法延迟的轨（WOLA/FIR 类）与干轨的脉冲在 Master 汇合处落在
  同一绝对样本（PDC 以"最迟路径对齐"补偿）；
- 线性图且同一取点时，分类 stems（pre-Master）数学求和与合成对得上；
- Master 有非线性处理（限幅器触发）时另测预期差异。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer

SR = 48000
FPS = 24.0


class _LatencyFx:
    """声明算法延迟 L 的测试效果：process 输出 = 输入延迟 L（有状态）。"""

    def __init__(self, latency: int) -> None:
        self._latency = int(latency)
        # 历史预填 L 个零——真实块式算法（WOLA/FIR）首输入前即有 L 个
        # 零的群延迟历史。
        self._history = np.zeros((2, self._latency), dtype=np.float32)
        self.is_active = True

    def process(self, samples: np.ndarray, sidechain_by_id=None) -> np.ndarray:
        combined = np.concatenate([self._history, samples], axis=1)
        out = combined[:, :samples.shape[-1]]
        self._history = combined[:, samples.shape[-1]:]
        if self._history.shape[-1] > self._latency:
            self._history = self._history[:, -self._latency:]
        return np.ascontiguousarray(out, dtype=np.float32)

    def latency_samples(self) -> int:
        return self._latency

    def tail_estimate_samples(self) -> int:
        return self._latency

    def reset_state(self) -> None:
        self._history = np.zeros((2, self._latency), dtype=np.float32)


def _two_track_sequence():
    """干轨 dry + 延迟轨 lat（10 ms），均直通 Master。"""
    dry = SimpleNamespace(
        track_id="dry", name="Dry", track_type="audio", index=0,
        clips=(), volume=1.0, pan=0.0, is_bus=False, output_bus_id=None,
        effect_chain=None, sends=(), enabled=True, muted=False, solo=False,
    )
    lat = SimpleNamespace(
        track_id="lat", name="Lat", track_type="audio", index=1,
        clips=(), volume=1.0, pan=0.0, is_bus=False, output_bus_id=None,
        effect_chain=None, sends=(), enabled=True, muted=False, solo=False,
    )
    return SimpleNamespace(
        audio_tracks=(dry, lat), master_volume=1.0, master_muted=False)


def _renderer_with_latency_fx(latency: int) -> AudioGraphRenderer:
    renderer = AudioGraphRenderer(float(SR))
    renderer.configure_from_sequence(_two_track_sequence())
    renderer.get_node("lat").effect_chain = _LatencyFx(latency)
    renderer._recompute_pdc()
    return renderer


def _impulse_block(block_len: int, impulse_at: int) -> np.ndarray:
    block = np.zeros((2, block_len), dtype=np.float32)
    if 0 <= impulse_at < block_len:
        block[0, impulse_at] = 1.0
        block[1, impulse_at] = 1.0
    return block


def _render_track_stream(renderer, track_id, total, block_len,
                          impulse_at=1000):
    """以固定块长推进一条轨，返回全长输出（模拟导出逐块求和）。"""
    out = np.zeros((2, total), dtype=np.float32)
    pos = 0
    while pos < total:
        n = min(block_len, total - pos)
        block = _impulse_block(n, impulse_at - pos if pos <= impulse_at < pos + n else -1)
        processed = renderer.render_track_block(track_id, block, 0)
        if processed is not None:
            out[:, pos:pos + n] += processed
        pos += n
        impulse_consumed = True
    return out


class TestPdcAlignment:
    def test_chain_latency_aggregation(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain

        chain = EffectChain(float(SR))
        stub = _LatencyFx(1024)
        # 注入两个启用实例语义：直接验证聚合方法对声明求和。
        chain._effects = [stub, _LatencyFx(256)]
        chain._enabled_flags = [True, True]
        assert chain.latency_samples() == 1024 + 256
        chain._enabled_flags = [True, False]
        assert chain.latency_samples() == 1024

    def test_pulse_alignment_dry_matches_latency_track(self) -> None:
        latency = 480  # 10 ms
        renderer = _renderer_with_latency_fx(latency)
        total = SR // 2
        dry = _render_track_stream(renderer, "dry", total, 2000)
        lat = _render_track_stream(renderer, "lat", total, 2000)
        # 干轨被 PDC 延迟 480，延迟轨自身延迟 480 → 两轨脉冲同一样本落点。
        dry_hit = int(np.argmax(np.abs(dry[0])))
        lat_hit = int(np.argmax(np.abs(lat[0])))
        assert dry_hit == lat_hit == 1000 + latency
        # PDC 预算：最迟路径 = 轨级 480。
        assert renderer.pdc_total_samples() == latency

    def test_block_size_invariance(self) -> None:
        renderer = _renderer_with_latency_fx(480)
        total = SR // 2
        small = _render_track_stream(renderer, "dry", total, 733)
        renderer2 = _renderer_with_latency_fx(480)
        big = _render_track_stream(renderer2, "dry", total, 4001)
        np.testing.assert_array_equal(small, big)
        # 延迟轨同样块长无关。
        lat_small = _render_track_stream(_renderer_with_latency_fx(480), "lat", total, 733)
        lat_big = _render_track_stream(_renderer_with_latency_fx(480), "lat", total, 4001)
        np.testing.assert_array_equal(lat_small, lat_big)

    def test_muted_block_advances_delay_line(self) -> None:
        """mute automation 整块静音也推进延迟线（时间不漂移）。"""
        renderer = _renderer_with_latency_fx(480)
        zeros = np.zeros((2, 1000), dtype=np.float32)
        assert renderer.render_track_block("dry", zeros, 0) is not None
        # 无自动化时直接驱动：先喂含脉冲块，再喂零块，再喂脉冲块。
        b1 = _impulse_block(500, 10)
        b2 = np.zeros((2, 500), dtype=np.float32)
        b3 = _impulse_block(500, 10)
        o1 = renderer.render_track_block("dry", b1, 0)
        o2 = renderer.render_track_block("dry", b2, 0)
        o3 = renderer.render_track_block("dry", b3, 0)
        joined = np.concatenate([o1, o2, o3], axis=1)
        hits = np.nonzero(joined[0])[0]
        # 两脉冲间隔 = 输入间隔 1000 样本（延迟线统一平移）。
        assert len(hits) == 2 and hits[1] - hits[0] == 1000


class TestStemSumSemantics:
    """线性图下分类 stems（pre-Master）求和 == 合成。"""

    def _sequence(self):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        c1 = ClipInstance("c1", "a1", "t1", 0, 6, 0, 6)
        c2 = ClipInstance("c2", "a2", "t2", 0, 6, 0, 6)
        t1 = EditTrack("t1", "DX", "audio", 0, clips=(c1,), volume=0.8, pan=0.25)
        t2 = EditTrack("t2", "MX", "audio", 1, clips=(c2,), volume=0.6, pan=-0.3)
        return TimelineSequence(
            timeline_id="s", name="Sum", audio_tracks=(t1, t2), fps=FPS)

    def _decode_factory(self):
        rng = np.random.default_rng(20261120)

        def decode(track, clip, frame_idx, fps, n_samples):
            # 低振幅：确保 MasterLimiter 全程不触发（真线性图）。
            seed = 1 if track.track_id == "t1" else 2
            r = np.random.default_rng(seed * 7717 + int(frame_idx))
            sig = 0.05 * r.standard_normal(n_samples).astype(np.float32)
            return np.stack([sig, sig * 0.9])
        return decode

    def test_pre_master_stems_sum_equals_full_mix(self) -> None:
        from src.features.edit.core.stem_export import (
            StemSpec,
            StemType,
            render_stem_pcm,
        )

        seq = self._sequence()
        decode = self._decode_factory()
        t1 = render_stem_pcm(seq, StemSpec(
            stem_type=StemType.TRACK, name="DX", track_ids=("t1",),
            include_master=False), sample_rate=SR, channels=2,
            decode_clip_fn=decode)
        t2 = render_stem_pcm(seq, StemSpec(
            stem_type=StemType.TRACK, name="MX", track_ids=("t2",),
            include_master=False), sample_rate=SR, channels=2,
            decode_clip_fn=decode)
        master = render_stem_pcm(seq, StemSpec(
            stem_type=StemType.MASTER, name="Master", track_ids=(),
            include_master=True), sample_rate=SR, channels=2,
            decode_clip_fn=decode)
        assert t1 is not None and t2 is not None and master is not None
        # 线性图（Master 无 FX/限幅未触发）：分轨和 == 合成。
        np.testing.assert_allclose(t1 + t2, master, atol=1e-5)

    def test_nonlinear_master_breaks_sum(self) -> None:
        """Master 限幅器触发（非线性）时求和预期不等——语义另测。"""
        from src.features.edit.core.stem_export import (
            StemSpec,
            StemType,
            render_stem_pcm,
        )

        seq = self._sequence()
        object.__setattr__(seq, "master_volume", 4.0)  # 触发 MasterLimiter

        def decode(track, clip, frame_idx, fps, n_samples):
            seed = 1 if track.track_id == "t1" else 2
            r = np.random.default_rng(seed * 7717 + int(frame_idx))
            sig = 0.9 * r.standard_normal(n_samples).astype(np.float32)
            return np.stack([sig, sig])
        t1 = render_stem_pcm(seq, StemSpec(
            stem_type=StemType.TRACK, name="DX", track_ids=("t1",),
            include_master=False), sample_rate=SR, channels=2,
            decode_clip_fn=decode)
        t2 = render_stem_pcm(seq, StemSpec(
            stem_type=StemType.TRACK, name="MX", track_ids=("t2",),
            include_master=False), sample_rate=SR, channels=2,
            decode_clip_fn=decode)
        master = render_stem_pcm(seq, StemSpec(
            stem_type=StemType.MASTER, name="Master", track_ids=(),
            include_master=True), sample_rate=SR, channels=2,
            decode_clip_fn=decode)
        assert t1 is not None and t2 is not None and master is not None
        assert not np.allclose(t1 + t2, master, atol=1e-3)
