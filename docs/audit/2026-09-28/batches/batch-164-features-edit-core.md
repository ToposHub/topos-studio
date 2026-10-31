# batch-164 审查报告 — src/features/edit/core（delivery_qc / edit_frame_coordinator）

编号区间：AUD-16400–AUD-16499。审查日期：2026-09-28。
说明：`edit_frame_coordinator.py` 文件头标记 DORMANT(2026-08-19 审计，暂无生产入口)，以下发现对将来接线仍然有效。
模型/API 交叉验证依据：`src/features/edit/core/project.py`（MediaAsset）、`src/features/edit/core/timeline_v2.py`（TimelineSequence/EditTrack/SubtitleClip/Rational）、`src/shared/video/frame_provider_service.py`（VideoFrameProviderService/FrameQuality）。

## src/features/edit/core/delivery_qc.py (行数: 393)

### 发现

- [AUD-16400] [Suggestion] reason 分支「素材未在工程中找到」不可达，asset 缺失时误报为「素材标记为离线」
  - 位置: src/features/edit/core/delivery_qc.py:153-161
  - 证据:
    ```python
    offline = bool(getattr(asset, "is_offline", False)) if asset is not None else True
    missing = bool(path) and not os.path.exists(path)
    if offline or missing or asset is None:
        ...
        reason = "素材标记为离线" if offline else (
            "源文件不存在" if missing else "素材未在工程中找到"
        )
    ```
  - 影响: `asset is None` 时 `offline` 被强制为 True，BLOCKING 消息永远显示「素材标记为离线」，而真实原因是素材 ID 在工程中查不到；第三条 reason 文案是死分支，误导排障。
  - 建议: reason 判定改为先判 `asset is None`，再判 offline/missing。
  - 置信度: 高

- [AUD-16401] [Suggestion] `is_virtual` 守卫对真实 MediaAsset 恒为 False（模型无该字段），守卫意图未落地
  - 位置: src/features/edit/core/delivery_qc.py:149
  - 证据:
    ```python
    # 宿主侧虚拟素材（嵌套序列/comp_clip/adjustment 等）本就没有
    # 真实源文件，画面由宿主递归渲染——不按"源文件不存在"阻断。
    if asset is not None and bool(getattr(asset, "is_virtual", False)):
        continue
    ```
  - 影响: 真实模型 `MediaAsset`（src/features/edit/core/project.py:98-118）没有 `is_virtual` 字段，虚拟性由 `media_type`（comp_clip/nested_sequence/adjustment 等）表达。当前因虚拟素材 path 为空且 is_offline=False 而碰巧不误报，但未来任何带占位路径的虚拟素材都会被错误 BLOCKING，且注释与实际保护机制不符。
  - 建议: 改判 `media_type` 是否属于 `_VIRTUAL_TYPES`（与 MediaAsset.__post_init__ 保持同源常量）。
  - 置信度: 高（机制）；当前实际影响低（碰巧不触发）

- [AUD-16402] [Suggestion] `av_tolerance_frames` 未验证非负，负容差使任何（含 0 帧）时长差都触发 WARNING
  - 位置: src/features/edit/core/delivery_qc.py:199（判定）、360-364（入口未校验）
  - 证据:
    ```python
    if video_end > 0 and audio_end > 0 and abs(video_end - audio_end) > tolerance_frames:
    ```
    入口签名为 `av_tolerance_frames: int = DEFAULT_AV_TOLERANCE_FRAMES`，无下界钳制。
  - 影响: 调用方传负数时，音画完全对齐的时间线也会被报 WARNING，QC 报告出现系统性误报。
  - 建议: 入口处 `av_tolerance_frames = max(0, av_tolerance_frames)` 或对负值抛 ValueError。
  - 置信度: 高（行为确定）；触发依赖调用方误传，概率低

- [AUD-16403] [Suggestion] `inspect_output_file` 中 exists 与 getsize 之间存在 TOCTOU，文件消失时抛未捕获 OSError
  - 位置: src/features/edit/core/delivery_qc.py:324-332
  - 证据:
    ```python
    if not os.path.exists(output_path):
        findings.append(QCFinding(
            check_id="output.missing",
            severity=QCSeverity.BLOCKING,
            message=f"导出输出文件不存在：{output_path}",
            details={"path": output_path},
        ))
        return findings
    size = os.path.getsize(output_path)
    ```
  - 影响: 竞窗内文件被删除/改名时 `getsize` 抛 OSError，未被捕获，异常穿透 `run_delivery_qc` 直接打断导出后校验流程（与该模块「检查必须保守、不误伤调用方」的约束相悖）。
  - 建议: 将 `getsize` 纳入 try/except OSError，失败时归入 `output.missing` BLOCKING。
  - 置信度: 中（竞窗极小，但行为真实）

### 通过项

- 线程安全：纯函数 + frozen dataclass，无共享可变状态，无锁需求
- GPU 资源生命周期 / GLSL·HDR 正确性：不涉及（纯模型 + 标准库文件操作，符合模块自身设计约束）
- 信号防递归：不涉及（无 Qt 依赖）
- 状态提交一致性：不涉及（只读检查，无 undo/redo/持久化写入）
- 异常与取消路径：哈希计算 OSError 已捕获降级为 WARNING；各检查对 mock/鸭子类型防御完善（is_offline 非 bool 跳过、metadata fps 类型核验、非 str path 归空）
- 输入验证：project/lookup 可调用性、path 类型、fps 数值类型均有核验（除上述容差与 getsize 竞窗）
- 性能模式：检查均为 O(轨道×片段) 单次遍历，asset 去重避免重复 lookup，无热点
- 类型注解：完整（含返回类型）

## src/features/edit/core/edit_frame_coordinator.py (行数: 856)

### 发现

- [AUD-16410] [Major] cleanup() 每资产只减 1 次引用，ref_count>1 的剪辑在「清理」后永不关闭，泄漏解码器会话
  - 位置: src/features/edit/core/edit_frame_coordinator.py:816-820（cleanup）；443-453（remove_clip 仅减 1 次、到 0 才 close）
  - 证据:
    ```python
        with self._lock:
            asset_ids = list(self._asset_to_clip.keys())

        for asset_id in asset_ids:
            self.remove_clip(asset_id)
    ```
  - 影响: 多个 timeline 片段引用同一资产时 ref_count>1（add_clip 每次调用 +1），cleanup/shutdown 只调用一次 remove_clip，ref_count 降不到 0，`service.close_clip` 永不执行——与「清理所有资源」的 docstring 承诺相反，VideoFrameProviderService 侧会话/解码器泄漏。
  - 建议: cleanup 改为强制关闭：直接遍历 `close_clip(handle.clip_id)` 并清空映射；或循环 release_ref 至 0。
  - 置信度: 高（机制确定；模块当前 DORMANT，未在生产触发）

- [AUD-16411] [Major] 播放热路径 request_frame 未传 allow_master_fallback=False，proxy miss 时同步解码 master 将拖死播放线程
  - 位置: src/features/edit/core/edit_frame_coordinator.py:539
  - 证据:
    ```python
            handle.last_requested_frame = frame_index
            handle.last_quality = quality

        # 请求帧
        frame = self._service.request_frame(clip_id, frame_index, quality)
    ```
  - 影响: 播放态下 `_select_quality` 返回 'proxy'，而 service 端 `request_frame` 默认 `allow_master_fallback=True`，其 docstring 明确要求「实时播放热路径必须传 False（规格书 §6 / Stage C2），避免 proxy miss 时同步解母版拖死播放线程」（src/shared/video/frame_provider_service.py:1282-1284）。本协调器正是编辑页播放帧路径，proxy 缓存未命中时会触发同步 4K master 解码，播放卡顿掉帧。
  - 建议: 播放态（quality=='proxy' 且非暂停）调用时显式传 `allow_master_fallback=False`；暂停/逐帧/导出路径保持 True。
  - 置信度: 高

- [AUD-16412] [Major] request_frame_sync 忙等循环无 sleep/backoff，超时窗口内 100% 空转一个核心并高频轰炸 service
  - 位置: src/features/edit/core/edit_frame_coordinator.py:591-608
  - 证据:
    ```python
        # 循环尝试获取帧（直到超时）
        while (time.perf_counter() - start_time) * 1000 < timeout_ms:
            frame = self._service.request_frame(clip_id, frame_index, quality)
            if frame is not None:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._stats.record_request(
    ```
  - 影响: 帧未就绪时（service 立即返回 None）以无间隔死循环重试最长 300ms：CPU 满载一个线程，同时对 service 提交海量重复请求挤占解码队列；与 4K 实时性能基准冲突。
  - 建议: 循环内加 2-5ms `time.sleep` 退避，或改用 service 的回调/等待原语。
  - 置信度: 高

- [AUD-16413] [Major] 持有不可重入锁期间 emit Qt 信号（clip_error/clip_loaded），直连槽回调任一加锁方法即死锁
  - 位置: src/features/edit/core/edit_frame_coordinator.py:395（clip_error.emit）、409（clip_loaded.emit），均在 `with self._lock:`（379 起）块内
  - 证据:
    ```python
            # 通过服务打开剪辑
            try:
                clip_id = self._service.open_clip(path)
            except Exception as e:
                logger.error(f"Failed to open clip: path={path}, error={e}")
                self.clip_error.emit(asset_id, str(e))
                return None
    ```
  - 影响: `threading.Lock` 不可重入；同线程 DirectConnection 下槽函数在锁内执行，槽内再调用 request_frame/get_clip_handle/remove_clip 等任意加锁方法即自锁，整线程挂死。且 409 处 emit 时句柄尚未写入 `_asset_to_clip`（411 行才写入），直连槽观察到不一致状态。
  - 建议: 在锁外收集待发信号，退出 with 块后再 emit；或将句柄先入表再 emit。
  - 置信度: 高（危险确定）；中（是否实际触发取决于连接方式与槽实现）

- [AUD-16414] [Major] 全局锁内执行重 I/O（open_clip/close_clip/get_clip_info），媒体打开期间阻塞所有并发帧请求
  - 位置: src/features/edit/core/edit_frame_coordinator.py:392（open_clip）、406（get_clip_info）、453（close_clip），均在 `self._lock` 临界区内
  - 证据:
    ```python
            # 通过服务打开剪辑
            try:
                clip_id = self._service.open_clip(path)
            except Exception as e:
    ```
    （remove_clip 内 453 行 `self._service.close_clip(handle.clip_id)` 同样处于锁内）
  - 影响: open_clip 是磁盘 I/O + 解码器初始化（可达数百 ms），期间播放线程的 request_frame 在 518 行锁上排队，画面冻结；4K 实时基准下不可接受。当前仅 539 行的 request_frame 调用在锁外，开/关/查信息均未做锁外化。
  - 建议: 锁内仅做映射表读写；open/close/get_clip_info 移到锁外执行，完成后短暂加锁回写句柄表。
  - 置信度: 高

- [AUD-16415] [Major] _stats 多线程读写无锁且锁纪律不一致，并发下统计丢失更新、均值漂移
  - 位置: src/features/edit/core/edit_frame_coordinator.py:545-550（锁外 record_request）、765（锁外读 `len(self._asset_to_clip)` 并写 active_clips）、789（锁外整体替换 stats 对象）；对照 412-413/455-456（锁内写 stats）
  - 证据:
    ```python
        self._stats.record_request(
            quality=quality,
            cache_hit=cache_hit,
            decode_ms=elapsed_ms,
            wait_ms=elapsed_ms,  # 同步请求，等待=解码
        )
    ```
  - 影响: `record_request` 是多步复合读写（累加+重算均值），播放线程与 UI 线程并发时丢失计数、avg 失真；`reset_stats` 直接换对象与在途 record 竞争；765 行无锁读字典长度与 `active_clip_count` 属性（317 行加锁）纪律相悖。「性能统计收集」是本模块三大职责之一，统计失真即功能缺陷。
  - 建议: 统一 stats 访问锁纪律（全部锁内或改用独立小锁/原子计数）；`_maybe_report_stats` 内读 active_clips 改走加锁属性。
  - 置信度: 高（竞争存在）；后果限于诊断数据失真，不致崩溃

- [AUD-16416] [Suggestion] frame_ready 信号已声明并写入文档，但全文无任何 emit，连接方永远收不到帧
  - 位置: src/features/edit/core/edit_frame_coordinator.py:234（声明）；全文 emit 仅 395/409/766 三处（clip_error/clip_loaded/stats_updated）
  - 证据:
    ```python
        # Signals
        frame_ready = pyqtSignal(str, int, str, object)  # asset_id, frame_index, quality, frame_data
        clip_loaded = pyqtSignal(str, object)  # asset_id, clip_info
    ```
  - 影响: 类 docstring「Signals: frame_ready: 帧就绪」承诺的核心输出不存在，接线时按文档连接该信号将静默无数据，属接口契约与实现不符。
  - 建议: 在 request_frame 成功后 emit frame_ready，或在文档/声明中移除。
  - 置信度: 高

- [AUD-16417] [Suggestion] cache_hit 以 `frame is not None` 判定，真实解码（未命中缓存）也计入命中，cache_hit_rate 失真
  - 位置: src/features/edit/core/edit_frame_coordinator.py:543
  - 证据:
    ```python
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        cache_hit = frame is not None  # 简化判断
    ```
  - 影响: `to_dict` 输出的 cache_hit_rate 恒接近 100%（只要有帧返回），性能诊断不可用，掩盖缓存配置问题。
  - 建议: service 端 request_frame 已统计自身 cache_hits，协调器直接透传 service 统计或让 request_frame 返回命中标志。
  - 置信度: 高

- [AUD-16418] [Suggestion] set_playback_controller 不解除旧控制器连接，重复设置导致双份信号处理
  - 位置: src/features/edit/core/edit_frame_coordinator.py:324-340
  - 证据:
    ```python
        self._playback_controller = controller

        # 连接信号
        controller.frame_changed.connect(self._on_frame_changed)
        controller.state_changed.connect(self._on_state_changed)
    ```
  - 影响: 二次调用（换控制器）后新旧控制器的信号都连着本协调器，状态/帧事件重复处理且旧控制器引用无法释放。
  - 建议: 连接前对旧 controller disconnect，并加幂等保护。
  - 置信度: 中（依赖调用方式，API 未设防）

- [AUD-16419] [Suggestion] _select_quality 未实现文档承诺的「逐帧移动→master」「长时间停帧→升级 master」规则，参数未使用
  - 位置: src/features/edit/core/edit_frame_coordinator.py:732-758
  - 证据:
    ```python
        规则：
        - 暂停/停止：master
        - 播放：proxy
        - 逐帧移动：master
        - 长时间停在同一帧：从 proxy 升级到 master
        ...
        # 暂停/停止时使用 master
        if self._is_paused:
            return 'master'

        # 播放时使用 proxy
        return 'proxy'
    ```
  - 影响: `frame_index` 与 `handle.last_requested_frame`（逐帧/驻留判断所需的输入）完全未参与决策，文档行为与实现不符；暂停逐帧查看时确实返回 master（依赖 _is_paused），但播放中停帧升级不会发生。
  - 建议: 实现驻留升级逻辑或修正 docstring，删除未用参数。
  - 置信度: 高

- [AUD-16420] [Suggestion] quality 参数未校验，非法字符串经 service 静默映射为 AUTO
  - 位置: src/features/edit/core/edit_frame_coordinator.py:501（形参）、539（透传）；映射行为见 src/shared/video/frame_provider_service.py:125-141
  - 证据:
    ```python
        return mapping.get(quality_str.lower(), FrameQuality.AUTO)
    ```
    协调器侧对传入 quality 无任何取值检查即透传。
  - 影响: 调用方拼写错误（如 'prox'）不会报错，静默变成 AUTO 语义，排查困难。
  - 建议: 入口校验 quality ∈ {'proxy', 'master', 'auto'}，非法值记日志并回退默认。
  - 置信度: 高

- [AUD-16421] [Suggestion] get_stats 返回类型注解 `dict[str, float | int]` 与实际嵌套 dict 结构不符
  - 位置: src/features/edit/core/edit_frame_coordinator.py:772-785
  - 证据:
    ```python
        def get_stats(self) -> dict[str, float | int]:
            ...
            return {
                'coordinator': coordinator_stats,
                'service': service_stats,
            }
    ```
  - 影响: 值为 dict 而非标量，注解错误误导静态检查与调用方。
  - 建议: 改为 `dict[str, dict[str, float | int]]`。
  - 置信度: 高

- [AUD-16422] [Suggestion] _is_paused/_current_quality 在 _on_state_changed、on_pause、on_play 无锁写，request_frame 锁内读，锁纪律不一致
  - 位置: src/features/edit/core/edit_frame_coordinator.py:687-697、716-717、724-725（无锁写）；518-535（锁内经 _select_quality 读）
  - 证据:
    ```python
        if state == 'playing':
            self._is_paused = False
            self._current_quality = 'proxy'
        elif state == 'paused':
            self._is_paused = True
            self._current_quality = 'master'
    ```
  - 影响: GIL 下单赋值原子、不致崩溃，但播放态翻转与帧请求读数之间无同步，质量切换可晚一帧生效；且同文件对同类状态两种纪律，维护易引入真实竞争。
  - 建议: 状态字段统一纳入 _lock 保护或声明为 UI 线程独占并在注释中固化约束。
  - 置信度: 高（不一致确定）；影响低

### 通过项

- GLSL·HDR 正确性：不涉及（无着色器/像素数据）
- GPU 资源生命周期：协调器不持有 GPU 对象，解码器生命周期经 service 间接管理（泄漏问题见 AUD-16410，非直接 GPU 句柄）
- 信号防递归：本类信号不驱动程序化控件更新，无递归回路；stats_updated 跨线程 emit 依赖 Qt 自动排队连接，安全
- 状态提交一致性：不涉及（无 undo/redo/revision/持久化）
- 异常与取消路径：add_clip 对 open_clip 的边界宽 except 有日志 + clip_error 信号上报，未吞错；request_frame_sync 有超时并返回 None；_schedule_prefetch 失败降级 warning（注：remove_clip 中 close_clip 若抛异常将使句柄滞留表中且 ref_count 可为负，因 service close_clip 无已知抛错路径未单列，接线时建议补防御）
- 性能模式（除已列发现）：request_frame 的 service 调用在锁外执行；预取经 service 异步且范围钳制在 total_frames 内、lookahead 钳制 1-32
- 类型注解：除 AUD-16421 外完整（含 dataclass 字段与返回类型）
