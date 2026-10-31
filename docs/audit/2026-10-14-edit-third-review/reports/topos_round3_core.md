# 第三轮核心只读复查（63c0bdeaeb5edcee59363479cd8c10b947c3ddb1）

对照版本833dcd382，项目根`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。已读取AGENTS.md/.clinerules和followup计划新§0，检查新增A/B/C focused测试与差异。未改生产代码、现有测试、历史证据，未创建可见GUI或执行GPU。

本轮证据均为 **M**（真实模型/command/JSON执行）及根因 **S**。原probe复制到新的round3路径再运行；不覆写旧`topos_recheck_*`。没有将已披露R07±1分数phase范围重新包装成隐藏回归。本报告明确区分：原例已修、已声明partial、超出声明边界的剩余问题、新事务回归。

证据文件：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_core_oldprobe.py`、`.out`：上一轮组合全部重新跑当前版本。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_core_pytest.out`：9文件 **110 passed in 2.08s**；新lock/trim/fade/time-map/slice/follower/transition与原focused文件。未在父agent后续提示后重跑这些测试；不与父agent121/177等统计相加。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_core_combo.py`、`.out`：新增组合。初版Macro例曾因create签名fixture错误未执行；已修fixture，仅单独执行该例，真实结果是下一文件，不把初版TypeError当应用bug。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_core_macro.out`：真实Macro删除/转场复活输出。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_core_precision.py`、`.out`：无slice、无裁剪的neutral速度点whole-clip FPS映射复现。

## 上轮条目当前状态

| 上轮ID | 已重新确认改善 | 当前边界 |
|---|---|---|
| R01 | 原六类Move/Trim/Delete/Insert/OW/RippleTrim锁轨apply均拒绝；批量sync锁音轨100仍100 | 部分：Insert redo快路绕守卫；另外七类内容命令仍可写锁轨（C3-04） |
| R02 | reverse右trim helper110/90，实际110/90；首199保持199；显式Undo源110→100 | 原例已修 |
| R03 | 30src/30fps插24fps，placement和cross delta均24；V/A followers124/124 | 原例已修 |
| R04 | Roll seam转场1→1→Undo1；整体Move两片转场保留1 | 新Macro复活会覆盖显式删除，并复活已删clip引用（C3-03） |
| R05 | 显式leftfade40→20不抛；speed5x duration20/fade20，Undo100/fade40；trim头转场40→20→40 | 原例已修 |
| R06 | step30→24 duration72，tail source218；smooth24/24积分102不再90 | 中间timebase先round会使neutralpoint改变未切分画面，max4（C3-02） |
| R07 | 新focused常速、有FPS、cut=dur//3矩阵按21精确/3±1声明通过；反放1xInsert和Roll原例改善 | 已声明partial的±1phase不新增计数；smoothmax22、step trimmax20、ripple reverse/FPS错域超出phase声明（C3-01） |
| R08 | 原目标轨Insert keys100/199→120/219、marker110→130；rippledelete keys/marker同移 | Insert新marker before/after/跨轨Undo/Redo/JSON不完整（C3-05） |
| R09 | Move/RippleDelete/BatchDelete/Slide/Split原JSONUndo快照例全部恢复 | 新Insert marker、A/V ripplecommand载荷以及OW/Append新FPS字段漏序列化（C3-06） |
| R10 | seam50 marker归右半，validator空 | 原例已修 |
| R11 | source160→timeline48→source160 | 原例已修，未声称所有采样域可逆 |
| R12 | Undo后原位Move返回原对象，Redo仍True，history未增加 | 原例已修 |
| R15 | 新focused通过菜单/Properties领域duration公式；父agent负责UI | 未将原属性路径判新bug；领域接口完整锁/linked覆盖仍属C3-04/07 |
| R20/21 | 新tests代码已读，父agent负责字幕/FPS工程集成 | 未从旧正文重复判缺陷 |
| R22 | 新素材A/V插入同点同duration，basic liveundo及锁音轨预检新增test通过 | 既有pair跨点切分后的rightlinks/marker非法；新cmd无完整redo/JSON（C3-06/07）；OW仍丢关联状态（C3-08） |

旧完整24矩阵当前仍20组存在某帧差异，但它与新test的语料不同（旧含noFPS、step/smooth、cut=dur//2+1；新为常速和指定cut）。**不把这个数字当成对已披露21/24的直接否证**。下面只将无切分neutral点、超过±1的曲线、step整数帧裁剪等独立证据列为新计划工作。

## C3-01 · P1 · curve slice/trim及ripple source域仍未统一（R07剩余） · M+S

精确位置：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2401`取整数split_source，`:2446–2471`截点/改右base；smooth曲线因此按缩短source_duration重新积分/归一。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:640`lefthelper仍base-factor×delta，不调整SpeedPoint offset/曲线窗口；Trim显式构造`commands.py:1810`及legacy`timeline_ops.py:230`沿用完整原speedpoints。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:2794–2796`RippleLeft、`:2898–2900`RippleRight仍旧effective_speed公式，没有调用方向/FPS/曲线边缘映射。当前`split_source_partition :1550`只给整数源区间，既不能承载原曲线域，也未被SplitClipCommand实际委托。

复现A（不等同已声明±1）：source[100,220)，base1/SpeedPoint(40,2,smooth)，timeline96，等FPS24/24或缺元数据，Split49。保留51个帧改变，max源帧差22；且frame43（在切点前）143→144，45=145→147、46=146→148。反放同样51/max22；30→24仍38/max13。这是整个smooth曲线域被重建，远超±1phase限制。

复现B：step source100、base1+point50→2x、timeline75，lefttrim20（整数speed1、整数切点，不需分数phase）。原保留timeline50/60/70源帧[150,170,190]；command和legacy helper都[150,160,170]，pointoffset仍50而应在保留window调整。

复现C：reverse1x source[100,200)、timeline100，RippleLeft裁20：保留原帧应179开头，实际199；RippleRight裁20：原首199，实际179，80个帧全差20。mixed30→24 RippleLeft20，源入点正确应125，实际120，60个保留帧差5。stepRippleLeft max20。Right mixed虽保留画面当前不变，源handle区间还是按旧base delta减20而非正确25，不能把source payload称正确。

修复：统一time-map slice窗口（原曲线、源相位、方向、FPS、曲线域），至少在本次schema没有phase前，也能正确处理整数step与smooth保留控制域；所有Trim/Ripple/Split/Insert/OW/Roll/Slide委托同plan，勿再靠仅source_start/duration改变后重新积分冒充slice。若分数schema未实施，明确记录已知误差范围，不能将本例22/20帧吞进±1声明。

验收：上述保留帧逐帧完全一致；smooth切点前样本不得变化；step切点前/后、reverse、mixedFPS、left/right/ripple各入口，Undo/Redo完整payload一致。保留原已声明phase用例作为独立待办。

## C3-02 · P1 · 先round timebase再乘速度，使neutral速度点改变完整画面（R06部分） · M+S

位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:449–453` `equiv=int(round(relative*ratio))`后才交SpeedParams乘segment_speed。ratio为30/24=1.25，先将source-time equivalent量化再8x会放大误差。

复现：source[100,1100)、30fps，timeline24fps/100帧，8x。加入SpeedPoint(source_offset=200,speed=8)，该点前后速度相同，语义上应没有任何变化。constant rational mapper的frame1/2/3源110/120/130；带neutralpoint后108/116/132。完整100帧75个不同、max4，duration100一致。

这与R07的“slice分数phase”无关：这里未切分、未trim、未改时长或源窗口，只增一个neutral control point。R06的修复路径本身新增了不必要的中间量化。

修复：时间域运算保持Fraction/浮点连续time，SpeedParams curve/step映射接受连续等效位置，最后一次按统一sample政策量化源frame；不能先量化speed-domain ticks。逆映射也同样避免多阶段round/floor。

验收：0.1–8x、neutralpoint任意位置、step/smooth和mixed/fractionalFPS，增加同速点不改变任一帧；正反方向、preview/export同mapper，明确最后舍入规则。

## C3-03 · P1 · Macro复活无删除意图信息，复活显式删除或孤儿引用（R04新回归） · M+S

位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:11271`总是调用复活；`:11329–11339`缺失before转场只要任一owner还在便restore，未区分RemoveTransition/删除clip/瞬时gap。

复现：A[0,100)、B[100,200)，transition seam(A,B,center100,dur20)。Macro只含RemoveTransitionCommand.create(timeline,'seam','v')，最终转场仍1（显式删除失效）。Macro只含DeleteClipCommand('a')，最终只剩B但转场seam(A,B)被复活，validator `Transition seam references non-existent left clip a`。

正确Move整体的原例已通过，不能因此用“总复活”覆盖用户内容意图。此为本批Macro复活新增回归。

修复：收集事务delete intents/几何temporary-deletion分类，或全部变换规划最终轨道一次reanchor；只复活确实因中间gap误删、所有原引用owner仍存在且最终合法的转场。显式RemoveTransition必须最终缺失；删除owner必须删除相关转场。

验收：Move整组保留，真分离删除，显式RemoveTransition/BatchDelete/OW/替换ID宏不复活；apply及Undo/Redo均validator空、转场IDs/params正确。

## C3-04 · P1 · 锁覆盖仍有缺口，Insert redo快路置守卫之前（R01部分/新接线错误） · M+S

位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:13440–13451` Insert snapshot redo直接return，而新增guard在`:13461`；注释称“redo快路径之上”，实际顺序相反。

另缺守卫apply入口：同文件Slip`:2191`、Slide`:11887`、Roll`:14207`、RippleDelete`:11474`、BatchDelete`:12167`、Append`:14024`、Freeze`:17709`。

复现：Insert中间40成功→Undo→将目标track.is_locked=True→已apply过cmd.apply；接受，clip数1→3。原首apply锁守卫已修，redo路径确实绕过。

锁轨三个合法source有余量的clips[0,100)/[100,200)/[200,300)，上述Slip5、Slide5、Roll5、RippleDelete、BatchDelete、Append10、Freeze@150七类全部`accepted content changed: True`。不依赖UI菜单是否禁用；领域/API/宏调用仍可写锁轨。

修复：完整affected editing-unit预检在所有apply/redo分支之前；锁策略覆盖所有内容command，不靠枚举七条名义“统一”。Undo继续允许恢复合法历史；被锁伙伴被解链/删除同样预检。

验收：主/伙伴/sync/旧被解链伙伴各锁态，所有writecommand和nestedMacro，首apply/redo拒绝无变更/无history；Undo历史可恢复。

## C3-05 · P1 · Insert marker事务重复逆变换、redo漏after、JSON漏before（R08新回归） · M+S

位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:13467`保存全sequence beforemarkers；`:13572–13578`Undo先恢复全beforemarkers、再restore_sync_lock_shifts对跨轨marker再次-δ；`:13441–13451`redo只restore clips/transitions/sync，没有目标aftermarkers。`:13616–13641`JSON漏_before_markers。

复现：V clip[100,200) marker vm110；sync A clip[100,200) marker am110；Insert20@0到V。

- Before clip100/100，marker110/110。
- Apply clip120/120，marker130/130（这一步改善正确）。
- Undo clip100/100，vm110但am90，validator am在owner[100,200)外。
- Redo clip120/120，vm110/am110，两个marker都在owner[120,220)外。
- Apply命令JSONroundtrip后Undo，vm130/am110，fullto_dict false。

修复：marker与clips/transitions共享完整before/after计划；选择只恢复编辑轨markers再对其他轨逆变换，或全sequence快照恢复一次。redo用after快照；JSON纳入新增字段并区分空tuple/None。

验收：本例多次Undo/Redo完整marker值/owner归属都正确，add/remove/editmarker中间历史不被全局快照吞掉；目标/linked/sync轨、cross-pointslice、JSON/nestedMacro都validator空。

## C3-06 · P1 · 新command和新增FPS/marker载荷未完整JSON往返，redo身份不稳定（R09/R22部分） · M+S

位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:11148–11182` InsertLinkedAssetRipple只序列化intent，不包括双轨before、sync moves、created track/clip IDs；`:13911–13947` OW以及`:14113–14142`Append漏新增_source/_timeline_fps字段；Insert marker遗漏见C3-05。

复现：现有linked V/A100，40插10新A/V。liveUndo本例恢复单pair；updatedcmd.to_dict→真实JSON→from_dict→Undo，仍保留6个clips（没有before载荷），完全无法撤销。liveUndo后apply同一updatedcmd，重新随机生成split/new IDs，redo前后ID集合不同。

新Overwrite/Appendintent带30fps源30帧/timeline24fps，原apply duration24；同命令JSONroundtrip后apply duration30。这是在同一真实API公开契约内，未声称.topos文件目前持久化Undo历史。

修复：完整before/after、source timebase、sync变换、created IDs随public payload往返，redo使用预定after；新命令不能继续“只序列化输入参数”而回退无Undo能力。明确旧载荷兼容/拒绝条件，不静默部分执行。

验收：上述JSON/NestedMacro等价liveapply/Undo/Redo，ID/链接/源范围/marker/keys/转场/轨道metadata相同。空目标轨和命令自动建audio轨也Undo移除、Redo复用ID。

## C3-07 · P1 · Linked ripple insert只链接新素材，原pair右残片不互逆（R22剩余） · M+S

位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1669–1678`右残片copy原linked_clip_id；新`commands.py:11060–11083`只给新source V/A链接，不修原被分割pair左右身份，未迁移right scoped markers。

复现：v0↔a0均[0,100)，marker75分别归v0/a0；40插10A/V。原左片v0/a0[0,40)仍互逆；原右V[50,110)却link到左a0，原右A[50,110)link到左v0；validator两条`link ... is not reciprocal`。marker75仍归左v0/a0，又有两条owner范围错误。

新source本身双向link正确，这不是原“新素材无音频”问题仍未修；剩余是既有editing-unit slice事务。右段后续move/trim无法识别对应partner，save/load normalization可能清掉错误链接，造成数据丢失。

修复：双轨slice plan同时分配old-left/old-right IDs、reciprocallinks与markerowners；按各自FPS/方向/曲线映射同一timeline seam，保存before/after。单轨选择政策不得复用APP-02局部Blade未决来掩盖Source A/V明确同时切分的契约。

验收：本例四个old残片各成两组互逆，右markerframe85随插入且owner为对应rightID；新pair同点linked；后续只移动右group两轨一致、left不变，Undo/Redo及存取validator空。

## C3-08 · P1 · Overwrite残片改身份但不迁移marker/转场，Undo仍丢转场（关联事务剩余） · M+S

位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1820–1858`中覆盖创建新residual IDs并删除原ID；`commands.py:13862–13908`Undo只回clips，无transition/marker快照与引用迁移。

复现：单c[0,100)，head transition20和scoped marker75归c；Overwrite[40,50)。aftermarker仍指c（c已删），validator`Marker m references non-existent clip c`；转场before/after/Undo 1/0/0，fullUndo false。

修复：覆盖slice必须完整关联事务；未被覆盖的headtrans挂leftresidue、marker75挂rightresidue，被覆盖marker明确定义删除或迁移policy；Undo准确复原转场与marker。SourceA/V覆盖宏最后一次validate，不能仅恢复clip几何或让Macro blindlyresurrect旧ID。

验收：头/尾/中/全覆盖，fades/transitions/keys/markers/link，apply/Undo/Redo完整to_dict及合法引用；mixedFPS/ramp由C3-01统一slice驱动。

## 建议下一批

优先修C3-03/C3-04/C3-05（非法写锁轨、删除失效/孤儿、Undo marker损伤）；随后以真实time-map/slice计划完成C3-01/C3-02，保持已声明phase限制透明；最后把C3-06/C3-07/C3-08收拢为完整命令before/after/JSON/redo与editing-unit关联状态事务。新增tests应从这些具体失败输入出发，禁止只放宽曲线错误到±22或将neutralpoint映射差解释为原slice±1待办。
