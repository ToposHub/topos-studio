# Topos 编解码速度优化计划 v2（2026-09-12）

v1 计划（`topos_speed_optimization_plan_2026-09-11.md`）P0~P2 已收口：
S4（编码免 idiv，ADR-C044）+ S5 一期（后向环专化，ADR-C045）落地，
位流逐字节不变；矩阵终态编码 +3.0~25.4%（4K 444 10bit 0.90→**1.03×
实时**、12bit 1.00×、extreme 0.92×），解码全格持平 = µarch 地板。
本文回答"下一步还想更快，空间在哪里"，并给出可执行立项表。

---

## 一、终态画像（S4+S5 后复剖析，2026-09-12，4K 444 pro444 16t sample）

| 成分 | 占比 | 状态 |
|---|---|---|
| rans2 编码核（`color_tokens_rans2_encode`，含内联 put 引擎/后向环/collect） | **~72% band** | 后向环贴串行依赖地板（3 符号操作 + suffix ≈30 cycles/pair） |
| └ put 引擎 + 后向环自体（内联） | ~47%×r2e | S5 一期后无法规化，仅换基座可破 |
| └ collect_joints | ~14%×r2e | 融合判负（ADR-C045 §5），访存地板 |
| fill（DCT 12% + 量化 7.5% + token 扫描 7% band） | ~25~30% band | AVX2 nat 已在线；GPU 可接管 DCT+量化 |
| **per-slice scratch alloc/free + madvise 链** | **~5% worker 时间（新发现）** | 池化可消——本计划 C1 |
| sized 搜索（m7 探针，摊销） | ~9%（首帧/GOP 头） | 探针与最终路径同构，仅搜索策略可调 |
| Python 绑定/驱动 | ~6% wall | 批 API 可减 |

解码（M0.3 后未变，代码与位流均冻结）：sink 47~53%（单块 dinv
43~48%）+ 符号环 47~53% + 装配 <2%——**C 路径四方向全关 = 本 µarch
单线程地板**（ADR-C041/C045）。

## 二、剩余空间定性：三条轴 + 一组小项

单线程 C 代码（本 µarch + 本位流格式）的免费午餐已吃完。剩余空间
按"换什么"分轴：

1. **换执行基座（GPU/Metal）——唯一的大窗口**。解码侧 ADR-C041 §4
   已指认；编码侧 DCT/量化天然 per-block 并行且 F-cache 架构
   （`fill_color_band_from_f`，DCT-once）已把变换与熵编码解耦。
2. **换位流格式（V8 窗口）**。v1 全程位流冻结；交错多状态 rANS
   在 CPU 1t 判 NO-GO（C041：4-lane 慢 24%）的形态恰是 GPU 需要
   的（每线程多 lane 隐延迟）——格式收益绑定 GPU 轴，不单独立项。
3. **换机器/ISA（AVX-512+）**。DCT/量化/IDCT 内核可 2× 宽，符号环
   仍延迟地板——温和收益，条件项。
4. **系统级位精确小项（本 µarch 可做）**：scratch 池化、搜索摊销、
   绑定开销、多线程扩展性。

## 三、与 Metal 并行轨道的关系（边界声明）

`docs/metal_backend_migration_and_cross_platform_gpu_plan.md`（另线
维护）**§2.5 已明确 native codec 不纳入其第一阶段**——其范围为编辑器
管线（源解码/调色/composite/UI 表面）。本计划拥有 codec 编解码轴，
互补不重叠；可复用其 Phase 4 已落地的 `MetalComputeContext`
（MSL 编译/dispatch/buffer/texture 绑定/fence）经验与基建。

## 四、立项表

预注册晋级门沿用 v1/ADR-C041 体例：系统级交错 A/B ≥+3% 才入库
（位精确项）；GPU/格式项走独立验收协议（见各行）。

### C 轴：系统级位精确小项（本 µarch，1 批内可清）

| 编号 | 项 | 位流 | 预期 | 量 | 状态 |
|---|---|---|---|---|---|
| C1 | per-slice scratch 池化：r2e 每带 `tc_alloc/tc_free`（madvise+首触页错误链 ~5% worker）；per-worker-slot 高水位复用池 | 位精确 | +2~4% 编码 wall | 0.5 批 | **已落地（2026-09-12）**：`topos_c1_scratch_pool_2026-09-12.md`——合成 A/B/A +1.7%（真实内容待复测）、madvise 链归因消失、V7-R/R2/A 稳态零分配测试门；V3/V6 intra 后置 |
| C2 | sized 搜索摊销：探针数自适应/跨帧 qp hint 模型强化 | 位精确（码控决策不变性需差分验证 qp 轨迹） | 首帧 92→<60ms（摊销 9%→<6%） | 0.5 批 | **已落地（2026-09-12）**：`topos_c2_sized_hint_2026-09-12.md`——双工作点弦线模型，换目标搜索 14→3 探（差分钉死 qp 轨迹）；冷启动路径未改善（负结果记录），收益集中在码率轨迹变化场景 |
| C3 | Python 绑定/驱动开销（~6% wall）：逐帧 ctypes 编组减量/批量参数 | 位精确 | +1~3% wall | 0.5 批 | **已落地（2026-09-12）**：`topos_c3_binding_overhead_2026-09-12.md`——`_encode_scratch` 稳态重建修复，4K 产品路径稳态 35.3→23.0ms（−35%，实测归因远超 ~6% 估计）；绑定/媒体测试全过 + RSS 浸泡口径重校 |
| C4（原 S6） | 4K 444 解码 16t 扩展性调查（3.8× vs 8K 7.3×；worker 86% 忙 = 带宽/尾部失衡） | — | 3.8→≥5× 则多线程吞吐 +25%+（单流地板不变） | 0.5 批调查 | **已完成（2026-09-12）**：`topos_c4_decode_scaling_2026-09-12.md`——3.8× 为矩阵无状态 decode 测量口径伪影（每帧 ~95MB 串行 churn）；native 实测 8.5×（1t 与矩阵吻合）；连带修复无状态 decode 平面零填（45.1→28.3ms），目标经口径修正即达成 |
| C5（原 S7） | 小 nnz 融合内核特化 | 位精确要求 | ≤15% | **已收口（2026-09-12）**：`topos_c5_r2e_stage_profile_2026-09-12.md`——开题首务六段剖析发现未识别大项（r2e 尾段逐字节刷写循环 19.3%×r2e）→ `tc_bitwriter_put_bytes` 批量追加修复，位精确 12/12 SHA + **A/B +6.4%** 编码墙钟；原标的"小 nnz 特化"判定不成立（后向环 75.5% + collect 17% 均已判死、fill 仅 ~11% band 且 AVX2 在线）→ C5 收口，编码剩余空间确认仅在 A 轴 |
| C6（原 S8c） | alpha 解码 SIMD | 位精确 | alpha 路径 | 条件项 | alpha 产品化时 |

### A 轴：GPU/Metal 后端（大窗口；依赖 Metal 基建，估）

| 编号 | 项 | 位流 | 预期（估） | 量 | 依赖 |
|---|---|---|---|---|---|
| A1 | 编码 GPU 化一期：DCT+量化 → F-cache 移交 Metal compute（per-block 并行；编辑器语境帧本已驻 GPU 免上传） | **位精确**（同数值同序，差分钉死） | fill 的 ~20% band 移出 CPU + 与熵环重叠 → 编码 wall −10~20% | 2~3 批 | **阶段交付 2/3（2026-09-12）+ 集成批改条件项（2026-09-12 重判）**：`topos_a1_dct_gpu_phase1_2026-09-12.md`（差分机 + 位精确 kernel）+ `topos_a_axis_reassessment_2026-09-12.md`——kernel 重构达成（VRAM 0.44ms/平面，6 变体 18 例位精确），但 PCIe 搬运墙实测（读回 6.4GB/s）判纯 F-卸载净亏；触发条件 = 统一内存机器（Apple Silicon）或编码路径全面 GPU 驻留 |
| A2 | 解码 GPU 化一期：CPU rANS 出 token → GPU 反量化+IDCT+写回（sink 47~53% 移出；编辑器语境输出直留 GPU 纹理） | 位精确（重建平面逐字节） | 解码 1.3~1.8×（符号环保留 CPU） | 2~3 批 | **阶段交付 2/3（2026-09-12）**：`topos_a2_decode_gpu_2026-09-12.md`——稀疏 CSR 契约 + 4 变体 kernel 三方位精确（真实包+合成）+ native q 捕获差分机（tc_dev_qcap_*）；4K VRAM 实测 w1 kernel 0.24~0.28ms/平面（空 kernel 地板 0.163 之上计算近免费）、稀疏上载 12MB/平面可重叠；编辑器语境投影 ~2.4×（好于立项估）；**阶段交付 3/3 + 生产批（2026-09-12）**：`topos_a2_decode_gpu_2026-09-12.md` §六/§七——C 直产 CSR 端到端位精确；**§七 生产融合发射落地**：rANS2 环内直出 CSR（无稠密中转/63 扫描）+ conceal 语义闭合（CSR 清零 = GPU mid）+ 直发目标 API（pinned 零拷贝，每帧零 getter/上载）+ wsd kernel（连续 off/soff/dc0）；4K 444 qp58 实测：CPU 18.5→11.8ms（−36%），帧流水 **1.57×**（串行 0.82× = CLI 读回墙不变）；投影修正 ~1.6×（原估 ~2×），剩余 CPU = 符号环自体 → 移交 C5/A3；编辑器 GUI 接线 = 产品化工作 |
| A-enc | 编码 token GPU 化：DCT→量化→扫描→token 化上 GPU，CPU 只跑后向 rANS 环（稀疏 token 下行替代稠密 F——A2 契约反转） | 位精确 | 编码 wall −10~20%（估） | 2 批 | **批 1 判条件项（2026-09-12）**：`topos_a_enc_token_gpu_2026-09-12.md`——差分机（tc_dev_set_tok_capture 族含 fastdiv 孪生单一真相源）+ `quant_token` kernel（组=块布局）三方位精确 4/4（DC 预测带局部语义钉死）；**本机生产判负**：4K qp58 token 7.12M 对 = 28.5MB u32 下行地板 ≥5.2ms vs 被替代 fill ~2.7ms 墙钟 → codec 轴净亏 ~3ms/帧（kernel 实测 11.3ms/平面 = Shared pairs PCIe 写墙 5.5GB/s，与 A1 同源）；正收益条件 = 编辑器 GPU 驻留语境（省 47MB 平面下行）或统一内存机器——同 A1 判式，资产保留 |
| A3 | V8 格式窗口：交错多状态 rANS（GPU 多 lane）+ 批量重建原生化（M10-2B 脚手架转正） | **非位精确**（格式版本机：conformance/golden/oracle 全套重铸） | GPU 解码 2~4×、符号环并行化 | 3~5 批 | **批 1 差分机成立 + 链数规格量化（2026-09-12）**：`topos_a3_v8_gpu_rans_2026-09-12.md`——`rans2_decode_slice` kernel（每线程一条 slice 完整 rANS 链，cum 二分替 LUT、DC 预测回读 dense dc、conceal 寄存）+ `tc_dev_packet_slices` 导出（同源归一化表）；差分 9/9 位精确（GPU CSR == C 融合发射 + token 全程不出显存端到端 == CPU 参考 + conceal 寄存）；4K 实测链数反比线性（51/204/405 链 = 1088/329/169ms vs CPU 26ms）——V7 slice≤512 上限锁死 0.15×，**外推 ≈2900 链 CPU 平价、8000 链 1.5×（V8 核心规格 = 熵流千级 + 段表共享 + CRC 移出关键路径）**；生产开关绑定 V8 窗口（C041 CPU 域负结果不适用 GPU 域） |

### B 轴：平台/ISA（条件项，换机触发）

| 编号 | 项 | 预期 | 触发 |
|---|---|---|---|
| B1（原 S8a/b） | AVX-512：DCT/量化/IDCT 内核 2× 宽 + NT-store（8K 16t ~5~10%） | 内核类 1.3~1.6×，整机 +15~30%（符号环不变） | AVX-512 机器到位 → 矩阵重跑后按数据定 |

## 五、路线图与优先级

1. **C1（scratch 池化）先行**——本 µarch 上最后的确定性位精确收益，
   半批量，独立过门；（✅ 已落地）
2. **A2（解码 GPU 化一期）升为主攻（2026-09-12 重判，原 A1）**——
   编辑器语境（输出即 GPU 纹理）零回读、sink 实测 66%、kernel 结构
   已位精确验证；解码 ~1.7× 预期；CLI 语境打平需在验收时单独记账；
3. **A1 集成批转条件项**——PCIe 搬运墙实测后纯 F-卸载判亏；统一
   内存机器（Apple Silicon）或编码全面驻留时重启，kernel/差分机
   资产保留复用；
4. C2/C3 已落地；C4 已完成；C5 待真实素材触发复测；B1 换机时启动；
5. **A3（V8 格式）仅在 A2 验证 GPU 解码形态成立后开题**——格式机
   成本高，先证收益后动刀。

## 六、明确不做（v1 关闭项 + 本轮新增，防重开）

交错多状态 rANS **CPU 域**（C041）、解码融合路径（S1）、lazy LUT/
装配（S2）、批量重整化、融合 LUT、寄存器驻留补丁、slice_rows 布局
sweep、T16 变换（C037）、RDO 门控（C040）、sparse/batch IDCT **CPU
贴牌**（M10-2B）、**collect 融合入 fill**（C045 §5，2026-09-12）、
rows_cost/模型行加速（<1% 不可见）。位流格式 v1 域内冻结不变。

## 七、诚实边界

- 本 µarch（i9-9900K Skylake）+ 现行位流格式下：编码 1.03~1.08×、
  解码 0.9×（4K 444）接近 C 域终态——v1 已用四方向证据钉死；
- 数量级提升（2×+）只存在于换基座（A 轴）或换格式（A3）或换机
  （B 轴），三者均已在上表立项并给出触发/依赖条件；
- A 轴预期数字全部标注"估"——编辑器语境（帧驻 GPU、输出即纹理）
  与 CLI 语境（需上载/回读）收益差异显著，立项时按语境分别验收；
- GPU 语境的传输成本（PCIe 上载 4K 帧 ≈ 47MB ≈ 3~5ms）对 25p 单流
  可隐藏，对 8K/多路需实测——A1 批首任务即测传输账。

## 八、证据索引

- v1 终态与矩阵：`topos_tier_matrix_c043_2026-09-11.md` 后记 1~3、
  `tools/tierspeed_*_s5_2026-09-11.json`；
- 本轮复剖析：/tmp/p0f/v2_enc.sample（2026-09-12，16t 12s，
  r2e 72.6% band / fill 30.3% / scratch+madvise 链 ~5% worker）；
- 关闭判据：ADR-C041/C044/C045、topos_s1_rans2_fusion_ab、
  topos_speed_p0_profiling（M0.2/M0.3）。
