"""
LUT Exporter Module for Topos Color.

Generates 3D LUT files from current color grading parameters.
Supports .cube and .3dl formats with configurable sample points.

Note: Only color operations that are pixel-independent (apply uniformly to all pixels)
can be exported to LUT. Spatial operations (blur, sharpen, masks, qualifiers) are excluded.

Threading boundaries:
- ``LutExporter`` is created and used on the UI thread; it owns a dedicated
  QThread and moves the ``LutExportWorker`` QObject onto it.
- Worker results cross back to the UI thread only via the ``finished`` /
  ``error`` / ``progress`` signals (Qt queued connections).
- AUD-0711：``export_lut`` 经 ``start_requested`` 信号把生成任务投递到
  worker 线程（跨线程信号 = queued invocation），UI 线程立即返回；此前
  直接调用 ``generate_lut_cpu`` 在 UI 线程同步执行，worker 线程空转、
  导出全程冻结界面。
"""

import copy
import logging

import numpy as np
import os
from typing import TYPE_CHECKING, Callable, Optional, Tuple, Dict, Any
from PyQt6.QtCore import QObject, pyqtSignal, QThread, Qt

from src.features.color.core.curves_soft_clip import curves_soft_clip_is_identity

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.features.color.core.layer import Layer

logger = logging.getLogger(__name__)


class LutExportWorker(QObject):
    """Background worker for LUT generation to avoid blocking UI."""
    finished = pyqtSignal(str)  # Output file path
    error = pyqtSignal(str)     # Error message
    progress = pyqtSignal(int)  # Progress percentage (0-100)
    # AUD-0711：跨线程任务投递——UI 线程 emit，worker 线程（事件循环）
    # 以 queued invocation 执行 generate_lut_cpu 槽。
    start_requested = pyqtSignal(list, int, str, str, str)

    def __init__(self) -> None:
        """Create the worker (stateless; all task data passed per call)."""
        super().__init__()

    @staticmethod
    def _has_active_mixer_params(params: Dict) -> bool:
        """Whether any mixer channel has a non-zero (h, s, l) adjustment.

        Args:
            params: ``Layer.mixer_params`` dict ``{'red': [h, s, l], ...}``.

        Returns:
            bool: True if any channel's first three values exceed 1e-6.
        """
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

    @staticmethod
    def _has_active_curves_lut(lut: Any) -> bool:
        """Whether an RGB curves LUT deviates from the linear identity.

        Treats non-(256,·) shapes, non-float input, or any deviation from
        the ramp ``i/255`` (atol 1e-4, after optional /255 normalization)
        as active.

        Args:
            lut: ``Layer.curves_lut`` ndarray of shape (256, 1, 3)/(256, 1)
                or anything.

        Returns:
            bool: True if the curve would change colors.
        """
        if lut is None:
            return False
        try:
            arr = np.asarray(lut, dtype=np.float32)
            if arr.shape[0] != 256:
                return True
            if arr.ndim == 3:
                samples = arr[:, 0, :]
            elif arr.ndim == 2:
                samples = arr[:, :1] if arr.shape[1] == 1 else arr
            elif arr.ndim == 1:
                samples = arr[:, None]
            else:
                return True
            if float(np.max(samples)) > 1.1:
                samples = samples / 255.0
            expected = (np.arange(256, dtype=np.float32) / 255.0)[:, None]
            return not np.allclose(samples, expected, atol=1.0e-4)
        except Exception:
            return True

    @staticmethod
    def _has_active_hsl_curve_lut(lut: Any) -> bool:
        """Whether an HSL curve LUT deviates from its identity defaults.

        Identity is all-ones except row 1 (HueHue) which is zero.

        Args:
            lut: ``Layer.hsl_curve_lut`` ndarray of shape (6, 256) or anything.

        Returns:
            bool: True if the LUT would change colors (parse failure also
                counts as active/conservative True).
        """
        if lut is None:
            return False
        try:
            arr = np.asarray(lut, dtype=np.float32)
            if arr.shape != (6, 256):
                return True
            expected = np.ones((6, 256), dtype=np.float32)
            expected[1, :] = 0.0
            return not np.allclose(arr, expected, atol=1.0e-4)
        except Exception:
            return True

    def generate_lut_cpu(self, layers_data: list, size: int, format_type: str,
                         output_path: str, title: str = "Topos LUT") -> np.ndarray:
        """
        Generate 3D LUT using CPU-based color grading calculation.
        Supports multiple layers with blend modes.

        Args:
            layers_data: List of dicts, each containing:
                - 'params': dict of grading parameters for this layer
                - 'opacity': layer opacity (0.0-1.0)
                - 'blend_mode': blend mode name string
            size: LUT cube size (17, 33, or 65)
            format_type: Output format ('cube' or '3dl')
            output_path: Output file path
            title: LUT title/metadata

        Emits:
            progress: 10-100 during generation.
            finished: with ``output_path`` on success.
            error: with the exception message on failure.
        """
        try:
            self.progress.emit(10)

            # Generate identity cube as starting color
            color = self._generate_identity_cube(size)
            self.progress.emit(20)

            # Apply each visible layer's effects in order
            n_layers = len(layers_data)
            for i, layer_info in enumerate(layers_data):
                layer_params = layer_info.get('params', {})
                opacity = layer_info.get('opacity', 1.0)
                blend_mode = layer_info.get('blend_mode', 'Normal')

                # Apply this layer's grading pipeline
                graded = self._apply_grading_cpu(color.copy(), layer_params)

                # Blend with current color
                blended = self._apply_blend_mode_cpu(color, graded, blend_mode)
                color = color * (1.0 - opacity) + blended * opacity

                self.progress.emit(20 + int(40 * (i + 1) / max(n_layers, 1)))

            # Write to file
            if format_type == 'cube':
                self._write_cube_file(color, size, output_path, title)
            else:
                self._write_3dl_file(color, size, output_path, title)

            self.progress.emit(100)
            self.finished.emit(output_path)

        except Exception as e:
            self.error.emit(str(e))

    def _generate_identity_cube(self, size: int) -> np.ndarray:
        """
        Generate an identity 3D LUT cube.

        Args:
            size: Number of samples per dimension (e.g., 17, 33, 65)

        Returns:
            numpy array of shape (size, size, size, 3) with RGB values in [0, 1]
        """
        # Create coordinate arrays
        r = np.linspace(0, 1, size, dtype=np.float32)
        g = np.linspace(0, 1, size, dtype=np.float32)
        b = np.linspace(0, 1, size, dtype=np.float32)

        # Create 3D meshgrid
        # Shape: (size, size, size) for each channel
        R, G, B = np.meshgrid(r, g, b, indexing='ij')

        # Stack into RGB array: (size, size, size, 3)
        lut = np.stack([R, G, B], axis=-1)

        return lut

    def _load_lut_file(self, path: str) -> Tuple[Optional[np.ndarray], int]:
        """
        Load a .cube or .3dl LUT file into a numpy array.

        P1-COLOR-11 复查：统一走 lut_loader（含 metadata/严格校验/.3dl
        归一化），此处只做 [b,g,r,c] → [r,g,b,c] 的轴序转置。

        Returns:
            Tuple of (lut_array of shape (size, size, size, 3), size) or
            (None, 0) on failure（失败原因已记 warning 日志）。
        """
        # AUD-0713：旧实现 `except Exception: return None, 0` 静默吞掉
        # 一切错误——已应用 LUT 损坏/被移动时导出悄悄丢失该效果且不可
        # 检索。改为区分"文件缺失"与"解析失败"并告警（lut_loader 的
        # 解析错误统一为 ValueError，文件读取为 OSError），不再宽泛吞掉。
        if not os.path.isfile(path):
            logger.warning(
                "LUT export: applied LUT file missing, effect dropped: %s", path)
            return None, 0
        try:
            from src.shared.color.lut_loader import load_lut
            loaded = load_lut(path)
            arr = np.ascontiguousarray(loaded.table, dtype=np.float32)
            arr = arr.transpose(2, 1, 0, 3).copy()  # [b,g,r,c] -> [r,g,b,c]
            return arr, loaded.size
        except (OSError, ValueError) as e:
            logger.warning(
                "LUT export: failed to load applied LUT %s (%s: %s); "
                "effect dropped from export", path, type(e).__name__, e)
            return None, 0

    def _apply_3dlut_cpu(self, color: np.ndarray, lut3d: np.ndarray,
                         lut_size: int, intensity: float) -> np.ndarray:
        """
        Apply a 3D LUT to color data using CPU trilinear interpolation.

        Mirrors the GLSL apply_3dlut function from shader_nodes.py:
        - Clamp to [0,1] for SDR lookup
        - Mix with intensity
        - Scale up for HDR highlights
        - Preserve negative values additively

        Args:
            color: Input colors, last axis RGB.
            lut3d: 3D LUT indexed by [r, g, b], shape (size, size, size, 3).
            lut_size: Samples per dimension of ``lut3d``.
            intensity: Blend factor between original and LUT result (0-1).

        Returns:
            np.ndarray: LUT-mapped colors, same shape as ``color``.
        """
        if intensity == 0.0 or lut3d is None:
            return color

        result = color.copy()
        orig_shape = result.shape
        flat = result.reshape(-1, 3)

        # Separate SDR and negative parts (matches GLSL)
        sdr = np.clip(flat, 0.0, 1.0)
        negative_excess = flat - np.clip(flat, 0.0, 99999.0)

        # Trilinear interpolation
        # Map [0,1] -> [0, size-1] for coordinate lookup
        coords = sdr * (lut_size - 1)  # (N, 3)

        # Integer floor, then clamp to valid range BEFORE fractional parts —
        # 上边界格点（coord == size-1，如输入 1.0）floor 后越界，clip 到
        # size-2 后分数部分必须重算为 1.0（先算 frac 会得 0，把权重全落
        # 在 0 号角点——1.0 输入查成表 [0,0,0]，与 GLSL texture() 的
        # 边缘钳制语义相反）。M05.2 接线暴露：嵌套 LUT 导出的上边界
        # 格点此前取错角。
        idx = np.floor(coords).astype(np.int32)
        idx = np.clip(idx, 0, lut_size - 2)
        frac = coords - idx

        # Gather 8 corner values for trilinear interpolation
        # lut3d shape: (size, size, size, 3) indexed by [r, g, b]
        ir, ig, ib = idx[:, 0], idx[:, 1], idx[:, 2]
        fr, fg, fb = frac[:, 0], frac[:, 1], frac[:, 2]

        c000 = lut3d[ir, ig, ib]
        c100 = lut3d[ir + 1, ig, ib]
        c010 = lut3d[ir, ig + 1, ib]
        c110 = lut3d[ir + 1, ig + 1, ib]
        c001 = lut3d[ir, ig, ib + 1]
        c101 = lut3d[ir + 1, ig, ib + 1]
        c011 = lut3d[ir, ig + 1, ib + 1]
        c111 = lut3d[ir + 1, ig + 1, ib + 1]

        # Trilinear interpolation
        fr3 = fr[:, np.newaxis]
        fg3 = fg[:, np.newaxis]
        fb3 = fb[:, np.newaxis]

        lut_result = (c000 * (1 - fr3) * (1 - fg3) * (1 - fb3) +
                      c100 * fr3 * (1 - fg3) * (1 - fb3) +
                      c010 * (1 - fr3) * fg3 * (1 - fb3) +
                      c110 * fr3 * fg3 * (1 - fb3) +
                      c001 * (1 - fr3) * (1 - fg3) * fb3 +
                      c101 * fr3 * (1 - fg3) * fb3 +
                      c011 * (1 - fr3) * fg3 * fb3 +
                      c111 * fr3 * fg3 * fb3)

        # Mix with intensity (matches GLSL: mix(sdr, lut_result, intensity))
        blended_sdr = sdr * (1.0 - intensity) + lut_result * intensity

        # HDR scale: preserve HDR highlight color ratio (matches GLSL)
        max_orig = np.max(flat, axis=1)
        hdr_mask = max_orig > 1.0
        if np.any(hdr_mask):
            max_sdr = np.max(sdr[hdr_mask], axis=1)
            scale = np.where(max_sdr > 1e-5, max_orig[hdr_mask] / max_sdr, 1.0)
            blended_sdr[hdr_mask] *= scale[:, np.newaxis]

        # Additive offset for negative values (matches GLSL)
        blended_sdr += negative_excess

        return blended_sdr.reshape(orig_shape)

    @staticmethod
    def _apply_blend_mode_cpu(base: np.ndarray, blend: np.ndarray,
                              mode: str) -> np.ndarray:
        """
        Apply blend mode between base and blend layer (matches GLSL apply_blend_mode).

        Supported modes: Normal, Multiply, Screen, Overlay, Soft Light, Hard Light,
        Color Dodge, Color Burn, Darken, Lighten, Difference, Exclusion,
        Hue, Saturation, Color, Luminosity, Addition, Subtract, Divide.
        """
        if mode == "Normal":
            return blend

        if mode == "Multiply":
            return base * blend

        if mode == "Screen":
            return 1.0 - (1.0 - base) * (1.0 - blend)

        if mode == "Overlay":
            def _overlay(a, b):
                return np.where(a <= 0.5, 2 * a * b,
                                1.0 - 2 * (1.0 - a) * (1.0 - b))
            return np.stack([_overlay(base[..., c], blend[..., c]) for c in range(3)], axis=-1)

        if mode == "Soft Light":
            def _soft_light(a, b):
                return np.where(b <= 0.5,
                                a - (1.0 - 2 * b) * a * (1.0 - a),
                                a + (2 * b - 1.0) * (_overlay_d(a) - a))
            def _overlay_d(a):
                return np.where(a <= 0.25,
                                ((16 * a - 12) * a + 4) * a,
                                np.sqrt(a))
            return np.stack([_soft_light(base[..., c], blend[..., c]) for c in range(3)], axis=-1)

        if mode == "Hard Light":
            def _overlay(a, b):
                return np.where(b <= 0.5, 2 * a * b,
                                1.0 - 2 * (1.0 - a) * (1.0 - b))
            return np.stack([_overlay(base[..., c], blend[..., c]) for c in range(3)], axis=-1)

        if mode == "Color Dodge":
            return base / np.maximum(0.001, 1.0 - blend)

        if mode == "Color Burn":
            return 1.0 - (1.0 - base) / np.maximum(0.001, blend)

        if mode == "Darken":
            return np.minimum(base, blend)

        if mode == "Lighten":
            return np.maximum(base, blend)

        if mode == "Difference":
            return np.abs(base - blend)

        if mode == "Exclusion":
            return base + blend - 2.0 * base * blend

        if mode in ("Hue", "Saturation", "Color", "Luminosity"):
            # HSV-based blends - need HSV conversion helpers
            b_hsv = LutExportWorker._rgb_to_hsv_static(base)
            a_hsv = LutExportWorker._rgb_to_hsv_static(blend)
            if mode == "Hue":
                result_hsv = np.stack([a_hsv[..., 0], b_hsv[..., 1], b_hsv[..., 2]], axis=-1)
            elif mode == "Saturation":
                result_hsv = np.stack([b_hsv[..., 0], a_hsv[..., 1], b_hsv[..., 2]], axis=-1)
            elif mode == "Color":
                result_hsv = np.stack([a_hsv[..., 0], a_hsv[..., 1], b_hsv[..., 2]], axis=-1)
            else:  # Luminosity
                result_hsv = np.stack([b_hsv[..., 0], b_hsv[..., 1], a_hsv[..., 2]], axis=-1)
            return LutExportWorker._hsv_to_rgb_static(result_hsv)

        if mode == "Addition":
            return base + blend

        if mode == "Subtract":
            return np.maximum(base - blend, 0.0)

        if mode == "Divide":
            return base / np.maximum(0.001, blend)

        return blend  # fallback: Normal

    @staticmethod
    def _rgb_to_hsv_static(rgb: np.ndarray) -> np.ndarray:
        """Static version of RGB→HSV for use in blend modes."""
        rgb = np.clip(rgb, 0, 1)
        max_c = np.max(rgb, axis=-1)
        min_c = np.min(rgb, axis=-1)
        diff = max_c - min_c
        v = max_c
        s = np.where(max_c > 1e-10, diff / max_c, 0)
        h = np.zeros_like(max_c)
        mask_r = (max_c == rgb[..., 0]) & (diff > 1e-10)
        h[mask_r] = (60 * ((rgb[..., 1][mask_r] - rgb[..., 2][mask_r]) / diff[mask_r]) + 360) % 360
        mask_g = (max_c == rgb[..., 1]) & (diff > 1e-10)
        h[mask_g] = 60 * (2 + (rgb[..., 2][mask_g] - rgb[..., 0][mask_g]) / diff[mask_g])
        mask_b = (max_c == rgb[..., 2]) & (diff > 1e-10)
        h[mask_b] = 60 * (4 + (rgb[..., 0][mask_b] - rgb[..., 1][mask_b]) / diff[mask_b])
        return np.stack([h / 360.0, s, v], axis=-1)

    @staticmethod
    def _hsv_to_rgb_static(hsv: np.ndarray) -> np.ndarray:
        """Static version of HSV→RGB for use in blend modes."""
        h = hsv[..., 0] * 360
        s = hsv[..., 1]
        v = hsv[..., 2]
        c = v * s
        x = c * (1 - np.abs((h / 60) % 2 - 1))
        m = v - c
        rgb = np.zeros_like(hsv)
        mask = (h >= 0) & (h < 60)
        rgb[mask] = np.stack([c[mask], x[mask], np.zeros(mask.sum(), dtype=hsv.dtype)], axis=-1)
        mask = (h >= 60) & (h < 120)
        rgb[mask] = np.stack([x[mask], c[mask], np.zeros(mask.sum(), dtype=hsv.dtype)], axis=-1)
        mask = (h >= 120) & (h < 180)
        rgb[mask] = np.stack([np.zeros(mask.sum(), dtype=hsv.dtype), c[mask], x[mask]], axis=-1)
        mask = (h >= 180) & (h < 240)
        rgb[mask] = np.stack([np.zeros(mask.sum(), dtype=hsv.dtype), x[mask], c[mask]], axis=-1)
        mask = (h >= 240) & (h < 300)
        rgb[mask] = np.stack([x[mask], np.zeros(mask.sum(), dtype=hsv.dtype), c[mask]], axis=-1)
        mask = (h >= 300) & (h < 360)
        rgb[mask] = np.stack([c[mask], np.zeros(mask.sum(), dtype=hsv.dtype), x[mask]], axis=-1)
        rgb += m[..., np.newaxis]
        return rgb

    def _apply_grading_cpu(self, lut: np.ndarray, layer_data: Dict) -> np.ndarray:
        """
        Apply color grading operations to LUT data using CPU.

        Supported operations (pixel-independent) in order:
        1. grading_params: lift, gamma, gain, exposure, temp, tint, contrast,
                          saturation, vibrance, shadow, highlights, hue_shift
        2. curves_lut: RGB curves
        3. hsl_curve_lut: HSL curves (6 modes)
        4. soft_clip_params: Filmic tone mapping
        5. mixer_params: RGB mixer (hue/sat/lum adjustments per color range)
        6. active_lut_path + lut_intensity: Applied 3D LUT

        This order matches the shader pipeline in shader_compiler.py:
        Grading → Curves → HSL Curves → Soft Clip → Mixer → LUT

        Excluded operations (pixel-dependent):
        - warper_params: Grid-based color warping (depends on input color position)
        - mask_shapes: Spatial masks
        - qualifier_params: HSL keyer (depends on original image content)
        - blur/sharpen: Spatial operations
        - film_grain: Random noise
        - mid_detail/sharpness: Local contrast enhancement

        Args:
            lut: Input LUT array (size, size, size, 3)
            layer_data: Dictionary containing grading parameters

        Returns:
            Processed LUT array
        """
        result = lut.copy()

        # 1. Apply basic grading params
        gp = layer_data.get('grading_params', {})
        if gp:
            result = self._apply_basic_grading(result, gp)

        # 2. Apply curves
        curves_lut = layer_data.get('curves_lut')
        if self._has_active_curves_lut(curves_lut):
            result = self._apply_curves_cpu(result, curves_lut)

        # 3. Apply HSL curves (NEW)
        hsl_curve_lut = layer_data.get('hsl_curve_lut')
        if self._has_active_hsl_curve_lut(hsl_curve_lut):
            result = self._apply_hsl_curves_cpu(result, hsl_curve_lut)

        # 4. Apply soft clip (moved before mixer)
        soft_clip = layer_data.get('soft_clip_params', {})
        if soft_clip and soft_clip.get('enabled', False):
            result = self._apply_soft_clip_cpu(result, soft_clip)

        # 4.5 曲线面板四参数柔化裁切（指数软膝；GPU 合成序中在 legacy
        # soft clip 之后）——与 advanced_grade_cpu.apply_curves_soft_clip_image
        # 同一数学
        curves_soft_clip = layer_data.get('curves_soft_clip')
        if curves_soft_clip and not curves_soft_clip_is_identity(curves_soft_clip):
            result = self._apply_curves_soft_clip_cpu(result, curves_soft_clip)

        # 5. Apply RGB mixer
        mixer_params = layer_data.get('mixer_params', {})
        if self._has_active_mixer_params(mixer_params):
            result = self._apply_mixer_cpu(result, mixer_params)

        # 6. Apply existing 3D LUT (if any)
        lut_path = layer_data.get('active_lut_path')
        if lut_path and os.path.isfile(lut_path):
            lut3d, lut_size = self._load_lut_file(lut_path)
            if lut3d is not None:
                lut_intensity = layer_data.get('lut_intensity', 1.0)
                result = self._apply_3dlut_cpu(result, lut3d, lut_size, lut_intensity)

        return result

    def _apply_basic_grading(self, color: np.ndarray, params: Dict) -> np.ndarray:
        """
        Apply basic grading parameters (lift, gamma, gain, exposure, etc.).

        This mirrors the GLSL apply_grading function from shader_nodes.py.
        """
        result = color.copy()

        # Exposure
        exposure = params.get('exposure', 0)
        if exposure != 0:
            exp_stops = (exposure / 100.0) ** 2 * 6.0
            if exposure < 0:
                exp_stops = -exp_stops
            result *= 2 ** exp_stops

        # White Balance (temp, tint)
        temp = params.get('temp', 0)
        tint = params.get('tint', 0)
        wb = np.array([1.0 + temp / 200.0, 1.0 - tint / 200.0, 1.0 - temp / 200.0])
        result *= wb

        # Contrast (pivot around 0.18)
        contrast = params.get('contrast', 0)
        if contrast != 0:
            c_factor = 1.0 + contrast / 100.0
            result = np.sign(result) * np.power(np.abs(result) / 0.18, c_factor) * 0.18

        # Lift / Gamma / Gain
        lift_hsb = params.get('lift', (0, 0, 0))
        gamma_hsb = params.get('gamma', (0, 0, 0))
        gain_hsb = params.get('gain', (0, 0, 0))

        # Convert HSB to RGB adjustments
        lift_rgb = self._hsb_to_rgb_adjustment(lift_hsb)
        gamma_rgb = self._hsb_to_rgb_adjustment(gamma_hsb, is_gamma=True)
        gain_rgb = self._hsb_to_rgb_adjustment(gain_hsb, is_gain=True)

        # Apply lift
        l = (lift_rgb - 1.0) * 0.2
        result = result * gain_rgb * (1.0 - l) + l

        # Apply gamma (HDR & negative safe)
        safe_gamma = np.maximum(gamma_rgb, 0.01)
        gm = 2 ** (-np.log2(safe_gamma))

        # Apply gamma with linear extension for HDR/negative values (vectorized)
        mask_sdr = (result > 0) & (result <= 1)
        mask_hdr = result > 1
        result = np.where(mask_sdr, np.power(np.maximum(result, 1e-10), gm),
                 np.where(mask_hdr, gm * (result - 1.0) + 1.0,
                          gm * result))

        # Shadows (power curve lift)
        shadow = params.get('shadow', 0)
        if shadow != 0:
            shadow_factor = 1.0 + shadow / 100.0
            if shadow_factor > 0:
                clamped = np.clip(result, 0, 1)
                result = np.power(clamped, 1.0 / shadow_factor) + (result - clamped)

        # Highlights (Reinhard compression)
        highlights = params.get('highlights', 0)
        if highlights != 0:
            max_c = np.max(result, axis=-1)
            mask = max_c > 0.1
            if np.any(mask):
                if highlights < 0:
                    k = abs(highlights / 100.0) * 2.0
                    mapped_max = 0.1 + (max_c[mask] - 0.1) / (1.0 + k * (max_c[mask] - 0.1))
                else:
                    k = (highlights / 100.0) * 0.5
                    mapped_max = 0.1 + (max_c[mask] - 0.1) * (1.0 + k)
                result[mask] *= (mapped_max / max_c[mask])[..., np.newaxis]

        # Vibrance
        vibrance = params.get('vibrance', 0)
        if vibrance != 0:
            gray = np.sum(result * [0.299, 0.587, 0.114], axis=-1)
            max_c = np.max(result, axis=-1)
            sat = max_c - gray
            amt = vibrance / 100.0
            boost = amt * (1.0 - np.clip(sat, 0, 1))
            result = result + (result - gray[..., np.newaxis]) * boost[..., np.newaxis]

        # Hue Shift
        hue_shift = params.get('hue_shift', 0)
        if hue_shift != 0:
            hsv = self._rgb_to_hsv(result)
            hsv[..., 0] = (hsv[..., 0] + hue_shift / 360.0) % 1.0
            result = self._hsv_to_rgb(hsv)

        # Saturation
        saturation = params.get('saturation', 0)
        if saturation != 0:
            gray = np.sum(result * [0.299, 0.587, 0.114], axis=-1)
            result = gray[..., np.newaxis] + (result - gray[..., np.newaxis]) * (1.0 + saturation / 100.0)

        return result

    def _hsb_to_rgb_adjustment(self, hsb: Tuple, is_gamma: bool = False,
                               is_gain: bool = False) -> np.ndarray:
        """
        Convert HSB (hue, saturation, brightness) adjustment to RGB multipliers.

        This mirrors the _params_to_rgb function from shader_compiler.py to ensure
        LUT export matches GPU rendering exactly.

        Args:
            hsb: Tuple of (hue, saturation, brightness) in range [-1, 1] or normalized
            is_gamma: True if this is a gamma adjustment (not used, kept for API compatibility)
            is_gain: True if this is a gain adjustment (not used, kept for API compatibility)

        Returns:
            RGB multiplier array [R, G, B]
        """
        import math
        hue, sat, bright = hsb

        # Default: identity
        if hue == 0 and sat == 0 and bright == 0:
            return np.array([1.0, 1.0, 1.0])

        # --- 亮度映射算法 (与 shader_compiler.py _params_to_rgb 完全一致) ---
        # 使用平方非线性映射：在中心区域保持细腻手感，在边缘区域快速扩大范围以实现全黑
        # 映射范围：输入 +/-1.0 对应 +/-6 档曝光 (2^-6 ≈ 0.015)
        stops = (bright ** 2) * 6.0
        if bright < 0:
            stops = -stops
        b_mult = math.pow(2.0, stops)

        if sat == 0:
            return np.array([b_mult, b_mult, b_mult])

        # --- 色调算法 (标准六段色调) ---
        h = hue / 60.0
        i = math.floor(h)
        f = h - i
        p = 1.0 * (1.0 - sat)
        q = 1.0 * (1.0 - sat * f)
        t = 1.0 * (1.0 - sat * (1.0 - f))

        ii = int(i) % 6
        if ii == 0:
            r, g, b = 1.0, t, p
        elif ii == 1:
            r, g, b = q, 1.0, p
        elif ii == 2:
            r, g, b = p, 1.0, t
        elif ii == 3:
            r, g, b = p, q, 1.0
        elif ii == 4:
            r, g, b = t, p, 1.0
        else:
            r, g, b = 1.0, p, q

        # 归一化并应用亮度因子
        avg = max(1e-6, (r + g + b) / 3.0)
        return np.array([(r / avg) * b_mult, (g / avg) * b_mult, (b / avg) * b_mult])

    def _rgb_to_hsv(self, rgb: np.ndarray) -> np.ndarray:
        """Convert RGB to HSV. RGB values in [0, 1].

        AUD-0710：委托 _rgb_to_hsv_static，消除双份维护——实例版与静态
        版逻辑本应一致，实例版漏掉静态版的修复曾导致 hue_shift/mixer
        导出 100% 失败。
        """
        return LutExportWorker._rgb_to_hsv_static(rgb)

    def _hsv_to_rgb(self, hsv: np.ndarray) -> np.ndarray:
        """Convert HSV to RGB. HSV values: h in [0, 1], s,v in [0, 1].

        AUD-0710：委托 _hsv_to_rgb_static——旧实例版在 np.stack 中混用
        数组与标量 0（`np.stack([c[mask], x[mask], 0], ...)`），mask 命中
        即抛 "all input arrays must have the same shape"，mixer 激活或
        hue_shift≠0 时 LUT 导出必然失败；静态版（:670 起）已用
        np.zeros(mask.sum()) 修复。
        """
        return LutExportWorker._hsv_to_rgb_static(hsv)

    def _apply_curves_cpu(self, lut: np.ndarray, curves_lut: np.ndarray) -> np.ndarray:
        """
        Apply RGB curves to LUT data.

        Args:
            lut: LUT array (size, size, size, 3)
            curves_lut: Curves lookup table (256, 1, 3) or (256, 1)

        Returns:
            Processed LUT array
        """
        result = lut.copy()

        # curves_lut shape: (256, 1, 3) for RGB or (256, 1) for master
        if curves_lut.ndim == 2:
            # Master curve only: apply to all channels
            lut_1d = curves_lut[:, 0]
            for c in range(3):
                # Map [0, 1] to curve index
                indices = np.clip(result[..., c] * 255, 0, 255).astype(int)
                result[..., c] = lut_1d[indices]
        elif curves_lut.ndim == 3 and curves_lut.shape[2] == 3:
            # RGB curves: apply per channel
            for c in range(3):
                lut_1d = curves_lut[:, 0, c]
                indices = np.clip(result[..., c] * 255, 0, 255).astype(int)
                result[..., c] = lut_1d[indices]

        return result

    def _apply_hsl_curves_cpu(self, lut: np.ndarray, hsl_curve_lut: np.ndarray) -> np.ndarray:
        """
        Apply HSL curves to LUT data.
        
        Matches the GLSL apply_hsl_curves function from shader_nodes.py.
        
        Args:
            lut: LUT array (size, size, size, 3)
            hsl_curve_lut: HSL curve LUT (6, 256) float32
                Index order: LumSat=0, HueHue=1, HueSat=2, HueLum=3, SatSat=4, SatLum=5
                - LumSat[0]: Saturation multiplier based on Luminance
                - HueHue[1]: Hue shift based on Hue (range -0.5 to +0.5)
                - HueSat[2]: Saturation multiplier based on Hue
                - HueLum[3]: Luminance multiplier based on Hue
                - SatSat[4]: Saturation multiplier based on Saturation
                - SatLum[5]: Luminance multiplier based on Saturation
        
        Returns:
            Processed LUT array
        """
        result = lut.copy()
        
        if hsl_curve_lut is None or hsl_curve_lut.shape[0] != 6:
            return result
        
        orig_shape = result.shape
        flat = result.reshape(-1, 3)
        
        hsv = self._rgb_to_hsv_static(flat)
        h = hsv[..., 0] * 360.0
        s = hsv[..., 1]
        v = hsv[..., 2]
        
        luma = 0.299 * flat[..., 0] + 0.587 * flat[..., 1] + 0.114 * flat[..., 2]
        
        def lookup_curve(idx, domain_val):
            curve = hsl_curve_lut[idx]
            domain_val = np.clip(domain_val, 0.0, 1.0)
            indices = (domain_val * 255.0).astype(np.int32)
            indices = np.clip(indices, 0, 255)
            return curve[indices]
        
        lum_sat_mult = lookup_curve(0, luma)
        hue_shift = lookup_curve(1, hsv[..., 0])
        hue_sat_mult = lookup_curve(2, hsv[..., 0])
        hue_lum_mult = lookup_curve(3, hsv[..., 0])
        sat_sat_mult = lookup_curve(4, s)
        sat_lum_mult = lookup_curve(5, s)
        
        total_sat_mult = lum_sat_mult * hue_sat_mult * sat_sat_mult
        total_lum_mult = hue_lum_mult * sat_lum_mult
        
        new_h = (h + hue_shift * 360.0) % 360.0
        new_s = s * np.clip(total_sat_mult, 0.0, 2.0)
        new_v = v * np.clip(total_lum_mult, 0.0, 2.0)
        
        new_s = np.clip(new_s, 0.0, 1.0)
        new_v = np.clip(new_v, 0.0, 1.0)
        
        new_hsv = np.stack([new_h / 360.0, new_s, new_v], axis=-1)
        result_flat = self._hsv_to_rgb_static(new_hsv)
        
        return result_flat.reshape(orig_shape)

    def _apply_mixer_cpu(self, lut: np.ndarray, mixer_params: Dict) -> np.ndarray:
        """
        Apply RGB mixer (hue/sat/lum adjustments per color range).

        Args:
            lut: LUT array (size, size, size, 3)
            mixer_params: Dictionary with color adjustments for each hue range

        Returns:
            Processed LUT array
        """
        result = lut.copy()

        # Color centers (in degrees)
        centers = [0, 30, 60, 120, 180, 240, 270, 300]  # Red, Orange, Yellow, Green, Aqua, Blue, Purple, Magenta
        param_keys = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']

        # Convert to HSV
        hsv = self._rgb_to_hsv(result)
        h_deg = hsv[..., 0] * 360

        # Accumulate adjustments
        total_hue_shift = np.zeros_like(h_deg)
        sat_mult = np.ones_like(hsv[..., 1])
        lum_mult = np.ones_like(hsv[..., 2])

        for i, key in enumerate(param_keys):
            params = mixer_params.get(key, [0, 0, 0])
            if len(params) < 3:
                continue

            h_adj, s_adj, l_adj = params[0], params[1], params[2]

            # Calculate distance to color center
            dist = np.minimum(np.abs(h_deg - centers[i]), 360 - np.abs(h_deg - centers[i]))

            # Create mask based on distance (30 degree range)
            mask = np.clip(1.0 - dist / 30.0, 0, 1)

            # Accumulate adjustments
            total_hue_shift += h_adj * 0.3 * mask
            sat_mult *= (1.0 + s_adj / 100.0 * mask)
            lum_mult *= (1.0 + l_adj / 100.0 * mask)

        # Apply adjustments
        hsv[..., 0] = (h_deg + total_hue_shift) / 360.0 % 1.0
        hsv[..., 1] *= sat_mult
        hsv[..., 2] *= lum_mult

        # Clamp
        hsv[..., 1] = np.clip(hsv[..., 1], 0, 1)
        hsv[..., 2] = np.clip(hsv[..., 2], 0, 1)

        # Convert back to RGB
        result = self._hsv_to_rgb(hsv)

        return result

    def _apply_curves_soft_clip_cpu(self, lut: np.ndarray, params: Dict) -> np.ndarray:
        """曲线面板四参数柔化裁切（指数软膝）作用于 LUT 数据。

        直接复用 ``advanced_grade_cpu.apply_curves_soft_clip_image``——
        同一实现保证导出 LUT、CPU 成片导出与 GPU 预览三方一致（数组
        按 ``[..., 3]`` 通道约定，任意网格形状通用）。
        """
        from src.features.color.core.advanced_grade_cpu import (
            apply_curves_soft_clip_image,
        )
        return apply_curves_soft_clip_image(lut, params)

    def _apply_soft_clip_cpu(self, lut: np.ndarray, params: Dict) -> np.ndarray:
        """
        Apply soft clip (filmic tone mapping) to LUT data.

        逐项镜像 shader_nodes.py 的 SOFT_CLIP_SNIPPET（面板曲线可视化同源），
        保证导出的 LUT 与 GPU 预览一致：幂律 toe（逐通道）+ 暗部通道平衡 +
        比例缩放 shoulder（保饱和度）。旧的 Reinhard 有理式与 /100 对比度
        映射与 GPU 不符，已在 CP-复查 中废弃。

        Args:
            lut: LUT array (..., 3)
            params: Soft clip parameters dictionary

        Returns:
            Processed LUT array
        """
        result = lut.astype(np.float64, copy=True)

        high = params.get('high', 1.0)
        white_point = params.get('white_point', 1.1)
        shoulder_strength = params.get('shoulder_strength', 1.0)
        low = params.get('low', 0.0)
        black_point = params.get('black_point', -0.1)
        toe_strength = params.get('toe_strength', 1.0)
        exposure = params.get('exposure', 0.0)
        contrast = params.get('contrast', 0.0)

        # 1. Exposure
        if exposure != 0.0:
            result *= 2.0 ** exposure

        # 2. Contrast（pivot 0.18）—— 与 shader 相同的因子映射：
        #    contrast>=0: (1+c)；contrast<0: 1/(1+|c|)。面板范围为 -4..2。
        if contrast != 0.0:
            factor = (1.0 + contrast) if contrast >= 0.0 else (1.0 / (1.0 + abs(contrast)))
            result = np.sign(result) * np.power(np.abs(result) / 0.18, factor) * 0.18

        # 3. Toe（逐通道幂式压缩，负值也压回黑场以上）
        if black_point < low:
            k = max(toe_strength, 0.01)
            rng = low - black_point
            mask_toe = result < low
            if np.any(mask_toe):
                diff = low - result[mask_toe]
                result[mask_toe] = black_point + rng * np.power(
                    1.0 + k * diff / rng, -1.0 / k)
        else:
            # black_point >= low：硬钳到黑点（shader 的 elif 分支）
            result = np.maximum(result, black_point)

        # 4. 暗部通道平衡（防 toe 逐通道压缩导致 RGB 失衡色块）
        toe_min = result.min(axis=-1, keepdims=True)
        toe_max = result.max(axis=-1, keepdims=True)
        mask_bal = (toe_max - toe_min > 0.2) & (toe_min < low)
        if np.any(mask_bal):
            balance = np.clip((toe_max - toe_min) * 0.25, 0.0, 0.15)
            luma = np.expand_dims(
                result[..., 0] * 0.299 + result[..., 1] * 0.587
                + result[..., 2] * 0.114, axis=-1)
            w = balance * np.abs(toe_min)
            result = np.where(mask_bal, result * (1.0 - w) + luma * w, result)

        # 5. Shoulder（按 max 通道比例缩放，死守色彩饱和度）
        max_c = result.max(axis=-1, keepdims=True)
        if white_point > high:
            mask_sh = max_c > high
            if np.any(mask_sh):
                k = max(shoulder_strength, 0.01)
                rng = white_point - high
                diff = max_c[mask_sh] - high
                mapped = white_point - rng * np.power(1.0 + k * diff / rng, -1.0 / k)
                scale = np.ones_like(max_c)
                scale[mask_sh] = mapped / max_c[mask_sh]
                result = result * scale
        else:
            # white_point <= high：超白部分直接缩回白点（shader 的 elif 分支）
            mask_wp = max_c > white_point
            if np.any(mask_wp):
                scale = np.ones_like(max_c)
                scale[mask_wp] = white_point / max_c[mask_wp]
                result = result * scale

        return result

    def _write_cube_file(self, lut: np.ndarray, size: int, path: str, title: str) -> None:
        """
        Write LUT data to .cube format.

        CUBE format specification:
        - Header with title, dimensions, domain
        - Data ordered as R, G, B samples (R outermost, B innermost)
        - Values in floating point [0, 1] or domain range
        """
        with open(path, 'w', encoding='utf-8') as f:
            # Header (Adobe .cube standard keywords only)
            f.write(f"TITLE \"{title}\"\n")
            f.write("# Created by Topos Color\n")
            f.write(f"LUT_3D_SIZE {size}\n")
            f.write("DOMAIN_MIN 0.0 0.0 0.0\n")
            f.write("DOMAIN_MAX 1.0 1.0 1.0\n")
            f.write("\n")

            # Data: .cube spec requires B (outer) -> G (middle) -> R (inner)
            # In numpy: lut[r_index, g_index, b_index, channel]
            for b in range(size):
                for g in range(size):
                    for r in range(size):
                        rgb = lut[r, g, b]
                        # Clamp to valid range
                        rgb = np.clip(rgb, 0.0, 1.0)
                        f.write(f"{rgb[0]:.6f} {rgb[1]:.6f} {rgb[2]:.6f}\n")

    def _write_3dl_file(self, lut: np.ndarray, size: int, path: str, title: str) -> None:
        """
        Write LUT data to .3dl format (Autodesk Lustre format).

        3DL format specification:
        - First non-comment line is the mesh size (``N N N``); metadata only
          in ``#`` comment lines.
        - Data as integer values (typically 10-bit: 0-1023 or 16-bit: 0-65535)
        - Values can be in different bit depths
        """
        # Determine bit depth based on size
        # Common: 10-bit (1024 levels) for 33x33x33, 8-bit (256) for smaller
        if size >= 33:
            bit_depth = 10
            max_val = 1023
        else:
            bit_depth = 8
            max_val = 255

        with open(path, 'w', encoding='utf-8') as f:
            # AUD-0712：旧头写 TITLE/LUT_3D_SIZE/LUT_BIT_DEPTH（.cube 方言
            # 关键字），Autodesk .3dl 首行应是网格尺寸——自家 parse_3dl
            # （lut_loader）对首行按 int 解析，`int('TITLE')` 必抛，导出
            # 产物 round-trip 断裂且第三方工具（Resolve/Nuke）同样读不了。
            # 规范头：首行 `{size} {size} {size}`，元数据仅入 # 注释。
            f.write(f"# TITLE \"{title}\"\n")
            f.write("# Created by Topos Color\n")
            f.write(f"# LUT_BIT_DEPTH {bit_depth}\n")
            f.write(f"{size} {size} {size}\n")

            # Data: .3dl row-major with R varying fastest
            for b in range(size):
                for g in range(size):
                    for r in range(size):
                        rgb = lut[r, g, b]
                        # Convert to integer
                        rgb_int = np.clip(rgb * max_val, 0, max_val).astype(int)
                        f.write(f"{rgb_int[0]} {rgb_int[1]} {rgb_int[2]}\n")


def apply_lut_file_to_color(
    color: np.ndarray, path: str, intensity: float = 1.0,
) -> Optional[np.ndarray]:
    """M05.2 · 3D LUT 文件 CPU 应用（主栈 LUT 的导出/预览消费口）。

    经 ``_load_lut_file``（lut_loader 严格解析 + BGR→RGB 轴序转置）与
    ``_apply_3dlut_cpu``（GLSL apply_3dlut 同式三线性）——与 LUT 导出
    管线同一数学。文件缺失/解析失败返回 ``None``：调用方按各自语义
    决定（成片导出 fail-fast 不静默缺效果；预览可告警跳过），本函数
    不吞交付语义。
    """
    worker = LutExportWorker()
    lut3d, size = worker._load_lut_file(path)
    if lut3d is None:
        return None
    return worker._apply_3dlut_cpu(color, lut3d, size, float(intensity))


class LutExporter:
    """
    Main LUT Exporter class.
    Coordinates LUT generation and file writing.
    """

    # Supported LUT sizes
    SIZES = [17, 33, 65]

    # Supported formats
    FORMATS = ['cube', '3dl']

    # Operations that CAN be exported to LUT (pixel-independent)
    EXPORTABLE_OPERATIONS = [
        'grading_params',      # Basic color adjustments
        'curves_lut',          # RGB curves
        'hsl_curve_lut',       # HSL curves
        'mixer_params',        # RGB mixer
        'soft_clip_params',    # Filmic tone mapping
        'lut_intensity',       # Applied LUT intensity
    ]

    # Operations that CANNOT be exported (pixel-dependent)
    NON_EXPORTABLE_OPERATIONS = [
        'warper_params',       # Grid-based color warping
        'mask_shapes',         # Spatial masks
        'qualifier_params',    # HSL keyer
        'effects_params',      # Blur/sharpen
        'film_grain',          # Random noise
        'mid_detail',          # Local contrast
        'sharpness',           # Edge enhancement
        'plugins',             # Plugin effects
    ]

    def __init__(self) -> None:
        """
        Create the exporter and start its dedicated worker QThread.

        The worker QObject is moved onto ``worker_thread``; call
        cleanup() to shut the thread down when the exporter is discarded.
        """
        self.worker_thread = QThread()
        self.worker_thread.setObjectName("LutExportWorkerThread")
        self.worker = LutExportWorker()
        self.worker.moveToThread(self.worker_thread)
        # AUD-0711：signal → 槽的连接按 emit 线程解析——UI 线程 emit 时
        # receiver（worker）在 worker 线程，Qt 自动走 queued connection，
        # generate_lut_cpu 因此真正在 worker 线程的事件循环里执行。
        self.worker.start_requested.connect(self.worker.generate_lut_cpu)
        self.worker_thread.start()

    def export_lut(self, layers: list, output_path: str,
                   size: int = 33, format_type: str = 'cube',
                   title: str = "Topos LUT",
                   on_finished: Optional[Callable[..., Any]] = None,
                   on_error: Optional[Callable[..., Any]] = None,
                   on_progress: Optional[Callable[..., Any]] = None) -> None:
        """
        Export color grading from all visible layers as a 3D LUT file.

        Args:
            layers: List of Layer objects containing grading parameters.
                    Only visible layers with grading effects are included.
            output_path: Output file path
            size: LUT cube size (17, 33, or 65)
            format_type: Output format ('cube' or '3dl')
            title: LUT title/metadata
            on_finished: Callback for successful export
            on_error: Callback for errors
            on_progress: Callback for progress updates

        Raises:
            ValueError: If ``size``/``format_type`` is unsupported, or no
                visible layer carries exportable grading effects.
        """
        # Validate parameters
        if size not in self.SIZES:
            raise ValueError(f"Invalid LUT size: {size}. Must be one of {self.SIZES}")
        if format_type not in self.FORMATS:
            raise ValueError(f"Invalid format: {format_type}. Must be one of {self.FORMATS}")

        # Build layers data list (skip invisible layers)
        layers_data = []
        for layer in layers:
            if not getattr(layer, 'visible', True):
                continue
            layer_params = self._extract_exportable_params(layer)
            layers_data.append({
                'params': layer_params,
                'opacity': getattr(layer, 'opacity', 1.0),
                'blend_mode': getattr(layer, 'blend_mode', 'Normal'),
            })

        if not layers_data:
            raise ValueError("No visible layers with grading effects found.")

        # Connect signals
        # AUD-0714: UniqueConnection——同一实例重复导出时重复 connect 会
        # 累积连接、回调重复触发（第二次导出触发两份 on_finished）。
        # PyQt6 对重复的 UniqueConnection 抛 TypeError（PyQt5 是返回
        # False）：捕获并忽略，语义即"同回调已连接，保持幂等"。
        for signal, callback in (
            (self.worker.finished, on_finished),
            (self.worker.error, on_error),
            (self.worker.progress, on_progress),
        ):
            if callback is None:
                continue
            try:
                signal.connect(callback, Qt.ConnectionType.UniqueConnection)
            except TypeError:
                logger.debug(
                    "export_lut callback already connected (UniqueConnection); "
                    "skipping duplicate connect")

        # AUD-0711：经 start_requested 投递到 worker 线程（queued）——
        # 不再在 UI 线程直接调用 generate_lut_cpu 同步执行。回调经
        # finished/error/progress 信号回到 UI 线程。
        self.worker.start_requested.emit(layers_data, size, format_type,
                                         output_path, title)

    def _extract_exportable_params(self, layer: 'Layer') -> Dict:
        """
        Extract only the exportable parameters from a layer.

        Returns a dictionary containing only pixel-independent operations.

        AUD-0711 联动（原 AUD-0715）：导出改为真异步后，参数快照与活
        图层跨线程共存——嵌套可变结构（mixer_params 的 [h, s, l] list
        等）必须深拷贝，浅拷贝会让 UI 线程的参数编辑与 worker 线程的
        读取共享同一 list。
        """
        data = {}

        # Grading params
        if hasattr(layer, 'grading_params') and layer.grading_params:
            data['grading_params'] = copy.deepcopy(layer.grading_params)

        # Curves
        if hasattr(layer, 'curves_lut') and LutExportWorker._has_active_curves_lut(layer.curves_lut):
            data['curves_lut'] = layer.curves_lut.copy()

        # HSL curves
        if hasattr(layer, 'hsl_curve_lut') and LutExportWorker._has_active_hsl_curve_lut(layer.hsl_curve_lut):
            data['hsl_curve_lut'] = layer.hsl_curve_lut.copy()

        # Mixer
        if hasattr(layer, 'mixer_params') and LutExportWorker._has_active_mixer_params(layer.mixer_params):
            data['mixer_params'] = copy.deepcopy(layer.mixer_params)

        # Soft clip
        if hasattr(layer, 'soft_clip_params') and layer.soft_clip_params:
            data['soft_clip_params'] = copy.deepcopy(layer.soft_clip_params)

        # 曲线面板侧栏状态（四参数柔化裁切：非恒等才携带；Edit 强度已
        # 折算进 layer.curves_lut，无需单独传递）
        if hasattr(layer, 'curves_soft_clip') and \
                not curves_soft_clip_is_identity(layer.curves_soft_clip):
            data['curves_soft_clip'] = copy.deepcopy(layer.curves_soft_clip)

        # LUT intensity (for applied LUT)
        if hasattr(layer, 'lut_intensity'):
            data['lut_intensity'] = layer.lut_intensity

        # Active LUT path (for applied 3D LUT)
        if hasattr(layer, 'active_lut_path') and layer.active_lut_path:
            data['active_lut_path'] = layer.active_lut_path

        return data

    def get_exportable_operations_description(self) -> str:
        """
        Get a human-readable description of what operations are exportable.
        """
        return """
可导出到 LUT 的调色操作:
• 色轮调整 (Lift, Gamma, Gain, 曝光, 色温, 色调, 对比度, 阴影, 高光, 饱和度, 振动, 色相偏移)
• RGB 曲线
• HSL 曲线
• RGB 混色器
• 软剪裁 (Soft Clip)
• 已应用的 3D LUT

无法导出的操作 (依赖图像内容或空间位置):
• 色彩扭曲器 (Warper)
• 蒙版 (Mask)
• 限定器 (Qualifier)
• 模糊/锐化
• 胶片颗粒
• 中间调细节
• 插件效果
"""

    def cleanup(self) -> None:
        """Clean up worker thread (retry quit, bounded wait, no terminate).

        QThread.terminate 可能在任意点撕裂持锁/初始化中的线程导致堆
        损坏与随机段错误（P0-COLOR-01 要消灭的正是这类崩溃）——即使
        wait 超时也只记录告警，不强杀。quit 在 event loop 尚未启动时
        可能丢失，补一次 quit 再等。
        """
        self.worker_thread.quit()
        if not self.worker_thread.wait(3000):
            self.worker_thread.quit()  # quit-before-start 竞态补偿
            self.worker_thread.wait(3000)
            if not self.worker_thread.isRunning():
                return
            import logging
            logging.getLogger(__name__).warning(
                "LutExport worker thread did not stop within 6s; "
                "leaving it to exit on its own")
