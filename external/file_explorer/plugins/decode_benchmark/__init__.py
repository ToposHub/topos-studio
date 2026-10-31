"""Topos Decode Speed Test file-explorer plugin.

The plugin adds a launcher to the file-explorer plugin menu and the video
context menu.  The actual benchmark window is created on the Qt main thread;
decoding itself runs in a QThread and uses the host application's
``SourceManager`` pipeline.
"""

from __future__ import annotations

from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request
from PyQt6.QtCore import QObject, pyqtSignal

NAME = "Topos Decode Speed Test"
VERSION = "1.1.0"
AUTHOR = "Topos Color"
DESCRIPTION = "支持 SourceManager 离线解码及当前 Edit 时间线真实解码 / GPU 合成 / 呈现跑分"

bp = Blueprint("decode_benchmark", __name__)


class GuiLauncher(QObject):
    """Bridge Flask requests to Qt's main thread."""

    launch_signal = pyqtSignal(list)

    def __init__(self, host_bridge: Any = None) -> None:
        super().__init__()
        self._host_bridge = host_bridge
        self.window: Optional[Any] = None
        self.launch_signal.connect(self._open_on_main_thread)

    def _open_on_main_thread(self, paths: list[str]) -> None:
        from .benchmark_gui import DecodeBenchmarkWindow

        try:
            if self.window is None:
                self.window = DecodeBenchmarkWindow(paths, host_bridge=self._host_bridge)
            else:
                self.window.add_paths(paths)
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
        except RuntimeError:
            # The Qt C++ object can be deleted by the host while Python still
            # holds a reference. Recreate it on the next launch.
            self.window = DecodeBenchmarkWindow(paths, host_bridge=self._host_bridge)
            self.window.show()


_launcher: GuiLauncher | None = None


def _normalise_paths(payload: object) -> list[str]:
    """Validate launcher payload and keep only string paths."""

    if not isinstance(payload, dict):
        return []
    raw_paths = payload.get("paths")
    if not isinstance(raw_paths, list):
        raw_paths = [payload.get("path")]
    return [str(path) for path in raw_paths if isinstance(path, str) and path.strip()]


def setup(context: Any) -> None:
    """Register menu entries and the small browser-side launcher helper."""

    global _launcher
    _launcher = GuiLauncher(getattr(context, "socket", None))
    context.register_js("/static_plugin/decode_benchmark/main.js")
    context.register_ui_component(
        "top_menu",
        """
        <div class="dropdown-item" onclick="DecodeBenchmarkPlugin.open(null)">
            <i class="bi bi-speedometer2 me-2 text-success"></i>解码跑分 / Decode Speed Test
        </div>
        """,
    )
    context.register_ui_component(
        "context_menu",
        """
        <div class="menu-divider"></div>
        <div class="menu-item" onclick="DecodeBenchmarkPlugin.open(window.STATE.contextTarget ? window.STATE.contextTarget.path : null)">
            <i class="bi bi-speedometer2 me-2 text-success"></i>用 Topos 解码跑分
        </div>
        """,
    )


@bp.route("/launch", methods=["POST"])
def launch() -> Response | tuple[Response, int]:
    """Open/focus the benchmark window with optional selected files."""

    if _launcher is None:
        return jsonify({"status": "error", "error": "插件尚未初始化"}), 500
    paths = _normalise_paths(request.get_json(silent=True) or {})
    _launcher.launch_signal.emit(paths)
    return jsonify({"status": "ok", "paths": paths})


@bp.route("/capabilities", methods=["GET"])
def capabilities() -> Response:
    """Expose the supported matrix for diagnostics and future browser UI."""

    from .benchmark import RESOLUTION_BUCKETS, SUPPORTED_CODECS

    return jsonify(
        {
            "codecs": [label for label, _aliases in SUPPORTED_CODECS],
            "resolutions": [label for label, _limit in RESOLUTION_BUCKETS],
            "modes": ["proxy", "master", "edit_realtime"],
        }
    )


__all__ = [
    "AUTHOR",
    "DESCRIPTION",
    "NAME",
    "VERSION",
    "bp",
    "setup",
]
