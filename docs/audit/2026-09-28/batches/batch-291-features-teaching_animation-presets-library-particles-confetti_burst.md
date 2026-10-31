# batch-291 — src/features/teaching_animation/presets/library/particles/confetti_burst//

审查范围：`src/features/teaching_animation/presets/library/particles/confetti_burst/build.py`（1 个文件，41 行）。
编号区间：AUD-29100–AUD-29199。
参照语义：`presets/builders.py`、`render/svg_renderer.py`。

## src/features/teaching_animation/presets/library/particles/confetti_burst/build.py (行数: 41)

### 发现

- [AUD-29100] [Suggestion] `build()` 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/particles/confetti_burst/build.py:21
  - 证据:
    ```python
    def build():
        rng = random.Random(7)
    ```
  - 影响: 返回 dict 无注解，与强类型规范不符。
  - 建议: `def build() -> dict[str, Any]:`。
  - 置信度: 高

- [AUD-29101] [Suggestion] 旋转绕矩形左上角而非形心，下落彩带绕角点"公转"
  - 位置: src/features/teaching_animation/presets/library/particles/confetti_burst/build.py:32-36
  - 证据:
    ```python
    f"confetti_{index}", x, -80.0, 16, 26, kind="rect", z=index,
    fill=color, corner_radius=3.0, rotation=rng.randrange(0, 90),
    tracks=(
        track("y", key(start, -80.0), key(end, fall, "ease_in")),
        track("rotation", key(start, 0.0), key(end, spin)),
    ```
    渲染端 rotate 作用于平移后的元素原点 (x,y)（svg_renderer.py:241-245），rect 从局部 (0,0) 画起，无绕形心补偿。
  - 影响: 16×26 矩形对角线约 31px，旋转 360–720° 期间每片彩带绕自身左上角扫出一个 ~31px 半径的圆，观感更接近"摆动/甩动"而非纸屑就地翻滚；属视觉风格瑕疵而非渲染错误，对庆祝场景可能反而可接受。
  - 建议: 如需就地翻滚，可仿照 pie_donut_chart 用 kind="path"、d 绕局部原点 (0,0) 定义矩形（旋转即绕形心）。
  - 置信度: 中（机制已核实；视觉接受度属主观判断）

### 通过项

- 随机性工程正确：固定种子 `random.Random(7)`，预设输出完全确定（利于预览缓存、回放一致与测试）；每次 build 新建 Random 实例，不污染全局 random 状态（线程安全维度通过）。
- 随机参数边界安全（按调用序复现推导）：start ∈ [0,19]，end = start+34+rand(0,14) ≤ 66 < 72（不触发 duration 扩展）；x ≤ 1860、x+16 ≤ 1876 < 1920；fall ≤ 880 < 1080；rotation 初值 ∈ [0,89]、spin ∈ {360,720}。
- opacity 四关键帧 start / start+4 / end-6 / end 严格有序唯一：end ≥ start+34 保证 end-6 ≥ start+28 > start+4，无乱序风险。
- 轨道属性（y/rotation/opacity）均在 ANIMATABLE_PROPERTIES 内；builders 对 rotation 取 float() 转换，整型随机值安全。
- z=index（0–23）互不冲突；元素数 24，规模合理无性能问题。
- PRESET 为字面量 dict，preset_id 与目录名一致；导入均被使用。
- GPU 生命周期 / 信号防递归 / GLSL·HDR / 状态提交不适用（纯数据构建）。
