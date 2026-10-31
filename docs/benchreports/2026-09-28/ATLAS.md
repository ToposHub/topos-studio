# ATLAS.md — bench_out 性能总图谱（计划05 P2，2026-09-28）

> 交叉汇总层：输入为本目录 P0 数据卡（`cards-group1~5.md`）与 P1 主题报告（`playback.md` G1、`encode-export.md` G2、`codec.md` G3、`native-threading.md` G4、`quality.md` G5、`ab-evolution.md` G6、`misc.md` G7），全部数字的文件级出处见各报告；本文只做矩阵化与提炼，不引入新测量。
> 有效性总纲（`bench_out/HISTORICAL.md` P2-08）：除 `058d29da/` 不可变结果树外均为**历史观察**；相对结论（"快/慢 x%"）须同 clean HEAD + 同 dylib SHA + 交替 A/B 中位数 + bootstrap 95% CI 且区间不跨 1.0。指标名与口径的精确定义见同目录 `METRIC_DICTIONARY.md`。

---

## 0. 总体结论（10 句以内）

1. `bench_out/` 共约 60 个数据目录、7 大主题、时间跨度 2026-08-31 至 09-28，规范性呈三级演进：P0-04 前的散点前史（哈希目录/m8–m11/codec_bench 等）→ P0-04 manifest 结果树（`058d29da` 为唯一当前有效根，`e1_encode`/`d1_playback` 为其后续追加）→ 09-27/28 自带源/CLI/脚本 SHA 的 `no_release_verdict` 探针（native domain、paired_q01、vt ramp）。
2. 全库满足"clean + 交替 A/B + CI 不跨 1"门槛、可写严格相对结论的只有 `d9050239`（黑苹果）与 `b0907366` 的 2K 轮（threads_eff=4），其余一切比值只能作方向性引用。
3. 速度面：4K 编码全六档 topos 领先 ProRes 1.5–2.1×（黑苹果口径）、解码 12 格 11 格领先（`codec.md` §2.1）；播放 D1 边界 2K proxy 约 707 fps、最重的 4K 4444xq 也有 39–43 fps（25fps 预算内，30fps 下 p99 有击穿）（`playback.md` §2）。
4. 体积/画质面呈"更多比特换更高 PSNR"结构：全六档体积大于对应 ProRes（1.03–3.79×，98% 体积帽验收线不满足），但码率配平 ±2% 下 Y-PSNR 全档大幅领先 prores_ks（+7.5~+27.2 dB）（`codec.md` §1.1、`quality.md` §3.1）。
5. 已被数据证实的三大优化主线：熵编码 V1→V2 同画质省 23–31% 码率（三个独立目录互证）、导出直通快路径 7.9×（identity 链 9.6→95.02 fps）、GPU 效果链解锁 24.3×（`ab-evolution.md` §0）。
6. 线程轴接近枯竭：4K XQ 分段 12 线程即饱和（12→16 仅 +1.5%）、p95 41.63 ms 仍高于 40 ms 帧预算，重载荷纯内核 t16 反劣于 t8（0.799）——产品 `min(8, cpu)` 策略有据（`native-threading.md` §1、`encode-export.md` §1.2）。
7. 引用红线共四类：`round2/` 整目录 UAF 失效、`topos_paired_q01_real_source/4444xq` 参照降位深自标无效、`topos_quality` 2K 4444xq MAE 异常、`9a8bfd99` vs `058d29da` 未锚定"速度翻盘"禁引（`quality.md` §6、`ab-evolution.md` §4）。
8. 最大数据缺口集中在播放与 GPU 侧：首帧/冷启动延迟零基线、端到端丢帧/卡顿遥测缺失、GPU vs CPU 导出无任何计时数据、4K 互压缩素材（H.264/HEVC）解码边界缺失（`playback.md` §8、`encode-export.md` §2）。
9. 机器谱系（黑苹果 24T → i9-9900K 8C16T → i9-12900T 16C24T → M1 → RX 6800 主机）与 fps 五级口径（内核循环/产品逐帧/D1 边界/E1 分段/时间线导出）叠加，使跨目录绝对值一律不可比——精确口径对照见 `METRIC_DICTIONARY.md` §9 歧义清单。

---

## 1. 主题 × 指标 × 时间线总矩阵

时间分档（7 期）：① 08-31（M8/M9 前史）｜② 09-01~02（M10/M11/readahead/达芬奇归因）｜③ 09-03~07（哈希目录群 + real 系列，P0-04 规则落地）｜④ 09-13~16（i9-12900T / D1 播放 / p0_runner / M1）｜⑤ 09-17~19（ab_* / export / anchor / ai_upgrade / codec_stages）｜⑥ 09-20~22（topos_quality / qgain / edge_jag / reexport）｜⑦ 09-26~28（e1_encode / native 域 / paired_q01 / 线程细化）。单元格 = 来源目录 + 代表值；"—" = 该期无该类指标落盘。

### G1 播放

| 指标族 | ① 08-31 | ② 09-01~02 | ③ 09-03~07 | ④ 09-13~16 | ⑤ 09-17~19 | ⑥ 09-20~22 | ⑦ 09-26~28 |
|---|---|---|---|---|---|---|---|
| 顺序解码延迟 p50/p95/p99（ms/帧） | m8：4K avg 12.30（均值口径，非分位） | — | — | d1_playback：2K proxy p50 1.35 / 4K 4444xq 23.2–24.6、p99 ≤36.6；p0_runner 稳态 p50（2K h264 2.234） | extended offscreen：presented p50 39.9（呈现侧，预算 40） | — | — |
| 解码吞吐 fps（多口径，见字典） | m8：4K 顺序 avg | readahead：2K 324→362（成对）；m11_stage4：2K 内核 390.6→429.4 | real_2k_h264：全片 380.69；wrapper 135.6 vs native 335.6 | d1_playback：707 / 249–277 / 171–204 / 39–43（四素材）；p0_runner：4K topos_proxy 212.1 | anchor matrix：native 433.92；current_anchor 415.06 | edge_jag/speed：w1 46→w16 492（2K proxy）；全池 4K hq 60 | — |
| 随机 seek 延迟（ms） | m8：4K 随机 avg 12.72 vs ProRes 73.15 | — | — | p0_runner：4K hevc 376.1 vs topos 12.9 / proxy 4.6（29–80×） | — | — | — |
| 冷启动/冷首帧（ms） | m8：4K 冷启动中位 281.6 vs 196.1/181.4 | — | — | p0_runner cold_first_frame（n=3 极小）；p2_09：冷首帧 h265 35.2 / prores 165.3 | — | — | — |
| RSS 峰值/增量 | m8：4K max 186.4 vs 582.6/952.7 MB | readahead：2K 稳态平台 ~195 MiB | — | d1_playback：83–146 / 730–746 MiB；p0_runner longrun 增量 ≤13.4 MB | — | — | — |
| 端到端丢帧/首帧遥测 | — | — | — | — | — | — | —（**全线空缺**，`playback.md` §8） |

### G2 编码/导出

| 指标族 | ① 08-31 | ② 09-01~02 | ③ 09-03~07 | ④ 09-13~16 | ⑤ 09-17~19 | ⑥ 09-20~22 | ⑦ 09-26~28 |
|---|---|---|---|---|---|---|---|
| 分段耗时 native/mux/python（ms/帧） | — | m10：分段计时 + CPU 相位（见下行） | — | topos_timeline_export：4K 解码 33.4 vs 编码循环 23.3（native 20.6 + mux 2.5） | topos_codec_stages：enc/trans/dec 三段（2K proxy 397.8/108.5/642.6） | — | e1_encode：native 占 wall 77–97%、mux 0.2–4.9、py <0.1（44 个 result.json） |
| native 内 CPU 相位占比（%） | — | m10：单遍 entropy 62/fill 32/crc 4；sized probe 82 | — | — | — | — | e1_encode `native_stage_ms_per_frame` 全 0 未填充（不可用） |
| 时间线导出 fps（产品口径） | — | davinci_context（叙事）；m11_stage1：4K hq 15.18→19.64 | — | topos_timeline_export：2K 66.2 全程/101.4 活跃；4K 29.9/38.4 | export_baseline：topos_proxy 67.0/157.2/45.1（三源最优）；anchor identity 9.598→T-P1-10 80.05→95.02；T-P0-31 暖 93.26；real_workflow_full 25.4 | — | — |
| 线程扩展 wall（t1–t16） | — | m103/m104/m1063：饱和点与流水线（详 G4） | — | threads_8v16：12900T 8→16 编码 +52%（详 G4） | — | — | e1_encode 5 轮：t1 224.1/t8 44.1/t16 32.7；3 轮细化：8T 39.43/12T 34.37/16T 33.86 |
| GPU vs CPU 导出 | — | — | — | cpu_real/gpu_real：体积差 1.69×（无计时）；macos_gpu_export（产物锚点） | anchor 14 组合 trail 全 `cpu_encoder_surface_path`；T-P1-21 24.3×；T-P0-31 全 GPU 链首通 | — | — |
| 阶段收益 A/B（m11 链） | — | m11_stage1/2b：+29%/+33%/+9%/+44%/+6.6%/+48~60%，三条负结果按门禁回退 | — | — | — | — | — |

### G3 codec（码率/档位/吞吐）

| 指标族 | ① 08-31 | ② 09-01~02 | ③ 09-03~07 | ④ 09-13~16 | ⑤ 09-17~19 | ⑥ 09-20~22 | ⑦ 09-26~28 |
|---|---|---|---|---|---|---|---|
| 档位码率（Mbps/bpp） | m8：4K(qp57) 612.83 Mbps | — | a986c3d9/c0f7068d：2K proxy 32.6→hq 136.8 Mbps；real_codec_bitrate：六档 48.1→584.6 | codec_bench M1：18.3→363.7 Mbps | topos_real_bench（12900T）：4K HQ 531.4 Mb/s @2.563 bpp | reexport：4K 60.2–1296.4 Mbps | — |
| 码率贴靶精度（bytes/target） | m8 sized：−18~−21% | m10 strict：2K +15.16%/4K −4.77%；m11_stage1：修到 ±0.5% | real_codec_bitrate：QP63 地板三档超支 1.40–1.67× | M1：+1.3~−6.9% | — | reexport：bpp ratio 0.985–1.003 | — |
| QP 分布（min/max/mean） | — | — | real_codec_bitrate：proxy/pro444/extreme 全 63（地板）；中档 54–59 | M1：32.4–77.9 | — | reexport qp_mean 68–82（**量纲跨代不可对齐**） | — |
| 编/解码速度 vs 参照（ms/帧、×） | m8：4K enc 82.77 vs ProRes 240.9 / DNxHR 30.8 | m1063 终局：4K enc 12.60 > ProRes 10.68（+18%）；m10 同线程解码 t8 | codec_bench：4K 全六档 1.5–2.1×；cbf6b308/77b91b28：0.136–0.238×ProRes、解码 2.0–2.6× | M1：六档编码 42–185 fps 全超实时 | topos_codec_stages：转码解码主导（2K proxy 解 6.3–7.3 vs 编 2.1–2.3） | reexport：dec vs ProRes 4.7–21.3× | — |
| core/E2E ratio + CI95 | — | 058d29da（09-02 23:42）：2K core 0.777 [0.753,0.801] | d9050239：2K 0.984 indist. / 4K 0.897 faster；9a8bfd99（09-04，**禁引翻盘**）；b0907366（09-07）：2K 0.439 CI 合格、4K dirty | — | — | — | — |
| 体积比 T/P（×） | m8：qp57 码率 −5.1% vs ProRes | — | real_codec_bitrate：1.03–3.79×；d9050239：T≈1.9×P | — | export_baseline：topos_proxy 18–37 MB vs ProRes 414–437 MB；ab_*/ab4k_* 字节漂移 ±1.1% | — | — |

### G4 native 域与线程

| 指标族 | ① 08-31 | ② 09-01~02 | ③ 09-03~07 | ④ 09-13~16 | ⑤ 09-17~19 | ⑥ 09-20~22 | ⑦ 09-26~28 |
|---|---|---|---|---|---|---|---|
| 线程扩展曲线/饱和点 | m8：t1–t8 packet_bytes 一致性 | m1063：2K 解码 t8 峰值 257（t16 −4%）；m103：t16≈t8 噪声级；git 0b6322d8：4K t8 饱和（t16 +0.7%） | — | threads_8v16（12900T）：8→16 enc +52%/dec +70%（16 物理核未饱和） | T-P1-11：2K t16 编码 121.8→170.3（sr 改动叠加） | reexport：t16/t1 = 6.4–7.4×（解码） | e1_encode 5 轮：1→16 加速 6.86×（效率 43%）；3 轮：12→16 仅 +1.5%（**~12 线程饱和**） |
| slice_rows 粒度效应 | — | m103：2K sr32→sr16 解码 +44%；m1063：sr16 解码 382.25（+48%）、编码 +21%，PSNR −0.2 dB | — | — | T-P1-11：sr16→sr8 编码 +39.9%/解码 +43.8%，码率 +≤1% | — | — |
| 流水线化增益 | — | m104：2K +54%/4K +22%（bitexact_vs_serial 全 true） | — | — | — | — | — |
| native 域压缩比/质量（RAW/float16） | — | — | — | — | — | — | native_domain_matrix（合成）：raw12 1.55–4.14×、raw16 1.48–5.85×、float16 2.84–10.14×；raw_real_cfa（真实）：2.54–102.4×/96.9→60.8 dB |
| float16 零/特殊类语义 | — | — | — | — | — | — | zero_audit：负零 ultra 变正零、high+ 变非零（不承诺 bit-exact）；Inf/NaN 类变化 0；max_half_ulp 2→1102 |
| 遮挡剔除（渲染层） | — | — | — | — | — | timeline_occlusion 4K B1：3.67×、请求 140→70（runs=2 观察值；2K 版无有效组） | — |

### G5 画质

| 指标族 | ① 08-31 | ② 09-01~02 | ③ 09-03~07 | ④ 09-13~16 | ⑤ 09-17~19 | ⑥ 09-20~22 | ⑦ 09-26~28 |
|---|---|---|---|---|---|---|---|
| 档位重编码 PSNR/SSIM/MAE | m8：4K PSNR-Y 60.39 vs 65.58/64.07 | — | — | — | topos_quality p0（09-20 起点） | p2 重校准（proxy +4.3 dB）；p3 空间 AQ 三项全负 | p5：2K 35.68→4444xq 44.14 dB（4444xq MAE 异常红线） |
| 同码率 Δ（插值，负=topos 差） | — | — | 6d749586/d9552407/cbf6b308：2K −1.8~−3.0；4K −2.1~−3.9；444 域 −5.8 dB | — | — | — | — |
| 配平配对 Y-PSNR（±2% 码率） | — | — | — | — | — | — | paired_q01 real：全档 +7.5~+27.2 dB（4444xq 非配平组自标无效）；synthetic：4 组方向性 |
| 感知/取证指标（grid/stair/sharp/block） | — | — | — | — | — | qgain：deblock 消块（block_h 2.67→−0.25，dec −5.7%）；sharp 0.838 vs ProRes 1.398；edge_jag：stair 0.319–0.360 < 0.6 无阶梯签名 | — |
| 世代损失（gen1→gen10） | m8：gen10 59.65 > ProRes 57.15 | — | 5bc83bef/6b3dfe9d：2K V1 qp45 十代 74.63→72.69 dB；c4082663：4K 同轨迹 | — | — | — | — |
| RAW/CFA 域质量 | — | — | — | — | — | — | raw_real_cfa：PSNR_min 96.9→60.8、max_abs 5→889 code（全档 bit_exact=false） |
| 12-bit 映射误差（code） | — | — | — | — | — | — | vt_12bit_ramp v1/v2：Y MAE 0.150/max 1，U/V 精确，逐位复现 |

### G6 A/B 演进

| 指标族 | ① 08-31 | ② 09-01~02 | ③ 09-03~07 | ④ 09-13~16 | ⑤ 09-17~19 | ⑥ 09-20~22 | ⑦ 09-26~28 |
|---|---|---|---|---|---|---|---|
| V1→V2 熵编码收益 | m9：6/6 格 −27.9~−31.7%、recon_parity 全 true | — | c4082663：4K qp45 −23.0%/qp60 −27.0%；5bc83bef/6b3dfe9d：2K qp45 −29.5% | — | — | — | — |
| 定档/AQ 消融 | — | — | 01a7466d（AQ 无操作 ratio 全 1.0）+64e7e219（生效 0.9617–0.9858）成对；94b31cc8 p4 定档省 26–33% | — | — | topos_quality p3：空间 AQ 三项全负 | — |
| 直通/GPU 链 fps 演进 | — | — | — | — | anchor 9.598→T-P1-10 80.05（7.9×）→T-P1-11 95.02；T-P1-21 grade 1.38→33.47（24.3×）；current_anchor 91.37 | — | — |
| 字节级 ab 漂移 | — | — | — | — | ab_*：h264 −0.17%、prores −0.45%、topos_proxy +0.06%、dnxhr 逐字节同；ab4k_*：h264 −1.10%、topos_proxy +0.52% | — | — |
| 回归/稳定性快照 | — | — | — | — | extended_real_tests：pytest 2879+1913、93 节点矩阵；W01–W08 8/8 PASS；T-P1-71 slope 2.199 MB/轮 | — | — |

### G7 杂项（归类层）

| 指标族 | ① 08-31 | ② 09-01~02 | ③ 09-03~07 | ④ 09-13~16 | ⑤ 09-17~19 | ⑥ 09-20~22 | ⑦ 09-26~28 |
|---|---|---|---|---|---|---|---|
| 哈希目录归类（17 个 = 8 AB + 9 非 AB） | — | — | 09-03 一天 11 个（P0-04 前大扫除）；09-04/07 复测与换机（i9-9900K 首现真名） | — | — | — | — |
| 外部参照叙事（达芬奇） | — | davinci_context：100/75 fps vs 本机 ffmpeg ~14；归因硬解+GPU+流水线（只作叙事） | — | — | — | — | — |
| 导出产物体积对照 | — | — | — | cpu_real/gpu_real：69.8 vs 118.2 MB（1.69×） | macos_gpu_export：39.87 MB 锚点 | — | — |

---

## 2. 各主题当前状态一句话（3–5 条/主题，带出处）

### G1 播放（playback.md）

1. 解码边界本身不是 25fps 播放瓶颈：D1 四档素材全部超实时，最重 4K 4444xq 39–43 fps（p50 23.2–24.6 ms），但 30fps 时间线下其 p99（最高 36.6 ms）会击穿 33.3 ms 预算（playback.md §0.1/§2；`bench_out/d1_playback/`）。
2. 读取策略（readahead/arena 聚读）是播放域证据最硬的收益：成对差分 2K +24.3% [+22.2,+28.4]、官方窗口 2K +10.5%/4K +11.7%（86.6→98.3 fps）（playback.md §3；`bench_out/readahead/`）。
3. 首帧/冷启动延迟在播放域覆盖目录内零数据（first_frame_repro 无计时、D1 random_seeks=0），仅 p2_09/p0_runner 有小样本旁证——本主题最大缺口（playback.md §5/§8）。
4. 端到端丢帧/卡顿遥测（dropped/late/queue_depth）全线缺失，丢帧只能由 p99 间接推断（playback.md §2.3/§8）。
5. 绑定层 wrapper 路径比 native 直调慢 2.3–2.5×（135.6 vs 335.6 fps），是播放路径可见的可优化项（playback.md §6.1；`bench_out/real_2k_h264_20260907/`）。

### G2 编码/导出（encode-export.md）

1. E1 编码边界（最新、manifest 化）内 native 占 wall 77–97%、mux 恒定 0.2–4.9 ms/帧、python <0.1 ms/帧——优化杠杆全在 native（encode-export.md §1.1/§4；`bench_out/e1_encode/`）。
2. 线程 t1→t16 提速 6.5–7×、12 线程饱和（<2%）；重载荷纯内核 t16 反劣于 t8（0.799 成对比值），产品 `min(8, cpu)` 策略有数据支撑（encode-export.md §1.2）。
3. 4K 无效果时间线导出是解码受限（源解码 33.4 vs 编码循环 23.3 ms/帧），时间线活跃 38.4 fps ≈ 编码子流程的 89%（encode-export.md §1.4；`bench_out/topos_timeline_export_2026-09-13/`）。
4. m11 五项落地改动把 4K hq 产品口径从 15.93 fps 推到全管线 2.22 fps（最后跳 +44%），三条负结果按门禁主动回退（encode-export.md §1.5；`bench_out/m11_stage1/`、`m11_stage2b/`）。
5. GPU vs CPU 导出在本主题目录内没有任何计时对比：仅有 cpu_real/gpu_real 字节差 1.69× 与 09-17 anchor"编码面 CPU"结构性证据，GPU 速度叙事须引用 ai_upgrade（encode-export.md §2/§4）。

### G3 codec 码率/档位/吞吐（codec.md）

1. 真实素材 1080p25 六档码率 48.1→584.6 Mbps、全档体积大于对应 ProRes（1.03–3.79×），98% 体积帽验收线当前不满足（codec.md §1.1/§5；`bench_out/real_codec_bitrate_20260907/`）。
2. 贴靶精度世代演进：−18~−21%（m8）→ ±0.5%（m11 RC）→ bpp 命中 0.985–1.003（reexport），但 proxy/pro444/extreme 的 QP63 地板超支 1.40–1.67× 是帧内 VLC 内核压缩上限、非码控问题（codec.md §1.2/§5.2）。
3. 速度分层：4K 编码全六档 1.5–2.1× ProRes（黑苹果口径）、M1 1080p 六档全超实时（42–185 fps）、解码 12 格 11 格领先；转码路径解码主导（2K proxy 解 6.3–7.3 vs 编 2.1–2.3 ms/帧）（codec.md §2/§5.3）。
4. 码率-画质关系证据不在本组：配平下 topos PSNR 大幅领先而体积更大（"更多比特换更高 PSNR"），"同质量更省"须等 Q01 扩样 + RD 曲线（决策 Q04）落地（codec.md §1.3/§5.4）。
5. QP 量纲跨代不可对齐（09-03 的 54–63 vs reexport 的 68–82 vs M1 的 32–78），任何跨代 QP 对比一律无效（codec.md §1.2 注）。

### G4 native 域与线程（native-threading.md）

1. 全部线程曲线均未达 HISTORICAL 门槛、一律趋势观察：4K XQ E1 分段在 i9-9900K 约 12 线程饱和（12→16 仅 +1.5%），p95 中位 41.63 ms 仍高于 4K25 的 40 ms 帧预算，未据此改产品线程策略（native-threading.md §1.1/§4；`bench_out/topos_perf_thread_refine_3rounds.json`）。
2. 最优线程数强依赖物理核数：8C16T 机饱和于 ≤8–12（t16 重载荷反慢 20%），16C24T 的 i9-12900T 上 8→16 仍 +52%/+70%——任何"最优线程数"结论只对列明机器成立（native-threading.md §1.2/§1.3）。
3. 2K 解码"t8 饱和"的真因是 slice 调度粒度而非线程数（sr32→sr16 解码 +48%）；流水线化（2K +54%/4K +22%，bitexact）是另一条正交收益路径（native-threading.md §4）。
4. native 域矩阵（raw12/raw16/float16，自带源/CLI/脚本 SHA）为合成 128×128 单次、全档 bit_exact=false、无计时字段，不进入任何线程/性能结论；真实 CFA 对照压缩比远超合成（102.4× vs 5.85×），二者必须分组呈现（native-threading.md §2.1）。
5. float16 零值审计：四个有损档均不承诺带符号零 bit-exact（负零 ultra 变正零、其余变非零），但无 Inf/NaN 类变化、无非零塌缩；产品侧已改为写盘前拒绝非有限值（native-threading.md §2.2）。

### G5 画质（quality.md）

1. YUV 域档位误差单调阶梯：2K proxy Y-PSNR 35.68 → 4444xq 44.14 dB、4K 同档普遍高 3–22 dB；低档位在 p0→p5 重校准中提升最大（proxy +4.29 dB），跨 tag 数值不可平均（quality.md §1；`bench_out/topos_quality/`）。
2. 码率配平（±2%）真实素材下 Topos 全档 Y-PSNR 领先 ffmpeg prores_ks 同名档 +7.5~+27.2 dB，是 Q01 核心证据；引用须带"prores_ks 非 Apple VT、源为有损解码素材"两条限定（quality.md §3.1；`bench_out/topos_paired_q01_real_source/`）。
3. 真实 CFA raw16 六档压缩 2.54×–102.4×、PSNR_min 96.9→60.8 dB、max_abs 5→889 code——真实低熵素材压缩比远超合成矩阵，绝对值不可外推（quality.md §4）。
4. 12-bit 边界：yuv444p12le→4444XQ→解码回 12-bit 的映射误差 Y MAE 0.150 code / max 1 code、U/V 精确，v1/v2 逐位复现（引用以 v2 为准）（quality.md §5）。
5. 三处无效数据为引用红线：round2 整目录（P0-01 UAF）、paired_q01 的 4444xq 非配平组（参照 12-bit 静默降级）、topos_quality 2K 4444xq 行 MAE≈14.3 异常（quality.md §6）。

### G6 A/B 演进（ab-evolution.md）

1. codec 层最大单笔收益是熵编码 V1→V2：同画质省 23–31% 码率，被 m9/c4082663/5bc83bef 三个独立目录互证且重建逐位一致（ab-evolution.md §0.1/§2.1）。
2. 产品导出层最大单笔收益是 T-P1-10 直通快路径（identity→topos_proxy 10.15→80.05 fps，7.9×），identity 场景链式改进 9.6（09-17）→95.02（09-18）fps（ab-evolution.md §0.2/§2.5）。
3. GPU 效果链解锁（T-P1-21）：grade 1.38→33.47 fps（24.3×）等 7 场景全部过门槛，基线"forced_gpu 与 cpu 同速"是 GPU 从未生效的结构性缺陷（ab-evolution.md §0.3）。
4. 满足 clean+CI 门槛的严格相对结论仅 d9050239 与 b0907366 2K 轮；9a8bfd99 vs 058d29da 的"速度翻盘"因双工作树未锚定而禁止引用（ab-evolution.md §2.2/§4）。
5. 回归信号总体干净（R1–R7）：字节级 ab 对仅 KB 级漂移、pytest 失败均定位到环境，正收益改动均伴随位流/质量门禁（ab-evolution.md §3）。

### G7 杂项归档（misc.md）

1. 17 个哈希目录按性质二分：8 个 AB/演进性质（归 G6）+ 9 个单方矩阵/RD 扫描（归档供 G3/G5 引用），09-03 一天 11 个目录是 P0-04 规则落地前的大扫除（misc.md §1）。
2. 哈希目录群机器构成混杂（黑苹果"Genuine Intel 0000" → i9-9900K 真名）且多个 dylib 指向旧仓（topos/topos_1），绝对值跨目录不可比是 P0-04 之前的系统性缺陷（misc.md §0.2）。
3. davinci_context 是唯一外部参照归因档案：达芬奇 7× 优势来自硬解+GPU 缩放+自研编码器+流水线，其数字只作叙事、不得当性能数据（misc.md §3）。
4. cpu_real/gpu_real 同项目同素材两次导出体积差 1.69×（69.8 vs 118.2 MB），是 GPU/CPU 路径输出比特流不同的观察点；性能结论须回对应运行目录（misc.md §4）。
5. 长期锚点候选：a986c3d9/77b91b28 的 topos 档位质量行（跨机构逐位一致）与 d9552407 的 Δ 插值表（口径注记必须随行）（misc.md §7）。

---

## 3. 跨主题横向观察

1. **规范性时间线**（misc.md §7 建议）：哈希目录应视为"P0-04 前史"分区（标注 commit 日期、机器、clean 与否、dylib 指向仓库）；`058d29da` 是唯一当前有效树根，`e1_encode`/`d1_playback`/`topos_perf_thread_refine` 为其 manifest 树后续；09-27/28 的 native domain/paired_q01/vt ramp 自带三重 SHA 与 `no_release_verdict` 状态，是历史目录中锚定最完整的范式。
2. **机器谱系**：黑苹果 24T（09-01~03 多数）→ i9-9900K 8C16T（09-02 M10/M11、09-07 起多次、09-26~28 E1）→ 24 逻辑核掩码 Intel（m103/m104）→ i9-12900T 16C24T（09-13）→ M1（09-16）→ RX 6800 24 核 X-TEST（09-17~19 ai_upgrade）。任何绝对值跨机器引用一律无效；线程结论只对列明机器成立。
3. **同一叙事的多源互证**（可直接引用的组合）：V1→V2 码率收益（m9 + c4082663 + 5bc83bef）；readahead 收益（readahead 四份文档内部成对）；直通/GPU 链（anchor + T-P1-10/11/21 + current_anchor + T-P0-31 五点链式）；QP63 地板（real_codec_bitrate + m107c strict + 全格式计划自注）。
4. **口径矛盾需带注引用**：E1 分段 t16 略优（33.9 vs 39.4 ms）与 m11_stage2b 纯内核 t16 慢 20% 方向相反（载荷/口径不同，两处均实测）；topos_vs_ref 静机重测的"4K 反超 +13~15%"修正了脏机口径的 1.36×（引用须保留其自判修正语义）。
5. **卡片勘误已采纳**（native-threading.md §6，供后续引用者使用）：m103 的 bytes_delta 实为 2K sr8 −1.20%（非"≤0.4%"概称）；native domain matrix 的 float16-finite 子目录 toos SHA 为 `4b8e5366`（非统一 06508c5c）；native_domain_smoke 实为 4 源 + 1 组往返产物。

---

## 4. 引用红线汇总（跨主题）

| # | 红线 | 出处 |
|---|---|---|
| 1 | `bench_out/round2/` 整目录：52a2dbf0 子树 UAF（P0-01）数字全部禁用；d9050239 两跑仅历史观察 | quality.md §6.1、cards-group4 |
| 2 | `topos_paired_q01_real_source/4444xq/`（非 match）：prores_ks 12-bit 输入静默降 10-bit，自标 `invalid_reference_encoder_format` | quality.md §6.2 |
| 3 | `topos_quality` 2K 4444xq 行 MAE≈14.3 异常：不可引用、不可当退化证据；PSNR/SSIM 引用需附注 | quality.md §6.3 |
| 4 | `9a8bfd99` vs `058d29da` "速度翻盘"：双工作树、interpretation 字段语义不稳、文件大小方向反转——禁止引用 | ab-evolution.md §4 |
| 5 | `benchmarks/timeline_occlusion_bench_result.json`（2K 版）：仅 B0 单轨组、剔除未激活，无有效 A/B 组 | native-threading.md §3/§5 |
| 6 | `e1_encode/*/result.json` 的 `native_stage_ms_per_frame`：44/44 文件全 0，字段未填充不可用 | encode-export.md §1.1 |
| 7 | result.json 的 `interpretation` 字段（058d29da 时期）：与 ratio 方向不一致，引用以 ratio+CI 为准 | cards-group1、ab-evolution.md §2.2 |
| 8 | 跨代 QP 值（54–63 vs 68–82 vs 32–78）与 m8 世代 sized 误差（−18~−21%）：量纲/RC 已换代，不可与后续对齐 | codec.md §1.2/§4 |
