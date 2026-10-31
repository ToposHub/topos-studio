# batch-137 审查报告 — src/features/edit/ai_editing/

- 批次：batch-137 [P1]
- 编号区间：AUD-13700–AUD-13799
- 文件数：5（合计 2,797 行，全部 Read 通读）
- 审查日期：2026-09-28
- 统计：Critical 0 / Major 4 / Suggestion 11

维度覆盖：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。本批次为纯 Python 后台/数据合约层，无 Qt 信号、无 GLSL、无 GPU 资源（GPU/GLSL/信号防递归维度不适用或通过）。

---

## src/features/edit/ai_editing/jobs.py (行数: 543)

### 发现

- [AUD-13701] [Major] JobWorkerPool 默认 2 个 worker，但 JobRegistry 共享事件计数器/事件表无锁，`_event_sequence += 1` 存在并发竞争
  - 位置: src/features/edit/ai_editing/jobs.py:118（及 345-347、329-331）
  - 证据:
    ```python
    def __init__(self, registry: "JobRegistry", *, max_workers: int = 2) -> None:
        ...
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="topos-ai-job")
    ```
    ```python
    def _record_event(self, job: JobHandle, kind: str, message: str = "") -> None:
        self._event_sequence += 1
        self._events.append(JobEvent(
            sequence=self._event_sequence,
    ```
    JobRegistry 文档字符串（330 行）声明："P4 不做锁（单 worker 场景足够）；多 worker 由宿主加锁"，而同一模块的 JobWorkerPool 默认 `max_workers=2`，两个 worker 线程会并发调用 `update_progress → _emit → _record_event`。
  - 影响: `+=` 是非原子读-改-写：并发时 sequence 号重复或跳号；`events_since(sequence)` 以 sequence 为 UI 重连游标，重复/跳号会导致事件丢失或重复投递；`del self._events[:-500]`（355-356 行）与遍历并发还可能产生截断竞态。模块自身默认配置就违反了自己声明的"单 worker"前提。
  - 建议: 在 `_record_event`（以及 `prune_events`/`restore_dict` 写路径）加 `threading.Lock`，或将 JobWorkerPool 默认 max_workers 改为 1 并在文档中强制多 worker 时由宿主加锁；至少用 `itertools.count` 原子取号。
  - 置信度: 高

- [AUD-13702] [Major] JobWorkerPool._futures 只增不减，且无任何清理 API，随会话无界增长（内存泄漏）
  - 位置: src/features/edit/ai_editing/jobs.py:131-133（及 123、154-155）
  - 证据:
    ```python
    future = self._executor.submit(self._run, job, work)
    self._futures[job.job_id] = future
    return future
    ...
    def future(self, job_id: str) -> Future[Any] | None:
        return self._futures.get(job_id)
    ```
    全文件检索确认 `self._futures` 仅有此一处写入与一处读取，无 `.pop`/`.del`/`.clear`；JobRegistry.cleanup_terminal 只清理 `_jobs`，不触及 futures。
  - 影响: 每个预览/导出 job 的 Future（及其持有的 result manifest 返回值）在长编辑会话中永久驻留；`_run` 失败路径返回 None 也不释放条目。桌面应用长会话下为无界内存增长。
  - 建议: 给 Future 附加 `add_done_callback` 在终态后从 `_futures` 移除，或提供 `cleanup(job_ids)` 与 JobRegistry.cleanup_terminal 联动。
  - 置信度: 高

- [AUD-13703] [Suggestion] `fail()` 无终态防护，可把已 SUCCEEDED 的 job 覆写为 FAILED（与 `complete()` 的防护不对称）
  - 位置: src/features/edit/ai_editing/jobs.py:231-236
  - 证据:
    ```python
    def fail(self, error_code: str, error_message: str) -> None:
        self.status = JobStatus.FAILED
        self.error_code = error_code
        self.error_message = error_message
        self.finished_at = time.time()
        self._emit("failed", error_message)
    ```
    对比 `complete()`（212-217 行）会检查 `CANCELLING` 并转 CANCELLED；`update_progress`/`set_checkpoint` 都检查 `is_active`，唯独 `fail()` 不检查任何状态。
  - 影响: 领域回调若在 `complete()` 之后（例如收尾清理异常）再调 `fail()`，已成功并写入 result_manifest 的 job 被翻转为 FAILED，UI 显示失败但产物已落盘，破坏"不产出看似成功/失败不一致状态"的契约。worker 自身路径有 `is_active` 防护（150 行），风险在直接驱动 JobHandle 的宿主代码。
  - 建议: `fail()` 开头加 `if self.status.is_terminal: return`（或至少不覆盖 SUCCEEDED）。
  - 置信度: 高

- [AUD-13704] [Suggestion] `from_dict` 未验证 `started_at`/`finished_at` 数值类型，恢复外部 JSON 后 `metrics()` 可能抛 TypeError
  - 位置: src/features/edit/ai_editing/jobs.py:304-305（及 368-370）
  - 证据:
    ```python
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
    ```
    ```python
        durations = [
            max(0.0, (job.finished_at or 0.0) - (job.started_at or job.created_at))
    ```
    `progress`/`created_at` 都做了 `float(... or 0.0)` 强转，唯独这两个时间戳原样透传。
  - 影响: 工程快照被手改/损坏（外部数据未验证铁律）时，字符串型时间戳使 `metrics()` 的减法抛 TypeError；由于 restore_dict 捕获的是构造阶段的 TypeError/ValueError（510 行），此错误发生在其后的 metrics 调用，直接冒泡到调用方。
  - 建议: 与 created_at 一致地做 `float(...) if ... else None` 归一化。
  - 置信度: 高

### 通过项
- 取消路径：`request_cancel` 非阻塞、`complete()` 对 CANCELLING 转 CANCELLED（不产出"取消后成功"）、worker `_run` 对 CANCELLING 立即终结。
- 持久化一致性：`persist` 临时文件同目录 + fsync + `os.replace` 原子替换，失败路径关闭 fd 并删除残留临时文件，旧快照不受影响。
- 恢复安全：`from_dict`/`restore_dict` 默认保守终结活动 job（JOB_INTERRUPTED），不会复活孤儿 worker 或伪造成功。
- 脱敏：日志、事件消息、manifest、checkpoint 均经 `redact_text`/`redact_mapping`（privacy_guard 已核实 `str()` 归一化，`redact_text(exc)` 传异常对象安全）。
- 事件表有界（500 条截断 + `prune_events` 负值校验）；metrics 对 cost_units 有 isinstance 防护。
- 类型注解完整；纯数据合约，无 Qt 信号递归、无 GPU/GLSL 维度。

---

## src/features/edit/ai_editing/keyframe_sampler.py (行数: 267)

### 发现

- [AUD-13705] [Suggestion] `derive_shot_spans` 对非 dict 的 cut 条目抛 AttributeError（try 只捕 TypeError/ValueError）
  - 位置: src/features/edit/ai_editing/keyframe_sampler.py:136-141
  - 证据:
    ```python
        for cut in scene_cuts:
            try:
                f = int(cut.get("frame", 0))
            except (TypeError, ValueError):
                continue
    ```
    若 `cut` 是字符串/None 等非 Mapping，`cut.get` 抛 AttributeError，不在捕获列表内。
  - 影响: 来自 `AssetAnalysis.scene_cuts`（历史工程/外部数据）的畸形条目会让整个派生函数崩溃，而不是按设计跳过该条目。
  - 建议: 捕获列表加入 AttributeError，或先判 `isinstance(cut, dict)`。
  - 置信度: 高

### 通过项
- 输入验证：`ShotSpan.__post_init__` 全字段校验；`total_frames <= 0` 返回空；边界点过滤 `0 < f < total_frames` 且排序去重。
- `keyframe_indices` 边界正确：duration 1/2/≥3 三分支核对无越界，head/mid/tail 去重保序；帧号均在 `[start, end)` 内。
- 红线符合：KeyframeManifest 只存帧号+shot+config_hash，不存图像/embedding（可重建缓存不进工程）。
- 纯函数、无 Qt/IO/GPU/线程；策略校验统一走 SUPPORTED_STRATEGIES；config_hash 稳定（sha256 前 16 hex）。
- 类型注解完整；StubFrameSampler 仅测试用途且 numpy 延迟导入。

---

## src/features/edit/ai_editing/kill_switch.py (行数: 286)

### 发现

- [AUD-13706] [Suggestion] `emergency_disable_all` 汇总事件以 AI_ASSISTANT 占位，审计口径失真且与逐能力事件重复
  - 位置: src/features/edit/ai_editing/kill_switch.py:219-227
  - 证据:
    ```python
            for c in ALL_CAPABILITIES:
                if self.disable(c, reason=reason):
                    count += 1
            if count > 0:
                self._events.append(KillSwitchEvent(
                    capability=Capability.AI_ASSISTANT,  # 占位（全关事件）
                    action="emergency_disabled_all",
    ```
  - 影响: 每个被关能力已各自记了一条 disabled 事件，追加的汇总事件又挂在其一（AI_ASSISTANT）名下：按 capability 统计事件数时 AI_ASSISTANT 双计，且事件流读起来像 AI_ASSISTANT 被额外禁用了一次，削弱 §15.4 可追溯性。代码注释已自知是占位。
  - 建议: 汇总事件 capability 设为 None（字段改 Optional）或去掉汇总事件、以 action 前缀/专用事件类型表达全关。
  - 置信度: 高

### 通过项
- disable/enable 幂等（已禁用/已启用返回 False 且不记事件）；`killable=False` 能力不可禁（返回 False）。
- verify_all_killable 语义正确（enabled ∧ ¬killable → 违规），且文档诚实声明"判定层未接线、不验证下游实际生效"。
- 纯判定层、不改 timeline、不持久化（符合红线声明）；类型注解完整；无 IO/线程维度问题。

---

## src/features/edit/ai_editing/llm_client.py (行数: 1615)

### 发现

- [AUD-13708] [Major] `OpenAICompatClient.complete` 在 provider 返回 null content 且 finish_reason 非 "length" 时返回 None，违反 str 返回协议
  - 位置: src/features/edit/ai_editing/llm_client.py:844（及 402-417、835-843）
  - 证据:
    ```python
    def _extract_choice_content(data: dict[str, Any]) -> tuple[Any, str]:
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
    ```
    ```python
            if (
                not content
                and finish_reason == "length"
                and thinking != {"type": "disabled"}
            ):
                content, _ = self._complete_request(...)
        return content  # type: ignore[return-value]
    ```
    content 保留 provider 原始值（可为 None）；防御性重试仅覆盖 `finish_reason == "length"` 一种情形。
  - 影响: 内容审查（finish_reason="content_filter"）或工具偏置等场景下 `content` 为 null、finish_reason 非 length，`complete()` 返回 None。调用方按协议注解当 str 使用（切片/拼接/strip）会抛 TypeError，且错误不带 LLMError 上下文，难排查。
  - 建议: 非 length 的空/None content 显式抛 `LLMError(code="LLM_INVALID_RESPONSE")`（可附 finish_reason），保证返回值恒为 str。
  - 置信度: 中（机制确凿；触发依赖 provider 在非 length finish 下返回 null content）

- [AUD-13709] [Major] `OpenAIResponsesClient` 把 `max_tokens=None` 强制为 1024，违反 LLMClient 协议"None 用后端默认"的约定，长输出被静默截断
  - 位置: src/features/edit/ai_editing/llm_client.py:1149、1188（协议文档 118 行）
  - 证据:
    ```python
            max_tokens: 最多生成多少 token（None 用后端默认）。   # LLMClient 协议，118 行
    ```
    ```python
        body: dict[str, Any] = {
            "model": self._profile.model_id,
            "input": input_items,
            "max_output_tokens": max_tokens if max_tokens is not None else 1024,
            "reasoning": {"effort": "low"},
    ```
    complete 与 stream_complete 两处同样处理；OpenAI Responses API 在省略 max_output_tokens 时本有模型级默认。
  - 影响: 调用方按协议传 None 期望"后端默认/全额预算"，实际只给 1024 token：长文档（生成式 HTML 等）被截断，complete() 再以"任务可能因输出上限未完成"报错或返回半截内容——可恢复却表现为故障。
  - 建议: None 时省略 `max_output_tokens` 字段交由后端默认；确需兜底也应设为远高于 1024 的文档化值。（注：AnthropicClient 1476/1509 行同样默认 1024，但 Messages API 强制要求该字段，属必要默认，仅需在文档标注。）
  - 置信度: 中（截断影响取决于调用方是否以 None 请求长生成；协议违背本身确凿）

- [AUD-13710] [Suggestion] 空 content 防御性重试使用原始 `max_tokens` 而非阶梯降级后的 `budget`，可再次触发 max_tokens 400 直接失败
  - 位置: src/features/edit/ai_editing/llm_client.py:842（及 925）
  - 证据:
    ```python
                budget = next_budget
        ...
                content, _ = self._complete_request(
                    messages, thinking={"type": "disabled"}, json_mode=json_mode,
                    temperature=temperature, max_tokens=max_tokens,   # 842 行：非 budget
                )
    ```
    stream_complete 的同型重试（925 行）同样传 `max_tokens=max_tokens`。
  - 影响: 当阶梯已因 provider 上限把 budget 降下来、且小预算下 thinking 仍烧光额度（空 content + finish=length）时，重试用原始大 max_tokens 会再次被 400 拒绝并直接抛出——明知合法预算却失败。
  - 建议: 两处重试改传 `budget`（stream 路径即 ladder 后的 budget）。
  - 置信度: 高（代码确凿；触发需多条件叠加）

- [AUD-13711] [Suggestion] `_upload_file` 轮询超时后不报错，且把未就绪的 file_id 写入缓存
  - 位置: src/features/edit/ai_editing/llm_client.py:1279-1297
  - 证据:
    ```python
        if status in {"processing", "queued", "pending"}:
            deadline = time.monotonic() + min(60.0, max(10.0, self._policy.timeout_seconds))
            while time.monotonic() < deadline:
                time.sleep(1.0)
                state = _get_json(...)
                status = str(state.get("status") or "").lower()
                if status in {"processed", "ready", "completed", "succeeded", "active"}:
                    break
                if status in {"failed", "error", "cancelled"}:
                    raise LLMError(...)
        self._uploaded_file_ids[cache_key] = file_id
        return file_id
    ```
  - 影响: 大附件预处理超 60 秒时循环自然退出，既不报错也不确认状态就返回并缓存 file_id；后续请求用未就绪文件得到 provider 侧模糊报错，且缓存导致重试也复用同一个未就绪 id，无法自愈。
  - 建议: deadline 耗尽且状态仍非就绪时抛 `LLM_ERROR`（code 如 LLM_FILE_PROCESSING_TIMEOUT，retryable=True），并不写入缓存。
  - 置信度: 高

- [AUD-13712] [Suggestion] `_prepare_responses_content` 对"带 file_id、无 file_path"的块原样透传内部元数据（filename/mime），与自身"不得发送内部元数据"的约束不一致
  - 位置: src/features/edit/ai_editing/llm_client.py:1304-1307（对照 1327-1333 注释与 1366-1374）
  - 证据:
    ```python
            file_path = str(block.get("file_path") or "")
            if not file_path:
                prepared.append(block)
                continue
    ```
    而 `_to_responses_content` 生成的 file 块携带内部字段：
    ```python
            block: dict[str, Any] = {
                "type": input_type,
                "filename": str(part.get("filename") or "attachment"),
                "mime": str(part.get("mime") or "application/octet-stream"),
            }
            if part.get("file_id"):
                block["file_id"] = str(part["file_id"])
    ```
    1327-1328 行注释明确："filename/mime/media_kind 是项目内部元数据，不能原样发送给 Responses"——但该剥离只发生在上传分支。
  - 影响: 直接携带 file_id 的附件（跳过本地路径）会把 filename/mime 一并发给 provider，严格网关可能因未知字段返回 400。
  - 建议: 无 file_path 的块也按 `{"type": ..., "file_id": ...}` 白名单重组后再 append。
  - 置信度: 高

- [AUD-13713] [Suggestion] `_post_multipart_file` 在 getsize 与实际读取之间无一致性保证，文件变动时 Content-Length 与实际字节不符
  - 位置: src/features/edit/ai_editing/llm_client.py:286、289-294（size 取自 255 行）
  - 证据:
    ```python
        conn.putheader("Content-Length", str(len(prefix) + size + len(suffix)))
        ...
            with open(file_path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
    ```
  - 影响: 上传期间文件被追加（日志、录制中的文件）会多发字节、被截断会少发：HTTP 帧长错位，轻则 provider 400/连接重置，重则把多余字节留给下一次响应解析（http.client 可能抛 BadStatusLine 类 OSError，被当作可重试网络错误反复重传 512MB 文件）。
  - 建议: 发送循环内累计已发字节数并在达到 Content-Length 预算时停止/报错；或上传前校验最终读取字节数 == size 再补发/截断。
  - 置信度: 高（机制确凿；触发需文件并发修改，概率低）

- [AUD-13714] [Suggestion] `_local_document_text` 对解压后 XML 无大小上限，恶意 Office 附件可造成内存放大（zip bomb）
  - 位置: src/features/edit/ai_editing/llm_client.py:707、711（及 686-691、732-734）
  - 证据:
    ```python
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    ...
            for name in names:
                root = ET.fromstring(archive.read(name))
    ...
                if sum(len(item) for item in chunks) >= max_chars:
                    break
        return "\n".join(chunks)[:max_chars]
    ```
    `max_chars`（120k）限制只在抽取完成后生效；`archive.read(name)` 会把单个 XML 成员完整解压进内存。512MB 的入口上限（1252-1258 行）针对的是压缩后大小。
  - 影响: 高压缩比恶意 docx/xlsx（用户附件属外部数据）可用小文件解压出 GB 级 XML，使后台 worker OOM/长卡顿；pptx 路径还会对每个 slide 全量解压。
  - 建议: 解压前用 `archive.getinfo(name).file_size` 过滤超限成员（如 >64MB），或用流式读取（ZipExtFile 限量读）。
  - 置信度: 高（机制确凿；需要用户主动附加恶意文件）

- [AUD-13715] [Suggestion] `make_client` 未知 provider_kind 的报错信息未列全受支持值
  - 位置: src/features/edit/ai_editing/llm_client.py:1612-1615
  - 证据:
    ```python
        raise LLMError(
            f"不支持的 provider_kind: {profile.provider_kind!r}"
            "（可选：openai / ollama / anthropic）"
        )
    ```
    而上方 1587-1598 行实际还接受 "deepseek"、"openai_responses"、"volcengine_responses"、"anthropic"/"claude" 及兼容旧值。
  - 影响: 用户配置错误时得到不完整（甚至误导，遗漏 deepseek/responses 系）的可选值提示，增加排障成本。
  - 建议: 由各分支的 kind 元组拼接生成提示，或手工补全为 "openai / ollama / deepseek / openai_responses / volcengine_responses / anthropic"。
  - 置信度: 高

### 通过项
- 重试与退避：指数退避封顶 max_backoff_seconds，Retry-After 仅取数值且仍受上限约束；三处 HTTP 路径（post/stream/multipart）的 retry 判定与错误码（AUTH/PERMISSION/ENDPOINT/RATE_LIMIT/NETWORK）稳定、可重试标记正确。
- 流式重试正确性：仅在 `not received`（未向 UI 投递任何 delta）时重试，不会重复推送；断流按协议抛带 code 的 LLMError。
- max_tokens 阶梯严格递减（`next_budget >= budget → raise`），无死循环；非 401/403 才认定为 max_tokens 拒绝。
- multipart 上传分块流式（1MB chunk），不整体读入内存；512MB 上限前置失败。
- 预算控制：三个 client 均在请求前 `_check_budget`，用量经 CostLedger 记录（含 estimated 标记）；重试产生的多次真实调用分别记账，不漏记不虚记。
- 错误信息与日志经 redact_text 脱敏（key/绝对路径）；工厂对废弃 kind 向后兼容并 logger.warning。
- URL 解析 `_resolve_api_endpoint` 对裸 host/带版本段/完整端点三种输入处理正确（含 /completions 别名）；上传缓存键含 mtime+size，文件变更不会误用旧 file_id。
- 工厂/客户端构造对 endpoint、model_id、Anthropic api_key 做前置校验；profile.limits 属性已在 shared 实现中核实存在。
- 类型注解完整（唯一的 type: ignore 有明确原因标注）；无 Qt/UI 线程操作（HTTP 由调用方放后台线程，模块文档明确）。

---

## src/features/edit/ai_editing/llm_cost.py (行数: 86)

### 发现

- [AUD-13707] [Suggestion] `can_spend` 与 `record` 是两次独立加锁，check-then-act 非原子，并发请求可小幅超出预算
  - 位置: src/features/edit/ai_editing/llm_cost.py:54-55（对照 42-48、50-52）
  - 证据:
    ```python
        def record(self, record: CostRecord) -> None:
            with self._lock:
                self._records.append(record)
    ...
        def can_spend(self, limit: float | None) -> bool:
            return limit is None or self.total_cost() < limit
    ```
    预算检查（llm_client._check_budget）与记账之间没有事务性；total_cost() 单独持锁读取。
  - 影响: 两个后台 worker 同时通过 can_spend 检查后各自发起请求，都会记账——最终成本可超出 limit 最多约一个请求的量。对"预算阻断"语义是软超支，通常可接受但未声明。
  - 建议: 如需硬预算，提供 `try_reserve(amount) -> bool` 在单锁内完成检查+预留；或在文档声明预算为"尽力而为（best-effort）"。
  - 置信度: 高（竞争机制确凿；影响幅度有界）

### 通过项
- RLock 保护一致，records()/total_cost()/snapshot() 读路径均持锁拷贝，无迭代竞争。
- total_cost 对 cost_units=None 安全（`or 0.0`）；frozen dataclass + to_dict 完整；make_cost_record 工厂默认值合理。
- 类型注解完整；无 Qt/IO/GPU 维度。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-13701, AUD-13702, AUD-13708, AUD-13709 |
| Suggestion | 11 | AUD-13703, AUD-13704, AUD-13705, AUD-13706, AUD-13707, AUD-13710, AUD-13711, AUD-13712, AUD-13713, AUD-13714, AUD-13715 |

共性观察：本批次整体质量较高（错误码体系、脱敏、原子持久化、取消语义都做得扎实）。主要风险集中在两处：jobs.py 在自家默认配置（max_workers=2）下违反自身"单 worker 不加锁"前提；llm_client.py 的 Responses 路径在 max_tokens=None 与 null content 两个边界上偏离自己声明的 str/后端默认协议。
