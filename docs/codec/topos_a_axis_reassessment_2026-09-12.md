# A 轴重判：GPU 数据面实测（kernel 重构 + PCIe/VRAM 归因，2026-09-12）

速度计划 v2 的 C 轴（C1~C4）落地后的复剖析 + A1 命门实验。
**核心结论：本机（i9-9900K + RX 6900 XT + PCIe）GPU kernel 本身已达标
（VRAM 驻留 0.44ms/平面，比 CPU 快 2~5×），但 CPU↔GPU 搬运成本是
结构性墙——A 轴的成立条件按语境重新划线。**

## 一、新鲜基线（C1~C3 后，2026-09-12，4K 3840×2160 444 10bit）

合成内容（testsrc2+mandelbrot 混合，频谱偏重，绝对值偏保守；
09-11 矩阵的真实素材源已清理，正式矩阵重跑待真实素材）：

| 指标 | 值 |
|---|---|
| 编码稳态 wall | **19.2~19.5 ms/帧（51~52fps，2.07×实时@25p）** |
| 编码首帧 sized | 冷 206ms / 热（qp hint）58ms |
| worker CPU 份额 | entropy（rans2）~63% / fill ~36% / crc ~1%（有效并行 ~10.6×） |
| fill 内部（v2 画像外推） | DCT ~12%带 / 量化 ~7.5% / token 扫描 ~7% |
| 解码 16t / 8t / 1t | 17.3 / 20.1 / 145.4 ms/帧（16t 扩展比 8.4×；8t→16t 仅 +16% = 带宽饱和） |
| 解码阶段份额（20 帧累计） | sink（反量化+IDCT，1/64 抽样估计）**66%** / entropy 34% |
| 内容统计 | nnz ≈ 17% AC；qp58 下 dc_only = 0（合成内容特征，真实素材 dc_only 会高） |

产品对照：422 家族早已 ≥1.5×实时；444 族是唯一痛点格族，现已 ~2× 实时。

## 二、MSL forward-DCT kernel 六变体（全部三方位精确）

`src/shared/gpu/metal/kernels_dct.py`（native F-cache == numpy 参考 ==
Metal 输出，3 几何 × 6 变体 = 18 例全过；tests/shared/gpu/metal 94/94）：

| 变体 | 思路 | Shared(系统内存) | Private(VRAM) |
|---|---|---|---|
| forward_dct | 原版（线程局部 int[8][8]×2） | 31~34 ms | 0.61 ms |
| forward_dct_rows | 行驻留、无 barrier | 8~12 ms | 0.70 ms |
| forward_dct_tg | group=块 64 线程、threadgroup 暂存 | ~5.9 ms | 0.87 ms |
| forward_dct_tg4 | tg + 4 累加器破链 | ~5.9 ms | 0.84 ms |
| forward_dct_prod | 预乘积表（constant 空间） | ~6.3 ms | 3.13 ms（负结果） |
| **forward_dct_m8** | **一组 8 块摊薄组固定成本** | ~6.5 ms | **0.44 ms** |

关键诊断链（4K luma 单平面，dispatch_sync p50）：

1. 骨架实验：无 MAC 骨架 6.15ms ≈ 标准 6.21ms ≈ 4×MAC 6.40ms
   ——计算在本 GPU 免费，地板在数据面；
2. Shared→Private 归因：**6ms 地板 = Shared 系统内存的 PCIe 往返**
   （49MB@8.2GB/s），非 kernel 问题；VRAM 驻留后同一 tg kernel
   5.9→0.87ms（6.8×）；
3. 组摊薄（m8）只在 VRAM 下兑现（barrier/组调度才成为次级瓶颈）。

## 三、传输账（Private + 显式拷贝口径，4K luma）

| 项 | 值 | 带宽 |
|---|---|---|
| 上载 15.9MB 系统→VRAM | 2.87 ms | 5.5 GB/s |
| DCT（m8）VRAM→VRAM | 0.44 ms | — |
| 读回 33.2MB VRAM→系统 | 5.0~5.3 ms | ~6.4 GB/s（宽载荷不破，PCIe 写向系统内存的墙） |

CPU 锚点：m7 prepare DCT ≈ 2.4ms/平面 wall（16t）；稳态 fill 融合
DCT ≈ 1.0ms/平面 wall。即 **VRAM kernel 比 CPU 快 2~5×，但一次
读回（5ms）就吃掉全部盈余**。

## 四、A 轴重判（按语境划线）

1. **A1（编码 DCT+量化 → GPU）纯 F-卸载形态在本机判死**：稳态 fill
   的 DCT 仅 ~3.1ms wall/帧，F（99MB）或量化系数（50MB）读回 8~15ms
   ——净亏。m7/sized 路径同理（探针反复消费 F，读回 × 探针数）。
   落档 §②"必须 F 驻留 + 量化同场"的判断经实测**成立**，且进一步
   收紧：token 扫描若不下放，中等体量回传同样吃掉收益。
2. **A1 存活形态**：(a) 统一内存机器（Apple Silicon——shared 存储
   即零拷贝，本报告的 PCIe 墙不存在，全部数字翻正）；(b) 完全驻留
   管线（DCT+量化+token 化同场，CPU 只剩 rANS，回传仅 token 流
   ~nnz 比例——工程量大，本质是 A3 的子集）。
3. **A2（解码 GPU 化）在编辑器语境成立且是当前最优立项**：解码
   输出即 GPU 纹理（编辑器显示路径，零回读）；CPU 符号环（~34%
   worker）产出稀疏量化系数（nnz 17% ≈ 数 MB~25MB/帧，上载可与
   符号环重叠），GPU 做反量化+IDCT（m8 同构 kernel，0.44ms/平面
   量级）+ 写纹理。sink 66% 移出 → 解码 ~17.3 → ~10ms（**~1.7×**，
   编辑器语境）。CLI/转码语境（需回读 47.7MB ≈ 8ms）打平——
   与计划 §七"按语境分别验收"一致。
4. **C5 触发线复测**：合成内容 r2e 份额 63% > 55% 门——但合成内容
   偏重（mandelbrot 高频），**需真实素材复测后定**（半天量）。

## 五、下一步建议（ranked）

1. **A2 编辑器语境解码 GPU 化一期**（2~3 批）：稀疏系数契约 +
   MSL 反量化/IDCT kernel（复用 m8 结构与三方对拍方法论）+ 纹理
   输出；验收 = 重建平面逐字节 + 编辑器路径 A/B wall。
2. **真实素材复测**（0.5 批）：C5 触发判定 + C1~C3 落地后的正式
   产品矩阵刷新（当前矩阵为 09-11 口径）。
3. **A1 集成批改为条件项**：触发条件 = 统一内存机器到位（Apple
   Silicon）或编辑器编码路径全面 GPU 驻留（与 Metal 迁移轨道
   Phase 汇合时）。本报告的 kernel 资产（6 变体位精确 + 差分机）
   直接复用。

## 六、证据

- 剖析脚本：/tmp/a2_prof/{enc_stage,dct_bench,dct_skel,dct_private}.py
  （dev 工具，关键数字已录入本文）；
- 测试：tests/shared/gpu/metal 94/94（含 DCT 18 例）；
- 稳态包样本：/tmp/a2_prof/steady.tpc（4K 444 qp58）。
