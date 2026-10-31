"""TRAW CFA GPU debayer 像素级测试（standalone ModernGL；批 2）。

GPU 输出 vs NumPy 参考实现（同一双线性 RGGB + PWL-log12 逆曲线 + WB/EI
语义）逐像素比对；附 LOG0 曲线常量与 Python 逆函数的往返钉。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

import moderngl

from src.shared.video.cfa_gpu_debayer import (
    TRAW_CURVE_FW_E,
    TRAW_CURVE_TOE_E,
    CfaGpuDebayer,
    traw_log12_constants,
)

pytestmark = pytest.mark.gpu_renderer


# ---------------------------------------------------------------------------
# NumPy 参考实现（与 shader 同语义；逐分支双线性 + LOG0 逆）
# ---------------------------------------------------------------------------

_CURVE_A, _CURVE_K, _CURVE_C0 = traw_log12_constants()


def _log12_inv_ref(c: np.ndarray) -> np.ndarray:
    x = np.where(c < _CURVE_A,
                 (c - _CURVE_C0) / _CURVE_K,
                 TRAW_CURVE_TOE_E * (np.exp2(c / _CURVE_A) - 1.0))
    return np.clip(x, 0.0, TRAW_CURVE_FW_E)


def _fetch(plane: np.ndarray, j: int, i: int) -> float:
    jj = min(max(j, 0), plane.shape[0] - 1)
    ii = min(max(i, 0), plane.shape[1] - 1)
    return float(plane[jj, ii])


def _debayer_reference(planes, fw: int, fh: int, wb=(1.0, 1.0, 1.0),
                       exposure_stops=0.0, ootf=True) -> np.ndarray:
    r_p, gr_p, gb_p, b_p = (p.astype(np.float64) for p in planes)
    out = np.zeros((fh, fw, 3), dtype=np.float64)
    for y in range(fh):
        j = y >> 1
        ey = y & 1
        for x in range(fw):
            i = x >> 1
            ex = x & 1
            if ex == 0 and ey == 0:  # R 位点
                r = _fetch(r_p, j, i)
                g = 0.5 * (_fetch(gr_p, j, i) + _fetch(gb_p, j, i))
                b = 0.25 * (_fetch(b_p, j, i) + _fetch(b_p, j, i - 1)
                            + _fetch(b_p, j - 1, i) + _fetch(b_p, j - 1, i - 1))
            elif ex == 1 and ey == 0:  # Gr 位点
                r = 0.5 * (_fetch(r_p, j, i) + _fetch(r_p, j, i + 1))
                g = _fetch(gr_p, j, i)
                b = 0.5 * (_fetch(b_p, j, i) + _fetch(b_p, j - 1, i))
            elif ex == 0 and ey == 1:  # Gb 位点
                r = 0.5 * (_fetch(r_p, j, i) + _fetch(r_p, j + 1, i))
                g = _fetch(gb_p, j, i)
                b = 0.5 * (_fetch(b_p, j, i) + _fetch(b_p, j, i - 1))
            else:  # B 位点
                b = _fetch(b_p, j, i)
                g = 0.5 * (_fetch(gr_p, j, i) + _fetch(gb_p, j, i))
                r = 0.25 * (_fetch(r_p, j, i) + _fetch(r_p, j, i + 1)
                            + _fetch(r_p, j + 1, i) + _fetch(r_p, j + 1, i + 1))
            lin = np.array([
                _log12_inv_ref(np.array([r]))[0] * wb[0],
                _log12_inv_ref(np.array([g]))[0] * wb[1],
                _log12_inv_ref(np.array([b]))[0] * wb[2],
            ])
            lin = lin * (2.0 ** exposure_stops) / TRAW_CURVE_FW_E
            lin = np.clip(lin, 0.0, 1.0)
            if ootf:
                lin = lin ** (1.0 / 2.2)
            out[y, x] = lin
    return out


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gl_context():
    try:
        ctx = moderngl.create_standalone_context()
        yield ctx
    except Exception as e:  # pragma: no cover - CI 无 GPU 时
        pytest.skip(f"Failed to create OpenGL context: {e}")


@pytest.fixture
def debayer(gl_context):
    d = CfaGpuDebayer(gl_context)
    yield d
    d.release()


def _synthetic_rggb(w=32, h=24, seed=42):
    """合成 RGGB 相位平面（渐变+伪噪声，uint16 12-bit 域）。"""
    rng = np.random.default_rng(seed)
    pw, ph = w // 2, h // 2
    planes = []
    for k in range(4):
        grad = (np.arange(ph)[:, None] * 37 + np.arange(pw)[None, :] * 53
                + k * 311) % 3800
        noise = rng.integers(-40, 40, (ph, pw))
        planes.append(np.clip(400 + k * 250 + grad + noise, 0, 4095)
                      .astype(np.uint16))
    return planes


def _read_fbo(ctx, fbo) -> np.ndarray:
    # RGBA16F FBO 必须按 f2 读回（half 精度即 shader 输出精度），再转 f4 比较
    raw = fbo.read(components=4, dtype='f2')
    rgba = np.frombuffer(raw, dtype=np.float16).reshape(fbo.height, fbo.width, 4)
    return rgba[..., :3].astype(np.float64)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestTrawCurveConstants:
    def test_constants_match_frozen_formula(self) -> None:
        """§3.2 冻结参数：A/K/C0 与 tools/bench_traw_pilot.py 同源。"""
        a, k, c0 = traw_log12_constants()
        assert a == pytest.approx(372.27, abs=0.01)
        assert k == pytest.approx(33.57, abs=0.01)
        assert c0 == pytest.approx(103.73, abs=0.01)

    def test_inverse_roundtrip_within_quantization(self) -> None:
        """逆曲线往返误差 ≤ 半码（高端 1 码 ≈ 30e，计划 §1.3）。"""
        xs = np.linspace(0.0, TRAW_CURVE_FW_E, 50000)
        codes = np.where(xs < TRAW_CURVE_TOE_E,
                         _CURVE_C0 + _CURVE_K * xs,
                         _CURVE_A * np.log2(1.0 + xs / TRAW_CURVE_TOE_E))
        codes = np.clip(np.rint(codes), 0, 4095)
        back = _log12_inv_ref(codes)
        assert np.abs(back - xs).max() <= 16.0


class TestCfaGpuDebayer:
    def test_matches_reference_unity_wb(self, debayer, gl_context) -> None:
        w, h = 32, 24
        planes = _synthetic_rggb(w, h)
        fbo = gl_context.simple_framebuffer((w, h), components=4, dtype='f2')
        fbo.use()
        fbo.clear(0.0, 0.0, 0.0, 1.0)
        debayer.upload_and_render(planes, w, h, fbo, display_ootf=False)
        got = _read_fbo(gl_context, fbo).astype(np.float64)
        ref = _debayer_reference(planes, w, h, ootf=False)
        # half-float 存储 + 三通道读回：容差按 half 精度（~1e-3）
        assert np.abs(got - ref).max() <= 2e-3, (
            np.abs(got - ref).max(), got[::4, ::4], ref[::4, ::4])

    def test_wb_and_exposure_live_effect(self, debayer, gl_context) -> None:
        """WB gain / EI 改变输出（uniform 路径，零重解码语义）。"""
        w, h = 16, 16
        planes = _synthetic_rggb(w, h, seed=7)
        fbo = gl_context.simple_framebuffer((w, h), components=4, dtype='f2')
        fbo.use()
        debayer.upload_and_render(planes, w, h, fbo, display_ootf=False)
        unity = _read_fbo(gl_context, fbo).copy()
        debayer.upload_and_render(
            planes, w, h, fbo, wb_gains=(1.5, 1.0, 0.8),
            exposure_stops=1.0, display_ootf=False)
        adjusted = _read_fbo(gl_context, fbo).copy()
        # 通道增益 × 曝光复合：net = gain × 2^stops（未饱和线性域）
        # R=1.5×2=3.0x、G=1.0×2=2.0x、B=0.8×2=1.6x → 比值序 R>G>B
        ru = max(unity[..., 0].mean(), 1e-6)
        gu = max(unity[..., 1].mean(), 1e-6)
        bu = max(unity[..., 2].mean(), 1e-6)
        ratio_r = adjusted[..., 0].mean() / ru
        ratio_g = adjusted[..., 1].mean() / gu
        ratio_b = adjusted[..., 2].mean() / bu
        assert ratio_r > ratio_g > ratio_b, (ratio_r, ratio_g, ratio_b)
        assert ratio_r == pytest.approx(3.0, rel=0.25)
        assert ratio_g == pytest.approx(2.0, rel=0.2)
        assert ratio_b == pytest.approx(1.6, rel=0.25)

    def test_display_ootf_brightens_midtones(self, debayer, gl_context) -> None:
        w, h = 16, 16
        planes = _synthetic_rggb(w, h, seed=9)
        fbo = gl_context.simple_framebuffer((w, h), components=4, dtype='f2')
        fbo.use()
        debayer.upload_and_render(planes, w, h, fbo, display_ootf=False)
        lin = _read_fbo(gl_context, fbo).copy()
        debayer.upload_and_render(planes, w, h, fbo, display_ootf=True)
        ootf = _read_fbo(gl_context, fbo).copy()
        assert ootf.mean() > lin.mean()  # 线性域 OOTF 提亮中间调

    def test_odd_dimensions_clamped(self, debayer, gl_context) -> None:
        """奇数维 ceil/2 几何（36×20 → 18×10 平面）不越界、不崩。"""
        w, h = 36, 20
        planes = _synthetic_rggb(w, h, seed=11)
        planes = [p[: (h + 1) // 2, : (w + 1) // 2] for p in planes]
        fbo = gl_context.simple_framebuffer((w, h), components=4, dtype='f2')
        fbo.use()
        debayer.upload_and_render(planes, w, h, fbo)
        got = _read_fbo(gl_context, fbo)
        assert np.isfinite(got).all()

    def test_plane_size_mismatch_rejected(self, debayer, gl_context) -> None:
        planes = _synthetic_rggb(32, 24)
        fbo = gl_context.simple_framebuffer((32, 24), components=4, dtype='f2')
        fbo.use()
        with pytest.raises(ValueError, match="不符"):
            debayer.upload_and_render(planes, 64, 48, fbo)


def _debayer_reference_linear16(planes, w, h, wb=(1.0, 1.0, 1.0),
                                exposure_stops=0.0, ootf=False):
    """16-bit linear 参考实现（与 _LINEAR16_FS_TEMPLATE 同式：双线性
    RGGB + /65535 归一化，无 LOG0 逆曲线；B 位点 = Gr/Gb 平均 + R 四对角
    ——2026-09-13 复查修复后与 12-bit 模板同构）。"""
    r_p, gr_p, gb_p, b_p = planes
    pw, ph = (w + 1) // 2, (h + 1) // 2

    def _fetch(p, j, i):
        return float(p[max(0, min(ph - 1, j))][max(0, min(pw - 1, i))])

    out = np.zeros((h, w, 3))
    for y in range(h):
        for x in range(w):
            i, j = x >> 1, y >> 1
            ex, ey = x & 1, y & 1
            if ex == 0 and ey == 0:  # R 位点
                r = _fetch(r_p, j, i)
                g = 0.5 * (_fetch(gr_p, j, i) + _fetch(gb_p, j, i))
                b = 0.25 * (_fetch(b_p, j, i) + _fetch(b_p, j, i - 1)
                            + _fetch(b_p, j - 1, i) + _fetch(b_p, j - 1, i - 1))
            elif ex == 1 and ey == 0:  # Gr 位点
                r = 0.5 * (_fetch(r_p, j, i) + _fetch(r_p, j, i + 1))
                g = _fetch(gr_p, j, i)
                b = 0.5 * (_fetch(b_p, j, i) + _fetch(b_p, j - 1, i))
            elif ex == 0 and ey == 1:  # Gb 位点
                r = 0.5 * (_fetch(r_p, j, i) + _fetch(r_p, j + 1, i))
                g = _fetch(gb_p, j, i)
                b = 0.5 * (_fetch(b_p, j, i) + _fetch(b_p, j, i - 1))
            else:  # B 位点
                b = _fetch(b_p, j, i)
                g = 0.5 * (_fetch(gr_p, j, i) + _fetch(gb_p, j, i))
                r = 0.25 * (_fetch(r_p, j, i) + _fetch(r_p, j, i + 1)
                            + _fetch(r_p, j + 1, i) + _fetch(r_p, j + 1, i + 1))
            lin = np.array([r, g, b]) / 65535.0 * np.array(wb)
            lin = lin * (2.0 ** exposure_stops)
            lin = np.clip(lin, 0.0, 1.0)
            if ootf:
                lin = lin ** (1.0 / 2.2)
            out[y, x] = lin
    return out


class TestCfaGpuDebayerLinear16:
    """16-bit linear 变体（raw16-* 归档档）——2026-09-13 复查修复的回归钉：
    uniform 条件注入（此前无条件注入 LOG0 uniform → KeyError 被外层吞
    成黑屏）与 B 位点邻域（此前单 Gb + 远点 R → 拉链伪影）。"""

    def test_linear16_matches_reference(self, gl_context) -> None:
        w, h = 32, 24
        rng = np.random.default_rng(20260913)
        pw, ph = w // 2, h // 2
        planes = []
        for k in range(4):
            grad = (np.arange(ph)[:, None] * 613 + np.arange(pw)[None, :] * 997
                    + k * 13007) % 60000
            noise = rng.integers(-600, 600, (ph, pw))
            planes.append(np.clip(2000 + k * 9000 + grad + noise, 0, 65535)
                          .astype(np.uint16))
        d = CfaGpuDebayer(gl_context, bit_depth=16)
        try:
            fbo = gl_context.simple_framebuffer((w, h), components=4, dtype='f2')
            fbo.use()
            fbo.clear(0.0, 0.0, 0.0, 1.0)
            d.upload_and_render(planes, w, h, fbo, display_ootf=False)
            got = _read_fbo(gl_context, fbo).astype(np.float64)
            ref = _debayer_reference_linear16(planes, w, h)
            assert np.abs(got - ref).max() <= 2e-3, (
                np.abs(got - ref).max(), got[::6, ::6], ref[::6, ::6])
        finally:
            d.release()

    def test_linear16_wb_exposure_effect(self, gl_context) -> None:
        w, h = 16, 16
        rng = np.random.default_rng(20260914)
        pw, ph = w // 2, h // 2
        planes = [rng.integers(8000, 40000, (ph, pw)).astype(np.uint16)
                  for _ in range(4)]
        d = CfaGpuDebayer(gl_context, bit_depth=16)
        try:
            fbo = gl_context.simple_framebuffer((w, h), components=4, dtype='f2')
            fbo.use()
            d.upload_and_render(planes, w, h, fbo, display_ootf=False)
            unity = _read_fbo(gl_context, fbo).copy()
            d.upload_and_render(planes, w, h, fbo, wb_gains=(1.4, 1.0, 0.7),
                                exposure_stops=1.0, display_ootf=False)
            adj = _read_fbo(gl_context, fbo).copy()
            assert (adj > unity - 1e-6).all()  # 增益/曝光不压暗任何通道
            assert adj[..., 0].mean() / max(unity[..., 0].mean(), 1e-6) > 2.0
            assert adj[..., 2].mean() / max(unity[..., 2].mean(), 1e-6) < 2.0
        finally:
            d.release()
