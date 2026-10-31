# batch-101 — src/features/composite/ui//（P1）

任务书：docs/audit/2026-09-28/assignments/batch-101.md
编号区间：AUD-10100–AUD-10199
审查方式：全文件通读（3,041 行，Read 分三段读完），并对可疑点交叉核对
keyframes_tree_view.py / timeline_view_model.py / timeline_time_display.py / composite_page.py / runtime.py，
另用离屏 PyQt6 实测 QSplitter.setSizes 的 splitterMoved 触发行为。

## src/features/composite/ui/keyframes_panel.py (行数: 3041)

Fusion 风格关键帧 Dock 顶层面板：工具栏 + 左树/右时间轴/Graph Editor + 表达式编辑器。
纯 QWidget 界面层，无 OpenGL/GLSL；渲染与 undo 归属 CompositePage（宿主）。

### 发现

- [AUD-10100] [Major] 多处宽泛 `except Exception` 吞掉宿主回调与领域错误（铁律违规）
  - 位置: src/features/composite/ui/keyframes_panel.py:548（另见 600、1479、1568、2107、2224）
  - 证据:
    ```python
    try:
        return bool(handler(mime_data, target_layer_id, insert_index))
    except Exception:
        return False
    ```
    ```python
            return str(value)
        except Exception:
            return None
    ```
    （`set_node_graph` 中选中层校验同样 `except Exception: pass`，1479–1480）
  - 影响: 媒体拖放处理器抛出的领域错误被静默降级为"拖放被拒绝"；Value 列取值
    provider 的任何节点/轨道数据错误被吞为 None（画成"—"），掩盖图数据 bug，与
    `.clinerules`"不得宽泛 except 掩盖领域错误"直接冲突。
  - 建议: 收窄为具体异常类型，或在捕获处记录日志（logger.debug/warning）并保留
    降级行为；宿主回调（media drop handler、value provider）至少要区分
    TypeError/AttributeError 与真实领域异常。
  - 置信度: 高

- [AUD-10101] [Major] `set_playback_controller` 重复绑定/置空时不断开旧连接
  - 位置: src/features/composite/ui/keyframes_panel.py:2339
  - 证据:
    ```python
    def set_playback_controller(self, controller: Optional[object]) -> None:
        ...
        self._playback_controller = controller
        if controller is not None:
            try:
                self.play_toggle_requested.connect(
                    controller.keyframes_panel_play_toggle)
            except (AttributeError, TypeError, RuntimeError):
                self._playback_controller = None
    ```
    对比同文件 `set_layer_stack_controller`（517–522）正确地先 disconnect 旧对象。
  - 影响: 对同一面板再次绑定（或绑 None 后重绑）时，旧 controller 仍挂在
    `play_toggle_requested` 上：播放按钮一次点击会触发新旧两个引擎，或向已分离的
    controller 发信号。当前生产路径（runtime.py:99–102）只绑定一次，属潜伏缺陷；
    API 形态（Optional 可重设）与姊妹 API 的断开语义不一致。
  - 建议: 仿照 `set_layer_stack_controller`，绑定前记录旧连接并 try/disconnect；
    controller 为 None 时同样清理。
  - 置信度: 高（代码事实），中（生产触发概率）

- [AUD-10102] [Major] 播放期间每帧全量刷新 Value 列，provider 每次调用都重新反序列化整条关键帧轨道
  - 位置: src/features/composite/ui/keyframes_panel.py:2392（另见 1540–1555、1684）
  - 证据:
    ```python
    def set_current_frame(self, frame: int) -> None:
        ...
        # Refresh the shared DragValue controls so their displayed value and
        # Properties-style edit base follow the same sampled frame.
        self._tree_view.refresh()
    ```
    ```python
            track_data = getattr(node, "keyframe_tracks", {}).get(param_name)
            value = None
            if isinstance(track_data, dict) and track_data.get("keyframes"):
                from src.shared.keyframe import keyframe_track_from_dict
                track = keyframe_track_from_dict({...})
    ```
    （`_value_control_metadata` 1684 行又调用 `_display_value_for`；树视图
    `_sync_value_controls`/`_draw_value` 对每个可见参数行每帧调用这些 provider）
  - 影响: 内部回放/宿主播放时每个 tick（24–60fps）对每个可见动画参数行执行
    `keyframe_track_from_dict`/`ColorKeyframeTrack.from_dict` 完整对象构造 + 求值，
    再全量 setRange/setValue 所有 DragValue —— UI 线程上每秒数百至上千次轨道反序列
    化，与 4K 实时基准冲突，大合成播放时造成 UI 卡顿。
  - 建议: 按 (node_id, param_name, graph generation) 缓存已构造的 track 对象
    （node 写入时失效）；或帧变化时仅更新可见行 DragValue 的显示值，跳过全量
    `_sync_value_controls`；`_display_value_for` 的采样与 `_value_control_metadata`
    可合并为一次调用。
  - 置信度: 高

- [AUD-10103] [Suggestion] `_value_provider` 宿主回调两处调用无异常防护，与同文件守卫写法不一致
  - 位置: src/features/composite/ui/keyframes_panel.py:1743（另见 1955；对照 2866–2871）
  - 证据:
    ```python
            value = None
            value = self._value_provider(node_id, param_name) \
                if self._value_provider else None
    ```
    而同文件 `add_keyframe_at_playhead` 对同一回调：
    ```python
            if self._value_provider is not None:
                try:
                    value = self._value_provider(node_id, param_name)
                except Exception:
                    value = None
    ```
  - 影响: `set_value_provider` 是公开可插拔 API（当前 CompositePage 实现内部自防，
    属潜伏风险）；第三方宿主 provider 抛异常会从点击槽直接传入 Qt 事件循环。1743
    行的 `value = None` 也是死赋值。
  - 建议: 统一用带 try/except 的私有 helper 调用 provider；删除死赋值。
  - 置信度: 高

- [AUD-10104] [Suggestion] `QTimer.singleShot` 携带 lambda，面板销毁后触发时访问已删除控件
  - 位置: src/features/composite/ui/keyframes_panel.py:1922（另见 1926–1927）
  - 证据:
    ```python
            QTimer.singleShot(2600, lambda: self._clear_binding_feedback(serial))
        ...
            QTimer.singleShot(0, lambda: self._open_expression_for_param(
                target_node_id, param_name))
    ```
  - 影响: PyQt 对 lambda 定时回调不随 QObject 销毁自动断开（绑定方法才会）。pick-whip
    后 2.6s 内关闭页面/dock，`_clear_binding_feedback` 会对已删除的 QLabel 包装调用
    `label.clear()` 抛 RuntimeError 进入事件循环（默认仅打印回溯，但破坏关闭路径的
    干净性，遇到严格 excepthook 可中止进程）。
  - 建议: 改用绑定方法 + 序号参数（存到实例属性），或使用以 label 为父对象的
    QTimer 并在 dispose() 中 stop；或捕获 RuntimeError。
  - 置信度: 高（机制），中（触发窗口 2.6s）

- [AUD-10105] [Suggestion] `_on_graph_selection_changed` 直接改写 AnimationSelectionModel 私有 `_selection`，绕过公开 API
  - 位置: src/features/composite/ui/keyframes_panel.py:636
  - 证据:
    ```python
        sm = self._selection_model
        sm.begin_batch()
        try:
            sm._selection.keyframes = set(keyframe_pairs)
            sm._selection.rows = rows
            if pairs:
                first_row, first_frame = pairs[0]
                sm._selection.primary_row_id = first_row
                sm._selection.primary_frame = int(first_frame)
    ```
    而 timeline_view_model.py:485 已提供 `set_keyframes(pairs)` 完成同样的
    keyframes+primary 写入。
  - 影响: 面板越过模型封装写字段，未来模型在 setter 中加入不变量（primary 必须在
    keyframes 集合内、rows 与 keyframes 一致性检查、通知去重）时该路径会失配；
    也使 `_notify` 语义依赖手工 batch 配对。
  - 建议: 使用/补充公开方法（`set_keyframes` + 新增 `set_rows` 或
    `replace_selection(keyframes, rows)`），面板只调公开 API。
  - 置信度: 高

- [AUD-10106] [Suggestion] `_on_timeline_track_changed` 校验传入 keyframes 的 frame，却不校验节点既有（持久化）keyframes 的 frame
  - 位置: src/features/composite/ui/keyframes_panel.py:1598
  - 证据:
    ```python
        existing_by_frame = {
            int(kf.get("frame", -1)): kf
            for kf in (existing.get("keyframes", []) if isinstance(existing, dict) else [])
            if isinstance(kf, dict)
        }
    ```
    同函数对传入列表则做了防护：
    ```python
            try:
                frame = int(keyframe.get("frame", 0))
            except (TypeError, ValueError):
                continue
    ```
  - 影响: 既有轨道来自节点/工程持久化数据（外部数据）；frame 为非数字字符串时
    `int()` 抛 ValueError，沿 track_changed 信号路径上传 Qt 事件循环，违反"外部数据
    须验证"。同函数内两套校验标准不一致。
  - 建议: 对 existing 构建同样套 try/except 或预过滤非法项。
  - 置信度: 中

- [AUD-10107] [Suggestion] 绑定反馈与 On/Off 等用户可见文案硬编码英文，绕过 I18N
  - 位置: src/features/composite/ui/keyframes_panel.py:1912（另见 1561–1562、1898）
  - 证据:
    ```python
        text = str(message or ("Binding applied" if success
                               else "Binding rejected"))
    ```
    ```python
            if isinstance(value, bool):
                return "On" if value else "Off"
    ```
    （1898 行 `"Parent binding is not supported by this node"` 直接进工具栏标签）
  - 影响: 工具栏绑定反馈标签、Value 列布尔值、父绑定错误提示在非英文locale下不翻
    译，与全文件其余 I18N.tr 用法不一致。
  - 建议: 改用 `I18N.tr(...)`（新增 keyframes.binding_applied / binding_rejected /
    value_on / value_off / parent_binding_unsupported 等键）。
  - 置信度: 高

- [AUD-10108] [Suggestion] 已动画参数的 no-op 值编辑仍走 `add_keyframe_at`，产生冗余 track_changed/undo 记录
  - 位置: src/features/composite/ui/keyframes_panel.py:1785
  - 证据:
    ```python
        frame = int(self._state.current_frame)
        if self._keyframe_status_for(node_id, param_name)["at_current"]:
            self._timeline_view.add_keyframe_at(
                node_id, param_name, frame, value_hint=float(value))
        elif old != value:
            self.parameter_changed.emit(node_id, param_name, value)
    ```
  - 影响: 当前帧已有 keyframe 时，即使新旧值相同（如重复提交同一数值）也会写入
    keyframe 并触发 track_changed → CompositePage 按 design 推 undo 并重渲染；非动画
    分支却有 `old != value` 短路。产生无意义 undo 噪声。
  - 建议: 在 at_current 分支同样先比较新旧值（采样当前轨道值），相同则直接 return。
  - 置信度: 中（依赖宿主是否去重 undo，但面板层缺失短路是事实）

- [AUD-10109] [Suggestion] 多个公开/私有方法缺少参数或返回类型注解
  - 位置: src/features/composite/ui/keyframes_panel.py:607（另见 356、392、531、537、2749）
  - 证据:
    ```python
    def _on_graph_selection_changed(self, selection) -> None:
    ```
    ```python
    def set_media_drop_handler(self, handler) -> None:
    ```
    （`_shortcut_sequence(event)`、`eventFilter(watched, event)`、
    `_layer_for_root(self, root_node_id: str):` 等同缺；Qt 覆写 wheelEvent/
    resizeEvent/showEvent 属惯例豁免）
  - 影响: 与 `.clinerules` 强类型要求不符；`selection`/`handler` 等外部传入回调与
    集合的形状不可静态检查。
  - 建议: 补注解（如 `selection: Iterable[Tuple[str, int]]`、
    `handler: Callable[[QMimeData, str, int], bool]`、`_layer_for_root(...) -> Optional[Any]`）。
  - 置信度: 高

- [AUD-10110] [Suggestion] 面板频繁读写兄弟视图/模型私有成员
  - 位置: src/features/composite/ui/keyframes_panel.py:653（另见 329、433、541–542、2001、2065、2980、3040）
  - 证据:
    ```python
        summary = summarize_selection(self._graph_view._series,
                                      fps=self._state.fps)
    ```
    ```python
        selected = getattr(self, "_selected_node_id", None)  # 即 self._model._selected_node_id
    ```
    （`_timeline_view._delete_selected_keyframes`、`_tree_view._close_keyframe_editor`、
    `_tree_view._row_index_at_y`、`_timeline_view._scroll_y` 等同类）
  - 影响: 跨类私有耦合使子视图重构（如 KeyframesTimelineView 改用 QAbstractScrollArea）
    会静默破坏面板；`_selected_node_id` 用 getattr 私有名取值尤其脆弱。
  - 建议: 为各视图补最小公开访问器（`selected_node_id()`、`timeline_series()`、
    `scroll_y` 属性、`close_editor()` 等），面板只调公开接口。
  - 置信度: 高

### 通过项
- 线程安全：全部交互在 UI 线程；内部回放 QTimer 属面板自身；与渲染线程的交互全部经宿主信号边界，本文件无跨线程 Qt/OpenGL 调用
- GPU 资源生命周期：不涉及 ModernGL/纹理/缓冲区（纯 QWidget 面板）
- GLSL·HDR 正确性：不适用（无 GLSL、无 HDR 数据路径）
- 信号防递归：`set_playing_state`/`_on_view_display_mode_changed` 用 blockSignals 更新控件；`set_play_range`/`set_frame_range` 幂等早退；工具栏 Graph 开关经 `set_view_mode` 二次进入即 no-op，无环路；实测确认 `_init_ui` 中 `setSizes([300,830])` 显示前不发 splitterMoved，`_splitter_user_moved` 不会被误置，AE 默认分栏逻辑有效（`_splitter_applying_defaults` 再加防护）
- 生命周期/取消路径：`dispose()`/`closeEvent` 停止内部 QTimer、清空回调引用并对 `_act_play` 的销毁期 RuntimeError 做了防护；`set_layer_stack_controller` 正确 disconnect 旧信号
- 播放范围一致性：`set_play_range`/`_clamp_play_range`/`_on_timeline_play_range_changed` 均按 comp extent 夹取并保持 in ≤ out；`seek_to_frame` 对 ruler 范围双向 clamp
- 状态提交一致性（总体）：关键帧/表达式/绑定/时间范围编辑均以意图信号（track_changed、graph_expression_changed、parameter_changed、range_changed）交宿主做权威写 + undo，面板不直接改 NodeGraph（display_mode 写的是面板自有的 state 副本，宿主经 set_frame_range 每次新建 state，无共享别名）
- 输入验证（总体）：`_on_property_value_changed` float 解析失败回退刷新；option/bool 拖拽有界；`_rebuild_shortcuts` 对旧 QShortcut 先禁用再 deleteLater，无泄漏
- eventFilter 快捷键派发不拦截子编辑器输入（过滤器仅装于面板/树/时间轴/graph 视图自身，搜索框与表达式编辑器不受影响）；auto-repeat 已排除
- `_expression_status_for` 的诊断缓存按 (graph id, generation, frame, fps) 去重，避免每行重跑表达式诊断
