"""NR-00/NR-01 回归：文件安全、数据与工程事务（FR-01~05、FR-07）。

来源：docs/validation/files_replacement_audit_20261116/repro/ 的隔离复现
脚本转为 pytest 回归，问题编号与《Files 整体复查计划》一一对应。

证据边界与审计一致：全部用 tmp_path/临时 SQLite，不碰真实素材与用户库。
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILE_EXPLORER = ROOT / "external" / "file_explorer"
if str(FILE_EXPLORER) not in sys.path:
    sys.path.insert(0, str(FILE_EXPLORER))

from external.file_explorer import file_tasks  # noqa: E402


def _wait(task, timeout: float = 4.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = task.snapshot()
        if snapshot["status"] in ("completed", "failed", "cancelled"):
            return snapshot
        time.sleep(0.005)
    raise AssertionError(f"task did not finish: {task.snapshot()}")


@pytest.fixture()
def service():
    svc = file_tasks.FileTaskService(lambda old, new: None)
    yield svc
    svc.shutdown(wait=True)


# ---------------------------------------------------------------------------
# FR-01（P0）：同目标并发 copy/move/rename —— 未授权覆盖必须为零
# ---------------------------------------------------------------------------

def _optimistic_resolve(svc, monkeypatch):
    """模拟 exists 检查与落盘之间的 TOCTOU 窗口：resolve 永远放行。"""
    def resolve(item, policy):  # noqa: ANN001 - 与被替换方法签名一致
        return item.destination
    monkeypatch.setattr(svc, "_resolve_destination", resolve)


def test_fr01_move_error_policy_never_overwrites_existing_target(tmp_path, service, monkeypatch):
    """resolve 过期（竞态窗口）时，error 策略必须原子失败，不得覆盖既有文件。"""
    svc = service
    src = tmp_path / "in" / "victim.mov"
    src.parent.mkdir()
    src.write_bytes(b"SOURCE")
    target = tmp_path / "victim.mov"
    target.write_bytes(b"VICTIM")
    # 计划目标名与既有 VICTIM 同名：resolve 过期放行后，O_EXCL 仍必须挡住
    monkeypatch.setattr(svc, "_resolve_destination",
                        lambda item, policy: str(target))

    snap = _wait(svc.start("move", [str(src)], policy="error",
                           options={"destination": str(tmp_path)}))
    assert snap["status"] == "failed"
    assert snap["failures"][0]["error"] == "目标已存在"
    assert target.read_bytes() == b"VICTIM"   # 既有文件分毫未动
    assert src.read_bytes() == b"SOURCE"      # 源保留


def test_fr01_copy_error_policy_atomic_reject(tmp_path, service, monkeypatch):
    svc = service
    src = tmp_path / "src.mov"
    src.write_bytes(b"SOURCE")
    target = tmp_path / "victim.mov"
    target.write_bytes(b"VICTIM")
    monkeypatch.setattr(svc, "_resolve_destination",
                        lambda item, policy: str(target))

    snap = _wait(svc.start("copy", [str(src)], policy="error",
                           options={"destination": str(tmp_path)}))
    assert snap["status"] == "failed"
    assert target.read_bytes() == b"VICTIM"
    assert not list(tmp_path.glob("*.topos-tmp"))


def test_fr01_rename_policy_retry_finds_unique_destination(tmp_path, service, monkeypatch):
    """rename 策略遇到竞态窗口：换下一个唯一名重试，不覆盖。"""
    svc = service
    src = tmp_path / "a" / "same.mov"
    src.parent.mkdir()
    src.write_bytes(b"SOURCE")
    target = tmp_path / "same.mov"
    target.write_bytes(b"VICTIM")
    _optimistic_resolve(svc, monkeypatch)

    snap = _wait(svc.start("copy", [str(src)], policy="rename",
                           options={"destination": str(tmp_path)}))
    assert snap["status"] == "completed"
    assert target.read_bytes() == b"VICTIM"
    assert (tmp_path / "same (1).mov").read_bytes() == b"SOURCE"


def test_fr01_overwrite_policy_replaces_but_never_both_survive(tmp_path, service, monkeypatch):
    """overwrite 是显式授权的替换：目标被替换、源按操作语义消失，无残留。"""
    svc = service
    src = tmp_path / "a" / "same.mov"
    src.parent.mkdir()
    src.write_bytes(b"NEW")
    target = tmp_path / "same.mov"
    target.write_bytes(b"OLD")
    _optimistic_resolve(svc, monkeypatch)

    snap = _wait(svc.start("move", [str(src)], policy="overwrite",
                           options={"destination": str(tmp_path)}))
    assert snap["status"] == "completed"
    assert target.read_bytes() == b"NEW"
    assert not src.exists()
    assert not list(tmp_path.glob("*.topos-tmp"))


def test_fr01_concurrent_same_destination_error_policy_no_data_loss(tmp_path, service, monkeypatch):
    """两份同名文件并发 move 到同目录（error）：只允许一份成功，另一份源必须保留。"""
    svc = service
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "out").mkdir()
    a = tmp_path / "a" / "same.mov"
    b = tmp_path / "b" / "same.mov"
    a.write_bytes(b"ORIGINAL A")
    b.write_bytes(b"ORIGINAL B")

    # 第一任务进入传输体后短暂停住，确保第二任务撞上目标预约
    release = threading.Event()
    entered = threading.Event()
    original_payload = svc._move_file_payload

    def stalled(source, destination, policy, ctx):
        if not entered.is_set():
            entered.set()
            release.wait(2)
        return original_payload(source, destination, policy, ctx)

    monkeypatch.setattr(svc, "_move_file_payload", stalled)
    jobs = [svc.start("move", [str(p)], policy="error",
                      options={"destination": str(tmp_path / "out")})
            for p in (a, b)]
    assert entered.wait(2)
    time.sleep(0.05)  # 让第二任务抵达预约点
    release.set()
    snaps = [_wait(job) for job in jobs]

    statuses = sorted(s["status"] for s in snaps)
    assert statuses in (["completed", "failed"], ["failed", "failed"])
    out = tmp_path / "out" / "same.mov"
    if out.exists():
        # 成功的那份内容完好；失败那份源必须保留
        assert out.read_bytes() in (b"ORIGINAL A", b"ORIGINAL B")
        survivors = [p for p in (a, b) if p.exists()]
        assert len(survivors) == len([s for s in snaps if s["status"] == "failed"])
    else:
        assert all(s["status"] == "failed" for s in snaps)
        assert a.read_bytes() == b"ORIGINAL A"
        assert b.read_bytes() == b"ORIGINAL B"
    # 未授权覆盖为零：目标内容必须是其中一份的原始字节
    assert not out.exists() or out.read_bytes() in (b"ORIGINAL A", b"ORIGINAL B")


@pytest.mark.parametrize("operation", ["copy", "move"])
@pytest.mark.parametrize("policy", ["error", "overwrite", "rename"])
def test_fr01_concurrent_matrix_no_unauthorized_overwrite(tmp_path, service, operation, policy):
    """同目标并发 copy/move × error/overwrite/rename：任何交错都不得静默丢数据。"""
    svc = service
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "out").mkdir()
    a = tmp_path / "a" / "same.mov"
    b = tmp_path / "b" / "same.mov"
    a.write_bytes(b"ORIGINAL A")
    b.write_bytes(b"ORIGINAL B")

    jobs = [svc.start(operation, [str(p)], policy=policy,
                      options={"destination": str(tmp_path / "out")})
            for p in (a, b)]
    snaps = [_wait(job) for job in jobs]

    outputs = {p.name: p.read_bytes()
               for p in (tmp_path / "out").iterdir() if p.is_file()}
    # rename 策略两份都必须到达不同名字；error/overwrite 允许部分失败
    if policy == "rename":
        assert len(outputs) == 2
        assert set(outputs.values()) == {b"ORIGINAL A", b"ORIGINAL B"}
        assert all(s["status"] == "completed" for s in snaps)
    elif policy == "error":
        assert sum(s["status"] == "completed" for s in snaps) == 1
        assert len(outputs) == 1
        winner = next(i for i, s in enumerate(snaps) if s["status"] == "completed")
        assert list(outputs.values()) == [(b"ORIGINAL A", b"ORIGINAL B")[winner]]
    else:
        assert any(s["status"] == "completed" for s in snaps)
        assert len(outputs) == 1
        for content in outputs.values():
            assert content in (b"ORIGINAL A", b"ORIGINAL B")  # 显式授权覆盖
    # move 语义：成功的源消失、失败的源必须保留
    for path, snap in zip((a, b), snaps):
        if snap["status"] == "completed":
            if operation == "move":
                assert not path.exists()
            else:
                assert path.read_bytes() in (b"ORIGINAL A", b"ORIGINAL B")
        else:
            assert path.exists()
    assert not list((tmp_path / "out").glob("*.topos-tmp"))


# ---------------------------------------------------------------------------
# FR-02：copy + overwrite 不得跟随目标符号链接/硬链接覆盖其他文件
# ---------------------------------------------------------------------------

def test_fr02_copy_overwrite_does_not_follow_destination_symlink(tmp_path, service, monkeypatch):
    svc = service
    (tmp_path / "src").mkdir()
    (tmp_path / "out").mkdir()
    source = tmp_path / "src" / "secret.mov"
    source.write_text("REPLACEMENT")
    locked = tmp_path / "locked"
    locked.mkdir()
    secret = locked / "secret.mov"
    secret.write_text("PROTECTED ORIGINAL")
    link = tmp_path / "out" / "secret.mov"
    link.symlink_to(secret)
    _optimistic_resolve(svc, monkeypatch)

    snap = _wait(svc.start("copy", [str(source)], policy="overwrite",
                           options={"destination": str(tmp_path / "out")}))
    assert snap["status"] == "completed"
    # 被替换的是链接本身；锁目录内文件内容不变
    assert secret.read_text() == "PROTECTED ORIGINAL"
    assert not link.is_symlink()
    assert link.read_text() == "REPLACEMENT"


def test_fr02_move_overwrite_hardlink_target_isolation(tmp_path, service, monkeypatch):
    """目标是指向受保护文件的硬链接：替换只断开该目录项，不动文件本体。"""
    svc = service
    (tmp_path / "src").mkdir()
    (tmp_path / "out").mkdir()
    source = tmp_path / "src" / "clip.mov"
    source.write_text("NEW CLIP")
    secret = tmp_path / "protected.mov"
    secret.write_text("PROTECTED ORIGINAL")
    alias = tmp_path / "out" / "clip.mov"
    alias.hardlink_to(secret)
    _optimistic_resolve(svc, monkeypatch)

    snap = _wait(svc.start("move", [str(source)], policy="overwrite",
                           options={"destination": str(tmp_path / "out")}))
    assert snap["status"] == "completed"
    assert secret.read_text() == "PROTECTED ORIGINAL"
    assert alias.read_text() == "NEW CLIP"
    assert not source.exists()


# ---------------------------------------------------------------------------
# FR-03：_move_meta_rows 列映射 + 单事务 + 失败不吞
# ---------------------------------------------------------------------------

@pytest.fixture()
def backend(tmp_path, monkeypatch):
    """routes/trash 全套指向临时 SQLite（审计脚本同款接线，不碰用户库）。"""
    pytest.importorskip("PyQt6.QtWebEngineCore")
    import db as fe_db
    import main as fe_main
    import routes as fe_routes
    import trash as fe_trash

    database = tmp_path / "audit.sqlite"

    def connect():
        return sqlite3.connect(database)

    monkeypatch.setattr(fe_db, "_db", connect)
    monkeypatch.setattr(fe_db, "_resolve_db_file", lambda: str(database))
    fe_db.init_db()
    monkeypatch.setattr(fe_routes, "_db", connect)
    monkeypatch.setattr(fe_routes, "invalidate_cache", lambda path: None)
    monkeypatch.setattr(fe_routes.thumb_engine, "get_thumb_path",
                        lambda *a, **kw: str(tmp_path / "no-thumbnail"))
    monkeypatch.setattr(fe_trash, "_db", connect)
    monkeypatch.setattr(fe_trash, "invalidate_cache", lambda path: None)
    (tmp_path / "trash").mkdir(exist_ok=True)  # 真实 get_trash_dir 会 makedirs，替身不会
    monkeypatch.setattr(fe_trash, "get_trash_dir", lambda: str(tmp_path / "trash"))
    return SimpleNamespace(db=fe_db, main=fe_main, routes=fe_routes,
                           trash=fe_trash, connect=connect)


def test_fr03_move_meta_rows_remaps_ocr_and_markers(tmp_path, backend):
    conn = backend.connect()
    conn.execute("INSERT INTO file_meta(path,tags,rating) VALUES('/old.mov','tag',4)")
    conn.execute("INSERT INTO ocr_text(file_path,text) VALUES('/old.mov','words')")
    conn.execute("INSERT INTO markers(file_path,t_in,label) VALUES('/old.mov',1,'mark')")
    conn.commit()
    conn.close()

    backend.routes._move_meta_rows("/old.mov", "/new.mov")

    conn = backend.connect()
    assert conn.execute("SELECT path FROM file_meta").fetchall() == [("/new.mov",)]
    assert conn.execute("SELECT file_path FROM ocr_text").fetchall() == [("/new.mov",)]
    assert conn.execute("SELECT file_path FROM markers").fetchall() == [("/new.mov",)]
    conn.close()


def test_fr03_move_meta_rows_failure_rolls_back_and_raises(tmp_path, backend, monkeypatch):
    conn = backend.connect()
    conn.execute("INSERT INTO file_meta(path,tags,rating) VALUES('/old.mov','tag',4)")
    conn.commit()
    conn.close()

    real_connect = backend.connect

    class _FailingConn:
        """markers 更新注入失败：验证 rollback + 抛出（不再吞异常伪成功）。"""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if "markers" in sql and "UPDATE" in sql:
                raise sqlite3.OperationalError("injected markers failure")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(backend.routes, "_db", lambda: _FailingConn(real_connect()))

    with pytest.raises(RuntimeError, match="markers"):
        backend.routes._move_meta_rows("/old.mov", "/new.mov")

    conn = real_connect()
    assert conn.execute("SELECT path FROM file_meta").fetchall() == [("/old.mov",)]
    conn.close()


# ---------------------------------------------------------------------------
# FR-04：回收站完整快照 + 恢复关系重映射
# ---------------------------------------------------------------------------

def test_fr04_trash_and_restore_name_conflict_keeps_relations(tmp_path, backend):
    trash = backend.trash
    media = tmp_path / "media.mov"
    media.write_text("original media")
    conn = backend.connect()
    conn.execute(
        "INSERT INTO file_meta(path,tags,rating,width,stack_id) VALUES(?, 'tag',5,3840,1)",
        (str(media),))
    conn.execute("INSERT INTO collections(id,name) VALUES(1,'collection')")
    conn.execute("INSERT INTO collection_items(collection_id,file_path) VALUES(1,?)",
                 (str(media),))
    conn.execute("INSERT INTO stacks(id,cover_path) VALUES(1,?)", (str(media),))
    conn.commit()
    conn.close()

    records, errors = trash.trash_items([str(media)])
    assert errors == []

    # 删除后：原路径被另一份素材占位（同名恢复必然成副本）
    media.write_text("different media now occupies original path")

    restored, restore_errors = trash.restore_items([records[0]["id"]])
    assert restore_errors == []
    restored_path = restored[0]["path"]
    assert restored_path != str(media)  # "(恢复)" 副本

    conn = backend.connect()
    # 技术字段与组织关系全部跟到副本（不再指向原位置/丢失）
    row = conn.execute(
        "SELECT tags,rating,width,stack_id FROM file_meta WHERE path=?",
        (restored_path,)).fetchone()
    assert row == ("tag", 5, 3840, 1)
    assert conn.execute(
        "SELECT file_path FROM collection_items").fetchall() == [(restored_path,)]
    assert conn.execute(
        "SELECT cover_path FROM stacks WHERE id=1").fetchone() == (restored_path,)
    conn.close()
    # 占位文件内容不被恢复流程改动
    assert media.read_text() == "different media now occupies original path"
    assert Path(restored_path).read_text() == "original media"


def test_fr04_trash_removes_ghost_relations(tmp_path, backend):
    """删除后集合/堆叠不得继续显示幽灵成员（恢复进快照、留待恢复时重建）。"""
    trash = backend.trash
    media = tmp_path / "ghost.mov"
    media.write_text("x")
    conn = backend.connect()
    conn.execute("INSERT INTO file_meta(path) VALUES(?)", (str(media),))
    conn.execute("INSERT INTO collections(id,name) VALUES(7,'c7')")
    conn.execute("INSERT INTO collection_items(collection_id,file_path) VALUES(7,?)",
                 (str(media),))
    conn.commit()
    conn.close()

    records, errors = trash.trash_items([str(media)])
    assert errors == []
    conn = backend.connect()
    assert conn.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM file_meta").fetchone()[0] == 0
    conn.close()

    restored, restore_errors = trash.restore_items([records[0]["id"]])
    assert restore_errors == []
    conn = backend.connect()
    assert conn.execute(
        "SELECT file_path FROM collection_items").fetchall() == [(str(media),)]
    conn.close()


# ---------------------------------------------------------------------------
# FR-05：任务锁校验必须覆盖真实写目标（mkdir 的 options.parent 等）
# ---------------------------------------------------------------------------

def _client(backend):
    backend.main.app.config["TESTING"] = True
    return backend.main.app.test_client(), {"X-Auth-Token": backend.main.APP_TOKEN}


def _wait_job(client, headers, payload):
    job = payload.get("job")
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        job = client.get("/api/tasks/status", query_string={"job_id": job["job_id"]},
                         headers=headers).get_json()["job"]
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        time.sleep(0.01)
    raise AssertionError(job)


def test_fr05_mkdir_locked_parent_rejected(tmp_path, backend):
    locked = tmp_path / "locked"
    locked.mkdir()
    unlocked = tmp_path / "unlocked"
    unlocked.mkdir()
    conn = backend.connect()
    conn.execute("INSERT INTO folder_locks(path,pwd_hash) VALUES(?, 'test')", (str(locked),))
    conn.commit()
    conn.close()

    client, headers = _client(backend)
    response = client.post("/api/tasks/start",
                           json={"operation": "mkdir", "targets": [str(unlocked)],
                                 "options": {"parent": str(locked), "name": "bypassed"}},
                           headers=headers)
    assert response.status_code == 423
    assert not (locked / "bypassed").exists()  # 磁盘无变化


def test_fr05_rename_new_name_inside_locked_dir_rejected(tmp_path, backend):
    locked = tmp_path / "locked"
    locked.mkdir()
    inside = locked / "inside.mov"
    inside.write_text("x")
    conn = backend.connect()
    conn.execute("INSERT INTO folder_locks(path,pwd_hash) VALUES(?, 'test')", (str(locked),))
    conn.commit()
    conn.close()

    client, headers = _client(backend)
    response = client.post("/api/tasks/start",
                           json={"operation": "rename", "targets": [str(inside)],
                                 "options": {"new_name": "renamed.mov"}},
                           headers=headers)
    assert response.status_code == 423
    assert inside.exists() and not (locked / "renamed.mov").exists()


def test_fr05_restore_into_locked_original_path_rejected(tmp_path, backend):
    locked = tmp_path / "locked"
    locked.mkdir()
    conn = backend.connect()
    conn.execute("INSERT INTO folder_locks(path,pwd_hash) VALUES(?, 'test')", (str(locked),))
    conn.execute(
        "INSERT INTO trash(original_path,item_name,is_dir,trashed_name,deleted_at,meta_json) "
        "VALUES(?, 'secret.mov', 0, 'secret.mov__1', 1, '[]')",
        (str(locked / "secret.mov"),))
    conn.commit()
    conn.close()

    client, headers = _client(backend)
    response = client.post("/api/tasks/start",
                           json={"operation": "restore", "targets": ["1"]},
                           headers=headers)
    assert response.status_code == 423


# ---------------------------------------------------------------------------
# FR-07：有界池、分块取消、进度、运行中任务不被历史淘汰
# ---------------------------------------------------------------------------

def test_fr07_cancel_mid_copy_leaves_no_partial_file(tmp_path, service, monkeypatch):
    svc = service
    svc._chunk_size = 64 * 1024
    source = tmp_path / "big.mov"
    source.write_bytes(b"A" * (256 * 1024))
    out = tmp_path / "out"
    out.mkdir()

    holder = {}
    original = svc._copy_stream

    def cancel_after_first_chunk(src, fd, ctx):
        with open(src, "rb", buffering=0) as fh:
            chunk = fh.read(64 * 1024)
            os_write_all(fd, chunk)
            ctx.add_bytes(len(chunk))
        holder["ctx"] = ctx
        ctx.task.cancel_requested = True  # 模拟传输中途用户取消
        ctx.check_cancel()  # 下一块前的取消检查

    def os_write_all(fd, chunk):
        import os as _os
        view = memoryview(chunk)
        while view:
            written = _os.write(fd, view)
            view = view[written:]

    monkeypatch.setattr(svc, "_copy_stream", cancel_after_first_chunk)
    task = svc.start("copy", [str(source)], policy="error",
                     options={"destination": str(out)})
    snap = _wait(task)
    assert snap["status"] == "cancelled"
    assert snap["items"][0]["status"] == "skipped"
    # 无半成品：目标不出现、无临时残留、字节进度如实上报
    assert not (out / "big.mov").exists()
    assert not list(out.glob("*.topos-tmp"))
    assert snap["progress"]["bytes_done"] == 64 * 1024


def test_fr07_pool_is_bounded(tmp_path, service, monkeypatch):
    svc = service
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def slow(item, policy, options):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.03)
        with lock:
            state["active"] -= 1
        return item.destination

    monkeypatch.setattr(svc, "_do_copy", slow)
    files = []
    for index in range(12):
        path = tmp_path / f"f{index}.mov"
        path.write_bytes(b"x")
        files.append(str(path))
    jobs = [svc.start("copy", [path], options={"destination": str(tmp_path)})
            for path in files]
    for job in jobs:
        _wait(job)
    assert state["peak"] <= file_tasks.FileTaskService.MAX_WORKERS
    assert state["peak"] >= 2  # 确实并发了，而不是串行假绿


def test_fr07_running_task_survives_history_trim(service, monkeypatch):
    svc = service
    svc.MAX_HISTORY = 3
    release = threading.Event()

    def slow(item, policy, options):
        if "long" in item.source:
            release.wait(3)  # 只有长任务阻塞，填充历史的短任务即时结束
        return item.destination

    monkeypatch.setattr(svc, "_do_copy", slow)
    # 先灌满历史（失败任务立即结束），再放一个长任务 + 更多短任务触发淘汰
    for index in range(8):
        _wait(svc.start("copy", ["/nonexistent"], options={"destination": "/tmp"}))
    long_running = svc.start("copy", ["/nonexistent-long"],
                             options={"destination": "/tmp"})
    for index in range(5):
        _wait(svc.start("copy", [f"/nonexistent{index}"],
                        options={"destination": "/tmp"}))
    assert svc.status(long_running.job_id) is not None  # 运行中任务不被淘汰
    release.set()
    _wait(long_running)


def test_fr07_shutdown_waits_for_running_task(tmp_path, service, monkeypatch):
    svc = service
    release = threading.Event()

    def slow(item, policy, options):
        release.wait(2)
        return item.destination

    monkeypatch.setattr(svc, "_do_copy", slow)
    path = tmp_path / "z.mov"
    path.write_bytes(b"x")
    task = svc.start("copy", [str(path)], options={"destination": str(tmp_path)})
    shutdown_thread = threading.Thread(target=svc.shutdown, kwargs={"wait": True})
    shutdown_thread.start()
    time.sleep(0.05)
    assert task.snapshot()["status"] not in ("completed", "failed")  # 仍在等待
    release.set()
    shutdown_thread.join(3)
    assert not shutdown_thread.is_alive()
    assert task.snapshot()["status"] in ("completed", "failed")
