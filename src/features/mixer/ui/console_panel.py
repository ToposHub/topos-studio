"""Dockable, UI-only channel console using the user's parameter-panel catalog."""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPoint, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import (
    QDoubleSpinBox, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from src.features.mixer.effects import EFFECT_CATALOG, EffectInstance, EffectWindowManager, get_effect
from .session import MixerUiSession, effect_scope
from .console_controls import EditableValuePill, LevelMeter, RotaryKnob, StudioFader, format_db
from src.shared.assets.icon_manager import get_icon, icon_add, icon_fx
from .workspace_style import PANEL_QSS, compact_combo, tool_button, toolbar_divider


class ChannelStrip(QFrame):
    def __init__(self, session: MixerUiSession, track_id: str, manager: EffectWindowManager,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session, self.track_id, self.manager = session, track_id, manager
        self._gesture: tuple | None = None
        self._effect_signature: tuple | None = None
        self._appearance: tuple[str, bool] | None = None
        self.setFixedWidth(104)
        self.setObjectName("MixerChannel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 6)
        layout.setSpacing(5)
        self.name_button = QPushButton()
        self.name_button.setToolTip("选中通道；右键重命名或复位")
        self.name_button.clicked.connect(lambda: session.select_track(track_id))
        self.name_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.name_button.customContextMenuRequested.connect(self._track_menu)
        layout.addWidget(self.name_button)
        self.routing = compact_combo()
        self.routing.setToolTip("输出路由 · 本地 UI 状态，尚未连接音频图")
        self.routing.activated.connect(lambda _: self.session.set_track_value(track_id, "output", self.routing.currentText()))
        layout.addWidget(self.routing)
        self.preamp = QWidget()
        pre = QHBoxLayout(self.preamp)
        pre.setContentsMargins(0, 0, 0, 0)
        pre.setSpacing(4)
        self.trim = RotaryKnob(-24, 24, 0, accent_color="#F0A848", size=24)
        self.trim.setToolTip("输入 Trim · UI 参数")
        self.trim.valueChanged.connect(lambda value: self._track_slider("trim_db", value))
        self.trim.installEventFilter(self)
        pre.addWidget(self.trim)
        pre.addWidget(QLabel("TRIM"))
        self.hpf = QPushButton("HPF")
        self.hpf.setCheckable(True)
        self.hpf.setToolTip("80 Hz 高通 · 待 DSP 接线")
        self.hpf.clicked.connect(lambda enabled: self.session.set_track_value(track_id, "hpf", enabled))
        pre.addWidget(self.hpf)
        layout.addWidget(self.preamp)
        rack = QWidget()
        rack_layout = QVBoxLayout(rack)
        rack_layout.setContentsMargins(0, 0, 0, 0)
        rack_layout.setSpacing(3)
        rack_header = QHBoxLayout()
        rack_header.addWidget(QLabel("插入效果"), 1)
        self.insert_button = tool_button("插入效果器", icon_add("#B6B9C2"), self._add_effect_menu, size=22, glyph=12)
        rack_header.addWidget(self.insert_button)
        rack_layout.addLayout(rack_header)
        self.fx_list = QListWidget()
        self.fx_list.setFixedHeight(48)
        self.fx_list.setToolTip("双击打开效果器；右键旁路、重排或删除")
        self.fx_list.itemDoubleClicked.connect(self._open_effect)
        self.fx_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.fx_list.customContextMenuRequested.connect(self._effect_menu)
        rack_layout.addWidget(self.fx_list)
        layout.addWidget(rack)
        send_panel = QWidget()
        send_row = QHBoxLayout(send_panel)
        send_row.setContentsMargins(0, 0, 0, 0)
        send_row.setSpacing(4)
        self.send = compact_combo()
        self.send.setToolTip("发送目标 · 待后端接线")
        self.send.activated.connect(lambda _: self.session.set_track_value(track_id, "send_target", self.send.currentText()))
        send_row.addWidget(self.send, 1)
        self.send_level = QDoubleSpinBox()
        self.send_level.setRange(-60, 6)
        self.send_level.setValue(-12)
        self.send_level.setDecimals(1)
        self.send_level.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.send_level.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.send_level.setStyleSheet("QDoubleSpinBox { padding: 2px 1px; font-size: 10px; }")
        self.send_level.setToolTip("发送量 dB")
        self.send_level.editingFinished.connect(lambda: self.session.set_track_value(track_id, "send_db", self.send_level.value()))
        send_row.addWidget(self.send_level)
        layout.addWidget(send_panel)
        self.send_pre = QPushButton("P")
        self.send_pre.setToolTip("推子前 / 推子后发送；本地路由状态")
        self.send_pre.setCheckable(True)
        self.send_pre.clicked.connect(lambda checked: self.session.set_track_value(track_id, "send_pre", checked))
        send_row.insertWidget(0, self.send_pre)
        self.send_pre.setFixedWidth(19)
        self.send_level.setFixedWidth(30)
        self.send.setMinimumWidth(30)
        self._section_panels = {"前级": self.preamp, "插入": rack, "发送": send_panel}
        flags = QHBoxLayout()
        flags.setSpacing(3)
        self.flag_buttons: dict[str, QPushButton] = {}
        controls = (("M", "muted"), ("S", "solo"), ("R", "armed"), ("Ø", "phase_inverted"))
        if track_id == "master":
            controls = (("M", "muted"), ("MONO", "monitor_mono"), ("DIM", "dim"))
        for text, key in controls:
            button = QPushButton(text)
            button.setFixedWidth({"MONO": 34, "DIM": 27}.get(text, 20))
            button.setCheckable(True)
            button.setToolTip({
                "M": "静音", "S": "独奏", "R": "录音待命", "Ø": "相位反转",
                "MONO": "单声道监听检查", "DIM": "监听衰减",
            }[text])
            button.clicked.connect(lambda checked, field=key: self.session.set_track_value(track_id, field, checked))
            self.flag_buttons[key] = button
            flags.addWidget(button)
        layout.addLayout(flags)
        pan_row = QHBoxLayout()
        self.pan = RotaryKnob(-100, 100, 0, accent_color=self._track_color(), size=28)
        self.pan.setToolTip("声像；双击归中")
        self.pan.valueChanged.connect(lambda value: self._track_slider("pan", value / 100))
        self.pan.installEventFilter(self)
        pan_row.addWidget(self.pan)
        self.pan_label = QLabel("C")
        self.pan_label.setFixedWidth(27)
        pan_row.addWidget(self.pan_label)
        self.width = RotaryKnob(0, 200, 100, bipolar=False, accent_color="#B67CF8", size=24)
        self.width.setToolTip("立体声宽度 0–200% · 待 DSP 接线")
        self.width.valueChanged.connect(lambda value: self._track_slider("width", value))
        self.width.installEventFilter(self)
        pan_row.addStretch()
        pan_row.addWidget(self.width)
        layout.addLayout(pan_row)
        fader_row = QHBoxLayout()
        self.fader = StudioFader(accent_color=self._track_color())
        self.fader.setToolTip("通道推子 · 当前仅改变 UI 状态")
        self.fader.dbChanged.connect(lambda value: self._track_slider("volume_db", value))
        self.fader.installEventFilter(self)
        fader_row.addStretch()
        fader_row.addWidget(self.fader)
        self.meter = LevelMeter(stereo=True, show_gr=True)
        self.meter.setToolTip("实时电平等待音频后端，未生成模拟信号")
        fader_row.addWidget(self.meter)
        fader_row.addStretch()
        layout.addLayout(fader_row, 1)
        readout = QHBoxLayout()
        self.volume = EditableValuePill("0.0", accent_color=self._track_color(), height=19)
        self.volume.setToolTip("通道增益 dB，点击输入")
        self.volume.valueEdited.connect(self._edit_volume)
        readout.addWidget(self.volume, 1)
        peak = QLabel("— dBFS")
        peak.setToolTip("峰值电平待接入")
        peak.setStyleSheet("font: 9px 'Menlo'; color: #9B9DA6;")
        readout.addWidget(peak)
        layout.insertLayout(layout.count() - 1, readout)
        self.automation = compact_combo()
        self.automation.addItems(["Off", "Read", "Touch", "Latch", "Write"])
        self.automation.setToolTip("自动化模式 · 记录与回放等待后端接线")
        self.automation.activated.connect(lambda _: self.session.set_track_value(track_id, "automation_mode", self.automation.currentText()))
        layout.addWidget(self.automation)
        self.sync()

    def _begin_gesture(self) -> None:
        self._gesture = self.session.snapshot()

    def _end_gesture(self) -> None:
        before, self._gesture = self._gesture, None
        if before is not None:
            self.session.commit_preview("调整通道推子 / 声像", before)

    def _track_color(self) -> str:
        track = self.session.track_by_id(self.track_id)
        return track.color if track else "#8CA6B8"

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                self.session.select_track(self.track_id)
                if self._gesture is None:
                    self._begin_gesture()
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._end_gesture()
        if event.type() == QEvent.Type.Hide:
            self._end_gesture()
        return super().eventFilter(watched, event)

    def _track_slider(self, key: str, value: object) -> None:
        track = self.session.track_by_id(self.track_id)
        if track:
            if self._gesture is None:
                self.session.set_track_value(self.track_id, key, value)
            else:
                setattr(track, key, value)
                self.session.touch()

    def sync(self) -> None:
        track = self.session.track_by_id(self.track_id)
        if not track:
            return
        selected = self.session.selected_track_id == track.id
        appearance = (track.color, selected)
        if appearance != self._appearance:
            self._appearance = appearance
            fill = "#303036" if selected else "#27272B"
            self.setStyleSheet(f"QFrame#MixerChannel {{ background: {fill}; border-top: 2px solid {track.color}; border-right: 1px solid #34363E; }}")
            for instrument in (self.pan, self.fader):
                instrument.accent_color = QColor(track.color)
                instrument.update()
        self.name_button.setText(track.name)
        self.name_button.setToolTip(track.name + " · 右键管理轨道")
        targets = ["Master"] + [t.name for t in self.session.tracks if t.role == "BUS" and t.id != track.id]
        for combo, values, current in ((self.routing, targets, track.output),
                                       (self.send, ["无发送"] + targets, track.send_target)):
            blocker = QSignalBlocker(combo)
            if [combo.itemText(i) for i in range(combo.count())] != values:
                combo.clear()
                combo.addItems(values)
            combo.setCurrentText(current)
            combo.setToolTip(current)
            del blocker
        self.routing.setEnabled(track.role != "MASTER")
        for key, button in self.flag_buttons.items():
            blocker = QSignalBlocker(button)
            button.setChecked(bool(getattr(track, key)))
            button.setEnabled(key != "armed" or track.role not in ("BUS", "MASTER"))
            del blocker
        self.pan.setValue(track.pan * 100, emit_signal=False)
        self.fader.setDb(track.volume_db, emit_signal=False)
        self.trim.setValue(track.trim_db, emit_signal=False)
        self.width.setValue(track.width, emit_signal=False)
        self.volume.setText(format_db(track.volume_db))
        blocker = QSignalBlocker(self.send_level)
        self.send_level.setValue(track.send_db)
        del blocker
        for button, enabled in ((self.hpf, track.hpf), (self.send_pre, track.send_pre)):
            blocker = QSignalBlocker(button)
            button.setChecked(enabled)
            del blocker
        blocker = QSignalBlocker(self.automation)
        self.automation.setCurrentText(track.automation_mode)
        del blocker
        self.pan_label.setText("C" if abs(track.pan) < .01 else f"{'L' if track.pan < 0 else 'R'}{abs(int(track.pan*100))}")
        signature = tuple((fx.instance_id, fx.effect_id, fx.enabled) for fx in track.effects)
        if signature != self._effect_signature:
            self._effect_signature = signature
            self.fx_list.clear()
            if not track.effects:
                empty = QListWidgetItem("空机架")
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                self.fx_list.addItem(empty)
            for effect in track.effects:
                item = QListWidgetItem(("● " if effect.enabled else "○ ") + get_effect(effect.effect_id).name_zh)
                item.setData(Qt.ItemDataRole.UserRole, effect.instance_id)
                self.fx_list.addItem(item)

    def _add_effect_menu(self) -> None:
        menu = QMenu(self)
        groups: dict[str, QMenu] = {}
        track = self.session.track_by_id(self.track_id)
        if track is None:
            return
        for descriptor in EFFECT_CATALOG:
            if effect_scope(track) not in descriptor.scope:
                continue
            if descriptor.group not in groups:
                groups[descriptor.group] = menu.addMenu(descriptor.group)
            submenu = groups[descriptor.group]
            action = submenu.addAction(descriptor.name_zh)
            action.triggered.connect(lambda checked=False, effect_id=descriptor.effect_id: self.insert_effect(effect_id))
        menu.exec(self.insert_button.mapToGlobal(self.insert_button.rect().bottomLeft()))

    def insert_effect(self, effect_id: str) -> None:
        track = self.session.track_by_id(self.track_id)
        if track and effect_scope(track) in get_effect(effect_id).scope:
            instance = EffectInstance.create(effect_id, track.id)
            self.session.set_effects(track.id, [*track.effects, instance])
            owner = self.session.track_by_id(self.track_id)
            if owner:
                self.manager.open(owner.effects[-1], owner.name)

    def _open_effect(self, item: QListWidgetItem) -> None:
        track = self.session.track_by_id(self.track_id)
        if track:
            effect = next((fx for fx in track.effects if fx.instance_id == item.data(Qt.ItemDataRole.UserRole)), None)
            if effect:
                self.manager.open(effect, track.name)

    def _effect_menu(self, position: QPoint) -> None:
        item = self.fx_list.itemAt(position)
        track = self.session.track_by_id(self.track_id)
        if not item or not track:
            return
        index = next((i for i, fx in enumerate(track.effects) if fx.instance_id == item.data(Qt.ItemDataRole.UserRole)), -1)
        if index < 0:
            return
        effect = track.effects[index]
        menu = QMenu(self)
        menu.addAction("打开参数", lambda: self._open_effect(item))
        menu.addAction("旁路 / 启用", lambda: self._toggle_effect(effect))
        menu.addAction("上移", lambda: self._reorder(index, -1)).setEnabled(index > 0)
        menu.addAction("下移", lambda: self._reorder(index, 1)).setEnabled(index < len(track.effects) - 1)
        menu.addAction("移除", lambda: self._remove_effect(effect.instance_id))
        menu.exec(self.fx_list.viewport().mapToGlobal(position))

    def _toggle_effect(self, effect: EffectInstance) -> None:
        track = self.session.track_by_id(self.track_id)
        if track is None:
            return
        current = next((item for item in track.effects if item.instance_id == effect.instance_id), None)
        if current is not None:
            before = self.session.snapshot()
            self.manager.set_enabled(current, not current.enabled)
            self.session.commit_preview("切换效果旁路", before)

    def _reorder(self, index: int, delta: int) -> None:
        track = self.session.track_by_id(self.track_id)
        if track:
            items = list(track.effects)
            items[index], items[index + delta] = items[index + delta], items[index]
            self.session.set_effects(track.id, items)

    def _remove_effect(self, instance_id: str) -> None:
        track = self.session.track_by_id(self.track_id)
        if track:
            self.manager.close_instance(instance_id, track.id)
            self.session.set_effects(track.id, [fx for fx in track.effects if fx.instance_id != instance_id])

    def _track_menu(self, position: QPoint) -> None:
        track = self.session.track_by_id(self.track_id)
        if not track:
            return
        menu = QMenu(self)
        menu.addAction("重命名", self._rename)
        menu.addAction("重置推子为 0 dB", lambda: self.session.set_track_value(track.id, "volume_db", 0.0))
        menu.exec(self.name_button.mapToGlobal(position))

    def _edit_volume(self, text: str) -> None:
        try:
            value = float(text)
        except ValueError:
            self.sync()
            return
        if value != value:
            self.sync()
            return
        self.fader.setDb(max(-60.0, min(12.0, value)))

    def show_sections(self, sections: set[str]) -> None:
        for label, widget in self._section_panels.items():
            applicable = self.track_id != "master" or label not in ("前级", "发送")
            widget.setVisible(label in sections)
            widget.setEnabled(applicable)

    def _rename(self) -> None:
        track = self.session.track_by_id(self.track_id)
        if track:
            text, accepted = QInputDialog.getText(self, "重命名通道", "名称", text=track.name)
            if accepted and text.strip():
                self.session.set_track_value(track.id, "name", text.strip())


class ConsolePanel(QWidget):
    effectsRequested = pyqtSignal()

    def __init__(self, session: MixerUiSession, manager: EffectWindowManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session, self.manager = session, manager
        self.setObjectName("MixerConsole")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_QSS)
        self.strips: dict[str, ChannelStrip] = {}
        self._track_ids: tuple[str, ...] = ()
        root = QVBoxLayout(self)
        root.setContentsMargins(5, 4, 5, 4)
        root.setSpacing(4)
        toolbar_frame = QFrame()
        toolbar_frame.setObjectName("MixerToolbar")
        toolbar_frame.setFixedHeight(32)
        toolbar = QHBoxLayout(toolbar_frame)
        toolbar.setContentsMargins(4, 0, 4, 0)
        toolbar.setSpacing(4)
        self.sections = {"插入"}
        self.filter = compact_combo()
        self.filter.addItems(["所有通道", "DX", "FX", "MX", "BUS"])
        self.filter.currentIndexChanged.connect(self._filter)
        toolbar.addWidget(self.filter)
        toolbar.addWidget(toolbar_divider())
        toolbar.addWidget(tool_button("添加音轨", icon_add("#B6B9C2"), self._add_track))
        toolbar.addWidget(tool_button("添加总线", get_icon("fa5s.stream"), self._add_bus))
        toolbar.addWidget(tool_button("效果器库", icon_fx(), self.effectsRequested.emit))
        toolbar.addWidget(toolbar_divider())
        self.section_button = tool_button("显示通道分区", get_icon("fa5s.sliders-h"), self._section_menu)
        toolbar.addWidget(self.section_button)
        toolbar.addStretch()
        root.addWidget(toolbar_frame)
        bank = QHBoxLayout()
        bank.setSpacing(4)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content.setObjectName("MixerChannelBank")
        self.content.setStyleSheet("QWidget#MixerChannelBank { background: #222226; }")
        self.channels = QHBoxLayout(self.content)
        self.channels.setContentsMargins(0, 0, 0, 0)
        self.channels.setSpacing(0)
        self.channels.addStretch()
        self.scroll.setWidget(self.content)
        bank.addWidget(self.scroll, 1)
        self.master = ChannelStrip(session, "master", manager)
        bank.addWidget(self.master)
        root.addLayout(bank, 1)
        session.changed.connect(self.sync)
        session.selection_changed.connect(self.sync)
        self.sync()

    def sync(self, *_args: object) -> None:
        ids = tuple(track.id for track in self.session.tracks if track.id != "master")
        if ids != self._track_ids:
            self._track_ids = ids
            for strip in self.strips.values():
                self.channels.removeWidget(strip)
                strip.deleteLater()
            self.strips.clear()
            for index, track_id in enumerate(ids):
                strip = ChannelStrip(self.session, track_id, self.manager)
                self.strips[track_id] = strip
                self.channels.insertWidget(index, strip)
        for strip in (*self.strips.values(), self.master):
            strip.sync()
            strip.show_sections(self.sections)
        self._filter()

    def _filter(self, *_args: object) -> None:
        role = self.filter.currentText()
        for track_id, strip in self.strips.items():
            track = self.session.track_by_id(track_id)
            strip.setVisible(role == "所有通道" or (track is not None and track.role == role))

    def _section_menu(self) -> None:
        menu = QMenu(self)
        for label in ("前级", "插入", "发送"):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(label in self.sections)
            action.toggled.connect(lambda visible, item=label: self._set_section(item, visible))
        menu.exec(self.section_button.mapToGlobal(self.section_button.rect().bottomLeft()))

    def _set_section(self, label: str, visible: bool) -> None:
        if visible:
            self.sections.add(label)
        else:
            self.sections.discard(label)
        for strip in (*self.strips.values(), self.master):
            strip.show_sections(self.sections)

    def _add_track(self) -> None:
        name, accepted = QInputDialog.getText(self, "添加音频轨道", "名称", text="新音频轨道")
        if accepted and name.strip():
            self.session.add_track(name.strip(), "FX")

    def _add_bus(self) -> None:
        name, accepted = QInputDialog.getText(self, "添加总线", "名称", text="新 Bus")
        if accepted and name.strip():
            self.session.add_track(name.strip(), "BUS")
