# Topos Color 真正专业 NLE 实时播放架构最终差距与实施计划

日期：2026-06-14  
目标：把当前 Edit 时间线播放从“已有专业架构外壳 + 局部止血”推进到“真正接近 DaVinci Resolve / Premiere / Final Cut 的专业 NLE 实时播放模型”。  
结论：**当前还没有完美实现真正专业 NLE 架构**。目前已经完成了若干关键基础设施，但仍有核心热路径、音频时钟、真实代理/缓存闭环、GPU 像素级验收和长时间播放稳定性没有完成。

---

## 1. 当前状态结论

### 1.1 已经变好的部分

当前仓库最近已经完成了这些重要基础：

| 已完成项 | 状态 | 说明 |
|---|---|---|
| `GpuPresentFrame + TextureLease + OutputSlotPool` | 已有 | renderer -> Viewer 不再只能靠裸 `tex_id`；已具备 fence/ack/slot lifecycle 模型。 |
| `ProgramViewer` present 接线 | 已修复 | 已修复之前 descriptor 被降级成 `(tex_id,w,h)` 的问题，Viewer 可以走 `set_present_frame()`。 |
| output slot 未 ack 不复用 | 已有基础 | `OutputSlotPool` 能阻止未 ack slot 被复用，避免显示半写或被覆盖纹理。 |
| 热路径 `glFinish` 守门测试 | 已有 | `tests/edit/test_no_glfinish_hotpath.py` 约束正常 fence 路径不调用 `glFinish()`。 |
| `RealtimePlaybackEngine` 外壳 | 已有 | 所有播放 tick 可以经过 `request_present()`，有 mode/state/miss/budget 统计。 |
| `SourceResolver` / `RenderCacheAdapter` / `TimelineDecodeScheduler` / `GpuFramePool` | 已有模块 | 已具备专业 NLE 所需的若干核心模块雏形。 |
| `AudioRingBuffer` | 已有单元模块 | 已有 float32 ring buffer 和 underrun 统计基础。 |
| 架构 gate 测试 | 已有 | `tests/edit/test_playback_architecture_gates.py` 能防止部分回退。 |

本次审计已跑过：

```bash
.venv/bin/python -m pytest \
  tests/edit/test_playback_architecture_gates.py \
  tests/edit/test_realtime_engine_hotpath.py \
  tests/edit/test_present_frame_integration.py \
  tests/edit/test_no_glfinish_hotpath.py \
  tests/edit/test_timeline_decode_scheduler.py \
  tests/edit/test_audio_ring_buffer.py -q
```

结果：`80 passed in 0.83s`。

### 1.2 仍未达到专业 NLE 的原因

测试全绿不等于架构完成。当前最关键的问题是：

1. `RealtimePlaybackEngine` 仍然是委托层，不是真正拥有 decode/source/composite/present 的主引擎。
2. `TimelineDecodeScheduler`、`SourceResolver`、`GpuFramePool`、`TimelineGpuCompositor` 有模块，但没有完全成为真实播放热路径。
3. `_try_gpu_render()` 仍在 `TimelinePreviewRenderer` 里直接调用 `EditGpuDecodeManager.get_frame()`，仍像旧架构。
4. 音频仍以 `AudioPlaybackManager + AudioPlaybackThread + QAudioSink` 为主体，新 `AudioRingBuffer` 还没接成完整 audio engine。
5. 没有真实素材级 benchmark/pixel test/long-run test 证明 ProRes、多轨、长时间播放、seek、A/V sync 达标。
6. 仍保留多条兼容/legacy 路径，例如 QImage fallback、legacy texture tuple、直接 `request_render()`，需要按模式严格收口。

因此，当前状态可以定义为：

> 已经修到“能继续重构的正确方向”，但还不是“真正专业 NLE 的完成态”。

---

## 2. 专业 NLE 完成态定义

真正完成后，Edit 实时播放热路径必须是：

```text
PlaybackController / TimelinePanel
  -> RealtimePlaybackEngine.request_present(frame, deadline)
  -> SourceResolver.resolve_layers()
      RENDER_CACHE -> DISK_PROXY -> HOLD/PLACEHOLDER
      realtime 禁止 MASTER
  -> TimelineDecodeScheduler.request_bundle()
      per clip 顺序 decode stream
      ring buffer
      generation 隔离
      deadline / cancel / back-pressure
  -> GpuFramePool.ensure_gpu_frame()
      cache key 完整
      GPU resident frame lifecycle
  -> TimelineGpuCompositor.submit()
      transform/crop/opacity/blend/color 全 GPU pass
      不 CPU readback
  -> GpuPresentFrame
      fence + slot_id + generation + orientation + color metadata
  -> ProgramViewer.set_present_frame()
      wait fence
      blit to viewer-owned texture
      ack slot
  -> AudioEngine / AVSyncController
      audio clock 或 unified monotonic clock
      video hold/drop 服从时钟
```

实时播放禁止：

- 禁止实时路径静默 master decode。
- 禁止实时路径 QImage 合成/发射。
- 禁止实时路径 CPU readback。
- 禁止缓存裸 `texture_id`。
- 禁止未 ack output slot 复用。
- 禁止每帧 `glFinish()`。
- 禁止每帧随机 seek 解码。
- 禁止音频 callback 里临时 seek/decode。

---

## 3. 关键差距总表

| ID | 优先级 | 当前差距 | 证据/位置 | 风险 | 完成标准 |
|---|---:|---|---|---|---|
| FINAL-P0-01 | P0 | `RealtimePlaybackEngine` 仍是委托旧 renderer | `engine.py` 注释写明“渐进式：delegate TimelinePreviewRenderer”；`_execute_delegate()` 调 `renderer.request_render()` | 新架构存在，但真实 decode/composite 仍可走旧逻辑 | engine 内部直接驱动 source/scheduler/gpu pool/compositor/present |
| FINAL-P0-02 | P0 | `TimelineDecodeScheduler` 未真正接管热路径 | 真实 `_try_gpu_render()` 仍直接 `EditGpuDecodeManager.get_frame()` | 多轨/ProRes 仍可能随机取帧、阻塞、抖动 | `_try_gpu_render` 不再直接 decode，frame bundle 全由 scheduler 提供 |
| FINAL-P0-03 | P0 | `SourceResolver` 未成为每帧 source 决策唯一入口 | engine 未构造 `LayerSourceQuery` 并调用 `resolve_layers()` | realtime 可能仍绕过 proxy/cache/source policy | 每个 layer 都有 `SourceDecision`，telemetry 记录 source_kind |
| FINAL-P0-04 | P0 | `GpuFramePool` 还不是真实上传/驻留唯一入口 | direct GPU frame/planar frame 仍直接被 backend 处理 | GPU resident 生命周期分散，cache key 再强也不发挥作用 | 所有 decoded frame 进入 compositor 前都经过 pool |
| FINAL-P0-05 | P0 | `TimelineGpuCompositor` 仍是 wrapper，不是真正主合成器 | 真实提交仍多处直接 `_gpu_renderer.submit(EditRenderTask)` | transform/crop/blend/color 管理分散，难保证像素正确 | compositor 成为唯一 submit API，backend 只做 GL executor |
| FINAL-P0-06 | P0 | 音频不是完整 professional audio engine | `AudioRingBuffer` 仅单元模块；`AudioPlaybackManager` 仍负责 QAudioSink 线程 | 杂音、underrun、A/V drift 仍可能出现 | AudioScheduler + AudioRingBuffer + AudioClock + AVSyncController 实际接入 |
| FINAL-P0-07 | P0 | 真实素材 benchmark 缺失 | 当前测试多为 mock/unit | “感觉快/不快”无法定位 | 自动 benchmark 输出 decode/render/present/audio 指标 |
| FINAL-P0-08 | P0 | GPU pixel / cross-context 验收不足 | 现有测试检查源码/状态机，非真实像素 | 混帧、倒置、残帧可能再次回归 | 离屏/真实 GL pixel sentinel 测试 |
| FINAL-P0-09 | P0 | realtime QImage/CPU fallback 仍存在兼容入口 | `ProgramViewerWidget.set_frame()` 支持 QImage/tuple；`TimelinePreviewRenderer` 多处 `frame_ready.emit(image.copy())` | realtime 卡顿、路径不纯 | REALTIME 模式 QImage count 必须为 0；仅 PAUSED/STEP/EXPORT 可用 |
| FINAL-P1-10 | P1 | Legacy texture tuple 未完全淘汰 | `as_legacy_tuple()` 和 `set_shared_texture()` 仍保留 | 后续改动可能绕过 fence/ack | realtime 禁用 tuple，legacy 只在测试/非实时 fallback |
| FINAL-P1-11 | P1 | Render cache 不是自动生成闭环 | `RenderCacheAdapter` 可查 cache，但生成/失效/预热不完整 | 复杂效果和 comp clip 仍退回慢路径 | cache job graph + invalidation + timeline dirty region |
| FINAL-P1-12 | P1 | 拖入素材 immediate playback 未彻底闭环 | proxy ensure 模块存在，但 UI/worker 状态闭环不完整 | 仍可能蓝屏等待 | 100ms 内显示 thumbnail/hold frame，proxy 生成进度可见 |
| FINAL-P1-13 | P1 | A/V master 策略不统一 | EditPage 当前描述中“video controller remains master” | 声画不同步时没有统一校正策略 | 统一 clock policy：audio present 时 audio/master monotonic 驱动视频 |
| FINAL-P1-14 | P1 | 混合帧率/变速/倒放未和 scheduler 统一 | 现有 mapper 有测试，但 scheduler/source bundle 未完整接入 | VFR、23.976/25、speed ramp 可能错帧 | source frame mapping 在 scheduler 内统一，10000 帧无 drift |
| FINAL-P1-15 | P1 | 真实多轨合成效果未像素验收 | compositor unit 只测数据结构 | opacity/crop/transform 顺序可能错 | 多轨 checkerboard/alpha/crop pixel diff |
| FINAL-P2-16 | P2 | 文档和实现仍可能不同步 | 多份计划表、实现分批推进 | AI 后续容易重复小修小补 | 建立“完成定义 gate”，所有 PR/commit 必须跑 |

---

## 4. 详细实施计划

### Phase A：把 RealtimePlaybackEngine 从委托层升级为真实主引擎

目标：engine 不再只是调用 `TimelinePreviewRenderer.request_render()`，而是直接完成 source -> scheduler -> gpu pool -> compositor -> present 的 orchestration。

| 项 | 内容 |
|---|---|
| 涉及文件 | `src/features/edit/core/realtime_playback/engine.py`, `timeline_preview_renderer.py`, `source_resolver.py`, `timeline_decode_scheduler.py` |
| 删除/迁移 | `_engine_delegate_render()` 只作为 fallback；真实播放不用 `_try_gpu_render()` |
| 新增 | `RealtimePlaybackEngine.configure_timeline(sequence, asset_resolver, gpu_compositor, gpu_pool, scheduler)` |
| 新增 | `RealtimePlaybackEngine.build_layer_queries(timeline_frame)` |
| 新增 | `request_present()` 内部调用 `SourceResolver.resolve_layers()` |
| 新增 | `TimelineDecodeScheduler.request_bundle(decisions, deadline_ns)` |

实施步骤：

1. 在 engine 中保存 timeline/asset resolver 引用。
2. 将 `TimelinePreviewRenderer` 中收集 `ClipLayerInfo` 的逻辑抽到可复用 `TimelineLayerPlanner`。
3. engine 每帧生成 `LayerSourceQuery`，调用 `SourceResolver`。
4. 如果任一 realtime layer 返回 `PLACEHOLDER/NONE`，engine hold 当前帧并触发 proxy/cache job，不进入旧 decode。
5. 如果全部 ready，engine 调 `TimelineDecodeScheduler` 取 `LayerFrameBundle`。
6. bundle complete 才提交 GPU compositor。
7. 删除播放模式下 direct `request_render()` 调用。

验收测试：

| 测试 | 方法 | 通过标准 |
|---|---|---|
| engine source gate | mock timeline 两层，proxy ready/cache ready/placeholder 三种状态 | 每层都有 SourceDecision，source_kind 正确 |
| no delegate hot path | monkeypatch `_engine_delegate_render` 抛异常 | REALTIME 播放仍可走新 engine |
| no direct decode | monkeypatch `EditGpuDecodeManager.get_frame` 抛异常 | REALTIME proxy/cache ready 路径不触发 |
| telemetry | 播放 300 帧 | `source_kinds` 非空，`delegate_miss` 不再出现 |

grep gate：

```bash
rg -n "_engine_delegate_render|_try_gpu_render|EditGpuDecodeManager|get_frame\\(" src/features/edit/core
```

完成标准：

- REALTIME 热路径不再依赖 `_try_gpu_render()`。
- `RealtimePlaybackEngine.request_present()` 是真实主路径，不是外壳。

---

### Phase B：让 TimelineDecodeScheduler 真正接管多轨解码

目标：解决 ProRes、一轨/多轨卡顿、随机 seek、错帧和预缓冲不足。

| 项 | 内容 |
|---|---|
| 涉及文件 | `timeline_decode_scheduler.py`, `clip_decode_stream.py`, `frame_ring_buffer.py`, `timeline_frame_mapper.py` |
| 核心 | 每个 timeline clip 一个 decode stream，顺序读取，ring buffer 缓存 |
| realtime | 只读 disk proxy/render cache，不读 master |
| paused/step | 允许 exact master，但不能污染 realtime stream |
| miss | hold/rebuffer，不展示 wrong frame |

实施步骤：

1. 定义 `StreamKey(timeline_id, clip_id, source_kind, source_path, proxy_key/cache_key, target_size)`。
2. 对每个 active clip 建立 `ClipDecodeStream`。
3. `request_bundle(timeline_frame)` 同时查询所有 visible layer。
4. 每个 layer 必须 exact source frame，不能拿相邻帧充数。
5. 根据 playback direction 做 forward/backward prefetch window。
6. seek/generation 变化时取消旧 stream 请求。
7. 对 ProRes master：REALTIME 不直接解；如果无 proxy/cache，返回 placeholder/hold。

验收测试：

| 测试 | 方法 | 通过标准 |
|---|---|---|
| exact multi-layer bundle | 三轨不同 clip/source_frame | bundle 中每层 source_frame 精确 |
| generation cancel | seek 后旧 generation late frame 到达 | 不缓存、不 present |
| ProRes realtime gate | ProRes 无 proxy/cache | 不调用 master decode，返回 hold/proxy_required |
| ring hit ratio | 连续播放 1000 帧 proxy | ring hit > 95%，随机 seek 次数 0 |
| reverse playback | 倒放 300 帧 | source_frame 单调递减且 exact |

benchmark：

```bash
.venv/bin/python tools/benchmarks/playback_benchmark.py \
  --videos tests/test_videos \
  --scenario single_proxy,multi_proxy,prores_no_proxy,seek_stress \
  --out docs/playback_benchmark_final_phase_b.md
```

完成标准：

- REALTIME 每帧不随机打开/seek decoder。
- ProRes 只有 proxy/cache ready 才 realtime。

---

### Phase C：GpuFramePool + TimelineGpuCompositor 成为唯一 GPU 合成入口

目标：所有画面帧进入 Viewer 前都有统一 GPU 生命周期、统一合成、统一 present。

| 项 | 内容 |
|---|---|
| 涉及文件 | `gpu_frame_pool.py`, `timeline_gpu_compositor.py`, `edit_gpu_preview_renderer.py` |
| 核心 | backend 只作为 GL executor，不再直接接收旧 dict layer |
| 输入 | `TimelineGpuRenderTask(layers=GpuLayerInput...)` |
| 输出 | `GpuPresentFrame` |
| 禁止 | backend 外部直接传 `EditRenderTask` |

实施步骤：

1. `GpuFramePool.ensure_gpu_frame(FrameHandle)` 成为 frame -> GPU resident 的唯一入口。
2. `TimelineGpuCompositor` 负责 layer order、opacity、crop、transform、blend mode。
3. backend `EditGpuPreviewRenderer` 只暴露 `_execute_gpu_task(RenderGraphTask)` 或内部 executor。
4. 删除 `TimelinePreviewRenderer` 对 `_gpu_renderer.submit(EditRenderTask)` 的直接调用。
5. `TimelineGpuCompositor.present_frame_ready` 成为唯一 frame-ready GPU 输出。
6. 所有 source frame release 绑定 fence/slot，不用全局 `_previous_gpu_frames`。

验收测试：

| 测试 | 方法 | 通过标准 |
|---|---|---|
| no direct backend submit | grep | hot path 无 `_gpu_renderer.submit(EditRenderTask)` |
| GPU pool key | 同 clip/frame 不同 proxy/cache/source_kind | 不复用错误 texture |
| compositor pixel alpha | 两层彩条 opacity 0.5 | pixel diff < 1/255 |
| crop/transform | 四角标记图做 crop/scale/position | 像素坐标正确 |
| source release fence | IOSurface fake release before fence | release 被延后到 fence/ack 后 |

完成标准：

- GPU 合成只存在一条入口。
- 任何 mixed frame / stale frame 都能被 source identity + generation + slot 检测出来。

---

### Phase D：彻底收口 Present 协议，淘汰实时裸 tuple

目标：防止 `GpuPresentFrame` 再次被降级，杜绝混帧/半帧/旧帧残留。

当前已经修复：

- `ProgramViewerWidget.set_frame()` 可识别 `{"present_frame": ..., "ack_cb": ...}`。
- `ProgramViewer._copy_shared_texture_to_display()` 会 wait fence、清目标、ack slot。
- present slot 池满时 renderer 不复用旧 slot。

仍需完成：

| 项 | 内容 |
|---|---|
| 禁止 REALTIME tuple | REALTIME 模式下 `(tex_id,w,h)` 视为测试/legacy，不准进入 Viewer |
| 删除 `as_legacy_tuple` 热路径 | 保留仅用于过渡测试 |
| 单一信号 | renderer 不再同时 emit `present_frame_ready` 和 legacy `frame_ready` 两套结果 |
| pixel sentinel | 每帧写入 frame id 角标，Viewer 采样验证 |

验收测试：

```bash
rg -n "as_legacy_tuple\\(|set_shared_texture\\(|frame_ready\\.emit\\(.*tex_id" \
  src/features/edit/core src/features/edit/ui
```

通过标准：

- REALTIME present 只能是 `GpuPresentFrame`。
- `set_shared_texture()` 不被 Edit realtime 调用。
- 未 ack slot 复用次数为 0。

---

### Phase E：音频重构为真正 AudioEngine + AVSyncController

目标：彻底解决声音杂音、断裂、A/V 对不上。

当前状态：

- `AudioPlaybackManager`、`AudioMixer`、`AudioPlaybackThread` 已存在。
- `AudioRingBuffer` 已存在并有单元测试。
- 但 `AudioRingBuffer` 还未成为 QAudioSink 的实际供给中心。
- EditPage 里仍有“播放时 video controller remains master / audio frame ignored”的逻辑。

最终架构：

```text
AudioTimelineScheduler
  -> per audio clip decode stream
  -> resampler to project sample rate
  -> AudioMixer
  -> AudioRingBuffer
  -> QAudioSink pull/read
  -> AudioClock
  -> AVSyncController
  -> video hold/drop/reanchor
```

实施步骤：

1. 新增 `AudioTimelineScheduler`：按 timeline audio clips 顺序生成 sample jobs。
2. `AudioPlaybackThread` 不再在 callback 附近 seek/decode；它只从 `AudioRingBuffer` 读。
3. `AudioMixer` 输出固定 `float32 interleaved`。
4. `AudioClock` 从实际写入/播放 sample position 推导，不靠视频 frame。
5. `AVSyncController` 比较 audio time 与 displayed video frame。
6. drift 小于 20ms 不动；20-80ms 轻微 hold/drop；超过阈值 rebuffer。
7. seek 时 flush ring，短 fade-in，禁止旧 audio residue。

验收测试：

| 测试 | 方法 | 通过标准 |
|---|---|---|
| sine continuity | 1kHz sine 连续播放模拟 | sample discontinuity 为 0 |
| underrun | 10 分钟模拟输出 | normal proxy playback underrun == 0 |
| seek pop | 100 次随机 seek | 无旧样本残留，fade-in 生效 |
| drift | 25fps + 48kHz 播放 10 分钟 | A/V drift < 20ms |
| multi-track mix | 4 轨音频混音 | 不 clipping，mute/solo/volume/pan 正确 |

完成标准：

- QAudioSink 输出不触发同步 seek/decode。
- 音频不再有杂音和跟不上画面的主因。

---

### Phase F：代理 / 渲染缓存闭环

目标：拖入时间线后立即能看见画面，复杂片段走 render cache，ProRes 不硬解实时。

| 子项 | 实施 |
---|---|
| asset proxy job | import/drag 到 timeline 时触发 `ensure_proxy_async` |
| placeholder | proxy/cache 未 ready 显示 thumbnail/last good frame |
| render cache | 有重效果/comp clip/非实时效果时生成 cache |
| cache invalidation | 参数、clip、source、timeline fps、resolution 改变时失效 |
| UI 状态 | clip 上显示 proxy/cache generating/ready/failed |

验收：

| 场景 | 通过标准 |
|---|---|
| 拖入素材 | 100ms 内 ProgramViewer 非蓝屏 |
| ProRes 无 proxy | 不 master realtime decode，显示 hold/proxy generating |
| proxy ready | source_kind=`disk_proxy` |
| 重效果 | source_kind=`render_cache` |
| cache stale | 参数修改后不会显示旧 cache |

---

### Phase G：真实 benchmark 与像素验收

目标：不再靠“感觉快/不快”，每次优化都有数据。

新增工具：

- `tools/benchmarks/playback_benchmark.py`
- `tools/benchmarks/gpu_present_pixel_test.py`
- `tools/benchmarks/audio_avsync_benchmark.py`

benchmark 场景：

| 场景 | 输入 | 指标 |
|---|---|---|
| single_proxy | 单轨 H264/H265 proxy | fps、p95 frame time、present latency |
| multi_proxy_4track | 4 轨 1080p proxy | drop/hold、GPU composite ms |
| prores_no_proxy | ProRes master 无 proxy | 不 master realtime decode，hold/proxy status |
| prores_proxy_ready | ProRes + proxy | 稳定实时 |
| seek_stress | 100 次随机 seek | stale frame=0，slot leak=0 |
| frame_step | 逐帧 300 次 | exact frame=100%，无混帧 |
| av_10min | 10 分钟音视频 | drift、underrun、glFinish/readback |

核心指标：

| 指标 | 目标 |
|---|---:|
| `wrong_source_frame_presented` | 0 |
| `stale_generation_presented` | 0 |
| `mixed_frame_detected` | 0 |
| `unacked_slot_reuse` | 0 |
| realtime `glFinish_count` | 0 |
| realtime `readback_count` | 0 |
| realtime `cpu_qimage_frames` | 0 |
| realtime `master_requests` | 0 |
| normal proxy audio underrun | 0 |
| 10min A/V drift | < 20ms |
| frame step exactness | 100% |

---

## 5. 最终开发批次安排

| 批次 | 名称 | 目标 | 必须提交 |
|---|---|---|---|
| Final-1 | Engine owns source decisions | engine 每帧调用 SourceResolver，禁止旧 source 绕路 | `engine/source resolver hot path` |
| Final-2 | Scheduler owns decode | TimelineDecodeScheduler 替代 `_try_gpu_render` 直接 decode | `scheduler real hot path` |
| Final-3 | GPU pool/compositor owns render | GpuFramePool + TimelineGpuCompositor 成为唯一 GPU 合成入口 | `gpu compositor real hot path` |
| Final-4 | Present protocol cleanup | REALTIME 禁止 tuple/set_shared_texture/as_legacy_tuple | `present protocol single path` |
| Final-5 | AudioEngine | AudioRingBuffer 接入 QAudioSink + AVSyncController | `audio engine av sync` |
| Final-6 | Proxy/cache product loop | proxy/cache 生成、状态 UI、失效规则 | `proxy cache realtime source loop` |
| Final-7 | Benchmark suite | 真实素材自动压测报告 | `playback benchmark suite` |
| Final-8 | Delete legacy hot paths | 删除或隔离旧 realtime fallback | `remove legacy realtime paths` |

每批必须满足：

1. 有单元测试。
2. 有集成测试。
3. 有 grep gate。
4. 有真实素材 benchmark 或明确说明不可跑原因。
5. 提交本地 git 版本。

---

## 6. 防止 AI 后续继续小修小补的硬性规则

### 6.1 不允许的做法

| 禁止 | 原因 |
|---|---|
| 继续在 `_try_gpu_render()` 里加补丁 | 这是旧热路径，继续补会越来越复杂 |
| 用 `glFinish()` 修混帧 | 会牺牲吞吐，不是专业方案 |
| 用 QImage 修 realtime 画面 | 会导致 CPU copy/upload 和卡顿 |
| 无 proxy 时偷偷解 master | ProRes/4K/多轨必卡 |
| 只加源码字符串测试 | 无法证明真实 GPU/音频正确 |
| 只写模块不接入 | 之前的问题就是模块存在但热路径没用 |
| 让音频跟视频各跑各的 | 必然出现 drift 或杂音 |
| 直接缓存 texture id | output pool 复用会导致错帧 |

### 6.2 每次提交前必须跑

基础：

```bash
.venv/bin/python -m pytest \
  tests/edit/test_playback_architecture_gates.py \
  tests/edit/test_realtime_engine_hotpath.py \
  tests/edit/test_present_frame_integration.py \
  tests/edit/test_no_glfinish_hotpath.py \
  tests/edit/test_timeline_decode_scheduler.py \
  tests/edit/test_audio_ring_buffer.py -q
```

新增阶段必须逐步加入：

```bash
.venv/bin/python -m pytest tests/edit/test_engine_source_resolver_hotpath.py -q
.venv/bin/python -m pytest tests/edit/test_scheduler_real_hotpath.py -q
.venv/bin/python -m pytest tests/edit/test_gpu_compositor_pixel_output.py -q
.venv/bin/python -m pytest tests/edit/test_audio_engine_avsync.py -q
.venv/bin/python -m pytest tests/edit/test_realtime_playback_benchmark_gates.py -q
```

grep gate：

```bash
rg -n "_try_gpu_render|EditGpuDecodeManager|get_frame\\(|_gpu_renderer\\.submit\\(" src/features/edit/core
rg -n "as_legacy_tuple\\(|set_shared_texture\\(|frame_ready\\.emit\\(.*tex_id" src/features/edit/core src/features/edit/ui
rg -n "glFinish|ctx\\.finish|fbo\\.read|Framebuffer\\.read|to_ndarray|frame\\.transfer" src/features/edit src/shared
```

最终通过标准：

- 第一条 grep 在 REALTIME hot path 中无命中。
- 第二条 grep 仅允许 SourceViewer 或非 realtime fallback。
- 第三条 grep 仅允许测试、自检、导出或明确非实时路径。

---

## 7. 当前是否“完美”的最终判定

不是。

当前更准确的阶段名称是：

> Professional NLE architecture scaffold with critical present-path fixes.

也就是：专业 NLE 架构骨架和关键 present 修复已经具备，但真正的专业 NLE 热路径还没有完全落地。

距离“完美接近真正专业 NLE”还必须完成：

1. engine 真实拥有 source/scheduler/compositor。
2. scheduler 真实替代直接 decode。
3. compositor/pool 真实成为唯一 GPU 合成入口。
4. audio engine 真实接入 ring buffer 和 AV sync。
5. proxy/cache 真实成为 realtime source 闭环。
6. 真实素材 benchmark 和 pixel/AV 测试成为发布门槛。

完成这 6 件事后，才能说它从“修补式播放系统”转为“专业 NLE 实时播放模型”。
