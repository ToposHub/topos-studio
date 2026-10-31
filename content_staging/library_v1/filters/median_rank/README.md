# F19 median_rank —— 小核中值/百分位滤波（试点）

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F19 行——全库无排序统计滤波（blur 五型/denoise/sharpen 均为加权平均或 unsharp，非秩选择）。

## 算法

- 邻域：3×3 或 5×5（select 常量档），索引 clamp 到 [0, n-1]（edge 边界）。
- 选择：对 n 个样本排序取第 k 小，`k = round(percentile·(n-1))`；
  GPU 侧用精确秩计数（`c_i = #{v_j<v_i} + #{j<i ∧ v_j=v_i}`）——并列值占连续秩，
  **值**口径与排序取第 k 个完全一致（手算向量钉住）。
- 模式：RGB=逐通道独立；Luma=对 Rec.709 luma 取 k 值后按比例保持色度
  `out = color·(med_l/max(l,1e-6))`，l≤1e-6 退化为 `vec3(med_l)`。
- Delta 混合 `out = color + (med − base)·Amount`；`Amount=0` 精确恒等（参数名 amount；`mix` 为 GLSL 内建保留名不可用）。
- 静态确定性：无 seed/frame 依赖。

## 与相邻算法的区分（验收点）

- 非 bilateral（无边缘保持权重，是排序统计）；
- 非 box/gaussian（无加权平均）；
- flat 区域恒等（常值邻域中值=常值）——参数有效性测试用梯度/噪声夹具验证。

## 交付物

| 文件 | 内容 |
|---|---|
| `median_rank.tglsl` | 源码草稿（常量双循环秩计数；u_input_tex 邻域采样；Delta 输出） |
| `component_manifest.json` | L01 契约 |
| `hand_vectors.json` | 9 条手算向量（5×5 灰梯度图独立枚举） |
| `references/median_rank_ref.py` | CPU oracle（窗口排序取第 k） |

## 验证现状

- CPU oracle ↔ 手算向量一致；真实 parser 解析通过；GLSL 红线静态检查通过。
- **未验**：真实 GPU 编译/像素（L02 纹理邻域子门，含 ROI padding 接缝）；
  5×5 档性能（L21）；Metal/CPU 导出。LUT 不可表达（邻域采样）——能力矩阵如实 untested/不声明。
