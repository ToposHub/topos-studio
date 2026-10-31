"""T03 · 统一洞察窗口（PyQt6 独立非模态工具窗口，offscreen 可测）。

纪律（计划 T03）：

- 结果表用 ``QAbstractTableModel`` + ``QTableView``（虚拟化），不为每行
  创建控件；
- 刷新=显式新捕获：``CaptureGuard`` 发布新 capture_id，后台结果绑定捕获
  ID，过时结果在回 UI 前拒收；窗口关闭后旧任务不再回写；
- 从结果定位只经 ``host.navigate_to`` 回调（UI 线程），不经全局快捷键/
  菜单旁路——输入框聚焦不会触发时间线 JKL（本窗口不注册任何全局键）；
- 词条在自有 ``locales/``，键名 ``insights.*`` 不覆盖旧键；缺词条回退
  英文默认值；
- 首版不做 dock/workspace 接入（可停靠升级留后续）。
"""

from __future__ import annotations

import os
from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.insights.dto import FpsSpec, InsightSnapshot
from src.features.edit.insights.host_protocol import (
    NavigateRequest,
    ThumbnailRequest,
)
from src.features.edit.insights.report import build_report, write_csv, write_html, write_json
from src.features.edit.insights.runtime import (
    BoundedWorker,
    CaptureGuard,
    InsightsConfig,
    TempArtifactManager,
)
from src.features.edit.insights.search import (
    SearchQuery,
    SearchResult,
    parse_query_string,
    unified_search,
)
from src.features.edit.insights.stats import compute_usage_rhythm
from src.shared.i18n.i18n import I18N

_LOADED = False


def _ensure_translations() -> None:
    """加载本包自有词条（幂等；目录登记后 reload_locale 也能重合并）。"""
    global _LOADED
    if _LOADED:
        return
    I18N.load_module_translations(
        os.path.join(os.path.dirname(__file__), "locales")
    )
    _LOADED = True


def _tr(key: str, default: str) -> str:
    _ensure_translations()
    return I18N.tr(f"insights.{key}", default)


def _format_tc(frame: int | None, fps: FpsSpec | None) -> str:
    if frame is None or fps is None:
        return "—"
    from src.shared.media.metadata import Rational
    from src.shared.media.timecode import Timecode, TimecodeType

    tc_type = (
        TimecodeType.DROP_FRAME if fps.drop_frame
        else TimecodeType.NON_DROP_FRAME
    )
    return Timecode.from_frame_number(
        max(frame, 0), Rational(fps.num, fps.den), tc_type
    ).to_string()


class SearchHitsModel(QAbstractTableModel):
    """搜索结果表模型（虚拟化；命中 2000 行也不建行控件）。"""

    _COLUMNS = ("type", "sequence", "track", "label", "tc_in", "tc_out")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._hits: tuple = ()
        self._fps_by_seq: dict[str, FpsSpec] = {}

    def set_result(self, fps_by_seq: dict[str, FpsSpec], hits: tuple) -> None:
        self.beginResetModel()
        self._fps_by_seq = fps_by_seq
        self._hits = hits
        self.endResetModel()

    def clear(self) -> None:
        self.set_result({}, ())

    def hit_at(self, row: int):
        return self._hits[row]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._hits)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self._COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _tr(f"col.{self._COLUMNS[section]}", self._COLUMNS[section])
        return section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        hit = self._hits[index.row()]
        field = self._COLUMNS[index.column()]
        if field == "type":
            return hit.entity_type
        if field == "sequence":
            return hit.sequence_name or hit.sequence_id or "—"
        if field == "track":
            return hit.track_id or "—"
        if field == "label":
            return hit.entity_label
        if field == "tc_in":
            return _format_tc(hit.timeline_start, self._fps_by_seq.get(hit.sequence_id or ""))
        return _format_tc(hit.timeline_end, self._fps_by_seq.get(hit.sequence_id or ""))


class _ResultRelay(QObject):
    """worker 线程 → UI 线程的结果中继（队列连接）。"""

    result_ready = pyqtSignal(object)


class InsightsWindow(QWidget):
    """统一洞察窗口（搜索 / 总表导出 / 统计）。"""

    search_requested = pyqtSignal(str)
    closed = pyqtSignal()  # shutdown() 时发出（factory 据此清理登记）

    def __init__(
        self,
        host: Any,
        config: InsightsConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        _ensure_translations()
        self._host = host
        self._config = config or InsightsConfig()
        self._snapshot: InsightSnapshot | None = None
        self._closed = False
        self._job_id: str | None = None
        self._guard = CaptureGuard()
        self._worker = BoundedWorker(self._config)

        self.setWindowTitle(_tr("title", "Insights"))
        self.setWindowFlag(Qt.WindowType.Window, True)
        self._build_ui()

        self._relay = _ResultRelay()
        self._relay.result_ready.connect(self._on_result)

    # ── UI 构建 ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.scope_combo = QComboBox(self)
        self.refresh_btn = QPushButton(_tr("refresh", "Refresh Capture"), self)
        self.cancel_btn = QPushButton(_tr("cancel", "Cancel"), self)
        self.status_label = QLabel(_tr("status.no_capture", "no capture yet"), self)
        top.addWidget(self.scope_combo, 1)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.cancel_btn)
        top.addWidget(self.status_label, 2)
        root.addLayout(top)

        self.tabs = QTabWidget(self)
        root.addWidget(self.tabs, 1)

        # 搜索页
        search_page = QWidget(self)
        sv = QVBoxLayout(search_page)
        bar = QHBoxLayout()
        self.search_edit = QLineEdit(search_page)
        self.search_edit.setPlaceholderText(
            _tr("search.hint", "text, type:cue timeline:name …")
        )
        self.search_btn = QPushButton(_tr("search", "Search"), search_page)
        bar.addWidget(self.search_edit, 1)
        bar.addWidget(self.search_btn)
        sv.addLayout(bar)
        self.hits_model = SearchHitsModel(self)
        self.hits_view = QTableView(search_page)
        self.hits_view.setModel(self.hits_model)
        self.hits_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.hits_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.hits_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.hits_view.doubleClicked.connect(self._navigate_index)
        # 键盘等价导航：结果表聚焦时 Enter/Return 定位当前行（双击的
        # 无障碍补充；QShortcut 限定 WidgetShortcut 不抢搜索框输入）。
        from PyQt6.QtGui import QKeySequence, QShortcut

        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.hits_view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._navigate_current)
        sv.addWidget(self.hits_view, 1)
        self.search_status = QLabel("", search_page)
        sv.addWidget(self.search_status)
        self.tabs.addTab(search_page, _tr("tab.search", "Search"))
        self.search_btn.clicked.connect(self._on_search_clicked)
        self.search_edit.returnPressed.connect(self._on_search_clicked)

        # 总表页
        report_page = QWidget(self)
        rv = QVBoxLayout(report_page)
        self.export_btn = QPushButton(
            _tr("report.export", "Export CSV / JSON / HTML …"), report_page
        )
        self.report_status = QLabel(_tr("status.no_capture", "no capture yet"), report_page)
        rv.addWidget(self.export_btn)
        rv.addWidget(self.report_status)
        rv.addStretch(1)
        self.tabs.addTab(report_page, _tr("tab.report", "Report"))
        self.export_btn.clicked.connect(self._on_export_clicked)

        # 统计页
        stats_page = QWidget(self)
        stv = QVBoxLayout(stats_page)
        self.seq_stats_table = QTableWidget(0, 7, stats_page)
        self.seq_stats_table.setHorizontalHeaderLabels([
            _tr("stats.sequence", "Sequence"),
            _tr("stats.loaded", "Loaded"),
            _tr("stats.duration", "Duration"),
            _tr("stats.shots", "Shots"),
            _tr("stats.mean", "Mean"),
            _tr("stats.median", "Median"),
            _tr("stats.cpm", "Cuts/min"),
        ])
        self.asset_table = QTableWidget(0, 4, stats_page)
        self.asset_table.setHorizontalHeaderLabels([
            _tr("stats.asset", "Asset"),
            _tr("stats.type", "Type"),
            _tr("stats.uses", "Uses"),
            _tr("stats.sequences", "Sequences"),
        ])
        stv.addWidget(QLabel(_tr("stats.seq_title", "Sequence rhythm"), stats_page))
        stv.addWidget(self.seq_stats_table, 1)
        stv.addWidget(QLabel(_tr("stats.asset_title", "Asset usage"), stats_page))
        stv.addWidget(self.asset_table, 1)
        self.tabs.addTab(stats_page, _tr("tab.stats", "Stats"))

        # 联系表页（T13 入口）
        sheet_page = QWidget(self)
        shv = QVBoxLayout(sheet_page)
        self.sheet_combo = QComboBox(sheet_page)
        self.sheet_btn = QPushButton(
            _tr("sheet.export", "Generate Contact Sheet…"), sheet_page
        )
        self.sheet_status = QLabel(
            _tr("status.no_capture", "no capture yet"), sheet_page
        )
        shv.addWidget(self.sheet_combo)
        shv.addWidget(self.sheet_btn)
        shv.addWidget(self.sheet_status)
        shv.addStretch(1)
        self.tabs.addTab(sheet_page, _tr("tab.sheet", "Contact Sheet"))
        self.sheet_btn.clicked.connect(self._on_sheet_clicked)

        # 资料包页（T14 入口）
        pkg_page = QWidget(self)
        pv = QVBoxLayout(pkg_page)
        from PyQt6.QtWidgets import QCheckBox

        self.pkg_zip_check = QCheckBox(_tr("pkg.make_zip", "Also create ZIP"), pkg_page)
        self.pkg_btn = QPushButton(
            _tr("pkg.export", "Export Review Package…"), pkg_page
        )
        self.pkg_status = QLabel(_tr("status.no_capture", "no capture yet"), pkg_page)
        pv.addWidget(self.pkg_zip_check)
        pv.addWidget(self.pkg_btn)
        pv.addWidget(self.pkg_status)
        pv.addStretch(1)
        self.tabs.addTab(pkg_page, _tr("tab.package", "Review Package"))
        self.pkg_btn.clicked.connect(self._on_package_clicked)

        # 对照页（T15 入口：保存/打开两份/比较）
        diff_page = QWidget(self)
        dv = QVBoxLayout(diff_page)
        diff_bar = QHBoxLayout()
        self.diff_save_btn = QPushButton(_tr("diff.save", "Save Current Snapshot…"), diff_page)
        self.diff_open_a_btn = QPushButton(_tr("diff.open_a", "Open Snapshot A…"), diff_page)
        self.diff_open_b_btn = QPushButton(_tr("diff.open_b", "Open Snapshot B…"), diff_page)
        self.diff_compare_btn = QPushButton(_tr("diff.compare", "Compare A ↔ B"), diff_page)
        for btn in (self.diff_save_btn, self.diff_open_a_btn,
                    self.diff_open_b_btn, self.diff_compare_btn):
            diff_bar.addWidget(btn)
        from PyQt6.QtWidgets import QPlainTextEdit

        self.diff_slots_label = QLabel(_tr("diff.slots_empty", "A: — · B: —"), diff_page)
        self.diff_view = QPlainTextEdit(diff_page)
        self.diff_view.setReadOnly(True)
        dv.addLayout(diff_bar)
        dv.addWidget(self.diff_slots_label)
        dv.addWidget(self.diff_view, 1)
        self.tabs.addTab(diff_page, _tr("tab.diff", "Compare"))
        self.diff_save_btn.clicked.connect(self._on_diff_save_clicked)
        self.diff_open_a_btn.clicked.connect(lambda: self._on_diff_open_clicked("A"))
        self.diff_open_b_btn.clicked.connect(lambda: self._on_diff_open_clicked("B"))
        self.diff_compare_btn.clicked.connect(self._on_diff_compare_clicked)
        self._snapshot_a: InsightSnapshot | None = None
        self._snapshot_b: InsightSnapshot | None = None

        self.refresh_btn.clicked.connect(self.refresh_capture)
        self.cancel_btn.clicked.connect(self.cancel_pending)

    # ── 捕获 / 刷新 ──────────────────────────────────────────────

    def refresh_capture(self) -> None:
        """显式刷新：新捕获 ID；未完成的后台作业结果将因过时被拒收。"""
        if self._closed:
            return
        capture = self._host.capture_readonly_state()
        if not capture.ok or capture.snapshot is None:
            self._snapshot = None
            self.status_label.setText(
                _tr("status.capture_failed", "capture failed: {error}").format(
                    error=capture.message or capture.error_code
                )
            )
            return
        self._snapshot = capture.snapshot
        self._guard.publish(capture.snapshot.capture_id)
        self._apply_scope_options()
        self.status_label.setText(
            _tr("status.captured", "captured {id} · {n} sequences").format(
                id=capture.snapshot.capture_id[:8],
                n=len(capture.snapshot.sequences),
            )
        )
        self.report_status.setText(
            _tr("status.report_ready", "{n} rows ready to export").format(
                n=len(capture.snapshot.sequences)
            )
        )
        self._submit_stats_job()
        self._submit_search_job(self.search_edit.text())

    def _apply_scope_options(self) -> None:
        self.scope_combo.blockSignals(True)
        self.scope_combo.clear()
        self.scope_combo.addItem(_tr("scope.all", "All loaded sequences"), None)
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        self.sheet_combo.addItem(_tr("scope.all", "All loaded sequences"), None)
        for seq in (self._snapshot.sequences if self._snapshot else ()):
            if seq.load_state != "loaded":
                continue
            # 同名序列可区分：名称 + id 前 8 位
            label = f"{seq.name} · {seq.sequence_id[:8]}"
            self.scope_combo.addItem(label, seq.sequence_id)
            if any(t.track_type == "video" for t in seq.tracks):
                self.sheet_combo.addItem(label, seq.sequence_id)
        self.scope_combo.blockSignals(False)
        self.sheet_combo.blockSignals(False)

    # ── 搜索（worker + 守卫）─────────────────────────────────────

    def _on_search_clicked(self) -> None:
        self._submit_search_job(self.search_edit.text())

    def _submit_search_job(self, text: str) -> None:
        if self._closed or self._snapshot is None or not text.strip():
            if self._snapshot is None:
                self.search_status.setText(
                    _tr("status.refresh_first", "refresh capture first")
                )
            return
        query = parse_query_string(text)
        scope_id = self.scope_combo.currentData() if self.scope_combo.count() else None
        if scope_id:
            query = SearchQuery(
                text=query.text,
                sequence_ids=(scope_id,),
                timeline_name_terms=query.timeline_name_terms,
                track_types=query.track_types,
                entity_types=query.entity_types,
                min_duration_frames=query.min_duration_frames,
                max_duration_frames=query.max_duration_frames,
                case_sensitive=query.case_sensitive,
            )
        snapshot = self._snapshot
        self._job_id = self._worker.submit(
            lambda token: unified_search(
                snapshot, query, limit=self._config.search_hit_limit
            ),
            capture_id=snapshot.capture_id,
            job_id=f"search-{snapshot.capture_id[:8]}",
            on_done=self._relay.result_ready.emit,
        )

    def _submit_stats_job(self) -> None:
        assert self._snapshot is not None
        snapshot = self._snapshot
        self._worker.submit(
            lambda token: ("stats", compute_usage_rhythm(snapshot)),
            capture_id=snapshot.capture_id,
            job_id=f"stats-{snapshot.capture_id[:8]}",
            on_done=self._relay.result_ready.emit,
        )

    def _on_result(self, result: Any) -> None:
        """UI 线程消费：过时捕获 / 已关闭 → 拒收（不改 UI）。"""
        if self._closed:
            return
        if not self._guard.is_current(result.capture_id):
            return  # 后台结果绑定捕获 ID：旧捕获结果拒收
        if not result.ok:
            self.search_status.setText(
                _tr("status.job_failed", "job failed: {e}").format(e=result.error)
            )
            return
        value = result.value
        if isinstance(value, SearchResult):
            self._apply_search_result(value)
        elif isinstance(value, tuple) and value and value[0] == "stats":
            self._apply_stats(value[1])

    def _apply_search_result(self, result: SearchResult) -> None:
        assert self._snapshot is not None
        fps_map = {
            s.sequence_id: s.fps
            for s in self._snapshot.sequences
        }
        self.hits_model.set_result(fps_map, result.hits)
        parts = [_tr("status.hits", "{n} hits").format(n=len(result.hits))]
        if result.excluded_unloaded:
            parts.append(
                _tr("status.excluded", "{n} sequences not loaded (excluded)").format(
                    n=len(result.excluded_unloaded)
                )
            )
        if result.truncated:
            parts.append(_tr("status.truncated", "result truncated"))
        self.search_status.setText(" · ".join(parts))

    def _apply_stats(self, report: Any) -> None:
        self.seq_stats_table.setRowCount(0)
        for s in report.sequences:
            row = self.seq_stats_table.rowCount()
            self.seq_stats_table.insertRow(row)
            rhythm = s.video_shot_rhythm
            values = [
                s.name,
                s.load_state,
                str(s.duration_frames if s.duration_frames is not None else "—"),
                str(rhythm.shot_count) if rhythm else "—",
                f"{rhythm.mean_length:.1f}" if rhythm and rhythm.mean_length else "—",
                f"{rhythm.median_length:.1f}" if rhythm and rhythm.median_length else "—",
                f"{rhythm.cuts_per_minute:.2f}" if rhythm and rhythm.cuts_per_minute else "—",
            ]
            for col, text in enumerate(values):
                self.seq_stats_table.setItem(row, col, QTableWidgetItem(text))
        self.asset_table.setRowCount(0)
        for a in report.assets:
            row = self.asset_table.rowCount()
            self.asset_table.insertRow(row)
            for col, text in enumerate([
                a.display_name, a.media_type, str(a.occurrence_count),
                ", ".join(a.sequence_ids) or "—",
            ]):
                self.asset_table.setItem(row, col, QTableWidgetItem(text))

    # ── 导航（只经 host 回调）────────────────────────────────────

    def _navigate_current(self) -> None:
        """Enter/Return：定位结果表当前行（与双击同一处理路径）。"""
        index = self.hits_view.currentIndex()
        if index.isValid():
            self._navigate_index(index)

    def _navigate_index(self, index: QModelIndex) -> None:
        hit = self.hits_model.hit_at(index.row())
        if self._snapshot is None or hit.sequence_id is None:
            return
        request = NavigateRequest(
            capture_id=self._snapshot.capture_id,
            project_id=self._snapshot.project.project_id,
            sequence_id=hit.sequence_id,
            target_frame=hit.timeline_start or 0,
            entity_id=hit.entity_id,
            track_id=hit.track_id,
        )
        result = self._host.navigate_to(request)
        if not result.ok:
            self.search_status.setText(
                _tr("status.nav_rejected", "navigate rejected: {r}").format(
                    r=result.reason
                )
            )

    # ── 导出 ─────────────────────────────────────────────────────

    def export_report_files(self, directory: str) -> list[str]:
        """把当前捕获的总表导出为 CSV/JSON/HTML（临时写全→原子发布）。"""
        if self._snapshot is None:
            return []
        report = build_report(self._snapshot)
        mgr = TempArtifactManager(directory)
        written: list[str] = []
        try:
            targets = {
                "report.csv": lambda p: write_csv(report, p),
                "report.json": lambda p: write_json(report, p),
                "report.html": lambda p: write_html(report, p),
            }
            for name, writer in targets.items():
                temp = mgr.temp_path(name)
                writer(str(temp))
                final = os.path.join(directory, name)
                mgr.publish(name, final)
                written.append(final)
        finally:
            mgr.cleanup()
        return written

    def _on_export_clicked(self) -> None:
        if self._snapshot is None:
            self.report_status.setText(
                _tr("status.refresh_first", "refresh capture first")
            )
            return
        from PyQt6.QtWidgets import QFileDialog

        directory = QFileDialog.getExistingDirectory(
            self, _tr("report.pick_dir", "Choose export directory")
        )
        if not directory:
            return
        written = self.export_report_files(directory)
        self.report_status.setText(
            _tr("status.exported", "exported {n} files").format(n=len(written))
        )

    # ── 取消 / 关闭 ──────────────────────────────────────────────

    def cancel_pending(self) -> None:
        if self._job_id:
            self._worker.cancel(self._job_id)

    # ── 联系表 / 资料包 / 对照（T13/T14/T15 用户入口）──────────

    def export_contact_sheets(self, directory: str, sequence_id: str | None = None) -> list[str]:
        """为选中（或全部含视频轨的已加载）序列生成联系表 PNG+HTML。"""
        if self._snapshot is None:
            self.sheet_status.setText(_tr("status.refresh_first", "refresh capture first"))
            return []
        from src.features.edit.insights.contact_sheet import (
            build_contact_cells,
            render_contact_sheet_png,
            write_contact_sheet_html,
        )

        targets = [sequence_id] if sequence_id else [
            s.sequence_id for s in self._snapshot.sequences
            if s.load_state == "loaded"
            and any(t.track_type == "video" for t in s.tracks)
        ]
        written: list[str] = []
        for seq_id in targets:
            cells = build_contact_cells(self._snapshot, seq_id, host=self._host)
            if not cells:
                continue
            out_dir = f"{directory}/contact_sheet_{seq_id[:12]}"
            os.makedirs(out_dir, exist_ok=True)
            render_contact_sheet_png(cells, f"{out_dir}/sheet.png", columns=3)
            write_contact_sheet_html(cells, out_dir, prefix="cs")
            written.append(out_dir)
        self.sheet_status.setText(
            _tr("sheet.done", "{n} contact sheet(s) written").format(n=len(written))
        )
        return written

    def _on_sheet_clicked(self) -> None:
        if self._snapshot is None:
            self.sheet_status.setText(_tr("status.refresh_first", "refresh capture first"))
            return
        from PyQt6.QtWidgets import QFileDialog

        directory = QFileDialog.getExistingDirectory(
            self, _tr("sheet.pick_dir", "Choose contact sheet directory")
        )
        if not directory:
            return
        self.export_contact_sheets(directory, self.sheet_combo.currentData())

    def export_review_package(self, directory: str, make_zip: bool = False):
        """导出审片资料包（含音频 cue CSV——复用宿主两层状态，可用时）。"""
        from src.features.edit.insights.review_package import build_review_package

        if self._snapshot is None:
            self.pkg_status.setText(_tr("status.refresh_first", "refresh capture first"))
            return None
        return build_review_package(
            self._snapshot, directory,
            host=self._host,
            make_zip=make_zip,
            audio_cue_state_fn=getattr(self._host, "capture_state_fn", None),
        )

    def _on_package_clicked(self) -> None:
        if self._snapshot is None:
            self.pkg_status.setText(_tr("status.refresh_first", "refresh capture first"))
            return
        from PyQt6.QtWidgets import QFileDialog

        directory = QFileDialog.getExistingDirectory(
            self, _tr("pkg.pick_dir", "Choose package directory")
        )
        if not directory:
            return
        result = self.export_review_package(directory, self.pkg_zip_check.isChecked())
        if result is None:
            return
        if result.ok:
            extra = f" · ZIP: {result.zip_path}" if result.zip_path else ""
            degrade = (
                _tr("pkg.degraded", " (contact sheet degraded: placeholders)")
                if result.degraded else ""
            )
            self.pkg_status.setText(
                _tr("pkg.done", "package written: {d}{extra}{degrade}").format(
                    d=result.directory, extra=extra, degrade=degrade
                )
            )
        else:
            self.pkg_status.setText(
                _tr("pkg.failed", "package failed: {e}").format(e=result.error)
            )

    def save_current_snapshot(self, path: str) -> bool:
        """保存当前捕获为资料快照文件（T15 入口，T01 唯一 codec）。"""
        from src.features.edit.insights.diff import save_snapshot_file

        if self._snapshot is None:
            return False
        save_snapshot_file(self._snapshot, path)
        return True

    def load_snapshot_slot(self, slot: str, path: str) -> bool:
        """打开快照到 A/B 槽位（hash/schema 校验失败返回 False 并提示）。"""
        from src.features.edit.insights.diff import load_snapshot_file

        result = load_snapshot_file(path)
        if not result.ok or result.snapshot is None:
            self.diff_slots_label.setText(
                _tr("diff.load_failed", "{slot} load failed: {e}").format(
                    slot=slot, e=result.error.message if result.error else "?"
                )
            )
            return False
        if slot == "A":
            self._snapshot_a = result.snapshot
        else:
            self._snapshot_b = result.snapshot
        self._refresh_diff_slots_label()
        return True

    def _refresh_diff_slots_label(self) -> None:
        def cap(s: InsightSnapshot | None) -> str:
            return s.capture_id[:8] if s else "—"

        self.diff_slots_label.setText(f"A: {cap(self._snapshot_a)} · B: {cap(self._snapshot_b)}")

    def run_compare(self):
        """比较 A ↔ B；缺槽位返回 None。结果为只读 Markdown（不恢复/不合并）。"""
        from src.features.edit.insights.diff import (
            compare_snapshots,
            render_diff_markdown,
        )

        if self._snapshot_a is None or self._snapshot_b is None:
            self.diff_view.setPlainText(
                _tr("diff.need_two", "load snapshot A and B first")
            )
            return None
        diff = compare_snapshots(self._snapshot_a, self._snapshot_b)
        self.diff_view.setPlainText(render_diff_markdown(diff))
        return diff

    def _on_diff_save_clicked(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        if self._snapshot is None:
            self.diff_view.setPlainText(
                _tr("status.refresh_first", "refresh capture first")
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, _tr("diff.save_title", "Save Insight Snapshot"),
            "insight_snapshot.json", "JSON (*.json)",
        )
        if path:
            self.save_current_snapshot(path)

    def _on_diff_open_clicked(self, slot: str) -> None:
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, _tr("diff.open_title", "Open Insight Snapshot"), "", "JSON (*.json)",
        )
        if path:
            self.load_snapshot_slot(slot, path)

    def _on_diff_compare_clicked(self) -> None:
        self.run_compare()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._worker.shutdown(timeout=5.0)
        self.closed.emit()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    # 供宿主（T02/G1）请求缩略图时复用闸门口径（T13 实装）
    def request_thumbnail(self, request: ThumbnailRequest):
        return self._host.request_thumbnail(request)
