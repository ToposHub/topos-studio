# F02 subtractive_density —— 减色密度/染料串扰

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F02 行——最近似 Cross_Process 是每通道有理曲线+染料雾**加性**串扰，
> 非减色密度模型；本项为全新算法候选（N）。

## 算法

```
δ = density · σ                    # 染料层密度，σ = (0.85, 0.95, 1.10)（玩具常数，蓝层最强）
A = (1-k)·I + k·C                  # 吸收串扰矩阵；C 行和 1（k=dye_coupling）
C = [[0.88,0.08,0.04],[0.10,0.84,0.06],[0.05,0.15,0.80]]
D = A·δ                            # 通道有效密度 = 各染料层吸收加权和
τ = exp(-D)                        # Beer-Lambert 透射
raw = color · τ                    # 乘性：HDR 线性外推，无 clamp
raw += W·(luma_in − dot(raw,W)) / dot(W,W) · luma_protection
                                   # 精确 709 亮度代数补偿（p=1 完全保持）
out = mix(color, raw, amount)
```

- **与 saturation/Cross_Process 的数值差量**：saturation 是色度向量增益（保 luma 分解）；
  Cross_Process 是有理曲线+加性雾。本件是**密度域**模型——先按染料层吸收矩阵
  混合密度再指数透射，负值通道/超白按同一透射率缩放，不裁不外推。
- **不宣称商业胶片**：σ 与 C 为可解释玩具常数（非任何特定胶片的光谱密度）。
- **亮度补偿精确性**：分母是 `dot(W,W)`≈0.5619 而非 1——不带除数的 `W·Δ`
  补偿只回补约 56% 亮度（常见笔误，本库显式修正并在手算向量钉住）。
- **域**：scene-linear in/out；纯逐像素（零采样/零循环，唯一超越函数 exp）。
- **恒等/旁路**：`density=0`（任意 coupling/protection/amount）或 `amount=0`
  → 首行早退**精确**恒等。默认参数（density=0）即恒等。

## 交付物

| 文件 | 内容 |
|---|---|
| `subtractive_density.tglsl` | 源码草稿（生产 ABI 逐字兼容：元数据块/裸参数/零循环/早退恒等） |
| `component_manifest.json` | L01 契约（参数/域/能力矩阵默认 untested） |
| `hand_vectors.json` | 12 条手算向量（独立一次性脚本，容差 1e-6） |
| `references/subtractive_density_ref.py` | CPU oracle（float64，与 GLSL 逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量：12/12 一致（tests/content_library_v1/test_f02_subtractive_density.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while、无循环、无未声明 u_*。
- **未验**：真实 GPU 编译/像素（L02 点处理子门）；与饱和度/交*冲洗的视觉并排
  （调色师样片）；Metal/CPU 导出；LUT 烘出（逐通道可分?——有串扰矩阵但仍逐像素
  纯 RGB 依赖，LUT 可表达性成立，待 L20 实测）。
