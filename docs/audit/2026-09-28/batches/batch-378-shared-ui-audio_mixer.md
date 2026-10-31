# 批次报告：batch-378 [P2] src/shared/ui/audio_mixer/

- 审查日期：2026-09-28
- 文件数：8（2,283 行），全部 Read 通读
- 编号区间：AUD-37800–AUD-37821
- 汇总：Critical 3 / Major 5 / Suggestion 14
- 关键结论：RoutingDialog 存在两处 Critical（打开对话框即清空该轨全部 sends；Send 空 target 构造抛 ValueError 未捕获可致崩溃）；PresetDialog 的 Apply 按钮存在 clicked(bool) 误传槽参数的崩溃路径。本批无 GPU/GLSL/HDR 相关代码，铁律冲突集中在信号防递归、输入验证与状态提交一致性。
- 依赖核验：ToggleSwitch.setChecked 为自定义实现（不 emit toggled，toggle_switch.py:43-46）；DragValue.setValue 受自定义 blockSignals 门控（drag_value.py:190-197）；Send.__post_init__ 对空 target_bus_id 抛 ValueError（timeline_v2.py:1602-1604）；消费端 edit_controller._on_mixer_routing_committed 以 diff 语义执行 RemoveSendCommand（edit_controller.py:3381-3428）。

## src/shared/ui/audio_mixer/mixer_separator.py (行数: 31)

### 发现
- [AUD-37800] [Suggestion] 公共方法参数缺类型注解（parent/event）
  - 位置: src/shared/ui/audio_mixer/mixer_separator.py:15,26
  - 证据:
    ```python
    def __init__(self, parent=None):
    ```
    ```python
    def paintEvent(self, event) -> None:
    ```
  - 影响: 违反项目强类型约束；paintEvent 的 event 应为 QPaintEvent，parent 应为 Optional[QWidget]。
  - 建议: 补齐 `parent: Optional[QWidget] = None` 与 `event: QPaintEvent` 注解。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程 QWidget 绘制，无跨线程访问
- GPU 资源生命周期：无 GPU/ModernGL 资源（N/A）
- GLSL·HDR 正确性：无着色器/HDR 数据（N/A）
- 信号防递归：无信号连接
- 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式：无逻辑分支，单一 drawRect，无问题

## src/shared/ui/audio_mixer/noise_gate_dialog.py (行数: 265)

### 发现
- [AUD-37801] [Suggestion] QDial 滚轮/键盘调整绕过三段式节流，每个步进发一次 committed
  - 位置: src/shared/ui/audio_mixer/noise_gate_dialog.py:174-177,202-205
  - 证据:
    ```python
    dial.sliderPressed.connect(lambda: self._dragging.update(active=True))
    dial.sliderReleased.connect(self._on_drag_released)
    spin.interaction_started.connect(lambda: self._dragging.update(active=True))
    spin.interaction_ended.connect(self._on_drag_released)
    ```
    ```python
    if self._dragging["active"]:
        self.noise_gate_preview.emit(self._track_id, self._current)
    else:
        self.noise_gate_committed.emit(self._track_id, self._current)
    ```
  - 影响: QDial 的 wheel/方向键事件发 valueChanged 但不触发 sliderPressed/Released（DragValue 侧已自行包了 started/ended，QDial 侧没有），滚轮每格都走 committed 分支进命令栈，undo 栈被逐步进值膨胀。
  - 建议: 对 QDial 补 wheel/key 交互的 started/ended 包裹（或由接收端对连续 committed 做合并）。
  - 置信度: 中（未逐行追命令层是否合并；机制本身确认存在）

- [AUD-37802] [Suggestion] 用动态私有属性在 QWidget 上传递控件状态，类型不安全
  - 位置: src/shared/ui/audio_mixer/noise_gate_dialog.py:127-130,182-186,192
  - 证据:
    ```python
    container._drag = spin
    container._dial = dial
    container._minimum = minimum
    container._step = step
    return container
    ```
    ```python
    threshold_db=float(self._thresh_w._drag.value()),
    ```
  - 影响: `_make_knob_spin` 无返回类型注解，返回的 QWidget 挂载非声明属性；`_collect_params` 跨对象访问 `._drag` 私有成员，IDE/类型检查不可见，重构易碎。
  - 建议: 封装为明确的 KnbSpin 复合控件类（持有 dial/spin/min/step 并暴露 value()/set_value()）。
  - 置信度: 高

### 通过项
- 线程安全：全部 UI 线程，无 OpenGL/跨线程调用
- GPU 资源生命周期 / GLSL·HDR 正确性：无 GPU 代码（N/A）
- 信号防递归：dial↔spin 联动双向均 blockSignals；`_load_params_to_ui` 对 spin/dial 加锁；preset 用 activated 且复位到索引 0 不触发回调；ToggleSwitch.setChecked 为自定义不发射实现，回填安全（已核验 toggle_switch.py:43-46）
- 状态提交一致性：preview/committed 分离清晰，preset/reset/开关均即时 committed 且语义正确（保留 enabled 状态）
- 输入验证：参数范围（-60..0/0..50/10..1000）与 NoiseGateParams.__post_init__ 校验一致（timeline_v2.py:2025-2031）
- 异常与取消路径：from_dict 构造参数均来自受控件 clamp 的值，无未捕获异常路径

## src/shared/ui/audio_mixer/preset_dialog.py (行数: 224)

（文件头标注 DORMANT(2026-08-19)：已实现有测试但暂无生产入口）

### 发现
- [AUD-37803] [Critical] Apply 按钮把 clicked(bool) 的 False 当 QListWidgetItem 传入槽，必崩 AttributeError
  - 位置: src/shared/ui/audio_mixer/preset_dialog.py:98,147-153
  - 证据:
    ```python
    self._apply_btn.clicked.connect(self._on_apply_clicked)
    ```
    ```python
    def _on_apply_clicked(self, item: Optional[QListWidgetItem] = None) -> None:
        """应用选中的预设到目标轨道。"""
        if item is None:
            item = self._list.currentItem()
        if item is None:
            return
        preset = item.data(Qt.ItemDataRole.UserRole)
    ```
  - 影响: QPushButton.clicked 携带 1 个 bool 参数（checked=False），PyQt6 会把它传给可接受 1 参的槽 → `item=False`；`False is None` 为假，跳过 currentItem() 回退，直接 `False.data(...)` 抛 `AttributeError: 'bool' object has no attribute 'data'`，未捕获异常在 Qt 槽中默认导致进程 abort。双击列表项路径（itemDoubleClicked 传真实 item）正常，仅按钮路径崩溃。当前模块 DORMANT 无生产入口，接线后立即触发。
  - 建议: 按钮用 `lambda: self._on_apply_clicked()` 或拆成无参槽；双击连接保留原方法。
  - 置信度: 高

- [AUD-37804] [Major] 保存预设丢弃 AM-13 补齐的 effect_chain/output_bus_id/sends/is_bus 字段
  - 位置: src/shared/ui/audio_mixer/preset_dialog.py:134-142
  - 证据:
    ```python
    preset = ChannelStripPreset(
        name=name,
        volume=self._current_source.volume,
        pan=self._current_source.pan,
        is_muted=self._current_source.is_muted,
        is_solo=self._current_source.is_solo,
        strip_eq=self._current_source.strip_eq,
        created_at=self._current_source.created_at,
    )
    ```
  - 影响: ChannelStripPreset 自 AM-13 起含 effect_chain/output_bus_id/sends/is_bus（mixer_preset.py:56-59，docstring 称"包含一条 strip 的全部可调参数"），此处未拷贝 → 保存的预设丢失整条 FX 链与路由，应用到其他轨道会把这些重置为默认（无效果、直送 Master、无 sends）；另 created_at 复用源值而非当前时间。
  - 建议: 补拷贝 effect_chain/output_bus_id/sends/is_bus；created_at 用当前时间戳。
  - 置信度: 高

- [AUD-37805] [Suggestion] UI 线程同步文件 IO，且保存失败静默
  - 位置: src/shared/ui/audio_mixer/preset_dialog.py:112-119,143-145
  - 证据:
    ```python
    self._list.clear()
    presets = load_presets()
    ```
    ```python
    add_preset(preset)
    self._name_edit.clear()
    self._refresh_list()
    ```
  - 影响: load_presets/save_presets 为同步磁盘 JSON 读写（mixer_preset.py:162-185），每次 add/remove 各做一次读+写且在 UI 线程；save_presets 失败返回 False 被忽略，用户无感知写入失败。
  - 建议: 磁盘 IO 移出 UI 线程或缓存；对保存失败弹提示。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程
- GPU 资源生命周期 / GLSL·HDR 正确性：无 GPU 代码（N/A）
- 信号防递归：列表刷新 clear/addItem 不触发已连接的 itemDoubleClicked；删除有确认对话框
- 异常与取消路径：空选择/空名称均有守卫与提示；corrupt JSON 由 load_presets 兜底返回空表
- 输入验证：preset_id 由 dataclass 生成；current_source 为 None 时保存被拦截
- SnapshotBar（W14）：按钮均 lambda 零参连接，信号语义清晰，无问题

## src/shared/ui/audio_mixer/reverb_dialog.py (行数: 272)

### 发现
- [AUD-37806] [Suggestion] QDial 滚轮/键盘调整绕过三段式节流（同 AUD-37801）
  - 位置: src/shared/ui/audio_mixer/reverb_dialog.py:179-182,208-211
  - 证据:
    ```python
    dial.sliderPressed.connect(lambda: self._dragging.update(active=True))
    dial.sliderReleased.connect(self._on_drag_released)
    ```
    ```python
    if self._dragging["active"]:
        self.reverb_preview.emit(self._track_id, self._current)
    else:
        self.reverb_committed.emit(self._track_id, self._current)
    ```
  - 影响: 与 AUD-37801 相同机制：QDial wheel/键盘每步进一次 committed，命令栈膨胀。
  - 建议: 同 AUD-37801。
  - 置信度: 中

- [AUD-37807] [Suggestion] 动态私有属性模式 + 与 noise_gate_dialog 大段复制
  - 位置: src/shared/ui/audio_mixer/reverb_dialog.py:132-135,187-191
  - 证据:
    ```python
    container._drag = spin
    container._dial = dial
    container._minimum = minimum
    container._step = step
    return container
    ```
  - 影响: `_make_knob_spin`/节流/回填逻辑与 noise_gate_dialog 逐行同构（约 200 行复制），类型不安全且双份维护。
  - 建议: 抽取共享的 FX 参数旋钮对话框基类/复合控件（本文件与 noise_gate、saturation 三处共用）。
  - 置信度: 高

### 通过项
- 线程安全：全部 UI 线程
- GPU 资源生命周期 / GLSL·HDR 正确性：无 GPU 代码（N/A）
- 信号防递归：dial↔spin 双向 blockSignals；`_load_params_to_ui` 加锁回填；preset 用 activated；ToggleSwitch 回填不发射（已核验）
- 状态提交一致性：preview/committed 三段式语义与 noise_gate 一致；preset/reset 保留 enabled
- 输入验证：四参数范围与 ReverbParams.__post_init__ 一致（timeline_v2.py:1838-1846）
- 异常与取消路径：无未捕获异常路径

## src/shared/ui/audio_mixer/routing_dialog.py (行数: 349)

### 发现
- [AUD-37808] [Critical] 打开对话框即触发 committed 且此时 sends 列表为空，消费端按替换语义删除该轨全部 Send
  - 位置: src/shared/ui/audio_mixer/routing_dialog.py:212,261-270,303-310
  - 证据:
    ```python
    self._output_combo.currentIndexChanged.connect(self._on_committed)
    ```
    ```python
    for i in range(self._output_combo.count()):
        if self._output_combo.itemData(i) == self._output_bus_id:
            self._output_combo.setCurrentIndex(i)
            break
    # sends
    for send in self._initial_sends:
        self._add_send_slot(send)
    ```
    ```python
    sends = []
    for slot in self._send_slots:
        collected = slot.collect()
    ```
  - 影响: `currentIndexChanged` 在 `_init_ui` 内已连接，而 `_load_initial_state` 先 setCurrentIndex（当 output_bus_id 指向某 Bus，即索引≥1 时触发）后添加 send slots → `_on_committed` 在 `_send_slots` 仍为空时 emit `routing_committed(track_id, {output_bus_id, sends: []})`。消费端 edit_controller._on_mixer_routing_committed 以 diff 替换语义处理：实时 `mgr.set_track_routing(track_id, bus, [])`（edit_controller.py:3397），并对所有旧 send 逐条 `RemoveSendCommand.create`（edit_controller.py:3424-3428）。即：对"已路由到 Bus 且有 sends"的轨道，仅仅是打开 Routing 对话框就会清空其全部发送（数据丢失 + 可 undo 但用户无感知）。output_bus_id 为 None（Master）时索引不变、无触发。
  - 建议: `_load_initial_state` 中设置 `_output_combo` 前 blockSignals（或加 `_loading` 状态锁），初始装载不发提交；并在装载完成前禁止 `_on_committed` 生效。
  - 置信度: 高（已对照消费端 diff/删除逻辑）

- [AUD-37809] [Critical] Send 空 target_bus_id 构造抛 ValueError 未捕获，多条用户路径触发崩溃
  - 位置: src/shared/ui/audio_mixer/routing_dialog.py:78,136-141,283-287；src/features/edit/core/timeline_v2.py:1602-1604
  - 证据:
    ```python
    self._target_combo.addItem("(选择 Bus)", "")
    ```
    ```python
    target = self._target_combo.currentData() or ""
    return Send(
        send_id=self._send_id,
        target_bus_id=target,
    ```
    ```python
    default_target = self._bus_tracks[0][0] if self._bus_tracks else ""
    new_send = Send(target_bus_id=default_target, level=1.0, enabled=True)
    ```
    （Send.__post_init__: `if not self.target_bus_id: raise ValueError("target_bus_id cannot be empty")`）
  - 影响: 三条路径都会在 Qt 槽内抛未捕获 ValueError，PyQt6 默认 abort 进程：(1) 项目无任何 Bus 轨时点"+ 添加发送"；(2) 用户在下拉中选择"(选择 Bus)"占位项后任一字段变化触发 collect；(3) 既有 send 的目标 Bus 已被删除（选项无匹配，combo 停在占位项）后任何编辑。另：被删除的槽位不回退、无提示。
  - 建议: collect() 对空 target 返回 None（连同 `_removed` 一并跳过）并显示告警；`_on_add_send` 在 `bus_tracks` 为空时禁用按钮或提示。
  - 置信度: 高

- [AUD-37810] [Major] 环检测告警结果被丢弃，校验失败仍提交；且图简化使 Bus-Bus 环实际不可检测
  - 位置: src/shared/ui/audio_mixer/routing_dialog.py:290-297,327-335
  - 证据:
    ```python
    def _on_committed(self) -> None:
        """收集当前状态，校验，发信号。"""
        payload = self._collect()
        # 实时环检测告警
        self._validate_and_warn(payload)
        # 提交（离散操作：output change, add/remove send, enable toggle）
        self.routing_committed.emit(self._track_id, payload)
    ```
    ```python
    routing = {bid: {"output_bus_id": None, "sends": [], "is_bus": True} for bid in bus_ids}
    ```
  - 影响: 模块 docstring 承诺"创建会导致引擎崩溃的环前阻止提交"，但 `_validate_and_warn` 的返回值被忽略，无效 payload 照样 emit；且其余 Bus 的 output 全部置 None（代码注释自认"简化"），validate_routing_graph 的 topological 环检测（audio_playback.py:476）对这些全 None 节点永远无环——本对话框实际只能查出未知目标，Bus→Bus 环完全漏检。
  - 建议: `is_valid` 为 False 时不 emit（或 emit 前弹确认）；`_validate_and_warn` 传入全部轨道的真实路由快照而非空图。
  - 置信度: 高

- [AUD-37811] [Major] routing_preview 信号声明但从未发射，拖动 send level 期间无任何实时反馈
  - 位置: src/shared/ui/audio_mixer/routing_dialog.py:153,112-121
  - 证据:
    ```python
    routing_preview = pyqtSignal(str, object)   # track_id, payload dict
    ```
    ```python
    def _on_changed(self) -> None:
        if not self._dragging:
            self.changed.emit()
    ```
  - 影响: 全文件仅有 `routing_committed.emit`（296、348 行），`routing_preview` 无 emit 点、也无外部消费者（全库 grep 确认）。拖动 level 时 `_dragging=True` 抑制 changed，直到松手才提交——与 docstring"拖动期实时预览（不进命令栈）"不符，用户拖动发送电平听不到变化。
  - 建议: `_on_drag_ended` 前的拖动期构造 payload 发 routing_preview（sends 收集需容忍进行中的值），消费端接实时听感接口。
  - 置信度: 高

- [AUD-37812] [Major] 删除 Send 后槽位控件不移除不隐藏，UI 与模型状态不一致
  - 位置: src/shared/ui/audio_mixer/routing_dialog.py:123-129,272-278
  - 证据:
    ```python
    def _on_remove(self) -> None:
        self._removed = True
        self.changed.emit()
    ```
  - 影响: 点击 ✕ 后仅置标记，控件仍留在滚动区显示原值；`collect()` 静默跳过它。用户看到的发送列表与将被提交的列表不一致，也无法在对话框内撤销误删，只能关闭重开。
  - 建议: `_on_remove` 中 `self.setParent(None)` + `deleteLater()`（父对话框同步从 `_send_slots` 移除），或至少 hide() 并给出"已删除"视觉态。
  - 置信度: 高

- [AUD-37813] [Suggestion] 杂项：closeEvent 冗余提交、未用导入、死属性、函数内 import
  - 位置: src/shared/ui/audio_mixer/routing_dialog.py:29,169,345-349,70/91/135/284/322
  - 证据:
    ```python
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """关闭时发最终提交。"""
        payload = self._collect()
        self.routing_committed.emit(self._track_id, payload)
    ```
    （另：`from PyQt6.QtCore import Qt` 全文件无 `Qt.` 使用；`self._dragging = False` 赋值后从未读取）
  - 影响: 未变更关闭也会发一次 committed（消费端 diff 下暂无实际命令，但实时路由被无谓重设一次）；死代码与散落的函数内 import 增加维护噪音。closeEvent 的 event 参数亦缺注解。
  - 建议: 删除未用导入与死属性；closeEvent 仅在存在未提交变更时 emit。
  - 置信度: 高

### 通过项
- 线程安全：全部 UI 线程，无 OpenGL 调用
- GPU 资源生命周期 / GLSL·HDR 正确性：无 GPU 代码（N/A）
- 信号防递归（除 AUD-37808 所列初始化时序外）：`_SendSlotWidget` 构造中 setChecked/setCurrentIndex 均在 connect 之前完成，无回环
- 输入验证（level）：DragValue min_v=0.0/max_v=1.0 与 Send.level 约束一致

## src/shared/ui/audio_mixer/routing_matrix_dialog.py (行数: 279)

（文件头标注 DORMANT(2026-08-19)：已实现有测试但暂无生产入口）

### 发现
- [AUD-37814] [Major] Spill 交互未接线：无表头点击连接，spill()/spill_requested 无法由用户触发
  - 位置: src/shared/ui/audio_mixer/routing_matrix_dialog.py:126,213-214,237-240
  - 证据:
    ```python
    self._table.cellClicked.connect(self._on_cell_clicked)
    ```
    ```python
    if row < 0 or row >= len(self._source_tracks):
        # 列头点击（row < 0 在 QTableWidget 不触发，用 header）
        return
    ```
    ```python
    def spill(self, bus_id: str) -> List[str]:
        """Spill：返回路由到指定 Bus 的所有上游源轨 track_id。
    ```
  - 影响: 注释自认需要用 header，但全文件没有 `horizontalHeader().sectionClicked` 连接（grep 确认无 sectionClicked/headerClicked），`spill()` 与 `spill_requested` 信号在整个 src 下无任何调用/消费者（grep 确认）——模块宣称的 Spill 功能（docstring §2）是死路径，`_spilled_bus`/`_spill_label` 的更新逻辑永远不执行。
  - 建议: 接线 `self._table.horizontalHeader().sectionClicked.connect(lambda c: self.spill(self._bus_tracks[c-1][0]) if c > 0 else None)`，或在 DORMANT 期间删除该路径。
  - 置信度: 高（以当前代码为准；模块休眠中）

- [AUD-37815] [Suggestion] routing_changed 直接外泄内部可变 dict，get_routing 仅浅拷贝
  - 位置: src/shared/ui/audio_mixer/routing_matrix_dialog.py:231-235,277-279
  - 证据:
    ```python
        r["output_bus_id"] = new_target
        self._routing[track_id] = r

        self._refresh_matrix()
        self.routing_changed.emit(self._routing)
    ```
    ```python
    def get_routing(self) -> Dict[str, Dict[str, Any]]:
        """返回当前路由快照。"""
        return dict(self._routing)
    ```
  - 影响: 信号把 `self._routing` 本体交给接收方，内层 dict 也共享（浅拷贝）——接收方一旦修改即污染对话框内部状态，后续 `_refresh_matrix`/`get_routing` 基于被污染数据。
  - 建议: emit 与 get_routing 返回深拷贝（copy.deepcopy 或逐项重建）。
  - 置信度: 高

- [AUD-37816] [Suggestion] 外部 routing/sends 结构未验证；存在未用导入
  - 位置: src/shared/ui/audio_mixer/routing_matrix_dialog.py:27-33,178-181,249
  - 证据:
    ```python
    QFrame,
    QGridLayout,
    QScrollArea,
    ```
    ```python
    for s_target, _level in sends:
        if s_target == bus_id:
    ```
  - 影响: 构造参数 routing 是外部数据，`sends` 被无条件按二元组解包——若传入 Send 对象列表（timeline 中的原生形态）会 TypeError；QFrame/QGridLayout/QScrollArea 导入后未使用。
  - 建议: 构造时规范化 sends（接受 tuple 或 Send，统一为 tuple）；清理未用导入。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程
- GPU 资源生命周期 / GLSL·HDR 正确性：无 GPU 代码（N/A）
- 信号防递归：表格 NoEditTriggers + cellClicked 单向，`_refresh_matrix` 不触发 cellClicked
- 异常与取消路径：`isinstance(r, dict)` 对缺项/非 dict 条目有守卫
- 状态提交一致性：点击切换 output 后同步刷新矩阵再 emit 快照（顺序正确）

## src/shared/ui/audio_mixer/saturation_dialog.py (行数: 245)

### 发现
- [AUD-37817] [Suggestion] QDial 滚轮/键盘调整绕过三段式节流（同 AUD-37801）
  - 位置: src/shared/ui/audio_mixer/saturation_dialog.py:163-166,189-192
  - 证据:
    ```python
    dial.sliderPressed.connect(lambda: self._dragging.update(active=True))
    dial.sliderReleased.connect(self._on_drag_released)
    ```
    ```python
    if self._dragging["active"]:
        self.saturation_preview.emit(self._track_id, self._current)
    else:
        self.saturation_committed.emit(self._track_id, self._current)
    ```
  - 影响: 同 AUD-37801：QDial wheel/键盘每步进一次 committed。
  - 建议: 同 AUD-37801。
  - 置信度: 中

- [AUD-37818] [Suggestion] 动态私有属性模式 + 与 noise_gate/reverb 大段复制
  - 位置: src/shared/ui/audio_mixer/saturation_dialog.py:171-175,181
  - 证据:
    ```python
    container._drag = spin
    container._dial = dial
    container._minimum = minimum
    container._step = step
    ```
    ```python
    drive=float(self._drive_w._drag.value()),
    ```
  - 影响: 同 AUD-37802/37807；三文件近重复维护。
  - 建议: 抽取共享复合控件/基类。
  - 置信度: 高

### 通过项
- 线程安全：全部 UI 线程
- GPU 资源生命周期 / GLSL·HDR 正确性：无 GPU 代码（N/A）
- 信号防递归：dial↔spin 双向 blockSignals；`_load_params_to_ui` 加锁；preset 用 activated；ToggleSwitch 回填不发射（已核验）
- 状态提交一致性：preview/committed 语义一致；preset/reset 保留 enabled
- 输入验证：drive 0..100 与 SaturationParams.__post_init__ 一致（timeline_v2.py:1943-1945）
- 异常与取消路径：无未捕获异常路径

## src/shared/ui/audio_mixer/strip_eq_dialog.py (行数: 618)

### 发现
- [AUD-37819] [Suggestion] 频响曲线在 paintEvent 中每次重绘都重算 scipy 频响，无缓存
  - 位置: src/shared/ui/audio_mixer/strip_eq_dialog.py:124-141
  - 证据:
    ```python
    if self._params is not None and self._params.enabled:
        freqs = np.logspace(
            np.log10(FREQ_MIN_HZ), np.log10(FREQ_MAX_HZ), 256
        )
        resp = compute_eq_response(self._params, self._sample_rate, freqs)

        path = QPainterPath()
    ```
  - 影响: compute_eq_response 最多执行 Low-Cut+High-Cut+15 band 共 17 次 scipy freqz（audio_dsp.py:966-989），叠加 logspace 与 QPainterPath 构建，全部在 UI 线程的 paintEvent 内。推子拖动期每次 set_params→update→repaint 都重算；窗口 expose/resize 等无关重绘也会重算相同结果。60Hz 拖动下每帧毫秒级 DSP 计算，挤占 UI 帧预算（项目 4K 实时基准下应避免）。
  - 建议: 在 set_params/update_sample_rate 中算好 (freqs, resp) 缓存，paintEvent 只做 path 构建与绘制；参数未变直接复用。
  - 置信度: 高（成本量级为估算，机制确定）

- [AUD-37820] [Suggestion] band 推子滚轮/键盘绕过三段式节流（同 AUD-37801 机制）
  - 位置: src/shared/ui/audio_mixer/strip_eq_dialog.py:230-231,584-587
  - 证据:
    ```python
    self._slider.sliderPressed.connect(lambda: self._on_drag_pressed() if self._on_drag_pressed else None)
    self._slider.sliderReleased.connect(lambda: self._on_drag_released() if self._on_drag_released else None)
    ```
    ```python
    if self._dragging:
        self.strip_eq_preview.emit(self._track_id, self._current)
    else:
        self.strip_eq_committed.emit(self._track_id, self._current)
    ```
  - 影响: QSlider wheel/方向键发 valueChanged 但无 sliderPressed/Released → 每步进一次 committed 进命令栈；15 个推子上滚轮尤为明显。
  - 建议: 同 AUD-37801（wheel/key 包 started/ended 或命令层合并）。
  - 置信度: 中

- [AUD-37821] [Suggestion] 跨对象访问私有成员、回调无类型注解、backend 探测异常被静默吞掉
  - 位置: src/shared/ui/audio_mixer/strip_eq_dialog.py:217,344-345,493-495
  - 证据:
    ```python
    def set_callbacks(self, on_changed, on_drag_pressed, on_drag_released) -> None:
    ```
    ```python
        except Exception:
            pass
    ```
    ```python
    bands = tuple(f._slider_to_gain(f._slider.value()) for f in self._band_faders)
    ```
  - 影响: (1) `_collect_params` 读取 BandFaderWidget 的 `_slider`/`_slider_to_gain` 私有成员，封装泄漏；(2) set_callbacks 三个回调参数无注解；(3) scipy 探测的 `except Exception: pass` 若因其他错误（如 i18n/样式异常）失败，降级横幅静默消失，违背"不得宽泛 except 掩盖领域错误"精神（此处属可选 UI，风险有限）。
  - 建议: BandFaderWidget 暴露 value()/公共 API；回调参数注解为 Callable；except 收窄为 ImportError 并 logger.debug。
  - 置信度: 高

### 通过项
- 线程安全：全部 UI 线程，无 OpenGL/跨线程调用；compute_eq_response 无共享可变状态
- GPU 资源生命周期 / GLSL·HDR 正确性：纯 QPainter 绘制，无 GPU 资源（N/A）；`_gain_to_y` 的 clamp 仅用于显示映射，不涉及 HDR 数据
- 信号防递归：`_load_params_to_ui` 对全部控件 blockSignals（DragValue 为自定义门控，已核验）；mode/preset 用 currentIndexChanged 仅在用户操作时生效、`activated` 防程序性复位回环；`_rebuild_band_faders` 中闭包以默认参数捕获索引，无晚绑定 bug；ToggleSwitch.setChecked 不发射（已核验）
- 状态提交一致性：mode 切换补零 bands 后立即 committed；preset/reset 保留 low/high-cut 与 enabled；update_sample_rate 仅刷新曲线不误发提交
- 输入验证：StripEQParams.__post_init__ 校验 mode/gain/low_cut/high_cut/bands 范围（timeline_v2.py:1654-1667）；EQ_BAND_CONFIGS 有 3-band 回退；from_dict 对 bands 长度补齐截断
- 异常与取消路径：`_on_mode_changed` 索引越界守卫；scipy 缺失时 compute_eq_response 返回零线且对话框有降级横幅
- 性能模式（除 AUD-37819/37820 外）：三段式节流与 preview/committed 分离正确；`update()` 由 Qt 合并重绘
