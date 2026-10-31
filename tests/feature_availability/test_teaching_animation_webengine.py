"""Real Qt WebEngine/QWebChannel Phase 0 bridge verification."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.webengine
def test_webengine_preview_bridge_connects_and_updates_dom():
    """Start a real WebEngine page and verify the frame reaches JavaScript."""
    project_root = Path(__file__).resolve().parents[2]
    script = r'''
import sys
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.facade import preview_frame
from src.features.teaching_animation.ui.preview_bridge import WebEnginePreviewBridge

app = QApplication.instance() or QApplication(sys.argv)
result = preview_frame({"title": "WebEngine", "arrow_color": "#2563eb"}, 9,
                       RenderContext(fps=24.0, width=320, height=180))
if not result.ok:
    raise RuntimeError(result.error)

bridge = WebEnginePreviewBridge()
ready_loop = QEventLoop()
bridge.channel_ready.connect(ready_loop.quit)
bridge.update_frame(result.value)
QTimer.singleShot(10000, ready_loop.quit)
ready_loop.exec()
if not bridge.is_ready:
    raise RuntimeError("QWebChannel did not connect")

dom_value = []
dom_loop = QEventLoop()
bridge.view.page().runJavaScript(
    "document.getElementById('scene').dataset.frame",
    lambda value: (dom_value.append(value), dom_loop.quit()),
)
QTimer.singleShot(5000, dom_loop.quit)
dom_loop.exec()
if dom_value != ["9"]:
    raise RuntimeError(f"unexpected DOM frame: {dom_value!r}")

bridge.close()
app.processEvents()
'''
    env = os.environ.copy()
    env.update({
        "QT_QPA_PLATFORM": "offscreen",
        "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --disable-gpu",
        "TOPOS_TEST_MODE": "1",
        "PYTHONPATH": str(project_root),
    })
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert completed.returncode == 0, (
        f"WebEngine bridge subprocess failed\nstdout:\n{completed.stdout}"
        f"\nstderr:\n{completed.stderr}"
    )
