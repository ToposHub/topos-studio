"""DESK-03D/07B · 交接打包（handoff）——与工作保存区分的单文件自包含导出。

与"工作保存"的区别（冻结）：
- 工作保存（``ToposProjectIO.save`` / ``EditProjectIO.save_project``）：
  面向本机继续编辑，外部路径素材照旧外置；
- 交接打包（本模块）：面向"拷给另一台机器/手机即可打开"，把**渲染
  必需**的外部素材内嵌进包（内容寻址 resources/），并携带来源包全部
  未知模块（DESK-03A backing）。

流程：生产 load → 依赖闭包审计（DESK-02C）→ blocked 拒绝打包（不产
出会误导"可完整精剪"的包）→ 外部在线素材内嵌（资源索引+流式
file_members）→ 生产 save（含 backing/取消检查/原子晋升）→ 复核
（重开审计 full）→ 摘要返回。

字体/LUT 等非素材资源：调用方（UI/07B）经 *extra_resources* 显式添加
（kind=font/lut/...）；自动发现系统字体文件不在本层职责内。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .archive_backing import ArchiveBackingStore
from .dependency_audit import (
    collect_dependencies,
    packaging_summary,
    portability_preflight,
)
from .resource_resolver import ResourceIndex, make_resource_entry
from .topos_project_io import ToposProjectIO

logger = logging.getLogger(__name__)

__all__ = ["HandoffResource", "HandoffResult", "export_handoff_package"]


@dataclass(frozen=True)
class HandoffResource:
    """调用方显式追加的内嵌资源（字体/LUT/贴图等）。"""

    source_file: str
    kind: str          # font / lut / texture / mask / generated / other
    resource_id: str
    roles: tuple[str, ...] = ()
    asset_id: str | None = None
    original_filename: str | None = None
    mime: str = ""


@dataclass(frozen=True)
class HandoffResult:
    """交接打包结果（UI/日志消费）。"""

    success: bool
    dest_path: str
    verdict: str                     # full / partial（blocked 时不产出包）
    summary: str = ""
    embedded_asset_ids: tuple[str, ...] = ()
    problems: tuple[dict[str, Any], ...] = ()
    detail: str = ""


def export_handoff_package(
    source_path: str,
    dest_path: str,
    *,
    extra_resources: Iterable[HandoffResource] = (),
    should_cancel: Callable[[], bool] | None = None,
) -> HandoffResult:
    """把 *source_path* 工程导出为自包含交接包 *dest_path*。

    - 渲染必需素材（外部在线）内嵌；已内嵌素材照旧（幂等）；
    - 依赖审计 blocked（悬空引用/必需素材缺失/哈希不符）→ 拒绝打包；
    - 来源未知模块/资源随行（backing）；
    - 全程可取消；失败/取消不残留目标与临时文件。
    """
    def _cancel() -> bool:
        return should_cancel is not None and should_cancel()

    if _cancel():
        return HandoffResult(
            False, dest_path, "partial", detail="打包前已取消")

    project, full_data = ToposProjectIO.load(source_path)
    if project is None or full_data is None:
        return HandoffResult(
            False, dest_path, "blocked",
            detail=f"来源工程无法加载: {source_path}")
    if _cancel():
        return HandoffResult(False, dest_path, "partial", detail="已取消")

    from src.features.edit.core.edit_project_io import (
        convert_topos_project_to_edit_project,
    )
    sequences_map = full_data.get("timeline_sequences") or {}
    edit_project = convert_topos_project_to_edit_project(project, full_data)

    report = collect_dependencies(
        assets=full_data.get("assets") or {},
        full_data=full_data,
        timeline_sequences=sequences_map.values(),
    )
    verdict, summary, problems = portability_preflight(report)
    if verdict == "blocked":
        return HandoffResult(
            False, dest_path, verdict, summary=summary,
            problems=tuple(packaging_summary(report)["issues"]),
            detail="依赖审计未通过（blocked）——不产出会误导的交接包；"
                   "请先修复问题清单")

    # ---- 内嵌外部在线素材（内容寻址） ----
    # 已有内嵌资源随 backing 流式携带（来源包 preserved members），此处
    # 只需重建索引 JSON；新内嵌素材走 file_members。
    from .resource_resolver import ResourceEntry
    resources = ResourceIndex(entries={
        rid: ResourceEntry.from_dict(payload)
        for rid, payload in _index_entries(full_data).items()
    })
    resource_files: dict[str, str] = {}
    embedded: list[str] = []
    assets_data = dict(full_data.get("assets") or {})
    for asset_id, asset in assets_data.items():
        if not isinstance(asset, dict):
            continue
        if asset.get("resource_uri"):
            embedded.append(str(asset_id))
            continue
        path = asset.get("path") or ""
        if not path or not os.path.isfile(path):
            continue
        media_type = str(asset.get("media_type", "other"))
        kind = {"video": "video", "audio": "audio", "image": "image"}.get(
            media_type, "other")
        entry = make_resource_entry(
            path,
            resource_id=f"res-{asset_id}",
            kind=kind,
            roles=("source_media",),
            asset_id=str(asset_id),
        )
        resources.entries[entry.resource_id] = entry
        resource_files[entry.archive_path] = path
        # 权威引用换成 URI；外部路径降级为提示（DESK-03C 形状）
        asset["resource_uri"] = entry.uri
        asset.setdefault("path_original", path)
        embedded.append(str(asset_id))

    # ---- 调用方显式资源（字体/LUT…） ----
    for res in extra_resources:
        entry = make_resource_entry(
            res.source_file, resource_id=res.resource_id, kind=res.kind,
            roles=res.roles or (res.kind,), asset_id=res.asset_id,
            original_filename=res.original_filename, mime=res.mime)
        resources.entries[entry.resource_id] = entry
        resource_files[entry.archive_path] = res.source_file

    if _cancel():
        return HandoffResult(
            False, dest_path, "partial", summary=summary, detail="已取消")

    # ---- 生产 save：backing 随行 + 原子晋升 + 取消检查 ----
    from src.features.edit.core.serialization import EditProjectSerializer
    backing = ArchiveBackingStore.capture(source_path)
    full_assets_data = {
        asset_id: EditProjectSerializer._asset_to_dict(asset)
        for asset_id, asset in edit_project.assets.items()
    }
    # 覆盖为交接形态（resource_uri + 外部提示路径）
    for asset_id, payload in full_assets_data.items():
        raw = assets_data.get(asset_id)
        if isinstance(raw, dict) and raw.get("resource_uri"):
            payload["resource_uri"] = raw["resource_uri"]
            payload["path_original"] = raw.get(
                "path_original", payload.get("path", ""))
            payload["path"] = payload["path_original"]

    success = ToposProjectIO.save(
        project, dest_path,
        full_assets_data=full_assets_data,
        full_bins_data={
            bin_id: EditProjectSerializer._bin_to_dict(b)
            for bin_id, b in edit_project.bins.items()
        },
        full_timelines_data={
            timeline_id: EditProjectSerializer._timeline_ref_to_dict(t)
            for timeline_id, t in edit_project.timelines.items()
        },
        timeline_sequences_data=dict(sequences_map),
        resources_data=resources.to_dict(),
        resource_files=resource_files,
        backing_store=backing,
        should_cancel=should_cancel,
    )
    if not success:
        return HandoffResult(
            False, dest_path, verdict, summary=summary,
            detail="保存失败（取消/磁盘/来源不可读）；目标未改动")

    # ---- 复核：重开 + 审计 full ----
    verify_project, verify_data = ToposProjectIO.load(dest_path)
    if verify_project is None:
        return HandoffResult(
            False, dest_path, verdict, summary=summary,
            detail="交接包复核加载失败（不冒充成功）")
    verify_report = collect_dependencies(
        assets=verify_data.get("assets") or {},
        full_data=verify_data,
        timeline_sequences=(verify_data.get("timeline_sequences")
                            or {}).values(),
    )
    verify_verdict, verify_summary, verify_problems = \
        portability_preflight(verify_report)
    return HandoffResult(
        success=True, dest_path=dest_path, verdict=verify_verdict,
        summary=verify_summary,
        embedded_asset_ids=tuple(sorted(set(embedded))),
        problems=tuple(packaging_summary(verify_report)["issues"]),
    )


def _index_entries(full_data: dict[str, Any]) -> dict[str, Any]:
    """把 full_data 里的资源索引 dict 还原为 {resource_id: entry} 形态。"""
    raw = full_data.get("resource_index") or {}
    return {
        str(entry.get("resource_id")): entry
        for entry in raw.get("resources", [])
        if isinstance(entry, dict) and entry.get("resource_id")
    }
