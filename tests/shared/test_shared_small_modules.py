"""src/shared 小模块专测（P5-T3）：paths / logging / flash_diag / resource_snapshot。

这些模块此前只有零散审计回归测试触及；本套件按"公共 API 每个至少一条
正路径 + 一条失败路径"补齐。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import time
from types import SimpleNamespace

import pytest

import src.shared.paths.paths as paths_mod
from src.shared.logging.log_manager import LogManager


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def test_project_root_points_at_repo_root_not_src():
    root = paths_mod.get_project_root()
    # 本文件位于 <root>/tests/shared/，root 必须是其上两层且含 src/ 与 tests/
    assert os.path.isdir(os.path.join(root, "src"))
    assert os.path.isdir(os.path.join(root, "tests"))
    assert not root.endswith(os.sep + "src")


def test_project_root_frozen_uses_executable_dir(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/app/topos.exe")
    assert paths_mod.get_project_root() == "/opt/app"


@pytest.mark.parametrize(
    "fn",
    [paths_mod.get_lut_dir, paths_mod.get_hdri_dir, paths_mod.get_cache_dir],
)
def test_dirs_are_created_under_root(fn):
    d = fn()
    assert os.path.isdir(d)
    assert paths_mod.get_project_root() in d


def test_models_dir_subdir():
    d = paths_mod.get_models_dir("denoise")
    assert d.endswith(os.path.join("models", "denoise"))
    assert os.path.isdir(d)


def test_dir_creation_failure_degrades_with_warning(monkeypatch, caplog, tmp_path):
    # makedirs 失败必须留痕（warning）且仍返回路径交调用方处理
    monkeypatch.setattr(paths_mod, "get_project_root", lambda: str(tmp_path))
    def boom(path, exist_ok=False):
        raise OSError("disk full")
    monkeypatch.setattr(os, "makedirs", boom)
    with caplog.at_level(logging.WARNING, logger=paths_mod.__name__):
        d = paths_mod.get_lut_dir()
    assert d == os.path.join(str(tmp_path), "luts")
    assert any("LUT" in r.message for r in caplog.records)


def test_paths_survive_spaces_and_unicode(tmp_path, monkeypatch):
    # 计划 T3 要求：含中文/空格的根目录不破坏解析（仓库路径本身含空格）
    weird = tmp_path / "目录 有 空格"
    weird.mkdir()
    monkeypatch.setattr(paths_mod, "get_project_root", lambda: str(weird))
    assert paths_mod.get_lut_dir() == str(weird / "luts")
    assert os.path.isdir(weird / "luts")


# ---------------------------------------------------------------------------
# LogManager（root logger 状态全程快照恢复，避免污染 pytest 日志）
# ---------------------------------------------------------------------------


@pytest.fixture()
def root_logger_guard():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_instance = LogManager._instance
    yield root
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    LogManager._instance = saved_instance


class _Cfg:
    def __init__(self, **kv):
        self._kv = kv

    def get(self, k, default=None):
        return self._kv.get(k, default)


@pytest.mark.parametrize(
    ("level_str", "expect"),
    [("debug", logging.DEBUG), ("info", logging.INFO), ("warning", logging.WARNING),
     ("error", logging.ERROR), ("critical", logging.CRITICAL),
     ("bogus", logging.WARNING), (None, logging.WARNING)],
)
def test_log_level_matrix(root_logger_guard, level_str, expect):
    mgr = LogManager.initialize(_Cfg(log_level=level_str))
    assert mgr._log_level == expect
    assert logging.getLogger().level == expect


def test_file_logging_enabled_writes_to_configured_path(root_logger_guard, tmp_path):
    log = tmp_path / "logs" / "app.log"
    mgr = LogManager.initialize(_Cfg(enable_file_logging=True, log_file_path=str(log)))
    logging.getLogger("t.file").warning("hello-file")
    mgr.get_log_file_path() and None
    for h in logging.getLogger().handlers:
        h.flush()
    text = log.read_text(encoding="utf-8")
    assert "hello-file" in text
    assert os.path.isabs(mgr.get_log_file_path())


def test_file_logging_disabled_has_no_file_handler(root_logger_guard):
    mgr = LogManager.initialize(_Cfg(enable_file_logging=False))
    assert mgr._file_handler is None


def test_unwritable_log_path_degrades_to_console(root_logger_guard, tmp_path, caplog, monkeypatch):
    from logging.handlers import RotatingFileHandler
    def boom(*a, **k):
        raise OSError("read-only filesystem")
    monkeypatch.setattr("src.shared.logging.log_manager.RotatingFileHandler", boom)
    with caplog.at_level(logging.WARNING):
        mgr = LogManager.initialize(_Cfg(enable_file_logging=True,
                                         log_file_path=str(tmp_path / "x.log")))
    assert mgr._file_handler is None  # 降级不崩溃


def test_reconfigure_closes_old_handlers(root_logger_guard, tmp_path):
    log1 = tmp_path / "one.log"
    mgr = LogManager.initialize(_Cfg(enable_file_logging=True, log_file_path=str(log1)))
    old = mgr._file_handler
    assert old is not None
    log2 = tmp_path / "two.log"
    mgr._config_manager = _Cfg(enable_file_logging=True, log_file_path=str(log2))
    mgr.reconfigure()
    assert old.stream is None  # FileHandler.close() 关流置空
    assert mgr._file_handler is not old
    assert mgr._file_handler.baseFilename == os.path.abspath(str(log2))
    mgr._remove_file_handler()


def test_default_log_path_under_platform_data_dir(root_logger_guard, monkeypatch, tmp_path):
    import src.shared.config.config_manager as cm_mod
    monkeypatch.setattr(cm_mod.ConfigManager, "get_platform_data_dir",
                        classmethod(lambda cls: str(tmp_path)))
    mgr = LogManager.initialize(_Cfg())
    assert mgr.get_log_file_path() == os.path.join(str(tmp_path), "logs", "topos.log")


# ---------------------------------------------------------------------------
# flash_diag（模块在导入时读取 TOPOS_FLASH_DIAG，用 reload 驱动两种模式）
# ---------------------------------------------------------------------------


def test_flash_diag_disabled_by_default(monkeypatch):
    import src.shared.diagnostics.flash_diag as fd
    monkeypatch.delenv("TOPOS_FLASH_DIAG", raising=False)
    fd = importlib.reload(fd)
    try:
        assert fd.enabled() is False
        fd.event("x")  # no-op，绝不抛
    finally:
        importlib.reload(fd) if os.environ.get("TOPOS_FLASH_DIAG") else None


def test_flash_diag_event_writes_jsonl(monkeypatch, tmp_path):
    import src.shared.diagnostics.flash_diag as fd
    sink = tmp_path / "flash.jsonl"
    monkeypatch.setenv("TOPOS_FLASH_DIAG", str(sink))
    fd = importlib.reload(fd)
    try:
        assert fd.enabled() is True
        t0 = fd.now_ms()
        time.sleep(0.001)
        fd.event("frame_publish", frame_id=7)
        lines = sink.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["k"] == "frame_publish" and rec["frame_id"] == 7
        assert rec["t"] >= t0
    finally:
        monkeypatch.delenv("TOPOS_FLASH_DIAG", raising=False)
        importlib.reload(fd)


def test_flash_diag_never_raises_on_bad_path(monkeypatch):
    import src.shared.diagnostics.flash_diag as fd
    monkeypatch.setenv("TOPOS_FLASH_DIAG", "/nonexistent-dir-xyz/f.jsonl")
    fd = importlib.reload(fd)
    try:
        fd.event("boom")  # 打开失败被吞：诊断不得伤业务
    finally:
        monkeypatch.delenv("TOPOS_FLASH_DIAG", raising=False)
        importlib.reload(fd)


# ---------------------------------------------------------------------------
# resource_snapshot：注册表公共 API
# ---------------------------------------------------------------------------


def test_registry_register_collect_unregister():
    from src.shared.diagnostics.resource_snapshot import ResourceSnapshotRegistry
    reg = ResourceSnapshotRegistry()
    provider = lambda: {"size": 42}  # noqa: E731 —— StatsProvider 为可调用
    reg.register("cache", "tex_a", provider)
    collected = reg.collect()
    assert collected["cache"]["tex_a"]["size"] == 42

    reg.unregister("cache", "tex_a")
    assert "tex_a" not in reg.collect().get("cache", {})


def test_registry_rejects_unknown_category():
    from src.shared.diagnostics.resource_snapshot import ResourceSnapshotRegistry
    reg = ResourceSnapshotRegistry()
    with pytest.raises(ValueError, match="unknown resource category"):
        reg.register("not-a-category", "x", lambda: {})


def test_registry_provider_exception_recorded_not_raised():
    from src.shared.diagnostics.resource_snapshot import ResourceSnapshotRegistry
    reg = ResourceSnapshotRegistry()
    reg.register("export", "job_bad", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = reg.collect()
    assert "error" in out["export"]["job_bad"]


def test_registry_unregister_owner_prefix():
    from src.shared.diagnostics.resource_snapshot import ResourceSnapshotRegistry
    reg = ResourceSnapshotRegistry()
    reg.register("cache", "node1.tex", lambda: {})
    reg.register("cache", "node1.fbo", lambda: {})
    reg.register("cache", "node2.tex", lambda: {})
    n = reg.unregister_owner("node1.")
    assert n == 2
    remaining = reg.collect()["cache"]
    assert set(remaining) == {"node2.tex"}


def test_sampler_sample_once_has_system_stats():
    from src.shared.diagnostics.resource_snapshot import ResourceSnapshotSampler
    s = ResourceSnapshotSampler(interval_s=0.5)
    sample = s.sample_once()
    assert "ts" in sample and "system" in sample
    assert "thread_count" in sample["system"]
    assert "categories" in sample
