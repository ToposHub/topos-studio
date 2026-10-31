"""DESK-02C · 依赖闭包推导、完整性/可携性预检与打包摘要。

问题
----
"这个工程能不能完整交给另一台机器继续精剪？"此前没有可机检的答案：
素材可能离线、LUT 可能指向本机路径、字体可能未内嵌——用户只能等到
打开/导出失败才发现。交付前置检查要能：

- **定位**：缺失/哈希错误落到具体对象（asset_id / stack_id /
  resource_id），不是一句"有 3 个问题"；
- **不误报**：内嵌资源（已过 SHA-256 校验）不算缺失；显式 is_offline
  的素材单列（用户知情），不混入"不可交付"；
- **摘要**：打包前的机器可读摘要（各类资源计数/字节数/结论），供
  07B"打包交接到移动端"任务与 UI 消费。

依赖闭包来源：
1. 素材表（project.assets / full_data["assets"]）——外部路径在线性 +
   内嵌资源（resource_uri → resolver 已校验）；
2. 时间线引用的 asset_id（引用了不在素材表的 = 悬空引用）；
3. 调色栈 lut_path（外部路径 / topos-res:// URI）；
4. 资源索引内的字体条目（kind=font）。

结论分级：
- ``full``：全部依赖可携（在线外部 + 已内嵌）；
- ``partial``：存在显式离线素材或未内嵌的本机路径资源（交出后需
  重链接，如实告知）；
- ``blocked``：存在悬空引用 / 内嵌资源哈希不符 / 渲染必需素材缺失
  （"可完整精剪"为误报，必须先修）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "DependencyRef",
    "DependencyReport",
    "collect_dependencies",
    "portability_preflight",
    "packaging_summary",
]

_RESOLUTION_ONLINE = "external_online"
_RESOLUTION_EMBEDDED = "embedded"
_RESOLUTION_OFFLINE_FLAGGED = "offline_flagged"
_RESOLUTION_MISSING = "missing"
_RESOLUTION_HASH_MISMATCH = "hash_mismatch"
_RESOLUTION_DANGLING = "dangling_ref"


@dataclass(frozen=True)
class DependencyRef:
    """一条依赖及其可携性判定。"""

    object_kind: str      # asset / timeline_ref / lut / font
    object_id: str
    reference: str        # 路径或 URI（定位用）
    resolution: str
    detail: str = ""

    @property
    def portable(self) -> bool:
        return self.resolution in (_RESOLUTION_ONLINE, _RESOLUTION_EMBEDDED)

    @property
    def blocking(self) -> bool:
        return self.resolution in (
            _RESOLUTION_MISSING, _RESOLUTION_HASH_MISMATCH,
            _RESOLUTION_DANGLING)


@dataclass(frozen=True)
class DependencyReport:
    """依赖闭包审计结果。"""

    refs: tuple[DependencyRef, ...] = ()
    embedded_bytes: int = 0

    def by_kind(self, kind: str) -> tuple[DependencyRef, ...]:
        return tuple(r for r in self.refs if r.object_kind == kind)

    @property
    def has_blocking(self) -> bool:
        return any(r.blocking for r in self.refs)

    @property
    def has_partial(self) -> bool:
        return any(
            r.resolution == _RESOLUTION_OFFLINE_FLAGGED for r in self.refs)


def _iter_asset_ids_in_sequences(sequences: Iterable[Any]):
    seen: set[str] = set()
    for seq in sequences or ():
        for track in getattr(seq, "video_tracks", ()) or ():
            for clip in track.clips:
                aid = getattr(clip, "asset_id", "")
                if aid:
                    seen.add(aid)
        for track in getattr(seq, "audio_tracks", ()) or ():
            for clip in track.clips:
                aid = getattr(clip, "asset_id", "")
                if aid:
                    seen.add(aid)
    return seen


def collect_dependencies(
    *,
    assets: dict[str, Any] | None = None,
    full_data: dict[str, Any] | None = None,
    timeline_sequences: Iterable[Any] | None = None,
    color_stacks: Iterable[Any] | None = None,
) -> DependencyReport:
    """推导依赖闭包并逐条判定。

    Args:
        assets: 素材对象表（EditProject.assets 或 full_data["assets"] 的
            dict 形态——两者取一，支持 duck-typing：path/is_offline/
            resource_uri）。
        full_data: 包加载产物（取 resource_index / resource_resolver /
            color_stacks；assets 缺省时从这里取）。
        timeline_sequences: 时间线集合（悬空引用检查）。
        color_stacks: 调色栈集合（缺省从 full_data 取 dict 形态）。
    """
    data = full_data or {}
    if assets is None:
        assets = data.get("assets") or {}
    resolver = data.get("resource_resolver")
    index = getattr(resolver, "index", None)
    refs: list[DependencyRef] = []
    embedded_bytes = 0

    embedded_ids: set[str] = set()
    if index is not None:
        for entry in index.entries.values():
            embedded_bytes += max(0, entry.size_bytes)
            if entry.asset_id:
                embedded_ids.add(entry.asset_id)
            if entry.kind == "font":
                refs.append(DependencyRef(
                    object_kind="font", object_id=entry.resource_id,
                    reference=entry.uri, resolution=_RESOLUTION_EMBEDDED,
                    detail=entry.original_filename))
    if getattr(resolver, "diagnostics", None):
        # 内容校验失败（哈希/大小/缺失）→ 逐条定位
        for line in resolver.diagnostics():
            refs.append(DependencyRef(
                object_kind="resource", object_id="resource",
                reference="", resolution=_RESOLUTION_HASH_MISMATCH,
                detail=line))

    for asset_id, asset in (assets or {}).items():
        path = getattr(asset, "path", "") or (asset.get("path", "")
                                              if isinstance(asset, dict) else "")
        resource_uri = getattr(asset, "resource_uri", None) or (
            asset.get("resource_uri") if isinstance(asset, dict) else None)
        is_offline = getattr(asset, "is_offline", False) or (
            asset.get("is_offline", False) if isinstance(asset, dict)
            else False)
        if resource_uri:
            refs.append(DependencyRef(
                object_kind="asset", object_id=str(asset_id),
                reference=str(resource_uri), resolution=_RESOLUTION_EMBEDDED,
                detail=path))
        elif path and os.path.exists(path):
            refs.append(DependencyRef(
                object_kind="asset", object_id=str(asset_id),
                reference=str(path), resolution=_RESOLUTION_ONLINE))
        elif is_offline:
            refs.append(DependencyRef(
                object_kind="asset", object_id=str(asset_id),
                reference=str(path), resolution=_RESOLUTION_OFFLINE_FLAGGED,
                detail="显式离线（用户知情；交接后需重链接）"))
        else:
            refs.append(DependencyRef(
                object_kind="asset", object_id=str(asset_id),
                reference=str(path), resolution=_RESOLUTION_MISSING,
                detail="文件不存在且无内嵌资源"))

    # 悬空引用：时间线引用了素材表外（或已删）的 asset_id
    referenced = _iter_asset_ids_in_sequences(timeline_sequences)
    for asset_id in sorted(referenced - set(assets or {})):
        refs.append(DependencyRef(
            object_kind="timeline_ref", object_id=str(asset_id),
            reference="", resolution=_RESOLUTION_DANGLING,
            detail="时间线引用的素材不在素材表"))

    # 调色栈 LUT 依赖
    stacks = color_stacks
    if stacks is None:
        stacks = []
        raw_stacks = data.get("color_stacks") or {}
        if isinstance(raw_stacks, dict):
            for stack_id, payload in raw_stacks.items():
                if isinstance(payload, dict):
                    stacks.append((stack_id, payload.get("lut_path")))
    for item in stacks or ():
        if isinstance(item, tuple):
            stack_id, lut_path = item
        else:
            stack_id = getattr(item, "stack_id", "")
            lut_path = getattr(item, "lut_path", None)
        if not lut_path:
            continue
        if str(lut_path).startswith("topos-res://"):
            refs.append(DependencyRef(
                object_kind="lut", object_id=str(stack_id),
                reference=str(lut_path), resolution=_RESOLUTION_EMBEDDED))
        elif os.path.exists(str(lut_path)):
            refs.append(DependencyRef(
                object_kind="lut", object_id=str(stack_id),
                reference=str(lut_path),
                resolution=_RESOLUTION_ONLINE if os.path.isabs(lut_path)
                else _RESOLUTION_ONLINE,
                detail="外部 LUT（交接需打包方决定是否内嵌）"))
        else:
            refs.append(DependencyRef(
                object_kind="lut", object_id=str(stack_id),
                reference=str(lut_path), resolution=_RESOLUTION_MISSING))

    return DependencyReport(refs=tuple(refs), embedded_bytes=embedded_bytes)


def portability_preflight(
    report: DependencyReport,
) -> tuple[str, str, tuple[DependencyRef, ...]]:
    """可携性预检：返回 (结论, 摘要文本, 问题清单)。

    结论 ∈ full / partial / blocked——blocked 时"可完整精剪"即误报。
    """
    if report.has_blocking:
        verdict = "blocked"
    elif report.has_partial:
        verdict = "partial"
    else:
        verdict = "full"
    problems = tuple(r for r in report.refs if not r.portable)
    counts: dict[str, int] = {}
    for ref in problems:
        counts[ref.resolution] = counts.get(ref.resolution, 0) + 1
    summary = (
        f"依赖闭包：{len(report.refs)} 条引用，"
        f"内嵌 {sum(1 for r in report.refs if r.resolution == _RESOLUTION_EMBEDDED)}，"
        f"在线 {sum(1 for r in report.refs if r.resolution == _RESOLUTION_ONLINE)}，"
        f"问题 {len(problems)}"
        + (f"（{counts}）" if counts else "")
    )
    return verdict, summary, problems


def packaging_summary(report: DependencyReport) -> dict[str, Any]:
    """打包摘要（07B 打包交接到移动端 / UI 展示消费）。"""
    verdict, summary, problems = portability_preflight(report)
    by_kind: dict[str, dict[str, int]] = {}
    for ref in report.refs:
        bucket = by_kind.setdefault(
            ref.object_kind, {"total": 0, "portable": 0, "issues": 0})
        bucket["total"] += 1
        if ref.portable:
            bucket["portable"] += 1
        else:
            bucket["issues"] += 1
    return {
        "portability": verdict,
        "summary": summary,
        "embedded_bytes": report.embedded_bytes,
        "by_kind": by_kind,
        "issues": [
            {
                "object_kind": p.object_kind,
                "object_id": p.object_id,
                "reference": p.reference,
                "resolution": p.resolution,
                "detail": p.detail,
            }
            for p in problems
        ],
    }
