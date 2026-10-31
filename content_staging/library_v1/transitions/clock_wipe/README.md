# X01 clock_wipe —— 时钟极角揭示（试点）

> 状态：**component_ready**（暂存 CPU 核 + GLSL 草稿；未接宿主）。
> 台账：L00 §2 X01 行——现有 33 转场零极角实现（iris=径向距离、wipe=线性投影、
> spin_blur=源侧拖影），本核是首个 mask 试点。

## 算法

```
x=(i+0.5)/W, y=(j+0.5)/H；dx=(x-cx)·(W/H)（aspect 校正），dy=cy-y（北=+）
θ = atan2(dx, dy)                       # 北 0°，顺时针正
rel = mod(dir·(θ - start_angle), 360°)  # 沿扫掠方向到起始边角距
sweep = p·360°；feather = softness·360°
mask = clamp((sweep - rel)/max(feather,1e-6), 0, 1)   # 前沿羽化（权重域）
p≤0 → 全 0；p≥1 → 全 1（端点精确，与既有 mask 核同惯例）
```

- 中心像素 atan2(0,0)=0（GLSL/np 同口径）随起始边率先揭示——中心连续。
- 角度接缝：rel∈[0,360°) 单值化后无跳变；未揭示楔形随 p→1 收缩到 0，连续。
- 可复用基座：`transition_mask._get_coord_grid` 的 aspect 校正中心网格思路
  （实现独立，L03 集成时对齐其缓存机制）。

## 参数（对齐既有转场参数风格）

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| center_x / center_y | 0.5 | 0–1 | 极角中心（归一） |
| start_angle | 0 | 0–360 度 | 起始边（0=12 点） |
| clockwise | true | bool | false=逆时针（复用既有 reverse 语义） |
| softness | 0.02 | 0–0.5 | 前沿羽化角宽（归一·2π，与 wipe softness 口径同族） |

方向变体（顺/逆时针）不另算算法家族；`curve`（缓动）走转场实例级既有参数。

## 交付物

| 文件 | 内容 |
|---|---|
| `clock_wipe_core.py` | CPU mask 核（numpy float64；L03 集成进 transition_mask.py 的草案基座） |
| `clock_wipe.glsl` | GPU procedural_mask 扩展草稿（uniform 复用现有块 + u_center 新增） |
| `hand_vectors.json` | 7 组手算向量（含 aspect 校正钉住向量与羽化向量） |
| `component_manifest.json` | L01 契约 |

## 验证现状

- CPU 核 ↔ 手算向量一致（含端点精确/方向镜像/起始边旋转/羽化/aspect）。
- **未验**：GPU spec/GLSL/MSL 三端（L03 接缝——Metal iris 旧半径先例警示三端同步）；
  真实 AddTransition 命令/保存重开/导出链（L03 后）。
