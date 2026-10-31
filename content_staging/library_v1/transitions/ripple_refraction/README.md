# X15 ripple_refraction —— 径向波场折射（L14）

> 状态：**component_ready**（暂存 CPU 核；未接宿主）。
> 台账：L00 §2 X15 行——UV 波场非 radial blur（plugins/Radial_Blur_Pro 对照
  排除：那是径向模糊重采样权重大全，本核是位移场折射）；进出位移归零。

## 算法

```
径距（宽高比修正）：r = |(u-cx)·(W/H), v-cy|；r_max = 中心至四角最大径距
波场：dr(r) = amplitude · sin(frequency·(r - p·r_max)) · exp(-damping·r)
  （波前相位随 p 外扩：p∈[0,1] 恰好穿到最远角；damping 随 r 衰减）
位移：(du,dv) = dr · r̂（径向单位向量，r=0 无定义 → 位移置 0——中心像素
  无位移，任务钉住）
层采样（w = 1-|2p-1| 三角窗）：
  A 层 = A(u + (1-p)·w·du, v + (1-p)·w·dv)
  B 层 = B(u + p·w·du,     v + p·w·dv)
out = (1-p)·A 层 + p·B 层
```

- **冲突记录（三角窗）**：任务原文"A 幅度 (1-p)"在 p=0 幅度=1，与"进出
  端点位移归零（p=0/1 幅度项为 0）"自相矛盾 → 同 X14/X16 补 w 窗
  （A=(1-p)·w、B=p·w），端点位移精确 0、p=0.5 幅度峰值。
- **折射非模糊**：输出是单点重采样（位移场），无邻域加权 → 与 radial blur
  的机器可辨差量（radial blur 输出含多半径能量混合，本核逐像素单采样）。
- **采样域**：位移可出界（幅度 0.25 归一 + 中心偏置）→ 双线性 clamp 声明。
- **端点**：位移 0 + early return → A/B 比特级。
- **波纹可见性**：径向 sin 相位 → 沿半径方向输出值振荡（测试钉住非单调
  径向剖面，区别于单调缩放类）。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| center_x / center_y | 0.5 / 0.5 | 0..1 | 波心（像素中心制） |
| frequency | 8.0 | 1..32 | 径向角频率（每 r_max 的周期数） |
| amplitude | 0.1 | 0..0.25 | 峰值位移（归一） |
| damping | 0.3 | 0..1 | 径向衰减 exp(-damping·r) |

## 交付物

`ripple_refraction_core.py` / `README.md` / `hand_vectors.json`（1 组全行 +
冲突 1 条）/ `component_manifest.json`。

## 验证现状

- CPU 核 ↔ 独立标量手算全帧对拍（3 参数组合 diff ≤ 1.2e-16）；端点比特级；
  径向振荡剖面、中心位移 0、幅度 0 恒等、换源单调测试通过。
- **未验**：GPU/GLSL/MSL、真实折射率物理模型（本核为程序波场）、预览/导出链。
