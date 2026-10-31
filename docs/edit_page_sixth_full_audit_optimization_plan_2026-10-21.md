# 剪辑功能第六轮真实交互审计与优化计划表

日期：2026-10-21（Asia/Shanghai）  
基线：`439a8cc15f40cdaa19edd86410399c0210d97089`，分支 `fix-ui`  
范围：Topos Studio 中的 Edit、相关 Color/Deliver 接口、音频、字幕、工程与交换能力。  
执行环境与入口：[environment.json](audit/2026-10-21-edit-full-review/records/environment.json)。

本轮仅新增本计划、诊断脚本、截图和测试证据。**未修改产品代码、既有测试、用户工程或测试视频。所有计划事项均待处理，本报告不代表已经修复。** 第五轮计划和修复记录已经核对；没有把其历史问题整体重新列为未修复。

## 1. 结论与处理顺序

当前基础剪辑链路可以工作，真实窗口成功导入九种媒体、进行源片段选择和时间线编辑、添加效果及效果关键帧、创建多机位和嵌套序列、操作字幕、执行场景检测、保存并重新打开工程。通过真实 Deliver 按钮完成了 **300 帧、640×360、25fps、H.264＋AAC** 导出，抽取的画面有正常内容。

但“测试通过”仍不能证明完整宿主链路可靠。本轮整理出 **26 项优化事项：P1 11 项、P2 11 项、P3 4 项**，包含实现错误、交互/交付故障及三项质量门禁/能力声明问题；另列 **17 项专业能力建设计划**。有些缺陷只在生产模型/渲染函数中复现，尚未完成对应鼠标路径，表中已明确证据等级。

建议首先处理：

1. **U01–U04：交付和播放头主链路。** 自定义范围导出误判零帧、嵌套导出缺少子序列解析、非当前时间线导出拿到轻量引用、逐帧/回到开头只改变控制器而不更新可见播放头。
2. **D01、D02、D05、D08：输出正确性。** 音频变速映射错误、立体声归一化错误、Stem 与成片不一致、ASS 字幕文本丢失。
3. **C02–C04、C06：编辑完整性。** 跨轨动画/转场和 Undo 快照、锁轨约束。
4. 再完善实时保调、Source Patching、专业修剪监看、多机位与 AAF 等能力。先保证已有能力产生正确结果，再扩展专业工作流。

P1＝核心操作阻断、内容错误或编辑状态损失；P2＝局部功能、生命周期、一致性或明显体验问题；P3＝文案、未接线契约或维护一致性。未观察到用户原始素材被改写或既有工程被破坏。

## 2. 测试方式、证据和边界

| 等级 | 本轮含义 |
|---|---|
| G | 生产 `src.main.main()` 启动的真实 `MainWindow`，屏幕可见，QTest 鼠标/键盘、真实按钮/对话框、实际解码/渲染/导出；部分场景以生产信号入口准备状态，具体注明 |
| H | 同一个真实窗口中调用生产 handler/service，再观察状态或点击控件；不能等同完整手工鼠标路径 |
| M-real | 生产领域模型/音频渲染器处理 `tests/test_videos` 中真实素材，检查 PCM 等输出 |
| M-fixture | 生产模型/命令或渲染器处理补充合成媒体；若 decoder 是受控替身，明确注明 |
| S / T | 源码调用链核查 / 本轮执行的既有测试 |

系统级桌面鼠标控制不可用，因此使用真实 Qt 窗口和 Qt 输入事件进行交互测试。主窗口使用正常生产入口、Studio 模式、1440×1000，**未使用 offscreen 或渲染器替身**；`TOPOS_TEST_MODE=1` 只用于禁止更新检查。`pytest` 批次另以 offscreen 运行，不能混为可见窗口验收。

隔离配置和临时工程位于 `/tmp/topos-edit-audit-20261021/`。输入媒体只读。补充 ASS/SRT、正弦音频和反相立体声位于临时目录，不冒充用户提供的真实语料。测试中先后修正过脚本字段名、按钮名和命令投递的原子写入问题；相应失败记录保留，**不计作产品缺陷**。详见 [证据说明与重放方法](audit/2026-10-21-edit-full-review/README.md)。

### 测试结果

| 批次 | 当前结果 | 使用口径 |
|---|---|---|
| `tests/edit` 全目录，619 个文件，31 个独立进程批次 | **10,672 passed、20 skipped、0 failed / error** | 每个文件分配一次；[批次清单](audit/2026-10-21-edit-full-review/regression/summary.json)及各批 XML/日志可核对 |
| 核心专项 26 文件 | 614 passed | 与全目录有重叠，不加到独立用例数；[核心专项报告](audit/2026-10-21-edit-full-review/specialists/topos_edit_core_audit_20261021.md) |
| Deliver 全目录＋音频/字幕/效果定向批次 | **330 passed、1 failed** | 其中包含与 Edit 全目录重复的用例；失败为测试夹具缺 `fps`，见 Q02；[日志](audit/2026-10-21-edit-full-review/logs/topos_delivery_pytest_20261021.log) |
| 单进程全量尝试两次 | 都在约 64% 处卡住，主动终止后改为分批 | 不能计为全量通过；faulthandler 定位到 `FFmpegSource.__del__ → close → container.close`，见 Q01 |
| 真实交付队列 | 普通成片成功；嵌套、非当前序列两次导出失败 | 不是“导出整体通过”；见 U01–U03 |

20 个跳过项的原因保存在 [skipped_tests.json](audit/2026-10-21-edit-full-review/records/skipped_tests.json)：包括缺少 OTIO 可选依赖、媒体文件名与现有文件不一致、GUI 测试显式跳过、已知 x265 10-bit 原生崩溃用例等。没有强行启用已知会击穿进程的用例，也没有把跳过当通过。

### 真实媒体覆盖

本轮实际导入、媒体池双击打开 Source、前后逐帧及跳至第100帧，并保存逐素材截图：

- `2K_h264.mp4`、`2K_H265.mov`、`2K_prores422.mov`
- `4K_H264.mov`、`4K_H265.mov`、`4K_prores422Proxy.mov`
- `2K_DNxHR HQX 10bit.mov`、`2K_topos_422_LT.mov`、`2K_Prores4444_Alpha.mov`

九种素材均建立 Source 资产和帧位状态；切源时出现 GPU 错误及释放告警，故不能将整个 Source 链路标为无问题，见 U06。[逐素材记录](audit/2026-10-21-edit-full-review/records/016_media_matrix.json)。BRAW、R3D、长相机原片、其余 ProRes/DNxHR 档位没有逐个 GUI 验收。

## 3. 功能检查矩阵

| 功能 | 本轮实际检查及结果 | 证据/限制 |
|---|---|---|
| 启动、页面装配 | 生产入口启动，Edit/Source/Program/Properties/Deliver 可见 | G；[启动](audit/2026-10-21-edit-full-review/records/startup.json) |
| 新工程、导入 | 隔离新工程，文件 URL 的真实 drag/drop 事件，9 素材异步导入 | G；[导入](audit/2026-10-21-edit-full-review/records/005_import.json) |
| 新时间线设置 | 实际新建对话框确认25fps、1080p、48kHz并建立V1/A1 | G；[新建](audit/2026-10-21-edit-full-review/records/018_new_sequence.json) |
| 媒体池浏览、Source | 9 素材 grid 双击、逐帧、seek、缩略图；切源异常见 U06 | G；[素材矩阵](audit/2026-10-21-edit-full-review/records/016_media_matrix.json) |
| Source 入出点 | 点 I/O 按钮建立50–149，100帧范围；Clear 后三个源编辑按钮禁用 | G；[源编辑](audit/2026-10-21-edit-full-review/records/010_source_edits.json)、[基础操作](audit/2026-10-21-edit-full-review/records/012_basic_interaction.json) |
| Insert/Overwrite/Append | 实际工具栏点击，生成链接 A/V、插入/覆盖/末尾追加 | G；上述记录含逐轨起止/链接信息 |
| 分割、Undo/Redo | 键盘分割增加链接A/V片段、撤销重做；发现程序化 seek 与可见播放头不同步 | G/H；[修剪/分割](audit/2026-10-21-edit-full-review/records/017_trim_tools.json)，U04 |
| Program 播放 | 实际播放按钮，约2.3秒推进至59帧，有正常图像 | G；初次观察器重复连接导致 frame_ready 计数翻倍，**不把118次发射当118个独立画面** |
| 尺规拖/点击、逐帧、Home | 尺规可同步；Program Next/Home 失同步 | G；[独立复验](audit/2026-10-21-edit-full-review/records/037_transport_fresh_timeline.json)，U04 |
| Trim、链接A/V | 实际拖右边缘，V/A均由50缩至42帧，撤销恢复 | G；[017](audit/2026-10-21-edit-full-review/records/017_trim_tools.json) |
| Roll/Slip/Slide | 实际工具栏＋拖动，模型起止/源点变化及Undo检查 | G/H；[021](audit/2026-10-21-edit-full-review/records/021_advanced_mouse_tools.json)；不是全组合验收 |
| Ripple、Rate Stretch | 切换工具成功；本次鼠标拖动未形成可确认的有效变化 | G仅入口；正确性主要依赖 T/M，未计“完整GUI通过”，需补独立夹具重测 |
| 复制粘贴、Lift/Extract、吸附、框选 | 菜单装配/快捷键与既有模型测试 | S/T；未逐项做完整鼠标流程 |
| 普通属性 | 真实内联输入 X=-250，模型为-250，Undo为0 | G；[属性和效果](audit/2026-10-21-edit-full-review/records/014_fx_and_transform.json) |
| 视频效果和◆ | Blur卡片双击添加，◆建立frame10关键帧；锁轨后frame20仍可加入 | G/H；[锁轨](audit/2026-10-21-edit-full-review/records/015_effect_keyframe_locked.json)，C06 |
| 普通Transform动画 | 无普通参数◆，不是“已点通”；底层值域错误另做 M 复现 | G/S/M；C01、G16 |
| 变速/倒放/冻结 | 模型/命令和真实音频渲染覆盖；导出映射错误 | M/T；C04/C05、D01；未把所有变速控件称GUI验收 |
| 标记、链接选择、循环 | 标记命令建立、浮动列表复验可见，链接开关/loop切换 | G/H；[菜单标记](audit/2026-10-21-edit-full-review/records/036_history_menus.json)、[复验](audit/2026-10-21-edit-full-review/records/037_transport_fresh_timeline.json) |
| 菜单、错误反馈 | 历史缺失的时间线动作现在存在；Undo可用，status信号有1个接收者 | G/S；[036](audit/2026-10-21-edit-full-review/records/036_history_menus.json)，不重报E03/E04/E11 |
| 多机位 | 媒体池生产请求创建2机位组，真实数字键2切角、Undo切角和创建 | H＋G；[024](audit/2026-10-21-edit-full-review/records/024_multicam_nested.json)；未验证9路长时同步 |
| 嵌套 | 上下文dispatcher建子序列、进入/返回、Undo删除子序列成功 | H/G；同上；交付失败见U02 |
| 字幕导入 | SRT/ASS素材实际拖入时间线，SRT保留“Hello, world”，ASS只剩“world” | G；[020](audit/2026-10-21-edit-full-review/records/020_import_captions.json)，D08 |
| 字幕工作区 | Find选中、Replace All、Split、Undo；Find未定位画面 | G；[022](audit/2026-10-21-edit-full-review/records/022_caption_actions.json)、[035](audit/2026-10-21-edit-full-review/records/035_find_and_seek_sync.json)，U05 |
| 字幕多格式/QC | ASS往返、TTML时间基准探针，其他格式既有测试 | M/T；D08/D09；未宣称广播合规已验收 |
| 场景检测 | 真2K ProRes，实际“自动场景检测”按钮，487帧分析完成、2候选、无错误 | G；[042](audit/2026-10-21-edit-full-review/records/042_scene_results.json)；候选准确率未做标注对照 |
| 音频混音、自动化、路由 | 生产DSP/模型、相关回归，真实素材PCM；未完成声卡监听/硬件回环 | M/T；D01–D07 |
| 归一化UI响应 | GUI线程生产分析真实60秒音频，0.982秒内20ms心跳0次 | H；[025](audit/2026-10-21-edit-full-review/records/025_normalize_ui_responsiveness.json)，D03 |
| 保存/重开 | 真Save As对话框生成.topos，随后生产open path重建12素材、3时间线、活动序列500帧一致 | G/H；[保存](audit/2026-10-21-edit-full-review/records/031_save_project.json)、[重开](audit/2026-10-21-edit-full-review/records/043_save_reopen.json) |
| 普通队列导出 | 实际“加入队列并开始”按钮，300帧视频＋AAC；抽取0/150/299帧查看 | G；[队列](audit/2026-10-21-edit-full-review/records/030_export_run.json)、[ffprobe](audit/2026-10-21-edit-full-review/records/gui_export_ffprobe.json) |
| 自定义/嵌套/多时间线交付 | 三种实际用户路径均发现阻断 | G；U01/U02/U03 |
| 取消导出 | 生产队列取消入口，结果单独记录 | H；[044](audit/2026-10-21-edit-full-review/records/044_export_cancel.json)，不等同D07资源清理已通过 |
| 关闭应用 | 保存隔离工程后请求正常关窗，进程退出码0；日志报告GPU线程2000ms未退出、推迟回收 | G/H；[关闭请求](audit/2026-10-21-edit-full-review/records/045_close_requested.json)、[退出](audit/2026-10-21-edit-full-review/records/process_exit.json)，归入U06生命周期问题，不能称清理完全通过 |
| 代理、离线重连、归档、FCPXML | 源码/既有回归，未移动用户原片或完成外部软件往返 | S/T；见G09和后续验收清单 |
| AI助剪 | 隔离配置下无模型profile，部分本地模型包未安装 | 未做真实付费模型/云调用；不是功能缺失的直接证据 |

## 4. 问题优化计划总表

下列全部为“未开始”。C/D的完整代码位置、复现参数和输出见 [核心专项](audit/2026-10-21-edit-full-review/specialists/topos_edit_core_audit_20261021.md)及[音频/字幕/交付专项](audit/2026-10-21-edit-full-review/specialists/topos_edit_delivery_audit_20261021.md)。这里按可执行修复单元列出，避免将同一音频时间映射错误拆成多个重复数量。

| ID | 优先级/证据 | 问题与影响 | 优化工作 | 验收要求 |
|---|---|---|---|---|
| C01 | P2，M/S | Transform动画值域错误：X=-250被求值为0，Y=500变100，rotation=-45变0/180变100。普通UI尚未暴露◆。 | `commands.py:3697`、Remove.undo统一使用实际参数schema，纠正position/translate命名和默认值域。 | add/sample/remove/undo/save-load保持负位移、大位移、角度；启用G16前必须修好。 |
| C02 | P1，M/S | 拖到新建轨时动画不随片段时间移动：100→300后key仍100/199，片头opacity由0变1。 | 跨轨命令复用`with_temporal_shift`，覆盖旧existing-track分支。 | clip＋effect曲线局部时域保持；单片/链接组/多选、新轨/已有轨、Undo/Redo一致。 |
| C03 | P1，M/S | 新建轨移动删除片段转场，Undo仍丢失：转场数1→0→0。 | 保存/恢复源目标轨转场快照；定义单边迁移、双边分离策略。 | head/tail/双边转场Undo后逐字段恢复，不改未选邻片。 |
| C04 | P1，M/S | 链接A/V变速后Undo只恢复音频片段长度，不恢复音频转场：80→50→Undo50。 | `ChangeClipSpeedCommand`在修改任何轨前获取linked音频转场快照。 | 选V/选A、缩短/放大、单/双边转场全部原子撤销。 |
| C05 | P2，M/S | Freeze手工分割未迁移clip marker/尾转场；marker75落在已缩至[0,50)的原clip，尾淡出错误移到切点。 | 复用Split的归属迁移事务，再给右段附freeze。 | validation无越界；淡出仍在最终结尾；markers、Undo/Redo及变速源映射正确。 |
| C06 | P2，G/H/M/S | 锁轨仍能点击效果◆写入内容：锁后revision20→21，新增frame20关键帧。 | 所有keyframe/effect写命令统一轨锁守卫，控件同步禁用/解释拒绝。 | 锁轨下新增/删除/启用/重排/动画都零提交；已有合法历史可正常Undo。 |
| D01 | P1，M-real/M-fixture | 倒放/速度曲线音频与普通PCM完全相同；未保调2x的440Hz正弦应880却约430Hz。 | 成片/预览/Stem共享连续源时间映射和样本域resample/reverse/stretch；不支持的组合先明确拒绝。 | 0.5x/2x/reverse/ramp、非零入点、trim/split后音画对应；未保调/保调频率与时长各自正确。 |
| D02 | P1，M-fixture | 归一化先`-ac 1`，反相立体声抵消后建议+47dB，UI夹到+12dB；同相测量也错约3dB。 | 保留实际声道布局分析BS.1770/true peak；低于门限不套用静音floor放大。 | 同相/反相/不相关stereo、mono/5.1对照，实际输出达到目标且各声道峰值不超限。 |
| D03 | P2，H-real/S | 分析按钮同步解码/DSP阻塞UI；真60秒素材0.982秒内无心跳，取消无事件机会。 | 可取消worker、流式解码/分析、终止子进程及丢弃过时结果。 | 长素材分析时UI/取消响应<200ms，失败/取消零历史提交并释放资源。 |
| D04 | P2，M-fixture/S | 音频片段FX的◆能保存，但预览/导出只读静态params。Limiter threshold关键帧-20仍输出0.5，静态-20应0.1。 | 在统一音频图中求值animated_params并平滑更新DSP；未支持前禁用对应◆。 | limiter/EQ/compressor动画真实改变PCM，预览/成片/Stem一致。 |
| D05 | P1，M-real/M-fixture | Stem未传clip FX且未处理冻结静音：真实素材冻结成片peak0，Master stem仍0.10838。 | 共享完整clip audio graph，补FX/freeze/crossfade/retime；避免简化副本。 | 同范围Master stem和成片解码PCM一致；Track/Bus/DME等价solo。 |
| D06 | P2，M-fixture | 非整数FPS固定floor块长留下采样洞：300帧29.97fps有180个、59.94有240个额外零样本/声道。 | 每帧块长使用精确相邻样本边界差，成片和Stem同源。 | NTSC长短序列恒定输入无额外零洞、正弦连续、总样本数准确。未做耳听，不能称已听见爆音。 |
| D07 | P2，M-fixture | 音频渲染取消提前return，绕过显式decoder.close。 | 用finally/ExitStack统一成功/取消/异常清理，stretch预解码同样检查取消。 | 每个opened decoder明确close一次，句柄/线程及时归零；不把GC兜底当生命周期保证。 |
| D08 | P1，G/M | ASS导入逗号截字，自导出再导入也损失；真实拖入“Hello, world”只剩“world”。 | 按Events Format列数切分，标准ASS maxsplit=9；Text保持全部逗号/标签/换行。 | 多个ASCII逗号、中英文、标签、SSA变体及本项目往返文本逐字一致。 |
| D09 | P2，M/S | TTML忽略tickRate/frameRate：24fps下10t→20t本应24起24帧，却0起1帧。 | 先读取文件时间基准，再映射目标时间线；不支持timeBase明确拒绝。 | tick/frame/clock、30→24、frameRateMultiplier、嵌套begin、dur/end参考向量正确。 |
| D10 | P3，M/S | 未生产消费的DeliveryCapability先str(enum)再读value，GIF容器/音频、ProRes4444 alpha报告错误。 | 规范枚举值提取；用真实TimelineExportConfig测契约，再接生产。 | enum/string等价，容器/audio/alpha/HDR与实际encoder一致；不夸大为现有UI故障。 |
| U01 | P1，G/S | 有300帧的时间线，自定义0–50被阻断为“Start frame 0…0 frames”。 | `video_export_settings_panel.py:2910`使用`duration_frames`/统一effective duration，不能缺`frame_count`就当0。 | 真TimelineSequence中Custom、In/Out、字幕-only、nested范围均正确；越界才阻断。 |
| U02 | P1，G/S | 嵌套在Edit可建/可预览，Deliver入队后失败：child sequence resolver unavailable。 | Workspace接收完整序列map；贯通JobContext→Runner→VideoExporter.export(timeline_sequences)。 | 新建嵌套直接从Deliver导出，单/多层及子音频成功；preflight与worker同一resolver。 |
| U03 | P1，G/S | Deliver下拉选非当前时间线，草稿仍是当前序列；controller得到TimelineSequenceRef，导出报无audio_tracks。 | combo选择解析完整目标序列；刷新duration/preflight/draft/queue context，禁止Ref冒充sequence。 | 两条不同长度/内容/声音序列交替选、批量入队、保存重开后输出都对应目标ID。 |
| U04 | P1，G/S | Program下一帧/回开头只更新controller：尺规25后Next控制器26但view/page25，Home控制器0但仍显示25。 | 区分播放tick和手动seek；暂停时frame_changed同步可见播放头、EditPage和预览请求。 | Next/Prev/Home/End/时间码/外部seek后四端同帧，随即Split发生在屏幕所示帧，不重复渲染。 |
| U05 | P2，G/S | 字幕Find能选中命中行，但画面/播放头没有定位至cue开始。历史修复只补emit，宿主仍只做选择。 | 给工作区Find/定位明确seek入口，普通选择是否seek单独定义。 | 从frame250找frame10字幕，命中高亮且画面/播放头/时码同步到10，无信号递归。 |
| U06 | P2，G日志/S待根因 | Source切源出现glUseProgram error1282、WRITING lease强制回收和帧未释放告警；无截图18次仍复现3组。关窗另报GPU线程2000ms未退出。 | 核GL program/context归属和generation，异常路径明确释放当前GPU帧；关闭先停止任务/解码再在线程归属中清理。各现象是否同根因仍待定位。 | 切源无GL/lease告警、长循环显存稳定；正常关窗GPU线程按期退出。未证明永久黑帧或泄漏字节数，退出码0也不代表清理正确。 |
| U07 | P3，G/S | 已实现的链接选择和标记列表仍显示“即将推出/Coming soon”。 | 更新zh_CN/en_US等已有本地化条目；新增功能和文案同步验收。 | 当前功能tooltip描述真实作用，语言切换一致；不改可用功能为disabled。 |
| Q01 | P2，T/原生栈 | 全量同进程两次挂在FFmpegSource析构关闭；分批无失败，说明存在生命周期/测试顺序风险。 | 显式关闭测试与应用decoder；定位container close与worker竞争，增加有超时/栈输出的回归门禁。 | 单进程全量可结束；关闭/切源压力测试稳定。未证明普通用户一定卡死，不能把它直接写成GUI必现挂死。 |
| Q02 | P3，T | Deliver嵌套音频preflight测试的SimpleNamespace子序列缺fps而失败。 | 用完整TimelineSequence夹具，另测真正的缺resolver错误。 | 定向失败用例通过，同时保留真实宿主嵌套导出测试；修夹具不等于修U02。 |
| Q03 | P3，S | 旧interchange表/测试仍宣称FCPXML unavailable，新registry/adapter已部分支持。 | 统一权威能力源和旧helper/测试描述。 | 能力查询均为受控子集可用；当前OTIO缺依赖单独报告，不误关FCPXML。 |

## 5. 关键新交互故障的可复现步骤

### U01：自定义范围导出必定被当作零帧

1. 导入视频，创建有内容的真实时间线。本轮序列实际 `duration_frames=300`。
2. 切换Deliver，选择Custom Frames，Start=0、End=50。
3. 实际面板构建任务返回空列表，preflight显示0帧越界；重新选择同一时间线仍失败。
4. 改成Entire Timeline后同一序列可成功导出300帧。

根因已确认：`VideoExportSettingsPanel._build_preflight_context`读取模型不存在的`frame_count`，默认0。不是片段没有内容，也不是测试媒体导入失败。[准备](audit/2026-10-21-edit-full-review/records/027_export_prepare.json)、[完整上下文](audit/2026-10-21-edit-full-review/records/029_delivery_context.json)。

### U02：嵌套序列导出宿主未传全量序列

1. 选择一对链接V/A，通过生产上下文入口建立嵌套，进入/返回均可。
2. 切Deliver，Entire Timeline，点击“加入队列并开始”。
3. 入队前没有阻断，worker报`child sequence resolver is unavailable`，任务failed。

`page_controller.py:868`尝试调用`deliver_page.set_timeline_sequences`，当前Workspace没有此方法，因callable守卫静默跳过；`DeliverWorkspace.set_project:443–480`给settings的是Ref；Runner调用export时也未带`timeline_sequences`。三层需一并修复。[真实失败](audit/2026-10-21-edit-full-review/records/032_nested_delivery.json)。

### U03：导出下拉菜单选择另一时间线失败

1. 项目内有500帧A、300帧B，当前Program处于B。
2. 在Deliver下拉只选A，无需切Program。
3. `job.timeline_id=A`，但`settings._sequence=B`，controller中A仍是`TimelineSequenceRef`。
4. 点击导出，任务failed：`'TimelineSequenceRef' object has no attribute 'audio_tracks'`。

相关入口：`video_export_settings_panel.py:2302`只改文件名/时长显示，没有切换完整sequence；`video_batch_export_controller.py:264`直接取缓存值作为JobContext。[真实失败](audit/2026-10-21-edit-full-review/records/033_other_timeline_export.json)。

### U04：可见播放头与控制器不一致

在普通500帧时间线用鼠标点尺规25，三端均25。点击Program Next后controller26、view/page25；点击Home后controller0、view/page仍25，等待350–450ms未恢复。`timeline_panel.py:1320`的frame_changed槽只更新时间码，视图只在播放timer.frame_ready更新；stop/step/seek发出的frame_changed不能覆盖暂停导航。[干净序列复验](audit/2026-10-21-edit-full-review/records/037_transport_fresh_timeline.json)。

### D08、C06：真实控件复现

- **ASS**：将同文本SRT和ASS作为素材导入，再各自拖入字幕轨。SRT为`Hello, world`，ASS为`world`；不是字体或显示裁切，模型文本已损失。[记录](audit/2026-10-21-edit-full-review/records/020_import_captions.json)。
- **锁轨效果◆**：选clip→双击Blur→点radius◆得到frame10；经正常锁轨handler锁定V1→点frame20的真实◆，仍写入且revision递增。[记录](audit/2026-10-21-edit-full-review/records/015_effect_keyframe_locked.json)。普通Transform没有◆，没有冒充该入口也已验证。

### U06：排除截图因素后的切源复验

按上述九种媒体顺序执行两轮Source open＋seek，总计18次，循环中不调用widget.grab。仍在`4K_H264.mov`、`2K_Prores4444_Alpha.mov`附近捕获3组GL program错误和GPU帧释放告警。记录中的file取事件发生时当前Source，异步旧任务可能晚到，**尚不能把单个文件编码认定为唯一根因**。[无截图重复实验](audit/2026-10-21-edit-full-review/records/038_source_switch_no_capture.json)。

最后保存隔离工程并正常关窗，终端进程退出码0，但应用日志末尾明确记录“GPU渲染线程未在2000ms内退出，已推迟销毁”。将此观察一并纳入U06，不另重复计数，也不据此断言所有关闭都会永久挂起。[退出记录](audit/2026-10-21-edit-full-review/records/process_exit.json)、[应用日志](audit/2026-10-21-edit-full-review/logs/application.log)。

## 6. 对标专业剪辑软件：已有能力与重要缺口

对标参考官方说明，核查日2026-10-21。官方资料用于定义专业工作流；Topos状态、优先级及验收是本次源码/实测判断。这里的“部分支持”不应改写成“完全没有”。

**当前已经有**基础三点Insert/Overwrite/Append、独立V/A目标轨、Ripple/Roll/Slip/Slide、同步锁轨、多机位组/波形同步/数字键切角、嵌套、代理/重连/归档、音频Bus/Send/automation、BWF/Stem、字幕多格式/QC、FCPXML和OTIO受限适配、自动保存及备份恢复。OTIO在本环境缺可选依赖。已有实现不等于所有边界已可靠，前文错误优先修。

| ID | 优先顺序 | 专业能力 | Topos现状与缺口 | 可执行建设目标及验收 |
|---|---|---|---|---|
| G01 | 第二阶段高 | 逐源流/声道Source Patching | 已有V/A目标轨；缺逐源声道开关和source→target映射。 | 多声道素材只把选中流送A1/A3；video-only/audio-only Insert/Overwrite不带入意外流；单次Undo。对标[Adobe Source Patching](https://helpx.adobe.com/premiere/desktop/edit-projects/intro-to-editing/add-media-to-the-timeline-using-source-patching.html)。 |
| G02 | 第二阶段高 | 高级三/四点、backtime、fit-to-fill | 当前强制源In/Out＋目的播放头；未找到目的Out倒推、目的范围定长和四点冲突入口。 | 可恰好结束于重拍；源In＋目的In/Out定长；冲突明确选择修源/变速，混帧率准确。对标[Apple三点/backtime](https://support.apple.com/en-ca/guide/final-cut-pro/ver549f253e/mac)。 |
| G03 | 第二阶段高 | 双画面Trim Viewer、JKL动态修剪 | 已有基础trim和步进；TrimMonitor仅时间码/Δ/headroom标签。 | 显示切点两侧实际帧、预/后卷循环监听、JKL commit/cancel；±1帧与导出一致。对标[Resolve Edit修剪](https://www.blackmagicdesign.com/products/davinciresolve/edit/)。 |
| G04 | 第二阶段高 | 实时多机位网格、独立音视频切角策略 | 已有多机位剪辑；网格是250ms缩略图请求，没有多路同步实时监看或明确Audio-Follows-Video模式。 | 4/9路同钟监看、切角切点准确；固定主音轨和音频跟随模式各自可控。对标[Resolve多机位](https://www.blackmagicdesign.com/products/davinciresolve/edit/)。 |
| G05 | 第二阶段高 | TC/In/marker同步与长录制漂移 | 当前波形同步主要用开头60秒、固定整数帧offset，搜索约±5秒。 | 无开头音频可用TC/In/marker；长录制检测起末漂移，失败不写错误offset。 |
| G06 | 第二阶段高 | 实时保调变速 | 离线WSOLA已有；实时仍resample变调，UI已披露export only。D01的离线时间映射错误需先修。 | 语音0.5/1.25/2倍预览与导出音高/时长一致，seek/loop重置无爆音。对标[Final Cut Preserve Pitch](https://support.apple.com/en-ae/guide/final-cut-pro/verb6aca45d/mac)。 |
| G07 | 第二阶段高 | 音频子帧/采样级剪辑与fade | decoder可sample-accurate seek，但用户模型起止/fade是整数视频帧。 | 48kHz可移动/修剪1 sample；保存、Undo、Stem精确保留；视频保持帧对齐。 |
| G08 | 第二阶段高 | AAF专业后期交接 | 明确unavailable；已有Stem/BWF/CSV不能替代可重建的时间线交接。 | 优先AAF而非急补OMF；轨位、源时码、handles、声道、音量自动化，真实Pro Tools/NLE导入。对标[Adobe AAF](https://helpx.adobe.com/premiere/desktop/render-and-export/export-files/export-aaf-files.html)。 |
| G09 | 第二阶段中 | FCPXML/OTIO往返与reconform | 已有受限子集；复杂嵌套/曲线变速/multicam/调色/音频路由降级。 | 建立外部软件黄金工程，逐项报告失真；补实际工作流需要的映射和回原片重套。 |
| G10 | 第三阶段 | 文稿直接剪视频 | 转录、字幕、AI计划基础已有；未找到文稿逐词删媒体的完整用户工作区。 | 文稿删除走既有事务，linked A/V、字幕、markers同步；混合源时域和Undo可恢复。对标[Premiere Text-Based Editing](https://helpx.adobe.com/premiere/desktop/edit-projects/edit-video-using-text-based-editing/transcribe-video.html)。 |
| G11 | 第三阶段 | 镜头候选版本/Audition | 未找到通用视频多take容器；ADR试听不等于镜头备选。 | 同位置多候选、不同长度可比较，保存/Undo保留；交换明确flatten。对标[Final Cut Auditions](https://support.apple.com/guide/final-cut-pro/intro-to-auditions-verbbd3587d/mac)。 |
| G12 | 第三阶段 | 命名版本、多人协作、审片评论 | 本地自动保存已有；未找到多人会话/冲突合并/外部审片回流。 | 先命名快照及评论→marker，再共享项目锁/合并；两人同改有明确冲突。对标[Resolve Collaboration](https://www.blackmagicdesign.com/products/davinciresolve/collaboration/)。 |
| G13 | 第二阶段中 | 批量素材属性/解释媒体 | 选多个资产时明确不支持批量Properties；与时间线多选编辑不同。 | 混合值显示、批量元数据/帧率/声道/标签，一次事务或明确逐项失败报告。 |
| G14 | 第二阶段中 | PNG/TIFF等通用图像序列交付 | 已有Topos Image Sequence；PNG/TIFF导出明确拒绝并披露。 | 标准命名/帧号、alpha/位深/色彩，取消和缺帧恢复；外部合成软件回导核验。 |
| G15 | 第三阶段后 | VST3/AU音频插件宿主 | 内置DSP已有；未找到扫描、宿主、延迟补偿、插件状态与隔离。 | 在现有音频一致性稳定后做；真实插件预览/离线一致，缺插件提示，崩溃不丢工程。 |
| G16 | 第二阶段高，依赖C01 | 普通Transform/Opacity动画入口 | 模型/槽存在，当前Inspector仅effect参数有◆，普通clip无可点击动画控件。 | 属性◆、曲线/插值编辑、前后关键帧导航，clip/effect统一schema；预览/导出与Undo/持久化贯通。 |
| G17 | 第二阶段高，依赖U02 | 嵌套音频、转场与管理完整性 | 基本嵌套已工作；父retime/mixedFPS音频需重采样；子轨transitions被清空；历史FE-F07仍未闭环。 | 共享样本时域、保留或显式拒绝子转场；补可理解的父子导航/展开，双层嵌套完整音频成片回归。 |

更完整源码位置和官方链接见 [专业能力专项](audit/2026-10-21-edit-full-review/specialists/topos_edit_pro_parity_20261021.md)。音频专项另记录 surround panner、auto-ducking从纯DSP到生产入口的建设候选；不把这些算法文件的存在等同已经能在UI使用。

## 7. 分阶段执行与关闭门槛

| 阶段 | 建议工作包 | 关闭条件 |
|---|---|---|
| 第一阶段A | U01–U04，Q02，完整sequence/resolver/timebase契约 | 新建真实MainWindow后，自定义/非当前/嵌套导出成功；导航→Split位置与屏幕一致；不得只用SimpleNamespace测 |
| 第一阶段B | D01/D02/D04/D05/D06，统一音频图和采样边界 | 正弦/脉冲/反相立体声＋真实素材PCM黄金对照，成片/Stem/预览一致；不支持的组合明确preflight |
| 第一阶段C | C02–C06、D08/D09、D03/D07/U06 | 命令Undo逐字段恢复、锁轨零写入、字幕往返不丢、取消资源释放，切源压力无GPU生命周期告警 |
| 第二阶段 | G01–G09、G13/G14/G16/G17，配合C01/U05 | 完整用户工作流＋外部软件往返/真实交付验收；能力表声明精确的可用/部分/不支持范围 |
| 第三阶段 | G10–G12/G15，硬件/多人/广播扩展 | 以真实用户场景和性能预算决定投入，不以“文件中有类/枚举”作为完成 |
| 持续门禁 | Q01/Q03、U07和真实宿主集成覆盖 | 单进程套件可结束；分批门禁有超时和诊断；能力声明/翻译与产品一致 |

建议新增的自动验收重点是**宿主装配与输出语义**：真实TimelineSequence/Project进入真实面板和worker，覆盖切页、换时间线、旧对象引用、异步取消、Undo/Redo和保存重开。当前10,672项通过的套件仍遗漏了上述用户可达问题，不应再仅增加与实现同构的局部断言。

## 8. 本轮未完成的验收与后续专项

- 全编码/位深/分辨率/RAW逐个组合，4K长时播放、真实丢帧和60fps目标、多显示器/HiDPI。当前导出速率不是4K性能结论。
- 真正声卡/监听耳听、CoreAudio延迟、音画打板回环、5.1/7.1接线；当前PCM数字证据不等于主观音质认证。
- 真实代理生成→只留代理→回原片/重连→master导出的完整链路；没有移动或删除用户原片来模拟离线。
- 崩溃强杀恢复、磁盘满/权限拒绝/拔盘、小时级工程；本轮保存重开是正常生命周期。
- Resolve/Premiere/FCP/Pro Tools真实外部往返、SCC/TTML播出链合规、第三方插件和协作冲突。
- 依赖外部模型的AI剪辑、ASR/OCR/翻译真实结果质量；当前无profile或缺模型包的状态已记录，未擅自调用付费服务。
- Ripple/Rate Stretch独立鼠标夹具、所有工具/快捷键预设、音频自动化推子触控的完整人工式覆盖。

以上明确标为未测/待验收，不以历史审计结果代替本轮证据，也不把未验证直接认定为缺失。

本轮证据入口：[README](audit/2026-10-21-edit-full-review/README.md)、[回归批次](audit/2026-10-21-edit-full-review/regression/summary.json)、[截图目录](audit/2026-10-21-edit-full-review/screenshots/)、[真实导出manifest](audit/2026-10-21-edit-full-review/records/gui_export_manifest.json)。
