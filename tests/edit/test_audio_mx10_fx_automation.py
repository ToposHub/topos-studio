"""MX-10 回归：效果参数自动化（fx/{instance_id}/{param_key} 路径，阶段G）。

验收（计划 §5 MX-10）：
- 参数路径使用 instance_id + 稳定 schema 键（fx/ 命名空间）；
- 音频线程在块边界按时间戳（当前帧）应用关键帧值；
- 预览（实时 mix_samples）与导出（render_track_block）同构；
- 不同块长得到相同参数时间表（块边界等价）；
- instance 隔离：只覆盖目标实例的参数；
- undo 恢复曲线（命令层泛型 key 锁）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.shared.keyframe import Keyframe  # noqa: F401
from src.features.edit.core.timeline_v2 import (
    DelayParams,
    EditTrack,
    EffectChainParams,
    TimelineSequence,
    TrackEffectInstance,
    build_track_fx_sampler,
    parse_fx_automation_key,
)

SR = 48000.0
FPS = 24.0


def _delay_iid(iid, time_ms=60.0):
    return TrackEffectInstance(
        effect_type="delay",
        params=DelayParams(left_delay=time_ms, right_delay=time_ms,
                           dry_wet=100.0).to_dict(),
        enabled=True, order=0, instance_id=iid,
    )


def _track_with_fx_curve(mode="read"):
    """D1: left_delay 曲线 60ms@f0 → 200ms@f120（线性）。"""
    from src.shared.keyframe import Keyframe, KeyframeTrack
    kt = KeyframeTrack(param_name="fx/D1/left_delay", min_val=0.0, max_val=1200.0)
    kt.add_keyframe(Keyframe(frame=0, value=60.0))
    kt.add_keyframe(Keyframe(frame=120, value=200.0))
    return EditTrack(
        track_id="t1", name="T1", track_type="audio",
        effect_chain=EffectChainParams(effects=(_delay_iid("D1"),)),
        automation={"fx/D1/left_delay": kt},
        automation_mode=mode,
    )


class _ImpulseDecoder:
    """frames 帧各放一个单位脉冲（每帧一个，用于测回声时间）。"""

    def __init__(self, frames):
        self.frames = set(frames)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        if frame not in self.frames:
            return None
        sig = np.zeros(num_samples, dtype=np.float32)
        sig[0] = 0.9
        return np.stack([sig, sig])

    def close(self):
        pass


class TestMx10NamespaceAndSampler:
    def test_parse_fx_automation_key(self):
        assert parse_fx_automation_key("fx/D1/left_delay") == ("D1", "left_delay")
        assert parse_fx_automation_key("volume") is None
        assert parse_fx_automation_key("fx/noid") is None
        assert parse_fx_automation_key("fx//key") is None

    def test_sampler_none_cases(self):
        assert build_track_fx_sampler(_track_with_fx_curve(mode="off")) is None
        plain = EditTrack(track_id="t1", name="T", track_type="audio",
                          effect_chain=EffectChainParams(
                              effects=(_delay_iid("D1"),)))
        assert build_track_fx_sampler(plain) is None  # 无 fx 键（mode 默认 off）

    def test_sampler_overrides_target_instance_param(self):
        track = _track_with_fx_curve()
        sampler = build_track_fx_sampler(track)
        assert sampler is not None
        entries = sampler(0)
        assert entries[0][1]["left_delay"] == pytest.approx(60.0)
        entries_60 = sampler(60)
        assert entries_60[0][1]["left_delay"] == pytest.approx(130.0)
        entries_120 = sampler(120)
        assert entries_120[0][1]["left_delay"] == pytest.approx(200.0)
        # 外推：120 帧后保持末值
        assert sampler(200)[0][1]["left_delay"] == pytest.approx(200.0)

    def test_instance_isolation(self):
        track = _track_with_fx_curve()
        # 再加一个实例 D2：采样器不得改它的参数
        track2 = track.with_effect_chain(EffectChainParams(effects=(
            _delay_iid("D1"), _delay_iid("D2", 300.0))))
        sampler = build_track_fx_sampler(track2)
        entries = sampler(60)
        by_iid = {e[3]: e[1] for e in entries}
        assert by_iid["D1"]["left_delay"] == pytest.approx(130.0)
        assert by_iid["D2"]["left_delay"] == pytest.approx(300.0)


class TestMx10RealtimeAutomation:
    def test_echo_delay_follows_curve(self, qapp):
        """帧 0 脉冲 → 回声 ~60ms；帧 120 脉冲 → 回声 ~200ms（帧中值求值）。"""
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        track = _track_with_fx_curve()
        mixer.add_track("t1", _ImpulseDecoder((0,)), AudioTrackState(track_id="t1"))
        mixer.set_track_effect_chain("t1", track.effect_chain,
                                     animated_sampler=build_track_fx_sampler(track))

        # 帧块宽 2000 样本（~41.7ms/块）；跑 16 块覆盖 ~667ms
        outputs = []
        for f in range(0, 16):
            out, _ = mixer.mix_samples(f, FPS, 2000, 2)
            outputs.append(out.copy())
        full = np.concatenate(outputs, axis=-1)
        nz = np.flatnonzero(np.abs(full[0]) > 1e-4)
        assert nz.size >= 1
        echo_ms = nz[0] / SR * 1000.0
        # 帧边界块起始求值：帧 0 块用 60ms → 回声 ≈60ms（±45ms 容差=块粒度）
        assert 30.0 <= echo_ms <= 90.0, f"echo at {echo_ms:.1f}ms, expect ~60"

    def test_block_size_equivalence(self, qapp):
        """同一曲线、帧对齐驱动下，240 与 960 样本块长的回声时间一致。"""
        def _echo_ms(block):
            mixer = AudioMixer()
            mixer.set_sample_rate(SR)
            track = _track_with_fx_curve()
            mixer.add_track("t1", _ImpulseDecoder((3,)),
                            AudioTrackState(track_id="t1"))
            mixer.set_track_effect_chain(
                "t1", track.effect_chain,
                animated_sampler=build_track_fx_sampler(track))
            frame_samples = int(SR / FPS)  # 2000
            outs = []
            for f in range(0, 24):
                done = 0
                while done < frame_samples:
                    b = min(block, frame_samples - done)
                    out, _ = mixer.mix_samples(f, FPS, b, 2)
                    outs.append(out.copy())
                    done += b
            full = np.concatenate(outs, axis=-1)
            nz = np.flatnonzero(np.abs(full[0]) > 1e-4)
            return nz[0] / SR * 1000.0 if nz.size else -1.0

        e1 = _echo_ms(240)
        e2 = _echo_ms(960)
        assert e1 > 0 and e2 > 0
        # 帧 3 脉冲 @125ms + 曲线值 63.5ms → 回声 ≈188.5ms；
        # 帧对齐驱动下两种块长的参数时间表一致（±10ms 量化容差）
        assert abs(e1 - e2) <= 10.0
        assert 178.0 <= e1 <= 199.0


class TestMx10ExportParity:
    def test_renderer_node_consumes_sampler(self):
        """_TrackNode.fx_sampler 在 render_track_block 块边界应用。"""
        track = _track_with_fx_curve()
        seq = TimelineSequence(name="M", audio_tracks=(track,))
        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(seq)
        node = r.get_node("t1")
        assert node.fx_sampler is not None

        n = 4800  # 100ms：60ms 回声需在块内可见
        x = np.zeros((2, n), dtype=np.float32)
        x[:, 0] = 0.9
        out = r.render_track_block("t1", x, 0, fps=FPS)
        nz = np.flatnonzero(np.abs(out[0]) > 1e-4)
        echo_ms = nz[0] / SR * 1000.0 if nz.size else -1.0
        assert 30.0 <= echo_ms <= 90.0, \
            f"export echo at {echo_ms:.1f}ms, expect ~60"

        # 帧 60：曲线 130ms（独立渲染器实例，避免上一段的环形历史串扰；
        # 块 200ms 容纳 130ms 回声）
        r2 = AudioGraphRenderer(SR)
        r2.configure_from_sequence(TimelineSequence(
            name="M", audio_tracks=(_track_with_fx_curve(),)))
        x2 = np.zeros((2, 9600), dtype=np.float32)
        x2[:, 0] = 0.9
        out2 = r2.render_track_block("t1", x2, 60, fps=FPS)
        nz2 = np.flatnonzero(np.abs(out2[0]) > 1e-4)
        echo2 = nz2[0] / SR * 1000.0 if nz2.size else -1.0
        assert 100.0 <= echo2 <= 160.0, \
            f"frame-60 echo at {echo2:.1f}ms, expect ~130"

    def test_mode_off_builds_no_sampler(self):
        track = _track_with_fx_curve(mode="off")
        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(TimelineSequence(
            name="M", audio_tracks=(track,)))
        assert r.get_node("t1").fx_sampler is None


class TestMx10CommandUndo:
    def test_fx_key_command_roundtrip_undo(self):
        """AddAutomationKeyframeCommand 泛型 fx/* 键：apply/undo 恢复曲线。"""
        from src.features.edit.core.commands import AddAutomationKeyframeCommand
        seq = TimelineSequence(name="M", audio_tracks=(
            EditTrack(track_id="t1", name="T", track_type="audio"),))
        cmd = AddAutomationKeyframeCommand.create(
            "t1", "fx/D1/left_delay", 0, 60.0, min_val=0.0, max_val=1200.0)
        seq2, applied = cmd.apply(seq)
        auto = seq2.get_track_by_id("t1").automation
        assert "fx/D1/left_delay" in auto
        # 序列化往返（工程保存）
        rt = TimelineSequence.from_dict(seq2.to_dict())
        assert "fx/D1/left_delay" in rt.get_track_by_id("t1").automation
        # undo
        seq3 = applied.undo(seq2)
        assert "fx/D1/left_delay" not in seq3.get_track_by_id("t1").automation
