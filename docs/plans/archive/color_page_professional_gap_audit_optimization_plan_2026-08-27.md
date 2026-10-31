# 调色页面专业化差距评估与优化计划表

日期：2026-08-27  
评估对象：Topos Color 调色页面、调色引擎、时间线调色栈、Scopes、色彩管理、预览与交付链路  
评估状态：已完成评估，尚未开始本计划中的代码实施  
对标范围：DaVinci Resolve、Baselight、Nuke/专业合成调色工作流中常见的节点、色彩管理、监看、追踪、版本与交付能力

## 1. 结论摘要

当前调色页不是“基础功能不足”的状态。它已经具备相当宽的工具面：基础色轮、曲线、HSL 曲线、混色器、Warper、Qualifier、蒙版、Tracker、LUT、RAW、Scopes、降噪、去模糊、胶片颗粒、插件、预设、脚本和时间线片段入口都已存在，GPU Scopes 也有独立的渲染通道。

与专业调色软件相比，当前主要差距在于“专业闭环”而不是单个滑块数量：

1. 调色数据仍主要以平面 Layer 参数和单个 ColorStack 表达，缺少可组合、可重排、可复用的节点图与多级调色上下文。
2. ColorStackData/Hydrator 与完整 Layer 序列化是两套状态契约，时间线切片调色路径只覆盖核心参数，复杂调色状态没有统一保存、恢复和撤销边界。
3. 色彩空间描述符已经有 HDR/宽色域基础，但偏好页仍只暴露三种传输函数；OCIO 在当前环境不可用，OCIO/内置/GPU 的等价性仍未形成可靠发布门槛。
4. Scopes 目前集中在 RGB Waveform、Vectorscope 和 Split，距离专业监看常用的 RGB Parade、Histogram、CIE、False Color、HDR 标尺、取样器和安全线仍有明显距离。
5. 4K 调色渲染吞吐已经不错，但 Color 页视频解码仍受 CPU 路径限制；重型模糊、磨皮插件和 USM 在播放降级中的收益不足，着色器结构变化仍可能带来明显编译卡顿。
6. 跟踪器的模型、双向跟踪、置信度、失败原因和手动修正基础已经存在，但真实遮挡素材的验收当前仍失败，不能把“输出了矩阵”直接等同于“专业可靠”。
7. 预设、LUT、插件和脚本入口已经有，但还没有形成专业的 Still/版本管理、可选择性复制调色、依赖打包、插件能力声明和批量调色工作流。

产品定位建议：当前更接近“功能丰富的非破坏性调色工具集”，尚未达到“可承担专业片段调色、镜头匹配、HDR 交付和长期工程交换”的完整调色系统。

## 2. 当前能力盘点

| 领域 | 当前已具备 | 成熟度判断 | 主要说明 |
|---|---|---|---|
| 画面预览 | Canvas、缩放/平移、叠加辅助线、对比模式、GPU 纹理预览 | 中 | 基础交互可用，但参考帧/版本/监看上下文仍不完整 |
| 基础调色 | Lift/Gamma/Gain、曝光、色温、Tint、对比度、阴影、高光、饱和度、Vibrance、Mid Detail、锐度、Hue Shift | 中高 | 常用参数覆盖较全，但缺少节点级组织、关键帧和上下文继承 |
| 曲线 | RGB 曲线、HSL 六类曲线 | 中 | 具备核心工具；HSL wrapper 的 changed 回调目前是空实现，信号归属需要收敛 |
| 色彩混合 | RGB/色相族 Mixer、Color Warper | 中 | 工具已在，但缺少专业的节点隔离、预览和批处理语义 |
| 图层/蒙版 | Layer 可见性、Opacity、Blend Mode、蒙版组、绘制/形状等 | 中 | 适合平面合成式调色；不是专业节点图的替代品 |
| Qualifier | HSL 取样和限定参数 | 中 | 基础能力存在，缺少成熟的 matte 预览、清理、收缩扩展和键混合工作流 |
| Tracker | 前后向、多个几何模式、置信度、失败原因、手动修正、序列化 | 中 | 基础架构较完整；真实遮挡验收仍有失败 |
| LUT | .cube Gallery、导入、强度、重置、导出部分调色参数 | 中 | 还缺输入/输出域、Shaper、插值、元数据和依赖管理 |
| RAW | RAW 面板和预览参数入口 | 中低 | 需要按相机/格式建立能力矩阵、质量测试和专业解码选项 |
| 降噪/去模糊/颗粒 | 真实参数面板和视频时域/空域处理管线 | 中 | 有实现，不应重复建设；还缺质量档位、缓存、确定性和交付一致性 |
| Scopes | RGB Waveform、Vectorscope、Split；GPU Scope 通道、代次防旧帧 | 中 | GPU 架构已建立，但 Scope 类型、标尺和硬件验收仍不足 |
| 色彩管理 | ColorSpaceDescriptor、Rec.709/sRGB 分离、PQ/HLG 基础、OCIO provider 接口、YUV metadata | 中低 | 数据模型方向正确，UI 暴露、OCIO 可用性、宽色域闭环和输出验证不足 |
| 调色栈 | ColorSession、ColorStackData、共享素材/片段作用域、Hydrator、Layer.to_dict | 中低 | 有基础骨架，但存在两套状态契约和字段覆盖不一致 |
| 性能 | GPU 渲染、渲染队列 lane、代次保护、4K 性能基准、GPU Scopes | 中高 | 基础渲染吞吐较好；CPU 解码、重型工具播放降级、编译和资源生命周期仍是瓶颈 |
| 工程保存 | .topos 保存、完整 Layer 旧路径、时间线调色栈 | 中 | 能保存很多内容，但不同入口的语义不一致，需统一 schema 和迁移 |
| 扩展 | GLSL/DCTL/AI 插件、插件管理器、Script Panel | 中低 | 有扩展点，但插件参数、能力声明、缓存、版本和交付边界不完整 |

## 3. 评估依据与验证结果

### 3.1 代码和架构依据

| 证据 | 观察 |
|---|---|
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/color/module.py | 调色模块注册了大量 Panel 和部分 Action，但 register_pages 为空，register_actions 与 register_save_handlers 仍由 MainWindow/SaveRouter 承接；模块边界尚未完全闭合 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/project/color_stack.py | ColorStackData 目前主要保存 grading、RGB/HSL curves、mixer、soft clip、LUT、tracking 和片段归属，未表达完整 Layer/节点/蒙版/Qualifier/Warper/插件/RAW/效果状态 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/color/adapters/color_stack_hydrator.py | Hydrator 应用和收集的字段集中在核心调色参数、LUT 与跟踪；它没有覆盖完整 Layer 序列化所支持的所有高级状态 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/project/save_router.py | 当前另有路径通过 Layer.to_dict 完整保存 masks、qualifier、warper、effects、plugins、曲线和 baked 状态；这说明系统同时存在“完整 Layer 保存”和“精简 ColorStack 保存”两种契约 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/color/core/layer.py | Layer 是可变对象，直接承载大量调色和效果字段；适合现有渲染，但还没有节点级语义、作用域继承、参数动画和统一命令事务模型 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/color/preferences.py | Preview/Output 当前只提供 sRGB、Rec.709、Linear 三种 transfer 选择，primaries、range、HDR tone mapping、target peak、HDR metadata 没有完整暴露和保存 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/color/color_space_descriptor.py | 有 HDR10、HLG、Display P3 等描述符，但 DCI-P3 PQ 映射会退化到 BT.2020 PQ，未知/未支持组合会静默回落到线性空间，需要能力声明与显式告警 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/color/ui/scope_panel.py | 生产 Scope 模式只有 waveform、vectorscope、split；GPU presenter 已存在，但没有更丰富的专业 Scope 类型和数据标尺设置 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/color/ui/plugin_panel.py | 插件 color 参数仍显示 Color picker coming soon，占位控件不能承载完整插件参数 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/color/ui/hsl_curves_panel.py | Panel 对 hsl_curves_widget.changed 的 _on_hsl_curve_changed 为空实现；当前逻辑通过其他 controller/直连路径工作，封装接口存在死回调和所有权不清 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/color/core/shader_compiler.py | composition shader 的 cache key 已区分结构参数，但构建仍会内联完整 snippets，插件结构变化会触发整套 program 编译 |
| /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/color_tools_perf_report_2026-08-21.md | 4K 主要基础调色工具接近基线，但播放降级对 blur/磨皮插件基本无效，USM 仍昂贵，Color 页 CPU 解码约 49–54 fps，结构改变时有 100ms 级编译代价 |

### 3.2 当前测试验证

| 验证项 | 结果 | 结论 |
|---|---:|---|
| 调色专项组合测试收集 | 1133 个 | 全量过程未能安全完成 |
| 全量过程 | 中止 | 在 image_engine_history 阶段出现 EQThread: Destroyed while thread '' is still running，随后 Python 进程 Abort；这是线程生命周期发布风险 |
| 可隔离完成的关键测试 | 239 passed | 色彩空间、HDR transfer、Hydrator、Session、GPU Scopes、追踪序列化、ColorStack 和片段切换等基础链路有相当覆盖 |
| 跳过的关键测试 | 11 skipped | OCIO provider/parity 相关用例在当前环境不可执行，不能把 skip 当成专业色彩闭环已验证 |
| 遮挡跟踪验收 | 1 failed, 7 passed | test_occluded_frames_marked_failed 要求遮挡帧至少 70% 标记失败，实际仅 5/20；存在输出假有效或失败检测阈值不可靠的风险 |

## 4. P0：发布阻断与正确性问题

P0 的共同标准：如果不解决，不应把调色页宣称为可靠的专业生产工具，也不应以“功能已存在”替代验收。

| ID | 当前问题 | 影响 | 优化动作 | 验收标准 | 责任模块/优先级 |
|---|---|---|---|---|---|
| P0-COLOR-01 | ImageEngine 相关测试触发 QThread 仍在运行时销毁，进程直接 Abort | 可能在退出、切片切换、渲染失败或测试清理阶段崩溃；破坏发布稳定性 | 建立 Renderer/Tracking/Decode/Scope 的统一 stop、cancel、quit、wait、GL cleanup 生命周期；所有 fixture、窗口关闭和异常路径都必须等待线程结束 | 连续运行 color tests、关闭窗口、快速切片切换、取消渲染不再出现 QThread destroyed；进程无 Abort；每个线程有可观测 shutdown 状态 | AsyncRenderer、ImageEngine、TrackingService、MainWindow；P0 |
| P0-COLOR-02 | 时间线 ColorStackData/Hydrator 只覆盖核心参数，而完整 Layer.to_dict 能保存更多高级状态 | 切片切换、保存重开、共享调色栈、撤销重做可能在 mask、Qualifier、Warper、effects、plugins、RAW、film grain、denoise/deblur、active layer 等状态上不一致 | 选定一个唯一的 ColorState schema；用完整节点/Layer snapshot 作为源，ColorStackData 只做强类型引用或完整嵌套；增加 schema_version、迁移和未知字段策略 | 逐字段 round-trip：核心调色、曲线、HSL、Mixer、LUT、Soft Clip、mask、Qualifier、Warper、effects、plugin、RAW、tracking、active layer、visibility、blend、opacity、baked 状态保存前后完全一致 | ColorStackData、Hydrator、SaveRouter、EditController；P0 |
| P0-COLOR-03 | 两套保存契约并存：legacy/standalone 走完整 Layer 序列化，timeline clip 走精简 immutable ColorStack 更新 | 同一份调色在不同入口得到不同恢复结果；用户无法判断哪些高级操作属于工程资产、片段本地还是当前 session | 统一 SaveRouter、Timeline Command、ColorSession 和 Hydrator 的读写契约；禁止 controller 手动拼接字段；所有变更走同一个 immutable snapshot/command | 新建 standalone image、timeline clip、共享 asset grade 三种场景都用同一套 schema 保存/读取；同一工程在不同入口重开结果一致；升级旧工程有迁移报告 | Project schema、SaveRouter、ColorSession、EditController；P0 |
| P0-COLOR-04 | 色彩管理的 UI 与运行时能力不对齐：代码有 HDR/宽色域描述符，但偏好页只暴露 sRGB、Rec.709、Linear；primaries/range/HDR 参数没有完整闭环 | 用户无法明确建立输入、工作、预览和输出色彩空间；HDR/宽色域可能依赖隐含默认值，出现黑位、白位、色域或高光错误 | 把 source/working/display/output descriptor 变成显式工程状态；偏好页和工程设置支持 primaries、transfer、range、HDR tone map、target peak、paper white、metadata；所有入口读同一 descriptor | Rec.709 limited、sRGB full、BT.2020 PQ、BT.2020 HLG、Display P3、10-bit limited/full 在预览、Scopes、保存、重开和输出中的 descriptor 一致；错误或未知 metadata 必须显示警告 | ColorSpaceDescriptor、preferences、ColorManager、Project schema；P0 |
| P0-COLOR-05 | OCIOColorProvider 在当前环境实测不可用；OCIO config/parity 用例有 11 项 skipped，运行时会回退内置 provider；P3 DCI PQ 还会映射到 BT.2020 PQ 名称 | 专业色彩转换无法证明与内置/GPU/导出一致；相同工程在不同环境可能得到不同颜色；错误 fallback 可能静默发生 | 明确 OCIO 的必需/可选策略；修正 minimal config 和 DCI-P3 映射；建立 OCIO、builtin、GLSL 三方 golden vector；加载失败必须进入能力诊断而非静默吞掉 | 有 OCIO 和无 OCIO 两种环境都能得到明确结果；同一组 RGB/灰阶/高光样本误差在定义容差内；不支持的组合显示明确降级原因 | OCIO config、ColorTransformProvider、ColorSpaceDescriptor、diagnostics；P0 |
| P0-COLOR-06 | 调色预览、Composite 输入、Scopes、Still/Video 导出和外部监看没有一份统一的端到端色彩一致性矩阵 | “画面看起来正确”不等于导出正确；HDR、宽色域、YUV range、BGR/RGB channel order、10-bit 容器等问题可能只在交付时暴露 | 建立 source → IDT → scene-linear → grade → ODT/display → output encode 的单一数据契约；为每条路径建立灰阶、纯色、HDR 白场、limited/full range golden tests | Rec.709、P3、BT.2020 PQ/HLG、8/10-bit、packed/planar YUV、CPU/GPU/Composite/Color 页面输出对同一测试素材的亮度、色相和 metadata 一致；预览与导出差异有可解释上限 | ColorTransformProvider、TimelineClipFrameProvider、Composite output、Video encoder；P0 |
| P0-COLOR-07 | 遮挡跟踪当前验收失败：遮挡素材中要求至少 70% 的遮挡帧进入 FAILED，实际只有 5/20 | 可能把错误矩阵继续绑定到 Power Window/Qualifier，造成整段画面污染，专业用户无法信任跟踪结果 | 复核遮挡检测、置信度、failure policy、recapture 和 HOLD_LAST 语义；对长遮挡禁止假有效；改进 matte/window 绑定前的失败可视化 | 遮挡帧按素材标注达到失败比例；所有失败带 FailureReason；遮挡后要么准确重捕获，要么保持失败，不允许错误矩阵伪装为 VALID | Tracker2D、TrackingService、TrackerPanel、Mask binding；P0 |

## 5. P1：达到专业调色工作流的核心功能

P1 是用户在长片、广告、纪录片或高质量短视频项目中会持续遇到的能力差距。

| ID | 缺失或不足 | 未来优化 | 验收标准 |
|---|---|---|---|
| P1-COLOR-01 | 当前主要是平面 Layer 参数，没有专业节点图语义 | 建立 Color Node Graph：Serial、Parallel、Layer/Mixer、Key Mixer、外部 matte、节点 bypass、node label、node color、输入/输出连接和节点级缓存；Layer 作为兼容导入层而不是最终模型 | 任意调色可拆成多个节点；节点可重排、禁用、复制、独立预览；节点图保存、撤销和重开保持一致 |
| P1-COLOR-02 | 没有 Clip、Group/Scene、Timeline、Output 等专业调色上下文 | 引入 Pre-Clip、Clip、Post-Clip、Group Pre/Post、Timeline、Output 等 grade context；支持共享 asset grade 与 clip-local override 的可视化继承 | 切换片段时上下文明确；修改共享 grade 能按选择影响多个 clip；局部 override 不会意外污染其他片段 |
| P1-COLOR-03 | 没有成熟的调色参数关键帧/动画工作流 | 为节点参数、Mixer、Qualifier、Power Window、节点混合强度提供统一 keyframe track；调色页与时间线 transport 共享帧率/时间码 | 在任意帧设置 grade keyframe，拖动时间线可平滑插值；保存、撤销、渲染和导出帧值一致 |
| P1-COLOR-04 | Capture Still 和 Presets 已存在，但不是完整的专业 Still/版本库 | 建立 Gallery：Still、Versions、Albums、节点图快照、参考帧、备注、时间码、来源 clip、色彩空间、缩略图；支持只应用节点、只应用 primary、只应用 windows 或全部应用 | 仍然图、版本比较、命名、搜索、排序、拖放应用、局部应用均可完成；应用操作是单一 undo 单元 |
| P1-COLOR-05 | 对比能力只有基础 toggle/split，缺少专业参考监看 | 增加 A/B、两幅/四幅比较、wipe、split-screen、reference still、gallery compare、差异闪烁、reference scopes | 参考图和当前图可同步缩放/定位；Scopes 可选择当前、参考或两者；比较模式不会改变工程状态 |
| P1-COLOR-06 | Scopes 只有 RGB Waveform、Vectorscope、Split | 增加 RGB Parade、Luma Waveform、Histogram、CIE xy/uv、False Color、HDR nits waveform、10-bit code values、zebra、skin-tone line、legal/full range overlay | 每种 Scope 可独立刷新率、分辨率、颜色空间和输入源；HDR 使用 nits/stop 标尺；Scope 和画面抽样位置能联动 |
| P1-COLOR-07 | Scope 设置少，无法选择输入阶段和采样区域 | 支持 source/before grade/after node/after ODT/output 选择，ROI、qualifier/mask only、全帧/可见帧、峰值保持、网格/safe line 和 sample picker | 用户能明确知道 Scope 分析的是哪一阶段；切换输入源有标签且不会显示旧帧 |
| P1-COLOR-08 | Qualifier 只有基础选择语义，缺少专业 matte 工作流 | 增加 matte view、highlight/black/gray preview、clean black/white、denoise、blur、erode/dilate、反选、交并差、键混合、限定器缓存和节点绑定 | 任何限定区域都有可解释的 matte 预览；清理参数可撤销；切换节点、切片和播放不会串用旧 matte |
| P1-COLOR-09 | Power Window/Mask 虽有形状与组，但缺少专业编辑密度 | 增加 Bezier polygon、曲线控制柄、渐变窗口、旋转/羽化可视化、锁定/隐藏、mask keyframe、按节点管理、边缘扩展与 matte 合成模式 | 窗口可精确编辑、批量复制、按节点重用；所有几何、动画和绑定完整保存 |
| P1-COLOR-10 | Tracker 基础架构较完整，但专业生产场景仍缺 shot boundary、长期遮挡和 planar UX | 增加 shot change 检测、occlusion confidence overlay、track forward/backward 合并策略、关键帧手动修正、Planar/Corners/ stabilization、跟踪结果诊断报告 | 真实素材集覆盖运动、模糊、遮挡、切镜、低纹理；所有失败能解释；用户可对失败段手动修正并局部重捕获 |
| P1-COLOR-11 | LUT 仅偏向 .cube 导入和强度 | 支持 LUT input/output domain、LUT 3D size、1D shaper、tetrahedral interpolation、CLF/CTF/3DL 兼容、TITLE/DESCRIPTION/metadata、LUT preview 和依赖打包 | LUT 作用域在 UI 明示；同一 LUT 在 GPU、CPU export、preview 的误差受控；工程移动机器后能自动 relink 或提示 |
| P1-COLOR-12 | RAW 面板不能等同于完整 camera raw pipeline | 建立相机/RAW 格式支持矩阵，支持 debayer quality、white balance、tint、exposure、highlight recovery、decode gamut、ISO/metadata 和 proxy/full quality | 每种支持格式有测试素材、错误能力提示和一致的 preview/export；未知 RAW 不静默当普通图像处理 |
| P1-COLOR-13 | 降噪、去模糊、颗粒已有实现，但缺专业质量控制 | 增加 temporal window、motion estimation、reference frame、quality tiers、cache/bake、确定性 grain seed、输出前后对比和 render status | 播放/暂停/导出明确显示当前质量；重复渲染结果稳定；缓存失效按 clip、grade revision、frame range 正确处理 |
| P1-COLOR-14 | 插件面板的 color 参数仍是占位文本，参数模型不够专业 | 实现 RGBA/HSV/色域感知 color picker；统一 slider、enum、curve、matrix、texture、file、button、dynamic array 参数；加入 capability manifest | 支持的插件参数没有 coming soon 占位；参数值可保存、撤销、动画、导出；缺依赖时显示可修复错误 |
| P1-COLOR-15 | 插件缺少色彩域、HDR、预览成本、GPU/CPU、导出能力声明 | 扩展插件 manifest：input/output space、alpha semantics、precision、preview cost、threading、determinism、export support、version/hash | 插件启用前能显示兼容性；播放质量降级可以按 cost 生效；不支持导出时阻止静默丢效果 |
| P1-COLOR-16 | Layer copy_params_from 虽覆盖很多字段，但用户没有可选类别复制的专业入口 | 提供 Copy/Paste Grade、Paste Attributes、选择 primary/curves/HSL/mixer/LUT/windows/qualifier/tracking/effects/plugins/RAW；支持链接/取消链接共享调色 | 从一个 clip 到多个 clip 复制时可预览变更清单；一次粘贴一个 undo；不相关字段不被意外覆盖 |
| P1-COLOR-17 | 当前作用域只有 asset_default、clip_local、timeline_adjustment 等字符串，没有上下文可视化 | 用强类型 GradeContext/GradeLink 表达所有权、继承、覆盖和断链；为每个 clip 显示最终生效链路 | 用户能一眼看到“来自哪里”；修改 shared grade 的影响范围在提交前可预览 |
| P1-COLOR-18 | 调色页面仍由 MainWindow 手动装配，Color module 的 page/action/save 注册未闭合 | 收敛到 Color module 的 page factory、action registry、save handler、lifecycle hook；MainWindow 只提供 host context | Color page 可以独立启动/测试；模块启停、保存、快捷键、面板恢复不依赖隐藏的 MainWindow 分支 |
| P1-COLOR-19 | 调色页 Dock 数量多，但缺少可保存、可切换的 Color workspace preset | 提供 Primary、Secondary、HDR、Tracking、Scopes、Delivery 等 workspace；保存 panel visibility、split ratio、active tool、scope input | 工作区可一键切换；工程内和用户全局布局不互相污染；升级后旧布局可迁移 |
| P1-COLOR-20 | 时间线 Color clip strip 不能替代专业镜头导航 | 增加上一镜/下一镜、scene grouping、shot thumbnails、marker、过滤、批量选择、当前 clip/源帧/时间码明确显示 | 调色员不离开页面即可按镜头浏览、匹配和批量应用；切换不会闪旧帧或丢 grade context |
| P1-COLOR-21 | 没有自动镜头匹配、参考帧匹配和可解释的 Auto Color | 增加基于 reference still 的 histogram/neutral/skin tone/曝光匹配，提供建议值和可撤销预览，不覆盖用户结果 | 自动匹配输出差异指标；用户可选择应用哪些参数；不会改动源素材和已锁定节点 |
| P1-COLOR-22 | 专业监看和校准入口不完整 | 支持 display transform 状态栏、10-bit monitor path、HDR peak/paper white、外部监看/SDI 能力探测、校准 profile 选择 | 画面上始终显示 source/working/display/output；硬件不可用时显式提示；监看和导出使用同一色彩契约 |
| P1-COLOR-23 | 项目依赖没有形成完整的可移植包 | 统一打包 LUT、OCIO config、ICC、插件、AI model、RAW profile、字体/纹理等依赖，记录 hash、来源和版本 | 工程复制到另一台机器能给出完整依赖报告；缺失项可定位、替换或降级；不允许静默使用错误同名文件 |
| P1-COLOR-24 | Script Panel 有入口，但缺少稳定的专业调色 API | 提供基于 project/clip/node/context/still 的 versioned API，支持批量应用、镜头遍历、导入导出和 dry-run diff | 脚本执行有权限、日志、错误定位和单一 undo；API 版本变化有迁移提示 |
| P1-COLOR-25 | 交付链路对“哪些操作可以导出为 LUT/ASC-CDL/视频”说明不足 | 增加 Delivery Matrix：CDL/ASC-CDL、3D LUT、ICC/OCIO、still/video、HDR metadata、无法表达的 spatial effect；输出前生成报告 | 导出前清楚列出会保留、烘焙或丢失的效果；用户确认后才允许有损转换 |

## 6. P2：性能、工程质量与体验优化

| ID | 当前问题 | 优化动作 | 验收标准 |
|---|---|---|---|
| P2-COLOR-01 | Color 页 4K 播放仍受 CPU decode 约 49–54 fps 限制，渲染 GPU 吞吐不是唯一瓶颈 | 把 Edit 页 VT/硬件零拷贝解码能力接入 Color page；对 PyAV fallback 明确显示 decode path | 4K H.264/HEVC/ProRes 在目标设备达到定义的实时门槛；decode/render 各自有 telemetry |
| P2-COLOR-02 | 播放降级对 bilateral、blur、磨皮插件基本无效 | 为重型工具定义 playback shader variant、采样预算、半分辨率/低质量模式和恢复策略；插件读取 preview_cost | 播放时帧率优先级生效，暂停时自动恢复质量；画面上有质量标识；导出永不误用低质量变体 |
| P2-COLOR-03 | USM 迭代模式仍是 4K 最昂贵内置效果之一 | 评估单 pass 多环采样、低分辨率 blur pyramid、缓存中间结果和参数上限 | 目标素材上达到约定 p95 ms；PSNR/视觉回归与当前实现保持容差 |
| P2-COLOR-04 | Shader compiler 会内联未启用 snippets，结构参数变化仍整段重编译 | 按 active capability 组装 shader；预编译常用排列；把 uniform-only 变化彻底留在 uniform 更新 | 常用首帧/开关/插件结构变化编译时间达标；slider 拖动不触发重编译 |
| P2-COLOR-05 | GPU scope 通道虽已实现，但真实 GPU/跨上下文/1080p/4K/快速切换验证仍不充分 | 建立硬件矩阵和录制式 scope golden tests；覆盖窗口关闭、panel hide、seek、clip switch、device loss | 无旧 Scope 闪回、无 GL leak、无 UI 线程 GL 调用；GPU 失败有可见降级状态 |
| P2-COLOR-06 | 预览缓存、matte cache、LUT/curve texture、denoise history 的失效契约较复杂 | 统一以 source revision、grade revision、frame、quality、space、node/context 生成 cache key；所有缓存有 owner 和释放路径 | 参数、切镜、改色彩空间、播放重启后不会复用错误缓存；缓存命中率和内存可观测 |
| P2-COLOR-07 | HistoryManager 以图像副本加 Layer 序列化保存状态，max_steps 固定且没有磁盘/内存预算 | 改为 immutable command/snapshot，按操作合并 slider gesture，支持内存预算和可选磁盘历史 | 大图连续调色不导致不可控内存增长；每次用户操作撤销粒度符合预期；undo/redo 跨 clip 安全 |
| P2-COLOR-08 | Layer 直接可变，UI、保存和渲染快照之间仍有多处同步责任 | 引入 ColorState revision、atomic snapshot、command boundary；UI 不直接改 renderer-owned 对象 | 每个 frame 带 source/grade/node revision；旧帧无法覆盖新状态；保存只读稳定 snapshot |
| P2-COLOR-09 | 色彩管理单测覆盖数学，但 OCIO/parity/hardware/output path 有 skipped 或缺少 real media | 建立无 GPU 的 reference vectors、带 OCIO 的 CI job、真实素材 smoke、软渲染和硬件 nightly | CI 不把 skipped 当 pass；每种 provider/路径有明确能力状态和失败原因 |
| P2-COLOR-10 | 诊断信息分散在日志，用户看不到当前有效色彩空间、质量和降级原因 | 增加 Color Diagnostics 面板/状态栏：source tag、working space、ODT、range、decode path、render quality、cache、plugin status | 用户可导出诊断包；每个“颜色不对/卡顿/效果未生效”问题都有可定位字段 |
| P2-COLOR-11 | HSL Curves panel 存在空 changed handler，封装 contract 不完整 | 让 Panel 成为唯一 signal owner，公开 typed changed 信号；移除 controller 直连绕过或给出明确 adapter | 面板单测能证明 UI 变更、engine、ColorStack、undo、render 四者都被触发一次 |
| P2-COLOR-12 | Scopes CPU fallback 和 GPU presenter 并存，能力边界不够显式 | 为 CPU diagnostic fallback 加状态标记、禁用原因和输入色彩空间标签；生产默认只走 GPU | 用户知道当前 Scope 是 GPU、CPU fallback 还是冻结帧；fallback 不会伪装实时 |
| P2-COLOR-13 | LUT/插件/RAW/ICC 文件路径与工程 schema 的迁移策略不统一 | 所有外部资源使用 asset reference + hash + relative package path；打开工程先做 dependency resolve | 移动工程、重命名资源、跨平台路径后不丢引用；缺失资源有可修复对话 |
| P2-COLOR-14 | 预设面板扫描、缩略图、导入导出和删除能力已有，但缺少内容版本和兼容提示 | 为 preset 添加 schema/version/source-space/target-space/required-plugin metadata；异步缩略图和大库索引 | 大量 Still/Presets 扫描不卡 UI；旧 preset 可迁移；应用前显示空间和依赖差异 |
| P2-COLOR-15 | 专业调色员所需快捷键、数字输入、鼠标精细拖动、范围/步长和 undo 合并策略未统一 | 建立 Color shortcut map、精细调整修饰键、直接输入、双击归零、可配置 gesture transaction | 轮盘、曲线、窗口、Qualifier、Scopes 操作都可键鼠完成；每次 gesture 只产生预期 undo |
| P2-COLOR-16 | 大量 Panel 默认隐藏，首次使用者不容易发现工具之间的组合关系 | 增加任务导向 onboarding、工具搜索、面板 quick switch、当前节点/当前 Layer 上下文提示 | 用户能搜索并打开任意调色工具；面板显示作用对象和当前状态，而非只显示孤立参数 |
| P2-COLOR-17 | 多处历史注释、占位命名和已 superseded 文件增加维护误判 | 清理 superseded tools_panel、统一命名、补 migration notes 和 architecture map | 新开发者能从 module registry 找到唯一入口；静态检查不再出现已失效 API 说明 |
| P2-COLOR-18 | 国际化、无障碍和高 DPI 下 Scope/轮盘/曲线精度尚未形成系统验收 | 补齐 i18n key、键盘访问、tooltip、对比度、DPI、最小尺寸和色觉友好配色 | 中文/英文切换无硬编码关键文案；高 DPI 下控件和 Scope 标尺仍可读可操作 |

## 7. P3：长期专业化能力

| ID | 方向 | 未来优化 |
|---|---|---|
| P3-COLOR-01 | 完整 ACES/OCIO 工作流 | 支持 OCIO config browser、roles、views、looks、named transforms、ACEScct/ACEScg、输入/显示/输出变换和 config 版本锁定 |
| P3-COLOR-02 | 高端 HDR finishing | 支持 PQ/HLG mastering display、峰值/纸白、gamut compression、highlight roll-off、HDR10 metadata、Dolby Vision/广播交付能力评估 |
| P3-COLOR-03 | 高级摄影机/胶片科学 | 扩展 camera RAW、log/logC/S-Log/V-Log/Canon Log 等 IDT、胶片负片/打印 LUT、grain model 和镜头/相机匹配 |
| P3-COLOR-04 | 高级实时监看 | 外部 SDI/DeckLink、10-bit/12-bit 输出、reference monitor calibration、legalizer、broadcast safe 和远程监看 |
| P3-COLOR-05 | 协作与审片 | 远程共享 Still/版本、批注、镜头锁定、审片状态、变更追踪和冲突合并 |
| P3-COLOR-06 | 控制面与自动化 | Tangent 等外部控制器、MIDI/OSC、批处理渲染、远程 headless grading 和稳定的 Python/CLI automation |
| P3-COLOR-07 | 可复用色彩资产生态 | 官方 LUT/Look/Power Window/Qualifier/Node template 市场、签名、版本兼容和企业部署策略 |

## 8. 分阶段实施路线

### Phase 0：正确性和发布门槛

目标：先保证工程不会丢调色、不会静默变色、不会在退出或切片时崩溃。

1. 修复 QThread/Renderer/Tracking/Decode 的统一关闭和测试清理。
2. 决定唯一 ColorState schema，合并 ColorStackData、Hydrator 和 Layer.to_dict 的职责。
3. 完成高级状态 round-trip：mask、Qualifier、Warper、effects、plugins、RAW、denoise/deblur、film grain、tracking、active layer。
4. 把 source/working/display/output descriptor 和 HDR 设置变成工程显式状态。
5. 修复/验证 OCIO 配置、P3/BT.2020 映射和 builtin/GPU/OCIO parity。
6. 建立 SDR、HDR、P3、YUV、10-bit、CPU/GPU、preview/export 的 golden output matrix。
7. 修复遮挡跟踪失败检测，禁止错误矩阵继续进入蒙版绑定。

Phase 0 退出条件：

- Color 专项测试可完整结束，不出现进程 Abort。
- 关键状态保存重开、撤销重做、片段切换全部通过。
- HDR/宽色域色彩闭环有可重复的 reference output。
- 遮挡、模糊、切镜素材的跟踪结果可解释且无假有效。

### Phase 1：专业调色模型

目标：从“多个工具面板”升级为“可管理的专业调色结构”。

1. Color Node Graph 与节点级输入/输出。
2. Clip/Group/Scene/Timeline/Output 多级 grade context。
3. 节点参数和窗口的 keyframe。
4. Still/Versions/Reference Gallery。
5. A/B、wipe、split-screen、reference scopes。
6. 共享 grade、局部 override、可选择性复制。

Phase 1 退出条件：

- 一个复杂镜头可以被拆成可读的节点图并逐节点查看。
- 多个镜头可通过 shared grade 和 local override 管理。
- 仍然图、版本、节点复制、关键帧在重开和导出后不改变语义。

### Phase 2：专业监看和选择性调色

目标：提高调色判断的精度和复杂素材的成功率。

1. RGB Parade、Histogram、CIE、False Color、HDR Scopes。
2. Qualifier matte 工具和 mask/Power Window 编辑增强。
3. Tracker 遮挡/切镜/重捕获/Planar 验收。
4. LUT 域管理、Shaper、插值和 metadata。
5. RAW 能力矩阵和质量档位。
6. 降噪/去模糊/颗粒的缓存、预览质量和 deterministic export。
7. 插件 color picker、参数类型和 capability manifest。

Phase 2 退出条件：

- 调色员能仅靠 Scopes、matte view、reference still 完成曝光、白平衡和肤色判断。
- 典型限定器、窗口、跟踪、LUT、RAW 素材在预览和交付中一致。

### Phase 3：性能、交付与生态

目标：让复杂调色在长项目中可持续运行，并能可靠交付给其他系统。

1. Color 页硬件解码/零拷贝、重型工具播放降级和 shader compile cache。
2. 统一缓存键、内存预算、诊断和 real hardware matrix。
3. ACES/OCIO、HDR finishing、外部监看和 broadcast safe。
4. Delivery Matrix、CDL/ASC-CDL/LUT/ICC/OCIO 交换与依赖打包。
5. 脚本、批处理、控制器、协作和审片。

## 9. 最小专业验收矩阵

| 场景 | 必须验证 |
|---|---|
| SDR still | sRGB full → scene-linear → grade → sRGB full；保存、重开、Scopes、导出一致 |
| Rec.709 video | BT.709 primaries、BT.709 transfer、limited range；YUV matrix/range、8/10-bit 和时间线帧号一致 |
| HDR10 | BT.2020 + PQ + 10-bit；target peak、paper white、tone map、HDR Scope、metadata 和 export 一致 |
| HLG | BT.2020 + HLG；预览 ODT、广播范围、输出 tagging、SDR fallback 行为明确 |
| Display P3/DCI-P3 | primaries、transfer、矩阵方向、ICC/OCIO 映射不混淆；纯 RGB 和灰阶 golden test 通过 |
| 多 Layer/节点 | 节点/Layer 顺序、opacity、blend、mask、Qualifier、LUT、plugin 和 active node 全部 round-trip |
| 片段切换 | source frame、grade context、scope input、tracking、matte、缓存和旧帧保护正确 |
| 跟踪 | 前后向、TRS/affine/homography、遮挡、模糊、切镜、失败原因、手动修正和重捕获 |
| 预览/导出 | CPU/GPU/Composite/Color page/Still/Video 输出在定义容差内一致 |
| 性能 | 1080p 与 4K、基础调色与重型工具、播放/暂停/拖动参数、首帧编译和解码 telemetry |
| 工程可靠性 | 保存、另存、升级旧 schema、移动依赖、撤销重做、取消、异常和关闭窗口均不丢状态 |
| 插件 | color 参数、结构参数、缺依赖、HDR/精度、预览降级、缓存和导出能力全部有明确状态 |

## 10. 已有基础，不建议重复建设

以下能力已经有可复用基础，后续应优先补齐契约、产品闭环和验收，不应重新做一套平行实现：

- src/features/color/ui/color_wheel.py：基础调色面板和色轮。
- src/features/color/ui/hsl_curves_panel.py 及 shared HSL widget：HSL 曲线编辑器，重点是收敛 changed 信号契约。
- src/features/color/ui/mask_panel.py、qualifier_panel.py、tracker_panel.py：选择性调色与跟踪入口。
- src/features/color/core/tracking：不可变请求、失败原因、双向跟踪、置信度、序列化和坐标映射。
- src/features/color/ui/scope_panel.py、src/shared/ui/widgets/gpu_scope_widget.py：GPU Scope 生产骨架。
- src/features/color/core/lut_exporter.py、src/shared/ui/widgets/lut_gallery.py：LUT 导入、Gallery 和部分导出基础。
- src/features/color/core/video_denoise_pipeline.py、video_temporal_denoise.py：视频降噪管线基础。
- src/features/color/core/color_space_descriptor.py、src/shared/color/color_transform_provider.py：结构化色彩描述符和变换 provider 基础。
- src/shared/project/color_stack.py、src/features/color/adapters/color_stack_hydrator.py、src/shared/project/save_router.py：调色栈保存/恢复骨架，下一步应统一而不是再增加第三套格式。
- docs/color_tools_perf_report_2026-08-21.md：已有真实 2K/4K 性能基准和视觉回归基础。

## 11. 建议的执行顺序

如果只允许先做一轮，建议顺序如下：

1. P0-COLOR-01：线程生命周期和测试 Abort。
2. P0-COLOR-02/P0-COLOR-03：统一 ColorState 与持久化契约。
3. P0-COLOR-04/P0-COLOR-05/P0-COLOR-06：色彩管理、OCIO/provider parity、预览与交付闭环。
4. P0-COLOR-07：遮挡跟踪可靠性。
5. P1-COLOR-01/P1-COLOR-02：节点图与多级调色上下文。
6. P1-COLOR-03/P1-COLOR-04/P1-COLOR-05：关键帧、Still/Versions、参考比较。
7. P1-COLOR-06 至 P1-COLOR-13：Scopes、Qualifier、Mask、Tracker、LUT、RAW 和视频工具质量。
8. P1-COLOR-14 至 P1-COLOR-25：插件、复制、工程依赖、脚本与交付能力。
9. P2 性能、诊断、测试矩阵和 workspace 产品化。
10. P3 ACES/HDR finishing/外部监看/协作/控制器生态。

## 12. 评估结论

当前 Topos Color 的优势是工具覆盖面广、GPU 渲染和 Scope 架构已有投入、核心色彩数学与调色栈基础测试较完整。当前不能直接与专业软件等价的根本原因，是“参数存在”尚未升级为“节点、上下文、版本、监看、色彩管理、持久化和交付都形成稳定闭环”。

因此，下一阶段不建议继续单纯增加新的调色滑块。优先级应放在 P0 正确性和状态契约，再建设 Node Graph、Grade Context、Still/Version、专业 Scopes 和色彩管理闭环，最后处理解码、重型效果、插件生态和高端交付。完成 Phase 0 和 Phase 1 后，调色页才具备向专业生产工作流迁移的可靠基础。
