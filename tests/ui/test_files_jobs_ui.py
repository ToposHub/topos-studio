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


# ---------------------------------------------------------------------------
# D5'/B3：中断恢复入账与操作
# ---------------------------------------------------------------------------

def test_transport_recovery_flow(qtbot, tmp_path):
    """遗留未终态任务 → recovery_loaded；recover_job → recovery_applied。"""
    from src.features.files.transactions.journal import OperationJournal

    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    moved = lib / "left.png"
    moved.write_bytes(b"x")
    transport = NativeFilesTransport(
        lambda: "", None, facade=FilesFacade(catalog, max_workers=1))
    try:
        # 模拟上个进程中断：目标已就位、源已不在（=已完成证据）
        journal = transport._facade.journal
        journal.begin(library.library_id, "move",
                      {"source": str(moved), "destination": str(moved) + ".moved"},
                      idempotency_key="move:fake")
        (lib / "left.png.moved").write_bytes(b"x")
        moved.unlink()

        with qtbot.waitSignal(transport.recovery_loaded, timeout=8000) as listed:
            transport.load_recoveries()
        records = listed.args[0]
        assert len(records) == 1
        assert records[0]["operation"] == "move"
        assert records[0]["source"] == str(moved)

        with qtbot.waitSignal(transport.recovery_applied, timeout=8000) as applied:
            transport.recover_job(records[0]["job_id"])
        verdict = applied.args[0]
        assert verdict["state"] == "completed"
    finally:
        transport.shutdown()
        catalog.close()


def test_registry_and_dialog_handle_pending_recovery(qtbot):
    registry = FilesJobsRegistry()
    dialog = JobsDialog(registry)
    qtbot.addWidget(dialog)
    registry.on_recovery_loaded([
        {"job_id": "job-x", "operation": "move", "state": "renaming",
         "source": "/media/mid.mov"}])
    job = registry.job("job-x")
    assert job["state"] == "pending_recovery"
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 2).text() == "待恢复"

    dialog.table.selectRow(0)
    assert dialog.recover_button.isEnabled()
    assert not dialog.cancel_button.isEnabled()  # 不是运行中
    recovers = []
    dialog.recover_requested.connect(recovers.append)
    dialog._recover_selected()
    assert recovers == ["job-x"]

    registry.on_recovery_applied({"job_id": "job-x", "state": "completed",
                                  "classification": "completed", "action": "none"})
    assert registry.job("job-x")["state"] == "done"
    assert not dialog.recover_button.isEnabled()

    # 证据不足：保持待恢复并展示分类原因
    registry.on_recovery_loaded([
        {"job_id": "job-y", "operation": "copy", "state": "staged",
         "source": "/media/amb.png"}])
    registry.on_recovery_applied({"job_id": "job-y",
                                  "state": "pending_recovery",
                                  "classification": "ambiguous", "action": "none"})
    job_y = registry.job("job-y")
    assert job_y["state"] == "pending_recovery"
    assert "证据不足" in job_y["failures"][0]["error"]
