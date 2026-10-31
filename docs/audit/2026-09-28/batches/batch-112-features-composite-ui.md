# batch-112 审查报告 — src/features/composite/ui//

- 批次: batch-112 [P1]
- 编号区间: AUD-11200–AUD-11299
- 审查文件: 2 个（1,253 行）
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/composite/ui/viewer3d_settings_section.py (行数: 1015)

### 发现

- [AUD-11200] [Major] hdri_preset 支持任意文件路径但下拉框只有 3 个固定项，值不匹配时 UI 显示残留错项
  - 位置: src/features/composite/ui/viewer3d_settings_section.py:752
  - 证据:
    ```python
    # 文档语义（70-74 行）：文件名/绝对路径 → 该 HDRI 贴图
    "hdri_preset": "",
    # 选项仅 3 个（289-294 行）：("", "studio.hdr", "__sky__")
    # 同步逻辑（752-756 行）：
    hdri_value = str(self._settings["hdri_preset"])
    for i in range(self._hdri_preset.count()):
        if str(self._hdri_preset.itemData(i) or "") == hdri_value:
            self._hdri_preset.setCurrentIndex(i)
            break
    ```
  - 影响: 从已保存项目加载自定义 HDRI 路径时，循环无匹配项且无 else 回退，下拉框停留在旧索引（初始即 0="None"）。用户看到 "None" 但场景实际用自定义 HDRI 渲染——UI 与内部状态不一致；用户误以为没设 HDRI，或一动下拉框就意外覆盖丢失原路径。`_msaa`(746-750) 与 `quad_view_quality`(764-768) 同构逻辑，遇到非法/超集值（如 msaa=16）同样显示残留项。
  - 建议: 同步时若无匹配项，为 combo 动态插入一项显示该自定义路径（或 "Custom"），或至少 setCurrentIndex(0) 并加一个独立的文本标签展示真实值；对 msaa/quad_view_quality 增加值合法性校验并回退默认。
  - 置信度: 高

- [AUD-11201] [Major] 外部持久化设置未做类型校验，畸形值在 load_from_graph → _sync_widgets 抛未捕获异常
  - 位置: src/features/composite/ui/viewer3d_settings_section.py:119
  - 证据:
    ```python
    def merged_viewer3d_scene_settings(settings: Optional[dict] = None) -> dict:
        merged = dict(DEFAULT_VIEWER3D_SCENE_SETTINGS)
        if isinstance(settings, dict):
            merged.update(settings)   # 任意键值原样透传，不校验类型
        return merged
    # 下游消费（675 / 746 行）：
    widget.setValue(float(self._settings[key]))   # key 值为 None/字符串时 TypeError/ValueError
    msaa_value = int(self._settings["msaa"])
    ```
  - 影响: `default_3d_scene` 来自 `_viewer_state`（随项目文件持久化）。手工编辑/旧版本/损坏的项目文件中 `"cam_fov": null` 或 `"msaa": "4x"` 会在 `load_from_graph`（634-638 行，无 try/except）内抛异常并向上传播到 NodePropertiesPanel.show_viewer3d_settings，属性面板打开失败甚至崩溃。违反"外部数据须验证"铁律。
  - 建议: 在 merged_viewer3d_scene_settings 中按 DEFAULT 的键/类型做白名单过滤与强制转换（float/bool/str/int），非法值回退默认；或 _sync_widgets 内对每个键 try 转换并跳过坏值。
  - 置信度: 中（代码路径确凿；实际触发依赖外部畸形数据，调用方是否兜底未在本次范围内确认）

- [AUD-11202] [Suggestion] 整型键（nav_restore_delay_ms / inactive_view_update_rate）经 valueChanged(float) 回写后类型漂移为 float，且 _drag 边界传 int 字面量
  - 位置: src/features/composite/ui/viewer3d_settings_section.py:582
  - 证据:
    ```python
    "nav_restore_delay_ms": 150,          # 默认 dict 中为 int（111 行）
    "inactive_view_update_rate": 8,       # 115 行
    self._drag("nav_restore_delay_ms", 0, 1000, decimals=0)      # 582 行，边界 int 字面量
    self._drag("inactive_view_update_rate", 1, 30, decimals=0)   # 627 行
    # drag_value.py: valueChanged = pyqtSignal(float) —— 交互后 _set_value 存入 150.0 等 float
    ```
  - 影响: 交互一次后这两个键从 int 变 float，与默认 dict 类型不一致；持久化 JSON 里输出 150.0。下游按 ms/FPS 消费场景下功能无碍，仅为类型一致性/可读性问题。`0, 1000, 1, 30` 也不符合项目"浮点字面量显式 .0"惯例（参数签名 minimum: float）。
  - 建议: `_set_value` 时对这两个键做 `int(round(value))` 归一（或 msaa 式 combo），边界字面量改 0.0/1000.0/1.0/30.0。
  - 置信度: 高（事实确定，影响轻微）

- [AUD-11203] [Suggestion] 7 个 _pick_*_color 方法结构完全重复（仅 key 前缀不同）
  - 位置: src/features/composite/ui/viewer3d_settings_section.py:778
  - 证据:
    ```python
    def _pick_bg(self) -> None:
        initial = QColor.fromRgbF(float(self._settings["bg_r"]), ...)
        result = get_color(parent=self, initial=initial)
        if result is None:
            return
        self._settings["bg_r"] = float(result.redF())
        ...  # _pick_viewport_bg/_pick_light_color/_pick_grid_minor_color/
        ...  # _pick_grid_major_color/_pick_render_region_mask_color/
        ...  # _pick_render_region_border_color 共 7 份同构代码（778-916 行）
    ```
  - 影响: ~140 行重复代码，新增颜色项需复制整段；后续若要加 HDR 色彩空间处理或 alpha 通道容易漏改其中一份。
  - 建议: 抽出 `_pick_color(prefix: str, swatch: QPushButton) -> None` 一个 helper，7 个入口一行委托。
  - 置信度: 高

### 通过项
- 线程安全: 纯 UI 线程控件代码，无 OpenGL 调用、无跨线程共享状态
- GPU 资源生命周期: 本文件不创建/持有任何 GPU 资源
- 信号防递归: `_sync_widgets` 用 `_block_emit` 状态锁包住全部程序化 setChecked/setValue/setCurrentIndex，`_set_value` 早退；`reset_to_defaults` 在同步后显式补发一次 settings_changed（文档注释正确）；下游 composite_page._apply_viewer3d_settings 不回调 load_from_graph，无反馈环
- 状态提交一致性: settings_changed 发射 `dict(self._settings)` 浅拷贝快照（值为标量，无共享可变状态）；load_from_graph 只读不回写；reset 会正确传播到 viewer_state（undo/revision 属页面层职责，本文件无相关逻辑需求）
- 异常路径: 无宽泛 except，不掩盖错误；get_color 取消（None）正确早退
- GLSL·HDR: 纯参数面板，无 clamp(0,1) 滥用（_swatch_style 的 0-255 钳制仅用于显示样式，合理）
- 性能模式: 单次拖动 tick 发一次全量 dict 浅拷贝（约 60 个标量键），开销可忽略，符合 params_changed 解耦渲染模式
- 类型注解: 公开方法/函数均有完整类型注解（重复代码问题见 AUD-11203）

---

## src/features/composite/ui/viewer_handle_drawing.py (行数: 239)

### 发现

- [AUD-11204] [Major] 角点手柄被 "_left/_right/_top/_bottom" 后缀分支截获，绘制成竖条，_draw_handle_square 对 crop/reformat 全部手柄不可达
  - 位置: src/features/composite/ui/viewer_handle_drawing.py:141
  - 证据:
    ```python
    for handle_id, view_pos in handles.items():
        h_color = hover_color if handle_id == hover_handle else handle_color
        if handle_id.endswith("_center"):
            _draw_handle_cross(...)
        elif any(handle_id.endswith(e) for e in ["_left", "_right", "_top", "_bottom"]):
            _draw_handle_edge(...)
        else:
            _draw_handle_square(...)
    ```
    而 viewer_tool_system.compute_crop_handles 返回的键为 `crop_top_left/_top_right/_bottom_left/_bottom_right/_top/_bottom/_left/_right/_center`——`"crop_top_left".endswith("_left") == True`。
  - 影响: 四个角点全部走 `_draw_handle_edge`，且 `is_horiz = endswith("_top") or endswith("_bottom")` 对角点为 False，统一绘成 4.8px 宽的细竖条；角点应为可对角拖拽的方块，视觉提示错误且几乎无法辨识/命中，`_draw_handle_square` 成为死代码。注: 本模块标记 DORMANT（无生产入口），接线启用时缺陷即生效。
  - 建议: 先精确匹配角点集合（如 `endswith(("_top_left", "_top_right", "_bottom_left", "_bottom_right"))` 走 square），再做边/中心判断；或对 handle_id 引入显式类型枚举而非后缀猜测。
  - 置信度: 高

- [AUD-11205] [Suggestion] GL 状态恢复不完整且 GL 句柄未验证
  - 位置: src/features/composite/ui/viewer_handle_drawing.py:148
  - 证据:
    ```python
    gl_api.glDisable(GL_BLEND)
    gl_api.glUseProgram(0)
    # 但 glLineWidth(2.0)（130/227 行）、glBindVertexArray(vao)、
    # glEnableVertexAttribArray(pos_loc)、ARRAY_BUFFER 绑定均未恢复；
    # overlay_prog/vao/quad_vbo == 0 时无校验，glGetLocation 返回 -1 后静默出错
    ```
  - 影响: 若调用方在同一帧后续还需叠加绘制（依赖 blend 开启、先前 VAO/线宽状态），状态被本函数改写且不恢复；句柄为 0 时无早退，产生一串静默 GL 错误难以排查。glBufferSubData 均为 64 字节（16×float32），依赖调用方 quad_vbo ≥64 字节的隐含约定。
  - 建议: 函数开头校验 `overlay_prog/vao/quad_vbo` 非零否则 return {}；结束时恢复进入时的 blend/line width/绑定状态（或文档化"调用方必须最后调用本函数"的契约）。
  - 置信度: 中（依赖调用方使用方式；当前无调用方）

- [AUD-11206] [Suggestion] 私有绘制函数缺类型注解、selected_node_id 参数与 GL_TRUE 常量未使用、DORMANT 头注与仓库现状不符
  - 位置: src/features/composite/ui/viewer_handle_drawing.py:154
  - 证据:
    ```python
    def _draw_handle_square(gl_api, view_pos, size, color_loc, pos_loc,
                            quad_vbo, vao, vp_w, vp_h, dpr, color):   # 无注解
        ...
    # selected_node_id: Optional[str] 参数（54 行）函数体内从未使用
    # GL_TRUE = 1（30 行）全文件未使用
    # 头注（1-2 行）声称"已实现且有测试"，但全仓库 grep 无任何
    # import/调用（含 tests/）
    ```
  - 影响: 与项目强类型注解规范不符；死参数/死常量误导维护者；头注的"有测试"已失实，DORMANT 复核结论（是否删除）缺少准确依据。
  - 建议: 补齐三个 `_draw_handle_*` 的参数/返回注解；删除或实现 selected_node_id；删除 GL_TRUE；修正头注为"无测试"。
  - 置信度: 高

### 通过项
- 线程安全: 纯绘制 helper，由调用方在 GL 上下文线程同步调用，自身无共享状态
- GPU 资源生命周期: 不创建任何 GL 对象（复用调用方 prog/vbo/vao），无泄漏路径；会覆写 quad_vbo 内容属既定设计（注释声明与 composite_viewport.py 共用）
- GLSL·HDR 正确性: GL 常量值（GL_ARRAY_BUFFER 0x8892 等）与标准枚举一致；顶点上传 stride=16 与 4 分量布局一致；覆盖层为 UI 颜色（非 HDR 数据），alpha 混合语义正确
- 输入验证: `params`/`node_type` 空值早退（75-76 行），未知 node_type 返回 {}（91-92 行）
- 性能模式: 每 handle 一次 64 字节 numpy 上传，量级（≤9 个手柄/帧）可忽略
- 状态提交一致性 / 异常与取消路径: 纯函数无状态提交需求；无 except 子句故无错误掩盖

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-11200, AUD-11201, AUD-11204 |
| Suggestion | 4 | AUD-11202, AUD-11203, AUD-11205, AUD-11206 |
