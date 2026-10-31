"""P2-7 结构化日志 + 任务历史测试。

测：
    1. logging_config：JsonFormatter / TextFormatter 输出格式
    2. setup_logging 幂等 + 环境变量 + 文件输出
    3. TaskHistory：record 上下文管理器（done/failed/duration）
    4. TaskHistory：list_runs / get_run / aggregate_stats
    5. schema v2 迁移（index_history 表）
"""

from __future__ import annotations

import json
import logging
import pytest


# ---------------------------------------------------------------------- #
# schema v2 迁移
# ---------------------------------------------------------------------- #

def test_schema_v2_has_index_history(initialized_db):
    """schema v2 创建了 index_history 表。"""
    import sqlite3
    conn = sqlite3.connect(str(initialized_db))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='index_history'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_schema_version_is_2(initialized_db):
    """当前 schema 版本是 2（含 index_history）。"""
    from apps.topos_vault.db import schema
    assert schema.get_schema_version() == 2


# ---------------------------------------------------------------------- #
# logging_config：格式化器
# ---------------------------------------------------------------------- #

def test_json_formatter_outputs_valid_json():
    """JsonFormatter 输出每行一个合法 JSON。"""
    from apps.topos_vault.logging_config import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="打标完成 %d", args=(10,), exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)  # 应是合法 JSON
    assert data["msg"] == "打标完成 10"
    assert data["level"] == "INFO"
    assert data["logger"] == "test"
    assert "ts" in data
    assert "func" in data


def test_json_formatter_includes_extra_fields():
    """extra={"extra": {...}} 字段并入 JSON。"""
    from apps.topos_vault.logging_config import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="x", args=(), exc_info=None,
    )
    record.extra = {"files": 10, "phase": "ingest"}
    output = formatter.format(record)
    data = json.loads(output)
    assert data["files"] == 10
    assert data["phase"] == "ingest"


def test_text_formatter_has_level_and_msg():
    """TextFormatter 输出含级别和消息。"""
    from apps.topos_vault.logging_config import TextFormatter
    formatter = TextFormatter()
    record = logging.LogRecord(
        name="test", level=logging.WARNING, pathname="x.py", lineno=1,
        msg="警告信息", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "WARNING" in output
    assert "警告信息" in output


def test_json_formatter_handles_exception():
    """异常信息进 exception 字段。"""
    from apps.topos_vault.logging_config import JsonFormatter
    formatter = JsonFormatter()
    try:
        raise ValueError("test error")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="x.py", lineno=1,
            msg="err", args=(), exc_info=sys.exc_info(),
        )
    output = formatter.format(record)
    data = json.loads(output)
    assert "exception" in data
    assert "ValueError" in data["exception"]


# ---------------------------------------------------------------------- #
# logging_config：setup_logging
# ---------------------------------------------------------------------- #

def test_setup_logging_idempotent(vault_dir, monkeypatch):
    """重复调 setup_logging 不重复加 handler。"""
    from apps.topos_vault import logging_config
    logging_config.setup_logging(level="INFO", fmt="text", force=True)
    n1 = len(logging.getLogger().handlers)
    # 不 force 再调一次
    logging_config.setup_logging(level="INFO", fmt="text", force=False)
    n2 = len(logging.getLogger().handlers)
    assert n2 == n1


def test_setup_logging_force_reconfigures(vault_dir):
    """force=True 重新配置（清旧 handler）。"""
    from apps.topos_vault import logging_config
    logging_config.setup_logging(level="DEBUG", fmt="text", force=True)
    assert logging.getLogger().level == logging.DEBUG
    logging_config.setup_logging(level="WARNING", fmt="text", force=True)
    assert logging.getLogger().level == logging.WARNING


def test_setup_logging_env_override(vault_dir, monkeypatch):
    """TOPOS_VAULT_LOG_LEVEL/FORMAT 环境变量生效。"""
    from apps.topos_vault import logging_config
    monkeypatch.setenv("TOPOS_VAULT_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("TOPOS_VAULT_LOG_FORMAT", "json")
    logging_config.setup_logging(force=True)
    assert logging.getLogger().level == logging.ERROR


def test_setup_logging_writes_file(vault_dir):
    """log_file=True → 写 <vault_dir>/logs/vault.log。"""
    from apps.topos_vault import logging_config
    from apps.topos_vault.paths import get_vault_dir
    logging_config.setup_logging(level="INFO", fmt="text", log_file=True, force=True)
    logging.getLogger("test_p27").info("写入测试")
    # flush 所有 handler
    for h in logging.getLogger().handlers:
        h.flush()
    log_file = get_vault_dir() / "logs" / "vault.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "写入测试" in content


# ---------------------------------------------------------------------- #
# TaskHistory
# ---------------------------------------------------------------------- #

def test_task_history_records_done_run(initialized_db):
    """record 上下文管理器记一次成功运行。"""
    from apps.topos_vault.task_history import TaskHistory
    history = TaskHistory()

    with history.record("index", {"path": "/x", "phased": True}) as run:
        run.total = 10
        run.done = 8
        run.skipped = 1
        run.failed = 1

    # 查回
    runs = history.list_runs()
    assert len(runs) == 1
    r = runs[0]
    assert r.command == "index"
    assert r.status == "done"
    assert r.total == 10
    assert r.done == 8
    assert r.duration_sec is not None
    assert r.duration_sec >= 0
    assert r.finished_at is not None


def test_task_history_records_failed_run(initialized_db):
    """异常时 status=failed + detail 记错误。"""
    from apps.topos_vault.task_history import TaskHistory
    history = TaskHistory()

    with pytest.raises(ValueError):
        with history.record("index", {"path": "/bad"}) as run:
            raise ValueError("故意失败")

    runs = history.list_runs()
    assert len(runs) == 1
    r = runs[0]
    assert r.status == "failed"
    assert "ValueError" in r.detail.get("error", "")
    assert r.duration_sec is not None  # 异常也记了 duration


def test_task_history_get_run_by_id(initialized_db):
    """get_run(run_id) 查单次。"""
    from apps.topos_vault.task_history import TaskHistory
    history = TaskHistory()

    with history.record("cluster") as run:
        pass

    fetched = history.get_run(run.run_id)
    assert fetched is not None
    assert fetched.run_id == run.run_id
    assert fetched.command == "cluster"


def test_task_history_get_run_unknown_returns_none(initialized_db):
    """未知 run_id → None。"""
    from apps.topos_vault.task_history import TaskHistory
    history = TaskHistory()
    assert history.get_run("nonexistent") is None


def test_task_history_list_runs_ordered_desc(initialized_db):
    """list_runs 按开始时间倒序。"""
    import time
    from apps.topos_vault.task_history import TaskHistory
    history = TaskHistory()

    for i in range(3):
        with history.record("index", {"i": i}):
            time.sleep(0.01)

    runs = history.list_runs()
    assert len(runs) == 3
    # 最新的在前
    assert runs[0].args["i"] == 2
    assert runs[2].args["i"] == 0


def test_task_history_aggregate_stats(initialized_db):
    """aggregate_stats 聚合统计正确。"""
    from apps.topos_vault.task_history import TaskHistory
    history = TaskHistory()

    with history.record("index") as run:
        run.total, run.done = 10, 10
    with history.record("index") as run:
        run.total, run.done = 5, 3
    with pytest.raises(Exception):
        with history.record("index"):
            raise Exception("x")

    stats = history.aggregate_stats()
    assert stats["total_runs"] == 3
    assert stats["success"] == 2
    assert stats["failed"] == 1
    assert stats["success_rate"] == 2 / 3
    assert stats["total_items"] == 15
    assert stats["done_items"] == 13


def test_task_history_aggregate_empty(initialized_db):
    """无运行记录 → aggregate_stats 返回 total_runs=0。"""
    from apps.topos_vault.task_history import TaskHistory
    history = TaskHistory()
    stats = history.aggregate_stats()
    assert stats["total_runs"] == 0


def test_task_history_to_dict(initialized_db):
    """TaskRun.to_dict 序列化。"""
    from apps.topos_vault.task_history import TaskHistory, TaskRun
    run = TaskRun(run_id="r1", command="test", args={"a": 1}, started_at="now")
    d = run.to_dict()
    assert d["run_id"] == "r1"
    assert d["command"] == "test"
    assert d["args"] == {"a": 1}
    assert d["status"] == "running"
