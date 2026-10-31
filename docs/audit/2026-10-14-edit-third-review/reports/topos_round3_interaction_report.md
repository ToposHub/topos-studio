# 第三轮剪辑交互只读复查

2026-10-14，HEAD `63c0bdeaeb5edcee59363479cd8c10b947c3ddb1`，对照 `833dcd382`。项目根 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。已遵循 `.clinerules`/AGENTS.md，读新计划§0和 `test_recheck_a_cancel_projection.py`、`test_recheck_c_multiselect.py`、`test_recheck_f_zoom_anchor.py`。未改生产代码、原测试或旧证据；未启动可见GUI。

全部新probe/JSON/log保存在 `/tmp/topos_round3_interaction*`。Q=offscreen真正Qt鼠标/键盘事件、真实控件、模型与命令栈；Properties轻量宿主继承真实EditControllerMixin，仅提供page/view/panel引用及空preview回调，不mock命令apply。新batch信号按当前MainWindow生产接线连接真实handler。失败路径安装审计excepthook记录异常并继续；另外保留默认hook的锁属性probe退出134日志。菜单使用旧审计的QMenu.exec替身；Alt移动只在该dispatch提供受控global modifier（QTest.mouseMove无modifier参数），不冒充OS持键。真实窗口与GPU由父agent负责。

## 0. 本轮判断

基础取消、disabled投影、普通trim source preview有实质改善。当前新失败集中在Properties多选提交之后的状态刷新、Speed facade/Undo、拒绝路径没有UI恢复，以及Roll自己的浮窗抢焦。**不能用54个输出均无最终变更，宣称54个有效拖动取消全过**：其中3个Roll在用户取消前就被TrimMonitor激活产生的FocusOut取消，根本没有形成有效拖中preview；仅51个其他组合可作为实际取消证据。

| 前轮项 | 第三轮状态 | 本轮验证与边界 |
|---|---|---|
| RI-01/R13 Roll取消 | guard/handle取消原语已落地；**正常真实Qt gesture存在G+Q失败，尚不能完整关闭** | started已发，随后ActiveWindowFocusReason、activeWindow=TrimMonitorDialog，state提前IDLE；详T3-07。父agent可见MainWindow已确认正常Roll不提交 |
| RI-02/R14 linked/Slide取消 | **已修本轮取消范围** | 51个非Roll组合含linked左右trim/Slide/Alt/multi/切工具/迟到release，模型/Item/历史复原；跨锁且不同speed按Esc再换工具后迟到release也干净。正常拒绝release仍有T3-01 |
| RI-03/R01锁轨 | 领域不再写锁轨；**UI拒绝闭环部分修，新增未捕获异常** | 单选Transform/Crop/Opacity/Speed各抛Qt槽ValueError，model/history不变但widget假值残留；batch目标锁预检拒绝整组、无异常 |
| RI-04/R15 Properties Speed | **部分修** | FPS时长120src/30→24的2x现在48正确；linked A仍96@1x；统一ChangeClipSpeed分支因facade缺导出永远不走，batch Speed Undo不恢复 |
| RI-05/R16多选 | **部分修** | 一次X/Opacity写入两片且单宏Undo已修，Transform Reset一步Undo已修；连续字段改动回退前一字段、Crop Reset不原子且未真Reset、Reset后panel缩单选。mixed显示新计划明确未做 |
| RI-06/R17 disabled投影 | **已修本轮范围** | full load读false，菜单Enable，点击正确启用；锁菜单/键盘D无写入；轨头Sync Lock/Undo仍正确 |
| RI-07/R18 trim preview | **普通左右2x已修** | snap开/关live source120/180、release同值；linked不同速度各用自身helper。Roll preview仍有1x直接source加减（S，尚不能用当前被抢焦的Q路径证明实际live跳变） |
| RI-08/R19 anchor | **部分修，与§0声明一致；更长序列精度仍不足** | 旧5次ppf0.1漂移-22.73帧已大幅改善；原≤1frame/亚像素origin明确未做。20正+20反缩放期间最大2.10px，超过新≤1px累计口径，末尾回原位 |
| Source范围/FE-B05 | **已修本轮状态契约** | I=O=15三个按钮启用，clear/close禁用且range_changed正确；Source实际编辑history/真实GPU像素由父agent负责，不借范围probe声明它们通过 |
| 旧INT-01/03/04/07/09/10/11 | **本轮未回归** | 旧5组复制probe验证波纹单击、手型/中键、Blade锁/右键、清空B、Shift去重、字幕Fit/边界/删除询问均正确 |

## 1. 当前失败与具体后续工作

### T3-01（P1；R01/R14新拒绝路径回归）：领域拒绝冒出Qt槽，控件/Item留假编辑

精确链：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:2408` `_on_edit_clip_param_changed`直接执行，没有处理domain拒绝。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:5583`直接CommandStack.execute。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:4294`→`:3206`→`:152`，新增UpdateClipParam轨锁assert抛ValueError。

Q输入：锁V轨、clip X10，属性输入99。结果model X仍10，Item locked=true、duration/source100/100、speed1、opacity.8；widget99；panel快照仍X10；undo0/redo0/revision0；审计hook捕获1个ValueError。Transform/Crop/Opacity/Speed各同型。默认hook脚本process exit134，见 `properties_speed.log`；不能据此断言所有生产宿主自定义hook都一定退出。父agent已可见MainWindow确认锁属性产生未捕获ValueError、model不变、控件仍enabled。

扩展Q：开放V2x linked到锁A1x，左右trim10后**正常release**抛`Trim clip rejected: track 'A1' is locked`。模型仍两片100帧、history0/revision0，但右修Item两片仍90，源V180/A90；左修Item start110、duration90、源V120/180与A110/90；state虽IDLE，affected还留['a']。Esc→换工具→迟到release则干净，说明取消修好了但失败收尾漏了。

证据：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_interaction_properties_full.json`键`locked_single_*`；`properties_speed.log`（默认hook）/`properties_speed_with_hook.log`；`extended_tools.json`键`linked_locked_trim_*_release`。

实施：保留领域守卫；统一commit adapter返回结构化allowed/reason/committed结果或在UI信号边界捕获已知领域拒绝，回显原因，并在finally从canonical model恢复控件及全部affected Item/preview/grab/session。Inspector按allowed状态禁用；linked trim在press预检整个editing unit，release仍要domain防线。不能只吞异常或放开锁让测试通过。

验收：单选所有native属性、锁伙伴trim、碰撞/源界/invalid value的所有拒绝，无未捕获Qt异常，无model/undo/redo/revision变化；widget/Item回原值，随后合法动作正常；失败不留下ghost/preview/affected登记。

### T3-02（P1；R15未接线）：Speed“统一入口”因facade漏导出始终回退，仍破坏linked A/V

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/commands_facade.py:37`导入表、`:51`返回表没有ChangeClipSpeedCommand；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:2375`与`:3935`依赖该key，实际None。

Q输入：30fps源120帧、24fps序列，linked V/A原96@1x，单选V属性Speed2。结果V48@2x、A96@1x，仍双向linked，undo1/revision1。FPS公式修好，但A/V完整性没修。另锁A伙伴也不阻止开放V100→50@2x，A保持100@1x，Undo1，违背整组拒绝契约。

证据：`properties_speed.json`键`linked_mixed_fps_speed`；`properties_full.json`键`locked_partner_single_speed`。

实施：facade正式导出既有ChangeClipSpeedCommand并契约测试key；每个真实Properties Speed入口都选择该命令，避免“pragma no cover fallback”其实默认生产路径。fallback若保留须明确同语义含linked/lock/动画/fade/transition。逐个实际事件验证new命令type而非只测helper公式。

验收：菜单/Properties单选、多选、mixedFPS、分段速度/反向/保调/插值改变的领域payload一致；linked A/V同帧同速；锁伙伴整组拒绝；Undo/Redo恢复全部payload。

### T3-03（P1；R16新回归）：batch Speed没有old_value，Undo弹掉历史但内容不还原

定位：同绝对路径`edit_controller.py:3944`old_value字典缺speed；facade漏key使batch走UpdateClipParam fallback、old_value=None；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:3332`/`:3345`undo在None时读“当前speed”为旧值。

Q输入：跨视频轨c/d原100帧@1x，多选Speed2。结果模型与Item都50帧@2x、undo1/revision1；一次Undo后模型与Item仍50帧@2x，undo0/redo1/revision2。无异常，历史已移动但数据没恢复。

证据：`properties_full.json`键`batch_speed_undo`逐步完整模型/Item/控件快照/历史。

实施：R15接线修复同时补批量builder完整before值；命令apply保存自身before payload，不能等undo时读取当前值推断旧值。即使fallback存在也必须保存old SpeedParams、duration/fades/transitions/linked相关状态；对macro序列化往返补Undo实际结果测试。

验收：Speed2→Undo每片原1x/100；Redo50@2x，连续3轮和command JSON往返同payload。old_value缺失既有命令应有安全兼容策略，不能成功返回未恢复状态。

### T3-04（P1；R16新回归）：多选第二字段基于旧panel快照，静默覆盖刚编辑的字段

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_properties_panel.py:1587`从`_current_clips`派生整dataclass，`:1645`per-clip derive；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:3969`batch结束只设置page，无single handler `:2417`的update_current_clip回填。真实timeline_changed链`:5787`更新page/project/renderer而不刷新Properties快照，因此不是轻量宿主漏掉生产刷新。父agent已G确认X55后Y66两片X回原0。

Q输入：c(X10,Y11)、d(X20,Y21)。X55后model两片X55、undo1；panel snapshots仍X10/20。接着Y66，model cX10/dX20、Y都66，undo2/revision2；**控件仍显示X55/Y66**。Crop同型：left5%后right7%，两片left回原10%/20%，UI仍left5/right7。

证据：`properties_full.json`键`batch_xy`、`batch_crop_sides`含model/panel snapshots/widgets/Item/history；并有父agent可见窗口独立复现。

实施：batch成功后按stable clip IDs用canonical committed snapshot刷新全部`_current_clips`；保留selection context、按_updating锁更新必要控件且不触发新命令。或builder接收scalar patch，提交时基于当前timeline逐片derive，不能信任UI缓存整对象。Undo/Redo/外部修改也共享该刷新路径。

验收：多选X→Y→rotation、Crop left→right→feather、Speed→reverse连续编辑不回退前项；其他未改字段每片各自保留；控件、canonical model、renderer、保存重载一致，逐步Undo/Redo准确。

### T3-05（P1；R16 Reset仍不原子）：native Reset逐参数发宏，Crop只留下最后一项默认值

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_properties_panel.py:1802`逐param发`_emit_section_param`，`:1805`提前把widget全部置默认；每项经新batch handler各成一次macro（controller3967），又触发T3-04旧快照覆盖。

Q输入：两片Crop均五项非默认。Reset一次：控件left/right/top/bottom/feather全部0，model却仅feather0，四边保留原值；undo5/revision5。一次Undo后只到“bottom0、feather旧值”中间状态，undo4/redo1/revision6。Speed本来默认1x的Reset仍生成3条历史/revision3（speed/reverse/preserve_pitch），没有实际内容改变。

证据：`properties_full.json`键`batch_crop_reset`、`batch_speed_reset`。

实施：Reset按每片snapshot一次构建完整默认参数，所有字段/target放同一宏一次commit，而非重放每个控件事件；拒绝时整组回model；所有成功控件显示依据committed result；在builder/apply阶段消除no-op命令。

验收：Transform/Crop/Speed/Opacity/RAW/effect reset各自一次动作一次Undo/revision，无半Reset；所有default实际写入；一次Undo恢复每片所有旧值；已默认Reset无history、无revision且不清Redo。

### T3-06（P2；R16上下文仍易丢）：Transform Reset后Inspector变单选，timeline仍是多选

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:4034`Reset后只`_show_clip_in_properties(first)`；`:4082`调用set_clip；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_properties_panel.py:2048`把context改成[clip]。

Q输入：选[c,d]，Transform Reset模型两片归0、undo1正常；但panel context=[c]/标题Properties，view selection仍[c,d]。接着Y88仅c变88，d仍0，undo2。旧probe把`_show_clip_in_properties`替为空回调，不能验收这个刷新契约；本轮扩展调用真实方法。

证据：`properties_full.json`键`batch_transform_reset_context`。

实施：Reset后按现有SelectionContext刷新完整clips/tracks/兼容属性（或统一selection刷新service），保留primary与全部IDs。不得为了刷新一个section走单选set_clip。失效ID/异类target有明确scope。

验收：2/10片Reset后context/count/title/selection一致，下一次任何共享字段仍写全部兼容片；Undo/Redo/跨页面/selection增减不退成primary。

### T3-07（P1/G+Q；R13新交互回归）：显示自己的TrimMonitor触发失焦取消，正常Roll也无效

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:9049`新增ROLLING；`:4146`→`:4160`在press内show monitor；`:4617`focusOut不区分自身被动显示窗/外部窗口，直接cancel；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/trim_numeric_dialog.py:165`Qt.Tool窗。

Q输入：相邻clip seam100，ROLL；QTest命中RollHandle（local中心、按钮Left、visible=true），started=[l,r,100]。派发后实际FocusOut reason=ActiveWindowFocusReason、active_window=TrimMonitorDialog、当时stateROLLING；move后stateIDLE、双方preview未变，正常release不提交。0.5x/2x相同；54旧matrix三个Roll也同样在cancel前IDLE，不算有效drag取消通过。

证据：`roll_preview.json/.log`含hit target、started、focusOut和模型；`roll_passive_probe.json`是在fixture给monitor WA_ShowWithoutActivating/WindowDoesNotAcceptFocus的诊断，offscreen仍出现同一抢焦，不作为已验证修法。父agent已G确认真实H264相邻50+50帧、source余量充足、seam50命中RollHandle，started时ROLLING；首次press100ms后IDLE/previewFalse/monitorFalse，移36px后finished delta2但model/revision不变。真实日志gui-session3/roll_signal_trace，截图23/24/28。Q事件filter进一步定位activeWindow=TrimMonitorDialog造成自FocusOut；G现象与Q根因证据分别保留。

实施候选：TrimMonitor使用已有窗口内非激活overlay/停靠panel或保证不会抢编辑焦点；session取消依据真正外部activation变化，不能只笼统忽略FocusOut破坏原取消保护。保持迟到release token守卫；用完整真实press→move→release正常Roll测试再测取消。

验收：首次monitor创建/已创建、正常±roll有一次合法提交；Esc/换工具/外部失焦不提交且双片恢复。self monitor显示不会取消正在进行的session；硬件/平台测试要确认activeWindow/focus/grab。

### T3-08（P2；R19声明边界扩展）：连续缩放期间累计量化超过1px

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:2884`每次重新取已量化anchor，`:2922`round滚动补偿；`_scene_origin_residual`预留但没有在这些路径承载残差。

Q输入：长100000帧scene、hbar800、pointer x813、ppf .1/.2/.5/1/3/10，各Ctrl pixelDelta(0,+40)20次再-40共40次，远离滚动边界。期间相对起点最差**2.10019148px**（所有初始ppf相同），最终回初始scroll800/ppf，误差接近0。原五次漂移明显改善，不能把这些当未修旧floorbug；但“全部≤0.90px”的5次测试不能外推长连续缩放。新计划已明确≤1frame/亚像素origin未做，这项不指控隐藏放宽。

证据：`extended_tools.json`键`wheel_20plus20_*`全40sample；旧`properties_wheel.json`锚定五次改善数据。

实施：保存gesture起点连续anchor及subpixel残差，并在统一viewport/layout offset生效；不能每次从已round后的状态取新anchor而使误差积累。各zoom入口统一政策，边界钳制明确。若现产品采用≤1px容差，也需测试长gesture累计范围。

验收：pixel/angle正反各20/100次、pointer多个位置、所有ppf期间累计≤约定容差；返回原ppf能回位；零delta/横滚/Shift区域滚动仍正确。

## 2. 已声明边界和未验收，不混作新bug

- R16 mixed控件尚未做，新计划§0明确部分完成。本轮初始多选cX10/dX20仍显示primary10、无mixed标记；后续mixed工程计划应保留，不能写“全部R16完成”。
- R19亚像素origin未做，源计划已声明≤1px平台量化与≤1frame未达到；T3-08是更长gesture额外证明累计误差，和这个声明边界同时记录。
- R18 ordinary trim已改模型helper；Roll `timeline_view.py:9135`/`:9148`仍直接source±clamped_delta，2x/0.5x live mapping静态不统一。由于Q正常Roll在move前被抢焦取消，本轮不把未实际生效的preview算作已动态复现；待正常RollGate通过后补取preview与release对比。
- Source range probe没有真实解码/有效GPU像素；CPU/GPU首帧、source三入口history由父agent可见GUI和对应核心分工验证。
- 当前新tests多数直接调用preview handler或MagicMock batch宿主，只证明取消原语/单次派生/公式；没有覆盖本轮实际signal wiring、第二字段、Reset之后上下文、拒绝Qt异常、长gesture。`test_recheck_f_zoom_anchor`为5正5反且未show；本轮使用真实wheel送入viewport+可显示offscreen QWidget。

## 3. 证据和重跑

推荐引用最新完整证据 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/evidence/probes/topos_round3_interaction_properties_full.py/.json/.log`（含model、Item、panel snapshots、widgets、selection、history、revision、uncaught）；其余保留以供核对：

- `legacy_probe1..5.py/.json/.log`：旧五组复制，未覆盖历史。
- `cancel_matrix.py/.json/.log`：旧54重跑；51有效非Roll组合和3个提前自取消Roll必须分开叙述。
- `properties_wheel.py/.json/.log`：本轮batch信号补接后的单次参数/Reset、5次anchor基线。
- `trim_preview_menu.py/.json/.log`：2x普通trim preview一致、disabled/锁菜单。
- `atomic_reset_sync.py/.json/.log`：含锁批量Reset整组拒绝、Sync Lock+Undo。
- `properties_speed.py/.json`、`.log`及`_with_hook.log`：默认hookabort和hook后model/history/linked结果。
- `properties_extended*.py/.json/.log`、`properties_full.py/.json/.log`：连续字段/native Reset/上下文/各锁入口。
- `extended_tools.py/.json/.log`：linked跨锁普通release失败、Esc换工具迟到release通过、20正20反wheel。这里两个Roll sample未形成drag，引用Roll专用focus证据代替。
- `roll_preview.py/.json/.log`、`roll_passive_probe.py/.json/.log`：真实Roll started/FocusOut证据和明确受控诊断。
- `source_range.py/.json/.log`：范围状态与facade AST。

全部文件均以上述`topos_round3_interaction_`前缀。在项目根执行`.venv/bin/python /tmp/topos_round3_interaction_<name>.py`；脚本设置offscreen。只有domain拒绝测试使用hook继续采证；最早无hook日志保留。未修改用户媒体或配置。

建议实施顺序：T3-01拒绝异常+恢复→T3-02/03真接线与Undo→T3-04模型快照同步→T3-05/06Reset事务与上下文→T3-07正常RollGate→T3-08长gesture精度。先把当前失败事件序列固化，补“成功正常gesture确实有模型提交”的前置断言，防止把无效拖动当取消成功。
