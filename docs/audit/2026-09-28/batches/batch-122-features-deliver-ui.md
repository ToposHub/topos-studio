# Batch 122 审查报告 — src/features/deliver/ui/

- 审查日期：2026-09-28
- 编号区间：AUD-12200–AUD-12299（实际使用 AUD-12200–AUD-12216）
- 文件数：4（2,013 行）
- 交叉验证：`src/features/edit/core/timeline_export.py`（TimelineExportConfig.validate / _calculate_frame_range / exporter API）、`src/features/deliver/ui/video_export_settings_panel.py`、`src/features/deliver/core/video_batch_export_controller.py`、`src/app/controllers/dock_layout_controller.py`

## src/features/deliver/ui/__init__.py (行数: 0)

### 发现

（无）

### 通过项
- 通过（无逻辑）：0 字节空文件，仅作为 package 标记，不导出任何符号。

## src/features/deliver/ui/deliver_page.py (行数: 1173)

### 发现

- [AUD-12200] [Critical] Custom Range 的 "To End" 哨兵值 -1 被 QSpinBox 钳位为 0，Custom 范围默认导出 0 帧
  - 位置: src/features/deliver/ui/deliver_page.py:258-260、691
  - 证据:
    ```python
    self._end_frame_spin = QSpinBox()
    self._end_frame_spin.setRange(0, 1000000)
    self._end_frame_spin.setValue(-1)  # -1 表示到末尾
    ...
    end_frame = self._end_frame_spin.value() if range_type == ExportRangeType.CUSTOM else -1
    ```
    `TimelineExportConfig` 约定 `end_frame: int = -1  # -1 表示到末尾`（timeline_export.py:439），范围解析为 `end = config.end_frame if config.end_frame >= 0 else duration`（timeline_export.py:3702-3703）。QSpinBox.setValue(-1) 会被钳位到最小值 0（specialValueText "To End" 恰好在 minimum 时显示），`validate()` 的 `end_frame >= 0 and end_frame < start_frame` 检查对 0 不报错（0 < 0 为 False）。
  - 影响: 用户选择 Custom Range 且不改动 End（显示 "To End"）时，end_frame=0 → 导出区间 [0, 0)，导出流程正常走完并返回 success=True，但产出 0 帧的空/损坏文件，且界面提示 "Export Complete"。属于静默渲染结果错误。
  - 建议: 在 `get_export_config` 中显式映射：当 spin 值等于 minimum（即 "To End"）时传 -1；或用独立 "到末尾" 复选框替代 specialValueText 哨兵。
  - 置信度: 高

- [AUD-12201] [Major] `_start_export` 异常路径不重置 `_is_exporting`，一次启动失败后导出功能被永久锁死
  - 位置: src/features/deliver/ui/deliver_page.py:1005、1038-1045、1160-1173、998-1000
  - 证据:
    ```python
    self._is_exporting = True
    ...
    except Exception as e:
        logger.exception(f"Failed to start export: {e}")
        self._reset_ui()
    ```
    `_reset_ui()` 只恢复按钮可见性并置 `self._exporter = None`，不含 `self._is_exporting = False`（该复位仅存在于 `_on_export_complete`/`_on_export_error`）。若 `create_timeline_exporter(...)` 抛异常，没有 worker 会被创建，也永远不会收到 complete/error 信号。
  - 影响: 之后点击 "Start Export" 在 `if self._is_exporting: return` 处静默返回，按钮看似可用但功能失效，直到页面重建。
  - 建议: 在 `_reset_ui()` 内统一复位 `_is_exporting`，或在 except 分支显式 `self._is_exporting = False`。
  - 置信度: 高

- [AUD-12202] [Major] `set_timeline_sequences` 引用从未创建的 `_settings_panel`，宽泛 except 吞掉 AttributeError，P1-14 同步实为空操作
  - 位置: src/features/deliver/ui/deliver_page.py:663-667
  - 证据:
    ```python
    # 复验 P1-14：preflight 与真实导出共用同一序列字典（nested 音频展开）
    try:
        self._settings_panel.set_timeline_sequences(self._timeline_sequences)
    except Exception:  # noqa: BLE001 — 面板未建时不阻断
        pass
    ```
    全文件检索确认 `DeliverPage._setup_ui` 从未创建 `_settings_panel`（该属性属于另一个类 DeliverWorkspace 的面板），每次调用必然抛 `AttributeError` 并被 `pass` 吞掉。
  - 影响: 注释声称的 "preflight 与真实导出共用序列字典" 在 DeliverPage 上完全未生效（嵌套序列音频展开的 preflight 依据缺失）；同时每次调用都产生一个被静默吞掉的异常，违反 "不得宽泛 except 掩盖领域错误" 铁律。
  - 建议: 删除该死代码分支，或改为 `hasattr` 显式判断并记录日志；如确需转发，应持有真实面板引用。
  - 置信度: 高

- [AUD-12203] [Major] Source 从 Color/Composite 切回 Timeline 后，Custom Range 输入控件不再恢复显示
  - 位置: src/features/deliver/ui/deliver_page.py:825-837
  - 证据:
    ```python
    self._range_combo.setEnabled(is_timeline)

    # 非 Timeline 时隐藏自定义范围
    if not is_timeline:
        self._custom_range_widget.setVisible(False)
    ```
    只有 `_on_range_changed`（range 下标变化时触发）会重新设置 `setVisible(is_custom)`；切回 Timeline source 时 range combo 的 index 未变，不会触发该槽。
  - 影响: Range 类型仍为 CUSTOM 时自定义范围 SpinBox 不可见且不可操作，用户无法调整起止帧；与 AUD-12200 叠加会以不可见的 0/0 帧配置导出空文件。
  - 建议: `_on_source_changed` 在 `is_timeline` 为真时按当前 range 类型恢复 `_custom_range_widget.setVisible(is_custom)`。
  - 置信度: 高

- [AUD-12204] [Major] 时长/大小估算被 output_path 必填校验挡死，且路径变化不触发刷新
  - 位置: src/features/deliver/ui/deliver_page.py:729-733、894-909、411-413
  - 证据:
    ```python
    errors = config.validate()
    if errors:
        logger.warning(f"Config validation errors: {errors}")
        return None
    ...
    config = self.get_export_config()
    if config is None:
        return
    ```
    `TimelineExportConfig.validate()` 无条件要求 `output_path` 非空（timeline_export.py:537-538 `"output_path is required"`），导致 `_update_estimates` 在用户尚未选路径时直接返回；同时 `_path_input` 未连接 `textChanged`，浏览/手输路径后估算也不会刷新。
  - 影响: 默认流程下 Duration/Size 估算一直显示 "--"，直到用户先选路径再改动其他任一控件才可能更新——估算功能在主路径上形同虚设。
  - 建议: 估算使用跳过 output_path 校验的轻量配置构造（或直接传参给 estimate_* 函数），并连接 `_path_input.textChanged → _update_estimates`。
  - 置信度: 高

- [AUD-12205] [Major] 页面销毁时无 ExportWorker 清理，导出进行中关闭页面/退出应用会触发 QThread 析构崩溃
  - 位置: src/features/deliver/ui/deliver_page.py:89-135、173
  - 证据:
    ```python
    class ExportWorker(QThread):
        ...
    ...
    self._worker: Optional[ExportWorker] = None
    ```
    `_worker` 仅在 `_on_export_complete`/`_on_export_error` 中 quit/wait；类中无 `closeEvent`/析构/`deleteLater` 钩子在页面销毁前等待线程结束。
  - 影响: 导出运行期间销毁 DeliverPage（如应用退出、模块替换 fallback 页面）会命中 Qt 的 "QThread: Destroyed while thread is still running" 崩溃路径。
  - 建议: 增加 `closeEvent`（或覆写 `deleteLater` 前的清理）：`cancel_export()` + `self._worker.wait()` 后再放行销毁。
  - 置信度: 中（崩溃机制确定；DeliverPage 为 fallback 页面，通常随主窗口存活，触发窗口有限）

- [AUD-12206] [Suggestion] 分辨率下拉项文案缺右括号 `"1280x720 (720p"`
  - 位置: src/features/deliver/ui/deliver_page.py:288
  - 证据: `self._resolution_combo.addItem("1280x720 (720p")`
  - 影响: UI 文案拼写缺陷（其余项均带右括号），且该字符串未走 I18N。
  - 建议: 改为 `"1280x720 (720p)"` 并纳入 I18N.tr。
  - 置信度: 高

- [AUD-12207] [Suggestion] `_on_resolution_changed` 存在死变量及基于显示文本的 i18n 脆弱判断
  - 位置: src/features/deliver/ui/deliver_page.py:803-807
  - 证据:
    ```python
    def _on_resolution_changed(self, index: int) -> None:
        """Resolution 选择变化"""
        is_custom = self._resolution_combo.currentText().contains("Custom")
        self._custom_resolution_widget.setVisible(index == 4)  # Custom
    ```
    `is_custom` 计算后从未使用；`currentText().contains("Custom")` 在翻译 locale 下永远为 False，所幸实际逻辑用的是 `index == 4`。
  - 影响: 死代码 + 误导性写法（暗示文本匹配是判定依据），维护风险。
  - 建议: 删除 `is_custom` 行；同理检查 `_on_fps_changed` 等处统一用 index/data 判断。
  - 置信度: 高

- [AUD-12208] [Suggestion] 未使用导入与不完整类型注解
  - 位置: src/features/deliver/ui/deliver_page.py:42、44-51、171
  - 证据:
    ```python
    from typing import Any, Callable, Dict, List, Optional, Tuple
    from PyQt6.QtWidgets import (..., QApplication, ...)
    from PyQt6.QtCore import Qt, pyqtSignal, QThread, QTimer
    from PyQt6.QtGui import QColor
    ...
    self._timeline_sequences: dict = {}
    ```
    `QApplication`、`QTimer`、`QColor`、`Callable`、`Dict`、`List` 在文件内均无使用点；`_timeline_sequences: dict` 未参数化。
  - 影响: 冗余导入增加启动开销并掩盖真实依赖；注解不完整削弱静态检查。
  - 建议: 删除未使用导入；注解改为 `Dict[str, Any]`。
  - 置信度: 高

### 通过项
- 线程安全：ExportWorker 通过 pyqtSignal（queued）回报进度/完成/错误，UI 线程不做导出工作；`_progress_callback` 在 worker 线程内先赋值后使用，无跨线程读写竞争。
- GPU 资源生命周期：文件不直接接触 ModernGL/OpenGL 对象，编码器生命周期由 TimelineExporter 管理。
- 信号防递归：`_on_codec_changed` 中 `setChecked(False)`、各 SpinBox 更新均不回写已连接的控件状态；`_update_estimates → get_export_config` 只读控件，无递归风险。
- 异常与取消路径：`_cancel_export`/`_toggle_pause_export` 的 exporter API（cancel_export/pause_export/resume_export，timeline_export.py:3580/3593/3600）存在且签名匹配；ExportWorker 的 broad except 位于线程边界并以信号+日志上抛，属可接受模式。
- 输入验证：分辨率/FPS/codec 范围由 SpinBox 区间与枚举 data 限定；GIF 强制关闭音频的逻辑正确。
- edit feature 缺失时的降级（EDIT_EXPORT_AVAILABLE=False）路径完整，含 source/range/codec 备选 data。

## src/features/deliver/ui/deliver_workspace.py (行数: 572)

### 发现

- [AUD-12209] [Major] `export_completed`/`export_cancelled` 信号声明后从未发射，`export_started` 在仅"加入队列"时即发射
  - 位置: src/features/deliver/ui/deliver_workspace.py:45-47、526-535
  - 证据:
    ```python
    export_started = pyqtSignal()
    export_completed = pyqtSignal(bool)
    export_cancelled = pyqtSignal()
    ...
    def _on_add_jobs(self, jobs: List[VideoExportJob]) -> None:
        if jobs:
            self._controller.add_jobs(jobs)
            self.export_started.emit()
    ```
    全文件仅有这两处 `export_started.emit()`；controller 的 `queue_running_changed`/job 完成事件未被桥接到 `export_completed`/`export_cancelled`（全仓库检索无发射点）。
  - 影响: 对外承诺的 Legacy DeliverPage 信号契约不成立——任何监听 `export_completed`/`export_cancelled` 的调用方永远收不到通知；`export_started` 语义失真（任务只是入队、尚未渲染）。当前仓库内暂无消费者，属潜伏缺陷。
  - 建议: 将 controller 的 `queue_running_changed(bool, bool)` 与 job 结束状态桥接到这三个信号；`_on_add_jobs` 不应发 `export_started`。
  - 置信度: 高（信号未发射为事实；当前影响面有限）

- [AUD-12210] [Suggestion] `get_export_config` 宽泛 except 静默返回 None，无日志
  - 位置: src/features/deliver/ui/deliver_workspace.py:498-506
  - 证据:
    ```python
    try:
        return jobs[0].to_timeline_export_config()
    except Exception:  # noqa: BLE001 - legacy API must remain defensive
        return None
    ```
  - 影响: 配置转换的真实错误（如字段缺失、类型错误）被无痕吞掉，调用方只看到 None，难以排查。
  - 建议: 至少 `logger.warning("legacy get_export_config failed", exc_info=True)`。
  - 置信度: 高

- [AUD-12211] [Suggestion] `_last_restored_project_id` 未在 `__init__` 中声明，依赖 getattr 默认值
  - 位置: src/features/deliver/ui/deliver_workspace.py:472-478
  - 证据:
    ```python
    previous_project_id = getattr(self, "_last_restored_project_id", None)
    is_new_project = previous_project_id is None or previous_project_id != project_id
    ```
  - 影响: 实例属性延迟诞生，静态检查与可读性受损，也易与其他 `_xxx` 已声明属性风格不一致。
  - 建议: 在 `__init__` 中初始化 `self._last_restored_project_id: Optional[str] = None`。
  - 置信度: 高

- [AUD-12212] [Suggestion] `_on_open_output` 的 Popen 即发即弃，失败无反馈
  - 位置: src/features/deliver/ui/deliver_workspace.py:542-554
  - 证据:
    ```python
    if sys.platform == "darwin":
        target = path if os.path.exists(path) else os.path.dirname(path) or path
        subprocess.Popen(["open", "-R", target])  # noqa: S603
    ```
  - 影响: 打开资源管理器失败（如 xdg-open 缺失、权限问题）时用户无任何提示；Popen 对象不被等待，存在短暂僵尸进程窗口。
  - 建议: 捕获 `OSError` 并 `logger.warning`，或改用 `QDesktopServices.openUrl(QUrl.fromLocalFile(...))`。
  - 置信度: 中

- [AUD-12213] [Suggestion] `detach_edit_panels` 在原始 dock 为 None 时共享 viewer 会失去父级且不再显示
  - 位置: src/features/deliver/ui/deliver_workspace.py:369-373
  - 证据:
    ```python
    if self._shared_program_viewer is not None:
        self._shared_program_viewer.setParent(None)
        if self._program_viewer_dock is not None:
            self._program_viewer_dock.setWidget(self._shared_program_viewer)
            self._shared_program_viewer.show()
    ```
  - 影响: 若 `attach_edit_panels` 时未提供 viewer dock（参数默认 None），detach 后 viewer 变成无父隐藏顶层窗口，从 UI 中消失（timeline 分支同构但有 display-mode 恢复）。当前调用方通常传入 dock，属边界缺陷。
  - 建议: dock 为 None 时至少 `show()` 或记录警告，理想情况下保存/恢复 attach 前的原始 parent。
  - 置信度: 中

### 通过项
- 线程安全：工作区自身不触碰渲染线程；controller/job 执行在独立模块，UI 仅经 pyqtSignal（jobs_changed/job_progress/queue_running_changed）刷新。
- GPU 资源生命周期：不直接持有 ModernGL 对象。
- 信号防递归：`_on_shared_timeline_changed → settings_panel.set_sequence` 下游（video_export_settings_panel.py:1505-1560）使用 `_building_ui` 状态锁与 blockSignals，无回环路径回到 workspace。
- 状态提交一致性：`set_project` 在切换工程时清空内存队列后 `restore_from_disk()`，按工程身份去重恢复；队列面板经 `jobs_changed` 强制刷新。
- 输入验证：`set_project` 对 project_id/revision/name 做 str/int 强转与默认值兜底；`timelines` 映射 vs 列表两种形态均正确展开（dict 取 values）。
- detach/attach 共享 Edit 面板的 reparent 流程有 `_edit_panels_attached` 幂等锁；`timeline_changed` 连接/断开对称且有 (TypeError, RuntimeError) 保护。
- `_on_batch_host_mode_changed` 对 main_window 的 all_docks/dockVideoBatchExport 操作全部使用 getattr/isinstance 防御，动态 dock 的注册与注销对称。

## src/features/deliver/ui/detachable_panel_host.py (行数: 268)

### 发现

- [AUD-12214] [Major] `float_panel` 已浮动分支的布尔表达式优先级错误，`floating` 参数在指定 area 时恒被忽略
  - 位置: src/features/deliver/ui/detachable_panel_host.py:135-140
  - 证据:
    ```python
    if self._mode == "floating" and self._dock is not None:
        # Already floating — update area/floating instead of recreating.
        if area is not None:
            self._main_window.addDockWidget(area, self._dock)
        self._dock.setFloating(floating and area is None or area is None)
        return True
    ```
    Python 运算符优先级下该表达式等价于 `(floating and area is None) or (area is None)`：area 非 None 时恒为 False。docstring 承诺 "when ``area`` is given, ``True`` keeps it as a floating window anchored to that area, ``False`` docks it"。
  - 影响: 公共 API 契约破坏——`float_panel(area, floating=True)` 会把本应保持浮动的窗口直接停靠。当前仓库内调用（workspace/restore_state）均为无参调用不受影响，属潜伏缺陷。
  - 建议: 改为语义明确的 `self._dock.setFloating(bool(floating) or area is None)` 并加注释，或拆成两个显式分支。
  - 置信度: 高（逻辑错误确定；影响为潜在）

- [AUD-12215] [Major] 注释声称 "关闭浮动 dock = 回到 splitter，由 workspace 监听 visibilityChanged 处理"，但该连接在全仓库不存在
  - 位置: src/features/deliver/ui/detachable_panel_host.py:262-266
  - 证据:
    ```python
    # Keep the panel visible when the dock is closed (close ≡ float back
    # to splitter — handled by the workspace listening to the dock's
    # visibilityChanged).
    dock.setMinimumHeight(220)
    ```
    全 src/ 检索 `visibilityChanged` 仅命中此注释（dock_layout_controller.py 中连接的均为其他 dock）；DeliverWorkspace 未对 `_batch_panel_host.dock` 做任何 visibilityChanged 连接。
  - 影响: 用户关闭浮动队列窗口后，batch panel 隐藏在已关闭的 QDockWidget 内且 DockWidgetClosable 已启用；Deliver 页内嵌槽位不会自动恢复，只能依赖主窗口 dock 菜单重新显示该 dock——与注释承诺的 "close ≡ float back to splitter" 行为不符，误导后续维护。
  - 建议: 在 workspace `attach_batch_panel_host` 中连接 `dock.visibilityChanged`，False 时调用 `return_batch_panel_to_deliver()`；或删除/更正注释。
  - 置信度: 高（连接不存在为事实；用户可恢复路径取决于主窗口 dock 菜单）

- [AUD-12216] [Suggestion] `restore_state` 的 singleShot lambda 持有 self，宿主在 tick 前销毁会对已删 QObject 调用方法
  - 位置: src/features/deliver/ui/detachable_panel_host.py:236-237
  - 证据:
    ```python
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(0, lambda: self.float_panel())
    ```
  - 影响: 窗口期极小（下一事件循环 tick），但若 host 已被销毁会抛 RuntimeError 打到 stderr。
  - 建议: 用 `QTimer.singleShot(0, self, float_panel)` 的 QObject 重载（PyQt6 支持 receiver 形式）自动断开。
  - 置信度: 低

### 通过项
- 线程安全：纯 UI 线程对象，无跨线程访问。
- 模式迁移资源管理：`return_to_splitter` 先 reparent 面板再 removeDockWidget+deleteLater，避免面板闪失父；`_splitter_index` 经 clamp 后插入，越界安全。
- 持久化：save_state/restore_state 的 broad except 均带日志或默认值回退（save_state 记 warning），键名与 dock_layout_controller 约定一致。
- `attach`/`float_panel`/`return_to_splitter` 的前置条件检查（panel/main_window/splitter/dock 为 None）完整，返回 bool 供调用方判断。
- PanelDockWidget 构造签名 `(title, parent)` 与 src/shared/ui/panel_divider.py:39-42 匹配；allowedAreas/features 配置正确。

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-12200 |
| Major | 8 | AUD-12201, 12202, 12203, 12204, 12205, 12209, 12214, 12215 |
| Suggestion | 8 | AUD-12206, 12207, 12208, 12210, 12211, 12212, 12213, 12216 |
