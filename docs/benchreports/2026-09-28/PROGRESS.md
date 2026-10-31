# 计划05 执行进度（PROGRESS）

> **状态：P0–P4 完成（2026-09-29）。**

| 阶段 | 状态 | 备注 |
|------|------|------|
| P0 数据卡片 | ✅ | 5 组 × 约 70 条目卡（cards-group1~5.md，含可比性陷阱与勘误） |
| P1 主题分析 | ✅ | 7 份报告（playback/encode-export/codec/native-threading/quality/ab-evolution/misc） |
| P2 交叉汇总 | ✅ | ATLAS.md（7 主题×40 指标族×7 时间分期）+ METRIC_DICTIONARY.md（86 条，含 11 组口径歧义） |
| P3 回归基线 | ✅ | REGRESSION_BASELINE.md/.json（20 锚点 ANC-01~20 + 20 条不可比数据清单） |
| P4 建议台账 | ✅ | RECOMMENDATIONS.md + FINDINGS.json（BENCH-0001~0017，对账一致） |
| P5 本机重跑 | ⛔ 未执行（可选阶段） | 原因：多数锚点无可直接复现的脚本入口（台账已标两处"建议补基准脚本"），且重跑需长时间占用编码/解码管线；留待第 2 轮在补齐基准脚本后执行。bench_out/ 历史数据全程零改动。 |

红线执行：全程未修改 bench_out/ 与 benchmarks/ 任何历史产物；未运行任何基准脚本。
