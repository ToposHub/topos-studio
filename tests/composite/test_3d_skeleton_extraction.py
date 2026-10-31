"""
F6 骨架提取集成测试（model_loader._extract_trimesh_skeletons）。

覆盖：
- 空 scene / 无 skins → 空列表
- 有 skins → 构建 SkeletonData（bones + rest_world）
- 骨骼名去重
- parent hierarchy 推断
- 异常容错（不阻断主加载）

纯逻辑测试（mock trimesh scene 结构）。
"""

import numpy as np
import pytest

from src.features.composite.render3d.model_loader import _extract_trimesh_skeletons


class _FakeGraph:
    """模拟 trimesh SceneGraph。"""
    class _Transforms:
        def __init__(self, node_to_matrix):
            self.node_to_matrix = node_to_matrix
    def __init__(self, node_to_matrix=None):
        self.transforms = self._Transforms(node_to_matrix or {})


class _FakeScene:
    """模拟 trimesh Scene。"""
    def __init__(self, metadata=None, graph=None):
        self.metadata = metadata or {}
        self.graph = graph


class TestExtractSkeletons:
    def test_empty_metadata(self):
        scene = _FakeScene()
        assert _extract_trimesh_skeletons(scene) == []

    def test_no_skins_key(self):
        scene = _FakeScene(metadata={"other": "stuff"})
        assert _extract_trimesh_skeletons(scene) == []

    def test_empty_skins_list(self):
        scene = _FakeScene(metadata={"skins": []})
        assert _extract_trimesh_skeletons(scene) == []

    def test_single_skin_single_bone(self):
        """单个 skin + 单根骨骼 → SkeletonData（1 bone）。"""
        eye4 = np.eye(4, dtype=np.float32).tolist()
        scene = _FakeScene(
            metadata={"skins": [{
                "name": "arm",
                "joints": ["root"],
            }]},
            graph=_FakeGraph({"root": np.eye(4, dtype=np.float32)}),
        )
        result = _extract_trimesh_skeletons(scene)
        assert len(result) == 1
        skel = result[0]
        assert skel.name == "arm"
        assert skel.bone_count == 1
        assert skel.bones[0].name == "root"

    def test_multiple_bones_rest_world(self):
        """多骨骼：rest_world 由 parent 累乘。"""
        parent_local = np.eye(4, dtype=np.float32)
        parent_local[0, 3] = 1.0  # 沿 X 平移 1
        child_local = np.eye(4, dtype=np.float32)
        child_local[1, 3] = 2.0   # 沿 Y 平移 2

        scene = _FakeScene(
            metadata={"skins": [{
                "name": "s",
                "joints": ["parent", "child"],
                "hierarchy": {"child": "parent"},
            }]},
            graph=_FakeGraph({
                "parent": parent_local,
                "child": child_local,
            }),
        )
        result = _extract_trimesh_skeletons(scene)
        assert len(result) == 1
        skel = result[0]
        assert skel.bone_count == 2
        # parent world = (1,0,0)
        np.testing.assert_array_almost_equal(
            skel.bones[0].rest_world[:3, 3], [1.0, 0.0, 0.0])
        # child world = parent_world @ child_local = (1,2,0)
        np.testing.assert_array_almost_equal(
            skel.bones[1].rest_world[:3, 3], [1.0, 2.0, 0.0])
        # parent-child 关系
        assert skel.bones[1].parent_index == 0
        assert skel.bones[0].children_indices == [1]

    def test_duplicate_bone_names_uniquified(self):
        """重名骨骼被 unique_bone_names 去重。"""
        scene = _FakeScene(
            metadata={"skins": [{
                "name": "s",
                "joints": ["bone", "bone", "bone"],
            }]},
            graph=_FakeGraph({
                "bone": np.eye(4, dtype=np.float32),
            }),
        )
        result = _extract_trimesh_skeletons(scene)
        skel = result[0]
        names = [b.name for b in skel.bones]
        assert names == ["bone", "bone_2", "bone_3"]

    def test_empty_joints_skipped(self):
        scene = _FakeScene(
            metadata={"skins": [{"name": "s", "joints": []}]},
        )
        result = _extract_trimesh_skeletons(scene)
        assert result == []

    def test_exception_returns_empty(self):
        """异常时不崩溃，返回空列表。"""
        class BadScene:
            metadata = {"skins": [None]}  # None 会触发异常
            graph = None
        result = _extract_trimesh_skeletons(BadScene())
        assert result == []

    def test_no_graph_still_works(self):
        """无 graph 时用 identity local（所有骨骼 world=identity）。"""
        scene = _FakeScene(
            metadata={"skins": [{"name": "s", "joints": ["a", "b"]}]},
            graph=None,
        )
        result = _extract_trimesh_skeletons(scene)
        assert len(result) == 1
        skel = result[0]
        # 无 graph → local=identity → world=identity
        for bone in skel.bones:
            np.testing.assert_array_almost_equal(bone.rest_world, np.eye(4))

    def test_multiple_skins(self):
        """多个 skin → 多个 SkeletonData。"""
        scene = _FakeScene(
            metadata={"skins": [
                {"name": "upper", "joints": ["spine"]},
                {"name": "lower", "joints": ["leg"]},
            ]},
            graph=_FakeGraph({
                "spine": np.eye(4, dtype=np.float32),
                "leg": np.eye(4, dtype=np.float32),
            }),
        )
        result = _extract_trimesh_skeletons(scene)
        assert len(result) == 2
        assert result[0].name == "upper"
        assert result[1].name == "lower"
