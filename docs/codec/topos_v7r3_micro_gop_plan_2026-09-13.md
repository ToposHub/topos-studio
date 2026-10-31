# LP 载体更换落地计划（V7-R3 帧间微 GOP：topos_v7r3_micro_gop_plan）

日期：2026-09-13 · 状态：**全部批次完成（2026-09-13 结项：批 0 bbb6a3a5 / 批 1 79fe2015 / 批 2 c88fa81b / 批 3 5cf7dac7 / 批 4 71eb071a / 批 5 本提交）**
决策依据：**ADR-C048**（载体换 V7-R2 同构子代际，D1~D6）；实测依据
`topos_lp_gate_review_2026-09-14.md` §5/§5.1（V8 载体编码 ~10× 慢 +
同 qp 体积代差 1.04~1.33×）、§5.2（R3 载体复测：4K 码率 −37%~−65% vs
LT、编码 ~2×）。
适用读者：全新会话的执行 agent——本文自包含。

## 0. 目标与非目标

**目标**：LP/帧间微 GOP 的写端换到 V7-R2 band 并行机器——4K 编码从
3~4 fps 到 ≥1.5× LT 实测口径（预期 25 fps 量级），同 qp 体积代差归零；
V9 全部资产冻结保留。

**非目标**：不改 §7 体积出货门（中位 <20% 的注册裁决是产品决策，换
载体不改写它）；不做运动估计（ADR-C047 维持）；不动 V9 读端行为。

## 1. 代际规格（批 1 冻结）

- cfg 选择子 `reserved[0]=11` → 帧头 **(major 7, entropy 8)**，三元组
  唯一 (8,0,0)；reserved[3]/[4]/[5] = frame_type/gop_id/ref_distance
  （字节 15/16-17/18，与 V9 同位同规则组：I=0/ref0，P=1/ref1，
  frame_type≤1 → UNSUPPORTED_VERSION，gop_id 全域 u16）。
- (7,6)/(7,7) 行为逐字节不变（V2.1 强制零规则组不动）；退役代际不受影响。
- 包结构 = 现行 V7 目录/slice 结构（scan/parse 走现行 V7 路径，
  parse_structure 对 major 7 已放行）。
- 读端：P 包无状态像素解码 → TC_ERR_STATE；查询模式开放；GOP 序列/
  参考事务复用 `topos_gop_context`（载体插头化）。
- 容器：tpcC major=7；mov 同步链/GOP 链校验条件扩展为
  「major 9 或 (major 7 ∧ em 8)」；stss 只列 I 语义不变。
- 能力面：manifest 增 `v7r3_micro_gop` 段（experimental，ADR-C048），
  先改 manifest 再实现；`ToposVideoEncoder gop='ip2'` 切写 em=11。

## 2. 批次分解（每批独立提交 + 全量门禁）

### 批 0：决策与计划 —— **✅ 完成（本文件 + ADR-C048 + INDEX）**

### 批 1：格式规则落地
- `tc_frame_config_validate`：reserved[0] 域 {0..11}；em=11 的
  reserved[3..5] 校验（与 em=10 同构：frame_type≤1、ref_distance 匹配）；
  `cfg_to_frame_header` em=11 → (7,8) + GOP 字段。
- `validate_impl`：major 7 分支接受 entropy 8（三元组 (8,0,0)）+ V9 式
  GOP 规则组；(7,6)/(7,7) 强制零不变。
- 解码入口：V7 路径遇 (7,8) P 包 → TC_ERR_STATE（查询 OK）。
- 单测 `test_v7r3_format.c`：域/规则组/截断/查询与 P-state/V8·V9 包
  互斥；既有 V7-R2 golden/测试零变化。

### 批 2：GOP context 载体插头化 + golden
- gop_context 的 scan/encode/decode 触点按代际分派（V9 → 现行 V8 机器；
  R3 → V7 目录 scan + frame_encode_shared + V7 解码）；几何视图统一。
- `golden_v73`（gen/check，I,P,P,I 序列 + 零漂锚 + MOV），对齐
  golden_v9 结构；V9 golden 零变化硬门。

### 批 3：容器
- mov 同步链/GOP 链条件扩展（D5）；`golden_v73` MOV 段（stss 只列 I、
  prev_sync、faststart）。

### 批 4：绑定/编码器/源/manifest
- manifest `v7r3_micro_gop` 段 + 门测试；绑定 em=11 cfg 支持；
  `gop='ip2'` 写端切 em=11（V9 写端保留为实验路径）；源读路径按 tpcC/
  包头分派 V9/V7-R3；Python 测试三层对齐。

### 批 5：复测 + 结项 —— **✅ 完成（2026-09-13）**

- `bench_lt_vs_lp_speed.py` 双载体口径重跑（`lt_vs_lp_speed_2026-09-13.json`）；
  复测数据与结论落门评审文档 §5.2。
- §7 体积门口径提醒：CQ qp72 vs LT sized 非等质量锚；LP 注册与否仍按
  §7 等质量线独立裁决（本窗口不动该门）。
- 遗留优化（登记 ADR-C048 §3）：残差合成/重建标量环的 SIMD/并行化
  （4K ~100-130ms → ~10ms，完成后 4K 编码预估 40~55ms/帧）。

## 3. 门禁（每批）

ctest 全绿 + pytest 三目录全绿 + **V1..V9 全部 golden SHA 零变化** +
ASan/UBSan 抽测（新规则路径）+ 每批独立提交（中文摘要含数据与判定）。
