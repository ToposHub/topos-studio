# batch-149 审查报告 — src/features/edit/ai_editing/

审查日期：2026-09-28。编号区间：AUD-14900–AUD-14999。
全部 6 个文件已完整通读（长文件分段读完），关键疑点已交叉验证调用方
（`edit_controller.py`、`ai_editing_mixin.py`、`analysis_scheduler.py`、`semantic_index.py`、`editorial_models.py`、`brief_service.py`、`audio_extractor.py`）。
本批无 GLSL/HDR 渲染代码、无 Qt 信号代码（provider 层纯逻辑），相关维度标 N/A。

统计：Critical 0 / Major 6 / Suggestion 17。

---

## src/features/edit/ai_editing/transcript_provider.py (行数: 1,446)

### 发现

- [AUD-14900] [Major] analyze() 调用 merge_diarization 未传 media_fps_ratio，帧坐标与 16kHz sample 坐标直接比较，speaker 归属必然错误
  - 位置: src/features/edit/ai_editing/transcript_provider.py:446（配合 1116-1148）
  - 证据:
    ```python
    if diarization:
        segments = merge_diarization(segments, diarization)   # L446：未传 media_fps_ratio / timebase_service
    ```
    ```python
    # L1116-1122（merge_diarization 内部）
    for d in diarization:
        if media_fps_ratio is not None:
            ds = tb.from_audio_sample(d.sample_start, d.sample_rate, media_fps_ratio)
            de = tb.from_audio_sample(d.sample_end, d.sample_rate, media_fps_ratio)
        else:
            ds = d.sample_start      # ← 裸 sample offset（16kHz）
            de = d.sample_end
    # L1139
    overlap = max(0, min(seg.end_frame, de) - max(seg.start_frame, ds))
    ```
  - 影响: 调用方（本文件 analyze，唯一生产调用点，grep 全库确认无其他 caller）传入的 `segments` 已经 `from_audio_sample` 换算成帧坐标，而 diarization 段保持 sample 坐标（约为帧数值的 667 倍 @16kHz/24fps）。典型结果：短视频（<~5 分钟@24fps）所有 overlap 计算为 0 → speaker 全 None；长视频几乎全部段落都落在第一个 diar 段的 sample 区间内 → 所有说话人都标成 S1。说话人分离功能在生产路径整体失效或产出错误归属，且进度条仍报告"说话人分析完成"。`merge_diarization` 的 `media_fps_ratio`/`timebase_service` 参数（L1085-1086）与 stub 数据都表明本意是传入 fps 换算。
  - 建议: 在 analyze() 中把 `_convert_segments` 已算出的 `media_fps = Rational.from_string(fps)` 连同 `self._timebase` 一起传给 `merge_diarization(segments, diarization, media_fps_ratio=media_fps, timebase_service=self._timebase)`；并为 merge 结果补一条回归测试（帧 vs sample 量纲断言）。
  - 置信度: 高

- [AUD-14901] [Major] 惰性引擎缓存（_asr_engine/_aligner/_diar_model 等）无锁，AnalysisScheduler 的 ThreadPoolExecutor 并发调用 analyze 时存在竞态
  - 位置: src/features/edit/ai_editing/transcript_provider.py:318-325, 702-765, 767-798, 800-831
  - 证据:
    ```python
    # L318-323（实例级缓存，无锁）
    self._asr_engine: Any = None
    self._asr_engine_model_id: str | None = None
    self._asr_engine_device: str | None = None
    self._asr_force_cpu = False
    self._aligner: Any = None
    self._diar_model: Any = None
    ```
    ```python
    # L719-726（check-then-act，非原子）
    if self._asr_engine is not None and self._asr_engine_model_id == model_id:
        return self._asr_engine
    if self._asr_engine is not None:
        # The settings dialog can switch between 0.6B and 1.7B...
        self._asr_engine = None
        self._asr_engine_device = None
        self._aligner = None
    ```
  - 影响: provider 在 ai_editing_mixin.py:947 以单实例注册进 AnalysisService，而 analysis_scheduler.py:139 用 `ThreadPoolExecutor` 执行分析任务（多 asset 并发分析是常规路径）。两线程同时首次调用会各自加载多 GB 模型（内存峰值翻倍，受限机器可能 OOM 崩溃）；一个线程切换 model_size 或 `_force_cpu_asr_engine()`（L530-536）置空引擎时，另一线程可能拿到与 `_asr_engine_device` 不一致的状态，MPS 乱码重试逻辑（`_is_mps_engine`）判断失真。模块级 monkeypatch 标志 `_rope_scaling_patched`/`_processor_compat_patched`（L1343-1344）同样无锁，理论上可被双重 patch。
  - 建议: 为引擎惰性初始化加 `threading.Lock`（双检锁）；`_force_cpu_asr_engine` 与引擎字段更新放在同一临界区；monkeypatch 注册用 `threading.Lock` 或 ` functools.lru_cache(maxsize=1)` 风格的原子哨兵。
  - 置信度: 中（并发调度确认为 ThreadPoolExecutor；实际并发度取决于运行期任务提交频率，但代码本身无任何防护）

- [AUD-14902] [Major] _run_diarization 只捕获导入类异常，Sortformer 运行期错误会击溃整次已成功的转录
  - 位置: src/features/edit/ai_editing/transcript_provider.py:666-674
  - 证据:
    ```python
    try:
        return self._run_sortformer_real(asset_id)
    except (ImportError, ModuleNotFoundError, ModelPackNotInstalledError) as exc:
        # Sortformer is an optional enrichment. ... transcript + alignment
        # must remain usable without it.
        logger.warning("Sortformer unavailable; continuing without speaker labels: %s", exc)
        _emit_progress(progress, 0.90, f"说话人分析不可用，已跳过：{exc}")
        return []
    ```
  - 影响: 注释明确"diarization 是可选增强，transcript+alignment 必须在没有它时仍可用"，但 `model.diarize()`（L686）抛出的任意运行期异常（显存不足、tensor 形状、音频解码失败等 RuntimeError）不在捕获列表内，会直接穿透 `analyze()`，丢弃已经完成的 ASR + 词级对齐全部结果——可选步骤的失败升级为整次分析失败。
  - 建议: 对 `_run_sortformer_real` 捕获 `Exception`（或至少加 `RuntimeError`），记录 warning + `_emit_progress` 后返回 []，与该函数声明的降级语义一致；真实错误经 logger.exception 保留可观测性，不违反"宽泛 except 掩盖领域错误"（此处领域错误即"分离失败"，降级是文档化行为）。
  - 置信度: 中（代码路径确定；触发依赖 Sortformer 运行期失败，属真实可发生场景）

- [AUD-14903] [Suggestion] set_progress_callback 注入的 _progress_cb 从未被调用，是死注入点
  - 位置: src/features/edit/ai_editing/transcript_provider.py:324-325, 1056-1058
  - 证据:
    ```python
    # L324-325
    # 进度回调注入点（签名 (current, total)）。由控制器注入驱动 UI 进度条。
    self._progress_cb: Callable[[int, int], Any] | None = None
    # L1056-1058
    def set_progress_callback(self, cb: Callable[[int, int], Any] | None) -> None:
        """注入进度回调（签名 ``(current, total)``）。"""
        self._progress_cb = cb
    ```
  - 影响: `_progress_cb` 在全文件中只有赋值、无任何读取/调用（进度实际只经 `analyze(progress=...)` 参数流动）。控制器若按注释注入回调会静默无任何效果，注释具有误导性。
  - 建议: 删除 `_progress_cb` 与 `set_progress_callback`，或在 `_emit_progress` 中桥接调用；同步修正注释。
  - 置信度: 高

- [AUD-14904] [Suggestion] analyze() 写入的 thread_local.request_options 在调用结束后不清理，线程池线程复用会残留上一次请求的选项
  - 位置: src/features/edit/ai_editing/transcript_provider.py:406-407
  - 证据:
    ```python
    request_options = dict(options or {})
    self._thread_local.request_options = request_options   # 无 finally 清理
    ```
  - 影响: 与 `validate_options`（L364-387）和 `analyze_with_preprocessor`（L343-357）都做了 save/restore 不同，`analyze()` 设置后不恢复/删除。当前所有读取路径都经 `analyze()` 入口重写所以未出错，但线程池线程被复用，残留值让"helper 直接读取时的语义"依赖隐式时序，未来新增不经 analyze 的入口（如直接调 `_get_asr_engine` 的预热代码）会读到陈旧 options。
  - 建议: 在 analyze() 用 try/finally 恢复（与 validate_options 同款的 sentinel 模式）。
  - 置信度: 高

- [AUD-14905] [Suggestion] _audio_sample_count 宽泛 except 后静默假设 60 秒，产生无标注的错误时长
  - 位置: src/features/edit/ai_editing/transcript_provider.py:907-914
  - 证据:
    ```python
    try:
        import wave
        with wave.open(wav_path, "rb") as wf:
            return wf.getnframes()
    except Exception:
        return self._sample_rate * 60  # 回退：假设 60 秒
    ```
  - 影响: 无对齐路径（L598-601）用该值作为 segment 的 end_sample；读取失败时（截断 wav、格式异常）时长被静默替换为固定 60 秒——字幕 end_frame 严重偏离真实音频长度，且 provenance.extra 中没有任何降级标注，违背模块自述的"可降级，必标注"原则。
  - 建议: 失败时至少把 `"duration_approx": "wave-read-failed-60s"` 写入 extra（经 thread_local 或返回值传递），或改为抛出明确异常由上层标注；捕获范围收窄到 `wave.Error`/`OSError`。
  - 置信度: 中（触发概率低——文件由 ffmpeg 刚生成；但一旦触发即为静默错误数据）

- [AUD-14906] [Suggestion] 词级时间戳置信度硬编码 0.95，虚增下游 router 的质量评分
  - 位置: src/features/edit/ai_editing/transcript_provider.py:1219, 1247
  - 证据:
    ```python
    out.append((ws, we, word.strip(), 0.95))     # L1219（_parse_qwen_time_stamps）
    out.append((s, e, str(word).strip(), 0.95))  # L1247（_parse_qwen_align_results）
    ```
  - 影响: Qwen 对齐结果本无置信度，这里以固定 0.95 填充并经 `sum(w[3])/len(words)`（L595）成为 segment confidence；transcript_router 的 `_quality` 依赖 confidence 决定是否触发 Demucs/WhisperX 回退——合成的 0.95 会让低质量转录被误判为高质量，绕过回退链。
  - 建议: 用对齐成功的先验值但在 provenance 标注 `word_confidence="synthetic"`，或取 ASR segment 级 confidence 下调作为词级值，避免精确的假数字。
  - 置信度: 中

- [AUD-14907] [Suggestion] _normalize_language 对未识别语言名截取前两个字符，产出错误 ISO 码
  - 位置: src/features/edit/ai_editing/transcript_provider.py:1173
  - 证据:
    ```python
    return mapping.get(lang_lower, lang_lower[:2] if lang_lower else "unknown")
    ```
  - 影响: "mongolian"→"mo"（实际 mn，mo 已废弃/巴马多夫）等；未收录语言一律产出貌似合法实则错误的两位码，下游按语言过滤/匹配时产生隐性偏差，比原样保留全名或 "unknown" 更糟。
  - 建议: 未收录时返回 "unknown" 或保留原字符串，由下游显式处理；如需截断应查 ISO 639-1 表。
  - 置信度: 高

- [AUD-14908] [Suggestion] merge_diarization 的 sample_rate 参数与两个解析函数的 language 参数是死参数
  - 位置: src/features/edit/ai_editing/transcript_provider.py:1084, 1177, 1225
  - 证据:
    ```python
    def merge_diarization(
        transcript, diarization, *,
        sample_rate: int = ASR_SAMPLE_RATE,   # L1084：函数体内从未引用（用的是 d.sample_rate）
        ...
    def _parse_qwen_time_stamps(time_stamps, sample_rate, language):   # L1177：language 未用
    def _parse_qwen_align_results(align_items, sample_rate, language): # L1225：language 未用
    ```
  - 影响: 误导调用者以为传 sample_rate 能覆盖换算基准（实际无效）；无用参数增加维护噪音。
  - 建议: 删除死参数或让其生效；`_parse_*` 的 language 移除。
  - 置信度: 高

### 通过项
- 线程安全：thread-local 的 save/restore（validate_options / analyze_with_preprocessor）实现正确（引擎缓存竞态另见 AUD-14901）
- GPU 资源生命周期：不涉及 ModernGL/OpenGL；临时音频文件在 finally 中清理（L612-615, 697-698），AudioExtractor 用 tempfile.mkstemp 落在系统临时目录，`_cleanup_temp_audio` 的 startswith(tempdir) 判定成立
- GLSL·HDR 正确性：N/A（无渲染代码）
- 信号防递归：N/A（无 Qt 控件）
- 状态提交一致性：provider 只产 proposal/analysis，不触碰 undo/revision；provenance 含 model 版本与降级标注（alignment/preprocessor/asr_fallback）
- 异常与取消路径：模型缺失显式抛 ModelPackNotInstalledError 不静默（L410-413, 985-998）；MPS 乱码检测 + CPU 重试路径完整（L489-517）
- 输入验证：options 经 `_request_option` 白名单化（language 白名单 L878-880，max_new_tokens 夹紧 L867-876）；`_qwen_runtime_checkpoint_issue` 对本地 config/权重索引做完整校验
- 性能模式：引擎惰性缓存 + 模型切换时正确弃用旧引擎（L719-726）；`is_pack_usable` 降级不阻塞主路径
- 类型注解：公开方法均有完整注解

---

## src/features/edit/ai_editing/transcript_router.py (行数: 302)

### 发现

- [AUD-14910] [Major] 强制"去除背景音乐"失败时静默降级用原声，provenance 不标注降级，且与"分离器不可用"路径的硬失败策略不一致
  - 位置: src/features/edit/ai_editing/transcript_router.py:80-99
  - 证据:
    ```python
    if self._demucs is not None and self._is_available(self._demucs):
        try:
            demucs_result = self._run_demucs(...)
            return self._annotate(demucs_result, "qwen3_asr_demucs_forced" if force_demucs else ...)
        except Exception as exc:
            if mode == "demucs":
                raise
            logger.warning("Forced Demucs preprocessing unavailable: %s", exc)
            _emit_progress(progress, None, f"人声分离不可用，继续使用原声：{exc}")
    elif mode == "demucs":
        raise RuntimeError("已选择人声分离，但 Demucs 模型不可用")
    elif force_demucs:
        raise RuntimeError(f"已选择去除背景音乐，但无法启动人声分离：{detail}")
    ```
  - 影响: 用户显式选择 `background_music=remove` 时：分离器不可用 → 硬抛错（好）；分离器可用但运行失败 → 仅 logger.warning + 一次性进度消息后继续用原声，最终结果的 provenance 只标 `router_selection="qwen3_asr_original"`，持久化的 analysis 里没有任何"请求过伴奏去除但被跳过"的痕迹。违背项目红线"可降级，必标注"（transcript_provider 模块文档 §2 同款原则），用户会误以为成品已去伴奏。
  - 建议: 异常降级路径在返回的 provenance.extra 里追加如 `demucs_requested_but_failed: str(exc)`（或在 `_annotate` 传入降级原因），并统一两条路径的策略（要么都硬失败，要么都软降级但必标注）。
  - 置信度: 高

- [AUD-14911] [Suggestion] validate_options 不区分 mode，whisperx 模式下仍按 Qwen pack 校验，可能误拒合法请求
  - 位置: src/features/edit/ai_editing/transcript_router.py:50-63
  - 证据:
    ```python
    def validate_options(self, options=None) -> str | None:
        request_options = dict(options or {})
        if str(request_options.get("background_music", "keep")) == "remove":
            ...
        validator = getattr(self._qwen, "validate_options", None)
        if callable(validator):
            return validator(request_options)   # 无论 mode 是什么都校验 Qwen
    ```
  - 影响: `mode == "whisperx"`（显式回退模式）时请求根本不会走 Qwen，但 Qwen pack 未装/校验失败仍会让 validate_options 返回错误，宿主据此拒绝启动任务——WhisperX 明明可用却被 Qwen 的校验挡住。
  - 建议: `mode == "whisperx"` 时校验 `_whisperx` 的可用性而非透传 Qwen 校验。
  - 置信度: 中（whisperx 模式需显式设置 TOPOS_TRANSCRIPT_MODE，默认 auto 不受影响）

- [AUD-14912] [Suggestion] ProfileAwareTranscriptProvider._remote_by_profile 缓存字典无锁，并发 analyze 可重复创建 remote provider
  - 位置: src/features/edit/ai_editing/transcript_router.py:245-257
  - 证据:
    ```python
    def _remote(self, profile: Any) -> Any:
        provider = self._remote_by_profile.get(profile.profile_id)
        if provider is None:
            from ... import RemoteTranscriptProvider
            provider = RemoteTranscriptProvider(profile, self._credential_store, ...)
            self._remote_by_profile[profile.profile_id] = provider
        return provider
    ```
  - 影响: check-then-set 无锁；两个线程首次并发命中同一 profile 会各建一个 provider（后者覆盖前者），若 RemoteTranscriptProvider 构造含资源开销则浪费。后果轻微（分析调度确认走 ThreadPoolExecutor，存在并发可能）。
  - 建议: 加 `threading.Lock` 双检，或用 `dict.setdefault` 语义。
  - 置信度: 中

### 通过项
- 线程安全：AdaptiveTranscriptProvider 无共享可变状态（mode/threshold 构造期定型）；provider 经 analyze_with_preprocessor 的 thread-local 切换正确恢复
- GPU 资源生命周期：N/A（无 GPU 资源持有）
- GLSL·HDR 正确性：N/A
- 信号防递归：N/A
- 状态提交一致性：`_annotate` 用 dataclasses.replace 重建 provenance/field_provenance，不改原对象；router_selection 保留路由证据
- 异常与取消路径：Qwen 异常被记录并在无任何可用结果时原样 re-raise（L147-148），错误不丢失；progress 回调异常被隔离（L292-299）
- 输入验证：quality_threshold 构造期夹紧到 [0,1]（L48）；demucs 请求前可用性双重检查
- 性能模式：Demucs 只在质量不达标/失败后触发，"never runs for every asset"（L118-119 注释与实现一致）；remote provider 惰性创建避免打开偏好设置即联网
- 类型注解：完整

---

## src/features/edit/ai_editing/tts_provider.py (行数: 435)

### 发现

- [AUD-14913] [Major] 单条 cue 合成失败被 continue 跳过后，cue_durations 与字幕 cue 的索引对应关系断裂，controller 按索引配对会错位
  - 位置: src/features/edit/ai_editing/tts_provider.py:159-162, 209-211, 291-293（消费方 src/app/controllers/edit_controller.py:1356-1368）
  - 证据:
    ```python
    # L159-162（_synthesize_custom_voice；voice_clone 同型 L209-211）
    wavs, sr = engine.generate_custom_voice(**cv_kwargs)
    if not wavs:
        continue                       # ← 跳过的 cue 不产生 entry
    results.append((int(cue.start_frame), wavs[0], int(sr)))
    ```
    ```python
    # L291-293（按 waves 重排，索引已相对 cues 偏移）
    cue_durations = tuple(
        float(len(w[1])) / float(w[2]) for w in waves
    )
    ```
    ```python
    # edit_controller.py:1366-1368（按位置索引一一配对）
    for i, sub_clip in enumerate(sub_clips):
        if i >= len(cue_durations):
            break
        audio_frames = int(cue_durations[i] * fps) + pause_frames
    ```
  - 影响: 引擎对某条文本返回空结果时（代码作者明确认为可能，否则不会写 continue），`waves`/`cue_durations` 少一条，其后所有 cue 的实际时长向前错一位——controller 把第 i+1 条语音的时长安到第 i 条字幕上，字幕与配音系统性错位且无任何告警。`SynthesizedSpeech.cue_durations` 的文档承诺"按输入 cues 顺序"也被破坏。
  - 建议: 失败 cue 以 0 时长占位（或返回 `(cue_index, duration)` 对/跳过标记），保证 `len(cue_durations) == len(clean_cues)` 且语义对位；或在结果里显式携带失败 cue 索引列表供 controller 感知。
  - 置信度: 中（触发依赖引擎对个别 cue 返回空；索引错位机制由代码结构直接确认）

- [AUD-14914] [Suggestion] _has_model_weights 尾部有 10 行不可达死代码，且引用了静态方法中不存在的 self/pack_id
  - 位置: src/features/edit/ai_editing/tts_provider.py:373-383
  - 证据:
    ```python
    @staticmethod
    def _has_model_weights(path: str) -> bool:
        ...
        return any(os.path.isfile(os.path.join(path, n)) for n in weight_names)  # L373 无条件 return
        try:                                                                       # L374 起不可达
            path = self._model_pack_manager.require_usable(pack_id)   # ← self/pack_id 未定义
            ...
        raise RuntimeError(f"模型 pack '{pack_id}' 未安装")
    ```
  - 影响: 明显的粘贴残留：`@staticmethod` 内引用 `self` 与未绑定的 `pack_id`。当前因不可达不会抛错，但任何重排（如把校验提前）都会立刻 NameError；同时误导读者以为有 manager 侧的错误处理。
  - 建议: 删除 L374-383。
  - 置信度: 高

- [AUD-14915] [Suggestion] _lay_down_waves 前段含停顿的 cue_durations 记账在末尾被整体覆盖，注释"停顿计入该 cue 时长"与实际返回值矛盾
  - 位置: src/features/edit/ai_editing/tts_provider.py:252, 267-272, 291-293
  - 证据:
    ```python
    cue_durations.append(float(len(seg)) / out_sr)      # L267
    if idx < len(waves) - 1:
        processed.append(np.zeros(pause_samples, dtype=np.float32))
        cue_durations[-1] += 0.3  # 停顿计入该 cue 时长   # L272
    ...
    cue_durations = tuple(                               # L291-293 整体覆盖，0.3 丢失
        float(len(w[1])) / float(w[2]) for w in waves
    )
    ```
  - 影响: 最终返回值不含 0.3s 停顿。已核实 controller 侧自行加 `pause_frames = max(1, int(fps*0.3))`（edit_controller.py:1363），所以现行为恰好对齐、无实际时序 bug，但 L252-272 的记账与注释是死逻辑，读者极易据此"修复"成含停顿版本从而引入双重停顿。
  - 建议: 删除 L267 处的中间记账与 L272 注释（或直接保留最终覆盖并注明"停顿由 controller 补加"）。
  - 置信度: 高

- [AUD-14916] [Suggestion] np.asarray 不拷贝时 50ms 淡入会就地改写引擎返回的波形缓冲
  - 位置: src/features/edit/ai_editing/tts_provider.py:260-264
  - 证据:
    ```python
    seg = np.asarray(waveform, dtype=np.float32)
    fade = min(int(0.05 * out_sr), len(seg))
    if fade > 0:
        seg[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    ```
  - 影响: 若引擎返回的已是 float32 ndarray，`np.asarray` 返回同一对象，`*= ` 就地修改引擎输出；引擎若复用内部缓冲（或调用方还持有该波形），数据被污染。
  - 建议: 改用 `np.array(waveform, dtype=np.float32)`（强制拷贝）。
  - 置信度: 中（是否污染取决于 qwen_tts 返回缓冲的所有权，防御性拷贝零成本）

- [AUD-14917] [Suggestion] _lay_down_waves 的 cues 与 fps 参数从未使用
  - 位置: src/features/edit/ai_editing/tts_provider.py:221-228
  - 证据:
    ```python
    def _lay_down_waves(
        self,
        waves: list[tuple[int, Any, int]],
        cues: tuple[SpeechCue, ...],   # 函数体未引用
        fps: float,                     # 函数体未引用（改为顺序拼接后遗留）
        settings: TtsSettings,
        output_path: str | None,
    ) -> SynthesizedSpeech:
    ```
  - 影响: 顺序拼接重构后的遗留签名，暗示仍按帧定位（实际不是），误导维护者。
  - 建议: 删除两参数并同步调用点。
  - 置信度: 高

- [AUD-14918] [Suggestion] _engines 引擎缓存无锁，并发 synthesize 可重复加载 3.8GB 权重
  - 位置: src/features/edit/ai_editing/tts_provider.py:61-62, 319-334
  - 证据:
    ```python
    self._engines: dict[str, Any] = {}  # model_kind → engine（无锁）
    ...
    if model_kind in self._engines:
        return self._engines[model_kind]
    ...
    engine = self._load_model(model_path, model_kind)
    self._engines[model_kind] = engine
    ```
  - 影响: 与 AUD-14901 同型；TTS 由用户动作触发，双击/并发触发两次即可能双载大模型（内存峰值翻倍）。风险低于 ASR 路径但机制相同。
  - 建议: 加锁双检，或首用时在调用侧串行化。
  - 置信度: 中

### 通过项
- 线程安全：无共享可变状态除引擎缓存外（另见 AUD-14918）
- GPU 资源生命周期：不持 ModernGL 资源；MPS/CUDA 初始化失败回退 CPU float32（L404-419）与 ASR 侧策略一致
- GLSL·HDR 正确性：N/A（音频路径，PCM_16 输出对 TTS 交付物合理，非 HDR 管线数据）
- 信号防递归：N/A
- 状态提交一致性：不直接改时间线；时长修正经 controller 的 UpdateSubtitleClipCommand/MacroCommand 走命令栈，符合 undo 一致性
- 异常与取消路径：空 cue/非法 fps/缺参考音频/模型不可用均显式抛错且消息可行动（L111-119, 176-197）；临时文件用 NamedTemporaryFile(delete=False) 由 controller 侧接管
- 输入验证：synthesize 入口校验空文本、fps>0；voice_clone 校验 ref_audio 存在、ref_text 与 x_vector_only_mode 互斥关系正确
- 性能模式：引擎按 model_kind 惰性加载且只加载当前模式所需；voice_clone prompt 预计算一次复用到全部 cue（L189-197）
- 类型注解：完整

---

## src/features/edit/ai_editing/tts_settings.py (行数: 118)

### 发现

- [AUD-14919] [Suggestion] 高级生成参数无任何范围校验，非法值直接透传给模型
  - 位置: src/features/edit/ai_editing/tts_settings.py:98-114
  - 证据:
    ```python
    temperature: float = 0.9
    top_p: float = 1.0
    top_k: int = 50
    max_new_tokens: int = 2048
    ...
    def to_generate_kwargs(self) -> dict[str, object]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "max_new_tokens": self.max_new_tokens,
        }
    ```
  - 影响: dataclass 无 `__post_init__` 校验：temperature≤0、top_k≤0、max_new_tokens 巨大（内存/时长失控）等值会原样进入 generate 调用，报错来自模型深处或表现为静默异常行为，不满足"外部数据须验证"精神（设置经持久化往返，坏值可能来自旧配置文件）。
  - 建议: 加 `__post_init__` 夹紧/校验（temperature>0、0<top_p≤1、top_k≥1、max_new_tokens 有上限），或在 to_generate_kwargs 内防御。
  - 置信度: 高（无校验确定；影响低）

### 通过项
- 线程安全：纯 frozen-able 数据定义（dataclass 不可变性未加 frozen 但无方法修改状态）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径：N/A（纯 DTO）
- 输入验证：见 AUD-14919（唯一缺口）
- 性能模式：N/A
- 类型注解：完整（Literal 模式、字段文档齐全）

---

## src/features/edit/ai_editing/user_ai_config.py (行数: 12)

### 发现

（无）

### 通过项
- 通过（无逻辑）：兼容 shim，把本模块条目替换为 `src.shared.ai_models.generation.user_ai_config` 的实现（`_sys.modules[__name__] = _impl`），历史导入路径（含私有名）继续可用；无控制流、无状态。

---

## src/features/edit/ai_editing/variant_planner.py (行数: 395)

### 发现

- [AUD-14920] [Major] 用户锁定的 source range 在选段中完全未被使用（locked 参数是死参数），评分却硬编码 hard_constraints_passed=True
  - 位置: src/features/edit/ai_editing/variant_planner.py:235-263, 341
  - 证据:
    ```python
    def _select_segment_for_beat(
        self, asset_id, beat_range, index, strategy,
        locked: tuple[LockedSourceRange, ...],   # ← 参数
        rejected: tuple[str, ...],
    ) -> SemanticSegment | None:
        ...
        candidates = index.query(asset_id=asset_id, kind=SegmentKind(prefer_kind), within=beat_range)
        if not candidates:
            candidates = index.query(asset_id=asset_id, within=beat_range)
        candidates = [c for c in candidates if _subject_sig(c) not in rejected]  # 只过滤 rejected
        ...
        return candidates[0]                    # 函数体从未引用 locked
    ```
    ```python
    # L341
    hard_constraints_passed=True,  # locked/rejected 已在选段时遵守   ← 注释与实现不符
    ```
  - 影响: 违反本模块文档声明的红线（"用户锁定/已接受/已拒绝的 decision 必须在重规划时保留"、TAEP §5.5 硬约束 pass/fail）。`plan()` 花代价合并 `constraints.locked_ranges + brief_hard`（L130-131）后传入，但选段既不优先保留锁定区间，编译出的 EditPlan 也不含保证锁定区间出现的 operation；更糟的是评分向用户断言"硬约束已通过"——用户比较 A/B 方案时看到的 pass 徽章是假的，锁定片段可能被方案悄悄丢掉。
  - 建议: 在 `_select_segment_for_beat` 中把 locked ranges 转为优先候选（或强制作为对应 beat 的 evidence）；`_score` 基于"锁定区间是否全部出现在 outline/plan"真实计算 hard_constraints_passed；至少在未实现前把硬编码值改为显式 TODO 并降低 confidence 标注。
  - 置信度: 高

- [AUD-14921] [Suggestion] _compile_plan 用 __import__ 动态导入并伪造 TRANSCRIPT 类型的 SemanticSegment 生成 intent 元数据
  - 位置: src/features/edit/ai_editing/variant_planner.py:306-311
  - 证据:
    ```python
    intent={"reason": self._explain(beat_id, SemanticSegment(
        segment_id="planned", asset_id=dec.subject.asset_id,
        source_range=dec.subject.source,
        kind=__import__("src.features.edit.ai_editing.editorial_models", fromlist=["SegmentKind"]).SegmentKind.TRANSCRIPT,
    ), "")},
    ```
  - 影响: 仅为生成一句解释文本而在循环内动态 import 并构造假 segment（B-roll 策略选的是 shot 段，也被标成 TRANSCRIPT）；`SegmentKind` 本可在文件顶部正常导入（`_select_segment_for_beat` L246 就是局部 import）。假类型元数据进入 plan 的 intent，对审计/回放有误导。
  - 建议: 顶部统一 import SegmentKind；把 chosen segment 的真实 kind/置信度传入 `_compile_plan` 复用其 payload 生成 reason，不构造假对象。
  - 置信度: 高

- [AUD-14922] [Suggestion] duration_fit 度量的是分配的 beat 区间总和而非实际选段时长，恒≈1.0 失去排序意义
  - 位置: src/features/edit/ai_editing/variant_planner.py:333-335
  - 证据:
    ```python
    # duration fit：实际选段总时长 / 目标
    actual = sum(b.target_range.duration for b in outline)   # ← outline 的 target_range 是 _allocate_beat_ranges 的分配值
    duration_fit = min(1.0, actual / max(1, inp.target_duration))
    ```
  - 影响: `outline[].target_range` 来自权重分配（总和≈target），与是否真的找到了 evidence segment 无关——即使所有 beat 都没选到段，duration_fit 仍≈1.0，无法区分方案质量，违背"必须保留 evidence 与 alternatives、分数可用于排序"的设计意图。
  - 建议: actual 应取各 beat 实际 evidence segment 的 `source_range.duration` 之和（无 evidence 记 0）。
  - 置信度: 中（也可能是有意的占位实现，但注释写着"实际选段总时长"，与实现不符）

- [AUD-14923] [Suggestion] on_plan_compiled 回调抛出的异常会中断其余策略的规划循环
  - 位置: src/features/edit/ai_editing/variant_planner.py:134-141
  - 证据:
    ```python
    for strategy in strategies:
        variant, plan = self._build_variant(...)
        if on_plan_compiled is not None and plan is not None:
            on_plan_compiled(plan, variant)      # 回调（repository 写入）异常直接冒泡
        variants.append(variant)
    return variants
    ```
  - 影响: 回调用于把 plan 写入 PlanRepository；一次存储写入失败会让第二个策略不再生成，返回路径直接异常，与"至少给出两个可比较方案"的契约冲突（且第一个 variant 可能已入库形成半状态）。
  - 建议: 捕获回调异常记 warning 后继续下一策略（回调属可观测性附属操作），或文档化该失败语义。
  - 置信度: 中

### 通过项
- 线程安全：planner 无跨调用可变状态（brief_service 只读查询）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：N/A
- 状态提交一致性：只产 proposal（PlanMode.PREVIEW、DecisionState.PROPOSED、variant status PROPOSED），不 commit；base_revision 乐观锁透传进 EditPlan.create；decision 携带 evidence_refs 与 alternatives_considered 符合 TAEP §5.5
- 异常与取消路径：无静默吞错；SemanticSegment 构造依赖的字段均有默认值（已核对 editorial_models.py:598-610），不会 TypeError
- 输入验证：`_allocate_beat_ranges` 对 unlisted_count=0 有除零防护（L227）；Rational/FrameRange 由上游模型校验
- 性能模式：确定性规则引擎，复杂度线性于 beats×segments，无重复计算热点
- 类型注解：完整（含 Callable 回调签名）；`VariantStrategy` 空类属残留抽象（未实例化，不影响运行）

---

## 批次总结

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 6 | AUD-14900, 14901, 14902, 14910, 14913, 14920 |
| Suggestion | 17 | AUD-14903–14909 区间内 6 项、14911–14912、14914–14919、14921–14923 |

重点：AUD-14900（diarization 帧/sample 量纲错配，speaker 归属在生产路径必然错误）与 AUD-14920（锁定区间约束未实现但评分恒报通过）直接影响 TAEP §5.5 的可信度红线；AUD-14913 在个别 cue 失败时会造成字幕-配音系统性错位。
