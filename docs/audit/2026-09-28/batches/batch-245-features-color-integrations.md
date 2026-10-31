# batch-245 — src/features/color/integrations//

编号区间：AUD-24500–AUD-24599 · 优先级 P2 · 文件 2 个 / 1,023 行
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

交叉核实过的依赖接口：`src/shared/control_surfaces/tangent/hub_client.py`（worker 线程发 Qt 信号、`_send` 带锁且停机后返回 False）、`.../tangent/settings.py`（9 个灵敏度键名与桥内拼接一致、`TANGENT_SENSITIVITY_MAX=4800`）、`src/features/color/ui/color_wheel.py`（`grading_changed` 12 参数、`get_current_params` 恒返回数值 3 元组、`set_params/set_wheel_params/reset_all` 信号阻塞 + `_syncing_ui`）、`src/app/main_window.py:1229-1235`（实例化顺序）、`menu_toolbar_controller.py:1607-1625`（`on_interaction_started` 含 `history.push_state`，undo 依赖手势包裹）。全仓库（排除 tests/docs）无 `sys.excepthook` 安装。

---

## src/features/color/integrations/__init__.py (行数: 1)

### 发现

（无）

### 通过项
- 通过（无逻辑）：仅一行模块 docstring（"Color-page integrations with external control surfaces."），无导出、无代码。

---

## src/features/color/integrations/tangent_bridge.py (行数: 1022)

### 发现

- [AUD-24500] [Major] 环形（brightness）硬件调节经 `grading_changed` 回灌会无条件清空在途 trackerball 批处理与平滑状态
  - 位置: src/features/color/integrations/tangent_bridge.py:500-509（环路径）、:433-441（唯一守卫与重置）、:531-538（pending 清零）
  - 证据:
    ```python
    # _on_parameter_change 环/brightness 分支（component 2，不经 _flushing_trackerball）
    self._begin_gesture()
    self._apply_params(params)          # set_params(..., emit=True) → 同步发射 grading_changed
    ...
    # _on_panel_changed（同步直达，_flushing_trackerball 为 False）
    if self._flushing_trackerball:
        return
    ...
    self._reset_trackerball_points_from_params(values)   # 无条件重置
    ...
    # _reset_trackerball_points_from_params 内部
    state.pending_x = 0.0
    state.pending_y = 0.0
    state.pending_axes = 0
    state.pending_started_ns = 0
    state.pending_latest_input_ns = 0
    state.velocity_tracking_available = False   # 速度曲线也被打断
    ```
  - 影响: Ripple 上"转环的同时滚球"是常规手势。环路径的 `_apply_params(emit=True)` 同步触发 `_on_panel_changed`，其重置会丢弃球在 12ms 批处理窗口内已累计的 X/Y 增量（每个环包至多丢一批），并把平滑未收敛的 `target/output` 塌缩到当前值、复位 `velocity_tracking_available` 与 `last_flush_ns`——球响应出现卡滞/丢步，速度增益曲线被打断。对照 `sync_from_panel`（:243）显式用 `not self._trackerball_timer.isActive() and not self._flushing_trackerball` 防护同一操作，此处缺失同样的防护（本文件自己的注释 :435-438 也只针对 `_flush_trackerball_motion` 的回灌做了窄同步，说明环路径是被遗漏的一方）。
  - 建议: 在 `_on_panel_changed` 中与 `sync_from_panel` 对齐：当 `self._trackerball_timer.isActive()` 且信号源可归因于桥自身（或至少保留 pending 分量仅重基 target/output）时跳过或窄化重置；或让环路径像球路径一样走带 `_flushing_trackerball` 保护的窄同步。
  - 置信度: 高（调用链与清零行为已逐行核实：`set_params(emit=True)` 在 color_wheel.py:1126-1129 解除 `_syncing_ui` 后同步 emit）；用户可感程度为中。

- [AUD-24501] [Major] 多个 Hub 驱动 slot 内直接 `raise ValueError`（含一处未防护的 `float()` 索引取值），异常逃逸进 Qt 事件循环将按 PyQt6 默认行为 qFatal 终止整个应用
  - 位置: src/features/color/integrations/tangent_bridge.py:808-810（`_on_parameter_reset` slot 内 raise）、:660-661（`_wheel_from_trackerball_point`，经 `_flush_trackerball_motion` timer slot :778 调用）、:965-967（`_parameter_value`，经 `_on_panel_changed`/`sync_from_panel`/`_on_parameter_value_requested` slot 调用）、:501（`_on_parameter_change` slot 内无校验的 `float(params[spec.wheel][spec.component])`）
  - 证据:
    ```python
    # _on_parameter_reset —— 直连 parameter_reset_received 信号的 slot
    if spec.component in (0, 1):
        current = params[spec.wheel]
        if not isinstance(current, (tuple, list)) or len(current) != 3:
            raise ValueError("invalid grading wheel value")

    # _on_parameter_change —— component 2 分支，无任何防护
    current = float(params[spec.wheel][spec.component])
    ```
  - 影响: 这些 slot 由外部设备事件（Hub socket 信号 / 8ms timer）驱动。一旦面板数据契约被破坏（如 GradingPanel 未来演进、控件半销毁返回异常值），异常将从 slot 逃逸到 C++ 事件循环；已核实全仓库生产代码未安装 `sys.excepthook`，PyQt6 默认行为是打印回溯后调用 qFatal 中止进程——即可恢复的单包数据问题被放大为整个应用崩溃，违反"异常与取消路径"维度的降级要求。
  - 建议: 在 Hub 驱动 slot 边界统一捕获 `(TypeError, ValueError, IndexError)`：记 warning 日志并丢弃该事件包（与 `_values_from_grading_args` 的既有降级风格一致）；:501 处补与其他分量一致的 isinstance 校验。
  - 置信度: 中（崩溃机制与无全局钩子已核实；当前 `GradingPanel.get_current_params` 恒返回数值 3 元组、Hub 客户端已把载荷转为 int/float，故现阶段触发需面板契约先被破坏，标注为防御性风险）。

- [AUD-24502] [Suggestion] `_is_default` 的第二个 `or` 子句被首条件完全吸收，属死逻辑
  - 位置: src/features/color/integrations/tangent_bridge.py:1018-1022
  - 证据:
    ```python
    @staticmethod
    def _is_default(value: float, spec: _WheelParameter) -> bool:
        return abs(value) < 1e-6 or (
            spec.component == 0 and abs(value) < 1e-6
        )
    ```
  - 影响: 整个表达式恒等价于 `abs(value) < 1e-6`，`spec` 参数实际无效；疑似是未完成的按分量默认值判断（如 X/Y 与 ring 阈值本应不同）的残留，误导后续维护且掩盖真实意图。
  - 建议: 化简为单条件，或补全本意的按分量判定并加注释。
  - 置信度: 高

- [AUD-24503] [Suggestion] 弱类型注解：`panels: object` 与 `main_window: Any` 削弱静态检查
  - 位置: src/features/color/integrations/tangent_bridge.py:274（`def _on_panels_received(self, panels: object)`）、:117（`def __init__(self, main_window: Any, ...)`）
  - 证据:
    ```python
    def _on_panels_received(self, panels: object) -> None:
        if not isinstance(panels, tuple):
            return
    ...
    def __init__(self, main_window: Any, parent: QObject | None = None) -> None:
    ```
  - 影响: `panels_received` 载荷实为 `tuple[PanelInfo, ...]`（hub_client.py:330），`main_window` 至少用到 `on_interaction_started/on_interaction_ended/config_manager`——`object`/`Any` 使这些调用的拼写错误无法被类型检查发现。
  - 建议: `panels` 收窄为 `tuple[PanelInfo, ...] | object` 或用 TypeGuard；`main_window` 定义最小 Protocol（含 `grading_panel`、`config_manager`、两个交互回调）。
  - 置信度: 高

### 通过项
- 线程安全：TangentHubClient 在后台线程仅发 Qt 信号，桥的槽经队列连接在 UI 线程执行；桥内无可跨线程共享的可变状态；`client._send` 有 socket 锁、停机后安全返回 False。
- GPU 资源生命周期：本文件不创建/持有任何 ModernGL 或纹理资源；渲染失效完全复用 `grading_changed` 既有扇出（on_grading_changed → grade_image → request_render），无 UI 线程 OpenGL 调用。
- GLSL·HDR 正确性：无 GLSL；`min(1.0, hypot(...))`/`_clamp_trackerball_target` 属色轮控件域（饱和度 0..1）而非 HDR 像素数据 clamp，brightness 全程原样保留，未违反铁律。
- 信号防递归（其余路径）：trackerball 刷新用 `_flushing_trackerball` 状态锁阻断 `set_wheel_params/set_params(emit=True)` 的同步回灌；`_on_panel_changed` 对 12 元组长度校验并降级告警（AUD-24500 所述环路径为已知缺口）。
- 状态提交一致性：硬件编辑经 `_begin_gesture/_finish_gesture` 包裹 `on_interaction_started/ended`（含 history.push_state 与 autosave），与 UI 编辑共用同一 undo/revision 路径；`reconfigure` 切换面板时清空 `_last_sent_display_values`；重连后 `_on_connected → _schedule_sync → sync_from_panel(force=True)` 重建显示缓存；`shutdown` 停三定时器、闭合手势、停客户端。
- 输入验证（其余部分）：灵敏度/旋转/平滑均 clamp + `(TypeError, ValueError)` 降级；surface 经 `normalize_control_surface` 白名单化；参数 ID 经 `_PARAMETERS` 白名单过滤未知项；panels 载荷做 isinstance 校验；`_normalize_axis_increment` 有界处理计数/归一双格式。
- 性能模式：8ms PreciseTimer + 12ms 向量批窗限制发布率；`set_wheel_params` 窄路径避免整面板重绘；Hub 显示值二级缓存避免重复 socket 写；`_on_error`/`_report_connection_failure` 抑制重试日志刷屏。
- 类型注解（其余部分）：函数签名与局部变量注解总体完整，`_TrackerballPoint`/`_WheelParameter` 数据类字段类型齐全。
