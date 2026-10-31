# 剪辑功能完整性、页面状态与实现方案审计（分项报告）

日期：2026-10-13（Asia/Shanghai）。项目根目录：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。

本报告是主审计的只读分项结果，已读取 AGENTS.md 和 .clinerules。未改生产代码、未启动 GUI。标为“已复现”的项目来自执行真实模型/现有方法，不等同于桌面鼠标测试。标为“静态确认”的项目有完整调用链证据；外部模型、录音硬件、第三方 NLE 未实际调用。主报告应合并主 Agent 的 Qt 交互结果，并保留这一区分。

## 当前真实能力矩阵

| 领域 | 当前已有能力和入口 | 仍缺或限制 | 状态 |
|---|---|---|---|
| 工程与序列 | 创建/打开/保存/另存、自动保存、发现 autosave 后恢复；多个 Timeline、序列格式设置、媒体池切换；工程收集/整合 | 设置和部分跨工程事务未统一 Undo/revision；EditProjectIO 单固定 .bak，不是多版本历史浏览 | 已有，部分一致性待修 |
| 素材池 | 列表/网格、缩略图、搜索、普通/智能 Bin、文件/文件夹后台导入、重命名、评级/旗标/日志、复制剪切粘贴、多选删除、重链接、替换媒体、离线状态、代理策略/生成/删除；AI 分析徽标 | 换源未探测新元数据/源范围；代理旧任务回调隔离有漏洞；代理进度没有可视化管理 | 已有，部分风险 |
| 源监视器 | 双击源素材、播放/逐帧/定位/拖动预听、GPU 取帧和 CPU 后备、In/Out 标记；时间线工具条 Insert/Overwrite/Append | 源编辑不创建视频关联音轨/音频路由；混合 FPS 时长错误；必须同时设 I/O 且不允许单帧；Clear 后按钮未同步；没有三/四点剪辑、Match Frame、Replace Edit 入口 | 部分 |
| 时间线常规操作 | 多轨、拖放/多选/链接音视频、移动/复制/跨轨/新建轨、吸附、锁轨与同步锁、分割、滚动/波纹/滑移/滑动/提取、Undo/Redo、范围/标记/章节、波形/缩略图、间隙、缩放、变速条 | 鼠标可用性由主 Agent 实测；部分源编辑绕开 A/V 原子事务；不足不能写成“没有波纹/滑移/变速” | 已有 |
| 节目监视器 | 时间线输出、运输控制、循环/时间码、画面变换手柄、字幕变换手柄、构图 Guides、安全区、离线提示、播放 HUD | 修剪预览接线有，但未见 2/4 路编辑点比较监视器；时间码显示未复用正式 NDF/DF converter | 已有，增强待补 |
| 属性与效果 | Transform、Crop、Opacity、Blend、源 RAW、速度/倒放/保调选项、Clip/Track/Transition 参数；效果开关/删除/排序；关键帧按钮；生成器/调色/合成联动 | 剪辑动画只有参数关键帧按钮，未见 Edit 页曲线/Dope Sheet、多点选择/复制粘贴/手柄编辑入口 | 已有，编辑体验部分 |
| 速度 | 恒速/倒放、速度点/分段速度、smooth ramp、定格、nearest 和 frame_blend；SpeedParams 序列化 | optical_flow 只在模型中允许，后端未实现且 UI 隐藏；实时 preserve_pitch 仍变调，导出 WSOLA 保调 | 部分 |
| 音频 | A/V 链接、Clip gain/fade、轨道静音/独奏/音量/pan、FX、Bus/Send/routing、音轨与 Master 自动化、混音器、播放图、ADR/录音/take sheet、BWF、Stem 导出 | 自动响度 Normalize 尚未接 Audio Gain UI；sidechain auto duck 只有 DSP，无生产调用；“True Peak”目前是样本峰值近似 | 已有，专业补齐 |
| 字幕 | 外部 SRT/VTT/ASS/SSA/TTML 文件导入，字幕轨、文本/样式属性、viewer 变换手柄、字幕 graphics/template/effects、时间线操作、查找替换/合并/拆分、质量检查、语言标记、语音转字幕、字幕转语音、多格式/多语言交付、burn/mux/sidecar | 工作区表格只读且英文文案；Speaker/Status 无直接编辑；Split 固定中点；人工批量翻译入口缺（AI 服务已有翻译 provider） | 已有，工作流部分 |
| 多机位 | 媒体池多选创建组、1–9 和 angle tile 切换、切换点可 Undo、后台波形相关同步/结果确认、角度静帧网格、保存重开 | 再次切角偏移累积；同步不走脏标记/Undo；网格每 250ms 静帧，无多路实时；无 timecode/marker 同步、音频 follows video、重同步已有切点策略 | 部分 |
| 嵌套 | 从选择创建、进入子序列/下拉回父、循环阻断、preview/export resolver、嵌套音频 flatten、保存重开 | 创建无 Undo；构建丢音轨状态并不平移动画；速度≠1x 嵌套音频被跳过、子轨转场未展开；无 breadcrumb/展开/替换嵌套入口 | 部分 |
| 场景切分 | 素材/clip 检测、后台 worker、候选图/预览/校正、overlay、切分应用、子素材、缓存/工程序列化/AI 分析共享 | 外部复杂样本检测质量未实测，需数据集验收，不能宣称整体缺失 | 已有 |
| 交换格式 | File 菜单 FCPXML/OTIO 导入预览/确认/报告、导出映射报告、离线素材处理；CSV Cue Sheet/BWF/Stem 服务 | FCPXML/OTIO 受控子集，复杂速度/第三方 FX/multicam/audition/routing 有显式降级；AAF 未实现，EDL adapter 未见；OMF 明确当前不实现 | 部分 |
| 导出 | Deliver 页/时间线 renderer、音频图共享、字幕交付/Stem/章节、缓存、能力诊断/manifest/QC；大量导出 tests | 主 Agent/导出分项应做真实渲染交付检查；本报告只核查入口与声明，不把整个导出功能列为缺失 | 已有 |
| AI 剪辑 | Assistant、多模型 profile/credential、analysis、transcript/scene/semantic、变体计划、diff/审批、transaction/revision/postcondition/audit、任务恢复、subtitle translation provider | 服务/模型依赖决定可用性；已有口播/去停顿规则，不代表有逐词人工文本剪辑 workspace；外部 AI 未真实调用 | 已有，取决配置 |

## 隐藏 Bug 与状态缺陷

### FE-B01｜P1｜修改 FPS 后字幕、动画、标记与自动化时序不保真（已复现）

证据：[现有 FPS remap 方法](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:665>) 只缩放 video/audio clips 的 placement/fades 和 transition、range；未重映射 subtitle_tracks、markers、clip/effect animated_params、track/master automation、caption 图形的时序内容。方法标题声明 retain absolute timing，与结果冲突。

真实执行：24fps→48fps，video 从 frame24→48（仍 1s），subtitle frame24 原样保留（1s→0.5s），marker仍24；clip opacity keyframes24=.2、48=.8 未 remap，新 1s 的值从 .2变成 .8。不能仅用视频/转场测试作为验收。

修复：在 feature service 建立 sequence timebase transform，按 Rational 把所有“序列帧”坐标统一转换，保留源帧坐标不变；重映射字幕/markers/animation/automation/graph bindings 的序列域，合理合并因降 FPS 落同帧的点；转换 pre/post/extrap 与 bezier X 坐标；以完整 before/after 快照 command 原子提交并 bump revision；更新预览/音频/缓存/持久化。验收：24↔48、23.976↔29.97，视频/字幕/标记/音轨/master/效果动画实时时间均容差≤1目标帧，Undo 恢复完整状态，保存重开一致。

### FE-B02｜P1｜序列设置直接改缓存，Undo 与 revision 路径脱节（静态确认）

证据：[设置提交](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:762>) 直接替换 `_current_timeline_sequences` / `_current_edit_project`、load_timeline，只有 dirty 标志；不执行 Timeline command，也不 bump sequence.revision。此前 AI plan 的 base revision 可以仍相同。已有代码注释承认 settings dialog updates cache directly。

修复：复用 FE-B01 的 UpdateTimelineSettingsCommand/工程事务，sequence/ref/cache 同步提交；拒绝非法重采样；为格式/名称/sample_rate/resolution/guide settings 维护 before/after；preview renderer 与音频图使用新 revision。验收：一次 Undo/Redo 完整恢复 name/FPS/format 与 payload，旧 AI plan 因 revision 改变被拒绝，取消对话框无修改。

### FE-B03｜P1｜源编辑缺关联音轨和音频目标路由（静态确认，待真实 Qt 验收）

证据：[源编辑调用](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_page.py:2670>) → TimelinePanel → [TimelineView.insert_clip](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:15531>)、overwrite/append；默认都 `get_target_track_id()`（视频目标），只建一个 Insert/Overwrite/AppendClipCommand。与 [素材拖入路径](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:15156>) 的 `has_audio` 检查、视频/音频链接 ID、InsertLinkedAssetCommand 分叉。

结果：同一有声视频用拖入可以有 A/V，两点源编辑插入缺 audio clip；纯音频源走视频目标（有视频轨时插错类型，无视频目标时失败）。应在主实测中用同素材对照两种入口。

修复：提取统一 SourceEditRequest/SourceEditService，显式 media type/streams/source I/O/destination patch/mode，构建同一 A/V 原子 command；视频、纯音频、无音轨、多个音轨均按用户 patch 路由，自动创建轨道走 command；覆盖/插入的 ripple 必须作为一个整体事务，失败无半成品。验收：三种入口的源区间/速度/时长/linked IDs一致；纯音频落 A目标，有声视频单步 Undo 同时移除 V/A。

### FE-B04｜P1｜源 Insert/Overwrite/Append 的混合 FPS 时长错误（已复现）

证据：[insert timeline duration](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1285>)、[overwrite](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1408>)、[append](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1642>) 都计算 `int(source_duration/speed)`，没有源帧率/序列帧率比例；Insert虽传入fps字段仍只保存映射元数据；Overwrite/Append UI 和 command 还不传这些字段。

真实执行：24fps timeline，30fps源 I=0/O=29（1s）三个 command 都产生30timeline帧（1.25s），期望24；Insert保存源30/序列24元数据，但 placement仍30。应复用已有 Rational/frame mapper：`round(source_frames * timeline_fps / source_fps / user_speed)`，speed_point则按积分时间转换，最小1帧；command ripple offset/overwrite区间/undo快照共用同一时长结果。验收：24/25/30/24000/1001/30000/1001混配及长源，三入口/拖放duration一致，音视频/后续同步锁轨道不漂移。

### FE-B05｜P2｜源 Clear In/Out 后时间线按钮仍可用；单帧源编辑被排除（静态确认）

证据：[clear_in_out](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/source_viewer.py:1921>) 不发 in/out changed；[EditPage监听](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_page.py:564>) 只依赖两者同步 enabled。[source params/按钮条件](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_page.py:2788>) 用 I<O，虽然模型和command把Out定义 inclusive、接受I=O。清空后看似可按，实际handler无参数只warning，体验是无反馈“假按钮”。

修复：给 source range 一个 `range_changed(Optional[int],Optional[int])` 状态信号，open/clear/close/set endpoints均发；有效范围I≤O；单端补0/末帧可作为 F01扩展；程序化同步加锁；失败在状态栏给具体提示。验收：Mark→Clear立刻禁用；相同帧I/O可插1帧；切素材、关闭源、撤销/撤回范围不残留enabled。

### FE-B06｜P1｜切换多机位会累计同步偏移（已复现）

证据：[角度切换 command](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2583>) 直接 `target.source_start + target_angle_sync_offset`；当前源窗已包含活动角度偏移。网格 [resolve_angle_source_frame](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/multicam.py:303>) 却使用target_offset-active_offset，所以网格与实际画面不同。

真实执行：A offset0，frame10切B offset+10，frame20切回A offset0，实际A源30，期望20。修复：在显式 group context 取 active offset，用 `clip.timeline_to_source_frame(playhead)` 映射源时间，再先去活动角度偏移/源FPS、转换到组公共时间、叠目标偏移/目标FPS；command传完整 mapping context，不依赖UI临时state；验证源窗末端并处理负offset，不要仅max(0)静默产生不同步。验收：A→B→A、不同偏移/不同FPS/trim/reverse/retime/多次切角及Undo/Redo，grid与实际同一源帧。

### FE-B07｜P1｜多机位波形同步确认后不标 dirty、不 Undo，不更新既有切点（静态确认）

证据：[apply_sync_offsets](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:6857>) 仅更新项目变量/media_pool、刷新网格与状态栏，不执行 project command、不给dirty/revision；set_project通常不发project_changed。组建立也先stamps资产再检查unlocked track，失败仍修改project。已有切点 source_start 不随offset更新，是目前声明限制。

修复：CreateMulticamGroupCommand/UpdateMulticamSyncCommand 先完整预检再原子修改 group/member资产；经统一project提交入口更新revision/dirty/save renderer，复用结果对话框选择“仅未来切换/全部既有切点重同步”；capture project ID/revision/group ID，异步结果过期时不提交；恢复原group与所有clip source窗。验收：所有视频轨锁定创建不改project；波形应用出现dirty提示、关闭询问保存、重开保留；一次Undo还原所有offset和切点，切project后旧worker不污染。

### FE-B08｜P1｜创建嵌套后丢失音轨状态并改变动画节奏（已复现）

证据：[子轨重建](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/nested_sequence.py:345>) 重新构造 EditTrack，仅传 ID/name/type/index/clips，丢 volume/pan/mute/solo/FX/routing/send/automation等；clip仅改timeline_start、保留绝对timeline-domain animated_params。后续flatten保留已有子轨参数并不能修复创建时已丢数据。

真实执行：音轨(.3 volume,-.7 pan,muted=True,automation=read)创建后变(1,0,False,off)；父frame24起 clip动画24=.2/48=.8，子clip起0，第24帧仍.2（应.8），动画延后24帧。mute状态可能令原本静音的内容重新发声。

修复：拷贝完整track语义，映射Bus/Send到子图并处理父与子增益/FX重复作用；clip/effect keyframes和track automation减min_start，保留区间开始前影响插值的锚点；跨选择边界transition/link/markers统一策略；构建前后使用同帧画面/PCM比较验收，不能仅检查clip计数和start。验收：普通+FX+自动化+Bus+静音轨嵌套前后音画容差一致；起始非0有动画仍对齐；保存重开同。

### FE-B09｜P1｜创建嵌套无法撤销（已声明限制，静态确认）

证据：[创建嵌套提交](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:6931>) 直接增加asset/ref/cache并替换父；[能力声明](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/capability_registry.py:383>) 明确创建无命令栈Undo。不是“嵌套未实现”，是高级编辑的历史事务缺口。

修复：把NestedSequenceTransaction包装成跨project+sequence的原子command，capture父/子/ref/asset和旧selection/navigation，Undo移除新child/ref/asset并恢复父完整状态；有后续引用时history时序正常回退，禁止不安全孤儿删除；提交revision/dirty/renderer/保存同步。验收：Nest→Undo→Redo→子修改→回父→Undo、重开和引用计数不留下孤儿，单次Undo符合用户预期。

### FE-B10｜P1｜重链接/换源/代理 helper 重置工程 revision（已复现）

证据：[relink_asset](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:5127>)、[replace_media](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:5245>)、[update_asset_proxy](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:5335>) 手动构造EditProject，不传revision，默认0。ProjectCommandStack随后bump也会从0起，不保证单调。

真实执行：project.revision=7，三helper返回revision=0。修复：纯helper用dataclasses.replace保留全部字段；revision只在统一commit边界递增，未提交模拟不bump；批量relink/online刷新/其他manual constructor做完整搜查；异步代理cache性改动与内容revision策略明确，避免伪锁冲突。验收：revision7→所有提交8（纯helper仍7），Undo/Redo保持单调，未知字段和项目payload不丢，旧plan被正确拒绝。

### FE-B11｜P1｜换源时旧代理任务的完成/失败仍能污染新素材（静态确认）

证据：[replace/relink](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10130>) pop `_proxy_jobs[asset_id]`；[完成回调](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10532>)只有expected_path非None才拒绝path不匹配。已pop后expectedNone，旧worker结束会把旧proxy标到新asset；[error回调](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10697>)同理。删除asset后Undo恢复也可能遇旧完成事件。

修复：任务携带project_id+asset_id+source fingerprint+generation/job UUID，完成/进度/error每种回调都要求当前token相同；取消/删除/换源递增generation保留tombstone；保留worker handle执行cancel，输出先临时文件、验证后原子publish，失败清理；成功仅绑定验证过源fingerprint且任务仍活跃的proxy。验收：生成过程中replace/relink/delete→Undo/切project，乱序success/error不能改新对象，合法任务仍正常接收，取消无残留。

### FE-B12｜P1｜Replace Media未探测新源元数据与媒体类型，短源越界未提示（静态确认+纯模型复现）

证据：[replace UI command](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10111>)不传new_metadata；[replace service](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:5221>)保留旧media_type/metadata，文档说调用方负责probe但UI未做。UI文件filter允许视频/图片/音频跨类型选择。使用位置对话框只有“参数保留”，不校验source windows。

真实执行：旧video(fps24,duration240,width1920,height1080)替换成new.jpg仍media_type=video、旧metadata完全保留。修复：后台MediaProbeJob/probe导入服务，替换前构造新asset metadata/color/audio/image sequence识别；asset_usage_index分析各clip实际使用源窗、速度/FPS、nested/composite引用；不兼容类型明确阻断或有说明的conversion，新源过短提供拒绝/缩短/保持时长并hold/占位策略，不得静默；一command保存完整asset/proxy/fingerprint数据Undo。验收：长→短、30→24fps、有声→无声、video→image、HDR色彩变化均提示且preview/export/显示时长一致。

### FE-B13｜P1｜三处 UI fractional FPS时间码互不一致且非SMPTE（已复现）

证据：[Source时间码](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/source_viewer.py:2360>)、[EditPage时间码](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_page.py:2426>)使用float除/模；[Caption workspace](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/caption_workspace.py:82>)还`ff>=int(fps)`clamp。现有正式 [frames_to_timecode](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/interchange.py:57>)支持NDF/DF，却未复用。

真实执行fps30000/1001：caption frame29显示00:00:00:28，规范NDF应:29；frame1799显示00:01:00:00应00:00:59:29；frame107892显示00:59:59:28，DF应01:00:00;00。修复：全UI/时间码对话框/markers/caption/导出使用统一Rational+explicit DF flag；帧码不是按浮点秒拼字符串，输入/输出roundtrip；起始timecode和源timecode持久化schema。验收：29.97/59.94分钟/10min/小时边界、NDF/DF和23.976、source/program/caption一致且定位roundtrip无损。

### FE-B14｜P2｜能力表关于Stem导出有过期声明（静态确认）

证据：[registry](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/capability_registry.py:241>)仍标DORMANT/无生产UI；[菜单](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/module.py:419>)已注册`export_stem_tracks`，[实现](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/file_controller.py:7502>)创建StemExportDialog+start导出。旧嵌套生产test头也仍称无音频，而当前已flatten。

修复：registry状态取入口/renderer backend自检，统一UI帮助和AI capability snapshot；把静态INTERCHANGE_CAPABILITIES标为兼容层或由registry派生，更新验证测试；测试入口确实存在并可dispatch，不能只断言常量。验收：Stem在菜单/帮助/注册表的状态一致、缺依赖时具体诊断。

### FE-B15｜P1｜Loudness report将样本峰值标为True Peak/dBTP（静态确认，具体差值待标准信号验收）

证据：[analyzer](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/loudness_analyzer.py:120>)用max(abs(block))，注释承认“完整实现需4x oversampling”；report/文本/normalize警告仍命名True Peak (dBTP)。样本峰值不能代表样本间峰值，非“没有响度计”，是QC标准精度缺口。

修复：实现有滤波状态的至少4x插值/oversampling true-peak estimator，跨block保持filter state；缓存标准版本/backend字段；无后端就标Sample Peak dBFS，禁止作为真峰值通过证据；Normalize/limiter用真实预估，并避免将未来限幅当成已输出合格。验收：已知intersample overs/低频/满刻度/多通道/块边界/空输入，与参考true-peak实现误差有明确阈值，中文报告/manifest保留真实指标类型。

## 仍缺失或未接完整功能：可执行实现计划

下面“缺失”只针对具体工作流，不能用此表否认上面的已有能力。P1是常用编辑或预览/交付一致性必需；P2是效率与专业工作流补齐；P3是可选扩展。每项都应遵循 `.clinerules`：不可变模型→command/service→统一发布→preview/export→序列化兼容；UI不直接解码/操作GPU。

| ID | 优先级 | 缺失范围/已有基础 | 依赖与预计规模 |
|---|---|---|---|
| FE-F01 | P1 | 完整源编辑：单端/全源默认、A/V stream patch、3/4点、Replace Edit、Match Frame（已有video/audio目标轨选择、I/O和insert/overwrite/append） | FE-B03/04/05/12，5–8人日 |
| FE-F02 | P2 | 两路/四路修剪监视器与精确edit-point反馈（已有trim-frame-preview单帧请求） | frame mapper+preview任务协调，3–5人日 |
| FE-F03 | P2 | Edit clip/effect动画Dope Sheet、曲线/Bezier手柄/多点编辑（已有keyframe buttons、shared keyframe模型和commands） | FE-B01/B08，shared keyframe UI适配，5–8人日 |
| FE-F04 | P1 | preserve_pitch实时音频与离线输出一致（已有WSOLA离线） | streaming WSOLA、audio ring/cache，4–7人日 |
| FE-F05 | P3 | optical_flow高质变速/插帧（目前模型可接受，UI隐藏；已有nearest/frame_blend/smooth speed ramp） | 可选算法/backend，GPU/CPU parity，8–15人日 |
| FE-F06 | P1/P2 | LUFS/峰值Normalize + Auto Duck完整工作流（已有analyze_loudness、gain command、SidechainDucker纯DSP、mix graph） | FE-B15、分析worker/缓存/FX registry，5–8人日 |
| FE-F07 | P1/P2 | 嵌套成熟化：Undo、倍速/倒放/变速音频、子音频转场、breadcrumbs、Unnest；允许字幕搬移需独立策略 | FE-B08/B09，recursive mapping/audio graph，6–10人日 |
| FE-F08 | P1/P2 | 多机位公共时基、实时proxy网格、TC/标记同步、音频切角策略、组管理/重新同步 | FE-B06/B07、decode预算、group schema，6–10人日 |
| FE-F09 | P2 | 字幕工作区可编辑表格、Speaker/Status/批量样式、按播放头/字词拆分、显式批量翻译和无障碍/i18n | 已有caption commands/translation/AI provider/quality，4–7人日 |
| FE-F10 | P2 | 代理任务管理：批量queue、真实progress、cancel/retry、磁盘容量/状态、统一job隔离 | FE-B11/B12、disk proxy policy已有，3–5人日 |
| FE-F11 | P2 | 版本化备份/历史列表、预览恢复、恢复副本及保存失败恢复工具 | 现有autosave/backup/restore服务，schema+disk retention，3–5人日 |
| FE-F12 | P2/P3 | FCPXML/OTIO交换保真扩展与可选AAF/EDL | 现有adapter/report/import_builder，optional OTIO/aaf2，6–15人日 |
| FE-F13 | P2 | 人工逐词文本剪辑工作区（已有speech/transcript/语义索引/AI口播与去停顿计划） | word-level ASR/VAD证据、transaction plan，5–8人日 |
| FE-F14 | P2 | 能力/帮助/诊断统一展示与手动工作流发现（已有capability registry/AI manifest/effect catalog） | FE-B14、入口健康探测，2–3人日 |

工期仅用于相对排期，未包含多系统GPU/音频设备/第三方软件互通验收，实施前应拆到可独立交付的command+UI闭环。

### FE-F01：完整源编辑

1. feature新增不可变SourceEditRequest（asset、source range、mode、sequence target range、V/A patch/stream IDs、用户speed、timebase）与SourceEditService。统一复用InsertLinkedAssetCommand及Insert/Overwrite/Append规则，消除源监视器与拖放两套计算。
2. RangeResolver无端点默认整源，单端补0/末帧，I=O允许1帧；image默认持续秒数按timeline FPS转换。Out inclusive规范贯穿工具条/slider/command。Patch既有目标轨按钮进一步显示V1/A1…到destination的source连接，可关闭视频或音频、选择音频channel layout。
3. 三点剪辑推导缺失源或timeline端点；四点时duration冲突对话选择fit-to-fill/保持源速裁掉/取消。定义明确圆整与handles；缺源时长/离线不猜测，给诊断。
4. Replace Edit替换选片而保持timeline位置/transition/effect/grade/link；复用ReplaceClipAssetCommand/源映射校验，允许对齐source In或playhead。Match Frame按TimelineFrameMapper反求源帧打开SourceViewer并恢复源范围，nested/multicam可逐层定位，reverse/ramp不能用线性猜。
5. patch/默认range/替换策略保存UI state（暂态选择不写媒体）；新增clip字段遵从schema版本；atomic undo保证A/V/轨道创建/ripple一组。
6. focused tests覆盖全源/单端/单帧、mixed FPS、3/4point歧义、音频only/multichannel、locked/patch off、linked AV；Qt点击快捷键和实际导出比较同源相同区间。验收：以30fps有声源在24fps timeline按I/O剪1s，V/A同时恰24帧，Match Frame回源正确，Undo单步恢复。

### FE-F02：修剪比较监视器

1. 复用现有trim_frame_preview_requested和EditFrameCoordinator，request包含edit mode、left/right clip/source帧、generation、delta/handles；由后台decoder/renderer取帧，禁止UI同步QImage/GPU解码。
2. ProgramViewer增加可切换2-up（outgoing/incoming）、4-up（slip/slide两邻居）展示，源与节目时间码、变化帧数、可用handles、重叠/源边界警告；拖动时合并请求，只呈现最新generation，取消清理预览texture。
3. 编辑期间只暂存preview映射，松手一个原command提交；Escape撤消暂态，状态栏提示失败原因。通用layout不改时间线model，无必要schema变化。
4. tests验证ripple/roll/slip/slide边界四帧映射、stale帧丢弃、取消资源释放；实际拖动在低/高zoom验证monitor跟手。验收：按帧step反馈无过期画面，预览首尾与提交导出对应，拖动不制造多条Undo。

### FE-F03：剪辑动画编辑器

1. adapter把clip/effect AnimatableParam映射为shared KeyframeTrack，明确frame是timeline域；复用Add/Remove/Update/SetKeyframeInterpolationCommand，补Move/Batch/Paste/BezierHandle command和批量macro，不原地修改mutable track。
2. 从Properties ◆旁“打开动画编辑器”和dock提供dope sheet/curve切换，多选、框选、时间吸附、value/frame数值、前后关键帧跳转、复制粘贴、hold/linear/ease/bezier手柄；selected effect/clip更新context、状态锁避免递归。
3. 统一renderer在每帧按AnimatableParam求值，曲线拖动走预览snapshot、松手原子commit；HDR/opacity/scale数值范围按参数schema，不把普通float一律clamp0..1。
4. keyframe已有to_dict应复用，新增handle/extrap字段有migration，未知参数保留；nested/FPS/source mapping changes统一转换。
5. tests覆盖multi keyframe move后冲突、Bezier求值、copy跨clip/effect、undo/redo、save reopen、preview/export同一采样帧；验收曲线UI显示实际插值而非独立UI曲线。

### FE-F04：实时保调

1. 将audio_timestretch.py已有WSOLA提取stateful processor（analysis/synthesis窗、尾部overlap、累计源相位），worker预取PCM块；queue按timeline mapping提供变速目标长度，支持seek/reverse/ramp时flush/reanchor。
2. AudioGraphRenderer/AudioPlayback/实时engine共用speed-preserve-pitch策略，ring buffer预热并暴露late/degraded metrics；受预算限制时明确降级状态，不悄悄变调。clip/speed已有字段无需新schema，但cache fingerprint含preserve_pitch和算法版本。
3. UI现有tooltip从“export only”改为实际backend状态，播放和export采用同一stretch时间语义；离线仍允许更大窗口高质量模式。
4. tests使用固定音高/扫频/对话、.5x/2x、ramp、反复seek/loop、cancel和长时A/V drift，比较实时offline长度/音高并检查callback不阻塞。验收1kHz在倍速保调仍约1kHz、输出时长符合mapper、无点击声/累积漂移。

### FE-F05：光流（可选高级）

1. 在Effect/Delivery能力registry声明retime interpolation backend与可用quality，依赖可选光流库/Metal/OpenGL compute资源；未安装保持nearest/frame_blend选择，读取旧optical_flow工程明确unsupported诊断。
2. FrameProvider取连续两帧、在source-domain估流、按映射fractionwarp/bidirectional合成并处理遮挡/scene cut；场景检测处禁用跨shot flow，fallback有manifest诊断。
3. cache key含source fingerprint、pair帧、尺寸、色域/算法/quality；GPU只渲染thread运行，取消/seek generation淘汰texture/cache；preview可代理质量但export重算native/HDR。
4. 模型SpeedParams.interpolation已存在，新增quality/version要schema兼容；验收以平移、运动遮挡、快速pan/硬切、HDR亮部样本；比frame_blend降低重影，preview/export采样time一致，失败具体提示。

### FE-F06：Normalize与Auto Duck

1. Audio Gain菜单升级为dialog（固定dB/目标LUFS/真峰值限制/逐片或整选组），分析用现有LoudnessAnalyzer后台解码且可cancel/cache；对源trim/speed/FX作用点明确，后端缺失禁止Normalize不可信floor。
2. analysis结果→SetClipAudioGainCommand或macro，预测over/limiter由已注册DSP effect应用；先完成FE-B15。多selected clips有“同增益保持相对电平/逐片均衡”选择，UI展示目标/测量/预计gain。
3. auto_duck不另造数学：接SidechainDucker进入audio graph snapshot/DSP factory；registry schema包含sidechain track/bus、threshold/ratio、attack/hold/release/makeup；validate graph阻断依赖环/不存在目标。
4. UI混音器/音乐clip允许指定对话track或bus；可选“烘焙volume automation”模式复用track automation command。preview/export/stem共用graph/DSP，seek清envelope，muted sidechain与bus位置语义明确。
5. duck params/路由schema版本、unknown字段保留；tests测silent source、multichannel、fallback、duck attack/release、cycle、seek、FX bypass/undo、save/reopen和preview/export PCM parity。验收语音期音乐降低、结束平滑恢复，显示true-peak/输出匹配。

### FE-F07：嵌套完善

1. 先FE-B08/B09语义保真与atomic history。flatten增加递归time transform（parent timeline→nested source→child timeline），用同Rational/SpeedParams处理speed/reverse/speed ramp，音频child也以样本域映射；当前`speed !=1 audio skipped`应改可用算法/显式不支持诊断。
2. 子轨transition搬到父混音图并保留映射后duration，FX/automation/Bus拓扑完整复制且避免父子double gain；更合适方案让child音频图作为subgraph，减少track flatten丢语义。
3. UI显示父→子breadcrumb、返回按钮、源range、子时长变化时如何处理父窗（保持/扩展），增加Unnest command恢复child内容于parent、链接和routing remap，所有变更进历史。
4. 字幕nest可选：native字幕应仍在delivery语言轨还是burn进child需明确，采用专用caption remap和graphics binding迁移，不能当普通asset clip移动。拒绝unsupported组合也需可见解释。
5. 保存child refs graph DAG+schema，删除/undo引用计数防孤儿；tests递归3层/ramp/reverse/audio transitions/child删缩时长/cycle/undo，nested前后画面/PCM golden比较。验收普通嵌套音画保持、倍速音频不断，child编辑即时反映。

### FE-F08：多机位完善

1. 新建project级MulticamGroup管理（asset不能只载单group，否则素材加入另一组会覆盖第一组），clip显式group ID与angle时间映射；保留asset-carried旧schema迁移。
2. 组以Rational公共时间/源TC或秒偏移，关联起始TC/reel/frame rate，修FE-B06；同步来源audio/TC/marker/manual，后台sync report confidence/handles且可修改，FE-B07所有提交可undo、既有切点是否追溯明确。
3. grid改低分辨率proxy多路decode streams，预算限制时降低各angle fps/resolution，active angle优先，隐藏dock即停止次路预取；telemetry暴露真实/静帧模式。线程与GPU复用当前decode manager，禁止每tile另起OpenGL context。
4. audio policy固定主音频/跟video角度/指定angle，切点短crossfade并按目标source channels处理，联动命令原子；组管理允许rename/reorder/add/remove angle、改默认audio、保护已使用angle。
5. tests同一asset多group、mixedFPS/TC、长offset、切角speed/reverse、重同步旧片段、recording连续1–9、preview/export/save reopen。验收网格与节目同瞬间、切角不漂、音频策略明确且单步Undo。

### FE-F09：字幕工作区

1. CaptionCueModel增加可编辑TEXT/SPEAKER/STATUS/IN/OUT列：setData只emit请求，不直接mutate；通过UpdateSubtitleClipCommand/metadata和批量macro提交；保留cue ID、selection/focus，输入验证重叠/最小时长/语言。
2. 将NoEditTriggers改指定列delegates，Text多行、speaker/status枚举、timecode统一FE-B13 converter；实现keyboard Enter/F2/Tab/上一下一cue；所有hardcoded English文案I18N，可访问名+focus sequence。原Properties编辑继续共享同模型。
3. Split支持播放头、文本光标或word alignment，按句读/ASR time映射；Merge只允许兼容track/language邻接区间，批量拆分/样式不逐次reset。当前固定midpoint可保留为明确选项。
4. 手动翻译入口复用captions.translation/AiAssistantTranslationProvider：worker按chunk/长度budget带cue ID返回、验证总数/空文/same-project revision；生成独立target-language track，原文不改，对齐metadata不错误沿用word timing；preview批准后CreateTranslatedSubtitleTrackCommand，失败/取消不半轨。
5. 预览和burn/mux/sidecar共用CaptionStyle/render_plan，quality panel定位CPS/WPM/超长/重叠/缺font，并批量建议command；schema用既有CaptionCueMetadata/track metadata，不再旁路存UI dict。
6. tests编辑→undo→save reopen→export文本与时间、同语多track查找scope、translation timeout/cancel/stale/长度错、2000cue虚拟化不reset焦点。验收直接改表格能看/听对应cue，真正支持中文工作区。

### FE-F10：代理任务管理

1. 复用ProxyGeneratorWorker/DiskProxyService/ProxyPolicy，新增集中ProxyJobManager（token修FE-B11、asset source fingerprint/目标尺寸/编码/profile/status/progress/cancel/输出temp），不使用UI `_proxy_jobs` dict作为唯一生存标识。
2. queue并发上限按磁盘/CPU预算，批量selected/bin生成、pause/cancel/retry/优先级，进度从ffmpeg progress实时解析，空间不足/离线/编码失败显示具体原因；每asset状态不因旧error覆盖。
3. 任务中心可见总体与每项progress/预计容量/原片-代理切换政策；cancel释放process/临时文件，关闭项目明确后台任务处理。
4. 工程只存可重建的policy/fingerprint与可重定位proxy引用，不保存机器绝对临时cache；管理cache位置/搬迁/清理、恢复后verify再ready。
5. tests高并发/取消/换源/删除undo/重开/job finish/error乱序/磁盘失败；验收用户可看进度、取消成功、旧源proxy绝不会替换新源。

### FE-F11：备份与恢复

1. 复用EditProjectIO异步snapshot/save，现有fixed .bak升级timestamp+project ID+revision+checksum版本ring并加manifest；保留旧.bak兼容，retention按数量/天数/容量，与autosave隔离。
2. project recovery UI列出正式/autosave/backups时间与revision、可读性、timeline/asset概要；先background validate package/schema、preview加载到隔离副本，“恢复为新副本”默认，替换原工程在事务前先备份。
3. restore服务不能直接裸copy2覆盖当前工程：临时文件/validate/fsync/atomic replace；打开恢复工程采用canonical path和新session generation，防旧save worker覆盖新恢复版本。
4. tests断电截断/损坏zip/磁盘满/权限/异步autosave竞态/旧schema/unknown payload/有未保存更改；验收恢复后完整timeline/字幕/AI/composite payload，原文件可回退、备份不会悄悄只有上一版。

### FE-F12：交换格式

1. FCPXML/OTIO现有adapter扩展必须按feature mapping matrix：源FPS、speed/timeMap、gain/pan、linked A/V、nested、subtitle/style、markers、transitions、effect能力逐项分类preserved/converted/dropped/requires_action；先做roundtrip子集，不承诺第三方FX通用。
2. export预检结果先显示将丢失项并支持“渲染替代asset/保留metadata sidecar/取消”，渲染替代复用render cache/export service，不能静默drop。导入probe/relink+preview确认维持现有workflow，所有refs/assets/sequences原子command。
3. AAF为可选adapter（aaf2），先音频子集：source mob/PCM media、track/clip placement、sample-accurate in/out、gain/pan/fade、BWF/reel，嵌入/引用媒体及handles；unsupported FX转Stem/sidecar；无依赖入口明确诊断。
4. EDL作为可选低成本video cuts/dissolve/reel/timecode adapter，可优先复用OTIO adapter插件（实际依赖/版本经环境验证），严格限制单video+audio layout/速度子集，超出显式report；OMF维持不实施除非出现明确交付需求。
5. serialized interchange provenance和未知source metadata保留version；tests24/25/29.97DF、源TC、missing media、long path、roundtrip、外部FCP/Resolve/DAW实际开项目验收，不能只成功生成XML/AAF就算完成。

### FE-F13：逐词文本剪辑

1. 复用analysis store/transcript provider/semantic index/timebase，定义word段（asset、source timestamp、speaker、confidence、evidence kind）和VAD silence，UI transcript虚拟化与source/timeline播放头双向定位。
2. 用户选句/词执行Insert/Lift/Extract/删除停顿，编译成existing typed edit operations/plan，使用EditProjectService事务与sync lock/link策略；按目标timeline FPS映射区间，原ASRsource evidence不因timeline变更覆盖。
3. AI voiceover polish已有规则仍保留；自动去停顿须用VAD/word证据、阈值/前后handles、crossfade而非只用transcript覆盖率；应用前diff预览，让用户排除口误/语气停顿。
4. 可选alignment provider声明准确度/网络/credential要求；无word evidence降级到句级，不伪造逐词精度。project存transcript/index版本与source fingerprint，失效重算后台进行。
5. tests跨句/不同FPS/linked AV/音乐sync lock/音频crossfade/Undo/无词级时间/外部模型失败，实际口播剪后无截字、音画对齐，diff可复审。

### FE-F14：能力与帮助

1. runtime registry与effect catalog按当前backend/dependency/入口实际dispatch能力产生可用/部分/不可用；清理Stem/嵌套旧声明，AI manifest和产品help只消费同一snapshot。
2. 菜单缺依赖/locked/无source/no selection用禁用原因和任务导向帮助；页面功能列表可跳到Multicam/Caption/ADR/proxy/exports dock，补关键快捷键和焦点上下文，避免已实现的功能“找不到”。
3. 强类型tool/service contract允许UI/AI同一项能力调用和参数schema，不通过模拟鼠标走业务；帮助写真实版本、partial限制/恢复途径，registry tests验证每个available存在entry+backend+focused test。
4. 验收中文环境能力发现与真实入口一致，不出现“支持不存在”或“功能有但帮助说没有”；optional dependency缺失只降低该项，不阻塞整个剪辑页。

## 本分项可重复证据摘要

- MULTICAM：实际运行SwitchMulticamAngleCommand两次，A_at_timeline20_actual_source=30、expected=20。
- NESTED：原音轨volume .3/pan -.7/mutedTrue/automation read → 子轨volume1/pan0/mutedFalse/automationoff。
- NESTED_ANIMATION：父clip frame24起，24=.2/48=.8 → 子clip frame0起，第24帧仍.2、expected=.8。
- FPS：现有 `_with_timeline_fps` 方法 AST 直接提取运行，24→48fps字幕start1s→.5s，marker未缩放，opacity 1s .2→.8。
- MEDIA：project.revision7 → relink/replace/update_asset_proxy均0；replace video→.jpg仍video与旧元数据。
- TIMECODE：Caption frame29 @30000/1001输出:28（expected NDF :29）；1799输出00:01:00:00（expected00:00:59:29）。
- SOURCE_FPS：source30fps 0..29，在24fps序列运行Insert/Overwrite/Append各clip.timeline_duration=30（expected24）。

以上执行没有GPU/硬件/网络调用，不可据此声称已通过完整剪辑页人工交互或实片导出。需主报告列出每项的Qt交互、视频输出和外部依赖覆盖程度。
