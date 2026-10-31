"""批 D 修复回归测试（wd9-g55）。

覆盖 REMEDIATION_PLAN 批 D 的 16 条 Major 条目：
AUD-16006/16100/16101/16103/16110/16204/16206/16207/16208/16209/
16210/16211/16212/16213/16304/16305。

每条用例对应一个审计 ID，断言"修复后语义"而非旧实现细节。
纯数据层测试（无 Qt / 无 GL）。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.shared.media.metadata import Rational

from src.features.edit.core.timeline_v2 import (
    CaptionGraphicBinding,
    ClipInstance,
    EditTrack,
    Send,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.core.commands import (
    AddBusCommand,
    AddEffectCommand,
    AppendClipCommand,
    CommandStack,
    DeleteBinCommand,
    MacroCommand,
    MoveClipsOverwriteCommand,
    ProjectCommandStack,
    RemoveBusCommand,
    SwitchMulticamAngleCommand,
    UpdateSubtitleClipCommand,
    _MACRO_SUBCOMMAND_TYPES,
)
import src.features.edit.core.commands as _commands_module
from src.features.edit.core.clip_params import (
    CropParams,
    RawParams,
    SpeedParams,
    SpeedPoint,
    TransformParams,
)
from src.features.edit.core.channel_layout import (
    ChannelLayout,
    bs1770_channel_weights,
    downmix,
)
from src.features.edit.core.chapter_export import format_youtube_chapters
from src.features.edit.core.composite_graph_data import CompositeGraphData
from src.features.edit.core.captions.translation import (
    CreateTranslatedSubtitleTrackCommand,
    build_translated_subtitle_track,
)


# ================================================================
# helpers
# ================================================================

def _video_timeline(*clips: ClipInstance, track_id: str = "v1") -> TimelineSequence:
    track = EditTrack(
        track_id=track_id, name="V1", track_type="video", index=0, clips=tuple(clips),
    )
    return TimelineSequence(
        timeline_id="seq", name="S", fps=Rational(24, 1),
        width=64, height=64, video_tracks=(track,),
    )


def _clip(clip_id: str, start: int, duration: int, *, asset_id: str = "a1",
          track_id: str = "v1", source_start: int = 100,
          speed: float | SpeedParams = 1.0) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=asset_id, track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=source_start, source_duration=duration,
        speed=speed,
    )


class _ChapterMarker:
    kind = "chapter"
    clip_id = None

    def __init__(self, frame: int, name: str) -> None:
        self.frame = frame
        self.name = name


# ================================================================
# AUD-16006: 命令 apply 内实时 LLM 谑用 → redo 确定性重放
# ================================================================

class TestAud16006TranslationRedoDeterministic:
    def _timeline(self) -> TimelineSequence:
        cues = tuple(
            SubtitleClip(clip_id=f"c{i}", track_id="src", timeline_start=i * 10,
                         timeline_duration=8, text=f"hello {i}")
            for i in range(3)
        )
        track = EditTrack(track_id="src", name="SRC", track_type="subtitle",
                          index=0, subtitle_clips=cues)
        return TimelineSequence(timeline_id="t", name="T", subtitle_tracks=(track,))

    def test_redo_replays_snapshot_without_invoking_translator(self):
        calls: list[list[str]] = []

        def translator(texts, language):
            calls.append(list(texts))
            return tuple(f"[{language}] {t}" for t in texts)

        timeline = self._timeline()
        stack = CommandStack()
        cmd = CreateTranslatedSubtitleTrackCommand(
            source_track_id="src", target_language="ja", translator=translator,
        )

        after_apply = stack.execute(cmd, timeline)
        assert len(calls) == 1
        ja_texts = sorted(
            cue.text
            for track in after_apply.subtitle_tracks if track.track_id != "src"
            for cue in track.subtitle_clips
        )

        after_undo = stack.undo(after_apply)
        assert [t.track_id for t in after_undo.subtitle_tracks] == ["src"]

        after_redo = stack.redo(after_undo)
        # 首次 apply 之后不再触网：redo 用持久化译文确定重放。
        assert len(calls) == 1, "redo must replay the captured translations"
        redone_texts = sorted(
            cue.text
            for track in after_redo.subtitle_tracks if track.track_id != "src"
            for cue in track.subtitle_clips
        )
        assert redone_texts == ja_texts == ["[ja] hello 0", "[ja] hello 1", "[ja] hello 2"]

    def test_build_with_pretranslated_skips_translator_and_validates(self):
        calls: list = []

        def translator(texts, language):
            calls.append(texts)
            return tuple(f"[{language}] {t}" for t in texts)

        track = EditTrack(track_id="src", name="SRC", track_type="subtitle", index=0)
        build_translated_subtitle_track(track, "ja", translator)  # 空 cue 无内容可翻
        assert calls == [()], "空 cue 时以空文本调用一次（provider 侧短路）"

        calls.clear()
        with pytest.raises(ValueError):
            build_translated_subtitle_track(
                track, "ja", translator, translations=("only-one", "too-many"),
            )
        assert calls == [], "预置译文路径不得调用 translator"


# ================================================================
# AUD-16100: BS.1770-4 环绕声道权重 1.41（对称）
# ================================================================

class TestAud16100Bs1770SurroundWeights:
    def test_51_weights_match_bs1770_4(self):
        w = bs1770_channel_weights(ChannelLayout.SURROUND_5_1)
        np.testing.assert_array_almost_equal(
            w, [1.0, 1.0, 1.0, 0.0, 1.41, 1.41])

    def test_71_weights_symmetric(self):
        w = bs1770_channel_weights(ChannelLayout.SURROUND_7_1)
        np.testing.assert_array_almost_equal(
            w, [1.0, 1.0, 1.0, 0.0, 1.41, 1.41, 1.41, 1.41])
        assert w[4] == w[5] == w[6] == w[7], "环绕声道必须左右对称"


# ================================================================
# AUD-16101: downmix 整型削波（无回绕）
# ================================================================

class TestAud16101DownmixIntegerClipping:
    def test_int16_no_wraparound(self):
        loud = np.full((6, 8), 30000.0, dtype=np.int16)
        out = downmix(loud, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert out.dtype == np.int16
        assert out.max() <= 32767 and out.min() >= -32768

    def test_int32_no_wraparound(self):
        loud = np.full((6, 8), 2.0e9, dtype=np.int32)
        out = downmix(loud, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert out.dtype == np.int32
        assert out.max() <= 2**31 - 1 and out.min() >= -(2**31)

    def test_float_passthrough_unchanged(self):
        samples = np.full((6, 4), 2.5, dtype=np.float32)
        out = downmix(samples, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert out.dtype == np.float32
        # Lo = L + 0.707*C + 0.707*Ls ≈ 3.91 > 1.0，浮点路径允许超界。
        assert float(out[0, 0]) > 3.0


# ================================================================
# AUD-16103: NaN/Inf 绕过范围校验 → 统一 isfinite 拦截
# ================================================================

class TestAud16103FiniteValidation:
    @pytest.mark.parametrize("ctor,kw", [
        (TransformParams, {"scale_x": float("nan")}),
        (TransformParams, {"scale_y": float("inf")}),
        (TransformParams, {"translate_x": float("nan")}),
        (TransformParams, {"rotation": float("inf")}),
        (TransformParams, {"pivot_x": float("nan")}),
        (CropParams, {"left": float("nan")}),
        (CropParams, {"feather": float("inf")}),
        (SpeedParams, {"speed": float("nan")}),
        (SpeedPoint, {"source_offset": 5, "speed": float("nan")}),
        (RawParams, {"wb_gain_g": float("nan")}),
        (RawParams, {"exposure_stops": float("inf")}),
        (RawParams, {"black_level": float("nan")}),
    ])
    def test_non_finite_rejected(self, ctor, kw):
        with pytest.raises(ValueError, match="finite"):
            ctor(**kw)

    def test_from_dict_nan_injected_project_data(self):
        # json.loads 默认接受 NaN 字面量——from_dict 路径同样必须拦截。
        with pytest.raises(ValueError, match="finite"):
            TransformParams.from_dict({"scale_x": float("nan")})
        with pytest.raises(ValueError, match="finite"):
            SpeedParams.from_dict({"speed": float("nan")})

    def test_valid_values_still_accepted(self):
        assert TransformParams(scale_x=5.0, rotation=-179.5).scale_x == 5.0
        assert CropParams(left=0.5, feather=12.0).feather == 12.0
        assert SpeedParams(speed=0.1).speed == 0.1
        assert RawParams(wb_gain_b=4.0, exposure_stops=-4.0).wb_gain_b == 4.0


# ================================================================
# AUD-16110: NTSC 分数帧率下 auto_first 不再产生两条 0:00
# ================================================================

class TestAud16110YoutubeChaptersAutoFirst:
    def test_ntsc_frame_29_is_still_zero_timestamp(self):
        text = format_youtube_chapters(
            [_ChapterMarker(29, "A"), _ChapterMarker(1800, "B")], 30000 / 1001.0)
        lines = text.splitlines()
        assert lines[0] == "0:00:00 A"
        zero_ts = [ln for ln in lines if ln.split(" ")[0] in ("0:00", "0:00:00")]
        assert len(zero_ts) == 1, "0:00 时间戳只能出现一条"

    def test_ntsc_frame_30_gets_prepended_chapter(self):
        text = format_youtube_chapters([_ChapterMarker(30, "A")], 30000 / 1001.0)
        assert text.splitlines()[0] == "0:00 Chapters"

    def test_integer_fps_behavior_unchanged(self):
        text = format_youtube_chapters([_ChapterMarker(24, "A")], 24.0)
        assert text.splitlines()[0] == "0:00 Chapters"
        text = format_youtube_chapters([_ChapterMarker(0, "A")], 24.0)
        assert text.splitlines()[0] == "0:00:00 A"


# ================================================================
# AUD-16204: undo/redo 抛异常时栈回填命令
# ================================================================

class _FailingUndoCommand:
    """undo 在前 N 次调用抛错、之后成功的可控命令（验证回填后可重试）。"""

    label = "failing-undo"

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times

    def apply(self, timeline):
        # R12 后 CommandStack 把"apply 返回原实例"判为 no-op 不入栈——
        # 本命令代表真实内容变更，必须返回新对象。
        return f"applied-{self.label}"

    def undo(self, timeline):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ValueError("undo boom")
        return f"undone-{self.label}"


class _FailingApplyCommand:
    label = "failing-apply"

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times

    def apply(self, timeline):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ValueError("apply boom")
        return f"applied-{self.label}"

    def undo(self, timeline):
        return timeline


class TestAud16204StackExceptionBackfill:
    def test_command_stack_undo_backfills_then_retry_succeeds(self):
        stack = CommandStack()
        ok = _FailingApplyCommand(fail_times=0)
        stack.execute(ok, "timeline")
        bad = _FailingUndoCommand(fail_times=1)
        stack.execute(bad, "timeline")

        with pytest.raises(ValueError, match="undo boom"):
            stack.undo("timeline")

        # 回填语义：失败的命令仍在栈顶且可重试（栈状态未被破坏）。
        assert stack.can_undo()
        recovered = stack.undo("timeline")
        assert recovered == "undone-failing-undo"
        # ok 命令仍在 undo 栈；bad 已进 redo 栈。
        assert stack.can_undo() and stack.can_redo()
        assert stack.undo("timeline") == "timeline"

    def test_command_stack_undo_failure_keeps_retrying_same_command(self):
        stack = CommandStack()
        bad = _FailingUndoCommand(fail_times=3)
        stack.execute(bad, "timeline")
        for _ in range(3):
            with pytest.raises(ValueError, match="undo boom"):
                stack.undo("timeline")
        assert stack.undo("timeline") == "undone-failing-undo"

    def test_command_stack_redo_backfills_on_failure(self):
        stack = CommandStack()
        cmd = _FailingApplyCommand(fail_times=0)
        stack.execute(cmd, "timeline")
        stack.undo("timeline")
        # 之后的 redo 才开始失败（模拟 apply 阶段网络/状态错误）。
        cmd.fail_times = 2
        for _ in range(2):
            with pytest.raises(ValueError, match="apply boom"):
                stack.redo("timeline")
        # 回填后 redo 仍可用并最终成功。
        assert stack.can_redo()
        assert stack.redo("timeline") == "applied-failing-apply"
        assert stack.can_undo()

    def test_project_command_stack_undo_backfills_on_failure(self):
        from src.features.edit.core.project import EditProject

        stack = ProjectCommandStack()
        project = EditProject.create("p")

        class _ProjectBadUndo:
            label = "bad"

            def __init__(self) -> None:
                self.fail_times = 1

            def apply(self, proj):
                return proj

            def undo(self, proj):
                if self.fail_times > 0:
                    self.fail_times -= 1
                    raise ValueError("project undo boom")
                return proj

        stack.execute(_ProjectBadUndo(), project)
        with pytest.raises(ValueError, match="project undo boom"):
            stack.undo(project)
        recovered = stack.undo(project)
        assert isinstance(recovered, EditProject)
        assert stack.can_redo() and not stack.can_undo()


# ================================================================
# AUD-16206: RemoveBusCommand undo 恢复 sends
# ================================================================

class TestAud16206RemoveBusRestoresSends:
    def _timeline(self) -> TimelineSequence:
        bus = EditTrack(track_id="bus1", name="Bus 1", track_type="audio",
                        index=0, is_bus=True)
        track = EditTrack(
            track_id="aud1", name="A1", track_type="audio", index=1,
            output_bus_id="master",
            sends=(
                Send(send_id="s1", target_bus_id="bus1", level=0.35, enabled=True),
                Send(send_id="s2", target_bus_id="other", level=0.8, enabled=False),
            ),
        )
        return TimelineSequence(
            timeline_id="t", name="T", audio_tracks=(bus, track),
        )

    def test_undo_restores_sends_with_level_and_enabled(self):
        timeline = self._timeline()
        after_apply, updated = RemoveBusCommand.create("bus1").apply(timeline)

        track = after_apply.get_track_by_id("aud1")
        assert [s.send_id for s in track.sends] == ["s2"], "指向被删 Bus 的 send 被移除"

        restored = updated.undo(after_apply)
        track = restored.get_track_by_id("aud1")
        sends = {s.send_id: s for s in track.sends}
        assert set(sends) == {"s1", "s2"}, "undo 必须恢复全部 sends"
        assert sends["s1"].target_bus_id == "bus1"
        assert sends["s1"].level == 0.35 and sends["s1"].enabled is True
        assert sends["s2"].target_bus_id == "other"

    def test_roundtrip_serialization_keeps_sends(self):
        from src.features.edit.core.timeline_v2 import Send as SendModel

        timeline = self._timeline()
        _, updated = RemoveBusCommand.create("bus1").apply(timeline)
        payload = updated.to_dict()
        assert payload["rerouted_tracks"][0][2] == [
            {"send_id": "s1", "target_bus_id": "bus1", "level": 0.35, "enabled": True},
            {"send_id": "s2", "target_bus_id": "other", "level": 0.8, "enabled": False},
        ]

        restored_cmd = RemoveBusCommand.from_dict(payload)
        assert restored_cmd._rerouted_tracks is not None
        track_id, output_bus, sends = restored_cmd._rerouted_tracks[0]
        assert track_id == "aud1" and output_bus == "master"
        assert all(isinstance(s, SendModel) for s in sends)
        assert [s.send_id for s in sends] == ["s1", "s2"]

    def test_legacy_two_field_snapshot_still_loads(self):
        legacy = RemoveBusCommand.from_dict({
            "command_id": "cmd", "label": "Remove Bus", "bus_id": "bus1",
            "old_bus": None,
            "rerouted_tracks": [["aud1", "master"]],
        })
        assert legacy._rerouted_tracks == [("aud1", "master")]
        # 旧格式 undo 不炸（sends 保持现状）。
        timeline = self._timeline()
        assert legacy.undo(timeline).get_track_by_id("aud1") is not None


# ================================================================
# AUD-16207: UpdateSubtitleClipCommand undo 对称同步 caption graphic
# ================================================================

class TestAud16207UndoSyncsCaptionGraphic:
    def _timeline(self) -> TimelineSequence:
        cue = SubtitleClip(
            clip_id="cue1", track_id="sub1", timeline_start=10,
            timeline_duration=20, text="hello",
        )
        sub_track = EditTrack(track_id="sub1", name="Sub", track_type="subtitle",
                              index=1, subtitle_clips=(cue,))
        graphic = _clip("graphic1", 10, 20, asset_id="cap-graphic")
        video = EditTrack(track_id="v1", name="V1", track_type="video",
                          index=0, clips=(graphic,))
        binding = CaptionGraphicBinding(
            caption_track_id="sub1", caption_cue_id="cue1",
            graphic_clip_id="graphic1", sync_timing=True,
        )
        return TimelineSequence(
            timeline_id="t", name="T", fps=Rational(24, 1),
            width=64, height=64,
            video_tracks=(video,), subtitle_tracks=(sub_track,),
            caption_graphic_bindings=(binding,),
        )

    def test_undo_resyncs_graphic_timing(self):
        timeline = self._timeline()
        cmd = UpdateSubtitleClipCommand.create(
            "cue1", {"timeline_start": 40, "timeline_duration": 30})
        stack = CommandStack()
        after_apply = stack.execute(cmd, timeline)

        graphic = after_apply.get_clip_by_id("graphic1")
        assert (graphic.timeline_start, graphic.timeline_duration) == (40, 30), \
            "apply 同步 graphic timing"

        after_undo = stack.undo(after_apply)
        graphic = after_undo.get_clip_by_id("graphic1")
        assert (graphic.timeline_start, graphic.timeline_duration) == (10, 20), \
            "AUD-16207: undo 后 graphic timing 必须随 cue 一起回退"

        cue = after_undo.get_subtitle_clip_by_id("cue1")
        assert (cue.timeline_start, cue.timeline_duration) == (10, 20)


# ================================================================
# AUD-16208: SwitchMulticamAngleCommand 精确 undo
# ================================================================

class TestAud16208MulticamPreciseUndo:
    def test_undo_restores_original_clip_exactly(self):
        original = ClipInstance(
            clip_id="mc1", asset_id="camA", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=500, source_duration=100,
            fade_in_duration=3, fade_out_duration=5,
        )
        timeline = _video_timeline(original)

        after_apply, updated = SwitchMulticamAngleCommand.create(
            "v1", "mc1", 60, "camB", angle_sync_offset=17,
        ).apply(timeline)
        # split 后两段；右段被重指向 camB。
        track = after_apply.get_track_by_id("v1")
        assert len(track.clips) == 2
        assert track.get_clip_by_id(updated._right_clip_id).asset_id == "camB"

        after_undo = updated.undo(after_apply)
        track = after_undo.get_track_by_id("v1")
        assert len(track.clips) == 1, "undo 必须合并回单片段"
        restored = track.get_clip_by_id("mc1")
        assert restored.asset_id == "camA"
        assert restored.source_start == 500 and restored.source_duration == 100
        assert restored.timeline_duration == 100
        assert restored.fade_in_duration == 3 and restored.fade_out_duration == 5

    def test_fallback_path_raises_instead_of_swallowing(self):
        # 无快照（模拟历史序列化数据）且片段已不在 → 通用合并失败，
        # 必须上抛而非吞掉异常返回未回滚的时间线。
        original = ClipInstance(
            clip_id="mc1", asset_id="camA", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        )
        timeline = _video_timeline(original)
        _, updated = SwitchMulticamAngleCommand.create(
            "v1", "mc1", 60, "camB").apply(timeline)
        # 伪造"快照缺失"的旧数据命令。
        legacy = SwitchMulticamAngleCommand(
            command_id=updated.command_id, label=updated.label,
            track_id="v1", clip_id="mc1", switch_frame=60,
            angle_asset_id="camB", _did_split=True,
            _right_clip_id=updated._right_clip_id,
            _original_asset_id="camA", _original_source_start=0,
            _original_clip=None,
        )
        # 破坏邻接：切点邻域的片段已不存在（空轨道），合并无从进行。
        broken = _video_timeline()
        with pytest.raises((ValueError, AttributeError)):
            legacy.undo(broken)


# ================================================================
# AUD-16209: 覆盖裁剪的保留段源窗口精确映射
# ================================================================

class TestAud16209OverwriteRetainedSourceWindow:
    def test_half_speed_retained_window_exact(self):
        # speed=0.5：时间线帧 1→源 100+int(0.5)=100，帧 2→101。
        # 旧实现 duration=int(2*0.5)=1 只保留 1 帧（丢帧）。
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=80,
            source_start=100, source_duration=40, speed=0.5,
        )
        retained = MoveClipsOverwriteCommand._subtract_overwrite_ranges(
            clip, [(0, 1), (3, 80)])
        assert len(retained) == 1
        assert retained[0].timeline_start == 1
        assert retained[0].timeline_duration == 2
        assert retained[0].source_start == 100
        assert retained[0].source_duration == 2, \
            "半速下 2 个时间线帧对应 2 个源帧，不得截断丢帧"

    def test_adjacent_retained_segments_share_boundary_no_drift(self):
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=60,
            source_start=200, source_duration=30, speed=0.5,
        )
        # 覆盖中段 [20,40)：保留 [0,20) 与 [40,60)。
        retained = MoveClipsOverwriteCommand._subtract_overwrite_ranges(
            clip, [(20, 40)])
        assert [c.timeline_start for c in retained] == [0, 40]
        first, second = retained
        # 20 个时间线帧 @0.5 → 各 10 个源帧，且窗口按时间线顺序衔接：
        # [200,210) 与 [220,230)（中间 [210,220) 是被覆盖丢弃的内容）。
        assert (first.source_start, first.source_duration) == (200, 10)
        assert (second.source_start, second.source_duration) == (220, 10)

    def test_apply_path_keeps_source_window_consistent(self):
        # 跨轨覆盖：v2 的片段移动覆盖 v1 目标片段中段 [30,70)。
        target = _clip("target", 0, 100, source_start=1000)
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(target,))
        moving = _clip("moving", 0, 40, asset_id="a2", track_id="v2", source_start=0)
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                       clips=(moving,))
        timeline = TimelineSequence(
            timeline_id="seq", name="S", fps=Rational(24, 1),
            width=64, height=64, video_tracks=(v1, v2),
        )
        after, _updated = MoveClipsOverwriteCommand.create([{
            "clip_id": "moving", "source_track_id": "v2",
            "target_track_id": "v1", "new_start": 30,
        }]).apply(timeline)
        track = after.get_track_by_id("v1")
        by_id = {c.clip_id: c for c in track.clips}
        # target 被覆盖掉 [30,70)：前段保留 [0,30)，源窗 [1000,1030)。
        assert by_id["target"].timeline_start == 0
        assert by_id["target"].timeline_duration == 30
        assert by_id["target"].source_start == 1000
        assert by_id["target"].source_duration == 30
        # moving 落位 [30,70)。
        assert by_id["moving"].timeline_start == 30


# ================================================================
# AUD-16210: MacroCommand.from_dict 全量注册表
# ================================================================

class TestAud16210MacroFromDictRegistry:
    def test_roundtrip_restores_non_legacy_subcommands(self):
        macro = MacroCommand.create("Batch", [
            _clip_cmd_for_serialization(),
        ])
        payload = macro.to_dict()
        restored = MacroCommand.from_dict(payload)
        assert len(restored.sub_commands) == 1, "不再静默丢弃未识别子命令"

    def test_unknown_command_type_raises(self):
        with pytest.raises(ValueError, match="unknown command_type"):
            MacroCommand.from_dict({
                "command_id": "m", "label": "M",
                "sub_commands": [
                    {"command_type": "TrimClipCommand", "track_id": "v1",
                     "clip_id": "c1", "old_end": 10, "new_end": 8},
                    {"command_type": "NoSuchCommand", "x": 1},
                ],
            })

    def test_missing_command_type_raises(self):
        with pytest.raises(ValueError, match="unknown command_type"):
            MacroCommand.from_dict({
                "command_id": "m", "label": "M",
                "sub_commands": [{"track_id": "v1"}],
            })

    def test_registry_covers_every_serializable_timeline_command(self):
        # 完备性：新增带 from_dict 的时间线命令必须登记进注册表。
        missing = []
        for name in dir(_commands_module):
            obj = getattr(_commands_module, name)
            if not (isinstance(obj, type) and obj.__module__ == _commands_module.__name__):
                continue
            if not hasattr(obj, "from_dict"):
                continue
            if name in ("MacroCommand",):
                continue  # 自身已登记（此处避免自引用干扰报告）
            doc = getattr(obj.apply, "__doc__", "") or ""
            import inspect
            try:
                sig = inspect.signature(obj.apply)
                params = [p for p in sig.parameters.values() if p.name != "self"]
                first_ann = params[0].annotation if params else None
            except (TypeError, ValueError):
                continue
            ann_name = getattr(first_ann, "__name__", str(first_ann))
            if ann_name == "TimelineSequence" and name not in _MACRO_SUBCOMMAND_TYPES:
                missing.append(name)
        assert not missing, f"注册表遗漏时间线命令: {missing}"
        assert "MacroCommand" in _MACRO_SUBCOMMAND_TYPES


def _clip_cmd_for_serialization():
    from src.features.edit.core.commands import UpdateClipParamCommand
    return UpdateClipParamCommand(
        command_id="u1", label="U", track_id="v1", clip_id="c1",
        param_type="opacity", new_value=0.5,
    )


# ================================================================
# AUD-16211: AddEffectCommand undo 按精确 id 移除
# ================================================================

class _FakeEffectDef:
    """最小 effect 定义（测试环境无插件目录，registry 为空）。"""

    def __init__(self, effect_type: str) -> None:
        self.effect_type = effect_type

    def create_instance(self, order: int = 0):
        from src.features.edit.core.effect_instance import EffectInstance
        return EffectInstance(
            effect_type=self.effect_type, params={}, order=order)


class _FakeEffectRegistry:
    def __init__(self, *types: str) -> None:
        self._defs = {t: _FakeEffectDef(t) for t in types}

    def get_definition(self, effect_type: str):
        return self._defs.get(effect_type)


@pytest.fixture
def fake_effect_registry(monkeypatch):
    registry = _FakeEffectRegistry("gaussian_blur")
    # P6 拆分·第六刀：AddEffectCommand 已搬至 effect_commands 子模块，
    # get_effect_registry 的模块级引用随之迁移——打桩必须落在定义它的
    # 子模块上，patch 包 __init__ 的属性不会影响子模块内的名字解析。
    import src.features.edit.core.commands.effect_commands as _fx_commands
    monkeypatch.setattr(_fx_commands, "get_effect_registry",
                        lambda: registry)
    return registry


class TestAud16211AddEffectPersistsInstance:
    def test_undo_removes_exact_instance_with_sibling_same_type(
            self, fake_effect_registry):
        clip = _clip("c1", 0, 10)
        timeline = _video_timeline(clip)
        stack = CommandStack()

        first = AddEffectCommand.create("v1", "c1", "gaussian_blur")
        after_first = stack.execute(first, timeline)
        second = AddEffectCommand.create("v1", "c1", "gaussian_blur")
        after_second = stack.execute(second, after_first)

        effects_after = after_second.get_clip_by_id("c1").effects
        assert len(effects_after) == 2
        first_ids = {e.effect_id for e in after_first.get_clip_by_id("c1").effects}

        after_undo = stack.undo(after_second)
        effects_undo = after_undo.get_clip_by_id("c1").effects
        remaining_ids = {e.effect_id for e in effects_undo}
        # 撤销第二次 add：必须精确移除 second 实例，保留 first。
        assert remaining_ids == first_ids, \
            "同类型多实例时 undo 必须移除本次添加的那个（按 id），不得误删 first"

    def test_undo_persisted_id_survives_between_apply_and_undo(
            self, fake_effect_registry):
        clip = _clip("c1", 0, 10)
        timeline = _video_timeline(clip)
        cmd = AddEffectCommand.create("v1", "c1", "gaussian_blur")
        after_apply = cmd.apply(timeline)
        assert cmd._effect_id is not None, "apply 必须持久化生成的 effect id"
        assert cmd._added_effect is not None
        assert cmd._added_effect.effect_id == cmd._effect_id
        after_undo = cmd.undo(after_apply)
        assert after_undo.get_clip_by_id("c1").effects == ()


# ================================================================
# AUD-16212: AppendClipCommand 用 ID 差集精确识别新片段
# ================================================================

class TestAud16212AppendClipIdentifiesNewClip:
    def test_undo_removes_new_clip_not_same_asset_sibling(self):
        old_clip = _clip("old", 0, 10, asset_id="same-asset", source_start=0)
        timeline = _video_timeline(old_clip)

        cmd = AppendClipCommand(
            command_id="ap1", label="Append", asset_id="same-asset",
            in_frame=100, out_frame=109, track_id="v1",
        )
        stack = CommandStack()
        after_apply = stack.execute(cmd, timeline)

        track = after_apply.get_track_by_id("v1")
        assert [c.clip_id for c in track.clips if c.clip_id == "old"] == ["old"]
        assert len(track.clips) == 2

        after_undo = stack.undo(after_apply)
        track = after_undo.get_track_by_id("v1")
        # 撤销后：旧的同素材片段保留，新追加的被移除。
        assert [c.clip_id for c in track.clips] == ["old"]
        assert track.clips[0].source_start == 0

    def test_inserted_id_is_the_diff_result(self):
        old_clip = _clip("old", 0, 10, asset_id="same-asset", source_start=0)
        timeline = _video_timeline(old_clip)
        cmd = AppendClipCommand.create("same-asset", 50, 59, "v1")
        after_apply, updated = cmd.apply(timeline)
        new_ids = {
            c.clip_id for c in after_apply.get_track_by_id("v1").clips
        } - {"old"}
        assert updated._inserted_clip_id in new_ids


# ================================================================
# AUD-16213: DeleteBinCommand 快照/恢复后代 bin
# ================================================================

class TestAud16213DeleteBinRestoresDescendants:
    def _project_with_tree(self):
        from src.features.edit.core.project import EditProject

        project = EditProject.create("p")
        project = project.create_bin("Parent", parent_id=project.root_bin_id)
        parent_id = next(
            bid for bid, b in project.bins.items() if b.name == "Parent")
        project = project.create_bin("Child", parent_id=parent_id)
        child_id = next(
            bid for bid, b in project.bins.items() if b.name == "Child")
        project = project.create_bin("Grandchild", parent_id=child_id)
        grandchild_id = next(
            bid for bid, b in project.bins.items() if b.name == "Grandchild")
        return project, parent_id, child_id, grandchild_id

    def test_apply_snapshots_descendants(self):
        project, parent_id, child_id, grandchild_id = self._project_with_tree()
        cmd = DeleteBinCommand.create(parent_id)
        after_apply = cmd.apply(project)
        assert parent_id not in after_apply.bins
        ids = {d["bin_id"] for d in cmd._deleted_descendants}
        assert ids == {child_id, grandchild_id}, "快照必须覆盖全部后代"

    def test_undo_roundtrip_restores_hierarchy(self):
        project, parent_id, child_id, grandchild_id = self._project_with_tree()
        cmd = DeleteBinCommand.create(parent_id)
        after_apply = cmd.apply(project)
        after_undo = cmd.undo(after_apply)

        assert parent_id in after_undo.bins
        assert after_undo.bins[child_id].parent_id == parent_id
        assert after_undo.bins[grandchild_id].parent_id == child_id

    def test_undo_restores_descendants_reparented_meanwhile(self):
        # remove_bin 若将子 bin 重挂 root（或外部编辑改挂），undo 必须还原
        # 原父子关系。
        from src.features.edit.core.project import EditProject, ProjectBin

        project, parent_id, child_id, grandchild_id = self._project_with_tree()
        cmd = DeleteBinCommand.create(parent_id)
        after_apply = cmd.apply(project)
        # 模拟 reparent 副作用：child 被改挂到 root。
        reparented = replace_bin_parent(after_apply, child_id, after_apply.root_bin_id)

        after_undo = cmd.undo(reparented)
        assert after_undo.bins[parent_id].parent_id == project.root_bin_id
        assert after_undo.bins[child_id].parent_id == parent_id, \
            "AUD-16213: undo 必须把被改挂的子 bin 还原回原父"

    def test_roundtrip_serialization(self):
        project, parent_id, child_id, _gc = self._project_with_tree()
        cmd = DeleteBinCommand.create(parent_id)
        cmd.apply(project)
        payload = cmd.to_dict()
        assert [d["bin_id"] for d in payload["deleted_descendants"]] or \
            payload["deleted_descendants"] == []

        restored = DeleteBinCommand.from_dict(payload)
        assert {d["bin_id"] for d in restored._deleted_descendants} == \
            {d["bin_id"] for d in cmd._deleted_descendants}


def replace_bin_parent(project, bin_id: str, new_parent: str | None):
    """测试工具：把某 bin 的 parent_id 换成 new_parent（不可变替换）。"""
    from src.features.edit.core.project import ProjectBin

    old = project.bins[bin_id]
    updated = ProjectBin(
        bin_id=old.bin_id, name=old.name, parent_id=new_parent,
        asset_ids=old.asset_ids, timeline_ids=old.timeline_ids,
    )
    new_bins = dict(project.bins)
    new_bins[bin_id] = updated
    from dataclasses import replace
    return replace(project, bins=new_bins)


# ================================================================
# AUD-16304: composite_graph_data with_* 快照隔离
# ================================================================

class TestAud16304WithMethodsIsolateContainers:
    def _graph(self) -> CompositeGraphData:
        return CompositeGraphData.create_default_for_clip(
            "clip1", "asset1", "/tmp/a.mov")

    def test_mutating_original_does_not_leak_into_snapshot(self):
        g1 = self._graph()
        g2 = g1.with_name("renamed")
        g1.nodes[0]["parameters"]["clip_id"] = "MUTATED"
        g1.viewer_state["zoom"] = 9.9
        assert g2.nodes[0]["parameters"].get("clip_id") == "clip1"
        assert "zoom" not in g2.viewer_state

    def test_caller_supplied_containers_are_copied(self):
        g1 = self._graph()
        nodes = [{"id": "n", "type": "blank"}]
        g2 = g1.with_nodes(nodes)
        nodes[0]["id"] = "HACKED"
        assert g2.nodes[0]["id"] == "n"

        viewer_state = {"zoom": 1.0}
        g3 = g1.with_full_graph_data(viewer_state=viewer_state)
        viewer_state["zoom"] = 99.0
        assert g3.viewer_state["zoom"] == 1.0

    def test_all_with_variants_refresh_modified_at_and_keep_ids(self):
        import time as _time

        g1 = self._graph()
        variants = [
            g1.with_name("x"), g1.with_nodes(list(g1.nodes)),
            g1.with_connections(list(g1.connections)),
            g1.with_resolution(1280, 720), g1.with_graph_state([], []),
            g1.with_viewer_state({}), g1.with_viewport_state({}),
            g1.with_full_graph_data(),
        ]
        old_modified = _time.time()
        for v in variants:
            assert v.composite_id == g1.composite_id
            assert v.modified_at >= g1.modified_at
            assert v.modified_at >= old_modified - 5.0


# ================================================================
# AUD-16305: from_dict 对 null 字段容错
# ================================================================

class TestAud16305FromDictNullFields:
    def test_explicit_nulls_fall_back_to_defaults(self):
        graph = CompositeGraphData.create_default_for_clip(
            "clip1", "asset1", "/tmp/a.mov")
        payload = graph.to_dict()
        for key in ("nodes", "connections", "viewer_state", "viewport_state",
                    "backdrops", "sticky_notes", "groups", "width", "height",
                    "fps", "name", "created_at", "modified_at",
                    "duration_frames", "source_asset_path"):
            payload[key] = None
        loaded = CompositeGraphData.from_dict(payload)
        assert loaded.node_count == 0
        assert loaded.connection_count == 0
        assert loaded.viewer_state == {} and loaded.viewport_state == {}
        assert (loaded.width, loaded.height) == (1920, 1080)
        assert loaded.fps == 24.0 and loaded.duration_frames == 0
        assert loaded.name == "" and loaded.source_asset_path == ""

    def test_null_composite_id_regenerates_instead_of_crashing(self):
        loaded = CompositeGraphData.from_dict({"composite_id": None})
        assert loaded.composite_id, "null composite_id 应回落生成新 id"

    def test_normal_roundtrip_unchanged(self):
        graph = CompositeGraphData.create_default_for_clip(
            "clip1", "asset1", "/tmp/a.mov")
        loaded = CompositeGraphData.from_dict(graph.to_dict())
        assert loaded.node_count == graph.node_count
        assert loaded.connection_count == graph.connection_count
        assert loaded.width == graph.width and loaded.height == graph.height
