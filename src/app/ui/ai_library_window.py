"""Native, bounded read-only Vault search; no legacy web host."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from PyQt6.QtCore import QObject, QRunnable, QSize, QSignalBlocker, QThreadPool, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)
import qtawesome as qta

from src.app.ui.add_library_dialog import AddLibraryDialog
from src.app.ui.library_folder_picker import LibraryFolderPicker
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.vault.client import VaultClient

if TYPE_CHECKING:
    from src.app.ui.native_files_transport import NativeFilesTransport


class _Signals(QObject):
    done = pyqtSignal(int, object, str)


class _Search(QRunnable):
    def __init__(self, generation: int, client: VaultClient, text: str) -> None:
        super().__init__()
        self.generation, self.client, self.text = generation, client, text
        self.signals = _Signals()

    def run(self) -> None:
        try:
            if not self.client.is_available():
                self.signals.done.emit(self.generation, (), "素材库查询服务离线；请启动 topos-vault serve。")
                return
            files = self.client.search(self.text, limit=100)
            stats = self.client.get_stats()
            self.signals.done.emit(self.generation, files,
                                  f"找到 {len(files)} 项 · 素材库 {stats.total if stats else '未知'} 项")
        except (OSError, ValueError, RuntimeError) as exc:
            self.signals.done.emit(self.generation, (), f"查询失败：{exc}")


class AILibraryWindow(QDialog):
    closed = pyqtSignal()
    path_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None, *, client: VaultClient | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 素材库")
        self.resize(720, 500)
        self._client = client or VaultClient(timeout=1.5)
        self._generation = 0
        self._closed = False
        self._task: _Search | None = None
        root = QVBoxLayout(self)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索素材库；查询服务和模型缺失时会显示原因")
        self.go = QPushButton("搜索")
        row.addWidget(self.search, 1)
        row.addWidget(self.go)
        root.addLayout(row)
        self.notice = QLabel("输入关键词搜索")
        root.addWidget(self.notice)
        self.results = QListWidget()
        root.addWidget(self.results, 1)
        self.go.clicked.connect(self.query)
        self.search.returnPressed.connect(self.query)
        self.results.itemActivated.connect(lambda item: self.path_requested.emit(str(item.data(Qt.ItemDataRole.UserRole))))

    def query(self) -> None:
        if self._task is not None:
            return  # one bounded request at a time
        self._closed = False
        self._generation += 1
        self.results.clear()
        self.notice.setText("正在查询…")
        self.go.setEnabled(False)
        task = _Search(self._generation, self._client, self.search.text())
        self._task = task
        task.signals.done.connect(self._finished)
        QThreadPool.globalInstance().start(task)

    def _finished(self, generation: int, files: Any, message: str) -> None:
        self._task = None
        if self._closed or generation != self._generation:
            return
        self.go.setEnabled(True)
        self.notice.setText(message)
        from PyQt6.QtWidgets import QListWidgetItem
        for file in files:
            if not isinstance(file, dict):
                continue
            path = file.get("path", "")
            if not isinstance(path, str) or not path:
                continue
            item = QListWidgetItem(str(file.get("name") or path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.results.addItem(item)

    def closeEvent(self, event: Any) -> None:
        self._closed = True
        self._generation += 1
        self.closed.emit()
        super().closeEvent(event)

    def showEvent(self, event: Any) -> None:
        self._closed = False
        self.go.setEnabled(self._task is None)
        super().showEvent(event)


def _tr(key: str, default: str, **values: Any) -> str:
    return I18N.tr(f"files.ai_library.{key}", default, **values)


def _format_seconds(seconds: float) -> str:
    minutes, second = divmod(seconds, 60)
    return f"{int(minutes):02d}:{second:05.2f}"


class RegisteredAILibraryWindow(QDialog):
    """Browse the Files catalog's persistent AI index across projects."""

    import_requested = pyqtSignal(dict)
    configure_models_requested = pyqtSignal(str)
    _MEDIA_LABELS = (
        ("image", "file_type_image", "图片"),
        ("video", "file_type_video", "视频（抽帧）"),
        ("audio", "file_type_audio", "音频（转录）"),
    )

    def __init__(self, transport: NativeFilesTransport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._transport = transport
        self._request_id = ""
        self._query = ""
        self._copying = False
        self._index_running = False
        self._saving_policy = False
        self._loading_scope = False
        self._scope_error = False
        self._restore_draft: dict[str, Any] | None = None
        self._pending_policy: dict[str, Any] = {}
        self._pending_policy_scope: tuple[str, str | None] | None = None
        self._scan_after_save: tuple[str, str | None, bool, bool] | None = None
        self._library_scope: str | None = None
        self._folder_scope: str | None = None
        self._policies: dict[str, dict[str, Any]] = {}
        self._scope_policy: dict[str, Any] = {}
        self._scope_stamp: tuple[str, str] | None = None
        self._embedding_route: dict[str, Any] = {}
        self._embedding_consent: str | None = None
        self._models: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind, _, _ in self._MEDIA_LABELS}
        self.setWindowTitle(_tr("title", "AI 素材库"))
        self.setObjectName("AiLibrary")
        self.resize(860, 650)
        self.setMinimumWidth(660)
        self.setStyleSheet(f"""
            QDialog#AiLibrary {{ background-color: {C.BG_PANEL}; }}
            QLabel#AiLibraryNote {{ color: {C.TEXT_LABEL}; }}
            QLabel#AiLibraryTitle {{ color: {C.TEXT_PRIMARY}; font-size: 14px; font-weight: 600; }}
            QFrame#AiLibrarySettings {{
                background: {C.BG_DARK}; border: 1px solid {C.BORDER}; border-radius: 6px;
            }}
            QFrame#AiLibrarySettings QLabel, QFrame#AiLibrarySettings QCheckBox {{
                background: transparent;
            }}
            QFrame#AiLibraryRule {{ background: {C.TOGGLE_PILL_BG}; border: none; }}
            QPushButton#AiLibraryPrimary {{
                background: {C.ACCENT_SURFACE}; color: white;
                border: 1px solid transparent; border-radius: 5px; padding: 5px 13px;
            }}
            QPushButton#AiLibraryPrimary:hover {{ background: {C.ORANGE_HOVER}; }}
            QPushButton#AiLibraryPrimary:focus {{ border-color: {C.ORANGE_HOVER}; }}
            QPushButton#AiLibraryPrimary:disabled {{
                background: {C.BG_BUTTON}; color: {C.TEXT_HINT}; border-color: {C.BORDER};
            }}
            QListWidget#registeredLibraryResults, QPlainTextEdit#AiLibraryDetails {{
                background: transparent; border: none; outline: none;
            }}
            QLineEdit#AiLibrarySourcePath {{
                background: transparent; color: {C.TEXT_LABEL}; border-color: transparent;
                padding-left: 0;
            }}
            QLineEdit#AiLibrarySourcePath:focus {{ border-bottom-color: {C.ORANGE}; }}
            QListWidget#registeredLibraryResults::item {{
                padding: 7px 8px; border-left: 2px solid transparent; border-radius: 4px;
            }}
            QListWidget#registeredLibraryResults::item:hover {{ background: {C.BG_HOVER}; }}
            QListWidget#registeredLibraryResults::item:selected {{
                background: {C.BG_ACTIVE}; color: {C.TEXT_PRIMARY}; border-left-color: {C.ORANGE};
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        actions = QHBoxLayout()
        scope = QLabel(_tr("scope_label", "范围"), self)
        self.analysis_directory = ChevronComboBox(self)
        self.analysis_directory.setAccessibleName(_tr("library_scope", "素材库范围"))
        self.analysis_directory.addItem(_tr("all_libraries", "全部素材库"), None)
        self.analysis_directory.currentIndexChanged.connect(self._scope_changed)
        scope.setBuddy(self.analysis_directory)
        actions.addWidget(scope)
        actions.addWidget(self.analysis_directory, 1)
        self.folder_button = QPushButton(self)
        self.folder_button.setIcon(qta.icon("fa5s.folder-open", color=C.TEXT_SECONDARY))
        self.folder_button.clicked.connect(self._choose_scope_folder)
        actions.addWidget(self.folder_button)
        self.add_button = QPushButton(_tr("add_library", "添加库…"), self)
        self.add_button.clicked.connect(self._reference_directory)
        actions.addWidget(self.add_button)
        self.copy_button = QPushButton(_tr("import_media", "导入素材…"), self)
        self.copy_button.setToolTip(_tr("copy_into_library", "复制素材到库…"))
        self.copy_button.clicked.connect(self._copy_files)
        actions.addWidget(self.copy_button)
        self.analysis_settings = QPushButton(_tr("analysis_settings", "分析设置"), self)
        self.analysis_settings.setCheckable(True)
        self.analysis_settings.setIcon(qta.icon("fa5s.chevron-down", color=C.TEXT_LABEL))
        self.analysis_settings.toggled.connect(self._toggle_settings)
        actions.addWidget(self.analysis_settings)
        self.index_button = QPushButton(_tr("analyze", "扫描并分析"), self)
        self.index_button.setObjectName("AiLibraryPrimary")
        self.index_button.clicked.connect(self._start_index)
        self.cancel_button = QPushButton(_tr("stop", "停止分析"), self)
        self.cancel_button.clicked.connect(transport.stop_ai_library_index)
        self.analysis_action = QStackedWidget(self)
        self.analysis_action.addWidget(self.index_button)
        self.analysis_action.addWidget(self.cancel_button)
        actions.addWidget(self.analysis_action)
        root.addLayout(actions)

        policy = QFrame(self)
        policy.setObjectName("AiLibrarySettings")
        self._settings_panel = policy
        policy_layout = QVBoxLayout(policy)
        policy_layout.setContentsMargins(12, 9, 12, 9)
        policy_layout.setSpacing(8)
        policy_row = QHBoxLayout()
        mode_label = QLabel(_tr("mode_label", "分析方式"), policy)
        self.analysis_mode_label = mode_label
        self.analysis_mode = ChevronComboBox(policy)
        self.analysis_mode.setAccessibleName(_tr("analysis_mode", "目录分析模式"))
        self.analysis_mode.addItem(_tr("analysis_by_type", "按类型选择模型"), "profiles")
        self.analysis_mode.addItem(_tr("analysis_basic", "基础索引（本地 CPU）"), "basic")
        self.analysis_mode.currentIndexChanged.connect(self._analysis_options_changed)
        mode_label.setBuddy(self.analysis_mode)
        policy_row.addWidget(mode_label)
        policy_row.addWidget(self.analysis_mode, 1)
        self.configure_models_button = QPushButton(_tr("configure_models", "模型配置…"), policy)
        self.configure_models_button.setIcon(qta.icon("fa5s.cog", color=C.TEXT_SECONDARY))
        self.configure_models_button.clicked.connect(lambda: self.configure_models_requested.emit("vision"))
        policy_row.addWidget(self.configure_models_button, 0, Qt.AlignmentFlag.AlignRight)
        policy_layout.addLayout(policy_row)
        types = QGridLayout()
        types.setHorizontalSpacing(12)
        types.setVerticalSpacing(4)
        self.type_checks: dict[str, QCheckBox] = {}
        self.model_pickers: dict[str, ChevronComboBox] = {}
        for column, (kind, key, label) in enumerate(self._MEDIA_LABELS):
            checkbox = QCheckBox(_tr(key, label), policy)
            checkbox.setChecked(True)
            checkbox.toggled.connect(self._filters_changed)
            picker = ChevronComboBox(policy)
            picker.setAccessibleName(checkbox.text())
            picker.currentIndexChanged.connect(self._analysis_options_changed)
            self.type_checks[kind] = checkbox
            self.model_pickers[kind] = picker
            types.addWidget(checkbox, 0, column)
            types.addWidget(picker, 1, column)
            types.setColumnStretch(column, 1)
        policy_layout.addLayout(types)
        scan_options = QHBoxLayout()
        self.recursive_check = QCheckBox(_tr("include_subfolders", "包含子文件夹"), policy)
        self.recursive_check.setChecked(True)
        self.recursive_check.toggled.connect(self._filters_changed)
        scan_options.addWidget(self.recursive_check)
        self.force_check = QCheckBox(_tr("reanalyze", "重新分析已索引项"), policy)
        self.force_check.setToolTip(_tr("reanalyze_hint", "重新调用选定模型。关闭时复用已有分析结果。"))
        scan_options.addWidget(self.force_check)
        scan_options.addStretch()
        self.save_policy_button = QPushButton(_tr("save_scope_settings", "保存范围设置"), policy)
        self.save_policy_button.clicked.connect(self._save_directory_policy)
        scan_options.addWidget(self.save_policy_button)
        policy_layout.addLayout(scan_options)
        vector_options = QHBoxLayout()
        self.vector_status = QLabel(policy)
        self.vector_status.setObjectName("AiLibraryNote")
        self.vector_status.setTextFormat(Qt.TextFormat.PlainText)
        self.vector_status.setWordWrap(True)
        vector_options.addWidget(self.vector_status, 1)
        self.vector_model_button = QPushButton(_tr("vector_model", "向量模型…"), policy)
        self.vector_model_button.clicked.connect(lambda: self.configure_models_requested.emit("embedding"))
        vector_options.addWidget(self.vector_model_button)
        self.revoke_vector_button = QPushButton(_tr("revoke_vector_access", "撤销云端授权"), policy)
        self.revoke_vector_button.hide()
        self.revoke_vector_button.clicked.connect(self._revoke_vector_access)
        vector_options.addWidget(self.revoke_vector_button)
        self.vector_button = QPushButton(_tr("update_vectors", "更新向量索引"), policy)
        self.vector_button.setToolTip(_tr("update_vectors_hint", "使用已保存的描述、标签和转录。模型更换后可在这里补建索引。"))
        self.vector_button.clicked.connect(self._start_vectors)
        vector_options.addWidget(self.vector_button)
        policy_layout.addLayout(vector_options)
        self.policy_status = QLabel(policy)
        self.policy_status.setObjectName("AiLibraryNote")
        self.policy_status.setTextFormat(Qt.TextFormat.PlainText)
        self.policy_status.setWordWrap(True)
        policy_layout.addWidget(self.policy_status)
        root.addWidget(policy)
        self.analysis_settings.setChecked(True)

        self.index_status = QLabel(self)
        self.index_status.setObjectName("AiLibraryNote")
        self.index_status.setTextFormat(Qt.TextFormat.PlainText)
        self.index_status.setWordWrap(True)
        root.addWidget(self.index_status)
        self.analysis_progress = QProgressBar(self)
        self.analysis_progress.setTextVisible(False)
        self.analysis_progress.setFixedHeight(5)
        self.analysis_progress.hide()
        root.addWidget(self.analysis_progress)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(_tr("placeholder", "输入脚本片段、标签或素材描述"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.addAction(qta.icon("fa5s.search", color=C.TEXT_LABEL), QLineEdit.ActionPosition.LeadingPosition)
        self.search_edit.returnPressed.connect(self.search)
        search_row.addWidget(self.search_edit, 1)
        self.search_mode = ChevronComboBox(self)
        self.search_mode.setAccessibleName(_tr("search_mode", "检索方式"))
        self.search_mode.addItem(_tr("hybrid_search", "混合检索"), True)
        self.search_mode.addItem(_tr("keyword_search", "关键词"), False)
        self.search_mode.currentIndexChanged.connect(self.search)
        search_row.addWidget(self.search_mode)
        self.pending_only_check = QCheckBox(_tr("pending_only", "只看待分析"), self)
        self.pending_only_check.setToolTip(_tr("pending_only_hint", "显示待分析素材，扫描时跳过已有有效 AI 结果的文件。"))
        self.pending_only_check.toggled.connect(self._pending_filter_changed)
        search_row.addWidget(self.pending_only_check)
        search_button = QPushButton(_tr("search", "搜索"), self)
        search_button.clicked.connect(self.search)
        search_row.addWidget(search_button)
        root.addLayout(search_row)

        divider = QFrame(self)
        divider.setObjectName("AiLibraryRule")
        divider.setFixedHeight(1)
        root.addWidget(divider)

        self.results_stack = QStackedWidget(self)
        empty = QWidget(self.results_stack)
        empty_layout = QVBoxLayout(empty)
        empty_layout.setSpacing(7)
        empty_layout.addStretch()
        empty_icon = QLabel(empty)
        empty_icon.setPixmap(qta.icon("fa5s.photo-video", color=C.ORANGE).pixmap(QSize(34, 34)))
        empty_layout.addWidget(empty_icon, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.empty_title = QLabel(empty)
        self.empty_title.setObjectName("AiLibraryTitle")
        empty_layout.addWidget(self.empty_title, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.empty_hint = QLabel(empty)
        self.empty_hint.setObjectName("AiLibraryNote")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(self.empty_hint)
        empty_layout.addStretch()
        self.results_stack.addWidget(empty)

        content = QWidget(self.results_stack)
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)
        self.results = QListWidget(self)
        self.results.setObjectName("registeredLibraryResults")
        self.results.currentItemChanged.connect(self._selection_changed)
        self.results.itemActivated.connect(self._import_current)
        content_layout.addWidget(self.results, 3)
        between = QFrame(content)
        between.setObjectName("AiLibraryRule")
        between.setFixedWidth(1)
        content_layout.addWidget(between)
        inspector = QVBoxLayout()
        self.detail_title = QLabel(content)
        self.detail_title.setObjectName("AiLibraryTitle")
        self.detail_title.setWordWrap(True)
        self.detail_title.setTextFormat(Qt.TextFormat.PlainText)
        inspector.addWidget(self.detail_title)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setObjectName("AiLibraryDetails")
        inspector.addWidget(self.details, 1)
        self.detail_path = QLineEdit(content)
        self.detail_path.setAccessibleName(_tr("source_path", "素材路径"))
        self.detail_path.setObjectName("AiLibrarySourcePath")
        self.detail_path.setReadOnly(True)
        inspector.addWidget(self.detail_path)
        content_layout.addLayout(inspector, 2)
        self.results_stack.addWidget(content)
        root.addWidget(self.results_stack, 1)

        footer = QHBoxLayout()
        self.search_status = QLabel(self)
        self.search_status.setTextFormat(Qt.TextFormat.PlainText)
        self.search_status.setObjectName("AiLibraryNote")
        self.search_status.setWordWrap(True)
        footer.addWidget(self.search_status, 1)
        self.import_button = QPushButton(_tr("add_to_pool", "加入媒体池"), self)
        self.import_button.setObjectName("AiLibraryPrimary")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._import_current)
        footer.addWidget(self.import_button)
        root.addLayout(footer)
        self._show_empty_results()

        transport.ai_library_results.connect(self._show_results)
        transport.ai_library_search_note.connect(self._show_search_note)
        transport.ai_library_embedding_consent_changed.connect(self.refresh_models)
        transport.ai_library_index_status.connect(self._show_index_status)
        transport.ai_library_copy_finished.connect(self._copy_finished)
        transport.ai_library_settings_loaded.connect(self._load_directory_policies)
        transport.ai_library_settings_saved.connect(self._directory_policy_saved)
        transport.library_changed.connect(self._show_library_change)
        transport.libraries_loaded.connect(self._libraries_loaded)
        self._show_index_status(transport.ai_library_index_state())

    def _libraries_loaded(self, _items: list[dict[str, Any]]) -> None:
        if self.isVisible():
            self.refresh_library_registry()

    def refresh_library_registry(self) -> None:
        self._reload_scope(preserve_draft=True)

    def refresh_models(self) -> None:
        self._reload_scope(preserve_draft=True)

    def search(self) -> None:
        if self._loading_scope or self._scope_error:
            return
        query = self.search_edit.text().strip()
        self._query = query
        self._request_id = uuid4().hex
        self.results.clear()
        self.results_stack.setCurrentIndex(0)
        if not self._selected_kinds():
            self._show_empty_results(queried=True)
            self.search_status.clear()
            return
        self.empty_title.setText(_tr("searching", "正在搜索…"))
        self.empty_hint.clear()
        self.search_status.clear()
        self._transport.search_ai_library(
            self._request_id, query, self._library_scope, folder=self._folder_scope,
            kinds=self._selected_kinds(), recursive=self.recursive_check.isChecked(),
            only_unanalyzed=self.pending_only_check.isChecked(),
            semantic=bool(self.search_mode.currentData()),
        )

    def _show_empty_results(self, *, queried: bool = False, error: str = "") -> None:
        self.results_stack.setCurrentIndex(0)
        if error:
            heading = _tr("search_failed", "暂时无法搜索")
            hint = ""
        elif queried:
            heading = _tr("no_matches", "没有匹配的素材")
            hint = _tr("try_another_query", "试试更短的关键词，或切换素材库范围。")
        elif self.pending_only_check.isChecked():
            heading = _tr("no_pending_media", "当前范围没有待分析的素材")
            hint = ""
        else:
            heading = _tr("empty_results", "还没有分析结果")
            hint = _tr("intro", "扫描后可搜索素材及分析结果。")
        self.empty_title.setText(heading)
        self.empty_hint.setText(hint)

    def set_library_scope(self, library_id: str | None, folder: str | None = None) -> None:
        if (self._library_scope, self._folder_scope) != (library_id, folder):
            self._library_scope = library_id
            self._folder_scope = folder if library_id else None
            self._scope_error = False
            self._restore_draft = None
            self._scope_policy = {}
            self._scope_stamp = None
            self._embedding_consent = None
            self.force_check.setChecked(False)
            self._loading_scope = True
            self._request_id = ""
            self.results.clear()
            self.import_button.setEnabled(False)
            self.results_stack.setCurrentIndex(0)
            self.empty_title.setText(_tr("searching", "正在搜索…"))
            self.empty_hint.clear()
            self.search_status.clear()
        index = self.analysis_directory.findData(library_id)
        with QSignalBlocker(self.analysis_directory):
            self.analysis_directory.setCurrentIndex(index)
        self._update_scope_label()
        self._update_policy_controls()

    def _scope_changed(self) -> None:
        self.set_library_scope(self.analysis_directory.currentData())
        self._reload_scope()

    def _start_index(self) -> None:
        if self._library_scope is None:
            kinds = self._selected_kinds()
            if not kinds or not self._policies:
                return
            if not any(self._models[kind] for kind in kinds) and not any(
                policy.get("mode") == "basic" and policy.get("models") is not None
                for policy in self._policies.values()
            ):
                self._open_model_settings(kinds[0])
                return
            self._transport.start_ai_library_index(
                kinds=kinds, recursive=self.recursive_check.isChecked(),
                only_unanalyzed=self.pending_only_check.isChecked(),
            )
            return
        self._save_scope(start_scan=True)

    def _toggle_settings(self, opened: bool) -> None:
        self.analysis_settings.setIcon(
            qta.icon("fa5s.chevron-up" if opened else "fa5s.chevron-down", color=C.TEXT_LABEL),
        )
        self._settings_panel.setVisible(opened)

    def _update_scope_label(self) -> None:
        title = _tr("entire_library", "整个素材库")
        if self._folder_scope:
            title = self.folder_button.fontMetrics().elidedText(
                Path(self._folder_scope).name, Qt.TextElideMode.ElideMiddle, 128,
            )
        self.folder_button.setText(title)
        self.folder_button.setToolTip(self._folder_scope or _tr("choose_scan_folder", "选择库内文件夹"))

    def _choose_scope_folder(self) -> None:
        row = self._policies.get(self._library_scope or "")
        if row is None:
            return
        root = Path(row["path"])
        picker = LibraryFolderPicker(
            self, title=_tr("choose_scan_folder", "选择库内文件夹"),
            initial_path=Path(self._folder_scope) if self._folder_scope else root,
            home=root, boundary=root,
        )
        try:
            if picker.exec() != QDialog.DialogCode.Accepted or picker.directory is None:
                return
            folder = str(picker.directory) if picker.directory != root else None
            if folder == self._folder_scope:
                return
            self.set_library_scope(self._library_scope, folder)
            self._reload_scope()
        finally:
            picker.deleteLater()

    def _selected_kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind, checkbox in self.type_checks.items() if checkbox.isChecked())

    def _picked_models(self) -> dict[str, str]:
        return {kind: str(profile) for kind, picker in self.model_pickers.items() if (profile := picker.currentData())}

    def _reload_scope(self, *, preserve_draft: bool = False) -> None:
        if preserve_draft and not self._loading_scope:
            self._restore_draft = {
                "mode": self.analysis_mode.currentData(), "models": self._picked_models(),
                "kinds": self._selected_kinds(), "recursive": self.recursive_check.isChecked(),
                "missing_models": tuple(kind for kind, choices in self._models.items() if not choices),
            }
        elif not preserve_draft:
            self._restore_draft = None
        self._loading_scope = True
        self._scope_error = False
        self._update_policy_controls()
        self._transport.load_ai_library_settings(self._library_scope, folder=self._folder_scope)

    def _show_results(self, request_id: str, hits: list[dict[str, Any]], error: str) -> None:
        if request_id != self._request_id:
            return
        if len(hits) == 1:
            result_status = _tr("one_result", "1 个候选")
        elif hits:
            result_status = _tr("results", "{count} 个候选", count=len(hits))
        else:
            result_status = ""
        self.search_status.setText(error or result_status)
        for hit in hits:
            title = str(hit["name"])
            start, end = hit.get("start_seconds"), hit.get("end_seconds")
            if start is not None and end is not None:
                title += f"   {_format_seconds(start)} – {_format_seconds(end)}"
            if not hit["online"]:
                title += f"   · {_tr('offline', '离线')}"
            item = QListWidgetItem(title, self.results)
            item.setData(Qt.ItemDataRole.UserRole, hit)
        if self.results.count():
            self.results_stack.setCurrentIndex(1)
            self.results.setCurrentRow(0)
        else:
            self._show_empty_results(queried=bool(self._query), error=error)

    def _show_search_note(self, request_id: str, note: str) -> None:
        if request_id == self._request_id and note:
            self.search_status.setText(" · ".join(filter(None, (self.search_status.text(), note))))

    def _selection_changed(self) -> None:
        hit = self._current_hit()
        self.import_button.setEnabled(bool(hit and hit["online"]))
        if hit is None:
            self.detail_title.clear()
            self.details.clear()
            self.detail_path.clear()
            self.detail_path.setToolTip("")
            return
        self.detail_title.setText(str(hit["name"]))
        tags = " · ".join(hit.get("tags") or ())
        description = str(hit.get("text") or "")
        if self.pending_only_check.isChecked() and not description:
            description = _tr("pending_media", "待分析")
        self.details.setPlainText("\n\n".join(filter(None, (description, tags))))
        self.detail_path.setText(hit["path"])
        self.detail_path.setCursorPosition(0)
        self.detail_path.setToolTip(hit["path"])

    def _current_hit(self) -> dict[str, Any] | None:
        item = self.results.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _import_current(self) -> None:
        hit = self._current_hit()
        if hit and hit["online"]:
            self.import_requested.emit(hit)

    def _reference_directory(self) -> None:
        destination = self._folder_scope or self._policies.get(self._library_scope or "", {}).get("path", "")
        dialog = AddLibraryDialog(self, initial_path=destination or str(Path.home()), transport=self._transport)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._transport.register_library(dialog.directory, dialog.library_name)
        finally:
            dialog.deleteLater()

    def _show_feedback(self, message: str) -> None:
        self.index_status.setText(message)
        self.index_status.setVisible(bool(message))

    def _show_library_change(self, _ok: bool, message: str) -> None:
        if self.isVisible() and not self._index_running and not self._copying:
            self._show_feedback(message)

    def _copy_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, _tr("copy_into_library", "复制素材到库…"), str(Path.home()))
        if not paths:
            return
        destination = self._folder_scope or self._policies.get(self._library_scope or "", {}).get("path", "")
        folder = QFileDialog.getExistingDirectory(
            self, _tr("copy_destination", "选择素材库存储目录"), destination)
        if not folder:
            return
        self._copying = True
        self._update_policy_controls()
        self._show_feedback(_tr("copying", "正在复制素材…"))
        self._transport.copy_to_ai_library(paths, folder)

    def _copy_finished(self, message: str) -> None:
        self._copying = False
        self._update_policy_controls()
        self._show_feedback(message)

    def _load_directory_policies(self, snapshot: dict[str, Any]) -> None:
        if "library_id" in snapshot and (
            snapshot["library_id"], snapshot.get("folder")
        ) != (self._library_scope, self._folder_scope):
            return
        self._loading_scope = False
        self._scope_error = bool(snapshot.get("error"))
        if snapshot.get("error"):
            self._show_policy_message(str(snapshot["error"]))
            self._show_empty_results(error=str(snapshot["error"]))
            self._update_policy_controls()
            return

        selected = snapshot.get("scope_library", self._library_scope)
        resolved_folder = snapshot.get("scope_folder", self._folder_scope)
        if (selected, resolved_folder) != (self._library_scope, self._folder_scope):
            self.set_library_scope(selected, resolved_folder)
            self._loading_scope = False
        self._policies = {row["library_id"]: row for row in snapshot.get("libraries", ())}
        if selected not in self._policies:
            self._library_scope = None
            self._folder_scope = None
        with QSignalBlocker(self.analysis_directory):
            self.analysis_directory.clear()
            self.analysis_directory.addItem(_tr("all_libraries", "全部素材库"), None)
            for library_id, row in self._policies.items():
                self.analysis_directory.addItem(row["name"], library_id)
                self.analysis_directory.setItemData(
                    self.analysis_directory.count() - 1, row["path"], Qt.ItemDataRole.ToolTipRole,
                )
            self.analysis_directory.setCurrentIndex(self.analysis_directory.findData(self._library_scope))

        for models in self._models.values():
            models.clear()
        for model in snapshot.get("models", ()):
            self._models[model["kind"]][model["profile_id"]] = model

        self._scope_policy = snapshot.get("scope_policy") or self._policies.get(self._library_scope, {})
        self._scope_stamp = snapshot.get("scope_stamp") if self._library_scope is not None else None
        self._embedding_route = snapshot.get("embedding_route") or {}
        self._embedding_consent = snapshot.get("embedding_consent")
        route = self._embedding_route
        if route:
            note = _tr("vector_ready", "向量模型 · {name}", name=route["name"])
            if route.get("upload") and self._library_scope:
                note += " · " + (
                    _tr("vector_authorized", "当前范围已授权") if self._embedding_consent == route.get("approval")
                    else _tr("vector_needs_authorization", "当前范围待授权")
                )
        else:
            note = snapshot.get("embedding_error") or _tr("vector_not_configured", "未配置默认向量模型")
        self.vector_status.setText(note)
        if self._folder_scope and not snapshot.get("scope_policy"):
            self._scope_policy = {}
        policy = self._restore_draft if self._restore_draft is not None else self._scope_policy
        self._restore_draft = None
        choices = policy.get("models")
        mode = policy.get("mode", "profiles")
        if choices is None and not policy.get("configured") and mode == "basic":
            mode = "profiles"
        with QSignalBlocker(self.analysis_mode), QSignalBlocker(self.recursive_check):
            self.analysis_mode.setCurrentIndex(self.analysis_mode.findData(mode))
            self.recursive_check.setChecked(policy.get("recursive", True))
        for kind, picker in self.model_pickers.items():
            with QSignalBlocker(self.type_checks[kind]), QSignalBlocker(picker):
                self.type_checks[kind].setChecked(kind in policy.get("kinds", self._models))
                picker.clear()
                picker.addItem(_tr("pick_model", "选择模型"), None)
                models = self._models[kind]
                for profile_id, model in models.items():
                    picker.addItem(model["name"], profile_id)
                    picker.setItemData(
                        picker.count() - 1, f"{model['model']} · {model['origin']}",
                        Qt.ItemDataRole.ToolTipRole,
                    )
                default = next((profile_id for profile_id, model in models.items() if model.get("default")), None)
                if default is None and len(models) == 1:
                    default = next(iter(models))
                profile_id = default
                if isinstance(choices, dict) and kind not in policy.get("missing_models", ()):
                    profile_id = choices.get(kind)
                picker.setCurrentIndex(max(0, picker.findData(profile_id)))
        self._update_scope_label()
        self._analysis_options_changed()
        if self.isVisible():
            self.search()

    def _filters_changed(self) -> None:
        self._analysis_options_changed()
        if self.isVisible() and not self._loading_scope:
            self.search()

    def _pending_filter_changed(self, enabled: bool) -> None:
        if enabled:
            self.force_check.setChecked(False)
        self.search_edit.setPlaceholderText(
            _tr("pending_search_placeholder", "搜索待分析的文件名") if enabled
            else _tr("placeholder", "输入脚本片段、标签或素材描述")
        )
        self._update_policy_controls()
        self.search()

    def _analysis_options_changed(self) -> None:
        self._update_policy_controls()
        if self._loading_scope:
            return
        kinds = self._selected_kinds()
        if self._library_scope is None:
            text = _tr("all_library_policy", "扫描全部素材库时，各库使用自己保存的分析设置。")
        elif not kinds:
            text = _tr("choose_types", "至少勾选一种素材类型。")
        elif self.analysis_mode.currentData() == "basic":
            text = _tr("analysis_basic_detail", "只读取文件信息，并在已安装 CPU 识别组件时识别图片文字。")
        elif any(not self._models[kind] for kind in kinds):
            text = _tr("missing_models_hint", "勾选的类型缺少可用模型，可从「模型配置」添加。")
        else:
            text = ""
        self._show_policy_message(text)

    def _open_model_settings(self, kind: str) -> None:
        self.analysis_settings.setChecked(True)
        self.configure_models_requested.emit("speech" if kind == "audio" else "vision")

    def _start_vectors(self) -> None:
        owner, folder, stamp = self._library_scope, self._folder_scope, self._scope_stamp
        route = self._embedding_route
        approval = None
        if not route or (route.get("upload") and route.get("upload_allowed") is False):
            self.configure_models_requested.emit("embedding")
            return
        if route.get("upload") and owner and route.get("approval") != self._embedding_consent:
            if stamp is None:
                self._show_feedback(_tr("scope_unavailable", "授权前请连接并重新选择素材文件夹。"))
                return
            confirm = QMessageBox(self)
            confirm.setIcon(QMessageBox.Icon.Question)
            confirm.setWindowTitle(_tr("vector_consent_title", "允许云端向量检索？"))
            confirm.setTextFormat(Qt.TextFormat.PlainText)
            confirm.setText(_tr(
                "vector_consent",
                "{service}（{model}）会接收 {folder} 及其子文件夹已保存的描述、标签、转录和语义搜索词。"
                "\n\n授权整个素材库时，剪辑助手的素材检索词也会发送给该服务。原始媒体文件留在本机。",
                service=route["origin"], model=route["model"], folder=folder or self._policies[owner]["path"],
            ))
            confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return
            if (owner, folder, stamp) != (self._library_scope, self._folder_scope, self._scope_stamp):
                return
            approval = route["approval"]
        self._transport.start_ai_library_embeddings(
            owner, folder=folder, kinds=self._selected_kinds(), recursive=self.recursive_check.isChecked(),
            approval=approval, expected_scope_stamp=stamp,
        )

    def _revoke_vector_access(self) -> None:
        if self._library_scope is not None:
            self._transport.revoke_ai_library_embedding_consent(self._library_scope, folder=self._folder_scope)

    def _save_directory_policy(self) -> None:
        self._save_scope(start_scan=False)

    def _save_scope(self, *, start_scan: bool) -> None:
        library_id = self._library_scope
        kinds = self._selected_kinds()
        mode = self.analysis_mode.currentData()
        if not library_id or not mode or not kinds or self._loading_scope or self._saving_policy or self._scope_error:
            return
        folder = self._folder_scope
        stamp = self._scope_stamp
        recursive = self.recursive_check.isChecked()
        models = self._picked_models()
        approvals: tuple[str, ...] = ()
        if mode == "profiles":
            if start_scan:
                for kind in kinds:
                    if not self._models[kind]:
                        self._open_model_settings(kind)
                        return
                    if kind not in models:
                        self.analysis_settings.setChecked(True)
                        self.model_pickers[kind].setFocus()
                        self._show_policy_message(_tr("choose_models_for_types", "请为勾选的类型选择模型。"))
                        return
            routes = [self._models[kind][models[kind]] for kind in kinds if kind in models]
            blocked = [route for route in routes if route["upload"] and not route["upload_allowed"]]
            if blocked and start_scan:
                self._show_policy_message(_tr("enable_model_upload", "请先在模型配置中允许所选服务接收素材。"))
                self._open_model_settings(blocked[0]["kind"])
                return
            uploads = [route for route in routes if route["upload"] and route["upload_allowed"]]
            approvals = tuple(dict.fromkeys(route["approval"] for route in uploads))
            if approvals and stamp is None:
                self._show_policy_message(_tr("scope_unavailable", "授权前请连接并重新选择素材文件夹。"))
                return
            previous = set(self._scope_policy.get("approved_routes", ()))
            expanded = recursive and not self._scope_policy.get("recursive", True)
            if approvals and (not set(approvals).issubset(previous) or expanded):
                if not self._confirm_uploads(folder or self._policies[library_id]["path"], recursive, uploads):
                    return
                if (library_id, folder) != (self._library_scope, self._folder_scope) or self._scope_stamp != stamp:
                    return

        self._pending_policy = {
            "mode": mode, "models": models, "kinds": kinds, "recursive": recursive,
            "approved_routes": approvals,
        }
        self._pending_policy_scope = (library_id, folder)
        self._scan_after_save = (
            (library_id, folder, self.force_check.isChecked(), self.pending_only_check.isChecked())
            if start_scan else None
        )
        self._saving_policy = True
        self._update_policy_controls()
        self._transport.save_ai_library_settings(
            library_id, mode, approvals, folder=folder, models=models, kinds=kinds, recursive=recursive,
            expected_scope_stamp=stamp,
        )

    def _confirm_uploads(self, folder: str, recursive: bool, routes: list[dict[str, Any]]) -> bool:
        kinds = {kind: _tr(key, label) for kind, key, label in self._MEDIA_LABELS}
        recipients = "\n".join(
            f"{kinds[route['kind']]}  ·  {route['name']} ({route['origin']} · {route['model']})"
            for route in routes
        )
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle(_tr("analysis_consent_title", "允许向已绑定模型发送素材？"))
        confirm.setTextFormat(Qt.TextFormat.PlainText)
        confirm.setText(_tr(
            "scoped_analysis_consent",
            "分析 {folder}（{depth}）时，以下类型的素材会发送至所选服务：\n\n{recipients}",
            folder=folder, recipients=recipients,
            depth=_tr("include_subfolders", "包含子文件夹") if recursive else _tr("this_folder_only", "仅当前文件夹"),
        ))
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return confirm.exec() == QMessageBox.StandardButton.Yes

    def _show_policy_message(self, message: str) -> None:
        self.policy_status.setText(message)
        self.policy_status.setVisible(bool(message))

    def _directory_policy_saved(self, error: str) -> None:
        if not self._saving_policy:
            return
        self._saving_policy = False
        scan, self._scan_after_save = self._scan_after_save, None
        scope, self._pending_policy_scope = self._pending_policy_scope, None
        if scope != (self._library_scope, self._folder_scope):
            self._update_policy_controls()
            return
        if error:
            self._update_policy_controls()
            self._show_feedback(error)
            self._reload_scope(preserve_draft=True)
            return
        self._scope_policy = self._pending_policy
        if scan is None:
            self._update_policy_controls()
            self._show_policy_message(_tr("scope_settings_saved", "已保存当前范围的分析设置。"))
            return
        library_id, folder, force, only_unanalyzed = scan
        self.force_check.setChecked(False)
        self._transport.start_ai_library_index(
            library_id, folder=folder, force=force, only_unanalyzed=only_unanalyzed,
        )

    def _update_policy_controls(self) -> None:
        has_libraries = bool(self._policies)
        idle = not self._index_running and not self._saving_policy and not self._copying
        ready = idle and not self._loading_scope
        scoped = self._library_scope in self._policies
        each_model = self.analysis_mode.currentData() == "profiles"
        has_types = bool(self._selected_kinds())
        self.analysis_directory.setEnabled(has_libraries and idle)
        self.folder_button.setEnabled(scoped and ready)
        can_edit = scoped and ready and not self._scope_error
        self.analysis_mode.setEnabled(can_edit)
        self.analysis_mode.setVisible(scoped)
        self.analysis_mode_label.setVisible(scoped)
        self.save_policy_button.setEnabled(can_edit and has_types)
        self.configure_models_button.setEnabled(idle)
        self.vector_model_button.setEnabled(idle)
        self.vector_button.setEnabled(has_libraries and ready and has_types and not self._scope_error)
        self.revoke_vector_button.setVisible(scoped and self._embedding_consent is not None)
        self.revoke_vector_button.setEnabled(can_edit)
        self.search_mode.setEnabled(not self.pending_only_check.isChecked())
        self.recursive_check.setEnabled(ready)
        self.force_check.setEnabled(can_edit and has_types and not self.pending_only_check.isChecked())
        for kind, picker in self.model_pickers.items():
            self.type_checks[kind].setEnabled(ready)
            picker.setVisible(scoped and each_model)
            picker.setEnabled(can_edit and self.type_checks[kind].isChecked())
        self.index_button.setEnabled(has_libraries and ready and has_types and not self._scope_error)
        self.add_button.setEnabled(idle)
        self.copy_button.setEnabled(idle)

    def _show_index_status(self, state: dict[str, Any]) -> None:
        running = bool(state.get("running"))
        self._index_running = running
        self._update_policy_controls()
        self.analysis_action.setCurrentWidget(self.cancel_button if running else self.index_button)
        self.analysis_progress.setVisible(running)
        if running:
            done, total = state.get("done", 0), state.get("total", 0)
            self.analysis_progress.setRange(0, total)
            self.analysis_progress.setValue(done)
            if state.get("phase") == "vectors":
                text = _tr("vector_progress", "正在更新向量 {done} / {total}", done=done, total=total or "…")
            else:
                text = _tr("progress", "正在分析 {done} / {total}", done=done, total=total or "…")
        elif "error" in state:
            text = str(state["error"])
        elif state:
            text = ""
            if "indexed" in state:
                text = _tr("summary", "已分析 {indexed} 项 · 跳过 {skipped} 项 · 离线 {offline} 项 · 失败 {failed} 项",
                           **{key: state.get(key, 0) for key in ("indexed", "skipped", "offline", "failed")})
            if state.get("metadata_only"):
                text += " · " + _tr("metadata_only", "{count} 项仅登记了文件信息",
                                     count=state["metadata_only"])
            if state.get("cancelled"):
                text = _tr("cancelled", "已停止") + " · " + text
            errors = state.get("errors") or ()
            if errors:
                text += "\n" + str(errors[0].get("message", ""))
            warnings = state.get("warnings") or ()
            if warnings:
                messages = [str(warning.get("message", "")) for warning in warnings]
                text += "\n" + "\n".join(dict.fromkeys(filter(None, messages)))
            vectors = state.get("vectors")
            if vectors:
                summary = str(vectors.get("error") or _tr(
                    "vector_summary", "向量 · 已更新 {embedded} 段 · 已有 {skipped} 段 · 待更新 {pending} 段",
                    **{key: vectors.get(key, 0) for key in ("embedded", "skipped", "pending")},
                ))
                if vectors.get("cancelled"):
                    summary = _tr("cancelled", "已停止") + " · " + summary
                text = "\n".join(filter(None, (text, summary)))
                if self.isVisible():
                    self.refresh_models()
            elif self.isVisible():
                self.search()
        else:
            text = ""
        self._show_feedback(text)
        if state.get("model_setup_required") and self.isVisible():
            self._open_model_settings(str(state.get("kind") or "image"))
