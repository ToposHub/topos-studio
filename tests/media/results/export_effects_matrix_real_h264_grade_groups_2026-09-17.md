# 导出效果矩阵基准（real_h264_grade_groups）

- 生成时间：2026-09-17 17:44:17
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | h264 | grade | adaptive | 28.803 | 30.233 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | grade | forced_gpu | 26.882 | 33.443 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | grade | cpu_render | 26.391 | 35.773 | h264/High/yuv420p/240f | cpu_encoder_surface_path |
