# batch-140 审查报告 — src/features/edit/ai_editing/（6 文件，2,250 行）

编号区间：AUD-14000–AUD-14099。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
本批次均为纯数据合同 / 策略 / provider 适配层：无 OpenGL 调用、无 GLSL、无 UI 控件，故 GPU·GLSL·信号防递归三个维度整体不适用（在各文件通过项中说明）。

---

## src/features/edit/ai_editing/models.py (行数: 560)

### 发现

- [AUD-14000] [Major] ValidationReport.from_dict 的容错回退引用不存在的 `PlanStatus.INVALID`，该分支自身抛 AttributeError
  - 位置: src/features/edit/ai_editing/models.py:355-358
  - 证据:
    ```python
    try:
        status = PlanStatus(data.get("status"))
    except ValueError:
        status = PlanStatus.INVALID
    ```
    而 PlanStatus 定义（models.py:298-304）只有 VALID/REJECTED/APPLIED/STALE 四个成员，无 INVALID。
  - 影响: 历史校验结果含未知/缺失 status 字符串（跨版本工程文件、损坏数据）时，本应降级重建的路径反而抛未捕获 AttributeError，加载回看功能崩溃；docstring 声称的"工程重开后回看历史校验结果"承诺被打破。全库 grep 确认 `PlanStatus.INVALID` 仅此一处引用。
  - 建议: 增加 `INVALID = "invalid"` 枚举成员，或回退为 `PlanStatus.REJECTED` 并追加一条 WARNING Diagnostic；补一条未知 status 的单测。
  - 置信度: 高

- [AUD-14001] [Suggestion] EditPlan.from_dict 的 requested_outputs.preset 未做类型归一，与同函数其余字段的强转换风格不一致
  - 位置: src/features/edit/ai_editing/models.py:278-285
  - 证据:
    ```python
    requested_outputs=tuple(
        RequestedOutput(
            kind=str(ro["kind"]),
            range=FrameRange.from_dict(ro["range"]) if ro.get("range") else None,
            preset=ro.get("preset"),
        )
        for ro in data.get("requested_outputs", [])
    ),
    ```
  - 影响: 外部（LLLM/MCP）传入的 preset 若为 dict/list 等非字符串值会被原样存入 `preset: str | None`，破坏类型合同，后续导出 preset 匹配可能出现不可预期比较行为。
  - 建议: `preset=str(ro["preset"]) if ro.get("preset") is not None else None`，与其他字段一致。
  - 置信度: 高

- [AUD-14002] [Suggestion] frozen 契约对象内嵌可变 dict 且构造时不做防御性拷贝，调用方保留引用即可改写"稳定合同"
  - 位置: src/features/edit/ai_editing/models.py:114-117（同类：390-391 SnapshotTrack.caption_metadata）
  - 证据:
    ```python
    op_id: str
    kind: OperationKind
    params: dict[str, Any] = field(default_factory=dict)
    intent: dict[str, Any] = field(default_factory=dict)
    ```
    构造时直接持有传入 dict 引用；to_dict 仅一层浅拷贝 `dict(self.params)`。
  - 影响: frozen 只阻止字段重绑定，不阻止内容改写；构造方在提交后修改 params 字典会改变已审计 plan 的语义，与模块自述的"稳定合同/审计友好"目标冲突（含嵌套结构时 to_dict 浅拷贝也会外泄内部引用）。
  - 建议: `__post_init__` 中 `object.__setattr__(self, "params", dict(self.params))` 做浅防御拷贝，或改用递归只读结构；至少在文档标注此限制。
  - 置信度: 中

### 通过项
- 线程安全：纯不可变数据合同，无可变共享状态（可变 dict 字段见 AUD-14002）。
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：不适用（无 OpenGL、无着色器、无 Qt 控件）。
- 状态提交一致性：EditPlan 携带 base_revision 乐观锁并在 `__post_init__` 校验 >= 0；PlanMode/OperationKind 白名单枚举化，from_dict 对未知 kind/mode 抛 ValueError（外部数据验证到位）。
- 输入验证：op_id/plan_id 非空、base_revision 非负均在构造期强制。
- 类型注解：完整。

---

## src/features/edit/ai_editing/motion_graphic_artifacts.py (行数: 260)

### 发现

- [AUD-14003] [Major] scene_is_stale 在"空 context_fingerprint + 未传 current_scene_revision"的默认组合下恒返回 False，写入门的过期检测被完全旁路
  - 位置: src/features/edit/ai_editing/motion_graphic_artifacts.py:210-228（配合 can_write_artifact:231-251）
  - 证据:
    ```python
    if current_scene_revision is not None:
        if request.scene_revision != current_scene_revision:
            return True
    if artifact.approved_scene_hash and request.context_fingerprint:
        if artifact.approved_scene_hash != request.context_fingerprint:
            return True
    return False
    ```
    而 `MotionGraphicBuildRequest.context_fingerprint: str = ""` 默认空，`can_write_artifact(current_scene_revision=None)` 默认 None。
  - 影响: 两个守卫都是"可选的"——宿主未填指纹且未传当前 revision 时（类型系统不阻止），场景在确认后任意变更都不会使 artifact 过期，`can_write_artifact` 仍放行写入，违背模块自述"confirmation must check scene revision + context fingerprint + artifact hash"。实测调用方 ai_edit_controller.check_motion_graphic_write_gate 即以 `scene_revision: int | None = None` 传入且 request 指纹默认可为空，风险可达。
  - 建议: 对"两者皆未提供"显式返回 stale（fail-closed），或至少返回 `(False, "缺少场景版本依据")`；在 MotionGraphicBuildRequest 构造期要求非空指纹或显式声明无指纹。
  - 置信度: 中（代码行为确定；实际触发取决于宿主是否总是填充指纹）

- [AUD-14004] [Major] HtmlArtifact.from_dict 无条件恢复 writable 与 render_evidence，违背"恢复后必须重新验证"不变量
  - 位置: src/features/edit/ai_editing/motion_graphic_artifacts.py:184-202
  - 证据:
    ```python
    quality_report=dict(d.get("quality_report", {})),
    render_evidence=dict(d.get("render_evidence", {})),
    approved_scene_hash=str(d.get("approved_scene_hash", "")),
    writable=bool(d.get("writable", False)),
    ```
  - 影响: motion_graphic_run.py 模块自述不变量"Re-verification on resume: a restored artifact is never trusted without fresh verification"，但 from_dict 把确认态（writable）和验证证据原样复活；持久化恢复或任何来自外部 dict 的重建调用 can_write_artifact 将直接通过全部门（staged/verified/writable），无需用户再次确认、无需新浏览器验证。当前 src 内无该 from_dict 的调用方（潜伏 API），风险随恢复功能接入而激活。
  - 建议: from_dict 强制 `writable=False`（恢复后须重新走确认），或提供显式参数 `restore_trusted=False` 且默认关闭；render_evidence 恢复时打上 `restored=True` 标记供门函数降权。
  - 置信度: 中

### 通过项
- 线程安全：不可变数据类 + 纯函数判定，无共享可变状态。
- GPU / GLSL·HDR / 信号防递归：不适用。
- 输入验证：task_id/artifact_id 非空校验、page_count 负数钳 0、render_mode 归一化；to_dict 对 scene_id/user_brief/quality_report 走 sanitize_for_storage（已核实存在于 ai_harness/invocation_models.py:669）。
- can_write_artifact 的 staged/verified/writable 四门顺序正确（staged → evidence → writable → stale）。
- 类型注解：完整。

---

## src/features/edit/ai_editing/motion_graphic_run.py (行数: 256)

### 发现

- [AUD-14005] [Suggestion] confirm_and_write 在门检查通过后直接 grant_approval，run 不在 WAITING_APPROVAL 时抛 RunNotTransitionableError 而非返回否定决策；request_confirmation 仅处理 GATHERING_CONTEXT 一种前置状态
  - 位置: src/features/edit/ai_editing/motion_graphic_run.py:218-220（配合 165-176）
  - 证据:
    ```python
    # All preconditions met: confirmation grants writable + advances to EXECUTING.
    writable_artifact = replace(artifact, writable=True)
    self._orchestrator.grant_approval(run_id)
    ```
    以及 request_confirmation 中仅：
    ```python
    if run is not None and run.state == AgentRunState.GATHERING_CONTEXT:
        self._orchestrator.mark_context_ready(run_id)
    ```
  - 影响: 已核实 AgentOrchestrator._advance 在 state != WAITING_APPROVAL 时抛 RunNotTransitionableError；宿主若未先调 request_confirmation（或 run 处于 WAITING_USER_INPUT——状态表不允许直接转 WAITING_APPROVAL，抛 ValueError），confirm 路径以异常逃逸而非返回 `writable=False + reason`，与该类"staging + gate 层返回决策对象"的定位不符。
  - 建议: grant_approval 前 get_run 检查状态，非 WAITING_APPROVAL 时返回否定决策（reason="run 尚未进入审批态"）；request_confirmation 对不可转移状态同样返回可解释错误。
  - 置信度: 高（行为确定；实际影响取决于宿主调用顺序）

- [AUD-14006] [Suggestion] record_verification 无条件 emit RUN_VERIFICATION_FINISHED，与 orchestrator 的同名事件构成两套语义，run 事件流中"验证"生命周期重复出现
  - 位置: src/features/edit/ai_editing/motion_graphic_run.py:136-143（对照 229-233 complete_write）
  - 证据:
    ```python
    updated = replace(
        artifact,
        render_evidence=render_evidence or {},
        ...
    )
    self._orchestrator.emit(run_id, AgentEventKind.RUN_VERIFICATION_FINISHED)
    return updated
    ```
    而 complete_write 又经 begin_verification/finish_verification 再次发出 RUN_VERIFICATION_STARTED/FINISHED（伴随真实状态迁移）。
  - 影响: evidence 为空或浏览器验证失败时也宣告"verification finished"；订阅方（Run Drawer/指标）会在 EXECUTING 之前看到一对 started/finished，之后又看到一对，指标统计（验证时长/次数）与 UI 阶段展示被污染。
  - 建议: 为浏览器验证使用独立事件（如 artifact.verification_finished），或仅在 render_evidence 非空时 emit；complete_write 复用 orchestrator 的状态化事件。
  - 置信度: 中

### 通过项
- 线程安全：自身无共享可变状态，全部状态变更经 AgentOrchestrator（已核实内部 RLock 保护 run/event/task 映射、订阅者通知在锁外）。
- 写入门逻辑：confirm_and_write 正确复检 staged → evidence → stale 且不误查 writable 自身（注释明确"确认即授权 writable"）；request_confirmation 先盖 approved_scene_hash 戳再进入 WAITING_APPROVAL。
- GPU / GLSL·HDR / 信号防递归：不适用（无 OpenGL、无控件）。
- 完成路径：complete_write 的 VERIFYING → 终态推进由 orchestrator 校验源状态，fail() 对终态 no-op。
- 类型注解：完整（MotionGraphicWriteDecision、返回值均有注解；complete_write/fail 返回 Any 与 orchestrator AgentRun 对齐可再收紧）。

---

## src/features/edit/ai_editing/music_policy.py (行数: 165)

### 发现

- [AUD-14007] [Major] license 映射把 "cleared" 吞进 LICENSED 分支，`LicenseStatus.CLEARED` 赋值分支不可达，"已清权"被归类为"已授权"
  - 位置: src/features/edit/ai_editing/music_policy.py:93-98
  - 证据:
    ```python
    license_str = str(meta.get("music_license", meta.get("license", "unknown"))).lower()
    license_status = LicenseStatus.LICENSED if license_str in ("licensed", "cleared", "授权", "已授权") else (
        LicenseStatus.CLEARED if license_str == "cleared" else (
            LicenseStatus.PENDING if license_str == "pending" else LicenseStatus.UNKNOWN
        )
    )
    ```
  - 影响: license_str == "cleared" 时第一个条件已命中 → LICENSED，第二个 `CLEARED if license_str == "cleared"` 永不执行（死代码）。模块核心职责是 TAEP §11.7"必须显示授权/来源"的诚实展示：已清权（用户自证版权）素材会被 UI 显示为"已授权"（渠道授权），授权语义失真；CLEARED 枚举成员实际永远无法通过该入口产生。
  - 建议: 第一个元组去掉 "cleared"，让 "cleared" 落入第二分支；补 LICENSED/CLEARED/PENDING/UNKNOWN 四路径单测。
  - 置信度: 高

- [AUD-14008] [Suggestion] register_from_media_asset 对外部 metadata 的 bpm 直接 float()、mood 不验类型，恶意/损坏元数据可抛异常或产生 NaN 破坏排序
  - 位置: src/features/edit/ai_editing/music_policy.py:99-107
  - 证据:
    ```python
    bpm = meta.get("bpm")
    music = MusicAsset(
        ...
        bpm=float(bpm) if bpm is not None else None,
        mood=meta.get("mood"),
    ```
  - 影响: metadata 属外部数据（工程文件/导入资产标签）：bpm 为 "fast" 等非数值字符串时 float() 抛 ValueError 中断整个音乐注册流程；为 "nan" 时得到 NaN，`applicable_music` 的排序键含 NaN 导致排序结果不确定。mood 可为任意类型却标注 `str | None`。
  - 建议: try/except (TypeError, ValueError) 包裹并降级为 None；`math.isnan` 过滤；mood 做 str() 归一。
  - 置信度: 高（路径确定；实际触发依赖元数据质量，故仅 Suggestion）

### 通过项
- 授权门完整性：is_applicable 严格限定 LICENSED/CLEARED；未授权音乐仍可 propose 但 is_applicable=False，与"提议-only、不伪造已添加音乐"的合同一致。
- BPM/情绪缺失降级：None 语义贯穿（不编造数据），best_matches 排序键对缺失 BPM 显式降权。
- 线程安全：单线程目录 dict，当前无跨线程调用方（未发现外部使用者）；GPU/GLSL/信号维度不适用。
- 类型注解：完整（含排序键内函数 `_score(m: MusicAsset) -> tuple[int, int, float]`）。

---

## src/features/edit/ai_editing/ocr_provider.py (行数: 522)

### 发现

- [AUD-14009] [Major] _model_version 无法区分 stub 与真实推理（两路径返回同一包版本串），切换 TOPOS_OCR_REAL 不触发重新分析，默认 stub 数据被标成真实 rapidocr 版本
  - 位置: src/features/edit/ai_editing/ocr_provider.py:374-390
  - 证据:
    ```python
    try:
        import rapidocr as _r  # type: ignore[import-not-found]
        return f"rapidocr-{getattr(_r, '__version__', 'real')}"
    except Exception:
        try:
            import rapidocr_onnxruntime as _r
            ...
        except Exception:
            return "rapidocr-stub-v1"
    ```
  - 影响: analyze 入口（149-152 行）已保证 find_spec("rapidocr") 通过，故无论 env 开关如何，import rapidocr 总是成功，stub 与真实路径的 provenance model 完全相同。docstring 声称的"足以与 stub 区分，触发指纹变化 → 重新分析"失效：用户装好包后先用默认 stub 得到编造检测（"新品上市"，confidence 0.92），再打开 TOPOS_OCR_REAL=1 时 stale 判定认为模型未变，缓存的假数据继续生效。也未按注释返回过 "rapidocr-real"。
  - 建议: `_model_version` 区分路径——stub 分支返回 `rapidocr-stub-v1`，真实分支返回 `rapidocr-{version}`；或直接依据 env 开关选择标识，并补"切换开关后指纹变化"单测。
  - 置信度: 高

- [AUD-14010] [Major] analyze 只探测 find_spec("rapidocr") 未探测 rapidocr_onnxruntime，与 _get_engine 的旧包回退设计矛盾——仅安装旧包的环境直接被拒
  - 位置: src/features/edit/ai_editing/ocr_provider.py:148-152（对照 288-294）
  - 证据:
    ```python
    if importlib.util.find_spec("rapidocr") is None:
        raise ModelPackNotInstalledError(
            "rapidocr_ocr", detail="python package 'rapidocr' is not installed"
        )
    ```
    而 _get_engine 注释称"``find_spec`` 在 ``analyze`` 入口已确认至少一个可用，故此处不重复探测"，实际实现：
    ```python
    try:
        from rapidocr import RapidOCR as _RapidOCR
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR as _RapidOCR
    ```
  - 影响: 明确支持旧包 rapidocr_onnxruntime 的引擎路径永远走不到——只装旧包的用户 analyze 直接抛 ModelPackNotInstalledError（含 stub 路径也被拒），注释承诺不成立，兼容代码成为死路径。
  - 建议: 入口探测改为 `find_spec("rapidocr") is None and find_spec("rapidocr_onnxruntime") is None` 才抛错，错误信息同步提及两个包名。
  - 置信度: 高

- [AUD-14011] [Suggestion] _select_frames/_total_frames 用宽泛 except Exception 静默吞掉 provider 故障且无任何日志
  - 位置: src/features/edit/ai_editing/ocr_provider.py:189-193, 205-211
  - 证据:
    ```python
    try:
        spans = self._shot_spans_provider(asset_id)
    except Exception:  # provider 故障不应阻断；退化为均匀
        spans = ()
    ```
    以及 `_total_frames` 中 `except Exception: return 0`。
  - 影响: 降级意图本身合理，但连 logger.debug 都没有：shot provider 的领域错误（编码 bug、缓存损坏）被无声掩盖，运维只能看到 provenance 里 frame_selection 变了，违反"不得宽泛 except 掩盖领域错误"铁律的精神。
  - 建议: 保留降级但补 `logger.warning/debug("...", exc_info=True)`；如可预期具体异常类型则收窄。
  - 置信度: 高

- [AUD-14012] [Suggestion] NaN 置信度经 `max(0.0, min(1.0, float(score)))` 变成 1.0（NaN 比较短路），异常分数反获满置信
  - 位置: src/features/edit/ai_editing/ocr_provider.py:473-476
  - 证据:
    ```python
    try:
        confidence = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        confidence = 0.0
    ```
  - 影响: score 为 float('nan')（numpy 侧 NaN 不会抛 TypeError/ValueError）时，`min(1.0, nan)` 因 `nan < 1.0` 为 False 返回 1.0，再 max(0.0, 1.0)=1.0——坏分数被钳成最高置信度，污染下游按 confidence 的过滤/展示。
  - 建议: 先 `s = float(score)`，`if math.isnan(s) or math.isinf(s): confidence = 0.0` 再钳制。
  - 置信度: 高

- [AUD-14013] [Suggestion] 杂项质量：derive_shot_spans 导入未使用；analyze 的 progress 形参被忽略（改用类内 _progress_cb）；_engine_kwargs 内重复 import os
  - 位置: src/features/edit/ai_editing/ocr_provider.py:45-50, 141, 315
  - 证据:
    ```python
    from src.features.edit.ai_editing.keyframe_sampler import (
        FrameSamplerProtocol, ShotSpan, StubFrameSampler, derive_shot_spans,
    )
    ```
    （grep 全文件 derive_shot_spans 仅此一处）；`def analyze(..., progress: Any | None = None)` 函数体内从未引用 progress；`import os` 在模块顶部（31 行）已存在，_engine_kwargs 内重复导入。
  - 影响: 未使用导入与被忽略的协议形参误导调用方（以为传 progress 生效）；冗余导入增加噪音。
  - 建议: 删除未用导入与形参（或让 analyze 把 progress 适配到 _progress_cb）；移除函数内 import os。
  - 置信度: 高

- [AUD-14014] [Suggestion] _engine 惰性初始化无锁，provider 实例被并发 analyze 时可能重复加载模型
  - 位置: src/features/edit/ai_editing/ocr_provider.py:121, 288-304
  - 证据:
    ```python
    self._engine: Any = None
    ...
    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        ...
        self._engine = _RapidOCR(**self._engine_kwargs(pack_path))
    ```
  - 影响: check-then-act 无同步：若同一 OcrProvider 实例被两个分析线程共享（AnalysisService 并发分析多素材），会各自构建 RapidOCR（模型加载昂贵、内存翻倍，先建实例被覆盖后交由 GC）。当前调用方线程模型未证实，故低置信。
  - 建议: `threading.Lock` 双检锁，或要求控制器保证单线程独占并在文档声明。
  - 置信度: 低

### 通过项
- 不可信数据处理红线：OCR 文本仅产出 OcrDetection，未触碰系统指令/字幕；bbox 统一归一化并钳制 [0,1]（OcrBox.__post_init__ 严格校验 0..1，已核实 analysis_models.py:199-203）。
- 取消/异常路径：真实推理逐帧 try/except，engine 异常记 warning 后 continue，None 帧跳过不抛；progress 回调异常仅 debug 不中断。
- 帧数上限：_even_sample 以 _DEFAULT_MAX_SAMPLED_FRAMES=30 封顶，长片不逐帧（§18.1 红线遵守）。
- RapidOCR 版本兼容：_normalize_rapidocr_output 对新旧版输出、None、numpy array 均有归一处理，zip 截断安全。
- 线程安全（除引擎惰性初始化外）：无可变共享状态；进度回调注入设计为可缺失。
- GPU/GLSL/信号防递归：不适用。

---

## src/features/edit/ai_editing/operation_protocol.py (行数: 487)

### 发现

- [AUD-14015] [Suggestion] `_op` 紧凑构造器的 **kw 无类型注解，且必填键缺失时报裸 KeyError
  - 位置: src/features/edit/ai_editing/operation_protocol.py:73-88
  - 证据:
    ```python
    def _op(operation_id: str, domain: str, **kw) -> OperationContract:
        """紧凑构造器：preview_diff 缺省 True；permission 缺省 "write"
        （保守值——写操作必须经宿主 approval 才能生效）。"""
        kw.setdefault("preview_diff", True)
        kw.setdefault("permission", "write")
        return OperationContract(
            operation_id=operation_id, domain=domain,
            permission=kw["permission"], scope=kw["scope"],
    ```
  - 影响: 模块内唯一未注解的函数（`**kw` 应为 `**kw: Any`）；scope/idempotency_key/apply/verify/undo 任一缺失时抛 `KeyError: 'scope'` 而非可读错误，新增条目漏字段时排障成本高（REQUIRED_FIELDS 完整性靠下游测试兜底）。
  - 建议: `**kw: Any`；构造前显式取所需键并在缺失时 raise ValueError 指明 operation_id。
  - 置信度: 高

### 通过项
- 数据完整性：12 必备字段在 OperationContract 全部落位；模块级 _REGISTRY 一次构建后只读（缓存线程安全）。
- 诚实条款：apply/verify 均引用具名真实入口（EditPlan OperationKind、CommandStack、ExportService 等），error_codes 引用 TAEP_*/ERR_* 词汇表；未发现"计划中"伪条目（与 models.py OperationKind 白名单交叉核对一致：edit.* 条目均有对应 kind）。
- 权限保守缺省：permission 缺省 "write"（须宿主 approval）；read/preview/file_write/expensive 分类与各条目语义相符（逐条核对）。
- 撤销路径：每个可写操作均有 undo_or_compensation；纯读操作标注 "n/a (read)"。
- GPU/GLSL/HDR/线程/信号维度不适用（纯静态元数据）。
- 类型注解：除 AUD-14015 外完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 6 | AUD-14000, AUD-14003, AUD-14004, AUD-14007, AUD-14009, AUD-14010 |
| Suggestion | 10 | AUD-14001, AUD-14002, AUD-14005, AUD-14006, AUD-14008, AUD-14011, AUD-14012, AUD-14013, AUD-14014, AUD-14015 |

无 Critical。共性主题：错误处理回退分支自身损坏（AUD-14000）、写入门在缺省输入下 fail-open（AUD-14003/14004）、stub 与真实路径不可区分导致缓存/诚实性问题（AUD-14009）、包探测与兼容层不一致（AUD-14010）、枚举映射死代码（AUD-14007）。
