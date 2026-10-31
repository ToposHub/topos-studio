"""Regression tests for audit batch A fixes (AUD-5901/6001/6105/6111).

Headless: the evaluator-path tests need no GL context. The harness below
fakes the evaluator collaborators (programs, resource pool, FBO manager,
context) while exercising the real mixin methods. Fake programs derive
their accepted uniform set from the actual GLSL sources in
``src/features/composite/core/shaders.py`` so evaluator/shader drift
(e.g. setting a uniform the shader does not declare) fails loudly.

Covered findings:
- AUD-5901: _apply_color_correct set a nonexistent u_brightness uniform
  (KeyError on every evaluation → empty ColorCorrect output).
- AUD-6001: erode/dilate iterations>=2 touched a nonexistent
  GPUResourcePool._texture_by_id attribute (AttributeError).
- AUD-6105: DeepMerge cleared the depth output with ctx.clear(color=1.0)
  — a scalar, which moderngl rejects with TypeError.
- AUD-6111: STANDARD_NAMES missed 4 registered shaders (roto, mask_merge,
  motion_blur_accum, mesh_pbr) so those code paths always degraded.
"""

import re

import pytest

from src.features.composite.core import gl_bind
from src.features.composite.core import shaders as shader_sources
from src.features.composite.evaluators.color_evaluator import ColorEvaluatorMixin
from src.features.composite.evaluators.filter_evaluator import FilterEvaluatorMixin
from src.features.composite.evaluators.merge_evaluator import MergeEvaluatorMixin
from src.features.composite.evaluators.shader_library import ShaderLibrary
from src.features.composite.nodes.base_node import TextureResult


# ──────────────────────────── fakes ────────────────────────────


class _Uniform:
    """Records the last value assigned to one uniform slot."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.value = None


class _FakeProgram:
    """Program accepting exactly the uniforms a GLSL source declares.

    Unknown names raise KeyError, mirroring moderngl.Program.__getitem__
    for missing / optimized-out uniforms.
    """

    def __init__(self, fragment_source: str) -> None:
        self.uniforms: dict = {}
        for match in re.finditer(
                r"uniform\s+\w+\s+(\w+)\s*(?:\[[^\]]*\])?\s*;",
                fragment_source):
            self.uniforms[match.group(1)] = _Uniform(match.group(1))

    def __getitem__(self, name: str) -> _Uniform:
        uniform = self.uniforms.get(name)
        if uniform is None:
            raise KeyError(name)
        return uniform


class _FakeTexture:
    _next_glo = 100

    def __init__(self) -> None:
        _FakeTexture._next_glo += 1
        self.glo = _FakeTexture._next_glo


class _FakeFBO:
    def __init__(self) -> None:
        self.used = 0

    def use(self) -> None:
        self.used += 1


class _FakeFramebufferManager:
    def __init__(self) -> None:
        self.fbos: list = []

    def get_fbo(self, texture) -> _FakeFBO:
        fbo = _FakeFBO()
        self.fbos.append(fbo)
        return fbo


class _FakeResourcePool:
    """Pool double exposing only the real GPUResourcePool public surface.

    Deliberately does NOT define ``_texture_by_id`` — that attribute never
    existed on the real pool (AUD-6001).
    """

    def __init__(self) -> None:
        self.acquired: list = []
        self.released_by_id: list = []
        self.released_objs: list = []

    def acquire_texture(self, w: int, h: int, **kwargs):
        tex = _FakeTexture()
        self.acquired.append(tex)
        return tex

    def acquire_depth_texture(self, w: int, h: int, **kwargs):
        tex = _FakeTexture()
        self.acquired.append(tex)
        return tex

    def release_texture(self, tex) -> None:
        self.released_objs.append(tex)

    def release_texture_by_id(self, tex_id: int, force_evict: bool = False) -> None:
        self.released_by_id.append(tex_id)


class _FakeContext:
    """Faithful emulation of moderngl 5.12 Context.clear argument rules."""

    def __init__(self) -> None:
        self.viewport = (0, 0, 0, 0)
        self.clear_calls: list = []

    def clear(self, red: float = 0.0, green: float = 0.0, blue: float = 0.0,
              alpha: float = 0.0, depth: float = 1.0, viewport=None,
              color=None) -> None:
        # moderngl 5.12: `tuple(color)` — a scalar raises TypeError.
        if color is not None:
            red, green, blue, alpha, *_ = tuple(color) + (0.0,) * 4
        self.clear_calls.append((red, green, blue, alpha, depth))


class _EvaluatorHarness(ColorEvaluatorMixin, FilterEvaluatorMixin,
                        MergeEvaluatorMixin):
    """Evaluator stand-in with stubbed BaseEvaluatorMixin collaborators."""

    def __init__(self, programs: dict) -> None:
        self.utility_shaders = programs
        self._resource_pool = _FakeResourcePool()
        self.fb_manager = _FakeFramebufferManager()
        self.ctx = _FakeContext()
        self._white_mask = _FakeTexture()

    # ── BaseEvaluatorMixin surface ──
    def _render_quad(self, prog) -> None:  # noqa: ARG002
        pass

    def _bind_external_texture(self, texture_id: int) -> None:  # noqa: ARG002
        pass

    def _bind_mask_texture(self, texture_id: int) -> None:  # noqa: ARG002
        pass

    def _get_white_mask_texture(self):
        return self._white_mask

    def _check_color_space_mismatch(self, a, b, tag: str) -> None:  # noqa: ARG002
        pass

    def _resolve_merge_dims(self, a, b, policy: str) -> tuple:  # noqa: ARG002
        return (max(a.width or 1, b.width or 1), max(a.height or 1, b.height or 1))

    def _resolve_merge_format(self, a, b, policy: str):  # noqa: ARG002
        return a.format

    def _resolve_merge_bbox(self, a, b, policy: str):  # noqa: ARG002
        return a.bbox


def _make_input(texture_id: int = 7, **kwargs) -> TextureResult:
    defaults = dict(width=4, height=4, alpha_premultiplied=True,
                    color_space="linear")
    defaults.update(kwargs)
    return TextureResult(texture_id=texture_id, **defaults)


# ──────────────────────────── AUD-5901 ────────────────────────────


class TestColorCorrectUniformContract:
    """_apply_color_correct must only set uniforms the shader declares."""

    def test_no_missing_uniform_keyerror(self):
        """Sanity: the shader really does not declare u_brightness."""
        program = _FakeProgram(shader_sources._COLOR_CORRECT_FRAGMENT)
        assert "u_brightness" not in program.uniforms
        assert "u_saturation" in program.uniforms

    def test_apply_color_correct_completes(self):
        """The evaluation path must not raise KeyError (AUD-5901)."""
        harness = _EvaluatorHarness(
            {"color_correct": _FakeProgram(
                shader_sources._COLOR_CORRECT_FRAGMENT)})
        result = harness._apply_color_correct(
            _make_input(), {"saturation": 1.2, "contrast": 1.1}, 4, 4)

        out = result["result"]
        assert isinstance(out, TextureResult)
        assert out.is_valid
        assert out.texture_id == harness._resource_pool.acquired[0].glo

    def test_saturation_and_contrast_reach_the_shader(self):
        program = _FakeProgram(shader_sources._COLOR_CORRECT_FRAGMENT)
        harness = _EvaluatorHarness({"color_correct": program})
        harness._apply_color_correct(
            _make_input(), {"saturation": 1.25, "contrast": 0.9, "mix": 0.5},
            4, 4)

        assert program.uniforms["u_saturation"].value == 1.25
        assert program.uniforms["u_contrast"].value == 0.9
        assert program.uniforms["u_mix"].value == 0.5


# ──────────────────────────── AUD-6001 ────────────────────────────


class TestErodeDilateIterationRelease:
    """iterations>=2 on the fragment path must release intermediates via
    the public by-id API instead of a nonexistent private dict."""

    def test_two_iterations_do_not_raise(self):
        program = _FakeProgram(shader_sources._ERODE_DILATE_FRAGMENT)
        harness = _EvaluatorHarness({"erode_dilate": program})
        # No _compute_shader_manager attribute → fragment path is taken.
        result = harness._apply_erode_dilate(
            _make_input(),
            {"operation": "erode", "size": 3, "iterations": 2},
            4, 4)

        pool = harness._resource_pool
        # Two iterations → two pooled textures acquired.
        assert len(pool.acquired) == 2
        # The intermediate texture is released exactly once, by id.
        assert pool.released_by_id == [pool.acquired[0].glo]
        # The final output is the second texture.
        assert result["result"].texture_id == pool.acquired[1].glo

    def test_single_iteration_releases_nothing(self):
        program = _FakeProgram(shader_sources._ERODE_DILATE_FRAGMENT)
        harness = _EvaluatorHarness({"erode_dilate": program})
        harness._apply_erode_dilate(
            _make_input(), {"operation": "dilate", "size": 2}, 4, 4)

        assert harness._resource_pool.released_by_id == []
        assert harness._resource_pool.released_objs == []


# ──────────────────────────── AUD-6105 ────────────────────────────


class TestDeepMergeDepthClear:
    """The depth-output clear must be a 4-component color (red=far=1.0)."""

    def _run_deep_merge(self, monkeypatch) -> _EvaluatorHarness:
        bound: list = []
        monkeypatch.setattr(gl_bind, "bind_texture_unit",
                            lambda tex_id, unit: bound.append((tex_id, unit)))
        programs = {
            "deep_merge": _FakeProgram(shader_sources._DEEP_MERGE_FRAGMENT),
            "depth_output": _FakeProgram(shader_sources._DEPTH_OUTPUT_FRAGMENT),
        }
        harness = _EvaluatorHarness(programs)
        outputs = {
            "_effect_input_a": _make_input(7, depth_texture_id=11,
                                           has_deep_data=True),
            "_effect_input_b": _make_input(8),
            "_effect_params": {"has_depth_a": True, "has_depth_b": False},
        }
        harness._apply_deep_merge(outputs)
        return harness

    def test_depth_path_completes_without_typeerror(self, monkeypatch):
        harness = self._run_deep_merge(monkeypatch)
        # moderngl rejects scalar colors with TypeError; completing the
        # evaluation proves the fix (AUD-6105).
        assert harness.ctx.clear_calls

    def test_depth_buffer_cleared_to_far(self, monkeypatch):
        harness = self._run_deep_merge(monkeypatch)
        # The depth texture is an R32F *color* attachment; red=1.0 is the
        # shader's far-depth convention.
        assert (1.0, 1.0, 1.0, 1.0, 1.0) in harness.ctx.clear_calls


# ──────────────────────────── AUD-6111 ────────────────────────────


class TestStandardNamesRegistration:
    """Every registered shader source must be reachable through
    STANDARD_NAMES (the sole utility_shaders population path)."""

    MISSING = ("roto", "mask_merge", "motion_blur_accum", "mesh_pbr")

    def test_missing_names_are_registered(self):
        for name in self.MISSING:
            assert name in ShaderLibrary.STANDARD_NAMES, name

    def test_standard_names_and_sources_are_in_sync(self):
        """No source without a STANDARD_NAMES entry and vice versa.

        AUD-6113 后 ``fused``/``fused_color`` 不再回写类级共享
        ``_SHADER_SOURCES``，改由 ``_resolve_fused_sources`` 在实例锁内
        现取——源集合 = ``_SHADER_SOURCES`` ∪ 惰性可解析名，须与
        STANDARD_NAMES 完全一致，且类字典在解析前后保持不被就地变更。
        """
        library = ShaderLibrary(ctx=None)
        before = dict(ShaderLibrary._SHADER_SOURCES)
        lazy = {
            name for name in ShaderLibrary._FUSED_SOURCE_NAMES
            if library._resolve_fused_sources(name) is not None
        }
        assert (set(ShaderLibrary._SHADER_SOURCES) | lazy
                == set(ShaderLibrary.STANDARD_NAMES))
        # 类级共享字典不被惰性解析就地变更（AUD-6113 契约）
        assert dict(ShaderLibrary._SHADER_SOURCES) == before
        assert not lazy & set(ShaderLibrary._SHADER_SOURCES)

    def test_missing_names_compile_and_resolve(self):
        """GL-gated: the 4 shaders compile and resolve through the same
        get_program lookup the evaluator init loop uses."""
        moderngl = pytest.importorskip("moderngl")
        try:
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover - no GL on this host
            pytest.skip(f"standalone GL context unavailable: {exc}")
        try:
            library = ShaderLibrary(ctx)
            for name in self.MISSING:
                program = library.get_program(name)
                assert program is not None, name
        finally:
            ctx.release()
