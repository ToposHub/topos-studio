"""P6-B：独立 viewport renderer 骨架测试（架构地基）。

P6-B 目标：让相机导航脱离完整 eval 链路。本轮交付：
- _view_signature 拆分为 cam 段 + non_cam 段（快速路径判定基础）
- CompositeCommand.RENDER_3D_VIEW 命令（渲染线程快速路径入口）
- _handle_render_3d_view handler（缓存命中时 view-only，其他情况完整 eval）

这里验证缓存兼容性、回退契约和不会重新触发完整 graph evaluator。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import queue
from types import SimpleNamespace
from unittest.mock import Mock

from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.render.render_scheduler import RenderMode


class TestViewSignatureSplit:
    """_view_signature 拆分为 cam 段 + non_cam 段（P6-B 判定基础）。"""

    def test_has_view_signature_cam_method(self):
        from src.features.composite.ui.composite_page import CompositePage
        assert hasattr(CompositePage, "_view_signature_cam")

    def test_has_view_signature_non_cam_method(self):
        from src.features.composite.ui.composite_page import CompositePage
        assert hasattr(CompositePage, "_view_signature_non_cam")

    def test_cam_sig_contains_camera_params(self):
        """_view_signature_cam 含相机参数（yaw/pitch/dist/target）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._view_signature_cam)
        assert "cam_orbit_yaw" in src
        assert "cam_orbit_pitch" in src
        assert "cam_dist" in src

    def test_non_cam_sig_contains_mode_quad_nav(self):
        """_view_signature_non_cam 含 mode/quad/ortho/node/nav（非相机状态）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._view_signature_non_cam)
        assert "get_view_mode" in src
        assert "is_quad_view" in src
        assert "nav_active" in src

    def test_non_cam_sig_excludes_camera(self):
        """_view_signature_non_cam 不含相机参数（cam_orbit_*/cam_dist）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._view_signature_non_cam)
        # non_cam 不应读取 _nav_params_dict（那是相机参数源）
        assert "_nav_params_dict" not in src


class TestRender3DViewCommand:
    """CompositeCommand.RENDER_3D_VIEW 命令 + handler。"""

    def test_command_constant_exists(self):
        from src.features.composite.render.composite_renderer import CompositeCommand
        assert CompositeCommand.RENDER_3D_VIEW == "render_3d_view"

    def test_process_command_handles_render_3d_view(self):
        """_process_command 分发 RENDER_3D_VIEW 到 _handle_render_3d_view。"""
        from src.features.composite.render.composite_renderer import CompositeRenderer
        src = inspect.getsource(CompositeRenderer._process_command)
        assert "CompositeCommand.RENDER_3D_VIEW" in src
        assert "_handle_render_3d_view" in src

    def test_handle_render_3d_view_exists(self):
        from src.features.composite.render.composite_renderer import CompositeRenderer
        assert hasattr(CompositeRenderer, "_handle_render_3d_view")

    def test_handle_render_3d_view_checks_cached_node(self):
        """handler 查 _render_nodes 缓存的 scene3d 实例。"""
        from src.features.composite.render.composite_renderer import CompositeRenderer
        src = inspect.getsource(CompositeRenderer._handle_render_3d_view)
        assert "_render_nodes" in src
        assert "_last_scene_content_sig" in src

    def test_handle_render_3d_view_fallback_to_evaluate(self):
        """无缓存实例时仍降级到完整图求值。"""
        from src.features.composite.render.composite_renderer import CompositeRenderer
        src = inspect.getsource(CompositeRenderer._handle_render_3d_view)
        assert "_handle_evaluate_graph(data)" in src

    def test_cached_scene3d_uses_view_only_redraw(self):
        """命中同一图代际时，handler 不应再次调用完整 evaluator。"""
        from src.features.composite.render.composite_renderer import CompositeRenderer

        snapshot = SimpleNamespace(
            generation=7,
            color_revision=3,
            color_texture_id=11,
            color_width=1920,
            color_height=1080,
        )
        request = SimpleNamespace(
            request_id=12,
            render_mode=RenderMode.PREVIEW,
            viewer_node_id="__viewer3d_scene__",
            graph_generation=7,
            color_revision=3,
            snapshot=snapshot,
            force_refresh=False,
            interaction_active=True,
            quad_view=False,
            single_ortho_view=None,
            quad_active_view=None,
            selected_3d_node_id=None,
            isolate_3d_node_id=None,
            view_mode_overrides={},
            global_3d_scene_settings={},
            viewer_roi=None,
            proxy_scale=1.0,
            view_signature="3d|cam=new",
        )
        result = TextureResult(texture_id=42, width=2, height=2)
        node = SimpleNamespace(
            node_type="scene3d",
            _last_scene_content_sig=("cached",),
            redraw_with_view=Mock(return_value=result),
        )
        scheduler = SimpleNamespace(
            is_stale=Mock(return_value=False),
        )
        renderer = SimpleNamespace(
            _render_nodes={"__viewer3d_scene__": node},
            _last_render_request=request,
            _eval_ctx=SimpleNamespace(),
            _snapshot=snapshot,
            _scheduler=scheduler,
            fb_manager=object(),
            _resource_pool=object(),
            _handle_evaluate_graph=Mock(),
            _render_to_output=Mock(),
            # AUD-8201: the fast path now releases transient textures after
            # presenting (same discipline as the full-evaluate path).
            _release_transient_textures=Mock(),
            _last_eval_generation=-1,
            _last_color_revision=-1,
            _last_render_mode="",
            _last_viewer_roi=None,
            _last_proxy_scale=1.0,
            _last_color_texture_id=0,
            _last_color_width=0,
            _last_color_height=0,
            _last_view_signature=None,
        )

        CompositeRenderer._handle_render_3d_view(renderer, {
            "node_id": "__viewer3d_scene__",
            "cam_params": {"cam_dist": 4.0},
            "request": request,
        })

        node.redraw_with_view.assert_called_once()
        renderer._render_to_output.assert_called_once_with(result, request_id=12)
        renderer._release_transient_textures.assert_called_once()
        renderer._handle_evaluate_graph.assert_not_called()

    def test_changed_graph_generation_falls_back(self):
        """场景图代际变化时不能使用旧 payload 做 view-only 重绘。"""
        from src.features.composite.render.composite_renderer import CompositeRenderer

        old_snapshot = SimpleNamespace(
            generation=7, color_revision=3, color_texture_id=11,
            color_width=1920, color_height=1080,
        )
        new_snapshot = SimpleNamespace(
            generation=8, color_revision=3, color_texture_id=11,
            color_width=1920, color_height=1080,
        )
        base = dict(
            request_id=12, render_mode=RenderMode.PREVIEW,
            viewer_node_id="__viewer3d_scene__", graph_generation=8,
            color_revision=3, snapshot=new_snapshot, force_refresh=False,
        )
        request = SimpleNamespace(**base)
        cached = SimpleNamespace(**dict(base, graph_generation=7, snapshot=old_snapshot))
        node = SimpleNamespace(
            node_type="scene3d", _last_scene_content_sig=("cached",),
            redraw_with_view=Mock(),
        )
        renderer = SimpleNamespace(
            _render_nodes={"__viewer3d_scene__": node},
            _last_render_request=cached,
            _eval_ctx=SimpleNamespace(), _snapshot=old_snapshot,
            _handle_evaluate_graph=Mock(),
        )

        CompositeRenderer._handle_render_3d_view(renderer, {
            "node_id": "__viewer3d_scene__", "request": request,
        })

        node.redraw_with_view.assert_not_called()
        renderer._handle_evaluate_graph.assert_called_once()

    def test_view_only_commands_are_coalesced(self):
        """高频导航只保留队列中 request_id 最大的 view-only 命令。"""
        from src.features.composite.render.composite_renderer import CompositeCommand
        from src.features.composite.render.composite_renderer import CompositeRenderer

        renderer = CompositeRenderer.__new__(CompositeRenderer)
        renderer.command_queue = queue.Queue()
        renderer._scheduler = SimpleNamespace(
            advance_request_id=Mock(),
        )
        for request_id in (1, 2, 3):
            renderer.submit(CompositeCommand.RENDER_3D_VIEW, {
                "request": SimpleNamespace(request_id=request_id),
            })

        queued = []
        while not renderer.command_queue.empty():
            queued.append(renderer.command_queue.get_nowait())
        assert len(queued) == 1
        assert queued[0][1]["request"].request_id == 3
        assert renderer._scheduler.advance_request_id.call_count == 3


class TestP6BNoRegression:
    """P6-B 骨架不破坏现有 view_signature 契约。"""

    def test_view_signature_still_works(self):
        """_view_signature 仍返回完整签名（cam + non_cam 合并）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._view_signature)
        # 仍包含相机段和非相机段的组合
        assert "cam=" in src
        assert "nav1" in src or "nav0" in src
