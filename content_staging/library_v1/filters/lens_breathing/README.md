# F11 lens_breathing —— 呼吸式缩放（L12，静态版）

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F11 行——`plugins/Lens_Distortion.tglsl` 是静态 r² 多项式
> 重映射+auto-zoom（桶/枕形畸变）；本项差量为**可动画的呼吸缩放**
> （统一缩放无畸变项 + 可移动 pivot + amount 插值）。

## 算法

```
s   = 1 + amount·(scale - 1)                 # amount∈[0,1] 在 1 与 scale 间插值
suv = pivot + (uv - pivot)/s                 # 以 pivot 为中心的统一缩放采样
    （归一坐标统一缩放 = 画幅内等比，内容几何保持；无需宽高比校正；旋转 0）
双线性重采样（像素中心制，Delta 惯例 out = color + (sample - base)）：
  Freeze：suv 钳半纹素 [0.5/ts, 1-0.5/ts]（clamp-to-edge 等价）
  Black：suv 出 [0,1]² 的像素输出 vec3(0.0)
```

- **恒等**：`amount=0` 或 `scale=1` **精确零值早退**（几何参数不用 `<0.001`
  阈值早退——亚阈值缩放会被吞且与 oracle 失同构；与 F08 同一纪律）。
- **动画口径（工单原文）**：先提供**静态参数 + 既有关键帧**——amount/scale/
  pivot 均为普通滑杆参数，动画走宿主既有 keyframe 通道（参数可关键帧）；
  **不声称自动估计真实镜头焦距**（呼吸量是用户参数，无焦距反求）。
- **与 F08 分工**：F08=位移+微旋转（seed 驱动、帧常量）；本项=纯缩放
  （参数驱动、无 seed）；两者零旋转/零畸变交叉。
- **域**：scene-linear in/out，重采样不引入 clamp（HDR 值原样搬运）；
  alpha 由宿主直通。

## 交付物

| 文件 | 内容 |
|---|---|
| `lens_breathing.tglsl` | 源码草稿（单采样统一缩放；零循环；Delta 输出） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |
| `hand_vectors.json` | 10 条手算向量（8×4 ramp 夹具，独立脚本推导，容差 1e-6） |
| `references/lens_breathing_ref.py` | CPU oracle（float64，几何/采样逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量一致（tests/content_library_v1/test_f11_lens_breathing.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while/无循环/u_* 白名单/半纹素 clamp 模式在场。
- **未验**：真实 GPU 编译/像素（L02 纹理邻域子门）；Metal/CPU 导出；
  关键帧动画链路（走宿主既有通道，不在本组件范围）。
