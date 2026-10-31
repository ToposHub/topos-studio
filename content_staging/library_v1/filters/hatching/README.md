# F14 hatching —— 亮度驱动交叉排线

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F14 行——Edge_Ink_Outline 是 Sobel 描边上墨，无排线；
> 差量=亮度驱动交叉排线（平坦灰阶也产生排线）。纸纹外部资源版依赖资源
> 子门（后续），本版为程序几何（零采样）。

## 算法

```
D    = 1 − clamp(luma, 0, 1)        # 目标墨密度（暗→密；clamp 仅几何参数域）
pq   = (x+0.5, y+0.5)               # 像素序坐标
φ    = radians(angle)；dir1 = (cos φ, sin φ)，dir2 = 正交
b    = (pq · dir) / spacing         # 旋转坐标里的周期条带相位
cell = |fract(b) − 0.5| ∈ [0, 0.5]
h    = −0.06 + 0.62·D               # 线半宽（AA=0.06：D=0 全露白 / D=1 满幅墨）
cov  = 1 − smoothstep(h−0.06, h+0.06, cell)
layers=2：cov_total = 1 − (1−cov1)(1−cov2)   # 第二层正交、半密度 D/2，概率并
hatched = mix(vec3(paper), ink_color, cov_total)
out  = color + (hatched − color) · amount
```

- 零纹理采样、零循环（程序几何，preview_cost low）；位置驱动 → 不可 LUT 烘出。
- `amount=0` 精确恒等（首行早退，含 HDR）；`amount<1` 按比例保留原 HDR 值，
  `amount=1` 输出为墨/纸有界域（显示域艺术效果，显式声明）。
- 线宽随密度连续加宽：暗→线宽大（D=1 满幅墨），亮→全露白（D=0）；
  端点饱和使平坦黑/白位置无关（手算向量 1/2 钉住）。
- 纯静态确定性；spacing 下限守护（max(spacing, 1.0)）。

## 与相邻算法的区分（验收点）

- **非 Edge_Ink_Outline**（Sobel 描边，平坦输入恒等）：本项平坦灰阶也产生
  可解释排线——平坦 0.18 灰在 spacing 3 胞心位满墨（向量 3 钉住）、
  平坦 0.5 灰产生空间周期性条带（测试统计方差 > 0 钉住）；
- 非 Pixel_Blocks（块量化无旋转条带/密度连续加宽）；
- 层数差量：正交第二层在层 1 露白位补充墨量（向量 4 vs 变体 1：同位
  0.45906582 vs 0.9 钉住）。

## 交付物

| 文件 | 内容 |
|---|---|
| `hatching.tglsl` | 单 pass 源码草稿（零循环零采样，运行期一次 cos/sin） |
| `component_manifest.json` | L01 契约 |
| `hand_vectors.json` | 5 主向量 + 6 变体（独立脚本 + 退化位手算） |
| `references/hatching_ref.py` | CPU oracle（float64 逐式同构） |

## 验证现状

- CPU oracle ↔ 独立脚本向量一致；真实 parser 解析通过；GLSL 红线静态检查通过
  （零循环；u_* 白名单）。
- **未验**：真实 GPU 编译/像素（L02 位置驱动子门，ROI padding = 0）；
  奇数尺寸/1px 图的条带走样；Metal/CPU 导出。
  LUT 不可表达（位置依赖）——@export_support opengl。
