"""素材级 In/Out、标记与子片段范围的统一服务（P0-2）。

媒体池对齐 DaVinci Resolve 的三件事在这里有唯一的语义入口：

1. **素材级 In/Out**（master clip 的 I/O 标记）：``metadata["source_in"]``
   / ``metadata["source_out"]``，源文件帧域、含端点。In/Out 是编辑标记，
   不改写 total_frames/duration_seconds 等探测事实。
2. **素材级标记**（asset markers）：``metadata["asset_markers"]``，帧域为
   源文件绝对帧（子片段同样使用父文件绝对帧，与 scene-cut 子片段键同规）。
   与时间线实例级标记（``TimelineSequence.markers``）是两个域，互不串用。
3. **子片段范围**：复用 ``scene_cut.subclips`` 的 SUBCLIP_* 元数据键
   （单一事实来源），手工子片段与场景剪切子片段共用同一套键、海报帧
   （thumbnail_source_frame）与序列化通道，互不串用。

所有函数都是纯函数：收 ``EditProject`` 返回新 ``EditProject``，可安全
接入 ``ProjectCommandStack``（命令层见 asset_commands 的
SetAssetInOutCommand / SetAssetMarkersCommand / CreateSubclipCommand）。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any, Mapping, Optional

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.scene_cut.subclips import (
    SUBCLIP_IS_SUBCLIP,
    SUBCLIP_PARENT_ASSET_ID,
    SUBCLIP_PARENT_PATH,
    SUBCLIP_SOURCE_START,
    SUBCLIP_SOURCE_DURATION,
    SUBCLIP_SOURCE_END,
    SUBCLIP_THUMB_FRAME,
    is_subclip,
)

__all__ = [
    "ASSET_SOURCE_IN",
    "ASSET_SOURCE_OUT",
    "ASSET_MARKERS",
    "AssetMarkerValidationError",
    "SourceRangeError",
    "get_asset_in_out",
    "get_effective_source_range",
    "get_insert_range",
    "set_asset_in_out",
    "list_asset_markers",
    "make_asset_marker",
    "set_asset_markers",
    "add_asset_marker",
    "remove_asset_marker",
    "create_manual_subclip",
    "validate_marker",
]


# 素材级 In/Out 元数据键（源文件帧域，含端点；None = 未设置）
ASSET_SOURCE_IN = "source_in"
ASSET_SOURCE_OUT = "source_out"
# 素材级标记元数据键（list[dict]，见 make_asset_marker）
ASSET_MARKERS = "asset_markers"


class SourceRangeError(ValueError):
    """In/Out 或子片段范围不合法（越界/倒置/未知时长）。"""


class AssetMarkerValidationError(ValueError):
    """素材标记结构或帧号不合法。"""


# ================================================================
# 素材级 In/Out
# ================================================================

def _known_total_frames(asset: MediaAsset) -> Optional[int]:
    """素材源文件总帧数（子片段用 source_total_frames——父文件全长）。"""
    key = "source_total_frames" if is_subclip(asset) else "total_frames"
    value = asset.metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def get_asset_in_out(asset: MediaAsset) -> tuple[Optional[int], Optional[int]]:
    """素材级 In/Out（源文件帧域，含端点）。未设置返回 (None, None)。"""
    return (
        asset.metadata.get(ASSET_SOURCE_IN),
        asset.metadata.get(ASSET_SOURCE_OUT),
    )


def get_effective_source_range(
    asset: MediaAsset,
) -> tuple[Optional[int], Optional[int]]:
    """有效源范围：子片段范围优先，否则素材级 In/Out。

    子片段的范围由创建时的 SUBCLIP_SOURCE_* 键定义（父文件绝对帧），
    与素材级 In/Out 不叠加；master 素材返回 In/Out 标记。
    """
    if is_subclip(asset):
        start = asset.metadata.get(SUBCLIP_SOURCE_START)
        end = asset.metadata.get(SUBCLIP_SOURCE_END)
        if start is not None and end is not None:
            return (int(start), int(end))
        return (None, None)
    return get_asset_in_out(asset)


def get_insert_range(asset: MediaAsset) -> tuple[int, int]:
    """插入时间线时的默认源窗口 [start, end]（含端点）。

    无任何标记时为整个源：0 到 total_frames-1；总帧数未知时抛
    SourceRangeError（不允许用 0 帧窗口假装成功）。
    """
    start, end = get_effective_source_range(asset)
    if start is None:
        start = 0
    if end is None:
        total = _known_total_frames(asset)
        if total is None:
            raise SourceRangeError(
                f"素材 {asset.display_name} 总帧数未知，无法确定默认插入范围。")
        end = total - 1
    return (int(start), int(end))


def set_asset_in_out(
    project: EditProject,
    asset_id: str,
    in_frame: Optional[int],
    out_frame: Optional[int],
) -> EditProject:
    """设置/清除素材级 In/Out（None 清除对应端点，双 None 全清）。

    Raises:
        SourceRangeError: 帧号倒置、负数或超出已知源长度。
    """
    if asset_id not in project.assets:
        raise ValueError(f"Asset not found: {asset_id}")
    asset = project.assets[asset_id]
    for value in (in_frame, out_frame):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise SourceRangeError(f"In/Out 必须是整数帧号: {value!r}")
        if value < 0:
            raise SourceRangeError(f"In/Out 不能为负: {value}")
    if in_frame is not None and out_frame is not None and in_frame > out_frame:
        raise SourceRangeError(
            f"In ({in_frame}) 不能大于 Out ({out_frame})。")
    total = _known_total_frames(asset)
    if total is not None:
        for label, value in (("In", in_frame), ("Out", out_frame)):
            if value is not None and value >= total:
                raise SourceRangeError(
                    f"{label} ({value}) 超出素材总帧数 {total}。")
    return project.update_asset_metadata(asset_id, {
        ASSET_SOURCE_IN: in_frame,
        ASSET_SOURCE_OUT: out_frame,
    })


# ================================================================
# 素材级标记
# ================================================================

def make_asset_marker(
    frame: int,
    *,
    name: str = "",
    color: str = "#ff0000",
    comment: str = "",
    marker_id: Optional[str] = None,
) -> dict[str, Any]:
    """构造一条素材级标记（与时间线标记同款 #RRGGBB 颜色约定）。"""
    marker: dict[str, Any] = {
        "marker_id": marker_id or str(uuid.uuid4()),
        "frame": frame,
        "name": str(name),
        "color": str(color),
        "comment": str(comment),
    }
    validate_marker(marker)
    return marker


def validate_marker(marker: Mapping[str, Any]) -> None:
    """校验标记结构（保留未知键——schema 前向兼容）。"""
    marker_id = marker.get("marker_id")
    if not isinstance(marker_id, str) or not marker_id:
        raise AssetMarkerValidationError("marker_id 必须是非空字符串。")
    frame = marker.get("frame")
    if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
        raise AssetMarkerValidationError(f"frame 必须是非负整数: {frame!r}")
    color = marker.get("color")
    if (not isinstance(color, str) or not color.startswith("#") or len(color) != 7
            or any(character not in "0123456789abcdefABCDEF" for character in color[1:])):
        raise AssetMarkerValidationError(f"color 必须是 #RRGGBB: {color!r}")


def list_asset_markers(asset: MediaAsset) -> tuple[dict[str, Any], ...]:
    """素材级标记（按 frame 升序）。结构非法的旧数据在读取时拒绝伪造，
    抛 AssetMarkerValidationError 由调用方提示修复。"""
    raw = asset.metadata.get(ASSET_MARKERS)
    if not raw:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise AssetMarkerValidationError(
            f"{ASSET_MARKERS} 必须是列表，实际为 {type(raw).__name__}。")
    markers = []
    for item in raw:
        if not isinstance(item, dict):
            raise AssetMarkerValidationError("标记项必须是 dict。")
        validate_marker(item)
        markers.append(dict(item))
    markers.sort(key=lambda m: (m["frame"], m["marker_id"]))
    return tuple(markers)


def set_asset_markers(
    project: EditProject,
    asset_id: str,
    markers: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> EditProject:
    """整体替换素材级标记列表（原子操作，undo 快照由命令层承担）。"""
    if asset_id not in project.assets:
        raise ValueError(f"Asset not found: {asset_id}")
    asset = project.assets[asset_id]
    validated = []
    seen_ids: set[str] = set()
    total = _known_total_frames(asset)
    for marker in markers:
        if not isinstance(marker, dict):
            raise AssetMarkerValidationError("标记项必须是 dict。")
        validate_marker(marker)
        if marker["marker_id"] in seen_ids:
            raise AssetMarkerValidationError(
                f"标记 ID 重复: {marker['marker_id']}")
        seen_ids.add(marker["marker_id"])
        if total is not None and marker["frame"] >= total:
            raise SourceRangeError(
                f"标记帧 {marker['frame']} 超出素材总帧数 {total}。")
        validated.append(dict(marker))
    return project.update_asset_metadata(asset_id, {ASSET_MARKERS: validated})


def add_asset_marker(
    project: EditProject,
    asset_id: str,
    marker: dict[str, Any],
) -> EditProject:
    """追加一条素材级标记（保持 frame 升序存储）。"""
    validate_marker(marker)
    markers = list(list_asset_markers(project.assets[asset_id]))
    markers.append(dict(marker))
    return set_asset_markers(project, asset_id, markers)


def remove_asset_marker(
    project: EditProject,
    asset_id: str,
    marker_id: str,
) -> EditProject:
    """按 ID 移除一条素材级标记；ID 不存在时抛 KeyError（fail-loud）。"""
    markers = list(list_asset_markers(project.assets[asset_id]))
    remaining = [m for m in markers if m["marker_id"] != marker_id]
    if len(remaining) == len(markers):
        raise KeyError(f"Marker not found: {marker_id}")
    return set_asset_markers(project, asset_id, remaining)


# ================================================================
# 手工子片段（统一 In/Out → 子片段链路）
# ================================================================

def create_manual_subclip(
    project: EditProject,
    asset_id: str,
    start_frame: int,
    end_frame: int,
    *,
    display_name: str | None = None,
    target_bin_id: str | None = None,
    subclip_asset_id: str | None = None,
) -> tuple[EditProject, MediaAsset]:
    """从素材（或其 In/Out 范围）创建手工子片段。

    与场景剪切子片段共用 SUBCLIP_* 元数据键与海报帧键：共享原文件，
    但范围/海报互不串用。总帧数未知时抛 SourceRangeError（不允许
    无法核验首尾帧的子片段静默成功）。``subclip_asset_id`` 供命令重放
    （redo）复用稳定 ID；None 时生成新 UUID。

    Returns:
        (新 project, 新建子片段 MediaAsset)
    """
    if asset_id not in project.assets:
        raise ValueError(f"Asset not found: {asset_id}")
    parent = project.assets[asset_id]
    if is_subclip(parent):
        raise SourceRangeError(
            "子片段不能再创建子片段；请回到父素材。")
    if parent.media_type not in ("video", "image_sequence"):
        raise SourceRangeError(
            f"仅视频/图片序列支持子片段，{parent.media_type} 不支持。")
    if (isinstance(start_frame, bool) or not isinstance(start_frame, int)
            or isinstance(end_frame, bool) or not isinstance(end_frame, int)):
        raise SourceRangeError("子片段首尾帧必须是整数。")
    if start_frame < 0 or end_frame <= start_frame:
        raise SourceRangeError(
            f"子片段范围不合法: [{start_frame}, {end_frame}]。")
    total = _known_total_frames(parent)
    if total is None:
        raise SourceRangeError(
            f"素材 {parent.display_name} 总帧数未知，无法核验子片段范围。")
    if end_frame >= total:
        raise SourceRangeError(
            f"子片段尾帧 {end_frame} 超出素材总帧数 {total}。")

    fps = parent.metadata.get("fps")
    duration_seconds: Optional[float] = None
    if isinstance(fps, (int, float)) and not isinstance(fps, bool) and fps > 0:
        duration_seconds = (end_frame - start_frame + 1) / float(fps)

    metadata: dict[str, Any] = {
        **parent.metadata,
        SUBCLIP_IS_SUBCLIP: True,
        SUBCLIP_PARENT_ASSET_ID: parent.asset_id,
        SUBCLIP_PARENT_PATH: parent.path,
        SUBCLIP_SOURCE_START: start_frame,
        SUBCLIP_SOURCE_END: end_frame,
        SUBCLIP_SOURCE_DURATION: end_frame - start_frame + 1,
        "start_seconds": start_frame / float(fps) if duration_seconds is not None else None,
        "duration_seconds": duration_seconds,
        "end_seconds": (end_frame + 1) / float(fps) if duration_seconds is not None else None,
        "source_total_frames": total,
        # 手工子片段标识（区别于 scene_cut_kind；未知字段保留约定）
        "subclip_kind": "manual",
        # 海报帧 = 范围首帧：与其它子片段的海报帧键天然隔离
        SUBCLIP_THUMB_FRAME: start_frame,
        "total_frames": end_frame - start_frame + 1,
    }

    name = display_name or f"{parent.display_name} [{start_frame}-{end_frame}]"
    subclip_asset = MediaAsset(
        asset_id=subclip_asset_id or str(uuid.uuid4()),
        path=parent.path,
        media_type=parent.media_type,
        display_name=name,
        metadata=metadata,
        thumbnail_key=None,  # 由缩略图服务按 SUBCLIP_THUMB_FRAME 取帧
        proxy_path=parent.proxy_path,
        proxy_status=parent.proxy_status,
        is_offline=parent.is_offline,
        color_metadata=parent.color_metadata,
        created_at=time.time(),
    )
    new_project = project.import_subclip(subclip_asset, bin_id=target_bin_id)
    return new_project, subclip_asset
