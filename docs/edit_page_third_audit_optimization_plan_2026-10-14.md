# 剪辑页面第三轮复查与新优化计划表

日期：2026-10-14。当前版本：`63c0bdeaeb5edcee59363479cd8c10b947c3ddb1`；对照第二轮版本：`833dcd382d40f3ff75cb31bc3e3374814efa2b1b`。

项目根：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。

本轮重新核查上一轮修复后的真实实现。**121项新增回归测试、177项既有导出及测试契约检查全部通过**，但完整提交链和连续操作仍发现 **27组当前可复现问题（24组P1、3组P2）**。包括已修基础路径之外的残余、修复引入的回归和新发现；相同根因下多个输入不重复计数。已声明未做的phase schema、mixed显示、高级工作流和结构事务另列，未伪装成新隐藏bug。

本次仅新增计划、审计脚本和证据，不修改业务代码、已有测试、旧文档或用户素材。上一轮实施声明保留作对照：[第二轮计划](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/edit_page_followup_audit_optimization_plan_2026-10-14.md>)。

## 1. 本轮测试范围及已确认改善

证据等级：G＝完整可见MainWindow、真实素材、QTest实际鼠标/键盘/按钮事件；Q＝offscreen真实Qt控件/事件；M＝真实模型、命令、JSON及实际handler，部分宿主/对话框/服务使用替身；T＝现有pytest（含实际OpenGL/Metal测试）；S＝当前源码调用链。G为自动交互测试，不称为人工鼠标操作；Q/M不能冒充真实GPU画面或OS失焦。审计excepthook用于保留Qt槽异常并继续测试，不是生产错误处理。

环境：Python3.12.13、NumPy1.26.4、Qt运行时6.10.2（编译版本6.10.0）、macOS cocoa、1440×1000。导入用户`tests/test_videos/`下三份2K素材，主要操作使用`2K_h264.mp4`。不同FPS、曲线、锁轨、代理乱序采用明确的合成fixture；元数据占位文件没有当作可解码视频。

| 范围 | 已验证结果 | 当前仍有的边界 |
|---|---|---|
| 新修复测试 | `tests/edit/test_recheck_*.py`：121 passed，9.98s | 多数为单命令/单字段/直接handler；绿色不代表实际signal接线或连续编辑完整 |
| 原失败范围 | LUT/GPU/Metal/SyncLock/preposition旧范围：177 passed，32.64s | 第二次LUT缓存命中和网格外合法颜色没有覆盖；见N25/N26 |
| 领域原例 | 原锁轨六入口、反放右修、fade/Speed缩短、Roll转场、基本Insert follower、原JSON快照、seam marker、no-op保留Redo已改善 | 新Macro意图、marker Undo、其他锁入口、curve window和新命令载荷仍未完整 |
| 时间线普通交互 | Q：disabled首次加载、普通2x左右trim preview、基本菜单/键盘/Hand/字幕导航通过；51个非Roll有效取消组合恢复完整 | 旧54矩阵的3个Roll在主动取消前就自取消，不能算有效Roll取消通过；正常Roll见N15 |
| 多选 | 一次X/Opacity确实写全部目标，Transform Reset基本一步Undo已修 | 第二字段覆盖第一字段、Speed Undo、native Reset和Reset后context问题见N11–N14 |
| Source有效画面与播放 | G：Source真正可见、有效CPU上传纹理、paint_failed=false，播放100→123；I=O合法、Clear禁用按钮 | GPU共享纹理/长时解码未验收；流/FPS/关联状态问题另列 |
| Source基础A/V | G/M：占用音床Overwrite不再抛原overlap异常；内部Insert已带新音频；Append共同落点、Insert mixedFPS时长改善 | 残片link/marker仍错，Append采样字段、无声分支及历史重放未完整 |
| 工程保存 | G：两条时间线成功ZIP保存/读取，project revision保存前后相同 | 两个由Overwrite产生的悬空link被加载规范化清除；不是全payload原样通过。revision此前已因切/建timeline倒退，见N27 |
| 短导出 | 两条真实素材时间线各49帧、640×360 H.264，均success、audio_omitted=false | 无调色连续帧/长时/耳听/硬编全面验收；CPU有调色快路仍存在N25/N26 |
| True Peak/兼容/Bus | M/T：尾峰finalize、任意分块/直流、原model extras/JSON revision、Nest Bus闭包原例改善 | 导出调用者取消未接、Nest fresh track extras、代理二阶段仍有剩余 |

核心分工110 passed、功能分工30 passed等记录保留，但与主测试存在重叠，不相加成独立用例总数。没有将“测试全部通过”改写成“剪辑所有功能通过”。

证据入口：[本轮README](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/README.md>)。详细输入输出、精确文件行及状态矩阵：[核心报告](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/reports/topos_round3_core.md>)、[交互报告](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/reports/topos_round3_interaction_report.md>)、[功能工程报告](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/reports/topos_round3_features.md>)、[导出LUT报告](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/reports/render_review.md>)。

## 2. 当前问题总表

P1＝错误内容/历史/引用/异步隔离或正常功能失败，优先处理；P2＝上下文、精度或扩展保留问题。所有条目待修。报告代号用于找到完整复现，不另计数量。

| ID | 优先级 | 当前问题及观测值 | 状态 / 证据 / 详细追踪 |
|---|---|---|---|
| N01 | P1 | smooth切分改变51个保留帧，最大差22；整数step左trim差20；reverse RippleTrim保留80帧全差20 | R07未完整，超出已声明±1phase；M/S，C3-01 |
| N02 | P1 | 未切分8x mixedFPS片段只加入同速neutral point，75/100帧改变，最大差4 | R06新中间round回归；M，C3-02 |
| N03 | P1 | Macro复活显式RemoveTransition；删除owner后还复活转场，产生孤儿引用 | R04新回归；M，C3-03 |
| N04 | P1 | Insert redo在guard前return；Slip/Slide/Roll/RippleDelete/BatchDelete/Append/Freeze仍能写锁轨 | R01部分修/redo新缺口；M，C3-04 |
| N05 | P1 | Insert marker apply110→130正确，Undo跨轨am90，Redo110且clip120；JSON也不恢复 | R08新历史回归；M，C3-05 |
| N06 | P1 | 新linked ripple命令JSON Undo保留6clips，Redo随机换IDs；Overwrite/Append JSON漏FPS使24变30帧 | R09/R22新增载荷遗漏；M，C3-06 |
| N07 | P1 | Source linked ripple只链接新pair，既有右残片仍指左伙伴；right marker仍归左owner | R22残余；M，C3-07 |
| N08 | P1 | Overwrite残片改ID不迁移link/marker/转场；Undo转场1→0→0；G出现指已删clip的link | 关联事务未闭合；G/M，C3-08 |
| N09 | P1 | 单选锁轨属性拒绝抛未捕获Qt ValueError；控件留假值；锁伙伴trim失败release留预览 | 新锁守卫拒绝路径回归；G/Q，T3-01 |
| N10 | P1 | facade没导出ChangeClipSpeed，Properties永远fallback；V48@2x/A96@1x仍linked，锁伙伴也未拦 | R15实际接线未修；Q/S，T3-02 |
| N11 | P1 | 多选Speed2后Undo历史弹掉，内容仍2x/50帧，旧speed未保存 | R16新Undo缺陷；Q，T3-03 |
| N12 | P1 | 两片X55后输入Y66，X回原值；Crop第二边也覆盖第一边，控件显示与model不同 | R16新缓存回归；G/Q，T3-04 |
| N13 | P1 | Crop Reset一次发5宏，只有feather真归0；默认Speed Reset仍加3历史 | R16 Reset未完整；Q，T3-05 |
| N14 | P2 | Transform Reset后view仍2选，Inspector变1选；下次Y只改primary | R16刷新上下文残余；Q，T3-06 |
| N15 | P1 | 正常Roll started时ROLLING，自身TrimMonitor抢焦导致IDLE；finish有delta但model不提交 | R13新正常交互回归；G/Q，T3-07 |
| N16 | P2 | 连续20正+20反缩放期间误差2.10px，超过新≤1px；末尾回位 | R19长gesture残差未解决；Q，T3-08 |
| N17 | P1 | FPS24→48，真实cue已48–96，但caption_metadata内word仍24–48 | R20部分修，新test用了fake顶层字段；M，RF01 |
| N18 | P1 | Source Append有声48→24时长24但源采样12而非24；无声OW/Append仍48帧 | R22未覆盖全部stream factory；M，RF02 |
| N19 | P1 | Multicam/source swap Undo保留目标FPS/源窗，timeline72→144；旧PTS继续用于新stream | R23换装历史/元数据残余；M，RF03 |
| N20 | P1 | 设置command送ProjectStack报TypeError；sequence rev2→3→9→3，Redo倒退 | U01新领域事务自身缺陷；M，RF04 |
| N21 | P1 | proxy旧二阶段probe借新pending的stat落盘；异source旧回调先pop吞掉新任务 | R26全链代次仍未闭合；M，RF05 |
| N22 | P1 | Replace坏MP4仍commit/offline=false；成功换PNG丢feature metadata，保留旧HDR色彩/thumbnail；JSON漏类型 | U04新失败/兼容边界；M/S，RF06 |
| N23 | P1 | export响度scan不传取消接口：首次cancel后仍第二scan、写报告并发布PCM | R27只修analyzer未修调用者；M，RF07 |
| N24 | P2 | Nest child fresh EditTrack构造漏_extra_fields，未来轨字段丢 | R28重构入口残余；M，RF08 |
| N25 | P1 | 合法Gain LUT首次成功，第二/三帧缓存ndarray与string比较抛ValueError | R30缓存新回归；M，RG01 |
| N26 | P1 | 7³安全网格接受合法wheel，实际RGB[112,140,59]蓝244而非252，差8 | R30安全条件不成立；M，RG02 |
| N27 | P1 | 实际切/新建timeline project revision7→0；Relink UI仍7；G新建后两次Append保存rev2 | R25导航helper/完整提交链残余，新增发现；G/M，功能报告§3追加 |

### 2.1 核心与历史实现计划（N01–N08）

| ID | 具体实施方式 | 关闭条件 |
|---|---|---|
| N01 | 用完整原time-map的window slice保存曲线控制域、方向/FPS/phase，而非改duration后重新积分。step左裁rebase source-offset点；Ripple左右用同边缘映射；Split/Insert/Overwrite/Roll/Slide全部委托一个planner。位置：commands.py2401/2446、timeline_v2.py640、timeline_ops.py2794/2898 | 上述smooth保留帧（含切点前）逐帧不变，step整数trim正确，reverse/ripple正确；独立保留3组已声明±1phase待schema，不把22帧错误吞进该边界 |
| N02 | 449–453行的等效时域不要先int(round)再乘速度；SpeedParams接受连续位置，最后统一量化source sample；正反映射共享rational策略 | 0.1–8x、不同FPS/方向增加同速neutral point不改变任何帧；无slice整片100帧全部一致 |
| N03 | Macro收集显式删除意图，最终态一次重锚；仅恢复中间gap导致的转场且所有owner存在。不能“任一owner还在”就复活 | 显式RemoveTransition确实删，Delete owner不留孤儿；整体Move保留合法转场；apply/Undo/Redo validator空 |
| N04 | 所有内容命令及snapshot redo分支前统一affected-unit锁预检，覆盖主/伙伴/sync/旧解链对象；Undo可恢复合法历史。检查13440 redo快路与其他七入口 | 七漏项+Insert首apply/redo+Macro各锁组合拒绝零变化/历史；领域/API不能绕过菜单禁用 |
| N05 | marker与clip/transition共享before/after计划；全量恢复只一次，不再恢复后又sync逆移；Redo读after；JSON保留新增beforemarkers和空值语义 | 本例110/110→130/130→Undo110/110→Redo130/130，owner有效；嵌套宏/JSON三循环完整相同 |
| N06 | 新命令序列化before/after、created IDs/tracks、sync变换及FPS/PTS；Redo复用稳定after身份；旧载荷迁移/拒绝明确 | JSON真实往返apply/Undo/Redo等价live；自动建A轨Undo移除/Redo复用，ID/link/key/marker/转场不丢。此指公开命令payload，不声称.topos保存Undo栈 |
| N07 | 双轨同一seam共同分配left/right IDs，重建两组互逆links；scoped marker迁移对应残片并随ripple+10；独立时间域映射每个stream | old左右各自成pair；右marker75→85归right；后续右group move/trim不误改左group，存取validator空 |
| N08 | Overwrite做完整关联slice事务：源窗保留、residual ID映射link/marker/head-tail转场/key/fade，覆盖区标记按明确policy处理；Undo完整before快照 | 头/尾/中/全覆盖G+M均无已删引用；转场Undo回1，link两残片互逆；save/reload不再靠normalize清除坏数据 |

### 2.2 实际交互实现计划（N09–N16）

| ID | 具体实施方式 | 关闭条件 |
|---|---|---|
| N09 | 保留领域锁guard，在实际Qt信号提交边界处理已知拒绝为结构化结果/reason；finally从canonical model恢复控件及全部affected Item/session/renderer preview；Inspector共用allowed状态；linked trim press预检整组 | 单选Transform/Crop/Opacity/Speed、锁伙伴trim、碰撞/源界拒绝无uncaught Qt异常；无历史/内容变化；widget/Item复原，下一次合法操作正常 |
| N10 | commands_facade正式导出ChangeClipSpeedCommand，真实controller2375/3935确实选择它；fallback若保留须同linked/lock/fade/key语义；测试断言实际command type | Properties与菜单同结果；V/A96→48均2x；锁A伙伴整组拒绝；多选/曲线/反放一致 |
| N11 | batch builder保存完整原SpeedParams和clip内容快照；旧value不能在undo时读取当前speed。即使fallback也必须具备before值与兼容策略 | 两片Speed2→Undo均1x/100，Redo2x/50；3循环及JSON回放；栈移动与内容恢复一致 |
| N12 | 批量成功后按stable IDs刷新所有_current_clips及控件，Undo/Redo也刷新。优先提交scalar patch时从当前canonical model逐片derive，避免信任UI旧整dataclass | X→Y→rotation、Crop left→right→feather不回退；未改字段各片原值保留；UI/model/renderer/save一致 |
| N13 | Reset从每片snapshot一次构造完整默认group，所有targets/字段一个事务；不逐控件发5个宏。消除已默认no-op，失败回canonical值 | Crop五项都归默认、一次Undo全还原；Speed已默认Reset零历史/revision且保留Redo；RAW/Opacity/effects也按group验收 |
| N14 | Reset后通过完整SelectionContext刷新全部IDs/primary/compatibility，不走_show_clip_in_properties(first)的单选set_clip | 两/十片Reset后selection/context/title一致；下一共享字段仍作用所有兼容片；失效/异类ID有明确scope |
| N15 | TrimMonitor采用不抢编辑焦点的窗口内overlay/dock或受验证的非激活机制；区分自身被动显示与真正外部失焦。保持token取消保护，不笼统忽略FocusOut | 首次monitor及已创建两路径正常±Roll各一次提交；Esc/换工具/外部失焦零提交且恢复；必须先断言正常gesture实际产生preview/提交，再测试取消 |
| N16 | gesture保留原连续anchor及subpixel残差，在layout/viewport偏移实际应用；各zoom入口统一，不能每次从round后的状态取新anchor | pixel/angle正反20/100次，多个pointer/ppf，期间累计≤约定像素容差且回位；不以末尾0误差代替过程验收 |

### 2.3 功能与异步实现计划（N17–N24、N27）

| ID | 具体实施方式 | 关闭条件 |
|---|---|---|
| N17 | 显式重建真实caption_metadata.word_timings并保留其他metadata，区分timeline与source证据；downsample极短word制定最短帧/合并策略；test换成真实SubtitleClip | 24↔48/分数/降FPS cue与word同秒，字幕高亮/ASS/graphics/export一致，Undo与重载一致 |
| N18 | 三Source入口统一request/stream factory，所有有声/无声/纯音频分支传完整FPS/PTS与source availability；duration和sampling同map；无A轨明示建轨或选择策略 | Append timeline12采source24；静音OW/Append正确24帧；无A轨不静默丢流；逐帧采样、source windows、links、history均正确 |
| N19 | source identity更换使用完整clip before/after；targetFPS/PTS/cache/freeze按新stream重建；updatedcmd/JSON保留新字段。公共时间按group rational ticks定位，网格共用 | A24→B48→A、边界/内部切角、CFR↔VFR、reverse/ramp，Undo几何72保持72，旧PTS不混用，JSON3循环全payload等价 |
| N20 | 定义结构EditTransactionResult(project,sequence-map,updated-command)与统一history协议；内容快照不携发布revision语义；commit分别按当前project/seq revision单调推进；controller只执行事务 | 真实ProjectStack接受设置command；apply/Undo/Redo seq不倒退；多个seq不同rev/ref/cache/UI/audio/dirty一致，一步Undo |
| N21 | 两阶段signal/record携session/project generation、asset、source fingerprint、job ID；先get并核对完整身份，只有匹配回调才pop；旧probe不得借新job stat | 同path重入/跨项目/异source/同path内容变更乱序，旧callback零写入且不能清新pending；合法新probe仍可完成 |
| N22 | 后台ReplacePreflight明确success/diagnostic，不可解码或error字典零commit；source-derived metadata刷新，feature/user/unknown扩展按policy保留，更新颜色/thumbnail/cache；command JSON保留media_type/new metadata；完成时校验代次 | 坏/空源无history/revision变化、有可行动提示；PNG/HDR→SDR类型/色彩正确，multicam/future字段政策明确；取消/失败/Undo/JSON不丢数据。同步slot耗时是S/受控M，未冒称长盘实测 |
| N23 | export将cancel predicate传入前后scan；显式传播LoudnessAnalysisCancelled；normalize、第二scan、report原子发布前后检查，不能宽泛catch继续 | first/post scan中cancel无后续scan/报告/last_report/PCM发布，有延迟上限；外层最终取消不掩盖分析阶段多余工作 |
| N24 | Nest从source immutable track replace而非列字段fresh构造，明确替换ID/time-domain/routing，extras深拷贝；扫描同类fresh writers | future track→Nest/Bus闭包→Undo/保存重载仍保留；未知字段不执行，已知字段优先 |
| N27 | set_active_timeline/create_timeline等纯helper保留revision，导航不重置；结构及Relink实际writer经canonical commit adapter，内容变更一次+1，导航/derived状态制定明确policy | project7切已有timeline保持7，创建内容提交变8，Undo/Redo单调；Relink旧AI plan失效；G创建/切换不再把高rev归0 |

### 2.4 导出保真实现计划（N25–N26）

| ID | 具体实施方式 | 关闭条件 |
|---|---|---|
| N25 | timeline_export.py9366的ndarray不能与字符串作if判定。使用cache_key in cache或唯一object sentinel做identity比较，区分缺失/None/数组；保留fail-fast，不返回未调色帧 | 同exporter合法Gain/identity/拒快路参数连续≥3帧，无异常、缓存复用；实际CPU有调色短导出成功，非只有单次helper |
| N26 | 343个样本不能证明跨RGB函数可分离。短期对可能触发暗部耦合的参数走float；长期LUT只做可分离前缀、RGB保护单独stage，安全条件由公式证明 | 固定三个合法wheel反例（差8/2/4）、near-black/中间值/HDR域，fast/float满足声明预算；扩大grid只作找错，不作无误认证；配N25连续帧验收 |

## 3. 已声明仍未完成的基础工程

这些不是新的隐藏回归，但上一轮§0仍标部分/未做，不能因用户完成修复批次就删掉排期。具体新失败另已进入N表。

| 待办 | 当前状态 | 具体后续实现与验收 |
|---|---|---|
| H01：分数采样phase schema | 常速指定24矩阵21精确/3允许±1，新计划已声明 | 给time-map window增加分数phase/原曲线域，版本迁移；所有slice/mapper共用；原3组逐帧精确。N01的max22不属于此容差 |
| H02：真实mixed属性 | 多选仍显示primary值，未显示混合 | 每字段mixed状态/compatibility scope，输入仅改该字段；控件不把primary默认强推其他片；配N12/N14刷新验收 |
| H03：亚像素zoom origin | 仅integer scrollbar量化仍有平台边界 | 连续origin/残差进入viewport transform；统一slider/wheel/工具，低zoom明确帧/像素预算；N16长gesture作为门禁 |
| H04：设置UI/history接线 | 新领域command存在，但controller仍0execute/1load | 先N20协议，后接真实controller；设置+V/A/字幕/word/keys/ref一步Undo，旧时基历史不会混用 |
| H05：Multicam/Nest结构事务 | U02/U03未做，仍直接stamp/cache/ref | Create/Update group/Nest跨project/seq事务，失败先preflight零stamp，stable IDs/refcount/导航历史；sync worker带generation；3层Nest无孤儿 |
| H06：Multicam公共时基/grid/resync | 目标FPS局部换装已修，public源位置仍错 | group rational ticks→各源PTS，网格/Program同mapper；重sync迁移旧shots或固定sync版本、音频策略明确；配N19历史 |
| H07：Replace usage-index处置 | probe已新增，但短源/跨类型使用窗兼容未做 | usage map核对全部source windows/ramp/reverse/linked；给裁窗/retime/拒绝方案，用户审查后事务提交；失败与取消零提交 |
| H08：导出HDR hue与跨后端golden | R31补暗部保护/gray顺序已过原GPU测试；HDR clamp语义与共享IR仍部分 | Color/CPU/GL/Metal统一primary IR或生成片段；合法负值/>1、hue+saturation/u8/u16/multilayer golden，不只GL≈Metal；GPU资源沿原worker/队列 |

## 4. 专业工作流补齐表

F01–F16本批没有完整UI建设；保留原详细实施范围，并按本轮已改善基础及N项更新依赖。已有Source基础、多机位、Nest、字幕、WSOLA、Stem、ASR、FCPXML/OTIO不整体写成缺失。

| ID / 优先级 | 当前剩余功能 | 具体实现步骤与依赖 | 验收条件 |
|---|---|---|---|
| F01 / P1 | Source高级编辑：完整stream patch、单端/全源默认、3/4点、Replace Edit、Match Frame | 先关闭N07/N08/N18并统一SourceEditService；分别解析source/record I/O，3点求缺端、4点明确fit或保调策略；Replace保留目标时长/FX并计算源窗；Match Frame复用已修正逆映射定位正确asset/stream/source帧；操作使用command而非UI直接改model | V/A/纯音频、焦点切换、单端range、不同FPS/速度/锁轨、Source↔Program往返；每项一步Undo且无源越界 |
| F02 / P2 | Trim Monitor真实双/四画面，普通/ripple/slip/slide比较 | 先关闭N15；现有TrimMonitorDialog只有TC QLabel/Δ/headroom，不是双画面。加入原/新in/out帧槽；后台decode由session generation协调，低清代理和缓存去重；全部tool共享preview proposal，cancel收尾；数值trim保留 | 两侧画面与源mapper一致、拖动反馈连续、取消无晚帧；普通/roll/ripple/slip/slide及reverse/ramp均显示正确 |
| F03 / P2 | Edit关键帧Dope Sheet/Curve完整UI | 复用shared keyframe model/commands；Properties入口绑定clip/effect context，dock实现多点框选、frame/value、hold/linear/ease/Bezier手柄、复制粘贴；一次拖动一个command；明确absolute timeline域/局部显示转换，依赖N05/N12/N14及已修Bezier时域 | 多片段/效果切换、mixed值、曲线手柄、Undo/Redo、FPS变更与preview/export一致；锁轨无法编辑 |
| F04 / P1 | 实时preserve_pitch与离线保调一致 | 现实时是变调resample、离线已有WSOLA。建立streaming WSOLA缓冲、hop/latency和seek reset；接公共time-map/audio graph、rate cache和pitch policy；算法工作在线程，不阻塞UI；不支持范围明确fallback | 0.5/1.25/2x语音与tone音高对照，seek/loop/ramp不卡断；preview/export差异和延迟有测量 |
| F05 / P3 | 可选optical_flow高质插帧 | 模型字段已有、UI隐藏且后端未完整。注册可选backend+依赖探测；相邻帧flow/interpolation、遮挡/scene cut回退、缓存预算、取消；能力不足隐藏或明确降级，交付复用同算法 | 硬切不跨场景合成、遮挡无明显拖影；preview/export一致；backend缺失与显存不足可恢复 |
| F06 / P1–P2 | LUFS/峰值Normalize与Auto Duck完整操作 | 先关闭N23的导出取消接线；分析worker基于asset/window/revision缓存，Normalize提出目标gain并一个command提交；Auto Duck注册sidechain FX与routing/UI，参数attack/release/threshold/depth，经audio graph调用现有DSP；分析取消不提交 | 标准PCM目标LUFS/峰值与gain正确；对话期间音乐按设置下降；Preview/Export/Stem一致、Undo完整 |
| F07 / P1–P2 | Nest成熟化：retime音频、子转场、导航与Unnest | 先H05/N24，保留已修Bus闭包；递归time-map把子audio windows/automation/transitions映射父，支持reverse/ramp/pitch，多实例独立cache；breadcrumb保存父返回点；Unnest按引用与时间窗展开并预检冲突，不静默丢字幕 | 三层、多实例、倍速/反放、Bus/子audio transitions，帧和PCM一致；Nest/Unnest一步Undo无孤儿 |
| F08 / P1–P2 | Multicam动态网格、TC/marker同步、音频切角和重同步管理 | 先H05/H06/N19；统一group时基；增加TC/marker对齐策略；proxy动态网格按可见角度调度限帧decode；audio-follow-video/固定audio策略明确；group管理更新既有shots或固定sync版本 | 混合FPS长片角度切换不中断，网格/program对齐；TC/marker/波形同步有偏差报告；音频策略和Undo一致 |
| F09 / P2 | 字幕表格可编辑Text/Speaker/Status/时间、词级split、批译 | 当前表NoEditTriggers、split中点。添加delegate校验并走caption commands；按playhead/word边界split/merge；批量样式与翻译worker携revision，结果以可审查batch提交；源字词证据和graphics绑定保留 | 表格修改可Undo，错误时间/锁cue拒绝；词边界不丢字；切工程/旧翻译回调拒绝；先关闭N17 |
| F10 / P2 | 代理集中队列、可见进度、cancel/retry、容量和迁移 | 先N21/N22/H07；job manager保存worker、token、状态与progress；queue限并发、cancel安全收尾、retry新代次；缓存按source fingerprint和policy索引，迁移原子更新且不删使用中资源 | 批量取消/重试/磁盘满/切工程/同路径竞态；状态与真实产物一致，旧任务无回写 |
| F11 / P2 | 多版本备份、历史预览、恢复副本和损坏恢复 | 已有固定bak/autosave。新增版本manifest含timestamp/schema/checksum；异步immutable snapshot、临时写+原子rename；retention按时间/数量/容量；历史预览和恢复副本不覆盖原工程；校验损坏模块给诊断 | 中断写入/磁盘满/损坏最新版本可恢复前一版本；未知module保留；恢复副本资产引用有效 |
| F12 / P2–P3 | 交换保真矩阵与降级报告；可选AAF/EDL | 已有FCPXML/OTIO受控子集。以公共time-map列各格式clip/retime/transitions/FX/routing支持矩阵；unsupported输出结构化报告与可选render替代；AAF/EDL独立optional adapter，缺依赖不影响已有功能 | 外部软件实际roundtrip比较源窗/帧/PCM；复杂FX明确降级、不静默丢字段；导入先预览再原子提交 |
| F13 / P2 | 人工逐词文本剪辑：选词剪、lift/extract、停顿编辑 | 已有ASR/transcript/AI口播服务。新增text selection→word evidence→Source window映射；Lift保留gap、Extract共享ripple planner、停顿阈值可预览；linked/sync/markers/caption一起事务；低置信度边界可修正 | 选词对应正确源音画，撤销恢复全部对象；跨cue/锁轨/FPS/ramp不误剪；文本与字幕证据可追踪 |
| F14 / P2 | 能力/帮助/AI声明与实际入口、后端统一 | Stem AVAILABLE已修。将capability绑定action/backend/optional dependency及范围，不只notes字符串；生成帮助与AI manifest；对菜单、快捷键、service做契约测试；局部降级不让整个Edit失效 | AVAILABLE有实际入口与后端；PARTIAL能说清范围；缺依赖时UI/帮助/AI一致，True Peak算法说明与实际分析/取消能力同步 |
| F15 / P2 | 拖动边缘自动滚屏/卷轨 | 当前播放follow不等于drag autoscroll。InteractionSession指针进左右24–48px启动约16ms timer，按距离加速限速；垂直对应V/A/字幕区域bar；固定scene offset重算preview/snap；release/cancel/drop/close停timer | 长时间线多选/linked/Alt-copy可以拖到原不可见位置；取消即停、不换错轨、不额外提交 |
| F16 / P2 | A/V片段逐帧Nudge与精确Move | Alt箭头现仅字幕nudge，A/V箭头移playhead。注册可改快捷键±1/±10 actions与timecode/relative frame Move对话框；领域plan取全组合法delta，预检锁/碰撞/边界，一次Macro保持相对关系 | 低zoom下AV多轨组精确1帧移动；碰撞/0边界明确反馈；一步Undo、caption绑定/keys/marker同步 |

## 5. 下一轮实施顺序与关闭标准

| 批次 | 范围 | 要求 |
|---|---|---|
| A：交互/导出阻断 | N09–N15、N25 | 保留guard；处理拒绝与canonical恢复；修真实facade/旧值/snapshot刷新；正常Roll先成功再测取消；LUT连续帧先过 |
| B：内容与关联事务 | N01–N08、N17–N20 | 唯一time-map/slice/transform；完整before/after和稳定IDs；delete intent/marker/link/transition最终态一次validate；真实模型fixture替换fake字段测试 |
| C：异步/兼容/版本 | N21–N24、N27、H04–H07 | 全链job身份、后台换源preflight、调用者cancel、typed extras、真实提交协议；导航helper保留版本 |
| D：色彩与精度 | N16/N26、H01–H03/H08 | 数学证明安全条件、跨后端数值预算、phase/origin schema；不通过放宽错误容差把测试改绿 |
| E：高级效率与功能 | F01–F16 | 基础关闭后按用户高频优先F15/F16、F02/F03，再推进高级工作流；可选能力有明确降级 |

遵循`.clinerules`：不可变model与command/service/facade，UI只展示proposal/发信号；GPU操作沿现有线程/任务队列，释放沿deferred模式。新事务先规划最终状态再validate/发布/改变历史，拒绝/取消/no-op无内容、历史和revision副作用。内容Undo复原，发布revision单调。

每项关闭必须满足：当前失败输入消失，相关有效回归测试通过，受影响完整Qt接线/真实素材操作通过，保存/重载和preview/export语义一致，并记验证版本。单次handler、fake字段或提前自取消的gesture不能代替完整验收。

## 6. 验收边界与审计记录说明

本轮没有证明4K60、长时VFR/HDR、多设备音频漂移、耳听、录音/AI可选provider、全部硬编或外部软件交换通过；这些仍是独立Gate。Source CPU像素有证据，GPU shared texture首帧没有。Roll live preview的2x/0.5x仍有S级source±delta公式，待N15正常gesture修好后再测，未计为已动态确认的第28项。

审计驱动前三个会话各自保留。早期脚本曾缺timeline_id、访问不存在的_sectioned_panel或误用validator名字；这些是审计脚本错误，不计生产bug。随后按真实接口重跑的command05/06/07提供有效G证据。早期modal输入诊断未完成时只结束专用审计进程，未影响用户进程；NewTimelineDialog在后续真实动作已成功创建，不列创建阻塞缺陷。最终退出状态见本轮README。

ZIP重载差异仅清除两条Source Overwrite悬空link，计入N08；不错误泛化为普通工程保存丢任意内容。N06等JSON为公开命令契约，不表示当前工程格式保存完整Undo历史。不同probe统计按实际路径单独记录，不重复累加。

---

## §0 第三轮修复实施状态索引（2026-10-14 追加）

基线 `63c0bdea` → 修复 HEAD 见 git log；本索引只记录本轮 N 表 27 项与 H 表直接被推进项的真实状态，不改动原表正文。

| 项 | 状态 | 提交 | 说明 |
|---|---|---|---|
| N01 | ✅核心/◑残余 | e767e1da5 | SpeedParams 曲线控制域窗口模型（点位曲线坐标+域物化；split/trim/OW/insert/slip 全入口经 copy_clip_with 统一切片）；step 切分/裁剪/反向与混合帧率 ripple 逐帧精确；smooth 切分左半逐帧精确、右半 ≤±1（分数采样相位，归 H01 待 schema，原 max22 已消除） |
| N02 | ✅ | e767e1da5 | 等效位置连续传递（不再 int(round) 后乘速度）+ SpeedParams 段边界精确浮点积分；中性速度点 0 帧漂移 |
| N03 | ✅ | d5d71c8bd | Macro 复活尊重显式 RemoveTransition 与 owner 删除（clip_ids_before 推导）；整体 Move 保留合法转场回归通过 |
| N04 | ✅ | d5d71c8bd | Insert redo 快路径移到锁守卫之后；Slip/Slide/Roll/RippleDelete/BatchDelete/Append/Freeze 七类 apply 加 assert_track_editable |
| N05 | ✅ | d5d71c8bd | Insert marker 事务：undo 单次恢复（先 sync 逆变换再快照覆盖）、redo 用 after 快照、JSON 携带 before/after_markers；110/110→130/130→undo→redo 全对 |
| N06 | ✅ | d5d71c8bd | InsertLinkedAssetRipple 完整 before/after+created IDs+sync+markers 载荷与 redo 快路径（ID 稳定）；OW/Append fps 字段入 JSON |
| N07 | ✅ | d5d71c8bd | 同 seam 双轨右残片互 reciprocal；切分点右半 scoped marker 迁移右半新 ID 且随波纹 +delta；undo marker 快照 |
| N08 | ✅ | d5d71c8bd | OW 原子终态重建（一次 _rebuild+reanchor）；中覆盖左半保留原 ID、tail 转场与右半 marker 迁移、覆盖区/全删 marker 清除；undo 恢复转场/marker 快照 |
| N09 | ✅ | d63fb601f | 领域拒绝在信号边界收敛（单/批/Reset 三入口 try+canonical 恢复面板）；视图层 trim 双 finish 包裹+链接锁预检+_revert_trim_visuals_to_model |
| N10 | ✅ | d63fb601f | facade 导出 ChangeClipSpeedCommand；单选 Speed 实际走统一命令（linked 随动/锁组拒绝） |
| N11 | ✅ | d63fb601f | UpdateClipParamCommand apply 时捕获真实旧值（object.__setattr__）；批量 speed old_value 补全 |
| N12 | ✅ | d63fb601f | 面板 update_clips+refresh_clip_widgets_from_snapshots；批提交成功/拒绝后按 stable ID 从 canonical 刷新快照与控件 |
| N13 | ✅ | d63fb601f | Crop/Opacity/Speed/RAW Reset 单批原子（整组默认 dataclass，no-op 跳过零历史）；契约测试同步更新 |
| N14 | ✅ | d63fb601f | Reset Transform 后 set_clips 全量上下文刷新（不再 _show_clip_in_properties(first)） |
| N15 | ✅ | d63fb601f | TrimMonitor WA_ShowWithoutActivating + focusOut 按 ActiveWindowFocusReason×activeWindow 精确身份判定（真外部窗口仍取消）；正常 Roll 完整提交回归通过 |
| N16 | ✅ | 614d53e5e | 手势级连续锚帧缓存（0.8s 窗）+绝对目标解析推导；20+20 次累计 2.10px→≤0.5px（整数滚动条半像素界，无累计） |
| N17 | ✅ | be2fb1b26 | 真实 caption_metadata.word_timings 随 FPS 同域缩放（保留 speaker 等其余字段）；旧 fake 顶层字段测试改真实模型 |
| N18 | ✅ | be2fb1b26 | Append A/V common 与 OW/Append 兜底三入口全部携带 fps 时基（无声 48fps 源 duration24/采样2:1） |
| N19 | ✅ | be2fb1b26 | UpdateClipSource/切换角 no-split 完整 clip 快照 undo（fps/源窗/时长/PTS）+换源清 PTS+updated 保留 fps+JSON；multicam 公共时基/grid 仍属 H06 |
| N20 | ✅ | be2fb1b26 | ProjectTransactionResult 协议；真实 ProjectStack 可提交设置命令（7→8→9→10 单调），内容快照不携发布 revision |
| N21 | ✅ | 17b0a02b5 | 二阶段信号/pending 携 asset_id+generation 三重身份；回调 get 核对后才 pop（旧回调零写入且不清新 pending） |
| N22 | ✅ | 17b0a02b5 | 探测失败/error 字典零提交零历史+可行动提示；metadata 迁移（feature 保留、色彩导入同口径刷新、缩略图失效）；命令 JSON 补 type/color；后台 ReplacePreflightWorker 仍属 H07 排期 |
| N23 | ✅ | 17b0a02b5 | 两次响度扫描传取消谓词、normalize 后二次核验、LoudnessAnalysisCancelled 显式重抛不落宽泛 except |
| N24 | ✅ | 17b0a02b5 | Nest 子轨 dc_replace 源轨（extras 深拷贝保留，transitions 仍由迁入逻辑显式添加） |
| N25 | ✅ | d63fb601f | 缓存命中改成员判定（ndarray/None 语义区分），连续 3 帧无异常 |
| N26 | ✅ | d63fb601f | 可分离性证明制：双上下文一致 + 全非负或闭式 luma 下界 + 精确 canary；固定反例 wheel 拒至 float 得 GradingMath 正确色 |
| N27 | ✅ | 17b0a02b5 | set_active_timeline/create_timeline 改 replace 保留 revision（导航 7→0 已消除）；Relink 统一 commit adapter 接线仍属 H04 |

H 表推进：H04 的领域协议（N20）就绪待接 controller；H01 的 smooth 窗口右半 ≤±1 残差与分数相位同源，待 schema；其余 H02/H03/H05-H08 未动。F01-F16 未动。

新增回归：tests/edit/test_round3_{a_interaction,a_lut_cache,a_roll_focus,b_transactions,b_features,c_async_compat,d_zoom_gesture}.py 共 42 项，全部通过；既有契约测试同步更新 6 处（crop reset 原子批、设置事务协议、N21 身份载荷、OW 左半保留 ID、ripple fps 边缘、词级真实字段）。
