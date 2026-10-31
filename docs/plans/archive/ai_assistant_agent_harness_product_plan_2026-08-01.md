# Topos AI Assistant：剪辑 Agent 与 Agent Harness 产品化计划（2026-08-01）

> 本文把 Topos 的 AI assistant 定义为一个**面向创作、剪辑和软件使用的受限 Agent Harness**，而不是单纯的聊天框或能任意操控本机的通用 Agent。
>
> 它的目标是：用户用自然语言完成“想什么、找什么、怎么剪、如何生成、怎样交付”；软件则始终把 AI 的建议、权限、计划、差异、成本、来源和可撤销性讲清楚。本文是产品路线图，建立在 TAEP、既有 `ToolFacade`、`EditPlan`、事务与审批架构之上；它不取代这些协议文件。

## 0. 实施者先读：如何按本计划开发

这份文档既是产品能力清单，也是工程实现说明。为了让新人可以按部就班完成，**唯一允许作为排期依据的是第 11.1 节的 M0–M12 主路径**；第 15–18 章和第 11.3 节是功能说明、字段定义和细化 checklist，不能跳过前置阶段直接实施。

每个里程碑都按同一个闭环执行：

1. 阅读该里程碑的“目标、前置条件、禁止事项”。
2. 先补或确认该阶段的测试 fixture 与失败场景，再改实现。
3. 只修改该阶段列出的边界；UI 不直接运行模型，模型/provider 不直接修改 timeline。
4. 运行该阶段 focused tests、相关 edit 回归和 `git diff --check`；手动完成验收项。
5. 只有所有退出条件通过，才标记完成并进入下一阶段；未通过时修复本阶段，不把问题推给后续 Agent、MCP 或生成能力。

贯穿全程的四条红线：

- 未经用户确认不得分析整批素材、上传数据、改时间线、生成付费素材或导出文件。
- 所有编辑必须是 `EditPlan → Preview/Diff → Approval → CommandStack → Undo/Redo`；不允许 UI 或 provider 直接改数据。
- 所有长任务都进入可取消 Job；Qt UI 线程与 renderer 线程不运行模型推理、全片解码或网络请求。
- 项目只保存版本化、可解释的结果；API key、绝对路径和可重建的大型缓存不进入工程文件。

## 1. 产品结论与边界

### 1.1 产品定位

Topos AI Assistant 有三种统一但明确隔离的角色：

| 角色 | 用户得到的价值 | Agent 可做的事 | 不可做的事 |
| --- | --- | --- | --- |
| 创作搭档 | 把想法变为 Brief、文案、脚本、分镜、剪辑大纲和多种方案 | 提问、检索授权资料、生成结构化创作产物、比较方案 | 把生成文本或建议悄悄写入时间线/导出成片 |
| 剪辑执行 Agent | 从已导入、已授权的素材中找内容、粗剪、加字幕/音乐/效果并给出 Diff | 调受限的 Topos 语义工具，提交预览计划、创建后台任务 | 操作任意文件、运行 shell/Python、绕过确认写时间线 |
| 软件使用助手 | 回答“这个功能在哪、怎么用、为何失败”，并能带着用户完成操作 | 搜索本地帮助、读取当前 UI/工程的脱敏状态、发起受限的导航或演示建议 | 假装功能存在、远程控制鼠标键盘、替用户确认破坏性动作 |

“Harness”在这里指运行时控制层：它负责模型路由、上下文、工具、Skill、任务图、权限、审批、记忆、审计、评估和失败恢复。模型只是可替换的推理组件，不是唯一真相，也不拥有宿主权限。

### 1.2 不变量（任何新能力都必须满足）

1. **人是最终剪辑师。** AI 的任何 timeline、媒体生成、导出或云上传写操作，都先生成结构化计划和可读 Diff，再由用户确认。
2. **模型与外部内容不可信。** prompt、MCP 输出、网页、文件名、字幕、OCR、转录、插件 manifest 都是数据，不能改变系统指令和授权。
3. **一份计划，一个事实源。** AI 不创建第二套时间线；所有编辑编译为既有 `EditCommand`/`MacroCommand`，经统一提交入口、revision、undo/redo 和 renderer 同步。
4. **最小权限且可见。** 无项目、无选择范围、无授权、无 capability 时，Agent 明确说明限制，绝不猜测或静默升级权限。
5. **预览即提交。** 用户看到的 Diff 绑定 project、timeline、revision、manifest hash 与 plan hash；其中任何一项变化均作废。
6. **来源、成本、版权可追溯。** 外部资料、生成视频/音乐、商用授权、模型、提示版本、输入素材、输出 artifact 都有 provenance；未知权利状态不能被表述为“可商用”。
7. **界面不阻塞。** 推理、转录、检索、代理/渲染、生成媒体和导出都是可取消 Job；UI 只消费状态事件。

### 1.3 明确不做

- 不向 LLM 暴露任意 shell、Python `exec`、OpenGL context、原始文件系统或自动化点击能力。
- 不把“所有 Skills/MCP/子 Agent 都显示为一长串按钮”当成功能完成；只显示当前任务可用、已安装、已授权的能力。
- 不在首版做公网 MCP server、多人自治 Agent 或全自动发布。先完成本机、项目作用域、可审计的闭环。
- 不让生成式视频/音乐默认混入成片；每个生成资产都必须可预览、可替换、可删除、可标记来源与授权。

## 2. 目标用户体验

### 2.1 主 Dock 信息架构

主界面应借鉴 Codex 的“会话 + Composer + 状态控制”心智模型，但内容应服务剪辑，不复制编程终端。

```text
┌ 新会话  会话名称/历史 ▾ ─────────────────────── ⚙ 设置 ┐
│ [本地执行] [模型 Profile ▾] [仅建议 ▾] [上下文 8.4k/32k] │
├──────────────── 对话 / 工作区 ───────────────────────────┤
│ 用户：做 30 秒竖版新品宣传，突出续航和快充                       │
│ Agent：已读取 12 条已授权素材、96% 转录、2 个锁定片段           │
│ [Brief 草稿] [方案 A] [方案 B] [证据] [Timeline Diff] [任务]   │
│ “A 使用产品特写；B 以人物反应开场。请选择或要求重规划。”        │
├──────────────── Composer ────────────────────────────────┤
│ + 添加上下文  范围：选中素材 ▾  /工作流  权限：仅建议 ▾         │
│ 输入你的需求…                                           发送 │
└──────────────────────────────────────────────────────────┘
```

窄面板时，顶栏把会话、模型、权限、上下文折叠为可点的状态胶囊；不得为了显示全部控制项而挤压 timeline。状态显示必须使用真实数据，特别是 token、费用、上传与 Job 状态，不能写死或伪造。

### 2.2 关键 UI 区块与验收

| 区块 | 必需信息/行为 | 规则 |
| --- | --- | --- |
| 会话栏 | 新会话、项目内历史、重命名、归档、恢复、搜索、删除 | 会话按 project/timeline 隔离；删除前区分“仅删聊天”和“删保存的计划/审计”。 |
| Profile 与隐私栏 | 模型、provider、local/cloud、连接、上传策略、成本提示 | 快速切换只影响后续 Job；密钥仅在 Preferences 管理，永不显示。 |
| 上下文栏 | 实际 token 用量/预算、内容类别、压缩状态、来源数、过期提示 | 点击可检查“哪些素材/片段/文档被纳入”，并可排除。 |
| 对话区 | 消息、推理结果摘要、工具调用摘要、结构化卡片、错误与取消 | 默认展示有用的行动摘要，不展示模型隐性思维链；工具参数可折叠查看。 |
| 计划工作区 | Brief、脚本、分镜、选段证据、Variant 对比、Diff、批准/撤销 | 每张卡都能定位到素材/时间线；任何写操作有明确风险标签。 |
| Composer | 多行输入、附件/上下文选择、范围、工作流、权限、发送/停止 | Enter 发送、Shift+Enter 换行；无权限时解释需要什么而不是静默失败。 |
| Job 抽屉 | 排队、进度、成本、取消、重试、结果、日志摘要 | 任务离开面板后仍可继续，工程关闭时可安全取消/恢复。 |

### 2.3 权限层级（用户能理解的语言）

| 模式 | 可用动作 | 默认值 | 提升方式 |
| --- | --- | --- | --- |
| 仅建议 | 阅读被选择的上下文；生成 Brief/文案/方案；搜索帮助 | 默认 | 无需确认 |
| 预览计划 | 验证计划、生成 timeline Diff、低成本临时预览 | 默认允许，但可在偏好关闭 | 用户/项目策略开启 |
| 应用到时间线 | 原子提交经验证的计划 | 关闭 | 对当前 plan+revision 的一次确认 |
| 生成或导入素材 | 发起视频/音乐/图像/语音或受控导入 Job | 关闭 | 每次确认 provider、成本、版权/上传与目标位置 |
| 导出/发布 | 写文件、上传或调用第三方发布连接器 | 关闭 | PathGrant + 最终确认；覆盖需额外确认 |

不要提供“完全访问”这种笼统开关；高级用户可保存一个严格范围的自动化 profile，但仍以能力、项目、路径、额度、时效和审计为边界。

## 3. Agent Harness 总体架构

```text
Qt AI Assistant UI
  │ typed UI events / state snapshots
Assistant Orchestrator ─ Session / Context / Policy / Task Graph
  ├── Model Router ─ text / vision / audio / video / music providers
  ├── Skill Runtime ─ versioned workflow packages and contracts
  ├── Tool Broker ─ TAEP ToolFacade / Help tools / Media-generation tools
  ├── Subagent Coordinator ─ bounded role workers, artifacts only
  ├── MCP Gateway ─ local stdio first, authenticated adapters later
  ├── Job Scheduler ─ cancellation / quota / retry / resource lanes
  └── Memory & Audit ─ project session / provenance / evaluation traces
                │
Topos application services ─ EditSession / Analysis / Effects / Export / Help index
                │
Canonical project + CommandStack + renderer + registries
```

### 3.1 必须新增或收口的服务

| 模块/服务 | 职责 | 关键约束 |
| --- | --- | --- |
| `assistant_orchestrator.py` | 执行一次用户请求，选择 Skill/模型/工具，维护 task graph | 只协调，不能直接改 UI、timeline 或 GPU。 |
| `agent_policy.py` | 会话、角色、工具、MCP、生成、网络、预算策略的统一判定 | 所有入口共用，不能把安全判断散在 UI。 |
| `context_service.py` | Token 预算、选择、引用、摘要/压缩、过期和 provenance | 输出可检查的 `ContextBundle`，不只是一段拼接 prompt。 |
| `session_service.py` | 会话列表、消息元数据、检查点、恢复、保留/删除 | 复用并扩展现有 `AssistantSessionStore`；不默认保存完整云端对话。 |
| `skill_registry.py` | 安装、签名/来源、版本、能力、参数 schema、启用策略 | Skill 不拥有裸权限，只声明所需工具/权限。 |
| `task_graph.py` | 父任务、子任务、依赖、artifact、取消传播、资源限额 | 子任务只能返回结构化 artifact 或 Job ref。 |
| `model_router.py` | 选择 provider、能力、成本、超时、fallback、实际 token 统计 | Profile 切换不影响已开始 Job；禁止把 key 写入日志。 |
| `media_generation_service.py` | 统一请求/监控视频、音乐、语音、图片生成 Job | provider adapter 可换；输出先入“生成素材箱”，不是直接进 timeline。 |
| `generation_recipe.py` | 将创作意图、镜头约束、参考资产、输出/交付要求编译为 provider-neutral `GenerationRecipe` | 不把厂商字段泄漏到 Agent/UI；引用素材按角色和授权范围显式声明。 |
| `generation_provider_packs/` | Seedance / 图像 / 音乐 / TTS 等厂商适配：能力发现、请求映射、上传、提交、轮询、取消、下载和错误归一化 | 每个 Pack 独立版本、许可、地域、数据保留和 kill switch；不直接写工程或 timeline。 |
| `generated_asset_materializer.py` | 将远端生成结果下载、校验 hash、probe、创建缩略图/代理并导入 Media Pool | 临时 URL 不是工程唯一依赖；失败或取消不写入项目。 |
| `generation_quality_service.py` | 生成候选的技术/时序/品牌/安全区质量门与可剪辑性评分 | 评分是排序建议，不替代用户预览，也不能静默丢弃候选。 |
| `creative_asset_registry.py` | 角色、产品、场景、风格、品牌与 reference package 的版本化复用 | 仅保存已授权 asset ref、token 与摘要；跨镜头一致性不可依赖隐式聊天记忆。 |
| `help_knowledge_service.py` | 构建本地功能文档索引、版本/feature-aware 检索和操作提示 | 答案携带本地文档引用与版本，找不到时坦诚说明。 |
| `mcp_gateway/` | 给外部模型或本地客户端提供工具协议适配 | 首版 stdio/local IPC；MCP 只做 transport，核心语义仍收口到 ToolFacade。 |

### 3.2 与现有代码的衔接

- 保持 `src/features/edit/ai_editing/tool_facade.py` 作为剪辑语义工具的唯一入口；新增的 orchestration、MCP、Skill 不能绕过它。
- 保持 `AssistantService` 的状态机与 `AiEditController` 的 UI 绑定，但把复杂多步编排下沉给 `AssistantOrchestrator`，避免它膨胀为 `Any` 驱动的上帝类。
- 复用 `AiProviderProfile`、credential store、`ApprovalService`、`Job`、`EditPlan`、`CapabilityManifest`、`AuditStore`、`AssistantSessionStore`；先修复/完成其生产闭环，再叠加体验层。
- 所有新增 effect、transition、caption、生成媒体、导出 preset 都必须先进入 capability registry、schema、validator、diff、审计和测试，才可被 Agent/Skill 看见。

## 4. 工具系统与 MCP

### 4.1 工具分类

| 工具域 | 代表工具 | 权限 | 交付物 |
| --- | --- | --- | --- |
| 工程理解 | capabilities、项目/时间线快照、选区、撤销状态 | read | 脱敏 Snapshot |
| 素材理解 | 搜索素材、转录/镜头/音频分析、相似镜头、版权状态 | read / expensive | evidence refs 或 analysis Job |
| 创作 | 创建 Brief patch、文案、脚本、分镜、镜头清单、Variant | proposal | 版本化文本/结构化 artifact |
| 编辑 | validate plan、预览 Diff、应用 plan、锁定/拒绝决策 | preview / write+approval | Preview/Commit Result |
| 效果 | 查询 Effect Catalog、推荐 preset、应用有 schema 的 effect | proposal / write+approval | Plan operation，不生成任意 GLSL |
| 生成媒体 | 创建/校验 Recipe、估算成本、发起/查询/取消 Job、比较候选、导入生成物、提议上时间线 | expensive / import approval | `GenerationRecipe`、生成素材箱 artifact、质量报告、EditPlan proposal |
| 交付 | 渲染预览、导出预检、导出、发布提议 | expensive / file-write+confirmation | Job/Artifact |
| 软件帮助 | 搜索帮助、解释当前选择、显示功能位置、生成教程步骤 | read / navigation proposal | citation + 步骤，不自动点击 |

### 4.2 MCP 实施策略

1. **先做内部 Tool Contract 完整性。** 每个 tool 有稳定名称、JSON Schema、版本、权限、输入/输出大小限制、诊断码、审计与 JSON serializable 测试。
2. **MCP server 只做薄适配。** 首版提供 stdio 或仅当前用户可访问的 local IPC；将请求变为 `ToolFacade.invoke()`，绝不在 MCP handler 内实现剪辑逻辑。
3. **动态暴露工具。** 每次 MCP session 根据 active project、feature flags、profile、用户 policy 和 capability manifest 返回可用工具；不支持的能力不得注册。
4. **MCP client 受 allowlist 管理。** 后续接入第三方 MCP server 时需要显示其作者、版本、工具、网络域名、数据范围和权限；默认只读、会话内临时启用。
5. **认证与网络边界。** 不监听公网；local transport 用用户级 socket/nonce，远程连接必须显式配对、最小 scope、可撤销、不可携带项目密钥。
6. **MCP 输出是非可信数据。** 所有返回经过大小限制、schema 验证、内容标记和 prompt-injection 隔离；任何“请执行以下指令”文本都不具权威性。

建议公开名称采用 `topos.*`，例如 `topos.search_assets`、`topos.create_edit_proposal`、`topos.preview_edit_plan`、`topos.request_media_generation`；不要把 Qt 控件点击、文件绝对路径或 Python 对象暴露为工具。

## 5. Skills：把专业工作流产品化

### 5.1 Skill 定义

Skill 是可版本化的、参数化的工作流包，不是任意 prompt 或任意代码。它由说明、输入 schema、输出 schema、允许的工具、风险/成本、质量检查、UI 卡片与测试组成。用户侧名称应优先叫“工作流”，开发者/扩展页可显示“Skill”。

```text
skills/
  social_short_form/
    skill.json              # id/version/author/signature/license
    instructions.md         # 目标、约束、失败降级（无隐式系统权限）
    input.schema.json
    output.schema.json
    tool_policy.json        # 声明式 allowlist 与 permission level
    ui.json                 # 参数表单、结果卡片、i18n keys
    evals/                  # golden fixtures / assertions
```

### 5.2 首批官方工作流

| Skill/工作流 | 输入 | 输出 | 高风险动作 |
| --- | --- | --- | --- |
| 口播精剪 | 转录、人物/静音规则、目标时长 | 句子级 selects、去停顿方案、字幕草稿 | 应用 trim/delete 前确认 |
| 30 秒产品广告 | Brief、产品 claim、素材范围、平台 | A/B 剪辑大纲、脚本、粗剪 Variant、CTA | 音乐/生成 B-roll/应用计划确认 |
| 访谈故事线 | 多人转录、主题、禁用话题 | 叙事结构、引用证据、问题缺口 | 无 |
| 多平台改版 | 已批准主版本、目标平台 | 16:9/9:16/1:1 方案、安全区/字幕差异 | 应用新 timeline 确认 |
| 字幕与本地化 | 音频、语言、风格、术语表 | 字幕轨提议、阅读速度诊断、翻译候选 | 覆盖现有字幕确认 |
| 音乐匹配与 ducking | 授权音乐库、节奏、情绪 | 音乐候选、beat map、ducking plan | 应用/生成音乐确认 |
| 效果风格建议 | 参考、项目色彩/效果能力 | 仅使用 Catalog 中 preset 的效果方案 | 应用 effect 确认 |
| 交付质检 | timeline、交付 preset | 缺媒体/响度/字幕/安全区/版权报告 | 无 |
| 新手引导 | 当前页面/选择、用户问题 | 带定位引用的步骤和术语解释 | 仅“显示我在哪里”导航提议 |

### 5.3 Skill 安全与生命周期

- Skill 只能请求声明在 manifest 的工具，不能安装后自动取得网络、写入、导出或生成额度。
- 安装来源分为官方、已验证第三方、本地开发三类；本地开发 Skill 必须有醒目的未验证标识。
- Skill 的 prompt/instructions 也需要版本、hash、迁移和评测；更新后不能改变旧会话已批准结果的解释。
- 每次运行记录 `skill_id/version`、输入摘要 hash、工具调用、产物、成本和审批；不记录 secret。
- 当 Skill 依赖能力/插件/模型不存在时，显示精确降级：例如“仅生成剪辑大纲，无法生成视频”。

## 6. 子智能体与任务图

### 6.1 原则：内部并发，外部可控

子智能体是 Harness 内的有界角色 worker，不是获得独立权限的“第二个用户”。用户看到的是任务计划和可检查产物，而不是混乱的 Agent 聊天瀑布。

可用角色：`creative_director`（Brief/脚本）、`researcher`（资料与引用）、`media_librarian`（素材检索）、`story_editor`（大纲/叙事）、`rough_cut_planner`（TAEP 方案）、`caption_editor`、`audio_director`、`visual_style_advisor`、`qa_reviewer`、`help_guide`。

### 6.2 调度规则

1. orchestrator 先生成显式 `TaskGraph`：目标、依赖、预算、允许工具、预期 artifact、失败策略。
2. 同一可写 timeline 同时至多一个提交候选；只读检索/分析可并行，但有 provider、CPU/GPU、网络和成本并发上限。
3. 子任务获得的上下文仅为完成其角色所需的最小 `ContextSlice`；不继承父任务的所有工具或密钥。
4. 子任务只能产出 typed artifact，例如 `ScriptDraft`、`EvidenceSet`、`EditorialBriefPatch`、`EditPlanProposal`、`GenerationRequest`、`QaReport`。
5. 子任务失败可重试/降级/跳过，但父任务必须报告“未完成什么、对结果有什么影响”；不得编造完成。
6. 取消父任务要取消尚未开始的子任务，并对进行中的 Job 请求安全取消；已成功的 artifact 可由用户保留。
7. 每个 task 有超时、token/成本预算、最大工具调用数和循环检测；超过限制进入 `NEEDS_USER_INPUT` 或 `FAILED`，不能无限自我反思。

### 6.3 典型任务图：从主题到粗剪

```text
用户主题 + 已选素材
  ├─ 创作总监：Brief / 受众 / 交付约束
  ├─ 媒体管理员：检索转录、镜头、授权与缺口
  └─ 故事编辑：文案脚本 / 分镜 / 关键证据
                 ↓
       粗剪规划：A/B EditPlan proposals
                 ↓
       QA 审查：约束、版权、时长、字幕/安全区、能力检查
                 ↓
       用户比较 + Preview Diff + 一次性批准
                 ↓
       CommandStack 原子提交 → 可选预览/导出 Job
```

首版以确定角色和固定图实现；不要先做“模型自己无限创建 Agent”。只有当评估证明计划质量、成本和取消语义稳定后，再允许可选的动态分解。

## 7. 上下文、记忆与压缩

### 7.1 Context Bundle

每个模型调用使用可审计的 `ContextBundle`，包含：系统策略版本、Skill、用户请求、Brief revision、project/timeline snapshot、选择范围、媒体 evidence、上次已批准决策、帮助文档引用、工具 manifest、预算与 provenance。每一块均有 token 估算、优先级、过期规则和可见的“为什么包含”。

优先级：硬约束/用户锁定 > 当前选区/交付目标 > 已批准决策 > 高置信证据 > 最近会话摘要 > 普通历史消息。绝对路径、secret、未授权原始媒体、无关长转录不进入 Bundle。

### 7.2 上下文压缩

| 机制 | 触发 | 保留 | 丢弃/外置 |
| --- | --- | --- | --- |
| 可逆索引 | 默认 | artifact ID、引用、原文可按需取回 | 大转录与大量缩略图不直接塞 prompt |
| 结构化摘要 | 达到 60% 预算或切换任务 | Brief、结论、拒绝/锁定项、未决问题、证据 refs | 冗余闲聊和已被 artifact 覆盖的内容 |
| 用户“立即压缩” | 手动 | 先展示摘要预览与范围 | 用户可取消，不静默丢失保留策略要求的数据 |
| 自动检查点 | 计划/审批/导出前 | 可复现所需 hash、plan、diff、决定 | 不保存完整云 prompt，除非用户开启 |

UI 显示 `当前 8.4k / 32k`、压缩次数、摘要范围、可恢复引用和“是否允许保存完整对话”。只有 provider 返回真实 usage 才显示精确 token/费用；未知时显示“估算”。

### 7.3 记忆范围

- **瞬时会话记忆：** 当前任务消息，工程关闭可按设置清除。
- **工程创作记忆：** Brief、术语表、品牌语气、锁定镜头、决策、variant/audit refs；须版本化且可编辑。
- **用户偏好记忆：** 默认平台、语言、字幕风格、常用工作流；跨工程前必须可见/可关闭。
- **团队/组织记忆（后续）：** 仅在明确账号、权限和数据保留设计后开放；不得把私人工程隐式用于训练或共享。

## 8. 创作、粗剪与生成式媒体路线

### 8.1 从文本到剪辑的标准闭环

1. 用户说目标，或从模板启动工作流。
2. Agent 提出最多 3 个真正影响结果的澄清问题；信息足够时使用可见默认值并生成 Brief 草稿。
3. 创作 Agent 产出文案、脚本、shot list、节奏和大纲；用户可编辑/批准。
4. 媒体 Agent 从已导入素材和已授权分析中找到证据，列出缺失镜头、音乐、音频和风险。
5. 粗剪 Agent 只用 asset ID 和 frame range 生成 A/B/C `EditPlan`；每个关键选择回链证据。
6. QA Agent 运行约束、时长、版权、效果可用性、字幕、响度/安全区预检。
7. 用户选择方案，审阅相同 plan 的 Diff 后批准；软件将其作为一个 Undo 单元提交。
8. Agent 生成短预览，用户可要求“更快、更克制、换开场”，形成新的 Brief revision，而不是对原计划做不可追溯覆写。

### 8.2 视频、音乐、语音与图像生成

生成能力必须通过 provider-neutral adapter，而非把某一厂商 SDK 绑进 UI。

| 阶段 | 交付能力 | 必须条件 |
| --- | --- | --- |
| G0 | 在大纲中标记“缺镜头”，生成可复制的 video/music prompt 和预算估算 | 不调用外部模型，不上传媒体 |
| G1 | 用户确认后创建异步生成 Job，回收多个候选到“生成素材箱” | provider/profile、数据上传提示、成本上限、取消与失败状态 |
| G2 | 用户预览候选并显式“导入到媒体池”；导入时写 provenance/许可/生成参数摘要 | 引入媒体必须走既有 Media Pool / project IO |
| G3 | Agent 在 Diff 中建议把候选放入 timeline；用户确认后应用 | 不自动替换已有镜头；每次是 plan/undo 单元 |
| G4 | 支持可控 B-roll、配音、SFX、音乐 stems、角色/品牌一致性 | 先有权利管理、内容安全、质量评测与可替换策略 |

每个生成 artifact 至少记录：provider/model/version、请求时间、输入来源摘要与 hash、seed/可复现参数（如 provider 支持）、使用的参考素材、授权声明、内容安全结果、费用、用户/审批、父任务/Skill。建议将生成资产以 badge 标为“AI 生成”，并支持导出时附 provenance report/C2PA 兼容信息（能力允许时）。

#### 8.2.1 生成配方（GenerationRecipe），不是“一个 prompt + 一个 API 按钮”

`GenerationRequest` 需升级为版本化的 `GenerationRecipe`。Agent 先从 Brief/Shot List 生成 Recipe 和成本预估，用户审阅后才由 Provider Pack 编译为厂商请求。这样新增厂商或模型不会改写编辑器 UI、Skill 和时间线逻辑。

| Recipe 区块 | 最小字段 | 约束 |
| --- | --- | --- |
| 创作意图 | shot/beat id、镜头目的、主体、动作、景别、运动、情绪、节奏、提示词及版本 | 属于可审阅的创作 artifact；用户修改优先于 Agent 文案。 |
| 输出合同 | modality、画幅、分辨率、目标时长/FPS、是否需要 alpha/音频、交付 profile | Provider 无法满足时返回 capability diagnostic，不能偷偷降级或拉伸。 |
| 参考资产 | `ReferenceBinding(asset_id, role, range, consent_scope)`；role 为 image/style/first_frame/last_frame/video/audio/character/product | 引用每一个用户素材均需单独显示上传范围、时段、是否出云与过期策略；不能只传裸 asset id。 |
| 可控参数 | seed、候选数、negative constraints、镜头控制、延长/重绘/超分等通用意图 | `provider_options` 必须以 Pack schema 进行 namespace 隔离、验证并记录版本；Agent 不得发任意 provider JSON。 |
| 治理 | provider/profile 候选、预计成本/时长、预算、region、许可/肖像/安全要求、审批 scope | 预估与实际费用、模型版本、数据上传清单、审批人均写入 provenance/audit。 |

首批只需暴露“文生视频、图生视频、参考视频、参考音频、首尾帧、延长、配音、音乐、图像”这些**语义能力**；某一 Provider 不支持时以 capability matrix 降级。以 Seedance 为例，官方资料说明其支持文字、图片、音频、视频多模态参考输入，并且有肖像与版权控制，因此适合作为 Video Provider Pack，而非把一个固定请求体塞进 Assistant。[Seedance 2.0 API 服务说明](https://developer.volcengine.com/articles/7628567056649125942)

#### 8.2.2 Provider Pack 与接入顺序

真实 Provider 必须是可安装/可禁用的 Pack，实现同一受控生命周期：`validate recipe → prepare upload → submit(idempotency key) → poll/resume → cancel → verify output → materialize`。通用 HTTP adapter 只能作为 transport 基础，不能假设第三方 API 都遵循同一种路径、认证、上传、异步状态或输出结构。

| Pack | 首批职责 | 不应由其承担的职责 |
| --- | --- | --- |
| Seedance Video Pack | 检查模型/地域/能力；上传已获授权的参考图、视频、音频；映射多模态 Recipe；保存远端 job checkpoint；获取并下载结果 | 编辑 timeline、决定镜头 placement、绕过肖像/版权确认、把远端 URL 当永久资产。 |
| Image Pack | 文生图、编辑/重绘、角色/产品参考图输出；将获选图升级为 `CreativeAsset` | 把未审批的图片覆盖工程内已有素材。 |
| Voice/TTS Pack | 声线/语言/时长/字幕对齐、SSML 或等价受控参数、响度和可用许可信息 | 克隆/使用声纹前绕过单独授权。 |
| Music/SFX Pack | 音乐/SFX/stem/loop 生成或检索、BPM/key/duration、许可和 loop 边界 | 自动混音、替换授权音乐或将未知许可音乐标为商用。 |

每个 Pack 还必须提供：连接测试、实时 capability snapshot、请求/并发/速率限制、可重试错误分类、模型弃用提示、数据保留期与地域说明、故障 kill switch、确定性 mock fixture。官方文档表明 Seedance/Seedream 结果通常进入对象存储产物桶，因此 `materialize` 必须下载、hash、probe 和纳入项目素材管理；工程不能只保存远端临时地址。[生成内容下载与对象存储说明](https://docs.volcengine.com/docs/85889/2479142?lang=zh)

#### 8.2.3 生成素材从候选到可剪辑资产

```text
Shot Plan / GenerationRecipe
        ↓（用户确认上传、成本、许可与 Provider）
Provider Pack Job → Generation Bin（候选，不改工程）
        ↓                 ↓
  provider safety     technical / temporal / brand QA
        ↓                 ↓
   用户比较/选中 → materialize（download + hash + probe + proxy + thumbnail）
        ↓
Media Pool asset → Agent 提议 EditPlan → Diff / Approval / Undo
```

质量门至少检查：可下载和 hash、编码/时长/FPS/画幅、黑帧/冻结/闪烁、音画/响度、关键文字与安全区、品牌色/Logo/产品或角色 reference 一致性、许可证/肖像/安全结果、代理与预览可生成。评分只影响推荐排序；用户仍可预览、保留、删除或手动导入候选。

#### 8.2.4 跨镜头一致性、Agent 编排与用户偏好

Agent 不应对每个缺镜头重新“自由抽卡”。建立版本化 `CreativeAssetRegistry`：`CharacterAsset`、`ProductAsset`、`SceneAsset`、`StyleKit`、`BrandKit` 和 `ReferencePackage`。Shot Plan 以这些资产引用驱动各 Recipe；镜头/角色/产品发生变化时，Agent 明确列出受影响 Recipe 并请求重新批准。

用户接受/拒绝候选、镜头节奏、品牌色、字幕、音乐偏好应沉淀为可检查的 `PreferenceProfile` / Brand token，而非把完整历史聊天无筛选地塞进上下文。生成工作流应是“识别缺镜头 → 写 Shot Plan/Recipe → 生成多个候选 → QA/用户选择 → 导入 Media Pool → 提议时间线 Diff”，不是“生成成功即自动插入或替换镜头”。

### 8.3 滤镜、转场与调色

Agent 不应生成任意 GLSL 或绕开 Color/Effect 模型。正确路径是：读取 Capability Manifest → 在 Effect Catalog 的带 schema preset 中提议 → 在 Diff 中展示受影响 clip、参数、预览/导出可用性与降级风险 → 用户批准后通过 command 应用。未来可以让受信任的开发者 Skill 创建插件草稿，但这属于开发工具链，不能与普通剪辑 Agent 权限混用。

## 9. 软件使用助手

### 9.1 知识来源与回答要求

知识库来源优先级：当前版本内置 help/tooltip/快捷键/功能 manifest → 官方项目文档与发布说明 → 用户已打开工程的脱敏状态 → 明确标注的在线官方资源。回答必须带功能版本/可用性、相关页面/菜单、快捷键和具体下一步；找不到时说“不确定”并建议搜索或反馈，而不是编造。

### 9.2 典型能力

- “我如何给片段加关键帧？”：解释、定位页面、展示步骤和当前选中对象是否符合前置条件。
- “为什么导出失败？”：读取结构化诊断和 job 日志摘要，给出最短修复路径；不要求用户复制绝对路径/secret。
- “新手模式做一条竖版短片”：以教程任务清单引导素材导入、选区、粗剪、字幕、导出，每一步由用户确认。
- “这里有什么功能？”：基于当前 panel、feature flag 和 capability manifest 输出搜索结果；不可用功能明确标为未安装/未启用。

初版只做“解释 + 高亮/导航提议”；若以后加入 UI 导航工具，必须是受限、可见、可取消的 `show_panel`/`focus_timeline_range` 一类语义动作，不能让模型模拟全局鼠标键盘。

## 10. 数据、审计、隐私与成本

### 10.1 最小数据模型

| 对象 | 核心字段 |
| --- | --- |
| `AssistantSession` | project/timeline scope、标题、profile refs、retention、状态、checkpoint refs |
| `Task` / `TaskGraph` | parent、role、state、budget、tool policy、input/output artifact refs、error/cancel reason |
| `SkillManifest` | id/version/author/source/signature、schemas、tool permissions、model requirements、license、eval version |
| `ContextBundle` | item refs、token estimate/actual、priority、provenance、compaction lineage、expiry |
| `GenerationRecipe` / `GenerationRequest` / `GeneratedAsset` | shot/intent、output contract、reference bindings、provider options schema/version、policy/consent、estimated/actual cost、job/checkpoint/asset refs、license/provenance/safety/materialization |
| `CreativeAsset` / `ReferencePackage` / `PreferenceProfile` | 角色/产品/场景/风格/品牌 token、可用 reference asset、授权范围、版本、用户接受/拒绝的可解释偏好 |
| `ApprovalGrant` | action、project/timeline/revision/plan/diff hash、scope、expiry、single-use/actor |
| `AgentAuditEvent` | task/skill/profile/model/tool、summary hashes、decision、cost/time, diagnostics, artifact refs |

所有项目 payload 有 schema version、迁移、未知字段保留策略；所有 secret 继续仅在系统 keychain。用户需要清楚选择：本地保存什么、是否保留云对话、是否允许上传转录/缩略图/代理媒体、是否允许遥测。

### 10.2 成本、配额与滥用保护

- 每个 profile 有单请求、会话、每日/月度 token 和货币限额；生成视频/音乐另有硬上限。
- 生成预算同时限制候选数、重试数、并发 Job、单镜头/单任务最长时长、上传字节数和 provider 轮询时长；预估费用与 provider 返回的实际费用必须分开记录，超限立即阻止后续提交。
- fallback 只能在用户已确认的 provider/region/上传策略集合内选择；不得因为主 provider 限流或失败而自动把素材上传给另一家服务。
- Job 启动前显示估算区间，超预算改为等待确认；完成后显示实际/估算消耗和可复制账单摘要。
- 对每个任务限制最大轮数、工具调用数、子任务数、并发、重试、输出大小与时长；异常循环自动停止。
- 网络请求默认只到 profile/MCP allowlist 域名；日志脱敏，错误页不显示 API key、请求头或绝对路径。

## 11. 分阶段实施计划与验收表

### 11.1 唯一主实施路径（M0–M12）

原文中的 H/A/S 编号保留在后文用于说明能力来源，但**不再单独作为开发顺序**。对应关系为：`M2 = A0/S0`、`M3 = A1/S1`、`M4 = A2/S2`、`M8 = A3/S3`、`M9 = A4/S4`、`S5 = M12 的质量门`。

| 顺序 | 里程碑与用户可见结果 | 必须先具备 | 绝对不要做 | 退出条件（全部满足才能继续） |
| --- | --- | --- | --- | --- |
| M0 | **工程基线与范围冻结**：开发者能运行 AI/edit 测试，产品明确首发只服务口播/访谈/产品短视频。 | 当前工程、fixture、目标平台/语言/本地或云策略。 | 不先接模型、MCP 或 UI 大改。 | 建立功能开关、测试命令、性能基线、错误/隐私验收清单；首发范围写入产品配置。 |
| M1 | **安全编辑闭环**：AI 提议可 Preview，用户批准后一次提交并一次 Undo。 | M0；现有 TAEP、CommandStack、ApprovalService。 | 不让 Agent、脚本、MCP 或 UI 直接改 timeline。 | plan/diff/revision/hash 绑定；跨工程、过期、重复、取消批准都无法写入；Undo/Redo 同步 timeline、project、renderer。 |
| M2 | **分析数据地基**：每个素材有稳定 ID、fingerprint、PTS/timebase、分析状态和版本化证据合同。 | M1；媒体导入/工程保存链路。 | 不先跑 WhisperX/OCR；不以浮点秒作为唯一时间。 | VFR/CFR fixture 均能从 evidence 回到 source frame；工程重开保留摘要，缓存可重建，素材替换准确变 stale。 |
| M3 | **媒体池可选快速分析**：用户手动点击“分析素材”，获得镜头边界、波形、响度、静音等本地证据。 | M2；Job 基础设施。 | 导入自动分析；在 UI/render 线程解码；未确认时“分析全部素材”。 | 默认导入零 AI 推理；选中素材可排队/取消/重试；播放不被后台任务卡顿；结果在媒体池可见。 |
| M4 | **文字剪辑证据**：用户选择 `editorial_local` 后得到转录、词/段时间码、匿名 speaker、beat，并可按台词搜索。 | M3；最小 Model Pack 下载/许可/硬件检测。 | 同时独立重复跑 Faster-Whisper 与 WhisperX；把 speaker 聚类当实名身份。 | WhisperX/ASR 任务可取消、可降级；每个搜索结果可定位；文本 Agent 不看原始媒体也能基于证据提出剪辑候选。 |
| M5 | **Agent Dock V2 与可检查上下文**：会话、Composer、模型/隐私/权限、分析覆盖率、真实 token/费用和 Job 抽屉可用。 | M1、M2、M4。 | 伪造 token/上下文数；把“分析所有素材”直接执行；保存 API key/完整云对话为默认。 | 用户能查看/排除上下文、取消任务、恢复会话；无工程/无模型/离线/partial/stale 均有明确界面。 |
| M6 | **创作搭档与软件助手**：可编辑 Brief、文案、脚本、分镜、剪辑大纲；可搜索功能和解释诊断。 | M5；本地帮助索引和 artifact contract。 | 把模型回答直接写 timeline；无来源地编造软件功能或事实。 | 每个 artifact 有 revision/来源；帮助结果指出当前版本/页面/前置条件；无证据时明确未知。 |
| M7 | **首个剪辑工作流闭环**：内置“口播精剪、30 秒产品广告、字幕初稿”从证据生成 A/B plan、Diff、批准、Undo。 | M4、M5、M6。 | 先建通用 Skill 市场、子智能体或视觉大模型；将未分析素材伪装为已理解。 | 三个工作流都有 fixture、金标准和失败降级；每一个关键选段带 evidence；应用仍只有一个 Undo 单元。 |
| M8 | **视觉语义分析**：关键帧、OCR、标签/embedding、镜头级聚合与视觉搜索。 | M3；M2 数据合同；最小 Model Pack 管理。 | 每帧 OCR；单帧标签代表整条长素材；让 OCR/字幕文本改变系统权限。 | “产品特写/屏幕文字”等查询能定位 frame；OCR 是 untrusted data；缓存可删/可重建且不膨胀工程。 |
| M9 | **可扩展 Agent 运行时**：模型包生态、正式 Skill registry、受控子智能体和预算/取消策略。 | M7；M8（若 Skill 需视觉）；稳定 ToolFacade/ContextBundle。 | Skill 自动获得系统权限；子智能体无限创建或并行写同一 timeline。 | Skill manifest/schema/eval/权限可审计；子任务只交付 typed artifact；同一 timeline 始终只有一个提交候选。 |
| M10 | **互操作与自动化**：Topos MCP server、`toposctl`、connected host、只读/预览优先的 automation contract。 | M1、M5、M9 的内部工具和 policy 稳定。 | 先开公网 HTTP；让 MCP handler 直接实现剪辑逻辑或读任意路径。 | 外部客户端只能看动态允许的工具；所有 transport 走 ToolFacade/Approval/SafePath；CLI/MCP/UI 结果一致。 |
| M11 | **生成素材箱与生成式剪辑**：视频/音乐/语音/图像候选可按 Recipe 生成、预览、QA、本地化导入媒体池，再通过 Plan 提议入时间线。 | M1、M3、M5、M9；provider 成本/许可/同意策略、Recipe/Pack contract。 | 生成结果直接入 timeline/导出；把厂商 SDK/参数塞进 UI；不记录模型、许可、成本、上传范围和来源。 | Seedance 等 Pack 可独立启停；取消/失败不污染工程；候选经 QA/materialize 入媒体池后才可选择；每个插入都显示 Diff、可 Undo、可导出 provenance report。 |
| M12 | **产品质量与发布**：评测、性能、可访问性、灰度、监控、数据删除、故障开关。 | M0–M11 中实际启用的能力。 | 仅靠 demo 判定完成；采集 prompt/素材作为默认 telemetry。 | 通过安全/回归/压力/无障碍测试；未确认写入和 secret/path 泄露均为零；可逐能力关闭 provider/Skill/MCP。 |

允许的并行仅限于不改变依赖的准备工作：M1 的测试审计可与 M0 同步，M3 的 UI 线框可与 M2 的数据模型同步，M6 的本地帮助索引可与 M5 的纯 UI 组装同步，M8 的离线视觉 fixture 可在 M7 验收时准备。**不能并行**：M3 不能早于 M2，M4 不能早于 M3，M5 的真实 Context 不能早于 M4，M7 不能早于 M4–M6，任何 MCP 写入、子智能体、生成素材都不能早于 M1。

### 11.2 逐步实施手册

#### M0：工程基线与范围冻结

**目标：** 让所有人知道“第一版做什么、不做什么、如何判断改坏了”。

1. 运行现有 edit/AI focused tests，记录通过数、耗时和当前已知失败；准备一组不含隐私的 fixture：口播、访谈、产品 demo、VFR、无音频、长素材、离线素材。
2. 在 feature flag 中定义 `ai_assistant`、`media_analysis`、`cloud_analysis`、`generation`、`mcp_server`；所有默认值为关闭，除了不会联网/写入的纯 UI 可见性。
3. 写下首发成功故事：用户导入素材 → 手动分析 → 输入需求 → 生成 A/B 粗剪 → 看 Diff → 应用 → Ctrl+Z 恢复。
4. 为每个后续模块预先约定 owner、测试文件、性能预算和数据策略；无 owner 的“以后再说”能力不进入当前 sprint。

**难点：** 范围膨胀。若本阶段不锁定“口播/访谈/产品短视频”，后续每个模型和 UI 都会被通用影视需求拖慢。  
**完成检查：** 新人能在不读全部源码的情况下运行测试、解释首发闭环和禁止能力。

#### M1：安全编辑闭环

**目标：** 无论模型多聪明，时间线也只会经用户批准的、可撤销的同一份计划改变。

1. 以 `EditPlan`、`PreviewResult`、`ApprovalGrant`、`CommandStack` 为唯一链路，逐项追踪从 UI/Agent 到 renderer 的调用。
2. 让 Preview 缓存绑定 `project_id`、`timeline_id`、`base_revision`、`plan_hash`、`diff_hash`、`manifest_hash`；任一变化即 stale。
3. 将 AI 提交编译为一个 `MacroCommand`，执行后走现有 project/sequence/renderer 刷新；Undo/Redo 使用同一路径。
4. 写失败测试：伪造 grant、重复 grant、换工程、换 timeline、revision 变化、批准后取消、编译中失败。

**难点：** “能改时间线”不等于“能安全改时间线”。最常见错误是 Preview 与 Commit 使用了不同 state，或 commit 绕过 UI command stack。  
**完成检查：** 任一失败不留下半套修改；一键 Undo 恢复全部状态，而不是只恢复画面或只恢复 project JSON。

#### M2：分析数据地基

**目标：** 先定义证据怎样被保存、检索、失效，再接任何模型。

1. 增加 `AnalysisRequest`、recipe、status、field-level provenance、`EvidenceSegment` 和 schema migration。
2. 导入素材时只创建 `asset_id`、媒体技术 metadata、fingerprint、PTS/timebase；不执行 AI。
3. 原始 provider 时间保留 PTS/timebase/sample offset；经 `TimebaseService` 转换为 source `FrameRange[start, end)`，仅在生成 EditPlan 时映射到 timeline。
4. 将工程 payload、可重建 cache、搜索索引、用户修订分开保存；用户修订永不被再次分析覆盖。

**难点：** VFR、重定时、代理文件和原始媒体的时间坐标不相同；绝不能靠 `seconds * fps` 草率换算。  
**完成检查：** 替换素材、修改分析配方或模型版本后，只有受影响证据 stale；工程中没有大缓存、secret 或绝对路径。

#### M3：媒体池可选快速分析

**目标：** 用户可在不使用 AI assistant 的情况下，从媒体池安全地发起本地轻量分析。

1. 在 `MediaPoolPanel` 添加“启用 AI 素材分析”状态、分析选中/当前 Bin/全部素材菜单、每素材 badge 与取消/重试/删除结果操作。
2. 新建 `MediaAnalysisController`，把 UI 的 `AnalysisRequestDraft` 转为待确认的 `AnalysisRequest`；“所有素材”必须列出范围并二次确认。
3. 把 `AnalysisService.request_analysis()` 的同步过渡路径替换为 JobScheduler worker；先接 Probe、现有 SceneCut、FFmpeg ebur128/silence/waveform。
4. 实现 cache key、进度、取消、项目关闭清理、stale 与 partial 状态；播放/导出优先级高于分析。

**难点：** 把“分析”放进媒体池不是把模型塞进 widget；UI 只发信号，解码与模型必须在 worker。  
**完成检查：** 导入 100 个素材时 CPU/GPU 不发生 AI 推理；选 10 个素材分析可取消，关闭工程后无 orphan worker。

#### M4：文字剪辑证据

**目标：** 让非多模态文本 Agent 也能可靠地按台词、说话人、节奏提出粗剪。

1. 做最小 `ModelPackManager`：显示许可证、体积、硬件需求、hash、下载/删除；不把大模型加入基础安装。
2. 实现 `WhisperXTranscriptProvider`，优先输出 segment/word timecode、语言、置信度；没有对齐模型的语言降级并标记精度。
3. 将 diarization 作为 WhisperX 内部能力或独立精修 provider，合并前校验时间范围与重叠语音；speaker 仅命名为匿名 `S1/S2`。
4. 接入 beat、响度、静音和 `SemanticIndex` 搜索；在媒体池/上下文卡显示 coverage、provider 与错误。

**难点：** 模型许可证、受限权重、GPU/CPU 依赖与词级对齐的语言覆盖；这些必须在安装和结果中明确，而不是静默降级。  
**完成检查：** 用户搜索一句台词可定位 source；Agent 生成的每个口播选段有 evidence reference；没有证据时明确请求分析或说明限制。

#### M5：Agent Dock V2 与上下文

**目标：** 把当前输入框升级为可控、可追溯、不会误导用户的工作台。

1. 先接 session 列表、Composer、Profile/隐私/权限胶囊、Job 抽屉与结构化消息卡；密钥设置仍留在 Preferences。
2. 实现 `ContextBundle`：素材选择、Brief、证据、锁定项、决策、token 预算、引用、过期状态都可检查/排除。
3. `分析所有素材` 只调用 proposal 工具，弹出范围、recipe、模型、费用/上传、预计时间确认卡；确认后才进入 M3/M4 的同一 scheduler。
4. 实现会话检查点与压缩：保留 Brief、锁定项、决策和 evidence refs；默认不保存完整云 prompt。

**难点：** 不要把对话历史直接拼成 prompt；上下文必须是有来源、可预算、可删除的 `ContextBundle`。  
**完成检查：** 用户能解释本次模型看到了什么、为何、花费多少；无工程、离线、partial、stale、无模型等状态不会出现空白或假成功。

#### M6：创作搭档与软件使用助手

**目标：** 在不写时间线的前提下，为用户提供立即可用的创作和学习价值。

1. 为 Brief、脚本、分镜、shot list、交付大纲建立 typed artifact、revision 和编辑卡；用户修改永远优先于模型结果。
2. 建立本地 help index，从 i18n、功能 manifest、快捷键、诊断和官方 docs 生成可引用的帮助结果。
3. 实现 `search_help`、`explain_diagnostic`、`show_relevant_panel` 等只读/导航提议；导航动作可见、可取消，不能模拟鼠标键盘。

**难点：** 帮助 Agent 不能以“听起来合理”的回答替代真实功能状态；答案必须带页面/版本/前置条件或明确未知。  
**完成检查：** 即使未配置 AI 模型，用户仍能使用本地帮助与手工 Brief；模型回答不能改变工程。

#### M7：首个剪辑工作流闭环

**目标：** 用三个内置、确定性的工作流验证整套链路，而不是先做泛化 Skill 平台。

1. 依次实现“口播精剪”“30 秒产品广告”“字幕初稿”，每个工作流都是固定输入 schema、输出 artifact/plan、质量检查与降级策略。
2. 每个 workflow 只经 `search_assets`/`get_asset_analysis` 取证据，产出至少 A/B Variant；没有分析证据时只能提出分析或给低置信度草稿。
3. 对每个 variant 跑 validator、Preview 和 Diff；用户可选、拒绝、锁定、重规划，最终提交走 M1。
4. 为每个工作流建立 golden fixture、人工评分 rubric、性能/成本阈值和失败样本。

**难点：** 不要把“prompt 模板”误称为 Skill 或 Agent；首发工作流必须先证明结果可解释、可验证、可撤销。  
**完成检查：** 新用户可完成首发成功故事；所有关键剪辑点能回到原素材证据，Ctrl+Z 一次恢复。

#### M8：视觉语义分析

**目标：** 在文字剪辑稳定后，增加 B-roll、产品镜头和屏幕文字检索，而不牺牲性能和隐私。

1. 按 shot 首/中/尾抽关键帧，去重并建立可重建 keyframe manifest；不要全片逐帧跑 OCR/视觉模型。
2. 接入 RapidOCR、视觉 tag/caption/embedding provider 与质量信号；对多个 keyframe 进行 shot-level 聚合。
3. 扩展 SemanticIndex，支持文字、标签、镜头类别和相似度检索；所有结果显示 frame、置信度、provider。
4. OCR/字幕/网页文字作为 untrusted data 传给 Agent；用户可关闭 OCR、人像等敏感分析。

**难点：** 单帧幻觉、重复帧、embedding 大缓存和模型 GPU 竞争。  
**完成检查：** 搜索“产品特写/屏幕文字”能定位证据；关闭或删除分析后索引/缓存同步消失且工程仍可打开。

#### M9：Skill Runtime 与受控子智能体

**目标：** 将已验证工作流产品化为可版本化 Skills，并只在确有收益时引入并发角色。

1. 将 M7 工作流提炼为带 manifest、版本、输入/输出 schema、工具 allowlist、风险、UI、eval 的官方 Skill。
2. 完成 Skill 安装、禁用、升级、来源/许可证/签名、feature flag；第三方 Skill 初始只读或 proposal 权限。
3. 实现固定角色的 `TaskGraph`（创意、素材检索、故事、QA），每个子任务最小上下文、预算、超时、artifact type 和取消传播明确。
4. 写入 policy：同一 timeline 同时最多一个提交候选，子智能体永远没有独立 approval。

**难点：** 扩展和子智能体最容易绕过宿主安全边界；先让所有角色共享同一 ToolFacade/Policy，再谈并行。  
**完成检查：** 任一 Skill/子任务都可追溯到版本、工具和 artifact；取消不会留下后台任务或未批准写入。

#### M10：MCP 与自动化 Harness

**目标：** 把已稳定的内部能力安全地提供给外部客户端、CLI 和可信自动化。

1. 先完成 transport-neutral JSON schema/property tests；内部仍以 `ToolFacade` 为唯一实现。
2. 提供只对当前用户开放的 stdio/local IPC MCP server，默认只读/preview；动态依据 project、feature、policy 暴露工具。
3. 实现 `toposctl` 的 read/validate/preview，后续才接 connected commit；所有写动作由 GUI/ApprovalService 签发 grant。
4. 外部 MCP client 采用 allowlist、临时授权、域名和数据范围提示；不实现公网监听。

**难点：** MCP 只是 transport，不是权限系统、更不是多模态能力；不可在 adapter 里复制剪辑业务。  
**完成检查：** 相同请求经 UI/CLI/MCP 得到相同 diagnostics；外部客户端不能读取路径、密钥或越权写入。

#### M11：生成素材箱与生成式剪辑

**目标：** 让视频/音乐/语音/图片生成成为可选择、可比较、可验证、可追溯的素材，而不是不可追溯的自动修改或厂商 API 按钮集合。

1. 先实现版本化 `GenerationRecipe`、`ReferenceBinding`、用户同意、成本上限、Job、取消、provenance 和内容安全结果；将输出/时长/FPS/画幅/参考素材角色写成显式合同。
2. 实现 Provider Pack contract 和确定性 mock。首个真实 Pack 可为 Seedance Video：能力发现、授权素材上传、多模态映射、异步 job/checkpoint/resume/cancel、输出下载；图像、TTS、音乐/SFX 均走同一 contract，不把 SDK 放进 UI/Agent。
3. 所有候选先进入“生成素材箱”；运行 provider safety 与 technical/temporal/brand QA。用户预览后显式 materialize（下载、hash、probe、proxy、thumbnail）并导入 Media Pool，才成为普通 `MediaAsset`。
4. 建立 `CreativeAssetRegistry` 与 `ReferencePackage`，使角色、产品、场景、风格、Brand token 能跨 Shot Plan/Recipe 复用；修改 reference 或品牌资产时能列出受影响镜头。
5. Agent 若建议放入 timeline，只能生成包含已导入生成资产 ID 的 EditPlan，仍经 Preview/Diff/Approval/Undo；不得自动替换既有镜头。
6. 导出时可产生 provenance report；许可未知、肖像授权缺失、风险未处理或 QA 阻断时不能声明“商用就绪”。

**难点：** 成本和版权风险不在模型质量本身，而在“生成后被静默使用”；而多镜头生产的真正难点是 reference、一致性、素材本地化与可剪辑质量，不是单次 API 返回成功。
**完成检查：** 取消/失败不改工程；删除生成候选不影响原素材；远端 URL 失效后已导入资产仍可重开工程；每个使用位置、模型/Pack 版本、参考素材上传范围、实际费用、用户批准和 QA 结论可追溯。

#### M12：质量、灰度与发布

**目标：** 将可演示的能力变成可靠产品。

1. 执行安全、编辑、Job、上下文、UX、Skill/MCP、生成素材和性能测试矩阵；加入 VFR、长转录、1000 clip 和离线/低显存 fixture。
2. 建立离线 eval 与人工 rubric，量化证据定位率、Preview 成功率、一次 Undo 正确率、任务取消残留率、成本/时长和帮助引用正确率。
3. 增加 opt-in 脱敏 telemetry、feature flags、provider/Skill kill switch、数据导出/删除、可访问性检查与故障演练。
4. 先内测 deterministic/local profile，再灰度云 profile，最后分别灰度 MCP、第三方 Skill 和生成 provider。

**难点：** 真实项目的长素材、弱硬件、断网和用户中途编辑比 demo 更容易暴露竞态。  
**完成检查：** 所有启用能力都有 kill switch；未确认写入/secret 泄露为零；关键质量指标达标且普通编辑器不依赖 AI 才能运行。

### 11.3 能力清单（参考，不作为排期）

下表的 `P0/P1/P2` 是旧的优先级标签，只用于定位模块和测试，**不得**理解为可脱离 M0–M12 独立开工。例如表中的 MCP 项必须在 M10 实施，生成项必须在 M11 实施。

| 优先级 | 任务 | 主要文件/边界 | 测试重点 |
| --- | --- | --- | --- |
| P0 | 将 `AiEditAssistantPanel` 重构为 composer + session/状态容器，组合已有 context/variant/diff/job 子组件 | `src/features/edit/ui/`；只发 typed signal | UI 状态回填不递归、窄宽度、中文长文本、键盘焦点。 |
| P0 | 建立 `AssistantSession` 列表与 project-local payload；历史与原始对话分离保存 | `assistant_session_store.py` 扩展 | 重开恢复、删除/归档、无 secret/path、schema migration。 |
| P0 | 建立 `ContextBundle` 与真实 usage adapter | 新 service，不在 widget 拼 prompt | token 预算、脱敏、压缩、过期、provider 不返回 usage 的降级。 |
| P0 | 统一 agent policy/approval/cost gates | orchestration/application service | 伪造 token、越权工具、额度、取消、stale revision。 |
| P1 | 产出 Brief/Script/Outline/ShotList typed artifacts 与编辑器卡片 | creative domain；不可直接写 timeline | artifact revision、引用、用户编辑、i18n。 |
| P1 | 建立 help index 与 `topos.search_help`/`topos.explain_diagnostic` | docs/help registry；只读工具 | 版本差异、不可用 feature、来源引用、无网络时可用。 |
| P1 | Skill manifest/registry 与前三个官方工作流 | extension boundary | 签名/来源、schema 验证、权限差异、golden eval。 |
| P1 | `TaskGraph`/子智能体 coordinator | service/job boundary | 依赖、限额、取消传播、单写者、artifact type validation。 |
| P1 | MCP server（stdio/local IPC） | thin transport adapter | schema/property tests、auth、动态 manifest、注入隔离。 |
| P2 | 生成媒体 provider adapter 与素材箱 | job/media pool/project IO | 用户同意、成本上限、provenance、取消、导入/删除。 |
| P2 | 效果/转场/字幕/音乐 Skill 与编辑 QA | capability registry + plans | 插件缺失、授权不足、preview/export 一致性、undo。 |
| P2 | 观测、评测、灰度发布 | opt-in telemetry/evals | 任务成功率、误提交为零、质量评分、性能与隐私检查。 |

## 12. 质量门、评测与发布

### 12.1 自动化测试矩阵

- **安全：** prompt injection、MCP 恶意输出、越权 tool、路径逃逸、伪造 grant、重放、过期、跨工程/跨 timeline、secret/redaction。
- **编辑语义：** variant → preview → approve → commit → undo/redo；revision 竞争；多操作原子性；effect/caption/music/生成素材在 preview/export 的一致性。
- **Job：** 并发、取消、重试、断网、provider 限流、项目关闭、GPU/CPU 资源清理、恢复与 orphan job。
- **上下文：** 超长转录、1000 clip、压缩、引用恢复、数据策略、token/成本统计不一致。
- **UX/无障碍：** 空项目、无 profile、离线、中文长文本、屏幕阅读器、键盘、窄 Dock、错误可复制、用户确认可理解。
- **Skill/MCP：** schema fuzz/property tests、未安装能力、版本迁移、第三方来源、allowlist、JSON transport。
- **生成：** Recipe schema/迁移、reference 角色与上传 scope、Pack capability/错误映射、幂等提交、轮询/resume/cancel、限流/fallback 不越过同意范围、重复/候选管理、授权未知、QA 阻断/人工保留、materialize 的 download/hash/probe/proxy、导入/删除、provenance report、预估/实际成本保护。

### 12.2 离线评测集

建立不含私人素材的 fixture：访谈、产品 demo、Vlog、多语言字幕、坏转录、无授权音乐、缺镜头、复杂时间线、离线媒体，以及带首尾帧/参考图/参考视频/参考音频的生成 Recipe、远端 URL 失效、黑帧/闪烁/画幅不符、角色/产品 reference 漂移、provider 限流和费用超限。每个 Skill/子任务有输入、期望 artifact、禁止行为和可测指标。

核心指标：Brief/大纲通过人工评分、证据可定位率、计划预览通过率、用户批准后一次成功率、一次 Undo 正确率、任务取消残留率、帮助答案引用正确率、每任务成本/时长、生成素材采纳率、生成候选 QA 误拦截/漏拦截率、跨 Shot reference 一致性评分、远端结果 materialize 成功率。安全指标必须是硬门槛：**未经确认的写入 = 0**、泄露 secret/路径 = 0。

### 12.3 发布策略

1. 内部 fake/deterministic planner → 本地模型 → 受控云 Profile → 可选生成 provider，按 capability 逐步开启。
2. 每个新 Skill/MCP/生成 provider 使用 feature flag、版本化 manifest、灰度用户组、额度和 kill switch。
3. 默认 opt-in telemetry，只上传匿名事件/耗时/错误码；不上传素材、转录、prompt、路径或密钥。
4. 遇到 provider/安全/质量问题时关闭高风险 capability，不破坏普通编辑器和已保存工程。

## 13. 首个可交付版本定义（Release 1 = M0–M7）

Release 1 不追求“万能 Agent”，而是严格完成 M0–M7 的可信闭环。实际工期取决于现有 H0 缺口、目标硬件和团队人数；只有 M0 的基线审计完成后才估算排期，不能先承诺固定周数。

1. 媒体池默认不分析；用户手动启用本地快速分析，并可取消、重试和查看 stale/partial 状态。
2. 选择 `editorial_local` 后，可得到可搜索的转录、匿名说话人、镜头、静音、响度和节拍证据。
3. 新会话/历史、模型/隐私/权限、ContextBundle、真实 Composer 与 Job 抽屉可用。
4. 用户输入需求 → Brief 草稿 → 可编辑脚本/剪辑大纲 → 由证据支撑的 A/B 粗剪方案。
5. 3 个内置工作流：口播精剪、30 秒产品广告、字幕初稿；每个都有 fixture、质量检查和降级。
6. 方案先呈现 Timeline Diff；用户一次确认后以单个 Undo 单元应用；取消、过期、换工程或失败均不留下修改。
7. 本地帮助可以搜索功能、解释当前选择和常见诊断；不接生成式媒体、第三方 MCP client、通用 Skill 市场或动态子智能体。

这版能让用户真实完成“不会剪 → 问软件怎么做 → 手动分析素材 → 写出脚本/大纲 → AI 根据证据给粗剪方案 → 看 Diff 后应用”的体验；后续才按 M8–M12 增加视觉搜索、Skills、子智能体、MCP 和视频/音乐生成。

## 14. 决策清单（开工前由产品确定）

1. 首发人群是否锁定“口播/访谈/产品短视频”，避免一开始承诺通用影视自动剪辑？
2. 允许的模型范围：仅本地，还是可选云；若可选云，默认能上传哪些数据（仅 metadata、转录、缩略图、代理媒体）？
3. 原始对话的默认保留期限和用户可导出/删除策略是什么？
4. 官方 Skill 的分发、签名、许可证和审核责任由谁承担？第三方 Skill 是否首发支持？
5. MCP 首发是只提供 Topos server，还是也允许连接外部 MCP server？后者必须先定义 allowlist 与网络/数据策略。
6. 生成媒体首发 provider、地域/版权/内容安全政策、成本包络和失败退款/提示策略是什么？
7. 是否允许某些本地受信任自动化 profile 跳过单次预览确认？建议首发答案为否，只有只读/建议可免确认。

## 15. 产品化补全清单（第二轮盲区检查）

本章不是要求首版全部实现，而是防止后续把 Agent 只做成“会聊天、会调用几个工具”的孤岛。每一项都应按 capability、权限、数据策略、审计、测试和 UI 反馈进入路线图。

### 15.1 创作与素材工作流补全

| 能力 | 用户场景 | Agent/系统应产出 | 实施约束与阶段建议 |
| --- | --- | --- | --- |
| 创作模板与 Brand Kit | 系列栏目、品牌广告、固定口播格式 | 品牌语气、禁用词、字幕/颜色/片尾/音乐策略、交付 preset | 作为版本化工程/用户资产；只有 effect/preset 引用，不允许模型自由改品牌源文件。P1。 |
| Research Brief | 先做竞品、受众、平台趋势或事实核验，再写脚本 | 带来源和日期的研究笔记、claim 清单、待核查项 | 联网检索必须单独授权、列出域名/内容范围；检索结果是不可信数据，不能变成工具指令。P2。 |
| 事实与品牌合规检查 | 商品参数、医疗/金融/法律敏感表述、竞品提及 | claim/evidence 对照、风险级别、需要人工法务审核的段落 | 只能提示，不能宣称法律/广告合规已保证。P2。 |
| 素材采集与导入向导 | 用户不知道应拍什么、如何整理文件 | shot checklist、命名建议、用户选择目录后的 import proposal、重复/离线素材报告 | 目录访问必须由用户在文件选择器明确授予；导入同样走 project command、可撤销。P1/P2。 |
| 智能素材管理 | 大量素材找不到、有重复、跨项目复用 | 镜头/人物/地点/产品/台词/情绪/质量标签、相似镜头、collections | 分析结果要有 provider/version/置信度、可纠正；人脸、生物特征需独立开关。P2。 |
| 质量与可用性评分 | 模糊、抖动、噪声、过曝、口型/画面问题 | 技术质量信号与候选排序，不把主观评分当事实 | 明示为辅助评分；原始视频理解不足时要求用户预览。P2。 |
| Storyboard 与审片 | 给客户/团队先确认故事，再进入精剪 | 可分享的脚本、分镜、镜头证据、variant、批注和批准记录 | 先本地导出/工程内 review；对外分享、账号协作和水印另设权限。P2。 |
| 版本、分支与回滚 | 同一片子做多种创意/客户版本 | Variant 关系图、命名、diff、比较播放、恢复点 | 不能把 Agent session history 当项目版本控制；仍以 canonical project/revision 为准。P1。 |
| 用户反馈学习 | 用户常说“太快”“不要这类镜头” | 显式偏好、拒绝理由、可编辑的个人/品牌规则 | 只保存用户确认的偏好；不得把隐性推断直接跨项目长期记忆。P2。 |

### 15.2 剪辑、后期与交付补全

| 能力 | 最小可交付 | 关键风险/前置 |
| --- | --- | --- |
| Transcript-based editing | 以句子/说话人/停顿为单位检索、选择和生成粗剪计划 | 词级时间码、多人说话人置信度、字幕锁与人工校对；不可直接改原始音轨。 |
| 多机位与同步建议 | 基于时间码/音频峰值给出同步候选、角度选择建议 | 先有正式 multi-cam canonical command、sync diagnostics 和 preview；不能只靠模型文本移动 clip。 |
| B-roll、J/L cut、节奏建议 | 在 existing media 中提议覆盖镜头/音频衔接/节奏变化 | 每项关联 source evidence、受影响范围和 Diff；不把“建议”伪装为已应用。 |
| 字幕、翻译、配音 | 字幕草稿、术语表、翻译候选、TTS 配音候选 | 语言/语速/可访问性检查、发音审阅、声音肖像/授权同意、角色分离。 |
| 音频后期 | 自动 ducking、噪声/静音候选、响度预检、SFX/音乐建议 | 音频处理必须进入既有音频图和 preview/export 语义；生成音乐须来源、许可和 stems 标注。 |
| 调色与风格匹配 | 基于 ColorStack/effect preset 的建议和对比预览 | 不生成任意 shader；HDR、肤色、广播/平台规范与原始素材保护。 |
| 可访问性交付 | 字幕、音频描述、闪烁/可读性/安全区检查 | 可访问性报告是质量提示，最终合规仍需人工判断与地区规则。 |
| 平台化发布包 | 竖版/横版、封面、标题/描述/标签、章节、不同码率导出建议 | “发布”与“导出”分离；第三方账号、排程、覆盖和公开可见性需最终确认。 |
| 客户审阅与批注 | 基于时间码的评论、任务、批准/退回 | 多人协作、权限/冲突与云同步是独立产品阶段，不能借 AI 会话私自共享项目。 |

### 15.3 交互、可访问性与可靠性补全

| 领域 | 要求 |
| --- | --- |
| 语音与无障碍输入 | 可选语音输入、快捷命令、屏幕阅读器 label、完整键盘操作、高对比、动态字体、色盲安全状态；语音录入和上传遵循独立同意策略。 |
| 多语言/术语 | UI、Skill、帮助、脚本、字幕和提示词分别 i18n；支持项目术语表、品牌禁用词、单位/时码/地区格式，不能以机器翻译覆盖用户锁定字幕。 |
| 渐进披露 | 新手显示引导式“下一步”，专家可打开工具调用、上下文、Diff、预算和 diagnostics；同一功能不应因用户等级改变安全边界。 |
| 离线与降级 | 无网络/模型不可用时保留本地帮助、规则规划、已缓存分析和手工编辑；清楚显示哪些结果来自缓存、可能过期。 |
| 失败恢复 | 每个任务可从检查点恢复、复制诊断、重试/降级/放弃；崩溃后检测 orphan job 和未完成导入，不留下半个工程修改。 |
| 性能资源 | 分离 UI、CPU 分析、GPU 渲染、网络、生成 provider 队列；设置资源预算与后台优先级，实时播放永远优先于低优先级分析。 |
| 透明性 | 对模型回复显示来源、Skill、工具、模型/版本、token/费用估算、运行时间和不确定性；不展示或保存隐性思维链。 |
| 数据可携带与删除 | 用户能导出/删除会话、偏好、审计和生成资产元数据；项目删除或权限撤销时遵循清晰的保留策略。 |

### 15.4 平台、扩展与运营补全

| 领域 | 需要预留的能力 | 约束 |
| --- | --- | --- |
| Provider 路由 | 按文本、工具调用、视觉、音频、成本、地区、隐私、延迟选择模型；支持 fallback 和固定模型重放 | 结果记录实际 provider/model/version；不能因 fallback 偷偷扩大上传范围。 |
| Extension/Plugin 生态 | Skill、MCP adapter、分析 provider、生成 provider、发布 connector 各自有 manifest、版本、来源、许可、禁用和兼容策略 | 不把它们混成“任意 Python 插件”；高风险扩展需审查和 kill switch。 |
| 配置同步 | Profile 仅同步非 secret 配置；组织策略、Brand Kit、Skill allowlist 可由管理员下发 | 用户项目数据、密钥、会话正文和生成输入不能被隐式同步。 |
| 组织治理（后续） | RBAC、审批人、保留期、费用中心、审计导出、DLP、Sso/SCIM | 面向团队的功能不可反向削弱个人本地模式的隐私默认值。 |
| 事件与可观测性 | OpenTelemetry/结构化日志、task/job trace、模型/工具耗时、成本和错误码 | 默认 opt-in，字段脱敏；审计与遥测分离，避免把 prompt/媒体送入 analytics。 |
| 安全响应 | Provider/MCP/Skill 撤销、版本禁用、项目级 kill switch、漏洞通报与迁移 | 能快速关闭生成/网络/第三方工具，同时普通编辑器仍可打开工程。 |

### 15.5 需要持续维护的风险登记表

| 风险 | 早期信号 | 控制措施 |
| --- | --- | --- |
| Agent 幻觉/过度自信 | 没有 evidence 仍声称看见素材内容 | 证据强制引用、置信度、未分析提示、人工预览入口。 |
| 工具循环/成本失控 | 重复工具调用、同一任务反复失败 | task/tool/token/cost 上限、幂等 key、循环检测、人工接管。 |
| Prompt injection | OCR/字幕/MCP 文本要求忽略规则 | trust label、隔离 prompt 通道、schema/allowlist、红队评测。 |
| 版权和肖像风险 | 未知音乐、生成参考来源不清、声音克隆 | license/provenance/consent 字段、阻断高风险导出、审阅任务。 |
| 错误编辑 | stale preview、隐藏批量删除、错误 asset ID | hash/revision-bound approval、影响范围 Diff、单 undo group、dry run。 |
| 隐私泄露 | 绝对路径、API key、未同意上传的媒体进入请求 | redaction、PathGrant、data policy、network allowlist、日志扫描。 |
| 第三方供应商不可用 | 429/断网/模型版本变更 | profile health、队列/重试、fallback、明确降级、可复现记录。 |

## 16. 模型能力、非多模态部署与 MCP 的关系

### 16.1 结论

**不接多模态大模型不会阻止 Topos 成为剪辑 Agent。** 一个高质量文本模型加上可靠的结构化分析和工具调用，已经可以完成：理解 Brief、写文案/脚本/大纲、读转录和镜头标签、搜索素材、生成 A/B 粗剪计划、调用预览/校验工具、解释软件功能和错误。

但纯文本模型不能直接“看懂”未被描述的视频帧、图片或界面截图，也不适合独立判断镜头美感、画面构图、品牌视觉一致性、表情/动作细节或音频质感。对此应由本地/云端的专用分析 provider 产生受控的结构化证据，或在需要时选择性调用多模态模型；不要要求文本模型假装看见了画面。

**MCP 不是多模态的替代品，也不是接入非多模态模型的前置条件。** MCP 是模型与工具之间的标准化 transport/协议。Topos 内部直接调用 `ToolFacade` 或使用 provider 原生 tool calling 都可以；只有需要让外部 MCP client/host 互操作时，才需要 MCP adapter。MCP 也不会自动让一个文本模型拥有视觉或音频理解能力。

### 16.2 三种可行部署模式

| 模式 | 模型能力 | Topos 如何提供上下文 | 能力与限制 | MCP 是否必要 |
| --- | --- | --- | --- | --- |
| A：文本 Copilot | text，无原生 tool calling | UI 组装受限的 `ContextBundle`；模型输出受 schema 校验的 JSON/文本 artifact | 可写脚本/大纲/帮助；只能提出计划，自动检索/循环执行较弱 | 否。可用严格 JSON protocol 作为过渡。 |
| B：文本 Tool Agent（推荐首发） | text + function/tool calling | 模型调用 `topos.search_assets`、`get_asset_analysis`、`preview_edit_plan` 等受限工具，拿到转录/镜头/音频/工程摘要 | 可完成可靠的规划与粗剪；视觉判断依赖分析质量 | 否。可直接接 `ToolFacade`；MCP 作为可选适配层。 |
| C：混合多模态 Harness（推荐演进） | 文本 Agent + 专用视觉/音频分析；可选 vision/video/audio LLM | 分析 provider 生成转录、OCR、shot/keyframe caption、object/tag、speaker、beat、质量信号；按需把少量关键帧传给多模态模型 | 同时兼顾成本、隐私、可解释性与更好视觉理解 | 否；MCP 仅用于跨客户端/扩展互通。 |
| D：端到端多模态 Agent | 单一模型支持文本/图像/音频/视频 + tools | 可直接看关键帧/代理/音频，同时读取结构化证据 | 体验强但成本、延迟、隐私与评测要求最高；仍需 TAEP/审批 | 否；同样只在互操作时使用。 |

### 16.3 推荐的“文本模型 + 分析工具”管线

```text
用户素材（仅在用户授权范围）
   ├─ 转录 / 说话人分离 / 静音 / 音乐节拍 / 响度分析
   ├─ 场景切分 / 关键帧抽取 / OCR / 镜头 caption / 标签 / 技术质量
   └─ 版权、来源、已有标记、用户锁定范围
                         ↓
脱敏、分页、带证据/置信度的 Semantic Index + Project Snapshot
                         ↓
文本 Tool Agent：search → 获取证据 → 写 Brief/脚本 → 提议 EditPlan
                         ↓
TAEP validator + Timeline Diff + 人工确认 + CommandStack
```

其中视觉/音频分析可以是本地模型、现有算法或云 provider；它们应各自实现 `AnalysisProvider`，输出统一的 `AssetAnalysis`/`SemanticSegment`，标明 `provider/model/version/created_at/confidence`。文本 Agent 应只根据证据说“在 00:12–00:18 的镜头标签显示产品特写”，而不是说“我看到了”却没有依据。

### 16.4 Provider Profile 与 Capability Manifest 的扩展

将模型选择从“一个 model ID”升级为能力画像。建议扩展 `AiProviderProfile`，但 API key 仍只存在 credential store：

```json
{
  "profile_id": "local-text-agent",
  "model_id": "...",
  "modalities": ["text", "tool_call", "json_schema"],
  "tool_transport": "native_function_call",
  "context_limits": {"input_tokens": 32000, "output_tokens": 4096},
  "policies": {"network": "local_only", "allow_media_upload": false},
  "cost_model": {"input_per_million": null, "output_per_million": null}
}
```

能力字段至少包括：`text`、`tool_call`、`json_schema`、`streaming`、`vision_image`、`audio_input`、`video_input`、`embedding`、`rerank`、`reasoning`（仅作模型选择标签，不暴露隐性思维链）。同时独立登记分析 provider 的能力；不要因为主聊天模型没有 vision 就隐藏可用的本地镜头分析。

Model Router 的选择规则：

1. 先检查任务所需能力和用户数据策略，再选本地/云、成本/延迟合适的 profile。
2. 首发优先选择 `text + tool_call + json_schema`；若只有 text，降级为 schema-constrained copilot，不开放自动工具循环。
3. 当任务需要视觉判断而无可信分析时，明确请求用户提供关键帧/描述，或提示切换到允许 vision 的 profile；不能以文本模型臆测补足。
4. profile/fallback/分析 provider 的实际版本必须写入 artifact/audit，便于复现与质量追踪。

### 16.5 MCP 何时值得配置

| 你的目标 | 是否需要 MCP | 推荐做法 |
| --- | --- | --- |
| Topos 内部调用当前配置的 LLM 进行文案、脚本、粗剪 | 不需要 | `AssistantOrchestrator → ToolFacade`，使用 provider 原生 tool calling 或受 schema 约束 JSON。 |
| 仅使用本地文本模型（如 Ollama） | 不需要 | 接 OpenAI-compatible/function-call 或 JSON adapter；分析能力在 Topos 内提供。 |
| 让 Claude Desktop、Codex、IDE Agent 等外部客户端操作 Topos | 需要 Topos MCP server | 先做 stdio/local IPC 薄 adapter，只暴露动态、受限的 `topos.*` 语义工具。 |
| 让 Topos 调用外部资源，如公司知识库/素材库/项目管理 | 可能需要 MCP client | 仅连接 allowlist 的 server，先只读、临时授权、显示数据范围与网络域名。 |
| 给文本模型补视觉理解 | 不需要 MCP 本身 | 配置本地/云视觉分析 provider 或支持 vision 的模型；将结果通过工具/ContextBundle 交给文本 Agent。 |

因此，建议技术顺序是：**先完成文本 Tool Agent + 本地素材分析 + TAEP 安全闭环；然后按互操作需求增加 MCP；最后按价值和成本选择性增加 vision/video/audio provider。** 这条路线比“一开始一定要多模态模型 + MCP”更稳、更便宜，也更容易保护用户素材。

## 17. 媒体池驱动的可选素材分析实施方案

### 17.1 产品原则

素材分析是 AI 剪辑的前置能力，但**不是媒体池的默认副作用**。很多用户只想手工剪辑、调色或快速导出，他们不应因为导入素材就遭遇 CPU/GPU 占用、模型下载、云上传、费用或隐私问题。

因此采用以下规则：

1. **默认关闭。** 导入媒体池只做编辑器必需的轻量媒体探测、缩略图/代理/波形等既有工作；不启动 AI 转录、视觉语义、说话人识别或云任务。
2. **媒体池是触发点和状态入口，非推理容器。** `MediaPool` 持有 `MediaAsset`、asset ID、fingerprint、基础 metadata 与分析状态；独立 `AnalysisService`/`JobScheduler` 执行模型。
3. **分析必须显式、可见、可取消。** 用户从媒体池选择范围和分析等级，或在 Assistant 中说“分析所有素材”；后者仍先创建分析提议卡，不能绕过确认直接扫描整个工程。
4. **本地优先、云端另行同意。** 用户可只启用本地开源模型；任何会上传代理、关键帧、音频或转录给云 provider 的步骤都要单独显示数据范围、模型、预计费用与保留策略。
5. **分析是缓存证据，不是编辑。** 结果生成结构化 `AssetAnalysis`/`SemanticSegment`，可搜索、可纠错、可失效、可删除；它不会自动把素材加入时间线或改变 timeline。

### 17.2 媒体池 UI 设计

媒体池新增一个全局 AI 分析开关和针对素材/集合的操作，避免把昂贵能力藏在 Assistant 内。

```text
媒体池工具栏
[导入] [新建 Bin] …  [ ✦ 分析素材 ▾ ]  [分析状态/任务]

“分析素材”菜单
  - 分析选中素材…
  - 分析当前 Bin…
  - 分析所有素材…
  - 管理分析模型与隐私设置…

素材卡/列表状态
  未分析 | 快速分析完成 | 深度分析完成 | 分析中 63% | 部分完成 | 已过期 | 失败
```

| 交互 | 行为 | 保护措施 |
| --- | --- | --- |
| 全局开关“启用 AI 素材分析” | 允许本项目显示分析菜单和 Assistant 的分析建议 | 关闭时不排队任何新任务；已有结果保留只读，用户可删除。 |
| 分析选中素材 | 对当前多选 `asset_id` 创建 `AnalysisRequest` | 显示数量、总时长、所选分析配方、硬件/费用估算。 |
| 分析当前 Bin/所有素材 | 先生成范围清单，可排除素材、离线素材、未授权素材、超大文件 | 默认不包含 Bin 外素材；“所有素材”必须二次确认。 |
| 素材右键菜单 | 分析、重新分析、取消、查看证据、编辑标签、删除分析结果 | 不删除原始素材；删除分析结果可恢复为“未分析”。 |
| Assistant 输入“分析所有素材” | Agent 调 `topos.propose_asset_analysis`，返回范围/缺口/本地或云 profile/预算卡 | 用户选择“快速/剪辑/深度”，并点确认后才调 `topos.request_asset_analysis`。 |
| 自动缺口提示 | 用户运行“口播精剪”等工作流时，提示“缺少转录，是否分析已选 6 条素材？” | 只建议，不自动启动；默认范围始终是工作流/用户已选上下文。 |

状态既要在媒体池显示，也要出现在 Assistant 的 Context 面板。Agent 只能把 `ready` 或明确标记为 `partial` 的证据用于计划；`stale`、`failed`、`unknown` 必须在方案卡里说明，而不是假装理解素材。

### 17.3 从导入到可搜索证据的实现链路

```text
1. 用户导入媒体
   └─ MediaAsset(asset_id, duration, fps, streams, fingerprint, rights metadata)

2. 用户显式选择“分析素材”或批准 Assistant 的分析提议
   └─ AnalysisRequest(scope, recipe, providers, policy, budget, project revision)

3. AnalysisScheduler 创建可取消 Job DAG
   ├─ 技术探测/音频波形（本地、低成本）
   ├─ 转录/说话人/静音/响度/节拍（按选择的 audio provider）
   ├─ 场景切分/关键帧抽取（本地）
   ├─ OCR/镜头标签/视觉质量（本地或已确认的云 provider）
   └─ 去重、质量校验、聚合

4. AnalysisService 合并 provider 片段
   └─ Versioned AssetAnalysis + SemanticSegment + per-field provenance

5. AnalysisStore 持久化、SemanticIndex 建索引
   └─ MediaPool 状态刷新；Topos Agent 仅经 search/get-analysis 工具读取

6. 指纹/设置/模型版本变化
   └─ 标为 stale，提示增量重分析；旧 evidence 仍可查看但不可当作当前事实
```

建议定义类型化请求，而不是由 UI 直接调用模型：

```python
@dataclass(frozen=True)
class AnalysisRequest:
    request_id: str
    project_id: str
    asset_ids: tuple[str, ...]
    recipe_id: str                 # fast_local / editorial_local / deep_semantic
    provider_ids: tuple[str, ...]
    data_policy: str               # local_only / approved_cloud
    budget: AnalysisBudget
    requested_by: str              # media_pool / assistant / skill
    user_confirmed_at: datetime | None
```

`AnalysisRequest` 创建、确认和执行应是三个不同步骤。特别是 Assistant 只能创建未确认的 proposal；`user_confirmed_at` 为空时，scheduler 必须拒绝实际执行。用户从媒体池手动发起时也应经过相同 service，而不是另写一条绕过审计的快捷路径。

### 17.4 分析配方（Recipe）与渐进式成本

| 配方 | 默认 provider 类型 | 产出 | 适用场景 | 上传/成本 |
| --- | --- | --- | --- | --- |
| `fast_local` | 媒体探测、波形/响度、镜头边界、关键帧 | streams、时长/FPS、LUFS、静音候选、scene cuts、代表帧 | 手工剪辑时的轻辅助 | 本地、低资源 |
| `editorial_local` | 加入本地 ASR、说话人分离、节拍/音乐检测 | 可搜索转录、speaker、speech ranges、BPM/beat、字幕草稿证据 | 口播/访谈/粗剪 | 本地、中资源 |
| `visual_semantic_local` | 加入本地 OCR、图像/视频 embedding、镜头 caption/tag、质量估计 | 产品/人物/地点/动作/屏幕文字标签、相似镜头、质量信号 | 产品片、Vlog、B-roll | 本地、较高 GPU/存储 |
| `cloud_enhanced` | 用户批准的云 ASR/vision/video provider | 更高质量或本地缺失的字段，并保留来源 | 用户明确追求质量或本机性能不足 | 明示上传、费用、保留期 |

配方必须是可编辑的默认组合，而不是“完整/不完整”的二元按钮。用户可勾掉人脸/人物识别、OCR、云端分析或音乐识别等敏感/昂贵模块；Agent/Skill 也只能建议受当前 policy 允许的 recipe。

### 17.5 开源模型分析引擎

Topos 适合采用“开放模型 provider pack”架构：每一种分析能力是独立 adapter，统一输出 TAEP 分析契约，而不是把某一个模型硬编码为软件依赖。这样用户可按硬件、语言、隐私和质量选择本地高质量开源模型。

| Provider 类别 | 典型输入/输出 | 模型运行方式 | 统一要求 |
| --- | --- | --- | --- |
| ASR/翻译 | 音频 → 带时间码的转录、语言、置信度 | 本地 CPU/GPU worker；可选量化模型 | 输出 frame-aligned transcript，保留语言/模型/词级时间码。 |
| 说话人分离 | 音频 → speaker turns、置信度 | 本地模型 worker | 与 ASR 时间轴对齐；身份名称默认 `S1/S2`，不做未经同意的人脸/身份推断。 |
| 场景/镜头 | 解码帧 → cut、shot ranges、代表帧 | 复用内置 scene cut + 本地视觉 worker | Cut 与 shot range 分开建模；所有范围使用整数帧半开区间。 |
| OCR | 关键帧 → 文本、区域、语言、置信度 | 本地 OCR worker | OCR 文本是不可信内容；保存 frame/region/evidence ref，不让其影响权限。 |
| 视觉语义/检索 | 关键帧或短镜头 → caption、tags、embedding、质量信号 | 本地 vision/video embedding worker | 标签、模型置信度和 frame range 必须可检查；embedding 是可重建缓存。 |
| 音频理解 | 音频 → speech/silence、BPM/beat、响度、音乐/SFX 标签 | DSP + 本地模型 worker | 音频分析不等于版权许可；响度/beat 结果标明算法与配置。 |
| 安全/版权辅助 | metadata/内容特征 → 风险提示 | 本地或用户批准云 provider | 只给风险/不确定性，不能声明法律结论或商用保证。 |

新增 `AnalysisProviderRegistry` 与 `ModelPackManager`：

1. Model Pack manifest 声明 `provider_id`、version、license、来源 URL/hash、支持语言/模态、运行时（ONNX/Core ML/PyTorch 等）、CPU/GPU/内存需求、输出 schema、默认 recipe、已知限制。
2. 模型下载必须由用户在“AI 分析模型”设置页选择并确认磁盘空间和许可；支持离线导入已校验的 model pack。
3. 下载/加载/推理在受控 worker process 中完成，不让模型包获得任意 Python、网络或项目写权限；model 文件校验 hash，出现不兼容/缺失时清楚降级。
4. 每次分析写入具体 provider/model/version/config/硬件 backend/时间/输入 fingerprint；同一指纹、同一配置可命中缓存，模型或设置变化则产生新 analysis revision。
5. 应提供“本机能力检测”：GPU/VRAM/内存/磁盘、预计速度、是否会影响播放；用户可限制并行度和仅在空闲时运行。

开源模型不是 MCP。它们作为本地 `AnalysisProvider` 在 Topos 的 Job worker 中运行，输出结构化证据；MCP 仅在需要让外部 Agent 调用 `topos.request_asset_analysis`、`topos.get_asset_analysis` 等语义工具时作为可选 transport。

### 17.6 分析存储与索引规范

每个 `asset_id` 应维护一份聚合分析记录，但不是把所有内容压成不可维护的单个文档。

| 存储层 | 保存内容 | 生命周期 |
| --- | --- | --- |
| 工程版本化 payload | `AssetAnalysis` 摘要、转录/镜头/标签时间码、user corrections、provenance、fingerprint、analysis revision、状态 | 随工程保存；可导出/删除；有 schema migration。 |
| 搜索索引 | `SemanticSegment`、倒排索引、轻量 metadata | 可由工程 payload 重建；更新时原子替换。 |
| 本机可重建缓存 | keyframes、waveform tiles、embedding vectors、临时音频/代理、模型中间输出 | 按 fingerprint/provider config 缓存，可清理，不应污染项目包。 |
| 审计 | AnalysisRequest、用户同意、provider、数据策略、Job、错误/取消、费用估算 | 最小化保存，不写原始媒体、绝对路径、secret。 |

现有 `AssetAnalysis` 已包含场景切分、转录、tags 和 audio 的基础字段；后续应扩展为**按字段 provenance**，而非只有一条全局 provenance。这样可以同时说明：“转录来自本地 ASR vX，镜头标签来自视觉模型 vY，响度来自 DSP vZ”，也能单独更新一种分析而不误覆盖用户修订。

### 17.7 调度、性能与失败语义

1. Job Scheduler 按资源池区分 `decode_cpu`、`local_gpu_analysis`、`cloud_network`、`render/export`，并把实时播放/导出优先级设高于后台分析。
2. 同一素材相同 fingerprint + recipe + provider config 的重复请求合并为一个 Job；不同素材可受硬件上限并行。
3. 长视频采用分块解码和检查点；取消后保留已完成的独立字段，状态为 `partial`，不把部分结果伪装成 complete。
4. 离线、模型缺失、VRAM 不足、语言不支持、素材损坏、云端拒绝、限流都应有稳定诊断码和可选恢复动作（换本地轻量模型、缩小范围、稍后重试、跳过该字段）。
5. 素材 fingerprint、代理重建、音频替换、用户修订和 provider/config 更新都触发精确失效：只重跑受影响字段，并使依赖该分析的未提交 AI variant 变为 stale。

### 17.8 实施顺序与验收

| 阶段 | 交付 | 验收 |
| --- | --- | --- |
| A0：契约与状态 | `AnalysisRequest`、recipe、provider/model manifest、媒体池 analysis state、工程 payload schema | 默认导入不启动 AI；任何请求都有 asset scope、fingerprint、来源、policy 和可见状态。 |
| A1：本地快速分析 | 媒体池按钮、scene cut 适配、响度/波形/静音、Job 化/取消/进度、缓存/失效 | 选中素材分析可后台运行、不影响 UI；取消无泄漏；素材变更后旧结果标 stale。 |
| A2：剪辑分析 | 本地 ASR、speaker、字幕证据、BPM/beat；Assistant 分析 proposal 卡 | 用户说“分析所有素材”时先审阅范围并确认；文本 Agent 能按台词/说话人检索并生成证据化 Variant。 |
| A3：视觉语义 | keyframe/OCR/tag/embedding/质量 provider packs、隐私开关、索引搜索 | 用户可按“产品特写/屏幕文字/人物反应”搜索；每一条结果可跳转到 frame 与 provider/置信度。 |
| A4：云增强与生态 | cloud provider consent、模型下载/许可 UI、pack registry、MCP 工具适配 | 云端绝不在无同意时上传；本地 pack 可离线使用；外部 Agent 只能请求分析 proposal，不能突破 project policy。 |

## 18. 标准化本地素材感知 Harness：组件选型与项目集成

### 18.1 决策

以下分析栈应作为 Topos 的 `editorial_local` / `visual_semantic_local` 两个分析配方的首选实现。它适合“所有素材先转为精确时间戳的结构化证据，文本 Agent 再进行检索、脚本规划和粗剪”的目标。

但它不是七个彼此无关的按钮：必须由统一的 `AnalysisScheduler` 编排为一个带依赖的 Job DAG，并由 `AnalysisService` 归一化为 TAEP 的 `AssetAnalysis` 与 `SemanticSegment`。文本 Agent 永远读取证据和引用，不直接读取未处理的视频文件。

| 证据域 | 推荐组件 | Topos Provider | 输出与使用方式 | 关键修正 |
| --- | --- | --- | --- | --- |
| ASR + 词级时间码 + speaker 归属 | WhisperX；内部可选 faster-whisper backend 与对齐模型 | `WhisperXTranscriptProvider` | 转录段、词级时间码、语言、置信度、`S1/S2` 说话人候选；供文字剪辑和字幕 | 不要分别跑 Faster-Whisper 和 WhisperX 两次；WhisperX 是完整管线。无可用对齐模型的语言要降级为 segment 级时间码。 |
| 独立说话人分割 | pyannote.audio adapter | `PyannoteDiarizationProvider`，可作为 WhisperX 的内部依赖或独立精修 provider | speaker turns、重叠语音、confidence；再与 ASR 词级时间码对齐 | speaker ID 默认匿名 `S1/S2`；不得把聚类结果宣称为真实身份。部分权重/访问可能有额外使用条款，不能只因为代码开源就自动下载。 |
| 镜头边界 | PySceneDetect，复用现有 scene-cut 能力 | 扩展现有 `SceneCutProvider` | cut/fade 事件、`ShotRange`、代表帧；供粗剪、关键帧抽样 | cut 不等于语义场景；必须把相邻 shot 的视觉证据聚合成可选 `SceneRange`。 |
| 关键帧/语义标签/相似检索 | SigLIP 类图像 embedding/caption 模型；后续可选轻量视频模型 | `VisualSemanticProvider` | keyframe caption、tags、embedding ref、质量信号；供“找产品特写/人物反应” | 单帧只代表局部画面。至少按 shot 的首/中/尾采样并合并置信度；embedding 是可重建缓存。 |
| OCR | RapidOCR 为本地/中文优先 adapter | `OcrProvider` | `text`、quad/box、语言、置信度、keyframe ref、frame range；供检索屏幕文字 | 只处理代表帧、检测到的画面变化或用户指定区域；OCR 输出永远标为不可信数据，不能影响 Agent 权限。 |
| 节拍/BPM | Librosa 首发；madmom 作为可选高质量 pack | `BeatAnalysisProvider` | BPM、beat/downbeat 候选、confidence；供卡点、音乐剪辑 | 不以 beat 结果替代音乐版权信息；无稳定节奏或人声为主时应返回低置信度/无结果。 |
| 响度/静音/音量包络 | FFmpeg `ebur128` + `silencedetect` + RMS/peak envelope | `AudioSignalProvider` | integrated/short-term LUFS、true peak、speech/silence ranges、RMS envelope | 一个 integrated LUFS 不足以支持剪辑决策；要存按时间片的音量包络。 |

参考实现与许可核验入口：

- [WhisperX](https://github.com/Likh-Alex/whisperx)：词级时间戳、对齐和 diarization 管线；部署前核验其依赖与模型权重的许可证/访问条件。
- [PySceneDetect](https://www.scenedetect.com/docs/latest/index.html)：镜头/转场检测，适合作为 shot boundary provider。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：本地 OCR 运行时；模型权利与代码许可证须分别记录。
- [librosa beat tracking](https://librosa.org/doc/latest/beat.html)：BPM/beat 基础实现。
- [FFmpeg ebur128](https://www.ffmpeg.org/doxygen/7.1/ebur128_8c.html)：响度测量基础。

### 18.2 时间轴与证据数据合同

“精确时间戳”不能仅保存浮点秒。每个 provider 的原始输出都先保留 media PTS/timebase，再由统一时间基转换为 Topos 的整帧半开区间；对 VFR 素材绝不使用简单的 `seconds × fps` 计算。

```text
provider 原始时间（PTS + Rational timebase / sample offset）
        ↓ TimebaseService（rounding policy 可审计）
source FrameRange [start_frame, end_frame)
        ↓ 仅在构建 EditPlan 时
timeline range（考虑 source offset、speed、reverse、retime）
```

新增/扩展的模型应保持强类型与字段级 provenance：

```python
@dataclass(frozen=True)
class EvidenceProvenance:
    provider_id: str
    model_id: str
    model_version: str
    config_hash: str
    input_fingerprint: AssetFingerprint
    created_at: str
    license_id: str | None

@dataclass(frozen=True)
class EvidenceSegment:
    evidence_id: str
    asset_id: str
    kind: SegmentKind                 # transcript/shot/ocr/beat/tag/audio/quality
    source_pts_start: int | None
    source_pts_end: int | None
    source_timebase: str | None
    source_range: FrameRange          # canonical [start, end)
    payload: Mapping[str, JsonValue]
    confidence: float | None
    provenance: EvidenceProvenance
    user_verified: bool = False
```

`AssetAnalysis` 继续作为每个 `asset_id` 的聚合摘要，但从“一条全局 provenance”演进为每个字段/证据都有 provenance。例如转录来自本地 ASR，OCR 来自 RapidOCR，响度来自 FFmpeg；它们可独立更新，且绝不覆盖用户修订。

### 18.3 Job DAG 与资源调度

```text
MediaPoolPanel 导入/选择素材
  ↓（用户点击“分析素材”或确认 Assistant proposal）
MediaAnalysisController → AnalysisRequest
  ↓
AnalysisScheduler
  ├─ ProbeAssetJob               ffprobe/PyAV：streams、duration、PTS、fingerprint
  ├─ ExtractAudioJob             单次解码为受控临时音频
  │    ├─ TranscriptDiarizationJob  WhisperX / optional pyannote
  │    └─ AudioSignalJob            LUFS、silence、RMS、beat
  └─ DecodeVideoJob              一次解码 + scene-cut + shot/keyframe manifest
       ├─ OcrJob                 关键帧/画面变化帧
       └─ VisualSemanticJob      shot-level tags/embedding/quality
                  ↓
        MergeAndValidateJob → AnalysisStore + SemanticIndex
                  ↓
        MediaPool status / Assistant context / topos.search_assets
```

调度规则：

1. 解码产物按 `asset fingerprint + recipe + provider config` 复用，避免多个 provider 重复全片解码。
2. 资源队列分为 `decode_cpu`、`local_gpu_analysis`、`network_cloud`、`render_export`；实时播放和用户主动导出优先级高于后台分析。
3. ASR、视觉和 OCR 使用 worker process，不在 Qt UI、render thread 或媒体池 widget 内运行；进度、取消和诊断通过 typed job events 回到 UI。
4. 长片按 shot/chunk 做检查点；取消时保留已验证的独立 evidence，状态为 `partial`，不得报告为完整分析。
5. 相同请求去重；模型缺失、VRAM 不足、语言不支持、离线、损坏媒体、云端限流必须有稳定诊断和降级建议。

### 18.4 具体代码接线计划

| 位置 | 修改/新增内容 | 不可违反的边界 |
| --- | --- | --- |
| `src/features/edit/ui/media_pool_panel.py` | 增加分析工具栏、范围菜单、素材分析 badge、右键操作和 `analysis_requested(AnalysisRequestDraft)` signal | UI 只收集范围/显示状态，不能 import 或直接执行模型。 |
| `src/app/controllers/` 新增 `media_analysis_controller.py` | 连接媒体池、AI Assistant、project lifecycle、AnalysisService 与 Job UI；负责确认 dialog 和状态刷新 | 不能解析模型结果/写 timeline；工程切换时取消或解绑 jobs。 |
| `src/features/edit/ai_editing/analysis_models.py` | 扩展 `AssetAnalysis`、增加 `EvidenceSegment`、字段级 provenance、analysis status/recipe/request models | 所有时间范围为 Rational/整数帧合同；保留 unknown fields/migration。 |
| `src/features/edit/ai_editing/providers/` 新增 package | `probe.py`、`whisperx.py`、`pyannote.py`、`audio_signal.py`、`ocr.py`、`visual_semantic.py`；每个实现 `AnalysisProvider` | provider 无 Qt、无 timeline 写权限、无任意路径权限。 |
| `src/features/edit/ai_editing/analysis_service.py` | 从当前同步过渡实现升级为 provider registry + request orchestration + merge/invalidations | 不在 service 同步长跑；只有 scheduler/worker 可调用 `provider.analyze()`。 |
| `src/features/edit/ai_editing/analysis_scheduler.py` | 新增 Job DAG、资源池、缓存 key、checkpoint、取消与重试 | 不阻塞 UI/GPU render thread；任何 cloud node 需已确认 policy。 |
| `src/features/edit/ai_editing/analysis_store.py` | 工程 payload、索引构建、可重建缓存索引、schema migration | 绝不写 secret/绝对路径/不可重建的大型缓存进工程。 |
| `src/features/edit/ai_editing/model_packs.py` | `AnalysisProviderRegistry`、model pack manifest、下载/校验/许可/硬件能力 | 下载和模型执行需用户同意；pack 没有 shell/network/project write 权限。 |
| `src/features/edit/ai_editing/tool_facade.py` | 新增 `topos.propose_asset_analysis`、`topos.request_asset_analysis`、`topos.get_analysis_status`、`topos.cancel_analysis`；保留已有 search/get | Agent 只能提出分析，真实请求需同一 `AnalysisRequest` confirmation policy。 |
| `src/app/controllers/ai_edit_host_bridge_impl.py` 与 Assistant UI | 将“分析所有素材”解析为 proposal card，而非直接启动任务；在 Context 区展示 coverage/freshness | Assistant 不读取绝对路径，不可静默把范围扩大到全项目。 |
| `src/features/edit/locales/*.json` 与测试 | 新增所有状态、许可、预算、错误和无障碍文案 | 不散写中文；长文本和错误必须可复制。 |

### 18.5 Model Pack 与依赖策略

不要把模型重量级依赖加入基础安装包。采用按需下载的可验证 model pack：

```json
{
  "pack_id": "analysis.whisperx.zh-multilingual",
  "version": "1.0.0",
  "provider_id": "whisperx_local",
  "capabilities": ["transcript", "word_alignment", "diarization"],
  "runtime": "python-worker",
  "hardware": {"cpu": true, "gpu_optional": true, "min_ram_gb": 8},
  "models": [{"source": "…", "sha256": "…", "license": "…"}],
  "output_schema_version": "topos.analysis/v1"
}
```

模型设置页需要显示磁盘占用、CPU/GPU/VRAM 要求、语言、离线能力、许可/访问要求、预计速度和删除按钮。默认安装基础不含任何大模型；用户选择 `editorial_local` 或 `visual_semantic_local` 时，展示缺失 pack 清单并逐个确认下载。对于必须登录、接受模型卡条款或使用 token 的权重，标明“受限权重”，不可标作完全离线/无账号开源模型。

### 18.6 分阶段实现与验收

| 小阶段 | 范围 | 完成定义 |
| --- | --- | --- |
| S0：数据合同 | `AnalysisRequest`、时间基/provenance/schema migration、media-pool 状态模型 | VFR fixture 中所有 evidence 能准确回到 source frame；素材替换后准确 stale。 |
| S1：快速本地证据 | media-pool opt-in 控件、probe、现有 scene-cut 接入、FFmpeg 响度/静音/波形、真实 async/cancel | 默认导入零 AI 推理；点击分析后 UI 不阻塞、可取消、可查看结果。 |
| S2：文字剪辑证据 | WhisperX/本地 ASR pack、可选 diarization、词/段级对齐、字幕/搜索 UI | Agent 能按台词和说话人提出 A/B 粗剪；每个选段带时间证据和置信度。 |
| S3：视觉搜索证据 | shot-level keyframe、RapidOCR、tags/embedding provider、SemanticIndex 扩展 | 用户搜索“屏幕显示快充/产品特写”可定位素材与 frame；OCR 文本不影响权限。 |
| S4：模型生态与策略 | Model Pack Manager、许可证/下载、GPU health、recipe/预算/隐私、MCP tool adapter | 本地-only 项目永不联网；云项目需要显示上传范围并确认；模型可卸载/替换。 |
| S5：质量与回归 | 大型工程压力、取消/恢复、缓存、许可证扫描、红队注入、E2E AI plan | 1000 clip/长转录不破坏播放；未确认分析/写入为零；所有产物可追溯。 |

首个真正可用的版本建议只做 S0–S2：媒体池手动启用、快速分析、可搜索转录/说话人、场景切分、响度/静音，以及基于证据的口播粗剪。视觉 embedding/OCR 和大量模型包在这一闭环稳定后加入，避免一次性引入重量级依赖、许可证风险和 GPU 资源竞争。
