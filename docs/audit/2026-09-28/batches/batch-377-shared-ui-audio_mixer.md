# Batch 377 — src/shared/ui/audio_mixer（AUD-37700–AUD-37799）

审查范围：8 个文件，2,028 行。编号区间 AUD-37700–AUD-37799。
本批次为纯 UI 对话框/数据模块（无 OpenGL、无 GPU 资源、无 GLSL）。审查重点落在
信号防递归 / 三段式节流（preview→committed）/ 状态提交一致性（undo 栈污染）/ 输入验证。

关键交叉验证的依赖实现（只读）：
- `src/shared/ui/widgets/drag_value.py`：`DragValue.setValue` 在值变化且未 blockSignals 时必发 `valueChanged`（190-197 行）；`blockSignals` 被覆写为实例级标志。
- `src/shared/ui/widgets/toggle_switch.py`：`ToggleSwitch.setChecked` 不发 `toggled`（43-46 行），对话框注释"天然防回环"属实。
- `src/shared/keyframe.py`：`get_keyframe_at` 为精确帧匹配；`add_keyframe` 同帧替换、异帧新增。
- `src/features/edit/core/commands.py:8866-9060`：Add/RemoveAutomationKeyframeCommand 每信号一条命令；`with_automation_keyframe_removed` 对不存在的帧静默无操作。
- `src/features/edit/core/audio_dsp.py:2296-2388`：`EffectChain.process` 同时按条目级 `enabled`（`to_dsp_entries`→`e.enabled`）与 `params["enabled"]`（`inst.is_active`）双重门控。

---

## src/shared/ui/audio_mixer/automation_lane.py (行数: 396)

### 发现

- [AUD-37701] [Major] 拖动关键帧会在模型中留下"关键帧轨迹"并按鼠标移动刷爆撤销栈
  - 位置: src/shared/ui/audio_mixer/automation_lane.py:211-224（另见 371-374）
  - 证据:
    ```python
    if self._dragging_kf_frame is not None:
        # 拖动中：移动关键帧
        new_frame = self._x_to_frame(pos.x())
        new_value = self._y_to_value(pos.y())
        old_frame = self._dragging_kf_frame
        ...
        self.keyframe_moved.emit(old_frame, old_val, new_frame, new_value)
    ```
    ```python
    def _on_kf_moved(self, old_frame: int, old_val: float, new_frame: int, new_val: float) -> None:
        # 移动 = remove old + add new（两次命令）
        self.remove_keyframe.emit(self._track_id, self._current_param, old_frame)
        self.add_keyframe.emit(self._track_id, self._current_param, new_frame, new_val)
    ```
  - 影响: `_dragging_kf_frame` 只在 mousePressEvent 设置、mouseReleaseEvent 清空，拖动期间从不更新为新帧。第一次移动后原帧上的关键帧已被 remove，此后每次 mouseMove 发出的 `remove_keyframe(old_frame)` 都命中不存在的帧（`KeyframeTrack.remove_keyframe` 返回 False 静默无操作），而 `add_keyframe(new_frame)` 每次都在新帧追加。一次普通拖动会：1) 在鼠标经过的每个中间帧留下一个杂散关键帧，自动化曲线被破坏，回放按错误曲线驱动；2) 每次鼠标移动提交 2 条命令（edit_controller.py:3236-3261 直接 `_execute_command`），撤销栈被刷爆，一次拖动需几十次 undo；3) 即便只有一次移动，"移动"也拆成 remove+add 两条命令，非原子操作。
  - 建议: 拖动期间本地预览（或每步更新 `_dragging_kf_frame` 为接受后的新帧），release 时用单条 Move 命令提交（可基于 `EditTrack.with_automation_keyframe_updated`，timeline_v2.py:2691 已有该原语）。
  - 置信度: 高

- [AUD-37702] [Major] 双击切换插值类型的信号全库无消费者，功能静默失效
  - 位置: src/shared/ui/audio_mixer/automation_lane.py:266、379-380（消费端缺失：src/app/controllers/edit_controller.py:3217-3220）
  - 证据:
    ```python
    set_keyframe_interp = pyqtSignal(str, str, int, str)  # track_id, param, frame, interp_name
    ```
    ```python
    def _on_kf_interp(self, frame: int, interp: str) -> None:
        self.set_keyframe_interp.emit(self._track_id, self._current_param, frame, interp)
    ```
    edit_controller 打开 AutomationLane 时只连接了 `add_keyframe` / `remove_keyframe` / `clear_param` / `automation_mode_changed` 四个信号；全库 grep `set_keyframe_interp` 仅有本文件的定义与 emit。
  - 影响: 底部提示文案承诺 "Double-click: change interpolation"，但双击后既无命令、也无状态变化、也无任何反馈——死功能，用户以为已切换。
  - 建议: 在 edit_controller 接线并实现 SetAutomationInterpolation 命令；实现前先禁用该交互并修正提示文案。
  - 置信度: 高

- [AUD-37703] [Suggestion] AutomationLane 直接改写子控件私有字段
  - 位置: src/shared/ui/audio_mixer/automation_lane.py:359-361
  - 证据:
    ```python
    self._curve._param_name = param
    self._curve._min_val = min_v
    self._curve._max_val = max_val
    ```
  - 影响: 破坏封装；若 AutomationCurveWidget 日后在参数切换时需要重置拖动/悬停状态，没有单一入口保证不变量。
  - 建议: 在 AutomationCurveWidget 上提供 `set_param(param, min_val, max_val)` 公共方法。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程控件，无跨线程访问、无 OpenGL 调用
- GPU 资源生命周期：不涉及
- GLSL/HDR：不涉及（`_value_to_y` 的 clamp 是像素坐标换算，非 HDR 数据）
- 信号防递归：`_param_combo`/`_mode_combo` 均 setCurrentText 先于 connect；无程序化回填路径
- 三段式节流：不适用（命令式交互，点击/删除各一条命令）
- 输入验证：`_x_to_frame`/`_y_to_value` 均夹取到合法范围；`set_total_frames` 有 max(1,...) 保护
- 异常路径：`except ValueError` 为窄捕获，无宽泛 except
- i18n：文案均经 I18N.tr

---

## src/shared/ui/audio_mixer/compressor_dialog.py (行数: 298)

### 发现

- [AUD-37704] [Suggestion] `_make_knob_spin` 旋钮工厂在 4 个对话框间复制粘贴，靠动态属性传递状态
  - 位置: src/shared/ui/audio_mixer/compressor_dialog.py:149-209（同构代码还存在于 delay_dialog.py:143-202、imager_dialog.py:123-182、dynamics_dialog.py:51-118）
  - 证据:
    ```python
    # 挂载引用，替代旧的 findChild(QDoubleSpinBox)
    container._drag = spin
    container._dial = dial
    container._minimum = minimum
    container._step = step
    return container
    ```
  - 影响: 约 60 行 dial+DragValue 联动逻辑（含 blockSignals 防回环、节流接线）复制了 4 份，`container._drag` 等动态属性无类型检查、无返回类型注解；修 bug 需改 4 处（dynamics_dialog 已示范了更好的模块级 `_make_knob` 模式）。本次审查发现 limiter 的复制版本已经出错（见 AUD-37712）。
  - 建议: 抽取到共享模块（参照 dynamics_dialog 的 `_make_knob`/`_set_knob_value`），返回带类型的封装对象。
  - 置信度: 高

### 通过项
- 三段式节流：dial sliderPressed/Released + DragValue interaction_started/ended 全部接线；wheelEvent 与内联编辑路径（drag_value.py:283-288、393-394）均包裹 started/ended，preview/committed 切换正确
- 信号防递归：dial↔spin 联动均单向 blockSignals；`_load_params_to_ui` 对 spin/dial 用 try/finally 成对恢复 blockSignals；`ToggleSwitch.setChecked` 不发 toggled（已核实 toggle_switch.py:43-46），preset 应用无回环；preset 下拉连 `activated`（仅用户触发），程序化 setCurrentIndex(0) 不重入
- 输入验证：UI 范围（-60..0 / 1..20 / 0..1000 / 10..5000 / 0..40 / 0..24）与 `CompressorParams.__post_init__` 校验一致，DragValue 自身 clamp，不会触发 ValueError
- 状态提交一致性：preset/reset 均先 `_load_params_to_ui()`（阻塞信号）再单次 committed，一次操作一条命令
- 线程安全 / GPU / GLSL：不涉及
- 异常路径：无宽泛 except
- 类型注解：公开方法均有注解

---

## src/shared/ui/audio_mixer/delay_dialog.py (行数: 294)

### 发现

- [AUD-37705] [Suggestion] 旋钮工厂复制品：decimals 硬编码 0，与 compressor 版本行为分叉
  - 位置: src/shared/ui/audio_mixer/delay_dialog.py:143-202（167-170 行 `decimals=0`）
  - 证据:
    ```python
    spin = DragValue(
        "", min_v=minimum, max_v=maximum, default=value,
        decimals=0, suffix=suffix,
    )
    ```
  - 影响: compressor_dialog 按步长推导 `decimals=(1 if step < 1 else 0)`，本文件硬编码 0——当前 4 个参数步长均为 ≥1 尚无实害，但两份复制版本已有行为分叉，后续调整步长（如 time_ms 精确到 0.5ms）会直接显示取整错误。同 AUD-37704 的复制问题。
  - 建议: 与 AUD-37704 一并抽取共享工厂，按步长推导 decimals。
  - 置信度: 高

### 通过项
- 三段式节流：与 compressor 相同的正确接线（含 wheelEvent / 内联编辑路径）
- 信号防递归：dial↔spin blockSignals 单向；`_pingpong_check` 回填时 blockSignals；ToggleSwitch.setChecked 无信号；preset `activated` 仅用户触发
- 状态提交一致性：ping-pong / enabled 切换立即单次 committed；preset/reset 走 `_load_params_to_ui` 单次提交
- 输入验证：UI 范围与 `DelayParams.__post_init__` 一致（1..2000 / 0..90 / 0..100 / 20..1000）
- 线程安全 / GPU / GLSL：不涉及
- 异常路径：无宽泛 except

---

## src/shared/ui/audio_mixer/dynamics_dialog.py (行数: 480)

### 发现

- [AUD-37706] [Major] 面板打开时无实例的 tab 显示"范围最小值"而非默认参数，与文档承诺相反
  - 位置: src/shared/ui/audio_mixer/dynamics_dialog.py:177-182、219（模块 docstring 10-11 行承诺"找不到则用默认参数显示"）
  - 证据:
    ```python
    @staticmethod
    def _params_to_dict(params) -> dict:
        """把 XxxParams 转 dict（None 用默认）。"""
        if params is None:
            return {}
    ```
    ```python
    for (label, key, minimum, maximum, step, suffix) in self._param_fields:
        value = float(self._params.get(key, minimum))
    ```
  - 影响: `None → {}` 使旋钮回退到 `minimum` 而非 `XxxParams.default()`。轨道没有 DY 实例时打开面板，Compressor tab 显示 threshold=-60（默认 -24）、ratio=1.0（默认 4.0）、release=10（默认 250）、knee=0（默认 30）；Noise Gate 显示 -60/0/10（默认 -40/1/100）；Limiter 显示 -20/10（默认 -3/100）。用户基于错误显示调参；且只动一个旋钮时提交的 params dict 仅含该键，其余键由 `from_dict` 服务端默认值填充——UI 显示值与实际提交状态不一致。
  - 建议: `_DynamicsTab` 增加 params 类参数，None 时用 `XxxParams.default().to_dict()` 初始化。
  - 置信度: 高

- [AUD-37707] [Suggestion] 外部删除 DY 实例后 `load_params(None)` 完全不回退，tab 显示陈旧参数
  - 位置: src/shared/ui/audio_mixer/dynamics_dialog.py:268-275
  - 证据:
    ```python
    def load_params(self, params) -> None:
        """外部回填参数（程序化更新旋钮 + 开关，不发信号）。"""
        self._params = self._params_to_dict(params) if params is not None else self._params
    ```
  - 影响: `update_chain`（外部 timeline 变化时由 track_mixer_widget.set_effect_chain 调用）在 DY 实例被 undo 掉之后传入 None，tab 保留旧值继续显示，用户以为效果仍在链上。
  - 建议: None 时同样回退到默认参数显示。
  - 置信度: 高

- [AUD-37708] [Suggestion] enabled 双真相源：tab 开关只写 params["enabled"]，实例级 enabled 恒为 old.enabled/True
  - 位置: src/shared/ui/audio_mixer/dynamics_dialog.py:427-440
  - 证据:
    ```python
    if idx >= 0:
        old = effects[idx]
        effects[idx] = TrackEffectInstance(
            effect_type=effect_type, params=dict(params_dict),
            enabled=old.enabled, order=old.order, instance_id=old.instance_id,
            panel="dynamics",
        )
    else:
        ...
        effects.append(TrackEffectInstance(
            effect_type=effect_type, params=dict(params_dict),
            enabled=True, order=new_order, panel="dynamics",
        ))
    ```
  - 影响: DSP 侧 `EffectChain.process`（audio_dsp.py:2367-2375）同时要求条目级 `e.enabled` 与 `params["enabled"]`（`inst.is_active`）为 True。当前 DY 实例创建时实例级恒为 True，开关经 params 路径生效，功能暂时正常；但同一开关状态存在两个字段，序列化/外部工具改了实例级 enabled=False 后，面板开关永远无法重新启用效果。
  - 建议: 提交时把 `enabled=bool(params_dict.get("enabled", True))` 同步到实例级字段，或在 DY 面板明确单一真相源。
  - 置信度: 中

- [AUD-37709] [Suggestion] `_DynamicsTab` 构造回调与 `_make_knob` 参数缺类型注解
  - 位置: src/shared/ui/audio_mixer/dynamics_dialog.py:51-54、152-161
  - 证据:
    ```python
    def _make_knob(
        label: str, value: float, minimum: float, maximum: float,
        step: float, suffix: str, on_changed, on_drag_pressed, on_drag_released,
    ) -> QWidget:
    ```
    （`_DynamicsTab.__init__` 的 `param_fields: list` / `presets: list` / `initial_params` / `on_params_changed` 同样无具体类型）
  - 影响: `param_fields`/`presets` 仅标 `list`，回调无 Callable 注解，调用方拼错元组长度或签名只能在运行时暴露。
  - 建议: 用 `Callable[[float], None]` 等补齐；param_fields 可定义 TypedDict/别名。
  - 置信度: 高

### 通过项
- 三段式节流：tab 级 `_dragging` 门控 commit，preview/committed 切换正确；`_on_drag_released` 有防重复保护
- 信号防递归：`_set_knob_value` / `load_params` 均 blockSignals + try/finally 成对恢复；ToggleSwitch.setChecked 无信号；preset `activated` 仅用户触发且应用后复位到索引 0 不重入
- 状态提交一致性：preview 期间只改对话框本地 chain 不发命令，release 单次 committed 提交整链
- panel 隔离：`_find_effect`/`_on_tab_params_changed` 均按 `panel="dynamics"` 过滤，不误碰 FX 列表同类实例（与 TrackEffectInstance.panel 字段语义一致）
- 输入验证：三个 tab 的旋钮范围与对应 Params 类 `__post_init__` 校验一致；`EffectChainParams` 仅 effects 一个字段，重建无字段丢失
- 线程安全 / GPU / GLSL：不涉及
- 异常路径：无宽泛 except（方法内局部 import 属避免循环依赖的惯例）

---

## src/shared/ui/audio_mixer/eq_presets.py (行数: 66)

### 发现

- [AUD-37710] [Suggestion] `get_presets_for_mode` 返回模块级可变列表的引用，调用方可污染全局预设
  - 位置: src/shared/ui/audio_mixer/eq_presets.py:56-58
  - 证据:
    ```python
    def get_presets_for_mode(mode: str) -> list[dict[str, Any]]:
        """返回指定 mode 的预设列表。未知 mode 回退到 3-band。"""
        return EQ_PRESETS.get(mode, EQ_PRESETS["3-band"])
    ```
  - 影响: 返回值与全局 `EQ_PRESETS` 共享可变结构；任何调用方就地 append/sort/改 bands 都会永久污染所有后续读取（`get_preset_by_name` 也受影响）。`get_preset_by_name` 已正确返回 `list(p["bands"])` 拷贝，说明作者意识到该问题但只修了一半。
  - 建议: 返回深拷贝或不暴露内部结构（如按 (mode, name) 查询）。
  - 置信度: 高

### 通过项
- 数据正确性：逐一核对 4 个 mode 的 bands 长度均与频段数匹配（3/7/10/15）
- 类型注解：完整（含 `list[float] | None`，配合 `from __future__ import annotations`）
- 纯数据模块：无逻辑分支（除 get 回退）、无异常路径、无线程/GPU/GLSL 维度
- 未知 mode 回退到 3-band：合理的输入容错

---

## src/shared/ui/audio_mixer/imager_dialog.py (行数: 255)

### 发现

- [AUD-37711] [Suggestion] 旋钮工厂第三份复制（同 AUD-37704）
  - 位置: src/shared/ui/audio_mixer/imager_dialog.py:123-182
  - 证据:
    ```python
    container._drag = spin
    container._dial = dial
    container._minimum = minimum
    container._step = step
    return container
    ```
  - 影响: 与 compressor/delay 同构的 ~60 行工厂第三份拷贝，动态属性 + 无返回注解；维护成本与分叉风险同 AUD-37704。
  - 建议: 一并抽取共享工厂。
  - 置信度: 高

### 通过项
- 三段式节流：接线正确（含 wheelEvent / 内联编辑路径）
- 信号防递归：dial↔spin 单向 blockSignals；`_load_params_to_ui` try/finally 成对恢复；ToggleSwitch.setChecked 无信号；preset `activated` 仅用户触发
- 状态提交一致性：preset/reset 均先阻塞回填再单次 committed
- 输入验证：width 0..200、pan -100..100 与 `ImagerParams.__post_init__` 一致
- 线程安全 / GPU / GLSL：不涉及
- 异常路径：无宽泛 except

---

## src/shared/ui/audio_mixer/limiter_dialog.py (行数: 206)

### 发现

- [AUD-37712] [Major] `_on_reset` 未阻塞信号：threshold 变化时 release 不会被重置，且一次点击多次 committed
  - 位置: src/shared/ui/audio_mixer/limiter_dialog.py:197-206
  - 证据:
    ```python
    self._current = LimiterParams.from_dict(d)
    self._thresh_spin.setValue(self._current.threshold_db)
    self._rel_spin.setValue(self._current.release_ms)
    self.limiter_committed.emit(self._track_id, self._current)
    ```
    （对照 drag_value.py:190-197——`DragValue.setValue` 值变化且未 blockSignals 时必发 `valueChanged`，连接到 `_on_spin_changed` → `_on_param_changed` → committed）
  - 影响: 设当前 UI 值 threshold=-10、release=500，点击 Reset（默认 -3/100）：1) `setValue(-3)` 触发 `valueChanged` → `_on_param_changed` 用 `_collect_params()`（此时 release spin 仍是 500）重建 `self._current`=(-3, 500) 并 committed（第 1 条命令，错误状态入栈）；2) 随后 `setValue(self._current.release_ms)` 写回的是旧值 500——release 永远不会被重置；3) 末尾再显式 committed（第 2 条命令）。一次 Reset 产生 2 条 undo 记录且功能不完整。compressor/delay/imager 的同位代码用 `_load_params_to_ui()`（blockSignals 包裹、单次 emit）是正确写法，本文件是漏改的复制变体。
  - 建议: 改用与 compressor_dialog 一致的 `_load_params_to_ui()` 模式：blockSignals 成对包裹两个 spin/dial 后单次 committed。
  - 置信度: 高

### 通过项
- 三段式节流（旋钮交互本身）：dial sliderPressed/Released + DragValue interaction_started/ended 接线正确，wheelEvent/内联编辑路径 preview→committed 切换正确
- 信号防递归：dial↔spin 联动单向 blockSignals
- 输入验证：threshold -20..0、release 10..1000 与 `LimiterParams.__post_init__` 一致
- 状态提交一致性（除 Reset 外）：enabled 开关、拖动松手均单次提交
- 线程安全 / GPU / GLSL：不涉及
- 异常路径：无宽泛 except

---

## src/shared/ui/audio_mixer/mixer_feature_flags.py (行数: 33)

### 通过项
- 通过（无逻辑）：纯常量模块，导出 `RECORD_ARM_IMPLEMENTED = True`（注释说明 W 阶段 4.4 已实现录音链路）与 `PURGE_CACHE_IMPLEMENTED = False`（缓存清理未实现，UI 入口应禁用）。无常量以外逻辑，九个维度均不适用。
- 注释与开关语义一致（True=已实现/可用，False=未实现/禁用）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-37701, AUD-37702, AUD-37706, AUD-37712 |
| Suggestion | 8 | AUD-37703, AUD-37704, AUD-37705, AUD-37707, AUD-37708, AUD-37709, AUD-37710, AUD-37711 |

共性观察：四个 FX 对话框共享一套正确但复制粘贴的 dial+DragValue 节流/防递归模式（limiter 的 Reset 是唯一漏改点）；automation_lane 是本批次唯一与命令栈/模型直接交互的组件，也是全部两个结构性 Major 的所在。
