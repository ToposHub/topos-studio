"""
ShaderLibrary - Centralized shader program cache.

Provides compile-once, key-based shader program management with error logging
and hot reload support. Used by both CompositeRenderer and CompositeExportRenderer.

Key = (shader_name, defines_tuple, glsl_version).
Programs are compiled on first access and cached.

Compute shader support via ComputeShaderManager for compute-intensive operations.
"""

import threading
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

from ..core.shaders import (
    _PASSTHROUGH_VERTEX, _PASSTHROUGH_FRAGMENT,
    _PASSTHROUGH_SUB_FRAGMENT,
    _BLEND_FRAGMENT, _BLUR_H_FRAGMENT, _BLUR_V_FRAGMENT,
    # Multi-type blur shaders (Box/Gaussian separable + Bilateral/Motion/Bokeh)
    _BLUR_SEPARABLE_H_FRAGMENT, _BLUR_SEPARABLE_V_FRAGMENT,
    _BLUR_BILATERAL_FRAGMENT, _BLUR_MOTION_FRAGMENT, _BLUR_BOKEH_FRAGMENT,
    _GRADE_FRAGMENT, _TRANSFORM_FRAGMENT, _INVERT_FRAGMENT,
    _PREMULT_FRAGMENT, _UNPREMULT_FRAGMENT,
    _SHUFFLE_FRAGMENT, _COPY_CHANNEL_FRAGMENT,
    _CS_TRANSFORM_FRAGMENT, _REFORMAT_FRAGMENT, _CROP_FRAGMENT,
    _SHARPEN_FRAGMENT, _ERODE_DILATE_FRAGMENT, _CHROMA_KEYER_FRAGMENT,
    _DIFFERENCE_KEYER_FRAGMENT,
    _KEYLIGHT_FRAGMENT,
    _DEFOCUS_FRAGMENT, _GLOW_H_FRAGMENT, _GLOW_V_FRAGMENT,
    _COLOR_CORRECT_FRAGMENT,
    _HUECORRECT_FRAGMENT,
    _LUMA_KEYER_FRAGMENT,
    _MATTE_FRAGMENT,
    _CLAMP_FRAGMENT, _MATTE_CLEANUP_FRAGMENT, _DISSOLVE_FRAGMENT,
    _SET_ALPHA_FRAGMENT,
    _NORMALIZE_FRAGMENT,
    _CORNERPIN_FRAGMENT,
    _REMOVE_CHANNELS_FRAGMENT,
    _DOWNSAMPLE_FRAGMENT, _UPSAMPLE_FRAGMENT,
    _LUT3D_FRAGMENT,
    _DEEP_MERGE_FRAGMENT, _DEPTH_OUTPUT_FRAGMENT,
    # Histogram shaders for percentile normalization
    _HISTOGRAM_DOWNSAMPLE_FRAGMENT,
    # Channel merge shader
    _CHANNEL_MERGE_FRAGMENT,
    # Generator shaders
    _NOISE_FRAGMENT,
    _RAMP_FRAGMENT,
    _READBACK_SRGB_BGR_FRAGMENT,
    _EDGE_DETECT_FRAGMENT,
    _MASK_FRAGMENT,
    _ROTO_FRAGMENT, _COMPARE_FRAGMENT, _LIGHT_WRAP_FRAGMENT,
    _MASK_MERGE_FRAGMENT,
    # Filter shaders
    _DENOISE_FRAGMENT,
    _LENS_DISTORTION_FRAGMENT,
    _MESH_WARP_FRAGMENT,
    _MOTION_BLUR_ACCUM_FRAGMENT,
    # A1 Relight (AOV re-lighting)
    _RELIGHT_FRAGMENT,
    # A2 Cryptomatte Select (ID -> matte)
    _CRYPTOMATTE_SELECT_FRAGMENT,
    # A3 Depth tools (ZBlur / Fog / DepthMatte)
    _ZBLUR_FRAGMENT, _FOG_FRAGMENT, _DEPTH_MATTE_FRAGMENT,
    # B3 God Rays
    _GODRAYS_FRAGMENT,
    # B4 Lens kit
    _CHROMATIC_ABERRATION_FRAGMENT, _VIGNETTE_FRAGMENT,
    _LENS_FLARE_FRAGMENT,
    # CP-002 node plugin effects
    _POSTERIZE_FRAGMENT, _VINTAGE_TONE_FRAGMENT, _FILM_GRAIN_FRAGMENT,
    # LIB1 内容库节点效果（计划 L / L04：C01/C17）
    _LIB1_GRADIENT_MAP_FRAGMENT,
    _LIB1_EDGE_EXTEND_DILATE_FRAGMENT, _LIB1_EDGE_EXTEND_OUTPUT_FRAGMENT,
    # LIB1（L16）：C02/C03/C04 单像素三节点
    _LIB1_RGB_MATRIX_FRAGMENT, _LIB1_DESPILL_FRAGMENT, _LIB1_SOLARIZE_FRAGMENT,
    _LIB1_MOSAIC_FRAGMENT, _LIB1_CRT_FRAGMENT, _LIB1_GLITCH_FRAGMENT,
    _LIB1_KALEIDOSCOPE_FRAGMENT,
    _LIB1_MEDIAN_FRAGMENT, _LIB1_CONVOLUTION_FRAGMENT, _LIB1_HALATION_FRAGMENT,
    _LIB1_HALFTONE_FRAGMENT, _LIB1_HATCHING_FRAGMENT,
    _LIB1_POLAR_FRAGMENT, _LIB1_TILE_ARRAY_FRAGMENT,
    _LIB1_VECTOR_DISPLACE_FRAGMENT, _LIB1_STMAP_FRAGMENT,
    _LIB1_FREQ_BLUR_FRAGMENT, _LIB1_FREQ_HIGH_FRAGMENT,
    # CP-044/045 universal mask gating + mix blend
    _GENERIC_MASK_MIX_FRAGMENT,
    # CP-011 Keymix
    _KEYMIX_FRAGMENT,
    # CP-019 DeGrain combine pass
    _DEGRAIN_COMBINE_FRAGMENT,
    # CP-014 ColorLookup
    _COLORLOOKUP_FRAGMENT,
    # CP-015 ASC CDL
    _CDL_FRAGMENT,
    # Topos Color shader
    _TOPOS_COLOR_FRAGMENT,
    # 3D Mesh shaders (Scene3D node)
    _MESH_VERTEX, _MESH_BASIC_FRAGMENT, _MESH_PBR_FRAGMENT,
)
# Note: _FUSED_FRAGMENT and _FUSED_COLOR_FRAGMENT are imported lazily in _ensure_fused_source()


class ShaderLibrary:
    """Centralized, cached shader program management.

    One instance per moderngl.Context. Programs are keyed by
    (shader_name, defines_tuple, glsl_version) and compiled on first access.

    Thread-safe via RLock for concurrent access from render and export threads.
    """

    STANDARD_NAMES = [
        "passthrough", "blend", "blur_h", "blur_v", "grade", "transform",
        "invert", "premult", "unpremult", "shuffle", "copy_channel",
        "cs_transform", "reformat", "crop", "fused", "fused_color",
        "clamp", "matte_cleanup", "dissolve", "set_alpha", "luma_keyer", "matte", "normalize",
        "sharpen", "erode_dilate", "chroma_keyer", "difference_keyer", "defocus",
        "glow_h", "glow_v", "color_correct", "huecorrect", "cornerpin",
        "remove_channels",
        "keylight",  # Keylight 1.2-style chroma keyer
        "downsample", "upsample",  # LOD shaders for viewport-based optimization
        "lut3d",  # LUT node shader
        "deep_merge", "depth_output",  # Deep compositing shaders
        "histogram_downsample",  # Histogram shader for percentile normalization
        "channel_merge",  # Channel merge shader for combining RGBA inputs
        "noise", "ramp", "edge_detect", "mask", "compare", "light_wrap",
        # AUD-6111: these were registered in _SHADER_SOURCES but missing
        # from STANDARD_NAMES, so utility_shaders never received them and
        # the Roto / MaskMerge evaluators plus motion-blur accumulation
        # silently ran their degraded passthrough paths.
        "roto",  # Roto Bezier matte (filter_evaluator _apply_roto)
        "mask_merge",  # Matte boolean combine (filter_evaluator _apply_mask_merge)
        "motion_blur_accum",  # Multi-sample motion blur accumulation (base_evaluator)
        "lens_distortion",  # Generator/filter shaders (A4 lens distortion)
        "mesh_warp",  # Grid/mesh warp shader (C7)
        "denoise",  # Bilateral filter denoise shader
        "relight",  # A1 AOV re-lighting (Lambert MVP)
        "cryptomatte_select",  # A2 ID -> matte
        "zblur", "fog", "depth_matte",  # A3 depth tools
        "godrays",  # B3 volumetric light shafts
        "chromatic_aberration", "vignette", "lens_flare",  # B4
        "posterize", "vintage_tone", "film_grain",  # CP-002 node plugin effects
        "generic_mask_mix",  # CP-044/045 universal mask gating + mix
        "keymix",  # CP-011 mask-driven A/B blend
        "degrain_combine",  # CP-019 grain-gated smoothing combine
        "colorlookup",  # CP-014 curve lookup
        "cdl",  # CP-015 ASC CDL
        "topos_color",  # Topos Color Stack shader
        # Multi-type blur shaders (Box/Gaussian separable + Bilateral/Motion/Bokeh)
        "blur_separable_h", "blur_separable_v",
        "blur_bilateral", "blur_motion", "blur_bokeh",
        "mesh_basic",  # 3D Scene3D node shader (uses its own vertex shader)
        "mesh_pbr",  # 3D Scene3D default PBR program (Cook-Torrance + IBL)
        "readback_srgb_bgr",  # Preview readback: linear->sRGB + BGR + flip
        "passthrough_sub",  # P0-1 GPU tile stitch: sub-rect UV sampling
        # LIB1 内容库节点效果（计划 L / L04）
        "lib1_gradient_map",  # C01 多停靠亮度映射（F16 共享核）
        "lib1_rgb_matrix",  # C02 3x3 通道矩阵（L16）
        "lib1_despill",  # C03 绿幕去溢色（L16）
        "lib1_solarize",  # C04 太阳化折返（F17 共享核，L16）
        "lib1_mosaic",  # C05 块采样马赛克（L16 批2）
        "lib1_crt",  # C08 CRT 扫描线/荫罩（L16 批2）
        "lib1_glitch",  # C09 条带故障（L16 批2）
        "lib1_kaleidoscope",  # C10 万花筒折叠（L16 批2）
        "lib1_median",  # C15 小核中值（L16 批3）
        "lib1_convolution",  # C16 自定义卷积（L16 批3）
        "lib1_halation",  # C20 高光红晕（L16 批3）
        "lib1_halftone",  # C06 套印网屏（L16 批3b）
        "lib1_hatching",  # C07 周期排线（L16 批3b）
        "lib1_polar_coordinates",  # C11 极坐标（L17）
        "lib1_tile_array",  # C14 瓦片阵列（L17）
        "lib1_vector_displace",  # C12 向量位移（多输入，L17 批3）
        "lib1_stmap",  # C13 绝对 UV 重采样（多输入，L17 批3）
        "lib1_freq_blur",  # C21 频率分离低通（多输出，L17 批3）
        "lib1_freq_high",  # C21 high=rgb−low（多输出，L17 批3）
        "lib1_edge_extend_dilate",  # C17 一轮扩张 pass（乒乓）
        "lib1_edge_extend_output",  # C17 输出组装（原 alpha 恢复）
    ]

    # Shader name → (vertex_source, fragment_source)
    _SHADER_SOURCES: Dict[str, Tuple[str, str]] = {
        "passthrough":   (_PASSTHROUGH_VERTEX, _PASSTHROUGH_FRAGMENT),
        "passthrough_sub": (_PASSTHROUGH_VERTEX, _PASSTHROUGH_SUB_FRAGMENT),
        "readback_srgb_bgr": (_PASSTHROUGH_VERTEX, _READBACK_SRGB_BGR_FRAGMENT),
        "blend":         (_PASSTHROUGH_VERTEX, _BLEND_FRAGMENT),
        "blur_h":        (_PASSTHROUGH_VERTEX, _BLUR_H_FRAGMENT),
        "blur_v":        (_PASSTHROUGH_VERTEX, _BLUR_V_FRAGMENT),
        # Multi-type blur shaders
        "blur_separable_h": (_PASSTHROUGH_VERTEX, _BLUR_SEPARABLE_H_FRAGMENT),
        "blur_separable_v": (_PASSTHROUGH_VERTEX, _BLUR_SEPARABLE_V_FRAGMENT),
        "blur_bilateral":   (_PASSTHROUGH_VERTEX, _BLUR_BILATERAL_FRAGMENT),
        "blur_motion":      (_PASSTHROUGH_VERTEX, _BLUR_MOTION_FRAGMENT),
        "blur_bokeh":       (_PASSTHROUGH_VERTEX, _BLUR_BOKEH_FRAGMENT),
        "grade":         (_PASSTHROUGH_VERTEX, _GRADE_FRAGMENT),
        "transform":     (_PASSTHROUGH_VERTEX, _TRANSFORM_FRAGMENT),
        "invert":        (_PASSTHROUGH_VERTEX, _INVERT_FRAGMENT),
        "premult":       (_PASSTHROUGH_VERTEX, _PREMULT_FRAGMENT),
        "unpremult":     (_PASSTHROUGH_VERTEX, _UNPREMULT_FRAGMENT),
        "shuffle":       (_PASSTHROUGH_VERTEX, _SHUFFLE_FRAGMENT),
        "copy_channel":  (_PASSTHROUGH_VERTEX, _COPY_CHANNEL_FRAGMENT),
        "cs_transform":  (_PASSTHROUGH_VERTEX, _CS_TRANSFORM_FRAGMENT),
        "reformat":      (_PASSTHROUGH_VERTEX, _REFORMAT_FRAGMENT),
        "crop":          (_PASSTHROUGH_VERTEX, _CROP_FRAGMENT),
        "sharpen":       (_PASSTHROUGH_VERTEX, _SHARPEN_FRAGMENT),
        "erode_dilate":  (_PASSTHROUGH_VERTEX, _ERODE_DILATE_FRAGMENT),
        "chroma_keyer":  (_PASSTHROUGH_VERTEX, _CHROMA_KEYER_FRAGMENT),
        "difference_keyer": (_PASSTHROUGH_VERTEX, _DIFFERENCE_KEYER_FRAGMENT),
        "keylight":      (_PASSTHROUGH_VERTEX, _KEYLIGHT_FRAGMENT),
        "defocus":       (_PASSTHROUGH_VERTEX, _DEFOCUS_FRAGMENT),
        "glow_h":        (_PASSTHROUGH_VERTEX, _GLOW_H_FRAGMENT),
        "glow_v":        (_PASSTHROUGH_VERTEX, _GLOW_V_FRAGMENT),
        "color_correct": (_PASSTHROUGH_VERTEX, _COLOR_CORRECT_FRAGMENT),
        "huecorrect":    (_PASSTHROUGH_VERTEX, _HUECORRECT_FRAGMENT),
        "luma_keyer":    (_PASSTHROUGH_VERTEX, _LUMA_KEYER_FRAGMENT),
        "matte":         (_PASSTHROUGH_VERTEX, _MATTE_FRAGMENT),
        "clamp":         (_PASSTHROUGH_VERTEX, _CLAMP_FRAGMENT),
        "matte_cleanup": (_PASSTHROUGH_VERTEX, _MATTE_CLEANUP_FRAGMENT),
        "dissolve":      (_PASSTHROUGH_VERTEX, _DISSOLVE_FRAGMENT),
        "set_alpha":     (_PASSTHROUGH_VERTEX, _SET_ALPHA_FRAGMENT),
        "normalize":     (_PASSTHROUGH_VERTEX, _NORMALIZE_FRAGMENT),
        "cornerpin":    (_PASSTHROUGH_VERTEX, _CORNERPIN_FRAGMENT),
        "remove_channels": (_PASSTHROUGH_VERTEX, _REMOVE_CHANNELS_FRAGMENT),
        "downsample": (_PASSTHROUGH_VERTEX, _DOWNSAMPLE_FRAGMENT),
        "upsample": (_PASSTHROUGH_VERTEX, _UPSAMPLE_FRAGMENT),
        "lut3d": (_PASSTHROUGH_VERTEX, _LUT3D_FRAGMENT),
        "deep_merge": (_PASSTHROUGH_VERTEX, _DEEP_MERGE_FRAGMENT),
        "depth_output": (_PASSTHROUGH_VERTEX, _DEPTH_OUTPUT_FRAGMENT),
        # Histogram shaders for percentile normalization
        "histogram_downsample": (_PASSTHROUGH_VERTEX, _HISTOGRAM_DOWNSAMPLE_FRAGMENT),
        # Channel merge shader
        "channel_merge": (_PASSTHROUGH_VERTEX, _CHANNEL_MERGE_FRAGMENT),
        # Generator/filter shaders
        "noise": (_PASSTHROUGH_VERTEX, _NOISE_FRAGMENT),
        "ramp": (_PASSTHROUGH_VERTEX, _RAMP_FRAGMENT),
        "edge_detect": (_PASSTHROUGH_VERTEX, _EDGE_DETECT_FRAGMENT),
        "lens_distortion": (_PASSTHROUGH_VERTEX, _LENS_DISTORTION_FRAGMENT),
        "mesh_warp": (_PASSTHROUGH_VERTEX, _MESH_WARP_FRAGMENT),
        "motion_blur_accum": (_PASSTHROUGH_VERTEX, _MOTION_BLUR_ACCUM_FRAGMENT),
        "mask": (_PASSTHROUGH_VERTEX, _MASK_FRAGMENT),
        "roto": (_PASSTHROUGH_VERTEX, _ROTO_FRAGMENT),
        "compare": (_PASSTHROUGH_VERTEX, _COMPARE_FRAGMENT),
        "light_wrap": (_PASSTHROUGH_VERTEX, _LIGHT_WRAP_FRAGMENT),
        "mask_merge": (_PASSTHROUGH_VERTEX, _MASK_MERGE_FRAGMENT),
        # Filter shaders
        "denoise": (_PASSTHROUGH_VERTEX, _DENOISE_FRAGMENT),
        "relight": (_PASSTHROUGH_VERTEX, _RELIGHT_FRAGMENT),
        "cryptomatte_select": (_PASSTHROUGH_VERTEX,
                               _CRYPTOMATTE_SELECT_FRAGMENT),
        "zblur": (_PASSTHROUGH_VERTEX, _ZBLUR_FRAGMENT),
        "fog": (_PASSTHROUGH_VERTEX, _FOG_FRAGMENT),
        "depth_matte": (_PASSTHROUGH_VERTEX, _DEPTH_MATTE_FRAGMENT),
        "godrays": (_PASSTHROUGH_VERTEX, _GODRAYS_FRAGMENT),
        "chromatic_aberration": (_PASSTHROUGH_VERTEX,
                                 _CHROMATIC_ABERRATION_FRAGMENT),
        "vignette": (_PASSTHROUGH_VERTEX, _VIGNETTE_FRAGMENT),
        "lens_flare": (_PASSTHROUGH_VERTEX, _LENS_FLARE_FRAGMENT),
        # CP-002 node plugin effects
        "posterize": (_PASSTHROUGH_VERTEX, _POSTERIZE_FRAGMENT),
        "vintage_tone": (_PASSTHROUGH_VERTEX, _VINTAGE_TONE_FRAGMENT),
        "film_grain": (_PASSTHROUGH_VERTEX, _FILM_GRAIN_FRAGMENT),
        # CP-044/045 universal mask gating + mix blend
        "generic_mask_mix": (_PASSTHROUGH_VERTEX, _GENERIC_MASK_MIX_FRAGMENT),
        # CP-011 Keymix
        "keymix": (_PASSTHROUGH_VERTEX, _KEYMIX_FRAGMENT),
        # CP-019 DeGrain combine pass (bilateral estimate via "denoise")
        "degrain_combine": (_PASSTHROUGH_VERTEX, _DEGRAIN_COMBINE_FRAGMENT),
        # CP-014 ColorLookup curve LUT
        "colorlookup": (_PASSTHROUGH_VERTEX, _COLORLOOKUP_FRAGMENT),
        # CP-015 ASC CDL
        "cdl": (_PASSTHROUGH_VERTEX, _CDL_FRAGMENT),
        # Topos Color Stack shader
        "topos_color": (_PASSTHROUGH_VERTEX, _TOPOS_COLOR_FRAGMENT),
        # 3D Scene3D node — 注意：用独立的 _MESH_VERTEX（带 proj/view/model 矩阵），
        # 而非全屏 _PASSTHROUGH_VERTEX。
        # 阶段四：mesh_pbr 是当前默认 PBR 程序（Cook-Torrance + IBL）；mesh_basic 保留为 fallback。
        "mesh_basic": (_MESH_VERTEX, _MESH_BASIC_FRAGMENT),
        "mesh_pbr": (_MESH_VERTEX, _MESH_PBR_FRAGMENT),
        # LIB1 内容库节点效果（计划 L / L04：C01/C17；evaluator dispatch
        # 见 base_evaluator _dispatch_effect 的 lib1_ 分支）
        "lib1_gradient_map": (_PASSTHROUGH_VERTEX, _LIB1_GRADIENT_MAP_FRAGMENT),
        "lib1_rgb_matrix": (_PASSTHROUGH_VERTEX, _LIB1_RGB_MATRIX_FRAGMENT),
        "lib1_despill": (_PASSTHROUGH_VERTEX, _LIB1_DESPILL_FRAGMENT),
        "lib1_solarize": (_PASSTHROUGH_VERTEX, _LIB1_SOLARIZE_FRAGMENT),
        "lib1_mosaic": (_PASSTHROUGH_VERTEX, _LIB1_MOSAIC_FRAGMENT),
        "lib1_crt": (_PASSTHROUGH_VERTEX, _LIB1_CRT_FRAGMENT),
        "lib1_glitch": (_PASSTHROUGH_VERTEX, _LIB1_GLITCH_FRAGMENT),
        "lib1_kaleidoscope": (_PASSTHROUGH_VERTEX, _LIB1_KALEIDOSCOPE_FRAGMENT),
        "lib1_median": (_PASSTHROUGH_VERTEX, _LIB1_MEDIAN_FRAGMENT),
        "lib1_convolution": (_PASSTHROUGH_VERTEX, _LIB1_CONVOLUTION_FRAGMENT),
        "lib1_halation": (_PASSTHROUGH_VERTEX, _LIB1_HALATION_FRAGMENT),
        "lib1_halftone": (_PASSTHROUGH_VERTEX, _LIB1_HALFTONE_FRAGMENT),
        "lib1_hatching": (_PASSTHROUGH_VERTEX, _LIB1_HATCHING_FRAGMENT),
        "lib1_polar_coordinates": (_PASSTHROUGH_VERTEX, _LIB1_POLAR_FRAGMENT),
        "lib1_tile_array": (_PASSTHROUGH_VERTEX, _LIB1_TILE_ARRAY_FRAGMENT),
        "lib1_vector_displace": (_PASSTHROUGH_VERTEX, _LIB1_VECTOR_DISPLACE_FRAGMENT),
        "lib1_stmap": (_PASSTHROUGH_VERTEX, _LIB1_STMAP_FRAGMENT),
        "lib1_freq_blur": (_PASSTHROUGH_VERTEX, _LIB1_FREQ_BLUR_FRAGMENT),
        "lib1_freq_high": (_PASSTHROUGH_VERTEX, _LIB1_FREQ_HIGH_FRAGMENT),
        "lib1_edge_extend_dilate": (
            _PASSTHROUGH_VERTEX, _LIB1_EDGE_EXTEND_DILATE_FRAGMENT),
        "lib1_edge_extend_output": (
            _PASSTHROUGH_VERTEX, _LIB1_EDGE_EXTEND_OUTPUT_FRAGMENT),
    }

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._cache: Dict[Tuple, object] = {}  # key → moderngl.Program
        self._lock = threading.RLock()

    # AUD-6113：fused 源经惰性导入解析，但不再回写类级共享
    # ``_SHADER_SOURCES``——历史上 ``_ensure_fused_source`` 就地变更类字典，
    # 全部实例共享同一可变映射，任何一次写（及与读路径的交错）都波及所有
    # 使用方。改为在实例锁内现取；程序级结果仍由 ``self._cache`` 缓存，
    # 零重复编译。
    _FUSED_SOURCE_NAMES = ("fused", "fused_color")

    @classmethod
    def _resolve_fused_sources(cls, name: str) -> Optional[Tuple[str, str]]:
        """Lazily import fused fragment sources (circular-import safe)."""
        try:
            from ..core.shaders import _FUSED_COLOR_FRAGMENT, _FUSED_FRAGMENT
        except ImportError:
            return None
        if name == "fused":
            return (_PASSTHROUGH_VERTEX, _FUSED_FRAGMENT)
        if name == "fused_color":
            return (_PASSTHROUGH_VERTEX, _FUSED_COLOR_FRAGMENT)
        return None

    def get_program(self, name: str, defines: Tuple[str, ...] = (),
                    version: str = "330"):
        """Get or compile a shader program. Thread-safe.

        Args:
            name: Shader name (e.g., "grade", "blur_h", "fused", "fused_color").
            defines: Preprocessor defines to prepend (future use).
            version: GLSL version string (default "330").

        Returns:
            moderngl.Program on success, None on compile error.
        """
        key = (name, defines, version)

        with self._lock:
            prog = self._cache.get(key)
            if prog is not None:
                return prog

            # Compile on first access
            sources = self._SHADER_SOURCES.get(name)
            if sources is None and name in self._FUSED_SOURCE_NAMES:
                sources = self._resolve_fused_sources(name)
            if sources is None:
                logger.warning(f"Unknown shader name '{name}'")
                return None

            vert_src, frag_src = sources

            # Prepend defines if any
            if defines:
                define_str = "\n".join(f"#define {d}" for d in defines) + "\n"
                # Insert defines after #version line
                frag_src = self._inject_defines(frag_src, define_str)

            prog = self._compile(name, vert_src, frag_src)
            if prog is not None:
                self._cache[key] = prog
            return prog

    def reload(self, name: str):
        """Hot-reload a specific shader by name.

        Removes all cached entries for this shader name and recompiles.
        The new program replaces the old one in the cache.
        """
        with self._lock:
            # Collect all keys for this name
            keys_to_remove = [k for k in self._cache if k[0] == name]

            # Remove old entries
            for key in keys_to_remove:
                old_prog = self._cache.pop(key, None)
                if old_prog is not None:
                    try:
                        old_prog.release()
                    except Exception:
                        pass

            # Recompile with the most recent defines/version if any were cached
            if keys_to_remove:
                for key in keys_to_remove:
                    _, defines, version = key
                    self.get_program(name, defines, version)
            else:
                self.get_program(name)

            logger.info(f"Reloaded shader '{name}'")

    def reload_all(self):
        """Hot-reload all cached programs."""
        with self._lock:
            names = list({k[0] for k in self._cache})
        for name in names:
            self.reload(name)

    def release(self):
        """Release all cached programs."""
        with self._lock:
            for prog in self._cache.values():
                try:
                    prog.release()
                except Exception:
                    pass
            self._cache.clear()

    @property
    def cached_programs(self):
        """Names of currently cached programs (for diagnostics)."""
        with self._lock:
            return sorted({k[0] for k in self._cache})

    @staticmethod
    def _inject_defines(source: str, define_str: str) -> str:
        """Insert preprocessor defines after the #version line."""
        lines = source.split("\n")
        insert_idx = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("#version"):
                insert_idx = i + 1
                break
        lines.insert(insert_idx, define_str)
        return "\n".join(lines)

    def _compile(self, name: str, vert_src: str, frag_src: str):
        """Compile a shader program with error logging."""
        try:
            prog = self._ctx.program(
                vertex_shader=vert_src,
                fragment_shader=frag_src,
            )
            return prog
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"COMPILE ERROR in '{name}': {error_msg}")
            return None
