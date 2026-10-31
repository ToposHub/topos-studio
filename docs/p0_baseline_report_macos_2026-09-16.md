# P0-11 基线复测报告（macOS，B-01..B-04）

> 性能总计划 §6 P0-11：在目标机重新建立当前基线。旧历史数据仅做对照。
> runner：`tests/benchmark/benchmark_runner.py`（P0-08 协议，完整参数）
> 原始 artifact：`bench_out/p0_runner/p0_runner_20260916_005041.json`

## 机器与口径

- 平台：macOS（darwin 24.3.0, x86_64），Python 3.13（项目 .venv）
- commit：94f42792（P0-08 runner 落地后的下一提交）
- 口径：**纯解码**（MediaSourceFactory → FFmpeg/ToposMediaSource，无 GPU
  upload / 合成 / present）。本表不能作为播放实时性结论，只用于定位
  decode 段瓶颈与代理资格判断（总计划 E-07 约束）。
- 协议：warmup=10、steady=120 帧、seek 随机 30 次（固定种子）、longrun=600 帧。

## 结果（p50 / p95，毫秒；fps 由 p50 折算）

| 场景 | 媒体 | cold 首帧 p95 | steady p50 (fps) | steady p95 | seek p95 | longrun RSS Δ |
|---|---|---:|---:|---:|---:|---:|
| B-01 | 2K_h264.mp4 | 193.2 | 2.2 (447.6fps) | 3.4 | 67.3 | +13.4MB |
| B-02 | 4K_H265.mov | 258.2 | 22.5 (44.5fps) | 27.7 | **519.3** | +3.4MB |
| B-03 | 4K_prores422HQ.mov | 97.3 | 31.2 (32.1fps) | 38.2 | 37.4 | +0.0MB |
| B-03 | 4K_prores422LT.mov | 64.2 | 24.4 (41.0fps) | 28.9 | 30.9 | +3.4MB |
| B-03 | 4K_DNxHR HQX 10bit.mov | 71.2 | 25.8 (38.8fps) | 31.7 | 34.8 | +0.0MB |
| B-04 | 4K_topos.mov | **19.7** | 13.2 (75.6fps) | 16.6 | **16.3** | +0.1MB |
| B-04 | 4K_topos_proxy.mov | **11.1** | 4.7 (212.1fps) | 7.5 | **7.4** | +0.0MB |

## 结论（供 T-01/T-10 与代理策略使用）

1. **4K ProRes HQ 软解 32fps**（E-06 复现）：单解码段已超 25fps 帧预算
   （40ms），realtime 原片播放必然需要 Topos proxy / 优化媒体接管——
   支持 D-02 的格式感知代理默认策略。
2. **Topos 422 在同机显著快于 ProRes**：4K_topos 75.6fps vs ProRes HQ
   32fps（2.4×）；seek p95 16ms vs 37ms。Topos proxy 达 212fps 且
   cold 首帧 11ms——满足"导入即可编辑"的代理档资格。
3. **4K H265 长 GOP seek p95=519ms**：远超 §3.2 的 seek 预算（120ms），
   需要代理 seek 索引（P2-05）或 hard-decode 路径（本机为 x86_64，
   需在 Apple Silicon 复测硬解对照）。
4. **无泄漏迹象**：600 帧 longrun RSS Δ ≤ 13.4MB（H264 long-GOP 累积
   属正常缓存），Topos/ProRes/DNxHR 均 ≤ 3.4MB。

## 已知限制

- x86_64 macOS：无 VideoToolbox 硬解对照（B-02 的"硬解可用机"分支
  需 Apple Silicon 真机补测）。
- 纯解码口径：不含 GPU import、调色、合成、present；播放实时性结论
  需 Edit 页 GPU 链路端到端 trace（P0-01 trace id 已具备接入条件）。
- Windows 真机基线未跑（需 NVIDIA/AMD/Intel 实机，P7 阶段闭环）。
