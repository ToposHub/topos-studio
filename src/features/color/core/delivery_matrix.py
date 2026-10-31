"""Delivery Matrix（P1-COLOR-25）—— 导出前调色能力报告。

回答一个问题：**当前调色里的每个操作，在选定的交付目标下会发生什么？**

- ``full``    完整保留（导出路径有逐像素等价实现）
- ``baked``   烘焙（量化进 3D LUT 网格，存在插值/精度损失）
- ``dropped`` 丢失（导出管线完全不应用，静默丢失）

三条交付路径的能力现状（与实现一一对应，勿凭感觉改）：
- still（Color 页静态图导出）：GPU 全分辨率管线，全部 ``full``；
- video（时间线视频导出）：CPU numpy 管线只应用平铺 ``grading_params``
  中的逐像素项（``timeline_export._apply_color_grade``），其余全部
  ``dropped``；纹理类（mid_detail/sharpness）虽在参数里但 CPU 实现不消费；
- lut（.cube/.3dl 导出）：``LutExporter.EXPORTABLE_OPERATIONS`` 烘焙，
  空间/纹理类 ``dropped``（见 ``NON_EXPORTABLE_OPERATIONS``）。

报告只列出**实际启用**的操作；验收要求"导出前清楚列出会保留、烘焙或
丢失的效果；用户确认后才允许有损转换"——UI 侧用
``DeliveryMatrixDialog`` 消费本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DELIVERY_FULL = "full"
DELIVERY_BAKED = "baked"
DELIVERY_DROPPED = "dropped"

TARGET_STILL = "still"
TARGET_VIDEO = "video"
TARGET_LUT = "lut"
VALID_TARGETS = (TARGET_STILL, TARGET_VIDEO, TARGET_LUT)

# 与 ColorStackData / Layer 默认值对齐的 primary 判定基准
_WHEEL_DEFAULTS = {
    "lift": (0.0, 0.0, 0.0),
    "gamma": (0.0, 0.0, 0.0),
    "gain": (0.0, 0.0, 0.0),
}
_SCALAR_DEFAULTS = {
    "exposure": 0.0, "temp": 0.0, "tint": 0.0, "contrast": 0.0,
    "shadow": 0.0, "highlights": 0.0, "saturation": 0.0, "vibrance": 0.0,
    "mid_detail": 0.0, "sharpness": 0.0, "hue_shift": 0.0,
}
_TEXTURE_KEYS = ("mid_detail", "sharpness")

# 每个操作的元数据：标签 i18n key / 兜底名 / 三种交付目标下的结果
_OPERATIONS: Dict[str, Dict[str, str]] = {
    "primary": {
        "label_key": "delivery.op_primary", "label": "Primary (色轮/曝光/白平衡)",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_FULL, TARGET_LUT: DELIVERY_BAKED,
    },
    "texture": {
        "label_key": "delivery.op_texture", "label": "中间细节 / 锐化",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    "curves": {
        "label_key": "delivery.op_curves", "label": "RGB 曲线",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_BAKED,
    },
    "hsl_curves": {
        # M05.2c：CPU 导出已逐像素应用（advanced_grade_cpu 同源数学）
        "label_key": "delivery.op_hsl_curves", "label": "HSL 曲线",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_FULL, TARGET_LUT: DELIVERY_BAKED,
    },
    "mixer": {
        "label_key": "delivery.op_mixer", "label": "RGB 混色器",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_BAKED,
    },
    "soft_clip": {
        # M05.2c：同上
        "label_key": "delivery.op_soft_clip", "label": "柔化裁切 (Soft Clip)",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_FULL, TARGET_LUT: DELIVERY_BAKED,
    },
    "lut": {
        "label_key": "delivery.op_lut", "label": "3D LUT",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_BAKED,
    },
    "warper": {
        "label_key": "delivery.op_warper", "label": "色彩扭曲器 (Warper)",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    "effects": {
        "label_key": "delivery.op_effects", "label": "锐化/纹理/光晕效果",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    "qualifier": {
        # M05.2/M06.2：基础三带（hue/sat/lum）+invert 已由 CPU 导出的
        # numpy keyer 像素级应用（qualifier_cpu.py）——video 不再是丢
        "label_key": "delivery.op_qualifier", "label": "限定器基础带 (Qualifier)",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_FULL, TARGET_LUT: DELIVERY_DROPPED,
    },
    "qualifier_postprocess": {
        # matte 后处理（clean/blur/denoise）仍为 GPU 专属——CPU 导出按
        # 基础 keyer 输出并显式告警
        "label_key": "delivery.op_qualifier_postprocess",
        "label": "限定器后处理 (Qual. Clean/Blur/Denoise)",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    "windows": {
        "label_key": "delivery.op_windows", "label": "Power Window / 蒙版",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    "tracking": {
        "label_key": "delivery.op_tracking", "label": "窗口跟踪",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    "ai_matte": {
        "label_key": "delivery.op_ai_matte", "label": "AI 蒙版",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    "plugins": {
        "label_key": "delivery.op_plugins", "label": "插件效果 (.tglsl)",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    "baked": {
        "label_key": "delivery.op_baked", "label": "AI 烘焙图层",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_DROPPED, TARGET_LUT: DELIVERY_DROPPED,
    },
    # P1-COLOR-13 收口：视频降噪经 headless GL 桥接入 TimelineExporter
    # 视频导出（与预览共用 VideoDenoisePipeline，export 档参数），时间线
    # 导出完整保留；仅 LUT 交付（无法表达时域）丢弃。
    "video_denoise": {
        "label_key": "delivery.op_video_denoise", "label": "视频降噪 (Video Denoise)",
        TARGET_STILL: DELIVERY_FULL, TARGET_VIDEO: DELIVERY_FULL, TARGET_LUT: DELIVERY_DROPPED,
    },
}


@dataclass(frozen=True)
class DeliveryRow:
    """报告中一行：一个实际启用的操作在目标交付下的命运。"""

    operation: str
    delivery: str
    label_key: str = ""
    label: str = ""
    #: 行级补充说明（如插件 export_support 声明与管线现实的差异）
    detail: str = ""
    detail_key: str = ""

    @property
    def is_lossy(self) -> bool:
        """非完整保留（烘焙或丢失）即为有损。"""
        return self.delivery != DELIVERY_FULL


@dataclass(frozen=True)
class DeliveryMatrixReport:
    """一次交付的能力矩阵（只含实际启用的操作）。"""

    target: str
    rows: Tuple[DeliveryRow, ...]

    @property
    def dropped_rows(self) -> Tuple[DeliveryRow, ...]:
        """交付结果为丢失（dropped）的行。"""
        return tuple(r for r in self.rows if r.delivery == DELIVERY_DROPPED)

    @property
    def baked_rows(self) -> Tuple[DeliveryRow, ...]:
        """交付结果为烘焙（baked）的行。"""
        return tuple(r for r in self.rows if r.delivery == DELIVERY_BAKED)

    @property
    def kept_rows(self) -> Tuple[DeliveryRow, ...]:
        """交付结果为完整保留（full）的行。"""
        return tuple(r for r in self.rows if r.delivery == DELIVERY_FULL)

    @property
    def has_loss(self) -> bool:
        """存在烘焙或丢失操作（需要用户确认）。"""
        return bool(self.dropped_rows) or bool(self.baked_rows)

    @property
    def has_dropped(self) -> bool:
        """存在丢失操作。"""
        return bool(self.dropped_rows)


# ---------------------------------------------------------------------------
# 操作检测
# ---------------------------------------------------------------------------

def _changed(value: Any, default: Any) -> bool:
    """值是否偏离默认（tuple 逐分量比较，标量按 1e-6 容差）。"""
    try:
        if isinstance(default, tuple):
            values = tuple(float(item) for item in value)
            return len(values) != len(default) or any(
                abs(item - expected) > 1e-6
                for item, expected in zip(values, default))
        return abs(float(value) - float(default)) > 1e-6
    except (TypeError, ValueError):
        return value != default


def _mixer_active(params: Any) -> bool:
    """与 LutExportWorker._has_active_mixer_params 同语义。"""
    if not isinstance(params, dict):
        return False
    for values in params.values():
        if not isinstance(values, (list, tuple)) or len(values) < 3:
            continue
        try:
            if any(abs(float(v)) > 1.0e-6 for v in values[:3]):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _layer_operations(data: Dict[str, Any]) -> set:
    """检测单个 layer dict（Layer.to_dict 或 ColorStackData.layers 元素）启用的操作。"""
    ops: set = set()
    if not data.get("visible", True):
        return ops

    gp = data.get("grading_params") or {}
    if any(_changed(gp.get(name, default), default)
           for name, default in _SCALAR_DEFAULTS.items()
           if name not in _TEXTURE_KEYS):
        ops.add("primary")
    if any(_changed(gp.get(name, default), default)
           for name, default in _SCALAR_DEFAULTS.items()
           if name in _TEXTURE_KEYS):
        ops.add("texture")
    for key, op in (("curves_data", "curves"), ("curves_lut", "curves"),
                    ("hsl_curve_data", "hsl_curves"), ("hsl_curve_lut", "hsl_curves")):
        if data.get(key):
            ops.add(op)
    if _mixer_active(data.get("mixer_params")):
        ops.add("mixer")
    if (data.get("soft_clip_params") or {}).get("enabled"):
        ops.add("soft_clip")
    if data.get("active_lut_path") or data.get("lut_path"):
        ops.add("lut")
    if data.get("warper_params") is not None:
        ops.add("warper")
    if (data.get("effects_params") or {}).get("enabled"):
        ops.add("effects")
    qp = data.get("qualifier_params") or {}
    if qp.get("enabled"):
        ops.add("qualifier")
        if any(float(qp.get(k, 0.0) or 0.0) > 0.0
               for k in ("clean_black", "clean_white", "blur", "denoise")):
            ops.add("qualifier_postprocess")
    if data.get("mask_shapes"):
        ops.add("windows")
    # Layer.ai_matte_enabled defaults to True even before a matte exists.
    # The operation is active only when matte data (or a saved matte file) is
    # present and the user has not disabled it. Avoid truth-testing ndarrays.
    has_ai_matte = (
        data.get("ai_matte") is not None
        or bool(data.get("ai_matte_file"))
    )
    if has_ai_matte and data.get("ai_matte_enabled", True):
        ops.add("ai_matte")
    if any(p.get("enabled", True) for p in (data.get("plugins") or [])
           if isinstance(p, dict)):
        ops.add("plugins")
    if data.get("is_baked"):
        ops.add("baked")
    dn = data.get("video_denoise")
    if isinstance(dn, dict):
        if (dn.get("spatial") or {}).get("enabled") or \
                (dn.get("temporal") or {}).get("enabled"):
            ops.add("video_denoise")
    elif dn:
        ops.add("video_denoise")
    return ops


def _normalize_layer(data: Any) -> Dict[str, Any]:
    """Layer 对象经 ``to_dict()`` 归一为 dict；dict 原样返回，其余给空 dict。"""
    if isinstance(data, dict):
        return data
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except TypeError:
            return to_dict(None)
    return {}


def detect_operations(layers: Iterable[Any]) -> set:
    """跨层汇总实际启用的操作集合。接受 Layer 对象或 layer dict。"""
    ops: set = set()
    for layer in layers or []:
        ops |= _layer_operations(_normalize_layer(layer))
    return ops


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------

_OP_ORDER = {op: i for i, op in enumerate(_OPERATIONS)}


def build_delivery_matrix_report(
    layers: Iterable[Any], target: str
) -> DeliveryMatrixReport:
    """为给定图层集合与交付目标生成能力矩阵。"""
    if target not in VALID_TARGETS:
        raise ValueError(f"Unknown delivery target: {target}")
    used = detect_operations(layers)
    detail_key = detail_text = ""
    if "plugins" in used:
        detail_key, detail_text = _plugins_export_support_detail(layers)
    rows = []
    for op in sorted(used, key=lambda o: _OP_ORDER.get(o, len(_OPERATIONS))):
        meta = _OPERATIONS[op]
        rows.append(DeliveryRow(
            operation=op,
            delivery=meta[target],
            label_key=meta["label_key"],
            label=meta["label"],
            detail=detail_text if op == "plugins" else "",
            detail_key=detail_key if op == "plugins" else "",
        ))
    return DeliveryMatrixReport(target=target, rows=tuple(rows))


def _plugins_export_support_detail(
    layers: Iterable[Any],
) -> Tuple[str, str]:
    """P1-COLOR-15 复查：按插件 manifest 的 export_support 声明补充说明。

    有插件声明支持导出（export_support 非 none）时，说明"插件作者声明
    可导出，但当前视频导出管线尚未实现 DCTL 渲染（仍会丢失）"——
    既消费声明，也不对管线能力撒谎。全部声明 none 时不加说明。
    """
    plugin_ids: List[str] = []
    for layer in layers or []:
        data = _normalize_layer(layer)
        for plugin in data.get("plugins") or []:
            if isinstance(plugin, dict) and plugin.get("enabled", True):
                pid = plugin.get("plugin_id")
                if pid and pid not in plugin_ids:
                    plugin_ids.append(pid)
    if not plugin_ids:
        return "", ""
    declared_exportable = False
    try:
        from src.features.color.core.plugin_manager import get_plugin_manager
        manager = get_plugin_manager()
    except Exception:
        return "", ""
    for pid in plugin_ids:
        try:
            meta = manager.get_plugin(pid) or {}
        except Exception:
            continue
        support = (meta.get("capabilities") or {}).get("export_support", "none")
        if support and str(support).lower() not in ("none", ""):
            declared_exportable = True
            break
    if not declared_exportable:
        return "", ""
    return ("delivery.detail_plugins_export_support",
            "插件声明支持导出，但视频导出管线尚未实现 DCTL 渲染（仍会丢失）")


def engine_video_denoise_enabled(engine: Any) -> bool:
    """ImageEngine 的视频降噪是否启用（spatial 或 temporal 任一）。"""
    params = getattr(engine, "video_denoise_params", None)
    if params is None:
        return False
    for section in ("spatial", "temporal"):
        part = getattr(params, section, None)
        if getattr(part, "enabled", False):
            return True
    return False


def _stack_layer_dicts(stack: Any) -> List[Dict[str, Any]]:
    """ColorStackData → layer dict 列表（v2 快照优先，旧工程回退平铺键）。"""
    layers = list(getattr(stack, "layers", ()) or ())
    if layers:
        return [_normalize_layer(layer) for layer in layers]
    # 旧格式：平铺精简键拼一个伪 layer dict
    pseudo: Dict[str, Any] = {
        "grading_params": dict(getattr(stack, "grading_params", {}) or {}),
        "curves_data": getattr(stack, "curves_data", None),
        "hsl_curve_data": getattr(stack, "hsl_curve_data", None),
        "mixer_params": getattr(stack, "mixer_params", None),
        "soft_clip_params": getattr(stack, "soft_clip_params", None),
        "active_lut_path": getattr(stack, "lut_path", None),
        "lut_intensity": getattr(stack, "lut_intensity", 1.0),
    }
    return [pseudo]


def report_from_color_stacks(
    stacks: Iterable[Any], target: str,
    video_denoise_enabled: bool = False,
    tracking_clip_ids: Optional[Iterable[str]] = None,
) -> DeliveryMatrixReport:
    """从 ColorStackData 集合构建报告（时间线视频导出路径用）。

    跟踪数据在栈上而非 layer 上：仅当前时间线中的剪辑仍引用的窗口，
    且存在可用跟踪结果时，才补充 ``tracking`` 操作。
    ``video_denoise_enabled``：引擎当前开着视频降噪时补 ``video_denoise``
    行（降噪参数是引擎会话级、不在调色栈上，导出方显式告知）。
    """
    all_layers: List[Dict[str, Any]] = []
    has_tracking = False
    has_stack_denoise = False
    allowed_clip_ids = (
        {str(clip_id) for clip_id in tracking_clip_ids if clip_id}
        if tracking_clip_ids is not None else None
    )

    def collection_has_usable_results(results: Any) -> bool:
        """Return whether a serialized result collection can change geometry."""
        if isinstance(results, dict):
            values = results.values()
        elif isinstance(results, (list, tuple)):
            values = results
        else:
            return bool(results)
        return any(
            not isinstance(result, dict)
            or str(getattr(result.get("status", "valid"), "value",
                            result.get("status", "valid"))).lower() != "failed"
            for result in values
        )

    def binding_has_results(
        binding: Any, *, include_legacy_frames: bool = False,
    ) -> bool:
        """A placeholder binding has no effect until it contains a result."""
        if not isinstance(binding, dict):
            return False
        return (
            collection_has_usable_results(binding.get("transforms"))
            or collection_has_usable_results(binding.get("manual_corrections"))
            or (include_legacy_frames
                and collection_has_usable_results(binding.get("frames")))
        )

    def collect_visible_shape_ids(layers: Iterable[Dict[str, Any]]) -> set[str]:
        shape_ids: set[str] = set()

        def visit(items: Any) -> None:
            if not isinstance(items, (list, tuple)):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                shape_id = item.get("id")
                if shape_id:
                    shape_ids.add(str(shape_id))
                visit(item.get("children", ()))

        for layer in layers:
            if layer.get("visible", True):
                visit(layer.get("mask_shapes", ()))
        return shape_ids

    def binding_is_current(
        binding: Any,
        visible_shape_ids: set[str],
        has_authoritative_layers: bool,
        fallback_shape_id: Optional[str] = None,
        stack_clip_id: Optional[str] = None,
        *,
        include_legacy_frames: bool = False,
    ) -> bool:
        """Ignore bindings for another clip or deleted/missing mask shapes."""
        if not binding_has_results(
                binding, include_legacy_frames=include_legacy_frames):
            return False
        assert isinstance(binding, dict)
        clip_id = str(binding.get("clip_id") or stack_clip_id or "")
        if allowed_clip_ids is not None:
            if not clip_id or clip_id not in allowed_clip_ids:
                return False
        shape_id = str(binding.get("shape_id") or fallback_shape_id or "")
        if has_authoritative_layers and (
                not shape_id or shape_id not in visible_shape_ids):
            return False
        return True

    for stack in stacks or []:
        stack_layers = _stack_layer_dicts(stack)
        all_layers.extend(stack_layers)
        # ColorStackData v2 snapshots are the authoritative mask list. Empty
        # legacy snapshots cannot prove that a stored binding is orphaned.
        has_authoritative_layers = bool(getattr(stack, "layers", ()))
        visible_shape_ids = collect_visible_shape_ids(stack_layers)
        stack_clip_id = getattr(stack, "clip_id", None)
        trackings = getattr(stack, "trackings", None)
        if isinstance(trackings, dict):
            # TrackingStore serializes its empty state as {}, but older or
            # migrated projects may retain only schema metadata.
            if "bindings" in trackings:
                bindings = trackings.get("bindings")
                if isinstance(bindings, dict):
                    binding_values = bindings.items()
                elif isinstance(bindings, (list, tuple)):
                    binding_values = ((None, binding) for binding in bindings)
                else:
                    binding_values = ()
                has_tracking = has_tracking or any(
                    binding_is_current(
                        binding, visible_shape_ids, has_authoritative_layers,
                        str(key) if key is not None else None,
                        str(stack_clip_id) if stack_clip_id else None,
                    )
                    for key, binding in binding_values
                )
            else:
                # Legacy payloads were maps keyed by window id. An empty
                # frame/result map is only a placeholder, not active tracking.
                legacy_bindings = (
                    (key, value) for key, value in trackings.items()
                    if key not in {"schema_version", "version"}
                )
                has_tracking = has_tracking or any(
                    binding_is_current(
                        binding, visible_shape_ids, has_authoritative_layers,
                        str(key),
                        str(stack_clip_id) if stack_clip_id else None,
                        include_legacy_frames=True,
                    )
                    for key, binding in legacy_bindings
                )
        elif trackings:
            has_tracking = True
        # P1-COLOR-13: 栈级 per-clip 降噪参数自动检测（工程数据驱动，
        # 不依赖引擎瞬态；与显式 video_denoise_enabled 参数取 OR）
        vd = getattr(stack, "video_denoise", None) or {}
        if isinstance(vd, dict) and (
                (vd.get("spatial") or {}).get("enabled")
                or (vd.get("temporal") or {}).get("enabled")):
            has_stack_denoise = True
    report = build_delivery_matrix_report(all_layers, target)
    if has_tracking and target != TARGET_STILL and "windows" not in {
            r.operation for r in report.rows}:
        # 旧版栈可能未保存完整蒙版快照；通过上面的剪辑/窗口校验后，
        # 仍需要单独报告其跟踪数据无法由 LUT/时间线导出表达。
        meta = _OPERATIONS["tracking"]
        rows = report.rows + (DeliveryRow(
            operation="tracking", delivery=meta[target],
            label_key=meta["label_key"], label=meta["label"]),)
        report = DeliveryMatrixReport(target=target, rows=tuple(rows))
    if (video_denoise_enabled or has_stack_denoise) and \
            "video_denoise" not in {r.operation for r in report.rows}:
        meta = _OPERATIONS["video_denoise"]
        rows = report.rows + (DeliveryRow(
            operation="video_denoise", delivery=meta[target],
            label_key=meta["label_key"], label=meta["label"]),)
        report = DeliveryMatrixReport(target=target, rows=tuple(rows))
    return report


def sequence_color_stacks(sequence: Any, project: Any) -> List[Any]:
    """收集时间线视频轨 clips 引用的 ColorStackData（去重，保序）。"""
    stacks: List[Any] = []
    seen: set = set()
    color_stacks = getattr(project, "color_stacks", {}) or {}
    for track in getattr(sequence, "video_tracks", ()) or ():
        for clip in getattr(track, "clips", ()) or ():
            stack_id = getattr(clip, "color_stack_id", None)
            if not stack_id or stack_id in seen:
                continue
            seen.add(stack_id)
            stack = color_stacks.get(stack_id)
            if stack is not None:
                stacks.append(stack)
    return stacks


def sequence_video_clip_ids(sequence: Any) -> set[str]:
    """Return clip ids that can contribute video to this sequence export."""
    clip_ids: set[str] = set()
    for track in getattr(sequence, "video_tracks", ()) or ():
        if (not getattr(track, "is_enabled", True)
                or not getattr(track, "is_visible", True)):
            continue
        for clip in getattr(track, "clips", ()) or ():
            if not getattr(clip, "is_enabled", True):
                continue
            clip_id = getattr(clip, "clip_id", None)
            if clip_id:
                clip_ids.add(str(clip_id))
    return clip_ids


# ---------------------------------------------------------------------------
# 文本 / preflight 输出
# ---------------------------------------------------------------------------

_DELIVERY_LABELS = {
    DELIVERY_FULL: ("delivery.result_full", "完整保留"),
    DELIVERY_BAKED: ("delivery.result_baked", "烘焙"),
    DELIVERY_DROPPED: ("delivery.result_dropped", "丢失"),
}

_DETAIL_BY_TARGET = {
    TARGET_VIDEO: {
        DELIVERY_DROPPED: ("delivery.detail_video_dropped",
                           "时间线视频导出为 CPU 管线，该效果不会被应用"),
    },
    TARGET_LUT: {
        DELIVERY_BAKED: ("delivery.detail_lut_baked",
                         "烘焙进 3D LUT 网格（存在量化/插值误差）"),
        DELIVERY_DROPPED: ("delivery.detail_lut_dropped",
                           "逐像素空间效果无法用 3D LUT 表达"),
    },
    TARGET_STILL: {},
}


def _tr(tr: Callable[[str, str], str] | None, key: str, fallback: str) -> str:
    """经翻译回调取文案；回调缺失/抛错/返回 key 时回退 ``fallback``。"""
    if tr is None:
        return fallback
    try:
        value = tr(key, fallback)
    except Exception:
        return fallback
    return value if value and value != key else fallback


def row_label(row: DeliveryRow, tr: Callable[[str, str], str] | None = None) -> str:
    """操作行的显示名（i18n key 优先，回退内置标签）。"""
    return _tr(tr, row.label_key, row.label)


def row_delivery_label(row: DeliveryRow, tr: Callable[[str, str], str] | None = None) -> str:
    """交付结果（完整保留/烘焙/丢失）的显示文案。"""
    key, fallback = _DELIVERY_LABELS[row.delivery]
    return _tr(tr, key, fallback)


def format_report_text(report: DeliveryMatrixReport, tr: Callable[[str, str], str] | None = None) -> str:
    """人类可读报告文本（状态栏/日志/对话框正文通用）。"""
    target_names = {
        TARGET_VIDEO: ("delivery.target_video", "时间线视频导出"),
        TARGET_STILL: ("delivery.target_still", "静态图导出"),
        TARGET_LUT: ("delivery.target_lut", "3D LUT 导出"),
    }
    key, fallback = target_names[report.target]
    lines = [_tr(tr, key, fallback)]
    if not report.rows:
        lines.append(_tr(tr, "delivery.no_operations", "没有检测到调色操作"))
        return "\n".join(lines)
    for row in report.rows:
        if row.detail_key:
            detail = _tr(tr, row.detail_key, row.detail)
        else:
            detail_key, detail_fallback = _DETAIL_BY_TARGET[report.target].get(
                row.delivery, ("", ""))
            detail = _tr(tr, detail_key, detail_fallback) if detail_key else ""
        line = f"• {row_label(row, tr)} — {row_delivery_label(row, tr)}"
        if detail:
            line += f"：{detail}"
        lines.append(line)
    return "\n".join(lines)


# ── M05.2 · 逐后端能力矩阵（消费者 × 载荷元素） ─────────────────────
#
# 声明每个调色载荷元素在各真实消费者下的支持级别（与 _OPERATIONS 的
# video 目标一致——video 导出主路径是 CPU 管线）。级别：
#   full    = 像素级应用
#   baked   = 需先 bake（如 LUT 导出）或经等价产物
#   dropped = 该后端不应用（消费方必须显式告知，不得静默）
# 更新规则：实现状态变化时同步本表与 _OPERATIONS，测试强制一致。

CAP_FULL = "full"
CAP_BAKED = "baked"
CAP_DROPPED = "dropped"

#: 消费者键（真实后端，非计划抽象）
CONSUMERS = (
    "color_still",        # Color 页整图渲染（GPU 管线，含高级段）
    "edit_preview_cpu",   # Edit 时间线 CPU 预览 fallback
    "edit_preview_gpu",   # Edit 时间线 GPU 预览
    "video_export_cpu",   # 时间线视频导出主路径（CPU 管线）
    "lut_export",         # LUT 导出（.cube）
)

#: op → {consumer: 级别}；未列出的消费者视同 dropped（fail-closed）
CONSUMER_CAPABILITY_MATRIX: Dict[str, Dict[str, str]] = {
    "primary":        {"color_still": CAP_FULL, "edit_preview_cpu": CAP_FULL,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_FULL},
    "texture":        {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED},
    "curves":         {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED,
                       "lut_export": CAP_BAKED},
    "hsl_curves":     {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_FULL,
                       "lut_export": CAP_BAKED},
    "mixer":          {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED,
                       "lut_export": CAP_BAKED},
    "soft_clip":      {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_FULL,
                       "lut_export": CAP_BAKED},
    "lut":            {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED,
                       "lut_export": CAP_BAKED},
    "qualifier":      {"color_still": CAP_FULL, "edit_preview_cpu": CAP_FULL,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_FULL},
    "qualifier_postprocess": {"color_still": CAP_FULL,
                              "edit_preview_cpu": CAP_DROPPED,
                              "edit_preview_gpu": CAP_FULL,
                              "video_export_cpu": CAP_DROPPED},
    "windows":        {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED},
    "tracking":       {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED},
    "ai_matte":       {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED},
    "plugins":        {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED},
    "warper":         {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED},
    "effects":        {"color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED},
    "video_denoise":  {"color_still": CAP_FULL, "edit_preview_cpu": CAP_FULL,
                       "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_FULL},
    "chain_segments_basic": {  # 调色链段（组前/微调/组后）基础参数
        "color_still": CAP_FULL, "edit_preview_cpu": CAP_FULL,
        "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_FULL},
    "chain_segments_advanced": {  # 链段含 curves/LUT 等高级操作
        "color_still": CAP_FULL, "edit_preview_cpu": CAP_DROPPED,
        "edit_preview_gpu": CAP_FULL, "video_export_cpu": CAP_DROPPED},
}

_LEVEL_TO_TARGET_LEVEL = {CAP_FULL: "full", CAP_BAKED: "baked",
                          CAP_DROPPED: "dropped"}


def capability_for(op: str, consumer: str) -> str:
    """查询 op 在 consumer 下的支持级别（未声明 = dropped，fail-closed）。"""
    return CONSUMER_CAPABILITY_MATRIX.get(op, {}).get(consumer, CAP_DROPPED)


def delivery_preflight_issues(
    report: DeliveryMatrixReport, tr: Callable[[str, str], str] | None = None
) -> List[Tuple[str, str]]:
    """把有损行转成 (stable_code, message)，供视频导出 preflight 聚合。

    code 形如 ``color.delivery.dropped.curves``，severity 固定 WARNING
    （有损但不阻断，由 UI 确认门槛把关）。
    """
    issues: List[Tuple[str, str]] = []
    for row in report.rows:
        if not row.is_lossy:
            continue
        if row.detail_key:
            detail = _tr(tr, row.detail_key, row.detail)
        else:
            detail_key, detail_fallback = _DETAIL_BY_TARGET[report.target].get(
                row.delivery, ("", ""))
            detail = _tr(tr, detail_key, detail_fallback) if detail_key else ""
        message = f"{row_label(row, tr)} — {row_delivery_label(row, tr)}"
        if detail:
            message += f"：{detail}"
        issues.append((f"color.delivery.{row.delivery}.{row.operation}", message))
    return issues
