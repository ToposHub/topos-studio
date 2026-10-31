"""调色工具链 MSL film grain / soft clip 内核测试（计划 P6-06）。

对照 `kernels_color.py` 的 numpy 参考实现（与 shader_nodes.py 的
``FILM_GRAIN_SNIPPET``/``SOFT_CLIP_SNIPPET`` 逐条对齐），验收
「固定 seed 一致」+ HDR 语义。分两层：

- 结构层（无 Metal 也运行）：参数布局、MSL 源守护断言（禁内建随机
  原语 / 禁 clamp）、参考实现语义钉子。
- 真机层（无 Metal 设备整类跳过）：确定性位级门禁 + vs 参考的
  1-2 fp16 ulp 门禁（量化噪声，来自 AIR 后端对 lerp 形状的代数重写
  与 pow 的 ulp 差异，实测见 kernels_color 模块文档）。

输入量化注意：kernel 从 RGBA16Float 纹理读入，参考实现必须使用
**先过 fp16 量化的颜色**（``astype(float16).astype(float32)``），
否则对比的是量化前后两份输入，差值全是量化噪声（实测 0.0028 @4K 值域）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.shared.gpu.metal import MetalComputeContext
from src.shared.gpu.metal.context import MetalComputeContext as _Ctx
from src.shared.gpu.metal.kernels_color import (
    FILM_GRAIN_PARAMS_DTYPE,
    METAL_FILM_GRAIN_MSL,
    METAL_SOFT_CLIP_MSL,
    SOFT_CLIP_PARAMS_DTYPE,
    build_film_grain_params,
    build_soft_clip_params,
    film_grain_reference,
    soft_clip_reference,
)
from src.shared.media.macos_vt.cv_metal_bridge import (
    mtl_pixel_format,
    read_texture_rgba16f,
)

requires_metal = pytest.mark.skipif(
    not _Ctx.is_supported(), reason="无 Metal 设备"
)


def _random_color(h: int, w: int, seed: int = 7,
                  lo: float = -0.2, hi: float = 3.5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    color = rng.uniform(lo, hi, (h, w, 3)).astype(np.float32)
    color[0, 0] = (3.0, -0.15, 0.0)  # 恒含 HDR 高值/负值/零
    return color


def _quantize_input(color: np.ndarray) -> np.ndarray:
    """kernel 从 fp16 纹理读入——参考实现用同一份量化后的输入。"""
    return color.astype(np.float16).astype(np.float32)


def _fp16_spacing(ref16: np.ndarray) -> np.ndarray:
    """ref16（float16）每个元素的 1 ulp 绝对间隔。"""
    up = np.nextafter(ref16, np.float16(np.inf)).astype(np.float32)
    return np.abs(up - ref16.astype(np.float32))


# ---------------------------------------------------------------------------
# 结构层：参数布局与 MSL 源守护（无需 Metal）
# ---------------------------------------------------------------------------


class TestParamsLayout:
    def test_film_grain_dtype_matches_msl_struct(self) -> None:
        assert FILM_GRAIN_PARAMS_DTYPE.itemsize == 52  # 13 × f4/u4，无填充
        for name in FILM_GRAIN_PARAMS_DTYPE.names:
            off = FILM_GRAIN_PARAMS_DTYPE.fields[name][1]
            assert off % 4 == 0, name
        assert FILM_GRAIN_PARAMS_DTYPE["monochrome"].base == np.uint32

    def test_soft_clip_dtype_matches_msl_struct(self) -> None:
        assert SOFT_CLIP_PARAMS_DTYPE.itemsize == 32  # 8 × f4
        for name in SOFT_CLIP_PARAMS_DTYPE.names:
            assert SOFT_CLIP_PARAMS_DTYPE.fields[name][1] % 4 == 0

    def test_build_film_grain_params_values(self) -> None:
        params = build_film_grain_params(
            intensity=0.35, size=2.5, monochrome=True, shadows=0.8,
            midtones=1.0, highlights=0.5, seed=0.42,
            visible_rect=(4.0, 6.0, 37.0, 23.0), tex_size=(2048.0, 1024.0),
        )
        assert params["monochrome"][0] == 1
        assert params["rect_w"][0] == 37.0
        assert params["tex_h"][0] == 1024.0
        mono_off = build_film_grain_params(
            intensity=0.35, size=2.5, monochrome=False, shadows=0.8,
            midtones=1.0, highlights=0.5, seed=0.0,
        )
        assert mono_off["monochrome"][0] == 0
        assert mono_off["rect_w"][0] == 0.0  # 缺省 → 回退分支

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_build_film_grain_params_rejects_nonfinite(self, bad) -> None:
        with pytest.raises(ValueError, match="非有限"):
            build_film_grain_params(
                intensity=bad, size=2.5, monochrome=False, shadows=0.8,
                midtones=1.0, highlights=0.5, seed=0.42,
            )

    def test_build_soft_clip_params_rejects_nonfinite(self) -> None:
        with pytest.raises(ValueError, match="非有限"):
            build_soft_clip_params(
                high=1.0, white_point=2.0, shoulder_strength=0.5,
                low=0.0, black_point=-1.0, toe_strength=0.5,
                exposure=float("nan"), contrast=0.0,
            )


class TestMslSourceGuards:
    """随机数确定性 / HDR 语义的源级守护（P6-06 两个风险项）。"""

    @staticmethod
    def _code(src: str) -> str:
        """剥离 // 行注释，只对实际代码做守护（注释可提及被禁原语）。"""
        return "\n".join(line.split("//")[0] for line in src.splitlines())

    def test_film_grain_forbids_builtin_random_primitives(self) -> None:
        # 内建 dot()/mix()/smoothstep() 的求和顺序与 FMA 不可控（实测
        # dot() 使哈希链整格翻转）——只允许显式展开
        code = self._code(METAL_FILM_GRAIN_MSL)
        assert "dot(" not in code
        assert "mix(" not in code
        assert "smoothstep(" not in code  # 自实现 smooth_step 不受影响

    def test_soft_clip_forbids_clamp(self) -> None:
        # HDR 语义：不 clamp，负值空间由 toe 公式处理
        assert "clamp(" not in self._code(METAL_SOFT_CLIP_MSL)

    @pytest.mark.parametrize("src,kernel", [
        (METAL_FILM_GRAIN_MSL, "film_grain"),
        (METAL_SOFT_CLIP_MSL, "soft_clip"),
    ])
    def test_kernel_structure(self, src: str, kernel: str) -> None:
        code = self._code(src)
        assert f"kernel void {kernel}" in code
        assert "gid.x >= dst.get_width()" in code  # 越界守护
        assert "access::read" in code and "access::write" in code
        assert "#pragma clang fp contract(off)" in src  # 实测必要的确定性前提


# ---------------------------------------------------------------------------
# 结构层：参考实现语义钉子（GLSL 行为的忠实性）
# ---------------------------------------------------------------------------


class TestFilmGrainReferenceSemantics:
    KW = dict(intensity=0.35, size=2.5, monochrome=False, shadows=0.8,
              midtones=1.0, highlights=0.5, seed=0.42,
              visible_rect=(0.0, 0.0, 0.0, 0.0), tex_size=(37.0, 23.0))

    def test_zero_intensity_is_identity(self) -> None:
        color = _random_color(8, 11)
        out = film_grain_reference(color, intensity=0.0, size=2.5,
                                   monochrome=False, shadows=0.8,
                                   midtones=1.0, highlights=0.5, seed=0.42)
        np.testing.assert_array_equal(out, color)

    def test_monochrome_shares_one_pattern(self) -> None:
        # 灰图输入 → 各通道加同一偏移 → 输出通道位级相等
        gray = np.full((16, 16, 3), 0.5, dtype=np.float32)
        out = film_grain_reference(gray, intensity=0.5, size=2.0,
                                   monochrome=True, shadows=0.8, midtones=1.0,
                                   highlights=0.5, seed=0.42)
        np.testing.assert_array_equal(out[..., 0], out[..., 1])
        np.testing.assert_array_equal(out[..., 1], out[..., 2])

    def test_seed_changes_pattern(self) -> None:
        color = _random_color(24, 24, seed=5)
        base = dict(intensity=0.6, size=3.0, monochrome=False, shadows=0.8,
                    midtones=1.0, highlights=0.5, visible_rect=(0, 0, 0, 0),
                    tex_size=(24.0, 24.0))
        a = film_grain_reference(color, seed=0.42, **base)
        b = film_grain_reference(color, seed=7.0, **base)
        assert not np.array_equal(a, b)

    def test_visible_rect_binds_grain_to_image_space(self) -> None:
        """颗粒"焊死"在图像纹理上：同 seed 同画布，视口原点平移后，
        对应物理像素的噪声骨架一致（mask/cp 只依赖颜色，可整幅对比）。"""
        kw = dict(intensity=1.0, size=4.0, monochrome=False, shadows=1.0,
                  midtones=1.0, highlights=1.0, tex_size=(2048.0, 2048.0))
        # 均匀灰图 → luma 恒定 → mask/cp 恒定，输出差异只来自噪声
        gray = np.full((16, 16, 3), 0.5, dtype=np.float32)
        a = film_grain_reference(gray, seed=0.42,
                                 visible_rect=(0.0, 0.0, 16.0, 16.0), **kw)
        b = film_grain_reference(gray, seed=0.42,
                                 visible_rect=(32.0, 0.0, 16.0, 16.0), **kw)
        assert not np.allclose(a, b)  # 不同图像区域 → 不同噪声


class TestSoftClipReferenceSemantics:
    def test_exposure_doubles(self) -> None:
        color = _random_color(8, 8, seed=2, lo=-0.4, hi=2.0)
        out = soft_clip_reference(
            color, high=1e6, white_point=2e6, shoulder_strength=0.5,
            low=-10.0, black_point=-20.0, toe_strength=0.5,
            exposure=1.0, contrast=0.0,
        )
        np.testing.assert_array_equal(out, color * np.float32(2.0))

    def test_hdr_no_clamp_with_inactive_compressors(self) -> None:
        color = _random_color(8, 8, seed=4, lo=-0.9, hi=4.0)
        out = soft_clip_reference(
            color, high=1e6, white_point=2e6, shoulder_strength=0.5,
            low=-10.0, black_point=-20.0, toe_strength=0.5,
            exposure=1.0, contrast=0.0,
        )
        assert out.max() > 1.0  # >1 保留
        assert out.min() < 0.0  # 负值保留（未触发 toe）

    def test_shoulder_caps_at_white_point_and_preserves_ratio(self) -> None:
        color = np.array([[[3.0, 1.5, 0.75]]], dtype=np.float32)
        out = soft_clip_reference(
            color, high=1.0, white_point=2.0, shoulder_strength=0.5,
            low=-10.0, black_point=-20.0, toe_strength=0.5,
            exposure=0.0, contrast=0.0,
        )
        assert out[0, 0, 0] < 3.0  # 被压暗
        # 等比缩放 → 通道比例不变（饱和度/色相保护）
        np.testing.assert_allclose(
            out[0, 0, 0] / out[0, 0, 1], 2.0, rtol=1e-6)
        np.testing.assert_allclose(
            out[0, 0, 1] / out[0, 0, 2], 2.0, rtol=1e-6)

    def test_toe_pulls_negatives_above_black_point(self) -> None:
        color = np.array([[[-0.05, 0.02, 0.10]]], dtype=np.float32)
        out = soft_clip_reference(
            color, high=1e6, white_point=2e6, shoulder_strength=0.5,
            low=0.0, black_point=-1.0, toe_strength=0.6,
            exposure=0.0, contrast=0.0,
        )
        v = out[0, 0, 0]
        assert -1.0 < v < 0.0  # 被压缩到 (black_point, low) 区间
        np.testing.assert_array_equal(out[0, 0, 1], np.float32(0.02))  # ≥low 不受 toe
        np.testing.assert_array_equal(out[0, 0, 2], np.float32(0.10))

    def test_toe_elif_branch_fidelity_pin(self) -> None:
        """GLSL 忠实性钉子：black_point ≥ low 时，c < low 的值被 elif
        分支抬到 black_point（原实现语义，非笔误修复——如实移植）。"""
        color = np.array([[[-0.05, 0.5, 0.5]]], dtype=np.float32)
        out = soft_clip_reference(
            color, high=1e6, white_point=2e6, shoulder_strength=0.5,
            low=0.0, black_point=0.1, toe_strength=0.6,
            exposure=0.0, contrast=0.0,
        )
        np.testing.assert_array_equal(out[0, 0, 0], np.float32(0.1))

    def test_contrast_keeps_zero_and_sign(self) -> None:
        color = np.array([[[-0.36, 0.0, 0.36]]], dtype=np.float32)
        out = soft_clip_reference(
            color, high=1e6, white_point=2e6, shoulder_strength=0.5,
            low=-10.0, black_point=-20.0, toe_strength=0.5,
            exposure=0.0, contrast=1.0,
        )
        assert out[0, 0, 1] == 0.0  # sign(0)=0
        assert np.sign(out[0, 0, 0]) == -1.0 and np.sign(out[0, 0, 2]) == 1.0
        # |x|/0.18 的 factor=2 次幂 × 0.18
        np.testing.assert_allclose(
            abs(out[0, 0, 2]), (0.36 / 0.18) ** 2 * 0.18, rtol=1e-5)


# ---------------------------------------------------------------------------
# 真机层：确定性 + parity 门禁
# ---------------------------------------------------------------------------


def _render(ctx: MetalComputeContext, msl: str, name: str,
            color: np.ndarray, params: np.ndarray) -> np.ndarray:
    h, w = color.shape[:2]
    fmt = mtl_pixel_format("rgba16float")
    rgba = np.empty((h, w, 4), dtype=np.float16)
    rgba[..., :3] = color.astype(np.float16)
    rgba[..., 3] = 1.0
    src_tex = ctx.create_texture_from_numpy(rgba, fmt, w, h)
    dst_tex = ctx.create_texture_2d(fmt, w, h, shader_write=True)
    kernel = ctx.compile_kernel(name, msl)
    ctx.dispatch_sync(kernel, textures={0: src_tex, 1: dst_tex},
                      buffers={0: ctx.create_buffer_from_numpy(params)},
                      threads=(w, h, 1))
    out = read_texture_rgba16f(ctx, dst_tex, w, h)
    return out[..., :3].copy()


def _assert_within_fp16_ulp(out: np.ndarray, ref: np.ndarray,
                            max_ulp: int) -> None:
    """out/ref 均为 fp16 量化值的 float32 载体；允许 ≤ max_ulp ulp。"""
    out16 = out.astype(np.float16)
    ref16 = ref.astype(np.float16)
    err = np.abs(out16.astype(np.float32) - ref16.astype(np.float32))
    budget = _fp16_spacing(ref16) * float(max_ulp)
    bad = err > budget
    n_bad = int(bad.sum())
    assert n_bad == 0, (
        f"{n_bad} 像素超出 {max_ulp} fp16 ulp"
        f"（最大 err={err.max():.4g} @ 该处预算="
        f"{budget[bad].max() if n_bad else float('nan'):.4g}）"
    )


@requires_metal
class TestFilmGrainDevice:
    # (名称, monochrome, seed, visible_rect, size, tex_size)
    CONFIGS = [
        ("回退分支/彩色/seed0.42", False, 0.42, (0.0, 0.0, 0.0, 0.0), 2.5, (37.0, 23.0)),
        ("视口分支/彩色/seed0.0", False, 0.0, (4.0, 6.0, 37.0, 23.0), 2.5, (37.0, 23.0)),
        ("视口分支/单色/seed7.0", True, 7.0, (2.0, 3.0, 33.0, 17.0), 8.0, (37.0, 23.0)),
        ("2048画布/局部视口/负原点", False, 0.42, (-50.0, -30.0, 37.0, 23.0), 4.0, (2048.0, 2048.0)),
    ]

    def _run(self, ctx, cfg, color=None):
        _, mono, seed, rect, size, tex = cfg
        color = color if color is not None else _random_color(23, 37, seed=7)
        kw = dict(intensity=0.35, size=size, monochrome=mono, shadows=0.8,
                  midtones=1.0, highlights=0.5, seed=seed,
                  visible_rect=rect, tex_size=tex)
        params = build_film_grain_params(**kw)
        out = _render(ctx, METAL_FILM_GRAIN_MSL, "film_grain", color, params)
        return out, film_grain_reference(_quantize_input(color), **kw)

    @pytest.mark.parametrize("cfg", CONFIGS, ids=[c[0] for c in CONFIGS])
    def test_matches_reference_within_1_fp16_ulp(self, cfg) -> None:
        ctx = MetalComputeContext()
        out, ref = self._run(ctx, cfg)
        _assert_within_fp16_ulp(out, ref, max_ulp=1)

    def test_determinism_bitwise(self) -> None:
        ctx = MetalComputeContext()
        cfg = self.CONFIGS[0]
        out1, _ = self._run(ctx, cfg)
        out2, _ = self._run(ctx, cfg)
        # 固定 seed 同设备两次运行位级一致（P6-06 验收核心）
        np.testing.assert_array_equal(
            out1.astype(np.float16).view(np.uint16),
            out2.astype(np.float16).view(np.uint16),
        )

    def test_seed_changes_output(self) -> None:
        ctx = MetalComputeContext()
        color = _random_color(23, 37, seed=7)
        kw0 = dict(intensity=0.35, size=2.5, monochrome=False, shadows=0.8,
                   midtones=1.0, highlights=0.5, visible_rect=(0, 0, 0, 0),
                   tex_size=(37.0, 23.0))
        a = _render(ctx, METAL_FILM_GRAIN_MSL, "film_grain", color,
                    build_film_grain_params(seed=0.42, **kw0))
        b = _render(ctx, METAL_FILM_GRAIN_MSL, "film_grain", color,
                    build_film_grain_params(seed=7.0, **kw0))
        assert not np.array_equal(a, b)


@requires_metal
class TestSoftClipDevice:
    CONFIGS = [
        ("exposure+toe/shoulder", dict(
            high=1.0, white_point=2.0, shoulder_strength=0.7,
            low=0.0, black_point=-1.0, toe_strength=0.6,
            exposure=1.0, contrast=0.0)),
        ("contrast+大动态", dict(
            high=1.2, white_point=4.0, shoulder_strength=0.5,
            low=-0.1, black_point=-2.0, toe_strength=0.3,
            exposure=0.0, contrast=0.4)),
        ("负contrast+黑点高于暗部阈", dict(
            high=0.8, white_point=1.6, shoulder_strength=0.25,
            low=0.0, black_point=0.1, toe_strength=0.9,
            exposure=-0.5, contrast=-0.3)),
    ]

    @pytest.mark.parametrize("label,kw", CONFIGS, ids=[c[0] for c in CONFIGS])
    def test_matches_reference_within_2_fp16_ulp(self, label, kw) -> None:
        # pow 的 libm/air ulp 差异 → 本机实测 1 ulp，门禁放宽到 2
        ctx = MetalComputeContext()
        color = _random_color(32, 24, seed=11, lo=-0.9, hi=6.0)
        color[0, 0] = (50.0, -0.95, 0.0)  # 极端 HDR + 深负值
        params = build_soft_clip_params(**kw)
        out = _render(ctx, METAL_SOFT_CLIP_MSL, "soft_clip", color, params)
        ref = soft_clip_reference(_quantize_input(color), **kw)
        _assert_within_fp16_ulp(out, ref, max_ulp=2)

    def test_identity_params_bitwise_passthrough(self) -> None:
        """恒等参数（压缩器全部不触发）→ 位级直通，证明无隐藏 clamp/舍入。"""
        ctx = MetalComputeContext()
        color = _random_color(16, 16, seed=3, lo=-0.5, hi=3.0)
        params = build_soft_clip_params(
            high=1e6, white_point=2e6, shoulder_strength=0.5,
            low=-10.0, black_point=-20.0, toe_strength=0.5,
            exposure=0.0, contrast=0.0,
        )
        out = _render(ctx, METAL_SOFT_CLIP_MSL, "soft_clip", color, params)
        np.testing.assert_array_equal(
            out.astype(np.float16).view(np.uint16),
            color.astype(np.float16).view(np.uint16),
        )
