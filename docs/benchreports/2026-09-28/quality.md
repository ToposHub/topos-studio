# G5 画质主题分析（quality.md）

> 计划 05 P1 · 组 G5，2026-09-28。基于 `docs/benchreports/2026-09-28/cards-group*.md` 数据卡 + `bench_out/` 原始数据（只读，关键数字已二次抽查核实）。
> 覆盖：`topos_quality`、`topos_vs_ref`、`topos_same_vs_cross`、`topos_paired_q01_real_source`、`topos_paired_q01_synthetic`、`topos_raw_real_cfa_probe`、`topos_vt_12bit_ramp_probe(_v2)`、`qgain`、`round2`。
> 有效性总纲（`bench_out/HISTORICAL.md`，P2-08）：除 `058d29da/` 外均为历史观察；相对结论须同 clean HEAD + 同 dylib SHA + 交替 A/B 中位数 + bootstrap 95% CI 不跨 1.0。本组多数数据为"单次测量 + 自带 SHA 锚定"的自标 `no_release_verdict` 口径，引用时须带此注。

---

## 0. 结论速览

1. **YUV 视频域（重编码对 4K ProRes 4444 母版）**：档位误差呈单调阶梯，2K proxy Y-PSNR 35.68 dB → 4444xq 44.14 dB；4K 同档普遍高于 2K 约 3–22 dB（分辨率摊薄量化误差）。
2. **同源配对（真实素材、码率配平 ±2%）**：Topos 全档 Y-PSNR 大幅领先 ffmpeg prores_ks 同名档（proxy +7.5 dB … 4444xq-vt-12bit +27.2 dB），是 Q01 核心证据。
3. **RAW/CFA 域（真实 ProRes RAW 中心裁切 RGGB raw16）**：六档压缩比 2.54×–102.4×，PSNR_min 96.9 → 60.8 dB，max_abs 5 → 889 code；真实低熵素材的压缩比远超合成 ramp（合成 raw16-16 仅 5.85×）。
4. **12-bit 边界**：yuv444p12le 经 p416le → 4444 XQ → 解码回 12-bit 的映射误差 Y MAE 0.150 code / max 1 code，U/V 完全精确（v1/v2 逐位复现，ffmpeg 9.0.2，软件编码器路径）。
5. **三处已知无效数据**：`round2/`（P0-01 UAF，整目录禁用）、`topos_paired_q01_real_source/4444xq`（参照编码器 12-bit 静默降 10-bit，自标 invalid）、`topos_quality` 2K 4444xq MAE≈14 异常（疑口径问题）。见 §6。

---

## 1. YUV 视频域：档位误差水平（topos_quality）

母版 `tests/test_videos/4K_prores4444.mov` 重编码、stride 采样（p5：stride 5，99 帧）。出处：`bench_out/topos_quality/quality_recal-p5.json`（generated 2026-09-28，本表由原始 JSON 逐档重算核实）。

| 档位 | 2K Y-PSNR | 2K SSIM | 2K MAE_y | 2K block | 4K Y-PSNR | 4K SSIM | 4K MAE_y |
|---|---:|---:|---:|---:|---:|---:|---:|
| proxy | 35.68 | 0.9286 | 10.37 | 2.12 | 39.15 | 0.9547 | 7.55 |
| lt | 39.13 | 0.9644 | 7.36 | 1.28 | 43.78 | 0.9815 | 4.63 |
| standard | 41.30 | 0.9784 | 5.75 | 1.16 | 47.72 | 0.9937 | 2.65 |
| hq | 43.01 | 0.9863 | 4.64 | 1.09 | 55.26 | 0.9987 | 1.15 |
| 4444 | 43.73 | 0.9895 | 4.12 | 1.09 | 60.32 | 0.9995 | 0.67 |
| 4444xq | 44.14 | 0.9925 | **14.34**（异常，见 §6.3） | 1.05 | 65.81 | 0.9998 | 1.58 |

### 1.1 档位重校准演进（p0 → p5）

出处：`bench_out/topos_quality/quality_baseline-p0.json`（2026-09-20）vs `quality_recal-p5.json`（2026-09-28），同为该目录复测序列（中间还有 recal-p2/p4）。

| 档位 | 2K p0 | 2K p5 | ΔdB | 4K p0 | 4K p5 | ΔdB |
|---|---:|---:|---:|---:|---:|---:|
| proxy | 31.39 | 35.68 | +4.29 | 36.31 | 39.15 | +2.84 |
| lt | 36.21 | 39.13 | +2.92 | 42.75 | 43.78 | +1.03 |
| standard | 39.49 | 41.30 | +1.81 | 45.98 | 47.72 | +1.74 |
| hq | 42.32 | 43.01 | +0.69 | 53.53 | 55.26 | +1.73 |
| 4444 | 43.51 | 43.73 | +0.22 | 60.19 | 60.32 | +0.13 |
| 4444xq | 43.91 | 44.14 | +0.23 | 65.58 | 65.81 | +0.23 |

- **实测**：低档位（proxy/lt）p0→p5 提升最大，高档位（4444/4444xq）几乎不动；p4（stride 5）与 p5 数值几乎相同、耗时 1322.4 s vs 959.3 s（`quality_recal-p4.json` / `-p5.json`）。
- **推测**（数据卡归因，非文件自证）：p0 与 p2 之间发生了档位重校准（proxy 目标 bpp 调整），跨 tag 数值不可直接平均。
- 4444xq 的 MAE 异常在 p0（14.83）与 p5（14.34）两轮同型存在（见 §6.3）。

### 1.2 空间 AQ 消融（A/B，无收益）

出处：`bench_out/topos_quality/p3_spatial_aq_ab.json`（2026-09-20，2K，stride 20，26 帧/臂）。

| 档位 | Y-PSNR Δ (AQ−off) | SSIM Δ | 编码 fps off→AQ | 码率 Δ |
|---|---:|---:|---|---:|
| 2K/proxy | −0.182 dB | −0.004 | 266.7 → 247.2（−7.3%） | +0.2% |
| 2K/lt | −0.268 dB | −0.003 | 197.7 → 173.7 | +0.03% |

**实测**：空间 AQ 在该素材/口径下三项全负（质量略降、速度降、码率微升），未见收益。单次 A/B、无 CI，属方向性结论。

---

## 2. 感知/取证指标（qgain）

母版 4K_prores4444 reformat 到 2K YUV 422 10-bit limited 作参考。阈值语义（文件自注）：grid>1.2 = 误差集中在 8×8 块界；stair>0.6 = 轴向锯齿；sharp<1 = 高频能量损失。出处：`bench_out/qgain/base_2K_report.json` 等（2026-09-21）。

### 2.1 基线 vs ProRes 同帧对照（`qgain/base_2K_report.json`）

| 项 | Topos 2K proxy（33.35 Mbps） | ProRes Proxy 同帧 |
|---|---:|---:|
| y_psnr | 40.13 | 39.86 |
| y_psnr_edge | 36.37 | 35.63 |
| sharp（高频保持） | **0.838** | **1.398** |
| grid | 1.266 | 1.312 |
| stair | 0.369 | 0.348 |
| rgb_psnr | 36.97 | 36.68 |

**实测**：Topos PSNR 略高但高频能量（sharp）明显低于 ProRes——PSNR 优势与观感锐度并不同向。

### 2.2 码率预算与去块 A/B

| 实验 | 关键变化 | 出处 |
|---|---|---|
| proxy.target_bpp 0.64483→0.7448（+15.5% 码率） | yPSNR +0.45 dB、edge +0.59、rgb +0.44；grid/stair 基本不动 | `qgain/budget_2K_report.json` |
| deblock 开启（2K proxy，old lib /tmp vs new lib） | block_h 2.67→−0.25、block_v 3.23→−0.76（块效应基本消除）；yPSNR +0.09 dB；dec_fps 365.2→344.2（−5.7%） | `qgain/deblock_cmp_2K_topos_422_Proxy.json` |
| 无去块参照 | block_h 3.878 / block_v 4.37 → 量化块效应是 proxy 档主要画质损失来源；dec_fps 602.1 | `qgain/nodeblock_2K_report.json` |

可比性备注（实测/推测分开）：
- **实测**：deblock_cmp 的 "old" 库位于 `/tmp`（topos_prefix_lib），无 SHA 锚定且可能已失效；`toos_deblock_cmp_2K.json` 的 old_lib 与 new_lib 路径相同（该文件对比无效）；4K deblock dec_fps 变化 −4%~+43% 波动为非成对测量。
- **推测**：4444_XQ 行 y_psnr=−1.02 / sharp=26.8 系参考口径不含该档所致，不能当质量退化引用（数据卡判断，文件内无该说明）。

---

## 3. 同源配对压缩对照（topos_paired_q01_*）——Q01 核心证据

### 3.1 真实素材 + 码率配平（±2%）

源：`2K_prores4444XQ.mov` 解出 30 帧 1920×1080（source_origin=decoded-lossy-source，非无损原始）。每臂含源/库/两侧 mov 全套 SHA-256 与 ffmpeg 命令行；native dylib SHA d052edae…。Y-PSNR 为 30 帧逐帧均值（本表由 `per_frame` 重算核实）。出处：`bench_out/topos_paired_q01_real_source/<tier>/paired_*.json`。

| 组（全部 rate_matched_2pct=true） | 码流比 T/P | Topos Y-PSNR | prores_ks Y-PSNR | ΔdB |
|---|---:|---:|---:|---:|
| proxy-match | 1.003 | 42.55 | 35.02 | +7.53 |
| lt-match | 0.991 | 47.53 | 36.49 | +11.04 |
| standard-match | 0.988 | 50.55 | 36.92 | +13.63 |
| hq-match | 0.997 | 55.10 | 37.22 | +17.88 |
| 4444-vt-12bit-match | 0.999 | 57.89 | 37.29 | +20.60 |
| 4444xq-vt-12bit-match | 1.004 | 64.53 | 37.32 | +27.21 |

未配平组（有效但非等比特，Topos 默认码率约低 17–24%）：proxy 0.813 / 41.33 dB；lt 0.768 / 45.73；standard 0.839 / 49.04；hq 0.828 / 52.85（同目录非 match 子目录）。
ProRes 侧压缩点扫描：proxy-crf63（最狠点）码流比 7.74×，Topos 62.36 vs 35.02 dB（`proxy-crf63/paired_proxy.json`）。
独立解码一致性：Apple（p416le 高 12 位）vs ffmpeg（yuva444p12le 前 3 平面）双解码器 Y MAE 0.315 code / max 3（`4444xq-vt-12bit-match/independent_decode_3f.json`）。

**实测**：等比特条件下 Topos 各档对 prores_ks 同名 profile 全档大幅占优。**注意**：ProRes 侧为 ffmpeg prores_ks（非 Apple VideoToolbox），且源为有损解码素材；引用时须带这两条限定。

> **RD 曲线采集注记（BENCH-0013，2026-09-29 追注）**：本节单点配对证据不足以支撑决策文档 Q04（"默认档位更小 ≠ 同质量更省"，须扫 ≥5 质量点绘 RD 曲线 + 码率匹配/画质匹配两表）。官方采集脚本已就位：`tools/bench_q01_rd_curve.py`——同一冻结 planar 语料上，TOPOS 臂固定 QP 扫描（默认 20/32/44/56/63，覆盖到 QP63 地板）+ ProRes 参照臂 prores_ks `bits_per_mb` 码控扫描（默认 64~2048 六点），两臂 <5 点直接拒绝；自动产出码率匹配表（同码率 ΔPSNR-Y，±2% 配平门）与画质匹配表（同 PSNR-Y 的 bits 比），预注册 Q04 判定 `topos_same_quality_cheaper` / `topos_costs_more_at_same_quality` / `mixed_no_verdict`（发布宣称仍须扩样 + 盲测，脚本不自动宣称）。域限定与限注随结果落盘：prores_ks 参照（非 VT）、source_origin 必填、12-bit 444 档显式拒绝（防 prores_ks 静默位深转换，须走 `measure_topos_paired_quality.py` 的 VT 配平路径）。结果落 `bench_out/q01_rd_curve/<head>/<dylib8>/<manifest8>/<utc>/result.json`。截至本注记日数据尚未采集，Q04 判定状态不变（证据不足）。

### 3.2 合成微样本（topos_paired_q01_synthetic）

320×192 × 8 帧，4 组（出处：`bench_out/topos_paired_q01_synthetic/*/paired_*.json`）：

| 组 | 码流比 T/P | Topos Y | prores Y |
|---|---:|---:|---:|
| proxy（422p10） | 0.944 | 46.35 | 41.41 |
| 4444xq（444p12，VT 前） | 1.349 | 79.53 | 47.47 |
| 4444-vt-12bit | 0.973 | 70.76 | 47.51 |
| 4444xq-vt-12bit | 0.992 | 77.27 | 47.54 |

**实测**：非 VT 的 4444xq 组码率高 35%（码率未配平的口径前身）；-vt-12bit 系列即为此做的口径对齐，比值回到 ~1.0。合成 8 帧，仅方向性。

### 3.3 跨口径速度对照的画质侧面（topos_vs_ref / topos_same_vs_cross）

这两目录主体是速度对照（见 G6 `ab-evolution.md` 与数据卡组 5），画质侧仅一点可引用：
- `topos_same_vs_cross`：4K topos_hq 输出 253.9 MB vs proxy 29.0 MB（同源同帧数，`20260919_154423` 等 results.json）——同族转码下档位体积差 ~8.8×，可作档位码率跨度证据。
- `topos_vs_ref`：无质量指标（纯吞吐对照）。

---

## 4. RAW/CFA 域（topos_raw_real_cfa_probe，真实素材）

真实 ProRes RAW 素材中心裁切 2048×2048 RGGB raw16（crop_manifest：全帧 70.78 MB，四相位均值 R 1317 / Gr 1749 / Gb 1748 / B 613，最大 39168，crop SHA 03961f03…）。六档往返，toos CLI SHA 333b7df3…。本表由 `bench_out/topos_raw_real_cfa_probe/raw16_six_tiers/native_domains_raw16.json` 逐档重算核实（PSNR 取四相位最小值，MAE/max_abs 取最大值）。

| 档位 | 压缩比 | PSNR_min | MAE_max (code) | max_abs (code) |
|---|---:|---:|---:|---:|
| raw16-2 | 2.54× | 96.9 | 0.66 | 5 |
| raw16-4 | 17.86× | 67.0 | 22.67 | 188 |
| raw16-6 | 41.37× | 63.8 | 31.68 | 348 |
| raw16-8 | 58.94× | 62.8 | 35.16 | 537 |
| raw16-12 | 79.15× | 61.8 | 38.67 | 726 |
| raw16-16 | 102.37× | 60.8 | 42.62 | 889 |

- **实测**：全档 bit_exact=false；真实低熵 CFA 数据下高档压缩比（102×）远超合成矩阵同档（合成 raw16-16 仅 5.85×，出处 `bench_out/topos_native_domain_matrix/`，详表在 G4 报告）。
- **归类规则（数据卡明确）**：真实与合成趋势方向一致但绝对压缩比差异巨大（素材熵不同），必须分组呈现、不可混排。
- 单裁切、单次、2048×2048 一块、无重复——历史观察级，自带源/CLI/脚本 SHA。

---

## 5. 12-bit 边界证据（topos_vt_12bit_ramp_probe / _v2）

合成 12-bit ramp（yuv444p12le → ProRes p416le 输入 → 4444 XQ → 解码回 12-bit），128×128、2 帧，`hardware_acceleration_confirmed: false`（软件编码器路径）。出处：`bench_out/topos_vt_12bit_ramp_probe/vt_12bit_ramp.json` 与 `_v2/vt_12bit_ramp.json`。

| 通道 | mean_error (code) | MAE (code) | max_abs (code) |
|---|---:|---:|---:|
| Y | −0.0293 | 0.150 | 1 |
| U | 0.0 | 0.0 | 0 |
| V | 0.0 | 0.0 | 0 |

- **实测**：v2 与 v1 三个 SHA-256（源/编码/解码）与逐通道指标逐位一致（movie_sha256 7321a052…相同）——复现成功；v2 补齐 `measurement_script_sha256`（a97ab678…）与 `ffmpeg_version`（ffmpeg 9.0.2），**引用以 v2 为准**。
- **实测**：状态自标 `synthetic_mapping_probe_no_quality_verdict`——只可作"码值映射机制证据"，不可作压缩质量结论。
- 12-bit 视频域压缩质量的可用数据是 §3.1 的 -vt-12bit-match 两组（配平后 +20.6/+27.2 dB）与 §6.2 的无效 4444xq 组对照。

---

## 6. 三处已知无效数据（引用红线）

| # | 位置 | 无效原因 | 处置 |
|---|---|---|---|
| 1 | `bench_out/round2/`（整目录） | `_Kernel._ensure_planes` use-after-free（P0-01，平面数组悬空，native 向已释放内存写入）：解码输出与全部吞吐/RSS 统计不可信。52a2dbf0 子树数字全部禁用；d9050239 两跑虽在修复日之后，但 library 指向另一工作树且 4K 全部未过 CV 门，只作历史观察 | 任何相对结论禁止引用本目录（`round2/round2_invalidated.md` + HISTORICAL.md 表格） |
| 2 | `bench_out/topos_paired_q01_real_source/4444xq/paired_4444xq.json` | 本机 prores_ks 不支持 yuv444p12le 输入被静默降为 10-bit（文件自标 `invalid_reference_encoder_format`，invalid_reason 字段原文注明），全部 ProRes 分数与两两对比无效 | 该组仅 Topos 侧单方数据可看；12-bit 对比一律改用 -match/VT 系列（正是重测原因） |
| 3 | `bench_out/topos_quality/`（2K 4444xq 行） | MAE≈14.3（p0 14.83 / p5 14.34），与同档 4444 的 4.1 及自身 PSNR 44.14 dB 严重不自洽，疑参考口径不含该档 | 该行的 MAE 不可引用、不可当质量退化证据；PSNR/SSIM 引用需附注（数据卡口径，基线亦然，同型异常） |

### 6.3 技术注记：2K 4444xq MAE 异常的定位（BENCH-0012，2026-09-29 追注）

**结论：主因是 MAE 单位域混排（指标口径缺陷），非参考帧 gamma/范围缺陷。** `tools/measure_topos_baseline_quality.py` 的 `mae_*` 以**档位原生码值域**为单位——4444 是 10-bit（峰值 1023）、4444xq 是 12-bit（峰值 4095），同量噪声在 12-bit 域的码值约放大 4×。上表把 4.12（10-bit 码）与 14.34（12-bit 码）并排直比即得出"异常"；归一化后档位单调性成立（xq 优于 4444）。

本机复核证据（2026-09-29，母版 `4K_prores4444.mov` 实测为 yuv444p12le 12-bit，6 抽样帧 2K 口径）：

| 档位 | 域 | MAE_y（码） | 归一化 MAE（/峰值） | 平均符号偏差 bias（码） |
|---|---|---:|---:|---:|
| 2K 4444 | 10-bit | 3.948 | 0.00386 | +0.026 |
| 2K 4444xq | 12-bit | 13.34 | **0.00326** | −0.074 |

两条假设的核验记录：

1. **参考帧 gamma/范围不一致（证伪）**：若参考链存在 limited/full 范围错配，逐帧误差会出现几十码量级的系统性符号偏差；实测 bias 为 +0.026 / −0.074 码（≈0），**范围/gamma 缺陷不成立**。母版本身是 12-bit，12-bit 参考链也不存在 10→12 位提升引入的舍入项。
2. **指标单位域缺陷（证实）**：见上表——归一化后 xq（0.00326）优于 4444（0.00386），与 PSNR/SSIM 的档位排序一致；"与自身 PSNR 不自洽"同样消失（RMSE 25.4 码 @12-bit，MAE/RMSE≈0.56，与 4444 行的 0.62 同量级）。

处置：`tools/measure_topos_baseline_quality.py` 已补跨域可比字段 `mae_*_norm`（误差/峰值）与 `mae_*_10bit`（10-bit 等效码），并保留原 `mae_*` 原生码值字段（旧结果可复算）；**既有 p0–p5 JSON 中的 mae 数值跨档（10↔12-bit）直接比较一律无效**，该行在 REGRESSION_BASELINE.md §10 #9 标为不可比-待重测。验证步骤（如需复算）：对 recal-p5 的 `psnr_*` 反推 RMSE=peak/10^(PSNR/20)，核对 `mae/RMSE` 比值与归一化排序同本注记一致。

---

## 7. 实测结论与推测分列

**实测（有文件级出处，见各表）**
1. 档位误差阶梯与 4K>2K 的分辨率效应（§1）。
2. 码率配平下 Topos 全档 Y-PSNR 领先 prores_ks（§3.1）。
3. 真实 CFA raw16 六档压缩/误差曲线及与合成矩阵的量级差（§4）。
4. 12-bit 映射误差上界：Y ≤1 code、U/V 精确；v1=v2 逐位复现（§5）。
5. 空间 AQ 在测试口径下三项全负（§1.2）；deblock 消块效应显著、PSNR 微升、解码 −5.7%（§2.2）。
6. 三处无效数据的失效原因均有文件自证（§6）。

**推测 / 待验证（引用须标注）**
1. p0→p2 质量跳变归因于档位重校准（数据卡推断，JSON 内无变更记录）。
2. 4444xq MAE 异常的"参考口径不含该档"归因（§2/§6.3）。
3. qgain 4K deblock 的 dec_fps 波动为非成对噪声（观察性描述）。
4. "ProRes 侧 35–37 dB 偏低是否部分源于 prores_ks 实现差异（vs Apple 编码器）"——尚无 Apple 侧同码率配平对照，仅有双解码器一致性（0.315 code）佐证解码侧。

---

## 8. 与其他组报告的边界

- 边缘锯齿取证（`edge_jag/`）在 G1 播放组；native 域合成矩阵与 float16 零值审计（`topos_native_domain_*`）在 G4；m8 的 PSNR/SSIM/多代质量在 G6。
- 跨编码器 Δ 画质（哈希目录 6d749586/d9552407/cbf6b308/77b91b28，同码率插值 −1.8~−3.9 dB、444 域 −5.8 dB 级）归 G7 杂项归类表，不在本组覆盖清单内；两组数字口径不同（96 帧矩阵 + 插值 vs 30 帧配平逐帧），不可互相校准。
