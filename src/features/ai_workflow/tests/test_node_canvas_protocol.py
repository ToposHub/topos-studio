"""P5.6 测试：节点画布 UI 协议（plan §10.7.1）。

验证 :class:`GraphUiProtocol` / :class:`VisualTokens` / :class:`NodeCanvasBase`
的核心契约：

- Protocol 定义画布最小方法集（build_from_graph/clear_all/add_node_visual/...）。
- ``GraphUiSignals`` 列出对外信号契约（Composite 已满足；AI Workflow 须等价）。
- ``VisualTokens`` 提供 node/port/status 视觉令牌（数据，不含 Qt 对象）。
- ``NodeCanvasBase`` 抽象基类不持有 GPU 资源。

红线（plan §10.7.1 / §1.4）：

- 不 import ``composite.core`` / ``moderngl``（本模块纯数据 + Protocol）。
- ``GraphUiProtocol`` 不含 GPU 渲染方法（preview_requested/thumbnail 等）。
"""
from __future__ import annotations

import ast
import pytest


def _protocol_module_imports() -> set[str]:
    """AST 扫描 graph_ui_protocol 源码的全部 import 模块名（AUD-23620）。

    子串检查可被 ``from moderngl import ...``、``import src.features.composite.core``、
    ``import PyQt6.QtCore`` 等形式绕过（docstring 中的模块名提及也会造成误报），
    AST 解析后按模块名精确判定。
    """
    import src.shared.node_canvas.graph_ui_protocol as mod
    source_path = mod.__file__
    assert source_path, "graph_ui_protocol 模块无 __file__，红线扫描无法执行"
    with open(source_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=source_path)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.add(node.module)
    return modules


def _imports_root(modules: set[str], root: str) -> bool:
    return any(m == root or m.startswith(root + ".") for m in modules)


def test_module_does_not_import_gpu_or_composite() -> None:
    """红线：本模块不 import moderngl / composite.core / PyQt6（任何导入形式）。"""
    imports = _protocol_module_imports()
    assert not _imports_root(imports, "moderngl"), (
        f"本模块不允许 import moderngl；发现：{sorted(imports)}"
    )
    assert not _imports_root(imports, "src.features.composite.core"), (
        f"本模块不允许 import composite.core；发现：{sorted(imports)}"
    )
    assert not _imports_root(imports, "PyQt6"), (
        f"纯数据 + Protocol，不允许 import PyQt6；发现：{sorted(imports)}"
    )


class TestVisualTokens:
    def test_default_tokens_present(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
        assert DEFAULT_VISUAL_TOKENS.node_corner_radius > 0
        assert DEFAULT_VISUAL_TOKENS.port_radius > 0

    def test_node_token_by_category(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
        t = DEFAULT_VISUAL_TOKENS.node_token("analysis")
        assert t.category == "analysis"
        assert t.color

    def test_node_token_unknown_falls_back(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
        t = DEFAULT_VISUAL_TOKENS.node_token("bogus")
        assert t.category == "bogus"

    def test_port_token_by_artifact_type(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
        t = DEFAULT_VISUAL_TOKENS.port_token("transcript_ref")
        assert t.artifact_type == "transcript_ref"
        assert t.color

    def test_status_color(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
        assert DEFAULT_VISUAL_TOKENS.status_color("running") != \
               DEFAULT_VISUAL_TOKENS.status_color("idle")

    def test_approval_token_is_diamond(self) -> None:
        """approval_token 端口用 diamond 形状（视觉区分审批）。"""
        from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
        t = DEFAULT_VISUAL_TOKENS.port_token("approval_token")
        assert t.shape == "diamond"


class TestGraphUiSignals:
    def test_all_signals_defined(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import GraphUiSignals
        for s in ("node_added", "node_removed", "node_connected",
                  "node_selected", "graph_changed", "zoom_changed",
                  "run_requested", "cancel_requested", "assets_dropped"):
            assert s in GraphUiSignals.ALL

    def test_no_gpu_signals(self) -> None:
        """红线：信号集不含 GPU 渲染相关（preview_requested/thumbnail）。"""
        from src.shared.node_canvas.graph_ui_protocol import GraphUiSignals
        assert "preview_requested" not in GraphUiSignals.ALL
        assert "thumbnail_requested" not in GraphUiSignals.ALL


class TestGraphUiProtocol:
    def test_protocol_is_runtime_checkable(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import GraphUiProtocol

        class _FakeCanvas:
            def build_from_graph(self, graph): pass
            def clear_all(self): pass
            def add_node_visual(self, node_id, spec_id, x=0, y=0): pass
            def remove_node_visual(self, node_id): pass
            def add_connection_visual(self, fn, fp, tn, tp): pass
            def remove_connection_visual(self, fn, fp, tn, tp): pass
            def set_node_status(self, node_id, status): pass
            def selected_node_ids(self): return ()
            def zoom_to(self, zoom): pass

        assert isinstance(_FakeCanvas(), GraphUiProtocol)


class TestNodeCanvasBase:
    def test_base_methods_raise_not_implemented(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import NodeCanvasBase
        base = NodeCanvasBase()
        with pytest.raises(NotImplementedError):
            base.build_from_graph(None)
        with pytest.raises(NotImplementedError):
            base.clear_all()

    def test_base_holds_visual_tokens(self) -> None:
        from src.shared.node_canvas.graph_ui_protocol import (
            DEFAULT_VISUAL_TOKENS, NodeCanvasBase, VisualTokens,
        )
        base = NodeCanvasBase()
        assert base.visual_tokens is DEFAULT_VISUAL_TOKENS
        custom = NodeCanvasBase(visual_tokens=VisualTokens(node_corner_radius=10))
        assert custom.visual_tokens.node_corner_radius == 10


class TestNoQtDependency:
    def test_protocol_module_importable_without_qt(self) -> None:
        """红线：本模块在无 Qt 环境下可导入（纯数据 + Protocol，AUD-23621）。

        用**子进程** + meta_path 钩子屏蔽 PyQt6 后真实重新执行模块导入——
        主进程里 ``importlib.import_module`` 命中 ``sys.modules`` 缓存不会重新
        执行模块，且当前环境装有 PyQt6，两者都证明不了 Qt 无关性。
        """
        import subprocess
        import sys
        from pathlib import Path

        # 工程根（src/ 的父目录）：tests → ai_workflow → features → src → root
        project_root = Path(__file__).resolve().parents[4]
        child_code = (
            "import sys\n"
            "class _QtBlocker:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname == 'PyQt6' or fullname.startswith('PyQt6.'):\n"
            "            raise ImportError(\n"
            "                f'PyQt6 is blocked in this test: {fullname}')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _QtBlocker())\n"
            "assert 'PyQt6' not in sys.modules\n"
            "import importlib\n"
            "mod = importlib.import_module("
            "'src.shared.node_canvas.graph_ui_protocol')\n"
            "assert hasattr(mod, 'GraphUiProtocol')\n"
            "assert hasattr(mod, 'DEFAULT_VISUAL_TOKENS')\n"
            "print('QT_FREE_IMPORT_OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", child_code],
            cwd=str(project_root),
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (
            f"无 Qt 环境导入失败（红线被破坏）：\nstdout={proc.stdout}\n"
            f"stderr={proc.stderr}"
        )
        assert "QT_FREE_IMPORT_OK" in proc.stdout
