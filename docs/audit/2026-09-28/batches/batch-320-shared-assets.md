# batch-320 审查报告 — src/shared/assets//

编号区间：AUD-32000–AUD-32099。审查日期：2026-09-28。
审查文件：`src/shared/assets/__init__.py`（1 行）、`src/shared/assets/icon_manager.py`（645 行）。
事实核验说明：qtawesome 相关结论均已对照 `.venv/lib/python3.12/site-packages/qtawesome` 内置 charmap（FA 5.15.4 solid/regular、Elusive 2.0）与 `iconic_font.py` 的字形解析源码；调用方影响均经 grep 定位到真实 file:line。

## src/shared/assets/icon_manager.py (行数: 645)

### 发现

- [AUD-32001] [Major] icon_favorite_outline 实为实心星形，与 icon_favorite 完全相同，收藏两态视觉不可分辨
  - 位置: src/shared/assets/icon_manager.py:304-306（ICON_MAP 同样问题见 :483）
  - 证据:
    ```python
    def icon_favorite_outline(color: str = IconColors.DEFAULT) -> QIcon:
        """收藏图标（空心星星）"""
        return get_icon('fa5s.star', color=color)
    ```
    对比 :299-301 的 `icon_favorite` 同样返回 `'fa5s.star'`。
  - 影响: `src/features/edit/ui/edit_effects_panel.py:700` 以 `icon_favorite() if is_favorite else icon_favorite_outline()` 切换收藏状态，两条路径渲染出的图标逐像素相同，用户无法从图标区分是否已收藏。
  - 建议: 空心星应取 FA5 regular 集——qtawesome 前缀映射为 `fa5`=regular / `fa5s`=solid（见 qtawesome `__init__.py:189-191`），且 regular charmap 已含 `star`，改为 `get_icon('fa5.star', color=color)`；ICON_MAP 的 `'favorite_outline'`（:483）同步修改。
  - 置信度: 高（charmap 已验证 regular 与 solid 均含 `star`）

- [AUD-32002] [Major] 'fa5s.object-intersect' 字形在 qtawesome FA5 solid charmap 中不存在，布尔交集按钮图标恒为空白
  - 位置: src/shared/assets/icon_manager.py:170-172（ICON_MAP `'op_intersect'` 同，:542）
  - 证据:
    ```python
    def icon_operation_intersect(color: str = IconColors.WHITE) -> QIcon:
        """布尔运算 - 交集"""
        return get_icon('fa5s.object-intersect', color=color)
    ```
  - 影响: 已对照 `fontawesome5-solid-webfont-charmap-5.15.4.json`，`object-intersect` 缺失（FA5 free 仅有 `object-group`/`object-ungroup`）；qtawesome `iconic_font.py:561-566` 对未知字形 `raise Exception('Invalid icon name ...')`，被 `get_icon` 的宽泛 except 吞掉后返回空 `QIcon()`。`src/features/color/ui/mask_panel.py:150` 的蒙版布尔运算 "intersect" 按钮因此显示空白图标，且每次调用记一条 error 日志。
  - 建议: 换用实际存在的字形（如 `fa5s.object-group`、`fa5s.shapes` 等 FA5 free 近似图形），并在 CI 中加一条对 ICON_MAP 全部字形的存在性校验，防止同类问题再入库。
  - 置信度: 高（charmap 与 qtawesome 抛错路径均已验证）

- [AUD-32003] [Major] icon_mask 的 SVG 成功路径完全忽略 color 参数，蒙版激活态橙色高亮永不生效
  - 位置: src/shared/assets/icon_manager.py:83-108
  - 证据:
    ```python
    def icon_mask(color: str = IconColors.DEFAULT) -> QIcon:
        """蒙版图标 - 从 SVG 文件加载"""
        ...
        if os.path.exists(svg_path):
            try:
                renderer = QSvgRenderer(svg_path)
                if renderer.isValid():
                    pixmap = QPixmap(64, 64)
                    pixmap.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(pixmap)
                    renderer.render(painter)
                    painter.end()
                    return QIcon(pixmap)
    ```
  - 影响: `mask.svg` 已确认存在于 `src/shared/assets/icons/buttons/mask.svg`，即 SVG 分支总是命中，`color` 只在加载失败的回退分支（:108）才生效。调用方 `src/shared/ui/widgets/viewport_toolbar_base.py:235-237` `_on_mask_toggled` 在激活时传 `IconColors.ORANGE` 以示高亮，但 SVG 路径按文件原色渲染，激活/非激活两态视觉无差异，tint 参数被静默丢弃。
  - 建议: 渲染后按 `color` 着色（例如以 `QPainter.CompositionMode_SourceIn` 叠加纯色层），或放弃 SVG 改走 qtawesome 字形管线保持统一着色。
  - 置信度: 高（路径解析、文件存在性、调用方预期均已核实）

- [AUD-32004] [Suggestion] get_icon 使用宽泛 except Exception，吞掉 qtawesome 的具体错误类型（铁律：不得宽泛 except 掩盖领域错误）
  - 位置: src/shared/assets/icon_manager.py:46-50
  - 证据:
    ```python
    try:
        return qta.icon(name, color=color, scale=scale)
    except Exception as e:
        logger.error(f"Failed to load icon '{name}': {e}")
        return QIcon()
    ```
  - 影响: qtawesome 对未知前缀/字形只 raise 通用 `Exception`（`iconic_font.py:556-566`），与调用方传错参数造成的 `TypeError` 等编程错误在此无法区分；错误仅降级为一条日志 + 空图标（AUD-32002 即由此长期潜伏），不可观测也易被日志刷屏淹没。
  - 建议: 对已知失败模式做显式预检（如启动时校验全部字形名），except 收窄至 `Exception` 之外至少区分可恢复/不可恢复；此处兜底逻辑可保留但应计数告警。
  - 置信度: 高（行为确认；严重度按"UI 图标兜底属可接受防御式代码"评估为 Suggestion）

- [AUD-32005] [Suggestion] icon_mask 每次调用都重新执行磁盘存在性检查 + SVG 解析 + 64×64 栅格化，无缓存
  - 位置: src/shared/assets/icon_manager.py:86-103
  - 证据: 同 AUD-32003 代码块（每次调用 `os.path.exists` → `QSvgRenderer(svg_path)` → `QPainter` 渲染）。
  - 影响: 蒙版按钮每次 toggle（viewport_toolbar_base.py:236）及各视口工具栏构建时都重复一次磁盘 IO + SVG 解析；单次开销小，但属可零成本消除的重复计算。
  - 建议: 模块级按 `(svg mtime, color)` 缓存 QIcon/QPixmap。
  - 置信度: 高（事实），影响小（调用频率为用户点击级），故定 Suggestion

- [AUD-32006] [Suggestion] gradient 图标两个入口不一致：函数用 'ei.tint'，ICON_MAP 用 'fa5s.sliders-h'
  - 位置: src/shared/assets/icon_manager.py:174-176 与 :513
  - 证据:
    ```python
    def icon_gradient(color: str = IconColors.WHITE) -> QIcon:
        """渐变图标"""
        return get_icon('ei.tint', color=color)
    ```
    ```python
    'gradient': 'fa5s.sliders-h',
    ```
  - 影响: `icon_gradient()` 渲染 Elusive 的水滴形，`get_icon_by_name('gradient')` 渲染 FA5 滑块形，同一语义两个入口视觉不一致；且 `ei` 字体族与全文件主体的 FA5 风格混用。
  - 建议: 统一到一个来源（如 `fa5s.sliders-h`，或语义更贴近渐变的 `fa5s.fill-drip`）。
  - 置信度: 高（两处代码直接对照；`ei.tint` 在 Elusive charmap 中存在，功能可用）

- [AUD-32007] [Suggestion] make_link_button 返回注解 "QToolButton" 缺少 TYPE_CHECKING 导入，静态检查无法解析
  - 位置: src/shared/assets/icon_manager.py:610（QToolButton 仅在 :625 函数体内局部导入）
  - 证据:
    ```python
    def make_link_button(initial_linked: bool = True, tooltip: str = "Link") -> "QToolButton":
        ...
        # 局部导入避免与 style_sheet 潜在的循环依赖。
        from PyQt6.QtWidgets import QToolButton
    ```
  - 影响: 模块命名空间中不存在 `QToolButton`，mypy/pyright 会将该注解判为未知名称，违背项目"强类型"约束。
  - 建议: 增加 `if TYPE_CHECKING: from PyQt6.QtWidgets import QToolButton`（局部导入防循环依赖的动机不受影响）。
  - 置信度: 高

- [AUD-32008] [Suggestion] icon_hidden 默认色 '#444444' 为魔法字面量，游离于 IconColors 常量体系之外
  - 位置: src/shared/assets/icon_manager.py:79
  - 证据:
    ```python
    def icon_hidden(color: str = "#444444") -> QIcon:
        """隐藏图标 (眼睛闭合)"""
        return get_icon('fa5s.eye-slash', color=color)
    ```
  - 影响: 全文件配色集中于 `IconColors`（连 `LINK_ICON_COLOR_OFF` 都有注释说明），此处例外，主题色调整时易漏改。
  - 建议: 提取为 `IconColors` 常量（如 `HIDDEN`）。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程工具模块，无可变共享状态（`ICON_MAP` 为只读字面量），无跨线程数据竞争面。
- GPU 资源生命周期：不涉及 ModernGL/OpenGL 资源，无泄漏面。
- GLSL/HDR 正确性：不涉及着色器与 HDR 数据。
- 信号防递归：`make_link_button` 的 `_on_toggled` 仅 `setIcon`，无再入环路；先 `setChecked` 后 `connect`、再显式 `setIcon` 的顺序正确，初始态不依赖信号触发。
- 状态提交一致性：无 undo/redo/revision/持久化逻辑，不适用。
- 异常与取消路径：SVG 加载失败与字形缺失均有兜底（回退 qtawesome / 空 QIcon），不会抛出到调用方；无异步可取消操作。
- 输入验证：`get_icon_by_name` 对未知 key 记日志并返回空图标，无崩溃路径。
- 资源路径与依赖：`mask.svg` 相对路径解析正确（文件已验证存在）；`src/shared/ui/style_sheet.py:450` 确有 `TOOLBAR_FLAT_BTN_STYLE`，局部导入目标有效。
- 类型注解：除 AUD-32007 外所有公开函数均有完整参数与返回类型注解。

## src/shared/assets/__init__.py (行数: 1)

### 发现

- [AUD-32009] [Suggestion] 星号 re-export 且上游无 __all__，将 os/qta/logger 等模块级名字一并泄漏到包命名空间
  - 位置: src/shared/assets/__init__.py:1（根因配合 icon_manager.py 未定义 `__all__`）
  - 证据:
    ```python
    from src.shared.assets.icon_manager import *
    ```
  - 影响: 因 `icon_manager.py` 无 `__all__`，`import *` 会把 `os`、`logging`、`logger`、`qta`、`QSvgRenderer` 等模块级导入一并导出，`src.shared.assets.os/qta/logger` 成为包的公开属性，污染命名空间并带来误引用/IDE 补全噪音。
  - 建议: 在 icon_manager.py 定义 `__all__`（显式列出公开函数、`IconColors`、`ICON_MAP`、`LINK_ICON_COLOR_*`），或 `__init__.py` 改为显式按名导入。
  - 置信度: 高

### 通过项
- 通过（无逻辑）：纯 re-export 文件，仅 1 行；导出 icon_manager 的全部公开名（含上述泄漏的模块级名，见 AUD-32009）。
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交 / 异常取消 / 输入验证 / 性能：不适用（无逻辑）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-32001, AUD-32002, AUD-32003 |
| Suggestion | 6 | AUD-32004, AUD-32005, AUD-32006, AUD-32007, AUD-32008, AUD-32009 |
