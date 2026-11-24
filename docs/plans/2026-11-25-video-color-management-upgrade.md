# Topos Studio 视频色彩管理升级计划

更新于 2026-11-25，已开始实施。视频模式优先，新工程继续默认 **Rec.709 Gamma 2.4**，图片模式默认 **sRGB**。视频工程加入影视制作常用的 P3、Rec.2020、PQ、HLG 和 ACES 预设，并允许按需独立选择色域与伽马。每个开放的预设都要与 DaVinci Resolve 对拍，分别验证素材进时间线的颜色、导出再导入的颜色，以及两个查看器的观感。首批采集使用标准色彩空间的图卡和素材，摄像机厂牌专用色域、Log 与自动 RAW 色彩科学继续由现有素材流程负责。

首轮已复测 Rec.709 中性和同 LUT，并对拍 Rec.709 时间线到 SDR 输出的实际 TIFF 交付。宽色域开放 Display P3、P3-D65 Gamma 2.4、DCI-P3 Gamma 2.6、Rec.2020 Gamma 2.4 及 Rec.2020 BT.2020 OETF 的 RGB 16 位 ICC TIFF。这些文件已按指定输入解释再次导入对拍。素材级受管输入、宽色域工作域的工具响应、硬件监看、HDR 与 ACES 产品路径仍待实施或验收。 [SDR 工程输出实测](../validation/resolve_comparison_workflow.md#sdr-工程输出实测2026-11-25)

第二轮已接入 SDR 桌面监看。剪辑的时间线查看器、调色和合成可使用同一个 macOS 显示器 ICC 开关。关闭 ICC 时，三种查看器的 Rec.709 Gamma 2.4 在同屏图卡上与 Resolve 逐码一致，其余七种输出最大差 1 个 8 位码值。ICC 开启已与本机 ColorSync 参考核对。Resolve 开启 ICC、双屏、探头和外接设备仍待验收。 [SDR 桌面查看器实测](../validation/resolve_comparison_workflow.md#sdr-桌面查看器实测2026-11-25)

第三轮已接入 ACES 1.3 和 2.0。视频工程可选 ACEScct、ACEScc、ACES2065-1 或 ACEScg 时间线，按 EXR 色度信息或工程默认变换解释输入。剪辑时间线、调色和合成使用同版 ACES Rec.709 查看器变换。交付支持 Rec.709 成片及 AP0／AP1 浮点 OpenEXR 序列。Resolve 对拍覆盖输入、桌面查看器、16 位 TIFF 和 32 位 EXR。逐素材 IDT、相机原生 IDT、影院及 HDR ACES 输出仍待验收。 [ACES 实测](../validation/resolve_comparison_workflow.md#aces-预览与交付实测2026-11-25)

第四轮开放 DaVinci Wide Gamut／Intermediate 和 Rec.2100 PQ／HLG 时间线，接通 HDR 素材标签、浮点输入和输出转换。桌面可切换信号码值或 Rec.709 参考，交付支持 HEVC Main 10 和 ProRes 422／HQ。已对拍 Resolve 的 RCM 无色调及色域映射设置。原生 EDR、外接 HDR 参考监视器、HDR 示波器亮度和 Resolve DaVinci DRT 仍待验收。 [HDR 实测](../validation/resolve_comparison_workflow.md#hdr-桌面预览与交付实测2026-11-25)

## 1. 现有基线和实施入口

| 位置 | 已有能力 | 升级任务 |
| --- | --- | --- |
| 工程设置 | 偏好与工程共用 `ColorSpaceControls`。工程时间线和输出选择可保存、撤销。工程覆盖全局默认。 | 增加输入解释、组合预设、单独色域与伽马、不同处理方式的有效选项。沿用工程命令及不可变快照。 [偏好控件](../../src/features/color/preferences.py) · [工程命令](../../src/features/edit/core/commands/project_color_spaces.py) |
| 输入与调色 | 普通 SDR 视频的手动路径把解码后的 RGB 信号按时间线空间解释。已有对应的 709 一级调色校准。 | 加入可指定的素材输入变换，同一素材的原片、代理、优化媒体和离线缓存使用同一解释。校准继续按节点保存，额外工作域的工具响应另行采集。 [视频信号](../../src/shared/color/project_video_signal.py) · [一级调色实测](../validation/resolve_primary_calibration.md) |
| 色彩变换 | 描述符、内置 CPU 变换和 OCIO 提供可复用入口。基础 OCIO 配置包含部分 HDR 与 ACEScg 定义。 | 盘点每个现有变换的矩阵、白点、曲线和数值单位，补足 AP0、ACEScc、ACEScct、Gamma 2.6 及版本化 ACES 输出变换。显式校验 OCIO 处理器能力。 [描述符](../../src/shared/color/color_space_descriptor.py) · [变换提供器](../../src/shared/color/color_transform_provider.py) · [OCIO 配置](../../ocio/minimal.ocio) |
| 预览与合成 | 剪辑和交付已经处理部分时间线信号与合成线性帧之间的转换。Color 有原生 Metal 查看器。 | 让剪辑、调色、合成、Scopes 与交付消费同一配置快照。明确纹理在哪个域、各分支在哪里转换，补齐 GL 与 Metal 路径。 [时间线预览](../../src/features/edit/core/timeline_preview_renderer.py) · [时间线素材桥接](../../src/features/edit/core/timeline_clip_frame_provider.py) · [合成查看器](../../src/features/composite/ui/composite_viewport.py) · [Metal 调色预览](../../src/features/color/core/metal_color_preview.py) |
| 导出 | Rec.709 模式已有实际交付对拍。同一 33³ LUT 下，Full 16 位 TIFF 的验证色块最大差 1 个码值，灰阶最大差 6 个码值。 | 按预设扩展像素、容器标签、编码器以及再次导入的一致性检查，把现有结果作为回归基线。 [对拍记录](../validation/resolve_comparison_workflow.md#对照项目色彩空间与-lut) · [实际交付](../../src/features/edit/core/timeline_export.py) |

## 2. 用户设置

| 入口 | 默认显示 | 可展开的选项与行为 |
| --- | --- | --- |
| 偏好设置 → 工程色彩默认值 | 时间线空间、视频输出空间，均为 Rec.709 Gamma 2.4 | 供新视频工程继承。切换偏好后，已保存工程按自己的设置打开。 |
| 文件 → 工程色彩空间 | 时间线空间、视频输出空间，使用工程内的值。 | 在高级区域选处理方式、默认输入解释、色调和色域映射。HDR 仅显示当前信号所需的参考白、目标峰值与 HLG 系统伽马等设置。ACES 仅显示版本、输入变换、参考色域压缩和输出变换所需的设置。 |
| 合并显示色域与伽马 | 复选框默认勾选，每行一个预设。 | 取消勾选后，输入、时间线和输出的适用行分别显示「色域」和「伽马」。切换显示模式保留当前参数，实现无码值变化的往返。重新勾选时，已登记的组合显示预设名，其他已支持组合显示「自定义」和当前两项值。 |
| 媒体池 → 素材色彩空间 | 使用工程输入规则。允许看见最终识别的色域、伽马和数据电平。 | 单素材、批量覆盖及恢复自动。优先级为用户指定、可信素材标记、工程回退。选「按时间线解释」时沿用手动输入行为。工程级默认输入与单素材覆盖均支持撤销和保存。 |
| 查看器 → 监看 | 跟随项目输出的观看目标，桌面显示适配按本机设置。 | 可指定标准参考监看或使用系统显示配置。桌面 ICC、HDR/EDR 能力和外接参考监看按设备保存。交付文件的像素与标签只取工程和交付设置。 |

优先把组合预设放在正常菜单。独立选择时，根据用途显示已验证的搭配。例如 AP1 可配线性、ACEScc 或 ACEScct，AP0 的交换预设为线性。PQ 和 HLG 参数跟随相应信号，缺少必要条件时在确认前解释原因。输入、时间线和输出三种用途分别登记能力。某个色彩空间能作为调色工作域时，交付列表显示该空间通过验收的文件类型。图片调色沿用现有简洁入口。

## 3. 预设和交付格式

| 批次 | 组合预设 | 用途 | 最小交付或验证载体 |
| --- | --- | --- | --- |
| SDR | Rec.709 Gamma 2.4、Rec.709 BT.709 OETF、sRGB | SDR 交付、视频信号、网页及图片素材。保留不同暗部曲线。 | RGB 16 位 TIFF。Rec.709 另测 ProRes 或兼容的 SDR 视频封装。 |
| SDR | Display P3（P3-D65 ＋ sRGB）、P3-D65 Gamma 2.4 | 带 ICC 的图像与宽色域 SDR 屏幕交付。区分两种传递函数。 | Display P3 TIFF 检查 ICC。P3-D65 Gamma 2.4 用可明确约定输入的高位深 RGB 母版复验。 |
| SDR | DCI-P3 Gamma 2.6 | 影院 RGB 母版、校准投影参考。保存影院白点，与 P3-D65 各有独立预设。 | 高位深 RGB TIFF 序列对拍。DCP 的 XYZ 编码和封装有独立规范。 |
| SDR | Rec.2020 Gamma 2.4、Rec.2020 SDR（BT.2020 传递函数） | 广色域 SDR 母版与互换。记录实际采用的 BT.2020 传递标记和位深。 | RGB 16 位 TIFF、经位深与信号标签验证的 10 位视频。 |
| HDR | Rec.2100 PQ、Rec.2100 HLG（均用 BT.2020 基色） | HDR 播出、交付及监看。PQ 按绝对亮度取样，HLG 按相对场景信号与确定的显示条件求值。 | 10 位或以上视频。PQ 核对 HDR10 静态元数据，HLG 核对信号与参考显示条件。 |
| ACES | ACES2065-1（AP0 线性）、ACEScg（AP1 线性） | 交换或归档、VFX 合成，支持符合相应像素精度的素材输入与母版输出。 | OpenEXR。检查 AP0／AP1 色度信息、通道、位深、负值和高光。 |
| ACES | ACEScct（AP1）、ACEScc（AP1） | ACEScct 主推用于调色，ACEScc 用于已有项目和工作域交换。 | 在线性 AP0 或 AP1 二次读回验证编码响应，另通过所选 ACES 输出变换验证最终交付。 |

P3-D65 与影院 P3 共享相应基色时仍要验证白点处理。Gamma 2.4、Gamma 2.6 与线性作为独立传递函数记录。Rec.709 的 BT.709 OETF 与 Gamma 2.4 分别回归。影院验收采用校准投影条件，P3 RGB 母版和 DCP XYZ 按各自信号检查。 [Resolve 20 调色指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf?_v=1757574010000) · [SMPTE 影院参考投影环境](https://pub.smpte.org/latest/rp431-2/rp0431-2-2011.pdf) · [DCI 系统规范](https://dcss.dcimovies.com/latest/dcss.html)

Rec.2020 的基色、传递函数、Y′CbCr 矩阵和范围在数据模型中分别记录。PQ 与 HLG 使用不同的参考亮度和处理约定。每个 SDR 或 HDR 视频格式分别验证容器和码流标记。 [ITU-R BT.2020](https://www.itu.int/rec/R-REC-BT.2020/en) · [ITU-R BT.2100](https://www.itu.int/rec/R-REC-BT.2100-3-202502-I/en) · [ITU-T H.273](https://www.itu.int/rec/T-REC-H.273-202407-I/en)

ACES 的 AP0、AP1 决定 RGB 基色，ACEScc、ACEScct 决定工作信号编码。ACES 输出变换还决定显示渲染，包含色调和颜色映射。锁定 Resolve 实测版本所用 ACES 版本、OCIO 配置版本和输出变换标识，再开放相应目标。ACES 1.x 和 ACES 2.0 各有参考样本与版本记录。 [ACES 编码](https://docs.acescentral.com/encodings/overview/) · [ACES 输出变换](https://docs.acescentral.com/system-components/output-transforms/) · [OCIO ACES 配置与 Display/View](https://opencolorio.readthedocs.io/en/v2.4.2/configurations/aces_cg.html)

## 4. 处理顺序与兼容

```text
素材解码 → 范围展开与 Y′CbCr 转 RGB → 按输入解释进工作空间
    → 现有调色、LUT 与合成 → 工作图像
        ├─ 交付目标变换 → 量化与范围打包 → 写入容器标签
        └─ 监看目标变换 → 本机显示适配或外接输出 → 查看器或参考监视器
```

- **处理方式。** 保留现有兼容路径，普通视频可按时间线解释，适合同设置复现现有工程。新增受管路径，从指定的输入解释转换到时间线工作空间。匹配工程输出和监看目标时两支共享同一个显示渲染变换，然后分别完成文件打包和设备适配。画幅调整、叠加元素、已有 LUT 和合成的颜色位置在 M0 画出并冻结。已有工程按保存语义加载。
- **描述与白点。** 预设用稳定标识关联基色、白点、传递函数、场景／显示参考域、必要的亮度参数与支持用途。UI 显示名可本地化。矩阵运算在线性光中进行，D65、影院白点与 ACES 白点之间的色适应方法写入变换规格，并按 Resolve 参考分别核实。标准参考优先用公开公式或经验证的 OCIO 处理器，缺失处理器的预设反馈不可用。
- **HDR 量纲。** M1 确定全链路的参考白和单位，再接 PQ、HLG 与 EDR。现有 CPU PQ 解码使用绝对亮度，Metal 部分处理使用相对白值。跨路径先明确单位换算，配平 PQ 峰值、HLG 系统伽马和参考白。浮点缓存保留所处域允许的负值和超白，量化与范围打包在交付端。合成在线性域按 Alpha 约定运算。 [HDR 数学契约](../hdr_math_contract.md) · [Metal 输入输出](../../src/features/color/core/metal_color_io.py)
- **GPU 与合成。** 先对拍合成片段从 CPU 读回及 GPU 直接传递两条路径。将工作域和 Alpha 语义通过帧描述传递，把进入时间线的转换放在片段调色前。对预乘图像应用非线性变换时按约定解预乘、转换再预乘，核对半透明边缘。HDR 和宽色域的 GPU 变换使用解析曲线或能覆盖工作值域的 OCIO 处理器，按纹理格式验证高光并记录开销。检查 4K 播放和缩略图、Scopes 的同域采样。 [GPU 时间线预览](../../src/features/edit/core/edit_gpu_preview_renderer.py) · [GPU 合成帧描述](../../src/shared/gpu/composite_gpu_frame_handle.py)
- **项目与快照。** 通过现有工程命令管理版本化配置。打开、另存、拷贝工程、撤销重做及批量交片要保留有效参数、输入覆盖、未知字段和旧空间的显示结果。预览纹理、Scopes、代理与渲染缓存按输入描述、项目修订、工作空间及适用输出变换失效。GPU 的变换、纹理和缓存管理沿用渲染线程。 [工程保存](../../src/features/edit/core/edit_project_io.py) · [色彩渲染架构](../architecture/color-rendering.md)
- **信号与标签。** 保留浮点 RGB 与 Alpha 的语义，RGB 到 Y′CbCr 时明确矩阵、色度位置和位深，Full／Video 只在信号边界转换一次。按 TIFF、OpenEXR、MOV、MP4 载体验证标签、ICC、EXR 色度、容器 `colr` 和码流标记。纯幂 Gamma 2.4 延续当前实测的 QuickTime 标记，Gamma 2.6 与 ACES 等按格式支持的标签或交付说明验证。原生快速导出和直接复制须证明像素变换和标签等效，才能用于该预设。 [视频编码](../../src/shared/export/video_encoder.py) · [Topos 原生编码](../../src/shared/export/topos_encoder.py)

## 5. 分阶段任务表

工期为单人有效工作日估算。Resolve 版本、HDR 屏幕、校色仪和影院监看设备在 M0 登记。每一批预设在交付验证完成后即可启用，后续批次使用同一套夹具和报告格式。

| 阶段 | 工期 | 依赖 | 实施任务 | 完成判定 |
| --- | ---: | --- | --- | --- |
| M0 基线与用例 | 2–3 天 | 现有 Rec.709 对比 | 复跑中性与同 LUT。记录实际可用的 Resolve 模式、预设、传递函数、CST 和输出变换。导出图卡、灰阶及负值／超白 EXR，冻结支持矩阵和门槛。 | 现有 Rec.709、旧工程和 sRGB 图片结果有可复跑的参考值。每个目标预设已登记 Resolve 对应项、角色、格式和监看目标。 |
| M1 模型与数学 | 4–6 天 | M0 | 扩展描述符、预设注册、格式匹配、精度与 HDR 亮度契约。补齐常用矩阵、白点、曲线；落实 CPU 参考实现和处理器版本。 | 独立规范向量通过，每个可供 UI 选择的组合有确定的变换。重复保存、撤销和旧配置往返通过。 |
| M2 SDR 配置与交付 | 5–8 天 | M1 | 接入复选框、独立选择、素材输入覆盖，贯通 Rec.709、sRGB、P3、Rec.2020 与剪辑／调色／合成／交付。 | 每个 SDR 预设的输入、输出图卡及同 LUT 对拍过线。实时预览与交付帧在相同观察点一致。旧工程回归通过。 |
| M3 桌面与参考监看 | 4–7 天 | M1、M2 | 接入 Color／Edit／Composite 的直接预览、系统 ICC 和参考监看路径。明确 Scopes 取样点。验证多屏、参考模式及外接设备。 | 文件值与监看互不误用。单次 ICC、双屏切换、探头光学测量和 Scopes 读数各有结果。 |
| M4 HDR | 5–8 天 | M1–M3 | 接入 Rec.2100 PQ／HLG，完成亮度与映射、10 位编解码、HDR 标签、EDR 及 HDR → SDR 监看回退。 | 匹配参考条件的 HDR 输入、交付、再次读回和实际显示过线。HDR → SDR 与原 HDR 交付分开验证。 |
| M5 ACES | 5–8 天 | M1、M2、M3，HDR 目标另依赖 M4 | 固定 ACES 与 OCIO 版本。接 AP0、AP1、ACEScct、ACEScc、EXR 及 ACES 输出变换，验证合成返回与 SDR／影院／HDR 目标。 | 浮点输入和母版交换、版本匹配的 ACES 成片及查看器各自过线。每个可交付目标登记对应的 ODT 和输出格式。 |
| M6 发布回归 | 3–5 天 | 已启用批次 | 测两种产品模式、CPU／OpenGL／Metal、硬件与软件交付、缓存和代理切换、旧档重开。更新测试和交付支持表。 | 每个开放预设存档参考样本、实际工程设置、误差报告和复跑命令。显示实测记录对应硬件与目标。 |

## 6. Resolve 对拍矩阵

Resolve 的工程处理方式必须和参考用例一起记录。DaVinci YRGB 手动模式用于隔离公式和 LUT。受管用例使用固定设置的 RCM Custom。ACES 用例固定版本以及 IDT、工作域和输出变换。实测发现某个 Resolve 映射选项无法等效选择时，单列差异和对应设置，所测组合的「对齐」结论限定在等效映射的范围。 [Blackmagic Design Resolve 20 调色指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf?_v=1757574010000)

| 用例 | 双端配置 | 实际取样与通过条件 |
| --- | --- | --- |
| 同空间中性与同 LUT | 对每个支持输出的预设，使用等效的输入解释、输出、码值范围、LUT 顺序、输入域、强度与插值。 | 先无节点，再加一个 100% LUT。读取无损导出后解码的灰阶、彩色色块及两端裁切点。至少保留现有 Rec.709 的三线性 `.cube` 回归。 |
| 只改输入解释 | 同一源文件、一致的解码和数据电平，输入分别指定为正确空间和另一预设。手动 Resolve 用明确的输入 CST，RCM 用素材输入设置。 | 先核对解码码值，再检查进入同一时间线后的 RGB。Resolve 在隔离用例中用明确的工作域浮点导出或可逆参考变换取得结果，并校验这条采样链的恒等误差。 |
| 只改工作／输出空间 | 首先用 DaVinci YRGB 中的显式 CST 对齐预览与导出变换，固定无映射基线。其次用 RCM Custom 对拍固定的参考白与色调／色域映射。 | 每个开放预设至少覆盖等效样本及一条跨空间路径。SDR 要有 Rec.709 ↔ P3、Rec.709 ↔ Rec.2020、P3 ↔ Rec.2020、影院白点和 P3-D65 的中性灰测试。 |
| SDR 与 HDR 交互 | 固定输入映射与目标峰值。PQ 和 HLG 分别测原域，另测 SDR ↔ PQ、SDR ↔ HLG，以及带高光与饱和色的混合时间线。 | PQ 回算绝对亮度。HLG 在同一个目标峰值、系统伽马和观看条件下比较。检查高光拐点、色域边缘、肤色、近黑与 10 位标签。 |
| ACES 工作域和显示渲染 | 等效输入变换、同一 ACES 版本、明确参考色域压缩状态和目标输出变换。 | 先用 EXR 验证 AP0 ⇄ AP1、AP1 ⇄ ACEScc／ACEScct 的规范向量及暗部接点，再测对应 SDR、影院或 HDR 输出。容器回读校验 LUT 与输出变换的先后顺序。 |
| 最终文件与回读 | 同分辨率、帧号、色度采样、位深与数据电平。代表格式使用实际交付入口。 | 独立解码，再分别导回两套软件。逐项核验基色、传递、矩阵、范围、ICC 或 HDR 元数据。显示截图和播放器截图另列。 |
| 素材与工程回归 | 有／无标签，手动覆盖，原片／代理，嵌套合成，Alpha，跨空间图层，旧工程与图片。 | 工程打开、保存、重开和撤销结果可重复。单帧、实时、后台交付与批量交付按同一配置求值。 |

使用 `33³` RGB 图卡（现有图卡或等效的 HaldCLUT）、1,024 点灰阶、暗部细阶、亮度阶梯、透明色边以及独立肤色、近中性和高饱和样本。HDR／ACES 加入超白及表示范围内的负值。采样留出验证色块及少量实际素材，分别保存原始图、导出帧、分析样本与差异分布。日常复跑沿用现有 Resolve 独立工程与脚本，采集后恢复原工程。 [Resolve 对比工作流](../validation/resolve_comparison_workflow.md) · [现有图卡脚本](../../tools/resolve_primary_calibration.py)

### 量化门槛

| 验收点 | 初始目标，M0 校核后冻结 |
| --- | --- |
| 无损恒等 | 同一批有效 RGB 码值一致。重复通过格式边界时，最多允许 16 位中的 2 个码值误差，配备黑白点和全程灰阶检查。既有 Rec.709 同 LUT 基线作为独立回归。 |
| SDR 跨空间标准变换 | 无损 RGB 在匹配映射的验证样本上，报告 16 位码值 RMSE、P95、最大通道差，另报告共同显示条件下的 ΔE00。以 ΔE00 P95 ≤ 1、最大 ≤ 2 为初始目标，按灰阶、肤色与色域边界分组。 |
| HDR 与 ACES | HDR 在共同显示目标的 ICtCp 域测 ΔE_ITP，初始目标 P95 ≤ 2，另外检查参考白和峰值实测亮度。ACES 浮点数学与独立规范向量先比对，在规定的可逆区间验收往返，再用目标显示域的 SDR／HDR 指标验收成片。曲线拐点、黑位及夹紧边界单列误差。 [ITU-R BT.2124 色差指标](https://www.itu.int/rec/R-REC-BT.2124/en) |
| 后端与压缩 | CPU、OpenGL 和 Metal 的同域浮点读回与参考实现对比，按位深和纹理格式冻结容差。有损编码另记录编解码器配置与自身误差，单独比对标签、峰值、范围和回读。 |
| 发布证据 | 每个公开预设保有输入、输出、适用监看目标、后端及已验格式的结果，状态为「通过」「差异待定位」或「待实测」。未通过的能力在其角色和格式中不开放为已验证选项。 |

## 7. 查看器和参考监看验收

| 场景 | 复跑方法与通过条件 |
| --- | --- |
| 软件信号 | 在相同监看目标、100% 显示或同一缩放方法下选同帧。Topos 取设备适配前的 framebuffer，Resolve 用隔离工程中等效目标变换生成同域参考。核对灰阶、暗部、肤色、饱和边界和高光。Scopes 明确采集时间线或所选输出信号。 |
| macOS 系统 ICC | 两应用在同一显示器，使用同一已校准的系统配置。记录 Resolve 的 Mac Display Color Profile for Viewers 设置。测试开关匹配、窗口移到另一显示器、配置切换和全屏。用不同 ICC 做对照，设备适配应恰好生效一次，切换 ICC 前后的导出文件保持同值。 [Blackmagic Design Resolve 20 调色指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf?_v=1757574010000) |
| HDR／EDR 查看器 | 固定显示器参考模式、亮度、参考白、环境、图卡窗口面积与目标峰值，记录 EDR 当前及潜在余量。分别验证 HDR 屏与 SDR 屏。在 HDR 屏用探头验证参考白、峰值和灰阶；在 SDR 屏对拍同一个 SDR 观看目标。 [Apple EDR 能力检测](https://developer.apple.com/documentation/metal/determining-support-for-edr-values) · [Apple Metal HDR 色彩空间](https://developer.apple.com/documentation/metal/using-color-spaces-to-display-hdr-content) |
| 外接参考监看与影院 | 两应用通过同一输出设备、视频电平和信号给同一台参考监视器或投影机。固定显示目标、监看 LUT 和环境光，用探头比较亮度及色度，记录重复测量误差。影院另测 DCI 白点与 Gamma 2.6。普通桌面可完成软件链路对拍，光学测量状态单独记录。 [外接监看入口](../../src/shared/video/monitor_output.py) |

SDR 可先以同屏探头 ΔE00 P95 ≤ 2、灰阶中高亮度偏差 ≤ 3% 为工程目标。HDR 在同一参考白和峰值测量条件下先以白点亮度偏差 ≤ 5%、ΔE_ITP P95 ≤ 3 为工程目标。近黑单独记录绝对亮度偏差与仪器噪声。M0 用同一软件反复输出测量图卡确定仪器重复性。截图可以检查缩放、裁剪和界面状态，光学指标使用探头读数。

## 8. 每批交付记录

每次验收存档两边的程序版本、工程设置和原始输入描述、对拍帧号、实现提交、处理模式、LUT 及 OCIO 配置的哈希、CPU/GPU 路径和精度、输出格式及读回元数据。监看检测另存设备、ICC、参考模式、目标亮度、EDR 余量、实测数据和仪器。报告同时包含每个阶段的取样值、失败色块、复跑命令和结果归档位置。现有 `test_project_video_signal.py`、`test_color_space_descriptor.py` 和交付矩阵测试作为第一批自动回归入口。 [信号测试](../../tests/color/test_project_video_signal.py) · [描述符测试](../../tests/color/test_color_space_descriptor.py) · [交付矩阵测试](../../tests/color/test_delivery_matrix.py)
