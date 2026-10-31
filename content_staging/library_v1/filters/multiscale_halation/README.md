# F06 multiscale_halation —— 多尺度高光红晕

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F06 行——Halation_Extended 为单尺度 13-tap ADR 核+HSV 重着色；
> Film_Halation 是空壳（process 直通）。差量=两尺度扩散+红色散射能量权重+阈值/能量控制。

## 算法（canonical，CPU oracle 定义）

```
m    = smoothstep(thr, thr + 0.35, luma)            # 软膝高光提取（Rec.709 luma）
E    = color * m                                    # 高光能量场（点态）
R    = max(2px, W * (0.004 + 0.026 * radius))       # 半径映射（Halation_Extended 口径）
big  = ring12(E, 2R)                                # 大尺度：12-tap 黄金角环，权重 0.6
small= ring12(E, R)                                 # 小尺度：同核，权重 0.4
halo = 0.6 * big + 0.4 * small
scatter = (halo.r*(1+w), halo.g, halo.b*(1-0.5w))   # 红色散射重着色，w = red_scatter
out  = color + scatter * strength                   # 加性叠加（HDR 单调无折返）
```

`ring12`：12 tap，方向 `i·137.50776°`（黄金角），等面积距离 `R·sqrt((i+0.5)/12)`，
等权平均；双线性采样 + clamp-to-edge（半纹素钳制）。

- **多 pass 红线声明**：E 场提取 → 双尺度扩散是天然多 pass 结构
  （pass1 E 提取；pass2/pass3 双尺度扩散；pass4 重着色叠加）。**不强塞一份 .tglsl**：
  真多尺度由原生 renderer 的多 pass 支持（EffectSpec）承接。
- **暂存 .tglsl 是单尺度降级模式**：单 pass、仅小尺度 R 的 12-tap 黄金角环 +
  逐 tap 软膝门控（多 pass 版为 E 场先提取后扩散，两者仅在软膝过渡带内有差异）。
  降级模式另有逐式同构 oracle 入口 `evaluate_degraded_single_scale`。
- `strength=0` 精确恒等（首行早退）；阈下均匀场（luma < thr）精确恒等（m=0）；
  纯静态确定性。
- `red_scatter=0`：三通道等增益（能量对称）；`w>0`：R 增益 (1+w)、B 增益 (1−0.5w)、G 不变。

## 与相邻算法的区分（验收点）

- 非 Halation_Extended（单尺度 ADR 核+HSV 重着色）：本项双尺度（2R@0.6 + R@0.4），
  点光源下距源 ~3px（超出小尺度 tap 可达 1.96px）仍有大尺度分量——手算向量 5 +
  降级模式对照向量钉住；
- 非 Soft_Glow_Bloom（无红色散射重着色/无能量权重参数）；
- 能量有界：均匀 HDR 12.0 输入输出 = 12 + 0.5·(18,12,9)（有限、线性、无折返）。

## 交付物

| 文件 | 内容 |
|---|---|
| `multiscale_halation.tglsl` | 单尺度降级草稿（12-tap 黄金角环；const 方向/距离表零运行期三角函数） |
| `component_manifest.json` | L01 契约 |
| `hand_vectors.json` | 8 主向量 + 3 变体（独立脚本 + 均匀场手算） |
| `references/multiscale_halation_ref.py` | CPU oracle（真多尺度 + 降级单尺度双入口） |

## 验证现状

- CPU oracle ↔ 独立脚本向量一致；真实 parser 解析通过；GLSL 红线静态检查通过
  （循环上限常量 12；u_* 白名单）。
- **未验**：真实 GPU 编译/像素（L02 纹理邻域子门，ROI padding ≈ 2R+1px）；
  多 pass 真多尺度实现（原生 renderer 子门）；Metal/CPU 导出。
  LUT 不可表达（邻域采样）——能力矩阵如实 untested，@export_support opengl。
