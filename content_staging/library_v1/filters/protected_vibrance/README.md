# F03 protected_vibrance —— 肤色分区保护的自然饱和

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F03 行——Vibrance_Saturation 的保护权重是**色度幅值**反比
> （无色相区分）；本项差量为独立 **HSV hue 肤色区间**保护（中心/宽度可调）。

## 算法

```
luma = dot(color, (0.2126, 0.7152, 0.0722));  chroma = color - luma
hue = hsv_hue_deg(color)        # max/min 比值法；无彩色（Δ<1e-6）→ -1
d = |mod(hue - skin_hue_center + 180, 360) - 180|      # 环形距离（度）
w = max(skin_hue_width, 5)
protect = 1 - smoothstep(0.5w, w, d)     # [0,w/2] 全保护 → [w,0] 零保护
gain = 1 + saturation + vibrance·(1 - skin_protection·protect)
out = luma + chroma·gain                 # 亮度按构造精确保持
```

- **保护语义（对旧件差量，机器可辨）**：保护窗只衰减 `vibrance` 增益——
  `saturation` 不衰减（用户显式全局意图）；权重来自 **hue 区间**而非
  饱和度幅值：同 luma 同色度幅值、不同色相的两色增益不同
  （手算向量 #1/#2：肤色 gain=1 vs 互补蓝 gain=2）。
- **环形回绕**：色带跨 0/360 连续（默认 center=20°/width=25° → 有效带
  ≈355..45°；348° 品红在默认窗外、宽窗 60° 内被保护——见手算变体）。
  center 滑杆用 ABI 合法的 0..360 表达（min>max 会违反 manifest 校验），
  与"330..40 带宽"语义经环形距离等价。
- **HDR 策略（显式声明）**：hue 对负值/HDR 用同一比值法（无 clamp）；
  gain 对 chroma 线性缩放、无端点饱和——HDR/负值按同一增益外推，
  增益>1 时负通道绝对值放大（如 HDR 混合色向量），如实透传。
- **域**：scene-linear in/out；纯逐像素（零采样/零循环）；alpha 由宿主直通。
- **恒等/旁路**：`saturation=0 && vibrance=0` → 首行早退**精确**恒等
  （默认参数即恒等）；无彩色像素任何参数下逐位不变（chroma=0）。
- **保护权重可视化**：走允许入口（宿主 scope/mask 通道），本组件不自带
  可视化输出（manifest 能力未声明）。

## 交付物

| 文件 | 内容 |
|---|---|
| `protected_vibrance.tglsl` | 源码草稿（生产 ABI 逐字兼容：元数据块/裸参数/零循环/早退恒等） |
| `component_manifest.json` | L01 契约（参数/域/能力矩阵默认 untested） |
| `hand_vectors.json` | 14 条手算向量（独立一次性脚本，容差 1e-6） |
| `references/protected_vibrance_ref.py` | CPU oracle（float64，与 GLSL 逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量：14/14 一致（tests/content_library_v1/test_f03_protected_vibrance.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while、无循环、无未声明 u_*。
- **未验**：真实 GPU 编译/像素（L02 点处理子门）；肤色/彩色灯光实拍样片
  并排（调色师验收）；Metal/CPU 导出；LUT 烘出（纯逐像素 RGB 依赖，可表达性成立）。
