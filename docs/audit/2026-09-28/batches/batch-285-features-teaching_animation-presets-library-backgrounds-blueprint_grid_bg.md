# batch-285 — src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg//

审查范围：`src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/build.py`（1 个文件，48 行）。
编号区间：AUD-28500–AUD-28599。
参照语义：`presets/builders.py`、`render/svg_renderer.py`。

## src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/build.py (行数: 48)

### 发现

- [AUD-28500] [Suggestion] `build()` 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/build.py:18
  - 证据:
    ```python
    def build():
        elements = [
            shape_element("bg", 0, 0, 1920, 1080, kind="rect", z=0,
    ```
  - 影响: 返回 dict 无注解，与强类型规范不符。
  - 建议: `def build() -> dict[str, Any]:`。
  - 置信度: 高

- [AUD-28501] [Suggestion] 横向网格线只覆盖画布上部，与纵向线全覆盖的密度不对称
  - 位置: src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/build.py:30-34
  - 证据:
    ```python
    for index in range(1, 5):
        y = index * 180
        elements.append(shape_element(f"grid_h{index}", 0, y, 1920, 2, kind="rect",
                                      z=z, fill="#123055", fill_opacity=0.8))
    ```
  - 影响: 纵向 7 条线以 240px 间距覆盖全宽（0–1920），横向仅 4 条覆盖 y=180–720；边框内 720–1020 区域（约 300px，跨 1.67 个网格周期）无横线，下半部网格明显稀疏，"细网格"观感上下不一致。可能为 range 上界笔误（1080/180=6，range(1,6) 才对称）。
  - 建议: 若非刻意留白，改为 `range(1, 6)` 补齐 y=900 横线（或按边框 60–1020 重新均分）。
  - 置信度: 低（属视觉设计判断，无法排除刻意构图的意图）

### 通过项

- scan_beam 的 y 轨道关键帧 0→72 有序唯一，"ease_in_out" 属合法缓动白名单；key(72,...) 帧号等于 duration=72，由 scene_document 自动扩为 73（builders.py:186-199 文档化行为），非循环预设无害。
- z 序递增（网格 1–11 → frame 12 → scan_beam 13 → corner_mark 14），无层级冲突；扫描带从边框上方扫过符合描述。
- 网格线几何安全：竖线 x=240..1680、横线 y=180..720 均在 1920×1080 画布内。
- `fill_opacity=0.0` + stroke 的 frame 矩形产生描边空框，渲染路径（svg_renderer rect + fill-opacity）支持，组合合法。
- PRESET 为字面量 dict，preset_id 与目录名一致；text_element 参数（size/family/color/z）均合法。
- 全部导入（key/linear_gradient/scene_document/shape_element/text_element/track）均被使用。
- 输出确定性（无随机）；线程安全 / GPU 生命周期 / 信号防递归 / GLSL·HDR 不适用（纯数据构建）。
