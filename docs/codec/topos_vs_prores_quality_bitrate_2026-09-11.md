# Topos vs ProRes 画质与码率差距总表（2026-09-11，六档 × 2K/4K）

用户问题的定量回答：当前产品终态（rans2 熵、flat 矩阵、444 含
ADR-C042 定标与色度偏移归零）下，各规格与 ProRes 同档的画质差与
码率差。三种读法：

1. **同档位**（产品语义）：P5/ADR-C032/C042 已把 tier 目标 bpp 定标
   为 prores_ks 同档实测 → 同档即同码率（实际落地 0.97~1.04×，
   见编码矩阵）；差距读画质列；
2. **同码率**（纯编码器效率）：Topos 在 ProRes 同档实测码率处的
   ΔPSNR（合成口径：422 = Y:U:V 0.5:0.25:0.25，444 = 1/3 均值）；
3. **同画质**（码率差回答）：Topos 固定 qp R-D 网格（档位 qp 中心
   ±4..±12，16 帧）在 ProRes 同档 PSNR 处插值 bpp → 码率比
   （harness `bench_iso_quality.py`，全部格点区间内插无外推）。

源与帧窗（与各批证据一致）：422 家族 = `{2K,4K}_prores422HQ.mov`
解码 yuv422p10le 第 300~315 帧；444 家族 = DNxHR 444 解码（10/12bit）
首 16 帧。**勘误**：4K 差距分解批（gap_decomp_2k_2026-09-11.json）的
2K 对照组实际源为 `2K_h264.mp4` 解码（当时 2K ProRes 素材尚不存在，
文档源标注有误），其 2K 行已被本批 v2（`gap_decomp_2k_v2_*.json`，
ProRes HQ 解码源）取代。

## 2K（1920×1080@25p）

| 档位对比 | ProRes 实测 | 同码率 Δ画质 | 同画质码率比 |
|---|---|---|---|
| proxy vs ProRes 422 Proxy | 0.78bpp / 40.5 Mbps | **+1.6 dB** | **0.76×（省 24%）** |
| lt vs ProRes 422 LT | 1.76bpp / 91.1 Mbps | **+1.5 dB** | **0.79×（省 21%）** |
| standard vs ProRes 422 | 2.49bpp / 129.3 Mbps | **+0.9~1.9 dB** | **0.87×（省 13%）** |
| hq vs ProRes 422 HQ | 3.76bpp / 194.7 Mbps | **+4.1~4.7 dB** | **0.77×（省 23%）** |
| pro444(10bit) vs ProRes 4444 | 5.61bpp / 290.9 Mbps | **+5.2~5.6 dB** | **0.61×（省 39%）** |
| pro444(12bit) vs ProRes 4444 | 5.61bpp / 290.9 Mbps | **+5.6 dB** | **0.61×（省 39%）** |
| extreme(12bit) vs ProRes 4444 XQ | 8.35bpp / 432.7 Mbps | **+6.7~7.3 dB** | **0.68×（省 32%）** |

## 4K（3840×2160@25p）

| 档位对比 | ProRes 实测 | 同码率 Δ画质 | 同画质码率比 |
|---|---|---|---|
| proxy vs ProRes 422 Proxy | 0.43bpp / 88.3 Mbps | **+0.6~0.7 dB** | **0.86×（省 14%）** |
| lt vs ProRes 422 LT | 1.42bpp / 293.6 Mbps | **+1.4~1.5 dB** | **0.75×（省 25%）** |
| standard vs ProRes 422 | 2.05bpp / 425.3 Mbps | **+1.2~1.4 dB** | **0.81×（省 19%）** |
| hq vs ProRes 422 HQ | 3.19bpp / 661.7 Mbps | **+0.9~1.5 dB** | **0.91×（省 9%）** |
| pro444(10bit) vs ProRes 4444 | 5.55bpp / 1151.3 Mbps | **+5.2 dB** | **0.73×（省 27%）** |
| pro444(12bit) vs ProRes 4444 | 5.56bpp / 1153.8 Mbps | **+5.7 dB** | **0.70×（省 30%）** |
| extreme(12bit) vs ProRes 4444 XQ | 7.60bpp / 1576.5 Mbps | **+5.3 dB** | **0.75×（省 25%）** |

## 读法与要点

1. **同码率画质差（Δ范围两值 = 产品反馈路径实测 / 固定 qp 曲线插值，
   互相印证）**：422 家族 2K +0.9~+4.7 dB、4K +0.6~+1.5 dB；
   444 家族 +5.2~+7.3 dB（2K/4K/10/12bit 一致碾压）。
   Topos 在全部 14 格无负点；
2. **同画质码率差**：422 家族省 9~25%、444 家族省 25~39%。换句话说
   达到 ProRes 同档画质，Topos 只需 0.61~0.91× 的码率；
3. **同档位语义**：tier bpp = prores_ks 实测定标（ADR-C032/C042），
   即"同档同码率"下白赚左列 dB——这是产品档位的设计选择（质量优先
   兑现，而非进一步压码率）；
4. 4K hq 是 422 家族最窄的一格（+0.9 dB / 省 9%）：高码率 + 高纹理
   内容下两编码器接近；2K hq 反而差最大（+4.1~4.7 dB）——2K 母带
   噪声重、ProRes 高码率档对颗粒的表示效率劣势被放大；
5. 444 家族的差距系统性大于 422：ProRes 4444/XQ 对全分辨率 chroma
   的表示成本高（ADR-C042 §2：其编码链已抹平 chroma 高频），Topos
   rans2+flat 在 chroma 全细节内容上同时赢码率与保真。

## 证据

- `tools/gap_decomp_4k_2026-09-11.json`（4K 422 锚点 + 反馈 matched）
- `tools/gap_decomp_2k_v2_2026-09-11.json`（2K 422 v2，ProRes HQ 源）
- `tools/align444_{2k,4k}_{10bit,12bit}_2026-09-11.json`（444 锚点）
- `tools/iso_{2k,4k}_{422,444}_2026-09-11.json`（等画质插值 + 同码率
  曲线插值，14 格；harness `bench_iso_quality.py`）
- 档位落地码率：`docs/codec/topos_tier_speed_matrix_2026-09-11.md`

---

**ADR-C043 加注（2026-09-11 晚）**：本表"同画质码率比"列已被采纳为
产品定标（保守规则 = 最差格 ×1.05）：六档 target_bpp 下调 4~24%，
档位语义从"ProRes 同档码率"切换为"ProRes 同档画质"。表内同码率
ΔPSNR 数据仍为有效技术事实（等效 ProRes 档位码率下的领先幅度）。
详见 ADR-C043。
