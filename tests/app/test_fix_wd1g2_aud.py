"""批 D（wd1-g2）修复回归测试：AI 编辑控制器域（Major×18）。

覆盖 REMEDIATION_PLAN 批 D 条目：

* AUD-2001 —— ``_start_chat`` 多模态校验失败早退必须终结 Run Drawer 活动项。
* AUD-2002 —— 非 chat 模式提交不得遗留 ``_pending_chat_attachments``。
* AUD-2003 —— ``_on_chat_cancelled`` 对 ``teaching_html_generate`` 终结 V2 run。
* AUD-2004 —— dispose 清 worker 引用 / 断开信号 / 移除 runtime 订阅，
  迟到信号被身份检查丢弃。
* AUD-2005 —— plan/chat/subtitle worker 经 ``finished → deleteLater`` 释放。
* AUD-2006 —— Run Drawer 取消分派到 V2 编排器（runtime.cancel 未知 id 时）。
* AUD-2007 —— 字幕规划 worker 只读主线程深快照（``_deep_copy_state_for_worker``）。
* AUD-2008 —— dispose 不对不可中断的 MG 后处理 worker 逐个 wait。
* AUD-2100 —— NullAiEditHostBridge 补齐协议方法（isinstance 协议一致）。
* AUD-2103 —— AI 导出绑定 scope.timeline_id 与 Edit 页当前时间线一致性。
* AUD-2104 —— 宿主静默取消路径下导出 Job 显式 fail，不永久 running。
* AUD-2105 —— ``check_ai_action`` 缺工程 + 缺 feature_flags 一律 fail-closed
  （legacy 放行改为显式 ``legacy_no_policy`` 注入）。
* AUD-2200 —— 装配失败的外层 except 复用 teardown（MCP host 等停用）。
* AUD-2210 —— ``_render_single_frame`` finally 调用 ``renderer.cleanup()``。
* AUD-2211 —— 渲染 provider 只消费主线程捕获的引用快照，不活读主窗口。
* AUD-2212 —— 生成候选入箱经 GUI 线程 queued invocation（无 Qt 回退同步）。
* AUD-2213 —— provider 整体异常防护 + 非法分辨率回退 1920x1080。
* GAP-010 —— 以上用例同时为 ai_edit_controller.py 低覆盖区补测。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.app


# ═══════════════════════════════════════════════════════════════════════
# 测试基座：controller / panel / service / registry
# ═══════════════════════════════════════════════════════════════════════


def _make_profile(*, multimodal: bool = True):
    from src.features.edit.ai_editing.provider_profiles import AiProviderProfile

    profile = AiProviderProfile(
        profile_id="profile-test",
        display_name="Test-Model",
        provider_kind="openai",
        endpoint="https://api.example.com",
        model_id="test-model",
        credential_ref="topos:profile-test",
    )
    if multimodal:
        profile = replace(
            profile,
            modalities=("text", "image"),
            capabilities=("language", "vision_understanding"),
        )
    return profile


class _FakeRegistry:
    """最小 ProfileRegistry 替身（可注入 language 路由的差异化 profile）。"""

    def __init__(self, profile, language_profile=None):
        self.active_profile = profile
        self._language_profile = language_profile

    def get(self, pid):  # noqa: ARG002
        return self.active_profile

    def set_active(self, pid):  # noqa: ARG002
        pass

    def save_to_user_file(self):
        pass

    def active_profile_for_capability(self, capability):
        return self._language_profile if capability == "language" else None


class _FakeCredentialStore:
    def get_secret(self, ref):  # noqa: ARG002
        return "sk-fake-test-key"


def _make_panel() -> MagicMock:
    """记录渲染入口的 fake panel（所有 signal connect/disconnect 为 no-op）。"""
    panel = MagicMock()
    sig_names = (
        "request_submitted", "variant_selected", "apply_requested",
        "cancel_requested", "brief_patch_requested", "context_inspector_requested",
        "analyze_context_requested", "composer_permission_changed",
        "composer_profile_changed", "composer_mode_changed",
        "composer_reasoning_changed", "context_compress_requested",
        "new_session_requested", "show_history_requested", "clear_history_requested",
        "history_session_requested", "conversation_title_change_requested",
        "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
        "skill_start_requested", "result_action_triggered",
        "show_workflow_requested", "analysis_confirm_requested",
        "analysis_cancel_requested", "draft_changed",
    )
    for name in sig_names:
        signal = getattr(panel, name)
        signal.connect = MagicMock()
        signal.disconnect = MagicMock()
    return panel


def _make_service() -> MagicMock:
    from src.features.edit.ai_editing.assistant_events import AssistantMessage

    service = MagicMock()
    service.on_state_changed = None
    service.active_brief_id = "default"
    service.config = MagicMock()
    messages: list[AssistantMessage] = []

    def _append(role, text, *, error_code=None, attachments=()):
        messages.append(AssistantMessage(
            message_id=f"m{len(messages)}", role=role, text=text,
            error_code=error_code, attachments=attachments,
        ))

    service.append_chat_message = _append

    def _recent(limit=10):
        return [m for m in messages if m.role in ("user", "assistant")][-limit:]

    service.recent_chat_messages = _recent
    return service


def _make_controller(
    qapp,
    *,
    profile=None,
    language_profile=None,
) -> tuple[Any, MagicMock, MagicMock]:
    """构造最小 AiEditController（Null bridge + fake panel/service）。"""
    from src.app.controllers.ai_edit_controller import AiEditController

    panel = _make_panel()
    service = _make_service()
    controller = AiEditController(
        panel=panel,
        service=service,
        profile_registry=_FakeRegistry(
            profile if profile is not None else _make_profile(),
            language_profile=language_profile,
        ),
        credential_store=_FakeCredentialStore(),
        parent=qapp,
    )
    return controller, panel, service


def _image_attachment():
    from src.features.edit.ai_editing.image_attachment import ImageAttachment

    return ImageAttachment(
        data_url="data:image/jpeg;base64,AAAA", mime="image/jpeg",
    )


# ═══════════════════════════════════════════════════════════════════════
# AUD-2001: multimodal-blocked early return finishes the drawer activity
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2001_blocked_multimodal_early_exit_finishes_activity(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 artifact 请求的附件校验失败：活动项必须终结，不留 running 幽灵。"""
    controller, panel, _service = _make_controller(
        qapp,
        # language 路由到不支持图片的模型 → _start_chat 内校验失败早退。
        language_profile=_make_profile(multimodal=False),
    )
    fake_client = MagicMock()

    def _fake_make_client(_profile, _credentials):  # noqa: ARG001
        return fake_client

    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client", _fake_make_client,
    )

    controller._pending_chat_attachments = (_image_attachment(),)
    controller._start_chat(
        "生成网页", chat_mode="teaching_html_generate",
        system_instruction="只输出 HTML",
    )

    item = controller._legacy_chat_activity_item
    assert item is not None, "早退前 _begin_legacy_chat_activity 已创建条目"
    # 修复前：status 停留 "running"、can_cancel=True（永不终结的幽灵行）。
    assert item.status == "cancelled"
    assert item.can_cancel is False
    # 终态已发布到共享 panel 的 Run Drawer。
    drawer_state = panel.set_run_drawer.call_args.args[-1]
    assert drawer_state.items[0].status == "cancelled"
    # 附件保留在暂存区供模块复用同一 request_id 的重试（旧行为不变量）。
    assert controller._pending_chat_attachments


# ═══════════════════════════════════════════════════════════════════════
# AUD-2002: non-chat submissions must not leave attachment residue
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2002_media_mode_submission_clears_pending_attachments(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """媒体生成模式不经 _start_chat：return 前清空附件暂存。"""
    from src.app.controllers import ai_edit_controller as aec
    from src.features.edit.ai_editing.assistant_events import AssistantRequest

    controller, _panel, _service = _make_controller(qapp)
    generation_calls: list[tuple] = []
    controller._request_ai_generation = (  # type: ignore[method-assign]
        lambda result, scope, text: generation_calls.append((result, scope, text))
    )
    monkeypatch.setattr(aec, "get_media_assistant_modes", lambda: frozenset({"video"}))

    controller._on_request_submitted(AssistantRequest(
        request_id="r-media", text="做一个风景视频", mode="video",
        attachments=(_image_attachment(),),
    ))

    assert len(generation_calls) == 1
    # 修复前：附件残留在 _pending_chat_attachments，泄漏进后续隔离请求。
    assert controller._pending_chat_attachments == ()


def test_aud_2002_routed_non_chat_submission_clears_pending_attachments(
        qapp) -> None:
    """router 分发的非聊天路径（skill/export 等）不消费附件：清空暂存。"""
    from src.features.edit.ai_editing.assistant_events import AssistantRequest

    controller, _panel, _service = _make_controller(qapp)
    controller.router = MagicMock()  # router 已装配
    controller._dispatch_via_router = (  # type: ignore[method-assign]
        lambda text: True
    )

    controller._on_request_submitted(AssistantRequest(
        request_id="r-skill", text="帮我导出成片", mode="chat",
        attachments=(_image_attachment(),),
    ))

    assert controller._pending_chat_attachments == ()


def test_aud_2002_legacy_brief_path_clears_pending_attachments(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 router 回退 brief/plan 路径：附件不被消费，同样清空。"""
    from src.features.edit.ai_editing.assistant_events import AssistantRequest

    controller, _panel, _service = _make_controller(qapp)
    brief_calls: list[str] = []
    monkeypatch.setattr(
        controller, "_run_legacy_brief_path",
        lambda text: brief_calls.append(text),
    )

    controller._on_request_submitted(AssistantRequest(
        request_id="r-brief", text="剪出一个精彩预告", mode="chat",
        attachments=(_image_attachment(),),
    ))

    assert brief_calls == ["剪出一个精彩预告"]
    assert controller._pending_chat_attachments == ()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2003: cancelled teaching_html_generate run is completed
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2003_chat_cancelled_completes_motion_graphic_run(qapp) -> None:
    """用户取消 HTML 生成：与 _on_chat_failed 对称，终结 V2 Motion Graphic run。"""
    controller, _panel, _service = _make_controller(qapp)
    completed: list[tuple] = []
    controller.complete_motion_graphic_run = (  # type: ignore[method-assign]
        lambda request_id, *, success=True: completed.append((request_id, success))
    )
    handler = MagicMock()
    controller._teaching_html_generate_response_handler = handler

    controller._chat_mode = "teaching_html_generate"
    controller._teaching_html_generate_request_id = "req-1"
    controller._on_chat_cancelled()

    # 修复前：cancel 路径不调 complete_motion_graphic_run，V2 run 永久 running。
    assert completed == [("req-1", False)]
    # module 兜底通知行为保持（与旧路径一致）。
    handler.assert_called_once_with("req-1", "")


def test_aud_2003_chat_cancelled_pops_v2_mg_run_mapping(qapp) -> None:
    """端到端：取消后 _v2_mg_runs 映射被 complete_motion_graphic_run 清除。"""
    controller, _panel, _service = _make_controller(qapp)
    controller._chat_mode = "teaching_html_generate"
    controller._teaching_html_generate_request_id = "req-1"
    controller._v2_mg_runs = {"req-1": "run-1"}
    controller._v2_orchestrator = None  # 无编排器 → pop 后安全早退

    controller._on_chat_cancelled()

    assert "req-1" not in controller._v2_mg_runs


# ═══════════════════════════════════════════════════════════════════════
# AUD-2004: dispose clears worker refs / drops late signals
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2004_dispose_clears_worker_refs_and_drops_late_signals(qapp) -> None:
    """dispose 清 worker 引用；迟到的 cancelled 信号被身份检查丢弃。"""
    from src.app.controllers.ai_edit_controller import _ChatWorker

    controller, panel, _service = _make_controller(qapp)
    late_worker = _ChatWorker(MagicMock(), [], parent=controller)
    controller._chat_worker = late_worker
    controller._legacy_chat_activity_item = SimpleNamespace(
        label="x", status="running", can_cancel=True, progress=0.0,
        output_summary="", duration_ms=0,
    )

    controller.dispose()

    assert controller._chat_worker is None
    assert controller._plan_worker is None
    assert controller._subtitle_plan_worker is None
    # dispose 后迟到的排队信号：不得再向共享 panel 发布旧会话状态。
    panel.set_run_drawer.reset_mock()
    controller._legacy_chat_activity_item = SimpleNamespace(
        label="x", status="running", can_cancel=True, progress=0.0,
        output_summary="", duration_ms=0,
    )
    controller._on_chat_cancelled(late_worker)
    panel.set_run_drawer.assert_not_called()


def test_aud_2004_dispose_removes_runtime_subscriber(qapp) -> None:
    """dispose 移除 HarnessRuntime 事件订阅（迟到 run 事件不再驱动本 controller）。"""
    controller, _panel, _service = _make_controller(qapp)
    subscribers: list[Any] = []

    class _FakeRuntime:
        def add_subscriber(self, fn):
            subscribers.append(fn)
            return lambda: subscribers.remove(fn) if fn in subscribers else None

    controller.runtime = _FakeRuntime()  # type: ignore[assignment]
    controller._runtime_unsubscribe = controller.runtime.add_subscriber(
        controller._on_run_event
    )
    assert subscribers == [controller._on_run_event]

    controller.dispose()

    assert subscribers == []


# ═══════════════════════════════════════════════════════════════════════
# AUD-2005: plan worker is released via finished → deleteLater
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2005_plan_worker_delete_later_on_finish(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """_start_plan 创建的 worker 在 run() 结束后被 deleteLater 释放。"""
    controller, _panel, service = _make_controller(qapp)
    monkeypatch.delenv("TOPOS_TEST_MODE")  # 走真实 _PlanWorker 路径
    service.plan_variants = MagicMock(return_value=None)

    controller._start_plan(SimpleNamespace(), MagicMock())

    worker = controller._plan_worker
    assert worker is not None and worker.isRunning()
    destroyed: list[bool] = []
    worker.destroyed.connect(lambda _obj=None: destroyed.append(True))
    worker.wait(5000)
    qapp.processEvents()
    qapp.processEvents()

    assert destroyed, "worker 应经 finished → deleteLater 释放（不再随 controller 存活）"


def test_aud_2005_chat_worker_delete_later_on_finish(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """_start_chat 创建的 chat worker 在 run() 结束后被 deleteLater 释放。"""
    controller, _panel, _service = _make_controller(qapp)
    monkeypatch.delenv("TOPOS_TEST_MODE")
    fake_client = MagicMock()
    fake_client.complete.return_value = "ok"
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda _profile, _credentials: fake_client,
    )

    controller._pending_chat_attachments = ()
    # teaching_scene_edit 保持 legacy 管线（绕开 V2 chat 分支）。
    controller._start_chat(
        "分析场景", chat_mode="teaching_scene_edit", system_instruction="JSON",
    )

    worker = controller._chat_worker
    assert worker is not None
    destroyed: list[bool] = []
    worker.destroyed.connect(lambda _obj=None: destroyed.append(True))
    worker.wait(5000)
    qapp.processEvents()
    qapp.processEvents()

    assert destroyed, "chat worker 应经 finished → deleteLater 释放"


# ═══════════════════════════════════════════════════════════════════════
# AUD-2006: drawer cancel falls through to the V2 orchestrator
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2006_run_cancel_dispatches_to_v2_orchestrator(qapp) -> None:
    """runtime.cancel 未知 id（V2 run）→ 分派到 AgentOrchestrator.request_cancel。"""
    controller, _panel, _service = _make_controller(qapp)
    runtime = MagicMock()
    runtime.cancel.return_value = False  # HarnessRuntime 对 V2 run 返回 False
    controller.runtime = runtime
    controller._materialization_job_ids = {}
    orchestrator = MagicMock()
    controller._v2_orchestrator = orchestrator

    controller._on_run_cancel_requested("v2-run-9")

    runtime.cancel.assert_called_once_with("v2-run-9")
    orchestrator.request_cancel.assert_called_once_with(
        "v2-run-9", reason="用户取消",
    )


def test_aud_2006_run_cancel_runtime_hit_skips_v2(qapp) -> None:
    """runtime 命中（legacy run）时不误触发 V2 取消。"""
    controller, _panel, _service = _make_controller(qapp)
    runtime = MagicMock()
    runtime.cancel.return_value = True
    controller.runtime = runtime
    controller._materialization_job_ids = {}
    orchestrator = MagicMock()
    controller._v2_orchestrator = orchestrator

    controller._on_run_cancel_requested("legacy-run-1")

    runtime.cancel.assert_called_once_with("legacy-run-1")
    orchestrator.request_cancel.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2007: subtitle planner consumes a main-thread deep snapshot
# ═══════════════════════════════════════════════════════════════════════


def _subtitle_state() -> Any:
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences

    project = SimpleNamespace(
        project_id="p1", active_timeline_id="t1",
        assets={"a1": SimpleNamespace(display_name="A", path="/x/a.mp4")},
    )
    seq_t1 = SimpleNamespace(timeline_id="t1", revision=4, markers=[])
    seq_t2 = SimpleNamespace(timeline_id="t2", revision=1, markers=[])
    return ProjectSequences(project=project, sequences={"t1": seq_t1, "t2": seq_t2})


def test_aud_2007_deep_copy_state_for_worker_is_private_snapshot() -> None:
    """快照只含目标 timeline，且与 live 对象图完全隔离。"""
    from src.app.controllers.ai_edit_controller import _deep_copy_state_for_worker

    state = _subtitle_state()
    snapshot = _deep_copy_state_for_worker(state, "t1")

    assert set(snapshot.sequences) == {"t1"}
    assert snapshot.sequences["t1"] is not state.sequences["t1"]
    assert snapshot.project is not state.project
    assert snapshot.project.assets["a1"] is not state.project.assets["a1"]
    # 主线程后续变异不影响 worker 的副本。
    state.sequences["t1"].revision = 99
    state.project.assets["a2"] = SimpleNamespace(display_name="B", path="")
    assert snapshot.sequences["t1"].revision == 4
    assert "a2" not in snapshot.project.assets


def test_aud_2007_subtitle_worker_receives_snapshot_not_live_state(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """_start_subtitle_edit_agent 把深快照传给 worker，不传 live state。"""
    from src.app.controllers import ai_edit_controller as aec

    controller, _panel, _service = _make_controller(qapp)
    monkeypatch.delenv("TOPOS_TEST_MODE")  # 走 worker 构造路径
    monkeypatch.setattr(
        "src.features.edit.ai_editing.llm_client.make_client",
        lambda _profile, _credentials: MagicMock(),
    )
    state = _subtitle_state()

    class _FakeBridge:
        def get_project_sequences(self):
            return state

    controller._bridge = _FakeBridge()  # type: ignore[assignment]

    captured: dict[str, Any] = {}

    class _CapturingWorker:
        def __init__(self, client, request, worker_state, *, project_id,
                     timeline_id, parent=None):
            captured["state"] = worker_state
            captured["timeline_id"] = timeline_id
            self.finished_plan = MagicMock()
            self.failed = MagicMock()
            self.finished = MagicMock()
            self.deleteLater = MagicMock()

        def start(self):
            pass

    monkeypatch.setattr(aec, "_SubtitlePlanWorker", _CapturingWorker)

    controller._start_subtitle_edit_agent(
        "把字幕改大一号", SimpleNamespace(project_id="p1", timeline_id="t1"),
    )

    worker_state = captured.get("state")
    assert worker_state is not None
    # worker 收到的是深快照：与 live sequence 不是同一对象，且其余 timeline
    # 不进入 worker（避免后台遍历 live ProjectSequences）。
    assert worker_state.sequences["t1"] is not state.sequences["t1"]
    assert set(worker_state.sequences) == {"t1"}


# ═══════════════════════════════════════════════════════════════════════
# AUD-2008: dispose does not wait for uninterruptible MG workers
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2008_dispose_does_not_block_on_mg_postprocess_worker(qapp) -> None:
    """Chromium 后处理不可中断：dispose 置 cancel 标志立即返回，不逐个 wait。"""
    import src.app.controllers.ai_edit_controller as aec
    from src.app.controllers.ai_edit_controller import _DETACHED_WORKERS

    controller, _panel, _service = _make_controller(qapp)
    release = threading.Event()

    def _processor(_request_id, _response):
        release.wait(10)
        return {}

    worker = aec._MotionGraphicPostprocessWorker(
        "req-mg", "<html></html>", _processor, parent=controller,
    )
    controller._motion_graphic_postprocess_workers["req-mg"] = worker
    worker.start()

    started = time.perf_counter()
    controller.dispose()
    elapsed = time.perf_counter() - started

    # 修复前：串行 wait(2000) → dispose 冻结 ≈2s。现在立即返回。
    assert elapsed < 1.5, f"dispose 在不可中断 worker 上阻塞了 {elapsed:.2f}s"
    assert worker.cancel_requested is True
    # worker 已与 controller 解耦（摘除 parent、由 detached 集合持有）。
    assert worker.parent() is None
    assert worker in _DETACHED_WORKERS
    assert controller._motion_graphic_postprocess_workers == {}

    # 让线程自然结束：detached 集合必须回收，不无界增长。
    release.set()
    worker.wait(5000)
    qapp.processEvents()
    qapp.processEvents()
    assert worker not in _DETACHED_WORKERS


# ═══════════════════════════════════════════════════════════════════════
# AUD-2100: Null bridge satisfies the runtime-checkable protocol
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2100_null_bridge_implements_full_protocol() -> None:
    """NullAiEditHostBridge 补齐 detect_autonomous_replay / rollback_ai_command。"""
    from src.app.controllers.ai_edit_host_bridge import (
        AiEditHostBridge,
        NullAiEditHostBridge,
    )
    from src.features.edit.ai_editing.models import EditPlan, PlanTarget

    bridge = NullAiEditHostBridge()
    assert isinstance(bridge, AiEditHostBridge), (
        "@runtime_checkable 协议一致性：Null 对象必须满足自己宣称实现的协议"
    )

    plan = EditPlan.create(PlanTarget("p1", "t1"), base_revision=0, operations=())
    # 未装配宿主：不可能有已提交计划 → 保守 known=False（修订门 fail-closed）。
    assert bridge.detect_autonomous_replay(plan, None) == {"known": False}
    # 未装配宿主无法证明 undo 目标 → None（调用方保守中止）。
    assert bridge.rollback_ai_command(
        "t1", expected_undo_label="AI", expected_revision=3,
    ) is None


# ═══════════════════════════════════════════════════════════════════════
# AUD-2103 / AUD-2104: AI export timeline binding + silent-cancel handling
# ═══════════════════════════════════════════════════════════════════════


def _export_sequence(timeline_id: str = "t1") -> Any:
    from src.shared.media.metadata import Rational

    try:
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

        return TimelineSequence(
            timeline_id=timeline_id, name="Main", fps=Rational(24, 1),
            video_tracks=(
                EditTrack(track_id="v1", name="V1", track_type="video", index=0),
            ),
        )
    except TypeError:
        # EditTrack 构造签名变化时的兜底（保持测试可运行）。
        return SimpleNamespace(
            timeline_id=timeline_id, name="Main", fps=Rational(24, 1),
            revision=0, all_tracks=(), subtitle_tracks=(),
        )


class _ExportFakeMW:
    """request_ai_export 需要的最小宿主（含导出启动状态观测点）。"""

    def __init__(self, *, active_timeline_id: str, export_started: bool):
        sequence = _export_sequence("t1")
        self._current_edit_project = SimpleNamespace(
            project_id="p1", active_timeline_id="t1", assets={},
        )
        self._current_timeline_sequences = {"t1": sequence}
        # AUD-2103 的观测点：Edit 页当前打开的时间线（可与 t1 不同）。
        self.edit_page = SimpleNamespace(
            _current_timeline=SimpleNamespace(timeline_id=active_timeline_id),
        )
        from src.shared.feature_flags import FeatureFlags

        self.feature_flags = FeatureFlags(
            generation=True, agent_auto_mode=True, cloud_upload=True,
            auto_export=True,
        )
        self.timeline_panel = None
        self._video_export_started_at = None
        self._video_exporter = None
        self.export_calls: list[dict] = []
        self._export_started = export_started

    def perform_video_export(self, settings: dict) -> None:
        self.export_calls.append(dict(settings))
        if self._export_started:
            # 宿主接受导出的可观测状态（deliver_controller.py:1048/1056）。
            self._video_export_started_at = time.perf_counter()


def _run_request_ai_export(
        monkeypatch: pytest.MonkeyPatch, mw: _ExportFakeMW,
) -> tuple[bool, str]:
    import PyQt6.QtWidgets  # noqa: F401 — monkeypatch 目标模块

    import src.features.edit.ai_editing.export_service as es_mod
    from src.app.controllers.ai_edit_host_bridge_impl import (
        MainWindowAiEditHostBridge,
    )
    from src.features.edit.ai_editing.approval_service import ApprovalService

    monkeypatch.setattr(
        "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
        lambda *a, **k: ("/tmp/ai_export_test.mp4", "MP4 Video (*.mp4)"),
    )
    monkeypatch.setattr(
        "PyQt6.QtWidgets.QMessageBox.question",
        lambda *a, **k: PyQt6.QtWidgets.QMessageBox.StandardButton.Yes,
    )
    # timeline_export 的 video_effects/fonts preflight 与 export_service 的
    # 调用约定（fn(config, sequence)）签名不一致（既有独立问题，与本批无关）：
    # 本测试聚焦导出绑定与 job 终态，预检替换为恒通过。
    monkeypatch.setattr(
        es_mod, "run_export_preflight",
        lambda *a, **k: SimpleNamespace(
            ok=True, has_blocking=False, blocking=(), degraded=(),
        ),
    )

    bridge = MainWindowAiEditHostBridge(mw, _NoBriefService(), ApprovalService())
    ok, message = bridge.request_ai_export(
        SimpleNamespace(project_id="p1", timeline_id="t1"),
        {"codec": "h264"},
    )
    return ok, message


class _NoBriefService:
    """无 brief 的 service 桩（get_brief → None，不注入交付合同字段）。"""

    active_brief_id = "default"

    def get_brief(self, brief_id):  # noqa: ARG002
        return None


def test_aud_2103_export_rejected_when_target_timeline_not_active(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """scope.timeline_id 与 Edit 页当前时间线不一致 → 确定性失败，不启动导出。"""
    mw = _ExportFakeMW(active_timeline_id="other-timeline", export_started=True)
    ok, message = _run_request_ai_export(monkeypatch, mw)

    assert ok is False
    assert "目标时间线未在编辑页打开" in message
    assert mw.export_calls == [], "宿主导出器不得被调用"


def test_aud_2104_export_job_fails_when_host_silently_declines(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主静默取消（提前 return 不抛异常）→ job 显式 FAILED，不永久 running。"""
    import src.features.edit.ai_editing.export_service as es_mod
    from src.features.edit.ai_editing.jobs import JobStatus

    captured: dict[str, Any] = {}
    real_export_service = es_mod.ExportService

    class _CapturingExportService(real_export_service):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["service"] = self

    monkeypatch.setattr(es_mod, "ExportService", _CapturingExportService)

    mw = _ExportFakeMW(active_timeline_id="t1", export_started=False)
    ok, message = _run_request_ai_export(monkeypatch, mw)

    assert ok is False
    assert "导出未启动" in message
    service = captured["service"]
    jobs = list(service.jobs._jobs.values())
    assert jobs, "job 已提交"
    export_job = jobs[-1]
    # 修复前：job 停留在 RUNNING（永不终结）。现在显式 FAILED + 错误码。
    assert export_job.status == JobStatus.FAILED
    assert export_job.error_code == "EXPORT_NOT_STARTED"


def test_aud_2104_export_accepted_when_host_starts_export(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主接受导出（写入启动状态）→ 正常返回 started。"""
    mw = _ExportFakeMW(active_timeline_id="t1", export_started=True)
    ok, message = _run_request_ai_export(monkeypatch, mw)

    assert ok is True
    assert "已启动" in message
    assert len(mw.export_calls) == 1
    assert mw.export_calls[0]["codec"] == "h264"


# ═══════════════════════════════════════════════════════════════════════
# AUD-2105: policy gate fails closed without explicit legacy opt-in
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2105_policy_fails_closed_without_legacy_opt_in() -> None:
    """缺工程 + 缺 feature_flags 的宿主 → 拒绝（不再隐式 fail-open）。"""
    from src.app.controllers.ai_edit_host_bridge_impl import (
        MainWindowAiEditHostBridge,
    )
    from src.features.edit.ai_editing.approval_service import ApprovalService

    bridge = MainWindowAiEditHostBridge(
        SimpleNamespace(), MagicMock(), ApprovalService(),
    )
    decision = bridge.check_ai_action("paid_generation")
    assert decision.allowed is False
    assert "no active project" in decision.reason


def test_aud_2105_legacy_no_policy_opt_in_keeps_test_fakes_working() -> None:
    """显式 legacy_no_policy=True（测试 fake 专用）保持旧放行行为。"""
    from src.app.controllers.ai_edit_host_bridge_impl import (
        MainWindowAiEditHostBridge,
    )
    from src.features.edit.ai_editing.approval_service import ApprovalService

    bridge = MainWindowAiEditHostBridge(
        SimpleNamespace(), MagicMock(), ApprovalService(), legacy_no_policy=True,
    )
    decision = bridge.check_ai_action("paid_generation")
    assert decision.allowed is True
    assert "legacy host" in decision.reason


# ═══════════════════════════════════════════════════════════════════════
# AUD-2200: setup failure path reuses teardown
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2200_setup_failure_path_runs_full_teardown(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """装配中途失败：复用 _teardown_ai_editing + 显式清 providers/executors。"""
    from src.app.controllers.ai_editing_mixin import AiEditingMixin

    class _Host(AiEditingMixin):
        def __init__(self):
            self._edit_available = True
            self.ai_edit_assistant_panel = MagicMock()
            self._open_ai_model_preferences = MagicMock()
            self._ai_edit_controller = MagicMock()
            self._ai_assistant_service = MagicMock()
            self._ai_host_bridge = MagicMock()
            self._ai_approval_service = MagicMock()
            self._ai_session_store = MagicMock()
            self._ai_mcp_host = MagicMock()
            self._ai_mcp_dispatcher = MagicMock()
            self._ai_mcp_socket_path = "/tmp/mcp.sock"
            self._ai_tool_facade = MagicMock()
            self._generation_providers = MagicMock()
            self._comfy_generation_executors = {"p1": MagicMock()}
            self._comfy_profile_registry = MagicMock()
            self._media_analysis_controller = None
            self._analysis_scheduler = None
            self._analysis_service = None
            self._shared_analysis_service = None
            self._model_pack_manager = None
            self._transcript_router = None
            self._tts_provider = None
            self._speech_to_text_worker = None
            self._subtitle_to_speech_worker = None
            self._current_edit_project = None

    host = _Host()
    teardown_calls: list[bool] = []
    monkeypatch.setattr(
        host, "_teardown_ai_editing",
        lambda: teardown_calls.append(True),
    )

    def _boom(self):  # noqa: ARG001
        raise RuntimeError("assembly exploded after MCP host start")

    # 装配链尾部（controller 已建、MCP host 已 start 之后）注入失败——即
    # AUD-2200 描述的"半拆除"窗口。
    monkeypatch.setattr(AiEditingMixin, "_wire_ai_persistence", _boom)

    host._setup_ai_editing()

    # 错误路径必须复用 _teardown_ai_editing（修复前只置 5 个 None，MCP socket
    # 继续对外服务、facade 指向已置 None 的装配）。>=2：装配前常规拆除 + 错误路径。
    assert len(teardown_calls) >= 2
    # teardown 不覆盖的 provider/executor 残留也被显式清空。
    assert host._generation_providers is None
    assert host._comfy_generation_executors == {}
    assert host._comfy_profile_registry is None
    assert host._ai_edit_controller is None
    assert host._ai_assistant_service is None
    assert host._ai_host_bridge is None
    assert host._ai_session_store is None


# ═══════════════════════════════════════════════════════════════════════
# AUD-2210: single-frame renderer cleanup
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2210_render_single_frame_calls_cleanup_always(monkeypatch) -> None:
    """渲染成功与异常路径都必须调用 renderer.cleanup()（GL 资源显式释放）。"""
    import src.features.composite.render.composite_frame_renderer as cfr_mod
    from src.app.controllers.ai_workflow_composite_render import (
        _render_single_frame,
    )

    cleanup_calls: list[bool] = []

    class _FakeRenderer:
        def render_comp_clip_frame(self, *_args, **_kwargs):
            return "frame"

        def cleanup(self):
            cleanup_calls.append(True)

    monkeypatch.setattr(cfr_mod, "CompositeFrameRenderer", _FakeRenderer)

    result = _render_single_frame(None, {"nodes": []}, None, None)
    assert result == "frame"
    assert cleanup_calls == [True]

    class _RaisingRenderer(_FakeRenderer):
        def render_comp_clip_frame(self, *_args, **_kwargs):
            raise RuntimeError("GL context lost")

    monkeypatch.setattr(cfr_mod, "CompositeFrameRenderer", _RaisingRenderer)
    with pytest.raises(RuntimeError):
        _render_single_frame(None, {"nodes": []}, None, None)
    # finally 保证异常路径同样释放。
    assert cleanup_calls == [True, True]


# ═══════════════════════════════════════════════════════════════════════
# AUD-2211: worker consumes the main-thread reference snapshot only
# ═══════════════════════════════════════════════════════════════════════


def _composite_invocation(graph: dict) -> SimpleNamespace:
    return SimpleNamespace(
        inputs={"composite_graph": {"_kind": "composite_graph_snapshot",
                                    "graph": graph}},
        scope=SimpleNamespace(selected_asset_ids=()),
        skill_id="topos.composite_render",
        invocation_id="inv-1",
    )


def test_aud_2211_resolver_captures_render_context_references() -> None:
    """resolver（主线程）把工程/时间线/配置引用捕获进快照。"""
    from src.app.controllers.ai_workflow_composite_render import (
        make_exposed_input_resolver,
    )

    project = SimpleNamespace(name="proj")
    sequences = {"t1": SimpleNamespace(name="seq")}
    config = SimpleNamespace(name="cfg")
    main_window = SimpleNamespace(
        _current_edit_project=project,
        _current_timeline_sequences=sequences,
        config_manager=config,
        composite_page=SimpleNamespace(
            get_composite_data=lambda: {"nodes": [{"id": "n1"}]},
        ),
    )
    invocation = _composite_invocation({})
    graph = SimpleNamespace(
        exposed_inputs=[("composite_graph", "graph", {})],
    )

    make_exposed_input_resolver(main_window)(invocation, graph)

    envelope = invocation.inputs["composite_graph"]
    context = envelope["render_context"]
    assert context.edit_project is project
    assert context.timeline_sequences is sequences
    assert context.config_manager is config


def test_aud_2211_provider_reads_snapshot_not_live_window(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 只消费快照引用：主窗口后续切换工程不影响进行中的渲染。"""
    import src.app.controllers.ai_workflow_composite_render as acr
    from src.app.controllers.ai_workflow_composite_render import (
        make_composite_render_provider,
        make_exposed_input_resolver,
    )

    project_v1 = SimpleNamespace(name="project-v1")
    main_window = SimpleNamespace(
        _current_edit_project=project_v1,
        _current_timeline_sequences={"t1": object()},
        config_manager=None,
        composite_page=SimpleNamespace(
            get_composite_data=lambda: {"nodes": [{"id": "n1"}]},
        ),
        _ai_host_bridge=None,
    )
    invocation = _composite_invocation({"nodes": [{"id": "n1"}]})
    graph = SimpleNamespace(exposed_inputs=[("composite_graph", "graph", {})])

    # 主线程 resolver 捕获引用。
    make_exposed_input_resolver(main_window)(invocation, graph)

    captured: dict[str, Any] = {}

    def _fake_render_headless(graph_data, edit_project, timeline_sequences,
                              config_manager, is_cancelled):
        captured["edit_project"] = edit_project
        captured["timeline_sequences"] = timeline_sequences
        return "/tmp/out.png", "image", None

    monkeypatch.setattr(acr, "_render_headless", _fake_render_headless)

    # 主线程在 worker 渲染前切换/关闭工程（活读旧代码会拿到 None / 新引用）。
    main_window._current_edit_project = None
    main_window._current_timeline_sequences = None

    def _noop_register(*_args, **_kwargs):
        return "gen-comp-1"

    monkeypatch.setattr(acr, "_register_generation_candidate", _noop_register)

    provider = make_composite_render_provider(main_window)
    artifact = provider(invocation, None)

    assert artifact is not None
    # worker 消费的是快照捕获的旧引用，而不是主窗口当前值（None）。
    assert captured["edit_project"] is project_v1
    assert captured["timeline_sequences"] is not None


def test_aud_2211_provider_degrades_without_render_context() -> None:
    """快照缺 render_context（resolver 未运行）→ 诚实降级 None，不活读主窗口。"""
    from src.app.controllers.ai_workflow_composite_render import (
        make_composite_render_provider,
    )

    main_window = SimpleNamespace(_current_edit_project=object())
    invocation = _composite_invocation({"nodes": [{"id": "n1"}]})
    invocation.inputs["composite_graph"].pop("render_context", None)

    provider = make_composite_render_provider(main_window)
    assert provider(invocation, None) is None


# ═══════════════════════════════════════════════════════════════════════
# AUD-2212: candidate insertion marshalled to the GUI thread
# ═══════════════════════════════════════════════════════════════════════


def _generation_bin_host(main_window: Any) -> Any:
    from src.features.edit.ai_editing.generation_bin import GenerationBin

    bin_ = GenerationBin()
    main_window._ai_host_bridge = SimpleNamespace(
        _generation_service=SimpleNamespace(generation_bin=bin_),
    )
    return bin_


def test_aud_2212_candidate_insertion_is_queued_to_gui_thread(qapp) -> None:
    """Qt 宿主：worker 线程入箱经信号 queued 投递 GUI 线程，处理后生效。"""
    from PyQt6.QtCore import QObject

    from src.app.controllers.ai_workflow_composite_render import (
        _make_bin_insert_marshaler,
        _register_generation_candidate,
    )

    main_window = QObject()
    bin_ = _generation_bin_host(main_window)
    marshaler = _make_bin_insert_marshaler(main_window)
    assert marshaler is not None
    invocation = SimpleNamespace(
        invocation_id="inv-1", scope=SimpleNamespace(selected_asset_ids=()),
        skill_id="topos.composite_render",
    )

    # 从 worker 线程注册（模拟生产：provider 在 editorial worker 线程执行）。
    worker = threading.Thread(target=lambda: _register_generation_candidate(
        main_window, "/tmp/out.png", "image", {"nodes": []}, invocation, None,
        bin_marshaler=marshaler,
    ))
    worker.start()
    worker.join()

    # 信号已 emit 但 GUI 线程尚未处理 → worker 线程没有同步写入（无锁 dict
    # 不被跨线程触碰）；GUI 线程处理事件后写入生效。
    assert bin_.count == 0
    qapp.processEvents()
    assert bin_.count == 1
    assert bin_.get(bin_.all_assets()[0].asset_id) is not None


def test_aud_2212_headless_host_falls_back_to_synchronous_insertion() -> None:
    """非 Qt 宿主（headless 测试）：回退同步写入（无并发读侧，安全）。"""
    from src.app.controllers.ai_workflow_composite_render import (
        _make_bin_insert_marshaler,
        _register_generation_candidate,
    )

    main_window = SimpleNamespace()
    bin_ = _generation_bin_host(main_window)
    assert _make_bin_insert_marshaler(main_window) is None
    invocation = SimpleNamespace(
        invocation_id="inv-1", scope=SimpleNamespace(selected_asset_ids=()),
        skill_id="topos.composite_render",
    )

    asset_id = _register_generation_candidate(
        main_window, "/tmp/out.png", "image", {"nodes": []}, invocation, None,
    )

    assert asset_id
    assert bin_.count == 1


# ═══════════════════════════════════════════════════════════════════════
# AUD-2213: provider-wide exception guard + resolution fallback
# ═══════════════════════════════════════════════════════════════════════


def test_aud_2213_provider_returns_none_when_render_raises(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """渲染异常被 provider 收敛为 None（失败返回 None 契约），不逃逸。"""
    import src.app.controllers.ai_workflow_composite_render as acr
    from src.app.controllers.ai_workflow_composite_render import (
        _CompositeRenderContext,
        make_composite_render_provider,
    )

    main_window = SimpleNamespace()
    invocation = _composite_invocation({"nodes": [{"id": "n1"}]})
    invocation.inputs["composite_graph"]["render_context"] = (
        _CompositeRenderContext()
    )

    def _raising_render_headless(*_args, **_kwargs):
        raise ValueError("bad graph payload")

    monkeypatch.setattr(acr, "_render_headless", _raising_render_headless)

    provider = make_composite_render_provider(main_window)
    artifact = provider(invocation, None)
    assert artifact is None


def test_aud_2213_invalid_resolution_falls_back_to_default() -> None:
    """Output 节点 width/height 非数字 → 回退 1920x1080（int() 不再逃逸）。"""
    from src.app.controllers.ai_workflow_composite_render import (
        _resolve_output_resolution,
    )

    graph = {"nodes": [{
        "type": "output",
        "parameters": {"format_mode": "custom", "width": "hd", "height": "fhd"},
    }]}
    assert _resolve_output_resolution(graph) == (1920, 1080)

    graph_ok = {"nodes": [{
        "type": "output",
        "parameters": {"format_mode": "custom", "width": 3840, "height": 2160},
    }]}
    assert _resolve_output_resolution(graph_ok) == (3840, 2160)
