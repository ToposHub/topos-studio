# W6 导出转换层分环节基准 + 帧级并行转换优化（AMD RX 6800）

日期：2026-09-12 ｜ 机器：AMD RX 6800 + 24 逻辑核 CPU + Win11 26200 ｜ PyAV 18.1.0
脚本：`scripts/bench_encode_conversion_windows.py`（`.md`+`.json` 配对，P9C-02）
素材：`tests/test_videos/4K_prores422HQ.mov` → 生产形态 bgr48le packed uint16 帧

## 背景

W6-02 收口后（`BENCH_HW_DECODE_WINDOWS.md`），4K HEVC 10bit AMF 端到端
22.6fps（44ms/帧），历史结论「剩余瓶颈是 uint16 转换层 ~35ms/帧，已无
低垂果实」。本测量把该结论精确化，并找到并验证了一个中间档优化。

## 分环节测量（稳态 48 帧，4K）

| 环节 | 耗时/帧 |
|---|---|
| A 直填 `plane.update`（bgr48le 本体） | 11.6ms |
| B swscale bgr48le→yuv420p10le（x265 路径） | 31.1ms |
| C swscale bgr48le→p010le（AMF 10bit，单线程） | 33.3ms |
| D 帧内分带并行（k=2/4/8，含拼装） | **45-63ms，且色度平面 bit 级不一致** ❌ |
| AMF hevc 10bit 纯编码（预转换 p010le 直喂） | **13.1ms** |

端到端地板 = 转换线程的单帧串行（≈45ms = A+C），编码（13.1ms）已被
`pipelined_conversion` 完全重叠。

## 帧内分带并行：实测证伪（负结果，防复蹈）

- **更慢**：k=2/4/8 均 45-63ms > 单线程 33ms——每带重开 swscale 上下文
  的固定开销主导，带越小开销占比越高；
- **不正确**：色度平面与单线程输出 bit 级不一致——swscale 垂直色度
  重采样跨带边界取邻行，行对自洽假设不成立。

## 采纳方案：帧级并行转换（`convert_workers`）

多转换线程各转**整帧**（独立 swscale 上下文，释放 GIL），主线程按 pts
序重排后编码。线程扩展性实测：1→2 线程 1.5×，3 线程饱和（~20ms/帧等效，
受内存带宽与 GIL 竞争限制）。

| 端到端（稳态） | 优化前 | 优化后（convert_workers=3） | 收益 |
|---|---|---|---|
| 4K HEVC **10bit** AMF | 22.6 fps | **29.8 fps** | +32%（对 W6-02 前的 CPU x265 1.8fps 累计 **~16×**） |
| 4K H.264 8bit AMF | 21.2 fps | **36.7 fps** | +73% |
| 4K libx265 10bit（对照） | 2.7-2.8 fps | 不变（CPU 编码器自身是地板） | — |

## 剩余边界（诚实标注）

- e2e 33.6ms/帧 vs 理论 max(20, 13.1)=20ms：差值为主线程 encode/mux 与
  转换线程的 GIL/带宽竞争残余，线程数已饱和（3→6 无增益）；
- 下一档收益需要 **GPU 渲染直出 YUV 平面**（消掉 RGB→YUV 转换本体，
  W6-03 结构性改造范畴）或原生层转换桥；
- `convert_workers` 内存上界 ≈ (workers+1) 输入帧 + workers 输出帧
  （4K 10bit ≈ 300MB），时间线导出默认 3，其余路径默认 1（语义不变）。

## 正确性回归

- 116 项编码器测试全过（新增：3 路并行乱序完成→按 pts 序编码的单调性
  与完整性、默认 workers=1 历史语义、AMF 选项级失败 CPU 重建）；
- 分带方案因 bit 不一致被否决，未进入生产代码。
