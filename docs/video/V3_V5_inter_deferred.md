# 视频 V3/V5：帧间产品线 deferred 决策

状态：当前 Topos 帧内中间编码不启动实现；该结论是计划允许的停止分支，不是
“已支持 P/B 帧”的替代说法。

## 为什么现在停止

当前 MOV 的每个 TPIC sample 都能独立 probe、decode、seek 和抽取为 `.toos`。
源码和测试没有 motion vector、reference frame、DPB、P/B frame 或跨帧 GOP
依赖。引入 V3 会改变错误传播、seek 回退、内存上界、抽帧成本和播放器调度，
不能把它作为 V2/V4 帧内实验的附带优化。

V5 同样不在当前 profile 启用：当前 `tc_frame_encode_sized` 是逐帧 QP 搜索，
没有 GOP 级复杂度、VBV/HRD、lookahead 或跨帧 AQ/RDO。当前帧内产品的 QP 地板
和码率边界已经在现有报告中冻结，不能用“跨帧码控”文档描述替代实际实现。

## 重新启动条件

只有同时具备以下输入，才创建独立的 inter 实验分支和新的 bitstream
major/profile：

1. 有真实素材的目标码率和体积收益需求，且 all-intra 对照组包含素材 hash、
   质量、编码/解码 p50/p95/p99、首帧、seek 和 RSS；
2. 先实现 I/P、短 GOP、IDR、有限 DPB 和损坏 sample 后的恢复边界，B 帧不进入
   第一版低延迟档；
3. 有 1000+ 帧长序列、随机 seek、反向、取消、坏帧和场景切换门禁；
4. V5 的 VBV、目标码率误差、瞬时峰值、temporal stability 和播放器实时性均
   有机器可读报告；
5. capability query 能明确区分当前 all-intra 与新 inter profile，缺能力时
   失败而不是猜测或静默降级。

在这些条件满足前，当前发布语义保持“独立 sample、低延迟、可直接抽帧”的
帧内中间编码；不会宣称跨帧压缩、P/B 帧或 GOP seek。
