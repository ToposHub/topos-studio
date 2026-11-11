# -*- coding: utf-8 -*-
"""CW-B07:真实密度云采样、代际守卫、播放节流与 viewer 取色映射(纯函数)。

计划:`docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B07。

契约
----
1. **真实采样**:输入是宿主**既有解码链**提供的帧(engine 当前帧或 GPU
   采样的低分辨率投影),本模块不新增解码/CPU 全帧 readback;确定性
   等步长降采样(stride,无随机);采样量受 ``max_samples`` 上限。
2. **标签完整**:每个结果携带 ``source/frame/revision/colorspace/
   sampled_pixels/bins/generation``;UI 只渲染带标签的真实云——无数据
   诚实为空(reason: no_data/black/uniform),不沿用上一个 clip 的云。
3. **代际守卫**:`DensityResult.generation` 单调;``accept_result`` 拒绝
   比当前已接受代际更晚到达的旧结果(切帧/切节点/undo 之后)。
4. **播放节流**:播放中每 ``interval`` 最多一次采样请求;停止播放立即
   请求一次精确刷新(``pending_stop_refresh``)。
5. **viewer 取色**:屏幕坐标 → 图像坐标,涵盖 zoom/pan/翻转/裁剪偏移/
   DPR;越界返回 None。不在此做颜色管理变换(输入即工作空间帧)。
6. **零色度政策**:灰/黑/白像素投影到色度原点,不产生彩色噪声(B03)。

线程边界:纯函数 + 小型状态类(UI 线程持有);worker 只调用采样函数并
携带 generation 回传。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "DensityResult",
    "sample_density",
    "accept_result",
    "DensityThrottle",
    "ViewState",
    "viewer_to_image",
    "pick_color",
]

#: 采样网格(色度平面 bin 数;96×96 覆盖 UI 云图分辨率足够)。
DENSITY_BINS = 96
DEFAULT_MAX_SAMPLES = 65536
#: 视为"接近零色度"的饱和度上限(与 B03 零色度政策一致)。
_ACHROMATIC_SAT = 0.02


@dataclass(frozen=True)
class DensityResult:
    """一次密度云采样结果(带完整标签;points 为 (N,2) HS 坐标 + 权重)。"""
    generation: int
    points: np.ndarray          # (N, 2) float32: (hue, sat)
    weights: np.ndarray         # (N,) float32: 每 bin 计数
    source: str                 # 如 "node_input:corrector-3" / "clip_frame"
    frame: int | None
    revision: int | None
    colorspace: str
    sampled_pixels: int
    reason: str | None = None   # no_data / black / uniform(此时 points 为空)

    def label(self) -> dict[str, Any]:
        return {"source": self.source, "frame": self.frame,
                "revision": self.revision, "colorspace": self.colorspace,
                "generation": self.generation, "sampled_pixels": self.sampled_pixels,
                "reason": self.reason}


def sample_density(
    image: np.ndarray | None,
    *,
    generation: int,
    source: str,
    frame: int | None = None,
    revision: int | None = None,
    colorspace: str = "working",
    max_samples: int = DEFAULT_MAX_SAMPLES,
    bins: int = DENSITY_BINS,
) -> DensityResult:
    """从真实帧确定性采样 HS 密度云(等步长,无随机;零色度政策)。"""
    meta = dict(generation=generation, source=source, frame=frame,
                revision=revision, colorspace=colorspace)
    if image is None:
        return DensityResult(points=np.zeros((0, 2), np.float32),
                             weights=np.zeros((0,), np.float32),
                             sampled_pixels=0, reason="no_data", **meta)
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] < 3 or array.shape[0] == 0 or array.shape[1] == 0:
        return DensityResult(points=np.zeros((0, 2), np.float32),
                             weights=np.zeros((0,), np.float32),
                             sampled_pixels=0, reason="no_data", **meta)

    pixels = array[:, :, :3].reshape(-1, 3)
    total = pixels.shape[0]
    # 确定性等步长二维降采样:行/列同 stride(无随机),量受上限约束
    stride = max(1, math.ceil(math.sqrt(total / max_samples)))
    sampled = array[::stride, ::stride, :3].reshape(-1, 3).astype(np.float64)

    magnitude = np.abs(sampled)
    flat = magnitude
    sat = _vectorized_saturation(flat)
    hue = _vectorized_hue(flat)
    chroma_mask = sat >= _ACHROMATIC_SAT
    sampled_count = int(flat.shape[0])

    if not chroma_mask.any():
        reason = "black" if magnitude.max() <= 1e-9 else "uniform"
        return DensityResult(points=np.zeros((0, 2), np.float32),
                             weights=np.zeros((0,), np.float32),
                             sampled_pixels=sampled_count, reason=reason, **meta)

    hues = hue[chroma_mask]
    sats = sat[chroma_mask]
    hue_bin = np.clip((hues * bins).astype(np.int64), 0, bins - 1)
    sat_bin = np.clip((sats * bins).astype(np.int64), 0, bins - 1)
    flat_bin = hue_bin * bins + sat_bin
    unique, counts = np.unique(flat_bin, return_counts=True)
    points = np.stack([unique // bins, unique % bins], axis=1).astype(np.float32)
    points = points / float(bins - 1)  # 归一到 [0,1]²
    return DensityResult(points=points, weights=counts.astype(np.float32),
                         sampled_pixels=sampled_count, **meta)


def _vectorized_saturation(pixels: np.ndarray) -> np.ndarray:
    maximum = pixels.max(axis=1)
    minimum = pixels.min(axis=1)
    delta = maximum - minimum
    return np.where(maximum > 1e-12, delta / (maximum + 1e-10), 0.0)


def _vectorized_hue(pixels: np.ndarray) -> np.ndarray:
    """向量化 hue(与 GLSL rgb2hsv 同序:红 0,经黄绿增大;零色度=0)。"""
    maximum = pixels.max(axis=1)
    delta = pixels.max(axis=1) - pixels.min(axis=1)
    r, g, b = pixels[:, 0], pixels[:, 1], pixels[:, 2]
    hue = np.zeros_like(maximum)
    nonzero = delta > 1e-12
    dr = np.where(maximum == r, ((g - b) / np.where(nonzero, delta, 1.0)) % 6.0, 0.0)
    dg = np.where((maximum == g) & (maximum != r),
                  (b - r) / np.where(nonzero, delta, 1.0) + 2.0, 0.0)
    db = np.where((maximum == b) & (maximum != r) & (maximum != g),
                  (r - g) / np.where(nonzero, delta, 1.0) + 4.0, 0.0)
    hue = np.where(maximum == r, dr, np.where(maximum == g, dg, db))
    return np.where(nonzero, hue / 6.0, 0.0)


def accept_result(incoming: DensityResult,
                  current: DensityResult | None) -> DensityResult | None:
    """代际守卫:晚到的旧结果拒绝,不覆盖新目标(返回 None=拒绝)。"""
    if current is not None and incoming.generation < current.generation:
        return None
    return incoming


@dataclass
class DensityThrottle:
    """播放节流:播放中限频,停止后补一次精确刷新(UI 线程持有)。

    ``_last_request=None`` 表示尚未发过请求,首次总是允许(测试可注入
    合成时钟,不受构造时刻影响)。
    """
    interval: float = 0.25
    _last_request: float | None = field(default=None, repr=False)
    _playing: bool = False
    pending_stop_refresh: bool = False

    def set_playing(self, playing: bool) -> None:
        if self._playing and not playing:
            self.pending_stop_refresh = True
        self._playing = playing

    def should_request(self, now: float | None = None) -> bool:
        clock = time.monotonic() if now is None else now
        if not self._playing:
            if self.pending_stop_refresh:
                self.pending_stop_refresh = False
                self._last_request = clock
                return True
            return True  # 非播放:总是允许(由宿主的编辑/切帧事件驱动)
        if self._last_request is None or clock - self._last_request >= self.interval:
            self._last_request = clock
            return True
        return False


@dataclass(frozen=True)
class ViewState:
    """viewer 视图状态(与画布变换约定一致;纯数据,宿主从画布读取)。"""
    viewport_width: int
    viewport_height: int
    image_width: int
    image_height: int
    scale: float = 1.0              # 图像→视口(在 DPR 之前的逻辑倍率)
    offset_x: float = 0.0           # 视口逻辑坐标下图像左上角
    offset_y: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    dpr: float = 1.0
    crop_x: int = 0                 # 显示裁剪窗口在原图中的左上角
    crop_y: int = 0


def viewer_to_image(x: float, y: float, view: ViewState) -> tuple[int, int] | None:
    """屏幕(物理)像素 → 原图像素;越界/无效返回 None。"""
    if view.scale <= 0 or view.dpr <= 0:
        return None
    logical_x = x / view.dpr - view.offset_x
    logical_y = y / view.dpr - view.offset_y
    image_x = logical_x / view.scale + view.crop_x
    image_y = logical_y / view.scale + view.crop_y
    if view.flip_h:
        image_x = view.image_width - 1 - image_x
    if view.flip_v:
        image_y = view.image_height - 1 - image_y
    ix, iy = int(math.floor(image_x)), int(math.floor(image_y))
    if 0 <= ix < view.image_width and 0 <= iy < view.image_height:
        return ix, iy
    return None


def pick_color(image: np.ndarray, position: tuple[int, int] | None) -> np.ndarray | None:
    """取原图像素颜色(最近邻;无插值无色彩变换,输入即工作空间)。"""
    if position is None or image is None:
        return None
    x, y = position
    if not (0 <= y < image.shape[0] and 0 <= x < image.shape[1]):
        return None
    return np.asarray(image[y, x, :3], dtype=np.float32).copy()
