# 4K 播放实时预览优化计划

日期：2026-05-23  
适用范围：Color 页面视频播放、4K H.264/HEVC、4K ProRes、macOS GPU YUV、AsyncRenderer、VideoPlaybackEngine、DecodeWorker。  
最终目标：尽量接近达芬奇式链路：`GPU 解码 -> GPU 纹理/显存 -> GPU 调色 -> GPU 输出`，播放热路径不做 CPU YUV->RGB、不做每帧 readback、不做每帧 glFinish。

---

## 1. 对原计划的复查结论

原计划的大方向是对的：2K 能实时、4K 卡顿，通常不是调色 shader 本身，而是解码、格式准备、CPU/GPU 往返、上传带宽和调度共同造成。但原计划有几个必须修正的点。

| 问题 | 原计划写法 | 修正意见 |
|---|---|---|
| PyAV VideoToolbox + NV12 被写成近期核心方案 | 这不是零拷贝。PyAV 很可能仍把硬解结果下载到 CPU 内存，再传给 numpy。 | 可作为中间方案，但验收必须写清楚：只要出现 `to_ndarray()`、numpy planes、`Texture.write()`，就不是达芬奇式零拷贝。 |
| 只强调 4K H.264/HEVC 解码 | 你的常见素材包含 ProRes `yuv444p12le`，瓶颈可能是 12-bit plane 转 float16 的 `plane_prepare_ms`。 | H.264/HEVC 走硬解零拷贝；ProRes 先解决 R16UI/P010/R8 直传和代理缓存。两类素材不能用同一套结论。 |
| “ModernGL 直接使用外部 OpenGL 纹理”写得过于乐观 | ModernGL 不一定有稳定的 external texture wrapper；只拿到 GL texture id 还不等于能安全纳入现有 Texture 生命周期。 | 必须先做最小原型：外部 texture id 能否被 shader 采样、生命周期如何管理、是否需要 PyOpenGL 手动 bind。 |
| CVOpenGLTextureCache 方案写得不够具体 | planar NV12/P010 需要明确 plane 0/plane 1、texture target、format/type、UV 坐标、range/matrix。 | macOS OpenGL 方案优先验证 `CVPixelBufferGetIOSurface` + `CGLTexImageIOSurface2D(..., plane)`，必要时再试 `CVOpenGLTextureCacheCreateTextureFromImage()`。 |
| 没有足够强的验证闭环 | “播放确认流畅”不够。 | 必须有 benchmark JSON、日志路径、颜色 readback/self-test、真实 UI 播放验收和硬件能力检查。 |
| FBO dtype 改成 `f1` 有风险 | 播放模式全链路降到 8-bit 可能破坏调色/HDR/Raw preview。 | 只允许在最终预览输出或明确 SDR proxy 上使用 `f1`；内部 Raw/调色 FBO 默认继续 `f2`，除非有像素对比测试证明无可见损失。 |
| 动态 QTimer 间隔容易治标不治本 | 仅靠 timer 追赶会导致跳帧抖动。 | 必须以媒体时钟为准，允许丢过期帧，禁止倒退显示旧帧。 |

---

## 2. 当前项目的已知状态

根据当前代码复查，项目已经具备一些基础，但还没有达到真正的 4K 零拷贝。

| 模块 | 当前状态 | 风险 |
|---|---|---|
| `src/core/video_playback.py` | 播放 tick 可提交 `DecodedFrame`，planar frame 可进入 GPU YUV 路径。macOS 下 `should_use_yuv_gpu_upload()` 受 self-test gate 保护。 | self-test 通过不等于 4K 实时；真实素材还要看 decode、prepare、write、render 分段耗时。 |
| `src/core/async_renderer.py` | `video_render_sync_mode` 默认是 `none`，避免每帧阻塞 GPU；YUV planes 上传和 R16UI 路径已有接入点。 | `_drain_pending_slots()` 当前仍是 `GRADING_REFRESH -> VIDEO_PLAYBACK -> VIEWPORT_REFRESH`，播放中调色可能抢占视频帧，需要结合 viewport 刷新语义谨慎调整。 |
| `src/core/yuv_gpu_upload.py` | 已有 float16 YUV 路径、R16UI/usampler2D 路径、自检和 `source_uv_scale/source_uv_offset`。 | 8-bit 仍可能走 float16 prepare；R16UI 只解决上传准备，不解决 CPU 解码。 |
| `tests/media/benchmark_yuv_gpu_pipeline.py` | 已能测 `decode_ms / plane_prepare_ms / texture_write_ms / render_ms`。 | 还需要扩展到真实播放链路、外部 GPU texture 路径和长期回归阈值。 |

历史 benchmark 结论要保留：ProRes `3840x2160 yuv444p12le` 在 float16 路径下，1080p proxy 的 `plane_prepare_ms` 曾达到几十毫秒级，shader render 反而很低。这说明 4K ProRes 卡顿不一定靠 VideoToolbox 解决，必须先消灭 Python/numpy 的大数组格式转换。

---

## 3. 性能目标

### 3.1 播放预算

| 场景 | 帧预算 | 最低目标 | 理想目标 |
|---|---:|---:|---:|
| 4K 25fps ProRes proxy 预览 | 40.0ms | p95 < 40ms | p95 < 28ms |
| 4K 30fps H.264/HEVC 预览 | 33.3ms | p95 < 33ms | p95 < 20ms |
| 4K 60fps H.264/HEVC 预览 | 16.7ms | 可自适应降级 | p95 < 16ms |
| UI tick | 不应占满帧预算 | p95 < 8ms | p95 < 3ms |
| YUV render shader | GPU pass | p95 < 2ms | p95 < 1ms |
| 每帧 CPU readback | 播放热路径 | 0 次 | 0 次 |

### 3.2 分段耗时目标

| 阶段 | 当前常见风险 | 近期目标 | 零拷贝目标 |
|---|---|---:|---:|
| demux | 通常很低 | < 2ms | < 2ms |
| decode | 4K GOP CPU 软解会爆 | H.264/HEVC < 8ms，ProRes proxy < 20ms | GPU decode callback 不阻塞 UI |
| plane_prepare | 10/12-bit 转 float16 很慢 | 8/10/12-bit < 3ms | 0ms 或近似 0ms |
| texture_write | CPU->GPU 上传 | 1080p proxy < 4ms | 0ms |
| yuv_render | GPU YUV->RGB | < 2ms | < 2ms |
| grading/render | 复杂效果另算 | proxy < 8ms | proxy < 8ms |

---

## 4. 实施前检查清单

执行任何优化前，先收集这些信息，否则很容易误判瓶颈。

### 4.1 素材与系统检查

```bash
ffprobe -hide_banner -show_streams -select_streams v:0 "/Users/testuser/Desktop/调色补充.mov"
ffmpeg -hide_banner -hwaccels
system_profiler SPDisplaysDataType
```

必须记录：

| 项目 | 要记录的值 |
|---|---|
| codec | h264、hevc、prores、dnxhr 等 |
| pix_fmt | yuv420p、nv12、p010、yuv422p10le、yuv444p12le 等 |
| width/height/fps | 原始尺寸和帧率 |
| color_range/color_matrix/color_primaries/transfer | limited/full、BT.709/BT.2020、PQ/HLG |
| macOS GPU | Apple Silicon、Intel iGPU、AMD 6900XT、黑苹果驱动状态 |
| OpenGL 能力 | macOS 通常只有 OpenGL 4.1，不支持 compute shader 是正常现象 |

### 4.2 当前播放路径检查

运行软件播放 30 秒，并打开性能日志，必须能看到：

```text
playback_frame_path=...
decode_ms avg/p95=...
plane_prepare_ms avg/p95=...
texture_write_ms avg/p95=...
yuv_render_ms avg/p95=...
render_submit_ms avg/p95=...
ui_tick_ms avg/p95=...
queue_depth=...
dropped_frames=...
coalesced_video_commands=...
```

如果日志还不能输出这些字段，先补可观测性，不要盲改架构。

### 4.3 禁止做的验证

| 禁止项 | 原因 |
|---|---|
| 只用肉眼说“好像顺了” | 不能定位 decode、prepare、upload 哪个环节变快。 |
| 每帧 `read()` 或截图检测 | readback 会强制同步 GPU，直接把播放测慢。 |
| 用单帧 seek 结果代表播放 | seek/HQ 路径和 playback 路径不同。 |
| 只测 10 帧 | shader 编译、缓存预热、磁盘缓存都会污染结果。 |
| 只测 ProRes 或只测 H.264 | 两类瓶颈完全不同。 |

---

## 5. 分阶段优化计划

### Phase 0：建立可信 benchmark 和回归门槛

目标：先知道 4K 卡在哪里，再开始改。

| 步骤 | 任务 | 文件 | 验收 |
|---|---|---|---|
| P0.1 | 扩展 `benchmark_yuv_gpu_pipeline.py` 输出 p50/p95/max、有效 FPS、掉帧、路径名 | `tests/media/benchmark_yuv_gpu_pipeline.py` | JSON 中包含 `decode_ms/prepare_ms/write_ms/yuv_render_ms/total_ms/effective_fps/upload_mode/pixel_format` |
| P0.2 | 新增真实播放链路 benchmark，不只测离线 upload/render | 新增 `tests/media/benchmark_playback_runtime.py` | 可模拟 120 帧播放，输出 queue depth、coalesced、dropped、late frames |
| P0.3 | 增加颜色验证开关 | benchmark | `--verify-color` 时抽样 readback 16 个点；默认不 readback |
| P0.4 | 写入基线结果 | `tests/media/results/` | 至少有 4K H.264、4K HEVC、4K ProRes 结果 |

建议命令：

```bash
.venv/bin/python tests/media/benchmark_yuv_gpu_pipeline.py \
  --file "/Users/testuser/Desktop/调色补充.mov" \
  --sizes 1920x1080,1280x720,960x540 \
  --frames 120 \
  --warmup 10 \
  --mode auto \
  --verify-color \
  --output-json tests/media/results/4k_prores_yuv_gpu_baseline.json
```

验收线：

| 素材 | 通过线 |
|---|---|
| 4K ProRes 25fps | 至少 960x540 p95 < 40ms；720p 尽量 p95 < 40ms |
| 4K H.264/HEVC 30fps | 1080p proxy p95 < 33ms，或者自动降到 720p |
| UI 播放 | 无前后跳帧、无黑屏、无尺寸突然缩小 |

### Phase 1：现有 CPU->GPU 路径先做到“稳定可用”

目标：不等真正零拷贝，也先让 4K 预览有稳定 fallback。这个阶段对 ProRes 尤其重要。

| 步骤 | 任务 | 文件 | 验收 |
|---|---|---|---|
| P1.1 | 完成并锁定 R16UI/usampler2D 路径 | `src/core/yuv_gpu_upload.py`, `src/core/async_renderer.py` | 10/12-bit planar 不再走大规模 float16 normalize，`plane_prepare_p95 < 3ms` |
| P1.2 | 给 8-bit YUV420/NV12 增加 R8/RG8 uint8 直传 | `src/core/yuv_gpu_upload.py` | 8-bit 不再转 float16；`upload_mode=r8_direct` 或 `nv12_r8_rg8` |
| P1.3 | P010 使用 R16UI/R16 路径，不转 float16 | `src/core/yuv_gpu_upload.py` | 10-bit NV12/P010 颜色 self-test 通过 |
| P1.4 | CPU fallback 上传使用 PBO 或至少双缓冲 staging | `src/core/video_texture_pool.py`, `src/core/async_renderer.py` | `Texture.write()` 不阻塞 UI/render 主循环；无每帧大分配 |
| P1.5 | 播放帧调度只显示最新到期帧，禁止倒退显示旧帧 | `src/core/video_playback.py`, `src/core/video/playback_state.py` | 播放不再前后跳帧；late frame 只丢弃，不回放 |
| P1.6 | 调整 lane 优先级但保留 viewport 最终发布 | `src/core/async_renderer.py` | 播放中调色不阻塞视频帧；滚轮缩放最终画面不被旧 visible_rect 覆盖 |

P1.6 建议不要简单把 `VIDEO_PLAYBACK` 放到所有项最前面。更稳的策略：

```text
AUX_TEX -> VIDEO_PLAYBACK -> GRADING_REFRESH -> VIEWPORT_REFRESH -> COLOR_OUTPUT
```

或者播放中将 grading 只更新参数，合并到下一帧 video render，不提交完整 `GRADING_REFRESH`。最终必须保证 `VIEWPORT_REFRESH` 在 video frame 后执行，防止缩放状态被旧帧覆盖。

### Phase 2：自适应代理和磁盘代理

目标：遇到硬件、编码或素材不可实时的情况时，像剪辑软件一样自动降级，而不是硬扛 4K。

| 步骤 | 任务 | 文件 | 验收 |
|---|---|---|---|
| P2.1 | 动态 proxy ladder | `src/core/video_playback.py` | 1920 -> 1280 -> 960 -> 640，性能恢复后逐级回升 |
| P2.2 | 降级依据从 UI tick 改为综合指标 | `src/core/media/performance_monitor.py` | 根据 `decode_p95 + prepare_p95 + write_p95 + render_p95 + late_frames` 决策 |
| P2.3 | 后台生成磁盘代理 | `src/core/video/frame_provider_service.py` 或新模块 | 4K GOP 素材可生成 1080p/720p ProRes Proxy 或 H.264 intra proxy |
| P2.4 | 播放中用 proxy，暂停/seek/HQ 用原始帧 | `src/core/video_playback.py` | 播放流畅，暂停后画质恢复 |
| P2.5 | UI 显示当前播放质量 | HUD/状态栏 | 能看到 `Preview 720p proxy`、`Original paused` |

降级阈值建议：

| 条件 | 动作 |
|---|---|
| `total_p95 > frame_budget * 1.25` 连续 2 秒 | 降一级 proxy |
| `late_frames > 5` 连续 2 秒 | 降一级 proxy |
| `decode_queue_empty` 连续出现 | 增加预读或降 proxy |
| `total_p95 < frame_budget * 0.65` 连续 5 秒 | 升一级 proxy |

### Phase 3：macOS 真零拷贝原型

目标：先在独立原型中证明 `VideoToolbox -> CVPixelBuffer/IOSurface -> OpenGL texture -> shader` 可行，再接入主工程。

这一步是大工程，不建议直接在 `AsyncRenderer` 里开改。

#### 3.1 原型目标

新建最小原型模块：

```text
src/core/media/macos_vt/
  videotoolbox_demux.py        # 用 PyAV 或 ffmpeg 只做 demux，拿压缩 packet
  vt_decompression.py          # VTDecompressionSession 封装
  iosurface_gl_bridge.py       # CVPixelBuffer/IOSurface -> OpenGL plane textures
  vt_zero_copy_probe.py        # 独立 self-test/benchmark
```

原型只做三件事：

```text
压缩 packet -> VTDecompressionSession -> CVPixelBuffer
CVPixelBuffer -> IOSurface plane 0/1 -> OpenGL texture id
OpenGL shader 采样 Y/UV -> RGB FBO -> 少量 readback 验证颜色
```

验收：

| 验收项 | 标准 |
|---|---|
| 无 numpy frame | 解码输出路径不出现 `to_ndarray()`、`np.array(frame)`、CPU YUV planes |
| 无 CPU 上传 | 播放热路径不调用 `Texture.write()` 写入视频帧 |
| 可采样 | shader 能采样外部 Y/UV texture，画面非黑、非绿、非蓝脸 |
| 生命周期安全 | CVPixelBuffer 在 GPU render 完成前不会释放 |
| 性能可信 | 120 帧 benchmark 中 decode callback + render p95 达标 |

#### 3.2 推荐技术路径

优先路径：

```text
PyAV/FFmpeg demux compressed packet
  -> VTDecompressionSessionDecodeFrame
  -> CVPixelBufferRef with IOSurface backing
  -> CVPixelBufferGetIOSurface
  -> CGLTexImageIOSurface2D for each plane
  -> external GL texture ids
  -> YUV shader
  -> existing grading pipeline
```

注意事项：

| 坑 | 解决方式 |
|---|---|
| CVPixelBuffer 没有 IOSurface backing | 创建 VT session 时设置 `kCVPixelBufferIOSurfacePropertiesKey` 为空 dict。 |
| NV12/P010 是双平面，不是 RGB | plane 0 作为 Y，plane 1 作为 UV；shader 负责 matrix/range。 |
| OpenGL texture target 可能不是 `GL_TEXTURE_2D` | 原型必须记录 `target/internalFormat/format/type`；如果是 rectangle texture，shader 坐标要改。 |
| ModernGL 不能包装外部 texture | 先用 PyOpenGL 手动 `glActiveTexture/glBindTexture` 绑定外部 id，再用 ModernGL program/VAO draw；必要时写一个 `ExternalGLTexture` 适配对象，只实现 `.glo/.size/.use()`。 |
| 每帧创建/销毁 texture cache 太慢 | texture cache 和 GL texture 对象做 ring buffer，CVPixelBuffer retain/release 跟 fence 绑定。 |
| GPU 同步处理不好会黑屏或撕裂 | 不使用每帧 `glFinish()`；用 3 到 5 帧 ring buffer，必要时 `glFenceSync` 检查上一轮资源是否可释放。 |
| 色彩发灰或偏色 | 对每帧 metadata 传递 `color_range/color_matrix/transfer`，不要默认全 BT.709 limited。 |
| 垂直翻转 | 不在 YUV shader 里盲目 `1.0 - y`；先确认 IOSurface plane 的原点和最终 display pass 的原点，只在一个统一位置处理。 |

#### 3.3 PyObjC 与扩展方案选择

| 方案 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| PyObjC 直接调 VideoToolbox/CoreVideo/CGL | 快速验证，不需要编译 | callback、指针、CF 生命周期复杂 | 用于 Phase 3 原型 |
| Objective-C/Swift 小扩展 | 生命周期可控，性能更稳 | 需要编译和打包 | 原型通过后转正式实现 |
| Rust + objc2/core-video | 类型更安全 | 初期成本高 | 后续可选 |
| 继续 PyAV hwaccel | 改动小 | 大概率不是零拷贝 | 只作为中间 fallback |

### Phase 4：接入主播放链路

目标：原型通过后再接入 Color 页面播放。

| 步骤 | 任务 | 文件 | 验收 |
|---|---|---|---|
| P4.1 | 新增 `GpuDecodedFrame` 数据结构 | `src/core/media/frame.py` 或新文件 | 表达 texture ids、plane formats、source size、metadata、release handle |
| P4.2 | DecodeWorker 输出 `GpuDecodedFrame` | `src/core/video/decode_worker.py` | H.264/HEVC macOS 可选择 GPU frame queue |
| P4.3 | `RENDER_VIDEO_FRAME` 支持 external planes | `src/core/async_renderer.py` | 跳过 `_do_upload_video_planes()`，直接 YUV shader 采样 |
| P4.4 | 生命周期 ring buffer | 新模块或 renderer | 不提前 release CVPixelBuffer；无显存泄漏 |
| P4.5 | fallback 策略 | `src/core/media/factory.py`, `src/core/video_playback.py` | 任何一步失败自动回到 P1/P2 路径，不黑屏 |
| P4.6 | UI/HUD 显示 frame path | HUD/日志 | `macos_vt_iosurface_zero_copy` 明确可见 |

正式接入时的播放路径：

```text
H.264/HEVC compressed packets
  -> VideoToolbox GPU decode
  -> GpuDecodedFrame(NV12/P010 IOSurface planes)
  -> AsyncRenderer external YUV shader
  -> Raw View / Grading / Output
```

ProRes 的策略：

| ProRes 类型 | 近期策略 | 远期策略 |
|---|---|---|
| ProRes 422 10-bit | CPU decode + R16UI/P010 直传 + proxy ladder | 评估 VT 是否稳定输出可采样 CVPixelBuffer |
| ProRes 4444 12-bit | CPU decode + R16UI YUV444 直传 + 磁盘代理 | 若 VT/IOSurface 支持 444/alpha，再接零拷贝 |
| 带 alpha | 暂停/HQ 保真，播放可降级 | 单独设计 alpha plane shader |

### Phase 5：Windows 与跨平台规划

项目是多平台软件，所以优化路线必须同时覆盖 Windows。关键原则：上层播放、调色、proxy、benchmark、fallback 策略尽量统一；底层 GPU decode/texture bridge 按平台分别实现。Windows 不要照搬 macOS IOSurface，macOS 也不要照搬 Windows D3D11。

#### 5.1 跨平台分层目标

建议把“硬解到 GPU 纹理”抽象成统一接口，避免 Color 页面到处写平台分支：

```text
GpuDecodedFrame
  plane_textures: Y/UV or RGB texture handles
  api: "opengl" / "metal" / "d3d11" / "cuda" / "cpu_upload"
  pixel_format: nv12 / p010 / yuv420p / rgba / bgra
  color_metadata: range, matrix, transfer, primaries
  source_size / visible_rect
  release_fence_or_callback
```

上层只关心：

```text
DecodedFrame(cpu planes or packed)
GpuDecodedFrame(external GPU planes)
```

渲染器只分两类路径：

```text
CPU frame    -> Texture.write/R16UI/R8/PBO -> GPU YUV shader -> grading
GPU frame    -> external texture bridge     -> GPU YUV shader -> grading
```

#### 5.2 Windows 近期方案：先把硬解下载路径做稳

当前项目已有 `HardwareDecoder.D3D11VA/NVDEC/QSV`、`FFmpegHwaccelSource` 和 `hwaccel_download` 状态。近期 Windows 可以先做到：

| 目标 | 做法 | 验收 |
|---|---|---|
| H.264/HEVC 使用硬解 | Windows 默认优先 `d3d11va`，NVIDIA 可选 `nvdec/cuda`，Intel 可选 `qsv` | `decode_ms` 明显低于 CPU 软解 |
| 硬解下载后不再转 RGB | 尽量保留 NV12/P010 CPU planes，再走 R8/RG8/R16UI 上传 | 不出现 packed RGB 大转换 |
| 上传路径统一 | Windows 与 macOS 共用 `YUVGPUUploader` 的 R8/R16UI/P010/PBO | `plane_prepare_p95 < 3ms` |
| fallback 清楚 | 硬解失败自动回 CPU decode + proxy，不黑屏 | 日志记录 `hwaccel_fallback_reason` |

注意：这个阶段仍不是零拷贝，因为 D3D11/NVDEC 解码结果仍可能下载到 CPU。但它能先解决大部分 4K H.264/HEVC 软解瓶颈，并复用 macOS 的 YUV 上传优化。

#### 5.3 Windows 真零拷贝路线

Windows 真正的目标链路：

```text
Compressed packets
  -> D3D11VA / NVDEC / QSV hardware decode
  -> D3D11 texture or CUDA surface
  -> OpenGL interop texture
  -> YUV shader
  -> ModernGL grading/output
```

按硬件分三条路线：

| Windows 硬件 | 推荐 decode | 推荐 texture bridge | 说明 |
|---|---|---|---|
| NVIDIA | D3D11VA 优先，NVDEC/CUDA 可选 | `WGL_NV_DX_interop` for D3D11 texture；或 CUDA-OpenGL interop | D3D11VA 更通用；CUDA interop 性能强但维护成本高。 |
| Intel | D3D11VA 或 QSV | D3D11 texture -> OpenGL interop | QSV 最终也要落到可共享 texture，优先 D3D11 统一。 |
| AMD | D3D11VA | D3D11 texture -> OpenGL interop | 不依赖 CUDA；重点测试驱动兼容性。 |

Windows 零拷贝原型建议新建：

```text
src/core/media/windows_hw/
  d3d11_decoder.py             # FFmpeg d3d11va 或原生 Media Foundation/D3D11 decoder
  d3d11_gl_bridge.py           # D3D11 texture -> OpenGL texture interop
  windows_zero_copy_probe.py   # 独立 self-test/benchmark
```

原型验收和 macOS 一样严格：

| 验收项 | Windows 标准 |
|---|---|
| 无 CPU download | 不出现 `av_hwframe_transfer_data`、`to_ndarray()`、numpy video planes |
| 无 CPU upload | 视频帧路径不调用 `Texture.write()` |
| 外部 texture 可采样 | OpenGL shader 能采样 D3D11 interop texture |
| 生命周期安全 | D3D11 texture 在 GL 使用结束前不会释放/复用 |
| fallback 安全 | interop fail 自动回到 `hwaccel_download` 或 CPU proxy |

#### 5.4 Windows 零拷贝的主要坑

| 坑 | 表现 | 解决方式 |
|---|---|---|
| FFmpeg PyAV 很难直接暴露 D3D11 texture handle | 看起来启用了 hwaccel，但只能拿 CPU frame | 原型阶段可能需要 C/C++/Rust 小扩展，或用 FFmpeg C API 封装。 |
| OpenGL context 与 D3D device 不匹配 | interop 注册失败或黑屏 | D3D11 device 必须和当前窗口/GPU 匹配；多显卡机器要锁定同一 adapter。 |
| NVIDIA Optimus/混合显卡 | 解码在 iGPU，OpenGL 在 dGPU，无法共享 | 启动时检测 GL renderer 与 D3D adapter，不一致则 fallback 或提示用户。 |
| texture format 不可直接采样 | NV12/P010 plane 无法按普通 RGBA 采样 | shader 需要专用 NV12/P010 plane 采样；必要时 D3D shader 先转换到 shared RGBA texture。 |
| 同步过度 | 每帧等待 GPU 导致卡顿 | 使用 ring buffer + fence，禁止每帧全局 flush/finish。 |
| 驱动差异 | AMD/Intel/NVIDIA 行为不同 | Windows probe 必须按 GPU vendor 输出 capability，不通过就 fallback。 |

#### 5.5 是否应该迁移渲染后端

当前项目用 PyQt + ModernGL/OpenGL，所以短期最现实的是：

```text
macOS: VideoToolbox/IOSurface -> OpenGL bridge
Windows: D3D11 texture -> OpenGL interop
```

但长期要考虑两个方向：

> **状态更新（2026-09，v2 计划 P9D-02）**：本节的方向抉择已由
> `docs/metal_backend_migration_and_cross_platform_gpu_plan.md`（v2）
> **接续并取代**：采用「新增跨平台 GPU 后端抽象」方向（四维
> graphics/compute/decode/interop 契约），但**修正了 v1 的排序错误**——
> Metal compute 能力解锁（Normalize 百分位在 macOS 静默失效，§0.4）
> 提前为第一收益并已完成（Phase 4）；视频零拷贝后移并条件化（Phase 5）；
> Qt RHI/Vulkan 统一路线经 probe 复核后否决（v2 计划 §1.3、ADR 0003）。

| 方向 | 优点 | 代价 |
|---|---|---|
| 保留 OpenGL，写平台 bridge | 改动较小，继续复用现有 shader/renderer | macOS OpenGL 已弃用；Windows interop 复杂。 |
| 新增跨平台 GPU 后端抽象 | 可逐步迁移 Metal/D3D11/Vulkan | 工程量大，需要 shader 体系迁移。 |
| 用 Qt RHI/QRhi 或 Vulkan/Metal/D3D 统一 | 更现代，长期潜力大 | 会影响当前 ModernGL 调色管线，不适合作为眼前止血。 |

建议顺序：先完成 P0/P1/P2，让 4K 播放有稳定 fallback；然后 macOS 和 Windows 分别做零拷贝 probe；两个 probe 都成功后，再决定是否做正式 `GpuDecodedFrame` 接口和后端抽象。

#### 5.6 跨平台测试矩阵

| 平台 | 必测 GPU | 必测素材 | 必测路径 |
|---|---|---|---|
| macOS Apple Silicon | M1-M5 | 4K H.264/HEVC、ProRes 422/4444 | VideoToolbox、CPU/R16UI fallback |
| macOS Intel/AMD/黑苹果 | AMD 6900XT 等 | 4K H.264/HEVC、ProRes | VideoToolbox gate、IOSurface probe、fallback |
| Windows NVIDIA | RTX/GTX | 4K H.264/HEVC 8/10-bit | D3D11VA、NVDEC/CUDA 可选、interop probe |
| Windows Intel | UHD/Iris/Xe | 4K H.264/HEVC | D3D11VA/QSV、adapter match |
| Windows AMD | Radeon | 4K H.264/HEVC | D3D11VA、interop probe |
| Linux 可选 | Intel/AMD/NVIDIA | 基础 H.264/HEVC | VAAPI/NVDEC fallback，零拷贝后续再做 |

Windows 验证命令建议补充：

```powershell
ffmpeg -hide_banner -hwaccels
ffmpeg -hide_banner -decoders | findstr /i "h264 hevc av1 d3d11va qsv cuvid"
python tests/media/benchmark_playback_runtime.py `
  --file "D:\media\4k_h264.mp4" `
  --frames 300 `
  --warmup 30 `
  --modes cpu_yuv,hwaccel_download,gpu_yuv,windows_zero_copy `
  --output-json tests/media/results/windows_4k_h264_playback_runtime.json
```

跨平台最终验收：

| 验收项 | macOS | Windows |
|---|---|---|
| 4K H.264/HEVC 30fps | 1080p proxy p95 < 33ms | 1080p proxy p95 < 33ms |
| 硬解 fallback | VideoToolbox fail 不黑屏 | D3D11VA/NVDEC/QSV fail 不黑屏 |
| 零拷贝判定 | 无 numpy、无 `Texture.write()` | 无 hwframe download、无 `Texture.write()` |
| 颜色一致 | seek/playback/zero-copy 对齐 | seek/playback/zero-copy 对齐 |
| 多 GPU | 黑苹果/外接 GPU 有 gate | iGPU/dGPU adapter mismatch 有 gate |

本项目短期建议：不要同时把 macOS、Windows、Linux 的零拷贝全接进主工程。正确顺序是先统一 benchmark、frame path 日志、YUV 上传优化和 proxy fallback，再分别做 macOS/Windows 独立 probe，最后用统一 `GpuDecodedFrame` 接口接入。

---

## 6. 黑苹果 6900XT 与 Apple Silicon 的结论

不要求必须是 M1-M5。理论上，Intel Mac + AMD dGPU、黑苹果 + 6900XT 都可能使用 VideoToolbox 硬解和 OpenGL texture 采样。但你这台机器有几个现实风险。

| 项目 | 对 6900XT 黑苹果的影响 |
|---|---|
| VideoToolbox 是否硬件加速 | 取决于 macOS 版本、SMBIOS、WhateverGreen、驱动注入和 codec。必须实际测，不能假设。 |
| OpenGL 版本 | macOS 上 AMD 也通常是 OpenGL 4.1，所以 compute shader 不可用是正常限制。YUV fragment shader 不受这个限制。 |
| CVPixelBuffer/IOSurface -> OpenGL | 原生 API 应可用，但黑苹果驱动组合可能出现 texture target、同步、颜色格式异常。必须用原型自检 gate。 |
| H.264/HEVC 10-bit | 6900XT 硬件支持不等于 VideoToolbox 一定暴露给当前系统。 |
| ProRes | ProRes 不一定通过 GPU decode 获得收益；12-bit plane prepare 和 proxy 才是更现实的收益点。 |

判断是否真的硬解，不看口头结论，看数据：

| 检查 | 通过标准 |
|---|---|
| FFmpeg/VideoToolbox 解码速度 | 4K H.264/HEVC 解码 p95 明显低于 CPU 软解 |
| CPU 占用 | GPU 路径播放时 CPU 占用明显下降 |
| 日志路径 | `frame_path=macos_vt_iosurface_zero_copy` 或至少 `videotoolbox_nv12` |
| 无 CPU upload | 该路径中 `texture_write_ms` 对视频帧为 0 或不存在 |

Apple Silicon 的优势是 VideoToolbox/Metal/CVMetalTextureCache 路径更标准，功耗和稳定性更好。但当前项目仍是 PyQt + OpenGL/ModernGL，想完全吃满 M 系列，最终可能要迁移 Metal 渲染后端。

---

## 7. 详细验证方案

### 7.1 单元测试

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

必须新增或保持：

| 测试 | 目的 |
|---|---|
| `test_r16ui_12bit_neutral_gray_renders_correctly` | 防止 12-bit 颜色偏绿/偏蓝 |
| `test_yuv_shaders_keep_direct_uv_and_transparent_bounds` | 防止盲目 y-flip 和黑边覆盖 |
| `test_upload_yuv_planar_falls_back_when_r16ui_self_test_fails` | self-test fail 不黑屏 |
| `test_video_playback_default_does_not_finish_gpu` | 播放默认不每帧 `glFinish()` |
| `test_external_gpu_frame_lifetime` | 零拷贝 frame 不提前释放 |

### 7.2 离线 benchmark

H.264/HEVC：

```bash
.venv/bin/python tests/media/benchmark_playback_runtime.py \
  --file "/path/to/4k_h264_or_hevc.mp4" \
  --frames 300 \
  --warmup 30 \
  --modes cpu_yuv,gpu_yuv,vt_zero_copy \
  --output-json tests/media/results/4k_h264_playback_runtime.json
```

ProRes：

```bash
.venv/bin/python tests/media/benchmark_yuv_gpu_pipeline.py \
  --file "/Users/testuser/Desktop/调色补充.mov" \
  --sizes 1920x1080,1280x720,960x540 \
  --frames 120 \
  --warmup 10 \
  --mode auto,float16,r16ui \
  --verify-color \
  --output-json tests/media/results/4k_prores_yuv_pipeline_verify.json
```

验收：

| 指标 | 通过线 |
|---|---|
| `decode_p95_ms` | H.264/HEVC 4K 硬解 < 10ms；ProRes 视素材而定但不能阻塞 UI |
| `plane_prepare_p95_ms` | R16UI/R8/P010 路径 < 3ms |
| `texture_write_p95_ms` | CPU 上传 fallback < 4ms；零拷贝路径为 0 |
| `yuv_render_p95_ms` | < 2ms |
| `total_p95_ms` | 30fps 素材 < 33ms；25fps 素材 < 40ms |
| `color_max_abs_error` | SDR <= 3/255；10/12-bit 路径 <= 0.01 float |

### 7.3 软件内真实播放验收

必须手动验证这些操作：

| 操作 | 验收 |
|---|---|
| 打开 4K H.264/HEVC 并播放 60 秒 | 不黑屏、不冻结、无明显掉帧 |
| 打开 4K ProRes 并播放 60 秒 | 自动选择可实时 proxy；暂停后可回到高质量 |
| 拖动进度条 | 颜色不变蓝、不变灰、不闪黑 |
| 点击播放/暂停 | playback 与 seek 同一帧颜色肉眼一致 |
| 滚轮缩放 | 画面不突然缩小、不跑到左侧、不抖动 |
| 播放中调曝光/对比度 | 可降级，但 UI 不长时间卡死 |
| 切换视频 | 旧 texture/frame 不污染新视频 |

日志必须能看到：

```text
YUV GPU self-test PASSED
R16UI YUV self-test PASSED 或 fallback reason=...
frame_path=planar_gpu_yuv_r16ui / nv12_r8_rg8 / macos_vt_iosurface_zero_copy
decode_p95_ms=...
plane_prepare_p95_ms=...
texture_write_p95_ms=...
yuv_render_p95_ms=...
late_frames=...
dropped_frames=...
```

### 7.4 颜色对比

抽样同一帧，比较三条路径：

```text
seek/HQ CPU reference
playback GPU YUV
zero-copy external YUV
```

要求：

| 项目 | 标准 |
|---|---|
| 中性灰 | RGB 三通道差值 <= 2/255 |
| 肤色 | 不蓝脸、不绿脸，均值差可解释 |
| 黑白场 | limited/full range 正确，不发灰、不压黑 |
| 缩放后 | 中心点采样不跑偏 |

---

## 8. 遇到问题时的定位表

| 现象 | 优先怀疑 | 排查方法 | 修复方向 |
|---|---|---|---|
| 点击播放黑屏，拖动进度条有画面 | playback GPU path 断了，seek CPU path 正常 | debug bypass：YUV RGB texture 直接输出 screen | 检查 FBO bind、external texture bind、alpha、raw_view pass |
| 播放画面变小或跑左侧 | `visible_rect`、proxy/source 尺寸映射错误 | 打印 source size、proxy size、visible_rect、source_uv_scale/offset | 只在 `_get_playback_render_params()` 做一次坐标转换 |
| 播放颜色怪，seek 正常 | playback YUV matrix/range 或 plane 顺序错 | 对同帧 readback 与 CPU reference 比较 | 修 matrix/range、UV plane、bit_depth |
| 拖进度条变蓝脸 | seek/HQ RGB/BGR 顺序错 | 检查 `set_image()` 输入 | seek path 输出 BGR uint8 |
| 4K H.264/HEVC CPU 占用很高 | 没有真正硬解或硬解后 CPU download | 看 decode_ms、CPU 占用、路径日志 | 接 VT zero-copy 或至少 VideoToolbox NV12 fallback |
| ProRes 4K 仍卡 | plane_prepare 或 proxy decode 卡 | 看 `plane_prepare_ms` 和 `decode_ms` | R16UI/R8 直传、磁盘代理、proxy ladder |
| GPU path 一开就卡 | 每帧 readback/glFinish/fence wait | 搜索 `read()`, `finish()`, sync | readback 只调试，播放默认 `video_render_sync_mode=none` |
| 黑苹果上自检失败 | 驱动/IOSurface/OpenGL interop 不稳定 | 跑 `vt_zero_copy_probe.py` | 自动 fallback，不强开 |

---

## 9. 推荐优先级

> **状态更新（2026-09，v2 计划 P9D-02）**：本表的 P0/P1/P2 项已由
> v2 计划 Phase 0「复用 + 补维」吸收（四维字段、P0-06 诚实性缺口）；
> P2/P3 的零拷贝原型已落地并有条件化结论（无 10-bit 422 的原生
> Metal 纹理格式，ProRes 零拷贝不可行）；P4「Metal 渲染后端评估不应
> 作为第一步」的结论被 v2 实测推翻——render-only Metal 快 81~85%，
> compute 侧 Normalize 链路在 macOS 静默失效，Metal compute 已作为
> 第一收益完成（Phase 4）。以 v2 计划为当前有效版本。

| 优先级 | 任务 | 原因 |
|---|---|---|
| P0 | 完成 benchmark 和日志字段 | 没有数据就不知道 4K 卡在哪里 |
| P0 | R16UI/R8/P010 上传路径稳定 | 直接解决 ProRes 和 YUV prepare 大瓶颈 |
| P1 | 自适应 proxy ladder + 磁盘代理 | 让任何机器都有稳定预览 fallback |
| P1 | 播放调度只显示最新帧 | 解决前后跳帧和播放抖动 |
| P2 | macOS VT/IOSurface/OpenGL 零拷贝原型 | 真正接近达芬奇链路 |
| P3 | 主工程接入 GpuDecodedFrame | 原型通过后再做，避免污染现有播放 |
| P4 | Metal 渲染后端评估 | 长期目标，当前不应作为第一步 |

---

## 10. 最终验收标准

完成后必须同时满足：

1. 4K H.264/HEVC 在支持硬解的 macOS 上可 1080p proxy 实时播放，30fps 素材 `total_p95 < 33ms`。
2. 4K ProRes 在 720p 或 960x540 proxy 下稳定实时，暂停/seek 可回到高质量。
3. 播放热路径没有 UI 同步解码，没有每帧 CPU YUV->RGB，没有每帧 GPU readback。
4. macOS 零拷贝路径中视频帧不经过 numpy，不调用 `Texture.write()` 上传视频图像。
5. 拖动进度条、播放、暂停、缩放、调色的颜色和尺寸一致，不黑屏、不蓝脸、不绿屏、不突然缩小。
6. 失败时能自动 fallback，日志清楚写出 fallback 原因。
7. 所有相关单测通过，benchmark JSON 留档，可对比优化前后收益。
