"""``AiEditingMixin`` —— MainWindow 的 AI 编辑组合根（M1.2 / M1.3 / M2.2 装配）。

把 AI 编辑子系统（``AssistantService`` / ``AiEditController`` /
``MainWindowAiEditHostBridge`` / ``ApprovalService``）在主窗口里**真正装配**
起来，并在工程打开/关闭生命周期里创建/拆除。

之前 ``AiEditAssistantPanel`` 只是一个空壳 dock；本 mixin 把它接到真实 service +
真实 CommandStack 提交路径（M2.2）。

设计约束（遵循 .clinerules）：

- 只在 ``_edit_available`` 且 dock 面板存在时装配；否则保持 None（确定状态）。
- 装配失败不 broad except-pass：捕获并记录，最差让 AI Dock 进入 FAILED，
  不影响工程打开。
- 工程关闭/切换时撤销该工程 grants、断开旧 controller；不保留上一工程的 snapshot。
- 不在 UI 线程跑长任务（本批确定性 planner 无长任务；真实 provider 在 M5）。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# _setup_ai_editing 可重跑（装配 + 自动建工程会各跑一次），同一进程内
# 对同一路径的"清单缺失"只警告一次，避免刷屏。
_missing_manifest_warned: set[str] = set()

#: AUD-2205: ComfyUI 能力查询/探活的收窄超时（秒）。client 默认 30s 是为
#: 生成下载等长操作准备的；绑定/探活是配置期短操作，/object_info 在大模型
#: 库上响应可达数 MB，30s 全额等待会让调用线程冻结过久。
_COMFY_PROBE_TIMEOUT_S = 10.0


@contextmanager
def _comfy_probe_timeout(client: Any, seconds: float = _COMFY_PROBE_TIMEOUT_S) -> Iterator[None]:
    """AUD-2205: 临时收窄 ComfyUI client 的 HTTP 超时，退出时恢复原值。

    仅对持有数值型 ``_timeout`` 的真实 client 生效（测试桩无此属性时是
    空操作）。约束：with 块内的请求使用收窄超时——绑定/探活是配置期
    单调用方操作，不存在并发使用同一 client 的长操作。
    """
    previous = getattr(client, "_timeout", None)
    if isinstance(previous, (int, float)):
        client._timeout = float(seconds)
    try:
        yield
    finally:
        if isinstance(previous, (int, float)):
            client._timeout = previous


def resolve_shared_runtime(host: Any) -> Any:
    """解析 Edit Assistant 与 AI Workflow 共享的单一 HarnessRuntime（plan §0.2）。

    优先复用 ``setup_docks`` 已建的 ``_ai_workflow_runtime``（flag 开时）；否则
    新建并存到 ``_shared_runtime``，供后续 dock 装配复用。幂等：重复调用返回
    同一实例，避免多 runtime 实例违反"三种入口只有一个 HarnessRuntime"红线。

    Args:
        host: MainWindow（持有 ``_ai_workflow_runtime`` / ``_shared_runtime`` 属性）。

    Returns:
        HarnessRuntime 实例（永不为 None）。
    """
    from src.features.ai_harness.runtime import HarnessRuntime

    existing = getattr(host, "_ai_workflow_runtime", None)
    if existing is not None:
        host._shared_runtime = existing
        return existing
    shared = getattr(host, "_shared_runtime", None)
    if shared is not None:
        return shared
    new_runtime = HarnessRuntime()
    host._shared_runtime = new_runtime
    return new_runtime


def build_p1_dependencies(
    *, service: Any, runtime: Any,
    editorial_provider: Any = None, has_profile: bool = False,
    help_sources: Any = None, localization_media_provider: Any = None,
) -> dict[str, Any]:
    """装配 P1 Skill 驱动 Agent 的全部依赖（plan §10.3 P1.1/P1.3 host 装配）。

    在 ``_setup_ai_editing`` 调用，产出一组共享依赖注入 ``AiEditController``：
    - ``skill_registry``：含 3 个官方 skill + 2 个 P2 editorial skill
    - ``router``：``InvocationRouter``，skill_resolver 绑到 registry，help_search 绑到 help_assistant
    - ``compiler``：``SkillCompiler``（无状态，三入口统一编译）
    - ``adapter``：``LegacyHarnessAdapter(service, runtime)``（单一 HarnessRuntime）
    - ``help_assistant``：``HelpAssistant``（无 index 时降级，不崩）
    - ``catalog``：``SkillCatalogView``（驱动聊天 Skill 推荐卡）

    Args:
        service: AssistantService（adapter 的 workflow/taskgraph 路径用）。
        runtime: 共享 HarnessRuntime（与 controller.runtime 同一实例）。
        editorial_provider: 可选 evidence provider（editorial 路径用；None 时降级）。
        has_profile: 是否已配置 profile（影响 catalog 前置条件展示）。
        help_sources: 可选 HelpSources（P1.4 帮助装配；满足 HelpSources Protocol。
            非 None 时走 build_help_assistant 装配真实 HelpIndex；None 时降级空 assistant）。

    Returns:
        依赖字典（keys: router/compiler/adapter/help_assistant/catalog/skill_registry）。
    """
    from src.features.ai_harness.editorial_skills import register_p2_skills
    from src.features.ai_harness.help_assistant import HelpAssistant
    from src.features.ai_harness.invocation_router import InvocationRouter
    from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
    from src.features.ai_harness.plugin_runtime import PluginRegistry
    from src.features.ai_harness.plugin_status import PluginEventJournal
    from src.features.ai_harness.skill_catalog import SkillCatalogView
    from src.features.ai_harness.skill_compiler import SkillCompiler
    from src.features.edit.ai_editing.composition_plugin import SkillPluginHost
    from src.features.edit.ai_editing.skill_registry import SkillRegistry

    # 共享 registry：3 官方 + 2 P2 editorial skill
    registry = SkillRegistry()
    try:
        register_p2_skills(registry)
    except Exception:  # noqa: BLE001
        logger.debug("register_p2_skills failed in build_p1_dependencies", exc_info=True)

    # help assistant（P1.4：help_sources 非 None 时走 build_help_assistant 装配真实
    # HelpIndex；None 或装配异常时降级空 assistant，entry_ids_for_query 仍可调）。
    if help_sources is not None:
        from src.features.ai_harness.help_assistant import build_help_assistant
        help_assistant = build_help_assistant(help_sources)
    else:
        help_assistant = HelpAssistant()

    # P7/P8：Skill 使用共享插件 registry 投影；event journal 供 UI 展示最近失败。
    # 这里仅注册已经由 SkillRegistry 校验过的 manifest，不扫描或导入外部代码。
    plugin_events = PluginEventJournal()
    plugin_registry = PluginRegistry(event_sink=plugin_events.record)
    skill_plugin_host = SkillPluginHost(
        registry,
        plugin_registry=plugin_registry,
    )
    for skill_manifest in registry.list_skills(include_disabled=True):
        if not skill_plugin_host.register(skill_manifest):
            logger.warning(
                "skill plugin projection failed: %s",
                getattr(skill_manifest, "skill_id", ""),
            )

    # router：skill_resolver 绑插件 host，help_search 绑 help_assistant
    router = InvocationRouter(
        help_search=help_assistant.entry_ids_for_query,
        skill_resolver=skill_plugin_host.is_active,
    )

    # compiler（无状态）
    compiler = SkillCompiler()

    # adapter（共享 runtime；editorial provider 可选）
    adapter = LegacyHarnessAdapter(service, runtime)
    if editorial_provider is not None:
        adapter.set_editorial_provider(editorial_provider)
    if localization_media_provider is not None:
        adapter.set_localization_media_provider(localization_media_provider)

    # catalog（驱动 Skill 推荐卡）
    catalog = SkillCatalogView(registry, has_profile=has_profile)

    return {
        "skill_registry": registry,
        "router": router,
        "compiler": compiler,
        "adapter": adapter,
        "help_assistant": help_assistant,
        "catalog": catalog,
        "plugin_registry": plugin_registry,
        "plugin_event_journal": plugin_events,
        "skill_plugin_host": skill_plugin_host,
    }


def make_editorial_provider(analysis_service: Any) -> Any:
    """把 AnalysisService 包装成 editorial_provider（P1：真实 transcript evidence）。

    provider 签名 ``(asset_ids: tuple, kind: str) -> Sequence[SemanticSegment]``，
    供 ``LegacyHarnessAdapter.set_editorial_provider`` 注入。对每个 asset_id 调
    ``analysis_service.get_segments(aid)`` 并按 ``SegmentKind(kind)`` 过滤。

    Args:
        analysis_service: 共享 AnalysisService（与 AssistantService 同一实例）。

    Returns:
        editorial_provider 闭包。未知 kind / 未知 asset / 异常时返回空（不崩）。
    """
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    def provider(asset_ids: tuple[str, ...], kind: str) -> list:
        try:
            want = SegmentKind(kind)
        except ValueError:
            return []
        out: list = []
        try:
            for aid in asset_ids or ():
                for seg in analysis_service.get_segments(aid):
                    if seg.kind == want:
                        out.append(seg)
        except Exception:  # noqa: BLE001
            return []
        return out

    return provider


def make_localization_media_provider(bridge: Any) -> Any:
    """Resolve a materialized GenerationBin TTS candidate to a MediaAsset ref.

    The assistant may only place an audio asset once the user has explicitly
    materialized and imported the candidate. The bridge owns that mapping, so
    the adapter never sees a remote URL or reaches into project internals.
    """
    from src.features.ai_harness.editorial_skills import SkillArtifact

    def provider(invocation: Any, _segments: tuple[Any, ...], _language: str) -> Any:
        inputs = getattr(invocation, "inputs", {})
        generated_asset_id = str(inputs.get("tts_generated_asset_id", "") or "").strip()
        if not generated_asset_id:
            return None
        resolver = getattr(bridge, "get_ai_generated_media_asset_id", None)
        if not callable(resolver):
            return None
        media_asset_id = str(resolver(generated_asset_id) or "").strip()
        if not media_asset_id:
            return None
        try:
            duration_frames = int(inputs.get("tts_duration_frames", 0) or 0)
            start_frame = int(inputs.get("tts_start_frame", 0) or 0)
        except (TypeError, ValueError):
            return None
        return SkillArtifact(
            artifact_id=f"generated_media:{generated_asset_id}",
            artifact_type="media_generation_ref",
            summary="已物化并导入媒体池的生成 TTS 配音",
            trust="unverified",
            payload_ref=generated_asset_id,
            media_asset_id=media_asset_id,
            duration_frames=max(0, duration_frames),
            start_frame=max(0, start_frame),
        )

    return provider


def _build_model_pack_manager() -> Any:
    """构造 ModelPackManager，合并主项目 BUILTIN_SPECS + vault 模型包 spec（P1-4）。

    vault（apps/topos_vault）的模型权重与主项目共享 models/ai_editing/ 目录，
    但其 spec（SigLIP2/Qwen3-VL/Qwen2.5-Omni/InsightFace 等）默认不在主项目
    BUILTIN_SPECS 里。本函数把两边 spec 合并，让主项目模型管理 UI 能看到并下载
    vault 模型，vault index 时也能复用同一份已下载权重。

    vault spec 失败（如 vault 模块不可用）不阻塞——降级用纯主项目 BUILTIN_SPECS。
    """
    from src.features.edit.ai_editing.model_pack_manager import (
        BUILTIN_SPECS, ModelPackManager,
    )
    specs = list(BUILTIN_SPECS)
    try:
        from apps.topos_vault.model_specs import to_main_project_specs
        vault_specs = to_main_project_specs()
        # 去重：vault 与主项目可能有同 pack_id（如 qwen3_asr），主项目优先
        existing_ids = {s.pack_id for s in specs}
        for s in vault_specs:
            if s.pack_id not in existing_ids:
                specs.append(s)
    except Exception as e:
        logger.warning("合并 vault 模型 spec 失败（降级用主项目 spec）：%s", e)
    # Production host must use the release manifest's real SHA-256 values.
    # The manager's directory-exists mode remains available only to explicit
    # deterministic/demo test hosts and is never used by the application.
    manager = ModelPackManager(specs=tuple(specs), strict_verification=True)
    release_manifest = manager.cache_dir / "release_manifest.json"
    if release_manifest.is_file():
        try:
            rejected = manager.load_release_manifest(release_manifest)
            if rejected:
                logger.error("模型发布清单包含无效 pack：%s", ", ".join(rejected))
        except (OSError, ValueError):
            # Keep strict mode: bad metadata must produce an actionable
            # NOT_INSTALLED state, never a directory-exists fallback.
            logger.error("模型发布清单加载失败：%s", release_manifest, exc_info=True)
    else:
        if str(release_manifest) not in _missing_manifest_warned:
            _missing_manifest_warned.add(str(release_manifest))
            # 没有部署 AI 编辑模型是常见常态（功能面板会显示未安装），
            # 属于预期配置，记 INFO 即可，不作为警告刷屏。
            logger.info("模型发布清单不存在（AI 编辑模型未部署）：%s", release_manifest)
    return manager


def _job_cards_from_registry(registry: Any) -> tuple:
    """把 ``AnalysisService.jobs``（JobRegistry）转成 ``JobCard`` 元组（P0.6）。

    供 ``AssistantService.set_job_cards_provider`` 注入；每次 ``_emit_state`` 重读，
    使 ``AssistantStateSnapshot.jobs`` 随 JobRegistry 状态实时刷新。registry 异常
    或为空时返回 ()（不破坏 snapshot）。
    """
    try:
        from src.features.edit.ai_editing.assistant_events import JobCard
    except Exception:  # noqa: BLE001
        return ()
    try:
        handles = registry.list_all() if hasattr(registry, "list_all") else []
    except Exception:  # noqa: BLE001
        return ()
    cards: list = []
    for h in handles:
        try:
            cards.append(JobCard(
                job_id=h.job_id,
                kind=h.kind.value if hasattr(h.kind, "value") else str(h.kind),
                status=h.status.value if hasattr(h.status, "value") else str(h.status),
                progress=float(getattr(h, "progress", 0.0) or 0.0),
                label=getattr(h, "label", "") or "",
            ))
        except Exception:  # noqa: BLE001
            continue
    return tuple(cards)


class AiEditingMixin:
    """提供 ``_setup_ai_editing`` / ``_teardown_ai_editing`` 组合根方法。

    由 ``MainWindow`` 继承；状态存在 ``self._ai_edit_controller`` /
    ``self._ai_assistant_service`` / ``self._ai_host_bridge`` /
    ``self._ai_approval_service``。
    """

    # 这些属性在 MainWindow.__init__ 里会被显式初始化为 None（见 _init_ai_editing_state）。
    _ai_edit_controller: Any | None
    _ai_assistant_service: Any | None
    _ai_host_bridge: Any | None
    _ai_approval_service: Any | None
    _ai_session_store: Any | None

    def _init_ai_editing_state(self) -> None:
        """在 MainWindow.__init__ 早期把 AI 状态初始化为 None（确定状态）。"""
        self._ai_edit_controller = None
        self._ai_assistant_service = None
        self._ai_host_bridge = None
        self._ai_approval_service = None
        self._ai_session_store = None
        # Autonomous editing has an independent emergency stop.  The switch is
        # intentionally separate from durable feature flags so incident response
        # can stop already configured automation without rewriting preferences.
        try:
            from src.features.edit.ai_editing.kill_switch import KillSwitch
            self._ai_kill_switch = KillSwitch()
        except Exception:  # noqa: BLE001
            self._ai_kill_switch = None
        # M3：媒体池分析子系统（controller + scheduler + service）
        self._media_analysis_controller = None
        self._analysis_scheduler = None
        self._analysis_service = None
        # R1：AssistantService 与 MediaAnalysisController 共享的同一 AnalysisService。
        # 启动自检据此断言二者引用同一实例（否则证据会泄漏 / 静默回退 Null）。
        self._shared_analysis_service = None
        # R4-a：模型包管理器（指向 models/ai_editing/），供 OCR/ASR provider 探测模型。
        self._model_pack_manager = None
        self._transcript_router = None
        self._tts_provider = None
        self._speech_to_text_worker = None
        self._subtitle_to_speech_worker = None

    def _attach_optional_ai_hosts(self) -> None:
        """Re-attach feature-owned AI host actions after controller rebuilds.

        ``_setup_ai_editing`` is intentionally rerunnable when a project or AI
        profile changes.  Optional feature modules therefore must not attach
        only to the first controller instance created during MainWindow startup.
        """
        registry = getattr(self, "feature_registry", None)
        get_module = getattr(registry, "get_module", None)
        if not callable(get_module):
            return
        try:
            module = get_module("teaching_animation")
            attach = getattr(module, "attach_main_window", None)
            if callable(attach):
                attach(self)
                logger.debug("Optional AI host integrations re-attached")
        except Exception:  # noqa: BLE001
            # Optional host wiring must not make the core AI controller fail.
            logger.debug("Optional AI host integration attach failed", exc_info=True)

    def _resolve_ai_timeline_id(self, project: Any) -> str:
        """Resolve an AI timeline id, including the UI-only provisional timeline."""
        active_id = str(getattr(project, "active_timeline_id", "") or "")
        if active_id:
            return active_id
        refs = getattr(project, "timelines", None) or {}
        if refs:
            return str(next(iter(refs)))
        panel = getattr(self, "timeline_panel", None)
        get_view = getattr(panel, "get_timeline_view", None)
        view = get_view() if callable(get_view) else getattr(panel, "timeline_view", None)
        timeline = getattr(view, "_timeline", None) if view is not None else None
        return str(getattr(timeline, "timeline_id", "") or "")

    def _setup_ai_editing(self) -> None:
        """装配 AI 编辑子系统（在 dock 面板创建之后调用）。

        构造 ApprovalService / AssistantService / MainWindowAiEditHostBridge /
        AiEditController，并把它们接到 ``self.ai_edit_assistant_panel``。
        重复调用（如工程切换）会先拆除旧的再建新的。
        """
        if not getattr(self, "_edit_available", False):
            return
        panel = getattr(self, "ai_edit_assistant_panel", None)
        if panel is None:
            return

        # ``@`` mentions and internal media-pool drags resolve lazily against
        # the live MediaPoolPanel, so project switches immediately expose the
        # new asset set without rebuilding the assistant UI.
        set_media_pool_provider = getattr(panel, "set_media_pool_provider", None)
        if callable(set_media_pool_provider):
            set_media_pool_provider(
                lambda: (
                    getattr(self, "media_pool_panel", None).get_ai_attachment_candidates()
                    if getattr(self, "media_pool_panel", None) is not None
                    and callable(getattr(
                        getattr(self, "media_pool_panel", None),
                        "get_ai_attachment_candidates",
                        None,
                    ))
                    else ()
                )
            )

        if hasattr(panel, "configure_models_requested"):
            try:
                panel.configure_models_requested.disconnect(
                    self._open_ai_model_preferences
                )
            except (TypeError, RuntimeError):
                pass
            panel.configure_models_requested.connect(self._open_ai_model_preferences)

        # E13: composer footer 的 profile/模型选择此前无人接收（注意
        # configure_models_requested 另有接收者，二者入口不同）。
        if hasattr(panel, "composer_profile_requested"):
            try:
                panel.composer_profile_requested.disconnect(
                    self._open_ai_model_preferences
                )
            except (TypeError, RuntimeError):
                pass
            panel.composer_profile_requested.connect(self._open_ai_model_preferences)

        # 先拆除旧装配（工程切换场景）
        self._teardown_ai_editing()

        try:
            from src.app.controllers.ai_edit_controller import AiEditController
            from src.app.controllers.ai_edit_host_bridge_impl import (
                MainWindowAiEditHostBridge,
            )
            from src.features.edit.ai_editing.analysis_service import AnalysisService
            from src.features.edit.ai_editing.approval_service import ApprovalService
            from src.features.edit.ai_editing.assistant_service import AssistantService
            from src.features.edit.ai_editing.edit_api import EditSession

            approval_service = ApprovalService()
            # EditSession 需要 EditSessionHost；AssistantService 主要用 session 做
            # snapshot/preview。这里 bind 一个最小 host（本批 apply 已改走 bridge，
            # service 不再经 session.commit）。
            session_host = _SessionHostShim(self)
            EditSession.unbind()
            session = EditSession.bind(session_host)

            # ============================================================ #
            # R1（Composition Root 装配修复）：
            # 先创建 **唯一的** AnalysisService，AssistantService 与
            # MediaAnalysisController 共享同一实例（同一证据仓库 / 同一 request
            # 三步流）。之前这里在两处分别 new 出 AnalysisService：
            #   - AssistantService 拿不到 → 回退 _NullAnalysisReader（永远 unknown）
            #   - MediaAnalysisController 持有真实实例
            # 两者从未连接，导致 Agent 工作流读不到任何媒体分析证据。
            # ============================================================ #
            analysis_service = AnalysisService()

            # 载入已保存的 profiles（M1.4）并构造对应的 planner。P0 规则：
            # - 没有 profile → 保持 NO_PROFILE（兼容离线/测试环境）；
            # - 有 profile 但本地配置不完整 → 使用显式 UnavailablePlanner，
            #   禁止把配置错误伪装成确定性 planner 成功；
            # - 配置完整 → 使用真实 LLM planner，网络错误由 P1 error code 暴露。
            active_profile_id = ""
            profile_connected = False
            planner = None
            role_executor = None
            registry = None  # M5：profile 查询所需；try 内可能赋值
            credential_store = None
            reasoning_effort = "medium"
            try:
                from src.features.edit.ai_editing.credential_store import (
                    get_credential_storage_preference,
                    make_credential_store,
                )
                from src.features.edit.ai_editing.llm_variant_planner import (
                    LLMVariantPlanner,
                    UnavailablePlanner,
                )
                from src.features.edit.ai_editing.provider_profiles import (
                    ProfileRegistry,
                )
                from src.features.edit.ai_editing.model_router import (
                    ModelRouteRequest,
                    ModelRouter,
                )
                registry = ProfileRegistry()
                registry.load_from_config(self.config_manager)
                registry.load_from_user_file()
                # The same user-level credential store is also needed by
                # configured media-generation profiles, even when the active
                # text profile is absent or points at a local model.
                if registry.list():
                    credential_store = make_credential_store(
                        get_credential_storage_preference()
                    )
                from src.features.edit.ai_editing.user_ai_config import load_user_ai_config
                saved_effort = load_user_ai_config().get("reasoning_effort")
                if saved_effort in ("low", "medium", "high"):
                    reasoning_effort = saved_effort
                active = ModelRouter(registry.list()).route(
                    ModelRouteRequest(task="editorial_planning", allow_fallback=False),
                    active_profile_id=registry.active_profile_id,
                ).profile if registry.active_profile_id else None
                if active is not None:
                    active_profile_id = active.profile_id
                    if credential_store is None:
                        credential_store = make_credential_store(
                            get_credential_storage_preference()
                        )
                    from src.features.edit.ai_editing.startup_diagnostics import (
                        run_startup_diagnostics,
                    )
                    secret_available = bool(
                        active.provider_kind in {"ollama", "local_http", "openai_compat"}
                        or (
                            active.credential_ref
                            and credential_store.get_secret(active.credential_ref)
                        )
                    )
                    self._ai_startup_diagnostics = run_startup_diagnostics(
                        profile=active,
                        credential_available=secret_available,
                        feature_flags=getattr(self, "feature_flags", None),
                        analysis_provider_ids=analysis_service.available_providers(),
                    )
                    if self._ai_startup_diagnostics.ok:
                        planner = LLMVariantPlanner(
                            active, credential_store, brief_service=None,
                            reasoning_effort=reasoning_effort,
                        )
                        from src.features.ai_harness.llm_role_executor import LlmRoleExecutor
                        from src.features.ai_harness.llm_role_model_client import LlmRoleModelClient
                        role_executor = LlmRoleExecutor(
                            model_client=LlmRoleModelClient.from_profile(active, credential_store),
                            skill_id="topos.multirole",
                            skill_version="1.0.0",
                        )
                        profile_connected = True
                        logger.info(
                            "AI planner 接入真实 LLM：profile=%s kind=%s model=%s",
                            active.display_name, active.provider_kind, active.model_id,
                        )
                    else:
                        planner = UnavailablePlanner(self._ai_startup_diagnostics.summary())
                        profile_connected = False
                        if any(
                            check.check_id == "ai_assistant_flag"
                            for check in self._ai_startup_diagnostics.checks
                        ):
                            logger.warning(
                                "AI Assistant 当前未启用：%s；如需使用已配置的 AI profile，"
                                "请显式设置 TOPOS_FEATURE_AI_ASSISTANT=1。",
                                self._ai_startup_diagnostics.summary(),
                            )
                        else:
                            logger.error("AI 启动自检失败：%s", self._ai_startup_diagnostics.summary())
            except Exception as exc:
                # 有活动 profile 时，任何装配错误都必须是显式失败；只有完全没有
                # profile 才允许保留旧的离线/测试行为。这样不会把 API/key 错误
                # 伪装成规则引擎成功。
                logger.warning(
                    "LLM planner 构造失败", exc_info=True
                )
                if active_profile_id:
                    planner = UnavailablePlanner(f"AI planner 构造失败：{exc}")
                else:
                    planner = None
                profile_connected = False

            # R1：把共享的 AnalysisService 注入 AssistantService——这是装配根修复
            # 的核心。此前漏传此参数，导致 WorkflowRunner 永久走 _NullAnalysisReader，
            # 即便媒体池已分析出真实 segment，Agent 也看不到任何证据。
            service = AssistantService(
                session, planner=planner, analysis_service=analysis_service,
                role_executor=role_executor,
            )
            # Multilingual subtitle workflow reuses the same active AI
            # Assistant profile and credential store as editorial planning.
            if registry is not None and credential_store is not None:
                active_translation_profile = registry.active_profile
                if active_translation_profile is not None:
                    try:
                        from src.features.edit.core.captions.translation import (
                            make_ai_assistant_workflow_translation_provider,
                        )
                        service.set_translation_provider(
                            make_ai_assistant_workflow_translation_provider(
                                active_translation_profile, credential_store,
                            )
                        )
                    except Exception:
                        logger.warning(
                            "AI subtitle translation provider unavailable",
                            exc_info=True,
                        )
            # M5：注入 profile 查询（派生 allow_media_upload）+ context 摘要查询
            # （派生 ContextSummary）。R1 修复：把共享的 analysis_service 一并注入，
            # 使 analyzed_count / analysis_fresh 从真实证据仓库派生（此前用 has_proxy
            # 冒充、analysis_fresh 恒 False）。
            try:
                self._inject_assistant_service_providers(
                    service, registry, analysis_service=analysis_service,
                )
            except Exception:
                logger.debug("assistant service provider injection skipped", exc_info=True)
            # P0.6：注入 job 卡片查询——把共享 AnalysisService 的 JobRegistry 桥接
            # 进 AssistantStateSnapshot.jobs（此前恒 ()，Run Drawer 永远空）。
            # provider 每次 emit 重读，使 snapshot 随 Job 状态实时刷新。
            try:
                service.set_job_cards_provider(
                    lambda: _job_cards_from_registry(analysis_service.jobs)
                )
            except Exception:
                logger.debug("job cards provider injection skipped", exc_info=True)
            bridge = MainWindowAiEditHostBridge(
                self, service, approval_service, analysis_service=analysis_service,
            )
            # B3：真实本机 MCP host。外部 stdio/CLI 只连当前用户的 Unix
            # socket；socket 线程通过 Qt dispatcher 回到 GUI 线程后才调用
            # ToolFacade，绝不在后台线程读取 Qt/OpenGL 状态。写权限不通过该
            # transport 发放，仍必须留在 GUI approval 流程。
            self._ai_tool_facade = None
            self._ai_mcp_host = None
            self._ai_mcp_dispatcher = None
            try:
                from src.features.edit.ai_editing.mcp_gateway import McpServer
                from src.features.edit.ai_editing.mcp_local_ipc import (
                    LocalMcpIpcHost,
                    QtMcpRequestDispatcher,
                )
                from src.features.edit.ai_editing.tool_call_log import ToolCallLog
                from src.features.edit.ai_editing.tool_facade import ToolFacade
                from src.features.edit.ai_editing.mcp_vision_gateway import (
                    get_mcp_vision_gateway,
                )

                # MCP 视觉网关（智谱 GLM-4.6V，经受控出站客户端）。不可用时
                # 自身优雅降级（工具不暴露），不影响本地工具面。
                mcp_vision = get_mcp_vision_gateway()
                facade = ToolFacade(
                    session,
                    analysis_service=analysis_service,
                    approval_service=approval_service,
                    jobs=analysis_service.jobs,
                    mcp_gateway=mcp_vision,
                )
                self._ai_tool_facade = facade
                flags = getattr(self, "feature_flags", None)
                enabled = bool(
                    flags.is_enabled("mcp_server") if hasattr(flags, "is_enabled")
                    else (flags or {}).get("mcp_server", False)
                )
                if enabled:
                    mcp_server = McpServer(facade=facade, call_log=ToolCallLog())
                    dispatcher = QtMcpRequestDispatcher(mcp_server)

                    def _mcp_context() -> tuple[str, str, dict[str, bool]]:
                        project = getattr(self, "_current_edit_project", None)
                        current_flags = getattr(self, "feature_flags", None)
                        current_enabled = bool(
                            current_flags.is_enabled("mcp_server")
                            if hasattr(current_flags, "is_enabled")
                            else (current_flags or {}).get("mcp_server", False)
                        )
                        return (
                            str(getattr(project, "project_id", "") or ""),
                            active_profile_id,
                            {"mcp_server": current_enabled},
                        )

                    local_host = LocalMcpIpcHost(
                        mcp_server,
                        dispatch=dispatcher.dispatch,
                        context_provider=_mcp_context,
                    )
                    socket_path = local_host.start()
                    self._ai_mcp_host = local_host
                    self._ai_mcp_dispatcher = dispatcher
                    self._ai_mcp_socket_path = str(socket_path)
                    logger.info("Local MCP host ready at %s", socket_path)
            except Exception:
                # MCP is optional and off by default.  Its failure must not
                # make the core editor unusable, but it stays observable.
                logger.error("Failed to start local MCP host", exc_info=True)
                self._ai_mcp_host = None
                self._ai_mcp_dispatcher = None
                self._ai_mcp_socket_path = ""
            # P0.2：解析单一 HarnessRuntime（复用 dock 已建的实例，消除 §0.2
            # 双实例红线）。runtime 为 None 时 controller 仍可工作（向后兼容）。
            try:
                shared_runtime = resolve_shared_runtime(self)
            except Exception:
                logger.debug("shared runtime resolve failed", exc_info=True)
                shared_runtime = None
            # M11：生成媒体共用 HarnessRuntime + JobRegistry；候选先进入 GenerationBin，
            # 不直接创建 MediaAsset 或写入时间线。
            generation_adapter: Any = None
            try:
                from src.features.ai_harness.generation_adapter import GenerationHarnessAdapter
                from src.features.edit.ai_editing.generation_provider import (
                    DeterministicGenerationProvider, ProviderRegistry,
                )
                from src.features.edit.ai_editing.http_generation_provider import (
                    HttpGenerationProvider,
                )
                from src.features.edit.ai_editing.volcengine_generation_provider import (
                    make_volcengine_generation_provider,
                )
                from src.features.edit.ai_editing.media_generation_service import GenerationService

                providers = ProviderRegistry()
                # The deterministic provider is retained for demos/tests only.
                # Production composition must not expose it when generation is
                # disabled by the safe default flag.
                generation_flags = getattr(self, "feature_flags", None)
                if generation_flags is not None and not generation_flags.is_enabled("generation"):
                    raise RuntimeError("generation feature flag 已关闭")
                providers.register(DeterministicGenerationProvider(), preferred=True)
                # P2：用户在 profile 中声明了 image/video/music/voice modality
                # 且凭据可用时，接入可配置的真实 HTTP adapter。没有完整配置时
                # 不注册远程 provider，保留 deterministic stub 的 demo 能力，
                # capability matrix 会明确标成 demo 而不是 production。
                if registry is not None and credential_store is not None:
                    for media_profile in registry.list():
                        try:
                            remote_provider = make_volcengine_generation_provider(
                                media_profile, credential_store,
                            )
                            if remote_provider is None:
                                remote_provider = HttpGenerationProvider(
                                    media_profile, credential_store,
                                )
                            if (
                                remote_provider.supported_modalities
                                and media_profile.credential_ref
                                and credential_store.get_secret(media_profile.credential_ref)
                            ):
                                providers.register(remote_provider, preferred=True)
                        except (TypeError, ValueError):
                            logger.debug(
                                "remote generation profile skipped: %s",
                                getattr(media_profile, "profile_id", ""),
                                exc_info=True,
                            )
                generation_service = GenerationService(
                    providers,
                    jobs=getattr(analysis_service, "jobs", None),
                )
                self._generation_providers = providers
                if shared_runtime is not None:
                    generation_adapter = GenerationHarnessAdapter(
                        generation_service, shared_runtime,
                    )
                    set_generation = getattr(bridge, "set_generation_runtime", None)
                    if callable(set_generation):
                        set_generation(generation_service)
            except Exception:
                logger.debug("generation service wiring failed", exc_info=True)
            # 批次 3：ComfyUI 桥——按用户配置构造 RealComfyClient + executor。
            # 诚实注册红线：executor 仅在 ``bind_comfy_workflow`` 绑定 snapshot
            # 并查询过 server 能力后才注册进 ProviderRegistry（未绑定不做能力
            # 广告），避免"可注册不可跑"。
            self._comfy_profile_registry = None
            self._comfy_generation_executors = {}
            try:
                from src.features.ai_workflow.adapters.comfyui_client import (
                    RealComfyClient,
                )
                from src.features.ai_workflow.adapters.comfyui_executor import (
                    ComfyWorkflowExecutor,
                )
                from src.features.ai_workflow.adapters.comfyui_profiles import (
                    ComfyProfileRegistry,
                )
                comfy_registry = ComfyProfileRegistry()
                config_manager = getattr(self, "config_manager", None)
                if config_manager is not None:
                    comfy_registry.load_from_config(config_manager)
                self._comfy_profile_registry = comfy_registry
                for comfy_profile in comfy_registry.list():
                    client = RealComfyClient(comfy_profile, credential_store)
                    self._comfy_generation_executors[
                        comfy_profile.profile_id
                    ] = ComfyWorkflowExecutor(
                        server=client,
                        server_capabilities=client._empty_capabilities(),
                    )
            except Exception:
                logger.debug("comfyui wiring failed", exc_info=True)
            # P0.4：会话目录 store（plan §10.2 P0.4）。controller 在 run 终态/请求
            # 提交时写脱敏摘要；store 持久化由 host 在工程保存时调 persist。
            try:
                from src.features.edit.ai_editing.assistant_session_store import (
                    AssistantSessionStore,
                )
                # 默认启用本机 AI 会话缓存（上限由 store 固定为 50 个会话）；
                # 工程 payload 仍会在保存时额外写入，二者互为恢复兜底。
                session_store = AssistantSessionStore(cache_limit=50)
            except Exception:
                logger.debug("session store create failed", exc_info=True)
                session_store = None
            # P1：Skill 驱动 Agent 依赖（plan §10.3）。build_p1_dependencies 装配
            # SkillRegistry/Router/Compiler/Adapter/Help/Catalog，共享 runtime 与
            # service。任一装配失败时降级（controller 回退旧 plan 路径）。
            # P1 最后一公里：editorial_provider 接入 AnalysisService，使
            # auto_caption/subtitle_localization 拿到真实 transcript evidence（不再
            # 降级 No evidence）。
            # P1.4：HostHelpSources 接入 MainWindow 的 5 个 registry，使 HelpAssistant
            # 装配真实 HelpIndex（用户问"怎么做X"/"TAEP_XXX 是什么"拿到真实答案）。
            p1_deps: dict[str, Any] = {}
            editorial_provider: Any = None
            localization_media_provider: Any = None
            try:
                editorial_provider = make_editorial_provider(analysis_service)
            except Exception:
                logger.debug("editorial provider build failed", exc_info=True)
            try:
                localization_media_provider = make_localization_media_provider(bridge)
            except Exception:
                logger.debug("localization media provider build failed", exc_info=True)
            help_sources: Any = None
            try:
                from src.app.controllers.host_help_sources import HostHelpSources
                help_sources = HostHelpSources(self)
            except Exception:
                logger.debug("host help sources build failed", exc_info=True)
            try:
                p1_deps = build_p1_dependencies(
                    service=service, runtime=shared_runtime,
                    editorial_provider=editorial_provider,
                    has_profile=profile_connected,
                    help_sources=help_sources,
                    localization_media_provider=localization_media_provider,
                )
            except Exception:
                logger.debug("p1 dependencies build failed", exc_info=True)
            controller = AiEditController(
                panel=panel, service=service, bridge=bridge,
                runtime=shared_runtime, session_store=session_store,
                router=p1_deps.get("router"),
                compiler=p1_deps.get("compiler"),
                adapter=p1_deps.get("adapter"),
                generation_adapter=generation_adapter,
                help_assistant=p1_deps.get("help_assistant"),
                catalog=p1_deps.get("catalog"),
                skill_registry=p1_deps.get("skill_registry"),
                plugin_registry=p1_deps.get("plugin_registry"),
                plugin_event_journal=p1_deps.get("plugin_event_journal"),
                skill_plugin_host=p1_deps.get("skill_plugin_host"),
                profile_registry=registry,
                credential_store=credential_store,
            )
            self._ai_approval_service = approval_service
            self._ai_assistant_service = service
            self._ai_host_bridge = bridge
            self._ai_edit_controller = controller
            # Project/profile changes recreate the controller.  Re-attach
            # Teaching Animation here so direct Motion Graphic requests keep
            # reaching the host operation instead of falling back to chat.
            self._attach_optional_ai_hosts()
            self._ai_session_store = session_store
            # T2：把 AI 持久化（会话目录 + plan 仓库）接到工程统一 IO——
            # 工程保存时由 IO 层回调取 payload，工程加载时回调投递 payload 还原。
            self._wire_ai_persistence()

            # Controller 必须在面板首次发送请求前启动会话；否则
            # AssistantService.config 尚未初始化，request → plan 路径会在读取
            # service.config 时失败。无工程/无 profile 也显式启动，以便面板显示
            # NO_PROJECT / NO_PROFILE 这类确定状态。
            from src.features.edit.ai_editing.assistant_service import AssistantConfig

            project = getattr(self, "_current_edit_project", None)
            project_id = getattr(project, "project_id", "") if project is not None else ""
            timeline_id = (
                self._resolve_ai_timeline_id(project) if project is not None else ""
            )
            assets = getattr(project, "assets", {}) if project is not None else {}
            asset_id = next(iter(assets), "")
            controller.start(
                AssistantConfig(
                    asset_id=asset_id,
                    timeline_id=timeline_id,
                    project_id=project_id,
                ),
                profile_id=active_profile_id,
                profile_connected=profile_connected,
            )

            logger.info("AI editing subsystem wired up")

            # M3：装配媒体池分析子系统（controller + scheduler）。
            # R1 修复：这里 **不再** 私自 `AnalysisService()`——复用上面已注入
            # AssistantService 的同一实例，使媒体池 UI、AssistantService workflow 与
            # MediaAnalysisController 三者指向同一证据仓库（analyzed_count / segments
            # 完全一致）。此前这里 new 第二份实例是装配断裂的另一端。
            try:
                from src.app.controllers.media_analysis_controller import (
                    MediaAnalysisController,
                )
                from PyQt6.QtCore import QObject
                from src.features.edit.ai_editing.analysis_scheduler import (
                    AnalysisJobScheduler,
                )

                # 生产分析离开 Qt 主线程；测试/脚本可以显式使用默认同步模式。
                scheduler = AnalysisJobScheduler(asynchronous=True)
                # R4-a/R4-c：构造 ModelPackManager（指向 models/ai_editing/），供
                # OCR/ASR/分离等 provider 探测模型是否已下载。每个 pack 落地为该目录
                # 下的子目录，用户把模型文件放入对应子目录即可被 require_usable 识别。
                # P1-4：合并 vault 的模型包 spec（SigLIP2/Qwen3-VL/Qwen2.5-Omni/InsightFace
                # 等），让主项目模型管理 UI 也能看到并下载 vault 模型。
                model_pack_mgr = _build_model_pack_manager()
                # R4-c：注册 QwenASRProvider 到共享的 AnalysisService。
                # ASR provider 接 Qwen3-ASR + ForcedAligner + Sortformer。注册本身
                # 不依赖 qwen_asr 是否安装（find_spec 探测在 analyze 时发生）；
                # 未装时 analyze 抛 ModelPackNotInstalledError，由上层捕获提示用户。
                # asset_path_provider 从当前工程的 assets 解析 asset_id → 文件路径，
                # 供 ASR 提取音频（ffmpeg → 16kHz mono wav）。
                from src.features.edit.ai_editing.transcript_provider import (
                    QwenASRProvider,
                )
                from src.features.edit.ai_editing.speech_preprocessor import (
                    DemucsV4Preprocessor,
                )
                from src.features.edit.ai_editing.transcript_router import (
                    AdaptiveTranscriptProvider,
                    ProfileAwareTranscriptProvider,
                )
                from src.features.edit.ai_editing.whisperx_provider import (
                    WhisperXProvider,
                )
                from src.features.edit.ai_editing.tts_provider import Qwen3TTSProvider
                from src.features.edit.ai_editing.profile_tts_provider import (
                    ProfileAwareTtsProvider,
                )

                def _resolve_asset_path(asset_id: str) -> str:
                    project = getattr(self, "_current_edit_project", None)
                    if project is None:
                        return ""
                    asset = getattr(project, "assets", {}).get(asset_id)
                    return getattr(asset, "path", "") if asset is not None else ""

                asr_provider = QwenASRProvider(
                    model_pack_manager=model_pack_mgr,
                    asset_path_provider=_resolve_asset_path,
                )
                analysis_service.register(asr_provider)
                # Optional fallback chain used by the clip right-click action:
                # Qwen3-ASR → (low quality) Demucs vocals → WhisperX.  Missing
                # optional packages are detected lazily and never block Qwen.
                demucs_preprocessor = DemucsV4Preprocessor(
                    model_pack_manager=model_pack_mgr,
                )
                whisperx_provider = WhisperXProvider(
                    asset_path_provider=_resolve_asset_path,
                    model_pack_manager=model_pack_mgr,
                )
                adaptive_provider = AdaptiveTranscriptProvider(
                    asr_provider,
                    whisperx_provider=whisperx_provider,
                    demucs_preprocessor=demucs_preprocessor,
                )
                analysis_service.register(adaptive_provider)
                profile_transcript_provider = ProfileAwareTranscriptProvider(
                    adaptive_provider,
                    registry,
                    credential_store,
                    asset_path_provider=_resolve_asset_path,
                )
                # Keep the historical transcript_auto id while routing by the
                # active ASR capability profile.  Existing actions/recipes do
                # not need to know whether the selected backend is local or cloud.
                analysis_service.register(profile_transcript_provider)
                self._transcript_router = profile_transcript_provider
                local_tts_provider = Qwen3TTSProvider(model_pack_manager=model_pack_mgr)
                self._tts_provider = ProfileAwareTtsProvider(
                    local_tts_provider,
                    registry,
                    credential_store,
                )
                # R4-a：注册 OcrProvider 到共享的 AnalysisService。
                from src.features.edit.ai_editing.ocr_provider import OcrProvider

                ocr_provider = OcrProvider(model_pack_manager=model_pack_mgr)
                analysis_service.register(ocr_provider)
                # B2：视觉语义走同一份 AnalysisService/调度器与证据索引。模型
                # 后端只会在主应用的严格 SHA 校验通过后装配；未安装时 provider
                # 仍给出可行动的失败，不会用 Vault 的 deterministic stub 伪造结果。
                from pathlib import Path
                from src.features.edit.ai_editing.visual_semantic_provider import (
                    VideoAssetFrameSampler,
                    build_vault_visual_provider,
                )

                def _total_frames_for_asset(asset_id: str) -> int:
                    project = getattr(self, "_current_edit_project", None)
                    asset = getattr(project, "assets", {}).get(asset_id) if project else None
                    if asset is None:
                        return 0
                    direct = getattr(asset, "total_frames", None)
                    metadata = getattr(asset, "metadata", {}) or {}
                    value = direct or metadata.get("total_frames") or metadata.get("frame_count")
                    try:
                        return max(0, int(value or 0))
                    except (TypeError, ValueError):
                        return 0

                def _visual_sampler_for_asset(asset_id: str) -> Any:
                    return VideoAssetFrameSampler(_resolve_asset_path(asset_id))

                def _scene_cuts_for_asset(asset_id: str) -> tuple[dict[str, Any], ...]:
                    previous = analysis_service.get_analysis(asset_id)
                    return tuple(getattr(previous, "scene_cuts", ()) or ())

                visual_provider = build_vault_visual_provider(
                    model_pack_manager=model_pack_mgr,
                    sampler_provider=_visual_sampler_for_asset,
                    total_frames_provider=_total_frames_for_asset,
                    scene_cuts_provider=_scene_cuts_for_asset,
                    cache_root=Path(model_pack_mgr.cache_dir) / "visual_cache",
                )
                analysis_service.register(visual_provider)
                self._model_pack_manager = model_pack_mgr
                media_ctrl = MediaAnalysisController(
                    analysis_service=analysis_service,
                    scheduler=scheduler,
                    project_getter=lambda: getattr(self, "_current_edit_project", None),
                    # MainWindow 是 QObject；轻量测试宿主不一定是。不要让一个
                    # 无关的 Qt parent 类型错误阻断 AI 分析装配。
                    parent=self if isinstance(self, QObject) else None,
                )
                self._analysis_scheduler = scheduler
                self._analysis_service = analysis_service
                self._media_analysis_controller = media_ctrl
                # AI 对话与媒体池共享同一分析 controller：聊天中的“分析/转录/OCR”
                # 现在会进入真实 propose → confirm → execute 闭环。
                if self._ai_edit_controller is not None:
                    self._ai_edit_controller.set_analysis_controller(media_ctrl)
                # R1：记录共享实例，供启动自检与 _inject_assistant_service_providers
                # 复用（同一实例引用断言）。
                self._shared_analysis_service = analysis_service
                logger.info(
                    "Media analysis subsystem wired up (M3, shared service, "
                    "model_packs at %s)", model_pack_mgr.cache_dir,
                )
            except Exception:
                logger.error("Failed to set up media analysis subsystem", exc_info=True)
                self._media_analysis_controller = None
                self._analysis_scheduler = None
                self._analysis_service = None
                self._model_pack_manager = None
                self._transcript_router = None
                self._tts_provider = None
                self._shared_analysis_service = None
        except Exception:
            # 装配失败不吞：记录错误，让 UI 保持未装配（面板仍是空壳状态）。
            logger.error("Failed to set up AI editing subsystem", exc_info=True)
            # AUD-2200：错误路径必须与正常 teardown 资源状态一致。走到这里时
            # MCP host（676-679 已 start）、ToolFacade、generation providers、
            # comfy executors 可能已部分启动——半拆除状态下本地 Unix socket
            # 仍对外服务、facade 仍指向已置 None 的装配。复用 _teardown_ai_
            # editing（stop host / dispose controller / close generation
            # service / 撤 grants / unbind session），再显式清 teardown 不覆盖
            # 的 provider/executor 残留。
            try:
                self._teardown_ai_editing()
            except Exception:
                logger.error("AI editing error-path teardown failed", exc_info=True)
            self._generation_providers = None
            self._comfy_generation_executors = {}
            self._comfy_profile_registry = None
            self._ai_edit_controller = None
            self._ai_assistant_service = None
            self._ai_host_bridge = None
            self._ai_approval_service = None
            self._ai_session_store = None

    # ------------------------------------------------------------------ #
    # T2：AI payload 持久化接线（会话目录 + plan 仓库 ↔ .topos 工程包）  #
    # ------------------------------------------------------------------ #

    def _wire_ai_persistence(self) -> None:
        """把 AI session store / plan repository 接到 ``EditProjectIO`` 的回调。

        - 保存：``get_ai_payload`` 返回 ``{"session": catalog.to_dict(),
          "plans": repo.export_all()}``；未装配/空仓返回 None 跳过。
        - 加载：``on_ai_payload_loaded`` 把磁盘上的 catalog/plans 还原进 store/repo。

        幂等：EditProjectIO 的 set_callbacks 会覆盖旧回调，工程切换时自动重接。
        失败不抛（持久化失败不应阻断保存主流程），仅 debug 日志。
        """
        edit_project_io = getattr(self, "edit_project_io", None)
        if edit_project_io is None:
            return
        try:
            edit_project_io.set_callbacks(
                get_project=(
                    getattr(self, "_get_current_edit_project", None)
                ),
                get_ui_state=getattr(self, "_get_edit_ui_state", None),
                get_timeline_sequences=getattr(self, "_get_timeline_sequences", None),
                get_ai_payload=self._collect_ai_payload,
                on_ai_payload_loaded=self._restore_ai_payload,
            )
        except Exception:  # noqa: BLE001
            logger.debug("wire_ai_persistence: set_callbacks failed", exc_info=True)

    def _collect_ai_payload(self) -> dict[str, Any] | None:
        """供工程保存回调：收集 AI session、plan 与 Job checkpoint 摘要。

        返回 ``{"session": dict|None, "plans": dict|None, "jobs": dict|None,
        "analysis": dict|None, "runtime": dict|None}``；
        全部为空时返回 None（EditProjectIO 据此跳过 ai/ 模块写入）。
        """
        session_data = None
        store = getattr(self, "_ai_session_store", None)
        if store is not None and hasattr(store, "catalog"):
            try:
                catalog = store.catalog
                # 空目录（无会话 + 无 message 摘要）不写，避免每工程都带空 ai/ 目录。
                if catalog.sessions or catalog.legacy_snapshot:
                    session_data = catalog.to_dict()
            except Exception:  # noqa: BLE001
                logger.debug("collect_ai_payload: catalog to_dict failed", exc_info=True)
        plans_data = None
        service = getattr(self, "_ai_assistant_service", None)
        repo = getattr(service, "plan_repository", None)
        if repo is not None and hasattr(repo, "export_all"):
            try:
                plans_data = repo.export_all()
                # 空仓不写
                if not plans_data.get("plans"):
                    plans_data = None
            except Exception:  # noqa: BLE001
                logger.debug("collect_ai_payload: repo export failed", exc_info=True)
        jobs_data = None
        jobs = getattr(getattr(self, "_shared_analysis_service", None), "jobs", None)
        if jobs is None:
            generation_adapter = getattr(getattr(self, "_ai_edit_controller", None), "generation_adapter", None)
            jobs = getattr(getattr(generation_adapter, "_svc", None), "job_registry", None)
        if jobs is not None and callable(getattr(jobs, "to_dict", None)):
            try:
                if getattr(jobs, "list_all", lambda: ())():
                    jobs_data = jobs.to_dict()
            except Exception:  # noqa: BLE001
                logger.debug("collect_ai_payload: jobs to_dict failed", exc_info=True)
        analysis_data = None
        analysis_service = getattr(self, "_shared_analysis_service", None)
        if analysis_service is not None and callable(getattr(analysis_service, "export_all", None)):
            try:
                analysis_data = analysis_service.export_all()
            except Exception:  # noqa: BLE001
                logger.debug("collect_ai_payload: analysis export failed", exc_info=True)
        runtime_data = None
        runtime = getattr(self, "_shared_runtime", None)
        if runtime is None:
            runtime = getattr(self, "_ai_workflow_runtime", None)
        if runtime is not None and callable(getattr(runtime, "export_snapshot", None)):
            try:
                snapshot = runtime.export_snapshot()
                if (
                    snapshot.get("runs")
                    or snapshot.get("detached_sessions")
                ):
                    runtime_data = snapshot
            except Exception:  # noqa: BLE001
                logger.debug("collect_ai_payload: runtime export failed", exc_info=True)
        controller = getattr(self, "_ai_edit_controller", None)
        trace_data = None
        export_trace = getattr(controller, "export_run_trace", None)
        if callable(export_trace):
            try:
                candidate = export_trace()
                if candidate.get("sessions"):
                    trace_data = candidate
            except Exception:  # noqa: BLE001
                logger.debug("collect_ai_payload: run trace export failed", exc_info=True)
        checkpoint_data = None
        audit_data = None
        mission_data = None
        goal_data = None
        goal_checkpoint_data = None
        tool_operation_ledger_data = None
        context_spill_data = None
        export_checkpoints = getattr(controller, "export_autonomous_checkpoints", None)
        if callable(export_checkpoints):
            checkpoint_data = export_checkpoints()
        export_audit = getattr(controller, "export_autonomous_audit", None)
        if callable(export_audit):
            audit_data = export_audit()
        export_missions = getattr(controller, "export_autonomous_missions", None)
        if callable(export_missions):
            mission_data = export_missions()
        export_goals = getattr(controller, "export_autonomous_goals", None)
        if callable(export_goals):
            goal_data = export_goals()
        export_goal_checkpoints = getattr(controller, "export_autonomous_goal_checkpoints", None)
        if callable(export_goal_checkpoints):
            goal_checkpoint_data = export_goal_checkpoints()
        export_tool_operation_ledger = getattr(controller, "export_tool_operation_ledger", None)
        if callable(export_tool_operation_ledger):
            tool_operation_ledger_data = export_tool_operation_ledger()
        export_context_spills = getattr(controller, "export_context_spills", None)
        if callable(export_context_spills):
            context_spill_data = export_context_spills()
        if (
            checkpoint_data or audit_data or mission_data or goal_data
            or goal_checkpoint_data or tool_operation_ledger_data or context_spill_data
        ):
            if runtime_data is None:
                runtime_data = {
                    "schema_version": 1,
                    "detached_sessions": [],
                    "runs": [],
                }
            # HarnessRuntime deliberately ignores unknown snapshot keys;
            # keeping these fields here preserves the existing .topos contract.
            if checkpoint_data:
                runtime_data["autonomous"] = checkpoint_data
            if audit_data:
                runtime_data["autonomous_audit"] = audit_data
            if mission_data:
                runtime_data["autonomous_missions"] = mission_data
            if goal_data:
                runtime_data["autonomous_goals"] = goal_data
            if goal_checkpoint_data:
                runtime_data["autonomous_goal_checkpoints"] = goal_checkpoint_data
            if tool_operation_ledger_data:
                runtime_data["tool_operation_ledger"] = tool_operation_ledger_data
            if context_spill_data:
                runtime_data["context_spills"] = context_spill_data
        plugin_data = None
        export_plugin_state = getattr(controller, "export_plugin_state", None)
        if callable(export_plugin_state):
            try:
                plugin_data = export_plugin_state()
            except Exception:  # noqa: BLE001
                logger.debug("collect_ai_payload: plugin state export failed", exc_info=True)
        if (
            session_data is None and plans_data is None and jobs_data is None
            and analysis_data is None and runtime_data is None and trace_data is None
            and plugin_data is None
        ):
            return None
        return {
            "session": session_data,
            "plans": plans_data,
            "jobs": jobs_data,
            "analysis": analysis_data,
            "runtime": runtime_data,
            "trace": trace_data,
            "plugins": plugin_data,
        }

    def _restore_ai_payload(self, payload: dict[str, Any]) -> None:
        """供工程加载回调：把磁盘上的 AI payload 还原进 store / plan repository。

        无对应数据（旧工程/非 AI 工作流）静默跳过。还原后 plan 的 preview 是
        历史子集（无 compiled/proposed_sequence），commit 前必须重新 preview。
        """
        if not isinstance(payload, dict):
            return
        controller = getattr(self, "_ai_edit_controller", None)
        restore_plugin_state = getattr(controller, "restore_plugin_state", None)
        if callable(restore_plugin_state):
            try:
                restore_plugin_state(payload.get("plugins"))
            except Exception:  # noqa: BLE001
                logger.debug("restore_ai_payload: plugin state restore failed", exc_info=True)
        trace_data = payload.get("trace")
        restore_trace = getattr(controller, "restore_run_trace", None)
        if callable(restore_trace) and trace_data:
            try:
                restore_trace(trace_data)
            except Exception:  # noqa: BLE001
                logger.debug("restore_ai_payload: run trace restore failed", exc_info=True)
        # 还原会话目录
        catalog_data = payload.get("session_catalog") or payload.get("session")
        store = getattr(self, "_ai_session_store", None)
        if store is not None and catalog_data:
            try:
                from src.features.edit.ai_editing.session_models import (
                    SessionCatalog,
                )
                restore_catalog = getattr(store, "restore_catalog", None)
                if callable(restore_catalog):
                    restore_catalog(catalog_data)
                else:
                    store._catalog = SessionCatalog.from_dict(catalog_data)
            except Exception:  # noqa: BLE001
                logger.debug("restore_ai_payload: catalog restore failed", exc_info=True)
            # 关键修复：把 active session 的脱敏消息摘要回灌进 AssistantService 的
            # 内存 _messages。持久化层（MessageSummary）只用于审计落盘，原先从不
            # 回灌——导致重开工程后多轮对话"记不住"（LLM 看到空历史）。
            # MessageSummary 只存角色 + 脱敏文本片段（前 500 字），可安全重建
            # 为自由对话的 AssistantMessage（契约允许所有 ref 为 None）。
            service = getattr(self, "_ai_assistant_service", None)
            restore = getattr(service, "restore_chat_messages", None)
            if callable(restore):
                try:
                    from src.features.edit.ai_editing.assistant_events import (
                        AssistantMessage,
                    )
                    active = store.catalog.get_or_create_active()
                    restored = tuple(
                        AssistantMessage(
                            message_id=m.message_id,
                            role=m.role,
                            text=m.text,
                        )
                        for m in active.messages
                    )
                    restore(restored)
                except Exception:  # noqa: BLE001
                    logger.debug("restore_ai_payload: chat history hydrate failed", exc_info=True)
            controller = getattr(self, "_ai_edit_controller", None)
            refresh_history = getattr(controller, "_refresh_history_menu", None)
            if callable(refresh_history):
                refresh_history()
        # 还原 plan 仓库
        plans_data = payload.get("plans")
        service = getattr(self, "_ai_assistant_service", None)
        repo = getattr(service, "plan_repository", None)
        if repo is not None and plans_data:
            try:
                repo.clear()
                n = repo.import_all(plans_data)
                logger.debug("restored %d AI plans from .topos payload", n)
            except Exception:  # noqa: BLE001
                logger.debug("restore_ai_payload: repo import failed", exc_info=True)
        # 恢复共享 JobRegistry。活动 job 默认安全终止，不恢复孤儿 worker，
        # provider 需要由显式 retry/resume 入口重新建立幂等任务。
        jobs_data = payload.get("jobs")
        jobs = getattr(getattr(self, "_shared_analysis_service", None), "jobs", None)
        if jobs is None:
            generation_adapter = getattr(getattr(self, "_ai_edit_controller", None), "generation_adapter", None)
            jobs = getattr(getattr(generation_adapter, "_svc", None), "job_registry", None)
        if jobs is not None and jobs_data and callable(getattr(jobs, "restore_dict", None)):
            try:
                if jobs.restore_dict(jobs_data, terminate_active=True):
                    logger.debug("restored AI jobs with active jobs safely terminated")
            except Exception:  # noqa: BLE001
                logger.debug("restore_ai_payload: jobs restore failed", exc_info=True)
        analysis = getattr(self, "_shared_analysis_service", None)
        analysis_data = payload.get("analysis")
        if analysis is not None and analysis_data and callable(getattr(analysis, "restore_all", None)):
            try:
                restored = analysis.restore_all(analysis_data)
                logger.debug("restored %d AI analysis cache records", restored)
            except Exception:  # noqa: BLE001
                logger.debug("restore_ai_payload: analysis restore failed", exc_info=True)
        runtime_data = payload.get("runtime")
        if runtime_data:
            runtime = getattr(self, "_shared_runtime", None)
            if runtime is None:
                runtime = getattr(self, "_ai_workflow_runtime", None)
            if runtime is None:
                try:
                    runtime = resolve_shared_runtime(self)
                except Exception:  # noqa: BLE001
                    logger.debug("restore_ai_payload: runtime resolve failed", exc_info=True)
            if runtime is not None and callable(getattr(runtime, "restore_snapshot", None)):
                try:
                    report = runtime.restore_snapshot(runtime_data, terminate_active=True)
                    logger.debug(
                        "restored AI runtime: %d runs, %d interrupted, %d skipped",
                        len(report.restored_run_ids),
                        len(report.interrupted_run_ids),
                        len(report.skipped_entries),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("restore_ai_payload: runtime restore failed", exc_info=True)
            restore_goals = getattr(controller, "restore_autonomous_goals", None)
            if callable(restore_goals):
                try:
                    restore_goals(runtime_data.get("autonomous_goals"))
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "restore_ai_payload: autonomous goal restore failed",
                        exc_info=True,
                    )
            restore_goal_checkpoints = getattr(
                controller, "restore_autonomous_goal_checkpoints", None,
            )
            if callable(restore_goal_checkpoints):
                try:
                    restore_goal_checkpoints(runtime_data.get("autonomous_goal_checkpoints"))
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "restore_ai_payload: autonomous goal checkpoint restore failed",
                        exc_info=True,
                    )
            restore_tool_operation_ledger = getattr(
                controller, "restore_tool_operation_ledger", None,
            )
            if callable(restore_tool_operation_ledger):
                try:
                    restore_tool_operation_ledger(runtime_data.get("tool_operation_ledger"))
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "restore_ai_payload: tool operation ledger restore failed",
                        exc_info=True,
                    )
            restore_context_spills = getattr(controller, "restore_context_spills", None)
            if callable(restore_context_spills):
                try:
                    restore_context_spills(runtime_data.get("context_spills"))
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "restore_ai_payload: context spill restore failed",
                        exc_info=True,
                    )
            restore_missions = getattr(controller, "restore_autonomous_missions", None)
            if callable(restore_missions):
                try:
                    # Mission state must be present before checkpoint restore can
                    # attempt a live, fail-closed continuation.
                    restore_missions(runtime_data.get("autonomous_missions"))
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "restore_ai_payload: autonomous mission restore failed",
                        exc_info=True,
                    )
            restore_audit = getattr(controller, "restore_autonomous_audit", None)
            if callable(restore_audit):
                try:
                    # Audit history is available before a checkpoint can be
                    # resumed, so crash-window recovery has complete evidence.
                    restore_audit(runtime_data.get("autonomous_audit"))
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "restore_ai_payload: autonomous audit restore failed",
                        exc_info=True,
                    )
            restore_checkpoints = getattr(controller, "restore_autonomous_checkpoints", None)
            if callable(restore_checkpoints):
                try:
                    restore_checkpoints(runtime_data.get("autonomous"))
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "restore_ai_payload: autonomous checkpoint restore failed",
                        exc_info=True,
                    )

    def _open_ai_model_preferences(self) -> None:
        """打开并定位到 Preferences 的 AI Editing 页面。"""
        open_preferences = getattr(self, "show_preferences_dialog", None)
        if not callable(open_preferences):
            return
        try:
            open_preferences(initial_page_id="edit.ai_editing")
        except TypeError:
            # 兼容旧版宿主方法签名，至少仍能打开 Preferences。
            open_preferences()

    def _inject_assistant_service_providers(
        self, service: Any, registry: Any, *, analysis_service: Any = None,
    ) -> None:
        """注入 profile/context 查询到 AssistantService（M5 / R1）。

        - profile_lookup：供 service 派生 allow_media_upload（§10）。
        - context_provider：供 service 派生 ContextSummary（asset/分析/锁定计数）。
        - analysis_service：R1 修复——传入驻 AssistantService 的同一份 AnalysisService，
          使 analyzed_count / analysis_fresh 从真实证据仓库派生（而非 has_proxy 冒充、
          恒 False 占位）。registry 为 None 时 profile 查询退化为 None（service 回退
          安全默认值），但 context_provider 仍可基于 analysis_service 派生。
        """
        if registry is not None:
            def _lookup_profile(profile_id: str) -> Any:
                try:
                    return registry.get(profile_id)
                except Exception:
                    return None

            service.set_profile_lookup(_lookup_profile)

        def _context_field(name: str) -> Any:
            project = getattr(self, "_current_edit_project", None)
            if project is None:
                return 0 if name != "analysis_fresh" else False
            try:
                assets = getattr(project, "assets", {}) or {}
                if name == "asset_count":
                    return len(assets)
                if name == "analyzed_count":
                    # R1：从真实 AnalysisService 派生（status == "ready" 的 asset 计为
                    # 已分析）。此前用 has_proxy（代理媒体存在性）冒充分析完成数——二者
                    # 是完全不同的概念。无 analysis_service 时退回 0（明确无依据，
                    # 不再静默用代理文件冒充）。
                    if analysis_service is None:
                        return 0
                    return sum(
                        1 for aid in assets
                        if _safe_get_status(analysis_service, aid) == "ready"
                    )
                if name == "music_count":
                    return sum(1 for a in assets.values() if getattr(a, "media_type", "") == "audio")
                if name == "locked_count":
                    # R1：从 AssistantService 自身的 decision 状态派生 LOCKED 计数。
                    # 此前恒为 0。decision 字典是 service 的内部状态，故经 service
                    # 提供的计数接口读取（不穿透私有字段）。
                    return _count_locked_decisions(service)
                if name == "snapshot_revision":
                    return getattr(project, "revision", 0)
                if name == "analysis_fresh":
                    # R1：聚合每个已分析 asset 的 fingerprint 是否仍有效。此前恒 False。
                    # 任一已分析 asset 过期（素材被替换/修改）即视为整体不新鲜——
                    # 提示 Agent 重新分析。无 analysis_service 或无已分析记录时返回
                    # True（无证据可过期，避免在未分析场景误报 stale）。
                    if analysis_service is None:
                        return True
                    return _all_analysis_fresh(analysis_service, assets)
            except Exception:
                logger.debug("context field %r computation failed", name, exc_info=True)
                return 0 if name != "analysis_fresh" else False
            return None

        service.set_context_provider(_context_field)

        # R1：启动自检——开发模式下断言 AssistantService 持有的 analysis_service 与
        # 这里注入 context_provider 的 analysis_service 是同一实例（否则证据会
        # 静默泄漏 / 回退 Null）。生产模式只记日志，不阻断启动。
        self._verify_shared_analysis_service(service, analysis_service)

    def _teardown_ai_editing(self) -> None:
        """拆除当前 AI 装配：撤销 grants、unbind session、清引用。

        在工程关闭/切换前调用。无装配时是 no-op。不抛。
        """
        project_id: str | None = None
        # 工程切换时 `_current_edit_project` 已可能指向新工程。优先从旧 service
        # 的启动配置取 project_id，才能撤销真正旧工程的 grants。
        previous_service = getattr(self, "_ai_assistant_service", None)
        if previous_service is not None:
            try:
                project_id = previous_service.config.project_id or None
            except (AttributeError, RuntimeError):
                project_id = None
        project = getattr(self, "_current_edit_project", None)
        if project_id is None and project is not None:
            project_id = getattr(project, "project_id", None)

        controller = getattr(self, "_ai_edit_controller", None)
        mcp_host = getattr(self, "_ai_mcp_host", None)
        if mcp_host is not None:
            try:
                mcp_host.stop()
            except Exception:
                logger.debug("Local MCP host shutdown failed", exc_info=True)
        self._ai_mcp_host = None
        self._ai_mcp_dispatcher = None
        self._ai_mcp_socket_path = ""
        self._ai_tool_facade = None
        # MCP 视觉网关：关闭其子进程（@z_ai/mcp-server）。
        try:
            from src.features.edit.ai_editing.mcp_vision_gateway import (
                shutdown_mcp_vision_gateway,
            )
            shutdown_mcp_vision_gateway()
        except Exception:
            logger.debug("MCP vision gateway shutdown failed", exc_info=True)
        if controller is not None:
            try:
                controller.dispose()
            except Exception:
                logger.debug("AI controller dispose failed", exc_info=True)

        # P2/P7：生成 provider 共享 AnalysisService 的 JobRegistry；关闭工程时停止
        # 由 GenerationService 创建的非 Qt worker，避免远程任务持有旧工程引用。
        generation_adapter = getattr(controller, "generation_adapter", None) if controller is not None else None
        generation_service = getattr(generation_adapter, "_svc", None)
        close_generation = getattr(generation_service, "close", None)
        if callable(close_generation):
            try:
                close_generation()
            except Exception:
                logger.debug("Generation worker shutdown failed", exc_info=True)

        bridge = getattr(self, "_ai_host_bridge", None)
        if bridge is not None and project_id is not None:
            try:
                bridge.teardown_for_project(project_id)
            except Exception:
                logger.debug("AI bridge teardown failed", exc_info=True)
        elif project_id is not None:
            approval_service = getattr(self, "_ai_approval_service", None)
            if approval_service is not None:
                try:
                    approval_service.revoke_for_project(project_id)
                except Exception:
                    logger.debug("AI approval teardown failed", exc_info=True)

        # 解绑 EditSession（service 持有的 session 不再有效）
        try:
            from src.features.edit.ai_editing.edit_api import EditSession

            EditSession.unbind()
        except Exception:
            pass

        # M3：拆除媒体池分析子系统——cancel 所有 pending/running 分析任务，
        # 保证工程关闭后无 orphan worker（§17.7）。controller.on_project_changed(None)
        # 内部调 scheduler.cancel_all() + 清 request 映射。
        media_ctrl = getattr(self, "_media_analysis_controller", None)
        if media_ctrl is not None:
            try:
                media_ctrl.on_project_changed(None)
            except Exception:
                logger.debug("Media analysis teardown failed", exc_info=True)

        scheduler = getattr(self, "_analysis_scheduler", None)
        if scheduler is not None and hasattr(scheduler, "shutdown"):
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                logger.debug("Analysis scheduler shutdown failed", exc_info=True)

        speech_worker = getattr(self, "_speech_to_text_worker", None)
        if speech_worker is not None and speech_worker.isRunning():
            speech_worker.blockSignals(True)
            speech_worker.requestInterruption()
        self._speech_to_text_worker = None
        tts_worker = getattr(self, "_subtitle_to_speech_worker", None)
        if tts_worker is not None and tts_worker.isRunning():
            tts_worker.blockSignals(True)
            tts_worker.requestInterruption()
        self._subtitle_to_speech_worker = None

        self._ai_edit_controller = None
        self._ai_assistant_service = None
        self._ai_host_bridge = None
        self._ai_approval_service = None
        self._ai_session_store = None
        self._media_analysis_controller = None
        self._analysis_scheduler = None
        self._analysis_service = None
        self._shared_analysis_service = None
        self._model_pack_manager = None
        self._transcript_router = None
        self._tts_provider = None

    # ------------------------------------------------------------------ #
    # R1：启动自检——开发模式下断言装配根共享同一 AnalysisService。
    # ------------------------------------------------------------------ #

    def _verify_shared_analysis_service(
        self, service: Any, analysis_service: Any,
    ) -> None:
        """断言 AssistantService 持有的 analysis_service 与宿主共享的实例一致。

        若不一致（装配链路断裂）：开发模式（``_ai_strict_composition=True``）直接抛
        ``RuntimeError``，便于早暴露；生产模式只记 error 日志并禁用"基于分析剪辑"路径
        （service 会回退 _NullAnalysisReader，UI 显示"分析未接线"）。不阻断主功能。
        """
        if analysis_service is None:
            return  # 无 AnalysisService 可共享（如装配失败），由上层处理
        service_internal = getattr(service, "_analysis_service", None)
        if service_internal is None or service_internal is analysis_service:
            return  # 一致或 service 未持有（兼容）
        # 装配断裂：service 持有了另一份 AnalysisService（或 Null 兜底）
        msg = (
            "Composition root wiring broken: AssistantService._analysis_service "
            "differs from the shared AnalysisService; agent will not see media "
            "analysis evidence. This indicates a regression in _setup_ai_editing."
        )
        if getattr(self, "_ai_strict_composition", False):
            raise RuntimeError(msg)
        logger.error(msg)

    # ------------------------------------------------------------------ #
    # 批次 3：ComfyUI 桥（绑定 / 探活；诚实注册——未绑定的 executor 不进
    # ProviderRegistry，不做能力广告）。
    # ------------------------------------------------------------------ #

    def bind_comfy_workflow(self, snapshot: Any) -> tuple[bool, str]:
        """把 ComfyUI workflow snapshot 绑定到活动 profile 的 executor。

        步骤：查询 server 能力（``/object_info``）→ 刷新 preflight 能力集 →
        ``bind_workflow`` → 注册进 GenerationService 的 ProviderRegistry。
        任何一步失败都返回 ``(False, reason)``，不半注册。

        线程契约（AUD-2205）：本方法内同步执行 HTTP（``/object_info``，
        大模型库上响应可达数 MB），**禁止在 GUI 线程调用**——请从 worker/
        任务队列调用；此处把探测超时从默认 30s 收窄到
        ``_COMFY_PROBE_TIMEOUT_S``，即使误用也不会冻结 UI 半分钟。
        """
        registry = getattr(self, "_comfy_profile_registry", None)
        executors = getattr(self, "_comfy_generation_executors", None) or {}
        profile = registry.active_profile if registry is not None else None
        if profile is None:
            return False, "未配置活动的 ComfyUI 连接 profile"
        executor = executors.get(profile.profile_id)
        if executor is None:
            return False, f"ComfyUI executor 未装配（profile {profile.profile_id}）"
        client = getattr(executor, "_server", None)
        try:
            with _comfy_probe_timeout(client):
                caps = client.capabilities()
            executor.set_server_capabilities(caps)
            executor.bind_workflow(snapshot, profile=profile)
        except Exception as exc:  # noqa: BLE001 — 绑定失败不半注册
            return False, f"ComfyUI server 能力查询/绑定失败：{exc}"
        providers = getattr(self, "_generation_providers", None)
        if providers is not None:
            try:
                providers.register(executor)
            except Exception:  # noqa: BLE001 — 重复注册等按幂等处理
                logger.debug("comfy provider re-register skipped", exc_info=True)
        return True, f"已绑定 workflow → {profile.base_url}"

    def test_comfy_connection(self, profile_id: str = "") -> tuple[bool, str]:
        """设置/诊断用：测试 ComfyUI server 连通性（``/system_stats``）。

        线程契约（AUD-2205）：``/system_stats`` 是同步 HTTP 请求，**禁止在
        GUI 线程调用**；探测超时收窄到 ``_COMFY_PROBE_TIMEOUT_S``，避免
        server 不可达时调用线程最长冻结 30s。
        """
        executors = getattr(self, "_comfy_generation_executors", None) or {}
        registry = getattr(self, "_comfy_profile_registry", None)
        if profile_id:
            executor = executors.get(profile_id)
        else:
            profile = registry.active_profile if registry is not None else None
            executor = executors.get(profile.profile_id) if profile else None
        client = getattr(executor, "_server", None) if executor is not None else None
        if client is None:
            return False, "未找到对应的 ComfyUI client（profile 未配置或未装配）"
        try:
            with _comfy_probe_timeout(client):
                return client.test_connection()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


# ---------------------------------------------------------------------- #
# R1：context provider 辅助函数（模块级，便于测试单独覆盖）。
# ---------------------------------------------------------------------- #


def _safe_get_status(analysis_service: Any, asset_id: str) -> str:
    """安全读取 AnalysisService.get_status（异常时返回 "unknown"）。"""
    try:
        return str(analysis_service.get_status(asset_id) or "unknown")
    except Exception:
        return "unknown"


def _count_locked_decisions(service: Any) -> int:
    """从 AssistantService 的 decision 字典派生 LOCKED 计数。

    经 service 的公开 decision_cards / decisions 访问；不穿透私有字段名，
    避免对内部实现细节的硬依赖。无 decisions 时返回 0。
    """
    # 优先用 AssistantService 暴露的 snapshot（state_changed 时已聚合）；
    # 其次回退到 _decisions 字典（保留兼容，不强耦合私有名）。
    decisions = getattr(service, "_decisions", None) or ()
    if hasattr(decisions, "values"):
        decisions = decisions.values()
    count = 0
    for d in decisions:
        state = getattr(d, "state", None)
        state_val = getattr(state, "value", state)
        if state_val == "locked":
            count += 1
    return count


def _all_analysis_fresh(analysis_service: Any, assets: Any) -> bool:
    """聚合判定所有已分析 asset 的 fingerprint 是否仍有效。

    - 无 asset 或无任何已分析记录 → True（无证据可过期，避免误报 stale）。
    - 任一已分析 asset 过期 → False（提示 Agent 触发重分析）。
    - fingerprint 计算失败（如文件不可访问）→ True（不因 IO 异常误报过期）。
    """
    try:
        asset_items = list(assets.items()) if hasattr(assets, "items") else list(assets)
    except Exception:
        return True
    if not asset_items:
        return True
    for aid, asset in asset_items:
        # assets 可能是 dict[asset_id -> MediaAsset] 或其他形态
        if isinstance(aid, tuple):
            # 非 dict.items() 形态（即 (asset_id, asset) 对），取第一项
            aid_value, asset_obj = aid, asset
        else:
            aid_value, asset_obj = aid, asset
        status = _safe_get_status(analysis_service, aid_value)
        if status == "unknown":
            continue  # 未分析，不参与新鲜度判定
        current_fp = _fingerprint_from_asset(asset_obj)
        if current_fp is None:
            continue  # 无法构造当前指纹（如虚拟素材），跳过
        try:
            if not analysis_service.is_valid(aid_value, current_fp):
                return False
        except Exception:
            continue  # is_valid 异常不误报
    return True  # 无已分析记录或全部有效


def _fingerprint_from_asset(asset: Any) -> Any:
    """从 MediaAsset 构造当前 AssetFingerprint（size + mtime_ns）。

    R1 的 analysis_fresh 判定需要"当前素材指纹"与"分析时记录的指纹"对比。asset.path
    不存在或不可 stat 时返回 None（调用方据此跳过该 asset 的新鲜度判定，不误报）。
    虚拟素材（comp_clip / solid 等无 path）也返回 None。
    """
    import os

    from src.features.edit.ai_editing.analysis_models import AssetFingerprint

    path = getattr(asset, "path", "") or ""
    if not path:
        return None
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return AssetFingerprint(size=st.st_size, mtime_ns=st.st_mtime_ns)


class _SessionHostShim:
    """最小 EditSessionHost 实现，供 AssistantService 通过 session 取 snapshot。

    本批 apply 已改走 bridge.commit_ai_command（真实 CommandStack），不再经
    session.commit；session 只用于 get_state/get_manifest（snapshot/preview）。
    """

    def __init__(self, mainwindow: Any) -> None:
        self._mw = mainwindow

    def get_state(self):
        from src.features.edit.ai_editing.edit_project_service import ProjectSequences

        project = getattr(self._mw, "_current_edit_project", None)
        sequences = getattr(self._mw, "_current_timeline_sequences", None)
        if project is None or sequences is None:
            return None
        return ProjectSequences(project=project, sequences=dict(sequences))

    def get_manifest(self):
        from src.features.edit.ai_editing.capability_manifest import build_manifest

        return build_manifest()

    def on_committed(self, result) -> None:
        # 本批 apply 不经 session.commit；此方法不会被调用。保留以满足协议。
        return None

    def request_user_approval(self, plan, diff):
        # 同上；授权经 bridge.request_plan_approval。
        return None

    def approval_service(self):
        svc = getattr(self._mw, "_ai_approval_service", None)
        return svc

    # Composite is exposed through the same EditSession used by the AI tool
    # facade. Keep this shim deliberately thin: validation/mutation semantics
    # live in CompositeEditService, while the MainWindow remains responsible
    # for approval and native undo/UI refresh.
    def get_composite_snapshot(self):
        from src.features.composite.edit_service import CompositeEditService

        page = getattr(self._mw, "composite_page", None)
        if page is None or getattr(page, "node_graph", None) is None:
            return None
        snapshot = CompositeEditService(page.node_graph).snapshot()
        snapshot["composition_id"] = getattr(page, "_current_composite_id", "") or None
        snapshot["project_id"] = self.get_project_id() or None
        return snapshot

    def validate_composite_edit(self, plan):
        from src.features.composite.edit_service import (
            CompositeDiagnostic, CompositeEditService, CompositeValidationReport,
        )

        page = getattr(self._mw, "composite_page", None)
        if page is None or getattr(page, "node_graph", None) is None:
            return CompositeValidationReport((CompositeDiagnostic("error", "Composite page is not available"),))
        return CompositeEditService(page.node_graph).validate(plan)

    def apply_composite_edit(self, plan, *, approval=None, client="ai"):
        from src.features.composite.edit_service import (
            CompositeApplyResult, CompositeDiagnostic, CompositeEditService,
            CompositeValidationReport,
        )

        page = getattr(self._mw, "composite_page", None)
        if page is None or getattr(page, "node_graph", None) is None:
            report = CompositeValidationReport((CompositeDiagnostic("error", "Composite page is not available"),))
            return CompositeApplyResult(False, plan.plan_id, plan.content_hash(), 0, 0, report=report, error="Composite page is not available")
        service = CompositeEditService(page.node_graph)
        report = service.validate(plan)
        current = service.generation
        if not report.ok:
            return CompositeApplyResult(False, plan.plan_id, plan.content_hash(), current, current, report=report, error="composite plan validation failed")
        if client != "script":
            from src.features.edit.ai_editing.approval_service import ApprovalScope, ApprovalGrant

            if approval is None:
                error = "host approval is required"
                return CompositeApplyResult(False, plan.plan_id, plan.content_hash(), current, current, report=CompositeValidationReport((CompositeDiagnostic("error", error),)), error=error)
            try:
                grant_id = approval.grant_id if isinstance(approval, ApprovalGrant) else str(approval)
                self.approval_service().consume(grant_id, ApprovalScope(
                    action="commit_plan", project_id=self.get_project_id(),
                    timeline_id=plan.composition_id, base_revision=plan.base_generation,
                    plan_hash=plan.content_hash(),
                ))
            except Exception as exc:
                error = str(exc)
                return CompositeApplyResult(False, plan.plan_id, plan.content_hash(), current, current, report=CompositeValidationReport((CompositeDiagnostic("error", error),)), error=error)
        result = service.apply(plan)
        if result.applied:
            page.record_external_composite_edit(result.before or {}, result.after)
        return result

    def get_project_id(self) -> str:
        project = getattr(self._mw, "_current_edit_project", None)
        return str(getattr(project, "project_id", "") or "") if project is not None else ""
