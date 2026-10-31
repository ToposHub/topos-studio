# 视频 V2/V4 帧内实验收口

状态：已实现实验路径，未替换当前默认 profile（2026-09-08）。

## 计划与 native 位流映射

| 计划阶段 | native 实现 | 语法/能力 | 默认状态 |
| --- | --- | --- | --- |
| V2 方向预测 | TPIC major 3 / `--entropy intra` | 8×8 闭环空间预测、8 个模式、VLC 残差 | experimental |
| V4 自适应熵 | TPIC major 6 / `--entropy intra-range` | V3 预测残差 + 自适应二进制 range context | experimental |
| 当前中间片 | TPIC V1/V2 + canonical VLC | 独立 sample、无跨帧依赖 | default |

## 已完成的正确性门

- `native/topos_codec/docs/bitstream_spec_v3.md` 和
  `bitstream_spec_v6_range.md` 分别冻结版本、模式信令、截断和尾随字节规则；
- `conformance_codec_intra` 固定合法 V3 golden；`unit_intra` 覆盖模式/边界/重建；
- `unit_range` 覆盖 range coder 互逆与截断；`fuzz_intra_replay` 覆盖合法包变异；
- Release 重点 CTest：`unit_range`、`unit_codec`、`unit_intra`、
  `conformance_codec_intra`、`fuzz_intra_replay`，本次 5/5 通过。

## 发布边界

实验路径保留 all-intra 的独立 sample 语义，不能写入当前默认 capability，原因是
它们仍需在相同素材、线程预算和质量门槛下完成完整边际收益报告；当前 native
报告已记录 V3/V6 的包大小、质量和解码路径，但不足以宣称普遍码率收益。V3
方向预测不是计划 V3 的 inter/DPB；后者仍为未来产品线，当前没有 motion vector、
reference frame、P/B frame 或 GOP 依赖。

## 验收命令

```text
ctest --test-dir native/topos_codec/build/release --output-on-failure \
  -R 'unit_range|unit_codec|unit_intra|conformance_codec_intra|fuzz_intra_replay'
```
