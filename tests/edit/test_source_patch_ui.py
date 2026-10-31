"""F2.1 · Source Patching UI 片——对话框常用配置 + 媒体池命令提交入口。

- SourcePatchDialog：直通/立体声 L+R/单声道任选/对调 预设回显与构造、
  自定义映射保留、混合值提示、双关警告、无音频素材降级。
- MediaPoolPanel.apply_source_patch：UpdateSourcePatchCommand 单步可撤销、
  project_changed 通知、未知素材/越界映射明确拒绝零写入。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_ui

from src.features.edit.core.source_patch import (
    DEFAULT_SOURCE_PATCH,
    SourcePatchSpec,
    patch_for_asset,
)
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.features.edit.ui.source_patch_dialog import SourcePatchDialog
from src.features.edit.core.project import EditProject, MediaAsset


class TestSourcePatchDialog:
    def _dialog(self, qapp, n_channels, current=None) -> SourcePatchDialog:
        return SourcePatchDialog(
            n_channels, current or DEFAULT_SOURCE_PATCH)

    def test_default_is_passthrough(self, qapp) -> None:
        dlg = self._dialog(qapp, 2)
        assert dlg._mode_combo.currentData() == "passthrough"
        assert dlg.spec().audio_channel_map is None

    def test_stereo_pair_option_for_multichannel(self, qapp) -> None:
        dlg = self._dialog(qapp, 6)
        idx = dlg._mode_combo.findData("stereo_pair")
        assert idx >= 0
        dlg._mode_combo.setCurrentIndex(idx)
        assert dlg.spec().audio_channel_map == ((0, 0), (1, 1))
        assert "2 声道" in dlg._preview_label.text()

    def test_mono_selection_roundtrip(self, qapp) -> None:
        """5.1 单选 C（源声道 2）回显为 mono 模式且重建同一映射。"""
        dlg = self._dialog(qapp, 6, SourcePatchSpec(audio_channel_map=((2, 0),)))
        assert dlg._mode_combo.currentData() == "mono"
        assert dlg._mono_combo.currentIndex() == 2
        assert dlg.spec().audio_channel_map == ((2, 0),)
        assert "源 C" in dlg._preview_label.text()

    def test_swap_option_for_stereo(self, qapp) -> None:
        dlg = self._dialog(qapp, 2)
        idx = dlg._mode_combo.findData("swap")
        assert idx >= 0
        dlg._mode_combo.setCurrentIndex(idx)
        assert dlg.spec().audio_channel_map == ((1, 0), (0, 1))

    def test_custom_map_preserved(self, qapp) -> None:
        """非预设映射（如 5.1 的 L+Ls）只改流开关不丢映射。"""
        custom = ((0, 0), (4, 1))
        dlg = self._dialog(qapp, 6, SourcePatchSpec(audio_channel_map=custom))
        assert dlg._mode_combo.currentData() == "custom"
        dlg._video_check.setChecked(False)
        spec = dlg.spec()
        assert spec.video_enabled is False
        assert spec.audio_channel_map == custom

    def test_stream_toggles_and_both_off_warning(self, qapp) -> None:
        dlg = self._dialog(qapp, 2)
        dlg._video_check.setChecked(False)
        dlg._audio_check.setChecked(False)
        spec = dlg.spec()
        assert spec.video_enabled is False and spec.audio_enabled is False
        assert "拒绝" in dlg._preview_label.text()

    def test_no_audio_source_degrades_gracefully(self, qapp) -> None:
        dlg = self._dialog(qapp, 0)
        assert not dlg._mode_combo.isEnabled()
        assert dlg.spec().audio_channel_map is None
        assert "无音频" in dlg._preview_label.text()


class TestMediaPoolApplySourcePatch:
    def _panel(self, qapp) -> tuple[MediaPoolPanel, EditProject]:
        asset = MediaAsset(
            "a51", "/tmp/x.wav", "audio", "x",
            metadata={"audio_info": {"channels": 6, "channel_layout": "5.1"}})
        project = EditProject("p", "P", assets={"a51": asset})
        panel = MediaPoolPanel()
        panel.set_project(project)
        return panel, project

    def test_apply_updates_overrides_and_emits(self, qapp) -> None:
        panel, _ = self._panel(qapp)
        emitted = []
        panel.project_changed.connect(lambda p: emitted.append(p))
        spec = SourcePatchSpec(audio_channel_map=((2, 0),))
        assert panel.apply_source_patch("a51", spec) is True
        assert patch_for_asset(
            panel._project.source_patch_overrides, "a51") == spec
        assert len(emitted) == 1

    def test_undo_restores_previous_state(self, qapp) -> None:
        panel, _ = self._panel(qapp)
        assert panel.apply_source_patch(
            "a51", SourcePatchSpec(audio_enabled=False)) is True
        undone = panel._command_stack.undo(panel._project)
        assert undone is not None
        panel._project = undone
        assert panel._project.source_patch_overrides == {}

    def test_unknown_asset_rejected(self, qapp) -> None:
        panel, _ = self._panel(qapp)
        before = dict(panel._project.source_patch_overrides)
        assert panel.apply_source_patch(
            "missing", SourcePatchSpec()) is False
        assert panel._project.source_patch_overrides == before

    def test_invalid_map_rejected_without_partial_write(
            self, qapp, monkeypatch) -> None:
        """源声道 6 越界（6 声道源）——弹窗反馈，零写入。"""
        panel, _ = self._panel(qapp)
        monkeypatch.setattr(
            "src.features.edit.ui.media_pool_panel.QMessageBox.warning",
            staticmethod(lambda *a, **k: None))
        before = dict(panel._project.source_patch_overrides)
        assert panel.apply_source_patch(
            "a51", SourcePatchSpec(audio_channel_map=((6, 0),))) is False
        assert panel._project.source_patch_overrides == before

    def test_clear_override_with_none(self, qapp) -> None:
        panel, _ = self._panel(qapp)
        assert panel.apply_source_patch(
            "a51", SourcePatchSpec(audio_enabled=False)) is True
        assert panel.apply_source_patch("a51", None) is True
        assert panel._project.source_patch_overrides == {}
