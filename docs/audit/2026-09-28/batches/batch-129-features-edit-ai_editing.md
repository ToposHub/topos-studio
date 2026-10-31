# batch-129 审查报告 — src/features/edit/ai_editing（analysis 请求/调度/服务、approval、artifact）

批次：batch-129 [P1]；编号区间 AUD-12900–AUD-12999；日期 2026-09-28。
审查方式：六个文件全文通读；跨文件核实了 jobs.py（JobHandle/JobRegistry 语义）、
ai_editing_mixin.py:909（生产装配 `AnalysisJobScheduler(asynchronous=True)`）、
media_analysis_controller.py（execute_analysis 经 scheduler 的生产调用链）、
transaction_service.py / ai_edit_controller.py（ApprovalService.consume 调用点）、
全库 grep（notify_finished / .budget / project_revision / scheduler.shutdown 调用者）。
本批次为纯 Python 逻辑层（无 Qt/GLSL/GPU 资源），GLSL·HDR 维度不适用。

汇总：Critical 0 · Major 6 · Suggestion 10。

---

## src/features/edit/ai_editing/analysis_request.py (行数: 352)

### 发现

- [AUD-12900] [Suggestion] `with_confirmation` / `with_state` 缺状态机守卫，可将终态/已取消请求转回 CONFIRMED
  - 位置: src/features/edit/ai_editing/analysis_request.py:238-251
  - 证据:
    ```python
    def with_confirmation(self, confirmed_at: float) -> "AnalysisRequest":
        return replace(
            self,
            user_confirmed_at=confirmed_at,
            state=RequestState.CONFIRMED,
        )

    def with_state(self, new_state: RequestState) -> "AnalysisRequest":
        return replace(self, state=new_state)
    ```
  - 影响: 状态机校验只存在于 `AnalysisService.confirm_analysis`（PROPOSED 才能确认）。
    任何直接持有 request 副本的调用方绕过 service 即可"复活"一个 CANCELLED/COMPLETED
    请求为 CONFIRMED（`user_confirmed_at` 重新置位），削弱 §17.3"未确认不执行"红线的纵深防御。
  - 建议: 在 `with_confirmation` 内拒绝非 PROPOSED 源状态（或至少拒绝终态）；
    `with_state` 增加合法转移表校验（与 `RequestState.is_executable` 一致）。
  - 置信度: 中

### 通过项
- 输入验证：`__post_init__` 校验 request_id/project_id/asset_ids 非空、recipe/policy 类型、
  CLOUD_ENHANCED 必须搭配 APPROVED_CLOUD（222 行）。
- 状态提交一致性：不可变 frozen dataclass + `replace` 转换；schema version 常量齐备。
- 序列化不泄漏 secret/绝对路径；`from_dict` 对缺失字段宽容且经 `__post_init__` 复验。
- 类型注解完整；无循环、无浮点比较问题（不适用）。

---

## src/features/edit/ai_editing/analysis_scheduler.py (行数: 425)

### 发现

- [AUD-12901] [Major] `shutdown()` 无锁清空 executor 且 `cancel_futures=True` 丢弃在途 work：job 永久 RUNNING、后续 submit 抛未捕获 RuntimeError
  - 位置: src/features/edit/ai_editing/analysis_scheduler.py:351-356, 397-399
  - 证据:
    ```python
    def shutdown(self, *, wait: bool = False) -> None:
        """关闭生产线程池；工程切换时调用。"""
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)
    ...
        if self._executor is not None:
            self._executor.submit(self._run_work_sync, item)
            return
    ```
  - 影响: ① 已在 `_run_work` 中 `mark_running()` 但尚未被池线程执行的 work 被
    `cancel_futures` 丢弃，对应 job 永久停留 RUNNING（`JobHandle.from_dict` 恢复时会误判为
    "JOB_INTERRUPTED"，但同进程内 registry 的 `active` 计数与 `is_idle` 永不归位）；
    ② `self._executor = None` 不持锁，与 `_run_work` 的读-用序列竞争：读到非 None 后
    `executor.submit` 在 shutdown 后抛 `RuntimeError`，该异常沿 `_try_dispatch` →
    `notify_finished` / `set_playback_active` / `submit` 传播，调用线程（UI 或 worker）未捕获。
    生产装配确有 shutdown 调用（ai_editing_mixin.py:1604，`wait=False`）。
  - 建议: shutdown 在 `self._lock` 内完成交换；被 cancel 的 future 对应 job 统一
    `fail("SCHEDULER_SHUTDOWN", ...)` 并触发 on_cancel；`_run_work` 对 `executor.submit`
    捕获 `RuntimeError` 走同步降级或 fail 路径。
  - 置信度: 高（代码行为确定；触发频率取决于工程切换时序）

- [AUD-12902] [Suggestion] 出队完全依赖外部 `notify_finished`，`submit()` 不主动 drain pending
  - 位置: src/features/edit/ai_editing/analysis_scheduler.py:209-224, 375-389
  - 证据:
    ```python
            can_run_now = self._can_dispatch_locked(item)
            if can_run_now:
                self._active[job.job_id] = item
            else:
                self._pending.append(item)
        # 锁外执行 work（避免 work 内回调死锁）
        if can_run_now:
            self._run_work(item)
        return job
    ```
  - 影响: `_try_dispatch` 仅由 `set_playback_active(False)`（且需状态变化）与
    `notify_finished` 触发；`submit` 只判定新 item 自身。一旦某个 work 忘记调
    `notify_finished`（正是 analysis_service 的实际情况，见 AUD-12903），即使容量已按
    `is_active` 释放，积压任务也永不出队。调度器对契约违反零防御。
  - 建议: `submit` 末尾追加 `self._try_dispatch()`；或在 `_run_work_sync` 成功返回后检测
    `item.job.status.is_terminal` 自动 `notify_finished` 兜底。
  - 置信度: 高

### 通过项
- 线程安全：队列/active/seq 全部在 RLock 内决策，work 与 on_cancel 回调锁外执行防死锁；
  `_safe_invoke` 隔离取消回调异常。
- 取消路径：pending 直接移除并经 `request_cancel()`+`complete({})` 走 CANCELLING→CANCELLED
  （与 jobs.py `JobHandle.complete` 的 CANCELLING 分支语义一致，已核实）；running 标
  CANCELLING 交协作中断，避免重复收尾。
- playback 优先级与 lane 并发上限逻辑（`_can_dispatch_locked` 只计 is_active）正确。
- 类型注解完整；`LaneLimits` frozen。

---

## src/features/edit/ai_editing/analysis_service.py (行数: 871)

### 发现

- [AUD-12903] [Major] 调度路径的 work 闭包从不调用 `notify_finished`、从不终结 asset_job：容量被永久占用，后续分析任务全部停滞
  - 位置: src/features/edit/ai_editing/analysis_service.py:432-472
  - 证据:
    ```python
        def _make_work(asset_id: str) -> "Callable[[], Any]":
            def _work() -> None:
                fingerprint = AssetFingerprint()
                job, err = self.request_analysis(
                    asset_id, fingerprint, fps=fps, provider_ids=provider_ids,
                    require_user_confirmation=req.is_cloud,
                )
                success = err == "" and job is not None and job.status.value == "succeeded"
                if success:
                    completed["count"] += 1
                ...
                _finalize_if_done()
            return _work
    ```
    （462-466 行将该 work 连同 asset_job 提交给 scheduler；闭包内没有任何
    `scheduler.notify_finished(...)` 或 `asset_job.complete(...)` 调用。）
  - 影响: 调度器契约是 "work 内应驱动 job 状态 + notify_finished"（analysis_scheduler.py
    L392、tests/edit/ai_editing/test_analysis_scheduler.py:228 证实）。实际后果三重：
    ① asset_job 被 `_run_work` 标 RUNNING 后永不终结，`_can_dispatch_locked` 按
    `is_active` 计数 → ANALYSIS_LOCAL 并发 2 个槽位在累计两次成功分析后永久占满，
    此后**所有**分析请求（含单素材）在 pending 中永不执行，request 卡 RUNNING；
    ② >2 素材的请求首批两个完成后其余永不出队（无 dispatch 触发，见 AUD-12902）；
    ③ `_active`/`_all` 条目与 summary_job（469-471 行，QUEUED 永不 complete）永久泄漏，
    `is_idle()` 永假。生产链路 media_analysis_controller.py:161 确经此路径
    （asynchronous=True）。
  - 建议: `_work` 末尾（finally）调用 `asset_job.complete/fail` + `scheduler.notify_finished(asset_job.job_id)`；
    或改用调度器自动终结兜底（见 AUD-12902 建议）。
  - 置信度: 高

- [AUD-12904] [Major] AnalysisService 共享状态无锁：worker 线程写 `_records`/`_embeddings`/`_requests`，UI 线程并发枚举可抛 RuntimeError
  - 位置: src/features/edit/ai_editing/analysis_service.py:189-197, 519-533, 502-507, 597-603
  - 证据:
    ```python
        self._records: dict[str, _AssetRecord] = {}
        self._embeddings: dict[str, tuple[float, ...]] = {}
        self._requests: dict[str, AnalysisRequest] = {}
    ...
        def ingest(self, asset_id, analysis, segments) -> None:
            existing = self._records.get(asset_id)
            ...
            self._records[asset_id] = _AssetRecord(analysis=merged, segments=tuple(merged_list))
    ...
        def known_asset_ids(self) -> tuple[str, ...]:
            return tuple(sorted(self._records.keys()))
    ```
  - 影响: 生产调度为线程池（ai_editing_mixin.py:909 `asynchronous=True`），work 在
    pool 线程里经 `request_analysis→ingest/ingest_embeddings` 插入字典；同一时刻 UI 线程的
    `search_assets`（`for rec in self._records.values()`）、`known_asset_ids`/`export_all`
    （`sorted(self._records...)`）、`list_requests` 若遇插入即抛
    "dictionary changed size during iteration"——UI 线程未捕获则崩溃。调度器有 RLock 而
    service 没有，线程边界防护不对称。
  - 建议: 为 `_records`/`_requests`/`_embeddings` 引入 `threading.RLock`（读写均持锁，
    枚举先快照 `list(...)`），或规定 service 只允许单线程访问并在桥接层排队。
  - 置信度: 高（并发写入路径与无锁事实确定；崩溃需时序窗口）

- [AUD-12905] [Major] `_execute_via_scheduler` 的完成计数在并发 worker 间非原子，丢失更新会使 request 永久卡 RUNNING
  - 位置: src/features/edit/ai_editing/analysis_service.py:418-456
  - 证据:
    ```python
        completed = {"count": 0}
        errors: list[str] = []
        canceled = {"count": 0}
        total = len(req.asset_ids)

        def _finalize_if_done() -> None:
            done = completed["count"] + len(errors) + canceled["count"]
            if done >= total:
                ...
    ...
                if success:
                    completed["count"] += 1
    ```
  - 影响: ANALYSIS_LOCAL 并发为 2，两个 pool 线程可同时执行 `completed["count"] += 1`
    （读-改-写，非原子）→ 丢失更新后 `done` 永不到达 `total`，request 停留 RUNNING、
    `on_request_done` 不触发；反向时序则可能双重触发 `_finalize_scheduled_request`。
  - 建议: 用 `threading.Lock` 保护计数与 finalize 判定，或改用 `itertools.count`/
    queue 汇聚单点统计。
  - 置信度: 中（竞争窗口小，但语义上无任何保护）

- [AUD-12906] [Major] `AnalysisBudget`（时长/成本/素材数上限）在执行路径完全未强制执行
  - 位置: src/features/edit/ai_editing/analysis_service.py:287-390
  - 证据:
    ```python
        # 标记 RUNNING；云请求的执行需调用方已获用户对上传范围/费用/保留的单独确认
        self._requests[request_id] = req.with_state(RequestState.RUNNING)

        missing_provider_ids = self.missing_provider_ids(req)
        ...
        for asset_id in req.asset_ids:
            fingerprint = AssetFingerprint()  # 真实指纹由 M3 导入期计算并传入
    ```
    （`execute_analysis` / `request_analysis` / `AnalysisJobScheduler` 全程未读取
    `req.budget`；全库 grep `.budget` 仅命中无关模块。）
  - 影响: `AnalysisBudget.max_duration_seconds / max_cost_units / max_assets` 是 §10.2
    滥用保护合同（"超预算改为等待确认"），但执行层不超时、不限成本、不限素材数——
    云配方（CLOUD_ENHANCED）下费用上限形同虚设，`max_assets` 也拦不住超大工程整批分析。
  - 建议: `execute_analysis` preflight 校验 `len(req.asset_ids) <= budget.max_assets`；
    work 外层加超时/成本检查点，超限迁移 PARTIAL/FAILED（§17.7 语义）。
  - 置信度: 高

- [AUD-12907] [Suggestion] `search_assets` 文本过滤对每个 segment 新建 `SemanticIndex`：O(n) 重对象构建/段
  - 位置: src/features/edit/ai_editing/analysis_service.py:813-827
  - 证据:
    ```python
        for rec in self._records.values():
            for seg in rec.segments:
                if kind is not None and seg.kind != kind:
                    continue
                if text is not None:
                    from src.features.edit.ai_editing.semantic_index import SemanticIndex
                    probe = SemanticIndex()
                    probe.add(seg)
                    if not probe.search_evidence(text, limit=1):
                        continue
    ```
  - 影响: 数小时转录的工程有数千 segment，每次搜索执行数千次"建索引+加一段+查一次"
    外加循环内 import 查询，媒体池搜索明显卡顿；正确做法是单索引一次构建或直接文本匹配。
  - 建议: 构建（并缓存）一个覆盖全部 segment 的索引后统一查询；import 移到模块顶部或
    函数首行。
  - 置信度: 高

- [AUD-12908] [Suggestion] `project_revision` stale 判定未实现：工程改动后 request 并不失效
  - 位置: src/features/edit/ai_editing/analysis_service.py:287-326（对照 analysis_request.py:188）
  - 证据:
    ```python
        # analysis_request.py:188
        project_revision: 创建时工程 revision，用于 stale 判定（工程改动后 request 失效）。
    ...
    # execute_analysis：仅检查 is_confirmed / state==CONFIRMED / provider preflight，
    # 从未比较 req.project_revision 与当前工程 revision。
    ```
  - 影响: 文档承诺的"工程改动后 request 失效"未落地；用户确认与执行之间工程被改动
    （revision 已 bump）时仍按旧范围跑（每素材 fingerprint 校验可部分缓解，但范围本身
    可能已过期）。装配层 media_analysis_controller.py 也仅在 propose 时记录该值（L123）。
  - 建议: `execute_analysis` 入口取当前 revision 与 `req.project_revision` 比对，不一致
    拒绝并要求重新确认。
  - 置信度: 中（未实现是事实；影响被 asset 级校验缓解）

### 通过项
- 红线执行：未确认（`user_confirmed_at=None`）或非 CONFIRMED 状态一律拒绝执行（§17.3）；
  `propose_analysis` 拒绝已确认入参。
- provider preflight：`missing_provider_ids` 拒绝缺必需 provider 的 recipe，防止缩减结果
  伪装为完成；`providers_for_request` 仅作发现过滤，职责分离清晰。
- 异常路径：`request_analysis` 捕获 provider 异常转 `job.fail`；进度回调异常隔离
  （`_notify_progress`）且有注释理由；`restore_all` 对坏记录容错跳过不阻塞工程打开。
- 状态提交一致性：request 状态迁移（RUNNING/FAILED/COMPLETED）与 evidence 的
  fingerprint 失效（`invalidate`/`invalidate_stale`）逻辑清晰；`invalidate` 同步清理
  embeddings。
- 输入验证：`_accepts_provider_options` 用签名探测避免吞掉 provider 内真实 TypeError。
- 类型注解完整。

---

## src/features/edit/ai_editing/approval_service.py (行数: 358)

### 发现

- [AUD-12909] [Major] `ApprovalScope.matches` 单侧 None 绕过：签发方绑定的 diff_hash/plan_hash 等强约束在 expected 侧缺省时不校验
  - 位置: src/features/edit/ai_editing/approval_service.py:79-97
  - 证据:
    ```python
        for field_name in (
            "timeline_id", "base_revision", "plan_hash", "diff_hash", "output_grant_id",
        ):
            mine = getattr(self, field_name)
            theirs = getattr(other, field_name)
            if mine is not None and theirs is not None and mine != theirs:
                return False
        return True
    ```
    （docstring 声称"签发时填写的强约束（如 plan_hash / diff_hash）仍会被强制"，实现并非如此。）
  - 影响: grant 以 `diff_hash=X` 签发后，消费方构造的 expected scope 若 `diff_hash=None`
    即跳过该绑定——grant 可被用于一份从未预览过的 diff，直接违反总体不变量 §1.4
    "预览即提交"。生产确有此类调用：ai_edit_controller.py:1328 / 6372 以
    `_expected_commit_scope(plan)`（不带 diff_hash）consume；transaction_service.py 的
    注释也承认"diff_hash 为 None 时不约束（向后兼容旧路径）"。安全边界的强度取决于
    最弱的调用点。
  - 建议: 匹配语义改为"签发侧非 None 的字段必须双侧相等"（expected 为 None 视为不匹配
    或显式 `enforce=False` 开关）；至少修正 docstring 与实现矛盾并为旧路径提供显式降级标记。
  - 置信度: 高（行为与调用点已核实；实际可利用性取决于签发侧是否总带 diff_hash）

- [AUD-12910] [Suggestion] `validate` 用宽泛 `except Exception` 掩盖错误；`time_fn: Any` 注解过宽
  - 位置: src/features/edit/ai_editing/approval_service.py:186-189, 268-279
  - 证据:
    ```python
        def validate(self, grant_id: str, expected_scope: ApprovalScope) -> bool:
            """非消费式校验（用于"是否仍有效"的预检，不标 used）。"""
            try:
                with self._lock:
                    ...
            except Exception:
                return False
    ...
        def __init__(self, *, time_fn: Any = None) -> None:
    ```
  - 影响: `matches` 不应抛异常，该 try/except 属死代码且违反"不得宽泛 except 掩盖领域
    错误"；`time_fn: Any` 丧失类型检查价值（应为 `Callable[[], float] | None`）。
  - 建议: 移除 try/except 或收窄；`time_fn` 注解改为 `Callable[[], float] | None`。
  - 置信度: 高

### 通过项
- 线程安全：RLock 全覆盖签发/消费/撤销/内省；consume 在锁内原子完成"存在→未过期→
  匹配→标 used"，失败绝不标 used（重放仍按原原因拒绝）。
- 一次性/过期语义正确；过期 grant 在 consume 命中时清理；`revoke_for_project`/
  `revoke_all` 支撑工程切换。
- grant 不携带 secret/原始模型文本/绝对路径；grant_id 用 `secrets.token_urlsafe(32)`。
- `is_expired_at` 使用注入时钟，测试确定性良好。
- 状态提交一致性：compute_plan_hash / compute_diff_hash 用 sort_keys 规范化序列，hash 稳定。

---

## src/features/edit/ai_editing/artifact_models.py (行数: 402)

### 发现

- [AUD-12911] [Suggestion] 四个容器 artifact 只有 `to_dict` 无 `from_dict`：序列化不对称，持久化无法往返
  - 位置: src/features/edit/ai_editing/artifact_models.py:174-189（及其余三类同构代码）
  - 证据:
    ```python
        def to_dict(self) -> dict[str, Any]:
            d: dict[str, Any] = {
                "artifact_id": self.artifact_id,
                "kind": self.kind.value,
                ...
            }
            return d
    ```
    （ScriptArtifact / ShotListArtifact / StoryboardArtifact / DeliveryOutlineArtifact 均无
    对应 `from_dict`；仅元素级 ShotItem/ScriptLine/DeliveryItem 有。）
  - 影响: 模块头部声明"工程关闭可持久化"（store 侧注释），但容器层无重建入口——
    后续接 AssistantSessionStore 模式时需补写，且 now 无测试能覆盖往返一致性。
  - 建议: 为四类容器补 `from_dict`（含 kind 校验）并加往返测试。
  - 置信度: 高

- [AUD-12912] [Suggestion] 死导入：`import time` 与 `field` 未使用
  - 位置: src/features/edit/ai_editing/artifact_models.py:26-27
  - 证据:
    ```python
    import time
    from dataclasses import dataclass, field, replace
    ```
    （文件内无任何 `time.` 调用、无 `field(` 调用，grep 已核实。）
  - 影响: 冗余导入，误导阅读者以为存在时间生成逻辑（实际 created_at 全靠调用方填字符串）。
  - 建议: 删除两个未用导入。
  - 置信度: 高

- [AUD-12913] [Suggestion] 元素级 `from_dict` 不做类型收敛/校验；容器不校验 kind 与类一致
  - 位置: src/features/edit/ai_editing/artifact_models.py:98-108, 161-165
  - 证据:
    ```python
        @staticmethod
        def from_dict(data: dict[str, Any]) -> "ShotItem":
            return ShotItem(
                shot_id=str(data.get("shot_id", "")),
                ...
                asset_id=data.get("asset_id"),
                start_frame=data.get("start_frame"),
    ...
        def __post_init__(self) -> None:
            if not self.artifact_id:
                raise ValueError("artifact_id cannot be empty")
    ```
  - 影响: 外部/持久化 payload 可把 `start_frame` 塞成字符串或把 `asset_id` 塞成 dict，
    `__post_init__` 只查 artifact_id 与 revision；`ScriptArtifact(kind=STORYBOARD)` 也能
    构造成功，`ArtifactStore.list_by_kind` 会把错类 artifact 列进错误分组。
  - 建议: `from_dict` 对 int/str 字段做显式转换或拒绝；`__post_init__` 校验 kind 与类型
    默认值一致。
  - 置信度: 高（事实确定，影响中低）

### 通过项
- 版本化/不可变：全类 frozen + `with_revision_bumped` / `with_user_edit` 转换；revision
  非负校验。
- 用户优先红线的数据基础：`user_edited` + `ArtifactSource`（user/model/skill）字段齐备，
  供 store 强制。
- `ArtifactPatch` 乐观锁字段（base_revision）+ rationale + source 设计合理。
- 不写 timeline；不携带 secret/绝对路径。

---

## src/features/edit/ai_editing/artifact_store.py (行数: 188)

### 发现

- [AUD-12914] [Suggestion] `_merge_patch` 白名单过宽：模型 patch 可写 `model_id`/`brief_revision`/`user_edited`，集合字段可为任意 list/dict
  - 位置: src/features/edit/ai_editing/artifact_store.py:162-180
  - 证据:
    ```python
        valid_fields = {f.name for f in fields(existing)}
        merged_changes: dict[str, Any] = {}
        for k, v in patch.changes.items():
            if k in valid_fields and k not in ("artifact_id", "kind", "revision"):
                merged_changes[k] = v
        merged_changes["revision"] = existing.revision + 1
        if patch.source in (ArtifactSource.MODEL, ArtifactSource.SKILL):
            merged_changes["source"] = patch.source
        merged_changes["created_at"] = str(time.time())
        return replace(existing, **merged_changes)
    ```
  - 影响: ① 模型 patch 的 `changes` 可伪造 provenance（改 `model_id`、改 `brief_revision`
    声称基于某版 brief、置 `user_edited=True` 混淆后续覆盖判定）；② `lines`/`shots`/
    `panels`/`items` 可被塞 list/dict 而非 tuple[...]，破坏 frozen 类型合同，下游
    `to_dict` 行为漂移；③ 任何值不校验类型即 replace。
  - 建议: 白名单收紧为各 kind 的内容字段（title/lines/shots/...），排除
    `model_id`/`brief_revision`/`user_edited`/`source`/`created_at`；集合字段统一
    `tuple(...)` 收敛并做元素类型校验。
  - 置信度: 高

- [AUD-12915] [Suggestion] patch 合并重写 `created_at`：创建时间被当作更新时间覆盖，且时间戳为 str 与全库 float 惯例不一致
  - 位置: src/features/edit/ai_editing/artifact_store.py:179
  - 证据:
    ```python
        merged_changes["created_at"] = str(time.time())
        return replace(existing, **merged_changes)
    ```
  - 影响: 每次模型 patch 后"生成时间"漂移为 patch 时间，provenance 语义失真
    （无法追溯 artifact 最初创建点）；`str(float)` 也无法与 analysis_request 等模块的
    float 时间戳比较/排序。
  - 建议: 保留原 `created_at`，新增 `updated_at: float` 字段记录合并时间。
  - 置信度: 高

### 通过项
- 用户优先红线：`save` 对 user_edited=True 拒绝 MODEL/SKILL 覆盖（allow_override 仅用户
  显式编辑路径）；`apply_patch` 同样拒绝已用户编辑的 artifact。
- 乐观锁：`base_revision` 不符抛 `ArtifactRevisionConflictError`；异常类型
  （Override/RevisionConflict/NotFound）清晰携带诊断字段。
- 无 IO、无 Qt 依赖；`list_by_kind`/`require`/`remove` 行为正确；类型注解完整。

---

## 附注（跨文件事实，供复核）

- 生产调度为线程池模式：src/app/controllers/ai_editing_mixin.py:909
  `AnalysisJobScheduler(asynchronous=True)`；media_analysis_controller.py:161 经
  `execute_analysis(scheduler=...)` 走 `_execute_via_scheduler` 路径。
- `notify_finished` 全库（src/ + tests/）除 analysis_scheduler.py 自身与测试外无生产
  调用者——AUD-12903 的契约违反因此无外部兜底。
- `AnalysisRequest.budget` 与 `project_revision` 在 src/ 内无任何执行期读取
  （grep 核实），AUD-12906/12908 据此立项。
- ApprovalService.consume 的无 diff_hash 生产调用点：ai_edit_controller.py:1328、
  6372（`_expected_commit_scope(plan)`）；带 diff_hash 的调用见
  ai_edit_host_bridge_impl.py:1033、transaction_service.py:526。
