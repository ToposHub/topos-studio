"""媒体池元数据 CSV 导入/导出（P1-7）。

约定（与 DaVinci Resolve 的 Metadata Import/Export 对齐）：

- **匹配键优先级**：``asset_id``（稳定 ID，首选）→ ``path``（来源身份，
  兜底）。两者都缺失/无法匹配的行进 warnings，绝不模糊匹配名称。
- **差异预览**：``plan_metadata_import`` 只产出计划（含逐字段 old→new），
  不写工程；``apply_metadata_import`` 应用计划；命令层
  （``ImportMetadataCommand``）把整个导入包成单步撤销事务。
- **空值语义（明确）**：
  - 单元格为空字符串 → **清除**该字段（评级归 0、旗标/颜色归 none、
    关键词清空、注释/制作信息清空）；
  - 整列缺失 → **保持**原值（不导入该字段）。
- **失败不静默部分写入**：非法值（评级越界、旗标/颜色域外）、重复
  asset_id 行、无法匹配行全部进 warnings/跳过项，预览可见；合法变更
  一次应用。同一素材被多行匹配时按行序合并（后行覆盖前行）并警告。
- **编码**：导出 UTF-8（含 BOM，Excel 兼容）；导入按
  utf-8-sig → utf-8 → gbk 依次尝试解码。
- 只导入日志/制作字段；来源探测字段（fps/分辨率/编码…）只导出，
  不允许 CSV 改写（探测事实以媒体为准）。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from src.features.edit.core.project import (
    ASSET_COLOR_LABELS,
    ASSET_FLAG_VALUES,
    EditProject,
)

__all__ = [
    "METADATA_CSV_COLUMNS",
    "IMPORTABLE_COLUMNS",
    "export_metadata_csv",
    "MetadataFieldChange",
    "MetadataAssetChange",
    "MetadataImportPlan",
    "plan_metadata_import",
    "apply_metadata_import",
    "decode_metadata_csv_bytes",
]

#: 导出列（顺序即 CSV 列序）。来源只读列用于核对，不可导入。
METADATA_CSV_COLUMNS: tuple[str, ...] = (
    "asset_id", "path", "display_name", "keywords", "rating", "flag",
    "color_label", "comment", "reel", "scene", "shot", "take",
    "media_type", "duration_seconds", "fps", "width", "height", "codec",
)

#: 允许导入的列（日志/制作字段）。空单元格 = 清除；列缺失 = 保持。
IMPORTABLE_COLUMNS: tuple[str, ...] = (
    "display_name", "keywords", "rating", "flag", "color_label", "comment",
    "reel", "scene", "shot", "take",
)

_KEYWORDS_SEPARATOR = ";"


# ================================================================
# 导出
# ================================================================

def export_metadata_csv(
    project: EditProject,
    asset_ids: Optional[Iterable[str]] = None,
) -> str:
    """导出素材日志/制作/来源只读字段为 CSV 文本（UTF-8，无 BOM——
    写文件时由调用方选择 utf-8-sig）。"""
    ids = list(asset_ids) if asset_ids is not None else list(project.assets)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(METADATA_CSV_COLUMNS)
    for asset_id in ids:
        asset = project.assets.get(asset_id)
        if asset is None:
            continue
        writer.writerow([
            asset.asset_id,
            asset.path,
            asset.display_name,
            _KEYWORDS_SEPARATOR.join(asset.keywords),
            asset.rating,
            asset.flag,
            asset.color_label,
            asset.comment,
            asset.reel,
            asset.scene,
            asset.shot,
            asset.take,
            asset.media_type,
            asset.metadata.get("duration_seconds") or "",
            asset.metadata.get("fps") or "",
            asset.metadata.get("width") or "",
            asset.metadata.get("height") or "",
            asset.metadata.get("codec") or "",
        ])
    return buffer.getvalue()


def decode_metadata_csv_bytes(payload: bytes) -> str:
    """字节流 → 文本（utf-8-sig → utf-8 → gbk 依次尝试，全败抛 ValueError）。"""
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码 CSV（尝试 utf-8-sig/utf-8/gbk 均失败）")


# ================================================================
# 导入计划
# ================================================================

@dataclass(frozen=True)
class MetadataFieldChange:
    field: str
    old_value: Any
    new_value: Any

    @property
    def is_changed(self) -> bool:
        return self.old_value != self.new_value


@dataclass(frozen=True)
class MetadataAssetChange:
    asset_id: str
    changes: tuple[MetadataFieldChange, ...]

    @property
    def changed_fields(self) -> tuple[MetadataFieldChange, ...]:
        return tuple(c for c in self.changes if c.is_changed)


@dataclass(frozen=True)
class MetadataImportPlan:
    """差异预览（不写工程）。warnings 与 changes 一并呈现给用户确认。"""

    asset_changes: tuple[MetadataAssetChange, ...] = ()
    unmatched_rows: tuple[int, ...] = ()      # 1 基行号（不含表头）
    warnings: tuple[str, ...] = ()
    total_rows: int = 0


def _parse_cell(field: str, raw: str, warnings: list[str],
                row_number: int) -> tuple[bool, Any]:
    """单元格 → 字段值。返回 (合法, 值)；空串是合法的"清除"。"""
    if field == "rating":
        if raw == "":
            return (True, 0)
        try:
            value = int(raw)
        except ValueError:
            warnings.append(f"第 {row_number} 行 rating 非整数: {raw!r}，跳过")
            return (False, None)
        if not 0 <= value <= 5:
            warnings.append(f"第 {row_number} 行 rating 越界: {value}，跳过")
            return (False, None)
        return (True, value)
    if field == "flag":
        if raw == "":
            return (True, "none")
        if raw not in ASSET_FLAG_VALUES:
            warnings.append(
                f"第 {row_number} 行 flag 非法: {raw!r}（合法: "
                f"{ASSET_FLAG_VALUES}），跳过")
            return (False, None)
        return (True, raw)
    if field == "color_label":
        if raw == "":
            return (True, "none")
        if raw not in ASSET_COLOR_LABELS:
            warnings.append(
                f"第 {row_number} 行 color_label 非法: {raw!r}，跳过")
            return (False, None)
        return (True, raw)
    if field == "keywords":
        if raw == "":
            return (True, ())
        return (True, tuple(k.strip() for k in raw.split(_KEYWORDS_SEPARATOR)
                            if k.strip()))
    # 其余字符串字段：空串 = 清除为 ""
    return (True, raw)


def _current_value(asset: Any, field_name: str) -> Any:
    if field_name == "keywords":
        return tuple(asset.keywords)
    if field_name == "display_name":
        return asset.display_name
    return getattr(asset, field_name, "")


def plan_metadata_import(
    project: EditProject,
    csv_text: str,
) -> MetadataImportPlan:
    """解析 CSV → 差异计划（匹配 asset_id 优先，path 兜底；不写工程）。"""
    warnings: list[str] = []
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return MetadataImportPlan(warnings=("CSV 为空",))
    header = [cell.strip() for cell in rows[0]]
    lower_header = [cell.lower() for cell in header]
    unknown = [cell for cell in header
               if cell.lower() not in METADATA_CSV_COLUMNS]
    if unknown:
        warnings.append(f"未知列将被忽略: {', '.join(unknown)}")

    importable_indexes = [
        (index, name)
        for index, name in enumerate(lower_header)
        if name in IMPORTABLE_COLUMNS
    ]
    if not importable_indexes:
        return MetadataImportPlan(
            warnings=tuple(warnings) + ("没有可导入的列（日志/制作字段）",),
            total_rows=max(0, len(rows) - 1))

    asset_id_index = lower_header.index("asset_id") if "asset_id" in lower_header else None
    path_index = lower_header.index("path") if "path" in lower_header else None
    if asset_id_index is None and path_index is None:
        return MetadataImportPlan(
            warnings=tuple(warnings) + ("缺少匹配列（asset_id 或 path）",),
            total_rows=max(0, len(rows) - 1))

    pending: dict[str, dict[str, Any]] = {}
    unmatched: list[int] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # 空行
        def _cell(index: Optional[int]) -> str:
            if index is None or index >= len(row):
                return ""
            return row[index].strip()

        asset = None
        key = _cell(asset_id_index)
        if key and key in project.assets:
            asset = project.assets[key]
        else:
            by_path = _cell(path_index)
            if by_path:
                asset = project.get_asset_by_path(by_path)
                if asset is None:
                    matches = [a for a in project.assets.values()
                               if a.path == by_path]
                    asset = matches[0] if matches else None
        if asset is None:
            unmatched.append(row_number - 1)
            continue

        fields = pending.setdefault(asset.asset_id, {})
        if fields:
            warnings.append(
                f"第 {row_number} 行与前面的行匹配同一素材 "
                f"{asset.display_name!r}，后行覆盖前行")
        for index, name in importable_indexes:
            raw = row[index] if index < len(row) else ""
            ok, value = _parse_cell(name, raw.strip(), warnings, row_number)
            if ok:
                fields[name] = value

    changes: list[MetadataAssetChange] = []
    for asset_id, fields in pending.items():
        asset = project.assets[asset_id]
        field_changes = tuple(
            MetadataFieldChange(
                field=name,
                old_value=_current_value(asset, name),
                new_value=fields[name])
            for name in sorted(fields)
        )
        changes.append(MetadataAssetChange(
            asset_id=asset_id, changes=field_changes))

    if unmatched:
        warnings.append(
            f"{len(unmatched)} 行无法匹配任何素材（asset_id/path 均未命中），"
            "已跳过")
    return MetadataImportPlan(
        asset_changes=tuple(changes),
        unmatched_rows=tuple(unmatched),
        warnings=tuple(warnings),
        total_rows=max(0, len(rows) - 1),
    )


def apply_metadata_import(
    project: EditProject,
    plan: MetadataImportPlan,
) -> EditProject:
    """应用差异计划（只写有实际变化的字段；纯函数）。"""
    for change in plan.asset_changes:
        if not change.changed_fields:
            continue
        asset = project.assets[change.asset_id]
        log_kwargs: dict[str, Any] = {}
        production_kwargs: dict[str, Any] = {}
        for item in change.changed_fields:
            if item.field in ("keywords", "rating", "flag", "color_label",
                              "comment"):
                log_kwargs[item.field] = item.new_value
            elif item.field == "display_name":
                project = project.rename_asset(
                    change.asset_id, str(item.new_value))
            else:  # reel / scene / shot / take
                production_kwargs[item.field] = str(item.new_value)
        if log_kwargs:
            project = project.update_asset_log_info(
                change.asset_id, **log_kwargs)
        if production_kwargs:
            project = project.update_asset_production_info(
                change.asset_id, **production_kwargs)
    return project
