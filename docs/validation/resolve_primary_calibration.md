# Resolve 初级调色校准

校准使用 DaVinci YRGB、Rec.709 基色、Gamma 2.4 和 Full 电平。测试 TIFF 的 RGB 是 Gamma 2.4 编码值。达芬奇读取和导出同一套值，Topos 对照则在进入现有线性 Rec.709 调色阶段前解码，比较前重新编码。采样直接读取 16 位 TIFF，绕开 Screen、系统 ICC 和查看器截图。Log 指初级调色的 Log 模式，素材仍是 Rec.709 Gamma 2.4。

## 在 Topos 启用

在一级调色面板按下 **709**，当前校正节点就会使用实测算法。主色轮和色条共用 Lift、Gamma、Gain，三个模式共用一个 Offset，默认显示为 25。色温使用达芬奇的数值刻度，默认 Pivot 显示为 0.435。校准开关随节点保存，原有未启用校准的工程沿用原算法。节点的普通 LUT 槽可以继续用于素材转换或创意调色。

初级调色阶段在 Gamma 2.4 编码域对实测响应插值，灰阶、补色和混合参数一起影响同一份 3D 响应。切换到线性 RGB 后继续走 Topos 的曲线、LUT 和蒙版。校准配置使用版本化的内置文件，工程中只记录版本及现有参数，不保存图卡路径。OpenGL 和 CPU 导出共用该配置。

## 采集与复跑

使用 Resolve Studio 的本地脚本模块。下列路径为 macOS 默认安装路径，其它平台使用 Resolve 随附脚本 README 给出的路径。`setup` 保存当前工程后切换到独立的校准工程，`restore` 返回原工程和页面。测试工程只用生成的图卡。

```sh
export PYTHONPATH="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
work=$(mktemp -d /tmp/topos-primary.XXXXXX)
.venv/bin/python tools/resolve_primary_calibration.py chart "$work"
.venv/bin/python tools/resolve_primary_calibration.py setup "$work"
.venv/bin/python tools/resolve_primary_calibration.py capture "$work" --case neutral
```

在 Color 页手动输入数值，核对实际显示，再记录每次编辑。每个单项样本从中性节点开始。切换模式时也检查另一模式的残留值，因为两组控件可同时生效。Offset 的达芬奇中性值为 25。Log 的默认低、高范围为 0.333、0.550。本轮保持范围默认值，Lum Mix 保持 100。`--edits` 记录实际输入，脚本只负责渲染与记录。

```sh
# 在 Resolve 主色轮输入 Lift Y=0.10 后执行
.venv/bin/python tools/resolve_primary_calibration.py capture "$work" \
  --case wheel-lift-y-plus --edits '[["wheels","lift","y",0.10]]'
.venv/bin/python tools/resolve_primary_calibration.py restore "$work"

# 验证当前 Topos 的数值输入及线性主调色结果
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/resolve_primary_calibration.py \
  report "$work" --compare-topos --output comparison.json

# 生成 Topos 线性 Rec.709 工作域下可导入的 LUT，Lift 在实测区间内插值
.venv/bin/python tools/resolve_primary_calibration.py bake "$work" \
  --edits '[["wheels","lift","y",0.05]]' --output lift-0.05-linear.cube
```

`bake --case <名称>` 直接复现该组采集值。`bake --edits <JSON>` 用实测单项响应在参数间插值，再合成各个通道的偏移。超出已有采样的参数区间时，先补采。默认导出线性 RGB `.cube`，用于 Topos 的线性 Rec.709 调色阶段。在 LUT 浏览器导入，先用中性节点、100% 强度试用。需要在 Gamma 2.4 编码域应用时，使用 `--domain encoded`。

## 实测结果

Resolve Studio 20.2.1.6 的中性 TIFF 对 41,057 个样本逐值一致。每组图卡包含 `33³` 采样网格、4,096 个独立 RGB 验证色块和 1,024 个灰阶点。单项采集覆盖 Lift/Gamma/Gain 的正负 Y、色条 Lift R、Log 暗部 R／中间调 G／高光 B，以及 Offset R。主色轮和 Log 的同一 Offset 实测输出一致。两组混合参数均未用于拟合。

| 验证组合 | 旧 Topos RGB | 参数插值 RGB | 65³ 线性 LUT RGB | 65³ 线性 LUT 灰阶 |
| --- | ---: | ---: | ---: | ---: |
| Lift Y、Gamma Y、Gain Y、Log 中间调 G | 33.2% | 94.6% | 87.0% | 79.3% |
| Gain Y、色条 Lift R、Log 暗部 R 与高光 B | 30.1% | 93.2% | 85.0% | 85.2% |

百分比是相对未调色图卡的 RMSE 降幅。`report` 同时提供误差的 10 位码值、灰阶统计和最大通道误差。首轮的线性 LUT 在暗部网格较疏，第一组灰阶的 p95 最大通道误差为 68.2 个 10 位码值。

## 原生校准的验证结果

第二轮补采了色温 ±1000、色调 ±20、对比度 0.85/1.15、色彩增强 ±25、阴影 ±25、高光 ±25、饱和度 35/65、色相 37.5/62.5。按实际 Topos 数值控件录入，在 CPU 调色阶段读取解码后的色块，输出经 Gamma 2.4 编码后和 Resolve TIFF 对照。

| 独立参数组合 | 原生校准 RGB RMSE 降幅 | 原生校准灰阶 RMSE 降幅 |
| --- | ---: | ---: |
| Lift Y、Gamma Y、Gain Y、Log 中间调 G | 94.6% | 91.1% |
| Gain Y、色条 Lift R、Log 暗部 R 与高光 B | 93.2% | 95.8% |
| 色温 +500、色调 −8、对比度 1.070 | 88.5% | 87.1% |

八项一级参数在所测幅度的独立 RGB 验证色上下降 92.8%–99.99%。部分未直接采样的 RGB 通道和反向 Log 响应用实测曲线推导，超过已测参数时使用线性外推，调节幅度很大时误差可能增大。阴影、高光有轻微空间响应，当前模型拟合色块中心。自定义 Pivot、Log 范围、Lum Mix 和中间细节仍属于 Topos 的处理范围。默认 Rec.709 Gamma 2.4 之外的色彩条件未纳入本轮验收。

重新生成内置模型时使用这两轮采样完整目录。`measured` 标记区分直接采集和推导的响应。更改已发布的配置应新建版本，保留旧节点的数学行为。

```sh
.venv/bin/python tools/resolve_primary_calibration.py profile "$wheel_session" \
  --additional-session "$primary_session" --output /tmp/primary_calibration_rec709_g24_v1.npz
```

## 色轮拖拽和主亮度拨轮

在 Resolve Studio 20.2.1.6 的独立 Rec.709 Gamma 2.4 图卡工程实测。内盘半径约 80 个逻辑像素。鼠标水平或垂直拖动 100 像素时，控制点约移动 9.4–9.7 像素。Lift 和 Gamma 水平拖动 200 像素时，控制点约移动 20 像素。Topos 使用 0.10 倍相对位移，从原控制点开始计算。

| 拨轮 | Resolve 右拖时的变化（显示单位） | Topos 每逻辑像素 |
| --- | ---: | ---: |
| Lift（色轮和色条） | 0.08 / 120 px | 0.000667 |
| Gamma（色轮和色条） | 0.04 / 120 px | 0.000333 |
| Gain（色轮和色条） | 0.16 / 120 px | 0.001333 |
| Log 暗部、中间调、高光 | 0.16 / 120 px | 0.001333 |
| Offset（色轮、Log） | 约 4.00 / 120 px | 0.033333 |
| Offset（色条） | 8.00 / 60 px | 0.133333 |

709 模式使用按半径归一的双向 RGB 映射。校准参考包括四个主色轮的水平、垂直及斜向拖拽，也包含三只 Log 色轮。向右主要增加蓝，向上主要增加红，绿色补偿近似满足 Rec.709 的亮度权重。以约 0.12 内盘半径为例，Resolve Lift 向右读数约为 `(0.00, −0.01, +0.08)`，Gain 向上读数约为 `(1.22, 0.93, 1.00)`。主亮度拨轮在可表示范围内等量平移各通道。数值编辑、回填和撤销使用同一套位置反算，工程继续保存原 HSB 参数。

三组实拖设置逐组导出了 Full 电平的 16 位 TIFF。下表比较同一归一化控制点位置下，旧映射和新映射通过 Topos 709 v1 调色后，与 Resolve 在 4,096 个独立验证色块上的 RGB RMSE。单位为 10 位码值。

| 验证设置 | 旧映射 | 新映射 |
| --- | ---: | ---: |
| Lift 向右 | 46.66 | 1.83 |
| Gain 向上 | 37.22 | 2.30 |
| Log 暗部、中间调、高光和 Offset 同时向右下 | 47.71 | 17.92 |

测量集中在中性附近，靠近参数边界时受现有 HSB 取值范围限制。旧工程仍用保存的 HSB 数值渲染。709 v1 在渲染时继续用通道中位数分解主亮度，界面的方向映射按 Rec.709 亮度加权。主色轮斜向的实际像素误差未纳入上表。Log 混合采样用匹配的 RGB 数值直接输入时仍有 17.92 个 10 位码值的 RMSE，这部分差异主要在渲染响应。
