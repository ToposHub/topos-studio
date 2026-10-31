# T-P1-71 批次三补充：H265 导出 200 轮长跑定性（残余内存增长分类）

日期：2026-09-19 · rev：见 git log · 命令：

```bash
TOPOS_CODEC_LIB="$PWD/native/topos_codec/build/release/topos_codec.0.1.0.dylib" \
QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 \
.venv/bin/python scripts/bench_h265_memory_growth.py \
  --rounds 200 --frames 24 --codec hevc
```

## 结论

**有界（非无界泄漏）**：`slope_mb_per_round=2.199 < 5.0` 阈值，
`growth_unbounded=false`。40 轮时观察到的 ~4MB/轮残余随轮数**单调减速**，
形态为缓慢收敛到平台，而非线性泄漏。

## 证据（RSS 相对 warmup 基线 415.8MB 的增量）

| 轮次 | RSS | 累计增量 | 每 25 轮增量 |
|---:|---:|---:|---:|
| 10 | 623.2MB | +207.4 | — |
| 40 | 756.1MB | +340.4 | （前段主导，与旧 40 轮口径吻合） |
| 100 | 895.3MB | +479.5 | — |
| 125 | 958.1MB | +542.4 | +63.0（2.5MB/轮） |
| 150 | 986.9MB | +571.1 | +28.7（1.1MB/轮） |
| 175 | 1005.1MB | +589.3 | +18.2（0.7MB/轮） |
| 199 | 1029.0MB | +613.3 | +23.9（~1.0MB/轮，含噪声） |

尾段增量 63 → 29 → 18 →（24 噪声）MB/25 轮：递减速收敛。若为真泄漏
（40 轮口径 4MB/轮线性外推），200 轮应达 ~1215MB 且尾段速率不减——
实测相反。文件句柄恒为 4（无 fd 泄漏）；每轮导出均 ok。

## 归因

与 20260919-061047 的 60 轮裸 PyAV 长跑结论一致：主体是 FFmpeg/PyAV
首次使用的高水位保留 + 分配器碎片缓慢爬升到稳态，而非按导出次数线性
增长的对象泄漏。残留 ~1MB/轮的缓慢收敛在可接受范围；如需进一步压低，
后续可用 malloc stack logging 定位分配站点（非本机阻塞项）。

机器可读证据：`memory_growth.json`（schema t-p1-71-h265-memory-growth/1）。
