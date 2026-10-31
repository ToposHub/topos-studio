# 批次报告：batch-184 [P1] src/features/edit/core

- 任务书：docs/audit/2026-09-28/assignments/batch-184.md
- 编号区间：AUD-18400–AUD-18499（实际使用 AUD-18400–AUD-18411）
- 审查日期：2026-09-28
- 审查方式：全文通读（11,028 行，6 段 Read），交叉验证 src/shared/export/video_encoder.py、src/shared/media/frame.py、src/features/edit/core/timeline_v2.py、src/features/deliver/（只读）；AUD-18400 的行为已用项目 venv 实证复现。

---

## src/features/edit/core/timeline_export.py (行数: 11028)

### 发现

- [AUD-18400] [Critical] CPU 效果处理器硬编码 /255 归一与 uint8 输出——高位深（uint16）导出帧经 sharpen/denoise/edge_detect 后整帧饱和
  - 位置: src/features/edit/core/timeline_export.py:1212, 1232, 1530, 1590, 1373, 1491
  - 证据:
    ```python
    # _cpu_sharpen (1212, 1232)
    source = frame_bgr_u8.astype(np.float32) / 255.0
    ...
    return np.rint(np.clip(result, 0.0, 1.0) * 255.0).astype(np.uint8)
    # _cpu_denoise (1530, 1590)
    center = frame_bgr_u8[:, :, (2, 1, 0)].astype(np.float32) / 255.0
    ...
    bgr = np.rint(np.clip(result[:, :, ::-1], 0.0, 1.0) * 255.0).astype(np.uint8)
    # _cpu_edge_detect (1375-1376, 1491)
    scalar = (source[..., 2] * 0.2126 + source[..., 1] * 0.7152
              + source[..., 0] * 0.0722) / 255.0
    ...
    gray = np.rint(np.clip(output, 0.0, 1.0) * 255.0).astype(np.uint8)
    ```
    对照组 `_cpu_blur`/`_cpu_glow`/`_cpu_defocus` 均按 `np.iinfo(dtype).max` 缩放并保留输入 dtype，仅这三个处理器不感知位深。`_apply_clip_effects_cpu`（6936）在 `_composite_layers` 主循环中直接处理 `_use_high_precision_export=True` 时由 `_convert_yuv_planes_to_bgr16` 产出的 uint16 层帧。
  - 影响: 10/12/16-bit 交付（默认 codec 即 ProRes 422，pix_fmt yuv422p10le）且 clip 带 sharpen/denoise/edge_detect 效果时，输出帧完全错误。实证复现（项目 venv）：对 32768 中灰 uint16 帧执行，sharpen 输出全 255（全白）、denoise 输出全 255、edge_detect 输出全 0，且 dtype 退化为 uint8。属渲染错误级缺陷。
  - 建议: 三个处理器改为 `max_value = np.iinfo(dtype).max if integer else 1.0` 归一（同 `_cpu_glow` 范式），输出 `astype(frame.dtype)`；入口处对 dtype 做契约校验。
  - 置信度: 高（已复现）

- [AUD-18401] [Major] `_export_direct_topos` 异常路径未汇合音频渲染线程——线程跨导出存活并竞争共享音频状态
  - 位置: src/features/edit/core/timeline_export.py:5132-5153（对照：取消路径 5077 有 join；主路径异常 3028 有 join）
  - 证据:
    ```python
    except Exception as e:  # noqa: BLE001 — 与主路径同语义：结构化失败
        logger.exception("Direct TOPOS delivery failed")
        if source is not None:
            try:
                source.close()
            ...
        self._cleanup_clips()
        return dataclasses.replace(
            ExportOutcome.failed(
                error_code="EXPORT_FAILED", ...
    ```
    `_direct_audio_prepare`（3755-3762）启动的 `topos-direct-export-audio` 守护线程在此分支既未 join 也未清引用；主路径注释明确该清理是"避免泄漏到下一次导出"（3027）。
  - 影响: 直通导出失败后旧音频线程继续解码/混音；下一次导出期间它完成后写入 `self._audio_master_pcm` / `_audio_render_error`（3833-3844），可能把上一次（已取消范围）的 PCM 覆盖进新导出的音频，或注入过期错误——交付音频内容错误或误报失败。
  - 建议: 异常分支与取消分支一致，先 `self._join_audio_render_thread()` 再清理返回。
  - 置信度: 高（join 缺失为代码事实；竞争后果需时序但音频渲染耗时数秒至数分钟，窗口现实）

- [AUD-18402] [Major] 后台 manifest 写线程与后续导出共享可变状态竞态（individual-clips / 同路径连续导出）
  - 位置: src/features/edit/core/timeline_export.py:3089-3101, 3125, 3129-3134；3221-3237；2667-2669
  - 证据:
    ```python
    # 3089-3101（后台线程在 build 时读取 exporter 可变状态）
    manifest = build_export_manifest(
        self._project,
        self._sequence,
        config,
        qc_report=self._last_qc_report,
        preflight_result=self._last_preflight_result,
        loudness_report=self._last_loudness_report, ...
        effect_export_skips=tuple(self._effect_export_skips), ...)
    # 3125 cheap 同步写不持有 _manifest_lock；3129 后台写持锁
    self._write_success_manifest(config, hash_assets=False)
    ...
    with self._manifest_lock:
        self._delivery_finalized.wait(timeout=300.0)
    ```
    individual-clips 模式下子导出循环会 `self._sequence = item.sequence`（3228）、`export_timeline(child)` 开头 `setdefault("_effect_export_skips", []).clear()`（2667）并覆写 `_last_preflight_result`/`_last_qc_report`——均与上一个子导出被 `_delivery_finalized` 唤醒的 manifest 线程并发。
  - 影响: 交付审计 manifest 内容错配：clip N 的 sidecar 可能嵌入 clip N+1 的序列/preflight/QC/skips，或 manifest 路径上出现锁外的 cheap 写与持锁全量写交错（连续两次导出同一路径时最终内容不确定）。审计记录（P1-18 的目的）不可信。
  - 建议: manifest 线程入口一次性快照全部所需状态（sequence/报告/skips 随 config 一起捕获）；cheap 写同样纳入 `_manifest_lock`。
  - 置信度: 高（结构成立；individual-clips 下窗口为数秒级哈希期，现实可触发）

- [AUD-18403] [Major] CPU 转场双源预合成丢弃 clip 效果链/crop/transform/frame-blend——转场期间处理静默丢失
  - 位置: src/features/edit/core/timeline_export.py:6769-6800（转场侧）对照 6919-6995（主循环）；Metal 侧 7730-7774 支持这些属性
  - 证据:
    ```python
    # 转场侧只做这四步（6774-6784）：
    left_frame = self._apply_video_denoise_export(left_frame, left_layer)
    left_frame = self._apply_color_grade(left_frame, left_layer)
    left_frame = self._resize_frame(left_frame, width, height)
    if trans.transition_type in TRANSITION_EFFECT_TYPES:
        left_frame = apply_transition_effect(...)
    # 主循环同一层却额外做（6927-6937）：
    if not crop_is_identity(crop):
        layer_frame = apply_crop_cpu(layer_frame, crop)
    ...
    layer_frame = self._apply_clip_effects_cpu(layer_frame, layer, frame_idx)
    ```
    wipe 双侧 clip 被加入 `wipe_skip_ids` 后在主循环 `continue`（6852-6870），效果/几何只依赖预合成结果；Metal 路径则通过 `side_properties` 显式下发 effects/transform/crop（7739-7774）。
  - 影响: CPU 合成导出中，任何参与空间/全画面转场的 clip 在转场区间内丢失 blur/glow/sharpen 等效果与裁剪/几何/帧混合变速——预览与非转场帧均正常，转场帧内容错误；且 CPU/Metal 两后端输出不一致。preflight 不拦截（效果本身可导出）。
  - 建议: 预合成前对两侧补齐 `_apply_clip_effects_cpu`/crop/transform/frame-blend（与主循环同序），或转场层整体回落到能处理完整链的路径。
  - 置信度: 高（代码路径对照自明）

- [AUD-18404] [Major] PNG/TIFF 图像序列通过配置校验但无编码器——必然运行时失败
  - 位置: src/features/edit/core/timeline_export.py:553-558, 4685-4687, 4739-4812；UI 暴露点 src/features/deliver/ui/deliver_page.py:358
  - 证据:
    ```python
    # validate() 553-558：图像序列按目录/图像扩展名放行
    if self.codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE,
                      ExportCodec.TOPOS_IMAGE_SEQUENCE):
        if not os.path.isdir(self.output_path) and ext not in ('', '.png', '.tiff', '.tif'):
            errors.append(f"Image sequence output requires directory or image extension")
    # _get_codec_settings 4685-4687：PNG/TIFF 被映射为 ProRes
    if config.codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE):
        return ("prores", "yuv422p10le", "hq")
    ```
    `_create_encoder` 仅特判 `topos_image_sequence`/`gif`/`topos`（4784-4793），ProRes 走 `FFmpegVideoEncoder(output_path=目录)`，`open()` 必失败。Deliver 页已把 "PNG Sequence" 加入编码器下拉（deliver_page.py:358），deliver 作业校验同样放行（video_export_job.py:562）。
  - 影响: 用户可选、双重校验通过的配置在导出中期才以混淆的错误（对目录打开 ProRes 编码器）失败；TOPOS_IMAGE_SEQUENCE 是唯一有实现（ToposImageSequenceEncoder）的序列格式。
  - 建议: 在 `validate()`/deliver 层对 PNG/TIFF 序列显式报"暂不支持"，或实现对应序列编码器；映射注释也需同步。
  - 置信度: 高

- [AUD-18405] [Major] CUSTOM 帧范围验证缺口：负值 start_frame 产出前导黑帧；start ≥ 时长产出 0 帧文件且按成功交付
  - 位置: src/features/edit/core/timeline_export.py:514-516, 3700-3704, 2799
  - 证据:
    ```python
    # validate() 514-516：只校验 end>=start，不校验 start>=0
    if self.range_type == ExportRangeType.CUSTOM:
        if self.end_frame >= 0 and self.end_frame < self.start_frame:
            errors.append("end_frame must be >= start_frame")
    # _calculate_frame_range 3700-3704：
    start = config.start_frame
    end = config.end_frame if config.end_frame >= 0 else duration
    return (start, min(end, duration))
    ```
    start=-50 时 `range(-50, end)` 渲染 -50.. 的无内容帧（黑帧）；start=1000、duration=100 时返回 (1000,100)，`_total_frames` 为负、循环为空，编码器 close 出 0 帧文件后仍返回 `ExportOutcome.ok` 并写成功 manifest（3016-3023）。
  - 影响: 边界配置静默产出内容错误的交付物（多余黑帧）或空文件被标为成功——与"不得静默产出不可信文件"的项目原则冲突；进度百分比还会为负（`_current_frame/_total_frames`）。
  - 建议: validate 增加 `start_frame < 0` 拒绝；`_calculate_frame_range` 后检查 `end <= start` 时显式失败（结构化 error_code），而非空循环成功。
  - 置信度: 高

- [AUD-18406] [Major] native VT bridge 以 `int(round(fps))` 传递帧率，非整数帧率时间线音画时长漂移
  - 位置: src/features/edit/core/timeline_export.py:4871（bridge 侧 `self.fps = max(1, int(fps))`，gpu_export_bridge.py:50）
  - 证据:
    ```python
    bridge = VideoToolboxGpuExportBridge(
        metal_context,
        output_path,
        width=int(getattr(config, "width", 0)),
        height=int(getattr(config, "height", 0)),
        fps=max(1, int(round(float(getattr(config, "fps", 25.0))))),   # 24000/1001 → 24
        codec=codec,
        pixel_format=bridge_pixel_format,
    )
    ```
    `FFmpegEncoderConfig.fps` 是精确 Rational（4513-4520 专门为 A/V 同步保留）；`can_use_native_gpu_bridge`（2133-2282）的 eligibility 检查不含 fps 匹配。音频由 `_direct_audio_prepare` 按时间线精确帧率渲染。
  - 影响: 开启 `TOPOS_EXPORT_NATIVE_VT=1` 导出 23.976/29.97 等时间线时，视频轨道以取整帧率封装（时长偏短 ~0.1%），与按原帧率渲染的音频产生随片长累积的漂移。（该路径默认关闭，env 显式开启才暴露。）
  - 建议: bridge 支持分数 timescale（num/den），或在 eligibility 中对非整数 fps 拒绝并记录 reason_code。
  - 置信度: 中（取整为代码事实；漂移量取决于 bridge 内部 timescale 用法）

- [AUD-18407] [Major] `_gpu_yuv422p10_output_allowed` 一刀切拒绝默认 `burn_subtitles=True`——TOPOS 10-bit Metal 原生 YUV 直出在默认配置下永不生效
  - 位置: src/features/edit/core/timeline_export.py:7187-7189（对照 yuv420 门已修的逐帧判据 7141-7146）
  - 证据:
    ```python
    # _gpu_yuv422p10_output_allowed 7187-7189：
    if (width <= 0 or height <= 0 or width % 2
            or bool(getattr(config, "burn_subtitles", False))   # 默认 True → 恒 False
            or bool(getattr(self, "_export_has_alpha", False))):
        return False
    # 对照 _gpu_yuv420_output_allowed 7141-7146（2026-09-17 已改逐帧惰性判据）：
    if bool(getattr(config, "burn_subtitles", False)):
        try:
            if tuple(self._sequence.get_active_subtitle_clips(frame_idx)):
                return False
    ```
    `burn_subtitles` 默认 True（486-487），即便时间线没有任何字幕轨，422p10 门也直接返回 False。
  - 影响: 默认配置下所有 TOPOS yuv422p10le 导出在 Metal 后端永远走 BGR uint16 回读 + 编码侧 swscale 转换，白付每帧全帧 GPU→CPU 拷贝与色彩转换；yuv420 门同类问题已被认定为缺陷修复（注释自述"此前一刀切导致……白付 BGR 回读"），此处未同步。
  - 建议: 复用与 yuv420 门相同的逐帧活跃字幕判据（含 frame_idx 参数）。
  - 置信度: 高（行为确定；归类为性能缺陷而非画质错误）

- [AUD-18408] [Major] `_get_image_frame` 每帧完整磁盘 imread 无缓存——静态图层导出按帧数重复解码
  - 位置: src/features/edit/core/timeline_export.py:10066-10090（消费点：CPU 主循环 6873/7664；GPU `_resolve_layer_source` 7664，OpenGL 无 per-frame 缓存，metal 缓存仅限单次合成调用内 7595）
  - 证据:
    ```python
    def _get_image_frame(self, asset_path: str) -> Optional["np.ndarray"]:
        try:
            import cv2
            import numpy as np
            ...
            if getattr(self, "_export_has_alpha", False):
                img = cv2.imread(asset_path, cv2.IMREAD_UNCHANGED)
            ...
            img = cv2.imread(asset_path, cv2.IMREAD_COLOR)
    ```
    同一 `asset_path` 在每个导出帧被重复读取+解码（转场预合成路径 6769-6770 每帧还会取左右两帧）。comp-clip 已有磁盘帧缓存（10554-10573）可对照。
  - 影响: 含静图层（片头图、字幕底图、图文板）的长导出为同一张图做数千次磁盘 IO+解码：4K PNG 单次 ~10-30ms，10 分钟 24fps 时间线即数千次重复，显著拉低导出吞吐（项目基准为 4K 实时性）。
  - 建议: 导出器级按 `asset_path (+alpha 模式)` 的 LRU 帧缓存（与 `_open_clips` 同生命周期，`_cleanup_clips` 释放）。
  - 置信度: 中（重复调用为代码事实；绝对耗时依赖图片尺寸与存储）

- [AUD-18409] [Major] 效果链/调色运行期失败被宽泛 except 吞掉后帧保持未处理继续交付——违反"不得静默缺效果"原则
  - 位置: src/features/edit/core/timeline_export.py:8943-8951（`_apply_clip_effects_cpu`）；9082-9086（`_apply_color_grade`）
  - 证据:
    ```python
    try:
        result = handler(result, params)
    except Exception as exc:  # noqa: BLE001 — one bad effect mustn't kill export
        logger.warning(
            "TimelineExporter: CPU effect '%s' failed (%s); "
            "frame left unfiltered", effect_type, exc,
        )
    ...
    except Exception as error:
        logger.warning(
            f"Export color grade failed for clip {layer.clip_id}: {error}"
        )
        return frame_data          # 未调色帧直接进入交付
    ```
    `_effect_export_skips`（manifest degradations 的数据源，8934-8941）只登记"无 CPU handler 的效果类型"，运行期 handler 异常不进任何交付记录；preflight 只保证效果存在后端，不保证执行成功。`.clinerules` 明令"不得宽泛 except 掩盖领域错误"。
  - 影响: 参数越界/数据异常导致效果或整段调色在交付物中静默缺失，仅留运行日志——与本项目 FX-M05/preflight 确立的"绝不静默丢效果"交付契约直接矛盾，且 manifest 审计不完整。
  - 建议: 运行期失败至少写入 `_effect_export_skips`（携带原因）进入 manifest degradations；调色失败建议 fail-fast（预览已可见的调色在交付中缺失属内容错误）。
  - 置信度: 中

- [AUD-18410] [Suggestion] 类型注解缺失/弱类型标注
  - 位置: src/features/edit/core/timeline_export.py:1746（`expand_sequence_audio_for_render(sequence, sequence_resolver, project)` 参数与返回值全裸）、10518（`_get_comp_frame_renderer(self)`）、10655（`_segment_cache_config(self)`）、10710/10768/10786（`_comp_frame_cache_key/get/put` 的 cache/key 参数无类型）、2472-2473 与 4278（`-> "list"` 弱返回类型）、282（`_IndividualClipExport.sequence: Any`）
  - 证据:
    ```python
    def expand_sequence_audio_for_render(sequence, sequence_resolver, project):
        ...
    def _get_comp_frame_renderer(self):
        """Lazy-initialize and return the CompositeFrameRenderer."""
    ```
  - 影响: 与模块自述"完整类型注解"的设计原则不符；sequence/cache 等接口需读者跳转确认。
  - 建议: 补齐 `TimelineSequence`/`EditProject`/`DiskRenderCache`/`SegmentRenderKey` 等具体类型（TYPE_CHECKING 块已具备大部分引用）。
  - 置信度: 高

- [AUD-18411] [Suggestion] 取消标志合并存在覆盖窗口——极窄竞态可吞掉一次取消请求
  - 位置: src/features/edit/core/timeline_export.py:2786-2792（对照 `cancel_export` 3580-3591）
  - 证据:
    ```python
    self._is_exporting = True
    # Preserve a cancel request that arrived in the small window between
    # worker creation and export_timeline(). ...
    self._cancel_requested = self._cancel_pending_before_start   # 直接赋值
    self._cancel_pending_before_start = False
    ```
    `cancel_export` 在 `_is_exporting=True`（2787）之后、2791 之前到达时直接置 `_cancel_requested=True`（不经 pending），随后 2791 用 pending(False) 覆盖，取消请求丢失。
  - 影响: 该窗口仅数条赋值语句，概率极低，但一旦发生导出忽略取消继续完整渲染/写盘。
  - 建议: 改为 `self._cancel_requested = self._cancel_requested or self._cancel_pending_before_start`。
  - 置信度: 中（逻辑成立，触发概率极低）

### 通过项

- 线程安全（音频/渲染/编码并行结构）：音频渲染只产 PCM、容器写入保持单线程；解码∥编码流水线的有界队列 + stop/done 收尾无互相等待路径；`_open_clips`/prefetch 注册表仅由渲染线程变更；manifest 写线程有登记/排空（atexit）机制。
- GPU 资源生命周期：GPU 合成渲染器在创建线程释放（串行=导出线程 finally、流水线=生产者 finally，幂等）；降噪桥 headless context 导出结束释放；`metal_gpu_frames` 在 finally 统一安全释放；GpuEncodeSurface 提交失败路径 release 并记录 fallback；bridge 初始化失败清理半成品并整体回退 CPU 后端。
- GLSL·HDR 正确性（合成链）：`_normalise_bgr_frame` 浮点输入不再无条件 clamp [0,1]（HDR 高光保留）；`_quantize_export_frame` 对 >1 值先显式 OETF（PQ/HLG）、SDR 边界一次性警告后可见裁剪；YUV 域 10→8 降位锚点精确整除、无 RGB 往返；alpha 归一化按 dtype 满刻度（修复硬编码 /255 的历史问题已到位）。
- 信号防递归：本模块无 Qt 信号连接，进度通过回调单向通知，无递归风险。
- 取消/暂停路径（主流程）：视频循环、音频渲染、直通串行/流水线、individual-clips 循环均检查取消；取消优先 `encoder.abort()` 不留伪成功文件；暂停用 Event 等待不烧 CPU，取消会唤醒暂停。
- 异常路径（主 dispatch）：异常路径汇合音频线程、abort 编码器、清理 clip 会话，失败以结构化 `ExportOutcome.failed`（错误码+类型链）返回，与用户取消显式区分。
- 输入验证（配置层）：codec/stems 声明/render_mode/filename 域/GIF 帧率与色数/分辨率/fps/扩展名匹配等校验完整；音频 DSP、视频效果、插件身份、字体、字幕图形绑定、音频路由图、交付 QC 六类 preflight 均 fail-closed，能力目录不可用时阻断而非放行。
- 资源清理：`_cleanup_clips` 全路径调用；导出解码会话 LRU 上限（防内存失控）；首开失败重试一次的陈旧 codec 库规避有据。
- 直通交付门（`_try_render_planar_passthrough`）：源格式/位深/siting/SAR/色彩元数据/平面几何/旋转/场序/代理逐项 fail-closed，白名单外一律回落合成路径。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-18400 |
| Major | 9 | AUD-18401–18409 |
| Suggestion | 2 | AUD-18410–18411 |
