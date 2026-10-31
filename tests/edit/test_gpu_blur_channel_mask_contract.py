"""GPU separable blur 的掩码/mix 契约回归（channel-mask contract）.

历史缺陷（2026-09-19 自查发现）：H/V 可分离模糊的 V pass 把
``original``（掩码/mix 的参考图）采样自 **H pass 输出**而非原始输入，
导致：

- ``channel_a=False`` 时 alpha 仍被**水平方向单遍模糊**（实测 diff
  mean 52.8 / max 135 LSB）——CPU 路径（``_cpu_blur``）语义正确（原始
  alpha 直通），GPU/CPU 出现真分歧；
- ``mix<1`` 时混入多余的 H 模糊分量（mix 参考被污染）；
- fast tier（下采样质量档）在半分辨率 V pass 应用掩码，掩码关闭通道
  被半分辨率往返退化。

修复：V shader 增加 ``u_original``（unit 1 纯净输入）；fast tier 改为
半分辨率纯 H+V（全掩码 mix=1）+ 全分辨率 combine pass。

本文件把契约固化为逐像素断言（结构签名测不出水平模糊——它保均值）：
掩码关闭通道**逐位**保真、mix=0 整帧恒等、mix=0.5 满足线性分解、
fast tier 掩码关闭通道同样逐位保真且 RGB 确实被模糊。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.edit_core

W, H = 128, 96

BASE_PARAMS = {
    "blur_type": "gaussian", "radius": 4.0, "mix": 1.0,
    "edge_policy": "clamp",
    "channel_r": True, "channel_g": True, "channel_b": True,
    "channel_a": False,
}


def _material() -> np.ndarray:
    """全通道随机素材：任何通道的任何泄漏都会被逐像素断言抓住."""
    rng = np.random.default_rng(11)
    return rng.integers(0, 256, (H, W, 4), dtype=np.uint8)


def _run_blur(gl_context, params: dict, rgba: np.ndarray) -> np.ndarray:
    """经生产 EPR 管线跑一次 blur 并读回（复用 parity 测试的读回协议）."""
    from src.features.shared.effects import get_effect_catalog
    from src.features.shared.effects.effect_pass_runner import EffectPassRunner

    spec = get_effect_catalog().get_spec("blur")
    assert spec is not None, "blur spec missing"
    runner = EffectPassRunner(gl_context)
    in_tex = gl_context.texture((W, H), components=4, dtype="f1")
    in_tex.write(rgba.tobytes())
    out_glo = runner.run(spec, in_tex.glo, dict(params), W, H)
    assert out_glo != in_tex.glo, "blur was a silent no-op on GPU"
    for name in ("_scratch_a", "_scratch_b", "_scratch_c"):
        tex = getattr(runner, name, None)
        if tex is not None and tex.glo == out_glo:
            fbo = getattr(runner, name + "_fbo")
            fbo.use()
            gl_context.viewport = (0, 0, W, H)
            raw = fbo.read(components=4)
            out = np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 4).copy()
            in_tex.release()
            return out
    in_tex.release()
    raise AssertionError("output glo not found in scratch textures")


class TestBlurChannelMaskContract:
    def test_masked_off_alpha_bit_exact_normal_tier(self, gl_context) -> None:
        """channel_a=False → alpha 逐位等于输入（不得有水平模糊泄漏）."""
        rgba = _material()
        out = _run_blur(gl_context, BASE_PARAMS, rgba)
        np.testing.assert_array_equal(out[:, :, 3], rgba[:, :, 3])

    def test_masked_off_rgb_channel_bit_exact(self, gl_context) -> None:
        """channel_r=False → R 逐位等于输入（掩码语义对 RGB 同样成立）."""
        rgba = _material()
        out = _run_blur(gl_context, dict(BASE_PARAMS, channel_r=False), rgba)
        np.testing.assert_array_equal(out[:, :, 0], rgba[:, :, 0])
        # 其余掩码开启通道必须真的被模糊（防直通退化）
        assert float(np.abs(out[:, :, 1].astype(int)
                            - rgba[:, :, 1].astype(int)).mean()) > 1.0

    def test_mix_zero_is_full_frame_identity(self, gl_context) -> None:
        """mix=0 → 整帧逐位恒等（mix 参考必须是原始输入）."""
        rgba = _material()
        out = _run_blur(gl_context, dict(BASE_PARAMS, mix=0.0), rgba)
        np.testing.assert_array_equal(out, rgba)

    def test_mix_half_linear_decomposition(self, gl_context) -> None:
        """mix=0.5 输出 = 0.5*输入 + 0.5*全模糊输出（容 u8 舍入 ±1）."""
        rgba = _material()
        full = _run_blur(gl_context, dict(BASE_PARAMS, mix=1.0), rgba)
        half = _run_blur(gl_context, dict(BASE_PARAMS, mix=0.5), rgba)
        expect = 0.5 * rgba.astype(np.float32) + 0.5 * full.astype(np.float32)
        diff = np.abs(half.astype(np.float32) - expect)
        assert float(diff.max()) <= 1.0

    def test_fast_tier_masked_off_alpha_bit_exact(self, gl_context) -> None:
        """quality=fast 大半径 → 掩码关闭通道逐位保真 + RGB 真模糊.

        fast tier 的掩码/mix 在全分辨率 combine pass 应用（而非半分辨率
        V pass），掩码关闭通道不得经过半分辨率往返。
        """
        rgba = _material()
        params = dict(BASE_PARAMS, radius=16.0, quality="fast")
        out = _run_blur(gl_context, params, rgba)
        np.testing.assert_array_equal(out[:, :, 3], rgba[:, :, 3])
        assert float(np.abs(out[:, :, :3].astype(int)
                            - rgba[:, :, :3].astype(int)).mean()) > 1.0

    def test_fast_tier_mix_zero_identity(self, gl_context) -> None:
        """fast tier mix=0 → 掩码关闭通道逐位恒等（combine 参考纯净输入）."""
        rgba = _material()
        params = dict(BASE_PARAMS, radius=16.0, quality="fast", mix=0.0)
        out = _run_blur(gl_context, params, rgba)
        np.testing.assert_array_equal(out, rgba)
