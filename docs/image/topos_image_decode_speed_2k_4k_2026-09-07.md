# Topos 图片解码速度：2K / 4K 对比（2026-09-07）

本次使用两段真实素材的第 300 帧：

- 2K：`tests/test_videos/2K_h264.mp4`，1920×1080，yuv420p，25 fps
- 4K：`tests/test_videos/4K_H264.mov`，3840×2160，yuv420p，25 fps

每帧先转换成 Topos 的 planar uint16 输入，再按图片档位编码。编码输出均通过
`toos verify --deep`。解码速度来自 `toos benchmark --iters 20` 的 hot-cache
`decode` 项，FPS = `1000 / decode_ms`。只统计 Topos 解码，不含磁盘读取、FFmpeg
色彩转换或显示上传。

## 2K（1920×1080）

| 档位 | qmatrix / qp | 文件大小 | 解码 p50 | 解码 p95 |
|---|---:|---:|---:|---:|
| 422 Low | qm3 / 63 | 0.310 MB | **494.6 fps** | 384.0 fps |
| 422 Medium | qm1 / 61 | 0.440 MB | **416.3 fps** | 321.3 fps |
| 422 High | qm1 / 58 | 0.678 MB | **315.8 fps** | 232.7 fps |
| 422 Ultra | qm0 / 58 | 1.408 MB | **165.9 fps** | 145.4 fps |
| 444 High | qm2 / 63 | 1.613 MB | **135.3 fps** | 121.1 fps |
| 444 Ultra | qm2 / 58 | 1.800 MB | **128.7 fps** | 107.0 fps |

## 4K（3840×2160）

| 档位 | qmatrix / qp | 文件大小 | 解码 p50 | 解码 p95 |
|---|---:|---:|---:|---:|
| 422 Low | qm3 / 63 | 0.655 MB | **242.3 fps** | 204.4 fps |
| 422 Medium | qm1 / 61 | 0.844 MB | **223.1 fps** | 186.1 fps |
| 422 High | qm1 / 58 | 1.181 MB | **174.9 fps** | 144.5 fps |
| 422 Ultra | qm0 / 58 | 2.131 MB | **107.2 fps** | 89.0 fps |
| 444 High | qm2 / 63 | 3.037 MB | **74.1 fps** | 64.2 fps |
| 444 Ultra | qm2 / 58 | 3.271 MB | **75.4 fps** | 66.1 fps |

## 结论

2K 422 Low/Medium/High/Ultra 的 p50 解码吞吐分别为约 495/416/316/166 fps；
4K 对应约 242/223/175/107 fps。2K 444 High/Ultra 约 135/129 fps，4K 约
74/75 fps。所有档位都高于 25 fps 播放需求；按 p95 作为更保守口径，4K 422
Ultra 约 89 fps，4K 444 两档约 64–66 fps。

4K 比 2K 的像素数多 4 倍，但解码时间没有严格变成 4 倍，原因是当前 codec
按 slice/块并行，且不同源帧的残差复杂度、零系数数量和文件大小不同。两段素材
不是同一母版的不同分辨率，因此文件大小只作为本次解码负载记录，不能直接作为
码率对照结论。

测试环境：Intel Core i9-9900K、macOS 15.3.1、release `toos`、20 次热缓存
重复测量，git `05bb9d9c`。
