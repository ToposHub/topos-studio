# RD4-08：V7-B scalable base + residual 参考封装

本项加入第一个可执行的 V7-B 参考封装，用于验证 RD4 的数据流和读取边界。
它已通过显式 opt-in `tc_frame_encode_scalable()` 暴露，但不是默认 writer，也
没有置位公开 `v7b_scalable_base` capability。

## 参考布局

- 外层仍使用 V7 major `7.0` frame header。
- `TPLD` directory 使用 `1.1`，固定包含两层：`layer 0` 为 base，`layer 1`
  为 residual。
- base 层只有一个 segment，内容是一个可独立解码的 legacy Topos frame
  packet，几何由 `tc_base_frame_prepare()` 限制在 `max_dim` 内。
- residual 层按 plane 生成一个 segment。segment payload 以 `RSD1` 和两个
  Rice 参数开头，后面是 `tc_base_residual_encode()` 的 signed residual
  pair stream，末尾 zero-pad 到字节边界。
- 当前参考实现拒绝 alpha；YUV 三平面是唯一可编码布局。

## 读取语义

`TC_V7B_DECODE_BASE_ONLY` 只校验并解码 base segment，不读取 residual 的 CRC、
熵流或重建；诊断结构记录跳过的 segment 数和字节数。完整模式才校验并解码
全部 residual，再用 `prediction + residual` 重建源平面。

单元测试还验证：只破坏最后一个 residual 字节时，base-only 仍成功，而完整
模式返回 `TC_ERR_CHECKSUM_MISMATCH`。这保证未来预览路径不会因为增强层损坏
而被迫触碰增强层。

## 明确的发布边界

这是 RD4-08 的 reference mux，现已通过 `tc_frame_encode_scalable()` 提供显式
opt-in 公共编码入口，但不等于默认协议 rollout。当前仍缺少：

- 面向最终质量预算的 base/residual 联合 writer 调参和 size/speed gate；
- 完整 elementary-frame、reduced/AUTO_2K one-shot、持久 decoder context、全
  V7-B batch 共享线程池和 MOV sample 读写均已接入；混合 batch 仍安全串行，且
  尚未形成默认 capability 承诺；统一 telemetry 也已记录外层跳段数据；
- capability manifest 置位和默认 rollout；
- alpha、跨帧预测以及最终发布格式的兼容承诺。

在上述门禁完成前，旧 V1–V6 路径和已公开的 V7-A one-shot 路径保持不变，
策略选择器继续回退到安全路径。
