# batch-027 [P1] — src/app/controllers/edit_controller.py

审查范围：`src/app/controllers/edit_controller.py`（9,861 行，全文通读）。
编号区间：AUD-2700–AUD-2799（实际使用 AUD-2700–AUD-2720）。
跨文件事实均已用只读 grep/Read 验证（EditPage 无 `_command_stack`；`_current_edit_page` 全库无赋值；
`MacroCommand.apply` 恒返回二元组；`EditProject`/`MediaAsset` 字段清单；`RenderCacheWorker` 无 parent）。

## src/app/controllers/edit_controller.py (行数: 9,861)

### 发现

- [AUD-2700] [Critical] Bypass Composite 处理函数引用全库不存在的属性/方法，右键菜单触发即 AttributeError
  - 位置: src/app/controllers/edit_controller.py:6888-6893
  - 证据:
    ```python
    timeline = self._current_edit_page.current_timeline() if self._current_edit_page else None
    if timeline:
        updated_timeline = timeline.replace_clip(new_clip)
        self._current_edit_project = self._current_edit_project.with_timeline(updated_timeline)
        if self._current_edit_page:
            self._current_edit_page.load_timeline(updated_timeline)
    ```
  - 影响: 该信号已实际接线（dock_layout_controller.py:521 `bypass_composite_requested.connect(self._on_bypass_composite_requested)`，源自时间线片段右键菜单）。`self._current_edit_page` 在全仓库无任何赋值点（grep 仅命中本文件这 3 行），第一条语句即抛 AttributeError；即使属性存在，后续 `EditPage.current_timeline()`/`load_timeline()`、`TimelineSequence.replace_clip`、`EditProject.with_timeline` 也均不存在（已逐一验证）。用户点 Bypass Composite 必然崩溃且功能完全失效，composite_bypassed 状态永远无法切换。
  - 建议: 改用 `self.edit_page._current_timeline` + `timeline.with_track_updated(track.with_clip_updated(new_clip))` 的既有不可变更新模式（参考本文件 8270-8282 行 comp clip 创建路径），并通过 TimelineView.execute_command 提交以获得 undo。
  - 置信度: 高

- [AUD-2701] [Critical] Solid 生成器参数修改用手工构造函数重建 EditProject，静默丢弃 smart_bins 并将 revision 归零
  - 位置: src/app/controllers/edit_controller.py:3696-3713（`_on_edit_generator_color_changed`）；3769-3786（`_on_edit_generator_params_changed`）
  - 证据:
    ```python
    new_project = project.__class__(
        project_id=project.project_id,
        name=project.name,
        assets=new_assets,
        bins=project.bins,
        timelines=project.timelines,
        compositions=project.compositions,
        color_stacks=project.color_stacks,
        comp_clips=project.comp_clips,
        render_cache_manifests=project.render_cache_manifests,
        ...
    )
    ```
  - 影响: `EditProject`（project.py:981-1008）还有 `smart_bins: dict = {}`（P1-12 用户自建 Smart Bin）与 `revision: int = 0`（TAEP P0 乐观锁版本号）字段，上述构造未传 → 用户每改一次生成器颜色/参数，全部 Smart Bin 被清空、revision 归零；保存后永久丢失（数据丢失），且 AI plan 的 base_revision 一致性比较被破坏。
  - 建议: 改用 `dataclasses.replace(project, assets=new_assets, modified_at=...)`，与文件其余部分一致；禁止手工罗列 frozen dataclass 字段。
  - 置信度: 高

- [AUD-2702] [Critical] 生成器重建 MediaAsset 时丢弃 keywords/rating/flag/color_label/comment 日志字段
  - 位置: src/app/controllers/edit_controller.py:3676-3692；3749-3765
  - 证据:
    ```python
    # MediaAsset is frozen; rebuild via dataclass replace.
    from src.features.edit.core.project import MediaAsset
    new_asset = MediaAsset(
        asset_id=asset.asset_id,
        path=asset.path,
        media_type=asset.media_type,
        display_name=asset.display_name,
        metadata=new_meta,
        ...
        take=asset.take,
    )
    ```
  - 影响: `MediaAsset`（project.py:98-119）含 `keywords/rating/flag/color_label/comment`（P1-12 素材日志），构造时未传 → 用户为该 Solid 素材设置的评级、旗标、颜色标签、关键词、注释在每次颜色/参数修改时被静默重置为默认并随保存持久化丢失（数据丢失）。注释声称 "rebuild via dataclass replace" 但实际是手工构造。
  - 建议: 同 AUD-2701，改用 `replace(asset, metadata=new_meta)`。
  - 置信度: 高

- [AUD-2703] [Critical] 片段 Transform 拖拽结束的提交命令在真实运行环境中永不执行，编辑结果可随保存丢失
  - 位置: src/app/controllers/edit_controller.py:9849-9857
  - 证据:
    ```python
    try:
        if hasattr(self.edit_page, '_command_stack'):
            new_timeline = self.edit_page._command_stack.execute(command, timeline)
            self.edit_page._current_timeline = new_timeline
        # Refresh the properties panel so position/scale/rotation numbers
        # reflect the dragged values. Full reload is fine here — drag end
        # is a one-shot, low-frequency event.
        refreshed_clip = (self.edit_page._current_timeline
                          .get_clip_by_id(clip_id))
    ```
  - 影响: `EditPage` 全文件无 `_command_stack`（本文件三处注释 2075-2077/8508-8509/8567-8568 也明确 "EditPage 没有 _command_stack"），条件恒为假且无 else 分支 → 拖拽结束命令被静默丢弃。最终变换仅存在于拖拽期间直接 apply 的 `edit_page._current_timeline` 与渲染器快照，`TimelineView._timeline`、`_current_timeline_sequences`（保存/自动保存取值源）仍是拖拽前状态：无 undo 记录，保存工程后变换丢失，下一次任意 TimelineView 命令即回滚该编辑。
  - 建议: 与 `_on_edit_clip_param_changed` 等一致补上 else 分支（`command.apply(timeline)` + `_sync_timeline_to_views`），或统一改走 `TimelineView.execute_command`（本文件 8567-8573 的推荐路径）。
  - 置信度: 高

- [AUD-2704] [Major] `_on_grading_changed_for_color_stack` 中 `trackings`/`video_denoise` 仅在 `engine.layers` 非空时赋值，空层时 UnboundLocalError
  - 位置: src/app/controllers/edit_controller.py:1759-1806
  - 证据:
    ```python
    if self.engine.layers:
        layer = self.engine.layers[0]
        ...
        tracking_store = getattr(self.engine, 'tracking_store', None)
        trackings = tracking_store.to_dict() if tracking_store else None
        vd_params = getattr(self.engine, 'video_denoise_params', None)
        video_denoise = (
            vd_params.to_dict() if vd_params is not None else None)

    command = UpdateColorStackCommand.create(
        ...
        trackings=trackings,
        video_denoise=video_denoise,
    )
    ```
  - 影响: `curves_data`…`raw_params` 都在 if 前初始化为 None，唯独 `trackings`/`video_denoise` 没有。当引擎无活动 layer（如源未加载/卸载后）提交调色时抛 UnboundLocalError，本次调色修改丢失且槽函数异常。
  - 建议: 在 if 块前补 `trackings = None`、`video_denoise = None`。
  - 置信度: 高（代码缺陷确凿；触发频率取决于 engine.layers 为空的状态可达性，标中低）

- [AUD-2705] [Major] 标题模板 fallback 分支未解包 `MacroCommand.apply` 的 `(timeline, macro)` 二元组，`_current_timeline` 变成 tuple
  - 位置: src/app/controllers/edit_controller.py:8575-8577（对照 4068-4078）
  - 证据:
    ```python
    else:
        new_timeline = macro.apply(timeline)
        self.edit_page._current_timeline = new_timeline
    ```
    同文件 4068-4073 已明确该契约："MacroCommand.apply (and any compound command) returns a ``(new_timeline, updated_command)`` tuple … otherwise the tuple gets stored as the "current timeline" and the next get_clip_by_id crashes"。
  - 影响: 无 TimelineView 的宿主（测试壳/降级环境）走该分支时，`edit_page._current_timeline` 被赋为 tuple，后续任何 `get_clip_by_id`/`get_subtitle_clip_by_id` 调用抛 `AttributeError: 'tuple' object has no attribute 'get_clip_by_id'`，标题模板功能崩溃。`on_subtitle_interaction_updated`（9429-9430）与 `on_clip_transform_drag_updated`（9795-9796）都有 `isinstance(result, tuple)` 防御，此处遗漏。
  - 建议: 与 4074-4078 相同的解包处理。
  - 置信度: 高

- [AUD-2706] [Major] RenderCacheWorker 无 parent、结束时直接删除最后引用，存在 QThread 运行中被析构的崩溃竞态
  - 位置: src/app/controllers/edit_controller.py:7867-7876（启动/登记）、7953-7955 与 8022-8024（完成/失败时 `del`）
  - 证据:
    ```python
    worker.progress_changed.connect(self._on_render_cache_progress)
    worker.finished_ok.connect(self._on_render_cache_finished)
    worker.failed.connect(self._on_render_cache_failed)
    ...
    self._render_cache_workers[manifest.cache_id] = worker

    worker.start()
    ```
    ```python
    if hasattr(self, '_render_cache_workers') and cache_id in self._render_cache_workers:
        del self._render_cache_workers[cache_id]
    ```
  - 影响: facade（facade.py:114-118）创建 worker 时不传 parent，QThread 无 Qt 父对象；`finished_ok`/`failed` 在 `run()` 内 emit、经队列投递到主线程，回调执行时工作线程可能尚未完全退出，此刻 `del` 掉字典中的最后 Python 引用会触发 C++ QThread 析构 → "QThread: Destroyed while thread is still running"，通常直接终止进程。同文件 6577 行的 MulticamSyncWorker 已用安全模式 `worker.finished.connect(worker.deleteLater)`，此处未用。
  - 建议: 为 render-cache worker 统一加 `worker.finished.connect(worker.deleteLater)`（并在 del 字典条目前确认 `isFinished()`）。
  - 置信度: 中（竞态窗口取决于时序，但缺失防护属实）

- [AUD-2707] [Major] 属性面板的剪辑参数/效果/转场命令在 EditPage 无 `_command_stack` 时全部走直接 apply，绕过 undo 栈
  - 位置: src/app/controllers/edit_controller.py:2130-2136、3537-3540、3886-3891、3958-3964、4064-4079、4170-4175、4257-4263、4321-4327、4386-4391、4528-4533、4609-4615（共 12 处 `hasattr(self.edit_page, '_command_stack')`）
  - 证据:
    ```python
    # Execute via edit_page command stack
    if hasattr(self.edit_page, '_command_stack'):
        new_timeline = self.edit_page._command_stack.execute(command, timeline)
        self.edit_page._current_timeline = new_timeline
    else:
        # Direct apply if no command stack
        new_timeline = command.apply(timeline)
        self.edit_page._current_timeline = new_timeline
    ```
  - 影响: EditPage 从不拥有 `_command_stack`（命令栈在 MediaPoolPanel/TimelineView 上），条件恒假 → 属性面板发起的 transform/crop/speed/opacity/raw、效果增删改/启停/重排/关键帧、转场参数等编辑永远走 else 直接 apply：全部不可撤销（Ctrl+Z 无法回退），与 .clinerules 的命令栈单一提交入口约束不符；命令里精心构造的 `old_value`（"Get old value for undo"）形同虚设。
  - 建议: 统一改走 `TimelineView.execute_command`（本文件 2075-2084、8508-8511、8567-8573 已示范），删除失效的 hasattr 分支。
  - 置信度: 高

- [AUD-2708] [Major] 调色栈创建（可撤销）与 clip 绑定/scope 盖章（直接改）非原子，undo 后 clip 悬空引用已删除栈
  - 位置: src/app/controllers/edit_controller.py:1491-1494 + 1503-1517 + 1534-1539（`_ensure_clip_has_color_stack`）；7047-7065（`_on_use_shared_grade_requested`）；7087-7118 + 7004-7008（`_on_make_local_grade_copy_requested`）
  - 证据:
    ```python
    cmd_stack = self.media_pool_panel.get_command_stack()
    new_project = cmd_stack.execute(command, self._current_edit_project)
    self._current_edit_project = new_project
    ...
    new_timeline = command.apply(timeline)
    self.edit_page._current_timeline = new_timeline
    ```
  - 影响: `CreateColorStackCommand` 进媒体池命令栈（可撤销），但随后的 ownership 盖章（`replace(project, color_stacks=...)`）与 `UpdateClipColorStackCommand.apply` 都绕过命令栈直接改。用户在媒体池 Ctrl+Z 撤销建栈后，栈被移除而 clip 的 `color_stack_id` 绑定仍在（时间线命令栈里无对应记录）→ 悬空引用，渲染回退原始片段，redo 也无法恢复一致状态；`_on_make_local_grade_copy_requested` 的 `project.create_color_stack` 干脆完全不经命令栈。
  - 建议: 把建栈+盖章+绑定合并为一个 ProjectMacroCommand/TimelineCommand 事务（参考 `_on_grade_paste_all_requested` 的 sink+macro 模式），保证单一 undo 步骤内一致。
  - 置信度: 中

- [AUD-2709] [Suggestion] `hasattr(fps, 'to_fps')` 死代码：fps 已被 `float()` 转换，恒无 `to_fps` 属性
  - 位置: src/app/controllers/edit_controller.py:5591-5593；8986-8988
  - 证据:
    ```python
    fps = float(getattr(timeline, 'fps', 24.0) or 24.0)
    if hasattr(fps, 'to_fps'):
        fps = float(fps.to_fps())
    cwp.set_sequence(timeline, fps)
    ```
  - 影响: 意图是在 Rational 上探测 `to_fps()`，但探测发生在 `float()` 之后，分支永不可达；若 Rational 未定义 `__float__`，第一行已直接 TypeError 进入外层 except。逻辑与意图不符。
  - 建议: 先取原始对象再判断：`raw = getattr(timeline, 'fps', 24.0); fps = float(raw.to_fps() if hasattr(raw, 'to_fps') else raw)`。
  - 置信度: 高

- [AUD-2710] [Suggestion] 占位片段 fallback 轨道解析可抛 KeyError/StopIteration；新素材未刷新媒体池
  - 位置: src/app/controllers/edit_controller.py:8493-8496（另 8483 后未调 `media_pool_panel.set_project`）
  - 证据:
    ```python
    if not track_id:
        track_id = next(iter(new_project.timelines[
            new_project.active_timeline_id].video_tracks)).track_id \
            if new_project.timelines else "v1"
    ```
  - 影响: 条件只判 `new_project.timelines` 非空；若 `active_timeline_id` 为 None 或不在字典中抛 KeyError，`video_tracks` 为空抛 StopIteration。另外 `create_placeholder_asset` 产生的 new_project 已写回 `self._current_edit_project` 但未调用 `media_pool_panel.set_project`，新占位素材在媒体池不可见。
  - 建议: 用 `new_project.timelines.get(new_project.active_timeline_id)` + 空轨防御；落轨后同步媒体池并置 dirty。
  - 置信度: 中

- [AUD-2711] [Suggestion] `_save_current_project` 临时切换 current_page 无 try/finally，save 抛异常时页面状态不恢复
  - 位置: src/app/controllers/edit_controller.py:6088-6094
  - 证据:
    ```python
    original_page = self.current_page
    self.current_page = "Edit"

    success = self.save_router.save()

    # 恢复原始页面
    self.current_page = original_page
    ```
  - 影响: `save_router.save()` 若抛异常（而非返回 False），`current_page` 永久停留在 "Edit"，后续页面切换/保存路由判断基于被篡改的状态。
  - 建议: 用 try/finally 包裹恢复。
  - 置信度: 高

- [AUD-2712] [Suggestion] Routing 提交逐命令执行，一次对话框操作产生多条 undo 记录
  - 位置: src/app/controllers/edit_controller.py:3414-3448
  - 证据:
    ```python
    if getattr(track, "output_bus_id", None) != output_bus_id:
        cmd = SetTrackOutputBusCommand.create(track_id, output_bus_id)
        tview._execute_command(cmd)
    ...
    for target, old_s in old_sends.items():
        if target not in new_sends_map:
            cmd = RemoveSendCommand.create(track_id, old_s.send_id)
            tview._execute_command(cmd)
    ```
  - 影响: output_bus、每个 send 的增/删/改各自 `_execute_command`，N 个 send 变更 = N+1 步 undo，与 AM-12（`_execute_batch_macro`，本文件 2602-2617）确立的"宏聚合单步 undo"模式不一致，用户需多次 Ctrl+Z 才能回退一次 Routing 对话框提交。
  - 建议: 收集子命令后经 `MacroCommand.create("Commit Routing", sub_commands)` 一次提交。
  - 置信度: 高

- [AUD-2713] [Suggestion] ADR 延迟校准在 UI 线程同步执行往返测量，可能卡住界面
  - 位置: src/app/controllers/edit_controller.py:3029-3035
  - 证据:
    ```python
        calibration = None
        try:
            calibration = measure_round_trip_latency(
                device_id=settings.get('device_id') or None)
        except Exception as e:
            logger.warning("Latency calibration failed: %s", e)
    ```
  - 影响: 往返 click 测量（播放-采集-对齐）通常耗时数百毫秒以上，直接在按钮槽里同步执行会冻结主线程/事件循环，音频回调时序也可能受影响导致测量失真。
  - 建议: 移到 QThread/worker（项目已有 `_SpeechToTextWorker` 模式可复用），完成后回主线程 `set_calibration`。
  - 置信度: 中

- [AUD-2714] [Suggestion] 生成器时间线色块回退默认色与 metadata["color"] 默认值不一致；且两个 handler 均不刷新媒体池
  - 位置: src/app/controllers/edit_controller.py:3799-3803（对照 3742-3746）
  - 证据:
    ```python
    new_sc = (
        float(params.get("color_r", 0.0)),
        float(params.get("color_g", 0.6)),
        float(params.get("color_b", 0.8)),
    )
    ```
  - 影响: `metadata["color"]` 的回退默认是 `(0.0, 0.0, 0.0)`（3742-3746），而时间线色块用 `(0.0, 0.6, 0.8)`，params 缺键时两处显示不同颜色。另外两个 handler 都没有 `media_pool_panel.set_project(new_project)`，素材池中的 swatch/预览不会更新。
  - 建议: 提取单一默认色常量供两处使用；提交后同步媒体池。
  - 置信度: 高

- [AUD-2715] [Suggestion] 渲染缓存路径在 asset 缺失时用面板快照整体覆盖当前工程引用
  - 位置: src/app/controllers/edit_controller.py:7516-7534
  - 证据:
    ```python
    asset = self._current_edit_project.assets.get(clip.asset_id)
    if asset is None:
        for candidate in (
            getattr(getattr(self, "timeline_panel", None), "_project", None),
            ...
        ):
            candidate_asset = (...)
            if candidate_asset is not None:
                self._current_edit_project = candidate
                asset = candidate_asset
                break
    ```
  - 影响: 用 timeline_panel/timeline_view 持有的旧不可变工程快照整体替换 `self._current_edit_project`，若该快照落后于会话真源（如刚执行的 undo/属性变更尚未回投面板），后续所有基于 `_current_edit_project` 的指纹/manifest 写入会基于回退状态。
  - 建议: 只从候选快照中借取单个 asset 定义，或等待信号同步后再重试，而不是整体采纳候选工程。
  - 置信度: 中

- [AUD-2716] [Suggestion] `remove_composite_binding_for_clip` 中的自赋值死语句
  - 位置: src/app/controllers/edit_controller.py:8349
  - 证据:
    ```python
    # Update state
    self._current_edit_project = self._current_edit_project  # keep project data for now
    ```
  - 影响: 无任何效果，误导读者以为发生了状态更新；合成图数据确实保留在工程中（注释意图），但语句本身是噪音。
  - 建议: 删除该行，仅保留注释说明数据保留策略。
  - 置信度: 高

- [AUD-2717] [Suggestion] RoutingDialog 旧实例 close 后不 deleteLater，父窗口子部件累积
  - 位置: src/app/controllers/edit_controller.py:3359-3379
  - 证据:
    ```python
    existing = getattr(self, "_routing_dialog", None)
    if existing is not None:
        try:
            if existing.isVisible() and existing._track_id == track_id:
                existing.raise_()
                existing.activateWindow()
                return
            existing.close()
        except Exception:
            pass
    dialog = RoutingDialog(... parent=self.window ...)
    self._routing_dialog = dialog
    ```
  - 影响: 换轨重开时旧 dialog 只 `close()`，其 parent 是主窗口，C++ 对象作为子节点常驻直到窗口销毁；反复开关 Routing 逐渐累积内存与 Qt 子节点。
  - 建议: `existing.close(); existing.deleteLater()` 后再建新实例（或复用实例 reset 数据）。
  - 置信度: 高

- [AUD-2718] [Suggestion] 字幕拖拽提交失败时仅恢复 item 几何，controller 侧 transient 时间线不回滚
  - 位置: src/app/controllers/edit_controller.py:9424-9431（transient 直接 apply）与 8685-8704（失败恢复）
  - 证据:
    ```python
    command = cmds["UpdateSubtitleClipCommand"].create(...)
    result = command.apply(timeline)
    new_timeline = result if not isinstance(result, tuple) else result[0]
    self.edit_page._current_timeline = new_timeline
    ```
    ```python
    ok = self._execute_subtitle_command(command)
    if ok:
        return True
    # Locate the SubtitleClipItem and restore its geometry.
    ```
  - 影响: 拖拽期间每次 mouse-move 都直接改写 `edit_page._current_timeline`（不入栈）；若松手提交因重叠校验失败，`SubtitleEditPolicy.restore_on_failure` 只把图元几何弹回，`edit_page._current_timeline` 仍保留非法 transient 值，与 `TimelineView._timeline` 分歧，直到下一次 timeline_changed 才收敛；期间读取该时间线的面板/手柄显示非法位置。
  - 建议: 失败分支同时把 `edit_page._current_timeline` 回滚为 `TimelineView._timeline`（或在会话内保存 drag 前 timeline 快照用于恢复）。
  - 置信度: 中

- [AUD-2719] [Suggestion] MG 空闲缓存 3 秒轮询定时器一旦创建常驻运行
  - 位置: src/app/controllers/edit_controller.py:8036-8043（仅 `_close_current_project`:8247-8249 停止）
  - 证据:
    ```python
    timer = QTimer(self)
    timer.setInterval(3000)
    timer.timeout.connect(self._maybe_start_idle_mg_cache)
    self._mg_idle_cache_timer = timer
    self._mg_idle_cache_ids: set[str] = set()
    timer.start()
    ```
  - 影响: 暂停播放一次（`_on_playback_state_for_mg_idle(False)` → `_ensure_mg_idle_cache_scheduler`）后，定时器永久每 3 秒唤醒一次 `_maybe_start_idle_mg_cache`（遍历全部轨道/片段做 quiet 探测），即使项目没有 Motion Graphic 也持续空转，与 4K 实时性能基准下的空闲功耗目标相悖。
  - 建议: 无 MG 素材或无工程时 `timer.stop()`，在素材/工程变化或播放停止时再重启。
  - 置信度: 高

- [AUD-2720] [Suggestion] 多处 broad `except Exception` 不带堆栈地吞掉领域错误
  - 位置: src/app/controllers/edit_controller.py:2916-2917（`_on_take_recorded`）、2437-2439（`_commit_automation_recordings`）等
  - 证据:
    ```python
    except Exception as e:
        logger.warning("Failed to import take %s: %s", take.file_path, e)
    ```
  - 影响: 录音 take 导入、自动化录制提交等关键路径把任意异常（含编程错误如 AttributeError）压成一行无堆栈 warning，违背 .clinerules "不得宽泛 except 掩盖领域错误"；故障排查时缺少 traceback，且失败后状态（已 pop 的 recorder/manager）不回滚。
  - 建议: 至少改为 `logger.exception(...)` 保留堆栈；对可预期领域错误（IO/探测失败）单独捕获具体异常类型。
  - 置信度: 高

### 通过项
- 线程安全（信号/槽）：后台任务（ASR/TTS/multicam 同步/render cache）均以 QThread + Qt 信号队列回主线程，回调内只操作 UI 与不可变快照；`update_sequence_silent` 在 60Hz 拖拽路径避免代际抖动的设计正确。
- GPU 资源生命周期：本文件不直接创建/释放 ModernGL 对象，仅向渲染器发布不可变 sequence/project 快照；渲染缓存文件清理（`_cleanup_obsolete_render_cache_files`）有共享路径去重与 rendering 状态保护。
- GLSL/HDR 正确性：不适用（无 GLSL/HDR 数据处理路径）。
- 信号防递归：`_color_stack_hydration_in_progress` 状态锁、grading 面板 stale-stack 信号过滤（1681-1688）、`_grading_panel_changed_slot` 显式 disconnect 再 connect、caption workspace `select_clip` 守卫等实现正确。
- 输入验证：转场类型/对齐/曲线白名单（3838-3878）、TTS/ASR 选项校验、音频文件存在性检查、fps 解析（`_fps_token`/`_rational_for_timeline_fps`）防御完善。
- 异常与取消路径（渲染缓存）：in-flight 双重守卫（clip 级 + cache_id 级）、孤儿 RENDERING manifest 恢复、被取代 worker 的取消逻辑完善（AUD-2706 的析构时序除外）。
- 渲染缓存合同校验：`_on_render_cache_clip_requested` 对 codec/container/尺寸/fps/指纹的 VALID 复用校验（7712-7744）严谨，MG 契约变更后旧文件正确判过期。
- 状态提交一致性（部分）：subtitle 命令统一走 `TimelineView.execute_command` 单一入口（SUB-P0-01）、MacroCommand 单步 undo（字幕/批量静音/批量粘贴调色）实现正确；`invalidate_render_cache_for_clip` 的单参/双参两种调用均已核对与 project.py:3250 的兼容签名一致。
