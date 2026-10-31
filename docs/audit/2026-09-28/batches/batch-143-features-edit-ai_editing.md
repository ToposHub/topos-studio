# 批次报告：batch-143 — src/features/edit/ai_editing（8 文件，2,139 行）

编号区间：AUD-14300–AUD-14399。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
本批次均为纯逻辑 / 网络适配 / 策略层模块，无 Qt 控件、无 OpenGL 调用、无 GLSL——信号防递归与 GPU 资源生命周期两维在适用文件中标记为不适用或通过。

---

## src/features/edit/ai_editing/remote_transcript_provider.py (行数: 356)

### 发现

- [AUD-14300] [Major] 远端返回非数值 confidence 时以未净化的 ValueError/TypeError 逃逸，破坏错误净化契约
  - 位置: src/features/edit/ai_editing/remote_transcript_provider.py:290（words 在 :320 同样问题）
  - 证据:
    ```python
    out.append(TranscriptSegment(
        ...
        confidence=float(item.get("confidence", 0.9) or 0.9),
    ```
  - 影响: 第三方 ASR 返回 `{"confidence": "高"}` 或嵌套对象时，`float()` 抛 ValueError/TypeError。该异常不属于 `RemoteTranscriptError`，而模块契约（`RemoteTranscriptError` docstring："Sanitized remote ASR error; response bodies are never included"）保证调用方只见净化错误；原始异常会击穿调用方的错误处理分支，且异常消息可能携带远端响应内容。
  - 建议: 用带 try/except 的安全数值转换（复用 `_time_ms` 的模式）并回退默认值。
  - 置信度: 高

- [AUD-14301] [Major] ffprobe 失败时整体时长回退 1 秒，整段转录的 end_frame 被钳到 1s
  - 位置: src/features/edit/ai_editing/remote_transcript_provider.py:343-353
  - 证据:
    ```python
    except (OSError, subprocess.SubprocessError, ValueError):
        return 1000.0
    ```
    配合 :254-255：`duration_ms = self._duration_ms(path)` → `utterances = [{"start_time": 0, "end_time": duration_ms, "text": str(text)}]`
  - 影响: 远端只返回整段 `text`（无 utterances）且 ffprobe 缺失/超时/解析失败时，数分钟音频的转录 segment 末帧被映射到 1 秒处，字幕/转录时间码系统性错误——这是静默数据错误，用户无从察觉。
  - 建议: ffprobe 失败时返回 None 并让 `_segments_from_payload` 对整段映射显式报错（或要求 progress 层提示时长未知），不要默认 1000ms。
  - 置信度: 高

- [AUD-14302] [Major] `_time_ms` 把非法/None 时间静默映射为 0.0，外部数据校验 fail-open
  - 位置: src/features/edit/ai_editing/remote_transcript_provider.py:335-341（消费点 :268-274）
  - 证据:
    ```python
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    ```
    以及 :268-269：`start_ms = self._time_ms(item.get("start_time", item.get("start", cursor_ms)), ...)`——`dict.get` 的 default 只在 key 缺失时生效，`"start_time": null` 会返回 None 并落入 0.0 分支。
  - 影响: 远端返回 null/非法时间时片段跳回帧 0，而不是沿用 cursor_ms 或报错；`.clinerules` 铁律"外部数据须验证"被绕过，错误时间码静默入库。
  - 建议: 转换失败时回退 `cursor_ms`（start）/ `start_ms`（end）并收集为可观测告警，而非统一 0.0。
  - 置信度: 中

- [AUD-14303] [Suggestion] 100 MB 音频全量读入内存并二次拷贝（read_bytes + b"".join）
  - 位置: src/features/edit/ai_editing/remote_transcript_provider.py:152（volcengine）、:187 与 :192（openai multipart）
  - 证据:
    ```python
    request = urllib.request.Request(
        self._profile.endpoint,
        data=Path(path).read_bytes(),
    ```
    ```python
    data=b"".join(chunks),
    ```
  - 影响: 上传 100 MB 文件时 `read_bytes()` 一份 + `join` 一份，瞬时约 200 MB+ 峰值内存；`_open_json` 又 `read(_MAX_AUDIO_BYTES)` 一次响应。
  - 建议: 用文件流式生成 multipart body（分块写临时缓冲），或至少在 join 前预估并复用 bytearray。
  - 置信度: 高

- [AUD-14304] [Suggestion] `additions` 字段未校验 Mapping 即调用 `.get`
  - 位置: src/features/edit/ai_editing/remote_transcript_provider.py:280
  - 证据:
    ```python
    raw_speaker = item.get("speaker") or (item.get("additions") or {}).get("speaker")
    ```
  - 影响: 火山 v3 的 `additions` 若为 list/字符串（畸形 payload），`.get` 抛 AttributeError，不在 `RemoteTranscriptError` 净化范围内。
  - 建议: `isinstance(item.get("additions"), Mapping)` 守卫后再取 speaker。
  - 置信度: 中

- [AUD-14305] [Suggestion] `analyze()` 无取消钩子，长上传期间无法取消
  - 位置: src/features/edit/ai_editing/remote_transcript_provider.py:91-115
  - 证据:
    ```python
    def analyze(
        self, asset_id, fingerprint, *, fps, progress=None, options=None,
    ) -> tuple[AssetAnalysis, tuple[Any, ...]]:
    ```
    对照同目录 request_recovery.py:80-87 的 `call(..., cancel_check)` 模式。
  - 影响: 默认 `request_timeout_seconds=120.0`（provider_profiles.py:42），用户取消分析后上传仍持续至超时；progress 只报进度不能中断。
  - 建议: 增加可选 `cancel_check` 参数，在 `_request` 前与 `_open_json` 返回后检查并抛净化取消错误。
  - 置信度: 高

### 通过项
- 输入验证：上传前置校验完整（媒体上传授权 → endpoint → API Key → 文件存在 → 100MB 上限，:79-109）
- 异常与取消路径：HTTP 401/403/429/网络/JSON 解析错误均转为净化 `RemoteTranscriptError`，响应体不入消息（:213-227）
- 线程安全：provider 无共享可变状态（仅持有只读 profile 与每实例 `TimebaseService`），transcript_router 按其构建缓存
- 时间码换算：走 `TimebaseService.to_source_frame_range`（PTS×有理 timebase，绝不用秒×fps），帧区间端点钳制正确（:324-333）
- 密钥处理：secret 仅进请求头，不落日志/异常消息；multipart filename 经 basename + 去引号（:182）
- 类型注解完整

---

## src/features/edit/ai_editing/replan_policy.py (行数: 298)

### 发现

- [AUD-14306] [Major] `max_clarifications` 声明但从未执行，文档承诺的硬上限失效
  - 位置: src/features/edit/ai_editing/replan_policy.py:143（定义）；:178-270（`decide()` 全程未引用该限制）
  - 证据:
    ```python
    max_clarifications: int = 3
    ```
    而 `decide()` 的 docstring 承诺："Hard limits always win: once ``max_cycles`` / tool / clarification limits are hit, the decision is FINISH regardless of the trigger."（:191-193）。`TRIGGER_REVISION_CONFLICT` 分支（:224-229）无条件返回 REQUEST_CLARIFICATION 且不递增任何计数器；`self._clarifications` 字段（:159）从不被读写。
  - 影响: 修订冲突/失败兜底路径可无限次产出 REQUEST_CLARIFICATION，形成"反复问用户"死循环，违背模块"bounded limits / 防无界 replan 循环"的核心设计。当前生产代码无调用方（仅 tests/ai/test_agent_w0_kernel.py 引用），接入 agent loop 后风险落地。
  - 建议: 在两个产出 REQUEST_CLARIFICATION 的分支递增 `_clarifications` 并在达到 `max_clarifications` 时强制 FINISH。
  - 置信度: 高

- [AUD-14307] [Suggestion] `ReplanDecisionKind` 是空的 str 子类，不提供任何类型约束
  - 位置: src/features/edit/ai_editing/replan_policy.py:33-35
  - 证据:
    ```python
    class ReplanDecisionKind(str):
        """What the agent should do next after a critique."""
    ```
  - 影响: `ReplanDecision.kind: str` 实际未使用该类；它既非 Enum 也非 Literal，纯死代码且误导调用方以为存在类型层级。
  - 建议: 删除，或改为 `Literal` 类型别名供 `kind` 字段使用。
  - 置信度: 高

- [AUD-14308] [Suggestion] frozen dataclass 携带可变 `Mapping` 字段，实例不可哈希且外部可改内部状态
  - 位置: src/features/edit/ai_editing/replan_policy.py:103
  - 证据:
    ```python
    adjustments: Mapping[str, Any] = field(default_factory=dict)
    ```
  - 影响: `frozen=True` + `eq=True` 会基于字段生成 `__hash__`，dict 不可哈希 → `hash(decision)` 抛 TypeError；调用方持有 decision 后仍可改 `adjustments` 内部 dict，破坏不可变语义。
  - 建议: 存储 `tuple[tuple[str, Any], ...]` 或 `MappingProxyType`，`to_dict` 时再转 dict。
  - 置信度: 中

### 通过项
- 输入验证：`ReplanDecision.__post_init__` 校验 kind/trigger 白名单（:105-109）；`ReplanLimits` 边界合理
- 状态提交一致性：与 revision 冲突解耦为 trigger 信号（TRIGGER_REVISION_CONFLICT），不自行读 revision，边界清晰
- 异常路径：无 I/O、无外部调用，纯决策函数无隐藏异常路径
- 性能模式：重试→换工具→回滚→求澄清的升级阶梯有界（retries/tool_switches 均有上限）
- 类型注解完整

---

## src/features/edit/ai_editing/request_recovery.py (行数: 132)

### 发现

- [AUD-14309] [Suggestion] `retryable_codes` 包含 `LLM_RETRY_EXHAUSTED`，外层重试会放大底层已耗尽的重试
  - 位置: src/features/edit/ai_editing/request_recovery.py:33-39（消费点 :54、:96-97）
  - 证据:
    ```python
    retryable_codes: tuple[str, ...] = (
        "LLM_NETWORK_ERROR",
        "LLM_HTTP_ERROR",
        "LLM_RATE_LIMITED",
        "LLM_RETRY_EXHAUSTED",
        "LLM_STREAM_INTERRUPTED",
    )
    ```
  - 影响: 该 code 语义上是底层客户端重试已耗尽的终态错误；外层再 replay 会使实际请求次数 = 底层 N 次 ×（外层 max_retries+1），与"Bounded policy"的定位矛盾，并可能在限流场景加倍触发 429。
  - 建议: 从默认白名单移除该 code，或将外层计数与底层尝试数合并核算。
  - 置信度: 中

### 通过项
- 异常与取消路径：取消检查在每次尝试前与等待切片（50ms）中执行（:91-92, :120-129），取消即时生效且抛稳定 code 的 LLMError；不可重试错误直接上抛（:96-97）
- 策略校验：`__post_init__` 完整校验 max_retries/delay/jitter 边界（:41-49），退避上限钳制正确（:59-62）
- 状态可观测性：重试经 orchestrator.emit 发 RUN_REQUEST_RETRY 事件且带完整审计字段（:100-111），emit 内部有锁（agent_orchestrator.py:360）
- 线程安全：实例无跨调用可变状态
- 类型注解完整

---

## src/features/edit/ai_editing/rollout_metrics.py (行数: 92)

### 发现
（无）

### 通过项
- 通过（无发现）：纯不可变 dataclass + 门禁纯函数，无 I/O、无线程、无外部数据
- `RolloutGate.metrics` 默认值为 frozen dataclass 实例，共享安全（:65）
- 输入验证：门禁 fail-closed（tasks<=0 即拒绝，crashes/repeated_charges/copyright_incidents 任一>0 阻断，:74-81）；BETA/STABLE 阈值分层合理（:82-88）
- 除零防护：全部比率属性对 tasks==0 返回 0.0（:28-41）
- 类型注解完整

---

## src/features/edit/ai_editing/run_context.py (行数: 239)

### 发现

- [AUD-14310] [Major] `selected_asset_ids` 为空时 `asset_in_scope` 对所有素材返回 True，与模块声明的 fail-closed 不变量矛盾
  - 位置: src/features/edit/ai_editing/run_context.py:119-120（对照模块 docstring :19-21）
  - 证据:
    ```python
    def asset_in_scope(self, asset_id: str) -> bool:
        return not self.selected_asset_ids or asset_id in self.selected_asset_ids
    ```
    模块设计规则："missing project / expired revision / out-of-scope asset / out-of-budget all fail closed"；字段 docstring："selected_asset_ids: Assets the user explicitly allowed in scope."
  - 影响: 空 selection 被解释为"全工程素材均授权"。在 `PERMISSION_RESTRICTED_AUTO`（受限自动写）+ 空选择下，Agent 可改动任意素材而用户从未明确圈定——授权边界 fail-open。
  - 建议: 将"未圈定"与"空集"区分（如 `selected_asset_ids: tuple[str,...] | None`，None=显式不限，()=拒绝全部），或至少在 `validate_run_context` 对 RESTRICTED_AUTO 要求非空选择。
  - 置信度: 中（可能是有意的默认全作用域语义，但与文档声明直接矛盾）

- [AUD-14311] [Suggestion] `from_dict` 不做类型收敛，畸形持久化数据触发 TypeError 而非干净校验错误
  - 位置: src/features/edit/ai_editing/run_context.py:164-184（对照 :100-107 的 `__post_init__` 比较）
  - 证据:
    ```python
    base_revision=data.get("base_revision"),
    budget_units=data.get("budget_units"),
    max_duration_seconds=data.get("max_duration_seconds"),
    ...
    if self.base_revision is not None and self.base_revision < 0:
    ```
  - 影响: 持久化 JSON 中这些字段为字符串（如 `"3"`）时，`__post_init__` 的 `< 0` / `<= 0` 比较抛 `TypeError: '<' not supported between 'str' and 'int'`，反序列化边界没有可读错误，违背"外部数据须验证"。
  - 建议: `from_dict` 内逐字段 `int()`/`float()` 收敛并抛带字段名的 ValueError。
  - 置信度: 高

### 通过项
- 输入验证：`__post_init__` 校验 run_id/project_id 必填、permission 白名单、revision/budget/duration 边界（:90-107）
- 状态提交一致性：`validate_run_context` 的 STALE_REVISION 检查将 base_revision 与 observed_revision 不一致判为过期（:208-214），BUDGET_REQUIRED_FOR_AUTO 强制受限自动模式带预算（:221-226），fail-closed 方向正确
- `host_allowed` 空白名单返回 False（fail-closed，:122-125）；大小写不敏感匹配合理
- JSON-safe / Qt-free：to_dict/from_dict 对称，`fingerprint()` 用 canonical json + sha256，排序键稳定
- 类型注解完整

---

## src/features/edit/ai_editing/runtime_capability_snapshot.py (行数: 509)

### 发现

- [AUD-14312] [Major] 模型包条目顺序来自 frozenset 迭代，跨进程不稳定，snapshot_id 违反"同一输入 → 同一 ID"核心契约
  - 位置: src/features/edit/ai_editing/runtime_capability_snapshot.py:366（`for pack_id in _LOCAL_PACK_IDS:`）与 :108-118（snapshot_id 对 entries 顺序敏感的 sha256）
  - 证据:
    ```python
    for pack_id in _LOCAL_PACK_IDS:
        entries.append(RuntimeCapabilityEntry(
            component_id=f"models.{pack_id}",
    ```
    （`_LOCAL_PACK_IDS: frozenset[str]`，capability_status.py:27；`snapshot_id` 对 `[e.to_dict() for e in self.entries]` 做 `json.dumps(..., sort_keys=True)` 后 sha256——sort_keys 只排序键不排序列表元素）
  - 影响: Python str 哈希随机化使 frozenset 迭代顺序随进程变化 → entries 元组顺序变化 → snapshot_id 变化。模块核心契约"同一输入 → 同一 snapshot_id（检测时间不参与 ID 计算），跨入口可对齐"（:12）被破坏：CLI（toposctl）与 GUI 进程比对 ID 会看到幻影"能力变化"，`CapabilitySnapshotStore.set()` 的"ID 变化=可见变化"判定（:144-155）跨进程失去意义。
  - 建议: `for pack_id in sorted(_LOCAL_PACK_IDS):`，或在 snapshot_id 计算前对 entries 按 component_id 排序。
  - 置信度: 高

- [AUD-14313] [Major] 快照构建串行执行多个秒级 subprocess 探测且模块层无缓存，UI 线程首次访问可冻结数十秒
  - 位置: src/features/edit/ai_editing/runtime_capability_snapshot.py:452-455（GPU 探测）、:421-429（git rev-parse）、:492-503（串行探测序列）
  - 证据:
    ```python
    gpu = (gpu_prober or detect_host_gpu_info)()
    ```
    （detect_host_gpu_info 默认 `timeout_s: float = 30.0`，直接 `subprocess.run(["system_profiler", "-json", "SPDisplaysDataType"], ...)`，无缓存——host_gpu_info.py:30-45；`_git_revision()` 同样每次 `git rev-parse`，timeout=10）
  - 影响: `AiEditHostBridgeImpl._runtime_capability_snapshot()` 首次访问同步构建（ai_edit_host_bridge_impl.py:352-364，缓存仅在 bridge 实例上），而 `get_capability_manifest` 被 ai_edit_controller.py:6303-6309 的 inspector 源在 UI 侧调用。system_profiler 在 macOS 上常见 1-3 秒、超时上限 30 秒，叠加 git/ffmpeg 探测与 dylib 加载，冷启动首访可造成明显 UI 冻结。
  - 建议: 启动时后台线程预构建并写入 store；或 UI 路径首访立即返回 `FALLBACK/GL_PROBE_DEFERRED` 风格的延迟探测快照，探测完成后 set()。
  - 置信度: 中（探测成本与无缓存确定；inspector 调用是否主线程未最终确证，但其位于 Qt 控制器信号路径）

- [AUD-14314] [Suggestion] `CapabilitySnapshotStore` 无锁，set/subscribe/退订存在竞态窗口
  - 位置: src/features/edit/ai_editing/runtime_capability_snapshot.py:137-161
  - 证据:
    ```python
    self._listeners: list[Callable[[RuntimeCapabilitySnapshot], None]] = []
    ...
    def subscribe(self, callback):
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)  # type: ignore[func-returns-value]
    ```
  - 影响: 模块定位为"进程级 snapshot 持有者"，供 UI/AI/CLI 多方消费。`set()` 虽用 `tuple(self._listeners)` 快照迭代、`_snapshot` 赋值在 GIL 下原子，但并发 subscribe 与 set 之间仍有丢失通知窗口；退订 lambda 二次调用抛 ValueError。当前未见并发调用点，风险为潜在。
  - 建议: 用 `threading.Lock` 保护 listeners 的增删与通知；退订改为幂等（discard 语义）。
  - 置信度: 低

- [AUD-14315] [Suggestion] `detected_at` 用 "+0000" 时区后缀，与项目其它 ISO 时间戳格式不一致
  - 位置: src/features/edit/ai_editing/runtime_capability_snapshot.py:508
  - 证据:
    ```python
    detected_at=now or time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime()),
    ```
    （对照 remote_transcript_provider.py:121 的 `datetime.now(timezone.utc).isoformat()` → "+00:00"）
  - 影响: "+0000" 在 Python 3.10 的 `datetime.fromisoformat` 无法解析（3.11+ 才兼容），跨模块统一解析会失败；文档声称"ISO 字符串"。
  - 建议: 统一 `datetime.now(timezone.utc).isoformat()`。
  - 置信度: 中

### 通过项
- 输入验证：四态状态机禁用 bool 掩盖原因，所有探测失败路径带稳定 reason_code + detail（:174-215, :359-406）；fail-closed 语义明确（model_pack_manager=None → UNAVAILABLE）
- GPU 资源生命周期：快照不创建 GL context（render.gl 默认 FALLBACK/GL_PROBE_DEFERRED，:484-490），真实 GL 由渲染/导出 trail 证明——符合"UI 线程不得操作 OpenGL"红线
- 异常路径：探测器内宽泛 except 均带 noqa 注释并转为可见的 UNAVAILABLE 条目，不掩盖领域错误（:153, :184, :197, :307, :377）
- codec 真相源单一：`_probe_topos_codec` 复用 topos_binding 三态解析（:226-254），无第四份手抄
- snapshot 不可变性：RuntimeCapabilitySnapshot/Entry 均 frozen，任务冻结引用安全
- 类型注解完整（Optional/Dict/Tuple 旧式风格但一致）

---

## src/features/edit/ai_editing/safe_path_policy.py (行数: 153)

### 发现
（无）

### 通过项
- 通过（无发现，纯逻辑无 I/O）：路径安全核心不变量实现正确
- symlink 跳转防护：`resolve()` 展开 symlink 后再前缀断言（:99-116），`_best_effort_resolve` 对 strict 语义处理正确
- 相对路径逃逸防护：`..` 在 resolve 后消除；越根抛 `PathPolicyError` 带 resolved 详情（:111-115）
- 前缀比较陷阱防护：`_root_str` 强制尾随 "/"，避免 `/root/sub` 误匹配 `/root/subdir`（:82-83, :129-131）
- `safe_filename` 正则首字符强制字母数字，"." / ".." / 含分隔符 token 全部落入 sha256 分支，无路径语义残留（:41-55）
- `join_within` 每段先过 `safe_filename` 再 resolve_within，双重防护（:118-127）
- resolve 失败 fail-closed（构造器退回 absolute 后前缀校验只会更严，:75-81）
- 类型注解完整

---

## src/features/edit/ai_editing/schema.py (行数: 360)

### 发现

- [AUD-14316] [Major] `op.kind` 为非字符串（list/dict）时 `OperationKind()` 抛 TypeError，`except ValueError` 未捕获 → 校验器自身崩溃
  - 位置: src/features/edit/ai_editing/schema.py:212-215
  - 证据:
    ```python
    try:
        kind = OperationKind(kind_str)
    except ValueError:
        if kind_str in READONLY_TOOLS:
    ```
    （`OperationKind(str, Enum)`——models.py:40；enum 查找对不可哈希对象抛 TypeError 而非 ValueError）
  - 影响: 模块定位是"覆盖 LLM 最容易犯的错"的结构校验层、并供外部 MCP/host 直接校验原始 JSON；`"kind": ["add_clip"]` 这类畸形输入会以未捕获 TypeError 击穿 `validate_plan_structure`。当前主调用方 plan_validator.py:86 传入的是 `EditPlan.to_dict()`（kind 已被 from_dict 归一为 str），故现网路径被上游遮蔽，但对原始 dict 的直接调用（模块契约允许）即崩。
  - 建议: 循环内先 `isinstance(kind_str, str)` 守卫，或同时捕获 TypeError 归入 UNSUPPORTED_OPERATION。
  - 置信度: 中（机制确定；现网可达性受 to_dict 前置归一限制）

- [AUD-14317] [Major] 模块声称"未知 core 字段默认拒绝"，实现只对纯数字键给 WARNING、其余未知字段静默透传
  - 位置: src/features/edit/ai_editing/schema.py:9-12（docstring）与 :265-276（实现）、:359（schema 生成）
  - 证据:
    ```python
    # 允许的参数名集合 = 该 kind 的必填 + 常见可选（宽容，不在此穷举）
    # 这里只对明显可疑的纯数字键做提示，避免误伤合法可选字段。
    if isinstance(pkey, str) and pkey.isdigit():
        findings.append((DiagnosticSeverity.WARNING, ...
    ```
    对照 docstring："**未知 core 字段默认拒绝**：防止模型注入未定义行为。"以及 `build_plan_json_schema()` 顶层 `additionalProperties: True`。
  - 影响: 声称的安全不变量未实现：任意未知 core 字段（非 `x_*` 前缀）静默通过校验并随 plan 流转；依赖此文档/schema 的外部 host 会误以为有未知字段防护。
  - 建议: 按 kind 维护允许字段集（必填 + 显式可选清单），对非 `x_*` 未知键产 ERROR；或同步修正 docstring 与生成的 JSON Schema，明确"透传不拒绝"的实际语义。
  - 置信度: 中（行内注释表明是刻意的宽容取舍，但与模块顶层契约矛盾）

- [AUD-14318] [Suggestion] `validate_plan_structure` 函数体内有两个连续 docstring 字面量，第二个是死代码
  - 位置: src/features/edit/ai_editing/schema.py:108-127
  - 证据: :108-118 是含 Args 的真实 docstring，紧接 :119-127 又出现 `"""校验 plan 字典的结构（不涉及 canonical domain）。..."""`——Python 只认第一个为 `__doc__`，第二个是无副作用表达式语句。
  - 影响: 文档意图分裂，`__doc__` 丢失后半段"不校验 source range/domain 语义"的重要说明。
  - 建议: 合并为一个 docstring。
  - 置信度: 高

- [AUD-14319] [Suggestion] `isinstance(pkey, str)` 守卫位于 `pkey.startswith(...)` 之后，守卫位置错误
  - 位置: src/features/edit/ai_editing/schema.py:267-271
  - 证据:
    ```python
    for pkey in params:
        if pkey.startswith(EXTENSION_PREFIX):
            continue
        ...
        if isinstance(pkey, str) and pkey.isdigit():
    ```
  - 影响: 编程调用方传入非 str 键（如 int）时 `startswith` 先抛 AttributeError；isinstance 检查放在了保护的分支之后。JSON 来源键恒为 str，故 LLM 路径不可达。
  - 建议: 将 `isinstance(pkey, str)` 提到循环首行，非 str 键直接 SCHEMA_INVALID。
  - 置信度: 高

- [AUD-14320] [Suggestion] `plan_dict` 本身无 dict 守卫；`findings_to_diagnostics` 返回类型为裸 `list`
  - 位置: src/features/edit/ai_editing/schema.py:104（签名）、:146（首个 `.get` 调用）、:281-285
  - 证据:
    ```python
    def findings_to_diagnostics(
        findings: list[tuple[...]],
        *,
        lang: str = "zh",
    ) -> list:
    ```
    以及 :146 `proto = plan_dict.get("protocol")`——plan_dict 为 list/str 时先在 :137 `key not in plan_dict` 静默通过，随后 `.get` 抛 AttributeError。
  - 影响: 对非 dict 输入没有结构化诊断而是原生异常；返回类型注解不完整（应为 `list[Diagnostic]`）。
  - 建议: 函数入口 `isinstance(plan_dict, dict)` 守卫产 SCHEMA_INVALID；补全返回类型参数。
  - 置信度: 高

### 通过项
- 输入验证（已实现部分）：顶层必填字段、base_revision 类型（显式排除 bool，:155）、target 结构、operations 列表性、op_id 必填+唯一、kind 白名单（含 READONLY_TOOLS 误用检测）、每 kind 必填 params、APPLY_CAPTION_GRAPHIC_TEMPLATE 的 track_id/track_ids 特例——覆盖系统
- 协议版本协商：不匹配产 PROTOCOL_VERSION_MISMATCH 带 expected/received 变量（:145-151）；negotiate_protocol 容忍 "1.0" 缺前缀为有意设计
- 状态提交一致性：compilable_operations 早拒绝（协议合法但未实现的 kind 在 schema 层即拒，:229-239）与 plan_validator 的显式注入配合正确，None/空集合回退兼容旧行为
- 无 I/O、无 Qt、无线程、无 GLSL——不适用的维度均无风险
- diagnostic 构造：SCHEMA_INVALID 用预置英文 detail、UNSUPPORTED/PROTOCOL 走 make_diagnostic 模板，语言路由清晰（:281-306）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 9 | AUD-14300, 14301, 14302, 14306, 14310, 14312, 14313, 14316, 14317 |
| Suggestion | 12 | AUD-14303, 14304, 14305, 14307, 14308, 14309, 14311, 14314, 14315, 14318, 14319, 14320 |

文件通过情况：rollout_metrics.py、safe_path_policy.py 全维度通过（无发现）；其余 6 个文件按上述发现输出。
