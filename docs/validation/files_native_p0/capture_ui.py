"""P0 验收截图：原生 Files 闭环（找素材 → 看清内容 → 多选 → 预览 → 标记）。

数据为**展示用固定样本**，不是真实后端扫描结果；真实服务联调见 README 的
验收矩阵与测试命令。运行：

    PYTHONPATH=. .venv/bin/python docs/validation/files_native_p0/capture_ui.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QItemSelectionModel, QPointF, QSettings, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import QApplication

from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.shared.ui.style_sheet import DARK_THEME


def _sample_image(width: int = 1600, height: int = 900) -> bytes:
    """生成一张带渐变的样张，让预览浮层显示的是真实解码结果。"""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    gradient = QLinearGradient(QPointF(0, 0), QPointF(width, height))
    gradient.setColorAt(0.0, QColor("#1b2a4a"))
    gradient.setColorAt(0.45, QColor("#3a7bd5"))
    gradient.setColorAt(1.0, QColor("#e07a3a"))
    painter = QPainter(image)
    painter.fillRect(image.rect(), gradient)
    painter.setPen(QColor("#f2f2f2"))
    font = QFont()
    font.setPointSize(64)
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "Topos · P0 预览")
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def _entries():
    rows = [
        ("Interview", "dir", {}),
        ("2026-11-16_Camera_A_001.mov", "video", {
            "size_bytes": 1_340_000_000, "rating": 4, "tags": "访谈,A机位",
            "comments": "主机位 · 正面镜头", "duration": 92.4,
            "width": 3840, "height": 2160, "codec": "ProRes 422",
            "cached_info": "3840x2160 · ProRes 422 · 25.00fps · 120 Mbps",
        }),
        ("2026-11-16_Camera_B_001.mov", "video", {
            "size_bytes": 1_030_000_000, "duration": 92.4,
            "width": 3840, "height": 2160, "codec": "ProRes 422",
            "cached_info": "3840x2160 · ProRes 422 · 25.00fps",
        }),
        ("Ambient_room.wav", "audio", {"size_bytes": 90_000_000, "duration": 186.0}),
        ("Cover_v2.png", "image", {
            "size_bytes": 12_600_000, "width": 1600, "height": 900, "codec": "PNG",
            "rating": 5, "tags": "封面,待定",
            "comments": "备选封面",
        }),
        ("Reference_frame.jpg", "image", {"size_bytes": 4_200_000, "width": 1920, "height": 1080}),
        ("Voice_take_01.wav", "audio", {"size_bytes": 22_000_000, "duration": 64.0}),
        ("Shot_notes.pdf", "doc", {"size_bytes": 820_000}),
        ("Offline_ghost.mov", "video", {"size_bytes": 0, "missing": 1}),
    ]
    entries = []
    for name, kind, extra in rows:
        entry = {"name": name, "path": "/Volumes/Media/" + name, "type": kind}
        entry.update(extra)
        entries.append(entry)
    return entries


def _select(panel: NativeFilesPanel, name: str) -> None:
    for row in range(panel.proxy.rowCount()):
        index = panel.proxy.index(row, 0)
        if index.data(Qt.ItemDataRole.DisplayRole) == name:
            panel.selection.select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows)
            return


def main() -> None:
    app = QApplication([])
    app.setStyleSheet(DARK_THEME)
    ini = Path(tempfile.mkdtemp()) / "files_native_p0.ini"
    prefs = NativeFilesPreferences(QSettings(str(ini), QSettings.Format.IniFormat))
    panel = NativeFilesPanel(home_path="/Users/creator", preferences=prefs)
    panel.locations.set_children(panel.home_path, [
        {"name": name, "path": panel.home_path + "/" + name}
        for name in ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures")
    ])
    panel.start_home_tree()
    panel.set_entries("/Volumes/Media", _entries())
    panel.resize(1180, 700)
    panel.show()
    app.processEvents()

    output = Path(__file__).parent
    _select(panel, "2026-11-16_Camera_A_001.mov")
    app.processEvents()
    panel.grab().save(str(output / "native_files_p0_inspector.png"))

    # 多选：技术信息聚合成"文件数 / 总大小 / 合计时长 / 类型数"
    panel.selection.select(
        panel.proxy.index(2, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    app.processEvents()
    panel.grab().save(str(output / "native_files_p0_multiselect.png"))

    # 预览浮层：走真实字节 → 后台解码 → QImage 的路径
    _select(panel, "Cover_v2.png")
    panel.preview_selected()
    panel.show_preview_bytes("/Volumes/Media/Cover_v2.png", _sample_image())
    for _ in range(40):
        app.processEvents()
        if panel._preview._image is not None:
            break
    app.processEvents()
    panel.grab().save(str(output / "native_files_p0_preview.png"))

    # 原尺寸 1:1（可滚动）
    panel._preview.toggle_fit()
    app.processEvents()
    panel.grab().save(str(output / "native_files_p0_preview_actual.png"))
    panel.close_preview()
    app.processEvents()

    panel.view_combo.setCurrentIndex(1)
    app.processEvents()
    panel.grab().save(str(output / "native_files_p0_list.png"))

    panel.resize(760, 560)
    app.processEvents()
    panel.grab().save(str(output / "native_files_p0_narrow.png"))
    panel.close()
    print("captured:", ", ".join(sorted(path.name for path in output.glob("*.png"))))


if __name__ == "__main__":
    main()
