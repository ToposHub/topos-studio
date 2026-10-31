"""
录音 take 导入事务测试（RMA-11 阶段 A2）。

验收门槛（计划表 A2）：
- 一次 take 恰好一个 asset（去重 + 不重复调用导入）。
- 保存重开可见（import_asset 返回的新 EditProject 被回写到会话）。
- discard 不落文件（discard 是终止操作，后续 stop 不写文件、不回调完成）。

三个 P0/P1 bug 的回归：
1. _stop_recording_and_import 不再二次调用 _on_take_recorded（回调已是唯一事件源）。
2. _on_take_recorded 用 _current_edit_project（而非不存在的 edit_page._current_project），
   且把返回的新 project 回写（_apply_project_change）。
3. discard_current_take 终止录音 + 清缓冲 + 归档，_finalize_take 对 discarded 不落盘。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.recording_manager import (
    RecordingConfig, RecordingManager,
)


SR = 48000


def _sine(amp: float, freq: float, duration: float) -> np.ndarray:
    """单声道 PCM (1, n)。"""
    n = int(SR * duration)
    t = np.arange(n) / SR
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return sig.reshape(1, -1)


# ─────────────────────────────────────────────────────────────────────────────
# discard 不落盘（RecordingManager 层）
# ─────────────────────────────────────────────────────────────────────────────

class TestDiscardDoesNotPersist:
    """RMA-11: discard 后再 stop 不得落盘，也不触发完成回调。"""

    def test_discard_then_stop_writes_no_file(self, tmp_path):
        """discard 标记后即使再 stop，也不写出 WAV 文件。

        discard 是终止操作：_current_take 归零并归档为 discarded。后续 stop_recording
        此时已无活动 take（返回 None）。被丢弃的 take 在 _tasks 里，但文件不落盘。
        """
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.3))
        mgr.discard_current_take()
        # 被丢弃的 take 已归档
        assert len(mgr.takes) == 1
        discarded_take = mgr.takes[0]
        assert discarded_take.status == "discarded"
        file_path = discarded_take.file_path
        # 再 stop（模拟 R 按钮松开后的流程）：已无活动 take
        take = mgr.stop_recording()
        assert take is None
        # 关键：文件不存在（discard 没落盘，stop 也不补落盘）
        assert not Path(file_path).exists()

    def test_discard_does_not_trigger_completed_callback(self, tmp_path):
        """discard 的 take 不触发 on_take_completed 回调。"""
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        completed_calls = []
        mgr.on_take_completed = lambda take: completed_calls.append(take)
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.2))
        mgr.discard_current_take()
        mgr.stop_recording()
        assert completed_calls == []

    def test_discard_clears_buffer(self, tmp_path):
        """discard 清空 PCM 缓冲，防止后续误落盘。"""
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.2))
        assert mgr._pcm_buffer is not None
        mgr.discard_current_take()
        assert mgr._pcm_buffer is None
        assert mgr.state == "idle"


# ─────────────────────────────────────────────────────────────────────────────
# Controller 层：take 完成只有一个事件源（不重复导入）
# ─────────────────────────────────────────────────────────────────────────────

class _FakeMixerPanel:
    """模拟 media_pool_panel 的 set_project 接口。"""
    def __init__(self):
        self.set_project_calls = []
    def set_project(self, project):
        self.set_project_calls.append(project)


class _FakeTimelineView:
    def __init__(self):
        self.set_project_calls = []
        self.project_changed = MagicMock()
    def set_project(self, project):
        self.set_project_calls.append(project)


class TestRecordingImportTransaction:
    """RMA-11: 一次 take 恰好产生一次 asset 导入，且回写工程。"""

    def _make_controller_instance(self, project):
        """构造一个最小 EditControllerMixin 实例（仅含录音相关属性）。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        ctrl._current_edit_project = project
        ctrl.media_pool_panel = _FakeMixerPanel()
        tview = _FakeTimelineView()
        tpanel = MagicMock()
        tpanel.timeline_view = tview
        ctrl.timeline_panel = tpanel
        ctrl.timeline_preview_renderer = MagicMock()
        return ctrl

    def _make_project_with_assets_dir(self, tmp_path):
        from src.features.edit.core.project import EditProject
        project = EditProject.create(name="rec-test")
        return project

    def test_stop_does_not_double_import(self, tmp_path, qapp):
        """_stop_recording_and_import 不二次调用 _on_take_recorded。

        take 完成只有一个事件源：stop_recording 内部回调。显式二次调用会重复导入。
        """
        from src.app.controllers.edit_controller import EditControllerMixin

        project = self._make_project_with_assets_dir(tmp_path)
        ctrl = self._make_controller_instance(project)

        import_calls = []
        # 用一个真实的 take 文件
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        mgr.on_take_completed = lambda take: import_calls.append(take)
        ctrl._recording_managers = {"t1": mgr}

        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.2))

        # stop_recording_and_import 不再显式调 _on_take_recorded，
        # 而是由 stop_recording 的内部回调触发。
        # 监视 _on_take_recorded 调用次数
        spy = MagicMock(wraps=ctrl._on_take_recorded)
        ctrl._on_take_recorded = spy  # 覆盖回调（RecordingManager 已绑定原引用，需重绑）
        mgr.on_take_completed = lambda take: spy("t1", take)

        ctrl._stop_recording_and_import("t1")

        # 回调只被调用一次（而非旧实现的两次）
        assert spy.call_count == 1
        # 录音管理器已从缓存移除
        assert "t1" not in ctrl._recording_managers

    def test_on_take_recorded_uses_current_edit_project(self, tmp_path, qapp):
        """_on_take_recorded 用 self._current_edit_project（旧代码读不存在的属性）。"""
        project = self._make_project_with_assets_dir(tmp_path)
        ctrl = self._make_controller_instance(project)

        # 先录一个真实 take
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.2))
        take = mgr.stop_recording()
        assert Path(take.file_path).exists()

        ctrl._on_take_recorded("t1", take)

        # 工程被回写：_current_edit_project 已更新为新实例
        assert ctrl._current_edit_project is not project
        # media_pool_panel 与 timeline_view 都收到新工程
        assert len(ctrl.media_pool_panel.set_project_calls) == 1
        assert ctrl.media_pool_panel.set_project_calls[0] is ctrl._current_edit_project
        # 新工程含该 asset
        asset = ctrl._current_edit_project.get_asset_by_path(take.file_path)
        assert asset is not None
        assert asset.metadata.get("is_recording") is True

    def test_import_is_idempotent_on_duplicate(self, tmp_path, qapp):
        """同一 take 路径二次导入不产生重复 asset（import_asset 去重）。"""
        project = self._make_project_with_assets_dir(tmp_path)
        ctrl = self._make_controller_instance(project)

        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.2))
        take = mgr.stop_recording()

        ctrl._on_take_recorded("t1", take)
        first_project = ctrl._current_edit_project
        first_asset_count = len(first_project.assets)

        # 再次导入同一文件（模拟重复回调）
        ctrl._on_take_recorded("t1", take)
        # asset 数量不增加（去重）
        assert len(ctrl._current_edit_project.assets) == first_asset_count

    def test_apply_project_change_propagates_to_views(self, tmp_path, qapp):
        """_apply_project_change 同步 media_pool/timeline_view/renderer。"""
        project = self._make_project_with_assets_dir(tmp_path)
        ctrl = self._make_controller_instance(project)
        # 构造一个真正的新 project（修改 name 模拟 functional update）
        from dataclasses import replace as dc_replace
        new_project = dc_replace(project, name="updated")
        ctrl._apply_project_change(new_project)
        assert ctrl._current_edit_project is new_project
        assert ctrl.media_pool_panel.set_project_calls == [new_project]
        ctrl.timeline_preview_renderer.set_project.assert_called_once_with(new_project)
