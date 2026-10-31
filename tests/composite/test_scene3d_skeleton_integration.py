"""
F6 Scene3D 骨骼 overlay 集成测试（结构验证）。

验证 Scene3D 正确接入了 skeleton overlay 渲染：
- show_skeleton 参数读取
- SkeletonOverlayRenderer 懒建 + cleanup 释放
- ModelPayload.skeletons 字段传递
- SceneGraphRoot.skeletons 字段

结构测试（inspect source），不需要 GPU。
"""

import inspect

import pytest

from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.render3d.scene_graph import SceneGraphRoot, SceneNode
from src.features.composite.render3d.scene_payload import ModelPayload
from src.features.composite.render3d.skeleton_renderer import SkeletonOverlayRenderer


class TestScene3DSkeletonStructure:
    def test_skeleton_renderer_imported(self):
        """Scene3D 导入 SkeletonOverlayRenderer。"""
        src = inspect.getsource(Scene3DNode)
        assert "SkeletonOverlayRenderer" in src

    def test_show_skeleton_param_read(self):
        """_do_evaluate 读取 show_skeleton 参数。"""
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert 'show_skeleton' in src
        assert '_sp("show_skeleton"' in src

    def test_skeleton_renderer_declared(self):
        """__init__ 声明 _skeleton_renderer。"""
        src = inspect.getsource(Scene3DNode.__init__)
        assert "_skeleton_renderer" in src

    def test_skeleton_overlay_render_in_do_evaluate(self):
        """_do_evaluate 含骨骼 overlay 渲染段。"""
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "show_skeleton" in src
        assert "skeletons" in src
        assert "_skeleton_renderer" in src

    def test_cleanup_releases_skeleton_renderer(self):
        """cleanup 释放 _skeleton_renderer。"""
        src = inspect.getsource(Scene3DNode.cleanup)
        assert "_skeleton_renderer" in src
        assert "release" in src

    def test_skeleton_renderer_default_none(self):
        n = Scene3DNode("t")
        assert n._skeleton_renderer is None


class TestModelPayloadSkeletons:
    def test_skeletons_field_exists(self):
        p = ModelPayload()
        assert hasattr(p, 'skeletons')
        assert p.skeletons is None  # 默认 None

    def test_skeletons_not_in_signature(self):
        """skeletons 不进 signature（辅助显示，不影响渲染结果）。"""
        p1 = ModelPayload(skeletons=["a"])
        p2 = ModelPayload(skeletons=None)
        # skeletons 不同但 signature 相同
        assert p1.signature() == p2.signature()


class TestSceneGraphRootSkeletons:
    def test_skeletons_field_exists(self):
        root = SceneGraphRoot(root=SceneNode(name="r"))
        assert hasattr(root, 'skeletons')
        assert root.skeletons == []  # 默认空列表

    def test_skeletons_assignable(self):
        root = SceneGraphRoot(root=SceneNode(name="r"))
        root.skeletons = ["skel1", "skel2"]
        assert len(root.skeletons) == 2


class TestSkeletonOverlayRendererInstantiation:
    def test_can_instantiate(self):
        r = SkeletonOverlayRenderer()
        assert r._built is False
        assert r._line_prog is None
        assert r._point_prog is None

    def test_release_without_build(self):
        r = SkeletonOverlayRenderer()
        r.release()
        assert r._built is False
