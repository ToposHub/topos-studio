# 审查报告：batch-136 — src/features/edit/ai_editing/

- 日期：2026-09-28
- 编号区间：AUD-13600–AUD-13699
- 审查文件：8 个（2,903 行）
- 统计：Critical 0 / Major 4 / Suggestion 10

审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
说明：本批次为 AI 生成/目标监督/帮助索引纯 Python 层，无 GLSL、无 OpenGL 资源、无 Qt 信号连接；GPU/GLSL/信号防递归三维在各文件均不适用（在通过项中注明）。

---

## src/features/edit/ai_editing/generation_models.py (行数: 12)

### 发现

（无）

### 通过项
- 纯 re-export shim，通过（无逻辑）：`_sys.modules[__name__] = _impl` 把本模块条目替换为 `src/shared.ai_models.generation.generation_models`，历史导入路径（含私有名）全部解析到 shared 实现。
- 输入验证 / 异常路径 / 线程安全 / GPU/GLSL/信号防递归：不适用（无逻辑）。

---

## src/features/edit/ai_editing/generation_provider.py (行数: 255)

### 发现

- [AUD-13608] [Suggestion] ProviderRegistry 无锁，注册与查找可跨线程竞争
  - 位置: src/features/edit/ai_editing/generation_provider.py:109
  - 证据:
    ```python
    mods = modalities if modalities is not None else tuple(provider.supported_modalities)
    if not mods:
        raise ValueError(f"provider {pid} declares no modalities")
    self._providers[pid] = provider
    ```
    以及 preferred_for 的退化遍历（generation_provider.py:129）：
    ```python
    for p in self._providers.values():
        if modality in tuple(p.supported_modalities):
            return p
    ```
  - 影响: `register`（如运行时换 provider）与 `preferred_for/list_providers` 并发时可能触发 `RuntimeError: dictionary changed size during iteration`；当前宿主（ai_editing_mixin）在初始化期一次性注册，实际风险低。
  - 建议: 与 goal_round_driver/goal_supervisor 同风格加 `threading.RLock` 保护 `_providers/_preferred` 的读写。
  - 置信度: 高

### 通过项
- 输入验证：register 校验空 provider_id / 空 modalities，require 缺失抛 KeyError 带 id。
- 异常与取消路径：stub 对不支持 modality 返回类型化 `UNSUPPORTED_MODALITY` 错误码，不抛裸异常。
- 状态提交一致性：stub 确定性 asset_id/provenance（prompt 只存 hash 摘要）。
- 性能模式：stub 仅 O(候选数) hash 计算；progress 回调频率合理。
- 类型注解完整。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（无 GPU、无 Qt）。

---

## src/features/edit/ai_editing/generation_provider_pack.py (行数: 564)

### 发现

- [AUD-13600] [Major] _normalize_result 对外部 payload 数值字段直接 float()，非数值内容抛 ValueError 穿透 poll/generate
  - 位置: src/features/edit/ai_editing/generation_provider_pack.py:550
  - 证据:
    ```python
                cost_units=float(item.get("cost_units", 0.0) or 0.0),
                preview_handle=handle,
                expected_sha256=str(item.get("sha256", item.get("checksum", "")) or ""),
                duration_seconds=(
                    float(item["duration_seconds"]) if item.get("duration_seconds") is not None else None
                ),
    ```
    以及 generation_provider_pack.py:556：`actual_cost_units=float(payload.get("actual_cost_units", 0.0) or 0.0),`
  - 影响: 远端/transport 返回 `"duration_seconds": "unknown"`、`"cost_units": "0.5USD"` 等非数值字符串时，`float()` 抛 `ValueError`，经 `_from_payload`→`poll`→`ProviderPackGenerationAdapter.generate` 一路上抛，把本应归类为 `GEN_PROVIDER_SCHEMA_INVALID` 的外部数据错误变成未类型化异常，整 job 以非预期方式失败（违反"外部数据须验证"铁律）。同文件的 http_generation_provider 用安全 `_float/_int` 做了防护，本处缺失。
  - 建议: 对齐 http_generation_provider 的 `_float/_int` 安全转换模式，转换失败时返回 `GenerationResult(error_code="GEN_PROVIDER_SCHEMA_INVALID")`。
  - 置信度: 高

- [AUD-13604] [Suggestion] SeedanceVideoProviderPack.poll/cancel 对未知 job_id 抛裸 KeyError，与 ContractMock 错误契约不一致
  - 位置: src/features/edit/ai_editing/generation_provider_pack.py:371
  - 证据:
    ```python
    def poll(self, job_id: str) -> ProviderJob:
        request = self._requests[job_id]
        job = self._from_payload(job_id, request, self.transport.poll(job_id))
    ```
    以及 cancel（generation_provider_pack.py:386-387）：`request = self._requests[job_id]`；对照 ContractMockProviderPack 的显式契约（generation_provider_pack.py:317-320）：`raise KeyError(f"provider job not found: {job_id}")`
  - 影响: 进程重启后未走 resume 直接 poll/cancel（job_id 来自持久化 checkpoint）得到无上下文 `KeyError: <job_id>`；adapter 的 is_cancelled 分支调用 `self.pack.cancel(...)`（generation_provider_pack.py:477）也未包异常，取消路径可能以裸异常终止。
  - 建议: 统一走 `_require` 风格的显式错误（带 pack/job 上下文的 KeyError 或类型化 FAILED job），adapter 取消调用处补 try/except。
  - 置信度: 高

- [AUD-13605] [Suggestion] _normalize_result 形参注解只写 GenerationRequest，实际还会接收 _ResumableRequest
  - 位置: src/features/edit/ai_editing/generation_provider_pack.py:515
  - 证据:
    ```python
    def _normalize_result(
        provider_id: str,
        model_id: str,
        request: GenerationRequest,
        payload: Mapping[str, Any],
    ) -> GenerationResult | None:
    ```
    而调用方 `_from_payload`（generation_provider_pack.py:392-396）声明 `request: GenerationRequest | _ResumableRequest`，resume 路径传入 `_ResumableRequest`（generation_provider_pack.py:383-384）。
  - 影响: 注解与真实鸭子类型合同不符，误导调用者/类型检查器；当前 duck-typing（prompt_hash/budget/recipe/effective_* 属性齐备）运行正常。
  - 建议: 注解改为 `GenerationRequest | _ResumableRequest`，或提取共同 Protocol（prompt_hash/budget/recipe/effective_reference_*）。
  - 置信度: 高

- [AUD-13606] [Suggestion] 远端已取消的任务在 adapter 终态回退中被误标为 GEN_PROVIDER_FAILED
  - 位置: src/features/edit/ai_editing/generation_provider_pack.py:427
  - 证据:
    ```python
            error_code="GEN_PROVIDER_FAILED" if state == ProviderJobState.FAILED else "",
    ```
    配合 adapter 终态回退（generation_provider_pack.py:470-475）：
    ```python
            if job.state.terminal:
                return job.result or GenerationResult(
                    request_id=request.request_id,
                    error_code=job.error_code or "GEN_PROVIDER_FAILED",
    ```
  - 影响: transport 轮询返回 `status=cancelled` 时 `_from_payload` 置 CANCELLED 但 error_code 为空，adapter 兜底填 `GEN_PROVIDER_FAILED`——远端取消被当成失败上报，宿主无法区分"被取消"与"失败"（本地主动取消路径则正确返回 GEN_PROVIDER_CANCELLED）。
  - 建议: `_from_payload` 对 CANCELLED 也设置 `GEN_PROVIDER_CANCELLED`，或 adapter 兜底按 `job.state` 选择错误码。
  - 置信度: 高

- [AUD-13607] [Suggestion] SeedanceVideoProviderPack 的 _jobs/_requests 字典无锁，并发 poll/cancel 有竞态
  - 位置: src/features/edit/ai_editing/generation_provider_pack.py:352
  - 证据:
    ```python
        self._requests: dict[str, GenerationRequest | _ResumableRequest] = {}
        self._jobs: dict[str, ProviderJob] = {}
    ```
  - 影响: 宿主在 worker 线程 poll、UI 线程 cancel 时可能丢失最后一次 `_jobs[job_id]` 状态更新（GIL 下不至崩溃，但状态回读可能滞后/被覆盖）。
  - 建议: 参照同目录 goal_round_driver 的 RLock 模式保护两个字典。
  - 置信度: 高

### 通过项
- 状态提交一致性：checkpoint 仅存脱敏 request 元数据（prompt hash、asset id），resume 用 `_ResumableRequest.from_checkpoint` 严格校验（modality/request_id/prompt_hash 缺失即 ValueError）。
- 异常与取消路径：adapter 超时后主动 cancel 远端任务且 cancel 失败不掩盖超时错误；`max_polls=1` 的终态先于超时分支处理（有注释说明的修复）。
- 输入验证：create 校验 modality、payload job_id 缺失返回 `GEN_PROVIDER_SCHEMA_INVALID`；outputs 非列表/元素非 Mapping/缺 URL 均 fail-closed。
- 性能模式：adapter 轮询带 sleep_fn 注入、上限 max_polls；无忙等。
- GPU/GLSL·HDR/信号防递归：不适用（无 GPU、无 Qt）。

---

## src/features/edit/ai_editing/goal_round_driver.py (行数: 162)

### 发现

- [AUD-13609] [Suggestion] execute() 以 bool(result) 判定成败，void/None 返回的操作会发出 goal.round.failed 事件
  - 位置: src/features/edit/ai_editing/goal_round_driver.py:98
  - 证据:
    ```python
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001 - release before propagating
            self.complete(reservation, success=False, reason=type(exc).__name__)
            raise
        self.complete(reservation, success=bool(result))
    ```
  - 影响: 宿主 operation 合法返回 None/空值时，预订虽正确释放，但事件流会记录 `goal.round.failed`（reason 为空），观测/审计语义失真。
  - 建议: 增加 `success: bool | None = None` 显式参数，或约定 None 视为成功，仅 False 视为失败。
  - 置信度: 高

### 通过项
- 线程安全：所有 `_active` 读写均在 RLock 内，reserve/start/complete/cancel 幂等且校验 reservation_id。
- 异常与取消路径：operation 抛异常先释放预订再 re-raise；`_emit` 捕获所有异常防止诊断链路成为失败路径（有注释说明，符合防御性设计而非宽泛吞错）。
- 状态提交一致性：cancel 主动弹出预订后，execute 的 complete 正确返回 False，无悬挂预订。
- 输入验证：goal_id 空/空白拒绝；round_number/expected_revision 下限钳制。
- 性能模式：无锁外工作、事件 payload 限长（reason[:160]）。
- 类型注解完整。
- GPU/GLSL·HDR/信号防递归：不适用。

---

## src/features/edit/ai_editing/goal_supervisor.py (行数: 659)

### 发现

- [AUD-13603] [Major] run() 未捕获 GoalRevisionConflict，与 record_cycle 的冲突处理不对称
  - 位置: src/features/edit/ai_editing/goal_supervisor.py:334
  - 证据:
    ```python
        if previous is not None:
            if previous.scope_hash and previous.scope_hash != goal.scope_hash:
                checkpoint = self._save(GoalCheckpoint(
                    goal_id=goal.goal_id,
                    status=GoalStatus.WAITING_FOR_USER,
                    scope_hash=goal.scope_hash,
                    reason="goal scope changed; fresh planning is required",
                    started_at=previous.started_at,
                ), expected_revision=previous.revision)
    ```
    `_finish`（goal_supervisor.py:583）同样裸调 `self._save(...)`；而 `_save` 在 compare 失败时抛异常（goal_supervisor.py:630-633）：
    ```python
                if not compare_and_save(
                    checkpoint.goal_id, int(expected_revision), candidate,
                ):
                    raise GoalRevisionConflict(checkpoint.goal_id)
    ```
    对照 record_cycle 的处理（goal_supervisor.py:461-462）：`except GoalRevisionConflict: return self._stale_result(goal)`
  - 影响: run() 与异步 record_cycle 并发操作同一 goal（模块设计明确支持的组合，GoalRevisionConflict 即为此而设）时，run() 的 scope-changed 分支或 _finish 以未类型化 `GoalRevisionConflict` 冒泡，违反 run() 返回 `GoalRunResult` 的契约，宿主同步路径收到异常而非可展示的 goal 状态。缓解：当前仓库生产代码只调用 record_cycle（src/app/controllers/ai_edit_controller.py:5513），run() 无生产调用方。
  - 建议: run() 内两处 `_save` 也包 `except GoalRevisionConflict: return self._stale_result(goal)`。
  - 置信度: 中（代码不对称为事实；实际触达需宿主并发使用 run()）

- [AUD-13610] [Suggestion] GoalSpec.from_dict 对超 32 条 constraint_refs 静默截断，随后以误导性 scope hash 错误失败
  - 位置: src/features/edit/ai_editing/goal_supervisor.py:120
  - 证据:
    ```python
        spec = cls(
            goal_id=str(data.get("goal_id", "")),
            project_id=str(data.get("project_id", "")),
            timeline_id=str(data.get("timeline_id", "")),
            objective_ref=str(data.get("objective_ref", "")),
            constraint_refs=tuple(str(item) for item in constraints[:32]),
        )
        if data.get("scope_hash") and str(data["scope_hash"]) != spec.scope_hash:
            raise ValueError("goal scope hash mismatch")
    ```
  - 影响: 构造路径 `GoalSpec.__post_init__` 对 >32 条约束直接抛 "too many goal constraints"（goal_supervisor.py:80-81），而 from_dict 截断后按全量算出的旧 scope_hash 比对失败，报出的是费解的 "goal scope hash mismatch" 而非真实原因（fail-closed 但报因错误）。
  - 建议: from_dict 对 `len(constraints) > 32` 先抛 "too many goal constraints"，与构造路径一致。
  - 置信度: 高

### 通过项
- 线程安全：GoalSupervisor 无共享可变状态，InMemoryGoalCheckpointStore 全操作 RLock 保护，compare_and_save 原子提交（revision 必须恰好 +1）。
- 状态提交一致性：checkpoint 持久化经 sanitize_for_storage + `_safe_reason` 双重脱敏（含 prompt/secret 关键字整体打码）；revision 乐观并发令牌端到端贯通（run/record_cycle/resume）。
- 输入验证：GoalLimits/GoalSpec/GoalCycleResult/GoalCheckpoint.from_dict 均严格校验（schema_version、类型、上限、负数计数拒绝）；import_all 对坏行跳过不中断。
- 异常与取消路径：runner 异常 fail-closed 转 FAILED 且 reason 经 redact_text；run 循环每轮检查 control() 的 CANCEL/PAUSE，预算超限走 BUDGET_EXCEEDED；record_cycle 对 WAITING_FOR_USER/PAUSED 门禁 resume_interactive。
- 性能模式：completed_step_ids 截断 256、reason 截断 500，防无界增长；store 淘汰按 updated_at 有界（max_items）。
- 类型注解完整。
- GPU/GLSL·HDR/信号防递归：不适用。

---

## src/features/edit/ai_editing/help_index.py (行数: 409)

### 发现

- [AUD-13612] [Suggestion] _feature_enabled 对每条 shortcut/panel 重复调用 list_features()，构建期 O(n·m) 且依赖源端缓存
  - 位置: src/features/edit/ai_editing/help_index.py:389
  - 证据:
    ```python
    @staticmethod
    def _feature_enabled(sources: HelpSources, feature_id: Any) -> bool:
        """查询某 feature 是否启用（feature_id 为 None 时总是 True）。"""
        if feature_id is None:
            return True
        features = sources.list_features()
    ```
    每个快捷键条目（help_index.py:292）与每个面板条目（help_index.py:369）都触发一次：`enabled = self._feature_enabled(sources, feature_id)`
  - 影响: 若注入的 HelpSources.list_features() 每次重建列表（协议未约定缓存），几百条快捷键 × 几十条 feature 的重复扫描与对象重建放大构建耗时；仅 build 期一次性成本。
  - 建议: build() 内先取一次 features 构建 `{feature_id: enabled}` 映射传入各 _add_* 方法。
  - 置信度: 高

- [AUD-13611] [Suggestion] 未使用的导入 field、Callable
  - 位置: src/features/edit/ai_editing/help_index.py:34
  - 证据:
    ```python
    import logging
    from dataclasses import dataclass, field
    from typing import Any, Callable, Protocol
    ```
  - 影响: 全文无 `field(` 与 `Callable` 使用（HelpSources 协议方法均为具体签名），死导入造成噪音。
  - 建议: 删除 `field` 与 `Callable`。
  - 置信度: 高

### 通过项
- 状态提交一致性（知识红线）：get/explain_diagnostic 找不到返回 None 不编造；条目带 source_type/source_ref 可回溯来源。
- 输入验证：各源条目缺 code/id/pid/feature_id 跳过；capabilities 的 effects 兼容 dict/str 两形态。
- 性能模式：search 为一次线性扫描 + 子串匹配 + 标题加权排序，本地索引规模下可接受；`list_by_category`/`all_entry_ids` O(n) 只读。
- 线程安全：build 后只读（dict 不再变更），无需锁。
- 类型注解完整。
- GPU/GLSL·HDR/信号防递归：不适用（无 Qt 依赖，模块 docstring 明确）。

---

## src/features/edit/ai_editing/http_generation_provider.py (行数: 567)

### 发现

- [AUD-13601] [Major] 非法 Content-Length 头使 int() 抛 ValueError 穿透类型化错误契约
  - 位置: src/features/edit/ai_editing/http_generation_provider.py:374
  - 证据:
    ```python
                    raw_length = response.headers.get("Content-Length")
                    if raw_length and int(raw_length) > self._max_response_bytes:
                        raise _HttpAdapterError("GEN_PROVIDER_RESPONSE_TOO_LARGE", "provider response exceeds limit")
    ```
    而 generate() 只捕获 `_HttpAdapterError`（http_generation_provider.py:184-189）：
    ```python
        try:
            submitted = self._request_json(
                "POST", self._url(self._routes.submit), headers, body,
            )
        except _HttpAdapterError as exc:
            return self._error(request, exc.code, exc.message)
    ```
  - 影响: 服务器/代理返回非数字 Content-Length（如 `"12x3"`、`"1e3"`）时 `int(raw_length)` 抛裸 `ValueError`，不被 `_HttpAdapterError`/HTTPError/URLError/OSError 任一分支捕获，从 `_request_json` 直接冒泡出 `generate()`/`cancel()`，违反本模块"失败返回带 error_code 的 GenerationResult"的契约（外部数据须验证铁律）。
  - 建议: 包一层 `try: length = int(raw_length) except ValueError: length=None`（视为未知长度，靠后续 `read(max+1)` 截断检查兜底）。
  - 置信度: 高

- [AUD-13602] [Major] _last_remote_job_id 为实例级共享状态，共享 worker 并发 generate 时互相覆盖，取消可能指向错误任务
  - 位置: src/features/edit/ai_editing/http_generation_provider.py:204
  - 证据:
    ```python
        self._last_remote_job_id = remote_job_id
        self._emit_checkpoint(checkpoint, request, remote_job_id, "running")
    ```
    resume 路径同样写入（http_generation_provider.py:173-174）：
    ```python
            remote_job_id = str(resume_remote_job_id)
            self._last_remote_job_id = remote_job_id
    ```
    且该属性被文档化为宿主取消入口（http_generation_provider.py:135-137）：`"""Most recently submitted remote job, useful for host-side cancellation."""`
  - 影响: 生产接线（src/app/controllers/ai_editing_mixin.py:730）按 profile 只注册一个 provider 实例，GenerationService 使用共享 JobWorkerPool（src/features/edit/ai_editing/jobs.py:118 默认 `max_workers=2`）——两个并发 generate() 时 `_last_remote_job_id` 指向最后提交的任务，宿主读它取消会取消错任务；模块本身提供的逐请求 `checkpoint` 回调才是正确通道。
  - 建议: 废弃/降级 `last_remote_job_id`（标注 deprecated，指引使用 checkpoint 回调），或改为 `{request_id: remote_job_id}` 映射并加锁。
  - 置信度: 高

### 通过项
- 输入验证：endpoint scheme/netloc 校验、modality 白名单过滤（非法 profile modality 静默忽略）、凭据缺失类型化错误、参考素材与 allows_media_upload 数据政策门禁、响应体积双重上限（Content-Length + 实读字节数）、JSON 必须为对象、输出必须带 URL、_float/_int 安全转换。
- 异常与取消路径：重试仅限 408/429/5xx/网络错误且指数退避封顶 8s；401/403→AUTH_FAILED、429→RATE_LIMITED；幂等键（request_id）保证 POST 重试安全；轮询超时/用户取消均 emit checkpoint 并返回类型化错误码。
- 状态提交一致性：RemoteGenerationCheckpoint 仅含 request_id/provider_id/remote_job_id/state/resumable，无 prompt 无凭据。
- 线程安全：除 `_last_remote_job_id`（已记 AUD-13602）外无可变共享状态；secret 只进请求头，不入日志/provenance。
- 性能模式：轮询间隔可注入、上限 max_poll_seconds（默认 15 分钟），progress 单调且封顶 0.95。
- 类型注解完整。
- GPU/GLSL·HDR/信号防递归：不适用。

---

## src/features/edit/ai_editing/image_attachment.py (行数: 275)

### 发现

- [AUD-13613] [Suggestion] _PNG_MAGIC 常量定义后未使用
  - 位置: src/features/edit/ai_editing/image_attachment.py:38
  - 证据:
    ```python
    IMAGE_TOKEN_ESTIMATE = 765
    _PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    ```
  - 影响: 全文（含 encode/解析函数）无任何 `_PNG_MAGIC` 引用，属遗留死代码，误导读者以为存在 PNG 魔数校验路径。
  - 建议: 删除该常量，或如需支持 PNG data URL 时补上真正使用它的 mime 嗅探。
  - 置信度: 高

### 通过项
- 输入验证：null QImage 拒绝、JPEG 编码失败/空 buffer 抛明确 ValueError、FileAttachment 空 path/负 size 拒绝并自动补 filename。
- 状态提交一致性（隐私红线）：ImageAttachment.to_dict 不含 data_url；FileAttachment.to_dict 不含本地路径与字节；extract_text_from_content 只保留文本与文件名占位符。
- 线程安全 / GPU 生命周期：QImage 为 CPU 侧编码（scaled/convertToFormat/JPEG），惰性导入 PyQt6，不触碰 OpenGL，可在非 UI 线程执行。
- 异常与取消路径：QBuffer 以 try/finally 确保 close。
- 性能模式：最长边降采样 1024 + JPEG 质量 85 控制 token/体积；token 估算按固定常数避免按 base64 长度高估。
- 类型注解完整（含 TypeAlias ChatAttachment）。
- GLSL·HDR / 信号防递归：不适用。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-13600, AUD-13601, AUD-13602, AUD-13603 |
| Suggestion | 10 | AUD-13604–AUD-13613 |

Major 主题：外部 payload 数值未验证即 float()（pack）、非法 Content-Length 崩穿类型化契约（http）、并发下取消指向错误远端任务（http）、run() 缺少 revision 冲突处理（goal_supervisor）。
