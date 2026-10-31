# 导出效果矩阵基准（baseline）

- 生成时间：2026-09-17 07:36:50
- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR

| source | codec | scenario | group | FPS | 帧时p50(ms) | 输出 probe | trail/回退 |
|---|---|---|---|---|---|---|---|
| h264 | h264 | identity | adaptive | 130.462 | 6.074 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | resize | adaptive | 212.922 | 2.978 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | opacity_fade | adaptive | 67.55 | 6.236 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | hevc8 | identity | adaptive | 22.361 | 41.257 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| h264 | hevc8 | resize | adaptive | 26.408 | 38.771 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| h264 | hevc8 | opacity_fade | adaptive | 21.307 | 42.77 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| h264 | hevc10 | identity | adaptive | 20.841 | 44.738 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| h264 | hevc10 | resize | adaptive | 24.052 | 38.782 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| h264 | hevc10 | opacity_fade | adaptive | 19.767 | 47.644 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| hevc8 | h264 | identity | adaptive | 25.58 | 35.314 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| hevc8 | h264 | resize | adaptive | 25.227 | 36.063 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| hevc8 | hevc8 | identity | adaptive | 21.057 | 48.662 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| hevc8 | hevc8 | resize | adaptive | 26.769 | 34.022 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| hevc10 | hevc8 | identity | adaptive | 21.652 | 42.814 | hevc/Main/yuv420p10le/240f | cpu_encoder_surface_path |
| h264 | h264 | resize | adaptive | 221.214 | 2.94 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | resize | forced_gpu | 205.089 | 3.013 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | resize | cpu_render | 213.706 | 3.059 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | grade | adaptive | 24.053 | 38.674 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | grade | forced_gpu | 23.928 | 39.43 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | grade | cpu_render | 1.737 | 576.621 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | blur | adaptive | 9.427 | 103.31 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | blur | forced_gpu | 9.561 | 107.43 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | blur | cpu_render | 9.59 | 103.162 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | two_layer | adaptive | 14.235 | 66.479 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | two_layer | forced_gpu | 14.654 | 64.288 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | two_layer | cpu_render | 5.199 | 186.783 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | transition | adaptive | 66.965 | 6.158 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | transition | forced_gpu | 66.671 | 6.181 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
| h264 | h264 | transition | cpu_render | 37.828 | 6.142 | h264/Main/yuv420p/240f | cpu_encoder_surface_path |
