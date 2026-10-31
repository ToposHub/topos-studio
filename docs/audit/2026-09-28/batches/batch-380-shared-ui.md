# 批次报告：batch-380 [P2] src/shared/ui//

编号区间：AUD-38000–AUD-38099。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

本批次为共享 UI 组件（无 GLSL/GPU 直接操作；`playback_hud_integration.py` 标记 DORMANT(2026-08-19)）。
GLSL·HDR 维度全部文件不适用（无着色器/HDR 数据路径），各文件通过项中不再重复列出。

---

## src/shared/ui/shortcut_manager.py (行数: 510)

### 发现

- [AUD-38000] [Critical] import_preset 对外部预设的 per-value 类型零验证，非字符串/不可哈希值可致未处理 TypeError 使应用中止
  - 位置: src/shared/ui/shortcut_manager.py:399-411（配合 464-482 的 `_apply_shortcut_to_action`）
  - 证据:
    ```python
    # Drop unknown ids first.
    known = {aid: seq for aid, seq in entries.items() if aid in self._registry}
    unknown = [aid for aid in entries if aid not in self._registry]

    # Detect duplicate sequences inside the preset (excluding "" = none).
    by_sequence: dict[str, list[str]] = {}
    for aid, seq in known.items():
        if seq:
            by_sequence.setdefault(seq, []).append(aid)
    ```
  - 影响: 调用方 `src/app/ui/shortcut_dialog.py:325-342` 用 `json.load(fh)` 把任意 JSON 文件原样传入。预设值若为 list/dict（如 `{"shortcuts": {"file.open": [1]}}`），`setdefault(seq, ...)` 抛 `TypeError: unhashable type`；该异常发生在 Qt 槽 `_on_import_preset` 内且无人捕获，PyQt6 对槽内未处理异常调用 qFatal 使整个应用中止。若值为 int 等可哈希非字符串，则会被 `set_shortcut` 存入 config 并在 `QKeySequence(seq)` 处抛 TypeError（注册时崩溃/配置污染）。违反"外部数据须验证"铁律。
  - 建议: 在构建 `known` 时逐项校验 `isinstance(seq, str)`（或进一步 `QKeySequence(seq)` 可构造），非法项计入 `errors`/`unknown` 报告而非进入冲突检测与应用流程。
  - 置信度: 高（代码路径与外部输入源均已核实；PyQt6 槽内未处理异常会中止进程）

- [AUD-38011] [Suggestion] import/replace 路径每条目触发一次全量 config 落盘（O(N) 次写盘）
  - 位置: src/shared/ui/shortcut_manager.py:416-420（`set_shortcut` 内 282 行 `self._save_custom_shortcuts()`）
  - 证据:
    ```python
    for aid, seq in sorted(known.items()):
        if aid in conflicting_ids:
            continue
        if self.set_shortcut(aid, seq):
            applied.append(aid)
    ```
    而 `set_shortcut` 每次都调用 `self._save_custom_shortcuts()` → `config_manager.save_config()`。
  - 影响: 导入含上百条快捷键的预设（replace 模式还叠加 `reset_all()` 的一次）会产生上百次同步完整配置写盘，UI 卡顿且磨损磁盘。
  - 建议: 为 `set_shortcut` 增加 `persist: bool = True` 参数（或引入批量事务），`import_preset`/`reset_category` 循环结束后一次性保存并发射信号。
  - 置信度: 高

- [AUD-38012] [Suggestion] 从 config 加载的 custom_shortcuts 值类型未验证，损坏配置可在注册阶段抛异常
  - 位置: src/shared/ui/shortcut_manager.py:122-126（配合 469-471）
  - 证据:
    ```python
    if self._config_manager:
        saved = self._config_manager.get("custom_shortcuts")
        if saved and isinstance(saved, dict):
            self._custom_shortcuts = saved
    ```
  - 影响: 只校验了顶层 dict，未校验 value 为 str。config.json 属外部可编辑数据，值被手改为 list/int 后 `QKeySequence(seq)` 在 `register()`→`_apply_shortcut_to_action()` 时抛 TypeError，可能阻断启动期注册流程。
  - 建议: 加载时逐项过滤 `isinstance(v, str)`，丢弃非法项并记日志。
  - 置信度: 中（需配置被外部篡改才触发；缺失验证本身确定）

- [AUD-38013] [Suggestion] import_preset 的 mode 参数未验证，未知值静默按 merge 处理
  - 位置: src/shared/ui/shortcut_manager.py:413-414
  - 证据:
    ```python
    if mode == "replace":
        self.reset_all()
    ```
  - 影响: 传入 `"replce"` 等拼错的 mode 不报错、行为静默退化为 merge，调用方无从察觉语义偏差。
  - 建议: 入口处校验 `mode in ("merge", "replace")`，非法值写入 `errors` 并提前返回报告。
  - 置信度: 高

### 通过项
- 线程安全：单例在 UI 线程创建与使用，无跨线程访问路径
- 信号防递归：shortcuts_changed 只在修改型方法尾部发射，槽内无再入修改路径
- 状态提交一致性：每次修改即时持久化（set/remove/reset 路径均落盘），export/import 预设可往返
- 冲突检测：find_conflicts/import_preset 内部冲突集（预设内重复序列整组跳过）逻辑正确
- 快捷键作用域：_shortcut_context 尊重 force_widget/force_application 属性与 global_shortcuts 偏好
- 异常路径：import_preset 对 schema/version/shortcuts 结构错误均有结构化报告

---

## src/shared/ui/column_grid.py (行数: 222)

### 发现

- [AUD-38001] [Major] apply_columns 每次执行都同步写 QSettings，窗口拖拽缩放期间产生高频磁盘写入
  - 位置: src/shared/ui/column_grid.py:182-206（关键在 203-206；触发源 96-102 的 0ms retile 定时器与 107 的 slider.valueChanged）
  - 证据:
    ```python
    self._view.doItemsLayout()
    if self._settings_key is not None:
        from PyQt6.QtCore import QSettings
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(
            self._settings_key, self._columns)
    ```
  - 影响: resize → 事件过滤器 → 0ms 单发定时器 → `apply_columns`，即交互式拖拽窗口期间每个事件循环回合都会新建 QSettings、setValue、对象析构触发 sync 落盘一次；滑动 slider 同理逐 tick 落盘。值几乎总是未变的列数，属纯浪费的同步磁盘 I/O，与 4K 实时性能基准相悖，可致缩放拖拽卡顿。
  - 建议: 仅当 `self._columns` 实际变化时才写 QSettings（在赋值前后比较），或用脏标记在定时器空闲时统一 sync。
  - 置信度: 高（新建 QSettings 对象析构即 sync，行为确定）

### 通过项
- 线程安全：纯 UI 线程对象，0ms 定时器父挂到 view，重父化安全（注释 Pitfall 4 明确）
- 信号防递归：apply_columns 不回写 slider 值，valueChanged→apply_columns 无环路
- 输入验证：columns_min/max/default 互相钳制、gap/aspect/extra_height 均防御；_stored_columns 对脏值 int() 失败回退默认
- 异常路径：eventFilter 仅捕获窄域 AttributeError 处理析构竞态并注释说明，非宽泛 except
- 性能模式：2px 松弛防换行、常显滚动条防宽度振荡、hint 重钉等 Qt 陷阱均有封装注释
- 类型注解：完整（含 Callable/Optional 精确类型）

---

## src/shared/ui/effect_degradation_indicator.py (行数: 309)

### 发现

- [AUD-38002] [Major] EffectDegradationPanel.refresh_settings 程序化 setChecked 未加信号锁，触发伪信号/manager 写入甚至递归回路
  - 位置: src/shared/ui/effect_degradation_indicator.py:306-309（对照同文件正确范式 164-166）
  - 证据:
    ```python
    def refresh_settings(self) -> None:
        """刷新设置显示"""
        self._enable_checkbox.setChecked(self._manager.is_degradation_enabled())
        self._update_effects_list()
    ```
    而该 checkbox 已连接 `stateChanged` → `_on_global_toggle`（298-304 行，会 `set_degradation_enabled` 并 emit `degradation_settings_changed`）。
  - 影响: 违反"程序化更新控件须状态锁防递归信号"铁律。只要 setChecked 引起状态翻转，就触发一次多余的 manager 写入和 `degradation_settings_changed` 发射；若任何消费者响应该信号又调用 refresh_settings，即形成无限信号回路。同文件 Indicator._update_display 已用 blockSignals 正确处理同一场景，范式不一致。
  - 建议: 与 Indicator 一致，setChecked 前后 blockSignals(True/False)。
  - 置信度: 高（伪发射确定发生；递环取决于外部接线）

- [AUD-38005] [Suggestion] 多个导入符号未使用（List/QToolTip/QFont/QColor/QPalette）
  - 位置: src/shared/ui/effect_degradation_indicator.py:25,33,36
  - 证据:
    ```python
    from typing import Optional, List
    ...
        QToolTip,
    )
    from PyQt6.QtGui import QFont, QColor, QPalette
    ```
  - 影响: 死导入增加阅读噪音，暗示未完成的功能残留。
  - 建议: 删除未使用导入。
  - 置信度: 高（全文件无这些名称的使用点）

- [AUD-38006] [Suggestion] 更新定时器无条件常驻运行（500ms），控件隐藏时不暂停
  - 位置: src/shared/ui/effect_degradation_indicator.py:67-69
  - 证据:
    ```python
    self._update_timer = QTimer(self)
    self._update_timer.timeout.connect(self._update_display)
    self._update_timer.start(500)  # 每 500ms 更新
    ```
  - 影响: 指示器不可见时仍每 500ms 调用 `get_degradation_summary()` 并执行 setText/setStyleSheet；对照 playback_hud.py 的 HUD 面板在隐藏时会停表，此处缺失该模式，属持续空转开销。
  - 建议: 重写 hideEvent/showEvent 或在 showEvent 中启停定时器。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程，manager 为进程内单例
- 信号防递归：Indicator._update_display 的 checkbox 更新正确使用 blockSignals（164-166）
- 状态提交一致性：开关状态经 manager.set_degradation_enabled 即时生效，Checkbox 初始同步自 manager
- 输入验证：manager.get_degradation_summary() 返回键（is_playing/degradation_enabled/degraded_count/degraded_effects）已在 preview_quality.py:523-539 核实存在，直接索引安全
- 异常路径：无 except，无掩盖；degraded_effects[:3] 切片对空列表安全
- 类型注解：完整

---

## src/shared/ui/playback_hud.py (行数: 1180)

### 发现

- [AUD-38003] [Major] HUD 关闭按钮直连 QWidget.hide，绕过 hide_hud 状态同步：_visible 残留 True、定时器不停、visibility_changed 不发射
  - 位置: src/shared/ui/playback_hud.py:788-802（对照 950-955 的 hide_hud 与 932-941 的 toggle_visibility）
  - 证据:
    ```python
    close_btn = QPushButton("X")
    ...
    close_btn.clicked.connect(self.hide)
    ```
    而 `hide_hud` 才做完整收尾：
    ```python
    self._visible = False
    self.setVisible(False)
    self.visibility_changed.emit(False)
    self._update_timer.stop()
    ```
  - 影响: 用户点 X 后面板隐藏但 `_visible` 仍为 True、500ms 更新定时器继续空转刷新隐藏标签；下一次 toggle_visibility 先翻成 False 再 setVisible(False)（无视觉变化），需按两次快捷键才能重新显示；集成层（playback_hud_integration.toggle_visibility 依赖 is_visible() 同步 dock 与 QAction 勾选态）随之失步，出现"空 dock 仍显示/勾选态错位"。
  - 建议: `close_btn.clicked.connect(self.hide_hud)`；或重写 hideEvent 同步 `_visible`/定时器并发射信号。
  - 置信度: 高（状态机路径直接推演可得）

- [AUD-38007] [Suggestion] record_sample 样本列表无界增长且每样本全量重排序，_max_history/_history 声明后从未使用
  - 位置: src/shared/ui/playback_hud.py:283-292, 557-597
  - 证据:
    ```python
    self._history: List[Dict[str, Any]] = []
    self._max_history: int = 1000  # 最多保留 1000 条记录

    self._decode_times: List[float] = []
    ...
    if decode_ms is not None:
        self._decode_times.append(decode_ms)
    ...
    self._perf_stats.decode_ms = self._calculate_percentiles(self._decode_times)
    ```
  - 影响: 每次调用对 5 个列表 `sorted()` 全量排序（O(n log n)），长期调用累计 O(n²)；列表无截断（`_max_history` 是死代码，`_history` 从未 append），内存无界。当前仅 `__main__` 测试块调用（已核实无生产调用方），故降为 Suggestion，但一旦按帧采样接入即是性能地雷。
  - 建议: 固定容量环形缓冲或滑窗截断到 `_max_history`；百分位改用增量/采样算法。
  - 置信度: 高

- [AUD-38008] [Suggestion] export_log 用宽泛 except Exception 吞掉全部失败且不给用户任何反馈
  - 位置: src/shared/ui/playback_hud.py:693-702（配合 1103-1110 调用方）
  - 证据:
    ```python
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            logger.info(f"Playback log exported to: {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"Failed to export log: {e}")
            return ""
    ```
  - 影响: 违反"不得宽泛 except 掩盖领域错误"：编码错误、权限、磁盘满乃至代码缺陷一律仅记日志返回 ""；调用方 `_on_export_clicked` 里 `if output_path:` 静默跳过，用户点击导出后无任何成功/失败提示（仅剪贴板写路径这一隐式暗示）。
  - 建议: 收窄为 OSError/ValueError，并向调用方/用户传递失败状态（如 QMessageBox 或返回 Result）。
  - 置信度: 高

- [AUD-38009] [Suggestion] 模块 docstring 使用示例错误：用 PlaybackHUDPanel 构造"数据模型"
  - 位置: src/shared/ui/playback_hud.py:49-51
  - 证据:
    ```python
    # 创建数据模型并连接
    hud_model = PlaybackHUDPanel(hud_panel)
    hud_model.set_video_info(metadata, pipeline_decision)
    ```
  - 影响: 示例把 Panel 当 Model 构造，且 `set_video_info` 方法在两个类中均不存在（实际是 start_session），照抄即报错，误导集成方。
  - 建议: 改为 `hud_model = PlaybackHUDModel()` 并调用真实 API `start_session(...)`。
  - 置信度: 高

### 通过项
- 线程安全：模型仅在 UI 线程被面板方法访问，无内部线程；若集成方跨线程调用需自行排队（本模块无此路径）
- GPU 资源生命周期：不接触 OpenGL/纹理，纯指标展示
- 信号防递归：visibility_changed 仅在显隐方法中单向发射；标签更新无信号回环
- 状态提交一致性：会话数据 reset/start_session 语义清晰，导出与显示分离
- 性能模式：500ms 节流更新、隐藏停表（show_hud/hide_hud 路径）、queue_depth 仅显示 p95，设计良好
- 输入验证：update_from_monitor 用 stats.get(..., 默认值) 全量防御，monitor/session 为 None 提前返回
- 类型注解：dataclass 与方法签名完整

---

## src/shared/ui/playback_hud_integration.py (行数: 464)

### 发现

- [AUD-38004] [Major] on_playback_started 深入访问 source_manager 私有属性且无 hasattr 防御
  - 位置: src/shared/ui/playback_hud_integration.py:299-317
  - 证据:
    ```python
    if source_manager:
        # disk_proxy/memory_proxy/master_only
        if source_manager._disk_proxy_source:
            source_type = 'disk_proxy'
        elif source_manager._proxy_source and source_manager._proxy_source != source_manager._master_source:
            source_type = 'memory_proxy'
        ...
        if hasattr(source_manager._proxy_source, 'is_planar_mode'):
    ```
  - 影响: 跨模块读取 `_disk_proxy_source/_proxy_source/_master_source` 私有内部状态（定义于 src/shared/video/playback_clock.py:167 等），破坏封装且极度脆弱：仅对 `_proxy_source.is_planar_mode` 做了 hasattr，三个属性本身无防御，换任何 source_manager 实现即 AttributeError。模块顶部已标 DORMANT(2026-08-19)，接线前必须先修。
  - 建议: 在 PlaybackClock/SourceManager 上提供公共的 `source_type` / `frame_path` 查询接口，集成层只读公共 API。
  - 置信度: 中（私有属性当前确实存在于 playback_clock.py；崩溃取决于未来接线对象）

- [AUD-38010] [Suggestion] 硬编码中文字符串与快捷键绕过 I18N/ShortcutManager 体系
  - 位置: src/shared/ui/playback_hud_integration.py:198, 221-228
  - 证据:
    ```python
    self._dock = PanelDockWidget("播放性能监控", self._main_window)
    ...
    self._shortcut_action = QAction("显示播放 HUD", self._main_window)
    self._shortcut_action.setShortcut("Ctrl+Shift+H")
    ```
  - 影响: 本批次其他 UI 均经 `I18N.tr(...)` 取文案，此处硬编码中文绕过国际化；快捷键不走 ShortcutManager 注册表，不参与冲突检测、用户自定义与 global_shortcuts 作用域管理，与项目"集中注册快捷键"架构相悖。
  - 建议: 接线时改用 I18N.tr 并通过 ShortcutManager.register_action 注册（如 "view.playback_hud"）。
  - 置信度: 高（DORMANT 模块，接线时落实）

### 通过项
- 线程安全：控制器在 UI 线程构造，QAction 父挂 main_window，toggle 链路无跨线程
- GPU 资源生命周期：无 GPU 资源操作
- 信号防递归：toggle_visibility/show_hud/hide_hud 单向同步 panel→dock→action，无回环
- 状态提交一致性：会话开始/停止与 panel.start_session 委托清晰；文件头 DORMANT 状态标注规范
- 异常路径：无 except，无掩盖
- 类型注解：完整（Optional/Any 使用得当）

---

## src/shared/ui/panel_divider.py (行数: 58)

### 发现

- [AUD-38014] [Suggestion] Qt 事件覆盖方法的 event 参数缺少类型注解
  - 位置: src/shared/ui/panel_divider.py:27,52,56
  - 证据:
    ```python
    def paintEvent(self, event) -> None:  # noqa: D401 - Qt override
    ...
    def resizeEvent(self, event) -> None:
    ...
    def showEvent(self, event) -> None:
    ```
  - 影响: 与项目强类型规范不符（本批次其他文件事件签名均有 QResizeEvent 等注解）。
  - 建议: 标注为 `QPaintEvent` / `QResizeEvent` / `QShowEvent`。
  - 置信度: 高

### 通过项
- 线程安全：纯 paint/几何，UI 线程
- GPU 资源生命周期：QPainter 栈内构造析构，无泄漏
- 信号防递归：无信号
- 输入验证：C.PANEL_DIVIDER 常量已在 style_sheet.py:66 核实存在
- 覆盖层不拦截鼠标（WA_TransparentForMouseEvents），resize/show 时几何同步正确

---

## src/shared/ui/dock_context_menu.py (行数: 37)

### 发现

（无）

### 通过项
- 通过（未发现上述九维度问题）：find_parent_dock 空值/父链遍历防御正确；lambda 闭包持有 dock 引用且菜单为短生命周期对象，无悬挂风险；文案均经 I18N.tr；类型注解完整

---

## src/shared/ui/dock_tab_style.py (行数: 78)

### 发现

（无）

### 通过项
- 通过（未发现上述九维度问题）：幂等保护（`_topos_dock_tab_style_applied` 属性）防止 Qt 重建 tab bar 时重复套样式；纯 QSS + 几何设置，无状态、无信号、无资源持有；docstring 明确记录了不挂 QProxyStyle 的安全权衡

---

## 汇总

| 严重度 | 数量 |
|--------|------|
| Critical | 1 |
| Major | 4 |
| Suggestion | 10 |

- Critical：AUD-38000（shortcut_manager.import_preset 外部预设值类型零验证，可致槽内未处理 TypeError 中止应用）
- Major：AUD-38001（column_grid 每次重铺同步写盘）、AUD-38002（降级面板 refresh_settings 缺信号锁）、AUD-38003（HUD 关闭按钮绕过 hide_hud 状态同步）、AUD-38004（HUD 集成访问 source_manager 私有属性）
