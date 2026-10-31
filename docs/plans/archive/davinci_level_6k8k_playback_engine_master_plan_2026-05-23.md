# Topos Color 达芬奇级 6K/8K 播放与调色引擎长期优化总计划

日期：2026-05-23  
适用范围：Color、Edit、Composite 共享媒体播放核心；macOS / Windows / Linux 跨平台；4K、6K、8K 视频实时预览；调色、剪辑、合成中的视频帧链路。  
目标：将当前项目从“Python 驱动的图像/视频预览软件”逐步升级为“GPU 原生媒体引擎”，最终接近 DaVinci Resolve 这类专业 NLE/调色软件的播放体验。

---

## 0. 一句话结论

要达到达芬奇那种 6K、8K 播放顺滑度，不能只靠修一个解码器或一个 shader。必须建立一整套体系：

```text
媒体文件
  -> demux / packet queue
  -> 平台硬件解码
  -> GPU resident frame / zero-copy texture
  -> GPU YUV/RGB/color management
  -> GPU 调色/合成
  -> timeline/render cache/proxy fallback
  -> 异步播放调度
  -> 显示输出
```

短期先稳定 4K，中期打通 4K/6K 硬解零拷贝和代理缓存，长期迁移到更现代的 GPU 后端与 native media core。  
**不要把目标理解成“强行实时播放所有原始 8K 素材”。专业软件也会大量使用 proxy、optimized media、render cache、timeline cache 和硬件路径。**

---

## 1. 当前与达芬奇的核心差距

| 领域 | 达芬奇级做法 | 当前项目主要差距 | 对 6K/8K 的影响 |
|---|---|---|---|
| 解码 | 平台硬解、GPU resident frame、专业 codec 优化 | PyAV/FFmpeg CPU 或硬解后 CPU download 仍占主流 | 6K/8K H.264/HEVC CPU 软解不可持续 |
| CPU/GPU 数据流 | 视频帧尽量不离开 GPU | numpy planes、`Texture.write()`、CPU 转换仍存在 | 带宽、延迟、同步开销高 |
| 播放调度 | 媒体时钟驱动，只显示最新到期帧 | 易受 UI tick、队列积压、旧帧回放影响 | 抖动、前后跳帧、响应慢 |
| Proxy/Optimized Media | 自动后台生成，多层质量 | 当前 proxy 和磁盘代理仍需进一步专业化 | 原始 8K 不可实时也应流畅预览 |
| Render Cache | 片段级、节点级、时间线级缓存 | 调色/合成效果缓存体系不完整 | 复杂节点播放无法实时 |
| GPU pass | pass fusion、persistent texture、GPU cache | 多 pass、FBO、纹理上传、同步仍有开销 | 8K 带宽压力非常大 |
| 平台后端 | macOS Metal/VideoToolbox，Windows D3D/NVDEC/QSV | PyQt + ModernGL/OpenGL 是可用但不理想的中间层 | 零拷贝 interop 更难 |
| 性能可观测 | 每条路径都有指标和自动降级 | 部分指标已有，但还没形成产品级决策闭环 | 无法自动判断该降级还是该缓存 |
| 测试矩阵 | 大量真实素材、GPU、OS 组合 | 素材矩阵和跨平台 benchmark 仍需强化 | 容易某平台修好、另一平台退化 |

---

## 2. 达芬奇级目标定义

### 2.1 不同阶段的目标

| 阶段 | 目标 | 允许的现实妥协 |
|---|---|---|
| 阶段 A：稳定 4K | 4K H.264/HEVC/ProRes 通过 proxy 或 GPU YUV 稳定实时 | ProRes 原始 full-res 可在播放时降 proxy，暂停回 master |
| 阶段 B：专业 4K/轻量 6K | 4K 原始或高质量 proxy 稳定，6K 通过 optimized media 稳定 | 复杂效果可触发 render cache |
| 阶段 C：6K/8K 可用 | 6K/8K 使用硬解、proxy、optimized media、缓存组合流畅 | 不保证所有原始 8K long-GOP full-res 直接实时 |
| 阶段 D：达芬奇级架构 | zero-copy、GPU frame cache、timeline cache、native media core | OpenGL 可先存在，但要有现代 GPU 后端路线 |

### 2.2 量化验收目标

| 场景 | 最低验收 | 理想目标 |
|---|---:|---:|
| 4K H.264/HEVC 30fps | 1080p proxy p95 < 33ms | 原始或高质量 proxy p95 < 20ms |
| 4K ProRes 422/4444 25fps | 720p/1080p proxy p95 < 40ms | 1080p proxy p95 < 25ms |
| 6K H.264/HEVC 30fps | optimized/proxy p95 < 33ms | 硬解 + GPU path p95 < 25ms |
| 6K/8K ProRes | proxy/optimized media 实时 | 缓存命中时实时 |
| 播放 UI tick | p95 < 8ms | p95 < 3ms |
| render queue | 不积压到可感知延迟 | queue depth <= 2 |
| 播放热路径 readback | 0 次 | 0 次 |
| 播放热路径 `glFinish`/全局同步 | 0 次 | 0 次 |
| 掉帧策略 | 只丢过期预览帧，不倒退显示旧帧 | dropped/late frame 可解释、可统计 |

---

## 3. 总体架构蓝图

### 3.1 目标数据流

```text
Media File
  |
  | demux compressed packets
  v
Packet Queue
  |
  | platform hardware decode
  v
GpuDecodedFrame
  |  macOS: CVPixelBuffer / IOSurface / Metal texture / GL texture
  |  Windows: D3D11 texture / NVDEC / QSV / CUDA interop
  |  Linux: VAAPI / DMABUF / EGLImage
  v
GPU YUV/RGB Convert + Color Management
  |
  | texture stays on GPU
  v
Color / Composite / Timeline Render
  |
  | pass fusion + cache
  v
Viewer Output
```

Fallback 数据流：

```text
Media File
  -> CPU decode / hwaccel_download
  -> planar YUV CPU planes
  -> R8/R16UI/P010/PBO upload
  -> GPU YUV shader
  -> Color / Composite / Output
```

低端机器或复杂素材：

```text
Media File
  -> background optimized media / proxy generation
  -> proxy playback
  -> pause/seek/export uses master quality
```

### 3.2 必须建立的核心抽象

| 抽象 | 作用 | 关键字段 |
|---|---|---|
| `DecodedFrame` | CPU 侧帧，保留 packed 或 planar 数据 | data/planes、pixel_format、bit_depth、color metadata |
| `GpuDecodedFrame` | GPU resident 帧，不经过 CPU 图像数组 | plane handles、api、format、size、release fence |
| `FramePath` | 描述每帧实际路径 | `cpu_packed`, `planar_gpu_yuv`, `hwaccel_download`, `macos_zero_copy`, `windows_zero_copy` |
| `PlaybackQualityTier` | 播放质量档位 | master、proxy_1080、proxy_720、proxy_540、cache |
| `MediaPerformanceSample` | 统一性能指标 | decode、prepare、upload、render、queue、drop、late |
| `GpuBackend` | GPU 后端抽象 | OpenGL、Metal、D3D11/12、Vulkan/Qt RHI |
| `TextureBridge` | 平台 texture 共享桥 | IOSurface->GL、D3D11->GL、DMABUF->EGL |
| `RenderCacheEntry` | 缓存帧或节点输出 | clip id、frame、params hash、resolution、colorspace |

---

## 4. 路线图总览

| Wave | 名称 | 目标 | 优先级 | 风险 | 结果 |
|---|---|---|---|---|---|
| 0 | 可信测量与验收矩阵 | 先知道瓶颈在哪 | P0 | 低 | benchmark、日志、素材矩阵 |
| 1 | 4K 止血与 YUV 上传优化 | 现有架构先稳定 4K | P0 | 中 | R8/R16UI/P010/PBO、proxy ladder |
| 2 | 播放调度重构 | 消灭抖动、旧帧、队列积压 | P0 | 中 | 媒体时钟、late frame drop |
| 3 | Proxy/Optimized Media | 像 NLE 一样优雅降级 | P0 | 中 | 多层 proxy、后台生成、暂停 master |
| 4 | Render Cache | 复杂调色/合成可缓存 | P1 | 中 | clip/timeline/node cache |
| 5 | macOS 零拷贝原型 | VT/IOSurface 到 GPU texture | P1 | 高 | `macos_zero_copy_probe` |
| 6 | Windows 零拷贝原型 | D3D11VA/NVDEC/QSV 到 GPU texture | P1 | 高 | `windows_zero_copy_probe` |
| 7 | `GpuDecodedFrame` 主链路接入 | 零拷贝进入真实播放 | P2 | 高 | frame path 可切换 |
| 8 | GPU 调色 pass fusion | 减少 pass 和带宽 | P2 | 中 | 基础调色合并、LUT/曲线优化 |
| 9 | Native Media Core | 热路径移出 Python | P2 | 高 | C++/Rust/ObjC/Swift 模块 |
| 10 | 现代 GPU 后端路线 | OpenGL 之外的长期后端 | P3 | 很高 | Metal/D3D/Vulkan/Qt RHI 评估 |
| 11 | 6K/8K 产品级验收 | 长时间、多平台、真实素材 | P3 | 中 | 报告与回归门槛 |

---

## 5. Wave 0：可信测量与验收矩阵

### 5.1 目标

在任何大改之前，先把“卡在哪里”变成数据。没有数据就不能判断下一步应该做硬解、上传优化、proxy、cache 还是调度。

### 5.2 任务表

| ID | 任务 | 文件/模块 | 验收 |
|---|---|---|---|
| W0.1 | 统一性能指标结构 | `src/core/media/performance_monitor.py` | 输出 p50/p95/max、samples、frame_path |
| W0.2 | 补真实播放 runtime benchmark | 新增 `tests/media/benchmark_playback_runtime.py` | 300 帧播放模拟，统计 queue/drop/late |
| W0.3 | 扩展 YUV pipeline benchmark | `tests/media/benchmark_yuv_gpu_pipeline.py` | decode/prepare/write/render/total 都可信 |
| W0.4 | 建立素材 manifest | 新增 `tests/media/fixtures_manifest.json` 或 `.md` | 记录 codec、pix_fmt、fps、range、matrix |
| W0.5 | 增加 UI HUD 或日志聚合 | `src/ui/playback_hud_integration.py` | 软件内可看到当前路径和瓶颈 |
| W0.6 | 自动生成结果 JSON | `tests/media/results/` | 每次优化可对比前后 |
| W0.7 | 建立达芬奇级验收脚本 | 新增 `tests/media/validate_davinci_level.py` | 失败能指出 decode/upload/render/cache 哪层失败 |

### 5.3 必须输出的指标

```text
frame_path
source_resolution
playback_resolution
codec
pixel_format
bit_depth
decode_ms avg/p95/max
plane_prepare_ms avg/p95/max
texture_write_ms avg/p95/max
yuv_render_ms avg/p95/max
grading_render_ms avg/p95/max
ui_tick_ms avg/p95/max
queue_depth avg/max
ready_queue_depth
late_frames
dropped_frames
displayed_fps
audio_clock_drift_ms
cache_hit_rate
proxy_tier
gpu_backend
zero_copy_enabled
fallback_reason
```

### 5.4 素材测试矩阵

| 类型 | 规格 | 用途 |
|---|---|---|
| H.264 SDR | 4K/6K/8K 8-bit 4:2:0 | GOP 硬解基础 |
| HEVC SDR | 4K/6K/8K 8/10-bit | 现代相机/手机 |
| HEVC HDR | 4K/6K 10-bit PQ/HLG | HDR 元数据与 tone mapping |
| ProRes 422 HQ | 4K/6K/8K 10-bit | 专业 intra codec |
| ProRes 4444 | 4K/6K 12-bit + alpha | 高位深/alpha/planar |
| DNxHR HQX | 4K/6K 10-bit | Windows/跨平台中间格式 |
| Long GOP 高码率 | 4K/6K 100-400Mbps | 解码压力 |
| Variable frame rate | 手机素材 | 时钟/PTS 稳定性 |
| 异常 metadata | full/limited、BT.601/709/2020 | 色彩正确性 |

### 5.5 验证命令模板

macOS：

```bash
ffprobe -hide_banner -show_streams -select_streams v:0 "/path/to/video.mov"
ffmpeg -hide_banner -hwaccels
system_profiler SPDisplaysDataType

.venv/bin/python tests/media/benchmark_playback_runtime.py \
  --file "/path/to/video.mov" \
  --frames 300 \
  --warmup 30 \
  --modes cpu_yuv,gpu_yuv,macos_zero_copy \
  --output-json tests/media/results/macos_playback_runtime.json
```

Windows：

```powershell
ffmpeg -hide_banner -hwaccels
ffmpeg -hide_banner -decoders | findstr /i "h264 hevc av1 d3d11va qsv cuvid"

python tests/media/benchmark_playback_runtime.py `
  --file "D:\media\video.mp4" `
  --frames 300 `
  --warmup 30 `
  --modes cpu_yuv,hwaccel_download,gpu_yuv,windows_zero_copy `
  --output-json tests/media/results/windows_playback_runtime.json
```

### 5.6 常见坑

| 坑 | 后果 | 规避 |
|---|---|---|
| 只测 10 帧 | 结果被 shader 编译/磁盘缓存污染 | 至少 120-300 帧 |
| benchmark 不计 `packet.decode()` | decode 数据虚高 | decode 必须包含真实 packet decode |
| 每帧 readback 验证颜色 | 播放被 readback 测慢 | readback 仅 debug/self-test |
| 离线 benchmark 不测 UI | 忽略真实队列和调度问题 | 必须有 runtime benchmark |
| 只测一台机器 | 跨平台退化无法发现 | 建立 macOS/Windows/GPU 矩阵 |

---

## 6. Wave 1：4K 止血与 YUV 上传优化

### 6.1 目标

在不等待零拷贝大工程的情况下，让 4K 预览稳定，并为 6K/8K 的 proxy/cache 打基础。

### 6.2 任务表

| ID | 任务 | 文件/模块 | 验收 |
|---|---|---|---|
| W1.1 | 锁定 R16UI/usampler2D 路径 | `src/core/yuv_gpu_upload.py` | 10/12-bit planar 不再 float16 normalize |
| W1.2 | 8-bit YUV R8/RG8 直传 | `src/core/yuv_gpu_upload.py` | yuv420/nv12 不转 float16 |
| W1.3 | P010/R16 直传 | `src/core/yuv_gpu_upload.py` | 10-bit NV12/P010 色彩正确 |
| W1.4 | PBO / staging ring buffer | `src/core/video_texture_pool.py` | CPU upload 不阻塞渲染 |
| W1.5 | persistent video FBO/texture pool | `src/core/async_renderer.py` | 播放时无频繁分配 |
| W1.6 | YUV shader self-test 完整化 | `tests/test_yuv_gpu_upload.py` | 8/10/12-bit，420/422/444/NV12/P010 |
| W1.7 | packed 8-bit 视频走 u1 texture | `src/core/async_renderer.py` | RGB/BGR/RGBA/BGRA 不转 float16 |

### 6.3 验收指标

| 指标 | 目标 |
|---|---:|
| `plane_prepare_p95_ms` | < 3ms |
| `texture_write_p95_ms` 1080p proxy | < 4ms |
| `yuv_render_p95_ms` | < 2ms |
| 4K ProRes proxy playback | 720p/960p 稳定实时 |
| 颜色对比 | CPU reference 与 GPU output 误差 <= 3/255 或 0.01 float |

### 6.4 必须避免

| 禁止项 | 原因 |
|---|---|
| 把 integer texture 当 normalized sampler2D | 10/12-bit 可能颜色错误 |
| 为性能关闭颜色 self-test | 会重新出现蓝脸/绿屏 |
| 播放路径每帧 `ctx.finish()` | 会直接把播放拖慢 |
| 在 UI tick 做 YUV->RGB | 破坏实时播放 |

---

## 7. Wave 2：播放调度重构

### 7.1 目标

播放系统必须从“尽量渲染所有帧”变成“按媒体时钟显示最新到期帧”。过期帧应丢弃，不允许旧帧倒退显示。

### 7.2 目标架构

```text
MediaClock / AudioClock
  -> desired presentation timestamp
  -> ReadyFrameQueue.select_latest_due_frame()
  -> submit latest frame to renderer
  -> renderer coalesces preview frames
  -> displayed frame stats
```

### 7.3 任务表

| ID | 任务 | 文件/模块 | 验收 |
|---|---|---|---|
| W2.1 | 明确媒体时钟 | `src/core/video/playback_state.py` | tick 基于 PTS，不基于“下一帧编号自增” |
| W2.2 | Ready queue 选择最新到期帧 | `src/core/video/decode_worker.py` | 旧帧不倒退显示 |
| W2.3 | late frame drop 策略 | `src/core/video_playback.py` | late frames 统计可见 |
| W2.4 | 渲染命令合并 | `src/core/async_renderer.py` | playback lane 只保留最新 |
| W2.5 | viewport refresh 与 video frame 分离 | `src/ui/canvas.py`, `src/core/video_playback.py` | 缩放不触发同步解码 |
| W2.6 | 播放中调色参数合并 | `src/ui/main_window.py` | slider 拖动不排长队 |
| W2.7 | audio/video sync 预留 | playback core | 视频可按音频时钟校正 |

### 7.4 验收

| 操作 | 验收 |
|---|---|
| 连续播放 60 秒 | 不前后跳帧 |
| 拖动滚轮缩放 | 不抖、不突然缩小、不跑左侧 |
| 播放中快速调色 | UI 不冻结，渲染队列不积压 |
| 解码偶尔慢 | 丢过期帧，不回放旧帧 |
| pause/resume | 时间线位置准确 |

---

## 8. Wave 3：Proxy / Optimized Media 系统

### 8.1 目标

达芬奇流畅的重要原因之一是它不会在所有情况下硬播原始 8K。Topos Color 必须有专业的 proxy/optimized media 系统。

### 8.2 设计原则

| 原则 | 说明 |
|---|---|
| 播放优先实时 | 播放可以降级，暂停/导出保真 |
| 后台生成 | 用户导入素材后自动后台生成 proxy |
| 多层质量 | 1080p、720p、540p、quarter、half |
| 透明切换 | 播放中 proxy，暂停切 master |
| 可解释 | HUD 显示当前使用 proxy/cache/master |
| 可控 | Preferences 可设置 proxy 编码和目录 |

### 8.3 任务表

| ID | 任务 | 文件/模块 | 验收 |
|---|---|---|---|
| W3.1 | 设计 proxy manifest | 新增 media cache 模块 | 记录源文件 hash、mtime、codec、proxy tiers |
| W3.2 | 后台 proxy generator | `src/core/media/proxy_generator.py` | 不阻塞 UI |
| W3.3 | 多层 proxy ladder | playback/session | 自动选择 1080/720/540 |
| W3.4 | Optimized Media 编码策略 | proxy generator | ProRes Proxy/DNxHR LB/H.264 intra 可选 |
| W3.5 | cache 目录管理 | config/preferences | 可清理、限制大小 |
| W3.6 | 播放 master/proxy 切换 | `src/core/video_playback.py` | 播放 proxy，暂停 master |
| W3.7 | Timeline 级 proxy 选择 | edit/timeline | 多 clip 时统一质量策略 |

### 8.4 推荐 proxy 编码

| 平台 | 低 CPU 解码 proxy | 高质量 proxy | 备注 |
|---|---|---|---|
| macOS | ProRes Proxy / ProRes LT | ProRes 422 LT | 解码友好，文件较大 |
| Windows | DNxHR LB/SQ | DNxHR SQ/HQ | 跨平台稳定 |
| 通用 | H.264 intra / all-I | H.264 high bitrate | 体积小，但编码/解码策略要测 |

### 8.5 自动降级策略

| 条件 | 动作 |
|---|---|
| `total_p95 > frame_budget * 1.25` 持续 2 秒 | 降一级 proxy |
| `late_frames > 5` 持续 2 秒 | 降一级 proxy |
| decode queue 经常为空 | 增加预读或降 proxy |
| render queue 积压 | 降效果质量或启用 cache |
| 连续 5 秒稳定 | 尝试升一级 proxy |

---

## 9. Wave 4：Render Cache / Timeline Cache

### 9.1 目标

复杂调色、合成节点、时间线效果无法每帧实时计算时，必须能缓存结果。专业软件不会每次播放都重算所有节点。

### 9.2 缓存层级

| 层级 | 缓存对象 | 适用场景 |
|---|---|---|
| Decode cache | 解码后的帧或 GPU frame | 顺序播放/来回 scrub |
| Proxy cache | 降采样代理文件 | 4K/6K/8K 源素材 |
| Node cache | 单个高成本节点输出 | AI、Blur、Denoise、复杂 keyer |
| Clip render cache | 单 clip 调色后结果 | Color 页面播放 |
| Timeline render cache | timeline 片段结果 | Edit 页面多 clip |
| Composite cache | 节点图中间结果 | Composite 页面 |

### 9.3 任务表

| ID | 任务 | 文件/模块 | 验收 |
|---|---|---|---|
| W4.1 | 统一 cache key | core/cache | 参数 hash、source hash、frame、resolution |
| W4.2 | Clip render cache | color playback | 调色参数不变时播放命中 cache |
| W4.3 | Timeline cache | edit timeline | 多 clip 播放可缓存片段 |
| W4.4 | Composite node cache | composite graph | 高成本节点可缓存 |
| W4.5 | Cache invalidation | color/edit/composite | 参数改动只失效相关缓存 |
| W4.6 | Disk/GPU cache policy | cache manager | VRAM/RAM/disk 上限和 LRU |
| W4.7 | Smart cache 后台生成 | background worker | 空闲时自动缓存高成本片段 |

### 9.4 缓存 key 必须包含

```text
source media id / file hash / mtime
timeline clip id
frame index / pts
source range / speed change
playback resolution
color space / display transform
grade params hash
node graph hash
effect quality mode
proxy tier
software version / shader version
```

### 9.5 验收

| 场景 | 验收 |
|---|---|
| 加复杂调色后第一次播放 | 可掉帧或生成 cache |
| 第二次播放同片段 | cache hit，稳定实时 |
| 调一个参数 | 只失效相关片段 |
| 缩放 viewport | 不重新生成 clip cache |
| 切换显示 LUT | 正确判断是否需要失效 |

---

## 10. Wave 5：macOS 零拷贝原型

### 10.1 目标

在独立原型中证明：

```text
VideoToolbox -> CVPixelBuffer/IOSurface -> GPU texture -> shader
```

播放热路径不经过 numpy，不调用 `Texture.write()` 上传视频帧。

### 10.2 建议模块

```text
src/core/media/macos_vt/
  __init__.py
  vt_demux.py
  vt_decompression.py
  iosurface_gl_bridge.py
  vt_zero_copy_probe.py
  vt_capability.py
```

### 10.3 实施步骤

| ID | 任务 | 验收 |
|---|---|---|
| W5.1 | PyAV/FFmpeg 只 demux compressed packet | 不调用 `packet.decode()` |
| W5.2 | VTDecompressionSession 解码 | 能输出 CVPixelBuffer |
| W5.3 | 确保 IOSurface backing | `CVPixelBufferGetIOSurface` 非空 |
| W5.4 | IOSurface plane -> OpenGL texture | Y/UV plane 可被 shader 采样 |
| W5.5 | NV12/P010 shader | 颜色与 CPU reference 对齐 |
| W5.6 | ring buffer 生命周期 | CVPixelBuffer 不提前释放 |
| W5.7 | probe benchmark | 300 帧 p95 达标 |
| W5.8 | fallback gate | 失败自动禁用，不影响普通播放 |

### 10.4 关键坑

| 坑 | 现象 | 解决 |
|---|---|---|
| CVPixelBuffer 没有 IOSurface | 无法共享给 GL | VT output 设置 `kCVPixelBufferIOSurfacePropertiesKey` |
| 纹理 target/format 错 | 黑屏、绿屏 | probe 输出 target/internalFormat/type |
| y-flip 乱改 | 播放缩小、翻转、UV 错 | 只在统一 display pass 处理方向 |
| 每帧创建 texture cache | 卡顿 | texture/cache/ring buffer 复用 |
| 每帧 glFinish | 卡顿 | fence + ring buffer |
| PyObjC callback 生命周期 | 崩溃 | 原型通过后考虑 ObjC/Swift 小扩展 |

---

## 11. Wave 6：Windows 零拷贝原型

### 11.1 目标

在 Windows 上证明：

```text
D3D11VA / NVDEC / QSV -> D3D11 texture or CUDA surface -> OpenGL interop texture -> shader
```

### 11.2 建议模块

```text
src/core/media/windows_hw/
  __init__.py
  d3d11_decoder.py
  d3d11_gl_bridge.py
  cuda_gl_bridge.py
  windows_zero_copy_probe.py
  adapter_match.py
```

### 11.3 实施步骤

| ID | 任务 | 验收 |
|---|---|---|
| W6.1 | 检测 D3D11VA/NVDEC/QSV capability | 能输出 GPU vendor、adapter |
| W6.2 | 先打通 hwaccel_download | decode 比 CPU 快，fallback 稳 |
| W6.3 | 原型获取 D3D11 texture handle | 不下载到 CPU |
| W6.4 | D3D11 texture -> OpenGL interop | shader 可采样 |
| W6.5 | NV12/P010 shader 对齐 | 颜色正确 |
| W6.6 | 多 GPU adapter match | iGPU/dGPU 不一致时 fallback |
| W6.7 | 300 帧 benchmark | p95 达标 |
| W6.8 | fallback gate | 不支持时自动回 `hwaccel_download` 或 CPU |

### 11.4 Windows 特殊坑

| 坑 | 表现 | 解决 |
|---|---|---|
| PyAV 不暴露 D3D11 texture | 只能拿 CPU frame | 需要 FFmpeg C API / C++ / Rust 扩展 |
| Optimus 混合显卡 | 解码在 iGPU，GL 在 dGPU | adapter match gate |
| WGL interop 驱动差异 | NVIDIA/AMD/Intel 表现不同 | 分 vendor probe |
| QSV 与 D3D11 共享复杂 | 黑屏或复制 | 优先 D3D11VA 统一路径 |
| CUDA 路径维护成本高 | 只支持 NVIDIA | 作为可选加速，不做唯一方案 |

---

## 12. Wave 7：`GpuDecodedFrame` 接入主链路

### 12.1 目标

macOS/Windows 原型通过后，再接入真实播放，避免主工程被平台实验污染。

### 12.2 数据结构草案

```python
@dataclass
class GpuDecodedFrame:
    frame_index: int
    pts: float
    width: int
    height: int
    pixel_format: str
    api: str  # "opengl", "metal", "d3d11", "cuda"
    planes: tuple[Any, ...]
    plane_formats: tuple[str, ...]
    color_range: str
    color_matrix: str
    color_transfer: str
    color_primaries: str
    release_handle: Any
    fence: Any | None = None
    debug_path: str = ""
```

### 12.3 任务表

| ID | 任务 | 文件/模块 | 验收 |
|---|---|---|---|
| W7.1 | 定义 `GpuDecodedFrame` | `src/core/media/frame.py` | 单元测试通过 |
| W7.2 | DecodeWorker 支持 GPU frame queue | `src/core/video/decode_worker.py` | CPU/GPU frame 可共存 |
| W7.3 | Renderer 支持 external planes | `src/core/async_renderer.py` | 跳过 CPU upload |
| W7.4 | `FramePath` 日志 | performance monitor | 真实路径可见 |
| W7.5 | fallback 统一 | playback engine | 单帧失败不永久黑屏 |
| W7.6 | 生命周期回收 | renderer/media bridge | 无泄漏、无提前释放 |

### 12.4 验收

| 验收项 | 标准 |
|---|---|
| macOS H.264/HEVC | `frame_path=macos_vt_iosurface_zero_copy` |
| Windows H.264/HEVC | `frame_path=windows_d3d11_zero_copy` |
| CPU upload | zero-copy 路径中为 0 |
| numpy | zero-copy 路径中无视频帧 numpy |
| fallback | 关闭 zero-copy 仍可播放 |

---

## 13. Wave 8：GPU 调色与合成 pass fusion

### 13.1 目标

6K/8K 的 GPU 带宽很贵。每多一个 full-frame pass 都可能造成明显压力。基础调色应该尽量合并，复杂效果播放时降级或缓存。

### 13.2 任务表

| ID | 任务 | 文件/模块 | 验收 |
|---|---|---|---|
| W8.1 | 基础调色 pass fusion | shader compiler / async renderer | exposure/contrast/saturation/temperature/tint 一 pass |
| W8.2 | LUT 3D texture 优化 | LUT manager | 3D LUT 采样稳定且缓存 |
| W8.3 | 曲线 LUT 化 | curves module | 曲线改动时生成 1D LUT |
| W8.4 | Raw view + YUV convert 合并评估 | async renderer | 只在颜色正确时启用 |
| W8.5 | 播放效果质量档 | effect preview quality | blur/sharpen/warper/AI 播放降级 |
| W8.6 | 合成节点 cache/fusion | composite scheduler | 可 fusion 的节点合并 |
| W8.7 | shader warmup | shader compiler | 首帧不因编译卡顿 |

### 13.3 Pass 预算

| 场景 | 目标 pass 数 |
|---|---:|
| 纯播放无调色 | 1-2 pass |
| 基础调色 | 2-3 pass |
| LUT + 基础调色 | 2-3 pass |
| 简单 composite | 3-5 pass |
| 高成本效果 | 播放 cache，暂停 HQ |

---

## 14. Wave 9：Native Media Core

### 14.1 目标

Python 适合 UI、项目管理和参数调度，但不适合每帧 6K/8K 热路径。需要把关键热路径逐步迁出 Python。

### 14.2 候选技术

| 技术 | 适用 | 优点 | 风险 |
|---|---|---|---|
| C++ | FFmpeg/D3D/VT/GL interop | 性能和生态最好 | 构建复杂 |
| Rust | 安全、跨平台 core | 生命周期更稳 | 多媒体绑定成本 |
| Objective-C/Swift | macOS VideoToolbox/Metal | 苹果 API 最顺 | 平台专属 |
| Cython/pybind11 | Python 集成 | 渐进迁移 | 维护双语言 |

### 14.3 优先迁移模块

| 优先级 | 模块 | 原因 |
|---|---|---|
| P0 | packet demux / decode worker hot path | 每帧运行 |
| P0 | platform texture bridge | Python 生命周期难管 |
| P1 | frame queue / sync / fence | 对稳定性关键 |
| P1 | proxy generator | 后台长任务 |
| P2 | render cache writer/reader | 大 IO 和并发 |
| P3 | color shader compiler | 可继续 Python 管理 |

### 14.4 验收

| 指标 | 目标 |
|---|---:|
| Python 每帧热路径 CPU 时间 | < 1ms |
| UI 线程每帧工作 | 提交命令和状态更新，不做图像转换 |
| Native module crash isolation | 失败可 fallback，不直接损坏项目 |

---

## 15. Wave 10：现代 GPU 后端路线

### 15.1 背景

OpenGL/ModernGL 能继续作为中期方案，但要达到真正长期的 6K/8K 跨平台专业体验，需要评估现代 GPU 后端。

### 15.2 路线选择

| 路线 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| 继续 OpenGL + bridge | 改动最小 | macOS 弃用、interop 复杂 | 中期保留 |
| macOS Metal 后端 | 与 VideoToolbox 最契合 | 要重写 shader/backend | 长期推荐 |
| Windows D3D11/12 后端 | 与 D3D11VA 最契合 | 跨平台分叉 | Windows 长期推荐 |
| Vulkan 后端 | 跨平台现代 API | 开发复杂 | 可作为统一长期方向 |
| Qt RHI/QRhi | 和 Qt 集成 | 自由度/资料有限 | 值得原型评估 |
| WebGPU/wgpu | 跨平台抽象好 | Python/Qt 集成未知 | 长期研究 |

### 15.3 建议策略

1. 不要现在立刻重写渲染后端。
2. 先把 `GpuDecodedFrame`、`GpuBackend`、`TextureBridge` 抽象设计好。
3. OpenGL 继续作为当前后端。
4. macOS 先做 Metal 小原型，Windows 先做 D3D11 小原型。
5. 等 zero-copy 和 cache 稳定后，再决定是否迁移主调色 pipeline。

---

## 16. Wave 11：6K/8K 产品级验收

### 16.1 长时间播放验收

| 场景 | 时间 | 验收 |
|---|---:|---|
| 4K H.264/HEVC | 10 分钟 | 不黑屏、不泄漏、drop 可控 |
| 4K ProRes | 10 分钟 | proxy/cache 稳定 |
| 6K HEVC | 5 分钟 | proxy/zero-copy 可实时 |
| 8K HEVC | 3 分钟 | optimized/proxy 流畅 |
| 复杂调色片段 | 5 分钟 | cache 命中后流畅 |
| scrub/seek 压力 | 2 分钟 | 不蓝脸、不错帧、不崩溃 |

### 16.2 跨平台验收矩阵

| 平台 | GPU | 必测路径 |
|---|---|---|
| macOS Apple Silicon | M1-M5 | VideoToolbox/Metal or IOSurface fallback |
| macOS Intel/AMD | AMD/Intel | VideoToolbox + OpenGL bridge |
| 黑苹果 AMD | 6900XT 等 | gate/fallback 必测 |
| Windows NVIDIA | RTX/GTX | D3D11VA/NVDEC, interop |
| Windows Intel | Iris/Xe/UHD | D3D11VA/QSV |
| Windows AMD | Radeon | D3D11VA |
| Linux 可选 | Intel/AMD/NVIDIA | VAAPI/NVDEC fallback |

### 16.3 通过标准

| 项目 | 标准 |
|---|---|
| 播放稳定 | 无黑屏、无持续卡死、无前后跳帧 |
| 色彩正确 | playback/seek/export 一致 |
| 自动降级 | 不实时会自动 proxy/cache，而不是硬卡 |
| 资源稳定 | VRAM/RAM 不持续增长 |
| 路径透明 | HUD/log 能说明当前为何快或为何降级 |
| fallback 安全 | 任意硬件特性失败都不导致不可播放 |

---

## 17. 里程碑建议

### Milestone 1：4K 稳定版

目标周期：先做这个，不要跳过。

必须完成：

- Wave 0 测量闭环。
- Wave 1 R8/R16UI/P010/PBO。
- Wave 2 播放调度。
- Wave 3 基础 proxy ladder。

验收：

- 4K H.264/HEVC 30fps proxy 稳定。
- 4K ProRes 25fps proxy 稳定。
- 无黑屏、蓝脸、缩放跳变、旧帧倒退。

### Milestone 2：6K 可用版

必须完成：

- Optimized Media 后台生成。
- Render Cache 初版。
- macOS/Windows 至少一个平台 zero-copy probe 通过。
- `GpuDecodedFrame` 接口设计完成。

验收：

- 6K H.264/HEVC 使用硬解/proxy 流畅。
- 复杂调色 cache 命中后流畅。

### Milestone 3：跨平台零拷贝版

必须完成：

- macOS zero-copy 主链路接入。
- Windows zero-copy probe 至少在 NVIDIA 或 D3D11VA 平台通过。
- fallback gate 产品化。

验收：

- zero-copy 路径无 numpy、无 `Texture.write()`。
- 4K/6K H.264/HEVC 高质量 proxy 或原始预览稳定。

### Milestone 4：8K 专业预览版

必须完成：

- 多层 proxy/optimized media。
- timeline/render cache。
- Native media core 初版。
- GPU backend 迁移路线确定。

验收：

- 8K 素材通过 optimized/proxy/cache 顺滑播放。
- 暂停/逐帧可高质量查看。
- 导出使用 master quality。

---

## 18. 实施顺序清单

给后续 AI 或开发者执行时，建议按这个顺序，不要乱跳。

1. 补齐 benchmark 和日志字段。
2. 用真实 4K/6K/8K 素材跑 baseline。
3. 修 R16UI/R8/P010 上传和颜色 self-test。
4. 修播放调度，禁止旧帧倒退显示。
5. 做动态 proxy ladder。
6. 做后台 proxy/optimized media。
7. 做 clip render cache 初版。
8. macOS 做 VT/IOSurface zero-copy probe。
9. Windows 做 D3D11VA/NVDEC/QSV zero-copy probe。
10. 设计并接入 `GpuDecodedFrame`。
11. 把 decode/texture bridge 热路径迁出 Python。
12. 做 GPU pass fusion。
13. 扩大 render cache 到 timeline/composite。
14. 做长时间 6K/8K 验收。
15. 决定 Metal/D3D/Vulkan/Qt RHI 长期后端。

---

## 19. 给执行 AI 的硬性规则

后续让其它 AI 执行本计划时，建议把这些规则原样贴给它。

| 规则 | 说明 |
|---|---|
| 不准只凭肉眼判断优化成功 | 必须输出 benchmark JSON |
| 不准把 PyAV hwaccel 误称为零拷贝 | 只要经过 numpy 或 `Texture.write()` 就不是零拷贝 |
| 不准每帧 readback | 只允许 self-test/debug |
| 不准每帧 `glFinish` | 播放热路径禁止全局 GPU 同步 |
| 不准无条件开启平台特性 | macOS/Windows 都必须有 probe gate |
| 不准牺牲颜色正确性换性能 | CPU/GPU reference 必须对齐 |
| 不准改 seek 正常但播放坏，或播放正常但 seek 蓝脸 | seek/playback/export 三路径都要测 |
| 不准同时重构所有平台 | 先 probe，再接入 |
| 不准让 UI tick 做解码/转换 | UI 只提交命令与状态 |
| 不准让旧帧倒退显示 | late frame 只能丢弃 |

---

## 20. 最终完成标准

当以下条件同时满足，才可以说 Topos Color 的播放引擎真正接近达芬奇级方向：

1. 4K 素材在 macOS 和 Windows 都可稳定实时预览。
2. 6K/8K 素材可以通过硬解、proxy、optimized media、render cache 组合顺滑播放。
3. H.264/HEVC 在支持平台上有真正 zero-copy 或接近 zero-copy 的 GPU frame path。
4. ProRes/DNxHR 等专业格式有高效 proxy/cache 和高位深 YUV 直传路径。
5. 播放热路径无 UI 同步解码、无每帧 CPU YUV->RGB、无每帧 GPU readback。
6. 播放、seek、暂停、调色、缩放、导出颜色一致。
7. 复杂调色/合成可以通过 render cache 保持播放顺滑。
8. 所有平台特性都有 self-test/probe/fallback，不会因为某台机器不支持而黑屏。
9. 性能数据可观测、可回归、可解释。
10. Python 不再承担每帧高负载图像处理热路径。

---

## 21. 相关计划引用

本计划是长期总蓝图，具体 4K 和 macOS/Windows 细节可继续参考：

- `docs/4k-playback-optimization.md`
- `docs/macos_gpu_yuv_conversion_fix_plan_2026-05-22.md`
- `docs/r16ui_yuv_gpu_pipeline_followup_plan_2026-05-22.md`
- `docs/video_v3_decode_render_realtime_optimization_master_plan_2026-05-21.md`
- `docs/professional_video_pipeline_upgrade_plan.md`

