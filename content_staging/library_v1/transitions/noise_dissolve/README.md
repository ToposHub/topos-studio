# X06 noise_dissolve —— 程序 rank-field 溶解

> 状态：**component_ready**（暂存独立 CPU 核；未接宿主）。
> 台账：L00 §2 X06 行——dissolve 族四项均全局混合无噪声场
> （transition_catalog.py:25-92）；差量表 §5"固定种子，噪声连续性可控；
> 区别已有 film dissolve"。mask 参数现状无 seed 类参数（L00 接缝清单）。

## 算法

```
gp = ((i+0.5)/W·scale, (j+0.5)/H·scale)     # 帧归一格坐标（分辨率无关）
Value: rank = 4-tap 双线性 value noise（smoothstep fade）
Cell : rank = hash(最近格点 floor(gp+0.5))   # 硬胞元
hash : uint32 murmur3 风格雪崩（x·0x9E3779B1 ^ y·0x85EBCA77 ^ s·0xC2B2AE3D，
       15/13/16 位移 + 0x2545F491/0x9E3779B1 乘子）→ rank = h/2^32 ∈ [0,1)
mask = clamp((p − rank)/max(soft,1e-6), 0, 1)
```

- 散列与库 references/seeded_grain_ref.lattice_hash_u32 同式（位宽 32 与
  GLSL 330 uint 逐位一致）；**本核文件内自带副本**，不 import 参考代码。
- p=0 → rank≥0 → 全 0；p=1 → rank<1 但可逼近 1，**端点早退**保证比特级全 1。
- p 单调：rank 逐像素固定，mask 随 p 线性不减。
- seed 静态（固定参数，无 seed 动画；time_domain=static）——同一 seed 图案
  逐帧确定，转场时序完全由 progress 承载。
- 坏参数防御：noise_mode 非法回退 "Value"、scale 取整 clamp [2,64]、
  seed 取整 clamp [0,100]、softness clamp [0,0.5] + 1e-6；NaN/Inf 回退默认。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| noise_mode | "Value" | Value/Cell | 双线性 value（平滑）或最近格点（硬胞元） |
| scale | 16 | 2–64 int | 帧归一格点数（分辨率无关） |
| seed | 0 | 0–100 int | 固定种子 |
| softness | 0.05 | 0–0.5 | 前沿羽化宽（rank 域） |

## 交付物

| 文件 | 内容 |
|---|---|
| `noise_dissolve_core.py` | 独立 CPU mask 核（自带 lattice_hash_u32；float64 → float32） |
| `hand_vectors.json` | 6 组手算向量（Value/Cell 二值场、羽化全场、seed 区分、端点；rank 场随注记留档） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |

## 验证现状

- CPU 核 ↔ 手算向量一致（独立脚本：Python 大整数按 uint32 截断模拟散列，
  与 numpy uint64 中间域逐位一致；不复用核代码；无冲突）。
- **未验**：GPU spec/GLSL/MSL 三端；L03 mask 子门+seed 参数 uniform 接缝；
  真实命令/预览/导出链。
