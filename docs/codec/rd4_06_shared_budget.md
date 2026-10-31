# RD4-06 shared size budget

新增 `tc_base_split_budget()`，把一帧总预算按 base/residual 的 dry-run size hint 一次性切分，输出字节数严格相加为总预算；hint 为空时使用确定性的 1:1 回退。新增 `tc_base_frame_encode_sized()` 复用现有 sized writer 编码 base-only packet。

这一步冻结的是预算与接口策略，不把最终 V7-B 的两层独立搜索提前实现成错误语义。V7-B mux 接入后，base 和 residual writer 将共同消费这一份预算，QP probe 由上层统一控制；当前测试覆盖比例分配、零 hint 回退、base-only sized 编码和 QP 范围。
