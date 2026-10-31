# 导出效果矩阵基准（real_h264_two_layer_groups）

- 生成时间：2026-09-17 17:47:34
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | h264 | two_layer | adaptive | 16.031 | 56.842 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | two_layer | forced_gpu | 17.11 | 54.277 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | two_layer | cpu_render | 5.952 | 163.619 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
