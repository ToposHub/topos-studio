# X09 true_push —— 双源平移推出（试点）

> 状态：**component_ready**（暂存 CPU 核 + GLSL 草稿；未接宿主）。
> 台账：L00 §2 X09 行——slide 是 wipe mask 揭示双源不动（transition_mask.py:185-190
> 抽查证实）；本核为首个 sample（双源重采样）试点。

## 算法

```
（direction=left；其余方向轴/符号交换；u 为像素中心归一坐标）
A 段（u+p ≤ 1）：out = A(u + p)          # A 向左推出
B 段（u+p > 1）：out = B(u + p - 1)      # B 自右进入
接缝 u_seam = 1 - p，随 p 连续左移
特征点轨迹：A 的 x_A → 视口 x_A - p；B 的 x_B → 视口 x_B + 1 - p（两源同向移动）
```

- **边界结论（规格修正，记录于台账口径）**：正规 push 双源采样坐标恒在
  [0,1] 内，不存在越界采样——计划中的 border 参数在本核语义下不必要
  （越界仅发生于 X10/X11 透视推动类）。接缝羽化 seam_softness（默认 0）
  是接缝两侧混合，非边界处理。时间线模型无媒体 handles 的问题
  （L00 §5.6：越界帧=冻结边帧）因此不影响本核。
- 采样 bilinear（像素中心制）；p 为像素间距整数倍时为精确平移，
  p=0/p=1 端点比特级还原 A/B。
- 缓动走转场实例级既有 `curve` 参数，不在核内二次缓动。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| direction | left | left/right/up/down（四方向同核，不另算家族——配方层四个预设） |
| seam_softness | 0.0 | 接缝两侧归一半宽羽化（0=硬接缝） |

## 交付物

`true_push_core.py`（CPU 核）/ `true_push.glsl`（双源 UV 通道草案）/
`hand_vectors.json`（6 组向量，含两源特征移动钉住向量）/ `component_manifest.json`。

## 验证现状

- CPU 核 ↔ 手算向量一致；端点比特级；方向变体/接缝移动/特征轨迹测试通过。
- **未验**：GPU spec/GLSL/MSL 三端（L03 重采样子门）；真实命令/预览/导出链。
