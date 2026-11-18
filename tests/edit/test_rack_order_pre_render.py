"""P0 · 按用户机架顺序执行并为交付保真（2026-11-19 计划第一阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- 拿到最后一个需要烘焙的步，把它及其前面的所有已启用步骤作为缓存
  前缀，按顺序处理；剩余后缀才走实时块；
- Gain→Denoise 与 Denoise→Gain 的非通勤 fixture 必须有可区分的结果；
- 版本（缓存键）包含全部前缀，变更任何有效字段生成新 key；
- pending/ready/failed/cancelled 状态机用于预检、导出 worker 和 UI；
- 导出读不到可信 PCM 时中止并报告片段、步骤和原因——不得以成功
  结果静默丢效果。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core import pcm_render_cache as prc
from src.features.edit.core.audio_edit import (
    append_step,
    apply_clip_audio_edit_block,
    make_step,
    new_audio_edit,
    render_prefix_steps,
    render_pre_render_steps,
    steps_signature,
    suffix_audio_edit,
)

RATE = 48000


def _noisy_tone(samples: int = RATE) -> np.ndarray:
    """1 s：440 Hz（0.25）+ 独立噪声（0.02）——gain 与 denoise 非通勤。"""
    rng = np.random.default_rng(20261119)
    t = np.arange(samples) / RATE
    tone = 0.25 * np.sin(2 * np.pi * 440.0 * t)
    noise = 0.02 * rng.standard_normal(samples)
    sig = (tone + noise).astype(np.float32)
    return np.stack([sig, sig])


def _gain_step(total: int, gain_db: float = -18.0) -> dict:
    return make_step("range_gain", 0, total, (0, 1), gain_db=gain_db)


def _denoise_step(total: int) -> dict:
    # 噪声窗 ≥0.25 s（denoise_spectral_subtraction 的下限）。
    noise_end = min(total, max(total // 4, 12600))
    return make_step(
        "spectral_denoise", 0, total, (0, 1),
        noise_start=0, noise_end=noise_end, reduction_db=18.0)


def _gain_then_denoise(total: int = RATE) -> dict:
    return new_audio_edit([_gain_step(total), _denoise_step(total)])


def _denoise_then_gain(total: int = RATE) -> dict:
    return new_audio_edit([_denoise_step(total), _gain_step(total)])


# ----------------------------------------------------------------------
# 机架顺序：前缀混排 inline 步骤按序烘焙
# ----------------------------------------------------------------------
class TestRackOrder:
    def test_gain_denoise_order_is_non_commuting(self) -> None:
        pcm = _noisy_tone()
        a = render_pre_render_steps(pcm, _gain_then_denoise(), sample_rate=RATE)
        b = render_pre_render_steps(pcm, _denoise_then_gain(), sample_rate=RATE)
        # 两种机架顺序结果必须不同（噪声底不同——gain 先压低信号，
        # 谱减按低电平谱估计；denoise 先做则谱估计按原始电平）。
        assert not np.allclose(a, b, atol=1e-5)

    def test_gain_then_denoise_matches_manual_reference(self) -> None:
        """前缀按机架序 == 手工按序组合（gain 先、denoise 后）。

        选区边缘有 3 ms 干湿过渡——gain 段参照用同一块应用器生成。"""
        from src.features.edit.core.audio_denoise import (
            denoise_spectral_subtraction,
        )
        from src.features.edit.core.audio_edit import apply_clip_audio_edit

        pcm = _noisy_tone()
        edit = _gain_then_denoise()
        rendered = render_pre_render_steps(pcm, edit, sample_rate=RATE)

        total = pcm.shape[1]
        dn_params = edit["steps"][1]["params"]
        gain_stage = apply_clip_audio_edit(
            pcm, 0,
            {"version": 1, "processing_rate": RATE,
             "steps": [edit["steps"][0]]},
            sample_rate=RATE)
        manual = denoise_spectral_subtraction(
            gain_stage, RATE,
            (int(dn_params["noise_start"]), int(dn_params["noise_end"])),
            (0, total), float(dn_params["reduction_db"]))
        np.testing.assert_allclose(rendered, manual, atol=1e-6)

    def test_prefix_includes_inline_before_last_pre_render(self) -> None:
        prefix = render_prefix_steps(_gain_then_denoise())
        assert [s["processor_id"] for s in prefix] == [
            "range_gain", "spectral_denoise"]
        # 后缀为空（最后一个预渲染步骤之后无步骤）。
        suffix = suffix_audio_edit(_gain_then_denoise())
        assert suffix["steps"] == []

    def test_suffix_keeps_steps_after_last_pre_render(self) -> None:
        edit = append_step(
            _denoise_then_gain(),
            make_step("mute", 0, 100, (0,)))
        # 最后预渲染步骤是 denoise（下标 0）→ 后缀 = [range_gain, mute]。
        suffix = suffix_audio_edit(edit)
        assert [s["processor_id"] for s in suffix["steps"]] == [
            "range_gain", "mute"]

    def test_bypassed_pre_render_not_in_prefix(self) -> None:
        edit = _gain_then_denoise()
        edit["steps"][1]["enabled"] = False  # 旁路 denoise
        assert render_prefix_steps(edit) == []
        from src.features.edit.core.audio_edit import requires_pre_render
        assert requires_pre_render(edit) is False

    def test_block_path_with_base_applies_suffix_only(self) -> None:
        """基底在场：块路径只应用后缀——前缀 inline 不重放（不叠加）。"""
        from src.features.edit.core.audio_edit import apply_clip_audio_edit

        pcm = _noisy_tone()
        edit = _denoise_then_gain()   # 前缀=denoise；后缀=range_gain(-18dB)
        base = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        # 实时块（第二半段）：内容取基底，后缀 gain 应用一次（含边缘 ramp）。
        block = apply_clip_audio_edit_block(
            pcm[:, RATE // 2:].copy(), RATE // 2, edit,
            sample_rate=RATE, pre_render_base=base)
        suffix_edit = suffix_audio_edit(edit)
        expected = apply_clip_audio_edit(
            base[:, RATE // 2:], RATE // 2, suffix_edit, sample_rate=RATE)
        np.testing.assert_allclose(block, expected, atol=1e-6)
        # 后缀确实生效（非恒等）。
        assert not np.allclose(block, base[:, RATE // 2:], atol=1e-6)

    def test_prefix_inline_domain_inversion_before_stretch(self) -> None:
        """[mute(最终域), time_stretch]：mute 区间折回拉伸前域再按序处理。

        WSOLA 相似度搜索会在静音边界产生有限渗漏——深处应干净。"""
        pcm = _noisy_tone()
        total = RATE
        edit = new_audio_edit([
            make_step("mute", 48000, 72000, (0, 1)),   # 最终域 [1s, 1.5s)
            make_step("time_stretch", 0, total, (0, 1),
                      ratio_pct=50.0, output_length=2 * total),
        ])
        out = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        assert out.shape[1] == 2 * total
        # 静音区深处（远离 WSOLA 搜索窗 ±delta+frame 的边界渗漏）干净。
        assert np.max(np.abs(out[:, 54000:66000])) < 1e-3
        # 两侧保持能量（若未做域折算，静音会落到 [24000,36000)）。
        assert np.max(np.abs(out[:, 1000:47000])) > 0.05
        assert np.max(np.abs(out[:, 72600:])) > 0.05

    def test_signature_changes_on_reorder_and_params(self) -> None:
        a = steps_signature({"steps": _gain_then_denoise()["steps"]})
        b = steps_signature({"steps": _denoise_then_gain()["steps"]})
        assert a != b
        louder = _gain_then_denoise()
        louder["steps"][0]["params"]["gain_db"] = -6.0
        assert steps_signature(louder) != a


# ----------------------------------------------------------------------
# 状态机 + 导出保真
# ----------------------------------------------------------------------
def _plan_fixture(tmp_path, audio_edit=None):
    """带真实 WAV 的 plan fixture（复用 test_pcm_render_cache 的形态）。"""
    import av

    pcm = _noisy_tone(RATE // 2)
    wav = tmp_path / "src.wav"
    container = av.open(str(wav), mode="w", format="wav")
    stream = container.add_stream("pcm_s16le", rate=RATE)
    stream.layout = "stereo"
    frame = av.AudioFrame.from_ndarray(
        np.clip(pcm, -1.0, 1.0).astype(np.float32),
        format="fltp", layout="stereo")
    frame.sample_rate = RATE
    for pkt in stream.encode(frame):
        container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()

    asset = SimpleNamespace(
        asset_id="asset-1", file_path=str(wav), path=str(wav),
        media_type="audio", category="Media",
        fps=24.0, audio_channels=2, audio_channel_layout="stereo",
        duration_seconds=0.5)
    project = SimpleNamespace(assets={"asset-1": asset})
    timeline = SimpleNamespace(fps=24.0, timeline_id="t1", revision=3)
    clip = SimpleNamespace(
        clip_id="c1", asset_id="asset-1", source_start=0, source_duration=12,
        timeline_duration=12, timeline_start=0, source_sample_offset=0,
        is_reverse_playback=lambda: False, effective_speed=1.0,
        audio_edit=audio_edit)
    return project, timeline, clip, asset


class TestPreRenderStateMachine:
    def test_ready_resolution_returns_base_and_step_ids(self, tmp_path):
        edit = _gain_then_denoise(RATE // 2)
        project, timeline, clip, asset = _plan_fixture(tmp_path, edit)
        resolution = prc.resolve_export_pre_render_base(
            project, timeline, clip, asset)
        assert resolution.status is prc.PreRenderState.READY
        assert resolution.base is not None
        assert resolution.base.shape[1] == RATE // 2
        # 归因包含前缀的全部步骤（含 inline）。
        ids = set(resolution.step_ids)
        assert len(ids) == 2

    def test_failed_backend_raises_with_clip_step_reason(self, tmp_path,
                                                         monkeypatch):
        edit = _gain_then_denoise(RATE // 2)
        project, timeline, clip, asset = _plan_fixture(tmp_path, edit)
        monkeypatch.setattr(
            prc, "get_pcm_render_cache", lambda: _FailingCache())
        resolution = prc.resolve_export_pre_render_base(
            project, timeline, clip, asset)
        assert resolution.status is prc.PreRenderState.FAILED
        assert "boom" in resolution.reason
        with pytest.raises(prc.PreRenderUntrustedError) as excinfo:
            resolution.ensure_exportable()
        assert excinfo.value.clip_id == "c1"
        assert excinfo.value.reason

    def test_blocked_plan_maps_to_failed(self, tmp_path):
        edit = new_audio_edit([_denoise_step(RATE // 2)])
        project, timeline, clip, asset = _plan_fixture(tmp_path, edit)
        # 倒放片段被计划阻断 → FAILED（携带步骤与原因）。
        clip.is_reverse_playback = lambda: True
        resolution = prc.resolve_export_pre_render_base(
            project, timeline, clip, asset)
        assert resolution.status is prc.PreRenderState.FAILED
        assert resolution.step_ids  # 归因包含前缀步骤
        assert "reverse" in resolution.reason

    def test_repeated_delivery_same_interval_hits_cache(self, tmp_path):
        edit = _denoise_then_gain(RATE // 2)
        project, timeline, clip, asset = _plan_fixture(tmp_path, edit)
        first = prc.resolve_export_pre_render_base(
            project, timeline, clip, asset)
        second = prc.resolve_export_pre_render_base(
            project, timeline, clip, asset)
        np.testing.assert_array_equal(first.base, second.base)


class _FailingCache:
    """get_or_build 总是失败（模拟缓存损坏/后端缺失）。"""

    def protect(self, spec):
        from contextlib import contextmanager

        @contextmanager
        def _p():
            yield spec
        return _p()

    def get_or_build(self, spec, builder):
        raise RuntimeError("boom: cache backend unavailable")


# ----------------------------------------------------------------------
# 成片/Stem 导出中止（不得静默丢效果）
# ----------------------------------------------------------------------
class TestExportFidelity:
    def _exporter(self, tmp_path, audio_edit):
        from src.features.edit.core.project import EditProject, MediaAsset
        from src.features.edit.core.timeline_export import (
            TimelineExporter,
            TimelineExportConfig,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        from src.shared.media.metadata import Rational

        pcm = _noisy_tone(RATE // 2)
        import av

        wav = tmp_path / "src.wav"
        container = av.open(str(wav), mode="w", format="wav")
        stream = container.add_stream("pcm_s16le", rate=RATE)
        stream.layout = "stereo"
        frame = av.AudioFrame.from_ndarray(
            np.clip(pcm, -1.0, 1.0).astype(np.float32),
            format="fltp", layout="stereo")
        frame.sample_rate = RATE
        for pkt in stream.encode(frame):
            container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)
        container.close()
        asset = MediaAsset("asset-1", str(wav), "audio", "Media", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        clip = ClipInstance("c1", "asset-1", "a0", 0, 12, 0, 12,
                            audio_edit=audio_edit)
        track = EditTrack("a0", "A1", "audio", 0, clips=(clip,), volume=1.0)
        seq = TimelineSequence(
            timeline_id="seq-a", name="A", width=64, height=48,
            fps=Rational(24, 1), audio_tracks=(track,))
        exporter = TimelineExporter(project, seq, Mock())
        return exporter, TimelineExportConfig(
            output_path=str(tmp_path / "out.mov"))

    def test_master_aborts_when_pre_render_unusable(self, tmp_path,
                                                    monkeypatch):
        edit = new_audio_edit([_denoise_step(RATE // 2)])
        exporter, config = self._exporter(tmp_path, edit)
        monkeypatch.setattr(
            prc, "get_pcm_render_cache", lambda: _FailingCache())
        with pytest.raises(prc.PreRenderUntrustedError):
            exporter._render_audio_master(0, 12, config, encoder=Mock())

    def test_stem_base_fn_aborts_when_pre_render_unusable(self, tmp_path,
                                                          monkeypatch):
        from src.features.edit.core.stem_export import create_pre_render_base_fn

        edit = new_audio_edit([_denoise_step(RATE // 2)])
        project, timeline, clip, asset = _plan_fixture(tmp_path, edit)
        base_fn = create_pre_render_base_fn(project, timeline)
        monkeypatch.setattr(
            prc, "get_pcm_render_cache", lambda: _FailingCache())
        with pytest.raises(prc.PreRenderUntrustedError):
            base_fn(clip)
