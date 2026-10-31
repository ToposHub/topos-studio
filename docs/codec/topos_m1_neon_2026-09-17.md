# Topos Codec Apple M1 (arm64) NEON 内核补齐（2026-09-16/17）

M10-8「ARM NEON 对齐」首批交付：把 M1 上跑标量回退的编解码热路径补齐到
NEON，与 x86 AVX2 后端对齐。位流格式、API、线程行为零变化。

## 背景

x86_64 dispatch 有 12 个 AVX2 内核；arm64 原有 4 个 NEON 内核（前向变换
i16/i32 + 逆采样）。缺口（`src/simd/dispatch.c`）：

| 热路径 | x86 | M1 现状（本次前） |
|---|---|---|
| 融合反量化+逆变换（解码 sink） | AVX2 融合 + ac_rowmask 行稀疏跳过 | 标量 compose，**行稀疏跳过丢失** |
| quant_zz / quant_nat（编码） | AVX2（fastdiv 分解） | 标量 |
| 全块逆变换 | AVX2 | 标量（维持 ADR-C009 决策，不动） |
| 色彩转换 packed | AVX2 | 标量（后续批次） |

M1 画像（`TOPOS_CODEC_PROFILE=1`，8 worker CPU 累计）：解码 dequant_idct
≈100 ms/f > entropy ≈76 ms/f（4444xq）；编码 fill（含 forward+quant）
≈96 ms/f > entropy ≈68 ms/f——与缺口一致。

前置修复：`transform_neon.c` 此前从未在真 arm64 编译过（缺
`entropy/scan.h` include + `vmulq_s64` 在 Apple clang 12 缺失），
commit `bd358bb70`。

## 交付

- `b3f1b3835` **tc_dequant_inverse_8x8_neon**：AVX2 融合内核同构移植。
  4×i64 lane 表达为 2×int64x2；乘法全走 `vmull_s32` 32×32→64 加宽
  （p ≤ 2^25、|W·M| < 2^18、16-bit 分解分量均落 i32 域）；恢复
  ac_rowmask 行稀疏跳过（高 qp 档主流形态：低频聚集、高频整行零）；
  对称输出 + round32 符号恢复同源。
- `79e95468c` **tc_quant_block_zigzag_neon / _nat_neon**：fastdiv
  q=(n·magic)>>51 恒等分解经 `vmull_u32`/`vmull_high_u32` 承载；
  nd/ne 用**有符号** max/sub（num−dz 可负，无符号回绕破坏语义）；
  符号恢复走「加符号位再异或」补码恒等；zigzag gather 标量装载
  （NEON 无 gather）。
- `test_stage9` dispatch API 用例补 arm64 分支（历史断言假设「无 AVX2
  即 scalar」，在 NEON 后端存在的 arm64 上恒假——与本批内核无关的
  既有盲区）。

## 验证（全部通过）

- 差分：`test_transform` / `test_codec` / `test_stage9`（1t + 4t）。
- Golden 全变体 byte-exact：codec v1/bd12/pf444/gbr/profiles/traw/
  v2_vlc/v7r2_intra + transform + bitstream。
- **编码确定性跨内核不变**：六档整片重编码与冻结基准 MOV
  （`tests/test_videos/christmas_1080p_*.mov`）逐字节一致。
- ctest 85/85（含 fuzz replay / conformance / CLI）。

## M1 实测（同条件交替 A/B）

方法：基线 dylib（`d3e80c020` worktree 构建）与新 dylib 经
`TOPOS_CODEC_LIB` 交替运行同一基准（christmas 1080P 真实素材，
901 帧/档，常驻 ToposDecoder + 复用平面；编码只计 codec 调用），
各 2 轮取最优。**无风扇 M1 Air 对并发负载极敏感（基线跨日差
1.7×），跨轮比较无效——必须同条件交替**（昨日负载下的对比曾把
hq/4444xq 的解码增益完全掩盖成"持平"）。

| 档位 | 解码 base→new | 提升 | 编码 base→new | 提升 |
|---|---|---:|---|---:|
| 422 Proxy | 204.8 → **378.7** | +85% | 274.7 → **421.6** | +53% |
| 422 LT | 169.4 → **267.8** | +58% | 204.5 → **275.2** | +35% |
| 422 Standard | 154.4 → **223.3** | +45% | 174.0 → **222.7** | +28% |
| 422 HQ | 133.8 → **175.1** | +31% | 141.6 → **172.5** | +22% |
| 4444 | 85.1 → **116.0** | +36% | 86.9 → **106.7** | +23% |
| 4444 XQ | 76.4 → **103.5** | +35% | 76.1 → **89.4** | +17% |

（fps；8 切片线程；两轮复测偏差 <6%。）

增益形态与理论一致：解码增益随 qp 升高（行稀疏跳过占比增大）在
proxy 最大；编码增益随档位升高递减（entropy 占比增大，量化相对
变小）。

## 批次 2（同日）：稀疏结构优化 + 输出散射 NEON

批次 1 后的符号级画像（proxy 解码叶函数采样）：`tc_dequant_inverse_8x8_neon`
58%、`tc_color_store_block_xy` 21%、`tc_color_rans2_decode_core` 16%。关键
事实：实拍颗粒素材在 qp78 下几乎每块仍有 ≥1 个 AC（DC-only 闭式快路命中
≈0），但均值仅 0.92 个/块——**内核大头是"极稀疏块在跑全结构"**（16 行
B 初始化 + 全零行 16-bit 分解 + pass3 8×8 全巡掩码检查）。

两项优化（`同日批次 2` commit）：

1. **内核稀疏结构**（transform_neon.c）：活动行紧凑列表（act[]/nact）
   ——pass B 与 16-bit 分解只跑活动行（行内即分解，非活动行不初始化）、
   pass3 按 act 迭代免掩码检查；表懒生成挪到 resolve 时一次完成
   （`tc_transform_neon_tables_ensure_public`，M10-1d 同 AVX2 模式）。
2. **输出散射 NEON**（color_store.h，`TOPOS_SINK_NEON`）：像素写入整行
   vst1q_u16（vqmovun_s32 饱和域 [0,65535] ⊇ clamp 域，bd≤16 全域精确
   ——无 SSE2 packs 的 4095 门禁）+ DC-only 常数填充快路。

验证同批次 1 全套（差分 1t/4t、golden 10 变体、ctest 85/85、全片解码
0 concealed slice）。

**同条件交替 A/B**（各 2 轮取最优，基线 = 批次 1 后 `316ceef0a`）：

| 档位 | base → new | 提升 |
|---|---|---:|
| 422 Proxy | 324.3 → **576.1** | +78% |
| 422 LT | 252.9 → **347.1** | +37% |
| 422 Standard | 207.8 → **283.5** | +36% |
| 422 HQ | 175.6 → **224.5** | +28% |
| 4444 | 105.1 → **137.9** | +31% |
| 4444 XQ | 96.9 → **119.1** | +23% |

相对批次 1 前的原始标量基线，1080P 解码累计：proxy **2.8×**、lt 2.05×、
standard 1.84×、hq 1.68×、4444 1.62×、xq 1.56×。

## 批次 3（同日）：Topos 导出流水线转换——实现正确、实测负收益、默认停用

时间线导出一直在向所有 codec 传 `pipelined_conversion=True /
convert_workers=3`（FFmpeg 路径 4K 实测 12→20fps 的机制），Topos 编码器
此前只是忽略该字段。本批次补齐实现（与 `FFmpegVideoEncoder` 同契约：
后台转换线程 + 有界队列背压 + close drain 保序 + 转换错误延迟至 drain；
planar 直通帧内联快路免两次线程交接），修掉两个实现级坑：

1. **`_cvt_bufs` 转换缓冲复用与流水线互斥**（gop_seg 分段并行当年同类
   坑）：prepare → encode 不再同帧完成，复用缓冲被下一帧转换覆写 →
   撕裂输入 → 运行间非确定输出。守卫补 `not self._pipelined`。
2. **close 排空**：哨兵改阻塞 put（有界队列满时 put_nowait 丢哨兵 →
   工人永堵）；FIFO 排空按提交计数收尾（EOF 语义不可靠，60s 空等
   曾致单 worker 路径 74s 病态收尾）。

**实测（M1，1080P standard，packed BGR48 300 帧）**：流水线 61.9 /
65.9 fps（w1/w3）vs 同步 87.4 fps——**慢 ~25%**。根因：Topos 的 native
融合转换（行分片）与编码**共用进程级线程池**，"重叠"只是同池工作交错 +
线程交接/每帧新分配开销，不产生新容量。FFmpeg 路径的收益来自 swscale
与 libx264 的分立线程池，前提在此不成立。

处置：实现保留（sync/pipe×{1,3} 输出逐字节一致已钉死，137 项导出测试
通过），默认停用保持同步语义；`TOPOS_ENCODER_PIPELINE=1` 显式启用
（4K / 分立资源形态待验证）。编码端到端的下一个真实收益面在
**驱动层**：源解码/合成与编码的流水线（不在编码器内部）。

## 批次 4（同日）：熵解码 fc 融合 LUT——实测中性，回退；SoA 判死

批次 2 后画像（standard 解码采样）：`tc_color_rans2_decode_core` 38.5%
（新第一）、`tc_dequant_inverse_8x8_neon` 34.9%、输出散射已被批次 2 压到
3%。对熵解码做了热路径实验：`tc_rans_lut` 增加 fc 融合码字表
（fc[s]=(freq−1)<<12|cum，256B 常驻 L1），符号解码从 3 次依赖装载
（sym LUT→freq[s]→cum[s]）降为 2 次。

**实测（M1 稳态轮）：±1.5% 打平，性能中性 → 按纪律回退。** 根因：
freq/cum 数组仅 128B 本就 L1 常驻，M1 乱序执行掩盖依赖装载延迟——
rANS 解码瓶颈在乘法+字节重整化，不在装载。（测量方法论教训：无风扇
机身连续负载下热漂移 5–17%/轮，交替顺序设计会与热漂移完全混叠——
只有"双方均达稳态"的整块对比可信。）

**SoA 四块批处理（原后续方向）判死**：`codec.c` 既有 A/B（4K 密素材，
2026-09-01）——SoA4 比逐块劣化 15~25%，默认禁用；逐块内核已是
lanes=x 的向量化形态，SoA 重组不减乘法数、反加 staging/转置。注释所指
"后续 IDCT 收益需 ARM NEON 对齐"即批次 1/2 已交付内容。

## 总结与剩余收益面

codec 执行后端（本文件批次 1/2）后，M1 上该分解形态的向量化已接近
上限；实测排除了 SoA 批处理、编码器内流水线、fc 融合 LUT 三条路径。
剩余收益面均不在"再向量化"：

1. **驱动层流水线**：导出循环里源解码/合成与编码重叠（资源分立，
   与编码器内同池竞争不同）。
2. **熵代际**（格式变更工程）：rANS 交错流等——战略选项，非优化。
3. 应用层播放链（Python wrapper 开销、预读调度）——另一战场。
