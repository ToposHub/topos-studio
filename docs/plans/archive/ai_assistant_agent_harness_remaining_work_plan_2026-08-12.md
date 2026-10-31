# AI Assistant Agent Harness：剩余工作精简计划（审计版）

> 日期：2026-08-12
>
> 依据：`ai_assistant_agent_harness_product_plan_2026-08-01.md` 的 M0–M12，结合当前代码、装配点与测试的审计。
> 本文件**只列尚未完成、仅有骨架、或尚未达到真实可交付标准的工作**；已完成的基础能力不在此重复立项。MotionGraphicClip 的渲染/编辑器路线继续以 `motion_graphic_clip_remotion_inspired_optimization_plan_2026-08-12.md` 为唯一计划，不混入本文件。

---

## 1. 审计口径与结论

### 1.1 口径

- **完成（代码/测试）**：已有生产路径代码，并有对应自动化测试；这不等同于真实云账号、真实模型包和完整 GUI E2E 已验收。
- **部分完成**：数据模型、协议、桩实现或局部 UI 已存在，但仍缺真实 provider、真实 transport、实际可执行编辑动作或发布级验证。
- **未完成**：旧计划明确要求的用户能力尚没有对应实现，或仅有接口名。

本轮已运行的核心回归命令：

```bash
python3 -m pytest tests/edit/ai_editing src/features/ai_harness/tests -q
```

此前核心回归为 **1811 passed, 1 skipped（8.82s）**；本轮继续收口后，包含 integration 的回归为 **1889 passed, 2 skipped**。结果证明核心编辑 AI、Harness 与离线 integration 链路稳定；它**不**替代真实 DeepSeek、ASR/OCR 模型包、MCP 客户端或生成平台的集成验收。

### 1.2 已完成、无需重新开发的基础

| 旧里程碑 | 审计状态 | 已有证据（示例） | 本计划处理方式 |
| --- | --- | --- | --- |
| M0 工程基线与功能开关 | 完成（代码/测试） | `src/shared/feature_flags.py`；AI/分析/生成/MCP 默认关闭 | 保留开关，后续只补发布配置与验收，不重做框架。 |
| M1 安全编辑闭环 | 完成（代码/测试） | `EditPlan`、`ApprovalService`、transaction/preview/undo 测试 | 所有新能力必须复用此链路，禁止另建直写时间线路径。 |
| M2 分析数据地基 | 完成（代码/测试） | `AnalysisRequest`、timebase、evidence、fingerprint、cache/持久化测试 | 后续视觉和生成只扩展合同，不改写坐标与证据边界。 |
| M3 媒体池可选分析 | 完成（代码/测试） | `AnalysisService`、`AnalysisScheduler`、`MediaAnalysisController`、媒体池 UI 接线 | 后续只增加 recipe/provider，不在 widget 内做解码或推理。 |
| M5 Dock/Context 基础设施 | 完成（代码/测试） | Composer、Job Panel、Context/Session 模型和 controller 接线 | 本计划补真实状态、真实 provider 与端到端体验，不重做 Dock。 |

### 1.3 仍需完成的旧里程碑

| 旧里程碑 | 状态 | 还缺什么（精确结论） | 对应批次 |
| --- | --- | --- | --- |
| M4 文字剪辑证据 | 本地实现收口；发布阻断 | capability 必须经连接验证才可用；placeholder SHA fail closed。仍缺干净机器、真实素材和真实模型包验收。 | B0 外部验收 |
| M6 创作搭档/软件助手 | 本地实现收口；发布阻断 | fallback/source 状态不可伪装为真实模型结果。仍缺无模型/断网下的人工 GUI 行为验收。 | B0、B1 外部验收 |
| M7 首个剪辑工作流 | 本地实现收口；发布阻断 | 字幕本地化只对已物化的 Media Pool 音频产生真实 `ADD_CLIP`，不再使用 `ADD_REVIEW_NOTE` 占位。仍缺三类工作流的人工 rubric。 | B1 外部验收 |
| M8 视觉语义分析 | 本地实现收口；发布阻断 | caption/tag/embedding 已按镜头接入分析、检索和可重建缓存；没有已验证模型会失败而非产出 demo。仍缺真实模型、性能和 UI 验收。 | B2 外部验收 |
| M9 Skill Runtime/受控子智能体 | 本地合同完成；发布阻断 | deterministic fallback 保持显式来源，预算/取消/单候选审批合同仍受既有测试保护。仍缺真实 LLM profile 的端到端评测。 | B3 外部验收 |
| M10 MCP/自动化 Harness | 本地实现收口；发布阻断 | 已接当前用户 Unix socket、stdio proxy 和 `toposctl --socket`；读/预览使用同一 ToolFacade，write 不可由 CLI 授权。仍缺真实客户端互操作与 GUI write 录证。 | B3 外部验收 |
| M11 生成素材箱 | 本地实现收口；发布阻断 | Provider Pack 已有脱敏 resume、最终 poll 正确完成和远端 cancel；物化仍受 SHA/MIME/probe 把关。仍缺真实 Seedance/云 provider 验收。 | B4 外部验收 |
| M12 质量与发布 | 证据门实现完成；发布阻断 | release evidence 缺失即 fail closed。仍缺真实性能、隐私、无障碍、灰度与回滚记录。 | B5 外部验收 |

### 1.4 不重复操作的硬规则

1. 新工作流、MCP、Skill、生成资产都只能经现有 `ToolFacade → Preview → ApprovalService → CommandStack` 写入；不得为“更快接入”绕过 M1。
2. 不重建 `AnalysisService`、`SemanticIndex`、JobRegistry、Dock 或 feature flag；新增能力以 provider/recipe/adapter 的方式接入。
3. 确定性 stub 只允许用于单测、离线演示和明确标识的 demo profile。普通用户界面不得把 stub 结果显示为真实分析或真实生成。
4. 每批先补测试和 fixture，再开下一个批次；真实云账号验收使用独立的 opt-in 环境变量，不能进入默认离线测试。

---

## 2. 推荐实施顺序

```text
B0 真实可用性封口
  ↓
B1 三个首发工作流变为真实可执行计划
  ↓
B2 视觉语义检索
  ↓
B3 正式 Skill Runtime + 本机 MCP
  ↓
B4 Provider Pack 与生成素材箱产品化
  ↓
B5 发布质量、灰度与运营闭环
```

这个顺序刻意把“先接更多模型/API”的冲动放在 B4 之后：没有 B0–B1 的真实证据与可撤销编辑，生成式能力只会扩大不可控输入和排障面；没有 B2 的素材理解，受控子智能体也难以给出稳定的检索和 B-roll 建议。

---

## 3. 分批实施计划

## B0：把已有 Harness 变成真实可用，而非 demo

**解决旧 M4、M5、M6 的剩余发布缺口。**本批不增加新的用户创作能力，目标是让已存在的本地分析、Dock、帮助和状态提示在真实机器上诚实可用。

### 设计与实现

1. 建立 `CapabilityStatus` 驱动的单一状态表：每个 ASR/OCR/LLM/help/provider 显示 `available`、`not_installed`、`offline`、`demo`、`blocked_by_policy` 或 `failed`；禁止把 fallback/stub 伪装成完成。
2. 把模型包从 placeholder manifest 收敛为可验证分发合同：版本、许可证、平台/显存要求、真实 URL 或受控本地来源、SHA-256、安装/删除/损坏重试。密钥继续只放系统安全存储，工程文件、日志和 artifact 不保存密钥。
3. 为 ASR、词级对齐、diarization 和 OCR 各建立一个脱敏的小型真实媒体 fixture；通过受环境变量保护的 integration marker 执行。结果必须携带 provider/model/version、时间基和覆盖率。
4. 在 Dock 与媒体池加入“为什么不可用/下一步如何修复”的行动卡：安装模型包、切换本地/云 profile、重新分析、查看权限范围。离线、无工程、stale、partial 都必须可见。
5. 给 help/brief/artifact 添加人工 GUI 验收脚本：无模型时可浏览本地帮助、编辑 Brief；模型失败时保留用户编辑并显示引用与失败原因。

### 自动验证

```bash
python3 -m pytest tests/edit/ai_editing src/features/ai_harness/tests -q
python3 -m pytest tests/edit/ai_editing/test_transcript_provider.py \
  tests/edit/ai_editing/test_ocr_provider.py \
  tests/edit/ai_editing/test_model_pack_manager.py -q
# 仅 CI secret / 测试机显式启用：真实模型包、真实脱敏媒体
TOPOS_RUN_REAL_AI_INTEGRATION=1 python3 -m pytest tests/integration/ai -m real_ai -q
```

### 人工验收与退出条件

- 在无模型的新用户 profile 中点击“分析素材”，界面明确提示安装而不是产出假字幕/OCR。
- 在已安装测试机上，口播、访谈、无音频、VFR 四类素材均得到可回跳的证据；取消、断网、模型包损坏均能恢复且不阻塞播放。
- Dock 中每个状态、上传范围和费用提示可被用户看见；离线帮助与 Brief 不依赖 API key。
- 上述条件和真实 integration suite 都通过，才开始 B1。

---

## B1：完成首个真实创作闭环

**解决旧 M6–M7。**范围严格限定为：口播精剪、30 秒产品广告、字幕初稿/字幕本地化。先把已有 Skill 做实，不先做 Skill 市场。

### 设计与实现

1. 将三个官方工作流各自定义为版本化输入/输出合同：`Brief + ContextBundle + evidence refs → typed artifacts + A/B EditPlan + rationale + diagnostics`。每个关键剪点必须能回到 source frame/字幕证据；证据不足只能请求分析或输出明确的低置信度草案。
2. 替换所有产品路径的 placeholder plan。尤其要新增并编译真实的“创建/选择音轨 + 放置已 materialize 音频”操作，替换 `editorial_skills.py` 中以 `ADD_REVIEW_NOTE` 代替 TTS 音频放置的实现。
3. 禁止 `skill_dry_run.py` 的 placeholder 进入用户执行路径；它只能保留为测试 fixture。官方 workflow 必须生成 schema 校验通过、可 Preview、可 Diff 的真 `EditPlan`。
4. 落实用户交互：比较 A/B、锁定片段/文案、拒绝建议、重规划；提交后必须是一个 MacroCommand，Undo/Redo 恢复 timeline、project 与 renderer 一致状态。
5. 为每个工作流建立固定 golden fixture、失败 fixture（无证据、stale、混合语言、长素材、受保护轨道）和人工评分 rubric：证据正确性、剪辑合理性、字幕可读性、撤销正确性。

### 自动验证

```bash
python3 -m pytest src/features/ai_harness/tests/test_p2_acceptance.py \
  src/features/ai_harness/tests/test_skill_dry_run.py \
  tests/edit/ai_editing/test_assistant_plan_lifecycle.py -q
python3 -m pytest tests/edit/ai_editing -q
```

新增测试最低要求：

- TTS 本地化计划必须编译出真实音频 clip/track command，不能包含 `ADD_REVIEW_NOTE` 占位。
- 三个 workflow 的 A/B variant 都经 validator、preview、apply、单次 undo/redo；失败、重复批准、revision 变化均无半写入。
- golden fixture 对每个 clip 的 evidence ref、source frame range、诊断和预期 Diff 做快照比较。

### 人工验收与退出条件

- 新用户从“导入 → 手动分析 → 输入需求 → 选 A/B → Diff → 应用 → Ctrl+Z”完成三类故事，不需开发者手工修项目 JSON。
- 30 秒广告未分析素材不能被声称“已理解”；字幕本地化的音频在新音轨中正确对齐并可撤销。
- 人工 rubric 达到团队预先写明的最低分，并记录失败样例后，才开始 B2。

---

## B2：补齐视觉语义检索，而不是重做 OCR

**解决旧 M8。**已有 `keyframe_sampler.py` 和 `OcrProvider` 的镜头级抽样/真实推理路径是本批基础；本批不逐帧扫描，也不把图片或 embedding 写进工程文件。

### 设计与实现

1. 新增三个可替换 provider 合同：`VisualTagProvider`、`VisualCaptionProvider`、`EmbeddingProvider`。输入只能是 B2 的可重建关键帧，输出必须包含 `asset_id`、shot/frame range、置信度、model/version 和字段级 provenance。
2. 定义 shot 聚合策略：首/中/尾关键帧的标签和 caption 采用可解释的置信度合并；相互冲突时保留多候选和冲突诊断，禁止由单帧覆盖整个长镜头。
3. 扩展 `SemanticIndex`：文字、OCR、标签、caption、相似度查询统一返回 evidence list，而非裸字符串。支持“产品特写”“屏幕文字”“有人在讲话”等检索，并显示命中帧、置信度和来源。
4. 设计媒体池/Agent Context 的视觉覆盖与隐私控制：按素材启用、关闭敏感分析、删除可重建缓存、删除索引；OCR/网页文字和模型 caption 均按 untrusted data 进入 Agent。
5. 在 worker/job scheduler 内运行 CPU/GPU 推理；与播放、导出争用时可暂停/降优先级。embedding、缩略图和图片缓存独立于工程文件并可按 manifest 重建。

### 自动验证

```bash
python3 -m pytest tests/edit/ai_editing/test_keyframe_sampler.py \
  tests/edit/ai_editing/test_ocr_provider.py \
  tests/edit/ai_editing/test_analysis.py -q
python3 -m pytest tests/edit/ai_editing -q
```

新增测试最低要求：shot 首/中/尾去重；长片不会逐帧推理；标签冲突不会伪装为确定结论；删除缓存后可由 manifest 重建；恶意 OCR/caption 文本不能提升权限或改变系统指令；相似度检索能准确返回 fixture 的 source frame。

### 人工验收与退出条件

- 在一套标注视频上搜索“产品特写/屏幕文字/人物口播”，每项可定位可播放证据，错例可看到 provider 与置信度。
- 分析时 UI 不冻结；停止/删除视觉分析后，工程仍能正常打开，缓存与索引同步消失。
- 测出并记录 1080p、4K、长素材的时间、显存/内存和缓存上限，达预算后才进入 B3。

---

## B3：正式 Skill Runtime 与本机 MCP

**解决旧 M9–M10。**先把官方、可信 Skill 做成稳定产品，再向外部客户端开放同一条安全通路。

### 设计与实现

1. 为官方 Skill 固化 manifest：版本、输入/输出 schema、工具 allowlist、所需分析 recipe、权限、风险级别、eval 套件、迁移策略。安装/升级/禁用必须可追溯；第三方 Skill 保持只读/proposal 默认。
2. 把真实 LLM `RoleExecutor` 纳入 profile，但显式标记 deterministic fallback；真实执行失败不得悄悄以 stub 冒充 AI 输出。所有子任务都有最小 ContextBundle、预算、deadline、取消传播和 typed artifact。
3. 强制单 timeline 单提交候选；并行角色只读、检索、规划或 QA，最终只交一个候选 EditPlan 给现有审批链。
4. 将现有 in-process `McpServer` 外接为本机 stdio 或 local IPC adapter：当前 OS 用户、动态 tools manifest、默认 read/preview、会话级 allowlist、GUI 签发的 write approval grant。绝不监听公网。
5. 完成 `toposctl` 的 read/validate/preview；connected commit 只在 GUI 显示确认卡且 grant 未过期时执行。UI、CLI、MCP 必须调用同一个 `ToolFacade`。

### 自动验证

```bash
python3 -m pytest src/features/ai_harness/tests tests/edit/ai_editing/test_mcp_gateway.py -q
python3 -m pytest tests/edit/ai_editing/test_skill_runtime.py \
  tests/edit/ai_editing/test_task_graph.py -q
# 新增：在子进程启动 stdio server，以真实 MCP 客户端协议跑 initialize/list/call
python3 -m pytest tests/integration/mcp -q
```

新增安全测试：未初始化会话、flag 关闭、非 allowlist 工具、伪造/重复/过期 grant、任意路径、secret、并发写候选、子任务取消和 transport 输出注入。每一项都必须无写入、无泄露、且 diagnostics 与 UI 一致。

### 人工验收与退出条件

- 外部桌面 MCP 客户端可连接当前用户的本机进程，只看到当前 project/profile 允许的工具。
- read/preview 正常；write 必然回到 Topos GUI 显式批准；撤销连接或关闭工程后会话失效。
- 在真实 LLM profile 断网/超时下界面如实显示失败或降级来源，无“假完成”。

---

## B4：Provider Pack 与生成素材箱产品化

**解决旧 M11。**本批才接入 Seedance 视频、图像、TTS、音乐等服务；它们是可卸载的 Pack，不侵入 UI、timeline 或 Agent 的安全边界。

### 设计与实现

1. 在现有 `GenerationRequest` 之上新增版本化 `GenerationRecipe`：画幅、分辨率、FPS、时长、风格、negative prompt、seed、输出约束、`ReferenceBinding`（首帧/尾帧/人物/产品/风格/音频）和 schema migration。保留 provider-neutral 层，禁止将厂商字段散落在 UI。
2. 定义 `GenerationProviderPack`：capability discovery、鉴权引用、同意前上传清单、请求映射、异步 create/poll/resume/cancel、费用、限流、错误归一化、输出 provenance。首个真实实现可以是 Seedance Video；其余 image/TTS/music 复用合同，先用 contract mock。
3. 将生成候选固定留在 `GenerationBin`：技术/时序/品牌 QA、许可/肖像/内容安全状态、人工预览和明确 `materialize` 操作。失败/取消/删除候选不得改工程或 timeline。
4. 扩展 `GeneratedMediaMaterializer`：大小限制之外增加校验和、MIME/FFprobe 媒体 probe、时长/FPS/分辨率校验、thumbnail、proxy、病毒/解码失败处理，以及通过正式导入服务进入 Media Pool。远端 URL 失效后，本地已导入素材必须仍可重开工程。
5. 新增 `CreativeAssetRegistry` / `ReferencePackage`：跨镜头复用人物、产品、场景、风格与品牌 token；变更 reference 时列出受影响 Recipe/shot。生成素材入时间线只由“已导入的 MediaAsset ID”形成 EditPlan，仍经 Diff/Approval/Undo。
6. 生成 provenance report：provider/模型版本、recipe、reference 上传范围、实际费用、同意、QA、license/safety 结论。未知权利或未通过 QA 禁止标记“商用就绪”。

### 自动验证

```bash
python3 -m pytest tests/edit/ai_editing/test_generation* \
  src/features/ai_harness/tests/test_generation_adapter.py -q
# 新增：Pack contract mock（create → poll → cancel/resume → materialize → import）
python3 -m pytest tests/integration/generation -q
# 仅隔离测试账号：真实 Seedance/其他 Pack 冒烟，不计入默认单测
TOPOS_RUN_REAL_GENERATION=1 python3 -m pytest tests/integration/generation -m real_provider -q
```

新增测试最低要求：每一种 modality 的 recipe schema/迁移；用户未同意、超预算、未授权 reference、重复回调、网络中断、取消、输出 URL 过期、hash 不符、probe 不符、QA/许可证阻断；以及最终 Media Pool 导入、timeline preview、单次 undo/redo 和 provenance 导出。

### 人工验收与退出条件

- 用测试账号生成一个视频、图片、语音/音乐候选；每次都在发送前看见 provider、费用、上传资产和版权提示。
- 候选在素材箱预览，materialize 后能生成缩略图/proxy 并成为普通媒体；未 materialize 的远端候选不能被拖入时间线。
- 导出的 provenance 报告可追溯每次生成；取消和删除候选不会污染工程。

---

## B5：发布质量、灰度与运营闭环

**解决旧 M12。**B5 是贯穿收口批，但必须在 B4 后统一形成发布证据；不以 demo 成功替代发布。

### 设计与实现

1. 固化测试金字塔：纯函数/contract 单测、控制器与 UI 集成、真实模型/真实 provider opt-in 集成、端到端用户故事和人工 eval。真实账号、模型包与网络测试不得污染默认 CI。
2. 建立可版本化的 eval corpus 与 rubric：证据定位率、视觉检索命中率、preview 成功率、单次 Undo 正确率、任务取消残留率、生成 QA 拦截率、帮助引用正确率、成本/时长。所有媒体须脱敏并可删除。
3. 做性能与可靠性预算：VFR、4K、长转录、低显存、1000 clips、并发 Job、断网、项目关闭；记录 p50/p95、内存/显存、缓存大小及 orphan worker 数。
4. 加入 opt-in、脱敏 telemetry；功能开关、provider/Skill/MCP kill switch、数据导出/删除、审计保留期、无障碍键盘/读屏检查和故障演练。默认不上传 prompt、原素材、绝对路径或密钥。
5. 发布顺序：内部 deterministic/local → 小范围真实本地模型 → 小范围云 LLM → MCP → 单个生成 Pack → 更多 Pack。每一层独立可关闭、可回滚，且使用不同验收指标。

### 验证与发布门

```bash
python3 -m pytest tests/edit/ai_editing src/features/ai_harness/tests -q
python3 -m pytest tests/integration -q
python3 -m pytest tests/e2e -q
```

发布前必须有：安全审计零未确认写入/零 secret-path 泄露；所有已启用能力的 kill switch 演练记录；性能预算报告；可访问性检查；真实 provider 成本与取消回收报告；灰度 cohort 的失败率/用户撤销率/人工质量评分；已知限制与回滚方案。

---

## 4. 每批交付物清单

| 批次 | 必交付代码/配置 | 必交付测试证据 | 必交付产品证据 |
| --- | --- | --- | --- |
| B0 | 真实模型包 manifest、CapabilityStatus、状态/修复 UI | 离线 + real-AI opt-in suite | 新机与测试机验收记录 |
| B1 | 三个官方 workflow、真实音频放置 command、A/B/lock/replan | golden + preview/apply/undo 回归 | 三个用户故事录屏/评分 |
| B2 | visual provider contracts、聚合、检索、缓存/隐私控制 | 关键帧、注入、查询定位、性能测试 | 标注视频命中率报告 |
| B3 | skill lifecycle、真实 executor 标识、本机 MCP transport、CLI 接线 | MCP 子进程 E2E + 越权测试 | 外部客户端连接与 GUI approval 记录 |
| B4 | Recipe、Pack、QA、probe/proxy/import、registry/report | Pack mock + real-provider smoke | 生成→素材池→剪辑→报告演示 |
| B5 | eval/telemetry/kill switch/灰度配置 | 全量回归、压力、E2E、安全报告 | 发布清单、回滚演练、灰度看板 |

## 5. 开工检查表

- [ ] 先在本文件的 B0 建 issue；不从 B4 的 Seedance 接入直接开工。
- [ ] 每个 issue 标注：所属批次、依赖、数据范围、feature flag、owner、自动测试文件、人工验收者和回滚方式。
- [ ] 新增 provider/Skill/MCP 前，先写 contract mock 和失败路径测试。
- [ ] 任何 timeline 写入先展示 Diff，并复用现有审批和一次 Undo 语义。
- [ ] 每完成一个批次，更新旧总计划的对应 M 状态与本文件的审计结论；已完成项从“剩余工作”移除，而不是复制到下一版。

## 6. 2026-08-12 本轮可自动完成项收口记录

本轮在不伪造真实云账号、真实模型包或人工 GUI 证据的前提下，继续完成了以下本地闭环：

- Provider Pack / HTTP provider 支持协作取消，Job 取消会发起远端 cancel，并保留失败诊断。
- `GeneratedAsset` 支持期望 SHA-256；物化阶段校验 checksum，默认媒体 probe 使用受限 ffprobe，probe 失败 fail closed。
- Model Pack 默认启用严格校验；placeholder SHA 只能由显式 demo/test manager 接受。
- 已配置 AI profile 接入受控 LLM RoleExecutor；失败时保留 `deterministic_fallback` 来源，不静默伪装真实模型。
- `EditSession`、预览/导出和 `ToolFacade` 共用 session-scoped `JobRegistry`；`get_job_status/cancel_job` 不再返回宿主占位结果。
- Capability matrix 将“已配置但未连接验证”单列为 `unverified`；任一 placeholder SHA 都不能被严格模型包管理器视为可用。
- 字幕本地化仅从已经 materialize 且导入 Media Pool 的 TTS 候选取得媒体 id，并生成可预览/撤销的音频 `ADD_CLIP`；没有该候选时不再生成占位 timeline 操作。
- 视觉 caption/tag/embedding provider 已按镜头首/中/尾关键帧接入 `AnalysisService` 和 `SemanticIndex`，结果为 untrusted evidence，embedding/缓存可按素材清除并重建。
- `McpServer` 已由当前用户 Unix socket 和 stdio proxy 对外提供，`toposctl --socket PATH` 走同一 `ToolFacade`；socket 仅暴露 read/preview，write 工具隐藏且不能由客户端自行批准。
- Provider Pack checkpoint 已脱敏并可恢复；最后一次允许的 poll 返回完成结果，超时时会向远端发 cancel，避免生成无主 job。

对应全量离线证据记录在 `docs/ai_assistant_agent_harness_acceptance_evidence_2026-08-12.md`。真实 provider、真实模型包、外部 MCP 客户端、GUI 无障碍/灰度回滚仍保留为发布前人工或隔离环境门槛。
