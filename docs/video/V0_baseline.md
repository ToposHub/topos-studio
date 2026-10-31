# Topos 视频 V0 基线冻结

状态：V0 事实与实验矩阵冻结；跨帧压缩不是当前帧内中间编码的完成门槛。

当前调用链为：

```text
ToposEncoder / MOV writer
  -> tc_frame_encode[_sized]
  -> intra prediction / transform / quant / entropy
  -> independent TPIC sample
  -> MOV sample table

ToposMovieSession / decoder
  -> MOV sample lookup
  -> tc_frame_decode
  -> inverse transform / entropy decode
  -> frame planes
```

当前 profile 的可验证事实：每个视频 sample 都是独立 TPIC 帧包；当前路径没有
motion vector、参考帧、DPB、P/B 帧或跨帧 GOP 依赖。因而它支持低延迟顺序解码、
直接抽帧和按 sample seek，但不会复用相邻帧的静态背景或运动区域。

V0 实验矩阵固定记录 4:2:2/4:4:4、10/12-bit、2K/4K、24/30/60 fps，以及静态、
平移、快速运动、切镜、闪光、噪声、HDR 高光和 Alpha 边缘素材。每个结果必须包含
素材 SHA-256、profile/prediction/entropy/GOP 标识、文件字节、bits/frame、质量指标、
编码/解码 p50/p95/p99、首帧、seek、RSS 和线程数；机器可读字段见
`docs/video/benchmark_schema.json`。

当前与后续能力的边界：

| 能力 | 当前帧内 profile | 后续 inter profile |
| --- | --- | --- |
| 独立 sample | 必须 | 可选/按 capability |
| 参考帧、DPB、P/B、GOP | 不支持 | 单独 major/profile |
| 随机抽帧 | 直接读取 sample | 从最近 IDR 解码 |
| 错误恢复 | sample CRC/长度检查 | 增加 GOP/IDR 恢复 |
| 跨帧码控 | 不适用 | V5 单独验收 |

V0 不把“方向预测 only”“inter only”“方向+inter”“方向+inter+arithmetic”
混成一个默认实现。实验必须保留 all-intra 对照组；在没有版本、素材 hash、质量和
p50/p95 速度数据前，不得将结果写入能力清单或产品宣传。
