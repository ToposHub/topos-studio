# 批次报告：batch-181 — src/features/edit/core/scene_cut/

- 审查日期：2026-09-28
- 编号区间：AUD-18100–AUD-18199（实际使用 AUD-18100–AUD-18125）
- 文件数：3（合计 2,741 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 汇总：Critical 0 · Major 3 · Suggestion 13

交叉验证过的依赖（只读）：`src/features/edit/core/project.py`（EditProject/ProjectBin）、`src/features/edit/core/scene_cut/models.py`（SceneCutSettings/SourceFrameTimeIndex）、`src/features/edit/core/scene_cut/frame_sampler.py`（CancelToken）、`src/features/edit/core/scene_cut/detector.py`（DetectionProgress）、`src/features/edit/core/timeline_v2.py` 与 `clip_params.py`（ClipInstance/SpeedParams 速度取值范围 0.1–8.0）、`src/features/edit/core/scene_cut_controller.py`（add_subclips_to_project 调用链）。

---

## src/features/edit/core/scene_cut/subclips.py (行数: 765)

### 发现

- [AUD-18100] [Major] add_subclips_to_bin 手工重建 EditProject 时丢失 revision 字段，乐观锁版本号被静默重置为 0
  - 位置: src/features/edit/core/scene_cut/subclips.py:410
  - 证据:
    ```python
    return EditProject(
        project_id=project.project_id,
        name=project.name,
        assets=new_assets,
        bins=new_bins,
        ...
        created_at=project.created_at,
        modified_at=project.modified_at,
    )
    ```
    而 EditProject 定义了 `revision: int = 0`（project.py:1006），注释明确其用途："项目级单调递增 revision（乐观锁）……用于 AI plan 的 base_revision 一致性比较（TAEP §6.2）"。未传 `revision` 即回退默认 0。同链路中的 `get_or_create_scene_cut_bin`→`create_bin`、`remove_existing_subclips`→`remove_asset` 同样丢失该字段（project.py 自身的手工重建方法也有此问题，属其他批次范围）。
  - 影响: "Add Cuts to Media Pool" 后返回的工程 revision 变为 0，且 scene_cut_controller.py:356-357 通过 `self._project_setter(result.project)` 直接写回活动工程；若该工程此前已有 timeline 提交（revision=N>0），乐观锁 base_revision 比较将全部失配，AI plan/脚本的一致性校验被破坏。
  - 建议: 用 `dataclasses.replace(project, assets=new_assets, bins=new_bins, modified_at=...)` 替代逐字段手工重建，天然保留 revision 及未来新增字段。
  - 置信度: 高

- [AUD-18101] [Suggestion] add_subclips_to_bin 变更工程内容但不刷新 modified_at
  - 位置: src/features/edit/core/scene_cut/subclips.py:427
  - 证据:
    ```python
        created_at=project.created_at,
        modified_at=project.modified_at,
    )
    ```
    EditProject 所有变更方法（import_asset/remove_asset/create_bin/move_asset_to_bin 等）均为 `modified_at=now`（project.py:1362、1408、1690 等）。
  - 影响: 素材池实际已变更，但工程 mtime 停留在旧值，依赖 modified_at 的脏检查、自动保存排序、"最近修改"展示失真。
  - 建议: 与其他变更方法一致，写 `modified_at=time.time()`（随 AUD-18100 一并改用 replace 修复）。
  - 置信度: 高

- [AUD-18102] [Suggestion] duplicate_strategy 未知取值无 else 分支，静默按 APPEND 语义执行
  - 位置: src/features/edit/core/scene_cut/subclips.py:484
  - 证据:
    ```python
    if existing_report.has_existing:
        if duplicate_strategy == DUPLICATE_CANCEL:
            ...
        elif duplicate_strategy == DUPLICATE_REPLACE:
            ...
        elif duplicate_strategy == DUPLICATE_APPEND:
            ...
    # 无 else：任何拼写错误/非法值直接落入隐式 append
    ```
  - 影响: 调用方传入错误策略字符串（如 "Replace"、"repalce"）时不报错，旧子剪辑保留、新子剪辑照常追加，产生重复素材且无告警。
  - 建议: 增加 else 分支抛 ValueError 或 logger.error，或在函数入口用集合白名单校验三个常量。
  - 置信度: 高

- [AUD-18103] [Suggestion] 子剪辑元数据读取/校验函数不验证值类型与范围，反序列化数据可触发 TypeError
  - 位置: src/features/edit/core/scene_cut/subclips.py:614
  - 证据:
    ```python
    source_start = asset.metadata.get(SUBCLIP_SOURCE_START, 0)
    source_duration = asset.metadata.get(SUBCLIP_SOURCE_DURATION, 0)
    return source_start, source_duration
    ```
    以及 validate_subclip_for_timeline（719-727 行）：
    ```python
    source_start = asset.metadata.get(SUBCLIP_SOURCE_START, 0)
    ...
    if source_start < 0:
    ```
    metadata 值为 `Any`；工程文件为外部数据，加载后若 "source_start" 为字符串（如 "120"），`<` 比较抛 TypeError；get_subclip_source_range 声明返回 `Tuple[int, int]` 但未做 int() 归一（对比 create_subclip_asset:191-195 对 total_frames 做了 try/int 防御，此处没有）。
  - 影响: 损坏/手改的工程文件可在时间线插入校验路径抛未捕获 TypeError；类型注解与实际返回不符。
  - 建议: 读取处统一 `int(...)` + try/except (TypeError, ValueError) 归一化（参照 191-195 行已有模式），失败按缺失字段处理。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：本文件不接触 OpenGL/ModernGL（纯数据层），无 GPU 泄漏面。
- GLSL·HDR 正确性：无着色器与像素数据处理，不适用。
- 信号防递归：无 Qt 信号，无递归风险。
- 线程安全：纯函数 + frozen dataclass 不可变数据流；remove_subclips_for_analysis/remove_existing_subclips 迭代原快照并重绑新实例，无迭代器失效。
- 异常与取消路径：add_subclips_to_project 入口校验 parent_asset 存在（470-473）；int 转换有 try 防御（190-195）；无宽泛 except。
- 性能模式：check_existing_subclips/get_subclips_for_analysis 为 O(assets) 单次线性扫描，量级可接受。
- undo/redo 入口本文件不直接创建命令，提交一致性风险集中在 AUD-18100。

---

## src/features/edit/core/scene_cut/timeline_mapping.py (行数: 997)

### 发现

- [AUD-18110] [Major] _cfr_source_to_timeline 反向分支忽略 fps 换算，与正向/逆向映射不对称，误差随帧距线性增长突破 ≤1 帧承诺
  - 位置: src/features/edit/core/scene_cut/timeline_mapping.py:388
  - 证据:
    ```python
    # Handle reverse
    if config.reverse:
        # In reverse, source frames go from end to start
        relative_source_reversed = config.source_duration - 1 - relative_source
        timeline_seconds = float(relative_source_reversed) / float(config.timeline_fps) / float(config.speed)
        timeline_relative = timeline_seconds * float(config.timeline_fps)
    ```
    非反向路径（379-386 行）为 `timeline_relative = relative * timeline_fps / (source_fps * speed)`，含 fps 比率；反向路径中 timeline_fps 约掉后变为 `relative_reversed / speed`，丢失了 `timeline_fps/source_fps` 因子。而逆方向的 _cfr_timeline_to_source 反向分支（561-571 行）却按 `source_seconds * source_fps` 换算：
    ```python
    if config.reverse:
        total_timeline_seconds = float(config.timeline_duration) / float(config.timeline_fps)
        source_seconds = (total_timeline_seconds - timeline_seconds) * float(config.speed)
    ...
    relative_source = source_seconds * float(config.source_fps)
    ```
  - 影响: 反向 + fps 转换（如 23.976 源→24 时间线）时两个方向映射互为不一致的函数；误差 ≈ relative × (24/23.976−1) ≈ relative × 0.001 帧，源内约 1000 帧处即达 1 帧容差边界，长片段达数帧，违反模块声明的 "Mapping error should never exceed 1 frame"，导致场景剪切点落到错误的时间线帧。
  - 建议: 反向分支复用非反向的换算链：`timeline_relative = relative_source_reversed * timeline_fps / (source_fps * speed)`，并与 _cfr_timeline_to_source 反向分支对拍往返一致性测试（source→timeline→source 应还原）。
  - 置信度: 高（代码数学可证的不对称；实际触发依赖"反向+fps 转换"片段存在）

- [AUD-18111] [Suggestion] 越界边界结果用 timeline_fps 计算 source_seconds，fps 不同时数值错误
  - 位置: src/features/edit/core/scene_cut/timeline_mapping.py:508
  - 证据:
    ```python
    source_seconds=float(config.source_duration) / float(config.timeline_fps),
    timeline_seconds=float(config.timeline_duration) / float(config.timeline_fps),
    ```
    _boundary_timeline_to_source 同样问题（707-708 行：`source_seconds=float(config.source_duration) / float(config.timeline_fps or 24)`）。source 时长应以 source_fps（缺失时才回退）折算；另 708 行 `config.timeline_fps or 24` 的兜底与 __post_init__ 已保证 timeline_fps>0 的前提矛盾（Fraction(24,1) 恒真值，该 or 分支死码）。
  - 影响: 越界候选的 source_seconds 展示/记录值在 fps 转换场景下偏大（如 23.976→24 时约 +0.1%），仅信息字段错误，不影响映射帧号。
  - 建议: 改用 `config.source_fps or config.timeline_fps` 折算 source_seconds，并删除多余的 `or 24`。
  - 置信度: 高

- [AUD-18112] [Suggestion] create_mapper 未复用同模块 fps_to_fraction 的 NTSC 快照对齐，两套 fps→Fraction 规则不一致
  - 位置: src/features/edit/core/scene_cut/timeline_mapping.py:873
  - 证据:
    ```python
    config = ClipMappingConfig(
        ...
        timeline_fps=Fraction(timeline_fps).limit_denominator(10000),
        source_fps=Fraction(source_fps).limit_denominator(10000) if source_fps else None,
    ```
    同文件 fps_to_fraction（890-918 行）专门将 23.976/29.97/59.94 对齐到 24000/1001 等精确分数；而 `Fraction(23.976).limit_denominator(10000)` 得到 2997/125（≈23.976000）而非 24000/1001（≈23.976024）。
  - 影响: 经 create_mapper 创建的映射器与 fps_to_fraction/estimate_fps_conversion_error 使用不同基准；NTSC 素材相对误差约 1e-6（2 小时累计约 0.02 帧，实际漂移微小），主要是语义不一致与 has_fps_conversion 判定口径分裂。
  - 建议: create_mapper 内部改调 `fps_to_fraction(timeline_fps)` / `fps_to_fraction(source_fps)`，统一全模块分数化规则。
  - 置信度: 高（不一致可证；实际数值影响经计算很小）

- [AUD-18113] [Suggestion] ClipMappingConfig.__post_init__ 不校验 speed，0 值致 ZeroDivisionError、负值致全部帧 clamp 到片段头
  - 位置: src/features/edit/core/scene_cut/timeline_mapping.py:154
  - 证据:
    ```python
    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.timeline_fps <= 0:
            raise ValueError(...)
        if self.timeline_duration <= 0:
        ...
    ```
    校验覆盖 fps/duration/start 却无 speed；随后 385 行 `source_seconds / float(config.speed)`、559 行 `timeline_seconds * float(config.speed)` 直接参与除法。经 from_clip_instance 路径 speed 恒为 [0.1, 8.0]（ClipInstance/SpeedParams 校验），但公开工厂 create_mapper(speed: float) 接受任意 float。
  - 影响: speed=0 在首次映射时抛 ZeroDivisionError（而非构造期清晰 ValueError）；speed<0 时 timeline_relative 为负、所有源帧被 clamp 到 timeline_start（399-402 行），映射完全失真且无警告。
  - 建议: __post_init__ 增加 `if self.speed <= 0: raise ValueError(...)`，与既有字段校验风格一致。
  - 置信度: 高（行为可证；可连性取决于调用方传参）

- [AUD-18114] [Suggestion] from_clip_instance 直接依赖 ClipInstance 私有下划线属性，且保留不可达的负标量速度分支
  - 位置: src/features/edit/core/scene_cut/timeline_mapping.py:201
  - 证据:
    ```python
    timeline_fps = Fraction(
        clip._timeline_fps_num or 24,
        clip._timeline_fps_den or 1
    )
    ...
    elif isinstance(clip.speed, (int, float)) and clip.speed < 0:
        reverse = True
    ```
    `_timeline_fps_num` 等私有字段无 hasattr 保护（对比同函数内 speed 处理用了 hasattr 防御）；且 ClipInstance.__post_init__ 校验标量 speed ∈ [0.1, 8.0]、恒有 effective_speed 方法，故 `clip.speed < 0` 的标量反向分支对真实 ClipInstance 不可达，若真被 duck-type 对象触发，speed 同时为负数又会落入 AUD-18113 的负速除法问题。
  - 影响: 与 ClipInstance 内部字段强耦合（改名即断）；死分支误导后续维护者以为支持负标量速度。
  - 建议: 优先让 ClipInstance 暴露公开的 fps 元数据访问器；删除不可达的负标量分支或改为显式异常。
  - 置信度: 中（耦合与死分支可证；是否重构属设计取舍）

- [AUD-18115] [Suggestion] estimate_fps_conversion_error 未校验 source_fps>0，0 值直接 ZeroDivisionError
  - 位置: src/features/edit/core/scene_cut/timeline_mapping.py:944
  - 证据:
    ```python
    source_frac = fps_to_fraction(source_fps)
    timeline_frac = fps_to_fraction(timeline_fps)

    duration_seconds = Fraction(duration_frames) / source_frac
    expected_timeline_frames = duration_seconds * timeline_frac

    # Float calculation
    float_expected = float(duration_frames) / source_fps * timeline_fps
    ```
    `Fraction(duration_frames) / source_frac` 与 `float(duration_frames) / source_fps` 在 source_fps=0 时均除零（fps_to_fraction(0) → Fraction(0)）。
  - 影响: 公开工具函数对非法输入无防御，调用方拿到裸 ZeroDivisionError 而非 ValueError。
  - 建议: 入口校验 `source_fps > 0 and timeline_fps > 0 and duration_frames >= 0`。
  - 置信度: 高

### 通过项
- 线程安全：ClipSourceMapper 为单线程构造/使用，VFR 查找表（294-299 行）构造期一次性预构建，无可变共享状态。
- GPU 资源生命周期 / GLSL·HDR 正确性：纯数值映射，不接触 GPU 与像素数据，不适用。
- 信号防递归：无 Qt 信号。
- VFR 路径正确性：_vfr_source_to_timeline/_vfr_timeline_to_source 均基于真实 PTS 秒（source_frame_to_seconds），反向分支用 total_source_seconds 折算，未受 AUD-18110 的 CFR 反向缺陷影响；bisect 查找表有序前提由 PTS 单调性保证。
- 性能模式：Fraction 预计算比率、VFR 秒表一次构建 + bisect 二分，符合"分数运算避免浮点累积"声明。
- 异常路径：越界输入走边界结果 + logger.warning（351-357、530-535），不抛异常。
- map_candidates 重建候选时保留全部原字段并合并 reasons（746-759），无字段丢失。

---

## src/features/edit/core/scene_cut/worker.py (行数: 979)

### 发现

- [AUD-18120] [Major] SceneCutWorkerManager._workers 的清理键与写入键格式不一致，条目永不移除，已完成 worker 强引用持续泄漏
  - 位置: src/features/edit/core/scene_cut/worker.py:942
  - 证据:
    写入（861-862 行）：
    ```python
    key_str = worker.task_key.to_string()
    self._workers[key_str] = worker
    ```
    DetectionTaskKey.to_string（281-284 行）恒为三段：`f"{asset_id}:{settings_hash}:{clip_part}"`（clip_part 为 clip_id 或 "full"）。
    清理（941-944 行）：
    ```python
    # Clean up worker reference
    key_str = f"{asset_id}:{analysis.settings.to_hash()}"
    if key_str in self._workers:
        del self._workers[key_str]
    ```
    两段 vs 三段，任何情况下都不相等，`del` 永不命中；且 _on_worker_cancelled/_on_worker_error（948-954 行）根本不做清理。
  - 影响: 每次经 manager 启动检测，完成后 SceneCutWorker（parent=self 的 QThread 子类，携带全部信号连接与 result/analysis 大对象）都滞留 _workers；长会话多次检测导致字典与 QObject 树无界增长（内存泄漏），也使同键"再次启动"永远走 `registry.is_running` 之外的旧引用路径失去意义。
  - 建议: 清理统一用 `worker.task_key.to_string()`（可在信号 lambda 中闭包捕获 worker 或 key_str）；cancelled/error 处理器同样移除；或改为按 finished 信号统一清理。
  - 置信度: 高

- [AUD-18121] [Suggestion] start_detection 中 self.start() 失败时 registry 条目永不注销，该任务键被永久占用
  - 位置: src/features/edit/core/scene_cut/worker.py:658
  - 证据:
    ```python
    # Register this worker
    if not registry.register(self._task_key, self):
        logger.warning(f"Failed to register worker: {self._task_key.to_string()}")
        return False

    # Start thread
    self.start()
    return True
    ```
    注销仅存在于 run() 的 finally（740-742 行）：若 start() 抛异常（Qt 线程启动失败）run() 永不执行，注册项泄漏。
  - 影响: 该 asset+settings+clip 组合此后永远返回 "Detection already running"（651-655 行），对应素材无法再检测，只能重启应用。
  - 建议: `try: self.start() except Exception: registry.unregister(self._task_key); raise`。
  - 置信度: 中（触发条件罕见，但路径可证）

- [AUD-18122] [Suggestion] ProgressThrottler 的 pending 机制无补发路径，节流窗口内的最后一次进度被丢弃；is_complete 时可能双发同一进度
  - 位置: src/features/edit/core/scene_cut/worker.py:746
  - 证据:
    ```python
    self._throttler.set_pending(progress, progress.candidates_found)
    if self._throttler.should_emit():
        self._throttler.record_emit()
        self.progress.emit(progress)
        if progress.candidates_found > 0:
            self._throttler.clear_pending()
    # Always emit on completion phases (no throttle)
    if progress.is_complete:
        self._throttler.clear_pending()
        self.progress.emit(progress)
    ```
    pending_progress/pending_candidates（221-222、239-257 行）存入后没有任何定时器或结束前 flush 消费它：100ms 窗口内到达且非 is_complete 的进度直接丢失；反之 is_complete 且恰逢 should_emit() 为真时同一 progress 连续 emit 两次（762 与 770 行）。
  - 影响: UI 进度可能从如 92% 直接跳到完成（末帧进度未送达）；完成阶段偶发重复刷新。属轻微 UI 缺陷，pending_candidates 为死字段。
  - 建议: 删除无效 pending 字段改为纯节流，或在 run() 结束前统一 flush pending；is_complete 分支加 `elif` 避免双发。
  - 置信度: 高

- [AUD-18123] [Suggestion] CancelToken 一次性且从不复位，已取消的 SceneCutWorker 对象被复用重启时立即误报取消并丢弃结果
  - 位置: src/features/edit/core/scene_cut/worker.py:542
  - 证据:
    ```python
    # Create cancel token
    self._cancel_token = CancelToken()
    ```
    run() 开头重置了 `_is_running/_was_cancelled/_error/_result`（681-684 行）但不重置 token；CancelToken（frame_sampler.py:63-89）无任何复位方法。worker 完成后 registry 已注销（741-742 行），同一对象再次 start_detection() 能正常注册启动，但沿用已 cancelled 的 token，detect() 将立刻经 check_and_raise 退出，710-713 行判 `is_cancelled()` 后丢弃完整分析结果并 emit cancelled。
  - 影响: 复用 worker 对象（API 上允许：start_detection 的注册检查不阻止）时第二次运行必然"秒取消"，结果静默作废。
  - 建议: run() 开头重建 `self._cancel_token = CancelToken()`，或在文档/断言中强制 worker 单次使用。
  - 置信度: 中（当前 Manager 路径每次新建 worker，不触发；属 API 契约陷阱）

- [AUD-18124] [Suggestion] SceneCutWorkerSignals 为死代码，且其 QRunnable 相关注释已过时
  - 位置: src/features/edit/core/scene_cut/worker.py:424
  - 证据:
    ```python
    class SceneCutWorkerSignals(QObject):
        """
        Signals for SceneCutWorker.

        Separated to allow signals from QRunnable (QRunnable cannot emit signals).
        ...
    ```
    SceneCutWorker（462 行起）直接继承 QThread 并自定义同名信号，全仓库仅 __init__.py:107/261 转出口引用该类，无任何实例化点。
  - 影响: 冗余维护面；误导读者以为存在 QRunnable 信号桥接路径。
  - 建议: 删除该类及其导出，或注明保留原因。
  - 置信度: 高（全库 grep 确认无实例化）

- [AUD-18125] [Suggestion] cancel_worker 不支持 clip_id 参数，无法取消基于时间线片段的检测任务
  - 位置: src/features/edit/core/scene_cut/worker.py:867
  - 证据:
    ```python
    def cancel_worker(self, asset_id: str, settings: SceneCutSettings) -> bool:
        key = DetectionTaskKey(asset_id, settings.to_hash())
        worker = self._registry.get_worker(key)
    ```
    DetectionTaskKey.to_string 对 clip_id=None 生成 "...:full"（283-284 行），而 clip 级任务注册键为 "...:<clip_id>"，get_worker 永远查不到，静默返回 False。对比 is_running/cancel_all 均支持或遍历 clip 级键。
  - 影响: 对片段级检测（start_detection 传 clip_id）调用 cancel_worker 永远失败，用户无法经 manager 取消该类任务。
  - 建议: 增加 `clip_id: Optional[str] = None` 参数并透传给 DetectionTaskKey。
  - 置信度: 高

### 通过项
- 线程安全（核心路径）：ActiveTasksRegistry 用 QMutex + QMutexLocker 保护全部读写；register 的原子 check-and-set 正确消除 is_running/register 间 TOCTOU；单例经 threading.Lock 初始化。
- 线程安全（进度）：ProgressThrottler 仅在 worker 线程内被 _handle_progress 访问，无跨线程竞争。
- GPU 资源生命周期 / GLSL·HDR：worker 线程不触碰 OpenGL（模块头声明并经代码核实），无 GPU 资源。
- 信号防递归：worker 信号经 Qt 自动连接回主线程；manager 转发信号不构成回环，无递归路径。
- 异常与取消路径：run() 顶层 except Exception 经 classify_error 分类后 emit error（729-733 行），无宽泛吞错；CancelledError 单独处理；finally 必然注销 registry（除 AUD-18121 罕见路径）。
- 输入验证：classify_error 覆盖文件缺失/无视频流/解码/损坏/内存/OpenCV 分类并给用户建议，顺序合理。
- 性能模式：进度节流 10Hz 设计成立（除 AUD-18122 的尾帧缺陷）；QThread 复用 Qt 事件循环语义正确。
- 类型注解：函数签名、dataclass 字段、返回类型注解完整（`pyqtSignal(object)` 传值类型已在 docstring 说明）。

---

## 批次结论

3 个 Major 集中在：工程乐观锁 revision 被子剪辑流程重置（AUD-18100）、CFR 反向映射的 fps 换算缺失（AUD-18110）、WorkerManager 的 worker 强引用泄漏（AUD-18120）。三者均有调用链实证且影响可累积；其余 13 条 Suggestion 为防御性校验与一致性改进。本批次无 Critical。
