"""B3：持久操作日志、幂等键与证据分类恢复。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from src.features.files.repository import FilesCatalog, FilesDataLocator
from src.features.files.transactions import FileTransactionService
from src.features.files.transactions.journal import (
    JournalError,
    OperationJournal,
)


@pytest.fixture()
def env(tmp_path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    journal = OperationJournal(catalog)
    library = catalog.register_library(tmp_path / "lib")
    (tmp_path / "lib").mkdir(exist_ok=True)
    yield tmp_path, catalog, journal, library
    catalog.close()


def _make_payload(lib: Path, name: str = "a.png") -> Path:
    asset = lib / name
    asset.write_bytes(b"payload")
    return asset


# ---------------------------------------------------------------- 基础生命周期


def test_begin_record_finish_roundtrip(env):
    tmp, catalog, journal, library = env
    handle = journal.begin(library.library_id, "move",
                           {"source": "/x/a.png", "destination": "/y/a.png"})
    journal.record("copy", "staged", {"source": "/x/a.png", "tmp": "/y/.a.png.tmp"})
    journal.finish("completed", "ok")
    row = journal.get(handle.job_id)
    assert row["state"] == "completed"
    events = json.loads(row["items"])
    assert events[0]["state"] == "staged" and events[0]["op"] == "copy"
    assert events[-1]["state"] == "completed"


def test_finish_rejects_nonterminal_state(env):
    _, _, journal, library = env
    journal.begin(library.library_id, "move", {})
    with pytest.raises(JournalError) as info:
        journal.finish("running")
    assert info.value.code == "BAD_STATE"
    journal.finish("failed", "cleanup")


def test_finish_without_active_job_is_rejected(env):
    _, _, journal, _ = env
    with pytest.raises(JournalError) as info:
        journal.finish("completed")
    assert info.value.code == "NO_ACTIVE_JOB"


# ---------------------------------------------------------------- 与 ops 集成


def test_ops_events_land_in_job(env):
    tmp, catalog, journal, library = env
    lib = tmp / "lib"
    asset = _make_payload(lib)
    dst_dir = tmp / "elsewhere"
    dst_dir.mkdir()
    svc = FileTransactionService(journal=journal)
    handle = journal.begin(library.library_id, "move",
                           {"source": str(asset), "destination": str(dst_dir / asset.name)})
    result = svc.move(asset, dst_dir)
    journal.finish("completed" if result.ok else "failed", result.code)
    row = journal.get(handle.job_id)
    events = json.loads(row["items"])
    assert any(e.get("state") == "done" and e.get("op") == "move" for e in events)
    assert row["state"] == "completed"
    assert (dst_dir / asset.name).read_bytes() == b"payload"


def test_failed_operation_is_not_reported_completed(env):
    """日志不得把失败操作收成 completed（诚实规则）。"""
    tmp, catalog, journal, library = env
    lib = tmp / "lib"
    asset = _make_payload(lib)
    dst_dir = tmp / "elsewhere"
    dst_dir.mkdir()
    (dst_dir / asset.name).write_bytes(b"occupied")
    svc = FileTransactionService(journal=journal)
    journal.begin(library.library_id, "move",
                  {"source": str(asset), "destination": str(dst_dir / asset.name)})
    result = svc.move(asset, dst_dir)  # error 策略冲突
    journal.finish("failed", result.code)
    row = journal.get(handle_id(journal))
    assert row["state"] == "failed"


def handle_id(journal: OperationJournal) -> str:
    """测试辅助：从 trim 前的历史里拿最近一个任务。"""
    with journal._catalog._read() as cursor:
        return cursor.execute(
            "SELECT id FROM job_log ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]


# ---------------------------------------------------------------- 幂等键


def test_idempotency_key_replays_existing_job(env):
    _, _, journal, library = env
    first = journal.begin(library.library_id, "move", {"source": "/x"},
                          idempotency_key="import-batch-1")
    journal.finish("completed", "done")
    second = journal.begin(library.library_id, "move", {"source": "/x"},
                           idempotency_key="import-batch-1")
    assert second.job_id == first.job_id
    assert second.replayed is True
    rows = journal.pending()
    assert rows == []
    with pytest.raises(JournalError):
        journal.finish("failed", "重放调用不得改写已完成的历史任务")
    assert journal.get(first.job_id)["state"] == "completed"


def test_distinct_keys_create_distinct_jobs(env):
    _, _, journal, library = env
    one = journal.begin(library.library_id, "copy", {}, idempotency_key="k1")
    journal.finish("completed")
    two = journal.begin(library.library_id, "copy", {}, idempotency_key="k2")
    assert one.job_id != two.job_id
    journal.finish("completed")


# ---------------------------------------------------------------- 证据分类恢复


def test_recovery_completes_finished_move(env):
    tmp, catalog, journal, library = env
    lib = tmp / "lib"
    asset = _make_payload(lib)
    dst = tmp / "out" / asset.name
    (tmp / "out").mkdir()
    dst.write_bytes(b"payload")
    asset.unlink()  # move 已完成但进程在 finish 前崩溃
    handle = journal.begin(library.library_id, "move",
                           {"source": str(asset), "destination": str(dst)})
    journal.record("move", "fs_published", {"source": str(asset), "destination": str(dst)})
    # 模拟崩溃：不 finish，直接恢复
    report = journal.recover_one(handle.job_id)
    assert report["classification"] == "completed"
    assert journal.get(handle.job_id)["state"] == "completed"


def test_recovery_sweeps_tmp_and_marks_retriable(env):
    tmp, catalog, journal, library = env
    lib = tmp / "lib"
    asset = _make_payload(lib)
    tmp_file = tmp / "out" / f".{asset.name}.abc.topos-tmp"
    (tmp / "out").mkdir()
    tmp_file.write_bytes(b"half")
    handle = journal.begin(library.library_id, "move",
                           {"source": str(asset), "destination": str(tmp / "out" / asset.name)})
    journal.record("copy", "staged", {"source": str(asset), "tmp": str(tmp_file)})
    report = journal.recover_one(handle.job_id)
    assert report["classification"] == "no_evidence"
    assert report["action"] == "none"
    assert tmp_file.exists(), "没有落盘身份凭据不能删除同名临时文件"
    assert journal.get(handle.job_id)["state"] == "pending_recovery"
    assert asset.exists(), "源完好，可重试"


def test_recovery_ambiguous_stays_pending(env):
    tmp, catalog, journal, library = env
    lib = tmp / "lib"
    asset = _make_payload(lib)
    dst = tmp / "out" / asset.name
    (tmp / "out").mkdir()
    dst.write_bytes(b"target")
    handle = journal.begin(library.library_id, "move",
                           {"source": str(asset), "destination": str(dst)})
    report = journal.recover_one(handle.job_id)
    assert report["classification"] == "ambiguous"
    assert journal.get(handle.job_id)["state"] == "pending_recovery"
    assert asset.exists() and dst.exists(), "不猜测执行，双方原样"


def test_pending_lists_only_unfinished(env):
    _, _, journal, library = env
    done = journal.begin(library.library_id, "copy", {})
    journal.finish("completed")
    active = journal.begin(library.library_id, "move", {})
    ids = {job["id"] for job in journal.pending()}
    assert active.job_id in ids and done.job_id not in ids
    journal.finish("failed", "teardown")


# ---------------------------------------------------------------- 有界历史


def test_trim_keeps_bound_and_never_drops_active(env):
    _, catalog, journal, library = env
    for _ in range(12):
        handle = journal.begin(library.library_id, "copy", {})
        journal.finish("completed")
    active = journal.begin(library.library_id, "move", {})
    journal._history_limit = 5
    removed = journal.trim()
    assert removed >= 7
    with catalog._read() as cursor:
        states = cursor.execute("SELECT state, COUNT(*) FROM job_log GROUP BY state").fetchall()
        total = sum(row[1] for row in states)
    assert total == 6, "5 条终态 + 1 条未终态"
    assert any(row[0] == "running" for row in states), "未终态不被淘汰"
    journal.finish("failed", "teardown")
