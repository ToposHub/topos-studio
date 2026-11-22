"""B08 · 波形多级金字塔与预算化解码窗口规划（主计划 §14）。

- ``WaveformPyramid``：min/max/RMS 多级金字塔（每级 tile 宽度 × 级倍增），
  按每屏像素取对应 level，**保留极值**（父 tile 的 min/max 由子 tile 聚合，
  不重采样丢失）。内存预算按 ``channels × tiles × itemsize`` 显式核算；
  超预算时降低级数而不是悄悄溢出。
- ``plan_decode_window``：视窗 + 预读的解码窗口规划（纯函数）——完整
  逻辑时长（ClipExtent）与当前缓存窗（DecodedWindow）分离的查询入口；
  字节预算 ``channels × samples × itemsize`` 硬约束，预算内按视窗前后
  小量预读。

坐标：全部为源绝对样本索引（int）；tile 半开区间 ``[start, end)``。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

BASE_TILE_SAMPLES = 512          # 第 0 级 tile 宽度（样本）
LEVEL_GROWTH = 8                 # 每升一级 tile 宽度倍增（8×）
DEFAULT_PYRAMID_LEVELS = 6       # 512→8192…，覆盖概览到放大层级


def _tile_count(total_samples: int, tile: int) -> int:
    return max(1, (int(total_samples) + tile - 1) // tile)


@dataclass(frozen=True)
class PyramidLevel:
    """单级聚合（min/max/RMS 各 (channels, tiles) float32/float64）。"""

    tile_samples: int
    minimum: np.ndarray     # (channels, tiles) float32
    maximum: np.ndarray
    rms: np.ndarray         # float32（sqrt(mean(x²))，无偏窗口）

    @property
    def tiles(self) -> int:
        return int(self.minimum.shape[1])


class WaveformPyramid:
    """确定性多级波形金字塔（构建一次；查询零分配路径供 UI 复用）。"""

    def __init__(self, pcm: np.ndarray, *,
                 levels: int = DEFAULT_PYRAMID_LEVELS,
                 max_bytes: int = 64 * 1024 * 1024) -> None:
        pcm = np.asarray(pcm)
        if pcm.ndim != 2:
            raise ValueError("pcm must be (channels, samples)")
        if levels < 1:
            raise ValueError("levels must be >= 1")
        self._channels = int(pcm.shape[0])
        self._total_samples = int(pcm.shape[1])
        self._levels: List[PyramidLevel] = []
        tile = BASE_TILE_SAMPLES
        # 内存预算：各级 (3 × channels × tiles × 4B)；超预算截断级数
        # （不静默溢出；概览级保留最后一级）。
        for level_index in range(levels):
            tiles = _tile_count(self._total_samples, tile)
            cost = 3 * self._channels * tiles * 4
            if level_index > 0 and cost > max_bytes:
                break
            src = pcm
            minimum = np.empty((self._channels, tiles), dtype=np.float32)
            maximum = np.empty((self._channels, tiles), dtype=np.float32)
            rms = np.empty((self._channels, tiles), dtype=np.float32)
            for ch in range(self._channels):
                row = src[ch]
                for t in range(tiles):
                    seg = row[t * tile:(t + 1) * tile]
                    if seg.size:
                        minimum[ch, t] = float(seg.min())
                        maximum[ch, t] = float(seg.max())
                        rms[ch, t] = float(np.sqrt(np.mean(
                            seg.astype(np.float64) ** 2)))
                    else:
                        minimum[ch, t] = 0.0
                        maximum[ch, t] = 0.0
                        rms[ch, t] = 0.0
            self._levels.append(PyramidLevel(tile, minimum, maximum, rms))
            tile *= LEVEL_GROWTH

    # ── 基本属性 ──────────────────────────────────────────────────────

    @property
    def total_samples(self) -> int:
        return self._total_samples

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def level_count(self) -> int:
        return len(self._levels)

    def memory_bytes(self) -> int:
        return sum(3 * lv.minimum.nbytes // max(1, lv.minimum.shape[0])
                   for lv in self._levels)

    # ── 查询 ──────────────────────────────────────────────────────────

    def level_for_width(self, pixels: int) -> PyramidLevel:
        """按每屏像素数选级（每像素 ≤1 tile；不足回最低级）。"""
        if pixels <= 0:
            pixels = 1
        for lv in self._levels:
            if lv.tile_samples * pixels >= self._total_samples:
                return lv
        return self._levels[-1]

    def range_extremes(self, start: int, end: int, channel: int = 0,
                       *, raw_pcm: Optional[np.ndarray] = None,
                       ) -> Tuple[float, float, float]:
        """任意样本区间 [start, end) 的 (min, max, rms)。

        极值跨 tile 精确聚合，**保留极值**（整 tile 直接取子聚合）。边界
        部分 tile：提供 ``raw_pcm`` 时逐样本精确；否则外扩到 level-0 tile
        边界的保守包含（超集——显示上不丢峰，不会漏极值；文档化语义）。
        """
        if not (0 <= start <= end <= self._total_samples):
            raise ValueError(f"range [{start}, {end}) outside "
                             f"[0, {self._total_samples})")
        if start == end:
            return 0.0, 0.0, 0.0
        level0 = self._levels[0]
        # 选区间内可整除的最细级
        best: Optional[PyramidLevel] = None
        span = end - start
        for lv in self._levels:
            if lv.tile_samples * 2 <= span:
                best = lv
            else:
                break
        best = best or level0

        minimum = math.inf
        maximum = -math.inf
        energy = 0.0
        count = 0

        def _tile_agg(lv: PyramidLevel, idx: int) -> None:
            nonlocal minimum, maximum, energy, count
            minimum = min(minimum, float(lv.minimum[channel, idx]))
            maximum = max(maximum, float(lv.maximum[channel, idx]))
            seg_len = min(lv.tile_samples,
                          self._total_samples - idx * lv.tile_samples)
            energy += float(lv.rms[channel, idx]) ** 2 * seg_len
            count += seg_len

        def _exact_agg(lo: int, hi: int) -> None:
            nonlocal minimum, maximum, energy, count
            seg = np.asarray(raw_pcm[channel, lo:hi], dtype=np.float64)
            minimum = min(minimum, float(seg.min()))
            maximum = max(maximum, float(seg.max()))
            energy += float(np.sum(seg * seg))
            count += int(seg.size)

        # 整 tile（best 级）主体（无整 tile 时 body 为空区间，不重复聚合
        # 同一段边界——min/max 幂等但 RMS/计数语义不得依赖这一点）
        first_full = max(0, (start + best.tile_samples - 1) // best.tile_samples)
        last_full = min(best.tiles, end // best.tile_samples) - 1
        has_body = first_full <= last_full
        body_lo = first_full * best.tile_samples if has_body else start
        body_hi = (last_full + 1) * best.tile_samples if has_body else start
        for idx in range(first_full, last_full + 1):
            _tile_agg(best, idx)

        # 边界：[start, body_lo) 与 [body_hi, end)
        for edge_lo, edge_hi in ((start, body_lo), (body_hi, end)):
            if edge_hi <= edge_lo:
                continue
            if raw_pcm is not None:
                _exact_agg(edge_lo, edge_hi)
                continue
            # 保守包含：边界处涉及的 level-0 整 tile
            l0_first = edge_lo // level0.tile_samples
            l0_last = (edge_hi - 1) // level0.tile_samples
            for idx in range(l0_first, l0_last + 1):
                tile_lo = idx * level0.tile_samples
                tile_hi = min(tile_lo + level0.tile_samples, self._total_samples)
                if tile_hi <= edge_lo or tile_lo >= edge_hi:
                    continue
                # 不足 level-0 tile 覆盖时回退 raw（调用方通常给 raw）；
                # 无 raw 时该 tile 全量计入（超集语义）
                _tile_agg(level0, idx)

        if count:
            return minimum, maximum, math.sqrt(energy / count)
        return 0.0, 0.0, 0.0

    def overview(self, width: int, channel: int = 0) -> Tuple[
            np.ndarray, np.ndarray, np.ndarray]:
        """概览（width 列；每列 min/max/rms），列边界精确对齐样本格。"""
        if width <= 0:
            raise ValueError("width must be positive")
        columns = np.linspace(0, self._total_samples, width + 1).astype(np.int64)
        minimum = np.empty(width, dtype=np.float32)
        maximum = np.empty(width, dtype=np.float32)
        rms = np.empty(width, dtype=np.float32)
        for i in range(width):
            mn, mx, r = self.range_extremes(int(columns[i]), int(columns[i + 1]),
                                            channel)
            minimum[i] = mn
            maximum[i] = mx
            rms[i] = r
        return minimum, maximum, rms


# ─────────────────────────────────────────────────────────────────────────────
# 预算化解码窗口规划（ClipExtent ↔ DecodedWindow 分离；§14 步骤 1/2）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DecodeWindowPlan:
    """一次解码的窗口计划（全部为源绝对样本坐标，半开区间）。"""

    extent_start: int          # 完整逻辑范围（ClipExtent）
    extent_end: int
    window_start: int          # 本次缓存窗（DecodedWindow）
    window_end: int
    prefetch_start: int
    prefetch_end: int
    channels: int
    bytes_window: int
    bytes_prefetch: int
    clamped: bool              # 预算/范围截断发生（调用方应提示）
    reason: str = ""


def plan_decode_window(
    extent_start: int,
    extent_end: int,
    view_start: int,
    view_end: int,
    channels: int,
    *,
    prefetch_samples: int = 48000,
    max_bytes: int = 256 * 1024 * 1024,
    dtype_bytes: int = 4,
) -> DecodeWindowPlan:
    """按字节预算规划解码窗口（视窗必含；预读小量对齐）。

    - 视窗超出完整范围时裁剪（extent 是硬边界）。
    - 预读在视窗前后各取 ``prefetch_samples``，与 extent 相交裁剪。
    - 预算不足时优先保视窗（window 预算减半后仍不足则保窗口最小段），
      预读丢弃；``clamped=True`` 记录原因——不静默截短可听内容。
    """
    if extent_end <= extent_start:
        raise ValueError("empty clip extent")
    if channels < 1 or dtype_bytes < 1:
        raise ValueError("invalid channels/dtype")
    extent_start = int(extent_start)
    extent_end = int(extent_end)
    # 视窗完全越过 extent 末尾时吸附到最后 1 个有效样本（extent_end 为
    # 半开边界，window 不得越界读取 1 样本）。
    view_start = max(extent_start, min(int(view_start), extent_end - 1))
    view_end = max(view_start + 1, min(int(view_end), extent_end))
    per_sample = channels * dtype_bytes

    def _bytes(a: int, b: int) -> int:
        return max(0, (b - a)) * per_sample

    reason = ""
    window_bytes = _bytes(view_start, view_end)
    clamped = False
    if window_bytes > max_bytes:
        # 保视窗头部最多 max_bytes（滚动窗按 budget 重开；不静默截短）
        keep = max(1, max_bytes // per_sample)
        view_end = min(view_end, view_start + keep)
        window_bytes = _bytes(view_start, view_end)
        clamped = True
        reason = "view exceeds decode byte budget; window truncated"
    pf_start = max(extent_start, view_start - prefetch_samples)
    pf_end = min(extent_end, view_end + prefetch_samples)
    prefetch_budget = max(0, max_bytes - window_bytes)
    if _bytes(pf_start, view_start) + _bytes(view_end, pf_end) > prefetch_budget:
        pf_start, pf_end = view_start, view_end
        clamped = True
        reason = (reason or "prefetch dropped") + "; prefetch over budget"
    return DecodeWindowPlan(
        extent_start=extent_start,
        extent_end=extent_end,
        window_start=view_start,
        window_end=view_end,
        prefetch_start=pf_start,
        prefetch_end=pf_end,
        channels=channels,
        bytes_window=window_bytes,
        bytes_prefetch=_bytes(pf_start, pf_end),
        clamped=clamped,
        reason=reason,
    )


__all__ = [
    "BASE_TILE_SAMPLES",
    "DEFAULT_PYRAMID_LEVELS",
    "DecodeWindowPlan",
    "LEVEL_GROWTH",
    "PyramidLevel",
    "WaveformPyramid",
    "plan_decode_window",
]
