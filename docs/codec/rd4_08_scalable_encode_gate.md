# RD4-08：V7-B 体积与编码速度门

## 当前状态

状态：**BLOCKED（显式 opt-in writer 已有；等待同质量门禁数据）**。

已加入 `topos_scalable_encode_gate` 和
`scripts/bench_topos_scalable_encode_gate.py`。工具对相同的确定性 10-bit
4:2:2 输入测量：

- V2：完整源分辨率 `tc_frame_encode`；
- V7-B：`tc_frame_encode_scalable()` 生成的完整目录、base 和 enhancement
  residual 包；
- 每个分辨率输出原始样本，并计算 p50/p95 编码耗时。

默认 corpus 覆盖 3840×2160、6144×3456、7680×4320、12288×6912 和
16384×9216；`--quick` 提供 1920×1080、4K、6K 的快速开发回归。

## 不可误读的边界

当前代码已加入显式 opt-in 的 `tc_frame_encode_scalable()`：它把独立 base packet
和 signed residual segment 放入两层目录，并支持 base-only/full 往返读取；完整
模式已接入 public elementary-frame reader，reduced/AUTO_2K one-shot 也已接入
有界 base 读取，持久 context、含 V7-B 批次的跨帧线程池分支和 MOV sample 读写
也已接入；全 V7-B 批次按帧 job 并行，默认策略仍未打开，也没有经过最终
尺寸/速度门。当前参考 residual 对固定 QP base predictor 是无损的，但最终
联合质量预算仍未冻结，不能用当前数据宣称通过计划表的 `median ≤+1%`、
`p95 ≤+3%`、编码 `p50 ≤+5%`、`p95 ≤+8%` 门禁。报告会记录实测值并将总决策
保持为 `BLOCKED`，防止把未经质量预算的参考 writer 接入默认 writer。

## 用法

```bash
cmake --build native/topos_codec/build/release --target topos_scalable_encode_gate -j 8
python scripts/bench_topos_scalable_encode_gate.py --quick --iters 3 \
  --output docs/codec/rd4_08_scalable_encode_gate_YYYY-MM-DD.json \
  --markdown docs/codec/rd4_08_scalable_encode_gate_YYYY-MM-DD.md
```

RD4-08 真正解除 BLOCKED 的前置条件是：冻结与 V2 可比的联合质量预算，确认
V7-B 总包体和编码 p50/p95 通过门限，并补齐最终质量回归。当前 V7-B 格式和边界见
[`rd4_08_v7b_reference_mux.md`](rd4_08_v7b_reference_mux.md)。
