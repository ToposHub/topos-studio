# 批次报告：batch-381 — src/shared/ui

- 批次: batch-381 [P2]
- 编号区间: AUD-38100–AUD-38199
- 审查文件: shortcut_scope.py / style_sheet.py / video_control_panel.py（3 个，2,130 行，全部通读）
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/shared/ui/shortcut_scope.py (行数: 83)

### 发现

（无发现）

### 通过项

- 线程安全：两个辅助函数仅查询 QApplication.activeWindow()/topLevelWidgets()，按设计只在 GUI 线程的快捷键处理器中调用，无共享可变状态
- GPU 资源生命周期：不涉及 OpenGL 资源
- GLSL·HDR 正确性：不涉及
- 信号防递归：无信号定义
- 状态提交一致性：不涉及 undo/redo/持久化
- 异常与取消路径：无 IO/外部调用，无宽泛 except
- 输入验证：objectName + isVisible + isActiveWindow 多重过滤逻辑正确，空结果返回 None 符合契约
- 性能模式：topLevelWidgets() 遍历仅在 Cmd 级命令触发一次，开销可忽略
- 类型注解：`shortcut_owner_window_active(widget: QWidget) -> bool`、`active_motion_graphic_editor() -> Optional[QWidget]` 均完整
- 模块 docstring 详细记录了 macOS Tool 窗口快捷键抢占陷阱及规避规则，与实现一致

---

## src/shared/ui/style_sheet.py (行数: 1,826)

### 发现

- [AUD-38110] [Suggestion] C 颜色常量承诺"修改此处即可全局生效"，但最大的消费方 DARK_THEME 等模板全部硬编码十六进制色值未引用 C
  - 位置: src/shared/ui/style_sheet.py:26-34（承诺），1028-1031、1241-1248（违反），1667-1722（TAB_STYLE/LAYER_LIST_STYLE 同样硬编码）
  - 证据:
    ```python
    # ============================================================================
    # 统一颜色常量 — 修改此处的颜色值即可全局生效
    # ============================================================================
    class C:
        """
        全局 UI 颜色常量集中定义。
        所有面板、控件的颜色均引用此类属性，修改此处即可统一调整配色。
        """
    ```
    而 DARK_THEME 内：
    ```css
    QWidget {
        background-color: #27272b;  /* 面板背景色（比主窗口稍浅） */
    ```
    ```css
    QSlider::handle:horizontal {
        background: #cc6600;         /* 与选中标签背景一致 */
    ```
    （#27272b == C.BG_PANEL、#cc6600 == C.ORANGE_PRESS/ACCENT_SURFACE，但为字面量复制）
  - 影响: 修改 C.BG_PANEL / C.ACCENT_SURFACE 等常量不会作用于全局主样式表，主题色随后续调整分叉漂移；同一颜色在两处维护
  - 建议: DARK_THEME 顶部模板化（如保留 __PLACEHOLDER__ 再 replace，或改为惰性函数/f-string 引用 C），使全局 QSS 与 C 常量单源化；短期内至少在 C 注释中明确 DARK_THEME 不受其控制
  - 置信度: 高

- [AUD-38111] [Suggestion] checkmark 图标路径在 import 期解析但未校验文件存在，缺失时静默降级
  - 位置: src/shared/ui/style_sheet.py:16-23、1443、1661
  - 证据:
    ```python
    _CHECKMARK_ICON_FILE = (
        Path(__file__).resolve().parents[1] / "assets" / "icons" / "checkmark.svg"
    )
    ...
    _CHECKMARK_ICON_PATH = _CHECKMARK_ICON_FILE.resolve().as_posix()
    ...
    DARK_THEME = DARK_THEME.replace("__CHECKMARK_ICON_URL__", _CHECKMARK_ICON_PATH)
    ```
  - 影响: 打包（PyInstaller 资源布局变动）或目录重组后若 svg 缺失，Qt QSS 对 url() 失效仅打印 "Could not parse stylesheet"/不渲染图标——所有复选框选中态失去勾选标记，无任何显式告警，排查成本高（当前仓库内该文件存在，属预防性缺口）
  - 建议: import 期增加 `_CHECKMARK_ICON_FILE.exists()` 检查，缺失时 log warning 并回退到无 image 的纯色方案，避免静默失效
  - 置信度: 高（无校验为事实；触发条件为部署布局变化）

### 通过项

- 线程安全：模块仅含 import 期求值的常量与纯 UI 工具函数，无共享可变状态；apply_combo_dropdown_style 仅在 GUI 线程使用
- GPU 资源生命周期：不涉及 OpenGL/ModernGL 资源
- GLSL·HDR 正确性：不涉及
- 信号防递归：无信号定义
- 状态提交一致性：不涉及 undo/redo/revision/持久化
- 异常与取消路径：无 except 块、无 IO 阻塞；QSS 解析容错由 Qt 承担
- 输入验证：curve_channel_btn_style/gradient_slider_groove_style 的参数仅做 f-string 插值不做二次解析，无 QSS 注入面；apply_combo_dropdown_style 参数类型固定为 QComboBox
- 性能模式：DARK_THEME 的两处 placeholder replace 为 import 期一次性 O(n) 字符串操作，运行时零开销；apply_combo_dropdown_style 的 setView/palette 操作为一次性构造期调用
- 类型注解：curve_channel_btn_style(channel_color: str) -> str、gradient_slider_groove_style(gradient_qss: str) -> str、apply_combo_dropdown_style(combo: QComboBox) -> None 均完整
- checkmark 路径注入的正斜杠处理正确（规避 Windows 反斜杠转义问题，注释记录了 as_uri() 不可用的实测原因）；svg 文件已确认存在
- QComboBox 弹出视图的 wrapper 保留（combo._topos_combo_view）有明确注释说明 deferred teardown 防崩溃动机

---

## src/shared/ui/video_control_panel.py (行数: 221)

### 发现

- [AUD-38100] [Suggestion] 类文档声明的信号契约与实际 pyqtSignal 声明不一致（frame_changed 不存在、play_toggled 缺 bool 参数）
  - 位置: src/shared/ui/video_control_panel.py:20-26 与 29-34
  - 证据:
    ```python
    """
    信号:
        frame_changed(int) - 当前帧变化
        seek_requested(int) - 请求跳转到指定帧
        play_toggled(bool) - 播放/暂停切换
        next_frame() - 下一帧
        prev_frame() - 上一帧
    """
    ```
    实际声明：
    ```python
    seek_requested = pyqtSignal(int)
    play_toggled = pyqtSignal()
    next_frame = pyqtSignal()
    prev_frame = pyqtSignal()
    first_frame = pyqtSignal()
    last_frame = pyqtSignal()
    ```
  - 影响: 按文档接线的调用方 `panel.frame_changed.connect(...)` 直接 AttributeError；`play_toggled` 按文档连接带 bool 参数的槽则签名不符；first_frame/last_frame 无文档。当前模块 DORMANT（文件头标注无生产入口），风险为潜伏型
  - 建议: 修正 docstring 与实际信号对齐（补 first_frame/last_frame、去掉 frame_changed 或补实现、play_toggled 标注无参）
  - 置信度: 高

- [AUD-38101] [Suggestion] 滑块拖动预览硬编码中文 "帧:" 文案，绕过 I18N
  - 位置: src/shared/ui/video_control_panel.py:202（对照 187）
  - 证据:
    ```python
    def _on_slider_changed(self, value: int):
        """滑块值变化"""
        if self._slider_dragging:
            self.label_frame.setText(f"帧: {value + 1} / {max(1, self.total_frames)}")
    ```
    而 `_update_display` 走翻译：
    ```python
    frame_text = I18N.tr("video.frame_count", f"帧: {self.current_frame + 1} / {max(1, self.total_frames)}")
    ```
  - 影响: 非中文语言环境下拖动滑块时帧号标签闪回中文，松手后才恢复翻译文案
  - 建议: _on_slider_changed 复用 I18N.tr("video.frame_count", ...) 构造文案
  - 置信度: 高

- [AUD-38102] [Suggestion] set_total_frames / set_current_frame 不做输入验证，负值与越界帧直接进入状态
  - 位置: src/shared/ui/video_control_panel.py:158-171
  - 证据:
    ```python
    def set_total_frames(self, total: int):
        """设置总帧数"""
        self.total_frames = total
        self.slider.setMaximum(max(0, total - 1))

    def set_current_frame(self, frame: int):
        """设置当前帧"""
        self.current_frame = frame
    ```
  - 影响: `set_total_frames(-5)` 将 -5 存入 self.total_frames（仅 slider 一侧被 clamp）；`set_current_frame(-3)` 显示"帧: -2 / N"，与 .clinerules"外部数据须验证"不符（控件对外接口应防御）
  - 建议: 入口 clamp：`self.total_frames = max(0, total)`、`self.current_frame = max(0, min(frame, max(0, self.total_frames - 1)))`
  - 置信度: 高

- [AUD-38103] [Suggestion] 未使用的导入 QStyle / QFrame / QIcon
  - 位置: src/shared/ui/video_control_panel.py:7-12
  - 证据:
    ```python
    from PyQt6.QtWidgets import (
        QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
        QSlider, QLabel, QStyle, QToolButton, QFrame
    )
    from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
    from PyQt6.QtGui import QIcon
    ```
  - 影响: QStyle、QFrame、QIcon 在文件内无任何引用，增加阅读噪音与 lint 负担
  - 建议: 删除三个未用导入
  - 置信度: 高

- [AUD-38104] [Suggestion] __init__ 的 parent 参数缺类型注解
  - 位置: src/shared/ui/video_control_panel.py:36
  - 证据:
    ```python
    def __init__(self, parent=None):
        super().__init__(parent)
    ```
  - 影响: 其余方法均有注解，此处违反项目强类型约束（.clinerules / AGENTS.md 要求所有函数有类型注解）
  - 建议: `def __init__(self, parent: Optional[QWidget] = None) -> None:` 并导入 Optional
  - 置信度: 高

### 通过项

- 线程安全：纯 QWidget 子类，无跨线程访问、无渲染线程交互
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：不涉及
- 信号防递归：set_current_frame 程序化更新 slider 时正确使用 blockSignals(True/False) 阻断 valueChanged 回环（167-170 行）；set_total_frames 触发的 valueChanged 在非拖动态下为 no-op
- 状态提交一致性：不涉及 undo/redo/revision/持久化（seek 交由外部控制器回填，单向数据流）
- 异常与取消路径：无 except 块、无 IO；无异常吞没
- 性能模式：更新均为单控件 setText/setValue，无热路径问题
- Dormant 标注清晰（文件头 2026-08-19 审计备注），当前无生产入口（src/ 内无外部引用，已 grep 确认）
