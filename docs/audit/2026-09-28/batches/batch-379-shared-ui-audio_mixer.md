# batch-379 [P2] src/shared/ui/audio_mixer — 审查报告

编号区间：AUD-37900–AUD-37999。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

## src/shared/ui/audio_mixer/track_mixer_widget.py (行数: 1919)

### 发现

- [AUD-37900] [Critical] Limiter 参数编辑只走 preview 出口，永不进命令栈，用户修改不持久化、不可撤销
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:1166-1168, 1174-1191
  - 证据:
    ```python
    dlg.limiter_committed.connect(
        lambda tid, p: self._on_limiter_edited(inst, p)
    )
    ...
    def _on_limiter_edited(self, inst: Any, limiter_params: Any) -> None:
        ...
        self._effect_chain = EffectChainParams(effects=effects)
        # enabled 通过 params 流转（开关/右键旁路），需刷新列表的 [name]/灰色显示
        self._refresh_effect_list()
        self.track_effects_preview.emit(self._track_id, self._effect_chain)
    ```
  - 影响: 对话框的 `limiter_committed`（语义为"松手/关闭，进命令栈"）被路由到只 emit `track_effects_preview` 的处理器；对比其余 7 类效果器均有 `commit: bool` 分支并在 commit 时 emit `track_effects_committed`。下游（edit_controller.py:3452-3481）preview 只调 `mgr.set_track_effect_chain` 做实时听感、不建命令，committed 才走 `SetTrackEffectChainCommand`。结果：Limiter 的所有参数修改（含开关/阈值等）不产生 undo 记录、不落库；undo/redo 其它链命令或重开项目即丢失。limiter_dialog.py 文件头（line 10）声明应走 preview/committed 双路，此处为接线遗漏；`limiter_preview` 信号也完全未连接。
  - 建议: 仿照 compressor 路径，连接 `limiter_preview` → `_on_limiter_edited(inst, p, commit=False)`、`limiter_committed` → `commit=True`，并在 commit 分支 emit `track_effects_committed`。
  - 置信度: 高

- [AUD-37901] [Major] balance 拖动期经 spinbox 直连泄露 balance_changed（commit 信号），每次拖动向 undo 栈灌入大量命令且松手重复提交
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:591-599, 1638-1650
  - 证据:
    ```python
    self._balance_fader.valueChanged.connect(self._on_balance_changed)
    self._balance_fader.valueChanged.connect(self._balance_spin.setValue)
    ...
    def _on_balance_spin_changed(self, value: int) -> None:
        self._balance = value
        self._balance_fader.blockSignals(True)
        self._balance_fader.setValue(value)
        self._balance_fader.blockSignals(False)
        self.balance_changed.emit(self._track_id, value)
    ```
  - 影响: 拖动期每过一个整数刻度，`valueChanged` 同时触发 `_on_balance_changed`（发 preview）与 `_balance_spin.setValue` → spin 的 `valueChanged` → `_on_balance_spin_changed`（无拖动守卫，直接 emit `balance_changed`）。下游（edit_controller.py:2476-2489）`balance_changed` 每次执行一条 `SetTrackPanCommand`，一次拖动产生数十条 undo 命令；`_on_balance_released`（line 1652-1656）再补发一次造成重复提交。音量路径在 `_on_volume_changed` 中用 blockSignals 同步 spin（line 1597-1599）是正确范本，balance 路径漏用。
  - 建议: 删除 `valueChanged → _balance_spin.setValue` 直连，改在 `_on_balance_changed` 内 blockSignals 同步 spin；或给 `_on_balance_spin_changed` 加 `_balance_dragging` 守卫。
  - 置信度: 高

- [AUD-37902] [Major] 删除效果器实例不处理其打开中的编辑对话框：后续编辑被静默丢弃并向命令栈发出无变化链的重复提交
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:1045-1061, 1180-1187, 1230-1242
  - 证据:
    ```python
    def _effect_remove(self, inst: Any) -> None:
        """删除一个效果器。"""
        if self._effect_chain is None:
            return
        ...
        effects = tuple(e for e in self._effect_chain.effects if e.instance_id != inst.instance_id)
    ```
    ```python
    effects = tuple(
        TrackEffectInstance(
            effect_type=e.effect_type,
            params=new_params if e.instance_id == inst.instance_id else e.params,
            ...
    ```
  - 影响: `_effect_remove` 不关闭、不清理该 instance_id 已打开的对话框（`_limiter_dialogs` 等 8 个字典）。对话框保持可编辑，但提交时 `instance_id` 已匹配不到任何条目 → 链内容不变却仍 emit `track_effects_committed`，下游生成一条 old=new 的空命令入栈；用户在对话框中的调整被静默丢弃。
  - 建议: `_effect_remove`（及 `_on_fx_clicked` 的旁路重建路径）中删除实例时关闭对应对话框并从字典清理；`_on_*_edited` 检测到 instance_id 不存在时直接 return 而非重建并 emit。
  - 置信度: 高

- [AUD-37903] [Major] 三处 `except Exception: pass` 掩盖错误，且 EQ 后端探测失败默认"可用"，SciPy 缺失时降级警示（橙色 badge）永不出现
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:803-807, 809-821, 1748-1755
  - 证据:
    ```python
    if self._dynamics_dialog is not None:
        try:
            self._dynamics_dialog.update_chain(chain)
        except Exception:
            pass
    ...
    eq_backend_ok = True
    try:
        from src.features.edit.core.audio_capabilities import (
            is_scipy_available_for_dsp,
        )
        eq_backend_ok = is_scipy_available_for_dsp()
    except Exception:
        pass
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"。裸 `except Exception` 连 ImportError/AttributeError 一并吞掉：EQ 后端探测失败时 `eq_backend_ok` 保持 True，`eq_degraded` 恒为 False，AM-01 专门设计的降级告警色（line 1792-1818）在此故障路径下失效，用户在 scipy 缺失（EQ 实际静默旁路）时看到的仍是"已生效"青色。line 818-819 还直接改写 `_eq_dialog._current` 私有成员并调用其 `_load_params_to_ui()`，失败同样被吞。
  - 建议: 捕获具体异常类型并 logger.warning；探测失败时按"不可用"处理（保守默认 False），并清理对对话框私有成员的越界访问（给 StripEQDialog 提供公开的 update_params 接口）。
  - 置信度: 高

- [AUD-37904] [Major] `_sync_dialog_toggle` 的注释断言"setChecked 不发 toggled"与 Qt 语义相反，程序化 setChecked 会触发对话框处理器，造成级联重复 preview/commit
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:1113-1121, 1136-1141
  - 证据:
    ```python
    # 若该实例的对话框正打开，同步其开关（setChecked 不发 toggled，不会回环 emit）
    self._sync_dialog_toggle(inst.instance_id, new_enabled)
    ...
    toggle = getattr(dlg, '_enable_toggle', None) or getattr(dlg, '_enable_check', None)
    if toggle is not None:
        toggle.setChecked(enabled)
    ```
  - 影响: QAbstractButton.setChecked 在状态实际变化时会发 toggled（无论用户操作还是程序调用），而对话框侧 `self._enable_toggle.toggled.connect(self._on_enabled_toggled)`（limiter_dialog.py:65、compressor_dialog.py:90 等）会随之触发，向 widget 再回发一轮 preview/committed —— FX 一键旁路（`_on_fx_clicked`）或右键 Bypass 后出现重复命令提交/多余刷新。错误注释还会误导后续维护者继续依赖该假设。未观察到无限循环（状态收敛后停止），故不升 Critical。
  - 建议: setChecked 前 `toggle.blockSignals(True)` / 后 `False`；或改为调用对话框的公开回填接口；同步修正注释。
  - 置信度: 中（对话框处理器细节在其它文件，级联的最终影响未逐一执行验证；"setChecked 状态变化即发 toggled"本身为确定的 Qt 语义）

- [AUD-37905] [Major] 标签右键菜单把 tag 标签坐标系的 pos 统一用 `_track_label.mapToGlobal` 映射，右键 tag 时菜单弹出位置偏移
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:313-318, 1662-1689
  - 证据:
    ```python
    self._track_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    self._track_label.customContextMenuRequested.connect(self._on_label_context_menu)
    ...
    self._track_tag_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    self._track_tag_label.customContextMenuRequested.connect(self._on_label_context_menu)
    ...
    menu.exec(self._track_label.mapToGlobal(pos))
    ```
  - 影响: `customContextMenuRequested(pos)` 的 pos 属于发射控件自身坐标系；tag 标签（位于 header 顶部）与 `_track_label`（位于 badges/效果器列表之下）相距数十像素，右键 tag 时菜单整体偏移两标签间距。窄模式下 `_track_label` 被隐藏（`_update_track_label` line 1836-1837），tag 是注释（line 310-312）明示的唯一菜单入口，错位必现。
  - 建议: 用 `self.sender()` 判定发射源并分别 `mapToGlobal`，或把两个标签包进同一容器 widget 并以容器坐标映射。
  - 置信度: 高

- [AUD-37906] [Suggestion] EQ 胶囊 tooltip 在构造期即被 `_update_badges_style` 清空，"Open Graphic EQ" 提示永不显示
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:267-270, 1819-1822
  - 证据:
    ```python
    self._eq_badge.clicked.connect(self._open_eq_from_badge)
    self._eq_badge.setToolTip(I18N.tr("audio.mixer.effects", "Open Graphic EQ"))

    self._update_badges_style()
    ...
    else:
        self._eq_badge.setStyleSheet(cyan_active_style if eq_active else inactive_style)
        # 清除降级 tooltip，避免状态切换后残留
        self._eq_badge.setToolTip("")
    ```
  - 影响: 构造期 line 270 调用 `_update_badges_style`，无 chain 时 `eq_degraded=False` 走 else 分支，把 line 268 刚设置的 tooltip 置空；从降级态恢复时也只清空不还原默认提示。FX/DY 胶囊不受影响（该函数只清 EQ 的 tooltip），行为不一致。
  - 建议: 保存默认 tooltip（构造时或常量），恢复正常态时还原默认值而非置空。
  - 置信度: 高

- [AUD-37907] [Suggestion] 模块声明的 `strip_eq_preview` / `strip_eq_committed` 信号从未 emit（死 API）
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:150-152
  - 证据:
    ```python
    # W8: Strip EQ 信号（StripEQParams | None）
    strip_eq_preview = pyqtSignal(str, object)   # track_id, StripEQParams|None（实时听感）
    strip_eq_committed = pyqtSignal(str, object)  # track_id, StripEQParams|None（命令栈）
    ```
  - 影响: 全文件（及全库 grep）无任何 `self.strip_eq_preview.emit` / `self.strip_eq_committed.emit`；EQ 编辑统一改走 `track_effects_*`（723、1524 行连接的是对话框自身的同名信号）。死信号误导维护者以为存在独立 EQ 提交通路。
  - 建议: 删除这两个信号声明，或在注释中标注已被 `track_effects_committed` 取代。
  - 置信度: 高

- [AUD-37908] [Suggestion] 8 类效果器对话框字典无任何清理机制，已关闭对话框被 widget 永久引用
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:189, 1169-1171, 1219-1221 等
  - 证据:
    ```python
    self._limiter_dialogs: dict = {}  # {instance_id: LimiterDialog}
    ...
    if not hasattr(self, '_limiter_dialogs'):
        self._limiter_dialogs = {}
    self._limiter_dialogs[inst.instance_id] = dlg
    dlg.show()
    ```
  - 影响: `_limiter_dialogs`/`_compressor_dialogs`/`_reverb_dialogs`/`_delay_dialogs`/`_saturation_dialogs`/`_imager_dialogs`/`_noise_gate_dialogs`/`_strip_eq_dialogs` 只增不减——没有任何 `finished`/`destroyed` 连接或删除逻辑；"添加效果→打开编辑→删除效果"反复操作会在会话期内缓慢累积已关闭对话框（Qt C++ 侧因 parent 存活到窗口销毁，Python 侧引用同样滞留）。
  - 建议: 对话框 `finished` 时从字典移除并 `deleteLater()`；`_effect_remove` 同步清理（与 AUD-37902 一并处理）。
  - 置信度: 高

- [AUD-37909] [Suggestion] FX 列表项显示名使用硬编码英文 `EFFECT_SHORT_NAMES`，与菜单/对话框的 i18n 显示名不一致
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:36-45, 840-843
  - 证据:
    ```python
    EFFECT_SHORT_NAMES = {
        "strip_eq": "Graphic EQ",
        "limiter": "Limiter",
        ...
    }
    ...
    short = EFFECT_SHORT_NAMES.get(inst.effect_type, inst.effect_type)
    ```
  - 影响: 添加菜单/对话框标题用 `_effect_display_name`（I18N.tr，line 76-88），而列表项显示用硬编码英文名；非英文语言下同一效果器在菜单与列表中名称不一致，违背项目 i18n 约定。
  - 建议: `EFFECT_SHORT_NAMES` 改为 I18N key，`_refresh_effect_list` 中经 `I18N.tr` 取短名。
  - 置信度: 高

- [AUD-37910] [Suggestion] 拖动热路径内重复 import；`pos`/`event` 参数与多个集合缺类型注解
  - 位置: src/shared/ui/audio_mixer/track_mixer_widget.py:1606-1609, 953, 1691, 189, 51
  - 证据:
    ```python
    from PyQt6.QtGui import QCursor
    from PyQt6.QtWidgets import QToolTip
    tip_text = f"{db_val:+.1f} dB"
    QToolTip.showText(QCursor.pos(), tip_text, self._volume_fader)
    ...
    def _on_effect_list_context_menu(self, pos) -> None:
    ...
    def _on_label_double_clicked(self, event) -> None:
    ```
  - 影响: 音量拖动每个 tick 都执行两次模块查找 + showText（首次后为 sys.modules 命中，开销小但属热路径坏味道）；`pos: QPoint`、`event: QMouseEvent`、`_limiter_dialogs: dict[str, Any]`、`-> dict[str, Any]` 等注解缺失，削弱本项目强类型约定。
  - 建议: 将 QToolTip/QCursor 提升为模块级 import；补齐参数与集合类型注解。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程控件，无 OpenGL/跨线程直接调用；电平更新经 Qt 信号（audio_level_changed → panel → update_audio_level）排队到达，链路安全
- GPU 资源生命周期：不涉及 ModernGL/纹理/缓冲区（纯 Qt Widgets）
- GLSL·HDR 正确性：不适用（音频 UI，无着色器代码）
- 信号防递归（回填路径）：set_mute/set_solo/set_armed/set_volume_linear/set_pan_linear/set_strip_eq 均正确 blockSignals 后 setChecked/setValue；`_refresh_effect_list` 对列表 blockSignals 正确（balance 拖动路径除外，见 AUD-37901）
- 状态提交一致性（音量路径）：volume 三段式节流（按下置位→拖动 preview→松手 changed）实现正确，spin 回填走 blockSignals，双击/失焦补发场景有 active 守卫（Limiter 与 balance 除外，见 AUD-37900/37901）
- 输入验证：linear_to_db/db_to_linear/pan_to_balance/balance_to_pan/_db_to_slider/_slider_to_db 均做边界 clamp，换算闭环（-60dB↔0、+6dB↔10000）自洽
- 性能模式：EQ 后端探测 `is_scipy_available_for_dsp` 为缓存 O(1) 查表（audio_capabilities.py:216-222）；效果器列表刷新仅在提交/回填路径触发，规模小；`_update_size_constraints` 用确定性数值不依赖 sizeHint
- 异常路径（对话框打开）：各 `_open_*_editor` 均有已打开去重（isVisible → raise_/activateWindow），instance_id 定位使用 next(..., None) 带默认值，无裸 index 崩溃路径（宽泛 except 问题见 AUD-37903）
