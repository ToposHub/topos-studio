# Topos Color 实时播放后续缺陷驱动优化计划表

日期：2026-06-14  
范围：Edit 页面时间线实时播放、GPU 合成、ProgramViewer present、代理/缓存/解码调度、音频同步。  
背景：在“专业 NLE 实时播放架构升级”之后，实际测试仍出现蓝屏、卡顿、跳帧、声音杂音，以及截图中“同一帧里叠加其它帧画面”的混帧现象。

---

## 1. 当前结论

这次优化还不能称为“专业 NLE 实时播放架构完成”。

当前代码已经有一批新模块和测试，例如：

- `src/features/edit/core/realtime_playback/types.py`
- `src/features/edit/core/realtime_playback/timeline_decode_scheduler.py`
- `src/features/edit/core/realtime_playback/gpu_frame_pool.py`
- `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py`
- `tests/edit/test_edit_realtime_regression.py`

但真实播放链路仍存在关键缺口：

1. GPU present 还没有正式的 fence + viewer ack 生命周期协议。
2. 新 realtime playback 模块还没有完全成为唯一热路径。
3. 多轨合成、输出纹理复用、跨 context 显示仍有竞态风险。
4. ProRes / 多轨 / 音频同步不是靠“小补丁”能稳定，需要代理、缓存、调度器和音频时钟一起重构。
5. 当前测试更偏“模块不变量”和静态守卫，缺少真实 GPU 像素级、跨 context、高压力播放验收。

已经做过一批临时止血修复：

- commit `7a7bceb 修复 GPU 预览输出混帧同步与 layer 契约`
- 将 GPU 输出给 Viewer 前的 `glFlush()` 临时改成 `glFinish()`，用于验证和避免半写纹理被读取。
- 修复 `TimelineGpuCompositor` 到 `EditGpuPreviewRenderer` 的 layer 数据契约。
- 多轨 ping-pong 写 FBO 前清屏，降低残留混入风险。

注意：`glFinish()` 不是最终专业方案。它能提升正确性，但会阻塞 GPU pipeline，最终必须替换成 `GL fence + TextureLease + ProgramViewer ack`。

---

## 2. 发现的问题总表

| ID | 严重级别 | 问题 | 现象 | 根因 | 当前状态 | 最终方向 |
|---|---:|---|---|---|---|---|
| RP-P0-01 | P0 | 输出纹理跨 context 未完成同步 | 同一帧里出现其它帧块状叠加、撕裂、混帧 | renderer 只 `glFlush()` 就 emit texture id；Viewer 可能在另一个 GL context 读到半写入纹理 | 已临时用 `glFinish()` 止血 | 改成 render thread `glFenceSync`，Viewer `glWaitSync`/ack，移除热路径 `glFinish()` |
| RP-P0-02 | P0 | 输出纹理 slot 生命周期没有 Viewer ack | 播放快时可能显示被下一帧复用/覆盖的 texture | output pool 只是延迟复用，没有明确“UI 已拷贝完成”确认 | 仍未根治 | `TextureLease` 状态机：FREE/WRITING/READY/IN_VIEWER/RELEASABLE |
| RP-P0-03 | P0 | 新 compositor 与旧 backend layer 契约不一致 | 多轨轨道信息丢失，可能错层/漏层/不透明度失效 | `TimelineGpuCompositor` 曾把裸 payload 直接传入 backend；backend 需要 dict | 已修复并加单测 | 固化 `BackendLayerDTO`，删除裸 payload 入口 |
| RP-P0-04 | P0 | 多轨 ping-pong 写 FBO 前未清屏 | 画面局部残留、块状拼接 | 写入 FBO 依赖 shader 全覆盖；若 viewport/scissor/quad 状态异常会保留旧内容 | 已加清屏 | 后续增加 GL state guard 和像素级测试 |
| RP-P0-05 | P0 | 新 realtime 模块不是唯一热路径 | 改了很多模块但播放仍像旧系统 | `TimelinePreviewRenderer._try_gpu_render()` 仍直接使用老 `EditGpuDecodeManager -> EditGpuPreviewRenderer` 路径 | 未完成 | 建立唯一 `RealtimePlaybackEngine.request_present()`，禁用旧旁路 |
| RP-P0-06 | P0 | `GpuFramePool` cache key 不够强 | 可能复用错误 source/proxy/cache 的 GPU texture | key 只看 clip/frame/generation，未完整纳入 source kind、proxy key、render cache key、pixel format、尺寸 | 未完成 | 使用完整 `FrameIdentity + payload signature + output profile` |
| RP-P0-07 | P0 | ProRes 一轨仍可能播不动 | 单轨 ProRes 卡顿、画面跳 | ProRes master 若走实时 CPU planar upload/随机解码，不是专业 NLE 热路径 | 未完成 | 实时只走 proxy/render cache；paused/step/export 才允许 master exact |
| RP-P0-08 | P0 | 音频仍有杂音和 A/V 对不上 | 声音杂音、跟不上画面 | 音频没有完整的 clock、ring buffer、resampler、drift correction、underrun 统计 | 部分历史修复，不完整 | 独立 AudioEngine，以 audio clock/monotonic clock 驱动视频 hold/drop |
| RP-P0-09 | P0 | 蓝屏/拖入时间线不能立即播放 | 拖入素材后先蓝屏，等待几秒 | 缺少 asset-level proxy ready gate、首帧 thumbnail/placeholder、后台代理生成状态机 | 未完成 | 拖入后立即显示缩略图/上一帧/占位，后台生成 proxy/cache |
| RP-P0-10 | P0 | 实时路径仍可能静默 CPU fallback | 多轨卡死、QImage/cpu upload 混入 | fallback 没有强制 telemetry gate，实时播放可能退到 CPU 合成 | 未完成 | realtime mode 下 `cpu_qimage_frames == 0`，fallback 必须 hold/rebuffer |
| RP-P1-11 | P1 | GPU tests 不够真实 | 单测过了，真实播放仍错 | 测试多为 mock/static，不验证像素、不验证跨 context、不压测 slot reuse | 未完成 | 增加 GPU pixel sentinel、cross-context stress、multi-track mosaic 检测 |
| RP-P1-12 | P1 | ProgramViewer present 协议太弱 | 可见帧和 renderer pool 生命周期脱节 | `set_shared_texture(tex_id,w,h)` 只传裸 texture id，没有 fence/slot/generation/release | 未完成 | 改为 `GpuPresentFrame` descriptor |
| RP-P1-13 | P1 | generation / stale frame 只在局部守护 | seek/播放时可能抖回旧帧 | 多模块各自判断 generation，没有统一 present queue | 部分存在 | 统一 `PresentQueue` 丢弃 stale，按 timeline generation 单调输出 |
| RP-P1-14 | P1 | 解码调度没有真正控制预算 | 多轨时 CPU/GPU 被同时打满 | 多轨并行 decode 缺少 back-pressure、deadline、drop/hold 策略 | 未完成 | `TimelineDecodeScheduler` 管 deadline、prebuffer、priority、cancel |
| RP-P1-15 | P1 | IOSurface/CVPixelBuffer 生命周期仍需绑定 slot | 局部混帧、释放后采样 | source frame release 与 GPU 采样完成需要 per-frame fence，不应只用全局标志 | 部分修复 | 每个 source texture lease 绑定 retain/release/fence |
| RP-P1-16 | P1 | 代理/缓存策略没有形成产品级闭环 | memory proxy 或 master 被当主路径 | 磁盘 proxy、render cache、状态 UI、失效规则不完整 | 未完成 | AssetProxyManager + RenderCacheAdapter 成为 realtime source resolver |
| RP-P1-17 | P1 | 色彩/尺寸/viewport 状态容易污染 | 不同素材尺寸混合时黑边、残影、错位 | GL state 分散，FBO resize 与 viewport/scissor 状态缺少强制 reset | 未完成 | 每个 pass 设置 viewport/scissor/blend/depth，并加 debug assert |
| RP-P1-18 | P1 | 性能指标没有作为发布门槛 | “感觉变快/没变快”无法定位 | 缺少每帧 decode/render/present/audio trace 和 benchmark 报告 | 未完成 | 固化 benchmark suite 和 telemetry dashboard |
| RP-P2-19 | P2 | 文档/实现不同步 | 文档写了专业架构，代码仍走旧链路 | 计划、规格、测试、实际入口没有绑定验收 | 未完成 | 每个阶段必须有 grep gate、runtime gate、benchmark gate |

---

## 3. 必须先做的 P0 架构重构

### 3.1 P0-A：实现正式的 GPU present fence + ack 协议

目标：彻底解决混帧、半帧、输出 texture 被复用的问题，并移除每帧 `glFinish()`。

| 项 | 内容 |
|---|---|
| 涉及文件 | `src/features/edit/core/edit_gpu_preview_renderer.py`, `src/features/edit/ui/program_viewer.py`, `src/features/edit/core/timeline_preview_renderer.py`, 新增 `src/shared/gpu/present_frame.py` |
| 新类型 | `GpuPresentFrame(texture_id, width, height, frame, generation, slot_id, fence, release_cb, color_space, orientation)` |
| Renderer 责任 | 写 output slot -> `glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)` -> `glFlush()` -> emit `GpuPresentFrame` |
| Viewer 责任 | paintGL 中 wait fence -> copy 到 viewer-owned texture -> emit/调用 ack -> release slot |
| Slot 状态 | `FREE -> WRITING -> READY -> IN_VIEWER -> RELEASABLE -> FREE` |
| 禁止 | 禁止裸 `tex_id` 在模块间长期传递；禁止 cache GPU texture id；禁止热路径每帧 `glFinish()` |
| 过渡方案 | 当前 `glFinish()` 保留为 correctness fallback；完成 fence/ack 后删除 |
| 验证 | 60fps 连续播放 5 分钟，slot 未 ack 不复用；`glFinish` 热路径调用次数为 0；无混帧截图 |

验收测试：

| 测试 | 方法 | 通过标准 |
|---|---|---|
| cross-context texture handoff | renderer 线程写彩条纹理，Viewer 线程异步拷贝 | 无半写纹理、无旧帧残留 |
| slot reuse stress | output pool size=3，强制高频渲染 | 未 ack slot 永不复用 |
| pixel sentinel | 每帧写入唯一颜色/编号角标 | 采样结果与 frame/generation 一致 |
| no finish hot path | monkeypatch `glFinish` 计数 | 播放 300 帧计数为 0 |

---

### 3.2 P0-B：把 Edit 播放收敛为唯一 RealtimePlaybackEngine 热路径

目标：避免“新架构写了，但真实播放仍走老路径”。

目标链路：

```text
TimelinePanel / PlaybackController
  -> RealtimePlaybackEngine.request_present(timeline_frame, deadline)
  -> RealtimePlaybackPolicy
  -> SourceResolver(proxy/cache only in realtime)
  -> TimelineDecodeScheduler
  -> FrameRingBuffer
  -> GpuFramePool
  -> TimelineGpuCompositor
  -> PresentQueue
  -> ProgramViewer
```

需要删除或禁用的旧旁路：

| 旧旁路 | 问题 | 处理 |
|---|---|---|
| `TimelinePreviewRenderer._try_gpu_render()` 直接 decode + submit | 绕过 scheduler/pool/compositor | 迁移到 engine 内部，原函数只做代理调用或删除 |
| CPU QImage realtime fallback | 实时路径卡顿、蓝屏、抖动 | realtime 中禁止，改为 hold/rebuffer/placeholder |
| 隐式 master decode fallback | ProRes/4K/多轨卡死 | realtime 禁止 master，paused/step/export 才允许 |
| ProgramViewer 裸 tuple `(tex_id,w,h)` | 无生命周期 | 改为 `GpuPresentFrame` |

验收：

```bash
rg -n "_gpu_renderer\\.submit|EditGpuDecodeManager|get_frame\\(|frame_ready\\.emit\\(frame, result\\)" src/features/edit/core src/features/edit/ui
```

通过标准：

- realtime 播放中只有 `RealtimePlaybackEngine` 能触发 decode 和 present。
- `TimelinePreviewRenderer` 不再直接随机取帧和提交 GPU renderer。
- ProgramViewer 只有一个写入入口：`set_present_frame(GpuPresentFrame)` 或统一 `set_frame()` 分发。

---

### 3.3 P0-C：代理 / 渲染缓存成为 realtime 主 source

目标：拖入时间线后不蓝屏，不等待 master 解码；多轨和 ProRes 不硬扛母版。

| 项 | 内容 |
|---|---|
| Realtime source 优先级 | `RENDER_CACHE -> DISK_PROXY -> PLACEHOLDER/HOLD` |
| 非 realtime source | `MASTER` 只允许 paused/step/export |
| 拖入时间线 | 立即创建 proxy job，显示缩略图/占位/生成进度 |
| Render cache | 对复杂效果、Composite clip、多轨重效果片段生成 cache |
| UI | clip 上显示 proxy generating/ready/cache ready 状态 |

验收：

| 场景 | 通过标准 |
|---|---|
| 拖入新素材 | 100ms 内 ProgramViewer 有非蓝屏画面 |
| proxy 未 ready | 显示 placeholder/thumbnail，不进入 master realtime decode |
| proxy ready | realtime source kind 为 `disk_proxy` |
| 重效果片段 | source kind 为 `render_cache` |
| telemetry | `realtime_master_requests == 0` |

---

### 3.4 P0-D：音频播放引擎重构

目标：解决声音杂音、断裂、A/V 不同步。

| 问题 | 方案 |
|---|---|
| leftover samples 丢失 | 每个 audio stream 使用连续 sample cursor，不按视频帧切片丢弃余样 |
| 声音杂音 | 使用固定格式 float32 interleaved ring buffer；避免重复转换/错位拼接 |
| A/V drift | 建立 audio clock；视频根据 audio clock hold/drop，而不是各跑各的 |
| seek 后爆音 | seek 时 flush ring buffer，做短 fade-in 或 zero-crossing guard |
| 多轨音频 | 统一 mixer，按 timeline source range 拉取样本，混音后进入 output ring |

需要新增/重构：

- `AudioTimelineScheduler`
- `AudioRingBuffer`
- `AudioMixer`
- `AvSyncController`
- `AudioTelemetry`

验收：

| 测试 | 通过标准 |
|---|---|
| 1kHz sine wave | 无爆音、无采样断点 |
| 10 分钟播放 | A/V drift < 20ms |
| seek 100 次 | 无残留旧音频、无爆音 |
| 多轨音频 | peak 不 clipping，混音延迟稳定 |
| underrun 统计 | 正常代理播放 underrun == 0 |

---

## 4. P1 深化优化计划表

| 阶段 | 目标 | 主要修改 | 验证方法 | 完成标准 |
|---|---|---|---|---|
| P1-1 | 固化 layer DTO | 新增 `BackendLayerDTO` / `GpuLayerInput` 严格转换；删除裸 dict/裸 payload 分支 | 单测覆盖 gpu/planar/cache/placeholder | backend 不再接收未知 layer 类型 |
| P1-2 | 强化 `GpuFramePool` cache key | key 纳入 `timeline_id, clip_id, asset_id, source_path, source_frame, source_kind, proxy_key, render_cache_key, pixel_format, size, generation` | 构造同 clip 不同 source/proxy/cache 测试 | 不发生跨 source texture 复用 |
| P1-3 | DecodeScheduler deadline | 每帧 request 带 deadline；超时 hold，不阻塞 UI | 多轨 4 层压力测试 | UI tick 不被 decode 阻塞 |
| P1-4 | 多轨 compositor 正式化 | transform/crop/opacity/blend/color 都在 GPU pass 完成 | 彩色四象限像素测试 | 多轨无残留、顺序准确 |
| P1-5 | GL state guard | 每 pass 显式设置 viewport/scissor/blend/depth/FBO completeness | debug build assert | 尺寸变化/多素材混播无黑边残留 |
| P1-6 | Render cache invalidation | 效果参数、素材、时间线变化时精确失效 cache | 修改参数后播放 | cache 不陈旧、不错帧 |
| P1-7 | Telemetry dashboard | 每帧记录 source_kind/decode_ms/upload_ms/composite_ms/present_ms/audio_drift | benchmark 自动输出 markdown | 每次优化有数据，不靠感觉 |
| P1-8 | 错帧检测 | 每帧携带 source_frame/generation/debug overlay 可选 | 自动播放 seek 压测 | `wrong_source_frame == 0` |
| P1-9 | ProgramViewer 单入口 | 移除直接 texture/qimage 旁路 | `rg` + UI 集成测试 | Viewer 只有统一 frame descriptor 入口 |
| P1-10 | 蓝屏替代策略 | 蓝屏仅 fatal；普通 miss 显示 hold/thumbnail/proxy status | 拖入素材测试 | 用户不再看到蓝屏等待 |

---

## 5. 不能再踩的坑

| 禁止做法 | 为什么不能做 | 正确做法 |
|---|---|---|
| 用 `glFinish()` 当最终方案 | 会强制 GPU/CPU 同步，播放越修越卡 | 使用 fence + ack + slot lifecycle |
| 在模块间传裸 `texture_id` | 不知道谁拥有、何时可复用、是否写完 | 传 `GpuPresentFrame` / `TextureLease` |
| realtime miss 时偷偷 master decode | ProRes/4K/多轨一定卡死 | realtime hold/rebuffer/proxy/cache |
| 缓存 GPU texture id 到 frame cache | texture pool 会复用，cache 命中可能显示错帧 | 只缓存 CPU immutable image 或 cache file descriptor |
| 让测试只检查源码字符串 | 真实 GPU 问题不会被发现 | 加像素级、跨 context、压力播放测试 |
| 每 tick 随机 seek 解码 | 解码器最怕随机 seek，尤其 GOP/ProRes/多轨 | 每 clip 顺序 decode stream + ring buffer |
| 多个模块都能 emit viewer frame | 会出现互相抢帧、stale frame | 统一 PresentQueue |
| source frame release 不等 GPU | IOSurface/CVPixelBuffer 被复用后会混帧 | source lease + fence 后释放 |
| CPU QImage fallback 静默进入实时路径 | 表面“能播”，实际抖动和卡顿 | fallback 必须 telemetry 可见，realtime 默认禁止 |
| 只写文档不接入真实入口 | 看起来像完成，实际播放没变 | 每阶段必须有 runtime grep gate 和 benchmark gate |

---

## 6. 最小可执行任务拆分

建议后续让 AI 按下面顺序逐批完成，每批完成后必须提交一个本地 git 版本。

| 批次 | 名称 | 必做内容 | 验收命令/方法 |
|---|---|---|---|
| Batch 1 | Present descriptor + ack | 新增 `GpuPresentFrame`，ProgramViewer 支持 fence wait + ack，renderer slot 状态机 | `tests/edit/test_program_viewer_shared_texture.py` 扩展；新增 slot reuse stress |
| Batch 2 | 移除 hot path `glFinish` | 用 fence/ack 替代当前临时 `glFinish()` | monkeypatch `glFinish`，播放 300 帧为 0 |
| Batch 3 | 唯一 RealtimePlaybackEngine | `TimelinePreviewRenderer` realtime 只调用 engine，不直接 decode/submit | `rg` gate + 集成测试 |
| Batch 4 | GpuFramePool key 修复 | 强化 cache key，加入 source/proxy/cache/pixel format/size | 新增同帧不同 source 不复用测试 |
| Batch 5 | Proxy/cache source resolver | realtime 只用 disk proxy/render cache；未 ready hold/placeholder | telemetry `realtime_master_requests == 0` |
| Batch 6 | Decode scheduler 接入真实播放 | 多轨 bundle 由 scheduler 产生，deadline 控制 | 多轨 4 层 benchmark |
| Batch 7 | GPU compositor 像素测试 | 彩条/象限/透明度/尺寸变化测试 | pixel diff 无残留 |
| Batch 8 | AudioEngine | ring buffer、mixer、clock、drift correction | sine wave + 10 分钟 A/V drift |
| Batch 9 | Benchmark suite | 统一输出性能报告 | 生成 `docs/playback_benchmark_*.md` |
| Batch 10 | 删除旧路径 | 删除/隔离旧 CPU realtime fallback 和裸 texture 旁路 | `rg` gate 全通过 |

---

## 7. 最终专业 NLE 级验收指标

| 类别 | 指标 |
|---|---|
| 拖入素材 | 100ms 内有非蓝屏画面；proxy/cache 状态可见 |
| 单轨代理播放 | 1080p/4K proxy 按时间线 fps 稳定实时，无跳帧/错帧 |
| 多轨代理播放 | 4 层 1080p proxy 仍可稳定 preview；超预算时明确降质/hold，不错帧 |
| ProRes | realtime 不硬扛 master；proxy/cache ready 后流畅；paused/step 能 exact master |
| 混帧 | 0 次半帧/叠帧/其它帧块状残留 |
| 错帧 | `wrong_source_frame == 0` |
| stale frame | `stale_generation_presented == 0` |
| CPU fallback | realtime `cpu_qimage_frames == 0`，除非显式 debug/fallback 模式 |
| GPU readback | realtime `readback_count == 0` |
| GPU sync | 最终热路径 `glFinish == 0` |
| 音频 | 10 分钟 drift < 20ms；无 underrun；seek 无爆音 |
| present slot | 未 ack slot 复用次数 == 0 |
| 测试 | 单元、集成、GPU pixel、cross-context、benchmark 全部覆盖 |

---

## 8. 推荐的验证命令

```bash
.venv/bin/python -m pytest tests/edit/test_timeline_gpu_compositor.py tests/edit/test_gpu_io_surface_sync.py -q
.venv/bin/python -m pytest tests/edit/test_edit_realtime_regression.py -q
.venv/bin/python -m pytest tests/edit/test_timeline_decode_scheduler.py tests/edit/test_render_cache_adapter.py tests/edit/test_realtime_playback_policy.py -q
rg -n "glFinish|ctx\\.finish|fbo\\.read|Framebuffer\\.read|to_ndarray|frame\\.transfer" src/features/edit src/shared
rg -n "_gpu_renderer\\.submit|EditGpuDecodeManager|get_frame\\(" src/features/edit/core src/features/edit/ui
rg -n "set_shared_texture\\(|texture_id|tex_id" src/features/edit src/shared
```

解释：

- 第一组测试守住当前已修复的 compositor/present 止血问题。
- 第二组测试守住 Stage A-H 的实时播放基础不变量。
- 第三组测试守住 proxy/cache/policy/scheduler。
- `rg glFinish` 用于确认最终热路径没有全局 GPU 阻塞。
- `rg _gpu_renderer.submit` 用于确认旧播放旁路被删除。
- `rg set_shared_texture/tex_id` 用于逐步替换裸 texture id 传递。

---

## 9. 下一步建议

第一优先级不是继续调参数，而是完成 `P0-A Present descriptor + fence + ack`。

原因：只要 renderer 和 ProgramViewer 之间仍然传裸 texture id，且没有明确的“写完/读完/可复用”协议，混帧、半帧、偶发错帧就会反复出现。当前 `glFinish()` 可以临时止血，但会牺牲播放性能；它证明了问题方向，也提醒我们必须把正式 fence/ack 协议补上。

完成 `P0-A` 后，再做 `P0-B` 把 realtime engine 接成唯一热路径。否则继续优化新模块，真实播放可能仍然走旧链路，用户体感不会变。
