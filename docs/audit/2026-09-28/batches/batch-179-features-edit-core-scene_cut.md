# 批次报告：batch-179 [P1] src/features/edit/core/scene_cut/

- 编号区间：AUD-17900–AUD-17999
- 审查文件：detector.py / filters.py / frame_sampler.py（共 3 个，2,647 行，全部 Read 通读）
- 交叉验证：models.py（SceneCutSettings/SceneCutMetrics/SceneCutCandidate/SourceFrameTimeIndex）、cache.py（MediaFingerprint/to_hash/缓存校验）、metrics.py（calculate 签名）、worker.py（取消与异常路径）
- 汇总：Critical 0 / Major 7 / Suggestion 11

---

## src/features/edit/core/scene_cut/detector.py (行数: 684)

### 发现

- [AUD-17901] [Major] 取消令牌抛出的 CancelledError 被宽泛 except 吞掉，取消被误报为 ERROR 相位
  - 位置: src/features/edit/core/scene_cut/detector.py:477
  - 证据:
    ```python
    for sampled_frame in self._sampler.sample_frames():   # 360
        if cancel_token.is_cancelled():                    # 362（生成器自身会先 check_and_raise）
    ...
    except Exception as e:                                 # 477
        logger.error(f"Scene cut detection error: {e}")
        diagnostics["error"] = str(e)
        report_progress(DetectionPhase.ERROR, 0, 0, f"Detection error: {e}")
    ```
    frame_sampler.py:466 的 `self._cancel_token.check_and_raise()` 抛出的 CancelledError 继承自 Exception（frame_sampler.py:92-94），在生成器下一次求值时直接进入此 except；worker.py:722 的 `except CancelledError` 分支因此不可达。
  - 影响: 用户取消时 progress_callback 收到 `DetectionPhase.ERROR`（"Detection error: Task cancelled"），diagnostics["cancelled"] 保持 False、diagnostics["error"]="Task cancelled"；UI 先闪错误状态再由 worker 兜底发 cancelled 信号，取消与失败状态混叠。
  - 建议: 在 `except Exception` 之前单独 `except CancelledError:` —— 置 diagnostics["cancelled"]=True、上报 CANCELLED 相位并按取消路径返回（或重新抛出，交由 worker 的既有 CancelledError 分支处理）。
  - 置信度: 高

- [AUD-17902] [Major] 检测失败被转换为"成功完成的空结果"，违背自身 Raises 契约并掩盖领域错误
  - 位置: src/features/edit/core/scene_cut/detector.py:477-493（契约声明在 270-272）
  - 证据:
    ```python
    Raises:
        RuntimeError: If detection fails        # 271-272 文档字符串
    ...
    except Exception as e:                       # 477
        ...
        return self._create_analysis_from_timepoints(   # 489
            time_points, [], diagnostics,
        )
    ```
    worker.py:716-719 对返回值只判 `is_cancelled()`，随后 `completed.emit(analysis)`。
  - 影响: 解码失败（容器损坏、解码器错误等）时不会触发 worker 的 error 信号，而是发出 completed 并显示"完成、0 个切点"；用户与上层无法区分"无切点"与"检测失败"。违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: detect() 失败时按文档契约 raise（worker 已有 `except Exception → error.emit(classify_error(e))` 完整通道）；若必须返回结果，则在 SceneCutAnalysis/worker 间增加显式 error 状态判定，避免空结果被当成功。
  - 置信度: 高

- [AUD-17903] [Major] SceneCutDetector.cancel() 无法取消进行中的检测
  - 位置: src/features/edit/core/scene_cut/detector.py:589-592
  - 证据:
    ```python
    def cancel(self) -> None:
        """Request cancellation of detection."""
        self._cancelled = True
    ```
    detect() 的采样循环只检查 `cancel_token.is_cancelled()`（362 行）；`self._cancelled` 仅在 token 已取消后作为标志读取（370、403 行），没有任何路径读取 cancel() 单独设置的值。
  - 影响: 公共取消 API 名存实亡：单独调用 detector.cancel() 对运行中的检测毫无作用。当前 worker.py:636 走 token.cancel() 未踩中，属潜伏 API 缺陷。
  - 建议: 在 detect() 中保存 cancel_token 引用，cancel() 转发 `self._cancel_token.cancel()`；或直接删除该误导性方法。
  - 置信度: 高

- [AUD-17904] [Major] "流式管线"仍把全部采样帧像素数据留在内存，长素材内存 O(N×~108KB)
  - 位置: src/features/edit/core/scene_cut/detector.py:339-342, 382-386
  - 证据:
    ```python
    # Streaming pipeline: only keep time points and metrics entries,
    # not full frame data (reduces memory for long footage)   # 339-340
    ...
    metrics_entries.append(FrameMetricsEntry(
        frame=sampled_frame,      # 384 —— SampledFrame.frame_data 为 256x144x3 uint8 ≈108KB/帧
        metrics=metrics,
        frame_index=frame_index,
    ))
    ```
    filters.py 只消费 `entry.metrics` 与 `entry.frame.source_frame/pts/seconds`，frame_data 下游无人使用。
  - 影响: 注释宣称的内存优化未实现：1 小时 25fps 素材 ≈ 9 万帧 × 108KB ≈ 9.3GB 常驻，长片检测易 OOM 或被系统压缩拖慢；与"4K 实时/长素材"目标冲突。
  - 建议: metrics 计算完成后仅保留 time_point + metrics（在构造 FrameMetricsEntry 前剥离 frame_data，或为管线定义只含轻量字段的 entry 类型），prev_frame 仍持上一帧用于差分。
  - 置信度: 高

- [AUD-17905] [Suggestion] 死代码：calculate_media_fingerprint/_create_analysis 无人调用，phase 参数被忽略
  - 位置: src/features/edit/core/scene_cut/detector.py:125-142, 495-504, 506-514
  - 证据: 全库 grep 仅命中定义处；`_create_analysis_from_timepoints(..., phase=...)` 直接调用 `_build_analysis(...)`，phase 从未传入；calculate_media_fingerprint 的格式 `f"{source_path}:{file_size}:{mtime_ns}"` 与 cache.py MediaFingerprint.to_hash() 的 `sha256("path|size|mtime_ns|asset_id")[:16]` 不一致（detector.py:539-554 注释明确要求匹配后者）。
  - 影响: 误导后续维护者；若未来误用该 helper 做缓存指纹将导致缓存永不命中/误命中。
  - 建议: 删除两处死代码与无用 phase 参数。
  - 置信度: 高

- [AUD-17906] [Suggestion] SourceFrameTimeIndex.media_fingerprint 恒为空串，注释声称的回填从未发生
  - 位置: src/features/edit/core/scene_cut/detector.py:529-534
  - 证据:
    ```python
    time_index = SourceFrameTimeIndex(
        asset_id=self._asset_id,
        media_fingerprint="",  # Set after fingerprint is calculated   # 531
        points=time_points_tuple,
        average_fps=source_fps,
    )
    ```
    随后 540-569 计算了 `media_fingerprint` 只赋给 analysis，未回填 time_index。
  - 影响: 时间索引自带媒体校验钩子失效；当前 timeline_mapping.py 仅消费 points 做映射，暂无功能性后果，但媒体更换后无法用该字段验证索引失效。
  - 建议: 计算指纹后将 time_index 替换为带真实 fingerprint 的实例（dataclass replace）。
  - 置信度: 高

- [AUD-17907] [Suggestion] 子范围检测时进度分母错位，百分比严重偏低
  - 位置: src/features/edit/core/scene_cut/detector.py:354, 394-400
  - 证据:
    ```python
    report_progress(DetectionPhase.SAMPLING, 0, self._sampler.total_frames, ...)  # 354
    ...
    report_progress(DetectionPhase.SAMPLING, frame_index, self._sampler.total_frames, ...)  # 395-399
    ```
    frame_index 从 0 计（采样范围内的序号），分母却是整片 total_frames。
  - 影响: settings.start_source_frame>0 或 end_source_frame 截断时，进度长期低位徘徊后在完成时突跳，剩余时间估算（estimated_remaining）随之失真。
  - 建议: 分母改用实际范围 `end - start`（可在 diagnostics 中已有 total_frames 之外记录采样范围）。
  - 置信度: 高

### 通过项
- 线程安全：纯 Python 无 PyQt/OpenGL 依赖，组件均为 detect() 调用内局部对象，无共享可变状态跨线程
- GPU 资源生命周期：不涉及 ModernGL/GLSL 资源
- 信号防递归：不涉及 Qt 信号
- 输入验证：SceneCutSettings 为 frozen dataclass 且 __post_init__ 校验范围（models.py:108-135）；proxy 路径使用前有 os.path.exists 检查（324-328）
- 指纹异常路径：os.stat 失败回退空指纹/proxy-missing，不会让检测崩溃（543-569）
- 类型注解：公共 API 与返回值注解完整
- 状态/持久化：analysis 构造字段与 models.py 定义一致（含 proxy_fingerprint、detector_version），缓存键由 models.to_cache_key() 统一生成

---

## src/features/edit/core/scene_cut/filters.py (行数: 993)

### 发现

- [AUD-17910] [Major] 自适应阈值实际仅反映"片尾最后 30 帧"统计，而非逐帧滚动，全片共用单一阈值
  - 位置: src/features/edit/core/scene_cut/filters.py:310-321（配合 134-144 的窗口裁剪）
  - 证据:
    ```python
    if self._settings.adaptive_threshold:                    # 311
        for entry in entries:                                # 312
            self._rolling_stats.update(entry.metrics.combined_score)  # 313
    ...
    if self._settings.adaptive_threshold and len(entries) >= 5:      # 317
        threshold = self._rolling_stats.get_threshold(self._settings.sensitivity)  # 318
    ```
    RollingStats.update 以 window_size=30 的滑窗只保留最后 30 个值（136-138），get_threshold 基于该最终窗口求 median+k*MAD，得到的单一 threshold 随后应用于第二遍全片所有帧（327 起）。
  - 影响: 与类/函数文档的 "Rolling median/MAD dynamic threshold / adaptive" 设计不符：阈值由片尾 30 帧决定——片尾运动密集则阈值抬高导致前段漏检，片尾安静则阈值过低导致前段误检；对运动分布不均的长素材切点质量明显劣化。
  - 建议: 在第二遍主循环中逐帧 `update(上一帧 score)` 后 `get_threshold(...)` 再比较（避免当前帧参与自身阈值），或分块计算局部阈值。
  - 置信度: 高

- [AUD-17911] [Suggestion] process() 重置 state 与 rolling_stats 但不清 diagnostics，复用时计数器跨次累积
  - 位置: src/features/edit/core/scene_cut/filters.py:260-269, 301-303
  - 证据:
    ```python
    self._state = DetectionState()                                    # 302
    self._rolling_stats = RollingStats(window_size=self._settings.rolling_window_size)  # 303
    ```
    `self._diagnostics` 只在 __init__ 初始化（260-269），process() 内的 `flash_suppressed_count` 等均为 `+=` 累加。
  - 影响: 同一 pipeline 实例二次 process 时诊断数据翻倍失真。当前 detector.py:337 每次 detect 新建 pipeline，影响受限。
  - 建议: process() 开头重建 diagnostics 字典。
  - 置信度: 高

- [AUD-17912] [Suggestion] FLASH 分类计入 flash_suppressed_count 但候选并未被抑制；guard 间 kind 覆盖不清理 reasons
  - 位置: src/features/edit/core/scene_cut/filters.py:443-446
  - 证据:
    ```python
    if is_likely_flash:
        reasons.append("flash_like")                  # 444
        kind = SceneCutKind.FLASH                     # 445
        self._diagnostics["flash_suppressed_count"] += 1   # 446 —— 候选仍会返回
    ```
    且后续 FAST_MOTION/BLACK_GAP/SOFT_TRANSITION 分支可覆盖 kind 而保留旧 reasons。
  - 影响: 诊断计数语义失真（"suppressed" 与 "classified" 混计）；reasons 与最终 kind 可能矛盾，影响 confidence 惩罚（calculate_confidence 按 reasons 扣减）。
  - 建议: 分类与抑制分别计数；kind 被后续 guard 覆盖时同步调整 reasons。
  - 置信度: 高

- [AUD-17913] [Suggestion] motion_frames/black_frames 无界增长且逐候选全表扫描，长素材 O(N²) 风险
  - 位置: src/features/edit/core/scene_cut/filters.py:340-345, 449-456, 465-470
  - 证据:
    ```python
    if metrics.motion_score >= self._settings.motion_score_threshold:   # 344
        self._state.motion_frames.append(frame_index)                   # 345
    ...
    recent_motion = [f for f in self._state.motion_frames               # 451-453
                     if abs(f - frame_index) <= 5]
    ...
    near_black = any(abs(f - frame_index) <= 3 for f in self._state.black_frames)  # 467-469
    ```
    两个列表仅在 register_cut（cut 被接受）时清理（208-210）。
  - 影响: 高运动/多黑场素材中列表长度趋近帧数，每个超阈值帧做 O(N) 扫描，最坏 O(N²)；对小时级素材是明确性能风险。
  - 建议: 用 deque 按时间窗（如 ±5/±3 帧）裁剪，或维护有序帧号集合二分查找。
  - 置信度: 高

- [AUD-17914] [Suggestion] min_scene_distance 原地排序修改调用方列表；black_gap_detector 默认阈值与管线设置不一致
  - 位置: src/features/edit/core/scene_cut/filters.py:781, 893
  - 证据:
    ```python
    candidates.sort(key=lambda c: c.source_frame)      # 781 —— 直接排序入参
    ...
    def black_gap_detector(entries, black_threshold: float = 0.95, ...):  # 893
    ```
    管线内黑帧判定用 `self._settings.black_score_threshold`（默认 0.7，models.py:103）；transition_guard 做了 `candidates = list(candidates)` 拷贝（852），本函数没有。
  - 影响: 调用方列表顺序被隐式改变；同一"黑场"概念在独立函数与管线中阈值相差 0.25，结果不可比。
  - 建议: min_scene_distance 先拷贝；独立函数默认值改为对齐 settings 或显式注明差异。
  - 置信度: 高

- [AUD-17915] [Suggestion] 未使用导入与变量：math、FrameTimePoint、consecutive、next_frame
  - 位置: src/features/edit/core/scene_cut/filters.py:29, 39, 579, 637
  - 证据:
    ```python
    import math                    # 29 —— 文件内无 math. 调用
    FrameTimePoint,                # 39 —— 仅出现在 import
    ...
    consecutive = 0                # 579 —— 累加但从未被读取
    next_frame = f + 1             # 637 —— 赋值后未使用
    ```
  - 影响: 死代码噪声，掩盖真实逻辑意图（dissolve 检测疑似本想用 consecutive 参与判定）。
  - 建议: 清理导入；确认 dissolve 判定是否遗漏了 consecutive 条件后移除或接线。
  - 置信度: 高

### 通过项
- 线程安全：纯数据处理无共享状态、无 Qt/GL 调用
- GPU 资源生命周期：不涉及
- 信号防递归：不涉及
- calculate_confidence 边界处理正确（最终 clamp 到 [0,1]，92 行）
- 黑场进/出双向处理自洽：entry 抑制 + exit 重分类为 BLACK_GAP（625-673）
- flash bounce 后处理保序且保留高分候选（678-700）
- 类型注解完整（含内部 dataclass 与回调签名）
- RollingStats 窗口尺寸来自 settings 并有 <5 样本回退（158-160）

---

## src/features/edit/core/scene_cut/frame_sampler.py (行数: 970)

### 发现

- [AUD-17920] [Major] start>0 时 seek 丢弃起始帧且后续帧 source_frame 整体错位一格
  - 位置: src/features/edit/core/scene_cut/frame_sampler.py:431-432, 793-805（sample_frames 469-471 / build_time_index 698-700 同受影响）
  - 证据:
    ```python
    if start > 0 and self._current_frame != start:     # 431
        self._seek_to_frame(start)                     # 432
    ...
    # _seek_to_frame 内部：
    for frame in self._frame_iterator:                 # 793
        ...
        if current_frame >= frame_index:               # 801
            self._current_frame = current_frame        # 802
            return                                     # 803 —— 目标帧已从迭代器取出并被丢弃
    ```
    返回 sample_frames 后首个迭代帧实为源帧 start+1，而 `_extract_time_point` 用 `self._current_frame`（=start）标注（855），skip 分支 `if self._current_frame < start: continue`（469）无法补偿。对比 sample_single_frame（574-587）在命中 `current_frame >= frame_index` 时转换并返回该帧，可佐证丢弃行为是缺陷。
  - 影响: 子范围分析（settings.start_source_frame>0 或 clip 范围检测）时起始帧缺失，且所有采样帧的 source_frame/seconds 标签整体前移 1 帧 → 切点帧位偏移、时间索引（build_time_index 同路径）错位，VFR 映射随之失真。
  - 建议: _seek_to_frame 解码到 `current_frame >= frame_index - 1` 即停（保留目标帧供后续迭代），或改为返回"已取出但未消费"的帧由 sample_frames 首轮处理。
  - 置信度: 高（纯逻辑推演，未运行验证； CFR 假设下结论确定）

- [AUD-17921] [Major] _convert_frame_data 转换失败静默回退全黑帧，错误像素数据进入检测管线
  - 位置: src/features/edit/core/scene_cut/frame_sampler.py:882-897
  - 证据:
    ```python
    try:
        small = frame.reformat(width=..., height=...)
        frame_bgr = small.reformat(format="bgr24").to_ndarray()
    except Exception as e:
        logger.warning(f"Frame conversion error: {e}")
        frame_bgr = np.zeros((self._target_height, self._target_width, 3),
                              dtype=np.uint8)           # 892-895
    return frame_bgr
    ```
  - 影响: 异常像素格式/解码问题被转化为合法黑帧：连续失败时 black_score 升高可产生伪 BLACK_GAP 候选或掩盖真实切点；仅有 warning 日志，无失败计数、无上限熔断，检测结果悄悄劣化。
  - 建议: 记录连续失败计数并在超阈值时抛出（走检测错误通道），至少把失败帧数写入 diagnostics 供上层判定。
  - 置信度: 中

- [AUD-17922] [Suggestion] sample_frames 中 `except StopIteration: pass` 为死分支且可能吞掉循环体异常
  - 位置: src/features/edit/core/scene_cut/frame_sampler.py:506-508
  - 证据:
    ```python
        except CancelledError:
            logger.info(...); raise
        except StopIteration:
            # 视频结束
            pass
    ```
    for 循环自身消化迭代器 StopIteration；该分支只可能捕获循环体（如 progress_callback、executor.submit）抛出的 StopIteration 并误判为"正常结束"。
  - 影响: 无功能收益，且掩盖循环体真实异常（PEP 479 场景）。
  - 建议: 删除该分支。
  - 置信度: 高

- [AUD-17923] [Suggestion] sample_single_frame 宽泛 except 把解码/seek 真实错误吞为"帧不存在"，且成功/失败路径留下不一致的游程状态
  - 位置: src/features/edit/core/scene_cut/frame_sampler.py:565-593
  - 证据:
    ```python
    except Exception as e:
        logger.warning(f"Single-frame seek failed at {frame_index}: {e}")
        self._frame_iterator = None
        self._current_frame = 0
    return None
    ```
    成功路径则保留已消耗至目标帧的迭代器并把 _current_frame 置为 frame_index+1（583-586）。
  - 影响: 真实解码错误与"帧不存在"不可区分；_frame_iterator/_current_frame 的跨方法耦合状态在两条路径下语义不同，后续 sample_next_frame 的 expected_frame 匹配易失效。
  - 建议: 至少区分错误类型（记录并向上传播解码错误），并统一迭代器/游标的重置策略。
  - 置信度: 高

- [AUD-17924] [Suggestion] 类型注解瑕疵：`Optional[Any]` 无意义、__exit__ 参数无注解，与文件头"完整类型注解"声明不符
  - 位置: src/features/edit/core/scene_cut/frame_sampler.py:521-522, 356-358
  - 证据:
    ```python
    def sample_single_frame(
        self,
        frame_index: int,
        time_index: Optional[Any] = None,     # Any 已包含 None
    ...
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:   # 参数无注解
    ```
  - 影响: 类型检查收益归零；建议改为 `SourceFrameTimeIndex | None`（模块内可用 TYPE_CHECKING 前向引用）并补 __exit__ 注解。
  - 置信度: 高

### 通过项
- 线程池生命周期正确：finally 中对未完成 future cancel 并 `executor.shutdown(wait=False, cancel_futures=True)`（512-516），有界预读队列 CONVERT_QUEUE_DEPTH=32 防内存无界（491-493）
- 取消路径：解码循环每帧 `check_and_raise()`，CancelledError 正确向上传播并记录（463-466, 503-505）；build_time_index 同样检查（696）
- 资源释放：close() 幂等、container.close 异常兜底并置空引用（335-349）；上下文管理器完整（351-358）
- open() 输入验证：文件不存在抛 FileNotFoundError、无视频流抛 ValueError、PyAV 缺失抛 RuntimeError，异常均带原因链不吞错（267-333）
- fps/时间基/总帧数元数据提取有 average_rate→base_rate→24.0 兜底与 duration 估算（292-313）
- 缩放尺寸计算保证 height>=1、保持宽高比（362-383）
- 转换流水线 planar-scale-first 设计与实测注释一致，转换在释放 GIL 的线程池执行，progress 回调按 100 帧节流（439-448, 862-897）
- 工厂函数 sample_frames_for_detection/build_time_index_for_source 正确用 with 管理生命周期（943-970）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 7 | AUD-17901, AUD-17902, AUD-17903, AUD-17904, AUD-17910, AUD-17920, AUD-17921 |
| Suggestion | 11 | AUD-17905, AUD-17906, AUD-17907, AUD-17911, AUD-17912, AUD-17913, AUD-17914, AUD-17915, AUD-17922, AUD-17923, AUD-17924 |

主题性结论：本批次不涉及 UI 线程/GL 资源/Qt 信号（纯后台检测核心），风险集中在——(1) detector 的异常/取消路径把失败与取消伪装成"完成的空结果"；(2) 名为流式的管线实际全量驻留帧像素；(3) 采样器子范围 seek 的 off-by-one 帧位错标；(4) 自适应阈值退化为"片尾窗口阈值"。
