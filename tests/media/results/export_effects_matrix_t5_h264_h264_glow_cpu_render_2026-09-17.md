# 导出效果矩阵基准（t5_h264_h264_glow_cpu_render）

- 生成时间：2026-09-17 13:35:31
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | h264 | glow | cpu_render | 5.793 | 172.575 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
