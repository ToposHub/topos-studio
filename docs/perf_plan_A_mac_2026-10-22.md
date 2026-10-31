# 计划表 A · Mac 线（v3：先归因，按门禁推进）

> 目标机器：i9-9900K / RX 6900 XT 16GB / macOS；机器、驱动与运行时以实际采集为准。
> 建议独立分支：`perf/a-playback`；本文件描述后续工作，不代表已执行或已测出收益。
> 配套：[Windows 计划 B](perf_plan_B_windows_2026-10-22.md)；依据：[审查报告](perf_plans_review_2026-10-22.md)。
> 总纲 `perf_davinci_parity_dual_track_plan_2026-10-22.md` 尚为旧版；执行本线时，以本文件和 B v3 的共同口径为准。旧总纲中的 GC、700fps、固定周数与 Token 配额不作为门禁。

## 0. 目标、事实与推进规则

**目标**：改善真实 4K 时间线的播放稳定性、首帧/seek 延迟和 CPU/内存水位，并保持 HDR、效果、preview/export 语义和回退能力。解码器吞吐、CPU 拷贝数只是归因指标。

当前源码可确认的事实：

- Mac 已有 `MacOSVtZeroCopySource` 和 IOSurface→GL 路径；是否使用取决于格式、能力探测、播放策略和开关。不能把所有 H264/H265 都视为硬解零拷贝。ProRes 走 VT 也不等于本机硬件解码，须记录实际 session 与回退原因。
- GPU 成功路径使用纹理/fence，CPU 回退和外接监看仍可能经过 QImage。`GpuFramePool` 已有实例化、统计、切代接线，上传消费是否使用需沿调用链确认；`AVSyncController` 也需确认实际消费者。
- `PlaybackFrameTelemetry` 已有 decode/upload/composite/present/total，但缺少充分的阶段有效性和呈现语义；`enrich_last()` 已支持 trace_id 匹配，并非完全没有回填能力。
- `gpu_pass_timer_stats()` 读取的是真实 GPU query 结果，但有帧延迟；槽位 ACK 也可能对应未显示即被替换的帧。
- DeckLink 的 `submit_gpu_texture()` 原生实现是占位，`supports_gpu_texture=False`。现阶段必须保留 CPU 回读。
- Metal 已有编译、compute、桥接和效果对拍资产；这些不等于生产实时预览已经完成大部分工作。

**推进规则**：A0 可以先做；A1/A2 只有热点证据成立才做；A3/A4 必须另过立项门禁。每个任务按「证据→最小变更→独立审查→focused test→同机基准」收口。没有测量依据，不承诺固定收益、工期或 Token 消耗。

## 1. 开工与协作边界

先检查当前分支与用户改动；不把 checkout/pull/环境重建串在自检命令后自动执行。需要隔离时，从明确的基准提交创建独立工作目录，并在记录中写明 base/head。

```bash
git status --short
git branch --show-current
git rev-parse HEAD
.venv/bin/python --version
.venv/bin/python -c "import av, moderngl, PyQt6; print('av', av.__version__); print('ffmpeg', av.library_versions)"
```

A/B 共同所有权约定（替代旧总纲中目录重叠的部分）：

| 范围 | 主责与交接 |
|---|---|
| `realtime_playback/`、Mac VT、Color/Metal | A；B 提出共享接口需求，A 负责集成 |
| `windows_hw/`、Windows 探针和桥接实现 | B；复用现有模块，避免平行建立第二套协议 |
| telemetry、帧/租约契约、`backend_types.py`、`composition_uniforms.py` | A/B 先约定 schema/ABI，由一个任务串行修改 |
| 将来的 `src/shared/gpu/d3d/`、Color 下 HLSL 文件 | 仅 ADR 证明有必要才新增；B 实施，A 审查共享边界 |
| `tests/`、`docs/`、基准 harness | 随任务补充；按具体文件分工，不设“共享只读”限制 |

所有实现遵循 `.clinerules`：复用 registry/service，渲染线程创建与释放 GPU 资源；禁止在 UI/脚本线程新增解码、效果计算或资源操作。测试中的 GPU 工作也须使用其既有隔离上下文。

## 2. A/B 共同测量契约（A0.1 定稿，B 复用）

### 2.1 帧身份、阶段与呈现状态

- 以 `(session_id, generation_id, trace_id)` 关联一次请求；源素材帧身份与 timeline_frame 分别记录。槽位事件还要带 slot_id 和租约代次，防复用错配。
- 保留旧字段及消费者兼容；为新报告增加 validity/status/reason。实测 0、该路径无需该操作、未测量/不支持三种情况分开表示，禁止用默认 0 冒充有效数据。
- 六类诊断为：CPU 编排、解码、导入/上传、GPU 合成、回读、呈现相关事件。另记队列等待与请求到可观测呈现的 wall time；异步阶段可能重叠，不把六段直接相加当作总耗时。
- GPU query 绑定发起帧身份，完成后回填原 trace；不能把最近读到的旧 query 写进当前帧。跨代/淘汰/失败的迟到结果显式丢弃并计数。
- 呈现区分 submitted、superseded、viewer_copy、可观测 swap/present、slot_released。ACK 只证明资源协议推进；无平台反馈时不得声称测到物理屏幕扫描时间。GPU 完成事件也不能用 CPU 发出绘制命令代替。
- 未完成、丢弃、超时样本单独统计；延迟分位数同时列出样本总数和覆盖率，不能只统计成功帧掩盖卡顿。使用单调时钟，不混减 wall-clock 与 GPU 时间戳。

### 2.2 场景、指纹与比较纪律

| 维度 | 最小覆盖 |
|---|---|
| 素材 | 2K/4K H264、HEVC 10bit、ProRes422HQ、DNxHR HQX、Topos；由实际文件生成清单，不硬编码旧文件数 |
| 时间线 | 单轨、至少双轨、代表性效果栈；记录源/预览分辨率、目标 fps、代理/缓存和效果参数 |
| 操作 | 连续播放、冷启动/首帧、seek/拖动、暂停恢复；监看开/关分组 |
| 运行模式 | 离屏吞吐与真实窗口播放分开；前者不验收上屏、vsync 或可见掉帧 |
| 资源 | CPU、RSS、可取得的 GPU/显存指标、队列深度；不可采集项标 unavailable |

起步采样建议：稳态预热不少于 30 帧、计时至少 240 帧，素材不足时如实记录；冷启动另测且不先预热。每配置至少 5 组交错/成对 A/B 采样，首轮据噪声决定是否增加。连续播放先做 10min，再由变更风险决定 soak；这些是协议起点，不是性能承诺。

- 每组记录素材哈希、时间线/配置指纹、CPU/GPU/驱动/OS、Python/Qt/PyAV/FFmpeg/ModernGL、base/head/dirty 和采样参数。源 fps 决定帧预算 `1000/fps`；不把所有项目固定为 25 或 60fps。
- 复用 `tools/bench_manifest.py` 的指纹与归档纪律；涉及 Topos native codec 时继续强校验实际加载库的版本、hash 和提交。先评估/扩展公共 manifest 对不依赖该库的场景的支持，不把 codec 库的存在当作普通 H264 基准前置条件；实际依赖不能省略。
- 结果写独立运行目录；保留旧锚点，不覆盖。dirty 结果只能作探索，正式对比使用可复现的提交/配置；跨机器和跨运行时各列一组，不能混成前后提升。
- A0 基线冻结时为每项优化登记一个主要指标、允许退化项/限值和停止条件。报告 p50/p95、掉帧/重复/过期帧、置信区间及绝对值。超过预登记限值回退；噪声范围内不得宣布提升。不能用拷贝少了证明用户体验必然变快。

## Phase 0 · 优先完成：测量与基线

### A0.1 遥测协议与最小 harness

**落点**：`realtime_playback/telemetry.py`、`engine.py`；`timeline_preview_renderer.py`；`edit_gpu_preview_renderer.py`；`src/shared/gpu/gl_pass_timer.py`；`program_viewer.py`。先对照 `playback_frame_trace.py` 的现有 trace/annotate，避免创建第二套互不关联的事件模型。

**步骤**：

1. 写出 §2 的字段、状态、单位、线程归属和兼容策略；扩展现有 trace 回填与聚合接口，用明确字段/schema 校验，禁止任意字段静默进入报告。
2. 补 CPU 编排/排队、`consume_planar_upload_stats()`、GPU query 和回读事件；回读区分普通预览、已启用监看、显式快照、CPU 回退，不能把所有回读都认定为违规。
3. 为 ACK、被覆盖帧和可观测呈现事件分别接线；保证通知和计数不阻塞渲染，不通过每帧同步 GPU 等待来计时。
4. 先检查 `tools/benchmarks/bench_realtime_playback_engine.py` 等现有入口；扩展或新增 `scripts/bench_frame_budget.py`。新增脚本的参数和退出码实现后再写可执行命令，支持离屏与窗口两模式。

**验收**：正常帧、同帧多请求、seek 切代、丢弃/超时、迟到 query、槽位复用、异常/关闭均能关联或明确拒收；有效零值与 unavailable 可区分。聚合字段有样本覆盖率，插桩开/关开销同机比较；受影响 focused test 通过。
**产物**：schema/兼容说明、harness、首批原始 JSON；B 可复用报告契约。

### A0.2 Mac 基线冻结

**依赖**：A0.1；环境实录可提前做。
按 §2 跑代表场景，先验证小矩阵再扩全素材；记录实际 VT/FFmpeg 路由、硬件 session、回退原因。把现机 Python/PyAV 实际版本与 `constraints.txt`/CI 区分，不自动升级或降级。
**验收**：离屏和有窗口结果分组，CPU/显示/解码瓶颈可区分；主要指标、噪声带、回归限值经审查登记，旧锚点保留。

### A0.3 拷贝台账与下一任务选择

**可并行**：只读源码/素材索引不等 A0.1。沿 source→frame→upload→composite→viewer/monitor 核实实际调用。
台账逐项记录读写字节、内存驻留、view/分配/传输、执行频次和触发条件；`flipud`/切片不自动算复制，不能把测试 mock 当运行证据。
**产物**：`docs/perf_frame_budget_report_mac_2026-10.md`，含证据排名、可证伪假设及一个优先 PoC。没有热点证据的任务保留候选状态。

## Phase 1 · 有证据后实施的局部优化

| 任务 | 进入条件、步骤与验收 |
|---|---|
| **A1.1 GC 观测与受控试验** | 先用低开销 GC 回调关联停顿和掉帧；仅关联成立时试验播放会话级策略。禁止每帧 `gc.collect()`/freeze/unfreeze，也不默认启动全局 freeze。“只禁 gen2”须先给出适用于目标 Python 的准确策略。若改变进程级 GC 状态，明确唯一所有者、原状态恢复、重入和内存上限；停止/异常/关闭均恢复。以延迟、掉帧与内存验收，不以 GC 次数归零验收。 |
| **A1.2 监看 CPU 路径优化** | 监看开时复制/转换确为热点才做；先查 PBO→NumPy→QImage→后端的 buffer 生命周期，保留异步背压。可试 UYVY422 打包后一次回读，但必须覆盖色域/range、位深、行跨距、奇数宽策略和设备格式。原生 GPU 直送另见 Phase 5，不能绕过 capability。验收实际字节/CPU 与监看帧内容/时序，无坏帧且关闭无泄漏。 |
| **A1.3 编排分配与遥测通知** | profile 证实 CPU 编排热点后，每次改一个点：惰性 trace、消费者允许的 UI 通知合批、可安全复用的临时对象。原始逐帧事件不得丢；错误/状态事件不合批延迟。不复用跨线程仍被引用的可变帧对象。按 A0 冻结的主要指标验收，不预承诺下降 30%。 |
| **A1.4 池与音画同步资产决策** | 核实 GpuFramePool 的生产消费者、已有 uploader 池是否重叠，以及 AVSyncController 与现有音频时钟/hold/drop 决策的边界；可选择保留、整合或移除，不能因“未用”就自动接入。涉及同步另做 drift、无音轨、seek、速率切换、rebuffer 测试，不按纯性能改动合并。 |

## Phase 2 · 路由与上传优化候选

| 任务 | 前提、实施范围与门禁 |
|---|---|
| **A2.1 VT 路由与 planar 回退** | A0 报告中该路径占比/成本显著才做。区分 VT 软解、硬件 session、IOSurface 导入失败，按 codec/profile/位深/色度生成路由矩阵；只对支持且收益成立的项转零拷贝。CPU 必需路径保留。10bit MSB/LSB 对齐变更须同步 source metadata 和 shader，按原始平面及 RGB 结果验证，避免双重移位。 |
| **A2.2 R16UI 与 float 回退** | `_auto_enable_r16ui()` 已按 uploader 实例只测一次；先核实是否反复建实例再谈缓存。保留自检失败时 float16/32 回退，禁止只发提示而不正确渲染。仅在归一化成本显著时优化 staging/分配；分别验收 fast/fallback 的图像与 HDR 正确性、耗时和真实转换字节。 |
| **A2.3 stride 上传 PoC** | 平面 tight copy 确为热点后，先小范围验证 uploader 的行跨距支持。检查 UNPACK_ALIGNMENT/ROW_LENGTH、像素与字节单位、状态恢复、非连续/负 stride、奇数尺寸和 AVFrame 引用寿命；不支持就继续 tight copy。渲染线程内实施，比较 decode+upload 耗时、字节和输出，不承诺统一复制次数。 |
| **A2.4 Topos GPU 解码 ADR** | 仅 Topos 素材的解码确实限制播放时立项；复核 `src/shared/gpu/metal/kernels_dec.py` 与 CPU/native 解码的真实覆盖，明确熵解码、重建、色彩/alpha、驻留与调度边界。与 B2.4 合并接口；ADR 不等于实现完成。 |

## Phase 3 · 条件立项：native 调度器

**A3.1 的前置门禁**：A1/A2 后 profile 仍证明 scheduler/ring/锁是主要瓶颈；测出 Python 回调/GIL/ctypes 成本，并给出保留 Python 的替代方案比较。条件不成立则终止该分支。

先从 `TimelineDecodeScheduler`/`FrameRingBuffer` 选一个有界操作 PoC，不直接承诺整套下沉或预定 C API 数量。保持 configure/attach/request/seek/cancel/generation/ownership 协议和 Python 回退；native 编译失败、库缺失须明确诊断。

**A3.1 验收**：同一请求/取消事件序列下比较源帧身份、generation、PTS/时间基、内容、顺序和释放次数；不能比较两个实现的 Python 对象 id。覆盖饱和队列、重排、EOF、失败、seek 风暴和关闭。采用至少 10 万事件/帧的可重放序列，再做真素材与同机收益验证；功能开关默认关闭。

**A3.2 扩展门禁**：只有 A3.1 跨 Python/native 边界后仍有收益才考虑 worker、budget 或更多池逻辑；逐项重测，不能把“Python CPU 占比 <15%”作为跨机器固定要求。与 C3.1 长跑交接，Windows 构建/ABI 也须验证后才列跨平台可用。

## Phase 4 · 条件立项：Metal 实时预览与显示租约

**A4.1 前提**：A0 数据显示 GL 路径或既有能力需求值得新增后端；先 ADR 明确 Color 视口与 Edit ProgramViewer 的不同生产入口，不把离屏 Color 效果通过等同 Edit 播放接通。

- 复用 `msl_composition.py`、`src/shared/gpu/metal/context.py`、`cv_metal_bridge.py`、`iosurface_gl_bridge.py`，先打通一个效果、一个 IOSurface 目标和一条生产入口，再扩覆盖。
- 明确 Metal command completion→共享 surface 可消费→GL 渲染消费→租约归还的跨 API 同步；Metal completion 不直接当成 GLsync。不要假定“两个锁独立”就足以保证资源线程安全。
- 效果以 Catalog/Registry、参数 schema 和版本为准。GLSL 权威语义经 MSL 翻译和 uniform ABI 对拍验证；SDR/HDR、噪声、邻域效果分别沿用对应测试的门槛，不把单个相对误差阈值套到全部效果。
- 复用既有开关/选择器，保持 opt-in 和原值兼容；不把视频开发开关直接改成静态图路由。自动启用前须过窗口播放、preview/export 能力、格式、失败/取消/关闭门禁；不支持效果显式拒绝或完整回退，不静默丢效果。

**A4.2 显示拷贝与租约 PoC**：仅显示 GPU 拷贝确为热点时试验直接租用输出槽。消费结束由完成同步确定，不在“已发命令”时过早归还；覆盖重绘、最小化、覆盖帧、尺寸切换和监看消费者。不满足生命周期或无收益时保留既有显示拷贝。

## Phase 5 · 持续回归与 DeckLink 原生能力预研

- 普通预览的意外 CPU 回读以运行时字节/调用计数和场景门禁守护；监看、显式快照、合法 CPU 回退单列。静态搜索只辅助定位，不能证明零拷贝。
- 显存/内存联合压力覆盖源帧、解码窗口、纹理池、合成缓存与输出槽；按稳定阶段趋势及停播后回落验收。启动、工程打开、缩略图分别建立场景，不与播放指标混用。
- A/B 共用 DeckLink 可行性 ADR：先验证 SDK、平台、适配器和格式约束，再实现 `src/native/decklink_bridge/topos_decklink.cpp` 能力。未实现/不支持/提交失败必须保留明确诊断和可用 CPU 路径；真机未验证不标 GPU 直送完成。

## 3. 源码与验证索引

| 主题 | 已有资产（定位用符号，不依赖旧行号） |
|---|---|
| 遥测/调度 | `src/features/edit/core/realtime_playback/telemetry.py` 的 `TelemetryAggregator.enrich_last`；同目录 `engine.py`、`timeline_decode_scheduler.py`、`gpu_frame_pool.py` |
| trace/计时 | `src/shared/video/playback_frame_trace.py`；`src/shared/gpu/gl_pass_timer.py`；`src/features/edit/core/edit_gpu_preview_renderer.py` 的 `gpu_pass_timer_stats` |
| 呈现/资源 | `src/features/edit/ui/program_viewer.py` 的 `set_present_frame`、`_ack_pending_present_without_display`；`src/shared/gpu/present_frame.py` |
| 解码/上传 | `src/features/edit/core/edit_gpu_decode_manager.py` 的 `_playback_vt_policy`；`src/shared/media/macos_vt/source.py`；`src/shared/video/yuv_gpu_upload.py` 的 `_auto_enable_r16ui` |
| focused tests | `tests/edit/test_realtime_playback_telemetry.py`、`test_gpu_present_frame.py`、`test_present_frame_integration.py`、`test_timeline_decode_scheduler.py`、`test_gpu_frame_pool_key.py`；`tests/shared/test_monitor_output.py`；`tests/color/test_metal_composition_*.py` |

纯逻辑、GPU、真素材/设备测试分组运行；GPU/设备不可用时报告未验证，skip 不算真机通过。根据实际变更选择上述已有测试，再补必要的行为/失败路径测试；不默认对每个小任务跑全库。

## 4. 交接、委派与立即执行清单

每个工作包交付：假设与进入条件、负责文件、base/head、测试/未验证项、成对原始数据、主要指标和退化项、回退方式。共享接口先定契约再分别实施，按小变更合并，不把长期分支合并积压到每个大 Phase 结束。

ZCode 可做只读调用链/素材索引、拷贝候选分类、测试与日志整理，以及契约明确的局部实现。委派需带绝对项目路径、PyQt6+ModernGL+GLSL 背景、`.clinerules`、允许修改文件和验收物；先验证 CLI/模型配置与小任务可回收，再批量派发。输出需独立复核，不用额度余额决定任务规模。

立即可做：环境实录与 A0.3 只读核对；随后完成 A0.1 契约/harness，再冻结 A0.2。**A1–A4 当前均为待证据/待门禁任务，不因出现在表内自动批准实施。**

## 修订记录

- 2026-10-22 第三轮（提交 e9d23a83a）：**H2 证实并交付 A1.2 第一刀**
  （监看链 85ms/帧@4K；PV QImage 转换 Qt 快路径 −43%，逐字节对拍锁定；
  后续刀与 DeckLink 真机验证记录在冻结文档 §11.1）；**A1.3 归因完成**
  （2K 对照 decode 0.2ms 证明编排极轻；4K 38ms = 同步解码+预取锁争用，
  靶点转 A1.4 语义）；A2.4 尝试记录（harness 未接 topos_source，数据未采）；
  Phase 5 soak 缩样通过（3.4min 无漂移无错误）。
- 2026-10-22 第二轮（提交 00c077b98）：真素材矩阵（4K H264/H265/
  ProRes422HQ/DNxHR HQX）+ 零拷贝 import 证据（0.64ms；生产 App 命中
  零拷贝、全链 pixel_copy 全 0）+ 真实 App 驱动场景 + A1.1 GC 观测器
  （观测完成、试验未触发）+ Phase 5 回读门禁 + `_GLWrapper` 崩溃修复。
  门禁关闭登记见冻结文档 §10：A1.4 决策保留现状；A2.1 证据完成无需
  变更；A2.2/A2.3 按停止条件关闭；A1.2（监看 A/B）为下一顺位候选；
  A1.3 获归因信号（in-app decode 38.2ms vs 纯解码 5.3ms）；A2.4/A3/A4
  维持未立项。
- 2026-10-22 v3 执行状态（perf/a-playback 分支，Phase 0 完成）：
  - A0.1 ✅（30737f826 契约 / dd1efbf61 接线 / dc9114e99+55beb13fa harness）：
    schema v3（段有效性/呈现状态/回读分类词表）、GPU query 发起帧回填、
    覆盖率与回填失败计数、`scripts/bench_frame_budget.py`（离屏+窗口）。
    顺带修复 ModernGL Query 无 release() 致 GPU 计时永久误判 unavailable。
    契约文档 `docs/perf_frame_budget_schema_2026-10.md`。
  - A0.2 ✅（42d87941c）：干净提交 55beb13fa 上冻结离屏/窗口基线
    （3+3 组重复噪声带、主指标/允许退化/停止条件登记、H1–H5 判定），
    `docs/perf_baseline_freeze_mac_2026-10.md`。插桩开销 < 噪声。
  - A0.3 ✅（4f03ab33e）：`docs/perf_frame_budget_report_mac_2026-10.md`。
  - A1–A4 维持候选/未立项（证据结论见冻结文档 §7：H2 监看未测、
    H3 部分证伪、H4/H5 证实；真实 App 时间线/监看场景为下一轮输入）。
- 2026-10-22 v3：按源码审查修正 GC、ACK、GPU 延迟回填、DeckLink 占位和 R16UI 回退；补同机比较、共享接口、失败路径与条件立项，移除无依据的固定周数和 Token 配额。
- 2026-10-22 v2：原函数级测绘版；v1：自总纲拆出的初版。
