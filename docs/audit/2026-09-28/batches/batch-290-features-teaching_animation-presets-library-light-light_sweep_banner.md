# batch-290 — src/features/teaching_animation/presets/library/light/light_sweep_banner//

审查范围：`src/features/teaching_animation/presets/library/light/light_sweep_banner/build.py`（1 个文件，41 行）。
编号区间：AUD-29000–AUD-29099。
参照语义：`presets/builders.py`、`render/svg_renderer.py`。

## src/features/teaching_animation/presets/library/light/light_sweep_banner/build.py (行数: 41)

### 发现

- [AUD-29000] [Suggestion] `build()` 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/light/light_sweep_banner/build.py:18
  - 证据:
    ```python
    def build():
        return scene_document([
    ```
  - 影响: 返回 dict 无注解，与强类型规范不符。
  - 建议: `def build() -> dict[str, Any]:`。
  - 置信度: 高

### 通过项

- 扫光时序编排正确：beam 的 x 轨道 -420→1980（帧 6–60）完整横扫 1920 画布；opacity 五个关键帧 0/6/14/52/60 有序唯一，首尾为 0（扫入前与扫出后不可见），且 0→6 保持 0 与 x 轨道起点 (帧 6) 同步，不会出现"光斑未动先闪"。
- 静态 rotation=-18.0 绕元素左上角（svg_renderer.py:241-245 语义）：由于倾角固定且 x 平移覆盖全宽、beam 高 480 足以覆盖 panel 高 400，轴心差异不影响扫光效果，非缺陷。
- 渐变组合合法：linear_gradient 3 stops 与 opacities 3 元素长度匹配，中间峰值 0.85 形成扫光亮带；`blur_effect(10.0)` 参数为 float 字面量。
- 布局核对：panel (160,340,1600,400) 覆盖标题基线 448 与副标题基线 580（文本均落在 340–740 面板内）；beam z=8 高于标题 z=5，光带从文字上方扫过符合描述。
- 全部关键帧最大帧号 60 < 72，不触发 duration 自动扩展；轨道属性（x/opacity）均可动画。
- PRESET 为字面量 dict，preset_id 与目录名一致；导入均被使用；输出确定性（无随机）。
- 线程安全 / GPU 生命周期 / 信号防递归 / GLSL·HDR 不适用（纯数据构建）。
