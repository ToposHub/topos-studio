# 批次报告：batch-206 — src/features/edit/ui/（P1）

- 任务书：docs/audit/2026-09-28/assignments/batch-206.md
- 编号区间：AUD-20600–AUD-20699
- 审查文件：`src/features/edit/ui/scene_cut_dialog.py`（2,740 行，已全文通读）
- 交叉验证依赖：`core/scene_cut/worker.py`、`detector.py`、`frame_sampler.py`、`scene_cut_result_model.py`、`scene_cut_graph_widget.py`、`scene_cut_preview_widget.py`、`scene_cut_shortcuts.py`、`shared/i18n/i18n.py`、`features/edit/locales/*.json`（只读核对）

统计：Critical 1 / Major 3 / Suggestion 7

## src/features/edit/ui/scene_cut_dialog.py (行数: 2740)

### 发现

- [AUD-20600] [Critical] closeEvent 对 wait 超时后仍在运行的预览线程调用 deleteLater，可销毁运行中的 QThread 导致崩溃
  - 位置: src/features/edit/ui/scene_cut_dialog.py:2663-2667（配合 1984-1995）
  - 证据:
    ```python
    for worker in list(self._preview_workers):
        if worker.isRunning():
            worker.requestInterruption()
            worker.wait(1000)
        self._on_preview_worker_finished(worker)
    ```
    `_on_preview_worker_finished` 内部无条件 `worker.deleteLater()`（1993 行）。`PreviewLoadWorker.run` 只在解码前后检查 `isInterruptionRequested()`（1376、1384 行），单次解码无法中断；文件自身注释（1344 行）承认长 GOP/4K/网络媒体解码可远超 1 秒，`wait(1000)` 返回值被忽略。
  - 影响: 解码超过 1 秒时（长 GOP/网络素材），线程仍在运行即被 `deleteLater`；事件循环处理 DeferredDelete 时析构运行中的 QThread（"QThread: Destroyed while thread is still running"），属未定义行为，典型表现为进程崩溃。
  - 建议: 检查 `wait()` 返回值；未结束的线程只置引用不 deleteLater，仅依赖其自身 `finished` 信号回调清理；或对无法中断的解码提供可中断采样。
  - 置信度: 中（结构性路径确凿，触发依赖慢解码素材）

- [AUD-20601] [Major] 持久缓存命中快路径被 sender 守卫整体吞掉：缓存分析永不应用且探测不再启动
  - 位置: src/features/edit/ui/scene_cut_dialog.py:1792-1796 与 2397-2403
  - 证据:
    ```python
    cached_analysis = self._try_load_cache() if self._use_persistent_cache else None
    if cached_analysis is not None:
        self._on_detection_completed(cached_analysis)
        logger.info(f"Loaded cached analysis: {len(cached_analysis.candidates)} candidates")
        return
    ```
    ```python
    def _on_detection_completed(self, analysis: SceneCutAnalysis) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._worker:
            return
    ```
    `_start_detection` 由 `auto_detect_clicked` 信号槽调用（1622 行），在此槽执行期间直接调用 `_on_detection_completed` 时 `self.sender()` 返回 BottomActionBar（非 None、非 `self._worker`），守卫直接 return。
  - 影响: `use_persistent_cache=True`（默认值）时点击 Auto Scene Detect 若命中缓存：`set_analysis`/`detection_completed` 均不执行且函数随即 return——既不显示缓存结果也不启动新探测，按钮表现为无响应；日志还打印"Loaded cached analysis"误导排查。
  - 建议: 缓存路径改为直接调用无守卫的内部方法（如提取 `_apply_analysis_result`），或给 `_on_detection_completed` 增加 `from_signal: bool` 参数区分直调与信号路径。
  - 置信度: 高（PyQt sender() 在槽内嵌套直调时返回外层信号发送者，语义明确）

- [AUD-20602] [Major] 批量接受/拒绝结果弹窗显示字面 `{0}` 占位符，计数未格式化
  - 位置: src/features/edit/ui/scene_cut_dialog.py:2284-2288、2294-2298
  - 证据:
    ```python
    QMessageBox.information(
        self,
        I18N.tr("scene_cut.batch_complete", default="Batch Complete"),
        I18N.tr("scene_cut.accepted_count", default=f"{count} candidates accepted."),
    )
    ```
    翻译文件实测存在该键且含位置占位符：`src/features/edit/locales/zh_CN.json:331` `"accepted_count": "{0} 个候选点已接受。"`、`en_US.json:331` `"{0} candidates accepted."`；`I18N.tr`（i18n.py:157-173）仅在传入 kwargs 时才 format，此处未传 kwargs，命中翻译后原样返回含 `{0}` 的字符串。本文件 487-502 行的 `_format_cut_confirmation_message` 专门为此写了双格式兜底，但批量弹窗没有复用。
  - 影响: 中文/英文等所有已翻译语言下，批量操作完成弹窗显示"{0} 个候选点已接受。"，用户看不到实际数量。
  - 建议: 改用 `I18N.tr(key, count=count)` 风格传 kwargs（需将翻译占位符统一），或复用 `_format_cut_confirmation_message` 的 `template.format(count, count=count)` 模式。
  - 置信度: 高（翻译文件与 I18N.tr 逻辑均已核实）

- [AUD-20603] [Major] UI 线程无超时 `wait()` 等待被取消的探测线程，初始化阶段无法取消可致界面无限冻结
  - 位置: src/features/edit/ui/scene_cut_dialog.py:1782-1784
  - 证据:
    ```python
    # Cancel any running worker
    if self._worker and self._worker.is_running:
        self._worker.cancel()
        self._worker.wait()
    ```
    `SceneCutWorker` 是 QThread（worker.py:462），`wait()` 无参即无限等待；取消检查点仅位于探测采样循环内（detector.py:360-371），而 `sampler.open()`（detector.py:347 → frame_sampler.py:277 `av.open`）在首个检查点之前且不响应 CancelToken。
  - 影响: 探测运行中再次点击 Auto Scene Detect 时，若线程正处于容器打开/探测器构造阶段（网络盘、慢速代理路径），UI 线程被无界阻塞，界面冻结直至该阶段完成；与 closeEvent 中的 `wait(1000)`（2656 行）超时策略也不一致。
  - 建议: 改用带超时的 `wait(timeout_ms)` 循环并允许放弃（旧 worker 终态信号已有 sender 守卫兜底），或在 `VideoFrameSampler.open` 前后增加取消检查。
  - 置信度: 中（本地快速媒体通常在下一个帧检查点即退出；慢速 I/O 场景为无界）

- [AUD-20604] [Suggestion] `_init_frame_provider` 忽略 wait 超时返回值，切换源可在 provider 锁上阻塞 UI 且旧源迟到帧可穿过陈旧守卫
  - 位置: src/features/edit/ui/scene_cut_dialog.py:1641-1648、1660-1688
  - 证据:
    ```python
    if self._preview_worker is not None and self._preview_worker.isRunning():
        self._preview_worker.wait(1000)
    # Close existing provider
    if self._frame_provider is not None:
        self._frame_provider.close()
    ```
    超时后 worker 仍在解码并持有 provider 的 RLock（get_frame 全程持锁，249 行），UI 线程的 `close()` 将阻塞至在途解码完成。另外 `set_source` 未重置 `_current_index`/scrubber，旧 worker 在 wait 窗口内完成并发出的 `frames_ready`（run 的中断检查在 emit 之前，1384-1386 行）可通过 2011-2026 行的候选/帧守卫，把旧素材画面显示到新源预览上。
  - 影响: 切换源瞬间 UI 冻结至慢解码结束；极端时序下预览短暂显示前一素材的画面。当前仓库内 `set_source` 无调用方（控制器每次新建对话框，scene_cut_controller.py:456），属公共 API 的潜在缺陷。
  - 建议: wait 超时后推迟 close 到 worker `finished` 回调；`set_source` 时重置 `_current_index = -1` 并失效在途结果（如代际计数）。
  - 置信度: 中

- [AUD-20605] [Suggestion] Backspace 被文档承诺为拒绝快捷键但未实现
  - 位置: src/features/edit/ui/scene_cut_dialog.py:1419、2629（承诺）；2596-2604（实现）
  - 证据:
    ```python
    - Delete/Backspace: Reject current candidate        # 1419 行 docstring
    ```
    实际处理表仅有 `REJECT_CURRENT_ACTION_ID, "Delete"`（2601-2604 行），Backspace 落入 `super().keyPressEvent` 无动作。
  - 影响: 用户按文档使用 Backspace 无法拒绝候选，行为与文档不一致。
  - 建议: 在 `_handle_shortcut_event` 中为 Backspace 增加与 Delete 相同的处理，或修正两处 docstring。
  - 置信度: 高

- [AUD-20606] [Suggestion] `_get_total_frames` 无 time_index 时返回硬编码 1000，图表时间轴比例失真
  - 位置: src/features/edit/ui/scene_cut_dialog.py:2454-2458
  - 证据:
    ```python
    def _get_total_frames(self) -> int:
        if self._analysis and self._analysis.source_time_index:
            return len(self._analysis.source_time_index.points)
        return 1000  # Default placeholder
    ```
    `set_analysis`/`_sync_graph_candidates` 用该值驱动 `graph.set_candidates` 的横向比例；分析无 time_index 时 10 秒素材与 10 分钟素材都按 1000 帧绘制，而 `_frame_provider.total_frames` 此时往往可用。
  - 影响: 波形图/候选点横向位置与真实素材长度不符，点击图定位帧（frame_clicked 反算）也随之偏移。
  - 建议: 优先 `self._frame_provider.total_frames`，其次 analysis 值，1000 仅作最后回退。
  - 置信度: 高

- [AUD-20607] [Suggestion] 预览 LRU 缓存容量按张数（300 张 512×288×3）折合约 130MB 常驻内存
  - 位置: src/features/edit/ui/scene_cut_dialog.py:172、398-401
  - 证据:
    ```python
    MAX_CACHE_SIZE = 300  # LRU cache capacity
    ...
    def _evict_if_needed(self) -> None:
        while len(self._cache) > self.MAX_CACHE_SIZE:
            self._cache.popitem(last=False)
    ```
    每张 QImage 为 `qimg.copy()` 独立内存（393 行），512×288×3 ≈ 442KB，300 张 ≈ 132MB，仅服务三面板预览。
  - 影响: 长时间浏览长素材时对话框独占逾百 MB 内存；与其他缓存（探测磁盘缓存等）叠加放大峰值。
  - 建议: 按字节预算驱逐（如 ≤48MB），或缩短预览采样尺寸后再提高张数。
  - 置信度: 高（数值按现有 PREVIEW_SAMPLE_WIDTH/HEIGHT 常量估算）

- [AUD-20608] [Suggestion] `_try_load_cache` 宽泛 `except Exception` 且无任何日志，掩盖缓存领域错误
  - 位置: src/features/edit/ui/scene_cut_dialog.py:1858-1876
  - 证据:
    ```python
    try:
        import os
        stat = os.stat(self._source_path)
        cache_key = generate_cache_key(...)
        cache = SceneCutCache()
        return cache.load_analysis(cache_key)
    except Exception:
        return None
    ```
    文件被删除（FileNotFoundError）、缓存损坏、反序列化失败等不同原因全部静默归并为"缓存未命中"。
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"；缓存写入/读取故障无法从日志定位，用户只会感到探测总是全量重跑。
  - 建议: 至少 `logger.debug(..., exc_info=True)`；对预期异常（缓存未命中）与意外异常分层捕获。
  - 置信度: 高

- [AUD-20609] [Suggestion] 手动剪切候选的 seconds 使用 `_playback_fps`，provider 打开失败时回落 24.0 产生错误时间码
  - 位置: src/features/edit/ui/scene_cut_dialog.py:2251-2260
  - 证据:
    ```python
    candidate = SceneCutCandidate(
        source_frame=frame,
        pts=None,
        seconds=frame / max(self._playback_fps, 1.0),
    ```
    `_sync_playback_source`（2138-2139 行）在 provider 未打开时把 `_playback_fps` 置 24.0；30/60fps 素材在 provider 打开失败（如编码器不支持）但存在分析数据时可添加手动剪切。
  - 影响: 候选元数据 seconds 与真实时间不符，下游 subclip 划分/时间码显示随之偏移。
  - 建议: 构建候选前从 `analysis.fps`（若可用）取帧率，取不到时禁用手动剪切或明确提示。
  - 置信度: 中

- [AUD-20610] [Suggestion] 多处弱类型注解：`Optional[Any]` 与事件对象用 `Any` 而非具体 Qt 类型
  - 位置: src/features/edit/ui/scene_cut_dialog.py:194、1054-1057、2612、2640
  - 证据:
    ```python
    def set_time_index(self, time_index: Optional[Any]) -> None:   # 194
    def _on_selection_changed(self, selected: Any, deselected: Any) -> None:  # 1054
    def eventFilter(self, watched: Any, event: Any) -> bool:       # 2612
    def closeEvent(self, event: Any) -> None:                      # 2640
    ```
  - 影响: `Optional[Any]` 等价于 Any，失去静态检查价值；QCloseEvent/QEvent/QItemSelection 均有具体类型可标注，违反项目强类型约束的可执行性。
  - 建议: `set_time_index` 标注为 `Optional["SourceFrameTimeIndex"]`（TYPE_CHECKING 导入）；事件参数改用 `QCloseEvent`、`QEvent`、`QItemSelection`。
  - 置信度: 高

### 通过项

- 信号防递归：`_select_candidate`/`SceneListWidget.select_row`/`SceneListWidget._on_selection_changed` 的 `_updating` 状态锁链路完整（含模型 `set_current_index` 重入 `selection_changed` 的路径）；scrubber `valueChanged → _seek_to_frame` 的同步递归为受控一层且有注释说明；`set_show_on_timeline_checked` 用 blockSignals 防回灌。
- GPU 资源生命周期：本文件无 OpenGL/ModernGL 调用，预览解码线程仅做 numpy→QImage 转换，QImage `copy()` 解耦 numpy 生命周期（393 行）。
- GLSL·HDR 正确性：不涉及（纯 UI/解码文件，无着色器与 HDR 数据路径）。
- 异常与取消路径（除 AUD-20601/20603 外）：探测 completed/error/cancelled 三条终态均恢复 `_detection_decoding_paused` 与挂起的代理生成，closeEvent 另有兜底恢复；陈旧 worker 终态信号按 sender 身份丢弃（2401-2403 行）。
- 输入验证（除 AUD-20609 外）：seek/帧号统一 clamp（254、2075 行）；手动剪切做 clip 范围校验与重复帧查重（2228-2249 行）；provider 打开失败降级并有日志。
- 状态提交一致性：决策/增删剪切均通过 `to_updated_analysis` 同步回 `_analysis`，`get_analysis` 每次重建保证外部读取一致；探测完成后按 `use_persistent_cache` 落盘（undo/redo 不在本对话框职责范围）。
- 线程安全（除 AUD-20600/20604 外）：`PreviewFrameProvider` 全程 RLock 保护 sampler 与缓存；预览请求单飞行 + pending 合并避免线程堆积；跨线程仅传递 QImage；`_on_preview_worker_finished` 对 closeEvent 与 finished 信号双重调用幂等。
- 性能模式：探测期间冻结预览解码与后台代理转码避免 CPU 争抢；顺序播放走 `sample_next_frame` 增量解码；进度信号经 worker 侧 10Hz 节流。
