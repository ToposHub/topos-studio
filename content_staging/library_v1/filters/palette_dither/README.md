# F12 palette_dither —— 调色板量化 + 有序抖动

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F12 行——Pixel_Blocks 是逐通道**均匀阶梯**量化+4x4 Bayer，
> 无任意调色板最近色；本项差量为 4 色程序调色板 + RGB 最近色 + Bayer 相位偏移。

## 算法

```
probe = clamp(color, 0, 1)                    # 显示域策略（显式声明，见下）
if dither == "Ordered 4x4":
    b   = BAYER4[y%4][x%4]                    # 像素坐标 = uv·u_tex_size
    φ   = (b + 0.5)/16 − 0.5                  # ∈ [-0.46875, 0.46875]
    Δ_c = (max_i p_i,c − min_i p_i,c) / 3     # 逐通道：调色板极差的 4 色阶梯近似
    probe += φ · strength · Δ
best = argmin_i ‖probe − palette_i‖²          # 平局取低索引
out  = mix(color, best, strength)
```

- **显示域策略（显式声明）**：本件是**显示域艺术效果**——最近色查找先
  `clamp(color,0,1)` 进调色板域再比较。这不是把管线 HDR 裁剪：输出是与
  **原值**（未 clamp）的线性混合，strength<1 时超白/负值按 strength 残留
  （手算变体：HDR 半强度 (12,0.1,4.5)@0.5 → (6.48,0.525,2.7)）。
- **对 Pixel_Blocks 的差量**：均匀阶梯 vs **任意 4 色**调色板最近色；
  抖动偏移幅度按调色板极差自适应（退化调色板 Δ=0 抖动自动失效，数值安全）。
- **程序资源**：调色板用 4 个 color 参数表达（无外部资源/hash 闭包）；
  蓝噪固定 pattern 需资源子门（M20），暂以程序 Bayer 4x4 先行
  （矩阵与 Pixel_Blocks 同源，标签口径一致）。
- **位置相关（确定性）**：Bayer 相位只由像素坐标 mod 4 决定，无随机流、
  无帧依赖（time_domain=static）；同输入不同像素可映射不同调色板色
  （手算变体：暖橙输入在 (1,0)→p2、(0,0)→p1 翻转）。
- **最近色展开**：4 路完全展开比较（无循环——点处理红线兼容），平局取
  低索引（与 oracle `np.argmin` 同口径）。
- **恒等/旁路**：`strength=0` → 首行早退**精确**恒等（抖动幅度与混合比
  同由 strength 缩放——双语义如实声明）；默认参数为满强度量化（非恒等，
  风格效果 bypass 语义同 F16）。
- **域**：scene-linear in/out（效果内部显式显示域查找）；逐像素、不采样
  邻域（仅读 u_tex_size 取像素坐标）；alpha 由宿主直通。

## 交付物

| 文件 | 内容 |
|---|---|
| `palette_dither.tglsl` | 源码草稿（生产 ABI 逐字兼容：元数据块/裸参数/零循环/早退恒等） |
| `component_manifest.json` | L01 契约（参数/域/能力矩阵默认 untested） |
| `hand_vectors.json` | 15 条手算向量（独立一次性脚本，容差 1e-6；含像素坐标） |
| `references/palette_dither_ref.py` | CPU oracle（float64，与 GLSL 逐式同构；evaluate 带可选 pixel_xy） |

## 验证现状

- CPU oracle ↔ 手算向量：15/15 一致（tests/content_library_v1/test_f12_palette_dither.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while、无 for 循环（4 路展开）、无未声明 u_*
  （仅 u_tex_size 白名单成员）。
- **未验**：真实 GPU 编译/像素（L02 点处理子门）；蓝噪 pattern（资源子门）；
  Metal/CPU 导出；LUT 烘出（**位置相关——逐像素 LUT 不可表达**，导出须
  走 per-pixel 路径，L20 如实验证）。
