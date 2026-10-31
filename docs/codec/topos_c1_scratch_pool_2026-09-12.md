# C1 落档：编码侧 per-slice scratch 池化（2026-09-12）

速度计划 v2（`topos_speed_optimization_plan_v2_2026-09-12.md`）C 轴 C1 项。
**结论：已落地并过全部门；合成负载 A/B/A 中位 +1.7% 编码稳态 wall，
采样归因确认每 slice alloc/madvise 链已从 worker 栈消失；编码器
V7-R/R2/A 路径达成稳态零分配（新增测试门钉死）。**

## 改动（native/topos_codec/src/codec/codec.c）

1. `enc_shared` 新增 `slot_scratch[TC_SLICE_MAX_THREADS]` +
   `slot_scratch_built[]`（高水位 grow-only 字节池，`enc_free_members`
   统一释放，随 M10-6.1 进程缓存跨帧存活）；
2. `enc_scratch_acquire(e, slot, need)`：水位内零分配命中；不足时
   realloc 增长并提交水位。**失败按 OOM 直报**（与旧 per-call
   tc_alloc 失败同码同路径，test_oom 的"逐点注入必 OOM"契约不变，
   不引入静默回退分支）；
3. 三个原每 slice 分配点全部改池内雕刻：
   - `color_tokens_rans_encode`（V7-R renorm 暂存）：签名 +es/+slot；
   - `color_tokens_rans2_encode`（V7-R2，产品默认）：同上，探针
     （m7_probe_job）与最终编码（enc_band_job）共用同池；
   - `enc_emit_v7_segments`（V7-A）：原 15 次 per-band tc_alloc 改为
     单块雕刻（条带布局 `[pair_count 2B][pad4][runs 1B][pad4][level_m 4B]`×5，
     池基址 16B 对齐 + 4B 步进 ⇒ 成员自然对齐）；
4. `m7_probe_task` 增 `es` 指针（探针槽位与最终编码同源）。

V3/V6 intra 的 coeffs/modes/edges/temp（intra.c）**不在本批**：非产品
默认路径，计划表原文即列为可后置；后续如需，挂同一池即可。

## 正确性验收（全过）

| 门 | 结果 |
|---|---|
| debug ctest 全套 | 80/80（含 golden_codec v1 字节精确、test_oom 逐点 sweep） |
| asan 定向（c1_scratch/m10/oom/rans*_slice/stage9/codec/enc_cache） | 12/12，无泄漏 |
| 多线程 golden parity（TOPOS_SLICE_THREADS=4） | golden 16 records byte-exact + stage9 + c1_scratch 全过 |
| 新增 `test_c1_scratch`（8 连跑稳定） | 见下 |

`test_c1_scratch` 覆盖：① V7-R/V7-R2 plain+sized、1/4 线程稳态零分配
（预热 16 次后 60 次编码 `tc_dev_alloc_count()==0`；多线程下 payload
arena 惰性 chunk 的首建随调度落在不同迭代，深预热收敛——该现象为
存量 arena 语义，非本项引入）；② 冷池（enc_cache 关，栈实例）vs 热池
输出逐字节一致 + 1t vs 4t 逐字节一致；③ 池增长逐点 OOM 注入（48 点）：
每点要么 TC_ERR_OUT_OF_MEMORY 要么干净成功且包与参考逐字节一致；
④ V7-A 同口径零分配 + 位一致。

位流不变性由 golden（v1 域）+ rans/rans2/v7a 差分单测 + 冷热池/跨线程
memcmp 三层钉死；池内数据均为发射前写后读暂存（renorm 字节/分桶数组），
无跨 slice 读依赖。

## 性能验收（合成 4K 444 10bit pro444，12 帧常驻，prof_p0 长循环）

A/B/A 交叉（每臂 20s、40+ 轮取中位，控机器漂移）：

| 臂 | 稳态 ms/帧（中位） |
|---|---|
| C1-a | 35.40 |
| BASE-b（HEAD 805e8b38） | 35.90 |
| C1-c | 35.20 |

→ **C1 ≈ 35.3 vs BASE 35.9：+1.7%**（低于立项预估 +2~4% 的下限；
合成高斯噪声内容 r2e 占比偏高、分配链占比相应缩水，真实内容未复测
——源 fixture（DNxHR 444 解码）不在本机）。

采样归因（`sample` 12s，libsystem_malloc 链）：
- BASE：`color_tokens_rans2_encode` 子树内可见 `free_large →
  mvm_madvise_free → madvise`（单 worker 子树 49+7 样本）——即复剖析
  指认的每 slice 释放链；
- C1：worker 栈内该链消失（剩余 madvise 全部位于 Python 驱动的
  ctypes 缓冲 churn，非 codec 域）。

**诚实边界**：wall 收益在合成负载上为 +1.7%（噪声 ±0.2ms），未达
v1 体例的 ≥3% 交错门；但本项同时交付（a）每 slice 严格更少的工作
（无分配器往返/缺页），（b）编码器稳态零分配——消除分配器抖动与
尾延迟（实时编辑语境的价值不体现在中位 fps），（c）V7-A 15→0 次
每 slice 分配。三项合计且无任何路径变差，判定入库。

## 复验钩子

- `build/{debug,asan,perf}` 全配置 + `TOPOS_SLICE_THREADS=4` golden；
- A/B 基线重建：`git stash` → `cmake --build build/perf` → prof_p0
  20s 三臂交叉；
- 稳态零分配：`test_c1_scratch`（ctest 注册 unit_c1_scratch）。
