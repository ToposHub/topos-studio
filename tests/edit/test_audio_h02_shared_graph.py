"""H02：共享图分块调度、Bus/Master 自动化与非零选区预卷（复审 §5 H02）。

修前复现：
- 相同电平曲线（f0=0、f10=−12dB）在轨级成片/Stem 正确（f20≈0.0502377），
  Bus/Master 却整段冻结在起点值 ~0.2——后级对整段 buffer 只按 start_frame
  采一次；
- Delay 从头渲染再裁 f4..10 峰 0.3440148，直接导出 f4..10 全零——
  选区前状态缺失。

修后契约：
- Bus/Master 阶段逐帧块内处理（fx 参数自动化/电平/静音按帧块起始帧求值，
  与实时帧粒度一致）；
- 非零 start_frame 按全图 FX 尾音预算确定性预卷（feed 坐标），交付裁掉
  预卷区——选区输出与"从头渲染再裁剪"一致。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from src.features.edit.core.audio_playback import AudioDecoder
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.stem_export import StemSpec, StemType, render_stem_pcm
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
from src.shared.keyframe import Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core
FPS = 25
SR = 48000
EXPECT_F20 = 0.0502377286301916  # 0.2 × 10^(-12/20)：f10=−12dB 后的电平


def _delay_chain(iid: str = "D", wet: float = 0.0) -> EffectChainParams:
    """wet=0 的纯电平链（automation 载体）。"""
    return EffectChainParams(effects=(
        TrackEffectInstance("delay", {
            "enabled": True, "left_delay": 200.0, "right_delay": 200.0,
            "left_feedback": 0.0, "right_feedback": 0.0, "dry_wet": wet,
            "level_db": 0.0, "low_cut": 20.0, "high_cut": 20000.0,
            "high_ratio": 0.0, "stereo_blend": 0.0, "input_format": "Stereo",
        }, instance_id=iid),
    ))


def _level_curve() -> KeyframeTrack:
    curve = KeyframeTrack(param_name="fx/D/level_db",
                          min_val=-24.0, max_val=6.0)
    curve.add_keyframe(Keyframe(frame=0.0, value=0.0))
    curve.add_keyframe(Keyframe(frame=10.0, value=-12.0))
    return curve


class _ConstDecoder(AudioDecoder):
    """常数 0.2 立体声、clip 限定帧内有效。"""

    _channels = 2

    def __init__(self, clip_start: int = 0, clip_end: int = 25,
                 amp: float = 0.2, *a, **k):
        self._lo, self._hi, self._amp = clip_start, clip_end, amp

    def decode_samples_for_frame(self, frame, fps, num_samples):
        if not (self._lo <= frame < self._hi):
            return np.zeros((2, num_samples), dtype=np.float32)
        t = (frame + np.arange(num_samples) / max(1.0, num_samples)) / max(1e-6, fps)
        sig = (self._amp * np.sin(2 * np.pi * 250.0 * t)).astype(np.float32)
        return np.stack([sig, sig])

    def open(self, *a, **k):
        return True

    def close(self):
        pass


def _project(tmp: Path, name: str) -> EditProject:
    return EditProject(name, name, assets={
        "a": MediaAsset("a", str(tmp / f"{name}.wav"), "audio", "A", {}),
    })


def _export(exporter, s: int, e: int, **kw) -> np.ndarray:
    out = exporter._render_audio_master(
        s, e, TimelineExportConfig(output_path="/tmp/h02.mov", **kw),
        encoder=Mock())
    assert out is not None
    return np.asarray(out)


class TestBusMasterAutomation:
    def _seq(self, location: str) -> TimelineSequence:
        auto = {"fx/D/level_db": _level_curve()}
        t = EditTrack("T", "T", "audio", 0,
                      clips=(ClipInstance("c", "a", "T", 0, 25, 0, 25),))
        if location == "master":
            return TimelineSequence(
                fps=Rational(FPS, 1), audio_tracks=(t,),
                master_effect_chain=_delay_chain(),
                master_automation=auto, master_automation_mode="read")
        bus = EditTrack("B", "B", "audio", 1, is_bus=True,
                        effect_chain=_delay_chain("D"),
                        automation=auto, automation_mode="read")
        return TimelineSequence(
            fps=Rational(FPS, 1),
            audio_tracks=(t.with_output_bus("B"), bus))

    @pytest.mark.parametrize("location", ["bus", "master"])
    def test_export_follows_curve(self, tmp_path, location):
        import src.features.edit.core.audio_playback as ap
        seq = self._seq(location)
        exporter = TimelineExporter(_project(tmp_path, location), seq, Mock())
        orig = ap.AudioDecoder
        ap.AudioDecoder = _ConstDecoder
        try:
            out = _export(exporter, 0, 25)
        finally:
            ap.AudioDecoder = orig
        f0 = float(np.max(np.abs(out[:, : SR // FPS])))
        f20 = float(np.max(np.abs(out[:, 20 * (SR // FPS):21 * (SR // FPS)])))
        assert f0 == pytest.approx(0.2, abs=5e-3)
        assert f20 == pytest.approx(EXPECT_F20, abs=5e-4), location

    @pytest.mark.parametrize("location", ["bus", "master"])
    def test_stem_follows_curve(self, tmp_path, location):
        seq = self._seq(location)
        stem = render_stem_pcm(
            seq, StemSpec(StemType.MASTER, "M", (), include_master=True),
            decode_clip_fn=lambda t, c, f, fp, n:
            _ConstDecoder(0, 25).decode_samples_for_frame(f, fp, n))
        f20 = float(np.max(np.abs(
            stem[:, 20 * (SR // FPS):21 * (SR // FPS)])))
        assert f20 == pytest.approx(EXPECT_F20, abs=5e-4), location


class TestNonZeroSelectionPreRoll:
    def _seq(self) -> TimelineSequence:
        # 脉冲（前 8 样本）→ 200ms Delay（wet 100%）；echo 落在 f4 之后
        t = EditTrack("T", "T", "audio", 0,
                      clips=(ClipInstance("c", "a", "T", 0, 3, 0, 3),),
                      effect_chain=EffectChainParams(effects=(
                          TrackEffectInstance("delay", {
                              "enabled": True, "left_delay": 200.0,
                              "right_delay": 200.0, "left_feedback": 0.0,
                              "right_feedback": 0.0, "dry_wet": 100.0,
                              "level_db": 0.0, "low_cut": 20.0,
                              "high_cut": 20000.0, "high_ratio": 0.0,
                              "stereo_blend": 0.0,
                              "input_format": "Stereo"}),
                      )))
        return TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(t,))

    class _PulseDecoder(AudioDecoder):
        _channels = 2

        def __init__(self, *a, **k):
            pass

        def decode_samples_for_frame(self, frame, fps, num_samples):
            out = np.zeros((2, num_samples), dtype=np.float32)
            if frame == 0:
                out[:, :8] = np.hanning(8).astype(np.float32)
            return out

        def open(self, *a, **k):
            return True

        def close(self):
            pass

    def test_selected_equals_full_crop(self, tmp_path):
        import src.features.edit.core.audio_playback as ap
        seq = self._seq()
        exporter = TimelineExporter(_project(tmp_path, "sel"), seq, Mock())
        orig = ap.AudioDecoder
        ap.AudioDecoder = self._PulseDecoder
        try:
            full = _export(exporter, 0, 10)
            sel = _export(exporter, 4, 10)
        finally:
            ap.AudioDecoder = orig
        # 选区内容 == 从头渲染再裁剪（预卷重建了区间前 Delay 状态）
        np.testing.assert_allclose(sel, full[:, 4 * (SR // FPS):],
                                   atol=1e-6)
        assert float(np.abs(sel).max()) == pytest.approx(
            float(np.abs(full).max()), abs=1e-6)
        assert float(np.abs(sel).max()) > 0.3  # echo 存在（修前为 0）

    def test_zero_start_unchanged(self, tmp_path):
        """start_frame=0 无预卷：路径与旧实现一致（无前置零段）。"""
        import src.features.edit.core.audio_playback as ap
        seq = self._seq()
        exporter = TimelineExporter(_project(tmp_path, "z0"), seq, Mock())
        orig = ap.AudioDecoder
        ap.AudioDecoder = self._PulseDecoder
        try:
            out = _export(exporter, 0, 6)
        finally:
            ap.AudioDecoder = orig
        assert out.shape[-1] == 6 * (SR // FPS)


class TestFractionalFps:
    def test_2997_automation_and_length(self, tmp_path):
        """30000/1001 非整数帧率：长度精确 + 曲线仍逐帧求值。"""
        import src.features.edit.core.audio_playback as ap
        fps_num, fps_den = 30000, 1001
        t = EditTrack("T", "T", "audio", 0,
                      clips=(ClipInstance("c", "a", "T", 0, 25, 0, 25),))
        seq = TimelineSequence(
            fps=Rational(fps_num, fps_den), audio_tracks=(t,),
            master_effect_chain=_delay_chain(),
            master_automation={"fx/D/level_db": _level_curve()},
            master_automation_mode="read")
        exporter = TimelineExporter(_project(tmp_path, "ntsc"), seq, Mock())
        spf = SR * fps_den / fps_num
        orig = ap.AudioDecoder

        class _Dec(_ConstDecoder):
            def decode_samples_for_frame(self, frame, fps, num_samples):
                if not (0 <= frame < 25):
                    return np.zeros((2, num_samples), dtype=np.float32)
                t_ = frame / fps + np.arange(num_samples) / (fps * num_samples)
                sig = (0.2 * np.sin(2 * np.pi * 250.0 * t_)).astype(np.float32)
                return np.stack([sig, sig])

        ap.AudioDecoder = _Dec
        try:
            out = _export(exporter, 0, 25)
        finally:
            ap.AudioDecoder = orig
        assert out.shape[-1] == int(round(25 * spf))
        f20 = float(np.max(np.abs(out[:, int(20 * spf):int(21 * spf)])))
        assert f20 == pytest.approx(EXPECT_F20, abs=5e-4)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
