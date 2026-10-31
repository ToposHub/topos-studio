# docs 信息架构方案（2026-09-28）

> 计划 04（文档体系化）P0 产出 · 分支 `dev`
> 性质：目录职责定义 + 全量盘点判定 + 归档候选登记。
> **红线遵守：本文只登记候选，不移动、不修改、不删除任何既有文件。**

---

## 1. 盘点口径与总量

- 方法：对指定范围逐文件读取首部（约 30 行），按标题、状态行、日期、关联引用判定类型与状态。
- 指定范围：docs 根级 md（57 份，另 2 份 txt 计划书与 5 份 json 报告数据）+ `adr/`（5）、`ai/`（1）、`benchmarks/`（1）、`codec/`（92 md + 证据数据）、`image/`（39 md + 资产）、`plans/`（活跃 6 + archive 119）、`superpowers/`（2）、`video/`（6 md）。指定范围 md 共 **209 份**。
- 本轮新出现的战役产出目录一并登记：`audit/2026-09-28/`（计划 01，含 batches/assignments 扇出产物约 750 份）、`plugins/2026-09-28/`（计划 03，53 份）、`testcoverage/2026-09-28/`（计划 02，25 份）；计划 05 的 `docs/benchreports/` 尚未创建。
- docs/ 全目录当前共 1224 个文件（其中 md 1120），约 75% 为 audit 扇出批片与 plans/archive。

## 2. 目录职责表

### 2.1 现有目录

| 目录 | 现状 | 职责（判定/建议） |
|---|---|---|
| `docs/`（根） | 57 md + 2 txt + 5 json，指南/契约/计划/报告混放 | 只保留**横向指南、契约与现行总计划**；专题计划与报告逐步下沉到专题子目录或归档（见 §4） |
| `adr/` | ADR 0001–0004a，全部 Accepted | 全项目级架构决策记录；新增 ADR 按编号续写 |
| `ai/` | 1 份接入记录 | AI 专题：接入配置、凭据、provider 记录；根级 AI 计划/隐私清单为归入候选（涉及引用链，暂不动） |
| `benchmarks/` | 1 份摘要 + json | 基准报告数据与摘要；机器可读基准数据的事实位置 |
| `codec/` | 92 md + evidence/ | 编解码专题：格式计划、门禁协议、A/B 与矩阵报告、代际批次落档；`evidence/` 存原始证据数据 |
| `image/` | 39 md + 资产/reports | 图片格式专题：ADR-I 系列、文件规范、SDK、风险表、实测报告 |
| `plans/` | 活跃 6 + archive 119 | **计划类文档的权威位置**；archive 为既有归档区（含 README 说明） |
| `superpowers/` | 2 份（已实现功能的历史计划/spec） | 历史工作法产物；整目录归档候选 |
| `video/` | 6 md + schema | Topos 视频格式 V0–V9 版本状态权威记录（冻结/deferred 决策） |
| `audit/2026-09-28/` | 计划 01 产出 | 只读审查产出（FINAL_REPORT/ISSUES/INVENTORY + batches/assignments/verification） |
| `plugins/2026-09-28/` | 计划 03 产出 | 新插件库的 specs/cards/i18n/工具与缺陷记录 |
| `testcoverage/2026-09-28/` | 计划 02 产出 | 覆盖率基线、缺口报告、BUGS 台账与运行脚本 |

### 2.2 建议新增（本轮不创建，供计划 04 后续阶段执行）

| 目录 | 职责 | 预期内容 |
|---|---|---|
| `architecture/` | 架构文档专区 | 计划 04 P1 的 5 篇：`color-rendering.md`、`edit-timeline.md`、`composite.md`、`deliver-export.md`、`shared-foundations.md`；后续结构性变更同步更新 |
| `getstarted/` | 新用户入门 | 从根 README 引导的安装、产品形态、首次运行、常见路径；与 INDEX.md 第 1 节互链 |

## 3. 文件归类判定汇总

类型定义：**指南/契约**＝长期有效的规范与参考；**ADR**＝决策记录；**计划**＝带任务书/路线图性质；**报告**＝实测/审计/验收记录。
状态定义：**现行**＝被引用或仍在执行；**归档候选**＝已完成/被取代但仍有证据价值；**疑似重复**＝存在更权威版本；**已归档**＝已在 archive 区或头部自带勘误。

### 3.1 docs 根级（57 md，逐文件）

| 文件 | 类型 | 状态 |
|---|---|---|
| `PLUGIN_DEV_GUIDE.md` | 指南 | 现行（计划 03 管追加） |
| `PROMPTS.md` | 参考 | 现行 |
| `product_test_matrix.md` | 参考 | 现行 |
| `factory-default-layout.md` | 参考 | 现行 |
| `module_boundary_rules.md` | 契约 | 现行 |
| `taep_engineering_conventions.md` | 契约 | 现行 |
| `hdr_math_contract.md` | 契约 | 现行 |
| `video_denoise_spec.md` | 规格 | 现行（V1 完成状态记录） |
| `ai_assistant_privacy_acceptance_checklist.md` | 清单/规范 | 现行（里程碑推进中） |
| `mg_macos_manual_qa_checklist.md` | 清单 | 现行 |
| `metal_backend_migration_and_cross_platform_gpu_plan.md` | 计划 | 现行（GPU 架构主计划，ADR 0003/0004 上游） |
| `topos_color_professional_nle_performance_master_plan_2026-09-15.md` | 计划 | 现行（性能总计划） |
| `ai_control_optimization_plan.md` | 计划 | 现行 |
| `ai_executable_optimization_upgrade_plan_2026-09-17.md` | 计划 | 现行（实施手册） |
| `composite_optimization_plan_2026-09-07.md` | 计划 | 现行（执行中） |
| `composite_advanced_plan_2026-09-08.md` | 计划 | 现行（Phase 2） |
| `composite_fullchain_gpu_audit_plan_2026-09-15.md` | 审计+计划 | 现行 |
| `edit_optimization_plan_2026-09-07.md` | 计划 | 现行（执行中） |
| `macos_full_chain_gpu_optimization_plan_2026-09-15.md` | 计划 | 现行（未达标） |
| `windows_optimization_plan_2026-09-11.md` | 计划 | 现行（执行中） |
| `windows_full_chain_gpu_optimization_plan_2026-09-15.md` | 计划 | 现行 |
| `windows_timeline_gpu_export_optimization_plan_2026-09-15.md` | 计划 | 现行 |
| `windows_topos_codec_sdk_optimization_plan_2026-09-28.md` | 计划 | 现行（大部已执行，含实机状态） |
| `float_toos_and_raw_productization_plan_2026-09-21.md` | 计划 | 现行（实施中，台账滚动） |
| `topos_image_optimization_implementation_plan.md` | 计划 | 现行 |
| `topos_prores_style_reduced_decode_implementation_plan.md` | 计划 | 现行 |
| `topos_prores_quality_optimization_plan_2026-09-21.md` | 计划 | 现行 |
| `topos_v7b_reduced_decode_master_plan_2026-09-09.md` | 计划 | 现行（重基线化） |
| `color_davinci_tracker_master_implementation_plan.md` | 计划 | 现行（规划中；已有实测报告） |
| `video_batch_export_panel_implementation_plan.md` | 计划 | 现行（设计阶段） |
| `video_denoise_implementation_plan.md` | 计划 | 现行（第一版已交付，spec 记录状态） |
| `project_full_audit_optimization_plan_2026-09-20.md` | 计划/台账 | 现行（滚动更新） |
| `vault_complete_plan.md` | 计划+架构 | 现行（vault 唯一真相源） |
| `file_explorer_optimization_plan_2026-09-07.md` | 计划 | 现行（U1–U10 已落地，能力对标仍有效） |
| `p0_baseline_report_macos_2026-09-16.md` | 报告 | 现行（被策略文档引用） |
| `topos_422_family_benchmark_2026-09-16.md` | 报告 | 现行（T-01 复验） |
| `topos_proxy_default_policy_2026-09-16.md` | 报告/策略 | 现行 |
| `color_tools_benchmark_20260911.md` | 报告 | 现行（metal plan §0.3.1 引用） |
| `color_tracker_real_media_report.md` | 报告 | 现行 |
| `topos_edge_jag_diagnosis_2026-09-21.md` | 诊断报告 | 现行 |
| `topos_prores_quality_gain_2026-09-22.md` | 报告 | 现行 |
| `s01_realtime_takeover_real_machine_acceptance_2026-09-17.md` | 验收清单 | 现行（S0-1 为剩余入口） |
| `4k-playback-optimization.md` | 计划 | 归档候选（2026-05-23；被后续计划接续，ADR 0003 仍引用其行号） |
| `optimization_plan.md` | 审计报告 | 归档候选（2026-05-14，最老） |
| `playback_benchmark_final.md` | 报告 | 归档候选（2026-06-15） |
| `file_explorer_upgrade_plan.md` | 计划 | 归档候选 + 疑似重复（被 2026-09-07 版取代） |
| `file_explorer_upgrade_plan_2026-09-07.md` | 计划 | 归档候选（133 项清单，后续批次已修复大半） |
| `ai_real_material_test_optimization_plan_2026-09-17.md` | 计划+报告 | 归档候选（一次性实测） |
| `ai_topos_feature_matrix_optimization_plan_2026-09-17.md` | 报告 | 归档候选（一次性矩阵） |
| `professional_nle_realtime_playback_optimization_plan.md` | 计划 | 归档候选（被 2026-09-15 性能总计划收敛） |
| `professional_nle_playback_engine_implementation_spec.md` | 规格 | 归档候选（同上配套） |
| `system_check_plan_2026-09-04.md` | 计划 | 归档候选（R0–R5 大部完成；archive/README 仍将其列为"当前有效"索引，移动需同步该 README） |
| `timeline_occlusion_culling_optimization_plan_2026-09-19.md` | 计划 | 归档候选（O0–O8 已实施） |
| `timeline_occlusion_culling_implementation_2026-09-19.md` | 实施记录 | 归档候选（与上成对） |
| `topos_aq_rdo_quality_ab_2026-09-07.md` | 报告 | 归档候选（早期 A/B） |
| `video_topos_encode_decode_speed_2026-09-07.md` | 报告 | 归档候选（早期速度数据） |
| `时间线UI优化大纲.md` | 大纲草案 | 归档候选（无日期，疑似过时） |

### 3.2 子目录汇总

| 目录 | 判定汇总 |
|---|---|
| `adr/`（5） | 全部 ADR，现行有效；`0004a` 是持续更新的迁移台账 |
| `ai/`（1） | 参考，现行 |
| `benchmarks/`（1） | 报告，现行（合成口径，已声明边界） |
| `codec/`（92） | ADR 式落档与协议约 12 份现行（rd2/rd4/rd5/rd7 无日期说明、v7_size_gate_protocol 等）；现行计划约 5 份（全格式对标、复验、速度 v2、容器音频后续、F01 相关）；报告约 55 份（矩阵/剖析/负结果，近期者仍被引用）；已结项批次与被复验覆盖的门禁记录约 23 份为归档候选（见 §5）；疑似重复 2 组（tier_decode 09-10 vs 09-11、rd4_08 双记录） |
| `image/`（39） | ADR-I001–I021 共 21 份（已接受/已实施，现行）；规范/SDK/协议/风险表 4 份现行；H4 评估 1 份现行；实测报告约 13 份，其中 7 份为归档候选（早期速度/体积实测，被 codec/ 后续矩阵覆盖） |
| `plans/`（活跃 6） | 战役总纲 + 5 份子计划，现行有效；`archive/`（119）为已归档区 |
| `superpowers/`（2） | 历史计划与 spec，功能已实现，整目录归档候选 |
| `video/`（6） | V0–V9 版本状态权威记录，全部现行（含 deferred 决策） |
| `audit/`、`plugins/`、`testcoverage/`（2026-09-28） | 本轮战役产出：审查台账 / 插件文档 / 覆盖基线，全部现行（计划合并说明见 plans README §1.5） |

**状态分布（指定范围 209 份 md，约数）**：现行指南/契约/规范 ≈ 33；ADR/决策记录 ≈ 27；现行计划 ≈ 40；现行报告/清单 ≈ 45；已完成可归档候选 ≈ 49（含 §5 明细 49 项中的 md 部分）；疑似重复/被取代 ≈ 8；已归档/历史 ≈ 123（archive 119 + 2 txt + superpowers 2）。

## 4. 归档候选清单（只列候选，本轮不移动任何文件）

> 建议去向统一为 `plans/archive/` 或其下按专题分批；**移动前必须**：①更新 `plans/archive/README.md` 的"当前有效计划"指引；②修复指向该文件的所有引用（下表已标注已知引用点）。

### 4.1 docs 根级（15 项）

| # | 文件 | 理由 | 已知引用点 |
|---|---|---|---|
| 1 | `optimization_plan.md` | 2026-05-14 节点体系审计，最老 | 无已知 |
| 2 | `playback_benchmark_final.md` | 2026-06-15 基准报告（同名 json 一并考虑） | 无已知 |
| 3 | `4k-playback-optimization.md` | 2026-05 播放纲领，已被 metal plan v2 与后续执行接替 | ADR 0003 引用其 `:413/:614` 行号 |
| 4 | `file_explorer_upgrade_plan.md` | 被 2026-09-07 版系统性取代（疑似重复） | `system_check_plan` 引用为"上一轮" |
| 5 | `file_explorer_upgrade_plan_2026-09-07.md` | 133 项问题清单，后续批次大部修复 | `file_explorer_optimization_plan` 引用 |
| 6 | `ai_real_material_test_optimization_plan_2026-09-17.md` | 一次性端到端实测 | 无已知 |
| 7 | `ai_topos_feature_matrix_optimization_plan_2026-09-17.md` | 一次性功能矩阵实测 | 无已知 |
| 8 | `professional_nle_realtime_playback_optimization_plan.md` | 被 2026-09-15 性能总计划收敛 | 总计划 §1 引用关系需保留说明 |
| 9 | `professional_nle_playback_engine_implementation_spec.md` | 同上配套规格 | 同上 |
| 10 | `system_check_plan_2026-09-04.md` | R0–R5 大部完成 | `plans/archive/README.md` 将其列为当前有效入口 |
| 11 | `timeline_occlusion_culling_optimization_plan_2026-09-19.md` | O0–O8 已实施 | 实施记录互相引用 |
| 12 | `timeline_occlusion_culling_implementation_2026-09-19.md` | 成对实施记录 | 同上 |
| 13 | `topos_aq_rdo_quality_ab_2026-09-07.md` | 早期 A/B（同名 json 一并考虑） | 无已知 |
| 14 | `video_topos_encode_decode_speed_2026-09-07.md` | 早期速度数据（同名 json 一并考虑） | 无已知 |
| 15 | `时间线UI优化大纲.md` | 无日期 UI 草案，疑似过时 | 无已知 |

### 4.2 codec/（23 项）

| # | 文件 | 理由 |
|---|---|---|
| 1–2 | `preview_decode_gate_{gradient,grain}_2026-09-09.md` | 被 2026-09-10 复验版覆盖（FAIL 结论未变） |
| 3–4 | `scalable_pyramid_gate_{gradient,grain}_2026-09-09.md` | BLOCKED 结案 |
| 5 | `v7_size_gate_2026-09-08.md` | PASS 结案（协议文档保留） |
| 6 | `rd4_08_scalable_encode_gate_2026-09-08.md` | BLOCKED 结案（与无日期版双记录，疑似重复） |
| 7–15 | `topos_v8_{table_loss,format_spec_batch1,encoder_batch2,decode_batch3,gpu_batch4,conformance_batch5,editor_integration,production_switch,format_plan}_2026-09-12.md` | V8 窗口已结项（生产切换另有拍板） |
| 16 | `topos_v9_micro_gop_plan_2026-09-13.md` | 批 0–7 结项，LP 不注册 |
| 17 | `topos_v7r3_micro_gop_plan_2026-09-13.md` | 全部批次完成结项 |
| 18 | `topos_version_consolidation_plan_2026-09-13.md` | 批 0–5 结项（ADR-C046 入库） |
| 19 | `topos_consolidation_audit_2026-09-13.md` | 收纳批 0 审计完成 |
| 20 | `topos_traw_format_plan_2026-09-12.md` | 批 0–5 完成（遗留项已在文内登记） |
| 21 | `topos_speed_optimization_plan_2026-09-11.md` | v1 收口，被 v2 取代（v2 保留） |
| 22 | `topos_container_audio_v1_1_plan_2026-09-19.md` | M-A0..A7 已全部交付 |
| 23 | `topos_tier_decode_matrix_2026-09-10.md` | 被 2026-09-11 复测取代（疑似重复） |

### 4.3 image/（7 项）

| # | 文件 | 理由 |
|---|---|---|
| 1 | `perf_report_stage7.md` | 阶段 7 首次实测，历史基线 |
| 2 | `topos_image_decode_speed_2k_4k_2026-09-07.md` | 早期实测，被后续矩阵覆盖 |
| 3 | `topos_image_encode_speed_2k_4k_2026-09-07.md` | 同上 |
| 4 | `topos_422_low_compact_2026-09-07.md` | 同上（同名 json 一并考虑） |
| 5 | `toos_rans2_alignment_2026-09-12.md` | 对齐收口已完成 |
| 6 | `toos_vs_jpg_png_2026-09-12.md` | 与 toos444 版为姊妹实测，可择一保留 |
| 7 | `toos444_vs_jpg_png_2026-09-12.md` | 同上（口径不同，非严格重复） |

### 4.4 其他（4 项）

| # | 位置 | 理由 |
|---|---|---|
| 1 | `superpowers/plans/2026-05-24-phase1-phase2-completion.md` | Phase 1/2 已完成执行 |
| 2 | `superpowers/specs/2026-06-12-composite-transform-handles-design.md` | 功能已实现（升级记录在案） |
| 3 | 根 `Topos Image（TPIC）…计划书.txt` | 头部已附勘误声明为历史档案 | 
| 4 | 根 `专属极速帧内中间片编码引擎（Topos V2.0）.txt` | 同上；注意 ADR-I001 仍将其列为输入记录 |

**归档候选合计 49 项**（根 15 + codec 23 + image 7 + 其他 4）。

## 5. 疑似重复清单

| 组 | 文件 | 判定 |
|---|---|---|
| A | `file_explorer_upgrade_plan.md` ↔ `file_explorer_upgrade_plan_2026-09-07.md` | 后者为系统性复审版，前者可归档 |
| B | `codec/rd4_08_scalable_encode_gate.md` ↔ `codec/rd4_08_scalable_encode_gate_2026-09-08.md` | 同一门禁两份记录（结论一致 BLOCKED） |
| C | `codec/preview_decode_gate_*_2026-09-09` ↔ `_2026-09-10` | 复验迭代关系，保留终版即可 |
| D | `codec/topos_tier_decode_matrix_2026-09-10.md` ↔ `_2026-09-11.md` | 后者复测取代前者 |
| E | `image/toos_vs_jpg_png_2026-09-12.md` ↔ `toos444_vs_jpg_png_2026-09-12.md` | 姊妹篇（RGB8 vs GBR12 口径不同），建议都保留但交叉链接 |
| F | 根 `file_explorer_optimization_plan_2026-09-07.md` 与 `file_explorer_upgrade_plan_2026-09-07.md` | 非重复但命名易混淆（能力对标 vs 缺陷清单），建议在 INDEX 保持区分说明 |

## 6. 后续执行建议（不属本轮红线内）

1. 计划 04 P1 创建 `architecture/` 五篇架构文档后，INDEX 第 3 节替换为指向 `architecture/` 的入口。
2. 归档执行时按 §4 分批移动，并同步修复引用与 `plans/archive/README.md`。
3. 根级 json 报告数据（`playback_benchmark_final.json` 等 5 份）建议与同名 md 同批处理，或集中到 `benchmarks/`。
4. `getstarted/` 建立后，将 `product_test_matrix.md`、`factory-default-layout.md` 的入口描述合并过去并互链。
