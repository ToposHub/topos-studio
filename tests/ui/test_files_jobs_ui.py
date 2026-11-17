"""D5：文件任务台账与列表——进度/逐项错误/取消/重试失败项/归属不丢。"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.app.ui.native_files_jobs import FilesJobsRegistry, JobsDialog  # noqa: E402
from src.app.ui.native_files_transport import NativeFilesTransport  # noqa: E402
from src.features.files.repository import FilesDataLocator  # noqa: E402
from src.features.files.repository.catalog import FilesCatalog  # noqa: E402
from src.features.files.services.facade import FilesFacade  # noqa: E402


# ---------------------------------------------------------------------------
# Registry：记账与重试参数
# ---------------------------------------------------------------------------

def test_registry_tracks_progress_results_and_retry_payload():
    registry = FilesJobsRegistry()
    registry.on_task_started({
        "job_id": "job-1", "operation": "copy", "total": 3, "completed": 0,
        "targets": ["/a.png", "/b.png", "/deny.png"],
        "policy": "rename", "options": {"destination": "/dst"}})
    registry.on_task_started({
        "job_id": "job-1", "operation": "copy", "total": 3, "completed": 2})
    assert registry.running_count() == 1
    registry.on_task_finished({
        "job_id": "job-1", "operation": "copy", "total": 3, "completed": 2,
        "successes": [{"path": "/a.png"}, {"path": "/b.png"}],
        "failures": [{"path": "/deny.png", "error": "没有权限", "code": "PERMISSION_DENIED"}],
        "cancelled": False})
    job = registry.job("job-1")
    assert job["state"] == "failed"
    assert registry.running_count() == 0
    assert registry.retry_request("job-1") == (
        "copy", ["/deny.png"], "rename", {"destination": "/dst"})
    assert registry.retry_request("missing") is None

    registry.on_task_started({"job_id": "job-2", "operation": "trash",
                              "total": 1, "completed": 1})
    registry.on_task_finished({"job_id": "job-2", "cancelled": True})
    assert registry.job("job-2")["state"] == "cancelled"
    assert registry.retry_request("job-2") is None  # 无失败项不可重试

    registry.on_task_failed("提交失败：素材数据不可用")
    assert any(not job["job_id"] and job["state"] == "failed"
               for job in registry.jobs())
    registry.clear_finished()
    assert registry.jobs() == []


def test_registry_survives_dialog_lifecycle(qtbot):
    """关窗不丢账：registry 独立于对话框存活。"""
    registry = FilesJobsRegistry()
    dialog = JobsDialog(registry)
    qtbot.addWidget(dialog)
    registry.on_task_started({"job_id": "j", "operation": "move",
                              "total": 2, "completed": 0,
                              "targets": ["/x", "/y"]})
    assert dialog.table.rowCount() == 1
    dialog.close()
    registry.on_task_finished({"job_id": "j", "completed": 2, "total": 2,
                               "successes": [{"path": "/x"}, {"path": "/y"}],
                               "failures": []})
    assert registry.job("j")["state"] == "done"  # 关窗后事实照记


# ---------------------------------------------------------------------------
# Dialog：渲染/取消/重试意图
# ---------------------------------------------------------------------------

def test_jobs_dialog_renders_and_emits_intents(qtbot):
    registry = FilesJobsRegistry()
    dialog = JobsDialog(registry)
    qtbot.addWidget(dialog)
    registry.on_task_started({"job_id": "run-1", "operation": "copy",
                              "total": 4, "completed": 1,
                              "targets": ["/m/first.mov", "/m/x.mov"],
                              "policy": "error", "options": {"destination": "/d"}})
    registry.on_task_finished({
        "job_id": "bad-1", "operation": "rename", "total": 1, "completed": 0,
        "successes": [],
        "failures": [{"path": "/m/old.png", "error": "目标已存在", "code": "DEST_EXISTS"}],
        "cancelled": False})
    # 运行中在前；首行是 run-1
    assert dialog.table.rowCount() == 2
    first_label = dialog.table.item(0, 0).text()
    assert "复制" in first_label and "2 项" in first_label
    assert dialog.table.item(0, 1).text() == "1/4"
    assert dialog.table.item(0, 2).text() == "进行中"

    dialog.table.selectRow(0)
    assert dialog.cancel_button.isEnabled()
    assert not dialog.retry_button.isEnabled()  # 运行中无失败项
    cancels = []
    dialog.cancel_requested.connect(cancels.append)
    dialog._cancel_selected()
    assert cancels == ["run-1"]

    dialog.table.selectRow(1)
    assert not dialog.cancel_button.isEnabled()  # 已终态不可取消
    assert dialog.retry_button.isEnabled()
    assert "目标已存在" in dialog.summary.text()
    retries = []
    dialog.retry_requested.connect(
        lambda op, targets, policy, options: retries.append((op, targets, policy, options)))
    dialog._retry_selected()
    assert retries == [("rename", ["/m/old.png"], "error", {})]


# ---------------------------------------------------------------------------
# Transport：首发任务事件携带重试所需参数
# ---------------------------------------------------------------------------

def test_transport_first_task_event_carries_call_params(qtbot, tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.png").write_bytes(b"a")
    transport = NativeFilesTransport(
        lambda: "", None, facade=FilesFacade(catalog, max_workers=1))
    started = []
    transport.task_started.connect(started.append)
    try:
        with qtbot.waitSignal(transport.task_finished, timeout=8000):
            transport.run_task("rename", [str(lib / "a.png")],
                               policy="rename",
                               options={"new_name": "b.png"})
        first = started[0]
        assert first["targets"] == [str(lib / "a.png")]
        assert first["policy"] == "rename"
        assert first["options"] == {"new_name": "b.png"}
        # 进度事件只有计数（不重复携带参数）
        assert all("targets" not in event for event in started[1:])
    finally:
        transport.shutdown()
        catalog.close()
