# F15 watercolor —— 水彩近似

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F15 行——全库无水彩/扩散模型；blur+saturation 冒充被禁止。

## 算法（单 pass 近似）

```
1. Sobel 亮度梯度（Edge_Ink_Outline 同式 8 tap，/8 归一，edge 边界 pad）：
   mag = |g|/8；边缘带掩码 em = smoothstep(0.02, 0.20, mag)
2. 沿梯度反向 8 tap 有界扩散（位移 ∝ diffusion，≤8px）：
   dir = g/|g|（|g|<1e-6 → 0 向量）；tap j 位移 = (j+1)·diffusion·em px
   权重 w_j = 1−(j+0.5)/8，与中心（权重 1）平均 → bleed
3. 边缘颜料聚边（工作单"饱和度/亮度提升 ∝ edge_pigment"的字面实现）：
   pig = (lb + (bleed−lb)·(1+edge_pigment·em))·(1+0.5·edge_pigment·em)
   ——色度绕 luma 放大（≤2x）后整体乘性提亮（≤1.5x），增益有界
4. 程序纸纹（调暗+去饱和）：整数格点 murmur3 风格 uint32 乘法散列
   （fixtures/_lattice_hash 的 uint32 同式缩减，无 seed 项 = 固定 seed 0，
   宿主白名单外零资源）双线性 value noise，格距 = paper_scale px，n∈[0,1)：
   desat = mix(vec3(luma), pig, 1−0.25n)；papered = desat·(1−0.25n)
5. Delta 混合：out = color + (papered − base)·amount
```

- **单 pass 近似声明**：扩散为有界单 pass 8-tap 直接采样（位移 ≤8px）；
  真正多 pass 迭代扩散（逐轮乒乓传播、更大范围浸润）需原生 renderer 多
  pass 支持，不强塞一份 .tglsl（多 pass 红线，同 F06 惯例）。
- `amount=0` 精确恒等（首行早退，含 HDR）；平坦区扩散位移=0、聚边增益=1
  （em=0）——平坦输入仅纸纹起作用，且对 diffusion/edge_pigment 精确不变；
  `diffusion=0` 位移恒 0（扩散级精确恒等）。
- 聚边实现口径记录：工作单"饱和度/亮度提升"按字面实现（色度放大 +
  亮度乘性提升）；物理聚边变暗（颜料密度模型）留待参数化后续版本。
- 循环上限字面常量 8；Sobel 8 采样手工展开；采样 UV 钳半纹素；
  em 只作用于权重/位移/增益，不钳制基色（HDR 安全）；纯静态确定性。

## 与相邻算法的区分（验收点）

- 非 blur+saturation（被禁的冒充路径）：梯度方向性扩散（暗侧采样，方向
  翻转输出显著不同——手算向量 1 vs 3 钉住）+ 边缘带聚边（em 门控）+
  程序纸纹（三者可独立关闭，变体向量逐项钉住）；
- 非 bilateral/blur（无梯度反向位移）、非 Film_Grain（纸纹调暗+去饱和
  而非亮度加权加性颗粒）。

## 交付物

| 文件 | 内容 |
|---|---|
| `watercolor.tglsl` | 单 pass 近似源码草稿（uint32 格点散列内置） |
| `component_manifest.json` | L01 契约 |
| `hand_vectors.json` | 3 主向量 + 3 变体（独立脚本 + 半手算链） |
| `references/watercolor_ref.py` | CPU oracle（float64 逐式同构） |

## 验证现状

- CPU oracle ↔ 独立脚本向量一致；真实 parser 解析通过；GLSL 红线静态检查通过
  （循环上限常量 8；u_* 白名单；uint 字面量依赖宿主 #version 330）。
- **未验**：真实 GPU 编译/像素（L02 纹理邻域子门，ROI padding ≤9px；
  uint hash 的 GPU 侧位一致性与浮点舍入容差）；多 pass 迭代扩散
  （原生 renderer 子门）；Metal/CPU 导出。LUT 不可表达（邻域+位置依赖）
  ——@export_support opengl。
