# W7-01 四维组合 A/B 总报告（AMD RX 6800 行）——分段归因

日期：2026-09-12（汇编；数据实测日期见各分项）｜ 机器：AMD RX 6800 + 16C/24T CPU + Win11 26200
性质：**单机 AMD 行汇总**。计划 W7-01 要求的 NVIDIA/D3D/CUDA 组合对比无真机，
按 K-W5 不写；本报告把本机已有分段数据集中归档，作为 W7-01 的 AMD 分册。

原始数据（均为 .md+.json 配对，实测脚本随仓库）：

| 分段 | 报告/数据 | 结论摘要 |
|---|---|---|
| decode（硬解下载 vs CPU） | `windows_decode_baseline_ab.md` + `scripts/bench_hw_decode_results.json` | AMD 硬解同步下载是瓶颈（4K ~47fps）；CPU planar 486fps → 默认 CPU 路由 |
| decode（能力矩阵） | `windows_capability_matrix.md` + `.json` | d3d11va：h264/hevc 420 8/10bit 全支持；422 全拒；AV1 静默软解 |
| hw planar 契约 | `windows_hw_planar_first_validation.md` + `.json` | NV12/P010 产出与 macOS VT 同契约；422 显式拒绝 |
| interop（W3 可行性） | `wgl_interop_probe_amd_rx6800.md` + `wgl_shared_texture_roundtrip_amd_rx6800.md` + `.json`×2 | WGL_NV_DX_interop 全链路（注册/锁定/像素级 roundtrip）真实可用 |
| compute（GL） | `gl_compute_probe_amd_rx6800.md` + `.json` | GL 4.5/4.6 但 compute 阶段被驱动拒收；fragment 回退成立；门控已改探测式 |
| export（硬编 AMF） | `scripts/bench_hw_encode_results.json` + `scripts/BENCH_HW_DECODE_WINDOWS.md` | h264_amf 3.3×、hevc_amf 5.7×；10bit/HDR 保持 CPU（显式门控） |

## 分段归因（AMD 行）

| 段 | 现状 | 主要收益路径 |
|---|---|---|
| decode | CPU planar 默认（硬解下载反而慢） | W3 zero-copy 若立项：绕开同步下载瓶颈（W3-06 A/B 裁决） |
| interop | 可行性已证（roundtrip ok），未接入生产 | W3-01/02 落地（ctypes 形态已被验证可行） |
| present | OpenGL/WGL（W5-01 决策维持） | W5-02 D3D flip-model 评估仅在 W3 后考虑 |
| compute | fragment（GL compute 驱动不可用） | W4-03 DirectML（ONNX 通道，依赖决策未做） |
| export | AMF 已接入（4:2:0 8bit SDR 自动） | 10bit AMF 待驱动支持；ProRes 无厂商硬编 |

## NVIDIA/D3D/CUDA 组合（缺真机，K-W5 声明）

- NVDEC/NVENC/CUDA interop/CUDA compute：**无实测行**。所有相关 capability
  表述均为「探测可用即放行」的逻辑层，无真机验收记录。
- CI 逻辑层（`windows-logic.yml`）覆盖上述无硬件可测部分。
