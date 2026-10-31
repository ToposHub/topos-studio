"""
SubtitleHandleController — Edit 页面字幕片段就地变换手柄控制器.

复用 composite 页面 viewer_handles.py 的纯函数：
- compute_transform_handles: 计算手柄位置（中心/pivot/scale/rotate）
- compute_transform_delta_params: 根据鼠标 drag 计算参数增量
- find_nearest_handle: 命中测试
- get_transform_handle_definitions: 生成 HandleDefinition 列表

SubtitleClip 的字段命名与 TextNode 完全对齐（translate_x/y、scale_x/y、rotation、
pivot_*），所以 viewer_handles 的函数能直接以 clip.to_dict() 作为 original_params 输入。

坐标约定
--------
- image_ndc_rect：composite canvas 在 viewport 的 NDC 矩形 (x0, y0, x1, y1)
  → 由 ProgramViewer 的 _calculate_display_rect() 提供（视频画面区域）
- image pixel space：Y-down、top-left origin、单位 = canvas 像素
- screen pixel space：QMouseEvent.position() 返回的 widget 像素坐标

控制流（与 composite CompositePage 对齐）
-----------------------------------------
1. 用户选中字幕片段 → EditController 调 viewer.set_selected_subtitle_clip(clip)
2. viewer.paintGL → controller.draw_handles(painter)
3. mousePress → controller.hit_test(pos) → 命中则 start_drag + emit started
4. mouseMove → controller.update_drag(pos) → emit updated(clip_id, delta_dict)
5. mouseRelease → controller.end_drag() → emit ended(clip_id, final_dict, original_dict)

emit 的 delta_dict 直接喂给 UpdateSubtitleClipCommand(updates=delta_dict)。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPen, QBrush, QPainter, QPolygonF

from src.features.composite.ui.viewer_handles import (
    TransformHandlesData,
    compute_transform_handles,
    compute_transform_delta_params,
    get_transform_handle_definitions,
    find_nearest_handle,
    ndc_to_screen_pixels,
    screen_pixels_to_ndc,
)

if TYPE_CHECKING:
    from src.features.edit.ui.program_viewer import ProgramViewer
    from src.features.edit.core.timeline_v2 import SubtitleClip

logger = logging.getLogger(__name__)


# ── 颜色常量（与 composite viewer_handles 内部一致） ──
HANDLE_COLOR_CENTER = (1.0, 1.0, 1.0, 0.94)      # 白：中心/平移
HANDLE_COLOR_PIVOT = (1.0, 0.78, 0.0, 0.94)      # 黄：pivot
HANDLE_COLOR_SCALE_X = (0.30, 0.85, 0.40, 0.94)  # 绿：水平缩放
HANDLE_COLOR_SCALE_Y = (0.35, 0.65, 1.0, 0.94)   # 蓝：垂直缩放
HANDLE_COLOR_ROTATE = (0.35, 0.95, 0.95, 0.94)   # 青：旋转
HANDLE_COLOR_CORNER = (1.0, 0.55, 0.30, 0.94)    # 橙：角点缩放
HANDLE_COLOR_FRAME = (1.0, 1.0, 1.0, 0.55)       # 半透明白：边框线
HANDLE_COLOR_HOVER = (1.0, 1.0, 0.0, 1.0)        # 黄：hover 高亮


class SubtitleHandleController:
    """字幕片段变换手柄控制器（无 widget 依赖，纯逻辑）。

    由 ProgramViewer 持有，接收鼠标事件、绘制手柄。
    参数变化通过回调通知（而非直接修改模型，避免循环依赖）。
    """

    def __init__(self, viewer: "ProgramViewer") -> None:
        self._viewer = viewer

        # 当前选中的字幕片段
        self._selected_clip: Optional["SubtitleClip"] = None
        # 已解析的分层样式（track-default ⊕ cue-override）；None 走 legacy 扁平字段
        self._resolved_style: Optional[dict] = None

        # 字形边界只依赖文本/样式/画布，不依赖 transform。手柄绘制、命中测试
        # 和高频鼠标拖拽都会查询它；重复创建 TextNode 并做字体测量会抢占播放
        # 所在的 UI 线程，因此在控制器内保留一项缓存。
        self._bbox_cache_key: Optional[tuple] = None
        self._bbox_cache: Optional[tuple[float, float, int, int]] = None

        # 当前画布尺寸（用于手柄位置计算和坐标转换）
        self._canvas_width: int = 1920
        self._canvas_height: int = 1080

        # drag 状态
        self._active_handle: Optional[str] = None  # 当前正在拖的手柄 id
        self._hover_handle: Optional[str] = None    # 当前 hover 的手柄 id
        self._drag_start_screen: Optional[QPointF] = None
        self._drag_current_screen: Optional[QPointF] = None
        self._original_params: Optional[dict] = None  # drag 开始时的 clip 字段快照
        # AUD-21008: 本次 drag 已应用（last-write-wins）的参数增量。
        # end_drag 的 final 必须与 original 处于同一取值空间（resolved
        # style ⊕ 拖拽增量），否则轨道 default_style 覆盖某字段时会把手
        # 段基线差当作变化提交。
        self._applied_delta: Optional[dict] = None

        # 回调（由 viewer 注入）
        self.on_drag_started = None  # callable(clip_id: str)
        self.on_drag_updated = None  # callable(clip_id: str, delta: dict)
        self.on_drag_ended = None    # callable(clip_id: str, final: dict, original: dict)

    # ── 公共 API ──

    def set_clip(self, clip: Optional["SubtitleClip"],
                 style: Optional[dict] = None) -> None:
        """设置当前选中的字幕片段。None 表示无选择（隐藏手柄）。

        ``style`` 为已解析的分层样式字典（见 captions.model.resolve_style），
        用于让手柄 bbox 与实际渲染像素一致（SUB-P2-01/06）。``None`` 时读
        clip 的扁平字段（旧行为）。
        """
        prev_id = self._selected_clip.clip_id if self._selected_clip else None
        new_id = clip.clip_id if clip is not None else None
        self._selected_clip = clip
        self._resolved_style = style
        if prev_id != new_id:
            self._bbox_cache_key = None
            self._bbox_cache = None
            # 选择变化时清除 drag 状态
            self._active_handle = None
            self._hover_handle = None
            self._drag_start_screen = None
            self._drag_current_screen = None
            self._original_params = None

    def set_canvas_size(self, width: int, height: int) -> None:
        """设置画布尺寸（用于坐标转换）。"""
        self._canvas_width = max(1, int(width))
        self._canvas_height = max(1, int(height))

    def has_clip(self) -> bool:
        return self._selected_clip is not None

    def selected_clip_id(self) -> Optional[str]:
        """Return the selected cue id without exposing the cue model."""
        return self._selected_clip.clip_id if self._selected_clip is not None else None

    # ── 手柄位置计算 ──

    def _get_image_ndc_rect(self) -> tuple[float, float, float, float]:
        """获取视频画面在 viewport 的 NDC 矩形 (x0, y0, x1, y1)。"""
        try:
            x, y, w, h = self._viewer._calculate_display_rect()
            return (float(x), float(y), float(x + w), float(y + h))
        except Exception:
            return (-1.0, -1.0, 1.0, 1.0)

    def _get_device_pixel_ratio(self) -> float:
        try:
            screen = self._viewer.screen()
            if screen is not None:
                return float(screen.devicePixelRatio())
        except Exception:
            pass
        return 1.0

    def _get_subtitle_bbox(self) -> tuple[float, float, int, int]:
        """获取字幕的字形 bbox (x, y, w, h)（canvas pixel space）。

        用于让手柄贴合字形（AE-style），而非整个画布。
        """
        clip = self._selected_clip
        if clip is None:
            return (0.0, 0.0, self._canvas_width, self._canvas_height)
        cache_key = self._subtitle_bbox_cache_key(clip)
        if cache_key == self._bbox_cache_key and self._bbox_cache is not None:
            return self._bbox_cache
        try:
            from src.features.edit.core.subtitle_renderer import measure_subtitle_bbox
            x, y, w, h = measure_subtitle_bbox(
                clip, self._canvas_width, self._canvas_height,
                style=self._resolved_style,
            )
            bbox = (float(x), float(y), max(1, int(round(w))), max(1, int(round(h))))
            self._bbox_cache_key = cache_key
            self._bbox_cache = bbox
            return bbox
        except Exception:
            logger.exception("Failed to measure subtitle bbox for handles")
            return (0.0, 0.0, self._canvas_width, self._canvas_height)

    def _subtitle_bbox_cache_key(self, clip: "SubtitleClip") -> tuple:
        """Return the subset of cue state that can alter its glyph bounds."""
        style_key = repr(sorted(self._resolved_style.items())) \
            if self._resolved_style is not None else None
        return (
            self._canvas_width, self._canvas_height, clip.clip_id,
            clip.text, clip.font_family, clip.font_size, clip.font_weight,
            clip.italic, clip.halign, clip.valign, clip.letter_spacing,
            clip.line_spacing, clip.outline_enable, clip.outline_width,
            clip.shadow_enable, clip.shadow_offset_x, clip.shadow_offset_y,
            clip.shadow_blur, style_key,
        )

    def _compute_handles(self) -> Optional[TransformHandlesData]:
        """计算当前字幕片段的手柄位置。"""
        clip = self._selected_clip
        if clip is None:
            return None
        bbox_x, bbox_y, bbox_w, bbox_h = self._get_subtitle_bbox()

        # compute_transform_handles 需要 pivot 在 image (bbox) 空间。
        # SubtitleClip.pivot_x/y 是 canvas 空间；center 模式下函数内部用 image_w/2。
        # 我们把 SubtitleClip 字段按 TextNode 期望传入：
        # - pivot_mode = center：函数用 bbox 中心，与 TextNode 行为一致
        # - pivot_mode = custom：把 canvas 坐标 pivot 转 bbox 局部坐标
        style = self._resolved_style or {}
        pivot_mode = style.get("pivot_mode", clip.pivot_mode)
        pivot_x = float(style.get("pivot_x", clip.pivot_x))
        pivot_y = float(style.get("pivot_y", clip.pivot_y))
        if pivot_mode == "custom":
            piv_x = pivot_x - bbox_x
            piv_y = pivot_y - bbox_y
        else:
            piv_x = bbox_w / 2.0
            piv_y = bbox_h / 2.0

        # viewer_handles 的小图像锚点在画布左下方；字幕纹理需要放到 bbox
        # 的实际 Y-down 位置。因此 Y 基准必须和 TextNode 的 transform pass
        # 一致：bbox_y + bbox_h - canvas_h。此前遗漏了这两个项，导致控制手柄
        # 与字幕本体在垂直方向明显错位。
        eff_tx = bbox_x + float(style.get("translate_x", clip.translate_x))
        eff_ty = bbox_y + bbox_h - self._canvas_height + float(style.get("translate_y", clip.translate_y))

        image_ndc_rect = self._get_image_ndc_rect()
        try:
            return compute_transform_handles(
                translate_x=eff_tx,
                translate_y=eff_ty,
                scale_x=float(style.get("scale_x", clip.scale_x)),
                scale_y=float(style.get("scale_y", clip.scale_y)),
                rotation=float(style.get("rotation", clip.rotation)),
                pivot_mode=pivot_mode,
                pivot_x=piv_x,
                pivot_y=piv_y,
                image_width=bbox_w,
                image_height=bbox_h,
                image_ndc_rect=image_ndc_rect,
                canvas_width=self._canvas_width,
                canvas_height=self._canvas_height,
            )
        except Exception:
            logger.exception("Failed to compute subtitle handles")
            return None

    # ── 命中测试 ──

    def hit_test(self, screen_pos: QPointF) -> Optional[str]:
        """命中测试。返回命中的手柄 id，或 None。

        screen_pos 是 widget-local 像素坐标（QMouseEvent.position()）。
        """
        if self._selected_clip is None:
            return None
        handles_data = self._compute_handles()
        if handles_data is None:
            return None
        try:
            defs = get_transform_handle_definitions(
                handles_data,
                hover_handle=self._hover_handle,
                active_handle=self._active_handle,
            )
            # ``center`` is a legacy virtual move handle.  In center-pivot
            # mode it overlaps the visible pivot dot and wins the shared
            # helper's tie-breaker, turning an attempted pivot move into a
            # translation.  Subtitle body drags already implement move.
            defs = [definition for definition in defs
                    if definition.handle_type.value != "center"]
            dpr = self._get_device_pixel_ratio()
            handle_id = find_nearest_handle(
                screen_pos, defs,
                viewport_width=float(self._viewer.width()),
                viewport_height=float(self._viewer.height()),
                device_pixel_ratio=dpr,
                hit_radius=12.0,
            )
            return handle_id
        except Exception:
            logger.exception("Subtitle handle hit_test failed")
            return None

    def hit_test_body(self, screen_pos: QPointF) -> bool:
        """命中测试：是否点在字幕 bbox 区域内（用于平移）。"""
        if self._selected_clip is None:
            return False
        handles_data = self._compute_handles()
        if handles_data is None:
            return False
        try:
            # 用 frame_corners（旋转后的 4 角）做凸多边形命中
            corners = handles_data.frame_corners
            if not corners or len(corners) != 4:
                return False
            dpr = self._get_device_pixel_ratio()
            screen_corners = [
                ndc_to_screen_pixels(c, float(self._viewer.width()),
                                     float(self._viewer.height()), dpr)
                for c in corners
            ]
            # 凸多边形命中（点在四边形内）
            return _point_in_quad((screen_pos.x(), screen_pos.y()), screen_corners)
        except Exception:
            return False

    # ── drag 流程 ──

    def start_drag(self, handle_id: Optional[str], screen_pos: QPointF,
                   modifiers: int) -> bool:
        """开始拖拽。handle_id 是命中的手柄（None 表示 body 平移）。

        返回 True 表示开始 drag（应阻止其它 mousePress 处理）。
        """
        if self._selected_clip is None:
            return False
        # 如果命中手柄或 body，开始 drag
        if handle_id is None and not self.hit_test_body(screen_pos):
            return False
        clip = self._selected_clip
        # handle_id：body 用 "move"（TransformHandleType.CENTER）
        self._active_handle = handle_id or "move"
        self._drag_start_screen = QPointF(screen_pos)
        self._drag_current_screen = QPointF(screen_pos)
        # AUD-21008: 清空上一次 drag 的增量记录
        self._applied_delta = {}
        # 快照原始字段（用于 drag 期间增量计算 + undo 基线）
        style = self._resolved_style or {}
        self._original_params = {
            "translate_x": float(style.get("translate_x", clip.translate_x)),
            "translate_y": float(style.get("translate_y", clip.translate_y)),
            "scale_x": float(style.get("scale_x", clip.scale_x)),
            "scale_y": float(style.get("scale_y", clip.scale_y)),
            "rotation": float(style.get("rotation", clip.rotation)),
            "pivot_mode": style.get("pivot_mode", clip.pivot_mode),
            "pivot_x": float(style.get("pivot_x", clip.pivot_x)),
            "pivot_y": float(style.get("pivot_y", clip.pivot_y)),
        }
        if self.on_drag_started is not None:
            try:
                self.on_drag_started(clip.clip_id)
            except Exception:
                logger.exception("on_drag_started callback failed")
        return True

    def update_drag(self, screen_pos: QPointF, modifiers: int) -> None:
        """更新 drag（mouseMoveEvent 调用）。计算 delta 并通知 viewer。"""
        if self._selected_clip is None or self._active_handle is None:
            return
        if self._drag_start_screen is None or self._original_params is None:
            return
        self._drag_current_screen = QPointF(screen_pos)

        # 把屏幕坐标转 image (bbox) pixel space
        bbox_x, bbox_y, bbox_w, bbox_h = self._get_subtitle_bbox()
        start_img = self._screen_to_image_px(self._drag_start_screen, bbox_x, bbox_y)
        cur_img = self._screen_to_image_px(self._drag_current_screen, bbox_x, bbox_y)

        try:
            # compute_transform_delta_params 用 original_params，但 pivot_mode 是
            # custom 时 pivot_x/y 期望是 image 空间。我们存的是 canvas 空间，
            # 这里转一下。
            params_for_calc = dict(self._original_params)
            clip = self._selected_clip
            if params_for_calc.get("pivot_mode") == "custom":
                # _original_params already contains the resolved track/cue
                # style; do not fall back to the cue's flat legacy fields.
                params_for_calc["pivot_x"] = (
                    float(params_for_calc["pivot_x"]) - bbox_x
                )
                params_for_calc["pivot_y"] = (
                    float(params_for_calc["pivot_y"]) - bbox_y
                )

            delta = compute_transform_delta_params(
                handle_id=self._active_handle,
                drag_start_image_pos=start_img,
                drag_current_image_pos=cur_img,
                original_params=params_for_calc,
                image_width=bbox_w,
                image_height=bbox_h,
                modifiers=modifiers,
                max_scale=20.0,
            )
        except Exception:
            logger.exception("compute_transform_delta_params failed")
            return

        if not delta:
            return

        # 把 pivot 的 image 空间值转回 canvas 空间（custom 模式）
        new_pivot_mode = delta.get("pivot_mode", clip.pivot_mode)
        if "pivot_x" in delta and new_pivot_mode == "custom":
            delta["pivot_x"] = delta["pivot_x"] + bbox_x
        if "pivot_y" in delta and new_pivot_mode == "custom":
            delta["pivot_y"] = delta["pivot_y"] + bbox_y

        # Keep handle geometry in sync with the pointer immediately.  The
        # immutable timeline and subtitle overlay are deliberately coalesced
        # to 60 Hz by EditController, but waiting for that round trip made the
        # handles visibly lag and could leave release with stale local values.
        self._apply_preview_delta(delta)

        if self.on_drag_updated is not None:
            try:
                self.on_drag_updated(clip.clip_id, delta)
            except Exception:
                logger.exception("on_drag_updated callback failed")

    def end_drag(self) -> None:
        """结束 drag。通知 viewer 最终 delta（用于 undo command）。"""
        if self._selected_clip is None or self._active_handle is None:
            self._reset_drag()
            return
        clip = self._selected_clip
        original = dict(self._original_params) if self._original_params else {}
        # AUD-21008: final 与 original 取同一取值空间 —— original 快照自
        # resolved style（轨道默认 ⊕ cue 覆盖），而 clip 扁平字段可能含
        # 与轨道默认不同的 legacy 基线值；若 final 直接读扁平字段，零位移
        # 的"按下-释放"也会产出非空 delta，把 cue 扁平值当作变化提交，静默
        # 抵消轨道 default_style。因此 final = original ⊕ 本次 drag 已应用
        # 的增量（update_drag 期间经 _apply_preview_delta 记录）。
        final = dict(original)
        if self._applied_delta:
            final.update({
                k: v for k, v in self._applied_delta.items() if k in original
            })
        # 只发变化的字段
        delta = {k: v for k, v in final.items() if original.get(k) != v}
        # Always notify the controller for an active drag.  Its transform
        # session owns the synchronous last-write-wins delta; ``clip`` can be
        # stale until the 16 ms preview coalescer refreshes this controller.
        # Suppressing an empty local delta here used to lose fast move-release
        # gestures entirely.
        if self.on_drag_ended is not None:
            try:
                self.on_drag_ended(clip.clip_id, delta, original)
            except Exception:
                logger.exception("on_drag_ended callback failed")
        self._reset_drag()

    def update_hover(self, screen_pos: QPointF) -> None:
        """更新 hover 状态（mouseMoveEvent 未 drag 时调）。"""
        if self._selected_clip is None or self._active_handle is not None:
            return
        new_hover = self.hit_test(screen_pos)
        if new_hover != self._hover_handle:
            self._hover_handle = new_hover
            self._viewer.update()

    def _reset_drag(self) -> None:
        self._active_handle = None
        self._drag_start_screen = None
        self._drag_current_screen = None
        self._original_params = None
        self._applied_delta = None

    def _apply_preview_delta(self, delta: dict) -> None:
        """Apply a validated drag result to this controller's local snapshot."""
        clip = self._selected_clip
        if clip is None:
            return
        allowed = {
            "translate_x", "translate_y", "scale_x", "scale_y", "rotation",
            "pivot_mode", "pivot_x", "pivot_y",
        }
        updates = {key: value for key, value in delta.items() if key in allowed}
        if not updates:
            return
        # AUD-21008: 记录本次 drag 已应用的增量（同字段 last-write-wins），
        # end_drag 据此在同一取值空间内计算 final。
        if self._applied_delta is None:
            self._applied_delta = {}
        self._applied_delta.update(updates)
        self._selected_clip = replace(clip, **updates)
        self._viewer.update()

    # ── 坐标转换 ──

    def _screen_to_image_px(self, screen_pos: QPointF,
                            bbox_x: float, bbox_y: float) -> tuple[float, float]:
        """Convert widget pixels to subtitle-bbox-local, Y-down coordinates.

        ``compute_transform_delta_params`` expects a top-left-origin local
        coordinate system: the bbox's corners are ``(0, 0)`` and ``(w, h)``.
        Mapping directly by bbox dimensions incorrectly shrinks a canvas-wide
        pointer move to the width of the text, making movement sluggish and
        giving rotation the wrong pivot vector.
        """
        ndc_rect = self._get_image_ndc_rect()  # (x0, y0, x1, y1) in NDC
        dpr = self._get_device_pixel_ratio()
        ndc = screen_pixels_to_ndc(
            (screen_pos.x(), screen_pos.y()),
            float(self._viewer.width()), float(self._viewer.height()), dpr,
        )
        # NDC → displayed canvas normalized [0,1].
        rx0, ry0, rx1, ry1 = ndc_rect
        rect_w = rx1 - rx0
        rect_h = ry1 - ry0
        if abs(rect_w) < 1e-6 or abs(rect_h) < 1e-6:
            return (0.0, 0.0)
        norm_x = (ndc[0] - rx0) / rect_w  # 0..1
        norm_y_up = (ndc[1] - ry0) / rect_h
        canvas_x = norm_x * self._canvas_width
        canvas_y = (1.0 - norm_y_up) * self._canvas_height
        return (canvas_x - bbox_x, canvas_y - bbox_y)

    # ── 绘制 ──

    def draw_handles(self, painter: QPainter) -> None:
        """绘制手柄（在 paintGL 内、QPainter 激活时调）。"""
        if self._selected_clip is None:
            return
        handles_data = self._compute_handles()
        if handles_data is None:
            return
        try:
            self._draw_frame_outline(painter, handles_data)
            self._draw_handles_points(painter, handles_data)
        except Exception:
            logger.exception("Failed to draw subtitle handles")

    def _draw_frame_outline(self, painter: QPainter,
                            handles_data: TransformHandlesData) -> None:
        """绘制字幕 bbox 的旋转边框（白线）。"""
        corners = handles_data.frame_corners
        if not corners or len(corners) != 4:
            return
        dpr = self._get_device_pixel_ratio()
        screen_corners = [
            ndc_to_screen_pixels(c, float(self._viewer.width()),
                                 float(self._viewer.height()), dpr)
            for c in corners
        ]
        poly = QPolygonF([QPointF(x, y) for x, y in screen_corners])
        pen = QPen(QColor.fromRgbF(*HANDLE_COLOR_FRAME), 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(poly)

    def _draw_handles_points(self, painter: QPainter,
                             handles_data: TransformHandlesData) -> None:
        """绘制手柄点（圆）。颜色按类型。"""
        dpr = self._get_device_pixel_ratio()
        vp_w = float(self._viewer.width())
        vp_h = float(self._viewer.height())

        # 手柄 id → NDC 位置 + 颜色
        handle_specs = [
            ("center", handles_data.center, HANDLE_COLOR_CENTER),
            ("pivot", handles_data.pivot, HANDLE_COLOR_PIVOT),
            ("scale_x_left", handles_data.scale_x_left, HANDLE_COLOR_SCALE_X),
            ("scale_x_right", handles_data.scale_x_right, HANDLE_COLOR_SCALE_X),
            ("scale_y_top", handles_data.scale_y_top, HANDLE_COLOR_SCALE_Y),
            ("scale_y_bottom", handles_data.scale_y_bottom, HANDLE_COLOR_SCALE_Y),
            ("scale_corner_tl", handles_data.scale_corner_tl, HANDLE_COLOR_CORNER),
            ("scale_corner_tr", handles_data.scale_corner_tr, HANDLE_COLOR_CORNER),
            ("scale_corner_bl", handles_data.scale_corner_bl, HANDLE_COLOR_CORNER),
            ("scale_corner_br", handles_data.scale_corner_br, HANDLE_COLOR_CORNER),
            ("rotate", handles_data.rotate, HANDLE_COLOR_ROTATE),
        ]
        radius = 5.0
        for handle_id, ndc_pos, color in handle_specs:
            sx, sy = ndc_to_screen_pixels(ndc_pos, vp_w, vp_h, dpr)
            is_active = (handle_id == self._active_handle)
            is_hover = (handle_id == self._hover_handle)
            draw_color = HANDLE_COLOR_HOVER if (is_active or is_hover) else color
            r = radius + 2 if (is_active or is_hover) else radius
            painter.setBrush(QBrush(QColor.fromRgbF(*draw_color)))
            painter.setPen(QPen(QColor(0, 0, 0, 180), 1))
            painter.drawEllipse(QPointF(sx, sy), r, r)

        # 旋转手柄连接线（从顶部中点到 rotate handle）
        try:
            # frame_corners: TL, TR, BR, BL
            tl = handles_data.frame_corners[0]
            tr = handles_data.frame_corners[1]
            top_mid_ndc = ((tl[0] + tr[0]) * 0.5, (tl[1] + tr[1]) * 0.5)
            rot_ndc = handles_data.rotate
            sx1, sy1 = ndc_to_screen_pixels(top_mid_ndc, vp_w, vp_h, dpr)
            sx2, sy2 = ndc_to_screen_pixels(rot_ndc, vp_w, vp_h, dpr)
            pen = QPen(QColor.fromRgbF(*HANDLE_COLOR_ROTATE), 1.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(QPointF(sx1, sy1), QPointF(sx2, sy2))
        except Exception:
            pass


def _point_in_quad(p: tuple[float, float],
                   quad: list[tuple[float, float]]) -> bool:
    """点是否在凸四边形内（4 个顶点，按顺序）。"""
    if len(quad) != 4:
        return False
    # 用 cross product 同号法
    px, py = p
    signs = []
    for i in range(4):
        x0, y0 = quad[i]
        x1, y1 = quad[(i + 1) % 4]
        cross = (x1 - x0) * (py - y0) - (y1 - y0) * (px - x0)
        signs.append(cross)
    # 全部非负或全部非正 → 在内部
    all_pos = all(s >= 0 for s in signs)
    all_neg = all(s <= 0 for s in signs)
    return all_pos or all_neg
