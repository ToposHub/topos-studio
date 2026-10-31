# 导出效果矩阵基准（t6_grade_cpu）

- 生成时间：2026-09-17 13:40:02
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | h264 | grade | cpu_render | 28.109 | 33.522 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
