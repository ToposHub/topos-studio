# batch-200 审查报告 — src/features/edit/ui//

审查对象：`src/features/edit/ui/edit_properties_panel.py`（4,896 行，单文件批次）。
审查方式：全文通读（Read 分段覆盖 1–4896 行），并对每个疑点在
`src/app/controllers/edit_controller.py`、`src/app/main_window.py`、
`src/features/composite/nodes/base_node.py`、`src/features/edit/core/effect_instance.py`、
`src/features/shared/effects/effect_catalog.py`、`src/shared/ui/widgets/drag_value.py`
中核实了信号接线与调用方（均为只读 grep/read）。

## src/features/edit/ui/edit_properties_panel.py (行数: 4896)

### 发现

- [AUD-20000] [Critical] 效果关键帧 ◆ 按钮永远在 frame 0 写/删关键帧：set_current_frame 全库无任何调用者
  - 位置: src/features/edit/ui/edit_properties_panel.py:838, 2127-2130, 351-352
  - 证据:
    ```python
    self._current_frame: int = 0            # L838，仅 set_current_frame 会更新
    ...
    def set_current_frame(self, frame: int) -> None:   # L2127
        """更新当前帧（驱动关键帧按钮状态）。"""
        self._current_frame = frame
        self._update_keyframe_buttons()
    ...
    def _on_clicked(self) -> None:          # L351
        self.keyframe_toggled.emit(self._param_name, self._current_frame, self._current_value)
    ```
    全库 grep `set_current_frame`：`EditPropertiesPanel.set_current_frame` 无任何调用点
    （main_window.py / edit_controller.py 只调用 composite/keyframes 面板的同名方法）。
    而 `effect_keyframe_toggled` 已在 main_window.py:1743 接到
    `edit_controller._on_edit_effect_keyframe_toggled`，该 handler 直接用传入的
    `frame` 构造 Add/RemoveKeyframeCommand（edit_controller.py:4145/4154/4166）。
  - 影响: 无论播放头在哪一帧，点击 ◆ 都在 frame 0 添加值为 0.0（按钮
    `_current_value` 的构造默认值）的关键帧 → 动画参数在第 0 帧被写成 0（渲染错误）；
    反之若 frame 0 恰好已有关键帧，点击会把它删掉（既有动画数据丢失）。按钮的
    菱形状态（是否有关键帧/已动画化）也因 `_update_keyframe_buttons` 不随播放头刷新而恒为初始灰态。
  - 建议: 在控制器侧于播放头变化和 `set_clip` 后调用 `panel.set_current_frame(frame)`；
    或让 ◆ 按钮回调直接向 controller/时间线查询当前帧与当前参数值，而不是依赖
    从未被喂过数据的按钮缓存。
  - 置信度: 高

- [AUD-20001] [Major] 特效 section 的 Reset 写入“全局选中特效”而非被重置的 section（_selected_effect_id 误用）
  - 位置: src/features/edit/ui/edit_properties_panel.py:1661-1663, 1593-1597, 1688-1696
  - 证据:
    ```python
    def _extract_effect_id_from_section(self, group_section: str) -> Optional[str]:
        """从当前选中的 effect 提取 effect_id。"""
        return self._selected_effect_id          # 完全忽略 section_id 中的 effect_id
    ...
    elif group == "effect":
        # section_id = clip:<clip_id>:effect:<effect_id>
        effect_id = self._extract_effect_id_from_section(group_section="effect")
        if effect_id:
            self.effect_param_changed.emit(clip_id, effect_id, param_name, value)
    ...
    # _on_section_reset 的 else 分支（effect section 也会走到）：
    for key, pdef in section.param_defs.items():
        if pdef.default is not None:
            self._emit_section_param(section, key, pdef.default)
    ```
    `_selected_effect_id` 仅由 `scroll_to_effect`（L3701）与控制器删除特效时置 None
    （edit_controller.py:4339）更新；普通参数编辑控件均绕过此路由直接 emit，唯独
    Reset 走此分支。
  - 影响: 展开“非当前选中”的特效 section（折叠箭头即可展开，不改变选中态）并点
    Reset，会把**选中特效**的全部参数重置为默认（修改错误目标的数据）；当
    `_selected_effect_id` 为 None（如刚删除过任一特效）时 Reset 完全无效。
  - 建议: 在 `_route_clip_param` 的 effect 分支解析 `section_id` 的 `parts[3:]`
    作为 effect_id（该 section_id 自带正确 id），删除 `_extract_effect_id_from_section`
    对全局选中态的依赖。
  - 置信度: 高

- [AUD-20002] [Major] _sync_paired_widget 注释称“同步 slider”但只更新 DragValue，链锁下用滑块拖 scale 时对侧滑块停滞
  - 位置: src/features/edit/ui/edit_properties_panel.py:1665-1676（对照 1259-1266、1421-1438）
  - 证据:
    ```python
    info = section._param_widgets.get(key)
    if info and "widget" in info:
        self._updating = True
        info["widget"].setValue(value)
        # 同步 slider
        self._updating = False
    break
    ```
    当用户拖动 **slider**（而非 DragValue）改 scale_x 时，`on_slider`（L1259-1266）
    对 drag `blockSignals` 后 setValue，因此挂在 `drag_x.valueChanged` 上的
    `sync_peer`（L1437，会同步对侧 drag+slider）不会触发；只有
    `_emit_transform_param → _sync_paired_widget` 这条路径执行，而它从不触碰
    `info["slider"]`。
  - 影响: 锁链开启时用滑块调 scale，对侧轴的 DragValue 数值更新但其滑块 thumb
    停留在旧位置，UI 自相矛盾；之后点该滑块会从旧位置突变。
  - 建议: 在 `_sync_paired_widget` 中仿照 `sync_peer` 同时 blockSignals 更新
    `info.get("slider")`（用 `_value_to_slider` 换算）。
  - 置信度: 高（代码缺失确定；触发路径经信号连接顺序核实）

- [AUD-20003] [Major] _create_effect_xy_row：链锁状态下拖 DragValue 不同步自身 slider（else 分支才同步）
  - 位置: src/features/edit/ui/edit_properties_panel.py:3884-3892, 3908-3915
  - 证据:
    ```python
    def _x_drag_changed(v):
        if chain.isChecked():
            emit_linked(x_key, int(v) if is_int_x else v, v)   # 只推对侧，不更新 slider_x
        else:
            # keep own slider in sync, emit single
            slider_x.blockSignals(True)
            slider_x.setValue(_value_to_slider(v, x_pdef, is_int_x))
            slider_x.blockSignals(False)
            emit_single(x_key, int(v) if is_int_x else v)
    ```
    `emit_linked → push_to_peer` 只同步**对侧**的 drag+slider（L3846-3862），自身
    slider_x/slider_y 在锁链分支中从未 setValue。
  - 影响: 链锁 ON 时用数值框改 radius/size 等 X/Y 参数，本轴滑块 thumb 停在旧值，
    与数值框、对轴滑块显示不一致（Y 轴 handler 同理）。
  - 建议: 在锁链分支中同样 `slider_x.blockSignals(True); slider_x.setValue(...)` 后
    再 emit_linked。
  - 置信度: 高

- [AUD-20004] [Major] Solid Color 数值行重接线：valueChanged.disconnect() 移除了 _build_numeric_row 的拖动→滑块同步闭包
  - 位置: src/features/edit/ui/edit_properties_panel.py:3188-3197（同型：3217-3232、3291-3306、3313-3327）
  - 证据:
    ```python
    info = section._param_widgets.get("checker_size")
    if info and "widget" in info:
        info["widget"].valueChanged.disconnect()      # 把 drag_changed（含滑块同步）一并断开
        info["widget"].valueChanged.connect(
            lambda v: _set_param("checker_size", int(v)))   # 只发参数，不同步滑块
        slider = info.get("slider")
        if slider:
            slider.valueChanged.disconnect()
    ```
    `_build_numeric_row` 原本通过 `drag_changed`（L1251-1257）在 DragValue 变化时
    blockSignals 同步 slider；`disconnect()` 无参断开全部连接后没有补回这条同步。
  - 影响: checker_size / start_(x,y) / end_(x,y) / center_(x,y) / radial_radius /
    feather 各行，直接拖 DragValue 时对应滑块 thumb 停留在旧位置（滑块→数值方向
    仍同步），视觉状态不一致。
  - 建议: 不用裸 `disconnect()`，改为只 connect 一个同时负责“滑块 blockSignals
    setValue + _set_param”的新 handler；或给 `_build_numeric_row` 增加自定义
    on_change 回调参数，避免断开全部连接。
  - 置信度: 高

- [AUD-20005] [Major] 字幕 Family 可编辑下拉同时挂 currentIndexChanged 与 editTextChanged：下拉选择一次提交两条命令、手输每键一条
  - 位置: src/features/edit/ui/edit_properties_panel.py:2385-2392
  - 证据:
    ```python
    def _on_family_changed(_idx: int):
        if self._updating:
            return
        self._emit_section_param(section, "font_family", family_combo.currentText())
    family_combo.currentIndexChanged.connect(_on_family_changed)
    family_combo.editTextChanged.connect(
        lambda _t: _on_family_changed(-1) if not self._updating else None
    )
    ```
    family_combo 是 `setEditable(True)`（L2363）；Qt 中可编辑组合框从弹出列表选择
    一项会**同时**发出 currentIndexChanged 与 editTextChanged。控制器侧
    `on_subtitle_param_changed`（edit_controller.py:9027-9057）每次发射构造并执行
    一条 UpdateSubtitleClipCommand，无合并。
  - 影响: 从下拉选择一次字体 = 两条相同命令 = 两次撤销项；在编辑框手输字体名则
    每个字符一条命令，撤销栈被逐字符污染（同文件 Text 字段已专门做了 250ms
    debounce，此处漏配）。
  - 建议: 只保留一个信号入口（用 currentTextChanged 或仅 editTextChanged 加去重），
    并为手输路径引入与 text 相同的 debounce/focus-out 合并提交。
  - 置信度: 高（双发机制为 Qt 标准行为；控制器一信号一命令已核实）

- [AUD-20006] [Major] 轨道名 QLineEdit 用 textChanged 逐字符提交 RenameTrackCommand（无 debounce、无 _updating 守卫）
  - 位置: src/features/edit/ui/edit_properties_panel.py:4505-4508（对照控制器 edit_controller.py:2221-2230）
  - 证据:
    ```python
    name_edit = QLineEdit(track.name)
    name_edit.textChanged.connect(
        lambda text, tid=track.track_id: self.track_param_changed.emit(tid, "name", text)
    )
    ```
    控制器对每次 `track_param_changed("name", …)` 构造一条 `RenameTrackCommand`
    并 `tview._execute_command(cmd)`（edit_controller.py:2221-2230），一信号一命令。
  - 影响: 重命名轨道时每个键入字符产生一条命令：撤销栈充满中间态名字（“T”→“Tr”→
    “Tra”…），且每键都走完整命令链（增量时间线更新+自动保存标脏）。
  - 建议: 改用 editingFinished / debounce 提交一次最终名；lambda 内补 `self._updating`
    守卫以符合程序化更新须状态锁的铁律。
  - 置信度: 高

- [AUD-20007] [Major] Solid Color section 的 Reset：信号被 _route_clip_param 静默丢弃，但控件显示被归位 → UI 与数据脱节且无撤销
  - 位置: src/features/edit/ui/edit_properties_panel.py:1688-1696, 1538-1597
  - 证据:
    ```python
    # _on_section_reset else 分支（solid_color 组落入此处）：
    section = self._sections.get(section_id)
    if section:
        for key, pdef in section.param_defs.items():
            if pdef.default is not None:
                self._emit_section_param(section, key, pdef.default)
        self._reset_section_widgets(section)      # 控件显示被重置为默认
    ```
    而 `_route_clip_param` 的分支仅有 transform/crop/raw/opacity/speed/effect，
    没有 "solid_color" → `_on_section_param_changed` 里 kind=="clip"、group==
    "solid_color" 进入 `_route_clip_param` 后落到函数尾部，什么都不发。
  - 影响: 点击 Solid Color section 的 Reset 按钮：界面数值显示回到默认值，但
    `generator_params` 实际未变、无命令、无撤销 —— 面板显示与真实数据从此分叉，
    直到下次重建。
  - 建议: 在 `_on_section_reset` 为 solid_color 增加专门分支（按当前 mode 回退
    默认参数并经 `generator_params_changed` 一次提交），或在不支持的分组上隐藏
    Reset 按钮并跳过 `_reset_section_widgets`。
  - 置信度: 高

- [AUD-20008] [Major] Transition / Subtitle section 的 Reset 按钮完全静默无效（_on_section_reset 未处理这两种 kind）
  - 位置: src/features/edit/ui/edit_properties_panel.py:1678-1706（按钮无条件创建于 561-568）
  - 证据:
    ```python
    parts = section_id.split(":")
    kind = parts[0] if parts else ""
    if kind == "clip" and len(parts) >= 3:
        ...
    elif kind == "track" and len(parts) >= 3:
        ...
    # 没有 kind == "transition" / "subtitle" 分支，函数静默返回
    ```
    而 `EditSectionWidget._build_header` 对每个 section（含 transition:… 与
    subtitle:…:text 等）都无条件添加 Reset 按钮（L561-568）。
  - 影响: 转场与字幕各 section 上可见的 Reset 按钮点击后无任何效果，误导用户
    （可编辑参数明确存在却无法重置）。
  - 建议: 增加 transition（duration/alignment/curve/direction/softness 回默认）
    与 subtitle（按 STYLE_FIELDS 回默认）的重置分支；只读 section（asset/timeline）
    隐藏 Reset 按钮。
  - 置信度: 高

- [AUD-20009] [Suggestion] Reset 逐参数各发一条命令：crop/opacity/speed/raw 一次 Reset 产生多条撤销项，与 transform 单命令不一致
  - 位置: src/features/edit/ui/edit_properties_panel.py:1689-1696
  - 证据:
    ```python
    for key, pdef in section.param_defs.items():
        if pdef.default is not None:
            self._emit_section_param(section, key, pdef.default)
    ```
    raw 组有 8 个参数 → 8 次 `clip_param_changed` → 控制器 8 条
    UpdateClipParamCommand；transform 则一次 `reset_transform_requested` 批量重置。
  - 影响: 一次 Reset 需要 N 次 Ctrl+Z 才能完全撤销，撤销粒度不一致。
  - 建议: 仿照 `effect_params_changed`/`subtitle_params_changed` 增加批量 reset 信号，
    由控制器合并为一条 MacroCommand。
  - 置信度: 高

- [AUD-20010] [Suggestion] 宽泛 except 吞错无日志：_refresh_effect_status_badge `except Exception: pass`（另 QFontDatabase 分支同型）
  - 位置: src/features/edit/ui/edit_properties_panel.py:706-709（另 2364-2367）
  - 证据:
    ```python
    try:
        from src.features.shared.effects import (...)
        ...
        self._title_label.setText(f"{self.title}{tag}")
    except Exception:  # noqa: BLE001 — badge must never break the panel
        pass
    ```
  - 影响: 违反“不得宽泛 except 掩盖领域错误”铁律；catalog/能力解析的真实错误
    （如拼写、导入错误）被完全吞掉，badge 静默缺失且无从排查。
  - 建议: 至少 `logger.debug(..., exc_info=True)`（同文件 L3643-3644 的 plugin
    status 分支即为此写法），必要时收窄异常类型。
  - 置信度: 高

- [AUD-20011] [Suggestion] 效果参数控件回调缺少 _updating 防递归守卫（铁律要求程序化更新须状态锁）
  - 位置: src/features/edit/ui/edit_properties_panel.py:4056-4083, 4210-4236, 4423-4436, 4463-4477
  - 证据:
    ```python
    def _slider_changed(v, pn=pdef.name, eid=effect_id, d=drag, inf={...}):
        d.blockSignals(True)
        fv = v if inf["type"] == "int" else v * inf["step"] + inf["min_val"]
        d.setValue(fv)
        d.blockSignals(False)
        if clip:
            self.effect_param_changed.emit(          # 无 if self._updating 检查
    ```
    同文件 `_build_numeric_row` 的同类回调（L1251-1266）均以 `if self._updating:
    return` 开头；effect/transition 行回调全部没有。
  - 影响: 当前 effect/transition 控件未注册进 `section._param_widgets`、无程序化
    setValue 路径，故暂未实际触发；但一旦未来加入刷新逻辑（如 undo 后刷新），
    程序化 setValue 将直接产生伪命令。
  - 建议: 为这些回调统一补 `if self._updating: return` 前置守卫。
  - 置信度: 高（缺失事实）；低（当前可触发性）

- [AUD-20012] [Suggestion] effect_id 解析不一致：drop 处理兼容含冒号 id（join parts[3:]），enable/delete/open 只取 parts[3]
  - 位置: src/features/edit/ui/edit_properties_panel.py:1071-1077 对照 3653-3657、3663-3667、3678-3682
  - 证据:
    ```python
    # _handle_effect_drop：
    parts = raw.split(":")
    ...
    effect_id = ":".join(parts[3:])  # effect_id may contain colons (uuid)
    ...
    # _on_effect_enable_toggled（delete/open 同型）：
    parts = section_id.split(":")
    if len(parts) < 4:
        return
    clip_id = parts[1]
    effect_id = parts[3]             # 截断，不合并余下分段
    ```
  - 影响: 当前 `EffectInstance.effect_id = str(uuid.uuid4())`（effect_instance.py:271）
    不含冒号，故无实际触发；但一旦 id 格式变化（drop 分支注释明确预期可能含冒号），
    enable/delete/open 会指向被截断的错误 id。
  - 建议: 三处统一改用 `":".join(parts[3:])`，或抽出一个公共解析函数。
  - 置信度: 高（不一致确定）；低（当前触发可能）

- [AUD-20013] [Suggestion] 模块级构建函数与若干方法缺类型注解
  - 位置: src/features/edit/ui/edit_properties_panel.py:2224-2226（`clip` 无注解）、2488-2491（`section, clip` 无注解）、3059（`_make_color_button(label_text, r_key, ...)` 无注解）、1042（`eventFilter(self, obj, event)`）、390（`__init__(..., parent=None)` 无注解）等
  - 证据:
    ```python
    def _build_subtitle_info_section(self: EditPropertiesPanel, clip) -> None:
        """字幕片段元信息（只读）。"""
    ...
    def _build_subtitle_color_button(self: EditPropertiesPanel, section,
                                     clip, group: str,
                                     r_key: str, g_key: str, b_key: str, a_key: str,
                                     label: str) -> QWidget:
    ```
  - 影响: 违反项目强类型约束；SubtitleClip/ParamDef 等类型信息缺失降低可维护性与
    IDE 检查能力（项目其余部分注解覆盖率很高）。
  - 建议: 补齐 `clip: SubtitleClip`、`section: EditSectionWidget`、
    `obj: QObject, event: QEvent`、`parent: Optional[QWidget]` 等注解。
  - 置信度: 高

- [AUD-20014] [Suggestion] raw 参数重建 `except ValueError: return` 静默吞错，无日志无用户反馈
  - 位置: src/features/edit/ui/edit_properties_panel.py:1573-1585
  - 证据:
    ```python
    try:
        raw = RawParams(
            wb_gain_r=float(values["wb_gain_r"]),
            ...
        )
    except ValueError:
        return
    ```
  - 影响: 若模型值异常（如序列化损坏的字符串），RAW 参数编辑静默失效，用户无
    任何提示，排查困难。
  - 建议: except 分支加 `logger.warning(...)` 并可考虑状态栏提示。
  - 置信度: 高

- [AUD-20015] [Suggestion] 字幕文本 debounce：_clear_sections 停止并删除定时器但不 flush 未提交文本，特定路径丢失最后 <250ms 输入
  - 位置: src/features/edit/ui/edit_properties_panel.py:975-980（对照 2306-2337）
  - 证据:
    ```python
    subtitle_timer = getattr(self, "_subtitle_text_timer", None)
    if subtitle_timer is not None:
        subtitle_timer.stop()          # 未先提交 _subtitle_text_pending
        subtitle_timer.deleteLater()
        self._subtitle_text_timer = None
    self._subtitle_text_edit = None
    ```
  - 影响: 焦点仍在文本框内时选择被程序化切换（快捷键换选等不触发 focusOut 的
    路径），最后一次 250ms 窗口内的输入被静默丢弃；`_commit_text` 在 `_updating`
    为真时也直接丢弃而非延后。
  - 建议: `_clear_sections` 停止定时器前，若 `_subtitle_text_pending != last_committed`
    先同步 `_commit_text()`（或经由 panel 层安全 flush）。
  - 置信度: 中

- [AUD-20016] [Suggestion] 每次重建字幕 Font section 都全量枚举 QFontDatabase.families()
  - 位置: src/features/edit/ui/edit_properties_panel.py:2363-2370
  - 证据:
    ```python
    try:
        families = list(QFontDatabase.families())
    except Exception:
        families = ["Arial", "Helvetica", "Times New Roman", "Courier New"]
    if clip.font_family not in families:
        families.insert(0, clip.font_family)
    family_combo.addItems(families)
    ```
  - 影响: 本面板为“销毁重建”架构（每次选中字幕片段都重建全部 section），在字体
    较多的系统上每次选择都重新枚举并填充数百项 combo，选择切换有可感知开销。
  - 建议: 模块级缓存 families 列表（首次枚举后复用）。
  - 置信度: 中

### 通过项

- 线程安全：纯 QWidget 代码，无 OpenGL/渲染对象触碰（铁律“UI 线程不得操作 OpenGL”满足）；与渲染侧仅经信号解耦，无跨线程直调。
- GPU 资源生命周期：文件不创建/持有任何 ModernGL 资源（无 GPU 泄漏面）。
- HDR 正确性：字幕 RGBA（0..65535）、RAW black_level（0..65535）等参数行保留 HDR 值域，数据层无不当 clamp(0,1)；色块 swatch 的 0..255 截断仅作用于显示预览，不回写数据。
- 状态锁主路径：set_clip/set_clips/set_transition/set_track/set_asset/set_timeline/set_subtitle_clip/clear 全程 `_updating=True` 包裹构建；`_refresh_section_values`、`update_subtitle_transform_values` 以 try/finally 正确持锁/恢复锁（含保存/恢复先前锁状态）；`_on_blend_mode_changed`/`_on_interp_changed` 用 try/finally 恢复 `_updating`。
- _clear_sections 的 macOS 原生 combo popup 悬空指针防护（hidePopup + 保留 parent 至 deleteLater）与字幕 debounce 定时器清理意图正确（flush 缺失已单列 AUD-20015）。
- 数据新鲜度：transform/crop/speed/raw 从 `self._current_clips[0]` 模型重建 dataclass（raw 的 2026-09-21 修复在位），控制器在每次 clip_param_changed 后经 `update_current_clip` 回写新 clip（edit_controller.py:2085-2089、2143-2147），连续编辑不会回退旧值。
- 音量/声像三段式节流实现正确：拖动中只发 `track_param_preview`（纯视觉），松手 `track_param_changed` 一次提交，一次拖动=一次撤销。
- 拖拽排序（FX-H04-UI）：MIME 解析有格式/前缀校验；drop 插入索引做了“下移减一”修正与 no-op 判定；`drag.exec` 从 handle 的 eventFilter 启动避免 QToolButton 吞事件。
- 输入验证：section_id 解析各处均有 parts 长度校验；`set_subtitle_clip` 对 resolved_style 经 hasattr 过滤；combo 回调对越界 idx 有 `0 <= idx < len(opts)` 保护；`update_subtitle_transform_values` 校验 clip_id 匹配并防御性 getattr。
- `_build_effect_sections`：catalog→registry→plugin 三级回退路径完整，legacy dict 效果与缺插件效果均可见不静默消失；plugin 状态解析失败有 logger.debug。
- 折叠状态记忆按类型 key（不含 clip_id/track_id）隔离，effect 按 effect_type 共享，解析失败返回 None 不记忆。
- keyframe 按钮的 effect 复合键路由（"effect:<eid>:<param>"）与 `_update_keyframe_buttons` 的分支查询逻辑本身正确（frame 不更新已单列 AUD-20000）。
- 字幕 RGBA 拖动 session（before/pending/interaction_ended 合并提交）与颜色选择器单 patch 提交实现正确，保证一次交互一条撤销。
- `_rebuild_solid_color_section` 的时序依赖成立：控制器在 `_on_edit_generator_params_changed` 中同步回写 `panel._current_generator_params`（edit_controller.py:3790），重建读到的是新参数（仅存在双重重建的小额冗余）。
