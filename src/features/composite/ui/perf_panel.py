"""
PerfPanel - Performance diagnostics panel for the composite node graph.

Shows per-node timing, cache stats, VRAM, request latency, and dropped requests.
Updates on each frame_ready signal. Text is selectable and copyable. A bounded
frame trace can be exported as JSON for before/after performance comparison.
Right-click context menu allows floating/docking the panel.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QPushButton, QLabel, QMenu, QFileDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from collections import deque
import json
from typing import Callable, Optional

from src.features.composite.utils.perf_monitor import (
    PerfSnapshot,
    build_perf_report,
)
from src.shared.i18n.i18n import I18N

# AUD-10809: refresh the cached trace-sample count roughly once per second at
# a 60 fps update rate instead of on every frame.
_TRACE_COUNT_REFRESH_INTERVAL = 60


class PerfPanel(QWidget):
    """Dockable performance diagnostics panel.

    Supports right-click context menu for floating/docking the panel.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._snapshot: Optional[PerfSnapshot] = None
        self._history_provider: Optional[Callable[[], list[PerfSnapshot]]] = None
        self._history = deque(maxlen=120)
        # AUD-10809: the provider hands back a full list copy; polling it on
        # every frame just to show one count is waste. Cache the count and
        # refresh it at a low rate (and before every export) instead.
        self._trace_count = 0
        self._trace_count_refresh_pending = 0
        self._init_ui()

    def _init_ui(self) -> None:
        # Enable custom context menu
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Header
        header = QLabel(I18N.tr("panel.performance", "Performance"))
        header.setStyleSheet("color: #aaa; font-weight: bold; font-size: 11px;")
        layout.addWidget(header)

        # Read-only text display
        self._text_edit = QPlainTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setFont(QFont("Menlo", 10))
        self._text_edit.setStyleSheet(
            "background-color: #1e1e1e; color: #ccc; border: none;"
        )
        self._text_edit.setMaximumHeight(300)
        layout.addWidget(self._text_edit)

        # Copy button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        copy_btn = QPushButton(I18N.tr("perf.copy", "Copy"))
        copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #444; color: #ccc; border: 1px solid #555;
                padding: 2px 10px; border-radius: 3px; font-size: 11px;
            }
            QPushButton:hover { background-color: #555; }
            QPushButton:pressed { background-color: #333; }
        """)
        copy_btn.clicked.connect(self._copy_to_clipboard)
        btn_layout.addWidget(copy_btn)
        export_btn = QPushButton(I18N.tr("perf.export_json", "Export JSON"))
        export_btn.setStyleSheet(copy_btn.styleSheet())
        export_btn.clicked.connect(self._choose_report_path)
        btn_layout.addWidget(export_btn)
        layout.addLayout(btn_layout)

    def set_history_provider(
        self, provider: Optional[Callable[[], list[PerfSnapshot]]]
    ) -> None:
        """Use the render monitor's completed-frame history for export."""
        self._history_provider = provider

    def _trace_snapshots(self) -> list[PerfSnapshot]:
        provider = self._history_provider
        if callable(provider):
            try:
                return list(provider())
            except Exception:
                # The panel must remain usable if the renderer is being torn
                # down while the user opens the export action.
                pass
        return list(self._history)

    def refresh_trace_count(self) -> int:
        """Re-read the provider's sample count into the cached display value.

        AUD-10809: isolated so the per-frame path and the export path share
        one refresh point without copying the whole history every frame.
        """
        self._trace_count = len(self._trace_snapshots())
        return self._trace_count

    def export_report(self, path: str) -> bool:
        """Write the current bounded performance trace to ``path``."""
        if not path:
            return False
        try:
            snapshots = self._trace_snapshots()
            report = build_perf_report(snapshots)
            # The exported count must be exact even between low-rate refreshes.
            self._trace_count = len(snapshots)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _choose_report_path(self) -> None:
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Performance Report",
            "composite_perf_report.json",
            "JSON files (*.json)",
        )
        if path:
            self.export_report(path)

    def update_from_snapshot(self, snapshot: PerfSnapshot) -> None:
        """Format and display perf data from a PerfSnapshot."""
        self._snapshot = snapshot
        self._history.append(snapshot)
        lines = []

        # Frame summary
        if snapshot.frame:
            f = snapshot.frame
            lines.append(f"=== Frame #{f.request_id} ===")
            lines.append(f"Eval time:       {f.total_eval_ms:.1f} ms")
            lines.append(f"Request latency: {f.request_latency_ms:.1f} ms")
            lines.append(f"Cache hit rate:  {f.cache_hit_rate * 100:.0f}%")
            lines.append(f"Active textures: {f.active_textures}")
            lines.append(f"VRAM estimate:   {f.vram_mb:.1f} MB")
            if f.dropped_requests > 0:
                lines.append(f"Dropped requests: {f.dropped_requests}")
            lines.append("")

        # Per-node breakdown (sorted by eval time descending)
        if snapshot.nodes:
            lines.append("--- Per-Node Timing (sorted) ---")
            sorted_nodes = sorted(snapshot.nodes.values(),
                                  key=lambda n: n.eval_time_ms, reverse=True)
            for ns in sorted_nodes:
                flags = ""
                if ns.was_cached:
                    flags += " [CACHED]"
                if ns.was_dirty:
                    flags += " [DIRTY]"
                if ns.error:
                    flags += f" [ERROR: {ns.error}]"
                lines.append(
                    f"  {ns.node_type:15s} {ns.node_id[:12]:12s}  "
                    f"{ns.eval_time_ms:7.2f} ms{flags}"
                )
            lines.append("")

        # Cache stats
        total = snapshot.cache_hits + snapshot.cache_misses
        rate = (snapshot.cache_hits / total * 100) if total > 0 else 0
        lines.append("--- Cache Stats ---")
        lines.append(
            f"  Hits: {snapshot.cache_hits}  "
            f"Misses: {snapshot.cache_misses}  "
            f"Evictions: {snapshot.cache_evictions}  "
            f"Rate: {rate:.0f}%"
        )

        # F12：3D 渲染统计（Scene3D record_3d_stats 填充；非 3D 帧无此段）
        if snapshot.render3d is not None:
            lines.append("")
            lines.append("--- 3D Render Stats ---")
            for stat_line in snapshot.render3d.summary_lines():
                lines.append(f"  {stat_line}")

        lines.append("")
        # AUD-10809: show the cached sample count. Polling the provider (a
        # full list copy) on every frame for one number is waste — refresh it
        # at a low rate here, and always exactly on export.
        self._trace_count_refresh_pending -= 1
        if self._trace_count_refresh_pending <= 0:
            self._trace_count_refresh_pending = _TRACE_COUNT_REFRESH_INTERVAL
            self.refresh_trace_count()
        lines.append(f"Trace samples: {self._trace_count}")

        self._text_edit.setPlainText("\n".join(lines))

    def _copy_to_clipboard(self) -> None:
        from PyQt6.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        text = self._text_edit.toPlainText()
        if text:
            clipboard.setText(text)

    # ── Context Menu ──

    def _show_context_menu(self, pos) -> None:
        """Show context menu with float/dock toggle."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #3a3a3a;
                color: #ccc;
                border: 1px solid #555;
                padding: 4px;
                font-size: 12px;
            }
            QMenu::item {
                padding: 4px 20px;
            }
            QMenu::item:selected {
                background-color: #cc6600;
            }
        """)

        dock = self._find_parent_dock()
        if dock:
            if dock.isFloating():
                action = menu.addAction(I18N.tr("perf.dock_panel", "Dock Panel"))
            else:
                action = menu.addAction(I18N.tr("perf.float_panel", "Float Panel"))
            triggered = menu.exec(self.mapToGlobal(pos))
            if triggered == action:
                dock.setFloating(not dock.isFloating())

    def _find_parent_dock(self):
        """Walk up widget hierarchy to find parent QDockWidget."""
        from PyQt6.QtWidgets import QDockWidget
        widget = self.parent()
        while widget:
            if isinstance(widget, QDockWidget):
                return widget
            widget = widget.parent()
        return None
