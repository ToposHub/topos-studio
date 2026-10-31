# RD2-03：V7-A 单遍 token 分区器

状态：已实现（内部参考路径，尚未接入默认 writer）。

`tc_v7_band_partition_tokens()` 接收现有颜色 token 路径已经生成的
`DC + (run, level)` 序列，在一次 token 遍历中把 block-wide zigzag run
转换为 B0–B4 的 band-local `pair_count/local_run/level_m` 数组。

它不执行 DCT、量化或熵解码，也不分配内存；调用方提供 grow-only 存储，
因此后续 V7-A writer 可以直接把这些数组交给 `tc_v7_band_encode()`，而不
需要再次扫描 64 个量化系数或重做前向变换。B0 另外保留每块 DC mapped
token，B1–B4 只保留本频带的 AC token。

验收覆盖：

- 两个 block 横跨五个 band 的分区结果；
- B4 scan position 63 的最大 local run；
- 输入 token 数量不一致和非法 run 的拒绝；
- 每个 band 的 block order、pair count、local run 和 mapped level 保持稳定。

这一步只完成编码前的 token 组织；V7-A packet writer/reader 仍需由上层
负责目录、CRC 和目标 band 选择，V7-B 2K base 也不由此功能宣称完成。
