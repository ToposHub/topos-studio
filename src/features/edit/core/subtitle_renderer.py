"""
Subtitle Renderer — 共享文字渲染原语 + 字幕专用合成 API.

本模块统一两条字幕渲染路径：
1. 预览（Edit 页面 ProgramViewer）：QPainter 直接绘制 RGBA buffer 到画布
2. 导出烧录（TimelineExporter._render_frame）：把字幕合成到视频帧上

复用策略
--------
SubtitleClip 的字段命名与 TextNode 完全对齐（text / font_family / font_size /
font_weight / italic / halign / valign / letter_spacing / line_spacing /
translate_x/y / scale_x/y / rotation / pivot_* / fill_* / outline_* / shadow_*）。
因此本模块把 SubtitleClip 字段映射到一个临时 TextNode 实例的 parameters 字典，
直接复用 TextNode 已经测试过的 _render_text_pixels / _measure_text_bbox /
_over / _color_layer / _gaussian_blur / _separable_convolve / _rasterize_mask /
_build_font / _draw_text_mask / _build_text_path 实现。

这样：
- composite page 文字渲染保持不变（无回归风险）
- 字幕预览/烧录/手柄 bbox 与 TextNode 像素级一致
- 未来若要把 TextNode 改为引用本模块，只需把这几个方法搬过来即可

字幕变换（translate/scale/rotation/pivot）按 TextNode._do_evaluate 中的同款矩阵
对 bbox 大小的纹理做仿射，并贴回全画布 —— 预览与烧录共用同一段代码。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Tuple

import numpy as np

from src.features.composite.nodes.text_node import TextNode
from src.features.edit.core.captions.model import STYLE_FIELDS
from src.features.edit.core.timeline_v2 import effective_subtitle_font_size

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import SubtitleClip


SubtitleCacheKey = tuple[object, ...]


# ─────────────────────────────────────────────────────────────────────────────
# SubtitleClip → TextNode.parameters 适配层
# ─────────────────────────────────────────────────────────────────────────────

# 这些字段在两个模型里同名同语义，直接透传。
_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "text",
    "font_family",
    "font_size",
    "font_weight",
    "italic",
    "halign",
    "valign",
    "letter_spacing",
    "line_spacing",
    "translate_x",
    "translate_y",
    "scale_x",
    "scale_y",
    "rotation",
    "pivot_mode",
    "pivot_x",
    "pivot_y",
    "fill_r", "fill_g", "fill_b", "fill_a",
    "outline_enable",
    "outline_width",
    "outline_r", "outline_g", "outline_b", "outline_a",
    "shadow_enable",
    "shadow_offset_x", "shadow_offset_y",
    "shadow_blur",
    "shadow_r", "shadow_g", "shadow_b", "shadow_a",
)


# The resolved style field names (from captions.model.STYLE_FIELDS). When a
# resolved style dict is supplied, these keys are taken from it instead of the
# clip's flat fields, so track-default ⊕ cue-override inheritance materializes.
# AUD-18304: 以 captions.model.STYLE_FIELDS 为唯一权威清单，不再在此重复声明。
_STYLE_FIELD_SET: frozenset[str] = frozenset(STYLE_FIELDS)


def _clip_to_text_params(clip: "SubtitleClip", canvas_w: int, canvas_h: int,
                         style: Optional[dict] = None) -> dict:
    """把 SubtitleClip 字段映射成 TextNode.parameters 兼容字典。

    TextNode 需要 width/height（画布尺寸）和 format_name（用于元数据），
    这些字段不在 SubtitleClip 上 —— 由 canvas_w/canvas_h 提供。

    当 ``style`` 非 None 时，分层样式字段（字体/排版/位置/变换/填充/描边/阴影）
    取自该已解析字典（track-default ⊕ cue-override 的合并结果），使分层样式
    真正影响像素。``text`` 仍取自 ``clip``。``style=None`` 时行为与旧路径完全一致（读 clip
    的扁平字段），保证旧项目渲染字节等价。
    """
    params: dict = {
        "width": int(canvas_w),
        "height": int(canvas_h),
        "format_name": "Subtitle",
        "filter": "linear",
        "size_preset": "",
        "fill_hex": "",
        "outline_hex": "",
        "shadow_hex": "",
    }
    for f in _PASSTHROUGH_FIELDS:
        if style is not None and f in _STYLE_FIELD_SET:
            params[f] = style[f]
        else:
            params[f] = getattr(clip, f)

    # 自适应字号：font_size == 0（哨兵值）时按画布高度比例计算。
    # 1080p ≈ 60px，2160p ≈ 119px（0.055 倍画布高度）。
    params["font_size"] = effective_subtitle_font_size(
        params.get("font_size", 0), canvas_h
    )

    return params


class _SubtitleTextNode(TextNode):
    """TextNode 的轻量子类：从外部 parameters 字典初始化，跳过默认值。

    用于在不修改 TextNode 主流程的前提下，复用其渲染方法。
    """

    def __init__(self, params: dict) -> None:
        # 跳过 TextNode.__init__ 的默认值填充；直接设外部字典。
        # Node 基类的 __init__ 会处理 node_id/port/parameters 等基础字段。
        super().__init__()
        self.parameters = dict(params)


# ─────────────────────────────────────────────────────────────────────────────
# 核心 API
# ─────────────────────────────────────────────────────────────────────────────

def measure_subtitle_bbox(clip: "SubtitleClip",
                          canvas_w: int = 1920,
                          canvas_h: int = 1080,
                          style: Optional[dict] = None) -> Tuple[float, float, float, float]:
    """测量字幕在画布上的字形 bbox（top-left 原点像素坐标）。

    返回 (x, y, w, h)。用于控制手柄贴合字形而非整个画布。
    无文本或无 QApplication 时返回整个画布。

    ``style`` 为已解析的样式字典（见 captions.model.resolve_style），用于让
    分层样式影响 bbox 度量；``None`` 时读 clip 的扁平字段（旧行为）。
    """
    node = _SubtitleTextNode(_clip_to_text_params(clip, canvas_w, canvas_h, style))
    return node.text_handle_bbox()


def render_subtitle_clip_to_rgba(clip: "SubtitleClip",
                                 canvas_w: int,
                                 canvas_h: int,
                                 style: Optional[dict] = None) -> np.ndarray:
    """渲染单个 SubtitleClip 到全画布 HDR RGBA float32 buffer.

    输出形状 (canvas_h, canvas_w, 4)，straight alpha，sRGB。
    包含字幕的变换（translate/scale/rotation/pivot），可直接作为
    预览 overlay 或导出烧录的合成层。

    与 TextNode._do_evaluate 的逻辑对齐：先渲染 bbox 大小的纹理，
    再通过仿射变换贴回全画布。

    ``style`` 为已解析的样式字典（见 captions.model.resolve_style），使分层样式
    真正影响像素；``None`` 时读 clip 的扁平字段（旧行为）。
    """
    node = _SubtitleTextNode(_clip_to_text_params(clip, canvas_w, canvas_h, style))

    # ── 1. 测量 bbox 并渲染 bbox 纹理 ──
    bbox_x, bbox_y, bbox_w, bbox_h = node.text_handle_bbox()
    tex_w = max(1, int(round(bbox_w)))
    tex_h = max(1, int(round(bbox_h)))

    bbox_rgba = node._render_text_pixels(tex_w, tex_h)  # (tex_h, tex_w, 4) float32

    # ── 2. 构造全画布透明 buffer，把 bbox 纹理贴到正确位置 ──

    # 变换参数也走 track-default ⊕ cue-override，不能只读 clip 的旧扁平值。
    user_tx = _resolved_value(clip, style, "translate_x")
    user_ty = _resolved_value(clip, style, "translate_y")
    sx = _resolved_value(clip, style, "scale_x")
    sy = _resolved_value(clip, style, "scale_y")
    rot = _resolved_value(clip, style, "rotation")

    # pivot（texture-local 空间）
    if _resolved_value(clip, style, "pivot_mode") == "custom":
        piv_x = _resolved_value(clip, style, "pivot_x") - bbox_x
        piv_y = _resolved_value(clip, style, "pivot_y") - bbox_y
    else:
        piv_x = tex_w / 2.0
        piv_y = tex_h / 2.0

    # 仿射矩阵（与 TextNode._do_evaluate 同款参数，但用 Y-down image-space）
    out = _warp_rgba_to_canvas(
        bbox_rgba,
        tex_w, tex_h,
        canvas_w, canvas_h,
        bbox_x, bbox_y,
        sx, sy, rot,
        piv_x, piv_y,
        user_tx, user_ty,
    )
    return out


def _resolved_value(clip: "SubtitleClip", style: Optional[dict], key: str) -> Any:
    """Read a resolved transform value, falling back to the legacy clip."""
    value = style.get(key) if style is not None else getattr(clip, key)
    if value is None:
        value = getattr(clip, key)
    return float(value) if key != "pivot_mode" else value


def _warp_rgba_to_canvas(bbox_rgba: np.ndarray,
                         tex_w: int, tex_h: int,
                         canvas_w: int, canvas_h: int,
                         bbox_x: float, bbox_y: float,
                         sx: float, sy: float, rot_deg: float,
                         piv_x: float, piv_y: float,
                         user_tx: float, user_ty: float) -> np.ndarray:
    """把 bbox 大小的 RGBA 纹理按变换贴到全画布。

    全部使用 Y-down image-space（与 numpy canvas 一致，行 0 = top）。
    bbox 纹理原点 (0,0) 贴到 canvas 的 (bbox_x, bbox_y)。
    pivot 在 bbox 纹理局部坐标（piv_x, piv_y）。

    采用 **反向映射 + 双线性采样**，保证缩放/旋转后无空洞：
    对 canvas 上每个可能落在变换后 bbox 区域内的像素，反向求出它在
    bbox 纹理空间的位置，再用 bilinear 插值采样 RGBA。

    返回 (canvas_h, canvas_w, 4) float32。
    """
    canvas = np.zeros((canvas_h, canvas_w, 4), dtype=np.float32)

    if tex_w <= 0 or tex_h <= 0:
        return canvas

    rad = math.radians(rot_deg)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)

    # pivot 在 canvas 坐标的位置（bbox 原点 + pivot 局部坐标 + 用户平移）
    piv_cx = bbox_x + piv_x + user_tx
    piv_cy = bbox_y + piv_y + user_ty

    # 计算 canvas 上的扫描包围盒：把 bbox 的 4 个角（含 pivot 偏移）经
    # 正向变换映射到 canvas，取 axis-aligned bbox 作为扫描范围。
    corners_local = [
        (-piv_x, -piv_y),
        (tex_w - 1 - piv_x, -piv_y),
        (-piv_x, tex_h - 1 - piv_y),
        (tex_w - 1 - piv_x, tex_h - 1 - piv_y),
    ]
    cx_canvas = []
    cy_canvas = []
    for lx, ly in corners_local:
        # forward: rx = sx*lx*cos - sy*ly*sin ; ry = sx*lx*sin + sy*ly*cos
        rx = sx * lx * cos_r - sy * ly * sin_r + piv_cx
        ry = sx * lx * sin_r + sy * ly * cos_r + piv_cy
        cx_canvas.append(rx)
        cy_canvas.append(ry)
    x_lo = max(0, int(math.floor(min(cx_canvas))))
    x_hi = min(canvas_w - 1, int(math.ceil(max(cx_canvas))))
    y_lo = max(0, int(math.floor(min(cy_canvas))))
    y_hi = min(canvas_h - 1, int(math.ceil(max(cy_canvas))))
    if x_hi < x_lo or y_hi < y_lo:
        return canvas

    sub_w = x_hi - x_lo + 1
    sub_h = y_hi - y_lo + 1
    if sub_w <= 0 or sub_h <= 0:
        return canvas

    # 反向映射：canvas 像素 → bbox 纹理局部坐标（以 pivot 为原点）
    xs = np.arange(x_lo, x_hi + 1, dtype=np.float32) - piv_cx
    ys = np.arange(y_lo, y_hi + 1, dtype=np.float32) - piv_cy
    gx, gy = np.meshgrid(xs, ys)  # (sub_h, sub_w)

    # 逆变换：先反旋转（cos^2+sin^2=1，旋转矩阵的逆 = 转置），
    # 再除以 scale，得到局部坐标
    # forward: rx = sx*lx*cos - sy*ly*sin ; ry = sx*lx*sin + sy*ly*cos
    # inverse: 我们要解 lx, ly。设 A = sx*cos, B = -sy*sin, C = sx*sin, D = sy*cos
    #   [rx]   [A B] [lx]
    #   [ry] = [C D] [ly]   → det = A*D - B*C = sx*sy
    #   lx = ( D*rx - B*ry) / det = (sy*cos*rx + sy*sin*ry) / (sx*sy)
    #       = (cos*rx + sin*ry) / sx
    #   ly = (-C*rx + A*ry) / det = (-sx*sin*rx + sx*cos*ry) / (sx*sy)
    #       = (-sin*rx + cos*ry) / sy
    safe_sx = sx if abs(sx) > 1e-6 else 1e-6
    safe_sy = sy if abs(sy) > 1e-6 else 1e-6
    lx = (cos_r * gx + sin_r * gy) / safe_sx + piv_x
    ly = (-sin_r * gx + cos_r * gy) / safe_sy + piv_y

    # AUD-18302: 旋转时扫描区是变换后 bbox 的 axis-aligned AABB，其中
    # 变换四边形之外的像素反向映射落在纹理外。旧实现把这些坐标 clamp
    # 到最近边缘 texel——字形 bbox 边缘必含不透明像素，会在 AABB 边界
    # 拖出不透明条纹（拖尾伪影），与预览路径按四边形光栅化的结果不一致。
    # 先计算 inside mask：mask 外像素整体置 0（含 alpha）再采样写回。
    inside = (lx >= 0.0) & (lx <= tex_w - 1) & (ly >= 0.0) & (ly <= tex_h - 1)
    outside_mask = ~inside[..., None]  # (sub_h, sub_w, 1)

    # 双线性采样：把 (lx, ly) clamp 到 [0, tex_w-1]/[0, tex_h-1]，做 bilinear
    lx_c = np.clip(lx, 0.0, tex_w - 1.001)
    ly_c = np.clip(ly, 0.0, tex_h - 1.001)
    x0i = np.floor(lx_c).astype(np.int32)
    y0i = np.floor(ly_c).astype(np.int32)
    x1i = np.minimum(x0i + 1, tex_w - 1)
    y1i = np.minimum(y0i + 1, tex_h - 1)
    fx = lx_c - x0i
    fy = ly_c - y0i
    fx = fx[..., None]  # (sub_h, sub_w, 1) for broadcasting over 4 channels
    fy = fy[..., None]

    # 4 个角的采样
    c00 = bbox_rgba[y0i, x0i]  # (sub_h, sub_w, 4)
    c10 = bbox_rgba[y0i, x1i]
    c01 = bbox_rgba[y1i, x0i]
    c11 = bbox_rgba[y1i, x1i]
    top_b = c00 * (1.0 - fx) + c10 * fx
    bot_b = c01 * (1.0 - fx) + c11 * fx
    sampled = top_b * (1.0 - fy) + bot_b * fy
    # 四边形外像素清零（straight-alpha 全 0 = 全透明）
    sampled = np.where(outside_mask, np.float32(0.0), sampled)

    # 写回 canvas 子区域（直接覆盖，因为源是 straight-alpha，混合由 painter 负责）
    canvas[y_lo:y_lo + sub_h, x_lo:x_lo + sub_w] = sampled
    return canvas


# ─────────────────────────────────────────────────────────────────────────────
# Porter-Duff 合成（导出烧录用）
# ─────────────────────────────────────────────────────────────────────────────

def composite_over(top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    """Porter-Duff 'over' 合成两个 RGBA buffer。

    top/bottom 形状相同 (h, w, 4) float32，straight alpha，RGB 可 HDR（>1.0）。
    返回 straight-alpha HDR RGBA。
    """
    return TextNode._over(top, bottom)


# ─────────────────────────────────────────────────────────────────────────────
# SRT 时间码工具
# ─────────────────────────────────────────────────────────────────────────────

def frames_to_srt_timecode(frame: int, fps: float) -> str:
    """把帧号转成 SRT 时间码字符串：``HH:MM:SS,mmm``（毫秒，逗号分隔）。"""
    if fps <= 0:
        fps = 24.0
    total_ms = int(round(frame * 1000.0 / fps))
    if total_ms < 0:
        total_ms = 0
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ─────────────────────────────────────────────────────────────────────────────
# 渲染缓存（预览性能优化：每帧不重算）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SubtitleTexture:
    """字幕的紧凑字形纹理及其在画布中的基础位置。

    ``rgba`` 仅覆盖字形的紧凑边界框，而不是整个时间线画布。预览端可以
    直接对它做 QPainter 变换，避免在每次重绘中分配和复制一张 4K 透明图。
    """

    rgba: np.ndarray
    x: float
    y: float
    width: int
    height: int
    cache_key: SubtitleCacheKey


class _SubtitleRenderCache:
    """按 (clip_id, 内容签名, 画布尺寸) 缓存紧凑字幕纹理。

    内容签名 = 排除变换的字幕参数（变换在 paint 时重新应用）。
    严格 LRU，默认 8 项。

    AUD-18301：``_GLOBAL_CACHE`` 同时被 UI 绘制线程（ProgramViewer 的
    paint 路径 ``get_subtitle_texture_cached``）与导出生产者线程
    （timeline_export 的烧录循环）并发读写。dict+list 无任何保护时，
    两线程同时 get 同一 key，后到者的 ``self._order.remove(key)`` 抛
    ValueError 击穿 paintEvent/导出循环。get/put/clear 全部持锁。
    """

    def __init__(self, capacity: int = 8) -> None:
        self._capacity = capacity
        self._cache: dict[SubtitleCacheKey, SubtitleTexture] = {}
        self._order: list[SubtitleCacheKey] = []
        self._lock = threading.Lock()

    def get(self, key: SubtitleCacheKey) -> SubtitleTexture | None:
        with self._lock:
            if key in self._cache:
                self._order.remove(key)
                self._order.append(key)
                return self._cache[key]
            return None

    def put(self, key: SubtitleCacheKey, value: SubtitleTexture) -> None:
        with self._lock:
            if key in self._cache:
                self._order.remove(key)
            elif len(self._order) >= self._capacity:
                old = self._order.pop(0)
                self._cache.pop(old, None)
            self._cache[key] = value
            self._order.append(key)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._order.clear()


_GLOBAL_CACHE = _SubtitleRenderCache(capacity=8)


def get_subtitle_texture_cached(clip: "SubtitleClip",
                                canvas_w: int,
                                canvas_h: int,
                                style: Optional[dict] = None) -> SubtitleTexture:
    """返回缓存的紧凑字形纹理，供交互式预览直接绘制。

    字幕的内容变化才会重新栅格化；平移、缩放和旋转由调用方在绘制时应用。
    这比先扩展为全画布 float32 buffer 再转 QImage 少掉了与画布面积成正比的
    内存分配和 CPU 拷贝。

    ``style`` 为已解析的样式字典（见 captions.model.resolve_style）。当提供时，
    缓存键基于该字典的值而非 clip 的扁平字段，从而正确区分共享同一 clip 身份
    但 track 默认样式不同的 cue（否则会串字）。
    """
    key = (_content_signature(clip, style), canvas_w, canvas_h)
    cached = _GLOBAL_CACHE.get(key)
    if cached is not None:
        return cached

    node = _SubtitleTextNode(_clip_to_text_params(clip, canvas_w, canvas_h, style))
    bbox_x, bbox_y, bbox_w, bbox_h = node.text_handle_bbox()
    tex_w = max(1, int(round(bbox_w)))
    tex_h = max(1, int(round(bbox_h)))
    cached = SubtitleTexture(
        rgba=node._render_text_pixels(tex_w, tex_h),
        x=float(bbox_x),
        y=float(bbox_y),
        width=tex_w,
        height=tex_h,
        cache_key=key,
    )
    _GLOBAL_CACHE.put(key, cached)
    return cached


def measure_subtitle_render_bbox(
    clip: "SubtitleClip",
    canvas_w: int,
    canvas_h: int,
    style: Optional[dict] = None,
) -> Tuple[float, float, float, float]:
    """测量最终字幕纹理经变换后的 axis-aligned bbox。

    ``measure_subtitle_bbox`` 只返回未变换的文字 bbox；交付质量门需要知道
    实际烧录到画布上的范围，因此这里复用缓存纹理和统一的 subtitle transform
    contract，计算缩放、旋转、平移、自定义 pivot 后的四角包围盒。只测量四个
    角，不分配整张画布，也不会重复栅格化字幕。

    返回值为画布坐标系中的 ``(x, y, width, height)``，不对画布边界做裁剪；
    这样超出画布或 safe area 的字幕会被质量门真实识别。
    """
    cached = get_subtitle_texture_cached(clip, canvas_w, canvas_h, style)
    from src.features.edit.core.captions.render_plan import (
        apply_affine,
        build_affine_ydown,
    )

    if _resolved_value(clip, style, "pivot_mode") == "custom":
        pivot_x = _resolved_value(clip, style, "pivot_x") - cached.x
        pivot_y = _resolved_value(clip, style, "pivot_y") - cached.y
    else:
        pivot_x, pivot_y = cached.width / 2.0, cached.height / 2.0
    affine = build_affine_ydown(
        translate_x=_resolved_value(clip, style, "translate_x"),
        translate_y=_resolved_value(clip, style, "translate_y"),
        scale_x=_resolved_value(clip, style, "scale_x"),
        scale_y=_resolved_value(clip, style, "scale_y"),
        rotation_deg=_resolved_value(clip, style, "rotation"),
        pivot_x=pivot_x,
        pivot_y=pivot_y,
    )
    corners = (
        (0.0, 0.0),
        (float(max(0, cached.width - 1)), 0.0),
        (0.0, float(max(0, cached.height - 1))),
        (
            float(max(0, cached.width - 1)),
            float(max(0, cached.height - 1)),
        ),
    )
    transformed = tuple(
        apply_affine(affine, local_x, local_y) for local_x, local_y in corners
    )
    x_values = tuple(cached.x + point[0] for point in transformed)
    y_values = tuple(cached.y + point[1] for point in transformed)
    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    return x_min, y_min, max(0.0, x_max - x_min), max(0.0, y_max - y_min)


def render_subtitle_clip_to_rgba_cached(clip: "SubtitleClip",
                                        canvas_w: int,
                                        canvas_h: int,
                                        style: Optional[dict] = None) -> np.ndarray:
    """带全局 LRU 缓存的字幕渲染。

    缓存键基于内容签名（排除变换），变换在 _warp_rgba_to_canvas 时重新应用。
    预览场景下字幕不变时只算一次。

    ``style`` 为已解析的样式字典（见 captions.model.resolve_style）；``None`` 时
    读 clip 的扁平字段（旧行为）。
    """
    cached = get_subtitle_texture_cached(clip, canvas_w, canvas_h, style)
    # SUB-P1-10: use the shared canvas→texture-local pivot conversion so the
    # export warp, preview painter, and on-canvas handles all agree.
    from src.features.edit.core.captions.render_plan import pivot_texture_local
    pivot_x, pivot_y = pivot_texture_local(
        clip, cached.x, cached.y, cached.width, cached.height, style=style
    )
    return _warp_rgba_to_canvas(
        cached.rgba,
        cached.width, cached.height,
        canvas_w, canvas_h,
        cached.x, cached.y,
        _resolved_value(clip, style, "scale_x"),
        _resolved_value(clip, style, "scale_y"),
        _resolved_value(clip, style, "rotation"),
        pivot_x, pivot_y,
        _resolved_value(clip, style, "translate_x"),
        _resolved_value(clip, style, "translate_y"),
    )


def apply_cue_animation(rgba: np.ndarray, clip: "SubtitleClip",
                        frame: int) -> np.ndarray:
    """M13.7 · cue 动画后处理（消费 title_animation 模型层）。

    ``clip.animation`` 声明动画（SubtitleClip 字段，随工程保存往返）：

    - ``{"roll": True}``：滚动片尾——内容（alpha>0 行域）高于画布时
      按 :func:`title_animation.roll_offset` 上滚（整幅垂直位移，
      顶部移入零填充）；
    - ``{"fade_in_frames": n, "fade_out_frames": n}``：cue 淡入/淡出
      ——alpha 乘 :func:`title_animation.cue_fade_alpha`。

    ``frame`` 为时间线绝对帧。无声明 → 原数组原样返回（零开销零变化
    ——未启用动画的既有渲染逐字节不变）。
    """
    anim = getattr(clip, "animation", None)
    if not isinstance(anim, dict) or not anim:
        return rgba
    out = rgba
    if anim.get("roll"):
        from src.features.edit.core.title_animation import roll_offset
        alpha_rows = np.any(out[..., 3] > 0.0, axis=1)
        content_h = float(int(np.max(np.nonzero(alpha_rows)[0])) + 1) \
            if alpha_rows.any() else 0.0
        offset = roll_offset(
            frame, clip_start=int(clip.timeline_start),
            clip_duration=int(clip.timeline_duration),
            content_height=content_h, viewport_height=float(out.shape[0]))
        shift = int(round(offset))
        if shift > 0:
            out = np.zeros_like(out)
            if shift < out.shape[0]:
                out[:out.shape[0] - shift] = rgba[shift:]
    fi = int(anim.get("fade_in_frames", 0) or 0)
    fo = int(anim.get("fade_out_frames", 0) or 0)
    if fi > 0 or fo > 0:
        from src.features.edit.core.title_animation import cue_fade_alpha
        alpha = cue_fade_alpha(
            frame, clip_start=int(clip.timeline_start),
            clip_duration=int(clip.timeline_duration),
            fade_in_frames=fi, fade_out_frames=fo)
        if alpha != 1.0:
            out = out.copy()
            out[..., 3] = out[..., 3] * alpha
    return out


def _content_signature(clip: "SubtitleClip",
                       style: Optional[dict] = None) -> tuple:
    """返回影响字幕纹理内容的字段元组（排除变换）。

    当 ``style`` 非 None 时，样式字段取自该已解析字典而非 clip 的扁平字段——
    这保证共享同一 clip 身份但 track 默认样式不同的 cue 不会撞缓存键（否则会
    串字）。``clip.clip_id`` 与 ``clip.text`` 仍取自 clip（身份与文本不在 style 中）。
    """
    if style is not None:
        # Sorted for a stable, order-independent key; reads the 34 style values
        # from the resolved dict so different track defaults don't collide.
        return (
            clip.clip_id,
            clip.text,
            tuple(style[f] for f in sorted(_STYLE_FIELD_SET)),
        )
    return (
        clip.clip_id,
        clip.text,
        clip.font_family,
        clip.font_size,
        clip.font_weight,
        clip.italic,
        clip.halign,
        clip.valign,
        clip.letter_spacing,
        clip.line_spacing,
        clip.fill_r, clip.fill_g, clip.fill_b, clip.fill_a,
        clip.outline_enable, clip.outline_width,
        clip.outline_r, clip.outline_g, clip.outline_b, clip.outline_a,
        clip.shadow_enable, clip.shadow_offset_x, clip.shadow_offset_y,
        clip.shadow_blur,
        clip.shadow_r, clip.shadow_g, clip.shadow_b, clip.shadow_a,
    )


def invalidate_cache() -> None:
    """清空全局字幕渲染缓存（字幕参数变化时调）。"""
    _GLOBAL_CACHE.clear()
