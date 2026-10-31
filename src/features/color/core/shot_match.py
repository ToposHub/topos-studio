"""F4.4 · 批量镜头匹配——可解释曝光/白平衡/分布匹配建议与批量提交。

现状（调研）：``color/ai/color_context.py`` 只有类型契约
（ShotMatchSuggestion/Report，零生产消费者）；本模块补齐**可解释算法
与批量应用**，并把 F4.1（微调栈）/F4.2（命名版本回退锚）接成闭环。

算法（全部可解释，无模型）：
- **曝光**：中性色基准（低饱和像素簇均值，灰世界锚）亮度比 → stops =
  log2(ref/shot)，夹 [-2, +2]（极端不硬拉）；
- **白平衡**：参考/镜头的通道比（对中性簇）→ 每通道增益，映射为
  temp/tint 参数（映射函数与 GradingMath 数值对拍锚定）；
- **饱和**：色度均值比，夹 [0.5, 1.5]；
- **置信度**：中性簇占比 + 肤色簇存在 + 亮度分布重叠（p50/p95 接近度）；
- **排除守卫**（不强拉同分布）：剪影（中性亮度<0.01 或中性簇<1%）、
  极端色偏（通道增益>2.5）、HDR 动态范围差异（peak 比>4 倍且一方
  超 4.0）、裁剪比例>15% 降置信、锁定镜头（F4.2 锁/F4.1 hero）、
  品牌色（提供 brand_hue_bins 时跳过 WB 保色相，曝光仍可对）。

适用边界声明：CFR 同场景近似（灰世界+肤色锚），非光谱级匹配；跨场景/
极端光比建议走人工确认（confidence 阈值 0.75 之下必须确认）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

from src.features.color.ai.color_context import (
    GradeOperation,
    OP_PRIMARY,
    OP_WHITE_BALANCE,
    ShotMatchReport,
    ShotMatchSuggestion,
)

#: 中性簇判定：通道间最大相对离散 < 8%
_NEUTRAL_SAT_MAX = 0.08
#: 肤色簇（Rec709 Y 行 hue 近似）：亮度 0.08..0.75 且 R>G>B 顺序
_SKIN_LUMA_LO, _SKIN_LUMA_HI = 0.08, 0.75
_SKIN_CHROMA_MAX = 0.75
_EXPOSURE_LIMIT_STOPS = 2.0
_WB_GAIN_LIMIT = 2.5
_SAT_RATIO_LIMIT = 1.5


@dataclass(frozen=True)
class ShotStatistics:
    """一镜的统计测量（scene-linear float RGB [H,W,3] 一次求值）。"""

    clip_id: str
    neutral_mean: Tuple[float, float, float]   # 中性簇均值 RGB（原始域）
    neutral_fraction: float                    # 中性簇占比 [0,1]
    skin_mean: Optional[Tuple[float, float, float]]  # 肤色簇均值（None=样本不足）
    skin_fraction: float
    channel_mean: Tuple[float, float, float]
    chroma_mean: float                         # 平均色度（通道间离散）
    luma_p50: float
    luma_p95: float
    peak_luma: float
    clipping_ratio: float
    # 校正域中性亮度（粗估 WB 后）——曝光锚不受色偏污染
    neutral_luma_corrected: float = 0.0

    @property
    def neutral_luma(self) -> float:
        """曝光锚：校正域中性亮度（缺省回退原始域 luma）。"""
        if self.neutral_luma_corrected > 0.0:
            return self.neutral_luma_corrected
        r, g, b = self.neutral_mean
        return 0.2126 * r + 0.7152 * g + 0.0722 * b


def measure_shot(frame: np.ndarray, clip_id: str = "") -> ShotStatistics:
    """从 scene-linear float RGB 帧测统计（中性/肤色簇基准）。

    中性簇经**两遍灰世界**：先按全帧通道均值粗估 WB 并归一，再在
    校正域找低色度像素（否则有暖/冷偏的灰墙本身不再"中性"，中性
    锚对它要纠正的偏差不不变——实测 +0.5 stop 恢复偏差 0.12）。
    报告的 neutral_mean 保留**原始域**均值（WB 增益的来源），
    neutral_luma 用校正域（曝光锚不受色偏污染）。
    """
    if frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError(f"expected (H, W, 3) frame, got {frame.shape}")
    rgb = np.asarray(frame[..., :3], dtype=np.float64)
    # 两遍灰世界：粗估 WB → 校正域找中性簇
    # 迭代灰世界（2 轮）：全帧均值被彩色内容（肤色/高光块）带偏，
    # 直接除会把灰墙整体染色（实测墙 chroma 恒 0.095 全军覆没）；
    # 第 1 轮粗估 → 取低色度候选 → 用候选均值精估 → 第 2 轮校正。
    flat = rgb.reshape(-1, 3)
    wb_est = flat.mean(axis=0)
    wb_est = wb_est / max(wb_est.mean(), 1e-9)
    for _ in range(2):
        work = flat / np.maximum(wb_est, 1e-6)
        cm = work.max(axis=-1)
        cn = work.min(axis=-1)
        ch = np.where(cm > 1e-6, (cm - cn) / np.maximum(cm, 1e-6), 0.0)
        candidates = flat[ch < 0.20]
        if candidates.shape[0] >= flat.shape[0] // 100:
            refined = candidates.mean(axis=0)
            wb_est = refined / max(refined.mean(), 1e-9)
    rough_wb = wb_est
    corrected = rgb / np.maximum(rough_wb, 1e-6)
    # 5×5 均值去噪后再判色度：18% 灰墙 +2% 独立噪声的逐像素色度
    # ~0.2（噪声主导），不模糊则中性簇为零、锚失效（实测）
    try:
        import cv2
        smoothed = cv2.blur(corrected.astype(np.float32), (5, 5)).astype(
            np.float64)
    except Exception:  # noqa: BLE001 — cv2 缺失回退原始
        smoothed = corrected
    c_max = smoothed.max(axis=-1)
    c_min = smoothed.min(axis=-1)
    chroma_corr = np.where(
        c_max > 1e-6, (c_max - c_min) / np.maximum(c_max, 1e-6), 0.0)
    neutral_mask = chroma_corr < _NEUTRAL_SAT_MAX
    neutral_fraction = float(neutral_mask.mean())
    if neutral_fraction > 1e-4:
        neutral = rgb[neutral_mask].mean(axis=0)
        neutral_corrected = corrected[neutral_mask].mean(axis=0)
    else:
        neutral = rgb.reshape(-1, 3).mean(axis=0)
        neutral_corrected = neutral / rough_wb
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    # 色度统计用校正域（WB 偏色不污染饱和锚——偏暖帧的"高色度"
    # 是色偏不是饱和）
    channel_max = smoothed.max(axis=-1)
    channel_min = smoothed.min(axis=-1)
    chroma = np.where(
        channel_max > 1e-6,
        (channel_max - channel_min) / np.maximum(channel_max, 1e-6), 0.0)
    cr, cg, cb = (smoothed[..., 0], smoothed[..., 1], smoothed[..., 2])
    skin_mask = (
        (luma > _SKIN_LUMA_LO) & (luma < _SKIN_LUMA_HI)
        & (cr > cg) & (cg > cb)
        & (chroma >= _NEUTRAL_SAT_MAX) & (chroma < _SKIN_CHROMA_MAX))
    skin_fraction = float(skin_mask.mean())
    skin = (
        tuple(rgb[skin_mask].mean(axis=0))
        if skin_fraction > 0.005 else None)
    nl = 0.2126 * float(neutral_corrected[0]) \
        + 0.7152 * float(neutral_corrected[1]) \
        + 0.0722 * float(neutral_corrected[2])
    return ShotStatistics(
        clip_id=clip_id,
        neutral_mean=(float(neutral[0]), float(neutral[1]), float(neutral[2])),
        neutral_fraction=neutral_fraction,
        neutral_luma_corrected=nl,
        skin_mean=(float(skin[0]), float(skin[1]), float(skin[2]))
        if skin is not None else None,
        skin_fraction=skin_fraction,
        channel_mean=(float(r.mean()), float(g.mean()), float(b.mean())),
        chroma_mean=float(chroma.mean()),
        luma_p50=float(np.percentile(luma, 50)),
        luma_p95=float(np.percentile(luma, 95)),
        peak_luma=float(luma.max()),
        clipping_ratio=float((luma >= 0.99).mean()),
    )


@dataclass(frozen=True)
class ShotMatchResult:
    """单镜头 → 参考的可解释匹配量（未映射到调色参数）。"""

    clip_id: str
    exposure_stops: float
    wb_gains: Tuple[float, float, float]   # 期望的 R/G/B 通道增益
    saturation_ratio: float
    confidence: float
    exclude_reason: str = ""

    def explain(self) -> str:
        return (f"exposure {self.exposure_stops:+.2f} stops, "
                f"WB gains ({self.wb_gains[0]:.2f}/{self.wb_gains[1]:.2f}/"
                f"{self.wb_gains[2]:.2f}), sat ×{self.saturation_ratio:.2f}, "
                f"confidence {self.confidence:.2f}"
                + (f" [excluded: {self.exclude_reason}]" if self.exclude_reason else ""))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def match_shot(
    stats: ShotStatistics,
    reference: ShotStatistics,
    *,
    locked: bool = False,
    brand_hue_protected: bool = False,
) -> ShotMatchResult:
    """可解释匹配：曝光 stops / WB 通道增益 / 饱和比 + 置信与排除。"""
    exclude = ""
    if locked:
        exclude = "镜头已锁定（F4.2 锁 / hero 保护）"
    elif stats.neutral_fraction < 0.01 or stats.neutral_luma < 0.01:
        exclude = "剪影/中性基准不足，不强拉同分布"
    elif reference.neutral_luma < 1e-6:
        exclude = "参考中性基准无效"

    ref_neutral = max(reference.neutral_luma, 1e-6)
    shot_neutral = max(stats.neutral_luma, 1e-6)
    exposure = _clamp(math.log2(ref_neutral / shot_neutral),
                      -_EXPOSURE_LIMIT_STOPS, _EXPOSURE_LIMIT_STOPS)

    # WB：以中性簇通道比为锚（参考通道比 / 镜头通道比）
    wb_gains = []
    for ch in range(3):
        ref_ratio = reference.neutral_mean[ch] / ref_neutral
        shot_ratio = stats.neutral_mean[ch] / shot_neutral
        gain = _clamp(
            ref_ratio / max(shot_ratio, 1e-4),
            1.0 / _WB_GAIN_LIMIT, _WB_GAIN_LIMIT)
        wb_gains.append(gain)
    if max(wb_gains) / max(min(wb_gains), 1e-6) > _WB_GAIN_LIMIT and not exclude:
        exclude = "极端光色（通道增益超界），需人工"
    if brand_hue_protected:
        wb_gains = [1.0, 1.0, 1.0]  # 品牌色保护：跳过 WB 保色相

    sat_ratio = _clamp(
        max(reference.chroma_mean, 1e-4) / max(stats.chroma_mean, 1e-4),
        1.0 / _SAT_RATIO_LIMIT, _SAT_RATIO_LIMIT)

    # HDR 动态范围差异：一方 peak>4.0 且比另一方高 4 倍 → 不强拉
    peak_ratio = max(stats.peak_luma / max(reference.peak_luma, 1e-6),
                     reference.peak_luma / max(stats.peak_luma, 1e-6))
    if (max(stats.peak_luma, reference.peak_luma) > 4.0 and peak_ratio > 4.0
            and not exclude):
        exclude = "HDR 动态范围差异，不强行同分布"

    # 置信度：中性簇占比 + 肤色存在 + 分布重叠（p50/p95 接近度）
    neutral_term = _clamp(stats.neutral_fraction / 0.25, 0.0, 1.0) * 0.5
    skin_term = 0.2 if (stats.skin_mean and reference.skin_mean) else 0.0
    dist_terms = []
    for a, b in ((stats.luma_p50, reference.luma_p50),
                 (stats.luma_p95, reference.luma_p95)):
        denom = max(abs(a), abs(b), 1e-3)
        dist_terms.append(1.0 - min(abs(a - b) / denom, 1.0))
    overlap = 0.3 * dist_terms[0] + 0.3 * dist_terms[1] if dist_terms else 0.0
    clip_penalty = 0.2 if stats.clipping_ratio > 0.15 else 0.0
    confidence = _clamp(neutral_term + skin_term + overlap - clip_penalty,
                        0.0, 1.0)
    if exclude:
        confidence = min(confidence, 0.5)  # 被排除项必须走人工确认
    return ShotMatchResult(
        clip_id=stats.clip_id, exposure_stops=exposure,
        wb_gains=(wb_gains[0], wb_gains[1], wb_gains[2]),
        saturation_ratio=sat_ratio, confidence=confidence,
        exclude_reason=exclude)


def deltas_to_grading_params(
    exposure_stops: float,
    wb_gains: Sequence[float],
    saturation_ratio: float,
) -> Dict[str, Any]:
    """匹配量 → GradingMath 参数域（单一映射点，测试数值锚定）。

    映射（与 the_decompiler.GratingMath.apply_grading 的公式互逆）：
    - exposure 参数 e：stops>0 → e = +100·sqrt(stops/6)；<0 对称负值
      （GradingMath: stops = sign(e)·(e/100)²·6）；
    - temp = 200·(g_r − 1)；tint = 200·(1 − g_g)（wb 乘子
      [1+t/200, 1−tint/200, 1−t/200] 的逆）；
    - saturation 参数 = 100·(ratio − 1)（近似线性域）。
    """
    stops = float(exposure_stops)
    if abs(stops) < 1e-4:
        exposure_param = 0.0
    else:
        exposure_param = math.copysign(
            100.0 * math.sqrt(abs(stops) / 6.0), stops)
    temp = _clamp(200.0 * (float(wb_gains[0]) - 1.0), -100.0, 100.0)
    tint = _clamp(200.0 * (1.0 - float(wb_gains[1])), -100.0, 100.0)
    saturation = _clamp(100.0 * (float(saturation_ratio) - 1.0), -50.0, 50.0)
    return {
        "exposure": exposure_param, "temp": temp, "tint": tint,
        "saturation": saturation,
    }


def plan_shot_match(
    reference: ShotStatistics,
    shots: Sequence[ShotStatistics],
    *,
    locked_clip_ids: Sequence[str] = (),
    brand_hue_clip_ids: Sequence[str] = (),
) -> ShotMatchReport:
    """批量规划：逐镜头建议（复用 color_context 契约类型）。"""
    locked = set(locked_clip_ids)
    brand = set(brand_hue_clip_ids)
    suggestions = []
    for stats in shots:
        result = match_shot(
            stats, reference,
            locked=stats.clip_id in locked,
            brand_hue_protected=stats.clip_id in brand)
        if result.exclude_reason:
            suggestions.append(ShotMatchSuggestion(
                clip_id=result.clip_id,
                reference_clip_id=reference.clip_id,
                confidence=result.confidence,
                suggested_operations=(),
                excluded=bool(result.exclude_reason.startswith(
                    ("镜头已锁定", "剪影", "极端光色", "HDR"))),
                exclude_reason=result.exclude_reason))
            continue
        params = deltas_to_grading_params(
            result.exposure_stops, result.wb_gains, result.saturation_ratio)
        ops = []
        if abs(params["temp"]) > 0.5 or abs(params["tint"]) > 0.5:
            ops.append(GradeOperation(
                op_id=f"wb-{result.clip_id}", kind=OP_WHITE_BALANCE,
                clip_id=result.clip_id,
                parameters={"temp": params["temp"], "tint": params["tint"]},
                label=f"WB: temp {params['temp']:+.1f} tint {params['tint']:+.1f}"))
        primary = {k: v for k, v in params.items()
                   if k in ("exposure", "saturation")
                   and abs(v) > 0.5}
        if primary:
            ops.append(GradeOperation(
                op_id=f"primary-{result.clip_id}", kind=OP_PRIMARY,
                clip_id=result.clip_id, parameters=primary,
                label=", ".join(f"{k} {v:+.1f}" for k, v in primary.items())))
        suggestions.append(ShotMatchSuggestion(
            clip_id=result.clip_id,
            reference_clip_id=reference.clip_id,
            confidence=result.confidence,
            suggested_operations=tuple(ops),
            exposure_delta=result.exposure_stops,
            wb_delta=params.get("temp"),
            saturation_delta=params.get("saturation")))
    return ShotMatchReport(
        reference_clip_id=reference.clip_id, suggestions=tuple(suggestions))


__all__ = [
    "ShotStatistics",
    "ShotMatchResult",
    "measure_shot",
    "match_shot",
    "deltas_to_grading_params",
    "plan_shot_match",
]
