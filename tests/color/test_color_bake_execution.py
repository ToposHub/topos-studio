"""R06-B: 离线 bake 执行闭环（协议扩展 + 后端注册表 + 结果会话 + service）。

覆盖（计划 R06-B 验证方法）：
- 协议扩展字段向后兼容（既有 4 用例不动照过）；
- 后端 fail-closed（未注册/不可用拒绝、not_implemented 显式）；
- 结果会话内容寻址/去重/释放/闭包（含 compound 递归、missing 保留）；
- service 生命周期：成功发布一次、取消丢弃无暂存、verify 失败丢弃、
  关工程 reset 释放结果文件、重复接受幂等；
- 图命令 set_bake/remove_bake + 资源指纹失效 + 预检 bake_unresolvable。
"""
from __future__ import annotations

import threading
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QEvent

from src.features.color.core.color_bake_backend import (
    BakeBackendError, BakeBackendOutput, BakeCancelled,
    ColorBakeBackendRegistry, install_default_backends,
)
from src.features.color.core.color_bake_protocol import (
    ColorBakeBroker, ColorBakeCapability, ColorBakeStatus, make_bake_request,
)
from src.features.color.core.color_bake_result_registry import (
    BakeResultSession, collect_graph_bake_closure, ensure_graph_bake_closure,
)
from src.features.color.core.color_bake_service import ColorBakeService

from tests.color.test_color_graph_schema import _serial_graph


class _Sink:
    def __init__(self) -> None:
        self.statuses: list[Any] = []
        self.results: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str]] = []
        self._event = threading.Event()

    def wait_result(self, timeout: float = 5.0) -> bool:
        """等待结果；期间泵事件（跨线程排队信号需要 processEvents 投递）。"""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _pump(rounds=4)
            if self._event.is_set():
                return True
            time.sleep(0.02)
        return self._event.is_set()

    def on_status(self, status: Any) -> None:
        self.statuses.append(status)

    def on_result(self, task_id: str, uri: str) -> None:
        self.results.append((task_id, uri))
        self._event.set()

    def on_failed(self, task_id: str, code: str) -> None:
        self.failures.append((task_id, code))
        self._event.set()


def _pump(app: Any = None, rounds: int = 40) -> None:
    app = app or QCoreApplication.instance()
    for _ in range(rounds):
        if app is not None:
            app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _ok_backend(output_bytes: bytes = b"bake-payload-v1",
                delay: float = 0.0):
    def worker(request, snapshot, source, cancel_check, progress_cb):
        import time
        progress_cb(10.0)
        if delay:
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if cancel_check():
                    raise BakeCancelled()
                time.sleep(0.01)
        if cancel_check():
            raise BakeCancelled()
        progress_cb(90.0)
        return BakeBackendOutput(payload=output_bytes, format_hint="npz",
                                 metadata={"width": 4, "height": 4})
    return worker


def _request(**overrides: Any):
    base = dict(
        target={"session_id": "s", "stack_id": "st", "node_id": "n1",
                "instance_id": "i1"},
        stack_id="st", graph_revision=3, source_content_revision=7,
        resource_id="ai_up", params={"scale": 4}, spec_version="1",
        backend_id="fake_ok")
    base.update(overrides)
    return make_bake_request(**base)


@pytest.fixture
def registry() -> ColorBakeBackendRegistry:
    import src.features.color.core.color_bake_backend as backend_mod
    original = backend_mod._registry
    fresh = ColorBakeBackendRegistry()
    backend_mod._registry = fresh
    fresh.register(
        ColorBakeCapability(backend_id="fake_ok", categories=("test",),
                            available=True),
        _ok_backend())
    yield fresh
    backend_mod._registry = original


# ── 协议扩展 ──────────────────────────────────────────────────────

def test_request_extension_fields_default_compatible():
    request = make_bake_request(
        target={"node_id": "n"}, stack_id="s", graph_revision=1,
        source_content_revision=1, resource_id="r")
    assert request.backend_id == "" and request.frame_range is None
    full = make_bake_request(
        target={"node_id": "n"}, stack_id="s", graph_revision=1,
        source_content_revision=1, resource_id="r", backend_id="onnx_tile",
        source_clip_id="c1", frame_range=(0, 24), cancel_token=object())
    assert full.frame_range == (0, 24)
    status = ColorBakeStatus(task_id="t", phase="running", percent=12.5)
    assert status.bake_uri == ""


# ── 后端注册表 fail-closed ────────────────────────────────────────

def test_registry_fail_closed(registry):
    worker, cap, reject = registry.resolve("fake_ok")
    assert reject == "" and worker is not None
    worker, cap, reject = registry.resolve("not_registered")
    assert reject == "backend_unregistered" and worker is None
    registry.register(
        ColorBakeCapability(backend_id="off", categories=(), available=False,
                            disabled_reason="not_implemented"),
        _ok_backend())
    _w, cap, reject = registry.resolve("off")
    assert reject == "backend_unavailable"
    assert cap is not None and cap.disabled_reason == "not_implemented"


def test_default_backends_declared_fail_closed():
    install_default_backends()
    install_default_backends()  # 幂等
    from src.features.color.core.color_bake_backend import (
        get_bake_backend_registry,
    )
    caps = {c.backend_id: c for c in get_bake_backend_registry().capabilities()}
    assert "onnx_tile" in caps and "gpu_prefix" in caps and "stylize" in caps
    assert caps["gpu_prefix"].available is False
    assert caps["gpu_prefix"].disabled_reason == "not_implemented"
    _w, _c, reject = get_bake_backend_registry().resolve("gpu_prefix")
    assert reject == "backend_unavailable"


# ── 结果会话 ──────────────────────────────────────────────────────

def test_result_session_content_addressed_and_release(tmp_path):
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    entry = session.register_result(b"payload-A", "npz", {"resource_id": "r"})
    same = session.register_result(b"payload-A", "npz", {"resource_id": "r"})
    assert same.resource_id == entry.resource_id  # 内容寻址去重
    assert session.status_of(entry.uri) == "registered"
    assert session.release(entry.uri) is True
    assert session.status_of(entry.uri) == "unregistered"
    assert session.release(entry.uri) is False


def test_result_session_save_and_reset(tmp_path):
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    session.register_result(b"payload-B", "npz", {"k": 1})
    data, files = session.save_payload()
    assert data and "resources" in data and len(files) == 1
    session.reset()
    assert session.save_payload() == (None, None)


def test_graph_bake_closure_recursive_and_missing_preserved():
    from src.features.color.core.color_graph_service import ColorGraphService
    service = ColorGraphService(effect_schema_provider=lambda rid: None,
                                lut_reference_validator=lambda ref: True,
                                bake_reference_validator=lambda ref: True)
    graph = _serial_graph(1)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    applied = service.apply_intent(graph, "set_node_bake", {
        "node_id": node.node_id,
        "bake_ref": "topos-res://bake_abc"})
    assert applied.ok, applied.diagnostics
    refs = collect_graph_bake_closure(applied.graph)
    assert refs == ["topos-res://bake_abc"]
    session = BakeResultSession(staging_root="/tmp/does-not-matter")
    closure = ensure_graph_bake_closure([applied.graph], session)
    assert closure["missing"] == ["topos-res://bake_abc"]  # 保留不删


# ── service 生命周期 ──────────────────────────────────────────────

def test_service_success_publishes_once(qapp, tmp_path, registry):
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    service = ColorBakeService(session=session,
                               broker=ColorBakeBroker())
    sink = _Sink()
    service.status_changed.connect(sink.on_status)
    service.result_ready.connect(sink.on_result)
    service.bake_failed.connect(sink.on_failed)
    source = np.zeros((8, 8, 3), dtype=np.float32)
    request = _request()
    try:
        task_id = service.submit_bake(
            request, snapshot=object(), source_frame=source,
            verify=lambda: True)
        assert task_id == request.task_id
        assert sink.wait_result(5.0), f"未完成: {sink.failures}"
        task, uri = sink.results[0]
        assert task == task_id and uri.startswith("topos-res://bake_")
        assert session.status_of(uri) == "registered"
        phases = [s.phase for s in sink.statuses]
        assert "queued" in phases and "succeeded" in phases
    finally:
        service.reset()


def test_service_cancel_discards_without_artifact(qapp, tmp_path):
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    service = ColorBakeService(session=session, broker=ColorBakeBroker())
    sink = _Sink()
    service.status_changed.connect(sink.on_status)
    service.result_ready.connect(sink.on_result)
    service.bake_failed.connect(sink.on_failed)
    import src.features.color.core.color_bake_backend as backend_mod
    backend_mod._registry.register(
        ColorBakeCapability(backend_id="slow", categories=("test",),
                            available=True),
        _ok_backend(delay=2.0))
    try:
        request = _request(backend_id="slow")
        service.submit_bake(request, snapshot=object(),
                            source_frame=np.zeros((4, 4, 3), np.float32))
        _pump()
        cancelled = service.cancel(request.task_id)
        assert cancelled is True
        assert service.result_ready is not None
        assert not sink.results  # 无结果发布
        assert service.broker.pending() == []
        assert session.save_payload() == (None, None)  # 无暂存条目
    finally:
        backend_mod._registry._workers.pop("slow", None)
        backend_mod._registry._capabilities.pop("slow", None)
        service.reset()


def test_service_verify_failure_discards(qapp, tmp_path, registry):
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    service = ColorBakeService(session=session, broker=ColorBakeBroker())
    sink = _Sink()
    service.result_ready.connect(sink.on_result)
    service.status_changed.connect(sink.on_status)
    try:
        service.bake_failed.connect(sink.on_failed)
        request = _request()
        # verify=False 模拟目标节点/实例已被删除（R04 token 校验失败）
        service.submit_bake(request, snapshot=object(),
                            source_frame=np.zeros((4, 4, 3), np.float32),
                            verify=lambda: False)
        assert sink.wait_result(5.0)
        assert not sink.results
        assert service.broker.pending() == []
        assert session.save_payload() == (None, None)
        assert sink.failures and sink.failures[0][1] == "verify_failed"
        phases = [s.phase for s in sink.statuses]
        assert "cancelled" in phases  # verify 失败 → cancelled 语义
    finally:
        service.reset()


def test_service_rejects_unknown_backend(qapp, tmp_path, registry):
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    service = ColorBakeService(session=session, broker=ColorBakeBroker())
    sink = _Sink()
    service.bake_failed.connect(sink.on_failed)
    try:
        result = service.submit_bake(
            _request(backend_id="ghost"), snapshot=object(),
            source_frame=np.zeros((4, 4, 3), np.float32))
        assert result is None
        assert sink.failures and sink.failures[0][1] == "backend_unregistered"
    finally:
        service.reset()


def test_service_reset_releases_result_files(qapp, tmp_path, registry):
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    service = ColorBakeService(session=session, broker=ColorBakeBroker())
    sink = _Sink()
    service.result_ready.connect(sink.on_result)
    service.status_changed.connect(sink.on_status)
    try:
        service.submit_bake(_request(), snapshot=object(),
                            source_frame=np.zeros((4, 4, 3), np.float32),
                            verify=lambda: True)
        assert sink.wait_result(5.0)
        _uri = sink.results[0][1]
        assert session.status_of(_uri) == "registered"
        service.reset()  # 关工程：释放全部结果文件
        assert session.status_of(_uri) == "unregistered"
        assert session.save_payload() == (None, None)
    finally:
        service.reset()


# ── 图命令 + 指纹 + 预检 ──────────────────────────────────────────

def _service_with_bake(tmp_path):
    from src.features.color.core.color_graph_service import ColorGraphService
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    entry = session.register_result(b"payload-C", "npz", {"task": "t"})

    def valid(ref: str) -> bool:
        return session.status_of(ref) == "registered"

    service = ColorGraphService(effect_schema_provider=lambda rid: None,
                                lut_reference_validator=lambda ref: True,
                                bake_reference_validator=valid)
    return service, session, entry


def test_set_and_remove_node_bake_roundtrip(tmp_path):
    service, session, entry = _service_with_bake(tmp_path)
    graph = _serial_graph(1)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    applied = service.apply_intent(graph, "set_node_bake", {
        "node_id": node.node_id, "bake_ref": entry.uri})
    assert applied.ok, applied.diagnostics
    assert applied.graph.graph_revision == graph.graph_revision + 1
    resources = next(n for n in applied.graph.nodes
                     if n.node_id == node.node_id).params["resources"]
    assert resources["bake_ref"] == entry.uri
    removed = service.apply_intent(applied.graph, "remove_node_bake", {
        "node_id": node.node_id})
    assert removed.ok
    assert "bake_ref" not in (
        next(n for n in removed.graph.nodes
             if n.node_id == node.node_id).params.get("resources") or {})


def test_set_node_bake_rejects_unknown_reference(tmp_path):
    service, _session, _entry = _service_with_bake(tmp_path)
    graph = _serial_graph(1)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    rejected = service.apply_intent(graph, "set_node_bake", {
        "node_id": node.node_id, "bake_ref": "topos-res://bake_missing"})
    assert not rejected.ok
    assert rejected.diagnostics[0].code == "resource_unknown"


def test_bake_ref_enters_resource_fingerprint(tmp_path):
    from src.features.color.core.color_graph_snapshot import (
        compute_resource_fingerprint, freeze_graph,
    )
    service, _session, entry = _service_with_bake(tmp_path)
    graph = _serial_graph(1)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    applied = service.apply_intent(graph, "set_node_bake", {
        "node_id": node.node_id, "bake_ref": entry.uri})
    assert applied.ok
    before = compute_resource_fingerprint(freeze_graph(
        graph, session_id="s").graph)
    after = compute_resource_fingerprint(freeze_graph(
        applied.graph, session_id="s").graph)
    assert before != after, "bake_ref 必须进入资源指纹（缓存失效）"


def test_preflight_reports_bake_unresolvable(tmp_path):
    from src.features.color.core.color_graph_preflight import (
        preflight_graph_resources,
    )
    service, _session, entry = _service_with_bake(tmp_path)
    graph = _serial_graph(1)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    applied = service.apply_intent(graph, "set_node_bake", {
        "node_id": node.node_id, "bake_ref": entry.uri})
    assert applied.ok
    # 预检期会话视角不可解析（如结果文件被外部删除）→ bake_unresolvable
    issues = preflight_graph_resources(
        applied.graph, bake_probe=lambda ref: False)
    codes = {(issue.node_id, issue.code) for issue in issues}
    assert (node.node_id, "bake_unresolvable") in codes


# ── 保存/重开闭包 roundtrip（R09 语义） ──────────────────────────

def test_bake_closure_survives_project_save_and_reopen(tmp_path):
    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.features.edit.core.project import EditProject
    from src.shared.project.resource_resolver import (
        ProjectResourceResolver, ResourceIndex, set_active_project_resolver,
    )

    # 保存路径使用全局单例会话（与生产装配一致）
    from src.features.color.core.color_bake_result_registry import (
        get_bake_result_session,
    )
    global_session = get_bake_result_session()
    global_session.reset()
    payload_entry = global_session.register_result(b"payload-C", "npz",
                                                   {"task": "reopen"})
    from src.features.color.core.color_graph_service import ColorGraphService
    service = ColorGraphService(
        effect_schema_provider=lambda rid: None,
        lut_reference_validator=lambda ref: True,
        bake_reference_validator=lambda ref: (
            global_session.status_of(ref) == "registered"))
    entry = payload_entry
    project, _sid = EditProject.create(name="r06b").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[_sid]
    # 新栈懒建图为 None：直接以串行图落栈，再用服务命令写 bake ref
    graph = _serial_graph(1)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    applied = service.apply_intent(graph, "set_node_bake", {
        "node_id": node.node_id, "bake_ref": entry.uri})
    assert applied.ok, applied.diagnostics
    object.__setattr__(stack, "color_graph", applied.graph)

    target = tmp_path / "r06b.topos"
    io = EditProjectIO()
    assert io.save_project(target, project, create_backup=False,
                           use_ai_callback=False)
    assert io._last_bake_closure_report["missing"] == []

    reopened = io.load_project(target)
    assert reopened is not None
    # 工程加载路径把 bake 会话绑定到包 resolver（R06-B 接线）；
    # 验收语义：重开后图中 bake 引用可解析（registered）。
    from src.features.color.core.color_bake_result_registry import (
        get_bake_result_session,
    )
    bound = get_bake_result_session()
    try:
        assert bound.status_of(entry.uri) == "registered", \
            "重开后 bake 引用不可解析（闭包失败）"
        # 且包内确实带上了结果文件（索引含 bake 条目）
        data, _files = bound.save_payload()
        rids = {item.get("resource_id")
                for item in (data or {}).get("resources", [])}
        assert entry.resource_id in rids
    finally:
        bound.reset()


def test_repeated_bakes_leave_no_orphan_resources(qapp, tmp_path):
    """计划验收：重复 100 次后无孤儿资源或活动 worker。"""
    session = BakeResultSession(staging_root=str(tmp_path / "stage"))
    service = ColorBakeService(session=session, broker=ColorBakeBroker())
    sink = _Sink()
    service.status_changed.connect(sink.on_status)
    service.result_ready.connect(sink.on_result)
    service.bake_failed.connect(sink.on_failed)
    try:
        for index in range(100):
            sink._event.clear()
            sink.results.clear()
            payload = f"payload-{index % 7}".encode()  # 7 种内容 → 去重
            import src.features.color.core.color_bake_backend as backend_mod
            backend_mod._registry.register(
                ColorBakeCapability(backend_id="loop", categories=("t",),
                                    available=True),
                _ok_backend(output_bytes=payload))
            request = _request(backend_id="loop")
            service.submit_bake(request, snapshot=object(),
                                source_frame=np.zeros((4, 4, 3), np.float32),
                                verify=lambda: True)
            assert sink.wait_result(5.0), f"iteration {index} 未完成"
        # 内容寻址去重：7 种 payload → 7 个条目，无孤儿
        assert len(session.entries()) == 7
        assert service.broker.pending() == []
        assert service.active_task_ids() == []
        thread = service._worker_thread
        if thread is not None:
            service.reset()
    finally:
        service.reset()
