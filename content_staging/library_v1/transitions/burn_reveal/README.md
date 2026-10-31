# X18 burn_reveal —— rank-field 灼烧揭示 + 焦边合成

> 状态：**component_ready**（暂存独立 CPU 核；未接宿主）。
> 台账：L00 §2 X18 行——rank-field+焦边/亮边合成无等价（grep 0 命中）；
> 差量表 §5"与 X06 区别由实际焦边/发光证明；HDR 能量/颜色空间明确"。
> L15 批（P2）。

## 算法

```
rank 场（与 X06 同式散列/双线性，格距常量 _SCALE=8 帧归一；文件自带）：
  Field = 双线性 value rank（平滑焦区场）
  Edge  = 最近格点 rank（前沿贴胞元边界、硬边烧穿）
mask  = clamp((p − rank)/max(cw,1e-6), 0, 1)          # 揭示（ramp 宽=char_width）
e     = clamp(1 − |p − rank|/max(cw,1e-6), 0, 1)      # 前沿带强度（前沿中心 1）
heat  = e · mix(橙(1, 0.3, 0.05), 白(1, 1, 1), e)      # 前沿白热、带缘橙红
out   = A·(1−mask) + B·mask + emission·heat            # scene-linear 加性发光
```

- **与 X06 的差量由焦边证明**：emission>0 时前沿带 |p−rank|<cw 出现
  base 之上的加性热色（emission=0 退化为纯 rank 揭示 mix——测试钉住两态差）。
- **HDR/颜色空间（明确声明）**：heat 为加性、不 clamp（scene-linear 无界，
  显示域压缩属宿主 tonemap）；uint8 输入按 push_frame 约定 round-trip。
- p=0 / p=1 早退**比特级还原 A/B**（前沿带在端点被早退清除——p=1 时带
  |1−rank|<cw 本会残留，早退是必要语义）。
- 灰度 (H,W) 源走 R 轴（heat=e）；rank 格距为常量 8（未提参数，预设层
  用 seed/char_width 变化即可；后续可提升参数）。
- 坏参数防御：pattern 非法回退 "Field"、char_width clamp [0,0.5]+1e-6、
  emission clamp [0,1]、seed 取整 clamp [0,100]；NaN/Inf 回退默认不抛；
  双源形状不一致 → ValueError（与 X09 同口径）。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| pattern | "Field" | Edge/Field | 硬边胞元前沿 / 平滑焦区场 |
| char_width | 0.08 | 0–0.5 | 前沿带宽（rank 域；兼揭示 ramp 宽） |
| emission | 0.5 | 0–1 | 发光强度 |
| seed | 0 | 0–100 int | 固定种子（静态） |

## 交付物

| 文件 | 内容 |
|---|---|
| `burn_reveal_core.py` | 独立 CPU mask+composite 核（`evaluate(a, b, p, params)` 返回合成帧） |
| `hand_vectors.json` | 8 组手算向量（Field/emission=0/seed/Edge-Field 分叉/RGB 橙→白/端点） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |

## 验证现状

- CPU 核 ↔ 手算向量一致（独立脚本：Python 大整数模拟 uint32 散列，不复用
  核代码；无冲突）。
- **未验**：GPU spec/GLSL/MSL 三端（L03 mask+合成接缝：现有管线只有
  "几何 mask × progress" 单一合成形态，焦边属第二 pass）；HDR 真实
  4K/导出链未验。
