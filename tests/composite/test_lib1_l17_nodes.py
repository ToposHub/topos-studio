"""L17 批1 节点（C11 polar_coordinates / C14 tile_array）生产集成测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.core.node_capabilities import _NODE_CAPABILITY_TABLE
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.core.shaders import (
    _LIB1_POLAR_FRAGMENT,
    _LIB1_TILE_ARRAY_FRAGMENT,
)
from src.features.composite.evaluators.shader_library import ShaderLibrary

pytestmark = pytest.mark.composite

_VERT = """
#version 330
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

_QUAD = np.array([-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1],
                 dtype="f4")


class TestChainWiring:
    def test_registry_capability_library(self) -> None:
        for node_type in ("lib1_polar_coordinates", "lib1_tile_array"):
            assert node_type in NODE_REGISTRY
            assert _NODE_CAPABILITY_TABLE[node_type].effect_type == node_type
            assert node_type in ShaderLibrary.STANDARD_NAMES


class TestRealGl:
    @pytest.fixture()
    def ctx(self):
        try:
            context = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        yield context
        context.release()

    def _render(self, ctx, fragment: str, image: np.ndarray,
                uniforms: dict) -> np.ndarray:
        # 生产约定：上传前 flipud（GL-natural），读回 flipud 回 top-left
        # CPU 行序（初版镜像存储曾掩盖 y 向口径缺陷——C11/C14 的 y 相关
        # 变换在镜像共轭下不抵消，故必须按生产约定对拍）
        program = ctx.program(vertex_shader=_VERT, fragment_shader=fragment)
        h, w = image.shape[0], image.shape[1]
        t = ctx.texture((w, h), 4,
                        np.ascontiguousarray(np.flipud(image)).tobytes(),
                        dtype="f4")
        t.repeat_x = False
        t.repeat_y = False
        t.use(0)
        program["u_texture"].value = 0
        for key, value in uniforms.items():
            if key in program:
                program[key].value = value
        target = ctx.texture((w, h), 4, dtype="f4")
        fbo = ctx.framebuffer(color_attachments=[target])
        vbo = ctx.buffer(_QUAD.tobytes())
        vao = ctx.vertex_array(program, vbo, "in_vert", "in_uv")
        fbo.use()
        vao.render(moderngl.TRIANGLE_STRIP)
        raw = fbo.read(components=4, dtype="f4")
        vao.release(); vbo.release(); target.release(); fbo.release()
        t.release()
        program.release()
        arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
        return np.flipud(arr).copy()

    @staticmethod
    def _rgba(img: np.ndarray) -> np.ndarray:
        out = np.zeros((img.shape[0], img.shape[1], 4), dtype=np.float32)
        out[..., :3] = img
        out[..., 3] = 1.0
        return out

    def test_fragments_compile(self, ctx) -> None:
        for frag in (_LIB1_POLAR_FRAGMENT, _LIB1_TILE_ARRAY_FRAGMENT):
            prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
            prog.release()

    @staticmethod
    def _assert_nearest_ties_ok(got: np.ndarray, expected: np.ndarray,
                                img: np.ndarray, src_yi: np.ndarray,
                                src_xi: np.ndarray, max_frac: float) -> None:
        """Nearest 对拍断言：精确相等为主 + 刀锋平局豁免。

        fp32（GPU）vs f64（oracle）在 floor(pos+0.5) 的半整数边界归属
        平台相关（ref 文档已声明）——失配像素必须 ≤max_frac 且其值
        等于期望源索引 ±1 邻域内某纹素（钉住语义，不容忍任意漂移）。
        """
        diff = np.abs(got - expected).max(axis=-1)
        bad = diff > 1e-5
        assert bad.mean() <= max_frac, \
            f"失配像素 {int(bad.sum())}/{bad.size} 超上限（{max_frac:.0%}）"
        h, w = img.shape[:2]
        for y, x in np.argwhere(bad):
            y0, y1 = max(0, src_yi[y, x] - 1), min(h, src_yi[y, x] + 2)
            x0, x1 = max(0, src_xi[y, x] - 1), min(w, src_xi[y, x] + 2)
            patch = np.asarray(img[y0:y1, x0:x1]).reshape(-1, img.shape[-1])
            assert any(np.allclose(patch[i], got[y, x], atol=1e-6)
                       for i in range(len(patch))), \
                f"({y},{x}) got 非期望源 ±1 邻域纹素（非刀锋平局，语义失配）"

    def test_c11_polar_parity(self, ctx) -> None:
        from content_staging.library_v1.references import polar_coordinates_ref
        rng = np.random.RandomState(71)
        image = rng.rand(12, 16, 3).astype(np.float32)
        h, w = image.shape[:2]
        yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                             np.arange(w, dtype=np.float64), indexing="ij")
        cx64, cy64 = 0.5 * (w - 1), 0.5 * (h - 1)
        span64 = np.deg2rad(360.0)
        r_max64 = 0.5 * min(w, h)
        for mode, u_mode in (("CartesianToPolar", 0), ("PolarToCartesian", 1)):
            uniforms = {"u_mode": u_mode, "u_center": (0.5, 0.5),
                        "u_span": 360.0, "u_radius_scale": 1.0, "u_filter": 0}
            rendered = self._render(ctx, _LIB1_POLAR_FRAGMENT,
                                    self._rgba(image), uniforms)
            expected = polar_coordinates_ref.evaluate(
                image, {"mode": mode, "center": (0.5, 0.5),
                        "angle_span": 360.0, "radius_scale": 1.0,
                        "filter": "Nearest"})
            if mode == "CartesianToPolar":
                theta = np.arctan2(yy - cy64, xx - cx64)
                u = 0.5 + theta / span64
                v = np.hypot(xx - cx64, yy - cy64) / r_max64
                sx64, sy64 = u * (w - 1), v * (h - 1)
            else:
                theta = span64 * (xx / (w - 1) - 0.5)
                rad = yy / (h - 1) * r_max64
                sx64 = cx64 + rad * np.cos(theta)
                sy64 = cy64 + rad * np.sin(theta)
            src_xi = np.floor(sx64 + 0.5).astype(np.int64)
            src_yi = np.floor(sy64 + 0.5).astype(np.int64)
            self._assert_nearest_ties_ok(rendered[..., :3], expected, image,
                                         src_yi, src_xi, max_frac=0.05)

    def test_c14_tile_array_parity(self, ctx) -> None:
        from content_staging.library_v1.references import tile_array_ref
        rng = np.random.RandomState(74)
        image = rng.rand(12, 16, 4).astype(np.float32)
        # offset=0.2：避开 u_s·W 恰落整数的刀锋（fp32 vs f64 在
        # floor 整数边界归属平台相关——残余平局由 _assert_nearest_ties_ok
        # 按源邻域钉住，fixture 不依赖舍入运气）
        uniforms = {"u_count_x": 3.0, "u_count_y": 2.0, "u_spacing": 0.15,
                    "u_offset": (0.2, 0.0)}
        rendered = self._render(ctx, _LIB1_TILE_ARRAY_FRAGMENT,
                                image, uniforms)
        expected = tile_array_ref.evaluate(
            image, {"count_x": 3, "count_y": 2, "spacing": 0.15,
                    "offset_x": 0.2, "offset_y": 0.0})
        h, w = image.shape[:2]
        tw, th = w / 3, h / 2
        cw, ch = tw * 0.85, th * 0.85
        yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                             np.arange(w, dtype=np.float64), indexing="ij")
        i = np.clip(np.floor(xx / tw).astype(np.int64), 0, 2)
        j = np.clip(np.floor(yy / th).astype(np.int64), 0, 1)
        u_c = (xx - i * tw - tw * 0.15 / 2.0) / cw
        v_c = (yy - j * th - th * 0.15 / 2.0) / ch
        u_s = u_c + 0.2 - np.floor(u_c + 0.2)
        v_s = v_c + 0.0 - np.floor(v_c + 0.0)
        src_xi = np.clip(np.floor(u_s * w).astype(np.int64), 0, w - 1)
        src_yi = np.clip(np.floor(v_s * h).astype(np.int64), 0, h - 1)
        # v_c=0.5 恰落刀锋行（2/12 行）→ 平局像素可达 ~12%；邻域校验
        # 钉住语义，上限仅约束规模
        self._assert_nearest_ties_ok(rendered, expected, image,
                                     src_yi, src_xi, max_frac=0.15)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
