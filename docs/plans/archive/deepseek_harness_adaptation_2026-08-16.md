# DeepSeek Harness 借鉴记录

本记录整理本地 `/Users/testuser/Documents/vscode/deepseek-harness` 对 Topos AI 剪辑助手的可迁移设计，不复制其 Node/Cordis 运行时。

## 可迁移的设计

- **事件日志优先**：将运行事件作为回放、持久化和 UI 投影的共同来源。
- **工具分阶段执行**：把审批、校验、执行、结果归一化和审计拆成可拦截阶段。
- **显式调度模式**：纯只读工具可以受限并行；写入、预览、取消、导出等操作形成独占屏障。
- **可组合能力**：Skill、目标、子 Agent、压缩和工作流通过稳定接口挂载，而不是不断膨胀主循环。

Topos 已有 `HarnessRuntime`、`AgentOrchestrator`、`TaskGraph`、`ToolFacade`、审批链和会话压缩，因此本轮先迁移低风险的工具调度语义，再补上不改变执行语义的运行轨迹投影层。

## 本轮落地

`ToolDefinition.execution_mode` 由宿主声明，默认 `exclusive`。当前只有能力清单、帮助查询、诊断解释和线程安全的渲染诊断可进入有界并发池；工程/Composite 快照、素材检索/分析和 job 状态查询仍保持独占，因为它们读取的宿主状态尚未提供完整并发契约。未知工具、MCP 工具以及任何可能改变状态的工具都安全降级为独占。

`AgentToolLoopBudget.max_parallel_tool_calls` 默认值为 `4`。连续的并行工具采用滚动线程池执行；遇到独占工具时先排空前一组，再执行屏障工具；结果和模型可见的 `role=tool` 消息始终按模型调用顺序提交。取消时不再补充新任务，但会排空已经提交的调用。

并行调度不改变现有审批语义：模型不能通过参数伪造授权，写入工具仍由宿主审批链决定。

`src/features/ai_harness/run_trace.py` 现在提供统一的 `RunTraceStore`：它把
`RunEvent` 和 `AgentEvent` 归一成带 session `seq` 的 append-only 事件，按
`(source, event_id)` 去重，并从同一份事件折叠出阶段、状态、工具调用、待处理工具、
artifact、token 和最后诊断。事件 payload 会再次脱敏并限制深度、字符串长度和集合
大小；达到保留上限时会显式标记 `trace_truncated`，不会把不完整投影伪装成完整历史。

编辑助手 controller 已订阅两条事件流，工具原始 `call_id` 会贯穿开始/结束事件；
取消或预算耗尽的未启动调用会生成 `TOOL_NOT_STARTED` 终态。工程 AI payload 会保存/恢复
trace，实际工程包路径是 `ai/trace.json`；清空助手历史时也会清空 trace。这样 Run Drawer 以后可以直接消费 projection，而不用再
从多个内存缓存猜测“这次运行到底做到了哪一步”。恢复只还原事实和摘要，不会重启
provider、重放写操作或恢复旧 approval。

工具调用现在也经过 `ToolInvocationRunner` 的 `pre → execute → post` 管线：
前置 middleware 可以做输入校验或宿主策略拒绝，`execute` 仍是唯一进入
`ToolFacade` 的路径，后置 middleware 可以做结果归一化和审计补充。管线是
不可变、可组合的，异常会转换成结构化失败；后置逻辑不能把失败/拒绝升级为成功，
因此不会绕过审批安全边界。当前聊天驱动器已启用 `ToolInputValidationMiddleware`，
后续可在不改 AgentToolLoop 的情况下挂载预算、指标和产品策略 middleware。

普通聊天请求在真正调用 provider 前会追加 `run.request_header` 事件，记录
provider/model、请求模式、工具名清单和不含正文的上下文摘要（消息数量、角色计数、
估算 token 与窗口上限）。这让 Trace Drawer 能解释“这次请求实际用了什么能力”，
同时不把用户 prompt 原文写入运行轨迹。

工具结果现在增加了独立的 `ToolResultPruner`：只裁剪模型下一轮看到的文本，默认保留
头部与尾部并插入 `[... tool result middle pruned ...]` 标记；`ToolResult` 本身、审计和
Trace 不被修改。新版 `AgentToolLoop` 与兼容旧入口的 `run_tool_loop` 都使用同一策略，
避免素材快照、字幕报告或转录结果挤占整个上下文窗口。

模型请求现在也有独立的 `ModelRequestRecovery`：仅对可重放的 LLM 请求做有限次指数退避，
重试前写入 `run.request_retry`，等待期间按短时间片检查取消。工具执行不在这个边界内，
因此不会因为模型请求恢复而重复执行已经发生副作用的剪辑工具；流式断流和网络/限流错误
可以恢复，鉴权、参数和响应格式错误仍然立即失败或按既有工具能力降级。

活动普通对话接入了有界 `AgentInbox` / pre-step：follow-up、steer、inject 会先进入线程安全
队列，在下一次模型请求前原子领取。steer 采用最后一条优先，inject 会明确标记为不可信运行时
数据，队列事件只记录长度和 hash，不把追加输入原文写入 Trace；当前请求不会被强制中断，
从而避免迟到输入覆盖已生成的结果或触发重复工具执行。

长目标检查点现在带有单调 `revision`，内存 checkpoint store 提供
`compare_and_save(goal_id, expected_revision, next_checkpoint)`；异步 continuation 使用 CAS，
旧回调只能得到 stale 诊断，不能覆盖更新后的目标状态、预算或已完成步骤。

工具执行增加 `ToolTimeoutPolicy`。若 Facade 支持 `timeout_seconds` / `cancel_check`，会把协作式
控制传入；旧 Facade 没有取消接口时仍会先等待调用完全退出，再以 `TOOL_TIMEOUT` 结束，避免
超时后遗留后台写操作或让下一轮重复执行不确定的副作用。

V2 编排器装配了可安装/卸载的 `RuntimeInvariantEngine`，当前检查工具 finished 必须有 matching
started（取消前未启动的 `TOOL_NOT_STARTED` 除外）以及 retry 不能出现在终态之后。生产模式只
记录诊断，测试或发布门禁可以启用 strict 模式抛出违规。

最后补上三类可复用扩展：`AgentPresetRegistry` 提供 editorial / subtitle quality 等安全默认
Preset，统一提示词、只读工具白名单和有限预算，`ChatRunDriver` 可按 `preset_id` 应用它们；
`SubagentSupervisor` 为命名子 Agent 保留 parent/child lineage，并限制深度、子任务数量、续接
轮次和写权限继承；`RunTraceStore.fork_session` 与 `SessionEventLog` 只在已存在的事件 seq 边界
创建子会话，复制的是事实事件而不是 provider、旧 approval 或后台 worker。

## 后续候选

1. 在现有 `ContextService`/压缩 checkpoint 上记录来源、替换关系和工具结果裁剪信息。
2. 为目标 continuation、子 Agent lineage 和动态 Skill catalog 增加可回放事件；恢复时禁止自动重放副作用。
3. 把 `RunTraceProjection` 接到 Run Drawer 的诊断详情页，支持按 `as_of_seq` 查看某一时刻的运行状态。
