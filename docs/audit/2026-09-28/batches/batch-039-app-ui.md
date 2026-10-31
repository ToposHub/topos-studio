# batch-039 [P1] 审查报告 — src/app/ui/

- 日期：2026-09-28
- 任务书：docs/audit/2026-09-28/assignments/batch-039.md
- 编号区间：AUD-03900–AUD-03999（实际使用 AUD-03900–AUD-03915）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 交叉验证文件（只读）：src/shared/preferences/__init__.py、src/shared/config/config_manager.py、src/shared/media/macos_vt/capability.py、src/shared/gpu/metal/context.py、src/shared/ui/widgets/pref_drag_value.py、external/file_explorer/main.py、external/file_explorer/plugin_system.py、external/file_explorer/db.py

## src/app/ui/preferences_dialog.py (行数: 2685)

### 发现

- [AUD-03900] [Critical] 打开偏好设置后直接 Apply/Save 会把 file_explorer 的 disabled_plugins 清空（用户插件停用状态丢失）
  - 位置: src/app/ui/preferences_dialog.py:2033-2050（辅证 :1386-1395、:1628-1639）
  - 证据:
    ```python
    # Collect disabled plugins from check states
    disabled_plugins = []
    for i in range(self._plugin_list.count()):
        item = self._plugin_list.item(i)
        if item.checkState() == Qt.CheckState.Unchecked:
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry:
                disabled_plugins.append(entry)
    ...
    fe_conf = fe_main.load_config() or {}
    fe_conf["disabled_plugins"] = disabled_plugins
    fe_main.save_config(fe_conf)
    ```
    而 `self._plugin_list` 仅在 `_create_advanced_page` 中创建（1386），唯一填充入口是用户手点 "Refresh Plugins" 按钮（1395 `refresh_btn.clicked.connect(self._refresh_plugins)`）；`__init__`/`_load_current_settings` 均不调用 `_refresh_plugins`，对话框打开时列表恒为空。
  - 影响: `_plugin_list.count()==0` → 收集结果恒为 `[]`，`_apply_settings` 无条件覆写 FE config 的 `disabled_plugins` 为空列表。用户此前停用的全部 FE 插件被静默重新启用；且 `_fe_disabled_plugins`（1619-1626）用宽泛 `except Exception: return []` 吞掉 FE 配置读取错误，同样放大为"全部启用"。这属于用户配置数据丢失。
  - 建议: 在对话框初始化或首次进入 Advanced 页时自动调用 `_refresh_plugins()`；当插件列表为空（未刷新/FE 未运行）时跳过对 FE config 的 `disabled_plugins` 覆写，仅在有真实条目或明确用户交互后才写回；`_fe_disabled_plugins` 失败时应中止写回而非返回 `[]`。
  - 置信度: 高

- [AUD-03901] [Major] `_restore_preview_snapshot` 程序化更新控件未加状态锁，Cancel 时每个外观项双重发射预览信号
  - 位置: src/app/ui/preferences_dialog.py:341-364
  - 证据:
    ```python
    def _restore_preview_snapshot(self) -> None:
        """Restore appearance settings from snapshot."""
        if not self._preview_snapshot:
            return
        scale = self._preview_snapshot.get('ui_scale', 100)
        scale_combo = self._widgets['ui_scale']
        for i in range(scale_combo.count()):
            if scale_combo.itemData(i) == scale:
                scale_combo.setCurrentIndex(i)
                break
        ...
        self._emit_batch_preview()
    ```
    `scale_combo.setCurrentIndex(...)`、`panel_opacity.setValue(...)` 会触发已在 `_connect_preview_signals`（375-383）连接的 `currentIndexChanged`/`valueChanged` → `_on_preview_setting_changed` 逐项发射一次；随后 364 行 `_emit_batch_preview()` 再对全部 key 发射一次。
  - 影响: Cancel/回滚路径上每个 previewable key 的 `appearance_preview_changed` 被发射两次，主窗口重复应用 ui_scale/accent（潜在闪烁、多余重排版）。同时直接违反 .clinerules 铁律"程序化更新控件须状态锁防递归信号"——`_on_restore_defaults`（2121/2257）正确使用了 `self._preview_blocked`，此路径漏用。
  - 建议: 在 `_restore_preview_snapshot` 开头置 `self._preview_blocked = True`，恢复完控件值后复位，再调用 `_emit_batch_preview()`（与 `_on_restore_defaults` 同一模式）。
  - 置信度: 高

- [AUD-03902] [Major] Restore Defaults 漏掉 `pass_fusion_mode` 与 `render_cache_auto_threshold`，恢复默认后这两项仍保留用户值
  - 位置: src/app/ui/preferences_dialog.py:2107-2258（缺失项对照 :804、:1199、:1966、:1984）
  - 证据:
    ```python
    # Performance (app-level)
    self._widgets['gpu_acceleration'].setChecked(defaults['gpu_acceleration'])
    ...
    self._widgets['processing_threads'].setValue(defaults.get('processing_threads', 0))
    #（随后直接跳到 Shortcuts 段，无 pass_fusion_mode）
    ...
    self._select_combo_by_data('render_cache_codec', defaults.get('render_cache_codec', 'topos_proxy'))
    self._select_combo_by_data('proxy_format', defaults.get('proxy_format', 'topos_proxy'))
    #（Render Cache 段无 render_cache_auto_threshold）
    ```
    `_apply_settings` 写入 `pass_fusion_mode`（1966）与 `render_cache_auto_threshold`（1984），但 `_on_restore_defaults` 全函数（2107-2258）无任何对这两个 widget 的重置。
  - 影响: 用户点 "Restore Defaults" 后：Pass Fusion Mode 仍保持 aggressive/off 等用户选择；Smart Score Threshold 仍保持用户数值——与"恢复全部默认值"的承诺不符，且这两个键均不在 `ConfigManager.DEFAULT_CONFIG`（已核实 config_manager.py 中无此二键），后续 Apply 会把残留用户值当作"默认"写回。
  - 建议: 在 restore 中补 `self._select_combo_by_data('pass_fusion_mode', defaults.get('pass_fusion_mode', 'conservative'))` 与 `self._widgets['render_cache_auto_threshold'].setValue(defaults.get('render_cache_auto_threshold', 6))`，并在 DEFAULT_CONFIG 中登记这两个键。
  - 置信度: 高

- [AUD-03903] [Major] Cancel 回滚不完整：Apply 已直写的 FE config 与模块页副作用无法撤销
  - 位置: src/app/ui/preferences_dialog.py:2098-2105（对照 :2042-2049、:2053-2057）
  - 证据:
    ```python
    def _on_cancel(self) -> None:
        """Restore settings and close dialog."""
        # If Apply was pressed, restore the full config from snapshot
        if self._applied:
            self._config_manager.config = dict(self._config_snapshot)
            self._config_manager.save_config()
        self._restore_preview_snapshot()
        self.reject()
    ```
    而 `_apply_settings` 中 `fe_main.save_config(fe_conf)`（2047）直接写 FE 自己的 config.json，`page_obj.apply(cfg)`（2055）可产生模块级副作用（如重建设置、失效缓存）。
  - 影响: Apply → Cancel 后宿主 config 被还原，但 FE 插件停用列表（叠加 AUD-03900 即被清空）与各模块页 apply 产生的外部副作用永久保留，Cancel 语义被破坏（用户以为一切还原）。
  - 建议: 在 `_apply_settings` 前记录 FE config 快照（`fe_main.load_config()`），Cancel 时一并还原；模块页协议增加可选的 `rollback()` 或将副作用延迟到 Save。
  - 置信度: 高

- [AUD-03904] [Major] Cancel 直接整体替换 `config_manager.config`，绕过 ConfigManager 的 `_file_lock`，与并发 `set()`/`save_config()` 存在竞态
  - 位置: src/app/ui/preferences_dialog.py:2102-2103
  - 证据:
    ```python
    if self._applied:
        self._config_manager.config = dict(self._config_snapshot)
        self._config_manager.save_config()
    ```
    ConfigManager 的写入路径以 `self._file_lock` 串行化（config_manager.py:617 `with self._file_lock:` 内读 self.config 并写盘；`set()`（650-656）无锁改 `self.config[key]` 并登记 `_dirty_keys`）。
  - 影响: 属性整体替换发生在锁外：若其它线程（自动保存计时器、退出时几何写入等）正在 `set()` 或已进入 `save_config()` 读取 self.config，会产生丢失并发键或"半旧半新"的撕裂保存；`_dirty_keys` 中残留的脏键在还原保存时又从被替换后的 dict 取值，行为依赖时序。
  - 建议: 在 ConfigManager 上提供原子的 `restore_snapshot(dict)`（锁内置换 config、清理或重放 `_dirty_keys`），对话框改调该方法，而不是裸赋值公开属性。
  - 置信度: 中（竞态窗口窄，且需存在非 UI 线程写配置；`_file_lock` 的存在表明设计上允许多线程写）

- [AUD-03905] [Major] Timeline 分辨率预设下拉从不与配置同步：打开即显示 "4K UHD"，实际宽度/高度可能是 1920x1080
  - 位置: src/app/ui/preferences_dialog.py:867-885（`_widgets['_timeline_res_combo']` 存后从未读取）
  - 证据:
    ```python
    for item in res_preset_items:
        res_combo.addItem(item[0])
    res_combo.currentIndexChanged.connect(
        lambda idx: self._on_timeline_res_preset_changed(idx, res_preset_items, width_spin, height_spin)
    )
    res_layout.addWidget(res_combo)
    self._widgets['_timeline_res_combo'] = res_combo
    ```
    `_load_current_settings`（1863-1868）只 `setValue` width/height，从不按当前宽高反选 combo；`_on_restore_defaults`（2231-2236）同样只设 spins。全文件 grep 确认 `_timeline_res_combo` 仅 873 行出现一次。
  - 影响: 预设框恒停在第 0 项 "4K UHD (3840x2160)"，与旁边 spins 显示的 1920x1080 矛盾；用户误读当前默认分辨率。所幸 `_apply_settings` 只读 spins（1996-2001），不会写坏数据——属误导性 UI 状态缺陷。
  - 建议: 装载/恢复时根据 width/height 匹配 preset（无匹配则选 "Custom"），或提供 `_sync_res_combo(width, height)` 在 load 与 restore 末尾调用。
  - 置信度: 高

- [AUD-03906] [Major] 模块偏好页 load/apply/restore_defaults 委托全部 `except Exception: pass`，静默吞掉模块页持久化失败
  - 位置: src/app/ui/preferences_dialog.py:1899-1903、:2053-2057、:2250-2254
  - 证据:
    ```python
    # Delegate apply to module preference pages
    for page_obj in self._module_pages.values():
        try:
            page_obj.apply(cfg)
        except Exception:
            pass  # Module page may not be visible
    ```
    load（1901-1903）与 restore_defaults（2252-2254）同模式。
  - 影响: 模块页（Color & Processing、Export、Composite、AI Editing 等）apply 抛错时用户毫无感知：界面提示已保存、`cfg.save_config()` 照常落盘，但该模块的设置实际未写入——违反铁律"不得宽泛 except 掩盖领域错误"，且注释"Module page may not be visible"与事实不符（不可见不致异常）。restore 路径同理会把异常吞成"默认值已恢复"的假象。
  - 建议: 捕获后至少 `logger.exception(...)` 并聚合到状态栏/消息框提示（apply 失败应阻止或警示 Save）；异常类型收窄为模块页协议声明的预期异常。
  - 置信度: 高

- [AUD-03907] [Major] 外部 config 数值未做类型验证即 `int()`/`setValue()`，手工编辑或损坏的 config.json 会使对话框构造即崩溃
  - 位置: src/app/ui/preferences_dialog.py:1853、:1879（同族 :1771-1800、:1827、:1843-1849）
  - 证据:
    ```python
    self._widgets['render_cache_auto_threshold'].setValue(int(cfg.get('render_cache_auto_threshold', 6) or 6))
    ...
    sub_dur_w.setValue(int(cfg.get('default_subtitle_clip_duration_frames', 72)))
    ...
    self._widgets['panel_opacity'].setValue(cfg.get('panel_opacity', 100))
    self._widgets['zoom_speed'].setValue(cfg.get('zoom_speed', 100))
    ```
  - 影响: config.json 属外部数据（用户可手改、旧版本可残留）：`"render_cache_auto_threshold": "6x"` → `int()` 抛 ValueError；`"panel_opacity": "100"`（字符串）→ `QSlider.setValue(str)` 抛 TypeError——均在 `__init__` 的 `_load_current_settings` 内，异常逃逸到槽/构造处直接崩溃，违反铁律"外部数据须验证"。
  - 建议: 加统一的 coerce 工具（如 `_as_int(value, default)` 容错转换）应用于所有数值键装载路径；枚举键白名单校验后再选中。
  - 置信度: 中（需配置值类型异常才触发，但一旦触发即崩溃）

- [AUD-03908] [Suggestion] 缓存体积统计在 UI 线程同步扫盘且只统计顶层文件：切页即卡顿、数值偏低
  - 位置: src/app/ui/preferences_dialog.py:1693-1733（触发点 :1914-1917）
  - 证据:
    ```python
    def _on_category_changed(self, row: int) -> None:
        ...
        if row < len(page_ids) and page_ids[row].page_id == "app.file_storage":
            self._update_cache_size_label()
    ...
    for f in os.listdir(cache_dir):
        fp = os.path.join(cache_dir, f)
        if os.path.isfile(fp):
            try:
                total_bytes += os.path.getsize(fp)
    ```
  - 影响: 每次切到 File & Storage 页都在 UI 线程 `os.listdir`+`getsize` 全量遍历两个缓存目录（缓存上限默认 10GB+50GB 渲染缓存，文件数可达数千），大缓存时阻塞 UI 数秒；且 `_clear_cache` 会 rmtree 子目录（1515-1518），统计却跳过目录，标签长期低估实际占用。
  - 建议: 移到后台线程/QtConcurrent 汇总后信号回填；用 `os.walk` 递归统计；缓存结果避免同会话重复扫描。
  - 置信度: 高（代码路径确定；卡顿时长依赖缓存规模）

- [AUD-03909] [Suggestion] `_on_apply` 清 VT capability 缓存而 `_on_save` 不清，两个提交路径行为不对称
  - 位置: src/app/ui/preferences_dialog.py:2066-2071（对照 :2073-2096）
  - 证据:
    ```python
    def _on_apply(self) -> None:
        self._applied = True
        self._apply_settings()
        try:
            from src.shared.media.macos_vt.capability import clear_capability_cache
            clear_capability_cache()
        except Exception:
            pass
    ```
    `_on_save` 只调 `self._apply_settings()`，无 `clear_capability_cache()`。已核实 capability.py 存在进程级 `_cached_capability`（:67、:433-437），`clear_capability_cache` 注释"使下次查询重新读取配置"。
  - 影响: 经 Save（而非 Apply）提交 VT 设置后，进程内 capability 缓存未失效；由于 `_get_zero_copy_mode` 每次现读配置，实际影响主要是探测结果刷新滞后，功能影响有限，故列为 Suggestion。
  - 建议: 将"apply 后清能力缓存"收敛进 `_apply_settings` 尾部（或抽出 `_post_commit()` 供两条路径共用）。
  - 置信度: 中

- [AUD-03910] [Suggestion] `_reset_all_shortcuts` 在对话框内立即破坏性提交，游离于 Apply/Cancel 模型之外
  - 位置: src/app/ui/preferences_dialog.py:1477-1488
  - 证据:
    ```python
    if reply == QMessageBox.StandardButton.Yes and self._shortcut_manager:
        self._shortcut_manager.reset_all()
    ```
  - 影响: 用户在 Shortcuts 页点 "Reset All Shortcuts" 后按 Cancel，所有自定义快捷键已被重置且无法经 `_on_cancel` 回滚（Cancel 只还原 config 快照与外观预览），与对话框其余设置的"确认后统一提交"语义不一致。有二次确认弹窗缓解，故仅 Suggestion。
  - 建议: 记录快捷表快照供 Cancel 恢复，或把重置动作并入 Apply/Save 提交阶段。
  - 置信度: 高

- [AUD-03911] [Suggestion] 重启提示文案固定为"语言变更"，但 work_mode / developer_mode 变更也走同一提示
  - 位置: src/app/ui/preferences_dialog.py:2260-2267（触发条件 :2077-2095）
  - 证据:
    ```python
    if wm_widget is not None and wm_widget.currentData() != self._initial_work_mode:
        self._needs_restart = True
    if self._widgets['developer_mode'].isChecked() != self._initial_developer_mode:
        self._needs_restart = True
    ...
    I18N.tr("dialog.restart_message",
            "The application needs to restart for the language change to take effect.\n\n"
    ```
  - 影响: 因切换工作模式或开发者模式触发的重启提示仍宣称是"语言更改"，误导用户。
  - 建议: 按 `_needs_restart` 的具体原因生成消息（locale/work_mode/developer_mode 分别或合并措辞）。
  - 置信度: 高

- [AUD-03912] [Suggestion] 诊断导出在 UI 线程同步 `import torch` / `import onnxruntime`，重依赖导入可阻塞界面数秒
  - 位置: src/app/ui/preferences_dialog.py:1548-1568
  - 证据:
    ```python
    try:
        import onnxruntime as ort
        diagnostics["onnx_runtime"] = {...}
    except ImportError:
        diagnostics["onnx_runtime"] = "not installed"
    try:
        import torch
        diagnostics["cuda"] = {...}
    ```
  - 影响: 点击 "Export Diagnostics Report" 后 UI 线程冷导入 torch（首次可达数秒）期间对话框冻结、无进度反馈。
  - 建议: 移入后台线程采集后回填，或采集前设忙碌光标/进度提示。
  - 置信度: 高（代码路径确定；耗时依赖环境）

- [AUD-03913] [Suggestion] `_select_combo_by_data` 未命中时静默选中第 0 项，未知配置值在下次 Apply 时被无声改写
  - 位置: src/app/ui/preferences_dialog.py:1737-1748（消费示例 :1962-1964）
  - 证据:
    ```python
    for i in range(combo.count()):
        if combo.itemData(i) == data_value:
            combo.setCurrentIndex(i)
            return
    # Default to first item if not found
    if combo.count() > 0:
        combo.setCurrentIndex(0)
    ```
    同族：`video_playback_resolution` 装载按值匹配 `VIDEO_RESOLUTIONS`（1820-1825），非表内值时 combo 停留 "4K (3840)"，Apply 再经 `VIDEO_RESOLUTIONS.get(res_label, 1920)` 固化。
  - 影响: 手改配置里的枚举值（如 `render_cache_codec: "prores"`、分辨率 2048）被静默替换为第一项并随 Apply 持久化，用户无从得知配置被改。
  - 建议: 未命中时记日志并可加视觉标记（如 "Custom/unknown" 项），或在 Apply 前提示将发生值迁移。
  - 置信度: 高

- [AUD-03914] [Suggestion] `_on_restore_defaults` 的 `_preview_blocked` 置位/复位无 try/finally 保护
  - 位置: src/app/ui/preferences_dialog.py:2121、:2257
  - 证据:
    ```python
    # Block preview signals during mass widget updates to prevent flickering
    self._preview_blocked = True
    defaults = self._config_manager.DEFAULT_CONFIG
    ...（约 130 行批量更新，含 `defaults['...']` 直接取键）
    # Unblock preview signals and emit batch preview for all previewable keys
    self._preview_blocked = False
    ```
  - 影响: 中途任何异常（如 DEFAULT_CONFIG 键缺失引发 KeyError、 AUD-03907 类类型错误）都会跳过复位，`_preview_blocked` 永久为 True，本会话内外观实时预览全部失效且无提示。
  - 建议: 用 `try: ... finally: self._preview_blocked = False` 包裹批量更新段。
  - 置信度: 高

- [AUD-03915] [Suggestion] 少数方法/参数缺少类型注解
  - 位置: src/app/ui/preferences_dialog.py:114、:1609、:974、:2281
  - 证据:
    ```python
    def _build_page_registry(self, module_pages=None):
    def _fe_plugin_bridge(self):
    def _on_timeline_res_preset_changed(self, idx: int, preset_items: list, width_spin: QSpinBox, height_spin: QSpinBox) -> None:
    def keyPressEvent(self, event) -> None:
    ```
  - 影响: 与代码库"所有函数有类型注解"约束不符；`preset_items` 用裸 `list`、`module_pages`/返回值未标注，降低可读性与静态检查覆盖。
  - 建议: 补齐 `module_pages: list[ModularPreferencePage] | None`、`-> PreferencesRegistry`、`event: QKeyEvent`、`preset_items: list[tuple[str, int, int]]` 等。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：全文件不创建/持有 ModernGL 或 GL 上下文对象；Metal 探测仅调用进程级缓存的元数据检查（metal/context.py:112-132 结果缓存），无 UI 线程 GPU 资源操作。
- GLSL·HDR 正确性：本文件无着色器代码与 HDR 像素数据路径，不适用。
- 信号防递归（装载路径）：`__init__` 顺序为 `_load_current_settings()` → `_connect_preview_signals()` → `_save_preview_snapshot()`（110-112），初始装载不会触发预览信号；`_on_restore_defaults` 正确使用 `_preview_blocked` 状态锁（缺陷仅在 `_restore_preview_snapshot`，见 AUD-03901）。
- 状态提交一致性（数值回写）：`_apply_settings` 读取口径（`intValue()`/`value()`/`currentData()`）与各控件实际类型匹配（PrefDragValue 提供 `intValue()`，pref_drag_value.py:47）；backend 嵌套 dict 采用复制后整体 `cfg.set('backend', ...)`（1955-1958），不原位修改快照共享的嵌套对象。
- 状态提交一致性（重启标记）：locale / work_mode / developer_mode 变更均正确比对初值并置 `_needs_restart`（2077-2090）。
- 图像模式守卫：Timeline Defaults 相关 widget 在 load/apply/restore 三处全部带 None 守卫（1863-1879、1993-2020、2231-2247），image 模式下不访问未创建控件。
- 异常路径（破坏性操作确认）：Clear Cache / Reset All Shortcuts / Restore Defaults 均有 QMessageBox 二次确认且默认 No（1479-1487、1494-1501、2109-2118）。
- 取消路径（外观回滚）：未 Apply 直接 Cancel 时 `_restore_preview_snapshot` 能将外观回滚至打开时状态并通知主窗口（341-364）。
- 短路退出：Escape 经 `keyPressEvent` 路由到 `_on_cancel()` 完成回滚而非裸 `reject()`（2281-2286）。
- 页面注册：未知 module page_id 记 warning 日志（189-193）；模块页 factory 由 descriptor `__post_init__` 自动构建（preferences/__init__.py:97-105），`desc.factory(self)` 调用安全；image 模式下 video_playback 页跳过注册且仍纳入 load/apply 委托（161-172）。
- 平台门控：Metal 选项按 Darwin 探测禁用并附原因（669-682），D3D11 按 Windows 门控（683-704），Linux/macOS 无法选中不可用后端。
- 输入边界（路径类）：`_browse_path` 空路径回退空串、选中才写回（1458-1468）；`_open_log_directory` 逐级回退并 `os.makedirs(exist_ok=True)`（1595-1607）。
