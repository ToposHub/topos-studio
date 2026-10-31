# batch-287 — src/features/teaching_animation/presets/library/charts/line_curve_chart//

审查范围：`src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py`（1 个文件，66 行）。
编号区间：AUD-28700–AUD-28799。
参照语义：`presets/builders.py`、`core/vector_shapes.py`（_SAFE_PATH_DATA 白名单）、`render/svg_renderer.py`。

## src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py (行数: 66)

### 发现

- [AUD-28700] [Suggestion] `linear_gradient` 导入后未使用
  - 位置: src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py:4
  - 证据:
    ```python
    from src.features.teaching_animation.presets.builders import (
        key, linear_gradient, scene_document, shape_element, text_element, track,
    )
    ```
    全文（第 39–65 行的元素定义）无任何 `gradient=` 调用。
  - 影响: 冗余导入（F401），误导读者以为本预设使用了渐变。
  - 建议: 从导入列表移除 `linear_gradient`。
  - 置信度: 高

- [AUD-28701] [Suggestion] 数据点 scale 弹出绕包围盒左上角，弹出过程中圆心偏移约 10px
  - 位置: src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py:54-61
  - 证据:
    ```python
    elements.append(shape_element(
        f"dot_{index}", px - 13, py - 13, 26, 26, kind="circle", z=33,
        ...
            track("scale_x", key(appear, 0.2), key(appear + 8, 1.0, "ease_out")),
            track("scale_y", key(appear, 0.2), key(appear + 8, 1.0, "ease_out")),
    ```
    而渲染端变换为 `translate(x y) rotate(r) scale(sx sy)`（svg_renderer.py:241-245），circle 圆心在局部 (r, r)（svg_renderer.py:331-334），无绕中心缩放的补偿。
  - 影响: scale=0.2 时圆心位于 (px-10.4, py-10.4)，随弹出逐渐滑向数据点，至 scale=1.0 才对齐；8 帧弹出期间点位轻微左上偏移，属视觉瑕疵而非渲染错误。
  - 建议: 改用 kind="path" 且 d 绕局部原点 (0,0) 定义的圆（旋转/缩放即绕圆心，参见 pie_donut_chart 做法），或渲染层引入 transform-origin 中心语义。
  - 置信度: 中（机制已核实；视觉影响幅度小）

- [AUD-28702] [Suggestion] `_smooth_path` 参数与 `build()` 均缺类型注解
  - 位置: src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py:21, 33
  - 证据:
    ```python
    def _smooth_path(points) -> str:
        parts = [f"M {points[0][0]} {points[0][1]}"]
    ...
    def build():
    ```
  - 影响: `points` 无注解（实际为 `Sequence[tuple[float, float]]`），`build()` 返回 dict 无注解，与强类型规范不符。
  - 建议: 补齐参数与返回类型注解。
  - 置信度: 高

### 通过项

- `_smooth_path` 数学无奇异：仅用差值与常系数（0.42/0.08），无除法无除零风险；输出 M/C 命令及面积路径的 L/Z 均在 `_SAFE_PATH_DATA` 白名单（vector_shapes.py:22）内，负坐标字符亦允许，sanitize 不会清空路径。
- 面积路径闭合正确：line_path + L(末点→BASE_Y) + L(首点→BASE_Y) + Z，形成曲线下填充区。
- 几何边界安全：数据点 x 400–1540 落在 x 轴 (360,1240 宽) 范围内，y 384–760 落在 y 轴 300–820 内；x 轴标签 y=852 位于轴下方不重叠。
- 全部轨道属性（opacity/scale_x/scale_y）可动画；关键帧有序唯一且最大帧 38 < 72，不触发 duration 扩展。
- 淡入编排合理：title 0–10 → line 4–22 → area 8–26 → 各数据点 appear=10+4i 依次弹出，节奏不冲突。
- 6 个 x 轴标签与 6 个数据点一一对应（两个列表长度一致，索引安全）。
- PRESET 为字面量 dict，preset_id 与目录名一致；`math` 未导入也不需要（纯 f-string 格式化）；输出确定性。
- 线程安全 / GPU 生命周期 / 信号防递归 / GLSL·HDR 不适用（纯数据构建）。
