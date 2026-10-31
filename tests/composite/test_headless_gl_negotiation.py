"""P0-2: headless standalone-context capability negotiation.

The default ``create_standalone_context()`` negotiates GL 3.3, which
disables every compute-shader path. The export renderer must request a
compute-capable context on Windows/Linux, honour
``TOPOS_HEADLESS_GL_REQUIRE``, and fall back to the plain context (with an
explicit ``compute_unavailable`` log) instead of failing headless export.
"""

import os
import sys

import pytest

from src.features.composite.render.composite_export_renderer import (
    CompositeExportRenderer,
)

pytestmark = pytest.mark.composite_core


def _make_renderer() -> CompositeExportRenderer:
    return CompositeExportRenderer()


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.delenv("TOPOS_HEADLESS_GL_REQUIRE", raising=False)
    yield


def _require_ctx_creation():
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
        ctx.release()
    except Exception as exc:  # pragma: no cover - CI GPU availability
        pytest.skip(f"Cannot create standalone GL context: {exc}")


def _gl_major_minor(info: dict) -> tuple:
    version = info.get("GL_VERSION", "")
    parts = ""
    for ch in version:
        if ch.isdigit() or ch == ".":
            parts += ch
        else:
            break
    nums = parts.split(".")
    return (int(nums[0]), int(nums[1]) if len(nums) > 1 else 0)


class TestHeadlessContextNegotiation:
    def test_windows_linux_requests_compute_context(self, clean_env):
        """P0-2 core behaviour: headless context must not silently be 3.3 on
        platforms that can expose GL 4.3+ (compute shaders)."""
        _require_ctx_creation()
        if sys.platform == "darwin":
            pytest.skip("macOS keeps the GL 4.1 fragment fallback")
        renderer = _make_renderer()
        ctx = renderer._create_headless_context()
        try:
            assert _gl_major_minor(ctx.info) >= (4, 3), (
                f"expected compute-capable context, got "
                f"{ctx.info.get('GL_VERSION')}")
        finally:
            ctx.release()

    def test_env_override_requests_specific_version(self, monkeypatch,
                                                     clean_env):
        _require_ctx_creation()
        monkeypatch.setenv("TOPOS_HEADLESS_GL_REQUIRE", "330")
        renderer = _make_renderer()
        ctx = renderer._create_headless_context()
        try:
            if sys.platform == "darwin":
                # macOS 的 GL 实现只有 2.1 legacy / 4.1 core 两档——请求
                # 3.3 会被提升到 4.1（功能超集 ⊇ 3.3），字面相等在
                # darwin 上不可满足（T-P1-71 平台假设修正）。
                assert _gl_major_minor(ctx.info) >= (3, 3)
            else:
                assert _gl_major_minor(ctx.info) == (3, 3)
        finally:
            ctx.release()

    def test_invalid_env_value_falls_back_gracefully(self, monkeypatch,
                                                     clean_env):
        _require_ctx_creation()
        monkeypatch.setenv("TOPOS_HEADLESS_GL_REQUIRE", "not-a-number")
        renderer = _make_renderer()
        ctx = renderer._create_headless_context()
        try:
            # Invalid override is ignored; normal negotiation applies.
            if sys.platform != "darwin":
                assert _gl_major_minor(ctx.info) >= (4, 3)
        finally:
            ctx.release()

    def test_unfulfillable_requirement_falls_back(self, monkeypatch,
                                                  clean_env):
        """A version the driver cannot provide must degrade to the plain
        context, not make headless export unavailable."""
        _require_ctx_creation()
        monkeypatch.setenv("TOPOS_HEADLESS_GL_REQUIRE", "99")
        renderer = _make_renderer()
        ctx = renderer._create_headless_context()
        try:
            assert ctx is not None
        finally:
            ctx.release()
