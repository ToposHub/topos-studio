# F01 wb_adaptation —— 显式白点色适应

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F01 行——White_Balance_Advanced 是固定系数通道增益（temp/tint），
> 不是白点色适应；本项差量为 source/target 白点 + Bradford/XYZ Scaling 可解释模型。

## 算法

```
W(rgb)  : xyz = RGB2XYZ·rgb; s = Σxyz
          s<1e-6 或 Y<1e-6 → 回退 D65 xy=(0.3127,0.3290)      # 守护
          (x,y) = xyz.xy / s;  W = (x/y, 1, (1-x-y)/y)        # xy 色度近似，Y=1
ws = W(source_white); wt = W(target_white)
Bradford    : M = A⁻¹ · diag(ρt/ρs) · A      # ρs=A·ws, ρt=A·wt（锥响应）
XYZ Scaling : M = diag(wt/ws)                # von Kries 对角模型下界
T = XYZ2RGB · M · RGB2XYZ                    # 3×3 线性变换（逐像素施加）
out = mix(color, T·color, strength)
```

- **HDR 策略（显式声明）**：纯 3×3 线性代数，无 clamp——超白/负值按同一矩阵
  外推；饱和色个别通道可能越界为负（如青色示例向量），如实透传给下游。
- **白点语义**：color 参数取白点的**色度**（幅值无关，Y 归一）；xy 色度坐标
  近似是显式声明的一阶近似（不做完整光谱加权）。工作空间冻结：Rec.709/sRGB
  线性原色（D65 基）固定 RGB↔XYZ 矩阵（Lindbloom 常数）。
- **域**：scene-linear in/out；纯逐像素（零采样/零循环）；alpha 由宿主直通。
- **恒等/旁路**：`source==target` 白点（Y=1 XYZ 距离 < 1e-6）→ 首行早退**精确**
  恒等（不走 A⁻¹·A 浮点往返）；`strength=0` 同样精确恒等。默认参数即恒等
  （双白点默认 (1,1,1)=D65）。
- **对拍锚点**：白点输入映射到白点输出——`evaluate(source_white)` 的 XYZ 比例
  精确等于 target 白点色度（两模型皆成立，见测试 test_white_maps_to_white）。

## 交付物

| 文件 | 内容 |
|---|---|
| `wb_adaptation.tglsl` | 源码草稿（生产 ABI 逐字兼容：元数据块/裸参数/零循环/u_* 白名单/早退恒等） |
| `component_manifest.json` | L01 契约（参数/域/能力矩阵默认 untested） |
| `hand_vectors.json` | 12 条手算向量（独立一次性脚本，容差 1e-6） |
| `references/wb_adaptation_ref.py` | CPU oracle（float64，与 GLSL 逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量：12/12 一致（tests/content_library_v1/test_f01_wb_adaptation.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过（不触全局 registry）。
- GLSL 红线静态检查：无 while、无循环、无未声明 u_*（仅引擎三注入白名单）。
- **未验**：真实 GPU 编译/像素（L02 点处理子门）；Metal/CPU 导出；LUT 烘出
  （纯 3×3 线性——LUT 可表达性成立，待 L20 矩阵实测）。
