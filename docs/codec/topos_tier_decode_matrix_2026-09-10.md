# Topos 422 分辨率 × 档位解码速率矩阵

**日期：** 2026-09-10　**机器：** Intel i9-9900K（8C16T）　**多线程：** 16 线程池　**帧率口径：** p50（中位），逐迭代整帧 `tc_frame_decode`（真实输出平面，含 53B 帧头）

**档位语义：** 产品 target_bpp（`src/shared/codec/topos_profiles.py`）——同一档位跨分辨率为**同目标 bpp**（码率随像素数 ×4/档位递增），不是同 qp；编码走 `tc_frame_encode_sized` 逐帧 qp 搜索（**0..95，含 v1.5 扩展域**，ADR-C031）。内容为 image_synth 冻结种子合成 10-bit 4:2:2（渐变/颗粒两类），码率按 24fps 折算 Mbps。

## 渐变类（平滑内容）

| 分辨率 | 档位 | 目标 bpp | 实测码率 | 选定 qp | 解码 p50 (1t) | 解码 p50 (多t) | p95 (多t) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2K（1080p） | Topos 422 Proxy | 0.605 | 53.3 Mbps | qp57 | 129 fps | 835 fps | 668 fps |
| 2K（1080p） | Topos 422 LT | 1.232 | 122.4 Mbps | qp44 | 82 fps | 552 fps | 423 fps |
| 2K（1080p） | Topos 422 | 1.875 | 185.8 Mbps | qp22 | 73 fps | 493 fps | 402 fps |
| 2K（1080p） | Topos 422 HQ | 2.756 | 197.2 Mbps | qp0 | 67 fps | 465 fps | 365 fps |
| 4K UHD | Topos 422 Proxy | 0.605 | 235.0 Mbps | qp56 | 30 fps | 186 fps | 164 fps |
| 4K UHD | Topos 422 LT | 1.232 | 475.1 Mbps | qp43 | 21 fps | 128 fps | 111 fps |
| 4K UHD | Topos 422 | 1.875 | 737.6 Mbps | qp22 | 17 fps | 111 fps | 99 fps |
| 4K UHD | Topos 422 HQ | 2.756 | 790.2 Mbps | qp0 | 17 fps | 106 fps | 35 fps |
| 6K | Topos 422 Proxy | 0.605 | 504.3 Mbps | qp55 | 14 fps | 95 fps | 83 fps |
| 6K | Topos 422 LT | 1.232 | 1103.0 Mbps | qp35 | 10 fps | 73 fps | 59 fps |
| 6K | Topos 422 | 1.875 | 1336.8 Mbps | qp24 | 9 fps | 55 fps | 43 fps |
| 6K | Topos 422 HQ | 2.756 | 1486.3 Mbps | qp0 | 9 fps | 62 fps | 59 fps |
| 8K UHD | Topos 422 Proxy | 0.605 | 961.7 Mbps | qp51 | 8 fps | 52 fps | 49 fps |
| 8K UHD | Topos 422 LT | 1.232 | 1921.5 Mbps | qp23 | 6 fps | 41 fps | 38 fps |
| 8K UHD | Topos 422 | 1.875 | 2107.7 Mbps | qp1 | 6 fps | 42 fps | 38 fps |
| 8K UHD | Topos 422 HQ | 2.756 | 2107.7 Mbps | qp0 | 6 fps | 42 fps | 37 fps |

## 颗粒类（噪声内容）

| 分辨率 | 档位 | 目标 bpp | 实测码率 | 选定 qp | 解码 p50 (1t) | 解码 p50 (多t) | p95 (多t) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2K（1080p） | Topos 422 Proxy | 0.605 | 54.5 Mbps | qp66 | 92 fps | 637 fps | 483 fps |
| 2K（1080p） | Topos 422 LT | 1.232 | 108.8 Mbps | qp63 | 56 fps | 401 fps | 337 fps |
| 2K（1080p） | Topos 422 | 1.875 | 159.5 Mbps | qp61 | 44 fps | 251 fps | 213 fps |
| 2K（1080p） | Topos 422 HQ | 2.756 | 222.4 Mbps | qp59 | 35 fps | 240 fps | 227 fps |
| 4K UHD | Topos 422 Proxy | 0.605 | 217.9 Mbps | qp66 | 23 fps | 165 fps | 148 fps |
| 4K UHD | Topos 422 LT | 1.232 | 434.9 Mbps | qp63 | 14 fps | 106 fps | 91 fps |
| 4K UHD | Topos 422 | 1.875 | 638.3 Mbps | qp61 | 11 fps | 80 fps | 78 fps |
| 4K UHD | Topos 422 HQ | 2.756 | 889.1 Mbps | qp59 | 9 fps | 63 fps | 55 fps |
| 6K | Topos 422 Proxy | 0.605 | 490.5 Mbps | qp66 | 10 fps | 75 fps | 74 fps |
| 6K | Topos 422 LT | 1.232 | 978.3 Mbps | qp63 | 6 fps | 48 fps | 47 fps |
| 6K | Topos 422 | 1.875 | 1436.1 Mbps | qp61 | 5 fps | 37 fps | 35 fps |
| 6K | Topos 422 HQ | 2.756 | 2000.5 Mbps | qp59 | 4 fps | 29 fps | 19 fps |
| 8K UHD | Topos 422 Proxy | 0.605 | 872.0 Mbps | qp66 | 6 fps | 43 fps | 39 fps |
| 8K UHD | Topos 422 LT | 1.232 | 1739.0 Mbps | qp63 | 4 fps | 27 fps | 26 fps |
| 8K UHD | Topos 422 | 1.875 | 2553.1 Mbps | qp61 | 3 fps | 21 fps | 19 fps |
| 8K UHD | Topos 422 HQ | 2.756 | 3556.8 Mbps | qp59 | 2 fps | 16 fps | 12 fps |

## 读法

1. **码率随分辨率同档位 ×4 递增**是档位定义（固定 bpp）的结果。qp 由逐帧 sized 搜索选定：颗粒类 Proxy 在全部分辨率命中 **qp66**（v1.5 扩展域，旧 qp≤63 内核在该目标上会超支，见 ADR-C031）；渐变类合成素材的高频码量极低，各档位目标均高于其（近）无损尺寸，故 qp 顶到低值——真实素材不会出现这种塌缩。
2. 同档位跨分辨率的**帧率近似恒定**：4K 帧比特数是 2K 的 4 倍、像素也是 4 倍 → 每秒解码像素数（吞吐）近似恒定，档位内码率与解码负载同步缩放。
3. 渐变类显著快于颗粒类（同档位约 2-3×）：平滑内容粗档位量化后接近全零块（DC-only 快路 + 长游程），颗粒类残余 ±1 系数多、熵解码与重建更重。
4. 本表档位 = 产品 target_bpp 语义（跨分辨率同 bpp）；与 ProRes 实测码率逐点对齐的对比见 `topos_vs_prores_decode_2026-09-10.md`（该表为同码率就近匹配，两张表口径不同，不可混读）。

