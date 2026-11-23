"""Native plugin discovery and quick launch views; snapshots and intents only."""
from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtCore import QEvent, QSize, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFormLayout, QHBoxLayout, QKeySequenceEdit,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea,
    QSplitter, QVBoxLayout, QWidget,
)

from src.features.files.plugin_contracts import FilesPluginInfo
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


def _tr(key: str, default: str) -> str:
    return I18N.tr(f"files.plugins.{key}", default)


def plugin_status(info: FilesPluginInfo) -> str:
    if info.error:
        return _tr("failed", "加载失败")
    if info.needs_restart:
        return _tr("pending_disable", "重启后停用") if not info.desired_enabled else _tr("pending_enable", "重启后启用")
    if not info.desired_enabled:
        return _tr("disabled", "已停用")
    return _tr("loaded", "已加载") if info.runtime_loaded else _tr("unloaded", "尚未加载")


def plugin_name(info: FilesPluginInfo) -> str:
    aliases = {"format_factory": "格式工厂", "image_factory": "图片工厂", "shutdown_timer": "定时关机"}
    return _tr(f"name.{info.plugin_id}", aliases.get(info.plugin_id, info.name)) if info.name == info.plugin_id else info.name


def plugin_description(info: FilesPluginInfo) -> str:
    return info.description or " · ".join(info.capabilities)


def _label(text: str, *, title: bool = False) -> QLabel:
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if title:
        result.setObjectName("pluginTitle")
    return result


def _style(widget: QWidget) -> None:
    widget.setStyleSheet(f"""
        QDialog {{ background: {C.BG_DARKEST}; color: {C.TEXT_PRIMARY}; }}
        QLabel {{ color: {C.TEXT_SECONDARY}; background: transparent; }}
        QLabel#pluginTitle {{ color: {C.TEXT_PRIMARY}; font-size: 22px; font-weight: 600; }}
        QLabel#pluginStatus {{ color: {C.ORANGE}; font-size: 12px; }}
        QLineEdit, QKeySequenceEdit {{ color: {C.TEXT_PRIMARY}; background: {C.BG_INPUT};
            border: 1px solid {C.BORDER}; border-radius: 6px; padding: 7px; }}
        QLineEdit:focus, QKeySequenceEdit:focus {{ border-color: {C.ORANGE}; }}
        QListWidget {{ background: transparent; color: {C.TEXT_PRIMARY}; border: none; outline: none; }}
        QListWidget::item {{ padding: 9px; border-radius: 5px; }}
        QListWidget::item:selected {{ background: {C.BG_ACTIVE}; color: {C.ORANGE}; }}
        QListWidget::item:hover {{ background: {C.BG_HOVER}; }}
        QPushButton {{ color: {C.TEXT_PRIMARY}; background: {C.BG_ACTIVE}; border: none;
            border-radius: 5px; padding: 8px 12px; }}
        QPushButton:hover {{ background: {C.BG_HOVER}; }}
        QPushButton:disabled {{ color: {C.TEXT_HINT}; }}
        QPushButton#pluginPrimary {{ background: {C.ORANGE_PRESS}; color: white; }}
        QCheckBox {{ color: {C.TEXT_SECONDARY}; spacing: 8px; }}
        QScrollArea {{ background: transparent; border: none; }}
        QWidget#pluginDetails {{ background: {C.BG_DARK}; border-radius: 8px; }}
        QSplitter::handle {{ background: {C.PANEL_DIVIDER}; }}
    """)


class PluginCenterDialog(QDialog):
    refresh_requested = pyqtSignal()
    directory_requested = pyqtSignal()
    launch_requested = pyqtSignal(str)
    pin_requested = pyqtSignal(str)
    enabled_requested = pyqtSignal(str, bool)
    shortcut_requested = pyqtSignal(str, str)
    legacy_requested = pyqtSignal()
    install_requested = pyqtSignal()
    uninstall_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_tr("center", "素材插件中心"))
        self.resize(1060, 680)
        self.setMinimumSize(820, 520)
        self.infos: tuple[FilesPluginInfo, ...] = ()
        self.pins: list[str] = []
        self.recent: list[str] = []
        self.shortcuts: dict[str, str] = {}
        self.busy: set[str] = set()
        self._selected = ""
        _style(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(_label(_tr("center", "素材插件中心"), title=True))
        header.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText(_tr("search", "搜索名称、功能或作者…"))
        self.search.setAccessibleName(_tr("search", "搜索插件"))
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(250)
        self.search.textChanged.connect(self._filter)
        header.addWidget(self.search)
        root.addLayout(header)
        self.notice = _label(_tr("scope", "管理已安装的素材工具；启用或停用将在重启后生效。"))
        root.addWidget(self.notice)
        body = QHBoxLayout()
        self.categories = QListWidget()
        self.categories.setFixedWidth(142)
        self.categories.setAccessibleName(_tr("categories", "插件分类"))
        for key, text in (("all", "全部插件"), ("pinned", "已固定"), ("recent", "最近使用"),
                          ("native", "原生工具"), ("legacy", "兼容扩展"),
                          ("disabled", "已停用"), ("failed", "加载失败")):
            item = QListWidgetItem(_tr(f"filter.{key}", text))
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.categories.addItem(item)
        self.categories.setCurrentRow(0)
        self.categories.currentRowChanged.connect(self._filter)
        body.addWidget(self.categories)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.items = QListWidget()
        self.items.setMinimumWidth(230)
        self.items.setAccessibleName(_tr("installed", "已安装插件"))
        self.items.currentItemChanged.connect(self._select)
        self.items.itemDoubleClicked.connect(lambda _: self._launch())
        split.addWidget(self.items)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.details = QWidget()
        self.details.setObjectName("pluginDetails")
        details = QVBoxLayout(self.details)
        details.setContentsMargins(20, 20, 20, 20)
        details.setSpacing(14)
        self.title = _label("", title=True)
        self.status = _label("")
        self.status.setObjectName("pluginStatus")
        self.description = _label("")
        self.context = _label("")
        for widget in (self.title, self.status, self.description, self.context):
            details.addWidget(widget)
        actions = QHBoxLayout()
        self.launch = QPushButton(_tr("open", "打开工具"))
        self.launch.setObjectName("pluginPrimary")
        self.launch.clicked.connect(self._launch)
        self.pin = QPushButton()
        self.pin.clicked.connect(lambda: self.pin_requested.emit(self._selected))
        actions.addWidget(self.launch)
        actions.addWidget(self.pin)
        self.uninstall = QPushButton("卸载插件")
        self.uninstall.clicked.connect(lambda: self.uninstall_requested.emit(self._selected))
        actions.addWidget(self.uninstall)
        actions.addStretch()
        details.addLayout(actions)
        self.reason = _label("")
        details.addWidget(self.reason)
        self.legacy = QPushButton(_tr("capability_details", "查看素材工具"))
        self.legacy.clicked.connect(self.legacy_requested)
        details.addWidget(self.legacy)
        self.enabled = QCheckBox(_tr("enabled", "启用插件"))
        self.enabled.toggled.connect(lambda checked: self.enabled_requested.emit(self._selected, checked))
        details.addWidget(self.enabled)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.version = _label("")
        self.author = _label("")
        self.features = _label("")
        self.runtime = _label("")
        self.origin = _label("")
        for key, text, widget in (("version", "版本", self.version), ("author", "作者", self.author),
                                  ("features", "功能", self.features), ("runtime", "当前会话", self.runtime),
                                  ("origin", "来源", self.origin)):
            form.addRow(_tr(key, text), widget)
        self.shortcut = QKeySequenceEdit()
        self.shortcut.setMaximumSequenceLength(1)
        self.shortcut.setAccessibleName(_tr("shortcut", "Files 内快捷键"))
        form.addRow(_tr("shortcut", "Files 内快捷键"), self.shortcut)
        details.addLayout(form)
        shortcut_actions = QHBoxLayout()
        self.save_shortcut = QPushButton(_tr("save_shortcut", "保存快捷键"))
        self.save_shortcut.clicked.connect(lambda: self.shortcut_requested.emit(
            self._selected, self.shortcut.keySequence().toString(QKeySequence.SequenceFormat.PortableText)))
        self.clear_shortcut = QPushButton(_tr("clear_shortcut", "清除快捷键"))
        self.clear_shortcut.clicked.connect(lambda: self.shortcut_requested.emit(self._selected, ""))
        shortcut_actions.addWidget(self.save_shortcut)
        shortcut_actions.addWidget(self.clear_shortcut)
        shortcut_actions.addStretch()
        details.addLayout(shortcut_actions)
        self.diagnostics = _label("")
        details.addWidget(_label("内建工具由应用托管；第三方插件执行需要经过已验证的隔离管线。"))
        details.addWidget(self.diagnostics)
        self.copy = QPushButton(_tr("copy_diagnostics", "复制诊断信息"))
        self.copy.clicked.connect(lambda: QApplication.clipboard().setText(
            f"{self.title.text()}\n{self.status.text()}\n{self.diagnostics.text()}"))
        details.addWidget(self.copy)
        details.addStretch()
        scroll.setWidget(self.details)
        split.addWidget(scroll)
        split.setSizes([300, 450])
        body.addWidget(split, 1)
        root.addLayout(body, 1)
        footer = QHBoxLayout()
        self.count = _label("")
        footer.addWidget(self.count)
        footer.addStretch()
        self.install = QPushButton("安装本地插件包…")
        self.install.clicked.connect(self.install_requested)
        footer.addWidget(self.install)
        for text, signal in (("打开插件目录", self.directory_requested), ("刷新清单", self.refresh_requested)):
            button = QPushButton(_tr("directory" if text.startswith("打开") else "refresh", text))
            button.clicked.connect(signal)
            footer.addWidget(button)
        close = QPushButton(_tr("close", "完成"))
        close.clicked.connect(self.close)
        footer.addWidget(close)
        root.addLayout(footer)

    def set_snapshot(self, infos: Sequence[FilesPluginInfo], pins: list[str], recent: list[str],
                     shortcuts: dict[str, str], busy: set[str]) -> None:
        self.infos = tuple(infos)
        self.pins, self.recent, self.shortcuts, self.busy = pins, recent, shortcuts, set(busy)
        # Keep functional categories separate from installation/runtime state.
        selected = self.categories.currentItem().data(Qt.ItemDataRole.UserRole)
        with QSignalBlocker(self.categories):
            while self.categories.count() > 7:
                self.categories.takeItem(7)
            for category in sorted({info.category for info in self.infos}):
                item = QListWidgetItem(category)
                item.setData(Qt.ItemDataRole.UserRole, f"category:{category}")
                self.categories.addItem(item)
            for row in range(self.categories.count()):
                if self.categories.item(row).data(Qt.ItemDataRole.UserRole) == selected:
                    self.categories.setCurrentRow(row)
                    break
        self._filter()

    def _filter(self, *_: object) -> None:
        query = self.search.text().strip().casefold()
        category_item = self.categories.currentItem()
        category = category_item.data(Qt.ItemDataRole.UserRole) if category_item else "all"
        visible = []
        for info in self.infos:
            matches = {"all": True, "pinned": info.plugin_id in self.pins,
                       "recent": info.plugin_id in self.recent, "native": bool(info.launch_path),
                       "legacy": not info.launch_path, "disabled": not info.desired_enabled,
                       "failed": bool(info.error)}
            if not matches.get(category, True):
                continue
            if str(category).startswith("category:") and info.category != category.partition(":")[2]:
                continue
            if query and query not in " ".join((plugin_name(info), info.name, info.plugin_id, info.author,
                                                  info.description, info.category, *info.capabilities)).casefold():
                continue
            visible.append(info)
        if category == "recent":
            visible.sort(key=lambda info: self.recent.index(info.plugin_id))
        selected = self._selected
        with QSignalBlocker(self.items):
            self.items.clear()
            for info in visible:
                prefix = "★ " if info.plugin_id in self.pins else ""
                item = QListWidgetItem(f"{prefix}{plugin_name(info)}\n{plugin_status(info)} · {info.category}")
                item.setData(Qt.ItemDataRole.UserRole, info.plugin_id)
                item.setToolTip(info.description)
                item.setSizeHint(QSize(0, 62))
                self.items.addItem(item)
            row = next((i for i, info in enumerate(visible) if info.plugin_id == selected), 0)
            self.items.setCurrentRow(row if visible else -1)
        self.count.setText(_tr("count", "{shown} / {total} 个插件").format(shown=len(visible), total=len(self.infos)))
        self._select()

    def _select(self, *_: object) -> None:
        item = self.items.currentItem()
        self._selected = str(item.data(Qt.ItemDataRole.UserRole)) if item else ""
        info = next((info for info in self.infos if info.plugin_id == self._selected), None)
        self.details.setVisible(info is not None)
        if info is None:
            return
        self.title.setText(plugin_name(info))
        self.status.setText(plugin_status(info))
        self.description.setText(plugin_description(info) or _tr("no_description", "作者尚未提供介绍。"))
        self.launch.setEnabled(info.can_launch and info.plugin_id not in self.busy)
        self.launch.setText(_tr("opening", "正在请求…") if info.plugin_id in self.busy else _tr("open", "打开工具"))
        self.pin.setText(_tr("unpin", "取消固定") if info.plugin_id in self.pins else _tr("pin", "固定到启动器"))
        self.reason.setText(info.launch_unavailable_reason if not info.can_launch else "")
        self.reason.setVisible(bool(self.reason.text()))
        self.legacy.setVisible(False)
        # [F1] 声明/运行版本分槽：宿主上报的运行版本与声明不一致时并列展示
        version_text = f"{info.version} · API {info.api_version}"
        runtime_version = getattr(info, "runtime_version", "")
        if runtime_version and runtime_version != info.version:
            version_text += f"{_tr('runtime_version', ' · 运行 {version}').format(version=runtime_version)}"
        self.version.setText(version_text)
        self.author.setText(info.author)
        self.features.setText(" · ".join(info.capabilities) or info.category)
        if info.source == "local_app":
            self.runtime.setText(_tr("app_running", "独立应用运行中；停用后窗口可继续单独使用")
                                 if info.runtime_loaded else _tr("app_stopped", "独立应用未连接"))
        else:
            self.runtime.setText(_tr("session_loaded", "已加载；停用需要重启") if info.runtime_loaded else _tr("session_unloaded", "未加载"))
        # [F1] 来源政策 + 安装事实直达 UI（未验证开发源显式标注）
        source = getattr(info, "source", "builtin")
        self.uninstall.setVisible(source == "installed")
        self.uninstall.setEnabled(info.plugin_id not in self.busy)
        install_state = getattr(info, "install_state", "installed")
        state_labels = {"installed": _tr("state_installed", "已安装"),
                        "discovered": _tr("state_discovered", "已发现"),
                        "missing": _tr("state_missing", "已缺失")}
        from src.shared.plugins.discovery import source_label
        origin_text = source_label(source)
        origin_text += f" · {state_labels.get(install_state, install_state)}"
        if source == "local_dev":
            origin_text += _tr("unverified", " · 未验证")
        self.origin.setText(origin_text)
        with QSignalBlocker(self.enabled):
            self.enabled.setChecked(info.desired_enabled)
        self.enabled.setEnabled(not info.self_disabled)
        with QSignalBlocker(self.shortcut):
            self.shortcut.setKeySequence(QKeySequence(self.shortcuts.get(info.plugin_id, "")))
        for widget in (self.shortcut, self.save_shortcut, self.clear_shortcut):
            widget.setEnabled(bool(info.launch_path))
        notes = [str(info.path), *info.diagnostics]
        if info.error:
            notes.append(info.error)
        self.diagnostics.setText("\n".join(notes))

    def _launch(self) -> None:
        if self._selected and self.launch.isEnabled():
            self.launch_requested.emit(self._selected)

    def set_context(self, count: int) -> None:
        self.context.setText(_tr("selection", "当前选中 {count} 个文件；打开时传入当前选择。").format(count=count))

    def select_plugin(self, plugin_id: str) -> None:
        self._selected = plugin_id
        with QSignalBlocker(self.search), QSignalBlocker(self.categories):
            self.search.clear()
            self.categories.setCurrentRow(0)
        self._filter()


class PluginLauncherDialog(QDialog):
    launch_requested = pyqtSignal(str)
    center_requested = pyqtSignal()
    pin_requested = pyqtSignal(str)
    details_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.resize(460, 460)
        self.setMinimumWidth(380)
        self.infos: tuple[FilesPluginInfo, ...] = ()
        self.pins: list[str] = []
        self.recent: list[str] = []
        self.shortcuts: dict[str, str] = {}
        self.busy: set[str] = set()
        _style(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        self.search = QLineEdit()
        self.search.setPlaceholderText(_tr("launcher_search", "查找素材工具…"))
        self.search.setAccessibleName(_tr("launcher_search", "查找素材工具"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        self.search.returnPressed.connect(self._launch)
        layout.addWidget(self.search)
        self.context = _label("")
        layout.addWidget(self.context)
        self.items = QListWidget()
        self.items.setAccessibleName(_tr("launcher", "插件快捷启动器"))
        self.items.itemActivated.connect(lambda _: self._launch())
        self.items.itemClicked.connect(lambda _: self._launch())
        self.items.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.items.customContextMenuRequested.connect(self._menu)
        layout.addWidget(self.items, 1)
        self.empty = _label(_tr("no_tools", "没有匹配的可用工具；可在插件中心检查状态。"))
        layout.addWidget(self.empty)
        center = QPushButton(_tr("center_more", "插件中心 · 管理全部扩展…"))
        center.clicked.connect(self._center)
        layout.addWidget(center)

    def set_snapshot(self, infos: Sequence[FilesPluginInfo], pins: list[str], recent: list[str],
                     shortcuts: dict[str, str], busy: set[str]) -> None:
        self.infos = tuple(infos)
        self.pins, self.recent, self.shortcuts, self.busy = pins, recent, shortcuts, set(busy)
        self._filter()

    def _filter(self, *_: object) -> None:
        query = self.search.text().strip().casefold()
        eligible = [info for info in self.infos if info.can_launch]
        def order(info: FilesPluginInfo) -> tuple[int, int, str]:
            pid = info.plugin_id
            return (0, self.pins.index(pid), plugin_name(info)) if pid in self.pins else (
                (1, self.recent.index(pid), plugin_name(info)) if pid in self.recent else (2, 0, plugin_name(info)))
        self.items.clear()
        for info in sorted(eligible, key=order):
            if query and query not in " ".join((plugin_name(info), info.name, info.description, info.plugin_id, *info.capabilities)).casefold():
                continue
            prefix = "★ " if info.plugin_id in self.pins else ""
            shortcut = self.shortcuts.get(info.plugin_id, "")
            item = QListWidgetItem(f"{prefix}{plugin_name(info)}" + (f"    {shortcut}" if shortcut else "") + f"\n{plugin_description(info)}")
            item.setSizeHint(QSize(0, 60))
            item.setToolTip(info.description)
            item.setData(Qt.ItemDataRole.UserRole, info.plugin_id)
            if info.plugin_id in self.busy:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.items.addItem(item)
        self.items.setCurrentRow(0)
        self.empty.setVisible(self.items.count() == 0)

    def _launch(self) -> None:
        item = self.items.currentItem()
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.launch_requested.emit(str(item.data(Qt.ItemDataRole.UserRole)))

    def _center(self) -> None:
        self.hide()
        self.center_requested.emit()

    def _menu(self, pos: object) -> None:
        item = self.items.itemAt(pos)
        if item is None:
            return
        plugin_id = str(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        menu.addAction(_tr("unpin", "取消固定") if plugin_id in self.pins else _tr("pin", "固定到启动器"),
                       lambda: self.pin_requested.emit(plugin_id))
        menu.addAction(_tr("details", "详情与快捷键…"), lambda: self.details_requested.emit(plugin_id))
        menu.exec(self.items.viewport().mapToGlobal(pos))

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                delta = 1 if event.key() == Qt.Key.Key_Down else -1
                self.items.setCurrentRow(max(0, min(self.items.count() - 1, self.items.currentRow() + delta)))
                return True
        return super().eventFilter(watched, event)
