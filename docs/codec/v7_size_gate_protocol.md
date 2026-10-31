# V7-A 体积决策门协议

本文对应实施计划 RD2-06，冻结 V7-A 标量 band 语法的第一道体积门。
它用于判断“可跳过高频段”带来的读取收益是否被目录、band map 和新段头
抵消；它不是画质或编码速度验收。

## 比较对象

- V2：53-byte frame header、每个 3 个 plane slice 的 17-byte slice header，
  加 canonical VLC slice payload。
- V7-A：53-byte frame header、固定 432-byte V7-A directory，加每个 plane
  的 B0..B4 scalar band payload。
- 两者使用完全相同的确定性量化 zigzag 系数。benchmark 不执行像素 DCT，
  因此结果只回答位流语法的体积问题。

## Corpus

native gate 固定生成四种内容分布：flat、low、texture、noise；每种分布
覆盖 1920×1080、3840×2160、6144×3456 和 7680×4320，共 16 个样本。
每个样本包含 3 个 YUV 4:2:2 plane，DC、band token 和 signed level 均由
固定 seed 生成，运行之间不依赖随机设备或输入文件。

## 指标和门限

对每个样本计算：

    delta_pct = (V7_A_packet_bytes - V2_packet_bytes) / V2_packet_bytes × 100

门限为：

- median delta ≤ +1%
- nearest-rank p95 delta ≤ +3%

百分位数采用有序样本的 nearest-rank 下标
floor((N - 1) × percentile / 100)，与现有 benchmark runner 保持一致。
目录占比、active-block map、pair-count map、padding 和各 band token 数作为
诊断字段单独记录，不能从总包大小中省略。

## 执行和报告

先构建 native gate：

    cmake -S native/topos_codec -B native/topos_codec/build/debug \
      -DCMAKE_BUILD_TYPE=Debug -DTOPOS_ENABLE_TESTS=ON
    cmake --build native/topos_codec/build/debug \
      --target topos_v7_size_gate --parallel 4

再运行可重复报告：

    python3 scripts/bench_topos_v7_size_gate.py \
      --executable native/topos_codec/build/debug/topos_v7_size_gate \
      --output docs/codec/v7_size_gate_2026-09-08.json \
      --markdown docs/codec/v7_size_gate_2026-09-08.md

JSON 的 schema_version 为 1，必须包含 environment、scope、gates、
diagnostics 和 samples。Markdown 只做人工阅读；CI 和后续策略选择读取
gates.pass。

## 决策语义

通过只表示 V7-A 在该确定性语法 corpus 上没有明显体积惩罚。它还不能证明
真实相机素材、画质、编码 p50/p95 或 V7-B 的 2K base 目标，因此 V7-A 仍不
自动成为默认 writer；下一步需继续做真实素材和热路径门禁。

