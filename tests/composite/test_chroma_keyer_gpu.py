"""Chroma keying orientation — GPU pixel-level regression.

T-P1-60 批次 B（W05）：给 chroma_keyer 接绿幕 fixture 时发现两个 chroma
shader 的 matte 方向是反的 —— 屏幕色（幕布）被保留、前景被抠掉。修复后
用真实 GL 上下文钉住方向语义：

  - 屏幕色附近 → matte 0（透明，result 模式下预乘为黑）
  - 前景（远离屏幕色）→ matte 1（不透明）

覆盖两条路径：
  - _apply_chroma_keyer（chroma_keyer 节点 / keylight 兜底）
  - _apply_keylight（keyer 节点默认 chroma 路径，Keylight 1.2 风格）
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.nodes.base_node import TextureResult

pytestmark = pytest.mark.composite_core

_W, _H = 64, 48


def _green_white_image():
    """左半纯绿幕布 (0,1,0)，右半白前景 (1,1,1)。"""
    img = np.zeros((_H, _W, 4), dtype=np.float32)
    img[..., 3] = 1.0
    img[:, :_W // 2] = (0.0, 1.0, 0.0, 1.0)
    img[:, _W // 2:] = (1.0, 1.0, 1.0, 1.0)
    return img


class TestGLChromaKeyerOrientation:
    @pytest.fixture()
    def harness(self):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        from src.features.composite.evaluators.keyer_evaluator import (
            KeyerEvaluatorMixin,
        )

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = self.ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

        class _H(BaseEvaluatorMixin, KeyerEvaluatorMixin, ColorEvaluatorMixin,
                 FilterEvaluatorMixin):
            def __init__(self, ctx):
                self.ctx = ctx
                self.utility_shaders = {}
                self._resource_pool = _Pool(ctx)

                from src.shared.gpu.gl_backend import FramebufferManager
                self.fb_manager = FramebufferManager(ctx)
                self._load_shaders()

        yield _H(ctx)
        ctx.release()

    def _upload(self, harness, rgba):
        h, w = rgba.shape[:2]
        tex = harness._resource_pool.acquire_texture(w, h)
        tex.write(np.ascontiguousarray(np.flipud(rgba)).tobytes())
        return TextureResult(
            texture_id=tex.glo, width=w, height=h,
            bbox=BBox.full_frame(w, h),
            alpha_premultiplied=False, color_space="linear")

    def _read(self, harness, tex_result):
        tex = harness._get_texture_by_id(tex_result.texture_id)
        fbo = harness.fb_manager.get_fbo(tex)
        fbo.use()
        harness.ctx.viewport = (0, 0, tex_result.width, tex_result.height)
        raw = fbo.read(components=4, dtype="f4")
        arr = np.frombuffer(raw, dtype=np.float32).reshape(
            tex_result.height, tex_result.width, 4)
        return np.flipud(arr).copy()

    # ── legacy chroma_keyer（chroma_keyer 节点路径）────────────────────

    def test_chroma_matte_keeps_foreground(self, harness, qapp):
        harness
        src = self._upload(harness, _green_white_image())
        out = harness._apply_chroma_keyer(
            src, {"screen_color_r": 0.0, "screen_color_g": 1.0,
                  "screen_color_b": 0.0, "tolerance": 0.15,
                  "softness": 0.05, "despill": False,
                  "output_mode": "matte"}, _W, _H)
        got = self._read(harness, out["result"])
        # matte 模式把 matte 值写进 RGB，alpha 恒为 1。
        assert np.allclose(got[:, :_W // 2, 0], 0.0, atol=1e-4)
        assert np.allclose(got[:, _W // 2:, 0], 1.0, atol=1e-4)

    def test_chroma_result_premultiplies_screen_away(self, harness, qapp):
        harness
        src = self._upload(harness, _green_white_image())
        out = harness._apply_chroma_keyer(
            src, {"screen_color_r": 0.0, "screen_color_g": 1.0,
                  "screen_color_b": 0.0, "tolerance": 0.15,
                  "softness": 0.05, "despill": False,
                  "output_mode": "result"}, _W, _H)
        got = self._read(harness, out["result"])
        # 幕布预乘为黑；前景保持白色不透明。
        assert np.allclose(got[:, :_W // 2], 0.0, atol=1e-4)
        assert np.allclose(got[:, _W // 2:, :3], 1.0, atol=1e-4)
        assert np.allclose(got[:, _W // 2:, 3], 1.0, atol=1e-4)

    # ── keylight（keyer 节点默认 chroma 路径）─────────────────────────

    def test_keylight_matte_orientation(self, harness, qapp):
        harness
        src = self._upload(harness, _green_white_image())
        out = harness._apply_keylight(
            src, {"screen_color": (0.0, 1.0, 0.0),
                  "screen_balance": 0.5,
                  # clip 黑白点收紧，让 d 的斜坡饱和到 0/1：
                  # 幕布 d≈0 → (0-0.1)/0.4 → 0；白前景 d≈0.53 → >1 → 1。
                  "clip_black": 0.1, "clip_white": 0.5,
                  "gain": 1.0, "softness": 0.0, "alpha_offset": 0.0,
                  "despill_strength": 0.0, "despill_method": 0,
                  "invert": False, "output_mode": "matte"}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :_W // 2, 0], 0.0, atol=1e-4)
        assert np.allclose(got[:, _W // 2:, 0], 1.0, atol=1e-4)

    def test_keylight_result_premultiplies_screen_away(self, harness, qapp):
        harness
        src = self._upload(harness, _green_white_image())
        out = harness._apply_keylight(
            src, {"screen_color": (0.0, 1.0, 0.0),
                  "screen_balance": 0.5,
                  "clip_black": 0.1, "clip_white": 0.5,
                  "gain": 1.0, "softness": 0.0, "alpha_offset": 0.0,
                  "despill_strength": 0.0, "despill_method": 0,
                  "invert": False, "output_mode": "result"}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :_W // 2, :3], 0.0, atol=1e-4)
        assert np.allclose(got[:, _W // 2:, :3], 1.0, atol=1e-4)

    def test_keylight_default_screen_color_is_green(self, harness, qapp):
        """u_screen_color 的求值器缺省是绿幕 (0,1,0)。"""
        harness
        src = self._upload(harness, _green_white_image())
        out = harness._apply_keylight(
            src, {"clip_black": 0.1, "clip_white": 0.5,
                  "despill_strength": 0.0, "despill_method": 0,
                  "output_mode": "matte"}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :_W // 2, 0], 0.0, atol=1e-4)
