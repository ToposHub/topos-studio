# Topos Color AI 全功能控制优化计划

## 0. 文档信息

| 项目 | 内容 |
|---|---|
| 文档目的 | 规划 Topos 项目从“部分 AI 辅助”升级为“可发现、可规划、可验证、可回滚的全功能 AI 控制系统” |
| 适用范围 | Color、Edit、Composite、Deliver、Teaching Animation、AI Workflow、媒体分析、生成、工程、素材库、脚本、诊断和外部控制接口 |
| 当前评估日期 | 2026-09-17 |
| 当前状态 | AI 编辑控制底座已存在，但尚未形成覆盖全产品的统一控制面 |
| 推荐文档位置 | `docs/ai_control_optimization_plan.md` |
| 关联规范 | `.clinerules`、`docs/ai_assistant_privacy_acceptance_checklist.md`、`docs/module_boundary_rules.md` |

本文是项目级开发计划和验收基线，不代表所有计划中的能力已经完成。后续每项实现都必须同步更新本文中的状态、测试结果和风险记录。

---

## 1. 最终目标和总体判断

### 1.1 最终目标

用户只提供自然语言需求，例如：

> 打开当前项目，整理素材，完成基础调色，建立合成节点，生成字幕和教学动画，输出横版、竖版两个版本，检查结果并保存项目。

AI 应能够：

1. 自动发现当前产品和项目能力；
2. 读取完整项目状态；
3. 将需求拆分为可执行计划；
4. 对每个步骤进行参数和依赖校验；
5. 预览变化和影响范围；
6. 按权限策略执行操作；
7. 等待、监控和取消异步任务；
8. 验证每一步是否达到预期；
9. 出错后重试、补偿、回滚或请求用户决策；
10. 保存审计记录，使每次 AI 操作都可解释、可复现、可撤销。

### 1.2 当前总体判断

当前项目已经完成以下基础：

- 有 `ToolFacade`、MCP、Local IPC、CLI 和 AI Agent 执行框架；
- Edit 时间线拥有较完整的计划、验证、应用链路；
- Composite 参数、关键帧和表达式已经有 AI 编辑协议；
- 已有能力目录、权限分级、审批授权、任务状态和诊断机制；
- 已有脚本 API、媒体生成插件、Teaching Animation AI 路由和 AI Workflow 实验模块。

当前尚未完成的核心问题：

- Color、Composite 节点结构、Deliver 全部配置、工程管理和素材库没有统一进入中央控制面；
- UI Action、脚本 API、模块内部 callback 和 MCP 工具之间存在多套入口；
- AI 能力受产品模式、feature flag、Edit 模块初始化和人工审批限制；
- 部分功能能由用户操作，但无法由 AI 通过正式、可验证的语义接口操作；
- 全自动任务缺少跨模块事务、统一事件流、统一回滚和端到端验收矩阵；
- 真实媒体探测测试当前仍有 `PROBE_UNAVAILABLE` 失败，交付链路尚不能宣称完全稳定。

### 1.3 目标原则

本项目的目标不是让 AI 直接“点击所有按钮”，也不是开放任意 Python、Shell 或文件系统权限，而是：

> 将每个用户可执行功能抽象为有明确输入、权限、作用域、预览、执行、验证和回滚语义的机器可调用命令，并让 UI、AI、MCP、CLI 和脚本复用同一条业务路径。

---

## 2. 状态标记和优先级

### 2.1 状态标记

| 标记 | 含义 |
|---|---|
| `已完成` | 已有可复用实现，并有明确入口或测试覆盖 |
| `部分完成` | 有局部实现，但未覆盖所有产品或未接入统一控制面 |
| `实验中` | 有原型、独立流程或测试，但接口和稳定性尚未定型 |
| `未完成` | 当前没有正式 AI 控制接口，或只能通过 UI/私有 callback 操作 |
| `阻塞` | 依赖外部媒体、硬件、模型、平台或未解决的架构问题 |
| `P0` | 影响整个控制面的基础能力，必须优先完成 |
| `P1` | 主要产品功能接入，完成后具备可用的跨模块自动化 |
| `P2` | 扩展能力、体验、性能和生产化完善 |

### 2.2 权限类别

| 权限 | 说明 | 默认建议 |
|---|---|---|
| `read` | 读取项目、素材、状态和诊断，不产生修改 | 自动允许 |
| `preview` | 生成差异、预览、计划、临时渲染，不修改正式项目 | 自动允许或低风险确认 |
| `write` | 修改时间线、节点、调色、字幕、工程状态 | 项目范围内可配置审批 |
| `file_write` | 写出视频、图片、字幕、工程文件 | 需要路径范围和输出策略 |
| `network` | 上传、下载、调用外部服务、发布 | 默认禁止，按域名和任务授权 |
| `destructive` | 删除、覆盖、清空、替换原始资产或不可逆操作 | 始终单独确认或显式自动化策略 |
| `system` | Shell、任意 Python、操作系统、凭据和全局文件 | 不提供给模型 |

---

## 3. 当前项目已完成内容清单

### 3.1 统一 AI 编辑控制底座：已完成 / P0 基础

主要文件：

- [`src/features/edit/ai_editing/tool_facade.py`](../src/features/edit/ai_editing/tool_facade.py)
- [`src/features/edit/ai_editing/capability_catalog.py`](../src/features/edit/ai_editing/capability_catalog.py)
- [`src/features/edit/ai_editing/capability_manifest.py`](../src/features/edit/ai_editing/capability_manifest.py)
- [`src/features/edit/ai_editing/edit_api.py`](../src/features/edit/ai_editing/edit_api.py)
- [`src/features/edit/ai_editing/approval_service.py`](../src/features/edit/ai_editing/approval_service.py)

当前已具备：

| 能力 | 当前状态 | 说明 |
|---|---|---|
| 工具统一入口 | 已完成 | `ToolFacade` 负责能力发现、权限过滤、工具调用和部分审计 |
| 能力目录 | 已完成但不完整 | 有工具元数据、域、权限、预算、幂等性、取消、质量门和补偿信息 |
| 能力清单 | 部分完成 | 能返回工具和产品能力，但目录中仍存在未被传输层暴露的概念能力 |
| 项目快照 | 已完成 | 能读取项目和时间线的结构化状态 |
| Composite 快照 | 已完成 | 能读取 Composite 状态和参数相关信息 |
| 素材搜索 | 已完成 | 已有素材搜索入口 |
| 素材分析读取 | 已完成 | 可读取已有分析结果；完整分析触发链路仍需统一 |
| 编辑计划验证 | 已完成 | 在正式修改前检查操作、参数和依赖 |
| 编辑计划应用 | 已完成但需授权 | 使用宿主发放的 `ApprovalGrant` |
| Composite 编辑验证/应用 | 已完成但需授权 | 使用独立 Composite 编辑协议 |
| 预览渲染 | 部分完成 | 工具和任务入口存在，需继续统一任务事件和结果验证 |
| 导出请求 | 部分完成 | 能请求导出和执行预检，但并非所有 Deliver 设置已进入 AI 接口 |
| 任务状态 | 已完成 | 能查询任务状态 |
| 取消任务 | 部分完成 | 有工具入口，但不同任务类型的取消和补偿语义仍需统一 |
| 帮助和诊断解释 | 已完成 | 有帮助搜索和诊断解释工具 |
| 渲染诊断 | 已完成 | 能读取部分运行时 shader/渲染错误 |
| 面板建议 | 已完成但受限 | 能提出显示相关面板的建议，需要用户确认，不能模拟 UI 操作 |

当前 `ToolFacade` 中已经存在的主要工具包括：

```text
topos.get_capabilities
topos.get_project_snapshot
topos.get_composite_snapshot
topos.inspect_subtitle_track
topos.search_assets
topos.get_asset_analysis
topos.validate_edit_plan
topos.validate_composite_edit
topos.apply_edit_plan
topos.apply_composite_edit
topos.render_preview
topos.request_export
topos.get_job_status
topos.cancel_job
topos.search_help
topos.explain_diagnostic
topos.show_relevant_panel
topos.get_render_diagnostics
```

### 3.2 Edit 时间线控制：已完成度较高 / P1

当前计划编译器已经覆盖一批可编译的操作，包括：

- 片段增加、删除、移动、裁剪、分割和替换；
- 添加转场、设置淡入淡出；
- 变换、裁剪、透明度和速度；
- 添加、删除效果；
- 确保轨道存在、重命名轨道、设置轨道状态；
- 设置标记；
- 创建字幕轨道；
- 更新字幕轨道；
- 增加、修改、删除字幕；
- 字幕转图形；
- 应用字幕图形模板和参数；
- 图形与字幕之间的转换。

实现位置：

- [`src/features/edit/ai_editing/plan_compiler.py`](../src/features/edit/ai_editing/plan_compiler.py)
- [`src/features/edit/ai_editing/capability_manifest.py`](../src/features/edit/ai_editing/capability_manifest.py)

已经具备的工程机制：

- 计划先验证再应用；
- 需要基于项目、时间线和修订版本的授权；
- 支持预览和差异；
- 与编辑会话、命令/服务和持久化链路连接；
- 支持一定程度的撤销、任务和错误处理。

尚未完成：

- 所有 Edit UI 操作没有全部映射到计划编译器；
- FCPXML、OTIO、字幕导入导出、音轨/stem、场景检测等 UI 功能没有全部进入统一工具表；
- 复杂编辑任务缺少标准化的跨操作事务和中间检查点；
- 计划应用后的视觉、音频和时间线后置验证还不够全面。

### 3.3 Composite 参数和动画控制：部分完成 / P1

当前已支持：

- 参数值修改；
- 参数关键帧；
- 插值方式；
- 贝塞尔控制柄；
- 切线模式；
- 外推方式；
- 参数表达式添加、修改和删除。

当前未完整接入：

- 添加节点；
- 删除节点；
- 复制节点；
- 节点分组和解组；
- 节点连接和断开；
- 节点顺序和层级；
- 材质、资源和依赖管理；
- 图结构导入导出；
- Scene Outliner 和节点图视图中的全部语义动作；
- Composite 渲染设置和节点级质量验证。

当前 UI action 已存在这些相关动作，但它们尚未全部进入中央 AI 控制面。因此 Composite 目前是“参数控制较强、图结构控制不完整”。

### 3.4 Agent 执行框架：已完成基础 / P0-P1

主要文件：

- [`src/features/edit/ai_editing/chat_run_driver.py`](../src/features/edit/ai_editing/chat_run_driver.py)
- [`src/features/edit/ai_editing/agent_task_executor.py`](../src/features/edit/ai_editing/agent_task_executor.py)
- [`src/features/edit/ai_editing/mcp_gateway.py`](../src/features/edit/ai_editing/mcp_gateway.py)
- [`src/features/edit/ai_editing/mcp_local_ipc.py`](../src/features/edit/ai_editing/mcp_local_ipc.py)
- [`src/features/edit/ai_editing/mcp_stdio.py`](../src/features/edit/ai_editing/mcp_stdio.py)
- [`src/features/edit/ai_editing/toposctl.py`](../src/features/edit/ai_editing/toposctl.py)

已经完成：

- Chat 工具循环；
- Agent 任务执行器；
- 计划、审批、执行、验证、恢复流程；
- 工具 schema 和动态工具可见性；
- MCP JSON-RPC 分发；
- 本地 Unix socket IPC；
- 当前用户 peer credential 检查；
- GUI 线程调度；
- CLI 读、验证、预览和部分编辑命令；
- 工具调用审计和限流基础；
- 模型不能自行生成写权限授权；
- 工具输出被视为不可信输入。

尚未完成：

- Chat、Agent、MCP、CLI 尚未共享全部产品域能力；
- 无统一的长任务事件流；
- 跨模块长任务缺少统一 checkpoint、补偿和恢复；
- 无完整 headless 项目执行模式；
- 普通聊天模式中写工具仍然被结构性限制；
- 默认 feature flag 和产品模式限制了实际可见能力。

### 3.5 权限、审批和路径保护：已完成基础 / P0

当前已经具备：

- `read`、`preview`、`write`、`file_write` 等权限分类；
- 宿主生成的随机、一次性、范围绑定授权；
- 授权绑定项目、时间线、修订版本、计划和差异；
- 项目切换或关闭时撤销授权；
- 文件输出需要路径授权和导出预检；
- MCP 默认最大权限为只读；
- 本地 IPC 的用户身份检查；
- 不允许模型自己伪造 approval token。

尚未完成：

- 项目范围自动模式的策略定义；
- 网络域名 allowlist；
- 云上传、发布和外部写入的统一授权；
- 删除、覆盖、替换等破坏性动作的统一策略；
- 任务预算、模型预算、文件大小预算和时间预算的统一执行；
- 全部工具都拥有一致的审计字段和追踪 ID。

### 3.6 脚本 API：功能较丰富，但不属于安全的 AI 控制面

主要文件：

- [`src/features/scripting/api.py`](../src/features/scripting/api.py)
- [`src/features/scripting/composite_api.py`](../src/features/scripting/composite_api.py)
- [`src/features/scripting/engine.py`](../src/features/scripting/engine.py)

当前脚本 API 已经覆盖：

- 打开、保存、保存项目；
- 图层管理；
- 曝光、对比度、饱和度、鲜艳度；
- 色温、色调、高光、阴影；
- 色相偏移、中间调细节、锐化；
- Lift、Gamma、Gain；
- 曲线和 HSL 曲线；
- 片段列表、片段调色、调色快照和差异；
- Composite 参数、关键帧和表达式。

当前脚本执行使用可信 Python 环境和 `exec`。它可以作为未来控制面的内部适配器，但不能直接暴露给模型。必须先增加沙箱、白名单、资源预算、项目作用域、超时和回滚，否则不能作为“全自动 AI 入口”。

### 3.7 AI 插件和媒体生成：部分完成 / P1-P2

当前已有：

- chat 插件；
- image generation；
- video generation；
- music generation；
- motion graphic；
- ASR、TTS、vision、embedding 等能力路由基础；
- GenerationBin 结果管理；
- provider-aware 和 fail-closed 的能力路由。

尚未完成：

- 所有生成结果没有用统一任务协议接入项目修改、素材落盘和后置验证；
- 外部 provider、网络、凭据和上传权限仍是分散管理；
- 生成能力默认受 feature flag 和 provider 配置限制；
- AI 不能统一控制每个模型的参数、重试、成本和质量门；
- 缺少跨模块的媒体生成、编辑、合成和导出流水线。

### 3.8 Teaching Animation：独立 AI 流程已存在，但未统一 / 实验中

主要文件：

- [`src/features/teaching_animation/module.py`](../src/features/teaching_animation/module.py)
- [`src/features/teaching_animation/ai/capability_context.py`](../src/features/teaching_animation/ai/capability_context.py)
- [`src/features/teaching_animation/ai/action_matrix.py`](../src/features/teaching_animation/ai/action_matrix.py)

当前已有：

- 创意 brief；
- 提案卡；
- 场景创建和修改；
- HTML/动画生成流程；
- 预览、故事板和帧预览；
- 插入或替换时间线片段；
- 确认卡和质量门；
- 场景导出。

尚未完成：

- 与中央 Capability Registry 统一；
- 与 ToolFacade/MCP/CLI 统一；
- 场景操作的 schema、修订版本、事务和回滚标准化；
- 场景资源、时间线、导出结果之间的完整验证；
- 上传和外部发布的统一权限策略。

### 3.9 测试基础：较好，但存在真实媒体阻塞

当前 AI 编辑、AI Harness 和 AI Workflow 相关测试规模较大。最近一次相关测试结果为：

```text
2856 passed
1 skipped
10 failed
```

失败集中在真实视频媒体探测矩阵，主要结果为 `PROBE_UNAVAILABLE`。这不等于全部 AI 架构失效，但意味着以下目标尚未满足：

- AI 自动分析真实媒体；
- 自动验证交付素材；
- 自动完成多编码格式验收；
- 在真实媒体环境下稳定完成导出闭环。

在进入生产级全自动模式前，必须修复或明确配置 ffprobe/PyAV/测试媒体依赖，并把真实媒体矩阵纳入发布门禁。

---

## 4. 尚未完成的全功能控制矩阵

### 4.1 工程和项目管理

| 编号 | 能力 | 当前状态 | 需要完成的工作 | 优先级 |
|---|---|---|---|---|
| PROJ-001 | 新建项目 | 未完成 | 增加项目模板、分辨率、帧率、色彩空间、音频设置的语义命令 | P1 |
| PROJ-002 | 打开/关闭项目 | 部分完成 | 增加项目生命周期状态、未保存变更检查和 AI 可读错误 | P1 |
| PROJ-003 | 保存项目 | 部分完成 | 暴露保存、另存为、自动保存和保存验证 | P1 |
| PROJ-004 | 项目快照 | 已完成 | 补充所有模块、资源、修订版本和脏状态 | P0 |
| PROJ-005 | 项目迁移 | 未完成 | 暴露版本检查、依赖收集、迁移预览和回滚 | P2 |
| PROJ-006 | 撤销/重做 | 部分完成 | 统一命令日志、事务 ID 和跨模块撤销 | P0 |
| PROJ-007 | 版本/检查点 | 未完成 | 增加 AI 自动检查点、标签和恢复入口 | P1 |
| PROJ-008 | 工程依赖 | 未完成 | 读取、定位、替换、离线检查和重新链接媒体依赖 | P1 |

### 4.2 素材和媒体

| 编号 | 能力 | 当前状态 | 需要完成的工作 | 优先级 |
|---|---|---|---|---|
| MEDIA-001 | 搜索素材 | 已完成 | 增加过滤、排序、路径范围、媒体类型和语义搜索 | P1 |
| MEDIA-002 | 媒体分析 | 部分完成 | 把分析触发、等待、结果 schema、失败和缓存统一 | P1 |
| MEDIA-003 | 素材导入 | 未完成 | 暴露导入、复制/链接策略、校验和重复检测 | P1 |
| MEDIA-004 | 素材整理 | 未完成 | 暴露 bin、标签、标记、收藏、代理和离线状态 | P1 |
| MEDIA-005 | 音频分析 | 部分完成 | 增加响度、波形、语音、静音和质量检查工具 | P1 |
| MEDIA-006 | 视频探测 | 阻塞 | 修复 PyAV/ffprobe 和真实媒体矩阵 | P0 |
| MEDIA-007 | 代理和缓存 | 未完成 | 暴露代理生成、状态、替换和清理策略 | P2 |
| MEDIA-008 | 素材转换 | 未完成 | 统一转码、代理、抽帧、缩略图和质量验证 | P2 |

### 4.3 Color

| 编号 | 能力 | 当前状态 | 需要完成的工作 | 优先级 |
|---|---|---|---|---|
| COLOR-001 | 基础调色 | 只有脚本/局部实现 | 暴露结构化 grade 参数、范围、单位和验证 | P1 |
| COLOR-002 | 色轮 | 未统一 | 暴露 lift/gamma/gain、offset、contrast/pivot 等命令 | P1 |
| COLOR-003 | 曲线 | 只有脚本/局部实现 | 暴露曲线点增删、移动、平滑和预览差异 | P1 |
| COLOR-004 | HSL 曲线 | 只有脚本/局部实现 | 暴露 hue/sat/lum 选择和曲线编辑 | P1 |
| COLOR-005 | LUT | UI/业务存在 | 暴露导入、应用、强度、替换、移除和路径校验 | P1 |
| COLOR-006 | Qualifier | UI 存在 | 暴露颜色范围选择、软化、反转和 matte 预览 | P1 |
| COLOR-007 | Mask | UI 存在 | 暴露创建、编辑、跟踪、组合、启用和验证 | P1 |
| COLOR-008 | Tracker | UI 存在 | 暴露跟踪开始、暂停、重算、应用和质量判断 | P2 |
| COLOR-009 | Film Grain | UI 存在 | 暴露类型、强度、尺度、种子和预览 | P2 |
| COLOR-010 | Camera RAW | UI 存在 | 暴露 RAW 参数、相机元数据和安全范围 | P2 |
| COLOR-011 | Denoise/Deblur | UI/模型存在 | 暴露模型选择、强度、预算、预览和结果验证 | P2 |
| COLOR-012 | 调色复制/粘贴 | 未统一 | 暴露 grade snapshot、diff、copy、paste、batch apply | P1 |
| COLOR-013 | 调色质量检查 | 未完成 | 暴露曝光、色域、肤色、噪声和闪烁检查 | P1 |

### 4.4 Edit

| 编号 | 能力 | 当前状态 | 需要完成的工作 | 优先级 |
|---|---|---|---|---|
| EDIT-001 | 基础片段编辑 | 已完成度较高 | 补充全部边界、音视频联动和批量操作 | P0 |
| EDIT-002 | 复杂时间线事务 | 部分完成 | 增加事务、checkpoint、冲突检测和回滚 | P0 |
| EDIT-003 | 音频轨道 | 部分完成 | 暴露音轨、音量、增益、淡化、静音、声道和混音 | P1 |
| EDIT-004 | 字幕 | 部分完成 | 统一导入、编辑、样式、图形化、导出和验证 | P1 |
| EDIT-005 | 场景检测 | UI/服务存在 | 暴露分析、候选切点、确认和应用 | P1 |
| EDIT-006 | FCPXML/OTIO | UI 入口存在 | 暴露导入、导出、映射、冲突和报告 | P1 |
| EDIT-007 | 标记和注释 | 部分完成 | 补充批量、颜色、范围和审阅状态 | P1 |
| EDIT-008 | 播放控制 | UI 存在 | 只暴露安全的读取、定位、播放状态，不依赖点击 UI | P2 |
| EDIT-009 | 代理/离线编辑 | 未统一 | 暴露离线诊断、代理切换和恢复 | P2 |

### 4.5 Composite

| 编号 | 能力 | 当前状态 | 需要完成的工作 | 优先级 |
|---|---|---|---|---|
| COMP-001 | 参数和关键帧 | 已完成 | 增加全面 schema、单位、范围和视觉验证 | P0 |
| COMP-002 | 表达式 | 已完成基础 | 增加编译、依赖、错误、预算和沙箱 | P1 |
| COMP-003 | 创建节点 | 未统一 | 暴露节点类型、默认参数和资源依赖 | P0 |
| COMP-004 | 删除节点 | 未统一 | 增加删除预览、引用检查和可恢复删除 | P0 |
| COMP-005 | 复制/分组 | UI 存在 | 暴露复制、分组、解组和命名 | P1 |
| COMP-006 | 节点连线 | 未统一 | 暴露端口、类型检查、循环检测和连线差异 | P0 |
| COMP-007 | 图结构导入导出 | 未统一 | 增加版本化 graph schema 和兼容迁移 | P1 |
| COMP-008 | 资源依赖 | 部分存在 | 统一素材、材质、纹理和字体依赖检查 | P1 |
| COMP-009 | Composite 质量验证 | 未完成 | 增加节点错误、黑帧、透明度、性能和输出验证 | P1 |

### 4.6 Deliver

| 编号 | 能力 | 当前状态 | 需要完成的工作 | 优先级 |
|---|---|---|---|---|
| DELIVER-001 | 单次导出请求 | 部分完成 | 补充完整 schema、预设和结果验证 | P0 |
| DELIVER-002 | 输出路径 | 部分完成 | 统一允许根目录、冲突策略、覆盖策略和磁盘检查 | P0 |
| DELIVER-003 | 编码器/封装格式 | 部分完成 | 暴露 codec、profile、level、码率、色深、色彩标签 | P1 |
| DELIVER-004 | 音频导出 | 部分完成 | 暴露采样率、声道、响度、格式和音轨选择 | P1 |
| DELIVER-005 | 字幕/SRT | UI 存在 | 暴露字幕格式、轨道、编码和验证 | P1 |
| DELIVER-006 | Stems/批处理 | UI 存在 | 暴露批量任务、依赖、命名和队列控制 | P1 |
| DELIVER-007 | 导出队列 | UI 存在 | 统一队列、优先级、暂停、恢复、取消和重试 | P1 |
| DELIVER-008 | 导出质量检查 | 部分完成 | 统一探测、首尾帧、时长、音画同步和码率检查 | P0 |
| DELIVER-009 | 云上传/发布 | 部分存在 | 增加网络权限、目标 allowlist、结果确认和审计 | P2 |

### 4.7 Teaching Animation 和 AI Workflow

| 编号 | 能力 | 当前状态 | 需要完成的工作 | 优先级 |
|---|---|---|---|---|
| MOTION-001 | 创意 brief | 已完成局部 | 接入中央能力目录和统一任务 ID | P1 |
| MOTION-002 | 场景提案 | 已完成局部 | 标准化 proposal schema 和确认策略 | P1 |
| MOTION-003 | 场景创建/修改 | 部分完成 | 统一命令、事务、验证和回滚 | P1 |
| MOTION-004 | 预览/故事板 | 部分完成 | 接入统一 preview job 和结果引用 | P1 |
| MOTION-005 | 时间线插入/替换 | 部分完成 | 使用中央时间线命令和 revision 检查 | P0 |
| MOTION-006 | 场景导出 | 部分完成 | 接入 Deliver 协议和质量检查 | P1 |
| WORKFLOW-001 | AI Workflow 编译 | 实验中 | 明确与中央 Agent Executor 的关系 | P1 |
| WORKFLOW-002 | Workflow 运行 | 实验中 | 统一取消、重试、预算、checkpoint 和审计 | P1 |
| WORKFLOW-003 | Workflow 外部控制 | 未完成 | 暴露 MCP/CLI/程序化调用入口 | P1 |

### 4.8 系统、素材库、脚本和设置

| 编号 | 能力 | 当前状态 | 需要完成的工作 | 优先级 |
|---|---|---|---|---|
| SYS-001 | 能力和 feature flag | 部分完成 | 返回实际启用状态、原因、依赖和可用替代方案 | P0 |
| SYS-002 | 面板显示 | 只支持建议 | 只提供语义导航，不允许 AI 模拟点击 | P2 |
| SYS-003 | Vault 查询 | 独立服务 | 接入只读素材/项目检索工具，明确数据范围 | P1 |
| SYS-004 | File Explorer | 独立功能 | 暴露受限文件操作，禁止任意路径访问 | P1 |
| SYS-005 | 偏好设置 | UI 存在 | 只暴露白名单配置，区分用户级和项目级 | P2 |
| SYS-006 | 脚本调用 | 可信 Python | 设计受限 DSL 或 allowlist，不开放任意 exec | P1 |
| SYS-007 | GPU/渲染诊断 | 部分完成 | 统一资源、shader、纹理、内存和性能诊断 | P1 |
| SYS-008 | 应用重启/更新 | UI/系统功能 | 保持为特殊权限，必须显式确认 | P2 |

---

## 5. 目标统一控制面设计

### 5.1 建议的模块分层

```text
AI Agent / Chat / MCP / CLI / Script DSL
                    │
                    ▼
          Topos Control Gateway
                    │
          Capability Registry
                    │
      Policy / Scope / Revision / Audit
                    │
          Command + Transaction Layer
                    │
 ┌────────┬────────┬────────┬────────┬────────┐
 │ Project│ Media  │ Color  │ Edit   │Composite│
 ├────────┼────────┼────────┼────────┼────────┤
 │Deliver │ Motion │Models  │Vault   │Diagnose │
 └────────┴────────┴────────┴────────┴────────┘
                    │
                    ▼
       Existing services / render / model / IO
```

### 5.2 所有能力必须遵循的统一生命周期

```text
discover
  → read_state
  → plan
  → validate
  → preview
  → approve_by_policy
  → apply
  → wait_or_stream_events
  → verify_postconditions
  → commit_or_compensate
  → audit
```

### 5.3 统一命令元数据

每个 AI 可调用动作至少必须声明：

```json
{
  "id": "color.apply_grade",
  "version": "1.0",
  "domain": "color",
  "description": "Apply a validated grade to one or more clips",
  "input_schema": {},
  "output_schema": {},
  "permission": "write",
  "scope": "project.timeline.clip",
  "requires_revision": true,
  "preview_supported": true,
  "undo_supported": true,
  "idempotency": "key_required",
  "estimated_cost": "gpu_medium",
  "cancel_supported": true,
  "verification": ["grade_snapshot_matches"],
  "compensation": "restore_grade_snapshot",
  "side_effects": ["timeline_revision"],
  "network": false,
  "destructive": false
}
```

### 5.4 UI、AI、CLI 必须共享命令

后续不应继续新增“只供 UI 使用的私有 callback”。正确方向是：

```text
UI action ─┐
MCP tool ──┼──> CommandRegistry.dispatch(command, input)
CLI ───────┤              │
Script DSL ┘              ▼
          validation → transaction → service → verify
```

UI 只是命令的一个调用者，不能成为 AI 自动化的唯一入口。

---

## 6. 推荐开发顺序和阶段计划

## P0：能力盘点、统一契约和安全基线

### 目标

先把“软件到底有哪些可操作功能”完整盘点清楚，并建立不会随着模块扩展而失控的统一协议。

### 工作项

| 编号 | 工作项 | 产出 | 验证方法 |
|---|---|---|---|
| P0-001 | 扫描所有 UI action、menu descriptor、controller callback、service public method | 全项目动作清单 | 静态扫描结果和人工抽样对照 |
| P0-002 | 按 Project/Media/Color/Edit/Composite/Deliver/Motion/System 分类 | 功能域矩阵 | 每个功能必须有 owner 和状态 |
| P0-003 | 为每个动作标记 read/preview/write/file/network/destructive | 权限矩阵 | 权限审查和危险动作抽样 |
| P0-004 | 设计统一 command schema | `CommandSpec` 或等价协议 | schema 校验、版本兼容测试 |
| P0-005 | 设计统一 result、error、job、event schema | 控制面数据协议 | JSON schema 和错误码测试 |
| P0-006 | 设计 revision、idempotency、transaction、checkpoint 字段 | 并发和恢复协议 | 重复调用、过期 revision、进程中断测试 |
| P0-007 | 明确 feature flag 与实际能力的对应关系 | `get_capabilities` 增强版 | 关闭依赖后不能伪报可用 |
| P0-008 | 修复真实媒体探测依赖 | 可运行的 probe 环境 | 10 个失败用例全部通过或明确跳过原因 |
| P0-009 | 建立能力矩阵测试模板 | 自动化测试骨架 | 新增能力没有测试时 CI 失败 |

### P0 退出条件

- 每个用户可执行动作都有唯一 ID；
- 每个动作都有明确权限、作用域和副作用；
- 工具清单只返回当前真实可用能力；
- 所有写操作都经过同一授权入口；
- 真实媒体探测测试不再出现未解释的 `PROBE_UNAVAILABLE`；
- 没有功能只能通过点击 UI 才能执行的隐藏路径。

## P1：建立中央 Control Plane

### 目标

把现有 `ToolFacade` 扩展为产品级控制面，并解除它对 Edit 模块的强耦合。

### 工作项

| 编号 | 工作项 | 说明 |
|---|---|---|
| P1-001 | 建立 `ControlGateway` | 统一 MCP、CLI、Chat、Agent 和脚本 DSL 的入口 |
| P1-002 | 抽离通用 Capability Registry | Color、Edit、Composite、Deliver 等模块独立注册能力 |
| P1-003 | 扩展统一 CommandRegistry | UI 和 AI 复用同一命令分发路径 |
| P1-004 | 保留现有 ToolFacade 兼容层 | 旧工具映射到新 command ID，避免一次性破坏现有调用 |
| P1-005 | 增加统一 JobRegistry | 渲染、分析、导出、生成、跟踪、转码全部使用统一任务模型 |
| P1-006 | 增加统一 EventStream | 支持进度、日志、结果、失败、取消和恢复 |
| P1-007 | 增加统一 AuditRecord | 记录用户、模型、工具、计划、授权、revision 和结果 |
| P1-008 | 增加统一 Undo/Compensation | 每个写命令声明撤销或补偿方法 |
| P1-009 | 增加 headless host | 不依赖 UI panel 是否显示，仍通过业务服务执行 |

### P1 退出条件

- Topos Color、Composite、Deliver 可以在没有 Edit panel 的情况下启动控制面；
- UI、MCP 和 CLI 对同一个命令得到相同结果；
- 所有异步任务都能查询、取消、等待和验证；
- 所有写命令都有 revision 检查、审计和撤销策略；
- 新模块接入不需要修改中央工具循环的大量分支。

## P2：补齐 Color 控制

### 目标

优先把 Color 变成第一个完整接入统一控制面的主要产品域。

### 推荐顺序

1. 调色快照、差异和恢复；
2. 基础 grade 参数；
3. 色轮；
4. 曲线和 HSL 曲线；
5. LUT；
6. Qualifier；
7. Mask；
8. Tracker；
9. Film Grain、Soft Clip、Camera RAW；
10. Denoise、Deblur 等 GPU/模型能力；
11. 调色质量检查。

### 每个 Color 能力必须提供

- 当前状态读取；
- 参数 schema 和单位；
- 最小值、最大值、默认值和有效组合；
- 单片段和批量作用域；
- 预览和 diff；
- 快照和恢复；
- GPU 任务状态；
- 视觉或数值验证；
- 与色彩空间、HDR 和项目色彩管理的兼容性检查。

### P2 退出条件

- AI 可以完成一套完整调色，而不需要生成 Python 或模拟点击；
- 任意调色修改可预览、应用、验证和撤销；
- 失败不会留下半完成的调色状态；
- Color 独立产品和 Studio 中表现一致。

## P3：补齐 Composite 图结构控制

### 目标

将 Composite 从“参数/关键帧可控”提升到“图结构完整可控”。

### 工作项

- 节点类型发现和版本化；
- 节点创建和默认参数；
- 节点删除前引用分析；
- 节点复制、分组和解组；
- 端口和类型检查；
- 连线、断线和循环检测；
- 节点命名、排序和布局；
- 材质、纹理、字体和外部资源依赖；
- graph diff、preview、apply、undo；
- 节点图性能和错误验证。

### P3 退出条件

- AI 可以从空图建立可运行 Composite；
- AI 可以理解当前图结构并只修改指定节点；
- 图结构修改具有可读 diff 和可恢复 checkpoint；
- 图编译错误和渲染错误能以结构化结果返回。

## P4：补齐 Deliver、媒体和工程闭环

### 目标

让 AI 不仅能修改项目，还能可靠地产出和验证最终文件。

### 工作项

- 完整导出 preset schema；
- 编码器、封装、色深、色彩标签和音频配置；
- 字幕、音轨、stem、批处理；
- 输出命名、目录、冲突和覆盖策略；
- 任务队列、暂停、恢复、取消、重试；
- 输出文件探测和质量验证；
- 时长、首尾帧、音画同步、码率和色彩信息检查；
- 保存项目、依赖收集、重新链接和版本检查；
- 导出失败的自动补偿和安全重试。

### P4 退出条件

- 同一导出配置可通过 UI、MCP 和 CLI 得到相同结果；
- AI 能知道文件是否真正写出、是否可读取、是否符合要求；
- 失败导出不会被错误报告为成功；
- 多版本导出任务可批量执行和逐项验证。

## P5：统一 Teaching Animation、AI Workflow 和生成能力

### 目标

将现有独立 AI 子系统纳入同一任务、能力、权限和审计框架。

### 工作项

- 将 `action_matrix` 转换为中央 command specs；
- 统一 proposal、confirmation、preview、apply、verify；
- 场景和时间线使用同一 revision；
- 生成结果使用统一 asset/job schema；
- provider、模型、成本、网络和凭据进入权限策略；
- 将 AI Workflow 连接到中央 Agent Executor；
- 统一长任务取消、重试、checkpoint 和审计；
- 将生成素材、场景、时间线和 Deliver 连接成可验证流水线。

### P5 退出条件

- 一个 Agent 可以跨 Edit、Color、Composite、Motion 和 Deliver 连续执行任务；
- 任一阶段失败都能定位责任域并恢复；
- 生成、编辑、合成和导出有统一 job/event/audit 记录。

## P6：实现可控的自主运行模式

### 目标

在不开放任意系统权限的前提下，让用户可以明确选择自动化程度。

### 建议策略

| 模式 | 允许范围 | 适用场景 |
|---|---|---|
| `manual` | 每个写操作确认 | 默认模式、探索阶段 |
| `approve_each_write` | 计划可自动生成，写操作逐项确认 | 普通生产流程 |
| `project_scoped_auto` | 当前项目范围内自动执行可逆写操作 | 用户明确授权后的自动剪辑/调色 |
| `sandbox_auto` | 沙盒副本中自动执行，完成后生成差异 | 高风险批量实验 |
| `external_publish_confirm` | 上传、发布、覆盖和删除仍需确认 | 交付和云服务 |

### 自动模式必须绑定

- 项目 ID；
- 时间线 ID；
- 允许的功能域；
- 允许的文件根目录；
- 网络域名 allowlist；
- 最大步骤数；
- 最大 GPU/CPU 时间；
- 最大导出文件数量和容量；
- 最大模型成本；
- 自动 checkpoint 间隔；
- 失败重试次数；
- 全局停止按钮；
- 失去连接后的安全状态。

### P6 退出条件

- 用户可以清楚看到并修改自动化范围；
- AI 不能越过项目、目录、网络和预算边界；
- 自动执行任务可实时暂停；
- 每次修改都可恢复到自动任务开始前的 checkpoint；
- 外部发布、删除、覆盖和系统操作不会被隐式自动批准。

## P7：生产化、性能和跨产品验收

### 目标

将全功能 AI 控制从开发能力变成可长期运行的产品能力。

### 工作项

- Studio、Color、Edit、Composite 四种产品模式验收；
- macOS、Windows 和目标 GPU 后端验收；
- 小项目、长时间线、4K/6K、HDR、多音轨和高节点图压力测试；
- 长时间运行内存、GPU 资源和线程安全测试；
- MCP 外部客户端兼容测试；
- CLI 和 headless CI 测试；
- 数据损坏、断电、断网、模型超时和进程重启恢复测试；
- 安全审计、提示注入测试、路径逃逸测试、凭据泄露测试；
- 文档和能力清单自动生成。

### P7 退出条件

- 关键路径端到端测试全部通过；
- 没有未解释的功能缺口；
- 所有高风险动作均有可验证的权限边界；
- 失败、取消和恢复行为有稳定结果；
- 能力清单与实际实现自动保持一致。

---

## 7. 统一验证方法

### 7.1 静态能力覆盖验证

每次新增用户功能时，必须检查：

1. 是否有唯一 command ID；
2. 是否加入 Capability Registry；
3. 是否有输入和输出 schema；
4. 是否声明权限和副作用；
5. 是否声明作用域和 revision 要求；
6. 是否有 preview；
7. 是否有 verify；
8. 是否有 undo 或 compensation；
9. 是否有 audit；
10. 是否在至少一个产品模式可见。

建议把这 10 项做成自动 lint。缺任何一项都应进入 `incomplete_capability` 报告。

### 7.2 单元测试

每个 command 至少覆盖：

- 合法输入；
- 缺少必填参数；
- 类型错误；
- 范围错误；
- 无效资源；
- 过期 revision；
- 重复 idempotency key；
- 权限不足；
- 路径越界；
- 任务取消；
- 执行失败；
- 验证失败；
- 补偿成功；
- 补偿失败；
- 审计记录完整性。

### 7.3 Contract 测试

对每个工具验证：

- MCP schema 与实际函数一致；
- CLI schema 与 MCP schema 一致；
- ToolFacade 与 CommandRegistry 结果一致；
- 能力清单与实际 feature flag 一致；
- 禁用 provider 或模块时不会虚报能力；
- 错误码、状态枚举和进度字段稳定。

### 7.4 集成测试

至少覆盖以下链路：

```text
素材导入
  → 媒体分析
  → Edit 修改
  → Color 调色
  → Composite 合成
  → 字幕/动画
  → Deliver 导出
  → 输出验证
  → 项目保存
```

每一步都要检查：

- 状态是否正确；
- revision 是否递增；
- UI、MCP、CLI 结果是否一致；
- 任务是否可查询和取消；
- 失败是否保留可恢复状态；
- 最终输出是否符合用户要求。

### 7.5 Agent 端到端测试

建立固定需求集，而不是只测试单工具：

| 场景 | 需要验证的内容 |
|---|---|
| 基础剪辑 | 读取项目、搜索素材、创建轨道、剪切、保存 |
| 自动调色 | 分析素材、生成 grade、预览、应用、检查、撤销 |
| Composite | 创建节点、连线、设置参数、渲染、验证错误 |
| 字幕动画 | 分析语音、生成字幕、图形化、插入动画、预览 |
| 多版本交付 | 横版/竖版导出、命名、队列、结果验证 |
| 失败恢复 | 模型超时、媒体探测失败、导出失败、revision 冲突 |
| 安全边界 | 越权路径、网络域名、删除、覆盖、提示注入 |
| 长任务 | 进度、取消、重试、断开重连、恢复 |

### 7.6 视觉和媒体质量验证

AI 控制不能只验证“函数返回成功”，还必须验证实际媒体结果：

- 视频时长；
- 帧率和分辨率；
- 首帧和尾帧；
- 音频采样率、声道和响度；
- 音画同步；
- 色彩空间、色深和 HDR 元数据；
- 黑帧、透明度、闪烁和丢帧；
- Composite 节点错误和 shader 编译错误；
- 字幕时间码和溢出；
- 导出文件可重新打开和读取。

### 7.7 破坏性和故障测试

必须模拟：

- 应用在写入中途崩溃；
- GPU 任务中途失败；
- 用户取消任务；
- 网络断开；
- 模型请求超时；
- 媒体文件被移动；
- 磁盘空间不足；
- 目标输出已存在；
- 项目 revision 被其他操作改变；
- AI 重复发送同一个请求。

通过标准：项目不损坏，且能明确返回成功、失败、取消或待恢复状态。

---

## 8. 安全和自主控制边界

### 8.1 不应该直接开放给模型的能力

以下能力不应作为普通 AI 工具暴露：

- 任意 Shell；
- 任意 Python `exec`；
- 任意路径读写；
- 任意网络 URL 下载；
- 任意凭据读取；
- 任意系统设置；
- 任意应用重启或更新；
- 未授权删除和覆盖；
- 未授权云上传和发布。

### 8.2 必须防御的攻击和误操作

- 素材文件中的提示注入；
- 字幕、项目名称和元数据中的恶意指令；
- 模型输出伪造 approval token；
- 路径 `..` 逃逸和符号链接逃逸；
- 网络请求绕过 allowlist；
- 工具输出伪造成功状态；
- 重复调用造成重复片段或重复导出；
- 过大的循环、节点图或批处理任务；
- 生成媒体导致磁盘或显存耗尽；
- 外部 provider 返回恶意或不可信内容。

### 8.3 推荐安全保证

- 最小权限；
- 项目范围隔离；
- 输出目录隔离；
- 可信工具输出和不可信外部内容分层；
- 有界步骤和有界资源预算；
- 任务可取消；
- 写入前 checkpoint；
- 统一审计；
- 破坏性动作单独授权；
- 自动模式显式开启且可随时关闭。

---

## 9. 建议的验收门禁

每个阶段合并前必须满足以下门禁：

### 功能门禁

- 目标功能的 command 已注册；
- ToolFacade、MCP、CLI 至少有一个可验证入口；
- UI 和 AI 使用同一业务服务；
- 能力清单准确反映启用状态。

### 数据门禁

- schema 有版本；
- revision 检查存在；
- 输出可序列化；
- 错误有稳定错误码；
- 审计记录包含完整关联 ID。

### 安全门禁

- 无任意路径；
- 无未授权写入；
- 无工具自授予权限；
- 网络和文件范围经过限制；
- 破坏性动作有明确确认或策略授权。

### 质量门禁

- 单元测试通过；
- contract 测试通过；
- 端到端任务通过；
- 真实媒体输出可读取；
- 失败和取消测试通过；
- 资源释放和长时间运行测试通过。

---

## 10. 推荐执行顺序总表

| 阶段 | 名称 | 主要结果 | 依赖 |
|---|---|---|---|
| P0 | 能力盘点和契约 | 全功能清单、权限矩阵、统一 schema、测试骨架 | 无 |
| P1 | 中央 Control Plane | 统一 Gateway、Registry、Command、Job、Event、Audit | P0 |
| P2 | Color 完整接入 | 基础调色、曲线、LUT、Mask、质量检查 | P1 |
| P3 | Composite 图结构 | 节点、端口、连线、图 diff、图验证 | P1 |
| P4 | Deliver 和媒体闭环 | 全配置导出、队列、质量验证、保存工程 | P1、P2、P3 |
| P5 | Motion/Workflow/生成统一 | 跨模块 Agent 流程和统一任务协议 | P1、P4 |
| P6 | 项目范围自主模式 | 自动执行、预算、checkpoint、回滚 | P2-P5 |
| P7 | 生产化验收 | 跨产品、跨平台、压力、安全、恢复测试 | P6 |

不建议跳过 P0 和 P1 直接给每个模块分别增加 AI 工具。这样会继续扩大现有的多套入口问题，最终难以保证权限、回滚和能力清单的一致性。

---

## 11. 最终完成定义

当以下条件全部满足时，才可以宣称“AI 能够自主控制整个 Topos 项目”：

1. 每个用户可见的修改动作都有唯一机器可读 command；
2. 每个 command 都有输入、输出、权限、作用域、副作用和版本；
3. AI 可以发现真实可用能力，而不是读取静态假清单；
4. 所有写入都支持预览、revision 检查、审计和撤销/补偿；
5. 所有长任务都支持等待、进度、取消、重试和结果验证；
6. Color、Edit、Composite、Deliver、Motion、Project、Media 均通过同一控制面；
7. UI、MCP、CLI 和脚本 DSL 复用同一业务命令；
8. 不存在只能通过模拟点击才能执行的关键功能；
9. 自主模式有项目、路径、网络、资源和预算边界；
10. 外部上传、发布、删除和覆盖不会被隐式放行；
11. 真实媒体、GPU、模型、网络和进程故障都有可验证恢复路径；
12. Studio、Color、Edit、Composite 各产品模式的端到端测试全部通过；
13. 能力矩阵、测试矩阵和审计日志可以持续反映真实状态。

---

## 12. 当前下一步执行清单

建议立即按以下顺序开始：

- [ ] 建立全项目 action inventory，扫描 UI action、controller callback、service 和脚本 API；
- [ ] 为每个动作分配唯一 ID、功能域、权限和状态；
- [ ] 输出第一版“AI 可控性矩阵”；
- [ ] 将 `ToolFacade` 与 Edit 模块解耦，建立产品无关的 Control Gateway；
- [ ] 先接入 Color 的 grade snapshot、基础调色和曲线；
- [ ] 补齐 Composite 节点创建、删除和连线；
- [ ] 统一 Deliver 配置、队列和质量检查；
- [ ] 修复真实媒体 `PROBE_UNAVAILABLE` 测试；
- [ ] 建立跨模块端到端自然语言任务集；
- [ ] 设计并实现 `project_scoped_auto` 自动模式；
- [ ] 对每个阶段执行安全、回滚和资源释放测试；
- [ ] 每完成一个功能，立即更新本计划的状态和验收证据。

本计划的核心判断是：Topos 不需要把 AI 变成一个可以任意操作操作系统的“超级脚本”，而需要把整个软件变成一个对 AI 友好的、权限明确的、可验证的语义控制系统。这样才能真正实现长期稳定的自主运行。
