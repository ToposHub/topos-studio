"""B7: 占位符片段渲染（彩条 + 斜纹静态帧，三路渲染共用）。

占位素材（``MediaAsset.media_type == "placeholder"``）无磁盘文件，
渲染走本模块的确定性静态帧生成，不触发解码器：
- 底色 = SMPTE 风格彩条（暗示"此处无画面"）；
- 对角斜纹叠加（业界占位约定，防止误当真实素材）；
- 中心标注素材名与备注（由 UI 层绘制，此处只出底图）。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Tuple

import numpy as np

logger = logging.getLogger(__name__)

__all__ = [
    "is_placeholder_asset",
    "placeholder_note",
    "render_placeholder_frame",
]


# SMPTE 风格 7 彩条（RGB 0-1）
_SMPTE_COLORS: tuple[Tuple[float, float, float], ...] = (
    (0.75, 0.75, 0.75),
    (0.75, 0.75, 0.0),
    (0.0, 0.75, 0.75),
    (0.0, 0.75, 0.0),
    (0.75, 0.0, 0.75),
    (0.75, 0.0, 0.0),
    (0.0, 0.0, 0.75),
)

# 斜纹周期（像素）
_STRIPE_PERIOD = 32


def is_placeholder_asset(asset: Any) -> bool:
    """素材是否为占位符（media_type 或元数据标记）。"""
    if asset is None:
        return False
    if getattr(asset, "media_type", "") == "placeholder":
        return True
    metadata = getattr(asset, "metadata", None) or {}
    return bool(metadata.get("placeholder", False))


def placeholder_note(asset: Any) -> str:
    """占位素材的备注文案。"""
    metadata = getattr(asset, "metadata", None) or {}
    return str(metadata.get("note", "") or "")


@lru_cache(maxsize=4)
def _render_placeholder_frame_cached(width: int, height: int) -> np.ndarray:
    """生成并缓存确定性占位帧（按分辨率键控，纯内部使用）。

    实现说明（AUD-17001）：
    - 输出确定性：同 (width, height) 结果恒定，缓存后预览/导出逐帧
      路径只付出一次 uint8 拷贝，不再逐帧重算。
    - 45° 斜纹按行周期 2*period 平铺：行 i 与行 i+2*period 的
      ((x+y)//period)%2 完全一致（商恰好 +2，奇偶不变），因此只需
      一块 (2*period, width) 布尔掩码 + np.tile，替代整帧 int64
      meshgrid（4K 下原先每次调用约百 MB 级中间分配）。
    - 暗化/正常两套调色行分别预转 uint8 后按掩码逐像素选取，
      数值与旧实现 ((c*0.65)*255 vs c*255，float32 左结合) 逐位一致，
      仅一次 (H, W, 3) uint8 分配。
    """
    n = len(_SMPTE_COLORS)
    palette = np.array(_SMPTE_COLORS, dtype=np.float32)[:, ::-1]  # RGB→BGR
    columns = np.arange(width)
    indices = (columns * n) // max(width, 1)
    row = palette[indices]

    period_rows = 2 * _STRIPE_PERIOD
    block = (
        (
            np.arange(width, dtype=np.int64)[None, :]
            + np.arange(period_rows, dtype=np.int64)[:, None]
        ) // _STRIPE_PERIOD
    ) % 2 == 0  # (period_rows, width) bool
    reps = (height + period_rows - 1) // period_rows
    stripe_mask = np.tile(block, (reps, 1))[:height]  # (height, width)

    bright_row = np.clip(row * 255.0, 0, 255).astype(np.uint8)
    dark_row = np.clip(row * 0.65 * 255.0, 0, 255).astype(np.uint8)
    return np.where(
        stripe_mask[..., None],
        dark_row[None, :, :],
        bright_row[None, :, :],
    )


def render_placeholder_frame(width: int, height: int) -> np.ndarray:
    """生成 BGR uint8 占位静态帧（彩条 + 45° 斜纹），确定性输出。

    结果按 (width, height) 缓存；每次调用返回缓存的独立副本，
    调用方可安全持有/修改而不影响后续帧。

    Args:
        width/height: 目标分辨率（像素）

    Returns:
        (height, width, 3) uint8 BGR
    """
    width = max(2, int(width))
    height = max(2, int(height))
    return _render_placeholder_frame_cached(width, height).copy()


def _metadata_dimension(value: Any) -> int:
    """从外部元数据安全提取像素尺寸（AUD-17000）。

    metadata 随工程持久化，JSON round-trip / 手工编辑后类型不保证：
    非数字字符串、dict/list、NaN/inf 等一律回退 0，由调用方改用
    传入尺寸，绝不在渲染路径抛 ValueError/TypeError。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return 0
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def render_placeholder_frame_for_asset(
    asset: Any,
    width: int,
    height: int,
) -> np.ndarray:
    """按占位素材元数据（宽高）生成静态帧；元数据缺失时用传入尺寸。"""
    metadata = getattr(asset, "metadata", None) or {}
    meta_w = _metadata_dimension(metadata.get("width", 0))
    meta_h = _metadata_dimension(metadata.get("height", 0))
    return render_placeholder_frame(meta_w or width, meta_h or height)
