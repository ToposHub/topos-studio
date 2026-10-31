"""
HistogramManager - GPU-accelerated histogram analysis for percentile normalization.

Provides histogram computation using compute shaders with atomic counters.
Used by NormalizeNode's percentile mode to find appropriate value ranges.

Process:
1. Downsample input texture to manageable size (e.g., 512x512)
2. Compute histogram using atomic counters in compute shader
3. Read histogram buffer to CPU
4. Compute CDF and find percentile values on CPU
5. Apply normalization with found values

Caching:
- Histogram results are cached per (texture_id, content token, channel_mask)
- The content token is the upstream texture revision (when known) or a
  cheap 8x8 content fingerprint, so cache entries are invalidated when the
  source texture content changes — including when the pool reuses the same
  GL texture name for a different image (AUD-6009)

Usage:
    manager = HistogramManager(ctx)
    percentiles = manager.compute_percentiles(input_tex, low_pct=0.01, high_pct=0.99)
    # percentiles = {'low': 0.05, 'high': 0.85}
"""

import threading
import hashlib
import logging

import moderngl
import numpy as np
from typing import Dict, Optional, Tuple, List, Any
from dataclasses import dataclass
from collections import OrderedDict

logger = logging.getLogger(__name__)

# 每个诊断 key 只告警一次（与 compute_shader_manager 的策略一致）
_UNAVAILABLE_WARNING_KEYS: set[str] = set()
_UNAVAILABLE_WARNING_LOCK = threading.Lock()


def _warn_unavailable_once(key: str, message: str) -> None:
    with _UNAVAILABLE_WARNING_LOCK:
        if key in _UNAVAILABLE_WARNING_KEYS:
            return
        _UNAVAILABLE_WARNING_KEYS.add(key)
    logger.warning(message)

from ..core.shaders import (
    _HISTOGRAM_COMPUTE,
    _HISTOGRAM_DOWNSAMPLE_FRAGMENT,
    _PASSTHROUGH_VERTEX,
)
from src.shared.gpu.metal import MetalComputeContext
from src.shared.gpu.metal.kernels_histogram import (
    PERCENTILE_BIN_SENTINEL,
    PERCENTILE_PARAMS_DTYPE,
    METAL_HISTOGRAM_MSL,
)


# Histogram configuration
HISTOGRAM_BINS = 256  # Number of bins per channel
HISTOGRAM_CHANNELS = 4  # R, G, B, A
HISTOGRAM_TOTAL_BINS = HISTOGRAM_BINS * HISTOGRAM_CHANNELS  # 1024

# Maximum downsample size for histogram computation
# Larger images are downsampled to this size for performance
MAX_HISTOGRAM_SIZE = 512

# AUD-6009: side of the tiny downsample used to fingerprint texture
# content for cache keys (8x8 RGBA16F readback = 512 bytes).
FINGERPRINT_SIZE = 8


@dataclass
class HistogramResult:
    """Result of histogram computation.

    Attributes:
        histogram: Array of 1024 bins (256 bins x 4 channels)
        cdf: Cumulative distribution function for each channel
        total_pixels: Total number of pixels sampled
        channel_mask: Which channels were analyzed (bitmask)
        texture_revision: Revision of source texture for cache invalidation
        gpu_percentile_bins: P4-03 Metal 路径的 GPU 归约结果
            [ch*2]=low_bin, [ch*2+1]=high_bin；GLSL compute 路径为 None
           （CPU 端 find_percentile_values 从 CDF 重算，结果一致）。
    """
    histogram: np.ndarray  # shape: (1024,)
    cdf: np.ndarray  # shape: (4, 256)
    total_pixels: int
    channel_mask: int
    texture_revision: int
    gpu_percentile_bins: Optional[np.ndarray] = None


class HistogramCache:
    """LRU cache for histogram results.

    Cache key: (texture_id, content_token, channel_mask, params_hash).
    ``content_token`` must describe the texture CONTENT — an upstream
    TextureResult revision or a content fingerprint — never a bare GL id
    with a constant, because the resource pool reuses texture names across
    different images.
    Max entries: 64 (enough for typical workflow)
    """

    def __init__(self, max_entries: int = 64) -> None:
        self._cache: OrderedDict[str, HistogramResult] = OrderedDict()
        self._max_entries = max_entries
        self._lock = threading.RLock()

    def compute_key(self, texture_id: int, content_token: str,
                    channel_mask: int, params_hash: str) -> str:
        """Compute cache key from texture identity and parameters."""
        return f"{texture_id}:{content_token}:{channel_mask}:{params_hash}"

    def get(self, key: str) -> Optional[HistogramResult]:
        """Get cached histogram result. Returns None on miss."""
        with self._lock:
            result = self._cache.get(key)
            if result is not None:
                self._cache.move_to_end(key)  # LRU refresh
                return result
            return None

    def put(self, key: str, result: HistogramResult):
        """Cache a histogram result."""
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                self._cache[key] = result
                # Evict oldest if over limit
                if len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)

    def clear(self):
        """Clear all cached results."""
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        """Number of cached entries (AUD-6012: lock-respecting access)."""
        with self._lock:
            return len(self._cache)


class HistogramManager:
    """Manages GPU histogram computation for percentile normalization.

    Thread-safe via RLock for concurrent access from render and export threads.

    Attributes:
        ctx: ModernGL context
        _cache: HistogramResult cache
        _compute_prog: Compiled histogram compute shader
        _downsample_prog: Compiled downsample fragment shader
    """

    def __init__(self, ctx) -> None:
        """Initialize histogram manager.

        Args:
            ctx: ModernGL context
        """
        self._ctx = ctx
        self._cache = HistogramCache()
        self._lock = threading.RLock()
        self._compute_prog: Optional[object] = None
        self._downsample_prog: Optional[object] = None
        self._histogram_buffer: Optional[object] = None
        self._counter_buffer: Optional[object] = None
        self._available: bool = False
        self._check_availability()

        # P4-03: GL compute 不可用（macOS GL 4.1）时的 Metal compute 路径
        # ——修复 Normalize percentile 模式的静默空操作（计划 §0.4）。
        self._metal_path: Optional["_MetalHistogramPath"] = None
        self._last_diagnostic: str = ""
        if not self._available:
            self._maybe_init_metal_path()

    def _maybe_init_metal_path(self) -> None:
        if not MetalComputeContext.is_supported():
            self._last_diagnostic = (
                "Normalize percentile 模式不可用：GL compute（需 4.3+）与 "
                "Metal compute 均不可用，percentile 结果等价于未启用"
            )
            _warn_unavailable_once(
                "histogram-no-compute", self._last_diagnostic
            )
            return
        try:
            self._metal_path = _MetalHistogramPath(self._ctx)
            logger.info(
                "Histogram: GL compute unavailable, Metal compute path "
                "enabled (device=%s)——Normalize percentile 解锁",
                self._metal_path.context.device_name,
            )
        except Exception as e:  # noqa: BLE001 — Metal 故障不阻塞渲染
            self._metal_path = None
            self._last_diagnostic = f"Metal histogram path init failed: {e}"
            _warn_unavailable_once(
                "histogram-metal-init", self._last_diagnostic
            )

    def _check_availability(self) -> bool:
        """Check if compute shaders are supported (OpenGL 4.3+).

        版本串解析与 ``ComputeShaderManager._parse_gl_version`` 同规则
        （正则取前缀 major.minor）：Mesa 风格的 "4.6 (Core Profile) …"
        走 ``split('.')`` 会因非数字后缀抛 ValueError，把 GL 4.3+ 机器
        误判为不可用且跳过试编译回退——静默能力降级（.clinerules:11）。
        """
        import re

        try:
            if hasattr(self._ctx, 'info'):
                gl_version = self._ctx.info.get('GL_VERSION', '')
                if gl_version:
                    match = re.match(r'^(\d+)\.(\d+)', str(gl_version).strip())
                    if match:
                        major, minor = int(match.group(1)), int(match.group(2))
                        if major > 4 or (major == 4 and minor >= 3):
                            self._available = True
                            return True
                        # 已解析且 < 4.3：直接判不可用，不再试编译（与
                        # ComputeShaderManager 一致——版本号是权威证据，
                        # 也避免 fake 上下文把 4.1 误报为可用）。
                        self._available = False
                        return False

            # Try to compile a test compute shader
            test_source = """
            #version 430
            layout(local_size_x = 1) in;
            void main() {}
            """
            try:
                test_prog = self._ctx.compute_shader(source=test_source)
                test_prog.release()
                self._available = True
                return True
            except Exception:
                self._available = False
                return False
        except Exception:
            self._available = False
            return False

    def is_available(self) -> bool:
        """Check if histogram compute shaders are available.

        P4-03: GL compute（4.3+）或 Metal compute 任一可用即为 True
        ——macOS GL 4.1 上由 Metal 路径解锁 Normalize percentile。
        """
        return self._available or self._metal_path is not None

    def get_diagnostic(self) -> str:
        """P4-03 ③: 显式诊断（无法支持时的原因，空串表示可用/无异常）。"""
        if self.is_available():
            return ""
        return self._last_diagnostic or "histogram compute unavailable"

    def _ensure_programs(self):
        """Ensure compute and downsample shaders are compiled."""
        if self._compute_prog is None:
            try:
                self._compute_prog = self._ctx.compute_shader(source=_HISTOGRAM_COMPUTE)
                logger.debug("Compiled histogram compute shader")
            except Exception as e:
                logger.warning(f"ERROR compiling compute shader: {e}")
                self._available = False

        if self._downsample_prog is None:
            try:
                self._downsample_prog = self._ctx.program(
                    vertex_shader=_PASSTHROUGH_VERTEX,
                    fragment_shader=_HISTOGRAM_DOWNSAMPLE_FRAGMENT,
                )
                logger.debug("Compiled downsample shader")
            except Exception as e:
                logger.warning(f"ERROR compiling downsample shader: {e}")

    def _ensure_buffers(self):
        """Ensure SSBO buffers are created."""
        if self._histogram_buffer is None:
            # Histogram buffer: 1024 uint32 values
            self._histogram_buffer = self._ctx.buffer(
                data=np.zeros(HISTOGRAM_TOTAL_BINS, dtype=np.uint32),
                dynamic=True,
            )

        if self._counter_buffer is None:
            # Counter buffer: 1 uint32 for total pixel count
            self._counter_buffer = self._ctx.buffer(
                data=np.zeros(1, dtype=np.uint32),
                dynamic=True,
            )

    def _clear_buffers(self):
        """Clear histogram and counter buffers."""
        self._ensure_buffers()
        self._histogram_buffer.write(np.zeros(HISTOGRAM_TOTAL_BINS, dtype=np.uint32))
        self._counter_buffer.write(np.zeros(1, dtype=np.uint32))

    def compute_histogram(self, input_tex: object, w: int, h: int,
                          channel_mask: int = 15,  # All channels by default
                          fb_manager: Optional[object] = None,
                          resource_pool: Optional[object] = None,
                          low_percentile: float = 0.0,
                          high_percentile: float = 1.0,
                          texture_revision: Optional[int] = None) -> HistogramResult:
        """Compute histogram from input texture.

        For large images, downsampling is applied first for performance.

        Args:
            input_tex: Input ModernGL texture
            w: Width in pixels
            h: Height in pixels
            channel_mask: Bitmask for channels (1=R, 2=G, 4=B, 8=A)
            fb_manager: Optional FBManager for downsample FBO
            resource_pool: Optional GPUResourcePool for texture allocation
            low_percentile: P4-03 Metal 路径的低百分位（0-1）
            high_percentile: P4-03 Metal 路径的高百分位（0-1）
            texture_revision: Optional authoritative upstream revision;
                falls back to the texture's own ``revision`` attribute
                (0 for raw GL textures).

        Returns:
            HistogramResult with histogram, CDF, and metadata
        """
        resolved_revision = (
            int(texture_revision) if texture_revision is not None
            else int(getattr(input_tex, 'revision', 0) or 0))
        with self._lock:
            self._ensure_programs()
            self._ensure_buffers()

            if not self._available:
                # P4-03: GL compute 不可用（macOS GL 4.1）→ Metal 路径；
                # 两者皆不可用时显式诊断（不再静默返回空直方图）。
                if self._metal_path is not None:
                    return self._compute_histogram_metal(
                        input_tex, w, h, channel_mask, fb_manager,
                        resource_pool, low_percentile, high_percentile,
                        resolved_revision,
                    )
                _warn_unavailable_once(
                    "histogram-empty",
                    "Histogram compute unavailable (no GL 4.3+ compute, "
                    "no Metal): Normalize percentile 模式无法获得直方图，"
                    "结果等价于未启用——" + self.get_diagnostic(),
                )
                return HistogramResult(
                    histogram=np.zeros(HISTOGRAM_TOTAL_BINS, dtype=np.uint32),
                    cdf=np.zeros((4, 256), dtype=np.float32),
                    total_pixels=0,
                    channel_mask=channel_mask,
                    texture_revision=0,
                )

            # Determine if downsampling is needed
            downsample_factor = 1.0
            if w > MAX_HISTOGRAM_SIZE or h > MAX_HISTOGRAM_SIZE:
                downsample_factor = MAX_HISTOGRAM_SIZE / max(w, h)
                ds_w = int(w * downsample_factor)
                ds_h = int(h * downsample_factor)
            else:
                ds_w, ds_h = w, h

            # Downsample if needed
            if downsample_factor < 1.0:
                ds_tex = self._downsample(input_tex, w, h, ds_w, ds_h,
                                          fb_manager, resource_pool)
            else:
                ds_tex = input_tex

            # Clear buffers
            self._clear_buffers()

            # Bind buffers to SSBO bindings
            self._histogram_buffer.bind_to_storage_buffer(binding=0)
            self._counter_buffer.bind_to_storage_buffer(binding=1)

            # Bind input texture
            ds_tex.use(0)

            # Set uniforms
            self._compute_prog['u_input'].value = 0
            self._compute_prog['u_resolution'].value = (float(ds_w), float(ds_h))
            self._compute_prog['u_channel_mask'].value = channel_mask

            # Execute compute shader
            gx = (ds_w + 15) // 16
            gy = (ds_h + 15) // 16
            self._compute_prog.run(gx, gy, 1)

            # Read results
            histogram_data = np.frombuffer(
                self._histogram_buffer.read(size=HISTOGRAM_TOTAL_BINS * 4),
                dtype=np.uint32,
            )
            total_count = int(np.frombuffer(
                self._counter_buffer.read(size=4),
                dtype=np.uint32,
            )[0])

            # Compute CDF on CPU
            cdf = self._compute_cdf(histogram_data, total_count, channel_mask)

            # Release downsampled texture if allocated
            if downsample_factor < 1.0:
                if resource_pool is not None:
                    resource_pool.release_texture(ds_tex)
                else:
                    # AUD-6010: the direct-built texture has no pool to
                    # return to — release it in place instead of leaking it.
                    ds_tex.release()

            return HistogramResult(
                histogram=histogram_data,
                cdf=cdf,
                total_pixels=total_count,
                channel_mask=channel_mask,
                texture_revision=resolved_revision,
            )

    def _compute_histogram_metal(self, input_tex: object, w: int, h: int,
                                 channel_mask: int,
                                 fb_manager: Optional[object],
                                 resource_pool: Optional[object],
                                 low_percentile: float,
                                 high_percentile: float,
                                 texture_revision: int = 0) -> HistogramResult:
        """P4-03: Metal compute 直方图路径（macOS GL 4.1 解锁）。

        流程：GL downsample（4.1 可用）→ tex.read → MTLBuffer →
        histogram kernel → histogram_cdf kernel（同一 command buffer，
        单次 finish 同步）→ 读回 hist/cdf/百分位桶。
        """
        # 下采样决策与 GL 路径一致（≤512 不降采样）
        downsample_factor = 1.0
        if w > MAX_HISTOGRAM_SIZE or h > MAX_HISTOGRAM_SIZE:
            downsample_factor = MAX_HISTOGRAM_SIZE / max(w, h)
            ds_w = int(w * downsample_factor)
            ds_h = int(h * downsample_factor)
        else:
            ds_w, ds_h = w, h

        if downsample_factor < 1.0:
            ds_tex = self._downsample(input_tex, w, h, ds_w, ds_h,
                                      fb_manager, resource_pool)
        else:
            ds_tex = input_tex

        pixels = np.frombuffer(
            ds_tex.read(), dtype="<f2"
        ).reshape(ds_h, ds_w, 4)

        result = self._metal_path.compute(
            pixels, channel_mask, low_percentile, high_percentile
        )

        if downsample_factor < 1.0:
            if resource_pool is not None:
                resource_pool.release_texture(ds_tex)
            else:
                # AUD-6010: direct-built texture, release in place.
                ds_tex.release()

        return HistogramResult(
            histogram=result[0],
            cdf=result[1],
            total_pixels=result[2],
            channel_mask=channel_mask,
            texture_revision=texture_revision,
            gpu_percentile_bins=result[3],
        )

    def _downsample(self, input_tex: object, src_w: int, src_h: int,
                    dst_w: int, dst_h: int,
                    fb_manager: Optional[object],
                    resource_pool: Optional[object]) -> object:
        """Downsample input texture using box filter.

        Args:
            input_tex: Input texture
            src_w, src_h: Source dimensions
            dst_w, dst_h: Destination dimensions
            fb_manager: FBManager for FBO
            resource_pool: GPUResourcePool for texture allocation

        Returns:
            Downsampled texture (RGBA16F)
        """
        if resource_pool is None:
            # Create texture directly
            ds_tex = self._ctx.texture((dst_w, dst_h), 4, dtype='f2')
            ds_tex.filter = (self._ctx.LINEAR, self._ctx.LINEAR)
        else:
            ds_tex = resource_pool.acquire_texture(dst_w, dst_h)

        if fb_manager is None:
            # Create FBO directly
            fbo = self._ctx.framebuffer([ds_tex])
        else:
            fbo = fb_manager.get_fbo(ds_tex)

        fbo.use()
        self._ctx.viewport = (0, 0, dst_w, dst_h)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)

        # Bind input texture
        input_tex.use(0)

        # Set uniforms
        self._downsample_prog['u_texture'].value = 0
        self._downsample_prog['u_src_resolution'].value = (float(src_w), float(src_h))
        self._downsample_prog['u_dst_resolution'].value = (float(dst_w), float(dst_h))

        # Render quad（4 顶点 triangle strip；render() 首参是图元模式）
        vao = self._get_quad_vao()
        vao.render(moderngl.TRIANGLE_STRIP)

        if fb_manager is None:
            # AUD-6010: the direct-built FBO is only needed for this one
            # draw — release it here instead of leaking it to context death.
            fbo.release()

        return ds_tex

    def _get_quad_vao(self) -> object:
        """Get a quad VAO for rendering.

        Creates a simple quad VAO if not exists.
        Uses in_vert and in_uv attribute names matching _PASSTHROUGH_VERTEX.
        """
        if not hasattr(self, '_quad_vao') or self._quad_vao is None:
            # Create quad vertices: 4 vertices with position and UV
            # Format: x, y (in_vert), u, v (in_uv)
            vertices = np.array([
                # Position (in_vert)  UV (in_uv)
                -1.0, -1.0,           0.0, 0.0,
                 1.0, -1.0,           1.0, 0.0,
                -1.0,  1.0,           0.0, 1.0,
                 1.0,  1.0,           1.0, 1.0,
            ], dtype=np.float32)

            vbo = self._ctx.buffer(vertices)
            # 注意 moderngl 的 simple_vertex_array 是 *varargs 签名，
            # 传 list 会落到 detect_format 的 unhashable 错误。
            self._quad_vao = self._ctx.simple_vertex_array(
                self._downsample_prog, vbo, 'in_vert', 'in_uv',
            )
            # AUD-6012: VAO.release() does not release the attribute buffer
            # (moderngl semantics) — keep the reference so release() can.
            self._quad_vbo = vbo

        return self._quad_vao

    def _compute_cdf(self, histogram: np.ndarray, total_pixels: int,
                     channel_mask: int) -> np.ndarray:
        """Compute cumulative distribution function from histogram.

        CDF[i] = sum(histogram[0:i]) / total_pixels

        Args:
            histogram: Histogram array (1024 bins)
            total_pixels: Total number of pixels
            channel_mask: Bitmask for channels

        Returns:
            CDF array (4 channels x 256 bins)
        """
        cdf = np.zeros((4, 256), dtype=np.float32)

        if total_pixels == 0:
            return cdf

        for ch in range(4):
            if (channel_mask & (1 << ch)) == 0:
                continue

            channel_hist = histogram[ch * 256:(ch + 1) * 256]
            channel_total = channel_hist.sum()

            if channel_total > 0:
                # Compute CDF: cumulative sum normalized
                cdf[ch] = np.cumsum(channel_hist).astype(np.float32) / channel_total

        return cdf

    def find_percentile_values(self, cdf: np.ndarray,
                               low_percentile: float,
                               high_percentile: float,
                               channel_idx: int = 0) -> Tuple[float, float]:
        """Find values at specified percentiles from CDF.

        Args:
            cdf: CDF array (4 channels x 256 bins)
            low_percentile: Lower percentile (0.0-1.0, e.g., 0.01)
            high_percentile: Upper percentile (0.0-1.0, e.g., 0.99)
            channel_idx: Channel index (0=R, 1=G, 2=B, 3=A)

        Returns:
            Tuple of (low_value, high_value) in 0-1 range
        """
        if cdf[channel_idx].sum() == 0:
            return (0.0, 1.0)

        # Find bin where CDF crosses percentile threshold
        low_threshold = low_percentile
        high_threshold = high_percentile

        low_bin = 0
        high_bin = 255

        channel_cdf = cdf[channel_idx]

        # Find low percentile bin
        for i in range(256):
            if channel_cdf[i] >= low_threshold:
                low_bin = i
                break

        # Find high percentile bin
        for i in range(255, -1, -1):
            if channel_cdf[i] <= high_threshold:
                high_bin = i
                break

        # Convert bin index to value (bin_center = (bin_idx + 0.5) / 256)
        low_value = (low_bin + 0.5) / 256.0
        high_value = (high_bin + 0.5) / 256.0

        return (low_value, high_value)

    def _texture_content_fingerprint(self, input_tex: object, w: int, h: int,
                                     fb_manager: Optional[object],
                                     resource_pool: Optional[object]) -> str:
        """Lightweight content probe for cache keys (AUD-6009).

        Renders the input into an 8x8 RGBA16F scratch texture with the
        existing downsample shader and hashes the 512-byte readback, so two
        different images never share a cache key even when the resource
        pool reused the same GL texture name for both — the failure mode
        of keying on ``(glo, revision=0)``.  Returns "" when the probe
        cannot run (downsample shader unavailable / readback failure); the
        caller then falls back to the legacy identity components and the
        entry is simply recomputed on the next call.
        """
        try:
            with self._lock:
                self._ensure_programs()
                if self._downsample_prog is None:
                    return ""
                ds_tex = self._downsample(
                    input_tex, w, h,
                    FINGERPRINT_SIZE, FINGERPRINT_SIZE,
                    fb_manager, resource_pool)
                try:
                    raw = ds_tex.read()
                finally:
                    if resource_pool is not None:
                        resource_pool.release_texture(ds_tex)
                    else:
                        ds_tex.release()
            return hashlib.sha256(bytes(raw)).hexdigest()[:16]
        except Exception as exc:  # noqa: BLE001 — probe is best-effort
            logger.debug("Histogram content fingerprint failed: %s", exc)
            return ""

    def compute_percentiles(self, input_tex: object, w: int, h: int,
                            low_percentile: float,
                            high_percentile: float,
                            channel_mask: List[float] = [1.0, 1.0, 1.0, 0.0],
                            fb_manager: Optional[object] = None,
                            resource_pool: Optional[object] = None,
                            use_cache: bool = True,
                            texture_revision: Optional[int] = None) -> Dict[str, float]:
        """Compute percentile values for normalization.

        This is the main entry point for percentile-based normalization.

        Args:
            input_tex: Input texture
            w, h: Dimensions
            low_percentile: Lower percentile (0-100, e.g., 1.0)
            high_percentile: Upper percentile (0-100, e.g., 99.0)
            channel_mask: Channel mask list [r, g, b, a]
            fb_manager: Optional FBManager
            resource_pool: Optional GPUResourcePool
            use_cache: Whether to use cached results
            texture_revision: Optional authoritative content revision from
                the upstream TextureResult.  When omitted, the raw GL
                texture carries no ``revision`` attribute, so the cache key
                is derived from a cheap content fingerprint instead (see
                ``_texture_content_fingerprint``).

        Returns:
            Dict with 'low' and 'high' values (0-1 range)
        """
        # Convert channel mask to bitmask
        mask_bits = 0
        for i, m in enumerate(channel_mask):
            if m > 0.5:
                mask_bits |= (1 << i)

        # Determine which channel to use for percentile (typically luma or first enabled)
        # For simplicity, we use the first enabled channel or compute luma
        primary_channel = 0  # R channel by default
        for i, m in enumerate(channel_mask[:3]):  # Check RGB only
            if m > 0.5:
                primary_channel = i
                break

        # Check cache
        if use_cache:
            # AUD-6009: the cache key must describe CONTENT, not the GL
            # object.  A raw moderngl texture has no ``revision`` attribute,
            # so the previous key degenerated to (glo, 0) and pooled
            # texture-name reuse let one image's percentiles leak into
            # another image's cache slot.  Prefer the authoritative upstream
            # revision; otherwise probe the texture content.
            if texture_revision is not None:
                content_token = f"rev{int(texture_revision)}"
            else:
                upstream_rev = int(getattr(input_tex, 'revision', 0) or 0)
                if upstream_rev > 0:
                    content_token = f"rev{upstream_rev}"
                else:
                    fingerprint = self._texture_content_fingerprint(
                        input_tex, w, h, fb_manager, resource_pool)
                    content_token = f"fp{fingerprint}" if fingerprint else "norev"
            cache_key = self._cache.compute_key(
                input_tex.glo,
                content_token,
                mask_bits,
                f"{low_percentile:.2f}:{high_percentile:.2f}"
                f":{int(w)}x{int(h)}",
            )

            cached = self._cache.get(cache_key)
            if cached is not None:
                return self._percentiles_from_result(
                    cached, low_percentile / 100.0, high_percentile / 100.0,
                    primary_channel,
                )

        # Compute histogram（P4-03: 百分位随行传入 Metal cdf kernel）
        result = self.compute_histogram(
            input_tex, w, h, mask_bits, fb_manager, resource_pool,
            low_percentile=low_percentile / 100.0,
            high_percentile=high_percentile / 100.0,
            texture_revision=texture_revision,
        )

        # Cache result — but never pin a degenerate/empty histogram onto
        # the key: an all-zero result (compute unavailable, empty texture)
        # must not shadow a later valid computation for the same key.
        if use_cache and result.total_pixels > 0 \
                and int(np.asarray(result.histogram).sum()) > 0:
            self._cache.put(cache_key, result)

        return self._percentiles_from_result(
            result, low_percentile / 100.0, high_percentile / 100.0,
            primary_channel,
        )

    def _percentiles_from_result(self, result: HistogramResult,
                                 low_p: float, high_p: float,
                                 primary_channel: int) -> Dict[str, float]:
        """从结果提取百分位：优先 GPU 归约桶，否则 CPU 扫描 CDF（等价）。"""
        gpu_bins = result.gpu_percentile_bins
        if gpu_bins is not None:
            if result.cdf[primary_channel].sum() == 0:
                return {'low': 0.0, 'high': 1.0}  # 与 find_percentile_values 兜底一致
            low_bin = int(gpu_bins[primary_channel * 2])
            high_bin = int(gpu_bins[primary_channel * 2 + 1])
            low_val = (low_bin + 0.5) / 256.0
            high_val = (high_bin + 0.5) / 256.0
            return {'low': low_val, 'high': high_val}

        low_val, high_val = self.find_percentile_values(
            result.cdf, low_p, high_p, primary_channel,
        )
        return {'low': low_val, 'high': high_val}

    def release(self):
        """Release all resources."""
        with self._lock:
            if self._histogram_buffer is not None:
                try:
                    self._histogram_buffer.release()
                except Exception as e:
                    logger.debug(f"Histogram buffer release error: {e}")
                self._histogram_buffer = None

            if self._counter_buffer is not None:
                try:
                    self._counter_buffer.release()
                except Exception as e:
                    logger.debug(f"Counter buffer release error: {e}")
                self._counter_buffer = None

            if self._compute_prog is not None:
                try:
                    self._compute_prog.release()
                except Exception as e:
                    logger.debug(f"Compute program release error: {e}")
                self._compute_prog = None

            if self._downsample_prog is not None:
                try:
                    self._downsample_prog.release()
                except Exception as e:
                    logger.debug(f"Downsample program release error: {e}")
                self._downsample_prog = None

            if hasattr(self, '_quad_vao') and self._quad_vao is not None:
                try:
                    self._quad_vao.release()
                except Exception as e:
                    logger.debug(f"Quad VAO release error: {e}")
                self._quad_vao = None

            # AUD-6012: VAO.release() leaves the attribute VBO alive in the
            # GL context — release the tracked quad VBO alongside it.
            quad_vbo = getattr(self, '_quad_vbo', None)
            if quad_vbo is not None:
                try:
                    quad_vbo.release()
                except Exception as e:
                    logger.debug(f"Quad VBO release error: {e}")
                self._quad_vbo = None

            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics."""
        return {
            'available': self.is_available(),
            'compute_backend': (
                'glsl_compute' if self._available else
                ('metal' if self._metal_path is not None else 'none')
            ),
            'metal_device': (
                self._metal_path.context.device_name
                if self._metal_path is not None else ''
            ),
            'diagnostic': self.get_diagnostic(),
            'bins': HISTOGRAM_BINS,
            'channels': HISTOGRAM_CHANNELS,
            'max_size': MAX_HISTOGRAM_SIZE,
            # AUD-6012: len() on the cache goes through HistogramCache's lock
            # instead of poking at its private OrderedDict unlocked.
            'cache_entries': len(self._cache),
        }


class _MetalHistogramPath:
    """P4-03: HistogramManager 的 Metal compute 执行路径。

    持有独立的 ``MetalComputeContext``（MTLDevice 为进程级对象，多实例
    无额外成本）；histogram 与 histogram_cdf 两个 kernel 编码进同一
    command buffer，单次 finish 同步（无每帧全局阻塞）。
    """

    def __init__(self, gl_ctx: object) -> None:
        self._gl_ctx = gl_ctx
        self.context = MetalComputeContext()
        self._hist_kernel = self.context.compile_kernel(
            "histogram", METAL_HISTOGRAM_MSL
        )
        self._cdf_kernel = self.context.compile_kernel(
            "histogram_cdf", METAL_HISTOGRAM_MSL
        )

    def compute(self, pixels: np.ndarray, channel_mask: int,
                low_percentile: float, high_percentile: float,
            ) -> tuple[np.ndarray, np.ndarray, int, np.ndarray]:
        """执行直方图 + CDF/百分位归约。

        Returns:
            (histogram u32[1024], cdf f32[4,256], total_pixels, bins u32[8])
        """
        metal = self.context
        ds_h, ds_w = pixels.shape[:2]

        src = metal.create_buffer_from_numpy(pixels)
        # 桶/计数缓冲以全零初始化（原子累加前必须归零）
        hist_buf = metal.create_buffer_from_numpy(
            np.zeros(HISTOGRAM_TOTAL_BINS, dtype="<u4")
        )
        total_buf = metal.create_buffer_from_numpy(np.zeros(1, dtype="<u4"))
        res_buf = metal.create_buffer_from_numpy(
            np.array([ds_w, ds_h], dtype="<u4")
        )
        mask_buf = metal.create_buffer_from_numpy(
            np.array([channel_mask], dtype="<u4")
        )

        metal.dispatch(
            self._hist_kernel,
            buffers={0: src, 1: hist_buf, 2: total_buf, 3: res_buf, 4: mask_buf},
            threads=(ds_w, ds_h, 1),
        )

        params = np.zeros(1, dtype=PERCENTILE_PARAMS_DTYPE)
        params["low_percentile"] = low_percentile
        params["high_percentile"] = high_percentile
        params["channel_mask"] = channel_mask
        param_buf = metal.create_buffer_from_numpy(params)
        # cdf 用全零初始化而非未初始化 buffer：mask 外/total==0 的通道
        # kernel 提前 return 不写——未初始化内存会读到 GPU 垃圾，破坏
        # `_percentiles_from_result` 的 cdf.sum()==0 → (0,1) 兜底语义。
        cdf_buf = metal.create_buffer_from_numpy(
            np.zeros(HISTOGRAM_TOTAL_BINS, dtype="<f4")
        )
        # 归约哨兵：0xFFFFFFFF = 无满足桶（atomic_min 的单位元）
        bins_init = np.full(8, PERCENTILE_BIN_SENTINEL, dtype="<u4")
        bins_buf = metal.create_buffer_from_numpy(bins_init)

        metal.dispatch(
            self._cdf_kernel,
            buffers={0: hist_buf, 1: cdf_buf, 2: bins_buf, 3: param_buf},
            threads=(256, 4, 1),
        )
        metal.finish()  # 单次同步点：两个 kernel 一个 commit

        hist = np.frombuffer(metal.read_buffer(hist_buf), dtype="<u4").copy()
        total = int(np.frombuffer(metal.read_buffer(total_buf), dtype="<u4")[0])
        cdf = np.frombuffer(metal.read_buffer(cdf_buf), dtype="<f4").reshape(4, 256).copy()
        # 解码：high 桶存的是 (255-bin) 编码，哨兵 → CPU 端点默认
        raw = np.frombuffer(metal.read_buffer(bins_buf), dtype="<u4").copy()
        bins = np.empty(8, dtype="<u4")
        for ch in range(4):
            low_raw = int(raw[ch * 2])
            high_raw = int(raw[ch * 2 + 1])
            bins[ch * 2] = 0 if low_raw == PERCENTILE_BIN_SENTINEL else low_raw
            bins[ch * 2 + 1] = 255 if high_raw == PERCENTILE_BIN_SENTINEL else 255 - high_raw
        return hist, cdf, total, bins
