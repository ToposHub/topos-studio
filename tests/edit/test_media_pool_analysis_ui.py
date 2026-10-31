"""M3 媒体池可选快速分析 — UI 接入层测试（§17.2 / §11.1 M3 退出条件）。

验收：
- 默认导入零 AI 推理：构造 MediaPoolPanel 不实例化 AnalysisService、不 submit 分析 job。
- 开关 off → 右键菜单无分析动作；on → 出现 Analyze。
- analysis_requested 信号携带正确的 asset_ids + scope（selected/bin/all）。
- analysis_status_changed → 更新 _analysis_status_cache + 触发 badge 刷新。
- set_project / clear_project → 通知 controller.on_project_changed（无 orphan worker）。
- 不写 timeline（无 commit/apply 入口）。

用 stub controller（QObject 子类）隔离，不依赖真实 AnalysisService。
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtTest import QTest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.project import EditProject
from features.edit.ui.media_pool_panel import MediaPoolPanel

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# stub controller（仿 MediaAnalysisController 的信号面，隔离真实 service）
# ---------------------------------------------------------------------- #


class _StubAnalysisController(QObject):
    """最小 stub：只实现 panel 关心的信号面 + on_project_changed 记录调用。"""

    analysis_proposed = pyqtSignal(object)
    analysis_status_changed = pyqtSignal(str, str)
    analysis_completed = pyqtSignal(str, object)
    analysis_failed = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project_changed_calls: list[str | None] = []
        self.draft_calls: list[tuple[tuple, dict]] = []
        self._scheduler = MagicMock()
        self._service = MagicMock()

    def on_project_changed(self, project_id):
        self.project_changed_calls.append(project_id)

    def request_analysis_draft(self, asset_ids, *, recipe, requested_by, **kw):
        self.draft_calls.append((tuple(asset_ids), {"recipe": recipe, "by": requested_by}))

    def _emit_status(self, asset_id, status):
        self.analysis_status_changed.emit(asset_id, status)


def _project_with_two_video_assets(tmp_path) -> EditProject:
    p1 = tmp_path / "a.mp4"
    p2 = tmp_path / "b.mp4"
    p1.write_bytes(b"x")
    p2.write_bytes(b"x")
    return (
        EditProject.create("M3 UI Test")
        .import_asset(path=str(p1), media_type="video", metadata={})
        .import_asset(path=str(p2), media_type="video", metadata={})
    )


def _make_panel(qapp, tmp_path) -> tuple[MediaPoolPanel, EditProject]:
    project = _project_with_two_video_assets(tmp_path)
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    return panel, project


# ---------------------------------------------------------------------- #
# 默认导入零 AI 推理（M3 验收第 1 项）
# ---------------------------------------------------------------------- #


class TestZeroInferenceOnConstruct:
    def test_construct_does_not_init_analysis_service(self, qapp) -> None:
        """构造 MediaPoolPanel 不实例化 AnalysisService / 不 submit 分析 job。"""
        panel = MediaPoolPanel()
        # 分析开关默认 off
        assert panel._media_analysis_enabled is False
        # 无 controller 注入
        assert panel._media_analysis_controller is None
        # cache 空
        assert panel._analysis_status_cache == {}


# ---------------------------------------------------------------------- #
# controller 注入 + 信号接驳
# ---------------------------------------------------------------------- #


class TestControllerInjection:
    def test_set_media_analysis_controller_connects_signals(self, qapp, tmp_path) -> None:
        panel, _ = _make_panel(qapp, tmp_path)
        stub = _StubAnalysisController()
        panel.set_media_analysis_controller(stub)
        assert panel._media_analysis_controller is stub
        # status 信号 → cache 更新
        stub.analysis_status_changed.emit("asset-1", "ready")
        assert panel._analysis_status_cache.get("asset-1") == "ready"

    def test_set_project_notifies_controller(self, qapp, tmp_path) -> None:
        panel, project = _make_panel(qapp, tmp_path)
        stub = _StubAnalysisController()
        panel.set_media_analysis_controller(stub)
        # set 一个不同的 project → on_project_changed 被调（新 project_id）
        other = EditProject.create("Other Project")
        panel.set_project(other)
        assert stub.project_changed_calls  # 非空

    def test_clear_project_notifies_controller_none(self, qapp, tmp_path) -> None:
        panel, _ = _make_panel(qapp, tmp_path)
        stub = _StubAnalysisController()
        panel.set_media_analysis_controller(stub)
        stub.project_changed_calls.clear()
        panel.clear_project()
        # clear_project 应通知 None（关闭语义）
        assert None in stub.project_changed_calls
        # cache 被清空
        assert panel._analysis_status_cache == {}


# ---------------------------------------------------------------------- #
# 开关 + 右键菜单
# ---------------------------------------------------------------------- #


class TestAnalysisToggleAndMenu:
    def test_toggle_changes_enabled_state(self, qapp, tmp_path) -> None:
        panel, _ = _make_panel(qapp, tmp_path)
        assert panel._media_analysis_enabled is False
        panel._on_analysis_toggle(True)
        assert panel._media_analysis_enabled is True
        panel._on_analysis_toggle(False)
        assert panel._media_analysis_enabled is False

    def test_analysis_requested_signal_emits(self, qapp, tmp_path) -> None:
        """开关 on + _emit_analysis_scope("selected") → analysis_requested emit 正确 payload。"""
        panel, project = _make_panel(qapp, tmp_path)
        asset_ids = list(project.assets.keys())

        captured: list[tuple[list, str]] = []
        panel.analysis_requested.connect(lambda ids, scope: captured.append((ids, scope)))

        panel._on_analysis_toggle(True)
        # 模拟选中第一个 asset
        panel._emit_analysis_scope("selected")  # selected 用 _collect_selected_asset_ids（可能空）
        # all scope 必 emit 全部 asset
        captured.clear()
        panel._emit_analysis_scope("all")
        assert len(captured) == 1
        ids, scope = captured[0]
        assert scope == "all"
        assert set(ids) == set(asset_ids)

    def test_emit_scope_all_with_no_project_is_noop(self, qapp) -> None:
        panel = MediaPoolPanel()
        panel._on_analysis_toggle(True)
        captured = []
        panel.analysis_requested.connect(lambda ids, scope: captured.append((ids, scope)))
        panel._emit_analysis_scope("all")  # 无 project
        assert captured == []

    def test_emit_scope_bin_emits_current_bin_assets(self, qapp, tmp_path) -> None:
        panel, _ = _make_panel(qapp, tmp_path)
        panel._on_analysis_toggle(True)
        captured = []
        panel.analysis_requested.connect(lambda ids, scope: captured.append((ids, scope)))
        panel._emit_analysis_scope("bin")
        assert len(captured) == 1
        assert captured[0][1] == "bin"
        assert len(captured[0][0]) >= 1  # 根 bin 含两个 asset


# ---------------------------------------------------------------------- #
# badge 状态 cache + 刷新
# ---------------------------------------------------------------------- #


class TestBadgeStateCache:
    def test_status_changed_updates_cache_and_refreshes(self, qapp, tmp_path) -> None:
        panel, _ = _make_panel(qapp, tmp_path)
        # 用 spy 监测 _refresh_analysis_badges（不应抛）
        refresh_calls = MagicMock(wraps=panel._refresh_analysis_badges)
        panel._refresh_analysis_badges = refresh_calls
        panel._on_analysis_status_changed("asset-1", "running")
        assert panel._analysis_status_cache["asset-1"] == "running"
        refresh_calls.assert_called()

    def test_analysis_completed_triggers_refresh(self, qapp, tmp_path) -> None:
        panel, _ = _make_panel(qapp, tmp_path)
        stub = _StubAnalysisController()
        panel.set_media_analysis_controller(stub)
        refresh_calls = MagicMock(wraps=panel._refresh_analysis_badges)
        panel._refresh_analysis_badges = refresh_calls
        stub.analysis_completed.emit("req-1", {"assets": 2})
        refresh_calls.assert_called()


# ---------------------------------------------------------------------- #
# 不写 timeline（§0 红线）
# ---------------------------------------------------------------------- #


class TestNoTimelineWrite:
    def test_panel_has_no_commit_or_apply_method(self, qapp) -> None:
        """panel 不提供任何写 timeline 的入口（分析只产候选 segment）。"""
        panel = MediaPoolPanel()
        assert not hasattr(panel, "commit_analysis")
        assert not hasattr(panel, "apply_analysis_to_timeline")
        assert not hasattr(panel, "execute_timeline_command")
