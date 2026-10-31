"""
ComputeShaderManager - Manages compute shader compilation and execution.

Provides a unified interface for using compute shaders in the composite
rendering pipeline. Handles:
- Compute shader compilation (GLSL 430+)
- Storage image binding (image2D for texture output)
- Work group size calculation
- Automatic fallback to fragment shader when compute unavailable

Compute shaders are used for:
- Large-radius Gaussian blur (radius > 16)
- Large-radius morphological operations (size > 16)
- Denoising and other compute-intensive operations

Usage:
    manager = ComputeShaderManager(ctx)

    # Check if compute shaders are available
    if manager.is_available():
        # Apply large-radius blur using compute shader
        output_tex = manager.apply_blur_compute(input_tex, radius=50.0, w=1920, h=1080)

    # Automatic dispatch: chooses compute or fragment based on radius
    output_tex = manager.apply_blur(input_tex, radius, w, h, fragment_prog, fb_manager)
"""

import logging
import threading
from typing import Dict, Optional, Tuple, Callable, Any

# Module-level logger
logger = logging.getLogger(__name__)

from ..core.shaders import (
    _BLUR_H_COMPUTE_SSBO,
    _BLUR_V_COMPUTE_SSBO,
    _BLUR_H_COMPUTE_SHARED,
    _BLUR_V_COMPUTE_SHARED,
    _ERODE_DILATE_COMPUTE,
    _ERODE_DILATE_COMPUTE_SHARED,
    # Histogram compute shader for percentile normalization
    _HISTOGRAM_COMPUTE,
)
from .metal_compute_bridge import MetalComputeBridge


# Threshold for switching from fragment shader to compute shader
# When radius exceeds this threshold, compute shader becomes more efficient
COMPUTE_THRESHOLD_RADIUS = 16.0
COMPUTE_THRESHOLD_SIZE = 16

# A CompositeExportRenderer owns a short-lived GPU evaluator for each frame.
# On macOS the compatibility OpenGL context is commonly 4.1, so the same
# capability check can run once per frame.  The fallback is expected, but
# logging it for every frame obscures real render failures.  Keep the warning
# process-wide and keyed by the reported capability so one environment still
# gets a useful diagnostic without producing a playback-sized log storm.
_UNAVAILABLE_WARNING_KEYS: set[str] = set()
_UNAVAILABLE_WARNING_LOCK = threading.Lock()


def _warn_unavailable_once(key: str, message: str, *args: object) -> None:
    """Log a known compute-shader fallback at most once per capability key."""
    with _UNAVAILABLE_WARNING_LOCK:
        if key in _UNAVAILABLE_WARNING_KEYS:
            return
        _UNAVAILABLE_WARNING_KEYS.add(key)
    logger.warning(message, *args)


class ComputeShaderManager:
    """Manages compute shader compilation and execution.

    Thread-safe via RLock for concurrent access from render and export threads.
    Provides automatic fallback to fragment shader when compute unavailable.

    Attributes:
        ctx: ModernGL context
        _cache: Dict mapping shader names to compiled programs
        _lock: Thread lock for safe concurrent access
        _available: Whether compute shaders are supported (GL 4.3+)
        _fallback_callback: Optional callback for UI notification on fallback
        _last_error: Last error message for diagnostics
    """

    # Shader name → compute shader source
    _COMPUTE_SOURCES: Dict[str, str] = {
        "blur_h": _BLUR_H_COMPUTE_SSBO,
        "blur_v": _BLUR_V_COMPUTE_SSBO,
        "blur_h_shared": _BLUR_H_COMPUTE_SHARED,
        "blur_v_shared": _BLUR_V_COMPUTE_SHARED,
        "erode_dilate": _ERODE_DILATE_COMPUTE,
        "erode_dilate_shared": _ERODE_DILATE_COMPUTE_SHARED,
        # Histogram compute shader for percentile normalization
        "histogram": _HISTOGRAM_COMPUTE,
    }

    # Work group size for compute shaders (16x16 = 256 threads)
    WORK_GROUP_SIZE = (16, 16, 1)

    def __init__(self, ctx: object, fallback_callback: Optional[Callable[[str, str], None]] = None) -> None:
        """Initialize compute shader manager.

        Args:
            ctx: ModernGL context
            fallback_callback: Optional callback(shader_name, error_msg) for UI notification
                              when compute shader fails and falls back to fragment shader.
        """
        self._ctx = ctx
        self._cache: Dict[str, object] = {}  # name → moderngl.ComputeShaderProgram
        self._lock = threading.RLock()
        self._available: bool = False
        self._output_texture_cache: Dict[Tuple[int, int], object] = {}
        self._fallback_callback: Optional[Callable[[str, str], None]] = fallback_callback
        self._last_error: str = ""

        # Check compute shader availability
        self._check_availability()

        # P4-02/04: GL compute 不可用（macOS GL 4.1）时的 Metal compute
        # 替代路径。初始化失败不阻塞渲染（显式诊断后落回 fragment fallback）。
        self._metal_bridge: Optional[MetalComputeBridge] = None
        if not self._available:
            self._maybe_init_metal_bridge()

    def _parse_gl_version(self, version_str: str) -> Tuple[int, int]:
        """Parse OpenGL version string robustly.

        Handles various formats:
        - "4.6.0 NVIDIA 535.154.05"
        - "4.6"
        - "1 ATI-6" (malformed, returns 0.0)
        - "3.3.0"

        Args:
            version_str: GL_VERSION string from context

        Returns:
            Tuple of (major, minor) version numbers, (0, 0) if parsing fails
        """
        import re

        # Match version pattern at the start: major.minor
        # This handles cases like "4.6.0 NVIDIA..." or "3.3 Core Profile..."
        match = re.match(r'^(\d+)\.(\d+)', version_str.strip())
        if match:
            return (int(match.group(1)), int(match.group(2)))

        # Fallback: try to extract any two consecutive numbers separated by dot
        match = re.search(r'(\d+)\.(\d+)', version_str)
        if match:
            return (int(match.group(1)), int(match.group(2)))

        return (0, 0)

    def _check_availability(self) -> bool:
        """Check if compute shaders are supported (OpenGL 4.3+).

        Compute shaders require:
        - OpenGL 4.3 or higher
        - GL_ARB_compute_shader extension

        Returns:
            True if compute shaders are available
        """
        try:
            # Check GL version via context info
            if hasattr(self._ctx, 'info'):
                gl_version = self._ctx.info.get('GL_VERSION', '')
                if gl_version:
                    # Parse version string robustly (handles "4.6.0 NVIDIA...", "3.3 Core Profile...")
                    major, minor = self._parse_gl_version(gl_version)

                    if not (major > 4 or (major >= 4 and minor >= 3)):
                        # Version too low — no need to try test compilation
                        self._available = False
                        self._last_error = f"OpenGL {major}.{minor} detected, requires 4.3+"
                        _warn_unavailable_once(
                            f"gl-version:{major}.{minor}",
                            "Compute shaders unavailable: OpenGL %d.%d detected, requires 4.3+. "
                            "Large-radius effects will use fragment shader fallback (slower).",
                            major, minor
                        )
                        return False
                    # W4-01 真机修正：版本达标也必须测试编译。AMD Adrenalin
                    # 26.8.1（RX 6800）实测 GL 报 4.5/4.6 但
                    # glCreateShader(GL_COMPUTE_SHADER) 返回 GL_INVALID_ENUM
                    # ——版本串不可作能力证据（P0-06 探测为准）；测试编译
                    # 每进程仅一次，成本可忽略。

            # Try to compile a simple compute shader (probe-based gating)
            test_source = """
            #version 430
            layout(local_size_x = 1) in;
            void main() {}
            """
            try:
                test_prog = self._ctx.compute_shader(source=test_source)
                test_prog.release()
                self._available = True
                logger.info("Compute shaders available: test compilation successful")
                return True
            except Exception as e:
                self._available = False
                self._last_error = f"Compute shader test compilation failed: {e}"
                logger.warning(
                    "Compute shaders unavailable: test compilation failed. "
                    "GPU may not support GLSL 430 or GL_ARB_compute_shader. "
                    "Error: %s. Large-radius effects will use fragment shader fallback.",
                    e
                )
                return False

        except Exception as e:
            self._available = False
            self._last_error = f"Compute shader availability check failed: {e}"
            logger.warning(
                "Compute shaders unavailable: availability check failed. "
                "Error: %s. Large-radius effects will use fragment shader fallback.",
                e
            )
            return False

    def is_available(self) -> bool:
        """Check if compute shaders are available.

        Returns:
            True if compute shaders can be used
        """
        return self._available

    # ── Metal compute path (P4-02/P4-04) ──

    def _maybe_init_metal_bridge(self) -> None:
        """GL compute 不可用时按需启用 Metal compute 桥（能力显式诊断）。

        默认**不启用**（P4-05 实测：CPU staging 桥的全链路成本远高于
        fragment fallback——见 tests/media/results/metal_compute_ab.md），
        置 ``TOPOS_METAL_COMPUTE=1`` 显式启用（适用于 fragment 路径
        不可用/降级的环境，或未来 Metal 原生纹理互通去除 staging 后）。
        Normalize 百分位链路（HistogramManager）不受此开关影响——那是
        正确性修复而非性能替代。
        """
        import os

        if os.environ.get("TOPOS_METAL_COMPUTE", "").strip().lower() not in (
            "1", "true", "yes",
        ):
            _warn_unavailable_once(
                "metal-disabled-by-default",
                "Metal compute bridge available but disabled by default: "
                "fragment fallback is faster for large-radius effects "
                "(see tests/media/results/metal_compute_ab.md). "
                "Set TOPOS_METAL_COMPUTE=1 to force the Metal path.",
            )
            return
        if not MetalComputeBridge.is_supported():
            _warn_unavailable_once(
                "metal-unsupported",
                "Metal compute unavailable on this machine; "
                "large-radius effects stay on the fragment shader fallback.",
            )
            return
        try:
            self._metal_bridge = MetalComputeBridge(self._ctx)
            logger.info(
                "Metal compute bridge enabled (TOPOS_METAL_COMPUTE=1, "
                "device=%s)",
                self._metal_bridge._ctx.device_name,
            )
        except Exception as e:  # noqa: BLE001 — Metal 故障不阻塞渲染
            logger.warning(
                "Metal compute bridge init failed (%s); "
                "large-radius effects stay on the fragment shader fallback.", e
            )
            self._metal_bridge = None

    def _apply_via_metal(self, effect_name: str, apply, *args, **kwargs) -> Optional[object]:
        """P4-04: Metal 路径走与 GL compute 相同的回退通道。

        失败时经既有 ``_fallback_callback`` 通知 UI，返回 None 让调用方
        落到 fragment fallback——不出现第二套回退语义。
        """
        if self._metal_bridge is None:
            return None
        try:
            return apply(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — 统一回退语义
            self._last_error = f"Metal {effect_name} failed: {e}"
            logger.error(
                "Metal compute failed for %s: %s. "
                "Falling back to fragment shader.", effect_name, e
            )
            if self._fallback_callback:
                try:
                    self._fallback_callback(effect_name, str(e))
                except Exception as cb_error:
                    logger.warning("Fallback callback failed: %s", cb_error)
            return None

    def get_program(self, name: str) -> Optional[object]:
        """Get or compile a compute shader program.

        Args:
            name: Shader name (e.g., "blur_h", "blur_v", "erode_dilate")

        Returns:
            moderngl.ComputeShaderProgram on success, None on error
        """
        if not self._available:
            return None

        with self._lock:
            prog = self._cache.get(name)
            if prog is not None:
                return prog

            # Compile on first access
            source = self._COMPUTE_SOURCES.get(name)
            if source is None:
                self._last_error = f"Unknown shader name: '{name}'"
                logger.error("Unknown shader name '%s'. Valid names: %s", name, list(self._COMPUTE_SOURCES.keys()))
                return None

            prog = self._compile(name, source)
            if prog is not None:
                self._cache[name] = prog
            return prog

    def _compile(self, name: str, source: str) -> Optional[object]:
        """Compile a compute shader with error logging.

        Args:
            name: Shader name for diagnostics
            source: GLSL compute shader source

        Returns:
            Compiled program or None on error
        """
        try:
            prog = self._ctx.compute_shader(source=source)
            logger.info("Compute shader '%s' compiled successfully", name)
            return prog
        except Exception as e:
            error_msg = str(e)
            self._last_error = f"Compile error in '{name}': {error_msg}"
            logger.error(
                "Compute shader compilation failed for '%s'. "
                "Error: %s. "
                "Falling back to fragment shader (performance may be degraded).",
                name, error_msg
            )
            # Notify UI about fallback if callback is registered
            if self._fallback_callback:
                try:
                    self._fallback_callback(name, error_msg)
                except Exception as cb_error:
                    logger.warning("Fallback callback failed: %s", cb_error)
            return None

    def reload(self, name: str) -> bool:
        """Hot-reload a specific compute shader.

        Args:
            name: Shader name to reload

        Returns:
            True if reload successful, False otherwise
        """
        with self._lock:
            old_prog = self._cache.pop(name, None)
            if old_prog is not None:
                try:
                    old_prog.release()
                    logger.debug("Released old compute shader '%s' for hot-reload", name)
                except Exception as e:
                    logger.warning("Failed to release old compute shader '%s': %s", name, e)
            result = self.get_program(name)
            if result is not None:
                logger.info("Hot-reloaded compute shader '%s'", name)
                return True
            else:
                logger.error("Failed to hot-reload compute shader '%s'", name)
                return False

    def reload_all(self) -> None:
        """Hot-reload all cached compute shader programs."""
        with self._lock:
            names = list(self._cache.keys())
        for name in names:
            self.reload(name)

    def release(self) -> None:
        """Release all cached programs and textures.

        Cleans up all GPU resources held by this manager.
        Logs any errors during release but continues cleanup.
        """
        with self._lock:
            # Release all cached compute shader programs
            for name, prog in list(self._cache.items()):
                try:
                    prog.release()
                    logger.debug("Released compute shader program '%s'", name)
                except Exception as e:
                    logger.warning("Failed to release compute shader program '%s': %s", name, e)
            self._cache.clear()

            # Release all cached output textures
            for key, tex in list(self._output_texture_cache.items()):
                try:
                    tex.release()
                    logger.debug("Released output texture %s", key)
                except Exception as e:
                    logger.warning("Failed to release output texture %s: %s", key, e)
            self._output_texture_cache.clear()

        logger.info("ComputeShaderManager: all resources released")

    # ── Blur Compute Shader Execution ──

    def _memory_barrier(self) -> None:
        """Issue glMemoryBarrier after a compute dispatch (AUD-8211).

        The compute kernels write through ``imageStore`` and the next pass
        (or the downstream fragment sampler) reads the same texture through
        ``texture()`` fetches. Per the OpenGL spec (§2.13.6/7.11) image
        writes are not visible to texture fetches without an explicit
        barrier — omitting it is undefined behaviour and shows up as torn
        or stale blur/morphology results on strict drivers. No-ops (with a
        one-time debug log) when the context backend lacks the method.
        """
        barrier = getattr(self._ctx, "memory_barrier", None)
        if not callable(barrier):
            logger.debug(
                "Context has no memory_barrier; skipping post-dispatch barrier")
            return
        try:
            import moderngl
            barrier(moderngl.TEXTURE_FETCH_BARRIER_BIT
                    | moderngl.SHADER_IMAGE_ACCESS_BARRIER_BIT)
        except Exception as exc:  # noqa: BLE001 — barrier 不可用不致命（多数驱动按序提交）
            logger.debug("memory_barrier failed: %s", exc)

    def apply_blur_compute(self, input_tex: object, radius: float,
                           w: int, h: int, resource_pool: object,
                           edge_policy: int = 0) -> Optional[object]:
        """Apply separable Gaussian blur using compute shaders.

        Two-pass approach:
        1. Horizontal blur: input → intermediate texture
        2. Vertical blur: intermediate → output texture

        Args:
            input_tex: Input ModernGL texture
            radius: Blur radius in pixels
            w: Width in pixels
            h: Height in pixels
            resource_pool: GPUResourcePool for texture allocation
            edge_policy: 0=clamp, 1=wrap, 2=mirror

        Returns:
            Output texture (RGBA16F), or None on error / unsupported
            policy (mirror falls back to the fragment path — see below).
        """
        if edge_policy == 2:
            # AUD-8212: neither the GLSL compute kernels nor the Metal
            # bridge implement the mirror(2) edge policy (both fall into
            # their clamp/wrap branches), while the fragment path has a
            # complete mirror branch. Decline the compute path so the
            # caller uses the fragment shader and stays visually correct.
            _warn_unavailable_once(
                "mirror-edge-compute-blur",
                "Compute blur does not implement the mirror edge policy; "
                "mirror blur uses the fragment shader path.")
            return None

        if not self._available:
            # P4-02: GL compute 不可用（macOS GL 4.1）→ Metal compute 桥。
            if self._metal_bridge is None:
                return None
            return self._apply_via_metal(
                "metal_blur", self._metal_bridge.apply_blur,
                input_tex, radius, w, h, resource_pool, edge_policy,
            )

        # Choose shader based on radius (shared memory vs simple)
        use_shared = radius > 32.0
        prog_h_name = "blur_h_shared" if use_shared else "blur_h"
        prog_v_name = "blur_v_shared" if use_shared else "blur_v"

        prog_h = self.get_program(prog_h_name)
        prog_v = self.get_program(prog_v_name)
        if prog_h is None or prog_v is None:
            return None

        # Create intermediate texture for horizontal pass output
        intermediate_tex = self._create_output_texture(w, h, resource_pool)

        # Horizontal pass: input → intermediate
        self._run_blur_pass(prog_h, input_tex, intermediate_tex, radius, w, h, edge_policy)

        # Create output texture
        output_tex = self._create_output_texture(w, h, resource_pool)

        # Vertical pass: intermediate → output
        self._run_blur_pass(prog_v, intermediate_tex, output_tex, radius, w, h, edge_policy)

        # Release intermediate texture
        if resource_pool:
            resource_pool.release_texture(intermediate_tex)
        else:
            intermediate_tex.release()

        return output_tex

    def _run_blur_pass(self, prog: object, input_tex: object,
                       output_tex: object, radius: float,
                       w: int, h: int, edge_policy: int) -> None:
        """Execute a single blur pass (horizontal or vertical).

        Args:
            prog: Compute shader program
            input_tex: Input texture (sampler2D)
            output_tex: Output texture (image2D)
            radius: Blur radius
            w: Width
            h: Height
            edge_policy: Edge handling mode
        """
        # Bind input texture to texture unit 0
        input_tex.use(0)

        # Bind output texture as image2D (write-only)
        # ModernGL uses bind_to_image method for compute shaders
        output_tex.bind_to_image(0, read=False, write=True)

        # Set uniforms
        prog['u_input'].value = 0  # Sampler binding
        prog['u_output'].value = 0  # Image binding (moderngl maps same slot)
        prog['u_resolution'].value = (float(w), float(h))
        prog['u_radius'].value = radius
        prog['u_edge_policy'].value = edge_policy

        # Calculate work groups: ceil(w/16) x ceil(h/16)
        gx = (w + 15) // 16
        gy = (h + 15) // 16

        # Execute compute shader
        prog.run(gx, gy, 1)
        # AUD-8211: the imageStore writes must be visible to the next
        # dispatch's texture() fetches (H→V chain) and to downstream
        # fragment sampling of the final output.
        self._memory_barrier()

    def _create_output_texture(self, w: int, h: int,
                               resource_pool: Optional[object]) -> object:
        """Create an output texture for compute shader writes.

        Compute shaders require textures that can be bound as image2D.
        RGBA16F format is used for HDR compatibility.

        Args:
            w: Width
            h: Height
            resource_pool: Optional GPUResourcePool for allocation

        Returns:
            ModernGL texture (RGBA16F)
        """
        if resource_pool:
            # Use resource pool for proper memory tracking
            return resource_pool.acquire_texture(w, h, components=4, dtype='f2')
        else:
            # Direct creation (fallback)
            tex = self._ctx.texture((w, h), 4, dtype='f2')
            tex.filter = (self._ctx.LINEAR, self._ctx.LINEAR)
            return tex

    # ── Erode/Dilate Compute Shader Execution ──

    def apply_erode_dilate_compute(self, input_tex: object,
                                    operation: str, size: int,
                                    w: int, h: int,
                                    resource_pool: object,
                                    channels: int = 0) -> Optional[object]:
        """Apply morphological erosion or dilation using compute shader.

        Args:
            input_tex: Input ModernGL texture
            operation: "erode" or "dilate"
            size: Kernel size
            w: Width
            h: Height
            resource_pool: GPUResourcePool for texture allocation
            channels: 0=alpha only, 1=all channels

        Returns:
            Output texture (RGBA16F), or None on error
        """
        if not self._available:
            # P4-02: GL compute 不可用（macOS GL 4.1）→ Metal compute 桥。
            if self._metal_bridge is None:
                return None
            return self._apply_via_metal(
                "metal_erode_dilate", self._metal_bridge.apply_erode_dilate,
                input_tex, operation, size, w, h, resource_pool, channels,
            )

        # Choose shader based on size (shared memory vs simple)
        use_shared = size > 16
        prog_name = "erode_dilate_shared" if use_shared else "erode_dilate"

        prog = self.get_program(prog_name)
        if prog is None:
            return None

        # Create output texture
        output_tex = self._create_output_texture(w, h, resource_pool)

        # Bind textures
        input_tex.use(0)
        output_tex.bind_to_image(0, read=False, write=True)

        # Set uniforms
        prog['u_input'].value = 0
        prog['u_output'].value = 0
        prog['u_resolution'].value = (float(w), float(h))
        prog['u_operation'].value = 0 if operation == "erode" else 1
        prog['u_size'].value = size
        prog['u_channels'].value = channels

        # Calculate work groups
        gx = (w + 15) // 16
        gy = (h + 15) // 16

        # Execute
        prog.run(gx, gy, 1)
        # AUD-8211: imageStore writes → downstream texture() fetches.
        self._memory_barrier()

        return output_tex

    # ── Automatic Dispatch (Compute vs Fragment) ──

    def should_use_compute_blur(self, radius: float) -> bool:
        """Determine whether compute shader should be used for blur.

        Args:
            radius: Blur radius in pixels

        Returns:
            True if compute shader is recommended
        """
        if not (self._available or self._metal_bridge is not None):
            return False
        return radius > COMPUTE_THRESHOLD_RADIUS

    def should_use_compute_erode_dilate(self, size: int) -> bool:
        """Determine whether compute shader should be used for erode/dilate.

        Args:
            size: Kernel size

        Returns:
            True if compute shader is recommended
        """
        if not (self._available or self._metal_bridge is not None):
            return False
        return size > COMPUTE_THRESHOLD_SIZE

    @property
    def cached_programs(self) -> list:
        """Names of currently cached programs (for diagnostics)."""
        with self._lock:
            return sorted(self._cache.keys())

    def get_last_error(self) -> str:
        """Get the last error message for diagnostics.

        Returns:
            Last error message, or empty string if no error.
        """
        return self._last_error

    def clear_last_error(self) -> None:
        """Clear the last error message."""
        self._last_error = ""

    def set_fallback_callback(self, callback: Optional[Callable[[str, str], None]]) -> None:
        """Set or update the fallback notification callback.

        Args:
            callback: Function(shader_name, error_msg) to call when compute shader
                     fails and falls back to fragment shader. Pass None to disable.
        """
        self._fallback_callback = callback

    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics for diagnostics.

        Returns:
            Dictionary with availability status, cached programs, and thresholds.
        """
        compute_api = "glsl_compute" if self._available else (
            "metal" if self._metal_bridge is not None else "fragment_only"
        )
        stats = {
            'available': self._available,
            'compute_api': compute_api,
            'cached_programs': self.cached_programs,
            'threshold_radius': COMPUTE_THRESHOLD_RADIUS,
            'threshold_size': COMPUTE_THRESHOLD_SIZE,
            'work_group_size': self.WORK_GROUP_SIZE,
            'last_error': self._last_error,
        }
        if self._metal_bridge is not None:
            stats['metal_device'] = self._metal_bridge._ctx.device_name
        return stats
