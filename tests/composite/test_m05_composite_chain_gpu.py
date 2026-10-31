"""M05 · Composite 链段消费（GPU 顺序 ping-pong pass）回归。

两层验证：

1. **生产侧**（``chain_for_stack``）：按主栈 id 经工程 clip 定位解析
   pre/trim/post 链段；无归属返回空（不静默应用错链）；
2. **GPU 顺序+数学**（standalone GL 上下文）：M05.1 验收向量
   contrast35 × exposure20（非交换）——链段按 pre→主→post 顺序经
   独立 scratch 纹理 ping-pong 落到 out_tex，输出与 CPU 权威
   ``GradingMath`` 顺序参考一致（M05.2f 起 composite topos_color
   shader 与 Edit 预览/导出共用 PRIMARY_GRADE_BODY，五路一致）、
   与错序显著分离；高级标记段跳过、无链段单 pass 不变。
"""

import os
import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytestmark = pytest.mark.composite_core

from src.features.color.core.the_decompiler import GradingMath
from src.features.edit.core.color_chain import chain_for_stack
from src.features.edit.core.color_group import ColorGroupData, ColorGroupMember
from src.shared.project.color_stack import ColorStackData

_W, _H = 32, 32


def _cpu_seq(value: float, segments) -> float:
    """CPU 权威参考：GradingMath 逐段顺序应用于均匀灰像素。"""
    rgb = np.array([[value, value, value]], dtype=np.float32)
    for seg in segments:
        rgb = GradingMath.apply_grading(rgb, seg)
    return float(rgb[0][0])


def _stack(stack_id, **params):
    return ColorStackData(
        stack_id=stack_id, asset_id="", name=stack_id,
        grading_params=dict(params))


def _project(pre_params=None, post_params=None, main_params=None,
             trim_params=None):
    """clip(c1@tl1) → 主栈 s-main + 组 g1(pre/post 栈)。

    timelines 携带完整序列（chain_for_stack 经 _iter_clips 定位 clip）。
    """
    stacks = {"s-main": _stack("s-main", **(main_params or {}))}
    group_kwargs = {}
    if pre_params is not None:
        stacks["s-pre"] = _stack("s-pre", **pre_params)
        group_kwargs["pre_group_stack_id"] = "s-pre"
    if post_params is not None:
        stacks["s-post"] = _stack("s-post", **post_params)
        group_kwargs["post_group_stack_id"] = "s-post"
    if trim_params is not None:
        stacks["s-trim"] = _stack("s-trim", **trim_params)
    group = ColorGroupData(
        group_id="g1", name="G",
        members=(ColorGroupMember(timeline_id="tl1", clip_id="c1"),),
        **group_kwargs)
    clip = NS(clip_id="c1", color_stack_id="s-main",
              color_trim_stack_id=("s-trim" if trim_params is not None
                                   else None))
    track = NS(clips=(clip,))
    return NS(
        color_stacks=stacks, color_groups={"g1": group},
        timelines={"tl1": NS(timeline_id="tl1", video_tracks=(track,))})


# ================================================================
# 生产侧：chain_for_stack
# ================================================================

class TestChainForStackProducer:
    def test_resolves_segments_via_owning_clip(self):
        project = _project(pre_params={"contrast": 35.0},
                           post_params={"exposure": 20.0})
        extras = chain_for_stack(project, "s-main")
        assert [e["_chain_segment"] for e in extras] == ["pre", "post"]
        assert extras[0]["contrast"] == pytest.approx(35.0)
        assert extras[1]["exposure"] == pytest.approx(20.0)

    def test_trim_segment_included(self):
        project = _project(trim_params={"shadow": -10.0})
        extras = chain_for_stack(project, "s-main")
        assert [e["_chain_segment"] for e in extras] == ["trim"]

    def test_no_owner_or_empty_returns_empty(self):
        """主栈不被任何 clip 引用 / 空 id / 无工程 → 空列表。"""
        project = _project(pre_params={"contrast": 35.0})
        assert chain_for_stack(project, "s-unknown") == []
        assert chain_for_stack(project, "") == []
        assert chain_for_stack(None, "s-main") == []


# ================================================================
# GPU 顺序 ping-pong（standalone GL）
# ================================================================

class TestCompositeChainGpuOrder:
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

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = self.ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

            def release_texture(self, tex, force_evict=False):
                try:
                    tex.release()
                    self._pooled_textures.get("f4", []).discard(tex)
                except Exception:
                    pass

        class _H(BaseEvaluatorMixin, ColorEvaluatorMixin):
            def __init__(self, ctx):
                self.ctx = ctx
                self.utility_shaders = {}
                self._resource_pool = _Pool(ctx)
                from src.shared.gpu.gl_backend import FramebufferManager
                self.fb_manager = FramebufferManager(ctx)
                self._load_shaders()

        yield _H(ctx)
        ctx.release()

    @staticmethod
    def _gray_input(harness, value=0.4):
        from src.features.composite.core.format import BBox
        from src.features.composite.nodes.base_node import TextureResult
        rgba = np.full((_H, _W, 4), value, dtype=np.float32)
        rgba[..., 3] = 1.0
        tex = harness._resource_pool.acquire_texture(_W, _H)
        tex.write(np.ascontiguousarray(np.flipud(rgba)).tobytes())
        return TextureResult(
            texture_id=tex.glo, width=_W, height=_H,
            bbox=BBox.full_frame(_W, _H),
            alpha_premultiplied=False, color_space="linear")

    @staticmethod
    def _read_center(harness, tex_result):
        tex = harness._get_texture_by_id(tex_result.texture_id)
        fbo = harness.fb_manager.get_fbo(tex)
        fbo.use()
        harness.ctx.viewport = (0, 0, tex_result.width, tex_result.height)
        raw = fbo.read(components=4, dtype="f4")
        arr = np.frombuffer(raw, dtype=np.float32).reshape(
            tex_result.height, tex_result.width, 4)
        return float(np.flipud(arr)[_H // 2, _W // 2, 0])

    def _apply(self, harness, project, main_params=None):
        harness._project = project
        input_tex = self._gray_input(harness)
        return harness._apply_topos_color(
            input_tex,
            {"stack_source": "clip_stack", "color_stack_id": "s-main",
             "intensity": 1.0},
            _W, _H)

    def test_pre_contrast_then_main_exposure_gpu(self, harness):
        """pre(contrast 仿射) → 主(exposure 乘性)：非交换——GPU 输出
        与顺序参考一致，与错序显著分离。"""
        main_params = {"exposure": 20.0}
        project = _project(pre_params={"contrast": 35.0},
                           main_params=main_params)
        result = self._apply(harness, project, main_params)
        gpu = self._read_center(harness, result["result"])
        seq_ok = _cpu_seq(0.4, [{"contrast": 35.0}, main_params])
        seq_bad = _cpu_seq(0.4, [main_params, {"contrast": 35.0}])
        # 顺序正确：与 pre→主 参考一致（GPU float 精度容差）
        assert gpu == pytest.approx(seq_ok, abs=2e-3), (gpu, seq_ok)
        # 非交换分离：错序参考显著不同（顺序错即像素错）
        assert abs(gpu - seq_bad) > 0.01, (gpu, seq_bad)

    def test_post_segment_applied_after_main_gpu(self, harness):
        """主(exposure) → post(contrast)：post 段在主 pass 之后。"""
        main_params = {"exposure": 20.0}
        project = _project(main_params=main_params,
                           post_params={"contrast": 35.0})
        result = self._apply(harness, project, main_params)
        gpu = self._read_center(harness, result["result"])
        seq_ok = _cpu_seq(0.4, [main_params, {"contrast": 35.0}])
        seq_bad = _cpu_seq(0.4, [{"contrast": 35.0}, main_params])
        assert gpu == pytest.approx(seq_ok, abs=2e-3), (gpu, seq_ok)
        assert abs(gpu - seq_bad) > 0.01, (gpu, seq_bad)

    def test_full_chain_pre_main_post_gpu(self, harness):
        """pre→主→post 三段全链 ping-pong 落 out_tex。"""
        main_params = {"exposure": 20.0}
        project = _project(pre_params={"contrast": 35.0},
                           main_params=main_params,
                           post_params={"contrast": -20.0})
        result = self._apply(harness, project, main_params)
        gpu = self._read_center(harness, result["result"])
        seq_ok = _cpu_seq(0.4, [
            {"contrast": 35.0}, main_params, {"contrast": -20.0}])
        assert gpu == pytest.approx(seq_ok, abs=3e-3), (gpu, seq_ok)

    def test_no_chain_unchanged_single_pass(self, harness):
        """无链段（栈不在组内）→ 单主 pass 行为与既有路径一致。"""
        main_params = {"exposure": 20.0}
        project = _project(main_params=main_params)
        result = self._apply(harness, project, main_params)
        gpu = self._read_center(harness, result["result"])
        expected = _cpu_seq(0.4, [main_params])
        assert gpu == pytest.approx(expected, abs=2e-3), (gpu, expected)

    def test_unsupported_advanced_segment_skipped_gpu(self, harness):
        """链段含高级操作（LUT）→ 该段跳过，不进 GPU pass。"""
        main_params = {"exposure": 20.0}
        project = _project(pre_params={"contrast": 35.0},
                           main_params=main_params)
        # 预组栈带 LUT → 高级标记段
        pre_stack = project.color_stacks["s-pre"]
        from dataclasses import replace as _dc_replace
        project.color_stacks["s-pre"] = _dc_replace(
            pre_stack, lut_path="/gone/x.cube")
        result = self._apply(harness, project, main_params)
        gpu = self._read_center(harness, result["result"])
        # pre 段被跳过：只剩主 exposure（不同于完整链）
        main_only = _cpu_seq(0.4, [main_params])
        full_chain = _cpu_seq(0.4, [{"contrast": 35.0}, main_params])
        assert gpu == pytest.approx(main_only, abs=2e-3), (gpu, main_only)
        assert abs(gpu - full_chain) > 0.01, (gpu, full_chain)
