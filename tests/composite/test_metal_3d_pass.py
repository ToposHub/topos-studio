"""阶段 5.5 第一步 · 最小 3D pass 的约定验证测试。

三层证据：

1. **CPU 约定层**（无 GPU）：用 ``render3d.math_utils`` 的
   perspective/look_at（GL 参考的矩阵源）驱动 ``rasterize_mesh_cpu``：
   NDC 中心投影、深度重映射 [0,1]、遮挡（LESS）、透视校正插值、
   top-left 图像行序。
2. **GL 参考对拍**（独立子进程，``run_standalone_gpu``）：同一网格用
   ``_MESH_VERTEX`` 同式的 GLSL（``u_proj * u_view * u_model``，列主序
   ``matrix_to_tuple`` 上传）渲染进带深度测试的 FBO，颜色缓冲与 CPU
   参考逐像素对拍（覆盖 + 插值 + 遮挡结局）。
3. **Metal 真机层**（无设备 skip）：``MetalMesh3DPass`` 与 CPU 参考
   对拍颜色（RGBA16F 容差）与深度（R32F）。

另含 Scene3D 映射评估的契约测试：3D 节点 → 显式「无法无歧义映射」
诊断（node.metal_3d_unmapped），AOV pass 因此显式回退。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.shared.gpu.metal import MetalComputeContext

from src.features.composite.render.metal.mesh3d import (
    FAR_DEPTH,
    MAX_TRIANGLES,
    evaluate_3d_mapping,
    rasterize_mesh_cpu,
)
from src.features.composite.render3d import math_utils as m3d

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备")

# 颜色容差：Metal 侧 RGBA16F（1 ulp @1.0 ≈ 4.9e-4），GL 侧亦 f2；
# 插值权重两端浮点精度差 < 1e-5。atol=3e-3 覆盖并保留裕量。
COLOR_TOL = 3e-3
# 深度容差：d∈[0,1]，两端 float32/float64 的重心插值差 < 1e-5；
# 约定性错误（如 z 重映射缺失）会造成 ~0.5 量级差异。
DEPTH_TOL = 1e-3

W, H = 96, 64


def _test_mesh() -> tuple:
    """三个三角形的确定性网格：背景大三角 + 两个部分遮挡的前景三角。

    顶点经过抖动，避免投影后顶点/边正好落在像素中心（边缘规则的
    浮点歧义）。所有顶点位于相机前方（近平面 0.1 之前）。
    """
    positions = np.array([
        # 背景大三角形（较远）。
        [-2.4, -1.6, -1.2], [2.6, -1.5, -1.4], [0.1, 2.2, -1.0],
        # 前景三角 1（近，遮挡背景的一部分）。
        [-0.73, -0.61, 1.35], [0.47, -0.58, 1.29], [-0.11, 0.66, 1.31],
        # 前景三角 2（更近，与三角 1 部分重叠）。
        [0.11, -0.42, 2.4], [1.02, -0.39, 2.35], [0.53, 0.44, 2.42],
    ], dtype=np.float32)
    colors = np.array([
        [0.10, 0.20, 0.30, 1.0], [0.10, 0.20, 0.30, 1.0],
        [0.10, 0.20, 0.30, 1.0],
        [0.90, 0.10, 0.10, 1.0], [0.10, 0.90, 0.10, 1.0],
        [0.10, 0.10, 0.90, 1.0],
        [0.95, 0.80, 0.10, 1.0], [0.80, 0.10, 0.90, 1.0],
        [0.10, 0.90, 0.90, 1.0],
    ], dtype=np.float32)
    return positions, colors


def _test_mvp() -> np.ndarray:
    """固定相机的 MVP（= P @ V @ M，列向量约定）。

    ``math_utils`` 的矩阵是列向量矩阵的行主序 numpy 存储；
    ``matrix_to_tuple`` 转置后按列主序上传，GLSL mat4 == numpy 矩阵本身。
    因此 GLSL 的 ``u_proj·u_view·u_model·vec4(pos,1)`` 等价于
    ``(P @ V @ M) @ pos``；本 pass 的 CPU 参考与 MSL kernel 消费同一份
    列主序 tuple（``matrix_to_tuple`` 布局）。
    """
    eye = np.array([0.4, 1.2, 4.2], dtype=np.float32)
    target = np.array([0.1, 0.0, 0.0], dtype=np.float32)
    up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    view = m3d.look_at(eye, target, up)
    proj = m3d.perspective(50.0, W / H, 0.1, 100.0)
    model = m3d.IDENTITY4.copy()
    model[0, 0] = 1.03   # 微非单位模型矩阵（验证 M 真正参与）
    return (proj.astype(np.float64) @ view.astype(np.float64)
            @ model.astype(np.float64))


# ── CPU 约定层 ──────────────────────────────────────────────────────────


class TestCpuRasterConventions:
    def setup_method(self):
        self.positions, self.colors = _test_mesh()
        self.mvp = _test_mvp()
        self.color, self.depth = rasterize_mesh_cpu(
            self.positions, self.colors, self.mvp, W, H)

    def test_coverage_is_partial_and_in_image_row_order(self):
        covered = self.depth < FAR_DEPTH
        assert 0.05 < covered.mean() < 0.95     # 覆盖既非空也非全屏
        # 行序：相机的 up 朝世界 +Y，视口上缘应对应图像第 0 行（top-left）。
        # 取世界坐标 (0.05, 1.0, 0.6)（画面上半）投影应落在图像上半。
        p = np.array([[0.05, 1.0, 0.6, 1.0]], dtype=np.float64)
        clip = (self.mvp @ p.T).T
        ndc = (clip[0, :2] / clip[0, 3])
        row = (1.0 - (ndc[1] + 1.0) * 0.5) * H
        assert 0.0 < row < H * 0.45             # 图像上半

    def test_depth_values_in_unit_range_with_gl_remapping(self):
        covered = self.depth[self.depth < FAR_DEPTH]
        assert (covered > 0.0).all() and (covered < 1.0).all()
        # 直接验证 GL 约定：z_ndc ∈ [-1,1] → d = z*0.5+0.5。
        pos = np.append(
            self.positions.astype(np.float64),
            np.ones((len(self.positions), 1)), axis=1)
        clip = (self.mvp @ pos.T).T
        z_ndc = clip[:, 2] / clip[:, 3]
        assert (z_ndc > -1.0).all() and (z_ndc < 1.0).all()
        d = z_ndc * 0.5 + 0.5
        assert (d > 0.0).all() and (d < 1.0).all()
        # 三个三角形的顶点深度依次更近（世界 z 更大 → 相机更近）。
        assert d[0:3].mean() > d[3:6].mean() > d[6:9].mean()

    def test_occlusion_nearest_triangle_wins(self):
        # 三角 2（最近，黄色系）覆盖区域应显示三角 2 的颜色而不是三角 1。
        center2 = np.array([[0.55, -0.05, 2.39, 1.0]], dtype=np.float64)
        clip = (self.mvp @ center2.T).T
        ndc_x, ndc_y = clip[0, 0] / clip[0, 3], clip[0, 1] / clip[0, 3]
        px = int((ndc_x + 1.0) * 0.5 * W)
        py = int((1.0 - (ndc_y + 1.0) * 0.5) * H)
        px, py = min(max(px, 0), W - 1), min(max(py, 0), H - 1)
        pixel = self.color[py, px]
        # 三角 2 顶点色 (0.95,0.8,0.1)/(0.8,0.1,0.9)/(0.1,0.9,0.9) 的插值，
        # 红通道明显高于背景 (0.1) 且不是纯背景色。
        assert pixel[0] > 0.25
        # 深度应接近三角 2 顶点深度（远小于背景深度）。
        d2 = (clip[0, 2] / clip[0, 3]) * 0.5 + 0.5
        assert abs(self.depth[py, px] - d2) < 1e-3

    def test_perspective_correct_color_interpolation(self):
        # 静态校验：对三角 1 内一点，透视校正插值 ≠ 屏幕空间线性插值
        # （相机有透视，两者应可区分），且插值在三顶点处回到顶点色。
        bary = np.array([0.2, 0.5, 0.3])
        verts = self.positions[3:6].astype(np.float64)
        cols = self.colors[3:6].astype(np.float64)
        homo = np.concatenate([verts, np.ones((3, 1))], axis=1)
        homo = (self.mvp @ homo.T).T
        ws = homo[:, 3]
        weights = bary / ws
        weights /= weights.sum()
        expected = (cols * weights[:, None]).sum(axis=0)
        naive = (cols * bary[:, None]).sum(axis=0)
        assert not np.allclose(expected, naive)     # 透视确实生效
        # 顶点处插值回到顶点色（自检）。
        for i in range(3):
            b = np.zeros(3); b[i] = 1.0
            w_i = b / ws; w_i /= w_i.sum()
            np.testing.assert_allclose(
                (cols * w_i[:, None]).sum(axis=0), cols[i], atol=1e-12)

    def test_triangle_cap_and_validation(self):
        big_positions = np.zeros((MAX_TRIANGLES * 3 + 3, 3), dtype=np.float32)
        big_colors = np.zeros((MAX_TRIANGLES * 3 + 3, 4), dtype=np.float32)
        with pytest.raises(ValueError):
            rasterize_mesh_cpu(big_positions, big_colors, self.mvp, 4, 4)
        with pytest.raises(ValueError):
            rasterize_mesh_cpu(self.positions[:2], self.colors[:2],
                               self.mvp, 4, 4)
        # 相机后方的顶点：本 pass 不做近平面裁剪 → 显式拒绝。
        behind = self.positions.copy()
        behind[0, 2] = 50.0    # 相机 z=4.2，顶点远在其后方
        with pytest.raises(ValueError):
            rasterize_mesh_cpu(behind, self.colors, self.mvp, 4, 4)


# ── GL 参考对拍（独立子进程） ────────────────────────────────────────────

_GL_VERTEX = """
#version 330
in vec3 in_pos;
in vec4 in_color;
uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
out vec4 v_color;
void main() {
    v_color = in_color;
    gl_Position = u_proj * u_view * u_model * vec4(in_pos, 1.0);
}
"""

_GL_FRAGMENT = """
#version 330
in vec4 v_color;
out vec4 fragColor;
void main() { fragColor = v_color; }
"""


def _check_gl_raster_matches_cpu_reference(color_path: str) -> None:
    """GL 真机渲染（_MESH_VERTEX 同式）与 CPU 参考颜色对拍。"""
    import moderngl

    positions, colors = _test_mesh()
    mvp = _test_mvp()
    ref_color, ref_depth = rasterize_mesh_cpu(positions, colors, mvp, W, H)

    # 与 Scene3D 相同的矩阵拆分（P/V/M）与 column-major 上传。
    eye = np.array([0.4, 1.2, 4.2], dtype=np.float32)
    target = np.array([0.1, 0.0, 0.0], dtype=np.float32)
    up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    view = m3d.look_at(eye, target, up)
    proj = m3d.perspective(50.0, W / H, 0.1, 100.0)
    model = m3d.IDENTITY4.copy()
    model[0, 0] = 1.03

    ctx = moderngl.create_context(standalone=True)
    prog = ctx.program(vertex_shader=_GL_VERTEX, fragment_shader=_GL_FRAGMENT)
    inter = np.concatenate(
        [positions.astype("f4"), colors.astype("f4")], axis=1)
    vbo = ctx.buffer(np.ascontiguousarray(inter).tobytes())
    vao = ctx.vertex_array(prog, vbo, "in_pos", "in_color")
    color_tex = ctx.texture((W, H), 4, dtype="f2")
    depth_tex = ctx.depth_texture((W, H))
    fbo = ctx.framebuffer(color_attachments=[color_tex],
                          depth_attachment=depth_tex)
    fbo.use()
    fbo.viewport = (0, 0, W, H)
    ctx.clear(0.0, 0.0, 0.0, 0.0, depth=1.0)
    ctx.enable(moderngl.DEPTH_TEST)   # 默认比较函数 less
    prog["u_proj"].value = m3d.matrix_to_tuple(proj)
    prog["u_view"].value = m3d.matrix_to_tuple(view)
    prog["u_model"].value = m3d.matrix_to_tuple(model)
    vao.render(moderngl.TRIANGLES)

    raw = np.frombuffer(color_tex.read(), dtype=np.float16)
    gl_color = raw.reshape(H, W, 4)[::-1]          # GL bottom-up → 图像行序
    np.testing.assert_allclose(
        np.asarray(gl_color, dtype=np.float32), ref_color,
        atol=COLOR_TOL, rtol=0)
    # 覆盖（alpha>0）完全一致——遮挡与裁剪空间的端到端证据。
    np.testing.assert_array_equal(
        np.asarray(gl_color)[..., 3] > 0.0, ref_color[..., 3] > 0.0)
    np.save(color_path, np.asarray(gl_color, dtype=np.float32))


def test_gl_raster_reference_matches_cpu_reference(tmp_path):
    from tests.composite.standalone_gpu import run_standalone_gpu
    run_standalone_gpu(
        _check_gl_raster_matches_cpu_reference,
        str(tmp_path / "gl_color.npy"))


# ── Metal 真机层 ────────────────────────────────────────────────────────


@requires_metal
class TestMetalMesh3DPass:
    def setup_method(self):
        self.positions, self.colors = _test_mesh()
        self.mvp = _test_mvp()
        self.ref_color, self.ref_depth = rasterize_mesh_cpu(
            self.positions, self.colors, self.mvp, W, H)

    def test_metal_raster_matches_cpu_reference_color_and_depth(self):
        from src.features.composite.render.metal.mesh3d import MetalMesh3DPass
        gpu_pass = MetalMesh3DPass()
        try:
            color, depth = gpu_pass.run(
                self.positions, self.colors, self.mvp, W, H)
        finally:
            gpu_pass.cleanup()
        # 覆盖（深度已写出 = FLT_MAX → inf 哨兵）完全一致。
        np.testing.assert_array_equal(
            np.isfinite(depth), self.ref_depth < FAR_DEPTH)
        # 颜色：RGBA16F 容差。
        np.testing.assert_allclose(color, self.ref_color,
                                   atol=COLOR_TOL, rtol=0)
        # 深度：R32F 与 CPU float64 参考。
        covered = np.isfinite(depth)
        np.testing.assert_allclose(
            depth[covered], self.ref_depth[covered],
            atol=DEPTH_TOL, rtol=0)

    def test_mesh_pass_returns_pool_to_idle(self):
        from src.features.composite.render.metal.mesh3d import MetalMesh3DPass
        gpu_pass = MetalMesh3DPass()
        try:
            for _ in range(3):
                gpu_pass.run(self.positions, self.colors, self.mvp, W, H)
            pool = gpu_pass.renderer.pool
            scheduler = gpu_pass.renderer.scheduler
            assert pool.live_count == 0          # R32F/RGBA16F 附件全部归还
            assert pool.retiring_count == 0
            assert scheduler.in_flight == 0
        finally:
            gpu_pass.cleanup()


# ── Scene3D 映射评估（契约：不接入 + 显式诊断） ─────────────────────────


class TestScene3dMappingAssessment:
    def _scene_graph(self) -> dict:
        # camera3d.display(scene) → scene3d.in(scene) → scene3d.display(image)
        # → output.image：合法的 scene 端口拓扑。
        return {
            "nodes": [
                {"id": "cam", "type": "camera3d", "parameters": {}},
                {"id": "s", "type": "scene3d", "parameters": {}},
                {"id": "o", "type": "output"},
            ],
            "connections": [
                {"src_node_id": "cam", "src_port_name": "display",
                 "dst_node_id": "s", "dst_port_name": "in"},
                {"src_node_id": "s", "src_port_name": "display",
                 "dst_node_id": "o", "dst_port_name": "image"},
            ],
        }

    def test_3d_nodes_reported_as_unmappable(self):
        issues = evaluate_3d_mapping(self._scene_graph())
        assert any("[s] scene3d" in line for line in issues)
        assert any("[cam] camera3d" in line for line in issues)
        assert all("无法无歧义映射" in line for line in issues)

    def test_preflight_reports_3d_code(self):
        from src.features.composite.render.metal import evaluate_metal_support
        report = evaluate_metal_support(self._scene_graph())
        assert not report.supported
        issue = next(i for i in report.issues if i.node_id == "s")
        assert issue.code == "node.metal_3d_unmapped"
        assert "ScenePayload" in issue.reason
