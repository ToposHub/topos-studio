"""P0-2 素材级 In/Out、标记与子片段范围——统一服务与命令验收。

对应《媒体池后端交接计划》P0 第 2 项：
- source range / marker / subclip 的统一服务及序列化；
- 命令保持 apply/undo/redo 原子性；
- 首尾帧准确，重开工程一致；
- 子片段共享原文件但海报与范围互不串用。
"""
from __future__ import annotations

import pytest

from src.features.edit.core.asset_source_range import (
    ASSET_MARKERS,
    ASSET_SOURCE_IN,
    ASSET_SOURCE_OUT,
    AssetMarkerValidationError,
    SourceRangeError,
    add_asset_marker,
    create_manual_subclip,
    get_asset_in_out,
    get_effective_source_range,
    get_insert_range,
    list_asset_markers,
    make_asset_marker,
    remove_asset_marker,
    set_asset_in_out,
    set_asset_markers,
)
from src.features.edit.core.commands import (
    CreateSubclipCommand,
    ProjectCommandStack,
    SetAssetInOutCommand,
    SetAssetMarkersCommand,
)
from src.features.edit.core.project import EditProject
from src.features.edit.core.serialization import EditProjectSerializer
from src.features.edit.core.scene_cut.subclips import (
    SUBCLIP_SOURCE_END,
    SUBCLIP_SOURCE_START,
    SUBCLIP_THUMB_FRAME,
)


def _project_with_clip(total_frames: int = 100, fps: float = 25.0) -> tuple[EditProject, str]:
    project = EditProject.create("P0 range")
    path = "/tmp/p0-range-clip.mov"
    project = project.import_asset(path, "video", {
        "fps": fps, "fps_num": 25, "fps_den": 1,
        "total_frames": total_frames,
        "duration_seconds": total_frames / fps,
        "width": 1920, "height": 1080,
    })
    return project, project.get_asset_by_path(path).asset_id


# ================================================================
# 素材级 In/Out
# ================================================================

def test_set_and_clear_asset_in_out() -> None:
    project, asset_id = _project_with_clip()
    project = set_asset_in_out(project, asset_id, 10, 49)
    assert get_asset_in_out(project.assets[asset_id]) == (10, 49)
    assert get_insert_range(project.assets[asset_id]) == (10, 49)
    # 清除后回到全源窗口
    project = set_asset_in_out(project, asset_id, None, None)
    assert get_asset_in_out(project.assets[asset_id]) == (None, None)
    assert get_insert_range(project.assets[asset_id]) == (0, 99)


@pytest.mark.parametrize("in_frame,out_frame,reason", [
    (50, 10, "倒置"),
    (-1, 10, "负数"),
    (0, 100, "尾帧越界（总帧 100）"),
    (100, 100, "首帧越界"),
])
def test_in_out_validation_rejects_bad_ranges(in_frame, out_frame, reason) -> None:
    project, asset_id = _project_with_clip()
    with pytest.raises(SourceRangeError):
        set_asset_in_out(project, asset_id, in_frame, out_frame)
    # 失败不留半状态
    assert get_asset_in_out(project.assets[asset_id]) == (None, None)


def test_in_out_unknown_total_frames_allows_any_nonnegative() -> None:
    """总帧数未知：不越界拒绝（无据可依），但仍拒绝倒置/负数。"""
    project = EditProject.create("unknown length")
    project = project.import_asset("/tmp/unknown.mov", "video", {"fps": 25.0})
    asset_id = project.get_asset_by_path("/tmp/unknown.mov").asset_id
    project = set_asset_in_out(project, asset_id, 5, 9)
    assert get_asset_in_out(project.assets[asset_id]) == (5, 9)
    # 清除 In/Out 后总帧数未知：无法核验默认插入窗口——fail-loud
    project = set_asset_in_out(project, asset_id, None, None)
    with pytest.raises(SourceRangeError):
        get_insert_range(project.assets[asset_id])


# ================================================================
# 素材级标记
# ================================================================

def test_marker_add_list_remove_roundtrip() -> None:
    project, asset_id = _project_with_clip()
    m1 = make_asset_marker(30, name="第一次", color="#00ff00")
    m2 = make_asset_marker(5, name="开头", color="#ff0000")
    project = add_asset_marker(project, asset_id, m2)
    project = add_asset_marker(project, asset_id, m1)
    # 按 frame 升序
    markers = list_asset_markers(project.assets[asset_id])
    assert [m["frame"] for m in markers] == [5, 30]
    # 未知字段保留（schema 前向兼容）
    project = project.update_asset_metadata(asset_id, {
        ASSET_MARKERS: [dict(m1, custom_field="keep-me")],
    })
    assert list_asset_markers(project.assets[asset_id])[0]["custom_field"] == "keep-me"
    project = remove_asset_marker(project, asset_id, m1["marker_id"])
    assert list_asset_markers(project.assets[asset_id]) == ()
    with pytest.raises(KeyError):
        remove_asset_marker(project, asset_id, m1["marker_id"])


@pytest.mark.parametrize("marker,exc", [
    ({"marker_id": "", "frame": 1, "color": "#ff0000"}, AssetMarkerValidationError),
    ({"marker_id": "x", "frame": -1, "color": "#ff0000"}, AssetMarkerValidationError),
    ({"marker_id": "x", "frame": 1, "color": "red"}, AssetMarkerValidationError),
    ({"marker_id": "x", "frame": 500, "color": "#ff0000"}, SourceRangeError),
])
def test_marker_schema_validation(marker, exc) -> None:
    project, asset_id = _project_with_clip()
    with pytest.raises(exc):
        set_asset_markers(project, asset_id, [marker])


def test_markers_reject_duplicate_ids() -> None:
    project, asset_id = _project_with_clip()
    m = make_asset_marker(10)
    with pytest.raises(AssetMarkerValidationError):
        set_asset_markers(project, asset_id, [m, dict(m)])


# ================================================================
# 子片段
# ================================================================

def test_manual_subclip_range_and_poster_isolation() -> None:
    """两个子片段共享原文件，但范围与海报帧互不串用、父素材不被改写。"""
    project, parent_id = _project_with_clip(total_frames=100)
    parent_path = project.assets[parent_id].path
    parent_meta_before = dict(project.assets[parent_id].metadata)

    project, sub1 = create_manual_subclip(project, parent_id, 0, 24, display_name="头")
    project, sub2 = create_manual_subclip(project, parent_id, 50, 99, display_name="尾")

    for sub, (start, end) in ((sub1, (0, 24)), (sub2, (50, 99))):
        meta = project.assets[sub.asset_id].metadata
        assert meta[SUBCLIP_SOURCE_START] == start
        assert meta[SUBCLIP_SOURCE_END] == end
        assert meta[SUBCLIP_THUMB_FRAME] == start  # 海报=各自首帧
        assert meta["total_frames"] == end - start + 1
        assert meta["source_total_frames"] == 100  # 父全长保留
        assert project.assets[sub.asset_id].path == parent_path  # 共享原文件
        assert get_effective_source_range(project.assets[sub.asset_id]) == (start, end)
    # 父素材 metadata 未被子片段创建改写
    assert dict(project.assets[parent_id].metadata) == parent_meta_before
    assert get_effective_source_range(project.assets[parent_id]) == (None, None)


def test_manual_subclip_validation() -> None:
    project, asset_id = _project_with_clip(total_frames=100)
    with pytest.raises(SourceRangeError):
        create_manual_subclip(project, asset_id, 90, 100)  # 尾帧越界
    with pytest.raises(SourceRangeError):
        create_manual_subclip(project, asset_id, 10, 10)   # 空范围
    project, sub = create_manual_subclip(project, asset_id, 0, 9)
    with pytest.raises(SourceRangeError):
        create_manual_subclip(project, sub.asset_id, 0, 4)  # 子片段不能再切
    project = project.import_asset("/tmp/p0-img.png", "image", {"width": 4})
    image_id = project.get_asset_by_path("/tmp/p0-img.png").asset_id
    with pytest.raises(SourceRangeError):
        create_manual_subclip(project, image_id, 0, 4)  # 静态图不支持


def test_manual_subclip_unknown_total_frames_rejected() -> None:
    project = EditProject.create("unknown")
    project = project.import_asset("/tmp/unknown-sub.mov", "video", {"fps": 25.0})
    asset_id = project.get_asset_by_path("/tmp/unknown-sub.mov").asset_id
    with pytest.raises(SourceRangeError):
        create_manual_subclip(project, asset_id, 0, 9)


# ================================================================
# 命令原子性（ProjectCommandStack）
# ================================================================

def test_in_out_command_undo_redo_atomic() -> None:
    project, asset_id = _project_with_clip()
    stack = ProjectCommandStack()
    project = stack.execute(SetAssetInOutCommand.create(asset_id, 12, 48), project)
    assert get_asset_in_out(project.assets[asset_id]) == (12, 48)
    project = stack.undo(project)
    assert get_asset_in_out(project.assets[asset_id]) == (None, None)
    project = stack.redo(project)
    assert get_asset_in_out(project.assets[asset_id]) == (12, 48)
    # 清除也是一次可撤销命令
    project = stack.execute(SetAssetInOutCommand.create(asset_id, None, None), project)
    assert get_asset_in_out(project.assets[asset_id]) == (None, None)
    project = stack.undo(project)
    assert get_asset_in_out(project.assets[asset_id]) == (12, 48)


def test_invalid_in_out_command_leaves_project_untouched() -> None:
    project, asset_id = _project_with_clip()
    stack = ProjectCommandStack()
    with pytest.raises(SourceRangeError):
        stack.execute(SetAssetInOutCommand.create(asset_id, 80, 20), project)
    assert get_asset_in_out(project.assets[asset_id]) == (None, None)
    assert stack.can_undo() is False  # 失败命令不进栈


def test_markers_command_undo_redo_atomic() -> None:
    project, asset_id = _project_with_clip()
    stack = ProjectCommandStack()
    marker = make_asset_marker(42, name="动作")
    project = stack.execute(SetAssetMarkersCommand.create(asset_id, [marker]), project)
    assert [m["marker_id"] for m in list_asset_markers(project.assets[asset_id])] == [marker["marker_id"]]
    project = stack.undo(project)
    assert list_asset_markers(project.assets[asset_id]) == ()
    project = stack.redo(project)
    assert list_asset_markers(project.assets[asset_id])[0]["name"] == "动作"


def test_create_subclip_command_undo_redo() -> None:
    project, asset_id = _project_with_clip()
    stack = ProjectCommandStack()
    project = stack.execute(
        CreateSubclipCommand.create(asset_id, 10, 39, display_name="中段"), project)
    sub = next(
        a for a in project.assets.values()
        if a.metadata.get("subclip_kind") == "manual")
    assert sub.display_name == "中段"
    project = stack.undo(project)
    assert sub.asset_id not in project.assets  # undo 移除子片段
    project = stack.redo(project)
    assert sub.asset_id in project.assets
    restored = project.assets[sub.asset_id]
    assert restored.metadata[SUBCLIP_SOURCE_START] == 10
    assert restored.metadata[SUBCLIP_THUMB_FRAME] == 10


# ================================================================
# 序列化往返（重开工程一致）
# ================================================================

def test_in_out_markers_subclip_survive_serialization_roundtrip() -> None:
    project, asset_id = _project_with_clip(total_frames=200)
    marker = make_asset_marker(60, name="往返", color="#0000ff", comment="备注")
    project = set_asset_in_out(project, asset_id, 20, 80)
    project = set_asset_markers(project, asset_id, [marker])
    project, sub = create_manual_subclip(project, asset_id, 30, 70, display_name="往返子片段")

    payload = EditProjectSerializer.to_json(project)
    reopened = EditProjectSerializer.from_json(payload)

    asset = reopened.assets[asset_id]
    assert asset.metadata[ASSET_SOURCE_IN] == 20
    assert asset.metadata[ASSET_SOURCE_OUT] == 80
    assert list_asset_markers(asset)[0]["name"] == "往返"
    assert list_asset_markers(asset)[0]["comment"] == "备注"
    reopened_sub = reopened.assets.get(sub.asset_id)
    assert reopened_sub is not None
    assert reopened_sub.metadata[SUBCLIP_SOURCE_START] == 30
    assert reopened_sub.metadata[SUBCLIP_SOURCE_END] == 70
    assert reopened_sub.metadata[SUBCLIP_THUMB_FRAME] == 30
