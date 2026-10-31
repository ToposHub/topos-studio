# 导出效果矩阵基准（t3_h264_hevc8_opacity_fade）

- 生成时间：2026-09-17 08:11:04
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | hevc8 | opacity_fade | adaptive | 75.91 | 4.072 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
