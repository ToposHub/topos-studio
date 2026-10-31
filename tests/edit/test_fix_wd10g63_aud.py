"""wd10-g63 批 D 修复回归测试（timeline ops / preview / 转场域，16 条 Major）。

对应 docs/audit/2026-09-28/batches/batch-188/189/190/191/192-features-edit-core.md：

- AUD-18802 roll_trim_clips source 侧按 effective_speed 缩放
- AUD-18803 insert_clip_at_playhead 分割跨越播放头的片段
- AUD-18804 roll / ripple 右延长的素材上界校验（max_source_frames）
- AUD-18805 remove/update_transition 经 _rebuild 保留轨道设置
- AUD-18901 GPU 路径 solid 分支缩进（sc 定义/使用同块）
- AUD-18902 PreviewRenderWorker.stop() 不在超时后销毁在飞解码资源
- AUD-18903 CPU 回退 limited-range yuv420p 摆幅拉伸
- AUD-19008 缩略图缓存 LRU 上限
- AUD-19100 EditTrack with_* 五方法保留 is_sync_locked / track_role
- AUD-19101 int/float fps 归一化为 Rational
- AUD-19102/19103 automation 反序列化收窄异常 + 告警
- AUD-19104 frozen 模型不内嵌可变共享子对象
- AUD-19202 dip 判定继承用户转场 base（catalog + 预览 CPU 路径）
- AUD-19207 无几何 mask 类型回退常数溶解而非方向 wipe
- AUD-19210 get_wipe_params 对 angle/softness 的坏值防护

无 GL / 无真实媒体；PyQt6 仅在 import 层面依赖（不创建 QApplication）。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.keyframe import Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational
from src.features.edit.core import custom_transitions as ct
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    TrackEffectInstance,
    TransitionInstance,
)
from src.features.edit.core.timeline_ops import (
    TimelineOperationError,
    insert_clip_at_playhead,
    remove_transition,
    ripple_trim_clip_right,
    roll_trim_clips,
    update_transition,
)
from src.features.edit.core.timeline_thumbnail_scheduler import (
    THUMBNAIL_CACHE_MAX_ENTRIES,
    TimelineThumbnailScheduler,
)
from src.features.edit.core.transition_catalog import (
    is_composite_transition,
    is_dip_transition,
    is_full_frame_transition,
    resolve_transition_base_type,
)
from src.features.edit.core.transition_mask import compute_transition_mask
from src.features.edit.core.transition_wipe import compute_wipe_mask, get_wipe_params


# ================================================================
# Fixtures / helpers
# ================================================================


def make_clip(clip_id: str, start: int, duration: int, source_start: int = 0,
              source_duration: int | None = None, speed: float = 1.0) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset1",
        track_id="track1",
        timeline_start=start,
        timeline_duration=duration,
        source_start=source_start,
        source_duration=source_duration if source_duration is not None else duration,
        speed=speed,
    )


def make_sequence(clips: tuple[ClipInstance, ...], **track_kwargs: Any) -> TimelineSequence:
    track = EditTrack(track_id="track1", track_type="video", clips=clips, **track_kwargs)
    return TimelineSequence(video_tracks=(track,), fps=Rational(24, 1))


@pytest.fixture(autouse=True)
def _clean_user_transition_registry():
    """用户自定义转场注册表用例间隔离（AUD-19202 相关）。"""
    ct._REGISTRY.clear()
    yield
    ct._REGISTRY.clear()


def register_user_transition(type_id: str, base: str, params: dict | None = None) -> None:
    ct._REGISTRY[type_id] = ct.CustomTransition(
        type_id=type_id,
        label=type_id,
        base=base,
        params=params or {},
        spatial=base in ct._SPATIAL_BASES,
    )


# ================================================================
# AUD-18802: roll_trim_clips source 侧按 effective_speed 缩放
# ================================================================


class TestRollSpeedScaling:
    def test_roll_speed_2x_scales_source(self) -> None:
        left = make_clip("left", 0, 100, 0, 100, speed=2.0)
        right = make_clip("right", 100, 100, 0, 100, speed=2.0)
        seq = make_sequence((left, right))

        result = roll_trim_clips(seq, "left", "right", 10)

        got_left = result.get_clip_by_id("left")
        got_right = result.get_clip_by_id("right")
        assert got_left is not None and got_right is not None
        # timeline 侧 +10；source 侧按 speed=2.0 缩放为 +20（旧实现 +10）
        assert got_left.timeline_duration == 110
        assert got_left.source_duration == 120
        assert got_right.source_start == 20
        assert got_right.source_duration == 80

    def test_roll_speed_half_scales_source(self) -> None:
        left = make_clip("left", 0, 100, 0, 100, speed=0.5)
        right = make_clip("right", 100, 100, 0, 100, speed=0.5)
        seq = make_sequence((left, right))

        result = roll_trim_clips(seq, "left", "right", 10)

        got_left = result.get_clip_by_id("left")
        assert got_left is not None
        assert got_left.source_duration == 105  # int(10 * 0.5)

    def test_roll_speed_1x_unchanged(self) -> None:
        left = make_clip("left", 0, 100)
        right = make_clip("right", 100, 100)
        seq = make_sequence((left, right))

        result = roll_trim_clips(seq, "left", "right", 10)

        got_left = result.get_clip_by_id("left")
        got_right = result.get_clip_by_id("right")
        assert got_left is not None and got_right is not None
        assert got_left.source_duration == 110
        assert got_right.source_start == 10

    def test_roll_left_uses_scaled_source_lower_bound(self) -> None:
        # 右片段 source_start=5，speed=2.0：左移 5 帧（timeline）需要 10 帧
        # source 余量 → 越界拒绝（旧实现按 timeline 帧数误判为合法）。
        left = make_clip("left", 0, 100, 0, 100, speed=2.0)
        right = make_clip("right", 100, 100, 5, 100, speed=2.0)
        seq = make_sequence((left, right))

        with pytest.raises(TimelineOperationError):
            roll_trim_clips(seq, "left", "right", -5)


# ================================================================
# AUD-18804: 素材上界校验
# ================================================================


class TestSourceUpperBound:
    def test_roll_left_extension_bounded(self) -> None:
        left = make_clip("left", 0, 100, 0, 100)
        right = make_clip("right", 100, 100, 0, 100)
        seq = make_sequence((left, right))

        # 素材仅 105 帧：roll +10 需要 source 到 110 → 拒绝
        with pytest.raises(TimelineOperationError):
            roll_trim_clips(seq, "left", "right", 10, max_source_frames=105)
        # 素材充足：接受
        ok = roll_trim_clips(seq, "left", "right", 10, max_source_frames=200)
        assert ok.get_clip_by_id("left").source_duration == 110

    def test_roll_no_asset_info_keeps_legacy_permissive(self) -> None:
        # max_source_frames=None（调用方无素材长度）维持既有宽容行为，
        # 不因修复而使鼠标 roll 延长不可用。
        left = make_clip("left", 0, 100)
        right = make_clip("right", 100, 100)
        seq = make_sequence((left, right))
        ok = roll_trim_clips(seq, "left", "right", 10)
        assert ok.get_clip_by_id("left").source_duration == 110

    def test_ripple_right_extension_bounded(self) -> None:
        clip = make_clip("b", 100, 100, 100, 100)
        seq = make_sequence((clip,))

        # 素材 150 帧：右延 50 帧 source 到 200 → 拒绝
        with pytest.raises(TimelineOperationError):
            ripple_trim_clip_right(seq, "b", 250, max_source_frames=150)
        # 素材充足：接受
        ok = ripple_trim_clip_right(seq, "b", 250, max_source_frames=300)
        assert ok.get_clip_by_id("b").source_duration == 150

    def test_ripple_right_shortening_unaffected_by_bound(self) -> None:
        clip = make_clip("b", 100, 100, 100, 100)
        seq = make_sequence((clip,))
        ok = ripple_trim_clip_right(seq, "b", 150, max_source_frames=100)
        assert ok.get_clip_by_id("b").source_duration == 50


# ================================================================
# AUD-18803: insert 分割跨越播放头的片段
# ================================================================


class TestInsertSplitsStraddling:
    def test_insert_mid_clip_splits_and_ripples(self) -> None:
        x = make_clip("x", 0, 300, 0, 300)
        seq = make_sequence((x,))

        result = insert_clip_at_playhead(
            seq, asset_id="asset2", in_frame=0, out_frame=9,
            playhead=100, track_id="track1",
        )

        track = result.get_track_by_id("track1")
        assert track is not None
        # x 左半片保留原 id：[0,100)；新片段 [100,110)；x 右半片 [110,310)
        by_id = {c.clip_id: c for c in track.clips}
        assert len(by_id) == 3
        assert by_id["x"].timeline_start == 0
        assert by_id["x"].timeline_end == 100
        assert by_id["x"].source_duration == 100
        inserted = [c for c in track.clips if c.asset_id == "asset2"]
        assert len(inserted) == 1
        assert inserted[0].timeline_start == 100
        assert inserted[0].timeline_end == 110
        right_parts = [
            c for c in track.clips
            if c.asset_id == "asset1" and c.clip_id != "x"
        ]
        assert len(right_parts) == 1
        assert right_parts[0].timeline_start == 110
        assert right_parts[0].source_start == 100

    def test_insert_at_clip_boundary_no_split(self) -> None:
        a = make_clip("a", 0, 100)
        b = make_clip("b", 100, 100)
        seq = make_sequence((a, b))

        result = insert_clip_at_playhead(
            seq, asset_id="asset2", in_frame=0, out_frame=9,
            playhead=100, track_id="track1",
        )
        track = result.get_track_by_id("track1")
        assert track is not None
        # 无分割：a 原样，b 后移 10，新片段 [100,110)
        assert len(track.clips) == 3
        by_id = {c.clip_id: c for c in track.clips}
        assert by_id["a"].timeline_end == 100
        assert by_id["b"].timeline_start == 110
        assert any(
            c.clip_id not in ("a", "b") and c.timeline_start == 100
            for c in track.clips
        )

    def test_insert_mid_clip_speed_split_alignment(self) -> None:
        # 变速片段的分割 source 侧按 effective_speed 缩放（split 同口径）。
        # speed=2.0、timeline [0,200) → source 400 帧。
        x = make_clip("x", 0, 200, 0, 400, speed=2.0)
        seq = make_sequence((x,))

        result = insert_clip_at_playhead(
            seq, asset_id="asset2", in_frame=0, out_frame=9,
            playhead=100, track_id="track1",
        )
        track = result.get_track_by_id("track1")
        assert track is not None
        by_id = {c.clip_id: c for c in track.clips}
        # 左半片：timeline 100 → source 200
        assert by_id["x"].source_duration == 200
        right = [c for c in track.clips if c.asset_id == "asset1" and c.clip_id != "x"]
        assert len(right) == 1
        assert right[0].source_start == 200
        assert right[0].source_duration == 200
        # 新片段未传 speed（默认 1.0）→ timeline_duration 10 → 右半片起始 110
        assert right[0].timeline_start == 110


# ================================================================
# AUD-18805: 转场操作的轨道字段保留
# ================================================================


def make_track_with_transition() -> tuple[TimelineSequence, EditTrack]:
    a = make_clip("a", 0, 100)
    b = make_clip("b", 100, 100)
    trans = TransitionInstance(
        transition_id="tr1", transition_type="cross_dissolve",
        track_id="track1", left_clip_id="a", right_clip_id="b",
        center_frame=100, duration=10, alignment="centered",
    )
    track = EditTrack(
        track_id="track1", track_type="audio", clips=(a, b),
        transitions=(trans,),
        volume=1.8, pan=-0.5, is_sync_locked=False, is_visible=False,
        is_muted=True, is_solo=True, color="#ff5722", stem_group="music",
        automation={"volume": KeyframeTrack(
            param_name="volume", keyframes=[Keyframe(frame=0, value=1.5)])},
    )
    return TimelineSequence(audio_tracks=(track,), fps=Rational(24, 1)), track


class TestTransitionOpsPreserveTrackFields:
    def _assert_preserved(self, got: EditTrack) -> None:
        assert got.volume == 1.8
        assert got.pan == -0.5
        assert got.is_sync_locked is False
        assert got.is_visible is False
        assert got.is_muted is True
        assert got.is_solo is True
        assert got.color == "#ff5722"
        assert got.stem_group == "music"
        assert "volume" in got.automation

    def test_remove_transition_preserves_track_settings(self) -> None:
        seq, track = make_track_with_transition()
        result = remove_transition(seq, "tr1", "track1")
        got = result.get_track_by_id("track1")
        assert got is not None
        assert got.transitions == ()
        self._assert_preserved(got)

    def test_update_transition_preserves_track_settings(self) -> None:
        seq, track = make_track_with_transition()
        result = update_transition(seq, "tr1", "track1", duration=20)
        got = result.get_track_by_id("track1")
        assert got is not None
        assert len(got.transitions) == 1
        assert got.transitions[0].duration == 20
        self._assert_preserved(got)


# ================================================================
# AUD-19100: EditTrack with_* 保留 is_sync_locked / track_role
# ================================================================


TRACK_METHOD_CASES = [
    ("with_strip_eq", {"new_strip_eq": None}),
    ("with_effect_chain", {"new_chain": None}),
    ("with_name", {"name": "renamed"}),
    ("with_color", {"color": "#00ff00"}),
    ("with_output_bus", {"output_bus_id": "bus1"}),
]


class TestWithMethodsPreserveFields:
    @pytest.mark.parametrize("method,kwargs", TRACK_METHOD_CASES)
    def test_sync_lock_and_role_preserved(self, method: str, kwargs: dict) -> None:
        track = EditTrack(
            track_id="t1", track_type="video", track_role="graphics",
            is_sync_locked=False, volume=1.5, pan=0.25, color="#112233",
            stem_group="music",
        )
        derived = getattr(track, method)(**kwargs)
        assert derived.is_sync_locked is False
        assert derived.track_role == "graphics"
        # 其余字段也不回落默认
        assert derived.volume == 1.5
        assert derived.pan == 0.25
        assert derived.color == "#112233" or method == "with_color"
        assert derived.stem_group == "music"

    def test_graphics_track_still_resolvable_after_rename(self) -> None:
        track = EditTrack(
            track_id="t1", track_type="video", track_role="graphics",
            is_sync_locked=False,
        )
        seq = TimelineSequence(
            video_tracks=(track.with_name("V2"),), fps=Rational(24, 1),
        )
        assert seq.get_graphics_track() is not None


# ================================================================
# AUD-19101: int/float fps 归一化
# ================================================================


class TestFpsNormalization:
    def test_int_fps_serializes(self) -> None:
        seq = TimelineSequence(timeline_id="s", fps=30)
        assert isinstance(seq.fps, Rational)
        assert seq.to_dict()["fps"] == "30/1"

    def test_float_fps_preserves_precision(self) -> None:
        seq = TimelineSequence(timeline_id="s", fps=29.97)
        assert seq.to_dict()["fps"] == "2997/100"

    def test_negative_fps_rejected(self) -> None:
        with pytest.raises(ValueError):
            TimelineSequence(timeline_id="s", fps=-30)

    def test_rational_fps_untouched(self) -> None:
        seq = TimelineSequence(
            timeline_id="s", fps=Rational(24000, 1001),
        )
        assert seq.fps == Rational(24000, 1001)


# ================================================================
# AUD-19102 / AUD-19103: automation 反序列化异常收窄 + 告警
# ================================================================


class TestAutomationDeserialization:
    def test_track_corrupt_automation_dropped_with_warning(self, caplog) -> None:
        good = {"param_name": "pan", "keyframes": [
            {"frame": 0, "value": 0.5},
        ]}
        data = {
            "track_id": "t1", "track_type": "video",
            "automation": {"volume": "junk-not-a-dict", "pan": good},
        }
        with caplog.at_level("WARNING"):
            track = EditTrack.from_dict(data)
        assert "pan" in track.automation
        assert "volume" not in track.automation
        assert any("automation" in message for message in caplog.messages)

    def test_track_corrupt_keyframes_dropped_with_warning(self, caplog) -> None:
        # keyframes 非可迭代（int）：from_dict 抛 TypeError → 整轨丢弃 + 告警
        # （单个坏关键帧由 KeyframeTrack.from_dict 内部逐帧容错，不属本路径）。
        data = {
            "track_id": "t1", "track_type": "video",
            "automation": {"volume": {"keyframes": 42}},
        }
        with caplog.at_level("WARNING"):
            track = EditTrack.from_dict(data)
        assert track.automation == {}
        assert any("automation" in message for message in caplog.messages)

    def test_sequence_corrupt_master_automation_dropped_with_warning(
        self, caplog,
    ) -> None:
        data = {
            "timeline_id": "s", "fps": "24/1",
            "master_automation": {"volume": None},
        }
        with caplog.at_level("WARNING"):
            seq = TimelineSequence.from_dict(data)
        assert seq.master_automation == {}
        assert any("master_automation" in m for m in caplog.messages)

    def test_sequence_good_master_automation_loads(self) -> None:
        data = {
            "timeline_id": "s", "fps": "24/1",
            "master_automation": {"volume": {
                "param_name": "volume",
                "keyframes": [{"frame": 0, "value": 0.8}],
            }},
        }
        seq = TimelineSequence.from_dict(data)
        assert "volume" in seq.master_automation


# ================================================================
# AUD-19104: frozen 模型不内嵌可变共享子对象
# ================================================================


class TestImmutableSnapshots:
    def test_track_effect_instance_params_copied(self) -> None:
        shared: dict = {"gain": 1.0}
        fx = TrackEffectInstance(effect_type="limiter", params=shared)
        shared["gain"] = 9.0
        assert fx.params == {"gain": 1.0}
        assert fx.params is not shared

    def test_transition_instance_params_copied(self) -> None:
        shared: dict = {"angle": 30.0}
        trans = TransitionInstance(
            track_id="t1", left_clip_id="a", right_clip_id="b",
            duration=10, params=shared,
        )
        shared["angle"] = 90.0
        assert trans.params == {"angle": 30.0}
        assert trans.params is not shared

    def test_track_rebuild_clones_automation(self) -> None:
        automation = {"volume": KeyframeTrack(
            param_name="volume", keyframes=[Keyframe(frame=0, value=1.0)])}
        track = EditTrack(track_id="t1", track_type="audio", automation=automation)
        derived = track._rebuild()
        assert derived.automation is not track.automation
        assert (derived.automation["volume"] is not track.automation["volume"])
        # 原位修改旧快照不穿透派生快照
        track.automation["volume"].add_keyframe(Keyframe(frame=50, value=0.2))
        assert len(derived.automation["volume"].keyframes) == 1

    def test_clone_automation_preserves_extrapolation(self) -> None:
        track = EditTrack(
            track_id="t1", track_type="audio",
            automation={"volume": KeyframeTrack(
                param_name="volume", keyframes=[Keyframe(frame=0, value=1.0)],
                pre_extrap="linear", post_extrap="linear",
            )},
        )
        cloned = track._clone_automation()
        assert cloned["volume"].pre_extrap == "linear"
        assert cloned["volume"].post_extrap == "linear"

    def test_sequence_rebuild_clones_master_automation(self) -> None:
        master = {"volume": KeyframeTrack(
            param_name="volume", keyframes=[Keyframe(frame=0, value=1.0)])}
        seq = TimelineSequence(
            timeline_id="s", master_automation=master,
        )
        derived = seq._rebuild()
        assert derived.master_automation is not seq.master_automation
        assert (derived.master_automation["volume"]
                is not seq.master_automation["volume"])

    def test_with_master_automation_deep_copies_input(self) -> None:
        external = {"volume": KeyframeTrack(
            param_name="volume", keyframes=[Keyframe(frame=0, value=1.0)])}
        seq = TimelineSequence(timeline_id="s").with_master_automation(external)
        external["volume"].add_keyframe(Keyframe(frame=10, value=0.5))
        assert len(seq.master_automation["volume"].keyframes) == 1


# ================================================================
# AUD-19008: 缩略图缓存 LRU
# ================================================================


class TestThumbnailCacheLRU:
    def test_cache_bounded_and_lru_eviction(self) -> None:
        sched = TimelineThumbnailScheduler()
        limit = THUMBNAIL_CACHE_MAX_ENTRIES
        for i in range(limit + 10):
            sched.cache_thumbnail("asset", i, 64, f"px-{i}")
        assert len(sched._completed_cache) == limit
        # 最旧的 10 条被逐出，最新的仍在
        assert sched.get_cached_thumbnail("asset", 0, 64) is None
        assert sched.get_cached_thumbnail("asset", limit + 9, 64) == f"px-{limit + 9}"

    def test_get_touches_lru_order(self) -> None:
        sched = TimelineThumbnailScheduler()
        sched.cache_thumbnail("a", 0, 64, "first")
        sched.cache_thumbnail("a", 1, 64, "second")
        # 触摸 first → second 变为最旧
        assert sched.get_cached_thumbnail("a", 0, 64) == "first"
        sched._completed_cache.popitem(last=False)
        assert sched.get_cached_thumbnail("a", 1, 64) is None
        assert sched.get_cached_thumbnail("a", 0, 64) == "first"

    def test_clear_cache_empties(self) -> None:
        sched = TimelineThumbnailScheduler()
        sched.cache_thumbnail("a", 0, 64, "first")
        sched.clear_cache()
        assert sched.get_cached_thumbnail("a", 0, 64) is None


# ================================================================
# AUD-19202: dip / composite 判定继承用户转场 base
# ================================================================


class TestUserTransitionClassification:
    def test_resolve_base_type(self) -> None:
        register_user_transition("user_mydip", "dip_to_color")
        register_user_transition("user_mywipe", "wipe")
        assert resolve_transition_base_type("user_mydip") == "dip_to_color"
        assert resolve_transition_base_type("user_mywipe") == "wipe"
        assert resolve_transition_base_type("wipe") == "wipe"
        assert resolve_transition_base_type("user_unregistered") == "user_unregistered"

    def test_is_dip_transition(self) -> None:
        register_user_transition("user_mydip", "dip_to_color")
        register_user_transition("user_mywipe", "wipe")
        assert is_dip_transition("dip_to_color") is True
        assert is_dip_transition("user_mydip") is True
        assert is_dip_transition("user_mywipe") is False
        assert is_dip_transition("cross_dissolve") is False

    def test_is_composite_transition_user_spatial_inherited(self) -> None:
        register_user_transition("user_mywipe", "wipe")
        register_user_transition("user_mydip", "dip_to_color")
        # 用户 spatial base 进入双源合成；dip 类经颜色 pass（不进 mask 循环）
        assert is_composite_transition("user_mywipe") is True
        assert is_composite_transition("wipe") is True
        assert is_composite_transition("blur_dissolve") is True
        assert is_composite_transition("user_mydip") is False
        assert is_composite_transition("dip_to_color") is False
        assert is_composite_transition("cross_dissolve") is False

    def test_is_full_frame_transition_user_dip_inherited(self) -> None:
        register_user_transition("user_mydip", "dip_to_color")
        assert is_full_frame_transition("user_mydip") is True
        assert is_full_frame_transition("dip_to_color") is True
        assert is_full_frame_transition("blur_dissolve") is True
        assert is_full_frame_transition("wipe") is False

    def test_preview_dip_color_resolution(self) -> None:
        """预览 CPU 路径的用户 dip 颜色解析（含 #RRGGBB / dict / list）。"""
        from src.features.edit.core.timeline_preview_renderer import (
            PreviewRenderWorker,
        )
        from src.features.edit.core.timeline_render_plan import TransitionBlendInfo

        register_user_transition("user_mydip", "dip_to_color")
        trans = TransitionInstance(
            track_id="t1", left_clip_id="a", right_clip_id="b",
            duration=10, transition_type="user_mydip",
        )
        info = TransitionBlendInfo(
            transition=trans, left_clip=None, right_clip=None,
            blend_factor=0.5, left_source_frame=0, right_source_frame=0,
        )

        info.transition.params["color"] = "#ff0000"
        assert PreviewRenderWorker._resolve_user_dip_color(info) == (1.0, 0.0, 0.0)
        info.transition.params["color"] = {"r": 0.2, "g": 0.4, "b": 0.6}
        assert PreviewRenderWorker._resolve_user_dip_color(info) == (0.2, 0.4, 0.6)
        info.transition.params["color"] = [0.1, 0.2, 0.3]
        assert PreviewRenderWorker._resolve_user_dip_color(info) == (0.1, 0.2, 0.3)
        info.transition.params.pop("color")
        assert PreviewRenderWorker._resolve_user_dip_color(info) == (0.0, 0.0, 0.0)


# ================================================================
# AUD-19207: 无几何 mask 类型回退常数溶解
# ================================================================


class TestMaskFallback:
    def test_cross_dissolve_constant_mask(self) -> None:
        mask = compute_transition_mask(64, 48, 0.5, "cross_dissolve")
        assert float(mask.min()) == pytest.approx(0.5)
        assert float(mask.max()) == pytest.approx(0.5)

    def test_dip_to_color_constant_mask(self) -> None:
        mask = compute_transition_mask(64, 48, 0.3, "dip_to_color")
        assert float(mask.min()) == pytest.approx(0.3)
        assert float(mask.max()) == pytest.approx(0.3)

    def test_unknown_type_constant_mask_not_wipe(self) -> None:
        mask = compute_transition_mask(64, 48, 0.5, "totally_unknown")
        # 常数溶解，而非"从左擦入"的 wipe（旧实现回退 compute_wipe_mask，
        # 首列 1.0 / 末列 0.0）
        assert float(mask.min()) == pytest.approx(0.5)
        assert float(mask.max()) == pytest.approx(0.5)

    def test_user_dip_resolves_to_constant_mask(self) -> None:
        register_user_transition("user_mydip", "dip_to_color")
        mask = compute_transition_mask(
            64, 48, 0.5, "user_mydip", {"color": "#ffffff"},
        )
        assert float(mask.min()) == pytest.approx(0.5)
        assert float(mask.max()) == pytest.approx(0.5)

    def test_wipe_still_geometric(self) -> None:
        mask = compute_transition_mask(
            64, 48, 0.5, "wipe", {"direction": "left"},
        )
        assert mask[0, 0] > 0.9
        assert mask[0, -1] < 0.1


# ================================================================
# AUD-19210: get_wipe_params 坏值防护
# ================================================================


class TestWipeParamGuards:
    def test_bad_angle_string_falls_back(self) -> None:
        wp = get_wipe_params({"angle": "45deg"})
        assert wp["angle"] == 0.0

    def test_none_softness_falls_back(self) -> None:
        wp = get_wipe_params({"softness": None})
        assert wp["softness"] == 0.02

    def test_nan_inf_fall_back(self) -> None:
        assert get_wipe_params({"angle": float("nan")})["angle"] == 0.0
        assert get_wipe_params({"softness": float("inf")})["softness"] == 0.02

    def test_numeric_strings_still_parse(self) -> None:
        assert get_wipe_params({"angle": "90"})["angle"] == 90.0

    def test_compute_wipe_mask_with_bad_params_does_not_raise(self) -> None:
        mask = compute_wipe_mask(
            32, 32, 0.5, {"angle": "bad", "softness": None, "direction": "angle"},
        )
        assert mask.shape == (32, 32)


# ================================================================
# AUD-18901 / AUD-18902: 预览渲染器（源级回归钉 + 行为测试）
# ================================================================


class TestPreviewRendererFixes:
    def _renderer_class(self) -> Any:
        from src.features.edit.core.timeline_preview_renderer import (
            PreviewRenderWorker,
        )
        return PreviewRenderWorker

    def test_solid_branch_sc_nesting(self) -> None:
        """AUD-18901：`if sc is not None` 必须嵌套在 `if solid_asset is not
        None` 块内（缩进缺陷回归钉——外层平级会在素材缺失时 NameError /
        沿用上一层 solid 的旧颜色）。"""
        cls = self._renderer_class()
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
            if name.startswith("__"):
                continue
            try:
                source = inspect.getsource(member)
            except (OSError, TypeError):
                continue
            if "solid_asset is not None" not in source:
                continue
            lines = source.splitlines()
            asset_indent = sc_assign_indent = sc_check_indent = None
            for i, line in enumerate(lines):
                if "if solid_asset is not None" in line:
                    asset_indent = len(line) - len(line.lstrip())
                elif "sc = getattr(solid_asset" in line:
                    sc_assign_indent = len(line) - len(line.lstrip())
                elif "if sc is not None" in line:
                    sc_check_indent = len(line) - len(line.lstrip())
            assert asset_indent is not None, "solid 分支缺失"
            assert sc_assign_indent is not None
            assert sc_check_indent is not None
            assert sc_assign_indent > asset_indent
            assert sc_check_indent > asset_indent
            return
        pytest.fail("未找到 solid 分支所在方法")

    def test_stop_waits_for_executor_and_defers_teardown(self) -> None:
        """AUD-18902 源级回归钉：线程池 shutdown 必须等待在飞 future；
        超时路径不得立即销毁共享解码资源（经 finished 延迟销毁）。"""
        cls = self._renderer_class()
        source = inspect.getsource(cls.stop)
        assert "shutdown(wait=True, cancel_futures=True)" in source
        assert "shutdown(wait=False" not in source
        assert "finished.connect(self._teardown_decode_resources)" in source

    def test_teardown_decode_resources_idempotent(self) -> None:
        cls = self._renderer_class()
        calls: list[str] = []
        stub = SimpleNamespace(
            _decode_resources_torn_down=False,
            _gpu_decode_manager=None,
            _clear_direct_preview_sources=lambda: calls.append("clear"),
            _comp_frame_renderer=None,
            # AUD-18909 统一销毁钩子：_teardown_decode_resources 末尾调用
            _teardown_comp_gl_resources=lambda: None,
        )
        cls._teardown_decode_resources(stub)
        cls._teardown_decode_resources(stub)
        assert calls == ["clear"]  # 第二次调用为 no-op

    def test_limited_range_yuv420p_black_white_levels(self) -> None:
        """AUD-18903：limited-range 黑位 16 → 输出近 0，白位 235 → 近 255。"""
        cls = self._renderer_class()
        stub = SimpleNamespace(
            _plane_to_uint8=staticmethod(lambda plane, bd: plane),
        )

        def convert(y_value: int, pixel_format: str) -> np.ndarray:
            y = np.full((8, 8), y_value, dtype=np.uint8)
            u = np.full((4, 4), 128, dtype=np.uint8)
            v = np.full((4, 4), 128, dtype=np.uint8)
            frame = SimpleNamespace(
                planes=[y, u, v], pixel_format=pixel_format, bit_depth=8,
            )
            out = cls._convert_planar_to_bgr(stub, frame)
            assert out is not None
            return out

        limited_black = convert(16, "yuv420p")
        limited_white = convert(235, "yuv420p")
        assert int(limited_black[4, 4, 0]) <= 8    # 旧实现 ≈16（黑不黑）
        assert int(limited_white[4, 4, 0]) >= 245  # 旧实现 ≈235（白不白）

        # full-range（yuvj420p）不经拉伸：16 仍显示为 ~16
        full_black = convert(16, "yuvj420p")
        assert 8 <= int(full_black[4, 4, 0]) <= 40

    def test_limited_range_still_interpolates_chroma(self) -> None:
        """拉伸后的色度仍参与转换（蓝色帧不是灰帧）。"""
        cls = self._renderer_class()
        stub = SimpleNamespace(
            _plane_to_uint8=staticmethod(lambda plane, bd: plane),
        )
        y = np.full((8, 8), 128, dtype=np.uint8)
        u = np.full((4, 4), 240, dtype=np.uint8)
        v = np.full((4, 4), 110, dtype=np.uint8)
        frame = SimpleNamespace(
            planes=[y, u, v], pixel_format="yuv420p", bit_depth=8,
        )
        out = cls._convert_planar_to_bgr(stub, frame)
        assert out is not None
        b, g, r = int(out[4, 4, 0]), int(out[4, 4, 1]), int(out[4, 4, 2])
        assert b > r + 20  # U 高 V 低 → 偏蓝
