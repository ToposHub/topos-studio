# RD7-03：V7 writer rollout 闸门

状态：**安全闸门与显式 opt-in V7-B writer 已实现；默认 rollout 仍关闭**（2026-09-08）。

`tc_base_policy_choose()` 现在是保守兼容入口：当前构建处于 `EXPERIMENTAL`，即使输入为 6K/8K/12K/16K，也不会因为内部 base-only primitive 存在而自动选择 scalable writer，统一回退 `band-only`。这样现有 V2 writer 与 V1–V6 reader 不受影响。

生成 scalable elementary packet 的调用方可显式调用 `tc_frame_encode_scalable()`；
通过策略选择器启用 scalable 产品策略的调用方仍必须使用
`tc_base_policy_choose_gated()`，并显式提供：

1. `TC_BASE_GATE_PUBLIC_V7B_WRITER`：公开 reader/writer 已能消费 V7-B directory + enhancement residual；
2. `TC_BASE_ROLLOUT_OPT_IN` 或 `TC_BASE_ROLLOUT_DEFAULT`；
3. `DEFAULT` 阶段还必须同时提供 `TC_BASE_GATE_SIZE_PASS` 和 `TC_BASE_GATE_SPEED_PASS`。

任意条件不满足都会返回 `TC_BASE_POLICY_BAND_ONLY`，并在 `fallback_reason` 中记录 `experimental`、`no_public_writer`、`release_gate` 或 `size_hint`。当前 `tc_query_capabilities()` 尚未置 `TC_CODEC_CAP_V7B_SCALABLE_BASE`，所以产品路径不能误启用该策略。

验收：`unit_base_policy` 覆盖安全默认、无 writer 回退、opt-in gate、default gate 和 size-guarded hint；RD4-08 的最终体积/编码速度门仍保持 BLOCKED，直到同质量 V7-B 体积/速度数据通过。
