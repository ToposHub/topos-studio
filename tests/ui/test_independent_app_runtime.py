"""Real pipe/process tests for protocol ownership and nonblocking GUI teardown."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from src.features.files.independent_app_runtime import (
    IndependentAppRuntime, check_dependencies, decode_event, validate_input,
)
from src.shared.plugins.independent_apps import load_independent_app


ECHO_APP = '''import json,sys,time
sid=sys.argv[-1]
def emit(event,request_id="",**payload):
    print(json.dumps(dict(protocol=1,session_id=sid,event=event,request_id=request_id,**payload)),flush=True)
emit("ready")
for line in sys.stdin:
    command=json.loads(line)
    if command["command"]=="open":
        emit("opened",command["request_id"],path=command["path"])
    elif command["command"]=="close":
        emit("closed",command["request_id"])
        break
    elif command["command"]=="detach":
        time.sleep(0.25)
        break
'''


def make_app(tmp_path: Path, code: str = ECHO_APP):
    root = tmp_path / "apps" / "test"
    root.mkdir(parents=True)
    (root / "main.py").write_text(code, encoding="utf-8")
    (root / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "apps.test", "display_name": "Test application", "version": "1.0.0",
        "schema_version": "1.0", "host_api": [1, 1],
        "capabilities": [{"capability_id": "open", "kind": "command", "run_location": "process"}],
        "metadata": {"independent_app": {"protocol": 1, "entrypoint": "main.py",
                     "extensions": [".txt"], "required_modules": {}, "format_modules": {}}},
    }), encoding="utf-8")
    return load_independent_app(root)


@pytest.fixture
def runtime(qtbot):
    runner = IndependentAppRuntime(ready_timeout=2, open_timeout=2)
    processes = []
    original_launch = runner.launch

    def launch(*args):
        original_launch(*args)
        processes.extend(s.process for s in runner._sessions.values() if s.process not in processes)

    runner.launch = launch
    yield runner
    runner.shutdown()
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def test_open_reuse_and_close_actual_process(runtime, qtbot, tmp_path):
    spec = make_app(tmp_path)
    path = tmp_path / "中文 file.txt"
    path.write_text("first", encoding="utf-8")
    opened = []
    runtime.opened.connect(lambda *args: opened.append(args))
    runtime.launch(spec, sys.executable, str(path))
    qtbot.waitUntil(lambda: opened == [(spec.plugin_id, str(path))])
    session = runtime._sessions[spec.plugin_id]
    pid = session.process.pid
    runtime.launch(spec, sys.executable, str(path))
    qtbot.waitUntil(lambda: len(opened) == 2)
    assert runtime._sessions[spec.plugin_id].process.pid == pid
    runtime._send(session, "close")
    qtbot.waitUntil(lambda: not runtime._sessions)


def test_shutdown_detaches_and_keeps_child_alive(runtime, qtbot, tmp_path):
    spec = make_app(tmp_path)
    runtime.launch(spec, sys.executable, None)
    qtbot.waitUntil(lambda: runtime._sessions[spec.plugin_id].ready)
    process = runtime._sessions[spec.plugin_id].process
    started = time.monotonic()
    runtime.shutdown()
    assert time.monotonic() - started < 0.1
    assert process.poll() is None
    qtbot.waitUntil(lambda: process.poll() is not None)


@pytest.mark.parametrize("change", [
    {"event": []}, {"event": {}}, {"protocol": True}, {"session_id": "other"},
    {"request_id": []}, {"event": "unknown"},
])
def test_bad_envelopes_fail_as_value_error(change):
    event = {"protocol": 1, "session_id": "session", "event": "ready", "request_id": ""}
    event.update(change)
    with pytest.raises(ValueError):
        decode_event(json.dumps(event).encode(), "session")


def test_deep_and_oversized_json_are_bounded():
    for line in (b"[" * 20000 + b"0" + b"]" * 20000, b"x" * 65537):
        with pytest.raises(ValueError):
            decode_event(line, "session")


def test_invalid_child_event_does_not_escape_qt_timer(runtime, qtbot, tmp_path):
    spec = make_app(tmp_path, 'import sys,time,json\nprint(json.dumps({"protocol":1,"session_id":sys.argv[-1],"event":[]}),flush=True)\ntime.sleep(5)\n')
    failures = []
    states = []
    runtime.failed.connect(lambda *args: failures.append(args))
    runtime.state_changed.connect(lambda *args: states.append(args))
    runtime.launch(spec, sys.executable, None)
    qtbot.waitUntil(lambda: bool(failures))
    assert "类型" in failures[0][1]
    assert not runtime._sessions
    assert states[-1] == (spec.plugin_id, "stopped")


def test_ready_timeout_terminates_only_startup(runtime, qtbot, tmp_path):
    spec = make_app(tmp_path, "import time\ntime.sleep(5)\n")
    runtime._ready_timeout = 0.05
    failures = []
    runtime.failed.connect(lambda *args: failures.append(args))
    runtime.launch(spec, sys.executable, None)
    process = runtime._sessions[spec.plugin_id].process
    qtbot.waitUntil(lambda: bool(failures))
    qtbot.waitUntil(lambda: process.poll() is not None)
    assert "ready" in failures[0][1]


def test_pipe_backpressure_never_blocks_gui(runtime, qtbot, tmp_path):
    spec = make_app(tmp_path, ECHO_APP.split("for line in sys.stdin:")[0] + "time.sleep(5)\n")
    runtime.launch(spec, sys.executable, None)
    qtbot.waitUntil(lambda: runtime._sessions[spec.plugin_id].ready)
    session = runtime._sessions[spec.plugin_id]
    started = time.monotonic()
    with pytest.raises(ValueError, match="队列"):
        for _ in range(100):
            runtime._send(session, "activate", padding="x" * 60000)
    runtime.shutdown()
    assert time.monotonic() - started < 0.5


def test_final_saved_event_survives_exit_before_reader_drain(runtime, qtbot, tmp_path, monkeypatch):
    from src.features.files import independent_app_runtime as module
    path = tmp_path / "saved.txt"
    path.write_text("saved", encoding="utf-8")
    code = ECHO_APP.split("for line in sys.stdin:")[0] + f'emit("saved",path={str(path)!r})\n'
    spec = make_app(tmp_path, code)
    reader = module._read_stdout

    def delayed(session):
        time.sleep(0.15)
        reader(session)

    monkeypatch.setattr(module, "_read_stdout", delayed)
    changed = []
    runtime.files_changed.connect(changed.append)
    runtime.launch(spec, sys.executable, None)
    qtbot.waitUntil(lambda: not runtime._sessions)
    assert changed == [[str(path)]]


def test_late_open_after_timeout_keeps_app_connected(runtime, qtbot, tmp_path):
    spec = make_app(tmp_path, ECHO_APP.replace('emit("opened",', 'time.sleep(0.2)\n        emit("opened",'))
    path = tmp_path / "slow.txt"
    path.touch()
    failures = []
    runtime.failed.connect(lambda *args: failures.append(args))
    runtime._open_timeout = 0.05
    runtime.launch(spec, sys.executable, str(path))
    qtbot.waitUntil(lambda: bool(failures))
    qtbot.wait(300)
    assert len(failures) == 1
    assert spec.plugin_id in runtime._sessions


def test_unrelated_error_does_not_finish_open_request(runtime, qtbot, tmp_path):
    code = ECHO_APP.replace('emit("opened",', 'emit("error",message="background notice")\n        time.sleep(0.1)\n        emit("opened",')
    spec = make_app(tmp_path, code)
    path = tmp_path / "notice.txt"
    path.touch()
    notices, opened, failures = [], [], []
    runtime.notice.connect(lambda *args: notices.append(args))
    runtime.opened.connect(lambda *args: opened.append(args))
    runtime.failed.connect(lambda *args: failures.append(args))
    runtime.launch(spec, sys.executable, str(path))
    qtbot.waitUntil(lambda: bool(notices))
    assert runtime._sessions[spec.plugin_id].pending_id
    qtbot.waitUntil(lambda: bool(opened))
    assert not failures


def test_upgrade_does_not_rebind_running_process(runtime, qtbot, tmp_path):
    spec = make_app(tmp_path)
    runtime.launch(spec, sys.executable, None)
    qtbot.waitUntil(lambda: runtime._sessions[spec.plugin_id].ready)
    (spec.root / "main.py").write_text(ECHO_APP + "# upgrade\n")
    upgraded = load_independent_app(spec.root)
    with pytest.raises(ValueError, match="旧版本"):
        runtime.launch(upgraded, sys.executable, None)


def test_dependency_diagnosis_and_selection_validation(tmp_path):
    from dataclasses import replace
    spec = make_app(tmp_path)
    missing = replace(spec, required_modules={"topos_missing_dependency_123": "missing-package"})
    with pytest.raises(ValueError, match="missing-package"):
        check_dependencies(missing, sys.executable, None)
    with pytest.raises(ValueError, match="单选"):
        validate_input(spec, ("a", "b"))
    path = tmp_path / "unsupported.obj"
    path.touch()
    with pytest.raises(ValueError, match="不支持"):
        validate_input(spec, (str(path),))


def test_package_bytecode_cannot_bypass_source_fingerprint(runtime, qtbot, tmp_path):
    import importlib.util
    import marshal
    import py_compile
    path = tmp_path / "source.txt"
    path.touch()
    spec = make_app(tmp_path, 'from helper import path\n' + ECHO_APP.split("for line in sys.stdin:")[0] +
                    'emit("saved",path=path)\n')
    helper = spec.root / "helper.py"
    helper.write_text(f"path={str(path)!r}\n")
    compiled = Path(py_compile.compile(str(helper), doraise=True))
    spec = load_independent_app(spec.root)
    # Preserve valid pyc header but replace its executable payload.
    compiled.write_bytes(compiled.read_bytes()[:16] + marshal.dumps(compile("path='/missing/forged.txt'", str(helper), "exec")))
    assert load_independent_app(spec.root).fingerprint == spec.fingerprint
    changed = []
    runtime.files_changed.connect(changed.append)
    runtime.launch(spec, sys.executable, None)
    qtbot.waitUntil(lambda: not runtime._sessions)
    assert changed == [[str(path)]]


@pytest.fixture
def app_host(qtbot, tmp_path):
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QLabel, QWidget
    from src.app.ui.files_plugins_controller import FilesPluginsController
    from src.app.ui.native_files_prefs import NativeFilesPreferences
    from src.app.ui.native_files_transport import NativeFilesTransport
    from src.features.files.builtin_plugins import NativePluginCatalog
    from src.shared.plugins.discovery import DesiredStateStore, PluginSourceConfig

    spec = make_app(tmp_path)
    panel = QWidget()
    panel.meta_status = QLabel(panel)
    qtbot.addWidget(panel)
    preferences = NativeFilesPreferences(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    desired = DesiredStateStore(tmp_path / "desired.json")
    catalog = NativePluginCatalog(desired_store=desired)
    sources = lambda: PluginSourceConfig.for_host(
        builtin_ids=tuple(row.plugin_id for row in catalog.snapshot()),
        install_root=tmp_path / "installed", app_roots=(tmp_path / "apps",))
    transport = NativeFilesTransport(lambda: "", None, panel)
    host = FilesPluginsController(catalog, transport, preferences, panel,
                                  lambda: [], lambda: None, panel,
                                  sources_factory=sources, desired_store=desired)
    qtbot.waitUntil(lambda: any(info.plugin_id == spec.plugin_id for info in host._infos))
    yield host, spec, preferences
    processes = [session.process for session in host._app_runtime._sessions.values()] if host._app_runtime else []
    host.shutdown()
    host._pool.waitForDone(5000)
    transport.shutdown()
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def test_discovered_local_app_opens_via_shared_center_and_file_resolver(app_host, qtbot, tmp_path, monkeypatch):
    host, spec, preferences = app_host
    monkeypatch.setattr(host, "_confirm_app_trust", lambda current: True)
    path = tmp_path / "打开 file.txt"
    path.touch()
    assert [info.plugin_id for info in host.app_candidates(str(path))] == [spec.plugin_id]
    assert host.open_associated_file(str(path))
    qtbot.waitUntil(lambda: spec.plugin_id in preferences.plugin_recent, timeout=5000)
    assert spec.plugin_id not in host._busy
    assert "已打开" in host.panel.meta_status.text()
    host._set_enabled(spec.plugin_id, False)
    qtbot.waitUntil(lambda: not host.app_candidates(str(path)))
    assert not host.open_associated_file(str(path))


def test_user_default_handler_and_open_with_menu(app_host, tmp_path):
    from PyQt6.QtWidgets import QMenu
    host, spec, preferences = app_host
    path = str(tmp_path / "test.txt")
    menu = QMenu()
    host.populate_open_with_menu(menu, [path])
    submenu = menu.actions()[0].menu()
    assert submenu.actions()[0].text() == "Test application"
    host._set_app_default(".txt", "__native__")
    assert not host.open_associated_file(path)
    assert preferences.app_default_handlers == {".txt": "__native__"}


def test_trust_binds_contents_and_does_not_trust_on_discovery(app_host, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    host, spec, preferences = app_host
    assert preferences.app_trust == {}
    answers = []
    def confirm(*args):
        answers.append(args)
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", confirm)
    assert host._confirm_app_trust(spec)
    assert host._confirm_app_trust(spec)
    assert len(answers) == 1
    (spec.root / "main.py").write_text(ECHO_APP + "# changed\n")
    assert host._confirm_app_trust(load_independent_app(spec.root))
    assert len(answers) == 2


def test_disabled_during_prepare_cannot_launch(app_host, qtbot, tmp_path, monkeypatch):
    import threading
    from src.features.files import independent_app_runtime as module
    host, spec, _preferences = app_host
    entered = threading.Event()
    released = threading.Event()
    def check(*args):
        entered.set()
        released.wait(2)
    monkeypatch.setattr(module, "check_dependencies", check)
    monkeypatch.setattr(host, "_confirm_app_trust", lambda current: True)
    path = tmp_path / "cancel.txt"
    path.touch()
    host.launch(spec.plugin_id, paths=(str(path),))
    qtbot.waitUntil(entered.is_set)
    host._set_enabled(spec.plugin_id, False)
    released.set()
    qtbot.waitUntil(lambda: spec.plugin_id not in host._busy)
    assert host._app_runtime is None


def test_stale_prepare_cannot_cancel_newer_launch(app_host, monkeypatch):
    host, spec, _preferences = app_host
    queued = []
    monkeypatch.setattr(host._pool, "start", queued.append)
    host.launch(spec.plugin_id, paths=())
    older = queued.pop()
    host._app_launch_tokens[spec.plugin_id] = "newer-launch"
    host._busy.add(spec.plugin_id)
    older.signals.finished.emit(0, "app-prepare", None, "older failure")
    assert host._app_launch_tokens[spec.plugin_id] == "newer-launch"
    assert spec.plugin_id in host._busy


def test_invalid_app_manifest_remains_visible(app_host, qtbot):
    host, spec, _preferences = app_host
    (spec.root / "topos.plugin.json").write_text("{broken")
    host.refresh()
    qtbot.waitUntil(lambda: any(info.source == "local_app" and bool(info.error) for info in host._infos))
    invalid = next(info for info in host._infos if info.source == "local_app")
    assert not invalid.can_launch
    assert invalid.launch_unavailable_reason


def test_uppercase_extension_declaration_matches_file_handler(app_host, qtbot, tmp_path):
    host, spec, _preferences = app_host
    path = spec.root / "topos.plugin.json"
    manifest = json.loads(path.read_text())
    manifest["metadata"]["independent_app"]["extensions"] = [".TXT"]
    path.write_text(json.dumps(manifest))
    host.refresh()
    qtbot.waitUntil(lambda: host._inventory.snapshot().record(spec.plugin_id).manifest.metadata[
        "independent_app"]["extensions"] == (".TXT",))
    assert host.app_candidates(str(tmp_path / "file.txt"))
