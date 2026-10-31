# 批次报告 batch-119 — src/features/deliver/core

- 任务书：docs/audit/2026-09-28/assignments/batch-119.md（编号区间 AUD-11900–AUD-11999）
- 审查日期：2026-09-28
- 文件数：8（2,977 行）
- 汇总：Critical 0 · Major 5 · Suggestion 13
- 维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/deliver/core/__init__.py (行数: 0)

### 发现

（无）

### 通过项
- 通过（无逻辑）：空文件，无导出符号；各子模块由使用方按模块路径直接导入。

---

## src/features/deliver/core/custom_export_presets.py (行数: 275)

### 发现

- [AUD-11901] [Suggestion] created_at 回退 os.path.getmtime 未捕获 OSError，竞态可炸掉整个预设列表
  - 位置: src/features/deliver/core/custom_export_presets.py:156
  - 证据:
    ```python
    preset, created_at = parsed
    entries.append((created_at or os.path.getmtime(path), preset))
    ```
  - 影响: 旧格式预设缺 created_at（解析为 0.0）时回退 getmtime；文件在 listdir 与 getmtime 之间被外部删除（同步盘/用户手动清理）→ OSError 未捕获 → list_user_presets 抛出 → 面板加载崩溃，与模块"一个坏文件不拖垮交付页"的自述目标相悖。
  - 建议: `try: mtime = os.path.getmtime(path) except OSError: mtime = 0.0` 后再参与排序。
  - 置信度: 高

### 通过项
- 原子写入（临时文件 + os.replace），坏 JSON 逐文件跳过且有日志，符合模块契约。
- preset_id 经 _slug 白名单净化，无路径穿越风险（`../../x` 被压平）。
- 保存/重命名的空名与重名校验（ValueError 显式抛出）。
- Presets 根目录使用 os.getcwd() 与 color/edit 各预设模块全库一致（presets_panel、custom_transitions 等同约定），非缺陷。
- 无 Qt/线程/GPU 依赖；类型注解齐全。

---

## src/features/deliver/core/delivery_capability.py (行数: 139)

### 发现

- [AUD-11902] [Suggestion] container 推导对 gif 与序列 codec 给出错误的 "mov"
  - 位置: src/features/deliver/core/delivery_capability.py:77-78
  - 证据:
    ```python
    container = "mov" if codec_value.startswith(("topos", "prores")) else \
        ("mp4" if codec_value in ("h264", "hevc") else "mov")
    ```
  - 影响: gif → "mov"（实际容器是 gif）、png/tiff/topos_image_sequence → "mov"（无容器）、h264+mkv/avi 等合法组合被硬编码为 mp4。当前 describe_delivery_capability 全库无调用方（已 grep 验证），属潜在事实错误；一旦接入 AI request_export/CLI 将对外播报错误容器信息。
  - 建议: 复用 video_export_formats.CODEC_CONTAINER_TABLE / SEQUENCE_CODECS 推导容器，而不是本地硬编码三元式。
  - 置信度: 高

- [AUD-11903] [Suggestion] 装饰性导入 ExportPreflightResult 使效果投影整体静默降级为 "_error"
  - 位置: src/features/deliver/core/delivery_capability.py:91-93（配合 123 行 except）
  - 证据:
    ```python
    from src.features.edit.core.timeline_export import (
        ExportPreflightResult,  # noqa: F401 — 类型可读性
    )
    ...
    except Exception:  # noqa: BLE001 — 能力求证失败显式标注 unknown
        effects = {"_error": {"exportable": False, ...
    ```
  - 影响: 该导入仅为"类型可读性"保留；将来 timeline_export 重命名/移除该符号时 ImportError 会被外层 except 捕获 → 全部效果投影静默变 "_error"（capability_unknown），而效果目录本身其实可用，掩盖了真实领域错误。
  - 建议: 删除该装饰性导入；或把导入与其余效果目录导入分开捕获。
  - 置信度: 中

### 通过项
- "未知显式标 unknown 而非猜测"的 fail-open 语义有注释且实现一致；fail 时显式产出 "_error" 条目而非静默空表。
- 无状态纯函数、无线程/GPU/Qt 依赖；frozen dataclass + to_dict JSON-safe；类型注解齐全。
- ALPHA/HDR/无音轨 codec 事实表（AUDIO_INCAPABLE_CODECS 等）与本批次 video_export_formats 表交叉一致。

---

## src/features/deliver/core/export_queue.py (行数: 434)

### 发现

- [AUD-11904] [Major] resume() 在暂停排空循环后无法重启队列（_running 已被置 False）
  - 位置: src/features/deliver/core/export_queue.py:335-341（缺陷）、421-425（成因）、365-368（retry 的正确对照）
  - 证据:
    ```python
    def resume(self) -> None:
        """Resume from pause and continue processing."""
        if not self._paused:
            return
        self._paused = False
        if self._running and not self._is_currently_executing():
            self._process_next()
    ```
    ```python
    # Continue to the next job if not paused.
    if self._paused:
        self._running = False
    else:
        self._process_next()
    ```
    对照 retry()：
    ```python
    if not self._paused and not self._is_currently_executing():
        # Resume processing — start() is idempotent if already running.
        self._running = True
        self._process_next()
    ```
  - 影响: 暂停期间当前作业结束时（如 pause 后 cancel 使 runner 返回，或 runner 不支持暂停/测试用同步 runner），_process_next 尾部把 _running 置 False；随后 resume() 因 `self._running` 为 False 不再调度，剩余 PENDING 作业滞留。控制器 resume_queue 只调 queue.resume()（video_batch_export_controller.py:191-194），UI 显示"空闲"，用户必须再按 Start。retry() 显式 `self._running = True` 证明此处是遗漏而非设计。
  - 建议: resume() 中去掉 `_running` 前置条件或像 retry() 一样先置 `self._running = True` 再 `_process_next()`（runner 为 None 时仍需防护）。
  - 置信度: 高

- [AUD-11905] [Major] load_export_queue 对"合法 JSON 但形状错误"的队列文件抛未捕获异常
  - 位置: src/features/deliver/core/export_queue.py:188-195（及 113-119 的强转）
  - 证据:
    ```python
    try:
        with _store_lock:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load export queue: {e}")
        return []
    jobs = [ExportJob.from_dict(j) for j in data.get("jobs", [])]
    ```
  - 影响: 文件是合法 JSON 但顶层非对象（如 `[]`、字符串）→ `data.get` 抛 AttributeError；"jobs" 项非 dict → ExportJob.from_dict 内 `data.get` 抛 AttributeError；progress 等字段为非数字字符串 → `float(...)` 抛 ValueError——均不在捕获范围。has_restorable_jobs()/启动恢复路径直接崩溃，违背"坏文件只警告不拖垮"的模块契约与外部数据须验证铁律。
  - 建议: 捕获 AttributeError/TypeError/ValueError；对每个 job 条目做 isinstance(j, dict) 预检，坏条目跳过并记日志。
  - 置信度: 高

- [AUD-11906] [Major] _process_next 尾递归调度，千级批量作业触发 RecursionError
  - 位置: src/features/deliver/core/export_queue.py:421-425
  - 证据:
    ```python
    # Continue to the next job if not paused.
    if self._paused:
        self._running = False
    else:
        self._process_next()
    ```
  - 影响: 每完成一个作业递归深一层（无循环展开）。NamingConfig.RenderMode.INDIVIDUAL_CLIPS（"一个片段一个文件"）在长时间线上可生成成百上千作业，接近 Python 默认 1000 层递归上限时抛 RecursionError；401-416 行的 try 只包 runner 调用不含此递归，异常从 start() 一路冒泡到 UI 槽，队列中断。
  - 建议: 把"取下一作业→执行"改为 while 循环，消除自递归。
  - 置信度: 中（触发需约千级作业，INDIVIDUAL_CLIPS 模式下可达）

- [AUD-11907] [Suggestion] clear_completed 用 O(n²) 值相等成员判断，且可误删字段全同的作业
  - 位置: src/features/deliver/core/export_queue.py:313-316
  - 证据:
    ```python
    keep = [j for j in self._jobs if j.status in (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED)]
    removed = [j.job_id for j in self._jobs if j not in keep]
    self._jobs = keep
    self._by_id = {j.job_id: j for j in keep}
    ```
  - 影响: `j not in keep` 按 dataclass 值相等比较（非 id），两个字段完全相等的作业会被误判为"已保留"；大队列下 O(n²)。
  - 建议: 先按 id 集合收集 removed（如 `keep_ids = {id(j) for j in keep}` 或按 status 直接分流）。
  - 置信度: 高

- [AUD-11908] [Suggestion] JobStatus.PAUSED 从未被赋值，是永不出现的状态
  - 位置: src/features/deliver/core/export_queue.py:52（定义）、313（唯一读取点）
  - 证据:
    ```python
    PAUSED = "paused"  # queue-level pause marker on the head job
    ...
    keep = [j for j in self._jobs if j.status in (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED)]
    ```
  - 影响: 全库 grep 无任何 `status = JobStatus.PAUSED` 赋值点（仅批量面板为其保留了颜色/图标/文案映射，video_batch_export_panel.py:82/92/102），clear_completed 的保留分支为死代码，UI 的"已暂停"状态显示永远不可达。
  - 建议: 删除该常量与 UI 映射，或实现队列级暂停落盘到头作业。
  - 置信度: 高

### 通过项
- 持久化原子性正确：mkstemp 同目录临时文件 + flush + fsync + os.replace，失败清理临时文件。
- _store_lock 保护跨线程 save/load/clear；队列本体按设计只在主线程变更。
- 陈旧 RUNNING 落盘状态在 load 时降级 PENDING 并清 progress，崩溃恢复语义正确。
- 取消标志在结果分类后消费（419 行），不泄漏到下一作业；start 前 cancel 仍生效的语义有注释支撑。
- 进度回调 clamp 到 [0,1]；enqueue 强制 PENDING（preserve_status 显式例外用于 NEEDS_RELINK 恢复）。
- 类型注解齐全；异常路径均有日志。

---

## src/features/deliver/core/video_batch_export_controller.py (行数: 295)

### 发现

- [AUD-11909] [Major] restore_from_disk 无异常防护：重复 job_id / 坏快照可冒泡炸掉工程切换主流程
  - 位置: src/features/deliver/core/video_batch_export_controller.py:229-247（配合 export_queue.py:290-291 的 raise、deliver_workspace.py:477 的无捕获调用）
  - 证据:
    ```python
    loaded = load_export_queue()
    for legacy in loaded:
        job = VideoExportJob.from_legacy_job(legacy)
        if not job.timeline_id:
            # Nothing to relink against; leave as-is.
            self._queue.enqueue(job.to_legacy_job(), preserve_status=False)
            continue
    ...
            self._queue.enqueue(legacy, preserve_status=True)
    ```
    export_queue.py:
    ```python
    if job.job_id in self._by_id:
        raise ValueError(f"Duplicate job_id: {job.job_id}")
    ```
  - 影响: 导出运行期间切换工程（runner 嵌套事件循环保持 UI 响应，set_project 可达）时，运行中作业无法被 remove（export_queue.py:303 拒绝 RUNNING），而队列文件在 job_started 时已持久化该 id；restore_from_disk 装入同一 job_id → enqueue 抛 ValueError → 冒泡至 deliver_workspace.set_project（477 行无 try）→ 主流程崩溃。持久文件本身含重复 id、或 from_legacy_job 解析坏快照抛 ValueError（见 AUD-11913）时，启动恢复同样崩溃。add_jobs 有 try/except ValueError 防护，restore 路径没有。
  - 建议: restore_from_disk 内逐作业 try/except（ValueError/TypeError/KeyError），坏/重复条目记日志跳过；或给 enqueue 提供"已存在则跳过"语义。
  - 置信度: 中（运行中切换工程的路径依赖嵌套事件循环期间的 UI 交互；持久文件重复 id 属低概率但后果为崩溃）

- [AUD-11910] [Suggestion] 无 timeline_id 的恢复作业被强制 PENDING，与"leave as-is"注释意图不符
  - 位置: src/features/deliver/core/video_batch_export_controller.py:232-234
  - 证据:
    ```python
    if not job.timeline_id:
        # Nothing to relink against; leave as-is.
        self._queue.enqueue(job.to_legacy_job(), preserve_status=False)
        continue
    ```
  - 影响: preserve_status=False 会把恢复来的 FAILED/NEEDS_RELINK 无时间线作业强制重置为 PENDING，下次 start 即自动运行并注定在 context 解析处失败（错误可显示，非静默），但多跑一轮注定失败的导出；注释声称"leave as-is"而实现相反。
  - 建议: 该分支改用 preserve_status=True 保留原终态，或直接不恢复无时间线引用的失败作业。
  - 置信度: 中

### 通过项
- 队列信号 → 控制器信号为单向转发，控制器不触碰任何 QWidget，无程序化更新控件的信号递归风险。
- _resolve_context 失败路径返回带 reason 的 JobContext（而非抛异常），与 video_job_runner.JobContext 签名（project/sequence/project_id/project_revision/reason）核实一致。
- start/pause/resume/cancel/retry 对队列与 runner 的成对调用与两个模块的状态机吻合；retry 期间队列正在执行时不二次调度（_is_currently_executing 防护）。
- restore_from_disk 的 NEEDS_RELINK 双写（legacy + 快照）保证队列货币与类型视图一致，且 NEEDS_RELINK 不会被 _process_next 拾取自动运行。
- 无 GPU/OpenGL 涉及；类型注解齐全。

---

## src/features/deliver/core/video_export_formats.py (行数: 95)

### 发现

- [AUD-11911] [Suggestion] 序列 codec 的兼容性错误文案渲染为 "Allowed: ."，用户无从理解
  - 位置: src/features/deliver/core/video_export_formats.py:53-55（表）、89-94（文案）
  - 证据:
    ```python
    "png_sequence": frozenset({""}),
    "tiff_sequence": frozenset({""}),
    "topos_image_sequence": frozenset({""}),
    ...
    if normalized not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        return (
            f"Codec “{codec}” cannot be wrapped in “{normalized}”. "
            f"Allowed: {allowed_text}."
        )
    ```
  - 影响: 序列 codec 的允许容器集为 {""}，join 后为空串 → 错误信息变成 "... Allowed: ."，没有告诉用户图片序列没有容器概念（恢复/程序化作业携带默认容器 "mp4" 时即触发该文案）。
  - 建议: 对 SEQUENCE_CODECS 成员给专门文案（如 "image sequence codecs have no container"）。
  - 置信度: 高

### 通过项
- 表为唯一真值源，UI 与 preflight 共用；未知 codec 显式报错而非放行。
- 所有查询入口做 lower().strip() 归一化；纯函数、无状态、无 IO、无线程风险。
- __all__ 导出完整；类型注解齐全。

---

## src/features/deliver/core/video_export_job.py (行数: 976)

### 发现

- [AUD-11912] [Suggestion] _topos_pixel_format_override 用 startswith("topos") 误吞 topos_image_sequence，默认 pixel_format 必打误导性告警
  - 位置: src/features/deliver/core/video_export_job.py:902-918（配合 156 行默认值）
  - 证据:
    ```python
    codec = str(video_config.codec or "")
    if not codec.startswith("topos"):
        return ""
    ...
    parsed = parse_topos_pix_fmt(name)
    if parsed is None:
        ...
        logging.getLogger(__name__).warning(
            "topos export: pixel_format %r not in the Topos encoder domain; "
            "falling back to codec-map default ...", name)
    ```
    156 行：`pixel_format: str = "yuv420p"`。已核实 topos_encoder._parse_topos_pix_fmt 的域只认 yuv422/yuv444/gbr 的 10/12/16-bit（topos_encoder.py:167-189），"yuv420p" 必然返回 None。
  - 影响: "topos_image_sequence" 同样命中 startswith("topos")；面板为序列作业保留默认 pixel_format="yuv420p"，于是每次图片序列导出/预检都打一条"回落 codec-map 默认"的告警——该值对图片序列毫无意义，告警纯属噪音，且冲淡了真正 Topos 视频档的降级日志信号。
  - 建议: 在 override 前排除 SEQUENCE_CODECS（或仅对视频 topos 档判定），避免图片序列进入该分支。
  - 置信度: 高

- [AUD-11913] [Suggestion] from_dict/from_flat_config 的 int()/float() 强转无容错，坏队列文件在恢复路径抛 ValueError
  - 位置: src/features/deliver/core/video_export_job.py:637-666（from_dict）、757-775（from_flat_config）
  - 证据:
    ```python
    return VideoExportJob(
        schema_version=int(data.get("schema_version", VIDEO_EXPORT_JOB_SCHEMA_VERSION)),
        ...
        created_at=float(data.get("created_at", 0.0)),
        ...
    ```
  - 影响: 队列文件被手工编辑或半损坏时，schema_version/created_at/retry_count 等为非数字字符串 → ValueError/TypeError；经 from_legacy_job → controller.restore_from_disk（无捕获，见 AUD-11909）冒泡，启动恢复崩溃。字段虽是程序自写 JSON，但与"外部数据须验证"的项目铁律不符。
  - 建议: 用带默认的安全转换工具（try/except 回落默认值），或在外层恢复路径捕获并跳过坏快照。
  - 置信度: 高（代码路径确凿；触发需文件损坏）

### 通过项
- 版本容错正确：schema_version 显式、未知字段存 _unknown_fields 并在 to_dict 回写，round-trip 不丢数据。
- 陈旧 RUNNING 状态在 from_dict 降级 PENDING 并清 progress，与 ExportQueue 的恢复语义一致。
- validate() 覆盖结构化字段（范围、分辨率、fps、GIF 颜色数/帧率、扩展名、overwrite/subtitle/render_mode/filename_use 枚举）。
- enum 映射（_map_codec/_map_source_kind/_map_range_type）未知值显式 ValueError，无静默猜测；HEVC 位深覆盖的 profile 优先语义有历史兼容注释。
- 与 edit 特性解耦（延迟导入），无 Qt/线程/GPU 资源；类型注解齐全。

---

## src/features/deliver/core/video_export_preflight.py (行数: 763)

### 发现

- [AUD-11914] [Major] _check_color_delivery_preflight 裸吞全部 Exception 且零日志，色彩丢弃告警可能整体静默失效
  - 位置: src/features/deliver/core/video_export_preflight.py:331-349
  - 证据:
    ```python
    try:
        from src.features.color.core.delivery_matrix import (
            delivery_preflight_issues, report_from_color_stacks, ...
        )
        ...
        for code, message in delivery_preflight_issues(report):
            result.add_warning(code, message, field="color_delivery")
    except Exception:
        # 报告生成失败不阻断导出（能力探测是尽力而为）
        return
    ```
  - 影响: 该检查的全部职责就是把"curves/HSL/LUT/窗口等在视频导出路径会被丢弃"提示给用户；任何集成错误（ImportError、delivery_matrix API 变更、序列数据异常）都被无声吞掉，功能整体失效而用户毫无感知地产出缺色彩处理的成片。违反 .clinerules"不得宽泛 except 掩盖领域错误"，且与同文件其它检查（649/669/707 行均 logger.exception）的做法不一致。
  - 建议: 至少 `logger.exception("color delivery preflight failed")` 后再 return；ImportError 与数据异常宜分开处理。
  - 置信度: 高

- [AUD-11915] [Suggestion] 序列 codec 的"音频将被丢弃"提示缺失或误导（仅 gif 有明确告警）
  - 位置: src/features/deliver/core/video_export_preflight.py:437-452（gif 告警与音频检查门）、296-307（topos 分支静默通过）
  - 证据:
    ```python
    if job.audio_config.include_audio:
        result.add_warning(
            "gif.audio_unsupported",
            "Animated GIF has no audio track; the audio setting will be ignored.",
    ...
    # Audio codec compatibility (warning — we suggest a fix rather than block)
    if job.audio_config.include_audio and codec != "gif":
        audio_ok, suggestion = _check_audio_codec(job.audio_config.codec, container)
    ```
    topos 分支（296-307）：
    ```python
    if job.audio_config.include_audio:
        audio_codec = str(getattr(job.audio_config, "codec", "") or "").lower()
        if audio_codec not in ("aac", "pcm_s16", "pcm_s16le", "pcm_s24", "pcm_s24le"):
            result.add_warning(...)
        return
    ```
  - 影响: delivery_capability.AUDIO_INCAPABLE_CODECS 明确把 topos_image_sequence 与 gif 并列为"无音轨"，但 preflight 只有 gif 得到准确告警：png/tiff 序列（container=""）只是碰巧触发 audio.codec_incompatible，文案是"音频编解码 aac 不支持空容器，建议 mp4/mov/…"——换容器对图片序列毫无意义；topos_image_sequence 若 container 非空（恢复/程序化作业默认 "mp4"）且音频编解码为 aac，则命中 topos 白名单完全静默。用户勾选"包含音频"后输出无音轨却得不到一条正确解释。
  - 建议: 按 SEQUENCE_CODECS（或 AUDIO_INCAPABLE_CODECS）统一给出"图片序列不含音频轨，include_audio 将被忽略"的显式 warning，与 gif 同级。
  - 置信度: 高

- [AUD-11916] [Suggestion] preflight 不调用 job.validate()，模型级结构校验在入队路径无人执行
  - 位置: src/features/deliver/core/video_export_preflight.py:213-220（检查清单）
  - 证据:
    ```python
    _check_context(job, ctx, result)
    _check_structural(job, ctx, result)
    _check_runtime_capability(job, ctx, result)
    _check_output_path(job, ctx, result)
    _check_disk_space(job, ctx, result)
    _check_edit_feature_preflights(job, ctx, result)
    _check_color_delivery_preflight(job, ctx, result)
    _check_topos_video_only(job, ctx, result)
    ```
  - 影响: 已核实设置面板构建作业只走 run_video_export_preflight（video_export_settings_panel.py:1643/2864），全 deliver 无 job.validate() 调用。validate() 中 preflight 未复制的检查——分辨率非正、非 GIF 的 fps≤0、输出扩展名与 codec 匹配、overwrite_policy/字幕模式/render_mode/filename_use/unique_filename_* 枚举——对恢复（from_flat_config 直传原始值）或迁移来的作业完全无人把关，非法值可入队直达导出器。
  - 建议: run_video_export_preflight 首步执行 `job.validate()` 并把每条错误映射为 blocking issue（带稳定 code 如 "model.invalid"）。
  - 置信度: 中（UI 控件约束了常规路径；风险集中在恢复/程序化作业）

- [AUD-11917] [Suggestion] 占位片段 allow 判定含死代码 extra_options 分支，且条件表达式优先级易误读
  - 位置: src/features/deliver/core/video_export_preflight.py:679-684
  - 证据:
    ```python
    allow = bool(
        getattr(job, "allow_placeholder_clips", False)
        or (job.extra_options or {}).get("allow_placeholder_clips", False)
        if isinstance(getattr(job, "extra_options", None), dict)
        else getattr(job, "allow_placeholder_clips", False)
    )
    ```
  - 影响: VideoExportJob 无 extra_options 字段（全库核实仅 shared/export/encoder.py 自身另有同名属性），isinstance 恒 False，中间分支为死代码；且 `A or B if C else D` 实际解析为 `(A or B) if C else D`，维护者极易误读为 `A or (B if C else D)`。
  - 建议: 删除 extra_options 分支，保留单一 `getattr(job, "allow_placeholder_clips", False)`（B7 语义只认作业快照字段）。
  - 置信度: 高

- [AUD-11918] [Suggestion] _check_disk_space 的 target_dir 表达式含完全重复的分支
  - 位置: src/features/deliver/core/video_export_preflight.py:552-558
  - 证据:
    ```python
    target_dir = (
        os.path.dirname(job.output_path)
        or os.path.dirname(job.output_path)
        or "."
    )
    free = shutil.disk_usage(target_dir).free
    ```
  - 影响: 前两个 or 操作数完全相同，纯冗余；对 output_path 本身就是目录的序列作业，探测的是父目录而非目标目录所在卷（同卷场景无实际偏差，仅语义不精确）。
  - 建议: 删除重复分支；序列作业可直接用 output_path（存在时）探测。
  - 置信度: 高

### 通过项
- BLOCKING/DEGRADED/WARNING/INFO 四级语义实现与文档一致；degraded 必须进结果而非只写日志。
- 占位片段 B7：默认逐条 blocking（含位置与备注），显式 allow 后降级 warning，符合"不静默修复"章程；_list_placeholder_clips 异常被外层捕获不拖垮其它检查。
- 运行时能力门禁 fail-closed：topos 系不可用即入队前 BLOCKING，快照缺 entry() 时跳过不误报。
- 磁盘空间三级阈值（不足/紧张/偏低）与告警文案清晰；edit 三项 preflight 委托均 logger.exception 后降级为 warning。
- 输出路径存在性/可写性检查完备；序列输出目录由 sequence_exporter 自动创建（已核实 makedirs），预检放行非存在目录是正确的。
- 输入验证：range 越界、GIF 颜色数/帧率、编解码-容器兼容、音频编解码兼容均有覆盖。
- 无 GPU/线程/Qt 依赖（纯函数 + 上下文注入，可测试性良好）；类型注解齐全。

---

## 批次总结

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-11904, AUD-11905, AUD-11906, AUD-11909, AUD-11914 |
| Suggestion | 13 | AUD-11901, AUD-11902, AUD-11903, AUD-11907, AUD-11908, AUD-11910, AUD-11911, AUD-11912, AUD-11913, AUD-11915, AUD-11916, AUD-11917, AUD-11918 |

主题性观察：① ExportQueue 的暂停/恢复状态机与尾递归调度是本批最大的功能风险源（AUD-11904/11906）；② 队列持久化文件的"形状容错"在 load 与恢复两条路径上都缺口（AUD-11905/11909/11913）；③ preflight 对"永不静默丢弃"章程执行良好，唯色彩检查自身可能被静默吞掉（AUD-11914）与序列音频提示缺口（AUD-11915）。GLSL·HDR 与 GPU 生命周期维度本批无涉（纯导出编排层），未发现相关问题。
