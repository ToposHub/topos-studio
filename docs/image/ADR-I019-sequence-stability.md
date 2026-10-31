# ADR-I019：图片序列长时稳定性与编辑器接线

## 状态

O6 当前交付已接受（2026-09-08，含 MediaSource/DecodeQueue 接线）。

## 决策

`.toos` 序列继续使用 `ToposImageSequenceSource` 的单预取线程、generation
cancel、实时文件指纹和按字节上限的平面 LRU。帧级并行固定为 1，slice 级并行
由 codec 的线程预算控制，避免序列预取与 native worker 叠加导致宿主过订阅。

图片序列导出沿用持久 Topos encoder context 和原子发布：每帧只写自己的临时
文件，`close()` 成功后才发布；取消或失败不会留下可被误认为成功的序列帧。
视频抽帧继续走 TPIC packet → TPIM envelope 的零重编码路径。

`ToposImageSequenceSource` 现在实现统一 `MediaSource` 读取面：播放序号从
0 开始并映射到实际磁盘帧号，`VideoMetadata.total_frames`、PTS、`DecodedFrame`
和 plane info 均按序列语义提供。`MediaSourceFactory` 对同目录下至少两个
numeric `.toos` 文件自动路由到该源；因此现有 UI/`DecodeQueue` 可以直接复用
普通媒体源入口，target-size 预览仍由 image binding 统一处理。

## 压力门禁

新增 1000 帧顺序、随机、反向和取消/预取收敛测试，使用小型 golden 文件，
并把 plane cache 预算压到只容纳一帧，验证缓存不会随序列长度增长。缺帧策略
仍严格区分 skip/error/hold，文件替换按实时 size+mtime 指纹失效旧缓存。

## 已知边界

- 非连续磁盘帧号仍按排序后的播放序号暴露；缺帧策略只作用于显式
  `read_slot`/预取路径，不伪造磁盘帧文件。
- NAS 冷缓存、跨平台 IO 尾延迟和 1000 帧的真实大分辨率 RSS 需要在发布设备上
  运行，不能用本地小 golden 冒充产品性能结论；报告列为 O9/V7 门禁。
