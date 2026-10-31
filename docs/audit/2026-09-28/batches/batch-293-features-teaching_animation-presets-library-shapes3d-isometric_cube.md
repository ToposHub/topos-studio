# batch-293 — src/features/teaching_animation/presets/library/shapes3d/isometric_cube/

编号区间：AUD-29300–AUD-29399。审查日期：2026-09-28。

## src/features/teaching_animation/presets/library/shapes3d/isometric_cube/build.py (行数: 50)

### 发现
- [AUD-29301] [Suggestion] build() 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/shapes3d/isometric_cube/build.py:25
  - 证据:
    ```python
    def build():
        y_track = track("y", *_FLOAT)
        return scene_document([
    ```
  - 影响: 违反项目"所有函数有类型注解"的约束（维度：类型注解缺失）；其他 bundled preset 的 build() 同样无注解，IDE/静态检查无法推导返回结构。
  - 建议: 标注为 `def build() -> dict[str, Any]:`（scene_document(...).to_dict() 返回 dict）。
  - 置信度: 高

- [AUD-29302] [Suggestion] 同一 KeyframeTrack 实例被三个立方体面共享，存在可变对象别名隐患
  - 位置: src/features/teaching_animation/presets/library/shapes3d/isometric_cube/build.py:26,41,45,49
  - 证据:
    ```python
        y_track = track("y", *_FLOAT)
    ...
        shape_element("face_left", 960, 170, 600, 490, kind="path", z=2,
                      fill="#2563eb", d=_LEFT,
                      gradient=linear_gradient((0.0, "#1d4ed8"), (1.0, "#1e3a8a")),
                      tracks=(y_track,)),
    ```
    （face_right/face_top 同样引用同一 `y_track` 实例。）
  - 影响: `KeyframeTrack` 是可变 dataclass（src/shared/keyframe.py:142 `add_keyframe` 就地修改 keyframes 列表）。当前评估器只读（render/evaluator.py:63 仅 `get_value`），编辑路径走拷贝（ui/editor_controller.py:237 `KeyframeTrack.from_dict(existing.track.to_dict())`），故现阶段无实际错误；但任何未来对 `local_animation_tracks` 的就地修改（例如直接在 element 的 track 上 add_keyframe）会同时改三个面，造成联动动画缺陷。
  - 建议: 每个元素独立构造 track（或在 builders.shape_element 内对传入 track 做防御性拷贝/冻结）。
  - 置信度: 中

### 通过项
- 线程安全：纯数据构建函数，无共享可变状态、无 Qt/GL 调用。
- GPU 资源生命周期：不涉及。
- GLSL·HDR 正确性：不涉及着色器；不透明度/缩放等数值均在合理范围（fill_opacity 0.45、渐变 opacity 0.5→0.0），无非法 clamp。
- 信号防递归：不涉及 UI。
- 状态提交一致性：scene_document 由 builders 工厂生成，关键帧帧号（0/24/48/72）在 duration_frames=72 边界处由 builders.scene_document 自动扩为 73（builders.py:188-199），验证通过。
- 异常与取消路径：不涉及。
- 输入验证：builders.track 校验 param 合法（y/scale_x/opacity 均在 ANIMATABLE_PROPERTIES）、关键帧有序唯一、插值名合法（linear/ease_in_out）；y 负值 -16.0 不会被 track 钳制（min 自动取 min(values,0.0)）。
- 性能模式：静态数据，无运行时开销问题。
