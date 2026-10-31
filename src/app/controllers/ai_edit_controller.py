"""TAEP AI Edit Controller（Phase 9，补齐 §11.8；M1.1 重构）。

把 UI（``AiEditAssistantPanel``）的 signal 映射到 ``AssistantService``；通过
**类型化**的 ``AiEditHostBridge`` 与宿主（MainWindow / 既有 EditController）解耦。

职责（TAEP §11.8）：

- 订阅面板的细粒度 signal（``request_submitted`` / ``variant_selected`` /
  ``apply_requested`` 等），转发到 service。
- 订阅 service 的 ``on_state_changed``，回填面板状态（用 ``_updating`` 锁）。
- 提交成功后通过 bridge 刷新 timeline view / renderer / media pool。
- **不**在这里解析模型输出（那是 service/planner 的职责）。

M1.1 重构要点：

- ``__init__`` 接收 ``bridge: AiEditHostBridge``（类型化协议），取消
  ``host: Any`` + ``hasattr``/``getattr`` 回退。生产宿主与测试 fake 各自实现协议。
- 不再依赖不存在的 ``build_plan_for_variant``；改用 ``bridge.get_plan_by_id``
  从 PlanRepository 取回被批准的那一份不可变 plan（M2.1）。
- apply 走真实授权：``bridge.request_plan_approval`` 返回宿主签发的
  ``ApprovalGrant``（M0.1），传给 ``service.apply_selected``。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.app.controllers.ai_edit_host_bridge import AiEditHostBridge, NullAiEditHostBridge
from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    RunPolicy,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.plugin_persistence import (
    PluginSnapshotJournal,
    PluginSnapshotStore,
)
from src.features.ai_harness.run_trace import RunTraceStore
from src.features.ai_harness.plugin_status import (
    PluginEventJournal,
    PluginStatusProjection,
)
from src.features.ai_harness.plugin_trace import PluginRunTraceAdapter
from src.features.edit.ai_editing.assistant_events import (
    AssistantRequest,
    AssistantState,
    AssistantStateSnapshot,
    ChatAttachment,
)
from src.features.edit.ai_editing.image_attachment import (
    extract_text_from_content,
    message_content,
)
from src.features.edit.ai_editing.assistant_modes import (
    get_media_assistant_modes,
    normalize_assistant_mode,
)
from src.features.edit.ai_editing.assistant_plugins import get_assistant_plugin
from src.features.edit.ai_editing.assistant_service import (
    AssistantConfig,
    AssistantService,
)
from src.features.edit.ai_editing.audit_store import AuditEntry, AuditStore
from src.features.edit.ai_editing.autonomous_recovery import (
    AutonomousCheckpoint,
    AutonomousCheckpointStatus,
    AutonomousCheckpointStore,
)
from src.features.edit.ai_editing.autonomous_mission import (
    AutonomousMissionStore,
    MissionStage,
)
from src.features.edit.ai_editing.goal_supervisor import (
    GoalCycleResult,
    GoalLimits,
    GoalSpec,
    GoalStatus,
    GoalSupervisor,
    InMemoryGoalCheckpointStore,
)
from src.features.edit.ai_editing.goal_round_driver import GoalRoundDriver
from src.features.edit.ai_editing.context_spill import ContextSpillStore
from src.features.edit.ai_editing.tool_operation_ledger import ToolOperationLedger

logger = logging.getLogger(__name__)

# 自由对话（unknown 分支 → LLM chat）的系统提示与温度映射。
# 温度与 LLMVariantPlanner 保持一致（low→0.8 / medium→0.6 / high→0.3）。
_CHAT_SYSTEM_PROMPT = (
    "你是 Topos Color 的 AI 剪辑助手。用户正在使用这款基于 PyQt6 的"
    "视频/图像编辑器。你可以帮助用户解答关于剪辑、调色、特效、工作流的"
    "问题，也可以提供创意建议。当用户的请求涉及具体操作（如生成字幕、"
    "精剪、广告方案）时，可以引导他们使用对应的功能入口。回复保持简洁友好。"
)
_CHAT_TEMPERATURE_MAP = {"low": 0.8, "medium": 0.6, "high": 0.3}

# Motion Graphic 网页生成是"一整个作品"型输出：完整多页 HTML + 内嵌 SVG/插画 +
# 响应式变体 + 动效。这里申请模型的满额输出预算而不是保守值，避免精致设计在
# sanitizer 之前就被截断（截断会丢页、丢闭合标签，宿主再靠猜补页标记）。
# 各供应商对 max_tokens 上限不同（8k/16k/64k/128k…）；超限时 LLM 客户端会识别
# 服务端的 max_tokens 拒绝并自动改用服务端默认值重试一次，因此该值可以放心取大。
_HTML_GENERATE_MAX_TOKENS = 65536
# 网页设计是纯视觉创作：用温度计的满刻度（1.0），不做人为压制。
_HTML_GENERATE_TEMPERATURE = 1.0


class _PlanStateGate:
    """规划 worker 运行期间的线程安全"挂起"计数门闩（AUD-2000）。

    之前 ``_PlanWorker.run()`` 用"摘除/恢复 service.on_state_changed"的方式
    挂起状态回调：两个规划 worker 重叠（旧 worker 的 LLM 调用超过
    ``_start_plan`` 的 2s wait 超时）时，旧 worker ``finally`` 的恢复会与新
    worker 的摘除交错——要么把回调在 worker 线程内复活（``_on_service_state``
    直接操作 QWidget，跨线程 UI 访问），要么新 worker 把恢复成 ``None`` 的
    回调写回（状态回填永久失效）。

    改为引用计数门闩：``on_state_changed`` 回调保持安装，
    ``_on_service_state`` 在挂起期间直接丢弃 worker 线程触发的快照；worker
    完成后由 ``finished_plan``/``failed``/``cancelled`` 信号在主线程经
    ``_refresh_state`` 统一补一次回填。计数语义保证重叠 worker 之间互不干扰。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._depth = 0

    def suspend(self) -> None:
        """进入挂起区间（每个规划 worker 的 run() 调用一次）。"""
        with self._lock:
            self._depth += 1

    def resume(self) -> None:
        """退出挂起区间（与 suspend 配对；计数不为负）。"""
        with self._lock:
            if self._depth > 0:
                self._depth -= 1

    def suspended(self) -> bool:
        """当前是否有规划 worker 处于挂起区间（可在任意线程调用）。"""
        with self._lock:
            return self._depth > 0


class _PlanWorker(QThread):
    """后台线程跑 ``service.plan_variants``（LLM 网络调用，避免阻塞 UI）。

    完成或失败后通过 Qt signal 自动切回主线程（``finished``/``failed``/``cancelled``）。
    在 worker 线程内 service 触发的 ``on_state_changed`` 经共享的
    ``_PlanStateGate`` 挂起（回调保持安装，``_on_service_state`` 检查门闩丢弃），
    由 ``finished`` 信号在主线程统一触发一次状态回填，避免跨线程 UI 访问。

    P1 最后一公里：``cancel_requested`` 是真实属性；``run()`` 在 plan_variants 返回后
    检查它——若已取消则 emit ``cancelled``（丢弃 plan 结果），由 controller 把 run 标
    CANCELLED。不中断进行中的 LLM HTTP 调用（协作取消语义，与 _wrap_plan_with_runtime
    一致）。
    """

    finished_plan = pyqtSignal()
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        service: AssistantService,
        index: Any,
        config: AssistantConfig,
        *,
        state_gate: _PlanStateGate | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._index = index
        self._config = config
        # AUD-2000：与 controller 共享的挂起门闩（缺省时用私有门闩，
        # 兼容旧测试/调用方——不与 controller 的回填联动）。
        self._gate = state_gate if state_gate is not None else _PlanStateGate()
        self._error: str = ""
        self.cancel_requested: bool = False

    def run(self) -> None:
        # AUD-2000：不再摘除 service 的状态回调（见 _PlanStateGate 的说明），
        # 改为挂起门闩：worker 线程内 service 触发的 on_state_changed 由
        # _on_service_state 检查门闩丢弃，由 finished 信号在主线程统一回填。
        self._gate.suspend()
        cancelled = False
        error = ""
        try:
            self._service.plan_variants(self._index, self._config)
            # P1：plan_variants 返回后检查 cancel——已取消则丢弃结果，emit cancelled。
            cancelled = self.cancel_requested
        except Exception as exc:  # noqa: BLE001
            logger.error("LLM 规划失败：%s", exc, exc_info=True)
            self._error = str(exc)
            error = self._error
            # 规划失败也要让 service 进入 FAILED（plan_variants 内部已处理，
            # 但若在 service 之外抛出，这里显式记录）。
            try:
                self._service.record_error(f"规划失败：{exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            # 先恢复门闩再 emit：下面三个信号以排队连接切回主线程，主线程
            # 可能在本线程执行到本 finally 之前就已处理信号并调用
            # _refresh_state；若门闩仍挂起，那次回填会被误丢弃。
            self._gate.resume()
        if cancelled:
            self.cancelled.emit()
        elif error:
            self.failed.emit(error)
        else:
            self.finished_plan.emit()


class _ChatWorker(QThread):
    """后台线程跑 LLM 自由对话（unknown 分支 → DeepSeek 普通文本对话）。

    复用 ``_PlanWorker`` 的协作取消 + 三信号模式，但 ``finished_reply`` 携带
    回复文本。``cancel_requested`` 在 ``complete`` 返回后检查（不中断进行中的
    HTTP 调用，与 ``_PlanWorker`` 一致——DeepSeek 规划请求可能耗时数十秒）。

    与 ``_PlanWorker`` 的差异：不动 ``service`` 状态机，不摘 ``on_state_changed``
    回调（chat 不经 service 的 plan 路径，worker 线程内只调 ``client.complete``）。
    """

    finished_reply = pyqtSignal(str)   # assistant 回复文本（空串表示模型返回空）
    stream_delta = pyqtSignal(str)     # 增量文本；由 controller 回到 UI 线程
    failed = pyqtSignal(str)           # 错误消息（可读原因）
    cancelled = pyqtSignal()

    def __init__(
        self,
        client: Any,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stream: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._messages = messages
        self._temperature = temperature
        self._json_mode = json_mode
        self._max_tokens = max_tokens
        self._stream = stream
        self.cancel_requested: bool = False

    def run(self) -> None:
        try:
            stream_complete = getattr(self._client, "stream_complete", None)
            # Structured scene edits must use the non-streaming JSON contract;
            # the current streaming provider API has no json_mode parameter.
            if callable(stream_complete) and not self._json_mode and self._stream:
                reply = stream_complete(
                    self._messages,
                    on_delta=self.stream_delta.emit,
                    temperature=self._temperature,
                )
            else:
                reply = self._client.complete(
                    self._messages,
                    json_mode=self._json_mode,
                    temperature=self._temperature,
                    # Scene-build JSON can legitimately contain inline SVG,
                    # gradients and many animation tracks.  Keep normal chat
                    # on the provider default, but give structured editor
                    # responses enough room to finish instead of truncating
                    # the JSON halfway through.
                    max_tokens=(8192 if self._json_mode else self._max_tokens),
                )
            if self.cancel_requested:
                self.cancelled.emit()
            else:
                self.finished_reply.emit(reply or "")
        except Exception as exc:  # noqa: BLE001
            logger.error("Chat LLM 调用失败：%s", exc, exc_info=True)
            self.failed.emit(str(exc))


class _MotionGraphicPostprocessWorker(QThread):
    """Run browser-backed Motion Graphic post-processing away from the UI.

    The LLM request already has its own worker, but HTML generation has a
    second expensive phase: sanitizing the document, starting Chromium and
    validating/storyboarding frames.  Keeping that phase in the chat callback
    used to make the main window spin after the model had replied.
    """

    finished_payload = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(
        self,
        request_id: str,
        response: str,
        processor: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._request_id = str(request_id)
        self._response = str(response or "")
        self._processor = processor
        self.cancel_requested = False

    def run(self) -> None:
        try:
            payload = self._processor(self._request_id, self._response)
            if self.cancel_requested:
                self.failed.emit(self._request_id, "已取消网页后台渲染")
            else:
                self.finished_payload.emit(self._request_id, payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("Motion Graphic browser post-processing failed: %s", exc, exc_info=True)
            self.failed.emit(self._request_id, str(exc))


class _SubtitlePlanWorker(QThread):
    """Build a constrained subtitle EditPlan off the Qt/UI thread."""

    finished_plan = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        client: Any,
        request: str,
        state: Any,
        *,
        project_id: str,
        timeline_id: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._request = request
        self._state = state
        self._project_id = project_id
        self._timeline_id = timeline_id
        self.cancel_requested = False

    def run(self) -> None:
        try:
            from src.features.edit.ai_editing.subtitle_ai_planner import SubtitleEditPlanner

            plan = SubtitleEditPlanner(
                self._client,
                model_id=getattr(self._client, "model_id", ""),
            ).plan(
                self._request,
                state=self._state,
                project_id=self._project_id,
                timeline_id=self._timeline_id,
            )
            if self.cancel_requested:
                return
            self.finished_plan.emit(plan)
        except Exception as exc:  # noqa: BLE001
            logger.error("AI subtitle planning failed: %s", exc, exc_info=True)
            self.failed.emit(str(exc))


#: dispose 后仍在运行的 QThread 暂存区（AUD-2004/2008）。
#:
#: 协作取消超时的 worker（HTTP 调用 / Chromium 后处理不可中断）在 dispose
#: 时断开 controller 侧槽位、摘除 Qt parent，由本集合持有到 run() 自然
#: 结束——``finished → deleteLater`` 释放对象并从集合移除。集合的作用仅是
#: 防止 controller 被 GC 时把仍在运行的子 QThread 一并销毁（Qt 的
#: "Destroyed while thread is still running" 崩溃）。
_DETACHED_WORKERS: "set[QThread]" = set()


def _deep_copy_state_for_worker(state: Any, timeline_id: str) -> Any:
    """主线程深拷贝字幕规划所需的最小 ``ProjectSequences``（AUD-2007）。

    bridge 的 ``get_project_sequences`` 只浅拷贝顶层 dict，project 与各
    Sequence/Track 仍是活引用；``SubtitleEditPlanner.plan`` 在 QThread 中经
    ``build_snapshot`` 迭代 ``project.assets`` / ``sequence.all_tracks``（LLM
    调用前后多次读取，历时数秒），与主线程时间线编辑并发会触发
    "dict changed size during iteration" 或读到半更新状态。工程模型均为
    frozen dataclass 纯数据，主线程深拷贝目标 timeline + project 即可让
    worker 只读私有副本（规划只需要这一个 timeline，其余序列不参与）。
    """
    import copy

    from src.features.edit.ai_editing.edit_project_service import ProjectSequences

    sequence = getattr(state, "sequences", {}).get(timeline_id)
    return ProjectSequences(
        project=copy.deepcopy(getattr(state, "project", None)),
        sequences={timeline_id: copy.deepcopy(sequence)},
    )


def _detach_running_worker(worker: QThread) -> None:
    """把仍在运行的 worker 与 controller 生命周期解耦（dispose 兜底）。"""
    _DETACHED_WORKERS.add(worker)
    try:
        worker.setParent(None)
    except (AttributeError, RuntimeError):
        logger.debug("detaching worker from controller failed", exc_info=True)
    try:
        worker.finished.connect(
            lambda w=worker: _DETACHED_WORKERS.discard(w)
        )
    except (RuntimeError, TypeError):
        # 无法挂接清理回调（极端 Qt 状态）：保留引用到进程结束，好过
        # 销毁运行中的线程。
        pass
    if not worker.isRunning():
        # 挂接回调前 run() 已结束：立即回收，避免集合残留。
        _DETACHED_WORKERS.discard(worker)


class AiEditController(QObject):
    """UI signal ↔ AssistantService 桥接（TAEP §11.8）。

    通过 ``AiEditHostBridge`` 与宿主解耦（M1.1）：宿主实现协议方法，controller
    只调用协议方法，不再 ``hasattr`` 探测或穿透私有字段。
    """

    # 向外广播的状态（供 MainWindow/其他面板订阅）
    state_changed = pyqtSignal(object)  # AssistantStateSnapshot
    # P0.3：runtime events 跨线程信号（订阅者回调 emit → Qt 自动切主线程槽）。
    _run_state_changed = pyqtSignal(str)  # run_id
    # AI 自动字幕 worker 的跨线程回调。bridge 只负责启动宿主 worker，所有
    # runtime/UI 更新经这些信号回到 controller 所在线程。
    _auto_caption_progress = pyqtSignal(str, object, str)
    _auto_caption_completed = pyqtSignal(str, object)
    _auto_caption_failed = pyqtSignal(str, str)
    # 生成候选远程句柄物化：worker 回调只 emit，实际导入在 controller 主线程完成。
    _generated_materialization_progress = pyqtSignal(str, object, str)
    _generated_materialization_completed = pyqtSignal(str, str, str, str)
    _generated_materialization_failed = pyqtSignal(str, str)
    # P1.5：run 终态后聚合的 ResultCardState（emit → panel.set_result_card 渲染）。
    result_card_ready = pyqtSignal(object)  # ResultCardState
    # B1：V2 orchestrator 事件跨线程信号（后台线程 emit → 主线程槽刷新 Run Drawer）。
    _v2_event_received = pyqtSignal(object)  # AgentEvent

    def __init__(
        self,
        panel: Any,
        service: AssistantService,
        *,
        bridge: AiEditHostBridge | None = None,
        runtime: "HarnessRuntime | None" = None,
        session_store: Any = None,
        router: Any = None,
        compiler: Any = None,
        adapter: Any = None,
        generation_adapter: Any = None,
        help_assistant: Any = None,
        catalog: Any = None,
        skill_registry: Any = None,
        plugin_registry: Any = None,
        plugin_event_journal: PluginEventJournal | None = None,
        skill_plugin_host: Any = None,
        profile_registry: Any = None,
        credential_store: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._panel = panel
        self._service = service
        # 类型化 bridge；None 时用 Null（确定状态，不掩盖装配缺失）。
        self._bridge: AiEditHostBridge = bridge or NullAiEditHostBridge()
        # P0.2：统一 HarnessRuntime（plan §0.2 "三种入口只有一个 runtime"）。
        # 为 None 时 chat 路径仍可工作（向后兼容旧测试），但 Run Drawer/取消
        # 贯通等 P0 能力需要装配真实 runtime。
        self.runtime: HarnessRuntime | None = runtime
        # P0.4：会话目录（plan §10.2 P0.4）。为 None 时跳过 record_run /
        # add_message_summary（向后兼容）。注入后在 run 终态/请求提交时写摘要。
        self.session_store: Any = session_store
        # Harness-inspired append-only trace.  It observes both the legacy
        # HarnessRuntime stream and the V2 Agent stream, while the UI remains a
        # projection of those facts rather than a second source of truth.
        self.run_trace_store = RunTraceStore()
        self._trace_run_sessions: dict[str, str] = {}
        self._plugin_trace_adapter: PluginRunTraceAdapter | None = None
        if plugin_event_journal is not None:
            self._plugin_trace_adapter = PluginRunTraceAdapter(
                self.run_trace_store,
                session_id_for_run=self._trace_session_for_plugin_run,
            )
            self._plugin_trace_adapter.attach(plugin_event_journal)
        # Side-effecting tool calls are admitted through one bounded ledger so
        # retries/restarts can replay known success but never blindly repeat an
        # operation whose outcome became unknown.
        self._tool_operation_ledger = ToolOperationLedger()
        # P1：Skill 驱动 Agent 依赖（plan §10.3）。任一为 None 时 controller 回退
        # 到旧 submit_request→brief→plan 路径（向后兼容）。全部注入后聊天请求经
        # router 分发到 skill 执行 / 澄清 / 帮助 / 推荐卡四条路径。
        self.router: Any = router
        self.compiler: Any = compiler
        self.adapter: Any = adapter
        self.generation_adapter: Any = generation_adapter
        self.help_assistant: Any = help_assistant
        self.catalog: Any = catalog
        self.skill_registry: Any = skill_registry
        self.plugin_registry: Any = plugin_registry
        self.plugin_event_journal = plugin_event_journal
        self.skill_plugin_host: Any = skill_plugin_host
        self.plugin_snapshot_journal = (
            PluginSnapshotJournal() if plugin_registry is not None else None
        )
        self._profile_registry: Any = profile_registry
        self._credential_store: Any = credential_store
        self._teaching_context_provider: Any = None
        self._teaching_request_handler: Any = None
        self._teaching_motion_graphic_mode_handler: Any = None
        self._teaching_motion_graphic_discussion_handler: Any = None
        self._teaching_scene_edit_request_handler: Any = None
        self._teaching_scene_edit_response_handler: Any = None
        # Freeform HTML generation is a text-chat mode, but its response is
        # consumed by the Teaching Animation module's sanitizer/approval
        # pipeline rather than displayed as ordinary assistant prose.
        self._teaching_html_generate_response_handler: Any = None
        self._teaching_html_generate_request_id: str = ""
        # B 方案：Motion Graphic LLM 从零构建场景。
        # response_handler 由 teaching_animation module 在 attach 时注入，
        # 签名 (request_id, response_text) -> bool；request_id 用于把 LLM 响应
        # 路由回对应的 pending plan。
        self._teaching_motion_graphic_build_response_handler: Any = None
        self._teaching_motion_graphic_build_request_id: str = ""
        self._chat_mode: str = "normal"
        self._teaching_scene_edit_text: str = ""
        # 当前 chat run 的 run_id（plan 终态 / cancel 时用）。
        self._active_chat_run_id: str = ""
        self._auto_caption_run_id: str = ""
        self._materialization_job_ids: dict[str, str] = {}
        # 受限自主执行：plan 仍由 Skill/Runtime 产生，提交由本 controller 之后的
        # AutonomousAgent 完成。两张表按 run_id 关联，避免把其它 run 的 plan
        # 错应用到当前时间线。
        self._autonomous_runs: set[str] = set()
        self._autonomous_plans: dict[str, Any] = {}
        self._autonomous_completed: set[str] = set()
        self._autonomous_inflight: set[str] = set()
        self._autonomous_checkpoints = AutonomousCheckpointStore()
        self._autonomous_missions = AutonomousMissionStore()
        self._autonomous_goals: dict[str, GoalSpec] = {}
        self._autonomous_goal_ids: dict[str, str] = {}
        self._autonomous_goal_checkpoints = InMemoryGoalCheckpointStore()
        self._autonomous_goal_supervisor = GoalSupervisor(
            limits=GoalLimits(max_cycles=2, max_tool_calls=64),
            checkpoints=self._autonomous_goal_checkpoints,
        )
        self._goal_round_driver = GoalRoundDriver(
            event_sink=self._record_goal_round_event,
        )
        self._autonomous_replan_attempts: dict[str, int] = {}
        self._autonomous_replan_guard = False
        self._autonomous_replan_goal_id = ""
        self._autonomous_audit = AuditStore(max_entries=256)
        # P0.3：Composer footer 当前权限（默认 propose；切换后写入后续 invocation）。
        self._current_permission: str = "propose"
        self._current_assistant_mode: str = "chat"
        self._current_reasoning_effort: str = "medium"
        try:
            from src.features.edit.ai_editing.user_ai_config import load_user_ai_config
            saved_effort = load_user_ai_config().get("reasoning_effort")
            if saved_effort in ("low", "medium", "high"):
                self._current_reasoning_effort = saved_effort
        except Exception:
            logger.debug("loading saved reasoning effort failed", exc_info=True)
        # P1.1：澄清往返状态（router 返回 clarify_question 时存 RouteResult，
        # 用户回答后用 prefilled_inputs 重路由；成功路由后清空）。
        self._pending_route: Any = None
        self._pending_route_original_text: str = ""
        # 最近一次用户请求用于错误页“一键重试”；不保存 secret/path，只保留
        # 当前会话内的文本，工程历史仍走 session store 的脱敏摘要策略。
        self._last_request_text: str = ""
        # 后台规划 worker（同一时刻只允许一个；新规划前取消旧的）
        self._plan_worker: _PlanWorker | None = None
        # AUD-2000：规划 worker 运行期间挂起状态回填的门闩（线程安全计数；
        # 详见 _PlanStateGate）。回调保持安装，_on_service_state 检查门闩。
        self._plan_state_gate = _PlanStateGate()
        # 后台自由对话 worker（unknown 分支 → LLM chat；同一时刻只允许一个）
        self._chat_worker: QThread | None = None
        # HTML/Chromium post-processing must not run from the chat callback on
        # the GUI thread.  The controller owns these workers so their queued
        # signals are delivered back to the Qt main thread safely.
        self._motion_graphic_postprocess_workers: dict[
            str, _MotionGraphicPostprocessWorker
        ] = {}
        # Legacy chat fallback activity.  V2 runs publish their own richer
        # event stream; this item keeps the fallback path equally observable.
        self._legacy_chat_activity_item: Any = None
        self._legacy_chat_activity_started_at: float = 0.0
        self._chat_activity_reply_label: str = "模型思考中"
        # V2 unified Agent runtime (plan §A1 / B0); lazy-constructed.  The flag
        # defaults ON (B0), so ordinary chat flows through AgentOrchestrator.
        # When driver construction fails the legacy _ChatWorker is the fallback.
        self._v2_chat_driver: Any = None
        self._v2_orchestrator: Any = None
        self._v2_router: Any = None
        self._v2_inbox: Any = None
        self._v2_active_chat_run_id: str = ""
        self._v2_invariant_engine: Any = None
        self._v2_preset_registry: Any = None
        # Last real usage reported by a V2 run (plan §A4): preferred over the
        # local estimate when refreshing the composer footer.
        self._v2_last_usage: Any = None
        # B1: V2 orchestrator 订阅句柄 + Run Drawer 条目缓存。
        self._v2_unsubscribe: Any = None
        self._v2_drawer_items: list[Any] = []
        self._v2_drawer_history: list[Any] = []
        # B3: Motion Graphic coordinator + request_id → run_id/artifact 映射。
        self._v2_mg_coordinator: Any = None
        self._v2_mg_runs: dict[str, str] = {}
        self._v2_mg_requests: dict[str, Any] = {}
        self._v2_mg_artifacts: dict[str, Any] = {}
        # B7: 媒体生成 runtime_run_id → V2 run_id 映射。
        self._v2_media_runs: dict[str, str] = {}
        # 现有字幕轨道的自然语言编辑 worker。
        self._subtitle_plan_worker: _SubtitlePlanWorker | None = None
        self._streaming_reply_text: str = ""
        # 当前发送上下文的 token 统计（估算；provider 返回 usage 时仍可由上层替换）。
        self._chat_context_tokens = 0
        self._chat_context_compressed = False
        self._active_context_bundle: Any = None
        # 多模态：待发送的图片附件（粘贴/抓帧）。在 _on_request_submitted 设置，
        # _start_chat 消费后清空——避免跨请求残留（AUD-2002：媒体/插件/router
        # 非聊天分支在 return 前显式清空；任何新提交都会整体覆写本暂存区；
        # Motion Graphic / Teaching 分支保留暂存，供模块授权后经
        # start_html_generate_chat 等隔离请求消费设计内的文件交接）。
        self._pending_chat_attachments: tuple[ChatAttachment, ...] = ()
        self._last_chat_compression_signature: tuple[int, int, str] | None = None
        self._context_spill_store = ContextSpillStore()
        # M3：与媒体池共享的分析控制器；由 composition root 在两者创建后注入。
        self._analysis_controller: Any = None
        self._analysis_signal_bindings: list[tuple[Any, Any]] = []
        # request_id -> (原始 RouteResult, 原始 scope)。分析完成后自动继续原任务。
        self._pending_skill_continuations: dict[str, tuple[Any, Any]] = {}

        # Plan observer 是唯一的“模型产出 → 宿主写入”交接点。若 adapter 未装配，
        # 仍保留旧的建议/预览路径，不让装配缺失改变既有行为。
        set_plan_observer = getattr(self.adapter, "set_plan_observer", None)
        if callable(set_plan_observer):
            set_plan_observer(self._on_autonomous_plan_observed)

        # service → controller → panel（状态回填）
        self._service.on_state_changed = self._on_service_state

        # panel → controller → service（signal 路由）
        self._panel.request_submitted.connect(self._on_request_submitted)
        self._panel.variant_selected.connect(self._on_variant_selected)
        self._panel.apply_requested.connect(self._on_apply_requested)
        self._panel.cancel_requested.connect(self._on_cancel)
        # M1.3：用户在 Brief 编辑器点"批准 Brief" → 触发规划（显式门禁）
        self._panel.brief_patch_requested.connect(self._on_brief_approved)
        # 多模态：用户点"抓取当前帧" → 经宿主抓取当前视口画面，回填为待发送图片附件。
        grab_sig = getattr(self._panel, "frame_grab_requested", None)
        if grab_sig is not None:
            grab_sig.connect(self._on_frame_grab_requested)
        # M5：上下文检查器 → 构建 ContextBundle 回填面板（§7.1 可检查上下文）
        self._panel.context_inspector_requested.connect(self._on_inspect_context)
        analyze_context = getattr(self._panel, "analyze_context_requested", None)
        if analyze_context is not None:
            analyze_context.connect(self._on_analyze_context_requested)
        # E13：上下文“排除”→ 重建 bundle 回填；/scope → 检查器；帮助卡转发
        for _sig_name, _slot in (
            ("context_block_excluded", self._on_context_block_excluded),
            ("composer_scope_requested", self._on_composer_scope_requested),
            ("help_navigate_requested", self._on_help_navigate_requested),
            ("help_open_panel_requested", self._on_help_open_panel_requested),
        ):
            _sig = getattr(self._panel, _sig_name, None)
            if _sig is not None:
                _sig.connect(_slot)
        # P0.3：Composer footer + Run Drawer 信号订阅（plan §10.2 P0.3/P0.6）
        composer_permission_changed = getattr(
            self._panel, "composer_permission_changed", None
        )
        if composer_permission_changed is not None:
            composer_permission_changed.connect(self._on_composer_permission_changed)
        if hasattr(self._panel, "composer_profile_changed"):
            self._panel.composer_profile_changed.connect(
                self._on_composer_profile_changed
            )
        if hasattr(self._panel, "composer_mode_changed"):
            self._panel.composer_mode_changed.connect(
                self._on_composer_mode_changed
            )
        if hasattr(self._panel, "composer_reasoning_changed"):
            self._panel.composer_reasoning_changed.connect(
                self._on_composer_reasoning_changed
            )
        run_cancel_requested = getattr(self._panel, "run_cancel_requested", None)
        if run_cancel_requested is not None:
            run_cancel_requested.connect(self._on_run_cancel_requested)
        if hasattr(self._panel, "run_retry_requested"):
            self._panel.run_retry_requested.connect(self._on_run_retry_requested)
        if hasattr(self._panel, "run_diagnostics_requested"):
            self._panel.run_diagnostics_requested.connect(
                self._on_run_diagnostics_requested
            )
        # 这些能力按批次渐进装配；旧版/轻量测试面板没有对应 signal 时，
        # 基础聊天与编辑控制器仍应可用。
        for signal_name, slot in (
            ("new_session_requested", self._on_new_session),
            ("show_history_requested", self._on_show_history),
            ("clear_history_requested", self._on_clear_history),
            ("history_session_requested", self._on_history_session_requested),
            ("conversation_title_change_requested", self._on_conversation_title_changed),
            ("context_compress_requested", self._on_context_compress_signal),
            ("retry_requested", self._on_retry_requested),
            ("draft_changed", self._on_draft_changed),
            ("skill_start_requested", self._on_skill_card_start),
            ("result_action_triggered", self._on_result_action),
            ("show_workflow_requested", self._on_show_workflow_requested),
        ):
            signal = getattr(self._panel, signal_name, None)
            if signal is not None:
                signal.connect(slot)
        set_result_card = getattr(self._panel, "set_result_card", None)
        if callable(set_result_card):
            self.result_card_ready.connect(set_result_card)

        # P0.3：runtime events 订阅（仿 WorkflowRunController 跨线程模式）。
        # 订阅者回调只 emit signal，由 Qt 自动切到主线程槽刷新 footer/drawer。
        self._run_state_changed.connect(self._on_run_state_changed_main)
        self._v2_event_received.connect(self._on_v2_event_main)
        self._auto_caption_progress.connect(self._on_auto_caption_progress)
        self._auto_caption_completed.connect(self._on_auto_caption_completed)
        self._auto_caption_failed.connect(self._on_auto_caption_failed)
        self._generated_materialization_progress.connect(
            self._on_generated_materialization_progress
        )
        self._generated_materialization_completed.connect(
            self._on_generated_materialization_completed
        )
        self._generated_materialization_failed.connect(
            self._on_generated_materialization_failed
        )
        analysis_confirm = getattr(self._panel, "analysis_confirm_requested", None)
        if analysis_confirm is not None:
            analysis_confirm.connect(self._on_analysis_confirm_requested)
        analysis_cancel = getattr(self._panel, "analysis_cancel_requested", None)
        if analysis_cancel is not None:
            analysis_cancel.connect(self._on_analysis_cancel_requested)
        if self.runtime is not None:
            try:
                # AUD-2004：保存退订句柄，dispose 时移除订阅——否则 dispose 后
                # 迟到的 runtime 事件仍会经 _run_state_changed 刷新共享 panel。
                self._runtime_unsubscribe = self.runtime.add_subscriber(
                    self._on_run_event,
                )
            except Exception:  # noqa: BLE001
                logger.debug("runtime subscriber attach failed", exc_info=True)
                self._runtime_unsubscribe = None
        else:
            self._runtime_unsubscribe = None
        self._refresh_plugin_status()

    def dispose(self) -> None:
        """断开面板与 service 的连接，供工程切换时调用。

        ``AiEditController`` 会被 panel 的 Qt signals 持有；只清除 MainWindow 的
        Python 属性并不能释放旧 controller。显式断连避免工程切换后新旧两个会话
        同时处理一次请求，也避免旧会话继续引用已解绑的 ``EditSession``。
        """
        # 先停掉后台 worker（plan + chat + subtitle），避免工程切换后旧 worker
        # 仍在跑触发已解绑的回调。协作取消：不中断进行中的 HTTP 调用，等其
        # 返回后丢弃（AUD-2004：超时后断开槽位信号并清引用，迟到信号被
        # 身份检查丢弃；AUD-2005：worker 经 finished→deleteLater 释放，不再
        # 连同完整消息数组存活到 controller 死亡）。
        for worker in (
            self._plan_worker,
            getattr(self, "_chat_worker", None),
            getattr(self, "_subtitle_plan_worker", None),
        ):
            self._retire_worker(worker)
        # AUD-2008：Motion Graphic 浏览器后处理不可中断（Chromium 无法打断），
        # 逐个 wait(2000) 只会让工程切换串行冻结 2s×N。置 cancel 标志后立即
        # 返回：worker 摘除 parent、由 _DETACHED_WORKERS 持有到自然结束，
        # controller 侧槽位已断开，迟到信号不会发布到共享 panel。
        for worker in tuple(
            getattr(self, "_motion_graphic_postprocess_workers", {}).values()
        ):
            self._retire_worker(worker, wait=False)
        self._motion_graphic_postprocess_workers.clear()
        # AUD-2004：清空 worker 引用，使 _on_chat_cancelled/_on_chat_finished
        # 的身份检查丢弃 dispose 后迟到投递的排队信号（引用未清时旧 worker
        # 的信号仍会向共享 panel 发布旧会话状态）。
        self._plan_worker = None
        self._chat_worker = None
        self._subtitle_plan_worker = None
        # AUD-2004：移除 HarnessRuntime 事件订阅，dispose 后的 run 事件不再
        # 经 _run_state_changed 驱动本 controller 的 footer/drawer 刷新。
        runtime_unsubscribe = getattr(self, "_runtime_unsubscribe", None)
        if callable(runtime_unsubscribe):
            try:
                runtime_unsubscribe()
            except Exception:  # noqa: BLE001
                logger.debug("runtime subscriber removal failed", exc_info=True)
            self._runtime_unsubscribe = None
        # V2：取消统一编排器中的活动 run 并释放 V2 组件引用，避免工程切换后
        # 旧 run/event dict 与 RLock 长期驻留（plan §P0 资源清理）。
        if self._v2_orchestrator is not None:
            # B4: 持久化 orchestrator 状态（暂停未完成 run → 磁盘）。
            self.persist_v2_state()
            try:
                self._v2_orchestrator.request_cancel_for_session("default")
            except Exception:  # noqa: BLE001
                pass
            # B1: 取消 V2 事件订阅，避免 dispose 后旧回调继续触发。
            if self._v2_unsubscribe is not None:
                try:
                    self._v2_unsubscribe()
                except Exception:  # noqa: BLE001
                    pass
                self._v2_unsubscribe = None
            if self._v2_invariant_engine is not None:
                try:
                    self._v2_invariant_engine.uninstall()
                except Exception:  # noqa: BLE001
                    pass
                self._v2_invariant_engine = None
            self._v2_orchestrator = None
            self._v2_chat_driver = None
            self._v2_router = None
            self._v2_last_usage = None
            self._v2_drawer_items = []
            self._v2_drawer_history = []
            self._v2_mg_coordinator = None
            self._v2_mg_runs = {}
            self._v2_mg_requests = {}
            self._v2_mg_artifacts = {}
            self._v2_media_runs = {}
            self._v2_inbox = None
            self._v2_active_chat_run_id = ""
            self._v2_preset_registry = None
        for signal, slot in (
            (self._panel.request_submitted, self._on_request_submitted),
            (self._panel.variant_selected, self._on_variant_selected),
            (self._panel.apply_requested, self._on_apply_requested),
            (self._panel.cancel_requested, self._on_cancel),
            (self._panel.brief_patch_requested, self._on_brief_approved),
            (self._panel.context_inspector_requested, self._on_inspect_context),
            (getattr(self._panel, "analyze_context_requested", None), self._on_analyze_context_requested),
            # E13：此前无人接收的检查器排除/scope/帮助卡信号
            (getattr(self._panel, "context_block_excluded", None), self._on_context_block_excluded),
            (getattr(self._panel, "composer_scope_requested", None), self._on_composer_scope_requested),
            (getattr(self._panel, "help_navigate_requested", None), self._on_help_navigate_requested),
            (getattr(self._panel, "help_open_panel_requested", None), self._on_help_open_panel_requested),
            (getattr(self._panel, "composer_permission_changed", None), self._on_composer_permission_changed),
            (getattr(self._panel, "composer_profile_changed", None), self._on_composer_profile_changed),
            (getattr(self._panel, "composer_mode_changed", None), self._on_composer_mode_changed),
            (getattr(self._panel, "composer_reasoning_changed", None), self._on_composer_reasoning_changed),
            (getattr(self._panel, "run_cancel_requested", None), self._on_run_cancel_requested),
            (getattr(self._panel, "run_retry_requested", None), self._on_run_retry_requested),
            (getattr(self._panel, "run_diagnostics_requested", None), self._on_run_diagnostics_requested),
            (getattr(self._panel, "analysis_confirm_requested", None), self._on_analysis_confirm_requested),
            (getattr(self._panel, "analysis_cancel_requested", None), self._on_analysis_cancel_requested),
            (getattr(self._panel, "new_session_requested", None), self._on_new_session),
            (getattr(self._panel, "show_history_requested", None), self._on_show_history),
            (getattr(self._panel, "clear_history_requested", None), self._on_clear_history),
            (getattr(self._panel, "history_session_requested", None), self._on_history_session_requested),
            (getattr(self._panel, "conversation_title_change_requested", None), self._on_conversation_title_changed),
            (getattr(self._panel, "context_compress_requested", None), self._on_context_compress_signal),
            (getattr(self._panel, "retry_requested", None), self._on_retry_requested),
            (getattr(self._panel, "draft_changed", None), self._on_draft_changed),
            (getattr(self._panel, "skill_start_requested", None), self._on_skill_card_start),
            (getattr(self._panel, "result_action_triggered", None), self._on_result_action),
            (getattr(self._panel, "show_workflow_requested", None), self._on_show_workflow_requested),
        ):
            if signal is None:
                continue
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                # 已断连或 QWidget 正在销毁；这两个情形都是正常 teardown。
                pass
        for signal, slot in (
            (self._generated_materialization_progress, self._on_generated_materialization_progress),
            (self._generated_materialization_completed, self._on_generated_materialization_completed),
            (self._generated_materialization_failed, self._on_generated_materialization_failed),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._service.on_state_changed = None
        self.set_analysis_controller(None)
        self._pending_skill_continuations.clear()
        self._materialization_job_ids.clear()
        self._autonomous_runs.clear()
        self._autonomous_plans.clear()
        self._autonomous_completed.clear()
        self._autonomous_inflight.clear()
        self._autonomous_checkpoints.clear()
        self._autonomous_missions.clear()
        self._autonomous_goals.clear()
        self._autonomous_goal_ids.clear()
        self._autonomous_goal_checkpoints = InMemoryGoalCheckpointStore()
        self._autonomous_goal_supervisor = GoalSupervisor(
            limits=GoalLimits(max_cycles=2, max_tool_calls=64),
            checkpoints=self._autonomous_goal_checkpoints,
        )
        self._goal_round_driver.clear()
        self._tool_operation_ledger.clear()
        self._autonomous_replan_attempts.clear()
        self._autonomous_replan_guard = False
        self._autonomous_replan_goal_id = ""
        self._autonomous_audit.clear()
        self._context_spill_store.clear()
        if self._plugin_trace_adapter is not None:
            try:
                self._plugin_trace_adapter.close()
            except Exception:  # noqa: BLE001
                logger.debug("plugin trace adapter teardown failed", exc_info=True)
            self._plugin_trace_adapter = None

    def _retire_worker(self, worker: Any, *, wait: bool = True) -> None:
        """dispose 期停用一个后台 worker（AUD-2004/2005/2008）。

        协作取消（cancel_requested + quit + 有限 wait）；超时仍在运行时断开
        controller 侧槽位并把 worker 摘到 ``_DETACHED_WORKERS``，run() 自然
        结束后经 ``finished → deleteLater`` 释放。``wait=False`` 用于不可中断
        的 Motion Graphic 浏览器后处理（AUD-2008：不阻塞 UI）。
        """
        if worker is None:
            return
        if not hasattr(worker, "isRunning"):
            return
        if worker.isRunning():
            try:
                worker.cancel_requested = True
            except (AttributeError, RuntimeError):
                pass
            try:
                worker.quit()
            except RuntimeError:
                pass
            if wait:
                worker.wait(2000)
        # 断开 controller 侧槽位（自定义信号全量断连；保留 QThread.finished →
        # deleteLater 的释放链）。dispose 后迟到的排队信号不得再向共享 panel
        # 发布旧会话状态。
        for signal_name in (
            "finished_plan", "finished_reply", "finished_payload",
            "stream_delta", "failed", "cancelled",
        ):
            signal = getattr(worker, signal_name, None)
            if signal is None:
                continue
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                # 该信号本就没有连接：正常 teardown。
                pass
        if worker.isRunning():
            _detach_running_worker(worker)

    def _record_goal_round_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Project continuation admission facts into the durable run trace."""

        try:
            run_id = str(payload.get("run_id") or payload.get("goal_id") or "goal")
            self.run_trace_store.append(
                session_id=self._trace_session_id(),
                run_id=run_id,
                source="assistant",
                kind=kind,
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            logger.debug("record goal round event failed", exc_info=True)

    def set_analysis_controller(self, controller: Any | None) -> None:
        """注入与媒体池共享的分析控制器（组合根在装配后调用）。"""
        for signal, slot in self._analysis_signal_bindings:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._analysis_signal_bindings.clear()
        self._analysis_controller = controller
        if controller is None:
            return
        for signal_name, slot in (
            ("analysis_proposed", self._on_analysis_proposed),
            ("analysis_completed", self._on_analysis_completed),
            ("analysis_failed", self._on_analysis_failed),
        ):
            signal = getattr(controller, signal_name, None)
            if signal is None:
                continue
            try:
                signal.connect(slot)
                self._analysis_signal_bindings.append((signal, slot))
            except (TypeError, RuntimeError):
                logger.debug("analysis signal binding failed: %s", signal_name, exc_info=True)

    # ------------------------------------------------------------------ #
    # service → panel（状态回填，用面板的 _updating 锁防递归）
    # ------------------------------------------------------------------ #

    def _on_service_state(self, snapshot: AssistantStateSnapshot) -> None:
        if self._plan_state_gate.suspended():
            # AUD-2000：规划 worker 线程内触发的状态变化不跨线程回填 UI；
            # worker 结束后 _on_plan_finished/_on_plan_failed/_on_plan_cancelled
            # 会在主线程经 _refresh_state 统一补一次回填。
            return
        self.state_changed.emit(snapshot)
        self._panel.refresh_state(snapshot)
        self._refresh_plugin_status()
        # 对话每次追加/恢复消息都会经过 service 状态回调；在同一处刷新
        # 顶部 token 胶囊，避免只在发送按钮点击时更新而显示滞后。
        self._refresh_composer_footer()
        self._refresh_history_menu()

    # ------------------------------------------------------------------ #
    # panel → service
    # ------------------------------------------------------------------ #

    def start(self, config: AssistantConfig, *, profile_id: str = "",
              profile_connected: bool = False) -> None:
        """启动会话（TAEP §11.6 步骤 1）。"""
        self._service.start_session(
            config, active_profile_id=profile_id, profile_connected=profile_connected,
        )
        self._resume_autonomous_checkpoints()
        self._refresh_composer_footer()

    def _on_frame_grab_requested(self) -> None:
        """用户点"抓取当前帧" → 抓取当前视口画面，加入待发送图片附件。

        在 GUI 线程执行（grabFramebuffer 要求）。抓不到（无可见 viewer）时给个
        友好提示，不报错。
        """
        viewer = None
        try:
            viewer = self._bridge.get_current_gl_viewer()
        except Exception:  # noqa: BLE001
            viewer = None
        if viewer is None:
            show = getattr(self._panel, "set_agent_notice", None)
            if callable(show):
                show("当前页面没有可抓取的画面。请先在 Color/Edit/Composite 页打开素材。", warning=True)
            return
        try:
            qimage = viewer.grabFramebuffer()
        except Exception as exc:  # noqa: BLE001
            logger.debug("frame grab failed", exc_info=True)
            show = getattr(self._panel, "set_agent_notice", None)
            if callable(show):
                show(f"抓取当前画面失败：{exc}", warning=True)
            return
        if qimage is None or qimage.isNull():
            return
        add = getattr(self._panel, "add_image_attachment", None)
        if callable(add):
            add(qimage, source="frame_grab")

    def _on_request_submitted(self, request: AssistantRequest) -> None:
        """用户提交需求 → 意图路由分发（plan §10.3 P1.1/P1.3）。

        P1 改造：若 router 已装配，用户输入先经 ``InvocationRouter.route`` 分发：
        - matched + skill_id 非空 → ``compiler.compile`` + ``adapter.invoke_compiled``
        - matched + clarify_question → ``panel.set_clarification``（问一个最小问题）
        - intent == "help" → ``help_assistant.answer`` + ``panel.set_help_answer``
        - intent == "unknown" → ``catalog.recommend`` + ``panel.set_skill_recommendation``
        - intent == "analyze"（skill_id=""）→ 显示 scope_hint 提示（不执行 skill）

        router 未装配时回退旧 submit_request→brief→plan 路径（向后兼容）。
        P0.4：无论走哪条路径，用户请求都写进会话目录（脱敏摘要）。
        """
        text = str(getattr(request, "text", "") or "").strip()
        attachments = tuple(getattr(request, "attachments", ()) or ())
        target_asset_ids = tuple(
            str(asset_id).strip()
            for asset_id in (getattr(request, "target_asset_ids", ()) or ())
            if str(asset_id).strip()
        )
        if not text and not attachments and not target_asset_ids:
            return
        if attachments and not self._validate_chat_attachments(attachments):
            return
        self._last_request_text = text
        # 多模态：暂存附件供 _start_chat 消费（仅内存，不持久化为字节）。
        self._pending_chat_attachments = attachments
        # P0.4：记录用户消息（脱敏摘要）。
        self._record_user_message(text)

        # The footer mode is an explicit routing choice.  Handle it before the
        # natural-language router so a request such as “做一段风景” cannot be
        # mistaken for ordinary chat or a different skill.
        mode = normalize_assistant_mode(getattr(request, "mode", "chat"))
        self._current_assistant_mode = mode
        if (
            mode == "chat"
            and not attachments
            and not target_asset_ids
            and self._queue_active_v2_followup(text)
        ):
            self._pending_chat_attachments = ()
            return
        if mode == "motion_graphic":
            # Once a Motion Graphic request is pending, every follow-up turn
            # (including the short confirmation "可以") belongs to that
            # feature state machine.  Route it through the Teaching Animation
            # handler first; otherwise the mode entry-point would blindly
            # create a second plan for the confirmation text.
            if callable(self._teaching_request_handler):
                try:
                    if self._invoke_teaching_request_handler(
                        self._teaching_request_handler, text, target_asset_ids,
                    ):
                        self._refresh_composer_footer()
                        return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Motion Graphic follow-up routing failed")
                    show = getattr(self._panel, "set_agent_notice", None)
                    if callable(show):
                        show(f"Motion Graphic 对话处理失败：{exc}", warning=True)
                    return
            handler = self._teaching_motion_graphic_mode_handler
            if callable(handler):
                try:
                    if self._invoke_teaching_request_handler(
                        handler, text, target_asset_ids,
                    ):
                        self._refresh_composer_footer()
                        return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Motion Graphic mode request failed")
                    show = getattr(self._panel, "set_agent_notice", None)
                    if callable(show):
                        show(f"Motion Graphic 生成失败：{exc}", warning=True)
                    return
            show = getattr(self._panel, "set_agent_notice", None)
            if callable(show):
                show("Motion Graphic 生成功能尚未装配。请先启用 Teaching Animation。", warning=True)
            return
        if mode in get_media_assistant_modes():
            # AUD-2002：媒体生成走 consent + provider 流程，不消费聊天附件；
            # 清空暂存，避免旧附件经 feature 发起的隔离 LLM 请求泄漏出去
            # （_pending_chat_attachments 声明的"避免跨请求残留"不变量）。
            if self._pending_chat_attachments:
                logger.debug("media mode ignores chat attachments")
                self._pending_chat_attachments = ()
            from types import SimpleNamespace

            try:
                scope = self._build_chat_scope()
                result = SimpleNamespace(
                    extracted_inputs={"modality": mode},
                )
                self._request_ai_generation(result, scope, text)
            except Exception as exc:  # noqa: BLE001
                logger.exception("direct media mode request failed")
                show = getattr(self._panel, "set_agent_notice", None)
                if callable(show):
                    show(f"{mode} 生成启动失败：{exc}", warning=True)
            return

        # A folder-backed Assistant plugin may expose a generic language mode.
        # Its prompt is user-editable data, while execution still goes through
        # the existing host-owned chat/approval pipeline.  Built-in handlers
        # such as Motion Graphic are handled by their feature route above.
        plugin = get_assistant_plugin(mode)
        if plugin is not None and mode != "chat":
            if plugin.handler != "generic_chat":
                # AUD-2002：非聊天型插件 handler 不消费附件，先清空暂存。
                if self._pending_chat_attachments:
                    logger.debug("assistant plugin %s ignores attachments", mode)
                    self._pending_chat_attachments = ()
            if plugin.handler == "generic_chat":
                system_prompt = plugin.read_prompt("system")
                if not system_prompt:
                    show = getattr(self._panel, "set_agent_notice", None)
                    if callable(show):
                        show(f"Assistant 插件 {mode} 缺少 system prompt。", warning=True)
                    return
                try:
                    self._start_chat(
                        text,
                        chat_mode="normal",
                        system_instruction=system_prompt,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Assistant plugin chat failed: %s", mode)
                    show = getattr(self._panel, "set_agent_notice", None)
                    if callable(show):
                        show(f"Assistant 插件 {mode} 启动失败：{exc}", warning=True)
                return
            show = getattr(self._panel, "set_agent_notice", None)
            if callable(show):
                show(
                    f"Assistant 插件 {mode} 已发现，但处理器 {plugin.handler!r} 尚未装配。",
                    warning=True,
                )
            return

        # Optional Teaching Animation host route.  The feature module owns the
        # scene/template implementation; the shared Assistant controller only
        # forwards an explicit user request and remains feature-agnostic.
        if callable(self._teaching_request_handler):
            try:
                if self._invoke_teaching_request_handler(
                    self._teaching_request_handler, text, target_asset_ids,
                ):
                    return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Teaching Animation AI request failed")
                show = getattr(self._panel, "set_agent_notice", None)
                if callable(show):
                    show(f"Teaching Animation 生成失败：{exc}", warning=True)
                return

        # Motion Graphic creation has a conversational design phase. The
        # feature module consumes only the final explicit authorization; all
        # earlier turns are routed to the configured LLM with a planning-only
        # instruction and cannot write to the host.
        if callable(self._teaching_motion_graphic_discussion_handler):
            try:
                if self._teaching_motion_graphic_discussion_handler(text):
                    if self._can_use_llm_chat():
                        self._start_chat(
                            text,
                            chat_mode="teaching_motion_graphic_discussion",
                            system_instruction=(
                                "你正在和用户讨论一个 Motion Graphic 动画设计。"
                                "当前处于规划讨论阶段，绝对不要创建场景、修改时间线、调用宿主操作，"
                                "也不要要求用户用文字模拟确认按钮。当前项目的画布比例、分辨率和帧率"
                                "已经由系统从当前时间线自动读取，并写在 Teaching Animation 上下文的"
                                "current_timeline 字段中；严禁再次询问用户横屏/竖屏/方形、比例或分辨率，"
                                "也不要把这些信息当成待确认选项。请直接基于 current_timeline 进行设计，"
                                "围绕主题、视觉风格、节奏、文案和镜头结构提出建议并逐步收集偏好。"
                                "如果用户没有指定时长，可以给出合理建议，但不要因画幅信息而停下来。"
                                "只有用户明确说‘可以生成了’、"
                                "‘按这个生成’或等价授权时，系统才会显示真正的确认按钮。"
                            ),
                        )
                    else:
                        show = getattr(self._panel, "set_agent_notice", None)
                        if callable(show):
                            show("Motion Graphic 规划需要先连接可用的 AI Profile。", warning=True)
                    return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Teaching Animation discussion routing failed")
                show = getattr(self._panel, "set_agent_notice", None)
                if callable(show):
                    show(f"Motion Graphic 规划失败：{exc}", warning=True)
                return

        # Complex Teaching Animation edits are planned by the configured LLM
        # as constrained JSON. The feature module still owns scene lookup,
        # diff validation, confirmation and host writes.
        if callable(self._teaching_scene_edit_request_handler):
            try:
                if self._teaching_scene_edit_request_handler(text):
                    if self._can_use_llm_chat():
                        self._start_chat(
                            text,
                            chat_mode="teaching_scene_edit",
                            system_instruction=(
                                "你正在为 Teaching Animation 生成可审阅的场景修改计划。"
                                "请严格只输出一个 JSON 对象，不要 Markdown，不要解释。"
                                "格式：{\"scene_id\":\"真实 scene_id\","
                                "\"summary\":\"简短变更摘要\","
                                "\"commands\":[{\"name\":\"update_element\","
                                "\"arguments\":{\"element_id\":\"真实 element_id\","
                                "\"updates\":{\"content/style/visible/transform 中的字段\":\"新值\"}}}]}。"
                                "只允许 update_element 或 set_animation；必须使用上下文中的真实"
                                "scene_id 和 element_id；不要创建新场景，不要直接修改时间线。"
                            ),
                        )
                    else:
                        show = getattr(self._panel, "set_agent_notice", None)
                        if callable(show):
                            show("复杂场景修改需要先连接可用的 AI Profile。", warning=True)
                    return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Teaching Animation scene edit planning failed")
                show = getattr(self._panel, "set_agent_notice", None)
                if callable(show):
                    show(f"场景修改规划失败：{exc}", warning=True)
                return

        # P1：router 已装配 → 意图路由分发
        if self.router is not None:
            if self._dispatch_via_router(text):
                # AUD-2002：router 分发到此结束。只有 unknown → _start_chat 的
                # 分支消费附件（消费即清空）；export/generate_media/subtitle/
                # skill/analyze/help 等非聊天分支不消费，清空暂存避免旧附件
                # 泄漏进后续 feature 发起的隔离请求。
                if self._pending_chat_attachments:
                    logger.debug("routed request ignored chat attachments")
                    self._pending_chat_attachments = ()
                return

        # 回退：旧 submit_request→brief→plan 路径（router 未装配或分发失败）
        # AUD-2002：brief/plan 路径不支持多模态附件，同样不保留暂存。
        if self._pending_chat_attachments:
            logger.debug("legacy brief path ignores chat attachments")
            self._pending_chat_attachments = ()
        self._run_legacy_brief_path(text)

    def _run_legacy_brief_path(self, text: str) -> None:
        """旧路径：submit_request → propose_brief_patch → preview → set_pending_brief。

        router 未装配 / 分发降级时走此路径（向后兼容；plan §10.3 P1 之前的行为）。
        """
        self._service.submit_request(text)
        brief_id = self._service.active_brief_id or "default"
        patch = self._service.propose_brief_patch(brief_id, text)
        preview_brief = self._service.preview_brief_patch(brief_id, patch)
        self._panel.set_pending_brief(preview_brief)

    def _dispatch_via_router(self, text: str) -> bool:
        """经 InvocationRouter 分发用户输入（plan §10.3 P1.1）。

        P1 批次4：澄清往返——若 ``_pending_route`` 存在（上次问了一个澄清问题），
        本次输入视为澄清答案，带 ``prefilled_inputs`` 重路由原意图。

        Returns:
            True 表示已分发（不再走旧路径）；False 表示分发失败需回退。
        """
        try:
            scope = self._build_chat_scope()
            # 澄清往返：pending_route 存在时，本次输入是澄清答案
            if self._pending_route is not None:
                pending = self._pending_route
                slot = getattr(pending, "pending_slot", "") or ""
                prefilled = dict(getattr(pending, "extracted_inputs", {}) or {})
                if slot:
                    prefilled[slot] = text
                try:
                    result = self.router.route(
                        self._pending_route_text(), scope=scope,
                        prefilled_inputs=prefilled,
                    )
                except Exception:  # noqa: BLE001
                    result = self.router.route(text, scope=scope)
                # 澄清往返后清空 pending（无论是否成功路由）
                self._pending_route = None
            else:
                result = self.router.route(text, scope=scope)
        except Exception:  # noqa: BLE001
            logger.debug("router dispatch failed, falling back to legacy", exc_info=True)
            return False

        intent = getattr(result, "intent", "unknown")
        matched = getattr(result, "matched", False)
        skill_id = getattr(result, "skill_id", "")
        clarify = getattr(result, "clarify_question", "")

        # 1. 需澄清（matched=False 但有 clarify_question）→ 问一个最小问题
        #    router 在缺槽位时 matched=False + clarify_question 非空
        if clarify:
            self._pending_route = result
            # 存原始文本供下次重路由（prefilled_inputs 已含部分槽位）
            self._pending_route_original_text = text
            try:
                self._panel.set_clarification(clarify, getattr(result, "pending_slot", ""))
            except Exception:  # noqa: BLE001
                pass
            return True

        # 2. help 意图 → 帮助回答
        if intent == "help":
            self._show_help_answer(text)
            return True

        # 3. 导出是文件写入动作：宿主负责路径选择、preflight、确认和启动
        # 后台编码；controller 不接触文件系统，也不绕过宿主授权。
        if intent == "export":
            self._request_ai_export(result, scope)
            return True

        # 生成媒体是独立的 consent + GenerationBin 工作流，不走普通 skill compiler。
        if intent == "generate_media":
            self._request_ai_generation(result, scope, text)
            return True

        if intent == "subtitle_quality":
            self._run_subtitle_quality_check(scope)
            return True

        # 现有字幕轨道的编辑：读取完整字幕快照，由当前 Profile 生成受限
        # EditPlan，再复用 preview → approval → CommandStack → undo 闭环。
        if intent == "subtitle_edit":
            self._start_subtitle_edit_agent(text, scope)
            return True

        # 4. matched + 有 skill_id → 执行 skill
        if matched and skill_id:
            return self._execute_skill_route(result, scope)

        # 5. analyze 意图（skill_id=""）→ 显示 scope_hint 提示（不执行 skill）
        if intent == "analyze":
            self._show_analyze_hint(result, text=text, scope=scope)
            return True

        # 6. unknown / script / generate_media（无 skill 执行路径）
        #    已配置 profile → LLM 自由对话（DeepSeek 普通文本回复）
        #    未配置 profile → 推荐卡（保持现状）
        if self._can_use_llm_chat():
            self._start_chat(text)
        else:
            self._show_skill_recommendation()
        return True

    def _run_subtitle_quality_check(self, scope: Any) -> None:
        """Run the same deterministic subtitle QC used by the timeline UI."""
        state = self._bridge.get_project_sequences()
        timeline_id = str(getattr(scope, "timeline_id", "") or "")
        sequence = getattr(state, "sequences", {}).get(timeline_id) if state is not None else None
        if sequence is None:
            self._panel.set_clarification("请先打开一个包含字幕轨道的时间线。", "subtitle_timeline")
            return
        from src.features.edit.core.captions.quality import inspect_subtitle_track

        reports = []
        for track in sequence.all_tracks:
            if getattr(track, "track_type", "") != "subtitle":
                continue
            reports.append(inspect_subtitle_track(
                track, sequence.fps,
                timeline_duration_frames=sequence.duration_frames,
            ))
        if not reports:
            message = "当前时间线没有字幕轨道。"
        else:
            errors = sum(report.error_count for report in reports)
            warnings = sum(report.warning_count for report in reports)
            cues = sum(report.cue_count for report in reports)
            message = f"字幕质检完成：{len(reports)} 条轨道、{cues} 条字幕，{errors} 个错误、{warnings} 个警告。"
            issues = [issue for report in reports for issue in report.issues]
            if issues:
                message += "\n" + "\n".join(
                    f"{issue.severity.upper()} {issue.code} [{issue.cue_id or 'track'}]：{issue.message}"
                    for issue in issues[:20]
                )
        show = getattr(self._panel, "set_agent_notice", None)
        if callable(show):
            show(message, warning=bool(reports and any(
                report.error_count or report.warning_count for report in reports
            )))

    def _start_subtitle_edit_agent(self, text: str, scope: Any) -> None:
        """Plan a natural-language edit for the active subtitle track."""
        import os

        if not self._can_use_llm_chat():
            self._panel.set_clarification(
                "字幕编辑需要先连接 AI Assistant 的模型 Profile。", "subtitle_profile",
            )
            return
        timeline_id = str(getattr(scope, "timeline_id", "") or "")
        project_id = str(getattr(scope, "project_id", "") or "")
        state = self._bridge.get_project_sequences()
        if state is None or not timeline_id:
            self._panel.set_clarification(
                "请先打开一个包含字幕轨道的时间线。", "subtitle_timeline",
            )
            return
        try:
            from src.features.edit.ai_editing.llm_client import make_client

            client = make_client(
                self._profile_registry.active_profile, self._credential_store,
            )
        except Exception as exc:  # noqa: BLE001
            self._panel.set_clarification(f"AI 字幕编辑模型不可用：{exc}", "subtitle_model")
            return

        if self._subtitle_plan_worker is not None and self._subtitle_plan_worker.isRunning():
            self._subtitle_plan_worker.cancel_requested = True
            self._subtitle_plan_worker.quit()
            self._subtitle_plan_worker.wait(2000)

        if os.environ.get("TOPOS_TEST_MODE"):
            try:
                from src.features.edit.ai_editing.subtitle_ai_planner import SubtitleEditPlanner

                plan = SubtitleEditPlanner(client).plan(
                    text, state=state, project_id=project_id, timeline_id=timeline_id,
                )
                self._on_subtitle_plan_ready(plan)
            except Exception as exc:  # noqa: BLE001
                self._on_subtitle_plan_failed(str(exc))
            return

        # AUD-2007：把规划输入固化为深快照（主线程拷贝），worker 线程不再
        # 遍历 live ProjectSequences——与主线程时间线编辑的并发竞态从根上移除。
        # 拷贝失败按确定性失败上报，而不是退回活引用静默恢复竞态。
        try:
            worker_state = _deep_copy_state_for_worker(state, timeline_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("building subtitle plan snapshot failed", exc_info=True)
            self._panel.set_clarification(
                f"无法为 AI 字幕编辑构建时间线快照：{exc}", "subtitle_snapshot",
            )
            return

        worker = _SubtitlePlanWorker(
            client, text, worker_state, project_id=project_id, timeline_id=timeline_id, parent=self,
        )
        worker.finished_plan.connect(self._on_subtitle_plan_ready)
        worker.failed.connect(self._on_subtitle_plan_failed)
        # AUD-2005：字幕规划 worker 持有快照 state 引用，run() 结束后经
        # deleteLater 释放。
        worker.finished.connect(worker.deleteLater)
        self._subtitle_plan_worker = worker
        try:
            self._service._set_state(AssistantState.GATHERING_CONTEXT)  # SLF001
        except Exception:
            pass
        worker.start()

    def _on_subtitle_plan_failed(self, message: str) -> None:
        self._subtitle_plan_worker = None
        try:
            self._service._set_state(AssistantState.IDLE)  # SLF001
        except Exception:
            logger.warning("failed to reset assistant state to idle", exc_info=True)
        self._panel.set_clarification(f"AI 字幕编辑未生成计划：{message}", "subtitle_plan_error")

    def _on_subtitle_plan_ready(self, plan: Any) -> None:
        """Preview and commit a model-produced subtitle plan after approval."""
        self._subtitle_plan_worker = None
        state = self._bridge.get_project_sequences()
        if state is None:
            self._on_subtitle_plan_failed("当前工程已关闭")
            return
        try:
            preview = self._service.preview_plan(plan, state)
        except Exception as exc:  # noqa: BLE001
            self._on_subtitle_plan_failed(f"预览失败：{exc}")
            return
        if preview is None or preview.has_errors:
            diagnostics = "；".join(
                str(getattr(item, "message", item))
                for item in getattr(getattr(preview, "report", None), "diagnostics", ())
            )
            self._on_subtitle_plan_failed(diagnostics or "字幕计划校验未通过")
            return
        grant = self._bridge.request_plan_approval(plan, preview)
        if grant is None:
            try:
                self._service._set_state(AssistantState.IDLE)  # SLF001
            except Exception:
                logger.warning("failed to reset assistant state to idle", exc_info=True)
            return
        approval_svc = self._bridge.approval_service()
        if approval_svc is None:
            self._on_subtitle_plan_failed("授权服务不可用")
            return
        try:
            from src.features.edit.ai_editing.transaction_service import _expected_commit_scope
            # AUD-12909：grant 由 request_plan_approval(plan, preview) 签发、绑定了
            # preview.diff_hash；expected 必须携带同一值（缺省会被严格匹配拒绝）。
            approval_svc.consume(
                grant.grant_id, _expected_commit_scope(plan, diff_hash=preview.diff_hash)
            )
            from src.features.edit.ai_editing.plan_compiler import compile_plan

            compiled = compile_plan(plan)
            result = self._bridge.commit_ai_command(
                plan.target.timeline_id, compiled.macro, undo_label=compiled.undo_label,
            )
        except Exception as exc:  # noqa: BLE001
            self._on_subtitle_plan_failed(f"字幕计划提交失败：{exc}")
            return
        if result is None:
            self._on_subtitle_plan_failed("宿主未能提交字幕计划")
            return
        try:
            self._service._set_state(AssistantState.IDLE)  # SLF001
        except Exception:
            logger.warning("failed to reset assistant state to idle", exc_info=True)
        self._bridge.refresh_after_ai_commit(result)
        show = getattr(self._panel, "set_agent_notice", None)
        if callable(show):
            show(
                f"字幕编辑已完成：{len(plan.operations)} 项操作，已进入撤销栈。",
                warning=False,
            )

    def _request_ai_export(self, result: Any, scope: Any) -> None:
        """把自然语言导出请求交给宿主的安全导出入口。"""
        def _notice(message: str, *, warning: bool = False) -> None:
            show = getattr(self._panel, "set_agent_notice", None)
            if callable(show):
                show(message, warning=warning)
            else:
                logger.info("AI export notice%s: %s", " (warning)" if warning else "", message)

        export = getattr(self._bridge, "request_ai_export", None)
        if not callable(export):
            _notice("当前宿主尚未接入 AI 导出。", warning=True)
            return
        try:
            ok, message = export(
                scope, dict(getattr(result, "extracted_inputs", {}) or {}),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI export request failed")
            ok, message = False, str(exc)
        if ok:
            _notice(message or "导出任务已启动。")
        else:
            _notice(
                message or "导出未启动。", warning=True,
            )

    def _request_ai_generation(self, result: Any, scope: Any, text: str) -> None:
        """生成媒体：宿主确认 consent 后进入 GenerationHarnessAdapter。"""
        def _notice(message: str, *, warning: bool = False) -> None:
            show = getattr(self._panel, "set_agent_notice", None)
            if callable(show):
                show(message, warning=warning)
            else:
                logger.info("AI generation notice%s: %s", " (warning)" if warning else "", message)

        if self.runtime is None or not callable(getattr(self.generation_adapter, "invoke", None)):
            _notice("当前宿主尚未接入 AI 媒体生成。", warning=True)
            return
        request_consent = getattr(self._bridge, "request_ai_generation_consent", None)
        if not callable(request_consent):
            _notice("当前宿主尚未接入 AI 生成确认。", warning=True)
            return
        try:
            from src.features.ai_harness.invocation_models import sanitize_for_storage

            inputs = dict(getattr(result, "extracted_inputs", {}) or {})
            # prompt 仅作为本次 provider 输入；GenerationRequest 的持久化默认只保留 hash。
            inputs["prompt"] = str(sanitize_for_storage(text).strip())
            consent_fields = request_consent(scope, inputs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI generation consent request failed")
            _notice(f"生成确认失败：{exc}", warning=True)
            return
        if not consent_fields:
            _notice("已取消 AI 媒体生成。")
            return
        inputs["consent_fields"] = dict(consent_fields)
        try:
            from src.features.ai_harness.invocation_models import (
                Invocation, InvocationSource, RunPolicy,
            )

            invocation = Invocation(
                source=InvocationSource.CHAT,
                skill_id="topos.generate_media",
                scope=scope,
                policy=RunPolicy(
                    permission=self._current_permission,
                    allow_generation=True,
                ),
                inputs=inputs,
            )
            run_id = self.generation_adapter.invoke(invocation)
            self._active_chat_run_id = run_id
            # B7: 创建 V2 AgentRun 使媒体生成在统一事件流中可见。
            gen_mode = str(inputs.get("mode", "image")) if isinstance(inputs, dict) else "image"
            self._v2_track_media_generation(run_id, mode=gen_mode)
            set_active = getattr(self._service, "set_active_run_id", None)
            if callable(set_active):
                set_active(run_id, runtime=self.runtime)
            self._refresh_run_drawer()
            self._refresh_composer_footer()
            self._record_run_finalization(
                run_id,
                summary="AI 媒体已生成候选，已放入生成素材箱，等待预览/导入。",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI generation invoke failed")
            _notice(f"生成启动失败：{exc}", warning=True)

    def _pending_route_text(self) -> str:
        """取 pending_route 对应的原始用户文本（供澄清往返重路由）。"""
        return getattr(self, "_pending_route_original_text", "") or ""

    def _build_chat_scope(self) -> Any:
        """从 bridge 构造 InvocationScope（P1：scope inputs）。"""
        from src.features.ai_harness.invocation_models import InvocationScope
        project_id = ""
        timeline_id = ""
        selected: tuple[str, ...] = ()
        try:
            project_id = self._bridge.get_project_id() or "default"
        except Exception:  # noqa: BLE001
            project_id = "default"
        try:
            tl = self._bridge.get_active_timeline_id()
            if tl:
                timeline_id = tl
        except Exception:  # noqa: BLE001
            timeline_id = ""
        try:
            selected = self._bridge.get_selected_asset_ids()
        except Exception:  # noqa: BLE001
            selected = ()
        return InvocationScope(
            project_id=project_id or "default",
            timeline_id=timeline_id,
            selected_asset_ids=tuple(selected),
        )

    def _execute_skill_route(
        self,
        result: Any,
        scope: Any,
        *,
        parent_session_id: str = "",
        inputs_override: dict[str, Any] | None = None,
    ) -> bool:
        """matched skill → compile + invoke_compiled（plan §10.3 P1.3 统一路径）。

        Returns:
            True 表示已执行（不论成功失败）；False 表示 compile 前就失败需回退。
        """
        if self.compiler is None or self.adapter is None or self.skill_registry is None:
            return False
        try:
            inv = self.router.to_invocation(
                result, scope=scope,
                policy=self._current_policy(), parent_session_id="",
            )
            if inv is None:
                return False
            # Retry/continuation callers may provide the original invocation
            # context. Preserve its scope and typed inputs instead of reading
            # the user's current selection, which may have changed since the
            # failed run was created.
            if parent_session_id or inputs_override is not None:
                from dataclasses import replace

                inv = replace(
                    inv,
                    parent_session_id=parent_session_id,
                    inputs=dict(inputs_override or inv.inputs),
                )
            # 口播精剪/产品广告需要真实 evidence。若还没分析，Agent 自主提出
            # 分析并在完成后继续原始 invocation；不把“缺证据”伪装成低质量成片。
            if self._needs_analysis_before_skill(result.skill_id, scope):
                return self._propose_skill_analysis(result, scope)
            # 自动字幕是一个真实的长任务：先跑 ASR，再把结果写入字幕轨道。
            # 不能直接交给 editorial builder，因为它只读取已有 transcript evidence，
            # 会在“Analyzed 0”时伪装成完成或静默降级。
            if result.skill_id == "topos.auto_caption" and self.runtime is not None:
                return self._start_auto_caption_agent(inv)
            definition = self._resolve_definition(result.skill_id)
            if definition is None:
                logger.debug("skill definition not found: %s", result.skill_id)
                return False
            compiled = self.compiler.compile(definition, inv)
            run_id = self.adapter.invoke_compiled(compiled, inv)
            self._active_chat_run_id = run_id
            goal_id = (
                self._autonomous_replan_goal_id
                if self._autonomous_replan_guard and self._autonomous_replan_goal_id
                else str(run_id)
            )
            self._autonomous_goal_ids[str(run_id)] = goal_id
            if self._autonomous_replan_guard:
                # The replacement run inherits the one-attempt budget from its
                # failed predecessor; this prevents a stale-plan cascade.
                self._autonomous_replan_attempts[str(run_id)] = 1
            if self._autonomous_mode_enabled():
                self._autonomous_runs.add(run_id)
            # 把 run_id 注入 service（cancel 透传）
            try:
                self._service.set_active_run_id(run_id, runtime=self.runtime)
            except Exception:  # noqa: BLE001
                logger.warning("set_active_run_id failed; run cancel may not propagate", exc_info=True)
            self._refresh_run_drawer()
            self._refresh_composer_footer()
            self._record_run_finalization(
                run_id,
                summary=self._run_summary_for(result.skill_id, compiled),
            )
            # 确定性/本地 provider 可能在 invoke_compiled 返回前已经完成；补一次
            # 同步检查，异步 provider 则由 runtime 状态事件触发同一个幂等入口。
            self._try_autonomous_apply(run_id)
        except Exception:  # noqa: BLE001
            logger.debug("skill execution failed", exc_info=True)
            return False
        return True

    def _needs_analysis_before_skill(self, skill_id: str, scope: Any) -> bool:
        """判断需要证据的剪辑 skill 是否可以进入 planner。"""
        if skill_id not in {
            "topos.voiceover_polish", "topos.product_ad_30s",
        }:
            return False
        controller = self._analysis_controller
        asset_ids = tuple(getattr(scope, "selected_asset_ids", ()) or ())
        if controller is None or not asset_ids:
            return False
        try:
            return any(
                controller.get_asset_status(asset_id) != "ready"
                for asset_id in asset_ids
            )
        except Exception:
            # 无法确认 freshness 时保守地先要求分析，不让 planner 猜素材内容。
            return True

    def _propose_skill_analysis(self, result: Any, scope: Any) -> bool:
        """为剪辑 skill 建立分析 proposal，并登记完成后的 continuation。"""
        controller = self._analysis_controller
        if controller is None:
            return False
        asset_ids = tuple(getattr(scope, "selected_asset_ids", ()) or ())
        from src.features.edit.ai_editing.analysis_request import AnalysisRecipe

        recipe = (
            AnalysisRecipe.VISUAL_SEMANTIC_LOCAL
            if result.skill_id == "topos.product_ad_30s"
            else AnalysisRecipe.EDITORIAL_LOCAL
        )
        try:
            request = controller.request_analysis_draft(
                asset_ids,
                recipe=recipe,
                requested_by="assistant",
                title=f"Agent：先分析素材，再执行 {result.description or result.skill_id}",
            )
            self._pending_skill_continuations[request.request_id] = (result, scope)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("skill analysis proposal failed")
            self._panel.set_clarification(f"无法准备剪辑所需分析：{exc}", "analysis_error")
            return True

    def _start_auto_caption_agent(self, inv: Invocation) -> bool:
        """执行“音频→ASR→字幕轨道”的真实 Agent 工作流。"""
        if self.runtime is None:
            return False
        get_clip = getattr(self._bridge, "get_selected_audio_clip", None)
        start_transcription = getattr(self._bridge, "start_ai_transcription", None)
        if not callable(get_clip):
            return False

        run_id = self.runtime.start(inv)
        self._active_chat_run_id = run_id
        self._auto_caption_run_id = run_id
        self.runtime.set_status(run_id, "validating")
        try:
            clip_scope = get_clip()
        except Exception as exc:  # noqa: BLE001
            clip_scope = None
            logger.debug("read selected audio clip failed", exc_info=True)
            self._fail_auto_caption_run(
                run_id, "AUDIO_SCOPE_FAILED",
                f"{self._auto_caption_tr('scope_failed', '无法读取当前选中的音频片段。')} {exc}",
            )
            return True
        if clip_scope is None:
            self._fail_auto_caption_run(
                run_id, "AUDIO_CLIP_NOT_SELECTED",
                self._auto_caption_tr(
                    "no_audio", "请先在时间线上选中一个音频片段，再请求生成字幕。"
                ),
            )
            return True
        clip_id, track_id, asset_id = clip_scope
        if not callable(start_transcription):
            self._fail_auto_caption_run(
                run_id, "ASR_HOST_UNAVAILABLE",
                self._auto_caption_tr("host_unavailable", "语音转文字宿主尚未初始化。"),
            )
            return True
        if not asset_id:
            self._fail_auto_caption_run(
                run_id, "AUDIO_ASSET_MISSING",
                self._auto_caption_tr("asset_missing", "选中的音频片段没有关联素材。"),
            )
            return True

        self.runtime.mark_running(run_id)
        options = {
            "model_size": "auto",
            "background_music": "keep",
            "language": "auto",
            "timestamps": "auto",
            "speakers": True,
            "max_new_tokens": 8192,
        }
        started, error = start_transcription(
            asset_id,
            options,
            on_progress=lambda fraction, message: self._auto_caption_progress.emit(
                run_id, fraction,
                str(message or self._auto_caption_tr("started", "AI 正在转录音频…")),
            ),
            on_completed=lambda analysis: self._auto_caption_completed.emit(
                run_id, (clip_id, track_id, asset_id, analysis)
            ),
            on_failed=lambda message: self._auto_caption_failed.emit(
                run_id, str(message)
            ),
        )
        if not started:
            self._fail_auto_caption_run(run_id, "ASR_START_FAILED", error)
        self._refresh_run_drawer()
        self._refresh_composer_footer()
        return True

    def _fail_auto_caption_run(
        self, run_id: str, code: str, message: str,
    ) -> None:
        show_message = getattr(self._bridge, "show_ai_transcription_message", None)
        if callable(show_message):
            show_message(
                f"{self._auto_caption_tr('failed_prefix', 'AI 自动字幕失败：')}{message}",
                warning=True,
            )
        self.runtime.record_diagnostic(run_id, {
            "severity": "error", "code": code, "message": message,
        })
        self.runtime.finish(
            run_id, success=False, error_code=code, error_message=message,
        )
        self._refresh_run_drawer()
        self._refresh_composer_footer()
        self._record_run_finalization(run_id, summary=f"自动字幕失败：{message}")

    def _on_auto_caption_progress(
        self, run_id: str, fraction: object, message: str,
    ) -> None:
        if self.runtime is None or self.runtime.get_run(run_id) is None:
            return
        if fraction is not None:
            try:
                self.runtime.record_progress(run_id, float(fraction))
            except (TypeError, ValueError):
                pass
        show_progress = getattr(self._bridge, "show_ai_transcription_progress", None)
        if callable(show_progress):
            show_progress(
                fraction if isinstance(fraction, (int, float)) else None,
                message,
            )
        self._run_state_changed.emit(run_id)

    def _on_auto_caption_completed(self, run_id: str, payload: object) -> None:
        if self.runtime is None or self.runtime.get_run(run_id) is None:
            return
        try:
            clip_id, track_id, asset_id, analysis = payload  # type: ignore[misc]
            commit = getattr(self._bridge, "commit_ai_transcript_subtitles", None)
            if not callable(commit):
                raise RuntimeError("字幕写入宿主尚未初始化")
            self.runtime.record_progress(run_id, 0.92)
            # 写入前进入 WAITING_FOR_USER；确认框显示时 Run Drawer 不会继续伪装成
            # 普通推理，用户能明确知道 Agent 正在等待最后的时间线写入授权。
            self.runtime.mark_waiting_for_user(
                run_id,
                message=self._auto_caption_tr(
                    "waiting_commit", "语音转文字已完成，等待确认写入字幕轨道。"
                ),
            )
            ok, message = commit(clip_id, track_id, asset_id, analysis)
            if not ok:
                self._fail_auto_caption_run(run_id, "SUBTITLE_COMMIT_FAILED", message)
                return
            self.runtime.set_status(run_id, "running")
            self.runtime.record_progress(run_id, 1.0)
            self.runtime.finish(run_id, success=True)
            self._refresh_run_drawer()
            self._refresh_composer_footer()
            self._record_run_finalization(
                run_id,
                summary=self._auto_caption_tr(
                    "completed", "已完成语音转字幕并写入字幕轨道。"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto caption completion failed")
            self._fail_auto_caption_run(run_id, "SUBTITLE_COMMIT_FAILED", str(exc))
        finally:
            if run_id == self._auto_caption_run_id:
                self._auto_caption_run_id = ""

    def _on_auto_caption_failed(self, run_id: str, message: str) -> None:
        if self.runtime is None or self.runtime.get_run(run_id) is None:
            return
        self._fail_auto_caption_run(run_id, "ASR_FAILED", message)
        if run_id == self._auto_caption_run_id:
            self._auto_caption_run_id = ""

    def _on_skill_card_start(self, skill_id: str, form_values: dict) -> None:
        """用户点 Skill 推荐卡 → 触发对应 skill 执行（plan §10.3 P1.1 快捷卡）。

        构造一个带 form_values 的 RouteResult-like 调用 _execute_skill_route。
        """
        if self.router is None or self.compiler is None or self.adapter is None:
            return
        try:
            scope = self._build_chat_scope()
            # 直接构造一个最小 RouteResult 风格对象（matched + skill_id）
            from src.features.ai_harness.invocation_router import RouteResult
            result = RouteResult(
                intent="unknown", matched=True, skill_id=skill_id,
                extracted_inputs=dict(form_values),
            )
            self._execute_skill_route(result, scope)
        except Exception:  # noqa: BLE001
            logger.debug("skill card start failed", exc_info=True)

    def _resolve_definition(self, skill_id: str) -> Any:
        """从 skill_registry 取 manifest → 构造 SkillDefinition（P1.3）。"""
        try:
            from src.features.ai_harness.skill_compiler import definition_from_manifest
            manifest = self.skill_registry.get(skill_id)
            if manifest is None:
                return None
            return definition_from_manifest(manifest, is_official=True)
        except Exception:  # noqa: BLE001
            return None

    def _current_policy(self) -> Any:
        """从 _current_permission 派生 RunPolicy。"""
        from src.features.ai_harness.invocation_models import RunPolicy
        try:
            return RunPolicy(permission=self._current_permission)
        except Exception:  # noqa: BLE001
            return RunPolicy()

    def _show_help_answer(self, text: str) -> None:
        """help 意图 → help_assistant.answer + panel.set_help_answer。"""
        if self.help_assistant is None:
            return
        try:
            answer = self.help_assistant.answer(text)
            self._panel.set_help_answer(answer)
        except Exception:  # noqa: BLE001
            logger.debug("help answer failed", exc_info=True)

    def _show_skill_recommendation(self) -> None:
        """unknown 意图 → catalog.recommend + panel.set_skill_recommendation。"""
        if self.catalog is None:
            return
        try:
            cards = self.catalog.recommend(limit=5)
            self._panel.set_skill_recommendation(cards)
        except Exception:  # noqa: BLE001
            logger.debug("skill recommendation failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # 自由对话（unknown 分支 → LLM chat）
    # ------------------------------------------------------------------ #

    def _can_use_llm_chat(self) -> bool:
        """是否具备 LLM 自由对话能力（profile 已配置 + credential_store 可用）。"""
        if self._profile_registry is None or self._credential_store is None:
            return False
        try:
            if self._profile_registry.active_profile is not None:
                return True
            # A user may configure a capability-specific language model
            # without designating a global default.  Structured HTML/Motion
            # Graphic calls can safely use that explicit route.
            getter = getattr(self._profile_registry, "active_profile_for_capability", None)
            return bool(callable(getter) and getter("language") is not None)
        except Exception:  # noqa: BLE001
            return False

    def _chat_profile_for_mode(self, chat_mode: str) -> Any | None:
        """Select the user-configured model for a chat transport.

        Motion Graphic authoring is a language-generation task.  Respect the
        existing ``language`` capability route when the user configured one;
        do not perform paid/provider fallback here.  Attachments remain
        validated against this exact choice below, so routing cannot silently
        send media to a profile that lacks the corresponding input capability.
        """
        registry = self._profile_registry
        if registry is None:
            return None
        if chat_mode in {
            "teaching_html_generate", "teaching_motion_graphic_build",
        }:
            getter = getattr(registry, "active_profile_for_capability", None)
            if callable(getter):
                try:
                    routed = getter("language")
                except Exception:  # noqa: BLE001 - a bad registry must not crash chat
                    routed = None
                if routed is not None:
                    return routed
        return getattr(registry, "active_profile", None)

    @staticmethod
    def _attachment_input_kind(attachment: object) -> str:
        """把图片/文件附件归一为能力检查使用的输入类型。"""
        if getattr(attachment, "data_url", ""):
            return "image"
        return str(getattr(attachment, "input_kind", "file") or "file").lower()

    def _validate_chat_attachments(
        self, attachments: tuple[ChatAttachment, ...], *, profile: Any | None = None,
    ) -> bool:
        """发送前阻断不支持多模态的模型，并给出可操作的切换提示。"""
        profile = profile or self._chat_profile_for_mode(self._chat_mode)
        if profile is None:
            show = getattr(self._panel, "set_agent_notice", None)
            if callable(show):
                show("当前没有可用的 AI 模型，请先配置并连接模型。", warning=True)
            return False
        from src.features.edit.ai_editing.ai_provider_catalog import (
            profile_multimodal_reason,
            profile_supports_input,
        )
        unsupported = [
            self._attachment_input_kind(attachment)
            for attachment in attachments
            if not profile_supports_input(
                profile, self._attachment_input_kind(attachment),
            )
        ]
        if not unsupported:
            return True
        show = getattr(self._panel, "set_agent_notice", None)
        if callable(show):
            show(profile_multimodal_reason(profile), warning=True)
        return False
    @staticmethod
    def _chat_activity_label(
        chat_mode: str,
        stage: str,
        system_instruction: str = "",
    ) -> str:
        """Return a user-facing phase label without exposing prompt internals."""
        if stage == "prepare":
            return "正在准备 AI 请求"
        if stage == "context":
            return "正在读取并整理项目上下文"
        if stage == "waiting":
            if chat_mode == "teaching_html_generate":
                return "正在连接网页设计模型"
            if chat_mode == "teaching_motion_graphic_build":
                return "正在连接 Motion Graphic 模型"
            if chat_mode == "teaching_scene_edit":
                return "正在连接场景编辑模型"
            return "正在等待 AI 模型响应"
        if stage == "reply":
            if chat_mode == "teaching_html_generate":
                return "正在设计网页与动画"
            if chat_mode == "teaching_motion_graphic_build":
                if any(keyword in str(system_instruction) for keyword in ("创意总监", "制作计划")):
                    return "正在制定 Motion Graphic 创意方案"
                return "正在设计 Motion Graphic 页面"
            if chat_mode == "teaching_scene_edit":
                return "正在分析当前场景"
            return "模型思考中"
        return "正在执行 AI 任务"

    def _start_chat(
        self,
        text: str,
        *,
        chat_mode: str = "normal",
        system_instruction: str = "",
        temperature_override: float | None = None,
    ) -> None:
        """unknown 分支 → LLM 自由对话（多轮，带最近 10 条历史）。

        结构对照 ``_start_plan``：TOPOS_TEST_MODE 同步分支 → 取消旧 ``_chat_worker``
        → 记录 user 消息 → 构造 messages → ``make_client`` → 起 ``_ChatWorker``。
        """
        import os
        if chat_mode == "normal" and not str(system_instruction or "").strip():
            active_plugin = get_assistant_plugin(self._current_assistant_mode)
            if active_plugin is not None and active_plugin.handler == "generic_chat":
                system_instruction = active_plugin.read_prompt("system")
        self._chat_mode = chat_mode
        self._teaching_scene_edit_text = text if chat_mode == "teaching_scene_edit" else ""
        self._chat_activity_reply_label = self._chat_activity_label(
            chat_mode, "reply", system_instruction,
        )

        # 取消旧 chat worker（同一时刻只允许一个；与 _plan_worker 语义一致）
        if self._chat_worker is not None and self._chat_worker.isRunning():
            self._chat_worker.cancel_requested = True
            self._chat_worker.quit()
            self._chat_worker.wait(2000)
            # 旧 worker 可能仍在 HTTP 调用中；后续信号必须被身份检查丢弃，
            # 不能覆盖新请求的状态。
            self._chat_worker = None

        self._begin_legacy_chat_activity(
            self._chat_activity_label(chat_mode, "prepare", system_instruction),
        )

        # Structured artifact prompts are an internal transport contract.  The
        # feature has already recorded the user's natural-language request in
        # the Assistant panel before starting this isolated call; exposing the
        # follow-up HTML/creative-brief prompt as another user bubble leaks
        # implementation details (and makes the UI look as if the user typed
        # JSON/schema instructions).  Keep ordinary chat visible, while still
        # registering attachments for the model's multimodal context.
        isolated_artifact = chat_mode in (
            "teaching_html_generate", "teaching_motion_graphic_build",
        )
        expose_user_message = not isolated_artifact

        # 记录 user 消息（UI 立即显示 + 供下一轮历史拼装）。携带多模态附件。
        attachments = self._pending_chat_attachments
        self._pending_chat_attachments = ()  # 消费即清空，避免跨请求残留
        if expose_user_message:
            try:
                self._service.append_chat_message("user", text, attachments=attachments)
            except Exception:  # noqa: BLE001
                logger.debug("append user chat message failed", exc_info=True)
        # MCP 视觉：把附件存盘并注册，供 topos.mcp.* 工具以 'latest' 引用。
        if attachments:
            self._register_mcp_session_images(attachments)

        # Artifact generation must be deterministic with respect to the
        # current brief, not with respect to whatever template-like answer
        # happened to be in the preceding chat.  Keep ordinary conversation
        # multi-turn, but give structured Motion Graphic/HTML requests a
        # fresh context containing only the dedicated contract and this brief.
        messages = self._build_chat_messages(
            system_instruction=system_instruction,
            include_history=not isolated_artifact,
            draft_text=text if isolated_artifact else "",
            # Structured Motion Graphic/HTML requests deliberately use a
            # fresh context, so their user message is not written to the
            # ordinary chat history first.  Keep dropped files in that same
            # isolated message; otherwise the UI accepts the attachments but
            # the artifact model never receives them.
            draft_attachments=attachments if isolated_artifact else (),
        )
        self._update_legacy_chat_activity(
            self._chat_activity_label(chat_mode, "context", system_instruction),
            detail=f"已准备 {len(messages)} 条消息",
        )

        # 构造 client（make_client 纯构造、无副作用；与 _on_composer_profile_changed
        # 的做法一致：不长期持有 client，避免切 profile 后的脏引用）。结构化
        # Motion Graphic 请求优先采用用户为 ``language`` 显式配置的路由。
        profile = self._chat_profile_for_mode(chat_mode)
        if profile is None:
            self._on_chat_failed("当前没有可用的 AI 模型，请先配置并连接模型。")
            return
        # 多模态附件必须使用这一次实际发起请求的 Profile。不能静默切换到另一
        # 个模型，否则底部显示的模型与实际处理请求的模型会不一致。
        if attachments and not self._validate_chat_attachments(
            attachments, profile=profile,
        ):
            # AUD-2001：早退路径必须终结 Run Drawer 的活动项，否则留下一个
            # 永不结束、也无法有效取消的 "running" 幽灵条目（profile is None
            # 分支经 _on_chat_failed 收尾，本分支此前没有对称处理）。
            # 附件保留在暂存区供隔离 artifact 请求的模块重试消费（模块复用
            # 同一 request_id 重试，重试前用户可切换 language 路由的 Profile）。
            self._pending_chat_attachments = attachments
            self._finish_legacy_chat_activity("已阻止", status="cancelled")
            return
        try:
            from src.features.edit.ai_editing.llm_client import make_client
            client = make_client(profile, self._credential_store)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat client 构造失败：%s", exc)
            self._on_chat_failed(str(exc))
            return

        self._update_legacy_chat_activity(
            self._chat_activity_label(chat_mode, "waiting", system_instruction),
        )

        temperature = (
            float(temperature_override)
            if temperature_override is not None else
            _CHAT_TEMPERATURE_MAP.get(self._current_reasoning_effort, 0.6)
        )

        # 进入 busy 态：复用 GATHERING_CONTEXT（is_busy=True → UI 显示停止按钮 +
        # 锁输入框；顶部状态胶囊显示"收集上下文中…"）。
        try:
            self._service._set_state(AssistantState.GATHERING_CONTEXT)  # SLF001 紧密协作层
        except Exception:  # noqa: BLE001
            pass

        # 测试模式：同步执行（保持现有断言式测试契约，与 _start_plan 一致）
        if os.environ.get("TOPOS_TEST_MODE"):
            try:
                reply = client.complete(
                    messages,
                    json_mode=chat_mode in ("teaching_scene_edit", "teaching_motion_graphic_build"),
                    temperature=temperature,
                    max_tokens=(
                        _HTML_GENERATE_MAX_TOKENS if chat_mode == "teaching_html_generate" else
                        (8192 if chat_mode in ("teaching_scene_edit", "teaching_motion_graphic_build") else None)
                    ),
                )
                self._update_legacy_chat_activity(
                    self._chat_activity_reply_label,
                )
                self._on_chat_finished(reply or "")
            except Exception as exc:  # noqa: BLE001
                self._on_chat_failed(str(exc))
            return

        # V2 path (plan §A1): route ordinary chat through the unified AgentRun
        # when the flag is on. Teaching modes keep the legacy pipeline because
        # they have dedicated response handlers.
        if chat_mode == "normal":
            from src.features.edit.ai_editing.agent_feature_flags import (
                is_agent_runtime_v2_enabled,
            )
            if is_agent_runtime_v2_enabled() and self._start_chat_v2(
                text, messages, temperature=temperature,
            ):
                # V2 owns the visible activity stream from run.created onward.
                # Drop only the fallback item; do not clear the panel because a
                # queued V2 event may already have rendered its first item.
                self._discard_legacy_chat_activity()
                return

        worker = _ChatWorker(
            client,
            messages,
            temperature=temperature,
            json_mode=chat_mode in ("teaching_scene_edit", "teaching_motion_graphic_build"),
            # HTML is a complete document rather than a conversational delta.
            # Keep it non-streaming and request the model's full output budget
            # so a rich webpage is not truncated before the sanitizer sees it.
            # Providers with lower caps are handled by the client-side
            # max_tokens fallback (see _is_max_tokens_rejection).
            max_tokens=_HTML_GENERATE_MAX_TOKENS if chat_mode == "teaching_html_generate" else None,
            stream=chat_mode != "teaching_html_generate",
            parent=self,
        )
        self._streaming_reply_text = ""
        clear_stream = getattr(self._panel, "clear_streaming_reply", None)
        if callable(clear_stream):
            clear_stream()
        worker.stream_delta.connect(
            lambda delta, w=worker: self._on_chat_delta(delta, w)
        )
        worker.finished_reply.connect(
            lambda reply, w=worker: self._on_chat_finished(reply, w)
        )
        worker.failed.connect(
            lambda error, w=worker: self._on_chat_failed(error, w)
        )
        worker.cancelled.connect(
            lambda w=worker: self._on_chat_cancelled(w)
        )
        # AUD-2005：worker 持有完整消息数组（多模态含 base64 图片 data URL，
        # 单附件可达数 MB），run() 结束后必须释放，不能随 controller 存活到
        # 会话结束（对照 Motion Graphic worker 的既有正确做法）。
        worker.finished.connect(worker.deleteLater)
        self._chat_worker = worker
        self._update_legacy_chat_activity(self._chat_activity_reply_label)
        worker.start()

    def _begin_legacy_chat_activity(self, label: str) -> None:
        """Start the Codex-like observable activity for the legacy fallback."""
        try:
            from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

            self._legacy_chat_activity_started_at = time.monotonic()
            self._legacy_chat_activity_item = RunDrawerItem(
                item_id=f"chat:{uuid.uuid4().hex}",
                label=label,
                status="running",
                kind="run",
                can_cancel=True,
            )
            self._publish_legacy_chat_activity()
        except Exception:  # noqa: BLE001
            logger.debug("publish legacy chat activity start failed", exc_info=True)

    def _update_legacy_chat_activity(
        self,
        label: str,
        *,
        detail: str = "",
        progress: float = 0.0,
    ) -> None:
        """Update the current fallback phase without exposing model internals."""
        try:
            item = getattr(self, "_legacy_chat_activity_item", None)
        except RuntimeError:
            # Some controller unit tests intentionally bypass QObject.__init__;
            # PyQt raises here instead of behaving like normal Python getattr.
            item = None
        if item is None:
            return
        item.label = label
        item.status = "running"
        item.progress = max(0.0, min(1.0, float(progress)))
        item.output_summary = detail[:160]
        self._publish_legacy_chat_activity()

    def _finish_legacy_chat_activity(
        self,
        label: str,
        *,
        status: str = "succeeded",
        detail: str = "",
    ) -> None:
        """Publish a terminal fallback activity row, keeping it inspectable."""
        try:
            item = getattr(self, "_legacy_chat_activity_item", None)
        except RuntimeError:
            item = None
        if item is None:
            return
        item.label = label
        item.status = status
        item.can_cancel = False
        started = getattr(self, "_legacy_chat_activity_started_at", 0.0)
        if started:
            item.duration_ms = max(0, int((time.monotonic() - started) * 1000))
        item.output_summary = detail[:160]
        self._publish_legacy_chat_activity()

    def _discard_legacy_chat_activity(self) -> None:
        """Remove only the fallback cache when V2 takes ownership of the run."""
        self._legacy_chat_activity_item = None
        self._legacy_chat_activity_started_at = 0.0

    def _publish_legacy_chat_activity(self) -> None:
        item = getattr(self, "_legacy_chat_activity_item", None)
        if item is None:
            return
        panel = getattr(self, "_panel", None)
        set_status = getattr(panel, "set_activity_status", None)
        if callable(set_status):
            try:
                set_status(
                    str(getattr(item, "label", "AI 任务") or "AI 任务"),
                    detail=str(getattr(item, "output_summary", "") or ""),
                    active=str(getattr(item, "status", "running"))
                    in ("running", "queued", "waiting_input"),
                    status=str(getattr(item, "status", "running") or "running"),
                )
            except Exception:  # noqa: BLE001
                logger.debug("publish fixed activity status failed", exc_info=True)
        try:
            from src.features.edit.ui.ai_assistant_composer import RunDrawerState

            active = int(item.status in ("running", "queued", "waiting_input"))
            done = int(item.status in ("succeeded", "skipped"))
            failed = int(item.status == "failed")
            self._panel.set_run_drawer(RunDrawerState(
                items=[item],
                active_count=active,
                completed_count=done,
                failed_count=failed,
            ))
        except Exception:  # noqa: BLE001
            logger.debug("publish legacy chat activity failed", exc_info=True)

    def _start_chat_v2(
        self,
        text: str,
        messages: list[dict[str, str]],
        *,
        temperature: float,
    ) -> bool:
        """Run ordinary chat through the unified AgentRun (plan §A1).

        Returns ``True`` if the V2 worker was started, ``False`` to fall back
        to the legacy ``_ChatWorker`` (e.g. driver unavailable).  Reuses the
        existing chat callbacks so the UI behaviour stays identical.
        """

        driver = self._get_v2_chat_driver()
        if driver is None:
            return False
        from src.app.controllers.agent_v2_chat_worker import AgentV2ChatWorker

        # B1: 新 run 开始时将上一轮的终态条目归档到历史，清空活动区。
        self._archive_v2_drawer_items()

        worker = AgentV2ChatWorker(
            driver,
            session_id="default",
            user_text=text,
            messages=messages,
            mode="chat",
            temperature=temperature,
            stream=True,
            parent=self,
        )
        self._streaming_reply_text = ""
        clear_stream = getattr(self._panel, "clear_streaming_reply", None)
        if callable(clear_stream):
            clear_stream()
        worker.stream_delta.connect(
            lambda delta, w=worker: self._on_chat_delta(delta, w)
        )
        worker.finished_reply.connect(
            lambda reply, w=worker: self._on_chat_finished(reply, w)
        )
        worker.failed.connect(
            lambda error, w=worker: self._on_chat_failed(error, w)
        )
        worker.cancelled.connect(
            lambda w=worker: self._on_chat_cancelled(w)
        )
        worker.finished_result.connect(self._on_v2_chat_result)
        # AUD-2005：与 legacy chat worker 一致——run() 结束后释放 worker
        # （消息数组含多模态内容，不能随 controller 存活）。
        worker.finished.connect(worker.deleteLater)
        self._chat_worker = worker
        worker.start()
        return True

    def _get_v2_chat_driver(self) -> Any:
        """Lazy-construct the V2 chat driver (cached). ``None`` if unavailable."""

        if self._v2_chat_driver is not None:
            return self._v2_chat_driver
        if self._profile_registry is None or self._credential_store is None:
            return None
        invariant_engine = None
        try:
            from src.features.edit.ai_editing.agent_orchestrator import (
                AgentOrchestrator,
            )
            from src.features.edit.ai_editing.assistant_capability_router import (
                AssistantCapabilityRouter,
            )
            from src.features.edit.ai_editing.agent_inbox import AgentInbox
            from src.features.edit.ai_editing.agent_presets import (
                create_default_agent_preset_registry,
            )
            from src.features.edit.ai_editing.chat_run_driver import ChatRunDriver
            from src.features.edit.ai_editing.llm_client import make_client
            from src.features.ai_harness.runtime_invariants import RuntimeInvariantEngine

            # W2 fix: construct everything in local variables first; only
            # commit to self.* after all steps succeed (avoids leaking a
            # half-constructed orchestrator + subscription on partial failure).
            orchestrator = AgentOrchestrator()
            unsubscribe = orchestrator.subscribe(self._on_v2_orchestrator_event)
            invariant_engine = RuntimeInvariantEngine(orchestrator)
            invariant_engine.install()
            router = AssistantCapabilityRouter(self._profile_registry)
            inbox = AgentInbox()
            preset_registry = create_default_agent_preset_registry()
            # B2: 尝试构造 ToolFacade，使普通对话可走 AgentToolLoop（只读工具）。
            tool_facade = self._try_build_tool_facade()
            driver = ChatRunDriver(
                orchestrator,
                router,
                client_factory=lambda profile: make_client(
                    profile, self._credential_store,
                ),
                profile_registry=self._profile_registry,
                tool_facade=tool_facade,
                inbox=inbox,
                preset_registry=preset_registry,
                operation_ledger=self.__dict__.get("_tool_operation_ledger"),
            )
            # All construction succeeded — commit to self.
            self._v2_orchestrator = orchestrator
            self._v2_unsubscribe = unsubscribe
            self._v2_router = router
            self._v2_chat_driver = driver
            self._v2_inbox = inbox
            self._v2_invariant_engine = invariant_engine
            self._v2_preset_registry = preset_registry
            # B4: 从磁盘恢复上次未完成的 run（PAUSED_FOR_RESUME 状态）。
            self.restore_v2_state()
        except Exception:  # noqa: BLE001
            logger.warning("V2 chat driver 构造失败", exc_info=True)
            # Clean up any partially constructed state.
            if self._v2_unsubscribe is not None:
                try:
                    self._v2_unsubscribe()
                except Exception:  # noqa: BLE001
                    pass
            if invariant_engine is not None:
                try:
                    invariant_engine.uninstall()
                except Exception:  # noqa: BLE001
                    pass
            self._v2_unsubscribe = None
            self._v2_orchestrator = None
            self._v2_router = None
            self._v2_chat_driver = None
            self._v2_inbox = None
            self._v2_active_chat_run_id = ""
            self._v2_invariant_engine = None
            self._v2_preset_registry = None
            return None
        return self._v2_chat_driver

    def _queue_active_v2_followup(self, text: str) -> bool:
        """Route an in-flight ordinary chat message into the Agent Inbox."""

        run_id = str(self._v2_active_chat_run_id or "")
        inbox = self._v2_inbox
        if not run_id or inbox is None:
            return False
        try:
            from src.features.edit.ai_editing.agent_inbox import InboxItemKind

            inbox.enqueue(run_id, InboxItemKind.FOLLOW_UP, text)
            show = getattr(self._panel, "set_agent_notice", None)
            if callable(show):
                show("已加入当前 AI 对话的下一步输入，模型完成当前请求后会继续处理。")
            return True
        except Exception:  # noqa: BLE001 - full routing remains available on failure
            logger.debug("queue active V2 follow-up failed", exc_info=True)
            return False

    def _try_build_tool_facade(self) -> Any:
        """B2: 尝试用当前 EditSession 构造 ToolFacade（只读工具集）。

        EditSession.current() 在工程打开后可用；未绑定时返回 None，
        ChatRunDriver 会降级为纯对话（不使用工具循环）。
        """

        try:
            from src.features.edit.ai_editing.edit_api import EditSession
            from src.features.edit.ai_editing.tool_facade import ToolFacade

            session = EditSession.current()
            return ToolFacade(session)
        except Exception:  # noqa: BLE001
            logger.debug("ToolFacade 构造失败，降级为纯对话", exc_info=True)
            return None

    # ------------------------------------------------------------------ #
    # B3: Motion Graphic 生命周期 → AgentRun + 安全门控
    # ------------------------------------------------------------------ #

    def _get_v2_mg_coordinator(self) -> Any:
        """B3: 懒构造 MotionGraphicRunCoordinator（共享 V2 orchestrator）。"""

        if self._v2_mg_coordinator is not None:
            return self._v2_mg_coordinator
        if self._v2_orchestrator is None:
            return None
        try:
            from src.features.edit.ai_editing.motion_graphic_run import (
                MotionGraphicRunCoordinator,
            )
            self._v2_mg_coordinator = MotionGraphicRunCoordinator(
                self._v2_orchestrator,
            )
        except Exception:  # noqa: BLE001
            logger.debug("MG coordinator 构造失败", exc_info=True)
            self._v2_mg_coordinator = None
        return self._v2_mg_coordinator

    def start_motion_graphic_run(
        self,
        request_id: str,
        *,
        scene_revision: int = 0,
        context_fingerprint: str = "",
    ) -> str | None:
        """B3: 为 HTML 生成创建 AgentRun，使全程在 Run Drawer 可见。

        Returns run_id（成功时），None 表示 V2 不可用（静默降级为 legacy）。
        """
        coordinator = self._get_v2_mg_coordinator()
        if coordinator is None:
            return None
        try:
            import time
            import uuid

            from src.features.edit.ai_editing.motion_graphic_artifacts import (
                MotionGraphicBuildRequest,
            )
            # Multi-phase edits reuse one request_id across chats (planner,
            # step 1..N, retries).  Retire the previous phase's run instead of
            # overwriting it — otherwise the Run Drawer keeps a phantom
            # "running" row forever.
            if self._v2_mg_runs.get(request_id):
                self.complete_motion_graphic_run(request_id, success=True)
            request = MotionGraphicBuildRequest(
                task_id=f"mg-{uuid.uuid4().hex[:8]}",
                request_id=request_id,
                scene_revision=scene_revision,
                context_fingerprint=context_fingerprint,
                created_at=time.time(),
            )
            run_id = coordinator.start_build(request)
            self._v2_mg_runs[request_id] = run_id
            self._v2_mg_requests[request_id] = request
            return run_id
        except Exception:  # noqa: BLE001
            logger.debug("start_motion_graphic_run failed", exc_info=True)
            return None

    def stage_motion_graphic_html(
        self,
        request_id: str,
        *,
        html_hash: str,
        sanitized_html_hash: str = "",
        page_count: int = 1,
        render_evidence: Any = None,
    ) -> None:
        """B3: HTML 生成完成后 stage artifact + 记录渲染验证证据。

        HTML 内容只存 hash 引用（红线 6：不内联聊天日志）。
        """
        # Keep legacy/unit-test controllers that bypass QObject.__init__ and
        # the optional V2 coordinator fully compatible with this no-op path.
        run_id = self.__dict__.get("_v2_mg_runs", {}).get(request_id)
        if not run_id:
            return
        coordinator = self._get_v2_mg_coordinator()
        if coordinator is None:
            return
        try:
            artifact = coordinator.stage_html(
                run_id,
                raw_html_hash=html_hash,
                sanitized_html_hash=sanitized_html_hash or html_hash,
                html_payload_ref=f"hash:{sanitized_html_hash or html_hash}",
                page_count=page_count,
            )
            if render_evidence is not None:
                artifact = coordinator.record_verification(
                    run_id, artifact, render_evidence=render_evidence,
                )
            self._v2_mg_artifacts[request_id] = artifact
        except Exception:  # noqa: BLE001
            logger.debug("stage_motion_graphic_html failed", exc_info=True)

    def check_motion_graphic_write_gate(
        self,
        request_id: str,
        *,
        scene_revision: int | None = None,
    ) -> tuple[bool, str]:
        """B3: 统一写入门控（staged + verified + not stale）。

        Returns ``(writable, reason)``。当 V2 未跟踪此请求时返回 ``(True, "")``
        以保持 legacy 兼容（不阻断现有行为）。
        """
        request = self._v2_mg_requests.get(request_id)
        artifact = self._v2_mg_artifacts.get(request_id)
        if request is None or artifact is None:
            return True, ""
        try:
            from src.features.edit.ai_editing.motion_graphic_artifacts import (
                scene_is_stale,
            )
            if scene_is_stale(
                request, artifact, current_scene_revision=scene_revision,
            ):
                return False, "场景已变更，旧 HTML 方案已过期（stale），请重新生成"
            if not artifact.has_verification_evidence:
                return False, "HTML 尚未通过渲染验证，无法写入"
            return True, ""
        except Exception:  # noqa: BLE001
            logger.debug("check_motion_graphic_write_gate failed", exc_info=True)
            return True, ""

    def complete_motion_graphic_run(
        self, request_id: str, *, success: bool = True,
    ) -> None:
        """B3: 标记 Motion Graphic run 完成/失败。"""

        run_id = self.__dict__.get("_v2_mg_runs", {}).pop(request_id, None)
        self.__dict__.get("_v2_mg_requests", {}).pop(request_id, None)
        self.__dict__.get("_v2_mg_artifacts", {}).pop(request_id, None)
        if not run_id:
            return
        coordinator = self._get_v2_mg_coordinator()
        if coordinator is None:
            return
        try:
            if success:
                # 推进 run 状态到 VERIFYING（可能跳过了中间阶段）。
                run = self._v2_orchestrator.get_run(run_id)
                if run is not None and not self._is_v2_terminal(run.state):
                    if run.state == "gathering_context":
                        self._v2_orchestrator.mark_context_ready(run_id)
                    run = self._v2_orchestrator.get_run(run_id)
                    if run is not None and run.state == "planning":
                        self._v2_orchestrator.mark_plan_ready(run_id)
                coordinator.complete_write(run_id, success=True)
            else:
                coordinator.fail(run_id, reason="Motion Graphic generation failed")
        except Exception:  # noqa: BLE001
            logger.debug("complete_motion_graphic_run failed", exc_info=True)
            try:
                self._v2_orchestrator.fail(run_id, reason="Motion Graphic run failed")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _is_v2_terminal(state: str) -> bool:
        return state in (
            "succeeded", "partial_success", "failed", "cancelled",
        )

    # ------------------------------------------------------------------ #
    # B7: 媒体生成统一事件流 → AgentRun
    # ------------------------------------------------------------------ #

    def _v2_track_media_generation(
        self, runtime_run_id: str, *, mode: str = "image",
    ) -> None:
        """B7: 为媒体生成创建 V2 AgentRun（Run Drawer 统一可见）。

        media generation 已通过旧 runtime 的 Run Drawer 可见；此方法额外
        创建 V2 AgentRun 使其在统一事件流中也有记录（artifact/usage/阶段）。
        """
        if self._v2_orchestrator is None:
            return
        try:
            from src.features.edit.ai_editing.agent_models import (
                AgentTask, AgentTaskKind,
            )
            kind_map = {
                "image": AgentTaskKind.IMAGE,
                "video": AgentTaskKind.VIDEO,
                "music": AgentTaskKind.MUSIC,
                "voice": AgentTaskKind.TEXT_TO_SPEECH,
            }
            kind = kind_map.get(mode, AgentTaskKind.IMAGE)
            task = AgentTask(
                task_id=f"media-{runtime_run_id}",
                session_id="media",  # B7: 独立 session，不与 chat 的 "default" 冲突
                title=f"AI {mode} 生成",
                user_goal="",
                kind=kind,
            )
            v2_run_id = self._v2_orchestrator.start_task(task, mode=mode)
            self._v2_media_runs[runtime_run_id] = v2_run_id
            self._v2_orchestrator.mark_context_ready(v2_run_id)
            self._v2_orchestrator.mark_plan_ready(v2_run_id)
        except Exception:  # noqa: BLE001
            logger.debug("V2 media generation tracking failed", exc_info=True)

    def _v2_stage_media_artifact(
        self, runtime_run_id: str, *, artifact_kind: str = "image",
    ) -> None:
        """B7: 媒体生成完成后 stage artifact（emit artifact.staged）。"""

        v2_run_id = self._v2_media_runs.get(runtime_run_id)
        if not v2_run_id or self._v2_orchestrator is None:
            return
        try:
            import time

            from src.features.edit.ai_editing.agent_models import (
                ArtifactKind, ArtifactRef,
            )
            kind_map = {
                "image": ArtifactKind.IMAGE,
                "video": ArtifactKind.VIDEO,
                "audio": ArtifactKind.AUDIO,
            }
            artifact = ArtifactRef(
                artifact_id=f"media-{runtime_run_id}",
                kind=kind_map.get(artifact_kind, ArtifactKind.IMAGE),
                location=f"runtime:{runtime_run_id}",
                created_at=time.time(),
            )
            self._v2_orchestrator.stage_artifact(v2_run_id, artifact)
        except Exception:  # noqa: BLE001
            logger.debug("V2 media artifact staging failed", exc_info=True)

    def _v2_complete_media_generation(
        self, runtime_run_id: str, *, success: bool, reason: str = "",
    ) -> None:
        """B7: 完成媒体生成的 V2 AgentRun。"""

        v2_run_id = self._v2_media_runs.pop(runtime_run_id, None)
        if not v2_run_id or self._v2_orchestrator is None:
            return
        try:
            if success:
                self._v2_orchestrator.begin_verification(v2_run_id)
                self._v2_orchestrator.finish_verification(v2_run_id, success=True)
            else:
                self._v2_orchestrator.fail(
                    v2_run_id, reason=reason or "媒体生成失败",
                )
        except Exception:  # noqa: BLE001
            logger.debug("V2 media generation completion failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # B4: Checkpoint 持久化 + 恢复
    # ------------------------------------------------------------------ #

    @staticmethod
    def _v2_state_file_path() -> Any:
        """B4: V2 orchestrator 状态持久化文件路径。"""
        from pathlib import Path
        return Path.home() / ".topos" / "ai_assistant" / "v2_state.json"

    def persist_v2_state(self) -> None:
        """B4: 导出 orchestrator 状态到磁盘（工程切换/应用关闭时调用）。

        VERIFYING 状态的 run 转为 PAUSED_FOR_RESUME（可恢复、需重新确认）；
        其他非终态 run 无法暂停（状态机限制），标记为 CANCELLED。
        红线 5：AgentRun 无 grant 字段 → 恢复后绝不自动授权。
        """
        if self._v2_orchestrator is None:
            return
        try:
            for run in self._v2_orchestrator.list_runs():
                if self._is_v2_terminal(run.state) or run.state == "paused_for_resume":
                    continue
                try:
                    if run.state == "verifying":
                        self._v2_orchestrator.pause_for_resume(
                            run.run_id, reason="工程切换/应用关闭",
                        )
                    else:
                        # 早期阶段的 run 无法暂停，安全取消。
                        self._v2_orchestrator.request_cancel(
                            run.run_id, reason="工程切换/应用关闭",
                        )
                except Exception:  # noqa: BLE001
                    pass
            state = self._v2_orchestrator.export_state()
            import json
            import os
            path = self._v2_state_file_path()
            if path is not None:
                from pathlib import Path
                state_path = Path(path)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                # AUD-2015: 临时文件 + os.replace 原子替换。直接 write_text
                # 覆盖唯一状态文件，写入中途崩溃/断电会留下截断 JSON，
                # restore_v2_state 只能全部放弃（可恢复 run 丢失）。
                tmp_path = state_path.with_name(state_path.name + ".tmp")
                tmp_path.write_text(
                    json.dumps(state, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                os.replace(tmp_path, state_path)
        except Exception:  # noqa: BLE001
            logger.debug("persist V2 state failed", exc_info=True)

    def restore_v2_state(self) -> int:
        """B4: 从磁盘恢复 orchestrator 状态（工程打开时调用）。

        恢复的 run 保持 PAUSED_FOR_RESUME，需用户重新确认才能继续
        （红线 5：不恢复旧 approval）。返回恢复的 run 数量。

        恢复后清除 active_run_by_session——paused/cancelled run 不应被
        视为"活动"，新对话必须创建新 run。
        """
        if self._v2_orchestrator is None:
            return 0
        try:
            import json
            from pathlib import Path
            path = Path(self._v2_state_file_path())
            if not path.exists():
                return 0
            data = json.loads(path.read_text(encoding="utf-8"))
            # 红线 5: 不恢复 active session（恢复的 run 不自动获得活动状态）。
            data = dict(data)
            data["active_run_by_session"] = {}
            restored = self._v2_orchestrator.restore_state(data)
            return restored
        except Exception:  # noqa: BLE001
            logger.debug("restore V2 state failed", exc_info=True)
            return 0

    def _on_v2_chat_result(self, result: Any) -> None:
        """Bridge V2 run usage into the composer footer (plan §A4)."""

        if result is None:
            return
        usage = getattr(result, "usage", None)
        if usage is None:
            return
        self._v2_last_usage = usage
        try:
            self._refresh_composer_footer()
        except Exception:  # noqa: BLE001
            logger.debug("refresh composer footer after V2 usage failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # B1: V2 orchestrator → Run Drawer 可观察性
    # ------------------------------------------------------------------ #

    def _on_v2_orchestrator_event(self, event: Any) -> None:
        """V2 orchestrator 订阅回调（后台线程）。

        只 emit Qt 信号——绝不在此方法中操作 QWidget。信号通过 queued
        connection 自动切到主线程，保证线程安全（plan §7.3 Qt 线程安全）。
        """
        try:
            run_id = str(getattr(event, "run_id", "") or "")
            kind = str(getattr(event, "kind", "") or "")
            # Media generation uses an intentionally separate V2 session; all
            # ordinary chat events follow the active assistant session.
            session_id = (
                "media"
                if run_id in set(getattr(self, "_v2_media_runs", {}).values())
                else self._trace_session_for_v2_run(run_id)
            )
            if kind == "run.created" and session_id != "media":
                self._v2_active_chat_run_id = run_id
            elif kind in {"run.completed", "run.failed", "run.cancelled"}:
                if run_id == self._v2_active_chat_run_id:
                    self._v2_active_chat_run_id = ""
        except Exception:  # noqa: BLE001
            session_id = "default"
        try:
            self.run_trace_store.append_agent_event(event, session_id=session_id)
        except Exception:  # noqa: BLE001
            logger.debug("append V2 event to run trace failed", exc_info=True)
        try:
            self._v2_event_received.emit(event)
        except Exception:  # noqa: BLE001
            logger.debug("V2 event relay to main thread failed", exc_info=True)

    def _trace_session_id(self) -> str:
        """Resolve the durable assistant session without mutating the store."""

        store = self.session_store
        catalog = getattr(store, "catalog", None) if store is not None else None
        active = str(getattr(catalog, "active_session_id", "") or "")
        return active or "default"

    def _trace_session_for_v2_run(self, run_id: str) -> str:
        """Pin a V2 run to its first observed durable session.

        ``AgentEvent`` has no session field; the orchestrator task is the
        stable owner. The first event maps its logical ``default`` session to
        the current catalog entry, and late events keep that mapping after the
        user switches conversations.
        """

        if run_id in self._trace_run_sessions:
            return self._trace_run_sessions[run_id]
        logical_session = ""
        orchestrator = self._v2_orchestrator
        if orchestrator is not None and run_id:
            try:
                run = orchestrator.get_run(run_id)
                task = (
                    orchestrator.get_task(getattr(run, "task_id", ""))
                    if run is not None else None
                )
                logical_session = str(getattr(task, "session_id", "") or "")
            except Exception:  # noqa: BLE001
                logical_session = ""
        session_id = "media" if logical_session == "media" else self._trace_session_id()
        if run_id:
            self._trace_run_sessions[run_id] = session_id
        return session_id

    def _trace_session_for_plugin_run(self, run_id: str) -> str:
        """Pin plugin invocation facts to the same session as their run."""

        if run_id in self._trace_run_sessions:
            return self._trace_run_sessions[run_id]
        session_id = self._trace_session_id()
        if run_id:
            self._trace_run_sessions[run_id] = session_id
        return session_id

    def export_run_trace(self) -> dict[str, Any]:
        """Export the bounded, sanitized event trace for project persistence."""

        return self.run_trace_store.export_snapshot()

    def restore_run_trace(self, snapshot: dict[str, Any] | None) -> Any:
        """Restore trace history; malformed rows are skipped individually."""

        if not snapshot:
            return None
        return self.run_trace_store.restore_snapshot(snapshot)

    def plugin_status_views(self) -> tuple[Any, ...]:
        """Return UI-safe plugin facts; widgets never read registry internals."""

        registry = self.plugin_registry
        if registry is None:
            return ()
        try:
            return PluginStatusProjection.project(
                registry,
                event_journal=self.plugin_event_journal,
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break chat
            logger.debug("plugin status projection failed", exc_info=True)
            return ()

    def _refresh_plugin_status(self) -> None:
        setter = getattr(self._panel, "set_plugin_status", None)
        if callable(setter):
            setter(self.plugin_status_views())

    def export_plugin_state(self) -> dict[str, Any] | None:
        """Persist plugin facts and bounded checkpoints into the project payload."""

        registry = self.plugin_registry
        if registry is None:
            return None
        try:
            snapshot = PluginSnapshotStore.export(registry)
            journal = self.plugin_snapshot_journal
            if journal is None:
                journal = PluginSnapshotJournal()
                self.plugin_snapshot_journal = journal
            checkpoint = journal.checkpoint(snapshot)
            return {
                "schema_version": "topos-plugin-state/1.0",
                "snapshot": snapshot,
                "journal": journal.to_dict(),
                "current_checkpoint_id": checkpoint.checkpoint_id,
            }
        except Exception:  # noqa: BLE001 - project save must remain available
            logger.debug("plugin state export failed", exc_info=True)
            return None

    def restore_plugin_state(self, payload: Any) -> Any:
        """Restore project lifecycle facts and journal; never activate plugins."""

        if not isinstance(payload, dict):
            return None
        journal_data = payload.get("journal")
        restore_report: Any = None
        if isinstance(journal_data, dict):
            journal, restore_report = PluginSnapshotJournal.from_dict(journal_data)
            self.plugin_snapshot_journal = journal
        project_restore_report: Any = None
        snapshot = payload.get("snapshot")
        registry = self.plugin_registry
        if isinstance(snapshot, dict) and registry is not None:
            state_restorer = None
            skill_host = self.skill_plugin_host
            skill_prefix = "composition.skill."
            if skill_host is not None:
                skill_registry = getattr(skill_host, "plugin_registry", None)

                def _restore_state(plugin_id: str, persisted_state: str) -> bool:
                    if (
                        str(plugin_id).startswith(skill_prefix)
                        and skill_registry is not None
                        and skill_registry.get(plugin_id) is not None
                    ):
                        return bool(skill_host.restore_project_state(
                            str(plugin_id)[len(skill_prefix):], persisted_state,
                        ))
                    return registry.restore_project_state(plugin_id, persisted_state)

                state_restorer = _restore_state
            project_restore_report = PluginSnapshotStore.restore_project_state(
                snapshot,
                registry,
                state_restorer=state_restorer,
            )
        # The live registry is composed from current, validated manifests during
        # startup.  Persisted implementations are never imported or replaced;
        # an old active state is only downgraded to enabled/awaiting activation.
        self._plugin_restore_report = restore_report
        self._plugin_project_restore_report = project_restore_report
        self._refresh_plugin_status()
        return project_restore_report or restore_report

    def export_tool_operation_ledger(self) -> dict[str, Any] | None:
        """Export bounded tool fingerprints/results for project persistence."""

        return self._tool_operation_ledger.export_all()

    def restore_tool_operation_ledger(self, snapshot: Any) -> int:
        """Restore the ledger; interrupted operations become reconciliation-required."""

        self._tool_operation_ledger.clear()
        return self._tool_operation_ledger.import_all(snapshot)

    def export_context_spills(self) -> dict[str, Any] | None:
        """Export metadata-only references for context omitted during compression."""

        return self._context_spill_store.export_all()

    def restore_context_spills(self, snapshot: Any) -> int:
        """Restore spill references without restoring raw prompt content."""

        self._context_spill_store.clear()
        return self._context_spill_store.import_all(snapshot)

    def _on_v2_event_main(self, event: Any) -> None:
        """主线程槽：将 V2 ``AgentEvent`` 翻译为 Run Drawer 更新。"""
        try:
            from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

            run_id = getattr(event, "run_id", "")
            kind = getattr(event, "kind", "")
            payload = getattr(event, "payload", {}) or {}

            # 用 active_run_id 过滤陈旧回调（plan B1 退出条件第 5 条）：
            # 若 orchestrator 认为该 session 已无活动 run，或活动 run 不是
            # 当前事件来源的 run，直接丢弃（取消后迟到的结果不覆盖）。
            # 媒体 run（session "media"）不参与此过滤——它们有独立 session。
            media_run_ids = set(self._v2_media_runs.values())
            if run_id not in media_run_ids and self._v2_orchestrator is not None:
                active_id: str | None = None
                try:
                    active_id = self._v2_orchestrator.active_run_id("default")
                except Exception:  # noqa: BLE001
                    active_id = None
                if active_id is not None and run_id != active_id:
                    return

            self._update_v2_drawer_from_event(run_id, kind, payload, RunDrawerItem)
            self._refresh_v2_run_drawer()
            if kind in ("run.completed", "run.failed", "run.cancelled"):
                self._finish_v2_run_observations(run_id)
        except Exception:  # noqa: BLE001
            logger.debug("V2 event drawer update failed", exc_info=True)

    def _finish_v2_run_observations(self, run_id: str) -> None:
        """Terminal-state housekeeping: inbox residue + invariant violations.

        AgentInbox.clear_run previously had no caller, so a finished run's
        unclaimed inputs lived in the queue forever; invariant violations
        were collected by RuntimeInvariantEngine but never surfaced.
        """
        inbox = getattr(self, "_v2_inbox", None)
        if inbox is not None:
            try:
                inbox.clear_run(str(run_id))
            except Exception:  # noqa: BLE001
                logger.debug("clear_run on terminal V2 event failed", exc_info=True)
        engine = getattr(self, "_v2_invariant_engine", None)
        if engine is None:
            return
        try:
            violations = [
                violation for violation in engine.violations
                if not str(run_id) or violation.run_id == str(run_id)
            ]
            if violations:
                show = getattr(self._panel, "set_agent_notice", None)
                if callable(show):
                    show(
                        "AI 运行时诊断："
                        + "；".join(
                            f"{v.code}（{v.message}）" for v in violations[:3]
                        ),
                        warning=True,
                    )
                for violation in violations:
                    logger.warning(
                        "V2 runtime invariant violated: %s run=%s: %s",
                        violation.code, violation.run_id, violation.message,
                    )
                engine.clear()
        except Exception:  # noqa: BLE001
            logger.debug("surface invariant violations failed", exc_info=True)

    #: AgentRunState → drawer status 字符串映射（AgentRunState 使用小写常量）。
    _V2_STATE_TO_DRAWER: dict[str, str] = {
        "created": "queued",
        "gathering_context": "running",
        "planning": "running",
        "executing": "running",
        "waiting_user_input": "waiting_input",
        "waiting_approval": "waiting_input",
        "verifying": "running",
        "succeeded": "succeeded",
        "partial_success": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
        "paused_for_resume": "waiting_input",
    }

    def _update_v2_drawer_from_event(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        RunDrawerItem: type,
    ) -> None:
        """根据事件类型更新 ``_v2_drawer_items`` 缓存。"""

        if kind == "run.created":
            # 新 run：创建主条目，保留旧条目（由 _archive 在下一轮归档）。
            self._v2_drawer_items.append(RunDrawerItem(
                item_id=run_id,
                label=self._tr_safe("ai_assistant.activity_chat", "AI 对话"),
                status="running",
                kind="run", can_cancel=True,
            ))
        elif kind in ("run.tool_started", "run.tool_finished", "run.tool_progress"):
            self._update_v2_tool_item(run_id, kind, payload, RunDrawerItem)
        elif kind == "run.usage_recorded":
            self._update_v2_usage_item(run_id, payload)
        elif kind == "artifact.staged":
            self._add_v2_artifact_item(run_id, payload, RunDrawerItem)
        else:
            # 其余 run.* 事件（context/plan/verification/completed/failed/...）
            # 从 orchestrator 查询 run 当前状态，用 presenter 生成阶段文案。
            self._sync_v2_main_item(run_id, RunDrawerItem)

    def _sync_v2_main_item(
        self, run_id: str, RunDrawerItem: type,
    ) -> None:
        """从 orchestrator 查询 run 状态，刷新主条目的 label/status。"""

        if self._v2_orchestrator is None:
            return
        try:
            from src.features.edit.ai_editing.agent_phase_presenter import (
                present_state,
            )
        except ImportError:
            return
        try:
            run = self._v2_orchestrator.get_run(run_id)
        except Exception:  # noqa: BLE001
            run = None
        if run is None:
            return
        state_str = getattr(run, "state", "")
        presentation = present_state(state_str)
        drawer_status = self._V2_STATE_TO_DRAWER.get(state_str, "running")
        # Some lightweight drawer tests construct the QObject without running
        # ``__init__``.  Trace enrichment is optional in that mode; the drawer
        # must still reflect the orchestrator state.
        trace_store = self.__dict__.get("run_trace_store")
        projection = None
        if trace_store is not None:
            projection = trace_store.project(
                session_id=self._trace_session_for_v2_run(run_id),
                run_id=run_id,
            )
        for item in self._v2_drawer_items:
            if item.item_id == run_id and item.kind == "run":
                item.label = presentation.label
                item.status = drawer_status
                item.can_cancel = drawer_status in ("running", "queued", "waiting_input")
                item.can_retry = drawer_status == "failed"
                if drawer_status in ("succeeded", "failed", "cancelled"):
                    started_at = float(getattr(run, "started_at", 0.0) or 0.0)
                    ended_at = float(getattr(run, "ended_at", 0.0) or 0.0)
                    if ended_at > started_at > 0:
                        item.duration_ms = max(
                            0, int((ended_at - started_at) * 1000),
                        )
                # 终态条目携带 state_reason 作为 error_code（如有）。
                if drawer_status == "failed":
                    diagnostic = projection.last_diagnostic if projection is not None else {}
                    item.error_code = (
                        getattr(run, "state_reason", "")
                        or str(diagnostic.get("error_code") or diagnostic.get("code") or "")
                    )
                    item.can_view_diagnostics = bool(item.error_code or diagnostic)
                    if diagnostic.get("message"):
                        item.output_summary = str(diagnostic["message"])[:160]
                return

    def _update_v2_tool_item(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        RunDrawerItem: type,
    ) -> None:
        """工具调用事件 → 工具卡片条目。"""

        tool_name = str(payload.get("tool_name", "工具"))
        # step_id/call_id 区分同一个工具的重复调用；旧事件没有 id 时仍
        # 回退到 tool_name，保持旧版 runtime/测试兼容。
        call_key = str(
            payload.get("step_id") or payload.get("call_id") or tool_name
        )
        item_id = f"{run_id}:tool:{call_key}"
        if kind == "run.tool_started":
            # 避免重复追加（同一工具多次 started）。
            if not any(i.item_id == item_id for i in self._v2_drawer_items):
                self._v2_drawer_items.append(RunDrawerItem(
                    item_id=item_id,
                    label=(
                        str(payload.get("description") or "").strip()
                        or f"{self._v2_tool_activity_prefix(tool_name)} · {tool_name}"
                    ),
                    status="running", kind="node", parent_id=run_id, depth=1,
                ))
        elif kind == "run.tool_finished":
            outcome = str(payload.get("outcome", "success"))
            duration_ms = payload.get("duration_ms")
            for item in self._v2_drawer_items:
                if item.item_id == item_id or (
                    not payload.get("step_id")
                    and not payload.get("call_id")
                    and item.item_id == f"{run_id}:tool:{tool_name}"
                ):
                    item.status = "succeeded" if outcome == "success" else "failed"
                    if duration_ms is not None:
                        item.duration_ms = int(duration_ms)
                    if item.status == "failed":
                        item.error_code = str(payload.get("error_code") or "TOOL_FAILED")
                        item.can_view_diagnostics = True
                        item.output_summary = str(
                            payload.get("error_message") or item.error_code
                        )[:160]
                    break
        elif kind == "run.tool_progress":
            # 进度事件既可能描述具体工具，也可能描述 replan/verification
            # 等运行阶段。没有 fraction 时用阶段文案，也不伪造精确百分比。
            phase = str(payload.get("phase") or "").strip()
            fraction = payload.get("fraction", payload.get("progress"))
            detail = str(
                payload.get("message")
                or payload.get("detail")
                or payload.get("reason")
                or ""
            ).strip()
            matching = next(
                (
                    item for item in self._v2_drawer_items
                    if item.item_id == item_id and item.kind == "node"
                ),
                None,
            )
            if matching is not None:
                if fraction is not None:
                    try:
                        matching.progress = max(0.0, min(1.0, float(fraction)))
                    except (TypeError, ValueError):
                        pass
                matching.output_summary = detail[:160]
                return
            for item in self._v2_drawer_items:
                if item.item_id != run_id or item.kind != "run":
                    continue
                if phase == "replan":
                    item.label = self._tr_safe(
                        "ai_assistant.activity_replan", "重新规划",
                    )
                elif phase == "verification_llm":
                    item.label = self._tr_safe(
                        "ai_assistant.activity_verify", "校验结果",
                    )
                elif phase:
                    item.label = phase
                if detail:
                    item.output_summary = detail[:160]
                if phase == "verification_llm":
                    item.progress = max(item.progress, 0.9)
                elif phase == "replan":
                    item.progress = max(item.progress, 0.5)
                break

    def _v2_tool_activity_prefix(self, tool_name: str) -> str:
        """Turn an internal tool name into a short user-facing activity verb."""
        normalized = tool_name.lower()
        if any(word in normalized for word in (
            "search", "find", "list", "snapshot", "read", "inspect", "explore",
        )):
            return self._tr_safe("ai_assistant.activity_explore", "探索")
        if any(word in normalized for word in (
            "run", "exec", "command", "write", "apply", "render", "edit",
        )):
            return self._tr_safe("ai_assistant.activity_execute", "执行")
        return self._tr_safe("ai_assistant.activity_call", "调用")

    def _update_v2_usage_item(self, run_id: str, payload: dict[str, Any]) -> None:
        """usage_recorded 事件 → 在主条目 output_summary 显示 token 数。"""

        prompt = payload.get("actual_prompt") or payload.get("window_prompt") or 0
        completion = payload.get("actual_completion") or 0
        total = (prompt or 0) + (completion or 0)
        if not total:
            return
        for item in self._v2_drawer_items:
            if item.item_id == run_id and item.kind == "run":
                item.output_summary = f"{total} token"
                return

    def _add_v2_artifact_item(
        self, run_id: str, payload: dict[str, Any], RunDrawerItem: type,
    ) -> None:
        """artifact.staged 事件 → artifact 卡片条目。"""

        artifact_kind = str(payload.get("kind", "artifact"))
        item_id = f"{run_id}:artifact:{artifact_kind}"
        if any(i.item_id == item_id for i in self._v2_drawer_items):
            return
        self._v2_drawer_items.append(RunDrawerItem(
            item_id=item_id, label=f"产出：{artifact_kind}",
            status="succeeded", kind="artifact", parent_id=run_id, depth=1,
        ))

    def _archive_v2_drawer_items(self) -> None:
        """新 run 开始前将当前条目归档到历史（保留可回溯）。"""

        if self._v2_drawer_items:
            self._v2_drawer_history = self._v2_drawer_items[-10:] + self._v2_drawer_history
            self._v2_drawer_history = self._v2_drawer_history[:20]
            self._v2_drawer_items = []

    def _refresh_v2_run_drawer(self) -> None:
        """把 V2 ``_v2_drawer_items`` 聚合成 ``RunDrawerState`` 推入 panel。"""

        if not self._v2_drawer_items:
            return
        try:
            from src.features.edit.ui.ai_assistant_composer import RunDrawerState
        except ImportError:
            return
        # 计数只统计主 run；工具节点是展开详情，不应把“完成 1”变成
        # “完成 4”或把一个运行误报成多个任务。
        roots = [i for i in self._v2_drawer_items if i.kind == "run"]
        active = sum(i.status in ("running", "queued", "waiting_input") for i in roots)
        done = sum(i.status in ("succeeded", "skipped") for i in roots)
        failed = sum(i.status == "failed" for i in roots)
        state = RunDrawerState(
            items=list(self._v2_drawer_items),
            active_count=active,
            completed_count=done,
            failed_count=failed,
            history=list(self._v2_drawer_history),
        )
        try:
            self._panel.set_run_drawer(state)
        except Exception:  # noqa: BLE001
            pass

    def _on_chat_delta(
        self, delta: str, worker: QThread | None = None,
    ) -> None:
        """把流式增量安全地交给 UI；不写入会话历史直到完成。"""
        if worker is not None and worker is not self._chat_worker:
            return
        # 取消后到达的迟到增量直接丢弃：HTTP 流无法中断，但 UI 不应再刷新
        # 已被用户放弃的回复气泡（plan §P0 — 无迟到结果）。
        if getattr(worker, "cancel_requested", False):
            return
        if not delta:
            return
        self._streaming_reply_text += delta
        show = getattr(self._panel, "set_streaming_reply", None)
        if callable(show):
            show(self._streaming_reply_text)
        self._update_legacy_chat_activity(
            getattr(self, "_chat_activity_reply_label", "模型思考中"),
            detail=f"已生成 {len(self._streaming_reply_text)} 字",
        )

    def _context_limit_tokens(self) -> int:
        """读取当前 Profile 的上下文上限，缺省使用稳定的 200k。"""
        try:
            active = (
                self._profile_registry.active_profile
                if self._profile_registry is not None else None
            )
            limit = getattr(getattr(active, "limits", None), "max_context_tokens", 200_000)
            return max(1_024, int(limit))
        except (TypeError, ValueError, AttributeError):
            return 200_000

    @staticmethod
    def _estimate_chat_tokens(messages: list[dict[str, str]]) -> int:
        """估算完整消息数组的 token 数（含每条消息的轻量协议开销）。"""
        from src.features.edit.ai_editing.token_estimator import estimate_text_tokens

        return sum(4 + estimate_text_tokens(str(m.get("content", ""))) for m in messages)

    def _compress_chat_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        limit: int,
        record_checkpoint: bool = True,
    ) -> list[dict[str, Any]]:
        """在发送前压缩旧消息，保留系统提示和最近对话回合。

        原始消息仍留在 UI 与本机历史缓存中；这里只生成本次请求的轻量摘要，
        因而不会破坏用户向上滚动查看历史的能力。
        """
        before_tokens = self._estimate_chat_tokens(messages)
        if before_tokens <= limit:
            self._chat_context_compressed = False
            self._chat_context_tokens = before_tokens
            return messages

        # system prompt（包括可选 feature context）放在前面，剩余是对话消息。
        system_messages = [m for m in messages if m.get("role") == "system"]
        history = [m for m in messages if m.get("role") != "system"]
        keep_count = min(6, len(history))  # 保留最近三轮，避免摘要掩盖当前意图
        old_messages = history[:-keep_count] if keep_count else history
        recent_messages = history[-keep_count:] if keep_count else []
        summary_parts = []
        for message in old_messages:
            role = "用户" if message.get("role") == "user" else "助手"
            text = extract_text_from_content(message.get("content", "")).replace("\n", " ").strip()
            if text:
                summary_parts.append(f"{role}：{text[:240]}")
        summary_text = (
            "以下是更早对话的自动摘要（原始历史仍保存在本地历史会话中）：\n"
            + "\n".join(summary_parts)
        )
        summary_messages = ([{"role": "system", "content": summary_text}] if summary_parts else [])
        candidate = system_messages + summary_messages + recent_messages
        spill_id = ""
        if old_messages:
            try:
                spill = self._context_spill_store.record(
                    scope=self._trace_session_id(),
                    kind="chat_history",
                    items=old_messages,
                    token_count=self._estimate_chat_tokens(old_messages),
                    preview=summary_text,
                )
                spill_id = spill.spill_id
            except Exception:  # noqa: BLE001 - compression must remain best effort
                logger.debug("record chat context spill failed", exc_info=True)

        # 摘要仍超限时逐条淘汰最旧的近期消息，至少保留当前最后一条。
        while self._estimate_chat_tokens(candidate) > limit and len(recent_messages) > 1:
            recent_messages.pop(0)
            candidate = system_messages + summary_messages + recent_messages

        # 极端小预算/超长系统提示下，逐步缩短摘要；不静默突破 provider 上限。
        while self._estimate_chat_tokens(candidate) > limit and summary_text:
            summary_text = summary_text[: max(64, int(len(summary_text) * 0.75))]
            summary_messages = [{"role": "system", "content": summary_text}]
            candidate = system_messages + summary_messages + recent_messages
            if len(summary_text) <= 64:
                break

        # 最后一条也可能本身超出极小 Profile；继续缩短消息正文，确保真正
        # 发送给 provider 的数组不会悄悄超过上限。
        while self._estimate_chat_tokens(candidate) > limit and recent_messages:
            last = recent_messages[-1]
            # 多模态 content（list）无法按字符串切片：先降级为纯文本（丢弃图片）
            # 再做长度裁剪，避免把 content-parts 列表当字符串 str() 化或切片。
            raw = last.get("content", "")
            if isinstance(raw, list):
                content = extract_text_from_content(raw)
                last["content"] = content
            else:
                content = str(raw)
            if len(content) <= 32:
                recent_messages.pop()
            else:
                last["content"] = content[: max(32, int(len(content) * 0.75))]
            candidate = system_messages + summary_messages + recent_messages

        while self._estimate_chat_tokens(candidate) > limit and len(system_messages) > 1:
            system_messages.pop()
            candidate = system_messages + summary_messages + recent_messages

        # A single system block can itself exceed a tiny provider budget. Keep
        # the policy prefix, but hard-cap the emitted text instead of silently
        # sending an over-limit request.
        while self._estimate_chat_tokens(candidate) > limit and system_messages:
            block = system_messages[-1]
            raw = str(block.get("content", ""))
            if len(raw) <= 64:
                system_messages.pop()
            else:
                system_messages[-1] = {
                    **block,
                    "content": raw[: max(64, int(len(raw) * 0.75))],
                }
            candidate = system_messages + summary_messages + recent_messages

        self._chat_context_compressed = True
        self._chat_context_tokens = self._estimate_chat_tokens(candidate)
        signature = (len(messages), before_tokens, spill_id)
        if record_checkpoint and signature != self._last_chat_compression_signature:
            self._last_chat_compression_signature = signature
            if self.session_store is not None:
                try:
                    from src.features.edit.ai_editing.session_models import CompressionCheckpoint
                    self.session_store.add_compression_checkpoint(CompressionCheckpoint(
                        summary=summary_text[:500],
                        message_count_before=len(messages),
                        message_count_after=len(candidate),
                        spill_id=spill_id,
                    ))
                except Exception:  # noqa: BLE001
                    logger.debug("automatic chat compression checkpoint failed", exc_info=True)
        return candidate

    def _register_mcp_session_images(self, attachments: tuple) -> None:
        """把图片附件写临时文件并注册到 MCP 视觉网关（'latest' 引用来源）。

        网关不可用时静默跳过（优雅降级）；路径只在宿主与 MCP server 之间流转，
        **不回传给模型**（模型用 'latest' 引用）。
        """
        import base64 as _b64
        try:
            from src.features.edit.ai_editing.mcp_vision_gateway import (
                get_mcp_vision_gateway,
            )
            gw = get_mcp_vision_gateway()
            for att in attachments:
                url = str(getattr(att, "data_url", "") or "")
                comma = url.find(",")
                if comma < 0:
                    continue
                try:
                    raw = _b64.b64decode(url[comma + 1:])
                except Exception:  # noqa: BLE001
                    continue
                mime = str(getattr(att, "mime", "image/jpeg") or "image/jpeg")
                gw.save_and_register_image(raw, mime)
        except Exception:  # noqa: BLE001 — MCP 注册失败不影响聊天主链路
            logger.debug("register MCP session images failed", exc_info=True)

    def _build_chat_messages(
        self,
        *,
        system_instruction: str = "",
        draft_text: str = "",
        draft_attachments: tuple[ChatAttachment, ...] = (),
        include_history: bool = True,
        record_checkpoint: bool = True,
    ) -> list[dict[str, Any]]:
        """构造完整多轮上下文，并在超过 Profile 上限时自动压缩旧消息。

        多模态：带图片附件的历史消息会被打包成 OpenAI content-parts 列表；
        压缩时会丢弃旧消息里的图片（只保留文本摘要）。
        """
        system_messages: list[dict[str, Any]] = [
            {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
        ]
        if include_history and callable(self._teaching_context_provider):
            try:
                teaching_context = self._teaching_context_provider()
                if teaching_context:
                    system_messages.append({
                        "role": "system",
                        "content": (
                            "当前工程已启用 Teaching Animation。以下是共享场景上下文；"
                            "场景必须通过 scene_id 引用，修改前应先给出可审阅的变更，"
                            "并等待用户确认后再应用：\n"
                            + json.dumps(teaching_context, ensure_ascii=False, sort_keys=True, default=str)
                        ),
                    })
            except Exception:  # noqa: BLE001
                logger.debug("build Teaching Animation context failed", exc_info=True)
        if include_history:
            try:
                history = [
                    {"role": m.role, "content": message_content(m.text, m.attachments)}
                    for m in self._service.recent_chat_messages(limit=100_000)
                ]
            except Exception:  # noqa: BLE001
                logger.debug("build chat messages failed", exc_info=True)
                history = []
        else:
            history = []
        if draft_text.strip() or draft_attachments:
            history.append({
                "role": "user",
                "content": message_content(draft_text.strip(), draft_attachments),
            })
        if system_instruction:
            system_messages.insert(1, {"role": "system", "content": system_instruction})
        return self._compress_chat_messages(
            system_messages + history,
            limit=self._context_limit_tokens(),
            record_checkpoint=record_checkpoint,
        )

    def set_teaching_context_provider(self, provider: Any | None) -> None:
        """Attach an optional feature context source to the shared Assistant Dock."""
        self._teaching_context_provider = provider

    def set_teaching_request_handler(self, handler: Any | None) -> None:
        """Attach an optional host action for explicit Teaching requests."""
        self._teaching_request_handler = handler

    @staticmethod
    def _invoke_teaching_request_handler(
        handler: Any, text: str, target_asset_ids: tuple[str, ...],
    ) -> bool:
        """Forward explicit media-pool targets while keeping old handlers working."""
        try:
            return bool(handler(text, target_asset_ids=target_asset_ids))
        except TypeError:
            # Compatibility with older optional feature handlers and test doubles
            # that still expose the original ``handler(text)`` contract.
            return bool(handler(text))

    def set_teaching_motion_graphic_mode_handler(self, handler: Any | None) -> None:
        """Attach the explicit Motion Graphic composer-mode entry point.

        Unlike the legacy natural-language predicate, this handler is called
        for every request sent while the Motion Graphic mode is selected.  The
        Teaching Animation feature still owns the confirmation card and all
        host writes; the controller only routes the request.
        """
        self._teaching_motion_graphic_mode_handler = handler

    def set_teaching_motion_graphic_discussion_handler(self, handler: Any | None) -> None:
        """Attach the feature predicate for Motion Graphic planning turns."""
        self._teaching_motion_graphic_discussion_handler = handler

    def set_teaching_scene_edit_request_handler(self, handler: Any | None) -> None:
        """Attach the predicate for complex model-planned scene edits."""
        self._teaching_scene_edit_request_handler = handler

    def set_teaching_scene_edit_response_handler(self, handler: Any | None) -> None:
        """Attach the validator/preview callback for model JSON edit plans."""
        self._teaching_scene_edit_response_handler = handler

    def set_teaching_html_generate_response_handler(self, handler: Any | None) -> None:
        """Attach the callback that consumes a model-produced HTML document.

        The Teaching Animation feature owns sanitization, preview evidence and
        the approval/write boundary.  The shared controller only transports the
        completed text and reports failure/cancellation with an empty response.
        """
        self._teaching_html_generate_response_handler = handler

    def set_teaching_motion_graphic_build_response_handler(self, handler: Any | None) -> None:
        """Attach the callback that consumes an LLM-built Motion Graphic scene plan.

        由 teaching_animation module 在 attach 时注入。签名：
        ``(request_id: str, response_text: str) -> bool``。controller 在
        ``teaching_motion_graphic_build`` chat worker 完成后调它，module 据此
        解析 JSON、构造场景并显示确认卡片。
        """
        self._teaching_motion_graphic_build_response_handler = handler

    def start_motion_graphic_build_chat(
        self, request_id: str, user_prompt: str, *,
        temperature: float | None = None,
    ) -> bool:
        """启动一个 LLM chat 让模型从零生成 Motion Graphic 场景命令（B 方案）。

        由 teaching_animation module 在授权后调用。走 ``_start_chat`` 复用既有
        LLM chat 通道（json_mode=True），只是 chat_mode 不同，响应分发逻辑在
        ``_on_chat_finished`` 里按 mode 路由。

        Args:
            request_id: 对应 ``_pending_motion_graphic_requests`` 里的 plan id。
            user_prompt: module 构造好的 user message（含用户描述 + 画布/时长约束）。

        Returns:
            True 表示已成功起 chat worker；False 表示 LLM 不可用（module 据此走兜底）。
        """
        if not request_id or not self._can_use_llm_chat():
            return False
        # module 的 system prompt（场景设计契约）。从 teaching_animation 取，避免
        # 在 controller 里硬编码 feature 细节。这里用一个稳定常量作为 fallback。
        system_instruction = getattr(
            self, "_teaching_motion_graphic_build_system_prompt", None,
        )
        if not system_instruction:
            # feature 未提供 system prompt → 退回，module 会走兜底。
            return False
        self._teaching_motion_graphic_build_request_id = str(request_id)
        try:
            self._start_chat(
                user_prompt,
                chat_mode="teaching_motion_graphic_build",
                system_instruction=system_instruction,
                temperature_override=temperature,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("start_motion_graphic_build_chat failed: %s", exc)
            self._teaching_motion_graphic_build_request_id = ""
            return False

    def start_html_generate_chat(
        self, request_id: str, user_prompt: str,
        *, system_instruction: str = "", temperature: float | None = None,
    ) -> bool:
        """Start a text chat whose response must be a complete HTML document.

        This deliberately reuses the existing ``_ChatWorker``.  HTML is not
        JSON, and the feature module performs the actual sanitizer/quality
        checks after the worker returns, so this method only owns transport and
        chat lifecycle state.

        ``temperature`` lets structured sub-calls of the edit pipeline (the
        step planner) run cooler than the full-creativity HTML authoring
        temperature.

        B3: Also creates a V2 AgentRun so the generation is visible in the
        Run Drawer (staged → verified → written).
        """
        if not request_id or not self._can_use_llm_chat():
            return False
        prompt = str(system_instruction or "").strip()
        if not prompt:
            return False
        self._teaching_html_generate_request_id = str(request_id)
        # AUD-2013: _teaching_html_generate_system_prompt 为只写不读的死状态，
        # 已移除（system prompt 直接经 _start_chat(system_instruction=...) 传递）。
        # B3: 创建 AgentRun 使 HTML 生成全程在 Run Drawer 可见。
        self.start_motion_graphic_run(request_id)
        try:
            self._start_chat(
                str(user_prompt or ""),
                chat_mode="teaching_html_generate",
                system_instruction=prompt,
                # HTML is visual authorship, not a reasoning-effort answer.
                # Full creative temperature: the browser host judges nothing,
                # so the model should not be damped below its natural range.
                temperature_override=(
                    temperature if temperature is not None
                    else _HTML_GENERATE_TEMPERATURE
                ),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("start_html_generate_chat failed: %s", exc)
            self._teaching_html_generate_request_id = ""
            return False

    def set_teaching_motion_graphic_build_system_prompt(self, prompt: str) -> None:
        """注入 Motion Graphic 场景构建的 system prompt（由 feature module 提供）。

        controller 不硬编码 feature 细节；feature 把自己的设计契约 prompt 注入进来，
        ``start_motion_graphic_build_chat`` 据此构造 LLM 请求。
        """
        self._teaching_motion_graphic_build_system_prompt = str(prompt or "")

    def _on_chat_finished(
        self, reply: str, worker: QThread | None = None,
    ) -> None:
        """LLM 回复到达 → 回 IDLE + 记录 assistant 消息 + 写会话摘要。"""
        if worker is not None and worker is not self._chat_worker:
            return
        self._finish_legacy_chat_activity("已完成")
        self._chat_worker = None
        clear_stream = getattr(self._panel, "clear_streaming_reply", None)
        if callable(clear_stream):
            clear_stream()
        self._streaming_reply_text = ""
        text = (reply or "").strip() or "（模型返回为空）"
        if self._chat_mode == "teaching_scene_edit":
            original = self._teaching_scene_edit_text
            self._chat_mode = "normal"
            self._teaching_scene_edit_text = ""
            handler = self._teaching_scene_edit_response_handler
            if callable(handler):
                try:
                    self._service._set_state(AssistantState.IDLE)  # SLF001
                    self._service.append_chat_message(
                        "assistant", "已读取当前场景上下文，生成了一份待确认的修改计划。",
                    )
                    if handler(original, text):
                        self._record_assistant_message("已生成场景修改计划，等待确认。")
                        return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Teaching Animation scene edit response handling failed")
                    self._on_chat_failed(str(exc))
                    return
        if self._chat_mode == "teaching_motion_graphic_build":
            request_id = self._teaching_motion_graphic_build_request_id
            self._chat_mode = "normal"
            self._teaching_motion_graphic_build_request_id = ""
            handler = self._teaching_motion_graphic_build_response_handler
            if callable(handler) and request_id:
                try:
                    self._service._set_state(AssistantState.IDLE)  # SLF001
                    # build 模式的 LLM 响应是结构化 JSON，不直接展示给用户；
                    # module 的 handler 会构造场景并显示确认卡片（含友好摘要）。
                    # handler 返回 True 表示场景构造成功；False 表示 JSON 无效，
                    # module 内部会标记 plan 走兜底并显示兜底卡片。
                    handler(request_id, text)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Motion Graphic build response handling failed")
                    self._on_chat_failed(str(exc))
                    return
        if self._chat_mode == "teaching_html_generate":
            request_id = self._teaching_html_generate_request_id
            handler = self._teaching_html_generate_response_handler
            self._chat_mode = "normal"
            self._teaching_html_generate_request_id = ""
            if callable(handler) and request_id:
                try:
                    self._service._set_state(AssistantState.IDLE)  # SLF001
                    handled_result = handler(request_id, text)
                    # ``None`` is the explicit asynchronous/pending result:
                    # the feature has queued browser work and will call
                    # finish_motion_graphic_html_postprocess when it returns.
                    if handled_result is None:
                        return
                    handled = bool(handled_result)
                    if handled:
                        # B3: stage HTML artifact（只存 hash 引用，不内联聊天日志）。
                        import hashlib
                        html_hash = hashlib.sha256(
                            text.encode("utf-8"),
                        ).hexdigest()[:16]
                        self.stage_motion_graphic_html(
                            request_id, html_hash=html_hash,
                            page_count=text.count("<html") or 1,
                        )
                        self._record_assistant_message(
                            "已生成 HTML 动效预览，等待确认。",
                        )
                    else:
                        # B3: handler 返回 False 表示生成失败。
                        self.complete_motion_graphic_run(
                            request_id, success=False,
                        )
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Teaching Animation HTML response handling failed")
                    self.complete_motion_graphic_run(request_id, success=False)
                    self._on_chat_failed(str(exc))
                    return
        self._chat_mode = "normal"
        self._teaching_scene_edit_text = ""
        try:
            self._service._set_state(AssistantState.IDLE)  # SLF001
            self._service.append_chat_message("assistant", text)
        except Exception:  # noqa: BLE001
            logger.debug("append assistant chat message failed", exc_info=True)
        self._record_assistant_message(text)

    def start_motion_graphic_postprocess(
        self,
        request_id: str,
        response: str,
        processor: Any,
        on_finished: Any,
        on_failed: Any,
    ) -> bool:
        """Start expensive Motion Graphic browser work in a QThread.

        ``processor`` must be UI-free and return a serializable-in-process
        payload. ``on_finished``/``on_failed`` are invoked on the controller's
        Qt thread, where the feature may safely update panels and project
        state.
        """
        request_id = str(request_id)
        current = self._motion_graphic_postprocess_workers.get(request_id)
        if current is not None and current.isRunning():
            return False
        worker = _MotionGraphicPostprocessWorker(
            request_id, response, processor, parent=self,
        )
        self._motion_graphic_postprocess_workers[request_id] = worker
        worker.finished_payload.connect(self._on_motion_graphic_postprocess_finished)
        worker.failed.connect(self._on_motion_graphic_postprocess_failed)
        worker.finished.connect(worker.deleteLater)
        # Store callbacks on the worker rather than on a lambda connected from
        # a worker thread; the bound controller slots below are guaranteed to
        # be queued back to the controller's Qt thread.
        worker._on_finished_callback = on_finished  # type: ignore[attr-defined]
        worker._on_failed_callback = on_failed  # type: ignore[attr-defined]
        worker.start()
        return True

    def cancel_motion_graphic_postprocess(self, request_id: str) -> None:
        worker = self._motion_graphic_postprocess_workers.get(str(request_id))
        if worker is not None:
            worker.cancel_requested = True

    def _on_motion_graphic_postprocess_finished(
        self, request_id: str, payload: object,
    ) -> None:
        worker = self._motion_graphic_postprocess_workers.pop(str(request_id), None)
        if worker is None:
            return
        callback = getattr(worker, "_on_finished_callback", None)
        if callable(callback):
            callback(str(request_id), payload)

    def _on_motion_graphic_postprocess_failed(
        self, request_id: str, error: str,
    ) -> None:
        worker = self._motion_graphic_postprocess_workers.pop(str(request_id), None)
        if worker is None:
            return
        callback = getattr(worker, "_on_failed_callback", None)
        if callable(callback):
            callback(str(request_id), str(error))

    def finish_motion_graphic_html_postprocess(
        self, request_id: str, raw_html: str, *, success: bool,
    ) -> None:
        """Finish B3 bookkeeping after the feature's UI callback completes."""
        if success:
            import hashlib
            self.stage_motion_graphic_html(
                request_id,
                html_hash=hashlib.sha256(str(raw_html).encode("utf-8")).hexdigest()[:16],
                page_count=str(raw_html).lower().count("<html") or 1,
            )
            self._record_assistant_message("已生成 HTML 动效预览，等待确认。")
        else:
            self.complete_motion_graphic_run(request_id, success=False)

    def _on_chat_failed(
        self, error: str, worker: QThread | None = None,
    ) -> None:
        """LLM 调用失败 → 显示红色错误消息 + 仍出推荐卡兜底（用户有可点击后续）。"""
        if worker is not None and worker is not self._chat_worker:
            return
        self._finish_legacy_chat_activity("调用失败", status="failed", detail=str(error))
        self._chat_worker = None
        clear_stream = getattr(self._panel, "clear_streaming_reply", None)
        if callable(clear_stream):
            clear_stream()
        self._streaming_reply_text = ""
        # Motion Graphic build 模式失败：通知 module 走兜底（传空响应），
        # 让 pending plan 标记 build_failed 并显示兜底确认卡片。
        if self._chat_mode == "teaching_motion_graphic_build":
            request_id = self._teaching_motion_graphic_build_request_id
            self._chat_mode = "normal"
            self._teaching_motion_graphic_build_request_id = ""
            handler = self._teaching_motion_graphic_build_response_handler
            if callable(handler) and request_id:
                try:
                    handler(request_id, "")
                except Exception:  # noqa: BLE001
                    logger.debug("Motion Graphic build fallback notify failed", exc_info=True)
            # 仍显示错误消息给用户（build 失败已由 module 兜底处理）。
            try:
                self._service._set_state(AssistantState.IDLE)  # SLF001
                self._service.append_chat_message(
                    "assistant", f"基于 AI 的场景生成失败：{error}", error_code="llm_error",
                )
            except Exception:  # noqa: BLE001
                logger.debug("append chat error message failed", exc_info=True)
            return
        if self._chat_mode == "teaching_html_generate":
            request_id = self._teaching_html_generate_request_id
            handler = self._teaching_html_generate_response_handler
            self._chat_mode = "normal"
            self._teaching_html_generate_request_id = ""
            # B3: 标记 Motion Graphic run 为失败。
            self.complete_motion_graphic_run(request_id, success=False)
            if callable(handler) and request_id:
                try:
                    handler(request_id, "")
                except Exception:  # noqa: BLE001
                    logger.debug("HTML generation failure notification failed", exc_info=True)
            try:
                self._service._set_state(AssistantState.IDLE)  # SLF001
                self._service.append_chat_message(
                    "assistant", f"HTML 动效生成失败：{error}", error_code="html_generation_failed",
                )
            except Exception:  # noqa: BLE001
                logger.debug("append HTML generation error message failed", exc_info=True)
            return
        self._chat_mode = "normal"
        self._teaching_scene_edit_text = ""
        try:
            self._service._set_state(AssistantState.IDLE)  # SLF001
            self._service.append_chat_message(
                "assistant", f"模型调用失败：{error}", error_code="llm_error",
            )
        except Exception:  # noqa: BLE001
            logger.debug("append chat error message failed", exc_info=True)
        # 仍出推荐卡兜底（用户有可点击的后续动作；行为对称现有 unknown 分支）
        self._show_skill_recommendation()

    def _on_chat_cancelled(self, worker: QThread | None = None) -> None:
        """用户取消 chat → 回 IDLE，不追加消息（与 _on_plan_cancelled 对称）。"""
        if worker is not None and worker is not self._chat_worker:
            return
        partial_reply = self._streaming_reply_text
        self._finish_legacy_chat_activity("已取消", status="cancelled")
        self._chat_worker = None
        # Motion Graphic build 取消：通知 module 走兜底，与失败语义一致。
        if self._chat_mode == "teaching_motion_graphic_build":
            request_id = self._teaching_motion_graphic_build_request_id
            handler = self._teaching_motion_graphic_build_response_handler
            if callable(handler) and request_id:
                try:
                    handler(request_id, "")
                except Exception:  # noqa: BLE001
                    logger.debug("Motion Graphic build cancel notify failed", exc_info=True)
        if self._chat_mode == "teaching_html_generate":
            request_id = self._teaching_html_generate_request_id
            handler = self._teaching_html_generate_response_handler
            # B3/AUD-2003：与 _on_chat_failed 对称——用户取消也必须终结
            # V2 Motion Graphic run，否则 Run Drawer 留下永久 running 的
            # 幽灵行（start_motion_graphic_run 注释明确担心的场景）。
            self.complete_motion_graphic_run(request_id, success=False)
            if callable(handler) and request_id:
                try:
                    handler(request_id, "")
                except Exception:  # noqa: BLE001
                    logger.debug("HTML generation cancel notification failed", exc_info=True)
        self._chat_mode = "normal"
        self._teaching_scene_edit_text = ""
        self._teaching_motion_graphic_build_request_id = ""
        self._teaching_html_generate_request_id = ""
        try:
            self._service._set_state(AssistantState.IDLE)  # SLF001
        except Exception:  # noqa: BLE001
            logger.warning("failed to reset assistant state to idle after cancel", exc_info=True)
        preserve = getattr(self._panel, "preserve_cancelled_streaming_reply", None)
        if callable(preserve):
            preserve(partial_reply)
        else:
            clear_stream = getattr(self._panel, "clear_streaming_reply", None)
            if callable(clear_stream):
                clear_stream()
        self._streaming_reply_text = ""

    def _record_assistant_message(self, text: str) -> None:
        """把 assistant 回复写进会话目录（对称 ``_record_user_message``；脱敏摘要）。

        B6: 存储完整正文（上限 64000 字符），而非 500 字截断摘要。
        ``MessageSummary.to_dict()`` 自动调用 ``sanitize_for_storage`` 脱敏。
        """
        if self.session_store is None or not text:
            return
        try:
            from src.features.edit.ai_editing.message_record import (
                truncate_for_storage,
            )
            from src.features.edit.ai_editing.session_models import MessageSummary
            full_body, _ = truncate_for_storage(text)
            self.session_store.add_message_summary(
                MessageSummary(role="assistant", text=full_body),
            )
            self._refresh_history_menu()
        except Exception:  # noqa: BLE001
            logger.debug("assistant message summary record failed", exc_info=True)

    def _show_analyze_hint(
        self, result: Any, *, text: str = "", scope: Any = None,
    ) -> None:
        """把分析意图变成真实 propose 卡，而不是停在 hint。

        Agent 可以自主选择本地 recipe，但不能替用户确认素材范围；确认后才由
        ``MediaAnalysisController`` 入队执行。
        """
        controller = self._analysis_controller
        asset_ids = tuple(getattr(scope, "selected_asset_ids", ()) or ())
        if controller is None:
            self._panel.set_clarification(
                "分析服务尚未初始化，请先打开一个剪辑工程。", "analyze_scope",
            )
            return
        if not asset_ids:
            hint = getattr(result, "scope_hint", "") or "请先在媒体池选择要分析的素材。"
            self._panel.set_clarification(hint, "analyze_scope")
            return

        from src.features.edit.ai_editing.analysis_request import AnalysisRecipe

        lowered = (text or "").lower()
        recipe = AnalysisRecipe.FAST_LOCAL
        if any(token in lowered for token in ("转录", "asr", "字幕", "口播", "访谈")):
            recipe = AnalysisRecipe.EDITORIAL_LOCAL
        elif any(token in lowered for token in ("ocr", "文字识别", "视觉", "标签")):
            recipe = AnalysisRecipe.VISUAL_SEMANTIC_LOCAL
        try:
            controller.request_analysis_draft(
                asset_ids,
                recipe=recipe,
                requested_by="assistant",
                title=f"Agent：{getattr(result, 'description', '') or '分析素材'}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI analysis proposal failed")
            self._panel.set_clarification(f"无法创建分析计划：{exc}", "analyze_error")

    def _on_analysis_proposed(self, request: Any) -> None:
        """分析控制器发出 proposal 后，渲染到 Agent 对话流。"""
        try:
            self._panel.set_analysis_proposal(request)
        except Exception:  # noqa: BLE001
            logger.debug("rendering analysis proposal failed", exc_info=True)

    def _on_analysis_completed(self, request_id: str, summary: Any) -> None:
        try:
            count = int((summary or {}).get("assets", 0)) if isinstance(summary, dict) else 0
            self._panel.set_analysis_progress(
                request_id, f"分析完成：已为 {count} 个素材建立证据，可以继续生成剪辑方案。",
            )
            continuation = self._pending_skill_continuations.pop(request_id, None)
            if continuation is not None:
                result, scope = continuation
                # signal 已经回到 controller 所在线程；重新读取 scope/evidence 后
                # 进入统一 compiler/adapter 路径，直到产生 plan/preview。
                self._execute_skill_route(result, scope)
        except Exception:  # noqa: BLE001
            logger.debug("rendering analysis completion failed", exc_info=True)

    def _on_analysis_failed(self, request_id: str, message: str) -> None:
        self._pending_skill_continuations.pop(request_id, None)
        try:
            self._panel.set_analysis_progress(request_id, f"分析失败：{message}")
        except Exception:  # noqa: BLE001
            logger.debug("rendering analysis failure failed", exc_info=True)

    def _on_analysis_confirm_requested(self, request_id: str) -> None:
        controller = self._analysis_controller
        if controller is None:
            return
        try:
            _job, error = controller.confirm_and_execute(request_id)
            if error:
                self._panel.set_analysis_progress(request_id, f"分析未启动：{error}")
            else:
                self._panel.set_analysis_progress(request_id, "分析已确认，正在后台执行…")
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI analysis confirmation failed")
            self._panel.set_analysis_progress(request_id, f"分析启动失败：{exc}")

    def _on_analysis_cancel_requested(self, request_id: str) -> None:
        try:
            controller = self._analysis_controller
            if controller is not None and hasattr(controller, "cancel_request"):
                controller.cancel_request(request_id)
            self._pending_skill_continuations.pop(request_id, None)
            self._panel.clear_analysis_proposal(request_id)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _run_summary_for(skill_id: str, compiled: Any) -> str:
        """为 record_run 生成简短摘要。"""
        target = getattr(compiled, "target", "") or "skill"
        return f"执行 {skill_id}（{target}）"

    def _record_user_message(self, text: str) -> None:
        """把用户请求写进会话目录（P0.4；脱敏摘要，不存原文/绝对路径/secret）。

        B6: 存储完整正文（上限 64000 字符），而非 500 字截断摘要。
        """
        if self.session_store is None or not text:
            return
        try:
            from src.features.edit.ai_editing.message_record import (
                truncate_for_storage,
            )
            from src.features.edit.ai_editing.session_models import MessageSummary
            full_body, _ = truncate_for_storage(text)
            self.session_store.add_message_summary(
                MessageSummary(role="user", text=full_body),
            )
            active = self.session_store.get_active_session()
            if active is not None and not str(getattr(active, "title", "") or "").strip():
                set_title = getattr(self.session_store, "set_session_title", None)
                if callable(set_title):
                    set_title(active.session_id, text.replace("\n", " ").strip()[:42])
            self._refresh_history_menu()
        except Exception:  # noqa: BLE001
            logger.debug("user message summary record failed", exc_info=True)

    def _record_run_finalization(self, run_id: str, *, summary: str = "") -> None:
        """run 终态后把 RunRef + assistant 摘要写进会话目录（P0.4）。

        从 runtime 读 RunRecord 派生 RunRef（run_id/skill_id/status/summary）；
        summary 参数可选（host 可传更丰富的描述）。store 未注入时跳过 session 记录，
        但 P1.5 结果卡渲染仍执行（独立于 store）。
        """
        # skill_id 在 runtime 已装配时从 invocation 派生（P1.5 结果卡标题追溯用）。
        skill_id = ""
        if self.runtime is not None and run_id:
            inv = self.runtime.get_invocation(run_id)
            if inv is not None:
                skill_id = inv.skill_id

        # session 目录记录（store 未注入时跳过）。
        if self.session_store is not None and run_id:
            try:
                from src.features.edit.ai_editing.session_models import (
                    MessageSummary, RunRef,
                )
                import time as _time
                status = ""
                created_at = _time.time()
                rec = self.runtime.get_run(run_id) if self.runtime is not None else None
                if rec is not None:
                    status = rec.status
                    created_at = getattr(rec, "created_at", created_at) or created_at
                self.session_store.record_run(RunRef(
                    run_id=run_id, skill_id=skill_id, status=status,
                    summary=summary[:300], created_at=created_at,
                ))
                if summary:
                    from src.features.edit.ai_editing.message_record import (
                        truncate_for_storage,
                    )
                    full_summary, _ = truncate_for_storage(summary)
                    self.session_store.add_message_summary(MessageSummary(
                        role="assistant", text=full_summary, run_id=run_id,
                    ))
            except Exception:  # noqa: BLE001
                logger.debug("run finalization record failed", exc_info=True)

        # P1.5：run 终态后聚合 ResultCardState 渲染（plan §10.3 P1.5）。
        # 独立于 session_store（即使无 store 也要让用户看到结构化结果卡）。
        # 不读原文/路径/secret：只用 RunRecord 公开字段 + typed artifact 摘要。
        self._emit_result_card_for_run(run_id, skill_id)

    def _emit_result_card_for_run(self, run_id: str, skill_id: str) -> None:
        """从 runtime 取终态 RunRecord，聚合成 ResultCardState 并 emit（P1.5）。

        ACTIVE/SKIPPED 不渲染；聚合异常降级 error_result（不崩）。
        runtime 未装配时 no-op。
        """
        if self.runtime is None or not run_id:
            return
        try:
            from src.features.ai_harness.result_card_builder import (
                build_result_card_from_run,
            )
            rec = self.runtime.get_run(run_id)
            if rec is None or not rec.is_terminal:
                return
            if rec.status == "skipped":
                return
            resolver = self._make_artifact_resolver()
            state = build_result_card_from_run(
                rec, skill_id=skill_id, artifact_resolver=resolver,
            )
            if state is not None:
                self.result_card_ready.emit(state)
        except Exception:  # noqa: BLE001
            logger.debug("result card build failed", exc_info=True)

    def _make_artifact_resolver(self) -> Any:
        """构造 artifact_resolver 闭包（供 result_card_builder 取 typed artifact）。

        首版从 ``service._artifact_store`` 取（内部属性）；store 为 None 时返回 None
        （builder 用 ArtifactRef.summary 兜底）。resolver 内部异常被吞，返回 None。
        """
        store = getattr(self._service, "_artifact_store", None)
        generation_service = getattr(
            getattr(self.generation_adapter, "_svc", None), "generation_bin", None,
        )
        if store is None and generation_service is None:
            return None

        def _resolve(artifact_id: str) -> Any:
            try:
                if generation_service is not None:
                    generated = generation_service.get(artifact_id)
                    if generated is not None:
                        return generated
                return store.get(artifact_id) if store is not None else None
            except Exception:  # noqa: BLE001
                return None

        return _resolve

    # ------------------------------------------------------------------ #
    # P1.5 动作接线：结果卡动作分派（retry/adjust_params；approve/apply 留下批）
    # ------------------------------------------------------------------ #

    def _on_result_action(self, run_id: str, action_kind: str) -> None:
        """结果卡动作分派入口（plan §10.3 P1.5 动作接线）。

        ``run_id`` == :class:`ResultCardState.result_id`（builder 用 run.run_id 赋值）。
        按 ``action_kind`` 分派到具体 handler；未知动作 no-op（不崩）。
        """
        if action_kind == "retry":
            self._on_result_action_retry(run_id)
        elif action_kind == "adjust_params":
            self._on_result_action_adjust_params(run_id)
        elif action_kind == "locate_in_pool":
            self._on_generated_media_notice(run_id)
        elif action_kind == "import_to_pool":
            self._on_import_generated_asset(run_id)
        elif action_kind == "place_on_timeline":
            self._on_place_generated_media(run_id)
        elif action_kind in ("approve", "apply"):
            # 留下批：语义断层需先明确哪些 skill 产 EditPlan（多数 skill 产物无
            # timeline 变更，ApprovalScope 要求 plan_hash，对这些结果 approve/apply 无意义）。
            logger.debug(
                "approve/apply 接线留下批：run_id=%s action=%s", run_id, action_kind,
            )
        # 其他动作（view_evidence/preview_diff/discard/view_diagnostics/
        # open_in_editor）留后续批次。

    def _generated_asset_id_from_run(self, run_id: str) -> str:
        if self.runtime is None:
            return ""
        record = self.runtime.get_run(run_id)
        if record is None:
            return ""
        for artifact in record.artifacts:
            if str(artifact.artifact_type).startswith("generated_"):
                return artifact.artifact_id
        return ""

    def _on_generated_media_notice(self, run_id: str) -> None:
        asset_id = self._generated_asset_id_from_run(run_id)
        preview = getattr(self._bridge, "preview_ai_generated_asset", None)
        if asset_id and callable(preview):
            try:
                ok, message = preview(asset_id)
                show = getattr(self._panel, "set_agent_notice", None)
                if callable(show):
                    show(message, warning=not ok)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("preview generated asset failed")
                message = str(exc)
        else:
            message = ""
        show = getattr(self._panel, "set_agent_notice", None)
        if callable(show):
            show(
                message or f"生成候选 {asset_id or '(未知)'} 已在生成素材箱中；请先预览，再选择导入媒体池。",
                warning=bool(message),
            )

    def _on_import_generated_asset(self, run_id: str) -> None:
        asset_id = self._generated_asset_id_from_run(run_id)
        starter = getattr(self._bridge, "start_ai_generated_materialization", None)
        if asset_id and callable(starter):
            try:
                ok, message, job_id = starter(
                    asset_id,
                    on_progress=lambda _asset_id, fraction, text: (
                        self._generated_materialization_progress.emit(run_id, fraction, text)
                    ),
                    on_completed=lambda _asset_id, path, text: (
                        self._generated_materialization_completed.emit(
                            run_id, _asset_id, path, text,
                        )
                    ),
                    on_failed=lambda _asset_id, text: (
                        self._generated_materialization_failed.emit(run_id, text)
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("start generated materialization failed")
                ok, message, job_id = False, str(exc), None
            if ok and job_id:
                self._materialization_job_ids[run_id] = job_id
                job = self._materialization_job(run_id)
                if self.runtime is not None and job is not None:
                    from src.features.ai_harness.invocation_models import JobRef
                    self.runtime.attach_job(
                        run_id,
                        JobRef(
                            job_id=job.job_id,
                            kind=job.kind.value,
                            label=job.label,
                            status=job.status.value,
                            progress=job.progress,
                        ),
                    )
                self._show_generated_notice(message or "已加入后台生成媒体下载队列。")
                self._refresh_run_drawer()
                self._refresh_composer_footer()
                return
            # 异步接缝存在但暂时不可用时，继续走同步兼容路径。
            if message and message != "AI 生成后台物化宿主尚未初始化":
                self._show_generated_notice(message, warning=True)
                return

        importer = getattr(self._bridge, "import_ai_generated_asset", None)
        if not asset_id or not callable(importer):
            message = "找不到可导入的生成候选。"
            warning = True
        else:
            try:
                ok, message = importer(asset_id)
                warning = not ok
            except Exception as exc:  # noqa: BLE001
                logger.exception("import generated asset failed")
                ok, message, warning = False, str(exc), True
        show = getattr(self._panel, "set_agent_notice", None)
        if callable(show):
            show(message or ("已导入媒体池。" if not warning else "导入失败。"), warning=warning)

    def _show_generated_notice(self, message: str, *, warning: bool = False) -> None:
        show = getattr(self._panel, "set_agent_notice", None)
        if callable(show):
            show(message, warning=warning)

    def _generation_job_registry(self) -> Any:
        service = getattr(self.generation_adapter, "_svc", None)
        return getattr(service, "job_registry", None)

    def _materialization_job(self, run_id: str) -> Any:
        job_id = self._materialization_job_ids.get(run_id, "")
        registry = self._generation_job_registry()
        return registry.get(job_id) if registry is not None and job_id else None

    def _update_materialization_runtime_job(self, run_id: str) -> None:
        if self.runtime is None:
            return
        job = self._materialization_job(run_id)
        if job is None:
            return
        self.runtime.update_job_progress(
            run_id, job.job_id, job.status.value, job.progress,
        )

    def _on_generated_materialization_progress(
        self, run_id: str, fraction: object, message: str,
    ) -> None:
        self._update_materialization_runtime_job(run_id)
        self._refresh_run_drawer()
        self._refresh_composer_footer()

    def _on_generated_materialization_completed(
        self, run_id: str, asset_id: str, path: str, message: str,
    ) -> None:
        importer = getattr(self._bridge, "import_ai_generated_asset_from_path", None)
        if callable(importer):
            try:
                ok, import_message = importer(asset_id, path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("import materialized generated asset failed")
                ok, import_message = False, str(exc)
        else:
            ok, import_message = False, "当前宿主不支持导入已物化生成媒体"
        self._update_materialization_runtime_job(run_id)
        self._show_generated_notice(
            import_message or message or "生成媒体已准备完成。", warning=not ok,
        )
        # B7: stage artifact + 完成 V2 AgentRun。
        self._v2_stage_media_artifact(run_id)
        self._v2_complete_media_generation(run_id, success=ok, reason=import_message)
        self._refresh_run_drawer()
        self._refresh_composer_footer()

    def _on_generated_materialization_failed(self, run_id: str, message: str) -> None:
        self._update_materialization_runtime_job(run_id)
        self._show_generated_notice(message or "生成媒体下载失败。", warning=True)
        # B7: 标记 V2 AgentRun 为失败。
        self._v2_complete_media_generation(run_id, success=False, reason=message)
        self._refresh_run_drawer()
        self._refresh_composer_footer()

    def _on_place_generated_media(self, run_id: str) -> None:
        """为已导入的生成候选提出 add_clip plan，并走统一审批/CommandStack。"""
        show = getattr(self._panel, "set_agent_notice", None)

        def _notice(message: str, *, warning: bool = False) -> None:
            if callable(show):
                show(message, warning=warning)
            else:
                logger.info("AI generated placement%s: %s", " (warning)" if warning else "", message)

        if run_id in getattr(self, "_generated_placement_done", set()):
            _notice("该生成候选已经放入时间线。")
            return
        asset_id = self._generated_asset_id_from_run(run_id)
        get_media_id = getattr(self._bridge, "get_ai_generated_media_asset_id", None)
        if not asset_id or not callable(get_media_id):
            _notice("找不到生成候选或媒体池导入记录。", warning=True)
            return
        try:
            media_asset_id = get_media_id(asset_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("read generated media asset id failed")
            _notice(f"无法读取媒体池素材：{exc}", warning=True)
            return
        if not media_asset_id:
            _notice("请先将生成候选导入媒体池，再提出时间线放置方案。", warning=True)
            return

        state = self._bridge.get_project_sequences()
        timeline_id = self._bridge.get_active_timeline_id()
        if state is None or not timeline_id:
            _notice("当前没有活动时间线，无法提出放置方案。", warning=True)
            return
        sequence = getattr(state, "sequences", {}).get(timeline_id)
        project = getattr(state, "project", None)
        asset = project.get_asset_by_id(media_asset_id) if project is not None else None
        if sequence is None or asset is None:
            _notice("找不到活动时间线或已导入的生成素材。", warning=True)
            return

        media_type = str(getattr(asset, "media_type", "") or "")
        expected_track_type = "audio" if media_type == "audio" else "video"
        track = next(
            (candidate for candidate in sequence.all_tracks
             if getattr(candidate, "track_type", "") == expected_track_type),
            None,
        )
        if track is None:
            _notice(f"当前时间线没有可用的{expected_track_type}轨道。", warning=True)
            return

        source_duration = self._asset_frame_duration(asset, sequence)
        placement_start, placement_label = self._generated_placement_start(
            run_id, sequence, track,
        )
        if placement_start is None:
            _notice("无法读取指定的时间线放置位置。", warning=True)
            return
        from src.features.edit.ai_editing.models import (
            EditOperation, EditPlan, OperationKind, PlanTarget,
        )
        from src.features.edit.ai_editing.editorial_models import FrameRange

        plan = EditPlan.create(
            target=PlanTarget(
                project_id=str(getattr(project, "project_id", "") or ""),
                timeline_id=str(timeline_id),
            ),
            base_revision=int(getattr(sequence, "revision", 0)),
            operations=(EditOperation(
                op_id=f"generated-placement-{asset_id}",
                kind=OperationKind.ADD_CLIP,
                params={
                    "asset_id": str(media_asset_id),
                    "track_id": str(track.track_id),
                    "source": FrameRange(start=0, duration=source_duration).to_dict(),
                    "timeline": FrameRange(
                        start=placement_start,
                        duration=source_duration,
                    ).to_dict(),
                },
                intent={
                    "reason": "用户确认后将生成素材放入时间线",
                    "generated_asset_id": asset_id,
                    "placement": placement_label,
                },
            ),),
            title="放置 AI 生成素材",
            rationale="生成候选已显式导入媒体池，等待用户审阅 diff 后放入活动时间线",
        )
        self._service.plan_repository.store(plan)
        preview = self._service.preview_plan(plan, state)
        if preview is None or preview.has_errors or preview.compiled is None:
            _notice("生成素材放置方案预览未通过，请检查时间线状态。", warning=True)
            return
        grant = self._bridge.request_plan_approval(plan, preview)
        if grant is None:
            _notice("已取消生成素材的时间线放置。")
            return
        approval_service = self._bridge.approval_service()
        if approval_service is None:
            _notice("授权服务不可用，未修改时间线。", warning=True)
            return
        try:
            from src.features.edit.ai_editing.plan_compiler import compile_plan
            from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

            approval_service.consume(
                grant.grant_id,
                _expected_commit_scope(plan, diff_hash=preview.diff_hash),
            )
            compiled = compile_plan(plan)
            new_timeline = self._bridge.commit_ai_command(
                str(timeline_id), compiled.macro, undo_label=compiled.undo_label,
            )
            if new_timeline is None:
                _notice("时间线提交失败，未完成放置。", warning=True)
                return
            self._bridge.refresh_after_ai_commit(new_timeline)
            if not hasattr(self, "_generated_placement_done"):
                self._generated_placement_done: set[str] = set()
            self._generated_placement_done.add(run_id)
            _notice("生成素材已按审批方案放入时间线，可用一次 Ctrl+Z 撤销。")
        except Exception as exc:  # noqa: BLE001
            logger.exception("generated media placement failed")
            _notice(f"时间线放置失败：{exc}", warning=True)

    @staticmethod
    def _asset_frame_duration(asset: Any, sequence: Any) -> int:
        total_frames = getattr(asset, "total_frames", None)
        try:
            if total_frames is not None and int(total_frames) > 0:
                return int(total_frames)
        except (TypeError, ValueError):
            pass
        duration = getattr(asset, "duration_seconds", None)
        fps = getattr(sequence, "fps", 24.0)
        try:
            fps_value = float(fps.to_fps()) if hasattr(fps, "to_fps") else float(fps)
            if duration is not None and fps_value > 0:
                return max(1, int(round(float(duration) * fps_value)))
        except (TypeError, ValueError):
            pass
        return 1

    def _generated_placement_start(
        self, run_id: str, sequence: Any, track: Any,
    ) -> tuple[int | None, str]:
        """根据原始生成 invocation 的 typed inputs 计算时间线起点。"""
        placement = "end"
        seconds: float | None = None
        if self.runtime is not None:
            invocation = self.runtime.get_invocation(run_id)
            inputs = dict(getattr(invocation, "inputs", {}) or {}) if invocation else {}
            placement = str(inputs.get("placement") or "end")
            if placement == "seconds":
                try:
                    seconds = max(0.0, float(inputs.get("placement_seconds", 0.0)))
                except (TypeError, ValueError):
                    return None, "seconds"
        if placement == "start":
            return 0, "start"
        if placement == "playhead":
            getter = getattr(self._bridge, "get_ai_playhead_frame", None)
            if not callable(getter):
                return None, "playhead"
            try:
                frame = getter()
            except Exception:  # noqa: BLE001
                return None, "playhead"
            return (max(0, int(frame)), "playhead") if frame is not None else (None, "playhead")
        if placement == "seconds":
            if seconds is None:
                return None, "seconds"
            fps = getattr(sequence, "fps", 24.0)
            try:
                fps_value = float(fps.to_fps()) if hasattr(fps, "to_fps") else float(fps)
            except (TypeError, ValueError):
                fps_value = 24.0
            return max(0, int(round(seconds * fps_value))), f"{seconds:g}s"
        return max(0, int(getattr(track, "duration_frames", 0))), "end"

    def _on_result_action_retry(self, run_id: str) -> None:
        """重试 run：从原 invocation 反查 skill_id + inputs，重走 _execute_skill_route。

        新 run 产生新 run_id + 新结果卡（旧卡保留）。runtime/invocation 缺失时降级（不崩）。
        """
        if self.runtime is None or self.router is None:
            return
        try:
            record = self.runtime.get_run(run_id)
            if record is None or record.status not in {"failed", "cancelled"}:
                logger.debug("retry ignored for non-retryable run %s", run_id)
                return
            inv = self.runtime.get_invocation(run_id)
            if inv is None:
                logger.debug("retry: invocation not found for run %s", run_id)
                return
            # 重新构造 RouteResult + scope，走标准 skill 执行路径（_execute_skill_route
            # 会调 invoke_compiled 真正执行，比 runtime.retry() 更完整——retry() 只
            # 创建 DRAFT 不执行）。
            from src.features.ai_harness.invocation_router import RouteResult
            scope = inv.scope
            result = RouteResult(
                intent="retry", matched=True,
                skill_id=inv.skill_id,
                extracted_inputs=dict(getattr(inv, "inputs", {}) or {}),
            )
            self._execute_skill_route(
                result,
                scope,
                parent_session_id=getattr(inv, "parent_session_id", ""),
                inputs_override=dict(getattr(inv, "inputs", {}) or {}),
            )
        except Exception:  # noqa: BLE001
            logger.debug("retry action failed for run %s", run_id, exc_info=True)

    def _on_result_action_adjust_params(self, run_id: str) -> None:
        """调整参数：从原 invocation 反查 skill_id + inputs，渲染预填表单卡。

        用户改完点开始 → _on_skill_card_start → 新 run。
        runtime/catalog/invocation 缺失时降级（不崩）。
        """
        if self.runtime is None or self.catalog is None:
            return
        try:
            inv = self.runtime.get_invocation(run_id)
            if inv is None:
                logger.debug("adjust_params: invocation not found for run %s", run_id)
                return
            card_view = self.catalog.card(inv.skill_id)
            if card_view is None:
                logger.debug("adjust_params: card not found for skill %s", inv.skill_id)
                return
            self._panel.set_skill_form(
                card_view, dict(getattr(inv, "inputs", {}) or {}),
            )
        except Exception:  # noqa: BLE001
            logger.debug("adjust_params failed for run %s", run_id, exc_info=True)

    def _on_show_workflow_requested(self) -> None:
        """右键触发：显示 AI Workflow 弹窗（P5：dock → 弹窗）。

        通过 panel 的顶层窗口（MainWindow）调 ``_show_ai_workflow_dialog``。
        MainWindow 未装配该方法（如测试环境）或 flag 关时静默 no-op。
        """
        main_window = self._panel.window()
        show = getattr(main_window, "_show_ai_workflow_dialog", None)
        if show is not None:
            try:
                show()
            except Exception:  # noqa: BLE001
                logger.debug("show ai workflow dialog failed", exc_info=True)

    def _on_new_session(self) -> None:
        """新建会话（不删历史；红线 §10.2 P0.4）。store 未注入时 no-op。

        关键修复：新建会话后**清空内存对话历史**（``service._messages``）。
        原先只切 catalog active 但不清内存，导致内存历史与持久化目录脱节——
        用户以为"新建=清空"但 LLM 仍看到旧对话，体感混乱。
        """
        if self.session_store is None:
            return
        try:
            self.session_store.create_session()
            # P1.1：切会话时清空澄清往返状态（新会话不应继承旧 pending）
            self._pending_route = None
            self._pending_route_original_text = ""
            # 清空内存对话历史，使新会话从空白开始（与 catalog active 切换对齐）。
            reset = getattr(self._service, "reset_chat_messages", None)
            if callable(reset):
                reset()
            self._chat_context_tokens = 0
            self._chat_context_compressed = False
            self._last_chat_compression_signature = None
            self._refresh_history_menu()
            self._refresh_composer_footer()
        except Exception:  # noqa: BLE001
            logger.debug("new session create failed", exc_info=True)

    def _on_show_history(self) -> None:
        """刷新历史二级菜单；菜单项由用户直接点击切换。"""
        self._refresh_history_menu()

    def _on_clear_history(self) -> None:
        """清空本机/当前工程的 AI 对话历史，并同步清空内存上下文。"""
        if self.session_store is None:
            return
        try:
            clear = getattr(self.session_store, "clear_history", None)
            if callable(clear):
                clear()
            self.run_trace_store.clear()
            self._context_spill_store.clear()
            self._trace_run_sessions.clear()
            reset = getattr(self._service, "reset_chat_messages", None)
            if callable(reset):
                reset()
            clear_stream = getattr(self._panel, "clear_streaming_reply", None)
            if callable(clear_stream):
                clear_stream()
            set_title = getattr(self._panel, "set_conversation_title", None)
            if callable(set_title):
                set_title("")
            self._streaming_reply_text = ""
            self._refresh_history_menu()
            self._refresh_composer_footer()
        except Exception:  # noqa: BLE001
            logger.debug("clear assistant history failed", exc_info=True)

    def _on_analyze_context_requested(self) -> None:
        """从右键菜单进入现有的 propose → confirm → execute 分析流程。"""
        controller = self._analysis_controller
        if controller is None:
            setter = getattr(self._panel, "set_clarification", None)
            if callable(setter):
                setter("分析服务尚未初始化，请先打开一个剪辑工程。", "analyze_scope")
            return
        scope = self._build_chat_scope()
        asset_ids = tuple(getattr(scope, "selected_asset_ids", ()) or ())
        if not asset_ids:
            setter = getattr(self._panel, "set_clarification", None)
            if callable(setter):
                setter("请先在媒体池选择要分析的素材。", "analyze_scope")
            return
        try:
            from src.features.edit.ai_editing.analysis_request import AnalysisRecipe

            controller.request_analysis_draft(
                asset_ids,
                recipe=AnalysisRecipe.FAST_LOCAL,
                requested_by="assistant_menu",
                title="分析当前素材",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("assistant menu analysis request failed")
            setter = getattr(self._panel, "set_clarification", None)
            if callable(setter):
                setter(f"无法创建分析计划：{exc}", "analyze_error")

    def _on_conversation_title_changed(self, title: str) -> None:
        """保存当前 active 会话标题，并立即刷新历史菜单。"""
        if self.session_store is None:
            return
        normalized = " ".join(str(title or "").split())
        if not normalized:
            return
        try:
            active = self.session_store.get_active_session()
            setter = getattr(self.session_store, "set_session_title", None)
            if callable(setter) and active is not None:
                setter(active.session_id, normalized)
            set_title = getattr(self._panel, "set_conversation_title", None)
            if callable(set_title):
                set_title(normalized, is_custom=True)
            self._refresh_history_menu()
        except Exception:  # noqa: BLE001
            logger.debug("conversation title update failed", exc_info=True)

    def _on_retry_requested(self) -> None:
        """错误页重试最近一次请求，保持相同文本并重新走完整路由。"""
        text = self._last_request_text.strip()
        if not text:
            return
        try:
            from src.features.edit.ai_editing.assistant_events import AssistantRequest
            self._on_request_submitted(AssistantRequest(
                request_id=str(uuid.uuid4()),
                text=text,
                profile_id=getattr(self._service, "active_profile_id", "") or "",
            ))
        except Exception:  # noqa: BLE001
            logger.debug("retry last assistant request failed", exc_info=True)

    def _on_draft_changed(self, text: str) -> None:
        """把当前草稿纳入顶部上下文 token 预估，但不写压缩 checkpoint。"""
        try:
            self._refresh_composer_footer(draft_text=str(text or ""))
        except Exception:  # noqa: BLE001
            logger.debug("refresh draft token usage failed", exc_info=True)

    def _refresh_history_menu(self) -> None:
        """把当前本地缓存/工程会话推送到面板历史菜单。"""
        if self.session_store is None:
            return
        try:
            sessions = self.session_store.list_sessions(include_archived=False)
            setter = getattr(self._panel, "set_history_sessions", None)
            if callable(setter):
                catalog = getattr(self.session_store, "catalog", None)
                setter(
                    sessions,
                    active_session_id=str(
                        getattr(catalog, "active_session_id", "") or ""
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.debug("refresh history menu failed", exc_info=True)

    def _on_history_session_requested(self, session_id: str) -> None:
        """历史二级菜单点击 → 切换 active，并恢复该会话的多轮消息。"""
        self._switch_to_session(session_id)

    def _switch_to_session(self, session_id: str) -> bool:
        """切换 active 会话（归档会话拒绝；返回是否成功）。

        关键修复：切换后**重建内存对话历史**——先清空，再从目标 session 的
        MessageSummary 回灌。原先只切 catalog active 不动内存历史，导致切换
        会话后 LLM 仍看到旧会话内容，与 UI 显示脱节。
        """
        if self.session_store is None or not session_id:
            return False
        try:
            ok = self.session_store.set_active_session(session_id)
            if not ok:
                return False
            # 重建内存历史：清空 + 从目标 session 回灌。
            reset = getattr(self._service, "reset_chat_messages", None)
            restore = getattr(self._service, "restore_chat_messages", None)
            entry = self.session_store.get_session(session_id)
            if callable(reset):
                reset()
            if callable(restore) and entry is not None:
                from src.features.edit.ai_editing.assistant_events import (
                    AssistantMessage,
                )
                restored = tuple(
                    AssistantMessage(
                        message_id=m.message_id,
                        role=m.role,
                        text=m.text,
                    )
                    for m in entry.messages
                )
                restore(restored)
            # 切会话时清空澄清往返状态。
            self._pending_route = None
            self._pending_route_original_text = ""
            self._chat_context_tokens = 0
            self._chat_context_compressed = False
            self._last_chat_compression_signature = None
            self._refresh_history_menu()
            self._refresh_composer_footer()
            return True
        except Exception:  # noqa: BLE001
            logger.debug("switch session failed", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # P0.5：上下文压缩
    # ------------------------------------------------------------------ #

    def _on_context_compress_signal(self) -> None:
        """用户点"压缩"按钮 → 构建当前 bundle → 调 _on_context_compress。

        checkpoint 持久化是可选的；即使 store 未注入，也应完成本次内存压缩。
        bundle 构建复用 _on_inspect_context 的 sources 逻辑。
        """
        try:
            from src.features.edit.ai_editing.context_service import (
                ContextBuildRequest, ContextService,
            )
            sources = self._build_inspector_sources()
            request = ContextBuildRequest(
                sources=sources,
                budget=self._context_budget(),
            )
            bundle = ContextService().build_bundle(request)
            self._active_context_bundle = bundle
            self._on_context_compress(bundle)
        except Exception:  # noqa: BLE001
            logger.debug("context compress (from signal) failed", exc_info=True)

    def _on_context_compress(self, bundle: Any) -> None:
        """压缩 ContextBundle → 写 CompressionCheckpoint → 刷新 footer（P0.5）。

        红线：压缩只生成新 RECENT_SUMMARY 摘要 block，原始 evidence/artifact 不丢
        （checkpoint 保留 refs）；不调 LLM（用规则摘要）。
        """
        if bundle is None:
            return
        try:
            from src.features.edit.ai_editing.context_service import ContextService
            new_bundle, cp = ContextService().compress(bundle)
        except Exception:  # noqa: BLE001
            logger.debug("context compress failed", exc_info=True)
            return
        self._active_context_bundle = new_bundle
        set_bundle = getattr(self._panel, "set_context_bundle", None)
        if callable(set_bundle):
            try:
                set_bundle(new_bundle)
            except Exception:  # noqa: BLE001
                logger.debug("refresh compressed context inspector failed", exc_info=True)
        # 写 checkpoint（store 未注入时跳过，但仍刷新 footer）
        if self.session_store is not None:
            try:
                self.session_store.add_compression_checkpoint(cp)
            except Exception:  # noqa: BLE001
                logger.debug("checkpoint persist failed", exc_info=True)
        # 刷新 footer（context_compressed=True）
        try:
            from src.features.edit.ui.ai_assistant_composer import ComposerFooterState
            state = ComposerFooterState(
                permission=self._current_permission,
                mode=self._current_assistant_mode,
                context_used=getattr(new_bundle, "total_tokens", 0),
                context_limit=getattr(new_bundle.budget, "limit", 200_000),
                context_compressed=True,
            )
            self._panel.set_composer_footer(state)
        except Exception:  # noqa: BLE001
            pass

    def _on_brief_approved(self, brief: Any) -> None:
        """用户在 Brief 编辑器点"批准 Brief" → save brief → 规划 variants。

        M1.3：这是从 AWAITING_BRIEF_APPROVAL 继续的唯一路径。editor emit 的是完整
        bumped EditorialBrief；service.apply_user_brief 直接 save（用户是真相源）。
        save 成功后才 plan_variants。

        规划经 ``_PlanWorker`` 后台线程执行（LLM 是网络调用，同步会阻塞 UI）。
        """
        brief_id = self._service.active_brief_id or "default"
        try:
            self._service.apply_user_brief(brief_id, brief)
        except Exception as exc:
            self._service.record_error(f"Brief 保存失败：{exc}")
            return
        # 规划 variants（需要 semantic index + config，都从 bridge 取）
        index = self._bridge.get_semantic_index()
        config = self._service.config  # AssistantConfig
        self._start_plan(index, config)

    def start_chat_run(
        self, *, skill_id: str = "", brief_summary: str = "",
        user_text: str = "", evidence_refs: tuple[str, ...] = (),
        project_id: str = "default",
    ) -> str:
        """构造一次 chat invocation 并交给 runtime.start（plan §0.2 / §10.2 P0.2）。

        chat 路径的"生成方案"动作经统一 HarnessRuntime 产生 RunRecord，使
        Run Drawer / 取消贯通 / 会话历史等 P0 能力可见。``user_text`` 在构造
        invocation 时经 ``sanitize_for_storage`` 脱敏（绝对路径 / secret 被替换），
        不落盘原始敏感输入。

        Args:
            skill_id: router 解析出的目标 Skill（空表示纯文本请求）。
            brief_summary: 已批准 brief 的脱敏摘要（进 invocation.inputs）。
            user_text: 用户原始输入（脱敏后存 invocation.user_text）。
            evidence_refs: 引用的 evidence ID 列表（进 invocation.inputs）。
            project_id: 工程绑定（InvocationScope.project_id 必填）。

        Returns:
            run_id（runtime 未装配时返回空串，chat 仍走旧 plan 路径）。
        """
        if self.runtime is None:
            return ""
        inv = Invocation(
            source=InvocationSource.CHAT,
            skill_id=skill_id,
            scope=InvocationScope(project_id=project_id or "default"),
            policy=RunPolicy(),
            inputs={
                "brief_summary": brief_summary,
                "evidence_refs": tuple(evidence_refs),
            },
            user_text=user_text,
        )
        run_id = self.runtime.start(inv)
        self._active_chat_run_id = run_id
        # P0.6：把 run_id + runtime 注入 service，使其 cancel() 能透传 cancel token。
        try:
            self._service.set_active_run_id(run_id, runtime=self.runtime)
        except Exception:  # noqa: BLE001
            logger.warning("set_active_run_id failed; run cancel may not propagate", exc_info=True)
        return run_id

    def _start_plan(self, index: Any, config: AssistantConfig) -> None:
        """在后台线程启动规划；完成后切回主线程刷新 UI。

        测试模式（``TOPOS_TEST_MODE``）下同步执行，便于现有断言式测试。

        P0.2：若 runtime 已装配，规划前后包 runtime 生命周期（mark_running →
        finish）；plan_variants 内部逻辑不变，runtime 只做可观测性包装。
        """
        import os
        if os.environ.get("TOPOS_TEST_MODE"):
            # 测试模式：同步执行，保持与旧测试契约一致
            self._wrap_plan_with_runtime(lambda: self._service.plan_variants(index, config))
            return
        # 取消未完成的旧规划
        if self._plan_worker is not None and self._plan_worker.isRunning():
            self._plan_worker.cancel_requested = True
            self._plan_worker.quit()
            self._plan_worker.wait(2000)

        self._service._set_state(AssistantState.PLANNING)  # noqa: SLF001  立即给 UI busy 态

        # P0.2：runtime 已装配时把 run 标 RUNNING（worker 完成后由
        # _on_plan_finished / _on_plan_failed 调 finish）。
        if self.runtime is not None and self._active_chat_run_id:
            try:
                if not self.runtime.is_cancelled(self._active_chat_run_id):
                    self.runtime.mark_running(self._active_chat_run_id)
            except Exception:  # noqa: BLE001
                pass

        worker = _PlanWorker(
            self._service, index, config,
            state_gate=self._plan_state_gate, parent=self,
        )
        worker.finished_plan.connect(
            lambda w=worker: self._on_plan_finished(w)
        )
        worker.failed.connect(
            lambda error, w=worker: self._on_plan_failed(error, w)
        )
        worker.cancelled.connect(
            lambda w=worker: self._on_plan_cancelled(w)
        )
        # AUD-2005：规划 worker 持有 service/config/index 引用，run() 结束后
        # 经 deleteLater 释放，不再随 controller 存活。
        worker.finished.connect(worker.deleteLater)
        self._plan_worker = worker
        worker.start()

    def _wrap_plan_with_runtime(self, plan_fn: Any) -> None:
        """在 runtime 生命周期里执行一次 plan_variants（P0.2）。

        runtime 已装配 + 有 active chat run 时：mark_running → 执行 →
        finish(success)。失败时 finish(success=False)。runtime 未装配则直接执行
        （向后兼容旧测试 / 未装配场景）。
        """
        if self.runtime is None or not self._active_chat_run_id:
            plan_fn()
            return
        run_id = self._active_chat_run_id
        try:
            if not self.runtime.is_cancelled(run_id):
                self.runtime.mark_running(run_id)
            plan_fn()
            # 终态：cancel 优先于 success（协作取消已发生）
            if self.runtime.is_cancelled(run_id):
                self.runtime.finish(run_id, success=False)
            else:
                self.runtime.finish(run_id, success=True)
        except Exception:
            try:
                self.runtime.finish(run_id, success=False)
            except Exception:  # noqa: BLE001
                pass
            raise

    def _on_plan_finished(self, worker: _PlanWorker | None = None) -> None:
        """规划完成（主线程）：触发一次状态回填刷新 UI。"""
        if worker is not None and worker is not self._plan_worker:
            return
        self._plan_worker = None
        # P0.2：runtime 已装配时把 active chat run 标成功（协作取消优先于 success）。
        if self.runtime is not None and self._active_chat_run_id:
            try:
                if self.runtime.is_cancelled(self._active_chat_run_id):
                    self.runtime.finish(self._active_chat_run_id, success=False)
                    # P0.4：把取消结果写进会话目录。
                    self._record_run_finalization(
                        self._active_chat_run_id, summary="规划已取消",
                    )
                else:
                    self.runtime.finish(self._active_chat_run_id, success=True)
                    # P0.4：把成功结果写进会话目录。
                    self._record_run_finalization(
                        self._active_chat_run_id, summary="规划完成",
                    )
            except Exception:  # noqa: BLE001
                pass
        # service 已在 worker 内更新到 VARIANTS_READY；这里主动触发一次回填，
        # 因为 worker 期间挂起了 on_state_changed。
        self._refresh_state()

    def _on_plan_failed(
        self, error: str, worker: _PlanWorker | None = None,
    ) -> None:
        """规划失败（主线程）：刷新 UI 显示 FAILED。"""
        if worker is not None and worker is not self._plan_worker:
            return
        self._plan_worker = None
        # P0.2：worker 失败时把 active chat run 标 FAILED（若 runtime 已装配）。
        if self.runtime is not None and self._active_chat_run_id:
            try:
                self.runtime.finish(self._active_chat_run_id, success=False)
                # P0.4：把失败结果写进会话目录。
                self._record_run_finalization(
                    self._active_chat_run_id, summary=f"规划失败：{error}",
                )
            except Exception:  # noqa: BLE001
                pass
        self._refresh_state()

    def _on_plan_cancelled(self, worker: _PlanWorker | None = None) -> None:
        """规划被取消（主线程，P1 最后一公里）：把 run 标 CANCELLED。

        worker 在 plan_variants 返回后发现 ``cancel_requested=True``，emit
        ``cancelled``（丢弃 plan 结果）。runtime.finish(success=False) + run 已
        cancel_requested=True → 终态 CANCELLED（runtime.py:333 协作取消语义）。
        """
        if worker is not None and worker is not self._plan_worker:
            return
        self._plan_worker = None
        if self.runtime is not None and self._active_chat_run_id:
            try:
                # 确保 cancel_requested 已置位（用户点取消时 runtime.cancel 已调）
                self.runtime.finish(self._active_chat_run_id, success=False)
                self._record_run_finalization(
                    self._active_chat_run_id, summary="规划已取消",
                )
            except Exception:  # noqa: BLE001
                pass
        self._refresh_state()

    def _refresh_state(self) -> None:
        """重新触发一次状态回填（worker 后恢复 UI 同步）。"""
        # 触发 service 重新构造 snapshot 并回调（worker 期间挂起了回调）。
        self._service._emit_state()  # noqa: SLF001  紧密协作层
        # P0.3：worker 完成/失败后顺便刷新 footer（has_active_run 状态变化）。
        self._refresh_composer_footer()

    # ------------------------------------------------------------------ #
    # P0.3：runtime events → Composer footer / Run Drawer 聚合
    # ------------------------------------------------------------------ #

    def _on_run_event(self, event: Any) -> None:
        """runtime 订阅者回调（可能在 worker 线程；只 emit signal 不碰 Qt）。"""
        try:
            run_id = str(getattr(event, "run_id", "") or "")
            session_id = ""
            if self.runtime is not None and run_id:
                record = self.runtime.get_run(run_id)
                session_id = str(getattr(record, "parent_session_id", "") or "") if record else ""
            session_id = self._trace_run_sessions.get(run_id, session_id)
            if not session_id:
                session_id = self._trace_session_id()
                if run_id:
                    self._trace_run_sessions[run_id] = session_id
        except Exception:  # noqa: BLE001
            run_id = str(getattr(event, "run_id", "") or "")
            session_id = "default"
        try:
            self.run_trace_store.append_runtime_event(
                event, session_id=session_id or self._trace_session_id(),
            )
        except Exception:  # noqa: BLE001
            logger.debug("append runtime event to run trace failed", exc_info=True)
        try:
            kind = getattr(event, "kind", "")
            from src.features.ai_harness.invocation_models import RunEventKind
            if kind == RunEventKind.RUN_STATE_CHANGED:
                if run_id == self._active_chat_run_id or run_id in self._autonomous_runs:
                    self._run_state_changed.emit(run_id)
        except Exception:  # noqa: BLE001
            logger.debug("run state changed signal dispatch failed", exc_info=True)

    def _on_run_state_changed_main(self, run_id: str) -> None:
        """主线程槽：run 状态变化后刷新 drawer + footer。"""
        self._refresh_run_drawer()
        self._refresh_composer_footer()
        self._try_autonomous_apply(run_id)

    def _autonomous_mode_enabled(self) -> bool:
        """Return the live host opt-in without reaching into MainWindow state."""
        checker = getattr(self._bridge, "is_autonomous_mode_enabled", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - a broken policy must fail closed
            return False

    def _on_autonomous_plan_observed(self, run_id: str, plan: Any) -> None:
        """Receive a plan reference; never commit from the observer callback."""
        if not run_id or plan is None:
            return
        run_id = str(run_id)
        if run_id not in self._autonomous_runs and not self._autonomous_mode_enabled():
            # Manual/propose runs may also produce plans, but they must not become
            # restartable autonomous checkpoints.
            return
        self._autonomous_plans[run_id] = plan
        goal_id = self._autonomous_goal_ids.get(run_id)
        if self._autonomous_replan_guard and self._autonomous_replan_goal_id:
            goal_id = self._autonomous_replan_goal_id
            self._autonomous_goal_ids[run_id] = goal_id
        goal_id = goal_id or run_id
        target = getattr(plan, "target", None)
        project_id = str(getattr(target, "project_id", "") or "")
        timeline_id = str(getattr(target, "timeline_id", "") or "")
        plan_id = str(getattr(plan, "plan_id", "") or "")
        if plan_id and project_id and timeline_id:
            mission = self._autonomous_missions.start(run_id, plan_id)
            invocation = self.runtime.get_invocation(run_id) if self.runtime is not None else None
            skill_id = str(getattr(invocation, "skill_id", "") or "")
            if goal_id not in self._autonomous_goals:
                self._autonomous_goals[goal_id] = GoalSpec(
                    goal_id=goal_id,
                    project_id=project_id,
                    timeline_id=timeline_id,
                    objective_ref=f"skill:{skill_id}" if skill_id else f"plan:{plan_id}",
                    constraint_refs=(f"plan:{plan_id}",),
                )
            if not mission.completed_stages:
                self._autonomous_missions.record(
                    run_id,
                    MissionStage.PLAN,
                    passed=True,
                    reason="计划已生成并绑定当前工程范围",
                )
            self._autonomous_checkpoints.upsert(AutonomousCheckpoint(
                run_id=run_id,
                plan_id=plan_id,
                project_id=project_id,
                timeline_id=timeline_id,
                base_revision=int(getattr(plan, "base_revision", -1)),
                asset_ids=self._autonomous_plan_asset_ids(plan),
                goal_id=goal_id,
            ))
        # A local deterministic adapter can finish before the controller receives
        # the returned run_id.  The later call from _execute_skill_route is the
        # normal path; this call covers an already-registered run.
        if run_id in self._autonomous_runs:
            self._try_autonomous_apply(run_id)

    @staticmethod
    def _autonomous_plan_asset_ids(plan: Any) -> tuple[str, ...]:
        """Extract only typed asset IDs for checkpoint scope binding."""
        asset_ids: set[str] = set()
        for operation in tuple(getattr(plan, "operations", ()) or ()):
            params = getattr(operation, "params", {}) or {}
            asset_id = params.get("asset_id")
            if asset_id:
                asset_ids.add(str(asset_id))
            for value in params.get("asset_ids", ()) or ():
                if value:
                    asset_ids.add(str(value))
        return tuple(sorted(asset_ids))

    def export_autonomous_checkpoints(self) -> dict[str, Any] | None:
        """Return the bounded, sanitized autonomous recovery payload."""
        return self._autonomous_checkpoints.export_all()

    def export_autonomous_audit(self) -> dict[str, Any] | None:
        """Return bounded autonomous outcomes without prompt or path data."""
        return self._autonomous_audit.export_all()

    def export_autonomous_missions(self) -> dict[str, Any] | None:
        """Return finite mission stages and quality-gate outcomes."""
        return self._autonomous_missions.export_all()

    def export_autonomous_goals(self) -> dict[str, Any] | None:
        """Return typed goal metadata without raw user requests."""
        if not self._autonomous_goals:
            return None
        return {
            "schema_version": "topos-goal-spec/1.0",
            "goals": [spec.to_dict() for spec in self._autonomous_goals.values()],
        }

    def export_autonomous_goal_checkpoints(self) -> dict[str, Any] | None:
        """Return bounded Goal Supervisor lifecycle checkpoints."""
        return self._autonomous_goal_checkpoints.export_all()

    def restore_autonomous_checkpoints(self, data: Any) -> None:
        """Restore checkpoint references after plans/runtime are loaded."""
        self._autonomous_checkpoints.import_all(data)
        self._resume_autonomous_checkpoints()

    def restore_autonomous_audit(self, data: Any) -> None:
        """Restore valid audit rows; malformed history never blocks project load."""
        self._autonomous_audit.clear()
        self._autonomous_audit.import_all(data)

    def restore_autonomous_missions(self, data: Any) -> None:
        """Restore mission ledger; malformed rows are ignored by the store."""
        self._autonomous_missions.clear()
        self._autonomous_missions.import_all(data)

    def restore_autonomous_goals(self, data: Any) -> None:
        """Restore typed goal metadata; invalid rows never block project load."""
        self._autonomous_goals.clear()
        if not isinstance(data, dict) or data.get("schema_version") != "topos-goal-spec/1.0":
            return
        rows = data.get("goals", ())
        if not isinstance(rows, (list, tuple)):
            return
        for raw in rows[:64]:
            try:
                spec = GoalSpec.from_dict(raw)
            except (TypeError, ValueError, KeyError):
                continue
            self._autonomous_goals[spec.goal_id] = spec

    def restore_autonomous_goal_checkpoints(self, data: Any) -> None:
        """Restore goal lifecycle state before autonomous continuation."""
        self._autonomous_goal_checkpoints.import_all(data)

    def _record_autonomous_mission_outcome(
        self, run_id: str, *, code: str, applied: bool, reason: str = "",
    ) -> None:
        """Advance the four finite mission stages through their quality gates.

        ``AutonomousAgent`` remains the transaction owner.  The controller only
        translates its typed result into restart-safe stage evidence; it never
        treats a partial commit as a successful mission.
        """
        mission = self._autonomous_missions.get(run_id)
        if mission is None:
            plan = self._autonomous_plans.get(run_id)
            plan_id = str(getattr(plan, "plan_id", "") or "")
            if not plan_id:
                return
            mission = self._autonomous_missions.start(run_id, plan_id)
            self._autonomous_missions.record(
                run_id, MissionStage.PLAN, passed=True,
                reason="计划已生成并绑定当前工程范围",
            )

        if applied:
            for stage in (MissionStage.PREVIEW, MissionStage.COMMIT, MissionStage.VERIFY):
                current = self._autonomous_missions.get(run_id)
                if current is None or current.terminal:
                    return
                if stage in current.completed_stages:
                    continue
                self._autonomous_missions.record(
                    run_id, stage, passed=True,
                    reason={
                        MissionStage.PREVIEW: "当前 revision 预览通过",
                        MissionStage.COMMIT: "命令栈提交成功且 revision 前进",
                        MissionStage.VERIFY: "提交后素材范围与时间线校验通过",
                    }[stage],
                )
            return

        preview_failed = code.startswith("AUTO_PREVIEW") or code in {
            "STALE_PLAN", "AUTO_ASSET_SCOPE_MISMATCH", "PROJECT_SCOPE_MISMATCH",
            "TIMELINE_SCOPE_MISMATCH", "NO_PROJECT_STATE", "AUTO_MODE_DISABLED",
        }
        verify_failed = code in {"AUTO_VERIFY_FAILED", "AUTO_POST_VERIFY_FAILED"}
        failed_stage = MissionStage.VERIFY if verify_failed else (
            MissionStage.PREVIEW if preview_failed else MissionStage.COMMIT
        )
        stage_order = (MissionStage.PREVIEW, MissionStage.COMMIT, MissionStage.VERIFY)
        for stage in stage_order:
            current = self._autonomous_missions.get(run_id)
            if current is None or current.terminal:
                return
            if stage in current.completed_stages:
                continue
            if stage == failed_stage:
                self._autonomous_missions.record(
                    run_id, stage, passed=False,
                    reason=reason or code,
                )
                return
            self._autonomous_missions.record(
                run_id, stage, passed=True,
                reason="阶段质量门通过",
            )

    def _on_autonomous_stage_event(self, run_id: str, event: Any) -> None:
        """Apply live guarded-stage evidence to the mission ledger."""
        try:
            stage = MissionStage(str(getattr(event, "stage", "")))
        except ValueError:
            return
        if not bool(getattr(event, "passed", False)):
            return
        self._autonomous_missions.record(
            run_id,
            stage,
            passed=True,
            reason=str(getattr(event, "message", "") or getattr(event, "code", "")),
        )
        self._refresh_run_drawer()

    def _resume_autonomous_checkpoints(self) -> None:
        """Resume only matching terminal runs; never revive a provider worker."""
        pending = self._autonomous_checkpoints.list_pending()
        if not pending:
            return
        if not self._autonomous_mode_enabled():
            for checkpoint in pending:
                self._autonomous_checkpoints.mark(
                    checkpoint.run_id,
                    AutonomousCheckpointStatus.NEEDS_REPLAN,
                    "autonomous mode is disabled; fresh planning required",
                )
            self._show_autonomous_notice(
                "发现待恢复的自主剪辑，但当前未开启受限自动模式；已转为重新规划。",
                warning=True,
            )
            return
        for checkpoint in pending:
            plan = self._service.plan_repository.get_plan(checkpoint.plan_id)
            if plan is None:
                self._autonomous_checkpoints.mark(
                    checkpoint.run_id,
                    AutonomousCheckpointStatus.NEEDS_REPLAN,
                    "plan is not present in the restored plan repository",
                )
                continue
            prior_audit = self._autonomous_audit.get(checkpoint.plan_id)
            if prior_audit is not None and prior_audit.applied:
                self._autonomous_checkpoints.mark(
                    checkpoint.run_id,
                    AutonomousCheckpointStatus.APPLIED,
                    "审计记录确认该计划已提交；跳过重复恢复",
                )
                continue
            if self.runtime is None or self.runtime.get_run(checkpoint.run_id) is None:
                self._autonomous_checkpoints.mark(
                    checkpoint.run_id,
                    AutonomousCheckpointStatus.NEEDS_REPLAN,
                    "runtime record is missing; fresh planning required",
                )
                continue
            self._autonomous_runs.add(checkpoint.run_id)
            self._autonomous_plans[checkpoint.run_id] = plan
            goal_id = str(getattr(checkpoint, "goal_id", "") or checkpoint.run_id)
            self._autonomous_goal_ids[checkpoint.run_id] = goal_id
            if goal_id not in self._autonomous_goals:
                self._autonomous_goals[goal_id] = GoalSpec(
                    goal_id=goal_id,
                    project_id=checkpoint.project_id,
                    timeline_id=checkpoint.timeline_id,
                    objective_ref=f"plan:{checkpoint.plan_id}",
                    constraint_refs=(f"plan:{checkpoint.plan_id}",),
                )
            self._try_autonomous_apply(checkpoint.run_id)

    def _show_autonomous_notice(self, message: str, *, warning: bool) -> None:
        show_notice = getattr(self._panel, "set_agent_notice", None)
        if callable(show_notice):
            show_notice(message, warning=warning)

    def _mark_autonomous_recovery(
        self, run_id: str, *, applied: bool, reason: str,
    ) -> None:
        status = (
            AutonomousCheckpointStatus.APPLIED
            if applied else AutonomousCheckpointStatus.NEEDS_REPLAN
        )
        self._autonomous_checkpoints.mark(run_id, status, reason)

    def _record_autonomous_audit(
        self,
        run_id: str,
        *,
        code: str,
        applied: bool,
        plan_id: str = "",
        new_revision: int | None = None,
    ) -> None:
        """Record one privacy-safe outcome for the current autonomous attempt."""
        checkpoint = self._autonomous_checkpoints.get(run_id)
        resolved_plan_id = str(plan_id or getattr(checkpoint, "plan_id", "") or "")
        if not resolved_plan_id:
            resolved_plan_id = f"run-{str(run_id)[:32]}"
        self._autonomous_audit.record(AuditEntry(
            timestamp=time.time(),
            tool="topos.autonomous_agent",
            plan_id=resolved_plan_id,
            applied=bool(applied),
            new_revision=new_revision,
            client=f"run:{str(run_id)[:64]}",
            diagnostics_summary={str(code)[:120]: 1},
        ))

    @staticmethod
    def _autonomous_recoverable_codes() -> frozenset[str]:
        return frozenset({
            "STALE_PLAN", "AUTO_PREVIEW_EXCEPTION", "AUTO_PREVIEW_FAILED",
            "AUTO_PREVIEW_INVALID", "AUTO_PREVIEW_INCOMPLETE",
            "AUTO_PREVIEW_COMPILE_MISMATCH", "AUTO_ASSET_SCOPE_MISMATCH",
            "AUTO_POST_VERIFY_FAILED",
        })

    def _record_autonomous_goal_cycle(
        self, run_id: str, *, code: str, applied: bool, reason: str,
    ) -> None:
        """Feed an async autonomous outcome into the generic Goal Supervisor."""
        goal_id = self._autonomous_goal_ids.get(run_id, run_id)
        goal = self._autonomous_goals.get(goal_id)
        if goal is None:
            return
        needs_user_input = code in {
            "AUTO_MODE_DISABLED", "AUTO_RISK_OPERATION", "AUTO_POLICY_BLOCKED",
            "AUTO_APPROVAL_UNAVAILABLE", "AUTO_REPLAY_AMBIGUOUS",
        }
        result = GoalCycleResult(
            completed=bool(applied),
            retryable=(not applied and code in self._autonomous_recoverable_codes()),
            needs_user_input=needs_user_input,
            tool_calls=1,
            reason=str(reason or code)[:240],
        )
        started_at = None
        if self.runtime is not None:
            run_record = self.runtime.get_run(run_id)
            started_at = getattr(run_record, "started_at", None)
            if started_at is None:
                started_at = getattr(run_record, "created_at", None)
        goal_result = self._autonomous_goal_supervisor.record_cycle(
            goal, result, started_at=started_at,
        )
        if self.runtime is not None:
            self.runtime.record_diagnostic(run_id, {
                "severity": "info" if goal_result.status.value in {"running", "succeeded"} else "warning",
                "code": "AUTONOMOUS_GOAL_STATE",
                "message": goal_result.status.value,
                "goal_cycle": goal_result.checkpoint.cycle,
                "goal_tool_calls": goal_result.checkpoint.tool_calls,
            })

    def _try_autonomous_replan(self, run_id: str, code: str) -> bool:
        """Perform at most one fresh route/compile pass for a recoverable failure."""
        recoverable = self._autonomous_recoverable_codes()
        if code not in recoverable or self._autonomous_replan_attempts.get(run_id, 0) >= 1:
            return False
        if self.runtime is None or self.router is None:
            return False
        goal_id = self._autonomous_goal_ids.get(run_id, run_id)
        goal_checkpoint = self._autonomous_goal_checkpoints.load(goal_id)
        if goal_checkpoint is not None and goal_checkpoint.status != GoalStatus.RUNNING:
            return False
        invocation = self.runtime.get_invocation(run_id)
        if invocation is None or not getattr(invocation, "skill_id", ""):
            return False
        reservation = self._goal_round_driver.reserve(
            goal_id,
            run_id=run_id,
            expected_revision=(goal_checkpoint.revision if goal_checkpoint is not None else 0),
            round_number=(goal_checkpoint.cycle + 1 if goal_checkpoint is not None else 1),
        )
        if reservation is None:
            return False
        self._autonomous_replan_attempts[run_id] = 1
        try:
            from src.features.ai_harness.invocation_router import RouteResult
            result = RouteResult(
                intent="retry", matched=True,
                skill_id=invocation.skill_id,
                extracted_inputs=dict(getattr(invocation, "inputs", {}) or {}),
            )
            scope = self._build_chat_scope()
            def _run_replan() -> bool:
                self._autonomous_replan_goal_id = goal_id
                self._autonomous_replan_guard = True
                try:
                    return bool(self._execute_skill_route(
                        result,
                        scope,
                        parent_session_id=getattr(invocation, "parent_session_id", ""),
                        inputs_override=dict(getattr(invocation, "inputs", {}) or {}),
                    ))
                finally:
                    self._autonomous_replan_guard = False
                    self._autonomous_replan_goal_id = ""

            started = bool(self._goal_round_driver.execute(reservation, _run_replan))
            if started:
                self._show_autonomous_notice(
                    "原计划与当前工程状态不一致，已执行一次受限重新规划。",
                    warning=True,
                )
            return started
        except Exception:  # noqa: BLE001 - bounded recovery must fail closed
            logger.debug("autonomous replan failed for %s", run_id, exc_info=True)
            return False

    def _try_autonomous_apply(self, run_id: str) -> None:
        """Enter the autonomous write boundary at most once per active run."""
        run_id = str(run_id or "")
        if (
            not run_id
            or run_id not in self._autonomous_runs
            or run_id in self._autonomous_completed
            or run_id in self._autonomous_inflight
        ):
            return
        self._autonomous_inflight.add(run_id)
        try:
            self._try_autonomous_apply_once(run_id)
        finally:
            self._autonomous_inflight.discard(run_id)

    def _try_autonomous_apply_once(self, run_id: str) -> None:
        """Run the autonomous write boundary once for a terminal skill run."""
        run_id = str(run_id or "")
        if (
            not run_id
            or run_id not in self._autonomous_runs
            or run_id in self._autonomous_completed
        ):
            return
        if self.runtime is not None:
            record = self.runtime.get_run(run_id)
            if record is None:
                return
            from src.features.ai_harness.invocation_models import RunStatus
            if record.status not in {
                RunStatus.SUCCEEDED,
                RunStatus.SUCCEEDED_WITH_WARNINGS,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                return
            if record.status not in {RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS}:
                self._mark_autonomous_recovery(
                    run_id,
                    applied=False,
                    reason=("run cancelled" if record.status == RunStatus.CANCELLED
                            else "planning run failed; fresh planning required"),
                )
                self._record_autonomous_audit(
                    run_id,
                    code=("AUTO_RUN_CANCELLED" if record.status == RunStatus.CANCELLED
                          else "AUTO_RUN_FAILED"),
                    applied=False,
                )
                self._autonomous_completed.add(run_id)
                return
        plan = self._autonomous_plans.get(run_id)
        if plan is None:
            # No EditPlan means the run produced an artifact or an informational
            # result; it is not a timeline write candidate.
            self._mark_autonomous_recovery(
                run_id, applied=False, reason="run produced no edit plan",
            )
            self._record_autonomous_audit(
                run_id, code="AUTO_NO_EDIT_PLAN", applied=False,
            )
            self._autonomous_completed.add(run_id)
            return

        try:
            from src.features.edit.ai_editing.autonomous_agent import (
                AutonomousAgent,
                AutonomousAgentConfig,
            )
            from src.features.edit.ai_editing.plan_compiler import compile_plan

            agent = AutonomousAgent(
                self._bridge,
                preview_plan=self._service.preview_plan,
                compile_plan=compile_plan,
                config=AutonomousAgentConfig(enabled=self._autonomous_mode_enabled()),
                on_stage=lambda event: self._on_autonomous_stage_event(run_id, event),
            )
            outcome = agent.apply(plan)
        except Exception as exc:  # noqa: BLE001 - surface a bounded diagnostic
            logger.exception("autonomous edit apply failed for run %s", run_id)
            outcome = None
            error = str(exc)
        else:
            error = ""

        if outcome is None:
            message = f"自主剪辑未提交：{error or '未知错误'}"
            self._record_autonomous_goal_cycle(
                run_id, code="AUTO_APPLY_EXCEPTION", applied=False, reason=message,
            )
            self._record_autonomous_mission_outcome(
                run_id, code="AUTO_APPLY_EXCEPTION", applied=False, reason=message,
            )
            self._mark_autonomous_recovery(
                run_id, applied=False, reason="AUTO_APPLY_EXCEPTION",
            )
            self._record_autonomous_audit(
                run_id, code="AUTO_APPLY_EXCEPTION", applied=False,
            )
            if self.runtime is not None:
                self.runtime.record_diagnostic(run_id, {
                    "severity": "error", "code": "AUTO_APPLY_EXCEPTION",
                    "message": message[:500],
                })
        else:
            message = outcome.message
            self._record_autonomous_goal_cycle(
                run_id,
                code=outcome.code,
                applied=bool(outcome.applied),
                reason=outcome.message,
            )
            self._record_autonomous_mission_outcome(
                run_id,
                code=outcome.code,
                applied=bool(outcome.applied),
                reason=outcome.message,
            )
            if self.runtime is not None:
                self.runtime.record_diagnostic(run_id, {
                    "severity": "info" if outcome.applied else "warning",
                    "code": outcome.code,
                    "message": outcome.message,
                    "plan_id": outcome.plan_id,
                    "new_revision": outcome.new_revision,
                    "rolled_back": outcome.rolled_back,
                })
            if outcome.applied:
                state = self._bridge.get_project_sequences()
                target = getattr(plan, "target", None)
                timeline_id = str(getattr(target, "timeline_id", "") or "")
                timeline = (
                    getattr(state, "sequences", {}).get(timeline_id)
                    if state is not None else None
                )
                if timeline is not None:
                    self._service.on_applied("", timeline)
                self._mark_autonomous_recovery(
                    run_id, applied=True, reason=outcome.code,
                )
                self._record_autonomous_audit(
                    run_id,
                    code=outcome.code,
                    applied=True,
                    plan_id=outcome.plan_id,
                    new_revision=outcome.new_revision,
                )
            else:
                self._mark_autonomous_recovery(
                    run_id, applied=False, reason=outcome.code,
                )
                self._record_autonomous_audit(
                    run_id,
                    code=outcome.code,
                    applied=False,
                    plan_id=outcome.plan_id,
                    new_revision=outcome.new_revision,
                )
                self._try_autonomous_replan(run_id, outcome.code)

        self._autonomous_completed.add(run_id)
        self._autonomous_plans.pop(run_id, None)
        show_notice = getattr(self._panel, "set_agent_notice", None)
        if callable(show_notice):
            show_notice(message, warning=not bool(outcome and outcome.applied))

    def _autonomous_drawer_item(self, run_id: str, record: Any, item_cls: Any) -> Any:
        """Build a transparent Run Drawer row for one autonomous attempt."""
        from src.features.ai_harness.invocation_models import RunStatus

        drawer_status = {
            RunStatus.RUNNING: "running",
            RunStatus.QUEUED: "queued",
            RunStatus.VALIDATING: "queued",
            RunStatus.WAITING_FOR_USER: "waiting_input",
            RunStatus.SUCCEEDED: "succeeded",
            RunStatus.SUCCEEDED_WITH_WARNINGS: "succeeded",
            RunStatus.FAILED: "failed",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.SKIPPED: "skipped",
        }.get(getattr(record, "status", ""), "running")
        checkpoint = self._autonomous_checkpoints.get(run_id)
        recovery = {
            AutonomousCheckpointStatus.PENDING: "等待自动提交",
            AutonomousCheckpointStatus.NEEDS_REPLAN: "需要重新规划",
            AutonomousCheckpointStatus.APPLIED: "已自动提交",
            AutonomousCheckpointStatus.CANCELLED: "已取消自动提交",
            AutonomousCheckpointStatus.EXPIRED: "断点已过期",
        }.get(getattr(checkpoint, "status", ""), "自主模式")
        mission = self._autonomous_missions.get(run_id)
        checkpoint_summary = ""
        if checkpoint is not None:
            checkpoint_summary = f"基于 revision {checkpoint.base_revision}"
            if checkpoint.asset_ids:
                checkpoint_summary += f" · {len(checkpoint.asset_ids)} 个素材"
        if mission is not None:
            stage_label = {
                MissionStage.PLAN: "计划",
                MissionStage.PREVIEW: "预览",
                MissionStage.COMMIT: "提交",
                MissionStage.VERIFY: "校验",
            }.get(mission.current_stage, "任务")
            gate_label = "质量门通过" if mission.quality_passed else "质量门待判定"
            checkpoint_summary = (
                f"{checkpoint_summary} · 阶段：{stage_label} · {gate_label}"
                if checkpoint_summary else f"阶段：{stage_label} · {gate_label}"
            )
        invocation = self.runtime.get_invocation(run_id) if self.runtime is not None else None
        scope = getattr(invocation, "scope", None)
        selected = tuple(getattr(scope, "selected_asset_ids", ()) or ())
        scope_summary = (
            f"素材：{', '.join(selected)}" if selected
            else "当前选择范围"
        )
        started_at = getattr(record, "started_at", None)
        finished_at = getattr(record, "finished_at", None)
        duration_ms = None
        if started_at is not None and finished_at is not None:
            duration_ms = max(0, int((float(finished_at) - float(started_at)) * 1000))
        diagnostics = tuple(getattr(record, "diagnostics", ()) or ())
        return item_cls(
            item_id=run_id,
            label="AI 自动剪辑",
            status=drawer_status,
            progress=float(getattr(record, "progress", 0.0) or 0.0),
            duration_ms=duration_ms,
            scope_summary=scope_summary,
            output_summary=str(getattr(invocation, "skill_id", "") or ""),
            can_cancel=drawer_status in ("running", "queued", "waiting_input"),
            can_retry=drawer_status in ("failed", "cancelled"),
            can_view_diagnostics=bool(diagnostics or getattr(record, "error_code", "")),
            error_code=str(getattr(record, "error_code", "") or ""),
            checkpoint_summary=checkpoint_summary,
            recovery_summary=recovery,
        )

    def _refresh_run_drawer(self) -> None:
        """聚合普通 run 与自主 run → RunDrawerState，灌进 panel。

        自主 run 也必须出现在同一 Run Drawer 中，显示其 revision 断点、素材范围、
        恢复状态，并复用取消/重试/诊断入口。无 run 时不发空 drawer，避免清空历史。
        """
        self._refresh_plugin_status()
        try:
            from src.features.edit.ui.ai_assistant_composer import (
                RunDrawerItem, RunDrawerState,
            )
            from src.features.ai_harness.invocation_models import RunStatus
        except ImportError:
            return
        if self.runtime is None:
            return
        run_ids: list[str] = []
        if self._active_chat_run_id:
            run_ids.append(self._active_chat_run_id)
        run_ids.extend(
            run_id for run_id in sorted(self._autonomous_runs)
            if run_id not in run_ids
        )
        if not run_ids:
            return
        items: list[Any] = []
        for run_id in run_ids:
            rec = self.runtime.get_run(run_id)
            if rec is None:
                continue
            if run_id in self._autonomous_runs:
                items.append(self._autonomous_drawer_item(run_id, rec, RunDrawerItem))
                continue
            run_status = rec.status
            drawer_status = {
                RunStatus.RUNNING: "running",
                RunStatus.QUEUED: "queued",
                RunStatus.VALIDATING: "queued",
                RunStatus.WAITING_FOR_USER: "waiting_input",
                RunStatus.SUCCEEDED: "succeeded",
                RunStatus.SUCCEEDED_WITH_WARNINGS: "succeeded",
                RunStatus.FAILED: "failed",
                RunStatus.CANCELLED: "cancelled",
                RunStatus.SKIPPED: "skipped",
            }.get(run_status, "running")
            materialization_job = self._materialization_job(run_id)
            materialization_error = ""
            materialization_diagnostics = False
            if materialization_job is not None:
                job_status = materialization_job.status.value
                drawer_status = {
                    "queued": "queued", "running": "running",
                    "cancelling": "running", "succeeded": "succeeded",
                    "failed": "failed", "cancelled": "cancelled",
                }.get(job_status, "running")
                drawer_label = f"生成媒体 · {materialization_job.label}"
                drawer_progress = materialization_job.progress
                materialization_error = materialization_job.error_code or ""
                materialization_diagnostics = bool(
                    materialization_job.error_message or materialization_error
                )
            else:
                drawer_label = self._tr_safe("assistant.run.chat_plan", "AI 规划")
                drawer_progress = getattr(rec, "progress", 0.0) or 0.0
            items.append(RunDrawerItem(
                item_id=run_id,
                label=drawer_label,
                status=drawer_status,
                progress=drawer_progress,
                can_cancel=drawer_status in ("running", "queued", "waiting_input"),
                can_retry=drawer_status in ("failed", "cancelled"),
                can_view_diagnostics=(
                    materialization_diagnostics or bool(getattr(rec, "diagnostics", None))
                ),
                error_code=materialization_error or (getattr(rec, "error_code", "") or ""),
            ))
        if not items:
            return
        active = sum(item.status in ("running", "queued", "waiting_input") for item in items)
        done = sum(item.status in ("succeeded", "skipped") for item in items)
        failed = sum(item.status == "failed" for item in items)
        state = RunDrawerState(
            items=items, active_count=active,
            completed_count=done, failed_count=failed,
        )
        try:
            self._panel.set_run_drawer(state)
        except Exception:  # noqa: BLE001
            pass

    def _footer_history_signature(self, draft_text: str) -> tuple[int, str, str] | None:
        """AUD-2014: 消息历史的轻量签名（计数 + 末条 message_id + 草稿文本）。

        ``recent_chat_messages(limit=100_000)`` 的返回只在 append/reset/
        restore 时变化，末条 ``message_id``（append 路径恒为新 uuid4）足以
        区分；service 读不到（异常）时返回 None 强制走全量重建。
        """
        try:
            messages = self._service.recent_chat_messages(limit=100_000)
        except Exception:  # noqa: BLE001 — 签名失败按"未知"处理
            return None
        last_id = str(getattr(messages[-1], "message_id", "") if messages else "")
        return (len(messages), last_id, draft_text)

    def _refresh_context_token_estimate(self, *, draft_text: str) -> None:
        """AUD-2014: 仅当消息历史/草稿变化时才全量重建上下文并估算 token。

        每次 service 状态变化（含每条流式 delta 后的追加）都会触发
        ``_refresh_composer_footer``；原先无条件 ``_build_chat_messages``
        在 UI 线程全量重建历史 + 逐条估算 token（O(全部消息)）。签名未变时
        直接复用 ``_chat_context_tokens``。注意：Teaching Animation 上下文
        变化不改变签名，顶部数字在下一条消息后刷新（展示性估算，可接受）。
        """
        signature = self._footer_history_signature(draft_text)
        if signature is not None and signature == getattr(
                self, "_footer_history_signature_cache", None):
            return
        try:
            self._build_chat_messages(
                draft_text=draft_text,
                record_checkpoint=not bool(draft_text.strip()),
            )
        except Exception:  # noqa: BLE001
            logger.debug("refresh chat token usage failed", exc_info=True)
        self._footer_history_signature_cache = signature

    def _refresh_composer_footer(self, *, draft_text: str = "") -> None:
        """聚合 RunRecord + ContextBundle → ComposerFooterState，灌进 panel。

        footer 显示真实 context used/limit（从 ContextBundle 算）+ has_active_run
        （从 runtime run 状态算）+ 当前权限（_current_permission）。
        """
        try:
            from src.features.edit.ui.ai_assistant_composer import ComposerFooterState
        except ImportError:
            return
        # 以同一套装配逻辑实时估算当前完整对话上下文；发送前若超限会在
        # ``_build_chat_messages`` 内生成摘要，因此顶部数字和实际请求一致。
        # AUD-2014: 历史/草稿未变化时跳过全量重建（签名缓存见上）。
        self._refresh_context_token_estimate(draft_text=draft_text)
        context_used = self._chat_context_tokens
        context_compressed = self._chat_context_compressed
        if not self._service.recent_chat_messages(limit=1) and self._active_context_bundle is not None:
            context_used = int(getattr(self._active_context_bundle, "total_tokens", 0) or 0)
            context_compressed = False
        # V2 path (plan §A4): when a unified run reported real usage, prefer it
        # over the local estimate so the top bar never fakes a provider number.
        v2_usage = self._v2_last_usage
        if v2_usage is not None:
            v2_actual = getattr(v2_usage, "actual_prompt", None)
            if v2_actual is not None:
                context_used = int(v2_actual)
        # has_active_run：runtime 有 active chat run 且非终态
        has_active_run = False
        if self.runtime is not None and self._active_chat_run_id:
            try:
                rec = self.runtime.get_run(self._active_chat_run_id)
                if rec is not None:
                    from src.features.ai_harness.invocation_models import RunStatus
                    has_active_run = rec.status in RunStatus.ACTIVE_STATES
            except Exception:  # noqa: BLE001
                pass
        if self.runtime is not None and not has_active_run:
            try:
                from src.features.ai_harness.invocation_models import RunStatus
                has_active_run = any(
                    (record := self.runtime.get_run(run_id)) is not None
                    and record.status in RunStatus.ACTIVE_STATES
                    for run_id in self._autonomous_runs
                )
            except Exception:  # noqa: BLE001
                pass
        materialization_job = self._materialization_job(self._active_chat_run_id)
        if materialization_job is not None and materialization_job.status.is_active:
            has_active_run = True
        registry_list = getattr(self._profile_registry, "list", None)
        profiles = registry_list() if callable(registry_list) else ()
        state = ComposerFooterState(
            permission=self._current_permission,
            context_used=context_used,
            context_compressed=context_compressed,
            context_limit=(
                self._profile_registry.active_profile.limits.max_context_tokens
                if self._profile_registry is not None
                and self._profile_registry.active_profile is not None
                else 200_000
            ),
            profile_label=(
                self._profile_registry.active_profile.display_name
                if self._profile_registry is not None
                and self._profile_registry.active_profile is not None
                else ""
            ),
            model_label=(
                self._profile_registry.active_profile.model_id
                if self._profile_registry is not None
                and self._profile_registry.active_profile is not None
                else ""
            ),
            profile_options=(
                tuple(
                    (p.profile_id, p.display_name, p.model_id)
                    for p in profiles
                )
                if self._profile_registry is not None else ()
            ),
            active_profile_id=(
                getattr(self._profile_registry, "active_profile_id", "")
                if self._profile_registry is not None else ""
            ),
            mode=self._current_assistant_mode,
            reasoning_effort=self._current_reasoning_effort,
            has_active_run=has_active_run,
        )
        try:
            self._panel.set_composer_footer(state)
        except Exception:  # noqa: BLE001
            pass

    def _context_budget(self) -> Any:
        """返回当前 Profile 的上下文预算（无 Profile 时使用 200k 默认值）。"""
        from src.features.edit.ai_editing.context_bundle import TokenBudget
        active = (
            self._profile_registry.active_profile
            if self._profile_registry is not None else None
        )
        limit = active.limits.max_context_tokens if active is not None else 200_000
        return TokenBudget(limit=max(1_024, int(limit)))

    def _on_composer_permission_changed(self, permission: str) -> None:
        """权限切换 → 写入 _current_permission + 刷新 footer。

        只作用于后续 invocation（不回写正在运行的 job）。合法值：read_only /
        propose / apply_with_each_approval；非法值忽略（防御）。
        """
        if permission not in ("read_only", "propose", "apply_with_each_approval"):
            return
        self._current_permission = permission
        self._refresh_composer_footer()

    def _on_composer_mode_changed(self, mode: str) -> None:
        """Select the pipeline for subsequent requests only."""
        self._current_assistant_mode = normalize_assistant_mode(mode)
        self._refresh_composer_footer()

    def _on_composer_profile_changed(self, profile_id: str) -> None:
        """切换底部 Profile，并让后续请求使用新的 provider/model。"""
        registry = self._profile_registry
        if registry is None or not profile_id:
            return
        profile = registry.get(profile_id)
        if profile is None:
            return
        try:
            registry.set_active(profile_id)
            registry.save_to_user_file()
        except Exception:
            logger.debug("saving selected AI profile failed", exc_info=True)

        connected = False
        if self._credential_store is not None:
            try:
                from src.features.edit.ai_editing.llm_variant_planner import LLMVariantPlanner
                planner = LLMVariantPlanner(
                    profile, self._credential_store,
                    reasoning_effort=self._current_reasoning_effort,
                )
                self._service.set_planner(planner)
                from src.features.edit.core.captions.translation import (
                    make_ai_assistant_workflow_translation_provider,
                )
                self._service.set_translation_provider(
                    make_ai_assistant_workflow_translation_provider(
                        profile, self._credential_store,
                    )
                )
                connected = True
            except Exception:
                logger.warning("切换 AI Profile 失败，回退到规则 planner", exc_info=True)
                self._service.set_planner(None)
                self._service.set_translation_provider(None)
        else:
            self._service.set_translation_provider(None)
        self._service.set_profile(profile_id, connected)
        self._refresh_composer_footer()
        pending_kinds = tuple(
            getattr(self._panel, "pending_attachment_kinds", lambda: ())() or ()
        )
        if pending_kinds:
            from src.features.edit.ai_editing.ai_provider_catalog import (
                profile_multimodal_reason,
                profile_supports_input,
            )
            if any(not profile_supports_input(profile, kind) for kind in pending_kinds):
                show = getattr(self._panel, "set_agent_notice", None)
                if callable(show):
                    show(profile_multimodal_reason(profile), warning=True)

    def _on_composer_reasoning_changed(self, effort: str) -> None:
        if effort not in ("low", "medium", "high"):
            return
        self._current_reasoning_effort = effort
        try:
            from src.features.edit.ai_editing.user_ai_config import (
                load_user_ai_config, save_user_ai_config,
            )
            config = load_user_ai_config()
            config["reasoning_effort"] = effort
            save_user_ai_config(config)
        except Exception:
            logger.debug("saving reasoning effort failed", exc_info=True)
        # Profile 不变，只重建 planner 以使后续请求使用新的推理强度。
        active = self._profile_registry.active_profile if self._profile_registry is not None else None
        if active is not None and self._credential_store is not None:
            try:
                from src.features.edit.ai_editing.llm_variant_planner import LLMVariantPlanner
                self._service.set_planner(LLMVariantPlanner(
                    active, self._credential_store,
                    reasoning_effort=effort,
                ))
            except Exception:
                logger.debug("updating reasoning effort failed", exc_info=True)
        self._refresh_composer_footer()

    def _on_run_cancel_requested(self, item_id: str) -> None:
        """Run Drawer 取消某 item → runtime.cancel(item_id)（plan §10.2 P0.6）。

        runtime 未装配时 no-op（向后兼容）。
        """
        if not item_id:
            return
        # B7: 清理 V2 媒体 run（防止取消后 V2 run 泄漏在 EXECUTING 状态）。
        self._v2_complete_media_generation(
            item_id, success=False, reason="用户取消",
        )
        if item_id in self._materialization_job_ids:
            registry = self._generation_job_registry()
            job_id = self._materialization_job_ids[item_id]
            if registry is not None:
                registry.cancel(job_id)
                self._update_materialization_runtime_job(item_id)
                self._refresh_run_drawer()
                self._refresh_composer_footer()
            return
        if self.runtime is None:
            return
        try:
            if self.runtime.cancel(item_id):
                return
        except Exception:  # noqa: BLE001
            logger.debug("runtime cancel failed for %s", item_id, exc_info=True)
        # AUD-2006：V2 AgentRun 由 AgentOrchestrator 管理，不在 HarnessRuntime
        # 中（runtime.cancel 对未知 id 返回 False 且此前被静默吞掉）。V2 drawer
        # 条目以 can_cancel=True 展示，取消必须分派到统一编排器，否则按钮
        # 完全无效（面板级 request_cancel_for_session 粒度不符）。
        if self._v2_orchestrator is not None:
            try:
                self._v2_orchestrator.request_cancel(item_id, reason="用户取消")
            except Exception:  # noqa: BLE001
                logger.debug(
                    "v2 orchestrator cancel failed for %s", item_id, exc_info=True,
                )

    def _on_run_retry_requested(self, item_id: str) -> None:
        """Run Drawer 的重试按钮 → 复用原 invocation 重新执行。"""
        if item_id:
            self._on_result_action_retry(item_id)

    def _on_run_diagnostics_requested(self, item_id: str) -> None:
        """Run Drawer 的诊断按钮 → 展示错误码、阶段和可读修复建议。"""
        if not item_id:
            return
        lines: list[str] = []
        record = self.runtime.get_run(item_id) if self.runtime is not None else None
        if record is not None:
            if getattr(record, "error_code", ""):
                lines.append(f"错误码：{record.error_code}")
            if getattr(record, "error_message", ""):
                lines.append(f"错误信息：{record.error_message}")
            for diagnostic in getattr(record, "diagnostics", ()) or ():
                code = diagnostic.get("code", "诊断")
                message = diagnostic.get("message", "")
                lines.append(f"{code}：{message}")

        # V2 agent runs are owned by AgentOrchestrator, not HarnessRuntime.
        # Their durable trace is still authoritative for failure details.
        trace_store = self.__dict__.get("run_trace_store")
        projection = None
        if trace_store is not None:
            projection = trace_store.project(
                session_id=self._trace_session_for_v2_run(item_id),
                run_id=item_id,
            )
        if projection is not None and projection.event_count:
            diagnostic = projection.last_diagnostic or {}
            code = str(diagnostic.get("error_code") or diagnostic.get("code") or "")
            message = str(diagnostic.get("error_message") or diagnostic.get("message") or "")
            if code and not any(code in line for line in lines):
                lines.append(f"{code}：{message}" if message else f"错误码：{code}")
            if projection.phase:
                lines.append(f"阶段：{projection.phase}")
            if projection.pending_tool_calls:
                lines.append(
                    "待完成工具：" + ", ".join(projection.pending_tool_calls[:6])
                )
            if projection.trace_truncated:
                lines.append(
                    f"运行轨迹已截断，丢弃 {projection.dropped_event_count} 条较早事件。"
                )
        if record is None and not (projection is not None and projection.event_count):
            return
        if not lines:
            lines.append(self._auto_caption_tr(
                "no_diagnostics", "本次运行没有额外诊断信息。"
            ))
        if getattr(record, "error_code", "") in {
            "AUDIO_CLIP_NOT_SELECTED", "AUDIO_ASSET_MISSING",
        }:
            lines.append(self._auto_caption_tr(
                "suggest_audio", "建议：在时间线上选中一个音频片段后重试。"
            ))
        elif getattr(record, "error_code", "") in {"ASR_FAILED", "ASR_START_FAILED"}:
            lines.append(self._auto_caption_tr(
                "suggest_asr", "建议：检查 ASR 模型是否完整安装，并确认音频素材仍可访问。"
            ))
        elif getattr(record, "error_code", "") == "SUBTITLE_COMMIT_FAILED":
            lines.append(self._auto_caption_tr(
                "suggest_commit", "建议：确认字幕轨道未锁定，且当前时间线仍处于打开状态。"
            ))
        try:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self._panel,
                self._auto_caption_tr("diagnostics_title", "AI 运行诊断"),
                "\n".join(lines),
            )
        except Exception:  # noqa: BLE001
            logger.info("AI run diagnostics %s: %s", item_id, " | ".join(lines))

    @staticmethod
    def _tr_safe(key: str, default: str) -> str:
        """i18n tr 安全包装（controller 可能在 i18n 未就绪时被测试调用）。"""
        try:
            from src.shared.i18n.i18n import tr
            return tr(key, default)
        except Exception:  # noqa: BLE001
            return default

    def _auto_caption_tr(self, key: str, default: str) -> str:
        """自动字幕 Agent 的本地化文案。"""
        return self._tr_safe(f"ai_assistant.auto_caption.{key}", default)

    def _on_inspect_context(self) -> None:
        """用户点"上下文"按钮 → 构建 ContextBundle 回填面板（M5 / §7.1）。

        用一个最小 ContextSources（从 service/bridge 读 brief + snapshot + decisions
        摘要）经 ContextService 组装 bundle。真实生产环境的 sources（接 AnalysisService
        的 candidate segments、tool manifest）在 host 注入 LLM planner 时已提供；
        这里复用同样降级策略：能取多少取多少，缺失字段不阻塞展示。
        """
        try:
            from src.features.edit.ai_editing.context_service import (
                ContextBuildRequest,
                ContextService,
            )

            sources = self._build_inspector_sources()
            request = ContextBuildRequest(
                sources=sources,
                budget=self._context_budget(),
            )
            bundle = ContextService().build_bundle(request)
            self._active_context_bundle = bundle
            self._panel.set_context_bundle(bundle)
            self._refresh_composer_footer()
        except Exception:
            logger.debug("context inspector bundle build failed", exc_info=True)

    def _on_context_block_excluded(self, block_id: str) -> None:
        """E13: 上下文检查器“排除”→ 重建 bundle 并回填面板（§7.1 可排除）。

        此前 ``context_block_excluded`` 全仓无接收者，排除按钮点击无效。
        """
        bundle = getattr(self, "_active_context_bundle", None)
        if bundle is None:
            return
        try:
            new_bundle = bundle.exclude_block(block_id)
        except Exception:  # noqa: BLE001 排除失败保留原 bundle
            logger.debug("exclude context block failed: %s", block_id, exc_info=True)
            return
        self._active_context_bundle = new_bundle
        self._panel.set_context_bundle(new_bundle)
        self._refresh_composer_footer()

    def _on_composer_scope_requested(self) -> None:
        """E13: /scope 命令与 footer scope 区域 → 打开上下文检查器。

        当前没有独立的范围选择卡；检查器的“排除”按钮就是素材范围调整
        入口，直接复用（此前该信号无人接收，/scope 点击无任何反应）。
        """
        self._on_inspect_context()

    _HELP_PAGE_ALIASES = {
        "edit": "Edit",
        "color": "Color",
        "composite": "Composite",
        "deliver": "Deliver",
    }

    def _on_help_open_panel_requested(self, page_ref: str) -> None:
        """E13: 帮助卡“打开页面”→ 按 page_ref 切换主页面。"""
        page = self._HELP_PAGE_ALIASES.get(str(page_ref or "").strip().lower())
        switch = getattr(self, "switch_to_page", None)
        if page is None or not callable(switch):
            logger.debug("help open panel: unknown page_ref %r", page_ref)
            try:
                self._panel.set_activity_status(
                    f"无法打开页面：{page_ref}", active=False, status="error"
                )
            except Exception:  # noqa: BLE001 状态条失败不影响主流程
                pass
            return
        switch(page)

    def _on_help_navigate_requested(self, entry_id: str) -> None:
        """E13: 帮助卡“引用”点击 → 给出可见反馈。

        帮助系统暂无深度定位目标（entry_id 指向帮助索引条目本身），
        在活动状态行显示引用，不再是无反应按钮。
        """
        try:
            self._panel.set_activity_status(
                f"帮助引用：{entry_id}", active=False, status="done"
            )
        except Exception:  # noqa: BLE001 状态条失败不影响主流程
            logger.debug("help navigate feedback failed: %s", entry_id)

    def _build_inspector_sources(self) -> Any:
        """构建检查器用的最小 ContextSources（读 service/bridge 现有数据）。"""
        service = self._service
        bridge = self._bridge

        class _InspectorSources:
            def get_brief_dict(self):
                brief_id = getattr(service, "_active_brief_id", "") or ""
                if not brief_id:
                    return None
                try:
                    brief = service.get_brief(brief_id)
                    return brief.to_dict() if brief is not None else None
                except Exception:
                    return None

            def get_snapshot_dict(self, *, detail_level, asset_limit):
                try:
                    from src.features.edit.ai_editing.edit_api import EditSession

                    session = EditSession.current()
                    if session is None:
                        return None
                    snap = session.get_snapshot()
                    return snap.to_dict() if hasattr(snap, "to_dict") else None
                except Exception:
                    return None

            def get_planning_constraints(self):
                decisions = getattr(service, "_decisions", {}) or {}
                if not decisions:
                    return None
                # 简化：列出 accepted/locked 的 id（完整 planning_constraints 在
                # DecisionStore；service 持的是 dict，这里只做检查器摘要）
                must_preserve = [
                    did for did, d in decisions.items()
                    if str(getattr(d, "state", "")).lower() in ("accepted", "locked")
                ]
                return {"must_preserve": must_preserve, "must_avoid": [], "user_overrides": []}

            def get_tool_manifest(self):
                try:
                    from src.features.edit.ai_editing.tool_facade import list_tool_definitions

                    return [t.to_dict() if hasattr(t, "to_dict") else dict(t)
                            for t in list_tool_definitions()]
                except Exception:
                    return None

            def get_capability_manifest(self):
                try:
                    manifest = bridge.get_capability_manifest()
                    return manifest.to_dict() if hasattr(manifest, "to_dict") else manifest
                except Exception:
                    return None

            def get_candidate_segments(self) -> None:
                return None  # 检查器展示当前 bundle，不重新跑候选收集

        return _InspectorSources()

    def _on_variant_selected(self, variant_id: str) -> None:
        """用户选定方案 → 校验（TAEP §11.6 步骤 10）。"""
        self._service.select_variant(variant_id)
        state = self._bridge.get_project_sequences()
        # state 可能为 None（无工程）；validate_selected 内部会给出明确诊断。
        self._service.validate_selected(variant_id, state=state)

    def _on_apply_requested(self, variant_id: str) -> None:
        """用户点"应用方案" → 走审批 → 提交（TAEP §11.6 步骤 11）。

        M2.1/M2.3/M0.1/M2.2：从 PlanRepository 取回被批准的那一份不可变 plan 及其
        preview；经 bridge 请求用户批准得到 ``ApprovalGrant``；consume 授权后，把
        现场编译的 ``MacroCommand`` 经 ``bridge.commit_ai_command`` 推入**真实
        CommandStack**（保证一次 Ctrl+Z 整体撤销 + 与手动编辑一致的刷新链）。

        不再走 ``service.apply_selected``（那条经 ``commit_command`` 绕过了 CommandStack）。
        service 仍负责 preview / state machine / messages；commit 这一步移到 bridge。
        """
        from src.features.edit.ai_editing.plan_compiler import compile_plan
        from src.features.edit.ai_editing.transaction_service import (
            _expected_commit_scope,
        )

        variant = self._service.get_variant(variant_id)
        if variant is None or variant.edit_plan_ref is None:
            return
        repo = self._service.plan_repository
        stored = repo.get(variant.edit_plan_ref)
        if stored is None or stored.plan is None:
            # plan 已不在仓库（工程重开 / variant 过期）→ 提示重新规划
            self._service.record_error("方案对应的 plan 已过期，请重新规划")
            return
        plan = stored.plan
        state = self._bridge.get_project_sequences()
        if state is None:
            self._service.record_error("无活动工程，无法应用方案")
            return
        # Apply 前始终以**当前**工程状态重新预览。仓库缓存的 preview 只用于
        # 选择方案时展示；它可能已经被手动编辑、undo/redo 或工程切换所淘汰。
        # 重新预览既验证 base_revision，也保证确认框展示的正是将要推入 stack 的 diff。
        preview = self._service.preview_plan(plan, state)
        if preview is None:
            return
        if preview.has_errors:
            self._service.record_error("方案校验未通过，无法应用")
            return
        # 宿主确认 → 真实 ApprovalGrant（M0.1）
        grant = self._bridge.request_plan_approval(plan, preview)
        if grant is None:
            return  # 用户拒绝

        # consume 授权（原子校验作用域 + 一次性消费）。失败则明确记错，不静默。
        approval_svc = self._bridge.approval_service()
        if approval_svc is None:
            self._service.record_error("授权服务不可用，无法安全应用方案")
            return
        try:
            # AUD-12909：grant 由 request_plan_approval(plan, preview) 签发、绑定了
            # preview.diff_hash；expected 必须携带同一值（缺省会被严格匹配拒绝）。
            approval_svc.consume(
                grant.grant_id,
                _expected_commit_scope(plan, diff_hash=preview.diff_hash),
            )
        except Exception as exc:
            self._service.record_error(f"授权校验失败：{exc}")
            return

        # M2.2：现场编译 macro（与 preview 同一 compiler，保证 byte-equal），
        # 经 bridge 推入真实 CommandStack —— 单 undo + 全量刷新。
        try:
            compiled = compile_plan(plan)
        except Exception as exc:
            self._service.record_error(f"方案编译失败：{exc}")
            return
        new_timeline = self._bridge.commit_ai_command(
            plan.target.timeline_id, compiled.macro,
            undo_label=compiled.undo_label,
        )
        if new_timeline is None:
            self._service.record_error("提交失败：找不到活动时间线视图")
            return
        # 成功：service 进 IDLE + 记一条消息；refresh_after_ai_commit 通常为空
        # （execute_command 已触发全量刷新）。
        self._service.on_applied(variant_id, new_timeline)
        self._bridge.refresh_after_ai_commit(new_timeline)

    def _on_cancel(self) -> None:
        """取消当前请求（TAEP §11.5）。"""
        # Service.cancel() 只负责状态机/runtime；本地 LLM worker 还需要显式
        # 置协作取消标志，否则 HTTP 返回后会把已停止的回复重新写回对话。
        for worker in (
            self._chat_worker,
            self._plan_worker,
            self._subtitle_plan_worker,
            *getattr(self, "_motion_graphic_postprocess_workers", {}).values(),
        ):
            if worker is not None and getattr(worker, "isRunning", lambda: False)():
                setattr(worker, "cancel_requested", True)
                worker.quit()
        # V2：把取消传递到统一编排器，使 run 进入 CANCELLED 终态（而非被
        # 迟到的 HTTP 返回误记为 SUCCEEDED）。HTTP 调用本身无法中断，但
        # ChatRunDriver 会在调用返回后复检 is_run_active 并返回 cancelled。
        if self._v2_orchestrator is not None:
            try:
                self._v2_orchestrator.request_cancel_for_session("default")
            except Exception:  # noqa: BLE001
                logger.debug("V2 session cancel failed", exc_info=True)
        self._service.cancel()

    # ------------------------------------------------------------------ #
    # profile / 上下文（供 UI header 调用）
    # ------------------------------------------------------------------ #

    def set_profile(self, profile_id: str, connected: bool) -> None:
        self._service.set_profile(profile_id, connected)
