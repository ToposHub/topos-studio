# AI Assistant 剩余差距与接线计划

> 日期：2026-08-14  
> 状态：基于真实 API 多模式测试（23 项全通过）后的差距审计  
> 范围：V2 纯逻辑层已交付后的"接线缺口"、生产路径未接入、UI 可观察性、Agent 执行闭环  
> 前置文档：`docs/ai_assistant_codex_parity_optimization_plan_2026-08-14.md`（V2 架构骨架）  
> 对标对象：Codex / Claude Code 等专业 Agent 工具的执行成熟度  

---

## 1. 文档定位

本文档是 `ai_assistant_codex_parity_optimization_plan_2026-08-14.md`（下称"原计划"）的**后续差距分析**。

原计划定义了 9 个阶段（A0–A7 + A1.5），目标是把 AI Assistant 从"聊天编辑面板"升级为"专业 Agent 工作台"。截至 2026-08-14，原计划的**纯逻辑层（17 个新模块 + 460 单元测试）已全部交付**，并通过了 23 项真实 API 端到端验证。

但真实测试暴露了一个核心事实：**纯逻辑层 ≠ 用户可感知的能力**。大量代码存在于 flag 后面或从未被生产路径调用。本文档系统记录这些"接线缺口"，并给出分阶段实施计划。

### 本文不重复原计划已完成的内容

原计划的 A0（基线 + feature flag）、A1（AgentRun 模型）、A1.5（能力路由）、A2（工具循环逻辑）、A3（阶段文案）、A4（Token 预算）、A5（MessageRecord）、A6（PostconditionChecker）、A7（发布门）的**纯逻辑实现**均已完成并有测试覆盖。本文档只记录"逻辑已就绪但未接入生产"的缺口。

---

## 2. 已完成基线（真实 API 验证状态）

以下能力已通过真实 API（glm-5.2 / DeepSeek-v4-flash / 火山方舟 Seedream / 豆包 Seed-TTS）端到端验证：

| 能力 | 验证结果 | 验证方式 |
|------|---------|---------|
| 普通对话（6 种风格 + 多轮 + 取消 + Profile 切换）| 9/9 ✅ | `scripts/v2_real_chat_smoke.py` |
| 能力路由解析（7 个模式 fail-closed 验证）| 7/7 ✅ | `scripts/v2_real_multimode_smoke.py` |
| Motion Graphic HTML 生成（3 种动效风格）| 3/3 ✅ | 同上 |
| 图片生成（Seedream，2 个 prompt）| 2/2 ✅ | 同上 |
| 语音合成（豆包 TTS，中文 + 中英混合）| 2/2 ✅ | 同上 |

### 已修复的真实 Bug

| Bug | 根因 | 修复 | commit |
|-----|------|------|--------|
| Motion Graphic 全量空回复 | glm-5.2 reasoning 烧光 max_tokens 预算（5978/6000 token 用于推理，正文为 0）| 扩展 `_request_thinking_options` 覆盖 glm-5.x / glm-4.6 | `04c0d638` |
| 取消后迟到结果记为 SUCCEEDED | `_on_cancel` 未触达 V2 orchestrator | `request_cancel_for_session` + driver 复检 + delta 丢弃 | `041eae59` |
| 终态 run 元数据可被旁路修改 | side-channel 绕过 `with_state` | record_usage/stage_artifact/save_checkpoint 加 is_terminal 守卫 | `041eae59` |
| DEMO_ONLY 媒体生成可运行 | demo stub 不在 BLOCKING 集合 | ChatRunDriver 非 chat 模式 demo_only 硬拒 | `041eae59` |

---

## 3. 差距分析（按严重度排序）

### 🔴 S0：V2 未接入生产路径（核心价值未交付）

**现状**：`AI_ASSISTANT_AGENT_RUNTIME_V2` 环境变量默认 OFF。即使开启，也只有 `chat_mode == "normal"` 的普通对话走 V2。所有其他模式（Motion Graphic / 图片 / 语音）走独立 legacy 管线，完全不经过 AgentRun。

**影响**：用户在编辑器中看到的 AI 助手行为，与添加 V2 代码之前**完全一致**。所有改进（事件流、终态保证、取消隔离、token 流回）都在 flag 后面。

**具体未接线的组件**：

| V2 组件 | 代码位置 | 生产调用方 | 状态 |
|--------|---------|-----------|------|
| `AgentOrchestrator` | `agent_orchestrator.py` | 仅 `_get_v2_chat_driver` 懒构造 | ⚠️ 仅 chat |
| `ChatRunDriver` | `chat_run_driver.py` | 仅 `_start_chat_v2` | ⚠️ 仅 chat |
| `AgentToolLoop` | `agent_tool_loop.py` | **零** | ❌ 死代码 |
| `MotionGraphicRunCoordinator` | `motion_graphic_run.py` | **零** | ❌ 死代码 |
| `PostconditionChecker` | `postcondition_checker.py` | **零** | ❌ 死代码 |
| `AgentMetricsCollector` | `agent_metrics.py` | **零** | ❌ 死代码 |
| `ToolInvocationRunner` | `tool_invocation.py` | **零**（仅被 AgentToolLoop 引用）| ❌ 间接死代码 |
| `AgentPhasePresenter` | `agent_phase_presenter.py` | **零** | ❌ 死代码 |
| `active_run_id()` 隔离机制 | `agent_orchestrator.py:476` | **零**（无 UI 订阅者）| ❌ 死代码 |

**证据**：`grep -rn "AgentToolLoop\|MotionGraphicRunCoordinator\|PostconditionChecker\|AgentMetricsCollector" src/app/ src/features/teaching_animation/` → 仅定义和 `__all__`，无实例化。

---

### 🔴 S1：无可观察的运行抽屉（Run Drawer）

**现状**：`AgentEvent` 有 19 种事件类型，`subscribe()` 机制完备，事件流数据完整。但**没有任何 UI 组件订阅这个事件流并渲染**。

**用户看不到的信息**：
- Agent 当前阶段（CREATED → GATHERING_CONTEXT → PLANNING → EXECUTING → VERIFYING）
- 调用了什么工具、工具返回了什么
- 正在等待什么审批、审批的风险等级
- 实时 token 消耗（prompt / completion / 窗口占比）
- 产出了什么 artifact（hash 引用、是否已确认）

**与 Codex 的差距**：Codex 最核心的体验是**全程透明**——用户实时看到 Agent 的每一步思考、工具调用和结果。Topos 有完整的数据管道，但缺最后一层渲染。

**涉及的文件**（需要新建或修改）：
- `src/features/edit/ui/ai_assistant_panel.py`（主面板，需嵌入 Run Drawer 区域）
- `src/features/edit/ui/` 下需新建 run drawer 组件
- `src/app/controllers/ai_edit_controller.py`（需 `subscribe` V2 orchestrator 并转发事件到 UI）

---

### 🟡 S2：没有真正的多步任务执行（工具循环未启用）

**现状**：`AgentToolLoop` 已实现模型驱动的工具循环（最多 8 轮迭代、16 次工具调用），`ToolFacade` 有完整的只读工具集（`get_project_snapshot`、`search_assets`、`inspect_subtitle_track` 等）。但 chat 路径只做 `client.complete/stream`——**Agent 无法在对话中调用工具**。

**影响**：
- Agent 不能自己查上下文（用户必须手动描述项目状态）
- Agent 不能搜索素材、检查字幕、分析场景
- 无法实现"给一个任务，Agent 自己拆步骤、调工具、验证、交付"的闭环

**与 Codex 的差距**：Codex 的力量来自 Agent 在循环中调用工具（读文件、搜索、运行命令）。这是"聊天"和"Agent"的本质区别。

**接线缺口**：
- `_start_chat_v2` 目前只走 `ChatRunDriver.run_chat`（纯 complete/stream）
- 需要在 driver 中增加"检测到 tool_call 能力时切换到 AgentToolLoop"的分支
- `ToolFacade` 需要 EditSession 实例（目前 V2 driver 不持有 session 引用）

---

### 🟡 S3：Motion Graphic 仍是独立 legacy 管线

**现状**：Motion Graphic HTML 生成走 `start_html_generate_chat` → legacy `_ChatWorker`（`chat_mode="teaching_html_generate"`），不经过 AgentRun。

**未生效的安全保障**：

| 保障 | 逻辑是否就绪 | 运行时是否生效 |
|------|:---:|:---:|
| 单一确认门（confirm 前不写时间线）| ✅ `MotionGraphicRunCoordinator.confirm_and_write` | ❌ coordinator 未接线 |
| 陈旧场景检测（scene_revision 变更后旧方案 stale）| ✅ `scene_is_stale` | ❌ 运行时无人调用 |
| HTML artifact 默认不可写 | ✅ `HtmlArtifact.writable=False` | ❌ artifact 模型未用于生产 |
| HTML 不内联聊天日志（只存 hash 引用）| ✅ `artifact_ref_from_html` | ❌ 实际 HTML 直接进 response handler |
| 渲染验证证据（必须通过浏览器验证才能写）| ✅ `has_verification_evidence` | ❌ 未接线 |

**影响**：Motion Graphic 是唯一涉及写时间线的 AI 模式，安全风险最高，但运行时完全没有 V2 的保护。

---

### 🟡 S4：无 checkpoint 持久化与崩溃恢复

**现状**：`AgentCheckpoint` + `export_state` / `restore_state` 已实现内存级导出/恢复。但**不落盘**——应用崩溃或工程切换后，所有进行中的 run 全部丢失。

**涉及的缺口**：
- `export_state()` 的输出需要写入 session store（磁盘）
- 应用启动时需要 `restore_state()` 恢复未完成的 run
- `PAUSED_FOR_RESUME` 状态的 run 需要宿主重新校验后才能继续（红线 5：不恢复旧 approval）

---

### 🟡 S5：reasoning 模型集成脆弱

**现状**：`_request_thinking_options` 对 reasoning 模型的处理是**逐模型 hardcode 前缀匹配**（`deepseek-v4-flash`、`glm-5.x`、`glm-4.6`）。

**风险**：
- 换一个新 reasoning 模型（如 Qwen3-Thinking、glm-6）→ 又会空回复
- 列表需要手动维护，容易遗漏

**更健壮的做法**：在 `OpenAICompatClient.complete` 中检测 `finish_reason == "length" and not content and reasoning_content`，自动以 `thinking=disabled` 重试一次。这样不依赖模型名前缀，对新模型也有效。

---

### 🟢 S6：历史会话仍是 500 字摘要

**现状**：`MessageRecord`（完整脱敏正文，64000 字符上限 + sanitize）已实现。但 `SessionStore.add_message_summary` 仍然只存 `text[:500]`。

**影响**：长回复被截断，用户切换历史后看不到完整内容。

**修复**：`_record_assistant_message` 改用 `MessageRecord.from_agent_message()` 存完整正文。

---

### 🟢 S7：图片 / 语音产物脱离统一事件流

**现状**：图片生成走独立 `GenerationService` → `GenerationBin`，TTS 走独立 provider。这些产物不在 AgentRun 事件流中展示。

**影响**：用户看不到"Agent 刚生成了 1 张图片（耗时 20s，消耗 X token）"的统一视图。

**理想状态**：媒体生成也应该创建 AgentRun，stage artifact，在 Run Drawer 中展示。

---

### 🟢 S8：无费用 / 配额反馈

**现状**：`CostLedger` 存在但用户看不到累计费用。真实 API 测试显示每次对话消耗 93–1554 token，但没有 UI 汇总。

---

## 4. 与 Codex 的差距对比

### 4.1 Topos 领先的维度（不应丢弃）

| 能力 | Topos | Codex |
|------|-------|-------|
| 多模态生成（图片 / 语音 / 视频 / HTML 动效）| ✅ | ❌ 纯文本/代码 |
| GPU 渲染管线（GLSL / ModernGL / 4K 实时）| ✅ | ❌ |
| 时间线非线性编辑上下文 | ✅ | ❌ |
| 色彩 / LUT / HDR 领域专业能力 | ✅ | ❌ |

### 4.2 Topos 落后的维度（需补齐）

| 维度 | Codex | Topos 现状 | 差距性质 |
|------|-------|-----------|---------|
| 自主多步执行 | 给任务→工作 10 分钟→调 N 个工具→交付 | 一问一答，无工具循环 | 🔴 核心 |
| 全程透明 | 实时显示每个思考/工具调用/结果 | 事件流有数据，无 UI 渲染 | 🔴 核心 |
| Diff 审批 | 显示精确变更，approve/reject | ApprovalService 有逻辑，无 UI | 🟡 机制有 |
| 自我纠错 | 检测错误→重试→换策略 | 无（失败即终止）| 🔴 核心 |
| 上下文感知 | 读代码库、搜索引用、理解结构 | 工具定义有，chat 不调用 | 🟡 能力有 |
| 可恢复会话 | 崩溃后恢复、跨重启续作 | export/restore 有逻辑，不落盘 | 🟡 机制有 |
| 结果验证 | 跑测试证明成功 | PostconditionChecker 有，未接线 | 🟡 机制有 |

### 4.3 一句话总结

> Codex 是"能自主干活的 Agent"，Topos 现在是"能聊天 + 能调生成 API 的面板"。V2 纯逻辑层让 Topos *具备*了变成前者的骨架，但还没把骨头连起来。

---

## 5. 分阶段实施计划

### 总表

| 阶段 | 名称 | 目标 | 前置依赖 | 预估工作量 |
|------|------|------|---------|-----------|
| B0 | V2 默认开启 + 安全网 | flag 转 ON，验证生产路径无回归 | 无 | 小 |
| B1 | Run Drawer 可观察性 | UI 订阅事件流，显示阶段/工具/token | B0 | 中 |
| B2 | 工具循环接入 chat | Agent 在对话中调用只读工具 | B0 | 中 |
| B3 | Motion Graphic 接入 V2 | HTML 生成走 AgentRun + 确认门 | B1 | 中 |
| B4 | Checkpoint 持久化 + 恢复 | 落盘 + 重启恢复 | B0 | 中 |
| B5 | reasoning 模型健壮化 | 自动检测空回复 + 重试 | 无 | 小 |
| B6 | 历史完整正文 | MessageRecord 替换 500 字摘要 | 无 | 小 |
| B7 | 媒体生成统一事件流 | 图片/语音纳入 AgentRun | B1 | 大 |

---

### B0：V2 默认开启 + 安全网

**目标**：把 `AI_ASSISTANT_AGENT_RUNTIME_V2` 默认值从 OFF 改为 ON，确保普通对话走 V2 路径，且行为与 legacy 完全一致。

**实施步骤**：
1. `agent_feature_flags.py`：`_DEFAULT = True`。
2. 全量回归：`tests/ui/test_p1_chat_free_conversation.py` + `tests/app/test_ai_edit_controller.py` 必须全绿。
3. 人工验证：发送普通对话，确认回复正常、流式正常、取消正常。
4. 保留 fallback：V2 driver 构造失败时自动回退 legacy（已有逻辑）。

**退出条件**：
- V2 默认开启后，所有现有测试通过。
- 普通对话的用户可见行为与 legacy 完全一致。

---

### B1：Run Drawer 可观察性

**目标**：新建 UI 组件订阅 V2 orchestrator 事件流，实时显示 Agent 的阶段、工具调用、token 消耗和 artifact。

**实施步骤**：
1. 新建 `src/features/edit/ui/run_drawer.py`：一个可折叠面板，渲染 AgentEvent 流。
2. `AgentPhasePresenter.present_event()` 已就绪——直接用它生成阶段文案。
3. `ai_edit_controller.py`：在 `_start_chat_v2` 成功后 `self._v2_orchestrator.subscribe(self._on_v2_event)`，转发到 panel。
4. `_on_v2_event(event)`：根据 `event.kind` 更新 Run Drawer（阶段进度条 / 工具调用卡片 / token 数字 / artifact 预览）。
5. 接入 `active_run_id()` 过滤陈旧回调（当前是死代码，这里激活它）。

**退出条件**：
- 普通对话进行时，Run Drawer 实时显示阶段流转。
- token 消耗数字实时更新（实测值，非估算）。
- 取消后 Drawer 正确显示 CANCELLED 终态。

---

### B2：工具循环接入 chat

**目标**：当用户在普通对话中提问涉及项目上下文的问题时，Agent 能自动调用只读工具获取信息，而非要求用户手动描述。

**实施步骤**：
1. `ChatRunDriver`：检测 profile 的 `modalities` 含 `tool_call` 时，在 plan_ready 后切换到 `AgentToolLoop.run()`。
2. `ToolFacade` 需要持有 `EditSession`——在 `_get_v2_chat_driver` 中注入。
3. 工具循环内所有写工具结构性 DENIED（已有逻辑：`AgentToolLoop` 不传 approval）。
4. Run Drawer（B1）展示工具调用卡片（工具名、参数摘要、结果摘要）。
5. 预算控制：`AgentToolLoopBudget(max_iterations=8, max_tool_calls=16)` 已就绪。

**退出条件**：
- 问"当前时间线有几个素材"→ Agent 调用 `get_project_snapshot` → 基于真实数据回答。
- 工具调用全程在 Run Drawer 可见。
- 只读工具无需审批；写工具被 DENIED 并显示原因。

---

### B3：Motion Graphic 接入 V2

**目标**：HTML 生成走 AgentRun，享受单一确认门 + stale 检测 + artifact hash 引用 + 渲染验证。

**实施步骤**：
1. `TeachingAnimationModule._trigger_html_generate`：改为通过 `MotionGraphicRunCoordinator.start_build()` 创建 run。
2. HTML 返回后 `stage_html()`（emit artifact.staged + verification_started）。
3. 渲染验证通过后 `record_verification()`。
4. 用户确认后 `confirm_and_write()`（检查 staged + evidence + not stale → writable=True）。
5. 宿主在 writable=True 后发出 `MotionGraphicClipCommand`。
6. Run Drawer 展示确认卡片（含 HTML 预览缩略图 + 页数 + viewport）。

**退出条件**：
- HTML 生成全程在 Run Drawer 可见（staged → verified → waiting_approval → written）。
- 用户修改场景后，旧 HTML 标记 stale，确认被阻断。
- HTML 内容不内联聊天日志（只存 hash 引用）。

---

### B4：Checkpoint 持久化 + 恢复

**目标**：应用崩溃或工程切换后，未完成的 run 可恢复或明确放弃。

**实施步骤**：
1. `AgentOrchestrator.export_state()` 输出写入 session store（JSON 文件）。
2. 应用启动 / 工程打开时 `restore_state()` 恢复 run。
3. `PAUSED_FOR_RESUME` 的 run 显示为"已暂停，需重新确认"。
4. 红线 5 保证：恢复时不恢复 WAITING_APPROVAL 的已授权 grant（结构性保证：AgentRun 无 grant 字段）。

**退出条件**：
- 对话中途关闭工程再打开 → run 状态恢复。
- 恢复后无自动授权（必须重新确认）。

---

### B5：reasoning 模型健壮化

**目标**：不再依赖模型名前缀列表，自动检测空回复 + 重试。

**实施步骤**：
1. `OpenAICompatClient.complete`：捕获响应后检测 `finish_reason == "length" and not content`。
2. 若检测到且当前未禁用 thinking → 以 `thinking={"type":"disabled"}` 重试一次。
3. 保留现有前缀列表作为快速路径（避免已知模型的额外往返）。

**退出条件**：
- 新接入的 reasoning 模型（未在前缀列表中）也能自动产出内容。
- 单元测试覆盖自动重试逻辑。

---

### B6：历史完整正文

**目标**：历史会话保存完整脱敏正文，而非 500 字摘要。

**实施步骤**：
1. `_record_assistant_message`：改用 `MessageRecord.from_agent_message()`。
2. `SessionStore` 增加 `MessageRecord` 存储（或扩展 `MessageSummary` 支持完整正文）。
3. 历史切换时加载完整正文。
4. `sanitize_for_storage` 自动脱敏（已有逻辑）。

**退出条件**：
- 长回复（>500 字）在历史切换后完整可见。
- 存储内容经过脱敏（无绝对路径 / API key）。

---

### B7：媒体生成统一事件流

**目标**：图片 / 语音生成纳入 AgentRun，在 Run Drawer 统一展示。

**实施步骤**：
1. 图片 / 语音生成前创建 AgentRun（mode=image / mode=voice）。
2. 生成完成后 `stage_artifact()`（emit artifact.staged）。
3. Run Drawer 展示生成卡片（含缩略图 / 音频预览 + 耗时 + token / 费用）。
4. `PostconditionChecker` 验证 artifact 非空（已有逻辑：`generation_artifact_present`）。

**退出条件**：
- 图片生成全程在 Run Drawer 可见。
- 生成失败时 Run Drawer 显示原因（fail-closed 可见）。

---

## 6. 验收清单

### P0（必须通过才能声称"Agent 工作台"）

- [ ] V2 默认开启，普通对话走 AgentRun（B0）
- [ ] Run Drawer 实时显示阶段流转 + token + 工具调用（B1）
- [ ] Agent 在对话中能调用只读工具获取项目上下文（B2）
- [ ] Motion Graphic 生成经过确认门 + stale 检测（B3）
- [ ] 崩溃后 run 可恢复，不自动授权（B4）
- [ ] 新 reasoning 模型不出现空回复（B5）

### P1（核心稳定后完成）

- [ ] 历史保存完整脱敏正文（B6）
- [ ] 图片 / 语音生成在 Run Drawer 统一展示（B7）
- [ ] 自我纠错：Agent 检测工具失败后重试或换策略
- [ ] Diff 审批 UI：写操作前显示精确变更

### P2（远期）

- [ ] 子 Agent / 并行只读子任务
- [ ] MCP 工具发现与状态展示
- [ ] 费用 / 配额 / 撤销率闭环指标
- [ ] 浏览器 / 终端等外部能力评估

---

## 7. 风险与约束

### 7.1 不破坏现有行为

V2 接线必须保证 legacy fallback 完好。每个阶段完成后，`tests/ui/test_p1_chat_free_conversation.py` + `tests/app/test_ai_edit_controller.py` 必须全绿。V2 driver 构造失败时自动回退 legacy `_ChatWorker`（已有逻辑）。

### 7.2 安全红线不可退化

接线过程中以下红线必须保持（已有结构性保证，但不能因接线引入旁路）：

1. **sanitize_for_storage 脱敏**：所有存储边界经过脱敏。
2. **模型 approval_token 永远忽略**：写操作授权只来自宿主。
3. **demo stub 不伪装真实 Provider**：媒体生成无真实 provider → fail closed。
4. **Motion Graphic 单一确认门 + stale 检测**：确认前不写时间线。
5. **重启不恢复旧 approval**：AgentRun 无 grant 字段（结构性保证）。
6. **HTML artifact 不内联聊天日志**：只存 hash 引用。

### 7.3 Qt 线程安全

V2 事件流从后台线程（`AgentV2ChatWorker`）产生，UI 更新必须在主线程。`subscribe` 的回调通过 Qt signal 转发到主线程（queued connection），不能直接在回调中操作 QWidget。

---

## 8. 实施顺序建议

```
B5（reasoning 健壮化，独立，快速）     ← 可立即做
B0（V2 默认开启）                     ← 解锁所有已有代码的用户价值
  ↓
B1（Run Drawer）                      ← 让事件流可见，是后续所有阶段的基础
  ↓
B2（工具循环接入 chat）                ← 从"聊天"变"Agent"的质变
  ↓
B3（Motion Graphic 接入 V2）           ← 唯一写时间线的模式，安全优先级最高
B4（Checkpoint 持久化）                ← 可与 B3 并行
  ↓
B6（历史完整正文，独立）               ← 可随时做
B7（媒体生成统一事件流）               ← 依赖 B1 的 Run Drawer 基础
```

**建议首先完成 B5 + B0 + B1**，因为：
- B5 是独立的小修复，立即提升健壮性。
- B0 是一个 flag 翻转，但解锁所有 V2 代码的用户价值。
- B1 是后续所有阶段（B2/B3/B7）的 UI 基础。

---

## 9. 附录：真实 API 测试证据

### 9.1 测试脚本

| 脚本 | 覆盖范围 | 结果 |
|------|---------|------|
| `scripts/v2_real_chat_smoke.py` | 普通对话 9 轮（6 风格 + 多轮 + 取消 + Profile 切换）| 9/9 ✅ |
| `scripts/v2_real_multimode_smoke.py` | 能力路由 7 + Motion Graphic 3 + 图片 2 + TTS 2 | 14/14 ✅ |

### 9.2 真实 token 消耗样本

| 模式 | 模型 | prompt | completion | 耗时 |
|------|------|--------|------------|------|
| 极简问候 | glm-5.2（thinking off）| 19 | 196 | 5.8s |
| 技术深问 | glm-5.2 | 30 | 712 | 12.7s |
| 多轮追问 | glm-5.2 | 54 | 1500 | 27.3s |
| DeepSeek 问答 | deepseek-v4-flash | 15 | 54 | 1.6s |
| Motion Graphic | glm-5.2 | ~80 | 1500–3000 | 18–35s |
| 图片生成 | Seedream 5.0 lite | — | — | 15–22s |
| TTS | 豆包 Seed-TTS 2.0 | 54–59 字 | 48–63KB MP3 | 1.1–1.5s |

### 9.3 glm-5.2 thinking 问题诊断数据

```
HTML 设计 prompt + max_tokens=6000 + thinking=ON:
  finish_reason: length
  content: 0 chars
  reasoning_content: 22913 chars (5978 tokens)
  completion_tokens: 6000 (reasoning: 5978)

同一 prompt + thinking=disabled:
  finish_reason: stop
  content: 4801 chars (完整 HTML)
  reasoning_content: 0
  completion_tokens: 1532
  耗时: 15.1s (vs 86.3s)
```

---

## 版本历史

- 2026-08-14：初始版本，基于 23 项真实 API 测试 + 深度安全审计的差距分析
