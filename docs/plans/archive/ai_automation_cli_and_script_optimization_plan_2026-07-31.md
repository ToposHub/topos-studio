# Topos AI 自动化命令行与脚本优化实施计划（2026-07-31）

## 0. 结论

**需要完善，而且应当把它作为 AI 剪辑落地前的基础设施。**

现有 Script Panel 适合熟悉 Topos 的本机用户写 Python 调色脚本：
`ScriptEngine` 在后台 `QThread` 执行 `exec()`，并把调用桥接回 UI 主线程；
`ToposAPI` 的公开能力集中在图层、调色、LUT、曲线、预设和图像批处理。它不是
稳定的无界面自动化 API，也不能安全地直接暴露给外部大模型。

未来应新增一个独立的、版本化的 **Topos Automation Protocol（TAP）**：

```text
AI / CI / 终端 / Script Panel / 未来 MCP
                 │
       Topos Automation CLI（toposctl）
                 │  JSON request / JSON result
       Automation Service（唯一应用服务入口）
          ├── TAEP EditSession / TransactionService
          ├── Media / Analysis / Project / Export services
          ├── Approval、Path Grant、Audit、Job Scheduler
          └── Interchange adapters（FCPXML / OTIO）
                 │
     既有 CommandStack、Project IO、Render/Export pipeline
```

这里的关键不是“让 AI 能在终端执行很多命令”，而是让所有调用者共享**同一份可验证、
可撤销、可审计的领域操作**。TAEP JSON 仍是 AI 剪辑意图与批量编辑的 canonical
格式；FCPXML/OTIO 只是交换适配器；命令行只是调用该服务层的客户端。

**绝不把以下能力开放给模型：** 任意 Python `exec`、Shell、任意文件读取、鼠标点击、
UI 自动化、OpenGL/GLSL 注入，或未确认的文件覆写。

---

## 1. 当前基线与需要补齐的缺口

### 1.1 已有基础，可直接复用

| 现有资产 | 位置 | 未来 TAP 中的定位 |
| --- | --- | --- |
| 调色脚本执行器 | `src/features/scripting/engine.py` | 保留为「本机受信任的 Legacy Color Script」运行时，不作为 AI 工具入口。 |
| 调色 Python facade | `src/features/scripting/api.py` | 维持兼容；逐步迁移其可复用能力到应用服务，不让新编辑功能继续堆入该大类。 |
| 编辑命令与撤销栈 | `src/features/edit/core/commands.py` | 所有 TAP 写操作的最终唯一落点。 |
| TAEP / EditSession | `src/features/edit/ai_editing/` | 编辑 automation 的声明式请求、validate、preview、commit 合同。 |
| `ToolFacade` | `src/features/edit/ai_editing/tool_facade.py` | 未来 MCP/IPC adapter 的语义入口，但先完成安全、JSON transport 与真实命令栈接线。 |
| 项目 revision / `.topos` IO | `src/shared/project/`、`src/features/edit/core/edit_project_io.py` | 乐观锁、持久化和跨进程互斥的依据。 |
| 导出器 | `src/features/edit/core/timeline_export.py` | 由 Job Scheduler 调度，CLI 不直接调用编码器。 |

### 1.2 当前不应直接拿来给 AI 用的部分

1. `ScriptEngine` 会执行任意 Python；即使在后台线程，脚本仍可能调用本机文件、网络或
   UI 相关代码，因此只能由用户本机明确运行的受信任脚本使用。
2. 当前主程序 CLI 仅有 `--product` 和 `--test-startup`，没有稳定的 project、timeline、
   export 或 job 命令面。
3. `EditSession`、`ToolFacade` 已提供方向正确的雏形，但必须先完成既有稳定化计划中的
   Approval、真实 `CommandStack`、顺序预览模拟、Path Grant 和 project persistence。
   在此之前，任何 `apply`/`export` CLI 都只能是 preview-only。
4. 用终端启动一个新的进程去修改正在 GUI 中打开的同一工程，会造成 revision 竞争、媒体
   缓存冲突和 UI 不刷新。必须先定义“离线工程模式”和“已连接 GUI 模式”的互斥规则。

---

## 2. 不变量与产品边界

以下规则适用于 CLI、Script Panel、未来 MCP、本地 HTTP/IPC 及 AI 对话面板：

1. **唯一写入路径：** 所有项目/时间线变更必须是
   `request → schema/domain validation → sequential preview → approval → canonical command → CommandStack → revision → persist/audit`。
   不允许 CLI 直接改 `.topos` JSON、直接改 dataclass 或绕过 undo。
2. **默认只读：** 未给 `--commit` 的命令只能 inspect、search、validate 或 preview。导出、
   覆盖、删除、重连、提交均是显式高风险操作。
3. **模型不可自我授权：** LLM 传入的 token、`--yes`、文件路径或“用户已同意”文本都不能
   视为批准。批准必须由宿主 UI、受信任的本机交互或受限的 automation profile 签发。
4. **revision 是前置条件：** 每次写请求携带 `expected_project_revision`；当前 revision 不符
   时返回 `STALE_REVISION` 和新的 snapshot 摘要，绝不静默重放。
5. **一次 invocation，一个原子 undo group：** 成功时产生一个可读 label、一个 audit ID、一个
   新 revision；任一 operation 失败则不留下半套时间线。
6. **文件路径以授权对象表示：** 输入限于用户允许的 source roots 或已导入 asset ID；输出必须
   由 `PathGrant` 指向，默认不允许覆写、符号链接逃逸和父目录遍历。
7. **JSON 是 transport，TAEP 是编辑意图：** 命令行参数不承担复杂时间线结构；复杂写入通过
   文件或 stdin 传入版本化 TAEP JSON，XML 不成为内部真相。
8. **UI 不做业务解析：** UI 只负责请求、显示 diff、批准、订阅 job；CLI/MCP/UI 必须调用同一
   `AutomationService`，不能各自拼 command。
9. **长任务异步：** 分析、代理、预览、导出只创建 Job 并返回 job ID；进度/取消/产物均经过
   Job Scheduler，不能阻塞 Qt 主线程或 CLI 的协议输出。
10. **可演进：** 每个命令、能力、preset、effect、adapter 和 schema 都有稳定 ID、版本及
    deprecated 策略；运行时 Manifest 是唯一真相，不让模型背写死的命令清单。

---

## 3. 目标架构与运行模式

### 3.1 四层设计

```text
Layer 1  transport
  toposctl CLI、Script Panel action、MCP adapter、可选 local IPC client

Layer 2  automation contract
  AutomationRequest / AutomationResult、capability manifest、error code、JSON schema

Layer 3  application services
  ProjectService、AssetService、EditSession、AnalysisService、ExportService、InterchangeService
  ApprovalService、SafePathPolicy、JobScheduler、AuditStore

Layer 4  domain and rendering
  canonical project/timeline、CommandStack、effect registry、project IO、render/export
```

`Layer 1` 永远不得 import Qt widget、timeline widget、OpenGL context 或 command internals。
`Layer 3` 使用类型化 Protocol 与 application state；`Layer 4` 不依赖 CLI、MCP 或模型 provider。

### 3.2 两种执行模式（首版只做前两种）

| 模式 | 典型调用 | 允许内容 | 并发/保存规则 |
| --- | --- | --- | --- |
| `offline` | CI、无人 GUI 的批处理、另一个 AI agent | inspect、validate、TAEP preview；完成授权后可 commit/export | 对工程取得锁；从磁盘加载 immutable snapshot；成功后原子写回。 |
| `connected` | Topos GUI 已打开，用户在终端或 AI 面板请求 | 全部 UI 可见范围内的操作 | CLI 通过本机 IPC 请求 GUI host；GUI 是唯一持有 CommandStack、媒体池和保存权的进程。 |
| `interactive` | 用户在 Script Panel 运行脚本 | 旧调色脚本与新增 plan script | 仍在应用内执行；AI 生成脚本必须先展示权限与 diff。 |
| `remote`（后续） | 云端编排服务 | 仅经本机 MCP/IPC 的受限 tool calling | 不开放公网监听；不让远端拿到任意路径、密钥或 Python 执行权。 |

首版不要同时实现 socket server、HTTP server、MCP server 和 daemon。先抽出无 UI 的
`AutomationService`，再实现 `toposctl` 的 offline client；connected IPC 只在 GUI 的真实
composition root 和 approval/undo 已稳定后加入。MCP 最后只做一个薄 adapter。

### 3.3 冲突与锁策略

1. 离线请求以 `.topos` 的规范化绝对路径计算 lock key，使用专用 lock 文件或平台锁；锁内记录
   `project_id`、PID、host、开始时间和只读/写入模式，异常退出后可安全恢复。
2. 同一工程被 GUI 打开时，offline 写入命令拒绝并提示改用 `--connect auto`；offline read 可以
   读取已保存版本，但结果标记 `possibly_stale=true`。
3. connected 请求由 GUI host 串行化，并在预览与 commit 前分别检查 revision；Job 使用提交时的
   immutable project snapshot，不能读取之后改变的 UI 状态。
4. 成功写入后更新 revision、写 audit、刷新 GUI；失败/取消不改变 revision。

---

## 4. 统一 Automation Contract

### 4.1 公共请求与结果

所有 transport 最终转换为以下类型（字段名在实现中可使用 dataclass/Pydantic，但 JSON 形状
必须由 schema 固定）：

```json
{
  "protocol": "topos.automation/v1",
  "request_id": "uuid",
  "client": {"kind": "cli", "name": "toposctl", "version": "0.1.0"},
  "project": {"ref": "project-path-or-connected-id", "expected_revision": 42},
  "operation": "edit.plan.apply",
  "mode": "preview",
  "payload": {},
  "idempotency_key": "uuid-or-content-hash"
}
```

```json
{
  "protocol": "topos.automation/v1",
  "request_id": "uuid",
  "ok": true,
  "status": "PREVIEW_READY",
  "project": {"project_id": "...", "revision": 42},
  "result": {},
  "diagnostics": [],
  "audit_id": "optional-uuid"
}
```

必填结果字段：`ok`、稳定 `status`、`diagnostics[]`。诊断至少有
`code`、`severity`、`message`、`operation_index`（如适用）、`entity_id`、`hint`；禁止只返回
Python traceback 或本地绝对路径。

### 4.2 固定错误码与进程退出码

| 结果状态 / 诊断 code | CLI exit code | 含义与调用者动作 |
| --- | ---: | --- |
| `OK` / `PREVIEW_READY` / `JOB_QUEUED` | 0 | 请求完成；从 JSON 读取 result。 |
| `VALIDATION_FAILED` | 2 | TAEP/schema/领域 preflight 失败；修正 plan 后重试。 |
| `APPROVAL_REQUIRED` | 3 | 已有 diff，但未获可信批准；等待 GUI/用户批准。 |
| `STALE_REVISION` / `PROJECT_LOCKED` | 4 | 不可安全提交；重新取 snapshot 或改用 connected mode。 |
| `PATH_DENIED` / `PERMISSION_DENIED` | 5 | 路径、scope 或 automation profile 不允许；不得绕过。 |
| `NOT_SUPPORTED` | 6 | runtime manifest 不支持该 operation/preset/adapter。 |
| `JOB_FAILED` / `INTERNAL_ERROR` | 10 | 可重试的运行失败；JSON 带 correlation/audit ID。 |
| 命令行用法错误 | 64 | 参数解析失败；stderr 显示简短帮助。 |

stdout 只输出一个 JSON result（`--output json` 为默认，`--output text` 仅供人读）；日志、进度和
warning 输出 stderr。这样 shell、CI、MCP bridge 和 AI 都不需要解析人类字符串。

### 4.3 Capability Manifest

新增 `AutomationCapabilityRegistry`，由 feature flag、effect/transition registry、导出 preset、
adapter、当前执行模式共同构建运行时 Manifest。每个 capability 至少声明：

```json
{
  "id": "edit.plan.apply",
  "version": "1.0",
  "modes": ["preview", "commit"],
  "permission": "write_with_approval",
  "input_schema_ref": "topos://schema/taep-edit-plan/v1",
  "availability": "enabled",
  "limitations": ["no nested sequences in v1"]
}
```

Manifest、TAEP validator、operation builder、CLI help、MCP tool schema 和 Script Panel 自动补全必须
从同一个 registry 派生。若 compiler 没有实现某 operation，Manifest 就不得宣称支持它。

### 4.4 Idempotency、revision 与审计

1. `preview` 用 `plan_hash + base_revision` 缓存，返回 `preview_id`、diff hash 和过期时间。
2. `commit` 必须引用同一份 immutable plan/preview，携带匹配的 base revision；同一
   `idempotency_key` 重复提交仅返回第一次成功的 result，绝不重复添加素材。
3. 可信 `ApprovalGrant` 绑定 `project_id`、`timeline_id`、`plan_hash`、`diff_hash`、权限 scope、
   单次使用标记和过期时间；它由 host 保存，模型/plan/命令行参数无法自行伪造。
4. Audit 记录请求摘要、client kind、操作者、批准方式、前后 revision、plan hash、job/artifact
   reference、diagnostic 和时间，不保存模型 API key、原始 prompt 中的 secret 或机器绝对路径。

---

## 5. `toposctl` 命令行规格

### 5.1 安装与根命令

在 `pyproject.toml` 增加一个独立入口：

```toml
[project.scripts]
toposctl = "src.features.automation.cli:main"
```

不要把自动化子命令塞进 `src/main.py` 的 GUI 启动参数。GUI 和 CLI 有不同的 Qt/资源生命周期；
`toposctl` 应尽可能不创建 `QApplication`，只有 connected client 或必要的 offscreen render host
才按需初始化。

根命令通用选项：

```text
toposctl [--output json|text] [--project PATH|--connect auto|SOCKET]
         [--expected-revision N] [--timeout SECONDS] [--request-id UUID]
         [--idempotency-key KEY] [--log-level LEVEL] <group> <command> ...
```

- 默认 `--output json`、默认只读/preview、默认 timeout 有上限。
- 复杂 JSON 使用 `--input FILE` 或 `--input -`（stdin），不使用 shell-escaped 大字符串。
- `--project` 只能指定一个 `.topos` 文件；不接受目录、glob 或未解析的 URL。
- `--connect auto` 只连接当前用户的本机 socket；不自动把 project 上传至网络。
- 所有写命令都需要 `--expected-revision`，不可用“最新版本自动提交”替代。

### 5.2 P0：只读与诊断命令（最先交付）

| 命令 | 输入 | 结果 | 备注 |
| --- | --- | --- | --- |
| `toposctl version` | 无 | CLI/protocol/schema 版本 | 用于 client compatibility check。 |
| `toposctl capabilities show` | project/mode | 动态 Manifest | AI 每次会话先调用。 |
| `toposctl project inspect` | `--detail summary|structure|full` | 脱敏 ProjectSnapshot | 支持 `--timeline ID`、分页，不回传大媒体或未过滤文本。 |
| `toposctl project verify` | project | schema、媒体、plugin、revision 健康报告 | 不写入；可供 CI。 |
| `toposctl assets list` | filters/paging | asset metadata | 只返回 ID、相对/opaque reference 和必要技术元数据。 |
| `toposctl assets search` | text/kind/time range | semantic segment 摘要 | OCR、转录、文件名全部标记 untrusted data。 |
| `toposctl assets analysis get` | `--asset ID` | 已有 analysis/provenance | 未分析时返回可执行的 analysis proposal，不隐式长跑。 |
| `toposctl timeline inspect` | timeline ID / range | track、clip、transition、caption 摘要 | timeline 时间一律 rational ticks，不用浮点秒。 |
| `toposctl jobs get|list|logs` | job ID | job 结构化状态 | logs 做脱敏和大小限制。 |

P0 不包含 `project create`、素材导入或改动项目。先让模型和人能够稳定“读懂”当前工程，
并用 JSON contract/Manifest 验证 transport 与错误语义。

### 5.3 P1：方案、预览与批准请求（仍不直接改工程）

| 命令 | 输入 | 结果 | 默认/安全规则 |
| --- | --- | --- | --- |
| `toposctl plan validate` | `--input PLAN.taep.json` | ValidationReport | 只检验 schema、capability、引用、project/revision。 |
| `toposctl plan preview` | TAEP input | diff、成本预估、`preview_id` | 使用顺序模拟；绝不写 CommandStack。 |
| `toposctl plan explain` | plan/preview ID | 逐 operation 的自然语言摘要 | 供用户确认，不能取代 machine diagnostics。 |
| `toposctl approval request` | preview ID | `approval_request_id` | connected 模式显示 GUI Diff；offline interactive 模式显示明晰摘要。 |
| `toposctl analysis request` | asset IDs / analysis profile | job ID | expensive scope；不会阻塞当前进程。 |
| `toposctl interchange inspect` | `--format fcpxml|otio`、用户授权输入 | inspection + loss report | 只解析到候选 canonical state，不导入。 |

`plan preview` 必须验证整个 operation 序列。例如 `add_clip → add_effect → add_transition`
在模拟状态中依序成立才可通过；不能每个 operation 都只对 base timeline 校验。

### 5.4 P2：受控写入命令

| 命令 | 额外前置条件 | 成功结果 |
| --- | --- | --- |
| `toposctl plan apply --preview PREVIEW_ID` | matching `ApprovalGrant`、expected revision、project write lock | 新 revision、单一 undo label、audit ID。 |
| `toposctl project save` | GUI owner 或 offline lock | 保存 result 与 archive warning。 |
| `toposctl assets import` | 用户选定 source root grant；不接受任意路径 | asset IDs、probe report、可选 proxy/analysis job。 |
| `toposctl assets relink` | 明确 mapping file + approval | remap report 与 revision。 |
| `toposctl interchange import apply` | 已 inspect 的候选 + approval | canonical commands、loss report、undo group。 |
| `toposctl decisions accept|reject|lock` | decision revision + approval scope | 持久化 preference/constraint，供下一轮规划。 |

所有写命令都只对**已保存的 immutable request**工作，不能通过临时命令行 options 自动生成
不透明的写入。特别是 `assets import` 只允许由用户选取/配置的 media roots；大模型可以提出
候选，不能扫描用户硬盘。

### 5.5 P3：预览、导出和交付命令

| 命令 | 输入 | 结果 | 约束 |
| --- | --- | --- | --- |
| `toposctl render preview` | committed revision、timeline/range、preview preset | job ID | 低分辨率/短区间，产物为受限 artifact reference。 |
| `toposctl export preflight` | deliver preset + output proposal | compatibility/loss/path report | 不创建文件。 |
| `toposctl export request` | preflight ID + PathGrant + approval | job ID | 不读取可变 UI；导出 committed snapshot。 |
| `toposctl jobs wait` | job ID、timeout | final JobResult | 可因 timeout 返回仍运行；不会取消 job。 |
| `toposctl jobs cancel` | job ID + ownership scope | cancellation status | 明确区分 queued、cancelling、completed。 |
| `toposctl interchange export` | committed revision、adapter options、PathGrant | job/result + InterchangeReport | Topos 扩展默认关闭。 |

导出设置不能直接写 FFmpeg/encoder 私有 option。必须由 Deliver preset registry 提供稳定 preset ID，
并在 Manifest 中标注 codec、位深、色彩空间、音频和 platform availability。这样 AI 可请求
`prores_422hq_master`，但没有能力绕过现有的 10-bit/HDR preflight。

### 5.6 首版非目标（明确不做）

- `toposctl python run`、`shell`、`ui click`、`render glsl`、任意 URL 下载；
- 通过 CLI 直接编辑单个 clip 的内部字段；必须生成 TAEP operation 或调用已注册的高层服务；
- 未经批准的批量删除、覆盖导出、替换媒体、文件系统遍历；
- 多用户协同写入、网络 daemon、云端项目存储；
- 将所有素材分析结果塞进 stdout，或让模型一次拿到整个工程/所有原始媒体。

---

## 6. Script Panel 的演进方案

### 6.1 保留兼容，但划清两类脚本

| 类型 | 入口 | 信任级别 | 允许能力 | 未来定位 |
| --- | --- | --- | --- | --- |
| Legacy Color Script | 当前 `.py` 编辑器和 `ToposAPI` | 用户本机受信任 | 既有图层/调色/批处理 API | 保持兼容，维护期不扩展新的编辑对象。 |
| Plan Script | Script Panel 新增“Automation”页签 / `.topos.py` | 用户明确运行；AI 生成默认未受信任 | 调用 `AutomationClient` / `EditSession` 的稳定方法 | 新功能唯一推荐脚本面。 |
| TAEP Plan | `.taep.json` | 数据，不可执行代码 | validate/preview/apply/export proposal | AI、CLI、MCP 与脚本共用。 |

AI 生成的内容优先是 `.taep.json`，而非 Python。只有用户需要循环、条件分支、批量规则等
程序性逻辑时才生成 Plan Script，并在运行前显示：来源、hash、所需 capability、涉及的 asset/
project/output 范围、dry-run diff 和审计开关。

### 6.2 新 `AutomationClient` Python API（目标形状）

```python
from topos.automation import AutomationClient, PlanFile

client = AutomationClient.current()
plan = PlanFile.open_with_user_grant("rough_cut.taep.json")
preview = client.plan.preview(plan, expected_revision=42)
preview.raise_if_errors()
client.ui.show_diff(preview)                 # 仅 interactive host 提供
result = client.plan.apply_after_approval(preview)
job = client.export.request_from_preset(
    "prores_422hq_master", source_revision=result.revision
)
```

API 规则：

1. 默认 `preview`；`apply_after_approval` 只接受 host 生成的 grant，不能填字符串绕过。
2. 文件打开/保存使用 `InputGrant`/`PathGrant`；`PlanFile.open` 不能无限制读取路径。
3. 返回 typed result/diagnostic/job handle，不暴露 MainWindow、renderer、timeline model 或
   command 对象引用。
4. 任务只可 `status()`、`wait(timeout)`、`cancel()`；禁止脚本线程同步调用 GPU。
5. 每个 run 建立 automation session，默认一组 undo 和一条 audit；异常中止应回滚或明确显示
   未提交状态。

### 6.3 Script Panel UI 交付项

1. 加入模式切换：`Legacy Color Python`、`Automation Plan`、`Automation Python`，旧脚本自动以
   Legacy 模式打开，不做破坏性迁移。
2. `.taep.json` 打开后显示 schema version、target project/timeline、operation 摘要和 capability
   compatibility；提供 Validate、Preview、Show Diff、Request Approval、Apply 五个独立按钮。
3. Apply 在 `preview_id` 不存在、revision stale、批准过期或 diagnostics 有 error 时禁用；不以
   “运行”按钮直接执行。
4. 对 Automation Python 提供只读的 API reference、type stub、补全和 lint；AI 生成脚本显示
   `Untrusted` 徽标并要求用户勾选权限清单。
5. Console 输出采用结构化 event view：request ID、phase、job ID、progress、diagnostic；保留
   traceback 仅给本机调试，并提供“复制已脱敏报告”。
6. 停止按钮先取消 pending job/approval，再等待安全点；不通过粗暴杀线程破坏 CommandStack。

### 6.4 迁移规则

- 不修改已有 `ToposAPI` 方法语义，不把其 Python facade 当作长期 versioned CLI。
- 新的 timeline、字幕、转场、滤镜、音频、导出能力禁止新增到 `ToposAPI`；实现到 canonical
  command + AutomationService + TAEP builder 后，由 `AutomationClient` 间接使用。
- 为旧脚本记录弃用矩阵，先 warning、后给迁移向导；任何删除至少跨一个大版本并提供转换示例。

---

## 7. 具体代码结构与职责

建议新增模块，名称可微调但层次不可混合：

```text
src/features/automation/
├── __init__.py
├── models.py                 # AutomationRequest/Result、Diagnostic、Grant、session DTO
├── schema.py                 # TAP JSON Schema、version negotiation、migration
├── capabilities.py           # AutomationCapabilityRegistry（唯一能力来源）
├── service.py                # AutomationService：transport-neutral 路由
├── context.py                # Offline/Connected AutomationContext Protocol
├── policy.py                 # SafePathPolicy、input root、output、symlink、overwrite policy
├── approvals.py              # host-signed one-time ApprovalGrant；复用/收敛现有 ApprovalService
├── locks.py                  # offline project lock 和 owner metadata
├── audit.py                  # audit writer/redaction/export bundle
├── jobs.py                   # 薄 wrapper，接既有 JobRegistry/ExportService
├── cli.py                    # toposctl parser；只负责参数 → request → JSON result
├── client.py                 # Script Panel、future MCP 的 Python client
├── ipc/
│   ├── protocol.py           # length-prefixed local JSON-RPC / protocol negotiation
│   ├── server.py             # 仅 GUI host 启动
│   └── client.py
└── tests/

src/features/edit/ai_editing/
├── ... existing TAEP modules
├── operation_registry.py     # 由 Automation capabilities 共享的 canonical operation builder
└── plan_repository.py        # immutable plan/preview/approval binding

src/features/scripting/
├── api.py                    # 旧调色 API，仅兼容维护
├── engine.py                 # 旧受信任 Python runner
└── automation_bridge.py      # Script Panel 调用 AutomationClient 的小型 adapter

src/app/automation/
├── host_bridge.py            # 实现 Connected AutomationContext；唯一可接触 UI/CommandStack 的层
├── approval_presenter.py     # GUI diff / grant 签发
└── ipc_lifecycle.py          # 工程打开、切换、关闭时启动/停止本机 IPC

tests/automation/
├── test_cli_contract.py
├── test_capability_truth.py
├── test_preview_commit_atomicity.py
├── test_path_and_approval_security.py
├── test_offline_locking.py
├── test_connected_host_e2e.py
└── fixtures/
```

依赖方向必须是：`automation` 可以依赖 edit 的 public service/DTO，不能 import UI；
`src/app/automation` 依赖 automation 与 app controller；任何 Qt/ModernGL import 不得进入
`automation.models/schema/policy/cli`。

---

## 8. 与 TAEP、FCPXML、OTIO 的关系

### 8.1 三者各司其职

| 格式/接口 | 目的 | 是否内部真相 | 是否直接由模型编写 |
| --- | --- | --- | --- |
| TAEP JSON | Topos 的编辑意图、AI plan、preview/commit 输入 | 是（编辑意图层） | 是，但必须按 runtime schema/Manifest 验证。 |
| TAP / `toposctl` JSON | 调用服务与接收结果 | 是（automation transport） | 通过受限 tools/CLI client。 |
| FCPXML | 与 Final Cut 生态交换 | 否，adapter | 可以导入/导出，但不要叫“通用 XML”。 |
| OTIO | Python/DCC pipeline 时间线交换 | 否，adapter | 可选第二阶段。 |
| Python Script | 用户的程序性自动化 | 否 | 仅用户审阅后运行；不作为远程模型权限通道。 |

### 8.2 适配器命令

在 AutomationService 提供统一语义：

```text
interchange.inspect → candidate canonical state + loss report
interchange.preview-import → diff + asset relink proposal
interchange.apply-import → canonical command transaction + undo
interchange.preflight-export → supported/lossy/dropped report
interchange.export → authorized artifact/job
```

任何新 effect、transition、字幕样式、音频功能先注册 canonical capability；adapter mapping 是其
额外属性。没有 mapping 时输出明确 `unsupported/lossy` report，可保留受限 opaque metadata，
但不能静默丢失或伪称无损。

### 8.3 XML 安全

FCPXML 导入器必须禁用外部实体（XXE）、限制节点数/嵌套深度/总大小、拒绝网络 URI，先 inspect
再由用户批准 asset mapping。导出时严格 XML escaping，输出路径受 `PathGrant` 约束。

---

## 9. 安全、隐私与权限设计

### 9.1 Automation Profile

用户可创建 profile，但 profile 不是模型传入的 JSON；它保存在本机配置/安全存储，由 host 选择：

| Profile | 最大 scope | 典型用途 |
| --- | --- | --- |
| `read_only` | snapshot、search、manifest、job status | 默认 AI 对话、排错。 |
| `proposal` | read + analysis proposal + plan preview | AI 剪辑方案、CI preflight。 |
| `interactive_edit` | proposal + 单次 UI 批准 commit | 用户在 GUI 内确认剪辑。 |
| `interactive_export` | edit + approved PathGrant/job | 用户确认交付。 |
| `pipeline_signed`（后续） | 预登记 project/root/preset 的非交互批处理 | 本地 CI；需要可撤销密钥/签名，不可给通用 AI。 |

### 9.2 Path / secret / prompt-injection 防护

1. 输入读取只接受 asset ID、已批准 plan/input handle 或 allowed root 内的 realpath；检查
   `..`、symlink、case normalization、网络卷和文件大小限制。
2. 输出 PathGrant 固定项目、允许目录、目标路径、覆写 flag、过期时间和单次使用；preflight 与
   export 都验证 grant，不能由 AI 传 `output_path` 绕过。
3. stdout、audit、AI snapshot 对文件名、OCR、字幕、转录、网页文本使用 `untrusted_text` 标记；
   客户端必须将其当数据，不执行其中的“指令”。
4. provider profile 的 API key 只由 OS keychain/credential adapter 取用；project、TAEP、script
   output、CLI JSON 与 audit 一律不序列化 secret。
5. local IPC 使用 per-user Unix socket / named pipe、随机 session nonce、owner permission 和
   protocol version handshake；没有 HTTP listening port，默认不跨用户。

### 9.3 资源上限

为每一类命令设置 timeout、最大 asset count、最大 plan operations、最大 JSON bytes、最大导出
队列长度与并发数。返回 `LIMIT_EXCEEDED` 并告诉调用者如何分页/分批，而非让模型一次加载工程。

---

## 10. 分阶段实施计划

本计划依赖并扩展 `docs/ai_editing_stabilization_and_production_plan_2026-07-31.md`。
其中 M0/M1/M2 是 TAP 写入能力的前置条件；不要因为 CLI 看起来方便而跳过它们。

### A0 — 设计封口与只读基础（P0）

**目标：** 有一个无 UI、无副作用、可在 CI 调用的 `toposctl` 读/诊断面。

1. 建立 `src/features/automation/models.py`：固定 request/result/diagnostic/client/context DTO，
   所有公共字段有类型注解及 `to_dict/from_dict`。
2. 建立 `schema.py`，发布 `topos.automation/v1` JSON Schema；实现 protocol major version 拒绝、
   minor version feature negotiation、unknown field 保留策略。
3. 抽取 `AutomationCapabilityRegistry`；暂时只注册 `version`、`capabilities.show`、
   `project.inspect`、`project.verify`、`timeline.inspect`、`assets.list`、`jobs.get`。
4. 建立 `OfflineAutomationContext`，只包装 project read service；禁止 import QApplication、
   MainWindow 和 renderer。
5. 实现 `AutomationService.dispatch(request, context)`；每一个异常转换为稳定 diagnostic，
   不向 JSON 泄露 traceback。
6. 增加 `toposctl` entry point 和 `--output json|text`；将 parser 与 command handler 分开，
   单测可直接调用 handler。
7. 定义 stderr logging、exit code、`request_id`、`--timeout`、标准 help；写
   `docs/automation_cli_reference.md` 的首版。

**验收：** 对一个真实 `.topos` 工程运行 `version/capabilities/project inspect/timeline inspect`
均返回可被 `json.loads` 解析的单一 JSON；错误 project、超前 schema、空 timeline、缺媒体都给
稳定 error code；在无显示环境的 pytest 中不创建 Qt/GL context。

### A1 — TAEP 预览与能力单一真相（P0）

**前置：** 先完成 AI 稳定化计划的 M0.3、M2.3、M3.1、M3.3：真实 JSON transport、
顺序模拟、operation registry、无 UI preview。

1. 把现有 `CapabilityManifest` 迁移为 automation registry 的编辑子域；禁止 manifest 与
   compiler 两张独立白名单。
2. 完成每个首批 operation 的 DTO builder（clip、track、transform、crop、speed、effect、
   transition、subtitle、marker）；禁止把任意 dict 塞入 `UpdateClipParamCommand`。
3. 新建 `PlanRepository`：保存 immutable plan、hash、target project/timeline、base revision、
   preview/diff、创建者和过期时间。
4. 实现 `toposctl plan validate`、`plan preview`、`plan explain`；plan 可以从 grant 的文件或
   stdin 读取，读取后存为 repository handle。
5. 对 result 中所有 untrusted 文本做可 JSON 序列化的 data wrapper，不能返回 Python
   `SanitizedText` 对象。
6. 加入 plan size/operation 数/单 operation payload 限制和精确 diagnostic location。

**验收：** `add_clip → set_effect → add_transition → add_subtitle` 能在预览中正确顺序模拟；
无 compiler 的 operation 不在 `capabilities show` 中；相同 plan 和 revision 得到稳定 diff hash；
没有一个 preview 改变 `.topos`、CommandStack 或 revision。

### A2 — Approval、路径、离线锁与原子 commit（P0）

**前置：** AI 稳定化 M0（ApprovalService、SafePathPolicy）和 M2.2（真实 CommandStack）。

1. 收敛现有/新增 `ApprovalService` 为唯一 grant issuer；grant 必须由 host 私有密钥或内存
   capability 保存，不能从 CLI、TAEP、MCP args 的字符串恢复为可信对象。
2. 实现 `SafePathPolicy`、`InputGrant`、`PathGrant`；覆盖 realpath、symlink、overwrite、
   path traversal、extension/preset policy、过期和一次性使用。
3. 实现 offline project lock 与 atomic save：先取得 lock、加载 revision、commit canonical command、
   append audit、临时文件 fsync/replace 保存、释放 lock；失败时原文件不变。
4. 实现 `toposctl approval request`（仅 interactive/connected）和
   `toposctl plan apply --preview`；模型传任意字符串、`--yes` 或旧 token 必须失败。
5. 将 commit 接入实际 project/timeline command stack，确保一次 CLI apply 在 GUI/工程层表现为
   一次 undo，undo/redo 后 project revision/audit 语义一致。
6. 在操作成功和失败两侧更新 AuditStore，并提供 `toposctl audit show --id` 的脱敏读取。

**验收：** token 伪造、重复使用、过期、跨工程、plan hash 不同、revision stale、路径穿越、
symlink 和覆写未批准全部被拒；成功 plan 只写一次、可 undo/redo、重开工程后仍正确；异常注入
不会留下半写入的项目。

### A3 — Connected GUI host 与 Script Panel 迁移（P1）

**前置：** A2 与 AI 稳定化 M1（`AiEditHostBridge`、真实 controller/UI composition root）。

1. 在 `src/app/automation/host_bridge.py` 实现 `ConnectedAutomationContext`：类型化获取当前
   project state/manifest/command stack，提交后刷新时间线、媒体池、renderer；不用 `getattr`
   猜 host 方法名。
2. 创建 per-user IPC lifecycle：工程打开后注册 connection id；切换/关闭工程时停止接收或取消
   与该工程绑定的 request/job；IPC 不得留跨工程悬挂 session。
3. 实现 CLI `--connect`：先 handshake，再读 Manifest/revision，所有请求显示在 GUI 的 automation
   activity/audit 面板中。
4. 扩展 Script Panel 的三模式 UI，优先实现 TAEP 文件的 validate/preview/show diff/request
   approval/apply；Legacy Python 行为不改。
5. AI 对话面板、Script Panel、CLI 共用同一个 `PlanRepository` 和 approval presenter；相同 plan
   在三个入口看到相同 preview/diff/approval 状态。
6. 按 AI 稳定化计划修复 Dock layout、Preferences profile persistence、Brief 明确审批，避免 CLI
   虽可用而 GUI 仍是空壳。

**验收：** GUI 打开工程时，`toposctl --connect auto plan preview` 的 diff 与面板一致；用户只在
GUI 批准一次，CLI 才能 apply；切换工程、关闭窗口、取消请求、IPC 断开均不会写错工程或泄漏 dock/job。

### A4 — Asset/analysis、导出和 Jobs（P1）

**前置：** AI 稳定化 M4/M5；尤其是 analysis scheduler 与 `.topos` AI payload persistence。

1. 把 media pool 的 import/relink/analysis 封装为 application services；先提供 list/search/get/request
   四类能力，后提供经批准的 import/relink。
2. 将 `JobRegistry` 升级为实际 scheduler：持久/可恢复的状态机、ownership、progress、cancel
   token、并发/显存限制、retry policy 和 structured failure。
3. 复用当前 high-bit export pipeline，但所有 export request 只引用 committed revision；在 preflight
   中报告 codec/pixel format/color metadata、素材离线、effect/transition export capability 和
   bit-depth 降级风险。
4. 实现 `render preview`、`export preflight/request`、`jobs get/list/wait/cancel`；输出只返回
   artifact ID 和经批准的相对位置，UI 决定是否展示 Finder/desktop 路径。
5. 将 analysis、brief、decision、variant、audit 的可保存 payload 迁入 `.topos` package，含
   `ai_schema_version`、迁移与 unknown field 保留，绝不含 provider secret。

**验收：** 请求分析/预览/导出不会阻塞 UI；用户可取消；重开工程后 job/history/AI decision 状态
可解释；10-bit ProRes 等 deliver preset 的 preflight 与实际导出 pixel format 一致；无 PathGrant
时不会创建输出文件。

### A5 — FCPXML / OTIO 与先进 pipeline（P2）

1. 实现 `InterchangeAdapter` Protocol 及 `fcpxml` inspect/import-preview/apply/export-preflight/export；
   把 loss report 作为 TAP 的一等 result。
2. 为支持子集建立 fixture：asset、gap、基础 clip in/out、track order、marker、简单字幕、精确
   dissolve；复杂 effect、speed ramp、compound clip、audio routing 一律先 warning/reject。
3. 在 capability registry 建立 adapter mapping；新增 effect/transition 时强制声明
   `round_trip/lossy/unsupported`。
4. OTIO 作为可选依赖/adapter，隔离其版本；不替代 canonical timeline，且不令其引入 GUI 依赖。
5. 最后才评估 `pipeline_signed` automation profile、MCP adapter 与多进程 render worker；每项均
   复用 TAP schema/registry/policy，不新开第二协议。

**验收：** 每个 adapter 都有 inspect、preview、apply、export 的安全边界与 round-trip fixture；
loss report 不能为空；恶意 XML/超大 XML/未匹配媒体有确定性失败或用户选择流程。

---

## 11. 测试与质量门

### 11.1 必需测试矩阵

| 类别 | 必测场景 |
| --- | --- |
| Schema/CLI contract | stdin/file JSON、stdout 单 JSON、exit code、unknown field、版本不兼容、text output。 |
| Capability truth | feature flag、缺 plugin、缺 compiler、adapter/preset 不支持时 Manifest、help、validator 一致。 |
| Transaction | 顺序 operation、preview=commit、原子失败、单 undo、redo、idempotent retry、stale revision。 |
| Security | token 伪造/重放/过期/跨工程、path traversal、symlink、overwrite、secret redaction、prompt injection。 |
| Offline | lock contention、崩溃恢复、原子保存、GUI 已打开时拒绝写入、只读 stale 标记。 |
| Connected | handshake、project switch、window close、IPC disconnect、UI refresh、批准复用/过期。 |
| Script Panel | legacy 兼容、AI plan preview、权限清单、取消、console 脱敏、不会在 UI 线程执行长任务。 |
| Jobs/export | queue、progress、cancel、失败重试、artifact authorization、预览/导出隔离、10-bit/HDR preflight。 |
| Interchange | XML 安全、asset relink、loss report、FCPXML/OTIO fixture round-trip。 |

### 11.2 强制手工验收

每个有写入能力的里程碑都至少验证：

1. 新建小工程：模型生成 TAEP → CLI preview → GUI diff 批准 → apply → undo/redo → save/reopen。
2. 真实复杂工程：多轨、字幕、effect、转场、缺素材、不同帧率，确认 diagnostics 精确且不静默降级。
3. 有 10-bit 素材的 export preflight 与实际 ProRes/HEVC 结果，检查 pixel format 与颜色元数据。
4. 路径、批准、stale revision、应用关闭、job 取消至少各做一次故障演练。

### 11.3 发布开关

| Flag | 默认 | 开启条件 |
| --- | --- | --- |
| `automation.read_only` | 开发可开 | A0 contract tests 通过。 |
| `automation.plan_preview` | 内部可开 | A1 顺序模拟与 capability tests 通过。 |
| `automation.commit_beta` | 关闭 | A2 安全/undo/atomicity 通过，且有 GUI approval。 |
| `automation.connected_ipc` | 关闭 | A3 跨工程/关闭 E2E 通过。 |
| `automation.export_beta` | 关闭 | A4 Job/path/preflight 通过。 |
| `automation.interchange_beta` | 关闭 | A5 fixture/loss/security 通过。 |

任何 flag 必须进入 Capability Manifest；关闭的能力不能被 CLI/MCP/AI 误报为可用。

---

## 12. 实施优先级、PR 切分与完成定义

建议按以下小 PR/任务推进，避免一次改动 CLI、协议、UI 和渲染：

1. **PR-1：TAP DTO + JSON schema + diagnostics + `toposctl version`**（无项目写入）。
2. **PR-2：只读 OfflineAutomationContext + inspect/capabilities/verify**。
3. **PR-3：共享 capability registry + TAEP validate/preview + PlanRepository**。
4. **PR-4：Approval/Path policy/lock/atomic commit**（依赖既有 AI M0/M2）。
5. **PR-5：真实 CommandStack + audit + `plan apply`/undo regression**。
6. **PR-6：GUI ConnectedHost + local IPC + project lifecycle tests**。
7. **PR-7：Script Panel Automation Plan 页签与 Legacy 兼容**。
8. **PR-8：asset/analysis/job/export commands + high-bit deliver preflight**。
9. **PR-9：FCPXML inspect/import/export，之后才是 OTIO/MCP/签名 pipeline**。

一个 PR/里程碑只有在以下全部满足时才可标记完成：

- 公共 API 有类型、schema、示例、错误码和兼容说明；
- CommandStack、revision、undo/redo、project save 与 audit 不出现第二写入路径；
- 正常、失败、取消、stale、跨工程、重开工程均有 focused test；
- `pytest` 对相关范围通过、`git diff --check` 通过，并完成至少一项手工验收；
- 不引入任意代码执行、secret 泄露、路径越权、UI 线程阻塞或无提示的数据丢失；
- 在 capability registry 与用户文档中准确声明限制，不提前宣传未实现的自动剪辑能力。

---

## 13. 第一轮可立即开始的清单

当前建议先做 A0，而不是立即开放导出/写入命令：

1. 创建 `src/features/automation/` 的纯 Python contract、schema、capability registry 和
   `AutomationService` 骨架。
2. 增加 `toposctl` entry point，交付 `version`、`capabilities show`、`project inspect`、
   `timeline inspect`、`project verify` 五个只读子命令。
3. 为这些命令建立 JSON fixture、exit-code tests 和无 Qt/GL import guard。
4. 同时按既有 AI 稳定化计划优先完成 M0/M1/M2；在真实 Approval、PathGrant、CommandStack、
   顺序 preview 准备完成前，把 `plan apply` 和 `export request` 明确保留为 disabled capability。
5. A0 验收后，再做 `plan validate/preview`；先让 AI 能看、能提案、能展示 diff，再让它请求
   用户批准，最后才允许写入和导出。

这样可以让项目在未来新增滤镜、转场、字幕、音频、效果和交换格式时，只需向 registry 和
operation/adaptor mapping 追加能力；AI、CLI、Script Panel 和 GUI 会同步获得正确的能力说明，
而不会变成四套彼此漂移的接口。
