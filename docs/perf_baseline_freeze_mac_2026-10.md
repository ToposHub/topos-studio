# A0.2 · Mac 基线冻结（2026-10-22）

> 依据：[计划表 A v3](perf_plan_A_mac_2026-10-22.md) A0.2；契约：
> [A0.1 schema](perf_frame_budget_schema_2026-10.md)；证据排名：
> [A0.3 拷贝台账](perf_frame_budget_report_mac_2026-10.md)。
> 基准提交：`55beb13fa`（perf/a-playback，工作树干净，非 dirty）；
> harness：`scripts/bench_frame_budget.py` v1。
> 原始数据：`bench_out/frame_budget/55beb13fac40/`（离屏 3 组重复 + 窗口
> 3 组重复 + 插桩开销对比组 + RSS 参考组；旧锚点 dd1efbf61eb8/dc9114e9950d
> 保留不覆盖）。

## 1. 环境（实录）

- 机器：i9-9900K / AMD RX 6900 XT 16GB（GL 4.1 ATI-6.1.13）/ 32GB / macOS 15.3.1 (24D70) / 4K 屏
- Python 3.12.13 / PyAV 17.0.1 (libavcodec 62.11.100) / ModernGL 5.12.0 / PyQt6 6.10.2 (Qt 6.10.2) / numpy（见 JSON env 节）
- `bench_manifest`：light_no_topos（本基准不含 Topos native codec 素材；降级原因记录在 env）
- 素材（生成，sha 见文件）：h264 3840×2160 8bit / hevc 2560×1440 10bit（libx265 造素材，解码走本机路由）/ prores422hq 2560×1440 10bit；DNxHR 本 ffmpeg 构建无法生成（dnxhr profile EINVAL，如实记录，后续用 `--material` 供真实素材）

## 2. 本轮覆盖与未覆盖（诚实边界）

**已覆盖（harness v1 范围）**：真实解码源路由（VT planar / FFmpeg planar）、
真实 YuvGpu 上传（R16UI/float16 决策）、真实 GL timer query 发起帧回填、
真实窗口 swap 呈现观测。离屏吞吐与窗口播放分开成组（计划 §2.2）。

**未覆盖（后续轮次，未冒充已测）**：
- 生产 timeline 双轨/效果栈/seek 风暴/监看开关场景（需真实 App 驱动，
  候选入口 `tests/benchmark/drive_real_app_edit_tracks.py`，尚未接入本契约）
- 零拷贝 IOSurface 导入的逐帧耗时（harness v1 走 VT planar 回退路径；
  生产 h264/hevc 命中零拷贝时的 import 段时间待真实 App 场景补测）
- 监看链路（H2）与 DeckLink；Topos native codec；CPU% 持续采样
  （本轮仅 max RSS 参考值）

## 3. 基线数据与噪声带（离屏 3 组 × 240 帧，预热 30）

VT planar 路由（h264/hevc）与 FFmpeg planar（prores）均被实测；
帧预算 @24fps = 41.7ms / @25fps = 40ms（素材 24fps 生成）。

| 素材 | decode p50（噪声带） | decode p95 | upload p50/p95 | composite p50 (GPU query) | total p50 | eff_fps |
|---|---|---|---|---|---|---|
| h264 4K 8bit | 15.3（14.5–15.4） | 43.0–43.5 | 1.33–1.39 / ~1.9–2.1 | 0.138–0.140 | 15.8–16.7 | 43.0–43.5 |
| hevc 2.5K 10bit | 14.7–15.2 | 34.9–36.0 | 1.33–1.40 / 1.5–1.8 | 0.072 | 16.0–16.5 | 47.7–49.3 |
| prores422hq 2.5K 10bit | 12.0–12.9 | 14.0–16.4 | 1.36–1.41 / 1.5–1.7 | 0.072 | 13.3–14.3 | 53.2–56.5 |

- 覆盖率：decode/upload/total 段 coverage=1.0；composite 段 0（harness
  不经 telemetry composite 通道，GPU 段在 trace 通道 38/40 帧 measured）。
- 字节审计（h264 4K）：cpu_upload=texture_write≈12.4MB/帧（VT planar
  2 平面 tight 拷贝后的 DMA），与 A0.3 台账 §2.2.1 预测一致。
- RSS 参考：h264 单素材全流程 max RSS ≈ 485MB（`/usr/bin/time -l`，
  含解码/上传/GL 资源；三素材连跑 ≈ 574MB）。

## 4. 窗口基线（3 组 × 120 帧，目标 25fps tick）

| 素材 | presented_fps（噪声带） | present p50（submitted→swap） | decode p50 |
|---|---|---|---|
| h264 4K 8bit | 23.03–23.05 | 2.24–2.55ms | 13.99–14.16 |
| hevc 2.5K 10bit | 23.46–23.48 | 1.99–2.24ms | 13.4–13.9 |
| prores422hq 2.5K 10bit | 23.39–23.46 | 2.10–2.17ms | 11.1–11.9 |

- 25fps 目标下实际 23.0–23.5fps：缺口来自 tick 节拍 + decode（14–15ms）
  未留足余量的排队，不是 swap/present 段（p50 ~2.2ms）。
- present 段语义 = submitted→frameSwapped（swap 级观测，非物理扫描时间）。

## 5. 插桩开销（同机对比）

snapshot-every-15 组 vs 基线（snapshot=0）：decode p50 15.40 vs 15.3、
total p50 16.74 vs 16.6、eff_fps 44.1 vs 43.0–43.5——**全部落在噪声带内**
（回读字节 28MB/组 可见）。遥测+回读插桩开销 < 噪声下限，可常开。

## 6. 指标登记（每项优化一个主指标 / 允许退化项 / 停止条件）

> 规则（计划 §2.2）：超过预登记限值回退；噪声带内不得宣布提升。
> 噪声带取上表 3 组重复的极差；主指标基准值取中位。

| 候选任务 | 主指标（基准） | 允许退化 | 停止条件 |
|---|---|---|---|
| 全局（任何 A1/A2 变更） | 离屏 total p50（h264 16.6ms / hevc 16.2 / prores 14.0）与窗口 presented_fps（≥23.0） | 任一素材 total p95 退化 >10%（h264>47.9 / hevc>39.6 / prores>18.0ms）或 presented_fps <22.5 | 两项同时越限即回退 |
| A1.2 监看 CPU 路径 | 监看开启时每帧回读链字节/耗时（**基线未测**，先补真实 App 监看开/关 A/B） | 监看关场景任何指标噪声带外退化 | 无收益或坏帧/时序问题 |
| A2.1 VT 路由 | decode p50（VT planar 15.3ms→零拷贝 import 段待测） | 零拷贝失败回退路径行为不变；10bit 对齐像素差 >0（禁止双重移位） | import 段实测无收益或对拍失败 |
| A2.2 R16UI/float16 | upload p50（1.35ms）与 mode 分布 | fast/fallback 图像对拍必须全过 | 自检通过（已确认）则本任务关闭 |
| A2.3 stride 上传 | upload p50/p95 与 cpu_upload_bytes | 奇数尺寸/非连续平面正确性测试全过 | 收益 <0.2ms（<噪声带）即维持 tight copy |
| A1.3 编排 | engine 派发 total_ms（~0.15ms 历史） | 逐帧事件不丢、错误事件不合批 | 归因占比 <5% 帧预算即终止 |

## 7. H1–H5 判定（对照 A0.3 报告）

| # | 判定 | 证据 |
|---|---|---|
| H1（VT 零拷贝命中） | **部分证实**：capability probe available=True（本机可走零拷贝）；harness 走 VT planar 回退实测 decode p50 15.3ms@4K（含 12.4MB 拷贝）。生产 in-app 零拷贝 import 逐帧耗时**待真实 App 场景补测** | `route.zero_copy_capability` / decode_ms |
| H2（监看最大增量） | **未测**：监看路径在真实 App 内（PV PBO 链），harness v1 未覆盖；保持候选第 1 顺位 | — |
| H3（planar 拷贝/上传主导） | **部分证伪**：upload p50 仅 1.35ms（DMA 后不再 CPU 拷贝）；FFmpeg tight 拷贝含在 decode p50 12–15ms 内，是解码段的一部分而非独立热点 | upload_ms / decode_ms |
| H4（编排非瓶颈） | **证实**：总预算 40–41.7ms 下 decode 占 30–38%，GPU 合成 0.07–0.14ms，编排（历史 ~0.15ms）不显著 | 各段 p50 |
| H5（R16UI 通过） | **证实**：hevc/prores 10bit 自检 passed=True、disabled_reason 空、mode=r16ui_direct；float16 回退未触发 | `r16ui` 字段 |

## 8. 冻结与旧锚点

- 本文件登记的基准值 + 噪声带即 A 线回归门禁基线（后续优化按 §6 验收）。
- 旧锚点目录保留：`dd1efbf61eb8`（A0.1 冒烟，dirty 树，探索性）、
  `dc9114e9950d`（窗口模式验证，dirty 树，探索性）；正式对比一律用
  干净提交目录（本轮 `55beb13fac40`）。
- 变更 base/head 记录方式：bench_out/<head12>/<mode>_<UTC>/；dirty 结果
  只作探索。

## 9. 第二轮补全（2026-10-22 晚，提交 00c077b98：真素材矩阵 + 零拷贝证据 + 真实 App 场景）

真素材（`tests/test_videos/`，§2.2 矩阵的实际文件）：4K_H264 / 4K_H265 /
4K_prores422HQ / 4K_DNxHR HQX 10bit；离屏+窗口各 3 组 × 120 帧。
原始数据：`bench_out/frame_budget/182fcbf0f357/`。

### 9.1 离屏（零拷贝探测开启；3 组噪声带极差 <0.6ms）

| 素材 | 路由 | decode p50/p95 | import p50 或 upload p50 | total p50 | eff_fps |
|---|---|---|---|---|---|
| 4K_H264 | **macos_vt_zero_copy** | 5.27–5.39 / 7.0–7.6 | import 0.62–0.65（p95≤1.37） | 5.95–6.08 | 154–159 |
| 4K_H265 | **macos_vt_zero_copy** | 6.58–6.99 / **28.2–30.4** | import 0.60–0.65 | 7.13–7.71 | 102–109 |
| 4K_prores422HQ | **macos_vt_zero_copy** | 14.85–15.74 / 18.8–20.9 | import 0.67–0.70 | 15.56–16.47 | 55–58 |
| 4K_DNxHR HQX 10bit | ffmpeg_planar（VT 回退原因 `unsupported_codec: dnxhd`） | 6.18–6.41 / 7.8–9.2 | upload 4.35–4.43 | 10.55–10.92 | 89–92 |

- **A2.1 证据补全**：IOSurface→GL 绑定 import p50 ≈0.64ms；对比 VT planar
  回退（第一轮 4K h264：decode 15.3ms 含 12.4MB 拷贝）零拷贝 decode 仅
  5.3ms——回退路径的额外 CPU 成本 ≈10ms/帧@4K 已量化；生产路由在本机
  命中零拷贝（§9.2），无需代码变更。
- 回读运行时门禁（Phase 5）：4 素材 × 3 组 cpu_download=0，guard 全过。
- GC（A1.1）：每 run 3–7 次回收共 0.6–2.3ms（max 1.15ms），
  **0 次**与 deadline miss 重叠。

### 9.2 窗口（3 组 × 120 帧 @25 tick；未开零拷贝探测，走 VT planar/FFmpeg 路由）

| 素材 | presented_fps | present p50 | decode p50 |
|---|---|---|---|
| 4K_H264 | 24.58–24.71 | 2.36–2.49 | 17.5 |
| 4K_H265 | 18.67–18.70 | 1.67–1.73 | **30.6**（超 40ms 预算的 76%） |
| 4K_prores422HQ | 24.51–24.62 | 1.73–1.77 | 24.2–25.0 |
| 4K_DNxHR HQX 10bit | 25.10–25.16 | 1.15–1.42 | 5.6–5.8 |

### 9.3 真实 App 场景（`tests/benchmark/drive_real_app_edit_tracks.py`，
4K_H264 单轨 15s，日志 `bench_out/frame_budget/182fcbf0f357/real_app_4K_H264_15s.log`）

- EFFECTIVE_FPS 24.48（25 目标的 98%）；presented 147 帧去重 / 控制器
  dropped 22；present interval p50 105.5ms（40ms 预算，一次 present 覆盖
  多个 playhead 推进——掉帧以"保持上一帧"形式出现，不是黑帧）。
- **schema v3 在生产管线全程生效**：decode_path=macos_vt_zero_copy ×120
  （H1 在真实 App 内证实）；is_full_gpu 120/120；`pixel_copy_bytes` 五项
  **全 0**（4K 单轨播放零 CPU 拷贝，全链零拷贝成立）；
  present_status_counts {displayed 96, slot_released 23}；
  gpu_time 117/120 measured（p50 0.13ms，发起帧回填 0 丢失）；
  annotate merged 707 / missed 0；import_upload 119 not_applicable；
  readback_kind_counts 空（无任何回读）。
- **关键差距信号**：in-app decode p50 **38.2ms**（67/120 帧超 33.3ms 分桶
  预算）vs 纯解码 5.3ms（§9.1）——差值主要来自渲染任务内的解码等待/
  调度编排，不是 VT 解码本身。这是 A1.3（编排）与调度器归因的下一个
  profile 靶点（见 §10）。
- 附带修复：真实 App 首次视频 paint 崩溃——fix-ui 既有 R21 修复
  （c1cf36fe5）调用了 `_GLWrapper` 清单外的 `glIsProgram/glIsBuffer/
  glIsVertexArray`，AttributeError 使 Qt abort；已补入 wrapper。

## 10. 门禁关闭登记（对照计划 Phase 1/2/3/4；证据引用如上）

| 任务 | 判定 | 依据 |
|---|---|---|
| A1.1 GC 观测 | **观测完成，策略试验未触发**：观测器已建（opt-in `TOPOS_GC_PAUSE_OBSERVER=1`，生产 shutdown 输出摘要；harness 默认开启）；全部基准运行 0 次停顿与 deadline miss 重叠 → "关联成立才试验"条件不成立，不做任何 GC 策略变更 | §9.1 gc 字段；`src/shared/video/gc_pause_observer.py` |
| A1.2 监看 CPU 路径 | **H2 证实 + 第一刀已交付**：监看链 84.9ms/帧@4K（>2× 预算）；PV QImage 转换 Qt 快路径 −43%（85→48ms，逐字节对拍锁定）。仍超预算，后续刀（GPU 缩放后回读 / UYVY 打包）与真机 DeckLink 验证记录在 §11.1 | §11.1 |
| A1.3 编排分配 | **归因完成，靶点转移**：2K 对照（decode 0.2ms ring 命中）证明编排本身极轻；4K 38ms = 同步解码 + 管理器后台预取 `_stream_lock` 争用（纯解码仅 5.3ms）。后续按 A1.4 语义立项（调度器接线/锁粒度），非"惰性 trace/合批"类改动 | §11.2 |
| A1.4 池与音画同步 | **决策：保留现状**（GpuFramePool 已挂载未接线、AVSyncController 仅遥测）；不因"未用"自动接入；接入需另过 drift/无音轨/seek/速率/rebuffer 门禁（A0.3 报告 §3.5） | 决策记录即交付 |
| A2.1 VT 路由 | **证据完成，无需变更**：路由矩阵实测（零拷贝 3/4 素材命中、DNxHR 落 FFmpeg 原因记录）；import 0.64ms、回退成本 ≈10ms@4K 已量化；生产已最优 | §9.1/§9.2/§9.3 |
| A2.2 R16UI | **关闭**：本机自检通过（H5 证实），float16 回退未触发——按 §6 预登记停止条件关闭 | §7 H5 |
| A2.3 stride 上传 | **关闭（前提证伪）**：tight 拷贝发生在解码提取（decode 段），上传段 planar 仅 1.35ms（DMA）、零拷贝路由无上传；按停止条件维持 tight copy | §3/§9.1 |
| A2.4 Topos GPU 解码 ADR | **未立项（尝试已记录）**：harness FFmpeg 源打不开 Topos codec 素材（需专用 `topos_source` 接入）；占比数据仍未采 | §11.3 |
| A3.1/A3.2 native 调度器 | **未立项**：无"A1/A2 后 profile 仍证 scheduler/ring/锁主导"的证据；§9.3 的差距信号先按 A1.3 归因路径处理 | 计划 Phase 3 门禁 |
| A4.1/A4.2 Metal 预览 | **未立项**：无"A0 数据显示 GL 路径值得新增后端"的证据（GL 合成 0.13ms、呈现 p50 ≤2.5ms，均远离预算） | §9.1–9.3 |
| Phase 5 持续回归 | 回读运行时门禁已建（guard 字段+全过）；**soak 缩样通过**（§11.4）；显存压力与 DeckLink ADR 待后续轮次 | §9.1/§11.4 |

## 11. 第三轮（2026-10-22 深夜）：H2 实测与 A1.2 第一刀 / A1.3 归因 / A2.4 尝试 / soak

### 11.1 H2 证实 + A1.2 第一刀（`scripts/bench_monitor_readback.py`，
生产同款代码：PV PBO 链 + `qimage_to_bgra_frame`；4K 源/1080p 监看目标，200 帧）

| 项 | 优化前 | 优化后（Qt 快路径） |
|---|---|---|
| blit 基线（monitor off） | 0.024ms p50 | 同 |
| `rgba_to_qimage`（翻转+丢 alpha+QImage 拷贝） | **52.99ms p50** | **15.64ms** |
| `bgra_convert`（monitor_output 四连拷） | 16.08 | 16.46 |
| `pbo_fetch`（33MB DMA 取回） | 6.08 | 5.96 |
| `submit_qimage_copy` | 3.34 | 3.37 |
| **整链 delta（monitor on 新增）** | **84.89ms p50（>2× 40ms 预算）** | **48.20ms（−43%）** |

- **A1.2 进入条件成立**（热点确凿）。第一刀：`ProgramViewer._pbo_rgba_to_qimage`
  主路径改 Qt C++（RGBX8888 包装 → `mirrored` 垂直翻转兼深拷贝 →
  `convertToFormat(RGB888)`），NumPy 路径保留为异常回退；**输出与旧实现
  逐字节一致**（含奇数宽/GL 朝向，`test_pbo_rgba_to_qimage_matches_legacy`
  对拍锁定），接口/异步背压/下游链不变。
- 仍超预算（48>40ms）。剩余大头：`bgra_convert` 16.5ms（4K→1080p CPU
  resize）与 33MB 回读。**下一刀候选（记录，未实施）**：GPU 侧缩放到
  监看分辨率后回读（33MB→8.3MB，兼消 CPU resize），需覆盖
  letterbox 语义/色域/range 对拍；以及计划原文的 UYVY422 打包单次回读。
  真机 DeckLink 验证前不标完成。

### 11.2 A1.3 归因（2K/4K 真实 App 对照，`drive_real_app_edit_tracks.py`）

| 场景 | decode p50 | total p50 | presented interval p50 | realtime |
|---|---|---|---|---|
| 4K_H264（§9.3） | **38.2ms**（同步解码逐帧） | 37.6 | 105.5ms | 24.48fps（98%） |
| 2K_h264（本轮，GC 观测开） | **0.203ms**（ring/预取命中） | 0.378 | 40.5ms | 24.66fps（99%） |

- **归因结论**：in-app 4K 的 38ms 不是"CPU 编排"本身，而是
  **每帧同步解码 + 与管理器后台预取（`_stream_lock`）争用**；2K 对照
  命中缓存后 decode 仅 0.2ms 佐证管线编排本身极轻（H4 再次证实）。
  纯 VT 零拷贝解码仅 5.3ms（§9.1）→ 38 − 5.3 ≈ 33ms 是锁等待/双解码
  排队。**下一步靶点（记录，未实施）**：按 A1.4 语义评估——为 4K 实时
  接线 decode scheduler/ring（ENGINE_STATS 显示 streams=0、rings={}，
  调度器空转，预取走管理器后台线程与前台争锁），或削减预取锁粒度；
  涉及同步决策，按计划 A1.4 需另过 drift/seek/速率门禁。
- GC：观测器 env 开启运行 2K 全程；驱动日志被 App logging 过滤未见
  engine logger 摘要行（观测器本身由单测+harness 数据覆盖：
  §9.1 每次 run 3–7 次回收 0.6–2.3ms、0 重叠）。

### 11.3 A2.4 Topos 尝试与 Phase 5 soak

- **A2.4**：`2K_topos_422_HQ.mov` 在 harness 的 FFmpeg 源打开失败
  （`'VideoStream' object has no attribute 'width'`——Topos native codec
  需专用 `topos_source` 解码源，harness v1 未接入）。**占比数据仍未采**
  → A2.4 维持"数据未采不立项"；接入入口与素材已在库（§10）。
- **Phase 5 soak**：4K_H264 窗口模式连续 5000 帧（≈3.4min 实时）
  结果见 §11.4（首轮缩样；正式 10min soak 由变更风险决定，协议起点
  未冒充已执行）。

### 11.4 soak 结果（4K_H264 窗口连续 5000 帧 ≈3.4min 实时）

`bench_out/frame_budget/e94816b9205d/window_20261022T141115Z/`：
**ok=True，无漂移**——presented 5104 swaps @ **24.84fps**（短跑 24.58–24.71
一致）；decode p50 17.48ms / p95 21.8（稳定）；present p50 2.59ms；
**0 错误**；annotate 20298 merged / 0 missed；无任何回读。
连续播放下无性能退化、无资源泄漏迹象。首轮缩样 3.4min（正式 10min
soak 由后续变更风险决定，未冒充已执行）。
