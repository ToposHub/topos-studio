# Topos 与 DaVinci Resolve 调色对照测试指南

用这套流程对齐调色工具的拖拽手感、参数读数和画面输出。先记录 Resolve 与当前 Topos 的基线，再修改 Topos。手感用同一组指针手势比较，渲染效果用同一个参数设定比较。最后复跑整条操作路径，从拖动到松手、保存和导出。

## 1. 建立可复跑的测试环境

1. 记录 Resolve 版本、Topos 提交及工作区状态、校准版本、操作系统、GPU、实际渲染后端、显示缩放、输入设备和测试分辨率。写明工具、模式、通道、默认值、数值单位，以及会影响它的其他开关。
2. 为 Resolve 建立独立测试工程，为 Topos 建立对应测试项目。每组从同一个中性节点和原始素材开始。关闭其他节点、LUT、自动调色和无关效果。切过面板模式后，确认上一模式也没有残留调整。
3. SDR 对照可从 **DaVinci YRGB、Rec.709、Gamma 2.4、Full 电平**开始，按现有图卡脚本关闭 Resolve 的色彩空间感知调色工具。两边匹配素材解释、工作色域、输出变换、分辨率和帧号。用无损 16 位 RGB TIFF 取输出码值。若 Topos 的被测阶段接收线性 Rec.709，先解码图卡，再按同一 Gamma 2.4 编码后比较。
4. 先导出两边的中性帧，与原始输入和彼此对拍。若中性帧已经有明显的亮度、色偏、位置或边缘差异，先查色彩管理、Full/Video 电平、缩放、帧号和渲染路径。现有图卡脚本对 Resolve 中性帧容许的最大偏差为 **2 个 16 位码值**。Screen 和系统 ICC 用于显示，像素比较直接读取导出文件。截图可用于记录控件位置和读数。

工具依赖 Log、HDR、色彩管理模式、RAW 解码或时间域状态时，另外建立符合该工具用途的素材和工程。例如 Camera RAW 要用两边均能解码的同一 RAW 原片并固定解码设置。测 HDR 时记录输入、工作域、输出域和参考白，给两边保留超白的导出格式，从该域的中性帧重新验收。SDR 图卡的结果只覆盖已测的色彩条件。

## 2. 用相同手势测交互

1. 从同一默认值或同一非中性值开始。分别测拨轮、数值格、控点、滑条等入口，包括共享参数的其他面板。先用相同的逻辑像素位移和事件节奏，长短各一档。横向数值格可用 ±60 和 ±120 逻辑像素。控点补测横向、纵向和两条对角线，记录限制边界与反拖。连续拖动时用数个移动事件覆盖整段路径，同时核对松手位置。
2. 每次保留起点、终点、移动方向、时长、修饰键、实际数值读数和控点位置。若记录来自屏幕像素，按显示缩放换算后再与 Qt 逻辑像素对照。圆盘、曲线和网格同时记录有效绘图区尺寸及归一化坐标。圆盘以中心为原点，坐标以有效半径为单位。曲线需记录横纵轴方向和刻度。
3. 分别计算 **每逻辑像素的显示值变化**、**控点实际位移 ÷ 指针位移**。对二维控点，还要在两边把控点停到相同的归一化位置，再比较 RGB、色相或饱和度读数，以检查方向映射。重复一段同位移手势，用相同起点和不同速度检查结果是否稳定。需要比较延迟时，再通过录屏或事件时间戳测移动到首次更新及松手到最终更新的时间。
4. 在 Topos 用正常控件复跑同一手势，核对显示值、实时预览、松手后的最终值、撤销与重做。支持取消、复位、模式共享或键盘修饰键的工具也按对应入口检查。把两边的实际读数记下来，留给下一步直接录入复测。

例如，一级色轮实测把拖盘控点、底部主亮度拨轮和 Y/RGB 数值格分别记录。它们横拖同样的距离时，显示数值与画面响应各有差别。

## 3. 用相同参数测输出

通过数值输入、明确的归一化锚点坐标或工具本身的设置录入同一参数。核对两边实际读回的值，再导出同一帧。参数同名时也要确认单位和中性值。若受取值范围、精度或存储自由度限制，记录实际值与换算，并在报告里标明该组的可比范围。对无数值输入的控件，记录定位方法和误差。

| 被测内容 | 输入与用例 |
| --- | --- |
| 点态颜色响应 | 用现有图卡的 `33³` RGB 网格测颜色映射，并用 4,096 个独立彩色色块和 1,024 个灰阶点评估。覆盖正反方向、低中高幅度和易出偏差的肤色、近中性、高饱和及暗部。 |
| 多参数和依赖项 | 单项外补采联合设置。让依赖项在可见的条件下工作，例如 Pivot 搭配非中性对比度，Log 范围搭配 Log 色轮偏移，Lum Mix 搭配相关的亮度调整。也测依赖项单独修改时应有的表现。 |
| 曲线、柔化裁切、扭曲器 | 曲线测单点、多点、端点和区段组合。软裁切检查软膝内外及极端点。扭曲器检查不同色相、内外环、正反方向、红色接缝和多点组合。 |
| 空间或时间响应 | 空间工具用多亮度、多尺度纹理、斜边、强边缘及代表性实拍，保持原分辨率并比较整帧或同一 ROI。时间工具用相同帧率、时间码、预滚和历史帧，测静止、运动、切镜和序列稳定性。 |

采样前约定哪些用于拟合、哪些用于验收。用于拟合的色块与独立验证色块要分开，还要留出**整组未参与拟合的参数**，如档位间的插值、非默认设置和混合操作。同一采集档位上的独立色块检验颜色插值，整组留出检验未见过的参数组合。若用验收样本调了模型，下一轮另留一组。用一级调色脚本的 `profile` 生成响应时，单项设置会进入拟合，留出用例应保存在单独的验证目录里。

## 4. 量化差异并定位原因

- 每个用例记录未调色输入、改动前 Topos、改动后 Topos 相对 Resolve 的误差。RGB 和灰阶分别报告 **10 位 RGB RMSE**、**逐样本最大通道误差的 P95**、**最大通道误差**。把归一化的码值差乘以 1023，便于复用 16 位 TIFF 的测量结果。发现局部差异时，标出色域、灰阶段、纹理区域或具体帧号。
- 改善率要写明基准。相对旧版的 RMSE 降幅为 `1 - 新 RMSE / 旧 RMSE`，相对未调色输入的降幅使用另一套基准。基准误差为零时只报绝对误差。降幅描述指定用例上的误差改善。
- 若同一手势的读数有差异，而同一参数的输出已接近，优先修控件的映射、速度或舍入。若相同参数仍有颜色差异，检查实际参数是否进了渲染、计算所用的颜色域、运算顺序、通道权重和边界处理。某一档正常、动一个最小刻度就跳变时，补测阈值两侧。拖动中和松手后画面不同则同时追查预览状态与提交状态。
- 灰阶响应、矩阵或解析式能解释实测时，优先用这些模型。点态且跨通道的响应可用 3D 网格或 LUT 拟合，保留中性直通和边界行为。带空间、时间或历史依赖的工具用对应的算子和样本验证。用同一套基线和留出集复测每次修改。

## 5. 验收与归档

1. 在 Topos 的生产预览和导出入口复核相同输入，并记录实际使用的 CPU、OpenGL、Metal 或回退路径。跨后端误差与跨软件误差分别报告。实时工具另记预览分辨率、冷启动和连续拖动的耗时。按已测的路径和分辨率给结论。
2. 回归界面读数、拖动中的预览、松手提交、撤销和工程重开。输出检查中性、典型设置、整组留出组合和相关边界。更新已发布的调色算法时，沿用节点版本机制，验证旧节点按保存版本渲染以及升级后的表现。
3. 将设置与样本清单、Resolve 实际读数、Topos 版本、报告、代表性对照图和重跑命令归档。记录接受的误差是针对哪些用例和后端，以及哪些情形仍缺样本。原始 TIFF 可单独归档，仓库保留定位样本的文件名、校验值和必要的小型回归数据。

报告中每个用例可用一行记录。

| 用例 | 工具与模式 | 手势／实际读数 | 素材、帧或 ROI | 拟合／留出 | 旧版 → 新版误差 | 实际后端 | 差异与结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `pivot-050` | 一级调色 | Contrast 1.11、Pivot 0.500 | Rec.709 独立色块 | 参数整组留出 | RGB RMSE 15.48 → 0.23 码值 | 原生 Metal | 另查灰阶 P95 和最差色块 |

## 用现有脚本跑一次一级调色对照

在仓库根目录运行。示例使用 macOS 上的 Resolve Studio 安装路径。其他平台按本机安装随附的脚本 README 设置。确认 Resolve 正在运行，`setup` 会保存当前工程并切换到独立测试工程。

```sh
export PYTHONPATH="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
work=$(mktemp -d /tmp/topos-resolve-compare.XXXXXX)
.venv/bin/python tools/resolve_primary_calibration.py chart "$work"
.venv/bin/python tools/resolve_primary_calibration.py setup "$work"
.venv/bin/python tools/resolve_primary_calibration.py capture "$work" --case neutral
```

在 Resolve 的主色轮把 Lift Y 手动设为 0.10 并读回。随后在同一个终端运行。脚本的 `--edits` 只记录元数据，拍摄前需要在 Resolve 应用这些值。其余档位从中性状态按同样方式采集。

```sh
.venv/bin/python tools/resolve_primary_calibration.py capture "$work" \
  --case lift-y-plus --edits '[["wheels","lift","y",0.10]]'
.venv/bin/python tools/resolve_primary_calibration.py restore "$work"
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/resolve_primary_calibration.py \
  report "$work" --compare-topos --output "$work/comparison.json"
```

完成或中断采集后，若仍在校准工程，使用 `restore` 返回原工程。`report --compare-topos` 用一级调色控件的参数映射和 Topos 的 CPU 路径生成报告。手势、其他工具的参数录入、空间或时间素材，以及实际 GPU 预览与导出需按前面的步骤单独采集。其他工具可以复用图卡和统计口径。

[图卡和一级调色采集脚本](../../tools/resolve_primary_calibration.py) · [初级调色实测](resolve_primary_calibration.md) · [曲线与柔化裁切实测](resolve_curve_calibration.md) · [色彩扭曲器实测](resolve_warper_calibration.md)

## 对照项目色彩空间与 LUT

在 Topos 视频模式打开「文件 → 工程色彩空间」。时间线空间定义 SDR 视频在调色节点中的 RGB 信号码值。输出空间定义节点链之后的交付变换。两个空间相同时，交付环节保留时间线码值。新项目默认两项都是 Rec.709 Gamma 2.4。BT.709 OETF 有自己的曲线和选项。RGB 在解码后和调色时使用归一化 Full 范围，容器的 Video/Full 电平在编解码边界处理。照片输出沿用交付偏好的 ICC 选择，默认 sRGB。

Resolve 的 DaVinci YRGB 是手动流程。对比相同时间线和输出空间时，两边可直接选择同一预设。对比不同输出空间时，在 Resolve 用明确的末端色彩空间转换，并配平色调映射和色域映射。节点 LUT 应放在转换的同一侧，输入空间、LUT 强度、插值和文件电平也需要对应。Screen 和显示器 ICC 只参与观看时的显示。以导出文件中的 RGB 码值验收。[Blackmagic Design Resolve 参考手册](https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_21.1_Reference_Manual.pdf?_v=1788850810000)

Resolve Studio 20.2.1.6 与 Topos 实际交付路径使用 Rec.709 Gamma 2.4、Full 16 位 RGB TIFF、单节点 100% LUT 和三线性插值。两套素材分别是 1920×1080 的 RGB TIFF 图卡和同图卡生成的未压缩 10 位 RGB QuickTime。数值以 16 位码值计。

| 素材和 LUT | 独立 RGB RMSE / 最大误差 | 灰阶 RMSE / 最大误差 |
| --- | ---: | ---: |
| TIFF，中性 | 0 / 0 | 0 / 0 |
| TIFF，33³ 平滑跨通道 LUT | 0.47 / 1 | 2.16 / 6 |
| TIFF，3³ 强跨通道 LUT | 0.32 / 1 | 29.06 / 95 |
| 10 位 RGB 视频，中性 | 0 / 0 | 0.03 / 1 |
| 10 位 RGB 视频，33³ LUT | 2.02 / 5 | 2.13 / 5 |

每行分别测 4,096 个独立彩色样本和 1,024 个灰阶样本。另将 Topos 的 33³ LUT TIFF 改为 sRGB 输出，实测与独立 Gamma 2.4 解码、sRGB 编码公式相差最多一个 16 位码值。3³ 用例用于放大插值差异，其灰阶峰值约为 1.48 个 10 位码值。当前一致性覆盖实测 SDR 信号、Full 电平与单位输入域的 3D Cube。HDR、相机 Log、带 1D shaper 的 LUT、其他插值或有损视频编码需要各自采样。

Gamma 2.4 的 RGB 像素使用纯幂函数。标准 QuickTime 文件实测达芬奇写入 `colr` 1-2-1，表示 BT.709 基色、未指定传输、BT.709 矩阵。Topos 标准视频出口采用同样的传输标记。TOPOS 原生码流需要可用传输标记，使用其 BT.709 兼容标签。

## SDR 工程输出实测（2026-11-25）

Resolve Studio 20.2.1.6 在独立工程使用 DaVinci YRGB Color Managed、Custom，关闭自动色彩管理、输出色调映射、色域映射和色彩空间感知调色。输入与时间线均为 Rec.709 Gamma 2.4。Topos 使用同空间的手动输入解释、实际时间线交付入口以及 OpenGL 渲染后端。环境为 macOS 15.3.1、AMD Radeon RX 6800、3840×2160 桌面。源图和交付均为 1920×1080 Full 16 位 RGB TIFF。统计单位为 16 位码值。

| 工程输出 | 独立 RGB RMSE / P95 / 最大差 | 灰阶 RMSE / P95 / 最大差 | 整张图卡最大差 |
| --- | ---: | ---: | ---: |
| Rec.709 Gamma 2.4 | 0 / 0 / 0 | 0 / 0 / 0 | 0 |
| Rec.709 BT.709 OETF | 0 / 0 / 0 | 0 / 0 / 0 | 0 |
| sRGB | 0 / 0 / 0 | 0.054 / 0 / 1 | 1 |
| Display P3 | 0.027 / 0 / 1 | 0.054 / 0 / 1 | 1 |
| P3-D65 Gamma 2.4 | 0.024 / 0 / 1 | 0 / 0 / 0 | 1 |
| DCI-P3 Gamma 2.6 | 0.034 / 0 / 1 | 0.036 / 0 / 1 | 1 |
| Rec.2020 Gamma 2.4 | 0.041 / 0 / 1 | 0 / 0 / 0 | 1 |
| Rec.2020 BT.2020 OETF | 0.049 / 0 / 1 | 0 / 0 / 0 | 1 |

整张图卡有 41,057 个样本，包含 33³ 网格、4,096 个留出 RGB 样本和 1,024 点灰阶。五个宽色域输出都已从实际 TIFF 读回三个 16 位 RGB 通道及 ICC，只开放此格式。Rec.2020 的精确基色矩阵修正了单个接近 0.018 的色块跳到错误曲线段的问题。BT.2020 OETF 的 ICC 使用采样逆函数，按 ICC 单调表要求处理 10 位标准舍入常量造成的短暂重叠。Resolve 的 P3-D65 合并预设使用 Gamma 2.6，对照 P3-D65 Gamma 2.4 和 Display P3 时使用独立 Gamma 2.4 和 sRGB。影院 P3 对拍实测保留绝对 XYZ，0.5 中性 Rec.709 输入转换为约 (0.546, 0.519, 0.557)。

把五种 Topos TIFF 再导入两套软件，显式指定输入解释并输出回 Rec.709 Gamma 2.4。两端的全图 P95 均为 0，留出 RGB 最大差为 1–2，灰阶最大差为 0–1。全图最大差分别为 Display P3 4、P3-D65 Gamma 2.4 20、DCI-P3 Gamma 2.6 19、Rec.2020 Gamma 2.4 62、Rec.2020 BT.2020 OETF 65，集中在色域边界的极低通道码值。此用例记录两套软件的逆向差异，报告另记录回算与原图的差异。

本轮另复跑了上节的 Rec.709 中性、3³ LUT 和 33³ LUT。新一轮中性逐码一致。两个 LUT 的独立 RGB 最大差均为 1，灰阶最大差分别为 95 和 6。跨空间测量使用无调色图卡，宽色域工作空间的工具响应、素材级自动输入解释、容器、跨平台编码及设备端数据仍待验证。Scopes 取样时间线工作信号。软件测试读回了 Edit 和 Composite 的 OpenGL 转换结果，设备端验收仍需记录 Resolve 查看器的显示配置，并用探头测量 ICC／EDR、DeckLink 或影院投影的亮度与色度。

```sh
export PYTHONPATH="$PWD:/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
work=$(mktemp -d /tmp/topos-rcm.XXXXXX)
QT_QPA_PLATFORM=offscreen .venv/bin/python -m tools.resolve_video_color_comparison "$work"
```

脚本在隔离工程导出后切回原工程，报告保存在 `$work/comparison.json`，随附两端 TIFF、Resolve 参数、Topos 后端和 ICC 哈希。首轮 Rec.709 基线和本轮实测的临时目录分别为 `/tmp/topos-video-color-upgrade.LZNWlp` 和 `/tmp/topos-rcm-parity.5vbU7J`。

## SDR 桌面查看器实测（2026-11-25）

在视频工作区打开 Topos 的「首选项 → 色彩与处理」，将「视频查看器使用 macOS 显示器 ICC」和 Resolve 的 “Use Mac Display Color Profile for Viewers” 设为同一状态。工程输入、时间线、输出空间和素材范围也须对应。合成查看器使用「工程输出」，查看器曝光、增益和伽马使用默认值。 [Resolve 20 调色指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf)

测试沿用上一节的 Resolve Studio 20.2.1.6 和 RCM Custom 设置。图卡改用 32 个大色块，含灰阶、肤色和高饱和样本。两应用在同一台 LECOO M3221PL 上取窗口中每个色块中央的像素中位数，捕获的屏幕信号是 8 位 RGB，系统 ICC 为 `Topos Auto Calibration`。两端查看器 ICC 均关闭。Resolve 每个输出在独立测试工程预设后再打开图卡，保证新设定被查看器实际使用。Topos 采集了剪辑时间线 CPU 预览到查看器、调色 AsyncRenderer 到查看器、合成线性浮点回读到查看器三条路径，另测了调色时间线 QImage 呈现。

| 工程输出空间 | 剪辑时间线最大差 | 调色最大差 | 合成最大差 |
| --- | ---: | ---: | ---: |
| Rec.709 Gamma 2.4 | 0 | 0 | 0 |
| Rec.709 (Scene) | 1 | 1 | 1 |
| sRGB | 1 | 1 | 1 |
| Display P3 | 1 | 1 | 1 |
| P3-D65 Gamma 2.4 | 1 | 0 | 0 |
| DCI-P3 Gamma 2.6 | 1 | 1 | 1 |
| Rec.2020 Gamma 2.4 | 1 | 1 | 0 |
| Rec.2020 BT.2020 OETF | 1 | 1 | 1 |

开启 Topos 的 ICC 后，逐色块与 CoreGraphics 将对应的工程输出 ICC 转换到该显示器 ICC 的结果比较。八种输出的绝大多数色块相差 0 至 1 个码值，最大差为 2 个码值，出现在剪辑 CPU 回退的两种视频 OETF 输出。128 灰阶从 ICC 关闭时的 RGB (128, 128, 128) 变为约 (118, 117, 119)，与该设备的 ColorSync 参考一致。OpenGL LUT 在当前显示器上用 4,096 个随机 RGB 和 1,024 个灰阶覆盖额外的色域边界。Rec.2020 Gamma 2.4 的 P95 最大通道误差为 0.089，最差为 4.63 个 8 位码值。该误差来自查看器查表近似，度量点在显示设备编码域。 [Core Graphics 色彩匹配](https://developer.apple.com/documentation/colorsync)

同机屏幕截图和统计存于 `/tmp/topos-desktop-sdr-7211685a`，`topos-gpu-viewer.json` 包含主调色和其余两个查看器的采样结果。开启 ICC 的比较以 ColorSync 为参考。Resolve 开启 ICC 的同屏对拍、多屏切换、亮度及色度探头和 HDR／EDR、DeckLink 设备测量待验收。

## ACES 预览与交付实测（2026-11-25）

在 Topos 的「文件 → 工程色彩空间」选 ACEScct 时间线、ACES 版本和默认输入。自动输入按 OpenEXR 色度选择 AP0、AP1 或线性 Rec.709，无色度标记的 EXR 按 AP0 解释。无专用设置的普通 SDR 视频按 Rec.709 Gamma 2.4 解释，普通静帧按 sRGB 解释。配平 Resolve 的输入变换和 Data Levels。测试的 AP0 图卡用 Resolve 的 No Input Transform 和 Full 范围，AP1 图卡用 ACEScg CSC。ACES 1.3 的参考色域压缩在两端按同一状态设置，Topos 把它放在输入变换后、调色前。 [ACES Reference Gamut Compression](https://docs.acescentral.com/rgc/specification/) · [Resolve 20 调色指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf)

同一台 Mac 使用 Resolve Studio 20.2.1.6 和 OpenColorIO 2.5.2。固定 OCIO Studio 配置为 `studio-config-v2.2.0_aces-v1.3_ocio-v2.4` 和 `studio-config-v4.0.0_aces-v2.0_ocio-v2.5`。Resolve 选 Rec.709 BT.1886，Topos 选 Rec.709 Gamma 2.4 输出。关闭两端查看器 ICC。32 色块在 Topos 剪辑时间线、调色、合成的桌面输出与 Resolve Color 查看器样本最大差均为 1 个 8 位码值。Resolve 样本来自 JPEG 窗口捕获，存在截图量化误差。Topos 调色用 AsyncRenderer 和 OpenGL，合成查看器使用场景线性浮点源，剪辑查看器使用 CPU 时间线预览。另以实际 OpenGL 输出测试了 ACES 输入、参考色域压缩和两版 ODT。

实际交付在 ACEScct 下比较相同图卡的 RGB 16 位 TIFF。共 42,049 个色块，含 33³ 网格、4,096 个留出色块、灰阶、负值和高光。误差为每个色块最大通道差。

| 版本及压缩状态 | 留出色块 P95 | 留出色块最大差 | 完整 RGB 网格最大差 |
| --- | ---: | ---: | ---: |
| 1.3，关闭 | 1 | 28 | 122 |
| 1.3，开启 | 1 | 29 | 122 |
| 2.0 | 38 | 391 | 453 |

另将相同 AP0、AP1 图卡分别送入 Resolve 和 Topos，交付 32 位 AP0 EXR，覆盖有符号 RGB 与超过 1.0 的高光。两端样本的最大绝对差为 `4.96e-5`，出现在高光。Topos 的 AP1 母版也按 AP1 色度回读，32 位像素和源向量的最大绝对差为 `3.06e-5`。16 位半浮点、32 位浮点、关联 Alpha、负值、超白、AP0／AP1 色度及图像序列回导已做自动回归。母版交换使用带色度属性的 PIZ OpenEXR。查看器继续使用所选 ACES 版本的 Rec.709 ODT。

临时证据保存在 `/tmp/topos-aces-resolve.PbzLzH`。其中 `topos-actual-delivery.json`、`topos-actual-delivery-1.3-no-rgc.json`、`topos-aces-viewer.json` 和 `viewer-measured.json` 记录两端像素。回归入口为 `tests/edit/test_preview_output_signal.py` 和 `tests/deliver/test_aces_exr_sequence.py`。逐素材变换、逆 Rec.709 ODT 输入、相机 IDT、ICC 开启对拍、影院／HDR 目标、外接参考监看和调色工具响应的对拍为后续验证范围。

## HDR 桌面预览与交付实测（2026-11-25）

使用 DaVinci Resolve Studio 20.2.1.6。Resolve 选择 DaVinci YRGB Color Managed、Custom，输入和输出 DRT／色彩空间转换设为无色调映射、无色域映射。工作域按用例选择 DaVinci WG／Intermediate 或 Rec.2100 ST2084。Topos 选择对应工作域和输出。SDR 参考白为 100 nit，HLG 参考显示峰值为 1000 nit、系统伽马为 1.2。RGB TIFF 图卡按 Full 电平解释，成片使用视频电平。

在 32 个灰阶、近黑、高光和有色平坦色块上，解码成片并读取各色块中心的原始 10 位 Y′CbCr。以下四组的最大差为零。两端均回读出 BT.2020 基色与 NCL 矩阵、对应的 PQ／HLG 传递函数和有限范围。

| 输入 | 交付 | 测得最大通道差 |
| --- | --- | ---: |
| PQ | PQ ProRes 422 HQ MOV | 0 个 10 位码值 |
| PQ | PQ HEVC Main 10 MP4 | 0 个 10 位码值 |
| PQ | HLG HEVC Main 10 MOV | 0 个 10 位码值 |
| Rec.709 Gamma 2.4 | PQ HEVC Main 10 MP4 | 0 个 10 位码值 |

Topos PQ HEVC 成片独立查验了 `hvc1`、10 位 4:2:0 以及 ST 2086。使用 P3-D65 和 2000 nit 的测试设置时，码流回读值与设置一致，黑位为 0.005 nit。MaxCLL／MaxFALL 报告为未知值。HLG 成片携带 HLG CICP 标签。ProRes 422 和 422 HQ 的编码档位、矩阵和传递函数另有真实文件回读测试。

同机屏幕截图采集了剪辑时间线、调色和合成界面。在两边关闭查看器 ICC 的条件下，PQ 码值、HLG 码值以及 PQ 工程的 Rec.709 SDR 参考三组各测 32 个色块，三个查看器的每组最大差均为 1 个 8 位码值。SDR 参考组对应 Resolve 输出改为 Rec.709 Gamma 2.4。剪辑桌面采集使用 CPU 时间线画面和实际 Program Viewer，另通过真实 OpenGL 读回核对 GPU 转换。合成桌面采集送入线性 RGB，素材进入合成图的亮度转换由单独回归覆盖。

使用 DaVinci WG／Intermediate 时间线和 Resolve 16 位 RGB TIFF 对照了 41,057 个样本，包括 33³ 色立方、4,096 个独立颜色和 1,024 个灰阶。六种具有 HDR 输出的组合中，模型与 Resolve 的 ΔE_ITP P95 最高为 0.010，最大值约为 0.022。16 位码值极差主要出现在跨工作域往返的极暗通道，PQ 往返最差点约 747 个 16 位码值。码值极差应和亮度域误差一起阅读。

本机显示器是 LECOO M3221PL，macOS 当前及潜在 EDR 余量均为 1.0。截图对拍覆盖桌面像素。原生 EDR、DeckLink 10 位参考输出、实体显示峰值和色度、HDR 示波器、Resolve 自动 DaVinci DRT、动态 HDR 元数据以及 ACES HDR ODT 是独立的待验项目。4K 长时播放和不同硬件平台的交付耗时尚未测量。

本次采集证据位于 `/tmp/topos-resolve-hdr.NJxmxv`，包括 `hdr-measurement.json`、`desktop-measured.json` 和 `topos-delivery.json`。回归入口为 `tests/color/test_hdr_project_signal.py`、`tests/edit/test_preview_output_signal.py` 和 `tests/deliver/test_hdr_video_delivery.py`。资料使用 [Blackmagic Design 的 DWG／Intermediate 规范](https://documents.blackmagicdesign.com/InformationNotes/DaVinci_Resolve_17_Wide_Gamut_Intermediate.pdf?_v=1628751610000) 和 [ITU-R BT.2100](https://www.itu.int/rec/R-REC-BT.2100/en)。
