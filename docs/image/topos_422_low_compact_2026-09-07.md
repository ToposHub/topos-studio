# Topos 422 Low Compact 复测（2026-09-07）

素材为 `tests/test_videos/2K_h264.mp4` 第 0 帧（1920×1080，源 `yuv420p`），
转换为 `yuv422p10le` planar uint16 LE 后编码。输出通过 `toos verify --deep`，
解码为同布局 raw 后计算 10-bit 峰值 PSNR。

| 档位 | qmatrix | qp | 文件大小 | PSNR | 解码 p50 | 解码 p95 |
|---|---:|---:|---:|---:|---:|---:|
| 422 Low（当前） | 3 | 63 | 1.081 MB (1,080,613 B) | 46.49 dB | 4.241 ms | 4.923 ms |
| 422 Low（原 Standard） | 1 | 63 | 1.117 MB (1,116,689 B) | 46.93 dB | — | — |
| 422 Medium | 1 | 61 | 1.365 MB (1,364,980 B) | 50.02 dB | 5.009 ms | 6.254 ms |

Low 使用 `qmatrix_id=3` 后比原实现小 3.2%，与 Medium 的体积差从 0.248 MB
增加到 0.284 MB。矩阵只影响编码量化，解码继续走现有查表、逆量化、逆变换
和 slice 路径。

测试命令：

```text
toos encode frame_422p10.raw -o 422-low.toos --width 1920 --height 1080 --tier 422-low --overwrite
toos verify 422-low.toos --deep
toos benchmark 422-low.toos --iters 20
```
