# 导出效果矩阵基准（t3_h264_hevc8_resize）

- 生成时间：2026-09-17 08:10:42
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | hevc8 | resize | adaptive | 221.404 | 3.501 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
