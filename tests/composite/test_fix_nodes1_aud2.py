"""Regression tests for wave-2 batch A node fixes (AUD-6500/6603/6708).

Headless: no GL context required — the GPU-touching paths are driven
through fakes that mirror the real collaborators' surface (moderngl
objects, GPUResourcePool, FramebufferManager, evaluator mixins).

Covered findings:
- AUD-6708: FileOutputNode._readback_texture referenced
  ``moderngl.LINEAR`` before a function-local ``import moderngl`` →
  UnboundLocalError on every export → readback always failed.
  (Attached: AUD-6709 — VAO/VBO now released on the failure path.)
- AUD-6603: DeflickerNode per-frame output textures had no owner and
  were never released (unbounded VRAM growth).  They now live in the
  node's bounded registry: reuse by (ctx, size, frame) key, retire on
  overflow / context change / cleanup, cache invalidation flagged.
- AUD-6500: ColorCorrectNode's effect params must cover every uniform
  the shader declares (node side locked here); the dispatcher-side full
  mapping is AUD-5902 (color_evaluator.py — owned by the evaluator fix
  batch) and is locked by the xfail test at the bottom.
"""

import re
import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import moderngl

from src.features.composite.core import shaders as shader_sources
from src.features.composite.evaluators.color_evaluator import ColorEvaluatorMixin
from src.features.composite.nodes import deflicker_node as deflicker_module
from src.features.composite.nodes import _gpu_readback
from src.features.composite.core import gl_bind
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.color_correct_node import ColorCorrectNode
from src.features.composite.nodes.deflicker_node import DeflickerNode
from src.features.composite.nodes.file_output_node import FileOutputNode

pytestmark = pytest.mark.composite_core


# ═══════════════════════════════════════════════════════════════════
# AUD-6708 — FileOutputNode readback
# ═══════════════════════════════════════════════════════════════════


class _ReadbackUniform:
    def __init__(self, name: str) -> None:
        self.name = name
        self.value = None


class _ReadbackProgram:
    """Program double accepting exactly the given uniform names —
    unknown names raise KeyError, mirroring moderngl.Program.__getitem__."""

    def __init__(self, names=("u_texture",)) -> None:
        self.uniforms = {name: _ReadbackUniform(name) for name in names}

    def __getitem__(self, name: str) -> _ReadbackUniform:
        uniform = self.uniforms.get(name)
        if uniform is None:
            raise KeyError(name)
        return uniform

    def release(self) -> None:
        pass


class _ReadbackTex:
    _next_glo = 500

    def __init__(self, w: int, h: int) -> None:
        _ReadbackTex._next_glo += 1
        self.glo = _ReadbackTex._next_glo
        self.width = w
        self.height = h
        self.filter = None
        self.released = False

    def release(self) -> None:
        self.released = True


class _ReadbackFBO:
    def __init__(self, tex: _ReadbackTex) -> None:
        self._tex = tex
        self.used = 0
        self.released = False

    def use(self) -> None:
        self.used += 1

    def read(self, components: int = 4, dtype: str = 'f4') -> bytes:
        return bytes(self._tex.width * self._tex.height * components * 4)

    def release(self) -> None:
        self.released = True


class _ReadbackVAO:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.rendered = False
        self.released = False

    def render(self, mode: int) -> None:
        self.rendered = True
        if self._fail:
            raise RuntimeError("simulated render failure")

    def release(self) -> None:
        self.released = True


class _ReadbackVBO:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class _ReadbackCtx:
    """Faithful-enough moderngl Context surface for _readback_texture."""

    def __init__(self, vao_fail: bool = False) -> None:
        self.viewport = (0, 0, 0, 0)
        self.textures: list = []
        self.fbos: list = []
        self.vaos: list = []
        self.vbos: list = []
        self.programs: list = []
        self.finished = 0
        self._vao_fail = vao_fail

    def texture(self, size, components, dtype='f1', data=None):
        tex = _ReadbackTex(size[0], size[1])
        self.textures.append(tex)
        return tex

    def framebuffer(self, color_attachments=None):
        fbo = _ReadbackFBO(color_attachments[0])
        self.fbos.append(fbo)
        return fbo

    def program(self, vertex_shader=None, fragment_shader=None):
        prog = _ReadbackProgram()
        self.programs.append(prog)
        return prog

    def buffer(self, data):
        vbo = _ReadbackVBO()
        self.vbos.append(vbo)
        return vbo

    def vertex_array(self, prog, attributes):
        vao = _ReadbackVAO(fail=self._vao_fail)
        self.vaos.append(vao)
        return vao

    def clear(self, *args, **kwargs) -> None:
        pass

    def finish(self) -> None:
        self.finished += 1


class TestAud6708FileOutputReadback:
    """_readback_texture must run past moderngl.LINEAR (AUD-6708)."""

    @pytest.fixture(autouse=True)
    def _mock_native_fbo_boundary(self, monkeypatch):
        """QA-02：fake GL 表面必须同时 mock 全部原生 GL 边界。

        ``_readback_texture`` 经 ``_query_draw_fbo_binding`` 调用原生
        ``glGetIntegerv``（AUD-6712 保存/恢复绘制 FBO 绑定）。本测试类
        没有真实 OpenGL context，原生查询在无 context 进程里是 SIGSEGV
        （returncode=-11），不是可捕获的 Python 异常——宽泛 except 挡不住
        native crash。mock 查询/恢复两个边界后 fake ctx 路径才完整离线。
        """
        monkeypatch.setattr(
            FileOutputNode, "_query_draw_fbo_binding",
            staticmethod(lambda: None))
        monkeypatch.setattr(
            FileOutputNode, "_restore_draw_fbo_binding",
            staticmethod(lambda name: None))

    def test_moderngl_resolves_at_module_level(self):
        """The module must expose moderngl — the pre-fix function-local
        import made the name local for the whole function scope, so the
        L227 reference raised UnboundLocalError."""
        from src.features.composite.nodes import file_output_node
        assert file_output_node.moderngl is moderngl

    def test_readback_returns_rgba_array(self, monkeypatch):
        """End-to-end readback through a fake GL surface returns the
        (h, w, 4) float32 array instead of None (pre-fix: the
        UnboundLocalError was swallowed by the except → None → every
        export reported "GPU readback failed")."""
        monkeypatch.setattr(gl_bind, "bind_texture_unit",
                            lambda *a, **k: None)
        node = FileOutputNode("aud6708")
        src = TextureResult(texture_id=7, width=4, height=3)
        ctx = _ReadbackCtx()

        out = node._readback_texture(src, ctx, fb_manager=None)

        assert out is not None
        assert out.shape == (3, 4, 4)
        assert out.dtype == np.float32
        # Temporary GL objects are all released on the success path.
        assert ctx.textures[0].released
        assert ctx.fbos[0].released
        assert all(vao.released for vao in ctx.vaos)
        assert all(vbo.released for vbo in ctx.vbos)

    def test_readback_releases_vao_vbo_on_render_failure(self, monkeypatch):
        """AUD-6709 (attached): a mid-render exception must not leak the
        VAO/VBO — they are released in the finally block alongside the
        readback objects."""
        monkeypatch.setattr(gl_bind, "bind_texture_unit",
                            lambda *a, **k: None)
        node = FileOutputNode("aud6709")
        src = TextureResult(texture_id=7, width=4, height=3)
        ctx = _ReadbackCtx(vao_fail=True)

        out = node._readback_texture(src, ctx, fb_manager=None)

        assert out is None  # failure still degrades to None
        assert all(vao.released for vao in ctx.vaos)
        assert all(vbo.released for vbo in ctx.vbos)
        assert ctx.textures[0].released
        assert ctx.fbos[0].released


# ═══════════════════════════════════════════════════════════════════
# AUD-6603 — DeflickerNode output texture ownership
# ═══════════════════════════════════════════════════════════════════


class _FakeTex:
    _next_glo = 900

    def __init__(self, w: int, h: int) -> None:
        _FakeTex._next_glo += 1
        self.glo = _FakeTex._next_glo
        self.width = w
        self.height = h
        self.written: list = []
        self.released = False
        self.release_count = 0

    def write(self, data) -> None:
        self.written.append(data)

    def release(self) -> None:
        self.release_count += 1
        self.released = True


class _FakeGL:
    """Context double counting raw texture allocations."""

    def __init__(self) -> None:
        self.created: list = []
        self.viewport = (0, 0, 0, 0)

    def texture(self, size, components, dtype='f1', data=None):
        tex = _FakeTex(size[0], size[1])
        self.created.append(tex)
        return tex


class _FakePool:
    """GPUResourcePool double — acquire/release bookkeeping only."""

    def __init__(self) -> None:
        self.acquired: list = []
        self.released: list = []

    def acquire_texture(self, w, h, components=4, dtype='f2'):
        tex = _FakeTex(w, h)
        self.acquired.append(tex)
        return tex

    def release_texture(self, tex) -> None:
        self.released.append(tex)


class _FakeFBManager:
    """FramebufferManager double without release_temp_texture — forces
    the direct-release retirement path when no pool is available."""

    def __init__(self) -> None:
        self._fbo_cache = {}

    def get_fbo(self, tex):
        return _NoopFBO()


class _NoopFBO:
    def use(self) -> None:
        pass


def _rgba(w: int = 4, h: int = 3) -> "np.ndarray":
    return np.zeros((h, w, 4), dtype=np.float32)


def _image(tex: _FakeTex) -> TextureResult:
    return TextureResult(texture_id=tex.glo, width=tex.width,
                         height=tex.height, format="f4", bbox=None,
                         alpha_premultiplied=False, color_space="linear")


class TestAud6603OutputRing:
    """Output textures are owned, reused by key, and retired."""

    def test_upload_reuses_texture_for_same_frame(self):
        """Re-evaluating the same (ctx, size, frame) renders into the
        existing texture instead of allocating a second one."""
        gl = _FakeGL()
        node = DeflickerNode("reuse")
        src = _FakeTex(4, 3)

        first = node._upload_result(_rgba(), _image(src), gl, frame=7)
        second = node._upload_result(_rgba(), _image(src), gl, frame=7)

        assert first is not None and second is not None
        assert first.texture_id == second.texture_id
        assert len(gl.created) == 1
        assert gl.created[0].written  # content uploaded, not just allocated
        # Content overwrite of a glo that cache entries may hold → the
        # evaluator must be told to drop stale entries.
        assert node._cache_invalidated_needed is True

    def test_ring_is_bounded_and_releases_old_textures(self, monkeypatch):
        """Beyond the entry budget the oldest textures are released —
        VRAM no longer grows with the number of evaluated frames."""
        monkeypatch.setattr(deflicker_module, "_OUTPUT_RING_MAX_ENTRIES", 3)
        monkeypatch.setattr(deflicker_module, "_OUTPUT_RING_MAX_BYTES",
                            1 << 40)
        gl = _FakeGL()
        node = DeflickerNode("bounded")
        src = _FakeTex(4, 3)

        results = [node._upload_result(_rgba(), _image(src), gl, frame=f)
                   for f in range(10)]

        assert all(r is not None for r in results)
        assert len(gl.created) == 10
        assert len(node._output_ring) == 3
        # Every texture except the three youngest was released exactly once.
        live_glos = {r.texture_id for r in results[-3:]}
        released = [t for t in gl.created if t.glo not in live_glos]
        assert len(released) == 7
        assert all(t.release_count == 1 for t in released)
        assert node._cache_invalidated_needed is True

    def test_pool_textures_retire_through_the_pool(self, monkeypatch):
        """Pool-acquired outputs return to the pool on ring overflow
        (release_texture), not to the void."""
        monkeypatch.setattr(deflicker_module, "_OUTPUT_RING_MAX_ENTRIES", 8)
        # 4×3 RGBA f4 = 192 bytes per texture; 576 bytes → 3 entries.
        monkeypatch.setattr(deflicker_module, "_OUTPUT_RING_MAX_BYTES", 576)
        gl = _FakeGL()
        pool = _FakePool()
        node = DeflickerNode("pool")
        src = _FakeTex(4, 3)

        for f in range(5):
            result = node._upload_result(_rgba(), _image(src), gl,
                                         frame=f, resource_pool=pool,
                                         fb_manager=_FakeFBManager())
            assert result is not None

        assert len(pool.acquired) == 5
        # Budget of 3 textures → the two oldest were returned to the pool.
        assert len(pool.released) == 2
        assert len(node._output_ring) == 3
        assert all(t in pool.acquired for t in pool.released)
        # Nothing went through the raw-context path.
        assert gl.created == []

    def test_context_switch_drops_old_entries_without_release(self):
        """After GL context recreation the old entries' objects are dead —
        the node drops them WITHOUT calling release() (particle
        precedent) and flags cache invalidation."""
        gl_a = _FakeGL()
        gl_b = _FakeGL()
        node = DeflickerNode("ctxswitch")
        src = _FakeTex(4, 3)

        node._upload_result(_rgba(), _image(src), gl_a, frame=1)
        node._upload_result(_rgba(), _image(src), gl_a, frame=2)
        node._upload_result(_rgba(), _image(src), gl_b, frame=3)

        assert len(node._output_ring) == 1
        assert all(key[0] is gl_b for key in node._output_ring)
        # Dead-context objects must not be released (would raise / is GL
        # usage on a gone context).
        assert all(t.release_count == 0 for t in gl_a.created)
        assert node._cache_invalidated_needed is True

    def test_cleanup_releases_every_owned_resource(self):
        """cleanup() retires the whole registry plus the luma texture,
        programs and quad VBO (removal-path half of AUD-6604)."""
        gl = _FakeGL()
        node = DeflickerNode("cleanup")
        src = _FakeTex(4, 3)
        for f in range(4):
            assert node._upload_result(_rgba(), _image(src), gl,
                                       frame=f) is not None
        released = []

        class _Releasable:
            def release(self) -> None:
                released.append(self)

        node._luma_tex = _Releasable()
        node._luma_prog = _Releasable()
        node._composite_prog = _Releasable()
        node._gpu_vbo = _Releasable()

        node.cleanup()

        assert len(node._output_ring) == 0
        assert node._output_ring_bytes == 0
        assert all(t.release_count == 1 for t in gl.created)
        assert len(released) == 4
        assert node._cache_invalidated_needed is True

    def test_gpu_composite_path_registers_output(self, monkeypatch):
        """The GPU composite path (pool acquire → render) also routes its
        output through the registry and reuses it for the same frame."""
        gl = _FakeGL()
        gl.program = lambda **kwargs: _ReadbackProgram(
            ("u_texture", "u_gain_tex", "u_gain", "u_use_tex"))
        gl.buffer = lambda data: _ReadbackVBO()
        gl.vertex_array = lambda prog, attrs: _ReadbackVAO()
        pool = _FakePool()
        fb = _FakeFBManager()
        monkeypatch.setattr(gl_bind, "bind_texture_unit",
                            lambda *a, **k: None)
        monkeypatch.setattr(_gpu_readback, "find_texture",
                            lambda tex_id, fbm, rp: _FakeTex(4, 3))
        monkeypatch.setattr(
            DeflickerNode, "_ensure_gpu_resources",
            lambda self, g: (object(), _ReadbackProgram(
                ("u_texture", "u_gain_tex", "u_gain", "u_use_tex")),
                object()))
        node = DeflickerNode("gpu")
        src = _FakeTex(4, 3)

        out1 = node._composite_gpu(_image(src), None, 1.5, gl, fb, pool,
                                   frame=5)
        out2 = node._composite_gpu(_image(src), None, 1.5, gl, fb, pool,
                                   frame=5)

        assert out1 is not None and out2 is not None
        assert out1.texture_id == out2.texture_id
        # One allocation; the second call reused the registered texture.
        assert len(pool.acquired) == 1
        assert len(node._output_ring) == 1
        assert node._cache_invalidated_needed is True  # reuse overwrite


# ═══════════════════════════════════════════════════════════════════
# AUD-6500 — ColorCorrectNode ↔ shader/dispatcher contract
# ═══════════════════════════════════════════════════════════════════

_UNIFORM_RE = re.compile(r"uniform\s+\w+\s+(\w+)\s*(?:\[[^\]]*\])?\s*;")


def _shader_uniform_names(fragment_source: str) -> set:
    return {m.group(1) for m in _UNIFORM_RE.finditer(fragment_source)}


_NODE_PARAM_VALUES = {
    "exposure": 1.5,
    "contrast": 1.25,
    "saturation": 0.75,
    "hue_shift": 30.0,
    "temperature": 0.2,
    "tint": -0.1,
    "shadows_lifting_r": 0.1, "shadows_lifting_g": 0.2,
    "shadows_lifting_b": 0.3,
    "midtones_gamma_r": 1.1, "midtones_gamma_g": 1.2,
    "midtones_gamma_b": 1.3,
    "highlights_gain_r": 0.9, "highlights_gain_g": 1.0,
    "highlights_gain_b": 1.1,
    "channel_r": True, "channel_g": True, "channel_b": True,
    "channel_a": True,
    "mix": 0.8,
}


class TestAud6500ColorCorrectContract:
    """The node's _effect_params must cover the shader's uniform set."""

    def _marker_params(self) -> dict:
        node = ColorCorrectNode("aud6500")
        for key, value in _NODE_PARAM_VALUES.items():
            node.parameters[key] = value
        image = TextureResult(texture_id=7, width=4, height=4)
        outputs = node._do_evaluate({"image": image}, None, None)
        assert "_effect_params" in outputs
        return outputs["_effect_params"]

    def test_marker_params_cover_shader_uniforms(self):
        """Static contract lock: every u_* the _COLOR_CORRECT_FRAGMENT
        declares (minus the texture/mask samplers) has a param the node
        sends, and the node sends nothing the shader lacks."""
        params = self._marker_params()
        shader_keys = _shader_uniform_names(
            shader_sources._COLOR_CORRECT_FRAGMENT)
        expected = {name[len("u_"):] for name in shader_keys
                    if name not in ("u_texture", "u_mask")}
        assert set(params) == expected
        # Vector uniforms arrive as 3/4-component sequences.
        assert list(params["shadows_lifting"]) == [0.1, 0.2, 0.3]
        assert list(params["channel_selector"]) == [1.0, 1.0, 1.0, 1.0]

    def test_no_changes_passes_input_through(self):
        """All-default parameters keep the zero-cost passthrough."""
        node = ColorCorrectNode("default")
        image = TextureResult(texture_id=7, width=4, height=4)
        outputs = node._do_evaluate({"image": image}, None, None)
        assert outputs == {"result": image}
        assert "_effect_params" not in outputs


class _CCUniform:
    def __init__(self, name: str) -> None:
        self.name = name
        self.value = None


class _CCProgram:
    """Accepts exactly the uniforms the real shader declares — unknown
    names raise KeyError, mirroring moderngl.Program.__getitem__."""

    def __init__(self, fragment_source: str) -> None:
        self.uniforms = {name: _CCUniform(name)
                         for name in _shader_uniform_names(fragment_source)}

    def __getitem__(self, name: str) -> _CCUniform:
        uniform = self.uniforms.get(name)
        if uniform is None:
            raise KeyError(name)
        return uniform


class _CCEvaluatorHarness(ColorEvaluatorMixin):
    """Evaluator stand-in with stubbed BaseEvaluatorMixin surface."""

    def __init__(self, programs: dict) -> None:
        self.utility_shaders = programs
        self._resource_pool = _FakePool()
        self.fb_manager = _FakeFBManager()
        self.ctx = _ReadbackCtx()

    def _render_quad(self, prog) -> None:
        pass

    def _bind_external_texture(self, texture_id) -> None:
        pass

    def _bind_mask_texture(self, texture_id) -> None:
        pass

    def _get_white_mask_texture(self):
        return _FakeTex(1, 1)


# 曾以 xfail 锁定 AUD-5902/AUD-6500（分发器只映射 3/13 uniform，
# channel_selector 缺省 0 抵消全部校正）；完整映射已落地，翻正断言。
class TestAud6500DispatcherMapping:
    """Node→dispatcher full-parameter mapping (AUD-5902 dependency)."""

    def test_apply_color_correct_maps_every_uniform(self):
        node = ColorCorrectNode("aud6500-dispatch")
        for key, value in _NODE_PARAM_VALUES.items():
            node.parameters[key] = value
        image = TextureResult(texture_id=7, width=4, height=4)
        effect_params = node._do_evaluate(
            {"image": image}, None, None)["_effect_params"]

        program = _CCProgram(shader_sources._COLOR_CORRECT_FRAGMENT)
        harness = _CCEvaluatorHarness({"color_correct": program})
        result = harness._apply_color_correct(image, effect_params, 4, 4)

        out = result["result"]
        assert isinstance(out, TextureResult) and out.is_valid

        def as_list(value):
            return list(value) if isinstance(value, (list, tuple)) else value

        uniforms = program.uniforms
        assert uniforms["u_exposure"].value == 1.5
        assert uniforms["u_contrast"].value == 1.25
        assert uniforms["u_saturation"].value == 0.75
        assert uniforms["u_hue_shift"].value == 30.0
        assert uniforms["u_temperature"].value == 0.2
        assert uniforms["u_tint"].value == -0.1
        assert as_list(uniforms["u_shadows_lifting"].value) == [0.1, 0.2, 0.3]
        assert as_list(uniforms["u_midtones_gamma"].value) == [1.1, 1.2, 1.3]
        assert as_list(uniforms["u_highlights_gain"].value) == [0.9, 1.0, 1.1]
        assert as_list(uniforms["u_channel_selector"].value) == \
            [1.0, 1.0, 1.0, 1.0]
        assert uniforms["u_mix"].value == 0.8
