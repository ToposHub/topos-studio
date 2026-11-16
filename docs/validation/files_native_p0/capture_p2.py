"""F13/F15 展示夹具截图：批改名预览表、并排比较。"""
from __future__ import annotations

import os

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from src.app.ui.native_files_batch_rename import BatchRenameDialog
from src.app.ui.native_files_compare import CompareDialog

OUT = os.path.dirname(__file__)


def _image(color: str, label: str) -> bytes:
    image = QImage(640, 400, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    painter = QPainter(image)
    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setPointSize(36)
    painter.setFont(font)
    painter.drawText(image.rect(), 0x84, label)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def main() -> None:
    app = QApplication([])
    batch = BatchRenameDialog([f"/Volumes/Media/shot_{i}.mov" for i in range(1, 6)])
    batch.show_preview({"plan": [
        {"src_name": f"shot_{i}.mov", "dst_name": f"Interview_A_{i:03d}.mov",
         "conflict": i == 3} for i in range(1, 6)], "errors": []})
    batch.show()
    app.processEvents()
    batch.grab().save(os.path.join(OUT, "native_files_p0_batch_rename.png"))

    compare = CompareDialog(["/Volumes/Media/A.png", "/Volumes/Media/B.png"])
    compare.resize(1100, 640)
    compare.show()
    app.processEvents()
    compare.set_image("/Volumes/Media/A.png", _image("#1f3b5c", "A · 正面光"))
    compare.set_image("/Volumes/Media/B.png", _image("#5c3a1f", "B · 侧逆光"))
    compare._set_zoom(0.7)
    app.processEvents()
    compare.grab().save(os.path.join(OUT, "native_files_p0_compare.png"))
    print("captured: batch_rename, compare")


if __name__ == "__main__":
    main()
