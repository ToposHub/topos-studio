"""P1-COLOR-23: 工程依赖清单与可移植性报告。

扫描工程调色数据引用的外部依赖，生成可序列化的依赖报告：

- **LUT**：所有调色栈图层引用的 ``active_lut_path``（去重，逐文件
  SHA-256）；
- **插件**：图层效果栈启用的 DCTL 插件（PluginManager 注册表的
  ``source_uri`` + ``content_hash`` + ``version``，FX-M07 身份元数据）；
- **AI 模型**（可选）：AIModelManager 注册表中已缓存模型的本地状态。

验收映射：
- 工程复制到另一台机器 → ``build_dependency_report`` 给出完整依赖
  报告（missing 项可定位）；
- ``compare_reports(baseline, current)`` 用基线报告对比当前哈希，
  「同名但内容不同」的文件会被显式标出，不允许静默使用错误同名文件。

报告是纯数据（dict/dataclass），可嵌入工程快照或独立 JSON 导出。
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 依赖类别
KIND_LUT = "lut"
KIND_PLUGIN = "plugin"
KIND_AI_MODEL = "ai_model"

# 状态
STATUS_OK = "ok"
STATUS_MISSING = "missing"


def _file_sha256(path: str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


@dataclass
class DependencyEntry:
    """单个外部依赖的解析结果。"""

    kind: str                 # lut / plugin / ai_model
    key: str                  # 唯一键（路径或插件/模型 ID）
    status: str               # ok / missing
    path: Optional[str] = None
    size_bytes: int = 0
    sha256: Optional[str] = None
    version: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "status": self.status,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "version": self.version,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DependencyEntry":
        # AUD-0820: 外部 JSON 的数值字段可能被手工编辑/截断成非数值
        # （如 "abc"），int() 抛 TypeError/ValueError——按 0 兜底而非崩溃。
        try:
            size_bytes = int(data.get("size_bytes", 0) or 0)
        except (TypeError, ValueError):
            logger.warning(
                "DependencyEntry: non-numeric size_bytes %r coerced to 0",
                data.get("size_bytes"))
            size_bytes = 0
        return cls(
            kind=str(data.get("kind", "")),
            key=str(data.get("key", "")),
            status=str(data.get("status", STATUS_MISSING)),
            path=data.get("path"),
            size_bytes=size_bytes,
            sha256=data.get("sha256"),
            version=data.get("version"),
            detail=str(data.get("detail", "")),
        )


@dataclass
class DependencyReport:
    """工程依赖报告（可序列化、可对比）。"""

    entries: List[DependencyEntry] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def missing(self) -> List[DependencyEntry]:
        return [e for e in self.entries if e.status != STATUS_OK]

    @property
    def ok_count(self) -> int:
        return sum(1 for e in self.entries if e.status == STATUS_OK)

    def by_kind(self, kind: str) -> List[DependencyEntry]:
        return [e for e in self.entries if e.kind == kind]

    def summary(self) -> str:
        if not self.entries:
            return "无外部依赖"
        lines = []
        for kind in (KIND_LUT, KIND_PLUGIN, KIND_AI_MODEL):
            subset = self.by_kind(kind)
            if not subset:
                continue
            missing = [e for e in subset if e.status != STATUS_OK]
            lines.append(
                f"{kind}: {len(subset) - len(missing)}/{len(subset)} 可用")
            for entry in missing:
                lines.append(f"  [缺失] {entry.key} — {entry.detail}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {"entries": [e.to_dict() for e in self.entries]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DependencyReport":
        # AUD-0820: 报告可嵌入工程快照或独立 JSON 导出——from_dict 消费的
        # 是外部数据。entries 中混入字符串/数字等非 dict 元素时跳过并记
        # warning，读取方不再以 AttributeError 崩溃；单条数值字段损坏由
        # DependencyEntry.from_dict 内部兜底。
        entries: List[DependencyEntry] = []
        for raw in data.get("entries", []):
            if not isinstance(raw, dict):
                logger.warning(
                    "DependencyReport.from_dict: skipped non-dict entry (%s)",
                    type(raw).__name__)
                continue
            try:
                entries.append(DependencyEntry.from_dict(raw))
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "DependencyReport.from_dict: skipped malformed entry: %s", exc)
                continue
        return cls(entries=entries)


# ---------------------------------------------------------------------------
# 工程数据收集
# ---------------------------------------------------------------------------

def _stack_layers(stack: Any) -> List[Dict[str, Any]]:
    """取调色栈的完整 Layer 快照（v2），非 dict 项忽略。"""
    layers = getattr(stack, "layers", None) or ()
    return [d for d in layers if isinstance(d, dict)]


def collect_lut_paths(project: Any) -> List[str]:
    """收集工程所有调色栈引用的 LUT 路径（去重、保持发现顺序）。"""
    seen: Dict[str, None] = {}
    stacks = getattr(project, "color_stacks", {}) or {}
    for stack in stacks.values():
        lut = getattr(stack, "lut_path", None)
        if lut:
            seen.setdefault(str(lut), None)
        for layer in _stack_layers(stack):
            lut = layer.get("active_lut_path")
            if lut:
                seen.setdefault(str(lut), None)
    return list(seen.keys())


def collect_plugin_ids(project: Any) -> List[str]:
    """收集图层效果栈中启用的 DCTL 插件 ID（去重）。"""
    seen: Dict[str, None] = {}
    stacks = getattr(project, "color_stacks", {}) or {}
    for stack in stacks.values():
        for layer in _stack_layers(stack):
            for plugin in layer.get("plugins", []) or []:
                if plugin.get("type", "dctl") != "dctl":
                    continue
                if not plugin.get("enabled", True):
                    continue
                pid = plugin.get("plugin_id")
                if pid:
                    seen.setdefault(str(pid), None)
    return list(seen.keys())


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------

def _resolve_path(path: str, base_dir: Optional[str]) -> Optional[str]:
    """解析依赖路径：绝对路径直接用，相对路径相对工程目录。"""
    if os.path.isabs(path):
        return path
    if base_dir:
        candidate = os.path.join(base_dir, path)
        if os.path.exists(candidate):
            return candidate
        return candidate  # 保留解析结果供 missing 报告定位
    return path


def _entry_for_file(kind: str, key: str, path: Optional[str],
                    version: Optional[str] = None,
                    detail: str = "") -> DependencyEntry:
    if path and os.path.isfile(path):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        return DependencyEntry(
            kind=kind, key=key, status=STATUS_OK, path=path,
            size_bytes=size, sha256=_file_sha256(path) or None,
            version=version, detail=detail)
    return DependencyEntry(
        kind=kind, key=key, status=STATUS_MISSING, path=path,
        version=version,
        detail=detail or "文件不存在（工程移动后需重新链接或导入）")


def build_dependency_report(
    project: Any,
    base_dir: Optional[str] = None,
    plugin_manager: Any = None,
    model_manager: Any = None,
) -> DependencyReport:
    """扫描工程依赖并生成报告。

    Args:
        project: EditProject（``color_stacks`` 映射）
        base_dir: 工程目录（相对路径解析基准）
        plugin_manager: PluginManager（缺省用全局单例；传 False 跳过插件）
        model_manager: AIModelManager（可选，None 跳过 AI 模型段）
    """
    report = DependencyReport()

    # LUT
    for lut_path in collect_lut_paths(project):
        resolved = _resolve_path(lut_path, base_dir)
        report.entries.append(
            _entry_for_file(KIND_LUT, lut_path, resolved))

    # 插件
    if plugin_manager is not False:
        if plugin_manager is None:
            from src.features.color.core.plugin_manager import (
                get_plugin_manager,
            )
            plugin_manager = get_plugin_manager()
        for pid in collect_plugin_ids(project):
            data = plugin_manager.get_plugin(pid) if plugin_manager else None
            if data is None:
                report.entries.append(DependencyEntry(
                    kind=KIND_PLUGIN, key=pid, status=STATUS_MISSING,
                    detail="插件未注册（本机 plugins 目录中不存在）"))
                continue
            source_uri = data.get("source_uri")
            report.entries.append(_entry_for_file(
                KIND_PLUGIN, pid, source_uri,
                version=data.get("version") or None,
                detail=f"content_hash={data.get('content_hash', '')[:12]}"))

    # AI 模型（可选段：报告本机模型缓存状态）
    if model_manager is not None:
        registry = getattr(model_manager, "_registry", None) or \
            getattr(model_manager, "registry", None) or {}
        for model_id, info in registry.items():
            local_path = getattr(info, "local_path", None)
            report.entries.append(_entry_for_file(
                KIND_AI_MODEL, str(model_id), local_path,
                version=getattr(info, "version", None) or None))

    return report


def compare_reports(baseline: DependencyReport,
                    current: DependencyReport) -> List[str]:
    """对比基线与当前报告，返回差异清单（可移植性校验）。

    检测：缺失（基线 ok → 当前 missing）、内容变化（同名不同 hash）。
    返回可读字符串列表；空列表 = 完全一致。
    """
    base_map = {e.key: e for e in baseline.entries}
    curr_map = {e.key: e for e in current.entries}
    diffs: List[str] = []

    for key, base in base_map.items():
        curr = curr_map.get(key)
        if curr is None:
            diffs.append(f"{base.kind} {key}: 基线报告中存在，当前未引用")
            continue
        if base.status == STATUS_OK and curr.status != STATUS_OK:
            diffs.append(f"{base.kind} {key}: 缺失（{curr.detail}）")
        elif (base.sha256 and curr.sha256
              and base.sha256 != curr.sha256):
            diffs.append(
                f"{base.kind} {key}: 同名文件内容不同 "
                f"({base.sha256[:12]} → {curr.sha256[:12]})，"
                f"禁止静默替换使用")

    for key in curr_map:
        if key not in base_map:
            diffs.append(f"{curr_map[key].kind} {key}: 新增依赖（基线无）")
    return diffs
