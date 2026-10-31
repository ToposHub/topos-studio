# RD3-04 跨帧任务图验收记录

日期：2026-09-08

RD3-04 的核心实现已由 `8fc9161c` 提前落在当前分支：入口先完成各帧结构解析和准备，把所有有效帧的 slice 展平成一个任务数组，再由一次 `tc_parallel_for` 统一领取。这样播放器批量预取不会为每一帧再开一个线程池，也不会出现 worker 内嵌套派发。

## 本次验收

`test_codec` 新增 8/16 worker 两档断言，使用 3 帧混合输入：

| worker 上限 | 每次批量池批次数 | nested fallback | 输出 |
| ---: | ---: | ---: | --- |
| 8 | 1 | 0 | 与逐帧参考逐位一致 |
| 16 | 1 | 0 | 与逐帧参考逐位一致 |

复现命令：

```text
cmake --build native/topos_codec/build/debug --target test_codec --parallel 4
native/topos_codec/build/debug/test_codec
```

既有阶段 4 的批量性能证据保存在 `bench_out/m11_stage4/20260902_stage4_decode_batch.txt`：2K 纯内核 390.6→429.4 fps（+10.4%），4K 98.5→103.4 fps（+5.2%）。这些是阶段 4 的历史基准，用于证明调度形态；本记录不把它们冒充为 RD3-04 当日重测结果。

错误和取消语义仍按现有契约处理：帧内 slice 错误在该帧 finalize 时 conceal/返回，邻帧继续完成；结构解析失败在进入任务图前拒绝整批。
