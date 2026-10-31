# 导出效果矩阵基准（dev）

- 生成时间：2026-09-17 07:12:53
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | h264 | identity | adaptive | 131.71 | 6.118 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | resize | adaptive | 215.293 | 2.962 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | opacity_fade | adaptive | 62.768 | 6.268 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | hevc8 | identity | adaptive | 35.548 | 25.723 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | hevc8 | resize | adaptive | 35.589 | 25.948 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | hevc8 | opacity_fade | adaptive | 19.272 | 26.119 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | hevc10 | identity | adaptive | 38.883 | 24.003 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| h264 | hevc10 | resize | adaptive | 37.954 | 24.358 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| h264 | hevc10 | opacity_fade | adaptive | 19.721 | 25.199 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| hevc8 | h264 | identity | adaptive | 37.106 | 24.803 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| hevc8 | h264 | resize | adaptive | 35.671 | 25.795 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| hevc8 | hevc8 | identity | adaptive | 190.077 | 3.578 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
| hevc8 | hevc8 | resize | adaptive | 276.387 | 2.325 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
| hevc10 | hevc8 | identity | adaptive | 19.793 | 51.039 | hevc/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | resize | adaptive | 204.252 | 3.022 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | resize | forced_gpu | 206.164 | 3.005 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | resize | cpu_render | 212.215 | 2.999 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | grade | adaptive | 23.881 | 39.096 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | grade | forced_gpu | 25.89 | 35.234 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | grade | cpu_render | 1.748 | 566.793 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | blur | adaptive | 9.119 | 108.906 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | blur | forced_gpu | 9.389 | 108.721 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | blur | cpu_render | 9.109 | 109.437 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | two_layer | adaptive | 14.485 | 65.565 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | two_layer | forced_gpu | 14.384 | 65.717 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | two_layer | cpu_render | 5.181 | 189.173 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | transition | adaptive | 67.225 | 6.15 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | transition | forced_gpu | 66.593 | 6.154 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | transition | cpu_render | 37.721 | 6.166 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
