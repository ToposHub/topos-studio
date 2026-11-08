# 时间线后端接线独立审查

日期：2026-11-08。审查基线：`6120873..720b875`（含 `18ca026`、`919ecb5`、`720b875`）。本审查只读产品代码，未修改业务逻辑。用户追加要求为“UI/交互问题本轮修复，后端问题留计划”；以下后端项均只提出修复与验收建议。

不能据内部契约或单元测试通过，声称与 DaVinci Resolve 行为完全一致。本审查没有操作 Resolve；真实应用对照由主 Agent 另行完成。

## 执行与证据

现有 focused suite **61 passed / 5.53s**：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_source_destination_wiring.py tests/edit/test_replace_edit_and_voiceover.py tests/edit/test_clip_flags.py tests/edit/test_position_lock_policy.py tests/edit/test_presentation_state_and_levels.py
```

另外编写三个独立探针，均通过产品公开模型/既有 view 或页面槽调用，不调用真实麦克风、不改运行中用户工程、不调用 GPU API。探针是观察脚本，输出异常/状态值；它们不以返回码 0 表示产品通过。

| 探针 | 证据 | 覆盖 |
|---|---|---|
| [topos_backend_review_repro.py](backend_evidence/topos_backend_review_repro.py) | [results.json](backend_evidence/topos_backend_review_results.json) | 混合旗标undo、无音轨Voiceover、linked伙伴速度 |
| [topos_backend_review_secondary.py](backend_evidence/topos_backend_review_secondary.py) | [secondary_results.json](backend_evidence/topos_backend_review_secondary_results.json) | 锁service异常、Delete事件边界、未知展示字段、坏scroll、clear后路由 |
| [topos_backend_review_edges.py](backend_evidence/topos_backend_review_edges.py) | [edges_results.json](backend_evidence/topos_backend_review_edges_results.json) | 蓝徽章显示/消费、冻结帧、速度点、媒体类型、非法Flag |

仓库根目录执行示例（探针中仓库路径为本机绝对路径，换机器需调整）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-08-timeline-review/backend_evidence/topos_backend_review_repro.py
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-08-timeline-review/backend_evidence/topos_backend_review_secondary.py
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-08-timeline-review/backend_evidence/topos_backend_review_edges.py
```

下述行号对应审查基线附近位置，后续 UI 修改可能挪动，以符号为准。

## B01 · P1 · 批量旗标的 undo 会清除原本无需修改的旗标（已复现）

位置：`src/features/edit/core/commands/clip_props_commands.py:2176`、`:2185`、`:2199`、`:2206`，`SetClipFlagCommand.apply/undo`。

复现：c1 原为蓝色 `#4BA6DD`，c2 原为 None；选中两片统一设蓝，再 undo。

- 预期：恢复 `[blue, None]`。
- 实际：apply 只对 c2 记录 `_previous`；undo 遍历全部 `targets`，对不在快照的 c1 使用 `restore.get(c1) == None`，恢复成 `[None, None]`。原有用户标记丢失。

建议：只撤销确实修改并有快照的身份，或为全部目标捕获原值；身份建议保存 `(track_id, clip_id)`。补混合“已是目标色/待变色”、已有多色、清除、redo、序列化后undo测试，核对完整模型快照。

## B02 · P1 · Voiceover 无音轨/蓝音频关闭时读取 str.track_id，创建轨后失败（已复现）

位置：`src/features/edit/ui/edit_page.py:635`、`:639`；返回契约：`timeline_view.py:17011` 的 `ensure_target_audio_track() -> Optional[str]`。

复现：只有视频轨的真实 TimelineView，经 `_on_timeline_voiceover_requested()` 进入 fallback。

- 预期：得到有效音轨ID，进入正确录音设置/启动链；失败不能留下不明局部编辑。
- 实际：`ensure_target_audio_track()` 返回字符串，槽读取 `track.track_id` 抛 `AttributeError("'str' object has no attribute 'track_id'")`；已新建一条音轨，undo history 已增加 1，录音未启动。已有音轨但蓝音频显式关闭，也进入同一错误分支。

建议：修正调用方返回类型，明确“创建轨”和后续录音启动的失败处理/用户可见结果。补无音轨、蓝音频关闭、有锁音轨、设备失败的真实页面槽测试。当前测试只覆盖音轨已存在且蓝路由有效的 fake host。

## B03 · P1 · Replace 只按主片base速度算源窗，速度点与linked伙伴会被截短（已复现）

位置：`src/features/edit/ui/timeline_view.py:17888`、`:17895`、`:17926`–`:17933`，`replace_clip_with_source()`。

复现一：48帧时间线clip，base speed=1，`SpeedPoint(source_offset=16, speed=2)`，原 `source_duration=80`，新源同fps且充足。

- 替换前采样帧 `[0,16,24,32,40,47]` 对应源 `[0,16,32,48,64,78]`。
- 替换后返回 True，但源窗按 `48 * base1x` 改成48，采样变成 `[0,16,32,47,47,47]`，尾段被夹到同一帧，速度曲线虽然“保留”但播放语义损坏。

复现二：24fps时间线中同起点同48帧的 linked 对，主视频1x，音频伙伴2x；换48fps有声新源。

- 实际：主片所需96源帧被原样写给伙伴；伙伴保留2x，其实需要192源帧，最后时间线帧映射被夹到源95，替换仍返回True。
- 预期：逐个成员按完整速度映射求源窗并独立预检；若无法保持链接同步，整次明确拒绝。

建议：统一使用现有速度曲线/窗口时基计算能力，保留连续采样相位，逐成员预检并单次宏提交；新增 mixed FPS × 速度点/反放 × linked伙伴不同速度/不同长度测试。

补充静态风险：`_find_linked_partner()` 只按单向 linked ID 查找，不检验 reciprocal链接、异轨/轨类型及同步边界。对比项目既有 `get_reciprocal_linked_clip()` 与linked trim/roll预检，本入口更宽松。建议同批规定并校验，避免替换被一个悬空/单向关系指到的无关片段。

## B04 · P1 · Replace 保留旧素材绝对冻结帧，结果落在新源窗之外（已复现）

位置：`src/features/edit/core/commands/clip_props_commands.py:2021` 起 `ReplaceClipWithSourceCommand.apply()` 的 `replace()`；`timeline_view.py:17912` 构造命令；`ClipInstance.timeline_to_source_frame()` 的freeze分支。

复现：目标 `freeze_source_frame=5`，替换新素材 In=200，目标48帧。

- 实际：新 `source_start=200`、`source_end=247`，旧freeze仍为5，首时间线帧映射返回5，取新源窗之外内容。
- 预期：冻结意图以明确语义映射到新源，或对不支持情况拒绝；不能声称保留速度/冻结而沿用另一素材的绝对帧引用。

建议：Replace payload和undo快照覆盖冻结帧重映射，确认是否保留相对冻结offset或以新源定位帧作为冻结帧；预检边界并验证预览/导出一致。

## B05 · P1 · Replace 没有目标轨与源流兼容性预检（已复现模型写入；解码后果为静态判断）

位置：`src/features/edit/ui/timeline_view.py:17863` 起，asset存在后没有 media/stream兼容性判定；命令 `clip_props_commands.py:2021` 只换asset/源窗。

复现：选择视频轨clip，传入 `media_type='audio'` 的真实 MediaAsset，源帧数足够，调用Replace。

- 实际：返回True，视频轨clip的asset_id已变成纯音频素材。静态上视频解码消费者没有视频流可用；本探针未启动实际解码，不能把黑帧作为已实测现象。
- 预期：不支持的流类型组合应在提交前拒绝；如产品允许指定仅替换音频伙伴，必须显式选择并构建该事务。

建议：按成员目标轨类型与源实际流预检；纯音频/无声视频/image/compound/generator分别明确支持矩阵，缺流整次拒绝、零历史。底层新增命令也不能只依赖UI传参合法。

## B06 · P2 · Position Lock service预检绕开错误归一与revision优先级（已复现）

位置：`src/features/edit/ai_editing/edit_project_service.py:217`–`:237`；`src/features/edit/core/position_lock.py:138`–`:140`。

复现：当前revision=0，base_revision=999，命令移动不存在的clip，比较有/无 `position_locked_clip_ids={'c1'}`。

- 无策略：返回 `CommitResult(applied=False, error='Revision conflict...')`。
- 有策略：先执行dry-run，在乐观锁检查/命令apply异常归一之前抛 `ValueError('Clip not found: missing')`，没有CommitResult。

建议：先做revision检查；在同一受控apply边界计算proposed一次，再对before/proposed做footprint diff，避免锁策略多执行一次命令。失败统一CommitResult，验证两层状态、历史、command快照均不泄露部分结果。补 stale plan、非法命令、宏中后半失败、空锁集合用例。

另有界限：当前service只在调用方显式传locked IDs时生效；默认None仍是无锁。未看到UI锁状态自动注入所有AI/transaction调用方的实现，不应把“service支持参数”写成“所有会话/客户端已自动一致”。后续按个人防误操作的明确策略来源接线。

## B07 · P2 · 展示偏好实际保存链丢弃未知字段和version（已复现）

位置：`src/features/edit/ui/timeline_view.py:4479`（export构造新dict）、`:4495`（apply只消费已知字段）；`timeline_panel.py:1025`；`src/app/controllers/file_controller.py:1453` 起重新创建UIState并使用panel export。

复现payload含 `future_field={'keep':True}`、`version=2` 和路由中的未知future_stream；apply后再export。

- 实际：上述字段全部消失。UIState直接from_dict/to_dict的小测试虽然保留字典，但真实load→panel恢复→FileController重新收集链不会复用那份字典。当前export也根本没有版本字段。
- 预期：保留未知字段以便未来工程重存；真正的versioned payload应写版本并处理未知版本语义。

建议：宿主保留presentation原始payload，已知键更新、未知键保留；定义version及嵌套路由扩展规则。补真实FileController收集/恢复往返测试，而非仅UIState构造测试。此项只涉及保存/兼容计划，本审查未改产品代码。

## B08 · P2 · Flag新写入命令不执行白名单/hex校验（已复现）

位置：`src/features/edit/core/commands/clip_props_commands.py:2160`–`:2163`。注释宣称模型/命令/UI同一白名单，但命令只验证字符串以#开头且长度7。

复现：`SetClipFlagCommand.create((('v1','c'),), '#zzzzzz').apply(sequence)` 成功，模型写入非法值。

建议：区分“读取未知历史值保留”和“新用户/AI命令写入合法值”。新写入调用统一validator，校验7色或明确支持的规范hex；from_dict不能作为绕过入口。补非法hex、合法非白名单色、未知历史值重存、清除兼容测试。

## B09 · P2 · 项目切换没有清理presentation路由上下文（已复现clear残留，跨工程后果为静态推导）

位置：`src/features/edit/ui/timeline_view.py:1845` 的 `set_project()` 只清历史，不清 `_source_patch_routes`；`clear()`亦保留；`apply_presentation_state()`对routes做merge。FileController仅在新工程有payload时调用apply。

复现：timeline ID='lock' 中关闭蓝视频路由→clear→再次load相同ID，`source_destination_stream_disabled('video')` 仍True。

静态后果：打开来自同一模板/工程副本、沿用timeline ID但没有presentation的另一个project时，可能继承旧工程的禁用路由；路由现已影响真实落轨，所以不再只是外观残留。新payload缺失的thumbnail/lock也不会自动恢复默认；究竟跨工程继承个人默认还是项目覆盖，需要明确归属。

建议：按project/timeline上下文隔离并在宿主切换时完整替换状态；旧包缺字段使用已定义默认。补A→B(无payload)→A，以及同timeline ID不同project ID测试；确认每次保存只包含当前工程上下文。

## B10 · P2 · Voiceover“正确轨道录制”的完成链仍只有素材导入（静态确认，未录真实设备）

位置：新增 `src/features/edit/ui/edit_page.py:631`–`:650` 调用既有 `_start_recording()`；`src/app/controllers/edit_controller.py:3221`–`:3223` 回调仅捕获project ID和track ID；`:3285`–`:3390` 的 `_on_take_recorded()` 导入asset、add_take_entry、loop续录，没有新增timeline clip命令。

这条既有链可作为ADR素材采集入口，但不能直接等价为“Voiceover已在选定轨道/播放头位置落clip”。当前没有timeline ID绑定的完成事务，且“录制中换timeline/删轨”没有新接线保证；同project内回调只校验project ID。此项尚未真实设备复现，请实施前用可控RecordingManager替身模拟take完成，并检查序列clip集合。

建议：若产品只要求打开ADR素材采集，按钮/计划明确限定；若承诺Voiceover时间线录制，则建立绑定project/timeline/track/anchor/session的录音完成事务，asset导入+clip落轨单次undo，完成回调重复/迟到/切工程/删轨均可验证且可取消。不要仅凭fake `_start_recording` 被调用一次判完成。

## 已交给主 Agent 的UI/交互项（不算后端修复）

这些问题属于本轮可直接修复范围；此报告记录初始观察，修复状态以主 Agent 最终回归为准。

| ID | 观察/证据 | 位置 | 建议 |
|---|---|---|---|
| U01 | Position Lock开启时真实 `keyPressEvent(Delete)` 向事件边界抛PositionLockViolation，零写入但未捕获；由secondary探针复现 | `timeline_view.py:10791` 附近 `_delete_selected_clips()` 的 `_execute_command()`；同类menu/paste也需审视 | UI handler捕获策略拒绝并正常显示状态，不让异常离开Qt事件处理；不可简单吞掉领域异常而假报成功 |
| U02 | 新视图无蓝显式路由，红target设v2：蓝徽章painted_source=v1、effective_source=v2；edges探针确认 | `timeline_view.py:4556` 与`:4591` 不同fallback | 让徽章显示有效路由；红target变化时同步投影，显式蓝选择继续独立 |
| U03 | `custom_pixels_per_frame=2, custom_scroll='garbage'`直接ValueError，可能使后续Tray状态恢复中断 | `timeline_panel.py:1050` | 逐字段安全校验，拒绝非有限ppf/非法scroll，不让一个坏字段阻断其余偏好 |
| U04 | Tray用118px gutter作frame0，时间线header宽220，坐标不同；Esc/Delete焦点及关闭也需真实事件验证 | `src/features/edit/ui/edit_keyframe_tray.py:135` 起坐标方法 | 共享view帧坐标映射和真实焦点/关闭生命周期；本项初始静态提示，非本报告后端已复现结论 |

## 建议修复分批及补测

1. 后端第一批：B01旗标undo、B02Voiceover返回契约、B03–B05 Replace源窗/成员/流预检。这些影响普通用户数据或结果，不宜继续把W01/W04/W05写成“全部完成”。
2. 后端第二批：B06统一提交错误契约、B07/B09状态隔离与兼容、B08命令校验。补真实host保存往返，不只测小对象。
3. Voiceover验收批：明确B10能力边界，补录音完成事务替身测试和真实设备smoke；不得使用假电平表来证明录音成功。
4. 保留现有61项测试；追加以上反例并使其失败后再修复。最后继续验证取消/失败时零内容修改、单步undo完整恢复、preview/export采样一致。

本报告没有将内部契约里的“源In作为Replace对齐帧”认定为Resolve一致行为；其真实定位帧语义、蓝/红patch的Resolve交互及Position Lock原生语义，应由实际Resolve对照另行决定。

## B17 · P1 · 变速撤销无法精确恢复动画关键帧（补充复现）

位置：`core/commands/clip_props_commands.py:ChangeClipSpeedCommand.apply/undo`。

[独立模型探针](backend_evidence/speed_undo_probe.py)与[输出](backend_evidence/speed_undo_results.json)：100帧的互链A/V片段，视频opacity关键帧在1，音频opacity关键帧在40。2x变速后分别到0与20；undo后仍为0与20，未恢复1与40。没有操作真实用户工程。

主片撤销用反向比例重映射，整数舍入不可逆；链接伙伴的undo分支未重映射关键帧。命令也未保存完整动画快照，因此点碰撞、handles、效果动画等还需要专项验证。

建议：保存完整before clip/animation快照，或等价完整动画状态；undo直接还原，禁止用反算代替原始数据。覆盖相邻关键帧压缩碰撞、不同linked时长、clip/effect动画、handles及extra、命令序列化、undo/redo与曲线采样。本轮不修改此命令。新的变速对话框对带动画片段统一发高级设置请求并提示未执行，等待后端安全事务。

## B18 · P1 · 比例拉伸忽略源/时间线帧率，并重置倒放与保调（补充复现）

位置：`ui/timeline_view.py:_on_rate_stretch_finished`。代码位于UI文件，但这一段承担真实变速时基计算和命令参数编排，按用户边界只记录后续计划。

[真实View槽探针](backend_evidence/rate_stretch_probe.py)和[输出](backend_evidence/rate_stretch_results.json)：48fps源200帧放在24fps时间线100帧片段，原倒放与preserve_pitch均True。要求比例缩短到50时间线帧，预期2x，实际按200/50得到4x，命令提交成25帧；同时float参数构造全新SpeedParams，reverse/preserve_pitch均变False。

建议：统一按完整速度积分、源/时间线时基和当前持续时间求目标速度，保留原SpeedParams非目标字段；复杂速度点/冻结/动画必须能力预检，不能偷偷退成常速。检查left/right、混fps、不同linked伙伴、缩放预览/释放一致、取消、无变化、undo；验收必须比较用户目标时长与最终时长。本轮不修改这段计算。

## B19 · P1 · 数值/键盘联动trim的两成员delta分别钳制（补充复现）

位置：`ui/timeline_view.py:_on_clip_numeric_trim_requested`、`_apply_step_trim`编排，底层`core/timeline_ops.py:build_numeric_trim_command`按单片计算边界。

[独立模型探针](backend_evidence/linked_numeric_trim_probe.py)和[输出](backend_evidence/linked_numeric_trim_results.json)：互链片段均100时间线帧，源总长1000；视频source_start890余10，音频source_start897余3。请求右侧ripple+5，单片构建器分别钳为+5/+3；原宏编排不比较返回delta，提交后时长105/103，失去同步。

本轮只在两条UI入口补提交前拒绝：两成员delta不一致时提示并零提交，未修改单片构建器/命令，未自行决定全组钳到3的策略。后端仍需共同边界规划、同delta预检与原子提交，覆盖不同fps/source phase/速度、sync-lock跟随、AI/service入口与单步undo。不能以UI拒绝代替后端统一事务修复。
