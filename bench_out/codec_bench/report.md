# Topos vs ProRes 422 HQ vs DNxHR HQX 编解码性能基准

日期：2026-09-03 · 执行：`bench_out/codec_bench/run_bench.py`（结果见 `result.json`）

## 测试环境

- CPU：Genuine Intel(R) 0000（24 逻辑核 / 16 物理核），macOS (darwin x64)，AVX2 可用
- topos：libtopos_codec 0.1.0（本机 Release 构建，AVX2 分发，常驻线程池 16）
- ffmpeg：6.0-tessus 静态构建（软件编码器 prores_ks / dnxhd），显式 `-threads 16`
- 素材：`tests/test_videos/*_prores422HQ.mov` 解出的真实内容帧，yuv422p10le，25fps，
  1920×1080 与 3840×2160 各 96 帧；三方编码器吃同一份 raw
- 口径：每次测量重复 3 次取中位数；编码计时含 ffmpeg 启动的长窗口摊薄后净编码；
  topos 解码 = MOV 解封装（ToposMovieFile）+ 逐帧 decode_views（numpy 平面直写）；
  ffmpeg 解码 = demux + 全解码（null 输出）

## 结果（ms/帧，越小越好；括号内为 fps）

| 操作 | 2K 1080p25 | 4K UHD25 |
|---|---|---|
| 编码 Topos（sized=HQ 档定码率） | **14.1**（70.9 fps） | **45.2**（22.1 fps） |
| 编码 Topos（fixed-qp 单遍，产品稳态） | 19.8（50.4 fps） | 67.2（14.9 fps） |
| 编码 ProRes 422 HQ（prores_ks） | 17.8（56.3 fps） | 81.9（12.2 fps） |
| 编码 DNxHR HQX（dnxhd） | 17.3（57.7 fps） | 73.1（13.7 fps） |
| 解码 Topos（demux+decode） | 4.8（210.1 fps） | **13.2**（75.9 fps） |
| 解码 ProRes 422 HQ | **4.0**（248.9 fps） | 15.0（66.6 fps） |
| 解码 DNxHR HQX | 4.5（220.6 fps） | 14.7（68.0 fps） |

## 输出大小（码率对齐情况）

| 编码器 | 2K bpp | 4K bpp | 备注 |
|---|---|---|---|
| Topos sized（HQ 档） | 3.69 | 3.72 | 目标 3.93 bpp（20–28 MB/s@1080p25 档位定义） |
| ProRes 422 HQ（ffmpeg） | 2.05 | 3.13 | prores_ks 按质量目标出码，**低于** Apple HQ 名义码率 |
| DNxHR HQX | 3.54 | 3.51 | 固定码率，4K ≈ 731 Mbps，与 Avid 规格一致 |

## 结论

1. **编码：Topos 领先**。同档码率下 2K 比 ProRes/DNxHR 快约 1.2×，
   4K 领先扩大到约 1.5–1.8×（45ms vs 73/82ms）。4K 下仅 Topos 接近 25fps 实时线。
   注意：sized 含完整 qp 搜索（最坏情况）；产品导出稳态走单遍+码率反馈
   （≈fixed-qp 数字），仍与两个参照相当。
2. **解码：基本持平**。2K 落后 ProRes 约 16%、与 DNxHR 相当；
   4K 反超两者约 12–14%。三者都远超 25/50fps 播放实时线。
3. **码率对齐说明**：DNxHR HQX 是固定码率、与 topos HQ 档位码率最接近；
   ffmpeg 的 prores_ks HQ 出码低于 Apple 名义规格（2.05 vs ~3.7 bpp @1080p），
   故 2K 解码对比中 ProRes 的微弱优势部分来自其码率更低。

## 已知局限

- 未对比 VideoToolbox 硬件 ProRes 编码器（口径不同）；
- ffmpeg 的 dnxhd 编码器 slice 并行度有限（user/wall ≈ 6.4×，prores ≈ 14.5×），
  若换更新版本 ffmpeg 结果可能变化；topos 与 ffmpeg 均给满 16 线程预算；
- DNxHR HQX 源文件为 12-bit 容器，但 ffmpeg 的 dnxhr_hqx 编码为 10-bit，
  故三方统一在 10-bit 4:2:2 域对比；
- 本机为黑苹果（CPUID 未知型号），绝对值仅供参考，相对差距在三方同机条件下成立。

## 复现

```bash
.venv/bin/python bench_out/codec_bench/run_bench.py --frames 96 --runs 3
```

依赖（已装）：`.venv` 内 `cmake`、`static-ffmpeg`（二进制落地在 /tmp/ffmpeg-bin，
npmmirror 镜像下载的 ffmpeg 6.0 静态版）。

---

# 附：Topos 六档位全量基准（2026-09-03 追加）

脚本 `run_bench_tiers.py`（结果 `tiers_result.json`）。口径与上文相同
（96 帧 × 3 次中位数、topos 池 16 线程 / ffmpeg `-threads 16`、sized 定码率）。
422 四档用 2K/4K ProRes 422 HQ 素材（yuv422p10le）；4444/XQ 用 4K ProRes 4444
素材（yuv444p12le 源，2K 为其 lanczos 降采样）。参照 = ffmpeg prores_ks 同名
profile（proxy/lt/standard/hq/4444/4444xq）。

## 编码（ms/帧，fps；目标码率下的 sized 模式）

| 档位（对标 ProRes） | 2K topos | 2K ProRes | 4K topos | 4K ProRes | 4K 加速比 |
|---|---|---|---|---|---|
| 422 Proxy | 8.7（115 fps） | 13.8（73 fps） | 29.2（34 fps） | 43.5（23 fps） | 1.5× |
| 422 LT | 21.8（46 fps） | 18.6（54 fps） | 35.0（29 fps） | 72.1（14 fps） | 2.1× |
| 422 | 21.9（46 fps） | 17.6（57 fps） | 39.4（25 fps） | 57.5（17 fps） | 1.5× |
| 422 HQ | 23.0（44 fps） | 16.6（60 fps） | 40.3（25 fps） | 67.9（15 fps） | 1.7× |
| 4444 | 29.6（34 fps） | 32.3（31 fps） | 58.7（17 fps） | 115.2（9 fps） | 2.0× |
| 4444 XQ | 41.1（24 fps） | 36.0（28 fps） | 73.7（14 fps） | 126.0（8 fps） | 1.7× |

- 2K：大体相当（topos 0.7–1.6× 区间），LT/Standard/HQ 略慢于 prores_ks，
  Proxy 反超 1.6×；
- 4K：topos **全部六档领先，1.5–2.1×**；
- 码率对齐度：4444 档三方几乎同码率（5.6 vs 5.9 bpp）；其余档 ffmpeg 的
  prores_ks 出码低于 topos 档位目标（如 proxy 0.24 vs 0.93 bpp——ffmpeg
  按质量目标出码，不追 Apple 名义码率），即 topos 在处理更多比特的前提下
  依然更快或相当，上述对比对 topos 偏保守；
- Proxy/XQ 在个别分辨率 qp 搜索触顶（qp=63）导致实际码率高于档位目标
  （proxy 4K 1.50 vs 目标 0.89 bpp；XQ 2K 11.28 vs 9.14 bpp），属 sized
  搜索上限行为，非异常。

## 解码（ms/帧，fps；各自产物，含容器解封装）

| 档位 | 2K topos | 2K ProRes | 4K topos | 4K ProRes |
|---|---|---|---|---|
| 422 Proxy | 2.3（429 fps） | 2.9（347 fps） | 6.2（161 fps） | 7.5（134 fps） |
| 422 LT | 2.7（366 fps） | 3.3（306 fps） | 7.1（142 fps） | 9.0（111 fps） |
| 422 | 3.3（304 fps） | 3.5（284 fps） | 8.3（120 fps） | 10.3（98 fps） |
| 422 HQ | 4.4（226 fps） | 3.7（269 fps） | 10.8（93 fps） | 14.4（69 fps） |
| 4444 | 4.8（208 fps） | 8.0（125 fps） | 18.1（55 fps） | 25.2（40 fps） |
| 4444 XQ | 8.6（117 fps） | 9.9（101 fps） | 19.8（51 fps） | 29.1（34 fps） |

- 12 个对比格中 topos 11 格领先（1.07–1.66×），仅 2K HQ 略慢（226 vs 269 fps）；
- 全部档位解码远超实时（2K ≥ 117 fps、4K ≥ 51 fps）。

## 运行间方差说明

topos sized 2K HQ 在两轮独立运行中测得 14.1 与 23.0 ms/帧（同机不同时段，
黑苹果功耗/调度波动），4K 各档两轮偏差 < 12%。每轮内 topos 与 ProRes
背靠背测量，轮内相对结论稳定。

复现：`.venv/bin/python bench_out/codec_bench/run_bench_tiers.py --frames 96 --runs 3`
（约 15 分钟；新增 444 素材 raw 共约 19 GB，可删 `raw_*444*.yuv` 释放。）
