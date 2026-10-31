"""Milestone F / P5: Viewport LOD Mesh 测试。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §9 (P5)：
1. lod_builder：顶点聚类简化、build_lod/build_lod_levels、choose_lod_level
2. LOD 保留 UV（§9.5 注意点1）
3. 蒙皮 mesh 不简化（§9.5 注意点3）
4. Model3D _resolve_lod_level 决策（大模型→LOD2）
5. Model3D _apply_lod_to_flat_meshes 附 lod_mesh
6. Scene3D _get_lod_vao + 导航态用 LOD mesh 渲染（源码级）
7. LOD 等级常量（§9.2）

注：LOD 算法用合成 mesh 真测；Scene3D/Model3D 集成用源码级断言。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import numpy as np
import pytest

from src.features.composite.render3d.primitives import MeshData
from src.features.composite.render3d.lod_builder import (
    build_lod, build_lod_levels, choose_lod_level, triangle_count,
    lod_ratio_for_level, LOD_LEVELS, _estimate_cluster_size,
)


# ──────────────────────────────────────────────────────────────────────────
# 合成 mesh 工具
# ──────────────────────────────────────────────────────────────────────────

def _make_grid_mesh(nx: int = 50, ny: int = 50) -> MeshData:
    """生成细分的平面网格 mesh（高顶点数，适合测 LOD）。"""
    xs = np.linspace(-1, 1, nx, dtype=np.float32)
    ys = np.linspace(-1, 1, ny, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys)
    verts = np.column_stack([gx.ravel(), gy.ravel(),
                             np.zeros(nx * ny, dtype=np.float32)])
    uvs = np.column_stack([
        np.tile(np.linspace(0, 1, nx, dtype=np.float32), ny),
        np.repeat(np.linspace(0, 1, ny, dtype=np.float32), nx),
    ])
    normals = np.tile(np.array([0, 0, 1], dtype=np.float32), (nx * ny, 1))
    idx = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            a = j * nx + i; b = a + 1; c = a + nx; d = c + 1
            idx += [a, b, c, b, d, c]
    return MeshData(vertices=verts, normals=normals, uvs=uvs,
                    indices=np.array(idx, dtype=np.uint32))


# ──────────────────────────────────────────────────────────────────────────
# Part 1: LOD 等级常量（§9.2）
# ──────────────────────────────────────────────────────────────────────────

class TestLODLevels:
    """LOD 等级比例常量。"""

    def test_lod0_full(self):
        assert LOD_LEVELS[0] == 1.0
        assert lod_ratio_for_level(0) == 1.0

    def test_lod_decreasing(self):
        """等级越高，比例越低。"""
        assert LOD_LEVELS[1] > LOD_LEVELS[2] > LOD_LEVELS[3]
        assert lod_ratio_for_level(2) < lod_ratio_for_level(1)

    def test_unknown_level_returns_full(self):
        assert lod_ratio_for_level(99) == 1.0


# ──────────────────────────────────────────────────────────────────────────
# Part 2: build_lod 顶点聚类
# ──────────────────────────────────────────────────────────────────────────

class TestBuildLOD:
    """build_lod 顶点聚类简化。"""

    def test_ratio_1_returns_copy(self):
        """target_ratio=1.0 → 不简化，返回原 mesh 的拷贝。"""
        mesh = _make_grid_mesh(20, 20)
        lod = build_lod(mesh, target_ratio=1.0)
        assert triangle_count(lod) == triangle_count(mesh)

    def test_lower_ratio_fewer_tris(self):
        """比例越低，三角形越少。"""
        mesh = _make_grid_mesh(50, 50)
        lod0 = build_lod(mesh, target_ratio=1.0)
        lod1 = build_lod(mesh, target_ratio=0.5)
        lod2 = build_lod(mesh, target_ratio=0.25)
        assert triangle_count(lod0) >= triangle_count(lod1)
        assert triangle_count(lod1) >= triangle_count(lod2)
        # LOD2 明显比 LOD0 少
        assert triangle_count(lod2) < triangle_count(lod0)

    def test_preserves_uv(self):
        """简化后 mesh 仍有 UV（§9.5 注意点1：LOD 必须保留 UV）。"""
        mesh = _make_grid_mesh(30, 30)
        lod = build_lod(mesh, target_ratio=0.25)
        assert lod.uvs.shape[0] == lod.vertices.shape[0]
        assert lod.uvs.shape[1] == 2

    def test_skinned_mesh_not_simplified(self):
        """蒙皮 mesh 不简化（§9.5 注意点3，返回 LOD0）。"""
        n = 100
        mesh = MeshData(
            vertices=np.random.randn(n, 3).astype(np.float32),
            normals=np.tile([0, 1, 0], (n, 1)).astype(np.float32),
            uvs=np.zeros((n, 2), dtype=np.float32),
            indices=np.arange(n * 3, dtype=np.uint32) % n,
            bone_indices=np.zeros((n, 4), dtype=np.uint32),
            bone_weights=np.tile([1, 0, 0, 0], (n, 1)).astype(np.float32),
        )
        orig_tris = triangle_count(mesh)
        lod = build_lod(mesh, target_ratio=0.25)
        assert triangle_count(lod) == orig_tris  # 不简化

    def test_small_mesh_not_simplified(self):
        """顶点过少的 mesh 不简化（< 24 顶点）。"""
        mesh = _make_grid_mesh(3, 3)  # 9 顶点
        lod = build_lod(mesh, target_ratio=0.1)
        assert triangle_count(lod) == triangle_count(mesh)

    def test_lod_mesh_is_new_object(self):
        """build_lod 返回新 MeshData（不改原 mesh）。"""
        mesh = _make_grid_mesh(20, 20)
        orig_verts = mesh.vertices.copy()
        _ = build_lod(mesh, target_ratio=0.25)
        assert np.array_equal(mesh.vertices, orig_verts)  # 原 mesh 不变

    def test_estimate_cluster_size_zero_for_full(self):
        """ratio=1.0 → cluster_size=0（不合并）。"""
        mesh = _make_grid_mesh(20, 20)
        assert _estimate_cluster_size(mesh, 1.0) == 0.0


# ──────────────────────────────────────────────────────────────────────────
# Part 3: build_lod_levels + choose_lod_level
# ──────────────────────────────────────────────────────────────────────────

class TestLODLevelsBuilder:
    """build_lod_levels 多级生成 + choose_lod_level 选择。"""

    def test_build_levels_count(self):
        mesh = _make_grid_mesh(30, 30)
        levels = build_lod_levels(mesh, ratios=(1.0, 0.5, 0.25))
        assert len(levels) == 3

    def test_build_levels_decreasing_tris(self):
        mesh = _make_grid_mesh(40, 40)
        levels = build_lod_levels(mesh, ratios=(1.0, 0.5, 0.25))
        assert triangle_count(levels[0]) >= triangle_count(levels[1])
        assert triangle_count(levels[1]) >= triangle_count(levels[2])

    def test_choose_lod_clamped(self):
        mesh = _make_grid_mesh(20, 20)
        levels = build_lod_levels(mesh, ratios=(1.0, 0.5, 0.25))
        # 超出范围钳制
        assert choose_lod_level(levels, 99) == 2
        assert choose_lod_level(levels, -1) == 0

    def test_choose_lod_threshold_small_model(self):
        """小模型（< 阈值）即使 target=2 也用 LOD0。"""
        mesh = _make_grid_mesh(10, 10)  # ~162 tris
        levels = build_lod_levels(mesh, ratios=(1.0, 0.5, 0.25))
        # 阈值 1000 > 162 tris → 用 LOD0
        assert choose_lod_level(levels, 2, triangle_thresholds=(0, 100, 1000)) == 0

    def test_choose_lod_empty(self):
        assert choose_lod_level([], 2) == 0


# ──────────────────────────────────────────────────────────────────────────
# Part 4: Model3D LOD 决策（源码级）
# ──────────────────────────────────────────────────────────────────────────

class TestModel3DLOD:
    """Model3DNode 解析 LOD 等级 + 附 lod_mesh。"""

    def test_node_has_lod_fields(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode.__init__)
        assert "_mesh_lod_variants" in src
        assert "_current_lod_level" in src

    def test_resolve_lod_level_method_exists(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        assert hasattr(Model3DNode, "_resolve_lod_level")

    def test_resolve_lod_full_export_zero(self):
        """合成输出（full/export）→ LOD0。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}

        class FakeCtx:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": True}
        assert node._resolve_lod_level(FakeCtx(), "full", []) == 0

    def test_resolve_lod_not_interacting_zero(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}

        class FakeCtx:
            interaction_active = False
            global_3d_scene_settings = {}
        assert node._resolve_lod_level(FakeCtx(), "preview", []) == 0

    def test_resolve_lod_large_model_nav2(self):
        """导航态 + 大模型（>=500k 三角）→ LOD2。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}

        class FakeCtx:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": True}
        # 600k 三角 = 1.8M indices
        big = [{"index_count": 1_800_000}]
        assert node._resolve_lod_level(FakeCtx(), "preview", big) == 2

    def test_resolve_lod_medium_model_nav1(self):
        """导航态 + 中模型（100k~500k）→ LOD1。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}

        class FakeCtx:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": True}
        # 200k 三角
        med = [{"index_count": 600_000}]
        assert node._resolve_lod_level(FakeCtx(), "preview", med) == 1

    def test_resolve_lod_disabled_setting_zero(self):
        """interactive_lod=False → 强制 LOD0。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}

        class FakeCtx:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": False}
        big = [{"index_count": 1_800_000}]
        assert node._resolve_lod_level(FakeCtx(), "preview", big) == 0

    def test_do_evaluate_calls_resolve_and_apply(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        assert "_resolve_lod_level" in src
        assert "_apply_lod_to_flat_meshes" in src
        assert "_current_lod_level" in src

    def test_ensure_model_clears_lod_variants(self):
        """模型变化时清空 LOD 变体缓存。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._ensure_model)
        assert "_mesh_lod_variants = {}" in src

    def test_textured_material_skips_lod_mesh(self):
        """带贴图材质的 mesh 不挂 LOD 代理，避免导航时 UV 聚类导致贴图花。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        from src.features.composite.render3d.scene_graph import (
            MaterialData, TextureDescriptor, TEXTURE_ROLE_COLOR)

        node = Model3DNode.__new__(Model3DNode)
        node._mesh_lod_variants = {}
        material = MaterialData(
            base_color_tex=TextureDescriptor(
                path="/tmp/fake_base_color.png", role=TEXTURE_ROLE_COLOR)
        )
        mesh = _make_grid_mesh(40, 40)
        flat_meshes = [{
            "mesh": mesh,
            "material": material,
            "is_skinned": False,
            "index_count": int(mesh.indices.shape[0]),
        }]

        result = node._apply_lod_to_flat_meshes(flat_meshes, 2)

        assert "lod_mesh" not in result[0]
        assert node._mesh_lod_variants == {}


# ──────────────────────────────────────────────────────────────────────────
# Part 5: Scene3D LOD VAO（源码级）
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DLODVAO:
    """Scene3D _get_lod_vao + 导航态用 LOD mesh 渲染。"""

    def test_get_lod_vao_method_exists(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert hasattr(Scene3DNode, "_get_lod_vao")

    def test_init_has_lod_vao_cache(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "_lod_vao_cache" in src

    def test_do_evaluate_uses_lod_in_nav(self):
        """_do_evaluate 导航态用 _get_lod_vao 渲染 LOD mesh。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_get_lod_vao" in src
        assert "m['lod_mesh']" in src
        assert "_lod_vao.render" in src

    def test_lod_tris_counted(self):
        """LOD draw 的三角形数计入 triangles_submitted。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_pstats.triangles_submitted += _lod_tris" in src

    def test_lod_level_in_stats(self):
        """用 LOD mesh 时 _pstats.lod_level 标记。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_pstats.lod_level = max(_pstats.lod_level, 2)" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
