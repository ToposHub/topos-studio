# A2 回归重跑：缓存格式 (gbrap12a16) + CompositePage 变更后矩阵复验

- 日期：2026-09-19 14:10；分支 chore/edit @ 1f7111d4
- 背景：节点磁盘缓存载荷格式修复（float→uint16、alpha 无损 16-bit、
  FORMAT_ID 诚实化）与 CompositePage 帧就绪蹦床/minimap timer 变更后，
  按计划重跑两大矩阵确认零回归。

## 一、93 节点矩阵（bench_all_composite_nodes_real_topos）

```
registered=93
by_status={"blocked_missing_model": 4, "blocked_missing_resource": 13,
           "passed_nonflat": 76}
```

与 T-P1-60 批次 D 基线逐类一致（4/13/76），零回归。
证据：real_topos_all_composite_nodes.json（本目录副本）。

## 二、W-matrix（bench_w_matrix_replay，W01–W08）

```
8/8 PASS（W01、W02、W03、W04、W05、W06、W07、W08）
```

证据：bench_out/ai_upgrade/W_matrix_20260919-141056/（上一轮
W_matrix_20260919-020619 同为 8/8，本轮证明缓存格式与 CompositePage
变更未破坏任何工作流语义）。

## 三、结论

- 磁盘缓存新格式（topos_gbrap12a16）对 93 节点渲染与 W05 图缓存语义
  （暖命中/失效）无回归。
- CompositePage 生命周期变更（弱引用蹦床、minimap timer 绑定方法）
  对八条真实工作流无回归。

## 四、T-P1-22 20 轮 RSS +933MB 分类（60 轮确认，2026-09-19）

协议同 T-P1-71（slope 阈值 5 MB/轮 + 尾段增量单调性）：

- 20 轮原序列：+933MB drift 由首轮爬升（590→3422，FFmpeg/PyAV 与三
  codec 各自首用高水位）叠加振荡主导，全序列斜率 78 MB/轮被爬升污染。
- **60 轮复跑**（bench_out/ai_upgrade/T-P1-22_loop_20260919-143012/，
  60/60 OK + cancel probe 通过）：爬升后（6..59，n=54）
  **slope 3.31 MB/轮 < 5 阈值，净增 +119MB**；
  10 轮窗口增量 +129/-130/-497/+521/+19 MB——大幅振荡、无单调趋势；
  尾 10 轮中位 3489MB（σ=157）；h264/hevc/prores 三 codec 前后半中位
  差 +114/+58/+46MB，各自收敛。
- **定性：有界保留（BOUNDED）**——多 codec 轮转的分配器/解码器
  高水位振荡，非无界泄漏；与 TP-71-MEM-001（纯 H265 200 轮有界）
  同族。RSS 平台 ~3.4–3.9GB。
