"""HelpIndex — 本地可引用帮助索引（M6 / §9.1 / 计划 §11.1 M6-2）。

计划 §9.1：知识库来源优先级 = 当前版本内置 help/tooltip/快捷键/功能 manifest → 官方文档 →
用户工程脱敏状态 → 在线官方资源。回答必须带功能版本/可用性、相关页面/菜单、快捷键和具体下一步；
找不到时说"不确定"并建议搜索或反馈，**而不是编造**（§11.1 M6 红线）。

设计（遵循 §9.1 红线）：

- **本地优先、不编造**：HelpIndex 只从可信本地源（diagnostics catalog、shortcut registry、
  capability manifest、feature flags、panel registry、i18n 文案）聚合条目。每个条目带
  ``source_type`` + ``source_ref``，可引用回原始来源；找不到时返回空而非编造。
- **可引用（citable）**：每个条目有稳定 ``entry_id``（如 ``diag:TAEP_ASSET_OFFLINE``、
  ``shortcut:file.open``、``panel:edit.timeline``）；这些 id 进 ContextBundle.help_refs
  为模型提供 grounding（context_service 已有 KIND_HELP_REFS 接口）。
- **可用性反映 feature flag**：feature-dependent 的条目标注 enabled/disabled（§9.2：
  "不可用功能明确标为未安装/未启用"）。
- **不依赖 AI 模型**：HelpIndex 是纯本地数据；即使无 profile/无模型，用户也能 search_help
  （§11.1 M6："即使未配置 AI 模型，用户仍能使用本地帮助"）。

源接入（首版）：
- diagnostics.TaepDiagnosticCode → 每个码一条 HelpEntry（含 prereqs/remediation 模板）。
- ShortcutManager（可选；未初始化时跳过）→ 每个动作一条。
- capability manifest（可选）→ operations/effects/export 条目。
- feature_flags（可选）→ feature 可用性条目。
- panel registry（可选）→ 每个面板一条（含 page/category）。

host 调 ``HelpIndex.build(sources...)`` 构建一次；``search(query)`` / ``get(entry_id)`` /
``explain_diagnostic(code)`` 查询。无 Qt 依赖。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# HelpEntry + HelpSource
# ---------------------------------------------------------------------- #


class HelpSource(str):
    """HelpEntry 来源类型标签（§9.1 来源优先级）。"""

    pass


SOURCE_DIAGNOSTIC = "diagnostic"
SOURCE_SHORTCUT = "shortcut"
SOURCE_CAPABILITY = "capability"
SOURCE_FEATURE = "feature"
SOURCE_PANEL = "panel"
SOURCE_DOC = "doc"  # 官方文档引用（首版无内置 docs，预留给 M6 后续）


@dataclass(frozen=True)
class HelpEntry:
    """单条帮助（§9.1：带版本/可用性/页面/下一步，找不到时返回空而非编造）。

    ``entry_id`` 是稳定可引用 id（进 ContextBundle.help_refs）。
    """

    entry_id: str                    # 稳定 id（如 diag:TAEP_ASSET_OFFLINE / shortcut:file.open）
    title: str
    category: str                    # diagnostic / shortcut / capability / feature / panel
    body: str                        # 解释/步骤/下一步
    source_type: str = SOURCE_DOC    # 来源类型（HelpSource）
    source_ref: str = ""             # 原始来源引用（如诊断码 / shortcut id / panel_id）
    page_ref: str = ""               # 相关页面/菜单（§9.1 "相关页面/菜单"）
    shortcut_hint: str = ""          # 快捷键提示（§9.1）
    prereqs: tuple[str, ...] = ()    # 前置条件（§9.1 "可用性/前置条件"）
    next_steps: tuple[str, ...] = ()  # 具体下一步（§9.1）
    feature_id: str | None = None    # 依赖的 feature（None = 总是可用）
    enabled: bool = True             # 当前是否可用（受 feature flag 影响；§9.2）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "entry_id": self.entry_id,
            "title": self.title,
            "category": self.category,
            "body": self.body,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "enabled": self.enabled,
        }
        if self.page_ref:
            d["page_ref"] = self.page_ref
        if self.shortcut_hint:
            d["shortcut_hint"] = self.shortcut_hint
        if self.prereqs:
            d["prereqs"] = list(self.prereqs)
        if self.next_steps:
            d["next_steps"] = list(self.next_steps)
        if self.feature_id is not None:
            d["feature_id"] = self.feature_id
        return d


# ---------------------------------------------------------------------- #
# HelpSources（producer 集合，宿主注入；解耦 HelpIndex 与具体 registry）
# ---------------------------------------------------------------------- #


class HelpSources(Protocol):
    """HelpIndex 的数据源契约（宿主注入，避免硬依赖具体 manager）。

    所有方法返回 None/空表示"无此源"；HelpIndex 据此跳过。
    """

    def list_diagnostics(self) -> list[dict[str, Any]] | None:
        """诊断码 + 模板。每项 {code, label, message_template, suggested_action}。"""
        ...

    def list_shortcuts(self) -> list[dict[str, Any]] | None:
        """快捷键。每项 {id, label, category, shortcut, feature_id}。"""
        ...

    def list_capabilities(self) -> dict[str, Any] | None:
        """capability manifest（operations/effects/export/limits）。"""
        ...

    def list_features(self) -> list[dict[str, Any]] | None:
        """feature flag。每项 {feature_id, enabled, label}。"""
        ...

    def list_panels(self) -> list[dict[str, Any]] | None:
        """panel registry。每项 {panel_id, title, category, page_ids, feature_id, enabled}。"""
        ...


# ---------------------------------------------------------------------- #
# 诊断解释模板（§2：TaepDiagnosticCode 的 prereqs/remediation/doc_link）
# ---------------------------------------------------------------------- #
#
# diagnostics.MESSAGE_TEMPLATES 只给一行文案；本表给每个码补 prereqs + remediation + 页面引用，
# 使 explain_diagnostic 能给"最短修复路径"（§9.2）。找不到的码 → explain_diagnostic 返回 None
# （模型/用户据此说"不确定"，不编造）。

_DIAGNOSTIC_EXPLANATIONS: dict[str, dict[str, Any]] = {
    "TAEP_ASSET_OFFLINE": {
        "prereqs": ("素材文件存在于磁盘",),
        "remediation": ("在媒体池右键素材 → 'Show in File Manager' 定位", "若已移动，用 Relink 重新链接"),
        "page_ref": "edit.media_pool",
    },
    "TAEP_REVISION_CONFLICT": {
        "prereqs": ("工程未被并发修改",),
        "remediation": ("刷新工程快照（重新打开或等待同步）", "重新发起请求"),
        "page_ref": "edit.ai_assistant",
    },
    "TAEP_PERMISSION_DENIED": {
        "prereqs": ("当前权限层级允许此操作",),
        "remediation": ("在 AI 面板切换权限模式（如仅建议 → 预览计划）", "确认 ApprovalGrant 未过期"),
        "page_ref": "edit.ai_assistant",
    },
    "TAEP_EXPORT_PREFLIGHT_FAILED": {
        "prereqs": ("时间线无离线素材", "字幕/响度检查通过"),
        "remediation": ("解决导出预检报告中的 diagnostics", "检查媒体离线/缺失轨道"),
        "page_ref": "deliver.export",
    },
    "TAEP_PLAN_TOO_LARGE": {
        "prereqs": ("plan 操作数在 limits.max_operations_per_plan 内",),
        "remediation": ("拆分为多个小 plan", "减少一次性变更范围"),
        "page_ref": "edit.ai_assistant",
    },
    "TAEP_QUOTA_EXCEEDED": {
        "prereqs": ("预算/配额未超",),
        "remediation": ("等待配额重置", "联系管理员提升配额"),
        "page_ref": "edit.ai_assistant",
    },
    "TAEP_SOURCE_RANGE_INVALID": {
        "prereqs": ("素材范围在 [0, duration) 内",),
        "remediation": ("重新分析素材或刷新 snapshot", "检查 fps 换算"),
        "page_ref": "edit.media_pool",
    },
}


# ---------------------------------------------------------------------- #
# HelpIndex
# ---------------------------------------------------------------------- #


class HelpIndex:
    """本地帮助索引（M6 / §9.1）。

    从 :class:`HelpSources` 聚合 :class:`HelpEntry`；提供 search/get/explain_diagnostic。
    不可变（build 后只读）；host 可重建以反映 feature flag 变化。
    """

    def __init__(self) -> None:
        self._entries: dict[str, HelpEntry] = {}

    @classmethod
    def build(cls, sources: HelpSources) -> "HelpIndex":
        """从 sources 聚合构建索引（一次性）。"""
        index = cls()
        index._add_diagnostics(sources)
        # AUD-13612：构建期只调一次 list_features()，预构建 {feature_id: enabled}
        # 映射传给各条目——避免每条 shortcut/panel 重复调用 list_features() 的
        # O(n·m) 重复扫描与对象重建（协议并不保证源端缓存）。
        feature_map = cls._feature_map(sources)
        index._add_shortcuts(sources, feature_map)
        index._add_capabilities(sources)
        index._add_features(sources)
        index._add_panels(sources, feature_map)
        logger.info("HelpIndex built: %d entries", len(index._entries))
        return index

    @classmethod
    def from_entries(cls, entries: "Iterable[HelpEntry]") -> "HelpIndex":
        """AUD-22510：显式测试缝——按已知条目集合装配索引。

        生产构建一律走 :meth:`build`（从 HelpSources 聚合）；测试/工具
        需要精确控制条目集合时用本方法，不再直接写私有 ``_entries``。
        重复 ``entry_id`` 以后者为准（与 build 的 _add_* 覆盖语义一致）。
        """
        index = cls()
        for e in entries:
            index._entries[e.entry_id] = e
        return index

    # ---------------- 查询 ---------------- #

    def get(self, entry_id: str) -> HelpEntry | None:
        """按 entry_id 取条目（找不到返回 None，不编造；§9.1）。"""
        return self._entries.get(entry_id)

    def search(self, query: str, *, limit: int = 20) -> list[HelpEntry]:
        """关键词搜索（title/body/category/source_ref 子串匹配，§9.1）。

        空查询返回全部（受 limit）。不区分大小写。
        """
        if not query:
            return list(self._entries.values())[:limit]
        q = query.lower()
        scored: list[tuple[int, HelpEntry]] = []
        for e in self._entries.values():
            haystack = f"{e.title} {e.body} {e.category} {e.source_ref}".lower()
            if q in haystack:
                # title 命中得分高
                score = 2 if q in e.title.lower() else 1
                scored.append((score, e))
        scored.sort(key=lambda x: (-x[0], x[1].title))
        return [e for _, e in scored[:limit]]

    def explain_diagnostic(self, code: str) -> HelpEntry | None:
        """解释一个诊断码（§9.2：最短修复路径；不编造，找不到返回 None）。

        ``code`` 是 TaepDiagnosticCode.value（如 "TAEP_ASSET_OFFLINE"）。
        """
        return self.get(f"diag:{code}")

    def list_by_category(self, category: str) -> list[HelpEntry]:
        return [e for e in self._entries.values() if e.category == category]

    @property
    def count(self) -> int:
        return len(self._entries)

    def all_entry_ids(self) -> list[str]:
        """所有 entry_id（供 ContextBundle.help_refs 引用）。"""
        return list(self._entries.keys())

    # ---------------- 内部：各源 → HelpEntry ---------------- #

    def _add_diagnostics(self, sources: HelpSources) -> None:
        diags = sources.list_diagnostics()
        if not diags:
            return
        for d in diags:
            code = str(d.get("code", ""))
            if not code:
                continue
            label = str(d.get("label", code))
            tmpl = str(d.get("message_template", ""))
            explanation = _DIAGNOSTIC_EXPLANATIONS.get(code, {})
            prereqs = tuple(explanation.get("prereqs", ()))
            remediation = tuple(explanation.get("remediation", ()))
            page_ref = str(explanation.get("page_ref", ""))
            entry = HelpEntry(
                entry_id=f"diag:{code}",
                title=f"诊断：{label}",
                category="diagnostic",
                body=tmpl or f"诊断码 {code}",
                source_type=SOURCE_DIAGNOSTIC,
                source_ref=code,
                page_ref=page_ref,
                prereqs=prereqs,
                next_steps=remediation,
                enabled=True,  # 诊断码本身总是可用（解释总是有效）
            )
            self._entries[entry.entry_id] = entry

    def _add_shortcuts(
        self, sources: HelpSources, feature_map: dict[str, bool],
    ) -> None:
        shortcuts = sources.list_shortcuts()
        if not shortcuts:
            return
        for s in shortcuts:
            sid = str(s.get("id", ""))
            if not sid:
                continue
            label = str(s.get("label", sid))
            category = str(s.get("category", "shortcut"))
            shortcut = str(s.get("shortcut", ""))
            feature_id = s.get("feature_id")
            enabled = self._feature_enabled(feature_map, feature_id)
            entry = HelpEntry(
                entry_id=f"shortcut:{sid}",
                title=f"快捷键：{label}",
                category="shortcut",
                body=f"{label}（{category}）",
                source_type=SOURCE_SHORTCUT,
                source_ref=sid,
                shortcut_hint=shortcut,
                feature_id=feature_id,
                enabled=enabled,
            )
            self._entries[entry.entry_id] = entry

    def _add_capabilities(self, sources: HelpSources) -> None:
        manifest = sources.list_capabilities()
        if not manifest:
            return
        # operations / effects / export 作为可用性条目
        for op in manifest.get("operations", []) or []:
            entry = HelpEntry(
                entry_id=f"capability:op:{op}",
                title=f"支持的操作：{op}",
                category="capability",
                body=f"EditPlan 支持 {op} 操作。",
                source_type=SOURCE_CAPABILITY,
                source_ref=str(op),
            )
            self._entries[entry.entry_id] = entry
        for fx in manifest.get("effects", []) or []:
            fx_type = fx.get("effect_type", "") if isinstance(fx, dict) else str(fx)
            if not fx_type:
                continue
            entry = HelpEntry(
                entry_id=f"capability:effect:{fx_type}",
                title=f"效果：{fx_type}",
                category="capability",
                body=f"软件支持 {fx_type} 效果（capability manifest）。",
                source_type=SOURCE_CAPABILITY,
                source_ref=fx_type,
            )
            self._entries[entry.entry_id] = entry

    def _add_features(self, sources: HelpSources) -> None:
        features = sources.list_features()
        if not features:
            return
        for f in features:
            fid = str(f.get("feature_id", ""))
            if not fid:
                continue
            label = str(f.get("label", fid))
            enabled = bool(f.get("enabled", False))
            entry = HelpEntry(
                entry_id=f"feature:{fid}",
                title=f"功能：{label}（{'已启用' if enabled else '未启用'}）",
                category="feature",
                body=f"功能 {label} 当前{'已启用' if enabled else '未启用'}。",
                source_type=SOURCE_FEATURE,
                source_ref=fid,
                feature_id=fid,
                enabled=enabled,
            )
            self._entries[entry.entry_id] = entry

    def _add_panels(
        self, sources: HelpSources, feature_map: dict[str, bool],
    ) -> None:
        panels = sources.list_panels()
        if not panels:
            return
        for p in panels:
            pid = str(p.get("panel_id", ""))
            if not pid:
                continue
            title = str(p.get("title", pid))
            category = str(p.get("category", "panel"))
            page_ids = p.get("page_ids", ()) or ()
            feature_id = p.get("feature_id")
            enabled = self._feature_enabled(feature_map, feature_id)
            page_ref = page_ids[0] if page_ids else ""
            entry = HelpEntry(
                entry_id=f"panel:{pid}",
                title=f"面板：{title}",
                category="panel",
                body=f"{title}（{category}）。{'当前已启用' if enabled else '当前未启用'}。",
                source_type=SOURCE_PANEL,
                source_ref=pid,
                page_ref=page_ref,
                feature_id=feature_id,
                enabled=enabled,
            )
            self._entries[entry.entry_id] = entry

    @staticmethod
    def _feature_map(sources: HelpSources) -> dict[str, bool]:
        """一次性读取 feature 源，构建 {feature_id: enabled} 映射（AUD-13612）。

        空源返回空 dict：查询端对未知 feature_id 一律"假定可用"，与旧行为一致。
        """
        features = sources.list_features()
        if not features:
            return {}
        return {
            str(f.get("feature_id", "")): bool(f.get("enabled", False))
            for f in features
            if str(f.get("feature_id", ""))
        }

    @staticmethod
    def _feature_enabled(feature_map: dict[str, bool], feature_id: Any) -> bool:
        """查询某 feature 是否启用（feature_id 为 None 时总是 True）。

        接受预构建映射而非 HelpSources：未列入映射 → 假定可用（与旧
        "未在列表 → 假定可用" 语义一致）。
        """
        if feature_id is None:
            return True
        return feature_map.get(str(feature_id), True)


__all__ = [
    "HelpIndex",
    "HelpEntry",
    "HelpSources",
    "HelpSource",
    "SOURCE_DIAGNOSTIC",
    "SOURCE_SHORTCUT",
    "SOURCE_CAPABILITY",
    "SOURCE_FEATURE",
    "SOURCE_PANEL",
    "SOURCE_DOC",
]
