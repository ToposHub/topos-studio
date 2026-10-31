# Topos 编解码速度全面提升计划（2026-09-11）

用户指令："全面提升解码编码的速度"。本文是该轴的总计划：基线、瓶颈
画像、优化项清单（每项含预期/风险/验收门）、分阶段路线图。结论先行：

1. **空间存在且可量化**——解码侧 Topos 仅 0.32~0.61× DNxHR（ffmpeg
   SIMD VLC）、0.5~1.0× prores_ks；4K 444 三格是全矩阵唯一亚实时格
   （编码 0.84~0.91×、解码 0.9×@25p）。编码侧对两个竞品全面领先
   （2.1~8.3×），短板只在 4K 444 格。
2. **空间不在"换 SIMD IDCT"**——AVX2 融合反量化+逆变换内核、SSE2
   整行存储、DC-only 闭式填充**均已存在**（runtime dispatch，
   `src/simd/transform_avx2.c`、`src/codec/color_store.h`）。旧口径
   "SIMD IDCT ~15%" 实为小 nnz 融合内核特化残余（ADR-C009 判不启）。
3. **真实空间在两处**：(a) 产品默认 rans2 解码路径**没有**V1/V2 独享
   的融合 scan-to-plane 结构（逐块函数指针 sink + memset + 单块
   IDCT）；(b) rans2 **per-slice 固定装配成本**（每 slice 堆分配 +
   全量上下文模型 LUT 重建）在产品 qp 62~87 域占比可观。编码侧从未
   系统剖析（M0 补），已确认一处原语级候选：编码环逐符号除法。
4. **符号原语环已定性收官（不要重开）**——交错多状态/批量重整化/
   融合 LUT/寄存器驻留四候选全部负结果（ADR-C041 +
   topos_decode_scan_opt_2026-09-11.md），本微架构（i9-9900K
   Skylake）上单状态 rANS 已达实用平台。下一代窗口 = 新 ISA/平台
   迁移（Metal 并行轨道，独立计划）。

## 一、基线（C043 定标，2026-09-11，60 帧真实素材）

编码 = 纯调用计时；解码 = 满配 16t 逐帧 p50 五轮；实时 = 25p。
证据 `tools/{tierspeed,tierdec}_*_c043_2026-09-11.json`、
`tools/dnxhr_*_2026-09-11.json`。

| 轴 | 2K（×实时） | 4K（×实时） | 解码 1t（fps） |
|---|---|---|---|
| 编码 proxy→extreme | 299→67（12.0→2.7×） | 76→21（3.0→**0.84×**） | — |
| 解码 proxy→extreme | 211→88（8.5→3.5×） | 54→23（2.1→**0.9×**） | 2K 88→18；4K 23→5 |
| 解码 vs DNxHR | 0.53~0.61× | **0.32~0.53×** | — |

亚实时格 = 4K pro444（10/12bit）编码 0.90/0.91×、extreme 编码
0.84×、4K 444 家族解码 0.9×。其余全格 ≥1.6× 实时。

## 二、瓶颈画像（现有证据汇总）

### 解码（剖析口径：rans2，1080p 真实素材，`sample` 采样）

| 素材点 | rANS 符号循环（内联原语） | AVX2 融合内核 | CRC 等 |
|---|---|---|---|
| qp63（产品域 62~87 内） | ~45% | ~52% | ~3% |
| qp44（高码率对照） | ~69% | ~27% | ~4% |

已关闭（**勿重开**，见 §四 P3）：交错多状态（4-lane 比单状态慢 24%，
ADR-C041）、批量重整化（系统 0%）、融合 u32-LUT（微基准 +23% → 系统
−1~6%，LUT 足迹压 L2）、寄存器驻留（编译器 SROA 已完成）、slice 布局
sweep（<2%）、16t 内存带宽天花板 7.3×@8K（结构性的，非调度问题）。

尚未尝试的结构性候选（本计划主攻）：

1. **rans2 解码路径缺融合结构**：V1/V2 Rice/VLC 走
   `tc_color_scan_to_plane_reduced`（`src/entropy/color_scan.h:266`，
   批 4 IDCT 累积 :459-469、稀疏收集 :379-437、sink 内联、EOB 快路
   径）；V7-R/R2 却经函数指针 sink `tc_c1_store_sink`
   （`src/bitstream/slice_codec.c:432`）→ `tc_color_store_block_reduced`
   逐块调用，含逐块 `memset(blk,0,64)`（slice_codec.c:637）与**单块**
   `c->dinv`（color_store.h:503），无批 4/稀疏路径。rans2 与 VLC 实测
   解码速度相当（±3%）——这一平价恰恰说明 sink 开销正被符号环差异
   抵消，把 VLC 的结构给它 = 白拿的差值（上限待 M0.3 定）。
2. **per-slice 固定装配成本**：每 slice 堆分配 LUT 块（`tc_alloc`
   slice_codec.c:612）+ **全量**模型 LUT 重建（dc_ctx_n+1+lvl_ctx_n 个
   模型 × 4096 表项，:617-619），无论该 slice 是否触达该模型。产品
   qp 62~87 域块内非零系数极少（DC-only 命中 ~50%+），假设大量 lvl
   模型从未使用（M0.3 验证）→ lazy build + 池化为位精确减负。4K 444
   每 slice 数更多（3 平面 × ~17 band = ~51 slice/帧），该项在最需要
   提速的格上最重。
3. **4K 444 16t 扩展性异常**：1t 6fps → 16t 23fps 仅 3.8×（8K 天花板
   7.3×；帧越小越差：2K proxy 仅 2.4×）——与"大帧带宽受限"方向相反，
   指向固定成本占比/并行粒度，未调查过。

### 编码（**从未系统剖析**——M0.2 补）

已确认的事实与候选：

- sized 搜索已最优（strict：F-cache DCT-once + 探针只做 quant+token+
  计数不写码流，p50 ≤4 探针，`codec.c:4295-4438`/`m7_probe :3675`）；
  首帧后走反馈单遍——码控成本不是短板；
- **rans2 编码环逐符号除法**：`e->x = ((e->x / f) << 12) + (e->x % f)
  + cum`（`src/entropy/rans.c:129`），u32 idiv ~26 cycle/符号，倒数值
  预计算（libdivide 式不变量除法）位精确可证；
- 4K 444 F-cache 流量假设：int32/系数 × 3 全分辨率平面 ≈ 100MB/帧
  写 + 探针×4 + 终编读回 ≈ 0.5GB/帧级访存（22.6fps 时 ~11GB/s），
  可能占编码时间可观份额（M0.2 验证）；
- C042→C043 下调码率后 4K 444 编码 +12~13%——证明 token/熵体积敏感，
  符号环与 token 化路径值得剖析。

## 三、优化项清单

格式：编号｜内容｜位流影响｜预期（系统级，待门验证）｜工作量。
**所有候选沿用 ADR-C041 沉淀的晋级门：先原型基线对账生产形态，后
系统级交错 A/B（≥+3% 才入库），禁止微基准直判。**

| 编号 | 项 | 位流 | 预期 | 量 |
|---|---|---|---|---|
| M0.1 | 基准协议固化：TOPOS_CODEC_LIB=build/release 钉死（run_tests.sh 的 pytest 钉的是 debug -O0 构建，勿混）；prof_decode 交替 A/B 工具链复用 | — | 地基 | 0.1 批 |
| M0.2 | 编码侧首次剖析（sample + 阶段统计，勘误口径教训）：4K 444 编码热点分解（平面准备/F-cache/量化+token/rans2 环/搜索收尾） | — | 定方向 | 0.2 批 |
| M0.3 | 解码 rans2 专项剖析：sink 调用/memset/dinv/LUT 重建/模型行解码分开计时 + lvl 模型触达率统计（lazy build 前提验证） | — | 定 S1/S2 上限 | 0.2 批 |
| S1 | ~~rans2 解码融合路径移植~~ | — | **P1 批关闭（REJECT，2026-09-11）**：位精确差分通过但系统级 A/B 噪声级（−1.1~+3.4% 混合），未过 +3% 门；sink 占比主体是 AVX2 内核本体时间，可消除成本（调用边界/memset/DC-only 快路）各 <1%。解码 C 路径四方向全关（符号原语/内核/装配/块结构），**解码速度 = 本 µarch 地板**，提升窗口只剩 Metal/新 ISA。见 topos_s1_rans2_fusion_ab_2026-09-11.md | — |
| ~~S2~~ | ~~per-slice 装配减负：LUT 池化 + lazy build~~ | — | **P0 关闭（REJECT）**：装配成本实测 <2% 不可见（topos_speed_p0 §M0.3），原计划批次取消 | — |
| S4 | rans2 编码除法消除 | **位精确**（逐字节同流） | **已交付（ADR-C044，2026-09-11）**：fastdiv round-up magic 域推广证明；A/B 1t +8.6~9.3% / 16t +4.1~6.0%，位流零变化 | ✅ |
| S5 | 4K 444 编码实时专项：~~第二靶 = collect_joints+精确代价模型（P0 实测 38%）~~ **靶心勘误（S4 后复剖析）**：38% 桶主体 = 后向环自体（rows_cost <1% 不可见）；一期已交付；二期（collect 融合）已试 | **位精确**（逐字节同流） | **一期已交付（ADR-C045）**：16t +8.7~10.2% / 1t +13.0~15.2%，4K 444 ≈1.08× 实时。**二期 REJECT（同日）**：fill 对环融合 A/B +1.2/−4.7/−4.7% 未过门回退——pos 链在 ctz 紧环关键路径，加价 > collect 重扫价；负结果 ADR-C045 §5 防重开 | ✅（一期） |
| S6 | 4K 444 解码 16t 扩展性调查：decode_worker_budget 核查、per-slice 串行成本占比、并行粒度（444 平面×band 切分） | — | 3.8×→≥5× 则解码 +20%+ | 0.5 批调查 |
| S7 | 小 nnz 融合内核特化（≤15% 上限，bit-exact 风险高） | 位精确要求 | ≤15% | **条件项**：仅当 P1 落地后重剖析内核占比 >55% 再议（ADR-C009 原判不启） |
| S8 | NT-store / AVX-512 / alpha 解码 SIMD | — | 5~10%（仅 8K 16t）/ 未知 | **条件项**：8K 多路需求 / 换新 ISA 机器 / alpha 产品化时 |

明确**不做**（已判死，防重开）：交错多状态 rANS（ADR-C041 NO-GO）、
批量重整化、融合 LUT、寄存器驻留补丁、slice_rows 布局 sweep、16×16
变换（C037）、RDO 门控（C040）。位流格式不动——速度轴所有立项均
位精确，新熵模式窗口已随 ADR-C041 关闭。

## 四、分阶段路线图

### P0 地基（1 批）：M0.1 + M0.2 + M0.3 —— **已完结（2026-09-11）**
交付 `topos_speed_p0_profiling_2026-09-11.md`：编码 79% 在 rans2
token 路径（除法环 41%）；解码 sink 47~53%（单块 dinv 43~48%）；
装配 <2%（**S2 关闭**）；DCT/F-cache/搜索均非热点。S5 靶心修订为
collect_joints+代价模型（38%）。

### P1 位精确重构（S1 关闭后仅剩 S4，2026-09-11 修订）—— **S4 已交付**
ADR-C044：编码稳态 1t +8.6~9.3%、16t +4.1~6.0%（三产品格一致），
位流逐字节不变；差分/定向 ctest/A-B 门全过。**S1 已关闭**
（topos_s1_rans2_fusion_ab）：解码侧无剩余 C 代码项。P1 出口判据
（编码 444 格 +5%）已达成。

### P2 4K 444 编码实时化（1~2 批）：S5（+S6 调查）—— **已收口（2026-09-11）**
S4 后复剖析勘误靶心：P0"38% 桶"精确拆分 = 后向环自体 ~37% band
（模型分支/argmin 双精度比较在环内 + e->x 栈往返 + pos_block 无条件
前缀和），collect_joints 11%，**rows_cost/代价模型 <1%（不可见）**。
一期 = 后向环按 lvl_model 专化 + 值语义 put 引擎 + pos_block 条件化：
A/B 16t +8.7~10.2% / 1t +13.0~15.2%，位流逐字节不变；4K 444 稳态
37.0 ms/帧 ≈ **1.08× 实时 @25p**。**二期（collect 融合入 fill 单遍）
已试未过门**（+1.2/−4.7/−4.7%，ADR-C045 §5）回退——编码轴 C 代码
项至此全部收口：**出口判据 1.15× 未达，诚实终态 1.08×**，剩余差距
在位精确 C 域无已立项项（后向环贴串行地板、fill 判负、collect 判负），
窗口 = Metal/新 ISA 轨道。**解码目标撤回**（本 µarch 地板，见 S1
报告 §4）——4K 444 解码 0.9× 为平台终态，改善窗口 = Metal 轨道。

### P3 条件项与长期窗口（默认不启）
S7/S8 触发条件见上表。**Metal/GPU 后端为并行轨道**，独立计划文件
（`docs/metal_backend_migration_and_cross_platform_gpu_plan.md`，
另条线维护），不在本计划范围；ADR-C041 §4 已指认其为下一代解码
收益窗口。

## 五、目标与诚实边界

| 指标 | 基线 | P1 后（S4） | P2 终态（S5 一期） | 出口判据 |
|---|---|---|---|---|
| 4K 444 编码 | 0.84~0.91× | ≥0.95× | **≈1.08×（收口）** | 1.15× 未达（诚实边界：C 位精确域无剩余项） |
| 4K 444 解码 | 0.9× | 撤回（平台终态） | 撤回 | 撤回（Metal 轨道） |
| 全矩阵解码 | 0.9~8.5× | 撤回 | 撤回 | 撤回（Metal 轨道） |
| 全矩阵编码 | 0.84~12× | +3~8% | +3.0~+25.4%（矩阵重测落档，tier 矩阵后记 3） | 4K 444 10bit 1.03× 跨实时 |

诚实边界：DNxHR/prores_ks 解码平价（0.32~0.61× → 1.0×）在**本微
架构上不可达**——符号环已达平台（ADR-C041 判定）+ mezzanine 算法
密度差（ProRes 变换动态范围小、舍入简单）。达平价的路径是平台迁移
（Metal 轨道）或新 ISA，不是继续压 C 代码。本计划的可交付承诺 =
P1+P2 的位精确收益与 4K 444 实时化；预期数字全部以 P0 剖析和系统
A/B 门为准修订。

## 六、验证资产（每批必过）

- 回归：`bash native/topos_codec/run_tests.sh`（debug/asan/ubsan/
  tsan/fuzz 五配置，158 ctest）+ `tests/conformance/` golden 位流
  `check` 模式逐位；
- 位精确差分：新旧 dylib 对同批实包解码逐位 diff（重构类）/
  编码输出逐字节 diff（S4）；
- 速度：prof_decode 交替 A/B（1t ±1% 纪律、16t 仅参考）+
  `tools/bench_tier_decode.py`/`bench_tier_speed.py` 干净进程产品格
  矩阵（每批出 ADR + 报告，负结果照写）。

## 七、代码锚点索引

- rans2 解码块环/装配：`src/bitstream/slice_codec.c:552,609-619,635-722`
- 融合参照（V1/V2）：`src/entropy/color_scan.h:266,334-483`；reduced
  sink/单块 dinv/DC-only：`src/codec/color_store.h:473-503`
- AVX2 内核/分发：`src/simd/transform_avx2.c:439,572`、
  `src/simd/dispatch.c:182-217`、每帧 resolve `src/codec/codec.c:5317`
- rans2 编码环/除法：`src/codec/codec.c:1838-1999`、
  `src/entropy/rans.c:104-131`
- sized 搜索/F-cache：`src/codec/codec.c:3411-3440,3675-3807,4295-4438`
- 线程池/预算：`src/common/tpool.c:254-261`、`src/shared/codec/
  decode_worker_budget.py`、`slice_threads_policy.py`
- 构建（测量钉 release）：`CMakeLists.txt:98-116`、
  `run_tests.sh:136-161`、`src/shared/codec/topos_binding.py:1128-1173`
