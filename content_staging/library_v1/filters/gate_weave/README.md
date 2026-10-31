# F08 gate_weave —— 门片位移/微旋转模拟（L12，静态版）

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F08 行——全库无门片位移类（无重复）。确定性帧身份：**不读
> 系统时钟**，位移由 seed 经整数格点散列决定。

## 算法

```
整帧常量位移（静态：每帧同一位移，由 seed 决定）：
  dx = amplitude_h·(h32((11,7), seed) - 0.5)        # 归一单位移 ∈ ±A/2
  dy = amplitude_v·(h32((23,5), seed) - 0.5)
  h32 = uint32 murmur3 风格格点散列（同 F07 常量族，README 常量族）
像素空间刚体逆变换（各向同性旋转，无宽高比失真）：
  p = uv·texSize（像素中心 (x+0.5, y+0.5)）
  v = p - 0.5·texSize - (dx,dy)·texSize
  q = R(-θ)·v，θ = radians(rotation)（绕画面中心微旋转 ≤0.5°）
  suv = (q + 0.5·texSize) / texSize
双线性重采样（像素中心制，Delta 惯例 out = color + (sample - base)）：
  Freeze：suv 钳到半纹素 [0.5/ts, 1-0.5/ts]（clamp-to-edge 等价，冻结边）
  Black：suv 出 [0,1]² 的像素输出 vec3(0.0)
```

- **单位**：amplitude 为归一化位移（相对画幅宽/高；0.02 = 2% ≈ 4K 下
  ~77px 上限、±A/2 实际幅面）；rotation 为度（0..0.5°）。
- **恒等**：`amplitude_h = amplitude_v = rotation = 0` **精确零值早退**
  （几何参数不用 `<0.001` 阈值早退——亚像素位移在阈值内会被吞，与 oracle
  保持逐式同构；这是与强度类参数早退惯例的有意差异）。
- **静态限制（L01 冻结口径）**：宿主无 frame/seed uniform——同一 seed
  每帧同一位移/旋转（整帧常量）；**动态逐帧抖动依赖正式 frame/seed 子门，
  落地前不声明**（specs/spec_schema.md §3）。
- **border 语义**：Freeze=位移后越界区域冻结边（clamp 采样）；Black=越界
  像素置 0（Delta 惯例下 = color - base；CPU oracle 中即 0）。
- **域**：scene-linear in/out，重采样不引入 clamp（HDR 值原样搬运）；
  alpha 由宿主直通。

## 交付物

| 文件 | 内容 |
|---|---|
| `gate_weave.tglsl` | 源码草稿（uint 散列 + 刚体逆变换 + 双线性；零循环；Delta 输出） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |
| `hand_vectors.json` | 10 条手算向量（8×4 与 64×32 ramp 夹具，独立脚本推导，容差 1e-6） |
| `references/gate_weave_ref.py` | CPU oracle（float64，散列/几何/采样逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量一致（tests/content_library_v1/test_f08_gate_weave.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while/无循环/u_* 白名单/半纹素 clamp 模式在场。
- **未验**：真实 GPU 编译/像素（L02 纹理邻域子门；GPU float32 sin/cos 与
  CPU float64 的差按邻域采样 2e-4 相对容差起步）；Metal/CPU 导出；
  动态逐帧抖动（frame/seed 子门）。
