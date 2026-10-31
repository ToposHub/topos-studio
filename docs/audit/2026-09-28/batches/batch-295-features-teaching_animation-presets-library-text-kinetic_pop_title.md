# batch-295 — src/features/teaching_animation/presets/library/text/kinetic_pop_title/

编号区间：AUD-29500–AUD-29599。审查日期：2026-09-28。

## src/features/teaching_animation/presets/library/text/kinetic_pop_title/build.py (行数: 45)

### 发现
- [AUD-29501] [Suggestion] build() 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/text/kinetic_pop_title/build.py:18
  - 证据:
    ```python
    def build():
        return scene_document([
    ```
  - 影响: 违反项目"所有函数有类型注解"的约束；静态检查无法推导返回结构。
  - 建议: 标注为 `def build() -> dict[str, Any]:`。
  - 置信度: 高

### 通过项
- 线程安全：纯数据构建函数，无共享可变状态。
- GPU 资源生命周期：不涉及。
- GLSL·HDR 正确性：不涉及；glow_effect 参数（radius 22.0、opacity 0.5）合法，双 scale 轨道数值一致（0.5→1.06→1.0）保证等比缩放不变形。
- 信号防递归：不涉及 UI。
- 状态提交一致性：所有关键帧最大帧号 42（caption）< duration_frames=60，无越界；各轨道关键帧有序唯一。
- 异常与取消路径：不涉及。
- 输入验证：track 参数（opacity/y/x/scale_x/scale_y/rotation）均为 ANIMATABLE_PROPERTIES 合法成员；插值名（linear/ease_out/ease_in_out）合法；scale 超过 1.0 的值（1.06）不会被 builders 自动推导的 max_val 钳制（max(hi,1.0)=1.06）。
- 性能模式：静态数据；动画分段（0–8/10–26/30–42）错峰编排合理。
- 图层核对：z=10..14 严格递增无重复；dot_left（x=620）与 dot_right（x=1282）空间上不重叠。
