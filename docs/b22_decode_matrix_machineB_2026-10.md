# B2.2 素材 × 解码路径能力矩阵（机器 B 侧 · 2026-10-24）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §B2.2。
> 脚本：`scripts/bench_decode_matrix.py`（发现式枚举 + 内容指纹 +
> 期望/实际后端逐格编码）；原始数据：
> `scripts/bench_decode_matrix_B22_20261024_run01.json`。
> 环境：RTX 4090 / 驱动 621.01 / PyAV 18.1.0。证据等级：**L2**。
> **范围声明**：本文件仅机器 B（NVIDIA）原始数据；A（Mac）/C（RX 6800）
> 部分由各自机器运行同一脚本产出，不代跑、不平均、不外推。

## 1. 矩阵摘要（decode-only 稳态 fps，8 warmup + 60 timed）

发现素材 32 项（排除 macOS AppleDouble 残留 5 项）；每格含内容指纹
（size + 头部 1MB md5）、格式探测、期望/实际后端、首帧延迟、fallback
reason（详见 JSON）。

| 素材族 | 格式 | cpu_planar | hw_download | cuda_resident | 期望后端（生产路由） |
|---|---|---|---|---|---|
| 2K H264 | 8bit 420 | 2927 | 494 | **824** | win_hw_planar_download（驻留=opt-in 候选） |
| 4K H264 | 8bit 420 | 860 | 151 | **253** | 同上 |
| 2K H265 | 10bit 420 | 529 | 639 | **1175** | 同上 |
| 4K H265 | 10bit 420 | 207 | 187 | **387** | 同上 |
| ProRes 422/4444（2K/4K） | 10/12bit 422/444 | 125–2800 | not_applicable | not_applicable | cpu_planar（Intra 剪辑码流无硬解） |
| DNxHR HQX/444（2K/4K） | 10/12bit 422 | 155–940 | not_applicable | not_applicable | cpu_planar |
| Topos 自有码流（2K/4K ×8） | 厂商私有 | unsupported（PyAV 无解码器，预期） | — | — | topos_native_cpu（与 GPU 路线解耦） |

GL-ready 终点（与 B1.2 同口径，含上传/interop；4K 实测）：

| 素材 | cpu_planar | hw_download | cuda_resident（0 CPU 字节，内容对拍 ✅） |
|---|---|---|---|
| 4K H264 | 778 | 102 | **245** |
| 4K H265 10bit | 356 | 83 | **320** |

## 2. 结论（机器 B 范围内）

1. **驻留候选在全部 H264/H265 格式上稳定反超下载路径**（decode-only
   1.67–3.08×；GL-ready 2.4–3.8×），且每格 `interleaved_content_match
   =true`、`cpu_bytes_per_frame=0`。
2. 8bit H264 单轨 CPU 仍最快（H1/H3 口径不变：单轨结论不外推多轨）；
   10bit H265 驻留在 decode-only（387 vs 207）与 GL-ready（320 vs 356，
   接近）上均具备竞争力——下载税此前完全掩盖了 NVDEC 优势。
3. ProRes/DNxHR/Topos 与 GPU 路线解耦（生产路由本就不选硬解），矩阵
   格编码 `not_applicable`/`unsupported`，不冒充。
4. 失败格：本机零（Topos 的 PyAV 探测失败为预期分类，非回归）。

## 3. 交接（C 汇总用）

- A/C 机器：运行同脚本（`--tag <机器>_<日期>_run01`）产出各自 JSON，
  不改脚本语义；C 汇总三机时**独立列**，不平均。
- 完整性校验字段在 JSON 逐格：`status`/`evidence`/`reason`/
  `first_frame_ms`/`fps`/`frames_measured`。
