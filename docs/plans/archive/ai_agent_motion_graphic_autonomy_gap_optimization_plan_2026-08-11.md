# 自主 AI Agent 剪辑助手与 Motion Graphic Clip 优化计划

日期：2026-08-11  
状态：审计规划，尚未实施  
范围：AI Assistant、Edit 时间线、Teaching Animation（Motion Graphic Clip）、Agent Runtime、生成/预览/导出与质量验收链路。

## 1. 目标体验与结论

目标体验是：用户输入“帮我创建一个 15 秒的 Motion Graphic Clip：AI Agent 介绍动画”后，助手先基于工程和素材生成**可阅读、可播放、可修改**的创意方案；用户可多轮改稿；只有点击一个与该方案版本绑定的“确认执行”按钮后，系统才开始生成、插入时间线并验证。生成完成后，用户仍可通过自然语言定向修改场景、镜头、文案、节奏、配色、音频或时长，并在每次写入前看到新的差异和预览。

当前工程已经具备重要基础：

- `src/features/teaching_animation/module.py` 已将首轮 Motion Graphic 请求引入规划对话，明确授权后才显示确认卡，并在确认后创建场景、插入时间线、打开编辑器和验证首帧。
- `src/features/teaching_animation/ai/commands.py` 已提供受限命令词表、dry-run、场景 diff、确认后 apply，以及基于场景 hash 的 stale 防护。
- `src/features/edit/ai_editing/` 和 `src/features/ai_harness/` 已有计划、审批、运行、恢复、审计、工作流和受限自主执行的基础设施；时间线写入也有 CommandStack/Approval 边界。
- `src/features/teaching_animation/export_service.py` 已支持渲染和若干视频/序列格式导出，并有素材预检。

但目前这个路径仍是“**单一 LLM 场景草稿 + 文字确认卡 + 创建/插入**”，还不是完整的自主剪辑 Agent 产品闭环。最关键的产品缺口是：确认前没有真正的可播放预览和可理解的场景/时间线 diff；改稿没有版本化的创意方案循环；生成失败会自动改用内置样板；生成后编辑能力主要局限在元素级文本、颜色、字号等；与通用 Agent Runtime、质量门、媒体生成、持久恢复和交付验收尚未形成一条统一可审计链路。

## 2. 审计依据与边界

本表依据 2026-08-11 对下列实现和现有测试的只读审计生成：

| 领域 | 证据位置 | 已确认事实 |
|---|---|---|
| Motion Graphic 入口 | `src/features/teaching_animation/module.py` | 首轮请求进入讨论；明确授权后通过 LLM 生成 scene commands，再给出确认卡；确认后才插入时间线。 |
| 场景命令与防护 | `src/features/teaching_animation/ai/commands.py` | 有 `dry_run`、`diff_hash`、`confirmed`、`base_hash` 校验与 undo。 |
| 卡片 UI | `src/features/edit/ui/ai_edit_assistant_panel.py` | `set_motion_graphic_proposal()` 当前只接收标题和纯文本详情，提供“取消 / 确认执行”两个按钮。 |
| LLM 场景契约 | `src/features/teaching_animation/module.py` | 固定为 1920×1080，允许 `add_element` / `set_animation`；失败后转硬编码演示场景。 |
| 导出 | `src/features/teaching_animation/export_service.py` | 有 asset preflight、同步 render、FFmpeg 输出和基础文件存在性检查。 |
| 通用 Agent 运行时 | `src/features/edit/ai_editing/agent_loop.py`、`src/features/edit/ai_editing/autonomous_agent.py`、`src/features/ai_harness/` | 有有界 Agent 循环、策略/审批、运行记录、取消/恢复等基础设施。 |
| 自动化测试 | `tests/ai/test_teaching_animation_phase5.py`、`tests/ui/test_p1_chat_free_conversation.py`、`tests/ui/test_ai_edit_assistant_dock.py` | 覆盖讨论→确认、基础场景命令、插入、部分 UI 信号；尚不是真实 provider、预览播放、反复改稿和最终交付的端到端验收。 |

“不足”只指向上述目标体验及其生产化要求；不应把现有通用剪辑、调色或播放模块的所有未来需求混入本计划。

## 3. 当前流程与目标流程的差距

```text
当前
用户请求 → 规划对话 → 用户说“可以生成” → LLM 场景 JSON
  → 文字确认卡 → 确认 → 创建 scene / 插入 timeline / 首帧验证

目标
用户请求 → 收集工程上下文/素材/品牌/交付约束
  → 多方案 Brief + Storyboard + 可播放低清预览 + 风险/成本/差异
  → 多轮修改（保留方案版本与反馈）
  → 版本绑定的确认 → 有界 Job DAG 执行 → 中间产物与进度
  → 时间线写入 + 全片质量门 + 可交付预览
  → 生成后自然语言精修/回滚/导出版本
```

## 4. 缺口总表

优先级：P0 为不能满足核心承诺或可能误导用户的阻断项；P1 为生产可用性与体验闭环；P2 为质量、规模与商业交付成熟度；P3 为后续增强。工作量为单人有效开发日的相对估计，实施前应按真实 provider、素材和平台范围复核。

| ID | 优先级 | 缺口 / 当前不足 | 优化内容与主要改造位置 | 验收标准 | 估计 |
|---|---:|---|---|---|---:|
| MG-01 | P0 | 请求没有结构化 Creative Brief；讨论内容没有明确沉淀为可编辑目标。 | 新建 `MotionGraphicBrief`（主题、受众、时长、画幅、平台、语言、品牌、风格、CTA、素材、音频、预算、禁用项、revision）；在 `module.py`、Assistant session 和项目 payload 间传递。 | 同一句请求能形成可见 brief；每次用户改稿使 brief revision 增加；缺少关键选择时只提问，不擅自猜测。 | 2–3 天 |
| MG-02 | P0 | 方案卡只有文字摘要，用户无法判断“满意”。 | 将 `set_motion_graphic_proposal()` 替换/扩展为类型化 `MotionGraphicProposalCard`：显示方案摘要、场景元素、节奏段落、时长、画幅、插入位置、风险、成本、来源与状态。修改 `ai_edit_assistant_panel.py`、新增专用 card/view-model。 | 用户不进入开发日志即可知道将生成什么、影响何处、是否为真实模型结果；文本与结构化计划 hash 对应。 | 2–3 天 |
| MG-03 | P0 | 确认前没有可播放预览、缩略图、首中尾帧或时间线差异。当前只在内部做 64×36 首帧可渲染检查。 | 在 `teaching_animation/render/` 增加 preview job：渲染 storyboard 缩略图、首/中/尾帧和低清 MP4/WebM；用现有 JobRegistry/Run Drawer 反馈进度与取消；在卡片展示播放器与 `SceneDiff`、timeline diff。 | 对 15 秒草稿可在确认前播放预览；预览失败清楚显示原因且不能执行；预览与提交场景的 `scene_hash` 相同。 | 4–6 天 |
| MG-04 | P0 | 当前一次 LLM 直接产出最终场景，不是“先给方案，满意再执行”。 | 拆成 `brief → creative plan → scene plan → preview` 三阶段：先让模型输出 1–3 个低成本的创意方案（文案、节奏、镜头/版式、风格），用户选定或要求修改后再编译场景命令。 | 首轮不会直接创建最终 scene；可选择方案 A/B/C 或继续改；用户选择被记录为方案版本的输入。 | 4–5 天 |
| MG-05 | P0 | 生成失败会自动回退到硬编码“AI Agent 介绍动画”样板，可能与用户以为已批准的自定义方案不一致。 | 将 fallback 改为显式状态：`provider_unavailable` / `invalid_scene_plan` / `optional_template_fallback`。只有用户明确点击“改用内置模板”才可创建模板；卡片标出生成来源。 | LLM 失败绝不自动以样板冒充定制结果；用户可重试、修改 brief、选模板或取消。 | 1–2 天 |
| MG-06 | P0 | 计划批准没有统一绑定 brief revision、scene hash、preview hash、timeline revision、插入范围和权限。 | 复用/适配 `ApprovalService` 与 `WorkflowActionService`，新建 `MotionGraphicApprovalScope`；确认 token 一次性绑定上述字段，执行前重新校验。 | 改了 brief、场景、时间线或插入点后旧确认必定 stale；重复点击不得插入两次。 | 3–4 天 |
| MG-07 | P0 | Motion Graphic 专用路径没有完全复用通用 Agent 的 run/job/artifact/checkpoint 协议，进度和诊断难统一。 | 为 Motion Graphic 实现受控 Skill/Workflow adapter，接入 `HarnessRuntime`、`AgentLoop`、`ArtifactStore`；阶段至少为 Observe、Plan、Preview、Wait approval、Apply、Verify、Export。 | Assistant、快捷任务和 Workflow 面板对同一任务显示同一 run_id、状态、取消、诊断与产物引用。 | 5–7 天 |
| MG-08 | P0 | 用户“继续修改直到满意”的回路是状态标志与自然语言讨论，不是版本化的可回滚方案迭代。 | 新建 `MotionGraphicRevision`、`FeedbackPatch`、`ProposalVersion`；支持“只改文案/配色/节奏/时长/布局/音频”等约束补丁，保留父版本、diff、预览和用户接受/拒绝原因。 | 连续三轮改稿后，用户可以查看、比较、恢复任一草案；只有选中的版本可确认。 | 4–6 天 |
| MG-09 | P0 | 生成后场景修改只覆盖少量关键词规则和 `update_element`，复杂修改依赖有限 JSON 命令。 | 扩充稳定命令 schema：新增/删除/替换元素、分组、布局约束、过渡、缓动、关键帧编辑、镜头段落、颜色 token、字体 token、音轨引用；所有命令须 dry-run、schema 校验、范围/复杂度限制。 | “把开场缩短到 2 秒、标题改为 X、卡片依次弹出、换成品牌蓝”能生成可审阅 diff；危险操作仍需确认。 | 6–9 天 |
| MG-10 | P0 | LLM 输出只做基础命令解析，缺少设计级、渲染级和可访问性级验证。 | 扩展 `core/validator.py`：画布越界/重叠、最小字号、对比度、安全边距、动画关键帧范围、元素/动画上限、重复 ID、时长、文本溢出、不可用资产、可读性与闪烁风险。 | 不合法方案在预览前被解释性阻断；每个 finding 定位元素与修复建议。 | 3–5 天 |
| MG-11 | P1 | 规格被硬编码为 1920×1080，无法可靠产出 9:16、1:1、不同 FPS 或平台安全区版本。 | 将画幅、FPS、像素尺寸、safe area、字幕区、交付 profile 移入 brief/template；建立 responsive scene layout 与 profile migration，避免硬编码在 system prompt。 | 同一 brief 可产出 16:9、9:16、1:1 版本；文本和 Logo 均不越过对应安全区。 | 4–6 天 |
| MG-12 | P1 | 创意规划不读取当前工程素材、时间线、品牌包、已有 clip 或选区，容易生成与工程脱节的内容。 | 在 Observe 阶段统一注入 project/timeline revision、选中素材、分析证据、场景库存、品牌资产、已有字幕/音轨和 delivery profile；无证据时明确说明。 | 选中素材/品牌规范能影响方案；用户切工程或改时间线后旧计划失效。 | 4–6 天 |
| MG-13 | P1 | 没有可复用的 Motion Graphic 模板、风格 token 和品牌系统，LLM 每次从零设计且可控性弱。 | 新建模板/主题 registry：产品发布、数据解释、流程图、标题包装、教程、社媒卡片；定义色板、字体、圆角、动画曲线和 Logo policy。 | 用户可从“按品牌模板 / 从零生成”选择；模板版本、许可证和兼容性可审计。 | 4–6 天 |
| MG-14 | P1 | 视觉资产、字体、图标、图片/视频、音乐和 TTS 缺少统一生成/导入/许可/provenance 流程。 | 接到既有 `GenerationBin`、provider registry、AssetResolver 与 credential/consent 边界；所有外部素材记录 provider、模型、版本、license、使用范围、cost 和 hash。 | 任一外部资源均可查看来源与许可；未授权上传/未知版权资产不得进入商业导出。 | 5–8 天 |
| MG-15 | P1 | 15 秒片段往往需要 BGM、配音、SFX 和音画同步；当前 scene 主要是视觉元素。 | 在 scene schema 或关联 timeline plan 中加入 narration、music、SFX、ducking、beat markers、音频时长与字幕映射；使用后台 job 分析/生成。 | 可为方案明确展示音频来源、时长和混音策略；音频缺失或越界不会静默导出。 | 6–9 天 |
| MG-16 | P1 | 确认执行后只有创建/插入和首帧检查，缺少阶段进度、取消边界、中间产物、幂等和失败恢复体验。 | 使用 Job DAG 分离生成、预览、资产物化、插入、验证、导出；每步写 checkpoint、idempotency key、成本和诊断；面板显示 cancel/retry/resume/handoff。 | 取消不破坏工程；重试不重复插入或扣费；失败后保留可定位诊断和可恢复下一步。 | 5–7 天 |
| MG-17 | P1 | 教学动画导出为同步 render，长片/高分辨率会阻塞调用方；FFmpeg 命令含 `-y`，也没有原子输出、可恢复导出与全面 postflight。 | 将导出放到后台 Export Job；输出写临时文件后原子替换；显式 overwrite approval；增加取消、空间预检、ffprobe、时长/帧数/codec/hash、音频/黑帧检查和 manifest。 | 4K/长片导出不阻塞 UI；取消不留下被误认为完成的最终文件；成功必须由 postflight 证明。 | 4–6 天 |
| MG-18 | P1 | 缺少 Motion Graphic 专属质量门：仅有首帧可渲染，不代表 15 秒动画可用。 | 新建 `MotionGraphicQualityGate`：首/中/尾/关键帧渲染、透明通道、文本溢出、安全区、闪烁/跳帧、镜头空白、资产可解析、音画时长、最终编码检查。 | 质量报告逐项显示；任何阻断失败不能标记为“可交付”。 | 4–6 天 |
| MG-19 | P1 | 没有生成后“定位到时间线/场景/元素/产物”的双向联动和版本管理。 | 在 artifact、scene、clip、timeline command、export 之间建立 typed references；卡片可打开 scene editor、定位 clip、打开预览/质量报告、创建新版本或回滚。 | 用户可从任一结果卡定位编辑对象；人工修改后系统显示 stale 而非覆盖。 | 3–5 天 |
| MG-20 | P1 | 会话与 pending request 为内存字典，未形成可恢复的 Motion Graphic 草稿/审批/版本生命周期。 | 将未确认草稿、revision、preview artifact、run refs 和拒绝原因写入安全的 `.topos` payload/assistant session store；不保存 secret、临时 URL、绝对路径或原始敏感 prompt。 | 关闭重开工程后可继续查看草稿；旧 grant 永远失效，需依据当前 revision 重新确认。 | 3–5 天 |
| MG-21 | P1 | 目前 LLM build 使用单个 controller 级 request id；多任务并行、排队、公平、预算与模型/provider 限流缺少明确合同。 | 统一调度 lane（LLM、CPU render、GPU render、网络 provider、编码），配置并发/队列/超时/预算/取消传播，优先级由用户可见。 | 两个生成任务并发时状态不串台；资源超限明确排队或阻断；不会阻塞 UI/GL 线程。 | 4–6 天 |
| MG-22 | P1 | 自动模式边界未针对 Motion Graphic 明确：哪些操作可自动、哪些仍必须单独同意。 | 新增 action matrix：只读、方案、低清预览、付费/云生成、下载字体/素材、创建 scene、写时间线、替换 clip、导出、上传/发布；将权限显示在确认卡和 Preferences。 | 默认仅方案；任何云上传、付费生成、替换现有 clip、最终导出/发布遵循单独策略且 fail closed。 | 2–3 天 |
| MG-23 | P1 | 方案与 LLM 错误的可解释性不足：用户不知道用了真实模型还是模板、哪些输入被使用、为何失败/被阻断。 | 建立脱敏 audit/event schema：provider/model、brief/plan/preview hash、证据 refs、工具、成本、耗时、policy decision、错误 code、人工操作；在 UI 提供摘要和诊断展开。 | 任何失败都有稳定 error code 和建议下一步；日志不含 API key、路径、完整素材或原始 prompt。 | 3–4 天 |
| MG-24 | P2 | 缺少针对“创意满意度”的评测集，现有测试主要验证功能和安全边界。 | 建立离线 fixture + 人审 rubric：时长准确、关键信息覆盖、可读性、品牌一致性、节奏、视觉重叠、字幕/音频、用户修改遵循度；保存评测 trace。 | 至少覆盖 AI Agent 介绍、产品广告、教程解释、竖屏社媒四类任务；回归能检测质量退化。 | 5–8 天 |
| MG-25 | P2 | 当前测试没有真实 provider、完整预览播放、连续改稿、重启/取消、最终导出质量门的端到端证据。 | 补分层测试：command/validator 单测、service/agent 集成、Qt 卡片交互、fake-provider 故障注入、真实授权环境 smoke、项目保存重开、预览到导出 E2E。 | 每个 P0/P1 项有 focused test；真实 provider 结果和 mock 结果严格区分；CI 不依赖付费 API。 | 5–8 天 |
| MG-26 | P2 | 无性能基线：15 秒动画的规划、低清预览、1080p/4K 渲染、内存与取消延迟没有 SLO。 | 新增 benchmark 脚本和 metrics：P50/P95 首次方案、预览首帧、预览完成、导出、取消收敛、峰值内存/磁盘/CPU/GPU；按素材与分辨率记录。 | 发布前 SLO 明确；超预算降级（预览分辨率/队列）可解释，不能牺牲最终语义。 | 3–5 天 |
| MG-27 | P2 | 没有真实用户反馈、灰度、事故/回滚机制来验证“完全自主”的生产承诺。 | 添加 feature flags、canary、kill switch、反馈（接受/拒绝/修改原因/满意度）、成本与版权事故指标、回滚 runbook。 | Alpha/Beta 指标阈值、回滚条件和数据保留策略书面化并实际演练。 | 3–5 天 |
| MG-28 | P3 | 缺少高级创作能力：多场景叙事、镜头模板、图表/代码高亮、角色/口型、协作审阅。 | 在上述安全/质量闭环后，再逐项扩展 scene graph、段落编排、协作评论/审批、跨语言版本、可访问性输出和更丰富渲染后端。 | 每项作为独立 capability，有 schema、preview/export 兼容声明、质量门和回退策略。 | 按项评估 |

### 4.1 面向“完整自主剪辑助手”的通用并行缺口

上表聚焦 Motion Graphic Clip。由于用户目标是一个可自主运行的**剪辑助手**，以下通用能力必须与 MG 垂直切片一起收敛；否则系统只能可靠地创建教学动画，无法承担完整剪辑任务。

| ID | 优先级 | 缺口 / 当前不足 | 优化内容与主要改造位置 | 验收标准 | 估计 |
|---|---:|---|---|---|---:|
| AG-01 | P0 | Agent 能声明的剪辑意图与真正可编译、可执行的动作并非完全等价；不能把“计划已生成”当成“剪辑/导出已完成”。 | 审计并收敛 `EditPlan`、`plan_compiler.py`、`ToolFacade`、Skill/Workflow node specs 与宿主 command mapping；为每个动作声明输入 schema、preview、approval、undo/compensation、export compatibility 和 error code。 | 不支持的动作在规划阶段清楚拒绝；所有可展示的“执行”动作都有真实 command/Job handler 和 focused test。 | 5–8 天 |
| AG-02 | P0 | 通用 Agent 的素材理解、规划、时间线编辑、字幕、音频、调色、生成媒体和交付仍是多条能力链，未保证每次都基于同一项目/时间线/素材/revision 上下文。 | 将 Chat、快捷 Skill、Workflow Canvas 的入口统一到 `RunContext`；其中包含 project、timeline、selected assets、brief、analysis fingerprint、profile、permission、budget、delivery profile。 | 任一入口对相同输入得到同一 scope/plan 语义；缺工程、过期 revision 或越权素材一律 fail closed。 | 5–7 天 |
| AG-03 | P0 | “完全自主”需要的真实模型/媒体 provider 还不能仅凭 deterministic/mock 成功来证明。 | 对 LLM、ASR/OCR、TTS、图像/视频/音乐 provider 分别实现真实授权环境 smoke、超时/重试/取消、幂等 request、成本、license、内容安全、区域与上传 consent；保留 fake 仅供 CI。 | UI 真实显示 provider/模型与 execution mode；无凭据/无授权不静默降级为假成功；重试不重复扣费。 | 8–15 天 |
| AG-04 | P1 | 自动剪辑需要由证据驱动，而不是只依据 prompt 或静态模板。 | 完成素材级 ASR、场景切分、镜头质量、OCR、语义索引、人声/响度/节拍、品牌元素与缓存失效；Evidence 记录来源、模型、素材 fingerprint 与置信度。 | Agent 的每个剪辑决策能链接到已有证据；素材变更使旧证据和计划 stale；低置信度进入人工确认。 | 8–12 天 |
| AG-05 | P1 | 生成媒体、代理、时间线写入和最终导出需要统一的 Job DAG、资源配额与恢复策略；不能让任一 controller 自己管理线程。 | 用共享 scheduler/JobRegistry 设置网络、CPU、GPU、编码 lane 与并发/磁盘/成本预算；为每个节点定义 checkpoint、retryability、idempotency、cancel safety 和 handoff。 | 并发任务不串状态；取消或崩溃不留半套时间线/未完成最终文件；恢复只在 current context 下显式进行。 | 6–10 天 |
| AG-06 | P1 | 成片“能导出”不等于“可交付”；不同入口的质量语义必须一致。 | 统一主剪辑、Motion Graphic、生成媒体的 Delivery Contract 和 Release Gate：ffprobe、hash、thumbnail、帧/黑帧、音频、字幕、品牌、安全区、平台 profile、manifest 与 provenance。 | 任何质量门失败均有报告且阻断“可交付”；预览和最终导出使用一致的时间线/效果语义。 | 6–9 天 |
| AG-07 | P1 | 自主模式、云上传、生成付费、破坏性时间线操作和最终发布需要操作级授权，而不是单一“全自动”开关。 | 扩展 `ExecutionPolicy`/Preferences：只读、提议、逐次确认、受限自动四档；按 action kind 设置审批、预算、网络、资产范围、kill switch 与审计保留期。 | 未分类动作默认拒绝；用户能看见并随时暂停 Agent；高风险动作永远单独确认。 | 3–5 天 |
| AG-08 | P2 | 尚缺能够证明产品真的稳定、好用、可运营的真实任务矩阵与灰度数据。 | 建立产品广告、访谈精剪、播客切片、教程、Motion Graphic、跨语言版本等 E2E 基准；收集成功率、质量门失败、重试/恢复、人工接管、成本、延迟、满意度和事故，配合 feature flag/canary/rollback runbook。 | 至少三类真实项目、两种平台、两种 codec、成功/失败/恢复路径均有脱敏证据；发布清单可据此 fail closed。 | 8–12 天 |

### 4.2 Codex 式 Agent 操作系统能力表

这里的目标不是让模型拥有不受限的系统权限，而是达到类似 Codex 的工作模式：维持一个明确目标，先理解环境，再以受控工具做动作；每步观察结果并调整；把计划、执行、验证、失败和人工决策都记录下来。对于剪辑软件，**宿主 API、工程模型、渲染线程和审批机制**相当于 Codex 的文件、终端、浏览器与权限沙箱。

| ID | 优先级 | Codex 式能力差距 | 详细实施内容 | 关键模块 / 新模块 | 可验收行为 |
|---|---:|---|---|---|---|
| OS-01 | P0 | 没有统一的长期 Goal / Mission 生命周期。 | 定义 `AgentMission`：目标文本、成功标准、允许范围、预算、deadline、状态、parent/child run、最新 evidence、用户决策和结束原因；使一个“做 15 秒广告”可跨多次聊天、重启和多个任务继续。 | 新建 `ai_editing/missions.py`、适配 `assistant_session_store.py`、`agent_loop.py`。 | 切换面板或重启后任务仍能说明“已完成什么、下一步是什么、为何等待用户”；终态 mission 不再接受旧事件回写。 |
| OS-02 | P0 | 计划仍偏一次性产物，缺少显式 Replan / Critique 循环。 | 每次 Observe、工具返回、预览质量门、用户反馈或 revision 冲突后生成 `ReplanDecision`：继续、局部重试、改用备选工具、请求澄清、回滚或结束；限制最大循环、工具调用和总成本。 | `agent_loop.py`、`workflow.py`、新建 `replan_policy.py`。 | 预览文字溢出时 Agent 不会误报完成，而是提出可见修订并产生新计划版本；超过限制时安全停止。 |
| OS-03 | P0 | 工具能力是分散实现，不是模型可检索的严格 Tool/Capability Catalog。 | 每个工具声明：名称、领域、输入/输出 JSON schema、只读/写入、所需 evidence、权限、预算、幂等性、取消语义、质量门、补偿操作、可见 UI 文案。禁止模型自由拼 Python/Shell。 | `tool_facade.py`、`skill_registry.py`、`execution_policy.py`；新建 `capability_catalog.py`。 | 模型只能选择 catalog 中工具；缺权限、缺前置证据或不支持的动作在调用前返回结构化诊断。 |
| OS-04 | P0 | Agent 缺少针对编辑工程的“环境地图”和变更感知。 | 构建只读 `ProjectStateSnapshot`：工程/时间线/轨道/clip/effect/marker/选区/媒体/调色节点/合成图/交付 profile 的稳定摘要及 fingerprints；状态事件使相关 mission 标 stale。 | `context_service.py`、host bridge、timeline/color/composite facade。 | Agent 能回答“我正在改哪条时间线、哪些 clip 会受影响”；人工修改任一依赖对象后旧计划不可 apply。 |
| OS-05 | P0 | 缺少面向视频的“观察结果”存储，模型输出与事实证据容易混淆。 | 将 ASR、镜头、OCR、人脸/主体、音频、色彩、质量、品牌、合成检测都存为 immutable evidence artifact，带 source fingerprint、模型版本、时间范围、confidence、过期策略。 | `analysis_service.py`、`semantic_index.py`、`artifact_store.py`；新建 domain evidence schema。 | 每个剪辑/调色/合成建议可跳转到素材时间码或质量测量；无 evidence 时只可提出分析计划。 |
| OS-06 | P1 | Agent 的执行过程缺少“可理解的工作台”，用户必须相信黑盒。 | 在 Assistant/Workflow 面板显示 Mission、Plan、当前 Step、工具、scope、预计成本、审批、artifact、质量门、诊断、取消/暂停/接管/回滚；区分拟议、运行、等待审批、已写入、已交付。 | `ai_edit_assistant_panel.py`、`ai_assistant_composer.py`、`workflow_panel.py`、Run Drawer。 | 用户无需看日志即可判断 Agent 是否真的执行、到底改了什么、是否可安全继续。 |
| OS-07 | P1 | 无结构化的用户反馈记忆，模型难从“我不满意”定位到应改什么。 | 把用户反馈归类为目标、节奏、镜头、文案、颜色、音频、品牌、平台、成本、质量、风险；生成 `FeedbackPatch`，关联 proposal/plan/artifact；只在同项目/允许范围内检索。 | 新建 `feedback_store.py`、更新 session/missions。 | 用户说“第二版节奏太慢、字幕太大”会产生有出处的局部修改，而不是丢失上下文重新开始。 |
| OS-08 | P1 | 子任务并行只有基础并发，缺少 DAG、优先级和写入串行化的全面策略。 | 将分析、代理、生成、渲染、导出定义为可并行 read jobs；把时间线/调色/合成写入限定为单 writer transaction；明确 join、失败隔离、fan-out、资源 lane 和公平队列。 | `JobRegistry`、scheduler、`SingleWriterPolicy`、workflow runtime。 | 可同时分析多段素材并生成多个 preview，但同一 timeline/grade/comp 永不发生竞态写入。 |
| OS-09 | P1 | 长任务的 resume 只解决状态存储，尚需真实世界副作用的可证明恢复。 | 每个外部 job 存 request fingerprint、provider job ref、artifact hashes、已完成步骤与确认边界；重启后先 reconcile，不盲目重发/扣费/插入。 | provider adapters、checkpoint store、`audit_store.py`。 | 应用重启后：本地已完成步骤不会重做；远程任务可查询；不能证明状态时请求用户选择而非猜测。 |
| OS-10 | P1 | 缺少“执行前模拟—执行后验证”的统一合约。 | 所有写动作必须提供 dry-run / diff / estimated cost 与 postcondition；将验证失败和补偿策略写入 action definition。 | `transaction_service.py`、`plan_validator.py`、quality gates。 | 任何可见的 Apply 按钮都对应 preview；提交后若 postcondition 失败，进入安全 rollback 或明确人工接管。 |
| OS-11 | P2 | 没有可复用、可版本化的专业技能包。 | 将“访谈精剪”“电商广告”“自动匹配镜头”“肤色保护调色”“去除绿幕溢色”“产品抠像合成”等实现为具备 form、输入 contract、工具链、质量门、eval 的 `SkillDefinition`。 | `skill_registry.py`、`ai_workflow/`、docs/skills。 | 用户能以自然语言或表单启动同一 skill；升级 skill 不破坏旧工程与旧 run 的可复现性。 |
| OS-12 | P2 | 缺少持续评测与基于证据的“自我改进”机制。 | 建立 golden projects、任务 rubric、自动评估、人工打分、回归阈值、失败聚类；仅用脱敏 trace 做指标，不把用户素材/secret 当训练数据。 | `release_test_matrix.py`、`quality_gate.py`、新建 `agent_eval/`。 | 模型/提示词/tool 变更必须给出成功率、质量、时延、成本和安全回归对比，未过线不得发布。 |

### 4.3 视频剪辑 Agent 能力计划

视频剪辑 Agent 的正确抽象不是“模型直接改时间线”，而是：素材理解产生 evidence → 编辑器生成可解释 `EditorialBrief` / `EditPlan` → host 模拟差异 → 用户/策略批准 → CommandStack 原子提交 → 预览与交付质量门验证。下面每一项都应保留这一链路。

| ID | 优先级 | 剪辑能力缺口 | 详细优化内容 | 建议涉及位置 | 验收标准 |
|---|---:|---|---|---|---|
| VE-01 | P0 | 缺完整的素材盘点和可用性评分。 | 对每个媒体生成 metadata、代理状态、时长、frame rate、色彩空间、音轨、缺帧、稳定性、清晰度、曝光、脸/主体、内容标签、版权/许可和建议可用区间。 | `shared/media/`、`analysis_models.py`、`analysis_service.py`。 | 导入任意素材后 Agent 可解释哪些段落可用、哪些不建议用以及依据。 |
| VE-02 | P0 | 缺少可编辑、可溯源的 transcript/字幕/说话人时间轴。 | ASR 输出应有词级时间码、语言、说话人、置信度、编辑 revision；支持 silence/filler、关键词、章节和引用回跳。 | `whisperx_provider.py`、subtitle models、timeline adapter。 | 用户说“保留介绍产品优势的段落”时，候选 cut 与具体 transcript timecode 对应。 |
| VE-03 | P0 | 缺少叙事级编辑规划，而不仅是按时长裁切。 | `EditorialBrief` 扩展 hook、核心论点、目标受众、平台、节奏、禁用内容、素材优先级、品牌 CTA；规划器输出 beat sheet、候选片段与删减理由。 | `editorial_models.py`、`variant_planner.py`、new `editorial_planner.py`。 | 对访谈/教程/广告，方案可展示叙事段落和每段使用的素材证据。 |
| VE-04 | P0 | 基础时间线操作的 Agent 语义、预览和撤销需完整覆盖。 | 收敛 insert/overwrite/lift/extract/ripple/roll/slip/slide/trim/split/compound/track layout/marker/clip attribute 为 typed action；每项有模拟、冲突规则、undo label、scope 与选区语义。 | `EditPlan`、`plan_compiler.py`、edit core command facade。 | 每项动作经 preview 后在 CommandStack 形成一个可撤销 transaction；无效操作不会产生半套序列。 |
| VE-05 | P1 | 缺少多机位与同步剪辑。 | 新增时间码/波形/人脸/拍板同步 evidence、angle groups、active angle plan、J/L cut、cutaway 与错位诊断。 | media analysis、timeline model、audio analysis skill。 | 多机位访谈可提出并预览切换点；同步不确定时请求用户确认。 |
| VE-06 | P1 | 缺少节奏、音乐和音频驱动编辑。 | 建立 beat grid、loudness、speech rate、silence、情绪/强调、BGM 窗口、ducking/sidechain 与 audio transition 规划。 | audio analysis、audio mixer、edit skills。 | “按音乐节拍剪成 30 秒短视频”生成可审阅节拍对齐方案，且输出响度符合 profile。 |
| VE-07 | P1 | 缺少高质量 B-roll、素材检索和镜头替代决策。 | 将 semantic index、OCR/transcript/视觉标签、资产许可和品牌规则联通；每个 B-roll 建议显示 source、timecode、原因、替代项和 confidence。 | `semantic_index.py`、Vault、asset library、editorial planner。 | Agent 绝不引用当前工程外未授权素材；低置信度候选标记为建议而非自动插入。 |
| VE-08 | P1 | 缺少字幕和图文包装的全流程。 | 支持字幕分段、断句、样式模板、避让人脸/Logo、安全区、关键词高亮、翻译、烧录/软字幕与版本化修订。 | `subtitle_placement.py`、Teaching Animation、delivery profiles。 | 在横竖屏输出上字幕不越界、不重叠、可读速度合格，且可从任意字幕回到 transcript。 |
| VE-09 | P1 | 缺少可控的转场、速度、定格、重构图、稳定和降噪策略。 | 给每类效果提供 eligibility detector、参数范围、preview/export support、渲染成本、质量风险和可逆 action；禁止盲目在所有 clip 叠效果。 | effect catalog、timeline effect commands、analysis evidence。 | Agent 会解释“为什么此镜头适合 speed ramp/稳定/降噪”，并在预览失败时回退。 |
| VE-10 | P1 | 缺少用户编辑后的协作与冲突解决。 | 把 AI transaction 与人工 transaction 分组；人工修改使相关 plan stale；支持 rebase suggestion、局部重规划、保留人类 locked clip/marker。 | command stack、revision service、assistant UI。 | 用户手动改一个镜头后，Agent 不覆盖它；会解释冲突并只重规划受影响片段。 |
| VE-11 | P1 | 缺少多版本交付策略。 | 由一个 master editorial plan 产生 16:9、9:16、1:1、不同长度/语言/CTA 的 linked versions；记录派生关系与每版安全区、字幕、重构图差异。 | project schema、timeline version service、deliver controller。 | 生成三个平台版本不会互相破坏；每版可独立预览、批准、导出和回滚。 |
| VE-12 | P2 | 缺少编辑质量评估。 | 计算时长误差、黑帧、冻结帧、跳剪密度、音频连续性、响度、字幕阅读速度、叙事覆盖、镜头重复、品牌元素覆盖；将结果反馈 Replan。 | `quality_gate.py`、delivery validators、new editorial evaluator。 | Agent 输出完成前必须通过目标任务相应的质量 profile，失败附带可行动修复建议。 |
| VE-13 | P2 | 缺少面向长片和海量素材的分层策略。 | 设计 proxy-first、分段分析、分层摘要、检索而非全文 prompt、缓存指纹、按需高清验证和预算驱逐策略。 | `shared/media/proxy_*`、context service、semantic index。 | 两小时素材不会一次性塞进模型上下文；Agent 能按问题检索证据并保持可追溯。 |
| VE-14 | P2 | 缺少真实编辑基准集。 | 建立访谈、产品广告、播客、教程、婚礼/活动、多机位、混合帧率、含问题素材的 fixture 和验收 rubric。 | `tests/ai/`、fixtures、benchmark/eval runner。 | 每类至少有成功、风险阻断、恢复三类 E2E；生成结果与人工基线可比较。 |

### 4.4 调色 Agent 能力计划

调色 Agent 的核心不是“给画面套 LUT”，而是先识别色彩管理与素材问题，再提出可撤销、分层、可匹配、可测量的 grade；最终必须确保预览和交付使用同一颜色语义，尤其不能损坏 HDR 高光。

| ID | 优先级 | 调色能力缺口 | 详细优化内容 | 建议涉及位置 | 验收标准 |
|---|---:|---|---|---|---|
| CG-01 | P0 | Agent 对输入色彩管理、HDR/SDR、相机 log、LUT 和显示目标的上下文不足。 | 建立 `ColorContextSnapshot`：素材 color metadata、检测置信度、input transform、working space、display/output transform、monitor profile、timeline color pipeline 与用户锁定项。 | `shared/media/color_metadata.py`、color core、OCIO adapters。 | Agent 在任何调色计划前都能说明源/工作/输出色彩空间；未知 metadata 先请求确认或分析。 |
| CG-02 | P0 | 缺少镜头级颜色/曝光/白平衡/肤色证据。 | 计算 waveform、histogram、vectorscope、RGB parade、肤色线偏差、曝光分布、clipping、noise、WB 估计、关键区域 mask 与镜头统计。 | color shaders/scopes、analysis service、AI mask engine。 | 每个调色建议引用测量；例如“白平衡偏暖”的依据可见且可复测。 |
| CG-03 | P0 | 调色意图未被建模为可审阅 grade plan。 | 新建 `ColorGradePlan`：target look、shot scope、node/layer operations、参数、mask/tracking refs、LUT/CDL、预览帧、风险、render cost、base revision；不允许模型直接改 shader state。 | `features/color/` core、AI editing adapters、command facade。 | 用户看到每个镜头将改什么；Apply 只经 color command stack/undo group。 |
| CG-04 | P0 | 缺少自动镜头匹配的受控工作流。 | 先选择 reference shot 或 look，分析 exposure/WB/contrast/saturation/hue 分布；产生每镜头建议和 confidence；支持锁定 hero shot、排除创意镜头、手动偏移。 | color analysis、timeline shot groups、grade planner。 | 一组素材可获得匹配建议；低置信度镜头不自动匹配；人工微调不会被后续 run 覆盖。 |
| CG-05 | P1 | 缺少分层与选择性调色 Agent 工具。 | 暴露白平衡、primary、curves、qualifier、window、tracker、skin protection、denoise、sharpen、halation、vignette、keyer 等，但每项定义 input constraints、preview/export support 与风险。 | color effect catalog、masks、tracker、plugins registry。 | “只让肤色自然且保留背景蓝”形成 layer/mask plan，可在 preview 中看 matte 与前后对比。 |
| CG-06 | P1 | HDR/广色域保护需要硬性保障。 | 对 peak/nits、gamut mapping、tone mapping、highlight roll-off、legal/video levels、SDR trim pass 加入 validator；禁止无理由 0–1 clamp 和不兼容 LUT。 | color core/shaders、OCIO config、export validator。 | HDR 素材经 Agent grade 后保留高光；不兼容变换被阻断并给出替代方案。 |
| CG-07 | P1 | 缺少 look development 与品牌色管理。 | 引入 `LookRecipe` / `BrandColorProfile`，支持参考帧、色板、LUT/CDL/DCTL、禁用肤色偏移、产品色保护、版本和 license；所有风格化在 neutral base grade 后。 | presets、effect registry、project payload。 | “科技冷色但产品红保持准确”可产生明确层级与颜色保护规则，导出保持一致。 |
| CG-08 | P1 | 缺少跨镜头、跨机型、跨场景的 grade 扩散控制。 | 建立 shot grouping、scene boundary、camera profile、grade inheritance、copy/apply with offsets、scene-level constraints 和异常检测。 | timeline/media analysis、color session。 | Agent 不把室内夜景 grade 错施到室外日景；批量操作可 preview、分组确认、撤销。 |
| CG-09 | P1 | 缺少调色前后质量比较和自动回退。 | 生成 still/video wipe、split-view、scope diff、clipping diff、skin/gamut warnings；若 post-grade 指标恶化则标记失败并允许回滚/重规划。 | color UI, scopes, quality gate。 | 用户可一键比较 before/after；导出前可定位任何超范围镜头。 |
| CG-10 | P2 | 缺少面向人像/商品/绿幕/低光等场景的专业技能。 | 将修肤、产品保色、曝光救援、相机匹配、日转夜、胶片风、Log 转 Rec.709、绿幕前后色彩协调封装为 skills，分别设定禁用条件。 | color skills/plugins、AI mask engine、keyer shaders。 | 每个 skill 有输入条件、参数解释、preview、风险和回退；不把不适用素材强行处理。 |
| CG-11 | P2 | 缺少调色的回归与客观评测。 | 建立 color chart、肤色、HDR highlight、wide gamut、噪声、keyer、跨机位样本；校验 ΔE/曝光/峰值/gamut/性能及 preview-export consistency。 | color tests、OCIO fixtures、benchmark tooling。 | 改动模型/算法/Shader 后能检测 HDR、肤色或输出变换退化。 |
| CG-12 | P3 | 缺少高级协作调色流程。 | 支持 grade notes、marker-based review、client-approved look lock、远程 reference still、版本比较、CDL/ACES/OTIO/FCPXML 交接审计。 | adapters、review UI、project model。 | 客户反馈可落到具体镜头和 node；交接丢失/降级有清晰报告。 |

### 4.5 合成 Agent 能力计划

合成 Agent 应该能把“我要让人物置于另一背景、加 HUD、把产品屏幕替换进去”拆成素材分析、节点图方案、预览、确认、渲染验证，而不是直接生成不可解释的结果。所有节点图修改都必须具有输入/输出类型、范围、预览/导出兼容性和可逆版本。

| ID | 优先级 | 合成能力缺口 | 详细优化内容 | 建议涉及位置 | 验收标准 |
|---|---:|---|---|---|---|
| CP-01 | P0 | 缺少对合成请求的结构化目标与 node graph plan。 | 新建 `CompositeBrief` / `CompositePlan`：shot scope、期望效果、输入层、背景、alpha、输出规格、节点图 diff、资产、keyframes、风险、render cost、quality checks。 | `features/composite/`、`ai_workflow/adapters/composite_adapter.py`。 | “给产品加科技 HUD”先生成可审阅节点图/时间线差异，确认后才写入 comp。 |
| CP-02 | P0 | 缺少素材可合成性分析。 | 分析 alpha、绿幕质量、spill、边缘、发丝、运动模糊、镜头运动、噪声、曝光/色温差、景深、反射、遮挡和跟踪点。 | keyer/morphology shaders、analysis service、AI mask engine。 | Agent 能说明“此片段需要 rotoscope/track，绿幕边缘置信度低”；不在低质量素材上假装可一键抠像。 |
| CP-03 | P0 | 节点、端口和动作虽有基础模型，但 AI 计划缺少完整强类型检查与受控写入。 | 统一 node spec、port types、参数 schema、asset refs、frame ranges、GPU/CPU backend capability、preview/export flags；node graph mutation 走 command transaction。 | composite core/node engine、workflow validator、host bridge。 | 无效连接、循环、缺素材、未批准 import、GPU 不支持节点在 plan/compile 阶段明确阻断。 |
| CP-04 | P1 | 缺少抠像、rotoscope、matte refinement 的多步骤 Agent skill。 | 编排 keyer 初始方案 → matte metrics → garbage/core matte → spill suppression → edge refine → temporal stability → 人工笔刷/修正请求；保存每步 artifact。 | keyer shaders、mask engine、composite skills。 | 用户能看到 matte 前后与边缘质量；帧间闪烁或低置信度自动进入人工修正。 |
| CP-05 | P1 | 缺少 tracking、screen replacement 和运动图形绑定。 | 支持 point/planar/camera tracking evidence、track confidence、occlusion、corner pin、motion blur、scene cut re-track；Motion Graphic scene 可作为合成节点输入。 | tracker subsystem、Teaching Animation host adapters、Composite nodes。 | “把画面插到手机屏幕上”可生成 track 与 corner-pin 方案；镜头切换处不会错误延续追踪。 |
| CP-06 | P1 | 缺少色彩、光照、颗粒、景深等整合质量。 | 设计 integration pass：颜色匹配、light wrap、shadow/contact shadow、grain match、blur/DOF、lens distortion/chromatic aberration、motion blur、noise consistency。 | color/composite bridge、effects registry。 | 合成质量门可测边缘、色温、颗粒/模糊一致性；用户可关闭每一层查看贡献。 |
| CP-07 | P1 | 缺少可解释的分层、预合成与模板化。 | 引入 groups/precomps/instances、公开参数、版本化 comp template、命名空间、依赖 graph、asset relink 和 embedding policy。 | composite project schema、node graph model、template registry。 | 同一 HUD/Lower Third 模板可在多镜头复用，实例修改不破坏模板或其他实例。 |
| CP-08 | P1 | 缺少节点图局部预览、缓存与性能预算。 | 按 node/region/time range 做 proxy render、cache key、GPU memory budget、invalidations、progress/cancel；将性能预测列入 plan。 | composite renderer/cache、shared media cache、Job scheduler。 | 用户改一个节点不必全图重渲；缓存过期、资源超限和降级选择可解释。 |
| CP-09 | P1 | 缺少合成的质量门与渲染后检查。 | 检查缺输入、alpha、premultiplied/straight alpha、黑边、边缘闪烁、帧间抖动、色彩空间、渲染错误、输出尺寸/时长、hash/ffprobe。 | composite export, delivery quality gate。 | 合成 render 成功后仍必须通过 alpha/边缘/色彩/编码验证，失败保留诊断和 preview。 |
| CP-10 | P2 | 缺少生成式合成工具的安全接入。 | 对 inpaint/outpaint、object remove、background replace、image/video generation 建立遮罩范围、素材上传 consent、provider license、安全审核、temporal consistency、provenance 和导入审批。 | generation service、mask engine、GenerationBin、privacy guard。 | 生成式结果先进入候选箱；用户显式选择后才进入 comp/timeline，且可查看来源和许可。 |
| CP-11 | P2 | 缺少合成专业评测集与故障注入。 | fixture 覆盖绿幕、发丝、透明物体、快速运动、运动模糊、反光屏幕、低光噪声、镜头切换、HDR 和不同 alpha 格式。 | composite tests、render fixtures、benchmark runner。 | 每类素材都有质量阈值、性能基线和失败降级行为；不存在只在 demo 素材可用的能力。 |
| CP-12 | P3 | 缺少高级视觉效果/3D/协作能力。 | 在基础闭环稳定后支持 3D camera、depth/normal、particles、physics、表达式、脚本受控 sandbox、版本评论、Nuke/AE interchange。 | composite roadmap、interchange adapters。 | 每项新后端/节点都有 capability declaration、可预览性、可导出性、资源预算和迁移策略。 |

### 4.6 跨域编排：剪辑、调色、合成如何由同一 Agent 协同

以下依赖顺序应固化在 Workflow / Skill 的 DAG 中，模型可以选择“跳过”或“请求人工确认”，但不能倒置或静默绕过质量门：

```text
导入与素材探针
  → 素材理解（转录 / 镜头 / 音频 / 颜色 / alpha / 品牌证据）
  → Editorial Brief 与粗剪计划
  → 粗剪 preview / 审批 / CommandStack 提交
  → Shot grouping 与基础色彩管理
  → Grade Plan preview / 审批 / 提交
  → Composite Plan（仅对已选镜头）preview / 审批 / 提交
  → 字幕、音频、Motion Graphic、平台重构图
  → 完整预览与交付质量门
  → 导出 / manifest / 可回滚交付版本
```

| ID | 优先级 | 编排风险 | 实施规则 | 验收标准 |
|---|---:|---|---|---|
| IX-01 | P0 | 时间线、grade、comp 的 revision 可能互相失效。 | 每个 domain plan 引用上游 artifact/hash/revision；上游改动只使下游受影响范围 stale，并提供 rebase/replan。 | 替换一个镜头后，只要求重评该镜头 grade/comp，而不是静默复用旧结果。 |
| IX-02 | P0 | Preview 和 export 在不同模块可能得到不同像素语义。 | 定义共享 render contract：resolution、fps、color pipeline、alpha、effects、time range、proxy/full-res 说明；所有 preview artifact 有 render profile。 | 同一 revision 的 preview/export 对抽样帧、时长、色彩元数据满足一致性阈值。 |
| IX-03 | P1 | 跨域 Agent 容易反复全量渲染，成本不可控。 | 使用 dirty range、dependency graph、asset/render cache、preview resolution tier、成本预测和用户可选预算。 | 修改字幕不触发无关的全片 comp；预算超限时先给出替代方案。 |
| IX-04 | P1 | 多个专业 Skill 可能相互覆盖用户意图。 | 建立 user lock / protected regions：锁定 clip、grade node、comp group、字幕轨、品牌层；规划器只能建议而不能覆盖锁定对象。 | 用户手工锁定的颜色和 Logo 在后续自动剪辑/合成任务中保持不变。 |
| IX-05 | P1 | 交付规格的错误往往最后才暴露。 | 在 brief 阶段绑定 delivery profile，并提前验证画幅、fps、codec、色彩、响度、字幕、safe area、透明通道和版权要求。 | 任一不兼容资产/效果早于最终导出被发现，并给出可操作修复路径。 |
| IX-06 | P2 | 跨域任务难以判断“效果好不好”。 | 汇总 editorial、color、composite、delivery 四类 scorecard，并允许项目配置权重/人工 override；评分只能辅助决策，不能绕过质量红线。 | Mission 完成报告显示每个领域的证据、阈值、未决风险和人工决策。 |

## 5. 推荐实施批次

不要按表格编号并行堆功能。先让“方案是否满意、确认执行是否安全、结果是否可验证”成为一条短而真实的链路，再扩展模型、媒体与自动化范围。

| 批次 | 包含项 | 交付结果 | 退出条件 |
|---|---|---|---|
| B0：基线与契约 | MG-01、MG-06、MG-10、MG-22、MG-23 | 统一 brief、计划/审批 scope、错误码、权限矩阵和 validator 合同。 | 所有写入前均可判定 scope/revision/permission；没有静默模板回退。 |
| B1：可满意方案 | MG-02、MG-03、MG-04、MG-05、MG-08 | 多方案、版本化改稿、真实预览和清晰确认卡。 | 用户能看方案、要求修改、比较版本，且只有指定版本可确认。 |
| B2：真实执行与编辑 | MG-07、MG-09、MG-16、MG-19、MG-20、MG-21 | Motion Graphic 接入统一 Agent runtime；生成后可安全编辑、取消、重试、恢复和定位。 | 从 Assistant 发起的一次任务具有统一 run、artifact、审计、幂等与恢复语义。 |
| B3：交付与素材能力 | MG-11、MG-12、MG-13、MG-14、MG-15、MG-17、MG-18 | 多画幅、工程上下文、品牌/素材/音频与后台导出/质量门。 | 15 秒片段能在目标 profile 下生成、预览、导出，并提供质量报告。 |
| B4：评测与发布 | MG-24、MG-25、MG-26、MG-27 | E2E、真实环境证据、性能 SLO、灰度和运营闭环。 | 真实数据而非 fake run 证明可在限定范围内稳定交付。 |
| B5：能力扩展 | MG-28 | 高级创作与协作功能。 | 每个新能力均不绕过 B0–B4 的安全、审计和质量边界。 |

### 5.1 全产品实施路线（视频剪辑 / 调色 / 合成）

原 B0–B5 是 Motion Graphic 的垂直切片。要把项目升级为 Codex 式专业视频 Agent，应按以下产品级波次推进；每一波都必须有代码、focused tests、真实/模拟证据边界和更新后的计划状态，不能只新增 UI 文案或 fake provider。

| 波次 | 覆盖计划项 | 目标 | 主要交付物 | 不可跳过的退出门 |
|---|---|---|---|---|
| W0：Agent 内核与安全基线 | OS-01–OS-05、OS-10、AG-01–AG-03、AG-07、IX-01 | 统一目标、上下文、工具、证据、计划、审批和 revision 语义。 | `AgentMission`、RunContext、Capability Catalog、Evidence schema、Action matrix、统一 preview/apply contract。 | 任一 Agent 入口无法越过 scope、revision、policy、approval 或 CommandStack；不支持动作不能假装已执行。 |
| W1：可观察且可修改的创作体验 | OS-06–OS-07、MG-01–MG-10、MG-19、VE-03、CG-03、CP-01 | 让用户真正看到方案、预览、差异、风险，并可连续改稿。 | Mission/Plan workspace、版本化 Brief/Feedback、Motion Graphic preview、Editorial/Grade/Comp plan cards。 | 用户能只靠产品 UI 完成“提出需求→看预览→三轮修改→确认”，确认前零工程写入。 |
| W2：受控的真实执行 | OS-08–OS-10、MG-07/MG-16/MG-20/MG-21、VE-04/VE-10、CG-04、CP-03 | 所有领域写入接入统一 Job DAG、事务、取消、恢复和人工接管。 | scheduler lanes、typed artifacts、checkpoint/reconcile、single-writer transactions、rollback UI。 | 并发、重复点击、取消、网络失败、重启、stale revision 均无重复写入或半套工程。 |
| W3：视频剪辑生产能力 | VE-01–VE-13、MG-11/MG-12/MG-15、AG-04–AG-06 | 让 Agent 能理解素材并安全交付可审阅的剪辑。 | ASR/shot/audio evidence、multi-cam、B-roll、字幕、音频、版本派生、Delivery Contract。 | 至少完成访谈精剪、15/30 秒广告、教程/短视频三条真实素材链路。 |
| W4：调色与合成生产能力 | CG-01–CG-11、CP-02–CP-11、IX-02–IX-05 | 让 Agent 可提出并执行色彩/合成方案，且不损坏色彩管理和渲染一致性。 | ColorContext/GradePlan、shot match、HDR guard、CompositePlan、key/track/integration pass、跨域 cache。 | HDR/SDR、绿幕/跟踪、品牌色保护等真实样本通过预览—应用—导出质量门。 |
| W5：技能化、评测和灰度运营 | OS-11–OS-12、VE-14、CG-12、CP-12、AG-08、IX-06、MG-24–MG-28 | 把能力沉淀为可复用、可评测、可运营的专业 Agent。 | Skill pack、golden projects、scorecards、benchmarks、canary、kill switch、runbook。 | 可量化的成功率、质量、时延、成本、安全、人工接管和满意度达标，且可回滚。 |

### 5.2 推荐的首批专业 Skill 清单

每个 Skill 都必须是可版本化的“受控工作流”，而不是一段 prompt：它需要输入表单、可用工具、默认权限、预览产物、质量门、失败恢复、预算和验收样例。

| Skill ID | 面向用户的能力 | 默认自动范围 | 必须人工确认 | 关键质量门 |
|---|---|---|---|---|
| `topos.interview_highlights` | 把访谈剪成 30–90 秒重点片段，保留核心观点和字幕。 | 分析、候选片段、粗剪 preview。 | 时间线提交、音乐/付费素材、导出。 | transcript coverage、跳剪、字幕阅读速度、响度、时长。 |
| `topos.product_ad` | 用产品素材制作 15/30 秒广告和多平台版本。 | 方案、Storyboard、低清 preview、候选 B-roll。 | 时间线/品牌资产写入、生成素材、交付。 | 卖点/CTA 覆盖、品牌色、safe area、平台规格。 |
| `topos.podcast_social_cut` | 从播客创建横竖屏社媒切片。 | 转录、话题检索、候选起止点、字幕 preview。 | 应用剪辑、配乐、导出。 | 论点完整、说话人正确、9:16 reframe、字幕/响度。 |
| `topos.course_cleanup` | 教程去停顿、章节化、重点字幕与 Motion Graphic 提示。 | 检测静音/重复、章节建议、MG 草案。 | 删除/重排、插入 MG、导出。 | 讲解语义连续、章节覆盖、删除操作可撤销。 |
| `topos.camera_match_grade` | 多机位基础匹配与中性校正。 | 色彩分析、reference/shot group 建议、still preview。 | GradePlan apply、LUT/DCTL 导入、交付。 | color metadata、肤色、clipping、HDR/gamut。 |
| `topos.brand_look` | 套用品牌 Look，同时保护产品色与肤色。 | look 方案、受影响镜头、before/after preview。 | 批量写入、输出版本。 | 品牌色容差、肤色、logo、preview/export consistency。 |
| `topos.green_screen_comp` | 绿幕抠像、溢色处理、背景替换和整合。 | key quality analysis、node graph proposal、proxy preview。 | comp graph apply、外部背景导入、final render。 | matte edge、spill、temporal stability、color/blur/grain integration。 |
| `topos.screen_replacement` | 屏幕跟踪、角点定位、内容替换与遮挡。 | track proposal、corner-pin preview。 | graph/timeline apply、生成的屏幕内容导入。 | track confidence、cut boundary、occlusion、motion blur。 |

## 6. 首个可交付的垂直切片（建议优先实现）

用用户给出的案例作为唯一验收用例，避免一开始泛化到所有视频生成能力：

> “创建一个 15 秒、16:9、中文的 Motion Graphic Clip：介绍 AI Agent；深色科技风；4 个步骤：理解、规划、执行、验证；无外部付费素材；插入播放头所在 Graphics 轨。”

完成路径：

1. Assistant 提取并展示可编辑 Brief；未确定画幅、时长或插入点时询问。
2. Planner 返回 2 个**文本化创意方案**（叙事、文案、配色、节奏、元素清单），不写工程。
3. 用户选择方案并可说“标题改成‘你的 AI 剪辑伙伴’，节奏更快，改为竖屏”。系统生成新 revision。
4. 系统把所选 revision 编译为受限场景命令，运行全部 validator，渲染 480p 可播放预览和 storyboard。
5. 卡片显示预览、SceneDiff、将新增的 clip/track、总时长、质量预检、来源与权限；按钮仅有“继续修改 / 取消 / 确认执行”。
6. 用户点击确认后，系统签发一次性 grant，创建场景并经 CommandStack 插入时间线；每步在 Run Drawer 可见。
7. 系统在关键帧、时长、引用和安全区均通过后展示“已生成”；用户可点“编辑此片段”或输入“把第二步改成自动调用工具”。
8. 修改产生新 preview/diff/revision；原片段保留，确认后才替换；用户可从时间线撤销或在版本列表回滚。

该切片通过后，再接入外部图片/视频/音乐/TTS provider。这样可以先验证用户真正需要的“方案满意—确认—可再编辑”，而不是被外部生成服务复杂度掩盖问题。

## 7. 数据模型与接口建议

以下为建议的领域对象；可与既有 `EditPlan`、`ApprovalGrant`、`Artifact`、`RunRecord` 和 Teaching Animation model 对齐，而不要在 UI 中用无约束 `dict` 复制状态。

| 对象 | 核心字段 | 用途 |
|---|---|---|
| `MotionGraphicBrief` | `brief_id`、`revision`、目标、画幅、时长、平台、品牌、素材 scope、风格、预算、constraints | 用户意图的可编辑、可审计来源。 |
| `CreativeProposal` | `proposal_id`、`brief_ref`、方案摘要、beat sheet、文案、style tokens、风险、成本估计、evidence refs | 供用户比较和改稿；不含写入权限。 |
| `MotionGraphicPlan` | `plan_id`、`creative_proposal_ref`、scene commands、scene hash、timeline diff、assets、quality checks、plan hash | 可 dry-run、预览、审批和执行的不可变计划。 |
| `MotionGraphicPreview` | `preview_id`、`plan_hash`、storyboard refs、video artifact、render profile、quality report | 确保用户看到的预览与确认执行的计划完全一致。 |
| `MotionGraphicRevision` | `revision_id`、父 revision、feedback patch、状态、用户决策、created_at | 支撑“持续修改直至满意”的历史与比较。 |
| `MotionGraphicApprovalScope` | project/timeline/brief/plan/preview hash、base revision、asset ids、permission、expires_at | 防止陈旧、错工程、错时间线或重复确认。 |
| `MotionGraphicArtifactLink` | scene、clip、preview、export、quality report、run/node refs | 实现结果卡、时间线与编辑器的双向定位。 |

建议把 LLM 限制在输出 `CreativeProposal` 或 `MotionGraphicPlan` 的严格 schema。LLM 不应获得确认 token、CommandStack、自由文件系统、Shell、任意网络或渲染线程访问能力；宿主只在 schema、policy、preview 和用户审批全部通过后调用写入入口。

## 8. 测试与真实证据矩阵

| 层级 | 必测场景 | 通过条件 |
|---|---|---|
| 单元 | brief patch、schema、hash、scope、validator、命令 dry-run、fallback 状态 | 非法输入 fail closed；相同输入 hash 稳定；修改任一绑定字段后 grant stale。 |
| 集成 | Planner → proposal → preview → approval → CommandStack → undo/redo | 未确认零写入；重复确认零重复插入；人工改时间线后旧 plan 被拒绝。 |
| UI | 方案选择、继续修改、预览播放、进度、取消、错误、retry、定位、版本回滚 | 每个操作都有真实 handler；失效按钮不显示或明确禁用。 |
| 故障注入 | JSON 无效、provider 超时、渲染失败、磁盘不足、FFmpeg 失败、取消、应用重启、revision 冲突 | 无半套 scene/clip/最终文件；有稳定诊断和安全恢复路径。 |
| 真实环境 | 已授权 LLM、可选真实素材/provider、1080p/4K、16:9/9:16 | 报告记录脱敏 provider/模型/版本、成本、耗时、质量门；不得用 fake 成功替代。 |
| 人工验收 | 用户连续三次改稿后确认、再精修、导出 | 用户能独立完成，且清楚知道何时是草稿、预览、已写入和可交付。 |

## 9. 完成定义（Definition of Done）

只有满足以下全部条件，才适合把此能力描述为“可自主运行的 AI Agent 剪辑助手（Motion Graphic 范围）”：

1. 用户先看到可播放、可比较、可修改的方案，确认前不会创建 scene、写入时间线、上传素材或产生未同意的付费请求。
2. 每个确认与 project、timeline、brief、plan、preview、权限、范围和 revision 绑定；重复、过期或越权请求均被拒绝。
3. 用户可以多轮修改，并能比较/恢复草案；生成后可以再次自然语言修改且每次仍走 preview → approval → apply。
4. 生成、预览、插入、替换、导出均有 run/job/artifact、进度、取消、诊断、幂等和受控恢复语义。
5. 预览、提交和导出都经场景、时间线、资产、关键帧、安全区、时长、编码与质量门验证。
6. 模板、真实模型、外部素材和 fallback 的来源均显式可见；模型失败绝不静默替换为“看似定制”的样板。
7. 有 CI 可运行的 deterministic 测试，以及在授权环境记录的真实端到端证据；两者不会混淆。
8. 自动模式始终受限、可暂停、可审计、可回滚；最终导出、云上传、付费生成和破坏性替换遵循独立审批策略。

## 10. 执行注意事项

- 先复用现有 `ApprovalService`、`CommandStack`、`HarnessRuntime`、`ArtifactStore`、`GenerationBin`、`AssetResolver` 和导出质量门，不要从 Assistant UI 直接写 timeline 或调用 GPU。
- 新的 scene/brief/revision 序列化必须具备 schema version 与迁移策略，并保留未知模块 payload；不得把 API key、绝对路径、原始敏感 prompt 或临时下载 URL 保存进 `.topos`。
- 预览/渲染/编码始终走既有后台 worker/队列，UI 只显示状态和发送意图；取消、失败和关闭窗口均释放资源。
- 第一阶段不承诺“完全无限自主”。正确产品承诺是“用户设置范围与权限后的有界自主执行”：方案可自动形成，写入和高风险外部行为必须可见、可控制、可恢复。
