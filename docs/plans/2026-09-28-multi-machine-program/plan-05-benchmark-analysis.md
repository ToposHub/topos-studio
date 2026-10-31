# 计划 05：基准深度分析（Benchmark Atlas）

分支：`plan/05-bench` ｜ 性质：**只读分析**（可选用本机重跑验证） ｜ 写路径：`docs/benchreports/2026-09-28/`、`bench_out/e5_20260928_*/`（仅新增前缀目录）

## 1. 目标

`bench_out/` 已积累 75 项基准输出（播放、编码、codec、画质、native 域、A/B 对比等），但从未做过系统性交叉分析。本计划把这些原始数据提炼成：性能图谱（ATLAS）、可比对的回归基线、以及带证据的优化建议台账（BENCH-xxx）。呼应近期主线工作（4K XQ 线程扩展、codec 分层、RAW 验证）。

## 2. 仓库关键事实（已核实）

- `bench_out/` 顶层条目（75 个目录/文件），主要主题分组：
  - **播放**：`d1_playback`、`readahead`、`first_frame_repro`、`real_2k_h264_20260907`、`real_user_workflow_h264(_full)`、`edge_jag`
  - **编码/导出**：`e1_encode`、`m10_encode_probe`、`export_baseline`、`reexport`、`macos_gpu_export_20260915`、`metal_timeline_export`、`topos_timeline_export_2026-09-13`
  - **codec**：`codec_bench`、`real_codec_bitrate_20260907`、`topos_codec_stages`
  - **native 域/线程**：`topos_native_domain_matrix`、`topos_native_domain_float_zero_audit`、`topos_native_domain_smoke`、`m103_slice_rows`、`m104_pipeline`、`m1063_kernel`、`topos_perf_thread_refine_3rounds.json`
  - **画质对比**：`topos_quality`、`topos_vs_ref`、`topos_same_vs_cross`、`topos_paired_q01_real_source`、`topos_paired_q01_synthetic`、`topos_raw_real_cfa_probe`、`topos_vt_12bit_ramp_probe(_v2)`、`round2`、`qgain`
  - **A/B 基线**：`ab_baseline` vs `ab_dev`、`ab4k_baseline` vs `ab4k_dev`、`m8/m9/m11_stage*`、`ai_upgrade`、`p0_runner`、`p2_09`、`davinci_context`、`extended_real_tests_20260917`、`topos_anchor_matrix_20260917`、`topos_real_bench_2026-09-13`
  - 十个哈希命名目录（`01a7466d` 等）：需先读内容归类。
  - `HISTORICAL.md`（历史说明，先读）、样例媒体（`cpu_real.mov`、`gpu_real.mov` + manifest.json）
- `benchmarks/` 目录：`timeline_occlusion_bench_result.json` 与 `_4k.json`。
- 相关文档背景：`docs/benchmarks/`、`docs/codec/`、`docs/4k-playback-optimization.md` 等可作解读上下文（只读）。

## 3. 阶段划分

### P0 数据清单与元数据提取
1. 先读 `bench_out/HISTORICAL.md` 与几个 manifest，理解目录命名与产物结构。
2. 逐目录建立条目卡（fan-out：每子智能体负责 8–10 个目录）：产出时间（从内容/文件时间推断）、测试对象、关键指标、产物类型（json/log/媒体/图像）、与哪些目录同主题。
3. 汇总为 `docs/benchreports/2026-09-28/INVENTORY.md` + 主题分组修订版。

### P1 主题分析（fan-out 主体，每组一报告）
| 组 | 覆盖目录 | 报告 |
|----|---------|------|
| G1 播放性能 | d1_playback、readahead、first_frame_repro、real_2k_h264、real_user_workflow_*、edge_jag | `playback.md`：首帧延迟、读取策略收益、丢帧特征 |
| G2 编码/导出 | e1_encode、m10、export_baseline、reexport、macos_gpu_export、metal_timeline_export、timeline_export | `encode-export.md`：各 stage 耗时分布、GPU vs CPU、重导出成本 |
| G3 codec | codec_bench、real_codec_bitrate、topos_codec_stages | `codec.md`：码率-画质关系、分层结论，对照 docs/codec 近期决策 |
| G4 native 域与线程 | native_domain_*、m103/m104/m1063、thread_refine_3rounds | `native-threading.md`：线程扩展曲线、饱和点，对照 4K XQ 试点结论 |
| G5 画质 | topos_quality、vs_ref、paired_q01_*、raw_cfa、12bit ramp、qgain、round2 | `quality.md`：域误差水平、RAW/CFA 与 12bit 边界证据 |
| G6 A/B 演进 | ab(_4k)_baseline vs dev、m8/m9/m11、ai_upgrade、p0_runner、p2_09、anchor_matrix、extended_real_tests | `ab-evolution.md`：各阶段 A/B 收益表、回归信号 |
| G7 杂项归档 | 10 个哈希目录、davinci_context、benchmarks/timeline_occlusion | `misc.md`：归类结论 |

每组报告必须：说明每个数字的出处（文件路径）、区分"实测结论"与"推测"、用表格给出关键指标。

### P2 交叉汇总
- `ATLAS.md`：总图谱——主题 × 指标 × 时间线的矩阵；每主题 3–5 条"当前状态一句话"。
- `METRIC_DICTIONARY.md`：统一指标字典（指标名、单位、来源目录、测量口径），消除同指标不同口径的歧义。

### P3 回归基线
- `REGRESSION_BASELINE.md` + `REGRESSION_BASELINE.json`：从 anchor/ab_baseline 系列中选出"应长期稳定的锚点指标"（如 4K 播放 fps、首帧时间、典型导出耗时、画质域误差上界），每条含：名称、当前值、允许漂移带、数据出处、复测命令/脚本路径。
- 明确标注哪些旧数据因环境变化已不可比（防止未来误报回归）。

### P4 建议台账
- `RECOMMENDATIONS.md`：BENCH-0001 起编号，每条 = 观察到的现象 + 数据证据 + 假设 + 建议动作（测量类/优化类）+ 优先级。**只建议，不实现**——优化实现属于主线开发，修复类事项若属 01/02 计划领域则写入 `HANDOFF.md`。
- **收口**：同步产出 `FINDINGS.json`，把 `RECOMMENDATIONS.md` 全部条目结构化（id 前缀 `BENCH-`，type 取 perf-opportunity/risk/process；schema 见总纲第七节），条目数与 MD 文件对账一致。

### P5（可选，需 GPU 机器）本机重跑验证
- 只对 P3 选出的锚点指标做复测：找到产生这些数据的既有基准脚本（从产物中的命令行/manifest 反查，或 `tests/test_topos_benchmark_protocol.py` 线索），在**新目录** `bench_out/e5_20260928_<topic>/` 运行并记录。
- 环境差异（机型/系统/GPU 驱动）如实记录；重跑结果只用于校准漂移带，不覆盖历史结论。
- 找不到可复现脚本时：在报告写明"该锚点暂不可复现，建议补基准脚本"，列入台账。

## 4. 边界与禁止

- **只读**：`bench_out/` 历史内容、`benchmarks/`、`docs/`。绝对不修改、移动、删除任何历史基准产物。
- 允许写：`docs/benchreports/2026-09-28/` 全部内容；P5 的新 `bench_out/e5_20260928_*/` 目录。
- 不跑会写历史目录的脚本；不确定脚本行为时先读脚本源码确认写入路径，或干脆不跑。
- 分析中的统计推断（如外推趋势）必须标注方法与样本量，禁止把单次数据当结论。

## 5. 预估 token 消耗

75 项数据 × 逐目录解读 + 7 组主题分析 + 交叉汇总，一轮约 **3000 万 – 8000 万 token**；P5 重跑视脚本可复现程度另计。

## 6. 启动提示词（在本机器的 ZCode 新会话中粘贴）

```text
你是计划 05（基准深度分析）的执行者。请先完整阅读以下两个文件并严格遵守：
1. docs/plans/2026-09-28-multi-machine-program/README.md（全局铁律，特别是：bench_out 历史内容只读）
2. docs/plans/2026-09-28-multi-machine-program/plan-05-benchmark-analysis.md（本计划全文）

然后从阶段 P0 开始执行。要求：
- 确认当前分支是 plan/05-bench，不是就先创建并切换。
- 先读 bench_out/HISTORICAL.md 建立全局认知，再按 fan-out 机制逐目录建卡、分组分析。
- 每个数字都要能追溯到出处文件路径；实测结论与推测分开表述。
- 每完成一组更新 PROGRESS.md 并 git commit（前缀 [plan-05]）；P5 重跑环节前必须先确认脚本写入路径安全。
```
