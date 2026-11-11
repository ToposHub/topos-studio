"""Reproduce isolated UI screenshots and probe the user's local video fixtures.

Run from the repository: QT_QPA_PLATFORM=offscreen .venv/bin/python
docs/validation/color_warper_20261112/render_ui.py
This does not process or grade video, synthesize density clouds, or edit projects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import av  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from src.features.color.ui.warper_panel import WarperPanel  # noqa: E402
from src.shared.i18n.i18n import I18N  # noqa: E402
from src.shared.ui.style_sheet import DARK_THEME  # noqa: E402


def capture(widget: QWidget, name: str) -> None:
    QTest.qWait(50)
    widget.repaint()
    if not widget.grab().save(str(Path(__file__).parent / name)):
        raise RuntimeError(f"Could not save {name}")


def probe_media() -> list[dict[str, str | int]]:
    results = []
    for name in ("2K_h264.mp4", "2K_prores422.mov", "2K_DNxHR HQX 10bit.mov"):
        path = ROOT / "tests" / "test_videos" / name
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            frame = next(container.decode(stream))
            results.append({"path": str(path.relative_to(ROOT)), "codec": stream.codec_context.name,
                            "width": frame.width, "height": frame.height, "pixel_format": frame.format.name})
    return results


def main() -> None:
    app = QApplication([])
    app.setStyleSheet(DARK_THEME)
    I18N.initialize()
    I18N.load_locale("en_US")
    panel = WarperPanel()
    panel.resize(877, 425)
    panel.show()
    capture(panel, "hue_saturation.png")
    panel.warper.selected = {(2, 1), (3, 1)}
    panel.warper.pins = {(5, 0), (5, 2)}
    panel._sync_tools()
    panel.warper.update()
    capture(panel, "hue_saturation_selected.png")
    panel.set_mode("chroma_luma")
    capture(panel, "chroma_luma.png")
    panel.resize(640, 425)
    capture(panel, "chroma_luma_narrow.png")
    panel.resize(877, 425)
    panel.set_mode("chroma_warp")
    capture(panel, "chroma_warp.png")
    panel.chroma_warp._finish_stroke([[0.32, 0.32], [0.38, 0.36], [0.46, 0.4], [0.6, 0.43]])
    panel.chroma_warp.pins.append({"position": [0.3, 0.7], "parameters": dict(panel.chroma_warp.parameters)})
    panel.chroma_warp.update()
    capture(panel, "chroma_warp_selected.png")
    panel.set_mode("hue_saturation")
    panel.toggle_expanded()
    capture(panel._dialog, "expanded.png")
    panel.toggle_expanded()
    panel.close()

    I18N.load_locale("zh_CN")
    chinese = WarperPanel()
    chinese.resize(877, 425)
    chinese.show()
    capture(chinese, "hue_saturation_zh.png")
    chinese.close()
    media = probe_media()
    (Path(__file__).parent / "media_probe.json").write_text(json.dumps(media, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(media, ensure_ascii=False))


if __name__ == "__main__":
    main()
