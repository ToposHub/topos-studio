# METRIC_DICTIONARY.md — 统一指标字典（计划05 P2，2026-09-28）

> 收录 `bench_out/` 各报告（`docs/benchreports/2026-09-28/` 下 cards-group1~5 与 G1~G7 主题报告）中出现过的全部指标，按主题分组表格化。每条含：指标名、单位、来源目录、测量口径/定义、可比性约束。
> 有效性总纲（`bench_out/HISTORICAL.md` P2-08）：除 `058d29da/` 外均为历史观察；相对结论须同 clean HEAD + 同 dylib SHA + 交替 A/B 中位数 + bootstrap 95% CI 不跨 1.0。**§9 列出同指标不同口径的显式歧义；§10 列出已判无效/不可用的字段。**

---

## 0. 通用约定

- 时间类：`ms/帧`（毫秒每帧）、`s`（墙钟秒）；分位数口径写明 p50/p95/p99 或 avg/median；fps = frames per second（注意 §9.1 的五级口径）。
- 质量类：`dB`（PSNR，注明参考基准）、`-`（SSIM 无量纲）、`code`（12/16-bit 码值单位）、`ULP`（half-precision 单位最后位）。
- 体积/码率：`B`（字节，导出 manifest 与 result.json 的 file size）、`MB/MiB`、`Mbps/Mb/s`（均指比特率；折算口径见 §9.3）、`bpp`（bits per pixel）。
- 比值：`×`（倍率）、`ratio`（两臂耗时比，<1 = 分子侧更快，除非另注）、`CI95`（bootstrap 95% 置信区间）。
- 一致性：`sha256`/`digest` 逐位一致（bool）、`recon_parity`/`bitexact_vs_serial`（bool）。

---

## 1. 跨主题通用元指标

| 指标名 | 单位 | 来源目录 | 测量口径/定义 | 可比性约束 |
|---|---|---|---|---|
| ratio_median（core/E2E 耗时比） | 无量纲 | `058d29da`、`9a8bfd99`、`d9050239`、`b0907366` 各 `result.json` | topos 与参照（ProRes HQ/DNxHR HQX）同任务耗时中位数之比，<1 = topos 更快；core = 编码内核直调，E2E = 含 Python 壳/mux 全路径 | 须同 clean HEAD + 同 dylib SHA + 交替 A/B + CI 不跨 1（仅 d9050239、b0907366 2K 轮达标）；interpretation 字段不可信（§10.2） |
| CI95 | 区间 | 同上、`readahead` | bootstrap 95% 置信区间（core/E2E 链）或成对差分区间（readahead/m11_stage4） | CI 缺失（null）的运行只能方向性引用 |
| 成对差分增益（%，+区间，n/N 对正） | % | `readahead`、`m11_stage2b`、`m11_stage4`、`m1063_kernel` | 同窗相邻成对运行 fps 比值中位数 −1；报告正对数（如 12/12 对正） | 跨会话机器漂移 ±5%，只信同窗成对（readahead 自注） |
| fps（吞吐） | fps | 全库 | 帧率吞吐；**五级口径见 §9.1** | 跨口径/跨机器/跨构建绝对值一律不可比 |
| wall ms/帧 | ms/帧 | `e1_encode`、`m10_encode_probe`、`m11_*`、`topos_timeline_export` | 墙钟时间 ÷ 帧数；E1 分段 = 平面准备→native→mux | 须注明分段边界与线程数；均值 vs p50 不可混 |
| p50/p95/p99（帧级延迟） | ms | `d1_playback`、`p0_runner`、`e1_encode`、`topos_timeline_export` diagnostic | 逐帧耗时分位数（sequential_runs / 帧级列表） | d1 为 repeats=2 范围值；e1 逐帧 p95 为轮中位 |
| RSS 峰值/增量 | MiB / GB / MB | `d1_playback`（rss_peak_mib）、`e1_encode`（process_peak_rss_scope，GB）、`m8`（RSS max，MB）、`p0_runner`（rss_delta_bytes） | 进程驻留内存峰值或 longrun 前后增量 | e1 含约 20 帧源预载非导出峰值；单位三制并存（§9.5） |
| 文件大小 / 输出体积 | B | 各 export manifest（topos.export_manifest.v1）、result.json | 最终产物字节数（含容器开销） | ab_* 对只能说明比特流漂移，非质量回归判定 |
| sha256 / digest 一致性 | bool | `m11_stage1/3a`、`m104_pipeline`、`reexport` | 输出文件或码流逐位哈希对比（A/B 输出不变性钉死） | digest 相同使性能差可归因纯速度 |
| git/dylib/manifest 锚定 | — | `bench_manifest.v1` 树（058d29da/e1_encode/d1_playback 等） | `<commit>/<dylib8>/<manifest8>/<UTC>/` 四级路径 | 无锚定目录（多数历史目录）只可作历史观察 |

---

## 2. G1 播放（playback.md）

| 指标名 | 单位 | 来源目录 | 测量口径/定义 | 可比性约束 |
|---|---|---|---|---|
| 顺序解码 p50/p95/p99 | ms/帧 | `d1_playback` 各 result.json `sequential_runs` | D1 边界：ToposMediaSource read_next_frame → CPU planar 帧（排除 GPU 上传/调色/合成/呈现），96 帧 × 2 repeats | read_ahead_depth=0（关闭态）测得；与 readahead 协议不可数值复合 |
| 稳态顺序解码 p50 | ms/帧 | `p0_runner` `steady_sequential` | 产品媒体源分阶段协议：warmup 10 / steady 120 / seek 30 / longrun 600 | repeats=1 无 CI；run1 与 run2/3 commit 不同 |
| 解码吞吐 fps（D1/产品/native/wrapper） | fps | `d1_playback`、`real_2k_h264_20260907` decode_comparison、`topos_anchor_matrix_20260917` | D1 边界 fps；120 帧对照 fps（FFmpeg/VT/wrapper/native 四路）；native 锚点 fps | 四路像素格式/硬件路径不同，仅瓶颈定位（real_2k 自注） |
| 随机 seek p50/max | ms | `p0_runner` `seek_random`（30 次）；`m8` decode_4k 随机 avg | 随机跳转后单帧取回延迟 | p0_runner 一次运行；m8 为 avg 口径 |
| 冷首帧 p50 | ms | `p2_09_pool_*.json`（16 流轮换后冷流）；`p0_runner` cold_first_frame（n=3） | 会话池逐出重建后取首帧延迟 | 两次运行口径不同（是否剔除预热帧），采信 114837 |
| 热流 p50 / fps | ms / fps | `p2_09` rotation_16_streams | LRU（cap=12）轮换后驻留流吞吐 | 行为验证级，无锚定 |
| 会话池行为计数 | 个 | `p2_09` | streams_opened/evictions/live_after_rotation/evicted_stream_rebuild_ok | 功能验证口径 |
| 尾部比 p99/p50 | 倍 | `d1_playback`（playback.md §2.3 派生） | 尾部抖动刻画 | 派生指标，单跑尖峰（25.7 ms）非稳定形态 |
| 丢帧/卡顿遥测（dropped/late/queue_depth） | — | **无落盘** | docs/4k-playback-optimization.md §4.2 要求字段 | 全线缺失，只能 p99 间接推断 |

## 3. G2 编码/导出（encode-export.md）

| 指标名 | 单位 | 来源目录 | 测量口径/定义 | 可比性约束 |
|---|---|---|---|---|
| wall_ms_per_frame（E1 分段） | ms/帧 | `e1_encode` 各 result.json | ToposVideoEncoder 输入准备→native→mux，排除源解码/GPU；16 测量帧 + 4 预热 | 7 构建仅 2 个 clean；跨构建绝对值不可比，只看构建内线程曲线 |
| native/mux/python/other 分段耗时 | ms/帧 | `e1_encode`、`topos_timeline_export_2026-09-13` diagnostic_4k_components | 分段计时：native_encode / mux_add_packet / prepare_planar 等 | native_stage_ms_per_frame 字段全 0 未填充（§10.1） |
| 逐帧 p95/p99 | ms | `e1_encode`、`topos_perf_thread_refine_3rounds.json` | 帧级耗时分位数（轮中位） | 试点自标 local_pilot_not_product_export |
| 线程 wall 中位（t8/t12/t16） | ms/帧 | `topos_perf_thread_refine_3rounds.json`（3 轮序平衡）、`e1_encode/c1245ec6`（5 轮扫描） | 同构建内线程数扫描 wall 中位 | 两构建（c1245ec6 vs 1d30609f）禁止相减 |
| native 内 CPU 相位占比 | % | `m10_encode_probe` 各 txt | worker 累计 CPU：entropy/fill/crc/串行（单遍）或 probe量化/fill/DCT/entropy（sized） | Mac-Pro t8、历史观察级；相位间含串行残差不可直接相加成 wall |
| probe 次数/帧 | 次 | `m10_encode_probe`、`m1063_kernel` qp_hint A/B | 定码率模式每帧率控探针编码次数 | qp 提示后 7→3（同窗 A/B） |
| strict 档 bytes/target 中位 | % | `m10_encode_probe/m107c_strict` | 帧字节/目标码率偏差中位 | 仅对合成素材语义成立 |
| 时间线导出 fps（全程/活跃） | fps | `topos_timeline_export_2026-09-13` report.md | 全程 = wall 均速；活跃 = 排除空转后的段落均速 | 单次运行；源路径旧仓 topos_1 |
| 导出编码器横评 fps | fps | `export_baseline_*.json` | 产品导出路径 4 编码器（h264/prores_422/dnxhr_lb/topos_proxy）同源横比 | 单次、源帧数不一（480/240/120），跨源不可比；24 帧轮排除 |
| 全管线流水线 fps | fps | `m11_stage1` stage3b（1.54→2.22） | 含源解码/渲染/编码的产品全管线口径 | 与产品逐帧、内核口径三级差异巨大，引用必须注明 |
| 端到端工作流 wall/fps | s / fps | `real_user_workflow_h264_full` workflow_result.json | 8 步全链（含模糊+调色+合成+含音频导出）墙钟 | 单次无 CI，仅量级；导出口径非播放 |
| GPU vs CPU 导出 | — | `cpu_real.mov`/`gpu_real.mov`（1.69× 体积差）、`topos_anchor_matrix_20260917`（trail 字段）、`ai_upgrade/T-P0-31` | 仅产物字节差 + 编码面路径标注 + 全 GPU 链 fps（93.26 暖） | manifest 无计时字段；GPU 速度叙事须引用 ai_upgrade |

## 4. G3 codec 码率/档位/吞吐（codec.md）

| 指标名 | 单位 | 来源目录 | 测量口径/定义 | 可比性约束 |
|---|---|---|---|---|
| 档位码率 | Mbps | `real_codec_bitrate_20260907`、`codec_bench`、`a986c3d9`/`c0f7068d`/`topos_real_bench_2026-09-13` | 最终 MOV 实占折算（含容器）或码流折算；`mbps_1080p25_new` 为 1080p25 归一 | 折算基准不一（§9.3）；机器/构建不可横比 |
| bpp（实际/目标） | bits/pixel | `real_codec_bitrate`、`94b31cc8` p4_retier、`topos_real_bench` | 每像素比特；target_bpp 为档位目标锚 | 目标锚本身多次重定档（0.89→0.6019→0.64483→0.7448） |
| 体积比 T/P | × | `real_codec_bitrate`、`d9050239`、`export_baseline` | topos 输出字节 ÷ ProRes 同任务字节 | 位深不对齐行（pro444：P12 vs T10）不作严格比较 |
| QP min/max/mean | 量化参数 | `real_codec_bitrate` topos_qp_*、reexport qp_mean、M1 平均 QP | 帧级 QP 分布 | **跨代量纲不可对齐**（§9.6） |
| QP63 地板超支倍数 | × | `real_codec_bitrate`（0.9286/0.6051=1.53× 等） | 实际 bpp ÷ 目标 bpp（qp 钉死 63 时） | 内核压缩上限，非码控误差 |
| 编/解码耗时（vs 参照） | ms/帧 或 × | `codec_bench`（96 帧 × 3 中位）、`cbf6b308`/`77b91b28` 配对耗时比、`reexport` spec_speed_matrix | topos 池 16 线程 vs ffmpeg -threads 16；或同帧配对比值 | 批次间（黑苹果→M1）绝对值严禁横比；码率未配平时速度声明须带码率条件 |
| core/E2E ratio | 无量纲 | 见 §1 | — | — |
| 转码分解（解码/编码 ms/帧） | ms/帧 | `topos_codec_stages` transcode 分解 | 逐帧解码与编码分段计时（96 帧 × 3 passes 中位） | 无 manifest、源旧仓，历史观察 |
| enc/trans/dec_out fps | fps | `topos_codec_stages` | encode_only（已解平面）/ transcode / decode_out 三口径 | 三口径不可互换 |
| 解码 vs ProRes 倍率 | × | `reexport` spec_speed_matrix | topos dec fps ÷ ProRes dec fps（4K 4.7–21.3×） | 无锚定目录、reps 不一 |

## 5. G4 native 域与线程（native-threading.md）

| 指标名 | 单位 | 来源目录 | 测量口径/定义 | 可比性约束 |
|---|---|---|---|---|
| 线程扩展曲线（t1–t16 fps 或 ms/帧） | fps / ms/帧 | `e1_encode`（5 轮/3 轮）、`m1063_kernel`、`m103_slice_rows`、git 0b6322d8、`reexport` decode_threads | 固定负载下扫描线程数 | 全部未达 CI 门槛，一律趋势观察；机器依赖强（§9.7） |
| 加速比 / 并行效率 | × / % | 5 轮扫描（1→16 6.86×、效率 43%）、reexport（t16/t1 6.4–7.4×） | 相对 t1 的提速倍数；效率 = 加速比 ÷ 线程数 | 仅构建内有效 |
| 饱和点 | 线程数 | native-threading.md §1.3 汇总表 | 收益 <2% 或转负的最小线程数 | 只对列明机器/负载成立 |
| slice_rows 扫描（fps + bytes_delta_vs_base） | fps / % | `m103_slice_rows`、`m1063_kernel`、`ai_upgrade/T-P1-11` | sr32/16/8 × t8/t16；码流体积相对基准变化 | 2K sr8 为 −1.20%（勘误）；PSNR 代价 −0.2 dB@67.7 档 |
| 流水线化增益（serial→w2/w3） | % | `m104_pipeline` | 流水线 worker 相对串行 fps 提升；bitexact_vs_serial 全 true | 同目录前后两轮差近 2×，必须点名子目录 |
| native 域压缩比 | × | `topos_native_domain_matrix`（合成）、`topos_raw_real_cfa_probe`（真实） | 源字节 ÷ 编码字节 | **合成与真实不可混排**（5.85× vs 102.4×）；每档单次、bit_exact 全 false |
| PSNR_min / MAE_max / max_abs | dB / code / code | 同上 | 四相位最小 PSNR、最大 MAE/max_abs（code = 码值） | 状态自标 synthetic_or_input_domain_measurement_no_release_verdict |
| max_half_ulp / p99 half_ulp | ULP | `topos_native_domain_matrix` float16、`float_zero_audit` | 半精度解码值相对源的单位最后位漂移 | 有损档语义，非 bit-exact 承诺 |
| 零/特殊类计数（zero_sign_changes、source_zero_to_nonzero、negative_zero_to_nonzero、source_nonzero_to_zero、special_class_changes） | 个 | `topos_native_domain_float_zero_audit` | 编解码前后 ±0、零↔非零、Inf/NaN 类变化的逐通道计数 | 与 matrix 同源码流可交叉验证；CLI SHA 三代注意版本 |
| 遮挡剔除 speedup / culled_ratio / 取帧请求 | × / 比例 / 次 | `benchmarks/timeline_occlusion_bench_result*.json` | cull on/off 的时间线渲染 wall 中位比；剔除比例；layer 取帧请求计数 | **渲染层帧请求口径非编码导出**；runs=1/2 无 CI，3.67× 为观察值；2K 版无有效组 |

## 6. G5 画质（quality.md）

| 指标名 | 单位 | 来源目录 | 测量口径/定义 | 可比性约束 |
|---|---|---|---|---|
| Y-PSNR（档位重编码） | dB | `topos_quality` quality_*.json | 对 4K ProRes 4444 母版重编码、stride 采样（p5：stride 5，99 帧） | 跨 tag（p0→p5）档位重校准，不可平均 |
| SSIM / MAE_y / block_index | - / 灰阶 / 指数 | `topos_quality` | 同上采样口径 | 2K 4444xq MAE 异常禁引（§10.4） |
| 同码率 Δ（插值） | dB | `6d749586`/`d9552407`/`cbf6b308` rd_vs_prores_result | topos−ProRes 在同码率点的 PSNR 差（ProRes RD 曲线插值；负 = topos 差） | HQ 档越界输出 null；与配平 Δ 不可互相校准（§9.8） |
| 配平配对 Y-PSNR / Δ | dB | `topos_paired_q01_real_source` `-match` 组 | 码率配平 ±2%（rate_matched_2pct）下 30 帧逐帧均值；Δ = Topos − prores_ks | prores_ks 非 Apple VT；源为有损解码素材；非 match 组码率未配平须注明 |
| 码流比 T/P（配对） | × | paired_q01 各组 | 同源两编码器输出字节比 | 4444xq 非 match 组自标无效（§10.3） |
| 世代损失 PSNR（gen1→gen10） | dB | `5bc83bef`/`6b3dfe9d` generations、`m8` quality_4k | 10 代重复编解码的每代 PSNR 轨迹 | V1/V2 同轨迹仅字节差；PSNR=Infinity 为非标准 JSON 字面量 |
| 感知/取证指标：grid_residual_ratio / stair_axis_ratio / sharpness_retention / block_h/block_v | 无量纲 | `qgain`、`edge_jag` | 阈值语义：grid>1.2 = 误差集中 8×8 块界；stair>0.6 = 轴向锯齿；sharp<1 = 高频损失 | 阈值仅对该工具链成立；deblock old 库在 /tmp 无 SHA |
| y_psnr_edge / rgb_luma_psnr_edge | dB | `qgain`、`edge_jag` rgb_report | 边缘区域 PSNR（Y 或 RGB luma） | 边缘掩码口径由脚本定义 |
| deblock 前后 block_h/v 与 dec_fps | 无量纲 / fps | `qgain` deblock_cmp_* | 去块开关 A/B 的块效应指数与解码速度 | 非成对（4K 波动 −4~+43%）；old 库锚定缺失 |
| 空间 AQ 消融（ΔPSNR/ΔSSIM/Δfps/Δ码率） | dB / - / % / % | `topos_quality/p3_spatial_aq_ab.json` | AQ on vs off（2K，stride 20，26 帧/臂） | 单次 A/B 无 CI，方向性 |
| RAW/CFA 域 PSNR_min/MAE_max/max_abs | dB / code / code | `topos_raw_real_cfa_probe` | 真实 RGGB raw16 六档往返（2048×2048 单裁切） | 单次；与合成矩阵分组呈现 |
| 12-bit 映射误差（mean_error/MAE/max_abs） | code | `topos_vt_12bit_ramp_probe(_v2)` | yuv444p12le→p416le→4444XQ→解码回 12-bit 的逐通道码值误差（128×128、2 帧） | synthetic_mapping_probe_no_quality_verdict；引用以 v2 为准（脚本+ffmpeg 版本锚定） |
| 双解码器一致性（Y MAE/max） | code | paired_q01 `independent_decode_3f.json` | Apple（p416le 高 12 位）vs ffmpeg（yuva444p12le）解码差 | 仅解码侧佐证 |
| maxerr（RD 扫描） | 码值 | `5bc83bef`/`6b3dfe9d`/`c4082663` rd_result | 重建最大误差（qp≤35 为 0 = 精确重建） | 确定性编码器可复现 |
| luma_max_abs | 码值 | `real_2k_h264_20260907` quality.json | H.264→TOPOS 往返对解码后 422 10-bit 的最大亮度误差 | 参考基准是有损 H.264 解码帧非原始源 |

## 7. G6 A/B 演进（ab-evolution.md）

| 指标名 | 单位 | 来源目录 | 测量口径/定义 | 可比性约束 |
|---|---|---|---|---|
| rate_delta_pct（V1→V2） | % | `m9/vlc_acceptance.json` | v2 相对 v1 同格码率变化（6 格全负） | 同 lib_commit 分流、无 CI；与 RD 曲线互证 |
| decode_speedup（V1→V2） | × | `m9` | v1/v2 解码耗时比（1.00–1.17×） | 同上 |
| recon_parity | bool | `m9` | v1/v2 重建逐位一致 | — |
| aq_retier ratio（b_aq/b_base） | 无量纲 | `01a7466d`、`64e7e219` | AQ 重定档后/前码率比 | 两 commit 成对解读"改动前后"；无 CI；无容差判定字段（match_pct=0）不可作脚本间比较 |
| p4_retier b2/b1 | 无量纲 | `94b31cc8` | 新/旧目标档实际编码字节比（语义文件内未注明，推测） | 跨 commit 不可比 |
| 直通/GPU 链 fps（链式） | fps | `topos_anchor_matrix_20260917`→`ai_upgrade` T-P1-10/11/21、current_anchor、T-P0-31 | 同场景（identity 时间线导出 topos_proxy）跨改动时点 fps | 各点独立运行，贡献拆分未经同窗交替 A/B |
| 效果额外成本倍率 | × | `topos_anchor_matrix_20260917` | (effect_total − identity 总耗时) ÷ identity | runs=1 |
| forced_gpu vs cpu_render fps | fps | anchor matrix、current_anchor | 两选路 fps 差 | 09-17 前差距 <3% 属路径相同噪声；GPU 链生效后（09-18+）才有意义 |
| full_gpu_blockers | 个 | `ai_upgrade/T-P0-31` | 全 GPU 链阻塞项计数（首次为空） | 诚实标注体系 |
| ab 字节漂移 | % / B | `ab_*`、`ab4k_*` manifest | dev 相对 baseline 的输出字节变化 | 无计时/质量 json，漂移无法定论（需像素对拍） |
| delivery_qc（blocking/warning/info） | 计数 | 各 topos.export_manifest.v1 | 交付质检分级计数 | 全部 passed（0/0/0）是功能验证非性能 |
| pytest 通过/失败/跳过 | 计数 | `extended_real_tests_20260917` | 回归套件尾行汇总 | 截断日志无汇总行不可当失败率；结论随代码演进过期 |
| 内存增长 slope | MB/轮 | `ai_upgrade/T-P1-71` | H265 200 轮长跑 RSS 线性斜率（2.199 < 5 阈值判有界） | 稳定性口径 |
| presented p50 / effective_fps | ms / fps | `extended_real_tests` edit_tracks_offscreen | 离屏播放呈现侧延迟（budget 40 ms）与有效帧率 | 早于 T-P1-21 修复，含 120/120 CPU 回退 |

## 8. G7 杂项（misc.md）

| 指标名 | 单位 | 来源目录 | 测量口径/定义 | 可比性约束 |
|---|---|---|---|---|
| 外部参照导出 fps（达芬奇/本机 ffmpeg） | fps | `davinci_context` | 用户实测 + 本机复现的 4K 导出速率 | 非受控基准，只作归因叙事，不得当性能数据 |
| GPU/CPU 产物体积比 | × | `cpu_real.mov`/`gpu_real.mov` | 同项目同素材两次导出字节比（1.69×） | manifest 无计时，仅比特流差异观察点 |
| 哈希目录计数与归类 | 个 | 17 哈希目录 | 8 AB + 9 非 AB；09-03 一天 11 个 | 归类层元数据，非测量指标 |
| capability registry 条目数 | 项 | 各 export manifest | 导出时功能能力注册表快照 | 功能面证据非性能 |

---

## 9. 同指标不同口径的歧义清单（显式）

**9.1 fps 至少五级口径，绝对值互比无意义**
① 内核循环口径（m1063 的 332/89.8、m103/m104 的 fps、reexport 矩阵）；② 产品逐帧口径（m11 各阶段、readahead 成对）；③ D1 播放边界（`d1_playback`，readahead 关闭态）；④ E1 编码分段折算（`e1_encode` wall ms/帧 → fps）；⑤ 时间线导出口径（全程 vs 活跃两栏，`topos_timeline_export`/export_baseline/anchor）。另有渲染层帧请求口径（timeline_occlusion）与 wrapper/native 双路（real_2k）。同名"fps"跨口径差可达 10×（如 4K hq：内核 40+ vs 全管线 2.22 vs E1 折算 ~63）。

**9.2 PSNR 至少四种参考基准**
① 对母版重编码（topos_quality，对 4K ProRes 4444 母版）；② 对源逐帧均值（paired_q01，对有损解码源）；③ 同码率插值 Δ（rd_vs_prores，ProRes RD 曲线插值）；④ 域最小值（native domain PSNR_min，四相位取最小）。数值不可互相校准；444 域两套（插值 −5.8 dB vs 配平 +20.6/+27.2 dB）方向相反即因口径不同。

**9.3 码率 Mbps 三种折算**
① 最终 MOV 文件实占按 25fps 折算（real_codec_bitrate，含容器开销）；② 码流/帧字节折算（rd_result 的 Mbps、topos_real_bench）；③ 档位目标归一 `mbps_1080p25_new`（retier 脚本）。引用须注明折算基准；prores probe 输出 12-bit vs topos 10-bit 的位深不对齐行另计。

**9.4 ms/帧 的均值/中位/p50 混用**
rd_result 的 enc_ms 为单值均值无 CI；codec_bench 为 96 帧 × 3 次中位；d1_playback 为逐帧 p50；e1 为 16 帧 wall。同一"编码耗时"四代口径不可混排。

**9.5 RSS 三种单位与三种口径**
`d1_playback` rss_peak_mib（MiB，解码边界峰值）；`e1_encode` process_peak_rss_scope（GB，含约 20 帧源预载）；`m8` RSS max（MB，解码会话）；`p0_runner` rss_delta_bytes（longrun 增量）。峰值 vs 增量语义不同，不可互换。

**9.6 QP 量纲跨代不可对齐**
09-03 黑苹果 54–63（63 = 地板）→ reexport qp_mean 68–82 → M1 平均 32.4–77.9。无文档确认换算关系（codec.md §1.2 注）；任何跨代 QP 对比无效。

**9.7 "最优线程数"的机器依赖**
8C16T（i9-9900K）饱和 ≤8–12 且重载荷 t16 慢 20%；16C24T（i9-12900T）8→16 仍 +52%/+70%；24 逻辑核掩码机 t16≈t8。结论只对列明机器成立。

**9.8 ΔdB 两套口径**
插值 Δ（96 帧矩阵 + ProRes RD 插值）与配平 Δ（30 帧 ±2% 码率配平逐帧）不可互相校准（quality.md §8）；前者负值（topos 差）、后者大幅正值（topos 优）并存。

**9.9 MAE 的位深与基准差异**
topos_quality 的 MAE_y（灰阶，4444xq 行异常禁引）、native domain 的 MAE_max（code，12/16-bit 码值）、vt ramp 的 MAE（code，12-bit）、independent_decode 的 Y MAE（code）。单位同名"code"但位深不同，数值不可横比。

**9.10 压缩比的合成/真实二分**
native_domain_matrix（合成 128×128，raw16-16 仅 5.85×）与 raw_real_cfa（真实 CFA，raw16-16 达 102.4×）——素材熵不同，必须分组呈现不可混排（数据卡明文规则）。

**9.11 ratio 的臂组合差异**
core vs E2E vs 配对速度比（codecs_matrix）vs 产品逐帧（topos_vs_ref）vs 成对差分（readahead/m11）——同为"topos vs ProRes 比值"至少五种臂组合；topos_vs_ref 静机重测还自判修正了脏机口径（4K 1.36→1.13~1.15），引用须保留修正语义。

---

## 10. 已判无效/不可用字段清单

| # | 字段/数据 | 位置 | 失效原因 | 处置 |
|---|---|---|---|---|
| 1 | `native_stage_ms_per_frame` | `e1_encode/*/result.json`（44/44） | 字段存在但全为 0（未填充） | native 相位回退 m10 探针 |
| 2 | `interpretation` | `058d29da` 等早期 result.json | 与 ratio_median 方向不一致（全 "ref faster" 而 ratio<1），语义当期不稳定 | 引用以 ratio+CI 为准 |
| 3 | 4444xq 非 match 组 ProRes 分数 | `topos_paired_q01_real_source/4444xq/` | prores_ks 12-bit 输入静默降 10-bit，自标 invalid_reference_encoder_format | 12-bit 对比只用 -match/VT 系列 |
| 4 | 2K 4444xq 行 MAE | `topos_quality/` | MAE≈14.3 与同档 4444 的 4.1 及自身 PSNR 严重不自洽（疑参考口径不含该档） | MAE 禁引；PSNR/SSIM 附注 |
| 5 | 52a2dbf0 子树全部数字 | `bench_out/round2/` | `_Kernel._ensure_planes` use-after-free（P0-01），输出与统计不可信 | 整目录禁用（round2_invalidated.md） |
| 6 | `match_pct` | `01a7466d` aq_retier.json | 无容差判定输出（均 0） | 只能作"无操作"证据，不可跨脚本比较 |
| 7 | 2K 版遮挡剔除 A/B | `benchmarks/timeline_occlusion_bench_result.json` | 仅 B0 单轨组、culled_ratio=0，剔除未激活 | 整体排除，不作 2K 证据 |
| 8 | b1/b2 字段语义 | `94b31cc8` p4_retier.json | 文件内未注明（推测旧/新档字节） | 跨 commit 不可比 |
| 9 | 截断的 pytest 日志 | `extended_real_tests_20260917`（feature_suites 等） | 无结束汇总行（运行被截断） | 不可当失败率引用 |
