# 导出效果矩阵基准（t3_hevc10_hevc8_identity）

- 生成时间：2026-09-17 08:10:09
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| hevc10 | hevc8 | identity | adaptive | 72.841 | 11.331 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
