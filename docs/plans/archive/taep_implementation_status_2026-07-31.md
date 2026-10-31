# TAEP 实施状态（截至 2026-07-31）

> 本文档记录 Topos AI Edit Protocol（TAEP）的实施进度。原始设计见
> `docs/ai_editing_protocol_and_implementation_plan_2026-07-31.md`，工程级硬约束见
> `docs/taep_engineering_conventions.md`。

## 总览

TAEP 的 9 个 phase + AI 助手 UI（UI-0..UI-5）的**协议基石、数据合同、编译器、
事务、应用 API、Job、分析、工具 host、FCPXML/OTIO adapter、规划器与助手 dock
面板**已全部落地，共 **231 个 focused test 全绿**，核心 edit 回归 348 个测试无破坏。

所有写入经唯一提交入口（`EditProjectService`），编译到现有 `EditCommand` /
`MacroCommand`，保持 revision / ref↔sequence 两层 / undo-redo 一致；AI 只能
经声明式 `EditPlan` 与受限工具面操作，绝不执行任意 Python / shell / 读任意路径。

## Phase 完成情况

| Phase | 内容 | 状态 | 关键交付 |
|---|---|---|---|
| P0 | 架构封口 | ✅ | `EditProject`/`TimelineSequence`/`ToposProject` 的 `revision` 字段（round-trip）；`EditProjectService` 统一提交入口（apply→validate→bump→sync 两层）；乐观锁 `RevisionConflict`；`TimelineExportConfig.validate()` Rational fps 修复 |
| P1 | 创作模型 + 只读合同 | ✅ | `editorial_models`（Brief/Variant/Decision/Segment，整帧合同）、`brief_service`（patch/锁定/硬软约束/版本关系）、`models`（EditPlan/operation/snapshot/manifest）、`schema`、`plan_validator`、`capability_manifest`（从真实 registry 生成）、`snapshot_service`（路径脱敏）、`semantic_index`、`timebase`（秒/时码→整帧 floor/ceil）、诊断码 |
| P2 | 编译器 + 事务 | ✅ | `plan_compiler`（operation→EditCommand/MacroCommand）、`variant_planner`（Brief+Segment+锁定→多 StoryOutline+EditDecision+EditPlan，规则引擎）、`transaction_service`（validate→compile→simulate→commit，preview 与 commit canonical-equal）、`TimelineDiff`、`decision_service`（反馈回灌约束） |
| P3 | 应用服务 + 脚本 API | ✅ | `EditSession`/`EditSessionHost` Protocol（无 PyQt）、`PathGrant` token、approval 门禁、安全（plan 是纯数据，无法 exec/fs） |
| P4 | 预览/导出 Job + 门禁 | ✅ | `jobs`（JobHandle/Registry 状态机）、`export_service`（ExportRequest→Config + 三套 preflight + job）、预览 preset（限帧） |
| P5 | 素材分析 | ✅ | `analysis_models`（AssetAnalysis/fingerprint/provenance）、`AnalysisProvider` 协议、`SceneCutProvider`（复用 scene_cut）、`analysis_service`（合并/失效/查询） |
| P6 | AI 工具 host | ✅ | `tool_facade`（§7 全部语义工具 + 权限级）、prompt injection 隔离（`SanitizedText`）、`audit_store`（replay） |
| P7 | FCPXML | ✅ | `interchange_types`（Adapter 协议 + Report 四类条目）、`fcpxml_adapter`（inspect/import/export，round-trip 验证） |
| P8 | OTIO（按需） | ✅ | `otio_adapter`（可选依赖 graceful degrade）、`flatten_nested_sequence` |
| P9 UI-0 | 助手空壳 | ✅ | `assistant_events`（typed 状态/事件）、`provider_profiles`、`AiEditAssistantPanel` dock + PanelDescriptor 注册 |
| P9 UI-1 | profile/隐私 | ✅ | `AiEditingPreferencePage`（keychain-only，永不回显 token） |
| P9 UI-2 | Brief/上下文 | ✅ | `AiBriefEditor`、`AiContextSummary` |
| P9 UI-3 | 对话/variant | ✅ | `AiVariantList`、`AiDecisionInspector` |
| P9 UI-4 | diff/审批 | ✅ | `AiTimelineDiffPanel`、`AiApplyConfirmationDialog` |
| P9 UI-5 | job | ✅ | `AiJobPanel` |
| §11.7/§11.8 补齐 | music/service/store/controller | ✅ | `music_policy`（授权筛选）、`assistant_service`（状态机编排）、`assistant_session_store`（工程本地持久化）、`ai_edit_controller`（UI signal→service） |

## 代码结构（新增，与 §10 / §11.8 一致）

```text
src/features/edit/ai_editing/        # TAEP 协议层（纯数据/逻辑，无 Qt/GPU）
├── __init__.py                       # 统一导出
├── edit_project_service.py           # P0 统一提交入口
├── diagnostics.py                    # P1 诊断码 + 消息模板
├── editorial_models.py               # P1 Brief/Variant/Decision/Segment
├── models.py                         # P1 EditPlan/operation/snapshot/manifest
├── schema.py                         # P1 JSON Schema + 版本协商
├── plan_validator.py                 # P1 schema + domain 校验
├── capability_manifest.py            # P1 从 registry 生成能力表
├── snapshot_service.py               # P1 脱敏只读快照
├── semantic_index.py                 # P1 SemanticSegment 查询
├── plan_compiler.py                  # P2 operation→EditCommand
├── variant_planner.py                # P2 Brief+Segment→多 variant（规则引擎）
├── transaction_service.py            # P2 validate/preview/commit/diff
├── decision_service.py               # P2 反馈回灌约束
├── edit_api.py                       # P3 EditSession facade
├── jobs.py                           # P4 Job 合约
├── export_service.py                 # P4 ExportRequest→Config+preflight
├── analysis_models.py                # P5 AssetAnalysis/provider 契约
├── analysis_service.py               # P5 provider 编排 + 失效
├── tool_facade.py                    # P6 语义工具面 + 注入隔离
├── audit_store.py                    # P6 审计/replay
├── assistant_events.py               # P9 typed 状态/事件
├── assistant_service.py              # P9 会话状态机/编排（补齐）
├── assistant_session_store.py        # P9 工程本地持久化（补齐）
├── music_policy.py                   # P9 §11.7 音乐授权筛选（补齐）
├── brief_service.py                  # P1 brief patch/锁定/约束（补齐）
├── timebase.py                       # P1 §4.1 秒/时码→整帧（补齐）
└── provider_profiles.py              # P9 profile（非 secret）

src/features/edit/adapters/           # 交换 adapter
├── interchange_types.py              # P7 Adapter 协议 + Report
├── fcpxml_adapter.py                 # P7 FCPXML
└── otio_adapter.py                   # P8 OTIO（可选）

src/features/edit/ui/                 # AI 助手 dock 子组件
├── ai_edit_assistant_panel.py        # P9 UI-0 主 dock
├── ai_brief_editor.py                # P9 UI-2
├── ai_context_summary.py             # P9 UI-2
├── ai_variant_list.py                # P9 UI-3
├── ai_decision_inspector.py          # P9 UI-3
├── ai_timeline_diff.py               # P9 UI-4
├── ai_job_panel.py                   # P9 UI-5
└── ai_preferences_page.py            # P9 UI-1
```

## 测试

- `tests/edit/ai_editing/`：10 个文件，覆盖 service/contracts/transaction/decision/
  export/analysis/tool_facade/fcpxml/otio/assistant_events。
- `tests/ui/`：4 个文件，覆盖 dock/brief+context+variant/diff/job+decision+prefs。
- **181 个 TAEP 相关测试全绿**；核心 edit（project/commands/ops/export/interchange/
  scene_cut）326 个测试无回归。

## 关键工程不变量（已由测试守护）

1. **唯一事实源**：AI 不创建第二套时间线；所有写入编译到 `EditCommand`/`MacroCommand`。
2. **统一提交入口**：UI/AI/脚本/导入都经 `EditProjectService.commit_command`，
   revision 与 ref↔sequence 两层一致，单 MacroCommand 单 undo。
3. **整帧合同**：非负 int 帧号、`[start, start+dur)` 半开区间、Rational fps。
4. **乐观锁**：`base_revision != current` → `RevisionConflict`/STALE，不自动覆盖。
5. **preview ≡ commit**：复用同一 compiler，proposed 与 committed canonical-equal。
6. **原子性**：任一 operation 无效不产生半套状态。
7. **路径脱敏**：snapshot 绝不含本机绝对路径/secret。
8. **prompt injection 隔离**：素材名/字幕/OCR 标记为不可信数据，不可改变工具权限。
9. **导出门禁**：preflight blocking 时不提交 job；输出需 path grant，不静默覆盖。

## 后续迭代（未在本轮范围，已在代码中预留接口）

- **UI-6 可靠性/可访问性**：会话恢复、键盘导航、telemetry opt-in、长转录压测。
- **真实 provider client**：`AiProviderProfile` 的“测试连接 + 拉取 capability”需
  具体 provider SDK adapter（OpenAI 兼容 / MCP host）。
- **decision/variant 持久化**：`assistant_session_store`（工程可选 payload）。
- **P9 高级能力**：文字剪辑、silence removal、多平台重构图、B-roll 建议、
  ducking、多机位、ColorStack/Composite preset 引用——每项需先扩展 canonical
  command/model/manifest/validator/diff/audit 后才对模型开放。
- **EditController 接线**：把现有绕过 `timeline_changed` 的属性/effect 面板路径
  收口到 `EditProjectService`（P0 已提供入口，收口是渐进式重构）。

## 提交历史

```
a7187b9c TAEP Phase 9 UI-1/UI-3/UI-5: job panel + decision inspector + preferences
a3259ea2 TAEP Phase 9 UI-2/UI-3/UI-4: brief/context/variant/diff subpanels
25446264 TAEP Phase 9 UI-0: AI edit assistant dock shell + state contract
ed6267f0 TAEP Phase 8: OTIO adapter (optional) + nested sequence flatten
9e7ac9fe TAEP Phase 7: FCPXML import/export adapter
45900057 TAEP Phase 6: tool facade + prompt injection isolation + audit/replay
939e9d85 TAEP Phase 5: asset analysis (AssetAnalysis + providers + SemanticSegment)
bdfc5530 TAEP Phase 4: preview/export job contract + deliver gate
663557af TAEP Phase 3: ToposEditAPI/EditSession local script facade
70d0757c TAEP Phase 2: plan compiler + transaction service + decision service
3215b6da TAEP Phase 1: AI-native creative models + read-only contracts
86b14809 TAEP Phase 0: revision + unified commit service (edit_core)
```
