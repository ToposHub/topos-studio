"""P7：对象级 DCC 交互闭环测试。

覆盖：
- P7-1：GPU ID picking（payload node_id + ID pass + eval_ctx 字段 + metadata 交接 +
  viewer pick_object_at + _handle_3d_left_press 接线）
- P7-2：Selected outline（outline shader + _render_outline_pass + selected_3d_node_id）
- P7-3：Frame selected（_frame_selected_3d + F 键语义）
- P7-4：Isolate selected（isolate_node_id 状态 + Shift+I toggle + 渲染过滤）

注：真实 GL 渲染需 GL；用源码级断言 + 纯逻辑单测，与 P0-P6 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest


# ──────────────────────────────────────────────────────────────────────────
# P7-1：GPU ID picking — payload + eval_ctx + shader
# ──────────────────────────────────────────────────────────────────────────

class TestPayloadNodeId:
    """ModelPayload / GeometryPayload 带 node_id 字段（picking 身份）。"""

    def test_geometry_payload_has_node_id(self):
        from src.features.composite.render3d.scene_payload import GeometryPayload
        gp = GeometryPayload(vao=None)
        assert hasattr(gp, "node_id")
        assert gp.node_id == ""

    def test_model_payload_has_node_id(self):
        from src.features.composite.render3d.scene_payload import ModelPayload
        mp = ModelPayload()
        assert hasattr(mp, "node_id")
        assert mp.node_id == ""

    def test_collect_payloads_injects_node_id(self):
        """_collect_connected_scene_payloads 给所有 payload 注入 node_id（不只 Camera）。

        AUD-64003 修复后注入落在 payload 副本上（共享 payload 不再被
        原地改写，多个聚合器互不污染）——由源码文本锁改为行为断言。
        """
        from src.features.composite.core.eval_context import CompositeEvalContext
        from src.features.composite.core.node_engine import NODE_REGISTRY
        from src.features.composite.render3d.scene_payload import (
            GeometryPayload, ModelPayload)

        for payload in (ModelPayload(), GeometryPayload(vao=None)):
            ctx = CompositeEvalContext(gl="fake", scene_payloads={
                "src1": {"display": payload},
            })
            ctx.scene_connections = {"agg": {"in": ["src1"]}}
            agg = NODE_REGISTRY["scene3d"]("agg")
            result = agg._collect_connected_scene_payloads(ctx, type(payload))
            assert len(result) == 1
            assert result[0].node_id == "src1"
            # 共享的原 payload 不被改写（AUD-64003）。
            assert payload.node_id == ""


class TestEvalContextP7Fields:
    """CompositeEvalContext 有 id_pick_info / selected_3d_node_id / isolate_3d_node_id。"""

    def test_fields_exist(self):
        from src.features.composite.core.eval_context import CompositeEvalContext
        ctx = CompositeEvalContext()
        assert hasattr(ctx, "id_pick_info")
        assert hasattr(ctx, "selected_3d_node_id")
        assert hasattr(ctx, "isolate_3d_node_id")
        assert ctx.id_pick_info is None
        assert ctx.selected_3d_node_id is None
        assert ctx.isolate_3d_node_id is None


class TestRenderRequestP7Fields:
    """RenderRequest 有 selected_3d_node_id / isolate_3d_node_id。"""

    def test_fields_exist(self):
        from src.features.composite.render.render_scheduler import RenderRequest
        src = inspect.getsource(RenderRequest)
        assert "selected_3d_node_id" in src
        assert "isolate_3d_node_id" in src


class TestIDWriteShader:
    """_MESH_ID_WRITE_FRAGMENT 存在且输出 u_object_id。"""

    def test_shader_exists(self):
        from src.features.composite.core import shaders
        assert hasattr(shaders, "_MESH_ID_WRITE_FRAGMENT")
        assert "u_object_id" in shaders._MESH_ID_WRITE_FRAGMENT

    def test_outline_shaders_exist(self):
        """P7-2：outline vertex/fragment shader 存在。"""
        from src.features.composite.core import shaders
        assert hasattr(shaders, "_MESH_OUTLINE_VERTEX")
        assert hasattr(shaders, "_MESH_OUTLINE_FRAGMENT")
        assert "u_outline_scale" in shaders._MESH_OUTLINE_VERTEX
        assert "u_outline_color" in shaders._MESH_OUTLINE_FRAGMENT


class TestScene3DIDPass:
    """Scene3D 有 ID pass + pick_map + outline pass 的方法和资源。"""

    def test_id_resources_in_init(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "_id_texture" in src
        assert "_id_write_prog" in src
        assert "_id_write_skinned_prog" in src
        assert "_id_fbo" in src
        assert "_id_write_vaos" in src

    def test_skinned_mesh_uses_skinning_contract_for_depth_and_id(self):
        """Animated meshes must not fall back to bind-pose auxiliary passes."""
        from src.features.composite.nodes.scene3d_node import Scene3DNode

        depth_src = inspect.getsource(Scene3DNode._get_depth_write_vao)
        id_src = inspect.getsource(Scene3DNode._get_id_write_vao)
        depth_pass_src = inspect.getsource(Scene3DNode._render_depth_pass)
        id_pass_src = inspect.getsource(Scene3DNode._render_id_pass)

        assert "_depth_write_skinned_prog" in inspect.getsource(Scene3DNode.__init__)
        assert "interleaved_skinned" in depth_src
        assert "interleaved_skinned" in id_src
        assert "_ensure_depth_write_skinned_program" in depth_pass_src
        assert "_ensure_id_write_skinned_program" in id_pass_src
        assert "_upload_bone_matrices" in depth_pass_src
        assert "_upload_bone_matrices" in id_pass_src

    def test_skinned_mesh_is_not_culled_by_bind_pose_aabb(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode

        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "GPU skinning can move vertices outside the bind-pose" in src
        assert "not is_skinned and not aabb_in_frustum" in src

    def test_outline_resources_in_init(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "_outline_prog" in src
        assert "_outline_vaos" in src

    def test_id_pass_methods_exist(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert hasattr(Scene3DNode, "_render_id_pass")
        assert hasattr(Scene3DNode, "_build_pick_id_map")
        assert hasattr(Scene3DNode, "_encode_pick_id")

    def test_outline_pass_method_exists(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert hasattr(Scene3DNode, "_render_outline_pass")

    def test_encode_pick_id_background(self):
        """pick_id=0 → 全零 RGBA（背景，viewer 读回 alpha=0 判定未命中）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        r, g, b, a = Scene3DNode._encode_pick_id(0)
        assert (r, g, b, a) == (0.0, 0.0, 0.0, 0.0)

    def test_encode_pick_id_nonzero(self):
        """pick_id>0 → alpha=1（命中）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        r, g, b, a = Scene3DNode._encode_pick_id(1)
        assert a == 1.0
        assert r > 0.0  # R 通道非零

    def test_encode_decode_roundtrip(self):
        """encode → decode 可逆（pick_id 1..1000）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        for pid in [1, 42, 255, 256, 1000, 65535]:
            r, g, b, a = Scene3DNode._encode_pick_id(pid)
            # 解码（viewer 读回时做）
            decoded = (int(r * 255) | (int(g * 255) << 8) | (int(b * 255) << 16))
            assert decoded == pid, f"roundtrip failed for {pid}"

    def test_build_pick_id_map(self):
        """_build_pick_id_map 给每个唯一 node_id 分配 pick_id（1 起）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode.__new__(Scene3DNode)

        class FakePayload:
            def __init__(self, nid):
                self.node_id = nid
        models = [FakePayload("m1")]
        geos = [FakePayload("g1"), FakePayload("g2")]
        pick_map, node_ids = node._build_pick_id_map(models, geos)
        assert pick_map[1] == "m1"
        assert pick_map[2] == "g1"
        assert pick_map[3] == "g2"
        assert node_ids["m1"] == 1
        assert node_ids["g1"] == 2

    def test_build_pick_id_map_dedup(self):
        """同 node_id 只分配一个 pick_id（多 mesh 共享）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode.__new__(Scene3DNode)

        class FakePayload:
            def __init__(self, nid):
                self.node_id = nid
        models = [FakePayload("m1")]  # 同一 node
        geos = [FakePayload("m1")]    # 同一 node（不同 payload 类型但同节点）
        pick_map, node_ids = node._build_pick_id_map(models, geos)
        assert len(pick_map) == 1  # 只有一个唯一 node_id
        assert node_ids["m1"] == 1

    def test_do_evaluate_calls_id_pass(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_render_id_pass" in src
        assert "_render_outline_pass" in src

    def test_do_evaluate_forwards_id_pick_info(self):
        """§6e 转发 id_pick_info 到 eval_ctx（cache-hit 帧也生效）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "eval_ctx.id_pick_info" in src
        assert "_p7_id_pick_info" in src

    def test_do_evaluate_isolate_filter(self):
        """主渲染循环用 _p7_isolate_nid 过滤非选中节点。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_p7_isolate_nid" in src

    def test_do_evaluate_isolate_model_path_really_skips(self):
        """隔离到非当前 Model3D 时，主模型路径必须真正跳过，而不是 pass 后继续画。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_render_model_path = not" in src
        assert "if _render_model_path:" in src
        assert "pass  # 隔离模式：当前模型不是隔离目标" not in src


# ──────────────────────────────────────────────────────────────────────────
# P7-1：renderer / evaluator 同步
# ──────────────────────────────────────────────────────────────────────────

class TestRendererEvaluatorP7Sync:
    """renderer → evaluator → eval_ctx 同步 P7 字段。"""

    def test_renderer_passes_selected_isolate(self):
        from src.features.composite.render.composite_renderer import CompositeRenderer
        src = inspect.getsource(CompositeRenderer)
        assert "selected_3d_node_id" in src
        assert "isolate_3d_node_id" in src

    def test_renderer_emits_id_pick(self):
        from src.features.composite.render.composite_renderer import CompositeRenderer
        src = inspect.getsource(CompositeRenderer)
        assert "id_pick" in src

    def test_evaluator_signature_has_p7_params(self):
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        sig = inspect.signature(BaseEvaluatorMixin._evaluate_snapshot)
        assert "selected_3d_node_id" in sig.parameters
        assert "isolate_3d_node_id" in sig.parameters

    def test_evaluator_syncs_p7_to_eval_ctx(self):
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        src = inspect.getsource(BaseEvaluatorMixin._evaluate_snapshot)
        assert "eval_ctx_ref.selected_3d_node_id" in src
        assert "eval_ctx_ref.isolate_3d_node_id" in src
        assert "eval_ctx_ref.id_pick_info" in src


# ──────────────────────────────────────────────────────────────────────────
# P7-1/3/4：viewer picking + frame + isolate
# ──────────────────────────────────────────────────────────────────────────

class TestViewerPicking:
    """CompositeViewer GPU ID picking 方法 + 接线。"""

    def test_set_id_pick_info_exists(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "set_id_pick_info")

    def test_pick_object_at_exists(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "pick_object_at")

    def test_pick_uses_glreadpixels(self):
        """pick_object_at 用 glReadPixels 读 1 像素 RGBA8。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.pick_object_at)
        assert "glReadPixels" in src
        assert "GL_RGBA" in src
        assert "GL_UNSIGNED_BYTE" in src

    def test_pick_decodes_pick_id(self):
        """读回 RGBA → 解码 pick_id → 查 pick_map → node_id。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.pick_object_at)
        assert "pick_map.get(pick_id)" in src

    def test_handle_3d_left_press_uses_pick(self):
        """_handle_3d_left_press 第 3 段调 pick_object_at（不再 no-op）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_3d_left_press)
        assert "pick_object_at" in src
        assert "_select_node_by_id" in src


class TestFrameSelected:
    """P7-3：F 键 frame selected。"""

    def test_frame_selected_method_exists(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "_frame_selected_3d")

    def test_frame_uses_selected_center(self):
        """_frame_selected_3d 复用 _selected_3d_orbit_center 拿对象中心。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._frame_selected_3d)
        assert "_selected_3d_orbit_center" in src

    def test_frame_returns_bool(self):
        """_frame_selected_3d 返回 True（有选中）/ False（无选中，回退 reset）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._frame_selected_3d)
        assert "return False" in src
        assert "return True" in src

    def test_f_key_uses_frame_then_reset(self):
        """F 键：有选中 → frame；无选中 → reset。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        # 找 keyPressEvent 里的 F 键处理
        src = inspect.getsource(CompositeViewer.keyPressEvent)
        assert "_frame_selected_3d" in src
        assert "_reset_3d_camera" in src


class TestIsolateSelected:
    """P7-4：Shift+I toggle isolate。"""

    def test_isolate_field_in_state(self):
        from src.features.composite.ui.viewport3d_state import Viewport3DState
        s = Viewport3DState()
        assert hasattr(s, "isolate_node_id")
        assert s.isolate_node_id is None  # 默认不隔离

    def test_toggle_method_exists(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "_toggle_isolate_selected")

    def test_toggle_sets_isolate_node_id(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._toggle_isolate_selected)
        assert "isolate_node_id" in src
        assert "None" in src  # 退出隔离分支

    def test_shift_i_handler(self):
        """Shift+I 触发 _toggle_isolate_selected。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.keyPressEvent)
        assert "Key_I" in src
        assert "_toggle_isolate_selected" in src


class TestPageP7Plumbing:
    """CompositePage 转发 id_pick + 填 selected/isolate 到 request。"""

    def test_metadata_forwards_id_pick(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_metadata_ready)
        assert "id_pick" in src
        assert "set_id_pick_info" in src

    def test_metadata_clears_stale_id_pick_when_missing(self):
        """id_pick=None 的帧也要同步到 viewer，避免沿用上一帧 picking 纹理。"""
        from src.features.composite.ui.composite_page import CompositePage

        class FakeViewer:
            def __init__(self):
                self.id_infos = []
                self.cleared = 0

            def set_id_pick_info(self, info):
                self.id_infos.append(info)

            def clear_quad_textures(self):
                self.cleared += 1

            def is_quad_view(self):
                return False

        page = CompositePage.__new__(CompositePage)
        page._viewer = FakeViewer()
        page._is_active = False
        page._on_metadata_ready({"id_pick": None, "ortho_views": None})
        assert page._viewer.id_infos == [None]

    def test_quad_metadata_without_ortho_keeps_existing_textures(self):
        """四视图开启时，静态/cached metadata 没有 ortho_views 不应清空已有三视图。"""
        from src.features.composite.ui.composite_page import CompositePage

        class FakeViewer:
            def __init__(self):
                self.cleared = 0
                self.id_infos = []

            def is_quad_view(self):
                return True

            def clear_quad_textures(self):
                self.cleared += 1

            def set_id_pick_info(self, info):
                self.id_infos.append(info)

        page = CompositePage.__new__(CompositePage)
        page._viewer = FakeViewer()
        page._is_active = False
        page._on_metadata_ready({"ortho_views": None, "id_pick": None})
        assert page._viewer.cleared == 0

    def test_request_includes_selected_isolate(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._build_render_request)
        assert "selected_3d_node_id=" in src
        assert "isolate_3d_node_id=" in src

    def test_selected_helper_exists(self):
        from src.features.composite.ui.composite_page import CompositePage
        assert hasattr(CompositePage, "_selected_3d_node_id_for_request")
        assert hasattr(CompositePage, "_isolate_3d_node_id_for_request")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
