# C11 lib1_polar_coordinates —— Cartesian↔Polar 坐标变换节点

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C11 行——全库无极坐标变换（正/反向），**新算法差量**；
> 与 Crop/Transform/Mirror 无等价（无角向缠绕语义）。
> 计划工作单 §5：Cartesian↔polar，中心/角度/半径/滤波；验收点：接缝/
> 中心奇点/反变换；bbox/采样域。

## 交付物

- `lib1_polar_coordinates_node.py`：Node 类草案（mode/center/angle_span/
  radius_scale/filter）。
- `references/polar_coordinates_ref.py`：CPU oracle（新算法，无既有核
  可复用——L00 判定"无等价"）。

## 算法 / 恒等语义

- 坐标约定：像素中心 = 整数索引（与 C10 kaleidoscope 同构）；center
  frame-normalized → cx = u·(W−1)、cy = v·(H−1)；
  r_max = radius_scale·min(W,H)/2；
- **CartesianToPolar**：输出像素 (x,y) 的极坐标 (θ, r) → 采样输入
  (u, v) = (0.5 + θ/span_rad, r/r_max)——输入 x 轴=角度、y 轴=半径
  （矩形图卷成极坐标视图）；
- **PolarToCartesian**（F⁻¹）：输出像素 frame 归一化坐标 (uq, vq) →
  θ = span_rad·(uq−0.5)、r = vq·r_max → 采样输入
  center + r·(cos θ, sin θ)——极坐标图展开回矩形；
- **反变换验收**（位置级）：G 的采样位 p' 代回正向映射 F 还原
  (uq, vq)——F∘G = id（独立标量重推，atol 1e-9）。**如实记录的数值
  例外带**：p' 经 float 舍入恰好落在中心行时（p'y == cy，sin(±π)
  符号被吞），atan2(+0.0, dx<0) = +π 把 θ 推到对侧接缝（0↔1）——
  零符号丢失的固有简并，测试独立重推判定并排除（命中数 >0 断言）；
  v=0 行（半径零奇点）同理塌缩到 center（uq≠0.5 的点在 F 下无原像，
  角度轴消失）；**图像级两趟往返另含中间重采样插值误差**（非线性
  映射下双线性 J(p') ≠ 连续 J(p')，随机噪声图 ~0.3 量级）——不作
  逐像素恒等断言；Nearest 下因浮点 round 边界不保证逐位；
- angle_span（度）< 360 时 |θ| > span/2 的输出采样位落在 [0,1] 外 →
  clamp-half-texel 边缘钳制（索引钳 [0, n−1]）；
- **接缝**：θ = ±π 处 u 跳变 1→0——Nearest 硬缝、Linear 在钳制边缘
  插值衰减（GPU 环形采样需 cube-map 式寻址，接线期声明，本口径为
  平面 clamp）；
- **中心奇点**：r → 0 时 v → 0（采样 y 轴顶行；center 像素自身
  θ = atan2(0,0) = 0 → 采样 (0.5, 0) 顶行中列）；
- α 通道参与重映射（整图重定位，非透传）；内容色不 clamp（HDR 透传）；
  filter=Nearest 为 oracle 钉定口径，Linear 双线性同构实现。

## 多子门声明

- 无多输入/多输出/全帧问题：单输入逐像素 UV 重映射（采样域整幅——
  **非 tile-safe**，tile_padding = 对角线上界 2204 @1920×1080 基准，
  width-aware 换算属接线期）。

## 验证现状

- marker 协议/中心-正东-对角手算采样位/反变换往返（Linear）/滤波差异/
  参数有效性/α 重映射/manifest 一致——已测
  （tests/content_library_v1/test_c11_polar_coordinates.py）。
  **未验**：L04 接线全家（多输入/多输出子门不涉及本项）。
