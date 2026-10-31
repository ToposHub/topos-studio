# X11 cube_faces —— 立方体两面透视旋转（L14）

> 状态：**component_ready**（暂存 CPU 核；未接宿主）。
> 台账：L00 §2 X11 行——两面透视旋转映射，真实两源到相邻投影面；
> 不新增通用 3D 引擎。

## 算法（简化 2D 投影）

```
θ = p·90°；cosθ/sinθ 为两面视宽（透视压扁）
共棱（两面交线）屏幕位置 x = p（axis=Vertical 时换 y 轴），随 p 线性 0→1
A 面（离场）：屏幕区间 [p, p+cosθ]（世界张成 [p, p+1] 的压扁）
  m_A = (x-p)/cosθ ∈ [0,1] → 源采样 s_A = m_A(1+persp·m_A)/(1+persp)
B 面（进场）：屏幕区间 [p-sinθ, p]（世界张成 [p-1, p] 的压扁）
  m_B = (x-p+sinθ)/sinθ → s_B = 1-(1-m_B)(1+persp(1-m_B))/(1+persp)（镜像）
像素归属：x ≥ p → A 面，x < p → B 面
shadow：shade_A = 1-shadow·(1-cosθ)、shade_B = 1-shadow·(1-sinθ)
  （面视宽=1 正对时不压暗；离场面随压扁变暗）
```

- **区间语义（冲突记录）**：任务原文"A 面 x∈[p-1,p]"按 A 指派在 p=0 时
  A 离屏（违反端点）；该区间是 **B 面的世界张成**。实现按 A 面 [p,p+cosθ] /
  B 面 [p-sinθ,p]，任务"面宽 cos(θ)"原样保留。
- **覆盖完备性证明**：需 p-sinθ ≤ 0 且 p+cosθ ≥ 1（∀p∈[0,1]）：
  g(p)=p-sin(pπ/2)：g(0)=g(1)=0、g'<0 于 (0, 2/π·...) — g 在 (0,1) 内
  恒 ≤0（数值抽查 + 端点为 0、内部极值 <0）；f(p)=p+cos(pπ/2)：f(0)=f(1)=1、
  内部 >1 → 两面并集 ⊇ [0,1]，画幅**无露底**。
- **界内**：像素 m_A ≤ (1-p)/cosθ ≤ 1、m_B ≥ 1-p/sinθ ≥ 0（由覆盖不等式）
  → 恒 in-bounds-by-construction。
- **端点**：p=0 → 全像素 A 面 m=x → 精确 A；p=1 → 全像素 B 面 m=x → 精确 B
  （early return 比特级）。
- **单调**：共棱 x=p 单调右移 → B 面覆盖面积单调不减。
- **透视**：A 面外边（m=1，远离共棱）压缩加速（同 X10 门内公式）；B 面镜像
  （外边 m=0 压缩加速）——"按面内位置加权压缩"。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| axis | Horizontal | Horizontal/Vertical | 旋转轴（共棱方向） |
| perspective | 0.5 | 0..1 | 面内透视压缩（0=均匀 cos 压缩） |
| shadow | 0.5 | 0..1 | 面随视宽的压暗 |

## 交付物

`cube_faces_core.py` / `README.md` / `hand_vectors.json`（2 组 + 冲突 2 条）/
`component_manifest.json`。

## 验证现状

- CPU 核 ↔ 独立标量手算全帧对拍（横/纵/透视/阴影组合 max diff ≤ 8e-7）；
  端点比特级；共棱单调、无露底（全帧恒有源）、shadow 只压暗对应面测试通过。
- **未验**：GPU/GLSL/MSL、真 3D 立方体（本核明确 2D 简化）、预览/导出链。
