"""B07 / B09：文件任务服务（真实临时目录，逐项结果、冲突策略、取消、路径传播）。"""
from __future__ import annotations

import threading
import time

import pytest

from external.file_explorer import file_tasks


@pytest.fixture()
def service():
    hooks: list[tuple[str, str]] = []
    svc = file_tasks.FileTaskService(lambda old, new: hooks.append((old, new)))
    yield svc, hooks


def _wait(task, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = task.snapshot()
        if snapshot["status"] in ("completed", "failed", "cancelled"):
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"task did not finish: {task.snapshot()}")


def test_mkdir_and_rename_report_destination(tmp_path, service):
    svc, hooks = service
    task = svc.start("mkdir", [str(tmp_path)], options={"name": "Shots"})
    snapshot = _wait(task)
    assert snapshot["status"] == "completed"
    assert (tmp_path / "Shots").is_dir()

    source = tmp_path / "Shots" / "A.mov"
    source.write_bytes(b"x")
    task = svc.start("rename", [str(source)], options={"new_name": "B.mov"})
    snapshot = _wait(task)
    assert snapshot["status"] == "completed"
    assert (tmp_path / "Shots" / "B.mov").exists()
    # B09：路径变化必须通知元数据平移，不能只改文件名
    assert hooks and hooks[-1][1].endswith("B.mov")


def test_conflict_policy_error_and_rename(tmp_path, service):
    svc, _ = service
    source = tmp_path / "A.mov"
    source.write_bytes(b"a")
    target = tmp_path / "B.mov"
    target.write_bytes(b"b")

    task = svc.start("copy", [str(source)], policy="error",
                     options={"destination": str(tmp_path)})
    snapshot = _wait(task)
    assert snapshot["status"] == "failed"
    assert snapshot["failures"][0]["error"] == "目标已存在"

    (tmp_path / "B.mov").unlink()
    (tmp_path / "A.mov").unlink()
    source.write_bytes(b"a")
    target.write_bytes(b"b")
    task = svc.start("copy", [str(source)], policy="rename",
                     options={"destination": str(tmp_path)})
    snapshot = _wait(task)
    assert snapshot["status"] == "completed"
    assert (tmp_path / "A (1).mov").exists()


def test_partial_failure_does_not_abort_batch(tmp_path, service):
    svc, _ = service
    good = tmp_path / "good.mov"
    good.write_bytes(b"g")
    missing = tmp_path / "missing.mov"
    task = svc.start("copy", [str(good), str(missing)], policy="error",
                     options={"destination": str(tmp_path / "out")})
    (tmp_path / "out").mkdir()
    snapshot = _wait(task)
    assert snapshot["status"] == "failed"
    assert snapshot["completed"] == 2
    assert len(snapshot["successes"]) == 1
    assert len(snapshot["failures"]) == 1
    assert (tmp_path / "out" / "good.mov").exists()


def test_cancel_marks_pending_items_skipped(tmp_path, service, monkeypatch):
    svc, _ = service
    release = threading.Event()
    entered = threading.Event()

    def slow_copy(item, policy, options):
        entered.set()
        release.wait(2)
        return item.destination

    monkeypatch.setattr(svc, "_do_move", slow_copy)
    files = []
    for index in range(4):
        path = tmp_path / f"f{index}.mov"
        path.write_bytes(b"x")
        files.append(str(path))
    task = svc.start("move", files, policy="error", options={"destination": str(tmp_path / "dst")})
    (tmp_path / "dst").mkdir()
    assert entered.wait(2)
    assert svc.cancel(task.job_id) is True
    release.set()
    snapshot = _wait(task)
    assert snapshot["status"] == "cancelled"
    assert snapshot["cancelled"] is True
    assert all(item["status"] in ("done", "skipped") for item in snapshot["items"])
    # 不允许"取消后仍继续跑完"
    assert sum(1 for item in snapshot["items"] if item["status"] == "skipped") >= 2


def test_unknown_operation_and_policy_are_rejected(tmp_path, service):
    svc, _ = service
    with pytest.raises(ValueError):
        svc.start("format_disk", [str(tmp_path)])
    with pytest.raises(ValueError):
        svc.start("mkdir", [str(tmp_path)], policy="yolo", options={"name": "x"})


def test_status_of_unknown_job_is_none(service):
    svc, _ = service
    assert svc.status("nope") is None
    assert svc.cancel("nope") is False
