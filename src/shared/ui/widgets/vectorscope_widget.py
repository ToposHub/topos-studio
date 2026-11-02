"""
Vectorscope Widget for Topos Color.
Professional-grade vectorscope for visualizing Hue and Saturation.

Features:
- Graticule with concentric circles (50%, 75% saturation markers)
- 6-color target boxes (R, G, B, C, M, Y) at 75% and 100% saturation
- Skin tone line (I-bar) at approximately 123°
- Fluorescent heatmap trace with density-based brightness
- Rec. 709 standard YCbCr conversion

Visual design inspired by DaVinci Resolve and professional broadcast monitors.
Color positions aligned with Color Wheel (HUE_OFFSET_ANGLE = 105°).
"""
import numpy as np
import cv2
from typing import Optional, Tuple
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QPen, QImage, QFont, QPainterPath
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


# Rec. 709 RGB to YCbCr conversion coefficients
# Y  = 0.299R + 0.587G + 0.114B
# Cb = (B - Y) / 1.772 = -0.169R - 0.331G + 0.500B
# Cr = (R - Y) / 1.402 = 0.500R - 0.419G - 0.081B
REC709_CB_COEFF = np.array([-0.169, -0.331, 0.500], dtype=np.float32)
REC709_CR_COEFF = np.array([0.500, -0.419, -0.081], dtype=np.float32)

# Standard color positions in vectorscope
# Display mapping aligned with Color Wheel (HUE_OFFSET_ANGLE = 105°):
#   x_display = Cb (horizontal)
#   y_display = -Cr (vertical, Qt Y-axis inverted)
# This ensures R is at ~105° (left-upper, 10-11 o'clock position)
# Format: (Cb, Cr) - Rec.709 coordinates
COLOR_TARGETS = {
    'R':  (-0.169,  0.500),   # Red: left-upper
    'Y':  (-0.500,  0.081),   # Yellow: left-upper-left
    'G':  (-0.331, -0.419),   # Green: left-lower
    'C':  ( 0.169, -0.500),   # Cyan: lower-right
    'B':  ( 0.500, -0.081),   # Blue: right-lower
    'M':  ( 0.331,  0.419),   # Magenta: right-upper
}

# Skin tone line angle
# Aligned with Color Wheel (HUE_OFFSET_ANGLE = 105°):
#   Color Wheel: Red=105°, Yellow=165° (screen angles from +X axis, CCW)
#   Skin tones typically have Hue ≈ 10°-25° (orange-red range)
#   Screen angle = Hue + HUE_OFFSET_ANGLE ≈ 115°-130°
# We use 123° which is the standard skin tone reference position
# between Red (105°) and Yellow (165°) on the color wheel.
# Note: In Qt coordinates (Y down), angles are measured CCW from +X axis.
SKIN_TONE_ANGLE = 123.0  # degrees (aligned with color wheel, between R and Y)


class VectorscopeWidget(QWidget):
    """
    Professional vectorscope widget for color analysis.

    Displays the chrominance (Cb vs Cr) distribution of an image,
    allowing colorists to verify color accuracy and skin tone placement.

    Features:
    - Fixed size mode (scope doesn't resize with parent)
    - Zoom level (x1/x2)
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(200, 200)

        # Cached heatmap image
        self._heatmap_image: Optional[QImage] = None
        self._last_update_time: float = 0

        # Throttle timer (max ~15 FPS for vectorscope)
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(66)  # ~15 FPS
        self._update_timer.timeout.connect(self._process_image)

        # Pending image data
        self._pending_image: Optional[np.ndarray] = None
        self._pending_mask: Optional[np.ndarray] = None

        # Cached Cb/Cr data for zoom changes
        self._cached_cb: Optional[np.ndarray] = None
        self._cached_cr: Optional[np.ndarray] = None

        # AUD-38408: 热图静态基底（坐标网格/基底 RGB/密度索引），按 (h, w) 缓存
        self._heatmap_base_cache: Optional[tuple] = None

        # AUD-38406: stable random subsample indices, cached per
        # (total_pixels, sample_count) so consecutive frames sample the
        # same pixels (no trace flicker, no per-frame permutation cost).
        self._sample_indices: Optional[np.ndarray] = None
        self._sample_indices_key: Optional[Tuple[int, int]] = None

        # Zoom level (1 = normal, 2 = 2x zoom)
        self._zoom_level = 1

        # Display options
        self._show_skin_tone_line = True
        self._show_targets = True
        self._graticule_style = 'standard'  # 'standard' or 'simplified'

        self.setStyleSheet(f"background-color: {C.SCOPE_BG};")

    def set_zoom(self, level: int) -> None:
        """Set zoom level (1 or 2)."""
        if level in (1, 2) and level != self._zoom_level:
            self._zoom_level = level
            # Regenerate heatmap from cached data
            if self._cached_cb is not None and self._cached_cr is not None:
                self._generate_heatmap()
            self.update()

    def update_vectorscope(self, image_rgb_float: np.ndarray, mask: Optional[np.ndarray] = None) -> None:
        """
        Update vectorscope with new image data.

        Args:
            image_rgb_float: RGB image as float32 array (0.0-1.0 range), shape (H, W, 3)
            mask: Optional mask array (float32, 0.0-1.0), shape (H, W). 
                  If provided, only masked areas will be displayed.
        """
        if image_rgb_float is None or image_rgb_float.size == 0:
            return

        # Store pending image and mask for throttled update
        self._pending_image = image_rgb_float
        self._pending_mask = mask

        # Trigger throttled update
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _process_image(self) -> None:
        """
        Process the pending image and generate heatmap.
        Called by throttle timer.
        """
        if self._pending_image is None:
            return

        image = self._pending_image
        mask = self._pending_mask
        self._pending_image = None
        self._pending_mask = None

        # Step 1: Sample pixels - use RANDOM sampling to avoid grid artifacts
        h, w = image.shape[:2]
        total_pixels = h * w
        
        # Target sample count for good density without performance hit
        target_samples = min(200000, total_pixels)  # Max 200k samples
        
        if total_pixels <= target_samples:
            # Use all pixels
            sampled = image[:, :, :3].reshape(-1, 3)
            if mask is not None:
                mask_flat = mask.reshape(-1)
                mask_valid = mask_flat > 0.5
                if np.sum(mask_valid) < 10:
                    return
                sampled = sampled[mask_valid]
        else:
            # AUD-38406: 旧实现 np.random.choice(N, k, replace=False) 对全部
            # 像素索引做完整置换（4K ≈ 830 万索引实测 ~349ms/次，直接卡
            # UI 线程），且逐帧重新随机导致迹线闪烁。改为按像素总数缓存
            # 的固定种子索引集：仅图像尺寸变化时生成一次（O(k)），帧间
            # 采样点稳定；密度直方图语义下少量重复采样无影响。
            indices = self._stable_sample_indices(total_pixels, target_samples)

            sampled = image[:, :, :3].reshape(-1, 3)[indices]

            if mask is not None:
                mask_flat = mask.reshape(-1)
                mask_valid = mask_flat[indices] > 0.5
                if np.sum(mask_valid) < 10:
                    return
                sampled = sampled[mask_valid]

        if sampled.shape[0] < 10:
            return

        # Step 2: Convert RGB to CbCr (Rec. 709)
        # AUD-38405: 旧实现 `if sampled.max() > 1.5: /255` + `np.clip(0,1)`
        # 对 HDR 是双重扭曲：真实超白（如 2.4 nit 域）被当 0-255 码值除以
        # 255 压到近黑，随后逐通道 clip(0,1) 又把色度非线性钳制（HDR 红
        # (2.4,1.2,1.2) 被压成 (1,1,1) 塌缩为白色）。
        # 管线契约是 float 0..1(+HDR)：仅当超出 [0,1.5] 的值全部为整数码值
        # （uint8 直接转 float 的特征，且峰值 ≤ 255）才走遗留 /255 路径；
        # 否则视为真实 HDR，保持原值进入下方常色度投影。
        over = sampled[sampled > 1.5]
        if over.size and sampled.max() <= 255.0 \
                and np.all(over == np.round(over)):
            sampled = sampled / 255.0

        # AUD-38405（续）：向量示波器只显示色度、不显示亮度。负值无示波
        # 语义仍截到 0（与旧 clip 下界一致）；超白像素（max 通道 > 1）沿
        # 常色度射线投影到单位立方体表面（除以 max(R,G,B)），色相与相对
        # 色度方向得以保留。np.maximum / 乘法均产生新数组，不会改动上游
        # 渲染缓冲。
        sampled = np.maximum(sampled, 0.0)
        peak = sampled.max(axis=1)
        hdr_mask = peak > 1.0
        if np.any(hdr_mask):
            scale = np.ones_like(peak)
            scale[hdr_mask] = 1.0 / peak[hdr_mask]
            sampled = sampled * scale[:, np.newaxis]

        # Calculate Cb and Cr
        cb = np.dot(sampled, REC709_CB_COEFF)  # Range: -0.5 to 0.5
        cr = np.dot(sampled, REC709_CR_COEFF)  # Range: -0.5 to 0.5

        # Cache for zoom changes
        self._cached_cb = cb
        self._cached_cr = cr

        # Step 3: Compute 2D histogram (density map)
        self._generate_heatmap()

    def _stable_sample_indices(self, total_pixels: int, count: int) -> np.ndarray:
        """按 (像素总数, 采样数) 缓存的固定种子随机采样索引（AUD-38406）。

        - 仅在图像尺寸/采样数变化时重新生成（O(k)，无全量置换）；
        - 固定种子保证同一尺寸下索引集可复现，帧间迹线稳定不闪烁。
        """
        key = (total_pixels, count)
        if self._sample_indices is None or self._sample_indices_key != key:
            rng = np.random.default_rng(0x5EED)
            self._sample_indices = rng.integers(
                0, total_pixels, count, dtype=np.int64)
            self._sample_indices_key = key
        return self._sample_indices

    def _generate_heatmap(self) -> None:
        """Generate heatmap from cached Cb/Cr data."""
        if self._cached_cb is None or self._cached_cr is None:
            return

        cb = self._cached_cb
        cr = self._cached_cr

        # High resolution bins for fine color detail
        bins = 800

        # Map Cb/Cr from [-0.5, 0.5] to [0, bins-1] for histogram
        cb_mapped = ((cb + 0.5) * (bins - 1)).astype(np.int32)
        cr_mapped = ((cr + 0.5) * (bins - 1)).astype(np.int32)

        # Clamp to valid bin indices
        cb_mapped = np.clip(cb_mapped, 0, bins - 1)
        cr_mapped = np.clip(cr_mapped, 0, bins - 1)

        # Use histogram2d for density calculation
        density, x_edges, y_edges = np.histogram2d(
            cb_mapped, cr_mapped,
            bins=bins,
            range=[[0, bins - 1], [0, bins - 1]]
        )

        # No blur - keep sharp, detailed color distribution
        # Apply heatmap coloring
        self._heatmap_image = self._create_heatmap_qimage(density)
        self.update()

    def _create_heatmap_qimage(self, density: np.ndarray) -> QImage:
        """
        Convert density matrix to a colored heatmap QImage.

        Uses logarithmic mapping for high visibility at all density levels.
        Background is black, colors are bright and saturated.

        Args:
            density: 2D numpy array of pixel counts (Cb rows, Cr columns)

        Returns:
            QImage with heatmap visualization
        """
        h, w = density.shape

        # Ensure density is non-negative after Gaussian blur
        density = np.maximum(density, 0)

        # AUD-38408: 坐标网格/基底 RGB/密度索引只依赖 (h, w)（固定 800×800），
        # 预计算并缓存，避免每帧重复 640k 元素的 meshgrid/乘法/clip 链。
        cached = getattr(self, '_heatmap_base_cache', None)
        if cached is None or cached[0] != (h, w):
            # Create display coordinate grids
            cb_coords = (np.arange(w) - w / 2) / (w / 2) * 0.5  # Range [-0.5, 0.5]
            cr_coords = -(np.arange(h) - h / 2) / (h / 2) * 0.5  # Range [-0.5, 0.5]

            cb_grid, cr_grid = np.meshgrid(cb_coords, cr_coords, indexing='xy')

            # Convert Cb/Cr back to RGB (assuming Y=0.5 for mid-luminance)
            # Using Rec.709 inverse formula
            Y = 0.5  # Mid luminance reference
            r_base = Y + 1.402 * cr_grid
            g_base = Y - 0.344136 * cb_grid - 0.714136 * cr_grid
            b_base = Y + 1.772 * cb_grid

            # Clamp to valid RGB range
            r_base = np.clip(r_base, 0, 1)
            g_base = np.clip(g_base, 0, 1)
            b_base = np.clip(b_base, 0, 1)

            # Map display Cb/Cr coordinates back to density array indices
            cb_idx = ((cb_grid + 0.5) * (h - 1)).astype(np.int32)
            cr_idx = ((cr_grid + 0.5) * (w - 1)).astype(np.int32)
            cb_idx = np.clip(cb_idx, 0, h - 1)
            cr_idx = np.clip(cr_idx, 0, w - 1)

            cached = ((h, w), r_base, g_base, b_base, cb_idx, cr_idx,
                      cb_grid, cr_grid)
            self._heatmap_base_cache = cached

        (_, r_base, g_base, b_base, cb_idx, cr_idx,
         cb_grid, cr_grid) = cached

        # Get density values for each display pixel
        pixel_density = density[cb_idx, cr_idx]

        # Logarithmic mapping for high visibility
        # This makes low-density areas visible while preserving high-density detail
        if pixel_density.max() > 0:
            # Normalize and apply log mapping
            pixel_norm = np.log1p(pixel_density) / np.log1p(pixel_density.max())
            # Apply mild gamma for smooth falloff
            pixel_norm = np.power(pixel_norm, 0.8)
        else:
            pixel_norm = np.zeros_like(pixel_density, dtype=np.float32)

        # High brightness gain for very visible colors
        # Base brightness ensures all traced colors are visible
        brightness = np.power(pixel_norm, 0.7) * 2.0  # Up to 200% brightness

        # Strong saturation boost for vivid colors
        sat_boost = 1.5  # 50% saturation increase
        output_r = np.clip(r_base * brightness * sat_boost * 255, 0, 255).astype(np.uint8)
        output_g = np.clip(g_base * brightness * sat_boost * 255, 0, 255).astype(np.uint8)
        output_b = np.clip(b_base * brightness * sat_boost * 255, 0, 255).astype(np.uint8)

        # Alpha: smooth gradient, high opacity for visible areas
        output_a = (np.power(pixel_norm, 0.6) * 255).astype(np.uint8)

        # Mask out areas outside the circular display
        sat_display = np.sqrt(cb_grid**2 + cr_grid**2)
        max_sat = 0.5
        outside_mask = sat_display > max_sat
        output_a[outside_mask] = 0
        output_r[outside_mask] = 0
        output_g[outside_mask] = 0
        output_b[outside_mask] = 0

        # Stack into RGBA array
        heatmap = np.stack([output_r, output_g, output_b, output_a], axis=-1)

        # Ensure contiguous array for QImage
        heatmap = np.ascontiguousarray(heatmap)

        # Create QImage
        q_img = QImage(
            heatmap.data, w, h,
            heatmap.strides[0],
            QImage.Format.Format_RGBA8888
        ).copy()

        return q_img

    def paintEvent(self, event) -> None:
        """Paint the vectorscope with graticule and heatmap."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Enable smooth pixmap transformation for bilinear interpolation
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Calculate center and radius for circular display
        center_x = w / 2
        center_y = h / 2
        radius = min(w, h) / 2 - 20  # Leave margin for labels

        # Background - pure black for additive blending
        painter.fillRect(rect, QColor(0, 0, 0))

        # Draw heatmap if available
        if self._heatmap_image is not None:
            # Scale heatmap to fit the circular display area
            # zoom_factor controls how much of the data range is shown
            # zoom=1: show [-0.5, 0.5] (full range)
            # zoom=2: show [-0.25, 0.25] (center region, 2x magnified)
            zoom_factor = float(self._zoom_level)
            heatmap_rect = QRectF(
                center_x - radius * zoom_factor,
                center_y - radius * zoom_factor,
                2 * radius * zoom_factor,
                2 * radius * zoom_factor
            )
            painter.drawImage(heatmap_rect, self._heatmap_image)

        # Draw graticule (overlay) - UI elements stay fixed size
        self._draw_graticule(painter, center_x, center_y, radius)

        # Draw title
        painter.setPen(QColor(150, 150, 150, 180))
        painter.setFont(QFont("Arial", 9))
        title = I18N.tr("analysis.vectorscope", "Vectorscope")
        painter.drawText(int(w - 80), 15, title)

    def _draw_graticule(self, painter: QPainter, cx: float, cy: float, radius: float) -> None:
        """
        Draw the vectorscope graticule (reference markings).

        UI elements stay fixed size regardless of zoom.
        Circles show standard saturation references:
        - 75% circle (outer)
        - 50% circle (middle)
        - 25% circle (inner)

        Includes:
        - Center cross
        - Concentric circles (saturation markers)
        - 6-color target boxes at 75% and 100%
        - Skin tone line (I-bar)
        """
        # Graticule pen: thin, low contrast
        graticule_color = QColor(80, 80, 80, 150)
        graticule_pen = QPen(graticule_color, 1.0)
        painter.setPen(graticule_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Center cross
        cross_size = 15
        painter.drawLine(int(cx - cross_size), int(cy), int(cx + cross_size), int(cy))
        painter.drawLine(int(cx), int(cy - cross_size), int(cx), int(cy + cross_size))

        # Concentric circles - standard saturation references (fixed positions)
        # 75% saturation circle (outer)
        painter.drawEllipse(QPointF(cx, cy), radius * 0.75, radius * 0.75)

        # 50% saturation circle (middle)
        painter.drawEllipse(QPointF(cx, cy), radius * 0.5, radius * 0.5)

        # 25% saturation circle (inner, very subtle)
        faint_pen = QPen(QColor(60, 60, 60, 80), 0.5)
        painter.setPen(faint_pen)
        painter.drawEllipse(QPointF(cx, cy), radius * 0.25, radius * 0.25)

        # Draw color targets (fixed positions)
        if self._show_targets:
            self._draw_color_targets(painter, cx, cy, radius)

        # Draw skin tone line
        if self._show_skin_tone_line:
            self._draw_skin_tone_line(painter, cx, cy, radius)

    def _draw_color_targets(self, painter: QPainter, cx: float, cy: float, radius: float) -> None:
        """
        Draw 6-color target boxes at their standard positions.

        Positions are fixed regardless of zoom.
        Display mapping: x = Cb, y = -Cr (aligned with Color Wheel)

        Each color has two markers:
        - 75% saturation (smaller box)
        - 100% saturation (larger box)
        """
        # Target box size
        box_75_size = 6
        box_100_size = 8

        for color_name, (cb, cr) in COLOR_TARGETS.items():
            # Convert Cb/Cr to widget coordinates
            # x = Cb, y = -Cr (aligned with Color Wheel)
            # Scale: [-0.5, 0.5] -> [-radius, radius]
            x_100 = cx + cb * radius * 2
            y_100 = cy - cr * radius * 2

            # 75% position (scaled from center)
            x_75 = cx + cb * radius * 2 * 0.75
            y_75 = cy - cr * radius * 2 * 0.75

            # Get color for the target
            target_color = self._get_target_color(color_name)

            # Draw 100% target (outer)
            painter.setPen(QPen(target_color, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(
                int(x_100 - box_100_size/2),
                int(y_100 - box_100_size/2),
                int(box_100_size),
                int(box_100_size)
            )

            # Draw 75% target (inner)
            painter.setPen(QPen(target_color.darker(120), 1.0))
            painter.drawRect(
                int(x_75 - box_75_size/2),
                int(y_75 - box_75_size/2),
                int(box_75_size),
                int(box_75_size)
            )

            # Draw color label
            painter.setFont(QFont("Arial", 7))
            painter.setPen(target_color)
            label_offset = 12
            painter.drawText(
                int(x_100 + label_offset),
                int(y_100 + 4),
                color_name
            )

    def _get_target_color(self, color_name: str) -> QColor:
        """Get the display color for a target marker."""
        colors = {
            'R': QColor(255, 80, 80),
            'G': QColor(80, 255, 80),
            'B': QColor(80, 80, 255),
            'C': QColor(80, 255, 255),
            'M': QColor(255, 80, 255),
            'Y': QColor(255, 255, 80),
        }
        return colors.get(color_name, QColor(150, 150, 150))

    def _draw_skin_tone_line(self, painter: QPainter, cx: float, cy: float, radius: float) -> None:
        """
        Draw the skin tone line (I-bar).

        Thin, subtle line that doesn't interfere with color observation.
        Position is fixed regardless of zoom.
        """
        # Skin tone line direction in our coordinate system
        angle_rad = np.deg2rad(SKIN_TONE_ANGLE)

        # Calculate line endpoints
        # The line extends from center to near the 75% circle
        line_length = radius * 0.75

        x1 = cx
        y1 = cy
        x2 = cx + line_length * np.cos(angle_rad)
        y2 = cy - line_length * np.sin(angle_rad)  # Y inverted in Qt

        # Draw skin tone line - thin and subtle
        skin_color = QColor(255, 180, 120, 150)  # Warm orange, more transparent
        skin_pen = QPen(skin_color, 1.0)  # Thin line (1px instead of 2px)
        painter.setPen(skin_pen)
        painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        # Draw small tick marks along the line - also thinner
        tick_pen = QPen(skin_color.darker(120), 0.5)  # Thinner ticks
        painter.setPen(tick_pen)
        for frac in [0.25, 0.5, 0.75]:
            tick_x = cx + line_length * frac * np.cos(angle_rad)
            tick_y = cy - line_length * frac * np.sin(angle_rad)
            # Small perpendicular tick
            tick_len = 3  # Shorter ticks
            perp_angle = angle_rad + np.pi/2
            painter.drawLine(
                int(tick_x - tick_len * np.cos(perp_angle)),
                int(tick_y + tick_len * np.sin(perp_angle)),
                int(tick_x + tick_len * np.cos(perp_angle)),
                int(tick_y - tick_len * np.sin(perp_angle))
            )

    def resizeEvent(self, event) -> None:
        """Handle resize - reprocess if we have pending data."""
        super().resizeEvent(event)
        if self._pending_image is not None and not self._update_timer.isActive():
            self._update_timer.start()


class WaveformWidget(QWidget):
    """
    RGB Waveform widget (extracted from original ScopePanel).

    Displays the luminance distribution of each RGB channel
    across the horizontal axis of the image.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.q_scope_image: Optional[QImage] = None
        self.setMinimumSize(100, 100)
        # P1-COLOR-06: 标尺模式（percent / nits / code10）与输入传输特性
        self._scale_mode = 'percent'
        self._transfer = 'srgb'
        self.setStyleSheet(f"background-color: {C.SCOPE_BG};")

    def set_scale_mode(self, mode: str) -> None:
        """设置标尺：'percent' | 'nits' | 'code10'（10-bit video range）。"""
        if mode in ('percent', 'nits', 'code10') and mode != self._scale_mode:
            self._scale_mode = mode
            self.update()

    def set_color_state(self, transfer: str = 'srgb') -> None:
        if transfer != self._transfer:
            self._transfer = transfer
            self.update()

    def update_waveform(self, image_bgr_float: np.ndarray) -> None:
        """
        Update waveform with new image data.

        Args:
            image_bgr_float: BGR image as float32 array (0.0-1.0 range)
        """
        if image_bgr_float is None or image_bgr_float.size == 0:
            return

        target_w = 1024
        h, w = image_bgr_float.shape[:2]
        target_h = max(1, min(int(h * (target_w / w)), 512))

        # Use INTER_NEAREST for fast sampling of float32 data
        small_img = cv2.resize(
            image_bgr_float, (target_w, target_h),
            interpolation=cv2.INTER_NEAREST
        )
        if not small_img.flags['C_CONTIGUOUS']:
            small_img = np.ascontiguousarray(small_img)

        # HDR scope range: -10% to 120%
        MIN_V, MAX_V = -0.1, 1.2
        RANGE_V = MAX_V - MIN_V
        BINS = 300

        def get_waveform_channel(chan_float: np.ndarray) -> np.ndarray:
            cols = chan_float.shape[1]
            rows = chan_float.shape[0]

            # Map float data to bin indices (Y inverted)
            normalized = (chan_float - MIN_V) / RANGE_V
            y_indices = np.clip(
                (1.0 - normalized) * (BINS - 1), 0, BINS - 1
            ).astype(np.int32)

            x_indices = np.tile(np.arange(cols), (rows, 1)).flatten()
            flat_indices = y_indices.flatten() * cols + x_indices

            counts = np.bincount(flat_indices, minlength=BINS * cols)
            wf = counts.reshape(BINS, cols).astype(np.float32)

            if wf.max() > 0:
                wf = np.power(wf / wf.max(), 0.5) * 255
            return wf.astype(np.uint8)

        # Note: input is BGR, so channels are [B, G, R]
        b_wf = get_waveform_channel(small_img[:, :, 0])
        g_wf = get_waveform_channel(small_img[:, :, 1])
        r_wf = get_waveform_channel(small_img[:, :, 2])

        scope_data = np.ascontiguousarray(cv2.merge([r_wf, g_wf, b_wf]))
        hh, ww = scope_data.shape[:2]
        self.q_scope_image = QImage(
            scope_data.data, ww, hh,
            scope_data.strides[0],
            QImage.Format.Format_RGB888
        ).copy()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Background
        painter.fillRect(rect, QColor(5, 5, 5))

        if self.q_scope_image is None:
            painter.setPen(QColor(100, 100, 100))
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter,
                I18N.tr("analysis.waiting", "等待图像...")
            )
            return

        # Scale to fit widget
        painter.drawImage(rect, self.q_scope_image)

        # Draw HDR reference lines
        MIN_V, MAX_V = -0.1, 1.2
        RANGE_V = MAX_V - MIN_V

        y_100 = int(h * (MAX_V - 1.0) / RANGE_V)
        y_0 = int(h * (MAX_V - 0.0) / RANGE_V)

        painter.setFont(self.font())

        # 100% line
        painter.setPen(QPen(QColor(100, 200, 255, 180), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, y_100, w, y_100)
        painter.drawText(5, y_100 - 5, I18N.tr("analysis.hundred_percent", "100%"))

        # 0% line
        painter.setPen(QPen(QColor(255, 100, 100, 180), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, y_0, w, y_0)
        painter.drawText(5, y_0 - 5, I18N.tr("analysis.zero_percent", "0%"))

        # P1-COLOR-06: HDR nits / 10-bit code 标尺（0/100% 线保持绘制）
        if self._scale_mode in ('nits', 'code10'):
            marks = waveform_scale_marks(self._scale_mode, self._transfer)
            painter.setPen(QPen(QColor(255, 220, 120, 190), 1,
                                Qt.PenStyle.DotLine))
            for code, label in marks:
                y = int(h * (MAX_V - float(code)) / RANGE_V)
                if 8 <= y <= h - 2:
                    painter.drawLine(w - 74, y, w - 4, y)
                    painter.drawText(w - 70, y + 3, label)
            unit = (I18N.tr("analysis.nits", "nits")
                    if self._scale_mode == 'nits' else
                    I18N.tr("analysis.code10", "10-bit"))
            painter.drawText(w - 74, 14, unit)

        # Title
        painter.setPen(QColor(200, 200, 200, 150))
        painter.drawText(int(w - 100), 15, I18N.tr("analysis.rgb_waveform", "RGB Waveform"))

# ============================================================
# P1-COLOR-06: Professional scope widgets (Parade / Luma / Histogram)
# ============================================================

def _downsample_columns(image_bgr_float: np.ndarray, target_w: int) -> np.ndarray:
    """Nearest-neighbour downsample preserving float precision (shared helper)."""
    h, w = image_bgr_float.shape[:2]
    target_h = max(1, min(int(h * (target_w / max(w, 1))), 512))
    small = cv2.resize(image_bgr_float, (target_w, target_h),
                       interpolation=cv2.INTER_NEAREST)
    if not small.flags['C_CONTIGUOUS']:
        small = np.ascontiguousarray(small)
    return small


def _waveform_density(chan_float: np.ndarray, bins: int = 300) -> np.ndarray:
    """单通道波形密度图（与 WaveformWidget 相同的 HDR -10%..120% 域）。"""
    MIN_V, MAX_V = -0.1, 1.2
    RANGE_V = MAX_V - MIN_V
    rows, cols = chan_float.shape
    normalized = (chan_float - MIN_V) / RANGE_V
    y_indices = np.clip((1.0 - normalized) * (bins - 1), 0, bins - 1).astype(np.int32)
    x_indices = np.tile(np.arange(cols), (rows, 1)).flatten()
    flat_indices = y_indices.flatten() * cols + x_indices
    counts = np.bincount(flat_indices, minlength=bins * cols)
    wf = counts.reshape(bins, cols).astype(np.float32)
    if wf.max() > 0:
        wf = np.power(wf / wf.max(), 0.5) * 255
    return wf.astype(np.uint8)


class ParadeWidget(QWidget):
    """RGB Parade：三通道并排波形（DaVinci Parade）。

    调色员用它判断通道间的电平错位（色偏）与逐通道曝光——
    每通道占 1/3 宽度，垂直域与其他波形一致（-10%..120%）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.q_scope_image: Optional[QImage] = None
        self.setMinimumSize(100, 100)
        self.setStyleSheet(f"background-color: {C.SCOPE_BG};")

    def update_parade(self, image_bgr_float: np.ndarray) -> None:
        if image_bgr_float is None or image_bgr_float.size == 0:
            return
        small = _downsample_columns(image_bgr_float, 768)
        thirds = small.shape[1] // 3
        # 输入 BGR：左→右显示 R, G, B
        r_wf = _waveform_density(small[:, :thirds, 2])
        g_wf = _waveform_density(small[:, thirds:2 * thirds, 1])
        b_wf = _waveform_density(small[:, 2 * thirds:3 * thirds, 0])
        # 每通道单色渲染：R 段红基、G 段绿基、B 段蓝基
        r_rgb = cv2.merge([np.zeros_like(r_wf), np.zeros_like(r_wf), r_wf])
        g_rgb = cv2.merge([np.zeros_like(g_wf), g_wf, np.zeros_like(g_wf)])
        b_rgb = cv2.merge([b_wf, np.zeros_like(b_wf), np.zeros_like(b_wf)])
        scope_data = np.ascontiguousarray(
            np.concatenate([r_rgb, g_rgb, b_rgb], axis=1))
        hh, ww = scope_data.shape[:2]
        self.q_scope_image = QImage(
            scope_data.data, ww, hh, scope_data.strides[0],
            QImage.Format.Format_RGB888).copy()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        w, h = rect.width(), rect.height()
        painter.fillRect(rect, QColor(5, 5, 5))
        if self.q_scope_image is None:
            painter.setPen(QColor(100, 100, 100))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             I18N.tr("analysis.waiting", "等待图像..."))
            return
        painter.drawImage(rect, self.q_scope_image)

        MIN_V, MAX_V = -0.1, 1.2
        RANGE_V = MAX_V - MIN_V
        y_100 = int(h * (MAX_V - 1.0) / RANGE_V)
        y_0 = int(h * (MAX_V - 0.0) / RANGE_V)
        painter.setFont(self.font())
        painter.setPen(QPen(QColor(100, 200, 255, 180), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, y_100, w, y_100)
        painter.drawText(5, y_100 - 5, I18N.tr("analysis.hundred_percent", "100%"))
        painter.setPen(QPen(QColor(255, 100, 100, 180), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, y_0, w, y_0)
        painter.drawText(5, y_0 - 5, I18N.tr("analysis.zero_percent", "0%"))

        # 通道标签（各段中心）
        painter.setPen(QColor(200, 200, 200, 150))
        for label, x in (("R", w / 6.0), ("G", w / 2.0), ("B", 5 * w / 6.0)):
            painter.drawText(int(x - 4), 15, label)
        painter.drawText(int(w - 100), 15,
                         I18N.tr("analysis.rgb_parade", "RGB Parade"))


class LumaWaveformWidget(QWidget):
    """Luma Waveform：仅亮度（BT.709 Y）波形。

    与 RGB Waveform 不同，不显示通道分离——用于纯曝光/对比度判断，
    白色迹线对应 BT.709 加权亮度（0.2126R + 0.7152G + 0.0722B）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.q_scope_image: Optional[QImage] = None
        self.setMinimumSize(100, 100)
        self.setStyleSheet(f"background-color: {C.SCOPE_BG};")

    def update_luma_waveform(self, image_bgr_float: np.ndarray) -> None:
        if image_bgr_float is None or image_bgr_float.size == 0:
            return
        small = _downsample_columns(image_bgr_float, 1024)
        b = small[:, :, 0].astype(np.float32)
        g = small[:, :, 1].astype(np.float32)
        r = small[:, :, 2].astype(np.float32)
        y = 0.2126 * r + 0.7152 * g + 0.0722 * b
        wf = _waveform_density(y)
        scope_data = np.ascontiguousarray(cv2.merge([wf, wf, wf]))
        hh, ww = scope_data.shape[:2]
        self.q_scope_image = QImage(
            scope_data.data, ww, hh, scope_data.strides[0],
            QImage.Format.Format_RGB888).copy()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        w, h = rect.width(), rect.height()
        painter.fillRect(rect, QColor(5, 5, 5))
        if self.q_scope_image is None:
            painter.setPen(QColor(100, 100, 100))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             I18N.tr("analysis.waiting", "等待图像..."))
            return
        painter.drawImage(rect, self.q_scope_image)

        MIN_V, MAX_V = -0.1, 1.2
        RANGE_V = MAX_V - MIN_V
        y_100 = int(h * (MAX_V - 1.0) / RANGE_V)
        y_0 = int(h * (MAX_V - 0.0) / RANGE_V)
        painter.setFont(self.font())
        painter.setPen(QPen(QColor(100, 200, 255, 180), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, y_100, w, y_100)
        painter.drawText(5, y_100 - 5, I18N.tr("analysis.hundred_percent", "100%"))
        painter.setPen(QPen(QColor(255, 100, 100, 180), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, y_0, w, y_0)
        painter.drawText(5, y_0 - 5, I18N.tr("analysis.zero_percent", "0%"))
        painter.setPen(QColor(200, 200, 200, 150))
        painter.drawText(int(w - 110), 15,
                         I18N.tr("analysis.luma_waveform", "Luma Waveform"))


class HistogramWidget(QWidget):
    """RGB Histogram：三通道直方图叠加（对数 y 轴）。

    通道独立归一化后按颜色叠加（重叠区自然混色，全图白峰）。
    对数刻度让暗部/低计数结构可见（DaVinci 默认行为）。
    """

    BINS = 256

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._hist_rgb: Optional[np.ndarray] = None  # (3, BINS) float 归一化
        self.setMinimumSize(100, 100)
        self.setStyleSheet(f"background-color: {C.SCOPE_BG};")

    def update_histogram(self, image_bgr_float: np.ndarray) -> None:
        if image_bgr_float is None or image_bgr_float.size == 0:
            return
        small = _downsample_columns(image_bgr_float, 512)
        hists = []
        for ch in (2, 1, 0):  # R, G, B（输入 BGR）
            # NaN/Inf 防护（AUD-38404，与 CIE 路径 linear_rgb_to_xy 的
            # nan_to_num 对齐）：np.clip 对 NaN 不生效，NaN 经 astype(int32)
            # 变 INT_MIN 会让 np.bincount 抛 ValueError——本方法在 QTimer
            # 槽内被调用，未捕获异常在 PyQt6 下默认 qFatal 终止进程。
            # NaN 无语义 → 0（黑）；±Inf 映射到 0..1 域边界后由 clip 钳到
            # [0, 255]（超白 → 顶 bin，避免默认大数替换引发溢出告警）。
            values = np.nan_to_num(
                small[:, :, ch].astype(np.float32),
                nan=0.0, posinf=1.0, neginf=0.0,
            )
            codes = np.clip(values * 255.0, 0, 255).astype(np.int32)
            counts = np.bincount(codes.flatten(), minlength=self.BINS)
            counts = counts.astype(np.float32)
            # 对数刻度：log(1 + count)，再按通道峰值归一化
            counts = np.log1p(counts)
            peak = counts.max()
            hists.append(counts / peak if peak > 0 else counts)
        self._hist_rgb = np.stack(hists)  # (3, 256) — R, G, B
        self.update()

    def paintEvent(self, event) -> None:
        from PyQt6.QtCore import QRectF as _QRectF

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        w, h = rect.width(), rect.height()
        painter.fillRect(rect, QColor(5, 5, 5))
        if self._hist_rgb is None:
            painter.setPen(QColor(100, 100, 100))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             I18N.tr("analysis.waiting", "等待图像..."))
            return

        # 参考线：0% / 50% / 100%（直方图 x 轴即 code 值 0..255）
        painter.setFont(self.font())
        for frac, label in ((0.0, "0"), (0.5, "50%"), (1.0, "100%")):
            x = int(w * frac)
            painter.setPen(QPen(QColor(80, 80, 80, 140), 1,
                                Qt.PenStyle.DotLine))
            painter.drawLine(x, 0, x, h - 12)

        # 三通道叠加填充（半透明）
        channel_colors = (
            (255, 60, 60, 110),    # R
            (60, 255, 60, 110),    # G
            (60, 120, 255, 110),   # B
        )
        top = h - 14  # 底部留刻度文字
        for ch, color in enumerate(channel_colors):
            hist = self._hist_rgb[ch]
            pts = []
            for i in range(self.BINS):
                x = w * i / (self.BINS - 1)
                y = top - hist[i] * (top - 10)
                pts.append(_QRectF(x - 1.0, y - 1.0, 2.2, 2.2))
            painter.setPen(QColor(*color[:3]))
            for p in pts:
                painter.drawEllipse(p)

        painter.setPen(QColor(200, 200, 200, 150))
        painter.drawText(int(w - 90), 15,
                         I18N.tr("analysis.histogram", "Histogram"))


# ============================================================
# P1-COLOR-06 (part 2): CIE 色度图 / False Color / Zebra /
# HDR nits 标尺 / 10-bit code values。
#
# 传输函数数学与 color_transform_provider 的 SMPTE ST 2084 /
# ARIB STD-B67 实现刻意一致，由 tests/color/test_professional_scopes.py
# 的 parity 用例锁定（两边漂移会被测试抓住）。
# ============================================================

# --- PQ / HLG 常量（SMPTE ST 2084 / ARIB STD-B67 发布值）---
_PQ_M1 = 0.1593017578125
_PQ_M2 = 78.84375
_PQ_C1 = 0.8359375
_PQ_C2 = 18.8515625
_PQ_C3 = 18.6875
_PQ_PEAK_NITS = 10000.0

_HLG_A = 0.17883277
_HLG_B = 0.28466892
_HLG_C = 0.55991073


def pq_code_to_nits(code):
    """PQ 码值 [0,1] → 绝对亮度 nits（ST 2084 EOTF，支持 ndarray）。"""
    v = np.power(np.clip(code, 0.0, 1.0), 1.0 / _PQ_M2)
    num = np.maximum(v - _PQ_C1, 0.0)
    den = np.maximum(_PQ_C2 - _PQ_C3 * v, 1e-10)
    return np.power(num / den, 1.0 / _PQ_M1) * _PQ_PEAK_NITS


def pq_nits_to_code(nits):
    """绝对亮度 nits → PQ 码值 [0,1]（ST 2084 inverse EOTF）。"""
    v = np.power(np.clip(np.asarray(nits, dtype=np.float64), 0.0, _PQ_PEAK_NITS)
                 / _PQ_PEAK_NITS, _PQ_M1)
    ratio = (_PQ_C1 + _PQ_C2 * v) / (1.0 + _PQ_C3 * v)
    return np.power(ratio, _PQ_M2)


def hlg_code_to_scene(code):
    """HLG 码值 [0,1] → 相对场景光（ARIB STD-B67 inverse OETF）。"""
    e = np.clip(code, 0.0, 1.0)
    low = e <= 0.5
    return np.where(
        low, np.square(e) / 3.0,
        (np.exp((e - _HLG_C) / _HLG_A) + _HLG_B) / 12.0,
    )


def hlg_code_to_display_nits(code):
    """HLG 码值 → 监看亮度 nits（BT.2408 1000-nit 监看锚定）。

    采用 BT.2408 公开锚点（0%→0、75%→203 参考白、100%→1000 峰值）
    分段线性插值；HLG 显示亮度依赖监看峰值与 system gamma，
    此表仅用于标尺标注（±20% 近似），不作像素转换。
    """
    anchors = np.array([0.00, 0.25, 0.50, 0.75, 0.90, 1.00])
    nits = np.array([0.0, 1.0, 50.0, 203.0, 640.0, 1000.0])
    return np.interp(np.clip(code, 0.0, 1.0), anchors, nits)


def hlg_nits_to_code(nits):
    """监看亮度 nits → HLG 码值（BT.2408 锚点反查，标尺用途）。"""
    anchors = np.array([0.00, 0.25, 0.50, 0.75, 0.90, 1.00])
    anchor_nits = np.array([0.0, 1.0, 50.0, 203.0, 640.0, 1000.0])
    return np.interp(np.clip(nits, 0.0, 1000.0), anchor_nits, anchors)


def srgb_code_to_linear(code):
    """sRGB 码值 [0,1] → 线性光（IEC 61966-2-1 EOTF）。"""
    c = np.clip(code, 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92,
                    np.power((c + 0.055) / 1.055, 2.4))


def code_value_to_10bit(value):
    """波形值（0=黑，1=标称白）→ 10-bit video range code（64..940）。"""
    return int(round(float(value) * 876.0 + 64.0))


def waveform_scale_marks(scale_mode: str, transfer: str = 'srgb'):
    """波形标尺参考线：返回 [(码值位置, 标签文字), ...]。

    码值位置即波形垂直域的 0..1 值（黑→白）。percent 模式由各
    widget 现有的 0%/100% 专用绘制处理，这里返回空表。
    """
    if scale_mode == 'nits':
        if transfer == 'pq':
            return [(float(pq_nits_to_code(100.0)), "100"),
                    (float(pq_nits_to_code(203.0)), "203"),
                    (float(pq_nits_to_code(1000.0)), "1k"),
                    (float(pq_nits_to_code(4000.0)), "4k"),
                    (1.0, "10k nits")]
        if transfer == 'hlg':
            return [(0.50, "50"),
                    (float(hlg_nits_to_code(203.0)), "203"),
                    (0.90, "640"),
                    (1.0, "1000 nits")]
        return [(1.0, "100 nits")]  # SDR：满刻度即参考白
    if scale_mode == 'code10':
        return [(0.0, str(code_value_to_10bit(0.0))),
                (0.25, str(code_value_to_10bit(0.25))),
                (0.50, str(code_value_to_10bit(0.50))),
                (0.75, str(code_value_to_10bit(0.75))),
                (1.0, str(code_value_to_10bit(1.0)))]
    return []


# --- CIE 1931 色度学数据（2° 标准观察者，10nm 采样 380-700nm）---
CIE_SPECTRAL_LOCUS = [
    (380, 0.1741, 0.0050), (390, 0.1738, 0.0049), (400, 0.1733, 0.0048),
    (410, 0.1726, 0.0048), (420, 0.1714, 0.0051), (430, 0.1689, 0.0069),
    (440, 0.1644, 0.0109), (450, 0.1566, 0.0177), (460, 0.1440, 0.0297),
    (470, 0.1241, 0.0578), (480, 0.0913, 0.1327), (490, 0.0454, 0.2950),
    (500, 0.0082, 0.5384), (510, 0.0139, 0.7502), (520, 0.0743, 0.8338),
    (530, 0.1547, 0.8059), (540, 0.2296, 0.7543), (550, 0.3016, 0.6923),
    (560, 0.3731, 0.6245), (570, 0.4441, 0.5547), (580, 0.5125, 0.4866),
    (590, 0.5752, 0.4242), (600, 0.6270, 0.3725), (610, 0.6658, 0.3340),
    (620, 0.6915, 0.3083), (630, 0.7079, 0.2920), (640, 0.7190, 0.2809),
    (650, 0.7260, 0.2740), (660, 0.7300, 0.2700), (670, 0.7320, 0.2680),
    (680, 0.7334, 0.2666), (690, 0.7344, 0.2656), (700, 0.7347, 0.2653),
]

# 色域三角形顶点（CIE xy）
GAMUT_XY = {
    'bt709':  {'R': (0.640, 0.330), 'G': (0.300, 0.600), 'B': (0.150, 0.060)},
    'p3':     {'R': (0.680, 0.320), 'G': (0.265, 0.690), 'B': (0.150, 0.060)},
    'bt2020': {'R': (0.708, 0.292), 'G': (0.170, 0.797), 'B': (0.131, 0.046)},
}

# 线性 RGB → XYZ（D65 白）矩阵，按色域
RGB_TO_XYZ = {
    'bt709':  ((0.4124, 0.3576, 0.1805),
               (0.2126, 0.7152, 0.0722),
               (0.0193, 0.1192, 0.9505)),
    'p3':     ((0.4866, 0.2657, 0.1982),
               (0.2290, 0.6917, 0.0793),
               (0.0000, 0.0451, 1.0439)),
    'bt2020': ((0.6370, 0.1446, 0.1689),
               (0.2627, 0.6780, 0.0593),
               (0.0000, 0.0281, 1.0610)),
}

D65_XY = (0.3127, 0.3290)


def linear_rgb_to_xy(rgb_linear: np.ndarray, primaries: str = 'bt709') -> np.ndarray:
    """线性 RGB (..., 3) → CIE xy (..., 2)。和为零的像素返回 (-1, -1)。

    xy 与亮度无关，因此 HDR 超白值不会移动色度位置。
    """
    matrix = np.array(RGB_TO_XYZ.get(primaries, RGB_TO_XYZ['bt709']),
                      dtype=np.float32)
    rgb = np.clip(np.nan_to_num(rgb_linear, nan=0.0, posinf=4.0, neginf=0.0),
                  0.0, None)
    xyz = rgb @ matrix.T
    s = xyz.sum(axis=-1)
    valid = s > 1e-9
    xy = np.full(rgb.shape[:-1] + (2,), -1.0, dtype=np.float32)
    xy[valid, 0] = xyz[valid, 0] / s[valid]
    xy[valid, 1] = xyz[valid, 1] / s[valid]
    return xy


class CieChromaticityWidget(QWidget):
    """CIE 1931 xy 色度图。

    光谱马蹄轨迹 + 紫线、BT.709 / P3-D65 / BT.2020 色域三角形、
    D65 白点；图像像素以密度热图叠加。xy 与亮度无关，适合判断
    色域覆盖与 out-of-gamut 溢出方向。
    """

    X_RANGE = (0.0, 0.8)
    Y_RANGE = (0.0, 0.9)
    _BINS_X = 384
    _BINS_Y = 432

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(150, 150)
        self._density: Optional[np.ndarray] = None   # (_BINS_Y, _BINS_X) 0..1
        self._density_image: Optional[QImage] = None
        self._transfer = 'srgb'
        self._primaries = 'bt709'

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(66)  # ~15 FPS
        self._update_timer.timeout.connect(self._process_image)
        self._pending_image: Optional[np.ndarray] = None
        self._pending_mask: Optional[np.ndarray] = None

        self.setStyleSheet(f"background-color: {C.SCOPE_BG};")

    def set_color_state(self, transfer: str = 'srgb', primaries: str = 'bt709') -> None:
        """设置输入图像的传输特性与色域（决定线性化与 XYZ 矩阵）。"""
        if transfer == self._transfer and primaries == self._primaries:
            return
        self._transfer = transfer
        self._primaries = primaries
        if self._pending_image is None and self._density is not None:
            # 无新帧时无法重算，直接丢弃旧密度避免用错误矩阵展示
            self._density = None
            self._density_image = None
            self.update()

    def update_cie(self, image_rgb_float: np.ndarray,
                   mask: Optional[np.ndarray] = None) -> None:
        """RGB float (0..1+, HDR 可超 1) → 节流更新色度图。"""
        if image_rgb_float is None or image_rgb_float.size == 0:
            return
        self._pending_image = image_rgb_float
        self._pending_mask = mask
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _process_image(self) -> None:
        image = self._pending_image
        mask = self._pending_mask
        self._pending_image = None
        self._pending_mask = None
        if image is None or image.shape[2] < 3:
            return
        small = _downsample_columns(image[:, :, ::-1] if image.shape[2] == 3
                                    else np.ascontiguousarray(image[:, :, :3]),
                                    512)
        # 注意：_downsample_columns 输入按 BGR 约定；此处强制构造 BGR
        bgr = small
        rgb = bgr[:, :, ::-1].astype(np.float32)

        # 传输特性 → 线性光
        if self._transfer == 'pq':
            linear = pq_code_to_nits(rgb) / 100.0  # 相对 100-nit 参考白
        elif self._transfer == 'hlg':
            linear = hlg_code_to_scene(rgb)
        elif self._transfer in ('bt709', 'gamma22'):
            linear = np.power(np.clip(rgb, 0.0, None), 2.4)
        elif self._transfer == 'scene' or self._transfer == 'linear':
            linear = rgb
        else:  # srgb
            linear = srgb_code_to_linear(rgb)

        xy = linear_rgb_to_xy(np.ascontiguousarray(linear),
                              self._primaries)

        x0, x1 = self.X_RANGE
        y0, y1 = self.Y_RANGE
        valid = (xy[:, :, 0] >= x0) & (xy[:, :, 0] < x1) & \
                (xy[:, :, 1] >= y0) & (xy[:, :, 1] < y1)
        if mask is not None:
            if mask.shape[:2] != xy.shape[:2]:
                mask = cv2.resize(mask, (xy.shape[1], xy.shape[0]),
                                  interpolation=cv2.INTER_NEAREST)
            valid = valid & (mask > 0.5)

        ix = ((xy[valid, 0] - x0) / (x1 - x0) * self._BINS_X).astype(np.int64)
        iy = ((y1 - xy[valid, 1]) / (y1 - y0) * self._BINS_Y).astype(np.int64)
        np.clip(ix, 0, self._BINS_X - 1, out=ix)
        np.clip(iy, 0, self._BINS_Y - 1, out=iy)
        counts = np.bincount(iy * self._BINS_X + ix,
                             minlength=self._BINS_X * self._BINS_Y)
        density = counts.reshape(self._BINS_Y, self._BINS_X).astype(np.float32)
        peak = density.max()
        if peak > 0:
            density = np.power(density / peak, 0.45)
        self._density = density
        self._density_image = None  # paintEvent 重建
        self.update()

    def _build_density_image(self) -> Optional[QImage]:
        if self._density is None:
            return None
        d = self._density
        # 荧光热图：黑→深蓝→青→白（与 vectorscope 观感一致）
        lut_t = np.array([0.0, 0.25, 0.6, 1.0], dtype=np.float32)
        lut_r = np.array([0, 10, 60, 235], dtype=np.float32)
        lut_g = np.array([0, 60, 220, 240], dtype=np.float32)
        lut_b = np.array([0, 160, 235, 245], dtype=np.float32)
        r = np.interp(d, lut_t, lut_r)
        g = np.interp(d, lut_t, lut_g)
        b = np.interp(d, lut_t, lut_b)
        rgb8 = np.stack([r, g, b], axis=-1).astype(np.uint8)
        rgb8 = np.ascontiguousarray(rgb8)
        h, w = rgb8.shape[:2]
        return QImage(rgb8.data, w, h, rgb8.strides[0],
                      QImage.Format.Format_RGB888).copy()

    def _plot_rect(self) -> Tuple[int, int, int, int]:
        """xy 绘制区（正方形单位比例，居中）。"""
        w, h = self.width(), self.height()
        span_x = self.X_RANGE[1] - self.X_RANGE[0]
        span_y = self.Y_RANGE[1] - self.Y_RANGE[0]
        scale = min(w / span_x, h / span_y)
        pw, ph = int(span_x * scale), int(span_y * scale)
        return ((w - pw) // 2, (h - ph) // 2, pw, ph)

    def _xy_to_px(self, x: float, y: float) -> QPointF:
        ox, oy, pw, ph = self._plot_rect()
        x0, x1 = self.X_RANGE
        y0, y1 = self.Y_RANGE
        px = ox + (x - x0) / (x1 - x0) * pw
        py = oy + (y1 - y) / (y1 - y0) * ph
        return QPointF(px, py)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(5, 5, 5))
        ox, oy, pw, ph = self._plot_rect()
        if pw < 40 or ph < 40:
            return

        if self._density_image is None and self._density is not None:
            self._density_image = self._build_density_image()
        if self._density_image is not None:
            painter.drawImage(
                QRectF(ox, oy, pw, ph), self._density_image,
                QRectF(0.0, 0.0, self._BINS_X, self._BINS_Y))

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 光谱轨迹 + 紫线
        path = QPainterPath()
        start = self._xy_to_px(*CIE_SPECTRAL_LOCUS[0][1:])
        path.moveTo(start)
        for _, lx, ly in CIE_SPECTRAL_LOCUS[1:]:
            path.lineTo(self._xy_to_px(lx, ly))
        path.lineTo(start)  # 紫线（380-700 直接连线）
        painter.setPen(QPen(QColor(120, 200, 160, 200), 1.2))
        painter.drawPath(path)

        # 色域三角形
        styles = (
            ('bt709', QColor(220, 220, 220, 200), Qt.PenStyle.SolidLine, "709"),
            ('p3', QColor(120, 180, 255, 170), Qt.PenStyle.DashLine, "P3"),
            ('bt2020', QColor(255, 170, 80, 170), Qt.PenStyle.DotLine, "2020"),
        )
        painter.setFont(self.font())
        for name, color, style, label in styles:
            pts = GAMUT_XY[name]
            tri = QPainterPath()
            tri.moveTo(self._xy_to_px(*pts['R']))
            tri.lineTo(self._xy_to_px(*pts['G']))
            tri.lineTo(self._xy_to_px(*pts['B']))
            tri.closeSubpath()
            width = 1.6 if name == self._primaries else 1.0
            painter.setPen(QPen(color, width, style))
            painter.drawPath(tri)
            anchor = self._xy_to_px(*pts['G'])
            painter.setPen(color)
            painter.drawText(anchor + QPointF(4, -4), label)

        # D65 白点
        d65 = self._xy_to_px(*D65_XY)
        painter.setPen(QPen(QColor(255, 255, 255, 220), 1.4))
        painter.drawEllipse(d65, 3.0, 3.0)
        painter.drawLine(QPointF(d65.x() - 6, d65.y()),
                         QPointF(d65.x() + 6, d65.y()))
        painter.drawLine(QPointF(d65.x(), d65.y() - 6),
                         QPointF(d65.x(), d65.y() + 6))
        painter.drawText(d65 + QPointF(6, 12), "D65")

        painter.setPen(QColor(200, 200, 200, 150))
        painter.drawText(8, 16, I18N.tr("analysis.cie", "CIE Chromaticity"))


# --- False Color / Zebra（曝光分析伪彩）---

# SDR 曝档表：(下界 IRE, 上界 IRE, RGB)
FALSE_COLOR_SDR_STOPS = (
    (0.00, 0.04, (0, 0, 130)),
    (0.04, 0.10, (45, 0, 140)),
    (0.10, 0.20, (120, 0, 150)),
    (0.20, 0.30, (255, 0, 0)),
    (0.30, 0.40, (255, 96, 0)),
    (0.40, 0.50, (255, 220, 0)),
    (0.50, 0.60, (0, 255, 0)),
    (0.60, 0.70, (0, 200, 120)),
    (0.70, 0.80, (0, 220, 255)),
    (0.80, 0.90, (0, 120, 255)),
    (0.90, 1.00, (255, 0, 255)),
    (1.00, 9.99, (255, 255, 255)),
)

# HDR nits 档表：(下界 nits, 上界 nits, RGB)
FALSE_COLOR_HDR_STOPS = (
    (0.0,    1.0,    (0, 0, 0)),
    (1.0,    50.0,   (0, 0, 139)),
    (50.0,   100.0,  (0, 0, 255)),
    (100.0,  200.0,  (0, 255, 0)),
    (200.0,  400.0,  (0, 200, 150)),
    (400.0,  1000.0, (0, 255, 255)),
    (1000.0, 2000.0, (255, 255, 0)),
    (2000.0, 4000.0, (255, 140, 0)),
    (4000.0, 10000.0, (255, 0, 0)),
    (10000.0, 1e9,   (255, 255, 255)),
)

_FC_LUT_SIZE = 1024


def build_false_color_lut(mode: str = 'sdr', transfer: str = 'srgb') -> np.ndarray:
    """构建 1024 项伪彩 LUT：uint8 (1024, 3)，索引为码值 *1023。

    SDR 模式按 IRE 分档；HDR 模式先把码值经 PQ/HLG 转为 nits 再分档。
    """
    codes = np.linspace(0.0, 1.0, _FC_LUT_SIZE, dtype=np.float64)
    if mode == 'hdr':
        if transfer == 'pq':
            levels = pq_code_to_nits(codes)
        elif transfer == 'hlg':
            levels = hlg_code_to_display_nits(codes)
        else:
            levels = codes * 100.0  # SDR 参考：满刻度=100 nits
        stops = FALSE_COLOR_HDR_STOPS
    else:
        levels = codes
        stops = FALSE_COLOR_SDR_STOPS
    lut = np.zeros((_FC_LUT_SIZE, 3), dtype=np.uint8)
    for lo, hi, rgb in stops:
        sel = (levels >= lo) & (levels < hi)
        lut[sel] = rgb
    # 尾部（最高档含上界）
    lut[levels >= stops[-1][0]] = stops[-1][2]
    return lut


def zebra_overlay_mask(luma: np.ndarray, threshold: float = 1.0,
                       tile: int = 8) -> np.ndarray:
    """超过阈值的像素返回 45° 棋盘条纹 mask（True=条纹处）。"""
    over = luma >= threshold
    if not over.any():
        return np.zeros_like(over)
    rows, cols = luma.shape[:2]
    yy, xx = np.mgrid[0:rows, 0:cols]
    stripes = ((xx // tile + yy // tile) % 2).astype(bool)
    return over & stripes


class FalseColorWidget(QWidget):
    """False Color / Zebra 曝光监看。

    - SDR：IRE 分档伪彩（0..100%+）
    - HDR：nits 分档伪彩（PQ/HLG 码值→nits）
    - Zebra：超过阈值的电平叠加斜纹斑马线（底图为灰度）
    底部带图例色条与档位标签。右键切换模式与阈值。
    """

    MODE_SDR = 'sdr'
    MODE_HDR = 'hdr'
    MODE_ZEBRA = 'zebra'

    # GPU demand 同步信号：模式/阈值改变时 ScopePanel 需要作废在途帧。
    mode_changed = pyqtSignal(str)
    zebra_threshold_changed = pyqtSignal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(150, 100)
        self._display_image: Optional[QImage] = None
        self._mode = self.MODE_SDR
        self._transfer = 'srgb'
        self._zebra_threshold = 1.0
        # AUD-38412: FalseColor LUT 与图例色条按 (mode, transfer) 缓存，
        # 不再每帧重建（LUT 构建含逐档位映射，图例条含 numpy 拼接+QImage 拷贝）。
        self._fc_lut_cache: Optional[Tuple[Tuple[str, str], np.ndarray]] = None
        self._fc_legend_strip: Optional[Tuple[Tuple[str, str], QImage]] = None
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setStyleSheet(f"background-color: {C.SCOPE_BG};")

    def set_color_state(self, transfer: str = 'srgb') -> None:
        if transfer != self._transfer:
            self._transfer = transfer
            # AUD-38412: transfer 变化作废 LUT/图例缓存
            self._fc_lut_cache = None
            self._fc_legend_strip = None
            self.update()

    def set_mode(self, mode: str) -> None:
        if mode in (self.MODE_SDR, self.MODE_HDR, self.MODE_ZEBRA) \
                and mode != self._mode:
            self._mode = mode
            # AUD-38412: mode 变化作废 LUT/图例缓存
            self._fc_lut_cache = None
            self._fc_legend_strip = None
            self.mode_changed.emit(mode)
            self.update()

    def _get_false_color_lut(self) -> np.ndarray:
        """AUD-38412: 取当前 (mode, transfer) 对应的伪彩 LUT，带缓存。"""
        key = (self._mode, self._transfer)
        if self._fc_lut_cache is None or self._fc_lut_cache[0] != key:
            self._fc_lut_cache = (key, build_false_color_lut(self._mode, self._transfer))
        return self._fc_lut_cache[1]

    def set_zebra_threshold(self, threshold: float) -> None:
        threshold = max(0.5, min(1.2, float(threshold)))
        if threshold != self._zebra_threshold:
            self._zebra_threshold = threshold
            self.zebra_threshold_changed.emit(threshold)
        self.update()

    def _show_context_menu(self, pos) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        title = menu.addAction(I18N.tr("analysis.false_color", "False Color"))
        title.setEnabled(False)
        for mode, label in (
            (self.MODE_SDR, I18N.tr("analysis.fc_sdr", "SDR (IRE)")),
            (self.MODE_HDR, I18N.tr("analysis.fc_hdr", "HDR (nits)")),
            (self.MODE_ZEBRA, I18N.tr("analysis.fc_zebra", "Zebra")),
        ):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._mode == mode)
            act.triggered.connect(lambda _=False, m=mode: self.set_mode(m))
        menu.addSeparator()
        thr_title = menu.addAction(
            I18N.tr("analysis.zebra_threshold", "Zebra Threshold"))
        thr_title.setEnabled(False)
        for thr in (0.90, 0.95, 1.00, 1.05, 1.10):
            act = menu.addAction(f"{thr * 100:.0f}%")
            act.setCheckable(True)
            act.setChecked(abs(self._zebra_threshold - thr) < 1e-6)
            act.triggered.connect(lambda _=False, t=thr:
                                   self.set_zebra_threshold(t))
        menu.exec(self.mapToGlobal(pos))

    def update_false_color(self, image_bgr_float: np.ndarray) -> None:
        if image_bgr_float is None or image_bgr_float.size == 0:
            return
        small = _downsample_columns(image_bgr_float, 768)
        b = small[:, :, 0].astype(np.float32)
        g = small[:, :, 1].astype(np.float32)
        r = small[:, :, 2].astype(np.float32)
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b

        if self._mode == self.MODE_ZEBRA:
            gray = (np.clip(luma, 0.0, 1.2) / 1.2 * 255.0).astype(np.uint8)
            rgb8 = np.stack([gray, gray, gray], axis=-1)
            stripes = zebra_overlay_mask(luma, self._zebra_threshold)
            rgb8[stripes] = (255, 255, 255)
        else:
            lut = self._get_false_color_lut()
            idx = np.clip(luma * (_FC_LUT_SIZE - 1), 0,
                          _FC_LUT_SIZE - 1).astype(np.int64)
            rgb8 = lut[idx]

        rgb8 = np.ascontiguousarray(rgb8)
        hh, ww = rgb8.shape[:2]
        self._display_image = QImage(
            rgb8.data, ww, hh, rgb8.strides[0],
            QImage.Format.Format_RGB888).copy()
        self.update()

    def _image_rect(self) -> Tuple[int, int, int, int]:
        w, h = self.width(), self.height()
        legend_h = 34
        avail_w, avail_h = w, max(10, h - legend_h)
        if self._display_image is None:
            return 0, 0, avail_w, avail_h
        iw, ih = self._display_image.width(), self._display_image.height()
        if iw <= 0 or ih <= 0:
            return 0, 0, avail_w, avail_h
        scale = min(avail_w / iw, avail_h / ih)
        dw, dh = int(iw * scale), int(ih * scale)
        return ((w - dw) // 2, (avail_h - dh) // 2, dw, dh)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        w, h = rect.width(), rect.height()
        painter.fillRect(rect, QColor(5, 5, 5))
        painter.setFont(self.font())

        if self._display_image is None:
            painter.setPen(QColor(100, 100, 100))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             I18N.tr("analysis.waiting", "等待图像..."))
            return
        dx, dy, dw, dh = self._image_rect()
        painter.drawImage(QRectF(dx, dy, dw, dh), self._display_image,
                          QRectF(0.0, 0.0, self._display_image.width(),
                                 self._display_image.height()))

        # 底部图例：色条 + 档位标签
        legend_y = h - 30
        legend_w = min(w - 16, 420)
        lx = (w - legend_w) // 2
        if self._mode == self.MODE_ZEBRA:
            painter.setPen(QColor(200, 200, 200, 180))
            painter.drawText(rect.left() + 8, 16,
                             I18N.tr("analysis.fc_zebra", "Zebra"))
            painter.drawText(
                lx, legend_y + 10,
                I18N.tr("analysis.zebra_threshold", "Zebra Threshold")
                + f" ≥ {self._zebra_threshold * 100:.0f}%")
            return

        # AUD-38412: 图例色条按 (mode, transfer) 缓存 QImage，重绘不再
        # 每帧做 numpy 拼接 + QImage 深拷贝。
        strip_key = (self._mode, self._transfer)
        if self._fc_legend_strip is None or self._fc_legend_strip[0] != strip_key:
            lut = self._get_false_color_lut()
            strip = np.repeat(lut[np.newaxis, :, :], 8, axis=0)
            strip = np.ascontiguousarray(strip)
            lh, lw = strip.shape[:2]
            self._fc_legend_strip = (
                strip_key,
                QImage(strip.data, lw, lh, strip.strides[0],
                       QImage.Format.Format_RGB888).copy(),
            )
        strip_img = self._fc_legend_strip[1]
        painter.drawImage(QRect(lx, legend_y, legend_w, 10), strip_img)

        if self._mode == self.MODE_HDR:
            marks = [(pq_nits_to_code(100.0), "100"),
                     (pq_nits_to_code(1000.0), "1k"),
                     (pq_nits_to_code(10000.0), "10k")]
            if self._transfer == 'hlg':
                marks = [(hlg_nits_to_code(203.0), "203"),
                         (hlg_nits_to_code(1000.0), "1000")]
        else:
            marks = [(0.25, "25"), (0.50, "50"), (0.75, "75"), (1.0, "100+")]
        painter.setPen(QColor(190, 190, 190, 200))
        for code, label in marks:
            mx = lx + int(float(code) * legend_w)
            painter.drawLine(mx, legend_y - 3, mx, legend_y + 13)
            painter.drawText(mx - 10, legend_y + 26, label)
        painter.setPen(QColor(200, 200, 200, 150))
        painter.drawText(rect.right() - 120, 16,
                         I18N.tr("analysis.false_color", "False Color"))
