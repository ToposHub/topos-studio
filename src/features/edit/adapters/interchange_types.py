# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""TAEP Interchange 共用类型（Phase 7）。

定义 ``InterchangeAdapter`` 协议与 ``InterchangeReport``，供 FCPXML / OTIO / AAF
等 adapter 实现（TAEP §9.2）。

设计（TAEP §9）：

- 没有“通用 XML”：每个 adapter 独立建，接口用明确名称（``fcpxml`` / ``fcp7_xml``）。
- ``InterchangeReport`` 强制列出 preserved/converted/dropped/requires_user_action
  四类条目，绝不静默丢信息（TAEP §9.4）。
- 导入同样以 preview/confirmation 的方式 commit，不绕过 command compiler。
- 资产重连：导入时生成 external resource → Topos MediaAsset 映射表。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence


class InterchangeEntryCategory(str, Enum):
    """InterchangeReport 条目类别（TAEP §9.4）。"""

    PRESERVED = "preserved"  # 完整保留
    CONVERTED = "converted"  # 已转换（可能有细微差异）
    DROPPED = "dropped"  # 丢弃（无法映射）
    REQUIRES_USER_ACTION = "requires_user_action"  # 需用户决定（如 drop/bake/cancel）


@dataclass(frozen=True)
class InterchangeEntry:
    """单条报告条目。"""

    category: InterchangeEntryCategory
    element: str  # 如 "asset_clip" / "transition" / "effect"
    description: str
    path: str = ""  # XML 路径或 canonical 引用


@dataclass(frozen=True)
class InterchangeReport:
    """导入/导出的结构化报告（TAEP §9.4）。

    每次 import/export 必须输出此报告；“无损”只能在明确的功能子集里承诺。
    """

    format_id: str
    entries: tuple[InterchangeEntry, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()  # TAEP 诊断码摘要

    @property
    def has_errors(self) -> bool:
        return any(
            d.get("severity") == "error" for d in self.diagnostics
        )

    @property
    def dropped_count(self) -> int:
        return sum(1 for e in self.entries if e.category == InterchangeEntryCategory.DROPPED)

    @property
    def requires_action(self) -> list[InterchangeEntry]:
        return [e for e in self.entries if e.category == InterchangeEntryCategory.REQUIRES_USER_ACTION]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_id": self.format_id,
            "entries": [
                {"category": e.category.value, "element": e.element, "description": e.description, "path": e.path}
                for e in self.entries
            ],
            "diagnostics": list(self.diagnostics),
            "dropped_count": self.dropped_count,
        }


@dataclass(frozen=True)
class AssetRelinkRequest:
    """导入时需要用户重连的素材（external resource → Topos MediaAsset）。"""

    external_ref: str  # FCPXML 里的 resource id / path
    suggested_path: str = ""
    matched_asset_id: str | None = None  # 同路径/fingerprint 自动匹配
    ambiguous: bool = False  # 歧义（多候选）需用户选


@dataclass(frozen=True)
class ImportResult:
    """导入结果：候选 timeline + 资产映射 + 报告。

    导入**不**直接 commit；调用方应 preview 后经 command compiler 提交
    （TAEP §9.2 ``ImportResult`` 必须以 preview/confirmation 方式 commit）。
    """

    sequences: tuple["TimelineSequence", ...]  # 候选 TimelineSequence（可能多个 event）
    assets: tuple[dict[str, Any], ...]  # 候选 MediaAsset 描述（未实例化）
    relink_requests: tuple[AssetRelinkRequest, ...] = ()
    report: InterchangeReport | None = None


@dataclass(frozen=True)
class ExportResult:
    """导出结果：输出文件 + 报告。"""

    output_path: str
    report: InterchangeReport


@dataclass(frozen=True)
class InterchangeInspection:
    """inspect 结果：不导入，只报告 schema/版本/资源/不支持特性（TAEP §9.3）。"""

    format_id: str
    version: str = ""
    supported: bool = False
    asset_count: int = 0
    timeline_count: int = 0
    unsupported_features: tuple[str, ...] = ()
    report: InterchangeReport | None = None


# ---------------------------------------------------------------------- #
# InterchangeAdapter 协议（TAEP §9.2）
# ---------------------------------------------------------------------- #


class InterchangeAdapter(Protocol):
    """每个交换格式的 adapter 合同（TAEP §9.2）。

    接口使用明确名称（``fcpxml`` / ``fcp7_xml``），不能统称 ``xml``。
    """

    format_id: str
    """格式标识，如 ``fcpxml``。"""

    def inspect(self, source: str | Path) -> InterchangeInspection:
        """检查源文件：schema/版本/资源/不支持特性，不导入。"""
        ...

    def import_to_canonical(
        self, source: str | Path, *, options: dict[str, Any] | None = None
    ) -> ImportResult:
        """导入到 canonical model（候选 timeline + 资产映射 + 报告）。

        不直接 commit；调用方应 preview 后经 command compiler 提交。
        """
        ...

    def export_from_canonical(
        self, sequence: Any, *, options: dict[str, Any] | None = None
    ) -> ExportResult:
        """从 canonical model 导出到文件。"""
        ...
