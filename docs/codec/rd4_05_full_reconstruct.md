# RD4-05 base + residual 完整重建

`tc_base_full_reconstruct_u16()` 和按 band 的 `tc_base_full_reconstruct_band_u16()` 固定了 V7-B 的完整输出顺序：base 先按 RD4-01 predictor 上采样，再以 signed residual 做 `int64` 累加，最后才按位深 clip。

坏层处理也在同一个入口冻结：`base_valid=0` 时整 band 使用调用方提供的中性值；`base_valid=1, residual_valid=0` 时只输出上采样后的 base 预测；两层都有效时输出完整重建。band 接口只写指定行区间，保留其它输出内存，适合逐 slice concealment 和并行调度。

单测覆盖 bit-exact 闭环、band 行区间、base 损坏、residual 损坏和有符号溢出 clip。
