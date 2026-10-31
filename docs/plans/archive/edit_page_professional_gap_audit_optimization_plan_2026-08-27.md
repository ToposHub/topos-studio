# 剪辑页面专业化差距评估与优化计划表

> 评估日期：2026-08-27
> 评估范围：Topos Edit 产品的剪辑工作区、媒体池、源监看器、节目监看器、时间线、效果、字幕、音频、场景切割、代理/缓存、项目模型、预览、导出和互操作链路。
> 当前判断：基础编辑骨架已经比较完整，但距离 DaVinci Resolve / Premiere Pro / Final Cut Pro / Avid Media Composer 这类专业 NLE，主要差距已经从“有没有控件”转向“生产链路是否真正贯通、不同路径是否一致、异常是否可恢复、专业交付是否可信”。

## 1. 结论摘要

当前剪辑系统不是空白原型。它已经具备：

- 媒体池、Bin、Smart Bin、搜索/排序、离线素材和重链接；
- Source Viewer / Program Viewer、标记 In/Out、J/K/L、循环、逐帧、时间码和插入/覆盖/追加；
- 多视频轨、音频轨、字幕轨、剪辑项、转场、标记、吸附、目标轨道、锁定/静音/独奏；
- Split、Trim、Ripple Delete、Roll、Slip、Slide、速度和淡化；
- 效果、调整片段、生成器、字幕图形、音频混音器、代理/渲染缓存、场景切割和部分 AI 入口；
- 不可变时间线模型、命令式变更、Undo/Redo、视频/音频导出、批处理、Stem 和 CSV Cue Sheet。

但是，当前产品仍处于“功能骨架已成、生产闭环未收口”的阶段。最重要的不是再堆更多按钮，而是先解决以下四类问题：

1. **同一份工程在实时预览、离线导出、音频播放、Stem 和字幕/效果渲染中必须得到一致结果。**
2. **已写出的模型和命令必须全部拥有可见、可达、可恢复的用户入口。** 当前多机位、嵌套序列、FCPXML、OTIO 等能力存在代码，但明确处于 dormant 或无生产入口状态。
3. **剪辑基本操作不能在常规拖拽路径中抛出未实现异常。** 当前跨轨移动到已有轨道仍是硬缺口。
4. **时间码、录音、BWF、混合帧率和交付互操作必须达到广播/后期可验证的准确度。**

本轮没有修改源代码，只新增本计划表。定向纯模型测试结果为 **331 项收集、326 项通过、5 项跳过**；该结果不能替代真实 Qt/ModernGL/GPU、长时间线、真实编码器、真实音频设备和外部 NLE 导入验收。

## 2. 评估证据与判定口径

### 2.1 主要检查入口

| 层级 | 当前实现位置 | 本轮关注点 |
|---|---|---|
| 产品注册 | src/products/topos_edit.py、src/features/edit/module.py | 页面是否真正注册、菜单/快捷键是否到达业务命令 |
| 页面编排 | src/features/edit/ui/edit_page.py、src/app/controllers/dock_layout_controller.py | EditPage 与各 Dock 的连接、页面状态和生命周期 |
| 时间线 UI | src/features/edit/ui/timeline_panel.py、timeline_view.py、timeline_items.py | 轨道、拖拽、剪辑、吸附、目标轨道、波形、标记、转场 |
| 时间线模型 | src/features/edit/core/timeline_v2.py、timeline_ops.py、commands.py | 不可变模型、原子命令、Undo/Redo、时间码和跨轨行为 |
| 预览/渲染 | src/features/edit/core/timeline_preview_renderer.py、edit_gpu_preview_renderer.py、render_plan.py | GPU/CPU、代理、缓存、效果、字幕和导出的路径一致性 |
| 音频 | audio_playback.py、audio_graph_snapshot.py、audio_graph_renderer.py、recording_manager.py | 实时/离线一致性、路由、自动化、录音、通道布局和 BWF |
| 媒体和交付 | project.py、media_pool_panel.py、timeline_export.py、stem_export.py、interchange.py | 素材管理、导出、队列、互操作和恢复 |
| 高级编辑 | multicam.py、nested_sequence.py、scene_cut_controller.py、adapters/ | 是否有用户入口、是否能完整保存和重新打开 |

### 2.2 优先级定义

| 优先级 | 含义 |
|---|---|
| P0 | 会导致结果错误、工程不可交付、操作直接失败或无法安全恢复；应阻断专业版发布 |
| P1 | 专业剪辑工作流中高频或高价值的能力缺失；完成后才具备可持续使用的 NLE 体验 |
| P2 | 效率、易用性、可维护性、扩展性和性能的专业化增强 |
| P3 | 面向高端客户、团队协作、脚本化或生态扩展的长期能力 |

状态使用“已具备 / 部分具备 / 代码存在但未接入 / 缺失 / 需验证”，不把测试模型或 dormant 文件直接算作产品能力。

## 3. 当前能力盘点：已经有的内容不应重复建设

| 能力域 | 当前状态 | 评估结论 |
|---|---|---|
| 媒体池 | 素材、Bin、Smart Bin、搜索、排序、网格/列表、缩略图、离线提示、重链接、生成器和时间线卡片 | 基础媒体管理已存在；下一步应补“专业素材日志和依赖管理”，不是重写媒体池 |
| 源/节目监看器 | Source/Program 双监看器、In/Out、播放控制、J/K/L、逐帧、时间码、循环、质量和视图菜单 | 基础监看器闭环已存在；仍需补 trim monitor、诊断 HUD 和多机位监看 |
| 时间线结构 | 视频、音频、字幕轨；轨道属性、标记、转场、选择、链接、自动化等模型字段齐全 | 数据模型较完整；生产入口和边界行为仍不完整 |
| 基本剪辑 | Split、Trim、Insert、Overwrite、Append、Ripple Delete、Roll、Slip、Slide、速度、淡化、复制/粘贴 | 传统三点编辑和常用 trim 已覆盖；Lift/Extract、完整跨轨移动和高级 Trim 还缺 |
| 效果与属性 | Clip/Transition/Track/Asset/Timeline 属性面板、效果启停/删除/排序、参数和部分关键帧 | 效果系统存在；预览/导出/HDR/失败诊断和转场直连仍需收口 |
| 字幕 | SubtitleClip、字幕轨、Caption Workspace、SRT 和文本样式/图形渲染、语音转字幕入口 | 不应再规划“从零做字幕”；应补专业交付格式、样式可移植性和文本剪辑体验 |
| 音频混音 | 音量、声像、EQ、轨道效果、发送、Bus、Master、自动化、淡化、Stem 和响度相关能力 | DSP 和离线渲染基础较强；实时路径仍没有统一消费编译后的音频图 |
| 代理/缓存 | Proxy/Render Cache、低分辨率设置、缓存状态和预览降级路径 | 基础设施已存在；需补统一策略、可观测性、失效规则和长项目性能验收 |
| 导出 | 视频/图片序列、H.264/HEVC/ProRes/DNxHR、音频、选区/In-Out/Selected/Custom、字幕、响度、单片段和批处理 | 导出能力不为空；需要把时间线工程导出提升为可审计、可恢复、可互操作的交付系统 |
| 项目安全 | 不可变时间线、命令式变更、项目序列、重链接、备份/恢复相关代码 | 架构方向正确；还需完善崩溃恢复、命令事务和跨模块失败回滚 |

## 4. P0：发布阻断与正确性问题

| 编号 | 问题/缺口 | 现状证据 | 对专业剪辑的影响 | 优化方案 | 完成验收 |
|---|---|---|---|---|---|
| P0-01 | 实时音频和离线音频仍是两条运行路径 | audio_graph_snapshot.py 明确写明实时仍使用 AudioMixer.mix_samples；AudioGraphRenderer 当前调用点主要是 timeline_export.py 与 stem_export.py | 同一个工程可能出现预览与导出声音不一致，尤其是速度、淡化、路由、Bus、自动化和有状态 DSP | 以 AudioGraphSnapshot 编译成线程私有 CompiledAudioGraph；实时播放、离线导出、Stem、响度分析共用同一节点处理器，在音频 block 边界原子换图 | 固定 PCM fixture 下，实时 memory sink、离线导出和 Stem 结果按 DSP 类型达到定义误差；每次换图有 revision、切换点和诊断 |
| P0-02 | 非法音频路由只记录警告，没有阻止错误播放或保持上一张有效图 | EditPage._sync_audio_tracks_from_timeline() 在 snapshot 无效时只 logger.warning，随后仍继续激活音频 | 环路、未知 Bus、无效 Send 目标可能造成错误声音或静音；用户只听到结果错误，不知道工程已损坏 | 路由图验证变成激活门：新图无效则保留上一张有效图，UI 显示错误路径；导出继续阻断；修复后才允许换图 | 非法路由不会静默回退 Master；播放、导出和保存工程都显示可定位错误；修复/撤销后恢复上一有效图 |
| P0-03 | 编辑预览、全帧预览、导出和 HDR 效果表面仍未完全同构 | 最新效果审计仍记录 Edit HDR surface、全帧 preview/export parity、低分辨率预览 pass 和诊断状态为部分完成 | 用户在预览中看到的效果可能与最终文件不同；HDR、效果链、调整片段和字幕最容易出现差异 | 明确统一的 RenderContext/RenderSurface；GPU/CPU/Proxy/Export 使用同一效果能力矩阵；对 unsupported/degraded/preview-only 做显式状态 | 每个视频效果都有 preview/export/HDR 能力矩阵；金样帧对比通过；任何降级在画面 HUD、属性面板和导出报告中可见 |
| P0-04 | 跨轨移动到已有轨道仍然直接抛出未实现异常 | src/features/edit/core/commands.py:709-711：MoveClipToTrackCommand 对 create_new_track=False 执行 NotImplementedError | 这是时间线最基本的拖拽动作之一；用户可能拖到已有轨道后操作失败，且可能丢失预期位置 | 实现已有目标轨道的冲突检查、覆盖/插入策略、链接 A/V 同步、字幕图形绑定、标记迁移和 Undo；UI 预览落点要与命令一致 | 向已有视频/音频轨移动、跨类型拒绝、重叠冲突、锁定轨、链接剪辑和撤销全部有 UI 与纯模型测试；不得出现 NotImplementedError |
| P0-05 | Drop-frame 时间码反向转换不是真正的逆运算 | interchange.py:83-95 的 timecode_to_frames() 只按四段时间码和四舍五入 FPS 计算，没有扣除 drop-frame 丢弃帧；正向函数却实现了 29.97 drop-frame | 广播时间码、录音对齐、Cue Sheet 和外部 NLE 交换会产生帧级偏移 | 使用 30000/1001、60000/1001 等有理数 timebase；统一 frames_to_timecode / timecode_to_frames / samples 转换，明确非法 DF 帧 | 29.97/59.94 的边界分钟、十分钟、小时、负值和长时长 round-trip 全通过；与独立 SMPTE 参考实现一致 |
| P0-06 | 录音能力被标称为 BWF，但当前实际不是完整 BWF | recording_manager.py 的 is_true_bwf_supported() 返回 False；写入的是 RIFF INFO，不含 bext、TimeReference、coding history；interchange.py 的能力表却把 BWF 标为 available | 影视录音、ADR、外部 DAW 交换时，时间参考和来源元数据不可信；产品能力声明误导用户 | 实现 EBU Tech 3285 bext，写入 originator、reference、date/time、TimeReference、coding history；若尚未完成，所有 UI/文档统一称 WAV with INFO metadata | 独立 BWF parser 能读取 bext 和 sample time reference；外部 DAW/NLE 能按时间码导入；能力表与实际 writer 自动一致 |
| P0-07 | 长时间录音采用不断 np.concatenate 的内存缓冲 | recording_manager.py 的 inject_pcm() 和设备回调路径对已有 PCM buffer 每次拼接 | 长录音出现 O(n²) 拷贝、内存峰值和卡顿；录音失败时还可能无法安全保存 take | 改为分块环形/分段 buffer，后台顺序写临时 WAV/CAF，结束时原子封装；限制内存并支持磁盘空间预警 | 连续 2 小时、多通道录音内存曲线稳定；设备拔出、磁盘不足、取消和恢复都不损坏工程，不丢已完成 take |
| P0-08 | 剪辑页面整体缺少“生产验收门”，当前绿灯主要来自纯模型测试 | 本轮 331 项定向测试中 326 通过、5 跳过；尚未以真实 Qt/ModernGL/GPU、混合编码器和真实设备完成全链路验收 | 纯函数通过不能保证 Dock 生命周期、GPU 资源释放、波形线程、播放、导出和关闭窗口安全 | 建立按场景分进程的 Edit smoke/soak 套件，禁止所有 GUI/GPU 测试依赖单一长进程；加入真实媒体金样和故障注入 | 新工程→导入→剪辑→播放→导出→重开、4K 长时间线、代理切换、窗口关闭、取消导出、设备失败均有可重复报告 |

## 5. P1：达到专业 NLE 工作流所需的核心缺口

| 编号 | 问题/缺口 | 当前状态 | 专业化优化方向 | 完成验收 |
|---|---|---|---|---|
| P1-01 | 多机位只有纯模型/命令，没有生产工作流 | multicam.py 顶部明确标记 DORMANT；虽然有 MulticamAngleGroup 和 SwitchMulticamAngleCommand，但未发现剪辑页创建多机位素材、同步角度和多机位监看入口 | 增加创建多机位序列/素材组、按音频波形/时间码/标记同步、Angle Viewer、实时切换、音频跟随/独立选择、角度标签和重新同步 | 从多素材创建 Multicam Group，在多机位 Viewer 实时切换，切换点可编辑、可撤销、可保存、重开后保持 |
| P1-02 | 嵌套序列/Compound Clip 代码存在但没有正式入口 | nested_sequence.py 顶部标记 DORMANT；渲染器能识别 nested sequence，但没有明确的创建、进入子序列、退出、展开和依赖管理工作流 | 增加“从选择创建嵌套序列/展开/进入编辑”，保存子序列资产和父子依赖；加入循环引用、代理、缓存、字幕和音频递归策略 | 创建嵌套序列后双击进入、修改子序列立即更新父序列；循环引用阻断；导出和重开结果一致 |
| P1-03 | Lift / Extract 语义缺失 | 当前检索到 Ripple Delete、Insert、Overwrite、Append，但没有对应的 Lift/Extract 业务入口 | 提供 Lift（保留间隙）和 Extract（按目标轨道/链接轨道移除并波纹收紧），明确视频、音频、字幕和 Sync Lock 影响 | In/Out 范围、选中片段、锁定轨、链接 A/V、字幕和撤销场景均有稳定结果 |
| P1-04 | 高级 Trim 监看与操作不完整 | 已有 Roll/Slip/Slide/Ripple Trim，但未形成专业 Trim Monitor、动态 Trim、数值 trim、双边/多剪辑 trim 工作流 | 增加 Trim Monitor 双帧显示、J/K/L trim、键盘单帧/多帧步进、输入数值、对齐到标记、A/V 独立 trim 和剪辑手柄可用性诊断 | 所有 trim 模式都能在预览中看到前后帧，移动后音频/字幕/标记和 Undo 保持一致 |
| P1-05 | 轨道编辑语义仍不够专业 | 已有锁定、静音、独奏、目标轨道和独立 V/A target；缺少 Sync Lock、Source Patching、轨道组/角色、轨道高度/显示状态的完整持久化策略 | 将 Source Patch、Track Target、Sync Lock、Link Selection、Track Group/Role 作为模型字段和可见状态；Insert/Ripple 行为必须读取这些状态 | 同一时间线在保存、重开和多 Dock 布局下状态一致；每个编辑命令有作用轨道清单 |
| P1-06 | 转场双击入口是明确的 no-op | edit_effects_panel.py:1208-1216 的 _on_transition_double_clicked() 仍记录 “not implemented”；虽然拖拽 MIME 路径存在 | 双击转场应直接应用到选区或最近邻剪辑，处理 handles、重叠、默认时长、居中/起点/终点和音频转场规则；无合法落点时给出原因 | 双击和拖拽使用同一命令；没有 handles、锁轨、轨道类型不符时不会静默失败；参数可立即编辑和撤销 |
| P1-07 | 音频效果/转场的能力展示会降级为占位文本 | 音频效果树在没有注册效果时显示 “Audio effects coming soon”；这容易让用户把注册/环境问题误认为产品能力缺失 | 建立效果注册健康检查、音频效果 capability 状态、内置效果最小集合和缺失原因；禁用项显示 install/unsupported/disabled，而不是泛化占位 | 空注册、插件缺失、参数不支持、效果失败均有可操作诊断；内置 EQ/Compressor/Limiter/Noise 等效果可完成预览和导出 |
| P1-08 | 音频变速目前是变调 resample，保持音调变速尚未实现 | audio_playback.py 的音频 Decoder 注释明确写明 preserve-pitch 尚未实现 | 增加 WSOLA/phase-vocoder 等保持音调模式，速度曲线和音频重采样统一离线/实时语义；明确反向、极慢速、seek 状态 | 0.5x/1.5x/速度曲线下人声音调可保持；实时、导出、Stem 的时长、相位和边界一致 |
| P1-09 | 多通道音频布局没有达到专业资产级别的真值管理 | channel_layout 可按通道数推断布局；录音 writer 也主要从通道数推导布局 | 在 MediaAsset、Clip、Track、Bus、Stem 和导出配置中持久化明确布局，支持 mono/stereo/5.1/7.1/自定义 mapping；禁止未知布局静默猜测 | 通道映射、downmix/upmix、导出 layout、外部软件导入结果均有 golden tests 和可视化 routing matrix |
| P1-10 | 录音/ADR 产品链路还不完整 | 有设备、take、pre-roll、loop、latency calibration 和录音按钮基础；但未形成专业 input patch、监听、输入电平、take sheet、时间码对齐和录音资产回写的完整验收链 | 增加输入设备/通道 patch、输入监听、预录/后录、take 列表、最佳 take 标记、记录起始时间码、延迟补偿和安全导入；与 P0 BWF 共用元数据 | R/停止只产生一个 asset 和一次 Undo；多 take 可比较/替换；校准后音视频偏移在目标阈值内 |
| P1-11 | 场景切割默认使用完整资产范围，没有严格尊重时间线剪辑范围 | scene_cut_controller.py:235-237 仍有 TODO，当前注释说明暂时使用 full asset range | 将 timeline clip 的 source_start/source_duration、速度、嵌套和代理映射传给分析器；结果回写为当前剪辑范围内的 cuts/subclips | 对裁剪片段、变速片段、代理和离线素材分析时，不会把片段外的镜头切点写回时间线 |
| P1-12 | 媒体池缺少专业素材日志能力 | 当前已有 reel/scene/shot/take 等生产字段，但没有完整的关键词、评级、旗标、颜色标记、注释、范围标记和多条件查询体验 | 增加 Keywords/Rating/Flag/Color/Comment/Marker/Range Metadata、批量编辑、Saved Search 查询构建器、按镜头/角色/声音/代理状态过滤 | 重开工程后元数据、Smart Bin 和搜索结果稳定；批量标记可 Undo；媒体池可显示素材使用位置 |
| P1-13 | 缺少素材使用关系和“安全替换/收集”能力 | 有重链接和 Batch Relink，但未形成完整依赖图、使用位置、替换源、Consolidate/Transcode/Project Archive | 建立 Asset→Clip→Timeline→Export 的依赖索引；提供 Reveal in Timeline、Replace Media、Consolidate/Collect、复制工程并校验哈希 | 替换素材不丢剪辑参数；收集工程可在另一台机器打开；缺失素材报告包含所有使用位置 |
| P1-14 | 代理和渲染缓存缺少统一的生产策略 | 已有 Proxy/Render Cache 和预览降级；仍需统一生成、绑定、失效、版本、磁盘配额和“当前实际使用哪一层”的诊断 | 建立项目级 Proxy Policy、缓存版本/hash、自动生成队列、断点恢复、清理策略和播放 HUD；禁止缓存命中错误素材 | 代理/原片切换不改时间码和色彩；素材/效果/工程 revision 变更时只失效必要缓存；缓存可诊断和可重建 |
| P1-15 | 混合帧率、VFR 和音视频时间基准尚未形成统一契约 | 时间线帧率、Source FPS、音频 sample rate 等字段存在，但跨 23.976/24/25/29.97/59.94、VFR、变速、字幕和录音的端到端契约仍不足 | 统一 Rational FPS、FrameTime、SampleTime 和 PTS/DTS；明确 timeline/master/source timebase，所有模块禁止自行 round | 变速、VFR、字幕、波形、导出、Cue Sheet 在长时长项目中不漂移；帧与样本边界有独立参考测试 |
| P1-16 | FCPXML/OTIO 适配器存在，但明确 dormant；AAF/OMF 仍不可用 | adapters/fcpxml_adapter.py、otio_adapter.py 和 core/interchange.py 都显示受控子集/暂无生产入口；能力表将 FCPXML/AAF/OMF 标为不可用或评估 | 先把 FCPXML 或 OTIO 的受控子集接入导入/导出菜单，再提供 mapping report、unsupported field report、媒体路径策略和 round-trip；根据用户群决定 AAF 或 OMF | 与 Resolve/Premiere/FCP/OTIO 真实样例双向交换；不支持的效果、速度、字幕和路由被明确列出，不静默丢失 |
| P1-17 | 互操作能力声明与实际实现存在分裂 | interchange.py 的 BWF capability 标 available=True，但 is_true_bwf_supported() 返回 False；多个模块采用 dormant 头注释 | 建立机器可生成的 Capability Registry，由测试结果决定 available/partial/unverified/dormant；UI、文档、导出菜单只读取同一来源 | 任何用户可见的“支持”都能指向测试证据；状态变更不会出现“代码写了但菜单不可用”或“菜单声称已支持但实际降级” |
| P1-18 | 导出虽然有 preflight，但时间线交付仍缺更完整的 manifest 与可复现性 | timeline_export.py 已有音频、视频效果、字体和字幕图形 preflight；批队列有持久化/暂停/恢复基础 | 为每个时间线导出保存工程 revision、源媒体 hash、代理/缓存选择、效果 capability、字体、音频路由、时间码和警告 manifest；支持按片段 checkpoint 和重试 | 导出报告能回答“使用了什么源、什么版本、哪些地方降级”；同一 revision 可复现；取消后重试不会产生半成品被误认成功 |
| P1-19 | 项目级交付检查还缺少统一的广播/平台质量门 | 有响度、字幕字体、效果和路由的一部分 preflight，但缺少统一视频色域/峰值/安全框、帧率/分辨率、黑帧、静帧、音画长度和文件完整性检查 | 建立 Delivery QC：色彩空间/合法范围/峰值、响度/LRA/True Peak、静帧/黑帧、字幕越界、音画时长、文件 hash 和容器 metadata | 导出前能得到 blocking/warning/degraded 分类；报告可保存并随交付文件归档 |
| P1-20 | 轨道级和剪辑级自动化虽然有模型，但实时/离线/编辑语义需要统一 | TimelineSequence 和 AudioGraphSnapshot 都有 automation 字段，部分 path 已实现；但统一编译、边界、采样精度和编辑操作迁移仍需系统验证 | 让自动化属于明确的 timebase；trim/move/ripple/retime 时定义 keyframe 是否随 clip、随 timeline 或保持绝对位置；同一自动化 evaluator 供 preview/export | 编辑后关键帧位置符合选择的语义；block 边界不跳变；实时/导出曲线和峰值一致 |
| P1-21 | 字幕专业交付格式和字体可移植性不足 | 当前有 SRT、SubtitleClip、样式和字体 preflight；但没有形成 CEA-608/708、TTML/IMSC、EBU STL 等广播字幕交付矩阵 | 分离“字幕数据轨”和“烧录图形轨”；实现高价值格式、语言轨、RTL、safe area、字体包/许可证报告和 style mapping | 目标平台导入后时间码、换行、位置、样式和语言轨正确；缺字体时阻断或有明确替代记录 |
| P1-22 | Effect/Adjustment Clip 的高级能力和失败回滚仍需收口 | create_adjustment_clip 和效果面板存在；最新审计仍记录 HDR surface、完整像素 parity、legacy migration/compile-status、generator 失败回滚等部分完成项 | 为 Adjustment Clip、Generator、Effect Chain 增加 typed capability、事务性创建/删除、编译状态、失败回滚和缓存依赖 | 任何创建/应用/导出失败都不留下半个轨道、孤儿 asset 或错误缓存；编辑/导出状态一致 |
| P1-23 | 预览性能有控制项，但缺少可量化的 4K/长时间线 QoS | 有丢帧开关、队列深度、预热、placeholder、proxy 和缓存路径；但缺少统一的 dropped frame、decode latency、GPU time、cache hit、memory HUD 和报告 | 建立 realtime performance recorder；区分 decode、upload、effect、composite、audio、present；按项目/设备输出基准 | 4K 目标素材在定义场景下有明确 FPS、首帧、seek、内存和掉帧阈值；低于阈值给出原因而不是只显示卡顿 |
| P1-24 | 页面是多个 Dock 的信号中心，功能发现和生命周期边界复杂 | edit_page.py 自身是不可见的协调容器，真实 UI 分布在多个 Dock；这利于解耦，但会造成选择、播放、关闭、切换页面时的状态边界 | 增加 Edit workspace preset（Editing/Audio/Captions/Review）、统一 selection/playhead/session context、Dock 生命周期 contract 和页面级状态恢复 | 任意 Dock 隐藏/重开、切换产品页、保存/恢复布局后，选择、时间线、播放和未保存状态不丢失 |

## 6. P2：专业效率、可维护性和体验优化

| 编号 | 优化项 | 为什么需要 | 计划方向 |
|---|---|---|---|
| P2-01 | 快捷键和鼠标语义可配置 | 专业剪辑高度依赖键盘；固定快捷键无法适应 Premiere/Avid/FCP 用户 | 建立 schema 化 Shortcut Map、冲突检测、按 workspace 配置、导入/导出预设和命令搜索 |
| P2-02 | 命令事务和多选择编辑 | 链接 A/V、字幕、标记、效果和轨道属性需要一次 Undo；分散信号容易产生半完成状态 | 所有用户动作使用 Macro/Compound Command，提交前验证，失败整体回滚；Undo history 显示人类可读操作 |
| P2-03 | 选择、链接和同步状态缺少统一可视诊断 | 当前有 linked selection、target track、hover group 等机制，但复杂操作需要知道“为什么这个剪辑会被带动” | 增加 Selection Inspector、同步锁/链接/目标轨道可视 badge、受影响对象预览 |
| P2-04 | 时间线显示密度和信息检索仍可提升 | 长时间线中缩略图、波形、关键帧、标记、字幕和效果信息竞争空间 | 提供轨道显示预设、clip metadata overlays、波形/缩略图密度、自适应标签、片段颜色规则和局部渲染 |
| P2-05 | 标记系统应升级为评审和剪辑日志 | 当前有 timeline/clip marker 和名称/备注；专业协作还需要状态、作者、颜色、范围和评审回复 | Marker 类型、作者/时间、状态、评论线程、导出 CSV/PDF/Review package，并与 AI/字幕/场景切割互通 |
| P2-06 | 素材缩略图、胶片带和波形缓存需要统一生命周期 | 目前多个 provider/extractor/cache 路径并存，长项目容易产生失效和资源回收边界 | 统一 cache key、revision、取消、失败重试、磁盘配额和窗口关闭释放；波形和缩略图复用媒体代理 |
| P2-07 | 插件/效果 SDK 需要版本化能力合同 | 当前效果注册、预览、导出、音频 DSP 和 GPU capability 分散 | 定义 EffectManifest、参数 schema、GPU/CPU/HDR/Audio/Export capability、版本迁移、sandbox/超时和错误报告 |
| P2-08 | 色彩管理与剪辑交付的边界需要可视化 | Topos Color 和 Edit 已有桥接，但剪辑用户需要知道素材/时间线/显示器/导出的色域和 HDR 变换 | 在 Program Viewer、项目设置、导出和 Media Pool 显示 Input/Timeline/Output color space、HDR metadata 和 tone mapping 状态 |
| P2-09 | 多媒体拖放需要明确的插入语义 | 当前媒体池和源监看器可拖放/插入/覆盖，但复杂目标轨和链接音频需要可预览 | 拖放前显示 V/A 落点、覆盖范围、handles、锁定/同步锁影响；没有合法落点时解释原因 |
| P2-10 | 搜索、帮助和诊断需要直接连到状态对象 | 当前有 I18N、状态 badge、placeholder 和日志；但用户仍可能不知道失败来自素材、解码、GPU、效果或插件 | 统一 Diagnostic Center，把错误关联到 asset/clip/track/effect/render job，并支持复制报告 |
| P2-11 | 关闭窗口和取消任务的资源释放还需系统化 | Qt、ModernGL、音频设备、波形线程、thumbnail worker、解码器和缓存都有异步生命周期 | 引入 PageSession/TaskRegistry，所有任务可 cancel/join/release；在退出和切换页面进行资源审计 |
| P2-12 | 端到端性能和稳定性应持续回归 | 定向纯模型测试不能覆盖原生库和异步线程 | 建立 nightly 媒体矩阵、随机编辑序列、长时 soak、GPU/CPU fallback、设备拔插和 crash artifact 收集 |
| P2-13 | 工程格式需要版本迁移和向后兼容报告 | 不可变模型字段持续增加；效果、字幕、代理、嵌套和 audio graph 的 schema 变化会影响旧工程 | 所有工程 manifest 带 schema/version/migration log；打开旧工程先 dry-run，显示迁移和降级项 |
| P2-14 | 页面可访问性和国际化还需覆盖动态控件 | 剪辑面板控件多、颜色状态多，键盘焦点和读屏容易遗漏 | 统一 objectName、tooltip、shortcut、焦点顺序、明暗主题、色盲安全状态和中英文完整覆盖 |
| P2-15 | 代码结构需要持续拆分超大 UI 文件 | edit_page.py、timeline_view.py、timeline_items.py 和预览渲染器体量很大，修改风险高 | 按 Selection/Playback/Audio/Timeline Edit/Export/Diagnostics 拆分 controller/service；保持信号契约和强类型 |

## 7. P3：长期竞争力与生态能力

| 编号 | 方向 | 目标 |
|---|---|---|
| P3-01 | 脚本化和命令行交付 | 让项目打开、代理生成、批量转码、导出、QC 和报告可以无界面执行，并复用同一命令/模型 |
| P3-02 | 团队协作和评审 | 工程版本、只读锁、评论、标记、Review Link、变更 diff、代理共享和冲突提示 |
| P3-03 | 外部控制协议 | 为 MIDI/Keyboard、控制面板、远程播放和自动化提供稳定命令 ID，而不是绑定 Qt slot |
| P3-04 | 高级音频制作 | Surround/immersive routing、sidechain、响度历史、ADM/BWF 完整交付、ADR session 和 DAW round-trip |
| P3-05 | 高级视频制作 | Multicam 深度同步、代理分辨率策略、光流/帧混合、运动模糊、稳定、跟踪、肤色和更完整的动态图形 |
| P3-06 | 外部插件和交换生态 | OFX/VST3/FFmpeg capability、FCPXML/AAF/OTIO 高质量映射、插件参数迁移和第三方验证矩阵 |
| P3-07 | AI 编辑的生产可靠性 | 语音/转录/场景/镜头选择/文本剪辑必须输出可审计 EditPlan，支持预览、Diff、局部应用、撤销和真实 provider 能力状态 |

## 8. 推荐实施顺序

### 阶段 0：先建立正确性与发布门

对应：P0-01 至 P0-08。

1. 建立统一时间基准和 drop-frame 参考实现。
2. 修复已有目标轨道的跨轨移动，并补链接/标记/锁轨/冲突/Undo。
3. 将音频 snapshot 校验和 compiled graph 接入实时、导出、Stem。
4. 实现真正 BWF 或诚实降级为 WAV with INFO；同时把录音改成分块写盘。
5. 统一 Edit 预览与导出的 HDR/effect capability 和失败状态。
6. 建立分进程 GUI/GPU/音频设备 smoke、soak 和故障注入测试。

退出条件：P0 全部关闭；任何错误结果都有阻断或可见降级；不存在常规用户路径触发的 NotImplementedError。

### 阶段 1：补齐日常专业剪辑动作

对应：P1-03 至 P1-07、P1-11、P1-20、P2-01 至 P2-03。

1. Lift/Extract、Trim Monitor、Dynamic Trim、Source Patching、Sync Lock 和轨道语义。
2. 转场双击、参数应用、handles 校验和音频/视频一致性。
3. 场景切割严格使用 timeline clip source range。
4. 完善 Macro Command、选择预览和统一诊断。

退出条件：一名熟悉专业 NLE 的用户可以只用键盘、鼠标和监看器完成导入、粗剪、精剪、转场、标记、字幕和撤销，不需要绕过 UI 或编辑工程文件。

### 阶段 2：音频、媒体和交付生产化

对应：P1-08 至 P1-19、P2-05 至 P2-13。

1. 保持音调变速、明确通道布局、输入 patch、监听和 ADR。
2. 关键词/评级/标记/使用关系/Consolidate/Archive。
3. 代理和缓存策略、Capability Registry、FCPXML 或 OTIO 生产入口。
4. Manifest、QC、队列断点、外部 NLE/DAW 导入验收。

退出条件：工程可在媒体断开、代理切换、跨机器收集、外部交付和重开后保持可追溯；导出报告能够说明所有降级和来源。

### 阶段 3：高级编辑和高端工作流

对应：P1-01、P1-02、P1-21、P1-23、P1-24、P3 全部。

1. 多机位创建、同步、Angle Viewer 和实时切换。
2. Nested/Compound Clip 创建、进入、展开、依赖和递归缓存。
3. 专业字幕标准、HDR/QC、workspace preset、性能 HUD。
4. 脚本化、协作、外部控制和生态插件。

退出条件：复杂多机位、嵌套、HDR、字幕交付和团队评审场景都拥有真实素材、可重复脚本和外部软件验收证据。

## 9. 最小验收测试矩阵

| 场景 | 必须验证的内容 |
|---|---|
| 新建工程 | 时间线 FPS、分辨率、采样率、色彩空间、默认轨道和保存/重开 |
| 媒体整理 | 导入、Bin/Smart Bin、关键词/评级、离线、重链接、使用位置 |
| Source 编辑 | In/Out、三点编辑、源音频、素材范围、变速、音画链接 |
| 基本精剪 | Split、Trim、Roll、Slip、Slide、Ripple、Lift、Extract、Undo/Redo |
| 轨道语义 | Track Target、Source Patch、Sync Lock、锁定、覆盖冲突、跨轨移动 |
| 预览 | GPU/CPU/Proxy、首帧、seek、4K、掉帧、HDR、效果状态和字幕 |
| 音频 | Clip speed/fade、音量/声像、EQ/FX、Bus/Send、自动化、Mute/Solo、通道布局 |
| 录音 | 设备失败、监听、pre-roll、loop take、BWF 时间参考、停止/丢弃/重开 |
| 高级编辑 | Multicam、Nested/Compound、场景切割、调整片段和生成器失败回滚 |
| 导出 | In/Out/Selected、字幕、字体、响度、视频效果、音频路由、QC、取消/恢复 |
| 互操作 | FCPXML/OTIO/CSV/BWF/Stem 导入导出、时间码、媒体路径和 unsupported report |
| 生命周期 | 切换页面、隐藏/重开 Dock、取消任务、关闭窗口、工程恢复、缓存释放 |

每项测试都应记录：工程 schema/version、输入媒体 hash、时间线 revision、使用的代理/缓存、GPU/CPU 路径、输出 hash、警告/阻断信息和可复现命令。

## 10. 不应重复建设的现有基础

以下内容已经有可复用基础，后续应做接线、统一和验收，不应再次创建平行系统：

- 不可变 TimelineSequence、ClipInstance、EditTrack、TransitionInstance 和 SubtitleClip 模型；
- 现有 timeline command/operation、Undo/Redo、link/unlink、marker、trim、ripple 和三点编辑入口；
- Source Viewer / Program Viewer 的播放与时间线控制；
- Media Pool、Bin、Smart Bin、离线/重链接、缩略图和波形 extractor；
- AudioGraphSnapshot、AudioGraphRenderer、音频 DSP、Stem、响度和 export preflight；
- Proxy/Render Cache、字幕 workspace、场景切割、调整片段、生成器和 AI EditPlan 基础；
- Deliver 批队列的持久化、暂停、恢复和失败状态；
- FCPXML/OTIO/multicam/nested sequence 的纯模型或适配器代码。

真正的优先级是把这些基础接入统一的生产入口，清除 dormant 状态，保证同一个 revision 在实时预览、编辑、音频、导出和重新打开后具有同一语义。

## 11. 本轮验证记录

执行命令：

python3 -m pytest -q -p no:cacheprovider tests/edit/test_edit_project.py tests/edit/test_edit_project_initialization.py tests/edit/test_timeline_ops.py tests/edit/test_timeline_render_plan.py tests/edit/test_timeline_frame_mapper.py tests/edit/test_timeline_ripple_delete.py tests/edit/test_timeline_link_integrity.py tests/edit/test_timeline_export.py tests/edit/test_multicam.py tests/edit/test_nested_sequence.py tests/edit/test_interchange.py tests/edit/test_timeline_command_regressions.py

结果：**326 passed, 5 skipped，耗时约 2.79 秒**。

限制：本轮没有把完整 Qt/ModernGL/GPU/真实设备/外部 NLE 交换作为通过项；上述限制已经纳入 P0-08 和后续验收矩阵。
