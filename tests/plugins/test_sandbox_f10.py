"""F10：受限进程与有界 RPC；平台隔离如实报告；未保护第三方入口禁用。"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

from src.shared.plugins.sandbox import (
    BoundedRPC,
    ExecutionGate,
    RestrictedProcessRunner,
    SandboxCapabilities,
    is_trusted_builtin,
)

PYTHON = sys.executable


# ---------------------------------------------------------------------------
# 隔离能力：如实报告（不冒充）
# ---------------------------------------------------------------------------

def test_capabilities_probe_reports_structure_and_level():
    caps = SandboxCapabilities.probe()
    assert caps.level in ("none", "restricted_env", "seatbelt")
    assert caps.details  # 细节非空：怎么达到/为何降级都说清


def test_capabilities_honest_when_seatbelt_missing(monkeypatch):
    import src.shared.plugins.sandbox as sandbox_module
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: None)
    caps = SandboxCapabilities.probe()
    assert caps.level == "restricted_env"       # POSIX rlimits 仍在
    assert "sandbox-exec 不存在" in caps.details  # 降级原因可见


# ---------------------------------------------------------------------------
# 受限进程：最小环境 / deadline / 有界输出 / seatbelt 拒写
# ---------------------------------------------------------------------------

def _run(code: str, **kwargs):
    runner = RestrictedProcessRunner(
        kwargs.pop("capabilities", None),
        seatbelt_root=kwargs.pop("seatbelt_root", ""))
    return runner.run([PYTHON, "-u", "-c", code], **kwargs)


def test_minimal_environment_allowlist():
    result = _run("import os, json; print(json.dumps(sorted(os.environ)))",
                  deadline_sec=15)
    assert result.ok, result.output_tail
    keys = set(json.loads(result.output_tail.strip().splitlines()[-1]))
    allowed = {"PATH", "LANG", "LC_CTYPE", "TMPDIR", "HOME"}
    injected = {key for key in keys if key.startswith("__CF")}  # macOS 平台注入
    assert keys - injected <= allowed         # 白名单之外的环境不进子进程


def test_deadline_kills_long_running_process():
    result = _run("import time; time.sleep(30)", deadline_sec=1.0)
    assert not result.ok and "预算" in result.error


def test_output_is_bounded():
    result = _run("print('x' * 1_000_000)", deadline_sec=15, max_output=4096)
    assert len(result.output_tail) <= 4096


def test_seatbelt_denies_writes_under_protected_root(tmp_path):
    target = tmp_path / "protected" / "secret.txt"
    target.parent.mkdir(parents=True)
    result = _run(
        f"open({str(target)!r}, 'w').write('pwned')",
        deadline_sec=15, require_seatbelt=True, seatbelt_root=str(tmp_path))
    assert result.isolation_level == "seatbelt"
    assert not target.exists()                # 拒写生效（exit != 0）
    assert not result.ok


def test_require_seatbelt_refuses_when_unavailable():
    weak = SandboxCapabilities(level="restricted_env", details="探测注入")
    runner = RestrictedProcessRunner(weak)
    result = runner.run([PYTHON, "-c", "print(1)"],
                        deadline_sec=5, require_seatbelt=True)
    assert not result.ok and "拒绝执行" in result.error
    assert "不冒充" in result.error


# ---------------------------------------------------------------------------
# 有界 RPC：往返 / 越界 / 超时
# ---------------------------------------------------------------------------

_ECHO_RPC = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        req = json.loads(line)
        print(json.dumps({"echo": req}), flush=True)
""")


def test_bounded_rpc_roundtrip():
    rpc = BoundedRPC([PYTHON, "-u", "-c", _ECHO_RPC], deadline_sec=15)
    try:
        response = rpc.call({"op": "ping", "n": 7})
        assert response == {"echo": {"op": "ping", "n": 7}}
    finally:
        rpc.close()


def test_bounded_rpc_oversized_response_kills_process():
    huge = ("import json, sys\n"
            "line = sys.stdin.readline()\n"
            f"print(json.dumps({{'blob': 'y' * 512}}), flush=True)\n")
    rpc = BoundedRPC([PYTHON, "-u", "-c", huge],
                     deadline_sec=15, max_message_bytes=256)
    with pytest.raises(RuntimeError, match="响应超界"):
        rpc.call({"op": "boom"})
    assert rpc._proc.poll() is not None       # 进程被终止，不留挂死通道


def test_bounded_rpc_deadline_terminates_hung_process():
    rpc = BoundedRPC([PYTHON, "-u", "-c", "import time; time.sleep(60)"],
                     deadline_sec=1.0)
    with pytest.raises(RuntimeError, match="预算"):
        rpc.call({"op": "hang"})
    assert rpc._proc.poll() is not None


def test_bounded_rpc_oversized_request_rejected_without_child_work():
    rpc = BoundedRPC([PYTHON, "-u", "-c", _ECHO_RPC], deadline_sec=15)
    with pytest.raises(RuntimeError, match="请求超界"):
        rpc.call({"blob": "x" * (4 * 1024 * 1024 + 64)})
    rpc.close()


# ---------------------------------------------------------------------------
# 执行门禁：未保护的第三方执行入口禁用
# ---------------------------------------------------------------------------

def test_gate_allows_trusted_builtin_at_any_isolation():
    gate = ExecutionGate(SandboxCapabilities(level="none", details="弱机"))
    assert is_trusted_builtin("files.format_factory")
    ok, reason = gate.can_execute(trusted=True, isolated_level="none")
    assert ok and reason == ""


def test_gate_refuses_third_party_below_seatbelt():
    gate = ExecutionGate(SandboxCapabilities(level="restricted_env",
                                             details="探测注入"))
    ok, reason = gate.can_execute(trusted=False,
                                  isolated_level="restricted_env")
    assert not ok and "未保护的第三方执行入口已禁用" in reason
    ok2, _ = gate.can_execute(trusted=False, isolated_level="none")
    assert not ok2


def test_gate_allows_third_party_only_under_seatbelt():
    gate = ExecutionGate(SandboxCapabilities(level="seatbelt", details="本机"))
    ok, _ = gate.can_execute(trusted=False, isolated_level="seatbelt")
    assert ok


def test_gate_plan_reports_honest_shortfall():
    weak = ExecutionGate(SandboxCapabilities(level="restricted_env",
                                             details="探测注入"))
    level, note = weak.plan_isolation(trusted=False)
    assert level == "restricted_env"
    assert "第三方执行被禁用" in note and "如实报告" in note
    strong = ExecutionGate(SandboxCapabilities(level="seatbelt", details="本机"))
    level2, note2 = strong.plan_isolation(trusted=False)
    assert level2 == "seatbelt" and "seatbelt 隔离" in note2


def test_production_launch_gate_blocks_third_party(qtbot, tmp_path):
    """控制器接线：非 builtin 源的启动在无隔离执行路径时被门禁拒绝。"""
    pytest.importorskip("PyQt6.QtWidgets")
    from src.app.ui.files_plugins_controller import FilesPluginsController
    from src.features.files.plugin_contracts import FilesPluginInfo
    from pathlib import Path

    class _Catalog:
        def snapshot(self):
            return ()

    class _Transport:
        plugin_launch_finished = None
        def launch_plugin(self, *args):
            raise AssertionError("第三方执行不得到达 transport")

    from PyQt6.QtCore import QObject, pyqtSignal
    _Transport.plugin_launch_finished = pyqtSignal(str, bool, str)

    controller = FilesPluginsController.__new__(FilesPluginsController)
    QObject.__init__(controller)
    controller.catalog = _Catalog()
    controller.transport = _Transport()
    controller.preferences = None
    controller.panel = None
    controller.selected_paths = lambda: []
    controller.legacy = lambda: None
    controller._sources_factory = None
    controller._desired_store = None
    controller._inventory = None
    controller._commands = None
    controller._closed = False
    controller._writing = False
    controller._busy = set()
    controller._launch_jobs = {}
    controller._retire_polls = {}
    controller._shortcut_plugins = set()
    from src.shared.plugins.host_session import PluginHostSession
    controller.host_session = PluginHostSession()
    controller._infos = (FilesPluginInfo(
        plugin_id="files.installed_tool", name="安装工具", version="1.0",
        author="", description="", api_version=1, category="扩展",
        capabilities=(), runtime_loaded=False, desired_enabled=True,
        self_disabled=False, needs_restart=False, error="",
        launch_path="/api/plugin/installed_tool/launch",
        launch_payload_kind="none", launch_unavailable_reason="",
        path=Path("."), source="installed"),)
    messages = []

    class _Sink:
        class _Notice:
            @staticmethod
            def setText(text):
                messages.append(text)

        notice = _Notice()
        context = _Notice()

        def setText(self, text):
            messages.append(text)

        def set_snapshot(self, *args, **kwargs):
            pass

        def set_context(self, *args, **kwargs):
            pass

    controller.center = _Sink()
    controller.launcher = _Sink()
    controller.panel = _Sink()
    controller.launch("files.installed_tool")
    assert any("未保护的第三方执行入口已禁用" in message for message in messages)
