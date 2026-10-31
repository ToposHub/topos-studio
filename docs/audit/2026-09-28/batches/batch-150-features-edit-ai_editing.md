# batch-150 审查报告 — src/features/edit/ai_editing（视觉语义 / WhisperX / 工作流）

批次：batch-150 [P1]，编号区间 AUD-15000–AUD-15099，6 个文件，2,048 行。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 /
状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

汇总：Critical 0 · Major 5 · Suggestion 12。

本批次为 AI 剪辑编排层（无 GLSL、无直接 OpenGL、无 Qt 信号），GPU/信号防递归/
HDR 维度不适用；重点落在异常路径、外部数据验证、状态一致性与资源/内存模式。
跨模块佐证引用：`analysis_scheduler.py`（ThreadPoolExecutor，`analysis_local_concurrency=2`）、
`analysis_service.py`（progress 映射与 drain_embeddings）、`artifact_store.py`（save 的
用户优先红线）、`timebase.py`（seconds_to_frame_range 校验）、`keyframe_sampler.py`
（每 shot 最多 3 帧 head/mid/tail）。

---

## src/features/edit/ai_editing/visual_semantic_provider.py (行数: 437)

### 发现

- [AUD-15000] [Major] 装配阶段宽泛 `except Exception` 把真实错误统一伪装成“缺模型包”
  - 位置: src/features/edit/ai_editing/visual_semantic_provider.py:367-386
  - 证据:
    ```python
    try:
        model_pack_manager.require_usable("qwen3_vl_8b_awq")
        from apps.topos_vault.providers.real_visual_caption import Qwen3VLCaptioner
        captioner = Qwen3VLCaptioner()
        if captioner.is_available():
            backends.append(VaultCaptionBackend(captioner))
        else:
            missing.append("qwen3_vl_8b_awq runtime dependency")
    except Exception:
        missing.append("qwen3_vl_8b_awq verified model pack")
    ```
    （siglip2 分支 377-386 同构）
  - 影响: `Qwen3VLCaptioner()` / `SigLIP2EmbeddingProvider()` 构造期或 `require_usable`
    中的真实异常（依赖冲突、显存不足、torch 版本不匹配、代码 bug）与“模型包未通过
    SHA 校验”归为同一句提示，且原始 traceback 被完全丢弃（无 logger 记录）。用户按
    “去装模型包”排查会被误导；违反 .clinerules “不得宽泛 except 掩盖领域错误”。
  - 建议: 捕获具体异常类型（require_usable 的签名失败类型 + ImportError + 构造期已知
    异常），`logger.exception` 保留原因，并把异常摘要拼进 unavailable_reason。
  - 置信度: 高

- [AUD-15001] [Major] `analyze` 在推理前一次性物化全部关键帧解码图像，长素材内存峰值失控
  - 位置: src/features/edit/ai_editing/visual_semantic_provider.py:196-222（配合
    visual_semantics.py:85 `image=sampler.sample(entry.frame)`）
  - 证据:
    ```python
    keyframes = keyframes_from_manifest(manifest, shot_spans, sampler)
    ...
    observed: list[tuple[VisualKeyframe, tuple[VisualObservation, ...]]] = []
    for keyframe in keyframes:
        keyframe_observations: list[VisualObservation] = []
        for backend in providers:
            result = tuple(backend.infer(keyframe) or ())
    ```
    `keyframes_from_manifest` 对 manifest 每个 entry 都附着整帧 ndarray；每个 shot 最多
    3 帧（keyframe_sampler.py:80-104 head/mid/tail）。
  - 影响: 4K RGB 单帧约 24MB；500 个镜头的长片 ≈ 1500 帧 ≈ 36GB 常驻内存，推理循环
    每次只用 1 帧却全部持有。本地分析 lane 默认并发 2（analysis_scheduler.py:78）会再
    翻倍，直接 OOM / 被系统杀进程风险（崩溃级后果，但触发依赖素材规模，故定 Major）。
  - 建议: 流式化——按 keyframe 迭代时再 `sampler.sample(frame)` 并在单帧所有 backend
    推理完成后释放引用；或 `keyframes_from_manifest` 只建 identity，图像延迟解码。
  - 置信度: 中（代码事实确定；是否实际 OOM 取决于镜头数与素材时长）

- [AUD-15002] [Suggestion] `_keyframe_image_file.__exit__` 只容忍 FileNotFoundError，清理异常会顶替原始推理异常
  - 位置: src/features/edit/ai_editing/visual_semantic_provider.py:425-430
  - 证据:
    ```python
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._path:
            try:
                os.remove(self._path)
            except FileNotFoundError:
                pass
    ```
  - 影响: 若 `os.remove` 抛 PermissionError/OSError（Windows 句柄占用、杀软扫描），
    该异常会替换 `with` 体内 provider 的真实推理异常，污染失败归因。
  - 建议: 捕获 `OSError` 并 `logger.debug` 记录。
  - 置信度: 高

- [AUD-15003] [Suggestion] Caption/Tag/Embedding 后端硬编码置信度，模型分数被丢弃
  - 位置: src/features/edit/ai_editing/visual_semantic_provider.py:98-110, 131-136
  - 证据:
    ```python
    output.append(VisualObservation(
        asset_id=keyframe.asset_id, shot_index=keyframe.shot_index,
        frame=keyframe.frame, kind="caption", value=caption,
        confidence=0.70, field="caption", provenance=provenance,
    ))
    ```
  - 影响: 下游 `aggregate_shot_observations` 的 max/mean 置信度聚合（visual_semantics.py:233-234）
    在常量上运算，聚合置信度与帧数退化为“多帧必然更高置信”，证据排序失真。
  - 建议: 后端能返回分数则透传；不能则在 provenance 标注 `confidence: "fixed"`，避免
    聚合层把常量当真实分布。
  - 置信度: 高

- [AUD-15004] [Suggestion] 推理循环对单帧解码失败 / 单 backend 异常无隔离，一处失败丢弃整次分析
  - 位置: src/features/edit/ai_editing/visual_semantic_provider.py:210-222（配合 :72-74
    `sample` 失败返回 None → `_keyframe_image_file.__enter__` 抛 ValueError）
  - 证据:
    ```python
    for keyframe in keyframes:
        keyframe_observations: list[VisualObservation] = []
        for backend in providers:
            result = tuple(backend.infer(keyframe) or ())
    ```
  - 影响: 一帧解码失败（`image=None`）或 captioner 对某一帧抛错，会让已完成的其它
    shot 的 caption/embedding 全部作废，整次 analyze 失败。模块“不做静默 fallback”的
    红线针对“伪造证据”，但“跳过坏帧并在 provenance/conflicts 里如实记录”并不伪造。
  - 建议: 对单帧/单 backend 失败收集为 conflict 诊断（带帧号与异常摘要），其余照常
    产出；整体仍失败时错误信息含帧号。
  - 置信度: 中

### 通过项
- 线程安全：无可变共享状态竞态面（`_pending_embeddings` 按 asset_id 分键，analyze 与
  drain 在 analysis_service 同一工作流内成对调用，analysis_service.py:787-795）
- GPU 资源生命周期：本模块不触碰 OpenGL/ModernGL；sampler 在 finally 中关闭（:199-202），
  临时 PNG 经 context manager 清理
- 输入验证：total_frames/shot_spans/keyframes 空值均显式 ValueError（:188-204）；
  `_normalize_observation` 校验 backend 观测与 keyframe 身份一致（:277-280）
- 状态提交一致性：embedding 只进可重建缓存不进工程 payload；fingerprint 随缓存写入（:232-239）
- 异常路径：无可用后端时抛可行动的 `VisualSemanticUnavailableError`，不用 demo 伪造（:180-186）
- 类型注解：完整（含 `from __future__ import annotations`）

---

## src/features/edit/ai_editing/visual_semantics.py (行数: 420)

### 发现

- [AUD-15005] [Suggestion] `_safe_id` 清洗后不同 asset_id 可碰撞，`put` 互相覆盖缓存文件
  - 位置: src/features/edit/ai_editing/visual_semantics.py:358, 408-410
  - 证据:
    ```python
    def _safe_id(value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)
        return safe[:120] or "asset"
    ```
    `put` 直接写 `self._root / f"{safe}.json"`（:358）。
  - 影响: `"a/b"` 与 `"a_b"` 清洗后同名；后写入者覆盖前者缓存文件。`load` 有
    asset_id 回查（:377-378）故不会读到错数据，但表现为缓存互相挤掉、反复重算。
  - 建议: 文件名纳入原始 asset_id 的短 hash（如 `safe-{sha256(asset_id)[:8]}.json`）。
  - 置信度: 高

- [AUD-15006] [Suggestion] `VisualCacheStore.load` 不校验 `schema_version` 与 fingerprint
  - 位置: src/features/edit/ai_editing/visual_semantics.py:371-379
  - 证据:
    ```python
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("asset_id") != asset_id:
        return None
    return value
    ```
  - 影响: `put` 写入了 `schema_version: 1` 与 fingerprint（:333-336），`load` 却不校验；
    未来 schema 变更或残留旧文件会把不兼容结构原样交给调用方，stale 判断完全依赖
    调用方自觉。
  - 建议: load 时校验 `schema_version == 1`，并提供按 fingerprint 比对的辅助方法。
  - 置信度: 中（当前调用方尚未消费 load 结果，属防线缺失而非现行缺陷）

### 通过项
- `put` 原子写：mkstemp + fsync + `os.replace`，失败路径清理临时文件（:359-368）
- 线程安全：纯函数/不可变 dataclass，无共享可变状态
- 输入验证：`VisualKeyframe.__post_init__` 校验 asset_id/帧号/帧在 shot 内（:48-54）；
  `VisualObservation.__post_init__` 校验 kind/confidence/value/embedding（:104-114）；
  `keyframes_from_manifest` 校验 manifest 帧落在 shot 区间（:76-79）
- 状态一致性：embedding 观测不进文本证据聚合（:224-225），caption 冲突保留为 conflict
  而非静默覆盖（:256-263）；evidence_id/digest 稳定可重放
- 信任边界：`visual_context` 显式包装 untrusted（:394-405），demo provider 均 `is_demo=True`
- GLSL·HDR / 信号防递归 / GPU 生命周期：不适用（纯数据契约层）
- 类型注解：完整

---

## src/features/edit/ai_editing/volcengine_generation_provider.py (行数: 12)

### 通过项
- 通过（无逻辑）：兼容 shim，`sys.modules[__name__] = _impl` 把本模块条目替换为
  `src.shared.ai_models.generation.volcengine_generation_provider`，历史导入路径
  （含私有名）不变。无执行逻辑、无状态，九个维度均无需审查。

---

## src/features/edit/ai_editing/whisperx_provider.py (行数: 197)

### 发现

- [AUD-15007] [Major] word 级时间戳未做外部数据校验，一条畸形 word 使整次转录失败
  - 位置: src/features/edit/ai_editing/whisperx_provider.py:136-150
  - 证据:
    ```python
    for word in item.get("words", ()) or ():
        word_text = str(word.get("word", word.get("text", ""))).strip()
        if not word_text or word.get("start") is None or word.get("end") is None:
            continue
        word_range = seconds_to_frame_range(
            float(word["start"]), float(word["end"]), timeline_fps
        )
    ```
    段级有防护（:132-134 `end = float(item.get("end", start) or start)` +
    `max(start, end)`），word 级没有。
  - 影响: `seconds_to_frame_range` 对 `end_seconds < start_seconds` 或负值直接抛
    ValueError（timebase.py:49-52）。WhisperX 对齐器输出属外部数据，边界情况下可出现
    end<=start 的 word；单条坏 word 让 `_convert_segments` 抛错 → `analyze` 失败 →
    丢弃整段已完成的转录结果。违反 .clinerules “外部数据须验证”。
  - 建议: word 同样使用 `max(start, end)` 并跳过负值；或逐 word 捕获 ValueError 记
    warning 后继续。
  - 置信度: 高（缺防护是确定的；触发频率取决于对齐器输出质量）

- [AUD-15008] [Major] 共享 provider 实例下 `_engine` 懒加载无锁，且并发分析共用同一模型对象
  - 位置: src/features/edit/ai_editing/whisperx_provider.py:88-95, 112-122
  - 证据:
    ```python
    def _get_engine(self, whisperx: Any) -> Any:
        if self._engine is not None:
            return self._engine
        compute_type = "float32" if self._device == "cpu" else "float16"
        self._engine = whisperx.load_model(
            self._model_name,
            self._device,
            compute_type=compute_type,
            language=None,
        )
        return self._engine
    ```
  - 影响: 该 provider 以单实例注册进共享 AnalysisService
    （ai_editing_mixin.py:958-961），调度器异步模式用 ThreadPoolExecutor 且
    `analysis_local_concurrency=2`（analysis_scheduler.py:78,138-145）。两个本地分析
    任务同时落到 WhisperX 时：check-then-act 竞态导致 `load_model`（large-v3 数 GB）
    被加载两次；更严重的是两线程并发在同一 faster-whisper/ctranslate2 模型对象上
    `transcribe`，该栈不保证线程安全，有崩溃/输出损坏风险。
  - 建议: `threading.Lock` 保护懒加载；并为本 provider 声明/实现串行化（如内部锁包住
    transcribe，或调度 lane 对其限 1）。
  - 置信度: 中（并发实际到达 WhisperX 依赖回退路由命中，但默认并发=2 使其可能）

- [AUD-15009] [Suggestion] `progress` 参数被接受但从未上报，长转录期间任务进度停滞
  - 位置: src/features/edit/ai_editing/whisperx_provider.py:75-95
  - 证据:
    ```python
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        real_enabled = _real_inference_enabled("TOPOS_WHISPERX_REAL")
        ...
        audio = whisperx.load_audio(wav_path)
        model = self._get_engine(whisperx)
        result = model.transcribe(audio, batch_size=4, language=None)
    ```
    全函数无一处 `progress(...)` 调用。
  - 影响: large-v3 在 CPU 上转录数分钟视频耗时数分钟；调度层把 provider 分数映射为
    总进度（analysis_service.py:769-779），此 provider 占用的进度区间全程停在 0，UI
    表现为“卡死”。
  - 建议: 在 load_model / transcribe 前后至少上报 0.0 与粗粒度阶段进度；有 chunk 回调
    时按音频时长折算。
  - 置信度: 高

- [AUD-15010] [Suggestion] 长转录无协作取消检查点，取消后仍占满 CPU 直至转录结束
  - 位置: src/features/edit/ai_editing/whisperx_provider.py:87-95
  - 证据:
    ```python
        try:
            import whisperx  # type: ignore[import-not-found]

            audio = whisperx.load_audio(wav_path)
            model = self._get_engine(whisperx)
            result = model.transcribe(audio, batch_size=4, language=None)
            segments = self._convert_segments(result, fps)
        finally:
            self._cleanup_temp_audio(wav_path)
    ```
  - 影响: `transcribe` 是单次阻塞调用，中途无法响应调度器取消；取消仅丢弃结果，工作
    线程继续占用 CPU（与本地分析并发=2 的预算互相挤压）。
  - 建议: 按音频分段循环转录（whisperx 支持分段 chunk），每段检查取消回调并提前返回。
  - 置信度: 中

### 通过项
- 资源清理：临时 wav 在 finally 中删除（:94-95, 188-194），清理失败仅 debug 日志
- 输入验证（段级）：空文本跳过、`max(start, end)` 防倒置、confidence 钳制 [0,1]
  （:129-157）；`extract_wav` 对空/不存在路径有防御（audio_extractor.py:83-84）
- 真实推理门控：stub 默认关闭（`allow_stub=False` + `_real_inference_enabled` 默认真实，
  transcript_provider.py:75-87），未安装/未校验抛明确 RuntimeError（:79-84）
- 状态一致性：走统一 `AssetAnalysis`/`analysis_to_segments` 合同，不改写工程状态
- GLSL·HDR / 信号防递归 / GPU 生命周期：不适用
- 类型注解：完整

---

## src/features/edit/ai_editing/workflow.py (行数: 696)

### 发现

- [AUD-15011] [Major] 字幕/多语言 artifact 保存未处理 `ArtifactOverrideError`，用户编辑后重跑工作流以异常而非降级结果终止
  - 位置: src/features/edit/ai_editing/workflow.py:524（`_run_subtitle`）、577（`_run_multilingual`）
  - 证据:
    ```python
    draft = build_subtitle_draft(segments)
    artifact = self._subtitle_draft_to_artifact(draft, inp.brief_id, primary_asset)
    saved = self._artifact_store.save(artifact)
    ```
    artifact_id 为确定性拼接：`f"subtitle-draft-{primary_asset}-{brief_id}"`（:667）、
    `f"multilingual-{inp.brief_id}-{primary_asset}"`（:571）。
  - 影响: `ArtifactStore.save` 在“已有 user_edited=True 且新 source 是 SKILL”时抛
    `ArtifactOverrideError`（artifact_store.py:92-100）。同一素材+brief 重跑字幕初稿
    （用户改过草稿后）必然命中：异常穿透 `run()`——违背本模块“失败以
    WorkflowResult.degradation 表达”的契约；`assistant_service.py:705` 的
    `runner.run(...)` 无 try 包裹，靠更外层 legacy adapter 兜底成通用
    WORKFLOW_EXCEPTION，用户丢失“草稿已被你编辑、模型不得覆盖”这一可行动信息。
  - 建议: 捕获 `ArtifactOverrideError`，返回带 degradation 说明的 WorkflowResult
    （并可附 diagnostic），或为新草稿生成新 artifact_id。
  - 置信度: 高

- [AUD-15012] [Suggestion] 字幕 artifact 硬编码 `revision=0` / `brief_revision=0` / `created_at=""`，与多语言路径不一致
  - 位置: src/features/edit/ai_editing/workflow.py:666-677
  - 证据:
    ```python
    return ScriptArtifact(
        artifact_id=f"subtitle-draft-{primary_asset}-{brief_id}",
        kind=ArtifactKind.SCRIPT,
        revision=0,
        ...
        created_at="",
        brief_revision=0,
    )
    ```
    对比 `_run_multilingual` 使用 `brief_revision=self._brief_revision(inp.brief_id)`（:575）。
  - 影响: 重跑时 `save` 以 revision=0 覆盖旧对象（非 user_edited 场景），修订号回退、
    brief 修订溯源丢失、时间戳为空，审计与 artifact 乐观锁语义失真。
  - 建议: `brief_revision` 走 `_brief_revision`；`revision`/`created_at` 由 store 或
    时钟填充；重跑时 bump 而非归零。
  - 置信度: 高

- [AUD-15013] [Suggestion] `_brief_revision` 对属性式 `current_revision` 静默返回 0，且 `except Exception` 吞掉 brief 服务错误
  - 位置: src/features/edit/ai_editing/workflow.py:638-647
  - 证据:
    ```python
    rev = getattr(self._brief, "current_revision", None)
    if callable(rev):
        try:
            return rev(brief_id) or 0
        except Exception:
            return 0
    return 0
    ```
  - 影响: 若注入的 brief_service 把 `current_revision` 实现为 int 属性（非方法），
    恒返回 0 且无任何告警；方法抛错（存储故障）也被静默为 0，artifact 锚定到错误
    brief 修订。
  - 建议: 属性值直接采用；异常时记 warning 并在 diagnostics 中可见。
  - 置信度: 中

- [AUD-15014] [Suggestion] `validate_input` 不校验 `base_revision` 与 `brief_id`
  - 位置: src/features/edit/ai_editing/workflow.py:378-403
  - 证据:
    ```python
    if not inp.timeline_id or not inp.project_id:
        ...
    if inp.target_duration <= 0:
        ...
    return diags
    ```
  - 影响: `base_revision` 是事务锚点（会传入 PlannerInput，:481），负值或未设置可通过
    校验直达 planner/plan 仓库；`brief_id` 为空串也会生成
    `subtitle-draft-{asset}-` 这类退化 artifact_id。
  - 建议: 增加 `base_revision >= 0`、`brief_id` 非空（产 artifact 的工作流）检查。
  - 置信度: 中

- [AUD-15015] [Suggestion] `_run_variant_workflow` 中 planner 异常未转为降级结果，与 `_run_multilingual` 的处理不一致
  - 位置: src/features/edit/ai_editing/workflow.py:485-490（对比 :556-563）
  - 证据:
    ```python
    variants = self._planner.plan(
        planner_inp, index,
        target_beats=spec.target_beats,
        strategies=spec.strategies,
        on_plan_compiled=self._on_plan_compiled or _noop_plan_compiled,
    )
    ```
  - 影响: `_run_multilingual` 把 provider 异常转为可见 degradation（:558-563），variant
    工作流对 planner 的异常却直接放行穿透 `run()`；同类失败两种表达，宿主若按
    “run() 只返回 WorkflowResult”假设编码会漏接。
  - 建议: 统一策略——要么文档化 run() 可抛异常，要么 planner 异常也包装为
    diagnostics + degradation。
  - 置信度: 高（不一致是事实；是否算缺陷取决于宿主契约，故 Suggestion）

### 通过项
- 状态提交一致性（设计层）：variant 的 EditPlan 只经 `on_plan_compiled` 回调进
  PlanRepository，复用唯一 approval/commit/undo 闭环（:485-490）；证据强制引用校验
  `_has_low_confidence_decisions` 缺引用即标降级（:491-497, 628-634）
- 降级红线：缺证据走 `degradation` + 空 variants，不伪造方案（:433-444）；多语言无
  provider/无目标语言均显式降级，不复制原文冒充（:536-553）
- 外部数据验证：translation_provider 返回逐条 `isinstance(line, ScriptLine)` 校验
  （:564-569）；字幕只取 TRANSCRIPT kind，OCR 等 untrusted 不进（:615-623）
- 输入校验：workflow_id/asset_ids/timeline/project/target_duration 均有诊断
  （:378-403）；排序确定化（:624-625）
- 线程安全：runner 无可变实例状态（构造后只读），可重入
- emit_stage 诚实映射：只在实际阶段边界触发，validation_failed/evidence_missing/
  degraded 均如实（:417-464）
- 类型注解：完整；GLSL·HDR / GPU 生命周期 / 信号防递归：不适用

---

## src/features/edit/ai_editing/workflow_transaction.py (行数: 286)

### 发现

- [AUD-15016] [Suggestion] `add_step` 对 inputs/outputs 只做浅拷贝，嵌套结构事后可变导致审计记录失真
  - 位置: src/features/edit/ai_editing/workflow_transaction.py:201-207
  - 证据:
    ```python
    step = WorkflowStep(
        index=len(self._steps), name=name,
        operation_id=operation_id,
        inputs=dict(inputs or {}), outputs=dict(outputs or {}),
        undo_token=undo_token, job_ref=job_ref,
    )
    ```
    对比锚点的深拷贝防线：`"capability_snapshot": copy.deepcopy(capability_snapshot or {})`
    （:117），注释明言“调用方事后的嵌套修改不得穿透（审计回放要求锚点逐字节可信）”。
  - 影响: 步骤历史与锚点同属审计回放数据，却共享调用方嵌套 dict/list 引用；调用方
    之后继续更新传入的结构（常见：把同一 config dict 逐步改写后多次 add_step）会
    改写已记录的历史，replay/补偿建议（undo tokens 之外的输入输出）失真。
  - 建议: 与锚点对齐使用 `copy.deepcopy`（或冻结为不可变结构）。
  - 置信度: 高（行为差异确定；是否被利用取决于调用方，故 Suggestion）

### 通过项
- 状态机正确性：终态无出边、越边/终态再转移抛 IllegalTransitionError（:59-77, 162-170）；
  progress 钳制 [0,1]（:168-169）；模块级断言保证终态不可取消（:87）
- 取消路径：cancel 仅在 CANCELLABLE_STATES 允许（:182-187），事件携带
  cancellable_after 与补偿建议（:170-177）
- 步骤记录契约：仅 RUNNING 可记录步骤（:196-200）
- 失败策略：FailurePolicy 封闭集 + 显式补偿函数，rollback 建议按记录顺序逆序回滚
  undo tokens（:211-224）
- 锚点不可变：anchor 属性返回深拷贝（:130-132）
- 线程安全：单事务单属主使用模型，无共享可变全局；类型注解完整
- GLSL·HDR / GPU 生命周期 / 信号防递归：不适用

---

## 附：佐证文件（只读引用，未修改）

- src/features/edit/ai_editing/analysis_scheduler.py:78, 138-145（本地分析并发 2 + ThreadPoolExecutor）
- src/features/edit/ai_editing/analysis_service.py:760-800（progress 映射、ingest、drain_embeddings）
- src/features/edit/ai_editing/artifact_store.py:81-102（save 的 ArtifactOverrideError 红线）
- src/features/edit/ai_editing/timebase.py:36-65（seconds_to_frame_range 对 end<start/负值抛 ValueError）
- src/features/edit/ai_editing/keyframe_sampler.py:80-104, 237-262（每 shot head/mid/tail 至多 3 帧）
- src/app/controllers/ai_editing_mixin.py:950-1030（WhisperX/Visual provider 单实例注册）
