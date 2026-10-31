"""AUD-9108 修复验证：程序化基元三角形缠绕与法线一致性。

约定（与 .clinerules 硬约束一致，右手系、CCW 为正面、cull back）：
    对每个非退化三角形 (v0, v1, v2)，
    几何法线 cross(v1-v0, v2-v0) 必须与顶点法线（或解析法线）同向，
    即点积 >= 0（允许数值 epsilon）。

覆盖基元族：cube（逐面）、plane、sphere（含解析径向法线）、
cylinder（侧面/顶盖/底盖分族）、cone（侧面/底盖分族）、torus（含解析法线）。
grid/null 为 LINES 语义（索引为线段对），不属于三角形缠绕校验范围。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np

from src.features.composite.render3d.primitives import (
    cone,
    cube,
    cylinder,
    plane,
    sphere,
    torus,
)

EPS = -1e-9          # 点积下限（几何法线未归一化，含面积因子，故只用符号判定）
DEGENERATE_LEN = 1e-12  # 几何法线模长低于此值视为零面积退化三角形（如球极点）


def _triangles(mesh: np.ndarray) -> np.ndarray:
    """MeshData.indices -> (T, 3) 三角形索引（防御非 3 倍数长度截断）。"""
    idx = mesh.indices
    tri = idx[: (idx.shape[0] // 3) * 3].reshape(-1, 3).astype(np.int64)
    return tri


def assert_winding_matches_normals(
    mesh, ref_normals: np.ndarray, label: str, tri_slice: "tuple[int, int] | None" = None,
) -> int:
    """断言每个非退化三角形 cross(v1-v0, v2-v0)·ref_normal >= 0。

    Args:
        mesh: primitives.MeshData。
        ref_normals: (N, 3) 参考法线（顶点法线或解析法线）。
        label: 失败信息用的人类可读族名。
        tri_slice: 可选 (lo, hi) 三角形行范围，用于分族（如 cylinder 侧面/盖）。

    Returns:
        参与校验的非退化三角形数。
    """
    tri = _triangles(mesh)
    if tri_slice is not None:
        tri = tri[tri_slice[0]: tri_slice[1]]
    v = mesh.vertices.astype(np.float64)
    ref = ref_normals.astype(np.float64)
    e1 = v[tri[:, 1]] - v[tri[:, 0]]
    e2 = v[tri[:, 2]] - v[tri[:, 0]]
    geo_n = np.cross(e1, e2)                       # (T, 3) 几何法线（含面积因子）
    face_ref = ref[tri[:, 0]] + ref[tri[:, 1]] + ref[tri[:, 2]]
    dots = np.einsum("ij,ij->i", geo_n, face_ref)   # (T,)
    area = np.linalg.norm(geo_n, axis=1)
    real = area > DEGENERATE_LEN
    assert real.any(), f"{label}: 该族三角形全部退化，测试无意义"
    bad = np.where(real & (dots < EPS))[0]
    assert bad.size == 0, (
        f"{label}: {bad.size}/{real.sum()} 个三角形几何法线与参考法线反向 "
        f"(dot min={dots[real].min():.3e})，首犯 tri#{bad[0]}={tri[bad[0]].tolist()}")
    return int(real.sum())


# ─────────────────────────── cube ───────────────────────────

def test_cube_all_faces_winding_matches_normals() -> None:
    m = cube(1.0)
    assert_winding_matches_normals(m, m.normals, "cube")


def test_cube_each_face_consistent_across_sizes() -> None:
    for half in (0.5, 1.0, 2.5):
        m = cube(half)
        assert_winding_matches_normals(m, m.normals, f"cube(half={half})")


def test_cube_outward_ccw_semantics() -> None:
    """每个面的几何法线必须与其面法线（=声明的顶点法线）正向重合。"""
    m = cube(1.0)
    tri = _triangles(m)
    v = m.vertices.astype(np.float64)
    n = m.normals.astype(np.float64)
    geo = np.cross(v[tri[:, 1]] - v[tri[:, 0]], v[tri[:, 2]] - v[tri[:, 0]])
    face_n = n[tri[:, 0]]
    cos = np.einsum("ij,ij->i", geo, face_n) / (
        np.linalg.norm(geo, axis=1) * np.linalg.norm(face_n, axis=1))
    assert np.all(cos > 0.999), f"cube 各面几何法线须与 +轴面法线共线同向, min cos={cos.min()}"


# ─────────────────────────── plane ───────────────────────────

def test_plane_winding_matches_plus_y_normals() -> None:
    for size in (1.0, 2.0, 7.3):
        m = plane(size)
        assert_winding_matches_normals(m, m.normals, f"plane(size={size})")
        assert np.allclose(m.normals, [0.0, 1.0, 0.0])


# ─────────────────────────── sphere ───────────────────────────

def test_sphere_winding_matches_vertex_normals() -> None:
    for seg, rings in ((8, 4), (32, 16)):
        m = sphere(1.0, segments=seg, rings=rings)
        assert_winding_matches_normals(m, m.normals, f"sphere({seg}x{rings})")


def test_sphere_winding_matches_analytic_radial_normals() -> None:
    """解析法线 = 归一化位置（球心在原点），独立于生成器自身输出的法线。"""
    m = sphere(2.0, segments=12, rings=6)
    radial = m.vertices.astype(np.float64)
    radial /= np.linalg.norm(radial, axis=1, keepdims=True)
    assert_winding_matches_normals(m, radial, "sphere(analytic radial)")


# ─────────────────────────── cylinder ───────────────────────────

def test_cylinder_side_and_caps_winding() -> None:
    """侧面（水平径向法线）与顶/底盖（±Y 法线）分族校验。

    索引布局：侧面 6*segments 个索引 → 2*segments 个三角形；
    之后顶盖 3*segments、底盖 3*segments 个索引。
    """
    for seg in (3, 8, 32):
        m = cylinder(radius=1.5, height=2.0, segments=seg)
        side_tris = 2 * seg
        cap_tris = seg
        n_side = assert_winding_matches_normals(
            m, m.normals, f"cylinder side (segments={seg})", (0, side_tris))
        assert n_side == side_tris
        assert_winding_matches_normals(
            m, m.normals, f"cylinder top cap (segments={seg})",
            (side_tris, side_tris + cap_tris))
        assert_winding_matches_normals(
            m, m.normals, f"cylinder bottom cap (segments={seg})",
            (side_tris + cap_tris, side_tris + 2 * cap_tris))


def test_cylinder_caps_analytic_axis_normals() -> None:
    """盖族参考法线直接取 ±Y 解析值，防止顶点法线本身写反掩盖缠绕问题。"""
    m = cylinder(radius=1.0, height=2.0, segments=16)
    seg = 16
    top_lo, top_hi = 2 * seg, 3 * seg
    bot_lo, bot_hi = 3 * seg, 4 * seg
    plus_y = np.tile([0.0, 1.0, 0.0], (m.vertices.shape[0], 1))
    minus_y = -plus_y
    assert_winding_matches_normals(m, plus_y, "cylinder top cap (+Y analytic)", (top_lo, top_hi))
    assert_winding_matches_normals(m, minus_y, "cylinder bottom cap (-Y analytic)", (bot_lo, bot_hi))


# ─────────────────────────── cone ───────────────────────────

def test_cone_side_and_cap_winding() -> None:
    for seg in (3, 8, 32):
        m = cone(radius=1.5, height=2.0, segments=seg)
        side_tris = seg
        assert_winding_matches_normals(
            m, m.normals, f"cone side (segments={seg})", (0, side_tris))
        assert_winding_matches_normals(
            m, m.normals, f"cone bottom cap (segments={seg})",
            (side_tris, side_tris + seg))


# ─────────────────────────── torus ───────────────────────────

def test_torus_winding_matches_vertex_normals() -> None:
    for maj, mnr in ((8, 4), (32, 16)):
        m = torus(major_radius=1.0, minor_radius=0.3,
                  major_segments=maj, minor_segments=mnr)
        assert_winding_matches_normals(m, m.normals, f"torus({maj}x{mnr})")


def test_torus_winding_matches_analytic_radial_normals() -> None:
    """解析法线 = 表面点 − 管中心（大圆上对应点），归一化（独立于顶点法线字段）。"""
    major_r = 2.0
    m = torus(major_radius=major_r, minor_radius=0.5, major_segments=12, minor_segments=6)
    v = m.vertices.astype(np.float64)
    # 管中心：顶点水平方向缩放到大圆半径 R（torus 生成时 u 方向与水平方位一致）
    horiz = v.copy()
    horiz[:, 1] = 0.0
    horiz /= np.linalg.norm(horiz, axis=1, keepdims=True)
    center = horiz * major_r
    radial = v - center
    radial /= np.linalg.norm(radial, axis=1, keepdims=True)
    assert np.all(np.isfinite(radial))
    # 解析法线应与生成器顶点法线一致（管径向），自检
    assert np.allclose(radial, m.normals.astype(np.float64), atol=1e-5)
    assert_winding_matches_normals(m, radial, "torus(analytic radial)")
