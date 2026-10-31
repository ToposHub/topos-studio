# AI 剪辑稳定化与生产闭环实施计划（2026-07-31）

> 本文是对 `ai_editing_protocol_and_implementation_plan_2026-07-31.md` 的**审查后续计划**。前一份文档定义目标架构；本文只处理已实现代码与目标之间仍未闭合的部分，并给出推荐实施顺序。

## 0. 审查结论与边界

当前项目已经具备很好的 AI 剪辑“内核雏形”：TAEP 数据模型、时间基准、计划校验、编译/事务、素材分析模型、方案/决策模型、FCPXML/OTIO 适配器、工具定义，以及一批 UI 子面板和测试。相关自动化测试当前为 **225 passed**。

但是，这不等于“用户已经可以在主窗口用 AI 完成一次可靠剪辑”。目前最重要的缺口集中在以下闭环：

1. 外部模型的权限、审批和导出授权尚不是可信边界。
2. AI 面板尚未由主窗口装配为真实 `EditSession → preview → approval → commit → UI refresh` 工作流。
3. AI 提交尚未接入现有 UI 的真实 `CommandStack`，所以不能保证 Ctrl+Z/Ctrl+Shift+Z 行为一致。
4. 能力表与 compiler 并非同一真相源；某些被声明支持的 operation 仍无法执行。
5. variant、用户决策、AI metadata、长任务仍没有跨重开工程的完整生命周期。

因此，本计划的目标不是“再叠一个聊天框”，而是把 AI 变成一个受约束的编辑客户端：**它只能提出和预览结构化计划；用户在看到同一份 diff 后批准；软件以统一命令栈原子提交；任何失败、过期、取消或重开工程都可解释且可恢复。**

### 已在本次审查直接修复的低风险问题

| 项目 | 处理 | 验证 |
| --- | --- | --- |
| pytest `app` 标记未注册 | 已在 `pytest.ini` 注册 | 不再产生 UnknownMarkWarning |
| AI Dock 被嵌套进第二个 `QDockWidget` | 改为直接注册 `AiEditAssistantPanel` 本身 | AI UI/controller 测试通过 |
| AI Dock 未纳入统一布局和跨页面可见性管理 | 已加入 `all_docks` 与 Edit 页可见性规则 | 相关 UI 测试通过 |
| Audit `plan_id` 可影响文件路径 | 非安全 ID 改用稳定 SHA-256 文件名 | 新增回归测试 |
| plan 未核对所属工程 | validator 检查 `target.project_id` | 新增跨工程计划回归测试 |

这些修复不替代下面的里程碑；尤其不能把“任意非空 token”当作用户审批。

## 1. 总体不变量（所有阶段均不可破坏）

1. **模型不可信。** LLM、MCP 客户端、脚本文本、文件名、字幕、OCR 和联网检索内容都只能提供数据，不能授予权限、调用 shell、读取任意路径或模拟用户点击。
2. **计划先于写入。** 任何时间线写入必须来自版本化 `EditPlan`，并经历 schema/domain/能力/revision 校验及预览模拟。
3. **批准绑定对象。** 批准必须绑定 project、timeline、base revision、plan hash、权限范围和过期时间；不能只检查字符串“非空”。
4. **预览即提交。** 用户看到的 `TimelineDiff` 必须由将要提交的同一 plan、同一 compiler、同一 base state 得到；revision 改变立即作废。
5. **一个 AI 提交 = 一个 Undo 单元。** AI 不能绕过既有编辑命令栈；Undo/Redo 必须同时恢复 timeline sequence、project refs、revision 与 UI/renderer 同步。
6. **能力表是真相。** manifest、JSON schema、validator、planner、compiler、UI 菜单都从同一可用能力注册表派生；未真正实现的操作绝不暴露给模型。
7. **AI 状态属于工程但 secret 不属于工程。** Brief/Variant/Decision/Audit 可选进入 `.topos` 的版本化 payload；密钥只保存 OS keychain，工程只引用 profile/credential ID。
8. **UI 线程不做长任务。** 模型调用、转录、视觉分析、渲染和导出都经可取消 job/scheduler；Qt 主线程只接收状态事件。
9. **可扩展但可降级。** 新滤镜、转场、字幕、插件、模型 provider 可以动态增加；缺失插件/未知字段/较新 schema 必须可诊断、可保留、不可静默改写。

## 2. 推荐实施顺序

```text
M0 安全与授权边界
        ↓
M1 真实主窗口装配（可用但只做 preview）
        ↓
M2 统一事务 + CommandStack + Plan Repository
        ↓
M3 能力注册与顺序模拟（可安全扩展滤镜/转场/字幕）
        ↓
M4 Decision / AI payload / 工程保存恢复
        ↓
M5 Job 调度、provider、预览与导出
        ↓
M6 Scripts 与 FCPXML/OTIO 的统一入口
        ↓
M7 端到端验收、可观测性与灰度
```

除 M0 外，不建议并行让 AI 写真实时间线。M1 可以先交付“输入需求 → 生成 brief/variants → 预览 diff”，但 `Apply` 必须保持禁用，直到 M2 完成。

---

## M0：安全与授权边界（P0，必须最先完成）

### M0.1 建立可信 ApprovalService

**现状问题**：`ToolFacade` 可从模型参数读取 `approval_token`，`TransactionService` 只检查 token 是否非空。任何客户端均可能伪造 token。

**新增模块建议**：`src/features/edit/ai_editing/approval_service.py`

```python
@dataclass(frozen=True)
class ApprovalScope:
    action: Literal["commit_plan", "render_preview", "export_file"]
    project_id: str
    timeline_id: str | None
    base_revision: int | None
    plan_hash: str | None
    output_grant_id: str | None

@dataclass(frozen=True)
class ApprovalGrant:
    grant_id: str
    scope: ApprovalScope
    expires_at: float
    single_use: bool = True
```

实施步骤：

1. `ApprovalService.issue_after_user_confirmation(...)` 只能被主窗口确认对话框调用；生成随机、不透明、一次性 grant。
2. 保存最小 grant 状态：`grant_id`、scope、expiry、used；不把 secret 或原始模型文本写入工程。
3. `consume(grant_id, expected_scope)` 必须原子检查：存在、未过期、未使用、project/timeline/revision/plan hash 完全匹配；成功后立即标为 used。
4. `TransactionService.commit()` 接收 `ApprovalGrant` 或受信任 `ApprovalService`，不再接收任意字符串。
5. `ToolFacade.invoke()` 的模型 `args` schema 中移除 `approval_token`；transport 只传“用户已确认后由宿主附带”的内部授权上下文。
6. 强制 `plan.mode == PlanMode.COMMIT` 才能进入真实写入；`PREVIEW` 永远不能提交。
7. revision 冲突、plan hash 改变、工程切换、关闭确认框、超过 TTL 后都 revoke grant。

验收：

- 伪造 token、重复 token、过期 token、不同 timeline、不同 revision、不同 plan hash 均返回稳定 `TAEP_PERMISSION_DENIED` 或 stale 诊断，且 project 完全不变。
- 用户批准一次后只能产生一次 `CommitResult.applied=True`。
- ToolFacade 的公开 JSON schema 不包含受信任 token。

### M0.2 预览和导出同样纳入授权

1. `render_preview` 至少需要项目/时间线/range/preset 范围的 preview approval policy；可以允许“本次会话记住”，但仍由宿主策略签发。
2. `request_export` 分为两步：`preflight_export`（无文件写入）与 `confirm_export`（需要 `PathGrant + ApprovalGrant`）。
3. `EditSession.request_path_grant()` 必须由文件选择器/可信宿主生成，包含 canonical 目标、覆盖许可和过期时间。
4. `output_path` 仅可作为 UI 的“建议路径”；外部 plan/MCP 输入的路径永远不能直接获得写文件权。
5. 导出前重新检查：离线素材、缺字体、codec/preset、磁盘空间、覆盖条件、输出仍在 grant 的目录/文件范围。

验收：外部模型仅传 `output_path` 时不能写文件；拒绝确认、preflight 失败、取消 job 都不产生“假成功”文件。

### M0.3 完成公开工具的 JSON transport 合约

**现状问题**：`sanitize_untrusted()` 返回 Python `SanitizedText` 对象，MCP/HTTP 下 `json.dumps()` 会失败。

实施步骤：

1. 保留 Python 内部的 `SanitizedText`（如确有必要），但新增 `to_transport_value()`。
2. 外部返回统一成 JSON，例如 `{"value": "...", "trust": "untrusted", "source": "transcript"}`，或把 trust 放在独立 metadata map；选定一种后写入 TAEP transport 版本。
3. 所有 ToolFacade 分支在返回前执行 JSON serializable contract check。
4. 对返回文本设定大小、条数和字段白名单；超限改为分页/cursor，而不是把完整字幕直接塞入 prompt。

验收：每个公开 tool response 均可 `json.dumps()`；非可信文本仍可被 UI 和模型明确识别为数据。

### M0.4 审计与文件路径安全

本次已修复 `AuditStore` 的 `plan_id` 路径穿越。后续补齐：

1. 所有工程 payload/audit/导出路径共用 `SafePathPolicy`，不得各自拼接 `Path`。
2. `resolve()` 后验证目标仍在授权根路径内；拒绝 symlink 跳转和相对路径逃逸。
3. 审计文件名使用安全 ID 或稳定 hash；原始 `plan_id` 保留在 JSON 内容中以便查询。
4. audit 写入要捕获 I/O 错误并报告为 warning，不能让已成功编辑被误报为失败。

---

## M1：真实主窗口装配与可用的 AI 面板（P0/P1）

### M1.1 建立类型化 `AiEditHostBridge`

**现状问题**：`AiEditController` 通过动态 `host` 属性调用方法；生产主窗口没有实现这些方法，测试 fake host 的方法名也不完全一致。

新增协议：`src/app/controllers/ai_edit_host_bridge.py`

```python
class AiEditHostBridge(Protocol):
    def get_project_sequences(self) -> ProjectSequences: ...
    def get_capability_manifest(self) -> CapabilityManifest: ...
    def get_semantic_index(self) -> SemanticIndex: ...
    def request_plan_approval(self, plan: EditPlan, preview: PreviewResult) -> ApprovalGrant | None: ...
    def commit_ai_command(self, compiled: CompiledPlan, plan: EditPlan, grant: ApprovalGrant) -> CommitResult: ...
    def refresh_after_ai_commit(self, result: CommitResult) -> None: ...
    def project_payload_store(self) -> ProjectPayloadStore | None: ...
```

要求：

- 主窗口或独立 adapter 实现该协议；不得让 `AssistantService` 访问 `session._host` 私有字段。
- Controller constructor 显式接收 bridge，取消 `Any` 和 `hasattr` fallback。
- 为 bridge 写真实集成测试，禁止只用方法名不一致的 fake host 掩盖问题。

### M1.2 在应用生命周期装配会话

1. 在 Edit 模块被启用、工程打开后创建/更新 `EditSession`、`AssistantService`、`AiEditController`、`ToolFacade`。
2. 通过单一 composition root 注入同一个 `CapabilityManifest`、`ApprovalService`、`JobScheduler`、`PlanRepository` 和 `AssistantSessionStore`。
3. 工程关闭/切换时取消对应 jobs、revoke grants、flush 可选 AI payload、解除 signals；不得保留上个工程的 snapshot 或 profile 状态。
4. `DockLayoutMixin` 只负责 Dock 创建和可见性，不能负责服务业务创建；可由 MainWindow 在 Dock 创建后把 panel 交给 composition root 绑定。
5. 为无工程、无 profile、provider 离线、工程只读、编辑模块不可用分别提供确定状态，而不是 broad `except: pass`。

### M1.3 完成 AI 面板最小可用闭环

当前面板仍是 UI-0 空壳，缺少输入框与子面板接线。按以下顺序实现：

1. 增加 request composer：多行需求、发送、停止、长度限制、历史摘要；不显示 API key。
2. 接入 `AiBriefEditor`：模型只生成 draft patch，进入 `AWAITING_BRIEF_APPROVAL`；用户显式“接受/编辑/拒绝”。
3. **删除自动 `approve_brief_patch()` 和 `except Exception: pass`。** 首次请求时由 `BriefService.create_default()` 或显式 draft 创建流程保证 base revision 明确。
4. 接入 `AiVariantList` 与 `AiDecisionInspector`：只展示已完成 variant，选择后进入 preview，不可直接 Apply。
5. 接入 `AiTimelineDiff`：展示 added/removed/moved/trimmed、duration、affected tracks、warnings、base revision、plan hash。
6. Apply 按钮仅在 M2 的 preview 缓存仍有效且用户确认后可用；stale 时只能“刷新/重新规划”。
7. 接入 `AiJobPanel`：显示排队/运行/取消/失败原因，不让取消按钮只改变本地标签。

验收场景：打开一个含素材工程 → 输入需求 → 确认 Brief → 看到至少两个方案 → 选择一项 → 看到真实 diff；此阶段可先停在 preview，不允许真正提交直到 M2。

### M1.4 Preferences 与 Profile 生命周期

1. 在 `PreferencesDialog._build_page_registry()` 注册 `edit.ai_editing`；当前 unknown-page 分支会使该页不可见。
2. `ProfileRegistry` 增加 `load_from_config()` / `save_to_config()`，仅存 `AiProviderProfile.to_dict()` 与 active ID。
3. API key 使用生产 `CredentialStore`（macOS Keychain，其他平台使用可替换 adapter）；不要把 secret 放入 ConfigManager、日志、项目、审计或 crash report。
4. profile 编辑需涵盖 endpoint/model/modalities/data policy/limits，测试连接必须经 M5 job，结果缓存 capability snapshot。
5. profile 改变只影响新 job；运行中的 job 记录实际 profile/capability snapshot，不可被切换后改写。

---

## M2：Plan Repository、真实预览与统一 CommandStack（P0）

### M2.1 保存“被批准的那一份 plan”

**现状问题**：`EditVariant` 只保留 `edit_plan_ref`，实际 plan 在规划后丢失；Controller 依赖没有生产实现的 `build_plan_for_variant()`。

新增 `PlanRepository`（可先为工程内存，M4 接 payload）：

```python
@dataclass(frozen=True)
class StoredPlan:
    plan: EditPlan
    canonical_json: bytes
    content_hash: str
    created_at: float
    source_variant_id: str | None
    preview: PreviewResult | None
    preview_state_fingerprint: str | None
```

实施要求：

1. planner 编译 variant 时立即存入 repository，variant 只引用不可变 plan ID/hash。
2. Preview 结果与 `(plan_hash, project_id, timeline_id, base_revision, manifest_hash, state_fingerprint)` 绑定。
3. 用户确认时显示并签发对该 hash 的 grant；Commit 必须取 repository 中同一 immutable plan，不接受 UI 重新组装的 plan。
4. 任一 fingerprint 改变即 preview stale，自动禁用 Apply。
5. 可对每次重规划保留历史 variant，但默认只显示有效的最新 batch。

### M2.2 将 AI commit 接入真实编辑命令栈

**现状问题**：`EditProjectService.commit_command()` 直接 `command.apply()`，并未注册 UI 的 `CommandStack`；因而 AI 改动不一定能用 UI Undo/Redo 恢复。

设计约束：

1. 提取一个宿主实现的 `TimelineCommandExecutor`，其职责是：
   - 在真实 command stack `push/execute` 一个已编译 `MacroCommand`；
   - 获得实际执行后的 command（包括内部 undo 所需快照）；
   - 通过统一 two-layer sync 写回 project refs + sequence map；
   - 触发 timeline view、media pool、program viewer、renderer snapshot 刷新。
2. `TransactionService` 继续做纯预览/校验/编译；真实 commit 改由 bridge 的 executor 执行，不能自行绕开 stack。
3. Undo/Redo 也必须调用同一同步层并 bump revision；旧 plan 因 revision 变化自动 stale。
4. 禁止 AI 直接修改 timeline widget 或 mutable list；所有路径均为 command。

必须新增的端到端测试：

- AI plan commit 后 `can_undo()` 为 true，执行一次 undo 恢复提交前完整 ProjectSequences；redo 再恢复提交后状态。
- 多 operation plan 只出现一个 undo label。
- commit 成功后 renderer/timeline/media-pool 接到一次刷新事件。
- undo 后旧 preview 不可再次 apply；必须重新 preview。

### M2.3 让 `validate_selected()` 成为真正 preview

1. 删除“返回 scores + plan ref 即视为校验”的实现。
2. `validate_selected(variant_id)` 从 `PlanRepository` 取 plan，调用 `TransactionService.preview()`，返回完整 `PreviewResult`。
3. 结构错误、domain 错误、compile 错误、revision 冲突以稳定 diagnostics 进入 UI；不得仅落入 `FAILED` 字符串。
4. `apply_selected()` 只能接收已缓存的 preview key 与 grant；内部不得访问 `EditSession._host` 私有字段。

---

## M3：统一能力注册、Operation Builder 与顺序模拟（P1）

### M3.1 单一 `OperationCapabilityRegistry`

**现状问题**：manifest 无条件声明很多 `OperationKind`，但 compiler 明确拒绝其中部分 operation；这会诱导模型产生必然失败的 plan。

为每个 operation 注册：

```python
@dataclass(frozen=True)
class OperationCapability:
    kind: OperationKind
    enabled: bool
    schema_fragment: dict[str, Any]
    compiler: Callable[[EditOperation, CompileContext], EditCommand]
    domain_validator: Callable[..., list[Diagnostic]]
    required_feature_ids: tuple[str, ...]
    capability_version: str
```

实施步骤：

1. `build_manifest()` 仅返回 `enabled=True` 且 compiler/validator 均存在的 operation。
2. JSON schema 的 `enum`、ToolFacade 文档、planner allowlist、UI 可选项都从 registry 派生。
3. feature/plugin 禁用或版本不兼容时，从 manifest 移除并给已有 plan 明确 diagnostics，不偷偷替换效果。
4. `capability_manifest_hash` 写入 snapshot、StoredPlan、audit 和 provider context。
5. 先建立对当前代码的 truth table：每个 operation 是否“声明、可校验、可编译、可模拟、可提交、可 undo”。未全绿的一律先隐藏。

### M3.2 正确编译现有和未来效果操作

1. `set_effect/remove_effect` 使用 canonical `AddEffectCommand` / `RemoveEffectCommand` 或新增专用 command，不要把 effect 塞进只支持 transform/crop/speed/opacity 的 `UpdateClipParamCommand`。
2. effect identity 必须包含 `effect_id`、实例 ID、provider/plugin ID、version、parameter patch；不能只用显示名。
3. `set_speed` 将 JSON 数字明确转换为 `SpeedParams`；transform/crop/opacity 同样通过 DTO converter 严格校验单位和值域。
4. 为转场、字幕、标记、音频增益分别编写 command builder，逐项加入 registry；未完成前不暴露。
5. 插件参数使用 capability schema 校验；未知参数、废弃参数、缺插件、版本不匹配必须可诊断。

### M3.3 用顺序模拟替代“每操作对 base 校验”

**现状问题**：`add_clip(c1) → set_effect(c1)` 会在 validator 阶段被误判 c1 不存在。

目标流水线：

```text
schema / capability precheck
  → operation builder 编译第 1 条
  → 在临时不可变 TimelineSequence 上 apply
  → 校验 intermediate state
  → 编译第 2 条并继续 apply
  → ...
  → 最终 timeline domain 校验 + diff
```

要求：

1. 不修改真实 state，不 bump revision，不写 undo stack。
2. diagnostics 标出具体 `op_id`、依赖的先前 op、JSON path 和建议修复方式。
3. 对顺序依赖、clip ID 生成、transition 邻接、轨道重排、删除后引用、效果实例 ID 均覆盖测试。
4. compiler 和 preview 使用同一 builder；不得出现“validator 通过、compiler 才发现不支持”。

---

## M4：Decision 反馈、AI payload 与工程保存恢复（P1）

### M4.1 决策要真正影响下一轮规划

1. `AssistantService` 不再只维护私有 dict；使用 `DecisionStore` 作为唯一工作集。
2. accept/reject/lock/user override 都写入 `DecisionStore`，并产生可审计 `UserOverride`。
3. 下一次 `VariantPlanner.plan()` 显式接收 `PlannerConstraints`：
   - `must_preserve`：锁定/确认的 decision 或 time range；
   - `must_avoid`：被拒绝的素材/范围/处理；
   - `user_overrides`：人工调整后的事实；
   - `locked_tracks`、`protected_caption_ranges` 等未来扩展字段。
4. planner 的选段算法必须真正消费这些约束；若约束不可满足，应返回 explainable conflict，而不是悄悄忽略。
5. 为“锁定了默认不会选中的片段”“拒绝删除口误后再规划”“手工挪动片段后重规划”写回归测试。

### M4.2 正式接入 `.topos` AI payload

当前 `AssistantSessionStore` 直接写 `ai/session.json`，尚未接入工程保存/重开流程。实施：

1. 在 project package 定义稳定入口，例如 `ai/session.json`、`ai/audit/<safe-id>.json`、`ai/plans/<hash>.json`；不要让各 service 直接随意操作磁盘。
2. 由既有 `topos_project_io` / payload cache 统一序列化、加载、原子写入和 unknown-field 保留。
3. `session.json` 加入 `ai_schema_version`、`project_id`、更新时间、可选 profile ID、brief/variant/decision/override/plan refs。
4. profile 密钥、原始聊天、绝对路径、原始音视频、隐私策略禁止保存；审计是否保存 plan snapshot 必须为用户可见的 opt-in。
5. 写 migration：旧版本缺 `ai/` 时正常打开；新版本未知字段 round-trip；损坏 payload 降级为 warning 而非整个工程打不开。
6. 工程另存为、导入、打包、复制、只读工程、协作冲突均定义策略。

验收：保存工程后重开，Brief/Variant 摘要/Decision/Override 可恢复；无 API key；旧工程与较新工程均不丢未知字段。

---

## M5：Job Scheduler、真实 Provider、预览与导出（P1）

### M5.1 让 `JobRegistry` 成为实际调度服务

当前 `jobs.py` 主要是数据合约，`AnalysisService` 仍在调用线程同步 `provider.analyze()`。新增 `AiJobScheduler`：

1. 使用受控 worker thread/process 执行网络、分析、渲染、导出；不在 Qt UI slot 直接执行。
2. 每个 job 记录 project ID、timeline ID、profile ID、capability hash、创建 snapshot revision、取消 token、资源类别。
3. 根据 profile `max_parallel_jobs` 与 GPU/CPU 编码资源限制队列；分析和导出可有不同优先级。
4. job 以 queued → running → cancelling → terminal 迁移；取消是协作式，worker 必须在 provider/ffmpeg/renderer 边界检查 token。
5. worker 不触碰 Qt；通过线程安全事件回到 controller，再用 Qt signal 投递 UI。
6. 工程关闭时 cancel 或 orphan；不得把旧工程 job 的结果写入新工程。

### M5.2 Provider adapter 的最小接口

```python
class AiProvider(Protocol):
    def test_connection(self, profile: AiProviderProfile, cancel: CancelToken) -> ProviderCapabilities: ...
    def propose_brief(self, context: SafeAiContext, cancel: CancelToken) -> BriefPatch: ...
    def propose_plan(self, context: SafeAiContext, tools: ToolClient, cancel: CancelToken) -> PlanProposal: ...
    def analyze(self, request: AnalysisRequest, cancel: CancelToken) -> AssetAnalysis: ...
```

要求：

- Provider 只输出 proposal/tool calls，不能直接修改工程。
- ToolClient 仅暴露按权限过滤的 ToolFacade；write 必经 M0/M2。
- 记录模型名、capability snapshot、成本/时长、错误分类；不记录 secret。
- 所有上传前遵守 `DataPolicy`；无 media-upload 许可时只发送文本/结构化摘要。

### M5.3 预览与导出

1. Preview job 只取短范围、低清 preset，可生成缩略图/waveform；不会覆盖最终文件。
2. Export 复用 committed timeline snapshot，不从正在变化的 UI 读取可变状态。
3. Job result manifest 应使用受授权的相对/opaque artifact reference，避免把本机绝对路径暴露给模型。
4. `AiJobPanel` 展示阶段、百分比、可取消性、错误 code、产物状态；失败时可重试但不自动绕过授权。

---

## M6：脚本、通用 XML 与 OTIO 的统一入口（P1/P2）

### M6.1 Script 面板只调用稳定 API

1. 在既有 Script Panel 增加 TAEP JSON plan 的 load/validate/preview/apply-with-confirmation 命令；不执行任意 Python 拼装时间线。
2. 脚本 API 只暴露 `EditSession` 的安全方法；`load_plan(path)` 必须使用用户授权的打开路径，输出使用 PathGrant。
3. 默认脚本执行 dry-run，显示 diagnostics/diff；真实 apply 必须进入 M0 的批准 UI 和 M2 的 command stack。
4. 每次脚本运行写可选审计记录，标明 client=`script`，但不保存脚本中的 secret。

### M6.2 FCPXML / OTIO 兼容策略

1. TAEP/`EditPlan` 始终是内部 canonical editing intent；FCPXML/OTIO 是导入导出 adapter，不反向成为内部真相。
2. 为每个 adapter 定义 capability/loss report：滤镜、转场、字幕、retime、复合片段、speed ramp、marker、音频混音分别标为 round-trip/lossy/unsupported。
3. 导出前运行 adapter preflight；有损项目必须在 UI 和 audit 中可见。
4. 导入外部 XML/OTIO 走 canonical command/transaction，不建立绕过 revision/undo 的第二条写入路径。
5. 后续新增滤镜/插件时只需向 capability registry 增加 adapter mapping；缺 mapping 时保留 opaque metadata 或明确 warning。

---

## M7：质量门禁、测试矩阵与灰度发布（P0/P1）

### M7.1 分层测试

| 层级 | 必测内容 |
| --- | --- |
| Unit | approval scope、safe path、DTO builder、registry、decision constraint、payload migration |
| Contract | ToolFacade JSON schema、无可信 token 输入、capability truth table、TAEP 版本协商 |
| Transaction | preview=commit、stale、顺序 operation、单 undo、redo、失败原子性 |
| Integration | MainWindow composition root、Preferences profile、工程保存/重开、Dock 页面切换 |
| E2E | 输入需求 → Brief approval → variants → diff → user approval → commit → undo/redo → save/reopen |
| Security | token 伪造/重放/过期、跨 project、路径穿越、symlink、prompt injection、日志 secret 扫描 |
| Performance | UI 主线程无长任务、取消延迟、并发限制、4K 工程预览/导出基线 |

### M7.2 必须新增的回归用例

1. 模型传入任意 approval 字符串不能 commit。
2. grant 只能用于被显示过的同 hash plan，且一次性消耗。
3. `add_clip → set_effect → add_transition` 在顺序模拟中成功，非法引用精确定位失败 op。
4. manifest 不会列出没有 compiler 的 operation。
5. 对 set_speed、effect add/remove、插件参数 patch 都能 preview/commit/undo/redo。
6. 锁定范围不在普通候选中时，重规划仍保留；被拒绝处理不会复现。
7. AI payload 保存/重开/迁移/unknown field 保留；工程中不出现 API key 或绝对路径。
8. 切换工程或关闭窗口时，运行 job 不会提交到错误工程。
9. AI Dock 在 Edit 可见，在 Color/Library/Deliver 不泄漏，布局可保存恢复。

### M7.3 灰度策略

1. `ai_edit.preview_only`：默认开启，允许分析/brief/variant/diff，不允许 commit。
2. `ai_edit.commit_beta`：仅开发/内部白名单启用，需要全量 M0/M2 测试通过。
3. `ai_edit.provider_cloud`：独立于 commit beta，受数据策略和 profile capability 控制。
4. 每个 feature flag 写入 capability manifest，模型永远以运行时 manifest 为准。
5. 提供“一键导出审计包”给 bug report，但先执行敏感信息扫描和用户预览。

---

## 3. 每个里程碑的完成定义（Definition of Done）

一个里程碑只有同时满足以下条件才可标记完成：

1. 有明确 public contract，类型化而非靠私有字段/`hasattr`。
2. 正常、失败、取消、过期、跨工程、重开工程均有测试。
3. 没有 broad `except: pass` 隐藏业务失败。
4. UI 状态可解释：用户能知道是无工程、无 profile、等待确认、stale、被拒绝、任务取消还是 provider 失败。
5. 不引入密钥泄露、路径越权、UI 线程阻塞或新的绕过 command stack 写入路径。
6. 运行相关测试集、`git diff --check` 通过；新增 protocol/diagnostic/迁移有版本说明。
7. 通过一次真实手工验收：新工程、已有复杂工程、缺素材工程、只读工程各至少一例。

## 4. 第一轮实施建议（可直接拆成任务）

建议下一轮只做 M0 + M1 的 preview 闭环，拆分为以下 8 个可审查 PR/任务：

1. ApprovalService + token/replay/expiry contract tests。
2. ToolFacade transport JSON + remove model-supplied approval token。
3. SafePathPolicy + export PathGrant/preflight separation。
4. `AiEditHostBridge` + MainWindow composition root。
5. Preferences `edit.ai_editing` 注册、Profile config persistence、keychain adapter interface。
6. Request composer + Brief explicit approval，删除自动批准与 silent exception。
7. `PlanRepository` + 真实 `validate_selected()` / diff UI（仍 preview-only）。
8. MainWindow E2E 测试：开工程 → 输入 → Brief → variant → diff，确认不会实际写入。

完成以上 8 项后，再进入 M2；不要在 M2 之前通过“临时直接改 model”让 Apply 按钮可用。
