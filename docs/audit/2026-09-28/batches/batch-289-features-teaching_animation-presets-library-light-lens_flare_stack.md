# batch-289 — src/features/teaching_animation/presets/library/light/lens_flare_stack//

审查范围：`src/features/teaching_animation/presets/library/light/lens_flare_stack/build.py`（1 个文件，60 行）。
编号区间：AUD-28900–AUD-28999。
参照语义：`presets/builders.py`、`render/svg_renderer.py`（`_transform_attributes`、circle 局部坐标渲染）。

## src/features/teaching_animation/presets/library/light/lens_flare_stack/build.py (行数: 60)

### 发现

- [AUD-28900] [Major] 呼吸缩放绕包围盒左上角，主光斑/光环中心随脉动对角漂移约 ±20px
  - 位置: src/features/teaching_animation/presets/library/light/lens_flare_stack/build.py:27-30, 36-39
  - 证据:
    ```python
    shape_element("halo_big", 560, 220, 640, 640, kind="circle", z=0, ...
                  tracks=(
                      track("scale_x", key(0, 1.0), key(26, 1.07, "ease_in_out"),
                            key(52, 0.98, "ease_in_out"), key(72, 1.0, "ease_in_out")),
                      track("scale_y", key(0, 1.0), key(26, 1.07, "ease_in_out"),
    ```
    渲染端变换为 `translate(x y) rotate(r) scale(sx sy)`（svg_renderer.py:241-245），circle 圆心在局部 (r, r)（svg_renderer.py:331-334），缩放直接乘局部坐标、无绕中心补偿。
  - 影响: halo_big 圆心基点 (880,540)，scale 1.0→1.07 时圆心漂至 (902.4,562.4)、0.98 时 (873.6,533.6)，即主光斑每次呼吸沿对角线往返 ±22px；ring_teal（(700,360,380)）圆心漂移约 -19~+15px。而 core 元素只有 opacity 轨道、位置固定于 (890,550)，脉动期间主光斑/光环与中心亮点错位，"整体呼吸脉动"退化为漂移摆动，偏离预设描述的稳定光轴效果。
  - 建议: 仿照 pie_donut_chart 用 kind="path"、d 绕局部原点 (0,0) 定义圆盘（此时 scale/rotate 恰绕圆心）；或改为成对动画 x/y 与 width/height 保持圆心不动；或在渲染层引入绕中心的 transform-origin 语义（影响面大，需单独立项）。
  - 置信度: 中（漂移机制在渲染器代码中已核实为必然；视觉严重度属判断）

- [AUD-28901] [Suggestion] `build()` 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/light/lens_flare_stack/build.py:18
  - 证据:
    ```python
    def build():
        return scene_document([
    ```
  - 影响: 返回 dict 无注解，与强类型规范不符。
  - 建议: `def build() -> dict[str, Any]:`。
  - 置信度: 高

### 通过项

- 全部轨道属性（scale_x/scale_y/opacity）在 ANIMATABLE_PROPERTIES 内；关键帧帧号有序唯一（halo 0/26/52/72，ring 0/36/72，sparks 各自递增）。
- `key(72, ...)` 帧号等于 duration=72，由 scene_document 自动扩为 73（builders.py:186-199 文档化行为）；本预设未声明循环，播放一次即止，无回绕瑕疵。
- radial_gradient 的 stops 与 opacities 长度匹配（halo 3/3，其余 2/2）；`blur_effect(6.0)` 参数为 float 字面量（显式 .0，符合规范）。
- 元素排布核对：halo_big 圆心 (880,540)、ring_teal (890,550)、core (890,550) 基本共轴；三个 spark 沿右下对角分布，与"沿对角线排布"描述相符。
- opacity 轨道值域均在 [0,1]；scale 值均为正数，不会被 evaluator 的 max(0.0001, ...) 钳制（evaluator.py:205-206）。
- PRESET 为字面量 dict，preset_id 与目录名一致；导入均被使用；输出确定性（无随机）。
- 线程安全 / GPU 生命周期 / 信号防递归 / GLSL·HDR 不适用（纯数据构建）。
