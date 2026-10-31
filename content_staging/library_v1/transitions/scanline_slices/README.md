# X16 scanline_slices —— 分行带 UV 错位（L14）

> 状态：**component_ready**（暂存 CPU 核；未接宿主）。
> 台账：L00 §2 X16 行——分段 UV 与源选择，非 RGB split 换名
  （transition_effects.py:462-488 对照：那是**通道分离**平移）。

## 算法

```
行带 k = floor(行像素号 / band_px)；每带独立方向：
  dir_k = +1 若 hash(seed, k) ≥ 0.5 否则 -1（uint32 murmur3 家族）
包络 w(p) = 1 - |2p-1|（Linear：三角窗，中段最大两端 0）
            curve=Ease 时 w = 三角窗²（端点与峰顶更缓——二次缓动）
带位移 d_k(p) = dir_k · amount · w(p)   （带内常量 → 带间硬边，不羽化声明）
A 层 = A(u + (1-p)·d_k)、B 层 = B(u + p·d_k)（水平 UV 位移，v 原位）
out = (1-p)·A 层 + p·B 层
```

- **与 RGB split 的钉住差量**：整行**同一 UV 位移作用于全部通道**（单色
  图案 → 输出仍单色、行带整体平移）；RGB split 是 R/G/B 各自不同位移 →
  单色输入出现彩色边。测试以单色图案钉住。
- **与 X14 同构**：1D 行带版（位移方向仅水平、按带离散），端点窗结构相同
  （w 两端 0 → 位移归零 + early return 比特级）。
- **端点/单调**：p=0/1 位移 0、A/B 比特级；常色源逐像素值随 p 单调不回退
  （混合权重线性）。
- **采样域**：位移可出界（amount ≤0.25 归一）→ 双线性 clamp 声明。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| band_px | 8 | 2..64（int） | 行带高度（像素） |
| amount | 0.1 | 0..0.25 | 峰值位移（归一 u） |
| seed | 0 | 0..100（int） | 行带方向散列种子 |
| curve | Linear | Linear/Ease | 包络形状（三角 / 三角²） |

## 交付物

`scanline_slices_core.py` / `README.md` / `hand_vectors.json`（2 组含带向表）/
`component_manifest.json`。

## 验证现状

- CPU 核 ↔ 独立标量手算全帧对拍（Linear/Ease diff ≤ 6e-17）；端点比特级；
  单色保持（非 RGB split）、行带整体平移、带间硬边、Ease≠Linear 测试通过。
- **未验**：GPU/GLSL/MSL、预览/导出链。
