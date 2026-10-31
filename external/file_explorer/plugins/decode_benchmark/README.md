# Topos Decode Speed Test

这是 file explorer 的解码跑分插件，入口在“插件 → 解码跑分 / Decode Speed Test”，也可在视频文件右键菜单中打开。

跑分线程复用 Topos 的 `SourceManager`、`MediaSourceFactory`、`VideoMetadata` 和代理/硬件解码策略，支持真实素材的：

- H.264、H.265 / HEVC、ProRes、DNxHR / DNxHD、AV1、VP9、VP8、MPEG-2、MPEG-4、MJPEG、FFV1
- 2K、4K、6K、8K（按探测到的实际宽高长边归类）
- 顺序播放或随机 Seek
- Proxy（SourceManager 离线代理 1920×1080）、Master（原分辨率）以及 Edit 实时管线三种模式
- 平均/P95 帧耗时、解码 FPS、实际 decode path、硬件解码器及 JSON/CSV 导出

能力矩阵按测试结果动态生成：只有已经完成有效测试的编码才会出现，
同一编码的 2K/4K/6K/8K 结果汇总在对应行，未测编码不会显示空行。

另外提供“当前 Edit 实时管线（宿主遥测）”模式。主窗口在 Edit 面板和
`TimelinePreviewRenderer` 就绪后注册 `EditBenchmarkHostService`，插件通过
`AppBridge` 发起一次受控的时间线播放测试。此模式不创建第二个解码器，也不从
Flask 路由触碰 OpenGL，而是直接使用现有
`RealtimePlaybackEngine.frame_presented`、`TelemetryAggregator` 和 GPU 合成统计，
因此报告的是当前时间线真正呈现的 FPS、解码/合成耗时、丢帧、source kind 以及
realtime invariant 状态，同时保存每帧实际 `decode_path` / `render_path` 分布（如
`macos_vt_zero_copy`、`gpu_composite`）。测试结束后会等待渲染 worker 的最后一批
trace 回填，再导出结果。

当前版本优先使用 `TimelinePreviewRenderer.frame_ready` 统计真正完成的异步输出；只有旧宿主
没有该信号时才退回 `RealtimePlaybackEngine.frame_presented`，报告会明确标记为“提交计数”。
Edit 跑分要求先暂停时间线，并从当前播放头开始顺序播放；若剩余时间线不足请求帧数，
结果会标记为“部分”，不会用循环或重复帧伪造完整样本。

如果主窗口尚未连接 host service，插件会保留 SourceManager 离线模式并提示“Edit
管线不可用”；这两类结果不能混为同一个 FPS 指标。

测试使用实际视频文件，不根据文件名伪造编码/分辨率，也不会自动生成“看似 8K”的缩放素材。建议分别添加各编码和分辨率的真实素材进行矩阵对比。
