"""KF-06：导出链路逐帧求值回归（窗口动画贯通 + 多节点完整消费）。

验收（计划 KF-06 行）：窗口动画在导出侧按 source_frame 求值（此前
恒为静态 base 形状）；两个动画节点同帧都被消费（无"只吃首个动画
节点"）；能力门（复杂图拒绝）不受影响。
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.features.color.core.grade_keyframes import (
    GradeKeyframe,
    GradeKeyframeTrack,
    evaluate_mask_tracks_payload,
    mask_track_name,
)
from src.shared.project.color_stack import ColorStackData

_SHAPE_ID = "kf06-shape-1"


def _window_stack(center_at_base=(0.5, 0.5)) -> ColorStackData:
    """单层：圆形窗口（base 中心）+ lift 提黑（窗口内可见）+ center 动画。

    用 lift 而非 exposure：纯黑源上乘性参数仍为黑，加性 lift 才能让
    "窗口内/外"在输出上可区分。
    """
    shape = {
        "id": _SHAPE_ID, "type": "circle", "name": None,
        "visible": True, "invert": False, "locked": False,
        "operation": "add", "feather": 0.0, "opacity": 1.0,
        "blur": 0.0, "clean_black": 0.0, "clean_white": 0.0,
        "center": list(center_at_base), "size": [0.25, 0.25],
        "rotation": 0.0, "softness": 0.0,
        "points": [], "grad_points": [[0.4, 0.5], [0.6, 0.5]],
        "shape_schema_version": 2,
    }
    track = GradeKeyframeTrack(mask_track_name(_SHAPE_ID, "center"), [
        GradeKeyframe(0, (0.25, 0.5)),
        GradeKeyframe(100, (0.75, 0.5)),
    ])
    return ColorStackData(
        stack_id="s1", asset_id="", name="w",
        grading_params={"lift": (0.2, 0.2, 0.2)},
        layers=[{"layer_index": 0, "name": "L",
                 "grading_params": {"lift": (0.2, 0.2, 0.2)},
                 "mask_shapes": [shape],
                 "grade_keyframes": {mask_track_name(_SHAPE_ID, "center"):
                                     track.to_dict()}}],
    )


# ---------------------------------------------------------------------------
# evaluate_mask_tracks_payload（dict 纯函数）
# ---------------------------------------------------------------------------


def test_payload_evaluation_patches_shape_per_frame():
    shapes = [{"id": _SHAPE_ID, "center": [0.5, 0.5], "size": [0.2, 0.2]}]
    tracks = {mask_track_name(_SHAPE_ID, "center"): GradeKeyframeTrack(
        mask_track_name(_SHAPE_ID, "center"), [
            GradeKeyframe(0, (0.0, 0.0)), GradeKeyframe(10, (1.0, 1.0))])}
    start = evaluate_mask_tracks_payload(tracks, shapes, 0)
    mid = evaluate_mask_tracks_payload(tracks, shapes, 5)
    end = evaluate_mask_tracks_payload(tracks, shapes, 10)
    assert start[0]["center"] == [0.0, 0.0]
    assert mid[0]["center"] == [pytest.approx(0.5), pytest.approx(0.5)]
    assert end[0]["center"] == [1.0, 1.0]
    assert shapes[0]["center"] == [0.5, 0.5]  # 入参不被原地修改


def test_payload_evaluation_accepts_serialized_tracks_and_skips_stale_ids():
    shapes = [{"id": "gone", "center": [0.5, 0.5]}]
    tracks = {mask_track_name(_SHAPE_ID, "center"): {
        "keyframes": [{"frame": 0, "value": [0.1, 0.1],
                       "interpolation": "linear"}]}}
    assert evaluate_mask_tracks_payload(tracks, shapes, 0) is None
    empty = evaluate_mask_tracks_payload({}, shapes, 0)
    assert empty is None


# ---------------------------------------------------------------------------
# 导出侧贯通：窗口动画逐帧生效（N03 matte 消费点）
# ---------------------------------------------------------------------------


def _exporter(stack):
    from src.features.edit.core.timeline_export import TimelineExporter
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = NS(
        color_stacks={"s1": stack}, color_groups={},
        audio_sync_bindings={})
    return exporter


def _graded_frame(exporter, source_frame):
    layer = NS(clip_id="c1", source_frame=source_frame,
               clip=NS(clip_id="c1", color_stack_id="s1",
                       color_trim_stack_id=None))
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    return exporter._apply_color_grade_unstabilized(frame, layer)


def test_window_animation_drives_export_matte_per_frame():
    exporter = _exporter(_window_stack())
    early = _graded_frame(exporter, 0)    # 窗口中心 x=0.25
    late = _graded_frame(exporter, 100)   # 窗口中心 x=0.75
    # lift 在窗口内提黑：两帧窗口位置不同 → 输出不同
    assert not np.array_equal(early, late)
    # 窗口外（角落）保持未调色黑
    assert early[2, 2].max() == 0 and late[2, 2].max() == 0
    # 各帧窗口内确有调色效果（逐帧形状跟随轨道）
    assert early[16, 8].max() > 0    # x=8/32≈0.25：early 帧窗口内
    assert late[16, 24].max() > 0    # x=24/32≈0.75：late 帧窗口内


def test_static_shape_still_works_without_tracks():
    stack = _window_stack()
    stack = ColorStackData(
        stack_id="s1", asset_id="", name="w",
        grading_params={"lift": (0.2, 0.2, 0.2)},
        layers=[{**stack.layers[0], "grade_keyframes": {}}],
    )
    exporter = _exporter(stack)
    frame = _graded_frame(exporter, 50)
    # 无动画：base 中心 0.5 生效，画面中央提亮、角落保持
    assert frame[16, 16].max() > 0
    assert frame[2, 2].max() == 0
