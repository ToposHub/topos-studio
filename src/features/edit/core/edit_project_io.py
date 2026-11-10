"""
Edit Project IO Module
编辑项目文件 IO 操作模块。

## 聧责

- EditProjectIO: 编辑项目文件管理器（薄包装层）
- 保存/加载（带备份）
- 自动保存（QTimer）
- 备份机制（.topos.bak）
- Recent files 管理

## 设计原则

- EditProjectIO 是 ToposProjectIO 的薄包装层
- 内部转换 EditProject <-> ToposProject
- 保存前自动备份上一版
- 自动保存到临时位置或覆盖原文件（可配置）
- 错误处理和日志记录
- 统一工程包格式（.topos ZIP 包）

## 文件扩展名约定

- `.topos`: 统一工程包格式（Edit/Color/Composite 项目）
- `.topos.bak`: 备份文件
- `.topos.autosave`: 自动保存临时文件

## 废弃说明

- 所有工程统一使用 .topos 格式
- 所有保存通过 ToposProjectIO 写入 .topos ZIP 包
"""

from __future__ import annotations

import os
import copy
import json
import shutil
import logging
import threading
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass
import time

from PyQt6.QtCore import QTimer, QObject, QRunnable, QThreadPool, pyqtSignal

from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin, SmartBin, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.edit.core.serialization import (
    EditProjectSerializer,
    UIState,
    EDIT_PROJECT_EXTENSION,
)

# 统一工程包 IO
from src.shared.project import ToposProject, ToposProjectIO, ToposProjectIOConfig
from src.shared.project.archive_backing import ArchiveBackingStore
from src.shared.project.payload_cache import ProjectArchiveReadError
from src.shared.project.resource_resolver import is_package_cache_path as _is_cache_path
from src.shared.project.topos_project import (
    MediaAssetRef,
    ProjectBinRef,
    TimelineRef,
    ColorStackRef,
    CompositionRef,
)
# DESK-04A：未知字段捕获（与各数据类 _KNOWN_KEYS 配对）
from src.shared.extra_fields import split_extras

logger = logging.getLogger(__name__)

# 默认自动保存间隔（分钟）
DEFAULT_AUTOSAVE_INTERVAL = 5

# 备份文件扩展名
BACKUP_EXTENSION = ".bak"
AUTOSAVE_EXTENSION = ".autosave"


def _safe_mtime_or_max(path: "Path") -> float:
    """AUD-16711：备份 mtime 排序键——stat 失败（并发删除/坏符号链接）时
    返回 +inf，让该文件排在淘汰序列最后（最不容易被删）。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


class _AsyncSaveSignals(QObject):
    """Queued completion signal for background project writes."""

    finished = pyqtSignal(bool, str, str, object)


class _AsyncSaveWorker(QRunnable):
    """Write an already-captured project snapshot off the UI thread."""

    def __init__(self, io: "EditProjectIO", kwargs: dict[str, Any], filepath: str) -> None:
        super().__init__()
        self._io = io
        self._kwargs = kwargs
        self._filepath = filepath
        self.signals = _AsyncSaveSignals()

    def run(self) -> None:
        try:
            success = bool(self._io.save_project(**self._kwargs))
            error = "" if success else f"Failed to save: {self._filepath}"
        except Exception as exc:  # noqa: BLE001
            success = False
            error = str(exc)
        self.signals.finished.emit(success, self._filepath, error, self)


# ================================================================
# 转换函数：EditProject <-> ToposProject
# ================================================================

def _collect_color_management_settings() -> dict:
    """收集用户全局色彩管理偏好（见 color_space_descriptor）。"""
    try:
        from src.shared.color.color_space_descriptor import (
            collect_color_management_settings,
        )
        return collect_color_management_settings()
    except ImportError:
        # 模块缺失属可接受的降级（精简构建），落盘侧回退空 dict。
        logger.debug(
            "color_space_descriptor unavailable; saving without "
            "color management settings",
        )
        return {}
    except Exception:
        # AUD-16713：收集逻辑的运行期错误不得静默——否则工程照常落盘但
        # color_management 为空，P0-COLOR-04 的"色彩管理随工程落盘"承诺
        # 无声失效且无从诊断。
        logger.exception(
            "Failed to collect color management settings; "
            "project will be saved without them",
        )
        return {}


def _serialize_source_patch_overrides(overrides: Any) -> dict[str, Any]:
    """F2.1：SourcePatchSpec 覆盖 → 可 JSON 化 dict。

    非 Spec 值（来自旧包/未来 schema 的未知载荷）原样保留，load→save
    不丢数据；运行时经 ``patch_for_asset`` 视为"无覆盖"。
    """
    if not overrides:
        return {}
    from src.features.edit.core.source_patch import SourcePatchSpec
    result: dict[str, Any] = {}
    for asset_id, value in overrides.items():
        result[asset_id] = (
            value.to_dict() if isinstance(value, SourcePatchSpec) else value
        )
    return result


def _serialize_color_groups(groups: Any) -> dict[str, Any]:
    """M02：ColorGroupData 组表 → 可 JSON 化 dict（与 _deserialize_color_groups
    成对；此前正向转换器内联此逻辑，serialization.py 无法复用）。"""
    if not groups:
        return {}
    result: dict[str, Any] = {}
    for gid, g in groups.items():
        result[str(gid)] = (
            copy.deepcopy(g.to_dict()) if hasattr(g, "to_dict") else copy.deepcopy(g)
        )
    return result


def _serialize_project_snapshots(table: Any) -> dict[str, Any]:
    """M02（S01）：工程快照表 → 可 JSON 化 dict（整表 deepcopy；
    payload 本身由 capture_snapshot_payload 产出，已是 JSON 化形态）。"""
    if not isinstance(table, dict):
        return {}
    return copy.deepcopy(table)


def _deserialize_project_snapshots(raw: Any) -> dict[str, Any]:
    """M02（S01）：落盘 dict → 快照表。逐条形状校验（meta+payload 双键），
    单条损坏丢弃并留日志，不阻断整表；恢复时 restore_snapshot 仍有
    payload_hash 防篡改校验。"""
    if not isinstance(raw, dict):
        return {}
    table: dict[str, Any] = {}
    for name, entry in raw.items():
        if (isinstance(entry, dict) and isinstance(entry.get("meta"), dict)
                and isinstance(entry.get("payload"), dict)):
            table[str(name)] = entry
        else:
            logger.warning(
                "project_snapshots: dropping malformed entry %r "
                "(meta/payload shape invalid)", name)
    return table


def _deserialize_color_groups(raw: Any) -> dict[str, Any]:
    """F4.1：组表载荷 → {group_id: ColorGroupData}。

    识别键 = group_id + members（与解释覆盖的 "fps"、绑定的
    "wav_asset_id" 同型约定）；未知载荷原样保留（不识别的字典不猜，
    防误吞其他域数据）。
    """
    if not isinstance(raw, dict):
        return {}
    from src.features.edit.core.color_group import ColorGroupData
    groups: dict[str, Any] = {}
    for gid, payload in raw.items():
        if isinstance(payload, dict) and "group_id" in payload \
                and "members" in payload:
            try:
                groups[str(gid)] = ColorGroupData.from_dict(payload)
            except (ValueError, TypeError, KeyError):
                continue  # 单条损坏不阻断整表；缺组可重建
        else:
            groups[str(gid)] = payload  # 未知载荷保留
    return groups


def _deserialize_source_patch_overrides(raw: Any) -> dict[str, Any]:
    """F2.1：落盘 dict → ``{asset_id: SourcePatchSpec}``，未知载荷保留。"""
    if not isinstance(raw, dict):
        return {}
    from src.features.edit.core.source_patch import SourcePatchSpec
    result: dict[str, Any] = {}
    for asset_id, payload in raw.items():
        if isinstance(payload, dict):
            try:
                result[asset_id] = SourcePatchSpec.from_dict(payload)
                continue
            except Exception:
                # 未知/畸形载荷：保留原样（重存不丢），行为回退默认直通。
                logger.warning(
                    "Unrecognized source patch payload for asset %s; "
                    "preserved verbatim, defaulting to passthrough",
                    asset_id,
                )
        result[asset_id] = payload
    return result


def _serialize_interpretation_overrides(overrides: Any) -> dict[str, Any]:
    """F2.7：解释覆盖 → 可 JSON 化 dict；未知载荷原样保留。"""
    if not overrides:
        return {}
    from src.features.edit.core.interpretation import InterpretationOverride
    result: dict[str, Any] = {}
    for asset_id, value in overrides.items():
        result[asset_id] = (
            value.to_dict() if isinstance(value, InterpretationOverride)
            else value)
    return result


def _deserialize_interpretation_overrides(raw: Any) -> dict[str, Any]:
    """F2.7：落盘 dict → 解释覆盖；未知/畸形载荷保留原样。"""
    if not isinstance(raw, dict):
        return {}
    from src.features.edit.core.interpretation import InterpretationOverride
    result: dict[str, Any] = {}
    for asset_id, payload in raw.items():
        # 仅识别已知 schema（含 fps 键）；其余载荷原样保留（重存不丢）
        if isinstance(payload, dict) and "fps" in payload:
            try:
                result[asset_id] = InterpretationOverride.from_dict(payload)
                continue
            except Exception as e:
                logger.warning(
                    "Unrecognized interpretation override payload for asset "
                    "%s (%s); preserved verbatim", asset_id, e)
        result[asset_id] = payload
    return result



def _serialize_audio_sync_bindings(bindings: Any) -> dict[str, Any]:
    """F2.10：外录绑定 → 可 JSON 化 dict；未知载荷原样保留。"""
    if not bindings:
        return {}
    from src.features.edit.core.audio_sync import AudioSyncBinding
    result: dict[str, Any] = {}
    for asset_id, value in bindings.items():
        result[asset_id] = (
            value.to_dict() if isinstance(value, AudioSyncBinding) else value)
    return result


def _deserialize_audio_sync_bindings(raw: Any) -> dict[str, Any]:
    """F2.10：落盘 dict → 外录绑定；仅识别含 wav_asset_id 的已知 schema。"""
    if not isinstance(raw, dict):
        return {}
    from src.features.edit.core.audio_sync import AudioSyncBinding
    result: dict[str, Any] = {}
    for asset_id, payload in raw.items():
        if isinstance(payload, dict) and "wav_asset_id" in payload:
            try:
                result[asset_id] = AudioSyncBinding.from_dict(payload)
                continue
            except Exception as e:
                logger.warning(
                    "Unrecognized audio sync binding payload for asset %s "
                    "(%s); preserved verbatim", asset_id, e)
        result[asset_id] = payload
    return result


def convert_edit_project_to_topos_project(
    edit_project: EditProject,
    force_project_mode: str | None = None,
) -> ToposProject:
    """
    将 EditProject 转换为 ToposProject。

    Args:
        edit_project: 源 EditProject。
        force_project_mode: 显式指定 project_mode ("edit_project" /
            "standalone_image")。不传时按旧规则从 timelines 推断——有
            timeline 视为 edit_project,否则 standalone_image。

            注意:新建的空视频项目在用户拖入素材前 timelines 为空,会被
            错误地推断为 standalone_image。调用方应为此类项目显式传入
            "edit_project"。
        edit_project: EditProject 实例

    Returns:
        ToposProject 实例
    """
    # 转换 assets
    assets: dict[str, MediaAssetRef] = {}
    for asset_id, asset in edit_project.assets.items():
        # DESK-03C：内嵌资源素材的 path 可能是解包缓存路径（load 物化）
        # ——project.json 索引层持外部原路径提示，缓存路径不落盘。
        if asset.resource_uri:
            ref_path = (
                asset.path_original
                or ("" if _is_cache_path(asset.path) else asset.path)
            )
        else:
            ref_path = asset.path
        assets[asset_id] = MediaAssetRef(
            asset_id=asset.asset_id,
            path=ref_path,
            media_type=asset.media_type,
            display_name=asset.display_name,
            is_offline=asset.is_offline,
        )

    # 转换 bins
    bins: dict[str, ProjectBinRef] = {}
    for bin_id, bin_obj in edit_project.bins.items():
        bins[bin_id] = ProjectBinRef(
            bin_id=bin_obj.bin_id,
            name=bin_obj.name,
            parent_id=bin_obj.parent_id,
            asset_count=len(bin_obj.asset_ids),
        )

    # 转换 timelines
    timelines: dict[str, TimelineRef] = {}
    for timeline_id, timeline in edit_project.timelines.items():
        timelines[timeline_id] = TimelineRef(
            timeline_id=timeline.timeline_id,
            name=timeline.name,
            # F4.2 修复：TimelineSequence.fps 是 Rational（无序比较），
            # TimelineRef 校验需要 float——此前任何带时间线的工程走
            # 转换器直接 TypeError
            fps=float(timeline.fps),
            width=timeline.width,
            height=timeline.height,
        )

    # 转换 color_stacks
    color_stacks: dict[str, ColorStackRef] = {}
    for stack_id, stack in edit_project.color_stacks.items():
        # N07：ColorStackRef 合法域无 "timeline"——映射到既有
        # timeline_adjustment（payload 侧 ColorStackData.scope 仍为 timeline）
        _ref_scope = (stack.scope if hasattr(stack, 'scope')
                      else "asset_default")
        color_stacks[stack_id] = ColorStackRef(
            stack_id=stack.stack_id,
            asset_id=stack.asset_id,
            name=stack.name,
            scope=("timeline_adjustment" if _ref_scope == "timeline"
                   else _ref_scope),
        )

    # 确定工程模式 (schema v2: edit_project or standalone_image)
    if force_project_mode is not None:
        project_mode = force_project_mode
    else:
        project_mode = "edit_project" if edit_project.timelines else "standalone_image"

    # 转换 compositions (P9.2)
    compositions: dict[str, CompositionRef] = {}
    for comp_id, comp_data in edit_project.compositions.items():
        if isinstance(comp_data, dict):
            compositions[comp_id] = CompositionRef(
                composition_id=comp_data.get("composition_id", comp_id),
                name=comp_data.get("name", "Default Composition"),
                source_kind=comp_data.get("source_kind", "standalone"),
                asset_id=comp_data.get("asset_id"),
                timeline_id=comp_data.get("timeline_id"),
                clip_id=comp_data.get("clip_id"),
            )

    primary_asset_id = None
    primary_color_stack_id = None
    if not edit_project.timelines:
        image_asset = next(
            (asset for asset in edit_project.assets.values() if asset.media_type == "image"),
            None,
        )
        if image_asset is not None:
            primary_asset_id = image_asset.asset_id
            image_stack = edit_project.get_color_stack_for_asset(image_asset.asset_id)
            if image_stack is not None:
                primary_color_stack_id = image_stack.stack_id

    return ToposProject(
        project_id=edit_project.project_id,
        name=edit_project.name,
        project_mode=project_mode,
        assets=assets,
        bins=bins,
        timelines=timelines,
        color_stacks=color_stacks,
        compositions=compositions,  # 正确映射 compositions
        deliver_presets={},  # EditProject 不包含 deliver_presets
        ui_state={},
        # Persist the implicit media-pool root identity separately from the
        # bin index.  Without this, loading a project whose bins are stored
        # in edit/bins.json makes EditProject think the root is missing and
        # creates another visible-looking Root container.
        settings={
            "root_bin_id": edit_project.root_bin_id,
            "active_comp_clip_id": getattr(edit_project, "active_comp_clip_id", None),
            # M14.3b：工程级采样率随工程包往返（settings 由 ToposProjectIO
            # 写入 project.json；0/无键 = 未设置，由访问器解析默认 48000）
            "project_sample_rate": int(
                getattr(edit_project, "project_sample_rate", 0) or 0),
            # P0-COLOR-04：色彩管理设定随工程落盘（换机器/交接不丢）
            "color_management": _collect_color_management_settings(),
            "render_cache_settings": (
                edit_project.render_cache_settings.to_dict()
                if hasattr(edit_project.render_cache_settings, "to_dict")
                else edit_project.render_cache_settings
            ),
            "proxy_policy": (
                edit_project.proxy_policy.to_dict()
                if hasattr(edit_project.proxy_policy, "to_dict")
                else edit_project.proxy_policy
            ),
            # F2.1：素材级 Source Patch 覆盖随工程落盘；非 Spec 的未知
            # 载荷原样保留（加载侧同语义，保证 load→save 不丢数据）。
            "source_patch_overrides": _serialize_source_patch_overrides(
                edit_project.source_patch_overrides),
            # F4.1: 场景/机位调色组表（识别键 group_id+members）
            "color_groups": _serialize_color_groups(edit_project.color_groups),
            # N07：timeline 栈映射（栈本体在 color_stacks 全量里）
            "timeline_grade_stacks": {
                str(k): str(v) for k, v in
                (getattr(edit_project, "timeline_grade_stacks", {}) or {}
                 ).items() if v},
            # F4.2: 栈级命名版本与共享锁（识别键 active/locked/versions）
            "color_stack_versions": copy.deepcopy(
                edit_project.color_stack_versions or {}),
            # F2.7：素材级解释覆盖随工程落盘；未知载荷原样保留
            "interpretation_overrides": (
                _serialize_interpretation_overrides(
                    edit_project.interpretation_overrides)),
            # F2.10：外录音频绑定随工程落盘；未知载荷原样保留
            "audio_sync_bindings": _serialize_audio_sync_bindings(
                edit_project.audio_sync_bindings),
            # M03.4：同步诊断随工程包往返（settings 写 project.json）
            "sync_diagnostics": dict(
                getattr(edit_project, "sync_diagnostics", None) or {}),
            "render_cache_manifests": dict(edit_project.render_cache_manifests),
            # M02（S01）：工程快照表随工程落盘（此前保存即丢）。载荷为
            # {name: {"meta": {...}, "payload": {...}}}——纯 JSON 化 dict，
            # deepcopy 即可；恢复侧 _deserialize_project_snapshots 校验形状。
            "project_snapshots": _serialize_project_snapshots(
                edit_project.project_snapshots),
        },
        primary_asset_id=primary_asset_id,
        primary_color_stack_id=primary_color_stack_id,
        active_timeline_id=edit_project.active_timeline_id,
        active_composition_id=getattr(edit_project, "active_composition_id", None),
        created_at=edit_project.created_at,
        modified_at=time.time(),
        revision=getattr(edit_project, "revision", 0),
    )


def convert_topos_project_to_edit_project(
    topos_project: ToposProject,
    full_data: dict[str, Any] | None = None,
) -> EditProject:
    """
    将 ToposProject 转换为 EditProject。

    Args:
        topos_project: ToposProject 实例
        full_data: 完整数据字典（包含 assets、bins 等详细数据）

    Returns:
        EditProject 实例
    """
    # P0-COLOR-04：工程级色彩管理设定在此注入运行时覆盖层——两条加载
    # 路径（Edit 工程 / standalone 图片工程）都经过本函数，一处注入
    # 全覆盖。工程无该键时清空覆盖层（回退用户全局偏好）。
    try:
        from src.shared.color.color_space_descriptor import (
            set_project_color_overrides,
        )
        set_project_color_overrides(
            (getattr(topos_project, "settings", None) or {}).get(
                "color_management"))
    except ImportError:
        # AUD-16714：模块缺失属可接受的降级（精简构建），保持全局默认。
        logger.debug(
            "color_space_descriptor unavailable; project color "
            "overrides not injected",
        )
    except Exception:
        # AUD-16714：注入失败意味着工程将以全局默认（而非作者所见）的
        # 色彩管理渲染——静默吞掉会击穿"两条加载路径一处注入全覆盖"的
        # 保证，必须留日志。
        logger.exception(
            "Failed to inject project color management overrides; "
            "falling back to user global defaults",
        )

    # 如果有完整数据，使用完整数据构建
    if full_data:
        # 从 full_data 获取完整的 assets
        assets_data = full_data.get("assets", {})
        assets: dict[str, MediaAsset] = {}
        for asset_id, asset_data in assets_data.items():
            if isinstance(asset_data, dict):
                assets[asset_id] = MediaAsset(
                    asset_id=asset_data.get("asset_id", ""),
                    path=asset_data.get("path", ""),
                    media_type=asset_data.get("media_type", "image"),
                    display_name=asset_data.get("display_name", ""),
                    metadata=asset_data.get("metadata", {}),
                    thumbnail_key=asset_data.get("thumbnail_key"),
                    proxy_path=asset_data.get("proxy_path"),
                    proxy_status=asset_data.get("proxy_status", "none"),
                    is_offline=asset_data.get("is_offline", False),
                    color_metadata=asset_data.get("color_metadata"),
                    created_at=asset_data.get("created_at", 0.0),
                    reel=asset_data.get("reel", ""),
                    scene=asset_data.get("scene", ""),
                    shot=asset_data.get("shot", ""),
                    take=asset_data.get("take", ""),
                    keywords=tuple(asset_data.get("keywords", ())),
                    rating=int(asset_data.get("rating", 0)),
                    flag=asset_data.get("flag", "none"),
                    color_label=asset_data.get("color_label", "none"),
                    comment=asset_data.get("comment", ""),
                    # DESK-01B：内嵌资源身份（load 侧物化后 path 可能指向
                    # 解包缓存；resource_uri 是权威引用）
                    resource_uri=asset_data.get("resource_uri"),
                    path_original=asset_data.get("path_original"),
                    # DESK-04A：捕获未知键（load→save 不丢）
                    _extra_fields=split_extras(
                        asset_data, MediaAsset._KNOWN_KEYS),
                )

        # 从 full_data 获取完整的 bins
        bins_data = full_data.get("bins", {})
        bins: dict[str, ProjectBin] = {}
        for bin_id, bin_data in bins_data.items():
            if isinstance(bin_data, dict):
                bins[bin_id] = ProjectBin(
                    bin_id=bin_data.get("bin_id", ""),
                    name=bin_data.get("name", ""),
                    parent_id=bin_data.get("parent_id"),
                    asset_ids=tuple(bin_data.get("asset_ids", [])),
                    timeline_ids=tuple(bin_data.get("timeline_ids", [])),
                    # DESK-04A：捕获未知键（load→save 不丢）
                    _extra_fields=split_extras(bin_data, ProjectBin._KNOWN_KEYS),
                )

        # 从 full_data 获取完整的 timelines
        timelines_data = full_data.get("timelines", {})
        timelines: dict[str, TimelineSequenceRef] = {}
        for timeline_id, timeline_data in timelines_data.items():
            if isinstance(timeline_data, dict):
                timelines[timeline_id] = TimelineSequenceRef(
                    timeline_id=timeline_data.get("timeline_id", ""),
                    name=timeline_data.get("name", ""),
                    fps=timeline_data.get("fps", 24.0),
                    width=timeline_data.get("width", 1920),
                    height=timeline_data.get("height", 1080),
                    sample_rate=timeline_data.get("sample_rate", 48000),
                    format_settings=dict(timeline_data.get("format_settings", {})),
                    created_at=timeline_data.get("created_at", 0.0),
                    # DESK-04A：捕获未知键（load→save 不丢）
                    _extra_fields=split_extras(
                        timeline_data, TimelineSequenceRef._KNOWN_KEYS),
                )
    else:
        # 从 ToposProject 的引用构建简化版
        assets = {}
        for asset_id, asset_ref in topos_project.assets.items():
            assets[asset_id] = MediaAsset(
                asset_id=asset_ref.asset_id,
                path=asset_ref.path,
                media_type=asset_ref.media_type,
                display_name=asset_ref.display_name,
                metadata={},
                is_offline=asset_ref.is_offline,
            )

        bins = {}
        for bin_id, bin_ref in topos_project.bins.items():
            bins[bin_id] = ProjectBin(
                bin_id=bin_ref.bin_id,
                name=bin_ref.name,
                parent_id=bin_ref.parent_id,
                asset_ids=(),
                timeline_ids=(),
            )

        timelines = {}
        for timeline_id, timeline_ref in topos_project.timelines.items():
            timelines[timeline_id] = TimelineSequenceRef(
                timeline_id=timeline_ref.timeline_id,
                name=timeline_ref.name,
                fps=timeline_ref.fps,
                width=timeline_ref.width,
                height=timeline_ref.height,
                sample_rate=48000,
            )

    # color_stacks 从 ToposProject 或 full_data 获取
    # 暂时使用空的 color_stacks，后续可以扩展
    from src.features.edit.core.project import ColorStackData
    color_stacks: dict[str, ColorStackData] = {}
    if full_data and "color_stacks" in full_data:
        color_stacks_data = full_data.get("color_stacks", {})
        for stack_id, stack_data in color_stacks_data.items():
            if isinstance(stack_data, dict):
                color_stacks[stack_id] = ColorStackData.from_dict(stack_data)

    # compositions 从 full_data 获取
    compositions: dict[str, Any] = {}
    if full_data and "compositions" in full_data:
        compositions = full_data.get("compositions", {})

    # comp_clips 从 full_data 获取
    comp_clips: dict[str, Any] = {}
    if full_data and "comp_clips" in full_data:
        comp_clips = full_data.get("comp_clips", {})

    # P1-12: 用户自建 Smart Bins（edit/smart_bins.json）
    smart_bins: dict[str, SmartBin] = {}
    for bin_id, bin_data in (full_data or {}).get("smart_bins", {}).items():
        if isinstance(bin_data, dict):
            smart_bins[bin_id] = SmartBin(
                bin_id=bin_data.get("bin_id", bin_id),
                name=bin_data.get("name", ""),
                filter_type=bin_data.get("filter_type", "custom"),
                filter_params=dict(bin_data.get("filter_params", {})),
                # DESK-04A：捕获未知键（load→save 不丢）
                _extra_fields=split_extras(bin_data, SmartBin._KNOWN_KEYS),
            )

    # The root identity lives in project settings for the unified .topos
    # format.  Fall back to the first Root/top-level bin for older packages.
    root_bin_id = ""
    if full_data:
        root_bin_id = str(full_data.get("root_bin_id") or "")
    if not root_bin_id:
        root_bin_id = str(
            (topos_project.settings or {}).get("root_bin_id", "")
        )
    # A partial package can legitimately omit ``bins`` while retaining the
    # root identity.  Do not erase that identity merely because there is no
    # bin index to validate it against.  When an index is present, however,
    # discard an invalid reference and repair it from the actual root bin.
    if root_bin_id and bins and root_bin_id not in bins:
        root_bin_id = ""
    if not root_bin_id:
        root_candidates = [
            bin_id
            for bin_id, bin_obj in bins.items()
            if bin_obj.name.casefold() == "root" and bin_obj.parent_id is None
        ]
        root_bin_id = root_candidates[0] if root_candidates else ""
    if not root_bin_id:
        root_bin_id = topos_project.root_bin_id or ""

    # Repair packages written before root_bin_id was persisted.  Repeated
    # open/save cycles could have accumulated multiple auto-created Root
    # bins. Keep the first canonical Root, merge their references, and
    # reparent any children that pointed at a duplicate.
    duplicate_root_ids = {
        bin_id
        for bin_id, bin_obj in bins.items()
        if bin_id != root_bin_id
        and bin_obj.name.casefold() == "root"
        and bin_obj.parent_id is None
    }
    if duplicate_root_ids and root_bin_id in bins:
        canonical = bins[root_bin_id]
        asset_ids = list(canonical.asset_ids)
        timeline_ids = list(canonical.timeline_ids)
        for duplicate_id in duplicate_root_ids:
            duplicate = bins[duplicate_id]
            asset_ids.extend(
                asset_id for asset_id in duplicate.asset_ids
                if asset_id not in asset_ids
            )
            timeline_ids.extend(
                timeline_id for timeline_id in duplicate.timeline_ids
                if timeline_id not in timeline_ids
            )
        bins[root_bin_id] = ProjectBin(
            bin_id=canonical.bin_id,
            name=canonical.name,
            parent_id=canonical.parent_id,
            asset_ids=tuple(asset_ids),
            timeline_ids=tuple(timeline_ids),
        )
        bins = {
            bin_id: (
                bin_obj.with_parent(root_bin_id)
                if bin_obj.parent_id in duplicate_root_ids
                else bin_obj
            )
            for bin_id, bin_obj in bins.items()
            if bin_id not in duplicate_root_ids
        }

    # active_comp_clip_id 从 full_data 获取，旧包回退到 project settings
    active_comp_clip_id = full_data.get("active_comp_clip_id") if full_data else None
    if not active_comp_clip_id:
        active_comp_clip_id = (topos_project.settings or {}).get("active_comp_clip_id") or None

    # Render-cache metadata lives in project.json settings so it survives the
    # unified .topos round trip without bundling large cache video files.
    project_settings = getattr(topos_project, "settings", {}) or {}
    render_cache_settings = (
        full_data.get("render_cache_settings") if full_data else None
    )
    if render_cache_settings is None:
        render_cache_settings = project_settings.get("render_cache_settings")
    render_cache_manifests = (
        full_data.get("render_cache_manifests", {}) if full_data else {}
    )
    if not render_cache_manifests:
        render_cache_manifests = project_settings.get("render_cache_manifests", {})
    proxy_policy = full_data.get("proxy_policy") if full_data else None
    if proxy_policy is None:
        proxy_policy = project_settings.get("proxy_policy")
    source_patch_overrides = _deserialize_source_patch_overrides(
        project_settings.get("source_patch_overrides"))
    color_groups = _deserialize_color_groups(
        project_settings.get("color_groups"))
    # N07：timeline 栈映射容错（非法形态→空表；栈 id 不在
    # color_stacks 里的登记保留但运行时查询返回空串语义由查询方兜底）
    raw_timeline = project_settings.get("timeline_grade_stacks")
    timeline_grade_stacks = {}
    if isinstance(raw_timeline, dict):
        timeline_grade_stacks = {
            str(k): str(v) for k, v in raw_timeline.items()
            if isinstance(k, str) and isinstance(v, str) and v}
    color_stack_versions = dict(
        project_settings.get("color_stack_versions") or {})
    interpretation_overrides = _deserialize_interpretation_overrides(
        project_settings.get("interpretation_overrides"))
    audio_sync_bindings = _deserialize_audio_sync_bindings(
        project_settings.get("audio_sync_bindings"))
    # M03.4：同步诊断（逐条形状校验，非法丢弃）
    from src.features.edit.core.sync_diagnostics import (
        sanitize_sync_diagnostics,
    )
    sync_diagnostics = sanitize_sync_diagnostics(
        project_settings.get("sync_diagnostics"))

    return EditProject(
        project_id=topos_project.project_id,
        name=topos_project.name,
        assets=assets,
        bins=bins,
        timelines=timelines,
        compositions=compositions,
        color_stacks=color_stacks,
        comp_clips=comp_clips,
        smart_bins=smart_bins,
        render_cache_manifests=render_cache_manifests or {},
        render_cache_settings=render_cache_settings,
        proxy_policy=proxy_policy,
        source_patch_overrides=source_patch_overrides,
        color_groups=color_groups,
        timeline_grade_stacks=timeline_grade_stacks,
        color_stack_versions=color_stack_versions,
        interpretation_overrides=interpretation_overrides,
        audio_sync_bindings=audio_sync_bindings,
        sync_diagnostics=sync_diagnostics,
        root_bin_id=root_bin_id,
        active_timeline_id=topos_project.active_timeline_id,
        active_comp_clip_id=active_comp_clip_id,
        created_at=topos_project.created_at,
        modified_at=topos_project.modified_at,
        revision=getattr(topos_project, "revision", 0),
        # M14.3b：工程级采样率从 settings 恢复（旧工程无键 = 0 → 未设置）
        project_sample_rate=int(
            (getattr(topos_project, "settings", None) or {}).get(
                "project_sample_rate", 0) or 0),
        # M02（S01）：快照表随工程包往返（settings.project_snapshots 由
        # ToposProjectIO 写入 project.json；旧工程无键 = 空表）
        project_snapshots=_deserialize_project_snapshots(
            (getattr(topos_project, "settings", None) or {}).get(
                "project_snapshots", {})),
    )


@dataclass
class EditProjectIOConfig:
    """
    编辑项目 IO 配置。

    Attributes:
        autosave_interval: 自动保存间隔（分钟），0 表示禁用
        autosave_to_temp: 是否保存到临时位置（True 保存到 .autosave，False 覆盖原文件）
        backup_on_save: 是否在保存前备份上一版
        max_backup_count: 最大备份文件数量
    """
    autosave_interval: int = DEFAULT_AUTOSAVE_INTERVAL
    autosave_to_temp: bool = True
    backup_on_save: bool = True
    max_backup_count: int = 3


class EditProjectIO(QObject):
    """
    编辑项目文件管理器。

    Features:
    - 保存/加载（带备份）
    - 自动保存（QTimer）
    - 备份机制（.topos.bak）
    - Recent files 管理
    - UI 状态保存/恢复

    Signals:
        project_saved: 项目保存成功
        project_loaded: 项目加载成功
        autosave_completed: 自动保存完成
        autosave_failed: 自动保存失败
        manual_save_failed: 手动后台保存失败（AUD-16715；filepath, error）
    """

    # 信号
    project_saved = pyqtSignal(str)  # filepath
    project_loaded = pyqtSignal(str)  # filepath
    autosave_completed = pyqtSignal(str)  # filepath
    autosave_failed = pyqtSignal(str)  # error message
    manual_save_failed = pyqtSignal(str, str)  # filepath, error (AUD-16715)

    def __init__(
        self,
        config: EditProjectIOConfig | None = None,
        parent: QObject | None = None,
    ) -> None:
        """
        初始化编辑项目 IO 管理器。

        Args:
            config: IO 配置（可选）
            parent: Qt 父对象
        """
        super().__init__(parent)

        self.config = config or EditProjectIOConfig()

        # 自动保存定时器
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._on_autosave_timeout)

        # 当前项目状态
        self._current_project: EditProject | None = None
        self._current_filepath: str | None = None
        self._current_ui_state: UIState | None = None
        self._current_timeline_sequences: dict[str, TimelineSequence] | None = None
        # DESK-03A：来源包载荷快照。load 时捕获；另存/复制/自动保存时
        # 作为保留基底随保存携带（目标已有条目优先）。同路径保存不参与。
        self._current_backing: ArchiveBackingStore | None = None
        self._last_generator_preflight: Any = None
        # AUD-16716：后台保存线程（autosave / 手动保存 worker）会写回该
        # 诊断状态，UI 线程经 get_last_generator_preflight() 读取——读写
        # 统一走这把锁，避免跨线程撕裂/丢失更新。
        self._generator_preflight_lock = threading.Lock()
        self._last_consistency_diagnostics: tuple[Any, ...] = ()
        # P2-13: 最近一次加载应用的内容 schema 迁移日志（空 = 无迁移）
        self._last_content_migrations: tuple[str, ...] = ()
        # DESK-04B：最近一次加载的包内容能力分级（E/P/B/X）
        self._last_capability_report: Any = None

        # UI 状态获取回调（由 MainWindow 设置）
        self._get_ui_state_callback: Optional[callable] = None
        self._get_timeline_sequences_callback: Optional[callable] = None
        self._get_project_callback: Optional[callable] = None
        # AI payload 获取回调（会话、plan、job、analysis 仓库，工程保存时调用）
        self._get_ai_payload_callback: Optional[callable] = None
        # AI payload 投递回调（工程加载完成时把 AI 仓库快照投回）
        self._on_ai_payload_loaded_callback: Optional[callable] = None
        self._get_teaching_animation_payload_callback: Optional[callable] = None
        self._on_teaching_animation_payload_loaded_callback: Optional[callable] = None
        self._get_ai_workflow_payload_callback: Optional[callable] = None
        self._on_ai_workflow_payload_loaded_callback: Optional[callable] = None

        # 自动保存路径（临时位置）
        self._autosave_filepath: str | None = None
        self._autosave_in_progress = False
        self._autosave_pending = False
        self._autosave_workers: set[_AsyncSaveWorker] = set()
        # AUD-16715：手动保存（save_project_as / quick_save）后台化的
        # 在飞 worker 集合。与 autosave 共用同一个私有单线程池。
        self._manual_save_workers: set[_AsyncSaveWorker] = set()
        # Recovery saves must not share the application's global worker pool.
        # During shutdown, waiting on that pool can time out behind unrelated
        # render/export work; its delayed autosave can then atomically recreate
        # the recovery file after cleanup has deleted it.
        self._autosave_pool = QThreadPool(self)
        self._autosave_pool.setMaxThreadCount(1)
        self._shutting_down = False

        # 启动自动保存定时器（如果配置启用）
        self._start_autosave_timer()

        logger.info("EditProjectIO initialized")

    # ==================== 配置 ====================

    def set_config(self, config: EditProjectIOConfig) -> None:
        """
        设置 IO 配置。

        Args:
            config: 新配置
        """
        self.config = config
        self._start_autosave_timer()

    def set_autosave_interval(self, minutes: int) -> None:
        """
        设置自动保存间隔。

        Args:
            minutes: 间隔分钟数，0 表示禁用
        """
        self.config.autosave_interval = minutes
        self._start_autosave_timer()

    def _start_autosave_timer(self) -> None:
        """启动或停止自动保存定时器"""
        if self.config.autosave_interval > 0:
            interval_ms = self.config.autosave_interval * 60 * 1000
            self._autosave_timer.start(interval_ms)
            logger.debug(
                f"Autosave timer started: {self.config.autosave_interval} minutes"
            )
        else:
            self._autosave_timer.stop()
            logger.debug("Autosave timer stopped")

    # ==================== 回调设置 ====================

    def set_callbacks(
        self,
        get_project: Optional[callable] = None,
        get_ui_state: Optional[callable] = None,
        get_timeline_sequences: Optional[callable] = None,
        get_ai_payload: Optional[callable] = None,
        on_ai_payload_loaded: Optional[callable] = None,
        get_teaching_animation_payload: Optional[callable] = None,
        on_teaching_animation_payload_loaded: Optional[callable] = None,
        get_ai_workflow_payload: Optional[callable] = None,
        on_ai_workflow_payload_loaded: Optional[callable] = None,
    ) -> None:
        """
        设置状态获取回调。

        Args:
            get_project: 获取当前 EditProject 的回调
            get_ui_state: 获取当前 UIState 的回调
            get_timeline_sequences: 获取当前 TimelineSequence 字典的回调
            get_ai_payload: 获取 AI 持久化 payload 的回调
                (``{"session": dict|None, "plans": dict|None, "jobs": dict|None,
                "analysis": dict|None}``)；工程保存时
                调用，返回 None 跳过 AI 模块。
            on_ai_payload_loaded: 工程加载完成后的 AI payload 投递回调
                (``{"session_catalog": dict|None, "session": dict|None,
                "plans": dict|None, "jobs": dict|None, "analysis": dict|None}``)；用于把磁盘上的 AI 会话/plan/job/analysis 还原进
                ``AssistantSessionStore`` / ``PlanRepository`` / ``AnalysisService``。
        """
        if get_project is not None:
            self._get_project_callback = get_project
        if get_ui_state is not None:
            self._get_ui_state_callback = get_ui_state
        if get_timeline_sequences is not None:
            self._get_timeline_sequences_callback = get_timeline_sequences
        if get_ai_payload is not None:
            self._get_ai_payload_callback = get_ai_payload
        if on_ai_payload_loaded is not None:
            self._on_ai_payload_loaded_callback = on_ai_payload_loaded
        if get_teaching_animation_payload is not None:
            self._get_teaching_animation_payload_callback = get_teaching_animation_payload
        if on_teaching_animation_payload_loaded is not None:
            self._on_teaching_animation_payload_loaded_callback = on_teaching_animation_payload_loaded
        if get_ai_workflow_payload is not None:
            self._get_ai_workflow_payload_callback = get_ai_workflow_payload
        if on_ai_workflow_payload_loaded is not None:
            self._on_ai_workflow_payload_loaded_callback = on_ai_workflow_payload_loaded

    # ==================== 保存 ====================

    def save_project(
        self,
        filepath: str | Path,
        project: EditProject,
        ui_state: UIState | None = None,
        timeline_sequences: dict[str, TimelineSequence] | None = None,
        create_backup: bool = True,
        autosave: bool = False,
        use_ai_callback: bool = True,
        ai_session_data: dict[str, Any] | None = None,
        ai_plans_data: dict[str, Any] | None = None,
        ai_jobs_data: dict[str, Any] | None = None,
        ai_analysis_data: dict[str, Any] | None = None,
        ai_runtime_data: dict[str, Any] | None = None,
        teaching_animation_data: dict[str, Any] | None = None,
        force_project_mode: str | None = None,
        ai_trace_data: dict[str, Any] | None = None,
        ai_plugins_data: dict[str, Any] | None = None,
        ai_workflows_data: dict[str, Any] | None = None,
        resources_data: dict[str, Any] | None = None,
        resource_files: dict[str, str] | None = None,
        backing_store: ArchiveBackingStore | None = None,
    ) -> bool:
        """
        保存项目到文件。

        使用 ToposProjectIO 保存到 .topos ZIP 包格式。

        Args:
            filepath: 文件路径
            project: EditProject 实例
            ui_state: UI 状态（可选）
            timeline_sequences: TimelineSequence 字典（可选）
            create_backup: 是否创建备份
            autosave: 是否写入恢复用的临时工程。自动保存不会改变当前工程路径，
                也不会清理刚写入的 autosave 文件。
            use_ai_callback: 是否在保存线程中读取 AI 状态回调。后台自动保存会
                在主线程先捕获 AI 快照，因此传入 False。
            ai_session_data: AI 会话目录数据（可选；未传时尝试 ``get_ai_payload`` 回调）
            ai_plans_data: AI plan 仓库数据（可选；未传时尝试回调）
            ai_jobs_data: JobRegistry 快照（可选；未传时尝试回调）
            ai_analysis_data: 素材分析证据缓存（可选；未传时尝试回调）
            ai_runtime_data: HarnessRuntime 快照（可选；活动 run 恢复时中断）
            ai_trace_data: AI 运行轨迹（可选；脱敏、有界，保存到 ai/trace.json）
            ai_plugins_data: AI 插件事实与 checkpoint journal（可选；保存到
                ai/plugins.json，不恢复 active 或 approval）
            force_project_mode: 显式指定 project_mode。新建的空视频项目
                (timelines 为空)应传 "edit_project",否则会被错误推断为
                standalone_image 导致下次打开弹模式不匹配警告。
            create_backup: 是否创建备份
            ai_session_data: AI 会话目录数据（可选；未传时尝试 ``get_ai_payload`` 回调）
            ai_plans_data: AI plan 仓库数据（可选；未传时尝试回调）

        Returns:
            是否成功

        Note:
            统一使用 .topos ZIP 包格式。显式参数优先于 ``get_ai_payload`` 回调；
            两者都未提供则跳过 AI 模块（旧工程/非 AI 工作流不受影响）。
        """
        filepath = Path(filepath)

        # FX-H03: save is the only point that owns the complete timeline map.
        # Reclaim unreferenced generator assets conservatively here and block
        # corrupt clip/bin references before any backup or disk write.
        if timeline_sequences is not None:
            expected_timeline_ids = set(project.timelines)
            provided_timeline_ids = set(timeline_sequences)
            if provided_timeline_ids != expected_timeline_ids:
                logger.error(
                    "Project save blocked: timeline sequence map is incomplete "
                    "(expected=%s, provided=%s)",
                    sorted(expected_timeline_ids),
                    sorted(provided_timeline_ids),
                )
                return False

            from src.features.edit.core.generator_transaction_guard import (
                preflight_orphan_check,
                reclaim_orphan_generators,
            )

            sequences = list(timeline_sequences.values())
            initial_check = preflight_orphan_check(project, sequences)
            # AUD-16716：本方法可能运行在后台保存线程（autosave / 手动
            # 保存 worker），共享诊断状态的写回必须在锁内完成。
            with self._generator_preflight_lock:
                self._last_generator_preflight = initial_check
            if initial_check.missing_asset_ids:
                logger.error(
                    "Project save blocked: clips reference missing assets: %s",
                    sorted(initial_check.missing_asset_ids),
                )
                return False
            reclaim = reclaim_orphan_generators(project, sequences)
            project = reclaim.project
            final_check = preflight_orphan_check(project, sequences)
            with self._generator_preflight_lock:
                self._last_generator_preflight = final_check
            if final_check.missing_bin_entries:
                logger.error(
                    "Project save blocked: generator assets missing bin entries: %s",
                    sorted(final_check.missing_bin_entries),
                )
                return False

        # 确保扩展名正确（统一使用 .topos）
        is_recovery_path = autosave and filepath.name.lower().endswith(AUTOSAVE_EXTENSION)
        if filepath.suffix != EDIT_PROJECT_EXTENSION and not is_recovery_path:
            filepath = filepath.with_suffix(EDIT_PROJECT_EXTENSION)

        # 备份上一版（如果配置启用且文件存在）
        if create_backup and self.config.backup_on_save and filepath.exists():
            # AUD-16712：备份失败必须中止保存——磁盘满/权限异常时若仍
            # 用新数据覆盖唯一原文件，后续 ZIP 写入再失败会令用户失去
            # 上一版且无备份可恢复（restore_from_backup 无从谈起）。
            # 与 save_project 的 bool 错误契约一致：返回 False 交由调用
            # 方报错。autosave 路径 create_backup=False，不受影响。
            if not self._create_backup(filepath):
                logger.error(
                    "Project save aborted: backup creation failed for %s",
                    filepath,
                )
                return False

        # 转换 EditProject 为 ToposProject
        topos_project = convert_edit_project_to_topos_project(
            project, force_project_mode=force_project_mode
        )

        # 准备完整数据用于保存到子目录 JSON
        full_assets_data = {
            asset_id: EditProjectSerializer._asset_to_dict(asset)
            for asset_id, asset in project.assets.items()
        }
        full_bins_data = {
            bin_id: EditProjectSerializer._bin_to_dict(bin_obj)
            for bin_id, bin_obj in project.bins.items()
        }
        full_timelines_data = {
            timeline_id: EditProjectSerializer._timeline_ref_to_dict(timeline)
            for timeline_id, timeline in project.timelines.items()
        }

        # TimelineSequence 数据
        timeline_sequences_data = None
        if timeline_sequences:
            timeline_sequences_data = {
                seq_id: seq.to_dict()
                for seq_id, seq in timeline_sequences.items()
            }

        # Color stacks 数据
        full_color_stacks_data = {
            stack_id: stack.to_dict()
            for stack_id, stack in project.color_stacks.items()
        }

        # Compositions 数据 (P9.2)
        full_compositions_data = None
        if project.compositions:
            full_compositions_data = dict(project.compositions)

        # Comp Clips 数据：媒体池合成剪辑元数据必须随工程包持久化，
        # 否则重启后合成会“消失”。空 dict 会清除包内旧数据（全部删除场景）。
        comp_clips_data = dict(getattr(project, "comp_clips", {}) or {})

        # P1-12: 用户自建 Smart Bins 持久化。与 comp_clips 相同，空 dict
        # 也要写出，否则全部删除后重开工程会复活旧搜索。
        smart_bins_data = {
            bin_id: EditProjectSerializer._smart_bin_to_dict(smart_bin)
            for bin_id, smart_bin in getattr(project, "smart_bins", {}).items()
        }

        # UI 状态数据
        ui_state_data = None
        if ui_state:
            ui_state_data = ui_state.to_dict()

        # AI 持久化 payload：显式参数优先；未传时通过回调取（autosave/quick_save 走回调）。
        effective_ai_session = ai_session_data
        effective_ai_plans = ai_plans_data
        effective_ai_jobs = ai_jobs_data
        effective_ai_analysis = ai_analysis_data
        effective_ai_runtime = ai_runtime_data
        effective_ai_trace = ai_trace_data
        effective_ai_plugins = ai_plugins_data
        if (effective_ai_session is None and effective_ai_plans is None and effective_ai_jobs is None
                and effective_ai_analysis is None and effective_ai_runtime is None
                and effective_ai_trace is None and effective_ai_plugins is None
                and use_ai_callback
                and self._get_ai_payload_callback is not None):
            try:
                ai_payload = self._get_ai_payload_callback() or {}
            except Exception:  # noqa: BLE001
                logger.debug("get_ai_payload callback failed", exc_info=True)
                ai_payload = {}
            if effective_ai_session is None:
                effective_ai_session = ai_payload.get("session")
            if effective_ai_plans is None:
                effective_ai_plans = ai_payload.get("plans")
            if effective_ai_jobs is None:
                effective_ai_jobs = ai_payload.get("jobs")
            if effective_ai_analysis is None:
                effective_ai_analysis = ai_payload.get("analysis")
            if effective_ai_runtime is None:
                effective_ai_runtime = ai_payload.get("runtime")
            if effective_ai_trace is None:
                effective_ai_trace = ai_payload.get("trace")
            if effective_ai_plugins is None:
                effective_ai_plugins = ai_payload.get("plugins")

        effective_teaching_animation = teaching_animation_data
        if effective_teaching_animation is None and self._get_teaching_animation_payload_callback is not None:
            try:
                effective_teaching_animation = self._get_teaching_animation_payload_callback()
            except Exception:  # noqa: BLE001
                logger.debug("get_teaching_animation_payload callback failed", exc_info=True)

        effective_ai_workflows: dict[str, Any] | None = ai_workflows_data
        if effective_ai_workflows is None and self._get_ai_workflow_payload_callback is not None:
            try:
                effective_ai_workflows = self._get_ai_workflow_payload_callback()
            except Exception:  # noqa: BLE001
                logger.debug("get_ai_workflow_payload callback failed", exc_info=True)

        # DESK-03A：保留基底。显式参数优先；未传时用当前工程的来源
        # backing（新工程 None = 无基底，纯新建保存）。
        effective_backing = (
            backing_store if backing_store is not None
            else self._current_backing
        )

        # R02：LUT 资源会话并入保存载荷。显式参数优先；未传且会话有条目
        # （工程加载条目 + 会话导入）时合并索引，仅新导入文件需要写包。
        # 会话为空 → (None, None)：保持"无资源协议"保存语义（payload
        # Phase B 原样保留包内已有索引/资源）。失败受控：暂存缺失的源
        # 文件由 IO 原子写入路径整体失败，不产生半包/半条目。
        effective_resources_data = resources_data
        effective_resource_files = resource_files
        if effective_resources_data is None and effective_resource_files is None:
            try:
                from src.shared.color.lut_resource_registry import (
                    ensure_graph_lut_closure,
                    get_lut_resource_session,
                )
                session = get_lut_resource_session()
                # R09：先确保图引用闭包（旧绝对路径自动迁移为工程资源；
                # 缺失引用记 missing——不静默移除、不阻断保存）。
                graphs = [stack.color_graph for stack in
                          project.color_stacks.values()
                          if getattr(stack, "color_graph", None) is not None]
                closure = ensure_graph_lut_closure(graphs, session)
                for ref in closure["missing"]:
                    logger.warning(
                        "project save: LUT 引用缺失（保留可修复状态）: %s",
                        ref)
                self._last_resource_closure_report = closure
                effective_resources_data, effective_resource_files = \
                    session.save_payload()
                # R06-B：bake 结果会话并入同一资源载荷（R09 闭包同语义：
                # 缺失保留可修复状态、不静默移除、不阻断保存）。
                from src.features.color.core.color_bake_result_registry import (
                    ensure_graph_bake_closure, get_bake_result_session,
                )
                bake_session = get_bake_result_session()
                bake_closure = ensure_graph_bake_closure(graphs, bake_session)
                for ref in bake_closure["missing"]:
                    logger.warning(
                        "project save: bake 引用缺失（保留可修复状态）: %s",
                        ref)
                self._last_bake_closure_report = bake_closure
                bake_data, bake_files = bake_session.save_payload()
                if bake_data or bake_files:
                    # 索引形状 = ResourceIndex.to_dict（resources 为条目
                    # 列表）：base 缺席直接采用 bake 索引；存在则按
                    # resource_id 去重合并。
                    if effective_resources_data is None:
                        effective_resources_data = bake_data
                    else:
                        existing = {
                            (item or {}).get("resource_id")
                            for item in effective_resources_data.get(
                                "resources", ()) or ()}
                        merged = list(effective_resources_data.get(
                            "resources", ()) or ())
                        merged.extend(
                            item for item in
                            (bake_data or {}).get("resources", ()) or ()
                            if (item or {}).get("resource_id") not in existing)
                        effective_resources_data = {
                            **effective_resources_data, "resources": merged}
                    effective_resource_files = dict(
                        effective_resource_files or {})
                    effective_resource_files.update(bake_files or {})
            except Exception:  # noqa: BLE001 — 资源载荷失败不阻断保存
                logger.exception("lut resource save payload failed")
                effective_resources_data = None
                effective_resource_files = None

        # 使用 ToposProjectIO 保存
        topos_config = ToposProjectIOConfig(
            serialization_format="json",
            compress_level=6,
            relative_paths=True,
            include_cache_assets=False,
        )

        success = ToposProjectIO.save(
            topos_project,
            str(filepath),
            topos_config,
            full_assets_data=full_assets_data,
            full_bins_data=full_bins_data,
            full_timelines_data=full_timelines_data,
            full_color_stacks_data=full_color_stacks_data,
            full_compositions_data=full_compositions_data,
            comp_clips_data=comp_clips_data,
            smart_bins_data=smart_bins_data,
            timeline_sequences_data=timeline_sequences_data,
            ui_state_data=ui_state_data,
            ai_session_data=effective_ai_session,
            ai_plans_data=effective_ai_plans,
            ai_jobs_data=effective_ai_jobs,
            ai_analysis_data=effective_ai_analysis,
            ai_runtime_data=effective_ai_runtime,
            teaching_animation_data=effective_teaching_animation,
            ai_trace_data=effective_ai_trace,
            ai_plugins_data=effective_ai_plugins,
            ai_workflows_data=effective_ai_workflows,
            resources_data=effective_resources_data,
            resource_files=effective_resource_files,
            backing_store=effective_backing,
        )

        if success:
            if not autosave:
                self._current_project = project
                self._current_filepath = str(filepath)
                self._current_ui_state = ui_state
                self._current_timeline_sequences = timeline_sequences
                # DESK-03B：另存/复制到新路径后，来源基底换成新写出的包
                # （它现在持有完整状态）；同路径保存保持原 backing（目标
                # 本身即最新，stale 条目只会被目标内容压住，永不复活）。
                if (
                    self._current_backing is None
                    or not self._current_backing.is_same_target(str(filepath))
                ):
                    try:
                        self._current_backing = ArchiveBackingStore.capture(
                            str(filepath))
                    except ProjectArchiveReadError:
                        logger.warning(
                            "Failed to re-capture archive backing after "
                            "save to %s", filepath, exc_info=True)
                        self._current_backing = None

            if not autosave:
                self.project_saved.emit(str(filepath))
            logger.info(f"Project saved to .topos package: {filepath}")

            # 清理自动保存临时文件（即使它是在本次启动前创建的）。
            if not autosave:
                self._cleanup_autosave_file_for_path(filepath)

        return success

    def get_last_generator_preflight(self) -> Any:
        """读取最近一次保存的 generator preflight 诊断结果（线程安全）。

        AUD-16716：保存可能由后台 worker 执行，诊断状态的写回与读取
        统一经 ``_generator_preflight_lock`` 串行化。
        """
        with self._generator_preflight_lock:
            return self._last_generator_preflight

    def save_project_as(
        self,
        filepath: str | Path,
        project: EditProject,
        ui_state: UIState | None = None,
        timeline_sequences: dict[str, TimelineSequence] | None = None,
        force_project_mode: str | None = None,
    ) -> bool:
        """
        另存项目（不覆盖当前路径）。

        AUD-16715：手动保存复用 ``_AsyncSaveWorker`` 通道——调用线程（UI）
        只做状态回调读取与深拷贝快照，ZIP 打包 + 大 JSON 序列化在私有
        QThreadPool 中执行；成功后 ``project_saved`` 经 queued 信号回 UI，
        失败经 ``manual_save_failed`` 上报。返回值语义为"是否成功排队"。

        Args:
            filepath: 新文件路径
            project: EditProject 实例
            ui_state: UI 状态（可选）
            timeline_sequences: TimelineSequence 字典（可选）
            force_project_mode: 显式工程模式（可选）

        Returns:
            是否成功排队（False = 前置校验失败或已在关闭/保存流程中）
        """
        if not self._queue_manual_save(
            filepath=filepath,
            project=project,
            ui_state=ui_state,
            timeline_sequences=timeline_sequences,
            force_project_mode=force_project_mode,
        ):
            return False
        logger.debug("Save As queued in background: %s", filepath)
        return True

    def quick_save(
        self,
    ) -> bool:
        """
        快速保存（使用当前文件路径和回调获取的状态）。

        AUD-16715：与 save_project_as 相同，走后台保存通道；返回值语义
        为"是否成功排队"。

        Returns:
            是否成功排队

        Note:
            需要先设置回调才能使用此方法。
        """
        if self._current_filepath is None:
            logger.warning("Quick save failed: no current filepath")
            return False

        # 通过回调获取当前状态
        project = self._get_project_callback() if self._get_project_callback else None
        ui_state = self._get_ui_state_callback() if self._get_ui_state_callback else None
        timeline_sequences = (
            self._get_timeline_sequences_callback()
            if self._get_timeline_sequences_callback
            else None
        )

        if project is None:
            logger.warning("Quick save failed: no project from callback")
            return False

        if not self._queue_manual_save(
            filepath=self._current_filepath,
            project=project,
            ui_state=ui_state,
            timeline_sequences=timeline_sequences,
        ):
            return False
        logger.debug("Quick save queued in background: %s", self._current_filepath)
        return True

    def _queue_manual_save(
        self,
        *,
        filepath: str | Path,
        project: EditProject,
        ui_state: UIState | None,
        timeline_sequences: dict[str, TimelineSequence] | None,
        force_project_mode: str | None = None,
    ) -> bool:
        """AUD-16715：手动保存的后台化队列入口（调用线程 = UI 线程）。

        在调用线程完成全部状态捕获（深拷贝快照 + AI payload 回调），
        ZIP/JSON 写入交给 ``_AsyncSaveWorker``。``save_project`` 成功路径
        对 ``_current_*`` 状态的赋值发生在 worker 线程，但它们先于
        ``project_saved`` 信号发射完成，queued 信号建立 happens-before，
        UI 线程在信号处理后读到的一定是新值。
        """
        if self._shutting_down:
            logger.warning("Manual save skipped: shutdown is in progress")
            return False
        if project is None:
            logger.warning("Manual save skipped: no project")
            return False
        if self._manual_save_workers:
            logger.warning(
                "Manual save skipped: another manual save is in flight (%d)",
                len(self._manual_save_workers),
            )
            return False

        # 离开 UI 线程前捕获可变状态（与 autosave 相同的快照纪律）。
        try:
            project_snapshot = copy.deepcopy(project)
            ui_state_snapshot = copy.deepcopy(ui_state)
            timeline_snapshot = copy.deepcopy(timeline_sequences)
            ai_session_snapshot = None
            ai_plans_snapshot = None
            ai_jobs_snapshot = None
            ai_analysis_snapshot = None
            ai_runtime_snapshot = None
            ai_trace_snapshot = None
            ai_plugins_snapshot = None
            teaching_animation_snapshot = None
            if self._get_ai_payload_callback is not None:
                ai_payload = self._get_ai_payload_callback() or {}
                ai_session_snapshot = copy.deepcopy(ai_payload.get("session"))
                ai_plans_snapshot = copy.deepcopy(ai_payload.get("plans"))
                ai_jobs_snapshot = copy.deepcopy(ai_payload.get("jobs"))
                ai_analysis_snapshot = copy.deepcopy(ai_payload.get("analysis"))
                ai_runtime_snapshot = copy.deepcopy(ai_payload.get("runtime"))
                ai_trace_snapshot = copy.deepcopy(ai_payload.get("trace"))
                ai_plugins_snapshot = copy.deepcopy(ai_payload.get("plugins"))
            if self._get_teaching_animation_payload_callback is not None:
                teaching_animation_snapshot = copy.deepcopy(
                    self._get_teaching_animation_payload_callback()
                )
            ai_workflows_snapshot = None
            if self._get_ai_workflow_payload_callback is not None:
                ai_workflows_snapshot = copy.deepcopy(
                    self._get_ai_workflow_payload_callback()
                )
        except Exception as exc:  # noqa: BLE001 — 快照失败按保存失败上报
            logger.warning("Manual save snapshot failed: %s", exc)
            self.manual_save_failed.emit(
                str(filepath), f"Failed to capture save snapshot: {exc}"
            )
            return False

        worker = _AsyncSaveWorker(
            self,
            {
                "filepath": filepath,
                "project": project_snapshot,
                "ui_state": ui_state_snapshot,
                "timeline_sequences": timeline_snapshot,
                "create_backup": True,
                "autosave": False,
                "use_ai_callback": False,
                "force_project_mode": force_project_mode,
                "ai_session_data": ai_session_snapshot,
                "ai_plans_data": ai_plans_snapshot,
                "ai_jobs_data": ai_jobs_snapshot,
                "ai_analysis_data": ai_analysis_snapshot,
                "ai_runtime_data": ai_runtime_snapshot,
                "teaching_animation_data": teaching_animation_snapshot,
                "ai_trace_data": ai_trace_snapshot,
                "ai_plugins_data": ai_plugins_snapshot,
                "ai_workflows_data": ai_workflows_snapshot,
                # DESK-03B：另存/快照保存携带来源 backing（引用捕获于 UI
                # 线程；backing 对象在 capture 后不可变，跨线程只读安全）。
                "backing_store": self._current_backing,
            },
            str(filepath),
        )
        worker.signals.finished.connect(self._on_async_manual_save_finished)
        self._manual_save_workers.add(worker)
        self._autosave_pool.start(worker)
        return True

    def _on_async_manual_save_finished(
        self,
        success: bool,
        filepath: str,
        error: str,
        worker: _AsyncSaveWorker,
    ) -> None:
        """Handle background manual-save completion on the UI thread (AUD-16715)."""
        self._manual_save_workers.discard(worker)
        if success:
            logger.info("Manual save completed: %s", filepath)
            return
        message = error or f"Failed to save: {filepath}"
        logger.error("Manual save failed: %s", message)
        # project_saved 不发射；失败经专用信号（queued）回 UI。
        self.manual_save_failed.emit(filepath, message)

    # ==================== 加载 ====================

    def _migrate_content_payload(self, full_data: dict | None) -> dict | None:
        """P2-13: 按内容 schema 版本链迁移 full_data 载荷。

        版本取自 manifest（``edit_content_schema_version``，缺省 1 = 历史
        工程）；迁移日志留在 ``last_content_migrations`` 供 UI 展示。
        迁移失败/缺 manifest 时不阻断打开（退回原载荷）。
        """
        self._last_content_migrations = ()
        if not isinstance(full_data, dict):
            return full_data
        try:
            from src.features.edit.core.project_migration import (
                EDIT_CONTENT_VERSION_KEY,
                migrate_project_payload,
            )

            manifest_data = full_data.get("manifest")
            if isinstance(manifest_data, dict) and EDIT_CONTENT_VERSION_KEY not in full_data:
                full_data[EDIT_CONTENT_VERSION_KEY] = manifest_data.get(
                    EDIT_CONTENT_VERSION_KEY, 1)
            migrated, log = migrate_project_payload(full_data)
            if log:
                for entry in log:
                    logger.info("project migration: %s", entry)
                self._last_content_migrations = tuple(log)
            return migrated
        except Exception:  # noqa: BLE001 — 迁移框架异常不阻断加载
            logger.exception("content schema migration failed; loading raw")
            return full_data

    @property
    def last_content_migrations(self) -> tuple[str, ...]:
        """最近一次加载应用的内容迁移日志（空 = 无迁移/新工程）。"""
        return self._last_content_migrations

    def load_project(
        self,
        filepath: str | Path,
        include_ui_state: bool = True,
    ) -> Optional[
        tuple[EditProject, UIState | None, dict[str, TimelineSequence] | None]
    ]:
        """
        从文件加载项目。

        使用 ToposProjectIO 加载 .topos ZIP 包格式。
        同时兼容旧的 JSON 格式文件。

        Args:
            filepath: 文件路径
            include_ui_state: 是否包含 UI 状态

        Returns:
            元组：(EditProject, UIState 或 None, TimelineSequence 字典或 None)
            失败返回 None

        Note:
            不支持加载旧格式工程文件。
        """
        filepath = Path(filepath)

        if not filepath.exists():
            logger.warning(f"Project file not found: {filepath}")
            return None

        # 检查文件类型
        if ToposProjectIO.is_topos_file(str(filepath)):
            # 使用 ToposProjectIO 加载 .topos ZIP 包
            topos_config = ToposProjectIOConfig(
                serialization_format="json",
                relative_paths=True,
            )

            topos_project, full_data = ToposProjectIO.load(
                str(filepath),
                topos_config,
                load_full_data=True,
            )

            if topos_project is None:
                logger.warning(f"Failed to load .topos package: {filepath}")
                return None

            # AUD-16710：外部 .topos 包属不可信输入（损坏/手改）。迁移 →
            # EditProject 转换（MediaAsset rating=int、ColorStackData.from_dict
            # 等强转换）→ UIState/TimelineSequence 反序列化链上任何畸形载荷
            # （缺键/类型不符/非法值）都必须转为受控加载失败：记录完整
            # 异常栈并返回 None（调用方据此弹用户可见错误对话框），绝不
            # 让异常穿出到 Qt 菜单槽使整个应用崩溃。
            try:
                # P2-13: 编辑内容 schema 迁移（版本来自 manifest；载荷升级留日志）
                full_data = self._migrate_content_payload(full_data)

                # 转换为 EditProject
                project = convert_topos_project_to_edit_project(topos_project, full_data)

                # 从 full_data 获取 UI 状态
                ui_state = None
                if include_ui_state and full_data and "ui_state" in full_data:
                    ui_state_data = full_data.get("ui_state", {})
                    ui_state = UIState.from_dict(ui_state_data)

                # 从 full_data 获取 TimelineSequence
                timeline_sequences = None
                if full_data and "timeline_sequences" in full_data:
                    timeline_sequences_data = full_data.get("timeline_sequences", {})
                    timeline_sequences = {
                        seq_id: TimelineSequence.from_dict(seq_data)
                        for seq_id, seq_data in timeline_sequences_data.items()
                    }
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,  # 手改成非对象形状（list/str 无 .get）的载荷
            ) as exc:
                logger.exception(
                    "Failed to deserialize .topos package payload "
                    "(corrupt or hand-edited package): %s (%s: %s)",
                    filepath, type(exc).__name__, exc,
                )
                return None

            if timeline_sequences is not None:
                from src.features.edit.core.generator_transaction_guard import (
                    validate_project_asset_consistency,
                )
                diagnostics = validate_project_asset_consistency(
                    project,
                    list(timeline_sequences.values()),
                )
                self._last_consistency_diagnostics = tuple(diagnostics)
                for diagnostic in diagnostics:
                    logger.warning(
                        "Project consistency issue [%s] %s: %s",
                        diagnostic.issue,
                        diagnostic.asset_id,
                        diagnostic.detail,
                    )

            self._current_project = project
            self._current_filepath = str(filepath)
            self._current_ui_state = ui_state
            self._current_timeline_sequences = timeline_sequences
            # R07（媒体池复查）：子片段 source_end 域迁移——旧场景剪切
            # 子片段写成不含尾端点，读取按含尾解释会多 1 帧。加载时升级
            # 可证明条目（等式不变式，不盲减 1）；失败不阻断打开。
            try:
                from src.features.edit.core.asset_source_range import (
                    migrate_subclip_range_semantics,
                )
                project, migrated_subclips = \
                    migrate_subclip_range_semantics(project)
                self._current_project = project
                if migrated_subclips:
                    logger.info(
                        "loaded project: migrated %d legacy subclip "
                        "source_end semantics", migrated_subclips)
            except Exception:  # noqa: BLE001 — 迁移失败不阻断打开
                logger.exception("subclip range semantics migration failed")
            # DESK-02A：工程内嵌字体注册（按内容身份）。无预装字体的
            # 机器上字幕换行/锚点仍正确；注册诊断留日志供 UI 预检展示。
            # headless（无 QGuiApplication）时跳过注册并留诊断——渲染
            # 前仍可补注册（ProjectFontRegistry.from_resources 可重入）。
            resource_resolver_obj = (full_data or {}).get("resource_resolver")
            try:
                from src.features.edit.core.captions.font_resolver import (
                    FontResolver,
                    ProjectFontRegistry,
                )
                if resource_resolver_obj is not None:
                    registry = ProjectFontRegistry.from_resources(
                        resource_resolver_obj)
                    FontResolver.set_project_families(registry.families)
                    if registry.diagnostics:
                        for line in registry.diagnostics:
                            logger.warning("project font: %s", line)
                    elif registry.registrations:
                        logger.info("project fonts: %s", registry.summary())
                else:
                    # 无资源索引的工程：清掉上一工程的字体，防跨工程串注册
                    FontResolver.set_project_families(None)
            except Exception:  # noqa: BLE001 — 字体失败不阻断打开
                logger.exception(
                    "Project font registration failed; captions fall "
                    "back to system fonts")
            # R02：LUT 资源会话绑定工程（合并包内索引，清上一工程导入）。
            # 失败不阻断打开——浏览器目录回退到会话空集并保留诊断。
            try:
                from src.shared.color.lut_resource_registry import (
                    get_lut_resource_session,
                )
                get_lut_resource_session().bind_project(resource_resolver_obj)
            except Exception:  # noqa: BLE001
                logger.exception("lut resource session bind failed")
            # R06-B：bake 结果会话同语义绑定（合并包内 generated 条目）。
            try:
                from src.features.color.core.color_bake_result_registry import (
                    get_bake_result_session,
                )
                get_bake_result_session().bind_project(resource_resolver_obj)
            except Exception:  # noqa: BLE001
                logger.exception("bake result session bind failed")
            # DESK-04B：包内容能力分级（不阻断加载；导出门/UI 按报告限制）
            try:
                from src.features.edit.core.package_capability import (
                    grade_package_capabilities,
                )
                self._last_capability_report = grade_package_capabilities(
                    full_data=full_data,
                    timeline_sequences=(
                        timeline_sequences.values()
                        if timeline_sequences else None),
                    assets=project.assets,
                )
            except Exception:  # noqa: BLE001 — 分级失败不阻断打开
                logger.exception("capability grading failed")
                self._last_capability_report = None
            # DESK-03A：捕获来源包载荷快照（大资源为惰性引用，不驻内存）。
            # 不可读时降级为无 backing（load 刚成功说明包整体可读，此分支
            # 极罕见），另存退化为旧行为并留日志。
            try:
                self._current_backing = ArchiveBackingStore.capture(str(filepath))
            except ProjectArchiveReadError:
                logger.warning(
                    "Failed to capture archive backing for %s; save-as will "
                    "not preserve source-only modules", filepath, exc_info=True)
                self._current_backing = None

            # 投递 AI 持久化 payload（会话目录 + plan 仓库）给已注册的还原回调。
            # 无 AI payload（旧工程/非 AI 工作流）或无回调时静默跳过。
            if self._on_ai_payload_loaded_callback is not None:
                ai_payload = {
                    "session_catalog": (full_data or {}).get("ai_session_catalog"),
                    "session": (full_data or {}).get("ai_session"),
                    "plans": (full_data or {}).get("ai_plans"),
                    "jobs": (full_data or {}).get("ai_jobs"),
                    "analysis": (full_data or {}).get("ai_analysis"),
                    "runtime": (full_data or {}).get("ai_runtime"),
                    "trace": (full_data or {}).get("ai_trace"),
                    "plugins": (full_data or {}).get("ai_plugins"),
                }
                try:
                    self._on_ai_payload_loaded_callback(ai_payload)
                except Exception:  # noqa: BLE001
                    logger.debug("on_ai_payload_loaded callback failed", exc_info=True)

            if self._on_teaching_animation_payload_loaded_callback is not None:
                teaching_payload = (full_data or {}).get("teaching_animation")
                try:
                    # Deliver None as an explicit project boundary signal so
                    # a newly opened project cannot inherit scenes from the
                    # previously open project.
                    self._on_teaching_animation_payload_loaded_callback(teaching_payload)
                except Exception:  # noqa: BLE001
                    logger.debug("on_teaching_animation_payload_loaded callback failed", exc_info=True)

            if self._on_ai_workflow_payload_loaded_callback is not None:
                workflows_payload = (full_data or {}).get("ai_workflows")
                try:
                    # None 同样是显式工程边界信号：新工程不带上一工程的图。
                    self._on_ai_workflow_payload_loaded_callback(workflows_payload)
                except Exception:  # noqa: BLE001
                    logger.debug("on_ai_workflow_payload_loaded callback failed", exc_info=True)

            self.project_loaded.emit(str(filepath))
            logger.info(f"Project loaded from .topos package: {filepath}")

            return (project, ui_state, timeline_sequences)
        else:
            # 旧 JSON 格式已不再支持，需通过显式 migration 流程导入
            logger.error(f"Unsupported project format (not .topos ZIP): {filepath}")
            return None

    def get_current_project(self) -> EditProject | None:
        """获取当前加载的项目"""
        return self._current_project

    def get_current_filepath(self) -> str | None:
        """获取当前文件路径"""
        return self._current_filepath

    def set_current_filepath(self, filepath: str | Path | None) -> None:
        """Adopt a canonical project path after loading a recovery file."""
        self._current_filepath = str(filepath) if filepath else None

    def get_current_ui_state(self) -> UIState | None:
        """获取当前 UI 状态"""
        return self._current_ui_state

    def get_current_timeline_sequences(
        self
    ) -> dict[str, TimelineSequence] | None:
        """获取当前 TimelineSequence 字典"""
        return self._current_timeline_sequences

    def get_current_backing(self) -> ArchiveBackingStore | None:
        """获取当前工程的来源包载荷快照（DESK-03A；诊断/测试用）。"""
        return self._current_backing

    def get_last_capability_report(self) -> Any:
        """最近一次加载的包内容能力分级（DESK-04B；None = 未加载/失败）。"""
        return self._last_capability_report

    # ==================== 自动保存 ====================

    def _on_autosave_timeout(self) -> bool:
        """Capture current state on the UI thread and write it in background."""
        if self._shutting_down:
            logger.debug("Autosave skipped: shutdown is in progress")
            return False
        if self._autosave_in_progress:
            self._autosave_pending = True
            logger.debug("Autosave already running; latest request queued")
            return False

        # 通过回调获取当前状态
        project = self._get_project_callback() if self._get_project_callback else None
        ui_state = self._get_ui_state_callback() if self._get_ui_state_callback else None
        timeline_sequences = (
            self._get_timeline_sequences_callback()
            if self._get_timeline_sequences_callback
            else None
        )

        if project is None:
            logger.debug("Autosave skipped: no project")
            return False

        # 决定保存位置
        if self.config.autosave_to_temp:
            # 保存到临时位置
            if self._current_filepath:
                source_filepath = self._current_filepath
                if source_filepath.lower().endswith(AUTOSAVE_EXTENSION):
                    source_filepath = source_filepath[: -len(AUTOSAVE_EXTENSION)]
                filepath = source_filepath + AUTOSAVE_EXTENSION
            else:
                # 没有当前路径，跳过自动保存
                logger.debug("Autosave skipped: no filepath for temp save")
                return False
        else:
            # 覆盖原文件
            if self._current_filepath:
                filepath = self._current_filepath
            else:
                logger.debug("Autosave skipped: no filepath")
                return False

        # Capture mutable state before leaving the UI thread. Timeline and
        # AI payloads can be changed by UI callbacks while a worker writes.
        try:
            project_snapshot = copy.deepcopy(project)
            ui_state_snapshot = copy.deepcopy(ui_state)
            timeline_snapshot = copy.deepcopy(timeline_sequences)
            ai_session_snapshot = None
            ai_plans_snapshot = None
            ai_jobs_snapshot = None
            ai_analysis_snapshot = None
            ai_runtime_snapshot = None
            ai_trace_snapshot = None
            ai_plugins_snapshot = None
            teaching_animation_snapshot = None
            if self._get_ai_payload_callback is not None:
                ai_payload = self._get_ai_payload_callback() or {}
                ai_session_snapshot = copy.deepcopy(ai_payload.get("session"))
                ai_plans_snapshot = copy.deepcopy(ai_payload.get("plans"))
                ai_jobs_snapshot = copy.deepcopy(ai_payload.get("jobs"))
                ai_analysis_snapshot = copy.deepcopy(ai_payload.get("analysis"))
                ai_runtime_snapshot = copy.deepcopy(ai_payload.get("runtime"))
                ai_trace_snapshot = copy.deepcopy(ai_payload.get("trace"))
                ai_plugins_snapshot = copy.deepcopy(ai_payload.get("plugins"))
            if self._get_teaching_animation_payload_callback is not None:
                teaching_animation_snapshot = copy.deepcopy(
                    self._get_teaching_animation_payload_callback()
                )
            ai_workflows_snapshot = None
            if self._get_ai_workflow_payload_callback is not None:
                ai_workflows_snapshot = copy.deepcopy(
                    self._get_ai_workflow_payload_callback()
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Autosave snapshot failed; keeping UI responsive: %s", exc)
            self.autosave_failed.emit(f"Failed to capture autosave snapshot: {exc}")
            return False

        worker = _AsyncSaveWorker(
            self,
            {
                "filepath": filepath,
                "project": project_snapshot,
                "ui_state": ui_state_snapshot,
                "timeline_sequences": timeline_snapshot,
                "create_backup": False,
                "autosave": True,
                "use_ai_callback": False,
                "ai_session_data": ai_session_snapshot,
                "ai_plans_data": ai_plans_snapshot,
                "ai_jobs_data": ai_jobs_snapshot,
                "ai_analysis_data": ai_analysis_snapshot,
                "ai_runtime_data": ai_runtime_snapshot,
                "teaching_animation_data": teaching_animation_snapshot,
                "ai_trace_data": ai_trace_snapshot,
                "ai_plugins_data": ai_plugins_snapshot,
                "ai_workflows_data": ai_workflows_snapshot,
                # DESK-03B：自动保存（.autosave 目标 ≠ 来源）携带 backing，
                # 恢复文件与正式包持有同等的未知模块/资源。
                "backing_store": self._current_backing,
            },
            filepath,
        )
        worker.signals.finished.connect(self._on_async_autosave_finished)
        self._autosave_workers.add(worker)
        self._autosave_in_progress = True
        self._autosave_pool.start(worker)
        logger.debug("Autosave queued in background: %s", filepath)
        return True

    def _on_async_autosave_finished(
        self,
        success: bool,
        filepath: str,
        error: str,
        worker: _AsyncSaveWorker,
    ) -> None:
        """Handle background autosave completion on the UI thread."""
        self._autosave_workers.discard(worker)
        if self._shutting_down:
            # cleanup() has waited for this private pool and removed the
            # recovery file. Do not resurrect state or schedule another save
            # while Qt is tearing down the window.
            self._autosave_in_progress = bool(self._autosave_workers)
            return
        self._autosave_in_progress = False
        if success:
            self._autosave_filepath = filepath
            self.autosave_completed.emit(filepath)
            logger.info("Autosave completed: %s", filepath)
        else:
            self.autosave_failed.emit(error or f"Failed to save: {filepath}")
            logger.error("Autosave failed: %s", error or filepath)

        if self._autosave_pending:
            self._autosave_pending = False
            QTimer.singleShot(0, self._on_autosave_timeout)

    def _cleanup_autosave(self) -> None:
        """清理自动保存临时文件"""
        if self._autosave_filepath:
            self.discard_autosave(self._autosave_filepath)
        self._autosave_filepath = None

    def discard_autosave(self, filepath: str | Path) -> None:
        """Discard the recovery file associated with a canonical project."""
        project_path = Path(filepath)
        if project_path.name.lower().endswith(AUTOSAVE_EXTENSION):
            project_path = Path(str(project_path)[: -len(AUTOSAVE_EXTENSION)])
        autosave_path = project_path.with_suffix(project_path.suffix + AUTOSAVE_EXTENSION)
        try:
            if autosave_path.exists():
                autosave_path.unlink()
                logger.debug("Autosave discarded: %s", autosave_path)
        except OSError as exc:
            logger.warning("Failed to discard autosave %s: %s", autosave_path, exc)
        if self._autosave_filepath and os.path.abspath(self._autosave_filepath) == os.path.abspath(str(autosave_path)):
            self._autosave_filepath = None

    def _cleanup_autosave_file_for_path(self, filepath: str | Path) -> None:
        """Clean an autosave even when it was created before this app run."""
        self.discard_autosave(filepath)

    def force_autosave(self) -> bool:
        """
        强制执行一次自动保存。

        Returns:
            是否成功
        """
        return bool(self._on_autosave_timeout())

    # ==================== 备份 ====================

    def _create_backup(self, filepath: Path) -> bool:
        """
        创建备份文件。

        Args:
            filepath: 原文件路径

        Returns:
            是否成功
        """
        backup_path = filepath.with_suffix(filepath.suffix + BACKUP_EXTENSION)

        try:
            # 复制文件到备份位置
            shutil.copy2(filepath, backup_path)
            logger.info(f"Backup created: {backup_path}")

            # 清理旧备份（超过最大数量）
            self._cleanup_old_backups(filepath)

            return True
        except Exception as e:
            logger.error(f"Failed to create backup: {e}")
            return False

    def _cleanup_old_backups(self, filepath: Path) -> None:
        """
        清理旧备份文件。

        Args:
            filepath: 原文件路径
        """
        if self.config.max_backup_count <= 0:
            return

        # AUD-16711：按精确文件名匹配备份。旧实现用 startswith(stem)
        # 前缀匹配——工程 "demo.topos" 会把 "demo2.topos.bak" /
        # "demo_reel.topos.bak" 等其它工程的备份一并计入并按 mtime 淘汰
        # 删除。_create_backup 只写单一固定名 <name>.topos.bak，精确匹配
        # 即本工程的全部备份。
        backup_name = filepath.name + BACKUP_EXTENSION
        parent_dir = filepath.parent
        backups = sorted(
            [
                f
                for f in parent_dir.iterdir()
                if f.name == backup_name
            ],
            # stat 可能因并发删除/坏符号链接抛 OSError——逐文件容错，
            # 无法取 mtime 的文件排最后（最不可能被淘汰）。
            key=lambda f: _safe_mtime_or_max(f),
        )

        # 删除超出数量的备份
        while len(backups) > self.config.max_backup_count:
            old_backup = backups.pop(0)
            try:
                old_backup.unlink()
                logger.debug(f"Old backup removed: {old_backup}")
            except OSError as e:
                logger.warning(f"Failed to remove old backup: {e}")

    def restore_from_backup(self, filepath: str | Path) -> bool:
        """
        从备份恢复文件。

        Args:
            filepath: 原文件路径

        Returns:
            是否成功
        """
        filepath = Path(filepath)
        backup_path = filepath.with_suffix(filepath.suffix + BACKUP_EXTENSION)

        if not backup_path.exists():
            logger.warning(f"Backup file not found: {backup_path}")
            return False

        try:
            shutil.copy2(backup_path, filepath)
            logger.info(f"Restored from backup: {filepath}")
            return True
        except Exception as e:
            logger.error(f"Failed to restore from backup: {e}")
            return False

    # ==================== Recent Files ====================

    def add_to_recent_files(
        self,
        filepath: str | Path,
        recent_files: list[str],
        max_count: int = 10,
    ) -> list[str]:
        """
        添加文件到最近文件列表。

        Args:
            filepath: 文件路径
            recent_files: 当前最近文件列表
            max_count: 最大数量

        Returns:
            更新后的最近文件列表
        """
        filepath_str = str(filepath)

        # 移除已存在的条目（避免重复）
        if filepath_str in recent_files:
            recent_files.remove(filepath_str)

        # 添加到开头
        recent_files.insert(0, filepath_str)

        # 限制数量
        if len(recent_files) > max_count:
            recent_files = recent_files[:max_count]

        return recent_files

    # ==================== 文件检查 ====================

    def is_edit_project_file(self, filepath: str | Path) -> bool:
        """
        判断文件是否为编辑项目文件。

        Args:
            filepath: 文件路径

        Returns:
            是否为编辑项目文件
        """
        filepath = Path(filepath)

        # 检查扩展名
        if filepath.suffix != EDIT_PROJECT_EXTENSION:
            return False

        # 检查文件内容
        return EditProjectSerializer.is_edit_project_file(filepath)

    def has_autosave(self, filepath: str | Path) -> bool:
        """
        检查是否存在自动保存文件。

        Args:
            filepath: 原文件路径

        Returns:
            是否存在自动保存文件
        """
        return self.get_autosave_filepath(filepath) is not None

    def get_autosave_filepath(self, filepath: str | Path) -> str | None:
        """
        获取自动保存文件路径。

        Args:
            filepath: 原文件路径

        Returns:
            自动保存文件路径（不存在返回 None）
        """
        filepath = Path(filepath)
        if filepath.name.lower().endswith(AUTOSAVE_EXTENSION):
            filepath = Path(str(filepath)[: -len(AUTOSAVE_EXTENSION)])
        autosave_path = filepath.with_suffix(filepath.suffix + AUTOSAVE_EXTENSION)
        if not autosave_path.exists():
            return None
        # A recovery file is useful only when it is newer than the canonical
        # project. Older files are stale and should not trigger a dialog.
        try:
            if filepath.exists() and autosave_path.stat().st_mtime <= filepath.stat().st_mtime:
                self.discard_autosave(filepath)
                return None
        except OSError:
            return None
        return str(autosave_path)

    # ==================== 清理 ====================

    def cleanup(self) -> None:
        """Stop autosaving and remove recovery data after a clean shutdown."""
        self._shutting_down = True
        self._autosave_pending = False
        try:
            if self._autosave_timer is not None:
                self._autosave_timer.stop()
        except RuntimeError:
            # QTimer 已被删除，忽略
            pass
        # Wait for *only our* recovery writes before deleting their output.
        # ToposProjectIO saves through os.replace(), so deleting after a
        # timeout is unsafe: a worker that completes later recreates the
        # .autosave file and triggers a false recovery prompt next launch.
        # AUD-16718：等待必须有上限——慢速/挂起的磁盘 IO 不允许把应用
        # 退出流程无限阻塞。超时后放弃等待（worker 为 daemon 池线程，
        # 最坏后果是上面注释所述的迟到 .autosave 复活，可接受）。
        if self._autosave_workers or self._manual_save_workers:
            if not self._autosave_pool.waitForDone(5000):
                logger.warning(
                    "EditProjectIO.cleanup: save pool did not finish within 5s "
                    "(%d autosave / %d manual save worker(s) still running)",
                    len(self._autosave_workers),
                    len(self._manual_save_workers),
                )
            self._autosave_workers.clear()
            self._manual_save_workers.clear()
        self._autosave_in_progress = False
        if self._current_filepath:
            self.discard_autosave(self._current_filepath)
        self._cleanup_autosave()

    def __del__(self) -> None:
        """析构函数"""
        try:
            self.cleanup()
        except RuntimeError:
            # 对象已被删除，忽略
            pass
