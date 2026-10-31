"""L17 批2 全帧 CPU 子门（C18 distance_matte / C19 fill_holes）生产集成测试。

覆盖：registry/capability（全帧声明 fusible=False）/dispatch 分支 +
真实 GL 回读→CPU 计算→上传链像素对拍 oracle（最小 evaluator facade，
同 test_paint_node 的 _PaintHarness 模式）。"""

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
from src.features.composite.evaluators.shader_library import ShaderLibrary

pytestmark = pytest.mark.composite

_L17B2_TYPES = ("lib1_distance_matte", "lib1_fill_holes")


class TestChainWiring:
    def test_registry_capability_fullframe_declaration(self) -> None:
        for node_type in _L17B2_TYPES:
            assert node_type in NODE_REGISTRY
            cap = _NODE_CAPABILITY_TABLE[node_type]
            assert cap.effect_type == node_type
            assert cap.roi_policy == "full_frame"   # 全帧声明子门
            assert cap.fusible is False             # CPU 全帧计算不融合

    def test_dispatch_branch_exists(self) -> None:
        import inspect
        from src.features.composite.evaluators import base_evaluator
        src = inspect.getsource(base_evaluator)
        for node_type in _L17B2_TYPES:
            assert f'"{node_type}"' in src
        assert "_apply_lib1_fullframe" in src
        # 红线：未知 lib1_* 不透传
        assert 'effect_type.startswith("lib1_")' in src

    def test_shader_library_not_required(self) -> None:
        # 全帧 CPU 通道不走 utility shader（与 GPU 家族口径差异）
        for node_type in _L17B2_TYPES:
            assert node_type not in ShaderLibrary.STANDARD_NAMES


class _Pool:
    def __init__(self, ctx):
        self.ctx = ctx
        self._pooled_textures = {}

    def acquire_texture(self, w, h, components=4, dtype="f4"):
        tex = self.ctx.texture((w, h), components, dtype=dtype)
        self._pooled_textures.setdefault(dtype, []).append(tex)
        return tex


class _FullframeHarness:
    """最小 evaluator facade（同 test_paint_node 模式）。"""

    def __init__(self, ctx):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        from src.shared.gpu.gl_backend import FramebufferManager

        # 生产合成类是多 Mixin 组合（回读/上传在 Base、_apply 在 Filter）；
        # 测试用同构组合类还原真实 MRO
        class _Combined(BaseEvaluatorMixin, FilterEvaluatorMixin):
            pass

        self.ctx = ctx
        self.fb_manager = FramebufferManager(ctx)
        self._resource_pool = _Pool(ctx)
        self.mixin = _Combined()
        # Mixin MRO 借用：把 facade 的属性映射到 mixin 视角
        self.mixin.ctx = ctx
        self.mixin.fb_manager = self.fb_manager
        self.mixin._resource_pool = self._resource_pool
        self._by_id = {}

        def _get_texture_by_id(texture_id):
            if texture_id in self._by_id:
                return self._by_id[texture_id]
            # 生产 _get_texture_by_id 扫描池化纹理（上传结果同源可解析）
            for tex_list in self._resource_pool._pooled_textures.values():
                for tex in tex_list:
                    if tex.glo == texture_id:
                        return tex
            return None

        self.mixin._get_texture_by_id = _get_texture_by_id

    def register(self, tex):
        self._by_id[tex.glo] = tex
        return tex


class TestRealGlFullframePath:
    """回读→CPU 计算→上传 链路像素对拍 oracle。"""

    @pytest.fixture()
    def ctx(self):
        try:
            context = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        yield context
        context.release()

    @staticmethod
    def _fixture(h, w):
        rng = np.random.RandomState(81)
        rgba = np.zeros((h, w, 4), dtype=np.float32)
        rgba[..., :3] = rng.rand(h, w, 3).astype(np.float32)
        rgba[..., 3] = (rng.rand(h, w) > 0.55).astype(np.float32)
        return rgba

    @pytest.mark.parametrize("node_type", _L17B2_TYPES)
    def test_fullframe_cpu_path_parity(self, ctx, node_type) -> None:
        from src.features.composite.evaluators.lib1_fullframe_compute import (
            distance_matte_evaluate,
            fill_holes_evaluate,
        )
        from src.features.composite.nodes.base_node import TextureResult
        h, w = 10, 14
        rgba = self._fixture(h, w)
        harness = _FullframeHarness(ctx)
        # 源纹理直传（RGBA，top-left → flipud 上传同生产约定）
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )
        data = np.ascontiguousarray(np.flipud(rgba))
        tex = harness.register(upload_pooled_texture(
            harness._resource_pool, data, w, h))
        input_tex = TextureResult(texture_id=tex.glo, width=w, height=h)
        params = {"threshold": 0.5}
        if node_type == "lib1_distance_matte":
            params.update({"metric": "Euclid", "radius": 3.0,
                           "output_mode": "DistanceField"})
        else:
            params.update({"connectivity": "4", "max_area": 1.0,
                           "output_mode": "Fill"})
        out = harness.mixin._apply_lib1_fullframe(
            node_type, input_tex, params, w, h)
        result = out["result"]
        assert result is not input_tex  # CPU 子门必须产出新纹理
        # _readback_texture_fullres 返回值已是 top-left origin（内部
        # flipud）——直接对拍，不得再翻（初版双重 flipud 即 xfail 根因）
        got = harness.mixin._readback_texture_fullres(result, want_alpha=True)
        if node_type == "lib1_distance_matte":
            expected = distance_matte_evaluate(rgba, params)
        else:
            expected = fill_holes_evaluate(rgba, params)
        np.testing.assert_allclose(got, expected, atol=1e-5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
