"""Application-owned protocol/adapter tests; no host imports in the apps."""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import pickle
import select
import subprocess
import sys
import time
from types import ModuleType
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication
import pytest

ROOT = Path(__file__).resolve().parents[2]
WORD = ROOT / "extensions" / "topos_word"
VIEWER = ROOT / "extensions" / "quick_3d_viewer"


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def app(qapp: QApplication) -> QApplication:
    # Keep the session-owned QApplication alive: destroying a module-local
    # instance also deletes unrelated Qt bridges collected by legacy tests.
    return qapp


@pytest.fixture
def sdk() -> Any:
    return load_module(WORD / "topos_app_sdk.py", "independent_sdk_test")


def command(name: str, request_id: str = "request", **payload: Any) -> bytes:
    return (json.dumps({"protocol": 1, "session_id": "session", "command": name,
                        "request_id": request_id, **payload}) + "\n").encode()


def pump(app: QApplication, condition: Any, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert condition()


def test_standalone_arguments_and_no_host_imports(sdk: Any) -> None:
    assert sdk.parse_args([]).file is None
    assert sdk.parse_args(["中文 文件.docx"]).file == "中文 文件.docx"
    assert sdk.parse_args(["--topos-session", "abc"]).topos_session == "abc"
    assert (WORD / "topos_app_sdk.py").read_bytes() == (VIEWER / "topos_app_sdk.py").read_bytes()
    for root in (WORD, VIEWER):
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "src" and not (node.module or "").startswith("src.")
                if isinstance(node, ast.Import):
                    assert not any(alias.name == "src" or alias.name.startswith("src.") for alias in node.names)


def test_commands_run_on_gui_thread_and_eof_detaches(app: QApplication, sdk: Any) -> None:
    output = io.StringIO()
    bridge = sdk.AppBridge("session", app,
                           input_stream=io.BytesIO(command("open", path="中文 文件.docx")), output_stream=output)
    received: list[dict[str, Any]] = []
    bridge.command_received.connect(received.append)
    bridge.start()
    pump(app, lambda: not bridge.connected)
    assert len(received) == 1 and received[0]["path"] == "中文 文件.docx"
    bridge.emit_event("saved", path="ignored.docx")
    assert output.getvalue() == ""


@pytest.mark.parametrize("bad", [b"not json\n", b"[]\n", b'\xff\n',
    b'{"protocol":true,"session_id":"session","command":"close","request_id":"x"}\n',
    b'{"protocol":1,"session_id":"wrong","command":"close","request_id":"x"}\n',
    command("open", path=5), command("open", path="bad\x00path"), command("launch"),
    command([], path="ignored"), command("open", path="\ud800"),
    command("close", request_id="\ud800"), b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"])
def test_bad_commands_report_error_without_dispatch(app: QApplication, sdk: Any, bad: bytes) -> None:
    output = io.StringIO()
    bridge = sdk.AppBridge("session", app, input_stream=io.BytesIO(bad), output_stream=output)
    received: list[dict[str, Any]] = []
    bridge.command_received.connect(received.append)
    bridge.start()
    pump(app, lambda: not bridge.connected)
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(messages) == 1 and messages[0]["event"] == "error"
    assert received == []


def test_detach_and_oversize_are_bounded(app: QApplication, sdk: Any) -> None:
    for data in (command("detach") + command("close"), b"x" * 65_537):
        output = io.StringIO()
        bridge = sdk.AppBridge("session", app, input_stream=io.BytesIO(data), output_stream=output)
        received: list[dict[str, Any]] = []
        bridge.command_received.connect(received.append)
        bridge.start()
        pump(app, lambda: not bridge.connected)
        assert not received


def test_event_limits_and_identity(app: QApplication, sdk: Any) -> None:
    output = io.StringIO()
    bridge = sdk.AppBridge("session", app, input_stream=io.BytesIO(), output_stream=output)
    bridge.emit_event("saved", "req", path="中文 文件.docx")
    assert json.loads(output.getvalue()) == {"protocol": 1, "session_id": "session",
        "event": "saved", "request_id": "req", "path": "中文 文件.docx"}
    with pytest.raises(ValueError, match="maximum"):
        bridge.emit_event("error", message="长" * 30_000)
    with pytest.raises(ValueError, match="envelope"):
        bridge.emit_event("error", protocol=3)
    with pytest.raises(ValueError, match="Unknown event"):
        bridge.emit_event([])


@pytest.fixture
def word_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sdk: Any) -> Any:
    monkeypatch.syspath_prepend(str(WORD))
    monkeypatch.setitem(sys.modules, "topos_app_sdk", sdk)
    word = load_module(WORD / "topos_word.py", "topos_word")
    monkeypatch.setitem(sys.modules, "topos_word", word)
    monkeypatch.setattr(word, "QSettings", lambda *_: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    module = load_module(WORD / "main.py", "independent_word_adapter")
    monkeypatch.setattr(module.QStandardPaths, "writableLocation", lambda *_: str(tmp_path))
    return module


def test_word_open_save_cancel_and_close(app: QApplication, sdk: Any, word_module: Any,
                                       tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()
    bridge = sdk.AppBridge("session", app, input_stream=io.BytesIO(), output_stream=output)
    window = word_module.HostedWordWindow(bridge)
    original = tmp_path / "中文 文件.txt"
    original.write_text("hello 中文", encoding="utf-8")
    window.handle_command({"command": "open", "request_id": "open-one", "path": str(original)})
    assert window.current_file == str(original)
    window.editor.insertPlainText("edit")
    window.editor.document().setModified(True)
    monkeypatch.setattr(window, "_maybe_save_changes", lambda: False)
    window.handle_command({"command": "open", "request_id": "cancel-open", "path": str(original)})
    assert window.current_file == str(original)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert not any(json.loads(line)["event"] == "closed" for line in output.getvalue().splitlines())
    destination = tmp_path / "保存.txt"
    assert window._write_to_path(str(destination))
    monkeypatch.setattr(window, "_maybe_save_changes", lambda: True)
    window.closeEvent(QCloseEvent())
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert any(message["event"] == "opened" and message["request_id"] == "open-one" for message in messages)
    assert any(message["event"] == "saved" and message["path"] == str(destination) for message in messages)
    assert any(message["event"] == "error" and message["code"] == "OPEN_CANCELLED" for message in messages)
    assert messages[-1]["event"] == "closed"
    window.deleteLater()
    app.processEvents()


def test_word_autonomous_open_does_not_reuse_previous_request(app: QApplication, sdk: Any,
        word_module: Any, tmp_path: Path) -> None:
    output = io.StringIO()
    bridge = sdk.AppBridge("session", app, input_stream=io.BytesIO(), output_stream=output)
    window = word_module.HostedWordWindow(bridge)
    original = tmp_path / "first.txt"
    later = tmp_path / "later.txt"
    original.write_text("first")
    later.write_text("later")
    window.handle_command({"command": "open", "request_id": "host-open", "path": str(original)})
    assert window._load_file_from_path(str(later))
    assert window._write_to_path(str(later))
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    opened = [item for item in messages if item["event"] == "opened"]
    assert opened[0]["request_id"] == "host-open"
    assert opened[1]["request_id"] == ""
    assert [item for item in messages if item["event"] == "saved"][-1]["request_id"] == ""
    window.closeEvent(QCloseEvent())
    window.deleteLater()
    app.processEvents()


def test_word_docx_edit_save_roundtrip(app: QApplication, sdk: Any,
                                     word_module: Any, tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    source = tmp_path / "文档 roundtrip.docx"
    document = docx.Document()
    document.add_paragraph("Original document")
    document.save(source)
    output = io.StringIO()
    bridge = sdk.AppBridge("session", app, input_stream=io.BytesIO(), output_stream=output)
    window = word_module.HostedWordWindow(bridge)
    window.handle_command({"command": "open", "request_id": "docx-open", "path": str(source)})
    window.editor.append("Edited in independent application")
    assert window._write_to_path(str(source))
    saved = docx.Document(source)
    assert "Original document" in "\n".join(p.text for p in saved.paragraphs)
    assert "Edited in independent application" in "\n".join(p.text for p in saved.paragraphs)
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert any(message["event"] == "opened" and message["request_id"] == "docx-open" for message in messages)
    assert any(message["event"] == "saved" and message["path"] == str(source) for message in messages)
    window.closeEvent(QCloseEvent())
    window.deleteLater()
    app.processEvents()


def test_real_stdio_detach_keeps_logging_usable_after_host_pipes_close(tmp_path: Path) -> None:
    marker = tmp_path / "survived.txt"
    script = """
import os, sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication
sys.path.insert(0, sys.argv[1])
from topos_app_sdk import AppBridge
app = QApplication([])
bridge = AppBridge('session', app)
bridge.emit_event('ready')
bridge.detach()
print('ordinary diagnostic after detach', flush=True)
print('stderr after detach', file=sys.stderr, flush=True)
os.write(1, b'native stdout after detach\\n')
os.write(2, b'native stderr after detach\\n')
Path(sys.argv[2]).write_text('alive')
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(WORD), str(marker)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert child.stdout is not None and child.stderr is not None and child.stdin is not None
    ready = json.loads(child.stdout.readline())
    assert ready["event"] == "ready"
    child.stdout.close()
    child.stderr.close()
    child.stdin.close()
    assert child.wait(timeout=20) == 0
    assert marker.read_text() == "alive"


def test_real_word_exits_cleanly_while_host_keeps_stdin_open(tmp_path: Path) -> None:
    """Regression: blocked sys.stdin.buffer daemon readers abort finalization."""
    source = tmp_path / "关闭退出.txt"
    source.write_text("No unsaved edits", encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    child = subprocess.Popen([sys.executable, str(WORD / "main.py"), "--topos-session", "session"],
                             cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert child.stdin is not None and child.stdout is not None

    def receive(event: str) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ready, _, _ = select.select([child.stdout], [], [], 1)
            if not ready:
                continue
            raw = child.stdout.readline()
            assert raw, f"Application exited before {event}: {child.poll()}"
            message = json.loads(raw)
            if message["event"] == event:
                return message
        pytest.fail(f"Application did not report {event}")

    try:
        receive("ready")
        child.stdin.write(command("open", path=str(source)))
        child.stdin.flush()
        assert receive("opened")["path"] == str(source)
        child.stdin.write(command("close", "close-request"))
        child.stdin.flush()
        receive("closed")
        # Intentionally keep stdin open during wait; communicate() would close
        # it and conceal the production teardown bug.
        assert child.wait(timeout=20) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


def test_word_recovery_is_separate_for_simultaneous_windows(app: QApplication, sdk: Any,
                                                           word_module: Any, tmp_path: Path) -> None:
    windows = [word_module.HostedWordWindow(sdk.AppBridge(None, app)) for _ in range(2)]
    for window in windows:
        window._set_recovery_path(str(tmp_path / "same.docx"))
    assert windows[0]._autosave_path != windows[1]._autosave_path
    Path(windows[1]._autosave_path).write_bytes(b"other window")
    windows[0]._clear_autosave()
    assert Path(windows[1]._autosave_path).read_bytes() == b"other window"
    for window in windows:
        window.editor.document().setModified(False)
        window.closeEvent(QCloseEvent())
        window.deleteLater()
    app.processEvents()


def test_model_worker_parses_without_a_gui_or_topos(tmp_path: Path) -> None:
    source = tmp_path / "triangle.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    output = tmp_path / "result.pkl"
    completed = subprocess.run([sys.executable, str(VIEWER / "quick_3d_parse_worker.py"),
                                str(source), str(output)], cwd=tmp_path, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr.decode()
    sys.path.insert(0, str(VIEWER))
    try:
        with output.open("rb") as stream:
            status, value = pickle.load(stream)
    finally:
        sys.path.remove(str(VIEWER))
    assert status == "ok", value
    assert value["total_faces"] == 1 and value["total_verts"] == 3


def test_fbx_worker_and_controller_cross_process_roundtrip(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("assimp_py")
    source = tmp_path / "triangle.fbx"
    source.write_text('''; FBX 7.4.0 project file
FBXHeaderExtension: {
 FBXHeaderVersion: 1003
 FBXVersion: 7400
}
Objects: {
 Geometry: 1, "Geometry::Triangle", "Mesh" {
  Vertices: *9 { a: 0,0,0,1,0,0,0,1,0 }
  PolygonVertexIndex: *3 { a: 0,1,-3 }
 }
 Model: 2, "Model::Triangle", "Mesh" {
  Version: 232
  Properties70: {
   P: "Lcl Translation", "Lcl Translation", "", "A",0,0,0
   P: "Lcl Rotation", "Lcl Rotation", "", "A",0,0,0
   P: "Lcl Scaling", "Lcl Scaling", "", "A",1,1,1
  }
 }
}
Connections: {
 C: "OO",1,2
 C: "OO",2,0
}
''')
    output = tmp_path / "fbx.pkl"
    completed = subprocess.run([sys.executable, str(VIEWER / "quick_3d_parse_worker.py"),
                                str(source), str(output)], cwd=tmp_path, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr.decode()
    status, value = pickle.loads(output.read_bytes())
    assert status == "ok", value
    assert value["total_faces"] == 1 and value["total_verts"] == 3
    # Animated FBX results additionally contain an application class. Verify
    # its module identity/state can cross the same independent process boundary.
    controller_pickle = tmp_path / "controller.pkl"
    script = """
import pickle,sys
from pathlib import Path
from types import SimpleNamespace
import numpy as np
sys.path.insert(0, sys.argv[1])
from quick_3d_viewer import FBXSceneController
root = SimpleNamespace(name='Root', transformation=np.eye(4), mesh_indices=(0,), children=[])
controller = FBXSceneController(SimpleNamespace(root_node=root, animations=[]))
Path(sys.argv[2]).write_bytes(pickle.dumps(controller))
"""
    completed = subprocess.run([sys.executable, "-c", script, str(VIEWER), str(controller_pickle)],
                               cwd=tmp_path, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr.decode()
    monkeypatch.syspath_prepend(str(VIEWER))
    controller = pickle.loads(controller_pickle.read_bytes())
    assert controller.root_node.name == "Root" and controller.mesh_node_names == {0: "Root"}


def test_model_adapter_waits_for_result_and_rejects_stale_success(app: QApplication, sdk: Any,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the adapter with an asynchronous renderer boundary, without GPU."""
    fake = ModuleType("quick_3d_viewer")

    class Viewer:
        def __init__(self) -> None:
            self._load_req_id = 0
            self.current_file_path = ""

        def load_model(self, path: str, preserve_camera: bool = False) -> None:
            self._load_req_id += 1
            self.current_file_path = path

        def _on_model_loaded(self, *_: Any) -> None:
            pass

        def showNormal(self) -> None:
            pass

        def raise_(self) -> None:
            pass

        def activateWindow(self) -> None:
            pass

    fake.QuickModelViewer = Viewer
    fake.SUPPORTED_EXTS = {".obj", ".fbx"}
    monkeypatch.setitem(sys.modules, "quick_3d_viewer", fake)
    monkeypatch.setitem(sys.modules, "topos_app_sdk", sdk)
    module = load_module(VIEWER / "main.py", "independent_model_adapter")
    output = io.StringIO()
    bridge = sdk.AppBridge("session", app, input_stream=io.BytesIO(), output_stream=output)
    viewer = module.HostedModelViewer(bridge)
    paths = [tmp_path / "first.obj", tmp_path / "second.obj"]
    for index, path in enumerate(paths):
        path.write_text("model fixture")
        viewer.handle_command({"command": "open", "request_id": str(index), "path": str(path)})
    assert not any(json.loads(line)["event"] == "opened" for line in output.getvalue().splitlines())
    viewer._on_model_loaded(1, str(paths[0]), {"parsed": True}, "")
    viewer._on_model_loaded(2, str(paths[1]), {"parsed": True}, "")
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert any(item["event"] == "error" and item["code"] == "OPEN_SUPERSEDED" for item in messages)
    opened = [item for item in messages if item["event"] == "opened"]
    assert len(opened) == 1 and opened[0]["request_id"] == "1" and opened[0]["path"] == str(paths[1])
    viewer.handle_command({"command": "open", "request_id": "missing", "path": str(tmp_path / "absent.obj")})
    assert json.loads(output.getvalue().splitlines()[-1])["code"] == "FILE_NOT_FOUND"
