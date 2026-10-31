# X07 voronoi_reveal —— 晶胞距离推进揭示（L14，mask 类）

> 状态：**component_ready**（暂存 CPU 核；未接宿主）。
> 台账：L00 §2 X07 行——全仓无 voronoi（grep 0 命中）。放本批因与 X04/X14/X16
> 同 hash 家族；**mask 类签名例外**：`evaluate_mask(width, height, progress,
> params)`（与 X01 clock_wipe 同口径，(H,W) float32）。

## 算法

```
种子网格：n = ceil(sqrt(cell_count))（8→3×3=9、16→4×4=16、32→6×6=36 胞元，
  档位是目标数、实际 n²，manifest/此处双声明）；种子位置 = 格内 hash 扰动：
  s = ((jx + h(seed, jx, jy))/n, (jy + h(seed, jx, jy, +1001))/n)
出生时间：t_i = rank_i · t_span；rank_i = hash(+2002) 升序名次/m ∈ [0,1)；
  t_span = clamp(1 - 2·edge_width, 0, 1)
距离场：d = |像素-种子|（宽高比修正：dx·(W/H)），归一 d/D，D=画幅对角线
mask = clamp((progress - t_i) · speed - d/D, 0, 1)，speed = 1/(edge_width+1e-6)
  （每像素取**最近**种子 i）
```

- **晶胞语义（钉住）**：同一胞元共享 t_i 与最近距离场 → 胞内揭示进度严格随
  d 排序：**胞元中心先揭示、胞界（d 大）后揭示**；edge_width 增大 →
  1/speed 变小 → 揭示带（0<mask<1 的 d 跨度 = edge_width·D）变宽。
- **端点全揭示推导**：p=1 时最坏像素 (1-t_max)·speed - d_max/D，t_max =
  t_span·(m-1)/m < 1-2·edge_width → (1-t_max)·speed > 2·edge_width/(edge_width+ε)
  ≈ 2 → 减 d/D ≤ 1 后仍 ≥ 1 → mask≡1。p≤0/p≥1 另走 early return（0/1 常量）。
- **取舍（冲突记录）**：①出生时间直接用满 [0,1) 会让最晚胞元在 p=1 无法
  全揭示（违反端点）→ 压缩 t_span=1-2·edge_width；②曾考虑全种子前沿并集
  max——早生邻胞会提前揭示晚胞的边界区，破坏"胞元中心先于边界"钉住语义 →
  钉住**最近种子前沿**。次近距离 d2 不入公式（仅描述性字段，本核无羽化用途）。
- **单调**：每像素 mask 随 p 单调不回退（clamp((p-t)·s-d) 对 p 单调）。
- **防御**：seed/edge_width NaN/Inf 回退默认、越界钳制；cell_count 非档位值
  ValueError。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| cell_count | "16" | "8"/"16"/"32" | 目标胞元数（实际网格 n²=ceil(sqrt)²） |
| seed | 7 | 0..100（int） | 扰动/出生时间散列种子 |
| edge_width | 0.1 | 0..0.5 | 揭示带归一宽度（相对对角线 D） |

## 交付物

`voronoi_reveal_core.py`（CPU 核，evaluate_mask）/ `README.md` /
`hand_vectors.json`（8x8 全帧向量 ×2 + 种子表 + 冲突 2 条）/
`component_manifest.json`。

## 验证现状

- CPU 核 ↔ 独立标量手算全帧对拍（4 参数组合 max diff ≤ 3e-8）；端点全 0/全 1
  比特级；逐像素单调；胞元中心先于边界、edge_width 带宽单调测试通过。
- **未验**：GPU（mask 通道 kind 扩展属 L03 集成）、MSL、真实预览/导出链。
