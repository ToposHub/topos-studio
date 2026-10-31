# 剪辑页面全面审计与优化计划表

检查开始：2026-10-13；计划整理：2026-10-14（Asia/Shanghai）。审计版本：`55351a5f45d5a34ff2fc91a3cb9eee53581a889b`，开始时工作树干净。项目根：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。

本次按项目 AGENTS.md 分工完成架构、核心命令与交互审查，并以 `.clinerules` 的线程/GPU、不可变模型、command/service、Undo/revision、预览/持久化一致性为约束。**未修生产代码**；交付是新的问题计划表、详细实现方案和可重复测试证据。

## 0. 实施状态索引（2026-10-14 实施批次）

本节由实施方追加，逐批记录已落地范围；状态流转见 §13。**实施版本**：本文件提交序列 `fix-A1` → `fix-D`（见 git log）。

| 批次 | 已实施（追踪ID） | 主要交付 |
|---|---|---|
| A1 | INT-14 | Source GL facade 补 glUniform1f/4f + 契约检查 + paintGL 失败隔离（P0 崩溃） |
| A2 | INT-01/02/03/04/09 | 波纹单击防护、Esc 统一取消、手型/中键平移、刀片左键+锁轨、选区身份去重 |
| A3 | C09/INT-05、INT-08、FE-B05 | 命令层统一轨锁守卫、菜单状态正确、源 range_changed 信号 + I=O 单帧 |
| B1 | C01/C02/C03 | Roll 原子撤销、Insert 全轨快照 undo/redo、批量波纹累计位移 |
| B2 | C11/C12/C14 | 转场快照与引用迁移、转场重锚（reanchor）、Slide 快照 undo/redo |
| B3 | C08、FE-B10、FE-B11 | 链接 A/V 波纹宏、project helper 保留 revision、代理任务 generation token |
| B4 | APP-01、FE-B02(部分)、FE-B03 | 提交边界 revision 单调推进、设置提交 revision+dirty、源编辑 A/V 路由（Insert 原子 A/V；Overwrite/Append 宏+链接回填） |
| C1 | C04/C05/C06(=INT-12)/C07(=FE-B04) | reverse 显式映射、方向感知切分（含速度点分组）、修剪时域映射（模型 helper+命令显式值）、混合 FPS 统一时长换算（含变速点积分） |
| C2 | C10/C13/C15/C16/C17、FE-B01 | fade 钳制、关键帧随动平移、切分 marker 迁移、跨轨 marker/关键帧跟随、右修源上界、FPS 重映射补齐（字幕/marker/动画/automation） |
| C3 | FE-B06/FE-B08/FE-B13/FE-B15 | 多机位组公共时间、嵌套轨道语义保真+动画平移、SMPTE 时间码统一、4x 过采样真峰值 |
| D | INT-06/07/10/11/13、FE-B14、APP-03(布局) | 滚轮归一+锚定缩放、空快捷键语义、字幕边界/删除统计、多选属性上下文、能力表 Stem 状态、过矮 dock 修复 |
| E+防护 | R01/R02/R03 | 命令快照深拷贝、ClipInstance 未知字段 roundtrip、validate_timeline 增加 seam/双向链接/marker 归属不变量 |

**未实施/部分实施**（依赖或规模原因，供后续排期）：

- FE-B02 完整单步 Undo 事务（revision/dirty 已落地，命令栈事务待 FE-B01 的 UpdateTimelineSettingsCommand 合并）
- FE-B03 的 3/4 点、Replace Edit、Match Frame（FE-F01 范围；Overwrite/Append 播放头在片段内部时的音频配对走回退路径）
- FE-B07/FE-B09（多机位同步/嵌套创建的工程级 Undo 事务）、FE-B12（Replace 探测）——见 §11 详情
- APP-02（局部 Blade 与 linked selection 契约）、APP-03 的 i18n 统一
- FE-F01~F14 功能补齐、UI-GAP、DOMAIN-01/02 服务的完整统一（DOMAIN-01 的快照式 undo 已在各命令落地，独立 planner 服务待整合）
- §7 验收门禁 G01~G09（需真实窗口/硬件/外部条件，见各 Gate 说明）

## 1. 当前判断与证据等级

基础剪辑已能导入、显示真实画面、常规联动裁剪、播放、标记、保存重载与实际导出；但源监视器会崩溃，取消/锁轨/手型/波纹单击存在真实操作问题，复杂裁剪的Undo、帧映射及关联状态存在多组缺陷。当前体验不能称为稳定顺手，尤其不宜把“现有测试全过”解释为所有编辑组合正常。

问题索引按共同根因合并为 **45 项修复/体验任务**；另列 **22 项功能补齐与工程能力任务**。C06=INT-12、C07=FE-B04、C09=INT-05在索引合并，详细分项保留原ID供追踪；功能表中的修复依赖不重复估算工期。静态风险另表，不混入已复现bug数量。所有任务初始状态为“待实施”，负责人未分配。

- **G**：真实 `MainWindow(test_mode=False)`、cocoa可见窗口，实际Qt鼠标/键盘/拖放/滚轮事件、真实模型和command stack；有截图/状态记录。使用QTest自动驱动，并非人工用系统鼠标逐项操作或人耳听感评价。
- **Q**：offscreen Qt事件或界面集成（部分菜单用exec替身选择真实action），有独立probe脚本；不冒充桌面手测。
- **M**：真实不可变模型、command、mapper或实际handler的独立执行；合成时间域用于mixed FPS/反放等确定边界，不冒充已测全部素材格式。
- **S**：完整源码调用链/能力声明确认；竞态或硬件结果仍需专门复测。**R**为防护性风险，尚未证明实际损害。

首次Source崩溃使用默认异常路径，日志见 [source-viewer-fatal.log](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-13-edit/evidence/source-viewer-fatal.log>)。后续仅在审计脚本安装excepthook保存未捕获Qt异常，使其余交互可继续；这不是修复，后续Source像素/播放不能记为通过。波纹无拖动单击也实际触发未捕获异常。生产脚本/应用没有被改动。

## 2. 测试环境、素材与真实结果

主窗口：Python 3.12.13、Qt 6.10.0、macOS cocoa、Studio全部相关feature注册，1440×1000正常窗口，并观察初始3840×2049状态；配置与工程均为隔离临时副本。测试素材来自用户提供的 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/test_videos`，文件只读未改。

| 实际素材 | 元数据 | 实际覆盖 |
|---|---|---|
| 2K_h264.mp4 | 1920×1080、25fps、1572帧、AAC stereo | 媒体池外部文件drop、源双击、timeline A/V drop、普通trim/undo/redo、blade、roll、slip、slide、rate stretch、Esc、H、锁轨、滚轮、I/O、标记、copy/paste/delete、播放、导出 |
| 2K_prores422Proxy.mov | 1920×1080、25fps、487帧、PCM24 | 导入、A/V drop、节目定位与真实运输按钮播放（325→369，40个非空frame_ready）；截图20-direct-prores |
| 2K_topos_422_Proxy.mov | 1920×1080、25fps、487帧、PCM24 | 原生TOPOS识别/导入、A/V drop、节目定位与真实运输按钮播放（817→860，43个非空frame_ready）；截图20-direct-topos |

这些选中实片均是25fps。30→24、24→48和fractional FPS的结论来自M级模型测试，**本次没有对全部test_videos里的4K/HDR/alpha/codec档位做实片逐一验收**。约2秒短播放只证明当前样本路径可用，不作为4K60FPS/长时间同步性能结论。

| 可见窗口操作/服务实测 | 结果 | 证据 |
|---|---|---|
| 空工程→导入三个实片→拖入H264 | 成功，drop生成双向链接V/A；画面与波形可见 | 02-imported / 03-linked-av |
| 普通右边trim及Undo/Redo | V/A 1572→301→Undo1572→Redo301，同步变化 | ordinary_right_trim / ordinary_trim_undo / ordinary_trim_redo |
| Source双击/清范围/单帧 | 绘制崩溃；清范围后按钮仍启用；I=O禁用 | fatal log / source_clear_io / source_single_frame |
| Source追加三段 | 3×50帧视频，音轨为空；有声源最终导出无AAC | source_append_three / export-validation.json |
| Source Insert/Overwrite | 实际提交并变化；中间Insert未复原，独立command复现给出永久洞；Overwrite Undo恢复本次前态 | command-08 / C02独立输出 |
| 刀片、Roll | 局部只切V；锁轨仍切；Roll149→138，Undo抛overlap | blade_split / locked_track_blade / roll_negative_undo |
| 拖动Esc、手型H | Esc后release仍将149→168；H把149→167且scroll不动 | escape_move / hand_tool |
| 波纹无拖动右边单击 | 发new_end=0，引发未捕获TimelineOperationError | ripple_zero_click / uncaught-qt-exceptions.log |
| Slip/Rate Stretch/Slide | 基础gesture提交成功；复杂gap/fade/source问题另有M级复现 | slip_tool / rate_stretch_tool / slide_tool |
| 水平滚动/Ctrl水平滚动 | scroll100不变；zoom4.016→3.347，意外缩小 | horizontal_scroll / ctrl_horizontal_scroll |
| Fit/Zoom、M、复制粘贴/删除撤销 | 基础操作有模型变化或状态变化；组合与字幕问题另列 | zoom_tool / zoom_fit / marker / copy_paste / delete_key_undo |
| 节目运输按钮H264 | 10→68，22个非空frame_ready，约2.2秒 | program_button_playback |
| 保存重载 | 两条sequence完整to_dict相等，3个asset保留 | save_reload / audit-scratch.topos |
| 实际短片导出 | 两条timeline经原TimelineExporter实际输出H264640×360，范围[0,49)，各49帧；原A/V工程有AAC，Source追加工程无音轨；PyAV重解码首帧有有效图像 | export-smoke-results / export-validation / smoke MP4 |
| 现有功能tests | 主任务25个测试文件381 passed，8.84s；其他分项133 passed、57 passed（有重叠，不相加宣称唯一覆盖数） | pytest-functional.log/xml；分项复现文件 |

中间尝试的`codec_playback_*`和`real_codec_transport_*`记录含锁轨拒绝/未成功切中目标序列的操作，不计为编码播放通过或播放引擎bug；最终`direct_selected_codec_*`先用既有序列切换服务设fixture，并将播放头定位到指定素材段，再实际点击可见运输按钮，成功且保存截图。跨编码定位属于fixture准备，不作为滚动到可见尺后定位交互通过的证据。原始记录保留，使结论可以复核。

证据目录：[全部交互记录、脚本、截图和产物](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-13-edit/evidence>)；测试driver：[interactive_driver.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-13-edit/interactive_driver.py>)。完整界面功能矩阵见下方“详细功能分项”，包含工程/媒体池/监视器/音频/字幕/多机位/嵌套/场景切分/AI/交换/导出等15类；“已有但部分接线不完整”与“完全缺少的新工作流”分开写。

## 3. 统一问题与优化计划表

P0：会中断正常操作的崩溃路径。P1：编辑正确性/数据/Undo/时域/交付一致性。P2：常用效率与一致交互。阶段A为可用性止血，B为事务完整性，C为time-map/音画，D为交互专业化，E为高级扩展。每项详情包含具体代码行、触发、实施与验收，点击ID可定位；状态统一“待实施”。分项保留原始等级，主表以本次复现补充更新（INT-01升级P0，FE-B03/05已补G级证据）。

| 追踪ID（详细方案） | 优先级 | 证据 | 问题与影响 | 实施摘要 | 最低验收 | 阶段 |
|---|---|---|---|---|---|---|
| [INT-14](#int-14) | P0 | G+S | 双击素材进入 Source 即因缺少 glUniform4f 退出；下一处 glUniform1f 也缺失 | 同时补 OpenGL import 和 facade API，增加调用契约检查；绘制失败进入明确错误状态 | CPU/GPU首帧、缩放、flip、切源、关闭均不崩且有正确图像 | A |
| [INT-01](#int-01) | P0 | G+Q | 波纹工具无拖动单击右边缘仍提交 end=0，本次产生未捕获 TimelineOperationError | 按下初始化 final；阈值内/无变更不提交；失败统一恢复预览并提示 | 左右单击、拖回原位、上次操作后再单击均无命令/异常 | A |
| [C01](#c01) | P1 | G+M | 向左 Roll 后撤销抛片段重叠异常，历史被堵住 | 左右 clip、转场/标记使用完整快照原子恢复 | ±delta、连续三次 undo/redo 的完整 to_dict 复原 | B |
| [C02](#c02) | P1 | M；G未恢复原形 | 在片段中间 Insert 的 undo 不还原原片段，留下洞/残片 | 保存跨点切分与所有 follower 的 before/after；redo复用ID | 100帧片在40插10，再undo恢复单一100帧且无洞 | B |
| [C03](#c03) | P1 | M | 多跨度波纹删除跨轨少移或undo只恢复中间位置 | 在原坐标系求累计delta，每对象仅存最初before与最终after | 50→30→undo50；30→10，含多个离散删除范围 | B |
| [C04](#c04) | P1 | M | 有FPS元数据时 reverse 被正速度有理映射忽略 | 显式使用 SpeedParams.reverse，与有理数/PTS逆映射共用规则 | source100–199倒放首3帧199/198/197，各入口一致 | C |
| [C05](#c05) | P1 | M | 倒放/分段速度切分改变原画面，尾帧重复或右段回到1x | 实现 direction-aware clip slice，切速度点、重定位base和easing | 切分前后逐帧source序列相同，身份变化不改内容 | C |
| [C06](#c06) / [INT-12](#int-12) | P1 | Q+M | 2x鼠标Trim把timeline帧当source帧，snap覆盖正确源区间 | Item只报timeline边界，preview/commit都调用同一time-map service | 2x裁10timeline帧等于20source帧，开关snap同值 | C |
| [C07](#c07) / [FE-B04](#fe-b04) | P1 | M | 混合FPS变速与源Insert/Overwrite/Append时长算错 | 用Fraction按源秒时长×timelineFPS÷speed求placement与ripple | 30fps的120帧→24fps下2x恰48；30源帧1s恰24 | C |
| [C08](#c08) | P1 | M实际release handler | 波纹Trim只改视频，仍链接的音频保持旧长度 | A/V editing unit 原子ripple事务，伙伴与follower完整预检 | V/A100→80同时变化，任一锁定/冲突整体拒绝 | B/C |
| [C09](#c09) / [INT-05](#int-05) | P1 | Q+M；链接为S | 锁轨仍能菜单变速/禁用，未锁主clip还能修改锁住的伙伴 | 统一 affected editing-unit 锁守卫；UI/属性/API共用 allowed/reason | 所有内容写操作不绕锁；拒绝时无半提交/历史/revision | A/B |
| [C10](#c10) | P1 | M | 带40帧淡出片段切分/缩到20帧抛 ValueError | 统一fade envelope slice策略，快照保存并恢复全部fade | blade/trim/speed/slide缩短不崩，undo恢复原40帧fade | B/C |
| [C11](#c11) | P1 | M | Split/Delete自动删相关转场，undo不能恢复 | 事务包括referencing transitions；切分迁移头尾、undo完整回填 | 头尾/双边/音频转场及params、IDs均可完整恢复 | B |
| [C12](#c12) | P1 | M | 移动和同步跟随后转场仍在旧绝对时间，sequence已不合法 | 用clip edge/seam锚或统一time-transform重求转场中心 | move/ripple/roll后 validate_timeline零转场错误 | B |
| [C13](#c13) | P1 | M | 移动片段后绝对关键帧留旧时间，原首帧动画变终值 | 统一clip-local动画或全入口平移clip/effect keys，迁移旧schema | move/duplicate/nest/ripple后局部曲线不意外变化 | C |
| [C14](#c14) | P1 | M；G基础slide | Slide吞掉原间隙，逆向slide式undo无法恢复fade/gap | 保存主/邻片完整快照；明确gap政策，原子恢复 | 有gap及fade80→50的slide undo恢复gap与fade80 | B |
| [C15](#c15) | P2 | M | 切分后右半的clip marker仍属于左半 | slice按内容位置重新绑定marker，保存before归属 | marker在正确半段，后续移动跟随正确对象 | C |
| [C16](#c16) | P2 | M | 跨轨Sync Lock只移动clip，scoped marker不跟随 | follower transform一起处理marker/keys/transitions | clip50→40时marker55→45；undo和保存一致 | B/C |
| [INT-02](#int-02) | P1 | G+Q | 拖动中Esc清选中，松手仍提交移动 | 把所有item动作纳入InteractionSession；取消释放grab并恢复快照 | Esc/换工具/失焦后release无提交，所有ghost/snap消失 | A |
| [INT-03](#int-03) | P1 | G+Q | 手型H在片段上移动内容，空白框选；中键也不平移 | press路由优先HAND/临时中键pan，隔离编辑item | 空白/clip/字幕/转场上只滚屏、不改模型/selection/undo | A |
| [INT-04](#int-04) | P1 | G+Q | 局部Blade无锁检查；右键也切片段 | 仅左键切分，right menu/middle pan；domain再校验锁 | 锁轨和右击无切分；正常与Shift全轨语义一致 | A |
| [INT-06](#int-06) | P2 | G+Q | 触控板水平/像素滚动无效，Ctrl+x方向事件反而zoom-out | pixelDelta优先、x/y归一化、zero delta不缩放、pointer anchor | Mac惯性与鼠标均顺手，缩放鼠标下帧误差≤1帧 | A/D |
| [INT-07](#int-07) | P2 | Q+S | 快捷键显式清空后仍回默认B；N登记名称与Snap行为不一致 | 区分不存在与空binding，统一ActionRegistry与提示文案 | 清空/改键/重启不误触，提示随用户按键更新 | D |
| [INT-08](#int-08) | P2 | Q | 已禁用片段菜单仍显示Disable Clip | 用唯一is_enabled状态生成menu，统一批量混合态 | 启用/禁用/Undo/Redo菜单与真实状态一致 | D |
| [INT-09](#int-09) | P1 | Q | Shift框选重复加入同一ID，虚假multi-selection | Selection及command builder稳定去重，primary纳入不变量 | 重复框选10次仍一个对象，复制/删除只处理一次 | A |
| [INT-10](#int-10) | P2 | Q | 字幕不参与Fit Selection/上下边界跳转 | 建立统一timeline object range iterator，明确visible/target scope | subtitle100–200：50按Down到100；字幕选中可Fit | D |
| [INT-11](#int-11) | P2 | Q集成 | 删除有字幕cue轨不询问，而媒体轨询问 | 统一track content summary，cue/transition/binding计数 | 取消无修改，确认后删除，undo恢复内容与绑定 | D |
| [INT-13](#int-13) | P2 | Q | Properties多选被set_clip截成第一片段，无mixed状态 | SelectionContext保持全部IDs；mixed widgets+兼容参数Macro | 两片段批量更改/Reset，单步undo各自旧值 | D |
| [FE-B01](#fe-b01) | P1 | M | 改序列FPS只缩放媒体，字幕/动画/标记/自动化错时 | 统一sequence timebase transform，source帧坐标保持不变 | 24↔48与fractional FPS，各序列域实时时间误差≤1目标帧 | C |
| [FE-B02](#fe-b02) | P1 | S | 序列设置旁路Undo与revision，仅改缓存/dirty | UpdateTimelineSettings原子工程事务同步ref/sequence/cache | 一次undo恢复设置与payload，旧AI plan失效 | B |
| [FE-B03](#fe-b03) | P1 | G+S | 源Append实测仅V没有A；Insert/Overwrite同单clip路由 | SourceEditService统一stream patch、媒体类型与linked命令 | 有声视频三个入口都V/A；纯音频落A，一步undo | B/C |
| [FE-B05](#fe-b05) | P2 | G+S | Clear后工具条仍启用；I=O单帧范围被禁用 | 统一range_changed，open/clear/close均发，有效I≤O | 清空立即禁用；单帧可插1帧，切源不残留状态 | A/D |
| [FE-B06](#fe-b06) | P1 | M | 多机位A→B→A累计角度同步offset | 映射到group公共时间，减active offset后加target offset | frame20回A源20，不是30；grid与节目同帧 | C |
| [FE-B07](#fe-b07) | P1 | S | 多机位同步/部分创建不Undo、不dirty/revision，失败可能先改资产 | 预检后Create/UpdateMulticam project command，结果带generation | 锁轨创建失败不改工程，同步可undo/保存，旧worker不污染 | B |
| [FE-B08](#fe-b08) | P1 | M | 创建Nest丢音轨volume/pan/mute/FX/routing，动画未减start | 完整track复制与routing映射，统一动画/automation time transform | 原静音仍静音，PCM/画面/局部动画在嵌套前后相同 | B/C |
| [FE-B09](#fe-b09) | P1 | S已声明 | 创建Nest增加asset/child并替换父，却无法单步撤销 | 跨project+sequences复合事务，引用计数/导航随历史恢复 | Nest→Undo→Redo无孤儿，父/子/asset/ref完整恢复 | B |
| [FE-B10](#fe-b10) | P1 | M | relink/replace/update_proxy把工程revision7重置0 | 用replace保留字段；只有commit边界单调递增 | 纯helper仍7，提交8，undo/redo仍单调，旧plan被拒绝 | B |
| [FE-B11](#fe-b11) | P1 | S竞态 | 换源pop任务映射后，旧代理success/error因expected=None被接收 | project+asset+fingerprint+generation+job token严格匹配 | 换源/删除Undo/切工程乱序回调不污染新对象 | B |
| [FE-B12](#fe-b12) | P1 | M+S调用链 | Replace未probe，video换jpg还保留video旧FPS/时长/尺寸 | 后台probe、usage-index校验源窗与媒体类型，预览批准后事务提交 | 短源/跨类型有明确处置，元数据正确，undo还原旧素材 | B/C |
| [FE-B13](#fe-b13) | P1 | M+S | fractional FPS源/节目/字幕时间码用实际fps浮算，相互不一致 | 全UI使用既有Rational SMPTE converter，显式NDF/DF | 29.97 frame29为:29；1799为00:00:59:29(NDF)，roundtrip无漂移 | C/D |
| [FE-B14](#fe-b14) | P2 | S | 能力表仍把已有Stem Export写成无UI/休眠 | registry entry与菜单、backend自动契约校验，统一帮助/AI manifest | 实际能力状态与当前入口一致，缺依赖仅降级单项 | D |
| [FE-B15](#fe-b15) | P1 | S精度缺口 | 样本峰值被标为True Peak/dBTP，QC指标误导 | 有状态4x插值true-peak estimator；无实现则标Sample Peak dBFS | 参考intersample信号与跨block测试，manifest正确标指标类型 | C |
| [APP-01](#app-01) | P1 | G+S | 多步手动剪辑后timeline/project revision一直0，AI乐观锁看不见人工修改 | 将UI command commit/undo/redo与EditProjectService统一；revision单调，ref/cache同提交 | 人工任意编辑后旧base_revision plan拒绝；1次macro仅增1 | B |
| [APP-02](#app-02) | P2 | G+S；设计问题 | 默认linked selection开时局部Blade只切V，右段脱链，左段继续挂完整A | 定义局部与linked editing-unit切分策略；联动切分或显式安全解链，勿保留误导链 | 左右V/A组对应正确，后续trim/move不裁错整音频；Shift策略不回归 | B/C |
| [APP-03](#app-03) | P2 | G体验 | 1440×1000初始时间线dock仅102px，头/尺后几乎看不清轨道；中英UI混杂 | 新布局按可用高度设置可操作初值/最小轨区；用户手动布局优先；统一I18N与工具提示 | 1280×800/1440×900/1920×1080可见V/A与工具；saved layout不反复被强制覆盖 | D |

## 4. 静态风险与防护任务（仍需复测，不冒充已发生bug）

| ID | 优先级/证据 | 风险 | 具体实施与验收 |
|---|---|---|---|
| C17 | P1/S | 裁剪source上界只看duration，漏source_start；非零In可能延到真实素材之外 | 用SourceAvailability统一source区间边界；总源200、In100延长到150必须拒绝exclusive end250；UI/API/AI都检查，真实中段素材复测 |
| R01 | P2/R | frozen模型的嵌套dict/params仍可原地修改，潜在污染历史快照 | 查所有writer，递归不可变或copy-on-write；实测改新snapshot不影响旧to_dict，含effects/keyframes/字幕 metadata |
| R02 | P2/S结构 | clip/sequence主block未知字段不像caption extras那样统一保留 | 建schema version/extension payload和迁移；用未来字段fixture读写roundtrip不丢，未知类型不得无条件执行 |
| R03 | P1/S结构 | validate_timeline未全面检查seam、marker归属及linked范围一致性 | 加editing-unit和附属对象不变量；对本次无效转场/越界marker/错误链接给具体诊断；提交前阻断非法状态，legacy导入先诊断可恢复 |

## 5. 缺失或未接完整功能的实施计划表

下表的“缺失”精确限定到工作流。项目已有波纹/滚动/滑移/滑动、变速/反放/关键帧、音频混音/ADR/Stem、字幕交付、多机位、嵌套、代理、FCPXML/OTIO及AI服务，不能把这些整体写成未实现。修复基础一致性后再扩入口，避免把新UI接到同样错误的命令。

| 方案ID | 优先级 | 要补齐的具体功能 | 已有基础/缺口边界 | 依赖/落地顺序 |
|---|---|---|---|---|
| [FE-F01](#fe-f01) | P1 | 源剪辑完整工作流：stream patch、单端/全源、3/4点、Replace/Match Frame | 已有I/O和三个编辑入口，当前音频/FPS分叉 | B/C；源mapper与联动事务 |
| [FE-F02](#fe-f02) | P2 | 双/四画面Trim Monitor：roll/ripple/slip/slide两端与邻接画面 | 已有单帧trim preview信号 | C/D；后台decode与session generation |
| [FE-F03](#fe-f03) | P2 | Edit动画Dope Sheet/Curve、多点/Bezier/复制粘贴 | 已有关键帧按钮/模型/command | C；动画时域迁移 |
| [FE-F04](#fe-f04) | P1 | 实时preserve_pitch与导出保调一致 | 已有离线WSOLA，实时尚未完整 | C；streaming WSOLA与audio graph |
| [FE-F05](#fe-f05) | P3 | 可选光流变速/插帧，scene cut与遮挡处理 | 模型字段存在，当前UI隐藏后端未实现 | E；可选backend和GPU资源能力 |
| [FE-F06](#fe-f06) | P1/P2 | LUFS/峰值Normalize + 可调Auto Duck完整工作流 | 已有analyzer/gain/DSP，没有完整UI/engine链 | C/D；真峰值/worker/cache/FX registry |
| [FE-F07](#fe-f07) | P1/P2 | Nest成熟化：事务Undo、变速音频/子转场、breadcrumb/Unnest | 已有嵌套预览、1x音频flatten和导出 | B/C/D；recursive time-map/audio graph |
| [FE-F08](#fe-f08) | P1/P2 | Multicam成熟化：公共时基、TC/marker同步、多路proxy动态网格、音频切角 | 已有波形同步、静帧网格、1–9切角 | B/C/D；group schema与decode预算 |
| [FE-F09](#fe-f09) | P2 | 字幕工作区直接编辑、Speaker/Status/时间、词级拆分、批量翻译 | 已有字幕属性编辑/服务/交付与AI翻译provider | D；caption commands/worker/revision |
| [FE-F10](#fe-f10) | P2 | 代理集中队列、进度、cancel/retry、容量与缓存搬迁 | 已有ProxyPolicy/生成器/磁盘代理 | B/D；先修job token |
| [FE-F11](#fe-f11) | P2 | 多版本备份、历史恢复预览、恢复副本与损坏工程工具 | 已有固定bak和autosave恢复 | D；异步snapshot/atomic IO/retention |
| [FE-F12](#fe-f12) | P2/P3 | 交换保真矩阵、降级报告/渲染替代，可选AAF音频/EDL | 已有FCPXML/OTIO受控子集，非完全缺失 | E；核心time-map修复与第三方实机验收 |
| [FE-F13](#fe-f13) | P2 | 人工逐词文本剪辑：选词剪、lift/extract、停顿编辑 | 已有ASR、transcript、语义索引和AI口播规则 | D；word evidence+linked/sync事务 |
| [FE-F14](#fe-f14) | P2 | 能力发现/帮助/诊断一处真实来源 | 已有registry和Effect Catalog，部分声明过期 | D；入口和backend契约 |
| [UI-GAP-01](#ui-gap-01) | P2 | 片段/多选/复制/字幕拖至边缘自动滚屏与自动卷轨 | 播放头有跟随；编辑拖动边缘计时器缺 | D；统一interaction session |
| [UI-GAP-02](#ui-gap-02) | P2 | 媒体组±1/±10帧微移与精确Move时间码弹窗 | 字幕已有nudge，A/V不完整 | B/D；editing-unit collision/lock |
| [UI-GAP-04](#ui-gap-04) | P2 | 多选mixed Inspector和Paste Attributes/批量兼容效果 | 目前Inspector只保留primary，blend有特判 | D；SelectionContext+schema兼容Macro |
| [UI-GAP-06](#ui-gap-06) | P2 | source/timeline/program/text/modal统一动作与焦点上下文 | 已有部分shortcut scopes；本次未证明JKL偷文本 | D；真实窗口focus矩阵，先验证再修 |
| [DOMAIN-01](#domain-01) | P1 | 统一时间变换事务：slice/move/trim/ripple/roll/slide附属状态一起变换 | 目前多套运算各自漏状态 | B/C；不可变planner+完整before/after |
| [DOMAIN-02](#domain-02) | P2 | 跨edit-point长音床/字幕Sync Scope和冲突预览 | 同步锁只处理起点在编辑点之后的媒体 | B/C；统一slice与字幕word timing |
| [DOMAIN-03](#domain-03) | P1/P2 | SourceAvailability素材handles服务与预览余量提示 | 现有边界检查散落，部分command不提供source max | C/D；媒体/图像/生成器/嵌套不同政策 |
| [DOMAIN-04](#domain-04) | P1 | 编辑事务组合回归矩阵与真实app验收门禁 | 基础tests多，但真实事件/组合边界漏测 | A至E持续；source indices、undo全payload、资源取消 |

每个功能详情必须按“模型/领域服务→命令与revision→UI信号→预览/导出→兼容持久化→回归验收”闭环实施；下面保留详细步骤和人日相对估计。估计不是承诺日期，具有共同依赖的任务不应简单累加或重复开发。

## 6. 推荐落地顺序与完成门槛

| 阶段 | 先交付的范围 | 完成门槛 |
|---|---|---|
| A，可用性止血 | Source GL桥接、波纹单击、Esc、手型/中键、Blade锁/右键、selection去重 | 真实窗口无未捕获槽异常；无动作操作不入历史；导航工具永不改内容；锁操作各入口一致 |
| B，编辑事务正确 | Roll/Insert/批量ripple/Slide Undo、转场快照、A/V editing-unit、UI revision、Nest/Multicam工程事务、代理generation | 所有操作apply→undo完整payload相等；redo稳定IDs；失败零半提交；所有写入单调revision，旧AI plan拒绝 |
| C，源时间与音画 | reverse/slice/mixedFPS/trim/retime、FPS重采样、动画/标记/seam、源路由、实时保调/真峰值 | 未改变的内容逐帧source映射不变；preview/export/reload同采样；声音时长/音高/指标有确定标准信号验收 |
| D，顺手与专业工作流 | 触控板anchor/edge-scroll/nudge、mixed Inspector、Trim Monitor、字幕表、proxy中心/备份、help/i18n | 低zoom可精确编辑；拖至屏幕外可完成；多选不误编辑；失败有明确可行动提示；设置/快捷键一致 |
| E，可选高级交付 | 动态Multicam完善、文本剪辑、光流、AAF/EDL及复杂交换保真 | 有registry能力与明确降级；可选依赖失败不影响基础页面；第三方实机打开/回导与GPU资源生命周期通过 |

负责人/目标版本：待分配。每批以独立可审查command+UI闭环提交，避免先大改渲染；遵循先写本次失败用例、最小领域修复、Review、focused测试、真实窗口复测的仓库工作流。资源释放与取消路径是修复验收，不要求本次直接修改渲染核心。

## 7. 尚未完成的全面验收门禁

这些项目已做源码/现有测试核查，但本次没有足够外部条件或覆盖量；它们是后续验收任务，不是声称相应功能缺失，也不是通过结论。

| Gate | 当前限制 | 具体补测 |
|---|---|---|
| G01 Source有效像素 | fatal阻断；excepthook只让其他测试继续 | 修INT-14后CPU/GPU首帧/播放/seek/resize/flip；图像/音频无纹理/离线、取消/关闭 |
| G02 全素材解码与性能 | 实测3个25fps 1080p素材；未跑全库档位与长期压力 | 4K H264/H265/ProRes/DNxHR/TOPOS、alpha/HDR、23.976/29.97/60/VFR；10/60分钟A/V漂移、连续seek与跨页面；GPU/CPU telemetry不只playhead |
| G03 全工具组合 | 所有主要工具有G或Q/M覆盖；没有遍历所有修饰键/焦点/选区/轨型组合 | moved/trim/ripple/roll/slip/slide/rate stretch×linked/multi/locked/sync×fps/reverse/fade/transition/key/marker/subtitle；取消与窗口失焦 |
| G04 音频设备 | 无人工监听、录音硬件和外部监控验收 | ADR/多声道/Bus/Send/solo/automation/FX/loop/scrub；低延迟与长时同步；实际耳听/录制，比PCM黄金样本 |
| G05 多机位/嵌套 | 有模型bug与focusedtests，未真实多路长素材切角/嵌套完整工作流 | 同步、1–9记录、组编辑/重同步、3层nest含字幕/变速/FX/音频及保存/导出 |
| G06 字幕/动画/效果 | 检查UI服务和tests；本次没有手点每一个效果/字幕格式/曲线操作 | captions各格式/语言/font/graphics；effect Catalog全参数、RAW/调色/合成/生成器跨页；preview与burn/sidecar/mux一致 |
| G07 AI/语音/翻译/生成 | 隔离配置没有profile和所需本地模型，未请求付费外部服务 | 有效配置下正常/超时/取消/stale revision/失败恢复/审批diff；ASR词级证据、TTS字幕几何、模型缺失提示 |
| G08 全交付/互通 | 两个真实短H264服务导出；没有逐一操作Deliver UI、全部格式、第三方NLE | Deliver queue/cancel/retry/多音轨/Stem/字幕/章节、HDR/alpha、individual clips、FCPXML/OTIO/AAF/EDL外部软件回导 |
| G09 保存故障与跨平台 | 两sequence正常save/load一致；没有真实磁盘满/断电/Windows/Linux测试 | autosave与recovery、损坏ZIP/权限/多save竞态、unknown payload、DPR多屏/系统触控板/Windows GPU、关闭资源释放 |

## 8. 主窗口新增的方案细节

<a id="app-01"></a>
### APP-01：手动编辑必须推进revision

定位：TimelineView._execute_command（timeline_view.py:5324）直接调用CommandStack.execute（commands.py:3839），该栈只apply/push，不bump；EditController._on_edit_timeline_changed（edit_controller.py:5580）写sequence/cache/ref，仍保留revision。真实记录从drop、trim、blade、roll到新序列源编辑、marker等，project/timeline revision均0。EditProjectService已有合法的无UI提交边界（ai_editing/edit_project_service.py:168），不能再建一套竞争入口。

1. 在controller/feature host引入command commit adapter，接收stack的proposed immutable timeline和完整updated-command，不二次apply。统一domain validate、revision+ref/cache/publish；stack只有成功commit后push。
2. undo/redo把内容快照恢复但revision继续增，不回旧revision；macro只提交一次。拒绝/no-op不增。项目与sequence修改拆清范围，cache性变化是否增内容revision有显式政策。
3. UI/AI/script/属性/字幕/工程结构都用相同adapter；信号中携带canonical new snapshot，renderer、audio graph、autosave与context bundle更新一致。
4. 验收：base_revision=0的AI plan在用户手动trim后必须conflict；存取重载有新revision；1个macro只增1，Undo再增1；不存在同内容多次bump或绕信号旧缓存。

<a id="app-02"></a>
### APP-02：局部Blade与linked selection的契约

定位：timeline_view.py:5199–5246明确注释局部Blade只切被点轨，Shift才全轨；SplitClipCommand保留左clip旧linked_clip_id，右段不链接。本次linked selection开启、V/A301帧，点击约150只得V149+152，A仍301；后续锁轨切分进一步V60/78/163而左仍链接完整音频。此处不能把“局部只切单轨”直接当架构误实现，但当前链关系会误导后续编辑。

1. 产品层明确：局部Blade默认作用clicked editing unit（linked selection开时切partner），或明确只单轨并安全解链。Shift-all不得重复切partner；关闭linked selection保留独立编辑。
2. 领域slice planner生成两组左右双向linked IDs，并分别按source/FPS映射；若采用独立切分，断开原伙伴的双向引用或引入显式group/range link，不允许旧1:1link静默指向跨两段长片。
3. 伙伴锁定/素材范围不足时拒绝整组或提供明确“只切此片并解链”操作；IDs与转场/marker快照纳入Undo。
4. 验收：B/Shift-B/source insert/overwrite与不同fps/speed，后续只移动/裁某一组影响对应A/V；保存重载无错误链。

<a id="app-03"></a>
### APP-03：默认可操作布局与语言

真实窗口从初始最大化恢复1440×1000后，时间线dock为102px，工具条/尺占去大半，V/A区域难以浏览；调整dock后可正常操作。这是本配置启动/缩窗的体验结果，不据此断言所有用户保存布局都错误。

1. DockLayoutController在新布局/首次实际窗口geometry稳定后按可用高度分配时间线，例如至少容纳工具条+尺+一个可辨V轨和A轨；紧凑屏可折叠AI侧栏或提供明确timeline-focus preset。
2. 保护用户主动调整；只对未设置/明显无效的布局进行一次迁移，取消不改真实偏好；改变窗口大小时保留合理比例与最小轨区。
3. 显示统一ActionRegistry当前快捷键；Source/Program/Properties/AudioMixer/Caption等英文未本地化项走i18n。
4. 验收三种常见分辨率、DPR与保存/重开，轨道不会隐藏到必须先修布局才能编辑；打开Inspector便于参数编辑，AI未配置时不挤占核心工作区域。

## 9. 核心命令详细分项与领域实现

#### 已复现的问题与优化计划

<a id="c01"></a>
#### C01 · P1 · 向左 Roll 后无法撤销

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:13082`（`RollTrimCommand.undo`）。
- 触发：A=[0,100)、B=[100,200)，Roll -10 → A=[0,90)、B=[90,200)；撤销先把 A 恢复到100，撞上尚在90的B。
- 本次实测：`ValueError: Updated clip overlaps with b`。CommandStack 已回填 undo 栈，命令仍在但无法成功撤销；更早历史被阻塞。
- 实施：用保存的左右 clip 快照建立 replacement map，一次 `track._rebuild(clips=...)` 原子恢复，并将 seam 转场与 clip-scoped marker 一并恢复；避免通过顺序调整掩盖其他组合问题。apply/undo 都保存确定的 before/after 事务载荷。
- 验收：Roll ±1/±10，贴接/有间隙、speed 0.5/2、带fade/转场、三次 undo/redo；比较完整 `to_dict()`，不能只比较实例ID。撤销不得抛错。

<a id="c02"></a>
#### C02 · P1 · 在现有片段中间 Insert 后撤销留下洞与残片

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:12393`（affected_clip_ids 只包括起点>=playhead）、`:12458`（undo）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1311`（已增加跨播放头切分但未补 undo 快照）。
- 触发：原A=[0,100)，在40插入10帧；apply A-left=[0,40)、new=[40,50)、A-right=[50,110)。
- 本次实测：undo变为 A-left=[0,40)、A-right=[50,110)，不是原单一A=[0,100)；40–50洞永久保留。
- 实施：Insert 命令保存目标轨完整 before/after（或覆盖所有切分/移位/转场/标记/链接的差集）；生成的左右身份只生成一次，redo复用。undo删除created IDs，恢复原跨点clip，原子还原followers。source/FPS映射调用统一slice服务。补 linked partner 切分策略，防止左右残片仍指向同一音频。
- 验收：播放头在头/尾/中间、不同素材、混合fps/倍速/倒放、已有下游clip/转场/marker；完整恢复clip身份与时间范围，redo稳定不多生残片。

<a id="c03"></a>
#### C03 · P1 · 多个不连续片段批量波纹删除时跨轨不同步，撤销仍漂移

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:11251`（逐原始 span 处理）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:668`（`restore_sync_lock_shifts` 用最后old_start覆盖第一次快照）。
- 触发1：删除V[0,10)、V[20,30)，另一同步锁音轨clip起点30。第一事件将30→20，第二仍拿原始阈值30判断，跳过该clip。
- 本次实测1：音频30→20；正确累计位移为30→10。
- 触发2：同上，音频clip起点50。apply两次移动50→40→30，记录两条 old_start(50、40)；undo按dict最后值回填。
- 本次实测2：50→30→undo40；应恢复50。
- 实施：先合并所有原始删除跨度，在**原坐标系**为每个clip求累计delta，一次提交；一条clip只存一份最初before+最终after。若保留事件循环，阈值必须扣减此前已删时长，undo反序应用每条snapshot而非最后值覆盖。跨轨冲突在提交前统一校验，不独立静默clamp。
- 验收：两个/三个离散跨度、首个后方/第二跨度结尾/所有跨度之后、linked A/V多选、轨道锁/同步锁组合；每条轨累计平移正确，一步undo完全回原位置。

<a id="c04"></a>
#### C04 · P1 · 有FPS元数据时倒放被忽略

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:415`–`:427`。
- 触发：SpeedParams(speed=1, reverse=True) 且 `_source_fps_num`、`_timeline_fps_num` 有值。`effective_speed()`永远为正，FrameMapRequest.reverse却取`spd < 0`。
- 本次实测：source=[100,199]，无元数据前3帧[199,198,197]，有24/24元数据变[100,101,102]。
- 实施：reverse从SpeedParams.reverse显式读取，数值speed路径保留兼容；mapper不从正速度猜方向。source_to_timeline逆映射同步通过相同有理数/FPS/反向逻辑。
- 验收：24/24、30/24、24000/1001、VFR PTS、reverse 0.5/1/2；播放/擦洗/导出同帧索引；序列化重载后不变。

<a id="c05"></a>
#### C05 · P1 · Blade切分倒放和变量速度片段会改变未编辑内容

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2195`–`:2234`；纯ops仍在 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:155` 使用`duration*effective_speed`。
- 倒放本次实测：原source100–199倒放前3帧[199,198,197]；在40切分后变[158,157,156]，切点附近跳到199，尾部连续重复159。原因：按正向source_start + left_source_duration分配两个区间。
- 变量速度本次实测：source offset50后2x、总timeline75；在60切分，原60..64=[170,172,174,176,178]，切后=[170,171,172,173,174]。右段继承原offset50速度点和base1，没有将局部起点/新base重定位。
- 实施：新增direction-aware `slice_clip_at_timeline_range` 领域服务：对原source播放方向切分，反向左段取高端source、右段取低端source；speed_points按播放方向切成两组，右段base取切点所在segment速度，保留point easing并减去其source offset；普通FPS通过Fraction/PTS统一边界换算。所有blade、insert、overwrite、range-lift/extract、freeze都复用该服务。
- 验收：操作前后除片段身份外，逐帧source索引序列完全相同；含0.5x、2x、倒放、混合FPS、分段/斜坡、极短source/冻结帧，且undo/redo完整恢复。

<a id="c06"></a>
#### C06 · P1 · 普通拖边裁剪把时间线帧数当素材帧数

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_items.py:5513`、`:5529`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:8294`、`:8475`，preview还有`:8239`。
- 触发：任何非1x或素材FPS≠sequence FPS；ClipItem松手直接source += timeline_delta；snap handler再次强制同公式，甚至覆盖调用方给出的正确source范围。
- 本次实测：实际 `TimelineView._on_trim_right_finished`，原timeline_duration100/source_duration200/speed2，新timeline80、输入正确source160，在snap开启且未吸附时也变成source180。
- 实施：Item只上报timeline目标边界，不自行推source。View调用纯领域slice/trim service得到before/after范围，preview和commit复用同一结果；linked两个clip分别按各自timebase求source变化，不能复制同source delta。snap只改变目标timeline边界再重新求映射。
- 验收：0.5/1/2x、反向、30→24fps、snap开/关/Shift暂禁、左右缩短/延长、linked AV；预览/松手/重载/export相同source帧；本例必须source160。

<a id="c07"></a>
#### C07 · P1 · 混合帧率素材改速后的时长计算错误

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:16249`–`:16262`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:590`；insert/overwrite纯ops的类似formula位于`:1290`和`:1404`（同为source_duration/speed）。
- 本次实测：source30fps120帧，sequence24fps，原timeline96帧；ChangeClipSpeed 2x后timeline60帧，应48帧。尾部取源帧被mapper钳制，产生保持尾帧或时长漂移。
- 实施：统一duration mapper：source_frames / source_fps / speed * timeline_fps；常数速度用Fraction决定统一取整政策，变量速度先积分source各segment的秒时长再转timeline ticks；source metadata缺失时明确legacy1:1处理并记录诊断。rate-stretch应由原实际秒时长求新speed，不直接source_frames/new_timeline_frames。
- 验收：24/25/30/60/30000/1001与各sequencefps，0.1–8x、变量速度、linked不同fps素材；总秒时长及最后有效source帧准确，preview/export/reload一致。

<a id="c08"></a>
#### C08 · P1 · 波纹裁剪不处理链接音视频，破坏A/V同步

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:9162`、`:9231`（只创建主clip命令）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:13251`（却把linked所在轨排除sync跟随）。
- 本次实测：真实UI `_on_ripple_trim_right_finished`（FakeView真实CommandStack），视频/音频双向链接，原各100帧；视频缩到80，音频仍100，链接ID继续有效。
- 实施：领域层构建一个A/V editing-unit事务，先校验双方及双方followers的素材余量、track lock/重叠；生成两条RippleTrim且第二条不重复sync-follow，或一个LinkedRippleTrimCommand。拒绝任一边时整组回滚；UI preview也按同结果更新两边与followers。
- 验收：linked selection开/关语义明确，左右trim、左右extension、锁linked track、被阻挡linked follower、不同sourcefps/speed；双边范围/时长保持同步，一步undo，两轨都收到refresh。

<a id="c09"></a>
#### C09 · P1 · 主轨可编辑时速度命令会直接修改已锁链接轨

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:16261`–`:16287`；UI入口 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:10491`；普通linked trim验证也只检查overlap而不检查伙伴track.is_locked。
- 本次实测：视频轨未锁，链接音轨`is_locked=True`；ChangeClipSpeed 2x后锁定音clip duration100→50。
- 实施：统一command/service前置校验 `assert_editable_editing_unit`，对所有将受影响的clip和目标track校验锁状态，主轨/伙伴/邻居/同步跟随策略一致；track-lock只由专门lock command改变，undo允许恢复合法历史而不是被现锁挡死。所有UI动作只依领域异常更新状态提示并恢复preview。
- 验收：移动、trim、speed、unlink、freeze、ripple、overwrite从未锁主clip发起时不得绕过锁伙伴；失败不增revision、不改undo、不发部分timeline。

<a id="c10"></a>
#### C10 · P1 · 带淡出片段切分或裁剪到更短时抛错

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2218`（左半只clamp fade-in，漏fade-out）；`:1697`–`:1717`（Trim用with_trim，不经copy_clip_with）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:547`（直接replace）。
- 本次实测：100帧clip淡出40，blade20或right trim到20均`ValueError: fade_out_duration (40) exceeds clip duration (20)`。
- 实施：明确fade的slice语义：blade左段不继承原尾部fade-out（或按原可见包络切片），右段继承原尾fade并clamp；trim按保留内容求fade envelope，所有affected属性快照在命令中保留，undo恢复原fade数值。Speed/retime shortening也复用相同规范，不依赖模型抛异常后失败。
- 验收：fade-in/out单独与相交组合，blade近头/尾、左右trim、speed升高、slide邻居缩短；操作成功，undo完整恢复fade；音/视频包络符合既定策略。

<a id="c11"></a>
#### C11 · P1 · Split/Delete及撤销丢失已有转场

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:2478`（with_clip_removed删除相关转场）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2241`（blade临时移除原clip）、`:2260`（只保存original_clip）；`:359`（Delete.undo只有clip快照），Batch/Ripple同类。
- 本次实测：A头部10帧单边淡入转场，blade50：转场数1→0→undo0；Delete：1→0→undo0。工程有效效果在可撤销编辑之后仍永久丢失。
- 实施：clip变更事务快照包括referencing transitions；slice时头转场迁移左段、尾转场迁移右段，双边seam依据两侧存活范围调整/拒绝/删除并记录；undo原子恢复clip+transition完整快照；transition IDs/params保持稳定。copy/delete/overwrite/range同服务。
- 验收：单边头尾、双边视频及音频转场，blade/普通delete/ripple/batch/overwrite/跨轨move/undo/redo；完整参数和转场身份恢复，save/reload仍存在。

<a id="c12"></a>
#### C12 · P1 · 移动与波纹跟随后的转场中心不随clip/seam移动

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:491`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:2482`（with_clip_updated不修转场anchors）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:657`（sync rebuild也只改clips）。
- 本次实测：A带head淡入center0，A移动到200，trans.start仍0。`validate_timeline`返回“not anchored to clip start”和“range exceeds clip”。这类无效时间状态仍可保存/重载。
- 实施：为transition定义以clip edge/seam为锚的语义坐标，或所有时间变换事务对受影响transition重求center；两个clip分离时有明确删除/转换/拒绝策略；command提交前统一timeline invariant检查。undo恢复before而不是反算。
- 验收：单边头尾跟随移动；整组双边转场移动、roll seam变更、ripple followers、跨轨移动、重载；validate_timeline零错误，预览与导出同帧转场。

<a id="c13"></a>
#### C13 · P2 · clip移动后关键帧留在旧绝对时间

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:491`；模型采样 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:770`；渲染 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_render_plan.py:889`。
- 本次实测：A opacity关键帧timeline0=0、99=1，A起点0→200，clip新起帧opacity从0变1；动画整体消失在片段前方。
- 实施：明确clip参数和effect参数统一clip-local time，schema加动画timebase版本并迁移旧absolute关键帧；或当前架构对所有move/ripple/slice同步平移absolute clip/effect关键帧。提供受试验保护的legacy读取，避免重开工程二次迁移。
- 验收：opacity、transform、clip effects的关键帧，move/跨轨/多选/ripple/duplicate/trim/split/retime；操作后原local-frame参数曲线保持预期，undo和save/reload准确。

<a id="c14"></a>
#### C14 · P1 · Slide遇到已有间隙或淡出时不可逆

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:2029`、`:2060`（把邻居边缘贴到目标，不保留原gap）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:11033`（undo只slide(-delta)，虽存snapshot却未使用）。
- 本次实测1：原A=(0,90)、B=(100,90)、C=(200,100)；slide B+5后A=(0,105)、B=(105,90)、C=(195,105)；undo A=(0,100)、B=(100,90)、C=(190,110)，两个原10帧gap丢失。
- 本次实测2：紧邻A100帧且淡出80，slide B-50后A淡出clamp到50；undo A时长回100，淡出仍50，应80。
- 实施：Slide命令存完整primary/prev/next before快照（包含fade、effects、keys、marker、transition），undo原子恢复；明确有gap时slide是保留原gap、将gap视作可编辑空片，还是要求连续三片并禁用；不能逆向函数代替undo。preview和commit同语义，并校验邻居source handles。
- 验收：左右/仅单邻居/三连续/有gap、带fade/变速/倒放/transition；`undo(apply(timeline)).to_dict()==before`；持续三次redo稳定。

<a id="c15"></a>
#### C15 · P2 · blade后右半片段上的clip-scoped marker仍属于左clip

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2245`（没有marker重新归属）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:151`（marker带clip_id+absolute frame）。
- 本次实测：A=[0,100)、marker frame75/clip_idA；blade50后marker75仍指向timeline_end50的左A。后续单独移动左A时marker错误跟随左A。
- 实施：slice事务按marker的source/clip-local位置迁移clip_id，frame保持content位置；原marker身份稳定；cut点marker规则（left/right）明确。Split命令保存before marker载荷，undo恢复归属；trim删去内容的marker明确隐藏/保留/清理策略。
- 验收：marker在切点前/上/后、clip move/linked split/range delete/重载；每个clip marker在所属范围，跟随正确半段。

<a id="c16"></a>
#### C16 · P2 · 跨轨Sync Lock平移clip却不平移其scoped marker

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:650`–`:661`，restore同缺marker；范围/普通本轨move有 `_move_scoped_markers_with_clip`，跨轨路径没有。
- 本次实测：删除V[0,10)，另一同步锁A clip起点50、scoped marker55；clip→40，marker仍55（原相对5帧变相对15帧，已越过10帧clip）。
- 实施：把clip、scoped markers、动画、单边转场作为同一 temporal-transform 载荷；跟随位移保存最初完整before/最终after，撤销同时恢复。
- 验收：跨轨insert/ripple trim/delete/batch delete、多个跨度、track locks，marker相对clip位置稳定；undo/redo/save/reload一致。

#### 静态确认、还需真实素材交互复测的风险

<a id="c17"></a>
#### C17 · P1 · 普通右裁剪的source上界校验漏掉source_start

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:8482`、`:8559`，仅比较new_source_duration<=max_source；ClipItem类似边界使用需统一；`TrimClipCommand`与model没有总source长度可校验。
- 触发条件（静态，不计作本次GUI实测）：总source200、source_start100、原duration100，右延长到150；校验150<=200放行，实际exclusive source_end250超素材200。
- 实施：source bounds从MediaAsset/PTS和领域服务取得，统一校验 `source_start + source_duration <= available_source_frames`；逆向/混合fps/变量速度先求真实source区间。对无元数据素材明确未知handles并限制延长，不用巨大默认值。所有入口均由service校验，UI只preview。
- 验收：在真实素材中间in点剪辑、普通/ripple/roll/slide/rate stretch向末端延长；边界最后可用帧能精确达到，超一帧不能提交，日志/状态栏给出可用余量。

#### 其他风险（建议追踪，不作为已确认用户bug统计）

- 多数字段虽然数据类 frozen，但animated_params/params等嵌套容器仍可变；要审核调用方是否原地修改，避免旧快照泄漏。这个审计没有证明现实调用方已发生，所以应列为防护任务。
- Clip/track/timeline from_dict 对主block未知字段不普遍保留；caption_graphics已有extra机制，clip和sequence主block没有。后向加载未来schema并保存可能丢新字段。需用schema version与extensions解决，不能无条件接受所有类型。这里只做结构确认，无未来版本真实项目可测。
- validate_timeline 对transition检查未要求双边center等于当前真实seam，对clip-scoped marker范围、linked AV时长/起点一致也不检查；应补领域不变量及报告机制，而不只修UI显示。

#### 缺失的领域能力（可写入新计划，避免与已存在功能混淆）

1. **统一时间变换事务服务（P1，约4–7日，依现有实现拆分）**：当前move/trim/split/ripple/slide各自修改clip，marker/transition/animation/retime控制曲线未一致随动。新增editing-unit+time-transform pure planner，输入原timeline、操作、asset metadata，输出完整before/after/delta/诊断；通过一个Macro或compound command提交，revision只增一次。先实现slice/move/follower-shift，接着迁移普通trim/roll/slide/insert/overwrite/range。采用完整serialized快照/结构化diff验收，不依ID equality。
2. **跨越编辑点的Sync Lock与字幕联动策略（P2，3–5日）**：现shift_sync_lock_followers仅处理video/audio中start>=point的整clip，subtitle track明确排除；跨point长片与字幕不会被切分/跟随，负delta在各轨独立clamp造成不同累计位移。增加Sequence Sync Scope（选定轨/所有同步锁轨、字幕跟随可选），对跨pointclip先slice、字幕cue/word timing按range调整；输出受影响轨和冲突预览，默认整事务拒绝或明确冲突解决，不静默desync。测试长音床、跨点字幕、多个波纹跨度、锁轨。
3. **可用素材handles的统一服务与UI提示（P2，2–3日）**：roll command/ripple right/slide邻居目前可不传max_source，normal trim校验在item层不完整。新增SourceAvailability查询（视频帧/图像无限或有限/生成器/composite/virtual MG的不同策略），支持FPS/PTS；trim monitor显示剩余head/tail并在达到边界时阻尼停止。任何command入口（UI/AI/API）都必须验证。包括缺失素材/未知duration的可编辑政策。
4. **完整编辑事务回归矩阵（P1，2–4日，可并行）**：已存在133项focused tests，但很多组合没有覆盖；新增参数化矩阵：speed×fps×direction×fades×transitions×markers×linked/lock×operation，最低先覆盖上面C01–C16；模型层property检查slice后逐帧source映射不变、undo/apply全量to_dict不变，UI release handler与模型planner语义一致；preview/export/source索引端到端小素材验收。需避免将只检查方法存在的test当行为验收。

#### 推荐执行依赖

- 第一批P1：C01/C02/C03（撤销与波纹数据完整性）→ C04/C05/C06/C07（source/timebase）→ C08/C09（编辑单元/lock）→ C10/C11/C12/C14（内容与附属状态）。
- 第二批P2：C13/C15/C16及统一transaction service、cross-point/subtitle同步、source handles UI。
- 每批先加失败用例再实现最小领域修复，避免直接复制逻辑到UI；command revision、undo、预览、保存共用同一新模型。
- 交互验收必须补在真正app窗口中，由主审计执行；本报告仅将独立代码复现与真实GUI手测分开标注，不声称所有功能已完整交互覆盖。

## 10. 交互详细分项与功能实现

#### 已验证问题清单

| ID | 优先级 | 功能/问题 | 证据 | 触发/结果 |
|---|---|---|---|---|
| INT-01 | P1 | 波纹边缘单击错误触发修剪完成 | QTest 真实 press/release（offscreen），probe1 | clip [100,200)，R 工具，仅左边缘单击发 old_start=100,new_start=0；右边缘单击发 old_end=200,new_end=0。完全无拖动也进入命令路径。 |
| INT-02 | P1 | Esc 无法取消媒体片段拖动/修剪 | QTest，probe1 | clip start100 拖到130；事件状态仍 IDLE；按Esc后 preview130/_is_dragging=true；release后model130，新增undo。 |
| INT-03 | P1 | H 手型工具不平移，反而框选或移动片段 | QTest，probe3 | 空白左拖进入 SELECTING，scroll50→50；片段体左拖使片段start100→70，scroll仍50；普通中键空白拖也无效。 |
| INT-04 | P1 | 刀片工具绕过轨道锁，右键也执行切割 | QTest，probe3 | 锁轨后B左击将1片段分成2片段；B右击同样切割，50+50并进undo。 |
| INT-05 | P1 | 锁轨片段菜单仍可变速/启用禁用/链接 | 自动集成验证，QMenu.exec 替身选真实 QAction + 真实命令栈，probe2 | 锁轨Disable Clip enabled=true且实际model.is_enabled=false；200% enabled=true且duration100→50/speed2。链接路径静态同类风险，尚未在菜单集成验证。 |
| INT-06 | P2 | 触控板水平滚动无效，Ctrl水平滚动意外缩小 | QWheelEvent，probe1 | angleDelta=(120,0)/pixelDelta=(40,0)：scroll80→80；Ctrl同事件zoom3→2.5。 |
| INT-07 | P2 | 清空自定义快捷键后旧键仍生效 | 实例化ShortcutManager+QKeyEvent，probe2 | 显式blade binding=''，matches_key_event(eventB,defaultB)=true，view仍变BLADE。 |
| INT-08 | P2 | 禁用片段菜单状态文案错误 | 自动集成菜单检查，probe2 | model.is_enabled=false，实际菜单仍显示Disable Clip；动作实际反向启用。 |
| INT-09 | P1 | Shift框选重复对象污染selection | QTest，probe4 | 先选c，再Shift框选c，selected_clip_ids=['c','c']，is_multi=true。后续批量复制/删除/属性命令可能重复处理同一对象。 |
| INT-10 | P2 | 字幕不参与Fit Selection和上下边界导航 | 调真实方法/Qt模型，probe4 | subtitle[100,200)唯一选中后zoom3→3；playhead50执行next边界到239，忽略cue开始100。 |
| INT-11 | P2 | 删除含字幕轨道不提示已有内容 | QMessageBox.question替身记录+真实DeleteTrackCommand，probe4 | 字幕轨有1cue，confirmation_calls=[]，轨直接删除并入undo；A/V轨有clip有确认。 |
| INT-12 | P1 | 2x变速片段的鼠标Trim错误换算源帧 | QTest，probe5 | timeline[100,200),source[100,300),speed2。左裁10 timeline帧后source_start110/source_duration190，应120/180；右裁10后source_duration190，应180。 |
| INT-13 | P2 | 属性面板多选退化为第一片段，无混合值提示 | 真实EditPropertiesPanel模型调用，probe2 | set_clips([c,d])后_current_clips只有[c]；Transform reset依赖该list，批量重置也只发c。属于实现缺失/误导交互，不是已支持批量编辑中的一般小bug。 |

#### 每项详细实施方案及验收

<a id="int-01"></a>
#### INT-01 波纹单击修剪（主任务升级为P0：可见窗口出现未捕获槽异常）

定位：`src/features/edit/ui/timeline_items.py:819-820` 构造时将 `_final_new_start/_final_new_end` 设0；`:4488` press保存原值时未初始化final；`:5580-5607` release仅比较final与original，没有was_dragging/threshold条件。

复现：建立源余量足够且timeline_start非0的clip；R后边缘按下马上松开。首个左单击可能使clip向0延长；右单击把end0发给命令，存在invalid command异常路径。之后普通边缘单击还可复用上次drag的stale final。

实施：每次press将final_start设为当前start，final_end设为当前end；release仅在was_dragging且有效preview与original有差异时发finished。最短时长/源范围在domain builder中验证，不依赖QGraphicsItem值。无变化release清理preview/trim cache，不推命令；失败恢复主/linked/follower geometry并显示诊断。

验收：左右边缘单击/移动阈值内/拖回原位无命令、无revision变更；完成一次drag后再单击无旧值重放；invalid拖动不会抛出未捕获Qt槽异常；单次有效操作只有一个undo。

<a id="int-02"></a>
#### INT-02 取消操作的状态机没有接上片段事件

定位：`timeline_view.py:4416` Esc仅非IDLE才cancel；`:3897`换工具同样检查；`:4493-4577`所有begin只处理区域交互；媒体item press/move未begin。`:9457-9516`现有cancel覆盖不全：不重置pending/fades/roll/subtitle copy ghosts，fallback依赖primary_clip_id但未设置。

实施：ClipItem/SubtitleClipItem/TransitionItem/RollHandle统一发interaction_started、preview、finished、cancelled信号；在view的InteractionSession保存完整immutable snapshot和所有affected IDs（主、linked、multi、ripple/sync-lock followers、scoped markers）。Esc/工具切换/失焦/窗口关闭都走单一cancel。cancel先取消item grab与pending状态，再从模型恢复所有preview，隐藏snap/ghost/trim monitor，恢复cache和cursor。release遇到已取消session必须无提交。不要依靠调用_private字段的散落恢复逻辑。

验收：Move、Alt-copy、trim左右、ripple、roll、slip、slide、fade、subtitle/transition拖动分别按Esc；scene=immutable model，undo长度不变；之后释放鼠标不提交；A/B/T换工具、失焦后等价恢复。

<a id="int-03"></a>
#### INT-03 手型/中键平移

定位：`timeline_view.py:3903-3904`设ScrollHandDrag，但`:4576-4584`空白左按总先框选；clip按下给item，`:4465+` item无HAND防守；全文件无MiddleButton处理。

实施：在全局press路由中先处理HAND+左键、中键临时平移，避免进入clip/edit handlers。记录viewport pointer与scroll origin；move按像素delta更新horizontal scroll，垂直按照光标所在V/A/字幕区域独立bar；release统一恢复cursor。焦点窗口变化解除pan；header按钮、ruler scrub等允许明确优先级。HAND不应改变selection或模型。可复用QGraphicsView pan但必须拦截item编辑路由。

验收：空白/clip body/字幕/转场上HAND拖动都只平移；临时中键任意编辑工具平移后保持工具；锁轨一样可浏览；scroll有限制无越界；撤销栈不变。

<a id="int-04"></a>
#### INT-04 刀片按键与锁轨

定位：`timeline_view.py:4534-4542`按active_tool调用split而无左键判断；`:5199-5246`未检track.is_locked；Shift全轨路径`:5278`已滤lock，局部路径却没滤。domain SplitClipCommand apply也无通用锁守卫。

实施：仅左按钮消费blade；右按钮保留上下文菜单、middle路由平移。所有局部split从同一service检查editable，包括轨道锁、媒体边界、字幕/嵌套/生成器适用性。track resolution失败应诊断或不操作，不能fallback误切别轨。核心命令另加统一锁检查，防菜单/快捷键绕过。

验收：锁轨局部split无变化；未锁正常split精确frame；B右击菜单无模型改变；Shift-B/Shift-click split-all尊重lock；undo恢复links/transitions/markers。

<a id="int-05"></a>
#### INT-05 锁轨模型防线缺失

定位：`timeline_items.py:4910-4936`enable/link菜单均不disable；`:4964-4980`speed菜单不disable；`timeline_view.py:10377-10391`toggle、`:10450-10489`links、`:10491-10495`speed无锁；core `commands.py:15964`Toggle、`:16049`SetLinked、`:16206+`Speed apply也不拒绝锁轨。

实施：统一editability resolver返回allowed/reason/affected_track_ids，UI menu/panel/canvas据此setEnabled+tooltip；命令apply对所有直接/linked/重链接旧partner/同步跟随目标实行同一轨锁策略。不把lock检查只放view，否则属性、AI、脚本等仍可绕过。拒绝时整个macro无revision/undo，保留原状态并给可理解的提示。

验收：锁轨Move/Trim/Blade/Enable/Speed/Link/Unlink/Fade/Effect/Crop/Transform所有入口一致；主未锁partner锁时不暗改partner；unlock后工作；菜单灰显与命令拒绝一致。范围由核心审查agent继续补齐。

<a id="int-06"></a>
#### INT-06 滚动/缩放

定位：`timeline_view.py:2743-2762`只用angleDelta.y，zoom分支0走zoom_out；未用pixelDelta/x轴；`:2320-2444`set_zoom无viewport pointer anchor。

实施：输入归一化先使用pixelDelta，回退angleDelta/120；无Ctrl水平x优先，垂直y映射timeline水平符合现有习惯；Shift按所在区域垂直；Ctrl只对非零有效zoom delta处理，小delta指数平滑。保存mouse下timeline frame，set_zoom后调scroll使该帧仍在同一屏幕x；zero delta/start/end phase只accept无变化；考虑系统natural-scroll/inverted语义一次，不重复反转。避免增大显示率时每tick重建所有clip。wheel超界留父容器或accept按明确约定。

验收：Mac触控板横/竖/惯性、小delta；鼠标滚轮；Ctrl+zero不zoom；anchor frame误差≤1frame；无意外跳屏；Shift只滚对应stack；125%/200%DPR同效。

<a id="int-07"></a>
#### INT-07 空快捷键与可定制范围

定位：`timeline_shortcuts.py:108-117`if not binding退default，无区分“不注册”和“注册但显式清空”；`timeline_view.py:4379+`标记与IO仍有hard-coded fallbacks；`timeline_shortcuts.py:62`N注册为Enable Next Clip，真实handler`:timeline_view.py:300`切Snap。

实施：manager.get_action不存在才用fallback，action存在且binding空直接False；只允许完全key+modifier匹配，不把QKeySequence多步chord每步都当独立shortcut；对M/I/O等移除不必要hard-coded回退，统一action scope，保留未初始化manager场景明确default。Naction label/id迁移为Toggle Snap（兼容旧custom key映射）。公开可定制键应与真正消费一致。

验收：清空/恢复默认/改键/冲突/启动时导入preset，旧键均按预期；未分配键不误触；UI显示N用于Snap；两段chord不会只按任一步就触发。

<a id="int-08"></a>
#### INT-08 禁用菜单文案

定位：`timeline_items.py:4910`读未存在的_clip_enabled退True；模型update使用_is_enabled。

实施：使用唯一`is_enabled`状态或公开getter，menu checked/text由当前model/item刷新；enable/disable action与keyboard D共用command。锁轨禁用动作按INT-05灰显。

验收：启用显示Disable、禁用显示Enable；操作后重新菜单立即正确；undo/redo更新；选中多片段用明确Enable Selected/Disable Selected或显示混合状态。

<a id="int-09"></a>
#### INT-09 选中去重

定位：`timeline_view.py:5166`Shift框选采用list(existing)+list(new)；`selection.py:116`selected_clip_ids保留duplicates。

实施：TimelineSelection建立identity `(item_type,item_id,timeline_id)`去重不变量，所有factory/merge/from_dict统一；Shift为稳定union、Ctrl为对称差；primary必须存在于items；box新对象排序按track/time。命令builder再防重复IDs，避免恢复不一致。selection indicator数量按unique对象。

验收：重复Shift框选10次仍唯一c；Shift新增和Ctrl切换符合结果；Ctrl+D重复一个对象只产生一个副本；Delete/Copy/Macro无重复项；跨字幕/clip/transition ID冲突也正确。

<a id="int-10"></a>
#### INT-10 字幕统一对象定位

定位：`timeline_view.py:12923-12931`Fit只get_clip_by_id；`:12630-12694`边界只track.clips。相比`:12823`content end已遍历subtitle_clips，显示实现不一致。

实施：core/facade公开`iter_timeline_objects(types=...)`与`get_time_range_for_selection`，统一媒体、字幕、caption graphic binding去重、转场等。Fit Selection用统一范围；上下键明确“当前目标轨优先/全部可见轨”策略，字幕边界纳入；marker独立导航不混淆。宽度0/最小zoom做健壮处理。

验收：字幕-only/混选/字幕+caption graphics/locked/hidden tracks的fit和navigation定义一致；字幕100-200，50按Down到100、再到200；上下键不跳无关空白。

<a id="int-11"></a>
#### INT-11 轨道删除内容统计

定位：`timeline_view.py:12024`只`len(track.clips)>0`；subtitle_clips/轨转场/绑定未统计。

实施：复用track content summary，对所有可删除内容一致确认；显示媒体/字幕/转场数量与名称；locked track提供解锁后删除的明确流程。删除后caption graphic binding、审阅状态等保留unknown字段/undo由domain command原子恢复。

验收：有cue的轨在取消确认后不变；确认才删除；空轨可免确认；undo恢复cue及关联；A/V/subtitle一致。

<a id="int-12"></a>
#### INT-12 变速鼠标修剪

定位：`timeline_items.py:5510-5537`source_delta=timeline_delta；`timeline_view.py:8064`preview同；`:8290`snap后重新直接加delta；`:8478`right同；`:8490`上限只看source_duration忽略source_start。

实施：统一time-map trim builder：时间线frame delta先映射素材fps/timebase、speed、reverse、speed-points/easing。左右新源区间由域模型计算，联动音频按自身time-map；preview与finished都消费相同builder结果，bounds验证source_start+source_duration≤source_total。不能在item复制速度算式。命令中储完整old/new ranges，undo精准恢复；源区间未知用保守边界/用户提示。

验收：speed 0.5/1/2/8、reverse、ramp、mixed FPS、linked音频；2x左裁10timeline帧对应20source帧；preview和提交一致；trim不改变原有剩余画面/速度；导出与preview同最后一帧；不越源尾。

<a id="int-13"></a>
#### INT-13 多选属性

定位：`edit_properties_panel.py:2049`暂存所有selected clips，`:2061`set_clip(first)又重置list为[clip]（`:1979`）；`:1721`批量reset用被截短list。

实施：Inspector独立SelectionContext保留完整IDs+primary，set_clipUI只构建primary但不覆盖context。每属性compute common/mixed；混合值显示“多个值”；输入绝对值或相对增量（scale/position尤其需明确）；command层为每个editable clip基于自己old dataclass创建新值，Macro一次undo；异构media类型仅显示共有兼容字段，跳过锁对象并解释数量。

验收：两不同translate/opacity片段，显示mixed；用户设50%同时变两clip、旧参数独立保留；Reset同时复位两个；取消/undo恢复；锁轨对象遵循统一策略；无UI rebuild导致失焦或写错对象。

#### 体验摩擦 / 目前缺少的交互能力（需排期）

<a id="ui-gap-01"></a>
<a id="ui-gap-02"></a>
<a id="ui-gap-04"></a>
<a id="ui-gap-06"></a>

| ID | 优先级 | 缺少/不完整的功能 | 当前依据 | 详细实现方向 | 验收 |
|---|---|---|---|---|---|
| UI-GAP-01 | P2 | 拖动到viewport边缘自动滚屏及跨区域自动卷轨 | timeline_view/items中没有edge auto-scroll计时器/边界路由；只存在播放时follow | InteractionSession pointer进入左右24-48px边缘带时启动16ms timer；速度按离边缘距离加速，有上限；按固定scene frame/鼠标offset重算preview，保留snap与目标track；V/A/字幕区域各自上下边缘滚对应bar；所有release/cancel/drop/close停timer。无需触发command。 | 按住片段拖到右边缘可以连续移动到原来不可见区域；linked/multi/Alt-copy也不掉；停止/取消即停；无超原帧/误轨道。 |
| UI-GAP-02 | P2 | A/V选中片段键盘逐帧微移与精确Move对话框 | Alt+Left/Right仅_nudge_selected_subtitle_clips；A/V方向键移playhead，鼠标精确移动依赖zoom/snap | action注册NudgeSelected ±1/±10，作用clip/字幕统一selection，domain builder算组的合法delta/locked/overlap/linked；数值Move弹窗可输入绝对timecode或相对frame；一次Macro保留多轨间相对关系；不会误拖或强制缩放。 | 选中A/V多轨组在10%zoom也能1帧移动；碰撞/0边界明确反馈；1undo；快捷键可改；caption binding同步。 |
| UI-GAP-03 | P2 | Slip/Slide的同步画面反馈和四画面Trim Monitor | _on_clip_slip:8613仅logger且注释“could emit Program Viewer”;现有roll monitor但slip/slide缺少可见源in/out对照 | 复用现有trim preview service，通过queued request/session+generation发滑移前后两端源帧、前后邻片段边界，worker解码缓存；节流30/60Hz合并请求，UI只设图像；显示源In/Out、变化量与可用head/tail；cancel立即恢复；preview禁止改undo模型。 | slip鼠标移动时能确认源镜头变化与两端余量，不仅数字；slide可见前后连接；cache命中流畅、无UI decode、过期帧不覆盖新session。 |
| UI-GAP-04 | P2 | 多选属性编辑/混合值/批量效果参数 | INT-13已验证；只有blend_mode controller特判多选 | 依INT-13构建SelectionContext+mixed widget；效果批量须按catalog identity/version/schema兼容匹配，禁止按index错套；新增Paste Attributes勾选Transform/Crop/Speed/Opacity/Effects/Audio/Color，将asset grade/clip grade区别明确；批量Macro。 | 两片段参数独立old值、一次改全；不同effect schema明确跳过；undo、autosave、保存重开一致。 |
| UI-GAP-05 | P3 | 统一交互说明与快捷键提示 | N登记名称Enable Next Clip而执行Snap；timeline class文档承诺middle pan实际无效；硬编码键多于settings可定制 | ActionRegistry同时供应toolbar tooltip/menu/settings/help；显示当前键而非写死字母；按操作context给状态栏可行动提示（Shift暂时禁吸附、Alt复制、锁轨拒绝原因）；统一中英文I18N，包括裸English菜单。 | 改键后菜单tooltip实时一致；无未支持操作宣称；首次尝试用户可通过现有提示完成移动、修剪、snap、copy。 |
| UI-GAP-06 | P3 | 交互对象和焦点的统一命令上下文 | viewer/timeline都有I/O和zoom单键，JKL为window范围；已有owner window guard只保证其他top-level窗不触发 | 保持已有focus-local I/O机制，建立EditCommandContext(source/timeline/program/text-input/active-modal)，toolbar/action读统一context；输入数值/字幕/搜索期间字符键不shuttle；序列切换/嵌套导航恢复selection/playhead/source范围与undo上下文。应主窗口实测后确定具体遗漏。 | source I/O只改source；timeline只改sequence；字幕键入J/K/L/I/O正常输入；浮动面板/工具窗不偷键；当前快捷键与状态可见。 |

UI-GAP-06 属于待真实窗口验证的改进项，不能据代码称JKL一定偷走文本：Qt输入控件可自行接受ShortcutOverride。

#### 覆盖与局限

本子任务运行现有7组focused测试：

`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_clip_item_trim_preview.py tests/edit/test_timeline_shortcuts_migration.py tests/edit/test_timeline_context_menu_guards.py tests/edit/test_selection_diagnostics.py tests/edit/test_timeline_disable_shortcut.py tests/edit/test_timeline_transport_shortcuts.py tests/edit/test_trim_professional.py`

结果：57 passed（3.75s pytest执行），分项原环境版本声明见原报告；本次主窗口/主任务功能测试实际Qt为6.10.0。说明常规测试覆盖了基础功能，但未覆盖本次找到的鼠标按下/移动/取消/释放真实链、空绑定、锁轨菜单命令等边界。新增回归应使用QTest事件与真实CommandStack，避免只测试helper与MagicMock误认为交互已完整。

未对真实媒体CPU/GPU画面解码、预览/导出一致性、实际声音、网络/AI服务成功失败、多机位切换、系统DPR/多显示器或外部桌面鼠标人工体验作亲测。源/节目监视器的wheel支持pixelDelta和pointer anchor，时间线却没有，形成可验证的输入设备一致性差异。

<a id="int-14"></a>
#### 根 agent 的真实窗口测试新增：Source Viewer 致命崩溃（INT-14，P0）

根 agent 2026-10-13 报告：在真实 cocoa MainWindow 中双击素材打开 Source，`paintGL -> _render_texture -> source_viewer.py:833` 抛 `AttributeError: '_GLWrapper' object has no attribute 'glUniform4f'`，进程退出。真实崩溃日志由根 agent 保存。其后仅在审计harness设置excepthook继续其余交互，生产代码未变，绕过崩溃后的画面测试必须注明该限制。

静态根因确认：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/source_viewer.py:85` 的 `OpenGL.GL` import只有`glUniform1i`；`:108-149` `_GLWrapper`暴露相同函数集；`:833`新加入的静态quad shader显示矩形路径调用`glUniform4f`，`:836`Y-flip又调用`glUniform1f`。两函数均未import、未加入wrapper。`initializeGL:571-578` GLSL已声明并消费`u_display_rect/u_flip_y`，uniform location缓存也有`:595-598`，唯独Python桥接遗漏。只修4f会下一行再次崩溃。

证据补充：对Source/Program AST扫描全部`_gl.xxx`使用与`_GLWrapper` class声明，Source缺`['glUniform1f','glUniform4f']`，Program缺项为空。结果`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-13-edit/evidence/topos_source_gl_wrapper_audit.json`。Source的wrapper固定PyOpenGL，与主渲染后端Metal选择不是同一接口；已在默认Metal环境实际触发，但不能写成仅Metal错误。CPU上传/GPU共享只要有效纹理和shader均会走该共同路径。

详细实现：在OpenGL import和wrapper同步补两个API，或复用一个受约束的GL facade避免人工列表漂移。启动时做wrapper能力契约检查并在renderer能力诊断页暴露错误；保留静态VBO+uniform优化，不回退每帧VBO重传。paintGL失败须终止当前绘制、进入明确错误占位/日志并继续可用操作，不能通过宽泛except继续使用部分GL状态，也不能让未捕获Qt槽异常杀死进程。清理/错误不修改共享texture ownership或thread/slot ack契约。

验收：AST/API契约测试调用到的所有_gl属性存在；mock_GL render_texture验证4f/1f参数，两个uniform location=-1也安全；真实QOpenGLContext Source素材首帧、CPU QImage和GPU共享texture、play/scrub/新素材切换、resize/anchor zoom、纵向flip与清理，各不崩、首帧非黑且方向正确。至少macOS默认Metal+Source OpenGL桥接，以及Windows OpenGL各一次smoke。图片、纯音频（无video texture）和失效texture错误占位另测。此P0应先于所有交互体验优化排期。

## 11. 功能矩阵、状态问题与新增工作流详细实现

#### 当前真实能力矩阵

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

#### 隐藏 Bug 与状态缺陷

<a id="fe-b01"></a>
#### FE-B01｜P1｜修改 FPS 后字幕、动画、标记与自动化时序不保真（已复现）

证据：[现有 FPS remap 方法](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:665>) 只缩放 video/audio clips 的 placement/fades 和 transition、range；未重映射 subtitle_tracks、markers、clip/effect animated_params、track/master automation、caption 图形的时序内容。方法标题声明 retain absolute timing，与结果冲突。

真实执行：24fps→48fps，video 从 frame24→48（仍 1s），subtitle frame24 原样保留（1s→0.5s），marker仍24；clip opacity keyframes24=.2、48=.8 未 remap，新 1s 的值从 .2变成 .8。不能仅用视频/转场测试作为验收。

修复：在 feature service 建立 sequence timebase transform，按 Rational 把所有“序列帧”坐标统一转换，保留源帧坐标不变；重映射字幕/markers/animation/automation/graph bindings 的序列域，合理合并因降 FPS 落同帧的点；转换 pre/post/extrap 与 bezier X 坐标；以完整 before/after 快照 command 原子提交并 bump revision；更新预览/音频/缓存/持久化。验收：24↔48、23.976↔29.97，视频/字幕/标记/音轨/master/效果动画实时时间均容差≤1目标帧，Undo 恢复完整状态，保存重开一致。

<a id="fe-b02"></a>
#### FE-B02｜P1｜序列设置直接改缓存，Undo 与 revision 路径脱节（静态确认）

证据：[设置提交](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:762>) 直接替换 `_current_timeline_sequences` / `_current_edit_project`、load_timeline，只有 dirty 标志；不执行 Timeline command，也不 bump sequence.revision。此前 AI plan 的 base revision 可以仍相同。已有代码注释承认 settings dialog updates cache directly。

修复：复用 FE-B01 的 UpdateTimelineSettingsCommand/工程事务，sequence/ref/cache 同步提交；拒绝非法重采样；为格式/名称/sample_rate/resolution/guide settings 维护 before/after；preview renderer 与音频图使用新 revision。验收：一次 Undo/Redo 完整恢复 name/FPS/format 与 payload，旧 AI plan 因 revision 改变被拒绝，取消对话框无修改。

<a id="fe-b03"></a>
#### FE-B03｜P1｜源编辑缺关联音轨和音频目标路由（静态确认，待真实 Qt 验收）

证据：[源编辑调用](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_page.py:2670>) → TimelinePanel → [TimelineView.insert_clip](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:15531>)、overwrite/append；默认都 `get_target_track_id()`（视频目标），只建一个 Insert/Overwrite/AppendClipCommand。与 [素材拖入路径](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:15156>) 的 `has_audio` 检查、视频/音频链接 ID、InsertLinkedAssetCommand 分叉。

结果：同一有声视频用拖入可以有 A/V，两点源编辑插入缺 audio clip；纯音频源走视频目标（有视频轨时插错类型，无视频目标时失败）。应在主实测中用同素材对照两种入口。

修复：提取统一 SourceEditRequest/SourceEditService，显式 media type/streams/source I/O/destination patch/mode，构建同一 A/V 原子 command；视频、纯音频、无音轨、多个音轨均按用户 patch 路由，自动创建轨道走 command；覆盖/插入的 ripple 必须作为一个整体事务，失败无半成品。验收：三种入口的源区间/速度/时长/linked IDs一致；纯音频落 A目标，有声视频单步 Undo 同时移除 V/A。

<a id="fe-b04"></a>
#### FE-B04｜P1｜源 Insert/Overwrite/Append 的混合 FPS 时长错误（已复现）

证据：[insert timeline duration](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1285>)、[overwrite](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1408>)、[append](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1642>) 都计算 `int(source_duration/speed)`，没有源帧率/序列帧率比例；Insert虽传入fps字段仍只保存映射元数据；Overwrite/Append UI 和 command 还不传这些字段。

真实执行：24fps timeline，30fps源 I=0/O=29（1s）三个 command 都产生30timeline帧（1.25s），期望24；Insert保存源30/序列24元数据，但 placement仍30。应复用已有 Rational/frame mapper：`round(source_frames * timeline_fps / source_fps / user_speed)`，speed_point则按积分时间转换，最小1帧；command ripple offset/overwrite区间/undo快照共用同一时长结果。验收：24/25/30/24000/1001/30000/1001混配及长源，三入口/拖放duration一致，音视频/后续同步锁轨道不漂移。

<a id="fe-b05"></a>
#### FE-B05｜P2｜源 Clear In/Out 后时间线按钮仍可用；单帧源编辑被排除（静态确认）

证据：[clear_in_out](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/source_viewer.py:1921>) 不发 in/out changed；[EditPage监听](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_page.py:564>) 只依赖两者同步 enabled。[source params/按钮条件](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_page.py:2788>) 用 I<O，虽然模型和command把Out定义 inclusive、接受I=O。清空后看似可按，实际handler无参数只warning，体验是无反馈“假按钮”。

修复：给 source range 一个 `range_changed(Optional[int],Optional[int])` 状态信号，open/clear/close/set endpoints均发；有效范围I≤O；单端补0/末帧可作为 F01扩展；程序化同步加锁；失败在状态栏给具体提示。验收：Mark→Clear立刻禁用；相同帧I/O可插1帧；切素材、关闭源、撤销/撤回范围不残留enabled。

<a id="fe-b06"></a>
#### FE-B06｜P1｜切换多机位会累计同步偏移（已复现）

证据：[角度切换 command](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2583>) 直接 `target.source_start + target_angle_sync_offset`；当前源窗已包含活动角度偏移。网格 [resolve_angle_source_frame](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/multicam.py:303>) 却使用target_offset-active_offset，所以网格与实际画面不同。

真实执行：A offset0，frame10切B offset+10，frame20切回A offset0，实际A源30，期望20。修复：在显式 group context 取 active offset，用 `clip.timeline_to_source_frame(playhead)` 映射源时间，再先去活动角度偏移/源FPS、转换到组公共时间、叠目标偏移/目标FPS；command传完整 mapping context，不依赖UI临时state；验证源窗末端并处理负offset，不要仅max(0)静默产生不同步。验收：A→B→A、不同偏移/不同FPS/trim/reverse/retime/多次切角及Undo/Redo，grid与实际同一源帧。

<a id="fe-b07"></a>
#### FE-B07｜P1｜多机位波形同步确认后不标 dirty、不 Undo，不更新既有切点（静态确认）

证据：[apply_sync_offsets](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:6857>) 仅更新项目变量/media_pool、刷新网格与状态栏，不执行 project command、不给dirty/revision；set_project通常不发project_changed。组建立也先stamps资产再检查unlocked track，失败仍修改project。已有切点 source_start 不随offset更新，是目前声明限制。

修复：CreateMulticamGroupCommand/UpdateMulticamSyncCommand 先完整预检再原子修改 group/member资产；经统一project提交入口更新revision/dirty/save renderer，复用结果对话框选择“仅未来切换/全部既有切点重同步”；capture project ID/revision/group ID，异步结果过期时不提交；恢复原group与所有clip source窗。验收：所有视频轨锁定创建不改project；波形应用出现dirty提示、关闭询问保存、重开保留；一次Undo还原所有offset和切点，切project后旧worker不污染。

<a id="fe-b08"></a>
#### FE-B08｜P1｜创建嵌套后丢失音轨状态并改变动画节奏（已复现）

证据：[子轨重建](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/nested_sequence.py:345>) 重新构造 EditTrack，仅传 ID/name/type/index/clips，丢 volume/pan/mute/solo/FX/routing/send/automation等；clip仅改timeline_start、保留绝对timeline-domain animated_params。后续flatten保留已有子轨参数并不能修复创建时已丢数据。

真实执行：音轨(.3 volume,-.7 pan,muted=True,automation=read)创建后变(1,0,False,off)；父frame24起 clip动画24=.2/48=.8，子clip起0，第24帧仍.2（应.8），动画延后24帧。mute状态可能令原本静音的内容重新发声。

修复：拷贝完整track语义，映射Bus/Send到子图并处理父与子增益/FX重复作用；clip/effect keyframes和track automation减min_start，保留区间开始前影响插值的锚点；跨选择边界transition/link/markers统一策略；构建前后使用同帧画面/PCM比较验收，不能仅检查clip计数和start。验收：普通+FX+自动化+Bus+静音轨嵌套前后音画容差一致；起始非0有动画仍对齐；保存重开同。

<a id="fe-b09"></a>
#### FE-B09｜P1｜创建嵌套无法撤销（已声明限制，静态确认）

证据：[创建嵌套提交](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:6931>) 直接增加asset/ref/cache并替换父；[能力声明](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/capability_registry.py:383>) 明确创建无命令栈Undo。不是“嵌套未实现”，是高级编辑的历史事务缺口。

修复：把NestedSequenceTransaction包装成跨project+sequence的原子command，capture父/子/ref/asset和旧selection/navigation，Undo移除新child/ref/asset并恢复父完整状态；有后续引用时history时序正常回退，禁止不安全孤儿删除；提交revision/dirty/renderer/保存同步。验收：Nest→Undo→Redo→子修改→回父→Undo、重开和引用计数不留下孤儿，单次Undo符合用户预期。

<a id="fe-b10"></a>
#### FE-B10｜P1｜重链接/换源/代理 helper 重置工程 revision（已复现）

证据：[relink_asset](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:5127>)、[replace_media](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:5245>)、[update_asset_proxy](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:5335>) 手动构造EditProject，不传revision，默认0。ProjectCommandStack随后bump也会从0起，不保证单调。

真实执行：project.revision=7，三helper返回revision=0。修复：纯helper用dataclasses.replace保留全部字段；revision只在统一commit边界递增，未提交模拟不bump；批量relink/online刷新/其他manual constructor做完整搜查；异步代理cache性改动与内容revision策略明确，避免伪锁冲突。验收：revision7→所有提交8（纯helper仍7），Undo/Redo保持单调，未知字段和项目payload不丢，旧plan被正确拒绝。

<a id="fe-b11"></a>
#### FE-B11｜P1｜换源时旧代理任务的完成/失败仍能污染新素材（静态确认）

证据：[replace/relink](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10130>) pop `_proxy_jobs[asset_id]`；[完成回调](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10532>)只有expected_path非None才拒绝path不匹配。已pop后expectedNone，旧worker结束会把旧proxy标到新asset；[error回调](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10697>)同理。删除asset后Undo恢复也可能遇旧完成事件。

修复：任务携带project_id+asset_id+source fingerprint+generation/job UUID，完成/进度/error每种回调都要求当前token相同；取消/删除/换源递增generation保留tombstone；保留worker handle执行cancel，输出先临时文件、验证后原子publish，失败清理；成功仅绑定验证过源fingerprint且任务仍活跃的proxy。验收：生成过程中replace/relink/delete→Undo/切project，乱序success/error不能改新对象，合法任务仍正常接收，取消无残留。

<a id="fe-b12"></a>
#### FE-B12｜P1｜Replace Media未探测新源元数据与媒体类型，短源越界未提示（静态确认+纯模型复现）

证据：[replace UI command](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/media_pool_panel.py:10111>)不传new_metadata；[replace service](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project.py:5221>)保留旧media_type/metadata，文档说调用方负责probe但UI未做。UI文件filter允许视频/图片/音频跨类型选择。使用位置对话框只有“参数保留”，不校验source windows。

真实执行：旧video(fps24,duration240,width1920,height1080)替换成new.jpg仍media_type=video、旧metadata完全保留。修复：后台MediaProbeJob/probe导入服务，替换前构造新asset metadata/color/audio/image sequence识别；asset_usage_index分析各clip实际使用源窗、速度/FPS、nested/composite引用；不兼容类型明确阻断或有说明的conversion，新源过短提供拒绝/缩短/保持时长并hold/占位策略，不得静默；一command保存完整asset/proxy/fingerprint数据Undo。验收：长→短、30→24fps、有声→无声、video→image、HDR色彩变化均提示且preview/export/显示时长一致。

<a id="fe-b13"></a>
#### FE-B13｜P1｜三处 UI fractional FPS时间码互不一致且非SMPTE（已复现）

证据：[Source时间码](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/source_viewer.py:2360>)、[EditPage时间码](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_page.py:2426>)使用float除/模；[Caption workspace](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/caption_workspace.py:82>)还`ff>=int(fps)`clamp。现有正式 [frames_to_timecode](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/interchange.py:57>)支持NDF/DF，却未复用。

真实执行fps30000/1001：caption frame29显示00:00:00:28，规范NDF应:29；frame1799显示00:01:00:00应00:00:59:29；frame107892显示00:59:59:28，DF应01:00:00;00。修复：全UI/时间码对话框/markers/caption/导出使用统一Rational+explicit DF flag；帧码不是按浮点秒拼字符串，输入/输出roundtrip；起始timecode和源timecode持久化schema。验收：29.97/59.94分钟/10min/小时边界、NDF/DF和23.976、source/program/caption一致且定位roundtrip无损。

<a id="fe-b14"></a>
#### FE-B14｜P2｜能力表关于Stem导出有过期声明（静态确认）

证据：[registry](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/capability_registry.py:241>)仍标DORMANT/无生产UI；[菜单](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/module.py:419>)已注册`export_stem_tracks`，[实现](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/file_controller.py:7502>)创建StemExportDialog+start导出。旧嵌套生产test头也仍称无音频，而当前已flatten。

修复：registry状态取入口/renderer backend自检，统一UI帮助和AI capability snapshot；把静态INTERCHANGE_CAPABILITIES标为兼容层或由registry派生，更新验证测试；测试入口确实存在并可dispatch，不能只断言常量。验收：Stem在菜单/帮助/注册表的状态一致、缺依赖时具体诊断。

<a id="fe-b15"></a>
#### FE-B15｜P1｜Loudness report将样本峰值标为True Peak/dBTP（静态确认，具体差值待标准信号验收）

证据：[analyzer](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/loudness_analyzer.py:120>)用max(abs(block))，注释承认“完整实现需4x oversampling”；report/文本/normalize警告仍命名True Peak (dBTP)。样本峰值不能代表样本间峰值，非“没有响度计”，是QC标准精度缺口。

修复：实现有滤波状态的至少4x插值/oversampling true-peak estimator，跨block保持filter state；缓存标准版本/backend字段；无后端就标Sample Peak dBFS，禁止作为真峰值通过证据；Normalize/limiter用真实预估，并避免将未来限幅当成已输出合格。验收：已知intersample overs/低频/满刻度/多通道/块边界/空输入，与参考true-peak实现误差有明确阈值，中文报告/manifest保留真实指标类型。

#### 仍缺失或未接完整功能：可执行实现计划

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

<a id="fe-f01"></a>
#### FE-F01：完整源编辑

1. feature新增不可变SourceEditRequest（asset、source range、mode、sequence target range、V/A patch/stream IDs、用户speed、timebase）与SourceEditService。统一复用InsertLinkedAssetCommand及Insert/Overwrite/Append规则，消除源监视器与拖放两套计算。
2. RangeResolver无端点默认整源，单端补0/末帧，I=O允许1帧；image默认持续秒数按timeline FPS转换。Out inclusive规范贯穿工具条/slider/command。Patch既有目标轨按钮进一步显示V1/A1…到destination的source连接，可关闭视频或音频、选择音频channel layout。
3. 三点剪辑推导缺失源或timeline端点；四点时duration冲突对话选择fit-to-fill/保持源速裁掉/取消。定义明确圆整与handles；缺源时长/离线不猜测，给诊断。
4. Replace Edit替换选片而保持timeline位置/transition/effect/grade/link；复用ReplaceClipAssetCommand/源映射校验，允许对齐source In或playhead。Match Frame按TimelineFrameMapper反求源帧打开SourceViewer并恢复源范围，nested/multicam可逐层定位，reverse/ramp不能用线性猜。
5. patch/默认range/替换策略保存UI state（暂态选择不写媒体）；新增clip字段遵从schema版本；atomic undo保证A/V/轨道创建/ripple一组。
6. focused tests覆盖全源/单端/单帧、mixed FPS、3/4point歧义、音频only/multichannel、locked/patch off、linked AV；Qt点击快捷键和实际导出比较同源相同区间。验收：以30fps有声源在24fps timeline按I/O剪1s，V/A同时恰24帧，Match Frame回源正确，Undo单步恢复。

<a id="fe-f02"></a>
#### FE-F02：修剪比较监视器

1. 复用现有trim_frame_preview_requested和EditFrameCoordinator，request包含edit mode、left/right clip/source帧、generation、delta/handles；由后台decoder/renderer取帧，禁止UI同步QImage/GPU解码。
2. ProgramViewer增加可切换2-up（outgoing/incoming）、4-up（slip/slide两邻居）展示，源与节目时间码、变化帧数、可用handles、重叠/源边界警告；拖动时合并请求，只呈现最新generation，取消清理预览texture。
3. 编辑期间只暂存preview映射，松手一个原command提交；Escape撤消暂态，状态栏提示失败原因。通用layout不改时间线model，无必要schema变化。
4. tests验证ripple/roll/slip/slide边界四帧映射、stale帧丢弃、取消资源释放；实际拖动在低/高zoom验证monitor跟手。验收：按帧step反馈无过期画面，预览首尾与提交导出对应，拖动不制造多条Undo。

<a id="fe-f03"></a>
#### FE-F03：剪辑动画编辑器

1. adapter把clip/effect AnimatableParam映射为shared KeyframeTrack，明确frame是timeline域；复用Add/Remove/Update/SetKeyframeInterpolationCommand，补Move/Batch/Paste/BezierHandle command和批量macro，不原地修改mutable track。
2. 从Properties ◆旁“打开动画编辑器”和dock提供dope sheet/curve切换，多选、框选、时间吸附、value/frame数值、前后关键帧跳转、复制粘贴、hold/linear/ease/bezier手柄；selected effect/clip更新context、状态锁避免递归。
3. 统一renderer在每帧按AnimatableParam求值，曲线拖动走预览snapshot、松手原子commit；HDR/opacity/scale数值范围按参数schema，不把普通float一律clamp0..1。
4. keyframe已有to_dict应复用，新增handle/extrap字段有migration，未知参数保留；nested/FPS/source mapping changes统一转换。
5. tests覆盖multi keyframe move后冲突、Bezier求值、copy跨clip/effect、undo/redo、save reopen、preview/export同一采样帧；验收曲线UI显示实际插值而非独立UI曲线。

<a id="fe-f04"></a>
#### FE-F04：实时保调

1. 将audio_timestretch.py已有WSOLA提取stateful processor（analysis/synthesis窗、尾部overlap、累计源相位），worker预取PCM块；queue按timeline mapping提供变速目标长度，支持seek/reverse/ramp时flush/reanchor。
2. AudioGraphRenderer/AudioPlayback/实时engine共用speed-preserve-pitch策略，ring buffer预热并暴露late/degraded metrics；受预算限制时明确降级状态，不悄悄变调。clip/speed已有字段无需新schema，但cache fingerprint含preserve_pitch和算法版本。
3. UI现有tooltip从“export only”改为实际backend状态，播放和export采用同一stretch时间语义；离线仍允许更大窗口高质量模式。
4. tests使用固定音高/扫频/对话、.5x/2x、ramp、反复seek/loop、cancel和长时A/V drift，比较实时offline长度/音高并检查callback不阻塞。验收1kHz在倍速保调仍约1kHz、输出时长符合mapper、无点击声/累积漂移。

<a id="fe-f05"></a>
#### FE-F05：光流（可选高级）

1. 在Effect/Delivery能力registry声明retime interpolation backend与可用quality，依赖可选光流库/Metal/OpenGL compute资源；未安装保持nearest/frame_blend选择，读取旧optical_flow工程明确unsupported诊断。
2. FrameProvider取连续两帧、在source-domain估流、按映射fractionwarp/bidirectional合成并处理遮挡/scene cut；场景检测处禁用跨shot flow，fallback有manifest诊断。
3. cache key含source fingerprint、pair帧、尺寸、色域/算法/quality；GPU只渲染thread运行，取消/seek generation淘汰texture/cache；preview可代理质量但export重算native/HDR。
4. 模型SpeedParams.interpolation已存在，新增quality/version要schema兼容；验收以平移、运动遮挡、快速pan/硬切、HDR亮部样本；比frame_blend降低重影，preview/export采样time一致，失败具体提示。

<a id="fe-f06"></a>
#### FE-F06：Normalize与Auto Duck

1. Audio Gain菜单升级为dialog（固定dB/目标LUFS/真峰值限制/逐片或整选组），分析用现有LoudnessAnalyzer后台解码且可cancel/cache；对源trim/speed/FX作用点明确，后端缺失禁止Normalize不可信floor。
2. analysis结果→SetClipAudioGainCommand或macro，预测over/limiter由已注册DSP effect应用；先完成FE-B15。多selected clips有“同增益保持相对电平/逐片均衡”选择，UI展示目标/测量/预计gain。
3. auto_duck不另造数学：接SidechainDucker进入audio graph snapshot/DSP factory；registry schema包含sidechain track/bus、threshold/ratio、attack/hold/release/makeup；validate graph阻断依赖环/不存在目标。
4. UI混音器/音乐clip允许指定对话track或bus；可选“烘焙volume automation”模式复用track automation command。preview/export/stem共用graph/DSP，seek清envelope，muted sidechain与bus位置语义明确。
5. duck params/路由schema版本、unknown字段保留；tests测silent source、multichannel、fallback、duck attack/release、cycle、seek、FX bypass/undo、save/reopen和preview/export PCM parity。验收语音期音乐降低、结束平滑恢复，显示true-peak/输出匹配。

<a id="fe-f07"></a>
#### FE-F07：嵌套完善

1. 先FE-B08/B09语义保真与atomic history。flatten增加递归time transform（parent timeline→nested source→child timeline），用同Rational/SpeedParams处理speed/reverse/speed ramp，音频child也以样本域映射；当前`speed !=1 audio skipped`应改可用算法/显式不支持诊断。
2. 子轨transition搬到父混音图并保留映射后duration，FX/automation/Bus拓扑完整复制且避免父子double gain；更合适方案让child音频图作为subgraph，减少track flatten丢语义。
3. UI显示父→子breadcrumb、返回按钮、源range、子时长变化时如何处理父窗（保持/扩展），增加Unnest command恢复child内容于parent、链接和routing remap，所有变更进历史。
4. 字幕nest可选：native字幕应仍在delivery语言轨还是burn进child需明确，采用专用caption remap和graphics binding迁移，不能当普通asset clip移动。拒绝unsupported组合也需可见解释。
5. 保存child refs graph DAG+schema，删除/undo引用计数防孤儿；tests递归3层/ramp/reverse/audio transitions/child删缩时长/cycle/undo，nested前后画面/PCM golden比较。验收普通嵌套音画保持、倍速音频不断，child编辑即时反映。

<a id="fe-f08"></a>
#### FE-F08：多机位完善

1. 新建project级MulticamGroup管理（asset不能只载单group，否则素材加入另一组会覆盖第一组），clip显式group ID与angle时间映射；保留asset-carried旧schema迁移。
2. 组以Rational公共时间/源TC或秒偏移，关联起始TC/reel/frame rate，修FE-B06；同步来源audio/TC/marker/manual，后台sync report confidence/handles且可修改，FE-B07所有提交可undo、既有切点是否追溯明确。
3. grid改低分辨率proxy多路decode streams，预算限制时降低各angle fps/resolution，active angle优先，隐藏dock即停止次路预取；telemetry暴露真实/静帧模式。线程与GPU复用当前decode manager，禁止每tile另起OpenGL context。
4. audio policy固定主音频/跟video角度/指定angle，切点短crossfade并按目标source channels处理，联动命令原子；组管理允许rename/reorder/add/remove angle、改默认audio、保护已使用angle。
5. tests同一asset多group、mixedFPS/TC、长offset、切角speed/reverse、重同步旧片段、recording连续1–9、preview/export/save reopen。验收网格与节目同瞬间、切角不漂、音频策略明确且单步Undo。

<a id="fe-f09"></a>
#### FE-F09：字幕工作区

1. CaptionCueModel增加可编辑TEXT/SPEAKER/STATUS/IN/OUT列：setData只emit请求，不直接mutate；通过UpdateSubtitleClipCommand/metadata和批量macro提交；保留cue ID、selection/focus，输入验证重叠/最小时长/语言。
2. 将NoEditTriggers改指定列delegates，Text多行、speaker/status枚举、timecode统一FE-B13 converter；实现keyboard Enter/F2/Tab/上一下一cue；所有hardcoded English文案I18N，可访问名+focus sequence。原Properties编辑继续共享同模型。
3. Split支持播放头、文本光标或word alignment，按句读/ASR time映射；Merge只允许兼容track/language邻接区间，批量拆分/样式不逐次reset。当前固定midpoint可保留为明确选项。
4. 手动翻译入口复用captions.translation/AiAssistantTranslationProvider：worker按chunk/长度budget带cue ID返回、验证总数/空文/same-project revision；生成独立target-language track，原文不改，对齐metadata不错误沿用word timing；preview批准后CreateTranslatedSubtitleTrackCommand，失败/取消不半轨。
5. 预览和burn/mux/sidecar共用CaptionStyle/render_plan，quality panel定位CPS/WPM/超长/重叠/缺font，并批量建议command；schema用既有CaptionCueMetadata/track metadata，不再旁路存UI dict。
6. tests编辑→undo→save reopen→export文本与时间、同语多track查找scope、translation timeout/cancel/stale/长度错、2000cue虚拟化不reset焦点。验收直接改表格能看/听对应cue，真正支持中文工作区。

<a id="fe-f10"></a>
#### FE-F10：代理任务管理

1. 复用ProxyGeneratorWorker/DiskProxyService/ProxyPolicy，新增集中ProxyJobManager（token修FE-B11、asset source fingerprint/目标尺寸/编码/profile/status/progress/cancel/输出temp），不使用UI `_proxy_jobs` dict作为唯一生存标识。
2. queue并发上限按磁盘/CPU预算，批量selected/bin生成、pause/cancel/retry/优先级，进度从ffmpeg progress实时解析，空间不足/离线/编码失败显示具体原因；每asset状态不因旧error覆盖。
3. 任务中心可见总体与每项progress/预计容量/原片-代理切换政策；cancel释放process/临时文件，关闭项目明确后台任务处理。
4. 工程只存可重建的policy/fingerprint与可重定位proxy引用，不保存机器绝对临时cache；管理cache位置/搬迁/清理、恢复后verify再ready。
5. tests高并发/取消/换源/删除undo/重开/job finish/error乱序/磁盘失败；验收用户可看进度、取消成功、旧源proxy绝不会替换新源。

<a id="fe-f11"></a>
#### FE-F11：备份与恢复

1. 复用EditProjectIO异步snapshot/save，现有fixed .bak升级timestamp+project ID+revision+checksum版本ring并加manifest；保留旧.bak兼容，retention按数量/天数/容量，与autosave隔离。
2. project recovery UI列出正式/autosave/backups时间与revision、可读性、timeline/asset概要；先background validate package/schema、preview加载到隔离副本，“恢复为新副本”默认，替换原工程在事务前先备份。
3. restore服务不能直接裸copy2覆盖当前工程：临时文件/validate/fsync/atomic replace；打开恢复工程采用canonical path和新session generation，防旧save worker覆盖新恢复版本。
4. tests断电截断/损坏zip/磁盘满/权限/异步autosave竞态/旧schema/unknown payload/有未保存更改；验收恢复后完整timeline/字幕/AI/composite payload，原文件可回退、备份不会悄悄只有上一版。

<a id="fe-f12"></a>
#### FE-F12：交换格式

1. FCPXML/OTIO现有adapter扩展必须按feature mapping matrix：源FPS、speed/timeMap、gain/pan、linked A/V、nested、subtitle/style、markers、transitions、effect能力逐项分类preserved/converted/dropped/requires_action；先做roundtrip子集，不承诺第三方FX通用。
2. export预检结果先显示将丢失项并支持“渲染替代asset/保留metadata sidecar/取消”，渲染替代复用render cache/export service，不能静默drop。导入probe/relink+preview确认维持现有workflow，所有refs/assets/sequences原子command。
3. AAF为可选adapter（aaf2），先音频子集：source mob/PCM media、track/clip placement、sample-accurate in/out、gain/pan/fade、BWF/reel，嵌入/引用媒体及handles；unsupported FX转Stem/sidecar；无依赖入口明确诊断。
4. EDL作为可选低成本video cuts/dissolve/reel/timecode adapter，可优先复用OTIO adapter插件（实际依赖/版本经环境验证），严格限制单video+audio layout/速度子集，超出显式report；OMF维持不实施除非出现明确交付需求。
5. serialized interchange provenance和未知source metadata保留version；tests24/25/29.97DF、源TC、missing media、long path、roundtrip、外部FCP/Resolve/DAW实际开项目验收，不能只成功生成XML/AAF就算完成。

<a id="fe-f13"></a>
#### FE-F13：逐词文本剪辑

1. 复用analysis store/transcript provider/semantic index/timebase，定义word段（asset、source timestamp、speaker、confidence、evidence kind）和VAD silence，UI transcript虚拟化与source/timeline播放头双向定位。
2. 用户选句/词执行Insert/Lift/Extract/删除停顿，编译成existing typed edit operations/plan，使用EditProjectService事务与sync lock/link策略；按目标timeline FPS映射区间，原ASRsource evidence不因timeline变更覆盖。
3. AI voiceover polish已有规则仍保留；自动去停顿须用VAD/word证据、阈值/前后handles、crossfade而非只用transcript覆盖率；应用前diff预览，让用户排除口误/语气停顿。
4. 可选alignment provider声明准确度/网络/credential要求；无word evidence降级到句级，不伪造逐词精度。project存transcript/index版本与source fingerprint，失效重算后台进行。
5. tests跨句/不同FPS/linked AV/音乐sync lock/音频crossfade/Undo/无词级时间/外部模型失败，实际口播剪后无截字、音画对齐，diff可复审。

<a id="fe-f14"></a>
#### FE-F14：能力与帮助

1. runtime registry与effect catalog按当前backend/dependency/入口实际dispatch能力产生可用/部分/不可用；清理Stem/嵌套旧声明，AI manifest和产品help只消费同一snapshot。
2. 菜单缺依赖/locked/无source/no selection用禁用原因和任务导向帮助；页面功能列表可跳到Multicam/Caption/ADR/proxy/exports dock，补关键快捷键和焦点上下文，避免已实现的功能“找不到”。
3. 强类型tool/service contract允许UI/AI同一项能力调用和参数schema，不通过模拟鼠标走业务；帮助写真实版本、partial限制/恢复途径，registry tests验证每个available存在entry+backend+focused test。
4. 验收中文环境能力发现与真实入口一致，不出现“支持不存在”或“功能有但帮助说没有”；optional dependency缺失只降低该项，不阻塞整个剪辑页。

#### 本分项可重复证据摘要

- MULTICAM：实际运行SwitchMulticamAngleCommand两次，A_at_timeline20_actual_source=30、expected=20。
- NESTED：原音轨volume .3/pan -.7/mutedTrue/automation read → 子轨volume1/pan0/mutedFalse/automationoff。
- NESTED_ANIMATION：父clip frame24起，24=.2/48=.8 → 子clip frame0起，第24帧仍.2、expected=.8。
- FPS：现有 `_with_timeline_fps` 方法 AST 直接提取运行，24→48fps字幕start1s→.5s，marker未缩放，opacity 1s .2→.8。
- MEDIA：project.revision7 → relink/replace/update_asset_proxy均0；replace video→.jpg仍video与旧元数据。
- TIMECODE：Caption frame29 @30000/1001输出:28（expected NDF :29）；1799输出00:01:00:00（expected00:00:59:29）。
- SOURCE_FPS：source30fps 0..29，在24fps序列运行Insert/Overwrite/Append各clip.timeline_duration=30（expected24）。

以上执行没有GPU/硬件/网络调用，不可据此声称已通过完整剪辑页人工交互或实片导出。需主报告列出每项的Qt交互、视频输出和外部依赖覆盖程度。

## 12. 补充领域服务实施细则

<a id="domain-01"></a>
### DOMAIN-01：统一时间变换事务

输入不可变sequence、operation、source metadata、editing-unit与锁/sync policy；输出完整before/after、affected IDs、source映射、transition/marker/keyframe变换和诊断。先完成slice、move、follower-shift，再迁移trim/roll/slide/insert/overwrite/range。只在成功service commit后入历史与发布一次revision。所有undo使用真实快照，不用逆向算法假设可逆；保存未知字段。验收为逐帧source映射和全payload往返，而非ClipInstance的ID equality。

<a id="domain-02"></a>
### DOMAIN-02：跨点与字幕同步范围

在Sequence Sync Scope定义目标轨/所有sync-locked轨、字幕是否跟随。跨编辑点的长音床先slice或按明确规则保持，字幕cue/word alignment用caption范围服务切/移；先在原坐标系合并跨度并求delta，再一次校验冲突与锁。UI显示受影响轨与冲突原因，不逐轨独立clamp。clip/marker/effect keys/bindings同事务。验收含跨点音乐、跨点字幕、多删除范围与锁组合，apply→undo完整一致。

<a id="domain-03"></a>
### DOMAIN-03：可用素材handles

SourceAvailability按真实asset fingerprint/FPS/PTS求可用source范围；图像、生成器、composite、nested、motion graphic提供不同extend政策。mapper先将目标timeline区间转source区间，command入口统一拒绝越界；缺元数据不给巨大默认handle。Trim Monitor显示剩余head/tail，边界阻尼停止但不在UI截断后冒充合法提交。验收非零source_start、倒放、ramp/mixedFPS、所有trim变体，精确最后可用帧且超一帧拒绝。

<a id="domain-04"></a>
### DOMAIN-04：组合回归门禁

每项bug新增失败用例；覆盖speed×FPS×reverse×fades×transitions×marker/keyframes×linked/multi/locks×operation。核心检查source帧映射/实际秒时长、完整Undo/Redo快照、单调revision；QTest检查press→preview→cancel/release真实链；实app检查CPU/GPU画面与音频、save/reload/export、失败/取消/关闭资源。执行focused范围后只因新风险扩大测试，避免重复无意义耗时。已通过381项基础测试仍允许本次bug复现，需增加能失败的边界断言。

## 13. 证据复跑与任务维护

主driver使用隔离配置`/tmp/topos-edit-audit-20261013/config.json`和临时工程，不加载用户近期工程。运行命令：仓库`.venv/bin/python`执行本目录下`docs/audit/2026-10-13-edit/interactive_driver.py`；将evidence中的command脚本逐个**先写临时文件、原子rename到session的command-NN.py**，等待对应done再发送下一份。driver使用Qt事件循环；它是审计fixture控制，不是生产业务入口。所有source seek/track lock/显式序列选中等fixture准备在脚本中可见，不记为用户点击的业务功能。

原子文件入队与driver防重入已补在审计脚本，避免qWait处理Qt事件时脚本交错。`command-06/07/10`保留测试中锁轨拒绝与未成功选中序列的尝试；以command-11最终正确fixture播放结果为准。数据记录中的source异常绝不作为通过结果。

核心复现：设置`PYTHONPATH`为项目根后执行`evidence/core_repro.py`，输出与core_repro.out对照；offscreen交互有5个probe脚本/JSON，默认不构造生产GPU。实际导出用`export_smoke.py`加载audit-scratch.topos与当前真实素材路径；export-validation.json保存重解码帧数/视频流/音频流和非黑像素检查。导出范围是exclusive end49，产物49帧；不得把它写为50帧通过。

维护字段建议：每个追踪ID在实施PR中引用，补负责人/目标版本/实际修复文件/失败用例/真实窗口证据/验收人。状态按待实施→实施中→待复测→完成流转；“完成”要求详情中的验收通过，不按功能按钮出现或静态代码存在关单。
