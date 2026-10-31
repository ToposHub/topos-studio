# batch-219 审查报告 — src/features/ai_harness（6 文件，2,577 行）

审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 /
异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
本批全部为纯 Python 模块（包红线：不 import Qt / ModernGL / 真实推理后端），GPU/GLSL/信号
防递归维度经核对不适用（无 OpenGL 调用、无 Qt 控件）。跨模块契约已实读核对：
`runtime.py`（状态机 / RLock）、`scheduler_adapter.py`（JobRunBridge）、
`media_generation_service.py` + `jobs.py`（JobStatus / JobWorkerPool.future）、
`transcript_router.py`（router_selection 词汇）、`ai_editing_mixin.py` / `ai_edit_controller.py`
（生产装配与取消入口）。

汇总：Critical 0 · Major 4 · Suggestion 14。

---

## src/features/ai_harness/__init__.py (行数: 237)

### 发现
- [AUD-21900] [Suggestion] 模块 docstring 仍停留在 P0 阶段描述，与实际导出内容严重脱节
  - 位置: src/features/ai_harness/__init__.py:6-17
  - 证据:
    ```python
    P0 阶段（本文件）只定义纯 Python 的领域对象与 fake runtime：
    - :mod:`invocation_models` —— ...
    - :mod:`runtime` —— ``HarnessRuntime``：唯一 ``start`` / ``cancel`` ...
    - :mod:`scheduler_adapter` —— ...
    - :mod:`legacy_adapters` —— ...
    ```
    而文件实际从 15+ 个子模块（plugin_runtime / plugin_broker / plugin_sandbox /
    plugin_isolation / plugin_discovery / plugin_rollout / run_trace / rollout_metrics 等）
    re-export 了 90 余个符号（23-141 行、`__all__` 143-237 行）。
  - 影响: 阅读者会误以为本包只有 4 个 P0 模块与 fake runtime，误导架构判断与后续维护。
  - 建议: 更新 docstring 为按阶段（P0/P2/P3）分组的完整模块清单。
  - 置信度: 高
### 通过项
- 纯 re-export，无逻辑分支；`__all__` 与 import 逐一核对，无未定义名、无遗漏导出
- 未 import Qt / ModernGL / provider 后端（符合包红线）

---

## src/features/ai_harness/analysis_skills.py (行数: 430)

### 发现
- [AUD-21901] [Major] 转录路由追踪把"最终"质量错误归因到中间步骤，Qwen/Demucs 步的 quality 与 note 失真
  - 位置: src/features/ai_harness/analysis_skills.py:301-339（配合 342-352）
  - 证据:
    ```python
    selection = str(extra.get("router_selection", ""))
    quality = _analysis_quality(analysis)   # ← 这是“最终入库”的 transcript 质量
    qwen_selected = selection in ("qwen3_asr_original",
                                  "qwen3_asr_original_low_confidence")
    steps.append(TranscriptRouteStep(
        provider="qwen3_asr", quality=quality, attempted=True,
        selected=qwen_selected,
        note=("质量达标" if qwen_selected and quality >= quality_threshold
              else f"质量 {quality:.2f} 低于阈值 ..." if quality < quality_threshold
              else "高置信"),
    ))
    ...
    demucs_quality = quality if used_demucs else 0.0
    ```
    当 `selection == "qwen3_asr_demucs_v4"`（Demucs 胜出）时，step 1（Qwen 原音频）拿到的
    是 Demucs 路径的最终质量（通常 ≥ 阈值），note 落到"高置信"——而事实是 Qwen 原音频
    质量低于阈值才触发 Demucs（transcript_router.py:111-131 已核对路由语义）；
    `selection == "whisperx_fallback"` 时同理；`low_confidence` 分支里 Demucs 未胜出时
    `quality=0.0` 是编造值。
  - 影响: 该追踪正是 plan §10.4 验收项（"RunRecord 明确显示 Qwen 质量、是否进入 Demucs、
    是否回退 WhisperX"），当前显示的是错误数值与相反结论，误导诊断与审计。
  - 建议: 在 provider 侧持久化真实的逐步质量（`provenance.extra["route_steps"]`，本函数
    已预留该优先路径），或在反推时对非选中步骤标 `quality=None`/"未知"而非冒用终态值。
  - 置信度: 高
- [AUD-21902] [Suggestion] 未使用的导入 `field`、`Callable`
  - 位置: src/features/ai_harness/analysis_skills.py:38-39
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from typing import Any, Callable, Iterable, Literal
    ```
    文件内所有 dataclass 均用普通默认值，无 `field(...)`；无任何 `Callable` 引用。
  - 影响: 死导入，增加阅读噪音。
  - 建议: 删除 `field` 与 `Callable`。
  - 置信度: 高
### 通过项
- 线程安全：纯函数 + frozen dataclass，无可变共享状态，无锁需求
- 异常/降级路径：selection/bin 空与 pool_analysis_enabled=False 均返回空 resolution + 提示，
  不抛错、不伪造；scope.asset_ids 为空时 build_analysis_request 返回 None
- 输入验证：analyze_all 强制 requires_confirmation=True 且提示含数量与预计耗时；
  CLOUD_ENHANCED/APPROVED_CLOUD 与 max_assets=200 守门按 AnalysisRequest.create 契约传递
  （create 签名已核对一致）
- 状态提交一致性：EvidenceRef 只产版本化引用（asset_id + kind + provider@model 版本），
  不传模型原文；OCR 一律标 untrusted
- 性能模式：collect_evidence_refs 每 asset 每字段 O(1) 读取，无隐藏重复扫描

---

## src/features/ai_harness/editorial_skills.py (行数: 611)

### 发现
- [AUD-21903] [Suggestion] build_rough_cut_plan 用 `replace()` 修改已入库 plan，同 plan_id 出现两个版本
  - 位置: src/features/ai_harness/editorial_skills.py:378-391
  - 证据:
    ```python
    # 强制 PREVIEW 模式（若已是 COMMIT 也降级为 PREVIEW —— skill 永不直接 commit）
    plan = variant_plan
    if plan.mode != PlanMode.PREVIEW:
        from dataclasses import replace
        plan = replace(plan, mode=PlanMode.PREVIEW, title=title)
    ...
        payload_ref=plan.plan_id,
    ```
    注释自述 variant_plan"已存 plan_repository"；替换后返回的副本与库中原对象同
    `plan_id` 但 `mode`/`title` 不同。
  - 影响: 后续若按 plan_id 从仓库取 plan 做审批/commit，取到的是旧 mode 版本，
    与 SkillOutcome 返回的对象分叉（状态提交一致性风险）。
  - 建议: 不改写原 plan——为 skill 视图新建 plan_id，或在 artifact 里记录"视图降级"诊断，
    保持仓库内对象唯一。
  - 置信度: 中
- [AUD-21904] [Suggestion] 未使用的导入 `field`、`Iterable`、`FrameRange`
  - 位置: src/features/ai_harness/editorial_skills.py:27-30
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from typing import Any, Iterable, Sequence
    from src.features.edit.ai_editing.editorial_models import FrameRange
    ```
    文件内无 `field(`、无 `Iterable`、无 `FrameRange` 的任何使用（已全文核对）。
  - 影响: 死导入。
  - 建议: 删除。
  - 置信度: 高
### 通过项
- 状态提交一致性：三个构造器产出的 EditPlan 全部 `mode=PlanMode.PREVIEW`，不直接写时间线；
  track_id 用 uuid5(project/timeline/track_name) 保证 preview 与 commit 两次编译 diff 一致
- 输入验证：TTS 放置强制 Media Pool id（`media_asset_id or payload_ref` 为空时只产诊断
  TTS_MEDIA_NOT_MATERIALIZED 不产 op）；时长缺失/<=0 时拒绝放置；无转录时 NO_TRANSCRIPT 降级
- 可追溯性：ADD_SUBTITLE 逐行携带 evidence_ref 与 reading_speed_cps 进 intent；
  生成音频 source.start=0、timeline 与 duration 显式成对
- register_p2_skills：已注册 skill 跳过、注册诊断聚合、`result or []` 容错 None；
  p2_official_manifests 的 SkillManifest 字段与 skill_manifest.py 定义逐一核对通过
- build_auto_caption/build_subtitle_draft 的 API（empty/lines/average_cps/over_fast_lines、
  EditPlan.create(plan_id/brief_id)、OperationKind 四个值）已核对存在且签名匹配

---

## src/features/ai_harness/generation_adapter.py (行数: 389)

### 发现
- [AUD-21905] [Major] run 取消在 invoke 返回后不再传播到生成 job；用户取消后 run 仍可能终态 SUCCEEDED
  - 位置: src/features/ai_harness/generation_adapter.py:259-278（唯一传播点）、282-300、363-369
  - 证据:
    ```python
    # 9. 取消传播（若 run 已 cancel）
    if self._runtime.is_cancelled(run_id):
        poll_cancel_to_job(self._runtime, run_id, job_handle)
        self._runtime.finish(run_id, success=False)
        return run_id
    ...
    future.add_done_callback(
        lambda completed: self._on_generation_worker_done(
            completed, run_id, request, job_handle,
        )
    )
    ```
    `poll_cancel_to_job` 只在 invoke 同步段调用一次。全库 grep 确认
    `JobRunBridge.propagate_cancel` 无任何调用方；生产装配（ai_editing_mixin.py:751）后，
    Run Drawer 取消走 `runtime.cancel(run_id)`（ai_edit_controller.py:_on_run_cancel_requested），
    而 provider 的 `is_cancelled` 只看 `job.status == CANCELLING`（media_generation_service.py:250），
    没有任何链路把 run 的 cancel token 翻成 `job.request_cancel()`。
  - 影响: 用户取消后付费生成继续跑完；worker 回调里 `finish(success=True)` 优先于
    cancel_requested（runtime.py:544-551），run 终态记为 SUCCEEDED 而非 CANCELLED——
    违反本模块自述红线"取消经 is_cancelled → poll_cancel_to_job 真实传播"。
  - 建议: 在 adapter 内 `runtime.add_subscriber` 订阅 `RUN_CANCEL_REQUESTED` 调
    `bridge.propagate_cancel(run_id)`；或回调/完成收尾前再查 `is_cancelled`。
  - 置信度: 高
- [AUD-21906] [Major] job 的 cancelled 终态被一律映射为 FAILED/GENERATION_JOB_FAILED
  - 位置: src/features/ai_harness/generation_adapter.py:318-321、363-369
  - 证据:
    ```python
    status = getattr(job_handle, "status", None)
    status_val = getattr(status, "value", str(status))
    success = status_val == "succeeded"
    ...
    self._runtime.finish(
        run_id, success=success, with_warnings=with_warnings,
        error_code="" if success else "GENERATION_JOB_FAILED",
        error_message="" if success else (
            getattr(job_handle, "error_message", "") or "生成 job 失败"
        ),
    )
    ```
    JobStatus 有独立 `CANCELLED = "cancelled"`（jobs.py:48）；job 面板直接按 job_id 取消的
    真实入口存在（ai_edit_controller.py:6127 `registry.cancel(job_id)`）。此时 run 自身未置
    cancel token，`finish(success=False)` 走 FAILED 分支。
  - 影响: 用户取消生成 job，对应 run 记为 FAILED 并带误导性 error_code/message；
    rollout 指标把取消算作失败率。
  - 建议: `status_val == "cancelled"` 时先 `runtime.cancel` 对应 run（或直接请求 CANCELLED
    终态），error_code 用 GENERATION_JOB_CANCELLED 区分。
  - 置信度: 高
- [AUD-21907] [Major] reference_asset_ids 未验证类型，字符串输入被 `tuple()` 拆成单字符 id
  - 位置: src/features/ai_harness/generation_adapter.py:97
  - 证据:
    ```python
    reference_asset_ids=tuple(inputs.get("reference_asset_ids", ())),
    ```
    `inputs` 来自 Invocation（chat/workflow 外部文本链路）。若传入字符串 `"asset-abc"`，
    `tuple("asset-abc")` → `('a','s','s','e','t',...)`；GenerationRequest 无
    `__post_init__` 校验（shared/ai_models/generation/generation_models.py:412），逐字符 id
    静默通过并送入 provider。
  - 影响: 图生视频/图生图的参考素材被静默替换为非法 id，生成结果错误且难排查
    （违反"外部数据须验证"红线）。
  - 建议: 仅接受 list/tuple 且逐项 `str` + 非空校验，否则记 REQUEST_BUILD 诊断。
  - 置信度: 中（需 host 传错类型触发，但无任何防线）
- [AUD-21908] [Suggestion] 无效 modality 静默回退 IMAGE，不产生任何诊断
  - 位置: src/features/ai_harness/generation_adapter.py:71-75
  - 证据:
    ```python
    try:
        modality = GenerationModality(modality_str)
    except ValueError:
        modality = GenerationModality.IMAGE
    ```
  - 影响: 拼写错误（如 "vedio"）的请求被静默改为图片生成；虽有 consent gate，
    但用户确认的意图与实际执行不一致。
  - 建议: 回退时 `record_diagnostic(warning, "MODALITY_FALLBACK")` 或直接走
    REQUEST_BUILD_FAILED。
  - 置信度: 高
- [AUD-21909] [Suggestion] _missing_consent_fields 与 GenerationConsent.is_complete 口径不一致
  - 位置: src/features/ai_harness/generation_adapter.py:372-383
  - 证据:
    ```python
    if not consent.provider_id:
        missing.append("provider_id")
    if not consent.cost_acknowledged:
        missing.append("cost_acknowledged")
    ...
    ```
    而 `is_complete` = `confirmed and cost and upload and copyright`
    （shared generation_models.py:151-159），不含 provider_id、含 confirmed。
    `confirmed=False` 且三项 ack 齐全时 missing 列表为空。
  - 影响: WAITING_FOR_USER 诊断里 `missing: []`，UI 无法告诉用户还差哪一项。
  - 建议: 与 is_complete 同步：补 `confirmed`，provider_id 单列为提示项。
  - 置信度: 高
- [AUD-21910] [Suggestion] retry 复用同一 invocation_id 作 request_id，历史候选被重复记入新 run
  - 位置: src/features/ai_harness/generation_adapter.py:85-86、313-333
  - 证据:
    ```python
    return GenerationRequest(
        request_id=inv.invocation_id,
    ...
    candidates = gen_bin.by_request(request.request_id)
    ...
    for cand in candidates:
        self._runtime.record_artifact(run_id, ArtifactRef(...))
    ```
    `runtime.retry` 基于同一 Invocation 新建 run（runtime.py:407-432），
    `record_artifact` 追加无去重（runtime.py:614）。
  - 影响: 重试一次已成功的生成后，新 run 的 artifacts 会把上一轮候选一并列入，
    审计/结果卡显示重复或串号。
  - 建议: request_id 加入 run 维度（如 `f"{invocation_id}:{run_id}"`）或按
    job.result_manifest.candidate_ids 过滤。
  - 置信度: 中
- [AUD-21911] [Suggestion] worker 线程回调调用无锁的 JobRunBridge.sync，依赖 GIL 单操作原子性
  - 位置: src/features/ai_harness/generation_adapter.py:271-278、358
  - 证据:
    ```python
    future.add_done_callback(
        lambda completed: self._on_generation_worker_done(...)
    )
    ...
    # _collect_generation_outcome 内（worker 线程执行）：
    self._bridge.sync(run_id, job_handle.job_id)
    ```
    回调在 `JobWorkerPool` 的 ThreadPoolExecutor 线程执行；`JobRunBridge` 的
    `_bindings`/`_job_to_runs` 是无锁 dict/set（scheduler_adapter.py:129-145），
    与 UI 线程的 `invoke → bind/track` 并发。CPython 下 setdefault/set.add 单操作原子，
    风险低，但属未声明的线程契约。
  - 影响: 一旦 bridge 增加复合操作（如 propagate_cancel 的 keys() 迭代）或换
    非 GIL 实现，即出现竞态。
  - 建议: JobRunBridge 加 `threading.Lock`，或在 adapter 文档化"仅 worker 单线程回调"约束。
  - 置信度: 中
### 通过项
- 线程安全（runtime 侧）：所有 run 状态写入走 HarnessRuntime 的 RLock 保护方法；
  回调内异常被捕获并归入 GENERATION_WORKER_CALLBACK_FAILED，不外抛到 executor
- 状态机合法性：policy 拒绝走 DRAFT→VALIDATING→FAILED；consent 未全走
  QUEUED→RUNNING→WAITING_FOR_USER（mark_running 逐级推进），均已对照
  RunStatus._TRANSITIONS 核对无非法迁移
- consent gate：is_complete 未过 → WAITING_FOR_USER 且不调 provider（红线 1）
- 异常路径：REQUEST_BUILD_FAILED / GENERATION_EXCEPTION / GENERATION_GATE_FAILED
  全部落 RunRecord 诊断与 error_code；宽泛 except 均带 noqa 理由且不吞诊断
- 输入验证：modality/duration 解析失败不崩；诊断经 sanitize_for_storage 入库
- 权限红线：job_handle 为 None（consent/cost/provider gate 拒绝）→ 失败收尾；
  候选只产 ArtifactRef 引用，不自动进 timeline；license unknown/pending 与
  safety.blocked 分别产 warning/error 诊断

---

## src/features/ai_harness/help_assistant.py (行数: 189)

### 发现
- [AUD-21912] [Suggestion] build_help_assistant 宽泛 except 且完全不记日志
  - 位置: src/features/ai_harness/help_assistant.py:177-181
  - 证据:
    ```python
    try:
        index = HelpIndex.build(sources)
        return HelpAssistant(index=index)
    except Exception:  # noqa: BLE001 — 帮助装配失败不应阻断启动
        return HelpAssistant(index=None)
    ```
  - 影响: 装配失败被静默降级为"帮助索引未装配"，现场无法区分"没配 sources"与
    "build 抛错"，违背"宽泛 except 不得掩盖领域错误"的精神（注释说明了意图但无日志）。
  - 建议: 至少 `_LOG.exception("help index build failed")`。
  - 置信度: 高
- [AUD-21913] [Suggestion] entry_ids_for_query 不拦空查询，search 空查询返回全部条目
  - 位置: src/features/ai_harness/help_assistant.py:129-133
  - 证据:
    ```python
    def entry_ids_for_query(self, query: str, *, limit: int = 10) -> list[str]:
        """供 InvocationRouter.help_search 注入：query → entry_id 列表。"""
        if self._index is None:
            return []
        return [e.entry_id for e in self._index.search(query, limit=limit)]
    ```
    HelpIndex.search 对空查询"返回全部（受 limit）"（help_index.py:214-216）；
    `answer()` 有 strip/空检查而本方法没有。
  - 影响: router 以空/空白 query 调用时，最多 10 条不相关 entry_id 被注入会话上下文。
  - 建议: 与 answer() 对齐——空/空白返回 []。
  - 置信度: 高
### 通过项
- 防编造红线：所有内容仅来自已索引 HelpEntry；找不到时 found=False；索引未装配时
  index_empty=True 友好提示，不崩、不编
- 无 Qt/模型依赖；frozen dataclass；无状态可变共享
- explain_diagnostic 找不到诊断码时显式 found=False

---

## src/features/ai_harness/invocation_models.py (行数: 721)

### 发现
- [AUD-21914] [Suggestion] sanitize_for_storage 的 secret 替换 lambda 含死代码分支
  - 位置: src/features/ai_harness/invocation_models.py:683-691
  - 证据:
    ```python
    s = _SECRET_RE.sub(
        lambda m: m.group(0).split("=", 1)[0].split(":", 1)[0].rstrip()
        + "=<redacted-secret>"
        if ("=" in m.group(0) or ":" in m.group(0))
        else "<redacted-secret>",
        s,
    )
    ```
    正则本身要求 `\s*[:=]\s*` 分隔符（659-664 行），任何匹配必含 `:` 或 `=`，
    else 分支不可达。
  - 影响: 安全关键路径的可读性差，读者需反推分支语义；维护时易引入偏差。
  - 建议: 删除 else 分支，直接构造 `key + "=<redacted-secret>"`。
  - 置信度: 高
- [AUD-21915] [Suggestion] Invocation 默认 scope 使用魔法 project_id="default"，绕过跨工程校验
  - 位置: src/features/ai_harness/invocation_models.py:405
  - 证据:
    ```python
    scope: InvocationScope = field(default_factory=lambda: InvocationScope(project_id="default"))
    ```
    InvocationScope.__post_init__ 只校验非空（244 行），docstring 明言"跨工程 invocation
    非法"（220 行），但忘设 scope 的调用会静默落到名为 "default" 的工程。
  - 影响: 未显式绑定 scope 的 invocation 不报错，分析/写入可能落到错误工程上下文，
    审计上也无法区分"显式 default"与"漏设"。
  - 建议: 默认工厂抛错（强制显式传 scope），或用可识别哨兵值并在 start 校验拒绝。
  - 置信度: 高（设计层面）
- [AUD-21916] [Suggestion] from_dict 对字符串型列表字段逐字符拆分（selected_asset_ids）
  - 位置: src/features/ai_harness/invocation_models.py:282
  - 证据:
    ```python
    selected_asset_ids=tuple(str(item) for item in data.get("selected_asset_ids", ())),
    ```
    快照是跨进程外部数据；若该字段损坏为字符串 `"abc"`，`for item in "abc"` 得到
    `('a','b','c')`，静默通过校验。
  - 影响: 恢复的 scope 带垃圾 asset id，下游按 id 逐个 miss，难定位。
  - 建议: 先 `isinstance(x, (list, tuple))` 检查，否则抛 ValueError（restore 路径已有
    fail-closed 捕获）。
  - 置信度: 中
- [AUD-21917] [Suggestion] 未使用的导入 `Callable`
  - 位置: src/features/ai_harness/invocation_models.py:32
  - 证据:
    ```python
    from typing import Any, Callable, Iterable, Literal
    ```
    文件内无 `Callable` 引用（Iterable 用于 collect_secret_free）。
  - 影响: 死导入。
  - 建议: 删除。
  - 置信度: 高
### 通过项
- 状态机：_TRANSITIONS 覆盖完整、五个终态出边为空（不可逆）；非法迁移抛 ValueError；
  ACTIVE/TERMINAL 集合与常量一致
- 输入验证（构造侧）：source 三值白名单；workflow/developer 强制 skill_id；
  permission/timeline_write 枚举校验；brief_revision/base_revision 非负；
  from_public_dict 缺 scope/policy 抛 ValueError，inputs 非容错形状回退安全默认
- 脱敏红线：sanitize_for_storage 递归 dict/list/tuple；绝对路径覆盖 Unix/Windows/UNC；
  secret 覆盖 key=value / Authorization: / sk- 形状；to_public_dict/from_public_dict 对
  inputs、user_text、event payload 双向过 sanitize
- 纯数据模块无 I/O、无线程、无 Qt；枚举用 str 常量 + 校验函数的设计与注释一致
- RunRecord.to_public_dict 输出全为 JSON 原生类型（无 Enum/对象泄漏）
