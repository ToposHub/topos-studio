# W2-05 Windows 播放 A/B 基线：硬解下载 vs CPU 软解（AMD RX 6800）

日期：2026-08-29（实测）／ 2026-09-12（归档入 results 目录）｜ PyAV 18.1.0
原始数据（P9C-02 配对）：
- `.md`：本文件 + `scripts/BENCH_HW_DECODE_WINDOWS.md`（含方法与逐素材表）
- `.json`：`scripts/bench_hw_decode_results.json`（解码）／ `scripts/bench_hw_encode_results.json`（导出对照）
- 脚本：`scripts/bench_hw_decode_windows.py`、`scripts/probe_hw_decode_windows.py`

## 基线结论（4K 分段，fps，越高越好）

| 路径 | 4K H.264 | 4K HEVC 10bit |
|---|---|---|
| 硬解下载（PyAV HWAccel d3d11va，含每帧同步 GPU→CPU 下载） | ~47 | ~43 |
| CPU planar（24 线程 frame-threaded） | **486** | **172** |

瓶颈根因：PyAV `HWAccel` 默认对每帧同步下载（`is_hw_owned=False` 固定行为），
流水线完全串行化；换后端无效（d3d11va/d3d12va/dxva2 同为 ~24ms/帧@4K）。

## K-W1 门槛判定（zero-copy 立项门）

- **download 路径在本机不满足帧预算**（47fps < 30fps 目标的余量存疑且远劣于 CPU），
  但 **CPU planar 路径足额**（61~486fps 全素材达标）→ 「维持 download/CPU 架构」
  的默认结论对 AMD 机器成立。
- **W3 立项的反向证据同样成立**（2026-09-12 补充）：W1-02 实测
  `WGL_NV_DX_interop` 在 AMD 上 `interop_open_ok`——zero-copy 恰好绕开
  「同步下载」这一已证实的瓶颈；收益假设由 W3-06 A/B 裁决。
- **当前决策**：W3 为「证据驱动后续项」，默认不启动 native interop 实现；
  立项与否由维护者拍板（native 层 L 级工作量）。

## 路由决策（已实施，见 edit_gpu_decode_manager）

Windows 硬解优先仅限 NVIDIA GPU（`has_nvidia_gpu()`，W2-02 修正后 NVDEC
判定不再被 ffmpeg CLI 编译项误触发）或 `TOPOS_FORCE_WIN_HW_DECODE=1`；
AMD/Intel 默认 CPU frame-threaded planar（本机快 4~10 倍）。
`TOPOS_DISABLE_WIN_HW_DECODE=1` kill switch 语义不变。
