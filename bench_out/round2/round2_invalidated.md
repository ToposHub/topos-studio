# round2 基准结果失效标记（P0-01）

**失效日期**：2026-09-03
**影响范围**：本目录（`bench_out/round2/52a2dbf0_20260901_051525/`）及所有更早的
round2 产物。原始文件保留不删除（按证据链规则只追加失效元数据）。

## 失效原因

`tools/bench_codec_round2.py` 的 `_Kernel._ensure_planes` 存在 use-after-free
（P0-01）：预分配平面 `arr` 是循环局部变量，仅 `ctypes.cast` 出的裸指针被存入
`self.ptrs`，数组本体在循环重绑/函数返回后由 GC 回收。native
`tc_frame_decode` 随后向已释放内存写入：

- 解码输出平面内容不可信（可能被堆管理器复用破坏）；
- 吞吐 / p50 / p95 / p99 / RSS 漂移等全部统计不可信；
- 进程堆可能被破坏，跨测量项的噪声不可估。

## 修复

2026-09-03 起 `_Kernel` 增加 `self._plane_keep`，平面数组由实例持有到解码
生命周期结束；后续基准输出 hash 与安全 decoder（`ToposCodec.decode`）对拍
通过后方可重新采信。

## 规则

- 本目录所有数字**不得**用于任何优化 A/B 判断或对外表述；
- 修复后的基准需以新目录重新生成，manifest 必须含 tc_version()、dylib SHA、
  git commit（见 P0-04），并满足 200 帧 × 5 次 CV ≤ 3% 的验收门。
