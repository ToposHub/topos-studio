# Topos vs ProRes 真实素材编码/解码对比（4K + 2K，四档位）

**日期：** 2026-09-10　**机器：** Intel i9-9900K（8C16T）　**Topos：** v1.5（qp 域 0..95，ADR-C031）　**ProRes：** FFmpeg 8.1 `prores_ks` 编码 / 内建 `prores` 软解

**素材：** `tests/test_videos/4K_prores422HQ.mov`（3840×2160 10-bit 4:2:2 25fps）与
`2K_prores422HQ.mov`（1920×1080）——各自取前 **120 帧**（4.8s）转 raw
（`yuv422p10le`，与 Topos 输入契约一致，零转换损失）。真实拍摄内容，
非合成素材。

**口径：** 编码 = 整文件墙钟（含 raw 读盘；Topos 为逐帧 sized qp 搜索
`--target-mb`，16 线程 `TOPOS_SLICE_THREADS=16` 与 1 线程；ProRes
`-threads 0/1` 实测等速——`prores_ks` 实际单线程）。解码 = 3 次取中位
（Topos `topos_decoder_cli` → /dev/null，1t/16t；ProRes `-benchmark` rtime，
threads 1/自动）。码率 = 文件大小 ×8×25fps。质量 = PSNR（对源 raw，
三平面平均，ffmpeg psnr 滤镜）。

**修复附带：** `topos_encoder_cli --target-mb` 原公式 ×fps 为量纲错误
（应为 ÷fps，目标随帧率平方漂移），本次修复后语义 = Mbps。

## 一、产品档位对比（各用各的档位定义）

Topos 档位 = 产品 target_bpp（`topos_profiles.py`，sized 搜索命中）；
ProRes 档位 = profile 0/1/2/3 固定质量（码率随内容浮动）。
**同名档位码率并不相等**——Topos 档位定标比 ProRes 重 1.7~4.2×，
PSNR 相应高 0.2~4.0 dB；质量/体积重定标属产品议题，不在本表评判。

### 2K（1920×1080，120 帧）

| 档位 | Topos 码率 | Topos 编码 (16t/1t) | Topos 解码 (16t/1t) | Topos PSNR | ProRes 码率 | ProRes 编码 | ProRes 解码 (auto/1t) | ProRes PSNR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Proxy | 59.4 Mbps | **150** / 52 fps | 327 / 95 fps | 59.7 | 14.2 Mbps | 67 fps | 741 / 321 fps | 55.7 |
| LT | 121.3 Mbps | **100** / 39 fps | 245 / 63 fps | 63.0 | 46.6 Mbps | 44 fps | 706 / 178 fps | 60.4 |
| Standard | 183.7 Mbps | **92** / 32 fps | 212 / 49 fps | 65.4 | 68.2 Mbps | 55 fps | 690 / 134 fps | 62.7 |
| HQ | 271.4 Mbps | **92** / 26 fps | 179 / 38 fps | 69.4 | 114.6 Mbps | 52 fps | 571 / 87 fps | 66.8 |

### 4K（3840×2160，120 帧）

| 档位 | Topos 码率 | Topos 编码 (16t/1t) | Topos 解码 (16t/1t) | Topos PSNR | ProRes 码率 | ProRes 编码 | ProRes 解码 (auto/1t) | ProRes PSNR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Proxy | 235.8 Mbps | **29** / 13.5 fps | 150 / 27 fps | 58.2 | 77.4 Mbps | 18 fps | 188 / 71 fps | 56.1 |
| LT | 482.0 Mbps | **35** / 10.3 fps | 95 / 16 fps | 61.3 | 284.1 Mbps | 12 fps | 163 / 33 fps | 60.9 |
| Standard | 734.1 Mbps | **31** / 8.2 fps | 75 / 12 fps | 63.5 | 409.5 Mbps | 15 fps | 157 / 25 fps | 62.9 |
| HQ | 1098.5 Mbps | **26** / 6.6 fps | 61 / 10 fps | 66.0 | 645.8 Mbps | 13 fps | 124 / 16 fps | 67.0 |

## 二、同码率对比（Topos sized 到 ProRes 实测码率）

公平速度/质量比较：Topos 逐帧 qp 搜索钉住 ProRes 各档实际码率（差 <3%）。

### 2K

| 对齐档位 | 码率 | Topos 解码 (16t/1t) | ProRes 解码 (auto/1t) | 速度比 (16t) | Topos PSNR | ProRes PSNR | ΔPSNR |
|---|---:|---:|---:|---:|---:|---:|---:|
| Proxy | ~14 Mbps | 517 / 225 fps | 741 / 321 fps | 0.70× | 47.3 | 55.7 | **−8.5** |
| LT | ~45 Mbps | 361 / 115 fps | 706 / 178 fps | 0.51× | 58.3 | 60.4 | −2.1 |
| Standard | ~66 Mbps | 313 / 89 fps | 690 / 134 fps | 0.45× | 60.0 | 62.7 | −2.7 |
| HQ | ~110 Mbps | 256 / 66 fps | 571 / 87 fps | 0.45× | 62.4 | 66.8 | −4.4 |

### 4K

| 对齐档位 | 码率 | Topos 解码 (16t/1t) | ProRes 解码 (auto/1t) | 速度比 (16t) | Topos PSNR | ProRes PSNR | ΔPSNR |
|---|---:|---:|---:|---:|---:|---:|---:|
| Proxy | ~76 Mbps | 213 / 62 fps | 188 / 71 fps | **1.13×** | 50.4 | 56.1 | −5.7 |
| LT | ~270 Mbps | 140 / 24 fps | 163 / 33 fps | 0.86× | 58.7 | 60.9 | −2.2 |
| Standard | ~395 Mbps | 113 / 19 fps | 157 / 25 fps | 0.72× | 60.3 | 62.9 | −2.6 |
| HQ | ~630 Mbps | 86 / 14 fps | 124 / 16 fps | 0.69× | 62.5 | 67.0 | −4.5 |

（对齐组编码速度：2K 0.8~1.0s/120帧、4K 2.8~3.7s/120帧 16t——比产品档位组更快，
因码率更低。）

## 三、P5 码率对齐重定标（当日第二版，已实施）

§一 的 1.7~4.2× 偏重当日即整改（ADR-C032）：产品四档 target_bpp 按
本表 ProRes 实测体积重定标（两段素材 bpp 均值：proxy 0.32310 / lt
1.13412 / standard 1.64528 / hq 2.66245），同时把 v1.5 qp 域 0..95
打通到产品编码路径（旧 qp63 硬顶使 proxy 档实际需 qp78~84 不可达）。
444 档无 4444 素材锚点，暂保留 P4 定标。**§一 表因此成为旧定标的历史
记录**；下表为重定标后实测（同机同口径，CLI 目标取整 Mbps）：

### 2K（1920×1080，120 帧）

| 档位 | Topos 码率 | 码率比 vs ProRes | Topos PSNR | ΔPSNR | Topos 编码 16t | Topos 解码 16t/1t | ProRes 解码 auto/1t | 解码速度比 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Proxy | 16.7 Mbps | 1.18× | 53.0 | −2.7 | 161 fps | **912** / 244 | 741 / 321 | **1.23×** |
| LT | 55.6 Mbps | 1.19× | 59.4 | −1.0 | 143 fps | 465 / 103 | 706 / 178 | 0.66× |
| Standard | 80.5 Mbps | 1.18× | 61.0 | −1.7 | 133 fps | 392 / 81 | 690 / 134 | 0.57× |
| HQ | 129.0 Mbps | 1.13× | 63.4 | −3.4 | 116 fps | 314 / 61 | 571 / 87 | 0.55× |

### 4K（3840×2160，120 帧）

| 档位 | Topos 码率 | 码率比 vs ProRes | Topos PSNR | ΔPSNR | Topos 编码 16t | Topos 解码 16t/1t | ProRes 解码 auto/1t | 解码速度比 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Proxy | 66.0 Mbps | 0.85× | 48.2 | −7.9 | 46 fps | **285** / 68 | 188 / 71 | **1.52×** |
| LT | 221.8 Mbps | 0.78× | 57.9 | −3.0 | 40 fps | **180** / 27 | 163 / 33 | **1.10×** |
| Standard | 321.9 Mbps | 0.79× | 59.5 | −3.4 | 38 fps | 139 / 21 | 157 / 25 | 0.89× |
| HQ | 522.5 Mbps | 0.81× | 61.7 | −5.3 | 34 fps | 102 / 15 | 124 / 16 | 0.82× |

- **码率**：两素材均值按构造 = 1.00×；单素材残余 0.78~1.19× 是固定
  bpp 档位 vs ProRes 内容浮动（两段素材同档 ProRes bpp 差 35~52%）的
  固有带宽。
- **解码**：码率减半 ⇒ 熵解码负载减半，吞吐比旧定标近乎翻倍；4K 低码率
  档反超 ProRes，2K 高码率档仍落后（0.55×）。
- **编码**：领先保持并扩大（2K 1.7~2.4×、4K 2.2~2.6× prores_ks）。
- **质量**：同码率差距 1.0~7.9 dB（§二 结构性结论不变；4K 侧含码率
  落点 0.78~0.85× 的因素）。

## 四、V2 口径复测（产品默认熵 = canonical VLC，当日第三版）

§一~§三 的 Topos 数字全部是 CLI 默认 V1 Rice 熵；产品导出默认 V2
canonical VLC。同目标码率复测（P5 档位，2K/4K 各四档）：

| 档位 | 2K PSNR V1→V2 | Δ | vs ProRes | 4K PSNR V1→V2 | Δ | vs ProRes |
|---|---|---:|---|---|---:|---|
| Proxy | 53.0→55.7 | +2.7 | **−0.0（追平）** | 48.2→53.0 | +4.8 | −3.1 |
| LT | 59.4→60.9 | +1.5 | **+0.5（反超）** | 57.9→59.6 | +1.7 | −1.3 |
| Standard | 61.0→62.6 | +1.6 | **−0.1（追平）** | 59.5→61.1 | +1.6 | −1.8 |
| HQ | 63.4→65.5 | +2.1 | −1.3 | 61.7→63.5 | +1.8 | −3.5 |

- **产品真实质量差距 = V2 列**：2K 四档全部追平/反超 ProRes，4K 落后
  1.3~3.5 dB（4K 档码率落点本就偏低 0.78~0.85×，部分差距来自码率）。
  §二/§三 结论中 "2.1~8.5 dB 结构性差距" 修正为：V1 口径的测量；
  V2 口径下 2K 追平、4K 减半。
- **代价**：VLC 解码比 Rice 慢 9~31%（16t：2K proxy 896→692、4K hq
  101→70 fps）；8K HQ 折算 ≈18 fps 跌破 25fps 实时线——解码提速
  （SIMD IDCT）优先级因此上调。编码 16t 降 ~20%（仍 1.5~2× prores_ks）。
- **产品含义**：质量/体积维度 V2 已无差距悬念（默认正确）；剩余工程
  重点是解码速度（§三 注 + SIMD 战役）与 4K 侧码率落点（P5 均值定标
  的单素材残余）。

## 五、矩阵切换 flat 复测（P5.1，当日第四版）

§四 的 V2 数字仍用 Standard 矩阵（qm=1）。同码率横向比较后 422 视频档
整体切换 flat（qm=0，ADR-C033——flat 全档 PSNR 占优 +0.2~1.6 dB 且
档位越高增益越大；native 矩阵表零变更，仅产品端选择）。最终口径：

### 2K（1920×1080，120 帧）

| 档位 | Topos 码率 | Topos PSNR | vs ProRes | Topos 解码 16t/1t | ProRes 解码 auto |
|---|---:|---:|---:|---:|---:|
| Proxy | 16.5 Mbps | 55.9 | **+0.2** | 682 / 182 | 741 |
| LT | 54.5 Mbps | 61.9 | **+1.5** | 326 / 75 | 706 |
| Standard | 77.6 Mbps | 63.7 | **+1.0** | 271 / 59 | 690 |
| HQ | 129.3 Mbps | 67.1 | **+0.3** | 229 / 42 | 571 |

### 4K（3840×2160，120 帧）

| 档位 | Topos 码率 | Topos PSNR | vs ProRes | Topos 解码 16t/1t | ProRes 解码 auto |
|---|---:|---:|---:|---:|---:|
| Proxy | 65.3 Mbps | 53.4 | −2.7 | **278** / 48 | 188 |
| LT | 212.7 Mbps | 60.0 | −0.9 | 141 / 19 | 163 |
| Standard | 305.9 Mbps | 61.7 | −1.2 | 105 / 15 | 157 |
| HQ | 505.4 Mbps | 64.5 | −2.5 | 77 / 10 | 124 |

- **2K 四档全部追平/反超 ProRes（PSNR，同码率档位语义）**；4K 差距
  −0.9~−2.7 dB（4K 档码率落点偏低 0.79~0.85× 占其中一部分）。
- 解码速度与 Standard 矩阵持平或略优（flat 同码率 qp 更高 → 零系数
  更多；4K +4~10%）。
- 矩阵搜索记录（几何族网格，含超越 flat 的 b<1 方向）见 ADR-C033；
  最优点 (1.0, 1.1) 仅 +0.12 dB，未采纳。

> **2026-09-11 勘误补充（ADR-C038 后口径）**：本节 4K 差距（−0.9~−2.7
> dB）为 flat+V2 + 档位落点口径。当前产品默认（flat+rans2）码率对齐
> （1.00× ProRes 实测码率）复测：4K 反转为 −0.2~+1.4 dB（两帧窗×四档）、
> 2K +2.0~+2.9 dB；原差距主因是档位固定 bpp 落点 0.75~0.94×（非编码器
> 效率）。分解与证据见 `topos_4k_gap_decomposition_2026-09-11.md`。

## 结论（如实）

1. **编码速度 Topos 全面领先**：16 线程下 2K 92~150 fps vs prores_ks
   44~67 fps（1.7~2.2×），4K 26~35 vs 12~19 fps（1.7~2.9×）——且 Topos
   做的是逐帧 qp 搜索（码率闭环），prores_ks 无码率控制。单线程 2K 互有
   胜负、4K Topos 慢于 prores_ks（帧内多 pass 编码 vs prores 单 pass）。
2. **解码速度**：产品档位表内不可直接比（码率不同）。**同码率下** Topos
   多线程为 FFmpeg ProRes 的 0.45~0.70×（2K）/ 0.69~1.13×（4K），
   单线程 0.65~0.88×——4K 低码率档（Proxy）已反超。分辨率越高差距越小
   （每帧比特多 → 熵解码占比升、ProRes 的每帧固定开销占比也升）。
3. **同码率质量**：Topos 落后 ProRes 2.1~8.5 dB（PSNR，对源）。ProRes
   是 20 年调校的 DCT+VLC；Topos V1 Rice 熵 + flat/Standard 矩阵尚无
   帧内预测与上下文自适应编码（ADR-I011 已列方向）。低码率档差距最大
   （Proxy 8.5dB），符合 qp 顶格时量化误差主导的预期。
4. **产品档位定标偏重 → 当日已整改**：§一 的 1.7~4.2× 偏重按 ProRes
   实测码率重定标为 P5（§三，ADR-C032），同名档位码率对齐到均值
   1.00×（单素材 ±0.78~1.19×）。连带效应：解码吞吐近乎翻倍，4K 低码率
   档反超 ProRes；质量差距（1.0~7.9 dB）转为编码器工程议题
   （ADR-I011 帧内预测/上下文编码）。

## 复现

```bash
# 素材（前 120 帧 → raw，25fps）
ffmpeg -i tests/test_videos/4K_prores422HQ.mov -frames:v 120 -pix_fmt yuv422p10le -f rawvideo src4k.raw
# Topos 编码（§一 旧定标 Mbps：2K 63/128/194/286；4K 251/511/777/1143；§二 对齐组 14/46/68/114、77/284/409/645；
#          §三 P5 新定标 Mbps：2K 17/59/85/138；4K 67/235/341/552）
TOPOS_SLICE_THREADS=16 topos_encoder_cli --width 3840 --height 2160 --fps 25 --qm 1 \
  --target-mb 251 --frames 120 --input src4k.raw --output out.mov
# ProRes 编码（profile 0/1/2/3 = Proxy/LT/Standard/HQ）
ffmpeg -f rawvideo -pixel_format yuv422p10le -video_size 3840x2160 -framerate 25 \
  -i src4k.raw -c:v prores_ks -profile:v 0 out.mov
# 解码：TOPOS_SLICE_THREADS={1|16} topos_decoder_cli --input out.mov --output /dev/null
#       ffmpeg -threads {1|0} -i out.mov -f null - -benchmark   （各 3 次取中位）
# PSNR：解码至 raw 后 ffmpeg -lavfi psnr 对源
```

**生成提交：** 同日提交（含 `--target-mb` 量纲修复）。
