"""B4 批次（3D 与能力补齐——本会话实施部分）focused 回归测试。

对应 2026-10-14 复合页审计计划：R-14（导出全局 3D 场景设置）、
R-21（体积文件热更新）、N-13（插件卸载同步核心注册表）、R-24（关窗
停机结果传播的可实施核心）。R-16~19（材质）、NG/RG 能力补齐未包含。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core

import numpy as np


def _make_page_stub(qapp, monkeypatch):
    from src.features.composite.ui.composite_page import CompositePage

    page = CompositePage(parent=None)
    for name in ("_evaluate_graph", "_evaluate_graph_now"):
        monkeypatch.setattr(page, name, lambda *a, **k: None,
                            raising=False)
    return page


# ── R-14：导出全局 3D 场景设置 ─────────────────────────────────────────


class TestExport3DSettings:
    def _renderer_harness(self, snapshot):
        from types import SimpleNamespace

        from src.features.composite.core.eval_context import CompositeEvalContext
        from src.features.composite.core.graph_snapshot import (
            GraphSnapshot, NodeSnapshot,
        )
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.render.composite_export_renderer import (
            CompositeExportRenderer,
        )

        r = CompositeExportRenderer(context=SimpleNamespace(info={}))
        r._persistent = True
        r.fb_manager = object()
        r._resource_pool = object()
        r._cache_manager = object()
        r._eval_ctx = CompositeEvalContext(gl=r.ctx)
        r._scheduler = SimpleNamespace(
            plan=lambda *a, **k: SimpleNamespace(
                output_dimensions=(1, 1), needs_tiling=False))
        r._sync_render_nodes = lambda sn: None
        r._readback = lambda res: np.ones((1, 1, 3), np.float32)
        r._evaluate_snapshot = lambda **kw: TextureResult(
            texture_id=1, width=1, height=1)
        r._ensure_gpu_resources = lambda: r._install_eval_context()
        ns = NodeSnapshot("o", "output", {}, False, [], [])
        return r, GraphSnapshot(
            generation=1, nodes={"o": ns},
            global_3d_scene_settings=snapshot)

    def test_r14_settings_installed_and_survive_rebuild(self):
        r, snap = self._renderer_harness(
            {"cam_orbit_yaw": 45.0, "use_ibl": False})
        result = r.render_and_readback(snap)
        assert result.error is None
        ctx_settings = r._eval_ctx.global_3d_scene_settings
        assert ctx_settings["cam_orbit_yaw"] == 45.0
        assert ctx_settings["use_ibl"] is False
        # 中途重建 eval context 继承（R-13 同一 pending 模式）
        r._install_eval_context()
        assert r._eval_ctx.global_3d_scene_settings[
            "cam_orbit_yaw"] == 45.0

    def test_r14_snapshot_carries_persisted_settings(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.scene3d_node import Scene3DNode

        graph = NodeGraph()
        graph.add_node(Scene3DNode(node_id="sc"))
        graph._viewer_state = {"default_3d_scene": {"cam_orbit_yaw": 45.0}}
        snap = graph.snapshot(frame_number=0)
        assert snap.global_3d_scene_settings == {"cam_orbit_yaw": 45.0}


# ── R-21：体积文件热更新 ──────────────────────────────────────────────


class TestVolumeHotUpdate:
    def test_r21_same_path_overwrite_reloads(self, tmp_path):
        from src.features.composite.nodes.volume3d_node import Volume3DNode
        from src.features.composite.render3d import volume_grid

        path = str(tmp_path / "vol.npz")

        def write(density):
            np.savez(path, density=np.full((4, 4, 4), density, np.float32),
                     spacing=0.5)

        write(1.0)
        node = Volume3DNode("v")
        node.set_parameter("file_path", path)
        node.set_parameter("output_width", 8)
        node.set_parameter("output_height", 8)
        node.set_parameter("step_count", 4)

        class Ctx:
            frame_number = 0

        calls = {"n": 0}
        orig_load = volume_grid.load_volume

        def counting_load(p):
            calls["n"] += 1
            return orig_load(p)

        # _do_evaluate 内部 from ..render3d.volume_grid import load_volume
        # 在调用时读取该模块属性——patch 源模块本身。
        volume_grid.load_volume = counting_load
        try:
            node._do_evaluate({}, Ctx(), None)
            assert calls["n"] == 1
            # 同路径覆盖（同秒、同尺寸）——内容签名必须失配并重载
            write(2.0)
            node._do_evaluate({}, Ctx(), None)
            assert calls["n"] == 2
        finally:
            volume_grid.load_volume = orig_load
            node.cleanup()


# ── N-13：插件卸载同步核心注册表 ──────────────────────────────────────


class TestPluginRegistrySync:
    def _make_plugin(self):
        from src.features.composite.core.node_plugin import NodePlugin
        from src.features.composite.nodes.base_node import Node, Port

        class TmpNode(Node):
            node_type = "n13_tmp_plugin_node"

            def __init__(self, node_id=None):
                super().__init__(node_id)
                self.inputs = [Port("image", "input", "image")]
                self.outputs = [Port("result", "output", "image")]

        class TmpPlugin(NodePlugin):
            @classmethod
            def name(cls):
                return "n13_tmp_plugin"

            @classmethod
            def category(cls):
                return "Plugins"

            @classmethod
            def node_types(cls):
                return [TmpNode]

        return TmpPlugin, TmpNode

    def test_n13_unregister_removes_from_core_registry(self):
        from src.features.composite.core import node_engine as ne
        from src.features.composite.core.node_plugin import NodePluginManager

        plugin_cls, node_cls = self._make_plugin()
        manager = NodePluginManager()
        manager._plugins[plugin_cls.name()] = plugin_cls
        manager._node_types[node_cls.node_type] = node_cls

        assert ne.register_plugin_nodes(manager) == 1
        assert node_cls.node_type in ne.NODE_REGISTRY

        assert manager.unregister_plugin(plugin_cls.name()) is True

        assert node_cls.node_type not in ne.NODE_REGISTRY
        assert ne.create_node_by_type(node_cls.node_type) is None

    def test_n13_reload_replaces_class(self):
        from src.features.composite.core import node_engine as ne
        from src.features.composite.core.node_plugin import NodePluginManager

        plugin_cls, node_cls = self._make_plugin()
        manager = NodePluginManager()
        manager._plugins[plugin_cls.name()] = plugin_cls
        manager._node_types[node_cls.node_type] = node_cls
        ne.register_plugin_nodes(manager)
        manager.unregister_plugin(plugin_cls.name())

        # “重载”：同名插件的新类
        class NewNode(plugin_cls.node_types()[0].__bases__[0]):
            node_type = "n13_tmp_plugin_node"

        class NewPlugin(plugin_cls):
            @classmethod
            def node_types(cls):
                return [NewNode]

        manager._plugins[NewPlugin.name()] = NewPlugin
        manager._node_types[NewNode.node_type] = NewNode
        result = ne.sync_plugin_node_registry(manager)
        assert "n13_tmp_plugin_node" in result["registered"]
        assert ne.NODE_REGISTRY["n13_tmp_plugin_node"] is NewNode
        manager.unregister_plugin(NewPlugin.name())
        assert "n13_tmp_plugin_node" not in ne.NODE_REGISTRY


# ── R-24（传播核心）：停机结果合同 ────────────────────────────────────


class TestShutdownPropagation:
    def test_r24_page_reports_incomplete_on_timeout(self, qapp, monkeypatch):
        """真实 CompositePage + 假渲染器：超时→引用保留+False，停止→引用清空。"""
        from unittest.mock import MagicMock

        page = _make_page_stub(qapp, monkeypatch)
        fake_renderer = MagicMock()
        page._renderer = fake_renderer
        fake_renderer.stop_thread.return_value = False  # 超时
        page.cleanup()
        assert page._renderer_stop_result == "timeout"
        assert page._renderer is fake_renderer  # 引用保留（GL 资源保护）
        assert page.pipeline_shutdown_complete() is False

        fake_renderer.stop_thread.return_value = True
        page.cleanup()
        assert page._renderer is None
        assert page.pipeline_shutdown_complete() is True

    def test_r24_main_window_keeps_done_clear_on_timeout(self):
        """done 标志在 owner 未停时保持 False（可重试），不再先行置位。"""
        from src.app.main_window import MainWindow

        mw = MainWindow.__new__(MainWindow)
        mw.__dict__.update({
            "_pipeline_shutdown_done": False,
            "timeline_panel": None,
            "edit_page": None,
            "video_controller": None,
            "timeline_preview_renderer": None,
            "composite_page": type("P", (), {
                "cleanup": lambda self: None,
                "pipeline_shutdown_complete": lambda self: False,
            })(),
        })
        done = mw._shutdown_playback_pipeline()
        assert done is False
        assert mw._pipeline_shutdown_done is False  # 可重试

        mw.composite_page = type("P", (), {
            "cleanup": lambda self: None,
            "pipeline_shutdown_complete": lambda self: True,
        })()
        done = mw._shutdown_playback_pipeline()
        assert done is True
        assert mw._pipeline_shutdown_done is True
