# 第三轮功能与工程状态只读复查

项目根：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。基线 `833dcd382` → HEAD `63c0bdeaeb5edcee59363479cd8c10b947c3ddb1`。已读规范与新计划§0及 recheck A/B/C/D/E 测试。没有修改生产代码、现有测试或历史证据；未启动可见GUI。`M` 为真实模型/命令/handler执行（GUI宿主/弹框/服务以替身控制）；`S` 为源码链核查，不能替代真实窗口、设备与用户耳听验收。

证据：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_features_probe.py/json`（旧probe的独立副本，临时文件全改新前缀）、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_features_extra.py/json/err`（扩大提交链）、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_features_cancel.py/json/err`（实际导出音频handler取消）、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_features_tests.log`（本范围新增tests 30 passed，3.55s）、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_features_old_contract.log`（旧真峰短流contract已改finalize，1 passed）。这些测试独立范围与主审计存在重叠，不累加为全部独立覆盖量。

## 1. 已修、部分完成与§0需要更正的状态

| 上轮条目 | 当前结论 | 当前证据/范围 |
|---|---|---|
| R20 字幕FPS | cue已修，真实词级时间未修；§0不能全关闭 | cue24–48→48–96；真实caption_metadata.word_timings仍24–48。新增测试用了fake顶层word_timings，RF01。|
| R21 Bezier | 原例已修 | handle(20,0)→(40,0)，相同实时时间值0.03399365保持，M。|
| R22 Source | 旧4例已改善，但当前不能全关闭 | 内部Insert有新A、Overwrite音床不抛、Append共同start96、A/V48fps→24fps Insert时长24；Append采样metadata丢失与静音源分支仍错，RF02。|
| R23 Multicam | 新目标fps/window换装已修；旧公共timebase/grid/resync明确待做；另有隐藏历史缺陷 | 实际controller目标fps48/window144；start仍24应48（已声明public timebase待做）；边界Undo时长72→144为未声明残余/新组合回归，RF03。|
| R24 Nest Bus | 原Bus依赖闭包与route/send重映射已修 | child含child-bus0，audio输出及send指本地Bus，M；新track构造仍丢extras，RF08。|
| R25 revision | ProjectCommandStack及实际Replace提交链已修，UI helper路径未完成且导航会重置0 | execute/undo/redo7→8→9→10；实际Relink UI→host仍7，实际创建/切timeline7→0。同R25根因，§3。|
| R26 proxy | 旧跨工程first-stage例已修，二阶段同路径重入未修；§0不能全关闭 | set_project(p2)旧token被拒；重新pending同proxy/source path旧resolution被接收，错source旧回调还会pop新pending，RF05。|
| R27 True Peak | 原文件尾/任意分块/直流已修；实际export未接取消 | 尾pulse报告1.744791dBTP，chunk1/2/64直流一致1.0002727；analyzer进度取消tests通过；export不传progress，cancel后仍第二次scan/报告发布，RF07。|
| R28 extras/JSON | model roundtrip与兼容JSON revision已修；重构性Nest仍丢扩展 | sequence/track/clip未知字段roundtrip及普通clip编辑都保留；JSON7→7；Nest child track未来字段丢，RF08。|
| U01 设置事务 | 控制器接线明确待做，新增领域command本身也不完整 | actual controller只load，0execute；领域redo seq revision2→3→9→3，并与ProjectStack返回协议不匹配，RF04。|
| U02/U03 | 明确未实施，不能解释为本轮已完成 | multicam/Nest工程事务及Undo仍直接写，§3。|
| U04 Replace probe | 成功的新类型/基本metadata已修，但整体✅过度 | png正确image/2×2；损坏源仍成功提交，异常回退旧metadata照样提交；全metadata替换丢multicam/未知payload；同步I/O跑菜单slot，RF06。usage处置策略已明示待做，单独保留。|
| F01–F16 | §0明确未实施的大工作流继续待建 | 不当作隐藏新bug；本轮没有高级工作流UI建设差异；§4按现有入口重新界定。|

## 2. 本轮需要进入新计划的确定问题

### RF01 · P1 · R20残余：真实字幕词级时间仍错域（M+S）

位置：[src/app/controllers/edit_controller.py:739](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:739>)取`getattr(sub,"word_timings",None)`，实际SubtitleClip词级存放在caption_metadata内（[src/features/edit/core/timeline_v2.py:1536](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:1536>)、[src/features/edit/core/captions/model.py:435](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/captions/model.py:435>)）。新测试[tests/edit/test_recheck_b_entries.py:167](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_recheck_b_entries.py:167>)使用独立fake dataclass顶层word_timings，因此fake通过没有覆盖生产字段。

动态值：24fps真实cue24–48与word24–48，改48fps后cue48–96，word仍24–48，首字高亮已在cue出现前结束。与原第二轮的cue+word都未转换相比，cue改善但word原问题仍在。

实施：sequence时域变换显式重建`caption_metadata.word_timings`并保留metadata其他字段/extras；source-domain evidence不盲目缩放。短word降fps后start/end合并必须定义至少1帧/合并政策，避免不可构造word。验收：真实SubtitleClip+metadata，24↔48/fractional/downsample、word highlight/ASS/graphic/export；Cue/word同秒误差≤1目标帧，Undo/save/reload一致。撤销错误的“SubtitleClip暂无该字段”假设，测试使用真实模型。

### RF02 · P1 · R22残余/新分支：Source Append采样时基遗漏，静音源OW/Append仍旧公式（M+S）

位置：[src/features/edit/ui/timeline_view.py:16511](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:16511>) Append A/V的common传正确duration却漏`_source_fps_num/_den/_timeline_fps_num/_den`。非A/V Overwrite `:16419`及Append `:16563`create不传fps，尽管新增命令已有fps参数。

动态值（真实TimelineView入口方法+真实macro/commands，48fps源0–47，24fpssequence）：

- 有声Append：duration24正确、V/A共同start正确，但clip source/timeline fps均None，timeline12采source12，应24；播放/导出只消耗前一半源窗口。新tests只断言共同start/duration，没有检验source sampling。
- 无声video的Overwrite/Append：duration48、fpsNone、timeline12采source12，应duration24/source24。不能用“有声视频已修”覆盖合法silent视频。
- 有声OW正确duration24/source12→24；无A目标轨的Overwrite/Append仍仅V（0audio tracks），Insert则可原子建A。这个补轨/stream policy是F01完整patch的已声明缺口，记录为尚未闭合能力，不能假设全部三入口均已满足A/V。

实施：三个入口先解析统一SourceEditRequest，所有stream/cardinality走同一fps/PTS/availability服务；clip factory传完整source metadata，planned duration与mapper一处求得；无A轨显式建轨或按明确stream patch政策反馈，不用无声丢弃作默认。验收：有声/无声/纯音频、无A轨/已有A、不同尾点与占用音床、48→24/29.97→23.976；每个有效timeline frame的source索引、A/V links、Undo/Redo/保存重载逐项等价，不只比较时长。

### RF03 · P1 · 新角度FPS换装未纳入历史与跨stream元数据（M+S）

位置：[src/features/edit/core/commands.py:3078](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:3078>) 边界切角（no split）Undo只还原asset/source_start，使用当前source_duration，没有原fps/window/完整clip；`UpdateClipSourceCommand :2821`返回updated command丢`_new_source_fps_*`，`:2860`序列化也没有新fps/原完整clip。

动态值：before `[A, source24, source_duration200, fps24, timeline_duration72]`，目标B48fpsapply `[B,24,144,48,72]`；Undo变`[A,24,144,48,144]`，即原素材挂着目标fps，timeline片段时长翻倍且源window没有恢复。影响直接source swap同型；history可再次引发clip overlap。原新C测试只验证apply的新fps/window。

跨stream：当前swap还保留旧`_source_pts_index`。M：A24fps的PTS序列(i/24)换到CFR48fpsB，source_start显式48后timeline36采60，应72；旧PTS把新源按A cadence求索引。frame mapper实际在[src/features/edit/core/timeline_v2.py:436](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:436>)接收旧PTS，不是仅保留了无害metadata。

已声明边界区别：actual controller [src/app/controllers/edit_controller.py:6856](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:6856>)可换fps48/window144，但源start仍按旧源帧相加（`commands.py:2985`），A在public1秒切48fpsB得到24而非48；group rational timebase/grid/resync已在§0 R23/F08声明未完成，不再冒充未声明问题。新增Undo/PTS残余没有被该声明覆盖。

实施：源stream identity改变时完整before/after clip snapshot，targetfps/PTS/cache/freeze等source-domain字段按目标元数据重建；command updated/JSON保留所有apply输入，Undo取原snapshot并在统一commit边界只bump发布revision。公共时间映射服务与grid共用，current→group→target以秒/rational ticks算。验收：边界和内部切角、A24→B48→A、CFR↔VFR、速度/反放/源窗/markers/transitions；三轮Undo/Redo+command JSON回放完整payload等价、clip几何不变，重新sync的旧shot policy明确。

### RF04 · P1 · 新U01领域command不能稳定重放（M），控制器未接是另一个已声明任务

位置：[src/features/edit/core/commands.py:6948](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:6948>) apply每次seq revision固定`before+1`；`:6976`undo把project revision当sequence revision；`:6967/:6995`返回 `(project,sequence,command)/(project,sequence)`，ProjectCommandStack `:7042`将返回值直接`replace(new_project,revision=...)`，不是兼容协议。

M：正常before sequence rev2/project7，apply/undo/redo的project `[7,8,9,10]`，sequence `[2,3,9,3]`，redo发布版本倒退。实际`ProjectCommandStack.execute(UpdateTimelineSettingsCommand...)`抛`TypeError: replace() should be called on dataclass instances`。这属于新领域实现自身缺陷，不能仅留“等待接controller”就关闭。

M actual controller：FakeSettings代替弹框，真实`_on_timeline_settings_requested`接受24→48，seqrevision3/project8，但TimelinePanel execute0/load1；与源码`:855`直接改缓存/ref一致。此接线仍是§0 U01明确未做，没有额外新bug计数。

实施：统一结构EditTransactionResult(project,sequence-map,updated-command)与历史栈协议；当前project/seq revision由canonical commit adapter分开单调bump，命令内容snapshot不携发布版本语义；controller只执行命令。验收：liveapply→undo→redo、多个sequence不同revision、JSON快照、旧AI plan；合法命令通过真实history栈，ref/cache/UI/render/audio/dirty一致。

### RF05 · P1 · R26二阶段仅path比较，旧probe仍能写新任务且吞掉新pending（M+S）

位置：[src/features/edit/ui/media_pool_panel.py:10676](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10676>)pending只保存 `(source_path,mtime,size)`，signal `:438`/worker`:468`只有proxy/source path/resolution；`:10687`先pop再比较source。

M actual handler：新任务重新登记同proxy+source path pending，旧probe resolution(111,222)回调被接受， `_apply_proxy_ready`被调用一次、使用新record的mtime200/size20配旧resolution，并消耗新pending；无generation/job身份可区分。另一个旧source不同的probe到达也先pop新record，后续合法(640,360)probe无法落（apply0）。第一次set/clear注销所有pending成功，不足以保证之后旧worker晚到时不碰重新登记的同key。

实施：两个阶段的record/signal都携project/session generation+asset+fingerprint+job id，`get`并核对完整record，只有匹配回调才`pop`；所有invalidate包括二阶段handle与产物；probe结果不可借用新job stat。验收：旧success/error/probe在新同path task前后乱序、相同ID/path跨工程、不同source共proxy、replace/delete/undo后新job、同path内容变更，旧回调零model/service write也不得清掉新pending。

### RF06 · P1（正确性）/P2（UI阻塞） · U04 Replace真实入口失败与跨模块payload尚不安全（M+S）

位置：[src/features/edit/ui/media_pool_panel.py:10129](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10129>)菜单slot同步get_media_type/probe；`:10131`捕获异常后退旧metadata继续replace；常规probe失败 [src/features/edit/core/project.py:4985](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:4985>)返回error字典不会throw，入口未判定error；`project.py:5230`以整份probed metadata替换asset.metadata，保留旧color_metadata与thumbnail_key。

M真实Replace handler（仅file/confirmation弹框替身，probe真实）：存在但内容为`not-a-video`的MP4，PyAV确定InvalidDataError，probe返回`{"format":"MP4","error":"probe failed"}`，入口仍改path、is_offline=false、revision7→8/history1、warning0。probe抛controlled异常时换PNG仍提交旧typevideo/fps24/total_frames100，warning0。成功2×2PNG能正确type=image/尺寸，但原multicam_group/future_asset metadata全丢，旧PQ/BT2020 color_metadata与old-thumb保留。导入路径有`_extract_color_metadata`（media_pool_panel.py:168），Replace并未执行同样的颜色刷新。

另一个同换源契约的M缺口：ReplaceMediaCommand新增`new_media_type`没有写入to_dict/from_dict（commands.py:6405/:6415）；live正确image，经JSON重放变video（`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_features_replace_contract.json`）。不是probe未传参数造成，是新command持久化字段遗漏，验收须覆盖command载荷重放。

S+受控M UI同步：probe在slot调用者上完成，没有worker；注入120ms probe后handler总时121.8ms（用于证明调用阻塞，不能把它冒称真实窗口卡顿耗时）。实际损坏小MP4probe约134ms；慢盘/长探测性能仍需真实条件，不推断精确最坏耗时。

实施：后台ReplacePreflightWorker返回明确success/diagnostic、type、source metadata/fingerprint和usage兼容方案；error/null/nondecodable不commit，给可行动提示；新source-derived字段替换、用户日志/feature annotations/未知extensions独立保留，multicam归属及其他模块refs按policy迁移；新color来源更新，旧source cache/thumbnail标识失效；每次worker completion重新检查project/asset/revision并作为一事务commit。验收：坏/空/短/无音频/跨类型/HDR→SDR/新未知metadata，正常/失败/取消/切工程/并发replace，失败零历史/零revision变化，Undo全payload恢复；short源/跨类型usage处置仍是§0已声明待做工作。

### RF07 · P1 · R27取消仅接analyzer函数，导出响度阶段仍不响应（M+S）

位置：[src/features/edit/core/timeline_export.py:4381](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_export.py:4381>)与`:4399`两次analyze_loudness未传progress；后续`:4407`写报告、`:4412`发布last_report；正常decode loop已有cancel检查不能替代这里。

M真实`TimelineExporter._render_audio_master`，合法Bus轨+真实CPU AudioGraphRenderer/PCM、spy analyzer在首次入口将 `_cancel_requested=True`（模拟这一阶段收到取消）：第一/第二scan均progress=None；第二scan已见cancel=True仍执行；报告write1次、last_report发布、返回PCM=True。外层export可能最终返回cancelled，本证据不声称最终视频编码必成功，但取消期间剩余响度扫描与报告发布没有被阻止。

实施：export把cancel predicate传入analyzer，块前/后检查，LoudnessAnalysisCancelled显式传播成取消，不被宽泛except吞成“分析失败继续交付”；normalize/postscan/report publication前再核验取消；临时报告原子发布。验收：长PCM first/post scan中途cancel无第二scan/报告/结果发布，延迟上限；preview/delivery不同调用者不因取消误报LUFS。真峰正确性边界已修，不重复开放尾峰与分块bug。

### RF08 · P2 · R28未知字段roundtrip已修，但Nest重建track仍丢（M+S）

位置：[src/features/edit/core/nested_sequence.py:386](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/nested_sequence.py:386>)通过全新EditTrack列字段copy，`:407`未传新`_extra_fields`；相比track `_rebuild`新保留（timeline_v2.py:2613），此重构性入口漏覆盖。

M：EditTrack.from_dict含future_track={x:1}，父普通roundtrip保留；build_nested_from_selection后child track.to_dict无future_track。不将既有未知archive module保持能力误说成仍全面失效。

实施：子track从source immutable track用dataclasses.replace构造，只显式替换ID/clips/transitions/time-domain/route及需要解锁的字段，extras深拷贝；逐一核查类似freshconstructor writers。验收：load未来字段→Nest→undo/redo→保存/重载保持，Bus闭包track同测，unknown字段不被执行，已知current字段优先。

## 3. 明确已声明未做的工程/时基任务（不混入隐藏新bug数）

| 任务 | 当前确认 | 实施/验收仍需 |
|---|---|---|
| R25统一commit adapter | 新ProjectStack7→8→9→10已修；但实际`MediaPoolPanel._on_relink_asset` `:10209`调用纯helper后emit，host `file_controller.py:1601`直接接受。M换新path后panel/host revision均7。更严重的G相关补充：actual `_on_timeline_switched`（edit_controller.py:502）调用`set_active_timeline`（project.py:2465）全新EditProject漏revision，7→0；实际create dialog→create handler→switch同样7→0，随后两次Append可变2。create_timeline helper也0，with_timeline_ref_added保持7。 | 修复纯helper统一保留revision（含导航无内容变更也不得归零），内容commit才单调+1；将Relink、同步/导入等writer统一提交，derived政策明确。切页/切timeline/创建/重开后旧AI plan不能重新匹配，project不会从高版本倒退。此严重残余仍合并R25一个根因，不另增计数。|
| U01 controller/history接线 | actual settingshandler0execute/1load，仍直写seq/ref；领域bugRF04需先修。 | canonical结构transaction，一步Undo恢复settings+全时域payload。|
| U02 Multicam工程事务 | `edit_controller.py:6759`先stamp assets再检查目标锁轨，sync`:7109`直接更新metadata，缺projectcommand/generation/history；已syncclip migration未做。 | preflight→project/sequence事务，创建失败零stamp，sync可Undo+dirty/revision，旧worker不落新session。|
| U03 Nest工程事务 | `edit_controller.py:7251`直接增asset/ref，`:7259–7262`先写child/parentcache再调用timelinechanged，无法看到旧revision；load不执行历史命令。 | 跨project/sequence transaction、stable IDs/refs/navigation、Nest→Undo→Redo无孤儿且revision单调。|
| R23/F08公共时基/grid/resync | targetfps/window已改善；group目前整数source-offset叠加、grid仍timeline帧直接相加；重syncBoffset10→20现有clip不动，切回A old公共40得到30。 | rational公共时间与source-specific offset，真实mapper+grid一致；重sync迁移旧shot或冻结sync version，明确音频policy。|
| U04 usage处置 | type/metadata探测有实现；short源/跨类型使用窗/clip时基校验和用户批准仍未做；RF06失败边界不可用该声明掩盖。 | source handles及usage-index预检、给明确crop/retime/reject proposal、原子commit与undo。|

## 4. F01–F16当前真实建设范围（S，全部已有明确未实施声明）

此处保留当前“已有基础→尚缺闭环”的区别，不再把现成multicam、nested、字幕、Stem、WSOLA、ASR整体列为缺失。本批主要是修复，不是完成16个专业工作流。

| 工作流/旧ID | 当前基础与仍缺范围 | 与本轮修复的依赖 |
|---|---|---|
| F01 Source高级 | 三基础按钮及新linked ripple planner存在；source/record3–4点、单端/全源默认、Replace Edit/Match Frame及统一stream policy仍无闭环。 | RF02/03与handles服务，先冻结source request/patch，再建高级UI。|
| F02 Trim Monitor | `trim_numeric_dialog.py:157`的dialog仍TC/Δ/headroom QLabel（`:170/:171`），不是真实双/四画面。 | 公共mapper/proposal+异步decode generation；窗口验收由主审计执行。|
| F03 动画编辑工作区 | shared关键帧/command与属性easing等局部控件已有；Edit独立Dope Sheet/Curve多点/Bezier拖动与复制工作区仍未建。 | R21原例已修可推进；需要locked/selection/context/history闭环，不能把局部Curve下拉当整工作区。|
| F04/F05 高质retime | 离线WSOLA已有；registry preserve_pitch仍PARTIAL且实时resample，光流仍UI隐藏（properties.py:3759）/无完整backend。 | realtimeaudio streaming/seek reset与可选renderbackend两条工程独立排期。|
| F06 音频操作 | gain手输存在；`timeline_view.py:11249`仍明确Normalize按钮待接；sidechain_ducker仍core独立，无graph/UI route闭环。 | RF07+可取消worker、gain建议command；preview/export/Stem一致标准PCM。|
| F07/F08 Nest/Multicam成熟化 | 新Bus闭包/目标fps补丁是局部基础；registry仍宣告Nest非1x音频skip/子transition无mapping/无Undo、多机位静帧/noTC-marker/audio切角/resync管理。 | RF03/U02/U03后递归audio time-map、动态grid decode预算/音频policy、navigation/unnest。|
| F09/F13 字幕表/人工文本剪 | `caption_workspace.py:387`仍NoEditTriggers，`:534`split中点；ASR/transcript词级模型与AI已有，直接Text/Speaker/Status/time delegates、逐词split/批译及人工word Lift/Extract未建。 | RF01+真实word evidence/字幕graphic绑定+linked/sync事务，勿混同已有属性编辑。|
| F10/F11 proxy与历史中心 | 有generation/proxygen/policy、固定.bak/autosave恢复；panel progress仍日志，未保存可控worker队列UI；`edit_project_io.py:1621`固定文件名，非多版本历史。 | RF05/RF06后queue manager/cancel/retry/迁移；version manifest+restore copy+retention。|
| F12 互通增强 | FCPXML/OTIO受控子集已有；AAF/EDL与完整保真矩阵/渲染替代仍未建。 | 公共source time-map先冻结，外部软件回导gate不能用单元tests替代。|
| F14 能力发现统一 | Stem已AVAILABLE；多数status/notes仍人工声明，没有绑定全部actualaction/backend动态契约。 | 本次RF01/02/05/06/07不能继续对外标整体修完；help/AI/UI状态同源。|
| F15/F16 精确交互 | playbackfollow已有；本批未建drag-edge timer；`timeline_view.py:3864`/`:4460/:4468`nudge仍字幕，A/V精确Nudge/Move UI未建。 | interaction/planner预检与clock域/locked/multi/linked/markers/keys一致。具体GUI行为由交互分工验证。|

## 5. 建议实施次序与关闭门槛

1. 先修RF01/02/03：真实词级、全stream fps/PTS factory、source-swap完整历史；测试以真实当前模型与handler为主，保留已通过旧常规例。
2. 统一结构commit协议RF04+已声明R25/U01/U02/U03，再接高级工作流；不能把一个不兼容command交给未来adapter后继续说领域事务已完成。
3. 修RF05/06：跨阶段严格job identity、Replace后台成功预检/metadata迁移/失败零提交；容许同路径任务乱序且不抢删新pending。
4. 将已正确的analyzer取消接口贯穿实际export RF07；保留取消侧零结果发布验收，含post-normalize/report阶段。
5. RF08系统核查freshconstructor对新字段的保留，再按F01–F16高频工作流分批建设。

本报告只支持上述M/S结论。真实窗口、长时A/V/设备、三方互通和GPU像素/资源门禁继续由主审计保留为独立验收，不因30个focused成功或§0状态勾选而冒称全页面完成。
