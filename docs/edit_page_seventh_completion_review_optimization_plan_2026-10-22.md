# 剪辑第七轮：已完成内容复核与下一阶段优化计划表

日期：2026-10-22（Asia/Shanghai）  
复核基线：`d11615bb85d057f92735dbfcbfdc0debb127fc13`，分支 `fix-ui`。  
变更范围：`e1b9e49b8..d11615bb8`，49 个文件，重点是第六轮修复、新增 PNG/TIFF 图序和普通属性动画入口。  
对照：[第六轮原计划](edit_page_sixth_full_audit_optimization_plan_2026-10-21.md)、[执行进度](edit_page_sixth_round_progress_2026-10-21.md)。

## 1. 复核结论

**不能把“25/26 优化项完成＋G14/G16 完成”直接理解为这些工作流已经全部验收通过。** 本轮确认了多项有效修复，也发现新增回归、修复遗漏和已披露但未关闭的遗留问题。合并同根因后，新增计划共 **23 项：P1 7 项、P2 15 项、P3 1 项**；其中 R21/R22 承接上一轮已经披露的遗留事项，其余也不都能断言是本次新引入，表中区分证据和来源。

最先处理的风险是：**旧版本时间线被导出、音频静音/读错入点/保调失效/混帧率时钟错误、动画属性编辑不落关键帧、图序覆盖和取消删除旧文件**。完成正确性和安全性复核后，再建设高级修剪、Source Patching、多机位实时监看、专业交换等能力。

本次只新增这份计划和审计证据，**没有修改业务代码、既有测试或原始视频，也没有执行修复或提交 Git**。下表的待办事项均尚未实施。

## 2. 本轮证据与测试范围

可见窗口由正常 `src.main.main()` 启动 Studio，使用真实 Qt 按钮、键盘、对话框、生产命令/handler、实际解码与编码器；系统级桌面控制不可用，交互采用 QTest 注入 Qt 事件。未替换应用渲染器，未以 offscreen 窗口冒充可见窗口测试。独立 pytest/部分面板诊断使用 offscreen，另行标注。

证据等级：**G**＝真实可见 MainWindow 交互和文件结果；**H**＝该真实窗口调用生产 handler 准备/操作状态；**M-real**＝生产模型与真实测试视频；**M-gen**＝真实生成 WAV/像素经生产解码/处理/编码；**Q/P**＝真实独立 Qt 调度/面板或受控 decoder 夹具；**S/T**＝源码/既有测试。生成样本不冒充相机素材，受控替身不冒充真实声卡或音频解码。

| 检查批次 | 结果 | 解释与证据 |
|---|---|---|
| `tests/deliver` 全目录 | **229 passed，12.45s** | [日志](audit/2026-10-22-edit-completion-review/tests/topos-round7-deliver-suite.log)、[JUnit](audit/2026-10-22-edit-completion-review/tests/topos-round7-deliver-suite.xml) |
| 音频/Stem/保调/嵌套 45 文件 | **937 passed、1 skipped，22.32s** | 含新增音频21例，不重复相加；跳过项缺指定 WAV；[日志与文件清单](audit/2026-10-22-edit-completion-review/tests/topos-round7-audio-suite.log)、[JUnit](audit/2026-10-22-edit-completion-review/tests/topos-round7-audio-suite.xml) |
| 核心命令/属性/交换专项 | **99 passed，4.24s** | [日志](audit/2026-10-22-edit-completion-review/tests/topos-round7-core-pytest.log) |
| 新图序/decoder 生命周期/能力专项 | **26 passed，5.29s** | 与 Deliver 全目录有重叠；[日志](audit/2026-10-22-edit-completion-review/tests/topos-round7-delivery-tests.log) |
| 宿主/播放头/关窗专项 | **80 passed，7.99s** | 与以上批次有重叠，不累计；[日志](audit/2026-10-22-edit-completion-review/tests/topos-round7-host-tests.log) |
| 实际 MainWindow | 9种素材导入，27次切源，导航、Split/Delete、动画属性、嵌套、非当前序列、图序、保存重开 | [逐步骤记录](audit/2026-10-22-edit-completion-review/records/)、[截图](audit/2026-10-22-edit-completion-review/screenshots/)；G/H 混合，不声称所有入口均手工点击 |
| 实际交付文件 | 4个 H.264/AAC 任务和1个 PNG 图序任务显示 done | 独立 ffprobe/PCM 检查发现静音和旧序列输出，**不等于5个交付均合格**；[实际文件核验](audit/2026-10-22-edit-completion-review/records/export_inspection.json) |

真实媒体为 `tests/test_videos` 下九种：`2K_h264.mp4`、`2K_H265.mov`、`2K_prores422.mov`、`4K_H264.mov`、`4K_H265.mov`、`4K_prores422Proxy.mov`、`2K_DNxHR HQX 10bit.mov`、`2K_topos_422_LT.mov`、`2K_Prores4444_Alpha.mov`。本轮未重新执行整个 `tests/edit` 单进程全量，不沿用上一轮10,672通过作为当前基线成绩。

正常保存的隔离工程实际路径是 `/tmp/topos-round7-gui/Round7Audit20261022.topos`，新进程重开成功：9资产、2序列、活动序列200帧，动画数据仍在；两个应用会话均正常关闭、进程退出码0。重开期间仍出现效果面板生命周期异常，见 R23，因此不能写成“保存重开全链路无异常”。

## 3. 上一轮完成项逐项复核

“本轮支持通过”只表示对应复现/测试覆盖的子集，不代替所有边界验收；“部分”表示有效改动存在，但不能关闭整个工作项。

| 原ID | 本轮判断 | 已通过内容 / 仍需处理 |
|---|---|---|
| C01 | 部分 | 新建动画负平移/旋转值域正确；旧工程范围仍会截断，见R11。 |
| C02 | 主命令通过，旧API遗漏 | 新/已有轨主命令动画迁移通过；旧注册命令缺省Undo起点错误，R14。 |
| C03 | 本轮支持通过 | 单边转场迁移、双边丢弃的Undo快照回归通过。 |
| C04 | 本轮支持通过 | 链接变速音频转场80帧快照恢复通过。 |
| C05 | 本轮支持通过 | 冻结后marker/尾转场归属、整轨Undo通过；未遍历所有变速冻结组合。 |
| C06 | 本轮支持通过 | 新增9个效果/关键帧写命令锁轨守卫回归通过。 |
| D01 | 部分且有输出错误 | 入点0基本2x/0.5x测试通过；非零入点、混帧率、倒放端点失败，R03/R06/R07。 |
| D02 | 本轮支持通过 | 实际同相/反相stereo增益均-8.8489215574dB，静音不建议正增益；未做实物环绕监听。 |
| D03 | 部分 | 工作移出UI线程有效，但取消信号排队、UI wait未闭环，R09。 |
| D04 | 部分 | 离线Limiter动画回归通过；实时链从inactive进入动画时不求值，R08。 |
| D05 | 部分 | 普通FX、freeze、基本Master stem等价回归通过；R03/R05/R06及嵌套G17仍需统一正确参考。 |
| D06 | 部分且新增回归 | NTSC恒定输入无旧零洞；真实AAC短块导致整段静音，R04。 |
| D07 | 本轮支持通过 | 取消/异常走finally显式关decoder的回归通过；不代表Q01原生竞争根因已修。 |
| D08 | 本轮支持通过 | 标准ASS/SSA逗号、Format列、换行/标签及往返专项通过。 |
| D09 | 部分 | 显式tickRate、multiplier、嵌套begin有效；缺省、非法时基、frame-clock及父end仍不完整，R12/R13。 |
| D10 | 部分 | enum.value与无音轨集合已修；容器/alpha事实仍误报，R20，当前未接生产消费。 |
| U01 | 原故障已通过 | 真窗口Custom范围不再被判0帧，实际输出49帧 `[0,49)`；未把排他end误判为少一帧。 |
| U02 | resolver主链路通过 | 真嵌套50帧入队并产出视频，原child resolver错误未再出现；本次声音静音属R04，G17也未完成。 |
| U03 | 部分 | Program为200帧B时，下拉选50帧A确实导出A；同页继续编辑仍读旧快照、恢复时误报失联，R01/R02。 |
| U04 | 原故障已通过 | 真窗口尺规25→Next26→Prev25→Home0，controller/view/page同步。 |
| U05 | 本轮支持通过 | Find独立信号与宿主seek回归通过；本轮未重新完成字幕Find的完整鼠标路径。 |
| U06 | 关窗通过，切源未关 | 两次正常关窗未再出现旧2000ms误报；27次切源仍3组GL/帧释放告警，R21。 |
| U07 | 本轮支持通过 | 10语言对应文本静态核对及回归通过；未逐语言截图验收。 |
| Q01 | 诊断有改进，未关闭 | focused生命周期用例通过，faulthandler只输出栈，原全套卡死未复验/未修根因，R22。 |
| Q02 | 本轮支持通过 | 嵌套preflight真实TimelineSequence夹具修正，Deliver全目录通过。 |
| Q03 | 本轮支持通过 | 旧表FCPXML声明与PARTIAL registry对齐，不再误报完全不可用。 |
| G14 | 基础已新增，部分可用 | 真PNG图序及PNG/TIFF生产编码通过；覆盖安全、配置、PNG16和结果语义待修，R15–R19。 |
| G16 | 入口已新增，编辑闭环未过 | 六个普通属性◆存在；实际不能制作不同值动画，R10，不能按“完整动画已完成”关单。 |

## 4. 新优化计划：交付、音频与取消

状态均为 **待处理**。P1＝输出内容错误/主要工作流失效/既有文件安全；P2＝边界、一致性、生命周期和门禁；P3＝当前未生产消费的声明错误。

| ID / 优先级 | 确认问题与来源 | 证据和源码定位（基线行号） | 建议修复与关闭条件 |
|---|---|---|---|
| R01 / P1 | **Deliver继续编辑后导出旧时间线。** 原U03只解决初次选择，快照同步仍断。真实100帧序列Split后删除后半，live/draft为rev3/50帧，preflight/controller仍rev1/100帧，任务done，实际MP4为100帧/4秒。 | G；[004记录](audit/2026-10-22-edit-completion-review/records/004_live_edit_in_deliver.json)、[ffprobe](audit/2026-10-22-edit-completion-review/records/export_inspection.json)；`deliver_workspace.py:422–425`仅更新panel，不更新controller/map；`video_export_settings_panel.py:2978`优先旧map。 | 统一活动/非活动完整序列和project revision的更新入口；父/子序列编辑、Undo/Redo后同时刷新预检/草稿/队列上下文。验收：Deliver内剪短/改FX/改子序列后，实际文件内容、帧数和所选revision一致，不能靠离页重进临时恢复。 |
| R02 / P2 | **恢复队列误把存在的序列标为needs_relink。** 新过滤Ref后，set_project在完整序列promote前restore；后续解析已成功，状态/错误仍不恢复。 | Q，真实EditProject/TimelineSequence及实际队列JSON；[queue_restore](audit/2026-10-22-edit-completion-review/records/queue_restore.json)；`deliver_workspace.py:460–491`、controller `restore_from_disk:255–266`、`register_sequence:162`。 | 完整map到位后恢复，或promote后重评估pending/needs_relink，保留真实离线状态。验收：关闭重开带pending/failed/running任务的真实工程，存在的目标自动可运行，真正丢失目标才失联；不要要求用户无谓重连。 |
| R03 / P1 | **非零入点变速音频可变整段DC常量。** D01新窗口用相对源帧解码，采样却加绝对入点，越界钳到尾样本。 | M-real：`2K_h264`入点100、2x、48kHz，peak .02717868、尾1秒std≈3.7e-9；1x对照有正常变化。M-gen坡度应.166→.322，实际恒.165985。`audio_retime.py:228–261`；[真实PCM](audit/2026-10-22-edit-completion-review/probes/topos-round7-audio-real-results.json)。 | 窗口/base_sample/采样位置统一绝对源样本坐标，贯通成片与Stem。验收：0/25/100/大入点×0.5/2x/曲线/倒放，逐段和外部参考对齐，无尾部常量；不能只检两条错误路径彼此相等。 |
| R04 / P1 | **AAC短块让整片段静音，导出仍done。** D06精确目标边界改动后，实际短块1748样本累加到1764样本slice抛异常，外层except直接跳过clip余下音频。 | G＋M-real；`timeline_export.py:4321,4413–4426`。本轮4个实际H.264输出AAC解码均peak0/nonzero0；直接源后续帧可正常解码，源有声音。[文件核验](audit/2026-10-22-edit-completion-review/records/export_inspection.json)、[真实probe](audit/2026-10-22-edit-completion-review/probes/topos-round7-audio-real-results.json)。 | 正确续读/补齐短块或收窄写入slice，保持时钟；不可整段静音后伪装成功。验收：44100/48000、AAC非零入点、短尾、Bus/Send和Custom均有正确连续PCM，异常有用户可见诊断。 |
| R05 / P1 | **离线Preserve Pitch实际变调，Stem也未完整支持。** helper运行时`np`未定义，捕获后静默回退；Stem另有明确回退变调分支。属于旧遗漏，D01/D05完成声明未覆盖。 | M-gen真实440Hz WAV、2x保调，成片/Stem都880Hz、两者maxdiff0，预期440Hz。`timeline_export.py:89–90,3354–3364,4241–4254`、`stem_export.py:861–862,927–941`；[主probe日志](audit/2026-10-22-edit-completion-review/probes/topos-round7-audio-probe.log)。 | 修实际WSOLA入口并共享Stem保调图；不支持反向/曲线等组合要预检明示。验收：0.5/1.25/2x保调频率/时长/入点正确，正常路径不进入降级；实时G06另行建设。 |
| R06 / P1 | **混合帧率音频用错时钟。** canonical返回源帧，却传序列fps给decoder；普通1x也错，非仅嵌套。 | M-gen，真实WAV＋生产ClipInstance元数据源50/序列25：时间线1秒读到源2秒，PCM .0880127，正确应.0490001。`timeline_export.py:4314–4316`、`stem_export.py:945–949`、`audio_retime.py:211–250`；[诊断](audit/2026-10-22-edit-completion-review/probes/audio_synthetic_results.json)。 | 在源样本时钟中读连续区间，明确source fps与timeline fps。验收：24/25/30/50/59.94不同源/序列，脉冲打板与视频同刻，成片/Stem不意外倍速。未完成该组合的GUI鼠标测试。 |
| R07 / P2 | **完整倒放最后一帧成为常量块。** D01端点外推-1先夹0，x0=x1。 | M-gen真实坡度WAV，最后1920样本std0，前帧std .00045047；`audio_retime.py:86–102,259–267`；[reverse_tail](audit/2026-10-22-edit-completion-review/probes/audio_synthetic_results.json)。 | 统一排他区间和反向样本端点。验收：完整首尾逐样本reverse、单帧、带入点、0.5/2x倒放都无平直块或丢帧。 |
| R08 / P2 | **实时FX动画不能从inactive激活。** 先判断静态chain.is_active才求动画，初值drive0时永远不采样；从active变inactive后也可能无法再次激活。 | P，生产AudioMixer/EffectChain＋DC decoder替身，sampler调用0次；实际peak.1，正确drive100应.85；`audio_playback.py:2340–2353`；[Qt/FX结果](audit/2026-10-22-edit-completion-review/probes/topos-round7-audio-qt-results.json)。 | 动画求值先于active优化判断，避免中性参数跳过动画。验收：0→非0→0→非0、EQ中性转换、bypass交替时预览与成片同帧生效。 |
| R09 / P2 | **响度分析取消到工作结束才生效，取消后UI还wait。** worker忙于run，queued cancel排在后面；progress返回后thread.wait(5000)阻塞GUI。D03移线程有效但取消验收未过。 | Q，真实Qt线程＋可控analysis，100ms发cancel时标志False，run完成才True；`timeline_view.py:11748–11769`；[调度证据](audit/2026-10-22-edit-completion-review/probes/topos-round7-audio-qt-results.json)。 | 线程安全cancel token从GUI直接置位，异步收尾，取消/过期结果不更新spin。验收：长素材解码/DSP期间取消<200ms响应、零增益提交、线程及时退出；关闭父窗/重复取消稳定。本轮未证实其导致崩溃。 |

## 5. 新优化计划：动画、字幕与编辑完整性

| ID / 优先级 | 确认问题与来源 | 证据和定位 | 建议修复与关闭条件 |
|---|---|---|---|
| R10 / P1 | **普通属性◆有了，但正常编辑写不出不同值动画，Inspector也不随帧求值。** 0帧X=0打点→25帧输入100，静态/控件100，渲染采样0；再点◆写入的仍0。另一独立面板向量已有0→100动画时，移动播放头数值仍静态0。 | G/H及Q；[006真实操作](audit/2026-10-22-edit-completion-review/records/006_g16_animation.json)、[保存重开后仍0/0](audit/2026-10-22-edit-completion-review/records/014_reopen_actual_saved.json)。`edit_controller.py:2467`、`commands.py:3464`只改静态值；properties `:1975`重新采样旧动画，`:2391,2533`未刷新显示。 | 明确已动画参数的编辑策略：更新当前关键帧/自动插点/静态base分别走正确命令；按当前帧求值刷新Inspector和手柄，程序刷新不反写。验收：真实0→100中点50、Opacity同验；Undo/Redo/保存重开/预览/导出一致；补曲线及前后关键帧导航。 |
| R11 / P2 | **旧工程动画值域未迁移。** C01权威范围只用于新建param，既有0..100轨新增负关键帧后仍夹0。 | M模型：旧序列to_dict/from_dict后raw[-250,-500]，range仍[0,100]，采样都0；`commands.py:3853`、`timeline_v2.py:1081`、`effect_instance.py:137`；[probe](audit/2026-10-22-edit-completion-review/probes/topos-round7-core-probe.json)。 | 明确schema迁移或参数修复策略，保留raw和未知字段，区分clip参数与FX schema。验收：旧负平移/旋转/scale工程新旧点、Undo、重开均正确。 |
| R12 / P2 | **TTML缺省tickRate错误。** 有frameRate30而无tickRate，30t→60t在24fps应24起24帧，实际720起720帧。D09只覆盖显式时基。 | M解析；`captions/importer.py:208–246`；[向量结果](audit/2026-10-22-edit-completion-review/probes/topos-round7-core-probe.json)。默认规则参见[W3C tickRate](https://www.w3.org/TR/ttml1/Overview.html#parameter-attribute-tickRate)。 | 按effectiveFrameRate×subFrameRate推导相应缺省值，兼顾multiplier。验收：显式/缺省frameRate、tickRate、subFrameRate全组合与目标帧率转换符合规范。 |
| R13 / P2 | **TTML非法时基/合法帧式clock/父结束范围处理不完整。** tickRate0被夹到1e-9，生成2400亿帧起点/时长；media的frame-clock解析失败当0；父div结束不裁切子p，字幕越过父范围。 | M解析，三个独立向量保存在[core结果](audit/2026-10-22-edit-completion-review/probes/topos-round7-core-probe.json)；`importer.py:227,259,331,351`；[W3C时间表达式](https://www.w3.org/TR/ttml1/Overview.html#timing-value-timeExpression)。 | 非正/非有限时基、无效token明确拒绝；支持的clock语法正确，父活动区间裁切。验收：0时基拒绝；源30fps的`00:00:01:00→00:00:02:00`映射目标24fps的帧24→48；父1–3s/子相对1–9s可见2–3s；不支持seq/timeBase明确报错。 |
| R14 / P2 | **旧跨轨注册命令缺old_start时Undo回0。** 旧create允许省略，apply未捕获原位置；100→300→Undo0。当前主UI未调用该旧API，不能写成UI拖动必现。 | M模型/S注册API：`MoveClipToExistingTrackCommand`，`commands.py:1450,1525,1578,19073`；[probe](audit/2026-10-22-edit-completion-review/probes/topos-round7-core-probe.json)。 | apply捕获原位置/快照，或明确淘汰旧入口且兼容重放。验收：缺省/序列化重放、marker/动画/转场、重复Undo/Redo逐字段恢复，测试同时断言位置和关键帧。 |

## 6. 新优化计划：图像序列、生命周期与门禁

| ID / 优先级 | 确认问题与来源 | 证据和定位 | 建议修复与关闭条件 |
|---|---|---|---|
| R15 / P1 | **同名图序直接覆盖旧帧，取消还删除旧文件。** G14新编码器wb覆盖，无归属/备份；abort无条件删除_written。更短重导出还可能留旧尾帧。只在临时夹具复现，未损坏用户素材。 | M-gen真实编码；[collision结果](audit/2026-10-22-edit-completion-review/probes/topos-round7-delivery-probe.json)；`image_sequence_encoder.py:84–94,150–156`。 | 逐帧冲突预检、独立暂存和成功提交，取消只清本次新建帧，处理旧尾帧。验收：Ask/Skip/Overwrite/Auto-number按真实帧生效；旧序列hash在失败/取消后不变；长→短不混帧。 |
| R16 / P2 | **接受目录目标但写到父目录，manifest不核验图序。** 真实PNG/TIFF给现有目录，目录空，帧在父目录；单文件hash报Is a directory；基路径同样不是实际输出文件。 | M-real真实解码/编码，非完整鼠标入口；[e2e结果](audit/2026-10-22-edit-completion-review/probes/topos-round7-delivery-e2e.json)；encoder `:48–54`、timeline config `:574–585`、`export_manifest.py:207–235`。真GUI另成功输出3张PNG，[011](audit/2026-10-22-edit-completion-review/records/011_png_export.json)。 | 统一目录/基名语义，序列manifest列帧清单、hash、FPS、范围/首尾；实际位置和UI一致。验收：已有/新目录/带扩展名基路径均正确，缺帧/取消不生成“完整成功”manifest。 |
| R17 / P2 | **16位及可配置序列帧号未贯通正式作业配置。** config无bit_depth/naming_config，正式TIFF仍rgb24；encoder读不到起始号和padding。直接uint16单测通过不代表用户能交付16bit。 | S＋真实config bridge；`timeline_export.py:4943`、`video_export_job.py:793–838`、encoder `:56–60`；[config_fields/production_config](audit/2026-10-22-edit-completion-review/probes/topos-round7-delivery-probe.json)。 | 显式序列配置schema＋UI＋持久化＋encoder桥；图序帧号和individual去重digits区分。验收：队列重开后8/16bit和首帧号/padding正确，外部回读精度/alpha一致。 |
| R18 / P2 | **PNG16端序重复处理，像素和alpha数值损坏。** 当前普通UI受R17限制8bit；不是所有PNG输出都错误。 | M-gen真实PyAV PNG16，RGBA[30,250,100,200]回读[7680,64000,25600,51200]，TIFF16对照正确；encoder `:131–142`；[像素结果](audit/2026-10-22-edit-completion-review/probes/topos-round7-delivery-probe.json)。 | 按PyAV数组数值/端序契约传值。验收：RGB/RGBA16的ramp/随机值/alpha逐值回读正确，8bit不回归。 |
| R19 / P2 | **图序无音轨却返回audio_omitted=False。** encoder缺supports_audio=False，主导出误尝试不存在的add_audio_stream，捕获后成功但无省略标识。 | G日志＋M-real结果；[真实e2e](audit/2026-10-22-edit-completion-review/probes/topos-round7-delivery-e2e.json)、[应用日志](audit/2026-10-22-edit-completion-review/application.log)；`timeline_export.py:3004,3030`。 | 正确无音轨能力，结果/队列/manifest一致披露，提供sidecar策略。验收：include_audio=True不抛缺方法异常，audio_omitted=True且可见；主动关音频不误报。 |
| R20 / P3 | **DeliveryCapability容器/alpha仍误报。** enum已修，但GIF/PNG/TIFF仍container mov，PNG/TIFF支持RGBA却alpha_capable=False。当前函数无生产调用，维护契约风险。 | S＋实际config查询；`delivery_capability.py:81–86`，ALPHA集合；[capability结果](audit/2026-10-22-edit-completion-review/probes/topos-round7-delivery-probe.json)。 | codec registry统一输出形态/容器/alpha/位深/音频，未知明确unknown。验收不仅enum=string，也与实际encoder事实一致。 |
| R21 / P2，遗留 | **切源GL1282/lease/帧释放仍复现。** 27次九素材循环、循环内无截图，3组glUseProgram错误，每组伴WRITING回收及未释放帧。关窗误报修复不等于切源修复。 | G；[007记录](audit/2026-10-22-edit-completion-review/records/007_switch_sources.json)。当前源名为Alpha但可能旧异步任务晚到，不认定单一编码根因。 | 查program/context/generation归属，异常释放GPU frame。验收：长循环无GL/lease警告、显存稳定、快速换源/seek/关窗稳定；本轮未证明永久黑帧或泄漏字节数。 |
| R22 / P2，遗留 | **Q01根因未关闭，新增“门禁”不能有界终止卡死。** faulthandler_timeout只dump栈；close返回后的耗时断言遇永久等待永不执行；del计时在del后开始，GC循环又持有强引用。 | S/T；`pytest.ini:16`、`test_fix_round6_q01_decoder_lifecycle.py:84,105–110`；[专项结果](audit/2026-10-22-edit-completion-review/tests/topos-round7-delivery-tests.log)；[pytest官方](https://docs.pytest.org/en/stable/how-to/failures.html#fault-handler)。本轮未重跑全套复现原卡死。 | 独立子进程硬超时＋栈捕获，del前计时，worker活跃时竞争关闭；另修根因。验收：故意阻塞close也能预算内失败退出；原单进程全套自然结束后才能关根因。 |
| R23 / P2，新观察 | **正常重开工程后效果面板访问已删除QTreeWidgetItem。** 可见MainWindow重开成功，但resize/reflow触发9次未处理RuntimeError；进程最终正常关闭。本轮新增观察，相关文件未在本批修改，具体触发的clear分支仍待定位。 | G/H；[slot_exceptions](audit/2026-10-22-edit-completion-review/logs/slot_exceptions.log)、[014重开记录](audit/2026-10-22-edit-completion-review/records/014_reopen_actual_saved.json)。`edit_effects_panel.py:537,568,572`；视频树重建`:1489/2013`未像transition树`:1845–1848`统一unbind。 | 重建前解绑所有响应式section、处理延迟resize/销毁归属。验收：新建/重开/清搜索/切分类/布局缩放循环无deleted-item异常，卡片仍可点击添加；不得用全局吞异常遮盖。 |

## 7. 关键真实复现步骤

### R01：剪短了，导出的仍是旧片

1. 导入2K H.264，源In50/Out149，真实Append得到100帧链接V/A。
2. 进入Deliver，成功跑一次Custom任务，建立正常导出上下文。
3. 保持Deliver页，播放头50，实际Ctrl+B切开，选后半V/A后Delete。
4. 当前可见时间线和draft为50帧，preflight/controller仍100帧。
5. Entire Timeline点“加入并开始”；实际输出100帧/4秒，删除的后半仍被导出。详见004和实际文件核验。

### R04：任务完成，有AAC轨，却全静音

同一实际素材源入点50，25fps时间线，H.264＋AAC44100导出，日志首块`(2,1764)/(2,1748)`广播错误。本轮Custom、旧快照、嵌套、非当前序列四份成片均done；独立FFmpeg解码音轨后nonzero_samples=0。不能用“存在音轨”或队列绿色状态作为声音通过标准。

### R10：第二个关键帧仍是0

真实选片，0帧点X的◆；导航25，在X框输入100并Enter，再点◆。控件/静态100，但动画点为0帧0、25帧0。重开工程这些错误值仍持久化，说明不是暂时未刷新显示。

R15的覆盖/取消风险仅在新建临时目录用哨兵旧文件复现，未对用户既有图像序列进行破坏性测试。源码、可运行probe和测量JSON已归档。

## 8. 下一阶段对标专业剪辑软件的能力计划

对标依据为2026-10-22复查的官方工作流；Topos状态和优先级为当前源码/实测判断。不是要求所有软件功能全复制，也没有把现有基础剪辑、代理、多机位、音频混音、字幕、备份恢复整体写成缺失。

G14/G16已从“无入口/无标准序列”变成**部分可用**，先补本轮R10/R15–R19；其余15个能力域仍需要建设或加强。以下17行沿用原ID，便于跟踪，并非另加17个已确证bug。

| 原ID / 阶段 | 已有基础与剩余差距 | 下一阶段可交付目标与验收 |
|---|---|---|
| G01 / A | 有V/A目标轨，缺逐源流/声道Source Patching。 | 源音频1/3选送A1/A3，video-only/audio-only Insert/Overwrite，一次Undo，无额外声道。[Premiere Source Patching](https://helpx.adobe.com/premiere/desktop/edit-projects/intro-to-editing/add-media-to-the-timeline-using-source-patching.html)。 |
| G02 / A | 有基本三点，缺backtime/fit-to-fill/四点冲突入口。 | 源In＋目标In/Out定长、目的Out倒推、四点冲突明确选修源/变速，混帧率精确。[Final Cut三点/backtime](https://support.apple.com/en-ca/guide/final-cut-pro/ver549f253e/mac)。 |
| G03 / A | 有trim工具/数字步进，TrimMonitor仍主要是标签。 | 切点A/B真实帧、JKL动态修剪、预后卷循环监听、commit/cancel；±1帧与输出一致。[Resolve Edit](https://www.blackmagicdesign.com/products/davinciresolve/edit/)。 |
| G04 / B | 有多机位组/数字切角，监看仍250ms缩略图。 | 4/9路共同时钟实时网格；固定主音轨、独立A/V与AFV可选；切点与成片一致。参考[Resolve多机位](https://www.blackmagicdesign.com/products/davinciresolve/edit/)。 |
| G05 / B | 有短窗口波形同步，缺更多同步基准和长录制漂移。 | TC/In/marker同步，起末漂移检测/补偿，失败不写伪offset；用长录制打板验收。 |
| G06 / A，先R05 | 实时仍变调，UI已披露export only；离线当前也有R05。 | 先修离线，再做实时0.5/1.25/2x保调，seek/loop无爆音，语音音高和时间与导出一致。[Final Cut Preserve Pitch](https://support.apple.com/en-ae/guide/final-cut-pro/verb6aca45d/mac)。 |
| G07 / B | decoder可sample seek，用户clip/fade位置仍整数视频帧。 | 48kHz移动/trim/fade一sample，Undo/保存/Stem精确保留；视频仍按帧。 |
| G08 / B | AAF仍不可用，已有Stem/BWF/CSV不能重建完整后期时间线。 | 优先AAF：轨位、源TC、声道、handles、音量自动化，真实Pro Tools/NLE导入。[Premiere AAF](https://helpx.adobe.com/premiere/desktop/render-and-export/export-files/export-aaf-files.html)。 |
| G09 / B | FCPXML/OTIO有受限子集，非完全缺失；缺复杂往返/reconform保证。 | 建外部黄金工程，nested/retime/multicam/音频路由损失逐项披露；代理回原片和重套真实验收。 |
| G10 / C | 有ASR/字幕/AI基础，无逐词文稿直接剪媒体的完整工作区。 | 文稿删词走既有事务，同步linkedAV/字幕/marker，Undo可还原；先验证词到源时域。[Premiere文稿编辑](https://helpx.adobe.com/premiere/desktop/edit-projects/edit-video-using-text-based-editing/transcribe-video.html)。 |
| G11 / C | ADR试听已有，无通用视频多take/Audition容器。 | 同位置多候选比较、不同长度切换，保存/Undo保留，交换明确flatten。[Final Cut Auditions](https://support.apple.com/guide/final-cut-pro/intro-to-auditions-verbbd3587d/mac)。 |
| G12 / C | 有自动保存/备份，无完整命名版本、多人冲突与审片回流。 | 先命名快照＋评论→marker，再共享锁/合并；双人同改显式冲突。[Resolve协作](https://www.blackmagicdesign.com/products/davinciresolve/collaboration/)。 |
| G13 / B | 批量日志字段有基础，混合值素材Properties/解释媒体不完整。 | 混合值提示、批量帧率/声道/标签，一次事务或清楚的逐项失败；不能用时间线多选替代。 |
| G14 / 先修复 | PNG/TIFF8基础已可真实交付，不能继续列“完全没有”。 | R15–R19安全/目录/位深/alpha/manifest关闭；序列帧号、取消续写、颜色元数据、外部合成软件回导完整验收。EXR/DPX按工作流需求后续排。 |
| G15 / C后 | 有内置DSP，无完整VST3/AU宿主。 | 扫描/隔离/延迟补偿/插件状态/缺插件诊断，真实插件预览和离线一致，崩溃不丢工程。 |
| G16 / 先修复 | 六个普通属性◆入口已有，动画作者工作流仍失败。 | 先R10/R11，再曲线/插值、前后关键帧导航、手柄同步；真实窗口制作动画后输出/保存逐帧一致。 |
| G17 / A，先R01/R06 | 有基本嵌套和本轮resolver修复；子转场展开仍被清、父retime/mixedFPS音频和管理不完整。 | 统一父子样本时域，保留或显式拒绝子转场，双层嵌套/父变速/混帧率/子音频实际成片对照；补清楚的父子导航/展开。 |

## 9. 建议执行顺序和关单门槛

| 工作包 | 内容 | 必须达到的关闭证据 |
|---|---|---|
| 0A 输出与文件安全 | R01、R03–R06、R15 | 真窗口实际成片＋外部解码PCM参考；旧图序失败/取消后逐文件hash不变；不能仅任务done。 |
| 0B 编辑闭环 | R10/R11、R12/R13、R14 | 普通用户动画制作、旧工程迁移、规范字幕向量、Undo逐字段恢复；输出和重开一致。 |
| 0C 生命周期与一致性 | R02/R07–R09、R16–R19、R21–R23；R20随后 | 重开队列不误失联，真实取消响应，图序回读、切源/布局压力、子进程硬超时；无未处理异常。 |
| A 核心专业效率 | G01/G02/G03/G06/G17 | 每项可从UI完成真实工作流，媒体/时间域正确，保存Undo闭环，再看性能。 |
| B 交接与长项目 | G04/G05/G07/G08/G09/G13 | 多机位共钟、长录漂移、采样精度、外部软件黄金工程往返，逐项可核验。 |
| C 产品扩展 | G10/G11/G12/G15 | 依实际用户优先级建设，有性能/兼容/失败恢复预算，不以类/枚举存在作为完成。 |

每个修复提交建议同时记录：复现向量→生产入口→正确外部结果→Undo/取消/重开→能力声明。R01/R04/R10已经证明，局部回归全部通过也可能漏掉用户实际结果，因此需要针对宿主更新顺序、真实AAC短块和已存在动画的行为补测试。

## 10. 未完成的验收及保留边界

- 没有重跑完整单进程Edit套件，Q01不能据focused通过关闭；没有证明每个原生hang都已消除。
- 未做外部Resolve/Premiere/FCP/Pro Tools实际往返，专业差距表是当前实现判断与下一步验收计划。
- 未做小时级工程、4K长时/多路60fps预算、显存增长测量、全部RAW/编码/位深组合、真声卡耳听/环绕接线。
- 未测试付费AI/缺模型包的真实结果质量；代理离线/强杀恢复/磁盘满等仍需专项。
- 009脚本误用控件名、012最初猜错保存文件名属于审计脚本问题，均已保留原记录并纠正，不列产品缺陷。014使用实际保存路径重开成功；013最初记录的猜测路径不是实际产物路径。

证据入口：[本轮README](audit/2026-10-22-edit-completion-review/README.md)、[核心专项](audit/2026-10-22-edit-completion-review/specialists/topos-round7-core.md)、[音频专项](audit/2026-10-22-edit-completion-review/specialists/topos-round7-audio.md)、[图序/专业差距专项](audit/2026-10-22-edit-completion-review/specialists/topos-round7-delivery.md)。专项报告保留原诊断临时路径；归档副本与重放对应关系见README。
