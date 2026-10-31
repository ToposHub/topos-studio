"""TAEP Tool Facade — transport-neutral 工具面（Phase 6）。

把 P1–P5 的本地服务封装给一个或多个模型供应商，而不是绑定某一家 SDK
（TAEP §7）。MCP / HTTP / WebSocket adapter 只负责认证与序列化，工具语义全部
收口在此。

工具表（TAEP §7）：

| 工具 | 权限 | 输入 | 输出 |
| topos.get_capabilities | read | 无/feature scope | 动态 manifest |
| topos.get_project_snapshot | read | 范围/timeline/detail | 脱敏 snapshot + revision |
| topos.inspect_subtitle_track | read | track_id/options | 字幕质量报告 |
| topos.search_assets | read | 文本/标签/时段/类型 | ID 列表与摘要 |
| topos.get_asset_analysis | read | asset_id/分析类型 | scene/transcript/tag/audio |
| topos.validate_edit_plan | preview | EditPlan | diff + diagnostics |
| topos.apply_edit_plan | write+approval | 已验证 plan + token | new revision + undo group |
| topos.render_preview | expensive+approval | timeline/range/preset | job id |
| topos.request_export | file-write+confirmation | ExportRequest | preflight + job |
| topos.get_job_status/cancel_job | scoped | job id | 状态/进度/产物 |

它**不能**调用：鼠标点击时间线、执行 Python 字符串、读取任意路径、调用 shell。

prompt injection 隔离（TAEP §12 P6）：素材文件名、字幕文本、OCR/网页内容均视为
不可信数据，不能改变工具权限或系统指令。本 facade 在所有返回里对不可信文本做
明确标记与边界控制。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from src.features.edit.ai_editing.approval_service import ApprovalGrant, ApprovalService
from src.features.edit.ai_editing.capability_catalog import (
    CapabilityCatalog,
    CapabilityCatalogEntry,
    DOMAIN_SYSTEM,
    SUPPORTED_DOMAINS,
    create_default_capability_catalog,
)
from src.features.edit.ai_editing.diagnostics import (
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.edit_api import EditSession
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.export_service import ExportRequest
from src.features.edit.ai_editing.jobs import JobRegistry
from src.features.edit.ai_editing.models import DetailLevel, EditPlan
from src.features.composite.edit_service import CompositeEditPlan
from src.shared.render_diagnostics import (
    SOURCE_COLOR_PIPELINE,
    SOURCE_COMPOSITION,
    SOURCE_EFFECT_PASS,
    get_render_diagnostic_sink,
)


# ---------------------------------------------------------------------- #
# 工具权限级（TAEP §7）
# ---------------------------------------------------------------------- #


class ToolPermission(str, Enum):
    """工具权限级。外部 LLM 初始只获 read/preview；write/expensive 需 approval。"""

    READ = "read"
    PREVIEW = "preview"  # 编译+校验+diff，不写工程
    PROPOSAL = "proposal"  # 生成 brief patch / variant / decision（不改 timeline）
    WRITE = "write"  # apply_edit_plan（需 approval token）
    EXPENSIVE = "expensive"  # render_preview（需 approval policy）
    FILE_WRITE = "file_write"  # request_export（需 confirmation）


class ToolExecutionMode(str, Enum):
    """调度模式（借鉴 DeepSeek Harness 的工具执行流水线）。

    ``PARALLEL`` 只给明确声明为纯只读的工具使用；未知工具和所有可能
    改变状态的工具默认走 ``EXCLUSIVE`` 屏障。
    """

    EXCLUSIVE = "exclusive"
    PARALLEL = "parallel"


# This is deliberately narrower than the READ permission tier. These tools
# only consult immutable/help data or the lock-protected diagnostic sink. A
# read-only tool backed by the live editor/session state remains exclusive
# until its host proves a stronger concurrency contract.
PARALLEL_SAFE_READ_TOOLS: frozenset[str] = frozenset({
    "topos.get_capabilities",
    "topos.search_help",
    "topos.explain_diagnostic",
    "topos.get_render_diagnostics",
})


# ---------------------------------------------------------------------- #
# 工具定义
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolDefinition:
    """单个语义工具的定义（供 MCP/HTTP host 注册）。"""

    name: str  # 如 "topos.get_capabilities"
    permission: ToolPermission
    description: str
    input_schema: dict[str, Any]  # JSON Schema 片段
    output_description: str = ""
    execution_mode: ToolExecutionMode = ToolExecutionMode.EXCLUSIVE

    def __post_init__(self) -> None:
        """Fail closed when an extension supplies an unknown scheduler mode."""

        raw_mode = (
            self.execution_mode.value
            if isinstance(self.execution_mode, ToolExecutionMode)
            else str(self.execution_mode).lower()
        )
        try:
            mode = ToolExecutionMode(raw_mode)
        except ValueError:
            mode = ToolExecutionMode.EXCLUSIVE
        object.__setattr__(self, "execution_mode", mode)

    def to_openai_tool(self) -> dict[str, Any]:
        """转换为 OpenAI tools API 格式 ``{"type":"function","function":{...}}``。"""

        perm = self.permission.value if hasattr(self.permission, "value") else str(self.permission)
        desc = f"[{perm}] {self.description}".strip()
        if self.output_description:
            desc = f"{desc}\n→ {self.output_description}"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": desc,
                "parameters": dict(self.input_schema),
            },
        }


def list_tool_definitions() -> list[ToolDefinition]:
    """返回全部工具定义（TAEP §7 工具表）。

    供 MCP/HTTP adapter 注册；host 只暴露当前会话有权限的工具。
    """
    return [
        ToolDefinition(
            name="topos.get_capabilities", permission=ToolPermission.READ,
            description="返回当前软件真正支持的能力、值域和限制（动态 manifest）",
            input_schema={"type": "object", "properties": {}},
            output_description="CapabilityManifest",
            execution_mode=ToolExecutionMode.PARALLEL,
        ),
        ToolDefinition(
            name="topos.get_project_snapshot", permission=ToolPermission.READ,
            description="返回脱敏的只读工程快照（不含本机路径/secret/二进制）",
            input_schema={
                "type": "object",
                "properties": {
                    "detail_level": {"type": "string", "enum": ["minimal", "structure", "full"]},
                    "timeline_id": {"type": "string"},
                    "asset_limit": {"type": "integer"},
                },
            },
            output_description="ProjectSnapshot",
        ),
        ToolDefinition(
            name="topos.get_composite_snapshot", permission=ToolPermission.READ,
            description="返回脱敏的 Composite 节点、参数定义、表达式和关键帧轨道快照",
            input_schema={"type": "object", "properties": {}},
            output_description="CompositeSnapshot",
        ),
        ToolDefinition(
            name="topos.inspect_subtitle_track", permission=ToolPermission.READ,
            description=(
                "对指定字幕轨道执行确定性的质量检查：时间范围、重叠、空文本、"
                "行数、单行长度、阅读速度和显示时长"
            ),
            input_schema={
                "type": "object",
                "required": ["track_id"],
                "properties": {
                    "track_id": {"type": "string"},
                    "timeline_id": {"type": "string"},
                    "options": {"type": "object"},
                },
            },
            output_description="SubtitleQualityReport",
        ),
        ToolDefinition(
            name="topos.search_assets", permission=ToolPermission.READ,
            description="按文本/标签/时段/媒体类型搜索素材与语义片段",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            output_description="SemanticSegment 列表",
        ),
        ToolDefinition(
            name="topos.get_asset_analysis", permission=ToolPermission.READ,
            description="返回某素材的 scene/transcript/tag/audio 分析",
            input_schema={
                "type": "object",
                "required": ["asset_id"],
                "properties": {"asset_id": {"type": "string"}},
            },
            output_description="AssetAnalysis",
        ),
        ToolDefinition(
            name="topos.validate_edit_plan", permission=ToolPermission.PREVIEW,
            description="校验 EditPlan：schema + domain，返回 diff + diagnostics，不写工程",
            input_schema={
                "type": "object",
                "required": ["plan"],
                "properties": {"plan": {"type": "object"}},
            },
            output_description="PreviewResult (diff + diagnostics)",
        ),
        ToolDefinition(
            name="topos.validate_composite_edit", permission=ToolPermission.PREVIEW,
            description="校验 Composite 编辑计划：参数、关键帧、曲线手柄和 generation，不写工程",
            input_schema={
                "type": "object", "required": ["plan"],
                "properties": {"plan": {"type": "object"}},
            },
            output_description="CompositeValidationReport",
        ),
        ToolDefinition(
            name="topos.apply_edit_plan", permission=ToolPermission.WRITE,
            description=(
                "原子提交已验证的 plan 为单个 MacroCommand。模型不能自行提供授权——"
                "授权由宿主在用户确认后附带（apply 时模型只需传 plan）。"
            ),
            input_schema={
                "type": "object",
                "required": ["plan"],
                "properties": {
                    "plan": {"type": "object"},
                    # 注意：没有 approval_token 字段。模型无法自行伪造授权。
                },
            },
            output_description="CommitResult (new revision + undo group)",
        ),
        ToolDefinition(
            name="topos.apply_composite_edit", permission=ToolPermission.WRITE,
            description=(
                "在用户批准后原子应用 Composite 编辑计划；一次调用对应一次撤销，"
                "模型不能自行提供授权 token"
            ),
            input_schema={
                "type": "object", "required": ["plan"],
                "properties": {"plan": {"type": "object"}},
            },
            output_description="CompositeCommitResult (generation + undo)",
        ),
        ToolDefinition(
            name="topos.render_preview", permission=ToolPermission.EXPENSIVE,
            description="生成短范围低质量预览 job（需 approval policy）",
            input_schema={
                "type": "object",
                "properties": {
                    "timeline_id": {"type": "string"},
                    "preset_name": {"type": "string"},
                    "range": {"type": "object"},
                },
            },
            output_description="job id + 缩略图/视频地址",
        ),
        ToolDefinition(
            name="topos.request_export", permission=ToolPermission.FILE_WRITE,
            description="创建导出提议（preflight + job），需最终 confirmation 才写文件",
            input_schema={
                "type": "object",
                "required": ["request"],
                "properties": {"request": {"type": "object"}},
            },
            output_description="preflight + job",
        ),
        ToolDefinition(
            name="topos.get_job_status", permission=ToolPermission.READ,
            description="查询 job 状态/进度/产物",
            input_schema={
                "type": "object",
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
            },
            output_description="JobHandle",
        ),
        ToolDefinition(
            name="topos.cancel_job", permission=ToolPermission.READ,
            description="请求取消 job",
            input_schema={
                "type": "object",
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
            },
            output_description="bool",
        ),
        # M6：软件使用助手（§9）—— 只读/导航提议，不写 timeline、不模拟输入。
        ToolDefinition(
            name="topos.search_help", permission=ToolPermission.READ,
            description="搜索本地帮助索引（快捷键/功能/诊断/面板），返回带来源的可引用条目",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            output_description="HelpEntry 列表（带 page_ref/shortcut_hint/prereqs）",
            execution_mode=ToolExecutionMode.PARALLEL,
        ),
        ToolDefinition(
            name="topos.explain_diagnostic", permission=ToolPermission.READ,
            description="解释一个 TAEP 诊断码：前置条件 + 最短修复路径 + 页面引用（不编造）",
            input_schema={
                "type": "object",
                "required": ["code"],
                "properties": {"code": {"type": "string"}},
            },
            output_description="HelpEntry（diagnostic）或 unknown 标记",
            execution_mode=ToolExecutionMode.PARALLEL,
        ),
        ToolDefinition(
            name="topos.show_relevant_panel", permission=ToolPermission.PROPOSAL,
            description="提议导航到某面板（仅提议；实际显示由宿主经用户确认，不模拟输入）",
            input_schema={
                "type": "object",
                "required": ["panel_id"],
                "properties": {
                    "panel_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
            output_description="navigation proposal（panel_id + reason + needs_user_confirm）",
        ),
        # 受限诊断（§9 扩展）：只读运行时编译报错，不读文件/源码/路径。
        # 让 Agent 能排查「为什么我的效果/着色器没生效」。
        ToolDefinition(
            name="topos.get_render_diagnostics", permission=ToolPermission.READ,
            description=(
                "返回最近的 shader/效果编译失败诊断（运行时捕获，不读取文件）。"
                "用于排查「为什么效果/着色器没有生效」：返回编译报错与涉及的"
                "插件/效果名（不含源码与路径）。无失败时返回空列表。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "source": {
                        "type": "string",
                        "enum": [
                            SOURCE_COLOR_PIPELINE,
                            SOURCE_COMPOSITION,
                            SOURCE_EFFECT_PASS,
                        ],
                    },
                },
            },
            output_description="CompileDiagnostic 列表（timestamp/source/subjects/message，最新在前）",
            execution_mode=ToolExecutionMode.PARALLEL,
        ),
    ]


# ---------------------------------------------------------------------- #
# prompt injection 隔离（TAEP §12 P6）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SanitizedText:
    """标记为不可信的文本（素材名/字幕/OCR/网页内容）。

    UI 与工具调用方必须把它当作**数据**，不能解析为指令。facade 在所有返回里
    对此类内容用 SanitizedText 包裹，提示宿主做边界渲染（如转义、加引号）。

    JSON transport（M0.3）：``json.dumps`` 默认无法序列化本类。为此提供
    ``to_transport_value()``，并在 ``sanitize_untrusted_for_transport`` 里把
    整棵树转成纯 JSON（``{"value": ..., "trust": "untrusted", "source": ...}``）。
    这样 MCP / HTTP adapter 对任意 tool response 都能 ``json.dumps``。
    """

    text: str
    source: str = "untrusted"  # 来源标识

    def __str__(self) -> str:
        return self.text

    def to_transport_value(self) -> dict[str, Any]:
        """转为 JSON-safe 的 transport 表示（TAEP transport 版本约定）。"""
        return {"value": self.text, "trust": "untrusted", "source": self.source}


def sanitize_untrusted(value: Any) -> Any:
    """递归地把字符串字段标记为不可信（prompt injection 隔离）。

    用于 snapshot / analysis / search 结果里的素材名、字幕文本、OCR、标签等。
    数字/bool/结构保持不变；只标记字符串叶子。

    注意：返回值仍含 ``SanitizedText`` 对象（便于 UI 渲染时识别）。如果结果要
    经 ``json.dumps``（MCP / HTTP transport），请用 ``to_json_safe`` 先转换。
    """
    if isinstance(value, str):
        return SanitizedText(value)
    if isinstance(value, dict):
        return {k: sanitize_untrusted(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(sanitize_untrusted(v) for v in value)
    return value


def to_json_safe(value: Any) -> Any:
    """把含 ``SanitizedText`` 的结构转成纯 JSON-safe 结构。

    - ``SanitizedText`` → ``{"value": str, "trust": "untrusted", "source": str}``
    - dict / list / tuple 递归（tuple → list，JSON 无元组）。
    - 其它原样返回（str/int/float/bool/None/dataclass via to_dict 都已是基础类型）。
    """
    if isinstance(value, SanitizedText):
        return value.to_transport_value()
    if isinstance(value, dict):
        return {k: to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(v) for v in value]
    return value


# ---------------------------------------------------------------------- #
# ToolFacade — 把工具调用路由到本地服务
# ---------------------------------------------------------------------- #


class ToolFacade:
    """transport-neutral 工具 facade：把语义工具路由到 EditSession / AnalysisService。

    MCP/HTTP adapter 调用 ``invoke(tool_name, args, permission_context)``；本类负责：
    - 权限门禁（write/expensive/file_write 需 approval）。
    - 路由到本地服务。
    - prompt injection 隔离（返回里的不可信文本标记）。
    - 拒绝越权请求（不能 exec / 读任意路径 / 调 shell）。
    """

    # 明确禁止的能力（TAEP §7）
    FORBIDDEN_CAPABILITIES = frozenset({
        "exec_python", "execute_code", "read_arbitrary_path",
        "call_shell", "click_ui", "inject_glsl", "download_url",
    })

    def __init__(
        self, session: EditSession, analysis_service: Any | None = None,
        audit_sink: Callable[[Any], None] | None = None,
        approval_service: ApprovalService | None = None,
        help_index: Any | None = None,
        jobs: JobRegistry | None = None,
        mcp_gateway: Any | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        plugin_registry: Any | None = None,
    ) -> None:
        self._session = session
        self._analysis = analysis_service
        self._audit_sink = audit_sink
        self._approval_service = approval_service
        # M6：本地帮助索引（§9）；None 时 search_help/explain_diagnostic 返回"未配置"。
        self._help_index = help_index
        # Preview/export and generation hosts can inject a shared registry;
        # the normal EditSession path owns one automatically.
        self._jobs = jobs or getattr(session, "jobs", None)
        # MCP 视觉网关（P3.2 出站客户端桥）；None 或不可用时 topos.mcp.* 不暴露。
        self._mcp_gateway = mcp_gateway
        self._plugin_registry = plugin_registry
        self._plugin_catalog_tools: dict[str, str] = {}
        # The enriched catalog is the runtime authority for which local tools
        # may be advertised.  A default catalog is reconciled against the
        # transport tool table, so adding a local tool cannot silently bypass
        # the planner/approval metadata contract.
        self._capability_catalog = capability_catalog or create_default_capability_catalog()
        self.refresh_plugin_capabilities()

    @property
    def capability_catalog(self) -> CapabilityCatalog:
        """Return the catalog used for runtime advertisement and admission."""

        return self._capability_catalog

    def refresh_plugin_capabilities(self) -> int:
        """Project active plugin capabilities into the shared catalog."""

        registry = self._plugin_registry
        if registry is None:
            return 0
        rows = getattr(registry, "active_tool_capabilities", lambda: ())()
        added = 0
        active_plugin_tools: dict[str, str] = {}
        for plugin_id, capability in rows:
            for tool_name in getattr(capability, "tool_names", ()):
                resolved = registry.plugin_capability_for_tool(str(tool_name))
                if resolved is not None and resolved[0] == str(plugin_id):
                    active_plugin_tools[str(tool_name)] = str(plugin_id)
        for tool_name, plugin_id in tuple(self._plugin_catalog_tools.items()):
            if tool_name not in active_plugin_tools:
                self._capability_catalog.remove(tool_name, owner_plugin_id=plugin_id)
                del self._plugin_catalog_tools[tool_name]
        for plugin_id, capability in rows:
            permission = str(getattr(capability, "permission", "read"))
            try:
                ToolPermission(permission)
            except ValueError:
                continue
            metadata = getattr(capability, "metadata", {}) or {}
            domain = str(metadata.get("domain", DOMAIN_SYSTEM))
            if domain not in SUPPORTED_DOMAINS:
                continue
            execution_mode = str(metadata.get("execution_mode", "exclusive"))
            if execution_mode not in {"exclusive", "parallel"}:
                execution_mode = "exclusive"
            for tool_name in getattr(capability, "tool_names", ()):
                if self._capability_catalog.register(CapabilityCatalogEntry(
                    name=str(tool_name),
                    domain=domain,
                    permission=permission,
                    description=str(getattr(capability, "description", "")),
                    input_schema=getattr(capability, "input_schema", {}) or {},
                    output_description=str(
                        (getattr(capability, "output_schema", {}) or {}).get("description", "")
                    ),
                    execution_mode=execution_mode,
                    owner_plugin_id=str(plugin_id),
                )):
                    added += 1
                    self._plugin_catalog_tools[str(tool_name)] = str(plugin_id)
        return added

    def list_tools(self, *, max_permission: ToolPermission = ToolPermission.READ) -> list[ToolDefinition]:
        """返回当前会话可见的工具（按权限过滤）。"""
        self.refresh_plugin_capabilities()
        order = list(ToolPermission)
        max_idx = order.index(max_permission)
        allowed = set(order[: max_idx + 1])
        tools = [
            t for t in list_tool_definitions()
            if t.permission in allowed and self._capability_catalog.get(t.name) is not None
        ]
        # MCP 网关工具（READ 级）合并进目录；不可用时 gateway 自己返回空列表。
        if self._mcp_gateway is not None:
            try:
                for t in self._mcp_gateway.tool_definitions():
                    if t.permission in allowed:
                        tools.append(t)
            except Exception:  # noqa: BLE001 — MCP 目录失败不影响本地工具
                pass
        local_names = {tool.name for tool in tools}
        if self._plugin_registry is not None:
            try:
                for plugin_id, capability in self._plugin_registry.active_tool_capabilities():
                    try:
                        permission = ToolPermission(str(capability.permission))
                    except ValueError:
                        continue
                    if permission not in allowed:
                        continue
                    execution_mode = str(
                        (getattr(capability, "metadata", {}) or {}).get(
                            "execution_mode", ToolExecutionMode.EXCLUSIVE.value,
                        )
                    )
                    mode = (
                        ToolExecutionMode.PARALLEL
                        if execution_mode == ToolExecutionMode.PARALLEL.value
                        and permission == ToolPermission.READ
                        and bool((getattr(capability, "metadata", {}) or {}).get("thread_safe"))
                        else ToolExecutionMode.EXCLUSIVE
                    )
                    for tool_name in capability.tool_names:
                        owner = self._plugin_registry.plugin_capability_for_tool(tool_name)
                        if owner is None or owner[0] != plugin_id:
                            continue
                        if tool_name in local_names:
                            continue
                        tools.append(ToolDefinition(
                            name=tool_name,
                            permission=permission,
                            description=str(capability.description),
                            input_schema=dict(capability.input_schema),
                            output_description=str(
                                (dict(capability.output_schema)).get("description", "")
                            ),
                            execution_mode=mode,
                        ))
                        local_names.add(tool_name)
            except Exception:  # noqa: BLE001 - plugin discovery must fail closed
                pass
        return tools

    def is_tool_declared(self, tool_name: str) -> bool:
        """Whether a tool is declared by the catalog or an active MCP gateway."""

        if self._capability_catalog.get(tool_name) is not None:
            return True
        if self._mcp_gateway is not None and tool_name.startswith("topos.mcp."):
            try:
                return any(
                    getattr(definition, "name", "") == tool_name
                    for definition in self._mcp_gateway.tool_definitions()
                )
            except Exception:  # noqa: BLE001 - admission must fail closed
                return False
        if self._plugin_registry is not None:
            try:
                return self.plugin_capability_for_tool(tool_name) is not None
            except Exception:  # noqa: BLE001 - admission must fail closed
                return False
        return False

    def plugin_capability_for_tool(self, tool_name: str) -> Any | None:
        """Return the active plugin owner/capability pair for a plugin tool."""

        if self._plugin_registry is None:
            return None
        return self._plugin_registry.plugin_capability_for_tool(tool_name)

    def execution_mode(self, tool_name: str) -> ToolExecutionMode:
        """Return the host-declared scheduler mode for a visible tool.

        Only the audited local allowlist can be parallel. MCP and
        editor-state tools remain exclusive until their transport/host gives
        an explicit thread-safety contract. Missing or malformed metadata
        fails closed to an exclusive barrier.
        """

        if tool_name.startswith("topos.mcp."):
            return ToolExecutionMode.EXCLUSIVE
        try:
            entry = self._capability_catalog.get(tool_name)
            if entry is None and self._plugin_registry is not None:
                resolved = self._plugin_registry.plugin_capability_for_tool(tool_name)
                if resolved is not None:
                    _, capability = resolved
                    metadata = getattr(capability, "metadata", {}) or {}
                    if (
                        capability.permission == ToolPermission.READ.value
                        and metadata.get("thread_safe") is True
                        and metadata.get("execution_mode") == ToolExecutionMode.PARALLEL.value
                    ):
                        return ToolExecutionMode.PARALLEL
                    return ToolExecutionMode.EXCLUSIVE
            if entry is None or entry.permission != ToolPermission.READ.value:
                return ToolExecutionMode.EXCLUSIVE
            if tool_name not in PARALLEL_SAFE_READ_TOOLS:
                return ToolExecutionMode.EXCLUSIVE
            return ToolExecutionMode(entry.execution_mode)
        except Exception:  # noqa: BLE001 - scheduling must fail closed
            pass
        return ToolExecutionMode.EXCLUSIVE

    def invoke(
        self, tool_name: str, args: dict[str, Any] | None = None,
        *,
        approval: ApprovalGrant | str | None = None,
        cancel_check: Any = None,
        timeout_seconds: float | None = None,
        run_id: str = "",
        approval_token: str | None = None,  # deprecated alias, kept for back-compat
    ) -> dict[str, Any]:
        """路由工具调用。返回 **JSON-safe** 结构化结果（含 diagnostics）。

        所有返回里的不可信文本（素材名/字幕/OCR）经 ``sanitize_untrusted`` 标记，
        再经 ``to_json_safe`` 转成 JSON transport 表示（M0.3），保证任意返回可被
        ``json.dumps`` 序列化。

        授权契约（M0.1）：write 类工具的授权由**宿主**通过 ``approval`` 关键字附带
        （一个 ``ApprovalGrant`` 对象）。模型在 ``args`` 里塞入的任何
        ``approval_token`` **一律被忽略**——模型无法自行伪造授权。``approval_token``
        关键字仅为兼容旧调用方保留，等价于 ``approval``。
        """
        args = args or {}
        # 拒绝任何禁止的能力（即使模型请求）
        if tool_name in self.FORBIDDEN_CAPABILITIES:
            return self._forbidden(tool_name)

        # 统一授权参数：优先 approval；approval_token 为兼容别名。
        grant: ApprovalGrant | str | None = approval if approval is not None else approval_token

        # Plugin tools are routed only when they do not shadow a built-in
        # facade operation.  The registry performs active-state and approval
        # checks before entering plugin code.
        if self._plugin_registry is not None:
            local_names = {definition.name for definition in list_tool_definitions()}
            if tool_name not in local_names:
                try:
                    owner = self._plugin_registry.plugin_capability_for_tool(tool_name)
                    if owner is not None:
                        return self._plugin_registry.invoke_tool(
                            owner[0], tool_name, args, approval=grant,
                            cancel_check=cancel_check,
                            timeout_seconds=timeout_seconds,
                            run_id=run_id,
                        )
                except Exception as exc:  # noqa: BLE001 - plugin boundary is fail-closed
                    return {
                        "ok": False,
                        "error": str(exc)[:500],
                        "diagnostics": [{"code": "PLUGIN_TOOL_EXCEPTION", "severity": "error"}],
                    }

        try:
            if tool_name == "topos.get_capabilities":
                # AUD-14725：成功返回统一带显式 "ok": True——此前多个读工具的成功
                # payload 无 "ok" 键，消费方 result_from_facade 按失败误判并丢数据。
                return self._json_safe(
                    {"ok": True, "manifest": self._session.get_capabilities().to_dict()}
                )
            if tool_name == "topos.get_project_snapshot":
                snap = self._session.get_snapshot(
                    detail_level=DetailLevel(args.get("detail_level", "structure")),
                    timeline_id=args.get("timeline_id"),
                    asset_limit=args.get("asset_limit"),
                )
                # 脱敏 + 不可信文本标记
                data = snap.to_dict()
                return self._json_safe({"ok": True, "snapshot": sanitize_untrusted(data)})
            if tool_name == "topos.get_composite_snapshot":
                snapshot = self._session.get_composite_snapshot()
                return self._json_safe({"ok": True, "snapshot": snapshot})
            if tool_name == "topos.inspect_subtitle_track":
                report = self._session.inspect_subtitle_track(
                    str(args["track_id"]),
                    timeline_id=args.get("timeline_id"),
                    options=args.get("options"),
                )
                return self._json_safe({"ok": True, "report": report})
            if tool_name == "topos.search_assets":
                return self._search_assets(args)
            if tool_name == "topos.get_asset_analysis":
                return self._get_analysis(args)
            if tool_name == "topos.validate_edit_plan":
                return self._validate_plan(args)
            if tool_name == "topos.validate_composite_edit":
                return self._validate_composite_edit(args)
            if tool_name == "topos.apply_edit_plan":
                return self._apply_plan(args, grant)
            if tool_name == "topos.apply_composite_edit":
                return self._apply_composite_edit(args, grant)
            if tool_name == "topos.render_preview":
                return self._render_preview(args, grant)
            if tool_name == "topos.request_export":
                return self._request_export(args, grant)
            if tool_name == "topos.get_job_status":
                return self._get_job_status(args)
            if tool_name == "topos.cancel_job":
                return self._cancel_job(args)
            # M6：软件使用助手（§9）—— 只读/导航提议
            if tool_name == "topos.search_help":
                return self._search_help(args)
            if tool_name == "topos.explain_diagnostic":
                return self._explain_diagnostic(args)
            if tool_name == "topos.show_relevant_panel":
                return self._show_relevant_panel(args)
            if tool_name == "topos.get_render_diagnostics":
                return self._get_render_diagnostics(args)
            # MCP 视觉网关（topos.mcp.*）：路由到受控出站客户端（优雅降级，不抛）。
            if tool_name.startswith("topos.mcp."):
                return self._invoke_mcp_tool(tool_name, args)
            return self._unknown_tool(tool_name)
        except Exception as exc:
            return {
                "ok": False,
                "diagnostics": [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                    detail=f"tool {tool_name} raised: {exc}",
                ).to_dict()],
            }

    @staticmethod
    def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
        """把含 ``SanitizedText`` 的 payload 转 JSON-safe（M0.3）。

        保证返回值可被 ``json.dumps`` 序列化（契约：每个公开 tool response 均可
        ``json.dumps``）。同时用断言在调试期捕获意外类型。
        """
        safe = to_json_safe(payload)
        # 契约检查：能 json.dumps 才返回（失败时抛出，便于发现未覆盖类型）。
        json.dumps(safe)
        return safe

    # ---------------- 各工具实现 ---------------- #

    def _search_assets(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._analysis is None:
            return {"ok": True, "segments": [], "note": "analysis service unavailable"}
        kind = args.get("kind")
        results = self._analysis.search_assets(
            text=args.get("text"),
            kind=SegmentKind(kind) if kind else None,
            limit=args.get("limit"),
        )
        # 标记不可信（标签/转录文本）
        return self._json_safe(
            {"ok": True, "segments": [sanitize_untrusted(s.to_dict()) for s in results]}
        )

    def _get_analysis(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._analysis is None:
            return {"ok": True, "analysis": None}
        asset_id = args.get("asset_id", "")
        analysis = self._analysis.get_analysis(asset_id)
        if analysis is None:
            return {"ok": True, "analysis": None, "diagnostics": [make_diagnostic(
                DiagnosticSeverity.WARNING, TaepDiagnosticCode.ASSET_NOT_FOUND,
                asset_id=asset_id,
            ).to_dict()]}
        return self._json_safe(
            {"ok": True, "analysis": sanitize_untrusted(analysis.to_dict())}
        )

    def _validate_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        plan = EditPlan.from_dict(args["plan"])
        preview = self._session.preview(plan)
        return {
            "ok": not preview.has_errors,
            "report": preview.report.to_dict(),
            "diff": preview.diff.to_dict() if preview.diff else None,
        }

    def _validate_composite_edit(self, args: dict[str, Any]) -> dict[str, Any]:
        plan = CompositeEditPlan.from_dict(args["plan"])
        report = self._session.validate_composite_edit(plan)
        return self._json_safe({"ok": report.ok, "report": report.to_dict()})

    def _apply_plan(
        self, args: dict[str, Any], approval: ApprovalGrant | str | None
    ) -> dict[str, Any]:
        plan = EditPlan.from_dict(args["plan"])
        # M0.1：模型在 args 里塞入的 approval_token 一律忽略（模型无法自行授权）。
        # 仅接受宿主通过 invoke(..., approval=...) 附带的 grant（或兼容的 grant_id）。
        if approval is None:
            return {
                "ok": False,
                "diagnostics": [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                    reason=(
                        "apply_edit_plan requires a host-issued ApprovalGrant; "
                        "model-supplied tokens are ignored"
                    ),
                ).to_dict()],
            }
        result = self._session.apply(plan, approval=approval)
        if self._audit_sink is not None:
            self._audit_sink({
                "tool": "topos.apply_edit_plan", "plan_id": plan.plan_id,
                "applied": result.applied, "new_revision": result.new_revision,
            })
        return {
            "ok": result.applied,
            "new_revision": result.new_revision,
            "undo_label": result.undo_label,
            "error": result.error,
        }

    def _apply_composite_edit(
        self, args: dict[str, Any], approval: ApprovalGrant | str | None
    ) -> dict[str, Any]:
        plan = CompositeEditPlan.from_dict(args["plan"])
        if approval is None:
            return {
                "ok": False,
                "diagnostics": [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                    reason=(
                        "apply_composite_edit requires a host-issued ApprovalGrant; "
                        "model-supplied tokens are ignored"
                    ),
                ).to_dict()],
            }
        result = self._session.apply_composite_edit(plan, approval=approval)
        if self._audit_sink is not None:
            self._audit_sink({
                "tool": "topos.apply_composite_edit", "plan_id": plan.plan_id,
                "plan_hash": plan.content_hash(), "applied": result.applied,
                "generation_after": result.generation_after,
            })
        return self._json_safe(result.to_dict())

    def _render_preview(
        self, args: dict[str, Any], approval: ApprovalGrant | str | None
    ) -> dict[str, Any]:
        # AUD-14718：EXPENSIVE 级门禁——与 ``_apply_plan`` 同款 PERMISSION_DENIED
        # 早退。render_preview 会创建昂贵渲染 job，授权只能由宿主经
        # invoke(..., approval=...) 附带；模型在 args 里塞的 token 已被忽略。
        if approval is None:
            return {
                "ok": False,
                "diagnostics": [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                    reason=(
                        "render_preview requires a host-issued ApprovalGrant; "
                        "model-supplied tokens are ignored"
                    ),
                ).to_dict()],
            }
        timeline_id = args.get("timeline_id") or self._session.get_snapshot().active_timeline
        if timeline_id is None:
            tid = None
        else:
            tid = timeline_id.get("timeline_id") if isinstance(timeline_id, dict) else timeline_id
        if tid is None:
            return {"ok": False, "error": "no active timeline"}
        result = self._session.request_preview(tid, preset_name=args.get("preset_name", "quick_10s"))
        return {"ok": result["job"] is not None, "job": result["job"].to_dict() if result["job"] else None}

    def _request_export(
        self, args: dict[str, Any], approval: ApprovalGrant | str | None
    ) -> dict[str, Any]:
        # AUD-14718：FILE_WRITE 级门禁——request_export 会创建导出 job（副作用），
        # 与 ``_apply_plan`` 同款 PERMISSION_DENIED 早退；最终写文件仍由导出链路的
        # confirmation/preflight 把关。授权只能由宿主附带，模型无法自行提供。
        if approval is None:
            return {
                "ok": False,
                "diagnostics": [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                    reason=(
                        "request_export requires a host-issued ApprovalGrant; "
                        "model-supplied tokens are ignored"
                    ),
                ).to_dict()],
            }
        req_data = args["request"]
        request = ExportRequest(
            timeline_id=req_data["timeline_id"],
            codec=req_data.get("codec", "h264"),
            resolution=tuple(req_data["resolution"]) if req_data.get("resolution") else None,
            fps=req_data.get("fps"),
            include_audio=req_data.get("include_audio", True),
            burn_subtitles=req_data.get("burn_subtitles", True),
            output_path=req_data.get("output_path"),
            commercial_ready_required=bool(req_data.get("commercial_ready_required", False)),
            plan_id=req_data.get("plan_id"),
        )
        result = self._session.request_export(request)
        return {
            "ok": result["job"] is not None,
            "job": result["job"].to_dict() if result["job"] else None,
            "preflight": {
                "ok": result["preflight"].ok,
                "blocking": list(result["preflight"].blocking),
                "degraded": list(result["preflight"].degraded),
            },
        }

    def _get_job_status(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        if self._jobs is None:
            return {"ok": False, "job_id": job_id, "error": "job_registry_unavailable"}
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "job_id": job_id, "error": "job_not_found"}
        return {"ok": True, "job": job.to_dict()}

    def _cancel_job(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        if self._jobs is None:
            return {"ok": False, "job_id": job_id, "error": "job_registry_unavailable"}
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "job_id": job_id, "error": "job_not_found"}
        if not job.status.is_active:
            return {
                "ok": True, "job_id": job_id, "cancelled": False,
                "status": job.status.value, "note": "job already terminal",
            }
        accepted = self._jobs.cancel(job_id)
        return {
            "ok": accepted, "job_id": job_id,
            "cancelled": accepted, "status": job.status.value,
        }

    # ---------------- M6：软件使用助手（§9，只读/导航提议）---------------- #

    def _search_help(self, args: dict[str, Any]) -> dict[str, Any]:
        """搜索本地帮助索引（§9.1）。无 help_index 时返回"未配置"而非编造。"""
        if self._help_index is None:
            return {"ok": False, "note": "help index not configured"}
        query = str(args.get("query", ""))
        limit = int(args.get("limit", 20))
        entries = self._help_index.search(query, limit=limit)
        return {"ok": True, "query": query, "results": [e.to_dict() for e in entries]}

    def _explain_diagnostic(self, args: dict[str, Any]) -> dict[str, Any]:
        """解释一个 TAEP 诊断码（§9.2：最短修复路径；不编造，找不到标 unknown）。"""
        code = str(args.get("code", ""))
        if self._help_index is None:
            return {"ok": False, "code": code, "note": "help index not configured"}
        entry = self._help_index.explain_diagnostic(code)
        if entry is None:
            # §9.1 红线：找不到时明确标 unknown，不编造
            return {"ok": True, "code": code, "known": False,
                    "note": f"诊断码 {code} 未知；建议搜索或反馈"}
        return {"ok": True, "code": code, "known": True, "entry": entry.to_dict()}

    def _show_relevant_panel(self, args: dict[str, Any]) -> dict[str, Any]:
        """提议导航到某面板（§9：仅提议；实际显示由宿主经用户确认，不模拟输入）。

        返回 navigation proposal（panel_id + reason + needs_user_confirm=True）。
        红线：本工具不直接 setVisible/raise_，也不模拟鼠标键盘（FORBIDDEN_CAPABILITIES
        含 click_ui）。宿主收到 proposal 后弹"是否跳转到 X 面板？"让用户确认。
        """
        panel_id = str(args.get("panel_id", ""))
        reason = str(args.get("reason", ""))
        if not panel_id:
            return self._json_safe({"ok": False, "note": "panel_id required"})
        # 验证 panel_id 在索引里（若有 help_index）；未启用 → 标记
        enabled = True
        known = True
        if self._help_index is not None:
            entry = self._help_index.get(f"panel:{panel_id}")
            if entry is not None:
                enabled = entry.enabled
            else:
                known = False
        return self._json_safe({
            "ok": True,
            "panel_id": panel_id,
            "reason": reason,
            "known": known,
            "enabled": enabled,
            "needs_user_confirm": True,  # 红线：导航必须用户确认，可见可取消
            "note": "navigation proposal only; host surfaces to user for confirmation",
        })

    # ---------------- 受限诊断（§9 扩展，只读运行时编译报错）---------------- #

    def _get_render_diagnostics(self, args: dict[str, Any]) -> dict[str, Any]:
        """返回最近的 shader/效果编译失败（只读运行时数据；不读文件/源码/路径）。

        数据来自渲染线程上报的诊断环形缓冲（shader_compiler / effect_pass_runner
        在编译失败时调用 ``record_compile_failure``）。这里只是把**已经在内存里**
        的运行时报错暴露给模型，红线：不读取任何文件、不返回路径或源码。
        """
        limit = int(args.get("limit", 20))
        source = args.get("source")
        entries = get_render_diagnostic_sink().recent(limit=limit, source=source)
        payload: dict[str, Any] = {
            "ok": True,
            "count": len(entries),
            # 不可信文本标记：报错/标识符来自运行时外部（driver/项目命名），统一标记。
            "diagnostics": [sanitize_untrusted(e.to_dict()) for e in entries],
        }
        if not entries:
            payload["note"] = "no recent compile failures"
        return self._json_safe(payload)

    # ---------------- MCP 视觉网关（P3.2 出站桥，只读分析）---------------- #

    def _invoke_mcp_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """路由 topos.mcp.* 到注入的 McpVisionGateway（经 McpOutboundClient 的
        allowlist/审计链）。输出按不可信数据标记（TAEP §12：MCP 输出只作数据）。"""
        if self._mcp_gateway is None:
            return {
                "ok": False,
                "diagnostics": [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                    op_kind=tool_name,
                ).to_dict()],
            }
        result = self._mcp_gateway.invoke(tool_name, args)
        if not result.get("ok"):
            return {"ok": False, "error": str(result.get("error", "MCP 调用失败"))}
        # 视觉分析文本是不可信数据 → sanitize 后返回。
        return self._json_safe({"ok": True, "analysis": sanitize_untrusted(result.get("analysis", ""))})

    # ---------------- 错误返回 ---------------- #

    def _forbidden(self, tool_name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "diagnostics": [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                reason=f"capability {tool_name!r} is forbidden (no exec/shell/fs/ui)",
            ).to_dict()],
        }

    def _unknown_tool(self, tool_name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "diagnostics": [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                op_kind=tool_name,
            ).to_dict()],
        }
