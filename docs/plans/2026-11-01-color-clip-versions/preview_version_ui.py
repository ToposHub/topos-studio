#!/usr/bin/env python3
"""Standalone UI demo. In-memory display metadata only; never opens a project.

.venv/bin/python docs/plans/2026-11-01-color-clip-versions/preview_version_ui.py
Add --capture <directory> to render the actual widgets and exit.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QVBoxLayout, QWidget

from src.features.color.ui.clip_version_ui import (
    ClipVersionCollection, ClipVersionRequest, ClipVersionResult, ClipVersionSummary,
    VersionActionDialog, VersionOperation as Op, VersionScope as Scope,
)
from src.features.color.ui.timeline_clip_strip import BottomLabelMode, ColorClipEntry, TimelineClipStrip
from src.shared.i18n.i18n import I18N


class VersionUiDemo(QWidget):
    """Fake acknowledgements stay in this demo, outside production UI."""

    def __init__(self, disconnected: bool) -> None:
        super().__init__()
        self.setWindowTitle("Topos · 片段版本交互演示（演示数据，不保存工程）")
        self.resize(820, 245)
        self.setStyleSheet("QWidget { background:#242426; color:#dddddf; }")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("演示数据 · 右键片段管理版本；双击底部切换编码／版本名。不会保存工程。"))
        self.strip = TimelineClipStrip()
        layout.addWidget(self.strip)
        self.status = QLabel("状态：演示模式" if not disconnected else "状态：未接线模式，提交不会保存")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.strip._current_project = SimpleNamespace(project_id="demo-project")
        self.strip.version_ui.bind_context("demo-project", "demo-timeline")
        self.strip._bottom_mode = BottomLabelMode.VERSION
        self.strip._entries = self.strip._all_entries = [
            ColorClipEntry("demo-timeline", f"clip-{i}", "v1", "V1", f"asset-{i}",
                           f"演示片段 {i:02d}", f"demo-{i}.mp4", "video", (i-1)*48, 48, 0, 48,
                           f"stack-{i}", False, False, "Demo") for i in range(1, 5)
        ]
        self.strip._rebuild_cards()
        self.states = {}
        for i, card in enumerate(self.strip._cards):
            pixmap = QPixmap(160, 90)
            painter = QPainter(pixmap)
            gradient = QLinearGradient(0.0, 0.0, 160.0, 90.0)
            gradient.setColorAt(0.0, QColor(("#557c75", "#70698b", "#9c7958", "#637b93")[i]))
            gradient.setColorAt(1.0, QColor("#20282f"))
            painter.fillRect(pixmap.rect(), gradient)
            painter.setPen(QColor("#eeeeee"))
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, f"DEMO {i+1:02d}")
            painter.end()
            card.set_preview_override(pixmap)
            target = self.strip._version_target(card.entry)
            for scope in Scope:
                items = (ClipVersionSummary("base", "版本 1", True), ClipVersionSummary("warm", "暖色方案")) if scope == Scope.LOCAL else ()
                state = ClipVersionCollection(target, scope, items, "base" if items else None,
                                              loaded=True, connected=not disconnected,
                                              in_use=scope == Scope.LOCAL)
                self.states[(target, scope)] = state
                self.strip.set_clip_version_collection(state)
        self.strip.set_active_clip("clip-1")
        self.strip.version_state_requested.connect(lambda target, scope: self.strip.set_clip_version_collection(self.states[(target, scope)]))
        self.strip.version_operation_requested.connect(self._request)
        self.strip.version_ui.feedback.connect(self.status.setText)

    def _request(self, request: ClipVersionRequest) -> None:
        self.status.setText(f"演示请求：{request.operation.value} · {request.target.clip_id}")
        QTimer.singleShot(120, lambda: self._ack(request))

    def _ack(self, request: ClipVersionRequest) -> None:
        key = (request.target, request.scope)
        state = self.states[key]
        items = list(state.versions)
        active = state.active_version_id
        if request.operation in (Op.CREATE, Op.DUPLICATE):
            item = ClipVersionSummary(str(uuid4()), request.name)
            items.append(item)
            if request.operation == Op.CREATE:
                active = item.version_id
        elif request.operation == Op.LOAD:
            active = request.version_id
        elif request.operation == Op.RENAME:
            items = [replace(item, name=request.name) if item.version_id == request.version_id else item for item in items]
        elif request.operation == Op.DELETE:
            items = [item for item in items if item.version_id != request.version_id]
        elif request.operation == Op.DELETE_ALL:
            items = [item for item in items if item.is_default]
            active = items[0].version_id if items else None
        elif request.operation == Op.COPY_TO_SCOPE:
            other_key = (request.target, request.destination_scope)
            other = self.states[other_key]
            copied = ClipVersionSummary(str(uuid4()), request.name, is_default=not other.versions)
            other = replace(other, versions=other.versions + (copied,), revision=other.revision+1)
            self.states[other_key] = other
            self.strip.set_clip_version_collection(other)
        state = replace(state, versions=tuple(items), active_version_id=active,
                        revision=state.revision+1, in_use=True)
        self.states[key] = state
        self.strip.finish_clip_version_operation(ClipVersionResult(request.request_id, request.target, state))
        self.status.setText("演示回填完成 · 仅更新界面演示数据，没有写入工程或真实调色栈")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--disconnected", action="store_true")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    I18N.load_locale("zh_CN")
    I18N.load_module_translations(str(ROOT / "src/features/color/locales"))
    window = VersionUiDemo(args.disconnected)
    window.show()
    if args.capture:
        args.capture.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            app.processEvents()
        window.grab().save(str(args.capture / "version-strip.png"))
        target = window.strip._version_target(window.strip._entries[0])
        root_menu = QMenu(window)
        root_menu.setStyleSheet("QMenu { background:#242426; color:#dddddf; } QMenu::item { padding:6px 20px; }")
        window.strip.version_ui.add_menus(root_menu, target)
        local = root_menu.actions()[0].menu()
        local.ensurePolished()
        local.resize(local.sizeHint())
        local.grab().save(str(args.capture / "version-menu.png"))
        dialog = VersionActionDialog(window.strip.version_ui, target, Scope.LOCAL, Op.CREATE, parent=window)
        dialog.show()
        app.processEvents()
        dialog.grab().save(str(args.capture / "version-create.png"))
        window.close()
        return 0
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
