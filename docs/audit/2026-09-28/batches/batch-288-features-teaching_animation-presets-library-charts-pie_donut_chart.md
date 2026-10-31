# batch-288 — src/features/teaching_animation/presets/library/charts/pie_donut_chart//

审查范围：`src/features/teaching_animation/presets/library/charts/pie_donut_chart/build.py`（1 个文件，73 行）。
编号区间：AUD-28800–AUD-28899。
参照语义：`presets/builders.py`、`core/vector_shapes.py`（_SAFE_PATH_DATA）、`render/svg_renderer.py`（transform 与 path 渲染）。

## src/features/teaching_animation/presets/library/charts/pie_donut_chart/build.py (行数: 73)

### 发现

- [AUD-28800] [Suggestion] `build()` 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/charts/pie_donut_chart/build.py:39
  - 证据:
    ```python
    def build():
        cx, cy = _CENTER
        elements = []
    ```
  - 影响: 返回 dict 无注解，与强类型规范不符。
  - 建议: `def build() -> dict[str, Any]:`。
  - 置信度: 高

### 通过项

- 旋转轴心几何正确（关键项）：扇形为 kind="path"、元素位于 (cx,cy)=(960,540)，`_arc_path` 的 d 坐标绕局部原点 (0,0) 定义；渲染变换为 `translate(x y) rotate(r) scale(...)`（svg_renderer.py:241-245），rotate 作用于平移后的原点，即饼心 (960,540)——"扇形绕圆心旋入"的实现正确（对比 rect/circle 绕左上角旋转的语义）。
- 图例百分比与扇区跨度一致：132/360≈36.7%↔"37%"、118/360≈32.8%↔"33%"、70/360≈19.4%↔"19%"、40/360≈11.1%↔"11%"，四扇区首尾相接 (0→360) 无缝无重叠。
- 全部扇区 span < 180°，large-arc 标志恒为 0，计算正确；A 弧命令与负坐标均在 `_SAFE_PATH_DATA` 白名单（vector_shapes.py:22）内，sanitize 不会清空路径。
- rotation 轨道负值起点（如 -54.4°）由 builders.track 自动下扩 min_val（builders.py:60-65），不会被 KeyframeTrack 默认 [0,1] 截断。
- 关键帧有序唯一：appear=4+5i 递增，appear+10 / appear+16 交错不冲突；最大帧 38 < 72，不触发 duration 扩展。
- 层级编排正确：扇形 z=10–13，中心遮盖圆 hole z=20 形成环形，中心文本 z=21–22，标题 z=30，图例 z=31–32；扇形半径 290（最远 y=830）不与图例（y≥912）重叠。
- 遮盖圆几何对齐：hole (cx-118, cy-118, 236, 236) 的圆心恰为 (960,540)=饼心。
- `import math` 被使用；`_arc_path` 注解完整；PRESET 为字面量 dict，preset_id 与目录名一致；导入均被使用；输出确定性。
- 线程安全 / GPU 生命周期 / 信号防递归 / GLSL·HDR 不适用（纯数据构建）。
