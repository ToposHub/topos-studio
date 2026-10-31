"""AUD-18800 回归测试：活路径时间线操作不得丢弃 ClipInstance 扩展字段。

背景（docs/audit/2026-09-28/batches/batch-188-features-edit-core.md）：
slip/slide/roll/ripple trim（左/右）/overwrite 曾用手工构造 ClipInstance
重建实例，未传入的扩展字段（调色联动、关键帧动画、淡入淡出、混音增益、
冻结帧、混合模式、RAW 参数、FPS 元数据等十余个）全部回落默认值，
属静默用户数据丢失。修复（w6-g8）统一改用不可变更新 helper
``timeline_ops.copy_clip_with``（replace 语义 + 动态属性回拷）。

验收点：
1. 六个操作后扩展字段逐一保留（含 slide 的前/后邻居、overwrite 的
   半片/残片）；
2. "未知新字段不丢"——动态附加到实例上的非字段属性在操作后存活
   （模拟未来给 ClipInstance 新增字段前的过渡期载荷）；
3. 缩短 duration 时淡入/淡出按模型边界钳制，操作不因保留字段而失败。

纯函数测试：无 PyQt/OpenGL 依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import ClipBlendMode, RawParams
from src.features.edit.core.effect_instance import AnimatableParam
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.timeline_ops import (
    copy_clip_with,
    overwrite_clip_at_playhead,
    ripple_trim_clip_left,
    ripple_trim_clip_right,
    roll_trim_clips,
    slide_clip,
    slip_clip,
)
from src.shared.media.metadata import Rational


# ================================================================
# Fixtures
# ================================================================

#: AUD-18800 点名的全部扩展字段（审查报告"未传入"清单 + 邻居共同字段）。
EXTENSION_FIELDS: tuple[str, ...] = (
    "label_color",
    "waveform_show_channels",
    "fade_in_duration",
    "fade_out_duration",
    "color_stack_id",
    "color_shared",
    "composite_id",
    "composite_bypassed",
    "render_cache_id",
    "freeze_source_frame",
    "blend_mode",
    "audio_gain_db",
    "raw",
    "animated_params",
    "linked_clip_id",
    "opacity",
    "is_enabled",
    "_source_fps_num",
    "_source_fps_den",
    "_timeline_fps_num",
    "_timeline_fps_den",
    "_source_pts_index",
)


def make_rich_clip(**overrides: Any) -> ClipInstance:
    """构造带全部扩展字段的"富"片段（AUD-18800 数据丢失的靶子）。"""
    params: dict[str, Any] = {
        "asset_id": "asset1",
        "track_id": "track1",
        "speed": 1.0,
        "linked_clip_id": "linked-anchor",
        "label_color": "#ff8800",
        "waveform_show_channels": True,
        "fade_in_duration": 10,
        "fade_out_duration": 12,
        "color_stack_id": "color-stack-9",
        "color_shared": True,
        "composite_id": "composite-7",
        "composite_bypassed": True,
        "render_cache_id": "render-cache-3",
        "freeze_source_frame": 42,
        "blend_mode": ClipBlendMode.MULTIPLY,
        "audio_gain_db": -6.0,
        "raw": RawParams(wb_gain_r=1.5, exposure_stops=0.5),
        "animated_params": {"opacity": AnimatableParam(base_value=0.5)},
        "_source_fps_num": 24000,
        "_source_fps_den": 1001,
        "_timeline_fps_num": 25,
        "_timeline_fps_den": 1,
        "_source_pts_index": (0.0, 1.0, 2.5, 4.0),
    }
    params.update(overrides)
    return ClipInstance(**params)


def make_sequence(clips: tuple[ClipInstance, ...]) -> TimelineSequence:
    """按 clips 构造单视频轨序列（所有 clip 的 track_id 须为 track1）。"""
    track = EditTrack(
        track_id="track1",
        name="Video 1",
        track_type="video",
        index=0,
        clips=clips,
    )
    return TimelineSequence(
        timeline_id="seq1",
        name="AUD-18800",
        fps=Rational(25, 1),
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(track,),
    )


def assert_extensions_preserved(
    original: ClipInstance, updated: ClipInstance,
    skip: tuple[str, ...] = (),
) -> None:
    """断言 updated 逐字段保留了 original 的扩展字段（skip 除外）。"""
    lost = [
        name for name in EXTENSION_FIELDS
        if name not in skip and getattr(updated, name) != getattr(original, name)
    ]
    assert not lost, (
        f"AUD-18800 复现：操作后扩展字段丢失/被改写: {lost}；"
        f"updated={updated!r}"
    )
    # 动态附加属性（"未知新字段"）必须存活。
    probe = getattr(original, "_w6g8_probe", None)
    if probe is not None:
        assert getattr(updated, "_w6g8_probe", None) == probe, (
            "AUD-18800 复现：动态附加属性（未知新字段载荷）在操作后丢失"
        )


def attach_probe(clip: ClipInstance, tag: str) -> ClipInstance:
    """动态附加非字段属性，模拟"未来新增字段/外部挂载载荷"。"""
    object.__setattr__(clip, "_w6g8_probe", {"tag": tag})
    return clip


# ================================================================
# helper 单元语义
# ================================================================

class TestCopyClipWith:
    def test_only_requested_field_changes(self) -> None:
        clip = make_rich_clip(
            clip_id="c1", timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        )
        new = copy_clip_with(clip, source_start=33)
        assert new.source_start == 33
        assert new is not clip
        assert_extensions_preserved(clip, new)

    def test_unknown_dynamic_attribute_survives(self) -> None:
        clip = attach_probe(
            make_rich_clip(
                clip_id="c1", timeline_start=0, timeline_duration=100,
                source_start=0, source_duration=100,
            ),
            "helper",
        )
        new = copy_clip_with(clip, timeline_start=7)
        assert new._w6g8_probe == {"tag": "helper"}  # type: ignore[attr-defined]

    def test_fades_clamped_when_duration_shrinks(self) -> None:
        clip = make_rich_clip(
            clip_id="c1", timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
            fade_in_duration=50, fade_out_duration=60,
        )
        new = copy_clip_with(clip, timeline_duration=30, source_duration=30)
        # min(50,30)=30；min(60,30)=30 后 __post_init__ 归一化 sum>duration
        # → fade_out = 30 - 30 = 0（模型既有边界语义，不抛错）。
        assert new.timeline_duration == 30
        assert new.fade_in_duration == 30
        assert new.fade_out_duration == 0


# ================================================================
# 六个活路径操作的字段保留回归
# ================================================================

class TestSlipPreservesExtensions:
    def test_slip_keeps_all_extension_fields(self) -> None:
        clip = attach_probe(
            make_rich_clip(
                clip_id="target", timeline_start=100, timeline_duration=100,
                source_start=50, source_duration=100,
            ),
            "slip",
        )
        seq = make_sequence((clip,))
        result = slip_clip(seq, "target", 10, max_source_frames=1000)
        updated = result.get_clip_by_id("target")
        assert updated is not None
        assert updated.source_start == 60
        assert updated.timeline_start == 100
        assert updated.timeline_duration == 100
        assert_extensions_preserved(clip, updated)


class TestSlidePreservesExtensions:
    def test_slide_keeps_fields_on_all_three_clips(self) -> None:
        a = attach_probe(make_rich_clip(
            clip_id="a", timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        ), "slide-prev")
        b = attach_probe(make_rich_clip(
            clip_id="b", timeline_start=100, timeline_duration=100,
            source_start=0, source_duration=100,
        ), "slide-target")
        c = attach_probe(make_rich_clip(
            clip_id="c", timeline_start=200, timeline_duration=100,
            source_start=0, source_duration=100,
        ), "slide-next")
        seq = make_sequence((a, b, c))
        result = slide_clip(seq, "b", 30)

        got_a = result.get_clip_by_id("a")
        got_b = result.get_clip_by_id("b")
        got_c = result.get_clip_by_id("c")
        assert got_a is not None and got_b is not None and got_c is not None
        # 几何（Wave3 AUD-18801 原子重建的最终态）不被本修复破坏。
        assert (got_b.timeline_start, got_b.timeline_end) == (130, 230)
        assert got_a.timeline_end == 130
        assert got_c.timeline_start == 230
        # AUD-18800：被 slide 片段与被波及的前/后邻居都保留扩展字段。
        assert_extensions_preserved(b, got_b)
        assert_extensions_preserved(a, got_a, skip=("fade_out_duration",))
        assert_extensions_preserved(c, got_c,
                                    skip=("fade_in_duration", "fade_out_duration"))
        # 邻居时长变化后的淡入出边界（A 延长→fade_out 仍 12；C 缩短 70→钳制内）。
        assert got_a.fade_out_duration == 12
        assert got_c.fade_in_duration <= 70 and got_c.fade_out_duration <= 70


class TestRollPreservesExtensions:
    def test_roll_keeps_fields_on_both_clips(self) -> None:
        left = attach_probe(make_rich_clip(
            clip_id="left", timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        ), "roll-left")
        right = attach_probe(make_rich_clip(
            clip_id="right", timeline_start=100, timeline_duration=100,
            source_start=0, source_duration=100,
        ), "roll-right")
        seq = make_sequence((left, right))
        result = roll_trim_clips(seq, "left", "right", 10)

        got_left = result.get_clip_by_id("left")
        got_right = result.get_clip_by_id("right")
        assert got_left is not None and got_right is not None
        assert got_left.timeline_duration == 110
        assert got_right.timeline_start == 110
        assert got_right.timeline_duration == 90
        assert_extensions_preserved(left, got_left)
        assert_extensions_preserved(right, got_right)


class TestRipplePreservesExtensions:
    def test_ripple_left_keeps_fields(self) -> None:
        a = make_rich_clip(
            clip_id="a", timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        )
        b = attach_probe(make_rich_clip(
            clip_id="b", timeline_start=100, timeline_duration=100,
            source_start=0, source_duration=100,
        ), "ripple-left")
        seq = make_sequence((a, b))
        result = ripple_trim_clip_left(seq, "b", 120)

        got_b = result.get_clip_by_id("b")
        assert got_b is not None
        assert got_b.timeline_start == 100  # 缩短锚定原位（P1-04 语义）
        assert got_b.timeline_duration == 80
        assert_extensions_preserved(b, got_b)

    def test_ripple_right_keeps_fields(self) -> None:
        a = make_rich_clip(
            clip_id="a", timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        )
        b = attach_probe(make_rich_clip(
            clip_id="b", timeline_start=100, timeline_duration=100,
            source_start=0, source_duration=100,
        ), "ripple-right")
        seq = make_sequence((a, b))
        result = ripple_trim_clip_right(seq, "b", 180)

        got_b = result.get_clip_by_id("b")
        assert got_b is not None
        assert got_b.timeline_duration == 80
        # N01/C3-01：ripple 右缘与 trim 同一边缘映射（fps 感知）——
        # 23.976fps 源在 25fps 序列收缩 20 时间线帧 = round(19.18) 源帧，
        # 源窗 100→81（旧基速公式无 fps 时才等于 80）。
        assert got_b.source_duration == 81
        assert_extensions_preserved(b, got_b)


class TestOverwritePreservesExtensions:
    def test_middle_split_pieces_keep_fields(self) -> None:
        """新剪辑完全落在既有片段中间：切成左右两半（半片）。"""
        x = attach_probe(make_rich_clip(
            clip_id="x", timeline_start=0, timeline_duration=300,
            source_start=0, source_duration=300,
        ), "overwrite-split")
        seq = make_sequence((x,))
        result = overwrite_clip_at_playhead(
            seq, asset_id="asset2", in_frame=0, out_frame=99,
            playhead=100, track_id="track1",
        )

        track = result.get_track_by_id("track1")
        assert track is not None
        pieces = [c for c in track.clips if c.asset_id == "asset1"]
        assert len(pieces) == 2
        left_piece, right_piece = sorted(pieces, key=lambda c: c.timeline_start)
        assert (left_piece.timeline_start, left_piece.timeline_end) == (0, 100)
        assert (right_piece.timeline_start, right_piece.timeline_end) == (200, 300)
        # AUD-18800：半片继承全部扩展字段（fade_in 在右半段清零属 split 语义）。
        assert_extensions_preserved(x, left_piece)
        assert_extensions_preserved(x, right_piece, skip=(
            "linked_clip_id",  # 右半段新生片段不延续链接（原代码语义）
            "fade_in_duration",  # 右半段从头开始（split_clip 同语义）
        ))
        assert right_piece.linked_clip_id is None
        assert right_piece.fade_in_duration == 0
        assert right_piece._w6g8_probe == {"tag": "overwrite-split"}  # type: ignore[attr-defined]

    def test_left_cut_residual_keeps_fields_and_id(self) -> None:
        """新剪辑覆盖既有片段左半：残片保留 clip_id 与全部扩展字段。"""
        x = attach_probe(make_rich_clip(
            clip_id="x", timeline_start=100, timeline_duration=200,
            source_start=0, source_duration=200,
        ), "overwrite-left-cut")
        seq = make_sequence((x,))
        result = overwrite_clip_at_playhead(
            seq, asset_id="asset2", in_frame=0, out_frame=99,
            playhead=100, track_id="track1",
        )
        got = result.get_clip_by_id("x")
        assert got is not None
        assert got.timeline_start == 200
        assert got.timeline_duration == 100
        assert_extensions_preserved(x, got)

    def test_right_cut_residual_keeps_fields_and_id(self) -> None:
        """新剪辑覆盖既有片段右半：残片保留 clip_id 与全部扩展字段。"""
        x = attach_probe(make_rich_clip(
            clip_id="x", timeline_start=0, timeline_duration=200,
            source_start=0, source_duration=200,
        ), "overwrite-right-cut")
        seq = make_sequence((x,))
        result = overwrite_clip_at_playhead(
            seq, asset_id="asset2", in_frame=0, out_frame=99,
            playhead=100, track_id="track1",
        )
        got = result.get_clip_by_id("x")
        assert got is not None
        assert got.timeline_start == 0
        assert got.timeline_duration == 100
        assert_extensions_preserved(x, got)

    def test_residual_clamps_fades_to_shrunk_duration(self) -> None:
        """残片时长缩到淡入出以下：钳制而非丢字段/抛错。"""
        x = make_rich_clip(
            clip_id="x", timeline_start=0, timeline_duration=200,
            source_start=0, source_duration=200,
            fade_in_duration=80, fade_out_duration=20,
        )
        seq = make_sequence((x,))
        result = overwrite_clip_at_playhead(
            seq, asset_id="asset2", in_frame=0, out_frame=99,
            playhead=100, track_id="track1",
        )
        got = result.get_clip_by_id("x")
        assert got is not None
        assert got.timeline_duration == 100
        assert got.fade_in_duration == 80  # 未超新时长，原样保留
        assert got.fade_out_duration == 20
        assert got.color_stack_id == "color-stack-9"


# ================================================================
# 故障注入：证明上述断言对"丢字段"回归敏感
# ================================================================

class TestFaultInjection:
    def test_legacy_reconstruction_style_fails_the_guard(self) -> None:
        """模拟旧实现（只传基础字段的手工构造）必须被断言抓住。"""

        def legacy_copy(clip: ClipInstance, **updates: Any) -> ClipInstance:
            return ClipInstance(
                clip_id=updates.get("clip_id", clip.clip_id),
                asset_id=clip.asset_id,
                track_id=updates.get("track_id", clip.track_id),
                timeline_start=updates.get("timeline_start", clip.timeline_start),
                timeline_duration=updates.get("timeline_duration", clip.timeline_duration),
                source_start=updates.get("source_start", clip.source_start),
                source_duration=updates.get("source_duration", clip.source_duration),
                speed=clip.speed,
                linked_clip_id=updates.get("linked_clip_id", clip.linked_clip_id),
                transform=clip.transform,
                crop=clip.crop,
                opacity=clip.opacity,
                effects=clip.effects,
                is_enabled=clip.is_enabled,
            )

        clip = attach_probe(make_rich_clip(
            clip_id="target", timeline_start=100, timeline_duration=100,
            source_start=50, source_duration=100,
        ), "fault")
        seq = make_sequence((clip,))
        import src.features.edit.core.timeline_ops as ops_module
        original_helper = ops_module.copy_clip_with
        ops_module.copy_clip_with = legacy_copy  # type: ignore[assignment]
        try:
            result = slip_clip(seq, "target", 10, max_source_frames=1000)
            updated = result.get_clip_by_id("target")
            assert updated is not None
            # 旧实现丢字段 + 丢动态属性 → 必须被断言抓住。
            with pytest.raises(AssertionError, match="AUD-18800"):
                assert_extensions_preserved(clip, updated)
        finally:
            ops_module.copy_clip_with = original_helper  # type: ignore[assignment]
