# F10 starburst —— 多叶星芒

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F10 行——Anamorphic_Streak 仅水平 ±X 单方向；差量=多叶/多方向
> （blades 有界常量档）+ angle + ray_length。

## 算法

```
N    = blades 档（2/4/6，select 常量分支）
φ_k  = angle + k·180/N          # N 条过中心的角度线（方形像素域）
span = ray_length · 0.25 · W    # 单侧行程（Anamorphic_Streak streak_reach 同口径）
每条线双向各 7 tap（常量展开）：
  t_i = (i+0.5)/7,  w_i = (1−t_i)²            # 距离平方衰减
  e   = max(luma(s) − threshold, 0)            # 线性超出门控（Anamorphic 惯例）
  acc += (s₊·e₊ + s₋·e₋)·w_i,  wsum += 2·w_i
star = acc / max(wsum, 0.001)   # 全部 2N 方向总权重归一——能量有界
out  = color + star · intensity # 加性叠加（HDR 单调无折返）
```

- 单 pass 天然成立：全部 tap 直接采样输入纹理，无中间场。
- `intensity=0` 或 `ray_length=0` 首行早退精确恒等；阈下均匀场（luma < thr）
  精确恒等（e=0）；纯静态确定性。
- 循环上限字面常量（7）；叶数经 select 早退 if 链调度到编译期常量基方向集
  （0/30/45/60/90/120/135/150 度），运行期仅一次 `radians(angle)` 旋转。
- **参数命名**：行程参数命名 `ray_length`——`length` 是 GLSL 内建函数且在
  shader_compiler AUD-0832 保留字黑名单（F16/F19 的 mix→amount 同类改名先例）。

## 与相邻算法的区分（验收点）

- 非 Anamorphic_Streak（仅水平 ±X）：多方向——45° 对角像素在 4 叶方向集内
  有能量、2 叶方向集内精确为零（手算向量 6 + 变体 1 钉住）；
- 非 Soft_Glow_Bloom/Radial_Blur（无方向性线状结构、无门控归一）；
- 能量有限：均匀 HDR 12.0 输入 out = 12 + 137.4·intensity（线性有界，无平方失控）。

## 交付物

| 文件 | 内容 |
|---|---|
| `starburst.tglsl` | 单 pass 源码草稿（const 基方向 + 7-tap 常量循环） |
| `component_manifest.json` | L01 契约 |
| `hand_vectors.json` | 6 主向量 + 5 变体（独立脚本 + 均匀场手算） |
| `references/starburst_ref.py` | CPU oracle（float64 逐式同构） |

## 验证现状

- CPU oracle ↔ 独立脚本向量一致；真实 parser 解析通过；GLSL 红线静态检查通过。
- **未验**：真实 GPU 编译/像素（L02 纹理邻域子门，ROI padding = 单侧行程
  ≤25% 画幅宽）；6 叶档 84 tap 成本（preview_cost 声明 high，L21 基准）；
  非方像素比 aspect 校正（角度现定义在方形像素域，声明为已知限制）；
  Metal/CPU 导出。LUT 不可表达（邻域采样+位置依赖）——@export_support opengl。
