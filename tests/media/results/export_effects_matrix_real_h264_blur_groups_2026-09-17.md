# 导出效果矩阵基准（real_h264_blur_groups）

- 生成时间：2026-09-17 17:45:43
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | h264 | blur | adaptive | 23.973 | 38.846 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | blur | forced_gpu | 23.954 | 38.192 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | blur | cpu_render | 8.174 | 119.185 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
