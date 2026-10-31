# X14 displacement_bridge —— 程序噪声双源位移桥接（L14）

> 状态：**component_ready**（暂存 CPU 核；**程序 map 模式**）。
> 台账：L00 §2 X14 行——RG/XY map 双源位移；程序 map 先行，**外部 map 过
> 辅助 sampler 运行时资源子门 + M20 归档闭包后才发**（本批不含外部 map，
> 计划 §E 口径）。

## 算法

```
位移场 d(p) = scale · w(p) · (nx, ny)，w(p) = 1 - |2p-1|（三角窗）
  Wave：nx/ny = 2·valueNoise(uv·freq; seed / seed+1001) - 1
        （F07 同式 4-tap 平滑 value noise，像素中心格坐标、uint32 murmur3）
  Cell：nx/ny = 2·latticeHash(floor(uv·freq); seed / seed+1001) - 1
        （逐格常量、无插值 → 块状错位；与 Wave 的机器可辨差量）
A 层 = A(u + (1-p)·d(p), v + …)、B 层 = B(u + p·d(p), v + …)
out = (1-p)·A 层 + p·B 层
```

- **冲突记录（三角窗）**：任务原文"A 位移 (1-p)·d"在 p=0 位移=d≠0，与
  "端点 p=0=A 位移 0=A 原位"自相矛盾。按 X16（任务自述与 X14 同构）补回
  时间窗：d(p) 含 w(p) 两端为 0 → 端点位移精确 0；中段位移最大（p=0.5 w=1）。
  A/B 的 (1-p)/p 份额原样保留。
- **与 dissolve 的差量**：UV 真位移（A 特征在视口内移动）非静态淡入淡出；
  scale=0 退化为普通 dissolve（manifest 声明）。
- **采样域**：位移可出界 → 双线性 clamp（clamp-half-texel，声明）。
- **端点**：p=0/1 → 位移 0 + early return → A/B 比特级。
- **单调（换源）**：B 混合权重 p 单调；常色源下逐像素值单调不回退。
- **hash 家族**：与 F07 seeded_grain / fixtures _lattice_hash 同式（位宽 32、
  同乘子链），seed 经 uint32 进第三乘子通道。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| scale | 0.1 | 0..0.25 | 峰值位移（归一 u/v 各轴，无宽高比修正——声明） |
| frequency | 4.0 | 1..16 | 噪声频率（Wave 格密度 / Cell 格数每轴） |
| seed | 0 | 0..100（int） | 散列种子 |
| mode | Wave | Wave/Cell | 平滑噪声 / 逐格常量 |

## 交付物

`displacement_bridge_core.py` / `README.md` / `hand_vectors.json`（2 组 +
冲突 1 条）/ `component_manifest.json`。

## 验证现状

- CPU 核 ↔ 独立标量手算全帧对拍（Wave/Cell/边界频率组合，diff ≤ 1.2e-16）；
  端点比特级；Wave≠Cell、seed 有效性、scale=0 dissolve 退化测试通过。
- **未验**：外部 map 模式（资源子门未开，本批声明不含）、GPU/GLSL/MSL、
  预览/导出链。
