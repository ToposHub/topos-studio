# F04 zoned_tint —— 可移动分界的三区着色

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F04 行——Three_Way_Tint 分界是编译期常量 [0.25,0.55]/[0.55,0.85]
> 固定两段；本项差量为 shadow/highlight **pivot 可移** + **交叉 width 可调** + 分区强度。

## 算法

```
luma = max(dot(color, (0.2126,0.7152,0.0722)), 0)     # 负 luma 按纯阴影分区
t = luma / (luma + 0.18)                              # Reinhard 色调坐标（0.18→0.5）
s_mid  = smoothstep(sp − sw/2, sp + sw/2, t)          # sw = max(shadow_width, 1e-4)
s_high = smoothstep(hp − hw/2, hp + hw/2, t)          # hp/highlight_pivot 同式
w_shadow = 1 − s_mid;  w_high = s_high
w_midtone = clamp(1 − w_shadow − w_high, 0, 1)        # 补集（浮点保险 clamp）
gain = Π_zone mix(1.0, tint_zone·2.0, w_zone·strength_zone)
out = color · gain
```

- **对 Three_Way_Tint 的差量（验收）**：分界/交叉宽度均为滑杆（0.10..0.4 /
  0.5..0.9 / 0..0.5），非固定三段复制；乘性 zone_gain 语义保留
  （tint 0.5=增益 1 的"无界 overlay"手感）。
- **分区连续性（验收）**：默认 pivot/width 下两段过渡带 [0.10,0.40] 与
  [0.60,0.90] 不重叠 → 权重和恒 1（partition of unity，全程 smoothstep
  无硬边）。极端设置（shadow_pivot=0.4+width=0.5 与 highlight_pivot=0.5
  重叠）时 w_midtone clamp 守护：有界、连续，但权重和可 >1——**如实声明**
  的非 unity 区间，不静默。
- **HDR 策略（显式声明）**：乘性增益无 clamp——超白/负值按原比例缩放、
  符号不翻转（与 Three_Way_Tint 同款 HDR 安全设计）；负 luma 只影响
  分区坐标（t=0 纯阴影），不触碰颜色本身。
- **恒等/旁路**：tint 全 0.5 灰 → `0.5*2.0=1.0` 乘法路径**精确**恒等
  （无舍入；默认参数即恒等）；三强度全 0 → 首行早退精确恒等。
  `width=0` 经 1e-4 下限守护为近似硬分界（GLSL smoothstep 等界未定义）。
- **域**：scene-linear in/out；纯逐像素（零采样/零循环）；alpha 由宿主直通。

## 交付物

| 文件 | 内容 |
|---|---|
| `zoned_tint.tglsl` | 源码草稿（生产 ABI 逐字兼容：元数据块/裸参数/零循环/早退恒等） |
| `component_manifest.json` | L01 契约（参数/域/能力矩阵默认 untested） |
| `hand_vectors.json` | 14 条手算向量（独立一次性脚本，容差 1e-6） |
| `references/zoned_tint_ref.py` | CPU oracle（float64，与 GLSL 逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量：14/14 一致（tests/content_library_v1/test_f04_zoned_tint.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while、无循环、无未声明 u_*。
- **未验**：真实 GPU 编译/像素（L02 点处理子门）；Metal/CPU 导出；
  LUT 烘出（逐像素纯 RGB 依赖，可表达性成立，待 L20 实测）。
