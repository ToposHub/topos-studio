"""P7.1 ComfyUI Workflow 导出（plan §9.4 #8 / §1.4.2 C2 闭环）。

> 提供"导出回 ComfyUI API format"，但仅对 ``ComfyUI Workflow`` 子图及其绑定参数保证等价；
> Topos 原生 Agent、审批、时间线和 MCP 节点导出时生成注释/占位，不伪装成可直接执行的
> Comfy 节点。

提供（plan §9.4 #8）：

- :class:`ComfyWorkflowExporter` — 把 :class:`ComfyWorkflowSnapshot` 导出回 ComfyUI
  API-format JSON。
    - :meth:`export_snapshot` — 纯 Comfy 子图导出（equivalence_guaranteed=True）。
    - :meth:`export_with_native_nodes` — 混合图导出（Comfy 子图 + Topos 原生节点占位）。
- :class:`NativeNodePlaceholder` / :class:`ComfyPlaceholderKind` — Topos 原生节点的占位
  表示（class_type 用 ``_ToposNativePlaceholder_<kind>``，Comfy 不执行）。
- :class:`ExportResult` — 导出结果（API-format JSON + warnings + skipped_native_specs）。

红线（plan §9.4 #8 / §0 / §1.4.2）：

- **仅对 ComfyUI Workflow 子图保证等价**：纯 Comfy 子图 → ``equivalence_guaranteed=True``。
- **原生节点不伪装**：Agent / 审批 / 时间线 / MCP 节点 → ``_ToposNativePlaceholder_*``
  class_type（下划线前缀，Comfy 不执行）；导出结果 ``equivalence_guaranteed=False`` +
  warning。
- **不写 credential**：导出 JSON 不含 API key / credential_ref / secret（§0 第四条）。
- **只读**：导出不联网 / 不执行任何东西。
- 导出 JSON 含 ``_topos_meta``（workflow_snapshot_hash + schema_version），可追溯。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyInputBinding,
    ComfyWorkflowSnapshot,
)


# ---------------------------------------------------------------------- #
# ComfyPlaceholderKind —— 原生节点占位类型
# ---------------------------------------------------------------------- #


class ComfyPlaceholderKind:
    """Topos 原生节点导出为占位的类型（plan §9.4 #8）。

    红线：原生节点导出为占位（class_type ``_ToposNativePlaceholder_<kind>``），
    **不**伪装成可执行 Comfy 节点。
    """

    NONE = "none"               # Comfy 子图节点（不需占位，直接导出 API-format）
    AGENT = "agent"             # topos.agent.* （子智能体 / TaskGraph）
    APPROVAL = "approval"       # topos.user.approval / topos.timeline.preview_diff
    TIMELINE = "timeline"       # topos.timeline.apply_approved_plan / propose_*
    MCP = "mcp"                 # topos.mcp.* （MCP 出站）
    ANALYSIS = "analysis"       # topos.analysis.* （ASR/OCR/scene cut）
    INPUT = "input"             # topos.media.* / topos.timeline.current
    OUTPUT = "output"           # topos.report.*
    GENERATION = "generation"   # topos.generation.* / topos.tts.* （非 Comfy 入口）

    ALL: Tuple[str, ...] = (
        NONE, AGENT, APPROVAL, TIMELINE, MCP, ANALYSIS, INPUT, OUTPUT, GENERATION,
    )

    @classmethod
    def for_spec_id(cls, spec_id: str) -> str:
        """根据 spec_id 推断占位类型。

        ComfyUI Workflow 节点（``topos.comfy.workflow`` / ``topos.comfy.*``）→ NONE
        （它们导出为真实 API-format）。
        """
        if spec_id.startswith("topos.comfy."):
            return cls.NONE
        if spec_id.startswith("topos.agent."):
            return cls.AGENT
        if spec_id.startswith("topos.mcp."):
            return cls.MCP
        if spec_id.startswith("topos.analysis."):
            return cls.ANALYSIS
        if spec_id.startswith("topos.media.") or spec_id == "topos.timeline.current":
            return cls.INPUT
        if spec_id.startswith("topos.report."):
            return cls.OUTPUT
        if spec_id.startswith("topos.generation.") or spec_id.startswith("topos.tts."):
            return cls.GENERATION
        if spec_id.startswith("topos.user.") or spec_id == "topos.timeline.preview_diff":
            return cls.APPROVAL
        if spec_id.startswith("topos.timeline."):
            return cls.TIMELINE
        if spec_id.startswith("topos.caption.") or spec_id.startswith("topos.text."):
            return cls.GENERATION  # 文本/字幕处理
        return cls.GENERATION  # 退化：未知 Topos 节点 → GENERATION 占位


# ---------------------------------------------------------------------- #
# NativeNodePlaceholder
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class NativeNodePlaceholder:
    """Topos 原生节点在 ComfyUI 导出中的占位表示（plan §9.4 #8）。

    红线：占位 class_type 用 ``_ToposNativePlaceholder_<kind>``（下划线前缀），
    Comfy server 不会执行（未知 class_type → 报错而非运行）。
    """

    topos_node_id: str
    topos_spec_id: str
    kind: str
    note: str = ""

    def __post_init__(self) -> None:
        if not self.topos_node_id:
            raise ValueError("topos_node_id cannot be empty")
        if not self.topos_spec_id:
            raise ValueError("topos_spec_id cannot be empty")
        if self.kind not in ComfyPlaceholderKind.ALL:
            raise ValueError(f"invalid kind: {self.kind!r}")

    def to_comfy_dict(self) -> Dict[str, Any]:
        """转为 Comfy API-format 节点 dict（占位；不可执行）。

        红线：class_type 用 ``_ToposNativePlaceholder_<kind>``（下划线前缀）。
        """
        return {
            "class_type": f"_ToposNativePlaceholder_{self.kind}",
            "inputs": {
                "topos_node_id": self.topos_node_id,
                "topos_spec_id": self.topos_spec_id,
                "kind": self.kind,
                "note": self.note,
                "_warning": (
                    "This is a Topos native node placeholder. It is NOT a real "
                    "ComfyUI node and cannot be executed by a Comfy server."
                ),
            },
        }


# ---------------------------------------------------------------------- #
# ExportResult
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportResult:
    """导出结果（plan §9.4 #8）。"""

    api_format_json: str
    suggested_filename: str
    equivalence_guaranteed: bool
    warnings: Tuple[str, ...] = ()
    skipped_native_specs: Tuple[str, ...] = ()
    workflow_snapshot_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "api_format_json": self.api_format_json,
            "suggested_filename": self.suggested_filename,
            "equivalence_guaranteed": self.equivalence_guaranteed,
            "warnings": list(self.warnings),
            "skipped_native_specs": list(self.skipped_native_specs),
            "workflow_snapshot_hash": self.workflow_snapshot_hash,
        }


# ---------------------------------------------------------------------- #
# ComfyWorkflowExporter
# ---------------------------------------------------------------------- #


# credential 相关字段名（导出时清除；红线 §0 第四条）
_CREDENTIAL_KEYS: Tuple[str, ...] = (
    "api_key", "apikey", "secret", "password", "credential_ref",
    "bearer", "authorization", "token",
)


class ComfyWorkflowExporter:
    """把 :class:`ComfyWorkflowSnapshot` 导出回 ComfyUI API-format JSON（plan §9.4 #8）。

    红线：
    - 纯 Comfy 子图导出 → ``equivalence_guaranteed=True``。
    - 含 Topos 原生节点 → 占位（``_ToposNativePlaceholder_*``）+ ``equivalence_guaranteed=False``。
    - 导出 JSON 不含 credential（自动清除）。
    - 只读：不联网 / 不执行。
    """

    def export_snapshot(
        self,
        snapshot: ComfyWorkflowSnapshot,
        *,
        bindings: Iterable[ComfyInputBinding] = (),
    ) -> ExportResult:
        """导出纯 Comfy 子图（保证等价）。"""
        return self._export(
            snapshot=snapshot,
            bindings=tuple(bindings),
            native_placeholders=(),
        )

    def export_with_native_nodes(
        self,
        snapshot: ComfyWorkflowSnapshot,
        native_placeholders: Iterable[NativeNodePlaceholder],
        *,
        bindings: Iterable[ComfyInputBinding] = (),
    ) -> ExportResult:
        """导出混合图（Comfy 子图 + Topos 原生节点占位）。

        红线（§9.4 #8）：原生节点占位化；equivalence_guaranteed=False。
        """
        return self._export(
            snapshot=snapshot,
            bindings=tuple(bindings),
            native_placeholders=tuple(native_placeholders),
        )

    # ---------------- 内部 ---------------- #

    def _export(
        self,
        *,
        snapshot: ComfyWorkflowSnapshot,
        bindings: Tuple[ComfyInputBinding, ...],
        native_placeholders: Tuple[NativeNodePlaceholder, ...],
    ) -> ExportResult:
        # 1. 从 snapshot.raw_payload 深拷贝
        payload: Dict[str, Any] = copy.deepcopy(dict(snapshot.raw_payload))
        # 应用绑定
        for b in bindings:
            self._apply_binding(payload, b)
        # 清除 credential 字段（红线 §0 第四条）
        self._scrub_credentials(payload)
        # 2. 加 _topos_meta（hash + schema_version）
        payload["_topos_meta"] = {
            "workflow_snapshot_hash": snapshot.content_hash,
            "schema_version": snapshot.schema_version,
            "workflow_id": snapshot.workflow_id,
            "display_name": snapshot.display_name,
            "exported_at_kind": (
                "pure_comfy" if not native_placeholders else "mixed_topos_comfy"
            ),
        }
        # 3. 占位化原生节点
        warnings: List[str] = []
        skipped: List[str] = []
        for ph in native_placeholders:
            # 用稳定的占位 node_id（避免冲突）
            placeholder_node_id = f"_topos_{ph.topos_node_id}"
            payload[placeholder_node_id] = ph.to_comfy_dict()
            skipped.append(ph.topos_spec_id)
        if native_placeholders:
            warnings.append(
                f"{len(native_placeholders)} Topos native node(s) exported as "
                "placeholders (class_type _ToposNativePlaceholder_*). These are "
                "NOT executable ComfyUI nodes; equivalence is NOT guaranteed for "
                "the full mixed graph."
            )
            equivalence = False
        else:
            equivalence = True
        # 序列化
        api_format_json = json.dumps(payload, indent=2, sort_keys=True)
        filename = self._suggest_filename(snapshot, mixed=bool(native_placeholders))
        return ExportResult(
            api_format_json=api_format_json,
            suggested_filename=filename,
            equivalence_guaranteed=equivalence,
            warnings=tuple(warnings),
            skipped_native_specs=tuple(skipped),
            workflow_snapshot_hash=snapshot.content_hash,
        )

    @staticmethod
    def _apply_binding(payload: Dict[str, Any], binding: ComfyInputBinding) -> None:
        node = payload.get(binding.slot.node_id)
        if not isinstance(node, dict):
            return
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            return
        if binding.slot.input_name not in inputs:
            return
        val: Any = binding.resolved_value
        if binding.slot.artifact_type == "integer":
            try:
                val = int(binding.resolved_value)
            except ValueError:
                pass
        elif binding.slot.artifact_type == "float":
            try:
                val = float(binding.resolved_value)
            except ValueError:
                pass
        inputs[binding.slot.input_name] = val

    @staticmethod
    def _scrub_credentials(payload: Dict[str, Any]) -> None:
        """递归清除 credential 字段（红线 §0 第四条）。"""
        for key in list(payload.keys()):
            low = key.lower()
            if any(bad in low for bad in _CREDENTIAL_KEYS):
                payload.pop(key, None)
                continue
            v = payload[key]
            if isinstance(v, dict):
                ComfyWorkflowExporter._scrub_credentials(v)

    @staticmethod
    def _suggest_filename(
        snapshot: ComfyWorkflowSnapshot, *, mixed: bool,
    ) -> str:
        base = (snapshot.workflow_id or "comfy_workflow").replace("/", "_")
        suffix = "_mixed" if mixed else ""
        return f"{base}{suffix}.json"


__all__ = [
    "ComfyPlaceholderKind",
    "ComfyWorkflowExporter",
    "ExportResult",
    "NativeNodePlaceholder",
]
