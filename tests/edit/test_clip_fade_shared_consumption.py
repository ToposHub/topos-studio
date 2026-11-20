"""P0-3 · 片段 gain/fade 的共享计算与三端对拍（2026-11-21 计划）。

验收来自 docs/plans/2026-11-21-mixer-clip-editor-backend-handoff.md：

- 跨率换算：采样域淡化按 48 kHz 参考域存储，``apply_clip_fade`` 内部
  有理换算到消费率——20 ms（960 参考样本）在 44.1/48/96 kHz 分别落
  882/960/1920 个样本；显式 0 在各消费率保持关闭；帧域参数不受换算
  影响；
- 统一信号链：片段编辑器预览/试听消费片段级 gain/fade（同一
  ``apply_clip_fade`` 路径）——恒定 0.25 素材 −6 dB + 0.25 s 线性淡入
  时，起点为 0、淡入中点约 0.06265；
- 画布统一：有效淡化长度（采样域优先）在 24/23.976/29.97 fps 下不以
  整数帧取整消失；帧域写回清同端采样覆盖（两类字段不串改），拖拽在
  采样域片段上写回采样。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import apply_clip_fade
from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE
from src.features.edit.core.timeline_v2 import ClipInstance

pytestmark = pytest.mark.edit_core

RATE = AUDIO_REFERENCE_SAMPLE_RATE


def _constant(amp: float, seconds: float = 1.0) -> np.ndarray:
    return np.stack([np.full(int(seconds * RATE), amp, dtype=np.float32)] * 2)


def _fade_span(samples: np.ndarray) -> int:
    """淡入区宽度：第一个到达平台 99.9% 的样本序号 +1。"""
    plateau = float(np.abs(samples[:, -1]).max())
    if plateau <= 0.0:
        return 0
    env = np.abs(samples[0]) / plateau
    reached = np.nonzero(env >= 0.999)[0]
    return int(reached[0]) + 1 if reached.size else 0


class TestReferenceDomainRateConversion:
    @pytest.mark.parametrize("rate,expected", [(44100, 882), (48000, 960),
                                               (96000, 1920)])
    def test_twenty_ms_lands_exact_samples(self, rate, expected) -> None:
        """20 ms（960 参考样本）→ 44.1/48/96 kHz 的消费域样本数。"""
        pcm = _constant(0.5)
        n = pcm.shape[1]
        out = apply_clip_fade(
            pcm, 0, n, 0, 0, 0, 24.0, float(rate),
            fade_in_samples_override=960, fade_curve_in="linear")
        assert _fade_span(out) == pytest.approx(expected, abs=2)

    def test_zero_override_stays_off_at_all_rates(self) -> None:
        """显式 0 只关闭该端：各消费率下淡入不存在（起点即平台）。"""
        for rate in (44100, 48000, 96000):
            pcm = _constant(0.5)
            out = apply_clip_fade(
                pcm, 0, pcm.shape[1], 0, 3, 0, 24.0, float(rate),
                fade_in_samples_override=0, fade_curve_in="linear")
            # 帧域 3 帧的旧淡入被显式 0 关闭：起点即全电平。
            assert abs(float(out[0, 0])) == pytest.approx(0.5, abs=1e-6)

    def test_frame_domain_not_converted(self) -> None:
        """帧域参数按消费率换算（帧 × rate/fps），不经参考域折算。"""
        pcm = _constant(0.5)
        # 3 帧 @23.976 fps、44.1 kHz 的非整比组合：淡入半程约
        # (3 × 44100/23.976)/2 ≈ 2759 样本（帧域直接乘消费率样本/帧）。
        out = apply_clip_fade(
            pcm, 0, pcm.shape[1], 0, 3, 0, 23.976, 44100.0,
            fade_curve_in="linear")
        env = out[0] / 0.5
        half = int(np.nonzero(env >= 0.5)[0][0])
        assert half == pytest.approx(int(3 * 44100 / 23.976) // 2, abs=3)

    def test_offset_converts_to_consumption_rate(self) -> None:
        """子帧偏移（参考域）换算到消费域：淡化随内容整体右移。"""
        pcm = _constant(0.5)
        rate = 96000
        offset_ref = 480  # 10 ms @48k 参考 → 960 消费样本
        out = apply_clip_fade(
            pcm, 0, pcm.shape[1], 0, 0, 2, 24.0, float(rate),
            fade_in_samples_override=9600, audio_sample_offset=offset_ref,
            fade_curve_in="linear")
        # 偏移区（前 960 消费样本）静音；淡入（9600 参考 → 19200 消费）
        # 中点随内容右移。
        assert float(np.abs(out[:, :960]).max()) == pytest.approx(0.0, abs=1e-6)
        mid = 960 + 19200 // 2
        assert float(out[0, mid]) == pytest.approx(0.25, abs=1e-3)


class TestModelEffectiveFade:
    def _clip(self, **overrides) -> ClipInstance:
        base = dict(clip_id="c", asset_id="a", track_id="t",
                    timeline_start=0, timeline_duration=48,
                    source_start=0, source_duration=48)
        base.update(overrides)
        return ClipInstance(**base)

    def test_samples_domain_converts_per_fps(self) -> None:
        clip = self._clip(fade_in_samples=960)
        assert clip.effective_fade_in_frames(24.0) == pytest.approx(0.48)
        assert clip.effective_fade_in_frames(23.976) == pytest.approx(
            960 / 48000 * 23.976, rel=1e-6)
        assert clip.effective_fade_in_frames(29.97) == pytest.approx(
            960 / 48000 * 29.97, rel=1e-6)

    def test_frame_domain_passthrough(self) -> None:
        clip = self._clip(fade_in_duration=12)
        assert clip.effective_fade_in_frames(24.0) == 12.0

    def test_frame_write_clears_same_end_only(self) -> None:
        """帧域写回清同端采样覆盖；另一端字段类型不动（不串改）。"""
        clip = self._clip(fade_in_samples=960, fade_out_samples=4800,
                          fade_out_duration=5)
        updated = clip.with_fade_in(6)
        assert updated.fade_in_duration == 6
        assert updated.fade_in_samples is None
        assert updated.fade_out_samples == 4800  # 另一端原样保留
        updated2 = updated.with_fade_out(7)
        assert updated2.fade_out_duration == 7
        assert updated2.fade_out_samples is None
        assert updated2.fade_in_samples is None  # 已在上一写清空


class TestEditorPreviewConsumesGainFade:
    def test_constant_minus6db_quarter_second_linear_fade(self, qapp) -> None:
        """验收：恒定 0.25、−6 dB、0.25 s 线性淡入 → 起点 0、中点 0.06265。"""
        from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

        pcm = _constant(0.25, 2.0)
        editor = ProjectClipEditor()
        panel = editor._ensure_panel()
        clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48,
                            fade_in_samples=12000,
                            fade_curve_in="linear",
                            audio_gain_db=-6.0)
        editor._clip = clip
        editor._fps = 24.0
        editor._locked = False
        panel._duration = pcm.shape[1] / RATE
        panel.attach_pcm(pcm, RATE)
        panel._preview_offline = True
        panel._model_audio_edit = None
        editor._sync_gain_fade()
        # 预览（试听数据源）消费片段级 gain/fade。
        assert panel._pcm is not None
        assert float(panel._pcm[0, 0]) == pytest.approx(0.0, abs=1e-6)
        mid = 6000  # 淡入中点（12000 样本线性）
        assert float(panel._pcm[0, mid]) == pytest.approx(0.06265, abs=2e-3)
        # 淡入结束后为 −6 dB 平台。
        plateau = float(panel._pcm[0, 20000])
        assert plateau == pytest.approx(0.25 * 10 ** (-6 / 20), abs=1e-3)

    def test_preview_key_tracks_gain_fade(self, qapp) -> None:
        from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

        pcm = _constant(0.25, 1.0)
        editor = ProjectClipEditor()
        panel = editor._ensure_panel()
        clip = ClipInstance("c1", "a1", "a1", 0, 24, 0, 24,
                            audio_gain_db=0.0)
        editor._clip = clip
        editor._fps = 24.0
        editor._locked = False
        panel._duration = pcm.shape[1] / RATE
        panel.attach_pcm(pcm, RATE)
        panel._preview_offline = True
        panel._model_audio_edit = None
        editor._sync_gain_fade()
        before = panel._pcm[0, 100]
        object.__setattr__(
            clip, "audio_gain_db", -12.0) if False else None
        clip2 = clip.with_gain_db(-12.0) if hasattr(clip, "with_gain_db") \
            else clip
        # 直接改快照路径：sync 用新 gain 重建。
        editor._clip = ClipInstance(
            "c1", "a1", "a1", 0, 24, 0, 24, audio_gain_db=-12.0)
        editor._sync_gain_fade()
        after = panel._pcm[0, 100]
        assert before == pytest.approx(0.25, abs=1e-6)
        assert after == pytest.approx(0.25 * 10 ** (-12 / 20), abs=1e-3)


class TestCanvasEffectiveFade:
    def test_display_frames_min_one(self) -> None:
        """极短淡化（<1 帧）显示至少 1 帧，0 保持 0。"""
        from src.features.edit.ui.timeline_items import ClipItem

        assert ClipItem._effective_fade_display_frames(0.0) == 0
        assert ClipItem._effective_fade_display_frames(-1.0) == 0
        assert ClipItem._effective_fade_display_frames(0.48) == 1
        assert ClipItem._effective_fade_display_frames(12.0) == 12

    def test_fade_drag_on_samples_domain_clip_commits_samples(self) -> None:
        """采样域片段上的时间线拖拽经帧→样本换算提交（undo 一次恢复）。"""
        from src.features.edit.core.commands import (
            UpdateClipSampleFieldsCommand,
        )
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        from src.features.edit.ui.timeline_view import TimelineView
        from src.shared.media.metadata import Rational

        clip = ClipInstance(
            "c1", "a1", "a1", 0, 48, 0, 48,
            fade_in_samples=960, fade_out_samples=4800)
        track = EditTrack(track_id="a1", name="A1", track_type="audio",
                          index=0, clips=(clip,))
        timeline = TimelineSequence(
            timeline_id="tl", name="T", fps=Rational(24, 1),
            audio_tracks=(track,))
        executed: list = []
        view = SimpleNamespace(
            _timeline=timeline,
            _items_map={},
            _execute_command=executed.append,
            status_message_requested=SimpleNamespace(emit=lambda *_a: None),
        )
        TimelineView._on_fade_finished(
            view, "c1", "a1", 0, 96, 6, 96)
        assert len(executed) == 1
        cmd = executed[0]
        assert isinstance(cmd, UpdateClipSampleFieldsCommand)
        applied, _updated = cmd.apply(timeline)
        new_clip = applied.get_clip_by_id("c1")
        # 6 帧 @24fps → 12000 参考样本；淡出端未动（另一端字段不串改）。
        assert new_clip.fade_in_samples == 12000
        assert new_clip.fade_out_samples == 4800
        undone = _updated.undo(applied)
        assert undone.get_clip_by_id("c1").fade_in_samples == 960

    def test_fade_drag_on_frame_domain_clip_commits_frames(self) -> None:
        from src.features.edit.core.commands import ChangeClipFadeCommand
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        from src.features.edit.ui.timeline_view import TimelineView
        from src.shared.media.metadata import Rational

        clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48)
        track = EditTrack(track_id="a1", name="A1", track_type="audio",
                          index=0, clips=(clip,))
        timeline = TimelineSequence(
            timeline_id="tl", name="T", fps=Rational(24, 1),
            audio_tracks=(track,))
        executed: list = []
        view = SimpleNamespace(
            _timeline=timeline,
            _items_map={},
            _execute_command=executed.append,
            status_message_requested=SimpleNamespace(emit=lambda *_a: None),
        )
        TimelineView._on_fade_finished(view, "c1", "a1", 0, 0, 5, 0)
        assert isinstance(executed[0], ChangeClipFadeCommand)
