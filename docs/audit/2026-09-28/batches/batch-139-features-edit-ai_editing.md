# 批次报告：batch-139 [P1] src/features/edit/ai_editing/

- 审查日期：2026-09-28
- 编号区间：AUD-13900–AUD-13999（实际使用 AUD-13900–AUD-13923）
- 文件数：8（2,067 行）
- 计数：Critical 0 · Major 11 · Suggestion 13
- 说明：本批全部为纯服务层/模型层模块，不涉及 ModernGL、GLSL、Qt 信号，相关维度按"不涉及"记录。发现均已对照 jobs.py / generation_bin.py / agent_models.py / session_models.py / generated_media_materializer.py / provider_profiles.py（shared）等依赖模块的真实契约核实。

---

## src/features/edit/ai_editing/media_generation_service.py (行数: 497)

### 发现

- [AUD-13900] [Major] 月度成本告警被计算后直接丢弃，告警链路整体失效
  - 位置: src/features/edit/ai_editing/media_generation_service.py:439-444
  - 证据:
    ```python
            # cost_ledger 累计检查（warning 级，不硬拒）
            if self._cost_ledger is not None:
                alert = self._cost_ledger.check_alert(provider.provider_id)
                if alert is not None:
                    # alert 是提醒，返回 warning 诊断（service 仍继续；调用方可据此提示用户）
                    pass  # v1：不阻断，仅 ledger 内部记录
            return []
    ```
    而方法 docstring（429-431 行）承诺"若已有 cost_ledger 且本月累计超 ``monthly_cost_alert``，发 warning"。
  - 影响: `check_alert` 的返回值被 `pass` 丢弃，且 ledger 内部也未记录任何告警（`check_alert` 只读不写）。§10.2 月度成本告警永远无法到达调用方/UI，用户在持续超支时得不到任何提醒；`alert` 变量未使用。
  - 建议: 在 `_check_cost` 的返回值中加入 warning 级 Diagnostic（携 `check_alert` 消息），或由 service 暴露告警回调/事件，让宿主可提示用户。
  - 置信度: 高

- [AUD-13901] [Major] CostLedger.add 为无锁读-改-写，多 worker 并发下丢失成本累计
  - 位置: src/features/edit/ai_editing/media_generation_service.py:468-472（调用点 309-314）
  - 证据:
    ```python
        def add(
            self, *, provider_id: str, modality: GenerationModality, cost_units: float,
        ) -> None:
            """累加一次生成的实际费用。"""
            self._totals[provider_id] = self._totals.get(provider_id, 0.0) + cost_units
    ```
    `add` 仅在 `_execute_generation`（309-314 行）中调用，而该函数经 `request_generation_async` 运行在 `JobWorkerPool`（jobs.py:118 默认 `max_workers=2`）的工作线程上。
  - 影响: 两个并发生成完成时对同一 provider 的 `get + 写回` 竞争，累计成本被覆盖丢失 → `check_alert`/`grand_total` 低估，进一步弱化成本上限治理（§10.2）。
  - 建议: 为 `CostLedger` 增加 `threading.Lock`（或改用 `collections.defaultdict` + 锁内累加），`add`/`reset` 在锁内执行。
  - 置信度: 高

- [AUD-13902] [Major] 取消检查与候选入箱之间存在 TOCTOU：取消后候选仍进入素材箱
  - 位置: src/features/edit/ai_editing/media_generation_service.py:262-264 与 316-321
  - 证据:
    ```python
            if job.status == JobStatus.CANCELLING:
                job.complete({})
                return []
    ```
    ```python
            self._bin.add_candidates(result.candidates)
            job.complete(result_manifest={
                "request_id": request.request_id,
                "candidate_ids": [c.asset_id for c in result.candidates],
                "actual_cost_units": result.actual_cost_units,
            })
    ```
    `JobHandle.complete`（jobs.py:212-217）在 CANCELLING 时会把状态置 CANCELLED 并丢弃 manifest，但 262 行的取消检查与 316 行 `add_candidates` 之间没有任何原子性。
  - 影响: 用户在 provider 返回后、262 行检查之后请求取消时，job 最终显示 CANCELLED，但候选已进入 `GenerationBin`——违反模块红线"生成取消/失败不污染工程（§11.1 M11 退出条件）"，UI 出现"已取消却多了候选"的不一致。
  - 建议: 将 `add_candidates` 移到 `job.complete(...)` 成功判定之后（先确认 complete 未走 CANCELLED 分支再入箱），或在 complete 后复查状态为 SUCCEEDED 才入箱；入箱与状态判定需在同一个临界区内完成。
  - 置信度: 中

- [AUD-13903] [Major] 默认 2 worker 的线程池驱动自述"不加锁"的 JobRegistry 与无锁 GenerationBin
  - 位置: src/features/edit/ai_editing/media_generation_service.py:113-118
  - 证据:
    ```python
        @property
        def worker_pool(self) -> JobWorkerPool:
            """Lazily create the shared non-Qt worker pool for remote work."""
            if self._workers is None:
                self._workers = JobWorkerPool(self._jobs)
            return self._workers
    ```
    依赖方自述（jobs.py:329-330）："P4 不做锁（单 worker 场景足够）；多 worker 由宿主加锁"；`GenerationBin`（generation_bin.py:116-118、154-159）同样无任何锁，`add_candidates` 直接写 `self._assets`。
  - 影响: `JobWorkerPool` 默认 `max_workers=2`，两个工作线程会并发调用 `job.mark_running/complete/fail` 与 `JobRegistry._record_event`（`self._event_sequence += 1` 非原子），事件序号可能重复，`events_since(sequence)` 游标会漏事件；UI 线程同时枚举 `GenerationBin.by_request()/all_assets()` 时，工作线程 `add_candidates` 写入可触发 `RuntimeError: dictionary changed size during iteration`。本服务正是"宿主"，却未按依赖方要求加锁。
  - 建议: 在 `GenerationService` 层为 registry 事件与 bin 写入提供锁（或给 worker_pool 传 `max_workers=1` 直到依赖方线程安全化），并推动 jobs.py/generation_bin.py 内置锁。
  - 置信度: 中

- [AUD-13904] [Major] close() 以 cancel_futures=True 关闭线程池后，未启动的 Job 永远停留 QUEUED
  - 位置: src/features/edit/ai_editing/media_generation_service.py:207-211
  - 证据:
    ```python
        def close(self) -> None:
            """Stop owned workers during host teardown; completed jobs remain queryable."""
            if self._workers is not None:
                self._workers.shutdown(wait=False)
                self._workers = None
    ```
    `JobWorkerPool.shutdown`（jobs.py:157-158）为 `self._executor.shutdown(wait=wait, cancel_futures=True)`；future 被取消后 `_run` 永不执行，其 JobHandle 无人调用 complete/fail。
  - 影响: teardown 时仍在排队（未开始）的生成 Job 变成僵尸：状态恒为 QUEUED、`is_active` 恒真，`list_active()`/`metrics().active` 永久虚高，重启恢复逻辑若按 active 状态续接会被卡住；与 docstring "completed jobs remain queryable" 的意图相悖。
  - 建议: close 前遍历 `list_active()` 将非终态 Job 显式 `fail("JOB_SHUTDOWN", ...)` 或标记 CANCELLED，再 shutdown。
  - 置信度: 高

- [AUD-13905] [Suggestion] cancel() 的 docstring 与 JobRegistry.cancel 实际行为不符
  - 位置: src/features/edit/ai_editing/media_generation_service.py:358-364
  - 证据:
    ```python
        def cancel(self, job_id: str) -> bool:
            """请求取消生成 Job（§0 第三条：可取消）。

            v1（同步 stub）：Job 通常已完成；取消已 terminal 的 Job 返回 False。
            真实异步 worker 接线后，取消会中断进行中的 provider 调用。
            """
            return self._jobs.cancel(job_id)
    ```
    `JobRegistry.cancel`（jobs.py:393-398）只要 job 存在就 `request_cancel()` 并返回 True；`request_cancel` 对 terminal 状态是 no-op。
  - 影响: 调用方据 docstring 用返回值判断"是否真的发起了取消"，对已完成的 Job 会误判为取消成功。
  - 建议: 修正 docstring，或改为返回 `job.request_cancel` 前后的状态变化（例如仅当状态进入 CANCELLING 才返回 True）。
  - 置信度: 高

- [AUD-13906] [Suggestion] request_generation_async 丢弃执行期 diagnostics，与同步入口不对称
  - 位置: src/features/edit/ai_editing/media_generation_service.py:150-158
  - 证据:
    ```python
            job = self._jobs.submit(
                JobKind.GENERATION,
                label=f"生成 {request.modality.value} ({request.request_id})",
            )
            self.worker_pool.submit(
                job,
                lambda handle: self._execute_generation(request, provider, handle),
            )
            return job, []
    ```
  - 影响: `_execute_generation` 产生的 ERROR 诊断（provider 异常、安全拦截、预算超限、结果校验失败）只落在 worker 线程返回值里被丢弃，异步调用方仅能从 job.error_code 反推；同步入口却能拿到结构化 Diagnostic 列表，两个入口的可观测性不一致。
  - 建议: 在 lambda 内捕获 `_execute_generation` 返回的 diagnostics 并通过 job 事件（`job.fail` 已有 error_message，可再发一条 log）或 service 级回调转发。
  - 置信度: 高

- [AUD-13907] [Suggestion] CostLedger._totals 用 None 哨兵默认值 + type: ignore，可变默认值反模式
  - 位置: src/features/edit/ai_editing/media_generation_service.py:461-466
  - 证据:
    ```python
        monthly_cost_alert: float | None = None  # 月度成本告警阈值（货币单位）
        _totals: dict[str, float] = None  # type: ignore[assignment]  # provider_id → 累计成本

        def __post_init__(self) -> None:
            if self._totals is None:
                self._totals = {}
    ```
  - 影响: 类型系统被显式压制；若调用方按位置/关键字传入共享 dict，多个 CostLedger 会共享同一累计表。且该 dataclass 非 frozen，与同文件其余冻结模型风格不一致。
  - 建议: 改为 `_totals: dict[str, float] = field(default_factory=dict)`，删除 type: ignore 与 `__post_init__` 补丁。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：不涉及（无 ModernGL 对象）
- GLSL·HDR 正确性：不涉及
- 信号防递归：不涉及（非 Qt 模块）
- 同意 gate：`_check_consent` 的缺项清单（confirmed/cost/upload/copyright）完整，未完整确认时硬拒
- provider 防偷换：`_resolve_provider` 对 consent.provider_id 的模态支持校验正确（§2.3）
- 结果校验：`_validate_provider_result` 覆盖 request_id 一致性、重复 asset_id、provenance provider/model_id/model_version/prompt_hash、候选数上限
- resume 路径：checkpoint 三重匹配（provider_id/request_id/remote_job_id 非空）+ 重新过同意/成本 gate，无跨作用域复用
- 异常路径：provider 异常被捕获后 `job.fail("GENERATION_ERROR", ...)` + ERROR 诊断，未吞异常（BLE001 有注释说明）
- 失败/取消不入箱的主路径：safety blocked、预算超限、结果校验失败均先 fail 再返回，未调用 add_candidates
- 状态提交一致性：不直接写 timeline，候选仅入 GenerationBin（M11 红线遵守）

---

## src/features/edit/ai_editing/media_materialization_pipeline.py (行数: 218)

### 发现

- [AUD-13908] [Major] fps 为有理数字符串（"30000/1001"），_as_float 无法解析，GeneratedMediaProbe.fps 恒为 None
  - 位置: src/features/edit/ai_editing/media_materialization_pipeline.py:63（上游 85 行；根因见 media_probe.py:254、454）
  - 证据:
    ```python
            duration_seconds=_as_float(extra.get("duration_seconds")),
            fps=_as_float(extra.get("fps")), width=_as_int(extra.get("width")),
    ```
    ```python
    def _as_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    ```
    而 `FfprobeGeneratedMediaProbe.__call__`（85 行）传入的 `quality.fps` 来自 `FfprobeMediaProbe._quality_input`（media_probe.py:454）`fps = str(video.get("r_frame_rate") or "") or None`（如 "30/1"）；PyAV 后端（media_probe.py:254）也是 `fps = f"{rate.numerator}/{rate.denominator}"`。`float("30/1")` 抛 ValueError。
  - 影响: 两个内置后端下 `GeneratedMediaProbe.fps` 永远是 None：生成素材的帧率元数据丢失，任何依赖 fps 的 QA/展示逻辑静默失效（失败被 except 吞成 None，无任何告警）。
  - 建议: 在 `_as_float` 或读取处先尝试 `Fraction` 解析（`float(Fraction("30000/1001"))`），或让 `FfprobeGeneratedMediaProbe` 输出前先归一化为 float。
  - 置信度: 高

- [AUD-13909] [Suggestion] `**metadata` 在精选键之后展开，探针元数据可覆盖已归一化字段
  - 位置: src/features/edit/ai_editing/media_materialization_pipeline.py:82-90
  - 证据:
    ```python
        return {
            "mime_type": _mime_type(path),
            "duration_seconds": quality.duration_seconds,
            "fps": quality.fps,
            "width": quality.resolution[0] if quality.resolution else None,
            "height": quality.resolution[1] if quality.resolution else None,
            "decoder": "ffprobe",
            **metadata,
        }
    ```
  - 影响: ffprobe 返回的原始 metadata（含 duration_seconds、fps 等同名键）后置覆盖精选值；当前内置后端取值恰好一致，但任何自定义 probe_fn 返回额外同名键即可绕过上层类型约定（如 width 传字符串），属于外部数据未验证即合并。
  - 建议: 反转合并方向（`{**metadata, "mime_type": ..., ...}`），或仅白名单挑选允许透传的键。
  - 置信度: 中

- [AUD-13910] [Suggestion] 先全量哈希再检查 max_bytes，超大媒体在被拒绝前已被完整读入
  - 位置: src/features/edit/ai_editing/media_materialization_pipeline.py:52-57（策略检查在 104-105 行）
  - 证据:
    ```python
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    ```
    ```python
        if probe.size_bytes > self.max_bytes:
            issues.append("media_size_exceeds_limit")
    ```
  - 影响: provider 产出/写坏的异常大文件（策略默认上限 512MB，但无前置拦截）会被完整读取并哈希后才判定超限，物化管线在 QA 拒绝前白白消耗 IO/CPU（4K 长视频场景可达数 GB）。
  - 建议: 哈希循环内累计 size，超过 `max_bytes` 即提前中止并直接给出 `media_size_exceeds_limit`（把上限传给 probe 或先 stat 预检）。
  - 置信度: 高

- [AUD-13911] [Suggestion] thumbnail/proxy builder 异常未防护，与 probe 失败的 QA-block 处理不一致
  - 位置: src/features/edit/ai_editing/media_materialization_pipeline.py:181-188
  - 证据:
    ```python
        issues = self._policy.validate(probe)
        thumbnail = (
            self._thumbnail_builder(result.path, asset.asset_id)
            if not issues and self._thumbnail_builder else ""
        )
        proxy = (
            self._proxy_builder(result.path, asset.asset_id)
            if not issues and self._proxy_builder else ""
        )
    ```
  - 影响: probe 失败被宽捕获转为 `qa_issues=("media_probe_failed",)`，但 builder 抛出的任何异常会直接冒泡使 `materialize` 整体失败，成功物化的主文件信息也随之丢失；两条降级路径行为不一致。
  - 建议: 对两个 builder 分别 try/except，失败时置空路径并追加 qa_issue（如 "thumbnail_build_failed"）。
  - 置信度: 中

- [AUD-13912] [Suggestion] mimetypes 重复导入（模块级与函数内各一次）
  - 位置: src/features/edit/ai_editing/media_materialization_pipeline.py:9 与 209-211
  - 证据:
    ```python
    import mimetypes
    ```
    ```python
    def _mime_type(path: str) -> str:
        import mimetypes
        return mimetypes.guess_type(path)[0] or "application/octet-stream"
    ```
  - 影响: 冗余的函数内导入掩盖依赖关系，无功能危害但属质量问题。
  - 建议: 删除 `_mime_type` 内的局部导入，直接使用模块级导入。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：不涉及
- 信号防递归：不涉及
- probe 失败降级：宽捕获有 noqa 注释且转为 QA 阻断项，不吞领域错误
- 校验策略：空文件、超限、MIME 前缀白名单、时长容差逻辑正确
- 隐私：`to_dict` 不泄漏本地 path（只导出 has_thumbnail/has_proxy 布尔），符合 §0 第四条
- `_as_float/_as_int` 对 None/TypeError/ValueError 的防护本身正确

---

## src/features/edit/ai_editing/media_probe.py (行数: 545)

### 发现

- [AUD-13913] [Suggestion] ffprobe duration 为 "N/A" 等非数值字符串时，整次 probe 误判为 PROBE_INVALID_JSON
  - 位置: src/features/edit/ai_editing/media_probe.py:455-456（误分类点 414-419）
  - 证据:
    ```python
        duration_raw = format_info.get("duration")
        duration = float(duration_raw) if duration_raw not in (None, "") else None
    ```
    ```python
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return MediaProbeResult(
                False,
                DeliveryQualityInput(output_exists=True, output_reopenable=False),
                "PROBE_INVALID_JSON",
            )
    ```
  - 影响: ffprobe 在流/容器时长未知时常输出字符串 "N/A"，`float("N/A")` 抛 ValueError，导致一份可正常解码的媒体整体被判 PROBE_INVALID_JSON（而非 duration 缺失），真实矩阵用例（build_real_matrix_case_from_probe）随之失败且错误码误导排查。
  - 建议: duration 解析复用 `_optional_float`（对非数值返回 None），把 ValueError 保留给真正的 JSON 解析失败。
  - 置信度: 中

- [AUD-13914] [Suggestion] 矩阵函数的 probe 参数注解为 FfprobeMediaProbe，但默认值 create_media_probe() 可返回 PyAVMediaProbe
  - 位置: src/features/edit/ai_editing/media_probe.py:80、143-154
  - 证据:
    ```python
        *,
        probe: "FfprobeMediaProbe",
    ```
    ```python
        probe: "FfprobeMediaProbe | None" = None,
    ...
        active_probe = probe or create_media_probe()
    ```
    `create_media_probe`（338-352 行）返回 `"PyAVMediaProbe | FfprobeMediaProbe"`。
  - 影响: 当前靠两个后端的鸭子类型（probe_and_evaluate 签名一致）才能工作；注解与实际类型不符，静态检查与后续维护者会被误导。
  - 建议: 引入 `Probe = Union[PyAVMediaProbe, FfprobeMediaProbe]`（或公共 Protocol）并统一三处注解。
  - 置信度: 高

### 通过项
- 线程安全：probe 无共享可变状态，每次调用使用局部对象
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：不涉及
- 信号防递归：不涉及
- 输入验证：`timeout_seconds <= 0` 拒绝；文件不存在返回 OUTPUT_NOT_FOUND；`_optional_float` 过滤 NaN/Inf；`_codec_matches` 不接受空值并做 h264/h265 别名归一
- 子进程安全：ffprobe 命令参数固定、无 shell、有超时、check=False，仅 FileNotFoundError/TimeoutExpired/OSError 被收敛为 PROBE_UNAVAILABLE
- PyAV 宽捕获：except Exception 收敛为 PROBE_FAILED 结果对象（非静默吞掉），符合探针语义
- 事件序号清理：`_events` 超 500 条裁剪，防无限增长

---

## src/features/edit/ai_editing/message_record.py (行数: 204)

### 发现

- [AUD-13915] [Major] from_dict 的 timestamp 未防护 float() 转换，损坏的持久化数据会使会话恢复崩溃
  - 位置: src/features/edit/ai_editing/message_record.py:131
  - 证据:
    ```python
            artifact_refs=tuple(d.get("artifact_refs", [])),
            usage=dict(usage) if isinstance(usage, dict) else None,
            timestamp=float(d.get("timestamp", 0.0)),
    ```
  - 影响: 其余字段全部做了防御性转换（str()/bool()/isinstance），唯独 timestamp 遇到损坏值（如字符串 "abc" 或嵌套结构）会抛 ValueError/TypeError，一条坏消息即可让整个会话的加载/迁移失败，与模块"Old data is migrated, not dropped"的恢复目标冲突。
  - 建议: 仿照 `_optional_float` 语义：转换失败时回退 `time.time()` 或 0.0 并置 `content_complete=False` 之类的标记。
  - 置信度: 中

- [AUD-13916] [Suggestion] 非法 role/status 被静默强制为 "user"/COMPLETED，掩盖持久化数据损坏
  - 位置: src/features/edit/ai_editing/message_record.py:88-93
  - 证据:
    ```python
        if self.role not in ("user", "assistant", "system", "tool"):
            object.__setattr__(self, "role", "user")
        status = str(self.status or "").strip().lower()
        if status not in MessageRecordStatus.KNOWN:
            status = MessageRecordStatus.COMPLETED
        object.__setattr__(self, "status", status)
    ```
  - 影响: 一条 FAILED/CANCELLED 消息若状态键损坏，会被静默改写为 COMPLETED，UI 呈现与重试逻辑据此做出错误判断，且无任何标记提示数据被修正。
  - 建议: 至少在改写时置一个 `migrated`/`redacted` 类标志，或对来自 `from_dict` 的非法值记录 warning；构造期新数据可考虑直接抛错。
  - 置信度: 高

### 通过项
- 线程安全：不可变 dataclass，无共享可变状态
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 存储 sanitize：`to_dict` 先截断后 `sanitize_for_storage`，`redacted` 标志在截断或净化发生时置位
- 截断语义：超限内容在 MESSAGE_CONTENT_LIMIT 处截断并追加可见标记，不静默丢尾部
- legacy 迁移：`from_dict` 同时接受 content/text 双键，`content_complete` 默认推导正确；`from_summary`/`from_agent_message` 的字段映射与 MessageSummary（session_models.py:43-49）、AgentMessage（agent_models.py:364-372，status 为纯字符串常量类）实际契约一致（已核对）
- usage 校验：`dict(usage) if isinstance(usage, dict) else None`

---

## src/features/edit/ai_editing/mission_workspace.py (行数: 154)

### 发现

- [AUD-13917] [Major] workspace_from_run_result 依赖不存在的 AgentRunResult 及与真实 AgentCheckpoint 不匹配的字段名
  - 位置: src/features/edit/ai_editing/mission_workspace.py:106-148（字段访问 127-147）
  - 证据:
    ```python
        cp = result.checkpoint
        plan = result.plan
    ...
        if plan is not None and cp.current_step_id:
            for step in plan.steps:
                if step.step_id == cp.current_step_id:
                    current_tool = step.tool_name
    ...
            completed_step_ids=tuple(cp.completed_step_ids),
            total_steps=len(plan.steps) if plan else 0,
            cost_units=float(cp.cost_units),
    ```
    全库检索 `AgentRunResult` 仅命中本文件 docstring（3、107 行）；真实 `AgentCheckpoint`（agent_models.py:791-797）字段为 `checkpoint_id/run_id/state/completed_steps/messages_digest/context_hash/protected_context/created_at`——没有 `phase/current_step_id/completed_step_ids/cost_units/reason`。
  - 影响: 该公共 API 一旦接到真实运行结果即抛 AttributeError（`plan`、`status.value`、`diagnostics`、`rollback_failures` 同样无真实来源）；目前仅有 mock 测试（tests/ai/test_os06_mission_workspace.py 用自造 `_MockRunResult`）通过，属于从未完成集成的幻影契约，后续接线者会被 docstring 误导。
  - 建议: 要么补齐真实的 run-result 类型并统一 checkpoint 字段名后重写此函数，要么删除/标注 deprecated，避免假装可用。
  - 置信度: 高

- [AUD-13918] [Suggestion] diagnostics 按关键词子串分类，"no error occurred" 也会被归为 error
  - 位置: src/features/edit/ai_editing/mission_workspace.py:71-77
  - 证据:
    ```python
        error_diagnostics = [d for d in diagnostics if any(
            kw in d.lower() for kw in ("error", "fail", "exception", "traceback")
        )]
        warning_diagnostics = [d for d in diagnostics if d not in error_diagnostics and any(
            kw in d.lower() for kw in ("warn", "stale", "retry", "degrad")
        )]
    ```
  - 影响: 子串匹配无法区分否定语境（"0 errors"、"failed step recovered automatically" 记为 error），错误/警告计数失真；且三次全量扫描 O(n·k)（影响很小）。
  - 建议: 上游 diagnostics 携带结构化 severity 字段后按字段分类，至少收紧为词边界匹配。
  - 置信度: 高（机制确定，实际误分类频率取决于文案）

### 通过项
- 线程安全：纯函数、无共享状态
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（模块刻意 Qt-free，已核实无 Qt import）
- build_mission_workspace_view：total_steps 除零有防护；所有入参做 str()/int()/float() 归一；未知 status 有默认 badge 兜底
- 状态提交一致性：只读视图构建，不产生副作用

---

## src/features/edit/ai_editing/missions.py (行数: 357)

### 发现

- [AUD-13919] [Major] set_status 不校验 status 字符串、不禁止终态复活，绕过构造期校验产生僵尸状态
  - 位置: src/features/edit/ai_editing/missions.py:319-330
  - 证据:
    ```python
        def set_status(
            self,
            mission_id: str,
            status: str,
            *,
            reason: str = "",
        ) -> None:
            with self._lock:
                mission = self.require(mission_id)
                mission.status = status
                mission.termination_reason = reason
                mission.updated_at = time.time()
    ```
  - 影响: `Mission.__post_init__`（143-144 行）只校验合法状态集合，而 `set_status` 直接赋值任意字符串：拼错的状态（如 "actve"）既不在 ACTIVE 也不在 TERMINAL 集合，`is_active`/`is_terminal` 同时为 False，mission 变成不可见僵尸；同时可把 SUCCEEDED 的终态 mission 改回 active，破坏状态机单向性（OS-01 生命周期）。
  - 建议: `set_status` 内复用与构造期相同的白名单校验（非法即 ValueError），并拒绝从终态回迁（需要显式 reopen API）。
  - 置信度: 高

- [AUD-13920] [Major] Store 的查询方法返回可变 Mission 活引用，锁外并发修改与锁内原子替换竞争
  - 位置: src/features/edit/ai_editing/missions.py:245-253（类 docstring 声称 209-211 行）
  - 证据:
    ```python
        def get(self, mission_id: str) -> Mission | None:
            with self._lock:
                return self._missions.get(mission_id)
    ```
    类 docstring："The mission is mutable (status / decisions / criteria evolve), so it uses a regular dataclass + a lock for thread safety via :class:`MissionStore`."而 `add_decision`（290-291 行）在锁内做 `mission.decisions = mission.decisions + (decision,)` 的读-改-写。
  - 影响: 持有 `get()/require()/active_missions()` 返回引用的线程可在无锁下写字段（`mission.status = ...`），与锁内的元组替换/状态迁移交错，造成决策丢失或状态被覆盖——锁只保护了字典操作，并未兑现"thread safety via MissionStore"的承诺。
  - 建议: 对外返回快照（`dataclasses.replace(mission)` 或 `to_dict` 的不可变视图），所有变更只经 store 方法；或将 Mission 改为 frozen dataclass 强制走 store。
  - 置信度: 中

- [AUD-13921] [Suggestion] parent_run_id 语义混乱：按 mission_id 查父，字段名义却是 run id，传错时静默不链接
  - 位置: src/features/edit/ai_editing/missions.py:237-243
  - 证据:
    ```python
        with self._lock:
            self._missions[mission.mission_id] = mission
            if parent_run_id:
                parent = self._missions.get(parent_run_id)
                if parent and mission.mission_id not in parent.child_run_ids:
                    parent.child_run_ids = parent.child_run_ids + (mission.mission_id,)
    ```
  - 影响: `self._missions` 以 `mission_id`（"mission-<uuid>"）为键，而参数/字段名是 `parent_run_id`（模块 docstring 称 "parent/child run links"）。若调用方按字段名传入真实 run id，`get` 落空后层级静默丢失，无诊断无报错。
  - 建议: 统一命名为 `parent_mission_id`，或在查不到父时抛 KeyError/记录诊断。
  - 置信度: 中

- [AUD-13922] [Suggestion] update_criterion 对未知 check 静默 no-op
  - 位置: src/features/edit/ai_editing/missions.py:302-313
  - 证据:
    ```python
            updated = tuple(
                SuccessCriterion(
                    check=c.check, target=c.target, label=c.label,
                    met=met if c.check == check else c.met,
                    evidence_ref=evidence_ref if c.check == check else c.evidence_ref,
                )
                for c in mission.success_criteria
            )
            mission.success_criteria = updated
    ```
  - 影响: `check` 与任何现有 criterion 都不匹配时，元组重建后内容不变，方法无异常返回，调用方以为已更新；拼写错误的 check 会让成功标准永远无法满足（或自动成功逻辑依赖错误的旧值）。
  - 建议: 重建后检测无任何条目命中时抛 KeyError 或返回 bool 供调用方检查。
  - 置信度: 高

### 通过项
- 线程安全（限店内路径）：字典读写与 decisions/criteria 的锁内读-改-写串行正确；RLock 支持嵌套
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 构造期验证：mission_id/project_id/objective_text 必填与状态白名单校验正确
- update_criterion 的自动成功判定：仅在所有条件满足且 is_active 时置 SUCCEEDED 并写 termination_reason，逻辑正确
- scope_hash：sort_keys + 紧凑分隔符的规范化序列化，default=str 兜底非常规类型
- 序列化：to_dict 与 from 侧字段一一对应（供宿主持久化），不泄漏 secret

---

## src/features/edit/ai_editing/model_pack_manager.py (行数: 12)

### 发现

（无）

### 通过项
- 通过（无逻辑）：`sys.modules` 垫片，将本模块整体替换为 `src.shared.ai_models.generation.model_pack_manager` 的实现，历史导入路径不变；已核实 shared 实现存在，且其自身不再回导本路径（无循环替换风险）。GPU/GLSL/线程/异常等维度不适用。

---

## src/features/edit/ai_editing/model_router.py (行数: 80)

### 发现

- [AUD-13923] [Major] _supports_reasoning 读取 AiProviderProfile 上不存在的 reasoning_efforts 字段，能力过滤退化为 no-op
  - 位置: src/features/edit/ai_editing/model_router.py:42-46、64-69
  - 证据:
    ```python
            eligible = [
                profile for profile in self._profiles
                if request.modality in profile.modalities
                and self._supports_reasoning(profile, request.reasoning_effort)
            ]
    ```
    ```python
        @staticmethod
        def _supports_reasoning(profile: AiProviderProfile, effort: str) -> bool:
            supported = getattr(profile, "reasoning_efforts", None)
            if supported is None:
                return effort in {"low", "medium", "high"}
            return effort in tuple(supported)
    ```
    已核实 `AiProviderProfile`（src/shared/ai_models/generation/provider_profiles.py:48-73，经本包 provider_profiles 垫片重定向）没有任何 `reasoning_efforts` 字段；全库 grep 仅本文件引用该名字。
  - 影响: 真实 profile 恒走默认分支：low/medium/high 对所有 profile 一律通过（路由声称的 reasoning 能力约束不存在，可能把 high 推理请求路由到不支持推理的模型），而任何非标准 effort 值（如 "none"/"minimal"）又使所有 profile 不可用直接抛 ModelRouteError。第二分支是永不执行的死代码。
  - 建议: 在 AiProviderProfile 中真正加入 `reasoning_efforts` 字段（带默认），或移除该过滤维度并从 ModelRouteRequest 删掉 reasoning_effort，避免 API 表达不存在的约束。
  - 置信度: 高

### 通过项
- 线程安全：构造期冻结 profiles 元组，route 无共享可变状态
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- fallback 语义：无隐式回退——active_profile 命中直接返回（fallback_used=False）；不命中且未显式 allow_fallback 抛错；回退排序键稳定（rank, profile_id）
- 异常路径：错误消息含任务/profile 上下文，便于定位
- 类型注解：完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 0 | — |
| Major | 11 | AUD-13900, 13901, 13902, 13903, 13904, 13908, 13915, 13917, 13919, 13920, 13923 |
| Suggestion | 13 | AUD-13905, 13906, 13907, 13909, 13910, 13911, 13912, 13913, 13914, 13916, 13918, 13921, 13922 |
