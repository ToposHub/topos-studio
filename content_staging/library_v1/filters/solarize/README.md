# F17 solarize —— 阈值折返太阳化

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F17 行——全库无 solarize（Invert 是全反相 (1−c)，不同算子）。

## 算法

```
thr = max(threshold, 1e-4);  h = max(softness·thr, 1e-4)
m    = smoothstep(thr − h, thr + h, v)
fold = mix(v, 2·thr − v, m)              # 折返与原值的软混合
Luma 档: v = dot(color, (0.2126,0.7152,0.0722));  out = color + (fold − v)
RGB  档: 逐通道 fold
out = mix(color, out_mode, amount)       # amount<0.001 早退恒等
```

- **HDR 策略（显式声明）**：**线性折返无 clamp**——v>thr 折返 2·thr−v，
  HDR 输入可折出大幅负值（如 12.0@thr=1 → −10.0）；负值输入 v<thr 原样
  保留（符号不翻、无裁剪）。这是数学折返的如实行为，交给下游处理。
- **处理域**：scene-linear（参数 threshold 为线性值 0..4）；v=thr 处
  fold(v)=v 恒连续，无跳变；softness=0 经 h≥1e-4 下限守护为近似硬折返
  （GLSL smoothstep 等界未定义）。
- **通道两档**：Luma=709 亮度折返+色度向量平移保留（彩色结构不变）；
  RGB=逐通道独立折返（通道间相位差异产生经典分色太阳化）。
- **对 Invert 的差量**：Invert 全反相 (1−c) 单调映射；本件是**非单调
  折返**（过阈值回落），thr→0 且 softness=0 的 RGB 档在数学上退化为
  近似反相（2·thr−v），但默认语义与参数空间完全不同。
- **恒等/旁路**：`amount=0` → 首行早退**精确**恒等；默认参数
  （thr=1/soft=0/Luma/amount=1）非恒等——风格效果 bypass 语义同 F16。
- **域**：scene-linear in/out；纯逐像素（零采样/零循环）；alpha 由宿主直通。

## 交付物

| 文件 | 内容 |
|---|---|
| `solarize.tglsl` | 源码草稿（生产 ABI 逐字兼容：元数据块/裸参数/零循环/早退恒等） |
| `component_manifest.json` | L01 契约（参数/域/能力矩阵默认 untested） |
| `hand_vectors.json` | 12 条手算向量（独立一次性脚本，容差 1e-6） |
| `references/solarize_ref.py` | CPU oracle（float64，与 GLSL 逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量：12/12 一致（tests/content_library_v1/test_f17_solarize.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while、无循环、无未声明 u_*。
- **未验**：真实 GPU 编译/像素（L02 点处理子门）；Metal/CPU 导出；
  LUT 烘出（Luma 档依赖全通道 luma——逐通道 LUT 不可精确表达，RGB 档
  可表达；导出路径区分待 L20）。
