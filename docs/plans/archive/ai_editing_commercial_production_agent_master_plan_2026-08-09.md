# Topos Color 商业化全自动 AI 剪辑 Agent 总体优化计划

版本：v1.0

制定日期：2026-08-09

适用分支：`fix-ui`

目标：在现有 AI 剪辑闭环的基础上，构建可配置、可观察、可恢复、可审计、可控成本，并能稳定交付成片的生产级 AI Agent。

---

## 1. 目标定义

### 1.1 最终用户体验

用户只需提出类似下面的请求：

> 把这段访谈剪成 60 秒竖屏短视频，保留核心观点，自动加字幕、BGM、节奏点和片尾 Logo，生成预览；我确认后导出 1080×1920 成片。

Agent 应自动完成：

1. 理解目标、平台、时长、风格和权限范围；
2. 检查素材、时长、音频、字幕和模型可用性；
3. 生成可解释的剪辑计划，而不是直接猜测并写入工程；
4. 调用 DeepSeek 完成理解、规划、改稿和异常解释；
5. 调用真实的分析、配音、音乐、图片或视频 provider；
6. 生成低质量预览并展示差异；
7. 按用户权限自动执行或等待审批；
8. 失败时重试、恢复或给出明确的下一步；
9. 导出经过质量门禁的最终文件，并保留完整来源、授权和操作审计。

### 1.2 商业化目标

| 目标 | 生产要求 |
|---|---|
| 可用性 | 常规任务可完成；外部 provider 失败不会污染工程 |
| 可控性 | 自动执行范围、预算、上传范围和时间线写入权限可配置 |
| 可恢复 | 应用重启后可恢复或明确终止后台任务，不产生幽灵任务 |
| 可解释 | 每个剪辑决策都有素材证据、规则或模型输出摘要 |
| 可审计 | 记录模型、provider、参数摘要、授权、成本、结果和版本 |
| 可扩展 | DeepSeek、ASR、TTS、图片、视频、音乐 provider 可替换 |
| 可交付 | 预览、导出、字幕、音频混合和媒体引用可通过质量门禁 |
| 可运营 | 有指标、日志、错误码、反馈闭环、灰度开关和回滚方案 |

---

## 2. 当前基线与剩余差距

### 2.1 已完成基线

| 能力 | 当前状态 | 主要位置/提交 |
|---|---|---|
| AI 剪辑闭环 | 已具备请求、规划、预览、审批、执行、撤销链路 | `src/features/ai_harness/`、`src/app/controllers/ai_edit_controller.py` |
| DeepSeek 文本能力 | 已支持配置后用于对话/规划类任务 | `src/features/edit/ai_editing/provider_profiles.py`、`llm_variant_planner.py` |
| 类型化 Agent Runtime | 已有 Invocation、Run、Job、Artifact 和权限模型 | `src/features/ai_harness/` |
| 本地/分析能力 | 已有分析、字幕、语义索引和若干本地 provider 接缝 | `src/features/edit/ai_editing/` |
| AI 导出流程 | 已有预检、确认、后台 Job 和失败可见性 | `src/features/edit/ai_editing/export_jobs.py` |
| 生成素材箱 | 已有生成候选、provenance、预览、显式导入和时间线审批 | `generation_bin.py`、`media_generation_service.py` |
| 远程媒体物化 | 已有 HTTP 缓存、原子写入、进度、取消和 Job Drawer 接入 | 提交 `39428b3c` |

### 2.2 必须补齐的差距

| 差距 | 现状 | 影响 |
|---|---|---|
| 真实媒体生成 | 默认仍注册 `DeterministicGenerationProvider`；已增加可配置 HTTP provider-neutral adapter，可由 profile + credential 接入真实服务 | 未配置真实媒体 profile 时仍只能演示；配置后可走提交/轮询/取消/物化闭环 |
| DeepSeek 生产韧性 | API 可用，但需要超时、重试、结构化输出、限流和成本记录 | 长任务容易失败或难以诊断 |
| Agent 自主循环 | 已有规划和执行骨架，尚需 Observe → Plan → Act → Verify → Recover | 无法稳定处理复杂、多步骤任务 |
| Job 持久化 | 当前 Job 主要是内存状态 | 应用重启后任务不可恢复 |
| 质量评估 | 有单元测试，尚需素材级、成片级和模型级评测集 | 无法判断“能运行”是否等于“剪得好” |
| 商业安全 | 已有部分 consent/provenance，需要系统化权限、隐私、版权和审计 | 无法对外承诺安全合规 |
| 全量回归 | 仍有少量既有配置/UI 断言需清理 | 发布门禁不够干净 |

---

## 3. 总体架构目标

```text
用户请求
   ↓
Invocation Router
   ↓
Context / Evidence Builder
   ↓
DeepSeek Planner（结构化计划，不直接写工程）
   ↓
Policy / Consent / Budget Gate
   ↓
TaskGraph Executor
   ├─ 本地分析：ASR / OCR / 场景 / 人声 / 音频
   ├─ 外部 provider：图片 / 视频 / 音乐 / 配音
   ├─ 编辑工具：剪切 / 拼接 / 字幕 / 混音 / 调色 / 导出
   └─ Verify：时长 / 黑帧 / 音频 / 字幕 / 引用 / 版权 / 质量
   ↓
Preview / Diff / Approval 或受限自动执行
   ↓
CommandStack / Export Job / Artifact / Audit
   ↓
反馈、重试、恢复、指标和模型评估
```

硬约束：

- 模型永远只提出结构化意图和计划，不能绕过 `ApprovalService`、`CommandStack` 或 `ExportPreflight`；
- 外部 provider 永远通过 provider-neutral 接口接入，不能把 SDK 直接放入 UI；
- 所有长任务都必须进入共享 `JobRegistry`，支持进度、取消、失败和恢复语义；
- 所有写入都必须幂等、可撤销，并绑定 `project_id`、`timeline_id` 和基础版本；
- 不保存 API Key、完整敏感 prompt、远程临时地址或不必要的绝对路径；
- 自动模式必须是用户明确打开的受限模式，不得把“已连接 API”解释成“允许无限自动执行”。

---

## 4. 分阶段执行总表

估时是单人连续开发的相对工作量，实际以阶段验收为准。每个阶段完成后必须测试、提交并更新本表状态。

| 阶段 | 优先级 | 主题 | 预计工作量 | 依赖 | 阶段出口 |
|---|---:|---|---:|---|---|
| P0 | P0 | 基线冻结与生产配置 | 1–2 天 | 无 | 能明确区分 Demo、Beta、Production 能力 |
| P1 | P0 | DeepSeek 生产级适配 | 3–5 天 | P0 | 文本 Agent 稳定、可重试、可审计、可限费 |
| P2 | P0 | 真实媒体 provider 适配 | 5–10 天 | P0、P1 | 图片/视频/音乐/配音至少各有一个真实 provider |
| P3 | P0 | 自主 Agent 执行循环 | 5–8 天 | P1、P2 | 能自动观察、规划、执行、验证和恢复多步任务 |
| P4 | P0 | 素材理解与证据增强 | 5–8 天 | P1、P3 | 剪辑决策基于可追溯素材证据 |
| P5 | P1 | 商业化剪辑工作流 | 5–8 天 | P3、P4 | 短视频、访谈、广告、播客等模板可交付 |
| P6 | P0 | 安全执行与自动模式 | 4–6 天 | P3、P5 | 自动模式有边界、有审批、有回滚 |
| P7 | P0 | Job 持久化、恢复与可观测性 | 5–8 天 | P2、P3 | 崩溃、断网、重启后任务可恢复或安全终止 |
| P8 | P0 | 导出交付与质量门禁 | 4–7 天 | P5、P6、P7 | 最终成片经过自动质量检查并可交付 |
| P9 | P0 | 安全、版权、隐私与成本 | 5–8 天 | P2、P6、P7 | 具备商业化运营所需的风险控制 |
| P10 | P0 | 评测、性能和发布门禁 | 7–10 天 | P4–P9 | 有数据证明稳定性、质量和成本可接受 |
| P11 | P1 | Beta 灰度与商业运营 | 持续 | P10 | 可灰度、可回滚、可收集反馈并持续优化 |

---

## 5. 阶段详细计划

### P0：基线冻结与生产配置

| 项目 | 具体任务 | 交付物 | 验收标准 |
|---|---|---|---|
| 能力分级 | 为每项能力标注 `demo / beta / production` | 能力矩阵 | UI 和日志不会把 deterministic provider 显示成真实生成 |
| 配置检查 | 检查 DeepSeek profile、endpoint、model、超时和凭证引用 | 启动自检报告 | API 未配置时给出可读错误，不发生静默降级 |
| Feature Flag | 增加真实 provider、自动模式、云上传、自动导出开关 | 配置项与默认值 | 默认安全，旧工程可继续打开 |
| 契约冻结 | 冻结 Invocation、Job、Artifact、Provider、Audit 的字段 | 契约文档/测试 | provider 替换不改 UI 和 controller |
| 测试基线 | 清理全量测试中既有 UI/用户配置污染问题 | 基线报告 | 发布相关测试命令可重复运行 |

建议文件：`src/features/edit/ai_editing/provider_profiles.py`、`src/app/controllers/ai_editing_mixin.py`、`src/features/ai_harness/`、`docs/`。

### P1：DeepSeek 生产级适配

DeepSeek API 已连接，本阶段不是重新接入，而是把现有可用连接变成生产级模型通道。

| 项目 | 具体任务 | 交付物 | 验收标准 |
|---|---|---|---|
| 请求层 | 统一 timeout、连接错误、HTTP 错误、限流和服务端错误 | `DeepSeekClient` 生产实现 | 每类错误有稳定 error code |
| 重试 | 只对幂等请求做指数退避；禁止重复执行写入动作 | Retry policy | 重试次数、间隔和最终错误可见 |
| 输出层 | 结构化 JSON schema、版本号、字段校验、模型拒答处理 | Planner response validator | 非法输出不会进入编辑执行 |
| 流式体验 | 对话流式显示，计划仍以完整结构提交 | Streaming adapter | UI 不阻塞，断流可恢复为明确失败 |
| 成本审计 | 记录 token、模型、请求类型、预算和累计成本 | Cost ledger | 超预算在调用前阻断 |
| 模型路由 | DeepSeek profile、低/中/高推理强度、fallback 策略 | Model router | fallback 不改变用户授权范围 |
| 隐私策略 | prompt 脱敏、素材上传范围提示、日志脱敏 | Privacy guard | 日志和 session 不含 API Key/敏感原文 |

### P2：真实媒体 provider 适配

`DeterministicGenerationProvider` 只保留为测试 provider。真实 provider 必须逐个通过统一协议接入。

| provider 类型 | 必须实现 | 结果要求 |
|---|---|---|
| 图片 | 提交、轮询/回调、取消、下载、尺寸/格式校验 | 返回真实预览句柄、文件类型和 provenance |
| 视频 | 异步任务、进度、轮询、取消、下载、时长校验 | 失败/超时/审核拒绝不能进入素材箱 |
| 音乐 | 生成、时长/采样率校验、授权状态 | 商用许可未知时必须阻断商业导出声明 |
| 配音/TTS | 文本分段、音频下载、音画对齐、失败重试 | 可加入字幕和时间线音频轨 |

每个真实 provider 都要有：

- 独立 adapter，不在 UI 中导入 SDK；
- API Key 只来自 credential store；
- consent、上传范围、费用和版权确认；
- JobRegistry 状态、进度、取消和超时；
- 幂等 request key，避免重试重复扣费；
- 内容安全结果、provider 版本、模型、授权和来源摘要；
- mock server/fixture 测试，不依赖真实付费 API 才能跑 CI。

建议入口：`src/features/edit/ai_editing/generation_provider.py`、`media_generation_service.py`、`generation_models.py`、`src/app/controllers/ai_editing_mixin.py`。

### P3：自主 Agent 执行循环

目标是从“一次请求调用一个 adapter”升级为可恢复的有限状态 Agent。

| 环节 | 具体任务 | 必须保留的状态 |
|---|---|---|
| Observe | 收集工程、时间线、素材、分析证据、能力和权限 | ContextBundle、evidence refs |
| Plan | DeepSeek 输出结构化 TaskGraph/EditPlan | plan hash、版本、预算、依据 |
| Gate | 校验权限、成本、上传、版权、时间线冲突 | consent、policy decision |
| Act | 顺序或并行执行分析、生成、编辑、导出节点 | node state、job refs、artifacts |
| Verify | 检查每个节点结果和整体成片 | diagnostics、quality report |
| Recover | 重试、替代 provider、等待用户、回滚 | checkpoint、retry count、reason |
| Learn | 记录用户接受/拒绝/修改结果 | feedback event、eval trace |

必须实现：

- TaskGraph 节点依赖和幂等键；
- 每个节点输入输出 schema；
- 单节点失败不隐藏，整体任务可部分成功；
- 预算、超时、最大循环次数和最大工具调用次数；
- “需要用户选择”状态，而不是模型自行猜测；
- 工具白名单，禁止模型构造任意 Python、Shell 或文件系统操作；
- 任何工程写入仍经 Preview → Approval → CommandStack。

### P4：素材理解与证据增强

| 能力 | 任务 | 验收 |
|---|---|---|
| ASR | 校验本地 ASR、云 ASR、时间戳、说话人和语言 | 字幕时间轴误差在目标阈值内 |
| 场景 | 场景切分、镜头质量、重复镜头和可用片段评分 | 可解释地选择镜头 |
| 语义 | transcript、OCR、标签、人物、情绪和主题索引 | planner 只引用已存在证据 |
| 音频 | 人声、噪声、响度、节拍、静音和音乐段落 | 混音前后有可验证指标 |
| 视觉 | 清晰度、曝光、黑帧、抖动、构图和人脸安全 | 低质量素材有诊断而非静默使用 |
| 缓存 | 分析 fingerprint、模型版本、素材版本和失效策略 | 素材改变后旧证据失效 |

### P5：商业化剪辑工作流

第一批建议做可复用模板，不让模型每次从零发明流程。

| 工作流 | 默认输入 | 默认输出 |
|---|---|---|
| 竖屏短视频 | 长视频/访谈、平台、时长、风格 | 9:16 粗剪、字幕、BGM、片尾 |
| 访谈精剪 | 多机位访谈、主题、目标时长 | 精华段落、J/L cut、标题和字幕 |
| 产品广告 | 产品素材、卖点、品牌规范 | 15/30 秒多版本广告 |
| 播客切片 | 音频/视频、主题和社媒平台 | 横版/竖版切片、波形字幕 |
| 教程课程 | 讲解视频、章节目标 | 去停顿、章节、重点字幕、目录 |
| 多语言版本 | 原片、目标语言、声音偏好 | 翻译字幕、配音和时间线版本 |

每个 workflow 必须具备：输入清单、默认 policy、TaskGraph、可调整参数、预览门禁、质量门禁、失败恢复和用户反馈入口。

### P6：安全执行与自动模式

自动模式分三级，不能只有一个“全自动”按钮：

| 模式 | 允许行为 | 必须确认 |
|---|---|---|
| 建议模式 | 读取、分析、生成计划、生成预览 | 所有工程写入和外部付费操作 |
| 半自动模式 | 自动执行本地分析和低风险编辑 | 时间线写入、云上传、付费生成、最终导出 |
| 受限全自动 | 在用户预设项目、素材、预算、模板内执行 | 超出范围、版权/安全风险、预算、导出发布 |

安全实现：

- project/timeline/base revision 乐观锁；
- 工具白名单和参数 schema；
- path sandbox、网络域名 allowlist 和文件类型校验；
- 资源数量、时长、分辨率、成本和循环次数限制；
- 一键停止全部 Agent Job；
- 一次 AI 编辑对应一个可撤销 MacroCommand；
- 任何自动发布动作必须独立 consent。

### P7：Job 持久化、恢复与可观测性

当前 JobRegistry 是内存状态，生产版必须引入持久化边界。

| 项目 | 任务 | 验收 |
|---|---|---|
| 状态存储 | 保存 run、job、node、artifact、checkpoint、审计摘要 | 重启后能列出未完成任务 |
| 恢复策略 | queued/running/cancelling 分别定义恢复或安全终止 | 不重复扣费、不重复写入 |
| 重试策略 | transient、provider、validation、permission 分级 | 非幂等动作默认不自动重试 |
| 进度事件 | 事件序列号、时间戳、job 状态、阶段消息 | UI 重连后可补齐状态 |
| 日志 | 结构化日志、错误码、trace/run/job 关联 | 单个失败可定位到 provider 和节点 |
| 指标 | 成功率、耗时、成本、重试率、用户修改率 | 可按 provider/model/workflow 聚合 |
| 清理 | 缓存、临时文件、孤儿 Job、过期审计 | 有上限、有保留策略、有恢复保护 |

### P8：导出交付与质量门禁

导出成功不等于成片质量合格，必须在导出前后分别检查。

| 门禁 | 检查内容 |
|---|---|
| 工程门禁 | 素材引用存在、时间线有效、无未解析 provider 句柄 |
| 视频门禁 | 分辨率、帧率、时长、黑帧、冻结帧、损坏帧 |
| 音频门禁 | 音频轨、采样率、响度、削波、静音和同步 |
| 字幕门禁 | 字幕范围、重叠、阅读速度、缺字和字体 |
| 品牌门禁 | Logo、颜色、片尾、免责声明和安全区 |
| 版权门禁 | 音乐/素材 license、来源和商业使用状态 |
| 平台门禁 | 横竖屏、码率、封装、文件大小和平台规格 |
| 结果门禁 | 文件存在、可重新打开、hash、manifest 和缩略图 |

### P9：安全、版权、隐私与成本

| 领域 | 必须完成 |
|---|---|
| 凭证 | 系统钥匙串/安全存储、轮换、删除、不可进入日志和工程文件 |
| 隐私 | 上传前明确素材范围、敏感素材标记、本地优先、保留期限 |
| 版权 | provider license、音乐授权、用户素材来源、商业就绪状态 |
| 内容安全 | provider 审核结果、本地拦截、人工复核和导出阻断 |
| 费用 | 单次、项目、用户、provider、模型预算与硬阻断 |
| 多用户 | 用户/团队/项目隔离，审计访问记录 |
| 删除 | 删除工程时同步清理缓存、远端任务、凭证引用和审计策略允许的数据 |
| 事故 | API 泄漏、错误发布、版权争议、provider 失效的应急预案 |

### P10：评测、性能和发布门禁

建立固定数据集，不用“看起来能跑”作为验收。

| 测试层 | 内容 | 发布门禁示例 |
|---|---|---|
| 单元测试 | schema、policy、router、provider、Job、cache、命令 | 关键模块覆盖率和边界用例达标 |
| 集成测试 | DeepSeek mock、provider mock、TaskGraph、工程写入 | 失败、重试、取消、恢复全部可重复 |
| 成片测试 | 标准素材集、不同语言/比例/时长/音频情况 | 输出可打开、无关键质量错误 |
| Agent 评测 | 意图分类、素材选择、计划正确性、工具调用安全 | 关键任务成功率达到产品阈值 |
| 回归测试 | 旧工程、旧插件、旧时间线、用户配置 | 不破坏既有编辑能力 |
| 性能测试 | 4K、长视频、多轨、并发任务、缓存命中 | UI 不阻塞，内存/显存/耗时在预算内 |
| 故障测试 | 断网、超时、限流、重启、磁盘满、provider 失败 | 不污染工程、不丢审计、不重复扣费 |
| 安全测试 | 越权工具调用、路径穿越、敏感日志、恶意 provider 返回 | 高风险用例全部阻断 |

发布前必须生成：版本号、变更记录、测试报告、已知问题、回滚方案、默认开关状态和支持的 provider 列表。

### P11：Beta 灰度与商业运营

| 阶段 | 做法 | 观察指标 |
|---|---|---|
| 内部 Alpha | 仅开发者和固定工程，真实 provider 小额调用 | 崩溃、任务失败、成本、重复扣费 |
| 小范围 Beta | 少量用户、默认建议模式、功能开关 | 首次成功率、人工修改率、满意度 |
| 扩大 Beta | 增加 workflow 和 provider，保持人工导出确认 | 质量、耗时、成本、版权风险 |
| 商业发布 | 分层套餐、用量限制、支持与回滚机制 | 留存、单位任务毛利、投诉和事故率 |

---

## 6. 每阶段统一执行规范

每个阶段都必须按以下顺序完成：

1. Explore：先分析影响范围、现有接口和线程边界；
2. Design：写出 provider/TaskGraph/状态机/失败路径；
3. Implement：遵循 `.clinerules` 和项目现有类型化边界；
4. Review：检查线程安全、资源释放、权限、幂等和错误处理；
5. Test：先目标测试，再受影响回归，再编译和静态检查；
6. Save：明确 `git add`、`git diff --cached --check`、`git commit`；
7. Update：更新本计划的状态、提交号、测试结果和已知风险。

禁止：

- 在 UI 线程调用真实 provider 或下载大文件；
- 用 `except: pass` 把 provider、权限或导出失败变成假成功；
- 把真实 API Key、绝对路径、完整敏感 prompt 写入日志或工程 JSON；
- 让模型直接生成 Python/Shell 代码修改工程；
- 自动模式绕过审批、预算、版权和 CommandStack；
- 在没有 mock/fixture 的情况下把真实付费 API 作为 CI 测试依赖。

---

## 7. 阶段保存记录表

| 阶段 | 状态 | 提交号 | 测试/说明 |
|---|---|---|---|
| 已完成基础闭环 | completed | `ae294f2c` | AI 剪辑自主运行骨架 |
| AI 导出工作流 | completed | `ae44af76` | 审批、预检、后台导出 |
| 生成素材箱 | completed | `7f9b9a18` | provider-neutral 生成候选 |
| 生成素材导入 | completed | `906c91e2` | 显式导入媒体池 |
| 生成预览元数据 | completed | `e8088175` | 结果卡和预览状态 |
| 时间线放置 | completed | `31e5f969` | 审批后 add_clip |
| 远程媒体异步物化 | completed | `39428b3c` | Job、进度、取消、主线程导入 |
| P0 基线冻结 | completed | `bf148876` | 能力矩阵、启动自检、安全 Feature Flag、demo provider 分级；1213 passed / 1 skipped |
| P1 DeepSeek 生产韧性 | completed | `a56657f1` | OpenAI-compatible DeepSeek、稳定错误码、有限重试、usage/成本 ledger、预算前置阻断、流式 UI、planner schema 版本校验、显式模型路由与隐私 guard；P1 回归 1217 passed / 1 skipped |
| P2 真实媒体 provider | in_progress（HTTP adapter/配置入口/共享 worker/checkpoint 完成，真实 provider 待凭据激活） | `fc0baf0f`, `901ccbb4`, `02b5e906`, `a64be037` | provider-neutral HTTP 提交/轮询/取消、幂等键、限时重试、内容安全/成本硬阻断、Preferences modality/route 配置、共享 JobWorkerPool、非 demo provider 异步 Harness 接线、可持久化 remote job checkpoint/resume 和 mock fixture；真实凭据连接测试待续 |
| P3 自主执行循环 | in_progress（有界循环、动作级策略、补偿回滚与显式 resume 完成） | `1f25b232`, `3ae87375`, `a64be037`, `2b070f50` | Observe → Plan → Gate → Act → Verify → Recover 状态机、工具/成本/循环上限、取消、审批、项目作用域、step 级资产/费用/网络/文件校验、宿主 rollback 边界和 context/plan hash resume 已接入；完整多节点 controller 与所有宿主动作接线待续 |
| P7 Job 持久化、恢复与可观测性 | in_progress（持久化、事件、指标、provider checkpoint、受控恢复与保留清理完成） | `1f25b232`, `3ae87375`, `901ccbb4`, `02b5e906`, `a64be037`, `2b070f50` | JobRegistry 原子快照、`.topos/ai/jobs.json`、重启安全终止活动任务、Agent/provider checkpoint、有序进度事件、成本/成功率/时长聚合、非 UI 线程生成、`JobRecoveryManager` 显式恢复、年龄/数量清理和事件裁剪已接入；宿主自动发现/重建 consented request 与运营值班流程待续 |
| P4 素材理解与证据增强 | in_progress（引用门禁与工程级证据缓存完成） | `b81c7b98`, `a64be037` | planner 强制目标 beat、已知 segment 和选中素材范围；分析 fingerprint/model/provenance/segments 随工程保存，素材指纹变化自动失效；更多分析 provider 与缓存淘汰策略待续 |
| P5 商业化剪辑工作流 | in_progress（七类模板合同完成） | `46a2626a`, `a64be037` | 内置口播、广告、访谈、播客、字幕、教程六类交付模板，新增多语言 typed artifact/provider seam；真实 provider 生成素材、翻译/TTS 和时间线版本待续 |
| P6 安全执行与自动模式 | in_progress（三档策略、配置入口、action-level 元数据和 rollback 边界完成） | `3ae87375`, `901ccbb4`, `a64be037`, `2b070f50` | 建议/半自动/受限全自动、项目/时间线/base revision/素材/模板/预算/网络白名单、工具 allowlist、独立发布审批、Preferences 开关、step 级费用/网络/扩展名/资产校验和宿主补偿回滚已接入；所有 controller/skill/composite 动作覆盖和最终导出回滚编排待续 |
| P8 导出交付与质量门禁 | in_progress（宿主完成回调、平台规格/字幕合同、最终渲染 bbox 安全区、品牌元素实测合同、静音策略、manifest/thumbnail、ffprobe/hash、帧级/峰值/LUFS 分析硬门完成） | `b81c7b98`, `3ae87375`, `a64be037`, `f9aae85b`, `932cd9f6`, `a9554967`, `42985aad`, `193cca77`, `15761ce7`, `2b735a94` | 既有音频/视频/字体预检 + 商用就绪 provenance 硬阻断；宿主编码线程已接入 manifest、首帧缩略图、ffprobe、hash、黑帧/冻结帧/损坏帧采样、峰值、最长静音、integrated LUFS/LRA 测量；真实超峰值输出会阻断，显式目标 LUFS 会按容差硬校验；新增 provider-neutral 平台交付合同（画幅/帧率/编码/封装/文件大小/码率/音频采样率）和字幕范围、重叠、阅读速度、空文本、长行、安全区硬门；宿主按最终解析样式测量 CJK/Latin 字幕的变换后 bbox，支持 title/action/platform/custom 安全区并在编码前阻断；品牌元素要求可声明 `logo`/`end_card`/`disclaimer` 的时序、最短展示帧数、bbox/safe-area 测量，缺失或不合格会阻断并写入 manifest；Qt/PIL bottom 对齐已统一；静音默认仅测量，显式 `hard_max` + 最大秒数才硬阻断并写入 manifest；从未知时间线素材自动识别品牌元素仍待接入 |
| P9 安全、版权、隐私与成本 | in_progress（核心红线、隔离/删除/事故策略完成） | `a56657f1`, `fc0baf0f`, `b81c7b98`, `a64be037` | 凭据引用、脱敏、上传确认、内容安全、成本、许可边界、用户/团队/项目精确隔离、远端 job/缓存/凭据/审计删除联动、API 泄漏/错误发布/版权争议/provider 事故登记已落地；真实密钥轮换与运营值班流程待续 |
| P10 评测、性能和发布门禁 | in_progress（9 素材真实探针、并发、故障、帧/音频、可选 LUFS、平台元数据、字幕/品牌元素测量合同、4K 全源长度多编码完成） | `46a2626a`, `3ae87375`, `901ccbb4`, `02b5e906`, `a64be037`, `f9aae85b`, `fdd0059a`, `3123b18e`, `932cd9f6`, `a9554967`, `42985aad`, `193cca77`, `15761ce7`, `2b735a94` | EvalTrace + quality gate + release decision；9/9 真实素材视频解码采样成功，黑帧/冻结帧/损坏帧均为 0；2K H.264 为 `-17.845 LUFS / 14.735 LU` 且峰值 `+2.903 dB` 被质量门阻断，4K ProRes 4444 为 `-27.527 LUFS / 1.707 LU`，其余 7 个样本为 `-70 LUFS` 静音 floor；2 worker 元数据探针 9/9 成功（总墙钟 0.503 秒，P95 150.862 ms），新增 9/9 真实素材封装/文件大小/视频码率字段可读；内置横版/竖版/方版项目交付 profile 与自定义 profile 均有边界测试，字幕合同覆盖范围、重叠、阅读速度、空文本、长行和最终渲染 bbox 安全区；品牌元素合同覆盖要求存在、kind、帧范围、最短展示时长和 bbox/safe-area 测量；真实 4K H.264 源 20 秒四编码单 worker `4/4` 成功，29.72 秒全源长度四编码单 worker `4/4` 成功（总墙钟 `94.993s`），H.264+HEVC 20 秒双 worker `2/2` 成功（总墙钟 `17.157s`）且输出均重新 ffprobe；8/8 确定性故障均阻断；完整 AI 回归 `1213 passed / 2 skipped`，品牌合同/交付质量门回归 `34 passed`，宿主桥接回归 `10 passed`；全 app 与 composite 的既有基线失败和 macOS VideoToolbox 环境级 segmentation fault 仍按前批报告记录；从未知时间线素材自动识别品牌元素、多源长片持续并发和完整商业端到端仍待续；详见 `docs/ai_editing_delivery_probe_matrix_2026-08-09.md` |
| P11 Beta 灰度与商业运营 | in_progress（release manifest 与 Alpha/Beta/Stable 指标门完成） | `46a2626a`, `a64be037` | Alpha/Beta/Stable manifest、默认开关、rollback 字段、成功率/崩溃/重复扣费/版权事故/满意度/成本指标与晋级门；真实灰度数据、套餐/支持/回滚演练待续 |

本批计划表增量（2026-08-10，`4cc613f6`；前置品牌物化提交 `aeb33786`，持久化提交 `65c6a4d6`）：P10 新增多源编码源级预检缓存、`source_count` 报告字段和任务级失败隔离；真实 `4K_H264.mov` + `4K_H265.mov` 两源、H.264/HEVC 四任务、双 worker 全源长度 `4/4` 通过，总墙钟 `59.136s`，全部 post-ffprobe。完整 AI 回归更新为 `1220 passed / 2 skipped`，编码 benchmark 专项 `6 passed`。仍待完成的是任意未标注素材的视觉 Logo/免责声明识别、更大规模多源长片持续并发和完整商业端到端验收。

---

## 8. 建议的下一步执行顺序

P0/P1 已完成；本批完成 P2 checkpoint/resume 接缝、P3/P6 宿主 action-level 策略边界、P4 工程级证据缓存、P5 教程/多语言模板合同、P7 provider checkpoint、P8 ffprobe 探针接缝、P9 运维红线、P10 四类风险矩阵和 P11 灰度指标门，剩余工作集中在真实环境激活与生产运营闭环：

详细的 Agent 收敛路线、Workflow 面板审计、批次依赖、逐项验收标准和最终发布门见：`docs/ai_editing_autonomous_agent_completion_plan_2026-08-10.md`。其中首要动作是将 Workflow 面板从 `FakeWorkflowService` 运行路径切换到真实 `AssistantService`/Skill backend，并绑定当前 project/timeline/brief/revision/assets。

1. 将本批已接入 AgentLoop 的 action-level policy、补偿回滚和受控 resume 继续覆盖到所有 AI controller/skill/composite 动作，并接通宏级回滚与清理保留策略；
2. 在用户提供实际授权后完成至少一个真实 provider 的连接测试，并验证真实 provider 的内容安全、license 和区域条款；
3. 在已完成的最终字幕 bbox/品牌元素测量合同、平台规格/字幕合同、静音策略和 29.72 秒全源四编码基础上，接入实际时间线品牌素材的自动物化/识别与 bbox 生成，继续完成多源长片持续并发和完整商业端到端验收；9 素材探针矩阵与终态验收记录在 `docs/ai_editing_delivery_probe_matrix_2026-08-09.md`，首个真实素材与 DeepSeek 规划闭环记录在 `docs/ai_editing_real_material_test_2026-08-09.md`，可靠性验收记录在 `docs/ai_editing_autonomy_reliability_batch_2026-08-09.md`；
4. 接入真实 Alpha/Beta 运营数据，完成密钥轮换、值班事故响应、套餐/支持和回滚演练。

P2 的 provider 选择必须基于用户实际拥有的 API、授权条款、区域可用性和预算，不在没有确认前硬编码某个厂商 SDK。

---

## 9. 商业化完成定义（Definition of Done）

只有同时满足以下条件，才可以称为“商业化、全自动生产级 Agent”：

- DeepSeek 文本调用有稳定错误处理、结构化输出、预算和审计；
- 至少一个真实媒体生成 provider 通过统一 adapter 接入并经过 mock/真实验收；
- Agent 能完成至少三类标准剪辑 workflow，并在失败时恢复或安全等待；
- 所有工程写入、云上传、付费生成和最终导出都受 policy/consent 控制；
- 应用重启、断网、provider 超时、磁盘满等故障不会污染工程或重复扣费；
- 预览和最终导出都有视频、音频、字幕、版权和平台质量门禁；
- 关键任务有素材级评测集、自动化回归和可量化通过阈值；
- 有日志、指标、错误码、反馈、灰度开关和回滚路径；
- 用户能看到 Agent 做了什么、为什么做、花了什么、用了什么 provider，以及如何撤销；
- 发布构建、依赖、凭证、隐私政策和支持流程可以被维护团队接手。

在达到上述定义前，产品应明确标记为 Beta/Preview，而不是承诺完全自动生产。
