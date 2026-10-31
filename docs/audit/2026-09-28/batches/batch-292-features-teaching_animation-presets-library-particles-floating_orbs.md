# batch-292 — src/features/teaching_animation/presets/library/particles/floating_orbs//

审查范围：`src/features/teaching_animation/presets/library/particles/floating_orbs/build.py`（1 个文件，44 行）。
编号区间：AUD-29200–AUD-29299。
参照语义：`presets/builders.py`（track 有序唯一校验、scene_document duration 扩展）。

## src/features/teaching_animation/presets/library/particles/floating_orbs/build.py (行数: 44)

### 发现

- [AUD-29200] [Major] 循环尾段关键帧被压缩，部分光球在循环点附近速度突跳约 8 倍
  - 位置: src/features/teaching_animation/presets/library/particles/floating_orbs/build.py:37-40
  - 证据:
    ```python
    track("y", key(0, y), key(24 + phase, y - drift, "ease_in_out"),
          key(48 + phase, y + drift, "ease_in_out"),
          key(72 + phase, y - drift, "ease_in_out"),
          key(96, y, "ease_in_out")),
    ```
    phase = rng.randrange(0, 24)，末段区间长度 = 96-(72+phase) = 24-phase 帧，却要完成与前段相同的 2×drift 行程。
  - 影响: 用固定种子 20260821 逐次复现随机序列实测：orb 8（phase=21，drift=36.6）末段 73.2px 压进 3 帧（约 24px/帧），orb 11（phase=20，drift=36.2）72.3px 压进 4 帧（约 18px/帧）；而其余各段同为 2×drift 行程走 24 帧（约 3px/帧）。即循环尾部这两个光球出现 ~8 倍速度的急冲，随后回绕到帧 0，与"缓慢漂浮、循环播放"的预设描述相悖。帧号仍满足 builders.track 的有序唯一校验（builders.py:56-58），属时序均匀性缺陷而非校验失败。
  - 建议: 保证末段时长与其余段一致——例如将最后两个关键帧改为 key(72+phase, y-drift) 后先回到 key(72+phase+ (24-phase)//2, …) 的中继再收口 key(96, y)，或将相位循环整体设计为周期恰 96（首关键帧按 phase 偏移取 y±drift），使任意段间距不小于其余段的一半；同时补一条轨道级断言（帧距最小值）防回归。
  - 置信度: 高（RNG 序列与帧距均已按代码逐次复现实测）

- [AUD-29201] [Suggestion] `build()` 缺少返回类型注解
  - 位置: src/features/teaching_animation/presets/library/particles/floating_orbs/build.py:22
  - 证据:
    ```python
    def build():
        rng = random.Random(20260821)
    ```
  - 影响: 返回 dict 无注解，与强类型规范不符。
  - 建议: `def build() -> dict[str, Any]:`。
  - 置信度: 高

### 通过项

- 随机性工程正确：固定种子 `random.Random(20260821)` 输出确定；每次 build 新建实例，不污染全局随机状态。
- 帧号有序唯一成立：phase ∈ [0,24) 保证 0 < 24+phase < 48+phase < 72+phase < 96（.builders 校验通过，无异常路径）。
- opacity 循环无缝：帧 0 与帧 96 均为 0.55，中段 36 帧处峰值 1.0，回绕无跳变。
- y 行程边界安全：y ∈ [100,920]、drift ∈ [26,68]，y-drift ≥ 32 ≥ 0、y+drift ≤ 988 < 1080，光球不会飘出画布。
- kind="circle" 的宽高相等（size,size），圆半径=size/2 语义成立；radial_gradient 2 stops 与 2 opacities 匹配，柔光球边缘渐隐正确。
- 轨道属性（y/opacity）均可动画；`key(96, ...)` 帧号等于 duration=96 由 scene_document 自动扩为 97（builders.py:186-199 文档化的循环收口模式），首末帧值相同，回绕本身无缝。
- PRESET 为字面量 dict，preset_id 与目录名一致；导入均被使用；元素数 18，规模合理。
- 线程安全 / GPU 生命周期 / 信号防递归 / GLSL·HDR 不适用（纯数据构建）。
