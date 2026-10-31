# Topos AI Assistant-first Agent Harness 与 AI Workflow：统一落地实施计划（2026-08-02）

> 状态：根据 2026-08-02 的代码审计重新排序；本文件是 AI Assistant、Skill、后台任务图和 AI Workflow 的**唯一实施顺序**。
> 核心目标：先让普通用户通过对话可靠完成任务，再让高级用户以节点图编排同一任务，最后让开发者把经过验证的图发布为 Skill。绝不为三类用户复制三套执行器、权限或任务系统。

---

## 0. 本次整体审计：已有地基、缺失闭环与最高优先级

### 0.1 代码审计结论（不是假设）

以下结论来自当前实现，而不是旧计划中的“已完成”标记。已有模块是可靠地基，但尚未自动构成完整的 Agent 产品。

| 能力 | 已有实现证据 | 当前缺口 | 优先级 |
| --- | --- | --- | --- |
| 对话面板与编辑提议 | `AiEditAssistantPanel`、`AiEditController`、`AssistantService` | 面板能聊天、看 Brief/方案/上下文，但没有新对话、历史会话、会话切换、任务队列、工具活动流或结果资产卡 | P0 |
| 模型 Profile / 数据策略 | `provider_profiles.py`、`ProfileRegistry`、`CredentialStore` | Profile 可保存且不泄露 secret；AI 面板只显示 profile，不能在底部选择模型、访问等级、上传范围、预算或本次覆盖参数 | P0 |
| 上下文 | `context_service.py`、`context_bundle.py`、上下文检查器 | 可查看 token/块；缺少常驻的“当前/上限/压缩状态”、手动压缩、压缩前后差异和每次运行的固定 snapshot | P0 |
| 普通文本请求 | `submit_request → Brief → VariantPlanner` | Composer 目前主要走 Brief/确定性 VariantPlanner；不会按用户意图发现或调度 Skill、帮助检索、分析、字幕、TTS、生成任务 | P0 |
| 内置工作流 / Skill | `workflow.py`、`skill_manifest.py`、`skill_registry.py`、`skill_runtime.py` | `run_workflow()` 与 `run_skill()` 是两个平行入口；未被聊天路由统一调用；默认角色仍是确定性 stub，缺少 Skill 卡片、输入表单、发布/回归评测流程 | P0/P1 |
| 子智能体 / TaskGraph | `task_graph.py`、`SingleWriterPolicy` | 有 DAG、预算和产物契约；缺真实模型角色、运行进度事件、父子任务可视化、恢复点和统一 Job 映射 | P1 |
| 长任务与取消 | `jobs.py`、分析 scheduler、控制器的 `_PlanWorker` | `AssistantStateSnapshot.jobs` 当前固定为空；取消主要改变 UI 状态，尚未贯穿 planner/provider/job 的 cancel token | P0 |
| 媒体分析证据 | AnalysisService / providers / `AdaptiveTranscriptProvider` | 已具备模型与服务基础；需要在 Agent 中提供“选中/本 Bin/全部素材”的明确范围确认、后台任务图和 evidence readiness 反馈 | P1 |
| 字幕 / TTS /生成素材箱 | 转录、TTS provider、`GenerationBin`、generation contracts | 功能链有基础但不在一个统一任务运行记录内；生成媒体、导入媒体池、放置提议、审批的 provenance 仍需统一呈现 | P1 |
| 帮助助手 | `HelpIndex` | 有可信本地帮助索引，尚未接入聊天路由、引用卡片和“带我操作”步骤 | P1 |
| MCP | `mcp_gateway.py` | 有安全的 in-process JSON-RPC/ToolFacade 适配；没有用户可管理的 server/profile 页面、真实 stdio transport、连接状态和授权审计 UI | P2 |
| 会话持久化 | `assistant_session_store.py` | 只持久化 Brief/Variant/Decision 摘要；不保存会话列表、消息摘要、任务/产物引用、压缩 checkpoint 和恢复策略 | P0 |
| AI Workflow | 当前仅有计划，尚无 `src/features/ai_workflow/` | 不能先建第二个 WorkflowExecutor；必须以后文的统一 `HarnessRuntime` 为运行时 | P2 |

### 0.2 第一原则：三种入口，只有一个 HarnessRuntime

完成本计划后，产品的真实调用链必须固定为下面这一条。任何新功能若不能接入此链，不能直接合并。

```text
普通用户：对话 / 快捷动作 / 帮助问题
                  │
高级用户：模板 / AI Workflow 节点图
                  ├──> Invocation API（同一输入、权限、预算、项目范围）
开发者：图定义 / 调试 / eval / 发布 Skill
                  │
                  ▼
       SkillRegistry + WorkflowLibrary + SkillCompiler
                  │      （Workflow 只是 Skill 的可视化/参数化定义）
                  ▼
       HarnessRuntime → TaskGraph → JobScheduler / ToolFacade / Providers
                  │                    │
                  │                    └── Analysis、ASR、TTS、LLM、MCP、生成、Composite
                  ▼
      Artifact & Evidence Store → Preview/Diff → ApprovalService → CommandStack
```

必须遵守的映射：

| 用户角色 | 入口行为 | 进入 Harness 的形态 | 产物 |
| --- | --- | --- | --- |
| 普通用户 | “分析所有素材”“给字幕配音”“帮我剪成 30 秒竖版” | `Invocation(source="chat", skill_id, inputs, policy)` | 对话回复、任务卡、artifact、可审批 plan |
| 高级用户 | 拖入节点、调整参数、运行模板 | `Invocation(source="workflow", skill_id/workflow_id, inputs, policy)` | **同一种** run、job、artifact、plan |
| 开发者 | 编写/调试图、填 eval、发布 | `SkillDefinition + graph_template + eval suite` | 可版本化、可审计的 Skill |

禁止出现：`ChatExecutor`、`WorkflowExecutor`、`SkillExecutor` 三套各自调 provider 的实现；三套各自的 Job、缓存、审批 token 或 timeline 写入路径；节点图直接调用 `TimelineSequence` 可变对象。

### 0.3 P0 必须先补齐的普通用户体验

P0 通过前，禁止开始可编辑 AI 节点画布。先把现有 AI Assistant 从“方案生成面板”升级成可操作、可观察的 Agent 控制台：

1. **底部控制栏**：项目范围、权限等级、Profile/模型、上下文 `已用/上限`、压缩按钮、成本/网络提示、新对话、历史对话。设置只作用于后续 invocation，绝不改写正在运行的 job。
2. **会话栏**：新建、重命名、搜索、归档、恢复；工程内只保存脱敏消息摘要和 artifact/job 引用，secret、绝对路径和未经许可的完整 prompt 不落盘。
3. **运行活动流**：每一次工具调用、子任务、下载、分析、生成、审批都显示状态、耗时、输入范围、输出、取消/重试/查看诊断；用户不能只能看到“规划中”。
4. **意图路由**：用户输入先匹配明确快捷意图/Skill，再由模型补齐参数；无法确定时只问一个最小问题，不能把所有内容一律送去 VariantPlanner。
5. **帮助模式**：`HelpIndex` 先检索本地可信帮助，再生成回答；回答必须显示功能是否可用、入口位置、快捷键/步骤与引用 ID。
6. **分析范围确认**：默认只分析当前选择；“分析所有素材”必须显示素材数量、预计模型/磁盘/耗时，并把用户确认写进 invocation。媒体池继续保留分析开关，绝不默认扫全库。

### 0.4 统一领域对象（所有后续代码必须复用）

| 对象 | 唯一职责 | 现有代码的演进方向 |
| --- | --- | --- |
| `Invocation` | 记录一次用户/图/开发者发起的请求、范围、profile、policy、输入和父会话 | 新增，取代 chat/workflow 各自的散参数 |
| `SkillDefinition` | 可版本化的能力定义：输入 schema、风险、工具策略、图模板、UI card、eval | 由 `SkillManifest` 扩展；不再把 WorkflowSpec 作为另一套能力定义 |
| `TaskGraph` | 唯一的可执行后台任务图；节点可为 provider、工具、子智能体、控制/审批步骤 | 扩展现有 `task_graph.py`，不再新建平行可执行器 |
| `WorkflowDefinition` | `TaskGraph`/Skill 的可视化布局、暴露参数和模板元数据 | AI Workflow 只保存/编辑它，运行时编译为同一 `Invocation` |
| `RunRecord` | 统一保存状态、节点事件、Job、成本、缓存、证据、artifact、诊断 | `jobs.py` + Artifact/Audit 的聚合视图；Assistant snapshot 不再固定空 jobs |
| `ArtifactRef` / `EvidenceRef` | 小型、可验证、可追溯输出引用 | 复用现有 artifact/analysis 模型，不在聊天或节点间传文件路径/Qt 对象 |
| `ApprovalGrant` / `EditPlan` | 唯一写入时间线的门禁 | 复用 `ApprovalService`、preview、`CommandStack`；不能新增 bypass |

---

## 1. 节点画布的复用边界：可以复用什么，不能复用什么

结论是 **“复用合成页的交互与视觉基础设施；新建 AI Workflow 的领域模型、编译器/适配器和数据契约”**。不要把 AI 节点硬塞进 Composite 的 `NodeGraph`，也不要新建平行执行器。

现有 Composite 的 `NodeGraph` 和 `Node` 以 GPU 图像管线为核心：端口传递的是 `TextureResult`，求值器假定帧、纹理、render context、dirty GPU cache 和 render thread。这很适合合成，不能安全表达耗时数分钟的 ASR、LLM、TTS、模型下载、MCP 或审批任务。

| 分类 | 现有位置 | AI Workflow 的处理方式 | 决策 |
| --- | --- | --- | --- |
| 画布主题、网格、缩放、平移、框选、拖拽、连线交互 | `src/features/composite/ui/node_editor_widget.py` | 提取为无领域依赖的 `NodeCanvasBase`，AI 页继承/组合 | **复用，先抽象** |
| 节点外观、端口、连线、选中态、错误态、缩略图位置 | `node_item.py`、`port_item.py`、`connection_item.py` | 复用设计 token 和绘制逻辑，改为读取工作流节点 schema | **复用样式 + 适配** |
| 搜索弹窗、自动布局、Backdrop、Sticky Note、复制粘贴 | `node_search_popup.py`、`graph_layout.py`、相关 UI command | 抽出通用接口；工作流有自己的命令对象和序列化 | **复用交互模式** |
| Composite `NodeGraph`、`Node`、`Port`、`TextureResult`、`GraphEvaluatorMixin`、GPU LRU cache | `core/node_engine.py`、`nodes/base_node.py`、`evaluators/` | 不直接调用；创建 `WorkflowGraph`、`WorkflowNodeSpec`、`WorkflowCompiler`，运行委托 HarnessRuntime | **不能直接复用** |
| 普通合成节点（Blur、Keyer、Merge、Topos Color 等） | `src/features/composite/nodes/` | 通过一个显式的 **Composite Subgraph** 节点运行已保存合成图，不把每个合成节点平铺到 AI 图上 | **兼容，但隔离** |
| AI 分析/转录/路由/模型包 | `src/features/edit/ai_editing/` | 包装为 Workflow task adapter；保持 provider/service 为唯一实现 | **直接接入** |
| 时间线写入 | `EditPlan`、`ApprovalService`、`CommandStack` | 只允许 `EditPlan → Preview/Diff → Approval → CommandStack` | **必须复用** |

### 0.1 兼容“常规节点”的产品定义

首版提供三层兼容，避免承诺“任意节点互相直连”造成技术债：

1. **视觉兼容（M1）**：AI 工作流与合成页使用同一套节点视觉语言、快捷键、搜索、分组、注释、布局和复制体验。
2. **资产兼容（M5）**：AI 节点输出 `MediaAssetRef` / `SelectionSet`；它们可以传给“创建 Composite Graph / 应用已保存 Composite Graph”节点。
3. **子图兼容（M8）**：一个 `Composite Subgraph` 节点引用已有 `CompositeGraphData`，输入为已批准的媒体或时间线片段，输出为 render job / 生成媒体 artifact。它不共享 GPU texture，也不允许 AI workflow 直接修改 Composite 内部节点。

**明确不做**：把 ASR 文本端口与合成页的 `TextureResult` 端口直接相连；让 AI Workflow 的运行线程持有 OpenGL context；让 Composite 节点绕开渲染器直接写时间线。

### 0.2 ComfyUI 兼容结论：分层兼容，而非虚假的“任意 JSON 无损转换”

Topos 应当兼容 ComfyUI 的模型生态、工作流文件和本地/云执行能力，但不能承诺把任意安装了社区 custom nodes 的 ComfyUI JSON 自动变成一张完全等价的 Topos 原生节点图。原因是 ComfyUI workflow 的节点类型、输入 schema、模型目录和第三方 custom node 由运行服务器决定，缺少相同节点/版本/模型时没有可验证的等价转换。

ComfyUI 官方已有版本化的 [Workflow JSON schema](https://docs.comfy.org/specs/workflow_json)，其 API 格式以节点 ID、`class_type` 与 `inputs` 描述图；提交后以异步 job 方式返回结果。Topos 应以这个稳定边界做 adapter，而不是依赖 ComfyUI 前端的内部对象。

| 兼容等级 | 用户得到的能力 | 实现方式 | 是否承诺 |
| --- | --- | --- | --- |
| C0 资产级 | Topos 可把图片、视频、mask、音频作为生成参考；结果自动进入生成素材箱 | 复用 `GenerationRequest` / `GeneratedAsset` | 首版必须 |
| C1 Provider 级 | 用同一工作流节点选择本地 ComfyUI、Comfy Cloud 或 Topos 原生 provider | `GenerationProvider` + profile adapter | 首版必须 |
| C2 工作流执行级 | 导入/保存 ComfyUI API-format JSON，绑定 prompt、seed、参考素材后提交、监听、下载结果 | `ComfyWorkflowExecutor`（外部子图节点） | M9 必须 |
| C3 可视化级 | 在 Topos 中查看外部图摘要、节点清单、缺失依赖、参数绑定和运行状态 | 只读 subgraph inspector，不运行第三方代码 | M9 必须 |
| C4 核心节点转换 | 将少数官方稳定节点映射为 Topos 原生高层节点，例如 Text-to-Image、Image-to-Video、Upscale | 显式 converter + golden workflow tests | M10 选择性 |
| C5 任意 custom node 原生转换 | 将任何第三方节点无损转成 Topos 节点 | 不可保证；缺失时保留 opaque 子图/占位节点 | **不承诺** |

这套定义让用户能实际使用 ComfyUI 的模型和工作流，同时不牺牲 Topos 的安全、审批、可追溯和工程稳定性。

---

## 2. 产品边界与核心不变量

### 1.1 用户能完成的事情

- 从媒体池中选素材，按需分析音频、镜头、OCR、节拍、响度和语义标签。
- 搭建“转录 → 字幕 → 翻译 → TTS → 音频轨道”的本地化配音流程。
- 搭建“镜头切分 → 人物/产品/口播筛选 → LLM 剪辑大纲 → 粗剪方案 → 用户确认”的 AI 粗剪流程。
- 搭建“脚本 → 视频/图片/音乐生成 → 生成素材箱 → 人工挑选 → 插入 timeline”的创作流程。
- 以官方模板为起点，保存、导出、复用和版本化自己的工作流。
- 在运行记录中查看模型、版本、输入素材范围、成本、许可证、缓存命中、错误和时间线差异。

### 1.2 四条不可突破的红线

1. **Workflow 节点不直接改 timeline。** 任何编辑节点先产出 `EditPlan` 或 `CommandBatchProposal`；只有 `Apply Approved Plan` 节点在审批有效且 revision 一致时提交。
2. **长任务不跑在 UI / render 线程。** ASR、TTS、Demucs、下载、LLM、视频生成和 Composite render 都是可取消 Job，由资源 lane 调度。
3. **外部输入不可信。** OCR、字幕、文件名、网页、MCP 输出和 Skill manifest 均只能作为数据，不能修改权限、系统指令或执行策略。
4. **工作流可复现、可解释。** 保存节点版本、模型/参数 hash、输入 artifact、输出 artifact、timeline revision、成本、许可证和审批记录；不保存 API key、绝对路径或可重建大缓存。

### 1.3 面向普通用户的两种模式

| 模式 | 面向对象 | UI 行为 |
| --- | --- | --- |
| 模板模式（默认） | 新手、普通剪辑用户 | 选择“自动字幕”“口播精剪”“多语言配音”等模板，只填写少量参数；画布可展开查看。 |
| 专业模式 | 高级用户、工作室、插件作者 | 完整节点库、节点搜索、子图、条件分支、缓存策略、运行调试、导入/导出 workflow JSON。 |

不要让用户第一次进入就面对空画布；空画布只属于“新建高级工作流”。

---

## 3. 高级节点图的目标架构（作为 Harness 的可视化入口）

```text
AI Assistant UI ──┐
AI Workflow UI ───┼──> InvocationController (Qt 只发 typed event)
Developer Studio ─┘
                            │
              SkillRegistry / WorkflowLibrary / Compiler
                            │
                    HarnessRuntime（唯一运行时）
     ┌──────────────────────┼────────────────────────────┐
     │                      │                            │
TaskGraphExecutor      RunRecord / ArtifactStore    Policy / Approval bridge
     │                      │                            │
JobScheduler / ToolFacade / Provider adapters       Preview → CommandStack
     │
Analysis / ASR / TTS / LLM / MCP / Generation / Composite render
```

`WorkflowController` 只能负责图的编辑、校验和把 `WorkflowDefinition` 编译为
`Invocation`；它**不能**再维护一套 `WorkflowExecutor`、Job registry、缓存或审批逻辑。
原计划中名为 `WorkflowExecutor` 的组件应改名为 `WorkflowCompiler` / `WorkflowRunAdapter`：
前者做 schema、DAG、端口和参数绑定校验，后者只调用 `HarnessRuntime.start(invocation)` 并订阅
同一份 `RunRecord`。

### 2.1 新模块目录

```text
src/features/ai_harness/
  invocation_models.py              # Invocation、RunRecord、RunEvent、RunPolicy
  runtime.py                        # HarnessRuntime：唯一 start/cancel/retry/resume 入口
  invocation_router.py              # chat intent → Skill + typed inputs（不执行 provider）
  skill_compiler.py                 # SkillDefinition / WorkflowDefinition → TaskGraph
  run_store.py                      # session/run/artifact/job/checkpoint 聚合查询
  run_event_bus.py                  # UI 可订阅事件（Qt 无关）
  scheduler_adapter.py              # TaskGraph ↔ AnalysisJobScheduler/JobRegistry
  policy_resolver.py                # profile、scope、权限、预算、网络预检
  tests/

src/features/ai_workflow/
  module.py                         # FeatureModule 注册，feature_id=ai_workflow
  runtime.py                        # 页面/控制器组合根
  core/
    workflow_models.py              # WorkflowGraph、NodeInstance、Edge、PortRef
    node_specs.py                   # WorkflowNodeSpec、参数/端口 schema、registry
    type_system.py                  # ArtifactType、兼容性、结构化 schema validator
    graph_validation.py             # DAG、端口、必填输入、cycle、版本迁移校验
    workflow_repository.py          # 项目内 workflow 定义、模板/版本/迁移
    workflow_compiler.py            # 图 → Invocation/TaskGraph 参数绑定；不执行任务
    workflow_artifacts.py           # 只读 ArtifactRef/UI 适配，不建第二个 store
    workflow_policy.py              # Workflow policy → RunPolicy 的受限映射
    workflow_migration.py           # schema_version 迁移、未知节点占位
  adapters/
    node_to_task.py                  # Node spec → TaskGraph node / existing service invocation
    media_nodes.py                   # 素材/选区/代理/导入
    analysis_nodes.py                # ASR、Demucs、OCR、scene cut、loudness
    editorial_nodes.py               # Brief、LLM、粗剪 plan、QA
    generation_nodes.py              # TTS/视频/音乐/图片生成
    timeline_nodes.py                # preview/apply/字幕/轨道提议
    control_nodes.py                 # branch/join/retry/user approval/subworkflow
    composite_adapter.py             # Composite Subgraph / render adapter
    comfyui_adapter.py                # ComfyUI API/workflow import/export/execution adapter
    comfyui_schema.py                 # API-format validator、binding、node discovery cache
    mcp_adapter.py                   # allowlisted MCP task adapter
  ui/
    workflow_page.py
    workflow_canvas.py               # 基于 NodeCanvasBase 的 AI 专用 canvas
    workflow_node_item.py
    workflow_connection_item.py
    workflow_node_library.py
    workflow_inspector.py
    workflow_run_drawer.py
    workflow_template_gallery.py
    workflow_diff_preview.py
  templates/
    auto_caption.workflow.json
    subtitle_localization.workflow.json
    talking_head_rough_cut.workflow.json
    product_ad.workflow.json
  tests/ ...

src/shared/node_canvas/
  canvas_base.py                     # 从 composite 提取的通用画布交互
  visual_tokens.py                   # 节点/端口/连线/状态颜色
  graph_ui_protocol.py               # UI 可依赖的最小 graph/node/port 协议
```

### 2.2 与既有模块的依赖方向

```text
ai_workflow.ui → ai_workflow.core → ai_workflow.executors
                                  → edit.ai_editing services
                                  → composite public facade/runtime
                                  → edit CommandStack bridge

禁止：composite.core → ai_workflow；ai_workflow.executor → Qt widget；
      executor → TimelineSequence 直接 mutable 修改。
```

---

## 4. 数据模型与端口类型

### 3.1 不使用 `Any` 作为端口协议

节点之间传递的是小型、可序列化、可检查的 artifact 引用，真实大文件留在媒体池/缓存目录。端口不得传 Qt 对象、OpenGL texture、模型实例、线程对象、API key 或绝对路径。

首批 `ArtifactType`：

| 类型 | 含义 | 典型来源 | 典型去向 |
| --- | --- | --- | --- |
| `asset_ref` | 单个媒体池素材 ID + fingerprint | 素材输入 | 分析、Composite 子图、生成参考 |
| `asset_set` | 有序素材集合 + 选择条件 | 媒体池查询、素材检索 | 批量分析、粗剪 |
| `timeline_ref` | timeline ID + revision + format | 当前时间线 | 计划、QA、交付 |
| `selection_set` | 时间线片段 / 镜头 / transcript 区间集合 | 镜头筛选、语义搜索 | 粗剪、导出、Composite |
| `analysis_ref` | `AssetAnalysis` / evidence 的版本化引用 | ASR、OCR、scene cut | 字幕、LLM、筛选 |
| `transcript_ref` | 带词级时间戳/说话人的转录引用 | Qwen/WhisperX | 字幕、翻译、口播精剪 |
| `subtitle_track_ref` | 字幕轨道 ID + revision | 字幕生成 | TTS、翻译、导出 |
| `text_document` | 脚本、Brief、翻译、提示词 | 用户/LLM | TTS、视频生成、计划 |
| `edit_plan_ref` | 绑定 timeline revision 的编辑提议 | 规划节点 | Diff、Approval、Apply |
| `media_generation_ref` | 生成任务/结果素材引用 | TTS/音乐/视频 | 媒体池、timeline 提议 |
| `composite_graph_ref` | 已保存合成图 ID + schema/version | Composite 输入 | Composite Subgraph |
| `render_job_ref` | 合成/交付渲染 Job | Composite/Export | 导入、交付报告 |
| `report` | QA、成本、许可证、执行报告 | 任意节点 | 用户、条件分支 |
| `approval_token` | 对特定 plan/revision/hash 的一次性确认 | Approval 节点 | Apply 节点 |

### 3.2 WorkflowGraph 最小 schema

```json
{
  "schema": "topos.workflow/1",
  "workflow_id": "wf-auto-caption",
  "name": "自动字幕（本地优先）",
  "schema_version": 1,
  "nodes": [
    {
      "node_id": "input",
      "spec_id": "topos.media.selected_assets",
      "spec_version": "1.0.0",
      "params": {"media_kind": "video_or_audio"},
      "ui": {"x": 120, "y": 200}
    }
  ],
  "edges": [
    {"from": {"node_id": "input", "port": "assets"}, "to": {"node_id": "asr", "port": "assets"}}
  ],
  "ui": {"viewport": {"zoom": 1.0, "center": [0, 0]}, "notes": [], "groups": []},
  "policy": {"network": "ask", "timeline_write": "approval_required"}
}
```

### 3.3 节点 spec 必填字段

每个 node spec 至少声明：

- `spec_id`、语义版本、显示名、分类、图标、说明、作者/许可证。
- 输入/输出端口：artifact type、是否必填、是否允许多连接、是否流式。
- 参数 JSON schema、默认值、敏感字段标记、是否影响 cache key。
- 所需 capability、模型 pack、资源 lane、预估成本/显存、网络/写权限级别。
- `executor_id`、可取消性、retry policy、输出 provenance schema。
- 失败策略：`fail`、`skip`、`fallback`、`ask_user`，不能无提示吞异常。

---

## 5. 节点库：首版范围和后续范围

### 4.1 M4 必须实现的 16 个节点

| 分类 | spec_id | 输入 → 输出 | 执行边界 |
| --- | --- | --- | --- |
| 输入 | `topos.media.selected_assets` | — → `asset_set` | 只读当前选择 |
| 输入 | `topos.timeline.current` | — → `timeline_ref` | 只读 current timeline/revision |
| 分析 | `topos.media.extract_audio` | `asset_ref` → `media_generation_ref` | ffmpeg Job，不改工程 |
| 分析 | `topos.analysis.transcribe_auto` | `asset_set` → `transcript_ref`,`analysis_ref` | 复用 Qwen → Demucs → WhisperX 路由 |
| 分析 | `topos.analysis.scene_cut` | `asset_set` → `analysis_ref` | 复用 SceneCut provider |
| 分析 | `topos.analysis.ocr` | `asset_set` → `analysis_ref` | 复用 RapidOCR provider |
| 分析 | `topos.analysis.audio_features` | `asset_set` → `analysis_ref` | BPM / loudness / speech ranges |
| 编辑 | `topos.caption.propose_track` | `transcript_ref` → `edit_plan_ref`,`subtitle_track_ref` | 只生成 proposal |
| 文本 | `topos.text.translate` | `subtitle_track_ref`/`text_document` → `text_document` | local/cloud profile 明示 |
| 生成 | `topos.tts.qwen3` | `subtitle_track_ref`/`text_document` → `media_generation_ref` | Qwen3-TTS Job；输出先入生成素材箱 |
| 编辑 | `topos.timeline.propose_audio_placement` | `media_generation_ref`,`timeline_ref` → `edit_plan_ref` | 不直接插入音轨 |
| Agent | `topos.agent.rough_cut_plan` | `asset_set`,`analysis_ref`,`text_document` → `edit_plan_ref`,`report` | 复用 TaskGraph / WorkflowRunner |
| 审批 | `topos.timeline.preview_diff` | `edit_plan_ref`,`timeline_ref` → `report` | revision/hash 校验，显示 diff |
| 审批 | `topos.user.approval` | `edit_plan_ref`,`report` → `approval_token` | UI 明确确认；不允许自动批准 |
| 输出 | `topos.timeline.apply_approved_plan` | `edit_plan_ref`,`approval_token` → `timeline_ref`,`report` | CommandStack 唯一写入口 |
| 输出 | `topos.report.run_summary` | 任意 → `report` | 汇总 provenance、成本、失败 |

### 4.2 M6–M10 增量节点

- 素材检索、相似镜头、人物/产品筛选、静音/口癖检测、节拍对齐、自动 ducking。
- 文案脚本、分镜、不同平台改版、字幕阅读速度/安全区质检。
- 生成视频、生成音乐、生成图片、生成 B-roll、授权音色克隆（每个均有成本与版权确认）。
- `If / Switch / Merge / Join / Retry / Wait for User / For Each Asset / Subworkflow`。
- `MCP Tool` 节点（只允许启用的、schema 已验证的工具）；`Skill` 节点（封装已审核工作流）。
- `Composite Subgraph`、`Render Composite`、`Import Render Result`。
- `ComfyUI Workflow`、`ComfyUI Text-to-Image`、`ComfyUI Image-to-Image`、`ComfyUI Inpaint`、`ComfyUI Image-to-Video`、`ComfyUI Upscale/Interpolate`；前五者可以先是同一个外部子图节点的受控模板，稳定后才拆成原生高层节点。

### 4.3 图片/视频生成的能力模型

图片和视频生成不能只有一个“Prompt → 文件”的过小接口。Workflow 节点需要以 provider-neutral 的 `GenerationRequest` 为核心，同时把复杂控制项收进可验证的 `GenerationControls` schema，而不是任意 `dict`：

| 能力组 | 必填/常见参数 | 适用模型或后端 |
| --- | --- | --- |
| 文生图 | prompt、negative prompt、宽高、seed、batch、steps、guidance | Topos 内置 provider / ComfyUI diffusion workflow |
| 图生图 | 源图、denoise strength、prompt、seed | ComfyUI 或本地 image provider |
| 局部重绘 | 原图、mask、prompt、mask blur、padding | ComfyUI inpaint workflow |
| Control / Reference | control image、姿态/边缘/深度、权重、参考图 | ComfyUI ControlNet/IP-Adapter 类工作流 |
| LoRA / 风格 | 模型 ID、LoRA 列表、权重、触发词 | 后端已发现且用户授权的模型 |
| 文生视频 | prompt、时长、分辨率、fps、seed、首帧/尾帧 | 视频 provider / ComfyUI video workflow |
| 图生视频 | 参考图、motion strength、镜头运动、时长 | 视频 provider / ComfyUI video workflow |
| 视频参考/重绘 | 参考视频、截取范围、动作/风格约束 | 明确允许的本地/云 provider |
| 后处理 | 放大、补帧、去闪烁、抠像、无损转码 | ComfyUI workflow 或 Topos media service |

所有 `GenerationControls` 字段都必须：有 JSON Schema、单位/范围、是否影响 cache、是否会上传参考素材、是否需要模型 pack、是否会增加成本。未知 provider 字段只保存于外部 Comfy 子图的 `bound_inputs` 中，不混入 Topos 的核心数据模型。

### 4.4 禁止作为普通节点暴露的能力

- 任意 shell、Python `exec`、文件系统遍历、鼠标键盘自动化、裸 OpenGL context。
- “完全访问”“跳过审批”“忽略许可证”等全局开关。
- 未做 schema/diff 的 `Run arbitrary edit command`。

---

## 6. 合成页复用的详细改造步骤

### M1：先提取无领域依赖的节点画布基础

**目标**：AI Workflow 和 Composite 共享一套交互/风格，Composite 功能不回归。

1. 审计 `NodeEditorWidget` 对 `NodeGraph`、`NodeItem`、`TextureResult`、renderer 的直接依赖，列成 import dependency 表。
2. 在 `src/shared/node_canvas/graph_ui_protocol.py` 定义 UI 所需最小 Protocol：
   - `GraphViewModel.nodes()` / `edges()` / `generation()`；
   - `NodeViewModel.id/title/category/ports/ui_state`；
   - `PortViewModel.name/direction/data_type/status`；
   - 不包含 `evaluate()`、texture、Qt 或 CommandStack。
3. 提取 `NodeCanvasBase`：网格、缩放、平移、框选、节点选择、连线拖拽、连接高亮、复制粘贴坐标、backdrop、sticky note、viewport 持久化。
4. Composite 写 `CompositeGraphCanvasAdapter`，保持现有 `NodeEditorWidget` 对外 signal 和快捷键兼容。
5. AI Workflow 写 `WorkflowGraphCanvasAdapter`；工作流节点端口显示 artifact 类型和状态，不显示 GPU thumbnail。
6. 将颜色、圆角、端口大小、错误/运行/缓存/等待用户状态抽为 `visual_tokens.py`，禁止在两页复制硬编码色值。

**验收**：Composite 页的节点创建/连线/撤销/搜索/组/注释/预览相关测试全部通过；AI canvas 能用 fake graph 展示节点、连线、错误和运行态。

### M2：不要复用 Composite 的图执行器

`NodeGraph` 的 `evaluate()`、dirty propagation、`TextureResult` LRU cache 继续只服务渲染。AI Workflow 的 executor 另写，理由：

- AI 任务输出是 artifact 或 Job，不是单帧 image；成功可能在数秒到数小时后发生。
- AI 任务需要排队、取消、重试、资源 lane、费用、模型下载、网络/隐私审批和 checkpoint。
- AI 任务 cache key 要包含素材 fingerprint、模型版本、prompt/params、上游 artifact hash 和权限，不是纹理 revision。
- AI 工作流允许控制节点和等待用户输入，这不满足 Composite 每帧 DAG 求值假设。

**验收**：导入 AI Workflow 包时不得 import `composite.evaluators`、ModernGL 或任何 renderer context。

### M8：以“Composite Subgraph”兼容常规节点

该节点参数只保存 `composite_graph_id`、输入映射、render range、proxy/quality、输出导入策略。运行时调用 Composite 的公开 facade / render service，返回 `render_job_ref` 或导入后的 `asset_ref`。

**验收**：取消 AI workflow 会请求取消 render job；不会释放 UI thread 的 GPU object；render 结果进入生成素材箱，用户确认后才插入 timeline。

### M9：以“ComfyUI External Subgraph”兼容生成模型生态

这个节点是兼容层的核心，不是把 ComfyUI 复制进 Topos。

1. 新增 `ComfyConnectionProfile`：`local_http`、`cloud_api` 两类；存 base URL、credential ref、TLS 策略、允许上传范围、最大并发、最大文件大小。不得把 key 写入 workflow JSON。
2. 新增连接预检：读取服务器 capability / node 信息、可用模型目录和版本摘要；结果缓存并可手动刷新。无法识别的 server 不执行。
3. 支持两种导入：
   - **API format**：保持官方 `class_type + inputs + node id` JSON，作为可执行外部子图；
   - **UI workflow format**：只做 schema 校验和转换提示；失败时仍以附件保留原文件，不丢用户图。
4. `ComfyUI Workflow` 节点只暴露由导入器声明的安全参数绑定：prompt、negative prompt、seed、尺寸、时长、参考素材、输出节点。禁止让用户通过 Topos UI 改写任意 `class_type` 或注入未验证 endpoint。
5. 提交前冻结 workflow snapshot，解析所有输入 artifact 为上传/本地路径映射，显示将上传的文件、模型/自定义节点缺失、预计时长和费用；经生成同意 gate 后提交。
6. Job adapter 使用 server job ID，监听进度并支持 polling fallback；取消时调用相应 interrupt 接口。完整下载输出后计算 hash，写成 `GeneratedAsset`，进入既有 `GenerationBin`。
7. custom node 不在 Topos 进程内加载/执行。缺失或版本不一致时 node 状态为 `BLOCKED`，展示 `class_type`、所需包/版本及原 workflow 中的位置；允许用户在 Comfy 端修复后刷新。
8. 提供“导出回 ComfyUI API format”，但仅对 `ComfyUI Workflow` 子图及其绑定参数保证等价；Topos 原生 Agent、审批、时间线和 MCP 节点导出时生成注释/占位，不伪装成可直接执行的 Comfy 节点。

**验收**：使用官方稳定 Comfy workflow fixture，可在本地 mock server 完成 `import → bind prompt/reference → submit → progress → output import`；模型/节点缺失、网络中断、取消、输出下载失败均不会污染媒体池和 timeline。

---

## 7. 执行、缓存、权限与恢复

### 6.1 WorkflowRun 状态机

```text
DRAFT → VALIDATING → QUEUED → RUNNING → WAITING_FOR_USER
                              ↘ FAILED / CANCELLED
WAITING_FOR_USER → RUNNING / CANCELLED
RUNNING → SUCCEEDED_WITH_WARNINGS / SUCCEEDED
```

节点状态：`IDLE`、`BLOCKED`、`QUEUED`、`RUNNING`、`CACHED`、`WAITING_INPUT`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`SKIPPED`。任何失败节点必须留下可读 error code、建议处理方式和 debug detail（后者不含 secret）。

### 6.2 调度规则

复用 `AnalysisJobScheduler` 的 lane 思路，并将其升级/包装为 `WorkflowResourceScheduler`：

| lane | 任务 | 默认并发 | 播放期间 |
| --- | --- | ---: | --- |
| `analysis_local` | ASR、OCR、scene cut、Demucs | 1–2 | 新任务暂停 |
| `generation_local` | TTS、local video/music generation | 1 GPU job | 新任务暂停 |
| `network` | 云 LLM、MCP、云生成/下载 | 1 | 可继续，按 policy |
| `render_export` | Composite render / export | 受 render scheduler 管理 | 高优先级 |
| `control` | 纯 schema、条件、缓存命中 | 多个 | 可继续 |

- 同一 GPU 不允许 Demucs、ASR large、TTS 1.7B、视频生成同时启动；由 `ModelPackManager` + VRAM policy 预检。
- `cancel` 必须向下游传播，正在运行的 provider 使用协作 cancel token；已完成 artifact 可保留并标注 partial。
- 工程关闭时，不可恢复的本地任务安全取消；可恢复的云/下载任务仅保存 job ref，重开后显式恢复。

### 6.3 缓存

缓存键：`spec_id@version + canonical params + input artifact hash + model pack/version + executor version + quality/profile + policy-sensitive inputs`。

- 命中缓存仍需显示其来源、创建时间和是否与当前素材 fingerprint/revision 一致。
- `timeline_ref` / `edit_plan_ref` 的 cache 不可跨 revision 直接应用。
- 生成媒体默认不自动复用到不同 workflow，除非用户 pin 或显式选择“使用已有结果”。
- `force_rerun` 只跳过当前节点及其下游缓存；必须显示成本/资源影响。

### 6.4 权限和审批

| 动作 | 默认 | 需要的确认 |
| --- | --- | --- |
| 读取用户已选素材 / 本地分析 | 允许 | 范围超过选择集时询问 |
| 下载模型 | 询问 | 体积、许可证、路径、显存需求 |
| 云上传 / 云模型 | 询问 | provider、数据范围、预计成本 |
| 生成媒体 | 询问 | 模型、输入、成本、版权/水印信息 |
| 写媒体池 | 询问或项目策略允许 | 文件名、来源、目标 bin |
| 写 timeline | 必须审批 | `EditPlan` diff + project/timeline/revision/plan hash |
| 导出/发布 | 必须审批 | 路径/覆盖/平台/成本 |

`ApprovalToken` 只能使用一次，绑定 workflow run、node、project、timeline revision、plan hash 和过期时间。旧 revision 上的审批不可复用。

---

## 8. UI 信息架构与交互细节

### 7.1 页面布局

```text
┌ 模板/节点库 ┬──────────── 可缩放节点画布 ────────────┬ 节点检查器 ┐
│ 搜索        │ 运行状态 badge / group / notes / minimap │ 参数、模型 │
│ 输入        │                                         │ 输入输出   │
│ 分析        │                                         │ 成本/许可  │
│ Agent       │                                         │ 缓存/日志  │
│ 生成        │                                         │            │
│ Timeline    ├─────────────────────────────────────────┴────────────┤
│ Composite   │ Run Drawer：队列、进度、artifact 预览、diff、审批、日志 │
└─────────────┴──────────────────────────────────────────────────────┘
```

### 7.2 节点视觉状态

- 灰：未运行；蓝：排队/运行；绿：成功；紫：缓存命中；黄：等待下载/用户输入；红：失败；橙：有高风险输出尚未审批。
- 节点头部始终显示类别图标和名称；运行中的节点显示可取消进度，不能只转圈。
- 端口标注 artifact 类型缩写，例如 `TXT`、`ASR`、`SUB`、`PLAN`、`MEDIA`、`TL`。
- 端口连接前即时显示“可连接 / 类型不匹配 / 需要转换节点”的原因。
- 右键节点提供：运行到此处、从此处重跑、查看输入/输出、查看 provenance、清缓存、禁用、复制、封装为子工作流。

### 7.3 节点检查器

检查器分五组：参数、输入概览、执行（模型/lane/timeout/retry）、成本与许可证、结果与日志。敏感 token/key 不显示；提供“转到模型管理”“转到审批 Diff”“在媒体池定位”的链接。

### 7.4 新手模板

首批模板必须可运行且包含失败提示：

1. 自动字幕（Qwen → Demucs/WhisperX fallback → 字幕提议 → 审批）。
2. 字幕翻译与配音（字幕 → 翻译 → Qwen3-TTS → 音轨提议 → 审批）。
3. 口播精剪（选中素材 → 转录/静音 → 精剪计划 → Diff → 审批）。
4. 产品短片粗剪（Brief → 素材分析 → 选段 → Variant → Diff）。
5. 合成渲染后导入（选素材 → Composite Subgraph → Render → 导入生成素材箱）。

---

## 9. 旧节点面板排期（已由第 10 节的统一排期取代）

每个里程碑必须按“先测试/契约 → 实现 → focused tests → Edit/Composite 回归 → `git diff --check` → 手工验收”的顺序执行。未通过不能跳到后续 UI 或生成能力。

| 里程碑 | 实施内容 | 主要文件 | 关键难点 / 禁止事项 | 退出条件 |
| --- | --- | --- | --- | --- |
| M0 基线 | 建立 feature flag、目录、ADR、现有 Composite UI 依赖审计 | `docs/`、`ai_workflow/module.py` | 不移动现有 Composite 文件；先列出直接耦合点 | 关闭 flag 时零导入 AI Workflow |
| M1 画布抽象 | 抽 `NodeCanvasBase`、UI Protocol、视觉 token；Composite adapter 回归 | `src/shared/node_canvas/`、Composite UI | 不把 GPU/TextureResult 放入 shared；保留所有旧 signal | Composite 节点编辑回归 + fake workflow canvas 测试 |
| M2 图模型 | `WorkflowGraph`、spec registry、端口 type、DAG validator、JSON 迁移、Unknown node | `ai_workflow/core/` | 强类型、不用 `Any` 端口；循环/悬空/版本错误明确 | schema/拓扑/迁移/未知节点测试 |
| M3 运行时 | repository、artifact store、run/node 状态机、planner、cache key、日志、取消 | `core/workflow_*.py` | 不在 executor import Qt；取消/失败必须传播 | fake executor 并发、重试、缓存、取消测试 |
| M4 MVP 节点 | 输入、ASR、字幕 proposal、Diff、Approval、Apply、Run Summary | `executors/` | 复用 provider/CommandStack；Apply 前强 revision 校验 | 自动字幕模板端到端，Apply 可撤销 |
| M5 首版 UI | WorkflowPage、节点库、画布、Inspector、Run Drawer、模板画廊 | `ui/`、`module.py` | UI 仅订阅状态；不能在 slot 里跑模型 | Qt smoke、拖拽/连线/取消/审批手测 |
| M6 分析扩展 | OCR、scene cut、音频特征、素材筛选、模型下载节点 | `analysis_nodes.py` | 大批量分析显式 scope；GPU lane 限流 | 缓存与播放期间暂停验证 |
| M7 Agent / Skill | Brief、TaskGraph、rough cut、QA、Skill node、子任务 artifact | `editorial_nodes.py` | 子任务只读；单写者 policy；不可绕过 Approval | 口播精剪只产生 plan、无静默写入 |
| M8 Composite 兼容 | Composite Subgraph、render job adapter、生成素材箱 | `composite_adapter.py` | 绝不跨图传 texture；render 生命周期由 Composite 管 | 现有合成图可由 workflow 渲染并导入 |
| M9 生成媒体与 ComfyUI | TTS、音乐/图片/视频 provider nodes、ComfyUI external subgraph、来源/版权/成本卡片 | `generation_nodes.py`、`comfyui_adapter.py` | 生成结果先入素材箱；云上传逐次同意；不加载 custom-node 代码 | Comfy fixture 可生成并导入候选，TTS 模板真实/模拟均可恢复 |
| M10 控制与生态 | 条件、批处理、子工作流、MCP node、第三方 Skill/node manifest、少数核心 Comfy node converter | `control_nodes.py`、registry | allowlist/schema/权限；不可执行任意第三方代码；converter 必须有 golden fixtures | 导入不可信 node 时安全占位/禁用；稳定官方图可选择性转原生节点 |
| M11 可靠性 | checkpoint、恢复、资源监控、性能、错误中心、审计导出 | executor/scheduler/tests | 长任务、项目关闭、模型缺失、revision 变化 | 故障注入和 soak test 通过 |
| M12 发布 | 文档、模板、示例项目、迁移、可访问性、遥测 opt-in | docs/locales/tests | 不默认上传项目数据；稳定 schema version | 新用户能从模板完成字幕→配音→审批闭环 |

---

## 10. 统一实施排期（本节覆盖第 9 节旧顺序）

### 10.1 实施纪律：每一阶段都先接同一条底层链

每个阶段按以下顺序完成，不能跳过：

1. 给共享数据契约写失败测试（没有权限、没有素材、取消、revision 变化、模型缺失）。
2. 在纯 Python `ai_harness` 实现，不在 Qt slot 或节点 item 中执行模型。
3. 用 fake provider / fake scheduler 跑通；再接现有真实 provider。
4. 对话入口先接入并完成手工验收；节点图只能作为同一 `Invocation` 的第二入口。
5. 任何 timeline 写入都检查 `EditPlan → Preview → ApprovalGrant → CommandStack`，再做回归测试。

### 10.2 P0：建立统一 Harness 契约并补齐 AI Assistant 控制台

这是**当前最高优先级**。目标不是更聪明的模型，而是用户能知道“它将做什么、正在做什么、是否可停止、将写入什么”。

| 子步骤 | 要做什么 | 建议文件边界 | 难点与验收 |
| --- | --- | --- | --- |
| P0.1 统一 run 模型 | 新建 `Invocation`、`RunRecord`、`RunEvent`、`RunPolicy`、`RunStatus`；所有 source 均填 `chat/workflow/developer` | `src/features/ai_harness/invocation_models.py`、`run_store.py` | 禁止存 API key、绝对路径、未授权 prompt 全文。用同一 run_id 关联 TaskGraph、Job、artifact、审批与审计。 |
| P0.2 Harness 适配层 | 提供唯一的 `start/cancel/retry/resume/get_run`；先把现有 `AssistantService.run_workflow()`、`run_skill()` 包装进去，不立刻重写 provider | `runtime.py`、`scheduler_adapter.py`、对 `assistant_service.py` 做薄接线 | 成功标准：同一“自动字幕”从 chat 和 API 发起，获得同结构的 RunRecord；不允许两边各自创建 JobRegistry。 |
| P0.3 Assistant 底部控制栏 | 添加 Scope、权限、Profile/模型、上下文、压缩、新会话/历史的紧凑控件；截图中的单一发送栏升级为 Composer footer | `ai_edit_assistant_panel.py`、专用 `ai_assistant_composer.py` | 权限采用明确枚举：`read_only`、`propose`、`apply_with_each_approval`；云上传、生成付费、模型下载仍是单独 consent，不能被“完全访问”一键越过。 |
| P0.4 会话与历史 | 将现有 session store 演进为会话目录：`session_id`、标题、脱敏消息摘要、active run、artifact/job refs、压缩 checkpoint、归档时间 | `assistant_session_store.py`、`session_models.py` | 新对话不能删除工程历史；恢复旧会话后旧 plan 必须以当前 revision 重做 preview，旧 grant 永不可恢复。 |
| P0.5 上下文可见/压缩 | Composer 常驻显示 `used / limit`、来源数量、压缩状态；增加“检查”“压缩”“恢复压缩前 checkpoint” | `context_bundle.py`、`context_service.py`、panel | 压缩只能生成新摘要 block，原始 evidence/artifact 不丢；压缩后仍可追溯来源，超过限额先排除低优先级块而非静默截断硬约束。 |
| P0.6 Job/取消贯通 | `RunRecord` 订阅 `JobRegistry` 与 TaskGraph 事件；修复取消为真实 cancel token 传播，而非仅变更状态文案 | `jobs.py`、`task_graph.py`、controller worker | 在 ASR/LLM fake job 中测试：取消后下游不再启动、已完成 artifact 标 partial、UI 有原因；工程关闭不回写关闭的项目。 |

P0 的可见 UI 验收：用户打开 Assistant 时，底部能看见“本工程/当前选择”“仅建议”“本地 Qwen”“8.4k/32k”“压缩”“新对话”“历史”；发出“分析所有素材”后看到确认卡和逐项任务，而不是空白面板或模糊的“处理中”。

### 10.3 P1：把聊天变成 Skill 驱动的 Agent，而非单一路径 Planner

| 子步骤 | 实施方式 | 不能犯的错误 | 验收 |
| --- | --- | --- | --- |
| P1.1 意图路由 | `InvocationRouter` 先做确定性命令/关键词/快捷卡匹配：分析、字幕、翻译配音、口播精剪、脚本、帮助、生成；模型只用于补槽位/解释/建议 | 不把用户文本直接拼接为 tool 参数；不确定时先问范围、语言、目标时长等一个必要问题 | “分析所有素材”“给当前字幕配中文配音”“这个功能怎么用”分别选择正确 Skill/Help，不误入粗剪 Planner |
| P1.2 Skill 目录与表单 | `SkillRegistry` 驱动聊天 slash command、推荐卡、风险/模型/证据前置条件、可编辑输入表单 | Skill manifest 不能自行提权；local_dev 必须标未验证 | 用户能在聊天中查看 Skill 所需模型、预计工作和输出，确认后才开始 |
| P1.3 统一编译 | 将现有 `WorkflowSpec`、`SkillManifest` 收敛为 `SkillDefinition`；`SkillCompiler` 生成 `TaskGraph` 或调用受控 legacy adapter | 不再让 `run_workflow`、`run_skill` 各自维护 variant/消息/状态副本 | 同一内置“口播精剪”可由 chat、快捷卡、后续 workflow 模板启动，最终都走同一 plan repo/approval/undo |
| P1.4 帮助助手 | 把 `HelpIndex.search()` 设为无模型可用 Skill；模型回答必须带 entry_id、页面位置、当前 feature 状态、下一步 | 不从模型记忆编造产品功能；不可用功能必须说未安装/未启用 | 无 profile 状态下也能搜索帮助；答案可点击定位或给准确路径 |
| P1.5 运行结果卡 | 统一渲染 evidence、脚本、字幕草稿、Variant、生成素材、QA、错误和可执行按钮 | 不把未审批 proposal 显示成已应用；不将 OCR/网页不可信文本当作字幕事实 | 每个 assistant 结果能“查看依据 / 重试 / 调整参数 / 预览 diff / 审批” |

### 10.4 P2：媒体分析与第一批端到端 Skill

先完成用户可获益最大的本地链路，再接更多子智能体或 ComfyUI。

1. `analyze_selection` / `analyze_bin` / `analyze_all_assets` 三个 Skill 共享同一分析任务图；媒体池“AI 分析”开关决定是否允许主动分析，聊天命令只请求用户已确认的 scope。
2. 每个素材的任务图按依赖执行：媒体探测/代理与音轨提取 → Qwen3-ASR → 低质量时 Demucs → WhisperX fallback → diarization/镜头/OCR/节拍/响度 → evidence normalize → 索引与 freshness 更新。每一阶段都产版本化 `EvidenceRef`，不能把模型原始文本直接塞给 LLM。
3. 完成四个官方 Skill：自动字幕、字幕翻译与配音、口播精剪、产品短片粗剪；它们在聊天优先呈现，Workflow 页面未来直接复用相同模板。
4. 字幕、音频放置和粗剪都只产 `EditPlan`；生成或已有音频先入媒体池/生成素材箱；只有最后的 Diff/Approval 写入时间线。

验收样例：对背景音乐很重的片段运行自动字幕，RunRecord 明确显示 Qwen 质量、是否进入 Demucs、是否回退 WhisperX、最终证据版本；用户可由该结果创建字幕轨道并一次 Undo。

### 10.5 P3：真实受控子智能体、MCP 与生成能力

| 方向 | 先做的内容 | 完成定义 |
| --- | --- | --- |
| 子智能体 | 用 model-backed `RoleExecutor` 替代确定性 stub；角色从创意/素材/故事/QA 开始，每个角色只能拿最小 ContextSlice 和允许工具 | 子任务有 token/成本/工具次数/超时、运行事件、失败/降级和可保留 artifact；没有任何角色能直接获得 ApprovalGrant 或写时间线 |
| MCP | 在现有 `McpServer` 之上加受控 stdio transport、连接 Profile、server allowlist、状态/日志/授权卡；MCP 工具仍经 ToolFacade | 默认只读、本机、会话级启用；每个外部 server 显示作者/版本/域名/数据范围，写操作仍弹 Topos 审批 |
| 生成 | 将已有 GenerationRequest/GenerationBin 接入 Harness job；图片/视频/音乐/TTS 都是 provider task，返回候选 artifact | 生成前显示模型、上传、成本、许可证/安全；结果不自动进入 timeline；取消/失败不污染工程 |

### 10.6 P4：开发者 Skill Studio（节点图发布前的必要层）

开发者不应通过复制 Python 代码来“发布一个工作流”。先提供一个受控的 Skill Studio：

- 从官方模板或现有 `WorkflowDefinition` 创建版本化草稿；声明输入/输出 schema、风险、工具 allowlist、模型/资源要求、默认 policy、UI card 和许可证。
- 运行 dry-run：用 fake provider、固定 evidence fixture、固定 timeline snapshot 生成 `RunRecord`，检查端口、DAG、预算、权限、缓存 key、plan diff。
- 为每个 Skill 编写 eval：成功、模型缺失、证据不足、拒绝上传、取消、revision stale、无 approval 六类最低用例；发布前必须通过。
- 发布产物是 `SkillDefinition + graph template + manifest + eval version + content hash`。本地开发 Skill 醒目标“未验证”；第三方不得执行任意 Python、shell、文件系统或 UI 自动化。

### 10.7 P5：AI Workflow 作为高级入口（只接同一 Harness）

完成 P0–P4 后才创建可编辑节点面板。顺序如下：

1. 先复用 Composite 的画布视觉/交互，抽取 `NodeCanvasBase`；不复用 GPU `NodeGraph`。
2. `WorkflowDefinition` 只保存节点、端口、参数、布局和暴露输入；点击 Run 时由 `WorkflowCompiler` 生成 `Invocation` 并调用 `HarnessRuntime`。
3. Run Drawer 直接订阅 `RunRecord`，因此聊天运行和节点运行可在同一个任务中心查看、取消、重试和审计。
4. “发布为 Skill”只是把当前 workflow 的 graph template 和声明打包到 `SkillDefinition`；聊天自动出现该 Skill 卡片，输入 schema 相同。
5. 先交付官方模板的查看、复制、参数编辑、运行和 debug；最后再开放空画布、子图、循环/批处理和第三方导入。

验收的关键反证：从聊天运行“自动字幕”和从节点模板运行“自动字幕”，应产生相同的 TaskGraph 拓扑、Job 类型、Artifact provenance、Diff 与 CommandStack commit 路径；若任一不同，说明已经形成第二套系统，必须停止并收敛。

### 10.8 P6：Composite / ComfyUI / 生态扩展

仅在节点面板已是同一 Harness 客户端后，执行本计划第 1、5、6、11、12 节描述的 Composite 子图、生成节点和 ComfyUI C0–C5 分层兼容。外部 ComfyUI workflow 仍应被包装为一个 `TaskGraph` provider 节点；不能让其绕开 RunRecord、媒体素材箱、权限或 timeline proposal 链。

### 10.9 完整验收矩阵：三类用户必须同底层

| 场景 | 普通用户（聊天） | 高级用户（节点图） | 开发者（Skill Studio） | 必须相同的底层证据 |
| --- | --- | --- | --- | --- |
| 自动字幕 | “给选中片段生成字幕” | `Selected Assets → Transcribe → Subtitle Proposal` | 发布 `auto_caption` Skill | Skill version、TaskGraph、ASR evidence、Job、EditPlan、approval/undo |
| 粗剪 | “用所有已分析素材做 30 秒竖版” | 分析输入→粗剪→QA→Diff | 编辑 rough-cut graph 并跑 eval | selection scope、TaskGraph、Variant/Plan repo、QA、revision gate |
| 生成配音 | “把这条字幕配成英文女声” | Subtitle→Translate→TTS→Placement | 发布 voiceover Skill | provider consent、GenerationBin、artifact provenance、placement plan |
| 功能教学 | “怎么做关键帧？” | 可选 Help 节点 | 编写官方帮助 Skill/eval | HelpIndex citation、feature state、无时间线写入 |

---

## 11. 关键实现流程（新人照着做）

### 9.1 “自动字幕”模板的执行顺序

1. `Selected Assets` 读取用户已选择的素材 ID；若为空，节点进入 `WAITING_INPUT`，不扫描整个媒体池。
2. `Transcribe Auto` 用现有 `AdaptiveTranscriptProvider`：Qwen3-ASR 原音频 → 低质量时 Demucs vocals → 仍不足时 WhisperX。结果保存 evidence/provenance。
3. `Propose Subtitle Track` 把时间戳 transcript 转为 `EditPlan`，不立即创建字幕轨道。
4. `Preview Diff` 检查 project/timeline revision、锁定轨道、冲突和字幕覆盖策略，给出可读 diff。
5. `User Approval` 创建一次性 token。
6. `Apply Approved Plan` 通过现有 CommandStack 提交 macro；成功后输出更新的 `timeline_ref`，支持 undo。

### 9.2 “字幕翻译 + AI 配音”模板的执行顺序

1. 输入现有字幕轨道或从转录生成字幕 proposal。
2. 翻译节点输出可编辑 `text_document`，在 UI 显示术语/行数/阅读速度诊断。
3. Qwen3-TTS 节点合成到生成素材箱；记录 `qwen3_tts_customvoice`、tokenizer、说话人、语言、语气、输入 hash 和许可。
4. 音频放置节点生成音轨插入 `EditPlan`，如果会覆盖现有音频则默认提议新建“AI 配音”轨道。
5. Diff + 一次性审批后才提交。

### 9.3 “Composite Subgraph”执行顺序

1. Workflow 输入的是 `asset_ref` 或 `selection_set`，不是 texture。
2. 节点选择一个已保存的合成图 `composite_graph_ref`，校验节点版本和输入映射。
3. adapter 请求 Composite render service 创建 render job，并把 quality/range/proxy 写入 provenance。
4. render 成功输出 `render_job_ref` 和临时结果 artifact；需要时经用户确认导入媒体池。
5. 如需进 timeline，继续走 `propose placement → diff → approval → apply`。

### 9.4 “ComfyUI 图片/视频生成”执行顺序

1. 用户在模板画廊选择一个受支持的 Comfy workflow，或导入自己的 `.json`；系统先离线解析 schema，不联网执行。
2. `ComfyUI Workflow` 节点连接 `text_document`、`asset_ref`、`selection_set` 等 Topos artifact；只允许填充该 workflow 预先声明的绑定槽位。
3. 点击运行时，UI 显示目标服务器、上传的参考素材、workflow hash、所需 custom nodes/模型、成本与输出位置；用户确认后创建 generation job。
4. adapter 将完整 API-format snapshot 提交给 Comfy server，保存外部 job ID 并把 websocket/polling 进度转成 Topos node 状态。
5. 结果图片、视频或音频完整下载、hash 和安全/许可证信息写入 `GenerationBin`；用户在生成素材箱预览、丢弃或导入媒体池。
6. 需要进入时间线时，仍走 placement proposal、Diff、Approval 和 CommandStack；生成节点没有直接插轨权限。

---

## 12. 测试、迁移和验收清单

### 10.1 单元测试

- `WorkflowGraph`：ID 唯一、DAG、端口兼容、单/多输入、cycle、未知节点、schema migration。
- `WorkflowValidator`：缺模型、缺权限、云上传、超预算、锁定轨道、stale revision。
- `HarnessRuntime` / `TaskGraphExecutor`：拓扑顺序、并行分支、取消传播、retry、cache hit/miss、checkpoint、partial failure。
- 每个 executor：只用 fake provider；断言输入/输出 artifact schema 和 provenance。
- `WorkflowCommitBridge`：无 approval、过期 approval、hash 不匹配、revision 变化、CommandStack rollback。

### 10.2 UI 测试

- 画布缩放/平移/框选/连线/类型拒绝、节点搜索、复制粘贴、分组、注释、状态色、运行 drawer。
- Inspector 参数校验、模型缺失提示、风险/费用显示、确认对话框。
- 关闭页面/工程时运行任务 UI 不崩溃；后台结果不回写已关闭项目。

### 10.3 集成与手工验收

1. 空工程打开模板，选择一个音频素材，完成自动字幕 proposal，确认后可 undo/redo。
2. 背景音乐重的素材能记录是否调用 Demucs 和最终 router selection。
3. 无 Qwen3-TTS 模型时显示精确的安装提示；不能假装生成成功。
4. 播放中提交 ASR/TTS，任务正确排队，播放不卡顿。
5. 运行时修改 timeline，再点 Apply，旧 plan 被拒绝并提示重新预览。
6. Composite Subgraph render 取消后不泄露 GPU/临时资源，也不产生半写入 timeline。
7. 导出 workflow 后在新工程导入，未知第三方节点显示占位，不执行其代码。
8. 导入 ComfyUI API workflow 后，缺少 `class_type`、custom node、模型或参数绑定时在执行前 BLOCKED；不得自动下载或执行未确认代码。
9. ComfyUI job 取消、网络中断、输出 hash 不一致或下载失败时，候选不进入媒体池，timeline 不发生变化。

### 10.4 性能基线

- 100 个纯控制/分析节点打开画布、拖动和缩放不明显卡顿。
- UI 每次状态更新只刷新变化节点，日志采用虚拟列表/限长缓存。
- 缓存命中不重新启动模型，也不重复触发成本/上传确认。
- Composite 页面性能回归基线不下降；AI Workflow 未启用时不增加常驻 GPU/模型内存。

---

## 13. 需要在开发前做出的产品决策

这些选项会改变数据模型和 UI，不能由实现者静默猜测：

1. AI Workflow 是独立主页面、Edit 里的 Dock，还是两者都有？本计划建议：**独立主页面 + Edit/Composite 的“在工作流中打开”入口**。
2. 工作流定义是否保存到项目文件？建议：项目内保存引用和版本；官方模板内置；用户模板另存到用户目录。
3. 第三方节点是否允许 Python 插件？建议首版只允许声明式 manifest + 内置/受控 executor；Python 插件留 M10 且必须隔离、签名、权限提示。
4. 云模型成本是否允许自动执行？建议只允许项目级上限内的已授权 provider；每种新 provider/上传范围首次确认。
5. 是否支持批量 `For Each Asset`？建议 M10 再做，首版模板节点内部批量但有明确最大素材数量和预算。
6. ComfyUI 后端是否允许 remote/cloud？建议首版支持用户显式配置的本机 server，Cloud API 作为独立 profile，默认关闭并每次展示上传范围与成本。
7. 是否自动管理/安装 Comfy custom nodes 与大模型？建议首版**不自动安装**；只检测、提示并跳转到用户已配置的 Comfy 环境，M11 后才评估受控下载器。
8. 是否把 ComfyUI 模型目录与 Topos 模型目录合并？建议不合并、不复制权重；通过 profile 配置和只读 discovery 识别，避免重复占盘和损坏模型管理边界。

---

## 14. 首次开发任务（从第 10 节 P0 开始）

第一张开发任务只做 **P0.1–P0.2**，先不创建 AI Workflow 页面、节点画布或新 provider：

1. 新建 `src/features/ai_harness/`，定义纯 Python 的 `Invocation`、`RunPolicy`、`RunRecord`、`RunEvent`、`RunStatus`；为序列化、脱敏、source、scope、policy、状态迁移写测试。
2. 写 `HarnessRuntime` 的 fake 版本：接受 `Invocation`，分配 run_id，发布 event，能够 cancel/retry；不接 Qt、OpenGL、网络或模型。
3. 为现有 `AssistantService.run_workflow()` 和 `run_skill()` 写 legacy adapter，并断言它们均将结果归入同一种 RunRecord。此时保留旧 public API，避免一次性大重构。
4. 扩展 `AssistantSessionStore` 的 schema，加入会话目录和 run/artifact 引用；提供 migration，旧工程读取后不丢 Brief/Variant/Decision。
5. 给 `AiEditAssistantPanel` 先接只读的 Run Drawer/Composer footer fake 数据：新会话、历史、scope、权限、profile、context token、压缩与任务状态。确认 UI 布局后才接真实 runtime。
6. 测试三条红线：取消会传播、旧 approval 不可复用、工程关闭后 run 不回写；通过后才启动 P1 的聊天意图路由。

在 P0 的 Assistant 验收通过前，**不要**提前抽 Composite 画布或注册空的 `ai_workflow` FeatureModule。那些工作移到 P5，避免团队把有限时间耗在高级界面而普通用户仍无法可靠完成“分析、字幕、配音、粗剪”。

### 14.1 生成模型与 ComfyUI 的首次开发任务（P6 之前不得开始）

1. 为 `ComfyConnectionProfile`、`ComfyWorkflowSnapshot`、`ComfyInputBinding`、`ComfyJobRef` 写纯 Python dataclass 和 JSON schema 测试。
2. 用本地 mock HTTP/WebSocket server 固化官方 API-format workflow fixture；测试 submit、progress、cancel、history/output 映射，不依赖真实 GPU。
3. 实现只读 Comfy workflow importer：输出节点/模型/自定义节点依赖报告；禁止导入时执行任何 custom node。
4. 实现 `ComfyUI Workflow` executor 的 `GenerationProvider` adapter，结果必须进 `GenerationBin`，不得直接导入 timeline。
5. 只在以上通过后，才引入真实本机 Comfy server 和一个官方图片生成模板；视频生成、ControlNet、LoRA、社区 custom nodes 分阶段增加。

这保证“复用成熟节点能力”先成为稳定公共基础，而不是复制一个很快分叉的第二套节点编辑器。
