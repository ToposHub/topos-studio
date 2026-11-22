"""Standalone 3D viewer and optional Topos Protocol v1 adapter."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCloseEvent, QSurfaceFormat
from PyQt6.QtWidgets import QApplication

from topos_app_sdk import AppBridge, parse_args
from quick_3d_viewer import QuickModelViewer, SUPPORTED_EXTS


class HostedModelViewer(QuickModelViewer):
    """Adapt request/result boundaries while retaining the existing viewer."""

    def __init__(self, bridge: AppBridge) -> None:
        self.bridge = bridge
        self._host_requests: dict[int, str] = {}
        self._opening_request = ""
        self._current_request = ""
        super().__init__()
        bridge.command_received.connect(self.handle_command)

    def handle_command(self, command: dict[str, Any]) -> None:
        request_id = command["request_id"]
        name = command["command"]
        if name == "activate":
            self.showNormal()
            self.raise_()
            self.activateWindow()
        elif name == "close":
            self._current_request = request_id
            self.close()
        elif name == "open":
            path = command["path"]
            if not os.path.isfile(path):
                self.bridge.emit_event("error", request_id, code="FILE_NOT_FOUND", message="模型不存在", path=path)
                return
            extension = Path(path).suffix.lower()
            if extension not in SUPPORTED_EXTS:
                self.bridge.emit_event("error", request_id, code="UNSUPPORTED_FORMAT", message="不支持的模型格式", path=path)
                return
            if extension == ".fbx" and importlib.util.find_spec("assimp_py") is None:
                self.bridge.emit_event("error", request_id, code="DEPENDENCY_MISSING",
                                       message="FBX 预览需要 assimp-py", path=path)
                return
            self._opening_request = request_id
            try:
                self.load_model(path)
            finally:
                self._opening_request = ""
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def load_model(self, filepath: str, preserve_camera: bool = False) -> None:
        if filepath and os.path.isfile(filepath):
            for previous in tuple(self._host_requests.values()):
                if previous:
                    self.bridge.emit_event("error", previous, code="OPEN_SUPERSEDED", message="模型打开请求已被替换")
            self._host_requests.clear()
            self._host_requests[self._load_req_id + 1] = self._opening_request
        super().load_model(filepath, preserve_camera)

    def _on_model_loaded(self, req_id: int, filepath: str, result: Any, error: str) -> None:
        current = req_id == self._load_req_id
        request_id = self._host_requests.pop(req_id, "")
        super()._on_model_loaded(req_id, filepath, result, error)
        if not current:
            return
        if error or not result:
            self.bridge.emit_event("error", request_id, code="OPEN_FAILED",
                                   message=error or "模型不包含有效几何体", path=filepath)
            return
        self._current_request = ""
        self.bridge.emit_event("opened", request_id, path=str(Path(filepath).resolve()))

    def _on_worker_finished(self, worker: Any) -> None:
        missing_result = (worker.req_id == self._load_req_id and not worker.result_emitted
                          and not worker.is_cancelled())
        super()._on_worker_finished(worker)
        if missing_result:
            request_id = self._host_requests.pop(worker.req_id, "")
            self.bridge.emit_event("error", request_id, code="OPEN_FAILED", message="模型解析进程未返回结果")

    def _notify_export(self, path: str) -> None:
        self.bridge.emit_event("exported", self._current_request,
                               paths=[str(Path(path).resolve())], source_path=self.current_file_path or "")

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        if event.isAccepted():
            self.bridge.emit_event("closed", self._current_request)
            self.bridge.detach()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Topos 3D Viewer")
    bridge = AppBridge(args.topos_session, app)
    viewer = HostedModelViewer(bridge)
    viewer.show()
    bridge.start()
    bridge.emit_event("ready")
    if args.file:
        QTimer.singleShot(0, lambda: viewer.handle_command(
            {"command": "open", "request_id": "", "path": str(Path(args.file).resolve())}))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
