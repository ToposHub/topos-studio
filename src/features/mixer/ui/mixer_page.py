"""UI workspace coordinator; the app owns the independently docked panels.

The local session is a deliberate presentation prototype. It does not construct
an EditPage, mutate an EditProject, decode audio, or create an audio device.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDockWidget, QFileDialog, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from src.features.mixer.effects import (
    EffectBrowserWidget, EffectInstance, EffectWindowManager, create_effect_panel,
    get_effect, stop_panel_timers,
)
from .console_panel import ConsolePanel
from .session import MixerUiSession, effect_scope
from .timeline_panel import TimelinePanel
from .viewer_panel import ViewerPanel
from .workspace_style import PANEL_QSS


class MixerPage(QWidget):
    """One UI session with three main docks and an optional per-clip editor dock."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = MixerUiSession(self)
        self.effects_manager = EffectWindowManager(self)
        self.console_panel = ConsolePanel(self.session, self.effects_manager)
        self.viewer_panel = ViewerPanel(self.session)
        self.timeline_panel = TimelinePanel(self.session)
        self.active = False
        self._disposed = False
        self._effect_before: tuple | None = None
        self._clip_panels: dict[str, QWidget] = {}
        self._clip_wrappers: dict[str, QWidget] = {}
        self._current_editor_clip: str | None = None
        self._editor_has_been_placed = False
        self._build_clip_editor_host()
        self._build_effect_browser()
        self.console_panel.effectsRequested.connect(self.show_effect_browser)
        self.timeline_panel.clipEditorRequested.connect(self.open_clip_editor)
        self.timeline_panel.importRequested.connect(self._choose_audio)
        self.session.changed.connect(self._sync_effect_instances)
        self.session.selection_changed.connect(self._sync_browser_owner)
        self.session.status_changed.connect(self._show_status)
        self.effects_manager.instanceAboutToChange.connect(self._effect_about_to_change)
        self.effects_manager.instanceChanged.connect(self._effect_changed)
        self.viewer_panel.installEventFilter(self)
        self.console_panel.installEventFilter(self)
        self.clip_editor_panel.installEventFilter(self)
        self._sync_browser_owner()

    def _build_clip_editor_host(self) -> None:
        self.clip_editor_panel = QWidget()
        self.clip_editor_panel.setObjectName("MixerClipEditorHost")
        self.clip_editor_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.clip_editor_panel.setStyleSheet(PANEL_QSS)
        root = QVBoxLayout(self.clip_editor_panel)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        toolbar = QHBoxLayout()
        self.clip_editor_title = QLabel("选择片段后右键 → 编辑片段")
        toolbar.addWidget(self.clip_editor_title, 1)
        self.clip_effects_button = QPushButton("片段效果")
        self.clip_effects_button.setEnabled(False)
        self.clip_effects_button.clicked.connect(self._show_clip_effects)
        toolbar.addWidget(self.clip_effects_button)
        self.float_editor_button = QPushButton("独立窗口")
        self.float_editor_button.setToolTip("将片段编辑器作为可浮动的独立面板")
        self.float_editor_button.clicked.connect(lambda: self.set_editor_floating(True))
        toolbar.addWidget(self.float_editor_button)
        self.merge_editor_button = QPushButton("与时间线合并")
        self.merge_editor_button.clicked.connect(self.merge_editor_with_timeline)
        toolbar.addWidget(self.merge_editor_button)
        timeline = QPushButton("回到时间线")
        timeline.clicked.connect(self.show_timeline)
        toolbar.addWidget(timeline)
        close = QPushButton("关闭")
        close.clicked.connect(self.close_clip_editor)
        toolbar.addWidget(close)
        root.addLayout(toolbar)
        notice = QLabel("单片段 UI 精调 · 波形为归一化概览；频谱 / 测量 / 音频处理为原型示意，尚未连接音频后端")
        notice.setWordWrap(True)
        notice.setStyleSheet("color: #B5B5C2; background: #222226; padding: 4px 8px; font-size: 10px;")
        root.addWidget(notice)
        self.clip_stack = QStackedWidget()
        root.addWidget(self.clip_stack, 1)
        placeholder = QLabel("在时间线中右键片段，选择“编辑片段”…")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clip_stack.addWidget(placeholder)

    def _build_effect_browser(self) -> None:
        self.browser_window = QDialog(self, Qt.WindowType.Window)
        self.browser_window.setWindowTitle("混音 · 效果器库")
        self.browser_window.setObjectName("MixerEffectBrowser")
        self.browser_window.setStyleSheet(PANEL_QSS)
        self.browser_window.setMinimumSize(360, 440)
        self.browser_window.resize(390, 620)
        self.browser_window.setModal(False)
        layout = QVBoxLayout(self.browser_window)
        layout.setContentsMargins(6, 6, 6, 6)
        self.browser_owner = QComboBox()
        self.browser_owner.addItems(["选中轨道 / 总线", "选中片段"])
        self.browser_owner.currentIndexChanged.connect(self._sync_browser_owner)
        layout.addWidget(self.browser_owner)
        self.browser_owner_label = QLabel()
        self.browser_owner_label.setWordWrap(True)
        layout.addWidget(self.browser_owner_label)
        self.effect_browser = EffectBrowserWidget()
        self.effect_browser.effectChosen.connect(self.insert_effect)
        layout.addWidget(self.effect_browser, 1)

    def _sync_browser_owner(self, *_args: object) -> None:
        clip_scope = self.browser_owner.currentIndex() == 1
        owner = (self.session.clip_by_id(self.session.selected_clip_id) if clip_scope else
                 self.session.track_by_id(self.session.selected_track_id))
        self.browser_owner_label.setText(f"插入到：{owner.name}" if owner else "请先选择一个片段")
        self.effect_browser.set_scope(effect_scope(owner) if owner else "clip")
        self.effect_browser.setEnabled(owner is not None)

    def _show_clip_effects(self) -> None:
        clip = self.session.clip_by_id(self._current_editor_clip)
        if clip is None:
            return
        self.session.select_clip(clip.id)
        menu = QMenu(self.clip_editor_panel)
        menu.addAction("打开效果器库…", self._browse_clip_effects)
        for index, effect in enumerate(clip.effects):
            description = get_effect(effect.effect_id)
            item = menu.addMenu(f"{'●' if effect.enabled else '○'} {description.name_zh}")
            item.addAction("打开参数", lambda _=False, fx=effect: self.effects_manager.open(fx, clip.name))
            item.addAction("旁路 / 启用", lambda _=False, fx=effect: self._toggle_clip_effect(fx))
            item.addAction("上移", lambda _=False, row=index: self._move_clip_effect(clip.id, row, -1)).setEnabled(index > 0)
            item.addAction("下移", lambda _=False, row=index: self._move_clip_effect(clip.id, row, 1)).setEnabled(index + 1 < len(clip.effects))
            item.addAction("移除", lambda _=False, fx=effect: self._remove_clip_effect(fx))
        menu.exec(self.clip_effects_button.mapToGlobal(self.clip_effects_button.rect().bottomLeft()))

    def _browse_clip_effects(self) -> None:
        self.browser_owner.setCurrentIndex(1)
        self.show_effect_browser()

    def _toggle_clip_effect(self, effect: EffectInstance) -> None:
        before = self.session.snapshot()
        self.effects_manager.set_enabled(effect, not effect.enabled)
        self.session.commit_preview("切换片段效果旁路", before)

    def _move_clip_effect(self, clip_id: str, index: int, delta: int) -> None:
        clip = self.session.clip_by_id(clip_id)
        if clip is None or not 0 <= index + delta < len(clip.effects):
            return
        ordered = list(clip.effects)
        ordered[index], ordered[index + delta] = ordered[index + delta], ordered[index]
        self.session.set_effects(clip_id, ordered)

    def _remove_clip_effect(self, effect: EffectInstance) -> None:
        clip = self.session.clip_by_id(effect.owner_id)
        if clip is not None:
            self.session.set_effects(clip.id, [fx for fx in clip.effects if fx.instance_id != effect.instance_id])

    def show_effect_browser(self) -> None:
        self._sync_browser_owner()
        self.browser_window.show()
        self.browser_window.raise_()
        self.browser_window.activateWindow()

    def insert_effect(self, effect_id: str) -> None:
        if effect_id == "clip_editor":
            if self.session.selected_clip_id:
                self.open_clip_editor(self.session.selected_clip_id)
            return
        owner = (self.session.clip_by_id(self.session.selected_clip_id) if self.browser_owner.currentIndex() == 1
                 else self.session.track_by_id(self.session.selected_track_id))
        if owner is None:
            return
        descriptor = get_effect(effect_id)
        if effect_scope(owner) not in descriptor.scope:
            return
        instance = EffectInstance.create(effect_id, owner.id)
        self.session.set_effects(owner.id, [*owner.effects, instance])
        current = self.session.track_by_id(owner.id) or self.session.clip_by_id(owner.id)
        if current:
            self.effects_manager.open(current.effects[-1], current.name)
        self._show_status(f"已插入 {descriptor.name_zh} · 仅本地参数界面，DSP 尚未连接")

    def _all_effects(self) -> list[EffectInstance]:
        return [effect for owner in (*self.session.tracks, *self.session.clips) for effect in owner.effects]

    def _sync_effect_instances(self) -> None:
        self.effects_manager.reconcile(self._all_effects())
        self._sync_browser_owner()
        for clip_id in list(self._clip_panels):
            if self.session.clip_by_id(clip_id) is None:
                panel = self._clip_panels.pop(clip_id)
                wrapper = self._clip_wrappers.pop(clip_id)
                stop_panel_timers(panel)
                self.clip_stack.removeWidget(wrapper)
                wrapper.deleteLater()
                if self._current_editor_clip == clip_id:
                    self._current_editor_clip = None
                    self.clip_editor_title.setText("选择片段后右键 → 编辑片段")
                    self.clip_effects_button.setEnabled(False)
                    self.close_clip_editor()

    def _effect_about_to_change(self, instance: EffectInstance) -> None:
        self._effect_before = self.session.snapshot()

    def _effect_changed(self, instance: EffectInstance) -> None:
        before, self._effect_before = self._effect_before, None
        if before is not None:
            self.session.commit_preview("调整效果器参数", before)

    def open_clip_editor(self, clip_id: str) -> None:
        clip = self.session.clip_by_id(clip_id)
        if clip is None:
            return
        for panel in self._clip_panels.values():
            stop_panel_timers(panel)
        panel = self._clip_panels.get(clip_id)
        if panel is None:
            panel = create_effect_panel("clip_editor", clip.name)
            stop_panel_timers(panel)
            # Reference peaks are a downsampled mono envelope, not decoded stereo PCM.
            source_duration = clip.source_duration or clip.duration + clip.offset
            peaks = clip.peaks or (0.0,) * 220
            start = min(len(peaks) - 1, int(clip.offset / source_duration * len(peaks)))
            end = max(start + 1, min(len(peaks), int((clip.offset + clip.duration) / source_duration * len(peaks))))
            excerpt = list(peaks[start:end])
            panel.load_audio_data(44100, clip.duration, [[-p for p in excerpt]], [excerpt],
                                  [[0.0 for _p in excerpt]], [], clip.name)
            panel.update_output_meter(-60, -60)
            panel.sr_badge.setText("44.1k · 概览")
            panel.btn_apply_lufs.setText("匹配目标响度 · UI 请求")
            for label in panel.findChildren(QLabel):
                if "DSP READY" in label.text():
                    label.setText("● UI PREVIEW")
            for button in panel.findChildren(QPushButton):
                if "保存修改并返回剪辑" in button.text():
                    button.setText("✓ 保留 UI 步骤并返回时间线")
                    button.setToolTip("当前不写入真实工程或修改音频文件")
            for name in ("peak_val_lbl", "rms_val_lbl", "noise_val_lbl", "lufs_val_lbl", "true_peak_lbl"):
                label = getattr(panel, name, None)
                if isinstance(label, QLabel):
                    label.setText("— 待测量")
            panel.processStackChanged.connect(lambda steps, identity=clip_id: self._process_requested(identity, steps))
            panel.commitAndCloseRequested.connect(self.close_clip_editor)
            panel.playStateChanged.connect(lambda _playing, widget=panel: self._stop_editor_audio(widget))
            wrapper = QScrollArea()
            wrapper.setWidgetResizable(True)
            wrapper.setFrameShape(QScrollArea.Shape.NoFrame)
            wrapper.setWidget(panel)
            self.clip_stack.addWidget(wrapper)
            self._clip_panels[clip_id], self._clip_wrappers[clip_id] = panel, wrapper
        self._current_editor_clip = clip_id
        self.clip_effects_button.setEnabled(True)
        self.clip_stack.setCurrentWidget(self._clip_wrappers[clip_id])
        self.clip_editor_title.setText(clip.name)
        self.clip_editor_title.setToolTip(clip.name)
        dock = self._editor_dock()
        if dock:
            dock.setWindowTitle(f"片段编辑 · {clip.name}")
            host = dock.parentWidget()
            tabbed = isinstance(host, QMainWindow) and bool(host.tabifiedDockWidgets(dock))
            if not (dock.isVisible() or self._editor_has_been_placed or tabbed or dock.isFloating()):
                dock.setFloating(True)
                dock.resize(1220, 780)
            self._editor_has_been_placed = True
            dock.show()
            dock.raise_()
        else:
            self.clip_editor_panel.setWindowFlag(Qt.WindowType.Window, True)
            self.clip_editor_panel.resize(1200, 760)
            self.clip_editor_panel.show()
        self._show_status(f"打开单片段编辑：{clip.name} · 参数操作暂存于界面，音频处理待接入")

    def _process_requested(self, clip_id: str, steps: list[dict[str, Any]]) -> None:
        self._show_status(f"片段精调已记录 {len(steps)} 个 UI 处理步骤 · 尚未提交工程或渲染音频")
        panel = self._clip_panels.get(clip_id)
        if panel:
            stop_panel_timers(panel)
            panel.lufs_val_lbl.setText("— 待测量")
            panel.true_peak_lbl.setText("— 待测量")
            panel.btn_apply_lufs.setText("匹配目标响度 · UI 请求")

    def _stop_editor_audio(self, panel: QWidget) -> None:
        stop_panel_timers(panel)
        panel._is_playing = False
        panel.btn_play.setChecked(False)
        panel.btn_play.setText("▶ 播放")
        self._show_status("片段编辑器仅提供 UI 精调；试听播放等待后端接线")

    def _editor_dock(self) -> QDockWidget | None:
        parent = self.clip_editor_panel.parentWidget()
        return parent if isinstance(parent, QDockWidget) else None

    def set_editor_floating(self, floating: bool) -> None:
        dock = self._editor_dock()
        if dock:
            self._editor_has_been_placed = True
            dock.setFloating(floating)
            if floating:
                dock.resize(1220, 780)
            dock.show()
            dock.raise_()

    def merge_editor_with_timeline(self) -> None:
        dock = self._editor_dock()
        host = dock.parentWidget() if dock else None
        if isinstance(host, QMainWindow):
            timeline = host.findChild(QDockWidget, "dockAudioTimeline")
            if timeline:
                self._editor_has_been_placed = True
                dock.setFloating(False)
                host.tabifyDockWidget(timeline, dock)
                timeline.show()
                dock.show()
                dock.raise_()

    def show_timeline(self) -> None:
        host = self.timeline_panel.parentWidget()
        if isinstance(host, QDockWidget):
            host.show()
            host.raise_()

    def close_clip_editor(self) -> None:
        for panel in self._clip_panels.values():
            stop_panel_timers(panel)
        dock = self._editor_dock()
        (dock or self.clip_editor_panel).hide()

    def _choose_audio(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self.timeline_panel, "选择音频素材", "", "音频 (*.wav *.mp3 *.aif *.aiff *.flac *.m4a);;所有文件 (*)")
        self.session.import_media_paths(paths)

    def _show_status(self, message: str) -> None:
        self.viewer_panel.status.setText(message)
        self.viewer_panel.status.setToolTip(message)

    def on_enter(self) -> None:
        self.active = True
        self.viewer_panel.sync_timecode()

    def on_leave(self) -> None:
        self.active = False
        self.stop_preview()
        self.effects_manager.pause_all()
        self.browser_window.hide()
        for panel in self._clip_panels.values():
            stop_panel_timers(panel)

    def stop_preview(self) -> None:
        self.viewer_panel.stop_preview()

    def dispose_ui(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self.on_leave()
        self.effects_manager.dispose_all()
        for clip_id, panel in self._clip_panels.items():
            stop_panel_timers(panel)
            self._clip_wrappers[clip_id].deleteLater()
        self._clip_panels.clear()
        self._clip_wrappers.clear()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Hide:
            if watched is self.viewer_panel:
                self.stop_preview()
            if watched is self.console_panel:
                self.effects_manager.pause_all()
                self.browser_window.hide()
            if watched is self.clip_editor_panel:
                for panel in self._clip_panels.values():
                    stop_panel_timers(panel)
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.dispose_ui()
        super().closeEvent(event)
