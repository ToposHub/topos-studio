# Topos 422 家族解码复验报告（T-01，macOS 2026-09-16）

> 性能总计划 §15 T-01：重跑 1080p/2K/4K、顺序/seek、冷/热；区分 decode 段。
> runner：`tests/benchmark/benchmark_runner.py`（P0-08 协议完整参数）
> 原始 artifact：`bench_out/p0_runner/p0_runner_20260916_005041.json`（B 矩阵）、
> `..._005645.json`（2K 家族）、`..._005815.json`（4K 家族）
> commit：df8e2847 之后

## 口径

**纯解码**（`MediaSourceFactory` → `ToposMediaSource`，libtopos_codec；
无 GPU upload / 合成 / present）。解码结论不能替代端到端播放结论（E-07）。
协议：cold=3 次全新 open+首帧；steady=120 帧顺序（p50/p95）；seek=30 次随机
（固定种子）；longrun=600 帧 + RSS。

## 2K 家族（2048×1080 或标称 2K）

| 媒体 | cold 首帧 p95 (ms) | steady p50 (fps) | steady p95 (ms) | seek p95 (ms) |
|---|---:|---:|---:|---:|
| 2K_topos_lt | 4.4 | 477.6 | 2.8 | 2.5 |
| 2K_topos_proxy | 3.5 | **740.2** | 1.5 | 1.5 |
| 2K_topos_standard | 6.0 | 382.9 | 3.5 | 3.2 |
| 2K_topos_hq | 7.2 | 268.2 | 5.6 | 4.6 |
| 2K_topos | 95.6* | 283.8 | 4.7 | 4.6 |
| 2K_topos_4444 | 9.2 | 174.3 | 7.0 | 6.2 |
| 2K_topos_4444xq | 11.9 | 142.2 | 8.2 | 7.8 |

\* 首个进程内 open 含 codec 首次初始化，后续家族成员 cold 均 < 12ms。

## 4K 家族（3840×2160）

| 媒体 | cold 首帧 p95 (ms) | steady p50 (fps) | steady p95 (ms) | seek p95 (ms) |
|---|---:|---:|---:|---:|
| 4K_topos_proxy | 8.1 | **215.7** | 5.5 | 5.8 |
| 4K_topos_lt | 16.2 | 134.9 | 9.4 | 8.8 |
| 4K_topos_standard | 17.7 | 111.4 | 11.4 | 11.5 |
| 4K_topos / 4K_topos_hq | 21.7 | 81.0–81.5 | 13.9–14.2 | 13.8–14.4 |
| 4K_topos_4444 | 24.3 | 49.6 | 23.2 | 22.5 |
| 4K_topos_4444xq | 30.9 | 44.5 | 25.0 | 23.9 |

## 结论

1. **全家族 4K 档 ≥ 44.5fps、2K 档 ≥ 142fps（纯解码）**：解码段不构成
   2K 实时瓶颈；4K 4444/4444xq（44–50fps）在多轨/复杂合成下可能超预算，
   需端到端 trace（P0-01 已具备）确认。
2. **proxy 档压倒性优势**：4K proxy 215.7fps、cold 8.1ms——T-02 的
   "导入即可编辑"默认档位有实证；编码层（encode 体积/质量）对照在
   T-05 范围另行复测。
3. **seek 与顺序成本同量级**（4K 家族 seek p95 ≈ steady p95 × 1.05–1.1）：
   Topos 帧内寻址无长 GOP 惩罚（对照 4K_H265 seek p95 519ms），P2-05
   索引需求对 Topos 不紧迫、对 H265 仍必需。
4. **长跑无泄漏**：全家族 600 帧 RSS Δ ≤ 0.5MB。
5. **4444/4444xq 比 422 慢约 2×**：444 采样 + 更高码率符合预期；
   alpha 用例（T-07）另行评估。

## 发现的矩阵缺口（阻塞 T-07/T-10 完成判定）

- ~~`*_topos_4444*.mov` 测试素材的码流 `alpha_mode=0`~~
  **已修复（2026-09-16）**：根因是 `tools/generate_topos_test_media.py`
  硬编码 `has_alpha=False`。脚本已加 `--alpha`（4444 档 →
  `topos_yuva444p10a16`，alpha mode1 无损 16-bit，合成 alpha 平面
  斜坡×棋盘逐帧漂移）并重生成全 corpus；`ToposMovieFile.info()` 探到
  `alpha_mode=1/depth=16/premult=False`，产品解码路径
  （MediaSourceFactory → ToposMediaSource）返回 4 平面且 alpha 值域
  真实变化。含 alpha 的 4K 4444 解码稳态 40.9fps（无 alpha 版 174fps，
  第 4 平面开销 ~4×，仍高于 25fps 实时线）。基准矩阵已重冻结
  （仅 4444 两文件指纹变化，其余档位字节级不变）。T-07/T-10 的
  alpha 样本前置已解除。
- 端到端（GPU import → 合成 → present）与 TOOS 图片序列缓存路径
  （T-03/T-09/T-10 剩余）未在本轮覆盖。

## 复现

```bash
.venv/bin/python -m tests.benchmark.benchmark_runner \
  --file tests/test_videos/4K_topos_proxy.mov # 或 --scenario B-04
.venv/bin/python tools/freeze_benchmark_media_manifest.py --check
```
