# G2 主题报告：编码/导出（encode-export）

> 计划05 P1 基准分析，2026-09-28。覆盖 `bench_out/` 下 `e1_encode`、`m10_encode_probe`、`export_baseline`、`reexport`、`macos_gpu_export_20260915`、`metal_timeline_export`、`topos_timeline_export_2026-09-13`、`m11_stage1/2b/4`。数据卡片见同目录 `cards-group2/3/4/5.md`。
>
> **有效性总纲**（`bench_out/HISTORICAL.md` P2-08）：除 `058d29da/` 结果树外均为历史观察；相对结论须同 clean HEAD + 同 dylib SHA + 交替 A/B 中位数 + bootstrap 95% CI 不跨 1.0。本文所有数字均标注出处；实测（M）与推测/推断（P）分开标注。机器口径混杂（i9-9900K 黑苹果 / Mac-Pro 8P16L / 24 逻辑核 Intel / M1 / RX 6800 主机），跨目录绝对值一律不可比。

---

## 1. 导出各 stage 耗时分布

### 1.1 E1 编码边界：输入准备 → native → mux（最新、manifest 化，M）

口径：`ToposVideoEncoder` 输入准备 → native 编码 → mux，排除源解码/GPU/Resolve（`e1_encode/*/…/result.json` 的 `scope` 字段）。7 个构建目录共 44 个 result.json，全部带 `bench_manifest.v1`（git head、dylib SHA、ABI、CPU、线程）。其中仅 `a3748229`（2026-09-26）与 `44784357`（2026-09-27）为 clean 构建。

clean 构建分段（出处：`e1_encode/a3748229/ff40a1fb/e3632f8e/20260926T2154*/result.json`、`e1_encode/44784357/d052edae/2709a8be/20260927T120*/result.json`；行标"源→档"沿用 `docs/codec/TOPOS_F01基线_2026-09-27.md` 表）：

| 素材→档位 | 线程 | wall ms/帧 | native | mux | python | other |
|---|---|---:|---:|---:|---:|---:|
| 2K prores422Proxy→hq | t1 | 28.52 | 27.30 | 0.48 | 0.05 | 0.69 |
| 2K prores422Proxy→hq | t16 | 5.45 | 3.86 | 0.74 | 0.06 | 0.79 |
| 2K prores422HQ→hq | t1 | 37.39 | 35.84 | 0.45 | 0.04 | 1.05 |
| 2K prores422HQ→hq | t16 | 5.82 | 4.35 | 0.69 | 0.05 | 0.72 |
| 4K prores422Proxy→hq | t1 | 106.93 | 102.72 | 1.86 | 0.05 | 2.30 |
| 4K prores422Proxy→hq | t16 | 19.69 | 14.58 | 2.77 | 0.07 | 2.27 |
| 4K prores422HQ→hq | t1 | 119.05 | 114.48 | 2.09 | 0.05 | 2.43 |
| 4K prores422HQ→hq | t16 | 18.62 | 14.30 | 2.10 | 0.06 | 2.16 |
| 4K 422 HQ（N09 补测） | t1 | 110.58 | 108.49 | 1.40 | 0.04 | 0.65 |
| 4K 422 HQ（N09 补测） | t16 | 15.87 | 13.71 | 1.53 | 0.05 | 0.59 |
| 4K 4444（10-bit 444） | t16 | 22.78 | 18.76 | 2.82 | 0.05 | 1.15 |
| 4K 4444 XQ（12-bit 444） | t16 | 29.67 | 24.28 | 3.92 | 0.05 | 1.42 |

阶段占比（P，由上表推算）：native 占 wall 77–97%（t16 约 77–86%，t1 时 mux/python 被放大相对变小、native ≥95%）；mux 恒定 0.23–4.92 ms/帧（4K 约 1.4–4.9，随档位字节量上升）；python 输入准备 <0.1 ms/帧（<1%）；other（含 prepare_planar/调度残差）0.3–2.4 ms/帧。与 `docs/codec/TOPOS编码全面检查与优化计划_2026-09-27.md` F03 判断"native 是主要时间项、mux ≤2.8ms 次要、python 非瓶颈"一致（该文口径为 4K HQ native 18.03/21.61、mux 1.93 ms）。

注意：`result.json` 中 `native_stage_ms_per_frame`（fill/entropy/crc/dct 等细相位）在全部 44 个文件里均为 0（字段存在但未填充，M）——native 内部相位分布须回退到 §1.3 的 m10 探针。
> **修复注记（BENCH-0010，2026-09-29 追注）**：全 0 根因已定位——native 侧 `tc_encode_stage_stats` 只在环境变量 `TOPOS_CODEC_PROFILE=1` 时累计（`native/topos_codec/src/codec/codec.c` 的 `tc_profile_enabled`，首触惰性读 env），而 `tools/bench_encode_stages.py` 当时未设置该变量。脚本现已默认在测量前设置（`--no-native-profile` 回退），并在采集不到时写 `native_stage_ms_per_frame: null` + `native_timing_available: false`（不再写 0 冒充数据）。口径警告：profile 开启的热路径含计时钩子，修复后 wall/native 段与上表 44 个历史结果**不可直接对账**（历史锚点 ANC-16/ANC-20 引用须注明 profile 状态）；本表历史数字维持原文不改。

峰值 RSS：4K 4444xq 16 帧 t1–t16 为 2.16–2.27 GB（`process_peak_rss_scope` 注明含源预载，M，`e1_encode/c1245ec6/*` 与 `1d30609f/*`）。

### 1.2 线程扩展与饱和点（M）

| 构建（clean 性质） | 素材/档 | t1 | t8 | t12 | t16 | 出处 |
|---|---|---:|---:|---:|---:|---|
| a3748229（clean） | 2K hq | 28.5–37.4 | – | – | 5.45–5.89 | a3748229 树 10 个 result.json |
| a3748229（clean） | 4K hq | 106.9–119.1 | – | – | 17.9–19.7 | 同上 |
| 44784357（clean） | 4K hq | 110.6 | – | – | 15.9 | 44784357 树 4 个 result.json |
| c1245ec6（dirty） | 4K 4444xq | 200.7–225.8 | 44.0–44.4 | – | 30.0–34.5 | c1245ec6 树 27 个 result.json |
| 1d30609f（dirty） | 4K 4444xq | – | 39.2–39.6 | 33.6–37.0 | 33.2–34.2 | 1d30609f 树 9 个 result.json |

- t1→t16 提速约 6.5–7.0×（4K hq 110.6→15.9；4444xq 224→33；M）。
- 12 线程以上饱和：t12 34.4 vs t16 33.9 ms 中位（9 次运行序平衡取中位，`bench_out/topos_perf_thread_refine_3rounds.json` 的 `median_by_threads`：8T 39.43 / 12T 34.37 / 16T 33.86 ms，p95 48.23/43.49/41.63；12→16 收益 <2%）。该文件自标 `local_pilot_not_product_export`，git_dirty=true、每组仅 3 样本无 CI——按 HISTORICAL 规则只可作趋势观察（M）。
- 反直觉点：**重载荷编码 t16 比 t8 慢**。t16/t8 = 0.799（成对 10 对逐对比值全负 [0.707~0.826]，`m11_stage2b/20260902_stage3a_mux_dead_crc.txt` 附录）——HT 同核争抢 entropy/fill，产品导出维持 `min(8, cpu)` 线程策略。与 E1 目录 t16 略优（33.9 vs 39.4）方向相反，属不同载荷/不同构建，引用时必须带口径（P，两处均实测、解释为载荷差异）。

### 1.3 native 内部 CPU 相位（M10 探针，2026-09-02，Mac-Pro i9-9900K t8，历史观察级）

| 场景 | wall ms/帧 | CPU 相位占比（worker 累计） | probe 次数/帧 | 出处 |
|---|---:|---|---:|---|
| 2K qp20 单遍 | 40.6 | entropy 62% / fill 32% / crc 4% / 串行 3% | – | `m10_encode_probe/20260902_2k_qp20_t8.txt` |
| 2K sized（定码率） | 52.2 | probe量化 82% / fill 8% / entropy 5% / DCT 4% | 10 | `…/20260902_2k_rate_t8.txt` |
| 4K sized（定码率） | 195.6 | probe量化 82% / fill 8% / DCT 5% / entropy 4% | 10 | `…/20260902_4k_rate_t8.txt` |
| strict 档 2K 真实素材 | 32.8 | qp63 钉死；bytes/target 中位 +15.16%（32/32 帧超靶） | 4 | `…/m107c_strict/20260902_real_material.txt` |
| strict 档 4K 真实素材 | 146.7 | 误差中位 −4.77% | 7 | 同上 |

- sized 定码率模式下探测量化占 CPU 的 82%，是当时编码路径第一大成本（M）；qp 提示优化后 probe 7→3 次、4K fps 8.35→12.91（+55%），输出误差分布不变（`m1063_kernel/20260902_qp_hint_ab.txt`，见 cards-group3）。
- m10-6.1 持久缓存：4K sized 126–129→108 ms（−16%），2K −17~20%，开关逐字节一致（`m1061_cache/20260902_ab.txt`）；m10-6.2 全 plane 统一提交：4K −11%（255→228 ms），1080p 打平（`m1062_ab/summary.txt`）。
- strict 档对照改动前 0f404603：2K 18.40→30.47 fps、4K 5.26→6.82 fps（`m107c_strict/20260902_batch_summary.txt`）。

### 1.4 产品级时间线导出分解（topos_timeline_export_2026-09-13，M）

时间线：单素材、无效果、planar 直通（487/487 与 743/743 帧全部直通），t16 编码 + t8 帧内源解码（`report.md`）。

| 分辨率 | 帧数 | 全程 | 全程均速 | 活跃速度 | 输出码率 |
|---|---:|---:|---:|---:|---:|
| 1920×1080 | 487 | 7.36 s | 66.2 fps | 101.4 fps | 134.0 Mb/s |
| 3840×2160 | 743 | 24.86 s | 29.9 fps | 38.4 fps | 531.2 Mb/s |

出处：`topos_timeline_export_2026-09-13/report.md` 主表、`results_product_policy_full_final.json`。

4K 组件分解（同解码帧分量计时，120 帧，`diagnostic_4k_components.json`，M）：

| 阶段 | mean ms/帧 | 等效 fps |
|---|---:|---:|
| 源 ProRes 解码（sequential） | 33.42（median 31.25 / p95 42.69） | 29.9 |
| 编码循环 `encode_frame` 合计 | 23.27 | 43.0 |
| ├ native `product_encode_packet` | 20.63 | 48.5 |
| ├ `mux_add_packet` | 2.50 | 400.7 |
| └ `prepare_planar` | 0.09 | – |

- 4K 无效果导出是**解码受限**：源解码 33.4 ms/帧 > 编码循环 23.3 ms/帧；时间线活跃 38.4 fps ≈ 完整编码子流程（43.0 fps）的 89%（`report.md` 对比段，P 由两行实测推算）。
- 对照纯编码口径（不含源解码/mux）：2K HQ 132.1 fps、4K HQ 53.2 fps（`../topos_real_bench_2026-09-13/report.md`，cards-group5；不同目录不同协议，只作方向参照）。

### 1.5 m11 导出提速各阶段（2026-09-02，i9-9900K，stash 同窗 A/B + sha256 位精确，M；dirty 树无 manifest）

| 阶段 | 改动 | 口径 | 前→后 | 增益 | 出处 |
|---|---|---|---|---:|---|
| 1 | 帧间反馈单遍码控 ToposRateFeedback | 产品 4K hq t16 | 15.18→19.64 fps | +29% | `m11_stage1/20260902_stage1_rc_singlepass.txt` |
| 1 | 同上（用户素材 1080p→4K 放大） | 产品 150 帧 | 15.93→19.79 fps | +24% | 同上 |
| 1 | 码率贴靶精度 | bytes/target | 系统性 −4.9% 欠靶 → ±0.5% | – | 同上 |
| 1 | 同码率画质 | PSNR | +0.3~0.55 dB | – | 同上 |
| 2a | 编码平面免拷贝直通 | 内核 4K qp55 t16 | 53.91→71.82 fps | +33% | `…/20260902_stage2a_plane_direct.txt` |
| 3a | Python 绑定零拷贝 | 产品 | 21.2→23.1 fps | +9%（输出 sha256 一致） | `…/20260902_stage3a_zerocopy.txt` |
| 3b | 导出渲染∥编码流水线 | 产品全管线（120 帧 4K hq） | 1.54→2.22 fps | +44%（480MB 输出一致） | `…/20260902_stage3b_pipeline.txt` |
| 3a-mux | mux 死 CRC 摘除 | 成对 10 对序平衡 | 比值中位 1.066，10/10 对正 [1.019~1.083] | +6.6%（输出不变） | `m11_stage2b/20260902_stage3a_mux_dead_crc.txt` |
| 3c | 绑定层 .raw 精确拷贝 | 绑定 / 产品 | 28.73→44.73 / 24.16→35.6–40.6 fps | +56% / +48~60% | `m11_stage2b/20260902_stage3c_binding_exact_copy.txt` |

累计：用户素材 4K hq 产品口径 15.93 → 19.79（阶段1）→ 24.9（+2a，累计 +56%）→ 23.1（3a）→ 全管线 2.22 fps（3b，含源解码/渲染侧）（各 txt 累计段，M）。

负结果档案（同为出口带宽探索，M，`20260902_stage2b_kernel_crc_attempts.txt`、`20260902_c028_crc_fold_reverted.txt`）：融合 AVX2 内核持平回退；逐 slice CRC 并行化净亏回退（唤醒风暴 > 2.24ms 串行 CRC 本体）；C028 CRC 折叠 PCLMULQDQ 版 −7.5%（10/10 方向一致）整案回退。**m11_stage4 为解码侧跨帧批量化（+5~10%），不属导出编码链，排除出本报告**（见 cards-group3 该卡）。

产品路径 ~24 ms/帧（4K hq rate，t8）剩余带宽账：内核 ~21ms（~87%）+ pmax 码值域守卫 1.70ms（~7%，正确性守卫不动）+ 输出拷贝 0.4ms + 壳 0.1ms（`mux_dead_crc.txt` 分段表，M）。

### 1.6 产品导出 4 编码器横评（export_baseline，2026-09-17，M）

单次运行、仅 platform 标注（无 git/dylib manifest）；三源帧数不一（480/240/120），跨源 fps 不可比，仅同源内横比成立。

| 源 | h264 | prores_422 | dnxhr_lb | topos_proxy | 出处 |
|---|---:|---:|---:|---:|---|
| 2K H264（480 帧） | 149.4 fps / 18.7 MB | 15.6 fps / 437.0 MB | 59.7 fps / 90.4 MB | 67.0 fps / 36.3 MB | `export_baseline_2K_H264.json` |
| 2K topos 源（240 帧） | 61.9 fps / 6.1 MB | 15.6 fps / 224.8 MB | 80.3 fps / 45.2 MB | **157.2 fps / 18.0 MB** | `export_baseline_2K_topos.json` |
| 4K H265（120 帧） | 23.4 fps / 19.8 MB | 4.5 fps / 413.6 MB | 27.1 fps / 89.9 MB | **45.1 fps / 37.2 MB** | `export_baseline_4K_H265.json` |

- topos_proxy 档在全部三源下均为**速度/体积同时最优或并列最优**（2K H264 源略慢于 h264 但体积小 48%）（M）。
- 24 帧小样本轮 fps 6–11（`export_baseline_2K_topos_proxy.json`），启动开销主导，仅验证通路，排除出结论。
- 同族（topos 源→topos 输出）planar 直通优势：157.2 vs 跨族 h264 61.9 fps（2.5×）（P，由同表推算）。

### 1.7 重导出/转码成本（reexport + 交叉引用，M）

`reexport/` 目录虽名为"重导出"，实际内容为 TOPOS 解码内核优化探针 + 2K/4K×7 档位 encode/decode 速度矩阵（无 manifest/commit/dylib SHA/时间戳，文件 mtime 2026-09-22；cards-group4）。

档位速度矩阵（`reexport/spec_speed_matrix.json`，bpp 命中率 ratio 0.985–1.003；"netproxy" 为该目录首见扩展档）：

| 档位 | 2K enc/dec fps | 4K enc/dec fps | 4K 码率 | 解码 vs ProRes（4K） |
|---|---|---|---:|---:|
| netproxy | 695.8 / 156.0 | 213.5 / 38.4 | 60.2 Mbps | 21.3× |
| proxy | 546.9 / 144.3 | 162.8 / 37.0 | 131.9 Mbps | 13.6× |
| lt | 364.1 / 131.7 | 107.1 / 36.7 | 280.4 Mbps | 10.8× |
| standard | 266.2 / 126.2 | 80.1 / 35.3 | 437.8 Mbps | 6.5× |
| hq | 192.8 / 120.8 | 62.3 / 32.5 | 654.4 Mbps | 5.4× |
| 4444 | 140.1 / 179.7 | 46.1 / 59.8 | 886.9 Mbps | 5.8× |
| 4444xq | 101.1 / 142.2 | 33.3 / 49.3 | 1296.4 Mbps | 4.7× |

转码（解+编逐帧）成本（`topos_codec_stages/20260919_15*/results.json`，96 帧×3 passes 中位，属 G3 目录但为重导出口径，交叉引用）：2K proxy 108.5 fps（解码 6.3–7.3 ms/帧 vs 编码 2.1–2.3 ms/帧，解码主导）、2K hq 80.6；4K proxy 47.4、4K hq 27.2（M）。

解码线程扩展（`reexport/decode_threads_2k.json` / `_4k.json`，3 reps 中位，digest 逐位一致）：2K proxy 53.5/192.0/338.3 fps（t1/t4/t16，6.3×）、4K proxy 13.3/50.7/92.1（6.9×）；16 线程约为单线程 6.4–7.4×（M）。解码滤波占解码耗时 ~44%（2K proxy t16 滤波关 537.2 vs 开 300.5 fps，`filtercost_off/on.json`，2 reps）；branchless +13%（335.8 vs 296.1 fps，digest 相同，`branchless_3reps.json` vs `reverted_3reps.json`）。

---

## 2. GPU vs CPU 导出

| 数据 | 内容 | 结论强度 |
|---|---|---|
| `macos_gpu_export_20260915/2K_h264_480frames.mp4.manifest.json` | macOS GPU（VideoToolbox）链路导出产物：1080p@25、480 帧 H.264、39,866,423 B（≈6.64 Mbps）、delivery_qc passed；manifest **无任何计时字段** | 仅证明当日 VT 链路可跑通 + 字节锚点（M）；**无性能数据** |
| `bench_out/cpu_real.mov` vs `gpu_real.mov`（+manifest，2026-09-15T05:15Z） | 同项目 "gpu-verify"、同素材 2K_topos_proxy.mov、1080p@24/48 帧/TOPOS_HQ：cpu 69,841,258 B vs gpu 118,211,337 B（**1.69×**）；cpu 侧资产 sha256 为 null（延后生成警告） | 两路径输出比特流不同（M）；体积差本身是路径差异观察点，但**无计时字段**，fps 声明无数据支撑（cards-group5 同判） |
| `metal_timeline_export/` | 仅 29 KB harness 脚本 `run_real_h264_ab.py`（Metal/OpenGL×h264/topos_proxy×16 混合模式×9 效果，默认 1280×720@120 帧；自注释"回读后交 FFmpeg/VT 编码，非 encoder-native 零拷贝 surface 路径"）+ pycache | **本目录无任何运行结果**——纯工具，性能数字须去 ai_upgrade/current_anchor 等目录（cards-group3） |
| `topos_anchor_matrix_20260917`（cards-group4，交叉引用） | TimelineExporter 14 组合 GPU trail/fallback_reason 全为 `cpu_encoder_surface_path`；forced_gpu 与 cpu_render fps 差 <3%（9.60 vs 9.77） | 截至 09-17 全链路 GPU 未达成（编码面仍 CPU）；差异属噪声 |
| `ai_upgrade/T-P0-31`（cards-group3，交叉引用） | 240 帧 1080p H.264 暖进程 93.26 fps 全 GPU 链，阶段 p50 decode 2.9 + render 9.2 + encode_submit 0.18 ms | GPU 导出链在 09-19 首次全通（full_gpu_blockers 空） |

结论（P，综合）：本组目录内**没有一组 GPU vs CPU 的计时对比数据**；GPU 侧仅有的量化证据是产物字节差 1.69×（cpu_real vs gpu_real）与 09-17 anchor 的"编码面 CPU"结构性证据；GPU 导出的速度叙事须引用 ai_upgrade 目录（其他主题）。GPU 渲染结果回读路径的口径声明以 `metal_timeline_export` 脚本注释为准。

---

## 3. 有效性与排除清单

| 目录/数据 | 处置 | 原因 |
|---|---|---|
| `e1_encode` dirty 构建（7 构建中 5 个 dirty） | 仅构建内线程曲线可用，跨构建绝对值不可比 | git_dirty=true（cards-group2）；`topos_perf_thread_refine_3rounds.json` 亦自标无 CI |
| `e1_encode/*/result.json` 的 `native_stage_ms_per_frame` | 全部为 0，不可用 | 字段未填充（M，44/44 文件核对） |
| `export_baseline` | 同源内横比可用；跨源/绝对 fps 排除 | 无 git/dylib manifest、单次无重复、源帧数不一（cards-group2） |
| `macos_gpu_export_20260915` / `cpu_real`+`gpu_real` | 仅产物锚点，无性能结论 | manifest 无计时字段 |
| `metal_timeline_export` | 排除（无结果） | 仅 harness 脚本 |
| `m10_encode_probe` / `m11_stage1/2b` | 历史观察级（方法学严谨：同窗成对、位精确对拍，但 dirty 无 manifest） | HISTORICAL P2-08 |
| `m11_stage4` | 排除出本报告 | 解码侧批量化，非导出链（cards-group3） |
| `reexport` | 方向性参考 | 无 manifest/时间戳、reps 口径不一（2 vs 3）（cards-group4） |
| `topos_timeline_export_2026-09-13` | 历史观察级 | 单次运行、源路径旧仓 topos_1、24T 机器 |
| `export_baseline_2K_topos_proxy.json`（24 帧轮） | 排除 | 启动开销主导（fps 6–11） |
| `e1_encode/probe_rate_*.mov`（44 个） | 未纳入分析 | 码率探针样本聚在 ~36/61/66/100 Mbps 四簇（文件名 35614–99959：~36 n=7、~61 n=10、~66 n=18、~100 n=9，M 按文件名统计），无配套指标 JSON |

## 4. 结论

1. **stage 分布**：编码边界内 native 占 wall 77–97%、mux 0.2–4.9 ms/帧、python <0.1 ms/帧——优化杠杆全在 native（M，e1_encode clean 构建 + F01/F03 交叉印证）。native 内部，单遍模式 entropy 62%/fill 32%（2K qp20），sized 模式探测量化 82%（m10）。
2. **线程**：t1→t16 约 6.5–7×，12 线程饱和（<2%）；重载荷下 t16 反劣于 t8（0.799 成对比值），产品策略 `min(8, cpu)` 有数据支撑（M）。
3. **产品导出**：4K 无效果时间线为解码受限（解码 33.4 vs 编码循环 23.3 ms/帧）；m11 五项落地改动累计把 4K hq 产品口径从 15.93 推到全管线 2.22 fps（+44% 为最后一跳），三条负结果按门禁回退（M）。
4. **GPU vs CPU**：本组内无计时数据；仅有字节差 1.69× 与"编码面 CPU"结构性证据，GPU 速度结论须引用 ai_upgrade（P）。
5. **重导出**：同族 topos→topos planar 直通 157.2 fps 为跨族 2.5×；转码路径解码占主导（2K proxy 解码 6.3–7.3 vs 编码 2.1–2.3 ms/帧）（M）。
