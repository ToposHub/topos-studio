"""P1-COLOR-11：统一 LUT 加载器 / tetrahedral 采样 / metadata / .3dl。

验收核心：
- 同一 LUT 在 GPU（硬件三线性）与 CPU（trilinear/tetrahedral）误差受控
- .cube metadata（TITLE/DESCRIPTION/DOMAIN/1D shaper）完整解析
- .3dl（Lustre 整数格式）解析与归一化正确
- tetra/tri 均随网格加密 O(1/N²) 收敛（数学正确性）
"""

import numpy as np
import pytest

from src.shared.color.lut_loader import (
    LoadedLut,
    load_lut,
    parse_3dl,
    parse_cube,
    probe_lut_metadata,
    sample_lut_tetrahedral,
    sample_lut_trilinear,
)


def _identity_cube_text(n: int = 4) -> str:
    lines = [f"LUT_3D_SIZE {n}"]
    axis = np.linspace(0, 1, n)
    for b in axis:
        for g in axis:
            for r in axis:
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    return "\n".join(lines) + "\n"


def _lut_from_fn(fn, n: int = 9) -> LoadedLut:
    axis = np.linspace(0, 1, n)
    bb, gg, rr = np.meshgrid(axis, axis, axis, indexing='ij')
    table = fn(rr, gg, bb).astype(np.float32)
    return LoadedLut(table=table, size=n, format='cube')


class TestCubeParsing:
    def test_identity_and_axes(self):
        lut = parse_cube(_identity_cube_text(4))
        assert lut.size == 4
        assert lut.table.shape == (4, 4, 4, 3)
        # 恒等：table[b, g, r] == (r, g, b)（n=4：axis=0,1/3,2/3,1）
        assert np.allclose(lut.table[1, 2, 3], [1.0, 2/3, 1/3], atol=1e-6)

    def test_metadata(self):
        text = ('TITLE "Warm Look"\n'
                'DESCRIPTION "Kodak 2383 emulation"\n'
                'LUT_3D_SIZE 2\n'
                'DOMAIN_MIN 0.0 0.0 0.0\n'
                'DOMAIN_MAX 1.0 1.0 1.0\n') + _identity_cube_text(2).split(
                    '\n', 1)[1]
        lut = parse_cube(text)
        assert lut.title == "Warm Look"
        assert lut.description == "Kodak 2383 emulation"
        assert lut.size == 2

    def test_1d_shaper(self):
        # 1D shaper（identity 2 点）+ 3D 表
        text = ("LUT_1D_SIZE 2\n0.0\n1.0\n"
                "LUT_3D_SIZE 2\n") + _identity_cube_text(2).split('\n', 1)[1]
        lut = parse_cube(text)
        assert lut.shaper_1d is not None and len(lut.shaper_1d) == 2
        assert lut.size == 2

    def test_corrupt_raises(self):
        with pytest.raises(ValueError):
            parse_cube("LUT_3D_SIZE 4\n0 0 0\n")  # 点数不足

    def test_describe(self):
        lut = parse_cube(_identity_cube_text(2))
        assert "2³" in lut.describe() and "cube" in lut.describe()


class Test3dlParsing:
    def test_integer_normalization(self):
        # 2³ 恒等，10-bit 整数编码
        lines = ["2 2 2"]
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    lines.append(f"{r*1023} {g*1023} {b*1023}")
        lut = parse_3dl("\n".join(lines))
        assert lut.size == 2 and lut.format == '3dl'
        assert np.allclose(lut.table[0, 0, 1], [1.0, 0.0, 0.0], atol=1e-3)
        assert np.allclose(lut.table[1, 1, 0], [0.0, 1.0, 1.0], atol=1e-3)  # r=0,g=1,b=1

    def test_float_direct(self):
        lines = ["2 2 2"]
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    lines.append(f"{r} {g} {b}")
        lut = parse_3dl("\n".join(lines))
        assert np.allclose(lut.table[0, 1, 0], [0.0, 1.0, 0.0])

    def test_round_trip_with_cube(self):
        # cube 恒等与 3dl 恒等应一致
        lut_c = parse_cube(_identity_cube_text(4))
        lines = ["4 4 4"]
        axis = np.linspace(0, 1, 4)
        for b in axis:
            for g in axis:
                for r in axis:
                    lines.append(f"{r*4095:.0f} {g*4095:.0f} {b*4095:.0f}")
        lut_d = parse_3dl("\n".join(lines))
        assert np.allclose(lut_c.table, lut_d.table, atol=2 / 4095)


class TestLoadByExtension:
    def test_load_cube_file(self, tmp_path):
        path = tmp_path / "test.cube"
        path.write_text(_identity_cube_text(2))
        lut = load_lut(str(path))
        assert lut.format == 'cube'

    def test_load_3dl_file(self, tmp_path):
        path = tmp_path / "test.3dl"
        path.write_text("2 2 2\n0 0 0\n0 0 1023\n0 1023 0\n0 1023 1023\n"
                        "1023 0 0\n1023 0 1023\n1023 1023 0\n1023 1023 1023\n")
        lut = load_lut(str(path))
        assert lut.format == '3dl' and lut.size == 2

    def test_load_unknown_ext_content_probe(self, tmp_path):
        path = tmp_path / "mystery.lut"
        path.write_text(_identity_cube_text(2))
        lut = load_lut(str(path))
        assert lut.size == 2


class TestProbeMetadata:
    def test_probe(self, tmp_path):
        path = tmp_path / "look.cube"
        path.write_text('TITLE "Big Look"\nLUT_3D_SIZE 33\n' +
                        _identity_cube_text(2).split('\n', 1)[1])
        meta = probe_lut_metadata(str(path))
        assert meta['title'] == 'Big Look'
        assert meta['size'] == 33
        assert meta['format'] == 'cube'

    def test_probe_3dl(self, tmp_path):
        path = tmp_path / "look.3dl"
        path.write_text("17 17 17\n" + "0 0 0\n" * 10)
        meta = probe_lut_metadata(str(path))
        assert meta['size'] == 17 and meta['format'] == '3dl'

    def test_probe_missing_file(self):
        assert probe_lut_metadata('/nonexistent/x.cube') == {}


class TestSampling:
    def test_identity_exact(self):
        lut = _lut_from_fn(lambda r, g, b: np.stack([r, g, b], -1))
        pts = np.random.default_rng(11).random((300, 3))
        assert np.abs(sample_lut_tetrahedral(lut, pts) - pts).max() < 1e-6
        assert np.abs(sample_lut_trilinear(lut, pts) - pts).max() < 1e-6

    def test_linear_exact(self):
        lut = _lut_from_fn(
            lambda r, g, b: np.stack([r * 0.5, g * 1.5, b + 0.0], -1))
        pts = np.random.default_rng(12).random((300, 3))
        expect = np.stack([pts[:, 0] * 0.5, pts[:, 1] * 1.5, pts[:, 2]], -1)
        assert np.abs(sample_lut_tetrahedral(lut, pts) - expect).max() < 1e-6

    def test_grid_point_exact(self):
        # 格点上的采样 = 格点值（非线性 LUT）
        lut = _lut_from_fn(
            lambda r, g, b: np.stack([r * g, (r + b) / 2, b * b], -1))
        axis = np.linspace(0, 1, 9)
        pts = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), -1) \
            .reshape(-1, 3)[:, ::-1]  # → RGB
        out = sample_lut_tetrahedral(lut, pts)
        expect = np.stack([pts[:, 0] * pts[:, 1],
                           (pts[:, 0] + pts[:, 2]) / 2,
                           pts[:, 2] ** 2], -1)
        assert np.abs(out - expect).max() < 1e-5

    def test_quadratic_convergence(self):
        """tetra/tri 对光滑非线性函数均按 O(1/N²) 收敛。"""
        fn = lambda r, g, b: np.stack(
            [(0.5 * r + 0.3 * g + 0.2 * b) ** 2, r * g,
             0.5 * (r ** 2 + b)], -1)
        pts = np.random.default_rng(3).random((500, 3))
        true = fn(pts[:, 0], pts[:, 1], pts[:, 2])
        errs = []
        for n in (5, 17):
            lut = _lut_from_fn(fn, n=n)
            t = sample_lut_tetrahedral(lut, pts)
            l = sample_lut_trilinear(lut, pts)
            errs.append((np.abs(t - true).max(), np.abs(l - true).max(),
                         np.abs(t - l).max()))
        # 网格 ×3.4（5→17）：误差应显著下降（≥5 倍）
        for which in range(3):
            assert errs[0][which] > errs[1][which] * 5.0
        # GPU/CPU 互差与同网格误差同量级（受控）
        assert errs[1][2] < 5e-3

    def test_domain_remap(self):
        lut = LoadedLut(
            table=np.zeros((2, 2, 2, 3), dtype=np.float32), size=2,
            format='cube', domain_min=(0.0, 0.0, 0.0),
            domain_max=(2.0, 2.0, 2.0))
        # DOMAIN [0,2]：输入 2.0 应映射到格点 1.0（不越界）
        out = sample_lut_trilinear(lut, np.array([[2.0, 2.0, 2.0]]))
        assert np.isfinite(out).all()


class TestGpuTrilinearParity:
    """真实 GPU 3D 纹理采样 vs CPU trilinear（误差受控验收）。"""

    def test_gpu_texture3d_matches_cpu_trilinear(self):
        moderngl = pytest.importorskip('moderngl')
        lut = _lut_from_fn(
            lambda r, g, b: np.stack(
                [(0.5 * r + 0.3 * g + 0.2 * b) ** 2, r * g,
                 0.5 * (r ** 2 + b)], -1), n=17)
        ctx = moderngl.create_standalone_context(require=330)
        try:
            tex = ctx.texture3d((lut.size,) * 3, 3,
                                data=np.ascontiguousarray(
                                    lut.table, dtype='f4').tobytes(),
                                dtype='f4')
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = tex.repeat_z = False
            tex.use(0)

            prog = ctx.program(
                vertex_shader="""
                    #version 330
                    in vec2 in_vert;
                    out vec3 v_rgb;
                    void main() {
                        gl_Position = vec4(in_vert, 0.0, 1.0);
                        v_rgb = vec3(in_vert * 0.5 + 0.5, 0.37);
                    }
                """,
                fragment_shader="""
                    #version 330
                    uniform sampler3D u_lut;
                    in vec3 v_rgb;
                    out vec4 f_color;
                    void main() {
                        // 生产端修复公式（shader_nodes.LUT3D_SNIPPET /
                        // apply_lut_hdr_safe）：LUT 网格点 i 对应输入
                        // i/(n-1)，映射到 GL texel 中心 (i+0.5)/n。
                        float n = float(textureSize(u_lut, 0).x);
                        vec3 t = (clamp(v_rgb, 0.0, 1.0) * (n - 1.0) + 0.5) / n;
                        f_color = vec4(texture(u_lut, t).rgb, 1.0);
                    }
                """)
            vbo = ctx.buffer(np.array(
                [-1, -1, 1, -1, -1, 1, 1, 1], dtype='f4').tobytes())
            vao = ctx.vertex_array(
                prog, [(vbo, '2f', 'in_vert')])
            fbo = ctx.simple_framebuffer((64, 64))
            fbo.use()
            prog['u_lut'].value = 0
            vao.render(moderngl.TRIANGLE_STRIP)
            gpu = np.frombuffer(
                fbo.read(components=4, alignment=1), dtype=np.uint8
            ).reshape(64, 64, 4)[..., :3].astype(np.float32) / 255.0
            vao.release(); vbo.release(); tex.release()

            # 重建采样点（与顶点插值一致：R=x, G=y, B=0.37）
            # （fbo.read 的行 0 对应 gl 底部 v=-1，无需翻转）
            xs = (np.arange(64) + 0.5) / 64.0
            yy, xx = np.meshgrid(xs, xs, indexing='ij')
            pts = np.stack([xx.ravel(), yy.ravel(),
                            np.full(yy.size, 0.37)], -1)
            # 驱动发现（AMD Radeon / macOS）：ModernGL repeat_*=False 映射到
            # GL_CLAMP 而非 CLAMP_TO_EDGE —— 直接用输入值当纹理坐标会把
            # 边缘半 texel 重映射（有效插值区间 [0.5/N, 1-0.5/N]），17³ LUT
            # 边缘 ~3% 区域误差 ≈ 相邻格点差。生产采样已改为 texel 中心映射
            # ((N-1)c + 0.5)/N（见上方 fragment shader），CPU 参照即网格点
            # 对齐采样本身——GPU/CPU 误差收敛到帧缓冲量化级即证明修复有效。
            cpu = sample_lut_trilinear(lut, pts).reshape(64, 64, 3)
            # GPU 8-bit framebuffer 量化：±1.5 LSB
            assert np.abs(gpu - cpu).max() < 1.5 / 255.0
        finally:
            ctx.release()
