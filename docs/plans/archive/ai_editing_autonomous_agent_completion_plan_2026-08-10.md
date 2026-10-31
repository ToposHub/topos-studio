# Topos Color 自主剪辑 AI Agent 完成计划表

日期：2026-08-10  
目标：把当前“具备自主循环骨架、可受控执行的 AI Assistant”升级为“能够在真实工程、真实素材和真实导出链路上持续完成剪辑任务，并且可观察、可审批、可恢复、可回滚的自主运行 AI Agent”。

## 1. 先给结论：这次完成的定义

本计划的“完成”不是让模型拥有无限权限，也不是把所有操作隐藏在一个 Run 按钮后面。商业剪辑场景的正确目标是：

> Agent 可以在用户指定的项目、时间线、素材范围、brief、预算和权限内，自主完成 Observe → Plan → Gate → Act → Verify → Recover；涉及时间线写入、生成媒体、云上传和最终发布时，严格经过可见的审批/质量门；任何失败都能停在可解释的状态，并支持重试、恢复、回滚或人工接管。

完成后必须同时满足：

1. Chat、快捷任务、Workflow 面板三条入口使用同一个真实运行时、同一个 Job/Run 记录和同一套取消、重试、恢复、审批、审计协议。
2. Workflow 面板不再执行合成服务；点击 Run 时能够读取真实工程的 project/timeline/brief/asset/revision，并调用真实 Assistant/Skill/Provider 后端。
3. Agent 生成的计划先以结构化 diff 展示，再按权限策略执行；不能绕过 `EditPlan → Approval → CommandStack` 写入时间线。
4. 每个动作拥有 scope、idempotency key、输入证据、输出 artifact、成本、耗时和诊断；重启后能从 checkpoint 继续或安全地重新规划。
5. 每次时间线写入后都必须做 canonical timeline 校验、revision 校验、素材引用校验和必要的媒体/字幕/品牌/平台/音频质量校验。
6. 最终导出只有在 manifest、thumbnail、ffprobe、hash、帧级分析、音频分析、字幕/品牌/平台合同全部通过后才报告成功。
7. 真实 provider、真实素材和真实长任务上有可复现证据；fake provider 只用于确定性单元测试，不能被当成生产完成证明。
8. 工作流面板能够让用户看懂“Agent 正在做什么、为什么做、改了什么、卡在哪里、下一步需要谁确认”。

## 2. 当前基线和关键审计结论

### 2.1 已经具备的基础

- `src/features/edit/ai_editing/agent_loop.py` 已有有界的 Observe → Plan → Gate → Act → Verify → Recover 合同、动作级策略、成本/工具/循环上限和 checkpoint 模型。
- `HarnessRuntime` 已提供统一的 run 生命周期、事件流、取消、retry、resume、session detach 和线程安全状态聚合。
- `AssistantService`、`SkillRuntime`、`WorkflowRunner`、`WorkflowCompiler` 已形成计划、workflow、skill 的基础接缝。
- 真实 DeepSeek 规划链路和真实素材分析/导出质量门已经通过实测；最近 AI 回归为 `1220 passed / 2 skipped`。
- 真实双源 4K 长样本已经完成 H.264/HEVC 四任务双 worker 并发，`4/4` post-ffprobe 通过。
- 导出侧已有平台、字幕、品牌元素、音频响度、静音、manifest、thumbnail、ffprobe、hash 和帧级质量门。

### 2.2 当前仍然阻止“真正生产 Agent”的事实

- `src/app/controllers/dock_layout_controller.py` 装配 Workflow 面板时仍使用 `FakeWorkflowService`；面板运行是合成运行，不是真实模型/真实时间线执行。
- `WorkflowRunController` 当前编译时使用空的 `timeline_id`、空的 `selected_asset_ids` 和固定的 `permission="propose"`，没有绑定活动工程上下文、brief revision 和实际资产范围。
- Workflow 面板的 Run Drawer 目前主要显示一个 run 项；节点状态可以显示，但计划 diff、审批、artifact、质量报告、恢复点和多 run 队列没有完整展示。
- `AiRunDrawer` 虽然存在 retry/diagnostics signals，但 Workflow 面板装配侧目前只连接 Run/Cancel，重试和诊断没有形成完整闭环。
- `FakeWorkflowService` 可以生成节点状态，但不产生真实 edit plan、真实 artifact、真实 provider 结果或真实 CommandStack 写入。
- `P3/P6/P7` 的核心骨架已在，但完整多节点 controller、所有宿主动作接线、重启后自动恢复、全动作覆盖和运营值班流程仍需完成。
- 当前品牌能力是“EditorialBrief + 显式素材标注 + 时间线物化 + 质量门”；任意未标注素材的视觉 Logo/免责声明识别仍未完成。
- 当前多源长样本已经有双源四任务证据，但更大源数量、更长商业片、资源压力、队列公平性和生产取消延迟仍需验收。
- P2 真实 provider 凭据、内容安全、license、区域条款和 P11 Alpha/Beta 灰度数据不能用 mock 结果替代。

## 3. 总体路线和完成批次

建议按以下顺序推进。每批必须先完成代码、测试、真实证据和文档，再进入下一批。

| 批次 | 目标 | 必须完成的结果 | 进入条件 | 退出条件 |
|---|---|---|---|---|
| B0 基线冻结 | 固定当前行为和测试矩阵 | 记录 provider、项目、时间线、素材、权限、feature flag、回滚点 | 当前分支可运行 | 基线测试、真实素材清单和失败分类可复现 |
| B1 运行时统一 | Workflow/Chat/Skill 共用真实 Agent Runtime | 一个 HarnessRuntime、一个 JobRegistry、一个 Invocation/Run/Event/Artifact 协议 | B0 | Workflow 不再依赖 FakeWorkflowService 作为应用默认执行器 |
| B2 工程上下文绑定 | 将真实 project/timeline/brief/revision/assets 注入 Invocation | scope、base revision、analysis hash、brief id、profile id 全部可审计 | B1 | stale revision、错误工程、越权素材在编译前阻断 |
| B3 真实 Observe/Plan | Agent 从真实证据生成结构化方案 | timeline snapshot、analysis evidence、brief、约束、候选方案、plan hash | B2 | 无证据时停在可解释诊断，不得假装理解素材 |
| B4 真实 Gate/Act | 计划预览、审批、命令提交、幂等写入、补偿回滚 | EditPlan/Approval/CommandStack 与 workflow node 一一映射 | B3 | 任一动作可取消、重试、回滚，绝不产生半套时间线 |
| B5 Verify/Recover | 每步验证、失败恢复、重启续跑 | checkpoint、retry/resume、人工接管、恢复原因、重规划 | B4 | 进程重启/网络失败/Revision 冲突均能安全处理 |
| B6 Workflow 面板升级 | 将面板变成 Agent 控制台 | 上下文栏、计划 diff、审批中心、分层 Run Drawer、artifact/诊断、恢复操作 | B2–B5 | 用户能从面板完成一次完整的观察→审批→执行→验收 |
| B7 Provider/素材增强 | 真实 provider、品牌识别和多源调度 | DeepSeek/ASR/OCR/TTS/生成 provider 按需接入；多源队列和品牌证据链 | B3–B5 | 真实凭据和条款验证通过，错误隔离和成本门有效 |
| B8 商业发布验收 | 完整端到端和灰度运营 | 从素材导入到最终导出、回滚、审计、指标、灰度开关全部实测 | B1–B7 | Alpha/Beta checklist 全部通过，才能宣称生产可用 |

## 4. 详细剩余任务表

优先级：P0 = 阻断“真正自主 Agent”；P1 = 阻断生产安全/可维护性；P2 = 提升覆盖和商业化成熟度。

| ID | 优先级 | 任务 | 主要改造位置 | 验收标准 | 证据/测试 |
|---|---|---|---|---|---|
| A-001 | P0 | 建立统一 `AgentSession/RunContext` | `agent_loop.py`、`runtime.py`、`jobs.py`、`assistant_service.py` | Chat、Skill、Workflow 使用同一 run_id、session_id、scope、checkpoint、event stream | 三入口同 run schema 集成测试；旧 run 不回写已关闭工程 |
| A-002 | P0 | Workflow 去除生产默认 Fake Service | `dock_layout_controller.py`、`workflow_run_controller.py`、`legacy_adapters.py` | fake 只在测试 flag；应用默认注入真实 `AssistantService`/Skill backend | 真实 provider workflow 测试；fake/default wiring 断言 |
| A-003 | P0 | 绑定真实工程上下文 | `WorkflowRunController`、`WorkflowCompiler`、`ai_edit_host_bridge_impl.py` | 传入 project_id、timeline_id、selected_asset_ids、brief_id、base_revision、analysis hash、profile id | 错工程/错时间线/stale revision/越权 asset 均编译失败 |
| A-004 | P0 | Workflow 模板映射到真实 Skill/Workflow | `template_registry.py`、`skill_manifest.py`、`assistant_service.py` | 每个官方模板有明确 skill_id、输入 schema、输出 artifact、所需能力、写入策略 | 七类模板逐一跑真实 runner；无默认隐式动作 |
| A-005 | P0 | 计划预览和结构化 diff | `workflow_compiler.py`、`transaction_service.py`、`ai_edit_controller.py` | Run 前显示节点顺序、工具、素材范围、时间线写入、风险、成本、预计输出和 plan hash | 同一输入 preview/compile 稳定；变更可定位到 clip/track/brief |
| A-006 | P0 | 审批与权限贯穿每个动作 | `execution_policy.py`、`workflow_validator.py`、`approval_service.py` | 读、提议、逐次审批、受限自动四档权限真实生效；生成/上传/导出不能绕过 consent | 缺审批 token、网络禁止、预算超限、timeline 写入均 fail closed |
| A-007 | P0 | 真实时间线写入和幂等 | `EditPlan`、`EditProjectService`、`commit_ai_command`、workflow actor | 每个 node action 有 idempotency key；重复事件不会重复加 clip/track；写入只经 CommandStack | 执行中断/重复重试/重复点击的 byte/canonical invariant 测试 |
| A-008 | P0 | Verify 与发布前质量闭环 | `delivery_quality_gate.py`、`export_service.py`、host bridge | 时间线校验、媒体重开、字幕、品牌、平台、音频和 manifest 全通过才成功 | 真实 H.264/HEVC/ProRes/DNxHR、多源、损坏输出、错误规格矩阵 |
| A-009 | P0 | 失败恢复与人工接管 | `agent_loop.py`、`runtime.py`、`assistant_session_store.py`、JobRegistry | provider timeout、网络断开、cancel、revision conflict、部分 action 失败可 retry/resume/rollback/ask | 故障注入覆盖；恢复后不重复已完成步骤 |
| A-010 | P0 | Workflow 面板接入真实运行时 | `workflow_panel.py`、`workflow_run_controller.py`、`dock_layout_controller.py` | 面板 Run 触发真实 Invocation；节点事件来自真实 runtime；状态不是 synthetic | 面板真实 workflow E2E；禁止 FakeWorkflowService 默认启用 |
| A-011 | P1 | Run Drawer 多层任务视图 | `ai_assistant_composer.py`、`workflow_run_controller.py` | 显示 run→node→job→artifact 层级、状态、进度、耗时、scope、成本、输出和错误 | 多节点并发、队列、等待确认、成功/警告/失败截图或状态快照 |
| A-012 | P1 | retry/resume/diagnostics/rollback 接线 | `AiRunDrawer`、`WorkflowPanel`、`AiEditController`、runtime | 每个按钮都有真实 backend handler；诊断可查看；恢复点、重试来源、回滚结果可见 | UI signal→runtime action 集成测试；无 handler 不显示按钮 |
| A-013 | P1 | 会话重启和长任务监控 | `assistant_session_store.py`、`jobs.py`、`session_catalog.py` | 重启后恢复未终态 run；面板能显示历史、checkpoint 和接管入口 | 关闭/重开项目、网络断开、进程重启演练 |
| A-014 | P1 | 真实 provider 连接和条款 | provider adapters、credential store、Preferences、privacy guard | DeepSeek、ASR/OCR/TTS/生成按实际授权完成连接测试；上传确认、license、区域、成本均有记录 | 真实凭据仅在本地安全环境；不把 mock 结果写成 provider 成功 |
| A-015 | P1 | 观察层升级 | `analysis_service.py`、`semantic_index.py`、context service | Agent 读取素材探针、转录、场景、镜头、音频、字幕和当前 timeline；证据带 fingerprint/provenance | 同素材变更自动失效；无证据明确阻断规划 |
| A-016 | P1 | 多源调度与资源预算 | `jobs.py`、`analysis_scheduler.py`、`encode_benchmark.py`、runtime scheduler | 多素材分析/编码有并发上限、队列公平、单源失败隔离、取消传播、磁盘/CPU/网络预算 | 双源四任务已通过；扩展到 4/8 源和长片压力矩阵 |
| A-017 | P1 | 品牌元素识别升级 | `delivery_contracts.py`、analysis providers、host bridge | 保留显式标注路径；新增可选 OCR/视觉 detector，结果必须带 confidence、bbox、source、人工确认状态 | 已知标注、无标注、误检、越界、遮挡、低置信度全部可解释 |
| A-018 | P1 | 面板上下文栏和安全状态 | `workflow_panel.py`、`AiAssistantComposer`、host bridge | 面板显示项目、时间线、revision、brief、素材数、provider、权限、预算、网络和自动模式 | 切工程/切时间线/brief 变化时 UI 与 invocation 一致 |
| A-019 | P1 | 面板计划/证据/审批工作区 | `workflow_panel.py`、new plan/diff widgets、approval service | 图、步骤列表、证据引用、风险、diff、审批项、质量门分栏；用户可逐步批准/拒绝/跳过 | 计划 hash 与批准 grant 绑定；过期计划不能继续 |
| A-020 | P1 | 面板 artifact 与时间线联动 | `workflow_panel.py`、`ai_edit_controller.py`、host bridge | 点击 artifact/diagnostic 可定位时间线 clip、字幕、素材、质量报告；preview 与 apply 清晰区分 | 无效引用不崩溃；人工修改后提示 stale 并要求重规划 |
| A-021 | P2 | 自动模式和人工接管策略 | `execution_policy.py`、Preferences、panel | 用户可配置“仅建议/半自动/受限全自动”；危险动作始终独立审批；一键 pause/hand-off | 策略矩阵覆盖所有 action kind，不存在未分类动作 |
| A-022 | P2 | 评测、成本和运营仪表盘 | `eval_runner.py`、`rollout_metrics.py`、RunDrawer | 记录成功率、重试率、恢复率、审批等待、成本、延迟、质量门失败原因和用户接管率 | Alpha/Beta 指标门、回滚门和隐私脱敏检查 |
| A-023 | P2 | 全商业端到端验收 | tests/e2e、真实素材、export service | 导入→分析→brief→规划→预览→审批→写入→字幕/品牌→导出→manifest→回滚全链路通过 | 至少 3 类真实剪辑任务、2 个平台、2 个 codec、成功/失败/恢复三套证据 |
| A-024 | P2 | 文档、教程和故障手册 | `docs/`、help index、panel tooltips | 用户知道如何配置 provider、设置 brief、批准计划、处理失败、恢复任务和验证导出 | 新工程按文档独立完成一次任务；无秘密/绝对路径泄露 |

## 5. Workflow 面板专项升级方案

### 5.1 当前面板能做什么

当前 `WorkflowPanel` 已经有：

- 模板下拉、Load/Clear、节点画布、节点搜索、节点连线、节点移动撤销/重做；
- Node Inspector 参数编辑；
- Run/Cancel 按钮；
- 复用 `AiRunDrawer` 的状态卡、进度、取消、重试、诊断信号；
- `WorkflowCompiler`、`WorkflowValidator` 和 `HarnessRuntime` 的基础接缝。

这些能力足够作为 UI 外壳继续升级，不建议重写面板。

### 5.2 当前面板的关键缺口

| 缺口 | 当前事实 | 高级 Agent 需要的升级 |
|---|---|---|
| 运行后端 | app 装配 `FakeWorkflowService` | 注入真实 Assistant/Skill backend；fake 仅测试 flag |
| 作用域 | compiler 使用空 timeline 和空 asset scope | 顶部显示并绑定 project/timeline/brief/revision/assets |
| 权限 | workflow controller 固定 `propose` | 显示并继承 Composer/Preferences 的执行策略 |
| 计划可见性 | 只有图和 Run | Run 前显示 Plan/Diff/Risk/Evidence/Cost |
| 审批 | graph policy 有字段，但面板没有审批工作区 | 每个写入/生成/上传/最终导出动作显示审批卡 |
| 运行粒度 | drawer 目前主要聚合单个 run | run→node→job→artifact 的层级树和并发状态 |
| 恢复入口 | runtime 有 retry/resume，但 Workflow UI 没有完整接线 | 失败卡提供 retry/resume/rollback/hand-off/diagnostics |
| 诊断 | 有 signal 定义，但 workflow 装配未完整连接 | 展示 error code、失败步骤、证据、建议下一步、原始脱敏日志 |
| 时间线联动 | 面板和编辑时间线相互独立 | 点击 node/artifact/diagnostic 高亮对应 clip/track/marker |
| 历史任务 | Run Drawer 偏当前活动 | 增加 Session/Run history、checkpoint、恢复上次任务 |
| 真实性提示 | fake run 仅在代码注释中说明 | UI 明确显示 provider、synthetic/real、scope、质量门状态 |

### 5.3 建议的面板布局

```text
┌─────────────────────────────────────────────────────────────────────┐
│ 工程 / 时间线 / Brief revision / 素材数 │ Provider │ 权限 │ 预算 │ 状态 │
├───────────────┬───────────────────────────────┬─────────────────────┤
│ 节点模板/搜索  │ Workflow Canvas               │ Inspector           │
│               │ 计划节点、风险角标、证据标记  │ 参数/输入/输出       │
├───────────────┴───────────────────────────────┴─────────────────────┤
│ Plan │ Diff │ Evidence │ Approval │ Run / Verify / Release          │
├─────────────────────────────────────────────────────────────────────┤
│ Run Drawer：run → node → job → artifact；进度、日志、诊断、恢复操作 │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.4 面板交互验收

1. 用户加载“30 秒产品广告”模板，面板自动显示当前项目、时间线、brief revision、素材数和可用 provider。
2. 用户点击“预览计划”，不执行任何写入；面板显示预计新增/删除/修改的 clip、字幕、品牌元素和导出设置。
3. 缺少转录、模型、授权、预算或安全区测量时，面板显示阻断原因和唯一下一步，不显示一个可点击的假 Run。
4. 用户逐项批准后执行；每个节点和真实 job 产生状态、进度、artifact 和成本记录。
5. 用户可以取消当前节点；已完成节点保留，未完成节点停止，时间线保持合法且可恢复。
6. 网络或 provider 失败后，面板显示失败步骤、error code、可重试性、checkpoint 和“重试/恢复/人工接管”按钮。
7. 用户修改时间线或 brief 后，面板立即显示 stale revision；旧计划不能继续 apply。
8. 最终导出显示质量门逐项结果，只有全部通过才显示“可交付”；失败时保留 manifest/诊断而不是只显示通用错误。

## 6. 具体实现顺序

### 第 1 批：真实运行链路收敛（P0）

- 新增统一 workflow invocation adapter，将 `WorkflowRunController` 的 `FakeWorkflowService` 替换为真实 `AssistantService`/Skill backend。
- 从 MainWindow/host bridge 读取 project、active timeline、brief、selected assets、base revision、analysis snapshot 和 profile。
- 将 compiler 的硬编码空 scope 和默认 brief 改为真实上下文；任何上下文缺失先返回诊断。
- 保留 fake backend 作为 `AI_WORKFLOW_FAKE_PROVIDER=1` 或测试 fixture，不允许生产默认路径使用。
- 验收：真实 DeepSeek profile 触发一次 workflow，从真实 analysis evidence 产生计划，未审批前不改 timeline。

### 第 2 批：真实动作、审批和恢复（P0）

- 将 workflow node 的 action 映射到 `WorkflowSpec/SkillManifest/AgentStep`，每个 action 声明 scope、预算、网络、timeline write、approval、idempotency key。
- 计划预览统一复用 `PreviewResult`/diff；批准统一复用 `ApprovalService` 和 host 的 `commit_ai_command`。
- 失败动作执行 host-owned rollback；retry 不重复已完成步骤；resume 必须检查 context hash、plan hash、base revision。
- 验收：执行中断、取消、重试、重启、revision 冲突、审批拒绝各跑一遍，时间线没有半套状态。

### 第 3 批：Workflow 面板 Agent 控制台化（P1）

- 增加 Context Bar、Plan/Diff/Evidence/Approval tabs、Run History 和 Release Gate tabs。
- 将 `AiRunDrawer` 的 retry/diagnostics/cancel 信号全部接到真实 controller；未连接 handler 的按钮不显示。
- Run Drawer 从单 run 扩展为 run/node/job/artifact 分层，并支持等待用户、暂停、恢复、人工接管。
- 计划节点与时间线 clip/字幕/导出报告建立可点击关联。
- 验收：仅使用面板完成一次真实广告工作流，不需要打开开发日志判断状态。

### 第 4 批：Provider、素材和多源生产能力（P1）

- 在实际授权环境完成 DeepSeek、ASR/OCR、TTS/生成 provider 的连接、取消、超时、license、区域和成本验证。
- 将声明式品牌标注保留为确定性基线，新增可选视觉/OCR detector；低置信度必须进入人工确认，不直接放行。
- 将当前双源四任务 benchmark 扩展为 4/8 源、长片、不同 codec、磁盘压力、取消和失败隔离矩阵。
- 验收：一条源失败不影响其他源；重试只重做失败节点；并发上限和成本预算真实生效。

### 第 5 批：商业端到端与灰度（P1/P2）

- 选择至少三类任务：产品广告、访谈/口播、字幕/多语言版本。
- 每类任务至少验证两个平台交付规格、两个 codec、一次正常导出、一次质量门失败、一次恢复/回滚。
- 建立 Alpha/Beta/Stable 指标：成功率、质量门失败率、人工接管率、重试率、恢复成功率、成本、延迟、崩溃、重复写入和版权/隐私事故。
- 验收：真实用户可以从项目打开、素材分析、brief 设置、计划批准、时间线提交到最终交付完成；任何失败都有可定位记录和安全回滚。

## 7. 必须坚持的安全和产品红线

- Agent 默认是“有界自主”，不是无限权限；最终发布、云上传、生成媒体和不可逆写入必须有独立策略。
- 模型输出不能直接成为 timeline mutation；必须经过 schema、scope、revision、policy、approval、command 和 verify。
- 不能把 fake provider、静态模板或成功返回的进程当成真实完成证据。
- 所有长任务必须有取消、超时、checkpoint、重试上限和资源预算；不能强杀造成半套工程。
- 绝对路径、API key、原始 prompt、远端临时 URL 和敏感素材内容不能进入公开 run drawer、manifest 或诊断日志。
- 视觉品牌识别必须显示来源和置信度；不确定时阻断或请求人工确认，不能凭文件名或模型猜测放行。
- 当前工程、时间线、brief revision 变化后，旧计划必须 stale；不能 last-write-wins。

## 8. 最终发布门（Definition of Done）

只有以下项目全部通过，才可以把产品对外称为“可协助剪辑的自主运行 AI Agent”：

| 门 | 通过条件 |
|---|---|
| 运行时统一 | Chat/Skill/Workflow 同一 Runtime、Job、Run、Event、Artifact、Cancel/Retry/Resume |
| 真实后端 | Workflow 默认不再走 FakeWorkflowService；真实 provider 有授权、取消、超时和成本证据 |
| 上下文正确 | project/timeline/brief/assets/revision/analysis/profile 绑定且 stale 能阻断 |
| 计划安全 | Plan hash、Diff、Evidence、Risk、Cost、Approval 可见且可审计 |
| 动作可靠 | 幂等写入、CommandStack、审批、回滚、重试不重复执行 |
| 观测完整 | 节点、job、artifact、诊断、日志摘要、成本、耗时、质量门在面板可见 |
| 恢复可用 | cancel、retry、resume、rollback、人工接管、重启恢复均有真实测试 |
| 质量可交付 | ffprobe、hash、thumbnail、manifest、平台、字幕、品牌、音频、帧级门全部通过 |
| 素材覆盖 | 至少三类真实任务、两类平台、两种 codec、多源长任务和失败隔离通过 |
| 面板闭环 | 用户可只通过 Workflow/Assistant 面板完成预览、审批、执行、诊断、恢复和验收 |
| 运营可控 | Alpha/Beta 指标、feature flag、kill switch、回滚、隐私/版权事故流程可演练 |

## 9. 本计划的第一执行点

下一批不应继续增加孤立的 fake node 或只增加 UI 文案。第一执行点应是：

1. 把 `WorkflowRunController` 的 fake backend 替换为真实 `AssistantService`/Skill backend adapter；
2. 把 active project/timeline/brief/revision/assets 注入 compiler/invocation；
3. 为真实 workflow 先做 Plan Preview，不允许第一步就写时间线；
4. 以“30 秒产品广告”作为第一条面板端到端验收基线；
5. 只有真实 workflow 跑通后，才继续扩展 Run Drawer 的高级交互和更多节点。

该计划与现有总计划的 P2/P3/P6/P7/P8/P10/P11 对齐；它把此前分散的剩余事项收敛为一条可执行的“真实后端 → 真实上下文 → 安全动作 → 面板闭环 → 商业验收”路径。

## 10. 本次执行记录（2026-08-10，首批 B1–B3）

本批已完成并通过聚焦回归：

- **B1 / A-001、A-002、A-010（首批范围）**：Workflow 继续复用共享 `HarnessRuntime` 和 `LegacyHarnessAdapter`；宿主默认通过动态 `AssistantService` provider 运行，`FakeWorkflowService` 只有在显式设置 `AI_WORKFLOW_FAKE_PROVIDER=1` 时才装配。未装配真实后端时面板显示明确诊断，不启动 synthetic run。
- **B2 / A-003**：新增 `WorkflowInvocationContext`，从 host bridge 读取 project、active timeline、selected assets、timeline revision、brief revision、profile 和 live analysis provider 能力；缺少工程、时间线或素材范围时在编译前 fail closed。
- **B3 / A-004、A-005、A-015（首批范围）**：官方模板的 `skill_id` 随 graph 进入真实编译；analysis evidence 通过 `AssistantService.get_analysis_evidence()` 注入 editorial adapter；编译产物提供非写入性的结构化 plan preview、scope、节点动作、风险、成本和稳定 `plan_hash`。相同输入重复编译 hash 稳定，revision/evidence 变化会使 hash 变化。
- **质量修复**：修复一个阻断全仓语法检查的 TTS 设置对话框字符串引号错误；`python3 -m compileall -q src` 已通过。

本批验证结果：Workflow compiler/context/runtime 相关测试、Workflow controller/panel acceptance 测试及 legacy adapter 测试全部通过；fake provider 测试仍保留为显式 fixture。B4 之后的 CommandStack apply、审批逐动作接线、重启恢复、真实 provider 凭据/素材压力和商业端到端验收仍按上文顺序继续推进，当前不能据此宣称整份计划已完成。

## 11. 本次执行记录（2026-08-10，第二批 B4–B5）

- **B4 / A-006、A-007（首个安全动作闭环）**：新增 `WorkflowActionService`。Workflow 产生的 `EditPlan` 只登记为 artifact/reference，不在 provider/adapter 内写时间线；apply 前重新读取当前工程并生成 Preview，审批 grant 严格绑定 project、timeline、base revision、plan hash 和 diff hash，缺 grant、scope 不匹配或 revision 过期均 fail closed。成功写入只调用宿主 `commit_ai_command`，重复 apply 返回 `already_applied`，不重复提交。
- **B5 / A-009、A-012（首批恢复接线）**：`WorkflowRunController` 暴露 `preview_current_plan`、`apply_current_plan`、`retry`、`resume`、`diagnostics`；Run Drawer 的 retry/diagnostics signal 已接入 controller。retry 会重新读取并编译当前上下文，不直接回放旧输入；plan observer 将 EditPlan 与统一 run/artifact 关联。
- **验证**：新增 Workflow action boundary 的审批、幂等、stale revision、缺审批故障测试；与 legacy adapter 回归共同通过。整个 AI Workflow 测试矩阵继续保持通过。

## 12. 本次执行记录（2026-08-10，第三批 B6 首段）

- **A-018（Context Bar）**：WorkflowPanel 增加工程、时间线、Brief revision、base revision、素材数、provider、权限和装配状态栏；内容只来自 controller 注入的公开 context snapshot，不在 UI 猜测当前工程。
- **A-019（计划/证据/审批工作区）**：增加 Plan、Diff、Evidence、Approval、Release Gate 五个只读标签页。编译预览只展示结构化节点、scope、风险、成本和 hash；真实 preview 生成后才能请求宿主审批，审批 grant 不直接显示在计划文本中。
- **A-011、A-012（首段控制台接线）**：Preview、Request Approval、Apply Approved Plan、Diagnostics 均接入 `WorkflowRunController` 与 `WorkflowActionService`；宿主负责弹审批并签发 grant，面板只转发意图，不直接写时间线。
- **验证**：新增 Context Bar/Plan workspace 与宿主审批 grant 测试；动作边界、WorkflowPanel、WorkflowRunController、dock wiring 定向测试共 46 项通过。Run History、run/node/job/artifact 分层和发布质量门的真实结果仍按后续 B6/B8 批次推进。

## 13. 本次执行记录（2026-08-10，第四批 B6 分层运行视图）

- **A-011**：`RunDrawerItem` 增加 run/node/job/artifact/history 层级、父子关系、断点摘要和恢复摘要；Workflow controller 从真实 `RunRecord.events/jobs/artifacts` 聚合当前运行，历史列表来自 runtime 快照并限制最近 8 条，不读取原始 prompt、路径或 secret。
- **A-012 / A-013 首段**：等待用户状态显示恢复入口并连接 `WorkflowRunController.resume`；失败/取消历史保留重试与诊断能力，当前运行仍支持取消、重试和诊断。等待审批、恢复、终态快照等 checkpoint 信息由事件流推导，不伪造持久 checkpoint。
- **稳定性**：`wait_for_run` 仅派发 controller 自身的 Qt Core 事件，避免全量 GUI 事件泵送触碰已延迟销毁的 dock 控件。
- **验证**：新增分层运行、历史快照和 Drawer 恢复元数据测试；Composer、WorkflowPanel、WorkflowRunController、P9 acceptance、dock wiring 定向测试共 86 项通过。跨进程持久 session 恢复和 host-owned rollback 仍留待后续 B5/B8 批次。

## 14. 本次执行记录（2026-08-10，第五批 B6 artifact/verify 可见性）

- **A-020（artifact 联动首段）**：Run Drawer 的 artifact 子项提供查看入口；controller 只展示 `artifact_id/type/producer_node_id/summary/run_id` 等脱敏 typed reference，并在宿主实现可选 `locate_artifact` 时转发定位，引用失效时显示安全状态而不崩溃。
- **Verify/Release Gate 可见性**：面板 Release Gate 现在展示真实 RunRecord 的执行状态、诊断数、artifact 数，以及“宿主交付质量门未执行”这一明确阻断项；Workflow 成功不再被误显示为导出或平台验收成功。
- **诊断工作区**：artifact 详情进入 Evidence tab，诊断仍进入 Approval/诊断 tab；preview、apply、artifact 查看和交付质量门状态彼此区分。
- **验证**：新增 artifact 安全引用、artifact tab、release gate 状态测试；完整 Workflow/UI 测试 461 项通过，Harness 选择集 95 项通过。真实导出质量门、平台规格和时间线 clip 定位仍需宿主端后续实测。

## 15. 本次执行记录（2026-08-10，第六批 B5 安全恢复闭环）

- **A-009 / A-021（人工接管）**：HarnessRuntime 增加显式 `handoff` 入口。它只设置协作取消 token、记录 `run.handoff_requested` 审计事件并等待 provider 自然收敛，不伪造暂停结果、不强杀线程；Run Drawer 显示“人工接管”操作及收敛状态。
- **A-012（回滚）**：WorkflowActionService 增加 host-owned rollback。回滚前必须同时匹配本次 EditPlan 的 undo label 和应用后 revision；宿主不能证明最近一条命令属于本次 AI 操作时返回 `ROLLBACK_NOT_SAFE`，不修改时间线。
- **面板接线**：Run Drawer 的人工接管、回滚按钮只在当前状态和真实 handler 可用时出现；controller、Dock wiring、宿主 bridge 均已贯通，回滚失败进入结构化诊断。
- **验证**：新增人工接管事件、终态拒绝、宿主回滚保护及无回滚能力 fail-closed 测试；真实导出质量门、跨进程重启恢复和 provider 压力矩阵仍待后续批次。

## 16. 本次执行记录（2026-08-10，第七批 A-022 运营指标基线）

- **运行指标聚合**：新增纯 Python `RunMetricsSnapshot`，从 `RunRecord` 快照计算运行总数、活动/终态、成功率、失败/取消、重试率、恢复成功率、审批等待、人工接管、诊断码、质量门失败、成本和平均/P95 延迟。
- **隐私边界**：指标只保留截断后的稳定诊断 code，不包含 prompt、诊断 message、路径、provider payload 或 secret；runtime 提供统一 `get_metrics()`，UI 不读取 provider 私有状态。
- **Workflow 面板**：新增 Metrics 工作区，展示上述 runtime-derived 指标；指标刷新失败不会影响运行状态或 Run Drawer。
- **验证**：新增空指标、恢复/接管/成本/延迟及脱敏测试；provider 真实授权、导出质量门和商业任务矩阵仍需后续 B7/B8 实测。

## 17. 本次执行记录（2026-08-10，第八批 B7 provider 可靠性）

- **幂等提交**：ComfyUI executor 以脱敏 request fingerprint 绑定 `request_id`；重复提交复用原外部 job，不重复创建 provider 任务；同一 ID 对应不同 payload 时返回 `IDEMPOTENCY_CONFLICT`。
- **超时与取消**：新增终态轮询；超过配置时限会先向 provider 传播取消，再返回 `TIMEOUT`，不把未完成 job 误报为成功。
- **授权与预算**：绑定 provider 与 consent provider 不一致时阻断；读取 provider 报告成本并执行硬预算 gate；候选数量遵守 `max_candidates`，失败结果不产生候选。
- **验证**：新增幂等冲突、超时取消、provider consent mismatch、成本超限和预算边界测试；真实云端网络、跨 provider license/safety 和商业素材矩阵仍待 B8 实测。

## 18. 本次执行记录（2026-08-10，第九批 B7 生成候选安全边界）

- **Provider 输出校验**：`GenerationService` 在候选进入 `GenerationBin` 前校验 request identity、provider provenance、model/version/prompt hash、候选数量、重复 asset ID 和非负实际成本；异常 metadata 进入结构化 schema 诊断，不污染素材箱。
- **素材导入审计**：`GenerationBin` 的 `ImportEvent` 记录 UTC 导入时间、批准者和导入时许可状态；安全阻断、重复导入和 discarded 状态继续 fail closed。
- **验证**：新增异常 provider 输出、重复候选、provider provenance 错配、超出候选预算和导入时间审计测试；真实安全模型、license provider 和商业素材矩阵仍待 B8 实测。

## 19. 本次执行记录（2026-08-10，第十批 B8 商业许可与来源审计）

- **许可证据结构化**：`GenerationProvenance` 增加许可证据引用、适用区域和核验时间；只保存引用/摘要，不保存合同原文、token 或绝对路径。
- **商业审计摘要**：`GenerationBin.provenance_report()` 现在输出导入审计、批准者、provider/model 身份、输入提示词摘要、许可证据、区域、许可证核验时间和安全状态的逐项计数，并在 entry 级别标注 `audit_complete`。
- **商用导出 fail closed**：商业就绪导出除了检查许可状态和安全状态，还必须通过完整来源审计；可通过 `ExportRequest.required_license_region` 约束目标区域。缺失证据、批准或身份信息时阻断，不把普通 `commercial_ready` 布尔值当作完整授权证明。
- **兼容边界**：旧的注入式 report 若未提供新审计字段，继续执行原有许可检查；真实 `GenerationBin` report 则启用完整审计门。
- **验证**：商业/生成/导出定向回归 `77 passed`；真实安全模型、跨 provider license 条款、区域法律矩阵和商业任务端到端实测仍待后续 B8 实机验收。

## 20. 本次执行记录（2026-08-10，第十一批 B8 发布证据闭环）

- **ReleaseManifest 三重证据**：发布清单现在同时要求质量门通过、测试报告 ID、完整测试矩阵通过、商业审计通过和商业审计报告 ID；`quality_gate_passed=True` 不再单独代表可发布。
- **稳定版安全边界**：Stable 除上述证据外仍必须声明明确的回滚版本；缺失项通过脱敏的 `release_blockers` 暴露，便于 UI/CI 解释阻断原因。
- **验证**：补充 Beta/Stable/缺失证据回归；后续仍需把真实 provider、真实素材矩阵和商业审计报告生成器接入发布流水线，不能用手工布尔值替代实测证据。

## 21. 本次执行记录（2026-08-10，第十二批 B8 评测发布 gate 统一）

- **评测 gate 对齐**：`ReleaseGatePolicy` 默认要求完整 dataset/performance/fault/security 矩阵，不再允许离线评测仅凭 case 全绿和质量门通过直接晋级。
- **商业审计绑定**：`EvalTrace` 记录商业审计通过状态与脱敏报告 ID；发布评测缺少审计结果或报告 ID 时 fail closed，并返回稳定阻断原因。
- **边界**：普通 trace 仍可用于调试和回归分析；只有同时满足 case、质量门、风险矩阵和商业审计，才可作为 Beta/Stable 晋级证据。真实 provider 和真实素材矩阵仍需后续实机运行产生证据。
- **验证**：评测 runner/trace 定向回归 `28 passed`。

## 22. 本次执行记录（2026-08-10，第十三批 B8 真实矩阵证据边界）

- **矩阵 provenance**：`MatrixCase` 增加 `execution_mode`、证据引用、provider、任务类型、平台和 codec；`MatrixResult` 汇总缺少真实证据的 case。
- **真实证据 gate**：synthetic/mock case 仍可用于本地诊断，但默认 release gate 要求每个矩阵 case 都有真实执行模式、provider 身份和脱敏证据引用；缺失时明确阻断。
- **验证**：矩阵/评测定向回归 `33 passed`；当前代码只建立证据合同，真实 provider、三类商业任务、两类平台和两种 codec 的实测报告仍需后续运行产生。

## 23. 本次执行记录（2026-08-10，第十四批 B8 商业矩阵覆盖 gate）

- **覆盖要求固化**：默认发布矩阵要求 `product_ad`、`interview`、`subtitle_multilingual` 三类任务，`web`、`mobile` 两类平台，以及 `h264`、`h265` 两种 codec。
- **可解释缺口**：`MatrixResult` 与 `ReleaseGateResult` 输出缺失维度（例如 `platform=mobile`），单一任务/平台/codec 的重复实测不能冒充完整商业矩阵。
- **兼容边界**：不带覆盖要求的矩阵仍可用于单元/本地诊断；Release gate 使用默认商业覆盖策略时 fail closed。
- **验证**：矩阵与评测定向回归 `12 passed`；仍需后续用真实 provider 和真实素材生成各维度的实测证据。

## 24. 本次执行记录（2026-08-10，第十五批 B8 真实媒体证据工厂）

- **真实 probe 接线**：新增 `build_real_matrix_case_from_probe()`，把 `FfprobeMediaProbe.probe_and_evaluate()` 的文件存在、可重开、hash、codec、规格和质量门结果转换为 `MatrixCase`。
- **防伪边界**：成功 case 的 `evidence_ref` 只保留 `sha256` 摘要；质量门失败仍标记为真实执行但 `passed=False`；探测失败或缺 hash 不会生成可发布的真实证据。
- **隐私边界**：矩阵 case 不保存绝对路径、原始媒体内容或临时 URL；错误信息经过脱敏和长度限制。
- **验证**：probe/矩阵/评测定向回归 `26 passed`；后续仍需在授权环境对真实 provider、三类商业任务、两类平台和两种 codec 运行并保存实测报告。

## 25. 本次执行记录（2026-08-10，第十六批 B8 真实媒体矩阵 runner）

- **批量执行**：新增 `RealMediaMatrixSpec` 与 `run_real_media_matrix()`，逐项调用有界 `ffprobe`，汇总真实 case、质量门、hash、codec、provider 和覆盖缺口。
- **失败隔离**：单个文件不存在、探测失败、缺 hash、codec 不匹配或质量门失败时，保留该 case 的诊断码和脱敏状态，整体矩阵不会伪造成功。
- **发布用途**：批量结果可直接作为 release gate 的矩阵输入；真实路径只存在于运行时 spec，不进入 case、manifest 或报告。
- **验证**：真实媒体矩阵定向回归 `29 passed`；后续仍需授权环境中的真实 provider/素材执行来产生最终商业证据。

## 26. 本次执行记录（2026-08-10，第十七批 B5 跨进程运行时恢复）

- **运行时快照落盘**：`HarnessRuntime.export_snapshot()` 现在通过 `ai/runtime.json` 接入 `.topos` 保存/加载链路，覆盖 run、event、job、artifact、invocation、detached session 和取消/诊断状态；落盘前统一脱敏，不保存 provider worker、绝对路径、原始 prompt 或 secret。
- **重启安全边界**：恢复终态 run 时保留历史；恢复 `RUNNING`、`WAITING_FOR_USER` 等活动 run 时统一转为 `FAILED/RUN_INTERRUPTED`，写入 `application_restart` 和 `recovery=explicit_retry_required` 诊断，不尝试复活跨进程孤儿 worker。用户必须在当前工程上下文和凭据重新建立后显式 retry/resume。
- **工程链路验收**：补充 `collect → .topos ZIP → load → restore` 测试，确认活动 run 会中断、终态历史保留、快照包含脱敏结构，malformed/duplicate/schema 不合法的条目会被逐项跳过并报告。
- **验证**：运行时、调用模型、AI 持久化与 payload IO 定向回归 `91 passed`。这批完成的是可验证的跨进程恢复基础设施，不等同于真实 provider 凭据、真实面板闭环、真实商业素材报告或长任务压力验收；这些仍必须在授权实机环境产生证据后才能关闭对应发布门。

## 27. 本次执行记录（2026-08-10，第十八批 B8 商业面板验收证据工厂）

- **验收报告入口**：新增 `CommercialAcceptanceSpec`、`CommercialAcceptanceObservation` 和 `CommercialAcceptanceReport`，要求每个商业用例报告分析、计划、时间线写入、导出、恢复五个用户阶段的证据引用。
- **真实凭据边界**：只有真实 provider 身份、凭据实际使用证明、每阶段脱敏证据引用和 `execution_mode=real` 才能成为真实证据；synthetic、developer API 或缺失凭据证明的结果只能生成阻断报告。
- **面板闭环边界**：报告要求入口为 `workflow_panel`、零非面板操作，并同时验证安全、license、区域、质量门、长任务、重启恢复和取消延迟预算。探针异常会被记录为失败用例，不会自动转绿。
- **发布门接线**：`EvalTrace` 可携带商业面板验收报告；`ReleaseGatePolicy(require_commercial_acceptance=True)` 会在报告缺失或未通过时 fail closed，且返回稳定报告 ID和阻断原因。
- **验证**：商业验收/评测 trace 定向回归 `34 passed`。本批只建立真实实机的证据采集与发布门，不伪造 provider 凭据、真实商业素材、平台 codec 报告或灰度运营数据；下一步仍需在授权环境执行探针并保存实际报告。

## 28. 本次执行记录（2026-08-10，第十九批 B8 压力与运营证据）

- **长任务压力证据**：商业验收 observation 增加长任务耗时、压力样本数和并发峰值；没有真实压力样本的用例不能通过 operational checks。
- **取消与重启证据**：取消延迟必须落在用例预算内，重启恢复仍要求显式通过；这些数据会与五阶段面板证据绑定到同一商业报告。
- **运营安全计数**：报告汇总重复写入、重复扣费、provider 失败、崩溃、版权事故、用户修改、满意度和成本，导出为现有 `RolloutMetrics`，不包含 prompt、路径、素材或 provider payload。
- **发布边界**：任何重复写入、重复扣费、崩溃或版权事故都会使该用例及商业报告失败；没有真实 pressure/latency 数据不会被静态布尔值掩盖。
- **验证**：AI 编辑全量回归 `1246 passed, 1 skipped`。本批仍未生成真实 provider/商业素材实机数据，下一步必须在授权环境执行压力探针并保存报告。

## 29. 本次执行记录（2026-08-10，第二十批 B8 发布交接强制验收）

- **ReleaseManifest 强制绑定**：Beta/Stable 发布清单新增 `commercial_acceptance_passed` 与 `commercial_acceptance_report_id`，商业面板验收缺失、失败或没有报告 ID 时直接进入 `release_blockers`。
- **防止证据旁路**：质量门、测试矩阵和来源审计全部通过，也不能替代真实 provider 凭据下的面板闭环验收；旧报告不能通过新增发布门。
- **验证**：发布清单、评测和商业验收定向回归 `17 passed`；AI 编辑全量回归 `1247 passed, 1 skipped`。真实 provider/素材报告仍需授权实机生成后才能填写这些字段并放行。
