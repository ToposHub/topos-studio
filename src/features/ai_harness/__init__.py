"""Topos AI Harness — 统一 Agent 运行时（plan §0.2 / §10.2 P0 起，多阶段演进）。

本包是普通用户（对话）、高级用户（节点图）、开发者（Skill Studio）三类入口的
**唯一运行时边界**。任何功能若不能接入 :class:`HarnessRuntime`，不能直接合并。

从 P0 的 4 个模块起步，现已覆盖 P1–P4 的完整装配面（与下方 ``__all__`` 一致）：

- :mod:`invocation_models` —— ``Invocation`` / ``RunPolicy`` / ``RunRecord`` /
  ``RunEvent`` / ``RunStatus`` 等不可变数据合约；脱敏、source、scope、policy、
  状态迁移均在数据层强制（红线 §1.3 / §1.4）。
- :mod:`runtime` —— ``HarnessRuntime``：唯一 ``start`` / ``cancel`` / ``retry``
  / ``resume`` / ``get_run`` 入口（RLock 保护；不接 Qt / OpenGL / 网络 / 模型）。
- :mod:`scheduler_adapter` / :mod:`generation_adapter` —— 把现有
  ``JobRegistry`` / ``AnalysisJobScheduler`` / ``GenerationService`` 桥接到
  ``RunRecord.jobs`` 与事件流。
- :mod:`legacy_adapters` —— 把现有 ``AssistantService.run_workflow`` /
  ``run_skill`` 包装成同一 ``RunRecord``。
- :mod:`run_trace` —— run 事件投影 / 持久化（``RunTraceStore`` 等）。
- :mod:`plugin_runtime` / ``plugin_broker`` / ``plugin_trust`` /
  ``plugin_persistence`` / ``plugin_status`` / ``plugin_isolation`` /
  ``plugin_trace`` / ``plugin_sandbox`` / ``plugin_discovery`` /
  ``plugin_rollout`` —— 插件运行时全链路：注册、资源 broker、签名信任、
  快照持久化、状态诊断、进程隔离、trace、沙箱启动器、发现与灰度路由
  （P2–P4；红线 §5：第三方插件默认沙箱 + fail-closed）。
- :mod:`skill_catalog` / ``skill_compiler`` / ``skill_dry_run`` /
  ``skill_eval_suite`` / ``skill_release`` / ``skill_studio_drafts`` ——
  开发者 Skill Studio：模板目录、编译、dry-run、评测、发布与版本化草稿
  （P4；草稿与生产 ``SkillRegistry`` 隔离）。
- :mod:`editorial_skills` / ``analysis_skills`` / ``help_assistant`` /
  ``llm_role_executor`` / ``llm_role_model_client`` / ``invocation_router`` /
  ``invocation_models`` / ``result_card*`` / ``rollout_metrics`` —— 编辑技能、
  分析技能、帮助助手、LLM 角色执行与结果卡。
- :mod:`mcp_outbound` / ``mcp_http_transport`` / ``mcp_stdio_transport`` /
  ``mcp_protocol`` —— 受控出站 MCP 客户端（allowlist + 审批 + 审计）。
- :mod:`subagent_runtime` —— 子代理运行时。

禁止：在本包内 import Qt、ModernGL、provider 真实推理后端；在 Qt slot 或节点
item 中直接执行模型（plan §10.1 实施纪律）。

导出清单见 ``__all__``；新增公共 API 必须同时更新 ``__all__`` 与本 docstring。
"""

from src.features.ai_harness.invocation_models import (
    ArtifactRef,
    Invocation,
    InvocationScope,
    InvocationSource,
    JobRef,
    PermissionLevel,
    RunEvent,
    RunEventKind,
    RunPolicy,
    RunRecord,
    RunStatus,
    sanitize_for_storage,
)
from src.features.ai_harness.legacy_adapters import (
    LegacyHarnessAdapter,
    invocation_to_skill_input,
    invocation_to_workflow_input,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.run_trace import (
    RunTraceProjection,
    RunTraceStore,
    TraceEvent,
    TraceRestoreReport,
    project_trace_events,
)
from src.features.ai_harness.plugin_runtime import (
    PLUGIN_API_VERSION,
    PluginCapability,
    PluginCallContext,
    PluginContext,
    PluginExecutionPolicy,
    PluginHealth,
    PluginInvocationCancelled,
    PluginInvocationProcessCrashed,
    PluginInvocationTimedOut,
    PluginLifecycle,
    PluginManifest,
    PluginRecord,
    PluginRegistry,
    PluginSource,
    PluginState,
    PluginToolHandler,
)
from src.features.ai_harness.plugin_broker import (
    AllowlistResourceBroker,
    BrokerDecision,
    DefaultDenyResourceBroker,
    HostIOResult,
    NetworkGrant,
    PathGrant,
    PluginHostIO,
    PluginResourceBroker,
)
from src.features.ai_harness.plugin_trust import (
    HmacPluginSignatureVerifier,
    PluginSignatureVerifier,
    PluginTrustPolicy,
)
from src.features.ai_harness.plugin_persistence import (
    LEGACY_REGISTRY_SCHEMA,
    PLUGIN_PROJECT_SCHEMA,
    PluginMigrationRegistry,
    PluginMigrationReport,
    PluginJournalRestoreReport,
    PluginRestoreReport,
    PluginSnapshotCheckpoint,
    PluginSnapshotJournal,
    PluginSnapshotStore,
)
from src.features.ai_harness.plugin_status import (
    PluginCapabilityView,
    PluginEvent,
    PluginEventJournal,
    PluginStatusDiagnostic,
    PluginStatusProjection,
    PluginStatusView,
)
from src.features.ai_harness.plugin_isolation import ProcessIsolatedPlugin
from src.features.ai_harness.plugin_trace import PluginRunTraceAdapter
from src.features.ai_harness.plugin_sandbox import (
    DarwinSandboxLauncher,
    LinuxBubblewrapLauncher,
    PluginSandboxLauncher,
    PluginSandboxReport,
    detect_plugin_sandbox,
    validate_sandbox_launcher,
)
from src.features.ai_harness.plugin_discovery import (
    PluginDiscovery,
    PluginDiscoveryCandidate,
    PluginDiscoveryDiagnostic,
    PluginDiscoveryReport,
    PluginDiscoveryState,
    PluginInstallResult,
)
from src.features.ai_harness.plugin_rollout import (
    PluginRolloutDecision,
    PluginRolloutFlag,
    PluginRolloutGate,
    PluginRolloutLedger,
    PluginRolloutMetrics,
    PluginRolloutPolicy,
    PluginRolloutRouter,
    PluginRunOutcome,
    PluginRunSample,
    aggregate_plugin_samples,
)
from src.features.ai_harness.rollout_metrics import (
    RunMetricsSnapshot,
    aggregate_run_metrics,
)
from src.features.ai_harness.scheduler_adapter import (
    JobRunBridge,
    job_ref_from_handle,
    poll_cancel_to_job,
    sync_job_to_run,
)

__all__ = [
    "ArtifactRef",
    "HarnessRuntime",
    "Invocation",
    "InvocationScope",
    "InvocationSource",
    "JobRef",
    "JobRunBridge",
    "LegacyHarnessAdapter",
    "PermissionLevel",
    "RunEvent",
    "RunEventKind",
    "RunPolicy",
    "RunRecord",
    "RunMetricsSnapshot",
    "RunStatus",
    "RunTraceProjection",
    "RunTraceStore",
    "PLUGIN_API_VERSION",
    "PluginCapability",
    "PluginCallContext",
    "PluginContext",
    "PluginExecutionPolicy",
    "PluginHealth",
    "PluginInvocationCancelled",
    "PluginInvocationProcessCrashed",
    "PluginInvocationTimedOut",
    "ProcessIsolatedPlugin",
    "PluginRunTraceAdapter",
    "PluginSandboxReport",
    "PluginSandboxLauncher",
    "DarwinSandboxLauncher",
    "LinuxBubblewrapLauncher",
    "detect_plugin_sandbox",
    "validate_sandbox_launcher",
    "PluginLifecycle",
    "PluginManifest",
    "PluginRecord",
    "PluginRegistry",
    "PluginSource",
    "PluginState",
    "PluginToolHandler",
    "AllowlistResourceBroker",
    "BrokerDecision",
    "DefaultDenyResourceBroker",
    "HostIOResult",
    "NetworkGrant",
    "PathGrant",
    "PluginHostIO",
    "PluginResourceBroker",
    "HmacPluginSignatureVerifier",
    "PluginSignatureVerifier",
    "PluginTrustPolicy",
    "LEGACY_REGISTRY_SCHEMA",
    "PLUGIN_PROJECT_SCHEMA",
    "PluginMigrationRegistry",
    "PluginMigrationReport",
    "PluginJournalRestoreReport",
    "PluginRestoreReport",
    "PluginSnapshotCheckpoint",
    "PluginSnapshotJournal",
    "PluginSnapshotStore",
    "PluginCapabilityView",
    "PluginEvent",
    "PluginEventJournal",
    "PluginStatusDiagnostic",
    "PluginStatusProjection",
    "PluginStatusView",
    "PluginDiscovery",
    "PluginDiscoveryCandidate",
    "PluginDiscoveryDiagnostic",
    "PluginDiscoveryReport",
    "PluginDiscoveryState",
    "PluginInstallResult",
    "PluginRolloutDecision",
    "PluginRolloutFlag",
    "PluginRolloutGate",
    "PluginRolloutLedger",
    "PluginRolloutMetrics",
    "PluginRolloutPolicy",
    "PluginRolloutRouter",
    "PluginRunOutcome",
    "PluginRunSample",
    "aggregate_plugin_samples",
    "aggregate_run_metrics",
    "invocation_to_skill_input",
    "invocation_to_workflow_input",
    "job_ref_from_handle",
    "poll_cancel_to_job",
    "sanitize_for_storage",
    "TraceEvent",
    "TraceRestoreReport",
    "project_trace_events",
    "sync_job_to_run",
]
