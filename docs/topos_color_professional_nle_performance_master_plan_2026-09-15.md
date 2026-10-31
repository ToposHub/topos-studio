# Topos Color 专业剪辑性能总计划

> 状态：首个可执行清单（§22）10 项 + 第二迭代第一批（P0-02 收尾、P1-06、P0-12、P0-09 本地门禁、T-07 alpha 素材、P1-01 步骤 1-3、P2-02/P2-03/T-02）+ P2-05（GOP 索引 + HEVC SLICE seek 会话，1f710878）+ T-03（GBR 修复 + 平面直通证据链，b6146ee4）+ P2-09（解码会话池上限/租约/LRU，e8b59ff6）+ P2-17（SegmentRenderKey 规范化 + composite 命中校验，c3066f21）+ P1-01 步骤 4（播放状态单源化，1398ea65）+ T-03 后半段第一步（render cache planar→encoder 接口，c1f19b86）+ P2-18 第一阶段（语义依赖指纹校验，99e0c2da）+ 隐藏缺陷复查批次（12 项修复，f2f89475，见审计文档 §十一）+ P1-05（miss 语义统一，42221aec）+ P1-10（自适应播放质量，0b47f58e）+ P1-07（预取窗口变速感知，3a334864）+ T-03 后半段产出端（planar 直通，a6e948b3）+ P2-14 复测（内核已达上限）+ P4 导出基线（965ac293）+ P4-05 Topos 段（Topos 中间片导出 YUV 域直通，见 §22.5）已于 2026-09-16/17 完成（见 §22.2/§22.3/§22.4/§22.5 与各提交号）；剩余入口：S0-1 实机验收（P1-08/13/14、P1-10 阈值调优、engine 同步 ready-bundle 反馈）、P2-10（ring/反放，需实机验证）、P2-18 后续、首帧黑帧竞态（Windows 侧，见 §11.6.2 协作记录）、`test_audio_waveform`/`test_workspace_presets` 等进程级崩溃单跑复现另行立项（本文件不代表现有性能已经全面达标）
> 建立日期：2026-09-15
> 适用范围：Edit、Media Pool、Color/Composite、Fairlight 风格音频播放、Quick Export、Deliver、GIF/动态预览、Topos 422 Proxy、macOS / Windows GPU 后端
> 维护原则：每完成一个条目，补上实测报告、测试链接和提交号；禁止只凭代码路径或日志宣称“实时”或“全 GPU”。

## 1. 目的与使用方法

这是一份面向专业非编软件的性能总计划。它把已有的播放、代理、缓存、GPU、编解码和 UI 优化计划收敛到一张可排期、可验收的路线图，避免不同模块各自优化而造成新的拷贝、竞争或回退。

本计划的工作顺序固定如下：

1. 先测量并定位，不能用猜测替代基线；
2. 再消除影响实时性的同步、CPU 像素往返和资源抖动；
3. 再提升吞吐、画质与自动化程度；
4. 每项优化必须同时通过正确性、尾延迟、内存/显存和降级路径验证；
5. 交互播放、后台缓存和导出必须互相隔离，交互优先级永远最高。

### 1.1 本计划不做什么

- 不为了“GPU 覆盖率”把无收益或不稳定的链路强行搬上 GPU。
- 不把 CPU fallback、纹理上传或最终 readback 隐藏在“全 GPU”描述里。
- 不在没有统一基线前，优先做小幅 shader 微优化。
- 不让预览因为缓存、代理、AI、缩略图或 GIF 动画任务抢占播放资源。
- 不以开发机单次平均 FPS 代替 p95/p99、长时间稳定性和真实项目表现。

## 2. 当前审计结论

### 2.1 已存在、应复用的能力

| 领域 | 已有基础设施 | 审计结论 |
|---|---|---|
| 播放编排 | `RealtimePlaybackEngine`、`RealtimePlaybackPolicy`、`SourceResolver`、generation guard、back-pressure | 已建立统一入口和状态机，但实际渲染仍有委托旧 `TimelinePreviewRenderer` 的渐进式路径，需完成热路径接管。 |
| 解码与队列 | `decode_queue`、`FrameRingBuffer`、`GpuDecodedFrame`、CPU/GPU frame cache、硬解能力探测 | 已具备队列/帧所有权模型；需统一所有解码源的背压、取消和显存预算。 |
| GPU 预览 | 共享 GL context 渲染线程、present slot pool、外部纹理、YUV GPU upload、合成缓存、fence | 已能避免大量 UI 线程渲染；仍保留 CPU QImage 上传和小图快照 readback 的受控 fallback，必须量化其触发率。 |
| 转场 | 统一 GPU GLSL 转场着色器；预览、GL 导出、Metal 导出已接入 GPU spec | 当前转场已优先全链路 GPU；下一步是 GPU trace、复杂效果 pass 合并及各平台对拍，而非再引入 CPU 蒙版。 |
| 代理 | `DiskProxyService`、`ProxyManager`、`AssetProxyManager`、Topos TPIC/MOV 路径 | 功能基础完整；代理 manifest 与实际播放源选择、优先级、批量生成和可观察性仍应收敛。 |
| 渲染缓存 | `RenderCacheWorker`、段键、缓存状态、Topos 编码支持 | 已有独立缓存体系；需把失效粒度、优先级、磁盘 I/O、缓存复用和播放时间线统一度量。 |
| 导出 | `TimelineExporter`、`ExportGpuTrail`、GPU encode surface、硬编路径/回退理由 | 能记录 CPU 上传/下载与回退原因；Windows D3D11 全链路仍是分阶段建设，不能提前标记完成。 |
| 编码 | Topos 422 Proxy/Topos 电影编码、Topos surface/plane pool、Topos profile | Topos 在现有软件对照中有潜力；完整时间线端到端、不同机器和并发工作负载仍需复测。 |
| UI 大数据 | 缩略图 LRU、波形缓存/工作线程、Effects 动态预览可见区域播放 | 已有局部优化；要统一为“可见区域虚拟化 + 内存压力驱逐 + 后台任务 QoS”。 |
| 诊断 | `playback_trace`、`PlaybackFrameTelemetry`、`performance_monitor`、`ExportGpuTrail` | 数据源分散，缺少一份可比较的端到端 trace、性能仪表盘和 CI 回归门禁。 |

### 2.2 已验证的风险或事实

以下内容来自当前代码和已有基准文档，均应作为优先调查对象；其中历史数据必须在当前提交重新复测后才能作为产品承诺。

| 编号 | 证据 | 结论与影响 |
|---|---|---|
| E-01 | `docs/professional_nle_realtime_playback_optimization_plan.md` 的历史基线 | 历史上 4K、多轨、原片 fallback 和同步开片曾显著拉低播放帧率；当前必须重新跑同口径基线，不能假定问题已消失。 |
| E-02 | `src/features/edit/core/realtime_playback/engine.py` 的模块说明 | Source decision 已用于 telemetry/代理触发，但重型 master 的严格禁用尚未完全执行；这会让实时策略与实际 decode source 不一致。 |
| E-03 | `src/features/edit/core/edit_gpu_preview_renderer.py` | GPU 渲染线程有有界队列、过期 scrub 丢弃、fence 和合成纹理缓存；仍有共享 context 创建失败、QImage upload/readback 等退化路径，需要统计频率和影响。 |
| E-04 | `src/features/edit/core/realtime_playback/playback_budget.py` | 已有帧预算和 back-pressure，但目前按渲染总耗时决策，尚不足以归因 decode / upload / effect / present / UI 的各段超时。 |
| E-05 | `docs/windows_timeline_gpu_export_optimization_plan_2026-09-15.md` | Windows T0 有实机证据，T1 部分接线，T2-T6 仍是未完成链路；D3D11 decoder/bridge 的存在不等于时间线全 GPU 导出完成。 |
| E-06 | `docs/metal_backend_migration_and_cross_platform_gpu_plan.md` | 特定 Intel/AMD macOS 机上，4K ProRes 软解曾是主要瓶颈；Metal/GL 改造不能消除没有硬件解码器时的 CPU decode 成本。 |
| E-07 | `native/topos_codec/docs/decode_speed_phase1_report_2026-09-06.md` | Topos 4K 顺序 CPU plane 交付已有约 57–59 fps 的历史样本，但不包含 GPU 上传、调色、合成和 present，不能当作完整播放结论。 |
| E-08 | 同报告的阶段画像 | Topos native decode 的 dequant/IDCT 是主要 CPU 热点；若当前复测仍一致，SIMD/缓存访问优先级高于继续试验不通过的熵编码分支。 |
| E-09 | `src/shared/export/gpu_trail.py`、`timeline_export.py` | 导出已有回退可追溯能力；所有“全 GPU 导出”声明应受 `cpu_upload_bytes == 0`、`cpu_download_bytes == 0` 与实际 surface trail 双重约束。 |
| E-10 | GIF 动态预览的现有可见区域/暂停设计 | 大量 GIF 卡片的正确方向是虚拟化、惰性解码、可见区域播放和预取窗口，不是一次性加载或常驻播放全部 GIF。 |

### 2.3 必须先复测、不能预设结论的事项

- 当前主分支在真实项目上的 1080p、2K、4K 单轨/多轨实际 fps、p95/p99 和掉帧率。
- Topos 422 Proxy 对当前 GPU、磁盘、源媒体、并发导出时的真实 decode/encode 和首帧延迟。
- H.264/H.265/ProRes/DNxHR/Topos 的硬解可用性、零拷贝比率与实际 fallback 原因。
- Metal、OpenGL、D3D11 在同一效果/转场/色彩规范下的段耗时和结果一致性。
- 动态预览、缩略图、波形和 AI 任务在数千资产项目中的 CPU、内存、GPU 与主线程占用。
- 单 GPU、混合显卡、多 GPU 和电池/低功耗状态下的设备选择、跨 adapter copy 和热降频影响。

## 3. 性能目标与统一口径

### 3.1 体验等级

先按项目负载而非“理论最大 FPS”定义等级。每台基准机必须同时记录 CPU、GPU、RAM、VRAM、磁盘、系统、驱动、素材和工程版本。

| 等级 | 典型工程 | 目标 |
|---|---|---|
| R1 编辑流畅 | 1080p 25/30fps，1–2 视频轨，基础调色/转场 | 100% 实时播放；连续 10 分钟无持续掉帧；拖动、切割和播放控制 p95 不阻塞。 |
| R2 创作流畅 | 2K/4K proxy，3–4 视频轨，常用转场、标题、LUT、基础降噪 | 代理策略下稳定实时；未命中时明确 hold/rebuffer，不错误加速或卡死 UI。 |
| R3 重度合成 | 4K、多轨、模糊/降噪/光流/复杂字幕/多层调色 | 自适应质量、缓存和代理介入后保持可编辑；优先保证音画时钟、交互和可解释的降级。 |
| R4 交付吞吐 | 4K 长片、多段缓存命中、硬编/Topos 中间片 | 可恢复、可取消、准确的 ETA；不因导出挤占前台播放；可证实的 GPU chain。 |
| R5 超大媒体库 | 数千到数万素材、数百效果/转场 GIF | 首屏快、滚动稳定、只处理可见/即将可见元素；空闲时再逐步预热。 |

### 3.2 建议验收预算

这些是起始门槛，须在 P0 复测后按目标硬件分级固化。所有延迟报告同时给 p50 / p95 / p99 / max，且区分冷缓存和热缓存。

| 指标 | 25fps | 30fps | 50fps | 60fps | 约束 |
|---|---:|---:|---:|---:|---|
| 单帧总预算 | 40.0ms | 33.3ms | 20.0ms | 16.7ms | 预算应预留 15–25% 给 Qt/UI/系统抖动。 |
| 前台渲染预算建议 | ≤32ms | ≤26ms | ≤16ms | ≤13ms | decode + upload + composite + present；不能只测 shader。 |
| 播放/暂停响应 p95 | ≤80ms | ≤80ms | ≤80ms | ≤80ms | 不等同于首帧解码完成。 |
| 常规 seek 预览 p95 | ≤150ms | ≤150ms | ≤120ms | ≤120ms | 代理/缓存命中目标；miss 必须有状态反馈。 |
| 普通 trim / blade 操作 p95 | ≤50ms | ≤50ms | ≤50ms | ≤50ms | 命令、撤销和波形/UI 不能同步重算整条时间线。 |
| 首次有画面（已打开工程）p95 | ≤500ms | ≤500ms | ≤500ms | ≤500ms | 应分别报告 proxy hit、master、缓存 hit。 |
| Effects 面板滚动帧 p95 | ≤16.7ms | ≤16.7ms | ≤16.7ms | ≤16.7ms | 仅可见卡片可启动 GIF decoder。 |
| 连续播放掉帧率 | <0.1% | <0.1% | <0.3% | <0.5% | 高负载可降级，但不得造成音画时钟漂移。 |

### 3.3 不可妥协的性能正确性约束

1. 实时路径中，过期 request 必须可取消或被丢弃，不能排队到播放头已经离开的位置。
2. CPU/GPU 的每次上传、下载、surface map、同步 wait 都必须被 trace；“未知”不能计入零拷贝。
3. 导出与缓存可以慢，但不能错帧、丢 alpha、改色彩元数据、损坏 PTS 或写出半成品。
4. 播放可 hold/drop 视频帧，但音频 sample clock 不可丢弃样本来伪造实时性。
5. GPU / RAM / 磁盘缓存必须有成本上限、驱逐原因和优先级，不能依赖进程被系统杀死来回收。
6. 所有 fallback 都要可见、可诊断、可测试；静默降级不算完成。

## 4. 目标端到端架构

```text
时间线修改 / 播放 tick / scrub
        │ generation + deadline + priority
        ▼
Frame graph compiler ─── invalidation map ─── cache keys
        │
        ├── SourceResolver ── proxy / optimized media policy
        │          │
        │          ▼
        ├── Decode scheduler ── bounded per-source rings ── GPU/CPU frame lease
        │          │                                      │
        │          └──────── cancellation / back-pressure ─┘
        ▼
GPU render graph ── import YUV ── effects / transitions ── composite ── present
        │               │                 │                    │
        │               └──── persistent pools + GPU timestamps ┘
        ▼
Viewer / cache fill / delivery surface
        │          │             │
        │          │             └── hardware encoder or Topos cache encoder
        │          └── segment cache (GPU first, disk second)
        ▼
Unified trace + performance HUD + benchmark artifacts + regression gates
```

目标并非让所有素材都永远走同一解码器，而是让每一帧的 source、资源归属、缓存命中、后端、复制和最终命运都可解释。

## 5. 优先级与阶段总览

| 阶段 | 主题 | 优先级 | 完成后可得到的价值 | 前置条件 |
|---|---|---:|---|---|
| P0 | 可测量性、基线与硬性回归门 | P0 | 知道真正慢在哪里，所有后续优化可量化 | 无 |
| P1 | 前台实时播放热路径收敛 | P0 | 停止旧/新双路径竞争，降低卡顿和不可解释回退 | P0 |
| P2 | 代理、缓存、解码与 I/O | P0 | 重媒体/大项目可编辑，Topos 422 Proxy 真正发挥作用 | P0、P1 |
| P3 | GPU render graph 与转场/效果 | P1 | 降低 upload/readback/pass 数，复杂效果更稳定 | P0、P1 |
| P4 | 导出、编码与交付 | P1 | 更快且可证实的 Deliver/Quick Export | P0、P3 |
| P5 | UI、媒体池、动态预览与音频 | P1 | 数千资产仍保持前台流畅 | P0 |
| P6 | 内存/显存、并发与系统 QoS | P1 | 长时稳定，后台任务不干扰编辑 | P0、P1、P2 |
| P7 | 平台后端与硬件矩阵 | P2 | macOS / Windows 的性能结论可信、可发布 | P0、P3、P4 |
| P8 | 高级效果、AI、光流和长期演进 | P2 | 高负载能力扩展但不破坏基础体验 | P0–P6 |

## 6. P0：可测量性、基线与性能门禁

### P0 的完成定义

任何人都能用固定命令、固定素材集和固定 JSON schema 复现一次基准；同机相对上个提交的退化能自动报警；开发者能从一次卡顿 trace 看到耗时/队列/缓存/fallback 的具体段。

| ID | 工作项 | 主要落点 | 验收标准 |
|---|---|---|---|
| P0-01 | 建立统一 performance event schema | `playback_trace`、`PlaybackFrameTelemetry`、`ExportGpuTrail`、cache/worker events | 一帧可串起 request → source → decode → import/upload → effects → transition → composite → present；导出也能串起 encode/mux。 |
| P0-02 | 给每帧加稳定 trace id | playback engine、renderer、decode scheduler、cache worker | generation、frame、source asset、priority、backend、surface/lease、deadline 可关联；日志不记录用户路径/素材内容。 |
| P0-03 | GPU 段级计时 | OpenGL/Metal/D3D11 render/encoder 路径 | 采用 GPU timestamp/query；CPU wall time 与 GPU time 分列；未支持时显式 `gpu_time_unavailable`。 |
| P0-04 | 复制审计 | `GpuDecodedFrame`、GPU bridge、export trail | 统计 CPU upload/download bytes、surface map、texture write/readback、cross-device copy；为 0 只能由真实数据证明。 |
| P0-05 | 统一资源统计 | CPU RAM、VRAM、纹理数、FBO/surface pool、文件句柄、线程数、队列深度 | 每 1s 快照与峰值；支持按播放、缓存、导出、缩略图、AI 分类。 |
| P0-06 | 建立标准素材矩阵 | `tests/test_videos` + 外部受控基准资产 | `tests/test_videos` 是每次功能/性能修复后的必跑真实视频集，不可由 synthetic fixture 替代；覆盖 H.264/H.265 8/10-bit、ProRes、DNxHR、Topos、alpha、VFR、长 GOP、4K、HDR、损坏/边界素材；记录 manifest、许可和 hash。 |
| P0-07 | 建立工程矩阵 | benchmark projects / deterministic project builder | 单轨、双轨转场、四轨、复杂调色、字幕、alpha、重度 FX、长项目、海量媒体池；可无 GUI 创建。 |
| P0-08 | 冷/热/失效基准协议 | benchmark runner | 固定 warmup、采样帧数、重复次数、中位数/p95、机器信息；分离首帧、稳态、seek、长跑。 |
| P0-09 | 性能回归 CI | unit / integration / self-hosted GPU runners | CPU 机器做 schema/逻辑门；Apple/Windows GPU 真机做 nightly；超过预算或 ≥5% 同机退化失败/告警。 |
| P0-10 | Performance HUD 与诊断导出 | Edit UI、Deliver UI、support bundle | 用户/开发者可看到 fps、drop、source、proxy/cache、backend、fallback；一键导出脱敏 trace。 |
| P0-11 | 重新建立当前基线 | macOS 主目标机、Windows NVIDIA/AMD/Intel、降级机 | 每个平台产生可版本化报告；旧历史报告仅做对照，不覆盖新数据。 |
| P0-12 | 性能 budget registry | source-controlled config | 每类场景的 SLA、硬门/软门、免责条件明确；PR 能引用 budget ID。 |
| P0-13 | 真实视频修复后门禁 | 每次修改播放、Composite、解码、缓存、GPU、转场、编码或导出后，用 `tests/test_videos` 跑受影响的真实视频矩阵 | 先验证无错误/黑帧/错帧/色彩-alpha-PTS 问题，再比较冷/热/seek/长跑性能；未跑、失败或 p95 回退未解释的变更不可标为完成。 |

### P0 基准场景最低集

| 场景编号 | 场景 | 必测指标 |
|---|---|---|
| B-01 | 1080p H.264 单轨，原片/代理 | 首帧、稳态 fps、seek p95、CPU/GPU、decode source。 |
| B-02 | 4K H.265 10-bit 单轨，硬解可用与不可用机 | backend、零拷贝率、drop、温度/功耗（可取得时）。 |
| B-03 | 4K ProRes / DNxHR 单轨 | 软件 decode、upload、代理接管和首帧。 |
| B-04 | 4K Topos 422 Proxy 单轨 | decode stage、GPU import、磁盘吞吐、随机 seek、并发导出。 |
| B-05 | 双轨 + GPU 转场 + 调色 | 每 pass GPU time、copy bytes、过渡边界的帧稳定性。 |
| B-06 | 四轨 + 标题 + 字幕 + 常用 effects | render plan 构建、cache hit、texture/FBO pool 峰值。 |
| B-07 | 复杂模糊/降噪/光流或 CPU fallback 效果 | 自适应质量动作、fallback、音画时钟、用户反馈。 |
| B-08 | 30 分钟持续播放 | memory/VRAM 泄漏、句柄/线程数、长尾卡顿、A/V drift。 |
| B-09 | 4K Deliver（H.264/H.265/Topos/GIF 适用处） | encode 吞吐、CPU/GPU trail、PTS/色彩/alpha、取消恢复。 |
| B-10 | 5,000 素材媒体池、1,000 动态预览效果库 | 首屏、滚动 p95、解码器数、内存、隐藏面板后台占用。 |

## 7. P1：实时播放热路径收敛

### 7.1 架构目标

`RealtimePlaybackEngine` 成为真实而不是名义上的唯一入口：它负责 deadline、generation、source policy、解码任务、present 结果和 telemetry。`TimelinePreviewRenderer` 逐步变为 GPU render executor，而不是自行决定播放策略的第二个控制中心。

| ID | 工作项 | 验收标准 |
|---|---|---|
| P1-01 | 完成 Engine → Decode Scheduler → Frame Ring → GPU compositor 的正式接管 | 播放 tick 不再绕过 engine 直接触发老路径；可用 trace 证明唯一入口。 |
| P1-02 | 定义帧优先级协议 | `scrub latest > playhead > imminent prefetch > cache fill > thumbnail/GIF > export`；满队列时按协议丢弃，绝不阻塞 UI。 |
| P1-03 | 统一 generation/cancellation | seek、trim、变速、切换序列、关闭工程可取消过期 decode/render/cache；没有悬挂 lease。 |
| P1-04 | 格式感知 realtime source policy | 高分辨率/重编码原片在实时模式优先 proxy/optimized media；可播放原片例外必须有性能分类和 trace 原因。 |
| P1-05 | 定义 miss 语义 | 区分 decode miss、GPU busy、cache warming、proxy pending、unsupported effect；策略是 hold、rebuffer 或降低质量，不能假装成功。 |
| P1-06 | 分段 deadline | 将 decode、import、effect、composite、present 分配预算；只要有一段越界即可归因。 |
| P1-07 | 预取窗口与方向感知 | 正放、反放、J/K/L、scrub 分别有窗口；跳转时立即撤销旧方向预取。 |
| P1-08 | 无阻塞 seek/open | decoder open、index build、proxy lookup、首帧读取全部后台化；UI 只显示状态。 |
| P1-09 | 低延迟 present | slot pool/fence 控制 in-flight 帧，禁止每帧 `finish` / `wait`; UI 只消费最新可展示帧。 |
| P1-10 | 自适应播放质量 | 在用户偏好允许下调整 preview scale、效果旁路级别、代理级别；每次动作可见且可回退。 |
| P1-11 | A/V 时钟完整性 | 音频为主时钟；视频 hold/drop 绝不丢音频样本；记录 drift、underrun、resync。 |
| P1-12 | 编辑命令轻量化 | trim、移动、blade、吸附、选中只更新受影响 render-plan 区间；禁止同步全项目重建。 |
| P1-13 | Comp Clip 实时 GPU 路径 | Comp Clip 以 GPU frame handle + fence/lease 接入 `TimelineGpuCompositor`；移除“Comp Clip 必然 CPU fallback”的旧热路径，输出尺寸/quality/deadline/generation 成为 render request 的一等字段。 |
| P1-14 | Comp Clip 预取与自适应质量 | 以依赖图和播放方向预渲 Comp Clip，静态/时域节点分别缓存；在 deadline 下自适应 preview scale/效果质量或读取已完成 cache，不能静默全帧 CPU 合成。 |

### 7.2 重点风险与具体对策

| 风险 | 对策 | 验收 |
|---|---|---|
| 双路径竞争 | 为旧委托路径建立迁移开关、trace tag 和删除里程碑 | 目标场景仅有一个 render owner；旧路径删除前覆盖完整回归。 |
| 任务队列雪崩 | 每类队列有长度上限、deadline、drop policy 和 cancel callback | 反复 scrub 10 秒后队列在 1 个帧周期内收敛，RAM/VRAM 不单调上涨。 |
| “实时”却偷偷解重原片 | source decision 必须实际影响 decoder 输入；proxy pending 明确进入 warming/rebuffer | trace 中 `planned_source == actual_source`，不相等必有允许理由。 |
| 迟到帧覆盖新画面 | present 前再次检查 generation、frame/timestamp、scrub token | 随机 seek/fuzz 测试不出现旧帧闪回。 |
| 背压永久冻结 | 探测帧/恢复机制与用户可见 rebuffer 状态 | 连续超预算后能够恢复；不会永远 drop。 |

## 8. P2：代理、缓存、解码和磁盘 I/O

### 8.1 Proxy / Optimized Media

Topos 422 Proxy 应成为重素材编辑的高优先级中间片选项，而不是只在偏好设置中存在。最终交付默认仍使用原始媒体或明确的渲染缓存，除非用户明确选择代理交付。

| ID | 工作项 | 验收标准 |
|---|---|---|
| P2-01 | 收敛 proxy manifest 与 asset 状态 | 导入、生成、失效、移动、重连、删除都有单一真相；不会生成重复代理。 |
| P2-02 | Topos 422 Proxy 默认策略验证 | 根据分辨率、codec、bit depth、磁盘和设备能力选择；所有自动决定可解释且用户可覆盖。 |
| P2-03 | 后台代理 QoS | 播放/交互时暂停或降权，空闲/插电时提速；可暂停、恢复、取消、限并发。 |
| P2-04 | Proxy 自动触发与预热 | 导入后按风险排序生成；时间线附近素材优先；不因媒体池全量导入抢满机器。 |
| P2-05 | Proxy seek index / GOP 索引 | 对长 GOP 和 Topos 建立可持久化索引；随机 seek 与顺序播放分别优化。 |
| P2-06 | Alpha/HDR/色彩 metadata 保真 | 代理完整保存 alpha、range、matrix、transfer、primaries 和时间基；不支持时阻止或明确降级。 |
| P2-07 | Proxy 存储管理 | 配额、项目归属、最近使用、可恢复清理、失效原因、磁盘健康检测。 |
| P2-08 | Proxy 性能对照报告 | 以同素材对比 original、Topos、DNxHR；报告首帧、顺序、seek、磁盘、CPU/GPU，不只比较编码时间。 |

### 8.2 解码

| ID | 工作项 | 验收标准 |
|---|---|---|
| P2-09 | Decoder session pool | 按素材/codec/backend 复用 session；有上限、租约和关闭策略，避免频繁 open/close。 |
| P2-10 | per-source frame ring | source 独立有界环，不让单个慢源拖垮所有轨道；支持顺放、反放和随机跳转。 |
| P2-11 | 硬解能力运行时验证 | 不仅检测“可创建”，还验证首帧、连续帧、像素格式、device 归属和稳定性。 |
| P2-12 | 硬解零拷贝导入 | VideoToolbox/CVPixelBuffer/IOSurface、D3D11VA/D3D11 surface 直进对应 GPU backend；禁止隐式 CPU map。 |
| P2-13 | 软件解码线程预算 | Topos、FFmpeg、缩略图、波形、导出共享全局 worker budget；前台播放保留最低资源。 |
| P2-14 | Topos decode 内核优化 | 先复测 dequant/IDCT，再做 SIMD、内存布局、slice 调度、预取与平台 dispatch；每项维持 bit-exact/容差契约。 |
| P2-15 | 解码错误与重试策略 | 损坏帧、网络/外置盘超时、decoder reset 有限重试；不在 UI 线程重复阻塞。 |
| P2-16 | VFR / B-frame / 反向播放 | 建立时间索引和展示时钟测试，性能优化不得破坏 PTS/DTS 语义。 |

### 8.3 Render Cache

| ID | 工作项 | 验收标准 |
|---|---|---|
| P2-17 | 规范化 `SegmentRenderKey` | 包含序列、帧范围、媒体版本、效果/转场参数、色彩、输出规格和后端语义；避免误命中。 |
| P2-18 | 精确失效图 | 修改一个 clip/effect 只失效依赖片段；撤销能复用仍有效片段。 |
| P2-19 | 缓存优先级队列 | 播放头附近 > 用户显式缓存 > idle pre-render；不可抢占前台 GPU/decoder。 |
| P2-20 | GPU 中间缓存 | 对可复用图层、预合成、LUT、标题、转场中间结果按 VRAM budget LRU；fence 后释放。 |
| P2-21 | 磁盘缓存编码 | 默认自研 TOPOS：连续/密集片段使用带 TPIC sample entry 的 Topos MOV，稀疏、局部失效和随机访问帧使用 `.toos` Topos 图片序列；短生命周期中间结果留在 GPU cache，不做无意义落盘；两种持久格式均验证 alpha 语义。 |
| P2-22 | 缓存 I/O 管线 | Topos MOV 段与 TOOS 序列共用 manifest、临时写入、校验、原子发布、限速、占用空间回收和 codec build identity；取消、缺帧、损坏或版本不匹配自动失效重建。 |
| P2-23 | cache hit 指标 | 统计 RAM/GPU/disk hit、partial hit、miss reason、节省时间和被驱逐原因。 |
| P2-24 | 缓存与导出复用 | Deliver 能复用兼容的 Topos MOV/TOOS render cache；规格、色彩、alpha、时间基或 graph revision 不兼容时明确重渲，不可错误复用 preview proxy。 |
| P2-25 | Comp Clip 缓存分层 | GPU warm cache、受字节预算的 CPU fallback cache、Topos MOV/TOOS 持久 cache 统一 key、失效、QoS 与资源统计；连续片段与局部/稀疏帧按实测选择格式。 |

## 9. P3：GPU render graph、转场和效果

### 9.1 全链路 GPU 的完成标准

对宣布支持的 backend 与效果组合，像素路径必须是：GPU decode surface 或一次可计量 upload → GPU import / YUV conversion → effect / transition / composite → GPU present 或 GPU encoder surface。只有 UI 截图、静态缩略图或显式 CPU fallback 才允许 readback，并且必须有原因与次数。

| ID | 工作项 | 验收标准 |
|---|---|---|
| P3-01 | Render graph IR | 把 clip、颜色、effect、transition、blend、subtitle、输出表述成可分析图；可计算 pass 数、临时纹理和失效边界。 |
| P3-02 | Render plan 增量编译 | 时间线小修改只更新受影响节点；计划缓存按结构与参数分层。 |
| P3-03 | 资源池统一 | texture/FBO/PBO/IOSurface/D3D11 texture/encoder surface 全部池化并预算化；禁止 hot path 每帧新建。 |
| P3-04 | shader/program pipeline cache | 编译移出播放帧；参数改变不重复链接；缓存有版本、容量、失效和失败诊断。 |
| P3-05 | pass fusion | 合并颜色转换、几何、常见 blend、简单 effect；以 GPU timestamp 验证收益，避免牺牲色彩精度。 |
| P3-06 | 多分辨率效果策略 | blur/denoise/defocus 等支持 downsample pyramid、tile/compute 或质量档；根据 deadline 自适应。 |
| P3-07 | 转场统一能力表 | 每个转场声明 GLSL/Metal/D3D11/CPU 支持、GPU only、临时纹理、alpha/HDR、性能等级。 |
| P3-08 | 转场 GPU 对拍 | GPU preview、GL export、Metal export、CPU reference 的关键帧、边界、alpha、HDR 一致；motion blur/smooth-cut 需独立质量指标。 |
| P3-09 | 避免过度同步 | 所有 `glFinish`、`glReadPixels`、Map/Unmap、fence wait 有审计；只在明确的所有权边界等待。 |
| P3-10 | GPU 内存压力回退 | 接近 VRAM 上限时先驱逐低优先级 cache/preview，再降预览质量；不让 driver OOM 终止进程。 |
| P3-11 | CPU fallback 质量与可见性 | 未支持插件/效果必须明确走 CPU，带预计成本和缓存建议；不静默丢 effect。 |
| P3-12 | GPU capture 回归流程 | RenderDoc/Xcode GPU frame capture/PIX 的可复现案例与诊断说明。 |

### 9.2 特效性能分类

| 类别 | 典型项 | 优化方向 |
|---|---|---|
| 点/单采样 | LUT、色相、曝光、简单 vignette | 尽量与前后 pass 合并。 |
| 邻域采样 | sharpen、glow、blur、边缘、磨皮 | 多尺度/可分离卷积/half-res；避免动态大循环。 |
| 时域 | optical flow、denoise、motion blur、稳定 | 管理多帧依赖与缓存；实时模式给质量档/后台预分析。 |
| 几何/合成 | transform、crop、mask、blend、转场 | 合并到 composite；复用矩阵/遮罩纹理。 |
| AI | 人像分割、超分、跟踪 | 独立 QoS、模型常驻预算、可取消、按帧/片段缓存。 |
| CPU/插件 | OFX、脚本、未迁移 GLSL | 透明标识、异步缓存、避免阻塞 UI；逐项决定是否值得 GPU 移植。 |

## 10. P4：导出、编码与交付

### 10.1 共通导出优化

| ID | 工作项 | 验收标准 |
|---|---|---|
| P4-01 | Export render graph | 预览/缓存/导出共享语义但可有不同输出节点；防止三套效果实现漂移。 |
| P4-02 | 导出帧流水线 | render、color convert、encode、 mux 分离为有界阶段；上一段背压不无限占用 RAM。 |
| P4-03 | GPU encode surface 规范 | RGB/linear → NV12/P010/YUV(A) surface 不经 CPU 回读；每 backend 用 trail 验证。 |
| P4-04 | 硬编会话管理 | session 数、profile、B-frame/lookahead、surface ring、fence、失败恢复有显式策略。 |
| P4-05 | Topos cache/export 快速路径 | Topos 平面直通、减少 RGB/YUV 往返；对不兼容图形输出使用一次明确转换。 |
| P4-06 | 编码并发 QoS | Deliver、proxy、cache 的并发预算可配置；编辑播放时优先级降低。 |
| P4-07 | 磁盘与 mux 优化 | 临时文件、顺序写、原子替换、取消清理、空间预检、网络盘降级。 |
| P4-08 | 长导出鲁棒性 | checkpoint、resume 能力评估、错误信息、ETA、温度/功耗节流监测。 |
| P4-09 | 输出校验 | 帧数、PTS、音频时长、metadata、HDR、alpha、解码回读、hash/抽样视觉对拍。 |
| P4-10 | Quick Export / Deliver 共享后端 | 相同 codec/profile 不能维护两套执行链；UI 差异留在配置层。 |

### 10.2 macOS

| ID | 工作项 | 验收标准 |
|---|---|---|
| P4-M01 | VideoToolbox decode → IOSurface/CVPixelBuffer → GL/Metal import 复测 | 能力探针和实际多帧 trace 一致；无硬解时明确走软件 decode。 |
| P4-M02 | Metal render → VideoToolbox encode surface | 对已支持输出实现 no-upload/no-download trail；色彩与 alpha 通过对拍。 |
| P4-M03 | OpenGL/Metal 选择策略 | Auto 依实际能力、效果支持、稳定性选择；用户强制选择的退化提示完整。 |
| P4-M04 | Apple Silicon 与 Intel/AMD 降级机矩阵 | 绝对性能只在目标 Apple Silicon 机承诺；Intel/AMD 保留兼容性与回退回归。 |

### 10.3 Windows

| ID | 工作项 | 验收标准 |
|---|---|---|
| P4-W01 | D3D11 decoder surface 正式接入 | decoder texture 的 adapter LUID 与 renderer 相同，导出前无 CPU download。 |
| P4-W02 | D3D11 render graph / HLSL 效果迁移 | T0–T6 每个场景有单独支持状态、fallback 原因和画质测试。 |
| P4-W03 | D3D11 → NVENC / AMF / oneVPL | NV12/P010 surface 直接提交；不以 FFmpeg 的隐式 download 冒充零拷贝。 |
| P4-W04 | 多 adapter 策略 | Optimus/混合显卡 LUID mismatch 安全降级；记录 cross-device copy。 |
| P4-W05 | Vendor 真实性能矩阵 | NVIDIA、AMD、Intel 至少各一台真机；CI runner 仅做逻辑测试，不作为性能证据。 |

### 10.4 GIF 与动态图输出

| ID | 工作项 | 验收标准 |
|---|---|---|
| P4-G01 | GIF 编码参数成本模型 | 尺寸、fps、色彩数、dither、透明度、时长对应预估体积/耗时，UI 可提示。 |
| P4-G02 | 帧采样与颜色量化异步化 | 不阻塞 Deliver/Quick Export UI；可取消；内存为有界帧窗口。 |
| P4-G03 | GIF 导出资源隔离 | GIF palette/编码 CPU 任务不抢占实时 decode；大量小 GIF 可限制并发。 |
| P4-G04 | 动态预览资产生成管线 | 转场 preview 从 GPU 渲染结果或一致的离线渲染路径生成；避免逐帧 CPU 合成。 |
| P4-G05 | GIF 质量/体积回归 | 关键帧视觉差、循环时长、透明/色带、256px 预览与高分辨率导出分别验收。 |

## 11. P5：UI、媒体池、动态图预览、波形和音频

### 11.1 UI 通用规则

1. Qt 主线程只负责输入、轻量布局和 present；文件 I/O、解码、图像缩放、波形、palette、shader compile 不得同步执行。
2. 任何可滚动的大列表都必须虚拟化或至少延迟实例化；隐藏面板不运行定时器、GIF decoder 或预取。
3. 每个 UI 后台任务带 owner/generation；面板关闭、筛选改变、项目关闭时取消。

| ID | 工作项 | 验收标准 |
|---|---|---|
| P5-01 | Media Pool 数据模型/视图分离 | 大媒体库使用 model/view 或等价虚拟化；不为每个素材常驻完整 widget。 |
| P5-02 | Thumbnail pipeline | 可见区域优先、尺寸分档、内存+磁盘 LRU、请求合并、取消、颜色管理；滚动不产生无界 decode。 |
| P5-03 | Effects / Transitions 动态预览窗口化 | 只激活 viewport + 预取边界内卡片；离屏/隐藏立即暂停并释放 decoder；卡片尺寸切换重用合适分辨率资产。 |
| P5-04 | 动态预览资源分级 | 256px GIF 用于小/中卡片，更大卡片按需用高质量版本；文件 hash/version 防止旧资产混用。 |
| P5-05 | 面板统一生命周期 | Float、Close、favorites、Icon/GIF preview 等菜单状态驱动同一 controller；关闭/浮动不留下后台活动。 |
| P5-06 | Timeline 虚拟化 | 大量 clip、marker、关键帧、轨道只绘制可见区；缩放/拖拽合并刷新。 |
| P5-07 | 波形分层缓存 | 原始波形、缩放 level、effect 波形分离；后台生成可取消；UI 只绘制当前 level。 |
| P5-08 | 音频 ring / 设备回调 | 解码、resample、设备 callback 有固定预算；overflow/underrun 可追溯，绝不静默丢 sample。 |
| P5-09 | 文本/字幕缓存 | 字形、layout、字幕位图按分辨率/样式/时间片缓存；导出与预览共享可复用结果。 |
| P5-10 | 输入响应 profiler | 记录鼠标拖动、滚轮、快捷键、菜单、窗口 resize 的主线程长任务；超过 16ms 输出诊断。 |

## 12. P6：内存、显存、并发、进程与系统 QoS

| ID | 工作项 | 验收标准 |
|---|---|---|
| P6-01 | 全局资源预算管理器 | CPU RAM、VRAM、磁盘、decode workers、encoder sessions、GIF decoders、AI model 有统一可配置预算。 |
| P6-02 | 内存压力分级策略 | normal / warning / critical 下按顺序停止预取、清除缩略图/GIF、驱逐 GPU cache、暂停后台任务；前台帧最后才受影响。 |
| P6-03 | 资源 lease 生命周期审计 | GPU frame、surface、FBO、mapped buffer、decoder session 可追踪 acquire/release；关闭工程后归零。 |
| P6-04 | 无界容器治理 | 所有 dict/list/cache/queue 必须定义 max、TTL 或代际回收；test 能模拟长项目。 |
| P6-05 | 线程池分域 | UI-critical、playback decode、cache/proxy、thumbnail/waveform、AI、export 分开配额并可动态调度。 |
| P6-06 | CPU affinity / worker budget | 避免 Topos slice、FFmpeg、AI、代理、导出各自“满核”；测量不同 CPU 核数的最优策略。 |
| P6-07 | GPU QoS | 将交互 render、cache fill、GIF 生成、AI inference、export 分优先级；同一 GPU 上避免 command queue 饥饿。 |
| P6-08 | 磁盘 QoS | cache/proxy/export 写入限速；识别外置盘/网络盘/空间不足，避免使项目保存与 UI 卡住。 |
| P6-09 | 热与功耗策略 | 长时播放/导出监控降频和温度（平台可用时）；ETA 适配当前吞吐，避免固定理想速度。 |
| P6-10 | 泄漏/稳定性 soak | 1h 播放、数百次 seek、导出取消循环、面板开关/GIF 滚动；RAM/VRAM/fd/thread 无线性增长。 |

## 13. P7：平台、后端与能力模型

| ID | 工作项 | 验收标准 |
|---|---|---|
| P7-01 | 后端 capability schema 完整化 | graphics/decode/interop/encode 分开表达，不用单一“GPU 可用”布尔值。 |
| P7-02 | 运行时自检与缓存 | 首次运行探针可验证真实 texture/surface 流；缓存有驱动版本/设备变更失效。 |
| P7-03 | 后端选择器可解释 | Auto 选择显示设备、backend、不可用原因、强制 fallback；支持 support bundle。 |
| P7-04 | 着色器语义契约 | GLSL、MSL、HLSL、CPU reference 定义坐标、alpha、premult、颜色、HDR、采样边界；golden tests 共享。 |
| P7-05 | 精度与像素格式矩阵 | 8/10/12-bit、420/422/444、full/limited、linear/display、alpha、HDR 的性能与正确性矩阵。 |
| P7-06 | GPU 驱动黑名单/绕过 | 对已知崩溃/错误驱动给保守路径与说明；不可用 feature 不重复重试。 |
| P7-07 | 多 GPU / eGPU 边界 | 只有实测收益超过阈值且无 staging/cross-copy 才启用；默认保证单 GPU 正确稳定。 |
| P7-08 | 发布前硬件验证 | 每个发布声明支持的组合必须有版本化实机 artifact；没有数据则标“实验性”或不声明。 |

## 14. P8：高级效果、AI 与长期性能演进

| ID | 工作项 | 验收标准 |
|---|---|---|
| P8-01 | 时间域 effect cache | optical flow、denoise、motion blur、稳定的邻帧数据可分层缓存、失效和预分析。 |
| P8-02 | 光流/平滑转场质量-性能档 | 预览可用近似/低分辨率，最终交付高质量；用户明确知道当前档位。 |
| P8-03 | AI model 生命周期 | 加载、预热、显存上限、batch、取消、隔离崩溃、模型版本 key 全部可追踪。 |
| P8-04 | AI / 编辑资源仲裁 | 播放开始时 AI 自动让出资源；后台分析在 idle 才加速。 |
| P8-05 | OFX / 外部插件 profiler | 插件以帧耗时、GPU/CPU、内存、回读分类；慢插件给缓存建议和用户诊断。 |
| P8-06 | 远期 native hot-path 评估 | 只有 trace 证明 Python 调度成为主要瓶颈，才将 render graph / queue submit 下沉 native bridge；保留可测契约。 |
| P8-07 | 分布式/后台渲染评估 | 先完成本机缓存 key、确定性和 asset packaging；没有这些前不启动 farm。 |

## 15. Topos 422 Proxy 专项计划

Topos 的优势应通过实际时间线体验证明：低延迟 decode、可控磁盘占用、alpha 通道、与缓存/代理的平面直通。Composite 的持久缓存默认优先使用自研 TOPOS：连续段使用 Topos MOV，稀疏/局部/随机访问帧使用 `.toos` Topos 图片序列；GPU 临时缓存保持显存内，不为短生命周期结果编码落盘。它不能仅凭单帧 codec benchmark 替代完整系统测试。

### 15.1 优先事项

| ID | 项目 | 具体工作 | 验收 |
|---|---|---|---|
| T-01 | 当前性能复验 | 重跑 1080p/2K/4K、顺序/seek、冷/热、不同 worker 数；归档机器与 commit | 结果可复现，区分 native decode、upload、render、present。 |
| T-02 | 代理默认档位 | Proxy 档位、尺寸、alpha bit depth、I/O 和质量的经验表 | 自动选择有证据；用户可一键覆写。 |
| T-03 | 平面直通 | Topos decode planar → GPU plane import；Topos render cache planar → encoder | 受支持组合没有 RGB 往返；trail/trace 证明。 |
| T-04 | 解码核优化 | 对 dequant/IDCT SIMD、slice worker、缓存行为 A/B | bit-exact/容差测试通过，端到端 p95 有净收益。 |
| T-05 | 编码核优化 | 只优化已测热点（量化/fill/entropy/mux），而非盲目替换码流语法 | 输出兼容、体积/质量/速度同时满足门槛。 |
| T-06 | 并发治理 | playback/proxy/cache/export 协作式 worker budget | 前台播放 p95 不因后台 Topos 编码退化超过预算。 |
| T-07 | Alpha 全链路 | 代理、cache、preview、transition、export 的 alpha/预乘语义 | alpha roundtrip、边缘、叠加和导出对拍通过。 |
| T-08 | 与 DNxHR 的定位 | 保留 DNxHR 作为互操作/用户选择；按用途比较而非简单宣称无价值 | 文档/偏好说明清楚区分兼容性、性能、体积、alpha。 |
| T-09 | Composite 缓存默认接线 | 以统一 manifest/失效图管理 `gpu_ephemeral`、Topos MOV 与 TOOS；连续段/稀疏帧的选择阈值由真实 I/O、seek、体积和并发基准决定 | 任何 Topos cache 命中均保留帧、色彩、时间基和 alpha；局部失效只重建需要的 TOOS 帧。 |
| T-10 | TOPOS 缓存测试矩阵 | 将 Topos MOV 与 TOOS 置于 Composite、Edit、Deliver benchmark 第一对照组；覆盖 alpha、HDR/linear、8/10/12-bit、连续/稀疏、冷/热、顺序/随机访问与并发 | 报告 render、encode、I/O、decode、GPU import、copy bytes 和端到端 p50/p95；不以 codec-only 数字宣称体验收益。 |

### 15.2 需要持续报告的数据

- codec-native：encode/decode p50/p95、每阶段、每 CPU 核/worker、压缩比、错误率；
- 媒体层：open/seek、索引、磁盘吞吐、代理生成 ETA；
- GPU 层：plane import、转换、surface pool、upload/download bytes；
- 时间线层：实际 fps、drop、首帧、双轨转场、cache hit；
- 交付层：Topos render cache 复用、最终交付速度、色彩/alpha/PTS 回读。

## 16. 性能验证与测试策略

### 16.1 每个优化项的四道门

| 门 | 内容 | 失败时处理 |
|---|---|---|
| Correctness | 帧、PTS、色彩、HDR、alpha、音频、转场边界、取消语义 | 不讨论性能，先修正确性。 |
| Performance | 目标场景 p50/p95/p99、GPU time、吞吐、交互延迟 | 无净收益或收益低于噪声则不合入/不启用。 |
| Resource | RAM/VRAM/磁盘/线程/句柄/队列峰值与长跑趋势 | 增加预算/驱逐/泄漏修复，不能以 OOM 作为回收。 |
| Fallback | 缺 GPU、驱动异常、编解码失败、低空间、取消、项目关闭 | 必须安全、可解释、不污染输出或播放状态。 |

### 16.2 测试类型

| 类型 | 目的 | 频率 |
|---|---|---|
| 单元测试 | key、policy、budget、驱逐、时间基、capability、fallback reason | 每次提交 |
| deterministic render test | CPU/GPU/后端 golden frame、转场边界、颜色/alpha | 每次提交或目标模块变更 |
| trace contract test | event schema、copy counters、generation/lease 完整性 | 每次提交 |
| headless integration | 导入→代理→播放计划→缓存→导出，取消/失效/重开 | 每日 |
| GPU device test | 真实 decode/interop/encode/driver 行为 | nightly / release |
| benchmark | 冷热、长跑、场景矩阵 | nightly、性能 PR、release |
| soak / fuzz | 内存、资源、seek、工程修改、decoder 异常 | 每周 / release |
| 用户项目回放 | 真实大型工程、不同素材组合 | beta / release candidate |

### 16.3 数据保留

- 源代码只保存 benchmark schema、脚本、汇总和小型测试资产；大型媒体与原始 trace 放 artifact storage。
- 每次报告带 commit、配置 hash、机器/驱动、命令、素材 hash、结果 JSON、汇总 Markdown。
- 不上传用户路径、素材内容、项目名称和可识别 telemetry；support bundle 默认脱敏。

## 17. 推荐执行顺序

### 17.1 第一个迭代（先获得可信数据）

1. P0-01 至 P0-05：统一 trace、GPU 时间、复制与资源统计。
2. P0-06 至 P0-11：素材/工程矩阵与当前基线报告。
3. P1-02、P1-03、P1-05、P1-11：先修优先级、取消、miss 和 A/V 时钟这些体验底线。
4. P2-02、P2-03、P2-08、T-01：确认 Topos 422 Proxy 是否在真实项目上达到默认资格。

### 17.2 第二个迭代（把基础设施接到真正热路径）

1. P1-01、P1-04、P1-06 至 P1-10：收敛播放 engine 与 decode/render 调度。
2. P2-09 至 P2-16：session、ring、硬解/软件解码预算、索引。
3. P2-17 至 P2-24：缓存键、精确失效、GPU/disk cache 和 Deliver 复用。
4. P3-01 至 P3-05、P3-09：render graph、池化、pipeline cache 和同步审计。

### 17.3 第三个迭代（扩大重负载能力）

1. P3-06 至 P3-12：复杂效果、转场完整对拍、VRAM 管理。
2. P4-01 至 P4-10：导出流水线和 Topos/硬编整合。
3. P5-01 至 P5-10：海量媒体池、动态预览、波形、音频和主线程性能。
4. P6-01 至 P6-10：全局资源与 soak。

### 17.4 第四个迭代（平台与高级能力）

1. P4-M01 至 P4-M04、P4-W01 至 P4-W05、P7 全部：在真实硬件上闭环。
2. P8-01 至 P8-07：以不破坏前台体验为前提再做光流、AI、插件和远期 native 化。

## 18. 依赖与决策清单

| 决策 ID | 需要决定的事项 | 建议 |
|---|---|---|
| D-01 | 主性能目标机 | Apple Silicon 作为 macOS 主基准；现有 Intel/AMD 作为降级机；Windows NVIDIA/AMD/Intel 各保留一台。 |
| D-02 | 实时模式默认行为 | 默认启用格式感知 proxy/optimized media；重原片无代理时优先显示状态而非无限硬扛。 |
| D-03 | 预览质量降级 | 默认允许可逆预览降级，最终交付永不复用 preview 降级结果。 |
| D-04 | 性能回归阈值 | 同机同配置 p95 退化 ≥5% 或超出 budget 触发调查；按场景允许不同硬门。 |
| D-05 | GPU 后端宣传 | 只有真实 `trail`、画质和多机报告均通过后，才能称为“全链路 GPU”。 |
| D-06 | Topos 默认范围 | 先将 Topos 422 Proxy 作为 render cache/optimized media 默认；最终交付 codec 仍按用户交付需求选择。 |
| D-07 | 大型动态预览策略 | 256px 作为小/中卡片基准资产；大卡片按需加载更高分辨率版本，不全量常驻。 |
| D-08 | AI 并发 | AI 是低优先级后台消费者，播放、scrub、输出关键阶段可暂停/限速它。 |

## 19. 逐项完成模板

每个计划项的 PR/提交说明必须包含：

```markdown
### Performance work item
- ID: P?-??
- 场景与目标机：
- 改动前（p50/p95/p99、资源峰值、fallback）：
- 改动后（p50/p95/p99、资源峰值、fallback）：
- GPU trail / copy bytes：
- 正确性验证：
- 冷/热/长跑结果：
- 已知限制与下一步：
- 结果 artifact：
```

若性能没有改善，但修复了正确性、稳定性或可观测性，应如实写为“基础建设/正确性改进”，不能包装为优化。

## 20. 完成判定（Definition of Done）

本总计划不要求所有高级能力一次完成；每一个阶段的“完成”必须满足以下条件：

1. 相关目标场景的基准报告可复现，且相对基线达到已登记目标；
2. p95/p99、资源峰值、回退率和长跑趋势均达标，而非只有平均值提升；
3. CPU/GPU 复制、同步和 fallback 都有可核查 trace；
4. 正确性测试覆盖色彩、alpha、时间码/PTS、音频与取消；
5. 前台播放没有被后台 proxy/cache/export/GIF/AI 回归拖慢；
6. 不支持的后端/效果/设备都有明确、安全、用户可理解的降级；
7. 文档、偏好设置说明、HUD/诊断和发布声明同步更新；
8. 代码审查确认未新建无界队列、缓存、线程或每帧资源分配热点。
9. 凡修复播放、Composite、缓存、编解码、GPU、转场或导出，均已在 `tests/test_videos` 的受影响真实视频上完成错误与性能复验；结果 artifact 可追溯，未解释的失败/性能回退不能合并为“完成”。

## 21. 关联资料

本计划整合但不取代下列专项文档。后续实现应优先更新本总计划的状态，再在专项文档记录深入设计和实验细节。

- `docs/professional_nle_realtime_playback_optimization_plan.md`
- `docs/professional_nle_playback_engine_implementation_spec.md`
- `docs/windows_timeline_gpu_export_optimization_plan_2026-09-15.md`
- `docs/metal_backend_migration_and_cross_platform_gpu_plan.md`
- `docs/edit_optimization_plan_2026-09-07.md`
- `docs/composite_optimization_plan_2026-09-07.md`
- `docs/topos_prores_style_reduced_decode_implementation_plan.md`
- `docs/topos_v7b_reduced_decode_master_plan_2026-09-09.md`
- `native/topos_codec/docs/decode_speed_phase1_report_2026-09-06.md`
- `native/topos_codec/docs/ADR-C008-stage8-export-integration.md`

## 22. 首个可执行清单

为了避免总计划过大而迟迟不启动，下一次性能开发建议严格只完成以下 10 项并提交一份新基准：

- [x] P0-01：统一 frame trace id 与 event schema。（commit e84db1c6：`src/shared/video/frame_trace_schema.py`，engine→delegate→worker 全链路 trace_id；df8e2847 补 priority）
- [x] P0-03：增加 GPU pass timestamp 和 unavailable 标识。（commit e84db1c6：`src/shared/gpu/gl_pass_timer.py` GL_TIME_ELAPSED 延迟一帧读回，接入 Edit 合成 pass；不支持时 `gpu_time_unavailable`，不冒充）
- [x] P0-04：记录 upload/download/map/cross-device copy bytes。（commit e84db1c6：`src/shared/video/pixel_copy_counters.py` + planar 上传/快照 readback/CPU 回退三测量点 + 帧 trace `*_bytes` 字段）
- [x] P0-05：统一 RAM/VRAM/queue/pool/worker 快照。（commit e84db1c6：`src/shared/diagnostics/resource_snapshot.py` 注册表 + 1s 采样器 + 峰值；Edit 池已注册）
- [x] P0-06：冻结最小媒体与工程基准矩阵。（commit 39189f7c：37 条真实视频指纹清单 `tests/test_videos/benchmark_media_manifest.json`（B-01..B-06 全覆盖，--check 校验漂移）+ 确定性工程构建器 E-01..E-06）
- [x] P0-08：实现冷/热/seek/长跑 benchmark runner。（commit 94f42792：四阶段协议 `tests/benchmark/benchmark_runner.py`，走 MediaSourceFactory 真实解码入口）
- [x] P0-10：在 Edit/Deliver 提供脱敏 performance HUD/trace 导出。（commit 7b13746e：`performance_bundle.py` 脱敏导出 + HUD 深度诊断行 + Export Performance Report 菜单）
- [x] P0-11：在目标 macOS 重跑 B-01 至 B-04。（commit df8e2847：`docs/p0_baseline_report_macos_2026-09-16.md` + `bench_out/p0_runner/*.json`；Windows 真机部分待 P7 闭环）
- [x] P1-02：实现全局任务优先级与满队列丢弃协议。（commit df8e2847：`task_priority.py` 协议 + 渲染器满队列按优先级裁决，低优先级绝不挤掉高优先级排队项）
- [x] T-01 / T-10：Topos 422 家族 decode/seek/cold 已复验（T-01 部分完成：4K_topos 75.6fps、proxy 212fps、seek p95 16ms；发现 4444 测试素材 `alpha_mode=0` 缺口）；TOOS 图片序列与 Composite 缓存端到端（T-09/T-10 剩余部分）待下一迭代。

完成这 10 项以后，才能可靠决定下一个性能瓶颈是代理、解码、GPU 合成、效果、I/O 还是 UI；这会使后续每一笔优化都更有把握。

### 22.1 首轮基线结论（2026-09-16，macOS x86_64）

详见 `docs/p0_baseline_report_macos_2026-09-16.md` 与 `docs/topos_422_family_benchmark_2026-09-16.md`。要点：

- 4K ProRes HQ 软解 32fps（E-06 复现）→ realtime 必须代理接管（D-02 依据）。
- 4K Topos 75.6fps / Topos proxy 212fps / seek p95 16ms；2K 家族全部 140fps 以上（proxy 740fps）→ T-02 默认档位有实证基础。
- 4K H265 长 GOP seek p95 519ms → P2-05 seek 索引立项。**已部分解除（2026-09-16，commit 1f710878）**：根因是 GOP 帧线程 seek 的管线排空税（~120ms/次）+ 短 walk 慢于 slice 并行，非 demux 定位；持久化 GOP 索引 + HEVC SLICE seek 会话后复测 266.7ms（-49%）。剩余缺口为 HEVC 软解单帧成本（~13ms/帧），归 P2-14；预算 BUD-B02-SEEK-P95 维持 known_breach 并更新责任指向。
- Topos 4444 测试素材 `alpha_mode=0`：~~alpha 全链路（T-07/T-10）尚无真实素材覆盖~~ **已解除（2026-09-16）**：`tools/generate_topos_test_media.py --alpha` 重生成 4444 档（alpha mode1 无损 16-bit），产品解码路径验证 4 平面 + alpha 值域真实；含 alpha 4K 4444 解码 40.9fps。

### 22.2 第二迭代第一批（2026-09-16，分支 fix-ui）

| 提交 | 内容 | 备注 |
|---|---|---|
| ceb2fe45 | P0-02 收尾 + P1-06 | (trace_id, deadline_ns) 贯通 guard-drop trace；SEGMENT_BUDGET_RATIOS 分段预算 + 首段越界归因（decode→composite→total）；schema v2 |
| 108f1e46 | P0-12 + P0-09 本地 | `config/performance_budgets.json` 硬/软门 + known_breach 免责（H265 seek→P2-05、ProRes steady→P2-02）；`tools/perf_regression_gate.py` 同机 ≥5% 回归门禁，完整基线 exit 0 |
| 94f84310 | T-07 alpha 素材 | 4444 档带真 alpha（mode1/16bit），产品解码 4 平面验证；alpha 4K 4444 解码 40.9fps（无 alpha 版 174fps）；矩阵重冻结 |
| d7f93f73 | P1-01 步骤 1-3 | `engine.request_scrub_present` 外壳（scrub 帧 trace_id≠0、priority=scrub_latest，renderer 状态机不动）；`dispatch_channel` 正字段（engine/scrub/background/direct/refresh）；背压/路由 miss 进 summary 词表；shutdown 清理。步骤 4（播放状态单源化）单独迭代 |
| 407a0f30 | P2-02 + P2-03 + T-02 | codec 感知策略（topos 全分辨率豁免；long_gop/heavy_edit 原因码分级）；未知尺寸显式 `dimensions_unknown`（修死路径 BUG）；播放挂起/恢复代理生成已接线；实证档位表 `docs/topos_proxy_default_policy_2026-09-16.md` |

复查修复（a3a6ecf5，前一批产物）：满队列窥视竞态、engine TypeError 双重提交、Windows 路径脱敏失效、GPU 时间观测链断裂、runner 泄漏/回绕污染等 14 项。

剩余入口：T-03 后半段（render cache planar→encoder，需与 PNG 逐位同质契约一并论证）；P2-10（per-source ring / 反放解码路径，需 GUI 实机验证）；P2-18 起精确失效图/撤销复用；P2-14 解码内核（B02 seek 剩余缺口承接）。

### 22.3 第二迭代第二批（2026-09-16，分支 fix-ui）

| 提交 | 内容 | 备注 |
|---|---|---|
| 1f710878 | P2-05 | `src/shared/media/gop_index.py` 持久化 keyframe 索引（demux 扫描不解码，md5(path\|mtime\|size) 失效，ProxyManifest 模式落盘，首次 GOP seek 后台构建）；`FFmpegMediaSource` HEVC 类 seek 路由独立 SLICE 会话（帧数据与旧路径逐字节一致），索引收紧 walk 上界，`keyframe_interval_hint` 接入 decode manager 决策。实测（benchmark_runner 口径，macos_intel_x86_64）：4K_H265 seek p95 **519.3 → 266.7ms（-49%）**；steady/cold/H264 无回退（H264 seek 65.5ms、steady 441.7fps）。根因记录：GOP 帧线程（AUTO）每次 container.seek 排空管线 ~120ms + 短 walk 慢于 slice 并行（251 vs 146ms p50）；H264 相反必须留在主会话（walk 51ms、顺序 441fps）。剩余缺口 = HEVC 软解单帧成本（~13ms/帧 × 平均 11 帧）→ P2-14；BUD-B02-SEEK-P95 维持 known_breach，note 已更新。测试 `tests/media/test_gop_index.py` 20 项 + tests/media 2926 passed（3 项 native CLI 陈旧 build 失败与本批无关）。artifacts：`bench_out/p2_05_after/`（gitignored） |
| b6146ee4 | T-03（前半段） | 审计结论：Edit GPU 播放主路径**本就是平面直通**（native 解码 → uint16 平面 → GL R16UI 直传 → shader 内 YUV→RGB，无 CPU RGB 往返），缺的是证据链。本批：① 修 GBR 预览错色（rgb_planar 分支曾换序 + 走 YUV 矩阵渲染；现 native G,B,R 顺序 + gbr_mode=True，edit_gpu_preview_renderer 与 timeline_clip_frame_provider 两处）② 修 topos_gbrap* 误分类 yuv420（分类器缺 "gbrap" 匹配导致几何断言失败）③ render_cache_encoder 消除整帧 BGR→RGB 翻转拷贝（两编码器均原生吃 bgr，FFmpeg 直填路径 bit 级一致）④ 证据链：`FrameTraceCollector.annotate()` 按 trace_id 迟到合并 GL 异步上传证据（bytes/mode/pixel_format/gbr/import_upload_ms）进成功 trace，`evaluate_deadline_miss` 补上 import_upload 段归因。验证：standalone GL 数值对拍（GBR 通道映射 ±2 容差）+ 19 项新测试 + media/edit/shared/video 回归绿。已知限制：非零拷贝（每帧 CPU→GPU 平面拷贝，IOSurface 零拷贝仅服务 Metal 导出）；R16UI self-test 失败的 float16 CPU 归一化回退仍在（现 trace 可见，非静默）；后半段 render cache planar→encoder 未做（缓存链 packed-BGR 为 headless 设计 + PNG 逐位同质契约，需单独论证） |
| e8b59ff6 | P2-09 | EditGpuDecodeManager 会话池有界化（验收"有上限、租约和关闭策略"；按素材/codec/backend 复用本就由 `_SourceKey` 缓存承担）：① 上限 `_MAX_LIVE_SOURCES=12` + LRU 逐出（last_used 最旧优先，单次插入最多逐 2 个防 VideoToolbox 关闭风暴，逐出对象在管理器锁外 close——契约有测试锁定）② 引用租约：get_frame 热路径/慢路径、get_frame_interactive、preposition_stream_frame、播放预取线程全部"取出即 +1、finally 归还"，逐出只回收租约 0 的会话（实现中曾漏掉 `_get_or_create_source` 新插入路径的租约，被租约断言测试当场抓出并修复）③ `_interactive_busy_sources` set[int]→id→本体字典，修 id 复用误标 busy 隐患 ④ 负缓存：上限 256 超限清空、close_source 按素材清、close_all/invalidate 全清 ⑤ `session_pool_stats()` + `session_evicted` trace 证据。决策：素材删除**不**接线 close_source——删除是可撤销命令，撤销即需原会话，关闭反而制造 open/close 抖动；回收交给 LRU 上限。实测（bench_out/p2_09，真实 4K 素材）：16 流轮转恰好逐出 4 冷流、存活恒 ≤12、活跃双流 H265 108.7fps / ProRes 47fps 不受逐出影响、被逐出流重建正常；单流顺序 H265 107fps（p50 4.2ms）/ ProRes 63fps。测试 13 项新增 + tests/media 2962 passed + edit 回归绿（3 项 toos native CLI 陈旧 build 失败与本批无关）。已知边界：池上限只管 Edit 页主会话（Composite 侧 VFPS 已有 ClipSession 引用计数）；P2-10 per-source ring 与反放解码路径未动 |
| c3066f21 | P2-17 | 调研结论：项目有两套互不相通的渲染缓存——A 套 `SegmentRenderKey`+`DiskRenderCache`（内容寻址，导出 comp-clip 帧级缓存，env opt-in 默认关）键骨架达标但取值大量硬编码占位；B 套 `RenderCacheManifest`（clip_id+uuid 寻址，时间线绿条）composite 类命中零校验。本批 A 套：① 键构造期归一化（dict→有序对、list→元组），键实例可哈希可去重（P2-18 前置），修复 from_mapping 读回 list 与构造 tuple 的 __eq__/digest 不一致裂缝 ② 新增 `media_versions` 字段（图引用外部文件逐路径 size+mtime_ns，缺失记 -1 也是身份）③ 键构造器接真实值：output_format 扩 (宽,高,fps_num,fps_den,位深)，grade 补 matrix，effects 从挪用的位深标记还原诚实空值，color_management 绑定新常量 `EXPORT_OETF_VERSION`（OETF 实现身份），renderer_version 绑定新常量 `COMPOSITE_GRAPH_RENDERER_VERSION`（渲染器实现身份）。本批 B 套（避免误命中）：composite 命中补时间线分辨率/帧率校验（对齐 MG 已有校验，edit_controller 的 format_matches 不再对 composite 恒 True）。语义决策：retime/effects 诚实取空——该缓存是合成前单层帧，clip 级效果/变速在下游应用。测试 12 项新增 + tests/export 全量 + edit 渲染缓存相关 235 passed。边界：B 套深层问题（精确失效图/撤销复用/is_valid_for 零调用/revision 字段空置/composite 图与调色栈编辑不触发失效）属 P2-18，未动 |
| 1398ea65 | P1-01 步骤 4 | 播放状态单源化：`EditPlaybackController.state_changed` 只在 `RealtimePlaybackEngine` 建立桥接；引擎将状态统一转换为 REALTIME/PAUSED 并发出 `playback_active_changed`，`TimelinePreviewRenderer` 订阅后集中处理质量策略、代理 QoS、GPU Viewer 模式和预热；TimelinePanel 移除重复的直接通知路径。新增控制器重绑定/旧控制器隔离测试。验证：播放引擎、架构门禁和预览策略 42 项通过；未替代 seek、反放和真实素材性能实机验收。 |
| c1f19b86 | T-03 后半段第一步 | `RenderCacheEncoder.write_frame()` / `write_frame_planar()` 接受 `DecodedFrame`，RenderCacheWorker 对已经是平面帧的结果直接交给编码器，并对几何不一致显式失败；packed BGR 旧路径保持不变。验证：T-03、缓存生命周期和 Topos 导出 164 项通过。边界：现有 headless `_CpuCompClipRenderer` 仍产生 packed BGR，GPU/Topos 平面产出端接线与真实素材测量尚未完成。 |
| 99e0c2da | P2-18 第一阶段 | 新增 `render_fingerprint`：按时间线输出规格、clip 参数/效果、合成图、引用调色栈、源文件 size+mtime 和缓存设置生成语义指纹；缓存状态查询、缓存创建复用判断、Motion Graphic 读取均核对指纹，漏掉一次命令级失效也不会误报绿色。旧 manifest 空指纹保持兼容；同时修复轻量控制器缺少清理辅助方法时会跳过失效标记的问题。验证：缓存/指标/效果失效相关 57 项 + 渲染器缓存回归 42 项通过。边界：旧缓存不会凭空获得指纹；撤销复用、多依赖版本和真实素材全链路仍属后续阶段。 |
- 长跑 600 帧 RSS Δ ≤ 13.4MB，无泄漏迹象（B-08 正式长跑仍需 30 分钟协议）。

### 22.4 S0-1 实时播放正式接管批次（2026-09-16，分支 dev）

| 提交 | 内容 | 备注 |
|---|---|---|
| f2f89475 | 隐藏缺陷复查批次 | 对此前全部已完成项的 12 项隐藏缺陷修复（外源 YUV sampler 绑定、source 纹理归属泄漏、GPU 池预算误淘汰、前台/预取优先级、租约泄漏、prefetch latest-only、跨线程缓存清理、GL 单例竞态等），逐项根因与回归测试见 `docs/composite_fullchain_gpu_audit_plan_2026-09-15.md` §十一；全量对拍证明 24 项失败均为存量基线问题，零回退 |
| 42221aec | P1-05 miss 语义统一 | ① 新增 `MissReason` 词表（decode_miss / gpu_busy / cache_warming / proxy_pending / unsupported_effect / back_pressure / delegate_miss）+ `classify_miss_reason()` 归一化已知生产方字符串（scheduler bundle miss、renderer GPU 结果 drop、frame provider 解码失败）② `RealtimePlaybackPolicy.should_rebuffer` 按类别决策：proxy_pending 立即 rebuffer、unsupported_effect 永不 rebuffer、其余瞬态类别 hold 优先达阈值才 rebuffer ③ **修复语义缺陷**：`engine._handle_miss` 曾伪造 `source_kind=PLACEHOLDER` 的 miss，policy 对 PLACEHOLDER 无条件立即 rebuffer，导致首次 delegate miss 就进 REBUFFERING、注释声明的"首次 miss hold"分支实际不可达；现在瞬态 miss 用 `SourceKind.NONE`，仅 proxy_pending 保留 PLACEHOLDER ④ `request_present` 单次解析归因（消费 worker 上报槽），telemetry / hold / rebuffer / warning 共用同一 reason；back_pressure 同样 hold-first ⑤ 新增 `engine.note_playback_miss()`（generation 围栏 + 单槽消费），renderer 在实时 GPU 结果 drop 点（no-present、playback-stale、render error）上报真实归因。验证：policy/engine +38 项新测试；tests/edit 全量失败集与基线 f2f89475 完全一致（25 failed + 7 errors 全为存量），7867 passed（+38）。边界：异步 drop 只做归因上报、尚未计入 engine miss 计数（engine 的 presented 统计仍以"delegate 提交成功"为准）——计入属 P1-01 "同步 ready bundle 反馈"范围，需实机验收阈值后接入 |
| 0b47f58e | P1-10 自适应播放质量 | ① 新增纯逻辑 `AdaptiveQualityController`（`realtime_playback/adaptive_quality.py`）：NORMAL/ELEVATED/CRITICAL 三档（auto2k 基准 ×1.0/0.5/0.25），迟滞（连续压力 N 帧才降档、连续健康 M 帧才升档）+ 每档最小驻留（换目标尺寸会命中解码管理器 64px 桶缓存键重建会话，防抖动风暴）+ 归因感知（复用 P1-05 词表：只有降 scale 有益的 miss 类别计入压力，proxy_pending/unsupported_effect 不计）② engine 每实时 tick 观测（miss 归因 + 预算 back-pressure），档位变化经新信号 `quality_pressure_changed` 广播（旧档/新档/streak/scale），seek 与每次播放重启都回 NORMAL 重评估（可见可回退）③ renderer 把档位映射为 AUTO_2K 目标上的缩放系数：仅在 engine 播放态、非 scrub、且预览质量为 auto/smooth 时生效（用户显式分辨率选择=手动覆盖，自适应不介入）；变更复用既有缓存+generation 失效链 ④ 用户偏好门控：新配置键 `video_preview_adaptive_quality`（默认开，旧配置文件合并回落默认），file_controller → renderer → engine 透传，关闭即钉 NORMAL。验证：27 项新测试（控制器状态机含双向驻留、engine 信号/重置/偏好、renderer 映射 + shell 兼容）；tests/edit 全量失败集与已记录基线完全一致，7894 passed（+27）；config 旧文件合并回落 True 已验证。边界：默认阈值（降档 6 帧/升档 24 帧/驻留 30 帧 @25fps≈0.24s/0.96s/1.2s）为保守初值，**端到端 FPS 收益与阈值调优需实机 tests/test_videos 验收**（P0-13）；效果旁路级别（FX-M04 PolicyContext 注入实测 p95 预算）与代理分级未在本批 |
| 3a334864 | P1-07 预取窗口方向/变速感知 | 现状核查结论：方向感知与跳转撤销已存在（`controller.playback_direction` → scheduler `prepare_window` 按方向翻转 radius_before/after 并倒序填充；latest-only 取代 + cancel_event 在方向/帧号变化时立即停止旧窗口，A→B→A 竞态已由隐藏缺陷批次 12ac277f 修复）。缺口是 J/K/L 变速：radius_after 固定 8，4x/8x 穿梭时预取耗尽退化为 hold。本批：engine 新增 `scheduler_rate_provider`（configure + 运行时 `configure_decode_scheduler`），`_schedule_decode_window` 把行进方向 lookahead 按倍率 clamp(rate,1,8) 只放大不缩小、上限 `_MAX_SCHEDULER_RADIUS_AFTER=32`（规格书 §9.3 前瞻 8-30 上界附近；超出 ring 容量的部分只是徒劳解码）；renderer 接 `rate_provider=_playback_rate_multiplier`（读 `controller.playback_speed`，shell 安全回退 1.0）。验证：9 项新测试（2x/4x/8x 扩展与上限、慢放不缩窗、无/异常 provider 回退、反放+2x 组合、renderer 契约）；受影响播放/renderer 套件 306 项通过。边界：scrub 不走 scheduler 窗口（renderer 自有单飞 latest-only，属既定设计）；变速扩展的端到端收益需实机验收；J/K/L 的 UI 触发链未在本批改动 |
| 71487af9 | S0-1 实机验收清单 | `docs/s01_realtime_takeover_real_machine_acceptance_2026-09-17.md`：P1-05/07/10 + ring bundle 命中 + Comp Clip 零 readback 的场景矩阵、trace 字段、pass/fail 判据与 P1-10 调参矩阵（A/B/C 组），按 P0-13 门在 GUI 实机执行 |
| a6e948b3 | T-03 后半段 | 渲染缓存产出端 planar 直通：`_CpuCompClipRenderer` 对恒等单层图（无调色/无效果/无混合模式/几何与 manifest 逐像素一致）直接返回解码平面 `DecodedFrame`，经既有 worker 分支进 `write_frame_planar`；格式白名单从 `topos_encoder` 导入（编码器扩展自动跟随）；任何不满足回退合成路径（拒绝发生在解码前，不双重解码）；`PreviewRenderWorker._get_decoded_frame_for_cache_passthrough` 与非实时解码同语义（proxy→master 帧准确）免 packed 提取。**实测（真实 2K_topos_proxy.mov，1080p，macOS Intel）**：旧链 planar→BGR(8bit)→packed 编码 31.3ms/帧（31.9fps）、mean abs err 14.87/1023；新链 9.4ms/帧（106.7fps，**3.35x**）、mean abs err **1.986/1023**——中间 8-bit 量化往返消除，缓存更快且更贴近源。验证：11 项单测（资格矩阵+render_frame 接线）+ 真实素材门 3 项（解码契约/往返保真界/直通不劣于旧链）+ media topos/render-cache 1131 + edit render-cache 262 通过。边界：多图层/带效果图仍走 BGR 合成（效果后 planar 化属 P1-14）；FFmpeg 直连源 packed BGR 不变（planar 化需 read_frame_planar 接线，另行评估） |
| （复测） | P2-14 第一阶段：dequant/IDCT 复测 | 按计划"先复测 dequant/IDCT"完成，**结论：内核已达该分解的向量化上限，单帧成本无安全收益空间，B02 剩余缺口的可行杠杆不在每帧解码**。证据（macOS Intel i9-9900K，当前源码 release 构建 + `prof_decode` + `sample` 采样，85 项原生测试全过）：① 1080p 帧解码 3.4ms wall（999% CPU 多 slice 并行）；热点占比：`tc_dequant_inverse_8x8_avx2` ≈31%（关键帧）/≈42%（delta 帧 slice 内），rANS2 熵解码本体 ≈33%，slice 调度/并行框架开销 <1%，`tc_rice_unmap` ≈2%——与 M10 系列内核注释一致；② 四块 SoA 批量 IDCT 已被 P1-06 A/B 实测否决（劣化 15-25%，逐块内核已是 lanes=x 向量化形态，代码与 bit-exact 差分保留）；③ B02 缺口（HEVC 软解 ~13ms/帧）复测：4K_H265 单线程 72.9ms/帧、SLICE+auto 线程 **11.2ms/帧**（与文档 13ms 吻合，线程配置已最优）；**VideoToolbox HEVC 硬解实测 32.9ms/帧**（p010le 输出含下载税，UHD630 10-bit 半率），反而更慢——本机每帧成本无剩余杠杆。B02 breach 维持 known_breach；剩余可行方向 = walk 帧数削减（P2-10 家族：seek 锚点缓存/近邻会话复用，需实机验证）或目标机硬件矩阵（P4-M04） |
| 965ac293 | P4 基线：导出端到端首测 | `tools/bench_export_baseline.py`：headless 驱动真实 `TimelineExporter.export_timeline` 的 codec 矩阵基准（h264/prores_422/dnxhr_lb/topos_proxy），JSON 落 `bench_out/export_baseline/`（gitignored）。**首次本机基线（macOS Intel i9-9900K，24 帧）**：2K_H264 源 → h264 **8.6fps** / prores_422 12.1 / dnxhr_lb 41.5 / topos_proxy **43.2**；2K_topos 源 → 全档 10.6~11.5fps；4K_H265 源 → h264 4.4 / prores 2.7 / dnxhr 8.8 / topos_proxy 1.9。发现（按 P4 拆解）：① H264 导出未配置 preset → libx264 默认 medium，是 8.6fps 的首要嫌疑（P4-04 会话/预设管理）；② Topos 源导出走 high-depth FFmpeg 探测回退（TPIC 非法属正确拒绝，日志 warning 为预期噪声），解码侧主导了 11.4fps——**P4-05 Topos 平面直通导出（复用 T-03 产出端契约）是明确杠杆**；③ topos_proxy 编码器本体 43fps@2K，不是瓶颈（口径勘误见 §22.5） |

### 22.5 P4-05 Topos 段：Topos 中间片导出 YUV 域直通（2026-09-17，分支 fix-ui，与 §11.6 并行会话合并后）

| 提交 | 内容 | 备注 |
|---|---|---|
| 4b411b3f | P4-05 Topos 段 | Topos TPIC 中间片源此前被直通门整体拒绝（§11.6.1 的 tier 顾虑只适用于容器位流拷贝；解码平面交付语义由目标 spec 重新定标），全档导出困在 ~11fps。本批：① 直通门对 topos 资产改走 provider 既有会话的原生解码平面（ToposMediaSource 契约 = T-03 同款），siting 从帧 extra `topos_chroma_siting` 读取，asset 键读 `codec`（导入路径持久化键，兼容 `codec_name`）② 新规格：h264/hevc yuv420p←422p10le（YUV 域 >>2 降位+色度半高一次转换）、prores yuv422p10le 同格式直填、prores/dnxhr yuv422p 降位、dnxhr yuv422p10le 同格式 ③ 修复交付段色度目标几何按目标 pix_fmt 推导（曾硬编码 420 半高 → 422 目标 got/need 字节不符）④ `_encode_frame` 白名单放行 prores/dnxhr 同格式平面。**实测（macOS Intel i9-9900K，2K_topos.mov 1080p25，240 帧）**：h264 11.0→**61.9fps（5.7x）**、dnxhr_lb 11.0→**80.3fps（7.3x）**、topos_proxy 11.2→**157.2fps（14.0x）**、prores_422 9.9→**15.6fps（1.6x，已达 prores_ks 59ms/帧软件编码地板，threads 1/0/4/8 实测均无收益）**。正确性（stash 对拍 48 帧）：h264 MAD 2.235/255（luma 无偏，R/B ±2.1 为 420 子采样相位差）、prores 0.617、dnxhr 0.821、topos_proxy 0.402；输出 probe 全对。验证：7 项新单测（gate 矩阵/白名单/几何回归）+ 5 项真实素材端到端门（P0-13）；tests/edit 全量对拍失败集与基线**逐项一致 23 项全存量**，7964→7971 passed（+7 恰为本批新测试）。**基线口径勘误（承 965ac293 行）**：该批 h264 8.6fps 与"未配置 preset"归因作废——bench 曾硬编码 24fps 时间线，源实际 25fps，fps 一致性门**正确**拒绝直通造成假慢；修正后 h264 恒等源实测 149fps@480 帧（VT 硬编已由并行会话接入，preset 缺口仅剩 CPU 回退路径，价值重估后不单独立项）；2K_topos 源 11fps 结论仍成立并由本批解决。基准工具同步修正（探测源真实 fps + 产品导入路径元数据）。另：首帧黑帧竞态（§11.6.2 Windows 侧发现）macOS 11 次未复现，协作记录入 Windows 文档；`tools/ab_topos_passthrough.py` + `tools/compare_ab_outputs.py` + `tools/repro_first_frame_black.py` 入库可复跑 |
| 451c9f69 | P4-05 续：10-bit 420 源 | `yuv420p10le` 源（4K HEVC/HDR 手机素材常见）此前只有 hevc 交付档，其余全档合成慢路。补规格：h264 yuv420p←420p10le（>>2 降位直交付，撤下"降位通道仅 hevc 档"旧策）、prores/dnxhr/topos 的 422 交付走 420→422 色度域垂直上采样（位深不变或降位组合）；交付段色度子采样域变化触发平面重排、重排后帧格式改写为目标 spec；0/2 siting 豁免按交付目标域判定；新规格 untagged 豁免含 h264/hevc。**实测（4K_H265.mov 3840x2160@25，120 帧，序列=源分辨率）**：h264 8.9→**23.4fps（2.6x）**、dnxhr_lb 10.5→**27.1fps（2.6x）**、topos_proxy 2.1→**45.1fps（21.4x）**、prores_422 2.0→**4.5fps（2.25x，prores_ks 4K ~230ms/帧编码地板）**。正确性（stash 对拍 24 帧）：4K topos_proxy **Y 平面逐位一致**、U 0.034/V 0.075/1023；4K h264 MAD 3.344/255（G 稳定 R/B 色度相位差——新链保留源生 420 色度，旧链经上采样→矩阵→降采样低通）。验证：4 项新单测 55 项全过 + 真实素材门 3 项（Y MAD<16/1023 实测 ~5.5）；tests/edit 失败集与基线逐项一致，7971→7975 passed。bench 口径补漏：序列分辨率探测源设置（此前默认 1080p，4K 源成缩放场景）；测试源平面取产品同款 planar 契约（PyAV gray16le 重格式化做 limited→full 拉伸不可比） |

S0-1 剩余缺口（均需按 §20.9/P0-13 在实机 GUI + `tests/test_videos` 验收）：P1-08 无阻塞 seek/open、P1-13/P1-14 Comp Clip 实时零 readback 与预渲（Ring 消费已在 worker 层就绪，`scheduler_bundle_hit` 可观测）、P1-10 阈值调优与端到端收益实测、engine `_execute_delegate` 默认路径的同步 ready bundle 查询（engine.py 注释 Batch 6 TODO）。
