"""
Timeline Occlusion Culling
时间线多轨遮挡裁剪共享纯分析模块。

对应计划：docs/timeline_occlusion_culling_optimization_plan_2026-09-19.md

## 职责

- 对给定帧的可见图层做保守可见性分析，找出被上层完全遮挡的下层。
- 预览（CPU/GPU）、导出（CPU/GL/Metal/批量）共用同一份裁剪语义。
- 输出稳定 ``reason`` 字符串供 telemetry 与诊断使用。

## 设计原则（计划 §4.1）

1. 纯 Python/几何分析：不请求解码帧、不创建 OpenGL/Metal 对象、不读取 GPU 像素。
2. fail-closed：任何不确定（未知 Alpha、非恒定几何、转场、效果、异常模型）
   一律保留全部图层；分析异常时回退为"保留全部层"，绝不回退成黑帧。
3. 第一阶段只接受"恒等几何 + 无 Crop + 完全不透明 + Normal 混合 + 无效果 +
   普通素材"的 full-frame 遮挡；矩形覆盖分析（O5）由独立开关控制。
4. 不修改 TimelineRenderPlan 的不可变模型；输出新的渲染选择结果。

## 环境开关（计划 §8）

- ``TOPOS_TIMELINE_OCCLUSION_CULL=0|1``  总开关，默认 1（开启保守 full-frame 裁剪）
- ``TOPOS_TIMELINE_OCCLUSION_RECT=0|1``  O5 矩形覆盖分析，默认 0（独立验证后开启）
- ``TOPOS_TIMELINE_OCCLUSION_ROI=0|1``   O6 ROI 数据契约，默认 0
- ``TOPOS_TIMELINE_OCCLUSION_DEBUG=0|1`` 每帧输出裁剪决策日志

环境变量在进程内解析一次后缓存；测试可通过 :func:`reset_env_cache` 重置。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

#: 统一的不透明判定容差（计划 §3.1）：有效 opacity >= 1 - epsilon 才算不透明。
OPACITY_EPSILON = 1e-6

#: 恒等几何判定容差（与 adjustment_composite.transform_is_identity 一致）。
GEOMETRY_EPSILON = 1e-4

#: 矩形覆盖分析的安全膨胀像素数（计划 §3.3：1 像素安全膨胀）。
RECT_SAFETY_INFLATE_PX = 1.0

#: 矩形覆盖分析接受的旋转角（度）。任意旋转的外接矩形 ≠ 实际覆盖区域，
#: 只有 90° 倍数旋转的变换结果是轴对齐矩形，才允许几何证明覆盖。
RECT_ALLOWED_ROTATIONS = (0.0, 90.0, 180.0, 270.0, -90.0, -180.0, -270.0)

CoverageKind = Literal["full_frame", "rect", "unknown"]


# ── 环境开关 ──


_ENV_CACHE: Dict[str, bool] = {}


def reset_env_cache() -> None:
    """清空环境变量缓存（测试专用）。"""
    _ENV_CACHE.clear()


def _env_flag(name: str, default: bool) -> bool:
    cached = _ENV_CACHE.get(name)
    if cached is not None:
        return cached
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        value = raw.strip().lower() in ("1", "true", "yes", "on")
    _ENV_CACHE[name] = value
    return value


def culling_enabled() -> bool:
    """总开关：保守 full-frame 遮挡裁剪（默认开启）。"""
    return _env_flag("TOPOS_TIMELINE_OCCLUSION_CULL", True)


def rect_coverage_enabled() -> bool:
    """O5 开关：放大后仍覆盖全画布的矩形覆盖分析（默认关闭）。"""
    return _env_flag("TOPOS_TIMELINE_OCCLUSION_RECT", False)


def roi_enabled() -> bool:
    """O6 开关：矩形可见区域（ROI）数据契约（默认关闭）。"""
    return _env_flag("TOPOS_TIMELINE_OCCLUSION_ROI", False)


def debug_enabled() -> bool:
    """诊断开关：每帧输出裁剪决策日志（默认关闭）。"""
    return _env_flag("TOPOS_TIMELINE_OCCLUSION_DEBUG", False)


# ── 数据类型 ──


@dataclass(frozen=True)
class LayerOcclusionFacts:
    """调用方为单层预计算的静态事实。

    模块本身不做 IO（不读文件、不探测媒体）；素材类型、源像素格式、
    效果链等事实由消费方（预览/导出）通过 ``facts_of`` 回调提供。
    所有"未知"值都导致该层不能作为遮挡层（fail-closed）。

    Attributes:
        asset_kind: 素材类型（"video"/"image"/"adjustment"/...）。
            只有 ``OCCLUDER_ASSET_KINDS`` 中的类型可作遮挡层。
        source_alpha_known_opaque: 源像素格式已知且确定无 Alpha 通道。
        blend_mode: 归一化小写混合模式名（"normal" 之外一律不可遮挡）。
        has_effects: 是否挂载视频滤镜/效果链（blur/glow 等可能改变覆盖范围）。
        background_independent: 效果链是否声明不依赖背景（O7 能力声明；
            仅在未来的高级裁剪中放行，第一阶段效果一律阻止遮挡）。
    """

    asset_kind: str = ""
    source_alpha_known_opaque: bool = False
    blend_mode: str = "normal"
    has_effects: bool = False
    background_independent: bool = False


#: 可作为全画布遮挡层的素材类型（计划 §3.1：普通 video/image；
#: adjustment、nested sequence、composite clip 等一律不可）。
OCCLUDER_ASSET_KINDS = frozenset({"video", "image"})


@dataclass(frozen=True)
class LayerCoverage:
    """单层在当前帧的覆盖能力结论。"""

    kind: CoverageKind = "unknown"
    #: kind == "rect" 时的覆盖矩形 (x, y, w, h)，输出画布像素坐标。
    rect: Optional[Tuple[float, float, float, float]] = None
    #: 是否确定性不透明（opacity 满刻度 + 源无 Alpha + Normal 混合 + 无效果）。
    opaque: bool = False
    #: 稳定原因字符串（不可作遮挡层时说明为什么）。
    reason: str = ""


@dataclass(frozen=True)
class OcclusionDecision:
    """一帧的遮挡裁剪决策。

    Attributes:
        render_layers: 应当参与解码/合成的图层（保持输入顺序）。
        culled_layers: 被裁掉的图层（确定性被遮挡，不解码不合成）。
        occluder_clip_id: 作为遮挡证据的 clip id（无遮挡时为 None）。
        reason: 稳定原因字符串。
        total_layers: 输入图层总数。
        analysis_ms: 分析耗时（毫秒，telemetry 用）。
    """

    render_layers: Tuple[Any, ...]
    culled_layers: Tuple[Any, ...] = ()
    occluder_clip_id: Optional[str] = None
    reason: str = ""
    total_layers: int = 0
    analysis_ms: float = 0.0

    @property
    def culled(self) -> bool:
        """本帧是否发生了裁剪。"""
        return bool(self.culled_layers)


# ── 事实归一化辅助 ──


def normalize_blend_mode(value: Any) -> str:
    """把 ClipBlendMode 枚举/字符串归一化为小写模式名。"""
    if value is None:
        return "normal"
    name = getattr(value, "value", value)
    try:
        return str(name).strip().lower()
    except Exception:
        return "normal"


def source_alpha_known_opaque(pixel_format: Any) -> bool:
    """判定源像素格式是否"已知无 Alpha"。

    判定顺序（fail-closed）：
    1. PixelFormatRegistry 精确查询 —— 已知格式用注册表结论。
    2. 名称启发式 —— 注册表未收录但名称含 Alpha 通道指示
       （yuva/rgba/bgra/argb/abgr/gbrap）的按"有 Alpha"处理。
    3. 其余（空/未知名称）一律视为未知 → 不可作遮挡层。

    覆盖注册表缺口：ProRes 4444 的 ``yuva444p10le``、Topos 4444 的
    ``topos_yuva*p*a*`` 等带 Alpha 格式未注册，但名称可安全识别。
    """
    fmt = str(pixel_format or "").strip().lower()
    if not fmt:
        return False
    from src.shared.media.pixel_format import PixelFormatRegistry

    info = PixelFormatRegistry.get(fmt)
    if info is not None:
        return not info.has_alpha
    alpha_markers = ("yuva", "rgba", "bgra", "argb", "abgr", "gbrap")
    if any(marker in fmt for marker in alpha_markers):
        return False
    # 已注册为无 Alpha 的上面已返回；未注册且无 Alpha 指示的名称
    # 仍按未知处理（保守保留下层）。
    return False


# ── 几何分析 ──


def _parse_transform(transform: Any) -> Optional[Tuple[float, ...]]:
    """解析 8 元组像素空间变换，非法/非有限值返回 None（fail-closed）。"""
    if not transform or len(transform) != 8:
        return None
    try:
        tx, ty, sx, sy, rotation = (float(transform[i]) for i in range(5))
        pivot_mode = str(transform[5])
        pivot_x, pivot_y = float(transform[6]), float(transform[7])
    except (TypeError, ValueError, IndexError):
        return None
    values = (tx, ty, sx, sy, rotation, pivot_x, pivot_y)
    if not all(math.isfinite(v) for v in values):
        return None
    if sx == 0.0 or sy == 0.0:
        # 退化缩放会把层压成线/点：不遮挡（保留下层），本身也不可靠。
        return None
    return (tx, ty, sx, sy, rotation, pivot_mode, pivot_x, pivot_y)


def transform_is_identity(transform: Any) -> bool:
    """恒等变换判定（与 adjustment_composite.transform_is_identity 同语义）。"""
    if not transform or len(transform) != 8:
        return False
    try:
        if str(transform[5]) != "center":
            return False
        values = [float(transform[index]) for index in (0, 1, 2, 3, 4, 6, 7)]
    except (TypeError, ValueError, IndexError):
        return False
    identity = (0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0)
    return all(
        abs(value - expected) <= GEOMETRY_EPSILON
        for value, expected in zip(values, identity)
    )


def crop_blocks_occlusion(crop: Any) -> bool:
    """裁切参数是否阻止该层作为全画布遮挡层。

    任何非零边裁切或羽化都可能露出下层（计划 §3.1 Crop/Mask 行）；
    fail-closed。
    """
    if not crop:
        return False
    try:
        values = [float(v) for v in tuple(crop)[:5]]
    except (TypeError, ValueError):
        return True
    if len(values) < 4:
        return False
    return any(abs(v) > GEOMETRY_EPSILON for v in values)


def transformed_coverage_rect(
    transform: Any,
    canvas_w: int,
    canvas_h: int,
    coordinate_w: Optional[int] = None,
    coordinate_h: Optional[int] = None,
) -> Optional[Tuple[float, float, float, float]]:
    """计算变换后的源画布覆盖矩形（输出画布像素坐标）。

    严格镜像 ``adjustment_composite.apply_transform_cpu`` 的像素空间仿射
    模型：源内容先缩放到输出画布尺寸，再经 M = T·R·S（绕 pivot）映射到
    输出。因此源矩形 = 整个输出画布矩形。

    只有 90° 倍数旋转（含 0）时结果是精确的轴对齐矩形；任意旋转返回
    None（fail-closed——外接矩形会高估覆盖，可能误裁）。

    Returns:
        (x, y, w, h) 覆盖矩形，或 None（无法证明精确覆盖）。
    """
    canvas_w = max(1, int(canvas_w))
    canvas_h = max(1, int(canvas_h))
    parsed = _parse_transform(transform)
    if parsed is None:
        return None
    tx, ty, sx, sy, rotation, pivot_mode, pivot_x, pivot_y = parsed

    coordinate_w = max(1, int(coordinate_w or canvas_w))
    coordinate_h = max(1, int(coordinate_h or canvas_h))
    output_scale_x = canvas_w / float(coordinate_w)
    output_scale_y = canvas_h / float(coordinate_h)

    if pivot_mode == "custom":
        anchor_x, anchor_y = pivot_x * output_scale_x, pivot_y * output_scale_y
    else:
        anchor_x, anchor_y = canvas_w / 2.0, canvas_h / 2.0

    # 任意旋转 fail-closed：外接矩形 ≠ 实际覆盖区域（四角可能露出下层）。
    rotation_norm = rotation % 360.0
    if not any(
        abs(rotation_norm - allowed) <= GEOMETRY_EPSILON
        for allowed in RECT_ALLOWED_ROTATIONS
    ):
        return None

    radians = math.radians(rotation)
    cos_r = math.cos(radians)
    sin_r = math.sin(radians)
    m00 = cos_r * sx
    m01 = -sin_r * sy
    m10 = sin_r * sx
    m11 = cos_r * sy
    m02 = anchor_x - m00 * anchor_x - m01 * anchor_y + tx * output_scale_x
    m12 = anchor_y - m10 * anchor_x - m11 * anchor_y + ty * output_scale_y

    xs = (0.0, float(canvas_w))
    ys = (0.0, float(canvas_h))
    dest_x: List[float] = []
    dest_y: List[float] = []
    for y in ys:
        for x in xs:
            dest_x.append(m00 * x + m01 * y + m02)
            dest_y.append(m10 * x + m11 * y + m12)
    min_x, max_x = min(dest_x), max(dest_x)
    min_y, max_y = min(dest_y), max(dest_y)
    return (min_x, min_y, max_x - min_x, max_y - min_y)


def rect_covers_canvas(
    rect: Tuple[float, float, float, float],
    canvas_w: int,
    canvas_h: int,
    inflate_px: float = RECT_SAFETY_INFLATE_PX,
) -> bool:
    """判断覆盖矩形是否（含 1 像素安全膨胀）完整覆盖输出画布。"""
    if rect is None:
        return False
    x, y, w, h = rect
    canvas_w = max(1, int(canvas_w))
    canvas_h = max(1, int(canvas_h))
    return (
        w > 0.0
        and h > 0.0
        and x <= -inflate_px
        and y <= -inflate_px
        and x + w >= canvas_w + inflate_px
        and y + h >= canvas_h + inflate_px
    )


# ── O7：效果链遮挡安全能力声明 ──


def effects_background_independent(effects: Any) -> bool:
    """O7：效果链是否**全部**声明遮挡安全（background_independent）。

    语义（计划 O7 行）：只有显式声明 ``background_independent`` 且覆盖
    测试通过的效果，才允许参与未来的高级遮挡裁剪。任一效果未声明、
    未知类型或目录查询失败都返回 False（fail-closed）。空链返回 False
    （"无效果"不等于"声明了安全"——调用方应以 ``has_effects`` 判断）。

    第一阶段该结论仅作为 ``LayerOcclusionFacts.background_independent``
    事实记录；当前 :func:`layer_coverage` 对任何挂载效果都阻止其所在
    层成为遮挡层，声明在本阶段不改变裁剪行为。
    """
    effect_tuple = tuple(effects or ())
    if not effect_tuple:
        return False
    try:
        from src.features.shared.effects import get_effect_catalog
        catalog = get_effect_catalog()
        for effect in effect_tuple:
            if not getattr(effect, "is_enabled", True):
                continue
            spec = catalog.get_spec(str(getattr(effect, "effect_type", "")))
            if spec is None or not bool(
                getattr(spec, "background_independent", False)
            ):
                return False
        return True
    except Exception:  # noqa: BLE001 — 目录不可用时按未声明处理
        return False


# ── 单层覆盖分析 ──


def layer_coverage(
    layer: Any,
    facts: Optional[LayerOcclusionFacts],
    output_size: Tuple[int, int],
    *,
    allow_rect: bool = False,
    coordinate_size: Optional[Tuple[int, int]] = None,
) -> LayerCoverage:
    """分析单层在当前帧的覆盖能力（计划 §3.1 条件表，fail-closed）。

    ``layer`` 需要 ``opacity``/``transform``/``crop``/``is_in_transition``/
    ``composite_id``/``clip_id`` 属性（ClipLayerInfo 契约）。
    """
    canvas_w, canvas_h = output_size

    # 图层存在性/时间状态：转场中的层有效 opacity 可能小于 1 或需要下层。
    if bool(getattr(layer, "is_in_transition", False)):
        return LayerCoverage("unknown", None, False, "layer_in_transition")

    # 图层类型：composite 绑定层可能产生额外内容或依赖上下文。
    if getattr(layer, "composite_id", None):
        return LayerCoverage("unknown", None, False, "composite_clip")

    if facts is None:
        return LayerCoverage("unknown", None, False, "facts_unavailable")

    if facts.asset_kind not in OCCLUDER_ASSET_KINDS:
        return LayerCoverage(
            "unknown", None, False, f"asset_kind:{facts.asset_kind or 'unknown'}"
        )

    # 混合模式：非 Normal 需要下层像素。
    if facts.blend_mode != "normal":
        return LayerCoverage(
            "unknown", None, False, f"blend_mode:{facts.blend_mode}"
        )

    # 效果：第一阶段任何视频效果都阻止遮挡（计划 §3.1）。
    if facts.has_effects:
        return LayerCoverage("unknown", None, False, "effects_present")

    # 不透明度：有效 opacity < 1 时下层会透出。
    opacity = getattr(layer, "opacity", 1.0)
    try:
        opacity_f = float(opacity)
    except (TypeError, ValueError):
        return LayerCoverage("unknown", None, False, "opacity_invalid")
    if not math.isfinite(opacity_f) or opacity_f < 1.0 - OPACITY_EPSILON:
        return LayerCoverage("unknown", None, False, "opacity_below_one")

    # 源 Alpha：未知或存在 Alpha 一律保留下层。
    if not facts.source_alpha_known_opaque:
        return LayerCoverage("unknown", None, False, "unknown_alpha")

    crop = getattr(layer, "crop", None)
    if crop_blocks_occlusion(crop):
        return LayerCoverage("unknown", None, False, "crop_present")

    transform = getattr(layer, "transform", None)
    if transform_is_identity(transform):
        return LayerCoverage("full_frame", None, True, "full_frame_opaque")

    if not allow_rect:
        return LayerCoverage("unknown", None, False, "transform_not_identity")

    # O5：放大/平移/90° 倍旋转后仍完整覆盖画布 → 视为全画布遮挡层。
    rect = transformed_coverage_rect(
        transform, canvas_w, canvas_h,
        coordinate_w=(coordinate_size or (canvas_w, canvas_h))[0],
        coordinate_h=(coordinate_size or (canvas_w, canvas_h))[1],
    )
    if rect is not None and rect_covers_canvas(rect, canvas_w, canvas_h):
        return LayerCoverage("rect", rect, True, "rect_full_cover_opaque")
    if rect is None:
        return LayerCoverage("unknown", None, False, "geometry_not_provable")
    return LayerCoverage("rect", rect, False, "rect_not_covering")


# ── 帧级分析入口 ──


FactsProvider = Callable[[Any], Optional[LayerOcclusionFacts]]


def analyze_visible_layers(
    layers: Any,
    transitions: Any,
    frame_idx: int,
    output_size: Tuple[int, int],
    *,
    mode: Literal["preview", "export"] = "preview",
    facts_of: Optional[FactsProvider] = None,
    enabled: Optional[bool] = None,
    allow_rect: Optional[bool] = None,
    coordinate_size: Optional[Tuple[int, int]] = None,
    record: bool = True,
) -> OcclusionDecision:
    """对一帧的可见图层做保守遮挡分析（计划 §4.1 主入口）。

    从上到下扫描（track_index 大的在上），找到第一个可靠的全画布不透明
    遮挡层后，保留该层及其上方所有层，裁掉其下方所有层。任何不确定都
    返回原始全部图层。

    Args:
        layers: 可见图层序列（ClipLayerInfo，按轨道从下到上）。
        transitions: 该帧转场信息序列；非空即整体禁用裁剪（第一阶段）。
        frame_idx: 时间线帧号（telemetry/ROI 用；ClipLayerInfo 已逐帧采样）。
        output_size: 输出画布 (w, h)。
        mode: "preview" 或 "export"（telemetry 维度）。
        facts_of: 单层静态事实回调（素材类型/Alpha/混合/效果）。
        enabled: 显式开关；None 时读 ``TOPOS_TIMELINE_OCCLUSION_CULL``。
        allow_rect: 显式 O5 开关；None 时读 ``TOPOS_TIMELINE_OCCLUSION_RECT``。
        coordinate_size: 原生序列画布（像素空间 Transform 的坐标基准；
            仅 O5 矩形覆盖分析使用，None 时等于 output_size）。
        record: 是否写入 telemetry。

    Returns:
        OcclusionDecision（render_layers/culled_layers/occluder/reason）。
    """
    t0 = time.perf_counter()
    layer_tuple = tuple(layers or ())
    transition_tuple = tuple(transitions or ())

    if enabled is None:
        enabled = culling_enabled()
    if allow_rect is None:
        allow_rect = rect_coverage_enabled()

    decision = _analyze_impl(
        layer_tuple, transition_tuple, output_size,
        enabled=enabled, allow_rect=allow_rect, facts_of=facts_of,
        coordinate_size=coordinate_size,
    )
    analysis_ms = (time.perf_counter() - t0) * 1000.0
    decision = OcclusionDecision(
        render_layers=decision.render_layers,
        culled_layers=decision.culled_layers,
        occluder_clip_id=decision.occluder_clip_id,
        reason=decision.reason,
        total_layers=decision.total_layers,
        analysis_ms=analysis_ms,
    )
    if record:
        get_occlusion_telemetry().record_decision(
            decision, mode=mode, frame=frame_idx,
        )
    if debug_enabled():
        logger.info(
            "occlusion_decision mode=%s frame=%s total=%d render=%d culled=%d "
            "occluder=%s reason=%s analysis_ms=%.3f",
            mode, frame_idx, decision.total_layers,
            len(decision.render_layers), len(decision.culled_layers),
            decision.occluder_clip_id or "-", decision.reason, analysis_ms,
        )
    return decision


def _analyze_impl(
    layers: Tuple[Any, ...],
    transitions: Tuple[Any, ...],
    output_size: Tuple[int, int],
    *,
    enabled: bool,
    allow_rect: bool,
    facts_of: Optional[FactsProvider],
    coordinate_size: Optional[Tuple[int, int]] = None,
) -> OcclusionDecision:
    """主扫描逻辑（不含 telemetry/日志副作用）。"""
    total = len(layers)

    def keep_all(reason: str) -> OcclusionDecision:
        return OcclusionDecision(
            render_layers=layers, culled_layers=(),
            occluder_clip_id=None, reason=reason, total_layers=total,
        )

    if not enabled:
        return keep_all("cull_disabled")
    if transitions:
        # 计划 §3.2：转场涉及全部图层一律保留。
        return keep_all("transition_present")
    if total <= 1:
        return keep_all("no_occluder_candidate")

    try:
        # 直接访问 track_index（无默认值）：缺失/非法的鸭子类型层在排序
        # 即抛异常，落入下方 fail-closed（保留全部层），而不是被默认 0
        # 排到最底后被错误当成"上方层"裁掉。
        sorted_top_down = sorted(
            layers, key=lambda l: int(l.track_index), reverse=True
        )
        for index, layer in enumerate(sorted_top_down):
            facts = facts_of(layer) if facts_of is not None else None
            coverage = layer_coverage(
                layer, facts, output_size, allow_rect=allow_rect,
                coordinate_size=coordinate_size,
            )
            if coverage.kind == "unknown" or not coverage.opaque:
                # 该层不能作遮挡证据（不确定、部分覆盖或几何不可证明）；
                # 但它也不阻止更下层的层成为遮挡层，继续向下扫描。
                continue
            if index + 1 >= len(sorted_top_down):
                # 已是最底层：没有可裁的对象，不构成遮挡结论。
                continue
            # coverage.kind in ("full_frame", "rect") 且 opaque：确定性遮挡。
            visible = tuple(sorted(
                sorted_top_down[: index + 1],
                key=lambda l: int(l.track_index),
            ))
            culled = tuple(sorted(
                sorted_top_down[index + 1 :],
                key=lambda l: int(l.track_index),
            ))
            return OcclusionDecision(
                render_layers=visible,
                culled_layers=culled,
                occluder_clip_id=str(getattr(layer, "clip_id", "") or "") or None,
                reason="full_frame_opaque_occluder"
                if coverage.kind == "full_frame"
                else "rect_full_cover_opaque_occluder",
                total_layers=total,
            )
        return keep_all("no_occluder_found")
    except Exception as exc:  # noqa: BLE001 — 分析异常回退为保留全部层
        logger.warning("timeline occlusion analysis failed, keeping all layers: %s", exc)
        return keep_all("analysis_error")


# ── O6：矩形可见区域数据契约（只输出，不改整帧语义） ──


@dataclass(frozen=True)
class LayerVisibleRegion:
    """单层在输出画布上的可见区域（ROI 数据契约）。

    Attributes:
        clip_id: 所属 clip。
        track_index: 轨道索引。
        region: 可见矩形 (x, y, w, h)；None 表示整画布（未证明可缩小）。
        complete_cover: 该层是否被上层完全覆盖（可见区域为空集）。
    """

    clip_id: str
    track_index: int
    region: Optional[Tuple[float, float, float, float]] = None
    complete_cover: bool = False


@dataclass(frozen=True)
class RegionPlan:
    """一帧的逐层可见区域计划（O6 数据契约，消费方后续阶段接入）。"""

    frame: int
    canvas: Tuple[int, int]
    regions: Tuple[LayerVisibleRegion, ...] = ()

    def region_for(self, clip_id: str) -> Optional[LayerVisibleRegion]:
        for region in self.regions:
            if region.clip_id == clip_id:
                return region
        return None


def compute_visible_region_plan(
    layers: Any,
    transitions: Any,
    frame_idx: int,
    output_size: Tuple[int, int],
    *,
    facts_of: Optional[FactsProvider] = None,
    coordinate_size: Optional[Tuple[int, int]] = None,
) -> RegionPlan:
    """计算逐层可见区域（保守几何，供未来 ROI 解码/局部合成）。

    语义：从上到下累计"确定性不透明覆盖矩形"；某层的可见区域 =
    画布减去其上方全部确定性不透明覆盖。没有任何确定性覆盖证据时，
    该层可见区域 = 整画布（None）。不透明但几何未证明（full_frame/
    rect 之外）的上层按"覆盖整画布"保守处理。单个部分覆盖矩形仅在
    余集为精确单矩形（覆盖横贯全宽/全高且贴边）时输出收缩区域
    （AUD-18700：此前该分支不可达，且遮挡矩形曾被当作可见区域存入——
    语义反转）；多矩形并集不求解，保守整画布。
    """
    canvas_w, canvas_h = output_size
    layer_tuple = tuple(layers or ())
    # 转场帧不做任何几何假设：全部区域保持整画布（计划 §3.2）。
    if tuple(transitions or ()):
        conservative = tuple(
            LayerVisibleRegion(
                str(getattr(layer, "clip_id", "") or ""),
                int(getattr(layer, "track_index", 0)),
                None, False,
            )
            for layer in layer_tuple
        )
        return RegionPlan(frame=frame_idx, canvas=(canvas_w, canvas_h), regions=conservative)
    try:
        return _compute_region_plan_impl(
            layer_tuple, frame_idx, output_size,
            facts_of=facts_of, coordinate_size=coordinate_size,
        )
    except Exception as exc:  # noqa: BLE001 — 异常回退为保守整画布
        logger.warning(
            "visible region plan failed, conservative full canvas: %s", exc)
        conservative = tuple(
            LayerVisibleRegion(
                str(getattr(layer, "clip_id", "") or ""),
                int(getattr(layer, "track_index", 0)),
                None, False,
            )
            for layer in layer_tuple
        )
        return RegionPlan(frame=frame_idx, canvas=(canvas_w, canvas_h), regions=conservative)


def _visible_region_minus_rect(
    canvas_w: int,
    canvas_h: int,
    rect: Optional[Tuple[float, float, float, float]],
) -> Optional[Tuple[float, float, float, float]]:
    """画布减单个确定性覆盖矩形的保守可见外包络（AUD-18700）。

    余集是精确单矩形仅当覆盖矩形横贯全宽且贴上/下边（余集为一条横带），
    或横贯全高且贴左/右边（余集为一条竖带）。其余情形（矩形悬浮在画布
    内部、余集呈 L 形/多块）单一矩形无法精确表达，外包络会退化为整画布，
    返回 ``None``（fail-closed 整画布）。带边界向外取整（floor/ceil），
    保证包络永远**包含**真实可见区域（ROI 解码不得漏像素）。

    Returns:
        可见外包络 (x, y, w, h)；宽度/高度为 0 表示完全被覆盖；
        ``None`` 表示无法收缩（保守整画布）。
    """
    if rect is None:
        return None
    rect_x, rect_y, rect_w, rect_h = (float(v) for v in rect)
    canvas_w_f = float(max(1, int(canvas_w)))
    canvas_h_f = float(max(1, int(canvas_h)))
    if rect_w <= 0.0 or rect_h <= 0.0:
        return None
    spans_width = rect_x <= 0.0 and rect_x + rect_w >= canvas_w_f
    spans_height = rect_y <= 0.0 and rect_y + rect_h >= canvas_h_f
    if spans_width and spans_height:
        # 完全覆盖整画布：可见区域为空集（零高度带）；调用方按
        # complete_cover 语义消费（正常路径该形态由 ("full", None) 承载，
        # 此分支是防御性兜底）。
        return (0.0, canvas_h_f, canvas_w_f, 0.0)
    if spans_width:
        if rect_y <= 0.0:
            band_start = math.floor(min(canvas_h_f, rect_y + rect_h))
            return (0.0, float(band_start), canvas_w_f, canvas_h_f - band_start)
        if rect_y + rect_h >= canvas_h_f:
            band_end = math.ceil(max(0.0, min(canvas_h_f, rect_y)))
            return (0.0, 0.0, canvas_w_f, float(band_end))
        return None
    if spans_height:
        if rect_x <= 0.0:
            band_start = math.floor(min(canvas_w_f, rect_x + rect_w))
            return (float(band_start), 0.0, canvas_w_f - band_start, canvas_h_f)
        if rect_x + rect_w >= canvas_w_f:
            band_end = math.ceil(max(0.0, min(canvas_w_f, rect_x)))
            return (0.0, 0.0, float(band_end), canvas_h_f)
        return None
    return None


def _compute_region_plan_impl(
    layer_tuple: Tuple[Any, ...],
    frame_idx: int,
    output_size: Tuple[int, int],
    *,
    facts_of: Optional[FactsProvider],
    coordinate_size: Optional[Tuple[int, int]],
) -> RegionPlan:
    """ROI 计算主体（直接访问 track_index：缺失即抛给上层保守回退）。"""
    canvas_w, canvas_h = output_size
    # 输入序号 -> 可见区域；扫描自上而下，输出按输入顺序（从下到上），
    # 便于消费方对齐 layer list。
    region_by_index: Dict[int, LayerVisibleRegion] = {}
    sorted_top_down = sorted(
        enumerate(layer_tuple),
        key=lambda pair: int(pair[1].track_index),
        reverse=True,
    )
    # 已确认的不透明覆盖：("full", None) 或 ("rect", (x, y, w, h))。
    # AUD-18700：kind=="rect" 的结论在 layer_coverage 已通过全部不透明
    # 性门（opacity 满刻度/源无 Alpha/Normal 混合/无效果/无 crop），仅
    # 几何上未覆盖整画布——此前该结论被同参数重复调用 rect_covers_canvas
    # 的分支丢弃（"rect" 覆盖不可达，单矩形 ROI 收缩永不产出），且若可
    # 达还会把遮挡矩形当作可见区域存入（语义反转）。现在部分覆盖矩形
    # 进入累计，下方层的可见区域按"画布减覆盖"的保守外包络收缩。
    opaque_covers: List[Tuple[str, Optional[Tuple[float, float, float, float]]]] = []

    for input_index, layer in sorted_top_down:
        clip_id = str(getattr(layer, "clip_id", "") or "")
        track_index = int(layer.track_index)
        facts = facts_of(layer) if facts_of is not None else None
        coverage = layer_coverage(
            layer, facts, output_size,
            allow_rect=True, coordinate_size=coordinate_size,
        )
        if not opaque_covers:
            region_by_index[input_index] = LayerVisibleRegion(
                clip_id, track_index, None, False,
            )
        elif any(kind == "full" for kind, _ in opaque_covers):
            region_by_index[input_index] = LayerVisibleRegion(
                clip_id, track_index, None, True,
            )
        else:
            # 多个矩形覆盖的并集不做复杂求交：单矩形覆盖时给出缩小
            # 区域（画布减覆盖的保守外包络），多矩形时保守返回整画布。
            if len(opaque_covers) == 1:
                visible = _visible_region_minus_rect(
                    canvas_w, canvas_h, opaque_covers[0][1])
                if visible is None:
                    region_by_index[input_index] = LayerVisibleRegion(
                        clip_id, track_index, None, False,
                    )
                elif visible[2] <= 0.0 or visible[3] <= 0.0:
                    region_by_index[input_index] = LayerVisibleRegion(
                        clip_id, track_index, None, True,
                    )
                else:
                    region_by_index[input_index] = LayerVisibleRegion(
                        clip_id, track_index, visible, False,
                    )
            else:
                region_by_index[input_index] = LayerVisibleRegion(
                    clip_id, track_index, None, False,
                )
        # 覆盖累计（AUD-18700）：full_frame / 覆盖整画布的 rect →
        # ("full", None)；部分 cover 的 rect（reason=="rect_not_covering"，
        # 不透明性门已过、几何可证明）→ ("rect", rect)，供下方层收缩
        # 可见区域。kind=="rect" 只由 layer_coverage 的
        # "rect_full_cover_opaque"/"rect_not_covering" 两个出口产生。
        if coverage.opaque and coverage.kind == "full_frame":
            opaque_covers.append(("full", None))
        elif coverage.kind == "rect" and coverage.rect is not None:
            if rect_covers_canvas(coverage.rect, canvas_w, canvas_h):
                opaque_covers.append(("full", None))
            else:
                opaque_covers.append(("rect", coverage.rect))

    regions: List[LayerVisibleRegion] = [
        region_by_index[index] for index in sorted(region_by_index)
    ]
    return RegionPlan(frame=frame_idx, canvas=(canvas_w, canvas_h), regions=tuple(regions))


# ── O0：telemetry ──


@dataclass
class OcclusionFrameStats:
    """单帧裁剪决策统计（计划 O0 验收字段）。"""

    mode: str
    frame: int
    total_layers: int
    render_layers: int
    culled_layers: int
    occluder_clip_id: Optional[str]
    reason: str
    analysis_ms: float


class OcclusionTelemetry:
    """线程安全的遮挡裁剪遥测聚合器。

    滑动窗口容量有限（默认 8192 帧）；``snapshot`` 输出聚合指标，
    供基准脚本与回归测试对比"优化前后实际减少的解码/渲染工作"。
    """

    def __init__(self, max_frames: int = 8192) -> None:
        self._lock = threading.Lock()
        self._frames: Deque[OcclusionFrameStats] = deque(maxlen=max_frames)

    def record_decision(
        self,
        decision: OcclusionDecision,
        *,
        mode: str,
        frame: int,
    ) -> None:
        stats = OcclusionFrameStats(
            mode=mode,
            frame=int(frame),
            total_layers=decision.total_layers,
            render_layers=len(decision.render_layers),
            culled_layers=len(decision.culled_layers),
            occluder_clip_id=decision.occluder_clip_id,
            reason=decision.reason,
            analysis_ms=decision.analysis_ms,
        )
        with self._lock:
            self._frames.append(stats)

    def reset(self) -> None:
        with self._lock:
            self._frames.clear()

    def snapshot(self) -> Dict[str, Any]:
        """输出聚合指标（帧数/总层数/渲染层数/被裁层数/原因分布/耗时）。"""
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return {
                "frames": 0,
                "total_layers": 0,
                "render_layers": 0,
                "culled_layers": 0,
                "culled_ratio": 0.0,
                "reasons": {},
                "modes": {},
                "analysis_ms_avg": 0.0,
                "analysis_ms_max": 0.0,
            }
        total_layers = sum(f.total_layers for f in frames)
        render_layers = sum(f.render_layers for f in frames)
        culled_layers = sum(f.culled_layers for f in frames)
        reasons = Counter(f.reason for f in frames)
        modes = Counter(f.mode for f in frames)
        analysis = [f.analysis_ms for f in frames]
        return {
            "frames": len(frames),
            "total_layers": total_layers,
            "render_layers": render_layers,
            "culled_layers": culled_layers,
            "culled_ratio": (culled_layers / total_layers) if total_layers else 0.0,
            "reasons": dict(reasons),
            "modes": dict(modes),
            "analysis_ms_avg": sum(analysis) / len(analysis),
            "analysis_ms_max": max(analysis),
        }


_GLOBAL_TELEMETRY = OcclusionTelemetry()


def get_occlusion_telemetry() -> OcclusionTelemetry:
    """进程级共享遥测实例（预览/导出统一写入）。"""
    return _GLOBAL_TELEMETRY
