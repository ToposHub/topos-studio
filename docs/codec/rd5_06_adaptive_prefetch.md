# RD5-06：自适应批量预取

## 已落地

- 播放预取窗口由固定 16 帧收敛为 2–4 帧：高帧率或较大 AUTO_2K 平面使用 2 帧，中等负载使用 3 帧，轻负载使用 4 帧。
- 首帧仍走即时解码；预取在后续调度中异步提交，不把整批成本压到首帧。
- 空闲缓存回填继续采用单帧 latest-only 入队，交互请求到达时后台任务让位，队列不会无限增长。
- `ToposMediaSource.diagnostics["read_ahead_stats"]` 暴露 batch 解码数、预取帧数、缓存命中、缓存丢弃和批量回退；渲染器通过 `playback_prefetch_diagnostics()` 暴露预取命中、填充、丢弃、提交、当前自适应深度和队列深度。
- Topos source 的显式 read-ahead 仍受既有 160 MiB 预算和硬上限保护；未显式开启时维持关闭，避免同步批量抬高首帧 p95。

## 验收

预取深度单测覆盖 24/60 fps、低分辨率和 4K 时间线；现有 Topos batch 测试同时核对 batch/cache hit 计数与逐帧 bit-exact 结果。
