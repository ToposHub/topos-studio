# Topos AI 剪辑闭环闭合与聚焦实施计划（2026-08-04）

## 0. 定位与本计划与原计划的关系

本计划替代 `docs/ai_automation_cli_and_script_optimization_plan_2026-07-31.md`（以下简称"原
CLI 计划"）的**全量实施**。原 CLI 计划作为"目标架构"文档保留，但不再作为现阶段的执行依据。

重新排序的依据是一次针对实际代码（非文档转述）的核查，它推翻了原计划的两个关键假设：

| 原计划假设（2026-07-31） | 实际代码现状（2026-08-04） |
| --- | --- |
| M4/M5 未完成，真实 LLM provider 是 stub，没有 Job worker 线程。 | **真实联网 provider 已实现并接入**：`llm_client.py` 含 `OpenAICompatClient`/`AnthropicClient`（走 `urllib.request` 真实 HTTP），`LLMVariantPlanner` 在 `ai_editing_mixin.py:309-321` 已注入。Job worker 已存在：`ai_edit_controller.py:62 _PlanWorker`、`:119 _ChatWorker`（QThread + 协作取消），`AnalysisJobScheduler` 有优先级队列 + 并发上限 + 播放抢占。 |
| CLI（toposctl）应作为"AI 剪辑落地前的基础设施"优先铺开 4 层架构。 | 真正卡脖子的是两个**极具体的接线断裂**（见 §1），工作量远小于"补 M4/M5"。铺完整 CLI/IPC/MCP 基础设施是 over-engineering，应让位给闭合内核闭环。 |

因此本计划把工程资源聚焦在**三件事**：闭合 GUI 内 AI 剪辑闭环、让闭环跨工程重开、能力单一真相收尾。
原 CLI 计划的 A0（只读 CLI）保留为**可选并行项**，但只做精简版（不建 4 层架构）。
原计划 A2–A5（离线锁、connected IPC、export/jobs CLI、FCPXML/OTIO/MCP）全部推迟。

> 注：仍为 stub 的是 `generation_provider.py` 的 `DeterministicGenerationProvider`（视频/音乐/语音生成），
> 这是按 `ai_assistant_agent_harness_product_plan` 的 G0/G1 留到 M11 的，不属于"AI 剪辑"范畴，本计划不涉及。

---

## 1. 现状基线：什么已经可用、什么断了

### 1.1 已就绪、可复用（不要重做）

- **TAEP 协议/数据/事务/审批层**：`tests/edit/ai_editing/` 全量 + composition root 共 **1060 passed, 1 skipped**。
  `EditPlan`/`EditOperation`/`TransactionService.preview()/commit()`、`ApprovalService`（一次性 grant + 作用域校验）、
  `SafePathPolicy`、`PathGrant`、`CapabilityManifest` 均为投产级 dataclass 实现。
- **真实命令栈接线**：`ai_edit_host_bridge_impl.py:160-188 commit_ai_command` 真的把 `MacroCommand` 推入活动
  timeline 的 `CommandStack`（`view.execute_command(macro)`），一次 Ctrl+Z 整体撤销，与手动编辑走同一刷新链。
- **能力单一真相（大部分）**：`plan_compiler.py:54 COMPILABLE_OPERATIONS`（18 个 kind）已是事实上的能力真相源，
  `build_manifest()`（`capability_manifest.py:131-133`）从它派生。9 个未实现的 kind（ADD_SUBTITLE 等）
  不会出现在 `capabilities` 输出里。**唯一未收敛的是 `schema.py`**（见工作流 3）。
- **真实 LLM 规划与对话**：`LLMVariantPlanner`（产 ≥2 variant）、`_ChatWorker`（多轮自由对话）均经真实 provider。

### 1.2 端到端闭环验证：两条路径，一通一断

GUI 内"输入意图 → AI 生成方案 → 预览 diff → 审批 → 提交"存在**两条并行路径**，状态不对称：

| 路径 | 入口 | 证据接线 | 状态 |
| --- | --- | --- | --- |
| **B：Skill 工作流路径** | `_dispatch_via_router` → matched skill → `WorkflowRunner.run_workflow` | `workflow.py:426 _build_index` 从 `AnalysisService.get_segments()` 拉**真实 segment** 建非空 `SemanticIndex`；并有 `_collect_missing_evidence` 强制检查 | **通**（前提：素材已分析拿到 transcript/scene 证据） |
| **A：自由对话 / legacy brief 路径** | `_on_request_submitted` → `_run_legacy_brief_path` → `_on_brief_approved` → `service.plan_variants` | `ai_edit_host_bridge_impl.py:77-83 get_semantic_index()` 返回 **空 `SemanticIndex()`**，注释明说"真实分析在 M5 接入" | **断**：`llm_variant_planner.py:196 index.for_asset(...)` 恒得 `[]` → 必抛 `LLMPlannerError("素材 ... 没有可用的分析片段")` |

**结论**：只要素材已分析，路径 B 能端到端跑通（含审批、真实 CommandStack 提交、undo 一致）。但路径 A 必败，
导致用户在面板里的行为不可预测（有时进 skill 卡片走通，有时进 brief→plan 必败）。**这是现阶段最该修的一处。**

### 1.3 持久化断裂

- `assistant_session_store.persist()`（`:139`）/`load()`（`:167`）在整个 `src/` **从未被调用**（仅测试用）。
- `.topos` 工程包没有 `ai/` 目录：`topos_project_io.py:318-348 save` 只写 `edit/color/composite/deliver/ui`。
- `plan_repository.py:17,83` 注释明说"首版为进程内存仓库（M4 接 .topos payload 持久化）"。
- 好消息：`topos_project_io.py:286` Phase B 加载旧包时通过 `unknown_files` 保留未知字段，所以一旦写 `ai/`，旧版本不会丢数据。

---

## 2. 不变量（沿用原计划，不放宽）

以下规则在本计划所有工作中继续生效，是验收的硬门槛：

1. **唯一写入路径**：所有时间线变更经 `request → validation → sequential preview → approval → canonical command → CommandStack → revision → persist/audit`。本计划的持久化工作只新增"读/写 AI payload"通道，不触碰 CommandStack 写入语义。
2. **preview ≡ commit**：preview 用顺序模拟（副本 apply，不 bump revision），commit 用同一份 immutable plan 绑定 base revision。
3. **能力单一真相**：`capabilities` 输出、schema 校验、CLI help 必须从 `COMPILABLE_OPERATIONS` 派生；compiler 没实现的 operation 不得在任何层被判"合法"。
4. **不引入任意代码执行 / secret 泄露 / 路径越权 / UI 线程阻塞**。
5. **重开工程可恢复**：本计划的核心目标之一——AI 状态跨工程重开不丢。

---

## 3. 工作流 1：闭合 GUI 内 AI 剪辑闭环（最高优先级）

**目标**：让"输入剪辑意图 → 生成方案 → 预览 → 审批 → 提交"在任意面板入口、对已分析素材稳定端到端跑通。

### T1.1 接通分析证据 → 规划器（路径 A 修复）

- **问题**：`ai_edit_host_bridge_impl.py:77-83 get_semantic_index()` 返回空 `SemanticIndex()`，导致路径 A（自由对话 / legacy brief）必败。
- **现成可复用**：`workflow.py:426-432 _build_index(asset_ids, analysis_service)` 已实现"从 `AnalysisService.get_segments()` 拉真实 segment 建非空索引"，路径 B 在用。
- **做法**：
  1. 让 `MainWindowAiEditHostBridge`（或其 composition root `ai_editing_mixin.py:227`）持有共享 `AnalysisService` 引用。
  2. `get_semantic_index()` 改为基于当前 timeline 涉及的 asset_ids，复用 `_build_index` 的逻辑构建索引（不要复制粘贴，提取为共享 helper）。
  3. 无证据时按既有红线返回空索引并让规划器产出明确的"请先分析素材"诊断（沿用路径 B 的 `_collect_missing_evidence` 语义），而不是抛异常。
- **规模**：小（~半天）。
- **验收**：对已分析素材，面板自由对话输入"帮我剪一个 30 秒口播精剪"→ 生成 ≥1 个 variant → 不再报"没有可用的分析片段"；对未分析素材给出可执行的"请先分析"提示。

### T1.2 统一两条规划路径

- **问题**：T1.1 完成后路径 A 虽通，但 A/B 并存仍带来 UX 不可预测与双份维护。
- **做法**：T1.1 完成后评估二选一——
  - 方案 a：让路径 A 也复用 `_build_index`（已在 T1.1 完成），保留 A 作为"无 skill 命中时的兜底规划入口"。
  - 方案 b：若 skill 路由已足够覆盖，废弃 `_run_legacy_brief_path`/`_on_brief_approved` 的 plan_variants 分支，统一到 skill 路径。
  - 倾向 a（改动小、保留灵活性），但需在 T1.1 完成后用真实交互验证再定。
- **规模**：小–中。
- **验收**：面板内任意入口产生的方案都拿到真实证据；不存在"进 A 必败、进 B 才通"的不一致。

### T1.3 SceneCutProvider 接真实场景数据

- **问题**：`analysis_service.py:48 SceneCutProvider.analyze()` 默认返回空（`:64` 注释"默认返回空分析"），依赖宿主注入既有 `SceneCutController` 结果，但 composition root 没接这条注入。
- **做法**：在 `ai_editing_mixin.py` composition root 把既有 SceneCutController 的检测结果接入 `SceneCutProvider`（参考 ASR/OCR provider 在 `:492-519` 的注册方式）。
- **规模**：中。
- **验收**：媒体池"分析素材"产出真实场景片段；场景片段进入 `SemanticIndex` 后能被规划器引用为候选。
- **依赖**：T1.1（否则接进来规划器也用不到）。

### T1.4 分析 job 进度上报

- **问题**：分析 provider 同步跑 `provider.analyze()`（`analysis_service.py:527`），不汇报进度，`JobHandle.progress` 字段空转，用户在 job 面板看不到进展。
- **做法**：给长时分析 provider（ASR/OCR）加 `update_progress` 回调；`JobHandle.update_progress` 已存在，只需在 provider 执行中调用。
- **规模**：小–中。
- **验收**：转录/OCR 分析在 job 面板显示进度百分比；可取消（`cancel_all` 在工程关闭时已有，验证 running worker 的协作取消有效）。

---

## 4. 工作流 2：AI payload 接入 .topos 持久化（高优先级）

**目标**：让 AI 状态（session/brief/decision/variant/plan）跨工程重开不丢，从 demo 变可交付。

### T2.1 assistant_session_store 接入工程统一 IO

- **问题**：`persist()`/`load()` 从未被调用；`.topos` 包没有 `ai/` 目录。
- **做法**：
  1. `topos_project_io.py:242 ToposProjectIO.save` 增加 `ai_session_data`（及 `ai_plan_data`）参数，写 `ai/session.json` + `ai/session_catalog.json`（沿用 store 现有文件名）。
  2. `edit_project_io.py:454 save_project` / `:673 load_project` 调 `session_store.persist(target_dir)` / `session_store.load(source_dir)`；composition root 已实例化 store（`ai_editing_mixin.py:369`），只需把保存/加载事件接到 store。
  3. 加载旧包（无 `ai/`）时静默跳过，不报错（`unknown_files` 保留链已支持，验证之）。
- **规模**：中。
- **验收**：分析素材 → 生成方案 → 审批提交 → 保存工程 → 关闭 → 重开 → session/brief/decision 仍在且可展示。

### T2.2 PlanRepository 落盘

- **问题**：`plan_repository.py` 是进程内存仓库，variant/plan 不落盘。
- **做法**：
  1. 给 `EditPlan`/`PreviewRecord` 加 `to_dict`/`from_dict`（多数是 dataclass，机械工作）。
  2. `PlanRepository.store()` 落 `ai/plans/<plan_hash>.json`；`load()` 从该目录恢复。
  3. preview/diff 结果随 plan 一起落盘（immutable，hash 寻址）。
- **规模**：中。
- **验收**：重开工程后，历史 variant/plan 仍可在 diff 面板回看；相同 plan_hash 命中同一份 immutable plan。

### T2.3 schema version 与迁移

- **做法**：AI payload 目录加 `ai_schema_version`；复用 `assistant_session_store.py:199 migrate_legacy_snapshot` 的迁移模式；未知字段 round-trip 保留（与工程 IO 的 `unknown_files` 策略一致）。绝不序列化 provider API key（key 只在 `CredentialStore`，profile 只存 `credential_ref`）。
- **规模**：小。
- **验收**：写入带版本号的 payload；用旧版本加载新包不丢未知字段；新版本加载无版本号旧包走迁移分支。

---

## 5. 工作流 3：能力单一真相收尾 + 精简只读 CLI（中优先级，可并行）

本工作流可与 1、2 并行，互不阻塞。

### T3.1 schema.py 收敛到 COMPILABLE_OPERATIONS

- **问题**：`schema.py:186-200 validate_plan_structure` 用 `OperationKind(kind_str)` 判合法性——只要在枚举里（27 个）就算结构合法，不查 `COMPILABLE_OPERATIONS`。后果：模型生成 `add_subtitle` plan，schema 校验**通过**，到 domain 层（`plan_validator.py`）才报 `UNSUPPORTED_OPERATION`。这是"诱导模型后才失败"，正是原计划 §4.3 想消除的。
- **做法**：让 `validate_plan_structure` 接受可选的 compilable 集合（默认从 `COMPILABLE_OPERATIONS` 取），对未实现 kind 直接在结构校验阶段产 `UNSUPPORTED_OPERATION` 诊断，不让它过 schema 关。
- **规模**：小。
- **验收**：模型生成未实现 operation 的 plan，结构校验阶段即被拒（诊断 code = `UNSUPPORTED_OPERATION`），不再留到 domain 层；已实现的 18 个 kind 不受影响。

### T3.2 DTO builder 强类型化

- **问题**：`plan_compiler.py:390-409 _compile_set_effect` / `362-387 _compile_set_param` 把 effect/param 作为**裸 dict** 塞进 `UpdateClipParamCommand`（`new_value={...}`），缺少类型约束。
- **做法**：为每个 operation 定义强类型 DTO builder，禁止任意 dict；沿用项目既有 dataclass 模式。
- **规模**：中。
- **验收**：compiler 内部不再出现裸 dict 作为 command 参数；类型错误在编译期可发现。

### T3.3 精简只读 toposctl（不建 4 层架构）

- **关键决策**：**不**按原计划建完整的 4 层 automation 架构（`AutomationService.dispatch` / `OfflineAutomationContext` / `topos.automation/v1` schema / `AutomationCapabilityRegistry` 框架）。理由：现有 `ToolFacade` 已是 transport-neutral 的语义入口（每个响应 `json.dumps` 安全，`sanitize_untrusted` 已防注入），建完整 dispatch 层对"只读 CLI"是 over-engineering。现有 `src/features/edit/ai_editing/toposctl.py` 的 `run_cli()` 已实现 capabilities/snapshot/search-assets/validate-plan 等命令路由，直接复用。
- **做法**：
  1. `pyproject.toml` 注册 console_scripts：`toposctl = "src.features.edit.ai_editing.toposctl:main"`。
  2. 给 `toposctl.py` 加 `main(argv=None)`：解析 → 调 `run_cli` → stdout 输出**单一 JSON**（`CliResult.to_dict()` 已可序列化）→ stderr 走日志 → `sys.exit(exit_code)`。
  3. 补 `version` 子命令（返回 `TAEP_PROTOCOL_VERSION` + CLI 版本）。
  4. 补 `timeline inspect`（专注 timeline 摘要，时间用 rational ticks，不浮点秒）——现 `snapshot --timeline` 可复用但要拆出独立命令语义。
  5. 加 `--output json|text`（默认 json）、`--request-id`、`--timeout`、`--log-level` 通用选项。
  6. **无 Qt/GL import guard 测试**：断言导入/运行 CLI 模块不拉入 `PyQt6.QtWidgets`/`moderngl`/`OpenGL`、不在无显示环境创建 `QApplication`/GL context。这是原计划 A0 的硬验收，必须落实。
- **规模**：中。
- **不做**：`project verify`（schema/media/plugin/revision 健康检查）延后——它依赖完整的离线工程加载，工作量与本工作流其它项不匹配，留到真正需要 CI 集成时。
- **验收**：对真实 `.topos` 工程，`version`/`capabilities`/`snapshot`/`timeline inspect`/`search-assets` 返回可 `json.loads` 的单一 JSON；错误情况（错误 project、未知命令）给稳定 exit code；pytest 中不创建 GL context。

---

## 6. 明确不做（现阶段推迟）

以下出自原 CLI 计划，**本计划不执行**，待 GUI 内闭环稳定投产后再评估：

| 原计划项 | 推迟原因 |
| --- | --- |
| 完整 automation 4 层架构（AutomationService/dispatch/schema v1/AutomationCapabilityRegistry 框架） | 现有 ToolFacade + CapabilityManifest 已覆盖其语义；建框架属 over-engineering。T3.3 用组合而非重写。 |
| A2 离线锁 + 原子 commit | GUI 闭环刚闭合，离线写工程与 GUI 抢 revision/媒体缓存，风险高。 |
| A3 Connected GUI host + 本机 IPC | 工程量大，回报要等闭环成熟后才兑现。 |
| A4 asset/analysis/export/jobs CLI 写命令 | 写入命令依赖离线锁与真实 JobScheduler 持久化（本计划 T2 仅做 payload，未做 job 持久化）。 |
| A5 FCPXML/OTIO/MCP adapter | 无争议的最后一步。 |
| generation_provider（视频/音乐/语音 stub） | 属 G0/G1，留 M11，不在"AI 剪辑"范畴。 |
| Job 持久化与可恢复（JobRegistry/AnalysisService 落盘 + 重开恢复） | 比 T2 的 payload 持久化优先级低；长转录被打断才痛，可排 T2 之后。 |

---

## 7. 实施顺序与依赖

```text
T1.1 接通 get_semantic_index（半天）
  │  立刻让闭环在已分析素材上跑通（最高 ROI）
  ├─→ T1.2 统一两条路径（评估后定 a/b）
  ├─→ T1.3 SceneCutProvider 接真实数据（依赖 T1.1 才有意义）
  └─→ T1.4 分析 job 进度上报（独立）

T2.1 + T2.2 + T2.3  AI payload 持久化（可与 T1 并行）
  │  让闭环跨工程重开
  └─→ （后续）Job 持久化（本计划不做，留待闭环投产后再排）

T3.1 schema 收敛（小，可随时插入，独立）
T3.2 DTO 强类型化（中，独立）
T3.3 精简只读 CLI（中，建议 T1.1/T2.1 完成后做，复用稳定的能力面）
```

**建议第一步**：T1.1（半天）。它是让既有全部能力（真实 provider、CommandStack、审批、证据）贯通的唯一开关，
做完立刻能在已分析素材上端到端跑通"输入意图 → 方案 → 审批 → 提交 → undo"。紧随其后做 T2 让闭环跨重开工程。

---

## 8. 验收门（DoD）

本计划任一任务标记完成前，须满足：

1. **类型化**：新增/修改的公共接口有类型注解；payload 有 schema version。
2. **唯一写入路径**：不出现绕过 CommandStack 的第二写入路径；preview ≡ commit 不破坏。
3. **测试**：正常 + 失败 + 边界（未分析素材、空 timeline、重开工程、撤销/重做）均有 focused test；
   `pytest` 相关范围通过；不引入 Qt/GL import 到纯逻辑层（T3.3 须有无 GL guard 测试）。
4. **手工验收**：至少跑一遍"分析素材 → 生成方案 → 预览 diff → 审批 → 提交 → undo → 保存 → 重开"。
5. **能力真相**：capabilities 输出 / schema 校验不宣称 compiler 未实现的 operation（T3.1 落实后此项为硬门槛）。
6. **无回归**：现有 1060 + 85 个 AI 相关测试不出现新的失败。

---

## 9. 与既有里程碑编号的关系

- 本计划对应 `ai_editing_stabilization_and_production_plan` 的 **M3 收尾**（能力注册顺序模拟）+ **M4**（Decision 反馈 + AI payload 接工程保存）。
- 本计划 **不** 对应 M5 的"真实 provider"（已提前完成）和"Job 持久化"（推迟）。
- 与 `ai_assistant_agent_harness_product_plan` 的 M0–M12 产品路径正交：本计划只闭合"已有能力的最后一公里接线"，不新增产品工作流。

---

## 10. 实施状态（2026-08-04）

| 任务 | 状态 | 提交 | 说明 |
| --- | --- | --- | --- |
| T1.1 接通 get_semantic_index | ✅ 完成 | `60c893ac` | 路径 A 不再必败；共享 helper `build_index_from_analysis` |
| T1.2 统一两条路径 | ✅ 完成（评估） | `7ab6557c` | 评估结论：T1.1 已足够，A/B 并存是合理设计 |
| T1.3 SceneCut 接真实数据 | ✅ 完成 | `d9ea6bcc` | `ingest_scene_cut_analysis` + EditPage 信号接线 |
| T1.4 分析 job 进度上报 | ✅ 完成 | `bd5578c3` | mark_running 提前 + progress callback 传 provider |
| T2.1/T2.2/T2.3 payload 持久化 | ✅ 完成 | `a9a021e9` | 序列化 + IO 通道 + 接线全闭环 |
| T3.1 schema 收敛 COMPILABLE | ✅ 完成 | `0f99a282` | schema 阶段早拒绝未实现 operation |
| T3.3 精简只读 toposctl | ✅ 完成 | `329f97e7` | version/离线 capabilities + main + console_scripts |
| T3.2 DTO 强类型化 | ⏸ 推迟 | — | 评估结论：风险高于收益（见下） |

### T3.2 推迟理由

`UpdateClipParamCommand.new_value: Any` 是有意设计——它要支持
TransformParams/CropParams/SpeedParams/float/bool/effect-dict 等 7+ 种值类型。
强类型化需要引入联合类型或泛型，触及 `commands.py`（13.8K 行核心模块）的
序列化/undo/redo 链路，回归风险高。当前裸 dict 的边界已被 schema 校验
（T3.1 已加强）和 compiler 局部封装保护，实际 bug 风险低。TypedDict 只能加
编译期提示、不约束运行时值，收益有限。故推迟到 commands.py 有独立重构窗口时。

### 测试覆盖

- 新增 8 个测试文件，共 **~50 个新测试**。
- `tests/edit/ai_editing/` 全量 **1079 passed, 1 skipped**（含 T3.1/T3.3/T1.3/T1.4 新增）。
- `tests/project/test_ai_payload_io.py` **5 passed**（IO 通道往返）。
- `tests/app/` 三个接线测试文件 **16 passed**（持久化接线 + 路径 A 证据流 + bridge）。
- 预先存在的 `test_ai_edit_controller.py` 5 个失败（`_FakePanel` 缺属性）与本次无关。

### 未做（明确推迟，符合 §6）

原 CLI 计划 A2–A5（离线锁、connected IPC、export/jobs CLI、FCPXML/OTIO/MCP）、
完整 automation 4 层架构、Job 持久化与可恢复——这些等 GUI 内闭环（本计划）
稳定投产后再评估。
