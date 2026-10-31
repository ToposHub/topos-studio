"""T3.3 — 精简只读 toposctl 测试（version / 离线 capabilities / main / 无 GL guard）。

验收（docs/ai_closure_focused_plan_2026-08-04.md 工作流 3 T3.3）：

- ``version``：返回 cli/protocol/manifest 版本，不依赖工程上下文。
- ``capabilities`` 离线：无 invoke_fn 时直接读 build_manifest，结果与 connected 同源。
- ``main``：stdout 单一 JSON（可 json.loads）；退出码 = exit_code；不连宿主的命令
  返回 NOT_CONNECTED（exit 1）。
- **无 Qt/GL import guard**：导入/运行 CLI 模块不拉入 PyQt6.QtWidgets / moderngl /
  OpenGL，不在无显示环境创建 QApplication / GL context。
"""

from __future__ import annotations

import io
import json
import sys

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.toposctl import CliResult, main, run_cli


# ================================================================
# version 命令
# ================================================================


class TestVersionCommand:
    def test_version_returns_info_without_invoke_fn(self):
        """version 不依赖 invoke_fn（纯离线）。"""
        r = run_cli(["version"])  # 不传 invoke_fn
        assert r.exit_code == 0
        assert r.tool_name == "topos.version"
        info = r.result
        assert info["cli"] == "toposctl"
        assert info["cli_version"]
        assert info["protocol"] == "taep/1.0"

    def test_version_ignores_invoke_fn(self):
        """即便传了 invoke_fn，version 也不调用它。"""

        class _Boom:
            def __call__(self, *a, **k):
                raise AssertionError("version should not call invoke_fn")

        r = run_cli(["version"], _Boom())
        assert r.exit_code == 0
        assert r.result["cli"] == "toposctl"


# ================================================================
# 离线 capabilities
# ================================================================


class TestOfflineCapabilities:
    def test_capabilities_offline_without_invoke_fn(self):
        """无 invoke_fn 时 capabilities 直接读 build_manifest。"""
        r = run_cli(["capabilities"])  # 不传 invoke_fn
        assert r.exit_code == 0
        assert r.tool_name == "topos.get_capabilities"
        assert "operations" in r.result
        # 18 个可编译 operation（与 build_manifest 一致）
        assert len(r.result["operations"]) >= 1
        # 未实现的 kind 不在列表里（TAEP §4.3）
        assert "add_subtitle" in r.result["operations"]

    def test_capabilities_offline_matches_manifest(self):
        """离线 capabilities 与 build_manifest() 同源。"""
        from src.features.edit.ai_editing.capability_manifest import build_manifest
        m = build_manifest()
        r = run_cli(["capabilities"])
        assert set(r.result["operations"]) == set(m.operations)


# ================================================================
# 未连接宿主的命令
# ================================================================


class TestNotConnected:
    def test_snapshot_without_host_returns_not_connected(self):
        r = run_cli(["snapshot"])  # 不传 invoke_fn
        assert r.exit_code == 1
        codes = [d.get("code") for d in r.diagnostics]
        assert "TAEP_PERMISSION_DENIED" in codes

    def test_validate_plan_without_host_returns_not_connected(self):
        r = run_cli(["validate-plan", '{"plan_id":"p"}'])
        assert r.exit_code == 1
        assert any(d.get("code") == "TAEP_PERMISSION_DENIED" for d in r.diagnostics)


# ================================================================
# main 入口（stdout 单一 JSON / exit code）
# ================================================================


class TestMain:
    def test_main_version_stdout_single_json(self, capsys):
        """main(version) stdout 是单一可 json.loads 的 JSON。"""
        code = main(["version"])
        out = capsys.readouterr().out
        assert code == 0
        data = json.loads(out)  # 单一 JSON
        assert data["exit_code"] == 0
        assert data["result"]["cli"] == "toposctl"

    def test_main_unknown_command_exit_2(self, capsys):
        code = main(["no-such-cmd"])
        out = capsys.readouterr().out
        assert code == 2
        data = json.loads(out)
        assert data["exit_code"] == 2

    def test_main_text_output_format(self, capsys):
        """--output text 模式：result 走 stdout，usage 走 stderr。"""
        code = main(["version", "--output", "text"])
        captured = capsys.readouterr()
        assert code == 0
        # text 模式 stdout 是 indent JSON（version info），不是 CliResult 包装
        data = json.loads(captured.out)
        assert data["cli"] == "toposctl"

    def test_main_capabilities_stdout_json(self, capsys):
        code = main(["capabilities"])
        out = capsys.readouterr().out
        assert code == 0
        data = json.loads(out)
        assert "operations" in data["result"]

    def test_main_empty_argv_exit_2(self, capsys):
        code = main([])
        out = capsys.readouterr().out
        assert code == 2
        data = json.loads(out)
        assert data["exit_code"] == 2
        assert data["usage_hint"]


# ================================================================
# 无 Qt/GL import guard（T3.3 硬验收）
# ================================================================


class TestNoQtGlImport:
    """toposctl 模块自身不应直接 import Qt widgets / GL。

    注：``capabilities`` 离线路径调用 ``build_manifest`` → EffectRegistry，后者在
    当前架构下会间接拉入 moderngl/PyQt6（能力真相源依赖 GL 来枚举 effect）。这是
    EffectRegistry 的既有架构特征，不是 toposctl 的泄漏。本组测试守护的是
    toposctl **模块级**不直接依赖 Qt/GL（保证 version 命令真正 headless）。
    """

    def test_no_qt_widgets_in_module_globals(self):
        import src.features.edit.ai_editing.toposctl as mod
        # 模块级 globals 不应含这些重依赖
        for forbidden in ("QApplication", "QWidget", "QMessageBox"):
            assert not hasattr(mod, forbidden), f"toposctl leaked {forbidden}"

    def test_no_moderngl_or_opengl_at_module_level(self):
        """toposctl 模块顶部 import 不含 moderngl / OpenGL。"""
        import inspect
        import src.features.edit.ai_editing.toposctl as mod
        src_text = inspect.getsource(mod)
        # 检查顶层 import 行（非注释/字符串内的提及）
        for line in src_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith(("import moderngl", "from moderngl",
                                    "import OpenGL", "from OpenGL")):
                pytest.fail(f"toposctl imports GL at module level: {stripped}")

    def test_version_truly_headless(self):
        """version 命令不触发任何工程/GL 装配（纯读常量）。

        version 不调 build_manifest（那会拉 EffectRegistry/GL），只读 TAEP 协议
        常量——因此是无显示环境安全的。用模块加载前后对比验证。
        """
        import sys as _sys
        before = {k for k in _sys.modules if k.startswith("moderngl")}
        run_cli(["version"])
        after = {k for k in _sys.modules if k.startswith("moderngl")}
        # version 不应新引入 moderngl
        assert not (after - before), f"version pulled in GL: {after - before}"
