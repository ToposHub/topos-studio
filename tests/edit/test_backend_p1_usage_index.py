"""P1-5 媒体使用次数 / Usage / 时间线定位——索引扩展验收。

对应《媒体池后端交接计划》P1 第 5 项：
- 嵌套序列展开计入（容器引用 + 子序列内部真实引用）；
- 同路径多实例（子片段/复制素材）分组：使用检查按整组算；
- 删除前使用检查与点击定位引用同一索引（usage_check_for_assets
  与 locations_for 同源）；
- Comp Clip 图内引用属 CompositeGraph 子系统，不伪造遍历。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.asset_source_range import create_manual_subclip
from src.features.edit.core.asset_usage_index import (
    build_asset_usage_index,
    same_path_asset_groups,
    usage_check_for_assets,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _asset(asset_id: str, media_type: str = "video",
           path: str | None = None, metadata: dict | None = None) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=path or f"/m/{asset_id}.mov",
        media_type=media_type, display_name=asset_id,
        metadata=metadata or {"fps": 25.0, "total_frames": 100},
        created_at=1.0,
    )


def _sequence(seq_id: str, clips: list[tuple[str, str, int, int]],
              name: str = "") -> TimelineSequence:
    """clips: (track_id, asset_id, start, duration)。"""
    tracks: dict[str, list[ClipInstance]] = {}
    for index, (track_id, asset_id, start, duration) in enumerate(clips):
        tracks.setdefault(track_id, []).append(ClipInstance(
            f"{seq_id}-c{index}", asset_id, track_id, start, duration,
            0, duration,
            _source_fps_num=25, _source_fps_den=1,
            _timeline_fps_num=25, _timeline_fps_den=1))
    video = [EditTrack(tid, f"V{n+1}", "video", n,
                       clips=tuple(clips))
             for n, (tid, clips) in enumerate(tracks.items())]
    return TimelineSequence(
        timeline_id=seq_id, name=name or seq_id, fps=Rational(25, 1),
        video_tracks=tuple(video))


def _project_with_assets(*assets: MediaAsset) -> EditProject:
    from dataclasses import replace as _dc_replace
    project = EditProject.create("P1-5")
    return _dc_replace(project, assets={
        **project.assets, **{a.asset_id: a for a in assets}})


def test_nested_sequence_inner_assets_counted_via_expansion() -> None:
    """嵌套展开：父序列只引用嵌套素材，但内部素材的真实使用被计入。"""
    inner_asset = _asset("inner-clip")
    nested_asset = _asset(
        "nested-1", media_type="nested_sequence",
        path="topos://sequence/child",
        metadata={"timeline_id": "child"})
    inner = _sequence("child", [("v1", "inner-clip", 0, 50)])
    parent = _sequence("parent", [("v1", "nested-1", 0, 50)])

    project = _project_with_assets(inner_asset, nested_asset)
    index = build_asset_usage_index([parent, inner], project)
    # 容器引用 + 内部真实引用都算使用
    assert index.usage_count("nested-1") == 1
    assert index.usage_count("inner-clip") == 1
    loc = index.locations_for("inner-clip")[0]
    assert loc.timeline_id == "child" and loc.clip_id == "child-c0"
    # 关闭展开 = 只看直接引用（导出单序列口径）
    plain = build_asset_usage_index([parent, inner], project,
                                    expand_nested=False)
    assert plain.usage_count("inner-clip") == 1  # 子序列也在传入集合


def test_nested_expansion_no_double_count_and_cycle_guard() -> None:
    """子序列同时单独传入时不双记；展开链成环时拒绝展开不死循环。"""
    nested_a = _asset("nA", media_type="nested_sequence",
                      path="topos://sequence/seqA",
                      metadata={"timeline_id": "seqA"})
    nested_b = _asset("nB", media_type="nested_sequence",
                      path="topos://sequence/seqB",
                      metadata={"timeline_id": "seqB"})
    # seqA 引用 nB；seqB 引用 nA（人为构造环，DFS 防护兜底）
    seq_a = _sequence("seqA", [("v1", "nB", 0, 10)])
    seq_b = _sequence("seqB", [("v1", "nA", 0, 10)])
    top = _sequence("top", [("v1", "nA", 0, 10)])
    project = _project_with_assets(nested_a, nested_b)
    index = build_asset_usage_index([top, seq_a, seq_b], project)
    # 每个引用恰好计一次（去重 + 防环）
    assert index.usage_count("nA") == 2  # top 引用 + seqB 引用
    assert index.usage_count("nB") == 1


def test_same_path_groups_and_usage_check_cover_subclips() -> None:
    """同路径整组：删除检查把子片段伙伴的使用一起列出。"""
    project = EditProject.create("P1-5 sub")
    project = project.import_asset(
        "/tmp/shared.mov", "video",
        {"fps": 25.0, "total_frames": 100, "duration_seconds": 4.0})
    parent_id = project.get_asset_by_path("/tmp/shared.mov").asset_id
    project, sub = create_manual_subclip(project, parent_id, 0, 24,
                                         display_name="头")

    groups = same_path_asset_groups(project)
    assert groups.get("/tmp/shared.mov") == tuple(sorted((parent_id, sub.asset_id)))

    # 子片段被时间线引用；父素材删除检查必须看到同一文件仍在使用
    timeline = _sequence("t1", [("v1", sub.asset_id, 0, 25)])
    report = usage_check_for_assets(project, [timeline], [parent_id])
    assert sub.asset_id in report
    assert len(report[sub.asset_id]) == 1
    # 与定位索引同源：locations_for 看到同一 clip
    index = build_asset_usage_index([timeline], project)
    assert (index.locations_for(sub.asset_id)[0].clip_id
            == report[sub.asset_id][0].clip_id)
    # 未使用素材不出现在报告里
    assert parent_id not in report


def test_usage_check_and_jump_use_same_index() -> None:
    """验收核心：删除前检查与点击定位同一索引（同一 ClipUsage）。"""
    asset = _asset("used")
    other = _asset("unused")
    timeline = _sequence("t1", [
        ("v1", "used", 0, 30), ("v2", "used", 10, 20)])
    project = _project_with_assets(asset, other)
    report = usage_check_for_assets(project, [timeline], ["used"])
    index = build_asset_usage_index([timeline], project)
    assert report["used"] == index.locations_for("used")
    assert len(report["used"]) == 2
    assert usage_check_for_assets(project, [timeline], ["unused"]) == {}
