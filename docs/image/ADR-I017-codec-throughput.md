# ADR-I017：图片 codec 吞吐、并行和内存优化收口

## 状态

已接受（O4 当前交付，2026-09-08）。

## 决策

图片层沿用 native codec 已验证的优化路径：scalar 参考实现先行，随后使用
运行时 SIMD dispatch、slice 级有界并行、常驻线程池和 grow-only worker 缓冲。
Python/CLI benchmark 必须把线程预算真正传给 native 进程；`scripts/bench_topos_image.py`
的 `--threads N` 现在设置 `TOPOS_SLICE_THREADS=N`，`0` 保留库默认值，并在
报告中写入 `applied_to_native_subprocess`。

性能报告继续拆分 probe、validate、payload read、codec decode、色彩转换、缩放
和 WebP 编码；完整 native 内核热点使用 `topos_quality perf` 和现有 R6 报告，
不把预览 WebP 成本混入 codec decode。线程池失败、嵌套调用和 ring 回压通过
native telemetry 暴露，正确性优先退化到串行。

## 证据与边界

- 既有 native 证据：`native/topos_codec/docs/ADR-C009-stage9-simd-parallel.md`
  与 `ADR-C022-r6-perf-thresholds.md`，覆盖 SIMD、slice 并行、buffer 槽位、
  常驻池、golden parity、ASan/UBSan/TSAN 和 p50/p95 对照。
- 本次 benchmark 回归：release `toos`、GBR10 golden、2 iterations、
  `--threads 1`，JSON 通过 `scripts/validate_benchmark_report.py`；测试同时
  锁定 threads 环境变量确实传入 native subprocess。
- 当前 Python image binding 仍为 caller-owned numpy plane；跨帧平面池化暂不
  引入，因为所有权和旧帧并发消费协议尚未能证明收益高于复杂度。
- 由于当前 native image ABI 仍输出完整平面，O3 的后缩放不能计入 native RSS
  降低；这项收益必须由后续 native target-size/ROI API 单独测量。

## 验收命令

```text
python3 -m pytest -q tests/test_topos_benchmark_protocol.py \
  tests/media/test_topos_image_source.py \
  tests/file_explorer/test_topos_image_preview.py
python3 scripts/bench_topos_image.py \
  native/topos_codec/tests/conformance/golden_image_v1_gbr10.bin \
  --toos native/topos_codec/build/release/toos --iters 2 \
  --threads 1 --build-type release --output /tmp/topos-image-o4.json
python3 scripts/validate_benchmark_report.py /tmp/topos-image-o4.json
```
