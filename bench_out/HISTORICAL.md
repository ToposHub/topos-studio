# HISTORICAL / NOT CURRENT（P2-08，2026-09-03）

本目录下除 `058d29da/`（P0-04 manifest 规则生效后的不可变结果树）之外的
全部原始数据均为**历史观察**，不得作为"当前性能/质量"结论引用：

| 目录 | 构建 | 口径 | 状态 |
| --- | --- | --- | --- |
| `codec_bench/` | dirty 工作树 | 混合口径 harness（详见其 report.md） | 历史观察（方向性参考） |
| `round2/` | `52a2dbf0` dirty | **含 P0-01 UAF 缺陷**（平面数组悬空） | 失效（见 round2/round2_invalidated.md） |
| `topos_vs_ref/`、`m8`/`m9`/`m10*`/`m11*`、`readahead/`、`davinci_context/` | 各期 dirty 工作树 | 各期脚本，无统一 manifest | 历史观察 |
| `058d29da/` | 以 git commit + dylib SHA + manifest 断言锚定 | P0-02 重写后的 core/E2E 双口径 | **当前有效**（新结果按 `<commit>/<dylib8>/<manifest8>/<utc>/` 追加） |

规则（审计 §0/§3，P0-02/03/04）：

1. 历史数据不删除、只追加失效/降级 metadata；
2. 相对结论（"快/慢 x%"）只有同 clean HEAD、同 dylib SHA、交替 A/B
   中位数 + bootstrap 95% CI 且区间不跨 1.0 时才可写；
3. dashboard/报告只允许读取含合格 manifest 的 raw。
