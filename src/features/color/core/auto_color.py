"""Auto Color：自动校色建议与参考帧匹配（P1-COLOR-21）。

对画面做统计分析，反推 primary 调色参数建议（DaVinci Auto Balance /
Color Match 语义）。全部数学与 ``shader_nodes.apply_grading`` 的正向
模型保持一致，应用后可用 ``predict`` 复核预测指标：

- 白平衡：乘性增益模型 ``wb=(1+t/200, 1-tint/200, 1-t/200)``，
  灰世界假设（通道均值应相等）最小二乘反解 temp/tint；
- 曝光：参数 p∈[-100,100] 映射 stops=sign(p)·(p/100)²·6，中位亮度
  对齐目标值；
- 对比度：0.18 枢轴幂曲线，按亮度高百分位反解缩放因子；
- 参考匹配：对齐当前（已调色）与参考 still 的逐通道均值/标准差，
  输出叠加在当前 grade 之上的增量建议。

建议只是参数目标值/增量，应用与否由用户在对话框中选择；应用走
单一 undo（timeline clip 单命令 / standalone 单次 history push）。
本模块纯 numpy，无 Qt 依赖。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np

#: shader 的 luma 权重（apply_grading 内部使用 0.299/0.587/0.114）
_LUMA_W = np.array([0.114, 0.587, 0.299], dtype=np.float64)  # B, G, R

#: 自动曝光目标中位亮度（SDR 常识值：中间调偏上）
TARGET_LUMA = 0.40

#: 自动对比度想把 p99 亮度推到的目标高光（保留高光余量）
TARGET_HIGHLIGHT = 0.92

#: 参数统一限幅（与 UI 滑条一致的量级）
_PARAM_LIMIT = 100.0


def _to_float01(image: np.ndarray) -> np.ndarray:
    """uint8/uint16/float 图像统一为 float 0..1（BGR 任意通道数≥3）。"""
    img = np.asarray(image)
    if img.ndim < 3 or img.shape[2] < 3:
        raise ValueError("need an HxWx3 image")
    img = img[:, :, :3].astype(np.float64)
    if image.dtype == np.uint8:
        img /= 255.0
    elif image.dtype == np.uint16:
        img /= 65535.0
    return img


@dataclass
class FrameStats:
    """一帧的统计分析（BGR float 0..1 输入，内部转 RGB 语义）。"""
    mean: Tuple[float, float, float]      # R, G, B
    std: Tuple[float, float, float]
    luma_median: float
    luma_p01: float
    luma_p99: float
    chroma_mean: float                    # mean(max(R,G,B) - min(R,G,B))

    @classmethod
    def from_image(cls, image: np.ndarray, *, max_side: int = 512) -> "FrameStats":
        """从图像计算统计（长边超过 ``max_side`` 时 AREA 插值下采样加速）。

        输入为 BGR（任意 ≥3 通道），统计在 RGB 语义上进行。
        """
        import cv2
        img = _to_float01(image)
        h, w = img.shape[:2]
        if max(h, w) > max_side:
            scale = max_side / max(h, w)
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        rgb = np.clip(img[:, :, ::-1], 0.0, None)  # BGR→RGB，暗部不参与统计
        mean = rgb.reshape(-1, 3).mean(axis=0)
        std = rgb.reshape(-1, 3).std(axis=0)
        luma = rgb @ _LUMA_W[::-1]  # RGB 权重
        chroma = np.ptp(rgb, axis=1).mean()
        return cls(
            mean=tuple(float(v) for v in mean),
            std=tuple(float(v) for v in std),
            luma_median=float(np.median(luma)),
            luma_p01=float(np.percentile(luma, 1)),
            luma_p99=float(np.percentile(luma, 99)),
            chroma_mean=float(chroma),
        )


@dataclass
class AutoColorSuggestion:
    """primary 参数建议。

    ``absolute=True``（Auto Balance，分析未调色源）时值为目标绝对值；
    ``absolute=False``（Color Match，分析已调色结果）时值为叠加在
    当前值之上的增量。
    """
    temp: float = 0.0
    tint: float = 0.0
    exposure: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    absolute: bool = True

    def items(self) -> Dict[str, float]:
        """导出为 ``{参数名: 值}`` 字典（合并与 UI 消费）。"""
        return {k: float(getattr(self, k))
                for k in ("temp", "tint", "exposure", "contrast", "saturation")}

    def merged_onto(self, current: Dict[str, float]) -> Dict[str, float]:
        """与当前参数合并为新的绝对值（限幅 ±100）。"""
        out = {}
        for key, value in self.items().items():
            base = float(current.get(key, 0.0) or 0.0)
            out[key] = float(np.clip(
                value if self.absolute else base + value,
                -_PARAM_LIMIT, _PARAM_LIMIT))
        return out


def _clamp_param(value: float) -> float:
    """参数统一限幅到 ±100（与 UI 滑条量级一致）。"""
    return float(np.clip(value, -_PARAM_LIMIT, _PARAM_LIMIT))


def _wb_from_gains(k_r: float, k_g: float, k_b: float) -> Tuple[float, float]:
    """由目标通道增益反解 (temp, tint)。

    shader 模型 wb=(1+t/200, 1-tint/200, 1-t/200)；对 r/b 最小二乘，
    对 g 直接反解。
    """
    temp = _clamp_param((k_r - k_b) * 100.0)
    tint = _clamp_param((1.0 - k_g) * 200.0)
    return temp, tint


def _exposure_for_mult(mult: float) -> float:
    """由目标倍率反解 exposure 参数（正向 stops=sign(p)(p/100)²·6）。"""
    mult = max(float(mult), 1e-6)
    stops = float(np.log2(mult))
    magnitude = np.sqrt(abs(stops) / 6.0) * 100.0
    return _clamp_param(np.sign(stops) * magnitude)


def _contrast_for_gain(gain: float) -> float:
    """由目标对比缩放因子反解 contrast 参数（c=1+p/100）。"""
    return _clamp_param((float(gain) - 1.0) * 100.0)


def auto_balance(stats: FrameStats, *,
                 target_luma: float = TARGET_LUMA,
                 with_saturation: bool = False) -> AutoColorSuggestion:
    """Auto Balance：从未调色源的统计反推参数（绝对值语义）。

    灰世界白平衡 + 中位亮度曝光 + p99 高光对比度。饱和度默认不建议
    （无内容先验，交给 Color Match）。
    """
    mr, mg, mb = stats.mean
    m_avg = (mr + mg + mb) / 3.0
    temp = tint = 0.0
    if m_avg > 1e-6:
        k_r, k_g, k_b = (m_avg / max(c, 1e-6) for c in (mr, mg, mb))
        temp, tint = _wb_from_gains(k_r, k_g, k_b)
        # WB 后中位亮度的近似缩放（通道增益的 luma 加权和）
        wb_luma = (k_r * 0.299 + k_g * 0.587 + k_b * 0.114)
    else:
        wb_luma = 1.0

    exposure = 0.0
    if stats.luma_median > 1e-4:
        exposure = _exposure_for_mult(
            target_luma / max(stats.luma_median * wb_luma, 1e-4))

    contrast = 0.0
    hi = max(stats.luma_p99 * wb_luma, 1e-4)
    # 平图（p99≈p01）没有可拉伸的动态范围，不建议对比度
    if 0.05 < hi < 5.0 and stats.luma_p99 - stats.luma_p01 > 1e-3:
        # 0.18 枢轴幂曲线：x' = (x/0.18)^c · 0.18，把 hi 推到目标高光
        c = float(np.log(TARGET_HIGHLIGHT / 0.18) / np.log(hi / 0.18))
        if 0.0 < c < 4.0:
            contrast = _contrast_for_gain(c)

    saturation = 0.0
    if with_saturation and stats.chroma_mean > 1e-4:
        saturation = _clamp_param((0.30 / stats.chroma_mean - 1.0) * 100.0)

    return AutoColorSuggestion(temp=temp, tint=tint, exposure=exposure,
                               contrast=contrast, saturation=saturation,
                               absolute=True)


def match_to_reference(cur: FrameStats, ref: FrameStats) -> AutoColorSuggestion:
    """Color Match：把当前（已调色）画面统计对齐到参考 still。

    输出增量语义：均值差→WB、中位亮度差→曝光、标准差比→对比度、
    chroma 比→饱和度。增益先做几何归一，避免与曝光重复计数。
    """
    gains = np.array([ref.mean[c] / max(cur.mean[c], 1e-6) for c in range(3)])
    gains /= float(np.exp(np.mean(np.log(np.maximum(gains, 1e-6)))))
    temp, tint = _wb_from_gains(float(gains[0]), float(gains[1]),
                                float(gains[2]))

    exposure = 0.0
    if cur.luma_median > 1e-4 and ref.luma_median > 1e-4:
        exposure = _exposure_for_mult(ref.luma_median / cur.luma_median)

    cur_spread = float(np.mean(cur.std))
    ref_spread = float(np.mean(ref.std))
    contrast = 0.0
    if cur_spread > 1e-4 and ref_spread > 1e-4:
        contrast = _contrast_for_gain(ref_spread / cur_spread)

    saturation = 0.0
    if cur.chroma_mean > 1e-4 and ref.chroma_mean > 1e-4:
        saturation = _clamp_param(
            (ref.chroma_mean / cur.chroma_mean - 1.0) * 100.0)

    return AutoColorSuggestion(temp=temp, tint=tint, exposure=exposure,
                               contrast=contrast, saturation=saturation,
                               absolute=False)


def predict(stats: FrameStats, params: Dict[str, float]) -> FrameStats:
    """按 shader 正向模型近似预测应用参数后的统计（指标复核用）。

    均值/中位走乘性 WB+曝光，标准差按对比度线性近似（枢轴附近
    幂曲线在 log 域近似为缩放），chroma 按饱和度线性混合。
    """
    temp = float(params.get("temp", 0.0) or 0.0)
    tint = float(params.get("tint", 0.0) or 0.0)
    exposure = float(params.get("exposure", 0.0) or 0.0)
    contrast = float(params.get("contrast", 0.0) or 0.0)
    saturation = float(params.get("saturation", 0.0) or 0.0)

    wb = np.array([1.0 + temp / 200.0, 1.0 - tint / 200.0, 1.0 - temp / 200.0])
    stops = np.sign(exposure) * (abs(exposure) / 100.0) ** 2 * 6.0
    exp_mult = float(2.0 ** stops)
    c_factor = 1.0 + contrast / 100.0
    sat_mult = 1.0 + saturation / 100.0

    mean = np.array(stats.mean) * wb * exp_mult
    std = np.array(stats.std) * wb * exp_mult * max(c_factor, 0.0)
    wb_luma = float(wb @ np.array([0.299, 0.587, 0.114]))
    luma_mult = wb_luma * exp_mult
    return FrameStats(
        mean=tuple(float(v) for v in mean),
        std=tuple(float(v) for v in std),
        luma_median=float(stats.luma_median * luma_mult),
        luma_p01=float(max(stats.luma_p01 * luma_mult, 0.0)),
        luma_p99=float(stats.luma_p99 * luma_mult * max(c_factor, 1.0)),
        chroma_mean=float(np.clip(
            stats.chroma_mean * sat_mult * exp_mult, 0.0, None)),
    )


def difference_metrics(a: FrameStats, b: FrameStats) -> Dict[str, float]:
    """两帧统计的差异指标（匹配质量展示用，越小越接近）。"""
    ma, mb_ = np.array(a.mean), np.array(b.mean)
    return {
        # 灰世界残差：各帧通道均值到自身平均的最大偏差
        "gray_error": float(max(
            np.max(np.abs(ma - ma.mean())), np.max(np.abs(mb_ - mb_.mean())))),
        "channel_delta": float(np.max(np.abs(ma - mb_))),
        "luma_delta": float(abs(a.luma_median - b.luma_median)),
        "chroma_delta": float(abs(a.chroma_mean - b.chroma_mean)),
        "spread_ratio": float(np.mean(b.std) / max(np.mean(a.std), 1e-6)),
    }
