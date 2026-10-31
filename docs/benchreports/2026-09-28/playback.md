# G1 播放性能主题报告（计划05 P1，2026-09-28）

覆盖目录（7 个）：`bench_out/d1_playback`、`bench_out/readahead`、`bench_out/first_frame_repro`、`bench_out/real_2k_h264_20260907`、`bench_out/real_user_workflow_h264`、`bench_out/real_user_workflow_h264_full`、`bench_out/edge_jag`。
出处路径均为仓库根相对路径（仓库根 = `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`）。原始数据只读核对自 `bench_out/`，输入卡片为同目录 `cards-group2.md`、`cards-group4.md`。

数据纪律（依据 `bench_out/HISTORICAL.md` P2-08）：本组全部目录属历史观察；相对结论仅 `readahead/`（成对差分 + 区间）接近"同 clean HEAD + 同 dylib SHA + 交替 A/B 中位数 + 95% CI 不跨 1"的门槛但仍无 manifest 锚定。`round2/` 类已判失效数据（`bench_out/round2/round2_invalidated.md`，52a2dbf0 UAF）不在本报告覆盖与结论中。

---

## 0. 结论速览

1. **解码边界本身不是 25fps 播放的瓶颈**：D1 边界（解码→CPU planar 帧，不含 GPU 上传/调色/呈现）下，4 档素材全部超过 25fps 实时，最重的 4K 4444xq 也有 39–43 fps（p50 23.2–24.6 ms）；但 30fps 时间线下 4K 4444xq 的 p99 尾部（最高 36.6 ms）会击穿 33.3 ms 预算。
2. **读取策略（readahead）是本组证据最硬的性能收益**：产品路径成对差分，批量预取 2K +24.3%、4K +2.3%；叠加 arena 聚读后官方窗口 2K +10.5%、4K +11.7%，4K 从 86.6 提到 98.3 fps。
3. **首帧延迟在覆盖目录内没有任何计时数据**——`first_frame_repro` 是功能复现语料（11 个 mov + manifest，零计时字段），D1 基准 `random_seeks=0` 且无冷启动字段。这是本主题最大数据缺口。
4. 丢帧/卡顿只能从 D1 的 p95/p99 尾部间接刻画：除一次 4K proxy 单跑 p99 尖峰（25.7 ms，p50 的 5 倍）外，尾部比 p50 普遍只有 1.2–1.9 倍；端到端（含呈现）dropped/late 帧遥测在本组目录缺失。
5. 真实素材 2K H.264 转码后 TOPOS 全片解码 380.69 fps；绑定层 wrapper 路径比 native 直调慢约 2.3–2.5 倍（135.6 vs 335.6 fps）——绑定层开销是播放路径可见的量级差。

---

## 1. 数据来源与有效性分级

| 目录 | 性质 | 有效性 | 是否纳入性能结论 |
|---|---|---|---|
| `d1_playback` | 受控解码边界基准（bench_manifest.v1，2 个 clean 构建 96 帧×2 repeats + 1 个 dirty 16 帧） | 历史观察；无参照解码器 | 是（量级刻度） |
| `readahead` | 方法学证据文档（成对差分 + 比值中位数 + 区间） | 本组最接近 HISTORICAL 规则 2；无 manifest 锚定 | 是（相对收益） |
| `real_2k_h264_20260907` | 真实素材端到端转码 + 解码对照 | 无 commit/dylib SHA（库路径指向 topos-color02 工作树） | 是（方向性） |
| `edge_jag`（speed 子目录） | 解码 worker 扫描 + 全池按档位解码速度 | 24 vCPU 机、无 manifest；主体是画质取证 | 部分（量级参考） |
| `real_user_workflow_h264_full` | 全链路功能验证（含 1 个导出 wall 数字） | 单次运行、无 CI | 仅量级（导出口径非播放） |
| `first_frame_repro` | 首帧缺陷复现语料（11 mov + manifest） | 无计时、无基准 | **否（功能验证）** |
| `real_user_workflow_h264` | 工作流冒烟（导出 + QC） | manifest 无任何计时字段 | **否（功能验证）** |

明确标注：`first_frame_repro`、`real_user_workflow_h264` 是功能验证而非性能数据，不进入任何性能结论；`real_user_workflow_h264_full` 仅其导出 wall 一个数字可作量级参考。`round2` 类失效数据（见 `bench_out/round2/round2_invalidated.md`）不纳入。

---

## 2. D1 播放边界实测（`bench_out/d1_playback`，2026-09-15）

边界定义（各 result.json `scope`）：`ToposMediaSource read_next_frame/read_frame → CPU planar frame`，显式排除 GPU 上传、调色、合成、呈现、Qt 调度。构建链：1459cef4（dirty，16 帧，repeats=1）→ b61cabf4 → b5f4520a（均 clean，96 帧 × 2 repeats，threads=16）。

诊断字段（全部 10 个 result.json 一致）：`decode_path=context`、`decode_path_reason=ok`、`native_target_decode=false`、`bitstream_major=7`、`stream_compatibility=unsupported_major`、`decode_workers_assigned=16`；**`read_ahead_depth=0`（readahead 关闭状态测得）**、`random_seeks=0`（寻址未测）。

### 2.1 关键指标（clean 构建两轮范围；出处为各素材对应 result.json）

| 素材 | p50 (ms) | fps | p95 (ms) | p99 (ms) | RSS 峰值 (MiB) | 出处 |
|---|---|---|---|---|---|---|
| 2K proxy | 1.345–1.363 | 702–716 | 1.62–1.87 | 1.75–2.60 | 83.4–84.6 | `d1_playback/b5f4520a/fb0712a5/b98ca792/20260915T052659309166Z/result.json`、`d1_playback/b61cabf4/9a36f61b/a17fde9f/20260915T051544736733Z/result.json` |
| 2K hq | 3.530–3.716 | 248–277 | 4.97–5.65 | 5.65–6.68 | 144.9–146.0 | `d1_playback/b5f4520a/fb0712a5/802c3a7b/20260915T052713822871Z/result.json`、`d1_playback/b61cabf4/9a36f61b/23a984a1/20260915T051546347499Z/result.json` |
| 4K proxy | 4.887–5.252 | 171–204 | 5.41–7.12 | 6.32–25.74 | 130.6–131.3 | `d1_playback/b5f4520a/fb0712a5/db53552b/20260915T052722129114Z/result.json`、`d1_playback/b61cabf4/9a36f61b/17ee5b38/20260915T051548268191Z/result.json` |
| 4K 4444xq | 23.245–24.608 | 39–43 | 26.99–29.27 | 29.16–36.64 | 730.2–745.5 | `d1_playback/b5f4520a/fb0712a5/a0314c95/20260915T052735146059Z/result.json`、`d1_playback/b61cabf4/9a36f61b/8fedcb41/20260915T051555356217Z/result.json` |

（dirty 构建 1459cef4 的 16 帧单轮与 clean 值一致方向：2K proxy 1.363 ms/716 fps、4K 4444xq 23.685 ms/42 fps，出处 `d1_playback/1459cef4/fe8d284e/{7fbcc807,10964298}/20260915T0446*/result.json`，仅作旁证。）

### 2.2 帧预算判定（对照 `docs/4k-playback-optimization.md` §3.1：25fps→40ms、30fps→33.3ms）

| 素材 | 25fps（40ms 预算） | 30fps（33.3ms 预算） |
|---|---|---|
| 2K proxy / 2K hq / 4K proxy | p99 全部 < 8ms，余量 > 5 倍 | 全部达标 |
| 4K 4444xq | p50 23.2–24.6、p99 ≤ 36.6 < 40：达标（p99 余量仅 9–27%） | p50 达标但 p99 29.2–36.6 ms 有轮次击穿 33.3（`8fedcb41` 第二轮 p99=36.638） |

### 2.3 丢帧/卡顿特征（间接刻画）

- 尾部比 p99/p50：2K proxy 1.5–1.9 倍；2K hq 1.6 倍；4K 4444xq 1.2–1.5 倍——顺序解码尾部平稳，无明显周期性卡顿签名（各 result.json `sequential_runs[].p99_ms/p50_ms`）。
- 唯一显著尖峰：`d1_playback/b61cabf4/9a36f61b/17ee5b38/20260915T051548268191Z/result.json` 第一轮 4K proxy p99=25.736 ms（p50 5.172 的 ~5 倍），同文件第二轮即回落到 7.505 ms、fps 171→187——单次调度/页错误级抖动，非稳定形态。
- RSS：4K 4444xq 峰值 730–746 MiB，为其余素材（83–146 MiB）的 5–9 倍（12-bit 444 4K 平面体量所致）；无跨轮增长迹象（repeats 内两轮峰值持平）。
- 边界限制（重要）：D1 明确排除呈现侧，**没有 dropped_frames/late_frames/queue_depth 遥测**；`docs/4k-playback-optimization.md` §4.2 要求的 `playback_frame_path/decode_ms/plane_prepare_ms/...` 播放日志字段在覆盖目录内无对应落盘数据。

---

## 3. 读取策略（readahead）收益（`bench_out/readahead`，2026-09-02，i9-9900K 8C16T）

四份方法学证据文档，全部为产品路径 ToposMediaSource 口径（成对差分、逐对 fps 比值中位数 + 区间），是本组最接近 HISTORICAL 规则 2 的相对结论（仍无 manifest 锚定，历史观察）。

### 3.1 收益汇总（出处见右列）

| 优化项 | 2K | 4K | 出处 |
|---|---|---|---|
| 批量预取（off→on） | 296.0→368.4 fps，**+24.3% [+22.2, +28.4]，12/12 对正** | 84.6→85.8 fps，+2.3% [+0.6, +3.6]，9/12 正 | `readahead/20260902_stage4_product_readahead.txt` |
| arena 聚读（最终官方窗口，空机 12 对） | 324.0→362.4 fps，**+10.5% [+7.8, +15.8]，12/12 正** | 86.6→**98.3 fps**，**+11.7% [+7.4, +14.9]，10/12 正** | `readahead/20260902_stage4_arena_batch_read.txt` |
| IO 层隔离（arena vs bytes，全片无解码） | 13.6 vs 15.7 ms（1.15×） | 28.7 vs 63.7 ms（**2.22×**，省 1.46 ms/帧） | 同上 |
| parse 并行化候选 | **否决**：parse 0.78 µs/包（2K hq）/1.26 µs（4K hq），占帧解码 0.033%；Amdahl 反推串行占比 s≈0.5–1.2%，收益 <0.1% 低于 3% 门 | 同左 | `readahead/20260902_stage4_parse_parallel_negative.txt` |
| t16 vs t8（静机 12 对） | 比值中位 0.998（打平） | 1.020，12/12 全正（t16 方差更小，t8 两次深坑 97.6/104.7） | `readahead/20260902_stage4_t16_quiet_reverify.txt`（含原始逐对日志） |

机制与边界条件（`product_readahead.txt`）：深度按"预取平面总字节 ≤160 MiB"自适应钳 [2,8]（2K 422≈8、4K 422≈4）；seek/跳读自动失效缓存；平面池容量 depth+2 是必要修复（无余量时 hold=2 收益从 +24.9% 掉到 +10.6%）；4K 深度敏感性 depth2 −3.9%、depth4（自动值）最优、depth6 −2.0%（工作集 212MB 转负）；内存 2K 稳态 RSS 第 8 遍起平台化 ~195 MiB、12 遍无增长。

线程口径附注（`t16_quiet_reverify.txt`）：产品解码路径（播放/scrub）是 native 默认线程数的实际消费者，且当时发现文档/代码三处默认口径分叉（实际默认已是 16）；静机证据追认维持 16 不改。负载 15–36 时 t16 对 t8 中位 +14%（`parse_parallel_negative.txt` 附带发现）。

**注意**：D1 基准（09-15）全部跑在 `read_ahead_depth=0`，即第 2 节数字是 readahead 关闭态；两套数据协议不同（96 帧 repeats=2 单素材 vs 成对差分 hq-rate 码流），**不可数值复合**。

---

## 4. 4K vs 2K 表现

同档位解码耗时随分辨率的放大（4K/2K 像素比 4.3×）：

| 口径 | 2K | 4K | 4K/2K 耗时比 | 出处 |
|---|---|---|---|---|
| D1 proxy p50 | 1.345–1.363 ms（702–716 fps） | 4.887–5.252 ms（171–204 fps） | 3.6–3.9× | 见 §2.1 各 result.json |
| edge_jag 全池 hq | 184–192 fps | 60 fps | ~3.1–3.2× | `edge_jag/speed/speed_all.json`（2K hq same_size_444 192 / 4k_master_downscale 184；4K hq 60） |
| edge_jag 全池 4444xq | 117 fps | 35 fps | ~3.3× | 同上 |

- 实测：同档位 4K 解码耗时约为 2K 的 3.1–3.9 倍，略低于像素比 4.3×（次线性）。
- 推测（未验证）：次线性放大与帧内 slice 并行（t16 满配）摊薄固定开销有关；`readahead/20260902_stage4_parse_parallel_negative.txt` 的扩展曲线（t1 68.9 → t8 380.7 → t16 481.3 fps）与"残余差距是吞吐层损失（写带宽/slice 尾部失衡/调度）"的归因支持该推测，但该曲线为负载 15–36 机器上采集，绝对值偏低。
- 跨机器旁证：4K 4444xq 在 D1（i9-9900K，t16）39–43 fps 与 edge_jag（24 vCPU，满池）35 fps 一致落在"25fps 实时 1.4–1.7 倍"区间；两机/素材不同，仅方向一致。
- 2K 全档位均远超实时（最重 2K 4444xq 也有 117 fps，`edge_jag/speed/speed_all.json`）。

### edge_jag 播放相关部分（主体为画质取证，属画质主题）

解码 worker 扩展（`edge_jag/speed/speed_all.json`，487 帧@25fps，`max_slice_workers=0` 进程默认满池、标注"产品播放同口径"）：

| 素材 | w1 | w4 | w8 | w16 | w0（默认满池） |
|---|---|---|---|---|---|
| 2K proxy | 46 | 158 | 305 | 492 | 500–548 |
| 4K proxy | 12 | — | 82 | 151 | 158 |

单 worker 到 16 worker 提速 10.7×（2K）/12.6×（4K）；w16 已接近满池。全池按档位解码 fps：4K proxy 158 / 4K hq 60 / 4K 4444xq 35（feed=4k_master_downscale）。变体批（`edge_jag/variants/v0–vf_*_bench.json`）为并行候选，只可排序不可写倍数；门禁 `edge_jag/gate_after_id4.json` ok=true 无违规（功能验证）。

---

## 5. 首帧延迟：无基线（数据缺口）

- `bench_out/first_frame_repro`（2026-09-17）：11 个 .mov（354 KB–227 MB）+ 逐一配套导出 manifest，按管线阶段命名（repro_h264/repro_topos → topos_stage/prores_stage/topos_inner → topos_pt/pt_check）。**目录内没有任何计时、日志或基准结果**——它是首帧缺陷的最小复现语料（价值：素材 sha256 锚定 + timeline/preflight/delivery_qc 快照，如 `repro_topos.mov.manifest.json`：1920×1080/24fps/12 帧，QC passed，素材 2K_topos.mov）。缺陷现象与修复效果不可从本目录得出。
- 附带实测（功能口径）：`topos_stage.mov.manifest.json` 与 `prores_stage.mov.manifest.json` 各有 1 条 `media.fps_mismatch` 警告（24fps 素材进 25fps 时间线，播放/导出按时间线重采样）——语料本身的帧率混排值得复用时注意。
- D1 基准 `random_seeks=0` 且无冷启动字段（各 result.json `config`/`sequential_runs`），readahead 证据链只测稳态顺序吞吐——**覆盖目录内首帧/冷启动延迟零数据**。
- 跨目录指引（非本报告覆盖，属其他主题报告范围）：首帧相关的最近数据在 `bench_out/p0_runner`（`cold_first_frame`，n=3 样本极小）与 `bench_out/p2_09`（冷首帧 p50：h265 35.2 ms / prores 165.3 ms，会话池逐出重建），见 `cards-group4.md` 对应卡。

---

## 6. 真实素材 / 用户工作流（性能与功能分列）

### 6.1 real_2k_h264_20260907（真实 2K H.264，方向性性能数据）

出处全部为 `real_2k_h264_20260907/result.json`（1920×1080/25fps/1572 帧转 TOPOS proxy 后全片解码）：

| 指标 | 数值 |
|---|---|
| TOPOS 全片解码 | 4.129 s / **380.69 fps**（2.627 ms/帧） |
| 120 帧解码对照 | FFmpeg H.264 **1079.0** fps > topos native **335.6**（p50 2.68 ms）> VideoToolbox 304.0 > topos wrapper **135.6**（p50 6.12 ms） |
| 端到端转码 | 16.362 s / 96.08 fps；编码净 9.823 s / 160.04 fps |
| 体积 | 781,505,543 B，为 H.264 源 14.88 倍 |
| 往返质量（对 H.264 解码后 422 10-bit） | Y 54.201 / U 61.972 / V 62.325 dB（`quality.json`） |

播放相关结论：① 真实素材 2K 档解码余量充足（380 fps ≈ 15 倍实时）；② **绑定层 wrapper 路径比 native 直调慢 2.3–2.5 倍**（135.6 vs 335.6 fps，p50 6.12 vs 2.68 ms）——产品若走 wrapper 形态，绑定开销本身是播放路径的可优化项。备注：三方对照像素格式/硬件路径不同（卡片已注明，仅用于瓶颈定位）；`topos_library` 指向 `topos-color02` 工作树的 real-bench-release 构建，无 SHA 锚定，绝对值引用须注明构建。

> **复现与验收注记（BENCH-0003，2026-09-29 追注）**：wrapper/native 开销倍数已有官方成对 A/B 采集脚本 `tools/bench_wrapper_binding_overhead.py`——同素材、同线程、偶对 wrapper 先/奇对 native 先交替（默认 12 对），统计量 median(wrapper p50)/median(native p50) 的成对 bootstrap 95% CI，并附预注册判据：CI 下界 >2.0 → `overhead_open_2x`（2×+ 缺口仍在，即本节现状）；CI 上界 <1.2 → `binding_at_parity`（m11_stage2b 式绑定修复 +48~60% 兑现后的验收线）；`--max-overhead X` 可作绑定层劣化回归门禁（CI 下界超 X → exit 1）。结果落 `bench_out/wrapper_binding_overhead/<head>/<dylib8>/<manifest8>/<utc>/result.json`（bench_manifest.v1 锚定，dirty 须 `--allow-dirty`）。截至本注记日 clean 构建数据尚未采集，本节 2.3–2.5× 数字仍为无 SHA 锚定的历史观察；**任何绑定层优化的验收必须以该脚本的 CI 结果取代本节单次数字**。

### 6.2 real_user_workflow_h264（纯功能验证，不纳入性能结论）

出处 `demo_export_2K_h264.mp4.manifest.json`：2K_h264.mp4 → 240 帧 demo 时间线 → H.264 导出成功（21,922,507 B，sha256 6b3b7cb7…，delivery_qc passed 全 0）。manifest 无任何计时字段。

### 6.3 real_user_workflow_h264_full（功能验证 + 单个导出量级数字）

出处 `real_user_workflow_h264_full/workflow_result.json`：8 步（探测/导入/120 帧刀切/高斯模糊/调色栈/Composite 4 节点/保存重载/H.264 含音频导出）全部 passed。唯一计时：生产导出 wall 9.4316 s / **25.446 fps**（240 视频帧 + 451 音频帧，输出 22,105,859 B），`hw_fallback_reason` 为空。此为端到端**导出**口径（含模糊+调色+合成渲染），非播放口径；单次运行无 CI，仅量级参考。注：结果内工程路径指向旧仓 `topos_1` 工作树（step 7 detail），与 `extended_real_tests_20260917` 卡片备注的旧仓现象一致。

---

## 7. 实测结论与推测的边界

**实测（有落盘数据支撑）**
1. §2.1 D1 四素材 p50/p95/p99/fps/RSS 及 25/30fps 预算判定。
2. §3.1 readahead 全部相对收益与区间、parse 否决、t8/t16 静机结论。
3. §4 同档位 4K/2K 耗时比 3.1–3.9×；edge_jag worker 扫描曲线。
4. §6.1 真实 2K 素材 380.69 fps 解码与 wrapper/native 2.3–2.5 倍差。

**推测（标注为推测，未经覆盖目录数据验证）**
1. 30fps 时间线 4K 4444xq 会偶发尾部超支——由 p99（最高 36.6 ms）对 33.3 ms 预算的推断；D1 不含上传/呈现，端到端是否真丢帧未测。
2. "D1 关闭 readahead 的数字 + readahead 收益可叠加"——方向合理但两套协议/素材/日期不同，不可数值复合。
3. 4K 次线性放大归因于 slice 并行摊薄固定开销——与扩展曲线一致，但曲线采于有负载机器。
4. `decode_path=context` 即产品播放实际路径——D1 诊断字段如此记录，本报告未核对源码路径选择逻辑。

---

## 8. 最大数据缺口（按影响排序）

1. **首帧/冷启动延迟零基线**：覆盖目录内无任何首帧计时（first_frame_repro 无计时、D1 无冷启动字段、readahead 只测稳态）；"首帧延迟"子题当前处于无数据状态，最近的可用数据（p0_runner cold_first_frame、p2_09 冷重建）样本极小且不在本组覆盖。
2. **产品默认配置（readahead on + arena + t16）的端到端播放数字缺失**：D1 是 readahead 关闭态解码边界，readahead 是另一协议稳态吞吐，二者之间没有一次"当前默认配置、真实素材、含丢帧计数"的播放基准。
   > **过渡运行采集注记（BENCH-0007，2026-09-29 追注）**：缺失的"过渡运行"已有官方脚本 `tools/bench_default_config_playback.py`——同一 harness、同素材、轮内交替跑两配置臂（`readahead_auto` = ToposMediaSource 全默认构造 vs `readahead_<N>` = 显式深度；arena 批量读已并入当前代码），**唯一差异是 readahead 深度，结果可数值复合**，即弥合"D1（read_ahead_depth=0 口径）与 readahead/（另一协议）不可数值复合"的断层。注意当前代码的播放角色默认 readahead 为**关闭**（`_READAHEAD_MAX_DEPTH = 0`，同步批量会把整批成本集中到首个显示帧抬高 4K p95/p99），"auto 臂"测到的即现行产品默认；readahead 证据链的收益配置须以显式深度臂测得。数据纪律：`--repeats` 默认 3（<3 写 `repeats_warning`）；结果落 `bench_out/default_config_playback/<head>/<dylib8>/<manifest8>/<utc>/result.json`。截至本注记日数据尚未采集，本节缺口状态不变。
3. **端到端丢帧/卡顿遥测缺失**：`docs/4k-playback-optimization.md` §4.2 要求的 `dropped_frames/late_frames/queue_depth/分段耗时` 字段在覆盖目录内无落盘；丢帧只能由 p99 间接推断。
   > **遥测采集注记（BENCH-0008，2026-09-29 追注）**：§4.2 的 decode 侧字段已由 `tools/bench_default_config_playback.py` 落盘（与 BENCH-0007 同脚本，见上注）：`playback_frame_path`（decode→CPU planar 边界声明）、`decode_ms avg/p50/p95/p99`（逐帧）、`late_frames`（decode_ms > 1000/timeline_fps）、`dropped_frames`（节拍消费模式下落后超过一个整帧周期而未被拉取的帧数；`--no-pace` 时为 null）、`queue_depth`（每次取帧后 readahead 缓存占用采样）。GPU/呈现侧字段（texture_write_ms/yuv_render_ms/render_submit_ms/ui_tick_ms/coalesced_video_commands）在结果的 `unmeasured_fields` 中如实声明为未测——须离屏 GL harness（`extended_real_tests_20260917` 先例）补齐，本注记不宣称端到端遥测已闭合，只闭合 decode 边界的落盘缺口。
4. **4K H.264/HEVC 真实素材的解码边界缺失**：D1 只测 TOPOS 素材，real 系列只有 2K——4K 互压缩素材（用户最常见的 4K 卡顿场景）在覆盖目录内无边界数据。
   > **待采集注记（BENCH-0009，2026-09-29 追注）**：本缺口已有官方采集脚本 `tools/bench_real_4k_h264_hevc.py`（参数化 codec=h264/hevc、解码臂 ffmpeg 软解/videotoolbox 硬解、`--frames/--repeats/--random-seeks`；输出到 `bench_out/real_4k_intercodec/<head>/<dylib8>/<manifest8>/<utc>/result.json`，每个 combo 块与 d1_playback result.json 同构——`input/config/diagnostics_after_open/sequential_runs/random_seek` 字段与统计口径逐一对应）。数据纪律：**正式采集必须 `--repeats >= 3`**（默认 3，<3 时结果写入 `repeats_warning` 且只能作单次观察），并经 `bench_manifest` 锚定 git head + dylib SHA（dirty 树须 `--allow-dirty` 并随结果标注）。`--dry-run` 可在无媒体环境自检。截至本注记日**数据尚未采集**，本节缺口状态不变。
