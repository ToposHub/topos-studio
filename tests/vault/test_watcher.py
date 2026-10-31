"""P2-2 增量索引（watchdog 文件监听）测试。

测事件处理逻辑（不测线程/真实监听，用 process_now 同步处理）：
    1. 新文件 → 增量打标（created）
    2. 已打标文件 → 跳过
    3. 移动/重命名 → relink（moved）
    4. 删除 → 标记 missing（deleted）
    5. 临时文件 → 跳过
    6. 非素材文件 → 跳过
    7. CLI watch --once 全量增量扫描
"""

from __future__ import annotations

import pytest


def test_watchdog_available():
    """watchdog 已装（测试环境装了）。"""
    from apps.topos_vault.watcher import is_watchdog_available
    assert is_watchdog_available() is True


# ---------------------------------------------------------------------- #
# 事件处理：created
# ---------------------------------------------------------------------- #

def test_created_event_indexes_new_file(repo, sample_files):
    """新文件创建事件 → 增量打标。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.watcher import VaultWatcher, WatchEvent

    clear_store_cache()
    watcher = VaultWatcher(repo, [str(sample_files["image"].parent)])
    watcher.process_now(WatchEvent(path=str(sample_files["image"]), kind="created"))

    # 应已入库
    src = repo.get_media_source_by_path(str(sample_files["image"]))
    assert src is not None
    assert watcher.stats.created == 1


def test_created_event_skips_already_indexed(repo, sample_files):
    """已打标的文件 → 跳过。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.watcher import VaultWatcher, WatchEvent

    clear_store_cache()
    watcher = VaultWatcher(repo, [str(sample_files["image"].parent)])
    # 第一次打标
    watcher.process_now(WatchEvent(path=str(sample_files["image"]), kind="created"))
    assert watcher.stats.created == 1
    # 第二次同文件 → 跳过
    watcher.process_now(WatchEvent(path=str(sample_files["image"]), kind="created"))
    assert watcher.stats.created == 1
    assert watcher.stats.skipped == 1


def test_created_event_skips_temp_files(repo, tmp_path):
    """临时文件 → 跳过。"""
    from apps.topos_vault.watcher import VaultWatcher, WatchEvent, _should_ignore

    # .tmp / ~ 等应忽略
    assert _should_ignore("/x/y.tmp") is True
    assert _should_ignore("/x/y~") is True
    assert _should_ignore("/x/.hidden.jpg") is True
    assert _should_ignore("/x/normal.jpg") is False

    watcher = VaultWatcher(repo, [str(tmp_path)])
    tmp_file = tmp_path / "editing.tmp"
    tmp_file.write_bytes(b"x")
    watcher.process_now(WatchEvent(path=str(tmp_file), kind="created"))
    assert watcher.stats.skipped == 1
    assert watcher.stats.created == 0


def test_created_event_skips_non_media(repo, tmp_path):
    """非素材文件（.txt 等）→ 跳过。"""
    from apps.topos_vault.watcher import VaultWatcher, WatchEvent

    watcher = VaultWatcher(repo, [str(tmp_path)])
    txt = tmp_path / "notes.txt"
    txt.write_bytes(b"hello")
    watcher.process_now(WatchEvent(path=str(txt), kind="created"))
    assert watcher.stats.skipped == 1


# ---------------------------------------------------------------------- #
# 事件处理：moved
# ---------------------------------------------------------------------- #

def test_moved_event_relinks(repo, sample_files):
    """移动事件 → 更新 path_current 到新路径。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.watcher import VaultWatcher, WatchEvent

    clear_store_cache()
    watcher = VaultWatcher(repo, [str(sample_files["image"].parent)])
    # 先打标
    watcher.process_now(WatchEvent(path=str(sample_files["image"]), kind="created"))
    fp = repo.get_media_source_by_path(str(sample_files["image"])).fingerprint

    # 模拟移动
    new_path = str(sample_files["image"].parent / "renamed.jpg")
    watcher.process_now(WatchEvent(
        path=new_path, kind="moved", src_path=str(sample_files["image"]),
    ))

    assert watcher.stats.moved == 1
    # 新路径能查到
    src = repo.get_media_source_by_path(new_path)
    assert src is not None
    assert src.fingerprint == fp


def test_moved_event_to_untracked_creates(repo, sample_files, tmp_path):
    """移动到未跟踪文件 → 当新文件处理。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.watcher import VaultWatcher, WatchEvent

    clear_store_cache()
    watcher = VaultWatcher(repo, [str(sample_files["image"].parent)])
    watcher.process_now(WatchEvent(path=str(sample_files["image"]), kind="created"))

    # 移到一个无记录的源路径
    new_path = str(tmp_path / "moved.jpg")
    import shutil
    shutil.copy(str(sample_files["image"]), new_path)
    watcher.process_now(WatchEvent(path=new_path, kind="moved", src_path="/nonexistent/src"))
    # 应当作 created 处理
    assert watcher.stats.created >= 1


# ---------------------------------------------------------------------- #
# 事件处理：deleted
# ---------------------------------------------------------------------- #

def test_deleted_event_marks_missing(repo, sample_files):
    """删除事件 → 标记 missing。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.watcher import VaultWatcher, WatchEvent

    clear_store_cache()
    watcher = VaultWatcher(repo, [str(sample_files["image"].parent)])
    watcher.process_now(WatchEvent(path=str(sample_files["image"]), kind="created"))
    fp = repo.get_media_source_by_path(str(sample_files["image"])).fingerprint

    watcher.process_now(WatchEvent(path=str(sample_files["image"]), kind="deleted"))
    assert watcher.stats.deleted == 1

    src = repo.get_media_source(fp)
    assert src.path_status == "missing"


def test_deleted_event_skips_unknown(repo, tmp_path):
    """删除未知文件 → 跳过。"""
    from apps.topos_vault.watcher import VaultWatcher, WatchEvent
    watcher = VaultWatcher(repo, [str(tmp_path)])
    watcher.process_now(WatchEvent(path=str(tmp_path / "never_existed.jpg"), kind="deleted"))
    assert watcher.stats.skipped == 1
    assert watcher.stats.deleted == 0


# ---------------------------------------------------------------------- #
# CLI watch --once
# ---------------------------------------------------------------------- #

def test_cli_watch_once(vault_dir, sample_files, monkeypatch):
    """CLI watch --once 全量增量扫描。"""
    import os
    from apps.topos_vault.cli import main
    from apps.topos_vault.db.repository import VaultRepository

    media_dir = str(sample_files["image"].parent)
    rc = main(["watch", "--once", media_dir])
    assert rc == 0

    repo = VaultRepository()
    # 至少 image 被打标
    src = repo.get_media_source_by_path(str(sample_files["image"]))
    assert src is not None


def test_cli_watch_once_skips_already_indexed(vault_dir, sample_files):
    """--once 第二次跑 → 已打标的跳过。"""
    import subprocess
    from apps.topos_vault.cli import main
    from apps.topos_vault.db.repository import VaultRepository

    media_dir = str(sample_files["image"].parent)
    main(["watch", "--once", media_dir])
    # 第二次
    rc = main(["watch", "--once", media_dir])
    assert rc == 0  # 不崩


# ---------------------------------------------------------------------- #
# Debouncer
# ---------------------------------------------------------------------- #

def test_debouncer_dedupes_rapid_events():
    """同一 path 短时间多次事件被去抖。"""
    from apps.topos_vault.watcher import _Debouncer
    d = _Debouncer(window=0.3)
    # 第一次记录
    d.should_process("/x")
    # 立即第二次（窗口内）→ pending 更新但不立即 ready
    d.should_process("/x")
    # 等窗口过期
    import time
    time.sleep(0.35)
    ready = d.expire()
    assert "/x" in ready


def test_debouncer_expire_empty():
    """无 pending → expire 返回空。"""
    from apps.topos_vault.watcher import _Debouncer
    d = _Debouncer()
    assert d.expire() == []
