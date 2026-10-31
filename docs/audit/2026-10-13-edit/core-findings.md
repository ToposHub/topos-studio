# 剪辑页核心与命令边界审计（2026-10-13）

审计根目录：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。只读审计；未修改生产代码、未启动 QApplication / GUI、未操作 GPU。

已读取根 `AGENTS.md` / `.clinerules`。范围：不可变 `TimelineSequence` / `EditTrack` / `ClipInstance` → `timeline_ops` 纯运算 → `commands.py` 的命令与 undo → `TimelineView` 的实际 release handler。最终命令由 `TimelineView._execute_command()` 交给 `CommandStack`，然后增量刷新与 timeline_changed 提交；UI release handler 中仍存在 source/timeline 单位混用以及链接规则不一致。

验证资料：
- `/tmp/topos_core_repro.py`：独立复现脚本，使用真实模型、命令栈；两项用实际 UI handler 配合轻量 FakeView，不构造窗口。
- `/tmp/topos_core_repro.out`：脚本完整输出。
- 现有 focused 测试：`.venv/bin/python -m pytest -q tests/edit/test_timeline_ops.py tests/edit/test_timeline_command_regressions.py tests/edit/test_timeline_clip_speed.py tests/edit/test_timeline_link_integrity.py tests/edit/test_timeline_linked_av_regressions.py tests/edit/test_timeline_ripple_delete.py --disable-warnings --maxfail=5`，**133 passed in 1.67s**。
- 现有测试全过不代表下列组合场景正常；所有复现均基于本次工作树代码，不抄旧审计。

## 已复现的问题与优化计划

### C01 · P1 · 向左 Roll 后无法撤销

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:13082`（`RollTrimCommand.undo`）。
- 触发：A=[0,100)、B=[100,200)，Roll -10 → A=[0,90)、B=[90,200)；撤销先把 A 恢复到100，撞上尚在90的B。
- 本次实测：`ValueError: Updated clip overlaps with b`。CommandStack 已回填 undo 栈，命令仍在但无法成功撤销；更早历史被阻塞。
- 实施：用保存的左右 clip 快照建立 replacement map，一次 `track._rebuild(clips=...)` 原子恢复，并将 seam 转场与 clip-scoped marker 一并恢复；避免通过顺序调整掩盖其他组合问题。apply/undo 都保存确定的 before/after 事务载荷。
- 验收：Roll ±1/±10，贴接/有间隙、speed 0.5/2、带fade/转场、三次 undo/redo；比较完整 `to_dict()`，不能只比较实例ID。撤销不得抛错。

### C02 · P1 · 在现有片段中间 Insert 后撤销留下洞与残片

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:12393`（affected_clip_ids 只包括起点>=playhead）、`:12458`（undo）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:1311`（已增加跨播放头切分但未补 undo 快照）。
- 触发：原A=[0,100)，在40插入10帧；apply A-left=[0,40)、new=[40,50)、A-right=[50,110)。
- 本次实测：undo变为 A-left=[0,40)、A-right=[50,110)，不是原单一A=[0,100)；40–50洞永久保留。
- 实施：Insert 命令保存目标轨完整 before/after（或覆盖所有切分/移位/转场/标记/链接的差集）；生成的左右身份只生成一次，redo复用。undo删除created IDs，恢复原跨点clip，原子还原followers。source/FPS映射调用统一slice服务。补 linked partner 切分策略，防止左右残片仍指向同一音频。
- 验收：播放头在头/尾/中间、不同素材、混合fps/倍速/倒放、已有下游clip/转场/marker；完整恢复clip身份与时间范围，redo稳定不多生残片。

### C03 · P1 · 多个不连续片段批量波纹删除时跨轨不同步，撤销仍漂移

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:11251`（逐原始 span 处理）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:668`（`restore_sync_lock_shifts` 用最后old_start覆盖第一次快照）。
- 触发1：删除V[0,10)、V[20,30)，另一同步锁音轨clip起点30。第一事件将30→20，第二仍拿原始阈值30判断，跳过该clip。
- 本次实测1：音频30→20；正确累计位移为30→10。
- 触发2：同上，音频clip起点50。apply两次移动50→40→30，记录两条 old_start(50、40)；undo按dict最后值回填。
- 本次实测2：50→30→undo40；应恢复50。
- 实施：先合并所有原始删除跨度，在**原坐标系**为每个clip求累计delta，一次提交；一条clip只存一份最初before+最终after。若保留事件循环，阈值必须扣减此前已删时长，undo反序应用每条snapshot而非最后值覆盖。跨轨冲突在提交前统一校验，不独立静默clamp。
- 验收：两个/三个离散跨度、首个后方/第二跨度结尾/所有跨度之后、linked A/V多选、轨道锁/同步锁组合；每条轨累计平移正确，一步undo完全回原位置。

### C04 · P1 · 有FPS元数据时倒放被忽略

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:415`–`:427`。
- 触发：SpeedParams(speed=1, reverse=True) 且 `_source_fps_num`、`_timeline_fps_num` 有值。`effective_speed()`永远为正，FrameMapRequest.reverse却取`spd < 0`。
- 本次实测：source=[100,199]，无元数据前3帧[199,198,197]，有24/24元数据变[100,101,102]。
- 实施：reverse从SpeedParams.reverse显式读取，数值speed路径保留兼容；mapper不从正速度猜方向。source_to_timeline逆映射同步通过相同有理数/FPS/反向逻辑。
- 验收：24/24、30/24、24000/1001、VFR PTS、reverse 0.5/1/2；播放/擦洗/导出同帧索引；序列化重载后不变。

### C05 · P1 · Blade切分倒放和变量速度片段会改变未编辑内容

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2195`–`:2234`；纯ops仍在 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:155` 使用`duration*effective_speed`。
- 倒放本次实测：原source100–199倒放前3帧[199,198,197]；在40切分后变[158,157,156]，切点附近跳到199，尾部连续重复159。原因：按正向source_start + left_source_duration分配两个区间。
- 变量速度本次实测：source offset50后2x、总timeline75；在60切分，原60..64=[170,172,174,176,178]，切后=[170,171,172,173,174]。右段继承原offset50速度点和base1，没有将局部起点/新base重定位。
- 实施：新增direction-aware `slice_clip_at_timeline_range` 领域服务：对原source播放方向切分，反向左段取高端source、右段取低端source；speed_points按播放方向切成两组，右段base取切点所在segment速度，保留point easing并减去其source offset；普通FPS通过Fraction/PTS统一边界换算。所有blade、insert、overwrite、range-lift/extract、freeze都复用该服务。
- 验收：操作前后除片段身份外，逐帧source索引序列完全相同；含0.5x、2x、倒放、混合FPS、分段/斜坡、极短source/冻结帧，且undo/redo完整恢复。

### C06 · P1 · 普通拖边裁剪把时间线帧数当素材帧数

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_items.py:5513`、`:5529`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:8294`、`:8475`，preview还有`:8239`。
- 触发：任何非1x或素材FPS≠sequence FPS；ClipItem松手直接source += timeline_delta；snap handler再次强制同公式，甚至覆盖调用方给出的正确source范围。
- 本次实测：实际 `TimelineView._on_trim_right_finished`，原timeline_duration100/source_duration200/speed2，新timeline80、输入正确source160，在snap开启且未吸附时也变成source180。
- 实施：Item只上报timeline目标边界，不自行推source。View调用纯领域slice/trim service得到before/after范围，preview和commit复用同一结果；linked两个clip分别按各自timebase求source变化，不能复制同source delta。snap只改变目标timeline边界再重新求映射。
- 验收：0.5/1/2x、反向、30→24fps、snap开/关/Shift暂禁、左右缩短/延长、linked AV；预览/松手/重载/export相同source帧；本例必须source160。

### C07 · P1 · 混合帧率素材改速后的时长计算错误

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:16249`–`:16262`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:590`；insert/overwrite纯ops的类似formula位于`:1290`和`:1404`（同为source_duration/speed）。
- 本次实测：source30fps120帧，sequence24fps，原timeline96帧；ChangeClipSpeed 2x后timeline60帧，应48帧。尾部取源帧被mapper钳制，产生保持尾帧或时长漂移。
- 实施：统一duration mapper：source_frames / source_fps / speed * timeline_fps；常数速度用Fraction决定统一取整政策，变量速度先积分source各segment的秒时长再转timeline ticks；source metadata缺失时明确legacy1:1处理并记录诊断。rate-stretch应由原实际秒时长求新speed，不直接source_frames/new_timeline_frames。
- 验收：24/25/30/60/30000/1001与各sequencefps，0.1–8x、变量速度、linked不同fps素材；总秒时长及最后有效source帧准确，preview/export/reload一致。

### C08 · P1 · 波纹裁剪不处理链接音视频，破坏A/V同步

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:9162`、`:9231`（只创建主clip命令）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:13251`（却把linked所在轨排除sync跟随）。
- 本次实测：真实UI `_on_ripple_trim_right_finished`（FakeView真实CommandStack），视频/音频双向链接，原各100帧；视频缩到80，音频仍100，链接ID继续有效。
- 实施：领域层构建一个A/V editing-unit事务，先校验双方及双方followers的素材余量、track lock/重叠；生成两条RippleTrim且第二条不重复sync-follow，或一个LinkedRippleTrimCommand。拒绝任一边时整组回滚；UI preview也按同结果更新两边与followers。
- 验收：linked selection开/关语义明确，左右trim、左右extension、锁linked track、被阻挡linked follower、不同sourcefps/speed；双边范围/时长保持同步，一步undo，两轨都收到refresh。

### C09 · P1 · 主轨可编辑时速度命令会直接修改已锁链接轨

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:16261`–`:16287`；UI入口 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:10491`；普通linked trim验证也只检查overlap而不检查伙伴track.is_locked。
- 本次实测：视频轨未锁，链接音轨`is_locked=True`；ChangeClipSpeed 2x后锁定音clip duration100→50。
- 实施：统一command/service前置校验 `assert_editable_editing_unit`，对所有将受影响的clip和目标track校验锁状态，主轨/伙伴/邻居/同步跟随策略一致；track-lock只由专门lock command改变，undo允许恢复合法历史而不是被现锁挡死。所有UI动作只依领域异常更新状态提示并恢复preview。
- 验收：移动、trim、speed、unlink、freeze、ripple、overwrite从未锁主clip发起时不得绕过锁伙伴；失败不增revision、不改undo、不发部分timeline。

### C10 · P1 · 带淡出片段切分或裁剪到更短时抛错

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2218`（左半只clamp fade-in，漏fade-out）；`:1697`–`:1717`（Trim用with_trim，不经copy_clip_with）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:547`（直接replace）。
- 本次实测：100帧clip淡出40，blade20或right trim到20均`ValueError: fade_out_duration (40) exceeds clip duration (20)`。
- 实施：明确fade的slice语义：blade左段不继承原尾部fade-out（或按原可见包络切片），右段继承原尾fade并clamp；trim按保留内容求fade envelope，所有affected属性快照在命令中保留，undo恢复原fade数值。Speed/retime shortening也复用相同规范，不依赖模型抛异常后失败。
- 验收：fade-in/out单独与相交组合，blade近头/尾、左右trim、speed升高、slide邻居缩短；操作成功，undo完整恢复fade；音/视频包络符合既定策略。

### C11 · P1 · Split/Delete及撤销丢失已有转场

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:2478`（with_clip_removed删除相关转场）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2241`（blade临时移除原clip）、`:2260`（只保存original_clip）；`:359`（Delete.undo只有clip快照），Batch/Ripple同类。
- 本次实测：A头部10帧单边淡入转场，blade50：转场数1→0→undo0；Delete：1→0→undo0。工程有效效果在可撤销编辑之后仍永久丢失。
- 实施：clip变更事务快照包括referencing transitions；slice时头转场迁移左段、尾转场迁移右段，双边seam依据两侧存活范围调整/拒绝/删除并记录；undo原子恢复clip+transition完整快照；transition IDs/params保持稳定。copy/delete/overwrite/range同服务。
- 验收：单边头尾、双边视频及音频转场，blade/普通delete/ripple/batch/overwrite/跨轨move/undo/redo；完整参数和转场身份恢复，save/reload仍存在。

### C12 · P1 · 移动与波纹跟随后的转场中心不随clip/seam移动

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:491`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:2482`（with_clip_updated不修转场anchors）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:657`（sync rebuild也只改clips）。
- 本次实测：A带head淡入center0，A移动到200，trans.start仍0。`validate_timeline`返回“not anchored to clip start”和“range exceeds clip”。这类无效时间状态仍可保存/重载。
- 实施：为transition定义以clip edge/seam为锚的语义坐标，或所有时间变换事务对受影响transition重求center；两个clip分离时有明确删除/转换/拒绝策略；command提交前统一timeline invariant检查。undo恢复before而不是反算。
- 验收：单边头尾跟随移动；整组双边转场移动、roll seam变更、ripple followers、跨轨移动、重载；validate_timeline零错误，预览与导出同帧转场。

### C13 · P2 · clip移动后关键帧留在旧绝对时间

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:491`；模型采样 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:770`；渲染 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_render_plan.py:889`。
- 本次实测：A opacity关键帧timeline0=0、99=1，A起点0→200，clip新起帧opacity从0变1；动画整体消失在片段前方。
- 实施：明确clip参数和effect参数统一clip-local time，schema加动画timebase版本并迁移旧absolute关键帧；或当前架构对所有move/ripple/slice同步平移absolute clip/effect关键帧。提供受试验保护的legacy读取，避免重开工程二次迁移。
- 验收：opacity、transform、clip effects的关键帧，move/跨轨/多选/ripple/duplicate/trim/split/retime；操作后原local-frame参数曲线保持预期，undo和save/reload准确。

### C14 · P1 · Slide遇到已有间隙或淡出时不可逆

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:2029`、`:2060`（把邻居边缘贴到目标，不保留原gap）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:11033`（undo只slide(-delta)，虽存snapshot却未使用）。
- 本次实测1：原A=(0,90)、B=(100,90)、C=(200,100)；slide B+5后A=(0,105)、B=(105,90)、C=(195,105)；undo A=(0,100)、B=(100,90)、C=(190,110)，两个原10帧gap丢失。
- 本次实测2：紧邻A100帧且淡出80，slide B-50后A淡出clamp到50；undo A时长回100，淡出仍50，应80。
- 实施：Slide命令存完整primary/prev/next before快照（包含fade、effects、keys、marker、transition），undo原子恢复；明确有gap时slide是保留原gap、将gap视作可编辑空片，还是要求连续三片并禁用；不能逆向函数代替undo。preview和commit同语义，并校验邻居source handles。
- 验收：左右/仅单邻居/三连续/有gap、带fade/变速/倒放/transition；`undo(apply(timeline)).to_dict()==before`；持续三次redo稳定。

### C15 · P2 · blade后右半片段上的clip-scoped marker仍属于左clip

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/commands.py:2245`（没有marker重新归属）；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_v2.py:151`（marker带clip_id+absolute frame）。
- 本次实测：A=[0,100)、marker frame75/clip_idA；blade50后marker75仍指向timeline_end50的左A。后续单独移动左A时marker错误跟随左A。
- 实施：slice事务按marker的source/clip-local位置迁移clip_id，frame保持content位置；原marker身份稳定；cut点marker规则（left/right）明确。Split命令保存before marker载荷，undo恢复归属；trim删去内容的marker明确隐藏/保留/清理策略。
- 验收：marker在切点前/上/后、clip move/linked split/range delete/重载；每个clip marker在所属范围，跟随正确半段。

### C16 · P2 · 跨轨Sync Lock平移clip却不平移其scoped marker

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_ops.py:650`–`:661`，restore同缺marker；范围/普通本轨move有 `_move_scoped_markers_with_clip`，跨轨路径没有。
- 本次实测：删除V[0,10)，另一同步锁A clip起点50、scoped marker55；clip→40，marker仍55（原相对5帧变相对15帧，已越过10帧clip）。
- 实施：把clip、scoped markers、动画、单边转场作为同一 temporal-transform 载荷；跟随位移保存最初完整before/最终after，撤销同时恢复。
- 验收：跨轨insert/ripple trim/delete/batch delete、多个跨度、track locks，marker相对clip位置稳定；undo/redo/save/reload一致。

## 静态确认、还需真实素材交互复测的风险

### C17 · P1 · 普通右裁剪的source上界校验漏掉source_start

- 定位：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/timeline_view.py:8482`、`:8559`，仅比较new_source_duration<=max_source；ClipItem类似边界使用需统一；`TrimClipCommand`与model没有总source长度可校验。
- 触发条件（静态，不计作本次GUI实测）：总source200、source_start100、原duration100，右延长到150；校验150<=200放行，实际exclusive source_end250超素材200。
- 实施：source bounds从MediaAsset/PTS和领域服务取得，统一校验 `source_start + source_duration <= available_source_frames`；逆向/混合fps/变量速度先求真实source区间。对无元数据素材明确未知handles并限制延长，不用巨大默认值。所有入口均由service校验，UI只preview。
- 验收：在真实素材中间in点剪辑、普通/ripple/roll/slide/rate stretch向末端延长；边界最后可用帧能精确达到，超一帧不能提交，日志/状态栏给出可用余量。

### 其他风险（建议追踪，不作为已确认用户bug统计）

- 多数字段虽然数据类 frozen，但animated_params/params等嵌套容器仍可变；要审核调用方是否原地修改，避免旧快照泄漏。这个审计没有证明现实调用方已发生，所以应列为防护任务。
- Clip/track/timeline from_dict 对主block未知字段不普遍保留；caption_graphics已有extra机制，clip和sequence主block没有。后向加载未来schema并保存可能丢新字段。需用schema version与extensions解决，不能无条件接受所有类型。这里只做结构确认，无未来版本真实项目可测。
- validate_timeline 对transition检查未要求双边center等于当前真实seam，对clip-scoped marker范围、linked AV时长/起点一致也不检查；应补领域不变量及报告机制，而不只修UI显示。

## 缺失的领域能力（可写入新计划，避免与已存在功能混淆）

1. **统一时间变换事务服务（P1，约4–7日，依现有实现拆分）**：当前move/trim/split/ripple/slide各自修改clip，marker/transition/animation/retime控制曲线未一致随动。新增editing-unit+time-transform pure planner，输入原timeline、操作、asset metadata，输出完整before/after/delta/诊断；通过一个Macro或compound command提交，revision只增一次。先实现slice/move/follower-shift，接着迁移普通trim/roll/slide/insert/overwrite/range。采用完整serialized快照/结构化diff验收，不依ID equality。
2. **跨越编辑点的Sync Lock与字幕联动策略（P2，3–5日）**：现shift_sync_lock_followers仅处理video/audio中start>=point的整clip，subtitle track明确排除；跨point长片与字幕不会被切分/跟随，负delta在各轨独立clamp造成不同累计位移。增加Sequence Sync Scope（选定轨/所有同步锁轨、字幕跟随可选），对跨pointclip先slice、字幕cue/word timing按range调整；输出受影响轨和冲突预览，默认整事务拒绝或明确冲突解决，不静默desync。测试长音床、跨点字幕、多个波纹跨度、锁轨。
3. **可用素材handles的统一服务与UI提示（P2，2–3日）**：roll command/ripple right/slide邻居目前可不传max_source，normal trim校验在item层不完整。新增SourceAvailability查询（视频帧/图像无限或有限/生成器/composite/virtual MG的不同策略），支持FPS/PTS；trim monitor显示剩余head/tail并在达到边界时阻尼停止。任何command入口（UI/AI/API）都必须验证。包括缺失素材/未知duration的可编辑政策。
4. **完整编辑事务回归矩阵（P1，2–4日，可并行）**：已存在133项focused tests，但很多组合没有覆盖；新增参数化矩阵：speed×fps×direction×fades×transitions×markers×linked/lock×operation，最低先覆盖上面C01–C16；模型层property检查slice后逐帧source映射不变、undo/apply全量to_dict不变，UI release handler与模型planner语义一致；preview/export/source索引端到端小素材验收。需避免将只检查方法存在的test当行为验收。

## 推荐执行依赖

- 第一批P1：C01/C02/C03（撤销与波纹数据完整性）→ C04/C05/C06/C07（source/timebase）→ C08/C09（编辑单元/lock）→ C10/C11/C12/C14（内容与附属状态）。
- 第二批P2：C13/C15/C16及统一transaction service、cross-point/subtitle同步、source handles UI。
- 每批先加失败用例再实现最小领域修复，避免直接复制逻辑到UI；command revision、undo、预览、保存共用同一新模型。
- 交互验收必须补在真正app窗口中，由主审计执行；本报告仅将独立代码复现与真实GUI手测分开标注，不声称所有功能已完整交互覆盖。
