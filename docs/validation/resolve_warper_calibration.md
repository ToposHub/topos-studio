# Resolve Hue–Saturation 色彩扭曲器校准

在 Topos 的「色彩扭曲器 → 色相–饱和度」点击 **709**。空白网格会进入 HSP，已编辑的 HSV、HSL 网格需先显式切到 HSP。开关随校正节点保存。开启后的拖拽支持实时预览，松手保存一次修改，Esc 取消本次拖拽。Hue 和 Sat 显示顶点的目标坐标，正向 Hue 从红走向品红。Luma 的中性值为 0.50，默认 6 环网格第三环的 Sat 显示为 0.58。

渲染在 Rec.709 的线性 RGB 上使用从 Gamma 2.4 样本拟合的径向和色相响应。网格允许独立设定两个轴的分辨率。环方向的预滤波在网格变化时更新，像素采样跨红色接缝连续。工程保存网格位移与算法版本。OpenGL、Metal、CPU 图执行和点态 LUT 烘焙都从同一网格重建响应。

## 测量

Resolve Studio 20.2.1.6 使用 DaVinci YRGB、Rec.709 Gamma 2.4、Full 电平。复用一级调色的 RGB 图卡，渲染 16 位 RGB TIFF，直接读取色块码值。拟合使用 `33³` RGB 网格中的子集。验证使用 4,096 个独立的 RGB 色块，另有 1,024 个灰阶检查中性色。配套脚本生成和读取图卡，比较无需查看器截图或 ICC。 [图卡工具](../../tools/resolve_primary_calibration.py)

| Resolve 设置 | Topos 相对未调色输入的 RGB RMSE 降幅 | 绝对 RMSE（10 位码值） |
| --- | ---: | ---: |
| 红色第三环，Sat 0.74 | 77.6% | 5.76 |
| 红色第三环，Hue 输入 0.075 | 63.7% | 10.31 |
| 红色第三环，Luma 0.63 | 84.9% | 1.84 |
| 青色第三环，Hue 0.43、Sat 0.44 | 69.4% | 13.35 |

采用相同设置比较独立色块。降幅指误差相对「用原始图卡作为输出」的改善。四组操作设置均参与拟合。更高密度网格、跨点叠加、极端调整以及 HDR 工作空间的跨软件误差仍待测量。

本机 OpenGL 与 CPU 的误差低于 0.001 个 10 位码值，Metal 与 CPU 的误差低于 0.064 个。四个样本中，OpenGL 与 Resolve 的灰阶最大差值约 0.25 个 10 位码值。

锁点约束画布顶点，临近编辑的渲染影响仍随插值连续衰减。自动锁定的色域响应、取色分布、Chroma–Luma 和 Chroma Warp 未纳入本轮实测校准。 [Blackmagic Design 调色师指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf)
