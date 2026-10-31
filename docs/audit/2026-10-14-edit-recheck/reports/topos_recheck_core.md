# Edit core 已修复批次只读复查

版本：基线 55351a5f45d5a34ff2fc91a3cb9eee53581a889b → HEAD 833dcd382；根目录 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。遵循 AGENTS.md 和 .clinerules；模型与命令为真实实现，合成时间域 fixture。独立复现脚本不创建 QWidget/QApplication、不调用 GPU；没有修改生产代码。补充 pytest 包含未显示的 Qt dialog/qapp fixture，不作为可见窗口操作验收。旧计划 §0 实施状态作为待验证声明，未把正文旧结论当作当前缺陷。

证据：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_core_old_repro.out`：原 core_repro.py 重跑。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_core_repro.py` + `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_core_repro.out`：本轮组合复现（直接 `.venv/bin/python /Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_core_repro.py`）。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_core_pytest.out`：8 个 focused 文件 **150 passed in 2.26s**。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_core_pytest_extra.out`：另6文件 **91 passed in 3.22s**，验证linked/ripple/sync/revision既有契约；包含未显示的Qt对象fixture。
- 本报告所称“JSON roundtrip”仅测试公开命令 `to_dict/from_dict` 契约，不声称当前工程保存文件持久化完整 Undo 历史。

## 原问题状态复核

| 旧 ID | 本轮状态 | 已验证改善与剩余边界 |
|---|---|---|
| C01 | 部分修复；新增组合回归 | 向左 Roll 基本 undo 成功、完整 clip 几何恢复；带 seam 转场的 Roll 会删转场，undo 仍丢（K04） |
| C02 | 部分修复 | Insert 100帧中间插10 Undo恢复原单clip，redoID稳定；旧 slice 仍反放/FPS/ramp不感知，目标轨动画/marker不跟随（K03/K08） |
| C03 | 部分修复；新回归 | 原50→30→Undo50、30→10实证改善；新累计 helper 移动锁轨（K01） |
| C04 | 原例已修 | 有/无FPS元数据 reverse 首3帧都是199/198/197 |
| C05 | 部分修复 | 等FPS1x反放、简单整数阶跃切分原例不变；24组合20组切分后逐帧不同（K07） |
| C06/INT-12 | 部分修复 | 2x普通右trim release正确80/160；reverse右trim新显式源入点失效、variable左trim不重定位速度点（K02/K07） |
| C07/FE-B04 | 部分修复；新组合回归 | 混合FPS常速变速120src30fps→24fps2x正确48；Insert跨轨delta不一致、ramp时长/mapper分叉（K03/K06） |
| C08 | UI有改善，领域统一仍未完成 | 原直接 RippleTrim仍V80/A100；新UI构造Macro明确linked-selection开启才trim伙伴，关闭时独立，不能将独立核心cmd结果单独判bug。原FakeView缺新属性报错是旧fixture过期，不是应用异常 |
| C09/INT-05 | 部分修复 | Speed伙伴锁定拒绝；新增守卫仅Split/Toggle/Link/Speed，其他核心命令依旧绕锁（K01） |
| C10 | 部分修复；新增路径未覆盖 | 默认righttrim及Split原40→20不再崩；显式lefttrim和Speed5仍崩，普通trim Undo不还fade（K05） |
| C11 | 部分修复 | Split/DeleteClip原live Undo转场1→1→1、1→0→1；Roll/宏中间态和新增payload持久化未覆盖（K04/K09） |
| C12 | 部分修复；新回归 | 单clip move头转场锚正确200，validator空；Roll/整体Move仍删本应保留转场（K04） |
| C13 | 部分修复 | Move原首帧动画随动正确；Insert和ripple目标轨followers仍留旧绝对keys（K08） |
| C14 | live路径已修，序列化契约部分修 | gap与fade的Slide live Undo完整恢复；JSONroundtrip落回旧逆操作，gap变形（K09） |
| C15 | 部分修复 | marker75归右半正确；marker恰切点50仍归左半非法、序列化Undo孤儿（K10/K09） |
| C16 | 原跨轨marker例已修 | 50→40，marker55→45；新的sync helpers keys/marker同移，目标轨Insert与ripple key边界尚漏（K08） |
| C17 | focused guard通过，未声明穷尽 | 现有source上界focused文件通过；反放/变速域完整planner仍是K02/K07范围 |
| R01/R02/R03 | focused防护有效但非完整事务 | Split before_clips深拷贝、ClipInstance未知字段roundtrip、seam/link/markervalidator通过；不代表所有命令完整payload/validate或history无缺口 |

## 当前可复现问题与后续验收

### K01 · P1 · 锁守卫不完整，C03累计跟随新增锁轨回归

精确位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:722–726`，新 `shift_sync_lock_followers_merged` 只过滤 exclude/sync-lock，漏掉旧helper `:620–621` 的 `is_locked`。命令直接写入口：`commands.py:510` Move、`:1733` Trim、`:337` Delete、`:12774` Insert、`:13069` Overwrite、`:13636` RippleTrim。统一guard定义 `commands.py:137`，只在4种命令加入。

复现：视频d1[0,10)、d2[20,30)；音轨locked=True/sync_locked=True，clip[100,200)。BatchDelete两视频，ripple=True。

输出：`locked audio start before/after: 100 80`。另锁定视频单轨上的Move/Trim/Delete/Insert/Overwrite/RippleTrim六类均`accepted changed: True`。首项相对旧helper是新回归；后六项属C09未完成，脚本/AI和其他提交入口不能依赖UI菜单守卫。

修复：在命令/领域planner预检完整 affected tracks 与reciprocal单位；明确锁定sync轨policy（既有skip策略或整组拒绝），累计helper遵循相同策略。Undo不因后来锁状态拒绝合法历史。

验收：上述六类+Batch/Slip/Roll/Slide/Macro/效果参数全写入口，主轨/伙伴/被解链旧伙伴/sync轨各锁组合；拒绝或skip必须无被锁内容变动、无半提交、无history/revision新增。此fixture锁音轨100保持100。

### K02 · P1 · reverse右trim忽略新source_start，显式成功后Undo也不还原

位置：`commands.py:1794–1809` 右trim要求new_timeline_start与new_source_start同时存在才使用新源入点；`create_right_trim :1971–1981`只设new_source_start，不携new_timeline_start；`:1872–1876`右undo只恢复duration，漏source_start。真实普通右trim调用 `timeline_view.py:8799–8808`正是该factory。

复现：reverse1x clip source[100,200)、timeline[0,100)，右缘缩到90。helper返回source[110,200)，factory执行。

输出：`helper start/duration: 110 90 actual: 100 90`，首3帧`[199,198,197]→[189,188,187]`；另直接replace命令携new_timeline_start=0可apply source110，undo仍110，预期100。

修复：right-edge trim直接以原clip.timeline_start保持时间线入点，明确新source_start可选值；before/after完整clip快照，Undo恢复整个源区间。

验收：reverse常速/FPS/ramp、左右缩/伸、factory+UI+API、两侧linked宏；保留帧逐帧不变；连续Undo/Redo完整payload一致。

### K03 · P1 · mixedFPS Insert跨轨波纹delta与实际插入时长分叉

位置：`commands.py:12815–12817`仍 `int(source_duration/self.speed)`，`:12854`传给sync followers；同方法调用的 `timeline_ops.py:1559`已经统一FPS求出实际duration。

复现：目标视频follower100，另一sync音轨follower100；在0 Insert源30帧/30fps、timeline24fps、speed1。

输出：`inserted duration/ripple_offset: 24 30`，`video/audio follower starts: 124 130 expected: 124 124`。这是新时长修复未同步跨轨路径的组合回归。

修复：从同一placement plan/实际新clip.timeline_duration获取delta，目标轨与每sync轨同一delta，Undo/Redo复用计划。

验收：30→24、24→30、30000/1001、0.5/1.25/2、最小单帧和ramp；全轨followers同移，目标与cross-track不因round/int差异分叉。

### K04 · P1 · 转场自动重锚在多对象中间态删内容，Roll Undo无法恢复

位置：`timeline_v2.py:2573–2574`每次clip更新立即reanchor，`:2603–2606`不相接即删；`timeline_ops.py:2124–2129` Roll顺序更新左右片；`commands.py:13515–13536` Roll Undo只有clip快照，无transition快照。Macro `commands.py:10685–10698`同样顺序apply。

复现：A[0,100)、B[100,200)，双边dissolve seam100 duration20。Roll-10；另一case Macro MoveA到200、MoveB到300，final仍相接。

输出：Roll转场数`1→0→undo0`，full Undo false；整体Move最终A[200,300)、B[300,400)却transitions0。本轮新reanchor策略导致中间态删除，是新回归。

修复：事务先规划全部clip几何，用clips+原transitions一次重建，再在最终快照重锚一次；Roll/宏保留before/after引用及参数；合法整体平移保持转场身份与duration，不把暂时gap解释为用户删除。

验收：带头尾/双边/音频转场的Roll±、Slide、同轨整体Move、linked/sync宏；apply保留合法转场且validator空，undo完整to_dict，至少三循环与JSONroundtrip。

### K05 · P1 · fade修复绕过新显式trim与speed入口，Undo丢被钳制字段

位置：`commands.py:1778–1784`显式left raw replace绕过fade clamp；`:1799–1805`显式right同风险；`timeline_v2.py:694–704`with_speed_params raw replace，ChangeSpeed`:16749`先走它，后续with_timeline_duration来不及clamp；`commands.py:1859–1876`trim Undo只还几何不回fade/transition。

复现1：clip100 fade_out40，普通lefttrim到start80/newduration20，显式new_source_duration20；真实UI `timeline_view.py:8610–8619`始终传此值。输出`ValueError: fade_out_duration (40) exceeds clip duration (20)`。

复现2：同clip ChangeSpeed5x，也相同ValueError。

复现3：clip100 fade40 + head transition40，defaultrighttrim到20然后Undo。输出fade`40→20→20`；transition.duration`40→20→20`，full Undo false。

修复：所有clip缩短路径经单一source/time slice+fade策略构造，避免raw replace中间非法实例；Trim/Speed保存完整before/after，Undo回fade envelope与transition params而非从post-state逆推。

验收：带fade超过保留时长，left/right/speed/ripple/roll/slide/split；所有合法缩短不崩，Undo逐字段还原，redo不累计钳制，UI+领域一致。

### K06 · P1 · 变速点mixedFPS与smooth ramp时長/映射不一致

位置：`timeline_v2.py:406–445`有speed_points故绕rational mapper，fallback直接传timeline relative给SpeedParams忽略FPS；`timeline_ops.py:905–913`新FPS分段积分只取constant `_segments`，无smooth积分，且分段统一round与SpeedParams每段round不同。

复现1：source120/30fps，timeline24fps，base1，offset60后2x；helper duration72。第71帧正确播放offset应约117，实际offset82，最后5源帧174/176/178/180/182，只播放源区间100–219的一部分。

复现2：base1、offset60→2x easing=smooth、source120，24/24元数据。新helper90，SpeedParams authoritative duration102；ChangeSpeed新路径将smooth曲线错裁短。

修复：source秒/sequenceticks/曲线积分与正反映射共用time-map；smooth从同一精确积分取duration，不用constant segments冒充。中间分段不要重复round；舍入只在最终timeline边界。

验收：step/smooth、24→30/30→24/fractional、reverse、preview/export/mapping互一致；首尾cover期望source，duration与积分一致；没有有/无元数据导致的曲线变化。

### K07 · P1 · slice保帧验收仍缺分数速度/曲线/FPS；其他修剪入口继续旧公式

位置：`commands.py:2323–2390`Split用整数源帧定slice与rebasepoint，无法保留分数采样phase，也改了smooth曲线域；`timeline_ops.py:1588–1608`Insert中间split仍用base effective_speed；Overwrite`:1737–1781`同类；Roll`:2031–2032`/`:2094–2100`仅base delta且假设正向；`timeline_v2.py:603–608`新lefthelper同basefactor，没有slice曲线重定位。

复现：脚本split矩阵3FPS×2方向×4速度，**24组中20组逐帧source不一致**。包括无FPS0.5x source120 timeline240，cut121，frame120原160变159（切点前一帧也错）；1.25x cut49，frame52原165变164；smooth source120 point40→2x，cut49，51帧不同。MixedFPS step切分甚至切点前frame3就由103变104，因为left子片失去speedpoint后切换另一mapper。

其他入口复现：reverse[100,200)在40 Insert10，保留内容首3帧原199/198/197变139/138/137；reverse Roll+10 A首帧199变209、B保留timeline110原189变199。step clip75/source100 offset50→2x，lefttrim20后点offset仍50，timeline60原170变160、70原190变170。

修复：落实DOMAIN-01统一slice planner，保存原time-map窗口、分数phase和smooth控制域；Split/Insert/Overwrite/Trim/Ripple/Roll/Slide共用，方向由model API识别。需要schema字段时版本迁移，禁止每入口单独复制公式。

验收：以本脚本24组合逐帧比较全部保留帧，另切在重复采样/phase/曲线段中/点边界，各入口保留source samples一致。不能仅检查range/duration或等FPS整数1x原例。

### K08 · P1/P2 · 目标轨followers未使用动画/marker共同time-transform

位置：`timeline_ops.py:1620–1625`Insert目标轨只with_timeline_start；markers没有target-track迁移、cross-point归属；Ripple/delete目标轨动画移动同样旧with_timeline_start（delete_clip→move_clip），新C16 helper仅对其余sync轨完善，`:2703`/`:2794`RippleTrim也只改timeline_start。

复现1：follower[100,200)，keys100=0/199=1，marker110；在0 Insert20。输出start120，marker110，keys仍100/199；局部首帧opacity0→0.2020202；validator `Marker m frame 110 outside owning clip follower range [120,220)`。

复现2：删除目标轨[0,20)波纹，follower100→80，marker110→90（这部分旧move helper正确），keys仍100/199；local10 opacity原0.1010101变0.0。

修复：所有移动集合经同一clip/effectkeys/scopedmarker/transition transform；slice的marker归属和clip-ID也进入before/after。避免只修sync followers而漏编辑轨。

验收：Insert/RippleDelete/Batch/RippleTrim/Slide多轨有keys/effectkeys/markers，局部曲线值保留；全流程validator空；Undo/Redo和save/load一致。

### K09 · P1/P2 · 新Undo快照未纳入命令JSON载荷

位置：`commands.py:632–654`Move漏_before_transitions；`:11105–11150`RippleDelete、`:11746–11787`BatchDelete漏_before_transitions_by_track；`:11388–11418`Slide漏_before/after_clips/transitions；`:2583–2620`Split漏_before_markers。注：DeleteClip的新transition载荷成功，不能泛化称所有command失败。

复现：apply→`json.loads(json.dumps(cmd.to_dict()))`→`type.from_dict`→undo。

输出：RippleDelete/BatchDelete headtrans`1→0→loadedundo0`；Move分离seam后loadedundo trans0；Slide原ranges[(0,90),(100,90),(200,100)]→loadedundo[(0,100),(100,90),(190,110)]；Split marker75移右后loadedundo恢复原单clip却marker仍指已删rightID，validator报non-existent。

修复：新增快照字段完整roundtrip（None与空tuple必须区分），old-version数据明确fallback/compatibilitypolicy；快照深拷贝嵌套字段，before与after同语义，不悄悄退回旧逆操作。

验收：所有新字段，real JSON roundtrip后的apply/Undo/Redo完整to_dict等于live路径；含empty track/empty transitions、unknown payload、nested Macro、标记/链接/曲线/转场参数。

### K10 · P2 · marker恰好位于切点时归错左半

位置：`commands.py:2467–2470`使用`<=`，而clip为半开区间。validator `timeline_ops.py:1179`按[start,end)检查。

复现：clip[0,100)，scoped marker.frame50，Split50。marker owner仍c左半[0,50)，右半从50开始。输出validator `Marker m frame 50 outside owning clip c range [0,50)`。

修复：用`frame < split_frame`归左，seam归右；同政策用于Insert/Overwrite等slice planner，并保存original归属。

验收：seam-1/seam/seam+1，reverse/mixedFPS、三次split/Undo，marker永远落合法owner且移动跟随正确段。

### K11 · P1/P2 · source→timeline逆映射未纳入FPS，影响字幕/源标记映射

位置：`timeline_v2.py:464–471`只除speed；调用方`ai_editing/subtitle_placement.py:63–64`、`ui/edit_page.py:2992`、`timeline_preview_renderer.py:3450`。

复现：clip source[100,200)、source30fps/timeline24fps、timeline80。source160应timeline48；输出inverse60，再正映射得到175（不再是160）。

修复：正/反映射统一FPS与curve/phase/PTS边界规则；逆映射明确定义有many-to-one帧采样时取nearest/first等策略。

验收：等/混合/fractionalFPS、reverse/speed/ramp；源字幕/日志/marker定位时间误差≤1timelineframe，正反可逆规则与采样策略一致。

### K12 · P2 · no-op命令依旧清Redo并写历史，APP-01不完整

位置：`commands.py:4129–4140`不检测no-op即append与clearredo。Move新no-op已返回原timeline（`:540–542`），UI仅bump函数检测同一性，不会防止CommandStack改历史。

复现：Move0→200；Undo0，此时Redo=True/history1；执行Move0→0返回同一timeline。

输出：`same timeline: True redo after: False history: 1`。没有内容变化却丢之前redo操作。此缺陷是旧栈行为，但新APP-01的no-op/rejected承诺未完成。

修复：命令stack在成功得到内容差异后才push和clearredo；统一提交adapter具备proposed snapshot、updatedcommand、no-op判定，history mutation发生于commit成功后。模型命令还应约定无变化返回原实例。

验收：Move原位/Toggle同状态/空Macro/trim原边界等no-op，不增history/revision、不清redo；拒绝/失败同样保留历史。内容变更macro只增一次，Undo/Redo单调revision由commitadapter处理。

## 建议批次

1. 回归止血：K01锁轨累计helper；K02righttrim/undo；K05fade路径；K04最终态转场事务。每项先补当前失败组合再实现，禁止仅扩大原例assert。
2. 单一时域基础：K06/K07/K11 unifiedtime-map/slice planner，明确source/timeline/FPS/phase/ramp/PTS语义，再让全入口委托；需要schema变更时一次迁移。
3. 关联状态与payload：K03/K08/K09/K10 follower整体transform+完整before/after+JSON契约；Macro最终态validate一次。
4. 提交历史：K12与主窗口/AI/service统一commit adapter一起完成；no-op/failed/rejected维持history与revision。

风险边界：本报告没有真实播放器像素/耳听/4K60/持久化故障验收。所有映射问题已在authoritativemodel逐帧复现，不能因没有GUI而降低为静态怀疑。新计划应将§0“已实施”更新为“基本原例通过，组合仍待修”的各对应ID，并把上述证据矩阵作为门禁。
