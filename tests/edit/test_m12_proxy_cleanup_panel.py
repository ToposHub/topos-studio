"""M12.2 · 代理清理面板回归（安全谓词映射/报告呈现/结果回填）。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.ui.proxy_cleanup_panel import ProxyCleanupPanel


class TestProxyCleanupPanel:
    def test_report_rendered(self, qtbot):
        panel = ProxyCleanupPanel(
            MagicMock(),
            report_fn=lambda: {"total": 12,
                               "statuses": {"ready": 10, "stale": 2},
                               "bytes": 3 * 1024 * 1024})
        qtbot.addWidget(panel)
        assert "12" in panel._usage_label.text()
        assert "ready: 10" in panel._usage_label.text()
        assert "3.0 MB" in panel._usage_label.text()

    def test_orphans_default_maps_to_safe_predicate(self, qtbot, monkeypatch):
        calls = {}

        def purge(**kwargs):
            calls.update(kwargs)
            return {"removed": 3, "skipped_active": 1,
                    "skipped_not_found": 0, "skipped_outside_dir": 0}

        panel = ProxyCleanupPanel(
            MagicMock(), report_fn=lambda: {"total": 0, "statuses": {},
                                            "bytes": 0},
            purge_fn=purge)
        qtbot.addWidget(panel)
        # 默认勾选孤儿模式
        assert panel._orphans_only.isChecked()
        monkeypatch.setattr(
            "src.features.edit.ui.proxy_cleanup_panel.QMessageBox.question",
            staticmethod(lambda *a, **k: QMessageBox_Yes()))
        monkeypatch.setattr(
            "src.features.edit.ui.proxy_cleanup_panel.QMessageBox.information",
            staticmethod(lambda *a, **k: None))
        panel._on_purge()
        # 安全谓词：only_if_source_missing=True（跨项目引用保护）
        assert calls["only_if_source_missing"] is True
        assert set(calls["statuses"]) == {"READY", "STALE", "FAILED"}
        assert panel.last_result["removed"] == 3

    def test_purge_failure_reported_not_crash(self, qtbot, monkeypatch):
        def broken(**kwargs):
            raise RuntimeError("disk error")

        panel = ProxyCleanupPanel(
            MagicMock(), report_fn=lambda: {}, purge_fn=broken)
        qtbot.addWidget(panel)
        monkeypatch.setattr(
            "src.features.edit.ui.proxy_cleanup_panel.QMessageBox.question",
            staticmethod(lambda *a, **k: QMessageBox_Yes()))
        monkeypatch.setattr(
            "src.features.edit.ui.proxy_cleanup_panel.QMessageBox.critical",
            staticmethod(lambda *a, **k: None))
        panel._on_purge()
        assert panel.last_result is None

    def test_no_service_entry_points_shown(self, qtbot):
        panel = ProxyCleanupPanel(MagicMock(spec=[]))  # 无方法
        qtbot.addWidget(panel)
        assert "不可用" in panel._usage_label.text()


class QMessageBox_Yes:
    """QMessageBox.StandardButton.Yes 替身（== Yes 恒真）。"""

    def __eq__(self, other):
        from PyQt6.QtWidgets import QMessageBox
        return other == QMessageBox.StandardButton.Yes


class TestCacheMigrationPlan:
    def test_active_frozen_and_invalid(self, tmp_path):
        from src.features.edit.core.source_patch import plan_cache_migration
        active = tmp_path / "in_use.proxy"
        active.write_bytes(b"px")
        normal = tmp_path / "normal.proxy"
        normal.write_bytes(b"px")
        result = plan_cache_migration(
            [(str(active), str(tmp_path / "new1")),
             (str(normal), str(tmp_path / "new2")),
             (str(tmp_path / "gone.proxy"), str(tmp_path / "new3")),
             ("", "x")],
            project_active_paths={str(active)},
        )
        assert result["migrate"] == [(str(normal), str(tmp_path / "new2"))]
        assert result["frozen_active"] == [str(active)]
        reasons = dict(result["invalid"])
        assert "源文件不存在" in reasons[str(tmp_path / "gone.proxy")]

    def test_duplicate_targets_rejected(self, tmp_path):
        from src.features.edit.core.source_patch import plan_cache_migration
        a = tmp_path / "a.proxy"; a.write_bytes(b"1")
        b = tmp_path / "b.proxy"; b.write_bytes(b"2")
        result = plan_cache_migration(
            [(str(a), str(tmp_path / "same")),
             (str(b), str(tmp_path / "same"))],
            project_active_paths=set())
        assert len(result["migrate"]) == 1
        assert len(result["invalid"]) == 1
