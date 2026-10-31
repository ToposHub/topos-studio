# batch-376 审查报告 — src/shared/ui/audio_mixer/（4 文件，2,627 行）

编号区间：AUD-37600–AUD-37699。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 /
信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

本批次为纯 Qt Widgets 音频混音器 UI，无 OpenGL/GLSL 路径（GPU 维度不适用）。
跨文件语义（preview/commit 三段式、静默回填）已对照 `track_mixer_widget.py`（他批次）与
`src/app/controllers/edit_controller.py:2448`、`src/features/edit/ui/edit_page.py:606` 验证。

统计：Critical 0 · Major 9 · Suggestion 12。

---

## src/shared/ui/audio_mixer/__init__.py (行数: 17)

### 发现
- [AUD-37600] [Suggestion] 包级 __all__ 未导出 MeterMode，公共 API 类型不可从包导入
  - 位置: src/shared/ui/audio_mixer/__init__.py:2-17
  - 证据: `from src.shared.ui.audio_mixer.audio_level_meter import AudioLevelMeter, LevelStyle, PeakStyle, TickLabelsMode` /
    `__all__ = [ "AudioFader", "AudioLevelMeter", "LevelStyle", "PeakStyle", "TickLabelsMode", "TrackMixerWidget", "MixerSeparator", "AudioMixerPanel", ]`
  - 影响: `AudioLevelMeter.set_meter_mode(mode: "MeterMode")` / `meter_mode_changed` 暴露了 MeterMode 语义，但外部代码无法 `from src.shared.ui.audio_mixer import MeterMode`，只能深入子模块导入，公共 API 不自洽（当前仓内暂无外部使用者，属 API 完备性缺口）。
  - 建议: 在 import 与 __all__ 中补充 `MeterMode`。
  - 置信度: 高

### 通过项
- 通过（无逻辑）：纯 re-export 模块，导出 AudioFader / AudioLevelMeter / LevelStyle / PeakStyle / TickLabelsMode / TrackMixerWidget / MixerSeparator / AudioMixerPanel，导入路径与被导出符号均存在，无循环导入问题。

---

## src/shared/ui/audio_mixer/audio_fader.py (行数: 676)

### 发现
- [AUD-37601] [Major] `_value_from_position` 在滑轨长度为 0（widget 高/宽 == knob 高/宽）时除零，鼠标按下/拖动路径可触发异常终止
  - 位置: src/shared/ui/audio_mixer/audio_fader.py:647-662
  - 证据:
    ```python
        groove = self._get_groove_rect(QRectF(self.rect()))
        if self.orientation() == Qt.Orientation.Vertical:
            top = groove.top() + self._calculate_knob_size().height() / 2.0
            bottom = groove.bottom() - self._calculate_knob_size().height() / 2.0
            ratio = (bottom - pos) / (bottom - top)
    ```
  - 影响: `_update_size()`（362-367 行）把最小高度设为恰好等于 knob 高（宽推子 31px、窄推子 7px）。当布局把 widget 压到最小高度时 `bottom - top == 0.0`，`mousePressEvent`/`mouseMoveEvent` 调用本函数即抛 ZeroDivisionError；PyQt6 对事件处理器中未捕获异常默认按 qFatal 处理，可致整个进程退出。`range_val <= 0` 有守卫但滑轨零长度无守卫（对照 `_position_from_value` 630-645 无此除法）。
  - 建议: 在 `_value_from_position` 中对 `bottom <= top`（横向 `right <= left`）时直接返回 `min_val`，与 range 守卫同风格。
  - 置信度: 中（代码缺陷确定；触发依赖窗口被压缩到极小几何的边界场景）

- [AUD-37602] [Major] 无值变化的点击释放与逐格滚轮均触发提交通道，叠加 controller 无去重导致 undo 栈被空/碎片命令污染
  - 位置: src/shared/ui/audio_mixer/audio_fader.py:209-213, 337-356, 309-335
  - 证据:
    ```python
        def mouseReleaseEvent(self, event) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self._left_button_pressed = False
                self.slider_released.emit()
    ```
    （wheelEvent 352-356 每格 `self.setValue(self.value() + step)`，keyPressEvent 309-316 Home/End 跳极值，右/中键 187-191 直接 `setValue(self._neutral_position)`）
  - 影响: 释放信号无条件发射——在 knob 上原地点击（值未变）也走 `slider_released`；滚轮/键盘的每次 valueChanged 均发生在非拖动态。下游 `track_mixer_widget.py:1610-1612` 对非拖动 valueChanged 直接 emit `volume_changed`，`edit_controller.py:2448-2462` 每次提交无条件创建 `SetTrackVolumeCommand`（无相同值合并，`_execute_command` 亦无合并）。结果：点击一下推子帽 = 1 条无效果 undo 命令；滚轮调 20 格 = 20 条 undo 命令，用户需要多次 Ctrl+Z 才能回退一次实际调整。
  - 建议: 在 fader 侧记录本次按下周期内值是否变化，未变化则不发射 slider_released；或在 controller/命令层对 old==new 的 SetTrackVolume/SetTrackPan 提交短路。
  - 置信度: 中（发射链与 controller 行为已核实；undo 体验受害程度取决于命令栈 UI 呈现）

- [AUD-37603] [Suggestion] sliderChange 钳制分支为不可达死代码，且一旦触发会用 blockSignals 吞掉 valueChanged
  - 位置: src/shared/ui/audio_mixer/audio_fader.py:268-283
  - 证据:
    ```python
            current_value = self.value()
            new_value = max(min_val, min(current_value, max_val))
            if new_value != current_value:
                self.blockSignals(True)
                super().sliderChange(change)
                self.setValue(new_value)
                self.blockSignals(False)
                return
    ```
  - 影响: Qt 的 `QAbstractSlider.setValue/setRange` 在进入 sliderChange 前已把 value bound 到范围内，此分支实际不会执行；若未来因范围收缩被触发，值变更会被 blockSignals 静默吞掉，UI 与音频引擎状态脱钩且无任何信号可追。
  - 建议: 删除该分支，或改为钳制后不屏蔽信号（让消费者收到一次确定的钳制通知）。
  - 置信度: 中

- [AUD-37604] [Suggestion] `set_tick_positions([])` 不清理旧的 `_max_label_width` 与固定宽度
  - 位置: src/shared/ui/audio_mixer/audio_fader.py:98-108
  - 证据:
    ```python
        self._tick_positions = tick_positions
        if tick_positions:
            self._max_label_width = self._calculate_max_label_width(tick_positions)
            if self._tick_labels_visible and self.orientation() == Qt.Orientation.Vertical:
                width = self.KNOB_WIDTH + self._max_label_width + self.TICK_LABELS_GROOVE_MARGIN
                self.setFixedWidth(width)
    ```
  - 影响: 先设非空刻度再清空时，`_max_label_width` 残留旧值（`_get_tick_label_rect` 624 行继续用），固定宽度不回退，控件保持为已无标签时的过宽尺寸。
  - 建议: 空/非空两个分支都重算 `_max_label_width` 并按 `_tick_labels_visible` 与方向统一重设宽度（含横向分支，当前横向从不调整）。
  - 置信度: 高

- [AUD-37605] [Suggestion] 事件处理器参数普遍缺类型注解
  - 位置: src/shared/ui/audio_mixer/audio_fader.py:144, 159, 195, 209, 215, 230, 236, 249, 285, 290, 337
  - 证据: `def paintEvent(self, event) -> None:` / `def mousePressEvent(self, event) -> None:` / `def wheelEvent(self, event) -> None:`
  - 影响: 与仓库强类型约定不符，IDE/静态检查无法校验事件类型（应为 QPaintEvent/QMouseEvent/QWheelEvent/QKeyEvent/QResizeEvent/QChangeEvent 等）。
  - 建议: 为全部事件处理器参数补齐具体 Qt 事件类型注解。
  - 置信度: 高

### 通过项
- 线程安全：控件仅 UI 线程使用，paint/事件路径无跨线程共享可变状态。
- GPU 资源生命周期：不涉及 OpenGL；QPixmap 背景缓存（_update_background_cache）按需重建、无泄漏路径。
- 信号防递归：对外 setter（set_tick_positions/set_label_formatter/set_ticks_visible 等）只重绘不发信号；B7 失焦兜底补发 slider_released 与上层 active 守卫（track_mixer_widget.py:1625）配合正确，正常 release 后再补发为 no-op。
- 双击 B3 抑制首击跳值 + 双击重置发 pressed/released 对，preview/commit 语义正确（对照 track_mixer_widget.py:1583-1631 三段式验证）。
- HiDPI：背景缓存按 devicePixelRatioF 缩放并 setDevicePixelRatio，绘制坐标正确。
- 浮点字面量：绘制代码常量均显式 .0（1.5/2.0/0.5 等），符合规范。
- B1 步进常量（singleStep=40/pageStep=160/Ctrl=7）为编译期常量，无循环上限问题。

---

## src/shared/ui/audio_mixer/audio_level_meter.py (行数: 1218)

### 发现
- [AUD-37611] [Major] `set_meter_mode` 用宽泛 `except Exception: pass` 吞掉信号发射中的任意异常
  - 位置: src/shared/ui/audio_mixer/audio_level_meter.py:346-349
  - 证据:
    ```python
        try:
            self.meter_mode_changed.emit(mode.name)
        except Exception:
            pass
    ```
  - 影响: Qt 信号 emit 同步调用全部槽（panel._recompute_lufs_metering → lufs_metering_changed → AudioMixer.set_lufs_metering）。任何槽内真实错误（属性缺失、状态机异常）都会被静默吞掉，K-weighting 开关可能停在错误状态且无日志可查——直接违反“不得宽泛 except 掩盖领域错误”铁律。
  - 建议: 移除 try/except；确需防御时捕获具体异常类型并 logger.exception 记录。
  - 置信度: 高

- [AUD-37612] [Suggestion] 渐变填充在 compact 与横向分支每帧新建 QLinearGradient+QColor，绕过 A8 缓存优化
  - 位置: src/shared/ui/audio_mixer/audio_level_meter.py:599-606, 948-957
  - 证据:
    ```python
                    # Smooth gradient: green -> yellow -> red
                    gradient = QLinearGradient(bar_x, 0, bar_x + bar_w, 0)
                    gradient.setColorAt(0.0, QColor(46, 204, 113))     # green
                    gradient.setColorAt(0.7, QColor(241, 196, 15))     # yellow
                    gradient.setColorAt(0.9, QColor(231, 76, 60))      # red
                    gradient.setColorAt(1.0, QColor(192, 57, 43))      # dark red
    ```
    （横向非 compact 分支 948-957 同样每通道每帧构建）
  - 影响: A8 优化（929-938 行注释）已为垂直分支缓存固定渐变，但 compact 模式与横向分支仍在每帧每通道分配渐变与 4 个 QColor，且 551-552 行每次 paint 局部 import QPainterPath；电平表以音频回调频率（数十 Hz × 声道数）重绘时产生持续小对象分配。
  - 建议: compact/横向渐变同样在 `_update_layout` 中按几何预构建缓存（渐变仅依赖 bar 几何，不随 dB 值变化）；QPainterPath 提升为模块级 import。
  - 置信度: 高

- [AUD-37613] [Suggestion] `set_level_style(GRADIENT)` 强制关闭块线，切回 SOLID 不恢复用户偏好，菜单勾选态与渲染脱节
  - 位置: src/shared/ui/audio_mixer/audio_level_meter.py:319-324
  - 证据:
    ```python
        def set_level_style(self, style: LevelStyle) -> None:
            self._level_style = style
            if style == LevelStyle.GRADIENT:
                self._draw_block_lines = False
            self._draw_background()
    ```
  - 影响: 用户在右键菜单勾选 "Show Block Lines" 后切到 Gradient 再切回 Solid：`_draw_block_lines` 永久为 False，但 `_block_lines_action` 仍处于 checked 状态（443-449 行持久化值未被重置），UI 显示“开启”而画面无块线，需手动取消再勾选才能恢复。
  - 建议: `set_level_style(SOLID)` 时恢复 QSettings 中保存的 block_lines 偏好（或同步 action 勾选态）。
  - 置信度: 高

- [AUD-37614] [Suggestion] `channels` 构造参数未验证，0/负值在横向布局与 compact 绘制路径引发除零
  - 位置: src/shared/ui/audio_mixer/audio_level_meter.py:150-159, 575, 1164-1167
  - 证据:
    ```python
            bar_h = max(3, (inner_h - bar_gap) // self._audio_channels)   # _paint_compact:575
    ...
                self._secondary_axis_length = max(
                    self.MIN_SECONDARY_AXIS,
                    (drawable_height - (self._audio_channels + 1) * self.CHANNEL_BORDER_WIDTH) // self._audio_channels  # _update_layout:1166
                )
    ```
  - 影响: 构造函数对 `channels` 无下限校验；channels=0 时横向 `_update_layout`（1166 行）在构造期即除零，compact 模式则在 paintEvent（先于 529 行空列表守卫执行，518-520 行）除零。当前唯一调用方 track_mixer_widget.py:387 恒传 2，属防御缺口而非现行 bug。
  - 建议: 构造函数开头 `channels = max(1, int(channels))`，与“外部数据须验证”规范一致。
  - 置信度: 中

- [AUD-37615] [Suggestion] 垂直与水平刻度标签符号不一致（abs vs 带负号）
  - 位置: src/shared/ui/audio_mixer/audio_level_meter.py:772, 793
  - 证据: `label = str(abs(int(db)))   # 垂直：显示 5/10/...（达芬奇标度）` / `label = str(int(db))   # 水平：显示 -5/-10/...`
  - 影响: 同一 DB_SCALE 在垂直电平表渲染为无符号数字、水平电平表渲染为负数，两种朝向的视觉规范不统一（当前产品内均为垂直使用，影响潜在）。
  - 建议: 统一为一种约定（建议与垂直的达芬奇标度一致）。
  - 置信度: 高

- [AUD-37616] [Suggestion] 事件处理器参数缺类型注解
  - 位置: src/shared/ui/audio_mixer/audio_level_meter.py:472-473, 489-493, 497-515
  - 证据: `def contextMenuEvent(self, event) -> None:` / `def mousePressEvent(self, event) -> None:` / `def enterEvent(self, event) -> None:`
  - 影响: 类型检查无法验证 QContextMenuEvent/QMouseEvent/QEnterEvent/QResizeEvent。
  - 建议: 补齐具体事件类型注解。
  - 置信度: 高

### 通过项
- 线程安全：set_audio_values/reset/update_per_track_levels 均由 UI 线程信号驱动（edit_page.py:606 连接 audio_track_levels_changed，跨线程经 Qt 信号排队），无裸共享状态。
- GPU 资源生命周期：无 OpenGL；QPixmap 背景缓存与 A8 渐变缓存在布局变化时重建，无泄漏。
- 信号防递归：右键菜单 QAction 连接 triggered（非 toggled），`_on_fill_style_changed` 中 setChecked 不会回流；偏好恢复直接赋值字段不经 setter，构造期无信号外溢。
- A7 值变化短路条件含 “peak 已触底”，且削波 hold 4s（1100 行）< peak 衰减到底时长（0dB→-100dB 约 8.3s @12dB/s），连续帧流下削波指示不会因短路卡死不熄。
- C1 峰值衰减基于 perf_counter 时间差并 clamp dt 到 [0, 0.5]，帧率解耦且长暂停恢复安全（269-283 行）。
- iec_scale/iec_inverse 分段互逆（各段斜率与端点核对一致，含 ±端点），`_slider_to_db` 使用域 [0.025, 1.0) 在覆盖范围内；db_to_position 对 max_db=0 合法值有专门处理（104-105 行）。
- 输入钳位：set_audio_values 将输入钳到 [MIN_DISPLAY_DB, 0]（249 行），缺声道补 MIN、超长截断，属显示层合理钳位（非 HDR 数据）。
- LUFS 降级标记（AM-01）绘制路径完整：`set_lufs_backend_unavailable` 仅状态变化时重绘，非 compact 与 compact 两模式均叠加告警层。
- QSettings 持久化：键固定分组 `_METER_SETTINGS_GROUP`，读写对称（373-470 行）。

---

## src/shared/ui/audio_mixer/audio_mixer_panel.py (行数: 716)

### 发现
- [AUD-37621] [Major] 每帧 LUFS 后端探测：热路径内逐轨 import + 宽泛 except，失败时默认“后端可用”→ 显示伪造 LUFS 读数
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:432-446
  - 证据:
    ```python
                        lufs_backend_ok = True
                        try:
                            from src.features.edit.core.audio_capabilities import (
                                is_scipy_available_for_dsp,
                            )
                            lufs_backend_ok = is_scipy_available_for_dsp()
                        except Exception:
                            pass
                        if lufs_backend_ok:
                            meter_vals = vals.get("lufs", vals.get("peak", [-100.0, -100.0]))
    ```
  - 影响: `update_per_track_levels` 由 `audio_track_levels_changed` 每帧驱动（edit_page.py:606-610），LUFS 模式下每帧每轨执行 import 查表 + try/except（性能浪费）；更严重的是 import 失败（模块重构/循环导入等任何异常）时 `lufs_backend_ok` 保持 True，直接取 "lufs" 字段——按 AM-01 注释该值在 scipy 缺失时恒为 -70 的伪造读数，用户会把“后端降级”误读为“信号静音”，恰是 AM-01 要防止的场景；异常还被静默吞掉无日志。
  - 建议: 面板初始化时探测一次并缓存结果（或订阅后端能力变化信号）；except 失败时按“不可用”处理（回退 peak + set_lufs_backend_unavailable(True)）并记日志。
  - 置信度: 高

- [AUD-37622] [Major] additive（Shift）solo 将轨道移出 `_solo_muted` 记账却不解除其联动静音，轨道保持“solo 且静音”且永远脱离恢复名单
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:353-364
  - 证据:
    ```python
            if additive:
                if track_id not in self._solo_tracks:
                    self._track_mixers[track_id] … self._solo_tracks.append(track_id)
                self._solo_muted = [t for t in self._solo_muted if t != track_id]
                for tid, mixer in self._track_mixers.items():
                    if tid not in self._solo_tracks:
                        was_muted = mixer.is_mute()
                        mixer.set_mute(True)
    ```
  - 影响: 场景——A 独奏使 B 被 `set_mute(True)` 且入 `_solo_muted`；用户 Shift+点击 B 的 solo：B 加入 `_solo_tracks` 并被移出 `_solo_muted`，但循环只处理 `tid not in _solo_tracks` 的轨道，B 的 `_is_muted=True` 无人清除。结果 B 显示 solo 却仍被静音（无声）；后续 `clear_all_solo`（263-265 行）只恢复 `_solo_muted` 成员、取消 A 独奏的恢复循环（336-341 行）也不再包含 B，B 的静音成为永久残留（仅当用户专门再切换 B 的 solo 才经 349-350 行解除）。
  - 建议: 从 `_solo_muted` 移除 track_id 的同时 `self._track_mixers[track_id].set_mute(False)`。
  - 置信度: 高

- [AUD-37623] [Major] `register_track` 不应用当前 `_narrow_layout`，窄布局下新增 strip 以宽模式注册
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:178-209（对照 set_narrow_layout 507-527）
  - 证据:
    ```python
        mixer._update_size_constraints()
        # 强制使用与 Master 相同的固定宽度
        mixer.setFixedWidth(self._uniform_strip_width)
    ```
  - 影响: `set_narrow_layout(True)` 只遍历既有 mixer 调 `_apply_layout_mode`（515-518 行）；此后 `sync_tracks`/`register_track` 新增的 strip 仍按面板初始化时缓存的宽模式 `_uniform_strip_width`（127 行）固定宽度、且不隐藏 balance/spinbox（未调 set_narrow），在窄布局流中呈现为一条异常宽的完整 strip，宽度与相邻轨道不一致。
  - 建议: register_track 末尾按 `self._narrow_layout` 调用 `_apply_layout_mode(mixer, self._narrow_layout)`（setFixedWidth 统一宽度逻辑需与窄宽 70px 协调）。
  - 置信度: 高

- [AUD-37624] [Major] `sync_tracks` 静默回填 solo/mute 不维护 `_solo_tracks`/`_solo_muted` 记账，undo/外部恢复 solo 后面板状态机脱节
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:599-602（记账仅在 set_track_solo 327-381 维护）
  - 证据:
    ```python
            if mixer.is_mute() != new_muted:
                mixer.set_mute(new_muted)
            if getattr(mixer, '_is_solo', False) != new_solo:
                mixer.set_solo(new_solo)
    ```
  - 影响: `mixer.set_solo` 是 blockSignals 静默回填（track_mixer_widget.py:623-629），不会回流 `_on_solo_toggled`。undo/redo 或工程加载恢复 solo 状态后：`_solo_tracks` 仍为空 → 右键菜单 "Clear All Solo" 被禁用（303 行 `bool(self._solo_tracks)`）尽管界面上有 solo 亮起；后续 `set_track_solo` 的 `len(self._solo_tracks) > 1` 分支（334 行）判断错误，恢复/联动静音行为偏离预期。
  - 建议: sync_tracks 回填 solo 后重建 `_solo_tracks`（= 实际 is_solo 的轨道集合），并同步校正 `_solo_muted`。
  - 置信度: 高

- [AUD-37625] [Major] solo 激活期间 sync_tracks 用模型 is_muted=False 复位被联动静音的 strip，显示状态与实际听感相反
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:590-600（联动静音来源 set_track_solo 373-377）
  - 证据:
    ```python
            new_muted = bool(t.get("is_muted", False))
            new_solo = bool(t.get("is_solo", False))
            if abs(mixer.get_volume_linear() - new_vol) > 1e-6:
                mixer.set_volume_linear(new_vol)
            ...
            if mixer.is_mute() != new_muted:
                mixer.set_mute(new_muted)
    ```
  - 影响: solo 的联动静音是纯 UI 派生态（set_mute 静默，不写模型，模型仅存 is_solo）。solo 激活期间任何触发 sync_tracks 的 timeline 变化（加片段、undo 等）都会把被联动静音的 strip 按模型值复位为“未静音”——引擎仍按 solo 只出独奏轨，但界面上该轨 M 按钮熄灭，用户误以为它在发声。
  - 建议: sync_tracks 中对 `tid in self._solo_muted` 且仍有 solo 激活的轨道跳过 mute 回填（或在有 solo 时按派生规则计算显示态）。
  - 置信度: 中

- [AUD-37626] [Major] `collapse_mixers` 对嵌套布局中的 widget 调 `setAlignment`，按 Qt 语义为无效 no-op，折叠居中不生效
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:460-464（布局结构 84-130）
  - 证据:
    ```python
        if collapse:
            # 折叠时居中显示主混音器
            self.layout().setAlignment(self._master_mixer, Qt.AlignmentFlag.AlignHCenter)
        else:
            self.layout().setAlignment(self._master_mixer, Qt.AlignmentFlag.Alignment(0))
    ```
  - 影响: `_master_mixer` 位于嵌套的 `master_container`（QHBoxLayout）内（115-130 行经 `main_layout.addLayout(master_container)` 挂载），而 `QLayout::setAlignment(QWidget*)` 只匹配布局直接子项（“not including child layouts”），找不到时返回 False 且不生效——“折叠时居中显示主混音器”功能实际无效。
  - 建议: 对 `master_container`（或其外层容器）设置对齐，或将 master 直接 addWidget 到 main_layout。
  - 置信度: 中（基于 Qt 文档语义，未运行验证）

- [AUD-37627] [Suggestion] `deregister_track` 不清理 `_hidden_track_ids` 与 `_armed_track`，残留过期 id
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:211-236（对照 clear_all_tracks 238-248 有清理）
  - 证据:
    ```python
        del self._track_mixers[track_id]
        self._solo_tracks = [t for t in self._solo_tracks if t != track_id]
        self._solo_muted = [t for t in self._solo_muted if t != track_id]
    ```
  - 影响: 已删轨道的 id 永久留在 `_hidden_track_ids`（`get_hidden_track_ids()` 供 controller 持久化，越积越多）；undo 删除后同 id 重注册时 strip 可见但 `is_track_hidden()` 为 True，状态自相矛盾。armed 轨道被删后 `get_armed_track()` 返回悬空 id。
  - 建议: deregister_track 中 `self._hidden_track_ids.discard(track_id)`，并在 track_id == `_armed_track` 时清空。
  - 置信度: 高

- [AUD-37628] [Suggestion] 大量跨模块私有成员访问，破坏封装边界
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:125, 187, 259, 424, 566, 588, 601, 642, 655
  - 证据: `self._master_mixer._update_size_constraints()`（125）/ `mixer._update_size_constraints()`（187）/ `getattr(mixer, '_is_solo', False)`（259, 601）/ `meter = getattr(mixer, "_meter", None)`（424, 642, 655）/ `m._track_tag`（566）/ `mixer._track_name`（588）
  - 影响: panel 与 TrackMixerWidget/AudioLevelMeter 的私有实现强耦合，重构任一内部字段即静默失效（getattr 默认值掩盖断裂，如 `_meter` 改名后 LUFS 汇总静默变 False）。
  - 建议: 在子控件上提供公开访问器（is_solo()/track_tag/meter_mode 属性），panel 只走公共 API。
  - 置信度: 高

- [AUD-37629] [Suggestion] 公共方法参数类型注解缺失/不完整
  - 位置: src/shared/ui/audio_mixer/audio_mixer_panel.py:294, 405, 539
  - 证据: `def _show_batch_menu(self, pos) -> None:` / `def update_per_track_levels(self, levels: dict) -> None:` / `def sync_tracks(self, tracks: list) -> None:`
  - 影响: `levels: dict`/`tracks: list` 未参数化键值类型（docstring 里有结构说明但类型系统不可见），`pos` 缺 QPoint 注解，不符合仓库强类型要求。
  - 建议: 补齐 `dict[str, dict[str, list[float]]]`、`list[dict[str, Any]]`、`QPoint` 等精确注解。
  - 置信度: 高

### 通过项
- 线程安全：全部操作在 UI 线程；电平更新经 Qt 信号排队送达，无 UI 线程外访问。
- GPU 资源生命周期：无 OpenGL 资源；deregister_track 对 mixer/separator 统一 deleteLater，无 widget 泄漏。
- 信号防递归：master/track 回填走 `set_volume_linear`/`set_mute`/`set_solo`（blockSignals 静默 setter，track_mixer_widget.py:612-668 验证），不会回流命令栈；批量操作（clear_all_solo/mute_all）经专用 `batch_*` 信号一次性合成，无逐轨 toggled 回环。
- 状态提交一致性（提交通道）：volume/balance 拖动 preview、松手 commit 的三段式在 panel 侧仅做信号转发，语义与 main_window/edit_controller 的连接注释一致；strip_eq/effects 的 preview/commit 双通道转发完整。
- 异常与取消路径：clear_all_tracks/deregister_track 幂等（不存在即返回）；`clear_all_solo` 对 `_track_mixers` 遍历中仅调静默 setter，无中途删除风险。
- LUFS 消费汇总：`_recompute_lufs_metering` 仅状态翻转时 emit（660-662 行），注册/注销/模式变化三个入口均触发重算。
- 布局一致性：交替背景色在注册/注销后重算（692-696 行）；strip 统一宽度策略（_uniform_strip_width）在宽模式下成立（窄模式问题见 AUD-37623）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 0 | — |
| Major | 9 | AUD-37601, 37602, 37611, 37621, 37622, 37623, 37624, 37625, 37626 |
| Suggestion | 12 | AUD-37600, 37603, 37604, 37605, 37612, 37613, 37614, 37615, 37616, 37627, 37628, 37629 |
