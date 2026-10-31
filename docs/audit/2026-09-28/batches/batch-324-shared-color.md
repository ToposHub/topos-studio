# 批次报告：batch-324 — src/shared/color//

编号区间：AUD-32400–AUD-32499。审查日期：2026-09-28。
文件清单：`src/shared/color/__init__.py` (53)、`src/shared/color/color_conversion.py` (39)、`src/shared/color/color_picker.py` (2,789)。
本包其余文件（types.py、color_space_descriptor.py 等）不在本批次范围内。

---

## src/shared/color/__init__.py (行数: 53)

### 发现

- [AUD-32400] [Major] 注释声称"延迟导入"color_picker，实际为顶层立即导入，未实现解耦目标
  - 位置: src/shared/color/__init__.py:26-32
  - 证据:
    ```python
    # 统一颜色选择器（CompactColorStudio）及其公共 API。
    # 延迟导入避免在只用到 color types 的模块里加载 PyQt6 UI 组件。
    from .color_picker import (
        CompactColorStudio,
        EyeDropperOverlay,
        get_color,
    )
    ```
  - 影响: 注释宣称的延迟加载并未实现——这是无任何条件的顶层 import，且包内无 `__getattr__`/TYPE_CHECKING 延迟机制。导入包的任何子模块都会先执行本 `__init__.py`（Python 先导入父包），因此 `from src.shared.color.types import ColorTransfer`（见 src/app/controllers/page_controller.py:824、src/features/composite/render/composite_render_output.py:365 等渲染侧调用点）会把 QtWidgets + QtSvg 全套 UI 栈拖进进程，并触发 color_picker 模块级副作用（`_get_user_config_dir()` 环境探测、`CONFIG_FILE_PATH` 构造）。违背 shared 层与 UI 依赖解耦的既定设计意图，增加导入重量与潜在循环导入风险。
  - 建议: 改为模块级 `__getattr__`（PEP 562）按需加载 `CompactColorStudio`/`EyeDropperOverlay`/`get_color`，或至少删除与实现相反的注释；调用方目前多用函数内延迟导入 `from src.shared.color import get_color`，说明该解耦确有需求。
  - 置信度: 高

### 通过项
- 纯 re-export 模块，`__all__` 与实际导出（types 枚举/映射、color_conversion 两函数、color_picker 三符号）一致，无遗漏无多余。
- 导出符号均为真实存在的名称（已对照三个子模块），无导入错误风险。
- 线程安全/GPU 资源/信号防递归等维度不适用于本文件（无逻辑）。

---

## src/shared/color/color_conversion.py (行数: 39)

### 发现
（无发现）

### 通过项
- 通过（无逻辑风险）。纯函数模块，`srgb_to_linear`/`linear_to_srgb` 数学正确（阈值 0.04045/0.0031308、指数 2.4 均正确）。
- `linear_to_srgb`/`srgb_to_linear` 对输入 clamp 到 0..1 属于显示值域转换，模块 docstring 明确说明用途（Qt 控件/hex 为 display-referred），符合"HDR 内部数据不得无理由 clamp"的豁免条件（有理由、有文档）。
- `linear_rgb_to_hex` 四舍五入与 0..255 边界保护正确；该函数被 src/features/composite/nodes/constant_node.py:377 实际使用。
- 类型注解完整，无外部输入、无状态、无可变全局，线程安全。
- 异常路径不适用（纯计算，无 IO）。

---

## src/shared/color/color_picker.py (行数: 2,789)

### 发现

- [AUD-32401] [Major] 屏幕吸管混合逻辑坐标与物理像素坐标，HiDPI/Retina 屏上取到错误位置的颜色
  - 位置: src/shared/color/color_picker.py:437,448-454
  - 证据:
    ```python
    self.full_screen_img = screen.grabWindow(0).toImage()
    ...
    def mousePressEvent(self, event):
        pos = event.globalPosition().toPoint()
        # 往左偏移8个像素以补偿吸管图标热点偏差
        # 边界保护：避免坐标越界
        px = max(0, min(self.full_screen_img.width() - 1, pos.x() - 8))
        py = max(0, min(self.full_screen_img.height() - 1, pos.y()))
        color = QColor(self.full_screen_img.pixel(px, py))
    ```
  - 影响: `grabWindow()` 返回的图像按物理像素存储（Retina 上宽高均为逻辑值的 2 倍），而 `globalPosition()` 返回逻辑坐标。在 devicePixelRatio>1 的屏幕（本项目主力平台 macOS 全系 Retina）上，实际采样点约为用户点击位置的一半偏左上，取色结果系统性错误。另外固定的 `-8` 像素 x 偏移只是热点补偿 hack，使采样点再偏离光标 8 逻辑像素。
  - 建议: 用 `img.devicePixelRatio()`（或 `screen.devicePixelRatio()`）把逻辑坐标换算为物理坐标后再 clamp 取样；热点偏差应通过精确设置 QCursor hotspot 消除而非采样偏移。
  - 置信度: 高

- [AUD-32402] [Major] get_color() 每次调用泄漏整个选择器 widget 树及全屏截图 QImage，注释与 WA_DeleteOnClose=False 的实际代码相反
  - 位置: src/shared/color/color_picker.py:2754-2755（另见 437、2444）
  - 证据:
    ```python
    # 设置窗口关闭行为：关闭即销毁（这样 closeEvent 后 widget 会被 deleteLater）
    studio.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
    ```
  - 影响: 注释声称"关闭即销毁"，代码却显式传 `False`（这也是默认值），关闭后 widget 不会被销毁，`get_color()` 返回后 `studio` 无任何引用释放路径——每次取色泄漏一个 `CompactColorStudio`（两页 UI、8 色板行 × 20 按钮等数百个 QObject）及其 `EyeDropperOverlay.full_screen_img`（4K 屏约 3840×2160×4 ≈ 33MB 的整屏 QImage，吸管用过一次就常驻）。长期编辑会话反复取色将累积数百 MB 内存增长。
  - 建议: 将该行改为 `True`（或去掉该行并在 `loop.quit()` 后对 studio 调用 `deleteLater()`，注意需在事件循环仍存活时调度）；同时在 `EyeDropperOverlay.close()/colorPicked` 后清空 `full_screen_img`。
  - 置信度: 高

- [AUD-32403] [Major] 导入色板 JSON 完全无结构验证，恶意/畸形数据导致 UI 与数据不一致或异常
  - 位置: src/shared/color/color_picker.py:1942-1948（配合 1784、1874）
  - 证据:
    ```python
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if "swatches" in data:
            self.swatch_data = data["swatches"]
            self._create_swatch_rows()
            QMessageBox.information(self, "导入成功", "色板已成功导入")
    ```
  - 影响: 外部文件内容直接整体替换 `swatch_data`，不校验行结构（`name`/`colors` 键、颜色是否合法 hex、行数/列数）。若某行缺键，`DraggableSwatchRow(row_data["name"], row_data["colors"], i)`（1784 行）抛 KeyError——虽被外层宽泛 except 捕获并弹"导入失败"，但此时 `swatch_data` 已被替换而 `_create_swatch_rows()` 已先 deleteLater 清空旧行、只建了一半，UI 与数据进入不一致状态；后续 `_add_current_to_custom` 假设最后一行是"自定义"行（1874 行 `self.swatch_rows[-1]`）在导入任意数据后不再成立。
  - 建议: 导入前逐行校验（dict、`name` 为 str、`colors` 为合法 hex 列表），校验失败则拒绝导入并保留原数据；或先在临时变量上构建成功后再提交。
  - 置信度: 高

- [AUD-32404] [Major] ASE 导出文件格式无效：头部颜色计数把占位色计入但循环跳过，且每个颜色块缺少 4 字节块长度字段
  - 位置: src/shared/color/color_picker.py:1973-1994
  - 证据:
    ```python
    total_colors = sum(len(row.get_colors()) for row in self.swatch_rows)
    f.write(struct.pack('>I', total_colors))

    for row in self.swatch_rows:
        for color_hex in row.get_colors():
            if color_hex != "#333333":
                f.write(struct.pack('>H', 0x0001))
    ```
  - 影响: 头部声明的块数包含所有 `#333333` 占位色，实际写入时跳过它们（1978 行），文件头计数与实际块数不一致；同时按 ASE 规范每个块应为 `uint16 块类型 + uint32 块长度 + 数据`，此处类型后直接写名称长度，缺少 uint32 块长度字段。两者叠加导致 Photoshop 等软件无法解析导出的 .ase 文件（功能缺陷：导出产物不可用）。块计数不一致由代码即可确证；缺块长度字段基于对 ASE 二进制格式的记忆。
  - 建议: 头部计数只统计实际写入的块；每个块先在内存中组装 payload，再写 `type + len(payload) + payload`。
  - 置信度: 中

- [AUD-32405] [Major] 色板页 RGB 输入的连续中间值经 colorSelected 信号灌入并挤掉颜色历史记录
  - 位置: src/shared/color/color_picker.py:1848-1856、2637-2640
  - 证据:
    ```python
    def _on_rgb_changed(self, key, val):
        """RGB输入框改变时处理"""
        ...
        self.current_color = QColor(r, g, b)
        self.preview.set_color(self.current_color.name().upper())
        self.hex_edit.setText(self.current_color.name().upper())
        self.colorSelected.emit(self.current_color)
    ...
    def _on_swatch_color_selected(self, color):
        """色板选择颜色后同步到拾色器页面"""
        self.picker_page.load_qcolor(color)
        self.picker_page.color_history.add_color(color)
    ```
  - 影响: `SpinLineEdit.valueChanged` 在滚轮（每格一次）和中键拖拽（每像素步进一次）时连续触发；每个中间颜色都经 `colorSelected → _on_swatch_color_selected → color_history.add_color` 插入历史。历史仅 12 格且确认时整体持久化（`_confirm_and_close`），一次拖拽即可用 12 个中间值清空用户真实历史并写入配置文件（用户历史数据丢失）。
  - 建议: 中间值只同步预览不发 `colorSelected`，或仅对离散来源（色块点击、editingFinished）记录历史；`add_color` 也可只在 confirm/picked 等明确动作时调用。
  - 置信度: 高

- [AUD-32406] [Major] 溢色检查对 Rec.709/ACEScg 回退按 sRGB 矩阵计算，警告结果错误；ACESCG_TO_XYZ 为死代码
  - 位置: src/shared/color/color_picker.py:216-223、1227、1288-1296（死常量 183-194）
  - 证据:
    ```python
    if color_space == "sRGB":
        matrix = SRGB_TO_XYZ
    elif color_space == "Display P3":
        matrix = P3_TO_XYZ
    else:
        matrix = SRGB_TO_XYZ
    ...
    self.space_combo.addItems(["sRGB", "Display P3", "Rec.709", "ACEScg"])
    ```
  - 影响: 设置面板下拉框提供 Rec.709/ACEScg 选项，`_check_gamut` 会以这两个空间作为 source 调 `check_out_of_gamut`，但 `rgb_to_xyz_linear` 对二者静默回退用 sRGB→XYZ 矩阵（正向转换分支从未使用 `ACESCG_TO_XYZ`，全仓库无引用），导致"溢色警告"对这两个空间给出错误的数学结果——或误报或漏报。`convert_color_space`（240 行）同样受此影响且全仓库无人调用（死代码）。
  - 建议: 为 Rec.709/ACEScg 补齐正向矩阵分支（ACEScg 用已定义的 ACESCG_TO_XYZ、白点 D60 需配 Bradford 适配），或从下拉框中移除未支持的空间选项，避免展示错误结论。
  - 置信度: 高

- [AUD-32407] [Major] 用户配置文件内容未做结构验证，畸形数据使取色器构造抛未捕获异常
  - 位置: src/shared/color/color_picker.py:1599-1604、2494-2505（消费点 1784）
  - 证据:
    ```python
    config = load_config()
    if config and "swatches" in config:
        self.swatch_data = config["swatches"]
        # 加载上次选择的颜色
        last_color = config.get("last_color", "#FF0000")
        self.current_color = QColor(last_color)
    ...
    config = load_config()
    if config and "picker_custom_swatches" in config:
        picker_custom = config["picker_custom_swatches"]
        for i, color in enumerate(picker_custom):
    ```
  - 影响: JSON 语法错误已被 `load_config` 兜住（返回 None），但结构畸形（如 `"swatches": [1,2]`、`picker_custom_swatches` 不是列表、行缺 `name`/`colors` 键）不做校验：`SwatchPage.__init__` 内 `_create_swatch_rows` 的 `row_data["name"]` 抛 KeyError/TypeError，异常沿 `CompactColorStudio()` → `get_color()` 直接抛给调用方，取色功能不可用直至手工删除配置文件。违反"外部数据须验证"铁律。
  - 建议: 加载后做逐层校验（swatches 为 dict 列表、colors 为 str 列表、hex 可被 QColor 解析），任一项不合法则回退默认色板并告警。
  - 置信度: 高（机制确定；实际触发需配置文件被手工改坏或损坏）

- [AUD-32408] [Suggestion] IconButton.paintEvent 每次重绘都重新解析两份 SVG（QSvgRenderer 无缓存）
  - 位置: src/shared/color/color_picker.py:401-407
  - 证据:
    ```python
    shadow_renderer = QSvgRenderer(self._svg_data.replace(b'fill="currentColor"', b'fill="#000000"'))
    painter.setOpacity(0.3)
    shadow_renderer.render(painter, rect.translated(1.5, 1.5))

    white_renderer = QSvgRenderer(self._svg_data)
    painter.setOpacity(1.0)
    white_renderer.render(painter, rect)
    ```
  - 影响: 每次重绘（hover、布局变化）都构造两个 QSvgRenderer 并重新解析 SVG 文档；该按钮被 8 个色板行的拖拽手柄和两页的取色/设置按钮复用，重绘频繁时纯属浪费，违背实时性能基准下的性能模式要求。
  - 建议: 在 `set_svg_data` 时把阴影/主体各渲染一次到 QPixmap 缓存，paintEvent 只 drawPixmap；顺带修复阴影分支对已替换 fill 的 SVG 再替换 `currentColor` 不生效（阴影与主体同色）的小问题。
  - 置信度: 高

- [AUD-32409] [Suggestion] HueSlider.paintEvent 逐像素 setPen+drawLine 绘制 250 条线，无缓存
  - 位置: src/shared/color/color_picker.py:498-504
  - 证据:
    ```python
    def paintEvent(self, event):
        p = QPainter(self)
        for y in range(self.height()):
            p.setPen(QColor.fromHsv(int(y/self.height()*359), 255, 255))
            p.drawLine(0, y, self.width(), y)
    ```
  - 影响: 拖动色相时每次鼠标移动都触发全量重绘（250 次 fromHsv + setPen + drawLine）；同样内容可一次绘制成 QPixmap 复用。
  - 建议: 首次绘制时生成渐变 pixmap 缓存，paintEvent 仅画缓存加指示线。
  - 置信度: 高

- [AUD-32410] [Suggestion] 配置读写与重置用宽泛 except Exception 吞错且仅 print 输出
  - 位置: src/shared/color/color_picker.py:44-50、54-71、2608-2613
  - 证据:
    ```python
    try:
        ...
        with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"配置已保存到: {CONFIG_FILE_PATH}")
    except Exception as e:
        print(f"保存配置文件失败: {e}")
    ```
  - 影响: 保存失败（磁盘满、权限）对用户完全无感知（仅 stdout print），用户以为色板已保存实际丢失；与项目"不得宽泛 except 掩盖领域错误"及日志规范不符。`SwatchPage.__init__` 中的 `print("使用默认色板配置")` 同类。
  - 建议: 捕获 `OSError`/`json.JSONDecodeError` 等具体异常，失败时经 QMessageBox 或项目日志系统上报给用户。
  - 置信度: 高

- [AUD-32411] [Suggestion] SwatchPage._confirm_color 的中间保存会先把 picker 自定义色板/历史瞬时写空，依赖紧随其后的第二次保存补救
  - 位置: src/shared/color/color_picker.py:1858-1866（对照签名 52-64、完整保存 2682-2683）
  - 证据:
    ```python
    # 保存配置到文件（包括当前选择的颜色）
    current_color_hex = self.current_color.name().upper()
    save_config(self.swatch_data, current_color_hex)

    self.colorConfirmed.emit(self.current_color)
    ```
  - 影响: `save_config` 缺省参数把 `picker_custom_swatches` 写成 20 个占位 `#333333`、`color_history` 写成 `[]`；正常流程靠随后 `_confirm_and_close` 的四参保存覆盖回来，但若第二次写盘失败（异常被 AUD-32410 的宽泛 except 吞掉）或 SwatchPage 被单独复用，用户自定义色板与历史即被真实抹掉。属带数据丢失隐患的冗余双写。
  - 建议: 删除 `_confirm_color` 内的这次保存，统一由 `CompactColorStudio._confirm_and_close` 落盘；或让 save_config 缺省保留而非清空既有字段。
  - 置信度: 高

- [AUD-32412] [Suggestion] 透明度不随 last_color/历史持久化，重开后丢失
  - 位置: src/shared/color/color_picker.py:2665-2683（另见 1164）
  - 证据:
    ```python
    # 保存配置（包括色板数据、当前颜色、PickerPage自定义色板、历史记录）
    current_color_hex = color.name().upper()
    save_config(self.swatch_page.swatch_data, current_color_hex, picker_custom_colors, history_colors)
    ```
  - 影响: `QColor.name()` 默认只输出 #RRGGBB，用户在拾色器里设置的 alpha（AlphaSlider/数值框）在 last_color 与历史记录中均被丢弃，下次打开回到 255；历史项点击也总返回不透明色。
  - 建议: 持久化用 `color.name(QColor.NameFormat.HexArgb)`，加载与历史还原时解析回 alpha。
  - 置信度: 高

- [AUD-32413] [Suggestion] 大量未使用导入与死代码
  - 位置: src/shared/color/color_picker.py:72-81（未用导入）、147-152（COLOR_SPACES）、183-194（ACESCG_TO_XYZ）、240-246（convert_color_space）
  - 证据:
    ```python
    from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                                 QLabel, QLineEdit, QPushButton, QGridLayout, QFrame, QScrollArea,
                                 QFileDialog, QMessageBox, QDialog, QSizePolicy, QSpacerItem,
                                 QMenu, QColorDialog, QStackedWidget, QInputDialog, QComboBox,
                                 QCheckBox)
    from PyQt6.QtGui import (QPainter, QColor, QLinearGradient, QPen, QScreen,
                             QCursor, QPainterPath, QBrush, QPixmap, QBitmap, QIcon, QFont, QAction, QDrag)
    ```
  - 影响: `QSpacerItem`、`QCheckBox`、`QScreen`、`QPainterPath`、`QBrush`、`QBitmap`、`QIcon`、`QSize` 在文件内零使用（已 grep 验证）；`COLOR_SPACES`、`ACESCG_TO_XYZ`、`convert_color_space` 全仓库无引用（其余同名命中均为其他模块的独立标识符）。徒增导入成本与维护噪声。
  - 建议: 清理未用导入与死常量/死函数（`convert_color_space` 若保留需先修复 AUD-32406）。
  - 置信度: 高

- [AUD-32414] [Suggestion] 屏幕吸管只覆盖主显示器，多屏环境无法取副屏颜色
  - 位置: src/shared/color/color_picker.py:432-442
  - 证据:
    ```python
    def start(self):
        """启动吸管模式：截图 → 显示全屏覆盖层 → 等待点击。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            self.cancelled.emit()
            return
    ```
  - 影响: 覆盖层几何与截图固定取 `primaryScreen()`，多显示器用户只能取主屏颜色，副屏区域无覆盖、点击无效（功能覆盖缺陷，非错误结果）。
  - 建议: 用 `QApplication.screenAt(QCursor.pos())` 取光标所在屏，或对全部 QScreen 各建覆盖层。
  - 置信度: 高

- [AUD-32415] [Suggestion] 模块级函数与各方法普遍缺失类型注解
  - 位置: src/shared/color/color_picker.py:52、196、270、286、1622、2269 等全文件
  - 证据:
    ```python
    def save_config(swatch_data, last_color="#FF0000", picker_custom_swatches=None, color_history=None):
        """保存配置文件"""
    ...
    def rgb_to_lab(r, g, b):
    ...
    def _on_rgb_changed(self, key, val):
    ```
  - 影响: 2,789 行的 UI 模块中除个别新 API（`set_current_color(self, color: QColor)`、`get_current_color(self) -> QColor`）外均无类型注解，违反项目强类型约束（Suggestion 级），不利于 IDE 检查与后续维护。
  - 建议: 至少为公共 API（save_config/load_config、颜色数学函数、各页面公共方法）补齐参数与返回值注解。
  - 置信度: 高

### 通过项
- 线程安全：全模块仅在 UI 线程运行，无 OpenGL 调用、无跨线程共享状态（本模块无渲染线程交互）。
- GPU 资源生命周期：无 ModernGL/纹理/缓冲区使用，不适用。
- GLSL·HDR 正确性：不适用（无 GLSL）；本模块颜色均为 display-referred 0..255，经 color_conversion 的文档化 clamp 转换，符合规范豁免。
- 信号防递归：`PickerPage.sync_all` 有 `_updating` 状态锁；`SpinLineEdit.setValue` 不发射 valueChanged，`SwatchPage.set_current_color` 程序化更新（setValue/setText）均不会回触发信号，检查未发现递归回路。
- 取消路径：`EyeDropperOverlay` 有 Esc 取消（cancelled 信号）与 grabWindow 失败/无屏幕的空值检查；`get_color` 通过重写 closeEvent 保证直接关窗时事件循环退出、返回 None。
- 拾色边界：`load_qcolor`/`sync_all` 对无彩色 hsvHue/hslHue 返回 -1 的情况均有回退处理；hex 输入有长度与 QColor.isValid 校验。
- 状态提交一致性：色板按钮级编辑（拖拽交换/右键改色）到 `swatch_data` 的同步采用"确认/导出时统一回读"策略，路径一致；`_on_row_moved`/`_on_name_changed` 同步更新数据侧。
