"""
Channel Layout & Downmix（AM-08 阶段 4.1，声道映射）。

此前多声道处理是 naive 截断（samples[:output_channels]），直接丢弃 Center/LFE/Surround
声道，违反专业影视声道映射规范。本模块提供：

1. ChannelLayout 枚举：mono/stereo/5.1/7.1 标准布局（ITUR BS.775 / ATSC A/52）。
2. downmix_matrix()：源→目标的标准 downmix 系数矩阵（Lo/Ro 系数，非 Lt/Rt 矩阵编码）。
3. downmix()：将 (src_channels, n) PCM 按 matrix 混为 (dst_channels, n)。
4. BS.1770-4 channel weights：L/R/C=1.0, 环绕声道（Ls/Rs/Lss/Rss）=1.41（+3 dB），LFE=0（排除）。
5. av_layout_name()：ChannelLayout → PyAV layout 字符串。

设计原则（计划表 4.1）
----------------------
- 统一 ChannelLayout：导入时读取 layout，导出时写 metadata。
- 明确 5.1/7.1 downmix matrix：标准 ITU-R BS.775 downmix 系数。
- 逐声道 impulse fixture 验证每一目标声道系数。
- 拒绝未知 mapping 而非截断。

downmix 系数（ITUR BS.775-1 Lo/Ro, -3 dB center mix）
------------------------------------------------------
5.1 → stereo:
    Lo = L + 0.707*C + 0.707*LFE + 0.707*Ls
    Ro = R + 0.707*C + 0.707*LFE + 0.707*Rs
  注：LFE 通常可选（0 或 0.707）。这里采用 0.707（专业交付保守包含 LFE）。
  实际播控设备多 bypass LFE，本实现提供 include_lfe 开关（默认 False，符合 BS.775 推荐）。

7.1 → 5.1:
    L' = L + 0.707*Lss  (侧环绕并入左右)
    R' = R + 0.707*Rss
    其余通道不变（C/LFE/Ls/Rs 直通）

声道顺序（ITUR / ATSC A/52 标准 WAV/CAF 顺序）
------------------------------------------------
mono:   [M]                              (1)
stereo: [L, R]                           (2)
5.1:    [L, R, C, LFE, Ls, Rs]           (6)
7.1:    [L, R, C, LFE, Ls, Rs, Lss, Rss] (8)
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# ChannelLayout 枚举
# ─────────────────────────────────────────────────────────────────────────────

class ChannelLayout(Enum):
    """标准声道布局（ITUR BS.775 / ATSC A/52）。"""
    MONO = "mono"
    STEREO = "stereo"
    SURROUND_5_1 = "5.1"
    SURROUND_7_1 = "7.1"

    @property
    def channel_count(self) -> int:
        """该布局的声道数。"""
        return {
            ChannelLayout.MONO: 1,
            ChannelLayout.STEREO: 2,
            ChannelLayout.SURROUND_5_1: 6,
            ChannelLayout.SURROUND_7_1: 8,
        }[self]

    @property
    def channel_labels(self) -> tuple:
        """该布局的声道标签顺序（ITUR/ATSC 标准 WAV 顺序）。"""
        return {
            ChannelLayout.MONO: ("M",),
            ChannelLayout.STEREO: ("L", "R"),
            ChannelLayout.SURROUND_5_1: ("L", "R", "C", "LFE", "Ls", "Rs"),
            ChannelLayout.SURROUND_7_1: ("L", "R", "C", "LFE", "Ls", "Rs", "Lss", "Rss"),
        }[self]

    @property
    def av_layout_name(self) -> str:
        """PyAV layout 字符串。"""
        return {
            ChannelLayout.MONO: "mono",
            ChannelLayout.STEREO: "stereo",
            ChannelLayout.SURROUND_5_1: "5.1",
            ChannelLayout.SURROUND_7_1: "7.1",
        }[self]

    @staticmethod
    def from_channel_count(count: int) -> "ChannelLayout":
        """根据声道数推断布局（1=mono, 2=stereo, 6=5.1, 8=7.1）。未知抛 ValueError。"""
        mapping = {1: ChannelLayout.MONO, 2: ChannelLayout.STEREO,
                   6: ChannelLayout.SURROUND_5_1, 8: ChannelLayout.SURROUND_7_1}
        if count not in mapping:
            raise ValueError(
                f"Unknown channel count {count}: 无法推断 ChannelLayout。"
                f"支持: 1(mono)/2(stereo)/6(5.1)/8(7.1)，其他需显式指定。"
            )
        return mapping[count]

    @staticmethod
    def from_av_layout_name(name: str) -> "ChannelLayout":
        """从 PyAV layout 名称解析（"mono"/"stereo"/"5.1"/"7.1"/"5.1(side)"/...）。

        宽松解析：匹配前缀，未知抛 ValueError（拒绝而非截断）。
        """
        n = (name or "").strip().lower()
        if n in ("mono", "1c"):
            return ChannelLayout.MONO
        if n in ("stereo", "2c", "2.0"):
            return ChannelLayout.STEREO
        if n.startswith("5.1"):
            return ChannelLayout.SURROUND_5_1
        if n.startswith("7.1"):
            return ChannelLayout.SURROUND_7_1
        raise ValueError(f"Unsupported PyAV layout: {name!r}")


# ─────────────────────────────────────────────────────────────────────────────
# downmix 系数矩阵
# ─────────────────────────────────────────────────────────────────────────────

# 标准系数（sqrt(0.5) ≈ 0.7071，对应 -3 dB）
_SQRT_HALF = float(np.sqrt(0.5))


def downmix_matrix(
    src: ChannelLayout,
    dst: ChannelLayout,
    include_lfe: bool = False,
) -> np.ndarray:
    """计算 src → dst 的 downmix 系数矩阵（AM-08 阶段 4.1）。

    返回 shape=(dst_channels, src_channels) 的矩阵 M，满足 output = M @ input。
    系数遵循 ITUR BS.775-1 Lo/Ro 标准。

    Args:
        src: 源声道布局
        dst: 目标声道布局
        include_lfe: 5.1→stereo 时是否包含 LFE 贡献（默认 False，符合 BS.775 推荐）

    Returns:
        (dst_channels, src_channels) float64 矩阵

    Raises:
        ValueError: 不支持的组合（如 7.1→mono，需显式定义）
    """
    # 同布局：单位矩阵
    if src == dst:
        return np.eye(src.channel_count, dtype=np.float64)

    lfe_gain = _SQRT_HALF if include_lfe else 0.0

    # ── 5.1 → stereo ──
    # 声道顺序: [L, R, C, LFE, Ls, Rs]
    # Lo = L + 0.707*C + lfe*LFE + 0.707*Ls
    # Ro = R + 0.707*C + lfe*LFE + 0.707*Rs
    if src == ChannelLayout.SURROUND_5_1 and dst == ChannelLayout.STEREO:
        return np.array([
            # L     R     C        LFE       Ls       Rs
            [1.0,  0.0,  _SQRT_HALF, lfe_gain, _SQRT_HALF, 0.0],        # Lo
            [0.0,  1.0,  _SQRT_HALF, lfe_gain, 0.0,        _SQRT_HALF], # Ro
        ], dtype=np.float64)

    # ── 5.1 → mono ──
    # M = 0.707*L + 0.707*R + C + 0.707*Ls + 0.707*Rs (LFE 可选)
    if src == ChannelLayout.SURROUND_5_1 and dst == ChannelLayout.MONO:
        return np.array([[
            _SQRT_HALF, _SQRT_HALF, 1.0, lfe_gain, _SQRT_HALF, _SQRT_HALF,
        ]], dtype=np.float64)

    # ── stereo → mono ──
    # M = 0.5*L + 0.5*R（等功率，-6 dB 每声道）
    if src == ChannelLayout.STEREO and dst == ChannelLayout.MONO:
        return np.array([[0.5, 0.5]], dtype=np.float64)

    # ── mono → stereo ──
    # L = R = M（复制，不衰减）
    if src == ChannelLayout.MONO and dst == ChannelLayout.STEREO:
        return np.array([[1.0], [1.0]], dtype=np.float64)

    # ── 7.1 → 5.1 ──
    # 源顺序: [L, R, C, LFE, Ls, Rs, Lss, Rss]
    # 目标顺序: [L, R, C, LFE, Ls, Rs]
    # L' = L + 0.707*Lss, R' = R + 0.707*Rss, 其余直通
    if src == ChannelLayout.SURROUND_7_1 and dst == ChannelLayout.SURROUND_5_1:
        m = np.zeros((6, 8), dtype=np.float64)
        # L = L + 0.707*Lss
        m[0, 0] = 1.0; m[0, 6] = _SQRT_HALF
        # R = R + 0.707*Rss
        m[1, 1] = 1.0; m[1, 7] = _SQRT_HALF
        # C, LFE, Ls, Rs 直通
        m[2, 2] = 1.0
        m[3, 3] = 1.0
        m[4, 4] = 1.0
        m[5, 5] = 1.0
        return m

    # ── 7.1 → stereo（经 5.1 中转）──
    if src == ChannelLayout.SURROUND_7_1 and dst == ChannelLayout.STEREO:
        m71_to_51 = downmix_matrix(ChannelLayout.SURROUND_7_1, ChannelLayout.SURROUND_5_1)
        m51_to_s = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO, include_lfe)
        return m51_to_s @ m71_to_51

    # ── 7.1 → mono（经 5.1）──
    if src == ChannelLayout.SURROUND_7_1 and dst == ChannelLayout.MONO:
        m71_to_51 = downmix_matrix(ChannelLayout.SURROUND_7_1, ChannelLayout.SURROUND_5_1)
        m51_to_m = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.MONO, include_lfe)
        return m51_to_m @ m71_to_51

    # ── stereo → 5.1 / mono → 5.1 等 upmix：暂不支持（专业交付多为 downmix）──
    raise ValueError(
        f"Unsupported downmix {src.av_layout_name} → {dst.av_layout_name}: "
        f"upmix 不支持。如需 upmix 请显式指定通道映射。"
    )


# ─────────────────────────────────────────────────────────────────────────────
# downmix 执行
# ─────────────────────────────────────────────────────────────────────────────

def downmix(
    samples: np.ndarray,
    src: ChannelLayout,
    dst: ChannelLayout,
    include_lfe: bool = False,
) -> np.ndarray:
    """将 PCM 按标准 downmix 矩阵从 src 布局混为 dst 布局（AM-08 阶段 4.1）。

    Args:
        samples: (src_channels, n) PCM
        src: 源布局（声道数必须与 samples.shape[0] 匹配）
        dst: 目标布局
        include_lfe: 是否包含 LFE 贡献

    Returns:
        (dst_channels, n) PCM

    Raises:
        ValueError: 声道数不匹配或不支持的组合
    """
    if samples.ndim == 1:
        samples = samples.reshape(1, -1)
    if samples.shape[0] != src.channel_count:
        raise ValueError(
            f"声道数不匹配: samples 有 {samples.shape[0]} 声道，"
            f"但 src layout {src.av_layout_name} 期望 {src.channel_count}"
        )
    matrix = downmix_matrix(src, dst, include_lfe=include_lfe)
    # output = M @ input → (dst_channels, n)
    output = matrix @ samples.astype(np.float64)
    target_dtype = np.float32 if samples.dtype == np.float16 else samples.dtype
    if np.issubdtype(target_dtype, np.integer):
        # AUD-16101: 多声道相加可超过整型满量程，numpy astype 对越界值做
        # 模 2^N 回绕而非饱和（导出爆音/哒声），必须先 clip 到该 dtype
        # 的 [min, max] 再 cast。
        info = np.iinfo(target_dtype)
        output = np.clip(output, info.min, info.max)
    return output.astype(target_dtype)


def adapt_channels(
    samples: np.ndarray,
    target_channels: int,
    src_layout: Optional[ChannelLayout] = None,
    include_lfe: bool = False,
) -> np.ndarray:
    """自适应声道数：根据 target_channels 推断目标布局并 downmix/upmix（AM-08 阶段 4.1）。

    替代此前的 naive 截断 samples[:target_channels]。当声道数已匹配时直接返回。

    Args:
        samples: (src_channels, n) PCM
        target_channels: 目标声道数（1/2/6/8）
        src_layout: 源布局，None=从 samples.shape[0] 推断
        include_lfe: 是否包含 LFE

    Returns:
        (target_channels, n) PCM

    Raises:
        ValueError: 无法推断布局或组合不支持
    """
    if samples.ndim == 1:
        samples = samples.reshape(1, -1)
    src_count = samples.shape[0]
    if src_count == target_channels:
        return samples

    if src_layout is None:
        src_layout = ChannelLayout.from_channel_count(src_count)
    dst_layout = ChannelLayout.from_channel_count(target_channels)
    return downmix(samples, src_layout, dst_layout, include_lfe=include_lfe)


# ─────────────────────────────────────────────────────────────────────────────
# 交付路径统一入口（P1-09：布局真相贯通）
# ─────────────────────────────────────────────────────────────────────────────

def resolve_export_channels(audio_channels: int, channel_layout: str = "") -> int:
    """导出声道数的单一真相（P1-09）。

    显式布局名优先：``channel_layout`` 非空时按布局的 channel_count 决定
    声道数（与 audio_channels 不一致时以布局为准）；布局名无法识别时
    显式抛错——禁止未知布局静默猜测。空串回落 audio_channels（历史行为）。

    Raises:
        ValueError: channel_layout 非空但不是已知布局名
    """
    if not channel_layout:
        return int(audio_channels)
    layout = ChannelLayout.from_av_layout_name(str(channel_layout).strip())
    return layout.channel_count


def adapt_samples_for_delivery(
    samples: np.ndarray,
    target_channels: int,
    src_layout: Optional[ChannelLayout] = None,
    *,
    context: str = "",
) -> np.ndarray:
    """时间线导出/Stem 导出共用的声道适配入口（P1-09）。

    优先用显式 ``src_layout``（来自素材探测/decoder 容器布局）走标准
    downmix 矩阵；无显式布局时按标准声道数（1/2/6/8）推断；两者都失败
    （如 4 声道非标准）才回退安全截断/复制——回退必须留下警告日志，
    不得静默丢弃声道（P1-09 验收："禁止未知布局静默猜测"的折中：播放
    与导出不因未知布局中断，但每次回退都有日志留痕）。

    Args:
        samples: (src_channels, n) PCM（1 维按单声道处理）
        target_channels: 交付声道数
        src_layout: 源 ChannelLayout，None=按声道数推断
        context: 警告日志中的定位信息（如 "clip xxx"）
    """
    if samples.ndim == 1:
        samples = samples.reshape(1, -1)
    src_count = samples.shape[0]
    if src_count == target_channels:
        return samples

    try:
        return adapt_channels(samples, target_channels, src_layout=src_layout)
    except ValueError as exc:
        logging.getLogger(__name__).warning(
            "channel adapt fallback (%s): %dch -> %dch 截断/复制，"
            "源布局未知或组合不支持（%s）",
            context or "delivery", src_count, target_channels, exc,
        )
        if src_count > target_channels:
            return samples[:target_channels]
        # 上混回退：复制首声道（broadcast_to 只能沿前导维广播，
        # (2,n)→(6,n) 必须显式取 samples[:1] 复制）。
        # 审计加固：broadcast_to 产物只读，任何下游就地操作都会炸——
        # 转成可写数组。
        return np.array(np.broadcast_to(
            samples[:1], (target_channels, samples.shape[-1])
        ))


# ─────────────────────────────────────────────────────────────────────────────
# BS.1770-4 channel weights（响度计算用）
# ─────────────────────────────────────────────────────────────────────────────

def bs1770_channel_weights(layout: ChannelLayout) -> np.ndarray:
    """返回 BS.1770-4 标准声道权重（响度计算用，AM-08 阶段 4.1 + AM-10）。

    BS.1770-4 规定各声道对 integrated loudness 的贡献权重：
    - L, R, C: 1.0
    - 环绕声道 Ls/Rs（及 7.1 的 Lss/Rss）: 1.41（约 +3 dB，补偿环绕声道
      的低入射角；各环绕声道对称等权）
    - LFE: 0.0（排除，低频效果声道不参与响度）

    Args:
        layout: 声道布局

    Returns:
        (channel_count,) 权重数组，顺序与 channel_labels 一致
    """
    if layout == ChannelLayout.MONO:
        return np.array([1.0], dtype=np.float64)
    if layout == ChannelLayout.STEREO:
        return np.array([1.0, 1.0], dtype=np.float64)
    if layout == ChannelLayout.SURROUND_5_1:
        # [L, R, C, LFE, Ls, Rs]
        return np.array([1.0, 1.0, 1.0, 0.0, 1.41, 1.41], dtype=np.float64)
    if layout == ChannelLayout.SURROUND_7_1:
        # [L, R, C, LFE, Ls, Rs, Lss, Rss]
        return np.array(
            [1.0, 1.0, 1.0, 0.0, 1.41, 1.41, 1.41, 1.41], dtype=np.float64)
    raise ValueError(f"Unknown layout: {layout}")
