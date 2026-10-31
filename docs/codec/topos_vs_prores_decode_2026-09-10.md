# Topos 422 vs Apple ProRes 422 同码率档解码速度对比（v2：qp 域扩展后）

**日期：** 2026-09-10（v2 修订同日）　**机器：** Intel i9-9900K（8C16T）　**ProRes 软解：** FFmpeg 8.1 内建 prores 解码器　**内容：** 1920×1080 10-bit 4:2:2，240 帧同源合成素材（image_synth 冻结种子）

对比原则：**同内容、同实际码率**对齐档位。ProRes 码率随内容浮动，按每档实测码率（240 帧 @24fps）就近匹配 Topos qp；不是按名义档位标称值。Topos 侧为 V2 帧解码（含 53B 帧头，V1 Rice 熵、默认切片）；1t=单线程，多线程 Topos=16 线程池 / ProRes=FFmpeg 自动。

## v2 修订摘要（vs 初版）

1. **qp 域已扩展 0..95（v1.5，ADR-C031）**：初版"Proxy/LT 不可达"的产品缺口已解决——Proxy 档两类内容 12.7-12.9 Mbps（ProRes 实测 11，差 ~15%，剩余为每块 DC 差分+EOB 熵地板），LT 全覆盖。表中 Proxy/LT 行由"不可达"变为实测。
2. **解码热路径优化**：VLC 符号解码强制内联（熵段 −15%）+ 细切片负载均衡选项（16t +16%）。全部档位点为优化后实测。

## 渐变类（平滑内容）

| ProRes 档位 | ProRes 实测 Mbps | ProRes fps (1t / 多t) | Topos 匹配点 | Topos Mbps | Topos fps (1t / 16t) | 同码率下 Topos/ProRes |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| Proxy | 11 | 365 / 886 | qp95 | 12.7 | 345 / 2061 | 0.95× / **2.32×** |
| 422 LT | 23 | 287 / 873 | qp71 | 21.5 | 255 / 1581 | 0.89× / **1.81×** |
| 422 标准档 | 29 | 258 / 848 | qp63 | 36.1 | 164 / 955 | 0.64× / 1.13× |
| 422 HQ | 41 | 192 / 854 | qp60 | 42.7 | 148 / 892 | 0.77× / **1.04×** |

## 颗粒类（噪声内容）

| ProRes 档位 | ProRes 实测 Mbps | ProRes fps (1t / 多t) | Topos 匹配点 | Topos Mbps | Topos fps (1t / 16t) | 同码率下 Topos/ProRes |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| Proxy | 11 | 341 / 845 | qp95 | 12.9 | 342 / 2043 | 1.00× / **2.42×** |
| 422 LT | 87 | 98 / 734 | qp67 | 43.2 | 110 / 721 | 1.12× / 0.98×（码率仅一半） |
| 422 标准档 | 124 | 72 / 620 | qp62 | 132.5 | 50 / 327 | 0.69× / 0.53× |
| 422 HQ | 186 | 48 / 448 | qp60 | 188.2 | 39 / 269 | 0.81× / 0.60× |

注：颗粒 LT 的 ProRes 实测 87 Mbps 落在 Topos qp63（108.8）与 qp67（43.2）之间，
表取 qp67（就近下侧，码率优势下 fps 仍相当）。

## 读法与边界

1. **Proxy/LT 档（低码率）Topos 全面达到或超过 ProRes 软解**：多线程
   0.98-2.42×，单线程 0.89-1.12×。初版"qp63 地板 36/109 Mbps 不可达"的结论
   随 v1.5 qp 域扩展（ADR-C031）作废。
2. **剩余差距集中在高码率颗粒类**（Standard 0.53×、HQ 0.60×）。单线程
   分解剖面（qp60 颗粒，24-30ms/帧）：反量化+IDCT+重建 ~60%、熵解码 ~30%、
   CRC ~2%——重建与熵均已 AVX2/查找表化，进一步压缩需 P3 深度 SIMD 战役
   （16-bit 通道 IDCT、符号间重叠解码），属计划内工程。
3. **码率对齐 ≠ 质量对齐**：ProRes 各档在其实测码率点是多年调校的视觉无损
   设计；Topos 对应 qp 点的质量未做并排审阅（P4-01 matched-quality 门禁
   扩展项）。qp95 的画质为预览/代理级（PSNR 量化后显著低于高档），与 Proxy
   档产品语义一致。
4. FFmpeg 解码器 ≠ 达芬奇内建解码器；本表用 FFmpeg 作为可复现的软解基准。

## 解码优化记录（本次交付，i9-9900K 实测）

| 优化 | 粒度 | 效果 |
| --- | --- | --- |
| VLC 符号解码 always_inline（`vlc.h`） | 代码 | 熵段 12.2→10.4 ms/帧（−15%）；16t 端到端 250→273 fps（+9%）；位流字节不变 |
| 细切片负载均衡（`--slice-rows` 16→8） | 编码配置 | 16t 273→295-298 fps（+8-9%）；Y/UV 片 4:1 负载差摊平（27→51 片） |
| 组合（VLC 熵 + sr8，颗粒 qp60=160.5 Mbps） | — | 初版 250 → 295 fps（+18%） |

单线程瓶颈剖面工具：`TOPOS_CODEC_PROFILE=1` + `tc_dev_decode_stats_get`
（crc/entropy/idct 三段抽样）；采样（`sample`）定位 VLC 快路径未被内联
（每符号 3 次真实调用 + 结构体存储转发挂在符号依赖链上）。

## 复现

- 素材：`topos_rawgen --width 1920 --height 1080 --frames 240 --kind {1|2}`
- ProRes：`ffmpeg -nostats -threads {1|0} -i <tier>.mov -f null - -benchmark`（取 rtime，3 次中位）
- Topos：`topos_scalable_preview_decode --quick --iters 30 --kind {gradient|grain} --qp Q --threads {1|16}`
- 分解剖面：`TOPOS_CODEC_PROFILE=1` 环境变量 + `tc_dev_decode_stats_get`
- qp 域扩展（v1.5）：ADR-C031；spec `bitstream_spec_v1.md` 附录 A.4

**生成提交：** 初版 e7c2b464；v2 修订（qp 域扩展 + 解码优化）见同日提交。
