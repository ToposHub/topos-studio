# batch-135 审查报告 — src/features/edit/ai_editing（8 文件，2,922 行）

编号区间：AUD-13500–AUD-13599。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 /
状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
（本批次为纯 Python 服务层，无 GLSL·HDR 与 UI 信号代码；GPU/信号防递归维度逐文件核查为不适用。）

统计：Critical 1 · Major 2 · Suggestion 12。

---

## src/features/edit/ai_editing/export_service.py (行数: 882)

### 发现

- [AUD-13500] [Critical] `complete_host_export` 引用未定义名 `subtitle_duration_frames`，每次调用必抛 NameError，AI 导出完成链路整体断裂
  - 位置: src/features/edit/ai_editing/export_service.py:765
  - 证据:
    ```python
    # 714: 参数名为 subtitle_timeline_duration_frames；719: brand_timeline_duration_frames
    accepted = self.finalize_export_with_probe(
        job,
        manifest,
        probe,
        ...
                    brand_elements=brand_elements,
                    brand_requirements=brand_requirements or None,
                    brand_timeline_duration_frames=subtitle_duration_frames,
                    export_telemetry=export_telemetry,
                )
    ```
    `subtitle_duration_frames` 在 `complete_host_export`（697–768 行）既非参数、非局部变量，也非模块级名字（全库仅 `ai_edit_host_bridge_impl.py:1239` 的调用方局部变量有此名）。该段 763–767 行的异常缩进（续行多缩 8 格）是明显的拼接编辑残迹。
  - 影响: 生产调用方 `src/app/controllers/ai_edit_host_bridge_impl.py:1259` 在编码 worker 的 `_post_export` 回调中调用本方法（AI 发起的成片导出路径）。Python 求值关键字实参时即抛 `NameError`，导出后的 probe / 交付质量门 / manifest 物化全部不会执行，job 永远得不到正常的 complete/fail 终态——与模块 docstring "取消、编码失败……均不会产出看似成功的文件" 的硬门设计直接冲突。
  - 建议: 改为 `brand_timeline_duration_frames=brand_timeline_duration_frames`（如需与字幕时长共用，则显式传 `subtitle_timeline_duration_frames`）；补一条经 `complete_host_export` 的端到端测试（当前仅 `finalize_export_with_probe` 有直调）。
  - 置信度: 高

- [AUD-13513] [Suggestion] `request_preview` 未校验 `clip_range.start` 落在序列范围内
  - 位置: src/features/edit/ai_editing/export_service.py:485-489
  - 证据:
    ```python
    if clip_range is not None:
        preview_range = FrameRange(
            clip_range.start,
            max(1, min(clip_range.duration, preset.max_frames)),
        )
    ```
  - 影响: `clip_range` 来自 TAEP `request_export` 类 operation（外部结构化输入），只对 duration 做了截断，start 可以超出 `sequence.duration_frames`，产生空转/越界的预览 job，错误只能在编码 worker 里晚期暴露。
  - 建议: 构造 `preview_range` 前校验 `0 <= clip_range.start < seq_duration`，越界返回诊断（与 `build_export_config` 对 timeline_id 的处理一致）。
  - 置信度: 低

- [AUD-13514] [Suggestion] manifest 脱敏只覆盖 `/Users/` 与 `/private/` 前缀，非 macOS 本机路径仍会写入交付 manifest
  - 位置: src/features/edit/ai_editing/export_service.py:833-836
  - 证据:
    ```python
    if isinstance(value, str):
        return value.replace("/Users/", "[PATH_REDACTED]/").replace(
            "/private/", "[PATH_REDACTED]/",
        )
    ```
  - 影响: docstring 承诺 "never stores the output path in the manifest"、"without persisting machine-local paths"，但 Linux `/home/...`、Windows `C:\Users\...` 等绝对路径不会被替换，元数据里携带的素材/中间产物路径会泄漏到交付物。
  - 建议: 按绝对路径启发式（POSIX `/home`、Windows 盘符 `^[A-Za-z]:[\\/]`）统一替换，或仅白名单保留相对路径。
  - 置信度: 低

- [AUD-13515] [Suggestion] `run_export_preflight` / `build_export_config` 关键参数与返回值缺类型注解
  - 位置: src/features/edit/ai_editing/export_service.py:256-260, 356-362
  - 证据:
    ```python
    def run_export_preflight(
        config, sequence, *, generation_bin: Any | None = None,
        ...
    ) -> PreflightReport:

    def build_export_config(
        self, request: ExportRequest, state: ProjectSequences
    ):
    ```
  - 影响: `config`/`sequence` 为隐式 duck type，`build_export_config` 返回 `(TimelineExportConfig | None, Diagnostic | None)` 无注解；跨模块调用方（宿主 bridge）只能靠读实现确认合同。
  - 建议: 标注为 `TimelineExportConfig`、`TimelineSequence`（局部 import 以避免循环依赖）与 `tuple[TimelineExportConfig | None, Diagnostic | None]`。
  - 置信度: 高

### 通过项
- 异常与取消路径：preflight 异常转 blocking（fail-closed，281–284 行）；`finalize_export_with_probe` 中 probe/分析/字幕/品牌校验异常均 `job.fail` 终态化，不产出"看似成功"文件。
- 状态提交一致性：`config.ai_job_id = job.job_id`（458 行）写入的是可变 `@dataclass` `TimelineExportConfig`（timeline_export.py:414, 500 存在 `ai_job_id` 字段），合法。
- 输入验证：codec 用 `ExportCodec(request.codec)` 枚举校验、fps 用 `Rational.from_string` 包裹校验、timeline 缺失返回诊断。
- 原子写：manifest 与 `_update_export_manifest` 均 mkstemp + fsync + `os.replace`，失败清理 fd/tempfile。
- 线程安全：`complete_host_export` 文档明确在编码 worker 线程执行，内部只做 subprocess/文件 IO，不触 Qt/OpenGL。
- `DeliveryQualityInput` 全部 `replace()` 关键字（platform_errors、subtitle_*、brand_*、expected_lufs 等）均与 delivery_quality_gate.py:27-78 字段核对一致。

---

## src/features/edit/ai_editing/execution_policy.py (行数: 444)

### 发现

- [AUD-13501] [Major] `restricted_auto` tier 覆盖会取消 AUTO_PUBLISH 的"独立同意边界"，自动发布可在无审批下放行
  - 位置: src/features/edit/ai_editing/execution_policy.py:299-305 与 368-370
  - 证据:
    ```python
    approval_required = self._approval_required(action)
    if approval_required_override is True:
        approval_required = True
    elif approval_required_override is False and self.permission_tier == "restricted_auto":
        approval_required = False
    ...
    def _approval_required(self, action: ExecutionAction) -> bool:
        if action == ExecutionAction.AUTO_PUBLISH:
            return True  # publishing has an independent consent boundary
    ```
  - 影响: `from_permission_tier("restricted_auto", ...)` 构造的策略（tier 恒为 "restricted_auto"）收到 AUTO_PUBLISH 请求时：`permission_tier_allows_write` 返回 True → `approval_required_override=False` → 302-303 行把 `_approval_required` 特意置 True 的发布同意要求覆盖为 False → `evaluate` 直接 `PolicyDecision(True)`。三种模式下的硬不变量"发布必须显式同意"被 AG-07 tier 逻辑静默穿透（risk_flags 为空时无任何兜底）。
  - 建议: 在 tier 覆盖分支前保留 `if action is ExecutionAction.AUTO_PUBLISH: approval_required = True`（或从覆盖逻辑中排除 AUTO_PUBLISH），并补 tier×AUTO_PUBLISH 的判定测试。
  - 置信度: 中（行为可从代码确证；是否"tier 有意放开发布"无注释依据，但 368-370 行注释表明发布同意是刻意硬边界）

### 通过项
- 输入验证：`ExecutionScope.__post_init__` 校验 project_id/revision/budget/duration；负 cost/duration 拒绝；未知 action、未知 tier 均 fail-closed（220-221, 284-290 行）。
- 异常路径：`from_feature_flags._enabled` 的宽 `except Exception` 返回 False（flag 坏 → 全部禁用，fail-safe，带 noqa 说明，可接受）；`check_plan._number` 解析失败返回 -1.0 → 被"negative cost or duration"拒绝，fail-closed（拒绝理由略有误导，见上）。
- 主机边界：`allows_host` 归一化大小写/尾点、缺 scheme 补 https；`allows_extension` 补前导点；空 allow-list 语义正确（hosts 空白名单=全拒、extensions 空=不限）。
- 类型注解完整；纯决策层无 Qt/GPU/信号，无状态无锁设计自洽。

---

## src/features/edit/ai_editing/generated_media_materializer.py (行数: 329)

### 发现

- [AUD-13502] [Major] 恶意/畸形 `Content-Length: 0` 头导致 `ZeroDivisionError`，逃逸下载错误边界
  - 位置: src/features/edit/ai_editing/generated_media_materializer.py:182-186（配合 162-163 的前置校验缺口）
  - 证据:
    ```python
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > self._max_bytes:
        return MaterializeResult(None, "远程媒体超过本地缓存大小上限")
    ...
    if progress is not None and content_length:
        progress(
            min(0.99, written / int(content_length)),
            "正在下载生成媒体",
        )
    ```
    而 204 行的错误边界是 `except (OSError, ValueError, TypeError, urllib.error.URLError)`，不含 `ZeroDivisionError`。
  - 影响: 服务器返回 `Content-Length: 0` 但仍发送 body 时（违反 RFC 但常见于故障网关/恶意 provider），`"0"` 为真值、能通过 163 行上限检查，首块写盘后 `written / 0` 抛 `ZeroDivisionError`：同步 `materialize_with_progress` 调用方直接收到未分类异常；异步路径虽被 301 行 `except Exception` 兜住，但错误码退化为 MATERIALIZE_EXCEPTION、诊断信息丢失。服务器响应头是外部数据，未验证为正数（违反"外部数据须验证"）。
  - 建议: 读取头时一次性 `length = int(content_length) if content_length and content_length.isdigit() else None`，仅当 `length and length > 0` 时用于进度分母；并把该解析纳入 204 行 try。
  - 置信度: 高（触发路径确定，仅前提依赖畸形服务器）

- [AUD-13503] [Suggestion] 物化下载无 host allow-list，provider 句柄可指向任意 http(s) 主机
  - 位置: src/features/edit/ai_editing/generated_media_materializer.py:115-119, 137-160
  - 证据:
    ```python
    if parsed.scheme in ("http", "https"):
        return self._download(
            asset, parsed, progress=progress, is_cancelled=is_cancelled,
        )
    ...
    url = urllib.parse.urlunparse(parsed)
    request = urllib.request.Request(url, headers={...})
    response = self._opener(request, timeout=self._timeout)
    ```
  - 影响: 模块 docstring 声明的安全边界只有 scheme/大小/原子写；`ExecutionScope.allows_host` 的主机白名单机制并未在此应用，被 compromise 的 provider 可让客户端向内网地址（SSRF 探测）或第三方站点发起 GET 并落盘缓存。
  - 建议: 构造器接受可选 `allowed_hosts`（复用 execution_policy 的归一化逻辑），不在白名单内的句柄直接返回失败。
  - 置信度: 中

- [AUD-13504] [Suggestion] 缓存命中仍全文重算 sha256，每次预览 O(文件大小)
  - 位置: src/features/edit/ai_editing/generated_media_materializer.py:141-149, 218-244
  - 证据:
    ```python
    if target.is_file() and target.stat().st_size > 0:
        cached = self._with_metadata(
            str(target), "已复用生成媒体缓存", expected_sha256=asset.expected_sha256,
        )
    ```
    `_with_metadata` 对文件做 1MB 分块全文 `hashlib.sha256`（226-229 行）。
  - 影响: `DEFAULT_MAX_BYTES` 上限 512MB；在 4K 视频工作流里每次复用缓存都要整段重哈希（数百 MB 读取，秒级阻塞），同一候选反复预览时被放大。
  - 建议: 下载成功后把 `<目标路径, size, sha256>` 写成 sidecar 记录，复用时按 size+记录跳过重哈希；或在复用路径仅当调用方要求校验时才重算。
  - 置信度: 低

### 通过项
- 异常与取消路径：下载循环每块检查 `is_cancelled`，取消/超限/空文件均返回 `MaterializeResult` 不留半文件；`finally` 清理临时文件；async worker 兜底 `except Exception` 并区分 cancelling。
- 原子写：同目录 mkstemp + fsync + `os.replace`；checksum 校验失败会删除坏缓存（199-202 行）。
- 输入验证：scheme 白名单（file netloc 仅空/localhost）；`_safe_id`/`_suffix` 阻止文件名注入与超长后缀；`max_bytes`/`timeout` 构造期校验；不信任 Content-Disposition。
- 线程模型：纯无共享状态实例 + 每任务 daemon 线程，进度经 `JobHandle` 回传，无 Qt 依赖。
- 缓存并发：双线程同时物化同一 asset 时 temp 文件互不冲突，`os.replace` 末写者胜，无撕裂。

---

## src/features/edit/ai_editing/generation_bin.py (行数: 353)

### 发现

- [AUD-13505] [Suggestion] `GenerationBin` 无锁也无线程契约说明，而同包存在后台物化线程回调入口
  - 位置: src/features/edit/ai_editing/generation_bin.py:116-118, 160-176
  - 证据:
    ```python
    def __init__(self) -> None:
        self._assets: dict[str, GeneratedAsset] = {}
        self._import_events: list[ImportEvent] = []

    def mark_previewed(self, asset_id: str) -> GeneratedAsset:
        a = self.require(asset_id)
        ...
        self._assets[asset_id] = updated
    ```
    对比同目录 `FeedbackStore`（feedback_store.py:181-183）用 `threading.RLock` 并在 docstring 声明 thread-safe；`EvalTraceStore` 也显式声明"线程安全由调用方保证"。本类两者皆无。
  - 影响: `AsyncGeneratedMediaMaterializer` 的 `on_completed` 在 daemon 线程触发；当前宿主（ai_edit_host_bridge_impl.py:1444 的 `set_preview_handle` 注释"在主线程"）恰好主线程访问，但没有任何机制阻止后续接线在 worker 回调里并发 `add_candidates`/`discard`，dict 迭代期间改写会 `RuntimeError`。
  - 建议: 或加一把 `threading.RLock` 包住读写，或照 `EvalTraceStore` 在类 docstring 显式声明"单线程模型，跨线程访问由调用方串行化"。
  - 置信度: 低

- [AUD-13506] [Suggestion] 未使用的导入 `Callable`
  - 位置: src/features/edit/ai_editing/generation_bin.py:33
  - 证据:
    ```python
    from typing import Any, Callable, Protocol
    ```
    全文件无任何 `Callable[` 使用（唯一 typing 消费是 `Any` 与 `Protocol`）。
  - 影响: 死导入；误导读者以为存在回调注入点。
  - 建议: 改为 `from typing import Any, Protocol`。
  - 置信度: 高

### 通过项
- 状态机一致性：`mark_previewed` 幂等不降级；IMPORTED 不可 `discard`；`import_to_media_pool` 失败不改状态、成功才迁移并记 `ImportEvent`（approved_by/license_at_import 齐全）。
- 输入验证：导入 gate 覆盖 safety.blocked / qa.blocked / already imported / discarded，理由逐条拼进诊断；`set_preview_handle` 拒绝空句柄；`require` 对缺失 id 抛 KeyError。
- `provenance_report` 审计计数与 `audit_complete` 聚合逻辑自洽；`clear_discarded` 保留 imported 审计链。
- `add_candidates` 对重复 asset_id 幂等跳过（docstring 声明）。
- 无 Qt/GPU/文件 IO，不适用 GLSL·HDR、信号防递归维度。

---

## src/features/edit/ai_editing/eval_trace.py (行数: 318)

### 发现

- [AUD-13507] [Suggestion] `clear()` 把 run_id 计数器归零，删除后再评估会产生与已导出轨迹相同的 run_id
  - 位置: src/features/edit/ai_editing/eval_trace.py:306-309
  - 证据:
    ```python
    def clear(self) -> None:
        """清空（§15.3 数据删除）。"""
        self._traces.clear()
        self._counter = 0
    ```
  - 影响: §15.3 允许"导出后删除"；若用户先导出 run-000001~N 的轨迹再 `clear()`，新一轮 eval 又从 run-000001 开始，外部留存的导出与后续回归对比/审计回放会出现同 ID 不同内容，破坏 "run_id 单调，便于按时间排序" 的可追溯承诺。
  - 建议: `clear()` 不重置 `_counter`（保留单调性），或重置时记录 epoch 前缀。
  - 置信度: 低

### 通过项
- 线程契约：docstring 明示"线程安全由调用方在 eval-runner 层保证（单线程模型）"，实现无隐藏共享可变状态（`values` dict 经 `from_gate_result` 重建，不共享引用）。
- 回归对比：`prev_val is None` 判断正确处理 0.0 值指标；方向映射默认 higher_is_better 且可覆盖。
- 输入验证：不采集 prompt/素材原文的设计红线在字段层落实（只记 code/计数/比率）。
- FIFO 上限 100 条，`previous` 按插入序语义正确；导出 `to_dict` 递归完整（依赖的 `QualityGateResult.metrics`/`MatrixResult`/`CommercialAcceptanceReport` 的 to_dict 均已核实存在）。

---

## src/features/edit/ai_editing/evidence_kinds.py (行数: 239)

### 发现

- [AUD-13508] [Suggestion] `fingerprint()` 实际未纳入 `selection`，与 docstring 相悖，选择状态变化不会被 stale 检测捕获
  - 位置: src/features/edit/ai_editing/evidence_kinds.py:179-192
  - 证据:
    ```python
    def fingerprint(self) -> str:
        """Stable hash over all domain fingerprints + revision + selection."""
        payload = {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "base_revision": self.base_revision,
            "domains": [
                {"domain": d.domain, "revision": d.revision, "fingerprint": d.fingerprint}
                for d in self.domains
            ],
            "delivery_profile_id": self.delivery_profile_id,
        }
    ```
    payload 中没有 `selection`（也没含 `markers`/`effects_summary`，但后两者未被文档承诺）。
  - 影响: OS-04 的跨域 stale 检测若以该指纹为凭据，用户仅改变 selection（选区）而域快照未变时指纹不变，下游 planner/policy 会误判上下文仍新鲜；同时文档合同与实现不一致误导后续维护。
  - 建议: 把 `selection: dict(self.selection)` 加入 payload，或修改 docstring 移除 "+ selection" 并注明选择变化由独立字段判脏。
  - 置信度: 高（文档与代码不一致确凿；实际影响取决于宿主消费方式）

- [AUD-13509] [Suggestion] `stale_domains` 只报告双侧同名域的指纹差异，单侧新增/删除的域永远不被视为 stale
  - 位置: src/features/edit/ai_editing/evidence_kinds.py:194-201
  - 证据:
    ```python
    def stale_domains(self, other: "ExtendedProjectSnapshot") -> tuple[str, ...]:
        """Return domain names whose fingerprint changed vs ``other``."""
        mine = {d.domain: d.fingerprint for d in self.domains}
        theirs = {d.domain: d.fingerprint for d in other.domains}
        return tuple(
            name for name in mine
            if name in theirs and mine[name] != theirs[name]
        )
    ```
  - 影响: 本快照新增的域（对方没有）或对方有而本快照缺失的域都不进结果；域级 stale 检测对"域被移除/新增"的结构性变化失明。
  - 建议: 视语义补充 `name in mine and name not in theirs`（或提供 `added/removed` 单独返回），至少在 docstring 标明只比较交集。
  - 置信度: 中

### 通过项
- 输入验证：`EvidenceRecord.__post_init__` 校验 evidence_id/kind 白名单/asset_id/confidence ∈ [0,1]；`is_expired` 语义（None=不过期）正确。
- 无 IO/线程/Qt；fingerprint 的 canonical JSON（sort_keys + 分隔符 + default=str）稳定可复现。
- 冻结 dataclass + `to_dict` 逐层拷贝（浅拷贝 Mapping 足够本用途）。

---

## src/features/edit/ai_editing/evidence_policy.py (行数: 67)

### 发现

- [AUD-13510] [Suggestion] `known_refs` 传集合形式时 selection-scope 校验静默失效（no-op 而非报错）
  - 位置: src/features/edit/ai_editing/evidence_policy.py:44-58
  - 证据:
    ```python
    if isinstance(known_refs, Mapping):
        known = set(known_refs)
        asset_by_ref = {...}
    else:
        known = {str(ref) for ref in known_refs}
        asset_by_ref = {}
    ...
    unscoped = tuple(sorted(
        ref for ref in selected
        if allowed and ref in asset_by_ref and asset_by_ref[ref] not in allowed
    ))
    ```
  - 影响: 调用方同时给出 `allowed_asset_ids` 与 set 形式的 `known_refs` 时（docstring 明示两种形态都合法），`asset_by_ref` 恒空 → `unscoped` 恒空 → "越权引用未选中素材的证据"这一检查静默通过，`valid` 可能误为 True。docstring 未警示该组合下范围校验不可用。
  - 建议: 当 `allowed_asset_ids` 非空且 `known_refs` 非 Mapping 时，在 `EvidenceValidation` 上显式返回不可验证（如 reason 附 "scope check unavailable"）或直接置 invalid，避免静默放行。
  - 置信度: 中

### 通过项
- 诊断输出只含 ID/asset_id，无 prompt/转写文本（符合隐私红线）。
- `required_count` 经 `max(0, int(...))` 归一；unknown 用集合差集后排序，输出确定序。
- 纯函数无状态，天然线程安全；类型注解完整。

---

## src/features/edit/ai_editing/feedback_store.py (行数: 290)

### 发现

- [AUD-13511] [Suggestion] 脱敏边界不一致：`from_dict` 不经 `sanitize_for_storage`，`to_dict` 只洗 `requested_changes` 不洗 `note`
  - 位置: src/features/edit/ai_editing/feedback_store.py:131-134, 149
  - 证据:
    ```python
    "requested_changes": sanitize_for_storage(dict(self.requested_changes)),
    "artifact_refs": list(self.artifact_refs),
    "note": self.note,
    ...
    note=str(data.get("note", "")),
    ```
    对比 `create()`（214 行）在构造边界对 note 做 `sanitize_for_storage(...)[:500]`。
  - 影响: 模块设计规则声称 "no raw prompts / secrets / absolute paths — feedback text is sanitized through sanitize_for_storage boundary"。经 `from_dict` 重建（导入/迁移路径）的 patch 携带未脱敏 note，且 `to_dict` 原样回写，脏文本可进入持久化/导出；`create` 的 500 字截断在 `from_dict` 路径同样缺失。
  - 建议: `from_dict` 对 note 与 requested_changes 走同一 sanitize+截断；或 `to_dict` 对 note 也过 `sanitize_for_storage`，保证任一出口都被清洗。
  - 置信度: 中

- [AUD-13512] [Suggestion] `latest()` 按插入序取末位，忽略 `created_at`（`create()` 支持显式传入该值）
  - 位置: src/features/edit/ai_editing/feedback_store.py:253-260
  - 证据:
    ```python
    def latest(self, *, run_id: str | None = None, project_id: str | None = None) -> GeneralFeedbackPatch | None:
        with self._lock:
            scoped = [
                p for p in self._patches
                if (run_id is None or p.run_id == run_id)
                and (project_id is None or p.project_id == project_id)
            ]
            return scoped[-1] if scoped else None
    ```
    而 `create(..., created_at: float | None = None)` 允许乱序构造（200, 215 行）。
  - 影响: 回放/导入历史反馈（created_at 早于现存 patch）后，`latest` 返回的是最后插入而非时间最近的反馈，replan 策略可能基于过期用户意见。
  - 建议: `return max(scoped, key=lambda p: p.created_at) if scoped else None`（同刻并列时保留插入序语义可用 `enumerate` 次键）。
  - 置信度: 低

### 通过项
- 线程安全：全部读写经 `threading.RLock`，`clear_for_project` 原子重建列表。
- 输入验证：`__post_init__` 强制 patch_id/project_id 非空、domain 与 categories 白名单校验；`categories` 非空。
- `hash()` 的 canonical JSON（sort_keys/分隔符/default=str）稳定；不含 note，避免把自由文本纳入指纹。
- 无 Qt/GPU/文件 IO；类型注解完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-13500 |
| Major | 2 | AUD-13501, AUD-13502 |
| Suggestion | 12 | AUD-13503–AUD-13515（见上各节） |

优先修复顺序建议：AUD-13500（生产路径必崩）→ AUD-13502（外部输入可触发未捕获异常）→ AUD-13501（同意边界被覆盖）。
