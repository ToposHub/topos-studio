# 剪辑交互修复复查（只读，2026-10-14）

项目根：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。基线 `55351a5f45d5a34ff2fc91a3cb9eee53581a889b`，复查 HEAD `833dcd382`。先读项目 `.clinerules`/`AGENTS.md`，检查新 diff，再复跑旧脚本及扩展事件矩阵；未改生产代码、旧计划和旧证据，未启动可见 GUI。

证据等级：Q=offscreen Qt 真正鼠标/键盘事件、真实 TimelineView/ClipItem/immutable model/command stack；Properties 用真实控件、真实 EditControllerMixin handler/command，轻量宿主只提供 page/view/panel引用和preview回调。菜单沿用旧审计的 `QMenu.exec` 替身选择实际 QAction。失焦以 Qt `QFocusEvent(FocusOut)` 送入真实 view，不冒充操作系统窗口切换。S=源码/AST契约。源GL实际CPU/GPU有效像素无法在offscreen验证（Qt明确显示 `QOpenGLWidget is not supported on this platform`）。 Alt组仍由QTest鼠标事件驱动，但QTest.mouseMove无modifier参数且offscreen会把全局modifier重置；仅在该move派发期间用scoped patch提供held-Alt键盘快照，实际copy ghosts达1/2个、取消后0。此项是受控modifier上下文验证，真实桌面Alt持续按下留给父agent补Gate。

## 1. 状态结论

| 旧项 | 当前判断 | 本轮证据 |
|---|---|---|
| INT-01 波纹无拖动单击 | 已修，本轮左右单击无提交；原final=0问题未再现 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe1.json` |
| INT-02 Esc/切工具/失焦 | **部分修**：普通/Alt/多选/linked move、单clip trim/ripple/slip/fade/rate可取消；真正Roll seam仍提交；linked trim/Slide有跨对象视觉残留 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_cancel_matrix.json`（54组合、0未捕获异常） |
| INT-03 手型/中键 | 已修：空白/clip/中键都PANNING，scroll50→140、model start100不变 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe3.json` |
| INT-04 Blade右键/锁轨 | 已修该两个入口：无split、无Undo | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe3.json` |
| C09/INT-05 锁轨统一守卫 | **部分修**：菜单Disable/200%和键盘D拒绝；Properties Transform/Reset仍写锁轨 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe2.json`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_trim_preview_menu.json`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_properties_speed.json`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_atomic_reset_sync.json` |
| INT-06 wheel/anchor | **部分修**：horizontal pixel有效、Ctrl横向现在zoom-in；低zoom锚定仍明显漂移，普通zoom亦偶尔>1帧 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe1.json`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_properties_wheel.json` |
| INT-07 清空快捷键/N名称 | 已修本轮清空B不匹配、不切刀片；名称已改Toggle Snap（S） | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe2.json` |
| INT-08 Enable/Disable menu | **未完整修复**：读取字段已改，但首次load的disabled模型被构造成enabled Item，仍显示Disable，点击反而启用 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe2.json`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_trim_preview_menu.json` |
| INT-09 Shift框选去重 | 已修：同ID一次、is_multi=false | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe4.json` |
| INT-10 字幕Fit/边界 | 已修本轮：Fit ppf3→8.15；playhead50→100 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe4.json` |
| INT-11 字幕轨删除统计 | 已修本轮：询问包含1 subtitle cues，No后仍有轨道且无Undo | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe4.json` |
| INT-13 多选Properties | **部分修**：列表/标题保留2片；Transform/Opacity仍只写primary、没有mixed显示；Transform Reset两次命令、一次Undo只恢复一个；Opacity Reset只primary | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_properties_wheel.json` |
| C06/INT-12 2x trim | **部分修**：release提交正确2x source区间（180），拖中preview仍按1x（190）；snap开关相同 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_legacy_probe5.json`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_trim_preview_menu.json` |
| C07/FE-B04 mixed FPS | **Properties入口未修**：30fps120source帧在24fps序列，Properties Speed2得到60（应48），linked A仍96@1x | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_properties_speed.json` |
| FE-B05 Source范围 | 已修本轮范围契约：I=O=15合法、3按钮启用，Clear/close立即禁用，range_changed正确 | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_source_range.json` |
| INT-14 Source GL crash | facade漏API已修（37个_gl调用均有桥接，uniform1f/4f callable）；paint异常隔离已落地（S）；**真实CPU/GPU首帧Gate仍待主agent可见窗口验收** | `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_source_range.json`，不能用此证据声称有效GPU画面通过 |

旧probe2的 `locked_menu_200%.selection.enabled=true` 是子action自身值；其祖先speed_menu已disabled，且实际handler拒绝、模型/Undo不变。本轮不把该值误报成锁菜单绕过。

## 2. 当前可复现问题及实施计划

### RI-01（P1，INT-02残余）：真正Roll seam不进入InteractionSession，取消后仍提交

定位：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:8932` `_on_roll_trim_started`只保存`_roll_preview_state`、开始双片trim preview，没有`_interaction.begin(ROLLING)`。
- 同文件`:3996` `set_tool`仅在interaction非IDLE时取消；`:4569` `focusOutEvent`也只认interaction.is_dragging。
- 同文件`:9880`取消路径没有RollHandle cancel/`_restore_roll_preview`/`_roll_preview_state=None`/trim monitor收尾。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_items.py:9625` RollHandle release看`_current_delta`直接发finished。

Q复现：相邻片seam100，Roll拖+10。拖中state=IDLE。Esc→release、FocusOut→release均把left duration100→110、right start100→110/duration100→90，Undo=true。切A后release模型未变，但两Item `trim_preview_active=true`，左右几何/源区间残留且`_roll_preview_state`仍在。JSON键`roll_seam_esc/tool/focus`。

实施：把RollHandle begin/update/finish/cancel作为正式session owner，登记双片affected对象及handle；取消原子恢复双片model投影、handle original_cut/current_delta/dragging/grab、trim preview/monitor/snap，清Roll state；release用session token确认仍有效。不可只给ClipItem补drag_mode。

验收：seam±10、Esc/工具/失焦、拖回0、单击、linked/locked组合；取消前后完整timeline.to_dict相等、Undo/revision不变；双片几何/源/fade/preview均等model，handle delta0、state IDLE、monitor/snap/ghost清空，随后新Roll可正常一次Undo。

### RI-02（P1，INT-02残余）：linked trim/Slide取消只恢复primary，伙伴/邻片留错预览

定位：同绝对路径`timeline_view.py:8283`、`:8298`、`:8469`把linked Item源区间/几何直接改；`:9198`、`:9211`把Slide两邻片直接改；`:9919`取消fallback只恢复primary，ClipDragSession位置快照不覆盖这些trim/slide affected对象。

Q复现：linked左裁10取消后V回100/100，但A仍start110/duration90/source110/90；右裁10取消后A仍90/90。Slide中片+10取消后primary回原位，左邻duration仍110、右邻start210/duration90。三种取消均如此，模型payload不变且无Undo，界面留在未提交状态。JSON键`linked_trim_left/right_*`、`slide_*`。

实施：session在press预计算并登记全部affected clips（linked/followers/邻接）完整preview快照或统一从immutable model重投影；各preview handler经session更新并由一个cancel恢复。恢复源区间、fade、x/y、cache/preview与renderer trim preview，不能只存位置。

验收：linked左/右、Slide带gap/fade/邻居源区间等，取消后每个affected Item与model一致，无Undo/revision；重新点击伙伴/邻片不会把旧preview当original提交。

### RI-03（P1，C09/INT-05残余）：Properties直接/批量Reset绕过轨道锁

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:3057` `UpdateClipParamCommand.apply`没有`assert_track_editable`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:2347`使用此命令；`:3872`多选Reset循环逐片执行。

Q复现：锁V clip Transform X10→输入99，model变99、Undo=true、revision1。跨轨多选Reset，开放c10和锁轨d20**都变0**，revision2；轨锁不拦此入口。菜单/键盘D已有保护，不能推论全部写入口受保护。

实施：在共用领域command/planner apply入口预检全部affected tracks，包含linked partner与批量target；UI根据same allowed/reason禁用控件/Reset，并保留domain拒绝。把Reset做一个macro/atomic计划，批量预检通过后一次commit，任何锁定拒绝整个受影响组。不要仅在菜单上补disabled。

验收：锁轨下Transform/Crop/RAW/Opacity/Speed/Effect/Reset/API/AI逐入口无变更、Undo/revision不推进；选区含一个锁轨target时整体拒绝，未锁target也不半提交；合法2片Reset一步Undo完整恢复两片各自原值。

### RI-04（P1，C07/C08残余）：Properties Speed仍走旧算式，与菜单/linked A/V分叉

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:3076`至`:3085`仍用`int(source_duration / speed)`，不使用本轮`timeline_duration_for_source`，不改linked partner；Properties `edit_properties_panel.py:1668`构造SpeedParams后走该入口。菜单的`ChangeClipSpeedCommand`已改统一helper，这是两个实际入口不同。

Q复现：source30fps120帧、timeline24fps，linked V/A都96帧@1x；Properties Speed输入2后V为60帧（应48）、A仍96帧@1x，双向linked保留。`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_properties_speed.json`键`linked_mixed_fps_speed`。

实施：Properties Speed委托既有ChangeClipSpeedCommand/统一retime planner（速度点积分、FPS、reverse/freeze/lock/linked/fade/animations），取消UpdateClipParamCommand的第二套时长计算；非时长参数如preserve_pitch/interpolation/reverse按明确策略更新，不能无意重算时长。

验收：菜单和Properties同输入结果to_dict一致；30→24/24→48/29.97→23.976、2x/反放/速度点，linked同长同步，一步Undo；preview/export/reload source indices一致，锁伙伴整组拒绝。

### RI-05（P2，INT-13部分修；Reset多条历史为本轮新暴露）：多选有计数标题仍只改primary，Reset不是一步Undo

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/edit_properties_panel.py:2063`只primary构建sections，`:1573`参数路由取首clip；同文件`:1720`只有Transform Reset发全部IDs，其他native Reset仍section逐参数primary；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:2347`单UpdateClipParamCommand，`:3872`Reset逐条命令。

Q复现：两片X10/20、Opacity80%/30%，标题`Properties (2 clips selected)`。输入X99只c99、d20；Opacity50只c50、d30；没有mixed字段显示（控件显示primary10/80）。Reset Transform把两片0，但一次Undo仅d回20、c仍0，栈有剩余历史；Opacity Reset只c回100%。

实施：SelectionContext有stable IDs/type/track/media兼容性；shared参数按每片原snapshot派生new_value，mixed控件保留未改字段；跨片参数和Reset以单macro提交，所有target锁/碰撞前置预检；更新全部panel clips并保留多选上下文。新恢复完整selection不能只靠title计数验收。

验收：2/10片各有不同Transform/Crop/Opacity/Speed，对单一子字段输入和Reset确实写所有兼容片，其他字段保留各自值；mixed标记、选区增减/Undo/Redo刷新正确；一步Undo恢复每片旧值；异类或锁对象有明确scope/reason。

### RI-06（P2，INT-08未闭环）：disabled模型首次加载菜单仍错误

定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:1847` full-load、`:6409` incremental-create均未传`is_enabled=clip.is_enabled`；`ClipItem.__init__`默认True。`timeline_items.py:4931`菜单虽已改读取`_is_enabled`，却读取错误投影。另`timeline_view.py:12718`track state直接覆盖Item enabled，没有结合clip own enabled。

Q复现：模型is_enabled=false，首次load Item=true，菜单显示`Disable Clip`，选择它后model false→true、Undo=true，文案与实际结果相反。不是拿旧bug当当前事实：本轮已重新用Qt右键→构造menu→真实handler证明。

实施：full/incremental/reload传完整clip状态；自身enable和track effective mute/visibility分开，菜单基于canonical clip enabled，绘制可另用effective state；删除不存在字段的修复保留，补投影闭环。

验收：disabled clip保存重载后菜单Enable、禁用视觉正确；Mute/lock/Undo/Redo/full-load/incremental-create不把clip enabled污染；一次点击启用，下一次文案Disable；混合多选状态符合明确policy。

### RI-07（P2，C06/INT-12部分修）：2x trim提交正确，拖中波形/缩略图仍1x源域

定位：同绝对路径`timeline_view.py:8285`/`:8287`、`:8461`至`:8464`、linked`:8307`/`:8476`仍以timeline_delta直接加减source。release`:8520`与`:8711`现用模型helper，因此preview与commit分叉。

Q复现：timeline100帧/source200帧@2x，左裁10timeline帧时preview source110/190，release source120/180；右裁preview source100/190，release100/180。snap开关相同。JSON`left_snapFalse/True`、`right_snapFalse/True`。

实施：preview直接用同一`with_left_edge_source_values`/`with_right_edge_source_values`或统一planner，每个linked对象按自身时基/方向；源upper/lower clamp与commit同一策略。也复查Roll/Ripple/Slide live source预览。

验收：2x/0.5x/reverse/mixedFPS/speed_points在drag每一步的Item source范围与最终command proposal完全一致；snap开/关同帧，release不跳波形/缩略图，取消完整回原源域。

### RI-08（P2，INT-06新锚定实现精度不足）：重复缩放有方向性漂移

定位：同绝对路径`timeline_view.py:2860`用整数`_frame_from_scene_x`，该方法`:4766`向0截断；`:2876`滚动补偿再向0截断。

Q复现：长100000帧timeline避免滚动范围钳制，鼠标x813，hbar800，Ctrl+pixelDelta(0,40)重复5次。ppf0.1下单次误差-4.84/-3.51/-8.07/-2.53/-3.76帧，累计-22.73；ppf0.2累计-11.36帧。ppf3、10也各有单次>1帧。不是端点scroll clamp导致。`properties_wheel.json`键`anchor_*`。

实施：保持连续double anchor_frame，避免floor导致每次丢fraction；scroll补偿用round并保留小数残差；若坚持旧≤1frame门禁，低ppf需布局/viewport支持subpixel origin或transform偏移，而单一integer scrollbar无法普遍保证≤1frame。统一函数供wheel/Zoom工具/slider并保留边界钳制政策。

验收：ppf0.1/0.2/0.5/1/3/10、pixel/angle、±缩放各20次，光标锚定帧无系统漂移；零delta不缩放，horizontal/Shift split-area仍正确；用连续frame测误差，明确低zoom帧/像素容差且不偷偷放宽原验收。

## 3. 非bug：Sync Lock单测契约过时

`tests/edit/test_track_sync_lock.py:171`隔离TrackHeaderItem，已正确断言菜单check False、captured=[('v1',False)]，但`:216`还要求无宿主header立即自改`_is_sync_locked=False`。当前`timeline_items.py:8357`明确只emit请求，由command成功回写，符合.clinerules防止UI/model脱节；该逻辑旧基线已存在，此轮未改。

本轮QTest真实TimelineView右键header+实际menu action+command证据：model/header True→False，Undo双方恢复True。见`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_atomic_reset_sync.json`键`sync_lock_integrated_after/undo`。应更新isolated单测期待header仍True，再加此集成测试；不要把它列为产品新回归或在UI先改状态迎合旧断言。

## 4. 证据清单和重跑

全部副本已归档至新审计目录的 evidence/probes；未覆盖旧审计目录。

- `topos_recheck_legacy_probe1.py/.json` ～ `topos_recheck_legacy_probe5.py/.json`：旧五组probe复制，仅把sys.path根改为当前绝对路径、输出随副本到/tmp。
- `topos_recheck_cancel_matrix.py/.json/.log`：18工具/selection场景×Esc/Tool/Focus三类=54；42组合无模型/visual变化，12组合出现上列问题，0uncaught。
- `topos_recheck_properties_wheel.py/.json`：多选真实widget/handler/command与6zoom×5次锚定。
- `topos_recheck_trim_preview_menu.py/.json`：2x trim snap开关live/commit、disabled menu真实点击、locked keyboard D。
- `topos_recheck_atomic_reset_sync.py/.json`：跨轨锁target Reset写入、header sync lock+Undo集成。
- `topos_recheck_properties_speed.py/.json`：锁轨直接Transform、linked mixedFPS Properties Speed。
- `topos_recheck_source_range.py/.json`：Source范围按钮/桥接、GL AST契约（无有效GL像素验收）。

重跑任意脚本：在项目根执行`.venv/bin/python /tmp/topos_recheck_<name>.py`。脚本自己设置`QT_QPA_PLATFORM=offscreen`。fixture使用missing assets/synthetic timeline以确定几何/命令，未证明全部真实codec/GPU/长时间播放；父agent负责可见窗口和真实素材门禁。

建议排期：先RI-01/02取消完整性、RI-03锁守卫和RI-04统一retime入口；随后RI-05真实多选原子编辑、RI-06状态投影、RI-07 preview time-map、RI-08 anchor。每批只改相关最小入口，先把上述失败复现固化成focused tests，再真实窗口验证，并同步旧计划§0状态为部分完成/已验收，避免仅凭新tests过就标全部关闭。
