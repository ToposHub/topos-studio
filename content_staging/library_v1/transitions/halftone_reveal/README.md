# X08 halftone_reveal —— 网点扩张揭示

> 状态：**component_ready**（暂存独立 CPU 核；未接宿主）。
> 台账：L00 §2 X08 行——`plugins/Halftone_Print.tglsl` 是单输入滤镜非转场
> （仅几何参考）；差量表 §5"网点频率独立，分辨率/像素比例一致"。

## 算法

```
(s,t) = ((i+0.5)·cosA − (j+0.5)·sinA, (i+0.5)·sinA + (j+0.5)·cosA)  # 像素单位
胞元 = floor((s,t)/spacing)；中心距 Δ=(s,t) − (胞元+0.5)·spacing
Dot:    d = √(Δs² + Δt²)/spacing            # 径向（圆点）
Square: d = max(|Δs|, |Δt|)/spacing          # 切比雪夫（方点）
r = p·√2                                    # 网点半径（归一 spacing）
mask = clamp((r + soft − d)/(2·max(soft,1e-6)), 0, 1)   # d<r−soft→1, d>r+soft→0
```

- **r_max=√2 的裁决**：胞元角距上界 √2/2 ≈ 0.7071，最大 softness 0.5 时
  全覆盖需 r ≥ 0.7071+0.5 ≈ 1.2071 → 取 √2 ≈ 1.4142（任务文本 1.2/√2 两说，
  取更保守的 √2，无早退也保证 p=1 全覆盖；端点早退再给比特级）。
- p=0 → r=0 → mask=0（soft>0 时 (soft−d)/2soft 可为正但 d≥0…注意 d 可小于
  soft：p=0 时 r=0，mask=clamp((soft−d)/2soft) 在胞元中心 d≈0 处为 0.5！
  ——故**端点早退是必要语义**（p≤0 全 0），与 X01 族同惯例。
- p 单调：d 逐像素固定，r 随 p 增长 → mask 不减。
- spacing 像素单位（网点频率独立于帧分辨率比例；台账"分辨率/像素比例一致"
  指 spacing 语义在 px 下跨端一致，GLSL 侧同单位移植）。
- 坏参数防御：spacing 取整 clamp [4,64]、angle clamp ±90、shape 非法回退
  "Dot"、softness clamp [0,0.5] + 1e-6 守护；NaN/Inf 回退默认。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| spacing | 16 | 4–64 px | 网点间距（像素） |
| angle | 45 | −90–90 度 | 网格旋转角（经典加网 45°） |
| shape | "Dot" | Dot/Square | 圆点/方点 |
| softness | 0.1 | 0–0.5 | 网点边缘羽化（归一 spacing） |

## 交付物

| 文件 | 内容 |
|---|---|
| `halftone_reveal_core.py` | 独立 CPU mask 核（float64 → float32） |
| `hand_vectors.json` | 7 组手算向量（Dot/Square 闭式距离、羽化 0.411612、spacing/angle 区分、端点） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |

## 验证现状

- CPU 核 ↔ 手算向量一致；冲突记录：spacing=4 向量初版手推误记中间行
  |dy|=0.375（实为 0.125，行 1/2 贴块中心 → 全揭示），独立脚本重推订正。
- **未验**：GPU spec/GLSL/MSL 三端（L03 mask 子门）；真实命令/预览/导出链。
