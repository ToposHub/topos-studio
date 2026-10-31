# 批次报告：batch-114 — src/features/composite/ui/（viewport 工具系统与 3D 视口层）

- 任务书：docs/audit/2026-09-28/assignments/batch-114.md
- 编号区间：AUD-11400–AUD-11499（实际使用 AUD-11400–AUD-11413）
- 审查文件：4 个，共 2,387 行
- 汇总：Critical 0 / Major 2 / Suggestion 12
- 说明：本批四个文件均为纯数据/交互判定层，不含 ModernGL 调用、不含 GLSL。交叉引用只读核实过
  composite_viewport.py、composite_page.py 中的调用点（引用行号以当前工作区为准）。

---

## src/features/composite/ui/viewer_tool_system.py (行数: 1683)

### 发现

- [AUD-11400] [Major] `reset()` 与 `start_drag()` 均不清空 `preview_params`，上一次拖拽的增量泄漏进下一次交互
  - 位置: src/features/composite/ui/viewer_tool_system.py:116-155
  - 证据:
    ```python
    def reset(self) -> None:
        """Reset interaction state to default (no active drag)."""
        ...
        self.original_params.clear()
        self.final_params.clear()
        self.is_dragging = False
    ```
    （`preview_params` 不在清除之列；`start_drag()` 只复制 `original_params`，同样不重置
    `preview_params`。消费方 composite_viewport.py:2221-2238 仅以 `is_dragging/tool/original_params/active_handle`
    为门槛直接叠加旧值：）
    ```python
    preview_params = st.preview_params or {}
    handle = st.active_handle
    params = dict(st.original_params)
    if handle in ("center", "move"):
        params["translate_x"] = st.original_params.get("translate_x", 0.0) + preview_params.get("delta_x", 0.0)
    ```
  - 影响: 新一轮拖拽在 press（start_drag）到首个 mouse-move 之间，`preview_params` 仍保留上一次
    拖拽的 delta；此时若发生重绘/防抖更新（composite_viewport.py:2233、composite_page.py:9566），
    旧 delta 会被叠加到新拖拽的 translate/pivot 预览上，出现"幽灵偏移"错误预览。undo 提交取
    `node.parameters`（composite_page.py:9338），不直接受损，故不评 Critical。
  - 建议: 在 `reset()` 中加入 `self.preview_params.clear()`，并在 `start_drag()` 开头重置
    `preview_params`（clear 或置空 dict），保证每轮交互从干净状态开始。
  - 置信度: 中（代码路径确凿；暴露窗口为 press→首 move 之间，依赖该窗口内是否有重绘/更新）

- [AUD-11401] [Major] `compute_constrained_delta` 的 Shift-锁定纵横比约定与现行实现及文档相反
  - 位置: src/features/composite/ui/viewer_tool_system.py:1604-1626（对照 1661-1672）
  - 证据（compute_constrained_delta，Shift 按下才锁定）:
    ```python
    if shift_pressed:
        if is_rotate:
            ...
        elif is_scale and original_size:
            config.lock_aspect_ratio = True
            # Aspect ratio will be applied after computing new size
    ```
    对照同文件 `get_constraint_config_from_modifiers`（未按 Shift 才锁定，即现行约定）:
    ```python
    elif is_scale and not shift_pressed:
        # Scale: aspect ratio is locked BY DEFAULT; Shift frees the axes for
        # independent x/y scaling (inverse of the old Shift-locks convention).
        config.lock_aspect_ratio = True
    ```
    `ConstraintConfig` 文档串（1410-1413 行）亦写明 "aspect ratio locked BY DEFAULT; Shift frees
    the axes"。
  - 影响: composite_viewport.py:75 导入了 `compute_constrained_delta`（当前全库无调用点，属死导入）。
    该"统一入口"一旦被接线，缩放时 Shift 语义与文档、与现行 `get_constraint_config_from_modifiers`
    管线（composite_viewport.py:8357/8413 实际使用）完全相反——按 Shift 反而锁比例，放开 Shift 反而自由缩放。
  - 建议: 修正 `compute_constrained_delta` 与现行约定对齐（`is_scale and not shift_pressed` 时置
    `lock_aspect_ratio`），或直接删除该函数及 composite_viewport.py 中的死导入，避免双约定并存。
  - 置信度: 高（代码矛盾可直接验证；当前为死代码，风险为潜在接线风险）

- [AUD-11402] [Suggestion] `update_drag` 的 `ndc_pos`/`format_pos` 参数被静默丢弃
  - 位置: src/features/composite/ui/viewer_tool_system.py:157-180
  - 证据:
    ```python
    if ndc_pos is not None:
        # Store NDC if needed for delta calculations
        pass
    if image_pos is not None:
        self.drag_current_image_pos = image_pos
    ```
  - 影响: 签名暗示会记录 NDC/format 坐标，实际 `pass` 丢弃（`format_pos` 完全未用）。全库调用点
    （composite_viewport.py:8221/8226/8386/8549/8938/8979/9058）均只传 view_pos/image_pos，
    属误导性死参数。
  - 建议: 删除未用参数，或真正存储 `drag_current_ndc_pos`/`drag_current_format_pos`。
  - 置信度: 高

- [AUD-11403] [Suggestion] `apply_angle_constraint` 使用可变默认参数
  - 位置: src/features/composite/ui/viewer_tool_system.py:1496-1499
  - 证据:
    ```python
    def apply_angle_constraint(
        angle_degrees: float,
        snap_angles: list[float] = [15.0, 45.0, 90.0],
    ) -> float:
    ```
  - 影响: 可变默认参数（lint B006）。当前函数只读不写该列表，无实际 bug，但存在被后续修改误用的风险。
  - 建议: 改为 `snap_angles: tuple[float, ...] = (15.0, 45.0, 90.0)` 或 None 哨兵 + 函数内默认。
  - 置信度: 高

- [AUD-11404] [Suggestion] `SnapConfig.threshold_pixels`（屏幕像素阈值）定义后全库未使用
  - 位置: src/features/composite/ui/viewer_tool_system.py:1124-1125（消费点 1340）
  - 证据:
    ```python
    threshold_pixels: float = 10.0  # Screen pixel threshold
    threshold_image: float = 5.0    # Image pixel threshold
    ...
    threshold = config.threshold_image
    ```
  - 影响: `apply_snapping` 只用 `threshold_image`；`threshold_pixels` 无任何消费方。吸附阈值用
    图像像素单位意味着缩放级别不同时屏幕上的实际吸附手感不一致，与配置字段的设计意图（屏幕像素）
    相悖。
  - 建议: 删除 `threshold_pixels`，或在吸附路径中按当前 zoom 把屏幕阈值换算为图像阈值后取用。
  - 置信度: 高

- [AUD-11405] [Suggestion] `view_pos_to_ndc`/`ndc_to_view_pos`/`image_pos_to_ndc` 缺零除防护，与 `compute_image_ndc_rect` 的防护不一致
  - 位置: src/features/composite/ui/viewer_tool_system.py:328-329、354-355、421-422
  - 证据:
    ```python
    ndc_x = (mx / (vp_w / 2.0)) - 1.0
    ndc_y = 1.0 - (my / (vp_h / 2.0))  # Flip Y
    ```
    以及 image_pos_to_ndc 中 `u = image_pos.x() / image_width`。同文件 `compute_image_ndc_rect`
    （513-514 行）对 `<= 0` 输入有显式退化防护，这三个转换函数没有。
  - 影响: viewport 宽高为 0（初始化/收起时）、DPR 为 0 或纹理尺寸为 0 时抛 ZeroDivisionError。
    现有调用方通常传入有效值，属防御缺口。
  - 建议: 与 `compute_image_ndc_rect` 一致地对非正尺寸做退化返回（如返回原点或 None）。
  - 置信度: 中

- [AUD-11406] [Suggestion] `parse_preset_dimensions` 对未知 preset 名静默回退 (1920, 1080)
  - 位置: src/features/composite/ui/viewer_tool_system.py:888-900
  - 证据:
    ```python
    presets = { ... "4K Scope": (4096, 1716), }
    return presets.get(preset_name, (1920, 1080))
    ```
  - 影响: preset 名来自节点参数（可持久化的工程文件=外部数据）。工程文件中的笔误、预设改名或版本
    差异会被静默改写成 HD 1080，Reformat 节点输出尺寸悄然变化且无日志可查，违反"外部数据须验证"。
  - 建议: 未知 preset 至少记录 warning 日志；或返回 None 由调用方决定回退策略。
  - 置信度: 高

- [AUD-11407] [Suggestion] `hit_test_rect` 不归一化矩形，反拖（x0>x1）时恒为 False
  - 位置: src/features/composite/ui/viewer_tool_system.py:572-587
  - 证据:
    ```python
    x0, y0, x1, y1 = rect_view
    return (view_pos.x() >= x0 and view_pos.x() <= x1 and
            view_pos.y() >= y0 and view_pos.y() <= y1)
    ```
  - 影响: 同文件 `hit_test_handles_in_rect`（670-674 行）与 `ViewerSelectionState.end_rubber_band`
    （736-740 行）都先归一化，唯独此函数不归一化，语义不一致。当前无外部调用方（死代码），一旦
    被用于原始拖拽矩形将出现"从右下往左上框选命中失败"。
  - 建议: 与 `hit_test_handles_in_rect` 一致地先归一化再判断。
  - 置信度: 高

- [AUD-11408] [Suggestion] `modifier_keys`/`modifiers` 注解为 int，实际承载 PyQt6 的 `Qt.KeyboardModifiers` 标志对象
  - 位置: src/features/composite/ui/viewer_tool_system.py:114、141、163
  - 证据:
    ```python
    modifier_keys: int = 0  # Qt.KeyboardModifier flags
    ...
        modifiers: int = 0,
    ```
  - 影响: PyQt6 中 `event.modifiers()` 返回 `Qt.KeyboardModifiers` 标志对象而非 int；运行时按位
    运算兼容，但注解不准确，误导调用方与类型检查器（typing 维度）。
  - 建议: 注解为 `Qt.KeyboardModifier` 类型的 flags（或 `int | Qt.KeyboardModifier` 并在文档注明）。
  - 置信度: 高

### 通过项
- 线程安全：纯 dataclass + 纯函数模块，无跨线程共享可变状态，UI 线程内使用，无 OpenGL 调用
- GPU 资源生命周期：不涉及 ModernGL/纹理/缓冲区
- GLSL/HDR：无着色器代码，无 clamp(0,1) 类 HDR 问题
- 信号防递归：本模块不定义/连接 Qt 信号（不适用）
- 异常路径：无 try/except，不存在宽泛 except 掩盖领域错误
- 性能：命中测试均为线性扫描且规模小（句柄数/node 区域数为十量级），无热点
- undo 支撑结构：original_params/final_params 拷贝语义正确（`start_drag` 与 `set_final_params` 均 `.copy()`）

---

## src/features/composite/ui/viewport3d_controller.py (行数: 152)

### 发现

- [AUD-11409] [Suggestion] `on_mmb_release` 的 `event` 参数未使用；`reset()` 以 `__class__` 间接引用枚举
  - 位置: src/features/composite/ui/viewport3d_controller.py:121-134、80-86
  - 证据:
    ```python
    def on_mmb_release(self, event: "QMouseEvent") -> bool:
        was_click = (self._mmb_press_pos is not None and not self._mmb_is_drag)
    ```
    ```python
    self._state.layout_mode = (
        self._state.layout_mode.__class__.SINGLE)
    ```
  - 影响: `event` 全函数未读（接口误导）；`__class__.SINGLE` 绕道运行时取枚举成员而非直接
    `from .viewport3d_state import ViewportLayoutMode`，可读性与静态检查性差。
  - 建议: 去掉未用参数（或注释保留为接口对称）；直接导入 `ViewportLayoutMode.SINGLE` 赋值。
  - 置信度: 高

### 通过项
- 线程安全：QObject 由 CompositeViewer 在 UI 线程持有（composite_viewport.py:1172-1173），无跨线程访问
- GPU/渲染边界：严格遵守"不直接操作 ModernGL/渲染"，只读写 Viewport3DState 并发信号（符合 audit plan 约束）
- MMB click/drag 判定：未先 press 的 release 不触发 toggle（`_mmb_press_pos is None` → was_click=False），Alt+MMB 永远 pan 不 toggle，边界防御正确
- 信号防递归：`set_tool_mode` 有 `!=` 变更守卫，重复设置同一模式不重复发信号
- 状态提交一致性：纯视图状态不进 undo/不脏化节点图，符合模块头声明的 audit plan 约定
- `handle_key` 修饰键过滤（仅 NoModifier）避免与 Ctrl+S 等冲突；快捷键映射经 TOOL3D_MODE_SHORTCUTS 单一来源
- 异常路径：无 try/except，无掩错风险

---

## src/features/composite/ui/viewport3d_state.py (行数: 382)

### 发现

- [AUD-11410] [Suggestion] PREVIEW 档位文档与参数表不一致：文档写"简化 PBR + 可选 MSAA"，参数与 FINAL 完全相同
  - 位置: src/features/composite/ui/viewport3d_state.py:48-52（对照 80-84）
  - 证据（枚举文档串）:
    ```python
    PREVIEW:      普通静止 viewport。baseColor + ORM + 简化 PBR + 可选 MSAA +
                  全分辨率。
    ```
    对照参数表:
    ```python
    ViewportQualityProfile.PREVIEW: {
        "msaa_factor": 1.0, "use_ibl": True,
        "resolution_scale": 1.0, "pbr_mode": "full",
        "backface_culling": False,
    ```
  - 影响: PREVIEW 档实际等价于 FINAL（full Cook-Torrance + IBL + MSAA 全开），静止预览不会降载；
    消费方 `Scene3DNode._apply_quality_profile()` 按参数表执行，文档承诺的"简化 PBR"不成立。
    大模型场景下静止视口失去一档本应存在的降载层级（性能维度）。
  - 建议: 对齐二者——要么让 PREVIEW 使用 `pbr_mode: "diffuse"`/`use_ibl: False` 等简化参数，要么
    修正文档说明 PREVIEW 目前与 FINAL 相同。
  - 置信度: 中（矛盾确凿；无法从本文件判定是参数遗漏还是文档滞后）

### 通过项
- 纯数据模块：仅 enum + dataclass，无 Qt 控件、无渲染逻辑、无 I/O，符合模块头设计原则
- 线程安全/GPU/GLSL：均不涉及
- 类型注解：完整（含 Optional/tuple 泛型），可变默认值均用 `field(default_factory=...)`，不可变默认用 tuple，正确
- `toggle_layout` 的 QUAD→SINGLE 正交视图保留逻辑（single_ortho_view）与 `effective_view_kind`/`is_ortho_effective` 自洽
- 异常路径：无 try/except，无掩错风险

---

## src/features/composite/ui/viewport3d_toolbar.py (行数: 170)

（文件头已标 DORMANT(2026-08-19 审计)：已实现且有测试，但无生产入口——composite_viewport.py:1336
注释确认 3D 工具已并入统一工具条，不再使用独立 Viewport3DToolbar。以下发现按接线前清理项给出。）

### 发现

- [AUD-11411] [Suggestion] 未使用的导入 `QSize`、`Qt`
  - 位置: src/features/composite/ui/viewport3d_toolbar.py:22
  - 证据:
    ```python
    from PyQt6.QtCore import QSize, Qt, pyqtSignal
    ```
  - 影响: 全文件无 `QSize`/`Qt.` 引用，死导入。
  - 建议: 删除未用导入；或接线时评估整个 DORMANT 文件去留。
  - 置信度: 高

- [AUD-11412] [Suggestion] `_COMPONENT_ENABLED` 注释称 Point/Edge/Polygon "Phase 6 标记 disabled"，实际全部为 True
  - 位置: src/features/composite/ui/viewport3d_toolbar.py:62-68（对照 129-135）
  - 证据:
    ```python
    # 组件模式启用状态（Point/Edge/Polygon 需 mesh picking，Phase 6 标记 disabled）
    _COMPONENT_ENABLED = {
        ComponentMode.OBJECT: True,
        ComponentMode.POINT: True,     # P1-22：BVH face hit → barycentric vertex
        ComponentMode.EDGE: True,      # P1-22：BVH face hit → stable canonical edge
        ComponentMode.POLYGON: True,   # P7-5/P1-22：shared BVH face hit
    }
    ```
  - 影响: 注释与代码矛盾；循环内 `if not enabled: tip += "（待 mesh picking 实现）"` 成为
    永不可达的死分支，误导后续维护者。
  - 建议: 更新注释；删除死分支；129-135 行循环内的注释同步修正。
  - 置信度: 高

- [AUD-11413] [Suggestion] `_buttons`/`_comp_buttons` 使用裸 `dict` 注解
  - 位置: src/features/composite/ui/viewport3d_toolbar.py:94、121
  - 证据:
    ```python
    self._buttons: dict = {}  # Viewer3DToolMode -> QToolButton
    ...
    self._comp_buttons: dict = {}  # ComponentMode -> QToolButton
    ```
  - 影响: 缺类型参数，键值类型只能靠行尾注释传达（typing 维度）。
  - 建议: 注解为 `dict[Viewer3DToolMode, QToolButton]` / `dict[ComponentMode, QToolButton]`。
  - 置信度: 高

### 通过项
- 信号防递归：正确——按钮槽连接在 `clicked` 上，程序化同步走 `setChecked`（只发 `toggled` 不发
  `clicked`），且 `set_tool_mode`/`set_component_mode`/`hide_for_2d` 不会回弹
  `tool_mode_requested`/`component_mode_requested`，无递归风险
- 互斥组：QButtonGroup exclusive 保证单选；默认 SELECT + OBJECT 选中态初始化正确
- 线程安全/GPU/GLSL：纯 UI 控件，UI 线程内使用，不涉及渲染资源
- 异常路径：无 try/except，无掩错风险
- lambda 闭包绑定：`lambda _checked=False, m=mode:` 正确捕获循环变量，无经典 late-binding bug
