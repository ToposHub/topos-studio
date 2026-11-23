# Topos Metal / OpenGL A/B 导出基线

- 采集时间（UTC）: `2026-11-22T16:56:21+00:00`
- 提交: `3416549b6cc061194eb02db57368c9dd0cb40fff`
- 机器: Mac Pro · Intel(R) Core(TM) i9-9900K CPU @ 3.60GHz · AMD Radeon RX 6900 XT, PHL 439P1
- Python 3.12.13 · macOS-15.3.1-x86_64-i386-64bit
- A/B 目录: `bench_out/metal-ab.2026-11-23` · 每条路径 6 轮（第 1 轮为首跑，其后为暖跑，逐轮交替顺序）

## 墙钟时间（提交任务到文件关闭）

| 路径 | 环境变量 | 首跑 (s) | 暖跑中位数 (s) | 暖跑 min–max (s) | 实际交付路径 | 全 GPU | CPU 上传/回读字节 | 成功 |
|---|---|---:|---:|---|---|---|---:|---|
| `gl_h264` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EXPORT_NATIVE_VT=0` | 11.149 | 10.843 | 10.708–11.861 | `(generic)` | False | 0/0 | 6/6 |
| `metal_h264` | `TOPOS_BACKEND_GRAPHICS=metal` | 7.114 | 7.124 | 7.05–7.302 | `metal_h264_nv12_lut` | True | 0/0 | 6/6 |
| `gl_dnxhr` | `TOPOS_BACKEND_GRAPHICS=opengl` | 19.225 | 19.276 | 18.586–19.634 | `(generic)` | False | 0/0 | 6/6 |

逐轮暖跑秒数：

- `gl_h264`: 10.753, 10.843, 11.291, 11.861, 10.708
- `metal_h264`: 7.218, 7.117, 7.302, 7.124, 7.05
- `gl_dnxhr`: 19.634, 18.665, 19.305, 18.586, 19.276

## 分阶段（暖跑中位数，毫秒）

### `gl_h264`

| 阶段 | 后端 | 样本数 | 墙钟 p50 | 墙钟 p95 | CPU 上传字节 | CPU 回读字节 |
|---|---|---:|---:|---:|---:|---:|
| decode | `provider_session` | 400 | 6.57 | 7.79 | 0 | 0 |
| encode | `h264` | 400 | 13.895 | 23.728 | 0 | 0 |
| encode_submit | `cpu_encoder` | 400 | — | — | 0 | 0 |
| mux | `container_writer` | 1 | 17.532 | 17.532 | 0 | 0 |
| render | `opengl` | 400 | 23.094 | 26.078 | 0 | 0 |

### `metal_h264`

| 阶段 | 后端 | 样本数 | 墙钟 p50 | 墙钟 p95 | CPU 上传字节 | CPU 回读字节 |
|---|---|---:|---:|---:|---:|---:|
| decode | `videotoolbox` | 400 | 0.111 | 0.159 | 0 | 0 |
| encode | `videotoolbox` | 400 | 6.91 | 7.677 | 0 | 0 |
| encode_submit | `videotoolbox` | 400 | — | — | 0 | 0 |
| mux | `videotoolbox` | 1 | 52.055 | 52.055 | 0 | 0 |
| render | `metal` | 400 | 1.943 | 2.152 | 0 | 0 |

### `gl_dnxhr`

| 阶段 | 后端 | 样本数 | 墙钟 p50 | 墙钟 p95 | CPU 上传字节 | CPU 回读字节 |
|---|---|---:|---:|---:|---:|---:|
| decode | `ffmpeg_planar` | 400 | 7.455 | 17.365 | 0 | 0 |
| encode | `dnxhr` | 400 | 43.254 | 62.868 | 0 | 0 |
| encode_submit | `cpu_encoder` | 400 | — | — | 0 | 0 |
| mux | `container_writer` | 1 | 18.113 | 18.113 | 0 | 0 |
| render | `opengl` | 400 | 27.923 | 49.364 | 0 | 0 |

## 成片核验（首跑产物）

| 文件 | 帧数 | 编码 | 像素格式 | 尺寸 | 帧率 | 实际码率 (Mbit/s) | 编码器 | range / matrix / transfer / primaries | 格式合格 |
|---|---:|---|---|---|---|---:|---|---|---|
| `Topos_h264_LUT_to_h264_101.mp4` | 400 | h264 | yuv420p | 3840×2160 | 25/1 | 29.78 | — | tv / bt709 / bt709 / bt709 | ✅ |
| `Topos_h264_LUT_to_h264_201.mp4` | 400 | h264 | yuv420p | 3840×2160 | 25/1 | 28.42 | — | tv / bt709 / bt709 / bt709 | ✅ |
| `Topos_dnxhr_LUT_to_dnxhr_301.mov` | 400 | dnxhd | yuv422p10le | 3840×2160 | 25/1 | 728.27 | — | tv / bt709 / bt709 / bt709 | ✅ |

