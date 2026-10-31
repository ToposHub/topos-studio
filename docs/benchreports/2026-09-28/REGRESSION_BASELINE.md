# REGRESSION_BASELINE（计划05 P3：回归基线，2026-09-28）

> 仓库根 = `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。数据出处均为仓库根相对路径。
> 输入：`docs/benchreports/2026-09-28/` 全部数据卡与 7 份主题报告；必要时回查 `bench_out/` 原始文件（只读）。
> 有效性总纲（`bench_out/HISTORICAL.md` P2-08）：除 `058d29da/` 结果树外均为历史观察。本基线从中挑选**应长期稳定的锚点**：优先选 (a) 有 manifest/SHA 锚定的、(b) 确定性可逐位复现的、(c) 结构性（比值/布尔/语义）而非绝对时间的指标；绝对时间类锚点一律给出较宽漂移带。
> **禁引注记（BENCH-0006，2026-09-29 追加）**：上文 `058d29da/` 的"结果树"身份仅指其作为 P0-04 不可变结果树的**结构根**，**不代表其数字可引用**——它与 `9a8bfd99/` 同族脚本的两份结果互为"翻盘"且无锚定参照，双双禁引，技术闭环见 §10 表后"BENCH-0006 禁引技术闭环注记"。
> 机器换算警告：全部 fps/ms 锚点默认基准机为 i9-9900K（8C16T）类主机；跨机器复测（M1 / i9-12900T / 黑苹果）绝对值不可比，须先重跑一轮建立本机基线再启用漂移带。

---

## 0. 锚点总览

共 20 条（ANC-01 ~ ANC-20），分七组：D1 播放解码（readahead-off 口径）、真实素材解码、readahead/arena 增益、native 域矩阵、timeline_occlusion、codec 六档贴靶、paired q01 画质配对、线程 p95、附加强锚点（12-bit ramp / RD 确定性 / 质量阶梯 / E1 编码边界）。

| ID | 名称 | 当前值 | 漂移带（超出即查） | 复测脚本 |
|---|---|---|---|---|
| ANC-01 | D1 2K proxy 顺序解码 | 702–716 fps（p50 1.345–1.363 ms） | fps ≥ 620（−12%）且 p50 ≤ 1.55 ms | `tools/bench_topos_playback_path.py` |
| ANC-02 | D1 2K hq 顺序解码 | 248–277 fps（p50 3.530–3.716 ms） | fps ≥ 220 且 p50 ≤ 4.3 ms | 同上 |
| ANC-03 | D1 4K proxy 顺序解码 | 171–204 fps（p50 4.887–5.252 ms） | fps ≥ 150（p99 单跑尖峰 25.7 ms 属已知噪声） | 同上 |
| ANC-04 | D1 4K 4444xq 顺序解码 | 39.5–42.8 fps（p50 23.24–24.61，p99 29.2–36.6 ms） | fps ≥ 35 且 p50 ≤ 27 ms 且 p99 ≤ 40 ms | 同上 |
| ANC-05 | D1 4K 4444xq RSS 峰值 | 730–746 MiB | ≤ 800 MiB（其余素材 83–146 MiB 基线不变） | 同上 |
| ANC-06 | 2K 真实素材全片解码 | 380.69 fps（2.627 ms/帧，1572 帧） | ≥ 320 fps（−15%） | **建议补基准脚本**（见注） |
| ANC-07 | readahead/arena 成对增益 | 2K +10.5% [+7.8, +15.8]；4K +11.7% [+7.4, +14.9] | 任一分辨率增益 < +5% 即回归 | `tools/bench_topos_readahead.py` |
| ANC-08 | native raw12-2（合成） | 压缩比 1.55× / PSNR_min 108.4 dB / max_abs 1 code | max_abs ≤ 1、PSNR ±1 dB、压缩比 ±5% | `tools/measure_topos_native_domains.py` |
| ANC-09 | native raw16-2（合成） | 1.48× / 96.8 dB / max_abs 3 code | max_abs ≤ 4、PSNR ±1.5 dB、压缩比 ±5% | 同上 |
| ANC-10 | native float16-ultra | 2.84× / max_half_ulp 2 | ulp ≤ 3、压缩比 ±3% | 同上 |
| ANC-11 | float16 零/特殊类语义 | 全档 source_nonzero_to_zero=0、special_class_changes=0；ultra zero_sign_changes=1 | 恒等 0（任何非 0 即语义回归） | 同上（audit 变体） |
| ANC-12 | 真实 CFA raw16 六档 | raw16-2 2.54×/96.9 dB/max_abs 5；raw16-16 102.4×/60.8 dB/max_abs 889 | 压缩比与 dB ±15%（素材熵敏感） | 同上 + crop manifest |
| ANC-13 | timeline_occlusion 4K B1 | speedup 3.67×（cull_on 11.53 vs off 3.14 fps）、帧请求 140→70、culled_ratio 0.5 | speedup ≥ 2.5× 且帧请求比精确 2:1 | `tests/benchmark/benchmark_timeline_occlusion.py` |
| ANC-14 | codec 六档体积比 T/P | proxy 3.79 / lt 1.46 / std 1.52 / hq 1.29 / 4444 1.03 / xq 1.36 | ±10%；QP63 地板超支 1.53×/1.40×/1.67× 结构性保持 | `tools/bench_real_bitrates.py` |
| ANC-15 | paired q01 配平画质差 | proxy +7.53 … hq +17.88 … 4444xq-vt-12bit +27.21 dB；码流比 0.988–1.004 | Δ 下限 ≥ +5 dB、码流比 ±3% | `tools/measure_topos_paired_quality.py` |
| ANC-16 | 4K XQ 线程 16T p95 | 41.63 ms（12T 43.49 / 8T 48.23） | p95 ≤ 50 ms 且 12→16 收益 < 5% | `tools/bench_encode_stages.py` |
| ANC-17 | 12-bit ramp 码值映射 | Y MAE 0.150 / max 1 code；U/V 精确 0 | 逐位复现（SHA 一致）或 MAE ≤ 0.2 / max ≤ 1 | `tools/probe_prores_vt_12bit_ramp.py` |
| ANC-18 | RD 确定性位流 | 2K V2@qp45 = 245.7 Mbps / PSNR-Y 74.76 dB（两目录逐位一致） | 逐位一致；任何漂移 = 位流变更（非噪声） | 疑似 `tools/bench_rd_baseline.py`（见注） |
| ANC-19 | 质量阶梯（recal-p5） | 4K hq 55.26 / 4444 60.32 / 4444xq 65.81；2K hq 43.01 dB | ±0.5 dB（档位重校准会整体移位，须随 retier 更新） | `tools/measure_topos_baseline_quality.py` |
| ANC-20 | E1 clean 4K hq t16 wall | 15.87 ms/帧（native 13.71 + mux 1.53）；t1 110.6（t1→t16 约 6.5–7×） | ±15%；mux 段维持 1.4–4.9 ms/帧区间 | `tools/bench_encode_stages.py` |

机器可读版本：`REGRESSION_BASELINE.json`（字段 name/value/tolerance/source/note，与本表逐条对应）。

> **锚点来源登记注记（BENCH-0017，2026-09-29 追注）**：锚点级数据的 manifest 覆盖缺口已闭环为可审计登记——`tools/check_regression_anchors.py` 逐锚点解析 source 字段（分号/中文括注/花括号/glob 均支持）、对每个来源文件记 SHA-256 并分类：`p04_manifest_tree`（bench_out 结果树内且文件含 bench_manifest.v1，抄录 git_head/git_dirty/dylib8）或 off-tree 历史观察（`bench_out_off_tree` / `repo_benchmarks_off_tree` / `other_off_tree`）；**任何来源文件缺失即该锚点标 `missing` 并 exit 1**（完整性门禁：底层数据被移动后基线不得继续被当有效引用）。首跑审计（本注记日）：20/20 锚点来源完整；来源文件 43 个中 **22 个在 P0-04 manifest 树内**（d1_playback 全部、e1_encode ANC-16 dirty 试点 + ANC-20 clean、ANC-18 两棵根树），**21 个树外无锚定**（readahead 文本、real_2k、native 域矩阵/audit/真实 CFA、timeline_occlusion、real_codec_bitrate、paired_q01、thread_refine 汇总、12-bit ramp、quality_recal——即 cards-group2/4/5 各可比性备注所指的全部弱锚点）。登记产物落 `bench_out/anchor_registry/<utc>/anchor_registry.json`（schema=anchor_registry.v1，含 baseline json 自身 SHA）；`--dry-run` 只打印不写盘。**复测纪律**：锚点复测产物必须落 `bench_out/<head8>/<dylib8>/<manifest8>/<utc>/`（族前缀树同构）并重跑本登记刷新 SHA——树外来源的锚点（§10 已列的不可比行）在重建为 manifest 树数据之前，维持"历史观察"引用级别。

---

## 1. D1 播放解码边界（readahead-off 口径，ANC-01 ~ ANC-05）

- **口径**：`ToposMediaSource read_next_frame/read_frame → CPU planar frame`，排除 GPU 上传/调色/合成/呈现；`threads=16`、`read_a_head_requested=0`、`repeats=2`、`random_seeks=0`、96 帧（各 result.json `config` 字段实测核对）。
- **数据出处**（两个 clean 构建各两轮）：
  - 2K proxy：`bench_out/d1_playback/b5f4520a/fb0712a5/b98ca792/20260915T052659309166Z/result.json`、`bench_out/d1_playback/b61cabf4/9a36f61b/a17fde9f/20260915T051544736733Z/result.json`
  - 2K hq：`bench_out/d1_playback/b5f4520a/fb0712a5/802c3a7b/20260915T052713822871Z/result.json`、`bench_out/d1_playback/b61cabf4/9a36f61b/23a984a1/20260915T051546347499Z/result.json`
  - 4K proxy：`bench_out/d1_playback/b5f4520a/fb0712a5/db53552b/20260915T052722129114Z/result.json`、`bench_out/d1_playback/b61cabf4/9a36f61b/17ee5b38/20260915T051548268191Z/result.json`
  - 4K 4444xq：`bench_out/d1_playback/b5f4520a/fb0712a5/a0314c95/20260915T052735146059Z/result.json`、`bench_out/d1_playback/b61cabf4/9a36f61b/8fedcb41/20260915T051555356217Z/result.json`
- **漂移带依据**：两 clean 构建（b61cabf4 / b5f4520a）间的自然波动——2K proxy ±1%、2K hq ±6%、4K proxy ±9%（172→203 属波动上界）、4K 4444xq ±4%；叠加 `readahead/` 文档自述的跨会话机器漂移 ±5%。漂移带按"自然波动上界 + 约 5 个百分点余量"设定。
- **复测命令**（脚本头部示例 + D1 实际 config 还原）：
  ```bash
  TOPOS_CODEC_LIB=<dylib> .venv/bin/python tools/bench_topos_playback_path.py \
      --input tests/test_videos/4K_topos_4444xq.mov \
      --frames 96 --repeats 2 --threads 16 --read-ahead 0 --random-seeks 0
  ```
- **注意**：D1 数字是 **readahead 关闭态**；与 readahead 开启态（ANC-07）协议不同，不可数值复合（playback.md §3 注）。

## 2. 2K 真实素材全片解码（ANC-06）

- **口径**：2K_h264.mp4（1920×1080/25fps/1572 帧）转 TOPOS proxy 后**全片顺序解码**；120 帧三方对照中 topos native 335.6 fps（p50 2.68 ms）、wrapper 135.6 fps（p50 6.12 ms）。
- **数据出处**：`bench_out/real_2k_h264_20260907/result.json`（decode / decode_comparison 段）、`report.md`。
- **漂移带**：≥ 320 fps（−15%）。依据：同目录 120 帧口径与 p0_runner 同档稳态（2K proxy 1.351 ms）交叉一致，波动主要来自构建差异。
- **⚠ 弱锚点标注**：该数据使用工作区 Release dylib（库路径指向 `topos-color02` 工作树），无 commit/dylib SHA 锚定。首次复测若偏差 > 15%，应先以新构建重建基线再判回归。
- **复测脚本**：**建议补基准脚本**——产物内无命令行记录；`tools/` 中最近似的是 `tools/bench_playback_decode_compare.py`（VT vs FFmpeg 两臂），与原 4 臂对照（FFmpeg/VT/wrapper/native）不符。建议按 result.json 字段补一个官方脚本并纳入 P0-04 manifest 树。

## 3. readahead / arena 增益（ANC-07）

- **口径**：产品路径成对差分（相邻成对交替、逐对 fps 比值中位数 + 区间），官方空机窗口。
- **数据出处**：`bench_out/readahead/20260902_stage4_arena_batch_read.txt`（最终官方窗口：2K 324.0→362.4 fps、4K 86.6→98.3 fps）；批量预取单独收益 `20260902_stage4_product_readahead.txt`（2K +24.3%）。
- **漂移带**：增益 ≥ +5%（原文区间下界 7.4–7.8% 扣除跨会话漂移 ±5% 后的保守线）；增益转负或 < 3% 即触发回退线语义（历史 3% 门）。
- **复测命令**：`.venv/bin/python tools/bench_topos_readahead.py --pairs 12`（脚本头部注明"阶段4 兑现的官方复现脚本"，默认素材即原基准码流）。
- **注意**：该脚本 on 臂 = readahead 自动 + 批量解码（arena 语义已并入当前代码）；与 09-02 逐字对账时须注意优化已叠加。t8/t16 结论（2K 打平、4K t16 +2%）为静机口径，复测须空机。

## 4. native 域矩阵（ANC-08 ~ ANC-12）

- **口径**：native codec CLI（toos）合成 128×128 输入域往返矩阵 + 真实 CFA 裁切；每份 JSON 自带源/CLI/测量脚本 SHA 与 encode/decode 命令行（复测命令直接取自 JSON）。
- **数据出处**：
  - `bench_out/topos_native_domain_matrix/raw12/native_domains_raw12.json`、`raw16/native_domains_raw16.json`、`float16/native_domains_float16.json`（toos SHA 06508c5c…/4b8e5366…）
  - `bench_out/topos_native_domain_float_zero_audit/native_domains_float16.json`（toos SHA 333b7df3…，字节与 matrix finite 版逐档一致）
  - `bench_out/topos_raw_real_cfa_probe/raw16_six_tiers/native_domains_raw16.json`（真实素材，crop SHA 03961f03…）
- **漂移带依据**：v1/v2 逐位复现（12-bit ramp）与 float16-finite 与 float16 编码字节逐档相同说明该链路**确定性**；合成档给窄带；真实 CFA 档因素材熵敏感放宽到 ±15%，且只能与同 crop SHA 复测比较。
- **复测脚本**：`tools/measure_topos_native_domains.py`；**注意 toos CLI 三代 SHA 混用**（06508c5c / 4b8e5366 / 333b7df3），复测前先固定 CLI 版本（BENCH-0016 起脚本支持 `--record-cli-sha [EXPECTED_SHA]`：把 CLI SHA 写入 result.json 并可断言匹配，影响面详见 native-threading.md §6 勘误 2 扩写），跨 CLI 版本的压缩比/误差变化属二进制演进而非回归（见 RECOMMENDATIONS BENCH-0016）。
- **语义红线**：ANC-11 的两个 0 是行为语义锚点（无 Inf/NaN 类变化、无非零塌缩为零）；ultra 档负零→正零（zero_sign_changes=1）是**已记录的预期行为**，不是回归。

## 5. timeline_occlusion 4K B1（ANC-13）

- **口径**：渲染层帧请求 wall 中位数，warmup 10 / measure 60 / **runs=2**；4K_H264.mov（c90cf62a5f99）+ 4K_prores422HQ.mov（7438f4962405）。
- **数据出处**：`benchmarks/timeline_occlusion_bench_result_4k.json`（B1 双轨全遮挡：cull_on 11.53 fps / off 3.14 fps / 请求 70 vs 140 / culled_ratio 0.5）。
- **漂移带**：speedup ≥ 2.5×（runs=2 无 CI，原文即按"观察值"表述，带宽放得比 3.67±20% 更宽）；**帧请求 140→70 与 culled_ratio=0.5 是确定性结构量，任何偏离即功能回归**。
- **复测**：`tests/benchmark/benchmark_timeline_occlusion.py`（tests/benchmark 内）。**BENCH-0015（2026-09-29）**：脚本已支持 `--runs N` + 每组 `speedup_ci95`（成对百分位 bootstrap）+ `occlusion_activated` 剔除组生效断言（无激活组时 WARNING，`--require-cull` 可 exit 1）——**ANC-13 复测须 runs>=3 并以 CI 取代观察值表述**。
- **排除**：2K 版（`timeline_occlusion_bench_result.json`）只有 B0 单轨组、culled_ratio=0，剔除机制未激活，**不构成 2K 锚点**（BENCH-0015 小样本自验显示 2K 素材含 B1 组即可得有效证据：speedup 3.05× CI95 [2.88, 3.20]；2K 锚点须重测建立）。

## 6. codec 六档贴靶精度与体积比（ANC-14）

- **口径**：1920×1080/25fps/96 帧，TOPOS vs ffmpeg prores_ks 同名 profile，同行同帧；码率为 MOV 实占。
- **数据出处**：`bench_out/real_codec_bitrate_20260907/report.md` 主表 + `result.json`（proxy/lt 行已逐字段核对）。
- **漂移带**：体积比 ±10%（同素材确定性编码，波动主要来自档位参数微调）；**QP63 地板超支**（proxy 实际/目标 = 0.9286/0.6051=1.53×、pro444 1.40×、extreme 1.67×）是内核压缩能力上限的**结构性表现**——若某次复测地板消失（qp < 63 或超支 < 1.2×）应视为熵内核代际变更，更新锚点而非报回归。
- **复测命令**（report.md 原文）：
  ```bash
  LIB="$PWD/native/topos_codec/build/real-bench-release/topos_codec.0.1.0.dylib"
  TOPOS_CODEC_LIB="$LIB" .venv/bin/python tools/bench_real_bitrates.py \
      --frames 96 --out-dir bench_out/real_codec_bitrate_<date> --lib "$LIB"
  ```
- **历史警戒**：proxy target_bpp 曾多次重定档（0.89 → 0.6019 → 0.64483 → 0.7448，出处 `bench_out/94b31cc8/…/p4_retier.json`、`bench_out/qgain/budget_2K_report.json`）。**任何 retier 落地后必须同步更新本锚点**，否则会持续误报。QP 量纲跨代不可对齐（09-03 的 63 地板 vs reexport 的 68–82 vs M1 的 32–78）。

## 7. paired q01 画质配对结论（ANC-15）

- **口径**：`2K_prores4444XQ.mov` 解出 30 帧真实素材，码率配平 ±2%（rate_matched_2pct=true），Topos 各档 vs ffmpeg prores_ks 同名 profile，Y-PSNR 30 帧均值；每臂含源/库/双 mov SHA 与完整 ffmpeg 命令行。
- **数据出处**：`bench_out/topos_paired_q01_real_source/{proxy,lt,standard,hq,4444-vt-12bit,4444xq-vt-12bit}-match/paired_*.json`。
- **漂移带**：全档 Δ ≥ +5 dB（实测最低 +7.53）；码流比 0.988–1.004 ± 3%。依据：合成微样本（`topos_paired_q01_synthetic`）与真实素材两套独立运行方向一致（+4.9 ~ +27.2 dB），量级稳定。
- **引用红线**：只取 `-match` 与 `-vt-12bit` 组；非 match 的 `4444xq/paired_4444xq.json` 自标 `invalid_reference_encoder_format`（12-bit 静默降 10-bit），**全部 ProRes 分数无效**。
- **限定**：参照为 ffmpeg prores_ks（非 Apple VideoToolbox）、源为有损解码素材——两条限定必须随锚点走。

## 8. 4K XQ 线程 p95（ANC-16）

- **口径**：E1 分段（输入准备→native→mux），16 测量帧 + 4 预热，3 轮轮内序平衡，git_head `1d30609f`（dirty）、dylib `7b0cc4d4…`。
- **数据出处**：`bench_out/topos_perf_thread_refine_3rounds.json`（`median_by_threads`：8T p95 48.23 / 12T 43.49 / 16T 41.63 ms）；底层数据 `bench_out/e1_encode/1d30609f/7b0cc4d4/4232ea12/20260928T01*/result.json`。
- **漂移带**：16T p95 ≤ 50 ms（5 轮扫描的轮间 p95 范围 35.40–47.95 ms 为自然波动依据）；**12→16 线程收益应 < 5%**（当前 +1.5%，若复测 > 5% 说明线程调度结构变化）。4K25 帧预算 40 ms：当前 p95 距预算差约 4%，本锚点的意义就是守住"不再恶化到击穿预算"。
- **复测**：`tools/bench_encode_stages.py`（E1 口径）。**不可**与 5 轮扫描（`docs/codec/evidence/TOPOS_2026-09-27_local_pilot/performance_4k_xq_thread_sweep_5rounds.json`，另一构建 c1245ec6/d5fb9ad5）的绝对值对账，两构建明令禁止相减。

## 9. 附加强锚点（ANC-17 ~ ANC-20）

- **ANC-17 12-bit ramp**：出处 `bench_out/topos_vt_12bit_ramp_probe_v2/vt_12bit_ramp.json`（引用以 v2 为准：脚本 SHA a97ab678… + ffmpeg 9.0.2）。v1/v2 三个 SHA 与逐通道指标逐位一致 → 该探针**逐位可复现**，是全库最强的回归锚点之一。漂移带：SHA 一致；不一致时 Y MAE ≤ 0.2 / max ≤ 1 code 才可接受为口径内变化。
- **ANC-18 RD 确定性**：出处 `bench_out/6b3dfe9d/24421afb/e5250099/20260903T110654Z/rd_result.json` 与 `bench_out/5bc83bef/8e335ef7/318dbea5/20260903T133743Z/rd_result.json`——两次不同构建、不同次运行，质量/码率字段**逐位一致**（2K V2@qp45 245.7 Mbps / PSNR-Y 74.76 dB），证明确定性编码器。任何复测漂移都意味着位流语义变更，应触发人工判读而非按噪声处理。注意 PSNR=Infinity 是非标准 JSON 字面量，机器解析需处理。复测脚本疑似 `tools/bench_rd_baseline.py`（P2-01 RD 扫描，名称对应但原始产物未内嵌命令行，参数须按 manifest 复原——建议补基准脚本确认）。
- **ANC-19 质量阶梯**：出处 `bench_out/topos_quality/quality_recal-p5.json`（stride 5 / 99 帧，2026-09-28）。漂移带 ±0.5 dB。**历史警戒**：p0→p2 之间发生过档位重校准（2K proxy +4.29 dB），跨重校准不可比；2K 4444xq 行的 MAE（≈14.3）是**已知无效字段**——BENCH-0012 已定位为 10/12-bit 码值域混排（quality.md §6.3 技术注记；归一化后档位单调性成立），旧 mae 跨档禁比、该行待用新字段（`mae_*_norm`/`mae_*_10bit`）重测；PSNR/SSIM 可引用但须附注。
- **ANC-20 E1 编码边界**：出处 `bench_out/e1_encode/44784357/d052edae/2709a8be/20260927T120446882365Z/result.json`（clean 构建）。t1→t16 约 6.5–7×（4K hq 110.6→15.9 ms/帧）。漂移带 ±15%。**注意**：7 个构建目录中 5 个 dirty，跨构建绝对值不可比，本锚点绑定 clean 构建 44784357/d052edae；复测后以新 clean 构建重建基线。

---

## 10. 因环境/口径不可比的数据（防未来误报回归清单）

复测时**严禁**与下列旧数据直接对账；若有人把它们当基线，会产生假回归/假进步：

| # | 数据 | 不可比原因 | 处置 |
|---|---|---|---|
| 1 | `bench_out/round2/`（整目录） | P0-01 UAF 缺陷（`round2_invalidated.md`），输出与统计不可信；52a2dbf0 数字全部禁用 | 禁用，永不作基线 |
| 2 | `058d29da` vs `9a8bfd99` "速度翻盘" | 两次 dylib 构建自不同工作树（topos vs topos_1），参照版本无断言链；interpretation 标签语义当期不稳 | 禁止引用方向；058d29da 仅作结果树结构根 |
| 3 | `codec_bench/` 三批间（09-03 黑苹果 ↔ 09-16 M1） | 机器/口径双重不同（HISTORICAL 明标混合口径 harness） | 各批内部自比；跨批绝对值严禁 |
| 4 | m8/m9/m10*/m11*/m103/m104/readahead/davinci_context 等 dirty 历史树 | 各期 dirty 工作树、机器混杂（黑苹果/24 核掩码机/i9-9900K）、无统一 manifest | 只引用各自目录内 A/B 比值 |
| 5 | `threads_8v16.json`（i9-12900T 16C24T） | 物理核数与 8C16T 机不同，8→16 有 +52%/+70% 收益 | 仅作"线程结论强依赖物理核数"的反例 |
| 6 | e1_encode dirty 构建间（7 构建仅 2 clean） | git_dirty=true；两构建 8T 44.12 vs 39.43 ms | 只看构建内线程曲线 |
| 7 | 源路径指向旧仓 `topos_1/` 的目录（topos_real_bench_2026-09-13、topos_timeline_export_2026-09-13、topos_codec_stages、topos_same_vs_cross、extended_real_tests） | 与本仓同名素材连续性未验证 | 历史观察，不复测对账 |
| 8 | QP 量纲跨代（63 地板 ↔ reexport 68–82 ↔ M1 32–78） | QP 换算关系跨代未文档化 | QP 值不进任何锚点；只用地板布尔与 bpp 比 |
| 9 | `topos_quality` 2K 4444xq MAE 行 | 与自身 PSNR 严重不自洽（疑口径缺陷） | **不可比-待重测（BENCH-0012，2026-09-29）**：根因已定位为 MAE 单位域混排（4444=10-bit 域 / 4444xq=12-bit 域，跨档直比无效；gamma/范围缺陷已由实测 bias≈0 证伪，见 quality.md §6.3）。旧 mae 字段跨档禁比；`tools/measure_topos_baseline_quality.py` 已补 `mae_*_norm`/`mae_*_10bit`，**须用新字段重测重建后该行才可恢复可比** |
| 10 | `topos_paired_q01_real_source/4444xq`（非 match） | 参照编码器 12-bit 静默降 10-bit（自标 invalid） | 12-bit 对比只用 -match/VT 组 |
| 11 | `benchmarks/timeline_occlusion_bench_result.json`（2K 版） | 仅 B0 单轨组、culled_ratio=0，剔除未激活；1.027× 为噪声 | 不作 2K 剔除证据 |
| 12 | `p0_runner` run1 vs run2/3 | git commit 不同（7b13746e vs df8e2847）、repeats=1 无 CI | 仅锚点级参考；复测须重建 |
| 13 | `davinci_context/` | 非受控基准 + 用户侧实测数字 | 只作归因叙事 |
| 14 | `export_baseline/` | 无 git/dylib manifest、单次、三源帧数不一（480/240/120） | 仅同源内横比 |
| 15 | `p2_09` 两次运行（114756 vs 114837） | 口径不同（是否剔除 2 帧预热），单流 fps 差 2.7× | 采信 114837（warmup_excluded） |
| 16 | m8 sized 码率误差 −18~−21% vs m11 后 ±0.5% | RC 已重写换代 | 不混用；贴靶锚点只用 ANC-14 当前口径 |
| 17 | m104_pipeline 前两轮 vs 后两轮 | 同目录同 commit 但窗口内代码演进（serial 63→114 fps） | 引用必须点名子目录 |
| 18 | d1_playback 1459cef4（dirty，16 帧单轮） | dirty + 样本小 | 仅作方向旁证，不入锚点 |
| 19 | 哈希目录跨目录耗时（a986c3d9 vs 77b91b28 等） | 构建/PyAV 版本双重差异（"质量可比、速度不可比"） | 质量/码率行可锚（逐位一致），耗时行不可 |
| 20 | 真实 CFA（ANC-12）vs 合成矩阵（ANC-08/09）绝对压缩比 | 素材熵不同（102× vs 5.85×） | 分组呈现，禁止混排 |

**BENCH-0006 禁引技术闭环注记（2026-09-29 追注，对应上表第 2 行，原数据行未改动）**

针对 `bench_out/058d29da/37659ad5/a5b6fae6/20260902T234241Z/result.json` 与 `bench_out/9a8bfd99/58730875/f21fc30a/20260904T141812Z/result.json` 这对"速度翻盘"数据，本注记把禁引理由落成可核验的三条技术事实（证据链：`docs/benchreports/2026-09-28/ab-evolution.md` §4；数据卡组 1）：

1. **两 dylib 来自不同工作树**：`058d29da`（dirty，dylib SHA 前 8 位 `37659ad5`）构建自主仓 `topos` 工作树；`9a8bfd99`（clean，dylib `58730875`）构建自**独立工作树 `topos_1`**。两次运行之间没有 manifest 断言链能证明参照侧（ffmpeg/PyAV）版本一致——HISTORICAL 规则 2 的可比性前提（同 clean HEAD + 同 dylib SHA + 交替 A/B + bootstrap CI）不成立。
2. **interpretation 标签当期不稳**：`058d29da` 的 interpretation 字段全部写 "ref faster" 而 ratio_median < 1（topos 实际更快），`9a8bfd99` 全部写 "topos faster"——该标签语义当期不稳定，**两文件的 interpretation 均不得作为结论依据**；如需读数只允许直接引用 ratio+CI 原文。
3. **无锚定参照**：文件大小方向反转（058d29da topos 为 ProRes 的 1.84×，9a8bfd99 4K 反转为小于 ProRes）、2K core ratio 0.777 vs 0.861、帧数与档位范围不同（24 帧 2K 单档 vs 48 帧 2K+4K）——四处实测差异没有任何一条被锚定，方向冲突无法归因。

**处置（维持禁引）**：这对目录**禁止作为任何回归/翻盘/反超结论的依据**（含"topos 由慢变快"与反向叙事）；`058d29da` 仅作为 P0-04 不可变结果树的结构根有效，其数字引用须带 dirty 标注与"2K 单档 24 帧"的样本范围注。若未来确需该结论，必须按 §11 复测协议第 2 条重跑（同 clean HEAD + 同 dylib SHA + 交替 A/B + bootstrap CI），并先修复结果生成脚本：interpretation 由 ratio+CI 推导而非独立字段、ffmpeg/PyAV 版本写入 manifest（BENCH-0006 建议②，未落地前重跑结果同样不得引用 interpretation 标签）。

---

## 11. 复测协议建议（一句话版）

1. 每次复测先跑 ANC-17（12-bit ramp）与 ANC-18（RD 确定性）确认位流未变，再跑性能类锚点——性能漂移在位流未变时才可归因调度/环境。
2. 性能锚点超出漂移带时，先排除机器负载（readahead 文档自述跨会话 ±5%、一次 ASan 并发窗口曾作废整批数据），再按"同 clean HEAD + 同 dylib SHA + 交替 A/B + CI"复测一轮定案。
3. 任何 retier（档位重定档）落地后，ANC-14 与 ANC-19 必须同步重建。
