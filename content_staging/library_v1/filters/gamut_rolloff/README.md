# F05 gamut_rolloff —— 色度幅值软膝滚降（L12）

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F05 行——全库无 gamut 压缩项（`Color_Space_Transform.tglsl`
> 是 CSC+tone map，非 rolloff），无重复。

## 算法

```
l     = dot(rgb, (0.2126, 0.7152, 0.0722))       # Rec.709 luma（严格保持）
chroma = rgb - l；m = |chroma|                    # 欧氏色度幅值（近似，见下）
m ≤ threshold → 恒等（灰像素 m=0 恒等）
m > threshold：
  ex   = m - threshold
  roll = smoothstep(clamp(ex / knee, 0, 1))       # [thr, thr+knee] 软过渡；knee=0 → roll=1 硬膝
  m'   = threshold + ex·(1 - strength·roll)
out   = l + chroma·(m'/m)                         # 色相方向不变，仅缩色度向量
```

- **不 clamp RGB**：负值/HDR 透传（输出只有色度向量长度变化，luma 可为
  任意实数；手算向量含 -4.0 与 (12,0.1,4.5) 探针）。
- **恒等/旁路**：`strength=0` 精确恒等（GLSL 首行早退 + oracle 同阈值镜像）；
  `m ≤ threshold` 的像素逐像素恒等；`threshold` 到达上限（4）时常规素材
  （色度幅值 < 4）整体恒等。
- **工单句修正记录**：工单原文"threshold≤0.5 恒等"与公式方向矛盾
  （阈值越低越多像素满足 m>threshold，压缩更强而非恒等）；按公式语义
  重推为上述"m≤threshold 恒等 + strength=0 恒等"，工单句按笔误记录。

## 近似与域声明（M19 口径）

- `m` 为 RGB 空间**欧氏色度幅值**，非完整 gamut 边界求解（不做
  primaries/白点映射、无 hue-dependent 边界距离）；README 声明此近似。
- 工作域假设 **scene-linear、Rec.709 primaries 近似**——按 M19.1 已验
  子集口径声明（M 计划工作/显示色彩契约的已验部分），非完整 ICC 级声明。
- 与 CST/ODT 分工：本项不做色域空间转换（无 primaries 变换），只做
  当前工作域内的色度压缩；显式区别见台账。
- `@export_support opengl`：域变换逐像素、原则上可 LUT 烘出，但依赖上述
  色度域近似假设——**LUT 烘出不声明**（能力矩阵保持 untested，待 L20
  矩阵实测后再议）。

## 交付物

| 文件 | 内容 |
|---|---|
| `gamut_rolloff.tglsl` | 源码草稿（生产 ABI 逐字兼容：元数据块/裸参数/零循环零采样零 u_*） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |
| `hand_vectors.json` | 8 条手算向量（独立脚本推导，容差 1e-6） |
| `references/gamut_rolloff_ref.py` | CPU oracle（float64，与 GLSL 逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量一致（tests/content_library_v1/test_f05_gamut_rolloff.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while/无循环/无 u_*（零采样点处理）。
- **未验**：真实 GPU 编译/像素（L02 点处理子门）；LUT 烘出（有意不声明）。
