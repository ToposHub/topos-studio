# Topos Color 文档索引（INDEX）

> 建立日期：2026-09-28（计划 04 文档体系化 · P0）
> 维护规则：结构性变更（新增/移动/归档文档）须同步更新本索引（依据 `.clinerules`）。
>
> **计划与报告类文档以 `plans/` 与各专题子目录（`codec/`、`image/`、`video/`、`audit/`、`plugins/`、`testcoverage/` 等）为准**；docs 根目录下的同名前缀文件多为历史时点记录，权威状态以各文档首部"状态"行及其指向的后续文档为准。归档候选清单见 `2026-09-28-docs-info-architecture.md`（本轮未移动任何文件）。

---

## 1. 新用户 / 快速了解产品

| 文件 | 一行说明 |
|---|---|
| `product_test_matrix.md` | 四种产品形态（studio / color / edit / composite）的功能开关与测试矩阵 |
| `factory-default-layout.md` | 出厂默认布局机制：随仓库提交的 `default_layout.json` 是唯一来源 |
| `image/SDK.md` | Topos Image（.toos）SDK：支持矩阵、CLI、C ABI（v1 Preview） |
| `video/V0_baseline.md` … `video/V8_V9_release_matrix.md` | Topos 视频格式 V0–V9 各阶段能力冻结与边界（建议按序浏览） |
| （仓库根）`README.md` / `README_EN.md` | 产品双语入口 |

## 2. 插件开发者

| 文件 | 一行说明 |
|---|---|
| `PLUGIN_DEV_GUIDE.md` | 主程序 `.tglsl`（DCTL）/`.tai`（ONNX）与 file_explorer 双插件体系开发指南 |
| `plugins/2026-09-28/specs/` 与 `plugins/2026-09-28/cards/` | 12 个新 GLSL 插件（Film_Grain 等）的规格书与文档卡片 |
| `PROMPTS.md` | GLSL/AI 插件开发、代码审查的 Agent prompt 模板库 |
| `adr/0004a-shader-migration-ledger.md` | 插件 shader 向 MSL 双发射迁移的台账与 G1–G3 门禁状态 |
| `adr/0004-dual-emitter-refactor-and-coverage-gates.md` | 双发射器重构决策：EffectSpec 权威化 + 结构化 uniform（插件 shader ABI 上游） |
| `benchmarks/3d_viewport/2026-07-07_summary.md` | 3D 视口合成基准摘要（合成口径，性能参考） |

## 3. 贡献者与架构

| 文件 | 一行说明 |
|---|---|
| `module_boundary_rules.md` | src 分层结构（products → features → shared → app）与允许导入关系 |
| `taep_engineering_conventions.md` | TAEP 时间/坐标合同等工程级硬约束（Phase 0 固化） |
| `hdr_math_contract.md` | HDR 数学契约：内部 float32、不在管线中途 clamp |
| `adr/` | 架构决策记录 0001–0004a（HTML 渲染后端 ×2、Metal 双轨、双发射器 + 迁移台账） |
| `image/ADR-I001…ADR-I021` + `image/topos_image_file_spec_v0.md` + `image/risk_register.md` | 图片格式决策链、文件规范真相源与风险登记表 |
| `metal_backend_migration_and_cross_platform_gpu_plan.md` | Metal 与跨平台 GPU 后端双轨架构主计划（ADR 0003/0004 的上游） |
| `codec/`、`video/` | 编解码专题：格式计划、门禁协议（`v7_size_gate_protocol.md`、`rd4_01_fixed_sampling_spec.md`、`rd5_01_surface_ownership.md`）、落档报告与版本状态 |
| `plans/2026-09-28-multi-machine-program/` | 多机并行优化战役总纲与 5 份子计划（01 审查 / 02 测试 / 03 插件 / 04 文档 / 05 基准） |
| `audit/2026-09-28/` | 计划 01 产出：全库审查问题台账（FINAL_REPORT / ISSUES / INVENTORY，AUD-xxx 编号） |
| `testcoverage/2026-09-28/` | 计划 02 产出：覆盖率基线与缺口报告（COVERAGE_BASELINE / GAP_REPORT） |
| `project_full_audit_optimization_plan_2026-09-20.md` | 全项目审计整改台账（P0-COL/CI 系列，滚动更新） |
| `ai_control_optimization_plan.md` | AI 全功能控制的项目级计划与验收基线 |
| `ai_assistant_privacy_acceptance_checklist.md` | AI Assistant 隐私与安全硬门槛清单（M0 基线） |
| `vault_complete_plan.md` | Vault AI 素材库子系统的唯一真相源（架构 + 计划 + 参考） |
| `topos_color_professional_nle_performance_master_plan_2026-09-15.md` | 专业剪辑性能总计划（B/T/P 编号体系，收敛各性能线） |
| `composite_pro_parity_gap_plan_2026-10-13.md` | 合成页对标 Nuke/Fusion/AE 差距审计与补齐计划（CP-001…CP-075 任务台账，勾选制） |
| `edit_page_fifth_audit_optimization_plan_2026-10-20.md` | 剪辑页第五轮全功能复查：22 项 E01–E22 问题、证据与分批优化计划（`audit/2026-10-20-edit-fifth-review/`） |
| `edit_page_fifth_audit_fixes_2026-10-20.md` | 第五轮 E01–E22 修复记录：A/B/C 三批全部修复、13 个新回归文件、接线门禁与保留边界 |

## 4. 运维 / 打包 / 发布

| 文件 | 一行说明 |
|---|---|
| `windows_topos_codec_sdk_optimization_plan_2026-09-28.md` | Windows SDK/发布实机执行状态（unsigned Preview，W01–W24 台账） |
| `image/ADR-I021-release-distribution.md` | Topos Image SDK 分发、发布与回滚决策 |
| `video/V8_V9_release_matrix.md` | 生态与发布能力矩阵（supported / deferred 边界） |
| `mg_macos_manual_qa_checklist.md` | Motion Graphic 编辑器 macOS 真实桌面手工验收清单（R-13） |
| `s01_realtime_takeover_real_machine_acceptance_2026-09-17.md` | 实时播放接管实机验收清单（S1–S7 场景矩阵） |
| `ai/volcengine_agent_plan_setup.md` | 火山方舟 Agent Plan 凭据（Keychain）与用户级 profile 配置记录 |
| `topos_proxy_default_policy_2026-09-16.md` | Topos 422 代理默认档位策略（实证档位表） |

## 5. 历史档案

| 位置 | 一行说明 |
|---|---|
| `plans/archive/` | 2026-05～08 历史计划/审计/报告归档（119 份，见其 README；行号与结论可能过时） |
| 根目录两份 `.txt` 计划书 | 早期 TPIC 图片 / V2.0 视频格式计划书，头部已附勘误并指向现行真相源 |
| `superpowers/` | 早期 superpowers 工作流的执行计划与设计 spec（功能已实现） |
| `codec/` 已结项代际批次 | V7-A/R3、V8、V9、TRAW、版本收纳等批次文档，保留为决策证据链 |

## 附：性能与基准报告常用入口

- `p0_baseline_report_macos_2026-09-16.md` / `topos_422_family_benchmark_2026-09-16.md` — macOS 纯解码基线（B 矩阵 / T-01 复验）
- `codec/topos_codec_speed_matrix_2026-09-15.md`、`codec/topos_tier_matrix_c043_2026-09-11.md` — codec 速度/码率终态矩阵
- `codec/TOPOS_F01基线_2026-09-27.md` — 最新热点筛查基线（冻结语料口径）
- `color_tools_benchmark_20260911.md` — 调色页工具性能基准
- 计划 05 将产出 `docs/benchreports/`（bench_out 交叉分析图谱，目录待建）
