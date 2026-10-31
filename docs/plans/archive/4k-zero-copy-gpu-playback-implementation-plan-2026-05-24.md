# 4K 全程 GPU 零拷贝播放实施方案

日期：2026-05-24  
目标项目：Topos Color  
适用范围：Color 页面实时播放预览、4K H.264/HEVC、NV12/P010、macOS VideoToolbox、Windows D3D11VA/NVDEC/QSV、AsyncRenderer、YUVGPUUploader、VideoPlaybackEngine、DecodeWorker。  

最终目标：

```text
压缩视频码流
  -> GPU/硬件解码
  -> GPU resident frame / external GPU texture planes
  -> GPU YUV 转 RGB
  -> GPU 调色 / Raw View / 节点处理
  -> GPU 输出到 viewport
```

最终热路径禁止：

```text
frame.to_ndarray()
frame.transfer()
np.array(video_frame)
Texture.write(video_frame_bytes)
每帧 fbo.read()
每帧 ctx.finish()/glFinish()
```

例外：

- self-test / benchmark 的少量颜色抽样可以 readback，但必须显式开关控制。
- fallback 路径可以 `to_ndarray()` / `Texture.write()`，但日志必须明确标记为 fallback，不能伪装成零拷贝。
- ProRes 4444/12-bit 在零拷贝未验证前允许继续走 CPU decode + R16UI/P010/R8 上传 + proxy。

---

## 0. 当前状态判断

当前项目已经完成了比较重要的 fallback 优化，但还没有完成真正的全程 GPU 链路。

已经完成或基本完成：

| 能力 | 当前状态 | 典型文件 |
|---|---|---|
| 播放 tick 不在 UI 线程做大数组 YUV->RGB | 已有 | `src/core/video_playback.py` |
| YUV planes 提交给 renderer 线程处理 | 已有 | `src/core/video_playback.py`, `src/core/async_renderer.py` |
| 8-bit planar / NV12 R8/RG8 直传 | 已有 | `src/core/yuv_gpu_upload.py` |
| 10/12/16-bit R16UI/usampler2D 路径 | 已有 | `src/core/yuv_gpu_upload.py` |
| GPU YUV->RGB shader + 后续调色 | 已有 | `src/core/yuv_gpu_upload.py`, `src/core/async_renderer.py` |
| 播放默认不每帧 `ctx.finish()` | 已有 | `src/core/async_renderer.py` |
| 分段性能统计 | 已有一部分 | `src/core/media/performance_monitor.py` |
| proxy ladder / backpressure | 已有一部分 | `src/core/video_playback.py`, `src/core/media/performance_monitor.py` |

尚未完成：

| 目标 | 当前缺口 |
|---|---|
| GPU 解码结果直接进入 OpenGL/ModernGL | 还没有 `GpuDecodedFrame` / external texture 主链路 |
| macOS VT/CVPixelBuffer/IOSurface -> GL texture | 还没有 probe 和桥接模块 |
| Windows D3D11 texture -> OpenGL interop | 还没有 probe 和桥接模块 |
| DecodeWorker 输出 GPU resident frame | 目前输出 `DecodedFrame`，仍以 CPU ndarray/planes 为核心 |
| 零拷贝 frame 生命周期管理 | 还没有 retain/release + fence/ring buffer |
| `frame_path=macos_vt_iosurface_zero_copy` 真实可见 | 目前只在计划文档中出现 |

必须牢记：

```text
hwaccel_download_packed_rgb != zero copy
VideoToolbox + to_ndarray != zero copy
NVDEC/D3D11VA + av_hwframe_transfer_data != zero copy
R16UI/R8 direct upload != zero copy
```

---

## 1. 总体验收标准

实施完成后，必须同时满足以下条件才算真正完成。

### 1.1 链路验收

4K H.264/HEVC 播放时，日志必须出现：

```text
decode_path=macos_vt_iosurface_zero_copy
frame_path=gpu_decoded_external_yuv
cpu_frame_download=false
cpu_texture_upload=false
texture_write_ms=0
plane_prepare_ms=0
external_texture_bind_ms=...
yuv_render_p95_ms=...
render_submit_p95_ms=...
```

Windows 对应为：

```text
decode_path=windows_d3d11_gl_zero_copy
frame_path=gpu_decoded_external_yuv
cpu_frame_download=false
cpu_texture_upload=false
```

fallback 时必须明确：

```text
decode_path=hwaccel_download_packed_rgb
frame_path=packed_rgb_cpu_upload
zero_copy=false
fallback_reason=...
```

### 1.2 禁止项验收

在零拷贝热路径中，以下函数不允许出现调用：

```text
frame.to_ndarray()
frame.transfer()
np.asarray(frame)
Texture.write() 写视频帧
Framebuffer.read()
ctx.finish()
glFinish()
```

检查命令：

```bash
rg -n "to_ndarray|frame\\.transfer|Texture\\.write|\\.write\\(|fbo\\.read|Framebuffer\\.read|ctx\\.finish|glFinish" src/core src/ui tests/media
```

要求：

- 命中可以存在于 CPU fallback、self-test、export、scope、benchmark verify 中。
- 零拷贝模块和 `GpuDecodedFrame` 渲染路径不能命中这些调用。

### 1.3 性能验收

| 指标 | 4K 30fps H.264/HEVC | 4K 60fps H.264/HEVC |
|---|---:|---:|
| decode callback p95 | < 8ms | < 6ms |
| external texture bind p95 | < 1.5ms | < 1ms |
| YUV render p95 | < 2ms | < 1.5ms |
| grading/render p95 | < 8ms | < 6ms |
| UI tick p95 | < 3ms | < 2ms |
| dropped/late frames | 60 秒内无持续堆积 | 可自适应降级 |

### 1.4 颜色验收

同一帧比较三条路径：

```text
seek/HQ CPU reference
CPU upload GPU YUV fallback
zero-copy external YUV
```

要求：

| 项目 | 标准 |
|---|---|
| 中性灰 | RGB 三通道差值 <= 2/255 |
| 黑白场 | limited/full range 正确 |
| 肤色 | 不蓝脸、不绿脸 |
| P010 10-bit | 不压黑、不发灰 |
| BT.709 / BT.2020 | matrix 不混用 |
| seek/playback | 同一帧颜色一致 |

---

## 2. 实施顺序总览

不要一上来直接改 `AsyncRenderer`。正确顺序：

```text
Phase A: 冻结当前 fallback 基线和日志
Phase B: 定义统一 GPU frame 抽象
Phase C: macOS 独立 zero-copy probe
Phase D: macOS 接入主播放链路
Phase E: Windows 独立 zero-copy probe
Phase F: Windows 接入主播放链路
Phase G: ProRes / proxy / fallback 完善
Phase H: 长期回归测试和验收脚本
```

每个 Phase 完成后都必须测试，不能跳 Phase。

---

## Phase A：冻结 fallback 基线和可观测性

目标：先确保现有 CPU->GPU fallback 是可信的，并且后续零拷贝接入不会把 fallback 伪装成成功。

### A1. 统一 frame path 命名

建议新增：

```text
src/core/media/frame_path.py
```

定义：

```python
from enum import Enum

class DecodePath(str, Enum):
    CPU_PLANAR_YUV = "cpu_planar_yuv"
    CPU_PACKED_RGB = "cpu_packed_rgb"
    HWACCEL_DOWNLOAD_PACKED_RGB = "hwaccel_download_packed_rgb"
    HWACCEL_DOWNLOAD_PLANAR_YUV = "hwaccel_download_planar_yuv"
    MACOS_VT_IOSURFACE_ZERO_COPY = "macos_vt_iosurface_zero_copy"
    WINDOWS_D3D11_GL_ZERO_COPY = "windows_d3d11_gl_zero_copy"

class RenderFramePath(str, Enum):
    PACKED_RGB_CPU_UPLOAD = "packed_rgb_cpu_upload"
    PLANAR_YUV_R8_UPLOAD = "planar_yuv_r8_upload"
    PLANAR_YUV_R16UI_UPLOAD = "planar_yuv_r16ui_upload"
    NV12_R8_RG8_UPLOAD = "nv12_r8_rg8_upload"
    P010_R16UI_UPLOAD = "p010_r16ui_upload"
    GPU_DECODED_EXTERNAL_YUV = "gpu_decoded_external_yuv"
    GPU_DECODED_EXTERNAL_RGB = "gpu_decoded_external_rgb"
```

涉及文件：

```text
src/core/media/decode_policy.py
src/core/media/factory.py
src/core/media/performance_monitor.py
src/core/video_playback.py
src/core/async_renderer.py
src/ui/playback_hud.py
tests/media/test_v3_hwaccel_policy.py
```

验收：

```bash
.venv/bin/python -m pytest tests/media/test_v3_hwaccel_policy.py tests/media/test_performance_monitor.py -q --tb=short
```

### A2. 增加零拷贝判定字段

`VideoPerformanceMonitor.get_aggregate_stats()` 增加：

```text
zero_copy_frames
cpu_download_frames
cpu_upload_frames
external_texture_frames
texture_write_video_frames
frame_path_counts
decode_path_counts
last_fallback_reason
```

要求：

- CPU fallback 每次 `Texture.write()` 视频帧时，`cpu_upload_frames += 1`。
- 硬解下载每次 `frame.transfer()` / `to_ndarray()` 时，`cpu_download_frames += 1`。
- external GPU frame 每次直接绑定外部 texture 时，`external_texture_frames += 1`。

验收：

```bash
.venv/bin/python -m pytest tests/media/test_performance_monitor.py -q --tb=short
```

### A3. 补强 benchmark

修改：

```text
tests/media/benchmark_playback_runtime.py
tests/media/benchmark_yuv_gpu_pipeline.py
```

要求输出 JSON 字段：

```json
{
  "decode_path": "cpu_planar_yuv",
  "frame_path": "planar_yuv_r16ui_upload",
  "zero_copy": false,
  "cpu_download_frames": 120,
  "cpu_upload_frames": 120,
  "external_texture_frames": 0,
  "decode_ms": {"p50": 0, "p95": 0, "max": 0},
  "plane_prepare_ms": {"p50": 0, "p95": 0, "max": 0},
  "texture_write_ms": {"p50": 0, "p95": 0, "max": 0},
  "external_bind_ms": {"p50": 0, "p95": 0, "max": 0},
  "yuv_render_ms": {"p50": 0, "p95": 0, "max": 0},
  "total_ms": {"p50": 0, "p95": 0, "max": 0},
  "dropped_frames": 0,
  "late_frames": 0,
  "coalesced_video_commands": 0
}
```

验收命令：

```bash
.venv/bin/python tests/media/benchmark_yuv_gpu_pipeline.py \
  --file "/path/to/4k_prores.mov" \
  --sizes 1920x1080,1280x720,960x540 \
  --frames 120 \
  --warmup 10 \
  --mode auto \
  --output-json tests/media/results/prores_fallback_baseline.json
```

```bash
.venv/bin/python tests/media/benchmark_playback_runtime.py \
  --file "/path/to/4k_h264.mp4" \
  --frames 300 \
  --warmup 30 \
  --sizes 3840x2160,1920x1080,1280x720 \
  --output-json tests/media/results/h264_playback_runtime_baseline.json
```

完成标准：

- benchmark 能跑完。
- JSON 字段齐全。
- 当前 fallback 路径必须显示 `zero_copy=false`。

---

## Phase B：定义统一 GPU resident frame 抽象

目标：让上层播放链路可以同时处理 CPU `DecodedFrame` 和 GPU `GpuDecodedFrame`。

### B1. 新增数据结构

建议新增：

```text
src/core/media/gpu_frame.py
```

定义：

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Literal, Sequence

GpuApi = Literal["opengl", "metal", "d3d11", "cuda", "unknown"]
TextureTarget = Literal["2d", "rectangle", "external", "unknown"]
PlaneComponent = Literal["Y", "UV", "U", "V", "R", "G", "B", "A", "RGBA"]

@dataclass(frozen=True)
class GpuFramePlane:
    component: PlaneComponent
    api: GpuApi
    texture_id: int
    target: TextureTarget
    width: int
    height: int
    internal_format: str
    pixel_format: str
    pixel_type: str
    texture_unit_hint: int = 0

@dataclass
class GpuFrameRelease:
    retain_token: object | None = None
    release_callback: Optional[Callable[[], None]] = None
    fence: object | None = None

    def release(self) -> None:
        if self.release_callback:
            self.release_callback()

@dataclass
class GpuDecodedFrame:
    frame_index: int
    pts: int | None
    time_seconds: float
    width: int
    height: int
    visible_rect: tuple[int, int, int, int] | None
    pixel_format: str
    bit_depth: int
    planes: Sequence[GpuFramePlane]
    color_range: str
    color_matrix: str
    color_primaries: str | None
    color_transfer: str | None
    decode_path: str
    zero_copy: bool
    release: GpuFrameRelease
```

要求：

- `GpuDecodedFrame.zero_copy` 对真正 external texture 路径必须为 `True`。
- 任何 CPU upload/download fallback 必须继续用 `DecodedFrame`，不要伪造 `GpuDecodedFrame`。
- `release.release()` 不能由 UI tick 调用，必须由渲染线程在安全时机调用。

测试：

```text
tests/media/test_gpu_frame.py
```

验收：

```bash
.venv/bin/python -m pytest tests/media/test_gpu_frame.py -q --tb=short
```

### B2. 新增 decoder 输出类型

修改或新增协议：

```text
src/core/media/source.py
src/core/video/decode_worker.py
src/core/video/session.py
```

统一类型：

```python
VideoFrameLike = DecodedFrame | GpuDecodedFrame
```

要求：

- `DecodeWorker` 可以 enqueue `GpuDecodedFrame`。
- `VideoSession.tick()` 可以返回 `VideoFrameLike`。
- `VideoPlaybackEngine._submit_decoded_frame_playback()` 分流：

```text
GpuDecodedFrame -> RENDER_VIDEO_FRAME payload: {"gpu_decoded_frame": frame}
DecodedFrame planar -> CPU planes upload fallback
DecodedFrame packed -> packed texture upload fallback
```

验收：

```bash
.venv/bin/python -m pytest \
  tests/media/test_video_frame_queue.py \
  tests/media/test_video_session_shared_provider_real.py \
  tests/media/test_video_playback_no_ui_copy.py \
  -q --tb=short
```

---

## Phase C：macOS VideoToolbox/IOSurface 零拷贝独立原型

目标：在不污染主工程的前提下，证明：

```text
compressed packet
  -> VTDecompressionSession
  -> CVPixelBuffer with IOSurface
  -> OpenGL plane texture
  -> YUV shader
  -> RGB FBO
```

### C1. 新建原型目录

新增：

```text
src/core/media/macos_vt/
  __init__.py
  packet_demux.py
  vt_session.py
  iosurface_gl_bridge.py
  external_texture.py
  vt_zero_copy_probe.py
  capability.py
```

职责：

| 文件 | 职责 |
|---|---|
| `packet_demux.py` | 用 PyAV/FFmpeg 只 demux 压缩 packet，不解码到 ndarray |
| `vt_session.py` | 封装 `VTDecompressionSession` |
| `iosurface_gl_bridge.py` | `CVPixelBuffer/IOSurface -> OpenGL texture id` |
| `external_texture.py` | 最小 external texture wrapper |
| `vt_zero_copy_probe.py` | 独立 probe、benchmark、颜色自检 |
| `capability.py` | 缓存 probe 结果，给主工程 gate 使用 |

### C2. packet demux

`packet_demux.py` 要求：

- 用 PyAV 打开容器。
- 只读取压缩 packet。
- 不调用 `container.decode()`。
- 不调用 `packet.decode()`。
- 输出 codec extradata、time_base、pts/dts、packet bytes。

伪代码：

```python
class CompressedPacket:
    data: bytes
    pts: int | None
    dts: int | None
    duration: int | None
    is_keyframe: bool

class PacketDemuxer:
    def open(path: str) -> VideoStreamInfo
    def packets(self) -> Iterator[CompressedPacket]
```

验收：

```bash
.venv/bin/python -m pytest tests/media/test_macos_vt_packet_demux.py -q --tb=short
```

测试必须断言：

```text
packet.decode 不被调用
frame.to_ndarray 不被调用
能读到 extradata / codec / width / height / fps
```

### C3. VTDecompressionSession

`vt_session.py` 要求：

- 先用 PyObjC 实现，后续可替换为 Objective-C/Swift 扩展。
- 支持 H.264 / HEVC。
- 输出 `CVPixelBufferRef`。
- pixel buffer attributes 必须包含 IOSurface backing。

关键设置：

```text
kCVPixelBufferPixelFormatTypeKey:
  kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange  # NV12 limited
  kCVPixelFormatType_420YpCbCr8BiPlanarFullRange   # NV12 full
  kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange # P010, if available

kCVPixelBufferIOSurfacePropertiesKey: {}
kCVPixelBufferOpenGLCompatibilityKey: True
```

输出要求：

```python
class VtDecodedPixelBuffer:
    pixel_buffer: object
    pts: int | None
    time_seconds: float
    width: int
    height: int
    pixel_format: str  # nv12/p010
    bit_depth: int
    color_metadata: dict
```

验收：

```bash
.venv/bin/python src/core/media/macos_vt/vt_zero_copy_probe.py \
  --file "/path/to/4k_h264.mp4" \
  --frames 30 \
  --stage decode-only \
  --output-json tests/media/results/macos_vt_decode_probe.json
```

JSON 必须包含：

```json
{
  "vt_session_created": true,
  "decoded_pixel_buffers": 30,
  "cvpixelbuffer_iosurface_backed": true,
  "pixel_format": "nv12",
  "cpu_download_frames": 0
}
```

### C4. IOSurface -> OpenGL texture bridge

`iosurface_gl_bridge.py` 优先使用：

```text
CVPixelBufferGetIOSurface(pixel_buffer)
CGLTexImageIOSurface2D(context, target, internalFormat, width, height, format, type, iosurface, plane)
```

plane 映射：

| 源格式 | Plane | GL internal format | GL format | GL type | shader sampler |
|---|---:|---|---|---|---|
| NV12 8-bit | 0 Y | `GL_R8` | `GL_RED` | `GL_UNSIGNED_BYTE` | `sampler2D` |
| NV12 8-bit | 1 UV | `GL_RG8` | `GL_RG` | `GL_UNSIGNED_BYTE` | `sampler2D` |
| P010 10-bit | 0 Y | `GL_R16UI` 或 `GL_R16` | `GL_RED_INTEGER` 或 `GL_RED` | `GL_UNSIGNED_SHORT` | `usampler2D` 或 `sampler2D` |
| P010 10-bit | 1 UV | `GL_RG16UI` 或 `GL_RG16` | `GL_RG_INTEGER` 或 `GL_RG` | `GL_UNSIGNED_SHORT` | `usampler2D` 或 `sampler2D` |

要求：

- 每帧不能创建无上限 texture；必须 ring buffer。
- 每个 `GpuDecodedFrame` 必须 retain `CVPixelBuffer` 到 GPU render 完成后。
- 如果 texture target 是 rectangle，shader 必须支持 rectangle 坐标，不能强行当 `sampler2D`。
- bridge 失败时返回明确 fallback reason。

验收：

```bash
.venv/bin/python src/core/media/macos_vt/vt_zero_copy_probe.py \
  --file "/path/to/4k_h264.mp4" \
  --frames 120 \
  --stage bridge-only \
  --output-json tests/media/results/macos_iosurface_gl_bridge_probe.json
```

JSON 必须包含：

```json
{
  "iosurface_present": true,
  "gl_texture_created": true,
  "texture_target": "2d",
  "planes": [
    {"component": "Y", "texture_id": 1, "width": 3840, "height": 2160},
    {"component": "UV", "texture_id": 2, "width": 1920, "height": 1080}
  ],
  "texture_write_frames": 0
}
```

### C5. external texture wrapper

`external_texture.py` 定义最小 wrapper：

```python
class ExternalGLTexture:
    def __init__(self, texture_id: int, target: int, size: tuple[int, int]):
        self.glo = texture_id
        self.target = target
        self.size = size

    def use(self, location: int = 0) -> None:
        # PyOpenGL glActiveTexture + glBindTexture
```

要求：

- 只实现 `.glo`, `.size`, `.use()`。
- 不拥有 texture 生命周期。
- 不在 `release()` 中删除 IOSurface 绑定的 texture，除非明确由 bridge 创建并管理。

### C6. 零拷贝 probe 完整渲染

`vt_zero_copy_probe.py` 完整模式：

```bash
.venv/bin/python src/core/media/macos_vt/vt_zero_copy_probe.py \
  --file "/path/to/4k_h264.mp4" \
  --frames 300 \
  --warmup 30 \
  --verify-color \
  --output-json tests/media/results/macos_vt_iosurface_zero_copy_probe.json
```

必须做：

```text
decode compressed packet
bind IOSurface planes as GL textures
YUV shader render to RGB FBO
默认不 readback
--verify-color 时只抽样 16 个点
```

验收 JSON：

```json
{
  "decode_path": "macos_vt_iosurface_zero_copy",
  "zero_copy": true,
  "frames_tested": 300,
  "cpu_download_frames": 0,
  "cpu_upload_frames": 0,
  "texture_write_frames": 0,
  "decode_ms": {"p95": 0},
  "external_bind_ms": {"p95": 0},
  "yuv_render_ms": {"p95": 0},
  "total_ms": {"p95": 0},
  "color_verification": {
    "passed": true,
    "max_abs_error": 0
  }
}
```

通过线：

| 指标 | 标准 |
|---|---:|
| `cpu_download_frames` | 0 |
| `texture_write_frames` | 0 |
| `external_bind_p95_ms` | < 1.5ms |
| `yuv_render_p95_ms` | < 2ms |
| 4K 30fps total p95 | < 33ms |
| 颜色 self-test | passed |

---

## Phase D：macOS 零拷贝接入主播放链路

目标：probe 通过后，再接入真实 Color 页面播放。

### D1. capability gate

`src/core/media/macos_vt/capability.py` 输出：

```python
@dataclass
class MacOSZeroCopyCapability:
    available: bool
    reason: str
    supported_codecs: set[str]
    supported_pixel_formats: set[str]
    gl_texture_target: str
    last_probe_json: str | None
```

主工程启用条件：

```text
platform.system() == "Darwin"
codec in {"h264", "hevc", "h265"}
probe.available == true
用户未禁用 zero-copy
当前 OpenGL context 与 probe 能力匹配
```

环境变量：

```text
TOPOS_VIDEO_ZERO_COPY=auto|off|force
TOPOS_VIDEO_ZERO_COPY_PROBE=1
TOPOS_VIDEO_FORCE_CPU_UPLOAD=1
```

规则：

- 默认 `auto`。
- `off` 必须完全禁用零拷贝。
- `force` 仅用于开发；失败仍然不能黑屏，必须 fallback。

### D2. MediaFactory 选择源

修改：

```text
src/core/media/factory.py
src/core/media/decode_policy.py
```

新增源：

```text
src/core/media/macos_vt/source.py
```

职责：

```text
MacOSVtZeroCopySource.open()
MacOSVtZeroCopySource.read_next_frame() -> GpuDecodedFrame
MacOSVtZeroCopySource.close()
```

factory 选择顺序：

```text
if preview and zero_copy_capability_ok:
    MacOSVtZeroCopySource
elif hwaccel allowed:
    FFmpegHwaccelSource(copy_to_cpu=True)
else:
    FFmpegMediaSource(preserve_planes=True)
```

日志：

```text
MediaFactory: selected decode_path=macos_vt_iosurface_zero_copy
MediaFactory: zero_copy unavailable, fallback_reason=...
```

测试：

```text
tests/media/test_macos_zero_copy_policy.py
```

验收：

```bash
.venv/bin/python -m pytest tests/media/test_macos_zero_copy_policy.py tests/media/test_v3_hwaccel_policy.py -q --tb=short
```

### D3. DecodeWorker 输出 `GpuDecodedFrame`

修改：

```text
src/core/video/decode_worker.py
src/core/video/frame_queue.py
src/core/video/session.py
```

要求：

- 队列可以保存 `DecodedFrame | GpuDecodedFrame`。
- old frame 被丢弃时必须调用 `GpuDecodedFrame.release.release()`。
- 被 renderer 接手后，release 由 renderer 负责，不由队列提前释放。

测试：

```text
tests/media/test_gpu_decoded_frame_queue_lifetime.py
```

必须覆盖：

```text
queue full drops old GPU frame -> release called once
renderer accepted GPU frame -> queue does not release early
session close releases all held GPU frames
seek clears stale GPU frames
```

### D4. VideoPlaybackEngine 分流

修改：

```text
src/core/video_playback.py
```

逻辑：

```python
if isinstance(frame, GpuDecodedFrame):
    renderer.submit(RenderCommand.RENDER_VIDEO_FRAME, {
        "gpu_decoded_frame": frame,
        "frame_index": frame.frame_index,
        "render_params": render_params,
        "generation_id": self._generation_id,
        "drop_if_stale": True,
        "quality": "proxy",
        "reason": "playback",
        "decode_path": frame.decode_path,
    })
elif frame.is_planar:
    # existing CPU planes upload fallback
else:
    # existing packed upload fallback
```

要求：

- UI tick 不能 bind texture。
- UI tick 不能 release GPU frame。
- UI tick 只做轻量 submit。

验收：

```bash
.venv/bin/python -m pytest tests/media/test_video_playback_no_ui_copy.py -q --tb=short
```

### D5. AsyncRenderer 直接采样 external planes

修改：

```text
src/core/async_renderer.py
src/core/yuv_gpu_upload.py
```

新增路径：

```text
_do_bind_gpu_decoded_frame()
_render_external_yuv_planes_to_rgb_texture()
```

规则：

```text
GpuDecodedFrame -> external planes -> YUV shader -> raw_view -> grading -> output
```

禁止：

```text
_do_upload_video_planes()
Texture.write()
np.ascontiguousarray()
```

渲染结束后：

```text
insert lightweight fence if available
release previous frame when safe
never release current frame before draw commands are submitted
```

如果 ModernGL 无法直接接管 external texture：

- 使用 `ExternalGLTexture.use()` 手动 bind。
- 仍然用 ModernGL shader/VAO draw。

测试：

```text
tests/media/test_async_renderer_gpu_decoded_frame.py
```

必须断言：

```text
GpuDecodedFrame path does not call Texture.write
GpuDecodedFrame path records frame_path=gpu_decoded_external_yuv
release callback is not called before render submit
stale GPU frame is released exactly once
fallback render path still works
```

### D6. HUD / 日志显示

修改：

```text
src/ui/playback_hud.py
src/core/media/performance_monitor.py
```

HUD 增加：

```text
decode_path
render_frame_path
zero_copy on/off
cpu_download_frames
cpu_upload_frames
external_texture_frames
external_bind_p95_ms
fallback_reason
```

验收：

真实播放 60 秒，HUD 能看到：

```text
Zero Copy: ON
Decode: macOS VT IOSurface
Frame: GPU external YUV
CPU Upload: 0
CPU Download: 0
```

---

## Phase E：Windows D3D11/OpenGL 零拷贝独立原型

目标：不要把 macOS IOSurface 方案硬套到 Windows。Windows 需要单独 probe。

### E1. 新建目录

```text
src/core/media/windows_hw/
  __init__.py
  d3d11_decoder.py
  d3d11_gl_bridge.py
  windows_zero_copy_probe.py
  capability.py
```

目标链路：

```text
compressed packet
  -> D3D11VA / NVDEC / QSV decode
  -> D3D11 texture
  -> WGL_NV_DX_interop or equivalent bridge
  -> OpenGL texture
  -> YUV shader
```

### E2. 推荐实现顺序

先实现 D3D11VA，因为它最通用：

```text
H.264/HEVC -> FFmpeg C API / small native extension -> AVHWFramesContext D3D11
```

如果 PyAV 无法暴露 D3D11 texture handle，不要硬绕。直接做小型 C++/Rust 扩展：

```text
topos_hwdecode_windows
  open(path)
  decode_next() -> D3D11 texture handle + metadata
  release(handle)
```

NVIDIA 后续可选：

```text
NVDEC/CUDA -> CUDA-OpenGL interop
```

### E3. Windows probe 验收

```powershell
.venv\Scripts\python.exe src\core\media\windows_hw\windows_zero_copy_probe.py `
  --file "D:\media\4k_h264.mp4" `
  --frames 300 `
  --warmup 30 `
  --verify-color `
  --output-json tests\media\results\windows_d3d11_gl_zero_copy_probe.json
```

必须输出：

```json
{
  "decode_path": "windows_d3d11_gl_zero_copy",
  "zero_copy": true,
  "d3d_adapter_matches_gl_gpu": true,
  "cpu_download_frames": 0,
  "cpu_upload_frames": 0,
  "texture_write_frames": 0,
  "external_bind_ms": {"p95": 0},
  "yuv_render_ms": {"p95": 0}
}
```

失败时必须说明：

```text
adapter_mismatch
unsupported_codec
interop_extension_missing
d3d11_texture_not_shareable
driver_rejected_registration
```

---

## Phase F：Windows 接入主链路

完成条件与 macOS 相同：

```text
MediaFactory -> WindowsZeroCopySource
DecodeWorker -> GpuDecodedFrame
AsyncRenderer -> external texture YUV shader
HUD -> windows_d3d11_gl_zero_copy
fallback -> hwaccel_download or CPU YUV upload
```

必须处理多 GPU：

| 场景 | 处理 |
|---|---|
| D3D decode 在 iGPU，OpenGL 在 dGPU | fallback，或提示用户切同一 GPU |
| NVIDIA Optimus | probe 检查 adapter LUID |
| AMD/Intel interop 不支持 | fallback |
| texture format 不可采样 | fallback 到 D3D shader 转 shared RGBA，再 GL 采样，或回 CPU upload |

---

## Phase G：ProRes / proxy / fallback 完善

ProRes 不要作为零拷贝第一目标。先保持稳定：

| 素材 | 近期策略 | 长期策略 |
|---|---|---|
| ProRes 422 10-bit | CPU decode + R16UI/P010 + proxy ladder | 单独评估 VT 是否可输出 IOSurface |
| ProRes 4444 12-bit | CPU decode + YUV444 R16UI + disk proxy | 若 VT/IOSurface 支持 444/alpha 再做 |
| ProRes with alpha | 播放 proxy 可忽略/简化 alpha，暂停/HQ 保真 | external alpha plane shader |

必须保留：

```text
R16UI fallback
R8/RG8 fallback
dynamic proxy ladder
disk proxy
seek/HQ master frame
```

零拷贝失败时 fallback 顺序：

```text
macOS/Windows zero-copy
  -> hwaccel download + packed or planar upload
  -> CPU planar YUV + R8/R16UI upload
  -> CPU packed RGB upload
  -> proxy lower resolution
  -> hold last frame / placeholder
```

要求：

- fallback 不黑屏。
- fallback 不阻塞 UI。
- fallback reason 进入 HUD 和 JSON。

---

## Phase H：最终验证脚本

新增：

```text
tests/media/verify_zero_copy_playback.py
```

命令：

```bash
.venv/bin/python tests/media/verify_zero_copy_playback.py \
  --file "/path/to/4k_h264.mp4" \
  --frames 300 \
  --warmup 30 \
  --require-zero-copy \
  --verify-color \
  --output-json tests/media/results/zero_copy_acceptance_h264.json
```

脚本必须检查：

```text
zero_copy == true
cpu_download_frames == 0
cpu_upload_frames == 0
texture_write_frames == 0
decode_path in allowed zero-copy paths
frame_path == gpu_decoded_external_yuv
total_p95 <= frame_budget
yuv_render_p95 < 2ms
ui_tick_p95 < 3ms
color_verification.passed == true
```

失败时 exit code 非 0，并输出明确原因。

也要支持 fallback 验证：

```bash
.venv/bin/python tests/media/verify_zero_copy_playback.py \
  --file "/path/to/prores_4444.mov" \
  --frames 120 \
  --allow-fallback \
  --output-json tests/media/results/prores_fallback_acceptance.json
```

---

## 3. 代码审查清单

每个阶段完成后，都要按这个清单审查。

### Critical

| 检查项 | 通过标准 |
|---|---|
| 零拷贝路径无 CPU download | 无 `to_ndarray()` / `transfer()` |
| 零拷贝路径无 CPU upload | 无 `Texture.write()` 写视频帧 |
| 生命周期安全 | GPU render 完成前不 release pixel buffer / D3D texture |
| fallback 安全 | 任意失败不黑屏、不崩溃 |
| OpenGL 线程安全 | 所有 GL bind/draw 在 renderer 线程 |
| 颜色正确 | range/matrix/bit_depth 正确传递 |
| 无每帧同步 | 播放路径无 `ctx.finish()` / `glFinish()` |

### Suggestion

| 检查项 | 目标 |
|---|---|
| probe 结果缓存 | 启动不反复慢 probe |
| HUD 可读 | 用户能看到当前路径 |
| benchmark 稳定 | 多次运行波动可解释 |
| 日志可定位 | fallback reason 明确 |

---

## 4. 推荐给 AI 的执行 Prompt

### Prompt 1：先做 Phase A

```text
请在 Topos Color 项目中实施 Phase A：冻结当前 fallback 基线和可观测性。

项目技术栈：
- PyQt6 + ModernGL + GLSL
- 视频播放链路：VideoPlaybackEngine / VideoSession / DecodeWorker / AsyncRenderer
- 当前已有 CPU->GPU YUV fallback、R8/R16UI 上传、PerformanceMonitor

严格约束：
1. 不实现零拷贝，只补可观测性和 frame path 命名。
2. 不破坏现有 CPU fallback 播放。
3. 不在 UI tick 做大数组转换。
4. 不引入每帧 readback / ctx.finish。
5. 遵循 .clinerules 和 AGENTS.md。

任务：
1. 新增 src/core/media/frame_path.py。
2. 为 PerformanceMonitor 增加 zero_copy/cpu_download/cpu_upload/external_texture 统计。
3. 让 hwaccel_download、CPU YUV upload、packed upload 都明确记录 frame path。
4. 扩展 benchmark JSON 字段。
5. 补测试。

验收：
.venv/bin/python -m pytest tests/media/test_performance_monitor.py tests/media/test_v3_hwaccel_policy.py tests/test_async_renderer_pass_count.py -q --tb=short
```

### Prompt 2：做 Phase B

```text
请实施 Phase B：定义统一 GPU resident frame 抽象。

任务：
1. 新增 src/core/media/gpu_frame.py，定义 GpuFramePlane / GpuFrameRelease / GpuDecodedFrame。
2. 让 VideoFrame 队列、VideoSession、VideoPlaybackEngine 的类型可以接受 DecodedFrame | GpuDecodedFrame。
3. GpuDecodedFrame 暂时只做数据结构和分流，不接真实 VideoToolbox。
4. 写生命周期测试，确保队列丢弃 GPU frame 时 release exactly once。

严格禁止：
- 不写 VideoToolbox 代码。
- 不动现有 CPU upload fallback 的行为。

验收：
.venv/bin/python -m pytest tests/media/test_gpu_frame.py tests/media/test_gpu_decoded_frame_queue_lifetime.py tests/media/test_video_frame_queue.py -q --tb=short
```

### Prompt 3：做 macOS probe

```text
请实施 Phase C：macOS VideoToolbox/IOSurface/OpenGL zero-copy 独立 probe。

任务：
1. 新建 src/core/media/macos_vt/。
2. packet_demux.py 只 demux compressed packet，不调用 decode/to_ndarray。
3. vt_session.py 用 PyObjC 创建 VTDecompressionSession，输出 IOSurface-backed CVPixelBuffer。
4. iosurface_gl_bridge.py 用 CVPixelBufferGetIOSurface + CGLTexImageIOSurface2D 生成 GL plane texture。
5. vt_zero_copy_probe.py 跑 decode + bridge + YUV shader render + optional color readback。
6. 输出 JSON，包含 cpu_download_frames=0、texture_write_frames=0。

验收：
.venv/bin/python src/core/media/macos_vt/vt_zero_copy_probe.py --file "/path/to/4k_h264.mp4" --frames 120 --warmup 10 --verify-color --output-json tests/media/results/macos_vt_iosurface_zero_copy_probe.json
```

### Prompt 4：接入主链路

```text
请在 Phase C probe 通过后实施 Phase D：将 macOS zero-copy 接入主播放链路。

任务：
1. 新增 MacOSVtZeroCopySource，read_next_frame() 返回 GpuDecodedFrame。
2. MediaFactory 在 preview + capability ok 时选择该 source。
3. DecodeWorker/VideoSession 传递 GpuDecodedFrame。
4. VideoPlaybackEngine 提交 gpu_decoded_frame 到 renderer。
5. AsyncRenderer 直接绑定 external Y/UV textures，跳过 _do_upload_video_planes。
6. HUD 显示 zero-copy 状态。

严格禁止：
- GpuDecodedFrame 路径中不能 Texture.write。
- GpuDecodedFrame 路径中不能 to_ndarray/transfer。
- 失败必须 fallback，不许黑屏。

验收：
.venv/bin/python -m pytest tests/media/test_macos_zero_copy_policy.py tests/media/test_async_renderer_gpu_decoded_frame.py tests/media/test_gpu_decoded_frame_queue_lifetime.py -q --tb=short
.venv/bin/python tests/media/verify_zero_copy_playback.py --file "/path/to/4k_h264.mp4" --frames 300 --require-zero-copy --verify-color
```

---

## 5. 最终交付物清单

完成整个计划后，仓库应包含：

```text
src/core/media/frame_path.py
src/core/media/gpu_frame.py
src/core/media/macos_vt/
src/core/media/windows_hw/              # Windows 阶段完成后
tests/media/test_gpu_frame.py
tests/media/test_gpu_decoded_frame_queue_lifetime.py
tests/media/test_macos_zero_copy_policy.py
tests/media/test_async_renderer_gpu_decoded_frame.py
tests/media/verify_zero_copy_playback.py
tests/media/results/*zero_copy*.json
```

真实播放日志必须能看到：

```text
Zero-copy capability: available
MediaFactory: selected decode_path=macos_vt_iosurface_zero_copy
VideoPlaybackEngine: submit gpu_decoded_frame frame=...
AsyncRenderer: frame_path=gpu_decoded_external_yuv
cpu_download_frames=0
cpu_upload_frames=0
texture_write_video_frames=0
external_texture_frames=...
yuv_render_p95_ms=...
```

只有达到这些日志、测试和 benchmark 标准，才可以宣布：

```text
GPU 解码 -> GPU 纹理/显存 -> GPU 调色 -> GPU 输出
```

已经真正实现。
