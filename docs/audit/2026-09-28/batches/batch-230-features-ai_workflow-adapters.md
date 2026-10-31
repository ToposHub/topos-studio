# batch-230 审查报告：src/features/ai_workflow/adapters/

- 批次：batch-230 [P2]
- 编号区间：AUD-23000–AUD-23099
- 文件数：6（含 1 个空文件）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 说明：本批次为纯 Python ComfyUI 兼容层（无 OpenGL / GLSL / Qt 信号参与），GPU 资源生命周期与信号防递归维度不适用。
- 交叉引用文件（只读取证）：`comfyui_models.py`（同目录，非本批次）、`src/app/controllers/ai_editing_mixin.py`、`src/shared/ai_models/generation/generation_models.py`。

---

## src/features/ai_workflow/adapters/__init__.py (行数: 0)

### 发现

（无）

### 通过项
- 通过（无逻辑）：空文件（0 行），不导出任何符号；模块消费者均直接 import 子模块，无 re-export 遗漏风险。

---

## src/features/ai_workflow/adapters/comfyui_client.py (行数: 495)

### 发现

- [AUD-23001] [Major] capabilities() 对 /object_info 的解析结构与 ComfyUI 真实格式不符，available_models 恒为空集，Real 路径 preflight 必然 MISSING_MODEL 拒绝
  - 位置: src/features/ai_workflow/adapters/comfyui_client.py:244-249
  - 证据:
    ```python
    for input_spec in (info.get("input") or {}).values():
        if isinstance(input_spec, list) and input_spec:
            # loader 输入的候选列表即 server 模型文件名
            for candidate in input_spec:
                if isinstance(candidate, str) and "." in candidate:
                    models.add(candidate)
    ```
  - 影响: 真实 ComfyUI `/object_info` 中每个节点的 `input` 是 `{"required": {...}, "optional": {...}}`，其 `.values()` 得到的是 dict（输入名 → spec），`isinstance(input_spec, list)` 恒为 False，模型候选永远收集不到。结果 `available_models=frozenset()`；而 importer 会从 snapshot 抽出 `required_models`（如 `sd_xl_base_1.0.safetensors`），preflight 第 2 项 `has_model()` 对每个模型判 False → 所有带模型 loader 的 workflow 都被 `MISSING_MODEL` 拒绝，Real 路径（宿主 `bind_comfy_workflow` → `capabilities()` → `set_server_capabilities`）实质不可用。测试文件 test_comfyui_client.py 无 capabilities/object_info 用例，故未暴露。
  - 建议: 按 `info["input"]["required"/"optional"].values()` 遍历，取 spec 形如 `[candidates, config]` 的 `spec[0]`（为 list of str 时）收集模型名；补一条以真实 object_info 结构为 fixture 的测试。
  - 置信度: 高（代码逻辑与 ComfyUI 公开 API 格式比对；未连真实 server 实测）

- [AUD-23002] [Major] job_outputs 吞掉下载异常并永久置 outputs_fetched=True：瞬时网络错误被固化为"无产物"或携带占位 hash 的候选
  - 位置: src/features/ai_workflow/adapters/comfyui_client.py:217-223（配合 435-481）
  - 证据:
    ```python
    if not record.outputs_fetched:
        try:
            self._refresh_state(record)
            self._download_outputs(record)
        except _ComfyHttpError as e:
            _LOG.warning("comfy outputs fetch failed: %s", e.message)
        record.outputs_fetched = True
    ```
  - 影响: `_download_outputs` 抛出可重试的 `NETWORK_ERROR` 时仅 warning，随后 `outputs_fetched=True` 使后续调用永不重试。失败后 artifacts 的 `content_hash` 保持 `"sha256:pending-download"`（429 行）且 `actual_output_hashes` 为空；executor 侧 `actual_output_hashes.get(filename, out.content_hash)` 回退到期望值（comfyui_executor.py:583），占位 hash 与自身"相等"通过校验 → 生成成功入 bin 但 `expected_sha256="sha256:pending-download"` 永不匹配，物化阶段才失败且归因错误。若 outputs 本就为空则误报 NO_OUTPUTS。另外下载超限/中断时 `open(target, "wb")` 已写入的部分文件残留在缓存目录，无清理。
  - 建议: 失败时不置 `outputs_fetched=True`（让下一轮 poll/fetch 重试），或在返回值中显式携带错误让 executor 映射 `SERVER_FAILED`；失败时删除半成品文件；对 pending 占位 hash 在 executor 侧显式拒绝。
  - 置信度: 高

- [AUD-23003] [Major] 下载落盘路径直接拼接 server 返回的 filename，无路径穿越校验（外部数据未验证）
  - 位置: src/features/ai_workflow/adapters/comfyui_client.py:450-452（写入点 456-469）
  - 证据:
    ```python
    target = self._download_dir / (
        f"{record.external_job_id[:12]}_{out.filename}"
    )
    ...
    with self._opener(request, timeout=self._timeout) as response, \
            open(target, "wb") as fh:
    ```
  - 影响: `out.filename` 来自外部 Comfy server 的 `/history` 响应（`_parse_history_outputs` 418-421 行仅做 `str()` 转换）。若 server 被控或数据被篡改，`filename="../../../../.zshrc"` 之类可在缓存目录外任意覆写文件（用户工程/配置，属数据丢失级，接近 Critical；本地 localhost 场景风险较低）。违反铁律"外部数据须验证"。
  - 建议: 取 `Path(out.filename).name` 只保留纯文件名（或校验不含 `/`、`\`、`..`），拼接后断言 `target.resolve()` 位于 `self._download_dir.resolve()` 之下。
  - 置信度: 高（代码路径确定；触发需要恶意/异常 server）

- [AUD-23004] [Major] cancel() 对 QUEUED 任务也乐观置 CANCELLED，但 POST /interrupt 只打断当前执行、不会出队，server 侧仍会执行该任务
  - 位置: src/features/ai_workflow/adapters/comfyui_client.py:191-203
  - 证据:
    ```python
    if record.state not in ComfyJobState.TERMINAL:
        try:
            self._request_json("POST", "/interrupt")
        except _ComfyHttpError as e:
            _LOG.warning("comfy interrupt failed: %s", e.message)
        else:
            record.state = ComfyJobState.CANCELLED
            record.progress_message = "interrupt requested"
    ```
  - 影响: ComfyUI HTTP 语义中 `/interrupt` 仅中断正在执行的任务；排队中的任务需 `DELETE /queue`（按 prompt_id 删除）才会移除。当前实现对 QUEUED 任务也直接标记 CANCELLED：客户端/用户认为已取消（executor `poll_until_terminal` 随即以 CANCELLED 终止并停止轮询），server 实际仍会执行该 workflow——本地浪费 GPU，云 profile 浪费费用。docstring 自称"queue 中移除尽力而为"但没有任何出队实现。
  - 建议: QUEUED 状态额外调用 `DELETE /queue`（body `{"delete":[prompt_id]}`）；或至少不把 QUEUED 任务标为终态 CANCELLED，而是保留状态由下一轮 `_refresh_state` 依据 /history 决定。
  - 置信度: 中（ComfyUI 各版本 /interrupt 语义以官方 API 文档为准）

- [AUD-23005] [Suggestion] /object_info 响应 2MB 上限对装较多 custom node 的真实 ComfyUI 偏小，capabilities() 会 RESPONSE_TOO_LARGE 失败
  - 位置: src/features/ai_workflow/adapters/comfyui_client.py:56-57, 322-327
  - 证据:
    ```python
    #: JSON 响应体上限（/object_info 可能较大；2MB 与 http_generation_provider 一致）。
    _MAX_JSON_BYTES = 2 * 1024 * 1024
    ...
    raw = response.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise _ComfyHttpError("RESPONSE_TOO_LARGE", ...)
    ```
  - 影响: 常见多插件安装的 /object_info 可达数 MB；超限后 `bind_comfy_workflow` 返回失败，ComfyUI 桥整体不可用。
  - 建议: 对 /object_info 单独放宽上限（如 16-32MB），流式解析或仅抽取所需字段。
  - 置信度: 中（阈值经验判断，未实测大体积 server）

- [AUD-23006] [Suggestion] _refresh_state 用 json.dumps 子串匹配判定任务在 running/pending 队列中的归属，脆弱且每轮全量序列化队列
  - 位置: src/features/ai_workflow/adapters/comfyui_client.py:387-400
  - 证据:
    ```python
    running = json.dumps(queue.get("queue_running") or [], default=str)
    pending = json.dumps(queue.get("queue_pending") or [], default=str)
    job_marker = record.external_job_id
    if job_marker in running:
    ```
  - 影响: 子串匹配理论上可被其他任务的 payload 内容误命中（极小概率）；每次轮询把整个队列（含所有任务的完整 prompt）序列化成字符串，队列长时是纯浪费的分配开销。
  - 建议: 结构化遍历 `queue_running`/`queue_pending` 元素，取每个条目的 prompt_id（通常为 `entry[1]` 或 `entry[2]` 依版本）精确比对。
  - 置信度: 高（代码事实）；影响概率低

- [AUD-23007] [Suggestion] 以 object.__setattr__ 绕过 frozen 契约改写 ComfyOutputArtifact，并以动态属性 _subfolders 扩展 _JobRecord
  - 位置: src/features/ai_workflow/adapters/comfyui_client.py:433, 440, 478-481
  - 证据:
    ```python
    record._subfolders = subfolders  # noqa: SLF001 — 自身记录
    ...
    object.__setattr__(out, "content_hash", "sha256:" + digest.hexdigest())
    object.__setattr__(out, "bytes_size", size)
    object.__setattr__(out, "local_path", str(target))
    ```
  - 影响: `ComfyOutputArtifact` 在 models 层声明为 frozen（"全部 frozen + 可序列化"红线）；绕过冻结机制使同一对象在下载前后值不同，任何依赖不可变性的下游（缓存/并发共享）行为不可预期。`_subfolders` 未在 `_JobRecord.__init__` 声明，依赖 `getattr(record, "_subfolders", {})` 兜底，可读性差。
  - 建议: 下载完成后用 `dataclasses.replace` 重建 artifact（或引入可变的下载结果 DTO）；`_subfolders` 提升为 `_JobRecord.__init__` 正式字段。
  - 置信度: 高（契约事实）；无直接功能故障

- [AUD-23008] [Suggestion] _headers 对凭据读取使用宽泛 except Exception，掩盖 CredentialStore 领域错误
  - 位置: src/features/ai_workflow/adapters/comfyui_client.py:292-300
  - 证据:
    ```python
    try:
        secret = self._credential_store.get_secret(
            self._profile.credential_ref,
        )
    except Exception:  # noqa: BLE001 — 凭据读取失败按缺失处理
        secret = None
    ```
  - 影响: 云 profile 下 store 损坏/锁冲突等真实故障被静默降级为"无凭据"，请求将以 401 AUTH_FAILED 呈现，用户与日志都难以定位真实原因（仅静默）。虽带 noqa 说明，但违反".clinerules 不得宽泛 except 掩盖领域错误"的精神。
  - 建议: 至少 `_LOG.warning` 记录异常再降级；或只捕获 store 声明的异常类型。
  - 置信度: 高（代码事实）；严重度低

### 通过项
- 线程模型：全同步、不在 client 内起线程，生产经 worker 线程执行，与文档一致。
- 凭据红线：secret 只经 CredentialStore 取用，绝不内联或写日志；本地 profile 不带 Authorization。
- HTTP 重试策略：仅对 408/429/5xx 等幂等状态码与网络错误重试，退避有上限；4xx 立即失败。
- 取消/异常路径：`get_job` 轮询瞬时失败降级为缓存状态（有 debug 日志），poll 循环不崩。
- 输入验证：`/prompt` 响应 prompt_id 回退自身生成值；历史 outputs 逐项 isinstance 校验。
- 资源上限：单产物 64MB 下载上限、1MB 分块流式 + 边下边算 sha256。

---

## src/features/ai_workflow/adapters/comfyui_converter.py (行数: 344)

### 发现

- [AUD-23009] [Suggestion] 构造器中赋值的 _known_targets 从未使用（死代码），注册校验实际走 registry.get()
  - 位置: src/features/ai_workflow/adapters/comfyui_converter.py:293-299
  - 证据:
    ```python
    self._known_targets = {
        "topos.generation.text_to_image",
        "topos.generation.upscale",
    }
    ```
  - 影响: 注释声称"构造时校验目标 spec_id 集合"，但该集合从未参与任何判断（convert 内直接 `self._registry.get(f.target_spec_id)`）；误导后续维护者以为构造期已校验。
  - 建议: 删除 `_known_targets`，或真正在构造期对集合逐项 `registry.get` 校验并给出明确报错。
  - 置信度: 高

- [AUD-23010] [Suggestion] 参数提取用 `or` 提供默认值，把合法的 0 与缺失/link 引用混淆
  - 位置: src/features/ai_workflow/adapters/comfyui_converter.py:182-187
  - 证据:
    ```python
    params: Dict[str, Any] = {
        "seed": _literal_input(snap, ksampler_id, "seed") or 0,
        "steps": _literal_input(snap, ksampler_id, "steps") or 30,
        "width": _literal_input(snap, empty_id, "width") or 1024,
        "height": _literal_input(snap, empty_id, "height") or 1024,
    }
    ```
  - 影响: `steps=0`（合法字面量）会被替换成 30；seed 为 link 引用（返回 None）时静默变 0，转换结果与源 workflow 语义不等价，违背"golden 等价断言"的目标。
  - 建议: 用显式 `value if value is not None else default` 判 None 而非真值；link 引用的关键参数（seed/steps）缺失时降置信度或退回 OPAQUE。
  - 置信度: 高（逻辑事实）；触发频率低

### 通过项
- 只读红线：纯模式匹配，不执行任何 custom node 代码、不联网。
- C5 不承诺：未知模式 / 目标 spec 未注册均降级 OPAQUE + warning，不强行转换。
- 可追溯性：ConversionFinding.source_node_ids 记录源节点；verdict 白名单校验（__post_init__）。
- 数据契约：frozen dataclass + to_dict 序列化完整。

---

## src/features/ai_workflow/adapters/comfyui_executor.py (行数: 725)

### 发现

- [AUD-23011] [Major] 默认 poll_timeout_s=30s 对真实生成任务过短且宿主未覆盖：超过 30 秒的生成会被主动取消并报 TIMEOUT
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:387, 509, 526-531（宿主证据 src/app/controllers/ai_editing_mixin.py:784-787）
  - 证据:
    ```python
    poll_timeout_s: float = 30.0,
    poll_interval_s: float = 0.05,
    ...
    if time.monotonic() >= deadline:
        self.cancel_job(current)
        raise ComfyExecutorError(
            code=ComfyExecutorErrorCode.TIMEOUT, ...)
    ```
    宿主装配（无覆盖）：`ComfyWorkflowExecutor(server=client, server_capabilities=client._empty_capabilities())`
  - 影响: 真实 ComfyUI 图像生成普遍 >30s（SDXL 常见 1-5 分钟）。`generate()` 调 `poll_until_terminal(ref, is_cancelled=...)` 未传 timeout_s → 用默认 30s；到点即向 server 发取消并返回 TIMEOUT。Real 路径上几乎所有非平凡生成都会被误杀。
  - 建议: 默认提到分钟级（如 600s）或按 modality 配置；宿主装配处显式传参；对 Fake/测试路径由测试显式传小值。
  - 置信度: 高（默认值与调用链为代码事实）

- [AUD-23012] [Major] generate 协议路径无法携带输入绑定，且构造的 ComfyImportReport 不含未绑 slot → preflight 的 MISSING_BINDING 检查恒为空转
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:432-442, 673-681, 688-691
  - 证据:
    ```python
    def bind_workflow(self, snapshot, *, profile) -> None:   # 无 bindings 参数
    ...
    comfy_req = ComfyGenerationRequest(
        request_id=request.request_id,
        snapshot=self._bound_snapshot, ... )   # bindings 用默认 ()
    ...
    report = ComfyImportReport(snapshot=self._bound_snapshot)  # unbound slots 为空
    ```
  - 影响: 生产唯一入口是宿主 `bind_workflow` → ProviderRegistry → `generate()`（src 内无其他 submit_job 调用方）。该路径 `ComfyGenerationRequest.bindings` 恒为 `()`，导入时快照里的字面量（占位 prompt/种子等）被原样提交，用户无法经 GenerationService 覆盖；同时 preflight 第 3 项 `required_unbound_slot_ids` 恒空，绑定门禁形同虚设。`ComfyGenerationRequest.bindings` 字段成为死参数。
  - 建议: `bind_workflow` 增加 bindings 入参（或从 GenerationRequest 的扩展字段解析），并在 generate 构造 report 时把未绑的必填 slot 传入 `required_unbound_slot_ids`。
  - 置信度: 中（可能是 v1 有意裁剪，但字段存在且无生产赋值点，属明确功能缺口）

- [AUD-23013] [Major] fetch_outputs 哈希校验在"实际 hash 缺失"时回退为期望值，校验恒等通过——弱化 §9.4 #5 红线
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:583-585
  - 证据:
    ```python
    actual_hash = actual_output_hashes.get(out.filename, out.content_hash)
    if actual_hash != out.content_hash:
    ```
  - 影响: server/客户端未提供实际 hash（如 AUD-23002 下载失败后的 pending 占位）时，`get` 的默认值就是期望值，比较必然相等。红线"下载内容 hash ≠ server 报告 → HASH_MISMATCH，候选不入 bin"被"无证据即放行"绕过；Fake 路径之外任何 hash 缺失场景都会静默产出 `expected_sha256="sha256:pending-download"` 的候选。
  - 建议: hash 缺失（不在 actual_output_hashes 中）应视为校验失败（HASH_MISMATCH 或专用诊断），不做回退；至少拒绝 `content_hash` 仍为 pending 占位的 artifact。
  - 置信度: 高

- [AUD-23014] [Suggestion] submitted_payload 的类型转换只捕 ValueError，且空 resolved_value 会覆写快照字面量
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:188-200
  - 证据:
    ```python
    val: Any = b.resolved_value
    if b.slot.artifact_type == "integer":
        try:
            val = int(b.resolved_value)
        except ValueError:
            pass
    ...
    inputs[b.slot.input_name] = val
    ```
  - 影响: 其一，`resolved_value` 非 str（如 None/列表，from_dict 反序列化 JSON null 时）`int(None)` 抛 TypeError 未捕获，提交路径崩溃；其二，按 models 契约"从 artifact 解析时 resolved_value 为空"，artifact 引用型绑定的空串会直接写入节点输入，覆掉快照里的字面量（如 seed 变 ""）。当前生产路径 bindings 恒空（见 AUD-23012）故未触发。
  - 建议: 捕 `(TypeError, ValueError)`；`resolved_value` 为空且 `source_artifact_id` 非空时跳过覆写或显式报错（artifact 未解析）。
  - 置信度: 中（依赖调用方传入形态）

- [AUD-23015] [Suggestion] _submitted_jobs 只增不减，长生命周期 executor 内存无界增长
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:405, 481
  - 证据:
    ```python
    self._submitted_jobs: Dict[str, Tuple[str, ComfyJobRef]] = {}
    ...
    self._submitted_jobs[req.request_id] = (fingerprint, ref)
    ```
  - 影响: executor 在宿主为每 profile 常驻（ai_editing_mixin 装配后不销毁），每次生成追加一条且终态后无清理/上限；长期会话缓慢累积。
  - 建议: 终态（fetch/cancel 完成）后清理，或加 LRU 上限。
  - 置信度: 高（事实）；增幅很小

- [AUD-23016] [Suggestion] supported_modalities 在无 AUDIO 枚举时返回重复的 IMAGE
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:419-424
  - 证据:
    ```python
    return (
        GenerationModality.IMAGE,
        GenerationModality.VIDEO,
        GenerationModality.AUDIO if hasattr(GenerationModality, "AUDIO") else GenerationModality.IMAGE,
    )
    ```
  - 影响: AUDIO 缺失时元组为 (IMAGE, VIDEO, IMAGE)，能力广告含重复项；且 hasattr 防御掩盖真实枚举依赖。
  - 建议: 条件构造列表去重；直接依赖 AUDIO（共享模型已定义）。
  - 置信度: 高

- [AUD-23017] [Suggestion] server 参数注解为 FakeComfyServer，但生产传入 RealComfyClient（靠 duck-typing）；应提取 Protocol
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:381-385（配合 629 行 getattr 探测 output_url）
  - 证据:
    ```python
    def __init__(
        self,
        *,
        server: FakeComfyServer,
    ```
  - 影响: 类型注解与生产用法不符，静态检查/IDE 推导误导；`getattr(self._server, "output_url", None)` 等能力探测散落，Fake/Real 协议面没有单一权威定义（协议一致性目前只靠测试 `test_real_implements_fake_surface` 兜底）。
  - 建议: 定义 `ComfyServerProtocol`（submit/get_job/cancel/job_outputs/output_url），executor 与 client 都面向 Protocol。
  - 置信度: 高（质量项）

- [AUD-23018] [Suggestion] poll_interval_s 默认 0.05s 对真实 HTTP server 轮询过密
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:388, 532-533
  - 证据:
    ```python
    poll_interval_s: float = 0.05,
    ...
    if self._poll_interval_s:
        time.sleep(self._poll_interval_s)
    ```
  - 影响: 0.05s 是为 Fake 内存 server 设计的；Real 路径等于每秒 20 次 `/history`+`/queue` 请求，直到 30s 超时（见 AUD-23011），对 server 与本机都是无谓负载。
  - 建议: 默认提到 0.5-1s，测试路径显式传小值。
  - 置信度: 高

- [AUD-23019] [Suggestion] bind_workflow / set_server_capabilities 与 worker 线程中的 generate 无同步保护
  - 位置: src/features/ai_workflow/adapters/comfyui_executor.py:428-442, 658-663
  - 证据:
    ```python
    def set_server_capabilities(self, caps: ComfyServerCapabilities) -> None:
        self._caps = caps
    ...
    def bind_workflow(self, snapshot, *, profile) -> None:
        self._bound_snapshot = snapshot
        self._bound_profile = profile
    ```
  - 影响: 宿主 `bind_comfy_workflow`（控制器线程，可能 UI 线程）可在 worker 正在执行 `generate()` 读 `_bound_snapshot`/`_caps` 时改写二者（重绑新 workflow/刷新能力）；Python 引用赋值本身原子，不会撕裂，但一次 generate 可能用到换绑前后的混合状态（preflight 用旧 caps、提交用新 snapshot），结果不一致且难复现。
  - 建议: bind 时若存在 in-flight request 则拒绝或排队；或用锁/不可变快照封装绑定状态。
  - 置信度: 中（竞态窗口小，依赖宿主调用时序）

### 通过项
- consent gate：generate 入口 + ComfyGenerationRequest.__post_init__ 双重校验，未确认不提交（CONSENT_INCOMPLETE）。
- 幂等：request_id 重复提交按指纹判 IDEMPOTENCY_CONFLICT 或复用原 job，不产生第二次付费提交。
- 取消传播：poll 循环中的用户取消与超时均先 cancel_job 再返回诊断；错误码 frozen 集合校验。
- 结果去向：仅返回 GenerationResult（候选），不触碰 timeline/GenerationBin 以外的顶层对象。
- preflight 红线：submit_job 前强制 check，未过即 PREFLIGHT_FAILED 异常。
- provenance 完整：provider_id/model_id/model_version(snapshot hash)/input_prompt_hash 均填充。

---

## src/features/ai_workflow/adapters/comfyui_export.py (行数: 326)

### 发现

- [AUD-23020] [Major] 导出 payload 顶层注入无 class_type 的 `_topos_meta` 键：纯 Comfy 导出无法被 ComfyUI /prompt 或本项目 importer 加载，与 equivalence_guaranteed=True 矛盾
  - 位置: src/features/ai_workflow/adapters/comfyui_export.py:238-246（冲突证据 comfyui_models.py:488-492）
  - 证据:
    ```python
    payload["_topos_meta"] = {
        "workflow_snapshot_hash": snapshot.content_hash,
        ...
    }
    ```
    importer 侧（comfyui_models.from_api_format）：
    ```python
    class_type = node_def.get("class_type")
    if not class_type or not isinstance(class_type, str):
        raise ValueError(f"node {node_id!r} missing string class_type")
    ```
  - 影响: 纯 Comfy 子图导出宣称 `equivalence_guaranteed=True`，但产物顶层多出 `_topos_meta`（dict、无 class_type）。直接 POST 给 ComfyUI `/prompt` 会因"node missing class_type"整单校验失败；用本项目 `ComfyWorkflowImporter.import_from_json_string` 回读同样抛 ValueError——导出→导入往返断裂。测试只在等价断言时手动排除 `_topos_meta`（test_comfyui_export.py:94-95），未覆盖真实回读。
  - 建议: 元数据不占节点命名空间——改为旁车文件、HTTP header 语义不可行则至少导出双版本，或 importer 侧显式跳过 `_` 前缀的 meta 键并在 ComfyUI 提交前剥离。
  - 置信度: 高（两侧代码均为事实）

- [AUD-23021] [Suggestion] _apply_binding 与 executor 同源的转换缺陷：只捕 ValueError、空 resolved_value 覆写字面量
  - 位置: src/features/ai_workflow/adapters/comfyui_export.py:287-298
  - 证据:
    ```python
    val: Any = binding.resolved_value
    if binding.slot.artifact_type == "integer":
        try:
            val = int(binding.resolved_value)
        except ValueError:
            pass
    ...
    inputs[binding.slot.input_name] = val
    ```
  - 影响: 同 AUD-23014：artifact 引用型绑定的空串会清掉节点字面量输入；非 str 值抛 TypeError 未捕获使导出崩溃。导出场景下会产出"绑定后即损坏"的 JSON。
  - 建议: 与 executor 的 submitted_payload 抽取公共的绑定应用函数并统一修（判空 + 捕 `(TypeError, ValueError)`）。
  - 置信度: 中（依赖调用方传入形态）

- [AUD-23022] [Suggestion] _scrub_credentials 只递归 dict 不递归 list，且子串匹配可能误删含 "token"/"secret" 字样的合法输入
  - 位置: src/features/ai_workflow/adapters/comfyui_export.py:175-178, 300-310
  - 证据:
    ```python
    _CREDENTIAL_KEYS: Tuple[str, ...] = (
        "api_key", "apikey", "secret", "password", "credential_ref",
        "bearer", "authorization", "token",
    )
    ...
    if any(bad in low for bad in _CREDENTIAL_KEYS):
        payload.pop(key, None)
        continue
    v = payload[key]
    if isinstance(v, dict):
        ComfyWorkflowExporter._scrub_credentials(v)
    ```
  - 影响: 其一，嵌在 list 里的 dict（API-format 输入值可为数组）不会被清理，凭据残留风险；其二，输入名如 `tokenizer`（含 "token"）的合法输入会被整项删除，导出仍标记 equivalence_guaranteed=True，静默破坏等价性。
  - 建议: 递归 list 元素；键匹配改为精确词表/后缀（如以 `_key`/`_token` 结尾）或仅对已知敏感节点类型处理，误删时写入 warnings。
  - 置信度: 高（代码事实）；触发频率低

### 通过项
- 原生节点不伪装：占位 class_type `_ToposNativePlaceholder_*` + 内嵌 `_warning`，混排导出 equivalence_guaranteed=False + warning，符合 §9.4 #8。
- 只读红线：不联网、不执行任何节点代码；导出前 deepcopy，不污染 snapshot.raw_payload。
- 契约校验：NativeNodePlaceholder.__post_init__ 校验 kind 白名单与必填字段；ExportResult 可序列化。
- 文件名建议：workflow_id 做 `/` 替换，避免路径分隔符。

---

## src/features/ai_workflow/adapters/comfyui_importer.py (行数: 396)

### 发现

- [AUD-23023] [Major] UNPINNED_CUSTOM_NODE 门禁不可达：两个数据源在生产路径恒为空，§13.7"未 pin 默认拒绝"红线形同虚设
  - 位置: src/features/ai_workflow/adapters/comfyui_importer.py:354-389（数据源证据 comfyui_client.py:250-254、comfyui_models.py:543-550）
  - 证据:
    ```python
    unpinned_repos: List[str] = []
    ...
    for repo in capabilities.custom_node_repos:   # RealComfyClient.capabilities() 从不填充
        ...
    for c in report.snapshot.required_custom_nodes:
        if c.repo_url and not c.has_pinned_commit:  # from_api_format 恒置 repo_url=""
    ```
  - 影响: `RealComfyClient.capabilities()` 构造 `ComfyServerCapabilities` 时未传 `custom_node_repos`（默认 frozenset()）；snapshot 侧 `from_api_format` 对未知 custom node 恒写 `repo_url=""`。因此 `unpinned_repos` 恒空，`require_pinned_custom_nodes=True` 的默认拒绝策略从未触发——任意未 pin 的 custom node workflow 都能通过 preflight。
  - 建议: capabilities() 从 `/object_info` 的 `aux_id`/`node site` 信息或 `/manager` 类端点尽力收集 repo 信息；至少在 repo 信息不可得时对 `is_unknown_custom_node` 的节点给出 warning 级诊断提示风险，而非静默放行。
  - 置信度: 高（三处代码交叉验证，纯逻辑推演）

### 通过项
- 只读红线：importer 全程静态解析，不执行 custom node 代码、不联网。
- 输入验证：JSON 根类型/节点 dict/class_type/inputs 逐项校验并给出可读 ValueError。
- 诊断完整性：四个检查项 message/detail/suggested_action 齐备；缺模型建议明确"不自动下载"（§13.7）。
- 数据契约：frozen dataclass + to_dict 往返；deny_code 取首个 error，语义稳定。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 9 | AUD-23001, 23002, 23003, 23004, 23011, 23012, 23013, 23020, 23023 |
| Suggestion | 14 | AUD-23005–23010, 23014–23019, 23021, 23022 |

主题性结论：Real ComfyUI 路径存在三处串联的功能性断裂——capabilities 模型解析错误（23001）导致 preflight 必拒、默认 30s 轮询超时（23011）导致长任务必被取消、下载异常吞噬 + hash 回退（23002/23013）破坏 §9.4 #5 完整性红线；导出侧 `_topos_meta`（23020）破坏"等价保证"承诺；UNPINNED 门禁（23023）不可达使 §13.7 红线失效。Fake/测试路径质量良好。
