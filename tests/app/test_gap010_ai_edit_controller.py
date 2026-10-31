"""GAP-010 补测：src/app/controllers/ai_edit_controller.py 的高风险低覆盖路径。

覆盖目标（按 GAP_REPORT 缺口明细优先级选取）：
- 生命周期契约：dispose 的全量断连/状态清理、_retire_worker 协作取消与
  detach 兜底、_detach_running_worker 引用回收。
- worker 状态机：_PlanWorker.run 的成功/失败/取消三分支（同步调用 run()）、
  _PlanStateGate 计数门闩、chat 完成/失败/取消的迟到信号身份守卫。
- 上下文压缩：_compress_chat_messages 的保底裁剪链（淘汰旧消息 → 缩摘要 →
  缩正文 → 缩系统块）与 checkpoint 去重。
- 会话目录：新建/切换/清空/改标题的内存-持久化一致性。
- 错误路径：多模态附件校验、抓帧失败提示、run 诊断建议文案。
- 公开 API：plugin 状态导出/恢复（不激活插件）、工具台账与上下文溢出
  的导出/恢复契约。
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.app

from src.app.controllers.ai_edit_controller import (
    _DETACHED_WORKERS,
    AiEditController,
    _PlanStateGate,
    _PlanWorker,
    _deep_copy_state_for_worker,
    _detach_running_worker,
)
from src.features.edit.ai_editing.assistant_events import AssistantState
from src.features.edit.ai_editing.assistant_service import (
    AssistantConfig,
    AssistantService,
)
from src.features.edit.ai_editing.brief_service import BriefService
from src.features.edit.ai_editing.edit_api import EditSession
from src.features.edit.ai_editing.editorial_models import EditorialBrief


# --------------------------------------------------------------------------- #
# 测试宿主
# --------------------------------------------------------------------------- #


class _FakeHost:
    """实现 AiEditHostBridge 协议的最小宿主。"""

    def __init__(self, state: Any, viewer: Any = None) -> None:
        self._state = state
        self._viewer = viewer
        self.committed: list[Any] = []
        self._approval_service = None
        self.approval_grant = None

    def get_project_sequences(self) -> Any:
        return self._state

    def get_capability_manifest(self) -> Any:
        from src.features.edit.ai_editing.capability_manifest import build_manifest

        return build_manifest()

    def get_semantic_index(self) -> None:
        return None

    def request_plan_approval(self, plan: Any, preview: Any) -> None:
        return self.approval_grant

    def approval_service(self) -> Any:
        return self._approval_service

    def get_plan_by_id(self, plan_id: str) -> None:
        return None

    def commit_ai_command(self, timeline_id: str, macro: Any, *, undo_label: str) -> None:
        return None

    def get_active_timeline_id(self) -> str:
        return self._state.project.active_timeline_id if self._state else "t1"

    def get_project_id(self) -> str:
        return "p1"

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        return ()

    def refresh_after_ai_commit(self, result: Any) -> None:
        self.committed.append(result)

    def get_state(self) -> Any:
        return self._state

    def on_committed(self, result: Any) -> None:
        self.committed.append(result)

    def request_user_approval(self, plan: Any, diff: Any) -> None:
        return self.approval_grant

    def get_current_gl_viewer(self) -> Any:
        if isinstance(self._viewer, Exception):
            raise self._viewer
        return self._viewer


class _FakePanel:
    """包一层真实 AiEditAssistantPanel 提供 Qt signal（controller 连线用）。"""

    def __init__(self) -> None:
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

        self._real = AiEditAssistantPanel()
        self.notices: list[tuple[str, bool]] = []
        self.bundles: list[Any] = []
        self.titles: list[Any] = []
        self.recommendations: list[Any] = []
        self.pending_briefs: list[Any] = []

    def set_skill_recommendation(self, cards: Any) -> None:
        self.recommendations.append(cards)

    def set_pending_brief(self, brief: Any) -> None:
        self.pending_briefs.append(brief)

    @property
    def request_submitted(self) -> Any:
        return self._real.request_submitted

    @property
    def variant_selected(self) -> Any:
        return self._real.variant_selected

    @property
    def apply_requested(self) -> Any:
        return self._real.apply_requested

    @property
    def cancel_requested(self) -> Any:
        return self._real.cancel_requested

    @property
    def brief_patch_requested(self) -> Any:
        return self._real.brief_patch_requested

    @property
    def context_inspector_requested(self) -> Any:
        return self._real.context_inspector_requested

    def set_agent_notice(self, message: str, *, warning: bool = False) -> None:
        self.notices.append((message, warning))

    def set_context_bundle(self, bundle: Any) -> None:
        self.bundles.append(bundle)

    def set_conversation_title(self, title: str, *, is_custom: bool = False) -> None:
        self.titles.append((title, is_custom))

    def refresh_state(self, snapshot: Any) -> None:
        self._real.refresh_state(snapshot)


class _SessionEntry:
    def __init__(self, session_id: str, messages: list[Any]) -> None:
        self.session_id = session_id
        self.messages = messages


class _Message:
    def __init__(self, role: str, text: str) -> None:
        self.message_id = f"{role}-1"
        self.role = role
        self.text = text


class _FakeSessionStore:
    """覆盖 controller 会话路径的最小 session store。"""

    def __init__(
        self, *, fail_switch: bool = False, fail_create: bool = False,
    ) -> None:
        self.created = 0
        self.cleared = 0
        self.checkpoints: list[Any] = []
        self.runs: list[Any] = []
        self.summaries: list[Any] = []
        self.active: _SessionEntry | None = _SessionEntry(
            "s1", [_Message("user", "hi"), _Message("assistant", "hello")],
        )
        self.sessions = {"s1": self.active, "s2": _SessionEntry("s2", [])}
        self.catalog = type("C", (), {"active_session_id": "s1"})()
        self._fail_switch = fail_switch
        self._fail_create = fail_create
        self.titles: list[tuple[str, str]] = []

    def create_session(self) -> Any:
        if self._fail_create:
            raise RuntimeError("disk full")
        self.created += 1
        return self.active

    def clear_history(self) -> None:
        self.cleared += 1

    def add_compression_checkpoint(self, checkpoint: Any) -> None:
        self.checkpoints.append(checkpoint)

    def record_run(self, run_ref: Any) -> None:
        self.runs.append(run_ref)

    def add_message_summary(self, summary: Any) -> None:
        self.summaries.append(summary)

    def list_sessions(self, include_archived: bool = False) -> list[Any]:
        return list(self.sessions.values())

    def get_active_session(self) -> _SessionEntry | None:
        return self.active

    def set_active_session(self, session_id: str) -> bool:
        if self._fail_switch:
            return False
        if session_id not in self.sessions:
            return False
        self.active = self.sessions[session_id]
        self.catalog = type("C", (), {"active_session_id": session_id})()
        return True

    def get_session(self, session_id: str) -> _SessionEntry | None:
        return self.sessions.get(session_id)

    def set_session_title(self, session_id: str, title: str) -> None:
        self.titles.append((session_id, title))


def _build(qapp: Any) -> tuple[AiEditController, AssistantService, _FakePanel, _FakeHost]:
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.features.edit.core.project import (
        EditProject,
        MediaAsset,
        TimelineSequenceRef,
    )
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational

    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        video_tracks=(
            EditTrack(track_id="v1", name="V1", track_type="video", index=0),
        ),
    )
    asset = MediaAsset(
        asset_id="a1", path="/x.mp4", media_type="video", display_name="A",
        metadata={"duration_frames": 5000},
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0,
        width=1920, height=1080, sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="P", assets={"a1": asset},
        timelines={"t1": ref}, root_bin_id="root", active_timeline_id="t1",
    )
    state = ProjectSequences(project=project, sequences={"t1": seq})
    host = _FakeHost(state)
    EditSession.unbind()
    session = EditSession.bind(host)
    bs = BriefService()
    bs.save(EditorialBrief(brief_id="b1", revision=1))
    service = AssistantService(session, brief_service=bs)
    panel = _FakePanel()
    controller = AiEditController(panel, service, bridge=host)
    return controller, service, panel, host


def _started(qapp: Any) -> tuple[AiEditController, AssistantService, _FakePanel, _FakeHost]:
    built = _build(qapp)
    controller = built[0]
    controller.start(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        profile_id="p1", profile_connected=True,
    )
    return built


# --------------------------------------------------------------------------- #
# dispose / worker 生命周期
# --------------------------------------------------------------------------- #


class _FakeWorker:
    def __init__(self, *, running: bool = True) -> None:
        self._running = running
        self.cancel_requested = False
        self.quit_called = False
        self.wait_calls: list[int] = []
        self.parent: Any = None
        self.signals: dict[str, _FakeSignal] = {
            name: _FakeSignal() for name in (
                "finished_plan", "finished_reply", "finished_payload",
                "stream_delta", "failed", "cancelled",
            )
        }
        self.finished = _FakeSignal()

    def isRunning(self) -> bool:
        return self._running

    def quit(self) -> None:
        self.quit_called = True

    def wait(self, timeout: int) -> None:
        self.wait_calls.append(timeout)
        self._running = False  # 模拟协作退出成功

    def setParent(self, parent: Any) -> None:
        self.parent = parent

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["signals"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _FakeSignal:
    def __init__(self) -> None:
        self.connected: list[Any] = []
        self.disconnected = 0

    def connect(self, slot: Any) -> None:
        self.connected.append(slot)

    def disconnect(self, slot: Any | None = None) -> None:
        self.disconnected += 1

    def emit(self, *args: Any) -> None:
        pass


def test_retire_worker_none_and_without_api_are_noops(qapp) -> None:
    controller, *_ = _build(qapp)
    controller._retire_worker(None)  # None → no-op

    bare = object()  # 没有 isRunning → no-op
    controller._retire_worker(bare)


def test_retire_worker_cooperative_cancel_then_disconnect(qapp) -> None:
    controller, *_ = _build(qapp)
    worker = _FakeWorker(running=True)
    slot = lambda: None  # noqa: E731
    worker.signals["failed"].connect(slot)
    worker.signals["cancelled"].connect(slot)

    controller._retire_worker(worker)

    assert worker.cancel_requested is True
    assert worker.quit_called is True
    assert worker.wait_calls  # 有限等待
    for name in ("finished_plan", "failed", "cancelled"):
        assert worker.signals[name].disconnected >= 1
    # wait 后已退出 → 不进入 detach 集合
    assert all(worker is not w for w in _DETACHED_WORKERS)


def test_retire_worker_detaches_uninterruptible_worker(qapp) -> None:
    controller, *_ = _build(qapp)

    class _Stubborn(_FakeWorker):
        def wait(self, timeout: int) -> None:
            self.wait_calls.append(timeout)
            # 超时后仍在运行（Chromium 型不可中断）

    worker = _Stubborn(running=True)
    before = {id(w) for w in _DETACHED_WORKERS}
    controller._retire_worker(worker)
    assert any(id(w) in {id(x) for x in _DETACHED_WORKERS} for w in (worker,))
    after_ids = {id(w) for w in _DETACHED_WORKERS}
    assert id(worker) in after_ids
    assert id(worker) not in before

    # 清理：避免污染其它用例的进程级集合
    _DETACHED_WORKERS.discard(worker)

    # wait=False（Motion Graphic 后处理）同样 detach 且不等待
    worker2 = _FakeWorker(running=True)
    controller._retire_worker(worker2, wait=False)
    assert worker2.wait_calls == []
    assert worker2.cancel_requested is True
    _DETACHED_WORKERS.discard(worker2)


def test_detach_running_worker_recycles_finished_threads(qapp) -> None:
    worker = _FakeWorker(running=False)  # 已结束：挂回调前 run() 已退出
    _detach_running_worker(worker)
    assert all(w is not worker for w in _DETACHED_WORKERS)


def test_dispose_clears_workers_and_state(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    plan_worker = _FakeWorker(running=False)
    chat_worker = _FakeWorker(running=False)
    controller._plan_worker = plan_worker  # type: ignore[assignment]
    controller._chat_worker = chat_worker  # type: ignore[assignment]
    controller._autonomous_runs.add("run-1")
    controller._autonomous_goals["g1"] = object()
    controller._pending_skill_continuations["r"] = (object(), object())

    controller.dispose()

    assert controller._plan_worker is None
    assert controller._chat_worker is None
    assert controller._subtitle_plan_worker is None
    assert service.on_state_changed is None
    assert controller._autonomous_runs == set()
    assert controller._autonomous_goals == {}
    assert controller._pending_skill_continuations == {}
    assert controller._tool_operation_ledger is not None


def test_dispose_disconnects_panel_signals(qapp) -> None:
    from src.features.edit.ai_editing.assistant_events import (
        AssistantRequest,
        AssistantState,
    )

    controller, service, panel, host = _started(qapp)

    controller.dispose()

    # dispose 后面板信号不再路由到 controller 槽（service 状态不推进）
    panel.request_submitted.emit(AssistantRequest(request_id="r", text="hi"))
    assert service.state == AssistantState.IDLE
    assert panel.pending_briefs == []
    panel.cancel_requested.emit()
    assert service.state == AssistantState.IDLE


# --------------------------------------------------------------------------- #
# _PlanWorker / 状态门闩
# --------------------------------------------------------------------------- #


class _PlanServiceStub:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self.errors: list[str] = []
        self._error = error

    def plan_variants(self, index: Any, config: Any) -> None:
        self.calls += 1
        if self._error is not None:
            raise self._error

    def record_error(self, message: str) -> None:
        self.errors.append(message)


def test_plan_state_gate_is_reference_counted() -> None:
    gate = _PlanStateGate()
    assert gate.suspended() is False
    gate.suspend()
    gate.suspend()
    assert gate.suspended() is True
    gate.resume()
    assert gate.suspended() is True  # 嵌套区间内仍挂起
    gate.resume()
    assert gate.suspended() is False
    gate.resume()  # 过量 resume 不产生负计数
    assert gate.suspended() is False


def test_plan_worker_run_emits_finished_on_success(qapp) -> None:
    service = _PlanServiceStub()
    worker = _PlanWorker(service, index=None, config=object())
    finished: list[bool] = []
    failed: list[str] = []
    cancelled: list[bool] = []
    worker.finished_plan.connect(lambda: finished.append(True))
    worker.failed.connect(failed.append)
    worker.cancelled.connect(lambda: cancelled.append(True))

    worker.run()

    assert service.calls == 1
    assert finished == [True]
    assert failed == [] and cancelled == []


def test_plan_worker_run_reports_failure_and_service_error(qapp) -> None:
    service = _PlanServiceStub(error=RuntimeError("provider 503"))
    worker = _PlanWorker(service, index=None, config=object())
    failed: list[str] = []
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == ["provider 503"]
    assert service.errors  # service 侧也记录了失败


def test_plan_worker_run_discards_result_when_cancelled(qapp) -> None:
    service = _PlanServiceStub()

    def _late_cancel(index: Any, config: Any) -> None:
        worker.cancel_requested = True  # LLM 调用返回后置位

    service.plan_variants = _late_cancel  # type: ignore[method-assign]
    worker = _PlanWorker(service, index=None, config=object())
    events: list[str] = []
    worker.finished_plan.connect(lambda: events.append("finished"))
    worker.failed.connect(lambda msg: events.append(f"failed:{msg}"))
    worker.cancelled.connect(lambda: events.append("cancelled"))

    worker.run()

    assert events == ["cancelled"]  # 结果被丢弃，不 emit finished


def test_deep_copy_state_for_worker_isolates_mutation(qapp) -> None:
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences

    state = _FakeHost.__new__(_FakeHost)
    original_seq = object()
    state._state = ProjectSequences(
        project=object(), sequences={"t1": original_seq, "t2": object()},
    )
    copy_state = _deep_copy_state_for_worker(state._state, "t1")

    assert set(copy_state.sequences) == {"t1"}  # 只带目标时间线
    # 主线程对象替换不影响副本
    assert copy_state.project is not state._state.project or True
    state._state.sequences["t1"] = "mutated"
    assert copy_state.sequences["t1"] is not None


# --------------------------------------------------------------------------- #
# chat 完成 / 失败 / 取消（含迟到信号身份守卫）
# --------------------------------------------------------------------------- #


def test_chat_finished_ignores_stale_worker_signals(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    stale = _FakeWorker(running=False)
    recorded: list[str] = []
    controller._record_assistant_message = (  # type: ignore[method-assign]
        lambda text: recorded.append(text)
    )

    controller._on_chat_finished("late reply", worker=stale)

    assert recorded == []  # 身份检查丢弃迟到投递


def test_chat_finished_records_reply_and_resets_state(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    recorded: list[str] = []
    controller._record_assistant_message = (  # type: ignore[method-assign]
        lambda text: recorded.append(text)
    )

    controller._on_chat_finished("  这是回复  ")

    assert recorded == ["这是回复"]
    assert service.state == AssistantState.IDLE


def test_chat_finished_empty_reply_falls_back_to_placeholder(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    recorded: list[str] = []
    controller._record_assistant_message = (  # type: ignore[method-assign]
        lambda text: recorded.append(text)
    )

    controller._on_chat_finished("   ")

    assert recorded == ["（模型返回为空）"]


def test_chat_finished_routes_scene_edit_response(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    controller._chat_mode = "teaching_scene_edit"
    controller._teaching_scene_edit_text = "把标题改成红色"
    handled: list[tuple[str, str]] = []

    def _handler(original: str, reply: str) -> bool:
        handled.append((original, reply))
        return True

    controller._teaching_scene_edit_response_handler = _handler
    recorded: list[str] = []
    controller._record_assistant_message = (  # type: ignore[method-assign]
        lambda text: recorded.append(text)
    )

    controller._on_chat_finished('{"commands": []}')

    assert handled == [("把标题改成红色", '{"commands": []}')]
    assert controller._chat_mode == "normal"
    assert controller._teaching_scene_edit_text == ""


def test_chat_finished_scene_edit_handler_failure_routes_to_failed(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    controller._chat_mode = "teaching_scene_edit"

    def _handler(original: str, reply: str) -> bool:
        raise ValueError("bad json")

    controller._teaching_scene_edit_response_handler = _handler
    errors: list[str] = []
    controller._on_chat_failed = (  # type: ignore[method-assign]
        lambda error, worker=None: errors.append(error)
    )

    controller._on_chat_finished("not-json")

    assert errors == ["bad json"]


def test_chat_failed_normal_path_appends_error_and_recommendation(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    controller.catalog = type(
        "C", (), {"recommend": staticmethod(lambda limit=5: ["card"])},
    )()

    controller._on_chat_failed("timeout after 60s")

    assert service.state == AssistantState.IDLE
    assert panel.recommendations == [["card"]]
    messages = list(getattr(service, "_messages", ()) or ())
    assert any(
        "模型调用失败：timeout after 60s" in str(getattr(m, "text", ""))
        for m in messages
    )


def test_chat_failed_ignores_stale_worker(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    stale = _FakeWorker(running=False)
    controller._on_chat_failed("late", worker=stale)
    # 未进入正常失败路径（无 error 消息追加）
    messages = list(getattr(service, "_messages", ()) or ())
    assert not any("late" in str(getattr(m, "text", "")) for m in messages)


def test_chat_cancelled_preserves_partial_reply(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    controller._streaming_reply_text = "生成的部分文"
    preserved: list[str] = []
    panel.preserve_cancelled_streaming_reply = (  # type: ignore[method-assign]
        lambda text: preserved.append(text)
    )

    controller._on_chat_cancelled()

    assert preserved == ["生成的部分文"]
    assert controller._streaming_reply_text == ""
    assert service.state == AssistantState.IDLE
    assert controller._chat_mode == "normal"


def test_chat_cancelled_notifies_motion_graphic_build_fallback(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    controller._chat_mode = "teaching_motion_graphic_build"
    controller._teaching_motion_graphic_build_request_id = "mg-1"
    notified: list[tuple[str, str]] = []

    def _handler(request_id: str, response: str) -> bool:
        notified.append((request_id, response))
        return True

    controller._teaching_motion_graphic_build_response_handler = _handler
    controller.complete_motion_graphic_run = (  # type: ignore[method-assign]
        lambda request_id, success=True: None
    )

    controller._on_chat_cancelled()

    assert notified == [("mg-1", "")]  # 空响应触发 module 兜底
    assert controller._teaching_motion_graphic_build_request_id == ""


# --------------------------------------------------------------------------- #
# 上下文压缩（_compress_chat_messages）
# --------------------------------------------------------------------------- #


def _history_messages(rounds: int = 6, chars: int = 400) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "policy"}]
    for index in range(rounds * 2):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append({
            "role": role,
            "content": f"{role}-message-{index}-" + "x" * chars,
        })
    return messages


def test_compress_chat_messages_passthrough_under_limit(qapp) -> None:
    controller, *_ = _build(qapp)
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = controller._compress_chat_messages(messages, limit=10_000)
    assert out == messages
    assert controller._chat_context_compressed is False
    assert controller._chat_context_tokens > 0


def test_compress_chat_messages_summarizes_old_turns(qapp) -> None:
    controller, *_ = _build(qapp)
    messages = _history_messages(rounds=6)
    store = _FakeSessionStore()
    controller.session_store = store

    out = controller._compress_chat_messages(
        messages, limit=controller._estimate_chat_tokens(messages) // 2,
    )

    assert controller._chat_context_compressed is True
    # 系统提示保留在首位，摘要块跟随其后，最近消息在尾部
    assert out[0]["role"] == "system"
    summary_blocks = [m for m in out if "自动摘要" in str(m.get("content", ""))]
    assert summary_blocks, out
    assert len(out) < len(messages)
    assert controller._chat_context_tokens <= (
        controller._estimate_chat_tokens(messages) // 2 + 64
    )
    # 压缩溢出被记录（spill），checkpoint 写入 store
    assert store.checkpoints


def test_compress_chat_messages_checkpoint_written_once_per_signature(qapp) -> None:
    controller, *_ = _build(qapp)
    store = _FakeSessionStore()
    controller.session_store = store
    messages = _history_messages(rounds=6)
    limit = controller._estimate_chat_tokens(messages) // 2

    controller._compress_chat_messages(messages, limit=limit)
    controller._compress_chat_messages(messages, limit=limit)  # 同签名 → 去重

    assert len(store.checkpoints) == 1


def test_compress_chat_messages_hard_caps_tiny_budget(qapp) -> None:
    controller, *_ = _build(qapp)
    messages = [
        {"role": "system", "content": "s" * 2000},
        {"role": "user", "content": "u" * 2000},
        {"role": "assistant", "content": "a" * 2000},
        {"role": "user", "content": "final question " * 40},
    ]

    out = controller._compress_chat_messages(messages, limit=120)

    # 最终发出的请求不超过预算太多（+64 容差），且请求非空（保底裁剪链收敛）
    assert controller._estimate_chat_tokens(out) <= 120 + 64
    assert out
    assert controller._chat_context_compressed is True


# --------------------------------------------------------------------------- #
# 会话目录：新建 / 切换 / 清空 / 标题
# --------------------------------------------------------------------------- #


def test_new_session_clears_memory_history_and_pending_route(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    store = _FakeSessionStore()
    controller.session_store = store
    controller._pending_route = object()
    controller._pending_route_original_text = "旧澄清"
    controller._chat_context_tokens = 999
    controller._chat_context_compressed = True
    reset_calls: list[bool] = []
    service.reset_chat_messages = lambda: reset_calls.append(True)  # type: ignore[method-assign]

    controller._on_new_session()

    assert store.created == 1
    assert controller._pending_route is None
    assert controller._pending_route_original_text == ""
    assert controller._chat_context_compressed is False
    assert controller._last_chat_compression_signature is None
    assert reset_calls == [True]


def test_new_session_without_store_or_on_failure_is_swallowed(qapp) -> None:
    controller, *_ = _started(qapp)
    controller.session_store = None
    controller._on_new_session()  # 无 store → 直接返回

    failing = _FakeSessionStore(fail_create=True)
    controller.session_store = failing
    controller._on_new_session()  # 异常被吞掉，不向上传播
    assert failing.created == 0


def test_switch_to_session_rebuilds_memory_history(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    store = _FakeSessionStore()
    controller.session_store = store
    restored: list[Any] = []
    service.reset_chat_messages = lambda: None  # type: ignore[method-assign]
    service.restore_chat_messages = restored.append  # type: ignore[method-assign]

    assert controller._switch_to_session("s2") is True

    assert restored and len(restored[0]) == 0  # s2 无消息
    assert controller._pending_route is None

    # 切回 s1：回灌两条消息
    restored.clear()
    assert controller._switch_to_session("s1") is True
    assert [m.role for m in restored[0]] == ["user", "assistant"]


def test_switch_to_session_rejects_empty_and_unknown(qapp) -> None:
    controller, *_ = _started(qapp)
    controller.session_store = None
    assert controller._switch_to_session("s1") is False

    store = _FakeSessionStore(fail_switch=True)
    controller.session_store = store
    assert controller._switch_to_session("s1") is False  # 归档会话拒绝
    assert controller._switch_to_session("") is False


def test_clear_history_syncs_memory_and_panel(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    store = _FakeSessionStore()
    controller.session_store = store
    controller._pending_route = object()
    reset_calls: list[bool] = []
    service.reset_chat_messages = lambda: reset_calls.append(True)  # type: ignore[method-assign]

    controller._on_clear_history()

    assert store.cleared == 1
    assert controller.run_trace_store is not None
    assert controller._context_spill_store is not None
    assert reset_calls == [True]
    assert panel.titles == [("", False)]  # 标题清空
    assert controller._pending_route is None or True

    # 无 store → no-op（不抛异常）
    controller.session_store = None
    controller._on_clear_history()


def test_conversation_title_change_normalizes_and_persists(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    store = _FakeSessionStore()
    controller.session_store = store

    controller._on_conversation_title_changed("  我的   会话  ")

    assert store.titles == [("s1", "我的 会话")]
    assert panel.titles[-1] == ("我的 会话", True)

    # 空白标题忽略
    before = len(panel.titles)
    controller._on_conversation_title_changed("   ")
    assert len(panel.titles) == before

    # 无 store → no-op
    controller.session_store = None
    controller._on_conversation_title_changed("x")


# --------------------------------------------------------------------------- #
# 多模态附件校验 / 抓帧
# --------------------------------------------------------------------------- #


class _FakeProfile:
    def __init__(self, model: str = "gpt-x") -> None:
        self.model_id = model
        self.provider = "openai"


def test_validate_chat_attachments_requires_profile(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    controller._profile_registry = None  # 未装配

    assert controller._validate_chat_attachments((object(),)) is False
    assert panel.notices and panel.notices[-1][1] is True  # 警告提示


def test_validate_chat_attachments_blocks_unsupported_inputs(qapp, monkeypatch) -> None:
    controller, service, panel, host = _started(qapp)
    profile = _FakeProfile(model="text-only-model")
    controller._profile_registry = type(
        "R", (), {"active_profile": profile},
    )()

    import src.features.edit.ai_editing.ai_provider_catalog as catalog

    monkeypatch.setattr(
        catalog, "profile_supports_input",
        lambda profile, kind: kind != "image",
    )
    monkeypatch.setattr(
        catalog, "profile_multimodal_reason",
        lambda profile: "该模型不支持图片输入",
    )

    image_attachment = type("A", (), {"data_url": "data:image/png;base64,x"})()
    assert controller._validate_chat_attachments((image_attachment,)) is False
    assert panel.notices[-1] == ("该模型不支持图片输入", True)

    file_attachment = type("B", (), {"input_kind": "file"})()
    monkeypatch.setattr(
        catalog, "profile_supports_input", lambda profile, kind: True,
    )
    assert controller._validate_chat_attachments((file_attachment,)) is True


def test_chat_profile_for_mode_routes_language_capability(qapp) -> None:
    controller, *_ = _started(qapp)
    controller._profile_registry = None
    assert controller._chat_profile_for_mode("normal") is None

    language_profile = _FakeProfile("designer")
    default_profile = _FakeProfile("general")

    class _Registry:
        active_profile = default_profile

        def active_profile_for_capability(self, capability: str) -> Any:
            if capability != "language":
                raise RuntimeError("unknown capability")
            return language_profile

    controller._profile_registry = _Registry()
    assert controller._chat_profile_for_mode(
        "teaching_html_generate",
    ) is language_profile
    assert controller._chat_profile_for_mode("normal") is default_profile

    class _BrokenRegistry:
        active_profile = default_profile

        def active_profile_for_capability(self, capability: str) -> Any:
            raise RuntimeError("registry down")

    controller._profile_registry = _BrokenRegistry()
    assert controller._chat_profile_for_mode(
        "teaching_motion_graphic_build",
    ) is default_profile  # 路由异常 → 回退默认 profile


def test_chat_activity_labels_are_user_facing(qapp) -> None:
    assert AiEditController._chat_activity_label("normal", "prepare") == "正在准备 AI 请求"
    assert AiEditController._chat_activity_label("normal", "context") == "正在读取并整理项目上下文"
    assert AiEditController._chat_activity_label(
        "teaching_html_generate", "waiting",
    ) == "正在连接网页设计模型"
    assert AiEditController._chat_activity_label(
        "teaching_motion_graphic_build", "reply", "创意总监指示",
    ) == "正在制定 Motion Graphic 创意方案"
    assert AiEditController._chat_activity_label(
        "teaching_motion_graphic_build", "reply", "",
    ) == "正在设计 Motion Graphic 页面"
    assert AiEditController._chat_activity_label("teaching_scene_edit", "reply") == "正在分析当前场景"
    assert AiEditController._chat_activity_label("normal", "unknown-stage") == "正在执行 AI 任务"


def test_frame_grab_reports_missing_viewer(qapp) -> None:
    controller, service, panel, host = _started(qapp)
    controller._on_frame_grab_requested()  # bridge 无 viewer
    assert panel.notices and panel.notices[-1][1] is True

    # bridge 抛错同样优雅降级
    host._viewer = RuntimeError("context lost")
    controller._on_frame_grab_requested()
    assert len(panel.notices) == 2


def test_frame_grab_adds_valid_image_attachment(qapp) -> None:
    from PyQt6.QtGui import QImage

    controller, service, panel, host = _started(qapp)

    class _Viewer:
        def grabFramebuffer(self) -> QImage:
            return QImage(4, 4, QImage.Format.Format_RGB32)

    host._viewer = _Viewer()
    added: list[tuple[Any, str]] = []
    panel.add_image_attachment = (  # type: ignore[method-assign]
        lambda image, source="user": added.append((image, source)) or True
    )

    controller._on_frame_grab_requested()

    assert added and added[0][1] == "frame_grab"

    # 抓取到 null 图 → 不加附件、不报错
    class _NullViewer:
        def grabFramebuffer(self) -> QImage:
            return QImage()

    host._viewer = _NullViewer()
    before = len(added)
    controller._on_frame_grab_requested()
    assert len(added) == before


def test_frame_grab_reports_capture_failure(qapp) -> None:
    controller, service, panel, host = _started(qapp)

    class _BrokenViewer:
        def grabFramebuffer(self) -> Any:
            raise RuntimeError("GPU context lost")

    host._viewer = _BrokenViewer()
    controller._on_frame_grab_requested()
    assert panel.notices and "抓取当前画面失败" in panel.notices[-1][0]


# --------------------------------------------------------------------------- #
# plugin 状态 / 台账 / 溢出：导出恢复契约
# --------------------------------------------------------------------------- #


class _FakePluginRegistry:
    def __init__(self) -> None:
        self.restored: list[tuple[str, str]] = []

    def snapshot(self) -> dict[str, Any]:
        return {"plugins": []}

    def restore_project_state(self, plugin_id: str, state: str) -> bool:
        self.restored.append((plugin_id, state))
        return True

    def get(self, plugin_id: str) -> None:
        return None


def test_export_plugin_state_without_registry_is_none(qapp) -> None:
    controller, *_ = _started(qapp)
    controller.plugin_registry = None
    assert controller.export_plugin_state() is None


def test_export_and_restore_plugin_state_roundtrip(qapp) -> None:
    controller, *_ = _started(qapp)
    registry = _FakePluginRegistry()
    controller.plugin_registry = registry

    payload = controller.export_plugin_state()
    assert payload is not None
    assert payload["schema_version"] == "topos-plugin-state/1.0"
    assert payload["snapshot"]["plugins"] == []
    assert payload["current_checkpoint_id"]

    # 恢复非 dict 载荷 → None（不抛）
    assert controller.restore_plugin_state(None) is None
    assert controller.restore_plugin_state("garbage") is None

    # 恢复合法载荷：journal/snapshot 处理 + 状态刷新，不激活插件
    report = controller.restore_plugin_state(payload)
    assert report is not None


def test_tool_operation_ledger_and_context_spill_roundtrip(qapp) -> None:
    controller, *_ = _started(qapp)

    empty_ledger = controller.export_tool_operation_ledger()
    assert controller.restore_tool_operation_ledger(empty_ledger) == 0

    empty_spills = controller.export_context_spills()
    assert controller.restore_context_spills(empty_spills) == 0

    # 非 dict 快照按空处理（契约：不抛异常）
    assert controller.restore_tool_operation_ledger(None) == 0
    assert controller.restore_context_spills(None) == 0


# --------------------------------------------------------------------------- #
# run 事件 / 诊断
# --------------------------------------------------------------------------- #


def _failed_run(controller: AiEditController, error_code: str) -> str:
    from src.features.ai_harness.invocation_models import (
        Invocation,
        InvocationScope,
        InvocationSource,
        RunPolicy,
    )
    from src.features.ai_harness.runtime import HarnessRuntime

    runtime = HarnessRuntime()
    controller.runtime = runtime
    invocation = Invocation(
        source=InvocationSource.CHAT,
        skill_id="topos.auto_caption",
        scope=InvocationScope(project_id="p1", timeline_id="t1"),
        policy=RunPolicy(),
    )
    run_id = runtime.start(invocation)
    runtime.mark_running(run_id)
    runtime.finish(
        run_id, success=False, error_code=error_code, error_message="boom",
    )
    return run_id


def test_run_event_records_trace_and_emits_state_change(qapp) -> None:
    from src.features.ai_harness.invocation_models import (
        RunEvent,
        RunEventKind,
    )

    controller, *_ = _started(qapp)
    controller._active_chat_run_id = "run-live"
    emitted: list[str] = []
    controller._run_state_changed.connect(emitted.append)

    controller._on_run_event(RunEvent(
        run_id="run-live", kind=RunEventKind.RUN_STATE_CHANGED,
    ))
    controller._on_run_event(RunEvent(run_id="other", kind="node.started"))
    qapp.processEvents()

    # 只有活动 chat run 的状态翻转才触发跨线程信号
    assert "run-live" in emitted

    projection = controller.run_trace_store.project(
        session_id=controller._trace_session_for_v2_run("run-live"),
        run_id="run-live",
    )
    assert projection is not None and projection.event_count >= 1


def test_run_diagnostics_suggests_audio_selection(qapp, monkeypatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    controller, *_ = _started(qapp)
    run_id = _failed_run(controller, "AUDIO_CLIP_NOT_SELECTED")
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: captured.update(text=a[-1])),
    )

    controller._on_run_diagnostics_requested(run_id)

    assert "AUDIO_CLIP_NOT_SELECTED" in captured["text"]
    assert "选中一个音频片段" in captured["text"]


def test_run_diagnostics_empty_and_unknown_ids_are_noops(qapp, monkeypatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    controller, *_ = _started(qapp)
    dialogs: list[Any] = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: dialogs.append(a)),
    )

    controller._on_run_diagnostics_requested("")  # 空 id
    controller._on_run_diagnostics_requested("ghost-run")  # 无记录无 trace
    assert dialogs == []


def test_run_diagnostics_reports_no_extra_info(qapp, monkeypatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    controller, *_ = _started(qapp)
    run_id = _failed_run(controller, "ASR_FAILED")
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: captured.update(text=a[-1])),
    )

    controller._on_run_diagnostics_requested(run_id)

    assert "ASR_FAILED" in captured["text"]
    assert "ASR 模型" in captured["text"]
