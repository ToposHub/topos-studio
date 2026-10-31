# batch-286 — src/features/teaching_animation/presets/library/charts/animated_histogram//

审查范围：`src/features/teaching_animation/presets/library/charts/animated_histogram/build.py`（1 个文件，50 行）。
编号区间：AUD-28600–AUD-28699。
参照语义：`presets/builders.py`、`render/evaluator.py`（width/height 可动画，evaluator.py:198-199）。

## src/features/teaching_animation/presets/library/charts/animated_histogram/build.py (行数: 50)

### 发现

- [AUD-28600] [Suggestion] `build()` 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/charts/animated_histogram/build.py:23
  - 证据:
    ```python
    def build():
        elements = [
            text_element("title", 960, 120, "Monthly Active Users", size=64, weight="700",
    ```
  - 影响: 返回 dict 无注解，与强类型规范不符。
  - 建议: `def build() -> dict[str, Any]:`。
  - 置信度: 高

### 通过项

- 柱条"从基线长出"几何正确：y（800→y_final）与 height（0→final_h）同步动画，任意帧底边 y+height 恒为 800，与基线（y=800, 3px, z=32）对齐；width/height 均为可动画属性（builders.py:32-34、evaluator.py:198-199）。
- 时序编排正确：start=6+4i、end=start+18、标签淡入 end→end+8（最大 56 < 72），柱条长成后标签才出现，标签静态置于最终位置无错位；全部关键帧有序唯一且不触发 duration 自动扩展。
- 数值一致性：`f"{value * 100:.0f}"` 产出 38/56/44/80/62/92/72，与副标题"单位：万人"口径一致；_VALUES（7）与 _BAR_COLORS（7）长度匹配，索引不会越界。
- 布局边界安全：末柱右缘 1372+116=1488 < 基线右缘 1600；最高柱顶 800-423.2=376.8，其标签基线 320.8，与副标题（y=196）无重叠。
- track min/max 自动推导（builders.py:59-65）覆盖 0→final_h 值域，不会被默认 [0,1] 截断。
- PRESET 为字面量 dict，preset_id 与目录名一致；导入均被使用；输出确定性（无随机）。
- 线程安全 / GPU 生命周期 / 信号防递归 / GLSL·HDR 不适用（纯数据构建）。
