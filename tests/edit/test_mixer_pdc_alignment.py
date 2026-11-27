"""P0 · PDC（实时监听）——AudioMixer 两级最迟路径对齐（边界 1 收口）。

验收（docs/plans/2026-11-19-film-post-audio-audit-and-roadmap-status.md 边界 1）：

- 干轨与声明算法延迟的轨在实时混音输出中脉冲落在同一绝对样本
  （离线导出已由 AudioGraphRenderer 两级延迟线对齐，实时监听同模型）；
- 实时对齐位置与离线 AudioGraphRenderer 一致；
- 块长无关（同一毫秒落点一致）；
- mute/无输入剔除的块推进延迟线（缓冲内容被推出，不延迟爆发）；
- 零延迟图不引入任何补偿（delay=0 直通）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState

pytestmark = pytest.mark.edit_core
SR = 48000
FPS = 24.0
BLOCK = SR // int(FPS)


class _LatencyFx:
    """声明算法延迟 L 的测试效果：process 输出 = 输入延迟 L（有状态）。

    提供 AudioMixer 轨/Bus 链路径消费的全部接口（侧链/遥测为空实现）。
    """

    def __init__(self, latency: int) -> None:
        self._latency = int(latency)
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

    def set_params(self, _entries, sample_rate: float = SR) -> None:
        pass

    def sidechain_sources(self) -> set:
        return set()

    def sidechain_map(self, _taps) -> dict:
        return {}

    def collect_gain_reduction(self) -> dict:
        return {}

    def collect_detector_levels(self) -> dict:
        return {}

    def tap_levels(self) -> dict:
        return {}

    def set_spectrum_metering(self, _enabled: bool) -> None:
        return None

    def reset_state(self) -> None:
        self._history = np.zeros((2, self._latency), dtype=np.float32)


class _PulseDecoder:
    """绝对样本位置 ``impulse_at`` 处发一个双声道脉冲的解码器。

    按调用顺序返回连续块（状态化）——任意块长下与输出写入位置一致。
    幅度默认 0.4：两轨重合 0.8、三轨重合 0.75，避开 Master 软限幅器
    （threshold=1.0）的非线性区，保证求和线性可断言。
    """

    _channels = 2

    def __init__(self, impulse_at: int, amp: float = 0.4) -> None:
        self._impulse_at = int(impulse_at)
        self._amp = float(amp)
        self._pos = 0

    def decode_samples_for_frame(self, frame, fps, num_samples):
        block = np.zeros((2, num_samples), dtype=np.float32)
        lo, hi = self._pos, self._pos + num_samples
        if lo <= self._impulse_at < hi:
            block[:, self._impulse_at - lo] = self._amp
        self._pos += num_samples
        return block

    def open(self, *a, **k):
        return True

    def close(self):
        pass


def _mixer(*track_specs: tuple[str, int], routing: dict | None = None,
           amp: float = 0.4) -> AudioMixer:
    """按 (track_id, impulse_at) 建轨；routing: {tid: (bus_id|None, is_bus)}。"""
    mixer = AudioMixer()
    routing = routing or {}
    for track_id, impulse in track_specs:
        mixer.add_track(track_id, _PulseDecoder(impulse, amp),
                        AudioTrackState(track_id=track_id))
        bus_id, is_bus = routing.get(track_id, (None, False))
        mixer.set_track_routing(track_id, bus_id, [], is_bus=is_bus)
    return mixer


def _run_realtime(mixer: AudioMixer, total: int, block_len: int) -> np.ndarray:
    out = np.zeros((2, total), dtype=np.float32)
    pos = 0
    frame = 0
    while pos < total:
        n = min(block_len, total - pos)
        chunk = mixer.mix_samples(frame, float(FPS), n)[0]
        out[:, pos:pos + n] = chunk
        pos += n
        frame += 1
    return out


def _pulse_peaks(out: np.ndarray, threshold: float = 0.15) -> list[tuple[int, float]]:
    """输出中的脉冲 [(落点, 峰值幅度)]（相邻合并）。"""
    amps = np.max(np.abs(out), axis=0)
    peaks: list[tuple[int, float]] = []
    for i in np.flatnonzero(amps > threshold):
        p, a = int(i), float(amps[i])
        if peaks and p - peaks[-1][0] <= 2:
            peaks[-1] = (peaks[-1][0], max(peaks[-1][1], a))
        else:
            peaks.append((p, a))
    return peaks


class TestMixerPdcAlignment:
    def test_dry_and_latency_track_impulses_align(self) -> None:
        # 两轨脉冲都在 1000；lat 链延迟 1200 → 全图推迟到 2200 且重合
        mixer = _mixer(("dry", 1000), ("lat", 1000))
        mixer._effect_chains["lat"] = _LatencyFx(1200)
        mixer._recompute_pdc_locked()
        assert mixer.pdc_total_samples() == 1200
        assert mixer._pdc_delay == {"dry": 1200}
        out = _run_realtime(mixer, SR, BLOCK)
        assert _pulse_peaks(out) == [(2200, pytest.approx(0.8, abs=1e-5))]

    def test_alignment_matches_offline_renderer(self) -> None:
        latency = 1200
        mixer = _mixer(("dry", 1000), ("lat", 1000))
        mixer._effect_chains["lat"] = _LatencyFx(latency)
        mixer._recompute_pdc_locked()
        realtime = _run_realtime(mixer, SR, BLOCK)

        dry = SimpleNamespace(
            track_id="dry", name="Dry", track_type="audio", index=0,
            clips=(), volume=1.0, pan=0.0, is_bus=False, output_bus_id=None,
            effect_chain=None, sends=(), enabled=True, muted=False, solo=False)
        lat = SimpleNamespace(
            track_id="lat", name="Lat", track_type="audio", index=1,
            clips=(), volume=1.0, pan=0.0, is_bus=False, output_bus_id=None,
            effect_chain=None, sends=(), enabled=True, muted=False, solo=False)
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(SimpleNamespace(
            audio_tracks=(dry, lat), master_volume=1.0, master_muted=False))
        renderer.get_node("lat").effect_chain = _LatencyFx(latency)
        renderer._recompute_pdc()
        offline = np.zeros((2, SR), dtype=np.float32)
        pos = 0
        while pos < SR:
            n = min(BLOCK, SR - pos)
            block = np.zeros((2, n), dtype=np.float32)
            if pos <= 1000 < pos + n:
                block[:, 1000 - pos] = 0.4
            for track_id in ("dry", "lat"):
                processed = renderer.render_track_block(track_id, block, 0)
                if processed is not None:
                    # B02：与生产导出同一 seam——主路边延迟在路由点应用；
                    # route_track_output 写 output[:, :n]，传滑动视图。
                    renderer.route_track_output(track_id, processed,
                                                offline[:, pos:pos + n], {})
            pos += n
        assert _pulse_peaks(realtime) == _pulse_peaks(offline)

    @pytest.mark.parametrize("block_len", [733, 4001, BLOCK])
    def test_block_length_independence(self, block_len: int) -> None:
        mixer = _mixer(("dry", 1000), ("lat", 1000))
        mixer._effect_chains["lat"] = _LatencyFx(1500)
        mixer._recompute_pdc_locked()
        out = _run_realtime(mixer, SR, block_len)
        assert _pulse_peaks(out) == [(1000 + 1500, pytest.approx(0.8, abs=1e-5))]

    def test_muted_blocks_flush_delay_line(self) -> None:
        # 干轨链延迟 300（PDC 补 900）：脉冲进入延迟线后静音该轨——
        # 静音块推进延迟线把缓冲内容推出（丢弃），重新激活后无迟到爆发。
        mixer = _mixer(("dry", 1000), ("lat", 1000))
        mixer._effect_chains["dry"] = _LatencyFx(300)
        mixer._effect_chains["lat"] = _LatencyFx(1200)
        mixer._recompute_pdc_locked()
        assert mixer._pdc_delay.get("dry") == 900
        out = _run_realtime(mixer, SR, BLOCK)
        # 两轨脉冲对齐在 1000+1200（链群延迟与 PDC 补偿之和）
        assert _pulse_peaks(out) == [(2200, pytest.approx(0.8, abs=1e-5))]
        # 重跑：脉冲进入 dry 延迟线后立刻静音 5 块（>>900 样本缓冲）
        mixer2 = _mixer(("dry", 1000), ("lat", 1000))
        mixer2._effect_chains["dry"] = _LatencyFx(300)
        mixer2._effect_chains["lat"] = _LatencyFx(1200)
        mixer2._recompute_pdc_locked()
        mixer2.mix_samples(0, float(FPS), BLOCK)  # 脉冲进入 dry 延迟线
        mixer2.set_track_state("dry", AudioTrackState(track_id="dry", muted=True))
        for f in range(1, 6):
            mixer2.mix_samples(f, float(FPS), BLOCK)
        mixer2.set_track_state("dry", AudioTrackState(track_id="dry"))
        rest = np.zeros((2, SR - 6 * BLOCK), dtype=np.float32)
        pos = 6 * BLOCK
        frame = 6
        while pos < SR:
            chunk = mixer2.mix_samples(frame, float(FPS), BLOCK)[0]
            rest[:, pos - 6 * BLOCK:pos - 6 * BLOCK + BLOCK] = chunk
            pos += BLOCK
            frame += 1
        # 缓冲内容在静音期间被推出：解禁后无任何迟到爆发
        assert _pulse_peaks(rest) == []

    def test_zero_latency_graph_passes_through(self) -> None:
        mixer = _mixer(("dry", 1000), ("lat", 1000))
        assert mixer.pdc_total_samples() == 0
        assert mixer._pdc_delay == {}
        out = _run_realtime(mixer, SR, BLOCK)
        assert _pulse_peaks(out) == [(1000, pytest.approx(0.8, abs=1e-5))]

    def test_bus_level_delay_alignment(self) -> None:
        # 延迟轨与干轨同入 Bus（Bus 无链）：轨级对齐后经 Bus 输出；
        # 对照轨 dry2 直通 Master——三路脉冲同点。
        mixer = _mixer(("dry", 1000), ("lat", 1000), ("dry2", 1000),
                       routing={"dry": ("bus", False), "lat": ("bus", False),
                                "bus": (None, True)}, amp=0.25)
        mixer._effect_chains["lat"] = _LatencyFx(900)
        mixer._recompute_pdc_locked()
        assert mixer.pdc_total_samples() == 900
        out = _run_realtime(mixer, SR, BLOCK)
        assert _pulse_peaks(out) == [(1900, pytest.approx(0.75, abs=1e-5))]

    def test_seek_resets_delay_lines(self) -> None:
        mixer = _mixer(("dry", 1000), ("lat", 1000))
        mixer._effect_chains["lat"] = _LatencyFx(1200)
        mixer._recompute_pdc_locked()
        mixer.mix_samples(0, float(FPS), BLOCK)
        assert mixer._pdc_lines, "延迟线应在首个块建立缓冲"
        mixer.reset_dsp_states()
        assert mixer._pdc_lines == {}

    def test_chain_change_recomputes_delay_map(self) -> None:
        mixer = _mixer(("dry", 1000), ("lat", 1000))
        mixer._effect_chains["lat"] = _LatencyFx(1200)
        mixer._recompute_pdc_locked()
        assert mixer._pdc_delay == {"dry": 1200}
        # dry 也挂一条更短的延迟链：两条都留缓冲，最迟路径仍是 lat
        mixer._effect_chains["dry"] = _LatencyFx(600)
        mixer.set_track_routing("dry", None, [], is_bus=False)  # 触发重算
        assert mixer._pdc_delay == {"dry": 600}
        assert mixer.pdc_total_samples() == 1200


def test_exciter_wet_automation_keeps_live_and_export_tracks_aligned() -> None:
    from src.features.edit.core.timeline_v2 import (
        EditTrack, EffectChainParams, Keyframe, KeyframeTrack, TimelineSequence,
        TrackEffectInstance, build_track_fx_sampler,
    )

    chain = EffectChainParams((TrackEffectInstance("exciter", {
        "ex_quality": "clean", "ex_oversampling": 2, "ex_wet": 0.0,
        "ex_bands": [{"enabled": True, "amount": .5}],
    }, instance_id="exciter"),))
    param = "fx/exciter/ex_wet"
    wet = KeyframeTrack(param_name=param, min_val=0.0, max_val=1.0)
    wet.add_keyframe(Keyframe(0, 0.0))
    wet.add_keyframe(Keyframe(1, 1.0))
    tracks = (
        EditTrack(track_id="dry", track_type="audio", index=0),
        EditTrack(track_id="fx", track_type="audio", index=1, effect_chain=chain,
                  automation_mode="read", automation={param: wet}),
    )
    renderer = AudioGraphRenderer(SR)
    renderer.configure_from_sequence(TimelineSequence(audio_tracks=tracks))
    position = BLOCK + 100
    mixer = _mixer(("dry", position), ("fx", position), amp=.2)
    mixer.set_track_effect_chain("fx", chain, build_track_fx_sampler(tracks[1]))
    assert mixer.pdc_total_samples() == 20

    live = _run_realtime(mixer, 2 * BLOCK, BLOCK)
    offline = np.zeros_like(live)
    for frame, output in enumerate(np.split(offline, 2, axis=1)):
        pcm = np.zeros_like(output)
        if frame == 1:
            pcm[:, 100] = .2
        for track in tracks:
            block = renderer.render_track_block(track.track_id, pcm.copy(), frame)
            renderer.route_track_output(track.track_id, block, output, {})
    np.testing.assert_array_equal(live, offline)
    assert [index for index, _ in _pulse_peaks(live)] == [position + 20]
