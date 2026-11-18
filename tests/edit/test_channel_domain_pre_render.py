"""P0 · 声道与源 patch 的先后顺序（2026-11-19 计划第一阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- 解码保留源布局与通道名；>2ch 经 BS.775 矩阵折到工作域（LFE 显式
  排除），不再由重采样器 naive 混声；
- 双单声道 BWF 独立脉冲零串漏；5.1 与 7.1 one-hot 脉冲按 ITU 系数
  落位（L/R 直通、C=0.707 双侧、LFE 不外溢、Ls/Rs=0.707 单侧）；
- Poly WAV 逐声道选择（decode 的 channel_map / 计划的源 patch）；
- PreRenderSpec 记录 codec 布局、源 patch 与处理前后域，参与缓存键。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core import pcm_render_cache as prc
from src.features.edit.core.clip_pcm import decode_audio_range

SR = 48000
SQ = 0.5 ** 0.5


def _write_wav(path: Path, pcm: np.ndarray, layout: str) -> Path:
    """多声道 16-bit WAV（stdlib wave，交错）——layout 参数仅为可读性。"""
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(pcm.shape[0])
        w.setsampwidth(2)
        w.setframerate(SR)
        interleaved = (np.clip(pcm.T, -1.0, 1.0) * 32767.0).astype("<i2")
        w.writeframes(interleaved.tobytes())
    return path


def _sine(freq: float, seconds: float = 0.5, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _band_energy(sig: np.ndarray, freq: float, bw: float = 60.0) -> float:
    spec = np.abs(np.fft.rfft(sig * np.hanning(sig.shape[0])))
    freqs = np.fft.rfftfreq(sig.shape[0], 1.0 / SR)
    mask = np.abs(freqs - freq) <= bw
    return float(np.max(spec[mask])) if mask.any() else 0.0


# 5.1 声道顺序：[L, R, C, LFE, Ls, Rs]
_FREQS_51 = [220.0, 330.0, 440.0, 550.0, 660.0, 770.0]


def _one_hot_51(freqs=None) -> np.ndarray:
    freqs = freqs or _FREQS_51
    n = int(0.5 * SR)
    out = np.zeros((6, n), dtype=np.float32)
    for ch, f in enumerate(freqs):
        out[ch] = _sine(f, 0.5)
    return out


class TestDualMonoBwf:
    def test_independent_pulses_no_crosstalk(self, tmp_path) -> None:
        left = _sine(440.0, 0.5, 0.8)
        right = _sine(880.0, 0.5, 0.3)
        wav = _write_wav(tmp_path / "dual.wav", np.stack([left, right]),
                         "stereo")
        pcm = decode_audio_range(str(wav), 0, SR // 2, SR)
        assert pcm is not None and pcm.shape[0] == 2
        # 零串漏：左声道无 880 Hz、右声道无 440 Hz；各自频率完整保留。
        assert _band_energy(pcm[0], 880.0) < 1e-3 * _band_energy(pcm[0], 440.0)
        assert _band_energy(pcm[1], 440.0) < 1e-3 * _band_energy(pcm[1], 880.0)
        # 声道顺序（order）正确：L→0，R→1。
        assert _band_energy(pcm[0], 440.0) == pytest.approx(
            _band_energy(left, 440.0), rel=0.05)
        assert _band_energy(pcm[1], 880.0) == pytest.approx(
            _band_energy(right, 880.0), rel=0.05)


class TestSurroundOneHot:
    def test_5_1_one_hot_itu_coefficients(self, tmp_path) -> None:
        wav = _write_wav(tmp_path / "s51.wav", _one_hot_51(), "5.1")
        pcm = decode_audio_range(str(wav), 0, SR // 2, SR)
        assert pcm is not None and pcm.shape[0] == 2
        scale_l = _band_energy(_sine(440.0, 0.5, 0.5), 440.0)
        # 期望（BS.775，LFE 默认排除）。
        expected = [
            # (ch, Lo 系数, Ro 系数)
            (220.0, 1.0, 0.0),    # L
            (330.0, 0.0, 1.0),    # R
            (440.0, SQ, SQ),      # C
            (550.0, 0.0, 0.0),    # LFE
            (660.0, SQ, 0.0),     # Ls
            (770.0, 0.0, SQ),     # Rs
        ]
        for freq, lo_c, ro_c in expected:
            lo = _band_energy(pcm[0], freq) / scale_l
            ro = _band_energy(pcm[1], freq) / scale_l
            assert lo == pytest.approx(lo_c, abs=0.02), f"{freq} Hz Lo"
            assert ro == pytest.approx(ro_c, abs=0.02), f"{freq} Hz Ro"

    def test_7_1_side_surround_folds_to_front(self, tmp_path) -> None:
        n = int(0.5 * SR)
        pcm8 = np.zeros((8, n), dtype=np.float32)
        pcm8[6] = _sine(220.0, 0.5)   # Lss
        pcm8[7] = _sine(330.0, 0.5)   # Rss
        wav = _write_wav(tmp_path / "s71.wav", pcm8, "7.1")
        pcm = decode_audio_range(str(wav), 0, SR // 2, SR)
        assert pcm is not None and pcm.shape[0] == 2
        scale = _band_energy(_sine(220.0, 0.5, 0.5), 220.0)
        # 7.1→5.1→stereo 复合：Lss 只进 Lo（0.707），Rss 只进 Ro。
        assert _band_energy(pcm[0], 220.0) / scale == pytest.approx(SQ, abs=0.02)
        assert _band_energy(pcm[1], 220.0) / scale == pytest.approx(0.0, abs=0.02)
        assert _band_energy(pcm[1], 330.0) / scale == pytest.approx(SQ, abs=0.02)
        assert _band_energy(pcm[0], 330.0) / scale == pytest.approx(0.0, abs=0.02)


class TestPolyWavChannelSelection:
    def test_decode_channel_map_selects_source_channel(self, tmp_path) -> None:
        # 逐声道选择：Ls（下标 4）→ 双声道输出。
        wav = _write_wav(tmp_path / "poly.wav", _one_hot_51(), "5.1")
        pcm = decode_audio_range(
            str(wav), 0, SR // 2, SR,
            channel_map=((4, 0), (4, 1)))
        assert pcm is not None and pcm.shape[0] == 2
        scale = _band_energy(_sine(660.0, 0.5, 0.5), 660.0)
        for ch in (0, 1):
            assert _band_energy(pcm[ch], 660.0) / scale == pytest.approx(
                1.0, abs=0.02)
            # 其余源声道不出现。
            for f in (220.0, 330.0, 440.0, 550.0, 770.0):
                assert _band_energy(pcm[ch], f) / scale < 0.02

    def test_plan_carries_patch_into_spec_and_base(self, tmp_path) -> None:
        # 工程源 patch：选 C（下标 2）→ 基底内容 = C 广播到双声道。
        wav = _write_wav(tmp_path / "poly2.wav", _one_hot_51(), "5.1")
        asset = SimpleNamespace(
            asset_id="asset-1", file_path=str(wav), path=str(wav),
            media_type="audio", category="Media", fps=24.0,
            audio_channels=6, audio_channel_layout="5.1",
            duration_seconds=0.5)
        from src.features.edit.core.source_patch import SourcePatchSpec
        project = SimpleNamespace(
            assets={"asset-1": asset},
            source_patch_overrides={
                "asset-1": SourcePatchSpec(
                    audio_channel_map=((2, 0), (2, 1)))})
        timeline = SimpleNamespace(fps=24.0, timeline_id="t1", revision=1)
        from src.features.edit.core.audio_edit import make_step, new_audio_edit
        # 降噪量取可忽略值（本测试验证 patch 选道，非 DSP）。
        edit = new_audio_edit([make_step(
            "spectral_denoise", 0, 24000, (0, 1),
            noise_start=0, noise_end=12600, reduction_db=0.25)])
        clip = SimpleNamespace(
            clip_id="c1", asset_id="asset-1", source_start=0,
            source_duration=12, timeline_duration=12, timeline_start=0,
            source_sample_offset=0, is_reverse_playback=lambda: False,
            effective_speed=1.0, audio_edit=edit)

        plan = prc.build_pre_render_plan(project, timeline, clip, asset)
        assert plan is not None
        spec = plan.spec()
        assert spec.source_channel_map == ((2, 0), (2, 1))
        assert spec.channel_layout == "5.1"
        assert spec.domain_before == "source:6ch:5.1"
        assert spec.domain_after == "process:2ch"
        # patch 参与 key。
        assert plan.spec().key != prc.PreRenderBuildPlan(
            source_path=plan.source_path, start_sample=plan.start_sample,
            end_sample=plan.end_sample, sample_rate=plan.sample_rate,
            source_fingerprint=plan.source_fingerprint,
            steps=plan.steps).spec().key
        # 基底内容 = C 声道广播（无 patch 时是全 5.1 downmix）。
        base = plan.builder()()
        assert base.shape[0] == 2
        scale = _band_energy(_sine(440.0, 0.5, 0.5), 440.0)
        for ch in (0, 1):
            assert _band_energy(base[ch], 440.0) / scale == pytest.approx(
                1.0, abs=0.15)
            assert _band_energy(base[ch], 220.0) / scale < 0.05


class TestSpecDomainFields:
    def test_layout_and_map_change_key(self) -> None:
        base = prc.PreRenderSpec(
            source_fingerprint="fp", sample_rate=48000, channel_count=2,
            total_samples=100, steps_signature=(("denoise", 6.0),))
        with_layout = prc.PreRenderSpec(
            source_fingerprint="fp", sample_rate=48000, channel_count=2,
            total_samples=100, steps_signature=(("denoise", 6.0),),
            channel_layout="5.1")
        with_map = prc.PreRenderSpec(
            source_fingerprint="fp", sample_rate=48000, channel_count=2,
            total_samples=100, steps_signature=(("denoise", 6.0),),
            source_channel_map=((2, 0), (2, 1)))
        assert len({base.key, with_layout.key, with_map.key}) == 3
        assert base.domain_before == "" or base.domain_after == ""
