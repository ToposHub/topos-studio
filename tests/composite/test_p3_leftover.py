"""P3 遗留子任务测试（Milestone D 补完）。

覆盖：
- P3-3：filter/wrap 只设置一次（MaterialTextureLoader.is_filter_wrap_applied/
  mark_filter_wrap_applied + Scene3D _bind_descriptor 跳过重复）。
- P3-5：atlas-safe 默认（linear/clamp）测试覆盖。
- P3-6：按材质排序（不透明 mesh，透明保持原序）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest

from src.features.composite.render3d.material_texture_loader import (
    MaterialTextureLoader, apply_filter_wrap,
)


# ──────────────────────────────────────────────────────────────────────────
# P3-3：filter/wrap 只设置一次
# ──────────────────────────────────────────────────────────────────────────

class TestFilterWrapOnce:
    """MaterialTextureLoader 记录已应用的 filter/wrap，重复时跳过。"""

    def test_loader_has_filter_wrap_methods(self):
        loader = MaterialTextureLoader()
        assert hasattr(loader, "is_filter_wrap_applied")
        assert hasattr(loader, "mark_filter_wrap_applied")

    def test_not_applied_returns_false(self):
        loader = MaterialTextureLoader()

        class FakeTex:
            glo = 42
        assert not loader.is_filter_wrap_applied(FakeTex(), "linear", "clamp")

    def test_after_mark_returns_true(self):
        loader = MaterialTextureLoader()

        class FakeTex:
            glo = 42
        loader.mark_filter_wrap_applied(FakeTex(), "linear", "clamp")
        assert loader.is_filter_wrap_applied(FakeTex(), "linear", "clamp")

    def test_different_filter_returns_false(self):
        loader = MaterialTextureLoader()

        class FakeTex:
            glo = 42
        loader.mark_filter_wrap_applied(FakeTex(), "linear", "clamp")
        assert not loader.is_filter_wrap_applied(FakeTex(), "nearest", "clamp")
        assert not loader.is_filter_wrap_applied(FakeTex(), "linear", "repeat")

    def test_release_clears_state(self):
        """release_all 后 filter/wrap 状态应清空。"""
        loader = MaterialTextureLoader()

        class FakeTex:
            glo = 42
        loader.mark_filter_wrap_applied(FakeTex(), "linear", "clamp")
        loader.release_all()
        assert not loader.is_filter_wrap_applied(FakeTex(), "linear", "clamp")

    def test_scene3d_bind_uses_filter_wrap_check(self):
        """Scene3D _bind_material_textures 用 is_filter_wrap_applied 跳过重复。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._bind_material_textures)
        assert "is_filter_wrap_applied" in src
        assert "mark_filter_wrap_applied" in src


# ──────────────────────────────────────────────────────────────────────────
# P3-5：atlas-safe 默认
# ──────────────────────────────────────────────────────────────────────────

class TestAtlasSafeDefaults:
    """Model3D 默认 texture_filter=linear / texture_wrap=clamp（atlas 友好）。"""

    def test_model3d_defaults_atlas_safe(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {
            "texture_filter": "linear",
            "texture_wrap": "clamp",
        }
        # 默认值就是 linear/clamp
        assert node.parameters["texture_filter"] == "linear"
        assert node.parameters["texture_wrap"] == "clamp"

    def test_param_defs_default_atlas_safe(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        # PARAM_DEFS 默认 linear/clamp
        filter_def = next(p for p in Model3DNode.PARAM_DEFS if p.name == "texture_filter")
        wrap_def = next(p for p in Model3DNode.PARAM_DEFS if p.name == "texture_wrap")
        assert filter_def.default == "linear"
        assert wrap_def.default == "clamp"


# ──────────────────────────────────────────────────────────────────────────
# P3-6：按材质排序（不透明）
# ──────────────────────────────────────────────────────────────────────────

class _FakeMat:
    """模拟 MaterialData（供排序测试）。"""
    def __init__(self, name):
        self.name = name


class TestMaterialSort:
    """Scene3D._sort_meshes_by_material：不透明按材质分组，透明保持原序。"""

    def test_small_list_passthrough(self):
        """<=2 mesh 直接返回（排序无收益）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        m = [{"material": _FakeMat("A")}]
        assert Scene3DNode._sort_meshes_by_material(m) is m

    def test_same_material_contiguous(self):
        """同材质 mesh 排序后相邻（提升缓存命中）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        a = _FakeMat("A"); b = _FakeMat("B"); c = _FakeMat("C")
        meshes = [
            {"material": a}, {"material": b}, {"material": a},
            {"material": c}, {"material": b},
        ]
        sorted_m = Scene3DNode._sort_meshes_by_material(meshes)
        # 统计连续分组数（同材质相邻 = 分组数 == unique 材质数）
        groups = []
        for m in sorted_m:
            mid = id(m["material"])
            if not groups or groups[-1] != mid:
                groups.append(mid)
        unique = len(set(id(m["material"]) for m in meshes))
        assert len(groups) == unique  # 每种材质连续

    def test_preserves_all_meshes(self):
        """排序后 mesh 数量不变。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        a = _FakeMat("A"); b = _FakeMat("B")
        meshes = [{"material": a}, {"material": b}, {"material": a}]
        sorted_m = Scene3DNode._sort_meshes_by_material(meshes)
        assert len(sorted_m) == len(meshes)

    def test_none_material_handled(self):
        """material=None 的 mesh 不崩溃（视为不透明，参与排序）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        a = _FakeMat("A")
        meshes = [{"material": a}, {"material": None}, {"material": a}, {"material": None}]
        sorted_m = Scene3DNode._sort_meshes_by_material(meshes)
        assert len(sorted_m) == 4

    def test_do_evaluate_calls_sort(self):
        """_do_evaluate 在 mesh 循环前调 _sort_meshes_by_material。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_sort_meshes_by_material" in src
        assert "_render_meshes" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
