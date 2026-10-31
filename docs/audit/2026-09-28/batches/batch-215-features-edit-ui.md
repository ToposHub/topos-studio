# 批次报告：batch-215 [P1] src/features/edit/ui//

- 审查日期：2026-09-28
- 编号区间：AUD-21500–AUD-21599（实际使用 AUD-21500–AUD-21508）
- 文件清单：
  - `src/features/edit/ui/trim_numeric_dialog.py` (202)
  - `src/features/edit/ui/tts_settings_dialog.py` (321)
- 交叉验证文件（只读，用于确认跨模块契约）：
  - `src/features/edit/ai_editing/tts_settings.py`（TtsSettings/BUILTIN_* 定义）
  - `src/shared/export/export_panel.py`（私有样式 helper 实现）
  - `src/features/edit/ui/timeline_view.py` 3900–4030（TrimNumericDialog/TrimMonitorDialog 调用方）
  - `src/app/controllers/edit_controller.py` 1248–1280（TtsSettingsDialog 调用方）

---

## src/features/edit/ui/trim_numeric_dialog.py (行数: 202)

### 发现

- [AUD-21500] [Major] `_refresh` 程序化修改 SpinBox 上下限可经 `valueChanged` 重入自身，无状态锁
  - 位置: src/features/edit/ui/trim_numeric_dialog.py:87-89, 109-115
  - 证据:
    ```python
    self._delta_spin.valueChanged.connect(lambda _v: self._refresh())   # :89
    ...
    def _refresh(self) -> None:
        low, high = self._bounds()
        roll = self.current_mode() == "roll"
        self._edge_combo.setEnabled(not roll)
        minimum, maximum = sorted((low, high))
        self._delta_spin.setMinimum(minimum)          # 钳取 value 时同步发 valueChanged
        self._delta_spin.setMaximum(max(maximum, minimum))
    ```
  - 影响: 切换 edge/mode 使当前 delta 落到新范围之外时，`setMinimum/setMaximum` 会钳取数值并同步发射 `valueChanged`，`_refresh` 被重入执行：调用方传入的 `bounds_for` 回调（timeline_view.py:3979-4004 中遍历 `track.clips` 并调用 `get_roll_trim_constraints`）被重复执行、三个标签被重复刷新。违反"程序化更新控件须状态锁防递归信号"铁律。重入会收敛（第二遍不再引起范围变化），不会死循环，但属于未加防护的信号回环。
  - 建议: 在 `_refresh` 入口加 `self._updating` 布尔锁提前返回，或在 `setMinimum/setMaximum` 前后对 `_delta_spin` 使用 `blockSignals(True/False)`。
  - 置信度: 高

- [AUD-21501] [Suggestion] 合法范围标签显示未排序的 low/high，与 SpinBox 实际范围（sorted）可能不一致
  - 位置: src/features/edit/ui/trim_numeric_dialog.py:113-118
  - 证据:
    ```python
    minimum, maximum = sorted((low, high))
    self._delta_spin.setMinimum(minimum)
    self._delta_spin.setMaximum(max(maximum, minimum))
    self._range_label.setText(I18N.tr(
        "edit.trim.range", "Valid range: {min} … {max} f",
    ).format(min=low, max=high))
    ```
  - 影响: 代码特意对 bounds 做了 `sorted` 与 `max(maximum, minimum)` 防御，说明调用方可能返回倒置对；此时标签仍显示原始 `low … high`（如 "Valid range: 20 … -20 f"），与 spin 实际允许范围相矛盾，误导用户。
  - 建议: 标签同样使用排序后的 `minimum/maximum`。
  - 置信度: 中

- [AUD-21502] [Suggestion] 结果时长用 `max(1, new_duration)` 掩盖非法值而非暴露
  - 位置: src/features/edit/ui/trim_numeric_dialog.py:124-128
  - 证据:
    ```python
    new_duration = self._duration - delta if self.current_edge() == "in" \
        else self._duration + delta
    result = I18N.tr("edit.trim.result_duration",
                     "New duration: {dur} f")
    result = result.format(dur=max(1, new_duration))
    ```
  - 影响: 若 bounds 计算异常或与 `duration_frames` 失配导致 delta 越界，界面显示 "New duration: 1 f" 而非暴露真实（可能为负）的数值，问题被静默吞掉。
  - 建议: 显示真实计算值，或在检测到 `new_duration < 1` 时输出明确的越界提示。
  - 置信度: 中

### 通过项
- 线程安全：纯 UI 线程控件，无 OpenGL/渲染线程交互
- GPU 资源生命周期：无 GPU 资源创建/持有
- GLSL·HDR 正确性：不涉及
- 状态提交一致性：对话框保持"哑"设计，trim 执行经调用方命令对象（`build_numeric_trim_command`，timeline_view.py:4029）进入 undo 体系，本文件无 undo/redo/持久化逻辑
- 异常与取消路径：Cancel/reject 直接返回不执行；`values()` 仅在 `DialogCode.Accepted` 后调用（timeline_view.py:4014-4016）
- 输入验证：delta 受 SpinBox 动态上下限硬约束；默认 bounds 回退 `(-99, 99)`
- TrimMonitorDialog：纯只读显示控件（`update_roll`/`update_trim` 仅 setText），无状态、无逻辑问题
- 类型注解：构造参数与公开方法均有完整类型注解
- i18n：全部用户可见文案经 `I18N.tr` 且带默认串

---

## src/features/edit/ui/tts_settings_dialog.py (行数: 321)

### 发现

- [AUD-21503] [Major] 对 `QTextEdit` 调用 `QLineEdit` 专用样式函数，样式完全不生效（静默失效）
  - 位置: src/features/edit/ui/tts_settings_dialog.py:150, 180；src/shared/export/export_panel.py:260-279
  - 证据:
    ```python
    # tts_settings_dialog.py:143-144, 150
    self._instruct_edit = QTextEdit()
    ...
    _apply_text_input_style(self._instruct_edit)      # :150
    # tts_settings_dialog.py:174, 180
    self._ref_text_edit = QTextEdit()
    ...
    _apply_text_input_style(self._ref_text_edit)      # :180
    ```
    而 export_panel.py:260-279 的实现全部使用 `QLineEdit` 类型选择器：
    ```python
    def _apply_text_input_style(line_edit: QLineEdit) -> None:
        line_edit.setStyleSheet("""
            QLineEdit { background-color: #232325; ... }
            QLineEdit:hover { ... }
            QLineEdit:focus { ... }
        """)
    ```
  - 影响: Qt 类型选择器 `QLineEdit {...}` 不匹配 `QTextEdit`（非其子类），setStyleSheet 成为静默 no-op——"语气指令"与"参考文本"两个多行输入框完全没拿到预期的背景/边框/hover/focus 统一视觉，与同对话框的 QLineEdit 参考音频路径框（:165，样式生效）形成明显不一致；同时违反 helper 的 `line_edit: QLineEdit` 签名契约。
  - 建议: 为多行输入扩展选择器（`QLineEdit, QTextEdit {...}`）或新增 `_apply_multiline_input_style`；helper 参数类型放宽为 `QLineEdit | QTextEdit`。
  - 置信度: 高

- [AUD-21504] [Major] 参考音频路径仅做非空校验，不验证文件存在性（外部数据未验证）
  - 位置: src/features/edit/ui/tts_settings_dialog.py:276-287
  - 证据:
    ```python
    def _on_accept(self) -> None:
        """校验输入后 accept。"""
        # 音色克隆模式校验参考音频
        if self._tabs.currentIndex() == 1:
            ref_path = self._ref_audio_edit.text().strip()
            if not ref_path:
                self._ref_audio_edit.setFocus()
                return
            if not self._xvector_check.isChecked() and not self._ref_text_edit.toPlainText().strip():
                self._ref_text_edit.setFocus()
                return
        self.accept()
    ```
  - 影响: 路径框支持手动键入（browse 之外的任意字符串），校验只查非空与（非 x_vector 时）ref_text 非空，不检查 `os.path.isfile(ref_path)` 也不检查扩展名/格式。不存在的路径会原样进入 `TtsSettings.ref_audio_path`，经 controller → worker 透传到 TTS 引擎，直到模型加载/合成阶段才失败——错误远离源头，浪费一次长耗时任务，违反"外部数据须验证"铁律。
  - 建议: `_on_accept` 中增加 `os.path.isfile(ref_path)`（可加后缀白名单 wav/mp3/flac/ogg）校验，失败时提示并阻止 accept。
  - 置信度: 高

- [AUD-21505] [Suggestion] 校验失败静默 return，用户无任何提示
  - 位置: src/features/edit/ui/tts_settings_dialog.py:280-286
  - 证据:
    ```python
    if not ref_path:
        self._ref_audio_edit.setFocus()
        return
    if not self._xvector_check.isChecked() and not self._ref_text_edit.toPlainText().strip():
        self._ref_text_edit.setFocus()
        return
    ```
  - 影响: 点击"确定"后对话框仅聚焦对应控件并保持打开，无消息/占位变红/statusTip 解释原因，用户可能误以为按钮失效。
  - 建议: 用消息提示、红色边框或字段旁错误标签说明"参考音频必填"/"非声纹模式需参考文本"。
  - 置信度: 高

- [AUD-21506] [Suggestion] 跨模块导入 export_panel 的私有下划线符号
  - 位置: src/features/edit/ui/tts_settings_dialog.py:41-45
  - 证据:
    ```python
    from src.shared.export.export_panel import (
        _apply_compact_button_style,
        _apply_text_input_style,
        _make_param_label,
    )
    ```
  - 影响: 三个符号均为 `export_panel` 的模块私有（下划线约定，docstring 也表明为该面板自用），features 层复用私有实现形成脆弱耦合：export_panel 重构/更名时此处静默破坏（ImportError 在运行期弹出对话框时才暴露）。
  - 建议: 将这三个通用 UI 样式 helper 提升为 shared UI 工具模块的公开 API，两个面板共同引用。
  - 置信度: 高

- [AUD-21507] [Suggestion] 两个语言下拉（内置音色 tab / 音色克隆 tab）互不同步，另一 tab 的选择被静默丢弃
  - 位置: src/features/edit/ui/tts_settings_dialog.py:133-140, 191-198, 295-298
  - 证据:
    ```python
    # 两个独立下拉，各自仅初始化时同步 self._settings.language
    self._lang_combo = QComboBox()          # :133（内置音色 tab）
    self._lang_clone_combo = QComboBox()    # :191（音色克隆 tab）
    ...
    # get_settings 只读当前激活 tab 的下拉：
    lang_combo = self._lang_clone_combo if is_clone else self._lang_combo
    language = lang_combo.currentData() or "Auto"
    ```
  - 影响: 用户在"内置音色"tab 选了"中文"后切到"音色克隆"tab 确认，语言回落为克隆 tab 下拉的独立状态（初始 "Auto"），先前选择被静默丢弃；反向同理。两处无信号联动。
  - 建议: 两个下拉 `currentIndexChanged` 互相同步（带防递归锁），或语言改为 tab 外的共享控件。
  - 置信度: 中

- [AUD-21508] [Suggestion] 构造参数 `parent` 类型注解为 `Any`
  - 位置: src/features/edit/ui/tts_settings_dialog.py:56-58
  - 证据:
    ```python
    def __init__(
        self,
        parent: Any = None,
    ```
  - 影响: 实际传入 `QWidget` 型父窗口（edit_controller.py:1273 传 `self`），注解过宽使类型检查失去保护，与项目"强类型"规范不符。
  - 建议: 改为 `parent: Optional[QWidget] = None` 并导入对应类型。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程对话框，无 OpenGL/跨线程访问（TTS 合成在 controller/worker 侧执行）
- GPU 资源生命周期：无 GPU 资源
- GLSL·HDR 正确性：不涉及
- 信号防递归：滑块 `valueChanged` 在 `setValue` 之后才连接（:242 与 :250 的顺序），无程序化更新回环；无 comboboxIndexChanged 业务逻辑连接
- 状态提交一致性：设置经 `get_settings()` 在 accept 后一次性取回（edit_controller.py:1278-1280），由 controller 生成可 undo 的导入命令，本文件无持久化/undo 逻辑
- 数值映射一致性：temperature 滑块 scale=10、top_p scale=100，与 `get_settings` 中 `/10`、`/100` 换算一致（:207-208, 212-213, 307-308）
- 输入数值范围：top_k 1–200、max_new_tokens 256–8192、temperature 0.1–20.0、top_p 0.01–1.00 均有硬约束
- 回退值有效性：`or "Vivian"`/`or "Auto"` 回退值与 `TtsSettings` 默认值及 BUILTIN 清单首项一致（tts_settings.py:31, 49, 80, 86），组合框恒非空，回退仅防御性
- i18n：用户可见文案均经 `I18N.tr`；文件路径过滤器文案亦本地化
