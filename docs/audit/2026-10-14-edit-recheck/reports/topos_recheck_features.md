# 功能、工程状态与兼容性复查（子报告）

根目录：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。已读 `.clinerules`、`AGENTS.md` 及旧计划§0。基线 `55351a5f45d5a34ff2fc91a3cb9eee53581a889b`，复查 HEAD `833dcd382`。仅审查与 `/tmp` 模型探针；没有修改生产代码，也没有启动GUI。证据 `M` 为真实模型/命令/实际handler在替身宿主上的执行，`S` 为完整静态调用链。替身宿主不等于真实窗口验收。

复现：项目根运行 `.venv/bin/python /Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_features_probe.py > /Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_features_probe.json`。脚本覆盖实际 `_with_timeline_fps`、source view builder+真实command、`set_project`/proxy completion、project stack+file controller提交、multicam sync handler。所有结果在 JSON 中。

## 1. 当前完成度（本范围）

| 旧ID | 当前判定 | 证据与边界 |
|---|---|---|
| FE-B01 | 部分完成，不能关闭 | marker、媒体动画及track/master automation重映射入口已添加；真实 `subtitle_tracks` 未接入；word timing没有转换；Bezier绝对handle dx未缩放，见 F-R01/02。|
| FE-B02 | 部分完成，旧§0明确未实施Undo | 设置序列/project revision+dirty已接；仍直接写缓存/ref并load，没有UpdateTimelineSettingsCommand。见 F-U01。|
| FE-B03 | 部分完成，新增A/V分支有回归 | 空轨常规插入可A/V成对；纯音频可落A；内部插入回退video-only，Overwrite在已有音床抛异常，Append两轨独立末尾造成错位，48→24fps插入时长错误。见 F-R03。|
| FE-B04 | 本报告不单独关闭 | 常规mixedFPS领域mapper已有focused tests，本次source新A/V分支绕过该helper，仍可破坏时长。核心命令侧复查另报。|
| FE-B05 | focused证据已修 | range_changed与I=O单帧支持有现有focused测试；未宣称真实窗口Clear/open/playback验收通过。|
| FE-B06 | 等FPS常量offset往返已修，剩余timebase缺陷 | 当前A→B→A常量offset测试通过；目标FPS元数据没有更新，grid仍用timeline帧直接相加；重同步既有片段公共时间会跳，见 F-R04。|
| FE-B07 | 明确未实施+仍有失败半提交 | 创建先stamp资产，再检查可用视频轨；同步直接写metadata，无project command、dirty/revision/generation。见 F-U02。|
| FE-B08 | 部分完成 | 音轨volume/pan/mute/FX等复制+动画平移通过；Bus路由闭包没有复制或重映射，见 F-R05。|
| FE-B09 | 明确未实施 | Nest创建仍直写project/child cache/parent，非历史事务；同样漏revision。见 F-U03。|
| FE-B10 | 纯helper修复完成，提交链仍漏revision | relink/replace/update_proxy helper保持7，现有测试通过；project stack→host提交/undo/redo仍保持7，见 F-R06。|
| FE-B11 | 单asset注销token防护已修，跨project/后续probe残余 | invalidate后success/error被拒，tests通过；set_project不注销旧tokens，复用ID/path旧回调污染新project；proxy resolution第二阶段无generation检查，见 F-R07。|
| FE-B12 | 明确未实施 | UI新建ReplaceMediaCommand不probe；jpg替换仍video/旧fps/旧metadata，见 F-U04。|
| FE-B13 | 已有timecode focused修复通过 | 29.97字幕例与领域timecode测试通过；本次没有真实窗口所有label的DF/NDF设置验证。|
| FE-B14 | Stem声明修复完成 | registry现在AVAILABLE+具体UI/后端证据；22个registry测试通过。多机位/nest/preserve_pitch仍诚实PARTIAL。|
| FE-B15 | 部分完成且新估算器有边界回归 | 中部intersample峰与随机97/whole分块测试通过；文件尾漏扫描，首块初始化依赖chunk，见 F-R08。|
| APP-01 | timeline命令提交部分已修 | execute/undo/redo序列revision单调+正常controller接收前进时project+1已修；媒体池project命令、multicam sync、Nest直写路径仍绕过，见 F-R06/F-U02/F-U03。|
| R02 | ClipInstance已修，其余typed模型仍不完整 | clip未知字段roundtrip+edit保留已复现成功；track/sequence未知字段丢失，archive其他module保留已有tests通过。见 F-R09。|

## 2. 确定剩余缺陷/新增回归与可执行计划

### F-R01 · P1 · FE-B01残余：真实字幕轨与词级时间没有改变FPS

位置：`src/app/controllers/edit_controller.py:727` 有字幕cue缩放代码，但 `:762–770` 的sequence rebuild只传 `video_tracks`、`audio_tracks`，未传 `subtitle_tracks`。模型字幕严格放在 `TimelineSequence.subtitle_tracks`（`src/features/edit/core/timeline_v2.py:3136`、`:3185`）；因此字幕分支在真实合法字幕轨上不会被调用。即使接上字幕轨，现有 `replace(sub)`仍没有处理 `caption_metadata.word_timings`，后者明确是timeline帧坐标（`src/features/edit/core/captions/model.py:435`）。

M：24fps cue24–48、word24–48改48fps后仍cue24–48、word24–48（应48–96），显示提前一半；源媒体帧坐标应保持。

实现依赖：统一sequence timebase transform（领域服务/command），遍历三个track组；cue、word timing及caption animation按字段所属域转换；保留未知caption metadata，避免只缩UI值。验收：24↔48与24000/1001↔30000/1001，同实际秒的cue/word/graphic binding一致，roundtrip后误差≤1目标帧，源入出点不改，一步Undo恢复全payload。

### F-R02 · P1 · FE-B01新变换残缺：Bezier曲线形状改变

位置：`src/shared/keyframe.py:142–164`，`time_scaled`只改key.frame；绝对data-space `handle_left/right=(frame_delta,value_delta)`（`:65–69`）的x未随fps变换。实际evaluate优先绝对handle（`:974–975`）。controller clip/effect/track/master animation都用此helper（`src/app/controllers/edit_controller.py:679–704`、`:723`、`:757`），因此影响所有相关曲线。

M：0→24帧Bezier，right handle(20,0)、left(-2,0)，24→48后0.25秒处值从0.03399365变0.09685793，right handle仍(20,0)，应(40,0)。线性曲线现有测试过不能证明Bezier保真。

实现：key.frame与绝对handle.x同倍率变换，normalized legacy bezier不改；处理downsample重复key时的形状/去重政策；Vec/Color/Bool轨统一helper或显式诊断。验收：clip/effect/track/master上broken/smooth/auto、绝对/legacy handles，采样同秒曲线值误差小于浮点容差；多次切FPS不累积失真。

### F-R03 · P1 · FE-B03新A/V接线回归：时长、落点与覆盖策略分裂

真实链：`src/features/edit/ui/edit_page.py:2688`、`:2721`、`:2753` → TimelinePanel → `src/features/edit/ui/timeline_view.py:16001`、`:16148`、`:16263`。

1. `timeline_view.py:16075` 用 `source_duration/speed`，漏sourceFPS→timelineFPS换算（同时common又正确带fps元数据，造成dur与mapper不一致）。M：48fps源48帧插24fps序列，V/A时长48，应24。这是fix-B4新增A/V分支的回归，单V基础mapper不会得到此时长。Overwrite/Append分支还没有带source/timelineFPS。
2. `:16112–16118`，内部重叠失败回退单V波纹插入。M：已有V/A0–96，source insert at24，新V三段，新源音频0个。不是完整A/V Insert；旧§0错误写作“Overwrite/Append内部播放头回退”，真正回退的是Insert。
3. `:16226`，Overwrite音频用AddClip，未用覆盖planner。M：音床0–96上at24 overwrite，抛`ValueError: New clip overlaps with oa at [0,96)`，macro未发布所以没有半提交，但正常源覆盖操作失败；空轨test只证明空轨。
4. `:16307`、`:16323`、`:16332`，Append分别以各轨尾点放V/A后回填双向链接。M：V尾96、A尾48，新Vstart96/Astart48仍双向linked，立即错位48帧。`InsertLinkedAssetCommand`的注释保证同start，但新Append宏没有这项保证。

实现依赖：SourceEditService/不可变stream patch planner，复用`timeline_duration_for_source`，确定一个共同record位置+fps/source window，Insert为同步ripple patch、Overwrite为V/A覆盖patch、Append为共同append位置；在preflight检查targets/locks/handles/clip collision，一次macro提交，失败明确用户提示，不用任意ValueError静默video-only回退。验收：空轨/内部V+A/错开尾点/音床/无A轨/锁A/纯音频/48→24/23.976→29.97；V/A时间窗、source map及links一致，Undo/Redo完整快照，不吞异常进Qt槽。

### F-R04 · P1 · FE-B06/F08残余：多机位timebase与已同步片段不统一

位置：`src/features/edit/core/commands.py:2858–2864` 用当前source帧减active_offset加target_offset直接当target source帧，目标asset FPS/PTS没有输入；`UpdateClipSourceCommand` `:2686–2696`还按timeline_duration×speed构造源时长，fps元数据继续沿用旧角度。grid `src/features/edit/core/multicam.py:275–306`直接把timeline帧加source帧，也不使用clip mapper；controller `src/app/controllers/edit_controller.py:6817–6827`调用此函数。

M：24fps A，timeline24fps at24切48fps B，得到target source_start24、fps24、duration72，应48、48、144。各角度等FPS测试覆盖不到。控制器建组 `edit_controller.py:6640–6659`也把各源metadata duration_frames直接取min，不是公共秒时长，且InsertClipCommand没带sourceFPS。

M（已sync）：既有B片段timeline20/source30、oldOffset10；实际 `_apply_multicam_sync_offsets`改BOffset20而不迁移clip（`:6978–6984`）；at40切回A得到source30，之前公共frame40。registry `capability_registry.py:365`已明确“重同步不追溯平移已放置片段”，此项是已声明未完成的成熟化边界，不称为未声明的新功能。

实现依赖：组定义明确rational公共时基/秒或统一ticks，角度保存sourceFPS/offset时基；公共位置→目标source映射复用真实mapper，refreshgrid与program一致；重同步事务明确保持public time/shot时间的政策并更新既有clips或固化原sync snapshot/version。验收：不同fps+speed/reverse、选不同reference、首次同步/重同步后已有clips和A→B→A相同公共位置、target handles越界拒绝/提示；grid/program同源帧，组、clips与revision一次Undo恢复。

### F-R05 · P1 · FE-B08残余：Nest子序列的Bus路由不自包含

位置：`src/features/edit/core/nested_sequence.py:382–383`原样copy output_bus_id/sends；`:404–411`只复制有selected clips的audio track，因此无clips Bus完全不进入child，也无bus-ID重映射。

M：a1输出到bus并send bus，选择a1的clip创建child，child仅`child-a0`，output_bus_id/send仍`bus`，child中没有bus；进入child/移至另一父序列就不具有原Bus拓扑。父原bus尚在时flatten可能继续借用父bus，本报告不把所有父PCM都断言为静音，确定问题是child路由悬空与工程语义不完整。

实现依赖：选中轨对应的output/send Bus依赖闭包，包括bus→bus、Bus FX/automation/mute/solo；用完整track ID map重写routing/sends；明确如何避免父/子重复gain，child master是否保留/只由父master承担。验收：两级Bus+send、solo closure、独立播放child、同child多实例/不同父序列、save/reload，PCM与原等价且无悬空bus，不串instance路由。

### F-R06 · P1 · APP-01/FE-B10残余：媒体池工程提交仍不推进revision

位置：`src/features/edit/core/commands.py:6755–6777` ProjectCommandStack只apply/push；undo/redo `:6779`/`:6807`同型。`src/app/controllers/file_controller.py:1600–1603`project_changed直接接收新project+dirty，不推进revision。新FE-B10helper保留revision是正确的，但没有覆盖这条实际提交边界。

M：ReplaceMediaCommand→真实ProjectCommandStack execute/undo/redo→真实FileOpsMixin handler，revision序列 `[7,7,7,7,7]`。旧base_revision=7的AI计划看不到换源/撤销；同类project命令需要统一审计，不能只为replace单点补+1。

实现依赖：一个project commit adapter覆盖project-level command、timeline-level command、结构事务与AI，共享验证、revision/ref/cache/publication；衍生proxy/offline/cache状态制定显式revision政策。验收：rename/import/delete/relink/replace/Undo/Redo/structure操作后旧AI plan冲突，一次macro只+1，undo不回旧revision，拒绝与derived no-op不增；重载仍单调。

### F-R07 · P1 · FE-B11残余：跨工程与代理二阶段回调仍可污染状态

位置：`src/features/edit/ui/media_pool_panel.py:5271–5278`切project只清timeline预览pending，没有清`_proxy_jobs/_proxy_job_tokens/_pending_proxy_meta`；clear_project `:5417–5439`同样无proxy注销。completion `:10564–10574`只校验token与path，未带project身份/fingerprint。

M：在p1登记asset/path/token17，实际set_project(p2)复用asset ID/path，再送p1旧token17回调，p2 asset被设ready并挂旧proxy。现有tests只覆盖显式`_invalidate_proxy_job`后旧callback拒绝。

S+M：resolution阶段 `:10634–10650`仅以proxy_path取pendingmeta，无token/project检查；`:10652–10693`写DiskProxyService/FrameProviderService。替换为spy服务执行真实handler，旧pending项能触发mark_ready一次。服务 `src/shared/media/disk_proxy_service.py:382–408`是公开写API，不自行判定project/source freshness。删除/换源/切工程期间的晚到probe可重新写旧缓存；需全链token，不只第一success/error。

实现依赖：job record包含project/session generation、asset identity/source fingerprint（stat+必要hash）、job token；project close/switch/delete/replace/Undo注销及cancel；resolutionprobe沿用record；失败/取消清pending与产物，考虑旧worker产物与新job同proxy_path竞态。验收：跨project复用ID/path、同路径内容变化、delete→undo、新job覆盖旧job、success/error/probe乱序，全链旧回调无任何model或service write；旧文件不误删新产物。

### F-R08 · P1 · FE-B15新估算器边界回归：文件尾漏峰与首块分区依赖

位置：`src/features/edit/core/loudness_analyzer.py:63–71`首块镜像历史取决首block内容/长度，n=1走零历史；`:88–95`丢弃卷积尾guard并指望下一block，`analyze_loudness :192–204`循环结束没有flush/finalize。

M：64样本最后4个是 `[0,1,1,0]`，estimator峰仅0.0154269；report退回sample peak0dBTP。为同有限序列尾加24个零flush后插值peak1.2379007（约1.85dBTP）。随机512的最大峰在中部，现有whole/chunk test因此通过。常量64：chunk64/2峰1.0002727，chunk1峰1.1190867；头部pulse在chunk1/2/3/4得到1.23790/1.21741/1.31954/1.22247。同音频报告不应随block_size改变。

实现依赖：有明确定义的stream start/end padding+filter delay，初始化缓冲不依赖caller分块，finalize drain待处理tail；采样率/通道/非有限值参数检查，报告声明估计方法/倍率/参考版本与实际精度。验收：whole与任意分区（首block1、短片、最后block<delay）一致，头/中/尾intersample脉冲、DC/高频/多channel、参考波形对照；正常/取消/失败不把samplefallback伪标完整true peak。

S取消缺口：`analyze_loudness :161`无cancel/progress接口，`:192`每块无取消检查；`timeline_export.py:4381`、`:4399`同worker连续测量前后两遍。出口解码loop `:4209`已有cancel，但不能证明响度阶段响应。需将callback/token贯穿scan并在post-normalize/reports publication前再检查；验收10/60分钟PCM中途cancel的延迟上限、零残留报告/输出。该项静态未实测，不标GUI失败或泄漏。

### F-R09 · P2 · R02残余：Clip未知字段已保留，但sequence/track仍丢

位置：`src/features/edit/core/timeline_v2.py:2891`/`:2917` EditTrack与`:3636`/`:3659`TimelineSequence typed读写未留extras。ClipInstance `:1254–1283`已保留，真实edit后仍保留。

M：未来fixture包含future_sequence、future_track、future_clip，roundtrip分别false/false/true；clip移动后future_clip仍true。Archive未知模块保留不等于已知module内部typed对象保留。

实现依赖：统一版本/extension payload策略覆盖track/sequence/marker/transition/asset/ref/effect（不要无版本盲目执行未知类型）；copy-on-write/深拷贝及既有helper `_rebuild`保持extras。验收：未来字段fixture在load→edit→undo→save/reload保留，已知键优先，caption_graphics/Color/Composite/AI的已有payload不丢；非支持类型给诊断。

附：JSON兼容入口 `src/features/edit/core/serialization.py:251`/`:544`漏project.revision，`from_json(to_json(project revision7))`实际变0。生产ZIP转换 `edit_project_io.py:275/:548`有revision，不能误称主要ZIP重载全丢；但兼容JSON/使用该serializer的caller仍需补字段迁移及focused验收。

## 3. 旧计划§0已声明但仍未实施，保留独立排期

| 新追踪 | 优先级 | 当前确定缺口/位置 | 实现依赖与验收 |
|---|---|---|---|
| F-U01（FE-B02） | P1 | `edit_controller.py:828–860` 设置直接写seq/ref/project，仅revision+dirty；未有UpdateTimelineSettingsCommand。 | timebase transform+project commit adapter；设置/clip/caption/automation payload一步Undo，旧AI plan拒绝、cache失效、renderer/audio/UI同步；变FPS后再undo旧编辑不得恢复成混合timebase。|
| F-U02（FE-B07） | P1 | `edit_controller.py:6624–6639`建组先写assets才检查lock；`:6960–6988`同步直接写metadata无project history/dirty/revision；worker仅存group_asset_id `:6884`，无session generation。 | Create/UpdateMulticam project command+预检，worker结果带project/group revision；失败零资产stamp、同步Undo完整、切工程旧worker不落新project。|
| F-U03（FE-B09） | P1 | `edit_controller.py:7102–7118`增asset/ref/childcache/parent并load，不执行command；先把父cache写新再调用_on_edit_timeline_changed，比较不到旧revision。 | 跨project+seq transaction；稳定IDs/引用计数/active导航随Undo/Redo，父/子/assets无孤儿，project/seq revision单调。|
| F-U04（FE-B12） | P1 | `media_pool_panel.py:10115`创建ReplaceMediaCommand只传asset/path；`project.py:5241`保留旧media_type/metadata；实际video→jpg仍video/fps24。 | 后台probe+usage-index source-window/type兼容preflight+用户可审查处置+command提交，token和fingerprint沿途校验；短源/跨类型正确metadata/明确裁窗或拒绝，Undo旧资产全恢复。|

## 4. 当前具体功能缺口（按现有状态整合，不重复旧22行）

| 工作流 | 已有 | 当前剩余 | 建议排期/验收 |
|---|---|---|---|
| 源编辑成熟化 | Source I/O、Insert/Overwrite/Append、目标轨与基础A/V命令 | 本报告F-R03先修；3/4point、record I/O约束、Replace Edit、Match Frame入口仍无；完整stream patch未建。 | 先source planner→上述三操作闭环，再增高级入口；全源/单端range与source/timeline焦点、混合FPS/link锁回归。|
| 多机位/嵌套成熟化 | 静帧网格、波形sync、1–9切角；Nest视频预览、1x音频flatten/Stem共享、保存/进入子序列 | registry `capability_registry.py:343–396`仍明确PARTIAL：静帧250ms、无TC/marker同步、audio不切角、resync不迁移；Nest speed≠1x音频skip、子audio transitions不映射、创建无Undo；本报告timebase/Bus缺陷先修。 | 工程事务→公共mapper/Bus闭包→nested recursive audio time-map与dynamic decode预算→breadcrumb/unnest/同步功能；preview/export/Stem同PCM与帧、3层nest与多个实例。|
| 音频专业处理 | clip gain命令、离线LUFS/WSOLA/ducker core、mix/Bus/Stem | `timeline_view.py:11082`明确Normalize分析按钮待接，当前手输dB；sidechain_ducker.py:auto_duck无UI/audio graph生产caller；registry `:331–339` realtime preserve_pitch仍变调resample。 | 先峰值+可取消analyzer→clip/track LUFS/peak gain worker与单步Undo→注册式sidechain FX/route/UI→streaming保调；标准PCM和preview/export一致验收。|
| 字幕工作区 | cue表/搜索替换/基本split/merge/属性编辑/字幕交付；词级model及provider已有 | `caption_workspace.py:387`NoEditTriggers，表格Text/Speaker/Status/时间不能直接提交，`:538`split仍中点；词级split/文本选词剪辑和批译完整UI仍待闭环。 | 复用caption commands+word证据/linked edits+revision worker，先修F-R01，补table delegates/词级split；locked/status、word边界、Undo/translation stale回调。|
| 代理队列与版本历史 | proxy generator/token/policy与disk cache；固定.bak/autosave恢复 | progress `media_pool_panel.py:10529`仅log；panel不保存worker可控队列/取消重试UI；`edit_project_io.py:1621`固定 `<project>.topos.bak`，没有多版本列表/恢复副本。 | 先F-R07生命周期，再集中queue+cancel/retry/容量；异步snapshot+版本retention/restore copy，取消/磁盘满/同路径job竞态。|
| 能力发现 | Stem AVAILABLE已修，partial范围说明相对诚实 | `build_capability_registry`没有对所有入口/菜单/实际后端自动做契约检查，notes仍人工字符串；不能从Stem一项推出功能补齐全部完成。 | 从actual entry/action/backend证据生成帮助/AI manifest，自动契约tests、可选依赖单项降级；F-R08改善后true_peak方法/局限同步声明。|

该表是本范围当前具体闭环缺口。Trim Monitor、dope sheet/curve、edge scroll/nudge、焦点/Inspector等交互细节由主审计其他分工核查，不能仅因为旧FE-F/UI-GAP表列过就再次当新发现。

## 5. 验证汇总与边界

现有focused测试累计 **184 passed**，都是无可见窗口执行：

- 46：`test_multicam_nested_timecode_truepeak.py`、`test_revision_and_source_av_routing.py`、`test_linked_ripple_revision_proxy_jobs.py`、`test_capability_registry.py`，2.43s。
- 72：`test_project_module_preservation.py`、`test_edit_project_composite_state_preservation.py`、`test_nested_audio_flatten.py`、`test_loudness_delivery.py`、`test_source_time_domain_mapping.py`，4.49s。
- 66：`test_multicam_sync.py`、`test_multicam_production.py`、`test_nested_sequence_production.py`、`test_new_timeline_dialog_settings.py`，3.22s。

新增probe失败条件与这些成功结果共存。现有测试验证常规单独路径，本报告证明真实字幕track、边界峰、不同FPS A/V、错开轨尾、占用音轨、跨project回调等组合未被覆盖。已知通过路径不要重做，新增失败fixture围绕上述可重复输入；修复之后再跑相关范围，真实窗口/音频设备/跨软件门禁另列，不冒充本次通过。

优先顺序：project/source/timebase原子提交及F-R01/R02/R03/F-U01 → multicam/Nest结构事务F-U02/U03与映射/Bus → proxy全链generation/Replace probe → true peak正确边界+取消 → typed兼容与后续工作流功能。实现时仍走features service/command/facade与既有audio/render工作线程，UI不直接新增GPU操作。
