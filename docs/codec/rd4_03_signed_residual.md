# RD4-03 signed residual 参考路径

RD4-03 新增 `tc_base_residual_build_u16()` 和 `tc_base_residual_reconstruct_u16()`：编码端按固定 base 几何调用同一个 half-pixel、edge-clamp predictor，生成有符号 `source - prediction`；解码端按 `prediction + residual` 做有符号累加，再在最终输出边界执行 clip。残差不会先转换成无符号样本，因此负残差和高亮溢出不会发生环绕。

新增的 `tc_base_residual_encode/decode()` 使用现有有界 Rice pair grammar 作为 signed residual 的参考熵流，支持非紧 row stride，返回的 `k_level/k_run` 可直接作为后续 V7-B residual segment 的 header 参数。它不创建全尺寸预测副本，重建和残差生成均逐像素使用相同 predictor API。

这一步暂不宣称最终 V7-B packet 已完成：当前仅有 RD4-08 的内部参考
directory/mux，最终完整帧 writer、公开 reader 和 MOV capability 接线仍需通过
RD4-08/RD7 门禁；当前测试已覆盖负残差、非紧 stride、bit-exact
base+residual 闭环和 Rice round-trip。
