# RD4-02 base 编码底座

RD4-02 新增 `tc_base_frame_prepare()` 和 `tc_base_frame_encode()`。它们把输入帧先按 RD4-01 的固定整数采样规则生成最长边不超过 `max_dim` 的独立 base 平面，再复用现有 Topos 帧编码器写出可独立解码的 base packet。

这一步的内存契约是：源平面始终由调用方持有；只有缩小后的 base 平面由适配层分配；源几何已经不超过目标时直接借用源指针，不复制。4:2:2 的 U/V 使用统一的 `ceil(luma_width / 2)` 几何，Alpha 按自身位深采样。

RD4-02 当前是 base-only packet 原语，不冒充最终 V7-B 容器。V7-A 的目录 writer 和 RD4-03 signed residual 会在后续步骤把 base 与 enhancement 合并；这样可以先独立验证 base 几何、采样确定性和内存上限。

测试覆盖 6K 输入到 2K base、8K/12K/16K 几何、4:2:2 plane 尺寸、≤2K 借用路径以及小帧 base packet 编码。
