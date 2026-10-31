# AI Assistant 对标专业 Agent 工具优化实施计划

> 日期：2026-08-14  
> 状态：待实施（底部模式选择器基础版已实现；能力路由与未接通模型类型待实施）  
> 范围：AI Assistant 的交互体验、模式选择、Profile 能力路由、Agent 执行内核、上下文 Token、历史会话、取消恢复、权限审批、结果验证  
> 对标对象：Codex 等专业 Agent 工具的任务化、可观察、可恢复工作流  
> 项目技术栈：PyQt6 + ModernGL + Topos Color 分层架构

## 1. 文档定位

本文档是 AI Assistant 的“专业 Agent 能力对齐”专项计划，重点解决普通对话路径与现有 Agent Harness 之间没有完全打通的问题。

本文档不替代以下已有计划：

- `docs/ai_assistant_agent_harness_product_plan_2026-08-01.md`
- `docs/ai_assistant_agent_harness_remaining_work_plan_2026-08-12.md`
- `docs/ai_assistant_agent_harness_acceptance_evidence_2026-08-12.md`
- `docs/ai_editing_autonomy_reliability_batch_2026-08-09.md`

已有计划主要覆盖 AI 编辑、分析、MCP、生成 Provider Pack 和发布证据；本文档专门补齐以下“用户在聊天窗口中能直接感知”的 Agent 能力：

1. 普通聊天与统一 Agent Runtime 的连接。
2. 模型思考、规划、工具调用、等待审批、校验等过程状态的可观察性。
3. 真实 Token 用量、上下文预算和自动压缩。
4. 可恢复的历史会话、任务运行记录和草稿。
5. 取消、断网、失败、重试和应用重启后的恢复。
6. 工具级权限、审批和安全范围。
7. 结构化结果、预览、验证、撤销和可追溯证据。

## 2. 审计结论

### 2.1 一句话结论

当前 AI Assistant 已经具备“AI 编辑器面板”的主要外观和若干基础能力，但还没有完全成为一个统一的 Agent 工作台。

目前的主要问题不是缺少按钮，而是不同执行路径之间没有形成统一闭环：

```text
当前形态：
普通聊天 → _ChatWorker → LLM 普通文本回复
Skill 请求 → Router → Skill/Runtime → 结构化编辑流程
工具循环 → tool_calling_client / agent_loop（已有，但未完整接入普通聊天）

目标形态：
所有请求 → AgentTask → AgentRun → Observe → Plan → Approval → Tool/Skill 执行
         → Verify → Artifact/Diff → Persist → 可继续、可重试、可撤销
```

### 2.2 当前已经具备的能力

以下能力已经有实现基础，不应重复重做：

| 能力 | 当前状态 | 相关位置 | 评价 |
| --- | --- | --- | --- |
| 对话区域滚动 | 已使用 `QScrollArea`，消息区域不会无限撑高面板 | `src/features/edit/ui/ai_edit_assistant_panel.py` | 基础交互已达标 |
| 流式回复 | 支持增量回复和自动滚动 | `AiEditAssistantPanel.set_streaming_reply()` | 需要补充状态事件和取消语义 |
| 思考提示 | 有“思考中…”动态提示 | `AiEditAssistantPanel.set_thinking()` | 需要从单一状态升级为阶段状态 |
| 历史二级菜单 | 历史会话可展开并点击切换 | `AiAssistantComposer.set_history_sessions()` | 需要完整保存正文、搜索和管理能力 |
| 缓存上限 | 默认最多缓存 50 个会话 | `AssistantSessionStore.DEFAULT_CACHE_LIMIT` | 机制已存在，内容粒度不足 |
| 上下文压缩 | 发送前按估算 Token 压缩旧消息 | `AiEditController._compress_chat_messages()` | 需要接入真实用量和清晰标注 |
| 运行抽屉 | 有 Run Drawer、取消、重试、诊断入口 | `AiRunDrawer` | 普通聊天尚未完整进入运行活动流 |
| 权限选择 | 有“仅建议 / 可提议 / 逐次审批” | `ComposerFooterState` | 需要落到工具级策略和审批卡 |
| 工具循环基础设施 | 已有 Tool Calling Loop 和 Agent Loop | `tool_calling_client.py`、`agent_loop.py` | 需要接入普通聊天主路径 |
| LLM 实际用量 | Client 能解析 `LLMUsage` 和 `last_usage` | `llm_client.py` | 目前没有回流顶部 UI 和会话记录 |

### 2.3 当前最重要的差距

| 编号 | 差距 | 用户可见表现 | 优先级 |
| --- | --- | --- | --- |
| P0-01 | 普通聊天没有统一进入 Agent Loop | 模型只能回答文本，不能稳定地观察、调用工具、执行并校验 | P0 |
| P0-02 | 实际 Token 没有接通顶部 | 顶部数字是本地估算，不知道真实 prompt/completion/cost | P0 |
| P0-03 | 取消不是强语义取消 | 用户点停止后，底层 HTTP 可能仍在运行 | P0 |
| P0-04 | 普通聊天不能跨重启恢复 | 应用关闭或网络中断后无法继续原任务 | P0 |
| P0-05 | 权限没有落实到每个工具动作 | “逐次审批”还不是完整的工具级审批和范围限制 | P0 |
| P1-01 | 思考提示没有阶段信息 | 用户只知道“模型在思考”，不知道卡在哪一步 | P1 |
| P1-02 | 历史保存的是摘要而不是完整对话 | 切换历史后可能无法准确继续长对话 | P1 |
| P1-03 | 历史没有任务管理能力 | 无搜索、重命名、归档、删除、置顶、分支和导出 | P1 |
| P1-04 | 普通聊天没有验收闭环 | 没有明确的目标、完成条件、校验结果和证据 | P1 |
| P1-05 | 消息模型不包含工具、状态和用量 | 工具调用、工具结果、错误、耗时和 Token 无法统一展示 | P1 |
| P1-06 | Run Drawer 与普通聊天没有完全统一 | 有运行抽屉，但普通文本请求可能不形成完整 Run | P1 |
| P2-01 | 没有子 Agent 和并行子任务的用户可见控制 | 后端存在任务图，但用户不能理解或管理子任务 | P2 |
| P2-02 | 缺少工具发现和能力状态 | 用户不知道当前可用哪些 Skill、MCP 和工具 | P2 |
| P2-03 | 没有统一的错误恢复中心 | 失败、断网、额度不足、权限拒绝的处理方式不一致 | P2 |
| P2-04 | 没有完整的 Agent 质量反馈闭环 | 缺少每次任务的成功率、撤销率、人工评分和耗时统计 | P2 |
| P0-06 | Profile 能力与 Assistant 模式没有统一路由 | 勾选了视觉/语音/生成能力，不代表 Assistant 会选中对应 Profile | P0 |
| P0-07 | 媒体模式可能落到 deterministic Stub | 未配置真实视频/音乐 Provider 时，用户可能看到“生成成功”的演示候选 | P0 |
| P1-07 | 模式选择器覆盖面不完整 | 当前只有普通对话、Motion Graphic、图片/视频/音乐；ASR、TTS、视觉理解、Embedding 没有统一入口 | P1 |
| P1-08 | 能力分类含义混淆 | “视觉模型”同时承载视觉理解和图片生成，Seedream 等 Profile 容易被误认为 VLM | P1 |

### 2.4 Motion Graphic Clip 影响结论

这份计划不会天然破坏 Motion Graphic Clip 的视觉生成能力。只要保持下面的
“功能边界不变、执行入口可替换”，A0、A3、A4、A5 和 A7 主要影响聊天体验、
可观察性和恢复能力；HTML/CSS 设计自由度、页面切换和渲染管线可以保持不变。

真正有风险的是把现有 Motion Graphic 专用流程粗暴改造成普通文本 Agent：

```text
现有安全链路：
用户讨论 → 明确生成意图 → LLM HTML/CSS transport → sanitize/quality gate
→ 真实浏览器验证 → 确认卡 → 用户点击确认 → MotionGraphicClipCommand
→ scene/timeline/postcondition 验证
```

实施 V2 时必须保留这条链路。`AgentOrchestrator` 可以接管任务、事件、取消、
checkpoint 和审批状态，但不能直接把模型文本当成已完成的时间线写入。

| 阶段 | 对 Motion Graphic 的影响 | 必须增加的兼容保护 |
| --- | --- | --- |
| A0 | 低：只增加运行契约和开关 | 旧入口可回退；保留现有 HTML 管道和确认卡 golden tests |
| A1 | 高：普通聊天与 HTML 专用请求要统一记录 | 用 `TeachingAnimationAgentAdapter` 桥接，不改写 `start_html_generate_chat()` 的非 JSON 语义 |
| A2 | 高：审批状态可能被重复实现 | `WAITING_APPROVAL` 只对应一个确认卡；写入仍只能走 `ToolFacade → ApprovalService → MotionGraphicClipCommand` |
| A3 | 中：增加阶段和运行抽屉 | HTML 生成、浏览器验证、确认等待必须有独立事件，不把长 HTML 当普通聊天气泡逐字解析 |
| A4 | 高：上下文压缩可能删除画布/版本约束 | 画布尺寸、fps、scene revision、context fingerprint、当前用户确认意图列为不可压缩事实 |
| A5 | 高：重启恢复可能误触发写入 | 只恢复为 `PAUSED_FOR_RESUME`/待确认草稿；旧 approval 永不恢复，必须重新验证并重新点击确认 |
| A6 | 正向：加强生成完成判定 | 增加 HTML 多页逐页捕获、场景引用、Clip 引用、revision 和 undo/redo 的确定性验收 |
| A7 | 低到中：增加质量指标 | 增加 HTML 空白帧率、页面可见率、首帧/中间页/末页验证和浏览器失败分类 |

### 2.5 Motion Graphic 必须锁定的不变量

以下不变量应写入 A0 的契约测试，后续任何 Agent Runtime 重构都不能改变：

1. 当前时间线的宽度、高度、fps 是唯一项目画布来源；模型不得再次询问或自行替换画幅。
2. 模型只接收画布和必要上下文；页面数量、页面内部布局、表格/图表/3D 视觉、CSS 动画和视觉语言由模型自由决定。
3. 页面切换时长、默认总时长、键盘切换和页面转场由 Motion Graphic Clip 编辑器接管；模型提供时长只能作为创作参考，不得写死编辑器的定时器。
4. 模型输出是 `HtmlArtifact`，不是可直接执行的工具指令；模型 HTML 禁止 JavaScript、远程资源和事件处理器，编辑器可以注入受信任的页面控制器。
5. `data-topos-page` 页面标记、宿主页面布局、缩放适配、逐帧捕获和导出 HTML 的控制器必须由宿主维护，不要求模型重复实现。
6. 生成阶段只能暂存 `raw_html/sanitized_html` 和验证证据，不能写工程、插入时间线或替换现有 Clip。
7. 只有一个明确的确认动作可以进入写入；用户在确认前发送修改意见时，不得自动执行旧方案。旧方案应保留为草稿并标记 stale，或由用户明确选择替换后重新生成。
8. 确认前后都要检查 scene revision、context fingerprint、artifact hash；确认后的场景变化必须阻止写入。
9. 写入后必须验证 `scene_id`、HTML layer、Clip 引用、时长、画布和至少一组真实浏览器帧；失败只能进入 `FAILED/PARTIAL_SUCCESS`，不能显示成功。
10. 所有 HTML 预览、时间线预览、导出和缓存路径必须使用同一个页面选择与渲染契约，不能出现“编辑器能看、时间线黑帧”两套行为。

### 2.6 当前实现与 V2 的桥接点

当前 Motion Graphic 不是普通聊天的纯文本分支，而是由以下专用入口组成。V2
应先包裹这些入口，再逐步替换内部实现：

| 当前入口 | V2 桥接责任 | 禁止的替换方式 |
| --- | --- | --- |
| `start_html_generate_chat()` | 创建 `AgentRun`，保留 HTML 非 JSON transport 和完整响应收集 | 不得把完整 HTML 强行塞进普通 JSON tool-call |
| `_handle_model_motion_graphic_html_response()` | 产生 `HtmlArtifact`，记录 raw/sanitized hash 和质量结果 | 不得直接 append 为普通 assistant 文本后丢失 artifact |
| `_validate_motion_graphic_scene()` | 作为 `VERIFYING` 阶段的真实浏览器 gate | 不得用“模型返回 HTML”或静态检查代替捕获验证 |
| `set_motion_graphic_proposal_card()` | 映射 `WAITING_APPROVAL`，显示唯一确认动作 | 不得同时显示旧确认按钮和新审批按钮 |
| `_confirm_motion_graphic_request()` | 映射审批批准后的唯一写入边界 | 不得让通用 Agent Loop 直接调用 timeline/view |
| `MotionGraphicClipCommand` | 保持现有 CommandStack、Undo/Redo 和插入验证 | 不得旁路工程 revision 或直接改 timeline 对象 |

建议新增明确的请求对象，而不是继续扩展 `dict[str, Any]`：

```text
MotionGraphicBuildRequest
  task_id / run_id / request_id
  scene_id / scene_revision / context_fingerprint
  canvas_width / canvas_height / fps / insert_frame
  render_mode = html
  user_brief / proposal_ref

HtmlArtifact
  artifact_id / raw_html_hash / sanitized_html_hash
  html_payload_ref / page_count / viewport
  sanitizer_version / quality_report / render_evidence
  approved_scene_hash / writable
```

### 2.7 本次审计发现的具体缺口

这些不是抽象的未来风险，而是当前实现接入 Agent V2 时需要明确处理的边界：

| 当前缺口 | 现状 | 计划补法 |
| --- | --- | --- |
| HTML 预览证据分两条路径 | `_html_preview_backend()` 可以返回空，生成结果先记录 preview blocked，再由模块单独跑验证 | 由 `TeachingAnimationAgentAdapter` 统一发出 `verification_started/finished`；不能因统一 Runtime 而删除真实浏览器 gate |
| 验证尺寸偏小 | 当前 Motion Graphic 快速 gate 使用低分辨率 smoke frame，不能单独证明 1920×1080 或竖屏页面布局完整 | A6 要求保持目标画幅比例，并在页面中间帧/边界帧做设计尺寸或等比例 viewport 验证；低分辨率只能作为快速失败检查 |
| 待确认计划主要在内存 | `_pending_motion_graphic_requests` 和确认卡状态可能随应用退出消失 | A5 持久化不可执行 artifact 草稿，恢复时重新验证、重新签发确认 |
| 请求相关性依赖 mode/request 字段 | `_chat_mode`、单个 request id 和响应 handler 需要防止并发、迟到、重复回调 | A1 引入 task/run/artifact 三层幂等键，所有回调先校验 run 状态和 artifact hash |
| 历史消息仍是短摘要 | `MessageSummary` 适合目录显示，不适合恢复长 HTML、完整工具事件和用量 | A4/A5 将正文、artifact、usage、event log 分层保存；目录仍可用短摘要 |
| 页面验证容易只看首帧 | 首帧可能是透明/纯色/动画尚未出现，或者第一页正常而后续页出画 | A6 固化逐页候选帧、DOM/viewport 可见性和 uniform frame 分类，不能只放宽 blank gate |
| 生成协议版本未进入任务模型 | HTML、场景 JSON、普通文本各自有专用 handler，但 Agent 统一入口容易误判 | `AgentTask.kind`、`Artifact.kind`、protocol version 必须参与路由和恢复，未知类型 fail closed |

### 2.8 AI Editing Profile 与 Assistant 能力路由审计（截至 2026-08-14）

这次审计把“底部模式选择器”和“Profile 能力配置”分开看。前者已经有基础实现，
后者虽然可以保存多种模型类型，但目前还没有形成一条对所有能力都可验证的统一路由。
因此下一步计划不能把“下拉框出现”误认为“模型已经接通”。

#### 已有基础实现

- `src/features/edit/ai_editing/assistant_modes.py` 已定义 `chat`、
  `motion_graphic`、`video`、`music`、`image` 五个稳定模式标识。
- `AiAssistantComposer` 已把模式选择器放在底部 Profile/权限/推理设置同一行，
  `AssistantRequest.mode` 会随请求提交；运行期间模式会锁定，避免中途改路由。
- `AiEditController` 已能把 Motion Graphic 交给现有 Teaching Animation 确认链，
  把视频、音乐、图片交给现有 `GenerationHarnessAdapter` 入口；这只是模式到入口的
  第一层连接，还不能证明每种 modality 都有真实 Provider。

#### 当前 Profile 配置与实际可用能力

以下只记录能力和路由状态，不记录 API key 等敏感值。用户当前配置可概括为：语言模型
（DeepSeek、GLM、火山文本）已配置，Seedream 图片生成和豆包 TTS 已配置；没有可确认的
远程视频、音乐、ASR、Embedding Profile，也没有真正的远程视觉理解（VLM）Profile。

| 用户模式/能力 | 当前路由 | 当前真实状态 | 计划结论 |
| --- | --- | --- | --- |
| 普通对话/规划 | `LLMClient`/`ModelRouter` | 语言 Profile 已接通 | 保留，迁移到统一 `CapabilityRouter` |
| Motion Graphic | Teaching Animation 专用适配器 | 可进入 HTML、浏览器验证、确认卡和写入链 | 保留专用协议，不能走普通文本 artifact |
| 图片生成 | `GenerationHarnessAdapter` → 图片 Provider | Seedream Profile 可用时可走真实 Provider | 运行前检查 `image_generation` contract；无 Provider 时禁用，不使用 Stub 冒充成功 |
| 视频生成 | `GenerationHarnessAdapter` → 通用/专用生成 Provider | 当前没有已确认的真实视频 Profile；通用 HTTP 只有在 `/v1/generations` 合约匹配时才可用 | 缺 Provider 时显示未配置并停止，不产生“成功候选” |
| 音乐生成 | `GenerationHarnessAdapter` → 通用/专用生成 Provider | 当前没有已确认的真实音乐 Profile | 同视频，禁止 deterministic fallback 伪装真实生成 |
| 语音识别（ASR） | `ProfileAwareTranscriptProvider` | 有路由和本地 fallback，但当前无远程 ASR Profile | 先作为内部能力；增加直达模式前必须补 UI、输入/输出和取消契约 |
| 语音合成（TTS） | `ProfileAwareTtsProvider` | 豆包 TTS Profile 已配置，当前主要服务字幕/配音链 | 可作为已接通能力；增加直接模式前须补预览、保存和审批语义 |
| 视觉理解（VLM/OCR/图像分析） | 本地视觉语义 Provider | 目前依赖本地模型包，不等同于已配置远程视觉模型 | 与图片生成拆成两个 capability；没有输入适配器时不能宣称已接通 |
| 向量化/Embedding | 本地模型包/配置预设 | 配置项存在，但 Assistant 没有直接运行入口 | 先做索引服务契约，再开放模式；不能把 embedding 当聊天模型调用 |

#### 必须修正的冲突

1. `modalities` 表示 Profile 能做什么输入/输出，`capabilities` 表示系统怎样使用它；
   二者不能只靠复选框推断。每个模式必须解析为一个带 `provider_id`、协议版本、
   输入类型、输出 artifact 类型和可取消能力的 `CapabilityResolution`。
2. “视觉模型”要拆为 `vision_understanding` 与 `image_generation`。Seedream 的图片
   生成 Profile 不能被标记为 VLM，也不能被 `vision` 复用到图片理解请求。
3. 视频/音乐/图片模式不能因为注册了 deterministic Stub 就显示真实生成成功。Stub 只
   能用于开发 fixture，并在 UI 和事件中明确标记 `demo`；生产路径无真实 Provider 必须
   fail closed，并给出配置/协议原因。
4. 全局 `active_profile` 不足以覆盖多能力。语言、视觉、ASR、TTS、Embedding 和各类
   media generation 要使用 capability-specific active profile；用户切换语言模型不能
   悄悄改变图片或配音模型。
5. OpenAI-compatible chat、VLM 多模态消息、ASR/TTS、视频/音乐异步生成和 Embedding
   的请求/响应结构不同，不能全部塞进 `list[dict[str, str]]` 或通用 `/v1/generations`
   假设；必须由独立 adapter 声明和验证 contract。

#### 本计划的路由原则

模式选择器只负责表达用户意图，真正执行前必须经过实时能力解析：

```text
用户模式 → CapabilityRouter → active profile（按 capability）
         → provider contract/输入适配 → consent/approval
         → AgentRun → artifact → verify → UI 结果卡
```

如果解析结果是“未配置、协议不兼容、未安装、被策略禁用或连接失败”，UI 应把模式置为
不可执行并说明原因；不得退回另一个 modality，也不得把配置错误变成空白画面或假成功。

## 3. 目标用户体验

用户发送一条复杂请求后，Assistant 应呈现如下流程：

```text
1. 用户发送请求
   ↓
2. 顶部立即显示：正在收集上下文 · 上下文 8.4k / 32k · 估算
   ↓
3. Assistant 显示计划摘要：准备执行 3 个步骤
   ↓
4. 每个步骤进入运行抽屉：读取素材 → 生成方案 → 校验时间线
   ↓
5. 涉及写入时显示审批卡：将修改哪些对象、原因、风险、权限范围
   ↓
6. 用户批准后执行，显示工具结果和进度
   ↓
7. 执行完成后显示：已完成 / 部分完成 / 等待补充信息 / 失败
   ↓
8. 结果卡提供预览、Diff、应用、撤销、重试、查看诊断
   ↓
9. 会话、任务、Token、压缩 checkpoint 和结果引用全部保存
```

注意：这里的“思考状态”只显示安全的阶段摘要，不显示模型内部逐字思维链。推荐显示：

- 正在收集上下文
- 正在分析素材
- 正在制定执行计划
- 正在调用工具：工具名称
- 等待你的确认
- 正在校验结果
- 正在保存任务状态

## 4. 目标架构

### 4.1 核心领域对象

新增或扩展以下领域对象，统一替代普通聊天目前只有“文本消息”的模型：

| 对象 | 必要字段 | 作用 |
| --- | --- | --- |
| `AgentTask` | `task_id`、`session_id`、`title`、`user_goal`、`acceptance_criteria`、`created_at` | 表示用户真正要完成的任务 |
| `AgentRun` | `run_id`、`task_id`、`status`、`phase`、`started_at`、`ended_at`、`checkpoint_id` | 表示一次可取消、可恢复的运行 |
| `AgentEvent` | `event_id`、`run_id`、`kind`、`timestamp`、`payload`、`parent_id` | 统一记录状态、工具、审批和结果事件 |
| `AgentMessage` | `message_id`、`role`、`content`、`status`、`run_id`、`usage`、`artifact_refs` | 统一对话消息和运行关联 |
| `ContextUsage` | `estimated_prompt`、`actual_prompt`、`completion`、`total`、`limit`、`reserved_output`、`is_estimated` | 统一 Token 和预算显示 |
| `CapabilityResolution` | `mode`、`capability`、`profile_id`、`provider_id`、`adapter`、`contract_version`、`availability`、`reason` | 把模式选择解析为唯一且可解释的执行能力 |
| `ProviderContract` | `endpoint_style`、`input_schema`、`output_schema`、`supports_stream`、`supports_usage`、`supports_abort` | 防止把不同模型协议错误地当成同一种聊天或生成接口 |
| `ApprovalRequest` | `approval_id`、`tool_name`、`risk`、`scope`、`diff`、`expires_at` | 工具级审批，而不是只保存一个权限字符串 |
| `ArtifactRef` | `artifact_id`、`kind`、`path/id`、`preview_ref`、`provenance` | 让结果可预览、可验证、可重建 |
| `AgentCheckpoint` | `run_id`、`phase`、`completed_steps`、`messages_digest`、`context_hash` | 支持取消、重试和重启恢复 |

### 4.2 状态机

普通聊天和 Skill 执行都必须使用同一套可观察状态：

```text
CREATED
  ↓
GATHERING_CONTEXT
  ↓
PLANNING
  ├─→ WAITING_USER_INPUT
  ├─→ WAITING_APPROVAL
  └─→ EXECUTING
          ↓
       VERIFYING
          ├─→ SUCCEEDED
          ├─→ PARTIAL_SUCCESS
          ├─→ FAILED
          ├─→ CANCELLED
          └─→ PAUSED_FOR_RESUME
```

状态转换的硬规则：

1. UI 只能订阅状态和事件，不直接调用 service 的私有 `_set_state()`。
2. 每次状态变化都必须生成 `AgentEvent`。
3. 写入时间线、工程或文件前必须经过 `ApprovalRequest` 或明确的只读策略。
4. `SUCCEEDED` 必须有验证结果或确定性后置条件，不能只因为模型返回文本就算成功。
5. `CANCELLED`、`FAILED`、`PAUSED_FOR_RESUME` 都必须保存最后一个 checkpoint。

### 4.3 统一执行入口

建议新增一个薄层 `AgentOrchestrator`，复用现有模块而不是新建另一套执行系统：

```text
AiEditController
      ↓
AgentOrchestrator
      ├── ContextBuilder / ContextCompressor
      ├── Planner / Router / SkillCompiler
      ├── ToolRegistry / ToolFacade / MCP Gateway
      ├── ExecutionPolicy / ApprovalService
      ├── AgentLoop / TaskGraphExecutor
      ├── VerifyService / PostconditionChecker
      ├── CheckpointStore / SessionStore
      └── EventSink → Panel / Run Drawer / Diagnostics
```

现有模块的复用关系：

- `tool_calling_client.py`：作为模型工具调用适配层。
- `agent_loop.py`：作为具有计划、执行、恢复语义的长任务循环。
- `ToolFacade`：作为所有工具的唯一执行入口。
- `ApprovalService`：作为写操作和高风险操作的审批入口。
- `HarnessRuntime`：作为运行记录、取消和事件来源。
- `AssistantSessionStore`：作为会话、任务摘要和 checkpoint 的持久化层。
- `ContextBundle` / `token_estimator.py`：作为上下文构建和估算基础。

## 5. 分阶段实施总表

建议按照依赖关系分为 9 个阶段（A1.5 是在已有 Agent Run 基础上的能力路由闸门）。每个阶段完成自动测试和人工验收后再进入下一阶段。

| 阶段 | 名称 | 重点目标 | 预计范围 | 前置依赖 | 退出条件 |
| --- | --- | --- | --- | --- | --- |
| A0 | 基线、契约和开关 | 固化当前行为，建立 V2 运行开关 | 模型、事件、测试基线 | 无 | 旧路径可回退，新增契约有测试 |
| A1 | 统一 Agent Run | 普通聊天、Skill、规划统一进入 `AgentRun` | Runtime/Controller/Service | A0 | 所有请求都有 run_id 和终态 |
| A1.5 | 模式与 Profile 能力路由 | 模式选择器连接真实 capability/provider，未接通时 fail closed | Profile/Router/Provider Adapter/UI | A1 | 每个模式都有可解释的可用/不可用状态，不能误用 Stub |
| A2 | 工具循环与安全审批 | 把 Tool Loop 接入普通对话 | ToolFacade/Policy/Approval | A1、A1.5 | 每个工具调用可审计、可取消、可审批 |
| A3 | 过程状态和运行抽屉 | 显示阶段、工具、等待和诊断 | Panel/Run Drawer | A1、A2 | 普通聊天完整显示活动流 |
| A4 | Token、压缩和费用 | 真实用量回流，自动压缩可解释 | LLM/Context/UI | A1 | 顶部数字、实际用量和压缩一致 |
| A5 | 完整历史和恢复 | 保存完整脱敏对话、草稿和 checkpoint | SessionStore/Schema | A1、A4 | 切换、重启、恢复均可验证 |
| A6 | 结果验证和任务闭环 | 增加目标、验收条件、Diff、校验 | Agent/Artifact/UI | A1、A2 | 任务有可证明的完成状态 |
| A7 | 子任务、工具发现和发布门 | 补齐高级能力和发布质量 | Runtime/Registry/QA | A1–A6 | 通过完整回归、性能、安全和人工验收 |

## 6. 分阶段详细计划

## A0：建立基线、契约和安全开关

### 目标

在不改变现有用户行为的前提下，为 Agent V2 建立可回退的边界，避免一次性重写普通聊天。

### 实施步骤

1. 新增功能开关 `AI_ASSISTANT_AGENT_RUNTIME_V2`，默认保持当前配置策略。
2. 明确旧路径和 V2 路径的入口：旧路径继续使用 `_start_chat()`，V2 路径进入 `AgentOrchestrator`。
3. 为 `AgentRun`、`AgentEvent`、`ContextUsage`、`ApprovalRequest` 建立最小 dataclass 和序列化版本。
4. 为每个新增对象编写状态合法性校验，禁止未知状态静默降级为 `IDLE`。
5. 把现有普通聊天、Skill、字幕、Teaching Animation、生成任务分别列出入口和退出点。
6. 建立基线测试：记录当前消息数量、Token 估算、历史切换、取消和错误行为。
7. 建立 Motion Graphic 专项 golden fixture：至少包含单页、三页、页面容器没有
   显式宽高、CSS 入场动画、纯色页面、透明页面、恶意 HTML 和空响应。
8. 为 HTML 生成协议增加版本号和能力声明，至少区分 `html_document`、
   `scene_json`、`stream_text`、`tool_call`；普通 Agent 不得根据文本猜测响应类型。
9. 建立 Provider capability contract：`supports_stream`、`supports_json_mode`、
   `supports_tool_calling`、`supports_usage`、`supports_abort` 和 `endpoint_style`。
   完整 URL、OpenAI-compatible base URL、`/v1` 拼接和模型名校验必须在 transport
   层一次归一化，不能让 Motion Graphic 的 HTML 请求承担 endpoint 修复逻辑。
10. 为模型配置保留迁移预览和回滚：检测重复 `/v1`、缺少 `/v1`、完整 chat-completions
    URL 时只在发送前生成规范化副本，不静默覆盖用户原始配置；HTTP 404、401、429、
    超时和模型不支持 JSON/工具调用要转换成结构化 capability error。

### 建议涉及文件

- `src/features/edit/ai_editing/assistant_events.py`
- `src/features/edit/ai_editing/session_models.py`
- `src/features/edit/ai_editing/assistant_service.py`
- `src/app/controllers/ai_edit_controller.py`
- 新增 `src/features/edit/ai_editing/agent_models.py`
- 新增 `src/features/edit/ai_editing/agent_feature_flags.py`
- 新增 `src/features/edit/ai_editing/provider_capabilities.py`
- 新增 `src/features/teaching_animation/ai/agent_adapter.py`

### 验证方法

```bash
python3 -m pytest tests/ui/test_p1_chat_free_conversation.py \
  tests/app/test_ai_edit_controller.py \
  tests/edit/ai_editing/test_session_catalog.py -q
python3 -m compileall -q src tests
git diff --check
```

### 退出条件

- V2 关闭时现有 AI 相关测试全部通过。
- V2 开启但尚未接入执行器时，普通请求能够明确显示“Agent V2 尚未启用”，不能静默改变结果。
- 所有新增模型均有 `to_dict/from_dict` 或等价的版本化序列化测试。
- Motion Graphic 在 V2 关闭和开启时都能生成相同的 sanitized HTML、页面数量和
  宿主渲染结果；只有事件/运行记录展示发生变化。
- 不配置真实 Provider 时，所有 fixture 仍能走到可验证的 transport、审批和渲染 gate；
  测试不得依赖用户 API key。

## A1：统一 Agent Run 和事件流

### 目标

让普通聊天不再只是一个脱离 Runtime 的后台线程，而是一次可观察、可记录、可取消的 `AgentRun`。

### 实施步骤

1. 新增 `AgentOrchestrator.start_task()`，输入为 `AssistantRequest + ContextScope + RunPolicy`。
2. Controller 不再直接控制多个 Worker 的业务状态，只负责把用户意图转为 invocation。
3. `AgentOrchestrator` 负责创建 `task_id/run_id`，并立即发出 `CREATED` 和 `GATHERING_CONTEXT` 事件。
4. 把现有 `_ChatWorker` 封装为 `ChatModelAdapter`，只负责模型 transport，不负责状态机和历史写入。
5. 将现有 `start_chat_run()` 从“仅构造 runtime invocation”升级为真正启动 Agent Run 的入口。
6. 为所有终态统一定义：成功、部分成功、失败、取消、等待用户、等待审批、可恢复暂停。
7. 每个运行事件都带 `run_id`，UI 只根据 `run_id` 丢弃过期回调，禁止旧 Worker 覆盖新会话。
8. 将 runtime 的运行记录、service 的状态快照和 panel 的展示状态统一映射，减少当前多处手动 `_set_state()`。
9. 新增 `TeachingAnimationAgentAdapter`：将 HTML 生成、场景 JSON 修改和普通问答
   映射成不同的 `AgentTask.kind`，但共享 run/event/cancel/checkpoint；适配器负责把
   HTML 完整响应收集成 artifact，不能让通用聊天流截断或重排 HTML。
10. `MotionGraphicBuildRequest` 创建时固定 `scene_revision`、画布、fps、插入帧、
    用户 brief 和 proposal 引用；这些字段进入 protected context，不得被摘要压缩删除。
11. 生成响应到达后先发 `artifact.staged` 和 `run.verification_started`，再由
    Teaching Animation 模块完成 sanitizer、quality gate、真实浏览器验证；验证成功
    才能发 `run.waiting_approval`。
12. 将“模型完成回答”和“场景已生成”分开：HTML 返回完成只代表 artifact 收齐，
    `SUCCEEDED` 必须等用户批准写入并完成 timeline postcondition。

### 事件类型最低集合

```text
run.created
run.context_started
run.context_ready
run.plan_started
run.plan_ready
run.waiting_user_input
run.waiting_approval
run.tool_started
run.tool_progress
run.tool_finished
run.verification_started
run.verification_finished
run.completed
run.failed
run.cancel_requested
run.cancelled
run.checkpoint_saved
```

### 验证方法

新增：

- `tests/edit/ai_editing/test_agent_run_events.py`
- `tests/edit/ai_editing/test_agent_orchestrator.py`
- `tests/app/test_ai_controller_agent_runtime_v2.py`

必须覆盖：

- 每个请求都有唯一 `run_id`。
- 同一会话同时发送第二条消息时，第一条不会覆盖第二条结果。
- 任意失败、取消和异常退出都有终态事件。
- 旧路径和 V2 路径都能恢复 UI 到可操作状态。
- 事件顺序符合状态机，不允许 `completed → running` 等非法逆序。
- HTML 生成的完整文档不会被流式 UI 的增量拼接、Markdown 清理或普通文本持久化破坏。
- 同一 Motion Graphic 请求的迟到响应、重复响应、取消后响应都不会生成第二张确认卡。

## A1.5：模式切换与 Profile 能力路由

### 目标

把底部模式选择器从“表达用户意图的 UI 控件”升级为可解释、可验证的能力入口。
模式本身不直接决定某个模型名；运行时根据 capability-specific active profile、
Provider contract 和策略状态解析出唯一执行路径。

### 实施步骤

1. 保留 `AssistantModeSpec`、稳定模式 ID 和 `AssistantRequest.mode`；`chat` 作为默认
   模式，未知/过期 ID 必须安全回退并记录诊断，不得静默改变为媒体生成。
2. 新增 `AssistantCapabilityRouter`（或等价 service），输出不可变的
   `CapabilityResolution`：`mode`、所需 capability、profile_id、provider_id、
   adapter、输入/输出类型、contract 版本、availability、reason 和 demo 标记。
3. 将 `active_profile_for_capability()` 作为运行时选择入口。Preferences 增加按
   capability 设置 active profile 的 UI；全局 active profile 只作为语言/兼容旧配置的
   fallback，不能覆盖用户已经选定的 TTS、ASR 或图片 Profile。
4. 固化模式到能力的路由表：

   | 模式 | 必需能力 | 结果边界 |
   | --- | --- | --- |
   | `chat` | `language` | 文本消息或 AgentRun 计划 |
   | `motion_graphic` | `language` + Teaching Animation adapter | `HtmlArtifact` → 浏览器验证 → 唯一确认卡 → Clip command |
   | `image` | `image_generation` | 图片 artifact，经过 consent/保存策略 |
   | `video` | `video_generation` | 异步视频 job/artifact，必须有真实 Provider |
   | `music` | `music_generation` | 音频 job/artifact，必须有真实 Provider |
   | `asr`（未来） | `speech_to_text` | Transcript artifact，不走聊天消息结构 |
   | `tts`（未来） | `text_to_speech` | Audio artifact，需试听/保存结果 |
   | `vision`（未来） | `vision_understanding` | 多模态输入 + 结构化分析结果 |
   | `embedding`（未来） | `embedding` | 向量/索引结果，不显示为普通文本生成 |

5. 模式下拉框的可用状态来自实时 `CapabilityResolution`。不可用模式可显示但必须置灰
   并提供“未配置/协议不兼容/未安装/被策略禁用/连接失败”的原因；切换模式时先做
   轻量解析，不发送真实模型请求。
6. 生成模式只允许调用声明了对应 modality 和 contract 的 Provider。通用
   `HttpGenerationProvider` 只有在 endpoint、认证、请求 schema、状态查询和取消接口
   全部通过探测/配置校验后才能启用；不能因填写了 URL、key、model ID 就默认可用。
7. deterministic Stub 仅保留给单元测试和开发 fixture。生产 UI、运行事件和结果卡都
   必须标出 `demo`；若没有真实 Provider，模式运行直接结束为 `FAILED/UNAVAILABLE`，
   不创建媒体 artifact、不写时间线。
8. 拆分 Profile 分类和能力标签：`vision_understanding` 不再与
   `image_generation` 共用一个“视觉模型”含义；Preferences 的分类、状态卡和错误
   文案同步更新，避免 Seedream 被误显示成 VLM。
9. 为不同协议建立独立 adapter：文本 chat、VLM 多模态消息、ASR、TTS、异步视频/音乐、
   Embedding 分别声明输入输出 schema、流式能力、usage、abort 和错误映射。未知协议
   或 404/401/429/超时必须转换为结构化 capability error。
10. 所有媒体模式都经过现有 consent gate；Motion Graphic 额外经过 sanitizer、
    浏览器验证和确认卡，不能因为用户选择了模式就跳过审批。

### 验证方法

新增或扩展：

- `tests/edit/ai_editing/test_assistant_capability_router.py`
- `tests/edit/ai_editing/test_provider_capability_contract.py`
- `tests/app/test_ai_edit_controller_modes.py`
- `tests/ui/test_ai_assistant_mode_availability.py`
- `tests/edit/ai_editing/test_generation_provider_fail_closed.py`

必须覆盖：

- 五个现有模式均使用稳定 ID，默认进入 `chat`，未知 ID 不会误触发媒体生成。
- 语言、图片、TTS 使用各自 capability-specific profile；切换语言 Profile 不影响其余能力。
- 未配置视频/音乐 Provider 时，模式显示不可用并停止在生成前，不产生 deterministic 成功结果。
- Seedream 只能解析为图片生成；视觉理解必须要求 VLM/本地视觉分析输入适配器。
- 完整 URL、OpenAI-compatible base URL 和 `/v1` 变体的规范化只发生在 transport 层，
  404/认证错误会显示可读 capability error。
- Motion Graphic 保持 HTML artifact、真实浏览器验证、单一确认卡和写入后置条件。
- ASR/TTS/Embedding 没有对应 UI 或 adapter 时不会被模式列表虚假宣称为“已接通”。

## A2：工具循环、权限和审批闭环

### 目标

把现有 `tool_calling_client.py`、`agent_loop.py`、`ToolFacade` 和 `ApprovalService` 接到普通对话，使“会回答”升级为“会安全执行”。

### 实施步骤

1. 为所有可调用工具建立 `ToolSpec`：名称、描述、输入 schema、输出 schema、风险级别、是否只读、影响范围、是否需要审批。
2. 建立 `ToolRegistry`，统一注册内置 Skill、编辑工具、分析工具、MCP 工具和诊断工具。
3. 普通聊天只向模型暴露当前 profile 和权限允许的工具，不把所有工具无条件暴露给模型。
4. 每次模型请求工具时，先经 `ToolFacade.invoke()`，不得直接调用 controller 或 timeline API。
5. 只读工具可直接执行；预览工具生成结果但不得写入；写工具必须生成 `ApprovalRequest`。
6. 审批卡必须展示：工具名称、对象范围、输入摘要、预期变更、风险等级、是否可撤销、有效期。
7. 审批 grant 绑定 `run_id + tool_name + scope + expiration`，不能被模型伪造或跨会话复用。
8. 工具结果必须返回结构化 `ToolResult`，包括成功、失败、部分成功、诊断和 artifact 引用。
9. 每个工具调用写入审计事件，但不得记录 secret、绝对路径和完整敏感 prompt。
10. 工具循环达到最大轮数、最大调用数、时间预算或费用预算时，终止并给出可读诊断。
11. 将 Motion Graphic 的“生成 HTML artifact”和“应用到时间线”登记为两个不同
    的能力：前者是无副作用的 staged operation，后者是高风险 host write；模型不能
    通过一句“确认”消息直接消费后者的 grant。
12. `WAITING_APPROVAL` 期间的新用户消息只允许执行三类动作：继续等待、取消当前
    方案、基于新意见生成替代方案；任何消息都不能隐式批准原方案。确认卡必须携带
    `run_id + artifact_id + scene_revision + artifact_hash`，批准时逐项匹配。
13. 审批批准后若浏览器验证、scene revision 或 timeline postcondition 任一失败，
    必须撤销/不提交本次写入，并将 grant 标记为 consumed/failed，不能自动重试写入。

### 权限矩阵

| 权限 | 允许读取 | 允许预览 | 允许写入 | 是否需要审批 |
| --- | --- | --- | --- | --- |
| `read_only` | 是 | 是 | 否 | 不适用 |
| `propose` | 是 | 是 | 否，生成 EditPlan | 应用时需要 |
| `apply_with_each_approval` | 是 | 是 | 单次批准范围内 | 每个高风险动作需要 |
| `trusted_session` | 按产品策略 | 按产品策略 | 仅限显式白名单 | 仍保留高风险动作审批 |

### 安全验证

新增：

- `tests/edit/ai_editing/test_tool_registry_policy.py`
- `tests/edit/ai_editing/test_agent_approval_flow.py`
- `tests/edit/ai_editing/test_tool_scope_isolation.py`

必须验证：

- 未授权工具不能被调用。
- 模型伪造 approval id、过期 approval id、跨 session approval id 均失败。
- 只读工具不能产生 timeline 或工程写入。
- 任意路径、secret、网络目标不在允许范围内时 fail closed。
- 工具异常、超时、取消后不产生半写入。
- 所有实际写入仍走 `Preview → ApprovalService → CommandStack → Undo/Redo`。
- Motion Graphic 只能在用户点击确认卡后调用 `MotionGraphicClipCommand`；
  普通聊天中的“可以/好/确认”不能在审批卡之外触发写入。
- 生成 HTML、预览 HTML、应用 HTML 三种权限互相隔离，artifact 可被查看但不可被
  当作已批准的工程变更。

## A3：思考状态、运行抽屉和可观察性

### 目标

让用户能够准确理解模型当前在做什么，同时避免暴露内部思维链。

### 实施步骤

1. 将 `set_thinking(bool)` 扩展为 `set_agent_phase(phase, label, detail, progress)`。
2. 将 `AgentEvent` 映射为顶部状态胶囊和 Run Drawer 条目。
3. 普通聊天必须在发送后立即创建一条运行记录，而不是只显示流式文本。
4. 每个工具调用显示名称、状态、持续时间、输入范围、输出摘要和失败码。
5. 等待审批时暂停计时和 Token 统计，明确显示“等待你的确认”。
6. 失败时展示用户可读说明；技术诊断放入可展开区域，并支持复制诊断摘要。
7. 运行抽屉支持：取消、重试、查看诊断、恢复、回到结果、查看关联消息。
8. 运行完成后保留历史活动记录，不因消息刷新而丢失。
9. 对 Motion Graphic 单独显示“HTML 已收齐 → 安全检查 → 浏览器验证 → 等待确认
   → 已写入时间线”的阶段链；不要把长 HTML 内容渲染到聊天气泡中，也不要把内部
   sanitizer 诊断伪装成模型思考内容。

### 推荐状态文案

| 内部阶段 | 用户文案 | 是否显示进度 |
| --- | --- | --- |
| `GATHERING_CONTEXT` | 正在收集上下文 | 可显示已加载块数 |
| `PLANNING` | 正在制定方案 | 可显示方案步骤数 |
| `TOOL_CALL` | 正在调用：{tool} | 显示当前工具 |
| `WAITING_APPROVAL` | 等待你的确认 | 不自动继续 |
| `EXECUTING` | 正在执行变更 | 显示已完成/总数 |
| `VERIFYING` | 正在校验结果 | 显示校验项 |
| `PAUSED_FOR_RESUME` | 任务已暂停，可继续 | 显示 checkpoint |

### 验证方法

新增：

- `tests/ui/test_ai_assistant_agent_timeline.py`
- `tests/ui/test_ai_assistant_thinking_phase.py`
- `tests/ui/test_ai_assistant_error_recovery.py`

人工验证：

1. 使用慢速模型，发送一条会触发多个工具的请求。
2. 确认发送后 200ms 内出现阶段提示。
3. 确认工具调用、等待审批和校验均有独立状态。
4. 确认向上滚动查看旧消息时不会被流式自动滚动强行拉到底部；只有用户当前位于底部时才自动跟随。
5. 确认错误页、运行抽屉和消息区显示同一个失败原因。

## A4：真实 Token、上下文预算和自动压缩

### 目标

将顶部 Token 从“本地估算数字”升级为“可解释的上下文窗口 + 实际调用用量 + 累计消耗”。

### 4.1 顶部显示规范

顶部建议同时显示两个指标，避免把“当前上下文窗口”与“整个会话累计消耗”混淆：

```text
上下文窗口 12.4k / 32k · 估算 · 将自动压缩
本次会话累计 38.7k tokens · 本轮 1.8k ↑ / 2.4k ↓
```

字段定义：

| 字段 | 含义 |
| --- | --- |
| `context_window_used` | 即将发送给模型的完整输入上下文 Token |
| `context_window_limit` | 当前 profile/model 的输入上下文上限 |
| `reserved_output_tokens` | 为模型输出预留的 Token |
| `prompt_tokens_actual` | Provider 返回的真实输入 Token |
| `completion_tokens_actual` | Provider 返回的真实输出 Token |
| `session_total_tokens` | 当前会话所有请求的累计用量 |
| `is_estimated` | 当前数字是否为估算值 |
| `compression_count` | 当前会话已执行的自动压缩次数 |

### 4.2 实施步骤

1. 扩展 `ComposerFooterState`，加入 prompt、completion、total、累计费用、估算标记和最后更新时间。
2. 扩展 `AssistantStateSnapshot`，携带统一 `ContextUsage`，避免 controller 和 panel 分别计算。
3. 将 `LLMClient.last_usage` 转换为 `LLMUsageEvent`，在普通流式和非流式请求完成后发送。
4. 流式请求必须消费 SSE 最终 usage；Provider 不返回 usage 时显示“估算”，不能伪装成真实用量。
5. 发送前使用当前 provider 的 tokenizer；没有 tokenizer 时使用 `token_estimator`，并标注估算。
6. 计算预算时预留输出 Token，不能只比较输入消息总量。
7. 设定压缩阈值：建议 80% 开始提示，90% 自动压缩，达到硬上限前必须完成压缩。
8. 自动压缩只改变发送给模型的上下文，不删除用户可见历史和本地保存的完整对话。
9. 压缩生成 `CompressionCheckpoint`，记录压缩前后消息数、Token 数、摘要版本和时间。
10. 压缩后重新计算并回填顶部；如果压缩后仍超限，继续裁剪最旧的低优先级上下文，并明确显示被省略的内容。
11. 用户手动点击压缩时给出预览，显示将保留的关键事实和被压缩的对话范围。
12. 压缩策略必须保留当前 Motion Graphic 的 protected context：项目画布、fps、
    当前 scene/clip、scene revision、用户最后一次明确生成/修改意图、待确认
    artifact hash 和页面时序策略；如果无法保留，应暂停并请求用户确认，而不是
    继续生成一个上下文不完整的页面。
13. HTML 长文档不计入聊天气泡的可见历史截断规则；上下文中使用 artifact 摘要和
    hash，重试时从 artifact store 读取完整文档，避免 500 字摘要破坏 HTML。

### 4.3 Token 验证

新增：

- `tests/edit/ai_editing/test_context_usage.py`
- `tests/edit/ai_editing/test_llm_usage_bridge.py`
- `tests/edit/ai_editing/test_chat_compression_checkpoint.py`
- `tests/ui/test_ai_assistant_context_footer.py`

必须覆盖：

- 空会话、只有系统提示、长用户消息、长 assistant 回复。
- 估算值和 provider 实际值同时存在时，UI 正确标注来源。
- Provider 不返回 usage 时不崩溃且保留估算。
- 压缩后发送消息绝不超过 profile 上限。
- 当前窗口 Token 和会话累计 Token 不混淆。
- 切换历史会话时 Token 统计同步切换。
- 旧会话没有 usage 字段时可以迁移并显示“暂无实际用量”。

## A5：完整历史、草稿和恢复

### 目标

让历史会话不仅能点击切换，而且能够可靠地继续对话、恢复运行和管理缓存。

### 5.1 存储策略

当前 `MessageSummary` 只保存摘要和最多 500 字正文，这会导致长会话切换后无法完全复原上下文。建议分层保存：

| 层级 | 保存内容 | 目的 |
| --- | --- | --- |
| 工程会话 | 完整的脱敏对话正文、消息元数据、任务引用、artifact 引用 | 工程内恢复和继续对话 |
| 本机缓存 | 最近 50 个完整的脱敏会话，按 LRU 淘汰 | 跨工程快速恢复历史 |
| 运行 checkpoint | 当前阶段、完成步骤、上下文 hash、resume metadata | 任务恢复，不保存 secret |
| 临时流式内容 | 仅内存；完成前不写正式 assistant 消息 | 防止半截回复污染历史 |

硬约束：

- 不保存 API key、secret、绝对机器路径和不可重建的大型媒体内容。
- 正文保存前经过 `sanitize_for_storage()`；发生脱敏时记录 `redacted=true`。
- 单条消息设置合理上限，例如 64k 字符；超长输出拆分为正文片段和 artifact 引用。
- 提供“清空本机 AI 历史”“导出脱敏历史”“删除当前会话”入口。
- 缓存上限默认 50，但允许在设置中修改并立即执行 LRU 淘汰。

### 5.2 实施步骤

1. 将 `MessageSummary` 升级为兼容的 `MessageRecord`，保留旧字段迁移。
2. 增加消息状态：`draft / streaming / completed / failed / cancelled`。
3. 每个会话独立保存 composer 草稿，切换会话时先保存当前草稿再恢复目标草稿。
4. 历史菜单显示标题、首条用户消息、更新时间、运行状态和是否有未完成任务。
5. 增加历史管理菜单：重命名、归档、删除、清空缓存、打开所在工程、复制会话摘要。
6. 增加会话搜索，至少支持标题、用户消息和 artifact/skill 名称搜索。
7. 切换会话前确认当前运行策略：运行中的任务不能被静默替换；应允许“保留后台运行 / 取消后切换”。
8. 切换完成后一次性恢复消息、草稿、Token 统计、运行抽屉和上下文 checkpoint。
9. 应用启动时扫描 `PAUSED_FOR_RESUME`，显示“继续 / 放弃 / 查看详情”。
10. 旧缓存损坏时只隔离损坏条目，不阻塞应用启动；保存采用原子替换并补充文件锁或进程冲突保护。
11. Motion Graphic 待确认方案必须持久化为“不可执行草稿”：保存 `artifact_id`、
    hash、scene revision、质量/渲染证据和用户确认状态，不保存可直接消费的 approval
    grant；重启后只能重新跑验证并重新显示确认卡。
12. 关闭应用时区分三种状态：模型请求可取消、HTML artifact 已收齐但未验证、
    已验证等待审批。三者恢复文案和恢复动作不同，均不得自动插入时间线。

### 5.3 历史验证

新增：

- `tests/edit/ai_editing/test_full_session_persistence.py`
- `tests/edit/ai_editing/test_session_cache_eviction.py`
- `tests/edit/ai_editing/test_session_migration.py`
- `tests/ui/test_ai_assistant_history_e2e.py`
- `tests/ui/test_ai_assistant_draft_restore.py`

必须覆盖：

- 新建会话、发送消息、关闭应用、重新打开、继续对话。
- 50、51、100 个会话的 LRU 淘汰，最终缓存严格不超过设定上限。
- 历史菜单展开、点击、活动会话标记和消息恢复。
- 切换会话后新请求不会带入旧会话消息。
- 历史搜索和重命名不改变消息正文。
- 归档会话默认不显示，但可以从归档入口恢复。
- 损坏缓存、旧版本缓存、无权限目录均有可读错误并可继续使用新会话。
- 应用重启后未完成任务出现恢复卡，而不是显示为成功或直接消失。
- 恢复待确认 Motion Graphic 后，页面数量、画布、sanitized hash 和浏览器版本证据
  不一致时必须重新生成/验证，旧卡片不能直接确认。

## A6：目标、验收条件和结果验证闭环

### 目标

把“模型返回一段话”与“任务完成”区分开。只有达到验收条件并通过校验，Agent Run 才能进入成功状态。

### 实施步骤

1. 用户提交请求时创建 `AgentTask`，从文本中提取或请求补充目标和范围。
2. 复杂任务在执行前显示简短计划：目标、步骤、预期产物、可能需要的权限。
3. 允许用户编辑计划、删除步骤或补充验收条件。
4. 每个写操作生成可审阅的 Diff 或预览 artifact。
5. 每个工具/Skill 声明 postcondition，例如：字幕轨道存在、素材已导入、timeline revision 已更新。
6. 执行完成后由确定性 `PostconditionChecker` 验证，而不是让模型自报成功。
7. 验证失败进入 `PARTIAL_SUCCESS` 或 `FAILED`，给出已完成项、未完成项和建议动作。
8. 结果卡统一提供预览、应用、撤销、重新规划、复制诊断和回到对应消息。
9. 记录 `evidence_refs`、`artifact_refs`、`revision_before/after` 和验证摘要。
10. 为 Motion Graphic 增加专用 postcondition：HTML artifact 可读取、sanitized hash
    与确认卡一致、页面数量可检测、每个候选页面至少有一帧视口内可见节点、真实
    浏览器捕获非崩溃、scene store 注册成功、graphics Clip 引用正确且时长/画布匹配。
11. 验证候选帧不能只测第 0 帧；至少覆盖首个可见帧、每个页面的中间帧、最后一页
    和页面边界帧。纯色/透明页面允许作为合法设计，但必须有独立的 DOM/页面可见性
    证据，不能用“允许 uniform”掩盖布局错误。
12. 将 HTML 质量、渲染错误和时间线错误分层展示：例如 sanitizer 拒绝、浏览器
    uniform frame、CSP/资源错误、scene stale、Clip 写入失败必须有不同 error code，
    便于重试正确阶段而不是整条链路盲目重跑。

### 验收示例

| 任务 | 成功条件 | 失败条件 |
| --- | --- | --- |
| 分析素材 | 分析 Job 成功、结果带素材/镜头引用 | 只有模型文本，没有真实分析证据 |
| 修改字幕 | EditPlan 通过 schema、预览和 revision 校验 | 直接修改模型或没有 undo |
| 生成场景 | 结构化场景通过 schema、资源引用有效 | 返回 JSON 但没有可预览 artifact |
| 生成 Motion Graphic HTML | raw/sanitized artifact、页面逐页捕获、确认 hash、Clip 引用和 postcondition 全部通过 | 只有 HTML 文本或单一首帧通过，后续页面为空/出画 |
| 导出文件 | 路径、格式、文件存在性和 probe 通过 | 仅显示“已导出”但文件不存在 |
| 普通问答 | 输出完整并记录实际用量 | 请求失败却被标记为成功 |

### 验证方法

新增：

- `tests/edit/ai_editing/test_agent_acceptance_criteria.py`
- `tests/edit/ai_editing/test_postcondition_checker.py`
- `tests/ai/test_motion_graphic_agent_adapter.py`
- `tests/ai/test_motion_graphic_multi_page_postconditions.py`
- `tests/ui/test_ai_assistant_result_actions.py`

必须覆盖成功、失败、部分成功、取消、revision 冲突、重复应用、undo/redo 和结果 artifact 缺失等路径。
Motion Graphic 还必须覆盖空 HTML、被清洗的 HTML、没有页面标记、三页以上页面、页面
容器没有宽高、后续页面出画、真实浏览器 uniform frame、CSP/资源失败和确认前修改需求。

## A7：子任务、工具发现和发布质量

### 目标

在核心闭环稳定后，再补齐专业 Agent 的高级能力；避免在普通聊天尚未可靠前引入复杂并发。

### 实施步骤

1. 在 Run Drawer 中显示父任务和子任务树，支持折叠和查看每个子任务输入/输出摘要。
2. 只允许无副作用的观察、检索、规划和 QA 子任务并行；所有写入仍汇聚成一个候选 EditPlan。
3. 增加能力面板：当前 profile、可用工具、所需权限、是否联网、是否支持流式/工具调用、费用提示。
4. 增加 MCP/Skill 连接状态：已连接、未配置、被策略禁用、连接失败、版本不兼容。
5. 增加模型 fallback 策略，但必须显示真实来源；不能把 deterministic fallback 伪装成模型结果。
6. 增加质量指标：首个状态延迟、首 token 延迟、运行时长、取消响应时间、恢复成功率、撤销率、人工评分、Token 误差。
   对 Motion Graphic 另加：HTML artifact 收齐耗时、sanitize 修改率、页面检测数、
   页面逐页验证通过率、uniform frame 分类数、浏览器捕获失败率、确认后写入成功率、
   stale 方案拦截数和后续页面可见率。
7. 将指标纳入 opt-in telemetry 或本地诊断，不默认上传原始 prompt 和媒体内容。
8. 更新 release evidence，加入 Agent V2 的功能、性能、安全、隐私、可访问性和回滚证据。

### 可选能力边界

终端、浏览器、计算机操作等属于 Codex 的通用能力，但 Topos 是专业媒体编辑器，不应为了“功能数量对齐”而强行加入。只有在以下条件满足后再考虑：

- 有明确的 Topos 用户场景。
- 有沙箱、路径 allowlist、网络策略和审批 UI。
- 有独立的安全测试、资源配额和回滚方案。
- 不会绕过现有 `ToolFacade → ApprovalService → CommandStack` 写入链路。

## 7. 关键文件改造建议

| 文件/模块 | 计划改造 |
| --- | --- |
| `src/features/edit/ai_editing/assistant_events.py` | 扩展消息、快照、用量、阶段和事件模型 |
| `src/features/edit/ai_editing/assistant_modes.py` | 保留模式稳定 ID 与显示规格；后续只作为模式注册表，不承载 Provider 选择 |
| `src/features/edit/ai_editing/assistant_capability_router.py`（新增） | 将模式解析为 capability-specific Profile、Provider adapter 和可用状态 |
| `src/features/edit/ai_editing/provider_profiles.py` | 增加按 capability 的 active profile、协议版本和输入输出契约校验 |
| `src/features/edit/ai_editing/ai_provider_catalog.py` | 拆分 `vision_understanding` 与 `image_generation`，声明每种 Provider 的 contract |
| `src/features/edit/ai_editing/assistant_service.py` | 管理 Agent Run 状态，不再让 Controller 直接驱动私有状态 |
| `src/app/controllers/ai_edit_controller.py` | 从多条特殊路径收敛到 `AgentOrchestrator`，保留适配层 |
| `src/features/edit/ai_editing/llm_client.py` | 统一 usage、stream usage、取消句柄和 provider 能力声明 |
| `src/features/edit/ai_editing/tool_calling_client.py` | 接入普通聊天，补工具事件和工具结果 schema |
| `src/features/edit/ai_editing/agent_loop.py` | 复用计划/执行/恢复机制，补事件 sink 和审批等待 |
| `src/features/edit/ai_editing/transcript_router.py` | 按 ASR capability 选择 Profile，区分远程 Provider 与本地 fallback |
| `src/features/edit/ai_editing/profile_tts_provider.py` | 按 TTS capability 选择 Profile，并提供试听/保存所需的 artifact 结果 |
| `src/features/edit/ai_editing/visual_semantic_provider.py` | 仅处理视觉理解，不把图片生成 Profile 当作 VLM |
| `src/features/edit/ai_editing/assistant_session_store.py` | 完整脱敏消息、草稿、任务、运行和恢复 checkpoint |
| `src/features/edit/ai_editing/session_models.py` | v2 → v3 兼容迁移，支持 MessageRecord/AgentTask |
| `src/features/edit/ui/ai_edit_assistant_panel.py` | 阶段状态、可控自动滚动、错误诊断、结果操作 |
| `src/features/edit/ui/ai_assistant_composer.py` | 上下文双指标、历史管理、能力与权限入口 |
| `src/features/edit/ui/ai_run_drawer.py` 或现有 Run Drawer | 普通聊天活动流、父子任务和恢复操作 |
| `src/features/ai_harness/runtime.py` | 统一 run 生命周期、事件、取消和终态 |
| `src/features/ai_harness/generation_adapter.py` | 统一媒体请求入口，但必须验证 modality 对应的真实 Provider contract |
| `src/features/ai_harness/generation_provider.py`、`http_generation_provider.py` | 视频/音乐/图片异步 job、状态查询、取消和错误映射；无 contract 时 fail closed |
| `src/features/edit/ai_editing/tool_facade.py` | ToolSpec、范围、审计和审批 grant 校验 |
| `src/features/teaching_animation/ai/agent_adapter.py` | 将 Motion Graphic HTML/scene 请求桥接到 AgentTask/AgentRun，保留专用响应协议 |
| `src/features/teaching_animation/ai/artifact_store.py` | 保存 raw/sanitized HTML、hash、质量和浏览器验证证据，不把长 HTML 塞进会话摘要 |
| `src/features/teaching_animation/render/html_page_deck.py` | 页面标记、宿主布局、时序和编辑器/导出/捕获共用契约 |
| `src/features/teaching_animation/render/html_validation.py` | 多页候选帧、uniform frame 分类和真实浏览器验证入口 |
| `src/features/teaching_animation/host_adapters.py` | 保持时间线/编辑器的只读渲染和显式写入边界 |

禁止事项：

- 不在 UI slot 中直接跑模型、解码、渲染或写 timeline。
- 不新建绕过 `ToolFacade` 的隐式工具入口。
- 不用一套新的 SessionStore 替换现有持久化体系。
- 不通过默认值吞掉未知状态、provider 错误或 schema 错误。
- 不把模型输出直接视为已完成的工程变更。

## 8. 测试与验证总表

### 8.1 单元测试

| 测试领域 | 必须验证 |
| --- | --- |
| Agent 状态机 | 合法转换、非法转换、终态、重复事件 |
| 事件流 | 顺序、run_id 隔离、旧 Worker 回调丢弃 |
| Tool Registry | schema、allowlist、参数校验、工具预算 |
| 审批 | scope、过期、重复、伪造、跨会话隔离 |
| Token | 估算、实际 usage、输出预留、累计用量 |
| 压缩 | 触发阈值、摘要、checkpoint、超限保护 |
| 历史 | 正文、脱敏、迁移、LRU、损坏文件 |
| 恢复 | checkpoint、上下文 hash、plan hash、取消恢复 |
| 结果验证 | postcondition、artifact、revision、undo/redo |
| 模式解析 | 稳定 mode ID、默认 chat、未知 ID、实时可用状态和原因 |
| Profile 路由 | capability-specific active profile、Profile/Provider 隔离、配置迁移 |
| Provider contract | 文本/VLM/ASR/TTS/媒体生成/Embedding 的 schema、usage、abort 和错误映射 |
| 失败闭环 | 未配置或协议不兼容时 fail closed，不产生 deterministic 假成功或空白 artifact |

### 8.2 UI 集成测试

最低测试文件建议：

```text
tests/ui/test_ai_assistant_agent_timeline.py
tests/ui/test_ai_assistant_context_footer.py
tests/ui/test_ai_assistant_history_e2e.py
tests/ui/test_ai_assistant_draft_restore.py
tests/ui/test_ai_assistant_error_recovery.py
tests/ui/test_ai_assistant_result_actions.py
tests/ui/test_ai_assistant_mode_availability.py
```

重点检查：

- 面板高度不会随消息数量无限撑开。
- 用户向上滚动时不会被流式回复强制拉到底部。
- 只有用户位于底部时才自动跟随新回复。
- Enter 发送、Shift+Enter 换行、发送期间输入锁定策略一致。
- 思考阶段、工具阶段、等待审批、完成状态显示一致。
- 历史菜单点击后消息、草稿、Token 和运行记录同时切换。
- 错误页的重试不会重复写入用户消息或产生重复任务。
- 新会话、历史切换和关闭面板不会泄漏 QThread。
- 底部模式选择器与 Profile/能力状态一致；不可用的视频/音乐模式显示原因且不能发送生成请求。
- 图片、TTS 等已配置 Profile 只影响对应 capability；切换语言模型不改变媒体 Profile。
- “视觉理解”和“图片生成”在 UI、路由和错误文案中明确区分。

### 8.3 集成与回归命令

本项目应优先使用当前工程虚拟环境的解释器，避免系统 `pytest` 或旧 shebang 指向
其他工作区：

```bash
./.venv/bin/python -m pytest ...
```

每个阶段至少运行：

```bash
python3 -m pytest tests/ui/test_ai_assistant_panel_p0_integration.py \
  tests/ui/test_ai_edit_assistant_m5_ui.py \
  tests/ui/test_p1_chat_free_conversation.py \
  tests/app/test_ai_edit_controller.py \
  tests/edit/ai_editing/test_session_catalog.py -q

python3 -m pytest tests/edit/ai_editing src/features/ai_harness/tests -q

./.venv/bin/python -m pytest tests/ai tests/render -q

python3 -m compileall -q src tests
git diff --check
```

合并前再运行：

```bash
python3 -m pytest tests/ui tests/app tests/edit/ai_editing src/features/ai_harness/tests -q

./.venv/bin/python -m pytest tests/ai tests/render tests/ui -q
```

如果完整测试出现与本次变更无关的既有失败，必须记录：测试名称、失败堆栈、是否可复现、是否阻断本批，而不能简单忽略。

### 8.4 人工验收矩阵

| 场景 | 操作 | 预期结果 |
| --- | --- | --- |
| 普通慢请求 | 使用慢速/高延迟模型发送请求 | 立即出现阶段状态，界面不被卡住 |
| 流式回复 | 回复过程中向上滚动 | 用户阅读位置保持，不强制跳到底部 |
| 取消 | 在上下文、模型、工具、审批四个阶段分别取消 | 状态正确终止，不出现迟到回复或半写入 |
| 失败重试 | 模拟 429、500、超时、无凭据 | 显示具体原因，重试不会重复消息 |
| 历史切换 | 建立两个多轮会话后互相切换 | 上下文、草稿、Token、运行记录完全隔离 |
| 应用重启 | 在工具执行或审批前关闭应用再打开 | 显示可恢复任务，用户可继续或放弃 |
| 压缩 | 构造超过上下文上限的长会话 | 自动压缩，原始历史仍可查看，实际发送不超限 |
| 权限 | 只读、提议、逐次审批分别执行写操作 | 未授权时不写入，审批卡展示范围和 Diff |
| Motion Graphic 确认 | 讨论 2 轮后发送“开始生成” | 只显示一次确认按钮；未点击前发送修改意见不会执行旧方案 |
| Motion Graphic 多页 | 生成 3–6 页并在编辑器、时间线、导出中切页 | 每页都能看到，页面边界帧不黑屏，时序由编辑器控制 |
| Motion Graphic HTML 安全 | 返回含 script、远程 URL、Markdown fence 的 HTML | sanitizer/quality gate 给出可修正原因，不能写入时间线 |
| Motion Graphic 失败恢复 | 模拟空响应、uniform frame、CSP、浏览器崩溃、scene stale | 错误分层，可重试正确阶段，不重复消息或重复插入 Clip |
| Motion Graphic 重启 | 在 HTML 已验证但待确认时关闭应用 | 恢复为不可执行草稿，重新验证并重新点击确认 |
| 模式切换 | 在普通对话、Motion Graphic、图片、视频、音乐之间切换 | 请求使用稳定 mode ID；运行中锁定；切换后不会串用上一模式的 Profile |
| 未接通媒体模型 | 选择视频或音乐，但未配置对应真实 Provider | 模式置灰或提交前明确不可用，不调用 deterministic Stub，不显示成功 |
| Profile 能力隔离 | 同时配置语言、图片、TTS，分别发起请求 | 各请求命中对应 capability-specific Profile，失败原因不被泛化为模型错误 |
| 视觉能力区分 | 配置 Seedream 后尝试图片理解 | Seedream 只用于图片生成；视觉理解提示需要 VLM/本地分析 Provider |
| 统一模型地址 | 输入完整 URL、base URL、带/不带 `/v1` 的地址 | transport 层规范化一次；原始配置不被静默覆盖，404/401/429 有结构化提示 |
| 缓存上限 | 连续建立 51 个会话 | 只保留最近 50 个，旧会话按 LRU 淘汰 |
| 数据安全 | 在消息中放入 secret、绝对路径和恶意工具参数 | 落盘脱敏，工具拒绝越权，日志无 secret |
| 线程清理 | 连续发送、取消、切换和关闭面板 | 无 QThread 泄漏、无崩溃、无 stale callback |

## 9. 性能、稳定性和安全指标

建议将以下指标作为发布门，而不是只依赖“功能能运行”：

| 指标 | 建议门槛 |
| --- | --- |
| 发送到首个用户可见阶段 | ≤ 200ms（本地 UI 路径，不含网络） |
| 取消后的 UI 状态响应 | ≤ 200ms；底层请求按 provider 能力停止或进入隔离回收 |
| 旧 Worker 覆盖新请求 | 0 次 |
| 实际发送上下文超出 provider 上限 | 0 次 |
| 未审批写操作 | 0 次 |
| 历史切换上下文串线 | 0 次 |
| 缓存超过配置上限 | 0 次 |
| 失败任务被标记为成功 | 0 次 |
| 关闭应用后的 orphan worker | 0 次 |
| 历史正文恢复成功率 | 100%（被脱敏字段必须有明确标记） |
| 可恢复任务 checkpoint | 所有执行中任务至少一个有效 checkpoint |
| Motion Graphic 未点击确认却写入时间线 | 0 次 |
| Motion Graphic 页面可见率 | 100%（每个检测到的页面至少一帧视口内可见） |
| Motion Graphic 错误被误报为成功 | 0 次 |
| Motion Graphic 重复 Clip 插入 | 0 次（同一 artifact/run 幂等） |

性能验证还必须关注：

- 大量消息渲染时 UI 内存和滚动性能。
- 流式输出频率过高时 Qt 信号合并和刷新节流。
- 历史缓存读写是否阻塞 UI。
- Token 估算是否重复遍历全部上下文。
- 多任务并行时 Worker、网络连接和临时文件的释放。
- AI 任务是否争用 ModernGL/播放/导出线程。

## 10. 数据迁移与回滚策略

### 10.1 数据迁移

1. `SessionCatalog` 增加 schema version，旧 v1/v2 数据只读迁移，不直接覆盖原文件。
2. 旧 `MessageSummary` 转为 `MessageRecord` 时保留原摘要字段，并标记 `content_complete=false`。
3. 旧会话没有 `task_id/run_id/usage` 时生成空值，不伪造历史运行信息。
4. 自动压缩 checkpoint 新增字段必须有默认值和向后兼容读取。
5. 迁移失败只隔离单个会话，不阻塞其他历史和新会话。

### 10.2 回滚

1. `AI_ASSISTANT_AGENT_RUNTIME_V2=false` 可以立即回到旧聊天路径。
2. 新事件记录即使旧 UI 不认识，也必须保留在持久化数据中，不能在降级时删除。
3. 新版本首次写入前备份旧 session 文件或使用原子替换保留上一版本。
4. 如果 Provider 的工具调用能力不可用，自动退化为只读回答或 Skill 建议，并明确显示来源。
5. 任何涉及时间线写入的回滚都必须经过现有 Undo/Redo 和工程 revision 检查。
6. V2 迁移期间保留 `start_html_generate_chat()`、Motion Graphic 专用 response handler
   和现有确认卡的兼容适配层；只有在 Agent Adapter 的 golden tests、真实浏览器多页
   tests 和 GUI 验收全部通过后，才允许删除旧直连路径。
7. 如果 V2 运行中断，已生成的 HTML artifact 可以保留为只读诊断，但不能自动恢复为
   可写场景或重新插入 Clip。

## 11. 实施顺序建议

推荐实际开发时按以下小步提交，每一步都保持可运行：

```text
Step 1  新增 AgentTask/Run/Event/Usage 数据模型和 V2 开关
Step 1.5  实现 AssistantCapabilityRouter、Profile contract 和模式可用状态；无真实 Provider 时 fail closed
Step 2  将普通聊天包成最小 AgentRun，但暂不执行工具
Step 3  将普通聊天 usage 接入顶部，补估算/实际标识
Step 4  将 Tool Registry 和 ToolFacade 接入只读工具
Step 5  接入规划、工具事件和 Run Drawer
Step 6  接入写工具审批、Diff、postcondition 和撤销
Step 7  将完整脱敏消息、草稿和 checkpoint 写入历史
Step 8  实现取消、暂停、重启恢复和失败重试
Step 9  增加历史搜索、重命名、归档、删除和缓存设置
Step 10 增加任务验收条件、artifact 和验证结果
Step 11 增加子任务、工具发现、MCP 状态和质量指标
Step 12 完成真实模型、人工 GUI、性能、安全和发布证据验收
```

Motion Graphic 建议在 Step 2–6 之间插入一条专用迁移线，避免它被普通聊天重构
“顺便带过”：

```text
MG-1  固化 MotionGraphicBuildRequest/HtmlArtifact 和现有 HTML response adapter
MG-2  将生成、验证、确认、写入映射为 AgentEvent，但保持单一确认卡
MG-3  增加 protected context、artifact hash、scene revision 和 stale 处理
MG-4  增加真实 Chromium 的逐页/边界帧/透明帧回归与后续页面可见率指标
MG-5  增加待确认草稿的重启恢复（不可执行）和取消/修改替代方案语义
MG-6  最后才切换默认 V2，旧路径保留一个版本周期用于回滚
```

每个 Step 的提交原则：

- 单次提交只解决一个可验证问题。
- 先补领域模型和测试，再接 UI。
- 模式选择器、Profile 配置、Provider adapter 和 AgentRun 分层实现；不能把“下拉框可选”当作能力完成。
- UI 只订阅事件，不在控件中实现业务逻辑。
- 变更核心状态、持久化、线程或权限时必须同时验证失败和取消路径。
- 不与用户现有未提交修改冲突；无关文件不纳入提交。

## 12. 最终验收清单

### P0 必须全部通过

- [ ] 普通聊天、Skill 和工作流都能形成统一 `AgentRun`。
- [ ] 每次运行都有完整事件流、终态和诊断。
- [ ] 普通聊天可以调用受控工具，并经过 ToolFacade。
- [ ] 写操作有工具级审批、范围和过期时间。
- [ ] 顶部同时显示上下文窗口和会话累计 Token。
- [ ] 实际 provider usage 能回流；无 usage 时明确标记估算。
- [ ] 超过阈值自动压缩，发送上下文不超过上限。
- [ ] 取消、失败和应用关闭都不会产生迟到结果或半写入。
- [ ] 执行中任务有 checkpoint，可恢复或明确放弃。
- [ ] 底部模式选择器使用稳定 mode ID，默认是普通对话；运行中锁定，未知值不会误触发媒体生成。
- [ ] 每个可执行模式都能解析到真实 Provider、协议和 capability-specific Profile；未接通时显示原因并 fail closed。
- [ ] 视频/音乐/图片没有真实 Provider 时不会使用 deterministic Stub 伪装成功，也不会创建假 artifact。
- [ ] Profile 的 `vision_understanding` 与 `image_generation` 已拆分；Seedream 不会被当作 VLM。
- [ ] Motion Graphic 的 HTML 生成仍是非 JSON artifact 流程，确认前不写入时间线。
- [ ] Motion Graphic 的画布来自当前时间线，页面/视觉/页内 CSS 动画仍由模型自由设计。
- [ ] Motion Graphic 的页面切换时序仍由编辑器控制，模型不会重新引入固定 JS 定时器。
- [ ] Motion Graphic 多页真实浏览器验证覆盖每个页面，确认卡只出现一次。

### P1 必须全部通过

- [ ] 思考提示升级为阶段化安全状态。
- [ ] Run Drawer 显示普通聊天的工具和子任务活动。
- [ ] 历史保存完整脱敏正文，而不是仅 500 字摘要。
- [ ] 历史支持切换、搜索、重命名、归档、删除和 LRU 缓存。
- [ ] 草稿、Token、压缩 checkpoint 和运行记录随会话恢复。
- [ ] 复杂任务显示计划、验收条件、Diff、artifact 和验证结果。
- [ ] 失败支持重试、诊断和可恢复暂停。
- [ ] 语言、图片、TTS、ASR、视觉和 Embedding 按 capability-specific active profile 路由，互不串用。
- [ ] 文本、VLM、ASR、TTS、媒体生成和 Embedding 使用各自输入输出 contract；404/401/429/超时可解释。
- [ ] 待确认 Motion Graphic 在用户继续修改、取消或应用重启后都不会自动执行旧方案。

### P2 可在核心稳定后完成

- [ ] 子 Agent 和并行只读子任务可见且可控。
- [ ] 工具发现、MCP 状态、模型能力和费用提示统一展示。
- [ ] 质量指标、人工反馈、撤销率和发布证据形成闭环。
- [ ] 根据 Topos 的真实用户场景评估是否加入浏览器、终端等外部能力。

## 13. 预期结果

完成本计划后，AI Assistant 应从当前的：

```text
“能聊天、能路由、能启动一些 AI 工作流的编辑器面板”
```

升级为：

```text
“能够理解任务、展示计划、受控调用工具、等待审批、执行变更、校验结果、
保存完整会话、支持取消和恢复，并且每一步都可追踪、可解释、可撤销的专业 Agent 工作台”
```

在模型能力层面，还必须达到：

```text
模式选择 ≠ 模型名称选择
模式选择 → 能力解析 → 对应 Profile/Provider → 合同验证 → AgentRun/Artifact/Verify
未配置或未接通 → 明确不可用与修复建议，不生成假成功、不写入、不留下空白结果
```

最终目标不是复制 Codex 的所有工具，而是吸收专业 Agent 的核心方法：

**任务化、可观察、可审批、可恢复、可验证、可追溯。**
