"""EF-11 批次审计修复验证（AUD-8607/8710/88020/88030/88040/88055/
8904/8911/8915/9011/9012/9015/9100/9105/9120/9204）。

范围：src/features/composite/render3d 的 16 条 Suggestion 级发现。
原则：
- 每条修复都有正例（新行为生效）+ 反例（回归/回退时失败）；
- 不创建真实 GL 上下文、不弹 Qt 对话框（GL 路径全部用 mock/stub）；
- 纯逻辑路径直接驱动。

计划：docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.features.composite.render3d import aov
from src.features.composite.render3d import math_utils as m3d
from src.features.composite.render3d import material_model as mm
from src.features.composite.render3d import primitives as prim
from src.features.composite.render3d import scene_graph as sg
from src.features.composite.render3d.aov import IDAllocator
from src.features.composite.render3d.deformers import (
    DeformerSpec, evaluate_deformer, DEFORMER_BEND,
)
from src.features.composite.render3d import deformers as deformers_pkg
from src.features.composite.render3d.frustum import aabb_screen_size
from src.features.composite.render3d.gizmo_overlay import TransformGizmo, GizmoMode
from src.features.composite.render3d.gizmo_renderer import GizmoRenderer
from src.features.composite.render3d.hdri import HDRIBaker
from src.features.composite.render3d.math_utils import spot_intensity
from src.features.composite.render3d.mesh_picking import (
    pick_face, ray_triangle_intersect, RayHit, _EPSILON,
)
from src.features.composite.render3d.model_loader import (
    _convert_trimesh_material, _convert_assimp_material,
    _default_material_data, _extract_trimesh_skeletons,
    _resample_gltf_track,
)
from src.features.composite.render3d.node_icon_overlay import NodeIconRenderer
from src.features.composite.render3d.perf_stats import Viewport3DPerfStats
from src.features.composite.render3d.scene_hierarchy import compute_hierarchy_stats
from src.features.composite.render3d.scene_payload import GeometryPayload

pytestmark = pytest.mark.composite_core


# ──────────────────────────────────────────────────────────────────────
# AUD-8607: IDAllocator docstring 收窄「跨会话一致」承诺
# ──────────────────────────────────────────────────────────────────────

class TestAud8607IdAllocatorDoc:
    def test_doc_no_longer_promises_cross_session(self):
        """反例（回退敏感）：docstring 不得再承诺「跨会话一致」。"""
        doc = IDAllocator.__doc__ or ""
        assert "跨会话一致" not in doc
        assert "同一分配器" in doc          # 新文档明确稳定性范围

    def test_same_session_stable(self):
        """正例：同一分配器内同名同 id、异名异 id（行为保持）。"""
        alloc = IDAllocator()
        assert alloc.allocate("cube") == alloc.allocate("cube")
        assert alloc.allocate("sphere") != alloc.allocate("cube")
        assert alloc.get_id("sphere") is not None
        assert alloc.count() == 2

    def test_order_dependence_documented_by_reset(self):
        """正例：reset 后重新分配 → id 重新从 1 开始（顺序敏感的事实）。"""
        alloc = IDAllocator()
        first = alloc.allocate("a")
        alloc.reset()
        assert alloc.allocate("b") == first  # 顺序变 → 同 id 给了不同对象


# ──────────────────────────────────────────────────────────────────────
# AUD-8710: deformers 包导出与文档一致；未实现常量不再导出
# ──────────────────────────────────────────────────────────────────────

class TestAud8710DeformersExports:
    def test_unimplemented_constants_not_exported(self):
        """反例（回退敏感）：SHEAR/SPHERIFY 无实现，不再从包导出。"""
        assert not hasattr(deformers_pkg, "DEFORMER_SHEAR")
        assert not hasattr(deformers_pkg, "DEFORMER_SPHERIFY")
        assert "DEFORMER_SHEAR" not in deformers_pkg.__all__
        assert "DEFORMER_SPHERIFY" not in deformers_pkg.__all__

    def test_docstring_matches_exports(self):
        """反例（回退敏感）：「公开接口」段不再把 FieldSpec/falloff 列为导出。"""
        doc = deformers_pkg.__doc__ or ""
        # 旧 docstring 的原句（把 fields.py 的接口列进本包公开接口）已删除
        assert "FieldSpec / linear_field / radial_field / box_field" not in doc
        assert "fields.py）" not in doc
        # 常量清单与 __all__ 一致
        for const in ("DEFORMER_BEND", "DEFORMER_STRETCH", "DEFORMER_NOISE"):
            assert const in deformers_pkg.__all__

    def test_implemented_deformer_still_works(self):
        """正例：已实现类型照常工作（导出收敛不影响功能）。"""
        spec = DeformerSpec(deformer_type=DEFORMER_BEND, enabled=True,
                            strength=1.0)
        verts = np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0]], dtype=np.float32)
        normals = np.tile([0.0, 0.0, 1.0], (3, 1)).astype(np.float32)
        out_v, out_n = evaluate_deformer(spec, verts, normals)
        assert out_v.shape == (3, 3)
        assert np.isfinite(out_v).all() and np.isfinite(out_n).all()


# ──────────────────────────────────────────────────────────────────────
# AUD-88020: aabb_screen_size 死注释「<0 表示在相机后」
# ──────────────────────────────────────────────────────────────────────

class TestAud88020AabbScreenSize:
    def test_doc_no_negative_claim(self):
        """反例（回退敏感）：docstring 不再承诺 <0 语义。"""
        doc = aabb_screen_size.__doc__ or ""
        assert "<0 表示" not in doc
        assert "恒 >= 0" in doc

    def test_never_negative_behind_camera(self):
        """正例：任意几何配置（含 AABB 在相机一侧）返回值恒 >= 0。"""
        bmin = np.array([-1.0, -1.0, 10.0])
        bmax = np.array([1.0, 1.0, 12.0])
        for cam in (np.array([0.0, 0.0, 0.0]), np.array([0.0, 0.0, 100.0]),
                    np.array([5.0, -3.0, 11.0])):
            val = aabb_screen_size(bmin, bmax, cam, 600)
            assert val >= 0.0

    def test_degenerate_and_inside_still_hold(self):
        """正例：退化 AABB → 0；相机在物体内 → 满屏（原语义保持）。"""
        zero = aabb_screen_size(np.zeros(3), np.zeros(3), np.zeros(3), 600)
        assert zero == 0.0
        inside = aabb_screen_size(
            np.array([-1.0] * 3), np.array([1.0] * 3), np.zeros(3), 600)
        assert inside == 600.0


# ──────────────────────────────────────────────────────────────────────
# AUD-88030: TransformGizmo 在 update() 之前调用的防御缺口
# ──────────────────────────────────────────────────────────────────────

def _make_gizmo() -> TransformGizmo:
    return TransformGizmo()


class TestAud88030GizmoDefensive:
    def test_build_draw_list_before_update_empty(self):
        """反例（回退敏感）：update 前 build_draw_list 返回空列表不崩溃。"""
        for mode in (GizmoMode.MOVE, GizmoMode.ROTATE, GizmoMode.SCALE):
            g = _make_gizmo()
            g.mode = mode
            assert g.build_draw_list() == []

    def test_begin_drag_before_update_no_crash(self):
        """反例（回退敏感）：update 前 begin_drag 不抛 None@None。"""
        g = _make_gizmo()
        g.begin_drag("x", (0.1, 0.2))       # 旧实现此处 TypeError
        assert g.active_axis == "x"
        g.begin_drag("xy", (0.1, 0.2))      # 平面手柄分支同样安全
        assert g.active_axis == "xy"
        g.end_drag()
        assert g.active_axis is None

    def test_world_to_tex_ndc_before_update_safe(self):
        """反例（回退敏感）：update 前 _world_to_tex_ndc 返回 (0,0)。"""
        g = _make_gizmo()
        assert g._world_to_tex_ndc(np.array([1.0, 2.0, 3.0])) == (0.0, 0.0)
        assert g._projection_ready() is False

    def test_after_update_still_works(self):
        """正例：正常 update 后绘制列表非空（守卫不拦截正常流程）。"""
        g = _make_gizmo()
        g.update(np.array([0.0, 0.0, 0.0]), np.eye(4), np.eye(4),
                 512, 512, 5.0, 50.0)
        assert g._projection_ready() is True
        draws = g.build_draw_list()
        assert len(draws) > 0
        g.begin_drag("x", (0.1, 0.2))
        assert hasattr(g, "_drag_axis_start_t")


# ──────────────────────────────────────────────────────────────────────
# AUD-88040: GizmoRenderer._build 异常安全 + ctx 变化检测（mock GL）
# ──────────────────────────────────────────────────────────────────────

def _make_fake_ctx(fail_second_program: bool = False):
    """mock ModernGL context：program/buffer/vertex_array 全部可追踪。"""
    ctx = MagicMock(name="ctx")
    prog_mocks = []

    def _program(**kwargs):
        idx = len(prog_mocks)
        if fail_second_program and idx == 1:
            raise RuntimeError("axes program compile fail")
        prog = MagicMock(name=f"prog{idx}")
        prog_mocks.append(prog)
        return prog

    ctx.program.side_effect = _program
    ctx.buffer.side_effect = lambda data: MagicMock(name="vbo")
    ctx.vertex_array.side_effect = lambda prog, fmt: MagicMock(name="vao")
    ctx._prog_mocks = prog_mocks
    return ctx


class TestAud88040GizmoRendererBuild:
    def test_build_failure_releases_partial_objects(self):
        """反例（回退敏感）：第二个 program 失败 → 已建 GL 对象立即释放。"""
        r = GizmoRenderer()
        ctx = _make_fake_ctx(fail_second_program=True)
        with pytest.raises(RuntimeError):
            r._build(ctx)
        assert r._built is False
        # 网格段已创建的 3 个对象全部被 release，属性复位 None
        grid_prog = ctx._prog_mocks[0]
        assert grid_prog.release.called
        assert r._grid_prog is None
        assert r._grid_vao is None
        assert r._grid_vbo is None
        assert r._axes_prog is None
        assert r._ctx is None

    def test_ctx_change_triggers_release_and_rebuild(self):
        """反例（回退敏感）：换 ctx 后 _build 先 release 旧资源再重建。"""
        r = GizmoRenderer()
        ctx1 = _make_fake_ctx()
        r._build(ctx1)
        assert r._built is True
        old_progs = list(ctx1._prog_mocks)
        ctx2 = _make_fake_ctx()
        r._build(ctx2)
        for prog in old_progs:
            assert prog.release.called          # 旧 context 资源被释放
        assert r._ctx is ctx2
        assert len(ctx2._prog_mocks) == 2       # 用新 ctx 完整重建

    def test_same_ctx_no_rebuild(self):
        """正例：同 ctx 重复 _build 是 no-op（不重复编 program）。"""
        r = GizmoRenderer()
        ctx = _make_fake_ctx()
        r._build(ctx)
        r._build(ctx)
        assert len(ctx._prog_mocks) == 2
        assert r._built is True

    def test_release_resets_ctx_binding(self):
        """正例：release() 后 _ctx 复位，下次 draw 用新 ctx 重建。"""
        r = GizmoRenderer()
        ctx = _make_fake_ctx()
        r._build(ctx)
        r.release()
        assert r._built is False
        assert r._ctx is None


# ──────────────────────────────────────────────────────────────────────
# AUD-88055: HDRI bake 保存/恢复 viewport+FBO；缓存键文档修正（mock GL）
# ──────────────────────────────────────────────────────────────────────

GL_VIEWPORT = 0x0BA2
GL_FRAMEBUFFER_BINDING = 0x8CA6
GL_FRAMEBUFFER = 0x8DE6
GL_NO_ERROR = 0


class _GlRecorder:
    """记录 raw GL 调用顺序的 stub（代替 OpenGL.GL 模块）。

    glGetIntegerv 按调用序返回：第 1 次 = GL_VIEWPORT，第 2 次 = FBO 绑定
    （与 _bake_from_array 的调用顺序一一对应，避免依赖 GL 常量值）。
    """

    def __init__(self):
        self.events: list = []
        self.viewport_value = (0, 0, 800, 600)
        self.fbo_value = 7
        self._int_calls = 0

    def glGetIntegerv(self, pname):
        self.events.append("GetIntegerv")
        self._int_calls += 1
        if self._int_calls == 1:
            return np.array(self.viewport_value)
        return self.fbo_value

    def glBindFramebuffer(self, target, fbo):
        self.events.append(("BindFramebuffer", target, fbo))

    def glViewport(self, x, y, w, h):
        self.events.append(("Viewport", x, y, w, h))

    def glGetError(self):
        return GL_NO_ERROR

    def __getattr__(self, name):
        # 其余 GL 符号（GL_* 常量 + release 里可能用到的函数）
        if name.isupper():
            return 1
        return MagicMock(name=f"gl.{name}")


def _patch_baker_core(baker: HDRIBaker, rec: _GlRecorder):
    """把 _bake_from_array 依赖的重活替换为 mock，返回还原器。"""
    env_tex = MagicMock(name="env_tex")
    patchers = [
        patch.object(baker, "release", side_effect=lambda: rec.events.append("release")),
        patch.object(baker, "_upload_equirect", return_value=env_tex),
        patch.object(baker, "_render_env_cubemap", return_value=MagicMock()),
        patch.object(baker, "_convolve_irradiance", return_value=MagicMock()),
        patch.object(baker, "_convolve_prefilter", return_value=MagicMock()),
        patch.object(baker, "_integrate_brdf_lut", return_value=MagicMock()),
    ]
    for p in patchers:
        p.start()
    def _stop():
        for p in patchers:
            p.stop()
    return _stop


class TestAud88055HdriBakeState:
    @staticmethod
    def _make_recorder() -> _GlRecorder:
        rec = _GlRecorder()
        # GL 常量透传（源码里 gl.GL_FRAMEBUFFER 等经 mock 取到真实值）
        rec.GL_VIEWPORT = GL_VIEWPORT
        rec.GL_FRAMEBUFFER_BINDING = GL_FRAMEBUFFER_BINDING
        rec.GL_FRAMEBUFFER = GL_FRAMEBUFFER
        rec.GL_NO_ERROR = GL_NO_ERROR
        return rec

    def test_bake_restores_viewport_and_fbo(self):
        """正例：bake 结束后恢复进入时的 viewport 与 FBO 绑定。"""
        baker = HDRIBaker()
        rec = self._make_recorder()
        stop = _patch_baker_core(baker, rec)
        try:
            with patch("src.features.composite.render3d.hdri.gl", rec):
                equirect = np.zeros((8, 16, 3), dtype=np.float32)
                baker._bake_from_array(MagicMock(), equirect,
                                       ("k", 0.0, 128, 64), label="t")
        finally:
            stop()
        # 进入时先保存状态，再释放旧资源
        assert rec.events[0] == "GetIntegerv"
        assert rec.events[1] == "GetIntegerv"
        assert rec.events[2] == "release"
        # 结束时恢复 FBO + viewport（保存的值）
        assert rec.events[-2] == ("BindFramebuffer", GL_FRAMEBUFFER, 7)
        assert rec.events[-1] == ("Viewport", 0, 0, 800, 600)

    def test_bake_restores_state_on_exception(self):
        """反例（回退敏感）：中途抛错也必须恢复 GL 状态（finally）。"""
        baker = HDRIBaker()
        rec = self._make_recorder()
        stop = _patch_baker_core(baker, rec)
        try:
            with patch("src.features.composite.render3d.hdri.gl", rec):
                baker._render_env_cubemap.side_effect = RuntimeError("boom")
                with pytest.raises(RuntimeError):
                    baker._bake_from_array(
                        MagicMock(), np.zeros((8, 16, 3), dtype=np.float32),
                        ("k", 0.0, 128, 64), label="t")
        finally:
            stop()
        assert rec.events[-2] == ("BindFramebuffer", GL_FRAMEBUFFER, 7)
        assert rec.events[-1] == ("Viewport", 0, 0, 800, 600)

    def test_cache_key_doc_matches_implementation(self):
        """反例（回退敏感）：缓存键文档与实现一致（配置常量，非输入分辨率）。"""
        doc = HDRIBaker.bake.__doc__ or ""
        assert "bake 配置常量" in doc
        assert "分辨率, 采样数" not in doc


# ──────────────────────────────────────────────────────────────────────
# AUD-8904: material_summary 的 os.path.isfile 磁盘 IO → TTL 缓存
# ──────────────────────────────────────────────────────────────────────

def _make_mat_with_slots(path: str):
    mat = SimpleNamespace()
    for slot_attr in ("base_color_slot", "normal_slot", "orm_slot",
                      "ao_slot", "emissive_slot"):
        setattr(mat, slot_attr, mm.TextureSlot(path=path))
    return mat


class TestAud8904MaterialSummaryStatCache:
    def test_isfile_cached_across_calls(self):
        """反例（回退敏感）：同一路径连续两次 summary 只 stat 一次磁盘。"""
        calls = {"n": 0}

        def counting_isfile(p):
            calls["n"] += 1
            return False

        mat = _make_mat_with_slots("/nonexistent_dir/tex.png")
        mm.clear_texture_stat_cache()
        try:
            with patch.object(mm.os.path, "isfile", side_effect=counting_isfile):
                s1 = mm.material_summary(mat)
                s2 = mm.material_summary(mat)
        finally:
            mm.clear_texture_stat_cache()
        assert s1["slots"]["base_color"]["missing"] is True
        assert s2["slots"]["base_color"]["missing"] is True
        assert calls["n"] == 1, f"expected 1 stat call, got {calls['n']}"

    def test_cache_clear_forces_restale(self):
        """正例：清缓存（或 TTL 过期语义）后重新 stat。"""
        calls = {"n": 0}

        def counting_isfile(p):
            calls["n"] += 1
            return False

        mat = _make_mat_with_slots("/nonexistent_dir/tex.png")
        mm.clear_texture_stat_cache()
        try:
            with patch.object(mm.os.path, "isfile", side_effect=counting_isfile):
                mm.material_summary(mat)
                mm.clear_texture_stat_cache()
                mm.material_summary(mat)
        finally:
            mm.clear_texture_stat_cache()
        assert calls["n"] == 2

    def test_empty_path_no_io(self):
        """正例：空路径不触发 stat，missing=False。"""
        calls = {"n": 0}

        def counting_isfile(p):
            calls["n"] += 1
            return True

        mat = _make_mat_with_slots("")
        mm.clear_texture_stat_cache()
        try:
            with patch.object(mm.os.path, "isfile", side_effect=counting_isfile):
                s = mm.material_summary(mat)
        finally:
            mm.clear_texture_stat_cache()
        assert s["slots"]["base_color"]["missing"] is False
        assert calls["n"] == 0

    def test_existing_file_not_missing(self):
        """正例：存在的文件 missing=False。"""
        mat = _make_mat_with_slots(__file__)
        mm.clear_texture_stat_cache()
        try:
            s = mm.material_summary(mat)
        finally:
            mm.clear_texture_stat_cache()
        assert s["slots"]["base_color"]["missing"] is False


# ──────────────────────────────────────────────────────────────────────
# AUD-8911: spot_intensity docstring 与实现（线性斜坡）一致
# ──────────────────────────────────────────────────────────────────────

class TestAud8911SpotIntensityDoc:
    def test_doc_states_linear_not_smoothstep(self):
        """反例（回退敏感）：docstring 不再声称 smoothstep。"""
        doc = spot_intensity.__doc__ or ""
        assert "内外间 smoothstep" not in doc
        assert "线性" in doc

    def test_values_are_linear_ramp(self):
        """正例：数值为线性斜坡（1/4 处 = 0.25，smoothstep 会是 0.15625）。"""
        inner, outer = 1.0, 0.0
        assert spot_intensity(1.0, inner, outer) == pytest.approx(1.0)
        assert spot_intensity(0.0, inner, outer) == pytest.approx(0.0)
        assert spot_intensity(0.25, inner, outer) == pytest.approx(0.25)
        assert spot_intensity(0.75, inner, outer) == pytest.approx(0.75)

    def test_degenerate_cone(self):
        """正例：内外锥重合 → 阶跃（原语义保持）。"""
        assert spot_intensity(0.9999, 0.9999, 0.9999) == 1.0
        assert spot_intensity(0.5, 0.9999, 0.9999) == 0.0


# ──────────────────────────────────────────────────────────────────────
# AUD-8915: pick_face 向量化（消除 O(n) 纯 Python 循环）
# ──────────────────────────────────────────────────────────────────────

def _reference_pick_face(ray_origin, ray_dir, world_verts, indices,
                         max_distance=1e9):
    """旧实现的逐三角形参考版（验证向量化语义等价）。"""
    best_t, best_idx, best_u, best_v = max_distance, -1, 0.0, 0.0
    n_tris = len(indices) // 3
    for i in range(n_tris):
        v0, v1, v2 = (world_verts[int(indices[i * 3])],
                      world_verts[int(indices[i * 3 + 1])],
                      world_verts[int(indices[i * 3 + 2])])
        t, u, v = ray_triangle_intersect(ray_origin, ray_dir, v0, v1, v2)
        if 0.0 < t < best_t:
            best_t, best_idx, best_u, best_v = t, i, u, v
    return best_idx, best_t, best_u, best_v


class TestAud8915PickFaceVectorized:
    def test_no_per_triangle_python_loop(self):
        """反例（回退敏感）：实现不再含逐三角形 Python 循环。"""
        src = inspect.getsource(pick_face)
        assert "for i in range(n_tris)" not in src
        assert "ray_triangle_intersect(" not in src.replace(
            "def ray_triangle_intersect", "")
        # 向量化算子确实在场
        assert "np.cross" in src and "einsum" in src

    def test_hit_nearest_triangle(self):
        """正例：命中最近三角形 + 重心坐标/命中点正确。"""
        verts = np.array([
            [0, 0, 2], [1, 0, 2], [0, 1, 2],      # tri0 @ z=2
            [0, 0, 5], [1, 0, 5], [0, 1, 5],      # tri1 @ z=5
        ], dtype=np.float64)
        indices = np.array([0, 1, 2, 3, 4, 5])
        hit = pick_face(np.zeros(3), np.array([0.0, 0.0, 1.0]), verts, indices)
        assert hit.triangle_index == 0
        assert hit.distance == pytest.approx(2.0, abs=1e-6)
        assert hit.barycentric[0] == pytest.approx(1.0, abs=1e-6)
        assert hit.point[2] == pytest.approx(2.0, abs=1e-6)

    def test_matches_reference_on_random_mesh(self):
        """正例：随机网格上与逐三角形参考实现结果一致（索引 + 距离）。"""
        rng = np.random.default_rng(20261001)
        for _ in range(20):
            verts = rng.uniform(-5.0, 5.0, size=(30, 3))
            verts[:, 2] += 10.0                    # 全部在相机前方
            indices = np.arange(30)
            origin = np.array([0.0, 0.0, -5.0])
            direction = np.array([0.05, -0.03, 1.0])
            direction = direction / np.linalg.norm(direction)
            got = pick_face(origin, direction, verts, indices)
            exp_idx, exp_t, _, _ = _reference_pick_face(
                origin, direction, verts, indices)
            assert got.triangle_index == exp_idx
            if exp_idx >= 0:
                assert got.distance == pytest.approx(exp_t, rel=1e-9)

    def test_miss_and_max_distance_cutoff(self):
        """正例：未命中 / 超出 max_distance → triangle_index=-1。"""
        verts = np.array([[0, 0, 2], [1, 0, 2], [0, 1, 2]], dtype=np.float64)
        indices = np.array([0, 1, 2])
        miss = pick_face(np.zeros(3), np.array([0.0, 1.0, 0.0]), verts, indices)
        assert isinstance(miss, RayHit) and miss.triangle_index == -1
        far = pick_face(np.zeros(3), np.array([0.0, 0.0, 1.0]), verts, indices,
                        max_distance=1.0)
        assert far.triangle_index == -1

    def test_model_matrix_path(self):
        """正例：model_matrix 变换路径与参考实现一致。"""
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        indices = np.array([0, 1, 2])
        model = np.eye(4)
        model[2, 3] = 4.0                     # 平移到 z=4
        origin = np.zeros(3)
        direction = np.array([0.0, 0.0, 1.0])
        hit = pick_face(origin, direction, verts, indices, model_matrix=model)
        world_verts = verts.copy()
        world_verts[:, 2] += 4.0
        exp_idx, exp_t, _, _ = _reference_pick_face(
            origin, direction, world_verts.astype(np.float64), indices)
        assert hit.triangle_index == exp_idx == 0
        assert hit.distance == pytest.approx(exp_t, rel=1e-6)


# ──────────────────────────────────────────────────────────────────────
# AUD-9011: _extract_trimesh_skeletons O(n²) → set/dict 预建
# ──────────────────────────────────────────────────────────────────────

def _make_skin_scene(joints, hierarchy):
    skin = {"name": "skin0", "joints": list(joints),
            "hierarchy": dict(hierarchy)}
    return SimpleNamespace(metadata={"skins": [skin]})


class TestAud9011SkeletonTopoSort:
    def test_no_rebuilt_set_or_linear_index(self):
        """反例（回退敏感）：不再每节点重建全集 set / 线性 .index()。"""
        src = inspect.getsource(_extract_trimesh_skeletons)
        assert "{unique_names[i] for i in range(len(unique_names))}" not in src
        assert ".index(nm)" not in src
        assert "unique_set" in src and "raw_index_of" in src

    def test_topology_order_parents_first(self):
        """正例：乱序输入 → parent 下标恒小于 child 下标。"""
        joints = ["hand_l", "root", "arm_l", "shoulder_l"]
        hierarchy = {"hand_l": "arm_l", "arm_l": "shoulder_l",
                     "shoulder_l": "root"}
        scene = _make_skin_scene(joints, hierarchy)
        out = _extract_trimesh_skeletons(scene)
        assert len(out) == 1
        skel = out[0]
        assert skel.bone_count == 4
        names = [b.name for b in skel.bones]
        assert set(names) == set(joints)
        for bone in skel.bones:
            if bone.parent_index >= 0:
                assert bone.parent_index < bone.index
                assert skel.bones[bone.parent_index].name == hierarchy[bone.name]
            else:
                assert bone.name == "root"

    def test_dedup_and_deep_chain(self):
        """正例：重复骨骼名去重 + 长链拓扑（100 节点性能路径正确性）。"""
        n = 100
        joints = [f"j{i}" for i in range(n)]
        hierarchy = {f"j{i}": f"j{i-1}" for i in range(1, n)}
        scene = _make_skin_scene(joints, hierarchy)
        out = _extract_trimesh_skeletons(scene)
        assert len(out) == 1
        for bone in out[0].bones:
            if bone.parent_index >= 0:
                assert bone.parent_index < bone.index


# ──────────────────────────────────────────────────────────────────────
# AUD-9012: _resample_gltf_track 向量化
# ──────────────────────────────────────────────────────────────────────

def _reference_resample(times, values, sample_times):
    """旧实现的逐行参考版。"""
    values = np.asarray(values, dtype=np.float32).reshape(times.size, -1)
    output = np.empty((sample_times.size, values.shape[1]), dtype=np.float32)
    for row, sample_time in enumerate(sample_times):
        if sample_time <= times[0]:
            output[row] = values[0]
            continue
        if sample_time >= times[-1]:
            output[row] = values[-1]
            continue
        right = int(np.searchsorted(times, sample_time, side="right"))
        left = right - 1
        span = max(1.0e-9, float(times[right] - times[left]))
        alpha = float((sample_time - times[left]) / span)
        output[row] = values[left] * (1.0 - alpha) + values[right] * alpha
    return output


class TestAud9012ResampleTrack:
    def test_no_python_row_loop(self):
        """反例（回退敏感）：实现不再逐行 Python 循环。"""
        src = inspect.getsource(_resample_gltf_track)
        assert "for row, sample_time in enumerate" not in src
        assert "searchsorted" in src

    def test_matches_reference_random(self):
        """正例：随机输入与逐行参考实现数值一致。"""
        rng = np.random.default_rng(20261002)
        times = np.sort(rng.uniform(0.0, 10.0, size=50)).astype(np.float32)
        times = np.unique(times)
        values = rng.uniform(-1.0, 1.0, size=(times.size, 3)).astype(np.float32)
        sample_times = np.sort(rng.uniform(-1.0, 11.0, size=200)).astype(np.float32)
        got = _resample_gltf_track(times, values, sample_times,
                                   np.zeros(3, dtype=np.float32))
        exp = _reference_resample(times, values, sample_times)
        assert got.shape == exp.shape
        np.testing.assert_allclose(got, exp, rtol=1e-4, atol=1e-5)

    def test_clamp_at_both_ends(self):
        """正例：两端 clamp 语义保持。"""
        times = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        values = np.array([[1, 1, 1], [2, 2, 2], [3, 3, 3]], dtype=np.float32)
        st = np.array([0.0, 0.5, 3.5, 100.0], dtype=np.float32)
        out = _resample_gltf_track(times, values, st,
                                   np.zeros(3, dtype=np.float32))
        np.testing.assert_allclose(out[0], [1, 1, 1])
        np.testing.assert_allclose(out[1], [1, 1, 1])
        np.testing.assert_allclose(out[2], [3, 3, 3])
        np.testing.assert_allclose(out[3], [3, 3, 3])

    def test_empty_samples_and_single_key(self):
        """正例：空采样表 / 单关键帧边界。"""
        out = _resample_gltf_track(np.empty(0, np.float32),
                                   np.empty((0, 3), np.float32),
                                   np.array([0.0, 1.0], np.float32),
                                   np.array([7.0, 8.0, 9.0], np.float32))
        assert out.shape == (2, 3)
        np.testing.assert_allclose(out, [[7, 8, 9], [7, 8, 9]])

        out1 = _resample_gltf_track(np.array([1.0], np.float32),
                                    np.array([[5.0, 6.0, 7.0]], np.float32),
                                    np.array([0.0, 1.0, 2.0], np.float32),
                                    np.zeros(3, np.float32))
        np.testing.assert_allclose(out1, np.tile([5.0, 6.0, 7.0], (3, 1)))


# ──────────────────────────────────────────────────────────────────────
# AUD-9015: 材质转换失败 → 记 warning + 返回默认标量材质
# ──────────────────────────────────────────────────────────────────────

class _ExplodingMaterial:
    """baseColorFactor 属性抛错的 trimesh material 替身。"""

    @property
    def baseColorFactor(self):
        raise RuntimeError("corrupt baseColorFactor")


class TestAud9015MaterialFallback:
    def test_trimesh_failure_returns_default_material(self, caplog):
        """反例（回退敏感）：trimesh 材质转换抛错 → 默认标量材质而非 None。"""
        visual = SimpleNamespace(material=_ExplodingMaterial())
        import logging
        with caplog.at_level(logging.WARNING, logger="src.features.composite.render3d.model_loader"):
            mat = _convert_trimesh_material(visual, source_path="/m.glb",
                                            material_key="mat_a")
        assert mat is not None                       # 旧实现返回 None
        assert isinstance(mat, sg.MaterialData)
        assert mat.material_id == "mat_a"
        np.testing.assert_allclose(mat.base_color, [0.8, 0.8, 0.8], rtol=1e-6)
        assert mat.base_color_tex.path == ""         # 无纹理（纯标量 fallback）
        assert any("材质转换失败" in r.message for r in caplog.records)

    def test_assimp_scalar_diffuse_returns_default(self, caplog):
        """反例（回退敏感）：assimp props['diffuse'] 是标量 → 默认材质。"""
        mat_stub = SimpleNamespace(properties={"diffuse": 3.2})  # 标量 → [:3] 抛错
        import logging
        with caplog.at_level(logging.WARNING, logger="src.features.composite.render3d.model_loader"):
            mat = _convert_assimp_material(mat_stub, material_key="material_0")
        assert mat is not None
        assert mat.material_id == "material_0"
        assert any("材质转换失败" in r.message for r in caplog.records)

    def test_none_inputs_still_none(self):
        """正例：显式 None 输入仍返回 None（契约保持，不属异常路径）。"""
        assert _convert_trimesh_material(None) is None
        assert _convert_assimp_material(None) is None

    def test_default_material_helper(self):
        """正例：_default_material_data 产出 0.8 灰标量材质。"""
        d = _default_material_data("k")
        assert isinstance(d, sg.MaterialData)
        assert d.metallic == 0.0 and d.roughness == 0.5
        np.testing.assert_allclose(d.base_color, [0.8, 0.8, 0.8], rtol=1e-6)

    def test_wellformed_paths_unaffected(self):
        """正例：正常材质转换路径不受影响。"""
        good = SimpleNamespace(
            baseColorFactor=[0.2, 0.4, 0.6, 1.0], metallicFactor=0.3,
            roughnessFactor=0.7, name="good")
        visual = SimpleNamespace(material=good)
        mat = _convert_trimesh_material(visual, material_key="k")
        assert mat is not None
        np.testing.assert_allclose(mat.base_color, [0.2, 0.4, 0.6], rtol=1e-5)
        assert mat.metallic == pytest.approx(0.3)
        assert mat.roughness == pytest.approx(0.7)


# ──────────────────────────────────────────────────────────────────────
# AUD-9100: add_camera_icon near/far 与 add_light_icon range 存而不用
# ──────────────────────────────────────────────────────────────────────

class TestAud9100IconParamsDocumented:
    def test_camera_docstring_no_longer_claims_near_far_used(self):
        """反例（回退敏感）：camera docstring 明示 near/far 不参与几何。"""
        doc = NodeIconRenderer.add_camera_icon.__doc__ or ""
        assert "存档不参与" in doc
        assert "视锥延伸距离用" not in doc

    def test_light_docstring_no_longer_claims_range_used(self):
        """反例（回退敏感）：light docstring 明示 range 不参与几何。"""
        doc = NodeIconRenderer.add_light_icon.__doc__ or ""
        assert "存档不参与" in doc
        assert "图标尺寸参考" not in doc

    def test_params_still_stored_api_preserved(self):
        """正例：参数仍随图标注册存档（API 兼容）。"""
        r = NodeIconRenderer()
        r.add_camera_icon("cam1", (0, 0, 0), (0, 0, -1), 0.1, 100.0, 50.0, 1.5)
        r.add_light_icon("light1", "point", (1, 2, 3), (0, -1, 0),
                         (1.0, 0.9, 0.8), range=42.0)
        assert r._icons[0]["near"] == 0.1
        assert r._icons[0]["far"] == 100.0
        assert r._icons[1]["range"] == 42.0

    def test_geometry_independent_of_near_far_range(self):
        """正例：near/far/range 不影响图标几何（与文档一致）。"""
        r = NodeIconRenderer()
        proj = np.eye(4)
        view = np.eye(4)
        view[2, 3] = 10.0                     # 相机前移，让图标点落在 w>0 侧
        r.update(proj, view, 512, 512, 5.0, 50.0)
        r.add_camera_icon("cam", (0, 0, 0), (0, 0, -1), 0.1, 10.0, 50.0, 1.5)
        geom_small = r.build_draw_list()
        r.update(proj, view, 512, 512, 5.0, 50.0)
        r.add_camera_icon("cam", (0, 0, 0), (0, 0, -1), 5.0, 1000.0, 50.0, 1.5)
        geom_big = r.build_draw_list()
        assert len(geom_small) == len(geom_big) > 0
        for a, b in zip(geom_small, geom_big):
            np.testing.assert_allclose(np.asarray(a.points_uv, dtype=float),
                                       np.asarray(b.points_uv, dtype=float))


# ──────────────────────────────────────────────────────────────────────
# AUD-9105: Viewport3DPerfStats.reset() 复位上下文描述字段
# ──────────────────────────────────────────────────────────────────────

class TestAud9105PerfStatsReset:
    def test_reset_restores_context_fields(self):
        """反例（回退敏感）：reset 后 quality/layout/view 回默认值。"""
        stats = Viewport3DPerfStats()
        stats.quality_profile = "preview"
        stats.viewport_layout = "quad"
        stats.active_view = "top"
        stats.draw_calls = 42
        stats.frame_id = 7
        stats.reset(frame_id=8)
        assert stats.quality_profile == "final"
        assert stats.viewport_layout == "single"
        assert stats.active_view == "perspective"
        assert stats.frame_id == 8
        assert stats.draw_calls == 0

    def test_reset_counters_still_cleared(self):
        """正例：计数器清零语义保持。"""
        stats = Viewport3DPerfStats()
        stats.triangles_submitted = 12345
        stats.meshes_culled = 9
        stats.resolution = (1920, 1080)
        stats.reset()
        assert stats.triangles_submitted == 0
        assert stats.meshes_culled == 0
        assert stats.resolution == (0, 0)


# ──────────────────────────────────────────────────────────────────────
# AUD-9120: compute_hierarchy_stats 的死参 hierarchy
# ──────────────────────────────────────────────────────────────────────

class TestAud9120HierarchyStatsSignature:
    def test_dead_hierarchy_param_removed(self):
        """反例（回退敏感）：签名为 root_data 单参（死参已删）。"""
        params = inspect.signature(compute_hierarchy_stats).parameters
        assert "hierarchy" not in params
        assert list(params) == ["root_data"]

    def test_stats_still_correct(self):
        """正例：统计数值保持正确（mesh/tri/mat 去重/tex）。"""
        mat_a = sg.MaterialData()
        mat_a.name = "shared"
        mat_a.base_color_tex.path = "/bc.png"
        mat_b = sg.MaterialData()
        mat_b.name = "shared"                 # 同名 → 材质去重后 1
        root = sg.SceneNode(name="root")
        root.children.append(sg.SceneNode(name="m1", mesh=prim.cube(1.0),
                                          material=mat_a))
        root.children.append(sg.SceneNode(name="m2", mesh=prim.cube(2.0),
                                          material=mat_b))
        stats = compute_hierarchy_stats(sg.SceneGraphRoot(root=root))
        assert stats.mesh_count == 2
        assert stats.triangle_count == 24
        assert stats.material_count == 1          # 去重
        assert stats.texture_count == 1           # 只有 m1 带贴图


# ──────────────────────────────────────────────────────────────────────
# AUD-9204: GeometryPayload.signature 不再全量拷贝 instance_offsets
# ──────────────────────────────────────────────────────────────────────

class TestAud9204GeoSignature:
    def test_no_full_offsets_tobytes(self):
        """反例（回退敏感）：signature 不再对 instance_offsets astype+tobytes。"""
        src = inspect.getsource(GeometryPayload.signature)
        assert "instance_offsets.astype" not in src
        assert "instance_offsets.tobytes" not in src

    def test_signature_shape_and_revision_sensitivity(self):
        """正例：shape 变化 / revision 变化 → 签名变化。"""
        off_a = np.zeros((4, 3), dtype=np.float32)
        off_b = np.zeros((9, 3), dtype=np.float32)
        s1 = GeometryPayload(vao=None, model_matrix=np.eye(4),
                             instance_offsets=off_a, content_revision=1).signature()
        s2 = GeometryPayload(vao=None, model_matrix=np.eye(4),
                             instance_offsets=off_b, content_revision=1).signature()
        s3 = GeometryPayload(vao=None, model_matrix=np.eye(4),
                             instance_offsets=off_a, content_revision=2).signature()
        assert s1 != s2                       # 实例数（shape）变化可感知
        assert s1 != s3                       # 内容变化由 revision 捕获

    def test_signature_stable_and_hashable(self):
        """正例：同内容同签名且可哈希（进 content_sig 缓存键的前提）。"""
        off = np.arange(12, dtype=np.float32).reshape(4, 3)
        p1 = GeometryPayload(vao=None, model_matrix=np.eye(4),
                             instance_offsets=off, content_revision=5)
        p2 = GeometryPayload(vao=None, model_matrix=np.eye(4),
                             instance_offsets=off.copy(), content_revision=5)
        s1, s2 = p1.signature(), p2.signature()
        assert s1 == s2
        assert hash(s1) == hash(s2)
        assert s1[0] == "geo"

    def test_signature_none_offsets(self):
        """正例：instance_offsets=None 签名正常（非实例化路径）。"""
        sig = GeometryPayload(vao=None, model_matrix=np.eye(4)).signature()
        assert isinstance(sig, tuple)
        assert hash(sig) is not None
