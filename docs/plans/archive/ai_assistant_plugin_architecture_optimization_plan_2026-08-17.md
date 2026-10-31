# AI Assistant 插件化架构优化计划

> 目标：把 Topos AI 剪辑助手从“内置能力集合”演进为“稳定内核 + 可发现、可审批、可观测、可禁用的能力插件平台”。
>
> 对标启发：DeepSeek Harness 的“一切皆插件”思想。本文采用其可迁移的边界设计，不复制 Node/Cordis 实现，也不把 Topos 的 Qt、OpenGL、时间线事务和模型密钥暴露给插件。
>
> 制定日期：2026-08-17  
> 当前分支：`codex/deepseek-harness-optimization`

---

## 1. 结论先行：插件化，但不把核心内核插件化

“一切皆插件”最有价值的部分，不是把所有 Python 文件都改成插件，而是让每一项可变化的能力都通过稳定协议接入：工具、分析器、Provider、MCP 连接器、Agent Preset、Subagent、导出器和 UI 卡片都可以替换或禁用。

以下内容必须继续由 Topos 核心内核掌管：

| 核心内核 | 原因 |
|---|---|
| 审批、权限和 Path Grant | 防止插件或模型自行授予写权限 |
| 时间线事务、CommandStack、Undo/Redo | 保证编辑状态原子性和可恢复性 |
| AgentRun、Trace、Checkpoint | 保证运行事实只有一个来源 |
| 插件调度、超时、取消和隔离 | 防止插件拖垮 UI 或遗留副作用 |
| 凭据存储、脱敏和安全审计 | 密钥不能进入插件、Prompt 或工程文件 |
| 版本迁移与兼容策略 | 防止插件升级破坏旧工程和旧运行记录 |

插件只能声明能力、申请权限和返回结构化结果；不能直接访问 Qt、OpenGL、任意路径、Shell、时间线私有对象或 CredentialStore。

---

## 2. 当前基线

本计划建立在已经落地的四批 Harness 借鉴工作之上：

| 已完成能力 | 当前实现 |
|---|---|
| 审批等待、恢复与 fail-closed | `ChatRunDriver`、`AgentTaskExecutor` |
| 工具幂等与副作用恢复 | `ToolOperationLedger`、`ToolInvocationRunner` |
| Goal continuation 串行化 | `GoalRoundDriver` |
| append-only trace 与 Run Drawer 投影 | `RunTraceStore`、`RunTraceProjection` |
| 上下文压缩与 metadata-only spill | `ContextSpillStore`、`CompressionCheckpoint` |
| 能力目录单一事实源 | `CapabilityCatalog`、`ToolFacade`、`ToolInvocationRunner` |
| Preset / Inbox / Subagent | `AgentPresetRegistry`、`AgentInbox`、`SubagentSupervisor` |
| 现有 Skill 声明与工具过滤 | `SkillManifest`、`SkillRegistry` |

当前仍然存在的架构事实：能力已经“可组合”，但还不是完整的插件生命周期；插件 Manifest、注册、健康检查、隔离、持久化、UI 发现和灰度发布仍需要统一协议。

### 2026-08-18：Assistant 模式磁盘插件化首批已落地

AI Assistant 模式现在有一个独立的磁盘插件空间：

```text
plugins/ai_assistant/
├── chat/
├── motion_graphic/
├── video/
├── music/
└── image/
```

每个目录通过 `plugin.json` 声明模式 ID、版本、能力、处理器和 UI 顺序；提示词通过同目录下的 Markdown 文件加载。当前 Motion Graphic 的网页设计、创意计划、提案和旧场景提示词分别位于 `motion_graphic/` 中，运行时优先读取这些文件，代码常量仅作为兼容回退。

扫描器位于 `src/features/edit/ai_editing/assistant_plugins.py`。它只读取声明式 JSON/Markdown，不导入插件目录中的 Python，也不会因为插件声明权限就自动获得时间线写入权。新增或删除插件目录后，重启应用即可更新模式；Preferences 或测试代码可以调用 `reload_assistant_plugins()` 立即重扫。`TOPOS_AI_ASSISTANT_PLUGIN_DIR` 可用于便携版、测试或用户自定义插件目录。

这一批完成了“文件夹即 Assistant 模式”的发现、校验、UI 注册、能力路由和 Motion Graphic 提示词迁移；时间线事务、审批、渲染、凭据和高风险处理器仍由宿主内核掌管。

---

## 3. 总体架构

```text
┌────────────────────────────────────────────────────────────┐
│                     Topos Plugin Kernel                    │
│  PluginRegistry · Lifecycle · Policy · Scheduler · Trace    │
│  Approval · Transaction · Credential boundary · Migration   │
└───────────────┬──────────────────┬─────────────────────────┘
                │                   │
       ┌────────▼────────┐ ┌────────▼────────┐
       │ Capability       │ │ Provider / MCP   │
       │ Plugins          │ │ Plugins          │
       │ tools/analyzers  │ │ LLM/media/remote │
       └────────┬────────┘ └────────┬────────┘
                │                   │
       ┌────────▼───────────────────▼────────┐
       │ Agent composition                    │
       │ Preset · Skill · Subagent · Workflow │
       └───────────────────┬──────────────────┘
                           │
                    ┌──────▼──────┐
                    │ Qt UI / Run │
                    │ Drawer      │
                    └─────────────┘
```

插件的唯一可信输出是结构化的 `PluginResult` / `ToolResult` / `ArtifactRef`；所有运行事实必须进入 `RunTraceStore`，所有写入必须回到核心审批和事务层。

---

## 4. 分阶段实施总表

优先级：P0 = 安全和架构地基；P1 = 用户可感知能力；P2 = 扩展生态；P3 = 高级体验。

| 阶段 | 优先级 | 名称 | 目标 | 前置依赖 | 主要交付物 | 验收门槛 | 状态 |
|---|---:|---|---|---|---|---|---|
| P0 | P0 | 插件 Manifest 与生命周期协议 | 统一插件身份、版本、能力、权限和状态 | 当前 CapabilityCatalog / SkillRegistry | `PluginManifest`、`PluginRegistry`、生命周期接口 | 未声明、冲突版本、非法权限均 fail-closed | 已完成首批基础 |
| P1 | P0 | 插件与 CapabilityCatalog/Skill 统一 | 消除三套能力表，模型和 UI 看到同一真相 | P0 | Skill/Tool/Plugin 适配层、能力索引 | 注册一次即可生成工具、Preset 和帮助索引 | 已完成首批 Tool 接线 |
| P2 | P0 | 插件安全沙箱与执行策略 | 限制路径、网络、写入、资源和线程 | P0/P1 | `PluginExecutionPolicy`、并发/输入输出边界、审批门 | 插件不能越权；资源超限结构化失败 | 已完成首批执行策略 |
| P3 | P1 | Provider/MCP/分析器插件化 | 把模型、ASR、镜头分析、视觉服务统一为插件 | P1/P2 | Provider adapter、MCP adapter、health check | 可插拔切换，失败有明确诊断 | 已完成首批 Provider/Analysis 接线 |
| P4 | P1 | 剪辑能力插件化 | 字幕、素材分析、调色、合成、导出成为能力插件 | P1/P2 | `EditCapabilityPlugin`、事务桥接 | 插件只能生成 Plan/Preview，不能绕过 CommandStack | 已完成首批事务桥 |
| P5 | P1 | Agent Preset/Skill/Subagent 插件化 | Preset、Skill、Subagent 可发现、可禁用、可版本化 | P1/P3 | manifest 适配、来源标识、lineage trace | 白名单、预算、写权限按插件声明生效 | 已完成首批组合接线 |
| P6 | P1 | 插件持久化、迁移与回滚 | 工程重开后能恢复插件事实，不恢复旧授权 | P0/P1 | plugin snapshot、migration、rollback | 旧工程可读，未知插件可保留并诊断 | 已完成持久化、版本迁移与内存回滚首批 |
| P7 | P2 | 插件 UI 与 Run Drawer | UI 根据插件 manifest 自动显示状态、参数、诊断 | P1/P3 | 插件卡片、健康状态、运行详情 | 禁用/失败/版本不兼容对用户可见 | 已完成首批状态投影 |
| P8 | P2 | 插件发现、安装与市场边界 | 支持内置、本地开发、验证第三方三种来源 | P2/P6 | discovery、签名接口、审核状态 | 本地插件醒目标记，未验证插件不能自动获得高权限 | 已完成首批发现/信任边界 |
| P9 | P2 | 评测、性能与灰度发布 | 建立插件级质量门和回滚机制 | P2–P8 | contract tests、benchmark、rollout flags | 失败率、耗时、费用和回滚均可追踪 | 已完成首批质量门与灰度路由 |
| P10 | P1 | 宿主集成与工程恢复 | 把插件运行时真正接入 AI Assistant、技能路由和 `.topos` 工程 | P5–P9 | shared registry、UI status strip、ai/plugins.json | 用户可见插件状态，重开工程可读 checkpoint，旧 run 不被重放 | 本批完成首批接线 |
| P11 | P1 | 工程级状态恢复与完整性校验 | 恢复项目级启用事实，同时阻断旧 active 状态和篡改 manifest | P6/P10 | state reconciliation、Skill sync、canonical content hash | disabled 保持禁用，active 仅待重新激活，hash 冲突不改变运行时 | 本批完成 |
| P12 | P1 | 第三方信任与资源 Broker | 让签名、安装、文件和网络访问都经过宿主边界 | P2/P8/P11 | detached verifier、install result、resource broker | 未验签第三方不能安装；路径穿越、未授权域名和 policy 越权均拒绝 | 本批完成 |
| P13 | P1 | 插件执行控制与调用观测 | 让取消、deadline、失败和插件调用与 run_id 对齐 | P2/P7/P9/P12 | cooperative context、invocation events、run_id filter | 协作式插件可安全退出；旧 handler 不伪装成强制终止；调用事实可诊断 | 本批完成 |
| P14 | P1 | 进程级隔离与统一 Run Trace | 为高风险/重资源插件提供可选地址空间边界，并把插件事件投影到统一运行轨迹 | P13 | `ProcessIsolatedPlugin`、hard-stop IPC、`PluginRunTraceAdapter` | 非协作插件超时/取消后进程确实退出；插件调用可在 Run Drawer 追踪；默认进程内插件行为不变 | 本批完成 |
| P15 | P1 | 宿主 I/O Capability 与 Sandbox 诚实降级 | 将文件/网络操作收回宿主，并明确 OS sandbox 是否真正生效 | P12/P14 | `PluginHostIO`、子进程 I/O RPC、sandbox capability report | 未授权 I/O fail-closed；路径/域名/请求有界；required sandbox 未配置时拒绝启动 | 本批完成 |
| P16 | P0 | Sandbox Launcher 注入与 Required Gate | 把平台级 sandbox 的启动、证明和运行时 IPC 接成可替换宿主契约 | P15 | `PluginSandboxLauncher`、launcher report validation、required gate | 未证明 `enforced` 的 launcher 不能启动 required 插件；默认 spawn 行为保持兼容 | 本批完成 |
| P17 | P0 | 平台 Sandbox Launcher 与外部 Worker | 将 launcher 契约落到首批 Unix 平台，并验证外部 worker 的 IPC/生命周期 | P16 | `DarwinSandboxLauncher`、`LinuxBubblewrapLauncher`、`plugin_sandbox_worker` | 命令无 shell；profile/mount 显式；bootstrap 与主 IPC 分离；启动失败可诊断；Windows 未实现时 required 继续拒绝 | 本批完成首批 Unix launcher |

---

## 5. 各阶段详细任务与验收标准

### P0：插件 Manifest 与生命周期协议

**目标**：先定义边界，再允许任何能力进入插件体系。

任务：

1. 定义稳定 `plugin_id`、SemVer、Plugin API 版本、来源、作者、许可证和内容 hash。
2. 定义插件能力声明：能力 ID、类型、工具名、权限上限、是否需要审批、资源预算、输入/输出 schema。
3. 定义生命周期：`registered → enabled → active → degraded/failed → disabled`。
4. 定义最小插件接口：`activate(context)`、`deactivate()`、`health()`；插件异常不能传播到 UI 主循环。
5. 注册时拒绝重复 ID、非法版本、未知权限、禁止能力、空能力声明和 hash 不一致。
6. 注册表提供稳定排序、按能力查询、启用/禁用、替换和诊断快照。

验收：注册一个只读插件可出现在能力目录；声明 Shell、任意路径或写权限升级的插件被拒绝；插件激活失败只影响该插件；所有状态变化可被测试读取。

### P1：Plugin / Capability / Skill 统一

任务：

1. `CapabilityCatalog` 成为工具调度和权限元数据的唯一事实源。
2. `SkillManifest.tool_policy`、`AgentPreset.tool_allowlist` 转换为统一的 capability scope。
3. ToolFacade、ProviderRegistry、SkillRegistry、PresetRegistry 不再各自隐藏工具。
4. 从插件注册表生成：模型工具 schema、帮助索引、Preferences 状态、Run Drawer 标签。
5. 未激活插件的工具不能被模型广告或 Runner 调用。

验收：同一个插件只注册一次，`list_tools()`、Preset 工具清单和帮助页得到相同结果；禁用插件后所有入口同时消失。

### P2：插件安全沙箱与执行策略

任务：

1. 权限分层：read / preview / proposal / write / expensive / file_write。
2. 路径能力采用 `PathGrant`，网络采用域名 allowlist，写入采用宿主签发的 `ApprovalGrant`。
3. 每个插件拥有独立的超时、并发、token、内存和输出大小预算。
4. 插件线程不能直接访问 Qt/OpenGL；跨线程只允许事件和不可变数据。
5. 工具异常、超时、取消和进程崩溃均产生结构化失败，不得伪造成功。

已完成的首批基础：

- `PluginExecutionPolicy` 为每个插件提供并发上限、输入字符上限和输出字符上限；
- `PluginRegistry.invoke_tool()` 在进入插件前校验 JSON 输入和审批，在离开插件后校验 JSON 输出；
- 并发额度使用宿主侧 `BoundedSemaphore`，超限返回 `PLUGIN_BUSY`，输入/输出超限返回可追踪诊断；
- 策略会进入插件快照和 `PluginContext`，但不把声明式 `allow_network/allow_filesystem` 误称为进程级沙箱；真正的路径/网络 broker、超时取消和子进程隔离仍作为后续 P2 子阶段实施。

验收：恶意参数、插件崩溃、超时、重复调用、取消后迟到结果均不能改变时间线或写文件。

### P3：Provider/MCP/分析器插件化

任务：

1. 把语言模型、图片/视频/音频 Provider 统一为 `ProviderPlugin`。
2. 把 ASR、OCR、镜头检测、语义搜索统一为 `AnalysisPlugin`。
3. 把 MCP 连接器统一为 `McpPlugin`，manifest 必须记录 endpoint、能力、TTL、来源和数据策略。
4. Provider 健康状态、模型能力、费用和数据策略进入 Run Trace header。
5. Provider 不可用时由 Router 选择明确 fallback，不得把 demo 结果标为真实产物。

已完成的首批基础：

- 新增 `ProviderDescriptor`，统一 provider 类型、能力、版本、来源、模型标识、数据策略和 consent 标记；
- 新增 `AnalysisPluginHost`，把现有 `AnalysisProvider` 接入 `PluginRegistry`；provider 注册后默认不可运行，只有插件激活后才进入 `AnalysisService`；
- provider 停用会从未来分析请求中移除，但不删除历史 `AssetAnalysis` 证据；插件快照包含 provider 描述、生命周期、健康状态和执行策略；
- 新增 `McpPluginDescriptor`/`McpPluginHost`，把 endpoint、tool/capability、TTL、来源、网络域名和数据策略纳入插件快照；未激活的 MCP 连接器不能创建会话或处理 JSON-RPC；
- 本批只完成生命周期/发现边界与 MCP transport 接线；Provider Router 统一 fallback、凭据/网络 broker 和 Run Trace header 接线仍是 P3 后续批次。

验收：切换 Provider 不改变 AgentRun、ToolResult、Approval 和 Artifact 协议；离线时 UI 能解释“不可用原因”。

### P4：剪辑能力插件化

第一批建议拆分：

| 插件 | 初始能力 | 权限 | 备注 |
|---|---|---|---|
| `topos.editorial` | 素材搜索、镜头分析、字幕检查、EditPlan 校验 | read/preview | 只读优先 |
| `topos.subtitle` | ASR、字幕草稿、字幕质量门 | read/proposal | Commit 仍走核心事务 |
| `topos.color` | 调色参数建议、LUT/效果能力声明 | preview/proposal | 不直接操作 GPU 上下文 |
| `topos.composite` | Composite Plan、节点验证、预览 | preview/proposal | 统一 revision |
| `topos.delivery` | 导出 preflight、质量检查、导出请求 | expensive/file_write | 文件写入必须二次确认 |

已完成的首批基础：

- 新增 `EditCapabilityPlugin`/`EditCapabilityPluginHost`，插件 manifest 只暴露 namespaced `propose` 工具；
- 插件返回值必须能还原为 `EditPlan`，且只能是 `preview` 模式；宿主先调用 `topos.validate_edit_plan`；
- 真正应用必须由宿主携带 approval 调用 `topos.apply_edit_plan`，插件不接触 `EditSession`、CommandStack 或时间线对象；
- 插件停用、非法计划、插件企图返回 commit 计划均 fail-closed。

验收：任何插件都只能输出 Plan、Preview、ArtifactRef 或 Job；真实时间线修改必须进入核心 CommandStack，并能 Undo/Redo。

### P5：Preset / Skill / Subagent 插件化

任务：

1. Preset 声明提示词、工具白名单、预算、Profile 引用和 UI 标签。
2. Skill 声明输入/输出 schema、风险级别、工具策略、来源和 eval 版本。
3. Subagent 声明 lineage、最大深度、子任务数、续接轮次和写权限上限。
4. 所有 continuation、fork、subagent 事件进入 trace；不保存完整 prompt，只保存 hash、长度和来源。
5. 禁用或升级插件后，旧运行仍能根据 manifest hash 解释，不自动重放副作用。

已完成的首批基础：

- 新增 `AgentPresetPluginHost`，把现有 `AgentPreset` 投影为 manifest-only 插件，保留工具白名单、预算、来源和版本快照；
- `ChatRunDriver` 增加可选 Preset 插件门控：接入后只有 active Preset 才能参与运行；未接入时保持现有兼容路径；
- 新增 `SkillPluginHost` 和 `SubagentPluginHost`：Skill 停用后工具发现为空，Subagent 停用后运行/续接被拒绝，同时复用原有工具白名单与 lineage/深度/轮次预算；
- 后续批次仍需把更多 UI/安装/升级入口接到这些 host，并补齐旧运行基于 manifest hash 的解释链。

验收：Preset/Skill/Subagent 的限制在真实 Runner 中生效，而不仅仅是提示词约定；越权工具在执行前被拦截。

### P6：持久化、迁移与回滚

任务：

1. 工程 AI payload 保存插件 ID、版本、manifest hash、启用状态和运行引用。
2. 插件升级提供 schema migration；迁移失败保留原始 payload 并进入 degraded 状态。
3. 工程关闭时取消插件 job、清除临时授权、保存安全 trace 摘要。
4. 重启只恢复事实和 checkpoint，不恢复旧 approval、不自动重放写操作。
5. 插件卸载后历史运行显示“插件不可用”，但不破坏工程文件。

已完成的首批基础：

- 新增 `PluginSnapshotStore` 和 `PluginRestoreReport`，生成版本化 `topos-plugin-project/1.0` 快照；
- 兼容旧的 `topos-plugin-registry/1.0` 以及早期无版本插件列表，并统一迁移到工程快照格式；
- 恢复时只注册 manifest、policy、health/state 事实，之前的 `active` 插件一律要求重新激活；
- 未找到实现的插件保留 manifest 并标记 `degraded`；审批 grants 强制清空，快照内容经过 secret/path 脱敏；
- 新增 `PluginMigrationRegistry`，支持按插件版本注册一跳迁移并串联执行；迁移缺失、循环或异常时 fail-closed，旧版本不会被静默加载；
- 新增有界的 `PluginSnapshotJournal`，记录快照 hash、时间和 checkpoint，并支持回滚到仍在保留窗口内的内存快照；`to_dict()` 为后续工程文件持久化保留接口；
- 工程文件级 journal 恢复、迁移脚本的版本治理和跨进程回滚仍待后续批次接入。

验收：旧工程可打开；缺失插件不会导致工程崩溃；恢复后的 run 必须重新确认权限。

### P7–P9：UI、发现、评测和发布

这些阶段在核心协议稳定后实施，避免过早建设插件市场：

- UI 展示来源、版本、权限、健康状态、最近失败和禁用原因；
- P7 首批新增 Qt-free `PluginStatusProjection`：从 `PluginRegistry` 生成稳定的 `PluginStatusView`，同时提供 capability 参数 schema、审批标记、API 兼容性、可执行动作和结构化 diagnostics；
- 新增有界 `PluginEventJournal` 作为可选 event sink，给插件卡片/Run Drawer 提供最近失败，不把完整 payload 或 secret 带入 UI 状态；
- P8 首批新增 `PluginDiscovery`：只接收已解析 manifest，不扫描/导入/执行代码；内置、本地开发、验证第三方分别映射为 ready、unverified、pending-enable；
- 第三方 manifest 必须通过 declared hash 完整性检查；发现结果与安装分离，`install()` 只登记为 disabled，绝不自动启用；
- 本地开发插件必须显示“未验证”；第三方插件先 manifest 校验，再进入待启用状态；
- P9 首批新增 `PluginRolloutLedger`/`PluginRolloutGate`：按插件版本聚合成功、失败、崩溃、取消、P95 延迟、费用和回滚可用性，质量门失败返回稳定诊断码；
- 新增 `PluginRolloutFlag`/`PluginRolloutRouter`：按 `run_id` 稳定分桶，只对灰度开始后的新 run 生效，旧 run 不会因为灰度配置变化而切换；
- 插件 contract test 覆盖 manifest、权限、取消、超时、输出 schema、trace 和迁移；
- 发布门同时检查插件失败率、P95 延迟、费用、崩溃率和回滚可用性；
- 插件灰度只影响新运行，不修改已有运行和工程事实。

### P10：宿主集成与工程恢复

本批首要目标是让前面完成的协议进入真实 Assistant 宿主，但仍保持插件边界：

- `build_p1_dependencies()` 创建共享 `PluginRegistry`/`PluginEventJournal`，将已校验的 Skill manifest 投影为插件记录；聊天 Router 的 Skill resolver 改由 `SkillPluginHost.is_active` 门控；
- `AiEditController` 暴露 `plugin_status_views()`、`export_plugin_state()` 和 `restore_plugin_state()`，状态投影进入现有 Assistant 面板；
- Assistant 面板新增只读插件状态条，展示插件数量、可用数量、需关注数量和未验证数量，详细版本/诊断放在 tooltip；
- `.topos` 新增 `ai/plugins.json` 模块，保存插件快照与 journal；journal 加载时逐 checkpoint 校验 hash，损坏条目跳过，不恢复 active 或 approval；
- 当前恢复策略保留 checkpoint 事实供诊断/后续显式迁移；真实第三方安装器、签名验签、known plugin 的项目级 enable/disable 还原仍需下一批补齐。

### P11：工程级状态恢复与完整性校验

本批补齐工程重开时“保存事实”和“当前运行时状态”之间的安全桥接：

- `PluginSnapshotStore.restore_project_state()` 只对当前已加载且 manifest hash 匹配的插件应用生命周期事实，不从工程文件导入或替换插件实现；
- 保存为 `disabled` 的插件重开后保持禁用；保存为 `active` 的插件只恢复为 `enabled`，并返回 `reactivation_required`，绝不隐式调用 `activate()`；
- `SkillPluginHost` 同步 `SkillRegistry`，确保插件内核状态和实际聊天路由/工具过滤不会分叉；
- 工程文件中的 manifest 使用脱敏后的 canonical content hash，保存、迁移和恢复使用同一规则，既保留 secret-free 红线，也能识别篡改；冲突只进入诊断，不改变当前运行时插件；
- 本批的 hash 是完整性校验，不等同于密码学签名；第三方签名验签、安装器和权限 broker 继续作为后续独立阶段。

### P12：第三方信任与资源 Broker

本批把“发现”和“可执行”之间的信任边界进一步明确：

- `PluginManifest` 支持 detached `signature`/`signature_key_id`，`PluginTrustPolicy` 通过可注入 `PluginSignatureVerifier` 验证 verified third-party；没有签名、验签器或验签失败均 fail-closed；
- `HmacPluginSignatureVerifier` 只作为受控部署/测试参考实现，生产市场可注入 Ed25519、PKI 或平台密钥链实现，密钥不会进入 manifest 或 `.topos` 工程；
- `PluginDiscovery.install_with_report()` 返回结构化安装结果，安装始终落在 `disabled`，不会导入代码、激活插件或隐式提升权限；
- `PluginContext` 获得按插件 ID 和 execution policy 绑定的 `ResourceBroker`；默认拒绝，allowlist broker 对路径做 canonical resolve/commonpath 检查，对网络做 scheme、hostname、子域和凭据校验；
- policy 的 `allow_filesystem`/`allow_network` 只是 Broker 的前置门，不被当作进程级沙箱；实际 I/O 仍由宿主审计适配器执行。

### P13：插件执行控制与调用观测

本批补齐插件调用进入真实运行链后的控制和诊断语义：

- `PluginExecutionPolicy` 支持可选 `timeout_seconds`；调用参数支持 `cancel_event`、deadline 覆盖和 `run_id`；
- `PluginCallContext` 向兼容的 handler 提供 `raise_if_cancelled()`、deadline 和 invocation_id，取消/超时/异常均返回稳定诊断码；
- 旧签名 handler 继续兼容，但超时/取消只会被标记为 `*_NONCOOPERATIVE`，不会声称 Python 代码已经被强制杀死；
- Registry 发出 `plugin.tool_started`、`plugin.tool_completed`、`plugin.tool_cancelled`、`plugin.tool_timed_out` 和 `plugin.tool_failed`，事件只记录大小、耗时、run_id 和诊断码，不记录完整 args/result；
- `PluginEventJournal` 支持按 `plugin_id`/`run_id` 过滤，供状态卡、Run Drawer 和后续 RunTrace adapter 使用；真正的子进程杀停和地址空间隔离仍作为下一阶段独立实现。

### P14：进程级隔离与统一 Run Trace

本批把 P13 中明确保留的“真正杀停”边界落地，同时让插件事件成为统一运行事实：

- `PluginExecutionPolicy.isolation_mode` 默认为 `in_process`，保持现有 Skill/Provider 的低开销行为；只有显式选择 `process` 且注册 `ProcessIsolatedPlugin` 适配器时才允许启用隔离，普通 handler 误配会 fail-closed；
- `ProcessIsolatedPlugin` 使用显式 `spawn` 子进程和版本化 IPC 协议，在子进程内创建插件实例、执行生命周期和工具调用；factory 必须可导入/可 pickle，宿主授权对象和资源 Broker 不会隐式进入子进程；
- 父进程轮询 `cancel_event`、`cancel_check` 和 deadline；非协作 handler 超时/取消会 terminate/kill 子进程，并返回 `PLUGIN_TIMEOUT`/`PLUGIN_CANCELLED`，不再使用 `*_NONCOOPERATIVE` 这种“代码仍可能在宿主线程继续运行”的降级码；
- `PluginRunTraceAdapter` 将带 `run_id` 的插件开始/结束事件映射为 `run.tool_started`/`run.tool_finished`，保留 `plugin_event_kind`、`plugin_id`、`invocation_id` 和诊断码；无 `run_id` 的注册/激活事件仍只留在插件事件账本；
- 进程插件子地址空间默认使用 `DefaultDenyResourceBroker`，实际文件/网络访问仍需通过宿主审计适配器；本批的“隔离”是执行边界，不等同于操作系统级 seccomp/container 权限收敛。

### P15：宿主 I/O Capability 与 Sandbox 诚实降级

本批把“插件能访问资源”从布尔权限进一步收敛成宿主执行的 capability：

- `PluginHostIO` 提供有界 `read_bytes`、`write_bytes` 和 `request_bytes`；每次操作先经过绑定的路径/网络 Broker，再由宿主执行，插件不拿 raw file handle、socket、subprocess 或 unrestricted URL opener；
- 文件读写使用 canonical allowlist、`O_NOFOLLOW`（平台支持时）、读写字节上限和安全创建模式；网络只允许声明的 HTTP 方法和 header，拒绝凭据 header、URL credential 和自动 redirect，并限制响应体和超时；
- `ProcessIsolatedPlugin` 通过版本化 IPC 把 `check_path`/`check_network`/文件读写/网络请求回到宿主，子进程只得到结构化结果；没有 grant 时继续 default-deny；调用 deadline 会约束宿主网络请求的 timeout；
- `PluginSandboxReport` 区分“系统存在候选工具”和“本次进程真的启用了 OS sandbox”。`sandbox_mode="required"` 在没有注入真实 launcher 时 fail-closed，`report` 模式只发出 `plugin.sandbox_degraded` 诊断；不会把普通 `spawn` 进程包装成 seccomp、container 或 macOS Sandbox；
- P15 本身不把候选工具探测误报为隔离；平台启动器的实际接入放在后续 P16/P17，并且仍不能由 Python capability broker 代替。

### P16：Sandbox Launcher 注入与 Required Gate

本批把平台级 sandbox 的最后一跳变成显式宿主契约：

- `PluginSandboxLauncher` 要同时提供 `report()` 和 `launch()`；report 必须说明 backend、available 和 enforced，launch 必须返回可接入现有双工 IPC 的 startable process；
- `ProcessIsolatedPlugin(sandbox_launcher=...)` 在启动前验证 launcher report；`sandbox_mode="required"` 只有 `available=True` 且 `enforced=True` 才能继续，普通工具探测或字符串声明不能绕过；
- 没有 launcher 时，默认 `spawn` 路径保持兼容并继续发出 `plugin.sandbox_degraded`；required 插件则在创建子进程前 fail-closed，不留下 pipe/process 泄漏；
- 平台实现可以独立注入 macOS Sandbox profile、Linux bubblewrap/seccomp 或 Windows AppContainer，但必须由实现方对 `enforced=True` 负责并通过平台专项测试；核心运行时不会假设某个平台工具一定存在或一定生效；
- 本批提供的是可验证的启动协议和拒绝门，不把测试 launcher 当成生产 sandbox。真正的平台 launcher 仍需在部署层完成命令、profile、IPC fd 传递和最小文件系统映射的验收。

### P17：平台 Sandbox Launcher 与外部 Worker

本批把 P16 的宿主契约落到首批可运行的 Unix 启动路径：

- `DarwinSandboxLauncher` 使用无 shell 的 `sandbox-exec -p <deny-default-profile> python -m ...` argv；profile 必须以 version 开始、包含 `(deny default)`，并拒绝 `(allow default)`；profile 的具体文件/网络 allowlist 仍由宿主部署层审查；
- `LinuxBubblewrapLauncher` 使用无 shell 的 `bwrap` argv，显式声明 `--ro-bind`/`--bind` 根目录、PID/UTS/IPC namespace、临时目录和默认 `--unshare-net`；不会把 `/` 作为 mount root，也不会自动把整个宿主文件系统暴露给插件；
- `plugin_sandbox_worker` 通过继承 FD 启动，bootstrap（factory、plugin_id、policy）使用独立的本地 IPC 通道传递，插件生命周期/调用继续使用原有版本化双工 IPC，避免 bootstrap 污染主协议；
- 外部进程 facade 对齐现有 `start/is_alive/terminate/kill/join` 生命周期，标准输入/输出/错误关闭，失败时清理子进程和 bootstrap 通道；Python import path 显式继承宿主运行环境，Linux mount 可由部署层进一步收窄；
- 当前没有 Windows AppContainer launcher。Windows 或其他平台注入不了 `available=True, enforced=True` 的真实实现时，`sandbox_mode="required"` 仍然 fail-closed；本批不宣称跨平台 OS sandbox 已全部完成。

---

## 6. 依赖与实施顺序

```text
P0 Manifest/Lifecycle
        ↓
P1 Capability/Skill/Preset 统一
        ↓
P2 Policy/Sandbox ──────┐
        ↓               │
P3 Provider/MCP         │
P4 Editing Capabilities ┘
        ↓
P5 Agent Composition
        ↓
P6 Persistence/Migration
        ↓
P7 UI → P8 Discovery → P9 Rollout
                         ↓
                    P10 Host Integration
                         ↓
                    P11 State Restore
                         ↓
                    P12 Trust/Broker
                    ↓
                    P13 Execution/Trace
                    ↓
                    P14 Process Isolation/Run Trace
                    ↓
                    P15 Host I/O/Sandbox
                         ↓
                    P16 Launcher/Required Gate
                         ↓
                    P17 Unix Launcher/Worker
```

每一阶段采用“小提交 + 回归测试 + 可回滚”的节奏：

1. 先写协议和失败测试；
2. 实现最小能力；
3. 接入一个真实只读插件；
4. 运行相关测试；
5. 提交后再进入下一阶段。

禁止一次性把所有剪辑模块重构成插件，也禁止在 P2 完成前开放第三方写入插件。

---

## 7. 当前执行记录

| 日期 | 阶段 | 结果 | 提交 |
|---|---|---|---|
| 2026-08-17 | P0 计划建立 | 本文档落地，开始实现 Manifest/Lifecycle 基础 | 已提交 `9fb30ad4` |
| 2026-08-17 | P0 首批基础 | 新增线程安全 `PluginRegistry`、不可变 `PluginManifest`、能力权限校验、生命周期状态、健康检查和安全事件快照；5 项测试通过 | 已提交 `9fb30ad4` |
| 2026-08-17 | P1 首批 Tool 接线 | 激活插件可生成 `ToolDefinition` 并接入 `ToolFacade`/`CapabilityCatalog`；插件停用后从广告和调用路径移除；84 项相关测试通过 | 已提交 `23c2acfb` |
| 2026-08-17 | P2 首批执行策略 | 新增 `PluginExecutionPolicy`；实现并发门控、输入/输出 JSON 大小限制、结构化超限诊断和策略快照；8 项插件运行时测试通过 | 已提交 `cf3b778b` |
| 2026-08-17 | P3 首批 Provider/Analysis 接线 | 新增 `ProviderDescriptor`、`AnalysisPluginHost` 和分析 provider 生命周期桥接；停用 provider 从 `AnalysisService` 移除但保留历史证据；75 项相关测试通过 | 已提交 `eb16d728` |
| 2026-08-17 | P3 首批 MCP 接线 | 新增 `McpPluginDescriptor`、`McpPluginHost`；MCP 连接器激活门控会话和 JSON-RPC，权限仍统一回到 `McpServer`/`ToolFacade`；37 项相关测试通过 | 已提交 `aa16b0e8` |
| 2026-08-17 | P4 首批事务桥 | 新增 `EditCapabilityPluginHost`；插件只能提出并校验 preview plan，应用必须走宿主 approval + `ToolFacade`；44 项组合测试通过 | 已提交 `f65e8524` |
| 2026-08-17 | P5 首批 Preset 接线 | 新增 `AgentPresetPluginHost`，并为 `ChatRunDriver` 提供可选 Preset 生命周期门控；47 项相关测试通过 | 已提交 `6eb192d0` |
| 2026-08-17 | P5 首批 Skill/Subagent 接线 | 新增 `SkillPluginHost`、`SubagentPluginHost`；插件状态分别门控工具发现和子智能体运行/续接；33 项相关测试通过 | 已提交 `04407328` |
| 2026-08-17 | P6 首批持久化 | 新增 `PluginSnapshotStore`/`PluginRestoreReport`；支持旧 schema 迁移、未知插件 degraded、旧授权清空和脱敏快照；19 项相关测试通过 | 已提交 `e6d48e5f` |
| 2026-08-17 | P6 版本迁移与回滚 | 新增 `PluginMigrationRegistry` 和有界 `PluginSnapshotJournal`；迁移失败不恢复旧 manifest，新增迁移链/阻断/回滚测试；22 项相关测试通过 | 已提交 `3a0d332c` |
| 2026-08-17 | P7 首批状态投影 | 新增 `PluginStatusProjection`/`PluginStatusView`/`PluginEventJournal`；UI 可统一读取来源、版本、权限、schema、健康、最近失败和诊断；18 项相关测试通过 | 已提交 `1f61d2cc` |
| 2026-08-17 | P8 首批发现与信任边界 | 新增 `PluginDiscovery`；支持来源分级、完整性 hash 校验、重复 manifest 拒绝和 disabled 安装边界；21 项相关测试通过 | 已提交 `ac56377a` |
| 2026-08-17 | P9 首批质量门与灰度路由 | 新增 `PluginRolloutLedger`/`PluginRolloutGate`/`PluginRolloutRouter`；覆盖失败率、崩溃率、P95、费用、回滚和新 run 稳定分桶；24 项相关测试通过 | 已提交 `5b0ac47f` |
| 2026-08-17 | P10 首批宿主集成与工程恢复 | Skill 路由接入共享插件生命周期；Assistant 面板显示状态条；`.topos` 保存/加载 `ai/plugins.json` 和 checkpoint journal；新增宿主、UI、工程 payload 回归测试；60 项相关回归通过 | 已提交 `1e69ce98` |
| 2026-08-17 | P11 工程级状态恢复与完整性校验 | 工程重开恢复 disabled/enabled 事实；active 降级为待显式激活；SkillRegistry 同步；脱敏 canonical hash 校验与篡改诊断；65 项相关回归通过 | 已提交 `286ce199` |
| 2026-08-17 | P12 第三方信任与资源 Broker | detached verifier、disabled-only 安装结果、默认拒绝资源 Broker、路径/网络 allowlist 和 policy 绑定；67 项相关回归通过 | 已提交 `e4a9166a` |
| 2026-08-17 | P13 插件执行控制与调用观测 | cooperative context、timeout/cancel 诊断、生命周期事件、run_id 过滤和非协作 handler 的诚实降级；93 项相关回归通过 | 已提交 `688e8729` |
| 2026-08-17 | P14 进程级隔离与统一 Run Trace | `ProcessIsolatedPlugin`、spawn IPC、非协作 handler hard-stop、插件事件到 `RunTraceStore` 投影；31 项专项回归通过 | 已提交 `0ae59390` |
| 2026-08-17 | P15 宿主 I/O Capability 与 Sandbox 诚实降级 | `PluginHostIO`、文件/网络有界操作、进程插件 I/O RPC、sandbox capability report 和 required fail-closed；36 项专项回归通过 | 已提交 `17511a35` |
| 2026-08-17 | P16 Sandbox Launcher 注入与 Required Gate | `PluginSandboxLauncher`、report 校验、launcher 启动接入和 required fail-closed；21 项专项回归通过 | 已提交 `8301e2e2` |
| 2026-08-17 | P17 平台 Sandbox Launcher 与外部 Worker | macOS `sandbox-exec` / Linux `bubblewrap` launcher、独立 bootstrap FD、外部 worker 生命周期和 Unix launcher 契约测试；111 项相关回归通过 | 已提交 `e3c7c8c7` |
