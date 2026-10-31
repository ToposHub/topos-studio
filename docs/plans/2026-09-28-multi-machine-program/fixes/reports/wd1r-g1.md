# 批 D 重派 G1 修复报告（wd1r-g1）— BENCH-0003/0007/0008/0013/0014/0017 + AUD-14100

- 日期：2026-09-29
- 修复人：worker wd1r-g1（原 Wave D1 G1 的重派；原组账号用量中断，其
  AUD-16205/40014 已被收编，不在本批）
- 仓库：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`
- 任务简报列了 7 个 ID（文字写"8 条"与枚举不符，按枚举的 7 条处置）。
- 白名单遵守：新建 `tools/bench_wrapper_binding_overhead.py`、
  `tools/bench_default_config_playback.py`、`tools/bench_q01_rd_curve.py`、
  `tools/bench_thread_pivot_gate.py`、`tools/check_regression_anchors.py`、
  `tests/benchmark/test_fix_bench_wd1r_g1.py`、
  `bench_out/anchor_registry/20260929T155633Z/anchor_registry.json`（登记产物）；
  文档注记 `docs/benchreports/2026-09-28/{playback,quality,codec,native-threading,REGRESSION_BASELINE}.md`
  （全部为追加式追注，原数据行零改动）。**未改动 src/ 任何文件**；未做任何
  git 分支操作，未 commit/push；REMEDIATION_PLAN.md 未动（orchestrator 记账）。
- 同仓库并行组（wd1r-g4/g8 的 composite_controller/page_controller/adapter
  及其测试与报告）不在本批范围，本组未触碰。
- 实测环境说明：本机 dylib 内嵌 commit `22ad555f` ≠ 工作树 HEAD `df513ccd`
  ——四只测量脚本的小样本实测按 P0-04 宽松断言（`--expect-commit 22ad555f`
  + `--allow-dirty`）完成，输出全部重定向 `/tmp`（已清理），未污染
  `bench_out/`；无该旗标的正式测量被 manifest 断言按设计拒绝（实测验证）。

## 结论总览

| 编号 | 严重度 | 状态 | 交付物 |
|------|--------|------|--------|
| AUD-14100 | Major/bug/M | **FIXED**（上轮已随 fa2b7c2c 收编落地，本轮复核验证） | plan_compiler `_compile_set_fade` 抛 `CompileError`（含 op_id/kind）+ schema 层 fade 字段存在性校验 + 2 条回归测试（tests/edit/ai_editing/test_taep_contracts.py） |
| BENCH-0003 | Major/perf-opportunity/L | **FIXED** | `tools/bench_wrapper_binding_overhead.py`（wrapper/native 成对交替 A/B + bootstrap 95% CI + 预注册判据 + `--max-overhead` 回归门禁）+ playback.md §6.1 追注 |
| BENCH-0007 | Major/risk/M | **FIXED** | `tools/bench_default_config_playback.py`（同 harness 双配置臂：`readahead_auto` 全默认 vs 显式深度，唯一差异可数值复合——即缺失的"过渡运行"）+ playback.md §8.2 追注 |
| BENCH-0008 | Major/risk/M | **FIXED** | 同上脚本落盘 §4.2 decode 侧遥测（decode_ms avg/p50/p95/p99、late_frames、dropped_frames（节拍消费模式）、queue_depth、readahead stats）+ `unmeasured_fields` 诚实声明（GPU/呈现侧须离屏 GL harness）+ playback.md §8.3 追注 |
| BENCH-0013 | Major/risk/M | **FIXED** | `tools/bench_q01_rd_curve.py`（冻结语料 ≥5 质量点双臂 RD 扫描 + 决策文档要求的码率匹配/画质匹配两表 + 预注册 Q04 判定）+ quality.md §3.1 / codec.md §3 追注 |
| BENCH-0014 | Major/perf-opportunity/L | **FIXED** | `tools/bench_thread_pivot_gate.py`（线程轴边际收益 + p95 预算 + entropy/fill 份额的预注册判定门，五种 verdict）+ native-threading.md §4 追注 |
| BENCH-0017 | Major/process/M | **FIXED**（实测执行） | `tools/check_regression_anchors.py`（20 锚点来源逐文件 SHA-256 登记 + P0-04 树分类 + 缺失即 exit 1 完整性门禁）+ 首跑登记产物 `bench_out/anchor_registry/20260929T155633Z/anchor_registry.json` + REGRESSION_BASELINE.md §0 追注 |

---

## 1. AUD-14100 [Major/bug/M] — FIXED（复核验证）

**问题**（batch-141 证据）：`_compile_set_fade` 缺 fade 值时抛裸 `ValueError`，
穿透 `TransactionService.preview()` 的 `except CompileError` 边界；schema/
validator 均不要求 fade 字段，畸形 plan 以 VALID 放行到 compile 才爆。

**处置过程（重派核验）**：读当前 HEAD 发现修复**已在库**——`git log -S`
定位到 `fa2b7c2c`（Wave D1 orchestrator 的"adopt completed fixes"提交，
FIXLOG 将 AUD-14100 列在待重派清单，但 diff 实际包含其完整修复）：
1. `src/features/edit/ai_editing/plan_compiler.py:598-634`：
   `_compile_set_fade(op, p)` 改收 `op`，缺 fade 值抛
   `CompileError(op.op_id, "set_clip_fade", ...)`（docstring 注明 AUD-14100）；
   `_compile_operation` 调用点同步传 op。
2. `src/features/edit/ai_editing/schema.py:264-276`：
   `validate_plan_structure` 对 SET_CLIP_FADE 要求 fade_in/fade_out 至少
   其一（结构校验层即产出可修复诊断）。
3. `tests/edit/ai_editing/test_taep_contracts.py:866-893`：2 条回归测试
   （compile 层 CompileError 携带 op_id；schema 层无 fade 字段拒绝）。

**验证**：`pytest tests/edit/ai_editing/test_taep_contracts.py -q` →
**42 passed**；邻接 `tests/edit/ai_editing/test_transaction_service.py`
（消费端 except CompileError 边界）→ **29 passed**；合并电池 161 passed。
按证据建议范围核对：AUD-14101（`_compile_operation` 的宽 ValueError/TypeError
转换）与 AUD-14106（validator 层值约束）是独立条目，不在本批，未扩scope。

## 2. BENCH-0003 — FIXED

**问题**：wrapper 绑定层解码开销 2.3–2.5×（real_2k 证据 135.6 vs 335.6 fps；
p50 6.12 vs 2.68 ms），同族先例 m11_stage2b 绑定修复 +48~60%——机会成立但
原始产物无命令行、单次无 CI，修复无从验收。

**交付物**：`tools/bench_wrapper_binding_overhead.py`
- 同素材同线程**成对交替**（偶对 wrapper 先/奇对 native 先，默认 12 对），
  统计量 median(wrapper p50)/median(native p50) 的成对 bootstrap 95% CI
  （复用 `bench_spatial_aq_ab.bootstrap_ratio_ci`）；fps 比值 CI 并行记录。
- **预注册判据**（`verdict_overhead` 纯函数，单测 7 条）：CI 下界 >2.0 →
  `overhead_open_2x`（缺口仍在，即现状）；上界 <1.2 → `binding_at_parity`
  （m11 式修复验收线）；否则 `overhead_partial`；pairs<3 →
  `insufficient_pairs`。`--max-overhead X` 回归门禁（CI 下界超 X → exit 1）。
- wrapper 臂采集 `diagnostics`（decode_path/深度/worker 分配）；结果落
  `bench_out/wrapper_binding_overhead/<head>/<dylib8>/<manifest8>/<utc>/`
  （bench_manifest.v1，dirty 须 `--allow-dirty`，非 HEAD 库须
  `--expect-commit`）；`--dry-run` 无媒体自检。
- playback.md §6.1 追注：脚本、判据、验收纪律（绑定层优化必须以 CI 结果
  取代历史单次数字）、"截至注记日 clean 构建数据未采集"。

**验证**：py_compile/--help/--dry-run OK（含 pairs=0、warmup≥frames、
max-overhead≤1 三类非法参数拒绝）；小样本实测（20 帧×2 对，
`--expect-commit 22ad555f`）：
```
p50 中位 wrapper 3.084 ms vs native 2.804 ms
overhead_latency_ratio_ci95: 1.100 CI95 [1.015, 1.190]  → verdict insufficient_pairs（2<3，纪律正确触发）
```
**顺带发现（重要，未下结论）**：本机当前构建的 wrapper/native 开销约
**1.10×**，远低于历史 2.3–2.5×（无 SHA 锚定的 topos-color02 旧构建）——
绑定层开销可能在后续构建中已大幅收敛，正式确认须 clean 构建 ≥3 对采集。

## 3. BENCH-0007 + BENCH-0008 — FIXED（两缺口同一脚本闭环）

**问题**：① 产品默认配置（readahead/arena/t16）的端到端播放数字缺失——
D1 全跑 `read_a_head_depth=0`、readahead 证据是另一协议稳态吞吐，两套协议
不可数值复合，中间没有过渡运行；② §4.2 要求的 dropped_frames/late_frames/
queue_depth 遥测无任何落盘。

**交付物**：`tools/bench_default_config_playback.py`
- **BENCH-0007**：同一 harness、同素材、轮内交替（奇数轮倒序）跑
  `readahead_auto`（ToposMediaSource 全默认构造，read_ahead=None → 播放
  角色自动策略）vs `readahead_<N>`（显式深度，对应 readahead 证据链官方
  窗口；arena 批量读已并入当前代码）——**唯一差异是深度，结果可数值复合**。
  注记中如实写明：当前代码播放角色默认 readahead **关闭**
  （`_READAHEAD_MAX_DEPTH=0`，理由：同步批量抬高 4K p95/p99），auto 臂
  测到的即现行产品默认。
- **BENCH-0008**：§4.2 对齐字段落盘——`playback_frame_path`（decode→CPU
  planar 边界声明）、`decode_ms avg/p50/p95/p99`（逐帧）、`late_frames`
  （decode_ms > 1000/timeline_fps）、`dropped_frames`（`paced_playback`
  节拍消费：落后超过一个整帧周期而未被拉取的帧；`--no-pace` 时 null 并
  注明）、`queue_depth`（每次取帧后 readahead 缓存占用采样）、readahead
  统计（跑后快照）；**`unmeasured_fields` 列名 GPU/呈现侧字段**
  （texture_write/yuv_render/render_submit/ui_tick/coalesced_video_commands）
  并注明须离屏 GL harness（extended_real_tests_20260917 先例）——不冒充。
- 数据纪律：`--repeats` 默认 3（<3 写 `repeats_warning`）；P0-04 manifest
  树输出；`--dry-run` 自检。

**验证**：py_compile/--help/--dry-run OK；`parse_depths`（auto/数字/空/
负数拒绝）与 `paced_playback`（注入时钟：快路径 0 dropped 0 late、
超周期 late 计数、落后整周期 drop 且不拉取）单测 7 条；小样本实测
（36 计帧帧、25fps 节拍）：
```
readahead_auto  p50=2.967 ms p95=3.939 ms late=0 dropped=0 queue_depth=0   （深度解析=0，现行默认）
readahead_8     p50=0.078 ms p95=21.006 ms late=1 dropped=0 queue(median)=3 max=7
  readahead 统计：batch_decodes=5 prefetched=35 cache_hits=34
```
一次运行即复现文档化的 readahead 权衡（吞吐↑/尾延迟↑）——正是缺失的
"同协议两配置过渡运行"形态；遥测字段全部真实落盘。

## 4. BENCH-0013 — FIXED

**问题**：Q01 配对证据为每档单点（30 帧单素材、码率配平），决策文档 Q04
要求冻结语料 ≥5 质量点 RD 曲线 + 码率匹配/画质匹配两表——"同质量更省"
判定证据不足。

**交付物**：`tools/bench_q01_rd_curve.py`
- 双臂扫描：TOPOS 固定 QP（默认 20/32/44/56/63，覆盖 QP63 地板）+
  prores_ks `bits_per_mb` 码控（默认 128~4096 六点）；**两臂 <5 点直接
  拒绝运行**（Q04 硬要求）。
- 复用 `measure_topos_paired_quality.py` 的编码/解码/逐帧 PSNR 契约
  （同一冻结 planar 语料、SHA 随行、12-bit 444 档显式拒绝——防 prores_ks
  静默位深转换，须走 VT 配平路径）。
- 两表 + 判定（纯函数，单测覆盖）：`rate_matched_table`（每 TOPOS 点配
  ±2% 内最近 ProRes 点，报同码率 ΔPSNR-Y）、`quality_matched_table`
  （每 ProRes 点在 TOPOS RD 曲线插值同 PSNR-Y 的比特率，越界不外推）、
  `q04_verdict`（≥60% 点位 ≤0.98 → `topos_same_quality_cheaper`；≥60%
  ≥1.02 → `topos_costs_more_at_same_quality`；否则 `mixed_no_verdict`；
  点数/重叠不足 → 硬拒；注记"发布宣称还须扩样与盲测"）。
- quality.md §3.1 / codec.md §3 追注（含 98% 体积帽与 Q04 两条验收线
  独立性的说明）。

**验证**：py_compile/--help/--dry-run OK；单测 8 条（插值边界/两表分类/
四种 Q04 判定路径/参数拒绝）；**真实语料端到端实测**（2K_h264.mp4 解出
30 帧 yuv422p10le 冻结语料，5×5 点，`/tmp` 输出）：
```
topos qp32..63   bits 1.387G→448M，psnr_y（120 封顶）→62.41 dB
prores bpb128..2048  bits 35M→496M，psnr_y 30.02→36.32 dB（单调）
rate-matched 表产出（nearest_above/below 分类正确）
quality-matched 全部 in_range=False → verdict insufficient_quality_overlap（不外推，如实）
```
实测中发现并固化 **prores_ks `bits_per_mb` 编码器下限 128**（<128 直接
EINVAL"too few bits per MB"）——默认点列与参数校验已按此收口。重叠校准
（本语料上 TOPOS 全档 PSNR 显著高于 prores 点域）属采集参数问题，脚本
以 `insufficient_quality_overlap` 诚实报告而非外推造数。

## 5. BENCH-0014 — FIXED

**问题**：4K XQ 线程轴枯竭（12→16 +1.5%、16T p95 41.63 ms 超 40 ms），
"收益应转向流水线/熵段"在 native-threading §4 是【推断】非数据结论。

**交付物**：`tools/bench_thread_pivot_gate.py`
- 同构建同素材轮内交错跑线程序列（默认 8/12/16 升序），E1 分段口径
  （prep→native→mux，ANC-16 同边界），逐轮记逐帧 wall p95（nearest-rank）
  与 native 阶段统计（`TOPOS_CODEC_PROFILE=1`，BENCH-0010 口径）中
  **entropy+fill 的 worker 累计份额**（结果内注明"分配信号，不得当 wall
  归因相加"）。
- **预注册判定门**（`pivot_verdict` 纯函数，单测 7 条含试点现状形态）：
  ① 最高两档收益 ≥5%（ANC-16 漂移带反命题）→ `threads_still_profitable`；
  ② 饱和 + p95 超 `--budget-ms`（默认 40）+ 份额 ≥30% → `pivot_justified`；
  ③ 饱和 + 超预算 + 份额不足/计时不可用 → `pivot_target_unclear`；
  ④ 饱和 + 预算内 → `budget_met_no_pivot`；rounds<2 → `insufficient_rounds`。
- native-threading.md §4 追注：把【推断】条目的落地路径、判定规则、试点
  现状形态的预期判定（`pivot_justified`，单测锁定）、clean 构建采集纪律
  写明；"后续线程/流水线决策以该门结果为准"。

**验证**：py_compile/--help/--dry-run OK（threads 非升序/单档/重复拒绝）；
真实 4K XQ 小样本实测（4 帧×1 轮，`/tmp`）：
```
t8  wall_mean=37.58 p95=46.02 entropy+fill share=0.962
t16 wall_mean=31.46 p95=38.41 share=0.976 → verdict insufficient_rounds（1<2，纪律正确触发）
```

## 6. BENCH-0017 — FIXED（实测执行）

**问题**：回归锚点级数据未纳入 P0-04 manifest 结果树——readahead/real_2k/
occlusion/e1 dirty 试点等锚点来源无 manifest 锚定（cards-group2/4/5 各目录
可比性备注 + REGRESSION_BASELINE §10），锚点数据无法核验存在性与改动。

**交付物**：`tools/check_regression_anchors.py`
- 逐锚点解析 REGRESSION_BASELINE.json 的 source 字段（分号分隔/全角中文
  括注/`{a,b}` 花括号/`*` glob 均支持，`extract_source_paths` 单测），
  对每个来源文件记 **SHA-256** 并分类：`p04_manifest_tree`（bench_out 结果
  树内**且**文件含 `bench_manifest.v1`——只有目录形状不算，防伪树；抄录
  git_head/git_dirty/dylib8；两种合法深度：根树 6 段/族前缀树 7 段）或
  off-tree（`bench_out_off_tree`/`repo_benchmarks_off_tree`/`other_off_tree`）。
- **完整性门禁**：任何来源缺失/不可解析 → 该锚点 `missing` + exit 1——
  锚点底层数据被移动后基线不得继续被当有效引用。
- 登记产物落 `bench_out/anchor_registry/<utc>/anchor_registry.json`
  （schema=anchor_registry.v1，含 baseline json 自身 SHA）；`--dry-run`
  只打印不写盘（单测锁定不新增产物）。
- REGRESSION_BASELINE.md §0 追注：首跑审计结果 + 复测纪律（复测产物必须
  落 P0-04 树并重跑登记刷新 SHA）。

**验证（真实执行）**：
```
.venv/bin/python tools/check_regression_anchors.py
→ 锚点 20/20 完整；来源文件 P04 树内 22 / 树外 21；rc=0
→ bench_out/anchor_registry/20260929T155633Z/anchor_registry.json
```
分类与证据吻合：P04 树内 22 个 = d1_playback 全部（10）+ e1_encode ANC-16
dirty 试点（9）+ ANC-20 clean（1）+ ANC-18 两棵根树（2）；树外 21 个 =
readahead 文本×2、real_2k、native 域矩阵/audit/真实 CFA×7、occlusion、
real_codec_bitrate×2、paired_q01×6、thread_refine 汇总、12-bit ramp、
quality_recal——即 cards 可比性备注所指全部弱锚点，缺口被精确量化。
单测 8 条（路径解析/伪树拒绝/四类分类/缺失门禁/登记回路/空目录 dry-run）。

---

## 验证记录汇总（全部实际执行）

| 项 | 命令/方式 | 结果 |
|---|---|---|
| py_compile（6 个新建 py） | `.venv/bin/python -m py_compile <files>` | OK |
| --help | 5 个新脚本 | 全部 rc=0 |
| --dry-run | 5 个新脚本（RD 需带契约参数） | 全部 rc=0；缺媒体仅 WARNING 不失败；非法参数（pairs/frames/warmup/threads 序/深度/点数/12-bit 档/max-overhead）全部拒绝 |
| 新测试 | `pytest tests/benchmark/test_fix_bench_wd1r_g1.py -q` | **41 passed** |
| 回归 | `+ test_fix_bench_w6_aud6.py`（Wave6 批 A/B 测试） | 合计 **90 passed** |
| AUD-14100 邻接 | `test_taep_contracts.py` + `test_transaction_service.py` | **71 passed** |
| 最终合并电池 | 四文件同跑 | **161 passed**（16.8s） |
| 锚点登记实跑 | `tools/check_regression_anchors.py`（写盘） | 20/20 完整、22/21 分类、rc=0，产物入库位 |
| wrapper 开销小样本 | 2 对×20 帧（`--expect-commit`，/tmp） | ratio 1.100 CI[1.015,1.190]、insufficient_pairs 纪律触发 |
| 默认配置播放小样本 | 36 帧×auto/8 两臂（/tmp） | 双臂遥测齐全；复现 readahead 权衡（p50↓ p95↑） |
| 线程门小样本 | 4K XQ 4 帧×t8/t16（/tmp） | share 0.96+、insufficient_rounds 纪律触发 |
| RD 曲线端到端 | 真实语料 5×5 点（/tmp） | 双臂单调、两表产出、越界不外推；固化 prores_ks bpb≥128 下限 |
| P0-04 断言负向 | 无 `--expect-commit` 正式测量 | MANIFEST MISMATCH（dylib 22ad555f ≠ HEAD df513ccd）拒绝出数——按设计 |

## 遗留与边界说明

1. **clean 构建正式采集待执行**：BENCH-0003（开销 CI 定论 + 是否已收敛到
   ~1.1×）、BENCH-0007（默认配置正式数字）、BENCH-0008（decode 侧遥测
   正式落盘）、BENCH-0013（Q04 判定 + 重叠校准）、BENCH-0014（`pivot_justified`
   正式判定）——本机 dylib≠HEAD，正式测量须 clean 构建；脚本与纪律均已就位。
2. **GPU/呈现侧遥测仍未闭合**（BENCH-0008 边界）：texture_write/yuv_render/
   render_submit/ui_tick 需离屏 GL harness（extended_real_tests 先例），
   本批以 `unmeasured_fields` 诚实声明，不冒充闭合。
3. RD 曲线的质量重叠取决于语料与点列校准；脚本对越界一律 `in_range=False`
   （不外推），判定门会如实给 `insufficient_quality_overlap`。
4. BENCH-0017 的登记是**存量数据的 SHA 锚定与完整性门禁**，不改变其
   "历史观察"引用级别；树外锚点升级为可比数据须按注记纪律重测入 P0-04 树。
