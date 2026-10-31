"""P6.2 ComfyUI 只读 importer + preflight gate（plan §14.1 step 3 / §9.4 #8）。

> 实现只读 Comfy workflow importer：输出节点/模型/自定义节点依赖报告；禁止导入时
> 执行任何 custom node。

提供两件事（plan §14.1 step 3 / §9.4 #8）：

1. :class:`ComfyWorkflowImporter` — 把 ComfyUI API-format JSON 解析为
   :class:`ComfyWorkflowSnapshot` + :class:`ComfyImportReport`（节点清单 / 模型依赖 /
   自定义节点依赖）。**不执行任何 custom node 代码**（红线 §14.1 step 3）。
2. :class:`ComfyPreflightChecker` — 提交 Comfy server 前的 gate。检查 server 是否具备
   workflow 所需节点/模型/绑定；缺失 → :class:`ComfyPreflightResult` 报 ERROR 并
   ``allowed=False``。**禁止**在 preflight 未通过时提交 workflow（§9.4 #8）。

红线（plan §14.1 step 3 / §9.4 / §1.4.2 / §13.7）：

- importer **不执行** custom node 代码（只读解析；未知 class_type 仅记为元数据）。
- importer **不联网**（不查 Comfy server）；server 状态由调用方注入
  :class:`ComfyServerCapabilities`。
- preflight 失败 → ``allowed=False``；调用方不得创建 :class:`ComfyJobRef`。
- 缺失模型 → preflight 报 ERROR；建议动作**不含**自动下载（§13.7："首版不自动安装"），
  只提示用户在自己配置的 Comfy 环境中解决。
- 未 pin commit 的 custom node 默认拒绝（``require_pinned_custom_nodes=True``；§13.7）。
- preflight 不替代 consent gate（生成仍需用户单独确认上传范围 + 成本）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Tuple

from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyCustomNodeRequirement,
    ComfyInputBinding,
    ComfyModelRequirement,
    ComfyWorkflowSnapshot,
)


# ---------------------------------------------------------------------- #
# ComfyServerCapabilities —— server 状态（调用方注入）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyServerCapabilities:
    """Comfy server 提供的能力（plan §9.4 #3 / preflight gate 输入）。

    由调用方（宿主）通过查询 Comfy server 的 ``/object_info`` / ``/models`` 接口构造。
    importer / preflight **不联网**；这里只是 DTO。

    Attributes:
        profile_id: 关联的 :class:`ComfyConnectionProfile.profile_id`。
        available_class_types: server 当前可用的 class_type 集合（含内置 + 已安装的
            custom node class_type）。
        available_models: server 当前可用的模型文件名集合。
        custom_node_repos: server 已安装的 custom node 仓库 URL（带可选 ``@commit``）。
    """

    profile_id: str
    available_class_types: FrozenSet[str]
    available_models: FrozenSet[str]
    custom_node_repos: FrozenSet[str] = frozenset()

    def has_class_type(self, class_type: str) -> bool:
        return class_type in self.available_class_types

    def has_model(self, model_name: str) -> bool:
        return model_name in self.available_models

    def has_custom_node_repo(self, repo_url: str) -> bool:
        return repo_url in self.custom_node_repos


# ---------------------------------------------------------------------- #
# ComfyImportReport —— importer 输出
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyImportReport:
    """importer 的输出报告（plan §14.1 step 3 依赖报告）。

    Attributes:
        snapshot: 解析后的 :class:`ComfyWorkflowSnapshot`。
        required_bindings: 调用方准备好的绑定（用于 preflight 检查必填）。
        required_unbound_slot_ids: 调用方声明的"必填但尚未绑定"的 slot_id 列表
            （用于 preflight 报 MISSING_BINDING）。
    """

    snapshot: ComfyWorkflowSnapshot
    required_bindings: Tuple[ComfyInputBinding, ...] = ()
    required_unbound_slot_ids: Tuple[str, ...] = ()

    @property
    def required_models(self) -> Tuple[ComfyModelRequirement, ...]:
        return self.snapshot.required_models

    @property
    def required_custom_nodes(self) -> Tuple[ComfyCustomNodeRequirement, ...]:
        return self.snapshot.required_custom_nodes

    @property
    def node_count(self) -> int:
        return self.snapshot.node_count

    @property
    def output_node_count(self) -> int:
        return sum(1 for n in self.snapshot.nodes if n.is_output)

    @property
    def unknown_custom_node_count(self) -> int:
        return sum(1 for n in self.snapshot.nodes if n.is_unknown_custom_node)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "required_models": [m.to_dict() for m in self.required_models],
            "required_custom_nodes": [c.to_dict() for c in self.required_custom_nodes],
            "node_count": self.node_count,
            "output_node_count": self.output_node_count,
            "unknown_custom_node_count": self.unknown_custom_node_count,
            "required_bindings": [b.to_dict() for b in self.required_bindings],
            "required_unbound_slot_ids": list(self.required_unbound_slot_ids),
        }


# ---------------------------------------------------------------------- #
# ComfyWorkflowImporter
# ---------------------------------------------------------------------- #


class ComfyWorkflowImporter:
    """ComfyUI API-format workflow 的只读 importer（plan §14.1 step 3）。

    红线：导入**不执行**任何 custom node 代码；只做静态解析 + 元数据抽取。
    """

    def import_from_dict(
        self,
        workflow_id: str,
        raw: Any,
        *,
        display_name: str = "",
    ) -> ComfyWorkflowSnapshot:
        """从 API-format dict 构造 snapshot（不执行 custom node）。"""
        return ComfyWorkflowSnapshot.from_api_format(
            workflow_id=workflow_id, raw=raw, display_name=display_name,
        )

    def import_from_json_string(
        self,
        workflow_id: str,
        json_str: str,
        *,
        display_name: str = "",
    ) -> ComfyWorkflowSnapshot:
        """从 JSON 字符串构造 snapshot。"""
        try:
            raw = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise ValueError(
                f"API-format workflow JSON must be an object mapping node_id -> "
                f"{{class_type, inputs}}; got {type(raw).__name__}"
            )
        return self.import_from_dict(
            workflow_id=workflow_id, raw=raw, display_name=display_name,
        )

    def import_and_report(
        self,
        workflow_id: str,
        raw: Any,
        *,
        display_name: str = "",
        required_bindings: Iterable[ComfyInputBinding] = (),
        required_unbound_slot_ids: Iterable[str] = (),
    ) -> ComfyImportReport:
        """import + 产出 :class:`ComfyImportReport`（含依赖摘要）。"""
        snap = self.import_from_dict(
            workflow_id=workflow_id, raw=raw, display_name=display_name,
        )
        return ComfyImportReport(
            snapshot=snap,
            required_bindings=tuple(required_bindings),
            required_unbound_slot_ids=tuple(required_unbound_slot_ids),
        )


# ---------------------------------------------------------------------- #
# PreflightDenyCode / ComfyPreflightResult
# ---------------------------------------------------------------------- #


class PreflightDenyCode:
    """preflight 拒绝原因（plan §9.4 #8）。"""

    MISSING_NODE = "missing_node"                       # server 缺 class_type
    MISSING_CUSTOM_NODE = "missing_custom_node"         # custom node 仓库未安装
    MISSING_MODEL = "missing_model"                     # 模型未安装
    MISSING_BINDING = "missing_binding"                 # 必填 slot 未绑定
    UNPINNED_CUSTOM_NODE = "unpinned_custom_node"       # custom node 未 pin commit

    ALL: Tuple[str, ...] = (
        MISSING_NODE, MISSING_CUSTOM_NODE, MISSING_MODEL,
        MISSING_BINDING, UNPINNED_CUSTOM_NODE,
    )


@dataclass(frozen=True)
class _PreflightDiagnostic:
    """preflight 内部诊断（避免直接依赖 ai_editing.Diagnostics，保持模块独立）。"""

    severity: str       # "error" / "warning" / "info"
    code: str           # PreflightDenyCode.*
    message: str
    detail: str = ""
    suggested_action: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True)
class ComfyPreflightResult:
    """preflight 检查结果（plan §9.4 #8）。"""

    allowed: bool
    deny_code: str | None = None
    diagnostics: Tuple[_PreflightDiagnostic, ...] = ()

    @property
    def has_blocking_errors(self) -> bool:
        return any(d.is_error for d in self.diagnostics)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "deny_code": self.deny_code,
            "diagnostics": [
                {
                    "severity": d.severity,
                    "code": d.code,
                    "message": d.message,
                    "detail": d.detail,
                    "suggested_action": d.suggested_action,
                }
                for d in self.diagnostics
            ],
        }


# ---------------------------------------------------------------------- #
# ComfyPreflightChecker —— 执行前 gate
# ---------------------------------------------------------------------- #


class ComfyPreflightChecker:
    """提交 Comfy server 前的 gate（plan §9.4 #8）。

    检查项（任一失败 → ``allowed=False``）：

    1. **MISSING_NODE**：workflow 的 class_type 是否都在 ``caps.available_class_types``。
    2. **MISSING_MODEL**：workflow 引用的模型是否都在 ``caps.available_models``。
    3. **MISSING_BINDING**：``report.required_unbound_slot_ids`` 是否为空（必填 slot 已绑）。
    4. **UNPINNED_CUSTOM_NODE**（可选，``require_pinned_custom_nodes=True``）：custom node
       是否都 pin 了 commit hash。

    红线：
    - 缺失模型 → ERROR；建议动作**不**含自动下载（§13.7）。
    - preflight 失败 → 调用方不得创建 :class:`ComfyJobRef`。
    """

    def __init__(self, *, require_pinned_custom_nodes: bool = True) -> None:
        self._require_pinned = require_pinned_custom_nodes

    def check(
        self,
        report: ComfyImportReport,
        *,
        capabilities: ComfyServerCapabilities,
    ) -> ComfyPreflightResult:
        diags: List[_PreflightDiagnostic] = []
        deny_code: str | None = None

        # 1. MISSING_NODE：每个 class_type 必须在 server
        missing_nodes: List[str] = []
        for node in report.snapshot.nodes:
            if not capabilities.has_class_type(node.class_type):
                missing_nodes.append(node.class_type)
        if missing_nodes:
            unique_missing = sorted(set(missing_nodes))
            is_custom = any(n.is_unknown_custom_node for n in report.snapshot.nodes
                            if n.class_type in unique_missing)
            code = (PreflightDenyCode.MISSING_CUSTOM_NODE if is_custom
                    else PreflightDenyCode.MISSING_NODE)
            diags.append(_PreflightDiagnostic(
                severity="error",
                code=code,
                message=f"server missing {len(unique_missing)} node type(s)",
                detail=f"missing class_type(s): {unique_missing}",
                suggested_action=(
                    "install the missing custom node in your configured ComfyUI "
                    "environment, then re-run preflight (Topos will NOT auto-install)"
                ),
            ))
            if deny_code is None:
                deny_code = code

        # 2. MISSING_MODEL：模型必须在 server
        missing_models = [
            m.model_name for m in report.required_models
            if not capabilities.has_model(m.model_name)
        ]
        if missing_models:
            unique_models = sorted(set(missing_models))
            diags.append(_PreflightDiagnostic(
                severity="error",
                code=PreflightDenyCode.MISSING_MODEL,
                message=f"server missing {len(unique_models)} model(s)",
                detail=f"missing model(s): {unique_models}",
                suggested_action=(
                    "manually place the missing model file in your ComfyUI models "
                    "directory, then re-run preflight"
                ),
            ))
            if deny_code is None:
                deny_code = PreflightDenyCode.MISSING_MODEL

        # 3. MISSING_BINDING：必填 slot 未绑定
        if report.required_unbound_slot_ids:
            diags.append(_PreflightDiagnostic(
                severity="error",
                code=PreflightDenyCode.MISSING_BINDING,
                message=(
                    f"{len(report.required_unbound_slot_ids)} required input slot(s) "
                    "still unbound"
                ),
                detail=f"unbound slot_id(s): {list(report.required_unbound_slot_ids)}",
                suggested_action=(
                    "bind Topos artifacts to the listed workflow input slots, "
                    "then re-run preflight"
                ),
            ))
            if deny_code is None:
                deny_code = PreflightDenyCode.MISSING_BINDING

        # 4. UNPINNED_CUSTOM_NODE（可选）：workflow 用到了 custom node（class_type 不在
        #    内置白名单），server 提供了它的 repo，但 repo 未 pin commit → 拒绝（§13.7）。
        if self._require_pinned:
            # workflow 引用的 custom node class_type（来自 importer 标记）
            # server 已安装但未 pin commit 的 repo（约定 repo 字符串形如
            # "https://github.com/x/y@commit_hash"；无 "@..." 视为未 pin）
            unpinned_repos: List[str] = []
            pinned_repos: List[str] = []
            for repo in capabilities.custom_node_repos:
                if "@" in repo:
                    base, _, commit = repo.partition("@")
                    if commit:
                        pinned_repos.append(base)
                    else:
                        unpinned_repos.append(repo)
                else:
                    unpinned_repos.append(repo)
            # 同时收集 importer 报告里的 custom node 依赖条目。AUD-23023：
            # from_api_format 对未知 custom node 恒写 ``repo_url=""``（无法从
            # workflow JSON 得知 repo 信息）。§13.7 红线是"未 pin 默认拒绝"——
            # repo 信息不可得即**无法验证 pin**，同样拒绝（此前 ``repo_url and
            # ...`` 使该数据源恒为空，UNPINNED_CUSTOM_NODE 门禁不可达）。
            for c in report.snapshot.required_custom_nodes:
                if not c.has_pinned_commit:
                    label = c.repo_url or (
                        f"<unknown repo for {c.display_name or 'custom node'}>"
                    )
                    if label not in unpinned_repos:
                        unpinned_repos.append(label)
            if unpinned_repos:
                diags.append(_PreflightDiagnostic(
                    severity="error",
                    code=PreflightDenyCode.UNPINNED_CUSTOM_NODE,
                    message=f"{len(unpinned_repos)} custom node repo(s) not pinned to a commit",
                    detail=f"unpinned repo(s): {unpinned_repos}",
                    suggested_action=(
                        "pin each custom node repo to a specific commit hash "
                        "(format: 'https://...@<commit>'), then re-run preflight"
                    ),
                ))
                if deny_code is None:
                    deny_code = PreflightDenyCode.UNPINNED_CUSTOM_NODE

        allowed = not diags  # 任何诊断 → 拒绝
        return ComfyPreflightResult(
            allowed=allowed,
            deny_code=deny_code,
            diagnostics=tuple(diags),
        )
