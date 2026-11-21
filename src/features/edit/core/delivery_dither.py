"""交付层 Dither（FX-23 · 2026-11-21 计划第 5 项）。

整数 PCM 交付的最终量化前一次性应用；浮点交付禁用。合同：

- **1 LSB 标尺**：``LSB = 2 ** -(bit_depth - 1)``（16-bit → 2⁻¹⁵、
  24-bit → 2⁻²³，与计划的量化对照口径一致）；
- **TPDF**：两个独立均匀随机量相加（峰峰 2 LSB，三角形概率分布），
  ``shape="shaped"``（噪声塑形）留待后续批次并显式报错（须校验滤波
  状态后再放行）；
- **舍入与饱和**：dither 后先饱和到有符号可表示域 ``[-1, 1-LSB]``，
  再在 LSB 网格上四舍五入（half-away-from-zero）——无削顶溢出；
- **固定 seed**：``np.random.default_rng(seed)``，同一交付设置/声道
  确定性复现；``seed=None`` 允许非确定（不建议交付使用）；
- **静音空白**：``dither_silence=False``（默认）时精确零样本保持
  数字黑；True 时全量加噪；
- **默认关闭**：所有入口 ``dither=None`` 时逐样本原样返回；
- 仅整数字长（16/24）有效；``bit_depth=32``（float）直接返回原样。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

SUPPORTED_BIT_DEPTHS = (16, 24)


def quantize_to_bit_depth(pcm: np.ndarray, bit_depth: int) -> np.ndarray:
    """按 1 LSB 标尺量化到整数字长网格（饱和到 [-1, 1-LSB]）。

    交付层在 dither 之后的最后一次舍入；浮点位深（32）原样返回。
    """
    if bit_depth not in SUPPORTED_BIT_DEPTHS:
        return np.ascontiguousarray(pcm, dtype=np.float32)
    lsb = 2.0 ** -(int(bit_depth) - 1)
    clipped = np.clip(pcm, -1.0, 1.0 - lsb)
    scaled = clipped / lsb
    # 四舍五入（half-away-from-zero：0.5 LSB 远离零进位）。
    steps = np.where(scaled >= 0,
                     np.floor(scaled + 0.5), np.ceil(scaled - 0.5))
    return np.ascontiguousarray(
        np.clip(steps * lsb, -1.0, 1.0 - lsb), dtype=np.float32)


def apply_dither(
    pcm: np.ndarray,
    bit_depth: int,
    *,
    seed: Optional[int] = None,
    shape: str = "tpdf",
    dither_silence: bool = False,
) -> np.ndarray:
    """交付最终量化前的 TPDF dither（默认关闭由调用方控制）。

    返回加噪后的 float PCM（尚未量化——量化由 :func:`quantize_to_bit_depth`
    或编码器完成；两步需在同一交付入口按序调用一次）。
    """
    data = np.asarray(pcm, dtype=np.float32)
    if bit_depth not in SUPPORTED_BIT_DEPTHS:
        # 浮点 WAV / 未声明字长：关闭 dither（避免导出两次叠加）。
        return data
    if shape != "tpdf":
        raise ValueError(
            f"unsupported dither shape: {shape!r} (noise shaping is a "
            "later batch; validate filter state before enabling)")
    if data.size == 0:
        return data

    lsb = 2.0 ** -(int(bit_depth) - 1)
    rng = np.random.default_rng(seed)
    u1 = rng.uniform(-0.5, 0.5, size=data.shape)
    u2 = rng.uniform(-0.5, 0.5, size=data.shape)
    noise = (u1 + u2).astype(np.float32) * lsb  # TPDF：峰峰 2 LSB
    if not dither_silence:
        # 精确零样本保持数字黑（空白不哈希成噪声）。
        noise = np.where(data == 0.0, np.float32(0.0), noise)
    dithered = data + noise
    # 无削顶溢出：饱和到有符号可表示域后再交给量化。
    return np.ascontiguousarray(
        np.clip(dithered, -1.0, 1.0 - lsb), dtype=np.float32)


class DitherSettings:
    """交付 dither 设置（工程交付设置持久化的最小载体）。"""

    __slots__ = ("enabled", "bit_depth", "shape", "seed", "dither_silence")

    def __init__(self, enabled: bool = False, bit_depth: int = 24,
                 shape: str = "tpdf", seed: Optional[int] = None,
                 dither_silence: bool = False) -> None:
        self.enabled = bool(enabled)
        self.bit_depth = int(bit_depth)
        self.shape = str(shape)
        self.seed = seed
        self.dither_silence = bool(dither_silence)

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "bit_depth": self.bit_depth,
                "shape": self.shape, "seed": self.seed,
                "dither_silence": self.dither_silence}

    @classmethod
    def from_dict(cls, data: dict) -> "DitherSettings":
        return cls(
            enabled=bool(data.get("enabled", False)),
            bit_depth=int(data.get("bit_depth", 24)),
            shape=str(data.get("shape", "tpdf") or "tpdf"),
            seed=(int(data["seed"]) if data.get("seed") is not None
                  else None),
            dither_silence=bool(data.get("dither_silence", False)),
        )

    def apply(self, pcm: np.ndarray) -> np.ndarray:
        """按设置应用 dither + 最终量化；关闭时仅按字长量化。"""
        if not self.enabled:
            return quantize_to_bit_depth(pcm, self.bit_depth)
        dithered = apply_dither(
            pcm, self.bit_depth, seed=self.seed, shape=self.shape,
            dither_silence=self.dither_silence)
        return quantize_to_bit_depth(dithered, self.bit_depth)
