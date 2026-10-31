# F07 seeded_grain —— 可复现胶片颗粒（L12，静态版）

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F07 行——`plugins/Film_Grain.tglsl` 为 hash value-noise 但
> **无 seed/无 frame uniform/无彩色颗粒**；本项差量为 seed 进入格点散列 +
> 彩色相关颗粒。

## 算法

```
格点散列（uint32 murmur3 风格，与库 fixtures/_lattice_hash 同式，位宽 32）：
  h = (x·0x9E3779B1) ^ (y·0x85EBCA77) ^ (s·0xC2B2AE3D)
  h ^= h>>15; h *= 0x2545F491; h ^= h>>13; h *= 0x9E3779B1; h ^= h>>16
  v = h/2^32 ∈ [0,1)                     # s = uint(int(seed))：seed 进入 hash
cell = max(grain_size, 1) 输出像素；gp = uv·texSize/cell（像素中心）
4-tap 平滑 value noise（smoothstep fade，无 sin/cos 无纹理采样）→ n∈[0,1) → 2n-1
  n0 = noise(gp, s)          # 单色相位
  n1 = noise(gp, s+1001)     # 彩噪 R 相位（独立 hash 相位）
  n2 = noise(gp, s+2002)     # 彩噪 B 相位
亮度加权（clamp 仅权重域，不触碰输出色）：
  l = clamp(dot(rgb, 709W), 0, 1)
  w = 4l(1-l) + (1-l)²·shadow_bias + l²·highlight_bias      # 乳剂密度响应
加性颗粒（HDR 安全不 clamp）：amp = strength·0.12
  R += (n0 + n1·chroma)·amp·w；G += n0·amp·w；B += (n0 + n2·chroma)·amp·w
```

- **静态版（L01 冻结口径）**：宿主 `.tglsl` ABI 无 frame/seed/time uniform——
  同一 seed 每帧输出相同（无 frame 输入）；`time_domain=static`，
  `seed_policy=fixed-param-default`（复制沿用）。**动画模式（逐帧颗粒演化）
  依赖正式 frame/seed 子门，落地前不声明**——见 specs/spec_schema.md §3。
- **seed 语义**：seed 滑杆 0..100（整数语义，GLSL 侧 `uint(int(seed))`），
  改变 seed 即改变全部格点散列值 → 输出图案整体改变（测试钉住）。
- **恒等**：`strength=0` 精确恒等（首行早退，oracle 同阈值镜像）；
  `chroma=0` 退化为纯单色颗粒（三通道同偏移，与 Film_Grain 同型）。
- **域**：scene-linear in/out；加性增量全程不 clamp 基色（HDR 4.5/12 探针
  直通 + 有界偏移）；alpha 由宿主直通。

## 与 Film_Grain 的差量（验收点）

| 维度 | Film_Grain | 本项 |
|---|---|---|
| 散列 | Dave Hoskins float hash（无 seed） | uint32 整数散列 + seed 通道 |
| 颗粒色 | 纯单色 | 单色 + 双相位彩色颗粒（chroma 滑杆） |
| 可复现性 | 固定图案（无入口改变） | seed 滑杆改变图案（0..100） |
| 动画 | 无（引擎不注入时间） | 同样无——静态声明一致（frame 子门前不发动态） |

## 交付物

| 文件 | 内容 |
|---|---|
| `seeded_grain.tglsl` | 源码草稿（uint 散列 + 3 相位 value noise；零循环/零纹理采样，textureSize 取尺寸） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |
| `hand_vectors.json` | 8 条手算向量（8×4 常色夹具，独立脚本推导，容差 1e-6） |
| `references/seeded_grain_ref.py` | CPU oracle（float64，uint32 散列逐位同构） |

## 验证现状

- CPU oracle ↔ 手算向量一致（tests/content_library_v1/test_f07_seeded_grain.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while/无循环/无未声明 u_*（textureSize(u_input_tex,0) 白名单）。
- **未验**：真实 GPU 编译/像素（L02 子门；uint 散列 GPU 逐位一致性与 float32
  噪声容差按邻域口径 2e-4 起步）；Metal/CPU 导出；动态逐帧颗粒（frame/seed 子门）。
