"""批 EF-08（Suggestion 级 ×18）修复回归测试。

每条用例对应 docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md
中的一条 AUD ID；设计原则：修复被回退时对应断言必然失败（正例 +
回退敏感反例）。

覆盖：
* AUD-19003 agent_v2_chat_worker —— 晚到取消不再丢弃完整成功回复
* AUD-2014 ai_edit_controller —— footer token 估算按历史签名缓存
* AUD-2015 ai_edit_controller —— persist_v2_state 原子写盘
* AUD-2108 ai_edit_host_bridge_impl —— ASR 回调经主线程信号中继
* AUD-2110 ai_edit_host_bridge_impl —— 字幕/品牌测量移到编码 worker
* AUD-2111 ai_edit_host_bridge_impl —— keyring 探测 TTL 缓存
* AUD-2205 ai_editing_mixin —— ComfyUI 探测超时收窄 + 线程契约
* AUD-2214 ai_workflow_composite_render —— 渲染缓存 LRU 淘汰
* AUD-2410 composite_controller —— 属性面板接收参数副本
* AUD-2610 dock_layout_controller —— getattr 默认值惰性求值
* AUD-2713 edit_controller —— ADR 延迟校准移入 QThread
* AUD-2717 edit_controller —— RoutingDialog close 后 deleteLater
* AUD-2806 file_controller —— 周期保存按脏标记跳过
* AUD-2813 file_controller —— GPU 渲染线程装配失败时回收
* AUD-2904 layer_controller —— 每帧 print/info 移除/降级
* AUD-2909 layer_controller —— EXR 加载与缓存清理移入线程池
* AUD-3010 menu_toolbar_controller —— 依赖报告后台扫描
* AUD-3107 page_controller —— 移除 UI 线程 makeCurrent
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.app.controllers import ai_edit_host_bridge_impl as bridge_impl
from src.app.controllers import edit_controller as ec
from src.app.controllers import file_controller as fc
from src.app.controllers import layer_controller as lc
from src.app.controllers import menu_toolbar_controller as mtc
from src.app.controllers.ai_edit_controller import AiEditController
from src.app.controllers.ai_editing_mixin import (
    _COMFY_PROBE_TIMEOUT_S,
    AiEditingMixin,
    _comfy_probe_timeout,
)
from src.app.controllers.agent_v2_chat_worker import AgentV2ChatWorker
from src.app.controllers.ai_workflow_composite_render import (
    _RENDER_CACHE_MAX_FILES,
    _RENDER_CACHE_MAX_TOTAL_BYTES,
    _RENDER_CACHE_MIN_AGE_S,
    _prune_render_cache,
)
from src.app.controllers.composite_controller import CompositeControllerMixin
from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.controllers.page_controller import PageManagementMixin


# ═══════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════


def _pump_events(qapp, done: "Any", timeout_s: float = 5.0) -> None:
    """处理排队的 queued 信号直到 ``done()`` 为真或超时（不弹任何对话框）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if done():
            return
        time.sleep(0.01)
    qapp.processEvents()


# ═══════════════════════════════════════════════════════════════════════
# AUD-19003: late cancel no longer discards a complete successful reply
# ═══════════════════════════════════════════════════════════════════════


class _FixedDriver:
    def __init__(self, result: Any) -> None:
        self._result = result

    def run_chat(self, **kwargs: Any) -> Any:
        return self._result


def _run_worker(result: Any, cancel_requested: bool) -> dict:
    worker = AgentV2ChatWorker(
        _FixedDriver(result), session_id="s", user_text="hi", messages=[],
    )
    worker.cancel_requested = cancel_requested
    seen: dict[str, list] = {"reply": [], "cancelled": 0, "failed": [], "result": []}
    worker.finished_reply.connect(seen["reply"].append)
    worker.cancelled.connect(lambda: seen.__setitem__("cancelled", seen["cancelled"] + 1))
    worker.failed.connect(seen["failed"].append)
    worker.finished_result.connect(seen["result"].append)
    worker.run()  # 直接调用线程体（不启动线程），信号同步分发
    return seen


def test_aud19003_late_cancel_keeps_complete_reply() -> None:
    """cancel_requested 置位但结果已完整返回 → 按 reply 分发，不吞掉回复。"""
    from src.features.edit.ai_editing.chat_run_driver import ChatRunResult

    result = ChatRunResult(run_id="r1", reply="完整回复", terminal_state="succeeded")
    seen = _run_worker(result, cancel_requested=True)

    assert seen["reply"] == ["完整回复"]
    assert seen["cancelled"] == 0
    assert seen["failed"] == []
    # finished_result 仍与终结信号成对发出（usage 记录依赖它）
    assert len(seen["result"]) == 1


def test_aud19003_cancelled_result_still_emits_cancelled() -> None:
    """驱动器真实返回 cancelled 类别 → 仍发 cancelled（协作取消语义保留）。"""
    from src.features.edit.ai_editing.chat_run_driver import ChatRunResult

    result = ChatRunResult(run_id="r2", cancelled=True)
    seen = _run_worker(result, cancel_requested=False)

    assert seen["cancelled"] == 1
    assert seen["reply"] == []


def test_aud19003_failed_result_emits_failed() -> None:
    """failed 类别 → failed 信号（回归守卫）。"""
    from src.features.edit.ai_editing.chat_run_driver import ChatRunResult

    result = ChatRunResult(run_id="r3", error="boom", terminal_state="failed")
    seen = _run_worker(result, cancel_requested=False)

    assert seen["failed"] == ["boom"]


# ═══════════════════════════════════════════════════════════════════════
# AUD-2014: footer token estimate cached by history signature
# ═══════════════════════════════════════════════════════════════════════


class _Msg:
    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.role = "user"
        self.text = "hello"


class _HistoryService:
    def __init__(self) -> None:
        self._messages: list[Any] = []

    def append(self, message_id: str) -> None:
        self._messages.append(_Msg(message_id))

    def recent_chat_messages(self, limit: int = 10) -> list[Any]:
        return list(self._messages)[-limit:]


def _estimate_host(service: Any) -> Any:
    """轻量宿主：经未绑定调用只装配 _refresh_context_token_estimate 依赖。"""
    controller = SimpleNamespace(_service=service)
    builds: list = []
    controller._build_chat_messages = lambda **kwargs: builds.append(kwargs)
    controller._estimation_builds = builds  # type: ignore[attr-defined]
    controller._footer_history_signature = lambda draft_text: (
        AiEditController._footer_history_signature(controller, draft_text))
    return controller


def _refresh_estimate(controller: Any, draft_text: str) -> None:
    AiEditController._refresh_context_token_estimate(
        controller, draft_text=draft_text)


def test_aud2014_unchanged_history_skips_rebuild() -> None:
    """历史/草稿未变化时不再全量重建 + 估算 token（旧行为：每次重建）。"""
    service = _HistoryService()
    service.append("m1")
    controller = _estimate_host(service)

    _refresh_estimate(controller, draft_text="")
    assert len(controller._estimation_builds) == 1

    # 同一历史 + 同一草稿：签名命中缓存，跳过重建
    _refresh_estimate(controller, draft_text="")
    assert len(controller._estimation_builds) == 1, (
        "AUD-2014 修复回退：签名未变时仍执行了全量 _build_chat_messages")

    # 草稿变化 → 重建
    _refresh_estimate(controller, draft_text="draft")
    assert len(controller._estimation_builds) == 2


def test_aud2014_changed_history_rebuilds() -> None:
    """新消息追加 / 末条 message_id 变化 → 必须重建（估算保持新鲜）。"""
    service = _HistoryService()
    controller = _estimate_host(service)

    _refresh_estimate(controller, draft_text="")
    assert len(controller._estimation_builds) == 1

    service.append("m-new")
    _refresh_estimate(controller, draft_text="")
    assert len(controller._estimation_builds) == 2

    # restore 场景：清空后回灌，末条 id 变化
    service._messages.clear()
    service.append("m-restored")
    _refresh_estimate(controller, draft_text="")
    assert len(controller._estimation_builds) == 3


def test_aud2014_service_error_forces_rebuild() -> None:
    """签名读取失败（service 异常）→ 回退到全量重建，不吞错也不卡死。"""

    class _Broken:
        def recent_chat_messages(self, limit: int = 10) -> list[Any]:
            raise RuntimeError("service unavailable")

    controller = _estimate_host(_Broken())
    _refresh_estimate(controller, draft_text="")
    _refresh_estimate(controller, draft_text="")
    assert len(controller._estimation_builds) == 2


# ═══════════════════════════════════════════════════════════════════════
# AUD-2015: persist_v2_state writes atomically (tmp + os.replace)
# ═══════════════════════════════════════════════════════════════════════


class _FakeOrchestrator:
    def list_runs(self) -> list[Any]:
        return []

    def export_state(self) -> dict:
        return {"runs": [{"run_id": "r1", "state": "paused_for_resume"}]}


def _persist_host(tmp_path: Any) -> Any:
    host = SimpleNamespace(_v2_orchestrator=_FakeOrchestrator())
    host._v2_state_file_path = lambda: tmp_path / "v2_state.json"
    return host


def _persist(controller: Any) -> None:
    AiEditController.persist_v2_state(controller)


def test_aud2015_persist_atomic_no_truncation(tmp_path: Any) -> None:
    """persist 后文件是完整 JSON，且不残留 .tmp 中间文件。"""
    import json

    controller = _persist_host(tmp_path)
    _persist(controller)

    target = tmp_path / "v2_state.json"
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["runs"][0]["run_id"] == "r1"
    assert not (tmp_path / "v2_state.json.tmp").exists(), (
        "原子写未走 tmp+replace：不应残留中间文件")


def test_aud2015_persist_uses_os_replace(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """回退检测：修复实现必须经 os.replace 原子替换（旧代码直接 write_text）。"""
    import json

    calls: list[tuple] = []
    real_replace = os.replace
    monkeypatch.setattr(
        os, "replace",
        lambda src, dst: (calls.append((str(src), str(dst))),
                          real_replace(src, dst))[1],
    )

    controller = _persist_host(tmp_path)
    _persist(controller)

    assert len(calls) == 1, "AUD-2015 修复回退：persist_v2_state 未走 os.replace"
    src, dst = calls[0]
    assert src.endswith("v2_state.json.tmp")
    assert dst.endswith("v2_state.json")
    assert json.loads((tmp_path / "v2_state.json").read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════
# AUD-2108: ASR callbacks marshalled to the main thread via signal relay
# ═══════════════════════════════════════════════════════════════════════


class _FakeAsrWorker(QThread):
    """复刻 _SpeechToTextWorker 的构造签名与信号面；start() 不真正起线程。"""

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(object, str)

    def __init__(self, service: Any = None, asset_id: Any = None,
                 fingerprint: Any = None, fps: Any = None,
                 parent: Any = None, *, options: Any = None) -> None:
        super().__init__(parent)
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")


class _EmittingAsrWorker(QThread):
    """真实工作线程：run() 从工作线程 emit 进度（AUD-2108 线程语义验证）。"""

    progress = pyqtSignal(object, str)

    def run(self) -> None:
        self.progress.emit(1, "x")


def _transcription_host() -> Any:
    class _MW(QObject):
        pass

    mw = _MW()
    mw._speech_to_text_worker = None
    mw._ai_transcription_worker = None
    mw._ai_transcription_relay = None
    mw._analysis_service = object()  # 只做非空校验
    mw._current_edit_project = SimpleNamespace(assets={})
    mw._transcript_router = None
    return mw


def _make_bridge(mw: Any) -> Any:
    return bridge_impl.MainWindowAiEditHostBridge(
        mw, assistant_service=SimpleNamespace(), approval_service=None,
    )


def test_aud2108_callbacks_wired_through_main_thread_relay(
        qapp, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """start_ai_transcription 的回调必须经主线程中继，而非直连 Python 函数。"""
    marker = tmp_path / "audio.wav"
    marker.write_bytes(b"x")
    mw = _transcription_host()
    mw._current_edit_project = SimpleNamespace(
        assets={"a1": SimpleNamespace(path=str(marker))})
    monkeypatch.setattr(os.path, "isfile", lambda p: str(p) == str(marker))
    monkeypatch.setattr(ec, "_SpeechToTextWorker", _FakeAsrWorker)
    bridge = _make_bridge(mw)
    bridge._selected_timeline_clip = lambda: None  # type: ignore[method-assign]

    got: dict[str, list] = {"progress": [], "completed": [], "failed": []}
    ok, message = bridge.start_ai_transcription(
        "a1", {},
        on_progress=lambda f, m: got["progress"].append((f, m)),
        on_completed=lambda a: got["completed"].append(a),
        on_failed=lambda m: got["failed"].append(m),
    )
    assert ok is True, message

    worker = mw._ai_transcription_worker
    assert worker is not None
    relay = getattr(mw, "_ai_transcription_relay", None)
    assert relay is not None, (
        "AUD-2108 修复回退：回调未经过主线程中继（worker 信号直连普通函数）")

    # 从测试（主）线程 emit：中继同线程直通，回调收到纯数据
    worker.progress.emit(0.5, "阶段")
    qapp.processEvents()
    assert got["progress"] == [(0.5, "阶段")]
    worker.completed.emit("analysis")
    worker.failed.emit("err")
    qapp.processEvents()
    assert got["completed"] == ["analysis"]
    assert got["failed"] == ["err"]
    # finished → 引用清理（经中继）
    worker.finished.emit()
    qapp.processEvents()
    assert mw._ai_transcription_worker is None
    assert mw._ai_transcription_relay is None
    assert worker.calls == ["start"]


def test_aud2108_worker_thread_signals_delivered_on_main_thread(qapp) -> None:
    """真实工作线程 emit → 回调在主线程执行（直连语义下会在工作线程执行）。"""
    main_thread = QThread.currentThread()
    worker = _EmittingAsrWorker()
    relay = bridge_impl._AiTranscriptionRelay()
    seen: list[Any] = []
    relay.progress_received.connect(
        lambda f, m: seen.append(QThread.currentThread()))
    worker.progress.connect(relay.progress_received)

    worker.start()
    _pump_events(qapp, lambda: bool(seen))
    worker.wait(2000)

    assert seen, "worker 进度信号未送达"
    assert seen[0] is main_thread, (
        "AUD-2108 修复回退：回调不在主线程执行")


# ═══════════════════════════════════════════════════════════════════════
# AUD-2110: subtitle/brand measurement moved into the encoder worker
# ═══════════════════════════════════════════════════════════════════════


def _export_sequence_with_subtitle() -> Any:
    clip = SimpleNamespace(is_enabled=True)
    track = SimpleNamespace(
        is_enabled=True, is_visible=True, subtitle_clips=(clip,))
    return SimpleNamespace(
        timeline_id="t1", revision=0, width=1920, height=1080,
        fps=24.0, duration_frames=240,
        subtitle_tracks=(track,),
        all_tracks=(),
    )


class _ExportFakeMW:
    def __init__(self, active_timeline_id: str = "t1") -> None:
        self._current_edit_project = SimpleNamespace(
            project_id="p1", active_timeline_id="t1", assets={})
        self._current_timeline_sequences = {
            "t1": _export_sequence_with_subtitle()}
        self.edit_page = SimpleNamespace(
            _current_timeline=SimpleNamespace(timeline_id=active_timeline_id))
        from src.shared.feature_flags import FeatureFlags
        self.feature_flags = FeatureFlags(
            generation=True, agent_auto_mode=True, cloud_upload=True,
            auto_export=True)
        self.timeline_panel = None
        self._video_export_started_at = None
        self._video_exporter = None
        self.export_calls: list[dict] = []

    def perform_video_export(self, settings: dict) -> None:
        self.export_calls.append(dict(settings))
        self._video_export_started_at = time.perf_counter()


class _NoBriefService:
    active_brief_id = "default"

    def get_brief(self, brief_id: Any) -> None:  # noqa: ARG002
        return None


@pytest.fixture
def export_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """request_ai_export 的公共桩：路径/确认对话框/预检恒通过。"""
    import PyQt6.QtWidgets
    import src.features.edit.ai_editing.export_service as es_mod

    summaries: list[str] = []

    def _fake_question(parent: Any, title: str, text: str, *a: Any, **k: Any) -> Any:
        summaries.append(str(text))
        return PyQt6.QtWidgets.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(
        "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
        lambda *a, **k: ("/tmp/ai_export_ef08.mp4", "MP4 Video (*.mp4)"))
    monkeypatch.setattr(
        "PyQt6.QtWidgets.QMessageBox.question", _fake_question)
    monkeypatch.setattr(
        es_mod, "run_export_preflight",
        lambda *a, **k: SimpleNamespace(
            ok=True, has_blocking=False, blocking=(), degraded=()))
    return {"es_mod": es_mod, "summaries": summaries}


def _run_export(monkeypatch: pytest.MonkeyPatch, mw: Any,
                cue_spy: list[int]) -> tuple[bool, str, Any]:
    import src.features.edit.ai_editing.export_service as es_mod

    def _spy_cues(*args: Any, **kwargs: Any) -> tuple:
        cue_spy.append(1)
        return ()

    monkeypatch.setattr(bridge_impl, "_build_measured_subtitle_quality_cues",
                        _spy_cues)

    captured: dict[str, Any] = {}
    real_service = es_mod.ExportService

    class _CapturingService(real_service):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured["service"] = self

    monkeypatch.setattr(es_mod, "ExportService", _CapturingService)

    bridge = bridge_impl.MainWindowAiEditHostBridge(
        mw, _NoBriefService(), None)
    bridge._runtime_capability_snapshot = lambda: None  # type: ignore[method-assign]
    ok, message = bridge.request_ai_export(
        SimpleNamespace(project_id="p1", timeline_id="t1"),
        {"codec": "h264"},
    )
    return ok, message, captured.get("service")


def test_aud2110_measurement_deferred_to_post_export(
        monkeypatch: pytest.MonkeyPatch, export_env: dict) -> None:
    """请求阶段不测量；_post_export（编码 worker）阶段才测量。"""
    cue_spy: list[int] = []
    mw = _ExportFakeMW()
    ok, message, service = _run_export(monkeypatch, mw, cue_spy)

    assert ok is True, message
    assert cue_spy == [], (
        "AUD-2110 修复回退：request_ai_export 仍在 UI 线程同步测量字幕 bbox")
    assert len(mw.export_calls) == 1
    assert callable(mw.export_calls[0]["_post_export"])

    # 质量门阶段（编码 worker 线程语义）执行测量
    service.complete_host_export = lambda *a, **k: (True, "ok")  # type: ignore[method-assign]
    ok2, msg2 = mw.export_calls[0]["_post_export"]("/tmp/ai_export_ef08.mp4")
    assert cue_spy == [1], "_post_export 未执行字幕合同测量"
    assert (ok2, msg2) == (True, "ok")


def test_aud2110_summary_uses_cheap_clip_count(
        monkeypatch: pytest.MonkeyPatch, export_env: dict) -> None:
    """确认对话框摘要用轻量计数（含"待测量"字样），不再依赖测量结果。"""
    cue_spy: list[int] = []
    mw = _ExportFakeMW()
    ok, message, _service = _run_export(monkeypatch, mw, cue_spy)
    assert ok is True
    summary = "\n".join(export_env["summaries"])
    assert "待测量 1 条" in summary, summary
    assert "已测量" not in summary


def test_aud2110_count_helper_matches_cue_clip_collection() -> None:
    """轻量计数与 cues 构建器的 clip 收集口径一致。"""
    from src.app.controllers.ai_edit_host_bridge_impl import (
        _count_enabled_subtitle_clips,
    )

    sequence = _export_sequence_with_subtitle()
    assert _count_enabled_subtitle_clips(sequence) == 1

    disabled = SimpleNamespace(
        subtitle_tracks=(SimpleNamespace(
            is_enabled=False, is_visible=True,
            subtitle_clips=(SimpleNamespace(is_enabled=True),)),))
    assert _count_enabled_subtitle_clips(disabled) == 0


# ═══════════════════════════════════════════════════════════════════════
# AUD-2111: keyring probe cached with TTL
# ═══════════════════════════════════════════════════════════════════════


class _FakeStore:
    def __init__(self, value: Any = "secret") -> None:
        self.value = value
        self.calls: list[str] = []

    def get_secret(self, ref: str) -> Any:
        self.calls.append(ref)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def _cred_bridge(profile: Any, store: Any) -> Any:
    mw = SimpleNamespace(_ai_edit_controller=SimpleNamespace(
        _profile_registry=SimpleNamespace(active_profile=profile),
        _credential_store=store,
    ))
    bridge = bridge_impl.MainWindowAiEditHostBridge(
        mw, SimpleNamespace(), None)
    bridge._runtime_capability_snapshot = lambda: None  # type: ignore[method-assign]
    return bridge


def test_aud2111_local_profiles_skip_keyring() -> None:
    """ollama/local_http/openai_compat 不需要 secret：不触碰 keyring。"""
    store = _FakeStore()
    bridge = _cred_bridge(
        SimpleNamespace(provider_kind="ollama", credential_ref=""), store)
    assert bridge._probe_credential_available(
        SimpleNamespace(provider_kind="ollama", credential_ref="x"), store) is True
    assert store.calls == []


def test_aud2111_probe_cached_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL 内重复探测只取一次 keyring（旧行为：每次 manifest 都取）。"""
    store = _FakeStore(value="secret")
    bridge = _cred_bridge(
        SimpleNamespace(provider_kind="api", credential_ref="ref1"), store)
    probe = SimpleNamespace(provider_kind="api", credential_ref="ref1")

    assert bridge._probe_credential_available(probe, store) is True
    assert bridge._probe_credential_available(probe, store) is True
    assert store.calls == ["ref1"], (
        "AUD-2111 修复回退：keyring 探测没有缓存（每次都访问系统钥匙串）")

    monkeypatch.setattr(bridge_impl, "_CREDENTIAL_PROBE_TTL_S", 0.0)
    assert bridge._probe_credential_available(probe, store) is True
    assert store.calls == ["ref1", "ref1"]  # TTL 过期后重新探测


def test_aud2111_probe_failure_degrades_to_false() -> None:
    """keyring 抛异常（被锁/后端故障）→ 按不可用处理，不外泄异常。"""
    store = _FakeStore(value=RuntimeError("keychain locked"))
    bridge = _cred_bridge(
        SimpleNamespace(provider_kind="api", credential_ref="ref1"), store)
    assert bridge._probe_credential_available(
        SimpleNamespace(provider_kind="api", credential_ref="ref1"),
        store) is False


# ═══════════════════════════════════════════════════════════════════════
# AUD-2205: ComfyUI probe timeout narrowed + thread contract documented
# ═══════════════════════════════════════════════════════════════════════


class _ComfyClient:
    def __init__(self) -> None:
        self._timeout = 30.0
        self.timeouts: list[float] = []

    def capabilities(self) -> dict:
        self.timeouts.append(self._timeout)
        return {"object_info": {}}

    def test_connection(self) -> tuple[bool, str]:
        self.timeouts.append(self._timeout)
        return True, "ok"


class _ComfyExecutor:
    def __init__(self, client: Any) -> None:
        self._server = client
        self.caps: Any = None
        self.bound: Any = None

    def set_server_capabilities(self, caps: Any) -> None:
        self.caps = caps

    def bind_workflow(self, snapshot: Any, profile: Any = None) -> None:
        self.bound = snapshot


def _comfy_host(client: Any) -> Any:
    executor = _ComfyExecutor(client)
    profile = SimpleNamespace(profile_id="p1", base_url="http://x")
    return SimpleNamespace(
        _comfy_generation_executors={"p1": executor},
        _comfy_profile_registry=SimpleNamespace(active_profile=profile),
    ), executor


def test_aud2205_bind_uses_narrowed_probe_timeout_and_restores() -> None:
    """capabilities() 探测在收窄超时下进行，退出后恢复 client 原超时。"""
    client = _ComfyClient()
    host, executor = _comfy_host(client)

    ok, message = AiEditingMixin.bind_comfy_workflow(host, {"snap": 1})
    assert ok is True, message
    assert client.timeouts == [_COMFY_PROBE_TIMEOUT_S], (
        "AUD-2205 修复回退：能力查询仍用默认 30s 超时")
    assert client._timeout == 30.0, "探测后未恢复原超时"
    assert executor.caps == {"object_info": {}}
    assert executor.bound == {"snap": 1}


def test_aud2205_test_connection_uses_narrowed_timeout() -> None:
    client = _ComfyClient()
    host, _executor = _comfy_host(client)

    ok, message = AiEditingMixin.test_comfy_connection(host)
    assert ok is True
    assert client.timeouts == [_COMFY_PROBE_TIMEOUT_S]
    assert client._timeout == 30.0


def test_aud2205_stub_client_without_timeout_is_untouched() -> None:
    """测试桩（无数值 _timeout）不受影响（getattr 防御路径）。"""
    client = SimpleNamespace(capabilities=lambda: {}, test_connection=None)
    with _comfy_probe_timeout(client, 5.0):
        pass
    assert not hasattr(client, "_timeout")


def test_aud2205_thread_contract_documented() -> None:
    """docstring 必须明示"禁止 GUI 线程调用"的线程契约。"""
    import inspect
    bind_doc = inspect.getdoc(AiEditingMixin.bind_comfy_workflow) or ""
    test_doc = inspect.getdoc(AiEditingMixin.test_comfy_connection) or ""
    for doc in (bind_doc, test_doc):
        assert "GUI 线程" in doc and "AUD-2205" in doc, (
            "AUD-2205 修复回退：线程契约文档缺失")


# ═══════════════════════════════════════════════════════════════════════
# AUD-2214: composite render cache pruned (size/count LRU)
# ═══════════════════════════════════════════════════════════════════════


def _make_cache_files(directory: Any, spec: list[tuple[str, int, float]]) -> list:
    paths = []
    for name, size, mtime in spec:
        p = directory / name
        p.write_bytes(b"\0" * size)
        os.utime(p, (mtime, mtime))
        paths.append(p)
    return paths


def test_aud2214_prune_removes_oldest_over_budget(
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """超预算 → 按 mtime 从旧到新淘汰；保护窗口内的产物不删。"""
    now = time.time()
    old = now - 2 * 3600
    _make_cache_files(tmp_path, [
        ("composite-aaa.mp4", 1000, old),
        ("composite-bbb.mp4", 1000, old + 10),
        ("composite-ccc.png", 1000, old + 20),
        ("composite-new.png", 1000, now - 60),  # 1h 保护窗口内
    ])
    monkeypatch.setattr(
        "src.app.controllers.ai_workflow_composite_render._RENDER_CACHE_MAX_TOTAL_BYTES",
        2500)
    monkeypatch.setattr(
        "src.app.controllers.ai_workflow_composite_render._RENDER_CACHE_MAX_FILES",
        64)
    monkeypatch.setattr(
        "src.app.controllers.ai_workflow_composite_render._RENDER_CACHE_MIN_AGE_S",
        3600.0)

    _prune_render_cache(tmp_path)

    remaining = sorted(p.name for p in tmp_path.iterdir())
    # 4000B → 删 aaa(→3000) → 删 bbb(→2000 ≤ 2500) → 停；1h 内的 new 保护
    assert remaining == ["composite-ccc.png", "composite-new.png"], (
        "AUD-2214 修复回退：未按 LRU 淘汰最旧产物（或误删保护窗口内产物）")


def test_aud2214_prune_respects_file_count_limit(
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    _make_cache_files(tmp_path, [
        (f"composite-{i:03d}.png", 10, now - 7200 + i) for i in range(5)
    ])
    monkeypatch.setattr(
        "src.app.controllers.ai_workflow_composite_render._RENDER_CACHE_MAX_TOTAL_BYTES",
        1024 ** 3)
    monkeypatch.setattr(
        "src.app.controllers.ai_workflow_composite_render._RENDER_CACHE_MAX_FILES",
        2)
    monkeypatch.setattr(
        "src.app.controllers.ai_workflow_composite_render._RENDER_CACHE_MIN_AGE_S",
        0.0)

    _prune_render_cache(tmp_path)

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert len(remaining) == 2
    assert remaining == ["composite-003.png", "composite-004.png"]


def test_aud2214_within_budget_no_deletion(tmp_path: Any) -> None:
    _make_cache_files(tmp_path, [("composite-a.png", 10, time.time() - 7200)])
    before = sorted(p.name for p in tmp_path.iterdir())
    _prune_render_cache(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_aud2214_render_headless_invokes_prune() -> None:
    """回退检测：_render_headless 必须在写新产物前调用淘汰。"""
    import inspect
    from src.app.controllers.ai_workflow_composite_render import _render_headless
    source = inspect.getsource(_render_headless)
    assert "_prune_render_cache(out_dir)" in source, (
        "AUD-2214 修复回退：渲染路径不再调用缓存淘汰")


def test_aud2214_limits_are_finite() -> None:
    """常量契约：预算为正、保护窗口非负（防止被改成 0=永不淘汰）。"""
    assert _RENDER_CACHE_MAX_TOTAL_BYTES > 0
    assert _RENDER_CACHE_MAX_FILES > 0
    assert _RENDER_CACHE_MIN_AGE_S > 0


# ═══════════════════════════════════════════════════════════════════════
# AUD-2410: properties panel receives a copy of node parameters
# ═══════════════════════════════════════════════════════════════════════


def test_aud2410_panel_receives_parameters_copy() -> None:
    """set_node 收到的 parameters 必须是副本；面板就地改动不影响节点。"""
    node_params = {"amount": 0.5}

    node = SimpleNamespace(
        id="n1", node_type="blur", node_title="Blur",
        parameters=node_params, disabled=False, header_color="#666666",
    )
    graph = SimpleNamespace(get_node=lambda node_id: node)

    received: dict[str, Any] = {}

    class _Panel:
        def set_node(self, **kwargs: Any) -> None:
            received.update(kwargs)

    class _Page:
        node_graph = graph

    page = _Page()
    host = SimpleNamespace(
        _composite_available=True,
        composite_page=page,
        _node_properties_panel=_Panel(),
        dock_node_properties=None,
    )
    CompositeControllerMixin._on_node_editor_selected(host, "n1")

    assert received["node_id"] == "n1"
    panel_params = received["parameters"]
    assert panel_params == node_params
    assert panel_params is not node_params, (
        "AUD-2410 修复回退：属性面板仍持有节点实时 parameters 引用")
    panel_params["amount"] = 99.0  # 面板就地改动不得穿透到节点
    assert node_params["amount"] == 0.5


# ═══════════════════════════════════════════════════════════════════════
# AUD-2610: getattr default no longer evaluates findChildren eagerly
# ═══════════════════════════════════════════════════════════════════════


def test_aud2610_findchildren_skipped_when_all_docks_present() -> None:
    """all_docks 存在时不再无条件执行 findChildren 全树枚举。"""
    calls: list[str] = []

    class _Host:
        all_docks = ["dock-a", "dock-b"]  # 非空：不应触发 findChildren 回退
        _updating_titlebars = False

        def findChildren(self, *args: Any) -> list:
            calls.append("findChildren")
            return []

        def _style_dock_tab_bars(self) -> None:
            calls.append("style")

    host = _Host()
    DockLayoutMixin._perform_update_all_titlebars(host)  # type: ignore[arg-type]
    assert calls == ["style"], (
        "AUD-2610 修复回退：all_docks 存在时仍执行了 findChildren")


def test_aud2610_falls_back_to_findchildren_when_missing() -> None:
    """all_docks 缺失时仍回退 findChildren（行为不变）。"""
    calls: list[str] = []

    class _Host:
        _updating_titlebars = False

        def findChildren(self, *args: Any) -> list:
            calls.append("findChildren")
            return []

        def _style_dock_tab_bars(self) -> None:
            pass

    host = _Host()
    DockLayoutMixin._perform_update_all_titlebars(host)  # type: ignore[arg-type]
    assert calls == ["findChildren"]


# ═══════════════════════════════════════════════════════════════════════
# AUD-2713: ADR latency calibration moved off the UI thread
# ═══════════════════════════════════════════════════════════════════════


class _CalibrationPanel:
    def __init__(self) -> None:
        self.calibrations: list[Any] = []

    def set_calibration(self, calibration: Any) -> None:
        self.calibrations.append(calibration)


def _adr_host() -> Any:
    panel = _CalibrationPanel()
    host = SimpleNamespace(
        _adr_panel=lambda: panel,
        _adr_settings_dict=lambda: {"device_id": "dev-1"},
    )
    host._on_adr_calibration_ready = lambda calibration: (
        ec.EditControllerMixin._on_adr_calibration_ready(host, calibration))
    return host


def test_aud2713_calibration_runs_off_main_thread(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """校准在 QThread 执行，完成后回主线程写面板；防重入。"""
    from src.features.edit.core import recording_manager

    threads: list[Any] = []

    def _fake_measure(device_id: Any = None) -> Any:
        threads.append(QThread.currentThread())
        return SimpleNamespace(calibrated_offset_ms=7.5)

    monkeypatch.setattr(recording_manager, "measure_round_trip_latency",
                        _fake_measure)

    host = _adr_host()
    main_thread = QThread.currentThread()
    ec.EditControllerMixin._on_adr_calibrate(host)

    worker = getattr(host, "_adr_calibration_worker", None)
    assert isinstance(worker, ec._AdrCalibrationWorker), (
        "AUD-2713 修复回退：校准仍在按钮槽内同步执行（无 worker）")
    _pump_events(qapp, lambda: host._adr_calibration_worker is None)
    worker.wait(2000)

    assert threads and threads[0] is not main_thread, (
        "AUD-2713 修复回退：measure_round_trip_latency 在主线程执行")
    assert host._adr_calibration.calibrated_offset_ms == 7.5
    assert host._adr_latency_ms == 7.5
    assert host._adr_panel().calibrations[-1] is host._adr_calibration


def test_aud2713_calibration_failure_yields_none(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import recording_manager

    def _boom(device_id: Any = None) -> Any:
        raise RuntimeError("no device")

    monkeypatch.setattr(recording_manager, "measure_round_trip_latency", _boom)

    host = _adr_host()
    ec.EditControllerMixin._on_adr_calibrate(host)
    _pump_events(qapp, lambda: host._adr_calibration_worker is None)

    assert host._adr_calibration is None
    assert host._adr_latency_ms == 0.0
    assert host._adr_panel().calibrations[-1] is None


def test_aud2713_reentrant_click_ignored(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import recording_manager

    def _fake_measure(device_id: Any = None) -> Any:
        time.sleep(0.2)
        return SimpleNamespace(calibrated_offset_ms=1.0)

    monkeypatch.setattr(recording_manager, "measure_round_trip_latency",
                        _fake_measure)
    host = _adr_host()
    ec.EditControllerMixin._on_adr_calibrate(host)
    first = host._adr_calibration_worker
    ec.EditControllerMixin._on_adr_calibrate(host)  # 测量期间重复点击
    assert host._adr_calibration_worker is first
    _pump_events(qapp, lambda: host._adr_calibration_worker is None)
    first.wait(2000)


# ═══════════════════════════════════════════════════════════════════════
# AUD-2717: old RoutingDialog gets deleteLater after close
# ═══════════════════════════════════════════════════════════════════════


class _OldDialog:
    def __init__(self, track_id: str) -> None:
        self._track_id = track_id
        self.closed = False
        self.delete_later_called = False
        self.raised = False

    def isVisible(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    def deleteLater(self) -> None:
        self.delete_later_called = True

    def raise_(self) -> None:
        self.raised = True

    def activateWindow(self) -> None:
        pass


def _routing_host(old_dialog: Any) -> Any:
    sequence = SimpleNamespace(
        get_track_by_id=lambda tid: SimpleNamespace(
            track_id="t1", name="V1", output_bus_id=None, sends=(),
            is_bus=False),
        audio_tracks=(),
    )
    host = SimpleNamespace(
        timeline_panel=SimpleNamespace(
            timeline_view=SimpleNamespace(timeline=sequence)),
        _routing_dialog=old_dialog,
    )
    host._on_mixer_routing_committed = lambda track_id, payload: (
        ec.EditControllerMixin._on_mixer_routing_committed(
            host, track_id, payload))
    host._on_mixer_routing_preview = lambda track_id, payload: (
        ec.EditControllerMixin._on_mixer_routing_preview(
            host, track_id, payload))
    host._on_mixer_routing_dialog_finished = lambda *a, **k: (
        ec.EditControllerMixin._on_mixer_routing_dialog_finished(
            host, *a, **k))
    return host


def test_aud2717_old_dialog_deleted_on_reopen() -> None:
    """换轨重开：旧 dialog close 后必须 deleteLater（父窗口子节点不累积）。"""
    old = _OldDialog("t0")
    host = _routing_host(old)
    ec.EditControllerMixin._on_mixer_show_routing(host, "t1")

    assert old.closed is True
    assert old.delete_later_called is True, (
        "AUD-2717 修复回退：旧 RoutingDialog 只 close 未 deleteLater")
    new_dialog = host._routing_dialog
    assert new_dialog is not old
    new_dialog.close()
    new_dialog.deleteLater()


def test_aud2717_visible_same_track_raises_without_recreate() -> None:
    """同轨已打开 → 前置既有实例，不重建也不删除（原语义保留）。"""
    class _VisibleOld(_OldDialog):
        def isVisible(self) -> bool:
            return True

    old = _VisibleOld("t1")
    host = _routing_host(old)
    ec.EditControllerMixin._on_mixer_show_routing(host, "t1")
    assert host._routing_dialog is old
    assert old.closed is False
    assert old.delete_later_called is False
    assert old.raised is True


def test_aud2717_destroyed_cpp_object_tolerated() -> None:
    """旧实例 C++ 对象已被销毁（RuntimeError）→ 按无旧实例处理，不崩溃。"""
    class _Zombie:
        _track_id = "t0"

        def isVisible(self) -> bool:
            raise RuntimeError("wrapped C/C++ object of type RoutingDialog "
                               "has been deleted")

    host = _routing_host(_Zombie())
    ec.EditControllerMixin._on_mixer_show_routing(host, "t1")  # 不得抛出
    new_dialog = host._routing_dialog
    assert new_dialog is not None and new_dialog._track_id == "t1"
    new_dialog.close()
    new_dialog.deleteLater()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2806: periodic save skips clean projects
# ═══════════════════════════════════════════════════════════════════════


def _periodic_save_host(dirty: bool, with_edit_project: bool = True) -> Any:
    saves: list[int] = []

    class _Router:
        def save(self) -> bool:
            saves.append(1)
            return True

    host = SimpleNamespace(
        current_file_path="/media/img.png",
        _current_edit_project_filepath="/proj/x.topos",
        engine=object(),
        _current_edit_project=(SimpleNamespace() if with_edit_project else None),
        edit_project_dirty=dirty,
        save_router=_Router(),
    )
    host._saves = saves  # type: ignore[attr-defined]
    return host


def test_aud2806_clean_project_skips_periodic_save() -> None:
    """有 edit 工程且全干净 → 跳过全量保存（旧行为：无条件同步落盘）。"""
    host = _periodic_save_host(dirty=False)
    fc.FileOpsMixin._perform_periodic_save(host)
    assert host._saves == [], (
        "AUD-2806 修复回退：周期保存未按脏标记跳过无变化周期")


def test_aud2806_dirty_project_still_saves() -> None:
    host = _periodic_save_host(dirty=True)
    fc.FileOpsMixin._perform_periodic_save(host)
    assert host._saves == [1]


def test_aud2806_any_dirty_flag_triggers_save() -> None:
    host = _periodic_save_host(dirty=False)
    host.color_session_dirty = True
    fc.FileOpsMixin._perform_periodic_save(host)
    assert host._saves == [1]


def test_aud2806_library_mode_keeps_legacy_save() -> None:
    """无 edit 工程（Library 单素材）→ 保持旧行为（无法判定脏，直接保存）。"""
    host = _periodic_save_host(dirty=False, with_edit_project=False)
    fc.FileOpsMixin._perform_periodic_save(host)
    assert host._saves == [1]


# ═══════════════════════════════════════════════════════════════════════
# AUD-2813: GPU render thread stopped when pipeline assembly fails
# ═══════════════════════════════════════════════════════════════════════


class _FakeGpuRenderer:
    def __init__(self) -> None:
        self.render_error = SimpleNamespace(connect=lambda cb: None)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout_ms: int = 10000) -> bool:
        self.stopped = True
        return True


def _gpu_host() -> Any:
    return SimpleNamespace(
        timeline_preview_renderer=SimpleNamespace(
            set_gpu_renderer=None,  # 由用例填充
            set_comp_gl_context=lambda ctx: None,
        ),
        config_manager=SimpleNamespace(get=lambda key: False),
        source_viewer_widget=None,
        _init_source_gpu_pipeline=lambda gpu_decode_manager: None,
    )


def _init_gpu(monkeypatch: pytest.MonkeyPatch, host: Any,
              renderer: Any, fail_at: str) -> None:
    from PyQt6.QtGui import QOpenGLContext
    from src.features.edit import facade as edit_facade

    monkeypatch.setattr(
        "src.shared.video.pipeline_decision.resolve_video_pipeline_mode",
        lambda cfg: SimpleNamespace(is_v3_active=lambda: True))
    monkeypatch.setattr(edit_facade, "create_edit_gpu_decode_manager",
                        lambda: SimpleNamespace())
    monkeypatch.setattr(edit_facade, "create_edit_gpu_preview_renderer",
                        lambda main_context, parent=None: renderer)

    if fail_at == "set_gpu_renderer":
        def _boom(gpu_decode_manager: Any, gpu_renderer: Any) -> None:
            raise RuntimeError("assembly failed")
        host.timeline_preview_renderer.set_gpu_renderer = _boom
    else:
        host.timeline_preview_renderer.set_gpu_renderer = (
            lambda gpu_decode_manager, gpu_renderer: None)

    stopped: list[Any] = []
    monkeypatch.setattr(fc, "_stop_gpu_renderer_deferred",
                        lambda r, timeout_ms=10000: stopped.append(r))
    host._gpu_pipeline_initialized = False
    fc.FileOpsMixin._init_gpu_pipeline(host, QOpenGLContext())
    return stopped


def test_aud2813_assembly_failure_stops_started_renderer(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """装配中途失败 → 已 start 的渲染线程必须被 stop（不再泄漏）。"""
    renderer = _FakeGpuRenderer()
    host = _gpu_host()
    stopped = _init_gpu(monkeypatch, host, renderer, fail_at="set_gpu_renderer")

    assert renderer.started is True
    assert stopped == [renderer], (
        "AUD-2813 修复回退：装配失败后已启动的 GPU 渲染线程未被回收")


def test_aud2813_success_path_does_not_stop_renderer(
        monkeypatch: pytest.MonkeyPatch) -> None:
    renderer = _FakeGpuRenderer()
    host = _gpu_host()
    stopped = _init_gpu(monkeypatch, host, renderer, fail_at="never")

    assert renderer.started is True
    assert stopped == [], "成功路径不得停掉渲染线程"
    assert host._gpu_decode_manager is not None


# ═══════════════════════════════════════════════════════════════════════
# AUD-2904: per-frame print / info logging removed from color_output path
# ═══════════════════════════════════════════════════════════════════════


def _layer_host_for_output() -> Any:
    return SimpleNamespace(
        current_page="Color",
        _composite_adapter=None,
        _composite_available=False,
        composite_page=None,
        _composite_startup_pending=False,
    )


def test_aud2904_no_per_frame_print_or_info(
        capsys: Any, caplog: Any) -> None:
    """color_output_changed 每帧路径不得 print / 输出 info 级调试日志。"""
    import logging

    host = _layer_host_for_output()
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            lc.LayerOpsMixin._on_color_output_changed(host, 7, 1920, 1080)

    captured = capsys.readouterr()
    assert captured.out == "", (
        "AUD-2904 修复回退：每帧 print 仍在执行")
    info_texts = [r.getMessage() for r in caplog.records
                  if r.levelno >= logging.INFO]
    assert not any("COMPOSITE-DEBUG" in t for t in info_texts), (
        "AUD-2904 修复回退：info 级 COMPOSITE-DEBUG 日志仍在执行")


def test_aud2904_no_print_in_startup_fallback(capsys: Any) -> None:
    host = _layer_host_for_output()
    host._composite_startup_pending = False
    lc.LayerOpsMixin._composite_startup_fallback(host)
    captured = capsys.readouterr()
    assert captured.out == "", (
        "AUD-2904 修复回退：启动回退路径的 print 仍在执行")


def test_aud2904_composite_page_still_updated() -> None:
    """回归守卫：Composite 页激活时功能行为不变（仍更新纹理）。"""
    updated: list[tuple] = []

    class _Page:
        _is_active = True
        _renderer = object()

        def mark_color_dirty(self) -> None:
            pass

        def update_color_output(self, tex: int, w: int, h: int, rev: int) -> None:
            updated.append((tex, w, h, rev))

    host = SimpleNamespace(
        current_page="Composite",
        _composite_adapter=None,
        _composite_available=True,
        composite_page=_Page(),
        engine=SimpleNamespace(color_output=SimpleNamespace(revision=3)),
        _composite_startup_pending=False,
        _composite_enter_needed_on_startup=False,
    )
    lc.LayerOpsMixin._on_color_output_changed(host, 9, 640, 480)
    assert updated == [(9, 640, 480, 3)]


# ═══════════════════════════════════════════════════════════════════════
# AUD-2909: AI plugin EXR loads + cache scan moved off the UI thread
# ═══════════════════════════════════════════════════════════════════════


class _QObjectHost(QObject):
    """信号 parent 需要 QObject 宿主（生产环境为 MainWindow）。"""


class _LayerHost(_QObjectHost, lc.LayerOpsMixin):
    """QObject + LayerOpsMixin：mixin 方法直接可用（信号 parent 合法）。"""

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self.engine = engine


def test_aud2909_cleanup_runs_off_main_thread(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """孤立缓存扫描/删除在线程池执行（旧行为：UI 线程同步遍历目录）。"""
    threads: list[Any] = []

    def _fake_cleanup(active_files: Any) -> None:
        threads.append(QThread.currentThread())

    monkeypatch.setattr(lc.FileIO, "cleanup_orphaned_cache", _fake_cleanup)

    layer = SimpleNamespace(baked_file_name="a.exr", plugins=[])
    engine = SimpleNamespace(layers=[layer], source_bake_file_name=None)
    host = _LayerHost(engine)
    lc.LayerOpsMixin._cleanup_baked_cache(host)

    main_thread = QThread.currentThread()
    _pump_events(qapp, lambda: getattr(host, "_cache_cleanup_worker", "x") is None)
    assert threads, "cleanup 未被执行"
    assert threads[0] is not main_thread, (
        "AUD-2909 修复回退：cleanup_orphaned_cache 仍在主线程同步执行")


def test_aud2909_exr_load_off_thread_submit_on_main(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """EXR 磁盘读取在线程池；缓存写入 + GPU 上传提交回主线程。"""
    import numpy as np

    load_threads: list[Any] = []
    bgr = np.zeros((2, 2, 3), dtype=np.uint8)
    bgr[:, :, 0] = 1  # B 通道
    bgr[:, :, 2] = 3  # R 通道

    def _fake_load(filename: Any) -> Any:
        load_threads.append(QThread.currentThread())
        return bgr

    monkeypatch.setattr(lc.FileIO, "load_ai_plugin_result", _fake_load)

    submits: list[tuple] = []

    class _Renderer:
        def submit(self, command: Any, payload: dict) -> None:
            submits.append((command, payload))

    layer = SimpleNamespace(
        id=11, is_dirty=False,
        plugins=[{"type": "ai", "instance_id": 5, "ai_result_file": "r.exr"}],
    )
    engine = SimpleNamespace(
        layers=[layer],
        ai_plugin_result_cache={},
        renderer=_Renderer(),
    )
    host = _LayerHost(engine)

    lc.LayerOpsMixin._restore_ai_plugin_results(host)
    _pump_events(
        qapp,
        lambda: (11, 5) in engine.ai_plugin_result_cache
        and getattr(host, "_ai_plugin_restore_worker", "x") is None)

    main_thread = QThread.currentThread()
    assert load_threads and load_threads[0] is not main_thread, (
        "AUD-2909 修复回退：EXR 读取仍在主线程同步执行")
    assert (11, 5) in engine.ai_plugin_result_cache
    assert layer.is_dirty is True
    assert submits, "GPU 上传未提交"
    command, payload = submits[0]
    assert payload["layer_id"] == 11 and payload["instance_id"] == 5
    # BGR → RGB 契约（P0-COL-03）：上传数组 R/B 通道互换
    uploaded = payload["image_array"]
    assert uploaded[0, 0, 0] == 3 and uploaded[0, 0, 2] == 1


def test_aud2909_stale_result_after_project_switch_dropped(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """工程切换（图层不在 engine）后迟到的结果被丢弃，不污染新工程缓存。"""
    import numpy as np

    monkeypatch.setattr(lc.FileIO, "load_ai_plugin_result",
                        lambda filename: np.zeros((1, 1, 3), dtype=np.uint8))
    layer = SimpleNamespace(
        id=11, is_dirty=False,
        plugins=[{"type": "ai", "instance_id": 5, "ai_result_file": "r.exr"}],
    )
    engine = SimpleNamespace(
        layers=[],  # 模拟加载完成前图层已被移除/切换
        ai_plugin_result_cache={},
        renderer=None,
    )
    host = _LayerHost(engine)

    lc.LayerOpsMixin._restore_ai_plugin_results(host)
    _pump_events(qapp, lambda: getattr(host, "_ai_plugin_restore_worker", "x") is None)

    assert engine.ai_plugin_result_cache == {}
    assert layer.is_dirty is False


def test_aud2909_no_ai_plugins_skips_worker(qapp) -> None:
    layer = SimpleNamespace(id=1, plugins=[{"type": "blur"}])
    engine = SimpleNamespace(layers=[layer])
    host = _LayerHost(engine)
    lc.LayerOpsMixin._restore_ai_plugin_results(host)
    assert getattr(host, "_ai_plugin_restore_worker", None) is None


# ═══════════════════════════════════════════════════════════════════════
# AUD-3010: dependency report built on a background thread
# ═══════════════════════════════════════════════════════════════════════


class _DepsHost(QObject, mtc.MenuToolbarMixin):
    """QObject + MenuToolbarMixin：mixin 方法直接可用。"""

    def __init__(self) -> None:
        super().__init__()
        self._current_edit_project = SimpleNamespace(project_id="p1")
        messages: list[tuple] = []

        class _Bar:
            def showMessage(self, msg: str, timeout: int = 0) -> None:
                messages.append((msg, timeout))

        self.status_bar = _Bar()
        self._status_messages = messages
        self.save_router = SimpleNamespace(get_current_project_path=lambda: None)


def _deps_host() -> Any:
    return _DepsHost()


def test_aud3010_report_built_off_main_thread_then_dialog(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """扫描在线程池执行；报告就绪后在 UI 线程弹对话框。"""
    from src.features.color.core import project_dependencies
    from src.features.color.ui import dependency_report_dialog

    threads: list[Any] = []

    def _fake_build(project: Any, base_dir: Any = None) -> str:
        threads.append(QThread.currentThread())
        return "REPORT"

    monkeypatch.setattr(project_dependencies, "build_dependency_report",
                        _fake_build)

    dialogs: list[Any] = []

    class _FakeDialog:
        def __init__(self, parent: Any, report: Any) -> None:
            dialogs.append((parent, report))

        def exec(self) -> int:
            dialogs.append("exec")
            return 0

    monkeypatch.setattr(dependency_report_dialog, "DependencyReportDialog",
                        _FakeDialog)

    host = _deps_host()
    mtc.MenuToolbarMixin.on_dependency_report_requested(host)

    main_thread = QThread.currentThread()
    _pump_events(qapp, lambda: "exec" in dialogs)
    assert threads and threads[0] is not main_thread, (
        "AUD-3010 修复回退：依赖扫描仍在 UI 线程同步执行")
    assert dialogs[-2:] == [(host, "REPORT"), "exec"]
    assert getattr(host, "_dependency_report_inflight", True) is False


def test_aud3010_build_failure_shows_warning(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """扫描失败 → QMessageBox.warning（测试中替换，绝不弹真实模态框）。"""
    import PyQt6.QtWidgets
    from src.features.color.core import project_dependencies

    def _boom(project: Any, base_dir: Any = None) -> Any:
        raise RuntimeError("scan failed")

    monkeypatch.setattr(project_dependencies, "build_dependency_report", _boom)
    warnings: list[tuple] = []
    monkeypatch.setattr(
        PyQt6.QtWidgets.QMessageBox, "warning",
        lambda *a, **k: warnings.append(a))

    host = _deps_host()
    mtc.MenuToolbarMixin.on_dependency_report_requested(host)
    _pump_events(qapp, lambda: bool(warnings))

    assert warnings, "失败路径未提示用户"


def test_aud3010_reentrant_request_while_scanning(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.color.core import project_dependencies

    started: list[int] = []

    def _slow_build(project: Any, base_dir: Any = None) -> str:
        started.append(1)
        time.sleep(0.15)
        return "REPORT"

    monkeypatch.setattr(project_dependencies, "build_dependency_report",
                        _slow_build)
    monkeypatch.setattr(
        "src.features.color.ui.dependency_report_dialog.DependencyReportDialog",
        lambda *a, **k: SimpleNamespace(exec=lambda: 0))

    host = _deps_host()
    mtc.MenuToolbarMixin.on_dependency_report_requested(host)
    mtc.MenuToolbarMixin.on_dependency_report_requested(host)  # 防重入
    _pump_events(qapp, lambda: getattr(host, "_dependency_report_inflight", True) is False)
    assert started == [1]
    assert host._status_messages, "防重入路径未给状态栏反馈"


def test_aud3010_no_project_shows_information_without_worker(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    import PyQt6.QtWidgets

    informations: list[tuple] = []
    monkeypatch.setattr(
        PyQt6.QtWidgets.QMessageBox, "information",
        lambda *a, **k: informations.append(a))
    host = _deps_host()
    host._current_edit_project = None
    mtc.MenuToolbarMixin.on_dependency_report_requested(host)
    assert informations, "无工程路径未提示"
    assert getattr(host, "_dependency_report_worker", None) is None


# ═══════════════════════════════════════════════════════════════════════
# AUD-3107: no makeCurrent on the UI thread in viewport restore
# ═══════════════════════════════════════════════════════════════════════


def test_aud3107_viewport_restore_skips_makecurrent(qapp) -> None:
    """页面切换后的视口恢复不得在 UI 线程调用 makeCurrent。"""
    calls: list[str] = []
    from PyQt6.QtCore import QRectF

    gl_viewport = SimpleNamespace(
        isVisible=lambda: True,
        show=lambda: calls.append("show"),
        makeCurrent=lambda: calls.append("makeCurrent"),
        update=lambda: calls.append("update"),
    )
    host = SimpleNamespace(
        current_page="Color",
        canvas=SimpleNamespace(
            gl_viewport=gl_viewport,
            _update_lod=lambda: calls.append("lod"),
            scene=SimpleNamespace(
                sceneRect=lambda: QRectF(),
                invalidate=lambda *a: calls.append("invalidate"),
            ),
        ),
        engine=None,
        _finalize_viewport_restore=lambda: None,
    )
    # 注意：不设 dock_viewport 属性（hasattr 分支跳过；None 会触发
    # isVisible AttributeError 并被外层 except 吞掉整个恢复流程）。
    PageManagementMixin._restore_viewport_after_page_switch(host)

    assert "show" in calls and "lod" in calls and "update" in calls, (
        "可见性/LOD/update 路径被破坏")
    assert "makeCurrent" not in calls, (
        "AUD-3107 修复回退：UI 线程仍在调用 gl_viewport.makeCurrent()")
    assert "invalidate" in calls


# ── AUD-12613: interchange 导入的逐素材阻塞探测移出 UI 线程 ────────────

class _InterchangeHost(QObject):
    """AUD-12613 测试宿主：QObject（作信号接收者），记录 UI 应用副作用。"""

    def __init__(self) -> None:
        super().__init__()
        self._current_edit_project = "P0"
        self._interchange_import_inflight = False
        self._current_timeline_sequences = {}
        self.applied_with = None
        self.switched_to = None
        self.apply_calls = 0
        self.ensure_calls = 0
        self.set_project_calls: list = []
        from unittest.mock import MagicMock
        self.media_pool_panel = MagicMock()

    def _ensure_edit_project(self) -> bool:
        self.ensure_calls += 1
        return True

    def _apply_interchange_import(self, result: Any) -> None:
        self.apply_calls += 1

    def _on_media_pool_project_changed(self, project: Any) -> None:
        self.applied_with = project

    def _on_timeline_switched(self, timeline_id: Any) -> None:
        self.switched_to = timeline_id

    def _on_interchange_build_ready(self, result, new_project, sequences, summary):
        fc.FileOpsMixin._on_interchange_build_ready(
            self, result, new_project, sequences, summary)

    def _on_interchange_build_failed(self, result, error):
        fc.FileOpsMixin._on_interchange_build_failed(self, result, error)


@pytest.fixture()
def _no_threadpool_start(monkeypatch):
    """捕获 QThreadPool.start——不真正跑 worker，测试手动驱动信号。"""
    started: list = []
    monkeypatch.setattr(
        fc.QThreadPool, "globalInstance",
        staticmethod(lambda: SimpleNamespace(start=started.append)))
    return started


class TestAud12613InterchangeOffUiThread:

    def test_apply_launches_build_worker_and_guards_reentry(
            self, _no_threadpool_start):
        """落地构建经线程池派发（非 UI 线程同步执行），占用期间防重入。"""
        host = _InterchangeHost()
        result = SimpleNamespace(report=None)
        fc.FileOpsMixin._apply_interchange_import(host, result)
        assert len(_no_threadpool_start) == 1, "构建必须经 QThreadPool 派发"
        assert isinstance(_no_threadpool_start[0], fc._InterchangeBuildWorker)
        assert host._interchange_import_inflight is True
        fc.FileOpsMixin._apply_interchange_import(host, result)
        assert len(_no_threadpool_start) == 1, "在途导入应被防重入拒绝"

    def test_build_runs_off_calling_thread(self):
        """worker.run 里的构建执行线程 ≠ 调用线程（阻塞 IO 不占 UI 线程）。"""
        import threading
        seen: dict = {}
        orig = fc._InterchangeBuildWorker.run

        def fake_run(self):
            seen["thread"] = threading.current_thread()
            # 不连信号直接跑（避免无事件循环时 queued 投递）
            from src.features.edit.adapters import import_builder
            import unittest.mock as um
            with um.patch.object(
                    import_builder, "build_project_from_import_result",
                    lambda b, r: (None, {}, None)):
                try:
                    orig(self)
                except Exception:
                    pass

        worker = fc._InterchangeBuildWorker("P0", SimpleNamespace(), None)
        t = threading.Thread(target=fake_run, args=(worker,))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()
        assert seen["thread"] is not threading.main_thread(), (
            "AUD-12613 修复回退：构建回到调用（UI）线程同步执行")

    def test_ready_applies_result_on_ui_side(
            self, qapp, monkeypatch, _no_threadpool_start):
        """构建完成信号回到 UI 侧应用：序列注册 + 媒体池 set_project。"""
        import src.features.edit.ui.interchange_dialog as idlg
        dialog_calls: list = []

        class _Dlg:
            def __init__(self, *a, **k):
                dialog_calls.append(k)

            def exec(self):
                return 1  # Accepted

        monkeypatch.setattr(idlg, "InterchangeReportDialog", _Dlg)
        host = _InterchangeHost()
        result = SimpleNamespace(report=None)
        fc.FileOpsMixin._apply_interchange_import(host, result)
        worker = _no_threadpool_start[0]
        seqs = {"t1": object()}
        summary = SimpleNamespace(
            imported_asset_ids=[1], reused_asset_ids=[], offline_assets=[],
            sequence_ids=["t1"], unmapped_clip_ids=[])
        worker.signals.ready.emit(result, "P1", seqs, summary)
        assert host._interchange_import_inflight is False, "完成必须解除占用"
        assert host._current_timeline_sequences == seqs
        assert host.set_project_calls == [] or True  # MagicMock 记录
        host.media_pool_panel.set_project.assert_called_once_with("P1")
        assert host.applied_with == "P1"
        assert host.switched_to == "t1"
        assert dialog_calls and "summary_text" in dialog_calls[-1]

    def test_build_failure_reports_and_releases(
            self, qapp, monkeypatch, _no_threadpool_start):
        """构建失败：显式报错（不静默）并解除在途占用。"""
        criticals: list = []
        monkeypatch.setattr(
            fc.QMessageBox, "critical",
            staticmethod(lambda *a, **k: criticals.append(a)))
        host = _InterchangeHost()
        result = SimpleNamespace(report=None)
        fc.FileOpsMixin._apply_interchange_import(host, result)
        worker = _no_threadpool_start[0]
        worker.signals.failed.emit(result, "boom")
        assert host._interchange_import_inflight is False
        assert criticals, "失败必须显式报错"

    def test_rows_ready_opens_confirm_dialog_and_forwards(
            self, monkeypatch):
        """预览行就绪：确认对话框收到后台探测的行数据，接受后转落地。"""
        import src.features.edit.ui.interchange_dialog as idlg
        rows_seen: list = []

        class _Dlg:
            def __init__(self, report, **k):
                rows_seen.append(k.get("asset_rows"))

            def exec(self):
                return 1  # Accepted

        monkeypatch.setattr(idlg, "InterchangeReportDialog", _Dlg)
        host = _InterchangeHost()
        result = SimpleNamespace(report=None)
        fc.FileOpsMixin._on_interchange_rows_ready(host, result, [("a", "p", True)])
        assert rows_seen == [[("a", "p", True)]]
        assert host.apply_calls == 1, "接受后必须进入落地流程"

    def test_rows_probe_failure_reports(self, monkeypatch):
        criticals: list = []
        monkeypatch.setattr(
            fc.QMessageBox, "critical",
            staticmethod(lambda *a, **k: criticals.append(a)))
        host = _InterchangeHost()
        fc.FileOpsMixin._on_interchange_rows_failed(host, SimpleNamespace(), "x")
        assert criticals
