# Topos Color 专业 NLE 实时播放架构彻底重构计划

创建日期：2026-06-13  
适用范围：Edit 时间线播放、Program Viewer、Source Viewer、音频播放、多轨合成、代理/优化媒体、Render Cache、Color/Composite 共享媒体核心。  
目标：把当前“按帧请求式预览播放”重构为“专业 NLE 实时播放模型”，对齐 DaVinci Resolve、Premiere Pro、Final Cut Pro 的核心播放架构思路。

> 重要说明：专业 NLE 不是靠“所有原始素材永远实时硬算”实现丝滑播放，而是靠统一播放时钟、预解码队列、代理/优化媒体、Render Cache、GPU 常驻纹理、严格背压和自动降级策略组合实现。本文档的目标是让 Topos Color 建立同一类架构，而不是继续在旧 `TimelinePreviewRenderer` 上打补丁。

---

## 1. 当前结论

### 1.1 一句话判断

当前播放问题以架构问题为主，局部代码问题为辅。  

现状不是专业 NLE 播放引擎，而是：

```text
UI 播放 tick
  -> 临时计算当前时间线帧
  -> 临时解码素材帧
  -> 临时多轨合成
  -> 临时上传/显示
  -> 音频另一路播放
```

这个模型在单轨、缓存命中、低分辨率时可能可用，但多轨视频必然出现跳帧、抖动、首帧蓝屏、音频杂音或卡死。

### 1.2 当前用户可见问题与根因映射

| 用户现象 | 直接触发点 | 根本原因 | 架构级解决方案 |
|---|---|---|---|
| 素材拖到时间线后先蓝屏 | 首帧播放时才打开/探测/解码 | 没有 timeline preroll 和 first-frame guarantee | 拖拽/放置后异步 open + probe + decode first frame + viewer placeholder + ready signal |
| 播放需要等待几秒 | 解码器、proxy、GPU 纹理在播放时才初始化 | 热路径承担初始化成本 | MediaSession 生命周期提前，播放前必须 preroll 成功 |
| 多轨视频跳帧 | 播放头按墙钟前进，渲染结果异步迟到 | 没有稳定的 video ready queue 和显示截止时间策略 | 音频主时钟 + 视频队列 + late frame drop，不显示过期旧帧 |
| 多轨画面来回抖动 | 不同轨道解码 seek 互相干扰或结果乱序 | 每帧临时合成，缺少帧序号、generation、deadline、track stream 隔离 | 每轨独立 decode stream，composite job 使用同一 timeline frame 的完整 layer bundle |
| 多轨卡死 | 当前帧没完成，后续 tick 继续堆积或互相等待 | 单帧渲染链和播放控制耦合，背压策略不完整 | PlaybackEngine 控制 queue depth，进入 buffering 或降级，不阻塞 UI |
| 声音杂音/爆音 | 视频背压时频繁 seek 音频或音频缓冲 underrun | 音频不是唯一连续主时钟，视频反向影响音频 | Audio master clock，播放中不 live seek 音频，视频追随音频 |
| 声画不同步 | 视频迟到，音频继续走，补偿方式粗暴 | 没有统一 PlaybackClock 和 drift policy | 音频连续，视频按 clock 选择最近 ready frame，必要时丢视频帧 |
| 低负载时偶尔也卡 | UI tick、渲染、解码、上传互相抢线程 | 热路径过多在 Python/Qt/UI 链路发生 | 控制面留 Python，热路径迁移到 worker/GPU/native |

### 1.3 不能继续采用的方向

| 方向 | 为什么不够 |
|---|---|
| 继续给 `TimelinePreviewRenderer` 加 if/else | 会让状态机更复杂，但仍是按帧临时请求模型 |
| 播放卡顿时 seek 音频追视频 | 会造成杂音，且专业 NLE 通常保持音频连续 |
| 当前帧渲染慢就跳到最新帧 | 会造成画面前后跳，尤其多轨 |
| 后台预取复用当前播放解码会话 | 会移动解码游标，造成多轨错帧和抖动 |
| 没有数据就优化某个 codec | 可能修错层，之前 benchmark 已显示 upload/同步也是关键瓶颈 |

---

## 2. 目标架构

### 2.1 目标数据流

```text
Timeline Edit Model
  -> TimelinePlaybackGraph
  -> PlaybackEngine
       -> AudioEngine / AudioClock
       -> TrackDecodeStream per visible video layer
       -> PrerollManager
       -> VideoReadyQueue
       -> CompositeScheduler
       -> GPU Composition Engine
       -> PresentQueue
       -> ProgramViewer
```

播放时热路径应该变成：

```text
Audio callback / Audio clock
  -> PlaybackClock time
  -> choose due timeline frame
  -> consume already decoded frames
  -> composite or consume cached composite frame
  -> present at deadline
```

而不是：

```text
timer tick
  -> decode now
  -> composite now
  -> upload now
  -> maybe display late frame
```

### 2.2 核心原则

| 原则 | 要求 |
|---|---|
| 音频主时钟 | 播放中音频连续，视频追随音频；禁止频繁 seek live audio |
| 播放前 preroll | 开始播放前至少准备首帧、音频缓冲、N 帧视频窗口 |
| 每轨独立解码流 | 多轨同素材不同时间位置不能共享解码游标 |
| 队列驱动 | 播放消费 ready frames，不在 UI tick 中同步解码 |
| 严格 deadline | 过期视频帧丢弃，不倒退显示旧帧 |
| 自动降级 | 复杂多轨超预算时降 proxy 或使用 render cache，而不是硬卡 |
| GPU 常驻 | 尽量避免 CPU QImage、numpy packed RGB、每帧同步 texture.write |
| 可观测 | 每帧记录 decode/upload/composite/present/audio drift |
| 可回滚 | 新引擎先 feature flag 接入，不一次性砸掉旧路径 |

### 2.3 目标状态机

```text
STOPPED
  -> LOADING_MEDIA
  -> READY
  -> PREROLLING
  -> PLAYING
  -> BUFFERING
  -> SEEKING
  -> PAUSED
  -> STOPPED
```

| 状态 | 允许行为 | 禁止行为 |
|---|---|---|
| READY | 显示当前帧、后台预热 | 直接开始播放未准备素材 |
| PREROLLING | 打开素材、填充队列、准备音频 | 推动播放头前进 |
| PLAYING | 音频连续、视频按 deadline present | live audio seek、UI 线程解码 |
| BUFFERING | 暂停视频推进或降级质量 | 继续堆积无限 render request |
| SEEKING | 清空旧 generation 队列，按目标帧重建 | 旧帧晚到后覆盖新帧 |
| PAUSED | 可请求 master quality 当前帧 | 继续后台抢占播放 decode |

---

## 3. 新模块设计

### 3.1 模块总览

| 模块 | 职责 | 新增/迁移位置 |
|---|---|---|
| `PlaybackEngine` | 播放总控状态机、时钟、队列调度 | `src/features/edit/playback/engine.py` |
| `PlaybackClock` | 音频主时钟、暂停/seek/rate 管理 | `src/features/edit/playback/clock.py` |
| `TimelinePlaybackGraph` | 把 timeline frame 映射成每轨 source frame 和效果 hash | `src/features/edit/playback/graph.py` |
| `TrackDecodeStream` | 每个可见视频 clip/track 的独立解码流 | `src/features/edit/playback/decode_stream.py` |
| `DecodeScheduler` | 解码任务优先级、lookahead、seek/preroll | `src/features/edit/playback/decode_scheduler.py` |
| `ReadyFrameQueue` | 存放已解码的 track frames 和 composite frames | `src/features/edit/playback/queues.py` |
| `PrerollManager` | 播放前准备 N 帧视频和音频缓冲 | `src/features/edit/playback/preroll.py` |
| `CompositeScheduler` | 只对完整同帧 layer bundle 提交合成 | `src/features/edit/playback/composite_scheduler.py` |
| `RealtimeCompositor` | GPU 多轨合成，支持 proxy/master texture | `src/features/edit/playback/compositor.py` |
| `AudioPlaybackEngine` | 连续音频输出、mix、drift 观测 | `src/features/edit/playback/audio_engine.py` |
| `ProxyManager` | proxy ladder、optimized media、后台生成 | `src/shared/media/proxy_manager.py` |
| `RenderCacheManager` | clip/node/timeline cache | `src/features/edit/playback/render_cache.py` |
| `PlaybackTelemetry` | 每帧性能与故障原因记录 | `src/features/edit/playback/telemetry.py` |
| `PlaybackQualityController` | 根据性能自动选择质量档 | `src/features/edit/playback/quality_controller.py` |

### 3.2 要替换或降级为兼容层的旧模块

| 旧模块 | 新角色 |
|---|---|
| `TimelinePreviewRenderer` | 暂时作为 still-frame renderer 和旧引擎 fallback，最终不负责实时播放 |
| `EditPlaybackController` | 保留 UI 控制接口，实际播放状态交给 `PlaybackEngine` |
| `TimelinePanel` timer playback | 只发 play/pause/seek/rate 命令，不逐 tick 驱动渲染 |
| `EditPage._on_playhead_changed` | 用户 seek/编辑时请求 still frame，播放中不作为每帧渲染入口 |
| `TimelineAudioManager` | 迁移/包装为 `AudioPlaybackEngine`，禁止播放中反复 seek |
| CPU QImage fallback | 只用于暂停/低端 fallback，不作为实时热路径 |

### 3.3 新核心数据结构

```python
@dataclass(frozen=True)
class TimelineFrameRequest:
    timeline_frame: int
    presentation_time_sec: float
    generation_id: int
    quality_tier: str
    deadline_monotonic: float
    direction: int


@dataclass(frozen=True)
class TrackFrameRequest:
    timeline_frame: int
    track_id: str
    clip_id: str
    asset_id: str
    asset_path: str
    source_frame: int
    opacity: float
    transform_hash: str
    effect_hash: str
    generation_id: int


@dataclass
class ReadyTrackFrame:
    request: TrackFrameRequest
    frame_path: str
    cpu_frame: object | None
    gpu_frame: object | None
    decoded_at: float
    decode_ms: float
    fallback_reason: str | None


@dataclass
class ReadyCompositeFrame:
    timeline_frame: int
    texture_id: int | None
    image: object | None
    generation_id: int
    composed_at: float
    composite_ms: float
    quality_tier: str
```

---

## 4. 线程与队列模型

### 4.1 线程职责

| 线程/执行域 | 职责 | 不能做 |
|---|---|---|
| UI thread | 按钮、时间线交互、显示已 ready 的 texture handle | 同步 decode、同步 seek、阻塞等待 worker |
| Playback control thread | 状态机、调度、队列监控 | OpenGL API、长时间 IO |
| Audio callback thread | 输出音频、提供 audio clock | 等视频、锁重资源、seek |
| Decode workers | 每轨/每素材解码、proxy/master 选择 | UI 更新、viewer present |
| GPU render thread | 上传/合成/色彩转换/present queue | 读 timeline model 原对象 |
| Proxy workers | 后台生成代理/优化媒体 | 抢占实时 decode 优先级 |
| Cache writer | 写 render cache 和 proxy metadata | 阻塞播放 |

### 4.2 队列预算

| 队列 | 建议容量 | 规则 |
|---|---:|---|
| Audio buffer | 100-300 ms | 低于阈值进入 buffering 或降级 |
| Per-track decode queue | 4-12 frames | seek 后清空旧 generation |
| Composite ready queue | 2-6 frames | 超过容量丢弃最旧非当前帧 |
| Present queue | 1-3 frames | 只保留即将到期帧 |
| Proxy job queue | 后台低优先级 | 播放 busy 时暂停重任务 |

### 4.3 多轨合成规则

| 规则 | 说明 |
|---|---|
| 同一 timeline frame 的 layer bundle 必须完整 | 不能 V1 是 frame 100，V2 是 frame 97 |
| 任一必须层迟到时按策略处理 | 等待短时间、降级、使用缓存、或跳过该 timeline frame |
| 不显示过期 composite | 如果 frame deadline 已过且后续 frame ready，丢弃旧帧 |
| 轨道 visibility/mute/solo 进入 playback graph hash | 改变后必须 invalidate graph/cache |
| 效果参数 hash 进入 cache key | 避免调色/合成参数变了还显示旧缓存 |

---

## 5. 分阶段实施总表

| Phase | 名称 | 目标 | 预计风险 | 入口条件 | 出口验收 |
|---|---|---|---|---|---|
| P0 | 诊断冻结与基准线 | 把当前卡顿量化，禁止盲修 | 低 | 当前代码可运行 | 有可复现 benchmark 和 trace |
| P1 | 新 PlaybackEngine 骨架 | 建立新状态机，不替换 UI | 中 | P0 数据可用 | 单元测试覆盖状态转换 |
| P2 | 音频主时钟 | 消除播放中音频 seek 杂音 | 中 | P1 完成 | 音频 underrun=0，播放中 seek=0 |
| P3 | 视频解码队列 | 从按需解码改成预解码 | 高 | P2 完成 | 单轨播放消费 ready frames |
| P4 | Preroll 与首帧保证 | 解决拖入后蓝屏/等待 | 中 | P3 完成 | 拖入后首帧 ready 再允许播放 |
| P5 | 多轨独立流与合成调度 | 解决多轨错帧/抖动 | 高 | P4 完成 | 2-4 轨 proxy 连续播放不倒退 |
| P6 | Proxy/Optimized Media | 超预算时自动降级不卡死 | 高 | P5 完成 | 多轨 4K 可自动 proxy 实时 |
| P7 | Render Cache | 复杂效果缓存播放 | 高 | P6 完成 | 效果区缓存命中后实时 |
| P8 | GPU 上传/零拷贝路线 | 降低 upload 瓶颈 | 很高 | P5/P6 可并行 | 2K/4K upload p95 大幅下降 |
| P9 | UI 切换新引擎 | 替换旧 realtime path | 高 | P1-P7 通过 | 默认播放走新引擎 |
| P10 | 专业级验收矩阵 | 长时间真实素材验收 | 中 | P9 完成 | benchmark 达标，trace 可解释 |

---

## 6. Phase P0：诊断冻结与基准线

### 6.1 目标

在重构前冻结问题定义，建立能复现“蓝屏、跳帧、音频杂音、多轨卡死”的基准。后续每个阶段都必须用同一套数据证明确实改善。

### 6.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P0.1 | 新建播放 trace schema | `src/features/edit/playback/telemetry.py` | 记录 frame、generation、decode_ms、upload_ms、composite_ms、present_ms、audio_drift_ms、drop_reason | 单元测试序列化 JSON |
| P0.2 | 建立多轨复现脚本 | `tests/edit/test_realtime_playback_benchmark.py` | 构造 1/2/4 轨 timeline，模拟 300 帧播放 | 输出 dropped/late/avg/p95 |
| P0.3 | 建立真实素材矩阵 | `tests/media/MATERIAL_MATRIX.md` | 增加用户素材类型、codec、fps、分辨率、是否 VFR | 文档和 fixture manifest 存在 |
| P0.4 | 加播放故障快照 | `tools/playback_trace_dump.py` | 一键导出最近 10 秒播放 trace | 本地能生成 JSON/Markdown |
| P0.5 | 定义失败门槛 | 本文档 + benchmark | late frame > 2%、audio underrun > 0、blue frame > 0 判失败 | CI/本地命令能返回非 0 |

### 6.3 验收命令

```bash
python3 -m pytest tests/edit/test_timeline_preview_renderer.py -q
python3 -m pytest tests/edit/test_audio_playback.py tests/edit/test_timeline_audio_integration.py -q
python3 tools/playback_trace_dump.py --last-seconds 10 --format markdown
```

### 6.4 P0 出口标准

| 指标 | 标准 |
|---|---:|
| Trace 覆盖率 | 每个显示帧和丢弃帧都有记录 |
| 多轨 benchmark | 至少覆盖 1/2/4 视频轨 |
| 音频指标 | 能统计 underrun、drift、live seek 次数 |
| 首帧指标 | 能统计 timeline clip add 到 first frame ready 的耗时 |

---

## 7. Phase P1：新 PlaybackEngine 骨架

### 7.1 目标

新建独立播放引擎，不再让 `TimelinePanel` timer 直接驱动每帧渲染。第一阶段只建立控制面，不替换全部渲染。

### 7.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P1.1 | 新建 playback package | `src/features/edit/playback/` | 添加 `__init__.py`, `engine.py`, `clock.py`, `state.py`, `telemetry.py` | `py_compile` |
| P1.2 | 定义 `PlaybackEngine` API | `engine.py` | `load_timeline`, `play`, `pause`, `stop`, `seek`, `set_rate`, `shutdown` | API 单测 |
| P1.3 | 定义状态机 | `state.py` | STOPPED/READY/PREROLLING/PLAYING/BUFFERING/SEEKING/PAUSED | 状态转换单测 |
| P1.4 | 接入 feature flag | `src/app/controllers/file_controller.py` 或 config | `TOPOS_EDIT_PLAYBACK_ENGINE=v2` 开启新引擎 | flag 默认关闭，测试可打开 |
| P1.5 | 旧路径兼容适配器 | `legacy_adapter.py` | 新引擎暂时可调用旧 still-frame renderer 获取当前帧 | 单帧 seek 不坏 |

### 7.3 API 草案

```python
class PlaybackEngine(QObject):
    state_changed = pyqtSignal(str)
    frame_presented = pyqtSignal(int, object)
    buffering_changed = pyqtSignal(bool)
    telemetry_sample = pyqtSignal(object)

    def load_timeline(self, project: EditProject, sequence: TimelineSequence) -> None: ...
    def play(self, start_frame: int | None = None) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def seek(self, frame: int, *, preroll: bool = True) -> None: ...
    def set_rate(self, rate: float) -> None: ...
    def shutdown(self) -> None: ...
```

### 7.4 P1 出口标准

| 场景 | 标准 |
|---|---|
| play/pause/stop/seek | 状态转换确定，无重复信号风暴 |
| timeline 修改 | generation_id 增加，旧任务可被丢弃 |
| 默认行为 | flag 关闭时旧功能不受影响 |

---

## 8. Phase P2：音频主时钟与无杂音播放

### 8.1 目标

音频成为播放主时钟。播放过程中不再通过 seek 音频追视频，避免杂音、爆音、缓冲断裂。

### 8.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P2.1 | 新建 `AudioPlaybackEngine` | `audio_engine.py` | 包装现有 audio mixer/output，提供 monotonic audio clock | 单测 clock 连续 |
| P2.2 | 禁止播放中 live audio seek | `edit_page.py`, `audio_engine.py` | 播放中 seek 请求转为 scheduled seek，只在 SEEKING/PREROLLING 边界执行 | trace 中 live_seek_count=0 |
| P2.3 | 音频 drift 观测 | `telemetry.py` | 每 100ms 记录 audio clock 与 expected timeline time | benchmark 输出 drift |
| P2.4 | 音频 underrun 统计 | `audio_engine.py` | 输出 callback buffer underrun 计数 | underrun 单测 |
| P2.5 | 变速播放策略 | `clock.py` | rate 变化时音频 time-stretch 可先 fallback mute 或 simple resample | JKL 测试不崩 |

### 8.3 同步策略

| 情况 | 策略 |
|---|---|
| 视频比音频慢 | 丢弃过期视频帧或降级质量，不 seek 音频 |
| 视频比音频快 | present queue 等待 deadline |
| 用户 seek | 停音频输出，清队列，目标帧 preroll 完成后重新启动音频 |
| 素材无音频 | 使用 monotonic playback clock 代替 audio clock |
| 音频解码失败 | 视频仍可播放，但 trace 标记 audio_unavailable |

### 8.4 P2 出口标准

| 指标 | 标准 |
|---|---:|
| 播放中 audio seek | 0 次 |
| Audio underrun | 0 次，或 trace 明确标记机器/素材超预算 |
| Audio drift | p95 <= 20ms，目标 <= 10ms |
| 用户 seek 后恢复 | 无爆音，首个 audio buffer 从目标时间开始 |

---

## 9. Phase P3：视频解码预读队列

### 9.1 目标

把“播放到哪帧才解哪帧”改为“播放前和播放中持续预解码未来窗口”。播放消费 ready frames，不能在 UI tick 中同步 decode。

### 9.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P3.1 | `TimelinePlaybackGraph` | `graph.py` | 把 timeline frame 解析为 visible video layers + source frames | 轨道/clip 映射单测 |
| P3.2 | `TrackDecodeStream` | `decode_stream.py` | 每个 clip/track 独立 session，维护 decode cursor | 同素材双轨不同 source frame 单测 |
| P3.3 | `DecodeScheduler` | `decode_scheduler.py` | 根据 clock 和 direction 提交 lookahead window | queue depth 单测 |
| P3.4 | `ReadyFrameQueue` | `queues.py` | 按 generation/frame/track 存取，支持清空旧 generation | seek 清队列单测 |
| P3.5 | 接入 `VideoFrameProviderService` | `decode_stream.py` | 初期复用 `request_frame/request_gpu_frame`，但放到 worker | UI thread no decode 测试 |
| P3.6 | Decode priority | `decode_scheduler.py` | 当前帧 > 近未来 > 远未来 > proxy job | 饥饿/抢占测试 |

### 9.3 Lookahead 策略

| 场景 | lookahead |
|---|---:|
| 单轨 24/25/30fps | 8-16 帧 |
| 双轨视频 | 6-12 帧 |
| 4 轨视频 | 4-8 帧，优先 proxy |
| 反向播放 | 4-8 帧反向窗口 |
| seek 后 | 目标帧 + 前后 2 帧 |
| 机器压力大 | 自动降低 lookahead，避免内存爆 |

### 9.4 P3 出口标准

| 场景 | 标准 |
|---|---|
| 单轨 1080p H.264 | 连续播放 300 帧，ready queue 不断流 |
| 单轨 seek | 旧 generation 结果不能覆盖新画面 |
| UI 响应 | 播放时 UI thread 不执行同步 decode |
| 解码错误 | 单帧错误不会卡死播放状态机 |

---

## 10. Phase P4：Preroll 与首帧保证

### 10.1 目标

彻底解决“拖入时间线后蓝屏、播放要等几秒”的体验问题。素材进入时间线或播放开始前必须建立 ready 状态。

### 10.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P4.1 | Clip add warmup | `timeline_panel.py`, `engine.py` | 拖入 clip 后立即 open/probe/decode first frame | drop 后 first_frame_ready trace |
| P4.2 | `PrerollManager` | `preroll.py` | play 前准备音频 buffer + N 帧视频/composite | preroll 单测 |
| P4.3 | Viewer first-frame contract | `edit_page.py`, `program_viewer.py` | 未 ready 前显示上一帧/缩略图/等待状态，不显示蓝屏 | UI 测试或 trace |
| P4.4 | Preroll timeout policy | `preroll.py` | 超时后降级 proxy 或进入 buffering，不假装播放 | timeout 单测 |
| P4.5 | Timeline edit invalidation | `graph.py`, `render_cache.py` | clip 移动/替换/效果改变只 invalidate 受影响范围 | invalidation 单测 |

### 10.3 首帧策略

| 优先级 | 显示内容 | 条件 |
|---:|---|---|
| 1 | 当前时间线真实合成帧 | decode + composite ready |
| 2 | render cache 帧 | cache key 命中 |
| 3 | timeline thumbnail/asset thumbnail | 真实帧未 ready，但有可用缩略图 |
| 4 | 上一个稳定 viewer frame | 避免蓝屏 |
| 5 | 显式 loading overlay | 只在没有任何替代帧时显示，不能蓝屏 |

### 10.4 P4 出口标准

| 指标 | 标准 |
|---|---:|
| clip drop 到 first visual | p95 <= 300ms，目标 <= 150ms |
| play 按钮到第一帧 present | p95 <= 250ms，目标 <= 100ms |
| 蓝屏/空 GL surface | 0 次 |
| preroll timeout | 有 trace，有 UI 状态，不死锁 |

---

## 11. Phase P5：多轨独立流与合成调度

### 11.1 目标

解决“多轨视频跳帧、错帧、来回抖动、卡死”。核心是同一 timeline frame 的所有 visible layers 必须以同一 generation 打包合成。

### 11.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P5.1 | Layer bundle builder | `composite_scheduler.py` | 收集同一 timeline frame 的所有 track frames | bundle 完整性单测 |
| P5.2 | 缺层策略 | `quality_controller.py` | 等待短 deadline、使用 cache、降级、或整帧 skip | late layer 测试 |
| P5.3 | GPU compositor v1 | `compositor.py` | 支持 opacity、transform、track order、basic blend | 双轨叠加 golden test |
| P5.4 | CPU fallback 隔离 | `compositor.py` | CPU fallback 只影响当前 frame，不污染播放队列 | fallback 单测 |
| P5.5 | Frame order guard | `queues.py` | present 不允许倒退 frame/generation | 乱序结果测试 |
| P5.6 | Multi-track stress benchmark | `tests/edit/test_realtime_multitrack_playback.py` | 2/4 轨 300-1000 帧模拟 | dropped/late 达标 |

### 11.3 多轨 deadline 策略

| 状态 | 处理 |
|---|---|
| 所有层 ready 且未过期 | 合成并 present |
| 一个非关键上层迟到 < 1 frame budget | 等到 deadline 前 |
| 一个层迟到且有 render cache | 用 cache |
| 一个层迟到且当前 quality 是 master | 降 proxy 重试 |
| 已经过期 | 丢弃整个 timeline frame，不显示半帧/错帧 |
| 连续过期超过阈值 | 进入 BUFFERING 或自动降级 |

### 11.4 P5 出口标准

| 场景 | 标准 |
|---|---:|
| 2 轨 1080p 30fps proxy | 1000 帧无倒退、late <= 1% |
| 4 轨 1080p 30fps proxy | 1000 帧不卡死、late <= 3% |
| 同素材双轨不同偏移 | 不共享 decode cursor，不错帧 |
| 快速 seek + play | 旧帧不闪回 |

---

## 12. Phase P6：Proxy 与 Optimized Media

### 12.1 目标

专业 NLE 的实时播放依赖代理和优化媒体。多轨 4K/6K/8K 或 long-GOP 原始素材不能强行实时硬算，必须自动生成和选择合适质量档。

### 12.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P6.1 | Proxy ladder 设计 | `proxy_manager.py` | 生成 540p/720p/1080p/prores_proxy 或 h264_intra | proxy metadata 测试 |
| P6.2 | 后台生成服务 | `src/shared/media/disk_proxy_service.py` | 播放低优先级，空闲高优先级 | 不抢实时播放 benchmark |
| P6.3 | Quality controller | `quality_controller.py` | 根据 p95 decode/upload/composite 自动选择 tier | 自动降级单测 |
| P6.4 | Pause master refresh | `engine.py` | 播放用 proxy，暂停后异步刷新 master 当前帧 | UI 验收 |
| P6.5 | Proxy UI 状态 | timeline clip badge / status | 显示生成中/已就绪/失败 | 手动验收 |
| P6.6 | Proxy cache invalidation | `proxy_manager.py` | 原文件 mtime/size/hash 变更后失效 | 单测 |

### 12.3 Quality tier

| Tier | 用途 |
|---|---|
| `cache` | render cache 命中，最高优先 |
| `proxy_540` | 多轨压力大/低端机器 |
| `proxy_720` | 默认多轨预览 |
| `proxy_1080` | 1080p/4K 时间线默认预览 |
| `optimized_media` | 中间格式，适合专业播放 |
| `master` | 暂停、导出、单帧检查、机器足够快时播放 |

### 12.4 P6 出口标准

| 场景 | 标准 |
|---|---:|
| 4K H.264 双轨 | 自动 proxy 后 30fps 预览 |
| 4K HEVC 单轨 | 有 proxy/optimized 后不卡死 |
| 6K/8K 素材 | 允许 proxy 播放，不要求原始强行实时 |
| Proxy 生成中 | UI 不蓝屏，不阻塞 |

---

## 13. Phase P7：Render Cache

### 13.1 目标

复杂调色、合成、转场、特效不能每帧播放时硬算。需要 clip cache、node cache、timeline cache。

### 13.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P7.1 | Cache key 规范 | `render_cache.py` | project_id, sequence_id, clip_id, frame, effect_hash, color_hash, proxy_tier | key 单测 |
| P7.2 | Clip render cache | `render_cache.py` | 对单 clip 效果输出缓存 | cache hit 单测 |
| P7.3 | Timeline render cache | `render_cache.py` | 对多轨合成结果缓存 | 多轨 cache 单测 |
| P7.4 | Cache invalidation | graph/effect/color modules | 参数变化只 invalidate 受影响范围 | invalidation 单测 |
| P7.5 | Cache background builder | `cache_builder.py` | 空闲时预渲染红线区域 | 不抢播放 benchmark |
| P7.6 | UI cache 状态 | timeline overlay | 显示 uncached/building/cached | 手动验收 |

### 13.3 Cache 层级

| 层级 | 缓存对象 | 适合场景 |
|---|---|---|
| Source frame cache | 解码后 proxy/master frame | 单素材复用 |
| Clip effect cache | 单 clip 调色/效果后输出 | 调色、稳定效果 |
| Composite cache | 多轨合成后输出 | 多轨叠加、转场 |
| Timeline cache | 最终 viewer frame | 整段复杂时间线 |

### 13.4 P7 出口标准

| 场景 | 标准 |
|---|---:|
| 复杂效果段第一次播放 | 可降级或 buffering，但不卡死 |
| Cache 完成后重播 | 30fps 稳定，late <= 1% |
| 参数变化 | 不显示旧 cache |

---

## 14. Phase P8：GPU 上传与零拷贝路线

### 14.1 目标

之前 benchmark 已显示 viewport playback 中 GPU upload 可能占 90% 以上。要达到专业高度，必须减少 CPU packed RGB、QImage、同步 texture write，逐步走 GPU resident frame。

### 14.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P8.1 | Texture pool | compositor/gpu modules | 固定尺寸/格式复用 texture，不每帧创建释放 | texture allocation count |
| P8.2 | PBO async upload | GPU backend | 使用 PBO/ring buffer 异步上传 planar data | upload p95 降低 |
| P8.3 | Planar YUV shader path | existing GPU renderer | 保持 Y/U/V/P010 planes，不转 packed RGB | 色彩 golden test |
| P8.4 | macOS VideoToolbox IOSurface | media/gpu bridge | CVPixelBuffer/IOSurface -> GL/Metal texture | macOS probe |
| P8.5 | Windows D3D11/NVDEC/QSV bridge | media/gpu bridge | D3D texture interop 路线验证 | Windows probe |
| P8.6 | Linux VAAPI/DMABUF | media/gpu bridge | DMABUF/EGLImage 原型 | Linux probe |
| P8.7 | GPU fence lifecycle | gpu backend | 防止 texture 被复用时 viewer 仍在读 | stress test |

### 14.3 阶段现实策略

| 阶段 | 可接受实现 |
|---|---|
| 短期 | planar YUV + texture pool + PBO，减少同步 upload |
| 中期 | macOS VT zero-copy 优先打通，因为当前用户环境大概率受益 |
| 长期 | 建立 `GpuDecodedFrame` 跨平台抽象，OpenGL 只是一个 backend |

### 14.4 P8 出口标准

| 指标 | 标准 |
|---|---:|
| 2K upload p95 | <= 8ms，目标 <= 3ms |
| 4K upload p95 | <= 16ms，目标 <= 8ms |
| 播放热路径 QImage | 0 次 |
| 播放热路径 full frame CPU RGB conversion | 尽量 0，fallback 必须 trace |

---

## 15. Phase P9：UI 接入与旧路径退役

### 15.1 目标

让 Edit 页面默认实时播放走新 `PlaybackEngine`，旧 `TimelinePreviewRenderer` 不再负责连续播放。

### 15.2 任务表

| ID | 任务 | 涉及文件 | 实施方案 | 验证 |
|---|---|---|---|---|
| P9.1 | TimelinePanel 控制接入 | `timeline_panel.py` | play/pause/seek/rate 发给 engine | UI 单测 |
| P9.2 | ProgramViewer present 接入 | `edit_page.py`, viewer | 只消费 `frame_presented` ready texture/image | viewer 单测 |
| P9.3 | EditPlaybackController 降级 | `edit_playback_controller.py` | 只保留 UI 状态/兼容 API | 旧测试通过 |
| P9.4 | Source Viewer 接入 | `source_viewer.py` | Source clip 播放也用 decode queue | source playback benchmark |
| P9.5 | 旧 realtime path disable | `timeline_preview_renderer.py` | 播放中不再按 tick 直接 request_render | regression test |
| P9.6 | Feature flag 默认开 | config | 所有验收通过后切换默认 | 完整测试矩阵 |

### 15.3 P9 出口标准

| 场景 | 标准 |
|---|---|
| 时间线播放 | 不再通过 UI tick 逐帧 request_render |
| 手动拖播放头 | 仍可快速请求 still frame |
| 播放中编辑 | engine 能进入 SEEKING/PREROLLING 并恢复 |
| 旧路径 | 可通过 flag fallback，用于紧急回滚 |

---

## 16. Phase P10：专业级验收矩阵

### 16.1 验收场景

| 场景 | 最低目标 | 专业目标 |
|---|---:|---:|
| 1080p H.264 单轨 30fps | dropped <= 0.5% | dropped = 0 |
| 1080p H.264 四轨 30fps | dropped <= 3% | dropped <= 1% |
| 4K H.264 单轨 | proxy/optimized 30fps | master 或 high proxy 30fps |
| 4K HEVC 单轨 | proxy/optimized 30fps | hardware decode 30fps |
| 4K 双轨 | proxy 30fps | optimized media 30fps |
| 复杂效果段 | render cache 后 30fps | render cache 后 dropped <= 0.5% |
| 拖入时间线首帧 | p95 <= 300ms | p95 <= 150ms |
| 播放开始延迟 | p95 <= 250ms | p95 <= 100ms |
| 音频 underrun | 0 | 0 |
| live audio seek | 0 | 0 |
| 蓝屏/空 viewer | 0 | 0 |

### 16.2 自动测试矩阵

```bash
python3 -m pytest tests/edit/test_realtime_playback_engine.py -q
python3 -m pytest tests/edit/test_realtime_multitrack_playback.py -q
python3 -m pytest tests/edit/test_playback_preroll.py -q
python3 -m pytest tests/edit/test_audio_playback.py tests/edit/test_timeline_audio_integration.py -q
python3 -m pytest tests/media/test_video_frame_provider_service.py -q
python3 -m pytest tests/media/test_proxy_manager.py -q
python3 -m pytest tests/benchmark/test_playback_acceptance.py -q
```

### 16.3 手动验收清单

| 编号 | 操作 | 预期 |
|---|---|---|
| M1 | 把 4K H.264 拖到空时间线，立即播放 | 不蓝屏，最多短暂 preroll，音频无杂音 |
| M2 | V1/V2 两轨不同视频叠加播放 | 画面不来回跳，不显示错帧 |
| M3 | V1/V2/V3/V4 四轨 proxy 播放 | 不卡死，可自动降级或 buffering |
| M4 | 播放中快速 seek | 旧帧不闪回，音频无爆音 |
| M5 | 播放中关闭上层轨 visibility | graph/cache invalidated，画面正确切换 |
| M6 | 添加效果后播放 | 未缓存时可降级，缓存后稳定 |
| M7 | 暂停在某帧 | 异步刷新 master quality 当前帧 |
| M8 | 反复 play/pause 50 次 | 无死锁、无线程泄漏、无音频设备错误 |

---

## 17. 关键代码落点

### 17.1 第一批新增文件

| 文件 | 内容 |
|---|---|
| `src/features/edit/playback/__init__.py` | playback package |
| `src/features/edit/playback/state.py` | 状态枚举和转换规则 |
| `src/features/edit/playback/clock.py` | PlaybackClock |
| `src/features/edit/playback/engine.py` | PlaybackEngine |
| `src/features/edit/playback/telemetry.py` | trace schema |
| `src/features/edit/playback/graph.py` | TimelinePlaybackGraph |
| `src/features/edit/playback/queues.py` | ReadyFrameQueue |
| `src/features/edit/playback/preroll.py` | PrerollManager |
| `tests/edit/test_realtime_playback_engine.py` | 引擎状态测试 |
| `tests/edit/test_playback_preroll.py` | preroll 测试 |

### 17.2 第二批新增文件

| 文件 | 内容 |
|---|---|
| `src/features/edit/playback/decode_stream.py` | TrackDecodeStream |
| `src/features/edit/playback/decode_scheduler.py` | DecodeScheduler |
| `src/features/edit/playback/composite_scheduler.py` | CompositeScheduler |
| `src/features/edit/playback/compositor.py` | RealtimeCompositor |
| `src/features/edit/playback/audio_engine.py` | AudioPlaybackEngine |
| `src/features/edit/playback/quality_controller.py` | 自动质量控制 |
| `src/features/edit/playback/render_cache.py` | RenderCacheManager |
| `tests/edit/test_realtime_multitrack_playback.py` | 多轨播放测试 |

### 17.3 需要谨慎修改的现有文件

| 文件 | 修改原则 |
|---|---|
| `src/features/edit/ui/timeline_panel.py` | UI 控制命令接入 engine，不在播放中逐 tick render |
| `src/features/edit/ui/edit_page.py` | 接收 engine frame_presented，停止音频 live seek |
| `src/features/edit/core/timeline_preview_renderer.py` | 只做 still-frame/fallback，逐步退出 realtime playback |
| `src/features/edit/core/edit_playback_controller.py` | 保持兼容，不再作为核心播放时钟 |
| `src/app/controllers/file_controller.py` | 初始化/销毁 PlaybackEngine，连接 viewer |
| `src/shared/video/frame_provider_service.py` | 支持更清晰的 session 隔离、GPU frame request、proxy tier |
| `src/shared/media/disk_proxy_service.py` | proxy ladder 和生成优先级 |

---

## 18. 风险与规避

| 风险 | 影响 | 规避 |
|---|---|---|
| 一次性替换导致大量回归 | Edit 页面不可用 | feature flag + strangler migration |
| Python 热路径仍然太重 | 多轨无法专业级 | 先队列化，再把 decode/upload/composite 热点迁 native/GPU |
| OpenGL/ModernGL 零拷贝受限 | 达不到 FCP/Resolve 级别 | P8 建立后端抽象，长期 Metal/D3D/Vulkan |
| Proxy 生成抢播放资源 | 播放更卡 | 实时播放高优先级，proxy worker 可暂停 |
| Cache invalidation 错误 | 显示旧画面 | cache key 必须包含 project/timeline/effect/color/track 状态 |
| 音频 callback 锁竞争 | 杂音/underrun | callback 不拿重锁，只消费 lock-free/ring buffer |
| 多轨内存暴涨 | 卡死或 OOM | queue 容量、texture pool、proxy tier、LRU |
| 跨平台差异大 | macOS 修好 Windows 坏 | backend capability probe + per-platform test |

---

## 19. 执行顺序建议

### 19.1 不建议跳过的顺序

```text
P0 数据
  -> P1 状态机
  -> P2 音频主时钟
  -> P3 解码队列
  -> P4 preroll
  -> P5 多轨合成调度
  -> P9 UI 切换
```

P6 proxy、P7 render cache、P8 GPU 上传/零拷贝可以与 P3-P5 部分并行，但不能替代 P1-P5。  
如果没有新的播放状态机和队列模型，proxy 和 cache 也只是继续给旧架构输血。

### 19.2 每阶段禁止事项

| 阶段 | 禁止 |
|---|---|
| P1 前 | 继续在旧播放 tick 上追加复杂逻辑 |
| P2 后 | 播放中通过 seek 音频追视频 |
| P3 后 | UI thread 同步 decode |
| P4 后 | 未 preroll 成功就开始推进播放头 |
| P5 后 | 显示 layer 不完整的多轨 composite frame |
| P6 后 | 多轨 4K 原始素材超预算时硬卡而不降级 |
| P7 后 | 参数变化后继续使用旧 render cache |

---

## 20. Definition of Done

这份重构计划完成时，必须同时满足以下条件：

| 类别 | 完成标准 |
|---|---|
| 架构 | Edit 实时播放默认走 `PlaybackEngine`，旧 `TimelinePreviewRenderer` 不再驱动连续播放 |
| 首帧 | 素材拖到时间线后不会蓝屏，播放前有 first-frame/preroll guarantee |
| 多轨 | 多轨播放同一 timeline frame 的 layers 同步合成，不前后抖动 |
| 音频 | 音频连续作为主时钟，播放中无 live seek、无 underrun、无杂音 |
| 性能 | benchmark 输出 decode/upload/composite/present/audio drift，失败原因可解释 |
| 降级 | 超预算时自动 proxy/render cache/buffering，而不是卡死 |
| UI | 播放/暂停/seek/拖拽/轨道开关/效果修改行为稳定 |
| 测试 | 新增 realtime playback、preroll、多轨、音频、proxy、cache 测试全部通过 |
| 回滚 | feature flag 可切回旧路径，便于阶段性验证 |

---

## 21. 第一周落地任务建议

如果从今天开始实施，建议第一周只做“新架构地基”，不要继续修旧播放补丁。

| Day | 任务 | 产物 |
|---|---|---|
| D1 | P0 trace schema + benchmark skeleton | `telemetry.py`, benchmark 初版 |
| D2 | P1 `PlaybackEngine` 状态机 | `engine.py`, `state.py`, 单测 |
| D3 | P2 `PlaybackClock` + audio live seek 禁止 | `clock.py`, audio 单测 |
| D4 | P3 `TimelinePlaybackGraph` | timeline frame -> layer requests 单测 |
| D5 | P3 `ReadyFrameQueue` + generation 清理 | queue 单测 |
| D6 | P4 `PrerollManager` skeleton | preroll 单测 |
| D7 | 小范围接入 feature flag demo | 可在测试里跑单轨假播放 |

第一周不追求 UI 彻底替换，只追求核心模型正确。否则会再次陷入 UI、解码、音频、合成一起改导致不可控。

---

## 22. 与 DaVinci/Premiere/FCP 的目标对齐

| 能力 | 专业 NLE 思路 | Topos Color 目标 |
|---|---|---|
| 播放主时钟 | 音频或统一媒体时钟 | P2 |
| Preroll | 播放前填队列 | P4 |
| 多轨解码 | 每轨独立 stream/reader | P3/P5 |
| 自动代理 | optimized/proxy media | P6 |
| Render cache | 效果段缓存 | P7 |
| GPU resident frame | 硬解/零拷贝/GPU texture | P8 |
| Late frame policy | 丢过期视频，不破坏音频 | P2/P5 |
| 可观测性能 | 内部 profiler/diagnostics | P0/P10 |
| 回放稳定性 | 状态机和队列，而不是 UI tick 硬推 | P1/P3/P9 |

达成以上能力后，Topos Color 的播放模型才会进入专业 NLE 的正确赛道。之后再继续优化 codec、GPU backend、平台硬解和 UI 细节，才有机会接近 DaVinci Resolve、Premiere Pro、Final Cut Pro 的实际体验。
