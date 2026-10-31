"""INT-14 回归：SourceViewer GL facade 契约与绘制失败隔离。

审计记录（docs/edit_page_full_audit_optimization_plan_2026-10-14.md §10）：
真实窗口双击素材进入 Source 时 `_render_texture` 调用 `_gl.glUniform4f`
抛 AttributeError（未 import / 未加入 _GLWrapper），下一行 `glUniform1f`
同样缺失，进程直接退出。本测试从三方面守住修复：

1. AST 契约：模块内所有 `_gl.xxx` 调用必须在 _GLWrapper 与
   _GL_FACADE_REQUIRED 中存在，防止人工列表再次漂移；
2. mock GL 下 `_render_texture` 确实以正确参数调用
   glUniform4f/glUniform1f，且 uniform location 为 -1 时安全；
3. paintGL 遇到异常不再向 Qt 槽外传播，进入错误占位态且可继续调度。
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.edit_core

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "features" / "edit" / "ui" / "source_viewer.py"
)


def _gl_attribute_usages() -> set[str]:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "_gl"
        ):
            names.add(node.attr)
    return names


def test_gl_facade_covers_all_usages() -> None:
    """AST 契约：模块里用到的每个 _gl.xxx 都被 facade 暴露。"""
    from src.features.edit.ui.source_viewer import _GLWrapper

    used = _gl_attribute_usages()
    assert used, "AST scan unexpectedly found no _gl usages"
    missing = sorted(name for name in used if not hasattr(_GLWrapper, name))
    assert not missing, f"_GLWrapper missing APIs used via _gl.*: {missing}"


def test_gl_facade_required_set_matches_wrapper() -> None:
    from src.features.edit.ui.source_viewer import (
        GL_FACADE_CONTRACT_ERRORS,
        _GL_FACADE_REQUIRED,
        _GLWrapper,
    )

    assert not GL_FACADE_CONTRACT_ERRORS, (
        f"GL facade contract violated at import: {GL_FACADE_CONTRACT_ERRORS}"
    )
    missing = sorted(
        name for name in _GL_FACADE_REQUIRED if not hasattr(_GLWrapper, name)
    )
    assert not missing, f"_GLWrapper missing contract APIs: {missing}"


def test_uniform_apis_available_on_facade() -> None:
    """INT-14 直接回归：glUniform4f / glUniform1f 必须可用。"""
    from src.features.edit.ui.source_viewer import _GLWrapper

    assert callable(getattr(_GLWrapper, "glUniform4f", None))
    assert callable(getattr(_GLWrapper, "glUniform1f", None))


def _make_viewer(qtbot):
    from src.features.edit.ui.source_viewer import SourceViewer

    viewer = SourceViewer()
    qtbot.addWidget(viewer)
    # 不调用 show()/initializeGL——直接构造，用受控状态驱动 _render_texture
    viewer._shared_tex_id = None
    viewer._current_tex_id = 7
    viewer._u_texture_loc = 0
    viewer._u_display_rect_loc = 1
    viewer._u_flip_y_loc = 2
    return viewer


def test_render_texture_sets_display_and_flip_uniforms(qtbot, monkeypatch) -> None:
    """mock GL 下验证 4f/1f 参数；location=-1 也安全。"""
    import src.features.edit.ui.source_viewer as sv

    gl_mock = MagicMock()
    gl_mock.glIsTexture.return_value = True
    monkeypatch.setattr(sv, "_gl", gl_mock)

    viewer = _make_viewer(qtbot)
    viewer._render_texture((-0.5, -0.5, 1.0, 1.0))

    gl_mock.glUniform1i.assert_called_once_with(0, 0)
    gl_mock.glUniform4f.assert_called_once_with(1, -0.5, -0.5, 1.0, 1.0)
    gl_mock.glUniform1f.assert_called_once_with(2, 0.0)  # CPU 路径不翻转

    # GPU 共享纹理路径翻转为 1.0
    gl_mock.reset_mock()
    gl_mock.glIsTexture.return_value = True
    viewer._shared_tex_id = 9
    viewer._render_texture((0.0, 0.0, 2.0, 2.0))
    gl_mock.glUniform1f.assert_called_once_with(2, 1.0)


def test_render_texture_tolerates_missing_uniform_locations(
    qtbot, monkeypatch
) -> None:
    """被优化掉的 uniform（location=-1）按 GL 规范被忽略，不得崩溃。"""
    import src.features.edit.ui.source_viewer as sv

    gl_mock = MagicMock()
    gl_mock.glIsTexture.return_value = True
    monkeypatch.setattr(sv, "_gl", gl_mock)

    viewer = _make_viewer(qtbot)
    viewer._u_display_rect_loc = -1
    viewer._u_flip_y_loc = -1
    viewer._render_texture((-1.0, -1.0, 2.0, 2.0))  # 不抛异常即通过

    gl_mock.glUniform4f.assert_called_once_with(-1, -1.0, -1.0, 2.0, 2.0)
    gl_mock.glUniform1f.assert_called_once_with(-1, 0.0)


def test_paintgl_contains_render_failure(qtbot, monkeypatch) -> None:
    """paintGL 内部异常不得外传：进入错误占位态且后续可继续调度。"""
    import src.features.edit.ui.source_viewer as sv

    gl_mock = MagicMock()
    monkeypatch.setattr(sv, "_gl", gl_mock)

    viewer = _make_viewer(qtbot)
    viewer._gl_initialized = True
    viewer._prog = 3
    monkeypatch.setattr(
        viewer, "_render_texture", MagicMock(side_effect=RuntimeError("boom"))
    )

    # 不外传异常
    viewer.paintGL()
    assert viewer._gl_paint_failed is True

    # 错误态下后续 paint 只清屏占位，不再进入失败的绘制路径
    viewer._render_texture.reset_mock()
    viewer.paintGL()
    viewer._render_texture.assert_not_called()
