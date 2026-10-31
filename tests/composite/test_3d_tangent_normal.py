"""F5 Tangent / Normal Map 单测（master_plan §8 / §21.8）。

覆盖：
1. MeshData.compute_tangents / interleaved_with_tangent：切线计算与持久化。
2. loader _precompute_tangents_for_normal_maps：为带 normal map 的 mesh 算切线。
3. MeshVBOPool has_tangent 维度：tangent 与非 tangent VBO 独立缓存。
4. model3d_node VAO 构建：has_tangent mesh 用 3f 3f 2f 3f layout（源码级）。

纯 numpy + dataclass，无 GL 依赖（VBO 池用 mock ctx）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import numpy as np
import pytest

from src.features.composite.render3d import primitives as prim
from src.features.composite.render3d import scene_graph as sg
from src.features.composite.render3d.mesh_vbo_pool import MeshVBOPool, _VBOEntry

pytestmark = pytest.mark.composite_core


# ── MeshData tangent 计算 ─────────────────────────────────────────────

class TestMeshDataTangents:
    def test_compute_tangents_shape(self):
        """cube 计算切线后得 (24,4)。"""
        cube = prim.cube(1.0)
        assert not cube.has_tangents
        tan = cube.compute_tangents()
        assert tan.shape == (24, 4)
        assert cube.has_tangents

    def test_tangents_cached(self):
        """重复调用返回缓存（同一对象）。"""
        cube = prim.cube(1.0)
        t1 = cube.compute_tangents()
        t2 = cube.compute_tangents()
        assert t1 is t2  # 缓存命中（同一 numpy 数组对象）

    def test_interleaved_with_tangent_shape(self):
        """含切线交错数组 11 float/顶点。"""
        cube = prim.cube(1.0)
        buf = cube.interleaved_with_tangent()
        assert buf.shape == (24, 11)

    def test_no_uv_returns_zero_tangent(self):
        """无 UV 的 mesh → xyz 零切线（w handedness 可非零，不崩）。"""
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        norms = np.zeros_like(verts)
        uvs = np.zeros((3, 2), dtype=np.float32)  # 全零 UV
        idx = np.array([0, 1, 2], dtype=np.uint32)
        m = prim.MeshData(verts, norms, uvs, idx)
        tan = m.compute_tangents()
        assert tan.shape == (3, 4)
        # xyz 分量应为零（无 UV 无法算切线方向）；w 是 handedness 符号（可非零）
        assert np.allclose(tan[:, :3], 0.0)


# ── loader 切线预计算 ────────────────────────────────────────────────

class TestPrecomputeTangents:
    def test_precompute_for_normal_map_mesh(self):
        """带 normal map 的 mesh 应被预计算切线。"""
        from src.features.composite.render3d import model_loader
        cube = prim.cube(1.0)
        mat = sg.MaterialData()
        mat.normal_tex.path = "/fake/normal.png"
        node = sg.SceneNode(name="m", mesh=cube, material=mat)
        root = sg.SceneGraphRoot(root=node)
        count = model_loader._precompute_tangents_for_normal_maps(root)
        assert count == 1
        assert cube.has_tangents

    def test_skip_mesh_without_normal_map(self):
        """无 normal map 的 mesh 不预计算切线。"""
        from src.features.composite.render3d import model_loader
        cube = prim.cube(1.0)
        mat = sg.MaterialData()  # 无 normal_tex.path
        node = sg.SceneNode(name="m", mesh=cube, material=mat)
        root = sg.SceneGraphRoot(root=node)
        count = model_loader._precompute_tangents_for_normal_maps(root)
        assert count == 0
        assert not cube.has_tangents

    def test_skip_mesh_without_material(self):
        """无材质的 mesh 不预计算。"""
        from src.features.composite.render3d import model_loader
        cube = prim.cube(1.0)
        node = sg.SceneNode(name="m", mesh=cube, material=None)
        root = sg.SceneGraphRoot(root=node)
        count = model_loader._precompute_tangents_for_normal_maps(root)
        assert count == 0

    def test_load_model_does_not_precompute_tangents(self):
        """回归：load_model 不应在导入阶段预计算切线（lazy 策略，避免大模型卡顿）。

        根因：F5 早期版本在 load_model 里调用 _precompute_tangents_for_normal_maps，
        对每个带 normal map 的 mesh 逐三角形 Python 循环算切线，65K 面 mesh 要 2.8s，
        导致 FBX 模型导入卡顿。修复后切线推迟到 VAO 构建时按需算（向量化 20ms）。
        """
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader.load_model)
        assert "_precompute_tangents_for_normal_maps" not in src
        assert "lazy" in src.lower() or "按需" in src or "VAO" in src


# ── MeshVBOPool has_tangent 维度 ─────────────────────────────────────

class _MockGLContext:
    """最小化 mock：buffer() 返回占位对象，能被 release。"""
    def __init__(self):
        self._id = id(self)
    def buffer(self, data):
        return _MockBuffer()


class _MockBuffer:
    def __init__(self):
        self.released = False
    def release(self):
        self.released = True


class TestVBOPoolTangent:
    def setup_method(self):
        """每个测试前清空进程级 _pools（避免跨测试 mock ctx id 冲突）。"""
        MeshVBOPool._pools.clear()

    def test_tangent_and_non_tangent_separate_entries(self):
        """同一 mesh 的 tangent 与非 tangent 版本应独立缓存。"""
        cube = prim.cube(1.0)
        cube.compute_tangents()
        ctx = _MockGLContext()
        pool = MeshVBOPool.for_context(ctx)
        vbo1, _, _ = pool.acquire(ctx, cube, has_tangent=False)
        vbo2, _, _ = pool.acquire(ctx, cube, has_tangent=True)
        # 两个不同的 VBO 对象（不同 layout 数据）
        assert vbo1 is not vbo2
        # 两个独立的缓存条目
        assert len(pool._entries) == 2

    def test_tangent_cache_hit(self):
        """同 mesh + has_tangent=True 两次 acquire → 复用同一 VBO。"""
        cube = prim.cube(1.0)
        cube.compute_tangents()
        ctx = _MockGLContext()
        pool = MeshVBOPool.for_context(ctx)
        vbo1, _, _ = pool.acquire(ctx, cube, has_tangent=True)
        vbo2, _, _ = pool.acquire(ctx, cube, has_tangent=True)
        assert vbo1 is vbo2
        assert pool.last_was_cached is True

    def test_release_with_tangent_flag(self):
        """release 需带 has_tangent 才能匹配缓存条目。"""
        cube = prim.cube(1.0)
        cube.compute_tangents()
        ctx = _MockGLContext()
        pool = MeshVBOPool.for_context(ctx)
        pool.acquire(ctx, cube, has_tangent=True)
        # 用错误 flag release 不应删条目
        pool.release(cube.content_hash(), False, has_tangent=False)
        assert len(pool._entries) == 1  # tangent 条目仍在
        # 正确 flag release 删除
        pool.release(cube.content_hash(), False, has_tangent=True)
        assert len(pool._entries) == 0

    def test_vbo_entry_has_tangent_field(self):
        """_VBOEntry 新增 has_tangent 字段。"""
        e = _VBOEntry(None, None, 0, False, has_tangent=True)
        assert e.has_tangent is True
        e2 = _VBOEntry(None, None, 0, False)
        assert e2.has_tangent is False


# ── model3d_node VAO 构建（源码级）───────────────────────────────────

class TestModel3DVAOTangentLayout:
    def test_ensure_flat_meshes_gl_supports_tangent(self):
        """_ensure_flat_meshes_gl 应支持 tangent layout（has_tangent 分支）。"""
        from src.features.composite.nodes import model3d_node
        src = inspect.getsource(model3d_node.Model3DNode._ensure_flat_meshes_gl)
        assert "has_tangents" in src or "has_tangent" in src
        # P2-3c（2026-10-28）：tangent 顶点字节改为经 MeshVBOPool 构建
        # （_pool.acquire(ctx, mesh, has_tangent=has_tangent) 内部调用
        # mesh.interleaved_with_tangent()），原先本地 data = ...tobytes()
        # 的死赋值已删除，源码 pin 同步改为断言 pool 的 has_tangent 接线。
        assert "has_tangent=has_tangent" in src
        assert "in_tangent" in src

    def test_flat_mesh_dict_has_tangent_flag(self):
        """flat_mesh dict 应含 has_tangent 标记。"""
        from src.features.composite.nodes import model3d_node
        src = inspect.getsource(model3d_node.Model3DNode._ensure_flat_meshes_gl)
        assert "'has_tangent'" in src

    def test_tangent_mesh_uses_tangent_program(self):
        """回归：tangent mesh 必须用 _ensure_mesh_tangent_program（含 in_tangent 属性的 shader）。

        根因：F5 VAO 构建给 tangent mesh 用了 '3f 3f 2f 3f' layout + in_tangent attr，
        但若仍用 _ensure_mesh_program（_MESH_VERTEX 无 in_tangent），ModernGL
        vertex_array 会报 KeyError: 'in_tangent' 导致 3D 模式卡死。
        """
        from src.features.composite.nodes import model3d_node
        src = inspect.getsource(model3d_node.Model3DNode._ensure_flat_meshes_gl)
        # tangent 分支必须调用 tangent 专用 program
        assert "_ensure_mesh_tangent_program" in src

    def test_vao_build_lazy_computes_tangents(self):
        """VAO 构建应按需计算切线（材质含 normal map 时），不在导入阶段预计算。"""
        from src.features.composite.nodes import model3d_node
        src = inspect.getsource(model3d_node.Model3DNode._ensure_flat_meshes_gl)
        # 检测材质 normal_tex.path，命中则 compute_tangents
        assert "normal_tex" in src
        assert "compute_tangents" in src

    def test_mesh_tangent_program_method_exists(self):
        """Model3DNode 应有 _ensure_mesh_tangent_program 方法（用 _MESH_VERTEX_TANGENT）。"""
        from src.features.composite.nodes import model3d_node
        assert hasattr(model3d_node.Model3DNode, "_ensure_mesh_tangent_program")
        src = inspect.getsource(model3d_node.Model3DNode._ensure_mesh_tangent_program)
        assert "_MESH_VERTEX_TANGENT" in src

    def test_tangent_program_released_on_cleanup(self):
        """cleanup 应释放 _mesh_tangent_prog（防泄漏）。"""
        from src.features.composite.nodes import model3d_node
        src = inspect.getsource(model3d_node.Model3DNode.cleanup)
        assert "_mesh_tangent_prog" in src
