# T-P1-71 缺陷立案：H265 导出循环 native 内存增长（TP-71-MEM-001）

- 日期：2026-09-18；状态：**已复现、已归因分层，native 根因待查**

## 复现（最小 reproduction）

进程内循环：`build_scenario("identity", hevc10_2K, h264, 4帧)` →
create_timeline_exporter → export_timeline → drop → gc.collect()。
20/30/60 循环均零导出失败、零 fd/线程增长。

## 测量

| 周期 | 瞬时 RSS (MB) | 增长/15 循环 |
|---|---|---|
| 15 | 776.6 | — |
| 30 | 847.8 | +71.2 |
| 45 | 908.3 | +60.4 |
| 60 | 950.9 | +42.7 |

减速但持续：~2.8MB/循环（第 60 循环时点）。

## 已排除（分层归因）

1. **Python 层零泄漏**：tracemalloc 快照差分（10 循环）= 0.0MB 保留增长。
2. 无 fd 泄漏（num_fds 4→3）、无线程增长（8 恒定）、
   TimelineExporter/ToposMediaSource 对象不累积。
3. GL 上下文/效果执行器每导出显式 release（release() 覆盖 EPR +
   ctx.release，两个 finally 路径均接线）。
4. 导出 clip 会话 LRU 正常关闭（close_clip → 引用计数 → 关源）。

## 结论与下一步

泄漏在 native 层：FFmpeg（PyAV 捆绑）解码/编码上下文或其缓冲池随循环
缓增。减速分量疑似有界缓存填充；稳态 ~2-3MB/循环指向小型 native 泄漏。
修复需 FFmpeg/AV 层排查（decoder/encoder free 路径、AVBufferPool 生命周期
）——按卡规则不在无根因时改核心架构。出口判据：60 循环瞬时 RSS 增长
< 0.5MB/循环。
