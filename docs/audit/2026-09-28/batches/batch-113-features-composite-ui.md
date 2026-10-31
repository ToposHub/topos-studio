# Batch 113 — src/features/composite/ui（viewer_handles.py）

审查日期：2026-09-28 ｜ 编号区间：AUD-11300–AUD-11399
模块性质：纯几何/参数计算模块（手柄位置计算、拖拽增量参数计算），无 Qt 控件、无信号、无 OpenGL 调用。
跨文件验证：调用方（composite_page.py / composite_viewport.py / edit/ui 两个 controller）、
TransformNode._build_transform_matrix、mask 渲染链（filter_evaluator._apply_mask → shaders.py _MASK_FRAGMENT →
base_evaluator._render_quad 四边形 uv 与 NDC 同向）、RotoShapeNode/MaskNode ParamDef、
本项目 .venv 下 PyQt6 枚举位运算实测（`Qt.KeyboardModifier & int` 抛 TypeError）。

## src/features/composite/ui/viewer_handles.py (行数: 2087)

### 发现

- [AUD-11300] [Major] Mask/Roto 边缘缩放的“对边固定”中心补偿公式错误（dx 的二次式而非线性），对边随拖拽漂移
  - 位置: src/features/composite/ui/viewer_handles.py:1709
  - 证据:
    ```python
    scale_delta = -dx / orig_half_w_px
    new_w = max(0.01, orig_w + orig_w * scale_delta)
    new_params["width"] = new_w
    # Adjust center to keep right edge fixed
    new_params["center_x"] = orig_cx + dx_norm * (1.0 - new_w / orig_w) / 2.0
    ```
  - 影响: 数值验证：image_width=1000、orig_w=1.0、orig_cx=0.5，左缘手柄向左拖 dx=−100 →
    new_w=1.2，正确中心应为 0.4（右缘保持 1.0），代码给出 0.5+0.1·0.1=0.501，右缘滑到 1.101——
    即左缘缩放时右缘不固定，形状整体多长 10%；WIDTH_R(1718)、HEIGHT_T(1727)、HEIGHT_B(1736) 同一公式镜像同错。
    另外角点缩放（1738-1783）完全不做中心补偿，形状绕中心缩放、对角也移动，与边缘手柄的锚定意图不一致。
  - 建议: 正确公式为线性：WIDTH_L/HEIGHT_T 场景 `new_center = orig_c + (orig_size - new_size) / 2`，
    WIDTH_R/HEIGHT_B 为 `orig_c + (new_size - orig_size) / 2`（等价于 orig_c ± dx_norm）；角点按对角锚定补偿两轴。
  - 置信度: 高

- [AUD-11301] [Major] Feather 手柄外法线用 R(−θ) 旋转，与形状角点/边缘的 R(+θ) 相反，旋转后的 Mask 羽化手柄方向镜像错位
  - 位置: src/features/composite/ui/viewer_handles.py:1272
  - 证据:
    ```python
    # Apply rotation to corners
    rx = cx_rel * cos_r - cy_rel * sin_r          # ← 角点用 R(+θ)（NDC Y-up，屏幕逆时针）
    ry = cx_rel * sin_r + cy_rel * cos_r
    ...
    # Edge normals (pointing outward from shape)
    top_normal = (sin_r, cos_r)      # ← R(−θ)·(0,1)；R(+θ)·(0,1) 应为 (−sin_r, cos_r)
    bottom_normal = (-sin_r, -cos_r)
    left_normal = (-cos_r, sin_r)    # ← R(−θ)·(−1,0)；R(+θ) 应为 (−cos_r, −sin_r)
    right_normal = (cos_r, -sin_r)
    ```
  - 影响: 同一函数内形状用 +θ、法线用 −θ：任何 rotation≠0 的 Mask/Roto，四个羽化手柄的偏移方向相对形状
    镜像（如 θ=90° 时 top 羽化手柄落到形状右侧即内部/后方而非外侧），手柄绘制与命中位置均错。
  - 建议: 法线与角点用同一旋转矩阵：`top=(-sin,cos) bottom=(sin,-cos) left=(-cos,-sin) right=(cos,sin)`。
  - 置信度: 高

- [AUD-11302] [Major] Mask/Roto 旋转手柄用绝对角度且手柄恒在 bbox 正上方：对已旋转形状发起旋转拖拽会瞬间跳回 ~0°
  - 位置: src/features/composite/ui/viewer_handles.py:1688
  - 证据:
    ```python
    rel_x = drag_current_image_pos[0] - center_px_x
    rel_y = drag_current_image_pos[1] - center_px_y
    rad = math.atan2(rel_y, rel_x)
    # Convert to degrees, offset by 90 (handle starts above center)
    new_rotation = math.degrees(rad) + 90.0
    ...
    new_params["rotation"] = new_rotation      # 完全丢弃 orig_rot，无拖拽起点基线
    ```
    而手柄位置恒为正上方（1256-1257）：`rotate_pos = (center_ndc_x, bbox_y1 + rotate_offset_ndc)`。
  - 影响: rotation=90° 的 Mask，其旋转手柄仍在 bbox 正上方；按下并轻微移动的瞬间，
    鼠标相对中心的角度≈“正上方”→ new_rotation≈0°，形状猛跳回 0°，当前旋转值被破坏
    （拖拽起点 drag_start_image_pos 可用但 ROTATE 分支未使用）。同文件 Transform 旋转手柄随形状旋转
    （417-434），绝对角度因此连续；Mask 无此性质。
  - 建议: `rotation = wrap(orig_rot + angle(cur) - angle(drag_start))`，用拖拽起点角度做基线；
    或让旋转手柄随形状旋转（Transform 做法）。
  - 置信度: 高

- [AUD-11303] [Major] Mask/Roto 旋转拖拽方向疑似与渲染/手柄旋转方向相反（与同文件 Transform 约定不一致）
  - 位置: src/features/composite/ui/viewer_handles.py:1693
  - 证据:
    ```python
    rad = math.atan2(rel_y, rel_x)          # rel 为 Y-down 图像坐标 → 该角度随鼠标顺时针增大
    new_rotation = math.degrees(rad) + 90.0  # φ=“自上方起顺时针角”：上=0，右=+90
    ```
    而手柄/渲染侧 +θ 为屏幕逆时针：compute_mask_handles 在 NDC(Y-up) 用标准矩阵 R(+θ)（1211-1214）；
    shader 侧 _MASK_FRAGMENT 将采样点旋转 +θ（形状等效 −θ，经显示 Y 翻转后在屏幕上亦为逆时针），
    与中心映射的 Y 翻转约定（1188：`ndc_y1 - center_y * ndc_h`）一致。对比 Transform 的正确实现（954）：
    `rad = math.atan2(dx, -dy)` 配“渲染顺时针为正”。
  - 影响: 若中心/形状映射正确（基本功能，可信），则顺时针拖拽旋转手柄会产生逆时针的形状旋转——
    与 Transform 手柄行为相反，形状不跟随鼠标转动方向。存在显示翻转次数假设，未能从本文件完全闭环。
  - 建议: 统一为“鼠标顺时针角 → 渲染顺时针旋转”，与 compute_transform_delta_params 的
    atan2(dx, −dy) 约定对齐后实测验证。
  - 置信度: 中

- [AUD-11304] [Major] Feather 拖拽 T/L 方向与 B/R 相反（且与自身注释矛盾）：向外拖反而减小羽化
  - 位置: src/features/composite/ui/viewer_handles.py:1800
  - 证据:
    ```python
    if handle_type == MaskHandleType.FEATHER_T:
        # Moving up (negative Y) increases feather
        sign = -1.0 if dy < 0 else 1.0      # 向上(外) → sign=−1 → 羽化减小，与注释相反
    elif handle_type == MaskHandleType.FEATHER_B:
        sign = 1.0 if dy > 0 else -1.0      # 向下(外) → +1 → 羽化增大 ✓
    elif handle_type == MaskHandleType.FEATHER_L:
        sign = -1.0 if dx < 0 else 1.0      # 向左(外) → 减小（同 T 一样反）
    elif handle_type == MaskHandleType.FEATHER_R:
        sign = 1.0 if dx > 0 else -1.0      # 向右(外) → 增大 ✓
    ```
  - 影响: 羽化手柄绘制在形状外侧（1278-1293，沿外法线偏移 feather），顶/左两个手柄向外拖会减小羽化、
    手柄随后向内移动，与鼠标对抗；同一工具内四个手柄方向不一致。FEATHER_T 的注释明确说“向上增加羽化”，代码相反。
  - 建议: T/L 取与 B/R 相同的“向外=+1”符号；用外法线方向的投影而不是逐手柄硬编码符号。
  - 置信度: 高

- [AUD-11305] [Major] Roto opacity 手柄拖拽方向与两处行内注释相反（代码：向下=减小；注释：向下=增大）
  - 位置: src/features/composite/ui/viewer_handles.py:1871
  - 证据:
    ```python
    # Higher Y = higher opacity (Y-down in image coords)
    ...
    # Moving up decreases opacity, moving down increases
    dy = drag_current_image_pos[1] - drag_start_image_pos[1]
    opacity_delta = -dy / image_height  # Negative because Y-down
    new_opacity = max(0.0, min(1.0, orig_opacity + opacity_delta * 2.0))
    ```
  - 影响: Y-down 下 dy>0（向下）→ opacity_delta<0 → 不透明度减小，与 1866、1870 两处注释（向下=增大）相反；
    手柄绘制在 bbox 下方（1376-1379），用户向下拖期望增大却减小（或注释错、方向语义未定义）。
  - 建议: 明确语义后统一符号；若“向下=增大”为设计意图，去掉负号。
  - 置信度: 中（注释与代码必有一错，哪边是设计意图无法从本文件判定）

- [AUD-11306] [Major] compute_mask/roto_delta_params 未归一化 modifiers：与 transform 版本同样的 PyQt6 枚举 TypeError 隐患，且 Shift 均匀缩放实际失效
  - 位置: src/features/composite/ui/viewer_handles.py:1741
  - 证据:
    ```python
    # compute_transform_delta_params 中已有防御（884-886）：
    if not isinstance(modifiers, int):
        modifiers = int(getattr(modifiers, "value", 0))
    # 但 mask 版本直接位测试（1741）：
    uniform = (modifiers & 0x2000000) != 0  # Shift for uniform scale
    ```
    本项目 .venv 实测：`Qt.KeyboardModifier.ShiftModifier & 0x2000000` →
    `TypeError: unsupported operand type(s) for &: 'KeyboardModifier' and 'int'`。
    viewer_tool_system.modifier_keys 虽声明 `int`，实际被赋 Qt 枚举（composite_viewport.py:1273/8063，
    `modifiers = self._pending_drag_modifiers`，即 `event.modifiers()`）。
  - 影响: 任何按 transform 同款方式把 Qt 枚举传入 mask/roto 增量函数的调用路径都会在拖拽中抛 TypeError
    （transform 正是因此崩溃过才加了防御，见 880-885 注释）；当前 composite_page 的 mask/roto 路径
    因 `_handle_mask_move/_handle_roto_move` 调 update_drag 不带 modifiers（每次归零为 int 0）而侥幸不崩，
    代价是 mask 角点的 Shift 均匀缩放永不触发（modifier_keys 恒 0）。
  - 建议: 把 884-886 的归一化提取为模块级 helper，在 compute_mask_delta_params /
    compute_roto_delta_params（及签名 `modifiers: int` 与实际枚举不符处）同样应用。
  - 置信度: 高

- [AUD-11307] [Suggestion] 六个导出无任何调用方（死代码）：定义生成器/命中测试/cornerpin 增量与真实实现双轨漂移
  - 位置: src/features/composite/ui/viewer_handles.py:557
  - 证据: 全库 grep（排除本文件）：
    `get_transform_handle_definitions`(557)、`get_mask_handle_definitions`(1406，仅被 get_roto 调)、
    `get_roto_handle_definitions`(1525)、`find_nearest_handle`(805)、`hit_test_handle_screen`(782)、
    `compute_cornerpin_delta_params`(1031) 均为 0 调用；composite_viewport 用自建 dict/命中逻辑，
    CornerPin 拖拽增量在 composite_page 内联实现。
  - 影响: 颜色/半径/命中半径等常量与真实绘制路径（composite_viewport 自绘）双份维护，修一处漏一处；
    compute_cornerpin_delta_params 与页内实际公式并存易被误用。
  - 建议: 删除或显式标注 dormant（参照 viewer_handle_drawing.py 的 DORMANT 标头做法）。
  - 置信度: 高

- [AUD-11308] [Suggestion] 拖拽热路径上每次调用都重建枚举值列表做成员检查
  - 位置: src/features/composite/ui/viewer_handles.py:913
  - 证据:
    ```python
    handle_type = TransformHandleType(handle_id) if handle_id in [h.value for h in TransformHandleType] else None
    ```
    同模式还有 1057、1672、1858（均为每次 mouse-move 触发的 delta 计算）。
  - 影响: 每次鼠标移动事件都构造 11-15 元素列表并线性扫描，纯浪费；也掩盖了“应预建 frozenset”的意图。
  - 建议: 模块级 `_TRANSFORM_HANDLE_IDS = frozenset(h.value for h in TransformHandleType)`（各枚举同理）。
  - 置信度: 高

- [AUD-11309] [Suggestion] 多边形半径手柄在竖幅（H>W）画面上不跟手：归一化用 max(W,H)、手柄绘制用宽度半轴
  - 位置: src/features/composite/ui/viewer_handles.py:1823
  - 证据:
    ```python
    radius_norm = dist / max(image_width, image_height)
    new_params["width"] = radius_norm * 2.0
    new_params["height"] = radius_norm * 2.0
    ```
    而手柄绘制在宽度半轴处（1302：`radius_rel = (shape_w_ndc, 0.0)`，shape_w_ndc = width·ndc_w/2）。
  - 影响: 横幅（W≥H）时 max=W，跟手精确；竖幅时手柄位置 = dist·W/H < 鼠标距离，拖拽不跟手。
    另 shader 侧多边形半径取 min(u_size.x,u_size.y)*0.5（shaders.py _MASK_FRAGMENT），基准又不同。
  - 建议: 半径归一化基准与手柄/渲染统一（统一用短边或宽度）。
  - 置信度: 中

- [AUD-11310] [Suggestion] Shift 语义在 Transform 与 Mask 角点之间相反：Transform 默认等比、Shift 解锁；Mask 默认自由、Shift 等比
  - 位置: src/features/composite/ui/viewer_handles.py:1012
  - 证据:
    ```python
    # transform（1012）：默认角点等比
    if affects_x and affects_y and not (modifiers & SHIFT_MASK):
        avg = (sx_new + sy_new) / 2.0
    # mask（1741）：默认自由，Shift 才等比
    uniform = (modifiers & 0x2000000) != 0  # Shift for uniform scale
    ```
  - 影响: 同一软件内两种变换工具的修饰键行为相反，用户心智负担；两处 docstring 均自洽，属约定不统一。
  - 建议: 与产品确认后统一（建议向 Transform 的 Fusion 式默认看齐）。
  - 置信度: 高（不一致是事实，是否属设计待确认）

- [AUD-11311] [Suggestion] 文档字符串陈旧：声称 RotoShapeNode feather 范围 0-1，实际节点与代码钳制均为 0-0.5
  - 位置: src/features/composite/ui/viewer_handles.py:1166
  - 证据:
    ```python
    - feather: Normalized (0-0.5 for MaskNode, 0-1 for RotoShapeNode)
    ```
    roto_shape_node.py:47：`ParamDef("feather", "float", 0.0, min_val=0.0, max_val=0.5, ...)`；
    本文件 1810 也钳到 0.5：`min(0.5, ...)`。1355 处 docstring 同样写 "(0-1 for RotoShapeNode)"。
  - 影响: 误导后续维护（以为 roto 可到 1.0），无运行时影响。
  - 建议: 更新两处 docstring 为 0-0.5。
  - 置信度: 高

- [AUD-11312] [Suggestion] ndc_to_screen_pixels / screen_pixels_to_ndc 的 DPR 乘除相互抵消，DPR 形同虚设
  - 位置: src/features/composite/ui/viewer_handles.py:742
  - 证据:
    ```python
    vp_w = viewport_width * device_pixel_ratio
    ...
    px_x = (ndc_pos[0] + 1.0) * vp_w / 2.0 / device_pixel_ratio
    ```
  - 影响: 化简后 px_x = (ndc+1)·viewport_width/2，device_pixel_ratio 完全抵消；
    正反变换互逆所以当前无功能错误，但代码暗示“处理了 DPR”，易误导后续修改。
  - 建议: 删除无效乘除，或注释说明 DPR 在此约定下无需参与（逻辑像素口径）。
  - 置信度: 高

### 通过项
- 线程安全：纯函数 + 不可变 dataclass，无共享可变状态、无 Qt 控件操作、无 OpenGL 调用（仅 QPointF/数学），可安全跨线程调用
- GPU 资源生命周期：不涉及任何 ModernGL/GPU 资源
- GLSL/HDR 正确性：本文件无 GLSL；颜色常量为归一化 RGBA 元组；无 HDR 数据被无理由 clamp
- 信号防递归：无信号定义
- Transform 手柄与渲染一致性：to_ndc 的 S→R→T 顺序与 TransformNode._build_transform_matrix（transform_node.py:87）逐项核对一致；中心/枢轴手柄在 scale/rotation 下 1:1 跟手（枢轴为变换不动点）
- 除零防护：compute_constant_delta_params 有 `<=0` 守卫（2054）；compute_transform_handles 对 canvas 尺寸守卫（328-331、342-343）；mask/roto 的 image_width/height 除法当前调用方以 max(1, ...) 钳制（composite_page.py:9817-9830），无活跃 ZeroDivisionError 路径
- 异常路径：compute_constant_delta_params 的 `except ValueError`（2057-2060）为窄捕获并返回空 dict，符合“不得宽泛 except”铁律；compute_transform_delta_params 的 modifiers 归一化（884-886）正确防御 PyQt6 严格枚举
- 浮点字面量：全文件数字均显式小数（含 60/255.0 写法），无 int/float 混用
- 类型注解：所有公共函数签名完整注解（`modifiers: int` 与实际枚举入参的不符已计入 AUD-11306）
