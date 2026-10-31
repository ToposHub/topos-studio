# 导出效果矩阵基准（t3_hevc8_h264_identity）

- 生成时间：2026-09-17 08:11:18
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| hevc8 | h264 | identity | adaptive | 125.931 | 6.126 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
