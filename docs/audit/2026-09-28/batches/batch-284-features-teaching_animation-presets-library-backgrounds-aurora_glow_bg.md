# batch-284 — src/features/teaching_animation/presets/library/backgrounds/aurora_glow_bg//

审查范围：`src/features/teaching_animation/presets/library/backgrounds/aurora_glow_bg/build.py`（1 个文件，48 行）。
编号区间：AUD-28400–AUD-28499。
参照语义：`presets/builders.py`（track/key/scene_document 契约）、`render/evaluator.py`、`render/svg_renderer.py`。

## src/features/teaching_animation/presets/library/backgrounds/aurora_glow_bg/build.py (行数: 48)

### 发现

- [AUD-28400] [Suggestion] `build()` 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/backgrounds/aurora_glow_bg/build.py:18
  - 证据:
    ```python
    def build():
        elements = [
            shape_element("bg_plate", 0, 0, 1920, 1080, kind="rect", z=0,
    ```
  - 影响: 返回 `scene_document(...).to_dict()`（dict）但无注解，与项目强类型规范（.clinerules）不符；同目录其余 build.py 同样缺失。
  - 建议: 改为 `def build() -> dict[str, Any]:`（或 SceneDocument dict 别名）。
  - 置信度: 高

### 通过项

- 轨道属性合法：`x`/`y`/`opacity` 均在 builders.ANIMATABLE_PROPERTIES（builders.py:32-34）内，不会触发 ValueError。
- 关键帧帧号 0/48/96 严格递增且唯一，满足 builders.track 的有序唯一校验（builders.py:56-58）；等距分布无速度突变。
- 缓动全部使用默认 "linear"，属于 _INTERPOLATIONS 白名单（builders.py:23-30）。
- `key(96, ...)` 帧号等于 duration_frames=96，由 scene_document 自动扩为 97（builders.py:186-199 文档化行为），且首末帧值一致（240.0/240.0 等），循环回绕无缝。
- PRESET 为字面量 dict，满足 loader 的 ast.literal_eval 解析契约（presets/library.py:81-101）；preset_id "aurora_glow_bg" 与目录名一致。
- 全部导入（key/radial_gradient/linear_gradient/scene_document/shape_element/track）均被使用，无冗余导入。
- 输出确定性：无随机源，每次 build 结果一致，利于预览缓存与测试。
- glow_violet 底边（560+640=1200）超出 1080 画布，但外层 SVG 根默认裁剪 viewBox 外内容，背景光斑溢出属预期表现，非缺陷。
- 线程安全 / GPU 资源生命周期 / 信号防递归 / GLSL·HDR：纯数据构建函数，一次执行产出 dict，不适用。
