# RD4-07 scalable 策略选择

新增 `tc_base_policy_choose()` 和 `tc_base_policy_decision`。最长边不超过 2K 或不超过 4K 时选择 `band-only`；6K 及以上在满足明确 rollout/capability gate 后才允许 `scalable-fast`；`size-guarded` 仅当 scalable dry-run hint 不大于 band-only hint 时选择。选择完全由配置、几何和 dry-run hint 决定，不通过试解猜测。

`tc_base_policy_choose()` 是安全兼容入口，当前处于 `EXPERIMENTAL` 阶段并强制回退 `band-only`。只有 `tc_base_policy_choose_gated()` 同时收到公开 V7-B writer 能力和调用方选择的 rollout stage，才会启用 scalable；`DEFAULT` 还必须通过 size/speed 两道 gate。decision 记录 rollout stage 和 fallback reason，播放器/写入器可据此诊断为什么回退。

decision 同时记录 base 几何、是否启用以及当前已实现的布局能力。当前代码
阶段仍明确报告 `BASE_ONLY_PACKET`：RD4-08 虽已有内部参考 directory/mux，
但尚未接入公开 writer/reader 和容器 capability，因此不会伪报最终 V7-B；等
最终 residual mux 和容器 capability 接入后，调用方才可显式打开 opt-in，reader
仍按记录选择路径。
