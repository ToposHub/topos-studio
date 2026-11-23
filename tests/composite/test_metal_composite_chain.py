"""阶段 5 Metal 合成链测试（5.1 最小 2D 链 / 5.2 部分 / 5.3 Tile 与缓存 /
5.6 有界异步发布）。

分层：

- 纯逻辑层（无 GPU，任何机器可跑）：ROI 断言、halo 传播、预检回退、
  CPU 参考语义（含计划中的 Alpha 最小语义例）、tile 无缝、缓存键。
- 真机层（无 Metal 设备自动 skip）：Metal kernel 与 CPU 参考逐像素对拍、
  分块 vs 整帧、handle 租约/完成语义、字节计量、异步发布（完成事件
  驱动，无 waitUntilCompleted；环满显式回退）。

容差依据：中间纹理为 RGBA16F（10 位尾数，相对误差 ≈ 2^-11 ≈ 4.9e-4），
值域 [0, 4] 内的绝对舍入上界约 2e-3；GPU 与 CPU 的双线性采样公式一致，
因此 atol=5e-3 覆盖舍入并保留裕量（无系统性偏差时实测 ≤ 2e-3）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.shared.gpu.composite_gpu_frame_handle import CompositeGpuFrameHandle

from src.features.composite.render.metal import roi
from src.features.composite.render.metal.cpu_reference import render_graph_cpu
from src.features.composite.render.metal.node_support import (
    METAL_COMPOSITE_SUPPORTED_TYPES,
    evaluate_metal_support,
)
from src.features.composite.render.metal.renderer import MetalCompositeRenderer
from src.features.composite.render.metal.texture_pool import (
    MetalPoolExhausted,
    MetalTexturePool,
    PooledTexture,
)
from src.features.composite.render.metal.tile_cache import (
    MetalTileCache,
    composite_cache_key,
    graph_params_digest,
)
from src.shared.gpu.metal import MetalComputeContext

GPU_TOL = 5e-3  # RGBA16F 舍入 + 双线性一致性的绝对容差（见模块 docstring）


# ── 图构造辅助 ───────────────────────────────────────────────────────────


def constant(nid: str, w: int, h: int, rgba: tuple, **kw) -> dict:
    params = {
        "width": w, "height": h, "mode": "solid",
        "color": rgba, "color2": (1.0, 1.0, 1.0, 1.0), "checker_size": 4,
    }
    params.update(kw)
    return {"id": nid, "type": "constant", "parameters": params}


def node(nid: str, ntype: str, **params) -> dict:
    return {"id": nid, "type": ntype, "parameters": dict(params)}


def conn(src: str, dst: str, src_port: str = "result",
         dst_port: str = "image") -> dict:
    return {"src_node_id": src, "src_port_name": src_port,
            "dst_node_id": dst, "dst_port_name": dst_port}


def graph(nodes: list, connections: list) -> dict:
    return {"nodes": nodes, "connections": connections, "graph_revision": 7}


def over_graph(alpha_policy: str = "premult_force", w: int = 8, h: int = 8) -> dict:
    """红(0.5,0,0,0.5) over 蓝(0,0,1,1) 的最小链（计划 Alpha 语义例）。"""
    return graph(
        [constant("red", w, h, (0.5, 0.0, 0.0, 0.5)),
         constant("blue", w, h, (0.0, 0.0, 1.0, 1.0)),
         node("m", "merge", blend_mode="over", alpha_policy=alpha_policy),
         node("o", "output")],
        [conn("red", "m", dst_port="A"), conn("blue", "m", dst_port="B"),
         conn("m", "o")])


def checker_transform_graph(w: int = 64, h: int = 48) -> dict:
    """棋盘 → 亚像素缩放平移 → over 纯色 → output（5.1 验收链）。"""
    return graph(
        [constant("fg", w, h, (1, 1, 1, 1), mode="checker",
                  color2=(0, 0, 0, 1), checker_size=8),
         node("t", "transform", translate_x=3.0, translate_y=-2.0,
              scale_x=1.37, scale_y=0.83, filter="linear"),
         constant("bg", w, h, (0.1, 0.2, 0.3, 1.0)),
         node("m", "merge", blend_mode="over", opacity=0.9),
         node("o", "output")],
        [conn("fg", "t"), conn("t", "m", dst_port="A"),
         conn("bg", "m", dst_port="B"), conn("m", "o")])


# ── 5.3 · ROI（计划参考实现 5A 的两个断言用例） ──────────────────────────


class TestInputRoi:
    def test_plan_assert_case_1(self):
        assert roi.input_roi((16, 32, 32, 48), 2, 64, 64) == (
            (14, 30, 34, 50), (2, 2, 18, 18))

    def test_plan_assert_case_2(self):
        assert roi.input_roi((0, 0, 16, 16), 2, 64, 64) == (
            (0, 0, 18, 18), (0, 0, 16, 16))

    @pytest.mark.parametrize("tile,radius", [
        ((16, 32, 32, 48), -1),
        ((-1, 0, 8, 8), 0),
        ((0, 0, 65, 8), 0),
        ((8, 8, 8, 8), 0),   # 非半开区间
    ])
    def test_invalid_tile_raises(self, tile, radius):
        with pytest.raises(ValueError):
            roi.input_roi(tile, radius, 64, 64)

    def test_crop_back_writes_tile_center(self):
        expanded, crop = roi.input_roi((10, 10, 20, 20), 3, 64, 64)
        # 裁剪矩形平移回扩张原点后应等于原 tile。
        assert (crop[0] + expanded[0], crop[1] + expanded[1],
                crop[2] + expanded[0], crop[3] + expanded[1]) == (10, 10, 20, 20)

    def test_iter_tile_rects_covers_canvas(self):
        tiles = list(roi.iter_tile_rects(45, 37, 17, 19))
        area = sum((t[2] - t[0]) * (t[3] - t[1]) for t in tiles)
        assert area == 45 * 37
        assert all(t[2] <= 45 and t[3] <= 37 for t in tiles)


class TestHaloPropagation:
    def test_transform_filter_margin_accumulates(self):
        nodes = [
            constant("c", 64, 64, (1, 1, 1, 1)),
            node("t1", "transform", filter="linear"),
            node("t2", "transform", filter="linear"),
            node("o", "output"),
        ]
        conns = [conn("c", "t1"), conn("t1", "t2"), conn("t2", "o")]
        result = roi.propagate_input_halos(nodes, conns, ["o"])
        assert result.node_input_halo["t1"] == 2  # 自身 1 + 下游 t2 的 1
        assert result.node_input_halo["t2"] == 1
        assert result.node_input_halo["o"] == 0

    def test_nearest_filter_has_no_halo(self):
        nodes = [constant("c", 64, 64, (1, 1, 1, 1)),
                 node("t", "transform", filter="nearest"), node("o", "output")]
        result = roi.propagate_input_halos(nodes, [conn("c", "t"), conn("t", "o")],
                                           ["o"])
        assert result.node_input_halo["t"] == 0

    def test_multi_consumer_takes_max(self):
        nodes = [
            constant("c", 64, 64, (1, 1, 1, 1)),
            node("t1", "transform", filter="linear"),
            node("t2", "transform", filter="linear"),
            node("t3", "transform", filter="linear"),
            node("o", "output"),
        ]
        conns = [conn("c", "t1"), conn("t1", "t2"), conn("t1", "t3"),
                 conn("t2", "o"), conn("t3", "o")]
        result = roi.propagate_input_halos(nodes, conns, ["o"])
        # t1 有两个消费者，各需 1，取 max（不叠加）。
        assert result.node_input_halo["t1"] == 2
        assert result.output_request_halo["t1"] == 1

    def test_unmigrated_blur_radius_declared(self):
        nodes = [constant("c", 64, 64, (1, 1, 1, 1)),
                 node("b", "blur", radius=5.2), node("o", "output")]
        result = roi.propagate_input_halos(nodes, [conn("c", "b"), conn("b", "o")],
                                           ["o"])
        assert result.node_input_halo["b"] == 6  # ceil(5.2)


# ── 5.1/5.2 · 预检与整图回退 ─────────────────────────────────────────────


class TestMetalPreflight:
    def test_minimal_chain_supported(self):
        report = evaluate_metal_support(over_graph())
        assert report.supported, report.fallback_summary

    def test_unmigrated_node_falls_back_whole_graph(self):
        g = graph(
            [constant("c", 8, 8, (1, 1, 1, 1)),
             node("b", "blur", radius=4.0),
             node("o", "output")],
            [conn("c", "b"), conn("b", "o")])
        report = evaluate_metal_support(g)
        assert not report.supported
        issue = next(i for i in report.issues if i.code == "node.metal_unmigrated")
        assert issue.node_id == "b"
        assert issue.node_type == "blur"
        assert "b" in report.fallback_summary and "blur" in report.fallback_summary

    def test_unconnected_unmigrated_branch_does_not_block(self):
        g = graph(
            [constant("c", 8, 8, (1, 1, 1, 1)),
             node("o", "output"),
             node("unused", "keyer")],
            [conn("c", "o")])
        report = evaluate_metal_support(g)
        assert report.supported

    def test_mask_ellipse_is_param_blocked(self):
        g = graph(
            [node("mk", "mask", mask_type="ellipse"), node("o", "output")],
            [conn("mk", "o")])
        report = evaluate_metal_support(g)
        assert not report.supported
        assert any(i.code == "node.metal_param_unsupported" and
                   i.node_type == "mask" for i in report.issues)

    def test_motion_blur_is_param_blocked(self):
        g = graph(
            [constant("c", 8, 8, (1, 1, 1, 1)),
             node("t", "transform", motion_blur=True), node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        report = evaluate_metal_support(g)
        assert not report.supported

    def test_structural_cycle_blocks(self):
        g = graph(
            [constant("c", 8, 8, (1, 1, 1, 1)),
             node("t1", "transform"), node("t2", "transform"),
             node("o", "output")],
            [conn("c", "t1"), conn("t1", "t2"), conn("t2", "t1"),
             conn("t2", "o")])
        report = evaluate_metal_support(g)
        assert not report.supported
        assert any("结构预检错误" in i.reason for i in report.issues)

    def test_supported_type_registry(self):
        # 已迁移登记（5.1 + 5.2 部分）。
        assert {"constant", "transform", "merge", "output", "crop",
                "premult", "unpremult", "mask", "noop", "dot"} <= \
            METAL_COMPOSITE_SUPPORTED_TYPES
        # 本次不做的节点绝不在登记表里。
        for blocked in ("keyer", "matte_cleanup", "blur", "erode_dilate",
                        "glow", "light_wrap", "text", "image_input",
                        "layer_style", "precomp", "scene3d"):
            assert blocked not in METAL_COMPOSITE_SUPPORTED_TYPES


# ── 5.1/5.2 · CPU 参考语义（无 GPU 机器可跑） ────────────────────────────


class TestCpuReferenceSemantics:
    def test_alpha_semantics_example_from_plan(self):
        # 线性、预乘：半透明红 (0.5,0,0,0.5) over 不透明蓝 (0,0,1,1)
        # → (0.5, 0, 0.5, 1)。
        frame = render_graph_cpu(over_graph("premult_force"))
        np.testing.assert_allclose(frame[3, 3], (0.5, 0.0, 0.5, 1.0), atol=1e-6)

    def test_straight_inputs_premultiply_on_the_fly(self):
        # auto 策略：Constant 是直 alpha，先预乘再 over：红贡献 0.5*0.5。
        frame = render_graph_cpu(over_graph("auto"))
        np.testing.assert_allclose(frame[3, 3], (0.25, 0.0, 0.5, 1.0), atol=1e-6)

    def test_three_alpha_values_transparent_base(self):
        for alpha in (0.0, 0.5, 1.0):
            g = graph(
                [constant("c", 4, 4, (1.0, 0.0, 0.0, alpha)),
                 node("o", "output")],
                [conn("c", "o")])
            frame = render_graph_cpu(g)
            np.testing.assert_allclose(frame[1, 1, 3], alpha, atol=1e-6)
        # alpha=0 的直 alpha 源：RGB 原样保留（彩色透明像素是合法直 alpha
        # 数据；GL 路径同样不做清除）。
        g0 = graph([constant("c", 4, 4, (1.0, 0.0, 0.0, 0.0)),
                    node("o", "output")], [conn("c", "o")])
        frame0 = render_graph_cpu(g0)
        np.testing.assert_allclose(frame0[1, 1], (1, 0, 0, 0), atol=1e-6)

    def test_premult_zero_alpha_clears_rgb(self):
        # 预乘语义：alpha=0 时 unpremult/premult 链把 RGB 归零（GLSL
        # _UNPREMULT_FRAGMENT 的 1e-5 守卫）。
        g = graph(
            [constant("c", 4, 4, (1.0, 0.0, 0.0, 0.0)),
             node("p", "premult"), node("o", "output",
                                        alpha_policy="premult")],
            [conn("c", "p"), conn("p", "o")])
        frame = render_graph_cpu(g)
        np.testing.assert_allclose(frame[1, 1], (0, 0, 0, 0), atol=1e-6)

    def test_premult_unpremult_roundtrip_and_alpha_zero(self):
        g = graph(
            [constant("c", 4, 4, (0.8, 0.6, 0.4, 0.5)),
             node("p", "premult"), node("u", "unpremult"),
             node("o", "output")],
            [conn("c", "p"), conn("p", "u"), conn("u", "o")])
        frame = render_graph_cpu(g)
        np.testing.assert_allclose(frame[1, 1], (0.8, 0.6, 0.4, 0.5), atol=1e-5)

    def test_transform_identity_is_passthrough(self):
        # 恒等 Transform 走计划级透传：与直连结果逐像素一致。
        nodes = [constant("c", 8, 6, (0.3, 0.6, 0.9, 1.0)),
                 node("t", "transform"),
                 node("o", "output")]
        g_passthrough = graph(nodes, [conn("c", "t"), conn("t", "o")])
        g_direct = graph([nodes[0], node("o", "output")], [conn("c", "o")])
        np.testing.assert_array_equal(render_graph_cpu(g_passthrough),
                                      render_graph_cpu(g_direct))

    def test_transform_integer_translation(self):
        g = graph(
            [constant("c", 8, 8, (1.0, 0.0, 0.0, 1.0)),
             node("t", "transform", translate_x=2.0, translate_y=3.0,
                  filter="nearest"),
             node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        frame = render_graph_cpu(g)
        assert frame[3, 1, 3] == 0.0   # 左侧 2 列让出 → 透明
        assert frame[2, 4, 3] == 0.0   # 顶部 3 行让出 → 透明
        assert frame[4, 3, 3] == 1.0   # 内容右移 2、下移 3
        assert frame[6, 6, 3] == 1.0

    def test_transform_flip_180(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1), mode="checker",
                      color2=(0, 0, 0, 1), checker_size=1),
             node("t", "transform", rotation=180.0, filter="nearest"),
             node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        base = render_graph_cpu(graph(g["nodes"][:1] + [node("o", "output")],
                                      [conn("c", "o")]))
        flipped = render_graph_cpu(g)
        np.testing.assert_allclose(flipped, base[::-1, ::-1], atol=1e-6)

    def test_transform_subpixel_scale_is_smooth(self):
        # 亚像素缩放：相邻像素不应有硬跳变（存在中间值）。
        g = graph(
            [constant("c", 16, 16, (1, 1, 1, 1), mode="checker",
                      color2=(0, 0, 0, 1), checker_size=4),
             node("t", "transform", scale_x=1.3, filter="linear"),
             node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        frame = render_graph_cpu(g)
        row = frame[8, :, 0]
        assert np.any((row > 0.05) & (row < 0.95))

    def test_crop_region_extraction_and_outside(self):
        g = graph(
            [constant("c", 8, 8, (1.0, 0.0, 0.0, 1.0)),
             node("k", "crop", x=2, y=2, width=4, height=4, reformat=True),
             node("o", "output")],
            [conn("c", "k"), conn("k", "o")])
        frame = render_graph_cpu(g)
        assert frame.shape == (4, 4, 3 + 1)
        np.testing.assert_allclose(frame[0, 0], (1, 0, 0, 1), atol=1e-6)

    def test_crop_outside_black(self):
        g = graph(
            [constant("c", 4, 4, (1.0, 0.0, 0.0, 1.0)),
             node("k", "crop", x=2, y=2, width=8, height=8, reformat=True,
                  outside="black"),
             node("o", "output")],
            [conn("c", "k"), conn("k", "o")])
        frame = render_graph_cpu(g)
        assert frame.shape == (8, 8, 4)
        # 裁剪窗在源（Y-up 参数 y=2，源高 4）上映射到 top-down 行
        # -6..2：只有底部 2 行采到源内容（源 x 2..3），其余黑不透明。
        opaque_black = (frame[..., 3] == 1.0) & (frame[..., 0] == 0.0)
        assert opaque_black[:4, :].all()
        assert frame[6, 0, 3] == 1.0 and frame[6, 0, 0] == 1.0  # 内容

    def test_mask_rectangle_matte_and_merge_mask(self):
        g = graph(
            [constant("bg", 8, 8, (0.0, 0.0, 1.0, 1.0)),
             node("mk", "mask", mask_type="rectangle", center_x=0.5,
                  center_y=0.5, width=0.5, height=0.5, output_mode="matte"),
             node("m", "merge", blend_mode="over", mask_channel="alpha"),
             node("o", "output")],
            [conn("bg", "m", dst_port="B"),
             conn("mk", "m", src_port="matte", dst_port="mask"),
             conn("m", "o")])
        frame = render_graph_cpu(g)
        # 中心 50% 区域：mask=1 → 需要有 A…本图没有 A 输入，走 B 透传
        # 分支；构造 A 存在的版本在 GPU 对拍里覆盖。这里检查 mask 端口
        # 采样不崩溃且输出形状正确。
        assert frame.shape == (8, 8, 4)

    def test_mask_rectangle_matches_gl_shader(self):
        # 与真机 GL _MASK_FRAGMENT 对齐（本仓库 GLSL 注释写「1 inside」，
        # 但 step(0.0, d) + d<0 在形状内部 → 实际 alpha=0 在内、1 在外；
        # 已用 standalone GL 探针核实。Metal/CPU 复刻该实测语义）。
        g = graph(
            [node("mk", "mask", mask_type="rectangle", center_x=0.5,
                  center_y=0.5, width=0.5, height=0.5, output_mode="result",
                  output_width=8, output_height=8),
             node("o", "output")],
            [conn("mk", "o")])
        frame = render_graph_cpu(g)
        assert frame[1, 1, 0] == 1.0   # 形状外 → 1
        assert frame[4, 4, 0] == 0.0   # 形状内（中心 4×4）→ 0
        # invert 参数翻回通常直觉的「内 1 外 0」。
        g2 = graph(
            [node("mk", "mask", mask_type="rectangle", center_x=0.5,
                  center_y=0.5, width=0.5, height=0.5, invert=True,
                  output_mode="result", output_width=8, output_height=8),
             node("o", "output")],
            [conn("mk", "o")])
        frame2 = render_graph_cpu(g2)
        assert frame2[4, 4, 0] == 1.0
        assert frame2[1, 1, 0] == 0.0

    def test_output_alpha_policies(self):
        base = [constant("c", 4, 4, (0.8, 0.6, 0.4, 0.5))]
        for policy, expect_rgb, expect_a in (
            ("keep", (0.8, 0.6, 0.4), 0.5),
            ("premult", (0.4, 0.3, 0.2), 0.5),
            ("straight", (0.8, 0.6, 0.4), 0.5),
            ("discard", (0.8, 0.6, 0.4), 1.0),
        ):
            g = graph(base + [node("o", "output", alpha_policy=policy)],
                      [conn("c", "o")])
            frame = render_graph_cpu(g)
            np.testing.assert_allclose(
                frame[1, 1, :3], expect_rgb, atol=1e-6,
                err_msg=f"policy={policy}")
            np.testing.assert_allclose(frame[1, 1, 3], expect_a, atol=1e-6)

    def test_output_background_composite(self):
        g = graph(
            [constant("c", 4, 4, (0.5, 0.0, 0.0, 0.5)),
             node("o", "output", background_color="0,0,1,1")],
            [conn("c", "o")])
        frame = render_graph_cpu(g)
        # 直 alpha 输入 0.5 红 over 蓝底：(0.25, 0, 0.5, 1)。
        np.testing.assert_allclose(frame[1, 1], (0.25, 0.0, 0.5, 1.0), atol=1e-6)

    def test_different_size_merge_from_b_canvas(self):
        g = graph(
            [constant("small", 4, 4, (1.0, 0.0, 0.0, 1.0)),
             constant("large", 8, 6, (0.0, 0.0, 1.0, 1.0)),
             node("m", "merge", blend_mode="over"),
             node("o", "output")],
            [conn("small", "m", dst_port="A"),
             conn("large", "m", dst_port="B"), conn("m", "o")])
        frame = render_graph_cpu(g)
        assert frame.shape == (6, 8, 4)
        # 左上 4×4 是红 over 蓝；右下只剩蓝。
        np.testing.assert_allclose(frame[1, 1], (1, 0, 0, 1), atol=1e-6)
        np.testing.assert_allclose(frame[5, 7], (0, 0, 1, 1), atol=1e-6)

    def test_noop_passthrough(self):
        g = graph(
            [constant("c", 4, 4, (0.25, 0.5, 0.75, 1.0)),
             node("n", "noop"), node("o", "output")],
            [conn("c", "n"), conn("n", "o", src_port="image")])
        frame = render_graph_cpu(g)
        np.testing.assert_allclose(frame[1, 1], (0.25, 0.5, 0.75, 1.0),
                                   atol=1e-6)

    def test_unmigrated_graph_raises_for_cpu_reference(self):
        from src.features.composite.render.metal.graph_plan import PlanError
        g = graph([constant("c", 4, 4, (1, 1, 1, 1)),
                   node("b", "blur", radius=3.0), node("o", "output")],
                  [conn("c", "b"), conn("b", "o")])
        with pytest.raises(PlanError):
            render_graph_cpu(g)


# ── 5.3 · Tile 无缝（CPU）与缓存键 ───────────────────────────────────────


class TestTilingCpu:
    @pytest.mark.parametrize("tile_size", [(17, 19), (64, 64), (7, 5)])
    def test_tiled_matches_full_frame(self, tile_size):
        g = checker_transform_graph()
        full = render_graph_cpu(g)
        tiled = render_graph_cpu(g, tile_size=tile_size)
        assert np.array_equal(full, tiled)

    def test_per_pixel_chain_tiled_matches(self):
        # 纯逐像素链（无 halo）也应无缝。
        g = over_graph("auto", w=23, h=17)
        full = render_graph_cpu(g)
        tiled = render_graph_cpu(g, tile_size=(5, 4))
        assert np.array_equal(full, tiled)


class TestTileCacheKeys:
    def test_key_contains_required_fields(self):
        kwargs = dict(graph_revision=3, frame=42, node_id="out", port="display",
                      width=64, height=48, tile_rect=(0, 0, 17, 19),
                      color_space="linear", alpha_premultiplied=True,
                      encode_srgb=False, params_digest="abc")
        base = composite_cache_key(**kwargs)
        variants = {
            "revision": {**kwargs, "graph_revision": 4},
            "frame": {**kwargs, "frame": 43},
            "port": {**kwargs, "node_id": "out2"},
            "size": {**kwargs, "width": 65},
            "tile": {**kwargs, "tile_rect": (17, 0, 34, 19)},
            "color": {**kwargs, "color_space": "srgb"},
            "alpha": {**kwargs, "alpha_premultiplied": False},
            "srgb": {**kwargs, "encode_srgb": True},
            "params": {**kwargs, "params_digest": "xyz"},
        }
        for name, variant in variants.items():
            assert composite_cache_key(**variant) != base, name

    def test_cache_hit_returns_identical_pixels(self):
        cache = MetalTileCache(budget_bytes=1 << 20)
        pixels = np.random.default_rng(7).random((8, 8, 4)).astype(np.float32)
        cache.put("k", pixels)
        got = cache.get("k")
        assert got is not None
        np.testing.assert_array_equal(got, pixels.astype(np.float16))
        assert cache.get("missing") is None
        hits, misses = cache.stats()
        assert (hits, misses) == (1, 1)

    def test_cache_lru_budget_eviction(self):
        cache = MetalTileCache(budget_bytes=np.dtype(np.float16).itemsize * 4
                               * 4 * 4 * 2)  # 恰好两条 4×4 tile
        tile = np.ones((4, 4, 4), dtype=np.float32)
        cache.put("a", tile)
        cache.put("b", tile)
        assert cache.entry_count == 2
        cache.put("c", tile)  # 超预算驱逐最旧
        assert cache.entry_count == 2
        assert cache.get("a") is None
        assert cache.get("b") is not None
        assert cache.get("c") is not None

    def test_params_digest_tracks_topology_and_values(self):
        g1 = over_graph()
        g2 = over_graph()
        g2["nodes"][0]["parameters"]["color"] = (0.6, 0.0, 0.0, 0.5)
        assert graph_params_digest(g1) != graph_params_digest(g2)
        g3 = over_graph()
        g3["connections"][0]["dst_port_name"] = "B"
        assert graph_params_digest(g1) != graph_params_digest(g3)


# ── 纹理池（字节计量 / 租约） ────────────────────────────────────────────


class _FakeContext:
    def create_texture_2d(self, pixel_format, width, height, shader_write=False):
        return (pixel_format, int(width), int(height), bool(shader_write))


class TestTexturePool:
    def test_byte_accounting(self):
        pool = MetalTexturePool(_FakeContext(), budget_bytes=10 ** 9)
        entry = pool.acquire(64, 48)
        assert entry.bytes_size == 64 * 48 * 8
        assert pool.bytes_allocated == 64 * 48 * 8
        pool.release_direct(entry)
        # 复用同尺寸：不新增字节。
        again = pool.acquire(64, 48)
        assert again.texture is entry.texture
        assert pool.bytes_allocated == 64 * 48 * 8

    def test_gpu_completion_gate(self):
        pool = MetalTexturePool(_FakeContext())
        entry = pool.acquire(4, 4)
        assert pool.retire(entry)
        assert pool.retire(entry) is False           # 幂等
        assert pool.retiring_count == 1
        assert pool.live_count == 0
        got = pool.acquire(4, 4)                     # 回收前不得复用
        assert got.texture is not entry.texture
        assert pool.finalize_retire(entry)
        pool.release_direct(got)
        reused = pool.acquire(4, 4)
        assert reused.texture is entry.texture       # 完成回调后回到复用池

    def test_budget_exhaustion_raises(self):
        pool = MetalTexturePool(_FakeContext(), budget_bytes=64 * 8 * 8)
        pool.acquire(64, 8)
        with pytest.raises(MetalPoolExhausted):
            pool.acquire(64, 8)

    def test_slot_key_lease_identity(self):
        pool = MetalTexturePool(_FakeContext())
        e1 = pool.acquire(2, 2)
        e2 = pool.acquire(2, 2)
        assert e1.slot_key[0] == e2.slot_key[0]      # 同代次
        assert e1.slot != e2.slot                    # 槽号不同


# ── 服务入口（默认关闭的开关） ───────────────────────────────────────────


class TestServiceMetalGating:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("TOPOS_COMPOSITE_METAL", raising=False)
        from src.features.composite.render.composite_graph_render_service import (
            CompositeGraphRenderService, composite_metal_enabled,
        )
        assert composite_metal_enabled() is False
        service = CompositeGraphRenderService()
        assert service.render_metal(over_graph()) is None
        assert service.get_telemetry()[-1]["path"] == "metal"
        assert service.get_telemetry()[-1]["success"] is False

    def test_enabled_but_no_device(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
        from unittest.mock import patch
        from src.features.composite.render.composite_graph_render_service import (
            CompositeGraphRenderService,
        )
        with patch("src.shared.gpu.metal.MetalComputeContext.is_supported",
                   return_value=False):
            service = CompositeGraphRenderService()
            assert service.render_metal(over_graph()) is None
            assert "不可用" in service.get_telemetry()[-1]["error"]

    def test_enabled_returns_handle(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
        from src.features.composite.render.composite_graph_render_service import (
            CompositeGraphRenderService,
        )
        if not MetalComputeContext.is_supported():
            pytest.skip("无 Metal 设备")
        service = CompositeGraphRenderService()
        try:
            handle = service.render_metal(over_graph())
            assert handle is not None and handle.available
            assert handle.backend == "metal"
        finally:
            service.cleanup()

    def test_render_metal_falls_back_with_diagnostics(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
        from src.features.composite.render.composite_graph_render_service import (
            CompositeGraphRenderService,
        )
        if not MetalComputeContext.is_supported():
            pytest.skip("无 Metal 设备")
        g = graph([constant("c", 4, 4, (1, 1, 1, 1)),
                   node("b", "blur", radius=3.0), node("o", "output")],
                  [conn("c", "b"), conn("b", "o")])
        service = CompositeGraphRenderService()
        try:
            handle = service.render_metal(g)
            assert handle is not None
            assert handle.available is False
            assert "b" in handle.fallback_reason
            assert "blur" in handle.fallback_reason
            assert service.get_telemetry()[-1]["success"] is False
        finally:
            service.cleanup()


# ── 真机层：Metal kernel 对拍（无设备 skip） ─────────────────────────────

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备")


@requires_metal
class TestMetalGpuParity:
    def setup_method(self):
        self.renderer = MetalCompositeRenderer()

    def teardown_method(self):
        self.renderer.cleanup()

    def _parity(self, g, **kw):
        ref = render_graph_cpu(g)
        got = self.renderer.render_readback(g, **kw)
        assert got is not None
        np.testing.assert_allclose(got, ref, atol=GPU_TOL, rtol=0)
        return got, ref

    def test_alpha_semantics_example_on_gpu(self):
        got, _ = self._parity(over_graph("premult_force"))
        np.testing.assert_allclose(got[3, 3], (0.5, 0.0, 0.5, 1.0),
                                   atol=GPU_TOL)

    def test_checker_transform_merge_parity(self):
        self._parity(checker_transform_graph())

    @pytest.mark.parametrize("tile_size", [(17, 19), (64, 64)])
    def test_tiled_matches_full_frame_on_gpu(self, tile_size):
        g = checker_transform_graph()
        full = self.renderer.render_readback(g)
        tiled = self.renderer.render_readback(g, tile_size=tile_size)
        np.testing.assert_allclose(tiled, full, atol=1e-6, rtol=0)

    def test_all_blend_modes_parity(self):
        from src.features.composite.nodes.blend_node import BLEND_MODES
        for mode in BLEND_MODES:
            g = graph(
                [constant("a", 6, 6, (0.75, 0.25, 0.5, 0.6)),
                 constant("b", 6, 6, (0.2, 0.9, 0.4, 0.9)),
                 node("m", "merge", blend_mode=mode, alpha_policy="auto"),
                 node("o", "output")],
                [conn("a", "m", dst_port="A"), conn("b", "m", dst_port="B"),
                 conn("m", "o")])
            self._parity(g)

    def test_masked_merge_parity(self):
        g = graph(
            [constant("fg", 8, 8, (1.0, 0.0, 0.0, 1.0)),
             constant("bg", 8, 8, (0.0, 0.0, 1.0, 1.0)),
             node("mk", "mask", mask_type="rectangle", center_x=0.5,
                  center_y=0.5, width=0.5, height=0.5, feather=0.15,
                  output_mode="matte", output_width=8, output_height=8),
             node("m", "merge", blend_mode="over", mask_channel="alpha"),
             node("o", "output")],
            [conn("fg", "m", dst_port="A"), conn("bg", "m", dst_port="B"),
             conn("mk", "m", src_port="matte", dst_port="mask"),
             conn("m", "o")])
        got, ref = self._parity(g)
        # 羽化边缘存在红/蓝中间值（遮罩确实参与；背景不透明所以看 RGB）。
        edge = got[:, 2, 0]
        assert np.any((edge > 0.05) & (edge < 0.95))

    def test_premult_unpremult_and_crop_parity(self):
        g = graph(
            [constant("c", 8, 8, (0.9, 0.7, 0.5, 0.5), mode="checker",
                      color2=(0.1, 0.2, 0.3, 1.0), checker_size=3),
             node("u", "unpremult"), node("p", "premult"),
             node("k", "crop", x=1, y=1, width=6, height=5),
             node("o", "output", alpha_policy="straight")],
            [conn("c", "u"), conn("u", "p"), conn("p", "k"), conn("k", "o")])
        self._parity(g)

    def test_cache_hit_matches_cold_render(self):
        g = checker_transform_graph()
        cold = self.renderer.render_readback(g, use_cache=True)
        warm = self.renderer.render_readback(g, use_cache=True)
        np.testing.assert_allclose(warm, cold, atol=GPU_TOL, rtol=0)
        hits, misses = self.renderer.cache.stats()
        assert hits == 1 and misses == 1
        # 参数变化 → 新键 → miss。
        g2 = checker_transform_graph()
        g2["nodes"][1]["parameters"]["translate_x"] = 5.5
        other = self.renderer.render_readback(g2, use_cache=True)
        assert not np.array_equal(other, cold)
        _, misses2 = self.renderer.cache.stats()
        assert misses2 == 2

    def test_handle_lease_and_release_idempotent(self):
        handle = self.renderer.render(over_graph())
        assert handle.available
        assert handle.backend == "metal"
        assert handle.fence_ns == 0            # 生产者已等待（内容完整）
        assert handle.is_gl_oriented is False  # 图像行序
        slot = handle.texture_id
        pool = self.renderer.pool
        released = pool._live.get(slot)
        assert released is not None
        handle.release()
        handle.release()                        # 幂等
        assert pool._live.get(slot) is None

    def test_unmigrated_graph_returns_diagnostic_handle(self):
        g = graph([constant("c", 4, 4, (1, 1, 1, 1)),
                   node("b", "blur", radius=3.0), node("o", "output")],
                  [conn("c", "b"), conn("b", "o")])
        handle = self.renderer.render(g)
        assert handle.available is False
        assert "[b] blur" in handle.fallback_reason
        assert not handle.is_presentable()

    def test_pool_bytes_bounded_after_render(self):
        self.renderer.render_readback(checker_transform_graph())
        pool = self.renderer.pool
        assert pool is not None
        assert pool.bytes_allocated <= pool.budget_bytes
        assert pool.retiring_count == 0        # 完成回调已全部回收


# ── 阶段 5.6 · 有界异步发布（真机层） ───────────────────────────────────


@requires_metal
class TestAsyncPublication:
    def setup_method(self):
        self.renderer = MetalCompositeRenderer()

    def teardown_method(self):
        self.renderer.cleanup()

    def test_pending_publishes_handle_on_completion_without_wait(self):
        pending = self.renderer.render_async(checker_transform_graph(),
                                             timeline_frame=3)
        assert not isinstance(pending, CompositeGpuFrameHandle)
        assert pending.frame == 3
        # 完成事件未到前不发布（try_get 顺带驱动 poll）。
        early = pending.try_get()
        if early is not None:                  # 极快完成也必须是合法句柄
            assert early.available
            early.release()
            return
        handle = pending.wait(timeout_s=10.0)
        assert handle.available
        assert handle.backend == "metal"
        assert handle.fence_ns == 0            # 完成事件已到达（内容完整）
        assert handle.is_gl_oriented is False
        pool = self.renderer.pool
        assert pool is not None
        assert pool._live.get(handle.texture_id) is not None
        handle.release()
        handle.release()                       # 幂等
        assert pool._live.get(handle.texture_id) is None
        assert self.renderer.in_flight_frames == 0

    def test_async_pixels_match_sync_render(self):
        g = checker_transform_graph()
        pending = self.renderer.render_async(g, timeline_frame=1)
        assert not isinstance(pending, CompositeGpuFrameHandle)
        handle = pending.wait(timeout_s=10.0)
        assert handle.available
        try:
            async_pixels = self.renderer.readback(handle)
        finally:
            handle.release()
        sync_pixels = self.renderer.render_readback(g, timeline_frame=1)
        assert sync_pixels is not None
        np.testing.assert_allclose(async_pixels, sync_pixels, atol=1e-6, rtol=0)

    def test_backpressure_ring_full_returns_explicit_fallback(self):
        from src.shared.gpu.metal.submission_scheduler import (
            MetalSubmissionScheduler,
        )
        context = self.renderer._ensure_context()
        self.renderer._scheduler = MetalSubmissionScheduler(
            context, max_in_flight=1,
            completion_timeout_s=MetalCompositeRenderer.COMPLETION_TIMEOUT_S)
        first = self.renderer.render_async(over_graph(), timeline_frame=0)
        assert not isinstance(first, CompositeGpuFrameHandle)
        second = self.renderer.render_async(over_graph(), timeline_frame=1)
        assert isinstance(second, CompositeGpuFrameHandle)   # 显式回退句柄
        assert not second.available
        assert "环已满" in second.fallback_reason
        assert self.renderer.in_flight_frames == 1           # 只有第一帧在途
        handle = first.wait(timeout_s=10.0)
        assert handle.available
        handle.release()

    def test_preflight_fallback_comes_back_synchronously(self):
        g = graph([constant("c", 4, 4, (1, 1, 1, 1)),
                   node("b", "blur", radius=3.0), node("o", "output")],
                  [conn("c", "b"), conn("b", "o")])
        outcome = self.renderer.render_async(g)
        assert isinstance(outcome, CompositeGpuFrameHandle)
        assert not outcome.available
        assert "[b] blur" in outcome.fallback_reason

    def test_cleanup_with_in_flight_frame_releases_lease(self, caplog):
        import logging as _logging
        pending = self.renderer.render_async(checker_transform_graph(),
                                             timeline_frame=7)
        assert not isinstance(pending, CompositeGpuFrameHandle)
        # 不等待直接清理：排空后已发布但无人消费的句柄必须归还租约——
        # 池销毁时不得出现「仍在租约中」告警。
        with caplog.at_level(_logging.WARNING, logger="src.features.composite"
                             ".render.metal.texture_pool"):
            self.renderer.cleanup()
        assert not [r for r in caplog.records if "仍在租约中" in r.message]
