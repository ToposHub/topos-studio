# Resolve Hue–Saturation 色彩扭曲器校准

## 色相–饱和度算法 v2

视频工程的时间线设为 **Rec.709 Gamma 2.4** 时，空白色相–饱和度网格会显示 HSP，第一次有效编辑时随网格保存 v2 算法。已有的 HSV、v1 和 v2 网格继续按保存的算法渲染。v1 网格仍可在色彩扭曲器的 **⋯ → 将色相–饱和度算法升级到 v2** 主动升级，操作保留网格位移和锁定点，可撤销。

v2 从相同的 full-range Rec.709 Gamma 2.4 图卡中测量不同色相、饱和度和亮度的响应。渲染线程将节点网格编成连续的三维颜色响应。组合 Hue 和 Sat 时，切向移动随目标饱和度变化。其他点使用保亮度的参数模型，第一环的影响向外更快衰减。CPU、OpenGL、Metal 预览和导出使用相同的响应纹理。

Resolve Studio 20.2.1.6 的七组单项调整用于生成响应。以下设置整组留出。每组均以 4,096 个独立 RGB 色块比较，数值是 10 位 RGB 码值的均方根误差。

| 留出的设置 | v1 RMSE | v2 RMSE | 比 v1 降低 |
| --- | ---: | ---: | ---: |
| 青色第三环，Hue 0.43 + Sat 0.44 | 13.35 | 1.38 | 89.7% |
| 红色第三环，Hue 0.04 | 7.33 | 3.33 | 54.5% |
| 蓝色第五环，Hue 0.303 + Sat 0.83 | 21.62 | 15.45 | 28.6% |
| 绿色第二环，Hue 0.626 + Sat 0.48 | 19.40 | 18.44 | 4.9% |

七组单项调整的独立色块误差为 0.05–0.96 个码值。最内环的洋红色相误差从 2.25 降至 0.05，红色反向色相从 35.97 降至 0.96。蓝色外环和绿色内环仍主要使用参数模型。高密度网格、大范围多点联动、锁点影响和 HDR 工作空间尚未跨软件实测。

在 AMD Radeon RX 6800 上，四组色卡的 OpenGL 与 CPU、Metal 与 CPU 的 RMSE 分别低于 0.007 和 0.067 个 10 位码值。使用已缓存的 12×12 响应纹理时，单节点 4K OpenGL 图渲染约为 9.7 ms。高密度网格的多点拖拽需要重建纹理，延迟高于单点拖拽。

从 Resolve 捕获中重新生成响应可运行 `python -m tools.resolve_warper_calibration <captures> <response.npz> --validation <validation.npz>`。[生成工具](../../tools/resolve_warper_calibration.py) · [独立色块回归](../../tests/color/test_warper_response.py)

## 色相–饱和度算法 v1

以下是历史 v1 算法的实测记录。HSP 网格的拖拽支持实时预览，松手保存一次修改，Esc 取消本次拖拽。Hue 和 Sat 显示顶点的目标坐标，正向 Hue 从红走向品红。Luma 的中性值为 0.50，默认 6 环网格第三环的 Sat 显示为 0.58。

渲染在 Rec.709 的线性 RGB 上使用从 Gamma 2.4 样本拟合的径向和色相响应。网格允许独立设定两个轴的分辨率。环方向的预滤波在网格变化时更新，像素采样跨红色接缝连续。工程保存网格位移与算法版本。OpenGL、Metal、CPU 图执行和点态 LUT 烘焙都从同一网格重建响应。

### 测量

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
