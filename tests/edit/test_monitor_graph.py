"""B09/B21/B10 核心回归：健康快照、渲染产物失效/打印锁、控制室监听。

- B09：AudioHealthSnapshot（unknown=None 语义、百分位、ring 统计注入、
  PDC/轨数遥测）。
- B21：RenderArtifact 往返、freeze_staleness 失效链、ArtifactRegistry
  原子持久化、PrintJobLock revision 锁。
- B10：MonitorService 监听隔离（Master/Stem 不被改写）、PFL/AFL/SIP 身份
  （997/1511Hz 频谱核对）、solo-safe 闭包、mono 矩阵、DIM/Mute 斜坡包络、
  零输入零输出；SetTrackSoloSafeCommand apply/undo + 序列化往返。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.audio_health import (
    AudioHealthSnapshot,
    render_percentiles_ms,
)
from src.features.edit.core.commands import SetTrackSoloSafeCommand
from src.features.edit.core.monitor_service import (
    MonitorService,
    MonitorSessionState,
    stereo_mono_matrix,
)
from src.features.edit.core.render_artifacts import (
    ArtifactRegistry,
    PrintJobLock,
    RenderArtifact,
    freeze_staleness,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


# ─────────────────────────────────────────────────────────────────────────────
# B09
# ─────────────────────────────────────────────────────────────────────────────

class TestAudioHealthSnapshot:
    def test_percentiles_unknown_when_empty(self) -> None:
        assert render_percentiles_ms(()) == (None, None, None, 0)
        p50, p95, p99, _ = render_percentiles_ms((5.0,) * 100)
        assert p50 == p95 == p99 == pytest.approx(5.0)

    def test_percentiles_ordered(self) -> None:
        data = tuple(float(i) for i in range(100))
        p50, p95, p99, _ = render_percentiles_ms(data)
        assert p50 <= p95 <= p99

    def test_mixer_health_snapshot_unknown_fields(self) -> None:
        """无播放线程/无 ring → 未知字段 None（不显示健康绿灯）。"""
        from src.features.edit.core.audio_playback import AudioMixer
        mixer = AudioMixer()
        snap = mixer.health_snapshot()
        assert isinstance(snap, AudioHealthSnapshot)
        assert snap.sample_rate == int(mixer._sample_rate)
        assert snap.buffer_size is None
        assert snap.ring_fill_ratio is None
        assert snap.underruns is None
        assert snap.pdc_total_samples == 0
        assert snap.sound_key_active is None
        assert snap.to_dict()["buffer_size"] is None

    def test_mixer_health_snapshot_counts_and_pdc(self) -> None:
        from src.features.edit.core.audio_playback import (
            AudioMixer,
            AudioTrackState,
        )

        class _Silent:
            _timeline_start = 0
            _timeline_duration = 48
            _channels = 2

            def decode_samples_for_frame(self, frame, fps, num_samples):
                return np.zeros((2, num_samples), dtype=np.float32)

            def close(self) -> None:
                pass

        mixer = AudioMixer()
        mixer.add_track("t1", _Silent(), AudioTrackState(track_id="t1"))
        mixer.set_track_routing("t1", None, [], is_bus=False)
        mixer.set_track_routing("bus", None, [], is_bus=True)
        mixer.set_active_sound_key("snd-abc")
        try:
            snap = mixer.health_snapshot()
            assert snap.active_tracks == 1
            assert snap.active_buses == 1
            assert snap.sound_key_active == "snd-abc"
            # 渲染两块后耗时分位出现
            mixer.mix_samples(0, 24.0, 480, 2)
            mixer.mix_samples(480, 24.0, 480, 2)
            snap2 = mixer.health_snapshot()
            assert snap2.render_p50_ms is not None
            assert snap2.render_p99_ms >= snap2.render_p50_ms
        finally:
            mixer.remove_track("t1")


# ─────────────────────────────────────────────────────────────────────────────
# B21
# ─────────────────────────────────────────────────────────────────────────────

def _artifact(**overrides) -> RenderArtifact:
    base = dict(
        artifact_id="art-1", kind="freeze", project_id="p", sequence_id="s",
        revision=7, sound_hash="snd-777", start_sample=0, sample_count=480,
        sample_rate=48000, channel_order=("L", "R"), tap="track:t1",
        pdc_total_samples=300, source_dependencies=("fx:comp:1", "route:t1->b1"),
        file_uri="freeze_captures/art-1.f32", file_sha256="ff00",
        status="ready", created_at=1700000000.0,
    )
    base.update(overrides)
    return RenderArtifact(**base)


class TestRenderArtifacts:
    def test_roundtrip(self) -> None:
        restored = RenderArtifact.from_dict(_artifact().to_dict())
        assert restored == _artifact()

    def test_invalid_kind_and_status(self) -> None:
        with pytest.raises(ValueError):
            _artifact(kind="bounce-in-place")
        with pytest.raises(ValueError):
            _artifact(status="done")

    def test_staleness_chain(self) -> None:
        art = _artifact()
        assert freeze_staleness(art, current_sound_hash="snd-777") is None
        assert freeze_staleness(art, current_sound_hash="snd-999") is not None
        assert freeze_staleness(
            art, current_sound_hash="snd-777",
            current_dependencies=("fx:comp:1",)) is not None
        assert freeze_staleness(
            art, current_sound_hash="snd-777", file_sha256="bad") is not None
        assert freeze_staleness(
            _artifact(status="failed"),
            current_sound_hash="snd-777") is not None

    def test_registry_atomic_roundtrip(self, tmp_path: Path) -> None:
        reg = ArtifactRegistry(tmp_path)
        reg.register(_artifact())
        reg.register(_artifact(artifact_id="art-2", kind="printmaster"))
        again = ArtifactRegistry(tmp_path)
        assert again.get("art-1").sound_hash == "snd-777"
        assert {a.kind for a in again.list()} == {"freeze", "printmaster"}
        assert again.remove("art-1")
        assert again.get("art-1") is None
        # 损坏 sidecar 不阻断（回空注册表）
        reg.path.write_text("{not json", encoding="utf-8")
        broken = ArtifactRegistry(tmp_path)
        assert broken.list() == ()

    def test_print_job_lock(self) -> None:
        lock = PrintJobLock(revision=12, sound_hash="snd-A")
        assert lock.verify_input("snd-A")
        assert not lock.verify_input("snd-B")  # 输入变化必须中止，不得混两版
        lock.finish()
        assert not lock.verify_input("snd-A")  # 完成后拒绝复用


# ─────────────────────────────────────────────────────────────────────────────
# B10
# ─────────────────────────────────────────────────────────────────────────────

def _taps() -> dict:
    """DX=997Hz(pre/post)、MX=1511Hz(post)、master 求和、input 噪声。

    4800 样本（0.1s）保证 FFT 分辨率 10Hz，997/1511 Hz 可分离。
    """
    n = 4800
    t = np.arange(n, dtype=np.float64) / 48000.0
    dx = np.sin(2 * np.pi * 997.0 * t).astype(np.float32) * 0.4
    mx = np.sin(2 * np.pi * 1511.0 * t).astype(np.float32) * 0.4
    return {
        "track:dx:pre": np.stack([dx, dx]),
        "track:dx:post": np.stack([dx * 0.5, dx * 0.5]),
        "track:mx:post": np.stack([mx, mx]),
        "master": np.stack([dx * 0.5 + mx, dx * 0.5 + mx]),
        "input:dev1": np.stack([mx * 0.1, mx * 0.1]),
    }


def _dominant_freq(block: np.ndarray, rate: int = 48000) -> float:
    spec = np.abs(np.fft.rfft(block[0].astype(np.float64)))
    freqs = np.fft.rfftfreq(len(block[0]), d=1.0 / rate)
    return float(freqs[int(np.argmax(spec))])


class TestMonitorService:
    def test_program_and_input_selection(self) -> None:
        svc = MonitorService(48000.0)
        taps = _taps()
        out = svc.render_block(taps)
        assert _dominant_freq(out) == pytest.approx(997.0, abs=12.0) or \
            _dominant_freq(out) == pytest.approx(1511.0, abs=12.0)
        # 输入监听：仅 input tap
        svc.set_state(MonitorSessionState(source_tap="input"))
        out = svc.render_block(taps, {"dev1": taps["input:dev1"]})
        assert _dominant_freq(out) == pytest.approx(1511.0, abs=12.0)
        # 节目未选 input
        svc.set_state(MonitorSessionState(source_tap="program"))
        out = svc.render_block(taps, {"dev1": taps["input:dev1"]})
        assert out.shape[0] == 2

    def test_pfl_vs_afl_tap_identity(self) -> None:
        taps = _taps()
        svc = MonitorService(48000.0)
        svc.set_state(MonitorSessionState(
            source_tap="pfl", selected_listen_ids=("dx",)))
        pfl = svc.render_block(taps)
        assert _dominant_freq(pfl) == pytest.approx(997.0, abs=12.0)
        # PFL 幅度 = 推子前（0.4）；AFL = 推子后（0.2）
        svc.set_state(MonitorSessionState(
            source_tap="afl", selected_listen_ids=("dx",)))
        afl = svc.render_block(taps)
        assert float(np.max(np.abs(pfl))) == pytest.approx(0.4, abs=1e-5)
        assert float(np.max(np.abs(afl))) == pytest.approx(0.2, abs=1e-5)

    def test_sip_closure_with_solo_safe(self) -> None:
        taps = _taps()
        svc = MonitorService(48000.0)
        svc.set_state(MonitorSessionState(source_tap="sip"))
        # DX solo；MX 无 safe → 不可听；DX solo_safe → 仍可听
        out = svc.render_block(taps, audible_solo={"mx"},
                               solo_safe=("dx",))
        spec = np.abs(np.fft.rfft(out[0].astype(np.float64)))
        freqs = np.fft.rfftfreq(out.shape[-1], d=1.0 / 48000.0)
        peak = freqs[int(np.argmax(spec))]
        # 可听集合 = {mx} ∪ {dx safe} → 两者都有；主峰在两者之一
        assert peak in (pytest.approx(997.0, abs=12.0),
                        pytest.approx(1511.0, abs=12.0))
        # 无 safe 时 SIP 只含 solo 闭包（mx）
        out2 = svc.render_block(taps, audible_solo={"mx"}, solo_safe=())
        assert _dominant_freq(out2) == pytest.approx(1511.0, abs=12.0)

    def test_monitor_never_mutates_program(self) -> None:
        """监听隔离不变量：Master/Stem 输入数组字节级不变。"""
        taps = _taps()
        master_before = taps["master"].copy()
        dx_post_before = taps["track:dx:post"].copy()
        svc = MonitorService(48000.0)
        for _ in range(3):
            svc.render_block(taps, audible_solo={"mx"}, solo_safe=("dx",))
        assert np.array_equal(taps["master"], master_before)
        assert np.array_equal(taps["track:dx:post"], dx_post_before)

    def test_dim_mute_and_gain_math(self) -> None:
        taps = _taps()
        svc = MonitorService(48000.0)
        svc.set_state(MonitorSessionState(dim=True))
        svc.render_block(taps)                # 首块为斜坡过渡
        out = svc.render_block(taps)          # 第二块达稳态
        input_peak = float(np.max(np.abs(taps["master"][0])))
        expected = 10.0 ** (-20.0 / 20.0)
        assert float(np.max(np.abs(out[0]))) == pytest.approx(
            input_peak * expected, rel=1e-3)
        svc.set_state(MonitorSessionState(muted=True))
        svc.render_block(taps)                # 斜坡过渡
        out = svc.render_block(taps)          # 稳态全零
        assert float(np.max(np.abs(out))) == 0.0

    def test_ramp_envelope_exact(self) -> None:
        """5ms 斜坡严格符合生成的包络（前 ramp 渐变、后段稳定）。"""
        n = 480
        block = np.full((2, n), 0.5, dtype=np.float32)
        svc = MonitorService(48000.0)
        svc.set_state(MonitorSessionState())  # gain 1.0
        svc.render_block({"master": block.copy()})
        svc.set_state(MonitorSessionState(muted=True))  # 目标 0（触发斜坡）
        out = svc.render_block({"master": block.copy()})
        ramp_len = int(48000 * 5.0 / 1000.0)
        # 斜坡单调不增，末段为 0
        mono = np.abs(out[0].astype(np.float64))
        assert np.all(np.diff(mono[:ramp_len]) <= 1e-9)
        assert mono[ramp_len:] == pytest.approx(0.0, abs=1e-9)
        # 下一块稳定为 0（斜坡结束）
        out2 = svc.render_block({"master": block.copy()})
        assert float(np.max(np.abs(out2))) == 0.0

    def test_zero_input_zero_output(self) -> None:
        svc = MonitorService(48000.0)
        out = svc.render_block({"master": np.zeros((2, 480), np.float32)})
        assert float(np.max(np.abs(out))) == 0.0

    def test_mono_matrix_semantics(self) -> None:
        taps = {"master": np.stack([
            np.full(480, 0.6, np.float32),
            np.full(480, -0.2, np.float32)])}
        svc = MonitorService(48000.0)
        svc.set_state(MonitorSessionState(mono=True))
        out = svc.render_block(taps)
        assert float(out[0, 0]) == pytest.approx(0.2, abs=1e-6)
        assert float(out[1, 0]) == pytest.approx(0.2, abs=1e-6)
        # 非 stereo 无注册矩阵 → 显式拒绝（不猜 mean）
        taps51 = {"master": np.zeros((6, 480), np.float32)}
        with pytest.raises(ValueError, match="matrix"):
            svc.render_block(taps51)
        svc.register_mono_matrix("m51", np.ones((2, 6), np.float64) / 6.0)
        svc.set_state(MonitorSessionState(mono=True, mono_matrix_id="m51"))
        out = svc.render_block(taps51)
        assert out.shape[0] == 2

    def test_missing_tap_is_diagnostic_not_crash(self) -> None:
        svc = MonitorService(48000.0)
        svc.set_state(MonitorSessionState(
            source_tap="pfl", selected_listen_ids=("ghost",)))
        out = svc.render_block(_taps())
        assert out.size > 0  # 空选择 → 静音块，不异常


class TestSoloSafeCommand:
    def _timeline(self) -> TimelineSequence:
        clip = ClipInstance(clip_id="c", asset_id="a", track_id="t",
                            timeline_start=0, timeline_duration=48,
                            source_start=0, source_duration=48)
        return TimelineSequence(audio_tracks=(
            EditTrack("t", "T", "audio", 0, clips=(clip,)),))

    def test_apply_undo_and_serialization(self) -> None:
        timeline = self._timeline()
        cmd = SetTrackSoloSafeCommand.create("t", True)
        new_timeline, applied = cmd.apply(timeline)
        assert new_timeline.get_track_by_id("t").solo_safe is True
        restored = applied.undo(new_timeline)
        assert restored.get_track_by_id("t").solo_safe is False
        # 序列化往返（宏命令反序列化分发）
        payload = SetTrackSoloSafeCommand.from_dict(applied.to_dict())
        assert payload.track_id == "t" and payload.safe is True

    def test_track_roundtrip_preserves_solo_safe(self) -> None:
        track = EditTrack("t", "T", "audio", 0).with_solo_safe(True)
        data = track.to_dict()
        assert data["solo_safe"] is True
        restored = EditTrack.from_dict(data)
        assert restored.solo_safe is True
        # 旧工程无字段 → False（迁移零变化）
        legacy = dict(data)
        legacy.pop("solo_safe")
        assert EditTrack.from_dict(legacy).solo_safe is False
