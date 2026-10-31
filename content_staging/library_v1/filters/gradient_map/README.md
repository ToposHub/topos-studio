# F16 gradient_map —— 多停靠点亮度映射（试点）

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F16 行——Duotone_Toner 仅 2 停靠，本项差量为 4 停靠+可动位置+插值模式。

## 算法

```
l = dot(color, (0.2126, 0.7152, 0.0722))          # Rec.709 luma
t = clamp(l, 0, 1)                                # 查找坐标：端点饱和策略
段（pos_0=0、pos_3=1 固定；pos_1/pos_2 可调）：
  t' = clamp((t - pos_i) / max(pos_{i+1} - pos_i, 1e-4), 0, 1)   # 等位置守护
  s  = Linear ? t' : t'²(3-2t')                   # 分段 smoothstep
  mapped = mix(stop_i, stop_{i+1}, s)

```

- **HDR 策略（显式声明）**：查找坐标端点饱和——超白/负值 luma 映射到端点停靠色；
  端点色本身可为 HDR/高饱和（如 S3=(1.0,0.98,0.9)），**不是**把内容 RGB 裁到 0–1。
  区别于 Duotone 的线性外推（风格 ramp 的外推斜率任意，饱和更可预测）。
- **域**：scene-linear in/out；纯逐像素（零采样/零循环/零三角）；alpha 由宿主直通。
- **恒等/旁路**：`amount=0` 精确恒等（参数名用 amount：`mix` 是 GLSL 内建名，Color 侧 shader_compiler 保留字黑名单（AUD-0832）显式拒绝同名参数；无自然全 neutral 默认——映射默认改色，这是
  源映射类效果的 bypass 语义，见 specs/spec_schema.md §2）。
- **参数有效性**：每停靠位置/插值/Amount 在 gray_ramp_linear 夹具上有真实像素影响。

## ABI 表达力限制（对集成者的提案，见 patches/proposal_f16_n_stops.md）

当前 `.tglsl @param` 四类无法表达数组：本试点以 4 color + 2 position 滑杆表达
**恰好 4 停靠**（ABI 内合法，未把数组塞进 slider）。N 停靠（>4）需宿主原生
参数适配：建议 EffectSpec 增加 `ptype="color_stops"` 的原生数组 ParamDef
（[(pos, rgb)] 列表），Color 页参数面板与 Composite ParamDef 同步扩展；
未适配前发布版固定 4 停靠，不静默降级为另一份 Duotone。

## 交付物

| 文件 | 内容 |
|---|---|
| `gradient_map.tglsl` | 源码草稿（生产 ABI 逐字兼容：元数据块/裸参数/常量约束/Delta 不需要——零采样） |
| `component_manifest.json` | L01 契约（参数/域/能力矩阵默认 untested） |
| `hand_vectors.json` | 10 条手算向量（独立笔算，容差 1e-6） |
| `references/gradient_map_ref.py` | CPU oracle（float64，与 GLSL 逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量：10/10 一致（tests/content_library_v1/test_f16_gradient_map.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过（不触全局 registry）。
- GLSL 红线静态检查：无 while、无循环、无未声明 u_*（仅引擎三注入白名单）。
- **未验**：真实 GPU 编译/像素（L02 点处理子门）；Metal/CPU 导出；LUT 烘出
  （点处理+仅当前 RGB 依赖——LUT 可表达性成立，待 L20 矩阵实测）。
