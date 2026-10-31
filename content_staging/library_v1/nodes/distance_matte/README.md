# C18 lib1_distance_matte —— α→有符号距离场节点（**全帧声明**）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C18 行——无距离变换（depth_matte 用 depth AOV 非距离场），
> **新算法差量**；**不等同 erode/dilate**（matte_cleanup 仅结构元腐蚀/
> 膨胀——本节点开关在"到对类最近像素的度量距离"）。
> 计划工作单 §5：alpha→有符号距离场/边缘带/重建；metric、radius、
> threshold；验收点：距离单位、float 输出表示、分块 halo；无法证明
> tile 支持就声明 full-frame。

## ⚠️ 全帧声明子门

**全帧声明子门未开：本组件按 full-frame 声明——生产接线待集成者开
全帧预算/取消子门；不宣称 tile-safe。** 距离上界 = 画幅对角线
（有限，`_tile_padding` = 2204 @1920×1080 基准），但算法按整帧计算
（oracle 为精确暴力对拍，非近似传播）；分块 halo 收益不证明前不做
分块声明。全帧算法预算/取消归 L04 全帧预算子门。

## 交付物

- `lib1_distance_matte_node.py`：Node 类草案（metric/radius/threshold/
  output_mode）。
- `references/distance_matte_ref.py`：CPU oracle（新算法，无可复用核；
  精确暴力距离，分块广播防 O(N²) 内存尖峰）。

## 算法 / 恒等语义

- FG = alpha ≥ threshold（α 域不设上限——HDR matte 合法，α=5.0 即 FG）；
- d(p) = 到最近对类像素的度量距离（像素单位；Euclid/Chebyshev/
  Manhattan 三种 metric——同图互异，测试钉住）；
- 有符号：s = +d（FG）/ −d（BG）——**像素网格离散 SDF：边界像素
  |s| ≥ 1，零穿越在异类像素之间**（非插值连续 SDF，如实声明）；
- dn = clip(s/radius, −1, 1)（radius 像素 = 归一化长度）；
- **float 输出表示**（验收点）：α 通道承载结果（RGBA float32）——
  DistanceField → dn；EdgeBand → clip(1−|dn|, 0, 1)（边缘带，边界
  像素对峰值 1−1/radius）；Matte → clip(0.5+0.5·dn, 0, 1)（软重建，
  0.5 穿越在异类像素之间）；RGB 原样透传；
- 防御：全 FG/全 BG（无对类）→ dn = ±1 饱和；缺 α 通道 →
  ValueError（matte 源必须显式，不静默猜测）。

## 参数命名（AUD-0832）

metric / radius / threshold / output_mode 均不在
`shader_compiler._GLSL_RESERVED_IDENTIFIERS` 黑名单（已查
`src/features/color/core/shader_compiler.py`；无同类 plugins/.tglsl 撞名）。

## 验证现状

- marker 协议/1D 三种输出模式手算（dn=[1,0.5,−0.5,−1] @radius 2）/
  3×3 三 metric 分辨（Euclid √2 vs Chebyshev 1 vs Manhattan 2）/threshold
  分界/HDR α/无对类饱和/α 缺失显式报错/manifest 一致——已测
  （tests/content_library_v1/test_c18_distance_matte.py）。
  **未验**：L04 接线全家 + 全帧预算子门（集成者）。
