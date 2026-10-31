"""第三轮复查批C回归：N21–N24、N27（异步身份/Replace 预检/导出取消/
Nest extras/revision 保持）。

对应 topos_round3_features.md RF05–RF08 与功能报告 §3（R25 残余）。
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.edit_core


# ------------------------------------------------------------------ N21

def test_n21_probe_identity_mismatch_keeps_pending() -> None:
    """身份不匹配的旧探测零写入且不消耗新 pending；匹配才消费。"""
    from types import SimpleNamespace

    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    panel = SimpleNamespace()
    panel._proxy_session_generation = 3
    panel._pending_proxy_meta = {
        "/p.mov": ("/new/src.mov", 1.0, 10, "asset-9", 3),
    }
    applied: list = []
    panel._apply_proxy_ready = lambda *a: applied.append(a[0])

    MediaPoolPanel._on_proxy_resolution_probed(
        panel, "/p.mov", "/old/src.mov", (640, 360), "asset-9", 3)
    assert applied == [] and "/p.mov" in panel._pending_proxy_meta

    MediaPoolPanel._on_proxy_resolution_probed(
        panel, "/p.mov", "/new/src.mov", (111, 222), "asset-9", 2)
    assert applied == [] and "/p.mov" in panel._pending_proxy_meta

    MediaPoolPanel._on_proxy_resolution_probed(
        panel, "/p.mov", "/new/src.mov", (640, 360), "asset-9", 3)
    assert applied == ["/new/src.mov"]
    assert "/p.mov" not in panel._pending_proxy_meta


# ------------------------------------------------------------------ N22

def _project_with_video_asset(tmp_path):
    from src.features.edit.core.project import EditProject, MediaAsset

    new_path = os.path.join(str(tmp_path), "new.png")
    with open(new_path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
    asset = MediaAsset(
        asset_id="a1", display_name="old.mp4", path="/tmp/old.mp4",
        media_type="video",
        metadata={"fps": 24, "total_frames": 100, "multicam_group": "G"},
        color_metadata={"color_primaries": "bt2020"},
        thumbnail_key="t-old",
    )
    project = EditProject(project_id="p", name="P")
    project = project.__class__(**{
        **{f: getattr(project, f) for f in project.__dataclass_fields__},
        "assets": {"a1": asset}, "revision": 7,
    })
    return project, new_path


def test_n22_replace_migrates_metadata_and_invalidates_cache(tmp_path) -> None:
    from src.features.edit.core.commands import (
        ProjectCommandStack, ReplaceMediaCommand,
    )
    from src.features.edit.ui.media_pool_panel import _extract_color_metadata

    project, new_path = _project_with_video_asset(tmp_path)
    probed = {"width": 2, "height": 2, "format": "PNG",
              "color_transfer": "srgb"}
    merged = dict(project.assets["a1"].metadata)
    merged.update(probed)
    color = _extract_color_metadata(probed)

    cmd = ReplaceMediaCommand.create(
        "a1", new_path, new_metadata=merged, new_media_type="image",
        color_metadata=color)
    stack = ProjectCommandStack()
    p2 = stack.execute(cmd, project)
    replaced = p2.assets["a1"]
    assert replaced.media_type == "image"
    assert replaced.metadata["multicam_group"] == "G", "feature 字段保留"
    assert replaced.metadata["width"] == 2
    assert replaced.thumbnail_key is None, "旧源缩略图失效"
    assert replaced.color_metadata == {"color_transfer": "srgb"}

    p3 = stack.undo(p2)
    undone = p3.assets["a1"]
    assert undone.media_type == "video"
    assert undone.thumbnail_key == "t-old"
    assert undone.color_metadata == {"color_primaries": "bt2020"}

    loaded = ReplaceMediaCommand.from_dict(json.loads(json.dumps(cmd.to_dict())))
    assert loaded.new_media_type == "image", "JSON 回放不得退回 video"


# ------------------------------------------------------------------ N23

def test_n23_export_loudness_cancel_propagates() -> None:
    """N23/RF07：导出响度阶段的取消接线。

    两级验证：
    1. S 级（源码契约）：_render_audio_master 的两次 analyze_loudness
       都传 progress 取消谓词，且 LoudnessAnalysisCancelled 显式重抛
       （不被宽泛 except 吞成"分析失败继续交付"）、normalize 后有
       二次取消核验；
    2. M 级：谓词返回 False 时 analyzer 立即抛 LoudnessAnalysisCancelled。
    """
    import inspect

    import numpy as np

    from src.features.edit.core.loudness_analyzer import (
        LoudnessAnalysisCancelled, analyze_loudness,
    )
    from src.features.edit.core.timeline_export import TimelineExporter

    source = inspect.getsource(TimelineExporter._render_audio_master)
    loudness_block = source[source.index("loudness_target_lufs"):]
    assert loudness_block.count("progress=_loudness_alive") == 2, (
        "两次响度扫描都必须传取消谓词")
    assert "except LoudnessAnalysisCancelled" in loudness_block
    assert "raise" in loudness_block[loudness_block.index(
        "except LoudnessAnalysisCancelled"):]
    assert "if self._cancel_requested" in loudness_block

    cancelled = False
    try:
        analyze_loudness(
            np.zeros((2, 48000), dtype=np.float32), 48000.0,
            progress=lambda _p: False)
    except LoudnessAnalysisCancelled:
        cancelled = True
    assert cancelled, "谓词返回 False 必须立即取消"


# ------------------------------------------------------------------ N24

def test_n24_nested_child_track_preserves_extras() -> None:
    from src.features.edit.core.nested_sequence import build_nested_from_selection
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

    track = EditTrack.from_dict({
        "track_id": "v1", "name": "V1", "track_type": "video", "index": 0,
        "clips": [{
            "clip_id": "c", "asset_id": "a", "track_id": "v1",
            "timeline_start": 0, "timeline_duration": 50,
            "source_start": 0, "source_duration": 50}],
        "future_track": {"x": 1},
    })
    tl = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    tl = tl.with_track_added(track)
    result = build_nested_from_selection(tl, ["c"], "Nest", "child1")
    child_track = result.child_timeline.video_tracks[0]
    assert child_track._extra_fields == {"future_track": {"x": 1}}


# ------------------------------------------------------------------ N27

def test_n27_navigation_and_create_preserve_revision() -> None:
    from src.features.edit.core.project import (
        EditProject, TimelineSequenceRef,
    )

    project = EditProject(project_id="p", name="P")
    project = project.__class__(**{
        **{f: getattr(project, f) for f in project.__dataclass_fields__},
        "timelines": {"t1": TimelineSequenceRef(
            timeline_id="t1", name="T", fps=24.0)},
        "revision": 7,
    })
    switched = project.set_active_timeline("t1")
    assert switched.revision == 7, "切换时间线不得把 revision 归 0"
    created = switched.create_timeline("New")
    assert created.revision == 7, "纯 helper 创建不重置 revision"
    assert created.active_timeline_id != "t1"
