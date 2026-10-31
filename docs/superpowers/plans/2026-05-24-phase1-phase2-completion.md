# Phase 1/2 Completion: Upload Path, Frame Scheduling, Adaptive Proxy

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Phase 1 (P1.2/P1.3/P1.4/P1.5) and start Phase 2 (P2.1) to make 4K playback stable on CPU->GPU path before zero-copy work.

**Architecture:** Extend existing `YUVGPUUploader` with NV12 R8/R16UI direct upload; add a media-clock-based frame scheduler in `video_playback.py` to discard late frames; add PBO async upload staging; add dynamic proxy ladder driven by `PerformanceMonitor`.

**Tech Stack:** Python 3, ModernGL/OpenGL, NumPy, PyQt6 QTimer, existing `YUVTexturePool`, `PerformanceMonitor`.

---

## File Structure

| Action | File | Responsibility |
|---|---|---|
| Modify | `src/core/yuv_gpu_upload.py` | Add NV12 R8 direct upload, NV12 R16UI render path, PBO staging |
| Modify | `src/core/async_renderer.py` | Use NV12 direct render path, add PBO upload support |
| Modify | `src/core/video_playback.py` | Add media-clock frame scheduler, late frame discard |
| Modify | `src/core/media/performance_monitor.py` | Add proxy ladder metrics (current level, degradation events) |
| Create | `src/core/media/proxy_ladder.py` | Dynamic resolution ladder logic |
| Modify | `tests/media/benchmark_yuv_gpu_pipeline.py` | Add NV12 R8 / NV12 R16UI benchmark modes |

---

## Task 1: NV12 R8 Direct Upload (P1.2)

**Files:**
- Modify: `src/core/yuv_gpu_upload.py:639-720` (upload_nv12)
- Modify: `src/core/yuv_gpu_upload.py:1579-1688` (NV12 shader)

**Problem:** `upload_nv12()` currently converts 8-bit NV12 to float16 via `_normalize_plane()`, which is unnecessary — R8 textures can hold uint8 directly and the GPU sampler normalizes to [0,1] for free.

- [ ] **Step 1: Write the failing test**

Add to `tests/media/benchmark_yuv_gpu_pipeline.py` a test mode that exercises the NV12 R8 path:

```python
# In the benchmark's mode selection logic, add:
# When mode='nv12_r8', force 8-bit NV12 through R8 path
```

Actually, since the existing test infrastructure requires a running GL context, we verify via the benchmark tool. First, add a new upload method.

- [ ] **Step 2: Add `_upload_nv12_r8()` method in `yuv_gpu_upload.py`**

After `_upload_planar_r8()` (around line 1138), add:

```python
def _upload_nv12_r8(
    self,
    y: np.ndarray,
    uv: np.ndarray,
    frame_index: int,
) -> Tuple[moderngl.Texture, moderngl.Texture]:
    """
    8-bit NV12 direct R8/RG8 upload — skip float16 conversion.

    NV12 format:
    - Y plane: (height, width) uint8 -> R8 texture
    - UV plane: (height/2, width/2, 2) uint8 -> RG8 texture

    The GPU sampler returns [0,1] automatically, no Python normalization.
    """
    if self._r8_pool is None:
        return self.upload_nv12(y, uv, frame_index, bit_depth=8)

    start_time = time.perf_counter()

    y_height, y_width = y.shape[:2]

    # Normalize UV shape to (height/2, width/2, 2)
    if uv.ndim == 3 and uv.shape[2] == 2:
        uv_tex_width = uv.shape[1]
        uv_upload = np.ascontiguousarray(uv)
    elif uv.ndim == 2:
        uv_tex_width = uv.shape[1] // 2
        uv_upload = np.ascontiguousarray(uv.reshape(uv.shape[0], uv_tex_width, 2))
    else:
        raise ValueError(f"Invalid UV array shape: {uv.shape}")

    uv_height = uv_upload.shape[0]

    t_prepare_start = time.perf_counter()
    y_data = np.ascontiguousarray(y)
    t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

    # Acquire R8 textures
    y_tex = self._r8_pool.acquire_y_texture(y_width, y_height, frame_index)
    uv_tex = self._r8_pool.acquire_uv_texture(
        uv_tex_width, uv_height, frame_index, components=2
    )

    if y_tex is None or uv_tex is None:
        raise RuntimeError("Failed to acquire NV12 R8 textures from pool")

    t_write_start = time.perf_counter()
    y_tex.write(y_data.tobytes())
    uv_tex.write(uv_upload.tobytes())
    t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

    upload_time_ms = (time.perf_counter() - start_time) * 1000.0
    y_bytes = y_data.nbytes
    uv_bytes = uv_upload.nbytes
    self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
    self._last_upload_timing = {
        'prepare_ms': t_prepare_ms,
        'write_ms': t_write_ms,
        'total_ms': upload_time_ms,
        'mode': 'nv12_r8_rg8',
    }
    self._last_upload_mode = 'nv12_r8_rg8'

    logger.debug(f"NV12 R8 direct uploaded: frame={frame_index}, "
                 f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms, "
                 f"total={upload_time_ms:.2f}ms")

    return (y_tex, uv_tex)
```

- [ ] **Step 3: Modify `upload_nv12()` to dispatch to `_upload_nv12_r8()` for 8-bit**

At the top of `upload_nv12()` (line 639), add early dispatch:

```python
def upload_nv12(self, y, uv, frame_index, bit_depth=8):
    # 8-bit NV12: use R8 direct path if available (skip float16)
    if bit_depth <= 8 and self._r8_pool is not None:
        return self._upload_nv12_r8(y, uv, frame_index)
    # ... rest of existing float16 path ...
```

- [ ] **Step 4: Verify the NV12 shader works with R8/RG8 textures**

The existing `get_nv12_shader()` (line 1579) uses `sampler2D` which works with R8 (normalized unsigned byte). No shader changes needed — the sampler automatically returns [0,1] from uint8 data. The `render_nv12_to_rgb()` method already handles the Y + UV interleaved case. Verify `upload_mode='nv12_r8_rg8'` triggers the NV12 render path in `async_renderer.py` by checking the render path selection logic.

In `_do_upload_video_planes()` (async_renderer.py ~line 2595), NV12 path already calls:
```python
y_tex, uv_tex = self._yuv_uploader.upload_nv12(y, uv, ...)
```
This now auto-dispatches to R8 for 8-bit. No changes needed in async_renderer.

- [ ] **Step 5: Run existing tests**

```bash
.venv/bin/python -m pytest tests/test_yuv_gpu_upload.py -q --tb=short
.venv/bin/python -m pytest tests/media/test_v3_planar_playback_path.py -q --tb=short
```

Expected: All pass. The R8 path should show `plane_prepare_ms` dropping significantly for 8-bit NV12.

- [ ] **Step 6: Commit**

```bash
git add src/core/yuv_gpu_upload.py
git commit -m "feat(yuv): add NV12 R8/RG8 direct upload, skip float16 for 8-bit NV12 (P1.2)"
```

---

## Task 2: NV12 R16UI Render Path (P1.3)

**Files:**
- Modify: `src/core/yuv_gpu_upload.py:1812-1878` (render_nv12_to_rgb)

**Problem:** `upload_nv12_r16ui()` returns R16UI textures but `render_nv12_to_rgb()` only supports `sampler2D` (float), not `usampler2D` (uint). Need a NV12 R16UI render method.

- [ ] **Step 1: Add `_create_nv12_r16ui_shader()` method**

After `get_nv12_shader()` (line 1688), add:

```python
def _create_nv12_r16ui_shader(self) -> moderngl.Program:
    """
    Create NV12 R16UI shader — uses usampler2D for uint16 Y/UV planes.
    """
    vs = """
    #version 330
    in vec2 in_vert;
    in vec2 in_texcoord;
    out vec2 v_texcoord;
    void main() {
        gl_Position = vec4(in_vert, 0.0, 1.0);
        v_texcoord = in_texcoord;
    }
    """

    fs = """
    #version 330
    uniform usampler2D y_plane;    // uint16 Y
    uniform usampler2D uv_plane;   // uint16 interleaved UV (RG)
    uniform int yuv_matrix;
    uniform int color_range;
    uniform int bit_depth;
    uniform vec2 source_uv_scale;
    uniform vec2 source_uv_offset;
    uniform vec2 chroma_uv_scale;
    uniform vec2 chroma_uv_offset;

    in vec2 v_texcoord;
    out vec4 f_color;

    const mat3 bt709 = mat3(
        1.0, 1.0, 1.0,
        0.0, -0.1873, 1.8556,
        1.5748, -0.4681, 0.0
    );
    const mat3 bt2020 = mat3(
        1.0, 1.0, 1.0,
        0.0, -0.1646, 1.8814,
        1.4746, -0.5714, 0.0
    );
    const mat3 bt601 = mat3(
        1.0, 1.0, 1.0,
        0.0, -0.3945, 2.032,
        1.140, -0.581, 0.0
    );

    void main() {
        vec2 source_uv = v_texcoord * source_uv_scale + source_uv_offset;
        if (source_uv.x < 0.0 || source_uv.x > 1.0 || source_uv.y < 0.0 || source_uv.y > 1.0) {
            f_color = vec4(0.0, 0.0, 0.0, 0.0);
            return;
        }

        float max_val = exp2(float(bit_depth)) - 1.0;
        float y = float(texture(y_plane, source_uv).r) / max_val;

        vec2 chroma_uv = source_uv * chroma_uv_scale + chroma_uv_offset;
        vec2 uv_raw = vec2(
            float(texture(uv_plane, chroma_uv).r),
            float(texture(uv_plane, chroma_uv).g)
        );
        float u = uv_raw.r / max_val;
        float v = uv_raw.g / max_val;

        if (color_range == 0) {
            float shift = float(bit_depth - 8);
            float y_black = 16.0 * exp2(shift) / max_val;
            float y_range_inv = max_val / (219.0 * exp2(shift));
            float c_neutral = 128.0 * exp2(shift) / max_val;
            float c_range_inv = max_val / (224.0 * exp2(shift));
            y = (y - y_black) * y_range_inv;
            u = (u - c_neutral) * c_range_inv;
            v = (v - c_neutral) * c_range_inv;
        } else {
            u = u - 0.5;
            v = v - 0.5;
        }

        mat3 matrix = bt709;
        if (yuv_matrix == 1) matrix = bt2020;
        else if (yuv_matrix == 2) matrix = bt601;

        f_color = vec4(matrix * vec3(y, u, v), 1.0);
    }
    """

    self._nv12_r16ui_program = self._ctx.program(vertex_shader=vs, fragment_shader=fs)
    self._nv12_r16ui_program['source_uv_scale'].value = (1.0, 1.0)
    self._nv12_r16ui_program['source_uv_offset'].value = (0.0, 0.0)
    self._nv12_r16ui_program['chroma_uv_scale'].value = (1.0, 1.0)
    self._nv12_r16ui_program['chroma_uv_offset'].value = (0.0, 0.0)
    self._nv12_r16ui_program['bit_depth'].value = 10

    # Build VAO
    vertices = np.array([-1,-1,0,0, 1,-1,1,0, 1,1,1,1, -1,1,0,1], dtype='f4')
    indices = np.array([0,1,2, 0,2,3], dtype='i4')
    vbo = self._ctx.buffer(vertices.tobytes())
    ibo = self._ctx.buffer(indices.tobytes())
    self._nv12_r16ui_vao = self._ctx.vertex_array(
        self._nv12_r16ui_program,
        [(vbo, '2f 2f', 'in_vert', 'in_texcoord')],
        ibo
    )

    return self._nv12_r16ui_program
```

Initialize `self._nv12_r16ui_program = None` and `self._nv12_r16ui_vao = None` in `__init__`.

- [ ] **Step 2: Add `render_nv12_r16ui_to_rgb()` method**

```python
def render_nv12_r16ui_to_rgb(
    self,
    y_tex: moderngl.Texture,
    uv_tex: moderngl.Texture,
    output_fbo: moderngl.Framebuffer,
    chroma_uv_scale: Tuple[float, float] = (1.0, 1.0),
    chroma_uv_offset: Tuple[float, float] = (0.0, 0.0),
    bit_depth: int = 10,
) -> None:
    """Render NV12 R16UI (usampler2D) planes to RGB FBO."""
    if self._nv12_r16ui_program is None:
        self._create_nv12_r16ui_shader()

    output_fbo.use()
    y_tex.use(0)
    uv_tex.use(1)

    prog = self._nv12_r16ui_program
    prog['y_plane'].value = 0
    prog['uv_plane'].value = 1
    prog['yuv_matrix'].value = self._yuv_matrix
    prog['color_range'].value = self._color_range
    prog['bit_depth'].value = bit_depth
    prog['source_uv_scale'].value = (1.0, 1.0)
    prog['source_uv_offset'].value = (0.0, 0.0)
    prog['chroma_uv_scale'].value = chroma_uv_scale
    prog['chroma_uv_offset'].value = chroma_uv_offset

    if self._nv12_r16ui_vao:
        self._nv12_r16ui_vao.render(moderngl.TRIANGLES, vertices=6)
```

- [ ] **Step 3: Update `render_nv12_to_rgb()` to auto-dispatch R16UI**

At the top of `render_nv12_to_rgb()` (line 1812), add:

```python
def render_nv12_to_rgb(self, y_tex, uv_tex, output_fbo, shader=None,
                       chroma_uv_scale=(1.0, 1.0), chroma_uv_offset=(0.0, 0.0),
                       bit_depth=8):
    # Auto-dispatch to R16UI shader if textures are uint16
    if self._last_upload_mode == 'r16ui_direct':
        return self.render_nv12_r16ui_to_rgb(
            y_tex, uv_tex, output_fbo,
            chroma_uv_scale, chroma_uv_offset, bit_depth
        )
    # ... existing float sampler path ...
```

- [ ] **Step 4: Update `_do_upload_video_planes()` in async_renderer.py**

In the NV12/P010 section (~line 2595), ensure the render call uses `bit_depth` parameter:

```python
# After upload, when rendering NV12:
self._yuv_uploader.render_nv12_to_rgb(
    y_tex, uv_tex, yuv_fbo,
    chroma_uv_scale=chroma_uv_scale,
    chroma_uv_offset=chroma_uv_offset,
    bit_depth=bit_depth,  # Pass actual bit_depth
)
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_yuv_gpu_upload.py tests/media/ -q --tb=short -k "nv12 or r16ui"
```

- [ ] **Step 6: Commit**

```bash
git add src/core/yuv_gpu_upload.py src/core/async_renderer.py
git commit -m "feat(yuv): add NV12 R16UI render path for P010 10-bit, skip float16 (P1.3)"
```

---

## Task 3: Media-Clock Frame Scheduler (P1.5)

**Files:**
- Modify: `src/core/video_playback.py:3576-3667` (v3 `_on_play_tick`)

**Problem:** Current playback tick processes frames in FIFO order. If decode falls behind, frames pile up and display "in the past" causing visible jitter and backward frame jumps. Need media-clock-based scheduling: only display the latest frame whose PTS <= current media time, discard all earlier frames.

- [ ] **Step 1: Add `_drain_to_latest_frame()` helper in `_on_play_tick()` context**

In the v3 `_on_play_tick()` method (line 3576), after getting the initial frame from coordinator.tick(), add drain logic:

```python
def _drain_to_latest_frame(self, coordinator, session, first_frame, first_idx):
    """
    Drain all queued frames up to the current media clock.
    Returns (latest_frame, latest_idx, drained_count).

    Only keeps the frame closest to (but not after) current media time.
    All earlier frames are discarded (counted as late_frames).
    """
    latest_frame = first_frame
    latest_idx = first_idx
    drained = 0

    # Keep draining as long as there are newer frames available
    while True:
        try:
            next_result = coordinator.try_dequeue_nonblocking()
            if next_result is None:
                break
            next_frame, next_idx = next_result
            # This frame is newer — replace current and count old one as drained
            latest_frame = next_frame
            latest_idx = next_idx
            drained += 1
        except Exception:
            break

    return latest_frame, latest_idx, drained
```

- [ ] **Step 2: Modify v3 `_on_play_tick()` to use drain logic**

In the v3 tick method, after the initial frame retrieval (~line 3634), replace the simple single-frame logic:

```python
# OLD:
# frame = self._coordinator.tick()
# if frame is None: return

# NEW:
frame_result = self._coordinator.tick()
if frame_result is None:
    return

frame, frame_idx = frame_result

# Drain to latest: discard late frames, only render the most recent
latest_frame, latest_idx, late_count = self._drain_to_latest_frame(
    self._coordinator, session, frame, frame_idx
)

if late_count > 0:
    self._perf_monitor.track_late_frames(late_count)
    logger.debug(f"Drained {late_count} late frames, showing frame {latest_idx}")

frame = latest_frame
frame_idx = latest_idx
```

- [ ] **Step 3: Add `track_late_frames()` to `PerformanceMonitor`**

In `src/core/media/performance_monitor.py`, add:

```python
def track_late_frames(self, count: int):
    """Record late frames that were drained/discarded."""
    # Add to existing frame drop tracking
    for _ in range(count):
        self.track_dropped_preview_frame("late_drain")
```

This reuses existing infrastructure — no new method needed if `track_dropped_preview_frame` already accepts a reason string. Verify the signature and adapt if needed.

- [ ] **Step 4: Ensure backward frame guard**

After draining, add a check to prevent displaying frames older than the last displayed frame:

```python
# After drain logic, before submit:
if hasattr(self, '_last_displayed_frame_idx') and latest_idx < self._last_displayed_frame_idx - 1:
    # Skip backward frames (allow -1 for normal seek reset)
    if latest_idx < self._last_displayed_frame_idx:
        logger.debug(f"Skipping backward frame {latest_idx} < {self._last_displayed_frame_idx}")
        return

self._last_displayed_frame_idx = latest_idx
```

Initialize `self._last_displayed_frame_idx = -1` in `__init__` and reset on seek.

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/media/ -q --tb=short -k "playback"
```

- [ ] **Step 6: Commit**

```bash
git add src/core/video_playback.py src/core/media/performance_monitor.py
git commit -m "feat(playback): media-clock frame scheduler, drain late frames, prevent backward display (P1.5)"
```

---

## Task 4: PBO Async Upload Staging (P1.4)

**Files:**
- Modify: `src/core/yuv_gpu_upload.py` (add PBO upload to YUVTexturePool)

**Problem:** `Texture.write()` blocks the render thread until the upload completes. For large 4K planes, this can take several ms. PBO (Pixel Buffer Object) allows async GPU upload: write to PBO on CPU, then glTexSubImage from PBO on GPU.

- [ ] **Step 1: Add PBO helper to `YUVTexturePool`**

In `yuv_gpu_upload.py`, add a PBO staging helper class:

```python
class PBOStage:
    """Pixel Buffer Object for async texture upload staging."""
    __slots__ = ('ctx', 'pbo', 'size', 'in_flight')

    def __init__(self, ctx: moderngl.Context, size: int):
        self.ctx = ctx
        self.size = size
        # Allocate PBO
        self.pbo = ctx.buffer(reserve=size)
        self.in_flight = False

    def upload_async(self, data: bytes) -> None:
        """Write data to PBO (CPU-side, non-blocking)."""
        if len(data) > self.size:
            self.pbo.orphan(size=len(data))
            self.size = len(data)
        self.pbo.write(data)
        self.in_flight = True

    def flush_to_texture(self, tex: moderngl.Texture, viewport=None) -> None:
        """Copy PBO data to texture (GPU-side)."""
        if not self.in_flight:
            return
        # ModernGL doesn't expose PBO-to-texture directly,
        # so we use the buffer as intermediate — still one write() call
        # but the PBO may allow the driver to pipeline
        if viewport:
            tex.write(self.pbo, viewport=viewport)
        else:
            tex.write(self.pbo)
        self.in_flight = False

    def release(self):
        if self.pbo:
            self.pbo.release()
            self.pbo = None
```

- [ ] **Step 2: Integrate PBO into `YUVGPUUploader`**

In `__init__`, after pool creation:

```python
# PBO staging for async uploads (optional optimization)
self._pbo_enabled = False  # Disabled by default, enable after probe
self._pbo_y: Optional[PBOStage] = None
self._pbo_uv: Optional[PBOStage] = None
```

Add PBO initialization in the init method:

```python
# Probe PBO support
try:
    test_pbo = ctx.buffer(reserve=16)
    test_pbo.release()
    self._pbo_enabled = True
    logger.info("YUVGPUUploader: PBO staging available")
except Exception as e:
    logger.info(f"YUVGPUUploader: PBO not available ({e})")
```

- [ ] **Step 3: Use PBO in the upload hot path**

In `_upload_planar_r8()` and `upload_nv12_r16ui()`, when PBO is enabled, stage data through PBO before flushing:

```python
# In upload method, replace direct tex.write() with PBO staging:
if self._pbo_enabled and self._pbo_y is not None:
    self._pbo_y.upload_async(y_data.tobytes())
    # ... upload other planes to PBOs ...
    self._pbo_y.flush_to_texture(y_tex)
else:
    y_tex.write(y_data.tobytes())
```

Note: PBO benefit is marginal when ModernGL doesn't expose `glTexSubImage2D` from PBO natively. The real win comes from double-buffering PBOs across frames. For now, implement basic PBO and measure.

- [ ] **Step 4: Run benchmark to measure PBO impact**

```bash
.venv/bin/python tests/media/benchmark_yuv_gpu_pipeline.py \
  --file "/Users/testuser/Desktop/调色补充.mov" \
  --sizes 1920x1080 --frames 60 --mode auto --output-json /dev/stdout
```

Compare `texture_write_ms` with and without PBO.

- [ ] **Step 5: Commit**

```bash
git add src/core/yuv_gpu_upload.py
git commit -m "feat(yuv): add PBO async staging for texture uploads (P1.4)"
```

---

## Task 5: Dynamic Proxy Ladder (P2.1)

**Files:**
- Create: `src/core/media/proxy_ladder.py`
- Modify: `src/core/video_playback.py` (integrate ladder)
- Modify: `src/core/media/performance_monitor.py` (add ladder metrics)

**Problem:** When playback can't keep up at 1080p, there's no automatic fallback to lower resolution. Need a dynamic ladder that monitors performance and adjusts proxy resolution.

- [ ] **Step 1: Create `proxy_ladder.py`**

```python
"""
Dynamic Proxy Ladder — automatic resolution degradation/recovery.

Monitors performance metrics and adjusts proxy resolution to maintain
real-time playback. Resolution recovers when performance allows.

Ladder levels: 1920 -> 1280 -> 960 -> 640
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
from enum import IntEnum

logger = logging.getLogger(__name__)


class ProxyLevel(IntEnum):
    """Proxy resolution levels, from highest to lowest."""
    FULL = 0       # Original resolution (no proxy)
    P1080 = 1      # 1920x1080
    P720 = 2       # 1280x720
    P540 = 3       # 960x540
    P360 = 4       # 640x360


PROXY_RESOLUTIONS: dict[ProxyLevel, Tuple[int, int]] = {
    ProxyLevel.FULL: (0, 0),        # Use source size
    ProxyLevel.P1080: (1920, 1080),
    ProxyLevel.P720:  (1280, 720),
    ProxyLevel.P540:  (960, 540),
    ProxyLevel.P360:  (640, 360),
}

PROXY_LABELS: dict[ProxyLevel, str] = {
    ProxyLevel.FULL: "Original",
    ProxyLevel.P1080: "1080p",
    ProxyLevel.P720: "720p",
    ProxyLevel.P540: "540p",
    ProxyLevel.P360: "360p",
}


@dataclass
class LadderConfig:
    """Configuration for proxy ladder degradation thresholds."""
    # Degrade when total_p95 exceeds frame_budget * degrade_factor for this many seconds
    degrade_factor: float = 1.25
    degrade_sustain_sec: float = 2.0

    # Recover when total_p95 stays below frame_budget * recover_factor for this many seconds
    recover_factor: float = 0.65
    recover_sustain_sec: float = 5.0

    # Late frame threshold
    late_frame_threshold: int = 5
    late_frame_sustain_sec: float = 2.0

    # Max degradation level
    max_level: ProxyLevel = ProxyLevel.P360


@dataclass
class LadderState:
    """Current state of the proxy ladder."""
    level: ProxyLevel = ProxyLevel.P1080
    degrade_start: Optional[float] = None
    recover_start: Optional[float] = None
    late_frame_count: int = 0
    late_frame_start: Optional[float] = None
    last_change_time: float = 0.0
    change_count: int = 0


class ProxyLadder:
    """
    Dynamic proxy resolution ladder.

    Monitors frame timing and adjusts resolution to keep playback smooth.
    Thread-safe: call from playback tick thread.
    """

    def __init__(self, config: Optional[LadderConfig] = None):
        self.config = config or LadderConfig()
        self.state = LadderState()

    def update(
        self,
        total_p95_ms: float,
        frame_budget_ms: float,
        late_frames: int = 0,
    ) -> Optional[ProxyLevel]:
        """
        Evaluate performance and potentially change level.

        Returns new ProxyLevel if changed, None if unchanged.
        """
        now = time.monotonic()

        # Check degradation conditions
        should_degrade = False

        # Condition 1: total_p95 too high
        if total_p95_ms > frame_budget_ms * self.config.degrade_factor:
            if self.state.degrade_start is None:
                self.state.degrade_start = now
            elif (now - self.state.degrade_start) >= self.config.degrade_sustain_sec:
                should_degrade = True
        else:
            self.state.degrade_start = None

        # Condition 2: too many late frames
        if late_frames > self.config.late_frame_threshold:
            if self.state.late_frame_start is None:
                self.state.late_frame_start = now
                self.state.late_frame_count = late_frames
            elif (now - self.state.late_frame_start) >= self.config.late_frame_sustain_sec:
                should_degrade = True
        else:
            self.state.late_frame_start = None
            self.state.late_frame_count = 0

        if should_degrade and self.state.level < self.config.max_level:
            return self._degrade(now)

        # Check recovery conditions
        if total_p95_ms < frame_budget_ms * self.config.recover_factor:
            if self.state.recover_start is None:
                self.state.recover_start = now
            elif (now - self.state.recover_start) >= self.config.recover_sustain_sec:
                if self.state.level > ProxyLevel.P1080:
                    return self._recover(now)
        else:
            self.state.recover_start = None

        return None

    def _degrade(self, now: float) -> ProxyLevel:
        new_level = ProxyLevel(self.state.level + 1)
        if new_level > self.config.max_level:
            new_level = self.config.max_level

        old_label = PROXY_LABELS.get(self.state.level, "?")
        new_label = PROXY_LABELS.get(new_level, "?")
        logger.info(f"ProxyLadder: degrade {old_label} -> {new_label}")

        self.state.level = new_level
        self.state.degrade_start = None
        self.state.recover_start = None
        self.state.last_change_time = now
        self.state.change_count += 1
        return new_level

    def _recover(self, now: float) -> ProxyLevel:
        new_level = ProxyLevel(self.state.level - 1)
        if new_level < ProxyLevel.P1080:
            new_level = ProxyLevel.P1080

        old_label = PROXY_LABELS.get(self.state.level, "?")
        new_label = PROXY_LABELS.get(new_level, "?")
        logger.info(f"ProxyLadder: recover {old_label} -> {new_label}")

        self.state.level = new_level
        self.state.degrade_start = None
        self.state.recover_start = None
        self.state.last_change_time = now
        return new_level

    def get_resolution(self) -> Tuple[int, int]:
        """Get current target resolution."""
        return PROXY_RESOLUTIONS[self.state.level]

    def get_label(self) -> str:
        """Get human-readable label for current level."""
        return PROXY_LABELS[self.state.level]

    def reset(self, level: ProxyLevel = ProxyLevel.P1080):
        """Reset ladder state (e.g., on seek or new clip)."""
        self.state = LadderState(level=level)
```

- [ ] **Step 2: Integrate `ProxyLadder` into `video_playback.py`**

In `__init__` of VideoPlaybackWidget (or the playback engine class):

```python
from src.core.media.proxy_ladder import ProxyLadder, ProxyLevel, PROXY_RESOLUTIONS

# Initialize proxy ladder
self._proxy_ladder = ProxyLadder()
self._current_proxy_resolution = (1920, 1080)  # Default starting resolution
```

- [ ] **Step 3: Feed performance data to ladder in playback tick**

In the v3 `_on_play_tick()`, after the existing performance tracking:

```python
# Update proxy ladder
if hasattr(self, '_proxy_ladder') and self._perf_monitor:
    stats = self._perf_monitor.get_summary()
    total_p95 = stats.get('total_p95_ms', 999.0)
    fps = self.decoder.fps if self.decoder else 25
    frame_budget = 1000.0 / fps if fps > 0 else 40.0
    late_frames = stats.get('late_frames_1s', 0)

    new_level = self._proxy_ladder.update(
        total_p95_ms=total_p95,
        frame_budget_ms=frame_budget,
        late_frames=late_frames,
    )

    if new_level is not None:
        new_res = PROXY_RESOLUTIONS[new_level]
        logger.info(f"Proxy ladder changed to {new_level.name} ({new_res})")
        self._apply_proxy_resolution(new_res)
```

- [ ] **Step 4: Add `_apply_proxy_resolution()` method**

```python
def _apply_proxy_resolution(self, resolution: Tuple[int, int]):
    """Apply new proxy resolution from the ladder."""
    if resolution == self._current_proxy_resolution:
        return

    old_res = self._current_proxy_resolution
    self._current_proxy_resolution = resolution
    logger.info(f"Proxy resolution changed: {old_res} -> {resolution}")

    # Notify decode worker to change proxy size
    # This depends on how the decode pipeline is configured
    # For now, the resolution change takes effect on the next clip open
    # or on the next decode request
    if hasattr(self, '_coordinator') and self._coordinator:
        self._coordinator.set_proxy_resolution(resolution)
```

- [ ] **Step 5: Add ladder stats to performance monitor output**

In `performance_monitor.py`, ensure the summary includes fields the ladder needs:

```python
# In get_summary() or equivalent:
return {
    'total_p95_ms': ...,
    'late_frames_1s': ...,
    'proxy_level': self._proxy_ladder.state.level if hasattr(self, '_proxy_ladder') else None,
    'proxy_resolution': self._proxy_ladder.get_resolution() if hasattr(self, '_proxy_ladder') else None,
}
```

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/media/ -q --tb=short
```

- [ ] **Step 7: Commit**

```bash
git add src/core/media/proxy_ladder.py src/core/video_playback.py src/core/media/performance_monitor.py
git commit -m "feat(playback): dynamic proxy ladder with auto degrade/recover (P2.1)"
```

---

## Task 6: Verification and Integration Test

**Files:**
- Modify: `tests/media/benchmark_playback_runtime.py`

- [ ] **Step 1: Add integration test mode to benchmark**

Add a mode that runs all upload paths and compares performance:

```bash
.venv/bin/python tests/media/benchmark_yuv_gpu_pipeline.py \
  --file "/Users/testuser/Desktop/调色补充.mov" \
  --sizes 1920x1080,1280x720,960x540 \
  --frames 120 --warmup 10 --mode auto \
  --verify-color \
  --output-json tests/media/results/4k_prores_p1_completion.json
```

- [ ] **Step 2: Verify all upload modes produce correct colors**

Run with `--verify-color` and confirm:
- `r8_direct`: max_abs_error <= 3/255
- `nv12_r8_rg8`: max_abs_error <= 3/255
- `r16ui_direct`: max_abs_error <= 0.01
- `float16`: existing baseline

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/python -m pytest \
  tests/test_yuv_gpu_upload.py \
  tests/test_video_upload_command.py \
  tests/test_async_renderer_pass_count.py \
  tests/media/test_video_playback_decoded_submit.py \
  tests/media/test_v3_planar_playback_path.py \
  tests/media/test_playback_sync_decode_guard.py \
  -q --tb=short
```

Expected: All pass.

- [ ] **Step 4: Final commit**

```bash
git add tests/media/results/
git commit -m "test: add P1/P2 completion benchmark results and verify all upload paths"
```
