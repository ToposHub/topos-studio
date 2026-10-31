# 批次报告：batch-214 [P1] src/features/edit/ui//

- 任务书：docs/audit/2026-09-28/assignments/batch-214.md
- 编号区间：AUD-21400–AUD-21499
- 审查日期：2026-09-28
- 审查方式：全文件通读（15,708 行，分 18 段 Read），交叉 grep 验证调用点与依赖签名

## src/features/edit/ui/timeline_view.py (行数: 15,708)

QGraphicsView 时间线主视图：轨道/片段/转场/标记/字幕条目管理、拖放、吸附、
增量更新、命令栈提交。纯 UI 层，不含 OpenGL/GLSL/HDR 数据路径。

### 发现

- [AUD-21400] [Major] 间隙指示器（Phase 3.3）在全部生产调用点被 `_updating` 锁短路，功能完全失效
  - 位置: src/features/edit/ui/timeline_view.py:2020、6064、15556
  - 证据:
    ```python
    # load_timeline（第 1565 行已置 self._updating = True）内：
    # Update gap indicators (Phase 3.3)
    self._update_gap_indicators()          # 行 2020

    # update_timeline_incremental（第 5858 行已置 self._updating = True）内：
    # Update gap indicators
    self._update_gap_indicators()          # 行 6064

    # _update_gap_indicators 自身首行：
    def _update_gap_indicators(self) -> None:
        if self._updating:                 # 行 15556 — 两个调用点都在锁内，恒真
            return
    ```
    另：`_update_gap_indicators_for_track`（行 15627）在 src/ 全仓库无任何调用者（已 grep 验证）。
  - 影响: 时间线加载与所有增量更新路径中 `_update_gap_indicators()` 均为空操作；
    GapIndicatorItem 永远不会被创建或刷新，"间隙指示"功能整体不存在，
    `_gap_items` 恒为空（`update_gap_visibility` 因此也无事可做）。
  - 建议: 让 `load_timeline`/`update_timeline_incremental` 在锁释放后调用
    （参照 `set_zoom` 对 `_update_clip_visibility` 的锁后补调用模式），或将
    `_update_gap_indicators` 改为内部不可重入锁；同时决定 `_update_gap_indicators_for_track`
    的去留。
  - 置信度: 高

- [AUD-21401] [Major] 增量更新中 `set_sync_locked` 被嵌套在 `is_locked` 分支内，sync-lock 单独变化时表头仍失步
  - 位置: src/features/edit/ui/timeline_view.py:6033-6037
  - 证据:
    ```python
    if 'is_locked' in track_changes:
        header.set_locked(track_changes['is_locked'])
        if 'is_sync_locked' in track_changes and hasattr(
                header, 'set_sync_locked'):
            header.set_sync_locked(track_changes['is_sync_locked'])
    if 'is_visible' in track_changes:
        header.set_visible(track_changes['is_visible'])
    ```
    而变更检测器将 sync-lock 作为独立键写入（行 5512-5514）：
    ```python
    if (hasattr(old_track, 'is_sync_locked')
            and old_track.is_sync_locked != new_track.is_sync_locked):
        changes['is_sync_locked'] = new_track.is_sync_locked
    ```
  - 影响: 当一次命令（如 undo 撤销 SetTrackSyncLockCommand）只改变 sync-lock 而不改
    lock 时，`track_changes` 只含 `is_sync_locked`，嵌套条件使 `header.set_sync_locked`
    永不执行——正是行 5510-5514 "审计修复"注释声称要解决的表头失步问题依旧存在。
  - 建议: 把 `if 'is_sync_locked' in track_changes:` 提升为与 is_muted/is_solo/is_locked
    平级的独立分支。
  - 置信度: 高

- [AUD-21402] [Major] 常规片段拖拽/修剪的"排除自身"吸附从未传给 SnapManager，旧版排除点集合被整体旁路
  - 位置: src/features/edit/ui/timeline_view.py:6795-6797、7367-7368、7948-7949、8128-8129、8181-8182、8360-8361、9036-9037、9101-9103、10974-10981
  - 证据:
    ```python
    # _on_clip_moved_impl（拖拽预览）：
    if self._snap_enabled:
        if self._snap_manager is None and not session.snap_points:   # 恒为 False
            session.snap_points = self._get_snap_points(exclude_clip_id=clip_id)
        snapped_start, did_snap = self._snap_frame(new_start, session.snap_points)
    ```
    `_snap_frame` 在 SnapManager 可用时走管理器路径且只透传 exclude 参数（行 10974-10981）：
    ```python
    if self._timeline is not None and self._snap_manager:
        snapped_frame, did_snap = self._snap_manager.find_nearest_frame(
            frame, self._pixels_per_frame,
            exclude_types=exclude_types,
            exclude_source_ids=exclude_source_ids,   # 调用方未传 → None
        )
    ```
    对比字幕路径正确传参（行 3150-3155）：
    ```python
    snapped_start, _ = self._snap_frame(
        new_start, tuple(), show_indicator=True,
        exclude_source_ids={clip_id},
    )
    ```
    SnapManager 的 `find_nearest_frame`/`update_from_timeline` 均支持
    `exclude_source_ids`/`exclude_clip_ids`（timeline_ops.py:2820/3043 已核实）。
  - 影响: 移动中的片段会把自身的原始入/出点当作吸附点（微小拖拽被吸回原位、贴滞），
    左/右修剪时边缘可吸附到本片段另一条边缘；`_get_snap_points(exclude_clip_id=...)`
    的计算结果被完全丢弃。与字幕处理路径行为不一致，注释声明的"exclude_clip_id
    (for moving clips)"语义未生效。
  - 建议: 在 `_on_clip_moved_impl`、`_on_drag_finished_impl`、各 trim finished 及
    `_snap_frame` 的这些调用点显式传 `exclude_source_ids={clip_id}`。
  - 置信度: 高（代码级失配确定；交互影响为推断）

- [AUD-21403] [Major] `_debounce_thumbnail_schedule` 每次调用新建 QTimer 且旧对象不释放，滚动/缩放期间持续累积泄漏
  - 位置: src/features/edit/ui/timeline_view.py:2733-2742
  - 证据:
    ```python
    def _debounce_thumbnail_schedule(self) -> None:
        if self._scroll_debounce_timer is not None:
            self._scroll_debounce_timer.stop()

        self._scroll_debounce_timer = QTimer(self)      # 每次 new，父对象是 self
        self._scroll_debounce_timer.setSingleShot(True)
        self._scroll_debounce_timer.setInterval(80)
        self._scroll_debounce_timer.timeout.connect(self._submit_viewport_thumbnails)
        self._scroll_debounce_timer.start()
    ```
  - 影响: 旧 QTimer 被 stop 但从未 deleteLater；由于其 parent 是视图，C++ 对象在视图
    销毁前一直存活（含 signal 连接）。每次 wheel/scroll 事件触发一次本函数，长时间
    编辑会话中对象数随滚动事件线性增长（内存泄漏 + 无谓的连接对象）。
    `set_playback_active_for_thumbnails`、`scrollContentsBy`、`set_zoom` 均会触发。
  - 建议: 在 `__init__` 中创建单例 single-shot QTimer 并复用（仅 start/stop），
    或替换前对旧 timer 调 `deleteLater()`。
  - 置信度: 高

- [AUD-21404] [Major] Alt 拖拽复制 solid 素材时，命令失败路径已在项目里留下孤儿 asset 且不广播 project_changed
  - 位置: src/features/edit/ui/timeline_view.py:7815-7857、7913-7924
  - 证据:
    ```python
    # 命令构建循环内（宏尚未执行）：
    if orig_asset is not None and getattr(orig_asset, "media_type", None) == "solid":
        new_asset = MediaAsset(...)
        ...
        self._project = EditProject(...)      # 项目在执行前已被替换，含新 asset

    ...
    try:
        ...
        self._execute_command(macro)
    except ValueError as exc:
        logger.warning(f"Alt-drag copy rejected: {exc}")
        return                                # 不回滚 self._project，不发 project_changed
    if self._project is not None:
        self.project_changed.emit(self._project)
    ```
  - 影响: 宏被模型校验拒绝（如重叠）时，新建的 Solid Color asset 已进入
    `self._project.assets` 且无任何 clip 引用（正是 FX-H03 针对 generator 修复的同类
    孤儿问题），还会被后续保存持久化；同时媒体池等外部监听者未收到
    `project_changed`，状态失步。另：手工枚举 EditProject 全部字段重建对象的方式脆弱。
  - 建议: 参照 generator 路径的 `GeneratorTransactionGuard`/`build_generator_clip_transaction`
    事务模式：asset 随宏命令原子提交，失败时回滚项目引用。
  - 置信度: 中（失败路径触发条件较窄，但代码顺序缺陷明确）

- [AUD-21405] [Suggestion] `add_transition_at_cut_point`/`add_transition_at_clip_edge` 返回注解 `-> None` 与实际返回 str(错误) 矛盾
  - 位置: src/features/edit/ui/timeline_view.py:9887-9954、9956-10005
  - 证据:
    ```python
    def add_transition_at_cut_point(
        self, ..., transition_params: Optional[dict[str, Any]] = None,
    ) -> None:
        ...
        except ValueError as e:
            logger.warning(f"Cannot add transition: {e}")
            return str(e)  # 审计修复：失败原因上报（不再静默当成功）
    ```
    调用方 `apply_transition_from_panel` 按字符串消费：`return self.add_transition_at_cut_point(...) or ""`（行 10089）。
  - 影响: 注解与契约不符，误导类型检查与调用方。
  - 建议: 改为 `-> Optional[str]`（空串/None 表示成功）。
  - 置信度: 高

- [AUD-21406] [Suggestion] 多处用户可见字符串硬编码英文，绕过 I18N 工具
  - 位置: src/features/edit/ui/timeline_view.py:3064、3619-3620、3629-3633、11875-11879、11900-11906
  - 证据:
    ```python
    open_action = menu.addAction("Open Motion Graphic Editor")            # 3064
    graphic_menu = menu.addMenu("Caption Graphic")                        # 3619
    detach_action = menu.addAction("Detach Caption Graphic")              # 3629
    new_name, accepted = QInputDialog.getText(
        self, "Rename Track", "Enter new track name:", text=current_name) # 11875-11879
    ```
  - 影响: 与同文件大量 `I18N.tr(...)` 用法不一致，非英文 locale 下这些菜单/对话框
    仍是英文。
  - 建议: 统一走 `I18N.tr` 并补词条。
  - 置信度: 高

- [AUD-21407] [Suggestion] 多处宽泛 `except Exception` 静默吞掉领域错误，违反 .clinerules 红线
  - 位置: src/features/edit/ui/timeline_view.py:3948-3949、8031-8032、8052-8053、8078-8079
  - 证据:
    ```python
    except Exception as e:
        logger.debug("Trim monitor update skipped: %s", e)   # 只 debug 级
    ...
    except Exception:  # pragma: no cover - optional teaching feature
        return 1       # _motion_graphic_page_count，完全静默
    ...
    except Exception:
        return         # _on_motion_graphic_duration_changed/_finished
    ```
  - 影响: Trim Monitor 与 Motion Graphic retime 的真实错误（含领域校验错误）被降级或
    完全吞掉，问题只在行为异常时间接可见，难以排查。
  - 建议: 收窄为预期异常类型（ImportError/ValueError/TypeError），其余至少
    `logger.exception` 后再降级。
  - 置信度: 高

- [AUD-21408] [Suggestion] 锁轨数值 trim 的用户反馈走 `getattr(self,'status_bar')` 死路径，提示永不显示
  - 位置: src/features/edit/ui/timeline_view.py:3966-3970
  - 证据:
    ```python
    if self._is_track_locked(track_id):  # 审计修复：锁轨不绕过
        status_bar = getattr(self, 'status_bar', None)
        if status_bar is not None:
            status_bar.showMessage("Track is locked", 3000)
        return
    ```
    `status_bar` 在本文件仅此一处（grep 已核实）；TimelineView 是 QGraphicsView，
    从未定义该属性，且类上已有 `status_message_requested` 信号（冻结帧失败路径即用它，行 10466）。
  - 影响: 锁轨时执行数值 trim 无任何反馈，用户以为按键失效。
  - 建议: 改为 `self.status_message_requested.emit(I18N.tr(...))`。
  - 置信度: 高

- [AUD-21409] [Suggestion] `TimelineChanges` 的 `track_changes`/`subtitle_track_changes` 仍是可变 dict 默认值，与 BUG-07 修复声明不一致
  - 位置: src/features/edit/ui/timeline_view.py:147、154
  - 证据:
    ```python
    # BUG-07 fix: Use immutable tuple defaults instead of mutable list defaults
    added_clips: tuple[ClipInstance, ...] = ()
    ...
    track_changes: Dict[str, Dict[str, Any]] = {}   # NamedTuple 类级默认，全实例共享
    ...
    subtitle_track_changes: Dict[str, Dict[str, Any]] = {}
    ```
  - 影响: NamedTuple 的类级默认在所有未显式传该字段的实例间共享同一个 dict；
    当前代码只读不写故未爆雷，但任何未来对 `changes.track_changes` 的原位修改都会
    污染默认值，且与紧邻注释声明的修复目标自相矛盾。
  - 建议: 保持检测器总是显式传参，或改用 `Mapping` 语义的不可变结构/工厂默认。
  - 置信度: 高（隐患性质）

- [AUD-21410] [Suggestion] `_layout_track_items` 在每轨道循环内重复调用 `all_ordered_tracks()`（O(n²)），且三个分区滚动条每个 tick 都触发全量重排
  - 位置: src/features/edit/ui/timeline_view.py:1184、1305、1506-1526
  - 证据:
    ```python
    for track in self._timeline.video_tracks + self._timeline.audio_tracks:
        ...
        # Find the visual index for this track
        all_ordered = engine.all_ordered_tracks()   # 循环体内每轨重算
    ...
    def _on_video_scrollbar_changed(self, value: int) -> None:
        ...
        self._layout_track_items()
        self.viewport().update()                    # 每个滚动 tick 全量重排+全量重绘
    ```
  - 影响: 轨道数多时布局复杂度平方级；`_layout_track_items` 还在
    `_sync_split_vertical_scrollbars`/resize/splitter drag 等热路径被反复调用，
    滚动流畅度受损（与文件内大量精细的 60fps 优化相悖）。
  - 建议: 将 `all_ordered_tracks()` 提升到循环外一次计算；滚动路径只更新必要
    item 的 y 偏移或缓存布局签名短路。
  - 置信度: 高

- [AUD-21411] [Suggestion] 若干方法缺失或错误的类型注解
  - 位置: src/features/edit/ui/timeline_view.py:11947（`_show_empty_area_track_menu(self, global_pos, scene_pos)` 参数无类型）、2797（`_on_thumbnail_batch_ready(self, jobs)`）、6319-6326（`_make_subtitle_drag_clamp_callback` 无返回注解，闭包参数无类型）、11103（`set_linked_selection_enabled(self, enabled: bool):` 缺 `-> None`）、9887/9956（返回注解错误，见 AUD-21405）
  - 证据:
    ```python
    def _show_empty_area_track_menu(self, global_pos, scene_pos) -> None:
    ...
    def _on_thumbnail_batch_ready(self, jobs) -> None:
    ...
    def set_linked_selection_enabled(self, enabled: bool):
    ```
  - 影响: 与文件整体"强类型"约束（.clinerules）不一致，IDE/类型检查覆盖出现空洞。
  - 建议: 补齐参数与返回注解（jobs: Iterable[ThumbnailJob]，global_pos: QPoint 等）。
  - 置信度: 高

- [AUD-21412] [Suggestion] 外部文件拖放在 UI 线程逐个同步探测媒体元数据，多文件时阻塞交互
  - 位置: src/features/edit/ui/timeline_view.py:13783-13792
  - 证据:
    ```python
    for path in file_paths:
        ...
        try:
            from src.features.edit.core.project import probe_media_metadata, get_media_type
            metadata = probe_media_metadata(path)     # 同步探测（dropEvent 内）
            media_type = get_media_type(path)
            new_project = self._project.import_asset(...)
    ```
  - 影响: dropEvent 在 UI 线程同步执行；拖入多个/大体积文件时逐个探测，
    界面冻结可达秒级，违背实时交互基准。
  - 建议: 复用媒体池的异步导入管线，或在 worker 上探测后回发结果再执行命令。
  - 置信度: 中（probe 的实际耗时未在本批次验证）

### 通过项
- GPU 资源生命周期：本文件刻意保持 raster 视口（构造注释说明避免 macOS 上
  QOpenGLViewport 崩溃），无任何 ModernGL/OpenGL 调用；场景 item 在替换前
  `dispose()` + `_invalidate_timeline_media_requests()` 防止悬挂回调（行 2038-2046）。
- GLSL·HDR 正确性：纯视图层，不涉及着色器与 HDR 像素数据，无 clamp/字面量问题（不适用）。
- 信号防递归：`_updating` 锁在 set_playhead/set_selection/set_zoom/load_timeline/
  update_timeline_incremental 一致使用；已核实 TrackHeaderItem 程序化 setter
  （set_muted/set_locked/set_sync_locked 等，timeline_items.py:8223-8251）不发射信号，
  表头命令路径无回环。
- 状态提交一致性：所有编辑经 `_execute_command`→CommandStack；批量拖放聚合
  MacroCommand（单次 undo/view 刷新）；linked A/V 联动在数值 trim/步进 trim 失败时
  整体拒绝；undo/redo 后全量 reload 并发射 timeline_changed。
- 异常与取消路径：Esc 取消恢复拖拽组/ghost/字幕组原位；`_on_drag_finished`
  try/finally 保证会话与 ghost 清理；跨轨字幕提交用 QTimer.singleShot 避免删除
  事件接收者；drop 宏失败时回滚并保持事件可恢复。
- 输入验证：外部路径过滤（本地、可读、is_media、归一去重，行 12868-12886）；
  字幕导入异常捕获（OSError/UnicodeError/ValueError）；拖放目标锁轨/类型校验
  （resolve_drop_target + locked_ids）。
- 标记增量更新：`update_marker` 传标量字段而非模型对象（此前 TypeError 已修复，行 6011-6023）。
- Marker/时间码跳转：`_move_playhead_to` 钳制到序列末帧，经公共 set_playhead 发射变更。
- 命令失败上报：过渡添加失败返回原因字符串、冻结帧失败走 status_message_requested（无静默成功路径）。
