"""wd12-g54 批 D 修复验证测试（16 条 Major：音频播放尾部 + 自动化 + 字幕域）。

逐条对应 REMEDIATION_PLAN.md 条目：
- AUD-15513: scrub 预览 worker 与播放启动竞态（cancel_pending + mix 前时钟态检查）
- AUD-15515: resume() 状态守卫（停止态不误发 state_changed("playing")）
- AUD-15517: get_progress() duration==1 除零
- AUD-15608: simplify_samples 迭代 DP（噪声之字形不触发 RecursionError）
- AUD-15609: Write 模式覆盖整段已录曲线（OverwriteAutomationCommand）
- AUD-15700: probe_errors 探测异常通道写入
- AUD-15701: drift 检测分档语义（低估方向可报告）
- AUD-15800: Demote 删除空图形轨后 undo 重建轨道
- AUD-15807: compute_trim_range 左缘裁剪上限不再混入整体平移约束
- AUD-15808: SCC CEA-608 pop-on 时序（9420+文本+942f / 942c）
- AUD-15809: SCC 29.97 drop-frame 换算（SMPTE ST 12-1）
- AUD-15811: 无 Qt 字体扫描不硬编码族名 + 匹配 .otf
- AUD-15900: cue_animation_envelope 四元组（X/Y 轴独立）
- AUD-15908: mkv → matroska muxer
- AUD-15909: mxf 显式拒绝
- AUD-16001: StylePresetRegistry category 往返
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.core import capability_registry as cr
from src.features.edit.core.audio_playback import (
    AudioClock,
    AudioMixer,
    AudioPlaybackManager,
    AudioPlaybackState,
    AudioScrubPreviewWorker,
)
from src.features.edit.core.automation_recorder import (
    AutomationRecorder,
    OverwriteAutomationCommand,
    _FaderSample,
    simplify_samples,
)
from src.features.edit.core.captions.commands import (
    DemoteGraphicToSubtitleCommand,
    PromoteSubtitleToGraphicCommand,
)
from src.features.edit.core.captions.edit_policy import SubtitleEditPolicy
from src.features.edit.core.captions.export import _format_scc_time, _render_scc
from src.features.edit.core.captions.font_resolver import FontResolver
from src.features.edit.core.captions.graphics import cue_animation_envelope
from src.features.edit.core.captions.model import CaptionCueMetadata
from src.features.edit.core.captions.mux import build_caption_mux_command
from src.features.edit.core.captions.style_preset import (
    BUILTIN_PRESETS,
    CaptionStyle,
    CaptionStylePreset,
    StylePresetRegistry,
)
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence


# ================================================================
# AUD-15513: scrub 预览与播放启动竞态
# ================================================================

class TestAud15513ScrubPreviewRace:
    def test_cancel_pending_invalidates_queued_tasks(self) -> None:
        """cancel_pending 递增 generation 并清空队列，排队旧任务全部过期。"""
        mixer = Mock(spec=AudioMixer)
        worker = AudioScrubPreviewWorker(mixer, is_playback_active=lambda: False)
        worker.request_preview(100, 24.0, 512, 2, generation=5)

        worker.cancel_pending()

        assert worker._latest_generation == 6  # 旧任务 generation=5 已过期
        assert worker._queue.empty()

    def test_worker_drops_task_before_mix_when_playback_active(self) -> None:
        """播放已启动时，已出队的任务在 reset_dsp_states/mix 前被丢弃。"""
        mixer = Mock(spec=AudioMixer)
        mixer.mix_samples.return_value = (None, {})
        emitted: list[tuple[int, int]] = []

        class _Recorder(QObject):
            samples_ready = pyqtSignal(int, int, object)

        worker = AudioScrubPreviewWorker(mixer, is_playback_active=lambda: True)
        worker.samples_ready.connect(
            lambda frame, generation, samples: emitted.append((frame, generation))
        )
        worker.request_preview(50, 24.0, 256, 2, generation=1)

        thread = threading.Thread(target=worker.run)
        thread.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not emitted:
                time.sleep(0.02)
        finally:
            worker.stop()
            thread.join(timeout=2.0)

        mixer.reset_dsp_states.assert_not_called()
        mixer.mix_samples.assert_not_called()
        assert emitted == []

    def test_manager_invalidate_bumps_generation_and_cancels_worker(self) -> None:
        manager = AudioPlaybackManager()
        worker = Mock(spec=AudioScrubPreviewWorker)
        manager._scrub_preview_worker = worker
        before = manager._scrub_preview_generation

        manager._invalidate_scrub_previews()

        assert manager._scrub_preview_generation == before + 1
        worker.cancel_pending.assert_called_once()


# ================================================================
# AUD-15515: resume() 状态守卫
# ================================================================

class TestAud15515ResumeGuard:
    def test_clock_resume_returns_bool(self) -> None:
        clock = AudioClock()
        assert clock.resume() is False  # STOPPED → no-op
        clock.start(0, 24.0)
        assert clock.resume() is False  # PLAYING → no-op
        clock.pause()
        assert clock.resume() is True  # PAUSED → 实际恢复

    def test_manager_resume_in_stopped_state_does_not_emit_playing(self) -> None:
        manager = AudioPlaybackManager()
        states: list[str] = []
        manager.state_changed.connect(states.append)

        manager.resume()

        assert states == []
        assert manager.get_state() is not AudioPlaybackState.PLAYING

    def test_manager_resume_from_paused_emits_playing(self) -> None:
        manager = AudioPlaybackManager()
        states: list[str] = []
        manager.state_changed.connect(states.append)
        manager._clock.start(10, 24.0)
        manager._clock.pause()

        manager.resume()

        assert states == [AudioPlaybackState.PLAYING.value]


# ================================================================
# AUD-15517: get_progress() 除零
# ================================================================

class TestAud15517ProgressDivision:
    def test_single_frame_timeline_does_not_raise(self) -> None:
        manager = AudioPlaybackManager()
        manager._timeline_duration = 1
        progress = manager.get_progress()
        assert isinstance(progress, float)

    def test_normal_timeline_progress_unchanged(self) -> None:
        manager = AudioPlaybackManager()
        manager._timeline_duration = 101
        manager._clock.seek(50, 24.0)
        assert manager.get_progress() == pytest.approx(0.5)


# ================================================================
# AUD-15608: simplify_samples 迭代 Douglas-Peucker
# ================================================================

class TestAud15608IterativeSimplify:
    def test_noisy_zigzag_samples_do_not_recursion_error(self) -> None:
        """两值交替（每层只剥一个点，旧递归深度 O(n)）3000 样本不再爆栈。"""
        samples = [
            _FaderSample(frame=i, value=0.0 if i % 2 == 0 else 1.0)
            for i in range(3000)
        ]
        started = time.monotonic()
        result = simplify_samples(samples, threshold=0.005)
        elapsed = time.monotonic() - started

        assert result[0].frame == 0 and result[-1].frame == 2999  # 端点必留
        assert elapsed < 30.0  # 迭代版可在合理时间内完成

    def test_result_equivalence_with_documented_rules(self) -> None:
        samples = [
            _FaderSample(0, 1.0),
            _FaderSample(5, 0.2),
            _FaderSample(10, 1.0),
        ]
        result = simplify_samples(samples, threshold=0.005)
        assert [(p.frame, p.value) for p in result] == [(0, 1.0), (5, 0.2), (10, 1.0)]


# ================================================================
# AUD-15609: Write 模式覆盖已录曲线
# ================================================================

def _automation_timeline(old_keyframes: dict[int, float]) -> TimelineSequence:
    track = EditTrack(track_id="t1", track_type="audio", index=0)
    if old_keyframes:
        from src.shared.keyframe import Keyframe, KeyframeTrack

        kf_track = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        for frame in sorted(old_keyframes):
            kf_track.add_keyframe(
                Keyframe(frame=frame, value=old_keyframes[frame])
            )
        track = track._rebuild(automation={"volume": kf_track})
    return TimelineSequence(audio_tracks=(track,))


class TestAud15609WriteOverwrite:
    def test_write_clears_old_keyframes_inside_recorded_range(self) -> None:
        seq = _automation_timeline({5: 0.1, 12: 0.9})
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        for f in range(0, 11):
            rec.record(f, 0.5 + f * 0.02)
        cmd = rec.stop()

        assert isinstance(cmd, OverwriteAutomationCommand)
        new_seq, updated = cmd.apply(seq)
        kf_track = new_seq.get_track_by_id("t1").automation["volume"]
        frames = [kf.frame for kf in kf_track.keyframes]
        assert 5 not in frames  # 录制区间 [0,10] 内旧关键帧被覆盖
        assert 12 in frames  # 区间外保留
        assert 0 in frames and 10 in frames  # 新关键帧已写入

        # undo 完整恢复（含区间外关键帧与旧值）
        restored = updated.undo(new_seq)
        restored_frames = {
            kf.frame: kf.value
            for kf in restored.get_track_by_id("t1").automation["volume"].keyframes
        }
        assert restored_frames == {5: 0.1, 12: 0.9}

    def test_touch_keeps_old_curve_between_segments(self) -> None:
        seq = _automation_timeline({5: 0.1})
        rec = AutomationRecorder()
        rec.start("volume", "touch", "t1")
        rec.record(0, 0.4, fader_down=True)
        rec.record(1, 0.5, fader_down=True)
        rec.record(2, 0.6, fader_down=False)  # 松手：闭合段
        rec.record(3, 0.7, fader_down=False)
        cmd = rec.stop()

        assert cmd.overwrite_ranges == ((0, 1),)  # 只覆盖按住区间
        new_seq, _ = cmd.apply(seq)
        frames = [kf.frame for kf in new_seq.get_track_by_id("t1").automation["volume"].keyframes]
        assert 5 in frames  # 段外旧曲线保留

    def test_command_serialization_roundtrip(self) -> None:
        cmd = OverwriteAutomationCommand.create(
            track_id="t1", param="volume",
            keyframes=((0, 0.5), (10, 1.0)),
            overwrite_ranges=((0, 10),),
            min_val=0.0, max_val=2.0,
        )
        restored = OverwriteAutomationCommand.from_dict(cmd.to_dict())
        assert restored.keyframes == ((0, 0.5), (10, 1.0))
        assert restored.overwrite_ranges == ((0, 10),)
        assert restored.param == "volume"


# ================================================================
# AUD-15700: probe_errors 写入
# ================================================================

class TestAud15700ProbeErrors:
    def test_bwf_probe_exception_recorded(self, monkeypatch) -> None:
        from src.features.edit.core import bwf

        def _boom() -> bool:
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(bwf, "is_true_bwf_supported", _boom)
        registry = cr.build_capability_registry()
        assert any(err.startswith("bwf:") for err in registry.probe_errors)
        assert registry.is_healthy is False
        assert "bwf" in registry.to_dict()["probe_errors"][0]

    def test_audio_effect_probe_exception_recorded(self, monkeypatch) -> None:
        from src.features.edit.core import effect_registry

        def _boom(*args, **kwargs):
            raise RuntimeError("registry gone")

        monkeypatch.setattr(effect_registry, "get_effect_registry_health", _boom)
        registry = cr.build_capability_registry()
        assert any(
            err.startswith("audio_effects:") and "probe error" in err
            for err in registry.probe_errors
        )

    def test_clean_probes_keep_errors_empty(self) -> None:
        registry = cr.build_capability_registry()
        assert registry.probe_errors == ()
        assert registry.is_healthy is True


# ================================================================
# AUD-15701: drift 低估方向
# ================================================================

class TestAud15701DriftUnderpromise:
    def test_fcpxml_underpromise_drift_reported(self, monkeypatch) -> None:
        """注册表 PARTIAL（已接线）vs 静态表 available=False 必须报告。

        F1.3/R1 后真实静态表 FCPXML=True 与 PARTIAL 一致（零漂移）；本用例
        通过 monkeypatch 静态表保留 AUD-15701 的欠承诺检测契约。"""
        real = cr._interchange_capabilities()
        patched = dict(real)
        patched["final cut pro xml"] = False
        monkeypatch.setattr(cr, "_interchange_capabilities", lambda: patched)
        drifts = cr.find_status_drift()
        by_id = {d.capability_id: d for d in drifts}
        assert "edit.interchange.fcpxml" in by_id
        drift = by_id["edit.interchange.fcpxml"]
        assert drift.interchange_available is False
        assert "低估" in drift.detail

    def test_wired_stems_no_longer_reported_as_overpromise(self) -> None:
        drifts = cr.find_status_drift()
        ids = {d.capability_id for d in drifts}
        assert "edit.interchange.audio_stems" not in ids
        registry = cr.build_capability_registry()
        stems = next(cap for cap in registry.capabilities
                     if cap.capability_id == "edit.interchange.audio_stems")
        assert stems.status == cr.CapabilityStatus.AVAILABLE


# ================================================================
# AUD-15800: Demote undo 重建被删轨道
# ================================================================

def _caption_timeline() -> TimelineSequence:
    subtitle_track = EditTrack(
        track_id="sub-track", track_type="subtitle", name="Captions",
        subtitle_clips=(SubtitleClip(
            clip_id="cue-1", track_id="sub-track", timeline_start=10,
            timeline_duration=20, text="Hello",
            caption_metadata=CaptionCueMetadata(),
        ),),
    )
    return TimelineSequence(
        timeline_id="timeline-1", name="Test",
        video_tracks=(EditTrack(track_id="v1"),),
        subtitle_tracks=(subtitle_track,),
    )


class TestAud15800DemoteUndoRestoresTrack:
    def test_undo_rebuilds_removed_graphics_track(self) -> None:
        timeline = _caption_timeline()
        promoted = PromoteSubtitleToGraphicCommand.create("cue-1").apply(timeline)[0]
        demoted, cmd = DemoteGraphicToSubtitleCommand.create("cue-1").apply(promoted)

        # apply 删除了清空的图形轨
        assert not [
            t for t in demoted.video_tracks if t.track_role == "graphics"
        ]
        assert not demoted.caption_graphic_bindings

        restored = cmd.undo(demoted)
        graphics = [t for t in restored.video_tracks if t.track_role == "graphics"]
        assert len(graphics) == 1  # 轨道按原 track_id 重建
        assert len(graphics[0].clips) == 1  # 图形片段恢复
        assert len(restored.caption_graphic_bindings) == 1  # 绑定恢复
        cue = restored.get_subtitle_clip_by_id("cue-1")
        assert cue is not None and cue.display_mode == "graphic"

    def test_undo_restores_original_graphics_track_metadata(self) -> None:
        """undo 重建的轨道沿用 apply 删除前的原 track_id。"""
        timeline = _caption_timeline()
        promoted = PromoteSubtitleToGraphicCommand.create("cue-1").apply(timeline)[0]
        original_graphics = [
            t for t in promoted.video_tracks if t.track_role == "graphics"
        ][0]
        demoted, cmd = DemoteGraphicToSubtitleCommand.create("cue-1").apply(promoted)
        restored = cmd.undo(demoted)
        graphics = [t for t in restored.video_tracks if t.track_role == "graphics"]
        assert graphics[0].track_id == original_graphics.track_id
        assert len(graphics[0].clips) == 1


# ================================================================
# AUD-15807: 左缘裁剪上限
# ================================================================

class TestAud15807LeftTrimRange:
    def test_left_trim_allowed_when_right_neighbour_adjacent(self) -> None:
        # cue [10,20)，右邻恰好相接于 20（密集字幕轨常态）
        rng = SubtitleEditPolicy.compute_trim_range(
            clip_id="cue", current_start=10, current_duration=10,
            neighbours=(
                {"clip_id": "nb", "timeline_start": 20, "timeline_duration": 10},
            ),
        )
        assert rng.editable
        assert rng.max_start == 19  # end-1，不再被 right_nb-duration 钳回 10
        assert rng.clamp_start(15) == 15

    def test_left_bound_still_respects_left_neighbour(self) -> None:
        rng = SubtitleEditPolicy.compute_trim_range(
            clip_id="cue", current_start=10, current_duration=10,
            neighbours=(
                {"clip_id": "left", "timeline_start": 0, "timeline_duration": 6},
                {"clip_id": "right", "timeline_start": 20, "timeline_duration": 5},
            ),
        )
        assert rng.min_start == 6
        assert rng.max_start == 19

    def test_right_trim_duration_cap_unchanged(self) -> None:
        rng = SubtitleEditPolicy.compute_trim_range(
            clip_id="cue", current_start=10, current_duration=10,
            neighbours=(
                {"clip_id": "nb", "timeline_start": 25, "timeline_duration": 5},
            ),
        )
        assert rng.max_duration == 15  # 25 - 10


# ================================================================
# AUD-15808: SCC pop-on 时序
# ================================================================

class TestAud15808SccTiming:
    def _cue(self, start: int, end: int) -> SubtitleClip:
        return SubtitleClip(
            clip_id=f"cue-{start}", track_id="sub", timeline_start=start,
            timeline_duration=end - start, text="HI",
            caption_metadata=CaptionCueMetadata(),
        )

    def test_cue_start_pops_on_and_cue_end_clears(self) -> None:
        scc = _render_scc([self._cue(0, 24)], 24.0)
        start_line, end_line = scc.splitlines()[1], scc.splitlines()[2]
        assert start_line.startswith("00:00:00:00\t")
        assert "9420" in start_line and "942f" in start_line  # 写完即上屏
        assert end_line == "00:00:01:00\t942c"  # 结束清屏

    def test_successive_cues_do_not_stack(self) -> None:
        scc = _render_scc([self._cue(0, 24), self._cue(24, 48)], 24.0)
        lines = scc.splitlines()
        # 第二个 cue 开始前有上一个 cue 的 942c 清屏
        assert lines[2] == "00:00:01:00\t942c"
        assert "9420" in lines[3] and "942f" in lines[3]
        assert lines[4] == "00:00:02:00\t942c"


# ================================================================
# AUD-15809: SCC drop-frame 时间码
# ================================================================

class TestAud15809SccDropFrame:
    RATE = 30000.0 / 1001.0

    def test_minute_boundary_skips_two_frames(self) -> None:
        assert _format_scc_time(1800, self.RATE) == "00:01:00:02"
        assert _format_scc_time(1799, self.RATE) == "00:00:59:29"

    def test_ten_minute_boundary(self) -> None:
        assert _format_scc_time(17981, self.RATE) == "00:09:59:29"
        assert _format_scc_time(17982, self.RATE) == "00:10:00:00"  # 整十分钟不跳帧
        assert _format_scc_time(17980, self.RATE) == "00:09:59:28"  # 块末不误跳下一分钟

    def _forward(self, tc: str) -> int:
        hh, mm, ss, ff = (int(part) for part in tc.split(":"))
        total_minutes = hh * 60 + mm
        return (total_minutes * 60 + ss) * 30 + ff - 2 * (total_minutes - total_minutes // 10)

    def test_roundtrip_against_smpte_forward_mapping(self) -> None:
        for frame in (0, 1, 29, 1798, 1799, 1800, 1801, 3598, 107892, 179999):
            tc = _format_scc_time(frame, self.RATE)
            assert self._forward(tc) == frame, f"{frame} -> {tc}"

    def test_non_drop_rate_unchanged(self) -> None:
        assert _format_scc_time(7199, 24.0) == "00:04:59:23"


# ================================================================
# AUD-15811: 字体扫描
# ================================================================

class TestAud15811FontScan:
    def test_no_hardcoded_families_without_files(self, monkeypatch) -> None:
        monkeypatch.setattr("os.path.isdir", lambda _p: True)
        monkeypatch.setattr("glob.glob", lambda _pattern, recursive=False: [])
        families = FontResolver._scan_filesystem_families()
        assert "Arial" not in families
        assert "Helvetica" not in families
        assert "Helvetica Neue" not in families
        assert "DejaVu Sans" not in families

    def test_otf_files_matched(self, monkeypatch) -> None:
        monkeypatch.setattr("os.path.isdir", lambda _p: True)

        def _fake_glob(pattern: str, recursive: bool = False) -> list[str]:
            if pattern.endswith("*.otf"):
                return ["/System/Library/Fonts/SourceHanSansSC-Regular.otf"]
            return ["/System/Library/Fonts/Arial.ttf"]

        monkeypatch.setattr("glob.glob", _fake_glob)
        families = FontResolver._scan_filesystem_families()
        assert "SourceHanSansSC" in families  # .otf 命中（-Regular 后缀清洗）
        assert "Arial" in families


# ================================================================
# AUD-15900: 四元组动画包络
# ================================================================

class TestAud15900EnvelopeAxes:
    def test_returns_four_tuple_with_separate_axes(self) -> None:
        opacity, tx, ty, scale = cue_animation_envelope(
            2, 8, in_animation="slide_left", out_animation="slide_up",
            in_frames=8, out_frames=8,
        )
        assert tx < 0  # 进入向左
        assert ty < 0  # 退出向上（Y-down）
        assert 0.0 < opacity < 1.0
        assert scale == 1.0

    def test_slide_up_enters_from_below_y_down(self) -> None:
        _o, _tx, ty, _s = cue_animation_envelope(
            0, 24, in_animation="slide_up", in_frames=12)
        assert ty > 0  # Y-down：从下方进入为正位移

    def test_none_constant(self) -> None:
        assert cue_animation_envelope(0, 24) == (1.0, 0.0, 0.0, 1.0)

    def test_signs_match_canonical_state(self) -> None:
        """包络位移符号与 caption_graphic_animation_state 的惯例一致。"""
        from src.features.edit.core.captions.graphics import (
            caption_graphic_animation_state,
        )

        params = {"in_frames": 12, "out_frames": 0, "distance": 1.0}
        canonical = caption_graphic_animation_state(
            "slide_up", 0, 24, params, content_width=100.0, content_height=50.0,
        )
        _o, _tx, env_ty, _s = cue_animation_envelope(
            0, 24, in_animation="slide_up", in_frames=12)
        assert (canonical.translate_y > 0) == (env_ty > 0)
        assert canonical.translate_x == 0.0 and _tx == 0.0


# ================================================================
# AUD-15908 / AUD-15909: mux 命令
# ================================================================

class TestAud15908MkvMuxer:
    def test_mkv_maps_to_matroska_muxer(self) -> None:
        command = build_caption_mux_command(
            "/tmp/v.mov", "/tmp/c.srt", "/tmp/out.mkv", container="mkv",
        )
        f_index = command.index("-f")
        assert command[f_index + 1] == "matroska"
        assert command[command.index("-c:s") + 1] == "srt"

    def test_mov_unchanged(self) -> None:
        command = build_caption_mux_command(
            "/tmp/v.mov", "/tmp/c.srt", "/tmp/out.mov", container="mov",
        )
        assert command[command.index("-f") + 1] == "mov"
        assert command[command.index("-c:s") + 1] == "mov_text"


class TestAud15909MxfRejected:
    def test_mxf_raises_at_construction(self) -> None:
        with pytest.raises(ValueError, match="MXF"):
            build_caption_mux_command(
                "/tmp/v.mov", "/tmp/c.srt", "/tmp/out.mxf", container="mxf",
            )


# ================================================================
# AUD-16001: category 往返
# ================================================================

class TestAud16001CategoryRoundtrip:
    def _registry_with_user_preset(self, category: str | None) -> StylePresetRegistry:
        reg = StylePresetRegistry(include_builtins=True)
        reg.register(CaptionStylePreset(
            preset_id="user.mine", label="Mine", style=CaptionStyle(),
            category=category,
        ))
        return reg

    def test_category_survives_roundtrip(self) -> None:
        reg = self._registry_with_user_preset("title")
        data = reg.to_dict()
        assert data[0]["category"] == "title"
        restored = StylePresetRegistry.from_dict(data)
        assert restored.get("user.mine").category == "title"

    def test_none_category_roundtrip(self) -> None:
        reg = self._registry_with_user_preset(None)
        restored = StylePresetRegistry.from_dict(reg.to_dict())
        assert restored.get("user.mine").category is None

    def test_builtin_categories_declared(self) -> None:
        categories = {p.category for p in BUILTIN_PRESETS}
        assert "social" in categories and "lower_third" in categories
