# 批次报告：batch-213 [P1] src/features/edit/ui//

- 编号区间：AUD-21300–AUD-21399
- 审查日期：2026-09-28
- 文件：2 个，共 2,817 行
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/edit/ui/timeline_panel.py (行数: 2,679)

### 发现

- [AUD-21301] [Major] 键盘切换工具时 Rate Stretch 按钮的互斥选中状态不同步
  - 位置: src/features/edit/ui/timeline_panel.py:1545
  - 证据:
    ```python
    def _on_view_tool_changed(self, tool_name: str) -> None:
        ...
        # Sync button states for all tools
        self.btn_select.setChecked(tool_name == EditTool.SELECT.value)
        self.btn_trim.setChecked(tool_name == EditTool.TRIM.value)
        self.btn_blade.setChecked(tool_name == EditTool.BLADE.value)
        self.btn_ripple.setChecked(tool_name == EditTool.RIPPLE.value)
        self.btn_roll.setChecked(tool_name == EditTool.ROLL.value)
        ...
        self.btn_zoom.setChecked(tool_name == EditTool.ZOOM.value)
    ```
    （对比 `_set_tool`（行 1508–1525）中包含 `self.btn_rate_stretch.setChecked(tool == EditTool.RATE_STRETCH)`，本函数 9 个按钮全部同步、唯独缺 `btn_rate_stretch`。）
  - 影响: 用户经 Shift+R（`timeline_shortcuts.TIMELINE_KEY_ACTIONS` 声明的 `edit.timeline.tool.rate_stretch`，经 `TimelineView.set_tool → tool_changed` 触发本函数）激活 Rate Stretch 后，之前选中的工具按钮仍保持 checked、Rate Stretch 按钮保持 unchecked——工具栏与实际工具状态不一致，误导用户；反向切回时视觉互斥关系错乱。
  - 建议: 在 `_on_view_tool_changed` 中补 `self.btn_rate_stretch.setChecked(tool_name == EditTool.RATE_STRETCH.value)`；更稳妥的做法是改为遍历 `_get_tool_button_map()` 驱动 checked 状态，避免两处手写清单漂移。
  - 置信度: 高

- [AUD-21302] [Major] `_on_timer_frame_ready` 缺少隐藏视图守卫，跨页（Color 页）播放时仍驱动隐藏的 Edit 时间线视图与背压 reanchor
  - 位置: src/features/edit/ui/timeline_panel.py:1376
  - 证据:
    ```python
    def _on_timer_frame_ready(self, frame: int) -> None:
        """Handle frame ready from playback timer."""
        playback_trace("timeline_panel.timer_frame_ready", frame=frame)
        if self._should_defer_playback_tick(frame):
            ...
            self._playback_controller.reanchor_to_frame(
                stable_frame,
                emit_frame_changed=False,
            )
            self.playback_reanchored.emit(stable_frame)
            ...
        self._update_timecode_display(frame)
        self._set_view_playhead_from_controller(frame)
    ```
    而平行的 `_on_controller_frame_changed`（行 1326–1329）有明确守卫：
    ```python
    timeline_view = getattr(self, "timeline_view", None)
    if timeline_view is not None and not timeline_view.isVisible():
        return
    ```
  - 影响: 本面板持有共享 `EditPlaybackController`，注释明言"also driven from the Color page"。Color 页发起播放时，本面板 `_on_controller_state_changed`（行 1347–1348）仍会 `self._playback_timer.start()`，随后每 16ms tick 都无条件执行 `_update_timecode_display`（→ 隐藏视图 `set_corner_timecode`）与 `_set_view_playhead_from_controller`（→ 隐藏视图 `set_playhead`），绕过了 frame_changed 路径专门为规避 macOS 隐藏 GL viewport 初始化 abort 而加的守卫（行 1322–1325 注释记录的崩溃场景可能复现）；同时 `_should_defer_playback_tick` 咨询的是 Edit 页的 `_preview_renderer`（由 file_controller 挂接，页面隐藏期间不清除），背压成立时会把共享控制器 reanchor 到隐藏 Edit 视图的旧 playhead 帧，可能令 Color 页播放反复回跳/停顿。
  - 建议: 在 `_on_timer_frame_ready` 开头加与 `_on_controller_frame_changed` 相同的 `timeline_view.isVisible()` 早退（或统一提取公共守卫）；并考虑在视图隐藏时跳过 `_should_defer_playback_tick`/reanchor 分支，由可见页的驱动方负责背压。
  - 置信度: 中（机制由代码与注释直接支持；实际触发依赖 Color 页驱动播放且 renderer 保持挂接的运行时路径）

- [AUD-21303] [Major] `TimecodeInputDialog` 在 fps∈(0, 0.5) 时整除零导致崩溃
  - 位置: src/features/edit/ui/timeline_panel.py:2609
  - 证据:
    ```python
    # BUG-04 fix: Use round() instead of int() for accurate non-integer FPS
    fps_int = round(self._fps) if self._fps > 0 else round(DEFAULT_FPS)
    hours = self._current_frame // (fps_int * 3600)
    minutes = (self._current_frame // (fps_int * 60)) % 60
    seconds = (self._current_frame // fps_int) % 60
    frames = self._current_frame % fps_int
    ```
  - 影响: `set_fps(fps)`（行 887–896）对 fps 无任何下限校验，工程文件等外部数据若带入 0 < fps < 0.5（如 0.1），`round(fps) == 0` 且 `self._fps > 0` 守卫不生效，`// 0` 抛 ZeroDivisionError——打开"跳转时间码"对话框即崩溃（未捕获，对话框构造在 `open_timecode_dialog`/`_on_timecode_clicked` 主线程直接执行）。
  - 建议: `fps_int = round(fps) if round(fps) >= 1 else round(DEFAULT_FPS)`，或在 `set_fps` 入口校验 `fps >= 1.0`（非法值回退 DEFAULT_FPS），两处（`_setup_ui` 与 `_parse_input`，后者尚安全）统一取用。
  - 置信度: 中（触发需异常 fps 值，但代码路径真实存在且无防护）

- [AUD-21304] [Suggestion] `_zoom_updating` 防递归锁多处未用 try/finally，异常后滑块缩放被永久禁用
  - 位置: src/features/edit/ui/timeline_panel.py:1650
  - 证据:
    ```python
    # _refresh_zoom_slider_bounds（行 1650–1652）
    self._zoom_updating = True
    self.zoom_slider.setValue(self._ppf_to_slider(self.timeline_view._pixels_per_frame))
    self._zoom_updating = False
    ```
    同样无保护的模式还出现在 `_on_view_zoom_changed`（行 1721–1723）与 `set_zoom_level`（行 2428–2430）；而 `_reset_empty_timeline_zoom`（行 1668–1672）已经用了 try/finally：
    ```python
    self._zoom_updating = True
    try:
        self.zoom_slider.setValue(self.zoom_slider.minimum())
    finally:
        self._zoom_updating = False
    ```
  - 影响: `setValue → valueChanged → ...` 链路中任何一处（如 `_ppf_to_slider` 传入 NaN ppf、下游 view 异常）抛错，`_zoom_updating` 永久为 True，此后 `_on_zoom_changed` 直接早退，缩放滑块/按钮全部失效直至重建面板。
  - 建议: 三处统一改为 `try/finally` 模式（照 `_reset_empty_timeline_zoom`）。
  - 置信度: 高（不一致模式确凿；实际触发依赖下游异常，概率低）

- [AUD-21305] [Suggestion] 多处裸 `except Exception: pass` 静默吞错且无日志
  - 位置: src/features/edit/ui/timeline_panel.py:1402
  - 证据:
    ```python
    # _get_visible_playhead_frame（行 1399–1404）
    try:
        if hasattr(self.timeline_view, "get_playhead_frame"):
            return int(self.timeline_view.get_playhead_frame())
    except Exception:
        pass
    ```
    同类还有：`_on_controller_state_changed` 的 QoS 分支（行 1360–1361，`except Exception: pass`）、`_sync_mixer_from_current_timeline`（行 1950–1953 与 1973–1976 两处 `except Exception: pass`）。
  - 影响: 混淆器同步、QoS 恢复、playhead 读取一旦真实出错（类型错误、状态损坏），既无日志也无任何可观测信号，现场无法排查，违背 .clinerules"不得宽泛 except 掩盖领域错误"的精神。
  - 建议: 至少统一补 `logger.debug(..., exc_info=True)`（文件内其他分支如行 845–846、1576 已是此做法），缩小可预期异常类型。
  - 置信度: 高

- [AUD-21306] [Suggestion] HUD 刷新时在 UI 线程对最多 200 个素材同步做磁盘代理状态校验
  - 位置: src/features/edit/ui/timeline_panel.py:2152
  - 证据:
    ```python
    service = get_disk_proxy_service()
    for asset in list(assets.values())[:200]:
        if getattr(asset, "media_type", "") != "video":
            continue
        path = getattr(asset, "path", None)
        ...
        info = service.get_disk_proxy_info(path)
    ```
    （`disk_proxy_service.get_disk_proxy_info` 内部 `with self._lock:` 后，缓存未命中时走 `_manager.get_valid_proxy_path(source_path)` 做磁盘校验。）
  - 影响: HUD 打开期间每次刷新（定时器驱动）都可能触发最多 200 次加锁+磁盘路径校验，冷缓存的大工程上 UI 线程可观测卡顿，与 4K 实时性能基准的目标相悖（虽属诊断功能，频率受限，影响可控）。
  - 建议: 结果按 asset 集合缓存/增量查询，或把统计移到后台线程后经信号回填。
  - 置信度: 中（磁盘校验成本取决于 `_info_cache` 命中率与实现）

- [AUD-21307] [Suggestion] 个别公开/私有方法参数与容器类型注解缺失
  - 位置: src/features/edit/ui/timeline_panel.py:898
  - 证据:
    ```python
    def set_fps_rational(self, fps) -> None:   # 行 898，fps 无注解（Rational|float）
    def _sync_mixer_from_current_timeline(self, main_win) -> None:  # 行 1946
    def update_audio_levels(self, levels: list) -> None:  # 行 1919，泛型未参数化
    ```
  - 影响: 文件整体注解良好，仅个别签名缺失，影响 IDE 检查与调用方类型安全（.clinerules 强类型约束）。
  - 建议: 补 `fps: "Rational | float"`、`main_win: QWidget`、`levels: list[float]`。
  - 置信度: 高

### 通过项
- 线程安全：面板全部操作在 UI 线程；`update_audio_levels` 经 Qt 信号（`AudioPlaybackManager.audio_level_changed`，auto 连接跨线程自动排队到 UI 线程）更新，无跨线程直接触控件；`_playback_controller` 的跨页共享经信号桥接。
- GPU 资源生命周期：文件不直接持有 ModernGL/纹理对象，渲染经 `_preview_renderer`/TimelineView 间接访问，无泄漏点。
- GLSL·HDR 正确性：无 GLSL/HDR 数据处理代码（纯 UI 面板），不适用。
- 信号防递归：`_updating` 锁正确覆盖 playhead 双向同步（`_on_playhead_changed`/`_set_view_playhead_from_controller`）；marker 按钮与 dock 用 `blockSignals` 防递归（行 1857–1873）；mixer 按钮同步 `sync_mixer_button` 同样 blockSignals；`_marker_filter_combo.currentIndexChanged → _refresh_marker_list` 无回灌。
- 状态提交一致性：Deliver 的 In/Out 默认范围经 `execute_command(SetTimelineRangeCommand...)` 走 undo 栈（行 774–781）；编辑模式持久化经 ConfigManager 且失败降级；`timeline_changed → _on_timeline_content_changed` 同步时长/范围/滑块边界。
- 异常与取消路径：播放、缩放、JKL、scrub 各路径异常基本有兜底（见 AUD-21305 的例外）；`clear()`/`load_timeline()` 先停播放再重置状态，顺序正确。
- 输入验证：`TimecodeInputDialog._parse_input` 解析失败返回 None 并标红输入框、帧号夹取到 [0, total-1]；`set_deliver_range_type` 对 duration<=0 早退；`_effective_timeline_duration` 保证最小时长（fps 异常路径见 AUD-21303）。
- 性能模式：滑块缩放用 16ms 合并 + `sliderReleased` 精确落点；缩放手势期间暂停缩略图/波形后台工作并经 token 防过期恢复（180ms 空闲判定；播放/scrub 期间不恢复由 EditPage 的 `_set_thumbnail_playback(False)` 停播路径兜底恢复）。
- zoom 滑块对数映射边界夹取（`_ppf_to_slider`/`_slider_to_ppf`）正确；`resizeEvent` 响应式布局在 Deliver 模式下不被覆盖。

---

## src/features/edit/ui/timeline_shortcuts.py (行数: 138)

### 发现

- [AUD-21308] [Suggestion] `matches_key_event` 两处裸 `except Exception` 静默吞掉绑定读取/解析错误
  - 位置: src/features/edit/ui/timeline_shortcuts.py:114
  - 证据:
    ```python
    binding = None
    try:
        manager = ShortcutManager.instance()
        if manager is not None:
            binding = manager.get_shortcut(action_id)
    except Exception:
        binding = None
    ...
    try:
        sequences = QKeySequence(binding)
    except Exception:
        return False
    ```
  - 影响: 用户在快捷键设置里保存了非法绑定（`get_shortcut` 抛错或 `QKeySequence` 解析失败）后，对应时间线编辑键会静默失灵且无任何日志，排障困难；正常无注册场景已有回退（`default_key`），异常路径与"未注册"不可区分。
  - 建议: 捕获后 `logger.debug(..., exc_info=True)` 再回退默认键；将 `QKeySequence` 构造移出按键热路径或在注册时校验一次。
  - 置信度: 高

- [AUD-21309] [Suggestion] `_normalize_modifiers` 参数无类型注解
  - 位置: src/features/edit/ui/timeline_shortcuts.py:92
  - 证据:
    ```python
    def _normalize_modifiers(modifiers) -> int:
        """Qt.KeyboardModifier flags/enum → 规范化 int（Shift/Ctrl/Alt/Meta）。"""
    ```
  - 影响: 文件其余签名注解完整，此处缺失（.clinerules 强类型约束）。
  - 建议: 注解为 `Qt.KeyboardModifiers`（或 `Any` 加说明）。
  - 置信度: 高

### 通过项
- 线程安全：纯注册/匹配辅助函数，仅 UI 线程调用（TimelineView keyPressEvent），无共享可变状态。
- GPU 资源生命周期 / GLSL·HDR 正确性：不适用（无 GPU/HDR 代码）。
- 信号防递归：不涉及信号发射，纯查询。
- 状态提交一致性：`register_timeline_shortcuts` 幂等（`get_action` 存在性检查），且正确避开了 EditModule 已注册的 `edit.timeline.add_marker` 防止冲突误报（行 58–61 注释）。
- 异常与取消路径：见 AUD-21308（其余兜底合理：未注册回退 `default_key`，双缺失返回 False）。
- 输入验证：`matches_key_event` 对空绑定/解析失败返回 False，不崩溃。
- 性能模式：每次按键一次 `ShortcutManager.instance()` + 字典查询，成本低；`_normalize_modifiers` 对 PyQt6 枚举/flags 的 int() 兜底（`.value`）处理正确。
- 数据模型：`TimelineKeyAction` frozen dataclass，`__all__` 导出明确（TIMELINE_KEY_ACTIONS / DEFAULT_KEY_BY_ACTION / register_timeline_shortcuts / matches_key_event）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-21301, AUD-21302, AUD-21303 |
| Suggestion | 6 | AUD-21304, AUD-21305, AUD-21306, AUD-21307, AUD-21308, AUD-21309 |
