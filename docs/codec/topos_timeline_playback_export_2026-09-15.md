# Topos 时间线播放与导出实测（2026-09-15）

本轮使用 `tests/test_videos` 中的真实 V7-R2 Topos MOV，在本机 macOS x86_64、Release native dylib、Metal 后端下测试。时间线测试由 `EditProject`、`VideoFrameProviderService` 和 `TimelineExporter` 组成，96 帧、25 fps、无效果，Topos Proxy 输入导出 Topos HQ。

## 播放源交付

这里的边界是 `ToposMediaSource` 从 MOV 读取并交付 CPU planar frame；不包含 GPU 上传、调色、合成、显示和 Qt 调度。

| 素材 | 分辨率 | 实测 FPS（两轮） | 平均 FPS |
|---|---:|---:|---:|
| `2K_topos_proxy.mov` | 1920×1080 | 702.3 / 703.3 | **702.8** |
| `2K_topos_hq.mov` | 1920×1080 | 258.6 / 248.5 | **253.6** |
| `4K_topos_proxy.mov` | 3840×2160 | 204.3 / 203.0 | **203.7** |
| `4K_topos_4444xq.mov` | 3840×2160 | 42.2 / 42.8 | **42.5** |

## 完整时间线导出

| 分辨率 | 导出 FPS | 实时倍速（25 fps） | Metal 合成成功 | 直接 planar passthrough | 渲染均值 | 编码均值 |
|---|---:|---:|---:|---:|---:|---:|
| 1920×1080 | **44.095** | **1.764×** | 96/96 | 0/96 | 11.047 ms/帧 | 6.194 ms/帧 |
| 3840×2160 | **17.964** | **0.719×** | 96/96 | 0/96 | 40.129 ms/帧 | 19.403 ms/帧 |

输出文件均通过首帧、末帧和 96 帧计数校验。修复重复的 Topos→FFmpeg 探测后，相同测试口径下 2K 从 39.283 FPS 提升到 44.095 FPS，4K 从 17.429 FPS 提升到 17.964 FPS。

## 结论

Topos 的播放解码可以接近单独 codec benchmark 的数量级，但完整时间线导出不能直接达到 500+ FPS 或 100+ FPS 的纯编码/解码数值，因为它还包含时间线调度、Metal 合成、GPU/主机交接、Topos 编码和 MOV 封装。当前 2K 已超过 25 fps 实时，4K 无效果仍低于实时；主要瓶颈在 Metal 渲染/交接和输出编码链路，而不是素材解码。

原始结构化结果：`tests/media/results/topos_timeline_playback_export_20260915.json`。
