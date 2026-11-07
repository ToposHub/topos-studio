"""Shared production Window rasterization; QImage/OpenCV only, no GL or UI state.

ImageEngine and timeline graph renderers inherit/use this same implementation.
All caches belong to caller-owned MaskShape copies and are safe on render workers.
"""
from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QImage, QPainter, QPainterPath

from src.features.color.core.layer import MaskGroup, MaskShape


class WindowRasterizer:
    def render_items(self, items: Sequence[MaskShape | MaskGroup], width: int,
                     height: int, source_width: int | None = None) -> np.ndarray:
        """Same recursive Boolean composition as Color, without tracking UI state."""
        result = np.zeros((height, width), dtype="f4")
        first = True
        for item in items:
            if not item.visible:
                continue
            if isinstance(item, MaskGroup):
                mask = self.render_items(item.children, width, height, source_width) * item.opacity
                if item.invert:
                    mask = 1.0 - mask
            else:
                if item.type not in (MaskShape.CIRCLE, MaskShape.RECT, MaskShape.GRADIENT,
                                     MaskShape.CURVE, MaskShape.BRUSH, MaskShape.BITMAP):
                    raise ValueError(f"Unknown window shape: {item.type}")
                if item.type == MaskShape.BITMAP and item.bitmap_data is None:
                    raise ValueError("Window bitmap data is unavailable")
                mask = self._render_single_shape(item, width, height, source_width or width)
            if first:
                result, first = mask, False
            elif item.operation == MaskShape.OP_ADD:
                result = np.maximum(result, mask)
            elif item.operation == MaskShape.OP_SUBTRACT:
                result = np.clip(result - mask, 0.0, 1.0)
            elif item.operation == MaskShape.OP_INTERSECT:
                result = result * mask
        return result.astype("f4", copy=False)

    def _render_brush_mask_to_canvas(self, shape: MaskShape, target_w: int, target_h: int, img_w: int) -> np.ndarray:
        """渲染笔刷蒙版到指定尺寸的 canvas。
        
        将笔刷绘制逻辑提取为独立方法，供预览渲染和全分辨率导出共同调用，
        确保两者使用完全相同的算法。
        
        Args:
            shape: MaskShape 对象（类型必须为 MaskShape.BRUSH）
            target_w: 目标宽度（像素）
            target_h: 目标高度（像素）
            img_w: 原始图像宽度（用于计算笔刷半径缩放比例）
        
        Returns:
            shape_arr: float32 numpy 数组，范围 0.0-1.0，尺寸为 (target_h, target_w)
        """
        shape_arr = np.zeros((target_h, target_w), dtype=np.float32)
        
        # Get brush parameters
        brush_size = getattr(shape, 'brush_size', 50)
        brush_feather = getattr(shape, 'brush_feather', 0)
        brush_opacity = getattr(shape, 'brush_opacity', 1.0)
        brush_strokes = getattr(shape, 'brush_strokes', [])
        
        # Render each stroke
        for stroke in brush_strokes:
            points = stroke.get('points', [])
            stroke_size = stroke.get('size', brush_size)
            stroke_opacity = stroke.get('opacity', brush_opacity)
            stroke_feather = stroke.get('feather', brush_feather)
            stroke_flow = stroke.get('flow', 1.0)
            is_eraser = stroke.get('is_eraser', False)  # 橡皮擦模式
            
            if not points or len(points) < 2:
                continue
            
            # Convert normalized coordinates to pixel coordinates
            px_points = [(int(p[0] * target_w), int(p[1] * target_h)) for p in points]
            
            # Calculate brush radius in mask space (scale by image width)
            radius = max(2, int(stroke_size * target_w / img_w))
            feather_radius = max(0, int(stroke_feather * radius / 100.0))
            
            # Draw connected line segments with anti-aliasing
            # Use cv2.polylines for smooth connected strokes
            pts_array = np.array(px_points, dtype=np.int32).reshape((-1, 1, 2))
            
            # Create a temporary canvas for this stroke
            stroke_canvas = np.zeros((target_h, target_w), dtype=np.float32)
            
            # Draw thick lines connecting all points
            line_thickness = max(1, radius * 2)
            cv2.polylines(stroke_canvas, [pts_array], False, 1.0, thickness=line_thickness, lineType=cv2.LINE_AA)
            
            # Draw circles at each point to ensure rounded ends and fill gaps
            for px, py in px_points:
                cv2.circle(stroke_canvas, (px, py), radius, 1.0, -1)
            
            # Apply feather (Gaussian blur) for soft edges
            if feather_radius > 0:
                blur_ksize = max(3, int(feather_radius * 2 + 1))
                if blur_ksize % 2 == 0:
                    blur_ksize += 1
                stroke_canvas = self._fast_feather_blur(stroke_canvas, blur_ksize)
                # Renormalize after blur to maintain opacity
                max_val = stroke_canvas.max()
                if max_val > 0:
                    stroke_canvas = stroke_canvas / max_val
            
            # Apply opacity and flow
            stroke_canvas = stroke_canvas * stroke_opacity * stroke_flow
            
            # 橡皮擦模式：减法混合 / 画笔模式：加法混合
            if is_eraser:
                # Eraser mode: subtract from mask
                shape_arr = np.clip(shape_arr - stroke_canvas, 0.0, 1.0)
            else:
                # Brush mode: additive blending (accumulate strokes)
                shape_arr = np.clip(shape_arr + stroke_canvas, 0.0, 1.0)
        
        return shape_arr

    def _render_single_shape(self, shape: MaskShape, target_w: int, target_h: int, img_w: int) -> np.ndarray:
        """渲染单个 MaskShape 到指定尺寸的蒙版数组。

        Performance Optimization:
        - 分离形状生成、旋转和位移：
          * 形状缓存不含旋转和位置（circle/rect）
          * 旋转变化时只需 cv2.warpAffine（~0.5ms）
          * 位置变化时同样只需 warpAffine 平移（~0.5ms）
        - 形状生成（QPainter）只在形状/尺寸/羽化变化时执行

        Args:
            shape: MaskShape 对象
            target_w: 目标宽度（像素）
            target_h: 目标高度（像素）
            img_w: 原始图像宽度（用于笔刷缩放）

        Returns:
            float32 numpy 数组，范围 0.0-1.0
        """
        # Check full render cache first (exact match including rotation + position)
        full_key = shape._get_render_cache_key(target_w, target_h, img_w)
        if shape._render_cache is not None and shape._render_cache_key == full_key:
            return shape._render_cache.copy()

        has_edge_softness = shape.type == MaskShape.RECT and shape.edge_softness is not None
        has_rotation = shape.type not in (MaskShape.BITMAP, MaskShape.BRUSH) and abs(shape.rotation) > 0.01
        is_translatable = shape.type in ('circle', 'rect')

        if has_edge_softness:
            from src.features.color.core.window_softness import rasterize_rect_edge_softness
            shape_arr = rasterize_rect_edge_softness(
                target_w, target_h, shape.center, shape.size, shape.rotation, shape.edge_softness)
        elif has_rotation:
            rot_pad = max(target_w, target_h) * 3 // 4 + 4
            if is_translatable:
                # Canonical circles/rectangles usually fit inside the source
                # canvas. Rotation needs the full source shape, not a blanket
                # 3/4-frame border (over five million pixels at 1024x576).
                half_w = shape.size[0] * target_w
                half_h = shape.size[1] * target_h
                if shape.type == 'rect':
                    half_w *= 0.5
                    half_h *= 0.5
                required = int(np.ceil(max(
                    0.0, half_w - target_w * 0.5 + 2.0,
                    half_h - target_h * 0.5 + 2.0,
                )))
                rot_pad = min(rot_pad, required)

            # Shape cache excludes rotation AND position (for circle/rect)
            padded_key = ('padded', rot_pad) + shape._get_shape_cache_key(target_w, target_h, img_w)
            if shape._shape_cache is not None and shape._shape_cache_key == padded_key:
                shape_arr = shape._shape_cache.copy()
            else:
                shape_arr = self._generate_shape_texture(shape, target_w, target_h, img_w, pad=rot_pad)
                shape._shape_cache = shape_arr.copy()
                shape._shape_cache_key = padded_key

            # Cached circle/rect textures use canonical center; point-based
            # windows use their own pivot, matching QGraphicsItem rotation.
            ccx = (0.5 if is_translatable else shape.center[0]) * target_w + rot_pad
            ccy = (0.5 if is_translatable else shape.center[1]) * target_h + rot_pad
            M = cv2.getRotationMatrix2D((ccx, ccy), -shape.rotation, 1.0)
            # Produce exactly the old central crop directly. Keep source
            # padding for off-canvas geometry, but never allocate/warp output
            # pixels that are immediately discarded.
            M[:, 2] -= rot_pad
            shape_arr = cv2.warpAffine(shape_arr, M, (target_w, target_h),
                                       flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_CONSTANT,
                                       borderValue=0.0)

            # For circle/rect: translate from canonical center to actual position
            if is_translatable:
                dx = (shape.center[0] - 0.5) * target_w
                dy = (shape.center[1] - 0.5) * target_h
                if abs(dx) > 0.5 or abs(dy) > 0.5:
                    M_trans = np.float32([[1, 0, dx], [0, 1, dy]])
                    shape_arr = cv2.warpAffine(shape_arr, M_trans, (target_w, target_h),
                                               flags=cv2.INTER_LINEAR,
                                               borderMode=cv2.BORDER_CONSTANT,
                                               borderValue=0.0)
        else:
            # No rotation: use standard (unpadded) shape cache
            # Blur is applied AFTER translation on the final canvas,
            # so no feather padding needed — same as rendering at actual position.
            shape_key = shape._get_shape_cache_key(target_w, target_h, img_w)
            if shape._shape_cache is not None and shape._shape_cache_key == shape_key:
                shape_arr = shape._shape_cache.copy()
            else:
                shape_arr = self._generate_shape_texture(shape, target_w, target_h, img_w)
                shape._shape_cache = shape_arr.copy()
                shape._shape_cache_key = shape_key

            # For circle/rect: translate from canonical center to actual position
            if is_translatable:
                dx = (shape.center[0] - 0.5) * target_w
                dy = (shape.center[1] - 0.5) * target_h
                if abs(dx) > 0.5 or abs(dy) > 0.5:
                    M = np.float32([[1, 0, dx], [0, 1, dy]])
                    shape_arr = cv2.warpAffine(shape_arr, M, (target_w, target_h),
                                               flags=cv2.INTER_LINEAR,
                                               borderMode=cv2.BORDER_CONSTANT,
                                               borderValue=0.0)

        # Apply feather/blur/clean AFTER all transformations (rotation, translation, crop).
        # This ensures blur operates on the final target_w × target_h canvas with
        # BORDER_REFLECT_101, preventing any edge clipping — identical to v4 behavior.
        # Performance: _fast_feather_blur is O(1) per pixel, ~5ms for 512×512.

        # Apply feather (ONLY for vector masks - NOT for BITMAP/AI masks)
        if shape.type == MaskShape.CURVE and shape.has_per_point_feather:
            shape_arr = self._render_curve_feather_sdf(shape, shape_arr, target_w, target_h)
        elif not has_edge_softness and shape.feather > 0 and shape.type != MaskShape.BITMAP:
            blur_radius = int((shape.feather / 100.0) * 4.0 * (target_w / 5.0))
            if blur_radius > 0:
                if blur_radius % 2 == 0: blur_radius += 1
                shape_arr = self._fast_feather_blur(shape_arr, blur_radius)

        # Apply blur (works for ALL mask types including BITMAP)
        if hasattr(shape, 'blur') and shape.blur > 0:
            blur_radius = int((shape.blur / 100.0) * (target_w / 5.0))
            if blur_radius > 0:
                if blur_radius % 2 == 0: blur_radius += 1
                shape_arr = self._fast_feather_blur(shape_arr, blur_radius)

        # Apply clean_black
        if hasattr(shape, 'clean_black') and shape.clean_black > 0:
            kernel_size = max(3, int(shape.clean_black * 3.0))
            if kernel_size % 2 == 0: kernel_size += 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
            shape_arr = cv2.erode(shape_arr, kernel, iterations=1)

        # Apply clean_white
        if hasattr(shape, 'clean_white') and shape.clean_white > 0:
            kernel_size = max(3, int(shape.clean_white * 3.0))
            if kernel_size % 2 == 0: kernel_size += 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
            shape_arr = cv2.dilate(shape_arr, kernel, iterations=1)

        # Apply shape-level opacity and invert
        shape_arr *= shape.opacity
        if shape.invert:
            shape_arr = 1.0 - shape_arr

        # Store full render cache
        shape._render_cache = shape_arr
        shape._render_cache_key = full_key

        return shape_arr

    def _render_curve_feather_sdf(self, shape: MaskShape, sharp_mask: np.ndarray,
                                   target_w: int, target_h: int) -> np.ndarray:
        """使用有符号距离场 (SDF) 渲染 Curve 的逐点内外羽化。

        算法：
        1. 计算 signed distance field（正值=内部，负值=外部）
        2. 密采样 bezier 曲线，为每个采样点插值 fi/fo 值
        3. 用 distanceTransformWithLabels 找到每个像素最近的曲线采样点
        4. 用 smoothstep 距离 / 局部羽化值 计算 alpha

        性能：512×512 约 4-6ms（distanceTransform ~1ms + 查表 ~3ms）
        """
        h, w = sharp_mask.shape
        mask_uint8 = (sharp_mask > 0.5).astype(np.uint8)

        # 1. 计算 signed distance field
        dist_inside = cv2.distanceTransform(mask_uint8, cv2.DIST_L2, 5)
        dist_outside = cv2.distanceTransform(1 - mask_uint8, cv2.DIST_L2, 5)
        sdf = dist_inside.astype(np.float32) - dist_outside.astype(np.float32)

        # 2. 羽化缩放因子（与 _fast_feather_blur 一致）
        feather_scale = 4.0 * (target_w / 5.0) / 100.0  # pixels per feather unit

        # 3. 构建曲线采样点和羽化查找
        # 从 QPainterPath 采样不够方便，直接从控制点采样 bezier
        pts = shape.points
        n_ctrl = len(pts)
        if n_ctrl < 2:
            return sharp_mask

        # 每个 bezier 段细分为 sub_div 个采样点
        sub_div = 30
        samples = []  # [(pixel_x, pixel_y, fi_pixels, fo_pixels), ...]

        for i in range(n_ctrl):
            p1 = pts[i]
            p2 = pts[(i + 1) % n_ctrl]

            # 获取端点的 fi/fo 值（None 则用全局 feather 的一半）
            global_fi = shape.feather / 2.0
            global_fo = shape.feather / 2.0
            fi1 = p1.get('fi') if p1.get('fi') is not None else global_fi
            fo1 = p1.get('fo') if p1.get('fo') is not None else global_fo
            fi2 = p2.get('fi') if p2.get('fi') is not None else global_fi
            fo2 = p2.get('fo') if p2.get('fo') is not None else global_fo

            # Bezier 段: pos1 -> c2_1 -> c1_2 -> pos2 (cubic)
            x0, y0 = p1['pos'][0] * w, p1['pos'][1] * h
            x1, y1 = p1['c2'][0] * w, p1['c2'][1] * h
            x2, y2 = p2['c1'][0] * w, p2['c1'][1] * h
            x3, y3 = p2['pos'][0] * w, p2['pos'][1] * h

            for s in range(sub_div):
                t = s / sub_div
                # Cubic bezier
                u = 1 - t
                px = u**3 * x0 + 3 * u**2 * t * x1 + 3 * u * t**2 * x2 + t**3 * x3
                py = u**3 * y0 + 3 * u**2 * t * y1 + 3 * u * t**2 * y2 + t**3 * y3
                # 线性插值 fi/fo
                fi = fi1 + (fi2 - fi1) * t
                fo = fo1 + (fo2 - fo1) * t
                samples.append((px, py, fi * feather_scale, fo * feather_scale))

        if not samples:
            return sharp_mask

        # 4. 构建曲线标记图用于 distanceTransformWithLabels
        # 在曲线上画标记像素（值为0），其余为255
        curve_marker = np.full((h, w), 255, dtype=np.uint8)
        # 同时创建一个坐标到采样索引的映射
        sample_map = {}  # (y, x) -> sample_index

        for idx, (sx, sy, fi_px, fo_px) in enumerate(samples):
            ix, iy = int(round(sx)), int(round(sy))
            if 0 <= ix < w and 0 <= iy < h:
                curve_marker[iy, ix] = 0
                # 多个采样可能映射到同一像素，保留第一个（距离最近的覆盖）
                if (iy, ix) not in sample_map:
                    sample_map[(iy, ix)] = idx

        # 5. 用 distanceTransform 找到每个像素最近的曲线采样点
        dist_result, labels = cv2.distanceTransformWithLabels(
            curve_marker, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL
        )

        sample_arr = np.array(samples, dtype=np.float32)  # (N, 4): px, py, fi_px, fo_px
        default_f = shape.feather / 2.0 * feather_scale

        # P0-COL-05：DIST_LABEL_PIXEL 的 label 是「零像素按行主序扫描的
        # 序号（从 1 起）」，不是线性坐标 (y*w+x)。旧实现按坐标解码，
        # label 几乎永远匹配不到采样点坐标，逐点 fi/fo 退化成全局
        # feather/2。这里改为：零像素表（np.nonzero，行主序，与 OpenCV
        # 编号一致）建立 label→采样点 的 LUT，整图一次向量化查表——
        # 同时替换掉旧实现 O(像素×采样点) 的逐点布尔掩码循环。
        fi_map = np.full((h, w), default_f, dtype=np.float32)
        fo_map = np.full((h, w), default_f, dtype=np.float32)

        zero_ys, zero_xs = np.nonzero(curve_marker == 0)
        max_label = int(labels.max()) if labels.size else 0
        lut_size = max(max_label, len(zero_ys)) + 1
        lut_fi = np.full(lut_size, default_f, dtype=np.float32)
        lut_fo = np.full(lut_size, default_f, dtype=np.float32)
        for k in range(len(zero_ys)):
            sidx = sample_map.get((int(zero_ys[k]), int(zero_xs[k])))
            if sidx is not None:
                lut_fi[k + 1] = sample_arr[sidx, 2]  # label 序号从 1 起
                lut_fo[k + 1] = sample_arr[sidx, 3]

        on_curve = labels >= 1
        fi_map[on_curve] = lut_fi[labels[on_curve]]
        fo_map[on_curve] = lut_fo[labels[on_curve]]
        # 曲线像素自身（label 0）直接取所在坐标的采样点值
        for (iy, ix), sidx in sample_map.items():
            fi_map[iy, ix] = sample_arr[sidx, 2]
            fo_map[iy, ix] = sample_arr[sidx, 3]

        # 7. 用 smoothstep 计算 alpha
        total = fi_map + fo_map
        total = np.maximum(total, 0.001)  # 避免除零
        t = np.clip((sdf + fo_map) / total, 0.0, 1.0)
        alpha = t * t * (3.0 - 2.0 * t)  # Hermite smoothstep

        return alpha

    def _fast_feather_blur(self, arr: np.ndarray, blur_radius: int) -> np.ndarray:
        """Fast feather blur using 3-pass box blur to approximate Gaussian.

        Uses integral-image based box blur (O(1) per pixel per pass) to approximate
        a Gaussian blur. This approach:
        - Has NO edge clipping artifacts (unlike downscale-blur-upscale)
        - Is fast regardless of kernel size (constant time per pixel)
        - Produces results visually identical to true Gaussian blur (<0.5% error)
        - Reference: "Fast Almost-Gaussian Filtering" by Peter Kovesi
        """
        if blur_radius <= 31:
            return cv2.GaussianBlur(arr, (blur_radius, blur_radius), 0)

        # Calculate Gaussian sigma from kernel size (OpenCV's formula)
        sigma = 0.3 * ((blur_radius - 1) * 0.5 - 1) + 0.8

        # Calculate box sizes for 3-pass approximation
        n = 3
        w_ideal = np.sqrt(12.0 * sigma * sigma / n + 1.0)
        wl = int(np.floor(w_ideal))
        if wl % 2 == 0: wl -= 1
        wu = wl + 2
        m = int(round((12.0 * sigma * sigma - n * wl * wl - 4 * n * wl - 3 * n) / (-4.0 * wl - 4.0)))
        m = max(0, min(m, n))
        box_sizes = [wl] * m + [wu] * (n - m)

        result = arr
        for box_size in box_sizes:
            if box_size >= 3:
                result = cv2.blur(result, (box_size, box_size))

        return result

    def _generate_shape_texture(self, shape: MaskShape, target_w: int, target_h: int, img_w: int, pad: int = 0) -> np.ndarray:
        """生成不带旋转的形状纹理（不含opacity/invert/rotation）。

        Args:
            pad: 当需要旋转时，在画布四周添加的额外像素。
                 形状会在更大的画布上绘制（位置偏移pad），这样旋转后
                 形状的羽化边缘不会被画布边界截断。
        """
        canvas_w = target_w + 2 * pad
        canvas_h = target_h + 2 * pad

        # Check for Bitmap type first - it doesn't need QPainter
        if shape.type == MaskShape.BITMAP and shape.bitmap_data is not None:
            shape_arr = cv2.resize(shape.bitmap_data, (canvas_w, canvas_h), interpolation=cv2.INTER_LINEAR)
        elif shape.type == MaskShape.BRUSH:
            shape_arr = self._render_brush_mask_to_canvas(shape, target_w, target_h, img_w)
            if pad > 0:
                tmp = np.zeros((canvas_h, canvas_w), dtype=np.float32)
                tmp[pad:pad+target_h, pad:pad+target_w] = shape_arr
                shape_arr = tmp
        else:
            # For vector shapes, use QPainter (WITHOUT rotation)
            shape_img = QImage(canvas_w, canvas_h, QImage.Format.Format_Grayscale8)
            shape_img.fill(Qt.GlobalColor.black)
            painter = QPainter(shape_img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()

            # Shape position: original coordinates + padding offset
            # Shape size: based on original target_w/target_h (NOT padded canvas)
            if shape.type == "circle":
                rx = shape.size[0] * target_w
                ry = shape.size[1] * target_h
                # Render at canonical center (0.5, 0.5) for cache reuse across positions
                cx = 0.5 * target_w + pad
                cy = 0.5 * target_h + pad
                path.addEllipse(QPointF(cx, cy), rx, ry)
            elif shape.type == "rect":
                sw, sh = shape.size[0] * target_w, shape.size[1] * target_h
                # Render at canonical center (0.5, 0.5) for cache reuse across positions
                cx = 0.5 * target_w + pad
                cy = 0.5 * target_h + pad
                path.addRect(QRectF(cx - sw/2, cy - sh/2, sw, sh))
            elif shape.type == "curve" and shape.points:
                pts = shape.points
                path.moveTo(pts[0]['pos'][0] * target_w + pad, pts[0]['pos'][1] * target_h + pad)
                for i in range(len(pts)):
                    p1 = pts[i]
                    p2 = pts[(i + 1) % len(pts)]
                    path.cubicTo(
                        p1['c2'][0] * target_w + pad, p1['c2'][1] * target_h + pad,
                        p2['c1'][0] * target_w + pad, p2['c1'][1] * target_h + pad,
                        p2['pos'][0] * target_w + pad, p2['pos'][1] * target_h + pad
                    )
            painter.setBrush(QBrush(Qt.GlobalColor.white))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)
            painter.end()

            ptr = shape_img.bits()
            # Grayscale QImage scanlines are padded to 32 bits. Rotation pads
            # frequently make the width non-divisible by four; treating that
            # padding as pixels shears the mask and can crop it away entirely.
            ptr.setsize(shape_img.sizeInBytes())
            shape_arr = (np.frombuffer(ptr, np.uint8)
                         .reshape((canvas_h, shape_img.bytesPerLine()))[:, :canvas_w]
                         .astype(np.float32) / 255.0)

        # Gradient type
        if shape.type == "gradient" and hasattr(shape, 'grad_points'):
            p1 = np.array([shape.grad_points[0][0] * target_w + pad, shape.grad_points[0][1] * target_h + pad])
            p2 = np.array([shape.grad_points[1][0] * target_w + pad, shape.grad_points[1][1] * target_h + pad])
            yy, xx = np.mgrid[:canvas_h, :canvas_w]
            pixels = np.c_[xx.ravel(), yy.ravel()]
            vec = p2 - p1
            vec_len_sq = np.sum(vec**2)
            if vec_len_sq > 1e-6:
                t = np.sum((pixels - p1) * vec, axis=1) / vec_len_sq
                shape_arr = np.clip(t, 0, 1).reshape((canvas_h, canvas_w))
            else:
                shape_arr = np.zeros((canvas_h, canvas_w), dtype=np.float32)

        # NOTE: feather/blur/clean are applied AFTER rotation/translation/crop
        # in _render_single_shape to avoid edge clipping artifacts.
        # This function returns the sharp shape only.
        return shape_arr

