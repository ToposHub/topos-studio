# Three-Way Tint（三路色调）

> 阴影 / 中间调 / 高光三段独立着色 · 色彩类 · v1.0 · Topos Team
> 文件：`plugins/Three_Way_Tint.tglsl` · 规格：`docs/plugins/2026-09-28/specs/Three_Way_Tint.md`

## 一句话

按亮度把画面分成三个平滑过渡的分区，各自乘上一个以 0.5 为中性锚点的颜色增益——
冷阴影 + 暖高光的经典分离一步到位，且完全不压缩 HDR 动态范围。

## 参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| Shadow Color | color | 冷蓝 (0.38, 0.46, 0.60) | 阴影区着色，0.5 灰 = 不动 |
| Shadow Strength | slider | 0.30 | 阴影着色强度 |
| Midtone Color | color | 近中性微暖 (0.52, 0.50, 0.47) | 中间调着色 |
| Midtone Strength | slider | 0.25 | 中间调着色强度 |
| Highlight Color | color | 暖橙 (0.60, 0.53, 0.42) | 高光区着色 |
| Highlight Strength | slider | 0.30 | 高光着色强度 |

默认即一条克制的 teal-orange 曲线：阴影偏冷、高光偏暖、中间调近乎不动。

## 工作方式

1. Rec.709 luma → 色调坐标 `t = l / (l + 0.18)`（中灰 0.18 映射到 0.5，
   HDR 高值单调不提前饱和）；
2. 两段 smoothstep 得到阴影/高光权重，中间调取补集——任意亮度下三权重和恒为 1，
   过渡带（线性 luma 0.06–0.22 与 0.22–1.02）全程平滑无硬边；
3. 每区增益 `mix(1.0, tint * 2.0, w * strength)` 连乘后乘回颜色。

## 何时选它

- 要"冷暗部 + 暖亮部"的氛围分离，但不想动黑位/白位（乘性增益无 additive lift）；
- 只想污染中间调做场景色调，保留两端干净；
- HDR 素材（内部 float16/32）需要分区着色且不允许被压回 [0,1]。

## 边界与注意

- tint 全 0.5 或三强度全 0 时严格恒等（首行早退出），可安全挂链；
- 负值（out-of-gamut）像素按纯阴影分区处理，符号不翻转；
- 逐像素纯数学、零采样零循环、无 pow/exp/log —— `@preview_cost low`，
  4K 时间线可多层叠加；
- 输入/输出均为 scene-linear；着色发生在调色链当前位置（直接变换上游结果）。

## 相关

- `warm_tint.tglsl`：单区整体暖调（本插件把同一思路推广为三区）；
- `Color_Space_Transform.tglsl`：先做色彩空间/伽马管理，再进本插件。
