"""N09 · 自动备份生产心跳接线测试。

审计句：调度器丢弃 take_snapshot 返回的新工程仅计数（虚报成功）；
全 src 无生产实例化。修复后契约：tick 返回新工程，心跳写回在册工程
+ dirty + 广播后 confirm；写回失败不 confirm；同 revision 不重复；
MainWindow 初始化启动心跳（懒建）。
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import replace as _dc_replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.snapshot_backup_scheduler import (  # noqa: E402
    AUTO_BACKUP_NAME, SnapshotBackupScheduler)

pytestmark = pytest.mark.edit_core


def _project():
    from src.features.edit.core.project import EditProject
    return EditProject(project_id="n09-proj", name="N09")


class _FakeMediaPool:
    def __init__(self):
        self.set_projects = []
        self.emitted = []

    def set_project(self, project):
        self.set_projects.append(project)

    class _Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            self.slots and self.slots[0](*args)

    project_changed = None  # 类级占位（实例化时建）




def _bind_heartbeat(host):
    from src.app.controllers.edit_controller import EditControllerMixin
    for name, fn in inspect.getmembers(EditControllerMixin,
                                       predicate=inspect.isfunction):
        if name in ("_auto_backup_heartbeat", "_ensure_snapshot_backup"):
            setattr(host, name, fn.__get__(host))
    return host


def _make_host(project):
    """QObject 宿主替身（QTimer parent 需要 QObject）。"""
    from PyQt6.QtCore import QObject
    media_pool = _FakeMediaPool()
    media_pool.project_changed = _FakeMediaPool._Signal()

    class _Host(QObject):
        def __init__(self):
            super().__init__()
            self._current_edit_project = project
            self.edit_project_dirty = False
            self.media_pool_panel = media_pool

    return _bind_heartbeat(_Host())


def test_heartbeat_writes_back_and_confirms():
    host = _make_host(_project())
    host._ensure_snapshot_backup()
    scheduler = host._snapshot_backup_scheduler
    # 直接调心跳（绕过 QTimer 间隔）
    host._auto_backup_heartbeat(now=1000.0)
    # 快照落库：在册工程带 __auto_backup__ 且 dirty（保存重开仍在的口径）
    assert AUTO_BACKUP_NAME in host._current_edit_project.project_snapshots
    assert host.edit_project_dirty is True
    assert scheduler.backups_written == 1
    assert host.media_pool_panel.set_projects  # 已广播
    meta = scheduler.last_backup_meta
    assert meta and meta["name"] == AUTO_BACKUP_NAME
    assert meta["payload_hash"]


def test_heartbeat_same_revision_no_storm():
    host = _make_host(_project())
    host._ensure_snapshot_backup()
    host._auto_backup_heartbeat(now=1000.0)
    # 立即再跳：revision 未前进 → 不重复（无备份风暴）
    host._auto_backup_heartbeat(now=1000.0)
    assert host._snapshot_backup_scheduler.backups_written == 1
    assert len(host._current_edit_project.project_snapshots) == 1


def test_heartbeat_revision_advance_rebacks_up():
    host = _make_host(_project())
    host._ensure_snapshot_backup()
    host._auto_backup_heartbeat(now=1000.0)
    host._current_edit_project = _dc_replace(
        host._current_edit_project,
        revision=host._current_edit_project.revision + 1)
    host._auto_backup_heartbeat(now=2000.0)  # 过节拍间隔再跳
    assert host._snapshot_backup_scheduler.backups_written == 2
    # 单槽滚动：仍只有一个备份槽
    names = list(host._current_edit_project.project_snapshots)
    assert names == [AUTO_BACKUP_NAME]


def test_heartbeat_without_project_is_noop():
    host = _make_host(None)
    host._ensure_snapshot_backup()
    host._auto_backup_heartbeat(now=1000.0)  # 无工程：安全空转
    assert host._snapshot_backup_scheduler.backups_written == 0


def test_main_window_init_starts_heartbeat():
    """MainWindow.__init__ 含心跳启动调用（源级断言——真实窗口构造在
    N18 场景验收执行，此处锁住接线的存在性）。"""
    source = Path(
        "src/app/main_window.py").read_text(encoding="utf-8")
    assert "_ensure_snapshot_backup()" in source
    from src.app.controllers.edit_controller import EditControllerMixin
    assert hasattr(EditControllerMixin, "_auto_backup_heartbeat")


def test_scheduler_failure_records_error_no_crash():
    scheduler = SnapshotBackupScheduler(interval_seconds=60.0)

    class _BadProject:
        revision = 3

        class _Snap:
            def __getattr__(self, name):
                raise RuntimeError("payload boom")

        def __getattr__(self, name):
            if name == "project_snapshots":
                raise RuntimeError("boom")
            return NS()

    result = scheduler.tick(_BadProject(), now=100.0)
    assert result is None
    assert scheduler.last_error
    assert scheduler.backups_written == 0
