# 批次报告：batch-178 — src/features/edit/core/scene_cut（apply_ops / cache / __init__）

- 审查日期：2026-09-28
- 编号区间：AUD-17800–AUD-17899
- 文件数：3（共 2,413 行），全部通读
- 交叉验证：`src/features/edit/core/commands.py`（SplitClipCommand/MacroCommand/AddMarkerCommand）、`src/features/edit/core/timeline_v2.py`（TimelineMarker/TransitionInstance/EditTrack）、`src/features/edit/core/scene_cut/models.py`（from_dict/枚举校验）、调用方 `src/features/edit/ui/scene_cut_dialog.py`

严重度统计：Critical 0 / Major 2 / Suggestion 13

---

## src/features/edit/core/scene_cut/__init__.py (行数: 342)

### 发现

（无）

### 通过项

- 通过（无逻辑）：纯 re-export 聚合模块，逐项核对 10 个子模块的 import 名单与 `__all__`（202–342 行）完全一致，无遗漏、无多余
- 子模块（models/frame_sampler/metrics/filters/detector/worker/cache/timeline_mapping/apply_ops/subclips）均实际存在，导入可解析
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性：不适用（无逻辑）
- 备注（非缺陷）：apply_ops 自身 `__all__` 中的 `verify_split_preserves_attributes`、`get_preserved_attributes_summary` 未做包级再导出，与其它导出策略略有不一致，不影响功能

---

## src/features/edit/core/scene_cut/apply_ops.py (行数: 907)

### 发现

- [AUD-17801] [Major] apply_splits 不去重相同 timeline_frame 的候选，宏执行时第二条 SplitClipCommand 抛 ValueError 导致整个操作回滚失败
  - 位置: src/features/edit/core/scene_cut/apply_ops.py:352-390（`_filter_split_candidates`）、313-341（排序与建命令循环）
  - 证据:
    ```python
    # _filter_split_candidates：只过滤不去重
    if not (clip.timeline_start < frame < clip.timeline_end):
        continue
    if hard_cut_only and cand.kind != SceneCutKind.HARD_CUT:
        continue
    valid.append(cand)
    ...
    sorted_splits = sorted(valid_splits, key=lambda x: x.timeline_frame, reverse=True)
    for cand in sorted_splits:
        frame = cand.timeline_frame
        ...
        split_cmd = SplitClipCommand.create(
            track_id=target_track_id, clip_id=target_clip_id, split_frame=frame)
    ```
    而 SplitClipCommand.apply（commands.py:2108-2112）在帧不在剪辑开区间内时 `raise ValueError`；MacroCommand.apply（commands.py:9941-9947）捕获后回滚全部已应用子命令并重新抛出。
  - 影响: 两个 accepted 候选经源帧→时间线帧映射（跨 fps / 变速圆整，`timeline_mapping.estimate_fps_conversion_error` 表明映射非单射）落到同一 timeline_frame 时，第一条切分后该帧成为边界，第二条命令必抛 ValueError——整个宏回滚、异常裸抛给调用方，用户"全部应用"操作整体失败。detect_conflicts（775-779 行）把 duplicate_splits 列为冲突（阻断应用），而 apply 侧既不去重也不降级告警，两条路径行为不一致；apply_all 也不强制先 validate_apply_prerequisites。
  - 建议: 在 `_filter_split_candidates` 末尾按 `timeline_frame` 去重（保留 confidence 最高者）并计入 warnings；或 apply_splits/apply_all 内部先调 detect_conflicts，将 duplicate 自动去重而非失败。
  - 置信度: 高（两侧代码均已核实；触发需要映射碰撞，作者在 detect_conflicts 中已预见过该场景）

- [AUD-17802] [Suggestion] detect_conflicts 用真值判断跳过 timeline_frame==0，与 apply_markers 已修复的同类缺陷不一致
  - 位置: src/features/edit/core/scene_cut/apply_ops.py:751
  - 证据:
    ```python
    for cand in candidates:
        if cand.is_accepted and cand.timeline_frame:
            frame = cand.timeline_frame
    ```
    对比同文件 173-176 行已修复的写法："timeline_frame == 0 是合法帧（旧 `or` 会错误回退…）"：
    ```python
    frame = (cand.timeline_frame if cand.timeline_frame is not None
             else cand.source_frame)
    ```
  - 影响: 帧号 0 的 accepted 候选不参与转场冲突检查（时间线允许负位置或转场覆盖帧 0 时漏报 transition_conflicts）；同一文件内两种判断风格并存，后续维护易再犯。
  - 建议: 改为 `if cand.is_accepted and cand.timeline_frame is not None:`。
  - 置信度: 高（行为确定；实际影响场景较少）

- [AUD-17803] [Suggestion] verify_split_preserves_attributes 与 SplitClipCommand/MacroCommand 实际语义不符，会产生误报与漏报
  - 位置: src/features/edit/core/scene_cut/apply_ops.py:538-556
  - 证据:
    ```python
    # Left clip preserves fade_in, fade_out
    if left_clip.fade_in_duration != original_clip.fade_in_duration:
        errors.append(f"Left clip fade_in_duration lost: ...")
    ...
    if right_clip.linked_clip_id is not None:
        errors.append(f"Right clip should have linked_clip_id=None, ...")
    ```
    而 SplitClipCommand.apply（commands.py:2145-2150）有意 clamp 左段 `fade_in_duration=min(clip.fade_in_duration, left_timeline_duration)`；MacroCommand.apply（commands.py:9949-9977）会为 A/V 成对切分重新链接右段（`with_linked_clip_id`）。
  - 影响: 该校验器把命令的预期行为（左段 fade_in 收缩、宏内右段重链）判为错误（误报）；同时未校验左段 fade_out（命令对左段 fade_out 未做 clamp，与右段 fade_in=0 的处理不对称，校验器也未覆盖，漏报）。当前无调用方（纯导出供测试/后续使用），属潜伏缺陷。
  - 建议: 校验规则对齐命令语义：左段 fade_in 允许 `<=` 原值、补充左段 fade_out 校验、宏内成对切分时允许右段重新互链；或直接以 `SplitClipCommand._original_clip` 对照断言。
  - 置信度: 高（两侧代码均已核实）

- [AUD-17804] [Suggestion] _split_linked_clip 只在 audio_tracks 中查找链接剪辑，反向链接（目标为音频剪辑、链接对象为视频）永远失败
  - 位置: src/features/edit/core/scene_cut/apply_ops.py:416-440
  - 证据:
    ```python
    # Find linked clip
    for audio_track in timeline.audio_tracks:
        linked_clip = audio_track.get_clip_by_id(linked_clip_id)
        if linked_clip:
            ...
    logger.warning(f"Linked clip {linked_clip_id} not found in audio tracks")
    ```
  - 影响: 若对音频剪辑执行 apply_splits 且其 linked_clip_id 指向视频剪辑（TimelineSequence.get_clip_by_id 是全轨查找，链接关系本身不限方向），链接切分静默失败并落入 warnings（"split failed"），视频侧不被切分。
  - 建议: 用 `timeline.get_clip_by_id(linked_clip_id)` 全轨查找后取其所在轨，或同时遍历 video_tracks + audio_tracks。
  - 置信度: 高（行为确定；场景较少，故仅 Suggestion）

- [AUD-17805] [Suggestion] 访问私有属性 _source_fps_num 削弱封装；apply_all 的命令列表类型退化为 List[Any]
  - 位置: src/features/edit/core/scene_cut/apply_ops.py:549-552、588、627
  - 证据:
    ```python
    if left_clip._source_fps_num != original_clip._source_fps_num:
        errors.append(f"Left clip _source_fps_num lost")
    ...
    "has_fps_metadata": clip._source_fps_num is not None,
    ...
    all_commands: List[Any] = []
    ```
  - 影响: 依赖 ClipInstance 私有字段，重命名即静默破坏（AttributeError）；`List[Any]` 丢失 MacroCommand.create(label, commands) 期望的 EditCommand 类型约束，类型检查失效。
  - 建议: 为 ClipInstance 增加 FPS 元数据的公共只读属性（或在 timeline_v2 提供访问器）；all_commands 标注为 `List[EditCommand]`。
  - 置信度: 高

### 通过项

- 线程安全：纯函数、无共享可变状态、无 OpenGL/UI 依赖（不适用信号防递归、GPU 生命周期、GLSL/HDR）
- 状态提交一致性：右到左切分策略经 commands.py:2131 核实成立（左段保留原 clip_id，后续更低帧仍落在左段内）；边界校验 `timeline_start < frame < timeline_end` 与 SplitClipCommand.apply 的校验完全一致；宏内单条失败会整体回滚（MacroCommand.apply 9941-9947），undo 走单条 MacroCommand
- apply_markers 的 timeline_frame==0 回退修复正确（175-176 行，`is not None`）；marker 颜色映射 `_get_marker_color` 用 `.get(kind, default)` 兜底未知枚举
- apply_splits 对锁定轨 / 禁用剪辑 / 目标不存在均提前返回带 warnings 的空结果，不生成半截命令
- detect_conflicts 的转场几何（center_frame ± duration//2）与 TransitionInstance 字段核实匹配；重复帧、越界帧均入 ConflictReport.has_conflicts
- 输入验证：get_accepted_count / get_hard_cut_count / validate_apply_prerequisites / estimate_result_clips 边界行为正确
- 类型注解：除 AUD-17805 所述两处外完整

---

## src/features/edit/core/scene_cut/cache.py (行数: 1,164)

### 发现

- [AUD-17810] [Major] load_analysis 未捕获 ValueError，损坏/异构的外部 JSON 会击穿 "invalid → None" 契约
  - 位置: src/features/edit/core/scene_cut/cache.py:341-367（对照 models.py:752-775、models.py:605-606、models.py:126-135）
  - 证据:
    ```python
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return SceneCutAnalysis.from_dict(data)
    except json.JSONDecodeError as e:
        ...
        return None
    except (KeyError, TypeError) as e:
        ...
        return None
    except OSError as e:
        ...
        return None
    ```
    而 SceneCutCandidate.from_dict 含 `kind=SceneCutKind(data.get("kind", "unknown"))`、`decision=SceneCutDecision(...)`（未知枚举值抛 ValueError），且多个 dataclass 的 `__post_init__` 对负 source_frame / 非法 settings 抛 ValueError。
  - 影响: 缓存文件属外部数据（手改、旧版本枚举值、第三方写入），一旦含未知 kind/decision 或越界数值，ValueError 直接抛给调用方而非返回 None。当前唯一调用方 scene_cut_dialog.py:1872 用 `except Exception: return None` 兜底掩盖（同时违反"不得宽泛 except 掩盖领域错误"铁律），契约破坏被转移而非修复；任何新调用方按 docstring 使用即崩溃。
  - 建议: 在 load_analysis 中追加 `except ValueError`（记录 warning 后返回 None），或让 from_dict 统一抛领域异常并由本层转换；调用方收敛为窄异常。
  - 置信度: 高

- [AUD-17811] [Suggestion] save_analysis 固定 .tmp 临时名：并发写同 key 互相覆盖，非 OSError 失败残留 .tmp 且永不清理
  - 位置: src/features/edit/core/scene_cut/cache.py:386-400（对照 437、459 的 glob("*.json")）
  - 证据:
    ```python
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temp_path.replace(path)
    ...
    except OSError as e:
        ... raise RuntimeError(...) from e
    ```
  - 影响: (1) 两个实例/线程同时保存同一 cache_key 时写同一 `<key>.tmp`，交错写入后 rename 可能落地损坏内容；(2) `json.dump` 遇不可序列化 diagnostics 抛 TypeError 时不被捕获且残留 .tmp；(3) invalidate_for_asset / clear_all 只清 `*.json`，.tmp 文件永久滞留膨胀缓存目录。
  - 建议: 临时名加唯一后缀（`f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"`）并在 finally 中清理；序列化异常单独捕获。
  - 置信度: 高（行为确定；触发频率低）

- [AUD-17812] [Suggestion] ThumbnailCache 全程无锁，与模块 docstring "Thread-safe operations" 的声明不符
  - 位置: src/features/edit/core/scene_cut/cache.py:600-608、699-721、746-776、880-906（对照 19-23 行设计原则）
  - 证据:
    ```python
    # LRU tracking: OrderedDict maintains insertion order
    self._entries: OrderedDict[str, ThumbnailEntry] = OrderedDict()
    self._current_size: int = 0
    ```
    `has_thumb`/`save_thumb`/`load_thumb`/`_evict_one` 对 `_entries` 做 `move_to_end`/`del`/`popitem(last=False)` 及 `_current_size` 增减，均无 `threading.Lock` 保护（全文件未 import threading）。
  - 影响: worker（QThread，worker.py:462）与 UI 或两个缓存实例并发操作同一 thumbs 目录时，LRU 顺序与 `_current_size` 统计可竞态漂移（重复计数/漏减 → 提前或失效驱逐）。当前调用方均在 UI 线程且每次新建实例，未实际共享同一实例，属潜伏风险；但文档声明已经失实。
  - 建议: 加 `threading.Lock` 保护 `_entries`/`_current_size` 的复合操作，或修正 docstring 移除线程安全声明并注明单线程使用约束。
  - 置信度: 高（无锁事实确定；实际并发场景当前不存在）

- [AUD-17813] [Suggestion] get_scene_cut_cache 非单例且每次构造全量 stat 扫描 thumbs 目录，UI 线程上重复付出 O(n) 开销
  - 位置: src/features/edit/core/scene_cut/cache.py:1113-1123、617-645、591-608（对照 scene_cut_dialog.py:1873、1884）
  - 证据:
    ```python
    def get_scene_cut_cache(project_cache_dir: str | None = None) -> SceneCutCache:
        return SceneCutCache(project_cache_dir)
    ...
    def _scan_existing(self) -> None:
        for path in self._thumbs_dir.glob("*.jpg"):
            ...
            size = path.stat().st_size
            self._current_size += size
    ```
  - 影响: `get_` 前缀暗示共享单例，实际每次新建；每次构造都遍历 thumbs 目录（上限 10000 项、每项一次 stat）并在 `invalidate_for_asset` 中再全量 json.load 所有分析文件。scene_cut_dialog 在 UI 线程每次加载/保存都新建实例，重复扫描造成可感知卡顿；且多实例各自维护 LRU，驱逐互不知情。
  - 建议: 模块级惰性单例（按 project_cache_dir 缓存）；或 `_scan_existing` 惰性化/限额（只统计计入 size，超出 max_entries 部分留给驱逐）。
  - 置信度: 高

- [AUD-17814] [Suggestion] SceneCutCache.save_analysis 内含未完成的死循环：计算结果弃用、访问私有方法、候选缩略图键永不回填
  - 位置: src/features/edit/core/scene_cut/cache.py:1056-1063
  - 证据:
    ```python
    # Update candidate thumb keys
    for candidate in analysis.candidates:
        if candidate.before_thumb_cache_key is None:
            before_key = self._thumbnail_cache._make_filename(
                cache_key, candidate.source_frame, "before"
            )
            # Update candidate with thumb key (would need new analysis)
            # For now, just track that thumbnails should use this naming
    ```
  - 影响: 循环体无副作用，`before_key` 未使用；跨对象调用 `ThumbnailCache._make_filename` 私有方法；注释自认未实现。误导阅读者以为已保存缩略图键，实际 `before/after_thumb_cache_key` 恒为 None。
  - 建议: 删除该循环，或实现为：保存后用 `with_updated`/新 analysis 回填键并重写（模型不可变，需返回新对象）。
  - 置信度: 高

- [AUD-17815] [Suggestion] invalidate_asset 绕过 ThumbnailCache 跟踪直接 unlink，造成 has_thumb 与 load_thumb 结果矛盾及 _current_size 虚高
  - 位置: src/features/edit/core/scene_cut/cache.py:1080-1093（对照 687-721 的 has_thumb、779-814 的 load_thumb）
  - 证据:
    ```python
    for path in self._analysis_cache.thumbs_dir.glob("*.jpg"):
        parts = path.stem.split("_")
        if parts:
            cache_key = parts[0]
            analysis_path = self._analysis_cache.get_analysis_path(cache_key)
            if not analysis_path.exists():
                try:
                    path.unlink()
                    thumbs_deleted += 1
    ```
  - 影响: 同一实例内 `thumbnail_cache._entries` 仍保留已删文件的条目：`has_thumb` 命中内存条目直接返回 True（不查盘），而 `load_thumb` 因 `path.exists()` 为 False 返回 None——调用方先查后读即出现"存在却读不到"；`_current_size` 统计的幽灵体积持续存在，导致后续有效缩略图被提前 LRU 驱逐。
  - 建议: 删除文件统一走 `thumbnail_cache.delete_thumbs_for_analysis(cache_key)`（内部同步维护 `_entries`/`_current_size`），本函数只负责找出孤儿 cache_key。
  - 置信度: 高

- [AUD-17816] [Suggestion] save_thumb 直接写最终路径，非原子写
  - 位置: src/features/edit/core/scene_cut/cache.py:752-757（对照 save_analysis 的 temp+replace，388-393）
  - 证据:
    ```python
    try:
        with open(path, "wb") as f:
            f.write(image_data)
    except OSError as e:
        logger.error(f"Failed to save thumbnail {path}: {e}")
        raise RuntimeError(f"Failed to save thumbnail: {e}") from e
    ```
  - 影响: 写入中途崩溃/断电会留下截断的 JPEG；下次 `has_thumb`/`load_thumb` 视其为有效缓存返回坏数据给 UI 解码。分析 JSON 已用 temp+replace，缩略图未保持一致标准。
  - 建议: 与 save_analysis 相同的临时文件 + `os.replace` 原子落盘。
  - 置信度: 高

- [AUD-17817] [Suggestion] AnalysisCache.is_cache_valid 的 settings 参数从未使用，校验语义与同名顶层函数不一致
  - 位置: src/features/edit/core/scene_cut/cache.py:505-526（对照 check_cache_validity 1126-1164，后者校验 settings.to_hash()）
  - 证据:
    ```python
    def is_cache_valid(
        self,
        cache_key: str,
        path: str,
        settings: SceneCutSettings,
    ) -> bool:
        ...
        analysis = self.load_analysis(cache_key)
        if analysis is None:
            return False
        return self.validate_analysis(analysis, path)
    ```
  - 影响: `settings` 形参被忽略（cache_key 内嵌 settings_hash 间接保证一致性，但 has_analysis/load 均按键进行，参数纯冗余），调用者误以为该校验含 settings 比对；与 `check_cache_validity` 的显式 settings 校验形成两套易混语义。
  - 建议: 移除参数，或在 validate_analysis 中补 `analysis.settings.to_hash() == settings.to_hash()` 显式校验。
  - 置信度: 高

### 通过项

- GPU 生命周期 / GLSL·HDR / 信号防递归：不适用（纯磁盘缓存模块，无 OpenGL、无 Qt 控件）
- 缓存键设计：media_fingerprint 覆盖 path+size+mtime_ns+asset_id，键内含 source range、settings hash、DETECTOR_VERSION、proxy 指纹、clip_id，截断 24 hex（96 bit）碰撞风险可忽略；`end_source_frame or 'end'` 的 0-假值隐患经核实不可达（models.py:130-135 校验 end > start ≥ 0，end==0 无法构造）
- 序列化安全：全程 JSON、无 pickle；分析保存为 temp + replace 原子替换
- 缓存失效：validate_analysis / check_cache_validity 均校验 detector_version 与媒体指纹（size+mtime_ns），文件缺失返回 False 不抛异常
- 目录创建失败降级为 warning 不崩溃；load/save/delete 各失败路径均有窄异常处理（除 AUD-17810/17811 所述）
- LRU 语义：OrderedDict move_to_end 方向正确（首端为最旧），按条目数与总字节数双重上限驱逐；_evict_one 对空表安全，_evict_if_needed 无死循环路径（_current_size 与条目和同步增减）
- _scan_existing 文件名解析 `<hex_key>_<frame>_<before|after>.jpg` 与生成端一致（cache_key 为纯十六进制不含下划线）
- 类型注解：完整（唯一 `Any` 在 generate_key_from_asset 的 asset 参数，见下）
- 备注（未计发现）：`generate_key_from_asset`（1013-1042）的 `asset: Any` 直接点取 `.path/.asset_id/.proxy_path/.has_proxy`，属性缺失会 AttributeError 而非返回 None——当前无调用方且注释已声明依赖 MediaAsset，暂不计

---

## 审查维度覆盖说明

九个维度全部逐文件检查；其中 GPU 资源生命周期、GLSL/HDR 正确性、信号防递归对本批次三个文件均为不适用（纯 Python 逻辑/缓存/命令构造模块，无渲染与控件交互）。
