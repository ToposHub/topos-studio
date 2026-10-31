# 批次报告：batch-246 — src/features/color/（模块注册 + 偏好页）

审查范围：`src/features/color/module.py`、`src/features/color/preferences.py`
编号区间：AUD-24600–AUD-24699
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/color/module.py (行数: 560)

### 发现

- [AUD-24601] [Major] 默认快捷键 Ctrl+Shift+S / Ctrl+Shift+D 与其他命令冲突
  - 位置: src/features/color/module.py:221（另见 :255）
  - 证据:
    ```python
    shortcut_manager.register(
        "color.capture_still",
        I18N.tr("action.capture_still", "Capture Still"),
        "Color",
        "Ctrl+Shift+S",
        feature_id="color",
    )
    ```
    同时 `src/app/controllers/menu_toolbar_controller.py:528` 注册 `"file.save_as"` 默认 `Ctrl+Shift+S`，`src/features/color/ui/canvas.py:2250` 又对 `capture_still_act.setShortcut("Ctrl+Shift+S")`；`Ctrl+Shift+D` 同样被 `canvas.py:2227` 的 `capture_reference_act` 占用。
  - 影响: 同一键序被 Save Project As / Capture Still（及 Toggle Comparison / Capture Reference）多方声明，快捷键目录内自相冲突；窗口内多个 QAction 同序时触发 Qt 歧义快捷键，按 Ctrl+Shift+S 可能执行“另存工程”或无响应，而非 Capture Still。
  - 建议: 为 color.capture_still / color.toggle_comparison 选择独占默认键序（如 Ctrl+Alt+S / Ctrl+Alt+D），并在 ShortcutManager 注册时检测重复键序。
  - 置信度: 中（目录级重复确定无疑；运行时实际歧义取决于各 QAction 的 shortcutContext）

- [AUD-24602] [Suggestion] get_facade/initialize_facade 为死代码，且 initialize_facade 无条件覆盖已发出的 facade
  - 位置: src/features/color/module.py:113-125、552-560
  - 证据:
    ```python
    def get_facade(self, main_window: Any) -> ColorFacade:
        if self._facade is None:
            self._facade = ColorFacade(main_window)
        return self._facade

    def initialize_facade(self, main_window: Any) -> None:
        self._facade = ColorFacade(main_window)
    ```
    全库检索无任何调用方；`src/app/main_window.py:579-580` 直接 `self.color_facade = ColorFacade(self)` 创建。
  - 影响: 死注册路径；若未来被使用，`initialize_facade` 会直接替换 `_facade`，此前经 `get_facade` 拿到旧 facade 的调用方将持有指向旧 MainWindow 的过期实例。
  - 建议: 删除这两个方法或让 `initialize_facade` 复用 `get_facade` 语义，并让 MainWindow 统一走模块入口。
  - 置信度: 高

- [AUD-24603] [Suggestion] FeatureModule 导入未使用
  - 位置: src/features/color/module.py:16
  - 证据:
    ```python
    from src.app.page_contract import PageDescriptor, FeatureModule
    ```
    文件其余处仅 `register_pages` 的返回类型用到 `PageDescriptor`，`FeatureModule` 无任何引用（ColorModule 对协议是结构化匹配，无需显式继承）。
  - 影响: 死导入；lint 噪音。
  - 建议: 移除 `FeatureModule` 或改为 `if TYPE_CHECKING:` 下引用以显式声明协议关系。
  - 置信度: 高

- [AUD-24604] [Suggestion] 注册类方法参数缺类型注解
  - 位置: src/features/color/module.py:134、194、203、260
  - 证据:
    ```python
    def register_menu_actions(self, action_registry) -> None:
    def register_preference_pages(self):
    def register_shortcuts(self, shortcut_manager) -> None:
    def register_panels(self, panel_registry) -> None:
    ```
  - 影响: 与 `.clinerules` 强类型要求不符；调用方无法从签名得知期望的 registry 类型。
  - 建议: 标注为 `ActionRegistry`、`ShortcutManager`、`PanelRegistry` 及 `List[ModularPreferencePage]`（可用 TYPE_CHECKING 避免循环导入），补 `register_preference_pages` 返回注解。
  - 置信度: 高

### 通过项
- 线程安全：全部为 UI 线程启动期的注册逻辑，无 OpenGL/跨线程访问
- GPU 资源生命周期：不创建/持有任何 ModernGL 对象
- GLSL·HDR 正确性：无着色器代码
- 信号防递归：无信号连接
- 状态提交一致性：不写配置/工程状态，仅注册描述符
- 异常与取消路径：无 IO；register_i18n 的 locales 目录实际存在（10 个语言文件）
- PanelDescriptor 调用字段（page_ids/dock_attr/work_mode/default_visible）与 `src/shared/panels/registry.py` 数据类定义一致，注册不会 TypeError；重复 panel_id 由 registry 防重
- register_pages 返回空列表、register_actions/register_save_handlers 为占位，与 MainWindow 直建 Color 页的注释一致，符合 FeatureModule 协议结构

---

## src/features/color/preferences.py (行数: 909)

### 发现

- [AUD-24611] [Major] 打开偏好页并点确定会把 image 范围键静默写成 "limited"，覆盖运行时 FULL 默认
  - 位置: src/features/color/preferences.py:646-660（load）、749-764（apply）、565-577（_set_combo_data）、378-386（_make_range_combo 首项 "limited"）
  - 证据:
    ```python
    # load(): 配置缺键 → _set_combo_data 直接返回，组合框停留 index 0 = "limited"
    self._set_combo_data(self._widgets[key], config.get(key))

    @staticmethod
    def _set_combo_data(combo: ChevronComboBox, value) -> None:
        if value is None:
            return

    # apply(): 无条件写回 12 个键
    config_manager.set(key, self._widgets[key].currentData(), auto_save=False)
    ```
    `src/shared/config/config_manager.py` 的 DEFAULT_CONFIG 仅定义 `"color.output.timeline_range": "limited"`（:192），`color.preview.timeline_range` / `color.preview.image_range` / `color.output.image_range` 三键无默认值；而运行时 `src/shared/color/color_space_descriptor.py` 对缺省 image 范围回退 `ColorRange.FULL if kind == "image"`。
  - 影响: 用户从未触碰该组控件，仅打开偏好对话框点 OK/Apply，apply() 即把两个 image 范围键持久化为 "limited"，图像预览/导出从全范围 0-255 变为视频合法范围 16-235，输出电平被静默压缩——违反“程序化更新不得隐式改变状态”的精神，属实际渲染输出差异。
  - 建议: apply() 只写回用户实际交互过的键；或 load() 对缺失键按运行时默认（image→full、timeline→limited）预选；并在 DEFAULT_CONFIG 补齐三个 range 键。
  - 置信度: 高

- [AUD-24612] [Major] load() 对外部配置值做未保护的 float()/int() 转换，坏值导致整页静默半途加载中断
  - 位置: src/features/color/preferences.py:622-636
  - 证据:
    ```python
    self._widgets[COLOR_WHEEL_ROTATION_CONFIG_KEY].setValue(
        float(config.get(
            COLOR_WHEEL_ROTATION_CONFIG_KEY,
            DEFAULT_COLOR_WHEEL_ROTATION_DEGREES,
        ))
    )
    self._widgets[TRACKERBALL_SMOOTHING_CONFIG_KEY].setValue(
        int(config.get(
            TRACKERBALL_SMOOTHING_CONFIG_KEY,
            DEFAULT_TRACKERBALL_SMOOTHING,
        ))
    )
    ```
    config.json 为用户可编辑外部文件；上层 `src/app/ui/preferences_dialog.py:1899-1902` 对 `page_obj.load(cfg)` 是 `except Exception: pass`。对照同页的 `tangent_sensitivity_to_display`（settings.py:82-88）自带 try/except 回退，说明项目已有防御惯例而此处缺失。
  - 影响: 这些键若为非数值（"abc" / null），load() 在此处抛 ValueError 被对话框吞掉，其后的一半控件（CMS 预设、图像处理、AI 组）全部保持构建默认值且无任何提示——设置显示值与实际配置不一致。
  - 建议: 复用 tangent settings 的防御式转换（try/except (TypeError, ValueError) 回退默认值）。
  - 置信度: 高

- [AUD-24613] [Suggestion] restore_defaults 混用直接索引与 .get，缺键即 KeyError
  - 位置: src/features/color/preferences.py:793、798、808-813
  - 证据:
    ```python
    idx = cs_combo.findText(defaults['default_color_space'])
    ...
    idx = bd_combo.findText(defaults['default_bit_depth'])
    ...
    self._widgets['gpu_color_processing'].setChecked(
        defaults['gpu_color_processing']
    )
    self._widgets['show_color_space_warnings'].setChecked(
        defaults['show_color_space_warnings']
    )
    ```
    同函数其余键均为 `defaults.get(...)`（如 :815 `defaults.get('auto_assign_icc', True)`）。
  - 影响: 当前 DEFAULT_CONFIG 恰好含这四个键，但任何键被移除/重命名后“恢复默认”在此抛 KeyError（同样被对话框吞掉，页面复位半途中断）。
  - 建议: 统一改为 `.get(key, fallback)`。
  - 置信度: 高

- [AUD-24614] [Suggestion] _dialog_ref 注释称弱引用，实为强引用，与对话框互持成环
  - 位置: src/features/color/preferences.py:80、86
  - 证据:
    ```python
    self._dialog_ref: Any = None  # Weak reference to PreferencesDialog for _browse_path
    ...
    def create_widget(self, parent) -> QWidget:
        self._dialog_ref = parent
    ```
    对话框侧 `preferences_dialog.py:99/196` `self._module_pages[pid] = page_obj` 强持有页面。
  - 影响: 并非 `weakref.ref`，页面↔对话框形成 Python 引用环，依赖循环 GC 回收；注释误导后续维护者以为是弱引用而放心持有。
  - 建议: 改用 `weakref.ref(parent)` 并在 `_browse_path` 解引用，或至少更正注释。
  - 置信度: 高

- [AUD-24615] [Suggestion] _make_transfer_combo 的 default 参数从未使用
  - 位置: src/features/color/preferences.py:352-361（定义）、402-405（调用）
  - 证据:
    ```python
    def _make_transfer_combo(key: str, default: str) -> ChevronComboBox:
        combo = ChevronComboBox()
        combo.setMinimumWidth(120)
        combo.addItem(..., "iec61966_2_1")
        ...
        return combo
    # 调用方：
    _make_transfer_combo(
        f"color.{scope}.{kind}_transfer",
        "bt709" if kind == "timeline" else "iec61966_2_1",
    )
    ```
    函数体从未引用 `default`。
  - 影响: “timeline 默认 Rec.709”的预选意图从未生效（目前靠 DEFAULT_CONFIG 兜底掩盖，与 AUD-24611 属同类根因）；死参数误导。
  - 建议: 在函数内按 default 预选 currentIndex，或删除该参数。
  - 置信度: 高

- [AUD-24616] [Suggestion] 文件选择对话框标题硬编码英文、OCIO 选择器不过滤文件类型
  - 位置: src/features/color/preferences.py:184、512、899-903
  - 证据:
    ```python
    lambda: self._browse_path(ocio_edit, "Select OCIO Config", is_file=True)
    lambda: self._browse_path(ai_path_edit, "Select AI Model Directory")
    ...
    path, _ = QFileDialog.getOpenFileName(
        self._dialog_ref, title, current if current else "",
        "All Files (*)"
    )
    ```
  - 影响: 全页其余文案均走 I18N.tr，这两处标题不随语言切换；OCIO 配置选择器接受任意文件，缺少 .ocio/.json 过滤，选错文件无提示。
  - 建议: 标题包 I18N.tr，过滤器改为 `"OCIO config (*.ocio *.ocio *.json);;All Files (*)"` 类似形式。
  - 置信度: 高

- [AUD-24617] [Suggestion] 跨模块导入 export_panel 的下划线私有样式函数
  - 位置: src/features/color/preferences.py:27-30
  - 证据:
    ```python
    from src.shared.export.export_panel import (
        _apply_text_input_style,
        _apply_compact_button_style,
    )
    ```
  - 影响: 偏好页依赖导出面板的私有 API（Python 下划线约定外的稳定接口缺失），export_panel 重构时易静默破坏偏好页样式。
  - 建议: 将两个样式函数提升到 `src/shared/ui/widgets/` 共享样式模块后双方引用。
  - 置信度: 高

- [AUD-24618] [Suggestion] 关键方法参数/返回缺类型注解
  - 位置: src/features/color/preferences.py:84（create_widget(self, parent)）、566（_set_combo_data 的 value）、690（apply(config_manager: Any)）
  - 证据:
    ```python
    def create_widget(self, parent) -> QWidget:
    @staticmethod
    def _set_combo_data(combo: ChevronComboBox, value) -> None:
    def apply(self, config_manager: Any) -> None:
    ```
  - 影响: 协议（ModularPreferencePage）对 parent 有 QWidget 约定，`Any`/缺注解削弱静态检查价值。
  - 建议: parent 标注 `"QWidget"`（文件已 `from __future__ import annotations`），value 标注 `object | None`，config_manager 可用 TYPE_CHECKING 下的 ConfigManager。
  - 置信度: 高

### 通过项
- 信号防递归：load()/restore_defaults() 的程序化 setChecked/setValue/setCurrentIndex 仅触发 `denoise_slider.valueChanged → 标签 setText` 类 UI lambda，无配置回写，无递归风险；apply() 为显式一次性提交
- 线程安全：全部 UI 线程操作，无 OpenGL 调用、无跨线程共享
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：传输函数/色域组合框取值（iec61966_2_1/bt709/linear/smpte2084/arib-std-b67；bt709/smpte432/bt2020）与 ColorTransfer/ColorPrimaries 枚举值一致，键名与运行时 `color.{scope}.{kind}_*` 读取一致
- Tangent 换算对称性：trackerball 走 to_display/from_display（10x 缩放）、brightness 走原值 int(round)，apply/load/restore_defaults 三处一致；转换函数自带坏值回退
- 状态提交一致性：apply() 全部 auto_save=False 由对话框统一 `cfg.save_config()`（preferences_dialog.py:2053-2060），符合批量提交模式（缺键写回问题已在 AUD-24611 记录）
- 异常与取消路径：_browse_path 取消（path 为空）正确不修改控件
- 输入验证（UI 侧）：QSpinBox/QDoubleSpinBox 范围由 setRange 约束，slider 越界值由 Qt 自动钳制；ocio/ai 路径无存在性验证（影响小，随 AUD-24616 一并改善）
- 性能：一次性构建，无逐帧/热路径代码

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-24601、AUD-24611、AUD-24612 |
| Suggestion | 9 | AUD-24602–24604、AUD-24613–24618 |
