"""EF-03 子运行 1/2 — src/features/edit/ui Suggestion 级修复测试（part 1）。

覆盖 AUD-19403/19407/19412/19511/19513/19620/19700/19730/19804/19807/
20011/20209/20210/20213/20301/20316/20405/20406/20411/20502。

约定：
- 不创建真实 GL 上下文（program_viewer 只做源级断言 + 纯函数行为测试）。
- 不弹模态对话框（不触发 exec 相关路径）。
- 每条修复都有正例（修复行为成立）+ 反例（回退修复后必然失败）。
"""

from __future__ import annotations

import inspect
import time
from types import SimpleNamespace
from typing import Any, List

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QPushButton, QTreeWidgetItem, QTreeWidget


# ────────────────────────────────────────────────────────────────────────
# AUD-19403: ADR 设备枚举不再在构造期同步执行
# ────────────────────────────────────────────────────────────────────────

class TestAud19403DeferredDeviceEnumeration:
    def _patch_enumerator(self, monkeypatch, devices):
        """RecordingManager.list_input_devices 是 reload_devices 内局部导入，
        必须在源模块上打补丁。"""
        from src.features.edit.core import recording_manager as rm_mod

        calls: List[int] = []

        def fake_list_input_devices():
            calls.append(1)
            return devices

        monkeypatch.setattr(
            rm_mod.RecordingManager, "list_input_devices",
            staticmethod(fake_list_input_devices),
        )
        return calls

    def test_constructor_does_not_enumerate_devices(self, qapp, monkeypatch) -> None:
        """正例: 构造函数零设备枚举；事件循环跑起来后延迟补扫恰好一次。"""
        from src.features.edit.ui import adr_take_sheet_panel as mod

        calls = self._patch_enumerator(
            monkeypatch, [("dev1", "Fake Mic")])
        panel = mod.AdrTakeSheetPanel()
        try:
            # 修复点：构造期不同步枚举
            assert calls == []
            # 延迟补扫（QTimer.singleShot(0)）在事件循环后触发
            qapp.processEvents()
            assert len(calls) == 1
            assert panel._device_combo.count() == 2  # System Default + dev1
        finally:
            panel.deleteLater()

    def test_reverted_constructor_enumerates_immediately(self, qapp, monkeypatch) -> None:
        """反例护栏: 若枚举回到构造期同步执行，此处立即失败。"""
        from src.features.edit.ui import adr_take_sheet_panel as mod

        calls = self._patch_enumerator(monkeypatch, [])
        panel = mod.AdrTakeSheetPanel()
        try:
            assert calls == [], (
                "AUD-19403 回退检测：设备枚举又在面板构造期同步执行了"
            )
        finally:
            panel.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-19407: 会话菜单打开不再无条件同步重扫插件目录
# ────────────────────────────────────────────────────────────────────────

class TestAud19407PluginRescanGate:
    def _make_composer(self, qapp):
        from src.features.edit.ui.ai_assistant_composer import AiAssistantComposer
        return AiAssistantComposer()

    def test_signature_gate_skips_rescan(self, qapp, monkeypatch) -> None:
        """正例: 目录签名不变 → 不重扫；签名变化/首次 → 重扫。"""
        from src.features.edit.ui import ai_assistant_composer as mod

        reload_calls: List[int] = []
        monkeypatch.setattr(
            mod, "reload_assistant_plugins",
            lambda *a, **k: reload_calls.append(1) or (),
        )
        composer = self._make_composer(qapp)
        try:
            assert composer._plugins_dir_signature is None

            sig = ("sig-a",)
            monkeypatch.setattr(
                composer, "_plugins_directory_signature",
                lambda: sig, raising=False,
            )
            composer._refresh_plugins_if_changed()
            assert len(reload_calls) == 1  # 首次（签名未知）必须扫
            assert composer._plugins_dir_signature == sig

            composer._refresh_plugins_if_changed()
            assert len(reload_calls) == 1  # 签名未变 → 跳过重扫（修复核心）

            monkeypatch.setattr(
                composer, "_plugins_directory_signature",
                lambda: ("sig-b",), raising=False,
            )
            composer._refresh_plugins_if_changed()
            assert len(reload_calls) == 2  # 签名变化 → 重扫
        finally:
            composer.deleteLater()

    def test_reverted_gate_always_rescans(self, qapp, monkeypatch) -> None:
        """反例护栏: 移除签名门控后连续两次菜单打开会重扫两次。"""
        from src.features.edit.ui import ai_assistant_composer as mod

        reload_calls: List[int] = []
        monkeypatch.setattr(
            mod, "reload_assistant_plugins",
            lambda *a, **k: reload_calls.append(1) or (),
        )
        composer = self._make_composer(qapp)
        try:
            sig = ("sig-a",)
            monkeypatch.setattr(
                composer, "_plugins_directory_signature",
                lambda: sig, raising=False,
            )
            composer._refresh_plugins_if_changed()
            composer._refresh_plugins_if_changed()
            assert len(reload_calls) == 1, (
                "AUD-19407 回退检测：签名未变时仍然每次同步重扫插件目录"
            )
        finally:
            composer.deleteLater()

    def test_signature_reflects_directory_changes(self, qapp, monkeypatch, tmp_path) -> None:
        """插件目录增删文件夹 → 签名变化；不可读目录 → None（保守重扫）。"""
        from src.features.edit.ui.ai_assistant_composer import (
            AiAssistantComposer,
        )

        monkeypatch.setenv("TOPOS_AI_ASSISTANT_PLUGIN_DIR", str(tmp_path))
        composer = AiAssistantComposer()
        try:
            empty_sig = AiAssistantComposer._plugins_directory_signature()
            (tmp_path / "my_plugin").mkdir()
            one_sig = AiAssistantComposer._plugins_directory_signature()
            assert empty_sig != one_sig
            assert empty_sig is not None and one_sig is not None

            monkeypatch.setenv(
                "TOPOS_AI_ASSISTANT_PLUGIN_DIR", str(tmp_path / "missing"))
            assert AiAssistantComposer._plugins_directory_signature() is None
        finally:
            composer.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-19412: 锁定/拒绝按钮按 decision 状态门控
# ────────────────────────────────────────────────────────────────────────

class TestAud19412DecisionButtonGating:
    @pytest.fixture(autouse=True)
    def _reset_i18n_translations(self):
        """顺序污染隔离：本测试用按钮中文文本（"锁定"/"拒绝"/"替换"）做键，
        而按钮文本来自 tr(key, 中文默认)。此前文件的 locale 测试经
        load_locale() 后无法回到"空词典"状态，en_US 词典会永久驻留
        I18N 类属性，使 tr('ai_decision.lock', '锁定') 解析为 'Lock'，
        按钮文本变英文导致 KeyError。进入前清空词典（tr 回落中文
        default），退出时恢复进入前的原状，不向进程注入新状态。"""
        from src.shared.i18n.i18n import I18N

        saved = (
            I18N._translations,
            I18N._fallback_translations,
            I18N._current_locale,
            list(I18N._module_locales_dirs),
        )
        I18N._translations = {}
        I18N._fallback_translations = {}
        I18N._module_locales_dirs = []
        try:
            yield
        finally:
            (
                I18N._translations,
                I18N._fallback_translations,
                I18N._current_locale,
                I18N._module_locales_dirs,
            ) = saved

    def _buttons_by_text(self, row_widget):
        return {
            btn.text(): btn for btn in row_widget.findChildren(QPushButton)
        }

    def test_buttons_gated_by_state(self, qapp) -> None:
        """正例: locked → 锁定禁用；rejected → 拒绝禁用；proposed → 全部可用。"""
        from src.features.edit.ai_editing.assistant_events import DecisionCard
        from src.features.edit.ui.ai_decision_inspector import AiDecisionInspector

        inspector = AiDecisionInspector()
        try:
            inspector.set_decisions((
                DecisionCard(decision_id="d1", state="locked"),
                DecisionCard(decision_id="d2", state="rejected"),
                DecisionCard(decision_id="d3", state="proposed"),
            ))
            qapp.processEvents()
            rows = []
            for i in range(inspector._container_layout.count()):
                w = inspector._container_layout.itemAt(i).widget()
                if w is not None:
                    rows.append(w)
            assert len(rows) == 3

            by_state = {"d1": rows[0], "d2": rows[1], "d3": rows[2]}
            lock_txt = "锁定"
            reject_txt = "拒绝"
            for decision_id, row in by_state.items():
                btns = self._buttons_by_text(row)
                if decision_id == "d1":
                    assert btns[lock_txt].isEnabled() is False
                    assert btns[reject_txt].isEnabled() is True
                elif decision_id == "d2":
                    assert btns[reject_txt].isEnabled() is False
                    assert btns[lock_txt].isEnabled() is True
                else:
                    assert btns[lock_txt].isEnabled() is True
                    assert btns[reject_txt].isEnabled() is True
                # 替换在所有状态下保留
                assert btns["替换"].isEnabled() is True
        finally:
            inspector.deleteLater()

    def test_reverted_buttons_always_enabled(self, qapp) -> None:
        """反例护栏: 回退门控后 locked 行的锁定按钮重新变为可用。"""
        from src.features.edit.ai_editing.assistant_events import DecisionCard
        from src.features.edit.ui.ai_decision_inspector import AiDecisionInspector

        inspector = AiDecisionInspector()
        try:
            inspector.set_decisions((
                DecisionCard(decision_id="d1", state="locked"),
            ))
            qapp.processEvents()
            for i in range(inspector._container_layout.count()):
                w = inspector._container_layout.itemAt(i).widget()
                if w is None:
                    continue
                btns = {b.text(): b for b in w.findChildren(QPushButton)}
                assert btns["锁定"].isEnabled() is False, (
                    "AUD-19412 回退检测：已锁定 decision 的锁定按钮未按状态禁用"
                )
        finally:
            inspector.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-19511: 图片附件解码/编码移到后台线程
# ────────────────────────────────────────────────────────────────────────

def _wait_for(predicate, qapp, timeout_s: float = 5.0) -> bool:
    """驱动事件循环直到 predicate 成立或超时。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestAud19511AsyncImageAttachment:
    def _make_panel(self, qapp):
        from src.features.edit.ui.ai_edit_assistant_panel import (
            AiEditAssistantPanel,
        )
        return AiEditAssistantPanel()

    def test_add_image_attachment_is_async(self, qapp) -> None:
        """正例: 编码不再同步完成 —— 先受理、后台完成后回填附件区。"""
        panel = self._make_panel(qapp)
        try:
            image = QImage(16, 16, QImage.Format.Format_RGB32)
            image.fill(QColor(200, 30, 40))
            assert panel.add_image_attachment(image, source="paste") is True
            # 修复核心：同步返回时附件尚未编码完成
            assert len(panel._pending_attachments) == 0
            ok = _wait_for(
                lambda: len(panel._pending_attachments) == 1, qapp)
            assert ok, "后台编码未在超时内完成"
            att = panel._pending_attachments[0]
            assert att.source == "paste"
            assert att.data_url.startswith("data:image/jpeg")
        finally:
            panel.deleteLater()

    def test_null_image_rejected_synchronously(self, qapp) -> None:
        panel = self._make_panel(qapp)
        try:
            assert panel.add_image_attachment(QImage(), source="paste") is False
            qapp.processEvents()
            assert panel._pending_attachments == []
        finally:
            panel.deleteLater()

    def test_file_attachment_decodes_off_thread(self, qapp, tmp_path) -> None:
        """正例: 文件路径图片的整图解码移入 worker（受理同步、解码异步）。"""
        panel = self._make_panel(qapp)
        try:
            path = tmp_path / "tiny.png"
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(QColor(10, 120, 240))
            assert image.save(str(path), "PNG")

            assert panel._add_file_attachment(str(path), source="file") is True
            assert len(panel._pending_attachments) == 0  # 未同步解码
            ok = _wait_for(
                lambda: len(panel._pending_attachments) == 1, qapp)
            assert ok, "后台文件解码/编码未在超时内完成"
        finally:
            panel.deleteLater()

    def test_reverted_encode_is_synchronous(self, qapp) -> None:
        """反例护栏: 回退为同步编码后，返回时附件区立即非空。"""
        panel = self._make_panel(qapp)
        try:
            image = QImage(16, 16, QImage.Format.Format_RGB32)
            image.fill(QColor(200, 30, 40))
            panel.add_image_attachment(image, source="paste")
            assert len(panel._pending_attachments) == 0, (
                "AUD-19511 回退检测：JPEG 编码又在 UI 线程同步完成了"
            )
        finally:
            panel.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-19513: 无 Run Drawer 时 shimmer 定时器不再空转
# ────────────────────────────────────────────────────────────────────────

class TestAud19513ShimmerTimerGating:
    def _make_panel(self, qapp):
        from src.features.edit.ui.ai_edit_assistant_panel import (
            AiEditAssistantPanel,
        )
        return AiEditAssistantPanel()

    def test_no_drawer_no_timer(self, qapp) -> None:
        """正例: 无 drawer / drawer 无 live 接口 → shimmer 定时器不启动。"""
        panel = self._make_panel(qapp)
        try:
            panel._run_drawer = None
            panel.set_activity_status("working", active=True)
            assert panel._activity_status_active is True
            assert panel._activity_shimmer_timer.isActive() is False

            # drawer 存在但无 set_live_status → 同样不启动
            panel._run_drawer = SimpleNamespace()
            panel.set_activity_status("working", active=True)
            assert panel._activity_shimmer_timer.isActive() is False
        finally:
            panel.deleteLater()

    def test_drawer_with_live_interface_starts_timer(self, qapp) -> None:
        """有 live 接口的 drawer → 正常启动（不破坏既有动画）。"""
        panel = self._make_panel(qapp)
        try:
            panel._run_drawer = SimpleNamespace(
                set_live_status=lambda *a, **k: None)
            panel.set_activity_status("working", active=True)
            assert panel._activity_shimmer_timer.isActive() is True
            panel.set_activity_status("done", active=False)
            assert panel._activity_shimmer_timer.isActive() is False
        finally:
            panel.deleteLater()

    def test_reverted_timer_starts_without_target(self, qapp) -> None:
        """反例护栏: 回退后无动画目标时定时器仍被启动。"""
        panel = self._make_panel(qapp)
        try:
            panel._run_drawer = None
            panel.set_activity_status("working", active=True)
            assert panel._activity_shimmer_timer.isActive() is False, (
                "AUD-19513 回退检测：无 Run Drawer 时 shimmer 定时器又在空转"
            )
        finally:
            panel.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-19620: Timeline Diff 文档与置顶行为一致
# ────────────────────────────────────────────────────────────────────────

class TestAud19620TimelineDiffDocAccuracy:
    def test_docstring_matches_behavior(self) -> None:
        """正例: 模块文档描述 removed + warnings 的真实置顶构成。"""
        import src.features.edit.ui.ai_timeline_diff as mod

        doc = mod.__doc__ or ""
        # 旧文案声称高风险置顶覆盖独立的“替换/音乐版权/降级 effect”分类
        assert "delete、替换、音乐版权、降级 effect" not in doc, (
            "AUD-19620 回退检测：文档又声称未实现的置顶分类"
        )
        assert "removed" in doc
        assert "warning" in doc.lower()

    def test_warnings_and_removed_pinned_in_order(self, qapp) -> None:
        """行为正例: warnings（音乐版权/降级类）最前，removed 次之。"""
        from PyQt6.QtWidgets import QLabel

        from src.features.edit.ui.ai_timeline_diff import AiTimelineDiffPanel

        panel = AiTimelineDiffPanel()
        try:
            panel.set_diff({
                "added": [{"change": "added", "clip_id": "c1", "track_id": "v1"}],
                "removed": [{"change": "removed", "clip_id": "c0", "track_id": "v1"}],
                "moved": [],
                "trimmed": [],
                "warnings": ["音乐版权提醒", "effect 已降级"],
            })
            widgets = []
            for i in range(panel._container_layout.count()):
                w = panel._container_layout.itemAt(i).widget()
                if w is not None:
                    widgets.append(w)
            assert len(widgets) == 4  # 2 warnings + removed + added
            # 前两条为 warning 条目（带 ⚠ 前缀的高风险样式）
            for w in widgets[:2]:
                labels = w.findChildren(QLabel)
                assert any("⚠" in lbl.text() for lbl in labels)
            # removed 是第三条（高风险 ⚠ 前缀），added 最后（无 ⚠）
            assert "⚠" in widgets[2].findChildren(QLabel)[0].text()
            assert "⚠" not in widgets[3].findChildren(QLabel)[0].text()
        finally:
            panel.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-19700: Find 命中后 cue_selected 不再被防递归锁抑制
# ────────────────────────────────────────────────────────────────────────

class TestAud19700FindEmitsCueSelected:
    def _make_panel(self, qapp):
        from src.features.edit.core.captions.model import CaptionCueMetadata
        from src.features.edit.core.timeline_v2 import (
            EditTrack, SubtitleClip, TimelineSequence,
        )
        from src.features.edit.ui.caption_workspace import CaptionWorkspacePanel

        def clip(cid: str, text: str, start: int) -> SubtitleClip:
            return SubtitleClip(
                clip_id=cid, track_id="t1",
                timeline_start=start, timeline_duration=10, text=text,
                caption_metadata=CaptionCueMetadata(),
            )

        track = EditTrack(
            track_id="t1", track_type="subtitle", index=0,
            subtitle_clips=(clip("c1", "hello world", 0),
                            clip("c2", "other", 20)),
        )
        seq = TimelineSequence(
            timeline_id="s1", name="main", subtitle_tracks=(track,))
        panel = CaptionWorkspacePanel()
        panel.set_sequence(seq, 24.0)
        return panel

    def test_find_selects_and_emits(self, qapp) -> None:
        """正例: Find 命中 → 选中该行且 cue_selected 发射（时间线跳转）。"""
        panel = self._make_panel(qapp)
        try:
            emitted: List[tuple] = []
            panel.cue_selected.connect(lambda cid, tid: emitted.append((cid, tid)))

            panel._find_edit.setText("world")
            panel._on_find()

            assert emitted, "Find 命中后 cue_selected 未发射"
            assert emitted[0][0] == "c1"
            # 状态栏有命中数
            assert "1 match" in panel._status_label.text()
        finally:
            panel.deleteLater()

    def test_controller_driven_select_stays_silent(self, qapp) -> None:
        """边界正例: 控制器驱动的 select_clip 仍被锁抑制（不产生回环）。"""
        panel = self._make_panel(qapp)
        try:
            emitted: List[tuple] = []
            panel.cue_selected.connect(lambda cid, tid: emitted.append((cid, tid)))
            panel.select_clip("c2")
            assert emitted == []
        finally:
            panel.deleteLater()

    def test_reverted_find_does_not_emit(self, qapp) -> None:
        """反例护栏: 回退后 Find 命中不再发射 cue_selected。"""
        panel = self._make_panel(qapp)
        try:
            emitted: List[tuple] = []
            panel.cue_selected.connect(lambda cid, tid: emitted.append((cid, tid)))
            panel._find_edit.setText("world")
            panel._on_find()
            assert emitted, (
                "AUD-19700 回退检测：Find 命中后 cue_selected 又被 _building_ui 抑制"
            )
        finally:
            panel.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-19730: Transform 手柄控制器文档与像素空间模型一致
# ────────────────────────────────────────────────────────────────────────

class TestAud19730TransformControllerDocstring:
    def test_docstring_describes_pixel_space(self) -> None:
        import src.features.edit.ui.clip_transform_handle_controller as mod

        doc = mod.__doc__ or ""
        assert "pixel-space" in doc or "像素空间" in doc
        assert "normalized ``TransformParams``" not in doc, (
            "AUD-19730 回退检测：文档又声称 clip 模型是 normalized 空间"
        )

    def test_params_dict_is_direct_copy(self) -> None:
        """行为正例: _params_dict 不做归一化↔像素桥接（字段直传）。"""
        from src.features.edit.core.clip_params import TransformParams
        from src.features.edit.ui.clip_transform_handle_controller import (
            ClipTransformHandleController,
        )

        t = TransformParams(
            translate_x=100.0, translate_y=50.0, scale_x=2.0, scale_y=1.5,
            rotation=15.0, pivot_mode="custom", pivot_x=10.0, pivot_y=20.0,
        )
        controller = ClipTransformHandleController(viewer=None)  # type: ignore[arg-type]
        d = controller._params_dict(t)
        # 1920 画布下若有归一化桥接 translate 会被乘成 192000 —— 直传保持 100
        assert d["translate_x"] == 100.0
        assert d["translate_y"] == 50.0
        assert d["pivot_x"] == 10.0

        delta = controller._delta_to_transform(
            {"translate_x": 120.0, "rotation": 30.0}, t)
        assert delta.translate_x == 120.0
        assert delta.rotation == 30.0
        assert delta.scale_x == 2.0  # 未指定的键保留 base 值


# ────────────────────────────────────────────────────────────────────────
# AUD-19804: search_input 程序化清空持 _updating 状态锁
# ────────────────────────────────────────────────────────────────────────

class TestAud19804SearchClearStateLock:
    def _make_panel(self, qapp):
        from src.features.edit.ui.edit_effects_panel import EditEffectsPanel
        return EditEffectsPanel()

    def test_on_search_changed_guarded_by_lock(self, qapp) -> None:
        """正例: _updating 期间 textChanged 不再触发重建调度。"""
        panel = self._make_panel(qapp)
        try:
            panel._updating = True
            panel._on_search_changed("x")
            assert panel._search_debounce_timer.isActive() is False

            panel._updating = False
            panel._on_search_changed("x")
            assert panel._search_debounce_timer.isActive() is True
        finally:
            panel.deleteLater()

    def test_clear_rebuilds_exactly_once(self, qapp) -> None:
        """正例: clear() 只做一次全量重建（程序化清空不重入）。"""
        panel = self._make_panel(qapp)
        try:
            rebuilds: List[int] = []
            original = panel._populate_video_effects

            def counting(tree):
                rebuilds.append(1)
                original(tree)

            panel.search_input.setText("blur")
            qapp.processEvents()
            panel._populate_video_effects = counting  # type: ignore[method-assign]
            panel._search_debounce_timer.stop()
            panel.clear()
            qapp.processEvents()
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.02)
            assert len(rebuilds) == 1, (
                f"AUD-19804 回退检测：clear() 触发了 {len(rebuilds)} 次全量重建"
            )
        finally:
            panel.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-19807: CaptionStylePreset 注解的名字已可解析
# ────────────────────────────────────────────────────────────────────────

class TestAud19807CaptionStylePresetImport:
    def test_annotation_name_is_imported(self, qapp) -> None:
        """正例: _populate_titles 源内局部导入了 CaptionStylePreset。"""
        from src.features.edit.ui.edit_effects_panel import EditEffectsPanel

        src = inspect.getsource(EditEffectsPanel._populate_titles)
        assert "CaptionStylePreset" in src
        assert (
            "from src.features.edit.core.captions.style_preset import" in src
        ), "AUD-19807 回退检测：CaptionStylePreset 的局部导入被移除"

    def test_populate_titles_runs(self, qapp) -> None:
        """行为正例: _populate_titles 正常执行无 NameError。"""
        from src.features.edit.ui.edit_effects_panel import EditEffectsPanel

        panel = EditEffectsPanel()
        try:
            panel._populate_titles(panel.video_effects_tree)
        finally:
            panel.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-20011: 效果参数控件回调带 _updating 防递归守卫
# ────────────────────────────────────────────────────────────────────────

class TestAud20011EffectParamCallbackGuards:
    def _make_row(self, qapp):
        from src.features.composite.nodes.base_node import ParamDef
        from src.features.edit.ui import edit_properties_panel as mod
        from src.features.edit.ui.edit_properties_panel import (
            EditPropertiesPanel,
        )
        from src.shared.ui.widgets.topos_controls import ToposClickableSlider

        panel = EditPropertiesPanel()
        panel._current_clips = [SimpleNamespace(clip_id="c1")]
        pdef = ParamDef(
            name="test_param", ptype="float",
            min_val=0.0, max_val=10.0, step=0.1, decimals=2,
        )
        row = mod._create_effect_param_widget_from_pdef(
            panel, pdef, 1.0, "eff1")
        sliders = [s for s in row.findChildren(ToposClickableSlider)]
        assert sliders, "未找到滑块控件"
        # panel 接管 row 的所有权，防止测试期间被 GC 连带销毁控件
        row.setParent(panel)
        return panel, sliders[0]

    def test_slider_callback_suppressed_during_update(self, qapp) -> None:
        """正例: _updating=True 时滑块回调不发射 effect_param_changed。"""
        panel, slider = self._make_row(qapp)
        try:
            emissions: List[tuple] = []
            panel.effect_param_changed.connect(
                lambda cid, eid, pn, v: emissions.append((cid, eid, pn, v)))

            panel._updating = True
            slider.setValue(50)
            qapp.processEvents()
            assert emissions == [], "程序化更新期间回调仍发射（守卫缺失）"

            panel._updating = False
            slider.setValue(60)
            qapp.processEvents()
            assert emissions, "用户交互路径被误伤（守卫过宽）"
            assert emissions[-1][0] == "c1"
            assert emissions[-1][2] == "test_param"
        finally:
            panel.deleteLater()

    def test_reverted_callback_emits_during_update(self, qapp) -> None:
        """反例护栏: 移除守卫后 _updating=True 时回调仍然发射。"""
        panel, slider = self._make_row(qapp)
        try:
            emissions: List[tuple] = []
            panel.effect_param_changed.connect(
                lambda cid, eid, pn, v: emissions.append((cid, eid, pn, v)))
            panel._updating = True
            slider.setValue(50)
            qapp.processEvents()
            assert emissions == [], (
                "AUD-20011 回退检测：程序化更新期间效果参数回调未被守卫抑制"
            )
        finally:
            panel.deleteLater()

    def test_all_effect_callback_sites_have_guards(self, qapp) -> None:
        """静态正例: 四处回调定义（两组 builder）都含 _updating 守卫。"""
        import src.features.edit.ui.edit_properties_panel as mod

        src = inspect.getsource(mod)
        for marker in ("_slider_changed", "_drag_changed",
                       "_eff_slider_changed", "_eff_drag_changed"):
            idx = src.find(f"def {marker}")
            assert idx >= 0, f"缺少回调 {marker}"
            body = src[idx: idx + 900]
            assert "self._updating" in body, (
                f"AUD-20011 回退检测：{marker} 缺少 _updating 守卫"
            )


# ────────────────────────────────────────────────────────────────────────
# AUD-20209: 缩略图到达不再全树扫描（path → items 索引）
# ────────────────────────────────────────────────────────────────────────

class TestAud20209ThumbnailIndex:
    def _make_panel_skeleton(self, qapp, assets: dict):
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel

        panel = MediaPoolPanel.__new__(MediaPoolPanel)
        panel.tree = QTreeWidget()
        panel._project = SimpleNamespace(assets=assets)
        panel._updating = False
        panel.asset_model = None
        panel._pending_timeline_image_previews = {}
        panel._tree_items_by_path = {}
        panel._tree_index_dirty = True
        return panel

    def test_index_used_and_rebuilt_lazily(self, qapp) -> None:
        """正例: 脏标记触发一次懒重建，缩略图按 path O(1) 命中节点。"""
        asset = SimpleNamespace(
            path="/media/a.mp4", asset_id="a1")
        panel = self._make_panel_skeleton(qapp, {"a1": asset})
        try:
            item = QTreeWidgetItem(["a.mp4"])
            item.setData(0, Qt.ItemDataRole.UserRole, "a1")
            panel.tree.addTopLevelItem(item)

            pixmap = QPixmap(16, 16)
            pixmap.fill(QColor(255, 0, 0))
            panel._on_thumbnail_ready("/media/a.mp4", pixmap)

            assert panel._tree_index_dirty is False
            assert panel._tree_items_by_path["/media/a.mp4"] == [item]
            assert not item.icon(0).isNull()
        finally:
            panel.tree.deleteLater()

    def test_dirty_flag_set_on_clear(self, qapp) -> None:
        """树重建点（_delayed_refresh / clear_project）会置脏标记。"""
        import inspect

        from src.features.edit.ui.media_pool_panel import MediaPoolPanel

        src = inspect.getsource(MediaPoolPanel._delayed_refresh)
        assert "_tree_index_dirty = True" in src
        src_clear = inspect.getsource(MediaPoolPanel.clear_project)
        assert "_tree_index_dirty = True" in src_clear

    def test_reverted_scan_does_not_use_index(self) -> None:
        """反例护栏: 回退到全树扫描后 _on_thumbnail_ready 不再消费索引。"""
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel

        src = inspect.getsource(MediaPoolPanel._on_thumbnail_ready)
        assert "_tree_index_dirty" in src and "_tree_items_by_path" in src, (
            "AUD-20209 回退检测：缩略图回调又退回每张全树扫描"
        )


# ────────────────────────────────────────────────────────────────────────
# AUD-20210: 代理分辨率探测移出 UI 线程
# ────────────────────────────────────────────────────────────────────────

class TestAud20210ProxyProbeOffThread:
    def test_probe_falls_back_to_estimate(self, qapp) -> None:
        """正例: 纯探测函数在不可读文件上走估算兜底（无 UI 依赖）。"""
        from src.features.edit.ui.media_pool_panel import _probe_proxy_resolution

        asset = SimpleNamespace(width=1920, height=1080)
        result = _probe_proxy_resolution(
            str(__import__("pathlib").Path(__file__).resolve()), asset)
        assert result == (1280, 720)

    def test_update_disk_proxy_services_uses_worker(self, qapp, monkeypatch, tmp_path) -> None:
        """正例: _update_disk_proxy_services 只做 stat，探测经 worker 回填。"""
        import threading

        from PyQt6.QtCore import QObject

        from src.features.edit.ui import media_pool_panel as mod

        panel = mod.MediaPoolPanel.__new__(mod.MediaPoolPanel)
        panel._pending_proxy_meta = {}

        received: List[tuple] = []

        class _Receiver(QObject):
            def on_probed(self, proxy_path: str, source_path: str,
                          resolution: object, asset_id: str = "",
                          generation: int = -1, request_id: str = "") -> None:
                # N21：信号载荷含 asset_id/generation
                received.append(
                    (proxy_path, source_path, resolution, asset_id,
                     generation, request_id))

        receiver = _Receiver()
        signals = mod.ProxyResolutionSignals()
        signals.probed.connect(receiver.on_probed)
        panel._proxy_resolution_signals = signals

        gate = threading.Event()
        probe_calls: List[str] = []

        def fake_probe(proxy_path, asset):
            # 用门闩保证「_update_disk_proxy_services 返回时探测尚未执行」
            # 的断言无竞态（worker 线程可能瞬间被调度）。
            gate.wait(5.0)
            probe_calls.append(proxy_path)
            return (640, 360)

        monkeypatch.setattr(mod, "_probe_proxy_resolution", fake_probe)

        marked: List[Any] = []

        class _FakeDiskService:
            def mark_ready(self, source_path, info):
                marked.append((source_path, info))

        class _FakeFrameService:
            def get_shared_clips_info(self):
                return {}

        monkeypatch.setattr(mod, "get_disk_proxy_service",
                            lambda: _FakeDiskService())
        monkeypatch.setattr(mod, "get_frame_provider_service",
                            lambda: _FakeFrameService())

        media_file = tmp_path / "src.mp4"
        media_file.write_bytes(b"x")
        asset = SimpleNamespace(path=str(media_file), width=1920, height=1080)

        panel._update_disk_proxy_services(asset, str(media_file))
        # 修复核心：同步返回时探测尚未执行（worker 被门闩挡住）
        assert probe_calls == []
        assert str(media_file) in panel._pending_proxy_meta

        gate.set()
        ok = _wait_for(lambda: bool(received), qapp)
        assert ok, "worker 探测未在超时内回填"
        assert probe_calls == [str(media_file)]

        # UI 线程回调回填服务缓存
        panel._on_proxy_resolution_probed(*received[0])
        assert marked, "探测结果未回填 DiskProxyService"
        source_path, info = marked[0]
        assert source_path == str(media_file)
        assert info.proxy_resolution == (640, 360)
        assert info.status == mod.ProxyStatus.READY
        assert str(media_file) not in panel._pending_proxy_meta

    def test_reverted_probes_synchronously(self, qapp, monkeypatch, tmp_path) -> None:
        """反例护栏: 回退为同步探测后，返回时探测已执行。"""
        import threading

        from src.features.edit.ui import media_pool_panel as mod

        gate = threading.Event()
        probe_calls: List[str] = []

        def fake_probe(proxy_path, asset):
            gate.wait(5.0)
            probe_calls.append(proxy_path)
            return (640, 360)

        monkeypatch.setattr(mod, "_probe_proxy_resolution", fake_probe)

        panel = mod.MediaPoolPanel.__new__(mod.MediaPoolPanel)
        panel._proxy_resolution_signals = mod.ProxyResolutionSignals()
        panel._pending_proxy_meta = {}

        class _FakeDiskService:
            def mark_ready(self, source_path, info):
                pass

        monkeypatch.setattr(mod, "get_disk_proxy_service",
                            lambda: _FakeDiskService())

        media_file = tmp_path / "src.mp4"
        media_file.write_bytes(b"x")
        asset = SimpleNamespace(path=str(media_file), width=1920, height=1080)
        panel._update_disk_proxy_services(asset, str(media_file))
        gate.set()
        assert probe_calls == [], (
            "AUD-20210 回退检测：代理分辨率探测又在 UI 线程同步执行了"
        )


# ────────────────────────────────────────────────────────────────────────
# AUD-20213: 永远空转的 _update_timeline_highlight_list 已移除
# ────────────────────────────────────────────────────────────────────────

class TestAud20213DeadHighlightListRemoved:
    def test_function_removed(self) -> None:
        """正例: 空转函数与其调用点均已删除。"""
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel

        assert not hasattr(MediaPoolPanel, "_update_timeline_highlight_list"), (
            "AUD-20213 回退检测：查找不存在节点的空转函数又回来了"
        )
        src = inspect.getsource(MediaPoolPanel.set_current_timeline)
        assert "_update_timeline_highlight_list" not in src
        src_refresh = inspect.getsource(MediaPoolPanel._delayed_refresh)
        assert "_update_timeline_highlight_list" not in src_refresh

    def test_grid_highlight_kept(self) -> None:
        """边界正例: grid 视图高亮路径保留。"""
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel

        assert hasattr(MediaPoolPanel, "_update_timeline_highlight_grid")
        assert hasattr(MediaPoolPanel, "set_current_timeline")


# ────────────────────────────────────────────────────────────────────────
# AUD-20301: multicam pending 请求清理与过期帧丢弃
# ────────────────────────────────────────────────────────────────────────

class TestAud20301MulticamPendingLifecycle:
    def _make_panel(self, qapp):
        from src.features.edit.ui.multicam_panel import MulticamPanel
        return MulticamPanel()

    def test_stale_pending_pruned(self, qapp) -> None:
        """正例: 超时未应答的请求被清理，新请求保留。"""
        panel = self._make_panel(qapp)
        try:
            panel._pending_by_key[("old.mp4", 1)] = (
                "a1", time.monotonic() - 100.0)
            panel._pending_by_key[("new.mp4", 2)] = (
                "a2", time.monotonic())
            panel._prune_stale_pending()
            assert ("old.mp4", 1) not in panel._pending_by_key
            assert ("new.mp4", 2) in panel._pending_by_key
        finally:
            panel.deleteLater()

    def test_late_result_for_superseded_tile_dropped(self, qapp) -> None:
        """正例: tile 已改请求新帧时，迟到的旧帧不回显。"""
        panel = self._make_panel(qapp)
        try:
            from src.features.edit.ui.multicam_panel import _AngleTile

            tile = _AngleTile()
            panel._tiles["a1"] = tile
            tile.set_request_key("a1", "p.mp4", 6)  # 已转向第 6 帧
            old_pixmap = QPixmap(8, 8)
            old_pixmap.fill(QColor(255, 0, 0))
            panel._pending_by_key[("p.mp4", 5)] = ("a1", time.monotonic())

            panel._on_frame_thumbnail_ready("p.mp4", 5, old_pixmap)

            assert ("p.mp4", 5) not in panel._pending_by_key
            assert tile._thumb.pixmap() is None or tile._thumb.pixmap().isNull()

            # 当前请求帧的结果正常上屏
            cur_pixmap = QPixmap(8, 8)
            cur_pixmap.fill(QColor(0, 0, 255))
            panel._pending_by_key[("p.mp4", 6)] = ("a1", time.monotonic())
            panel._on_frame_thumbnail_ready("p.mp4", 6, cur_pixmap)
            assert not tile._thumb.pixmap().isNull()
        finally:
            panel.deleteLater()

    def test_reverted_shows_stale_frame(self, qapp) -> None:
        """反例护栏: 回退后迟到旧帧会直接上屏。"""
        panel = self._make_panel(qapp)
        try:
            from src.features.edit.ui.multicam_panel import _AngleTile

            tile = _AngleTile()
            panel._tiles["a1"] = tile
            tile.set_request_key("a1", "p.mp4", 6)
            old_pixmap = QPixmap(8, 8)
            old_pixmap.fill(QColor(255, 0, 0))
            panel._pending_by_key[("p.mp4", 5)] = ("a1", time.monotonic())
            panel._on_frame_thumbnail_ready("p.mp4", 5, old_pixmap)
            assert tile._thumb.pixmap() is None or tile._thumb.pixmap().isNull(), (
                "AUD-20301 回退检测：过期帧又短暂回显到 tile"
            )
        finally:
            panel.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-20316: 反向播放实测 fps 更新
# ────────────────────────────────────────────────────────────────────────

class _FakeHudController:
    def __init__(self) -> None:
        self.state = SimpleNamespace(name="PLAYING")
        self.current_frame = 0
        self.fps = 24.0
        self.playback_speed = 1.0
        self.dropped_frames = 0
        self.is_playing = True


class TestAud20316ReversePlaybackFps:
    def _make_hud(self, qapp, controller, clock):
        from src.features.edit.ui.playback_hud import PlaybackHud

        return PlaybackHud(
            controller_getter=lambda: controller,
            now_fn=lambda: clock[0],
        )

    def test_reverse_playback_updates_fps(self, qapp) -> None:
        """正例: 反向播放（帧号递减）时实测 fps 正确更新。"""
        controller = _FakeHudController()
        clock = [0.0]
        hud = self._make_hud(qapp, controller, clock)
        try:
            controller.current_frame = 100
            hud._refresh()
            clock[0] = 0.5
            controller.current_frame = 88  # 反向：12 帧 / 0.5s = 24 fps
            hud._refresh()
            assert abs(hud._measured_fps - 24.0) < 1.0, (
                "AUD-20316 回退检测：反向播放时实测 fps 冻结在旧值"
            )
        finally:
            hud.deleteLater()

    def test_forward_and_stopped_unchanged(self, qapp) -> None:
        """边界正例: 正向播放照旧；停止后清零。"""
        controller = _FakeHudController()
        clock = [0.0]
        hud = self._make_hud(qapp, controller, clock)
        try:
            controller.current_frame = 0
            hud._refresh()
            clock[0] = 0.5
            controller.current_frame = 12
            hud._refresh()
            assert abs(hud._measured_fps - 24.0) < 1.0

            controller.is_playing = False
            hud._refresh()
            assert hud._measured_fps == 0.0
        finally:
            hud.deleteLater()


# ────────────────────────────────────────────────────────────────────────
# AUD-20405: VBO 流式上传 + uniform 位置缓存
# ────────────────────────────────────────────────────────────────────────

class TestAud20405RenderHotPath:
    def test_render_texture_structure(self) -> None:
        """静态正例: _render_texture 用 STREAM_DRAW 且不再每帧查 uniform。"""
        from src.features.edit.ui.program_viewer import ProgramViewer

        src = inspect.getsource(ProgramViewer._render_texture)
        assert "GL_STREAM_DRAW" in src, (
            "AUD-20405 回退检测：每帧重建的 VBO 又标记为 GL_STATIC_DRAW"
        )
        assert "_gl.glGetUniformLocation" not in src, (
            "AUD-20405 回退检测：_render_texture 又在每帧查询 UniformLocation"
        )

    def test_uniform_location_cached(self, monkeypatch) -> None:
        """行为正例: location 只查询一次并缓存。"""
        from src.features.edit.ui import program_viewer as mod

        queries: List[int] = []

        class _FakeGL:
            def glGetUniformLocation(self, prog, name):
                queries.append(1)
                return 7

        monkeypatch.setattr(mod, "_gl", _FakeGL())
        fake_self = SimpleNamespace(_prog=5, _tex_uniform_loc=None)
        assert mod.ProgramViewer._tex_uniform_location(fake_self) == 7
        assert mod.ProgramViewer._tex_uniform_location(fake_self) == 7
        assert len(queries) == 1, (
            "AUD-20405 回退检测：UniformLocation 缓存失效，被重复查询"
        )

    def test_cache_invalidated_on_program_init(self) -> None:
        """正例: program 重建时缓存置 None。"""
        from src.features.edit.ui.program_viewer import ProgramViewer

        src = inspect.getsource(ProgramViewer._initializeGL_impl)
        assert "_tex_uniform_loc = None" in src


# ────────────────────────────────────────────────────────────────────────
# AUD-20406: CPU 上传路径减少全帧级拷贝
# ────────────────────────────────────────────────────────────────────────

class TestAud20406CpuUploadCopies:
    def test_padded_image_converted_correctly(self, qapp) -> None:
        """行为正例: 行跨距对齐路径（width*3 % 4 != 0）输出正确 RGBA。"""
        from src.features.edit.ui.program_viewer import _rgb888_qimage_to_rgba

        image = QImage(3, 2, QImage.Format.Format_RGB888)
        image.setPixel(0, 0, 0xFF0000)  # red
        image.setPixel(1, 0, 0x00FF00)  # green
        image.setPixel(2, 0, 0x0000FF)  # blue
        image.setPixel(0, 1, 0xFFFFFF)  # white
        image.setPixel(1, 1, 0x000000)  # black
        image.setPixel(2, 1, 0x808080)  # gray

        rgba = _rgb888_qimage_to_rgba(image)
        assert rgba is not None
        assert rgba.shape == (2, 3, 4)
        assert rgba[0, 0].tolist() == [255, 0, 0, 255]
        assert rgba[0, 1].tolist() == [0, 255, 0, 255]
        assert rgba[0, 2].tolist() == [0, 0, 255, 255]
        assert rgba[1, 0].tolist() == [255, 255, 255, 255]
        assert rgba[1, 1].tolist() == [0, 0, 0, 255]
        assert rgba[1, 2].tolist() == [128, 128, 128, 255]

    def test_compact_image_converted_correctly(self, qapp) -> None:
        """行为正例: 紧凑行宽路径（width*3 % 4 == 0）同样正确。"""
        from src.features.edit.ui.program_viewer import _rgb888_qimage_to_rgba

        image = QImage(4, 1, QImage.Format.Format_RGB888)
        image.setPixel(0, 0, 0xFF0000)
        image.setPixel(3, 0, 0x0000FF)
        rgba = _rgb888_qimage_to_rgba(image)
        assert rgba is not None
        assert rgba.shape == (1, 4, 4)
        assert rgba[0, 0].tolist() == [255, 0, 0, 255]
        assert rgba[0, 3].tolist() == [0, 0, 255, 255]

    def test_helper_avoids_temporary_full_frame_arrays(self) -> None:
        """静态反例护栏: 回退到 concatenate/ascontiguousarray 即失败。"""
        from src.features.edit.ui import program_viewer as mod

        src = inspect.getsource(mod._rgb888_qimage_to_rgba)
        assert "np.concatenate" not in src, (
            "AUD-20406 回退检测：helper 又引入 concatenate 全帧临时数组"
        )
        assert "np.ascontiguousarray" not in src, (
            "AUD-20406 回退检测：helper 又引入 ascontiguousarray 全帧拷贝"
        )


# ────────────────────────────────────────────────────────────────────────
# AUD-20411: 字幕 overlay 每帧循环内的 import 提升
# ────────────────────────────────────────────────────────────────────────

class TestAud20411SubtitleOverlayImports:
    def test_no_import_inside_per_subtitle_loop(self) -> None:
        """正例: render_plan / graphics 的 import 都在循环之前。"""
        from src.features.edit.ui.program_viewer import ProgramViewer

        src = inspect.getsource(ProgramViewer._paint_subtitle_overlay)
        loop_idx = src.find("for item in self._subtitle_overlays:")
        assert loop_idx >= 0
        tail = src[loop_idx:]
        assert "from src.features" not in tail, (
            "AUD-20411 回退检测：每帧×每字幕的循环体内又出现 import"
        )
        head = src[:loop_idx]
        assert "from src.features.edit.core.captions.render_plan import from_clip" in head
        assert "caption_graphic_animation_state" in head


# ────────────────────────────────────────────────────────────────────────
# AUD-20502: QuickProjectDialog 构造期不再同步探测媒体元数据
# ────────────────────────────────────────────────────────────────────────

class TestAud20502QuickProjectAsyncProbe:
    def _capture_worker(self, monkeypatch):
        from src.features.edit.ui import quick_project_dialog as mod

        captured: List[Any] = []

        class _NoStartPool:
            def start(self, worker):
                captured.append(worker)

        monkeypatch.setattr(
            mod.QThreadPool, "globalInstance",
            staticmethod(lambda: _NoStartPool()),
        )
        return mod, captured

    def test_constructor_defers_probe(self, qapp, monkeypatch) -> None:
        """正例: 构造期只派发 worker 不同步探测；worker 手动运行后 UI 回填。"""
        mod, captured = self._capture_worker(monkeypatch)

        probe_calls: List[str] = []

        def fake_probe(self, path: str) -> dict:
            probe_calls.append(path)
            return {
                "width": 640, "height": 360, "fps": 25.0,
                "duration_seconds": 12.0, "total_frames": 300,
                "media_type": "video", "codec": "h264",
            }

        monkeypatch.setattr(mod.QuickProjectDialog,
                            "_probe_media_metadata", fake_probe)
        dialog = mod.QuickProjectDialog("/media/clip_test.mp4")
        try:
            # 修复核心：构造返回时探测尚未执行（worker 已被捕获）
            assert len(captured) == 1
            assert probe_calls == [], (
                "AUD-20502 回退检测：构造函数又在 UI 线程同步探测媒体元数据"
            )
            # 占位元数据先行（扩展名判定 video）
            assert dialog._media_metadata["media_type"] == "video"
            assert dialog._metadata_probe_pending is True

            captured[0].run()
            qapp.processEvents()

            assert probe_calls == ["/media/clip_test.mp4"]
            assert dialog._metadata_probe_pending is False
            assert dialog._media_metadata["width"] == 640
            # Auto 分辨率模式回填 + 媒体信息标签刷新
            assert dialog.width_spin.value() == 640
            assert dialog.height_spin.value() == 360
            assert dialog._media_info_labels["resolution"].text() == "640x360"
            assert dialog._media_info_labels["duration"].text() == "00:12"
        finally:
            dialog.deleteLater()

    def test_placeholder_media_metadata_by_extension(self, qapp) -> None:
        """正例: 占位元数据按扩展名判定类型，默认值无 IO。"""
        from src.features.edit.ui.quick_project_dialog import QuickProjectDialog

        video = QuickProjectDialog._placeholder_media_metadata("/x/a.mp4")
        assert video["media_type"] == "video"
        assert video["width"] == 1920
        image = QuickProjectDialog._placeholder_media_metadata("/x/b.png")
        assert image["media_type"] == "image"

    def test_reverted_constructor_probes_synchronously(self, qapp, monkeypatch) -> None:
        """反例护栏: 回退后构造期同步调用探测。"""
        from src.features.edit.ui import quick_project_dialog as mod

        # 不 patch QThreadPool —— 真实派发也无所谓，只要同步调用被检出
        probe_calls: List[str] = []

        def fake_probe(self, path: str) -> dict:
            probe_calls.append(path)
            return {"media_type": "video"}

        monkeypatch.setattr(mod.QuickProjectDialog,
                            "_probe_media_metadata", fake_probe)
        dialog = mod.QuickProjectDialog("/media/clip_test.mp4")
        try:
            assert probe_calls == [], (
                "AUD-20502 回退检测：构造期同步探测"
            )
        finally:
            dialog.deleteLater()
