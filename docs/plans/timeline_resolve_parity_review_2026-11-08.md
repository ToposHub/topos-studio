# Resolve 时间线对齐复审与后续后端修复计划

日期：2026-11-08。新建计划，保留 [2026-11-07 原接线计划](timeline_resolve_backend_wiring_2026-11-07.md)，不覆盖其历史记录。

本次复审区分三件事：**按钮/信号已经接线、内容操作是否安全、UI/行为是否与本机 Resolve 20 一致**。三者不能互相替代。原 W00–W09 的内部测试通过记录保留为历史事实，不能据此把全部功能和 Resolve parity 判为完成。

用户本轮范围：UI 与交互问题直接修复；后端问题只整理详细计划，由用户后续处理。本文 UI 清单均属于**本轮实施**，不推迟到未来；后端 B01–B19 共19项，全部为待修/待实现任务，本文不改变 core、schema、渲染或音频后端。

## 1. 证据口径与边界

| 证据类型 | 本次使用范围 | 不能据此宣称的内容 |
|---|---|---|
| 本机 DaVinci Resolve Studio 20 直接观察 | linked A/V 两段选择态均为红色轮廓，没有常显角标；Change Clip Speed 窗口的 Speed/FPS/Duration、ripple/reverse/freeze/pitch、maintain/stretch 选项；Retime 顶部 title/箭头与底部percentage布局 | 没有逐项成功执行的所有原生鼠标手势、每个选项的后端语义、全部混合帧率/音频设备行为 |
| 官方 Edit 页 | 智能trim按鼠标位置切换、常见剪辑工具及编辑方式的方向性参照 | 当前官网已是更新版本，不能把当前网页截图当成本机20的像素级基准 |
| Resolve 20 官方 Beginner's Guide | 20版本编辑工作流的书面参照 | 未验证的选项不能仅凭手册名称认定行为一致；需记录具体页码和版本后再制定细则 |
| Topos 真实 Qt 手势探针 | 向真实TimelineView.viewport投递press/move/release；验证模型、预览、历史与取消 | 探针返回pass或fake host计数不等于用户工作流正确，更不等于Resolve实际手势已全部自动执行 |
| 产品代码静态审查/独立模型探针 | Replace源窗、Flag undo、service错误边界、持久化、录音调用链 | 未执行真实解码/设备的结论必须标为静态推断，不把推断写成已观察黑帧或真实录音成功 |

官方来源：[Blackmagic Design Edit 页](https://www.blackmagicdesign.com/products/davinciresolve/edit/)，[DaVinci Resolve 20 Beginner's Guide PDF](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Beginners-Guide.pdf)，[官方 Training 入口](https://www.blackmagicdesign.com/products/davinciresolve/training/)。本次工具已确认PDF官方链接；正文较大，不以当前抓取结果假称已逐页审读。

本地证据：

- [后端发现与三个独立探针](../audit/2026-11-08-timeline-review/backend_findings.md)。原审查基线 `6120873..720b875`，代码行号以该基线及复审中的工作树为参考，后续修改以函数名定位。
- [真实Qt拖拽审查](../audit/2026-11-08-timeline-review/ui_evidence/drag_audit_findings.md)、[原始输出](../audit/2026-11-08-timeline-review/ui_evidence/drag_audit_output.txt)。其中19个探针执行完成；前7项打印观察值，并非19个行为均无缺陷。
- 既有后端 focused suite 独立重跑：61 passed（5.53s）。反例仍存在，说明原用例覆盖不足；此记录不是本轮最终UI回归结果。
- 本轮UI代码、原生观察与最终测试状态已在第8节收尾；完整验证命令另存记录。

## 2. 用户八项要求对照

保留Topos现有配色；对齐参考的是控件层级、形状、位置、选择反馈和操作语义，而非强制换成Resolve的素材颜色。

| 用户要求 | 对照依据 | Topos复审问题 | 本轮直接UI/交互修复 | 后端后续任务 | 验收界限 |
|---|---|---|---|---|---|
| 1. 图标 icons | 本机20工具栏/窗口观察；官方Edit工具说明 | 个别旧字体图标、工具含义与布局不一致，链接角标干扰选择表达 | 本地矢量图标、统一描边和命中区、工具主次层级；移除常显修剪角标与链接组底色线，保留名称旁链条标识 | 无须为换图标新增后端 | 同尺寸/高DPI清晰；工具tooltip、快捷键、状态含义一致；不宣称所有官方图标资源逐像素复制 |
| 2. 选择 select | 本机20直接观察linked两段红轮廓；Topos真实选择与Option事件 | 伙伴只是视觉发光而不是真selection；已选伙伴持续发光；临时独立选择不完整 | 真正联动选择、Option临时独立、已选伙伴统一轮廓、取消多余常显修剪角标与残余发光 | B01旗标与其他批量操作必须消费真实selection，避免undo损坏 | 点击/框选/多选/Option/linked开关后Selection和画面一致；不以hover代替选中 |
| 3. 缩短 shorten/trim | 官方smart trim方向；Topos真实trim/ripple/return/Esc探针 | T区域、接缝和修剪反馈需一致；已有命令边界仍需验证 | 按位置反馈正确cursor，A/T精确接缝、拖动预览、回原位/Esc零写入；本轮修残余表现 | B03/B04/B06涉及源窗及锁策略，不重写已接通T工具 | 同一gesture预览/提交一致；保持原值时零历史；混合帧率正确性需后端专项 |
| 4. 移动 move/复制 | Topos真实Qt手势；官方编辑工作流 | 普通移动可同步；Alt复制ghost与最终避让落点不一致；组复制可失同步 | ghost显示既有最终候选位置，不改变后端避让策略；确认snap/取消/联动反馈 | B11：共同delta的组复制事务，阻挡时不得逐片独立避让 | UI预览可对上当前提交；音画一致性要等B11完成，不能因ghost改对就标内容安全 |
| 5. 跨轨 cross-track | Topos真实QTest跨轨与不同轨高探针 | 目标140px轨预览仍82px，释放才跳高 | preview/ghost同步目标轨高，取消还原；frame/track落点投影一致 | B11组复制跨轨冲突预检；B09路由上下文隔离 | V1/A1→V2/A2的Y/高/时间预览正确；Shift轴锁未有可靠原生证据，不自行发明 |
| 6. 解除链接 unlink | 本机linked选择反馈；Topos既有link命令与Option实测 | 按钮状态、真正链接数据、一次性独立选择容易混淆 | Link/Unlink继续既有命令；linked toggle控制选择；Option只作用当前gesture，松开恢复 | B03需Replace验证互链伙伴；B11新复制链接需同步 | link模型与selection分清；无须永久unlink即可临时独立编辑；撤销恢复模型 |
| 7. 变速 speed | 本机20 Change Clip Speed与Retime布局直接观察 | 原对话框字段不完整；Retime上下布局/关闭不一致；高级设置没有后端语义 | 完整可取消draft对话框，Speed/FPS/Duration联动、reverse/freeze/pitch/ripple/maintain/stretch；Retime上title/箭头下percentage与关闭 | B03/B04 Replace；B14高级draft接收；B15 MoveSpeedPoint；不支持选项明确发请求并拒绝静默丢失 | 取消零写入；支持基础设置走现有命令；高级选项未接后端时clip不变且有明确反馈，不假称已生效 |
| 8. 淡入淡出 fades | Topos真实fade In/Out回原位/Esc；官方编辑方向 | 端点/命中/预览要稳定；曲线后端尚无字段 | 保留既有duration handles与命令；本轮调整可见性/命中/取消表现 | B16 FadeCurve模型与音视频消费者；UI curve request不等于效果已实现 | 当前duration调整preview/export不回退；未建模型前曲线只能请求/反馈，不能只画曲线冒充实际处理 |

## 3. 本轮UI与交互修复清单（立即实施）

本表为本轮已实施的UI修正。后端ID表示领域能力依赖；相关音视频内容正确性仍按后端表单独验收。

| UI ID | 优先级 | 问题/入口 | 本轮动作 | 验收 | 状态 |
|---|---|---|---|---|---|
| U01 | P1 | linked A/V选择、Option临时独立；`timeline_view.py` selection/drag入口 | linked伙伴进入真正Selection；Option gesture独立且不改永久链接；菜单/快捷键消费同selection | 鼠标真实事件、multi/linked toggle/Option组合；选择对象和画面一致 | 已修复（验证边界见第8节） |
| U02 | P2 | 已选partner仍发光、常显修剪corner；`timeline_items.py` paint | 去常显修剪corner，以两段完整轮廓表示选择；已选不重复hover发光 | normal/hover/selected/disabled/locked/linked开关状态图与截图 | 已修复（验证边界见第8节） |
| U03 | P1 | ChangeClipSpeed缺完整UI；`change_clip_speed_dialog.py`、view速度槽 | 建立完整draft，取消不改模型；高级不支持选项请求后明确反馈 | Speed/FPS/Duration联动，reverse/freeze/pitch/ripple/maintain/stretch全部可review；取消零写入 | 已修复（验证边界见第8节）；后端B14另列 |
| U04 | P2 | Retime上下布局、关闭入口 | 上方title/箭头、下方percentage；百分比菜单和关闭入口命中一致 | 本机20截图对照；关闭后恢复clip主体编辑；零多余命令 | 已修复（验证边界见第8节） |
| U05 | P2 | 跨不同轨高preview跳变、copy ghost落点不准；view drag preview | preview投影目标轨高及既有避让候选；Esc/回原轨恢复 | 82→146px目标轨，move/copy/cancel；ghost帧与现有提交帧一致 | 已修复（验证边界见第8节）；不能代替B11 |
| U06 | P1 | 蓝徽章painted v1、effective v2 | 徽章显示有效路由；红target变化时同步未显式蓝路由；显式蓝路由仍独立 | 无条目/显式选中/显式关闭/删轨/undo及红target切换 | 已修复（验证边界见第8节） |
| U07 | P2 | presentation custom恢复/非法scroll阻断；`timeline_panel.py:apply_presentation_state` | 保持custom ppf/scroll；逐字段校验非法类型、非有限值与超大整数，恢复不发编辑请求 | 正常scroll、0、负值、字符串、NaN/Inf及Full→Custom往返 | 已修复（验证边界见第8节）；未知字段保存B07 |
| U08 | P1 | PositionLock Delete异常离开Qt事件边界 | Delete/Cut/Paste等UI handler捕获已知策略拒绝，显示错误，保留状态；不吞真实未知错误 | QTest投递Delete等，Qt事件无异常，模型/history/revision不变 | 已修复（验证边界见第8节）；service归一B06 |
| U09 | P2 | clear路由/selection通知缺失 | clear时重置应清UI状态、发送必要selection通知，使Tray/属性面板不留旧投影 | clear/close/reload后source和selection一致，订阅者收到刷新 | 已修复（验证边界见第8节）；跨项目归属B09 |
| U10 | P1 | Tray坐标、焦点、取消、密集key命中、可见关闭 | 共享时间线映射，显式focus、Esc/隐藏/失焦/换选区取消，按像素命中密集key，可见关闭并回写按钮 | 0.01px/frame等overview、滚动/缩放、拖动取消、按钮/Tray关闭同步 | 已修复（验证边界见第8节）；完整key数据移动B12 |
| U11 | P2 | 本地矢量图标及过时工具/控件布局 | 统一已改工具、速度与源编辑图标，保留当前调色令牌 | 编译、资源可解析、正常/高DPI截图、窄窗口/Deliver往返 | 已修复（验证边界见第8节） |
| U12 | P1 | 用户18:59截图：真实host拖不动、光标闪烁、叠刻度/浅色底/粗线 | 属性数值/效果滑块owned-before-show；scene光标归属；整除刻度单次绘制；局部完整QSS与1px分隔线 | 原生真实host拖动550→610、A/V同步、CmdZ恢复；保留真实切窗取消；169组合及新专项通过 | **已修复**；[根因、原生证据及验证](../audit/2026-11-08-timeline-review/ui_evidence/drag_focus_and_chrome_fix.md) |
| U13 | P1/P2 | 淡变标签、首尾与接缝修剪样式/命中/选中不一致 | 白色窄圆角顶标签、绿色细端点括号、接缝双边括号、8种矢量光标；A/T顶部fade分区；持久选中与键盘1/5帧微调，兼容macOS实际Shift标点；不一致linked delta在UI拒绝 | 376项组合回归及后续专项；本机Resolve观察、Topos真实素材原生拖动/Undo/Shift+period验证 | **已修复本轮查实项**；[明细与验证边界](../audit/2026-11-08-timeline-review/ui_evidence/clip_affordance_parity.md)；曲线后端B16、共同trim边界B19 |
| U14 | P1/P2 | 播放头、顶部变速拖动、底部三角与展开高度 | 平面红色指针/细线；顶部独立拖速并保留完整参数；底部矢量三角与分组菜单；开启时顶部向上展开24px、关闭恢复；高度变更先通知Qt几何 | 混fps 100→50帧=2x；预览不提交、松手单次undo、Esc/关闭/回原位取消；真实Qt菜单点击；同轨多片展开不累积；本机Resolve/独立Topos观察 | **已修复本轮查实UI项**；[证据与限制](../audit/2026-11-08-timeline-review/ui_evidence/playhead_retime_controls.md)；高级段落编辑依赖B14/B15/B17/B18 |

## 4. 后端详细修复计划表（后续实施）

计数规则：原审查B01–B10不重复；新增B11–B19。presentation未知字段只列B07一次。UI修复不计入这19个后端任务。估时为单名熟悉项目开发者净人日，含focused tests，不含设备排障和评审排期。

| ID/优先级 | 代码定位 | 触发条件与实际结果 | 风险 | 修复步骤 | 验收标准 | 依赖/估时/状态 |
|---|---|---|---|---|---|---|
| **B01 P1** 旗标undo | `core/commands/clip_props_commands.py:2176,2185,2199,2206`，SetClipFlagCommand | c1已蓝、c2无色，多选设蓝后undo→两片都无色；已复现 | 撤销丢用户原旗标 | ①快照覆盖所有目标或仅遍历实际变更项undo；②身份用track+clip；③redo/序列化保持快照；④修混合no-op批次 | before `[blue,None]`完整恢复；已有多色/清除/重复targets/undo-redo/单步history均正确 | 无；0.5–1日；**已修复（后端批次 2b216b7）** |
| **B02 P1** Voiceover返回契约 | `ui/edit_page.py:635–639`；`timeline_view.py:ensure_target_audio_track`返回Optional[str] | 无音轨或蓝音频关闭时，返回str却读`.track_id`；已建轨/history1后AttributeError | 功能失败且残留半状态 | ①按track ID契约消费；②先确定录音上下文再创建轨；③失败清理/可撤销反馈；④无轨/蓝关闭/锁轨分支 | 无异常；返回有效ID；启动失败不留下不明轨；真实页面槽而非只测fake `_start_recording` | B10生命周期；0.5–1日；**已修复（78722e3）** |
| **B03 P1** Replace速度/伙伴源窗 | `ui/timeline_view.py:17888–17933`、`_find_linked_partner` | base1x、offset16→2x速度点，48帧clip源窗80被缩48，尾部clamp；linked1x/2x伙伴所需192却写96；已复现 | 尾帧冻结/音频源窗错误，linked结果损坏 | ①完整速度曲线/相位计算；②每个伙伴独立求源窗；③互链、异轨、边界、流检查；④全部预检后宏提交 | mixedFPS/速度点/反放/不同伙伴速度与时长覆盖；源不足整次拒绝；采样与expected clock一致；undo完整 | B05、B14映射原则；2–4日；**已修复（a971afe，逐成员积分+互链校验）** |
| **B04 P1** Replace冻结帧重映射 | `core/commands/clip_props_commands.py:ReplaceClipWithSourceCommand.apply`；view构造payload | freeze5的clip换In200后，source窗200..247而首帧仍映射5；已复现 | 读取新源窗外、不遵守新定位意图 | ①确定冻结相对/对齐语义；②payload、快照和undo覆盖freeze；③预检源范围；④与speed/reverse组合 | 新freeze处于有效源域且符合约定；源不足拒绝；preview/export同帧；undo恢复原值 | B03、B14；1–2日；**已修复（a971afe，冻结重映射到新源对齐帧）** |
| **B05 P1** Replace流兼容性 | `ui/timeline_view.py:replace_clip_with_source`（约17863）；replace命令 | 纯audio源Replace视频clip返回True，video轨指向audio asset；模型写入已复现，解码后果静态推断 | 无视频流、错误解码/预览，能力误报 | ①按目标成员轨类型验证源流；②video/audio/image/nested/generator矩阵；③需要只换伙伴时显式构建事务；④底层命令兜底 | 缺流整次拒绝零历史；允许组合真实解码/导出；不以fake asset存在判成功 | 无；1–2日；**已修复（a971afe，流预检整次拒绝）** |
| **B06 P2** PositionLock service错误契约 | `ai_editing/edit_project_service.py:217–237`；`core/position_lock.py:138–140` | stale revision999+missing clip：无policy返回conflict，带policy先dry-run直接ValueError；已复现 | 调用方拿不到CommitResult，冲突/失败处理分裂；重复apply | ①revision先验；②受控apply一次得proposed；③before/proposed footprint；④所有领域失败归一；⑤明确会话policy来源 | stale/invalid/macro后段失败/空锁集都返回约定结果且状态原样；UI与service同语义；历史不先pop | U08只修UI异常边界；1–2日；**已修复（3a9380b，revision先验+单次受控apply+快照footprint）** |
| **B07 P2** presentation未知字段/版本 | `ui/timeline_view.py:export/apply_presentation_state`；panel export；`app/controllers/file_controller.py:_get_edit_ui_state` | apply含future_field/version后export消失；UIState单对象小测试不能覆盖host重收集；已复现 | 重存未来包丢扩展配置；“versioned”无版本 | ①保留原始payload；②已知键更新、未知键merge；③写版本及未来版本策略；④嵌套路由扩展规则 | 真实FileController load→apply→collect→save往返，顶层/嵌套未知键保留；旧包默认可读 | B09；1–2日；**已修复（03f35703，version=1+未知键往返保留）**；不再另列重复任务 |
| **B08 P2** Flag命令校验 | `core/commands/clip_props_commands.py:2160–2163`；CLIP_FLAG_COLORS | 新写入`#zzzzzz`命令成功；已复现。UI挡住不代表service挡住 | 非法内容经AI/脚本落盘、白名单口径分裂 | ①统一新写validator；②白名单或规范hex语义定稿；③from_dict执行同校验；④读取未知历史值继续保留 | 非法hex/非白名单新写拒绝；未知历史重存不丢；清除与UI/service一致 | B01；0.5–1日；**已修复（2b216b7，白名单统一validator）** |
| **B09 P2** 跨项目展示/路由隔离 | `timeline_view.py:set_project/clear/apply_presentation_state`；FileController恢复链 | clear后同timeline ID蓝禁用残留；已复现。跨工程沿用ID后果静态推导 | 新工程插入流被旧工程关闭，旧偏好误继承 | ①project/timeline key归属；②新工程完整替换上下文；③旧包缺字段明确默认；④只导出本工程；⑤切timeline保存恢复 | A→B无payload→A；同timeline ID不同project ID；未泄露禁用路由和PositionLock；旧包安全 | U09只清UI通知；B07；1–2日；**已修复（03f35703，set_project 按工程隔离）** |
| **B10 P1** Voiceover完成事务 | `app/controllers/edit_controller.py:3221–3223,3285–3390`；EditPage voiceover槽 | 新入口复用链仅import asset/add_take_entry，无新增timeline clip命令；只捕获project+track无timeline；静态确认，未录真设备 | “录入选定轨道”未完成；同项目切timeline/删轨缺会话边界 | ①明确ADR素材采集vs时间线Voiceover；②捕获project/timeline/track/anchor/session；③take完成asset+clip事务；④重复/迟到/取消/切项目拒绝；⑤统一资源释放 | RecordingManager替身模拟完成；timeline clip正确帧/轨；单步undo、一次导入；真实设备smoke；录制中切timeline/删轨安全 | B02、B06；2–4日+设备回归；**已修复（c46a3609，会话绑定+落clip事务；真实设备 smoke 待用户验收）** |
| **B11 P1** 组Alt-copy同步避让 | `ui/timeline_view.py:_commit_copy_group`成员循环（原约8962，现约8977）；`_resolve_copy_start` | A/V原100..200，Alt指向220；A1有200..300 blocker；视频副本220、音频副本300仍互链，差80帧；真实Qt探针确认 | 音画内容错位且关联数据声称成对 | ①建立组复制计划；②所有成员共同delta和track映射；③统一冲突策略；④一次预检；⑤宏提交和共同预览计划 | blocker只在任一成员轨时，整组共同移动或整次拒绝；A/V同起点；多选/跨轨/锁轨/undo单步；不能只修ghost | U05展示不代替事务；B06；2–3日；**已修复（4a549c8，_plan_group_copy 共同位移）** |
| **B12 P1** 关键帧移动保留完整数据 | `ui/edit_keyframe_tray.py:_commit_drag`；`core/commands/effect_commands.py:Add/RemoveKeyframeCommand`；`src/shared/keyframe.py:Keyframe` | Remove+Add即使保留interpolation仍只携value/type，丢tangent_mode、handle_left/right、legacy bezier、extra；静态数据路径确认 | 移时间却改变曲线形状；undo也可能无法恢复完整key | ①新增完整key移动事务或扩命令payload；②复制整key只改frame；③处理目的帧碰撞；④before/after完整快照；⑤对handles坐标语义校验 | broken/smooth/自定义handles/extra保留；仅frame改变；冲突规则可review；undo/redo含邻点采样一致 | U10只修交互；B06；1–2日；**已修复（9acc643，MoveKeyframeCommand 整key复制）** |
| **B13 P2** 电平停止/暂停生命周期 | `core/audio_playback.py:AudioPlaybackManager.pause:4983,stop:5028,_on...levels:5216`；EditPage meter订阅 | stop/pause发state不发静音level；meter可能保留最后dB；静态确认，订阅重复已另有测试 | 停止后仍显示有声，真实level与可见反馈不同步 | ①约定pause/stop/seek/scrub/结束的瞬时值与peakhold；②真实manager发静音或统一状态feed；③generation丢旧level；④不重写meter、不伪造音频 | 注入最后-6dB→stop/EOF后瞬时值归静音；peakhold按规则衰减；旧manager回调不回亮；单/多声道 | W08已有订阅链与电平控件完整保留；0.5–1日；**已修复（c0819d9a，静音广播+迟到回调丢弃）** |
| **B14 P1** 高级速度draft接收 | `ui/change_clip_speed_dialog.py:settings`；`timeline_view.py:clip_speed_settings_requested(str,str,object)`、高级速度槽（约12045） | 本轮完整draft已经能表示ripple/freeze/maintain/stretch；现有backend不能全部执行时发请求并保持clip不变 | 若宿主只接部分字段会静默丢用户设置；不能把UI完整当能力完整 | ①声明versioned settings contract；②host接收并能力预检；③支持则单事务执行全部意图；④不支持明确拒绝；⑤刷新/undo/取消；⑥preview/export能力登记 | 所有字段逐一验证；ripple/freeze/animated-maintain/stretch/speedpoints组合不静默降级；单步undo；取消零写入 | B03–B06、B12、B15；3–5日；**stage 1+3 已实现（d3eae5d2：接收链/契约/一致性/能力矩阵+SetClipSpeedSettingsCommand 单事务+clear_point）；stage 2/4/5（分段 stretch/freeze/reverse/rewind/ramp/reset 的曲线事务）仍为显式请求+诊断** |
| **B15 P2** MoveSpeedPoint领域操作 | `core/clip_params.py:SpeedParams/SpeedPoint`；view既有retime point handlers；当前未发现MoveSpeedPoint命令 | 速度点增删/段速度已有部分能力；将点拖到新时刻需保持积分与窗口时基，不能只换UI X | 曲线时间/源offset混淆、跳帧、长度及linked失配 | ①规定source_offset与timeline拖动转换；②新增MoveSpeedPointCommand（建议名）；③边界排序/碰撞；④连续积分/phase/反放/linked策略；⑤序列化/undo | 混fps、8x、smooth/linear、反放、切片curve_offset、linked；移动点保留采样连续性；预览取消零写入 | B14、既有SpeedParams采样；2–4日；**已实现（b7816ee9，with_speed_point_moved+MoveSpeedPointCommand）** |
| **B16 P2** FadeCurve模型与渲染 | `core/timeline_v2.py`当前fade_in/out_duration等字段；view/item fade handlers；当前无FadeCurve字段 | duration handles有效；UI曲线请求尚无持久模型和统一音视频消费者 | 只画曲线/发请求会产生“界面像生效、声音/画面未变”的假实现 | ①定义FadeCurve schema（建议名、形状/控制点/域/默认）；②版本/旧包兼容；③SetFadeCurveCommand；④视频预览/导出、音频mixer/离线共同消费；⑤UI请求结果回写 | 默认曲线保留旧结果；每种曲线preview/export、实时/离线一致；HDR不无理由clamp；undo/保存/取消/失败路径完整 | Effect/能力registry与后端消费者；3–5日；**已实现（0622fb88，fade_curve 模型+SetFadeCurveCommand+视频render plan/实时/离线/Stem 统一消费；UI 曲线控件仍按边界只发 draft）** |
| **B17 P1** 变速undo丢动画 | `core/commands/clip_props_commands.py:ChangeClipSpeedCommand.apply/undo` | 100帧互链A/V，视频key1、音频key40；2x变速变0/20，undo仍0/20；独立模型探针已复现 | 主片反向重映射舍入不可逆；伙伴undo遗漏动画恢复，可能永久损坏关键帧 | ①完整before clip/animation快照；②undo直接还原，禁止反算；③完整key/handles/effect动画保留；④碰撞/序列化/redo测试 | undo回到视频1、音频40及完整曲线；高倍率压缩碰撞后无丢点；snapshot往返和linked不同span可恢复 | B12/B14；1–2日；**已修复（d04992f，完整动画快照直接还原+伙伴动画恢复）**；新速度窗口对动画统一发请求 |
| **B18 P1** 比例拉伸时基/参数 | `ui/timeline_view.py:_on_rate_stretch_finished`（虽在UI文件，承担真实速度计算与命令编排） | 源48fps/200帧，时间线24fps/100帧，拉到50帧应2x；实际4x/25帧，reverse与preserve_pitch均从True变False；真实View槽已复现 | 松手结果不等于目标时长，倒放/保调设置被重置 | ①统一源/时间线时基和完整速度积分；②保留原SpeedParams非目标字段；③复杂curve/freeze/动画能力预检；④linked各成员一并预检；⑤保证preview与commit同计划 | 混fps左/右stretch得到目标50帧及2x；reverse/pitch不丢；复杂曲线有明确支持或拒绝；取消/零变化不入历史；undo精确 | B14/B15/B17；1–2日；**已修复（d513753，当前时长比换算+参数保留+陈旧信号忽略+高级分派）**；本轮未改该计算 |
| **B19 P1** 联动数值trim共同边界 | `ui/timeline_view.py:_on_clip_numeric_trim_requested/_apply_step_trim`，`core/timeline_ops.py:build_numeric_trim_command` | 互链100帧；源1000，视频起890余10，音频起897余3；ripple+5单片各钳+5/+3，原宏提交后105/103；[探针](../audit/2026-11-08-timeline-review/backend_evidence/linked_numeric_trim_probe.py)已复现 | 音视频编辑终点失同步；AI/service若直接照搬仍可写入不一致宏 | ①共同范围预检；②明确定义整组共同钳制或拒绝策略；③全组一个delta计划；④sync-lock跟随一次；⑤原子提交/undo；⑥UI和服务复用计划 | 不同fps/速度/源余量/phase、两侧trim、ripple跟随、锁轨、序列化、undo覆盖；组delta必须一致或整次拒绝 | 本轮UI已拒绝不相同delta，不修改后端共同计算；B06；1–2日；**已修复（10953a84，build_linked_numeric_trim_command 共同区间计划，UI 两入口复用）** |

### B14 高级速度请求的数据契约

**已经存在的UI请求**：`TimelineView.clip_speed_settings_requested(str, str, object)`，参数为 `clip_id`、`track_id`、settings草稿。现阶段ripple、freeze（含当前已冻结）、speedpoints以及任何clip/effect动画选项发此请求并明确提示clip保持不变，不假装完成。TimelinePanel同名信号已转发给宿主。动画stretch也等待B17安全撤销修复，不能只支持apply。

当前draft字段：

```text
speed: finite positive playback multiplier
reverse: bool
freeze: bool
preserve_pitch: bool
ripple: bool
keyframe_timing: "maintain" | "stretch"
duration_frames: positive timeline-frame count
```

后端接收时补齐project/timeline/source身份、base_revision、完整源fps与时间线fps、有声流及linked上下文；version字段属于建议后续契约，不是当前settings已经含有。Speed/FPS/Duration是同一速度意图的多种编辑表达，接收层必须验证一致性，不能分别覆盖互相矛盾的值。

建议接收链：EditPage读取最新上下文→EditController编排→领域预检/命令→既有提交边界。不能简单把回调接到基础`ChangeClipSpeedCommand`后丢弃ripple/freeze/keyframe_timing。支持时一次提交全部设置；不支持时返回明确诊断、clip/history/revision均不变。服务/AI入口使用同一能力和领域规则。

### B14 追加：顶部拖速与百分比菜单请求（U14）

现有 `clip_speed_settings_requested` 新增区分类型的 UI 草稿。旧速度对话框 settings 保持上文字段；接收者必须先分辨 `operation`，不能把下列菜单请求直接按旧 settings 解析。

```text
operation: "retime_controls"
action: "stretch" | "clear_point" | "freeze" | "reverse" | "rewind" |
        "ramp_up" | "ramp_down" | "reset_clip" | "pitch"
source_offset: int  # 所选速度段的源坐标；接入时明确 clip-relative 与 curve_offset 换算
value: object
  stretch -> {original_duration: int, duration: int, speed: float}
  rewind  -> positive multiplier (1/2/4/8)
  pitch   -> bool
  others  -> None
```

| 接线阶段 | 后端工作 | 验收与禁止降级 |
|---|---|---|
| 1. 接收/能力判断 | EditPage/Controller 按operation分派；补 timeline/project/revision、源/时间线fps、源相位、clip当前curve、完整linked成员；返回可执行/不支持/冲突及诊断 | 无消费者、失效revision、不支持参数都不得算成功；失败零history/revision |
| 2. 顶部stretch | 共同 timing plan 返回每个成员的新 duration/speed 与非目标参数；包含混fps、整数舍入、倒放、源相位、curve/freeze/动画保护；拖动preview与松手commit共用计划 | 目标时长与结果一致；原参数不丢；linked音频实时preview与提交一致；Esc、回原位和失败恢复全部投影；旧rate-stretch B18也统一 |
| 3. clear单点 | 区分选中段左边界与全曲线；确定删除边界后左右段的速度及时间锚；提供独立可撤销操作 | 禁止调用 `without_speed_points()`冒充单点删除；首段无可清边界；剪过的curve窗口坐标正确 |
| 4. freeze/reverse/rewind | 定义选中段的源窗、入/出锚和方向；Rewind按参考工作流生成其段结构与倍率；freeze按选中位置定格；reverse只作用目标段 | 禁止把Rewind映射为整clip反向；禁止把段冻结无条件映射为既有“播放头分割右段”命令；linked和音频策略明确 |
| 5. ramp/reset/pitch | 0速边界需要可积、有限且可序列化的时间映射；reset定义保留/清除哪些曲线及音高选项；pitch与preview/export能力一致 | MIN_SPEED=0.1的当前模型不能伪装支持0%；尾端锚、持续时间、无穷/NaN防护、保存重开/undo全验证 |
| 6. 完成/失败回写 | 将最终snapshot或诊断返回UI，移除“不支持”降级；复用统一提交与撤销入口 | 不能只用“信号计数=1”验收；必须核对源采样、A/V时长、关键帧、保存重开和撤销快照 |

本轮基础恒速顶部拖动复用已存在命令，并采用时间线时长比保留原参数；没有改旧B18函数，也没有修B17命令的动画undo。对主片或链接伙伴带动画/curve/freeze的顶部拖动，先发上述请求、明确提示并恢复UI。以上是B14/B15/B17/B18的实施细化，不重复新增后端任务计数。

### B15/B16 的“新模型”边界

`MoveSpeedPointCommand`、`FadeCurve`、`SetFadeCurveCommand` 是**建议新增符号**，本文不声称当前仓库已有。速度点沿源坐标和积分时基定义；fade曲线沿各自duration的归一化进度定义，两者不能共用一个不明含义的float数组。

当前没有fadecurve模型字段。若本轮UI增加曲线控制，只能展示可review草稿并发出明确请求，由宿主报告不支持；不能直接改图形item曲线而不改真实音视频处理。后续新增请求应明确clip/track/timeline/curve_type/control_points/version与失败结果回写，不能以“signal被发出”验收完成。

## 5. 原W00–W09完成度复评

“已接线”只描述存在消费者或可达入口；安全性和原生对齐分别评估。本表替代“W00–W09全部实施=全部完成”的推断，但不改写原计划历史。

| 原ID | 接线/已有能力 | 当前功能安全与缺口 | Resolve parity判断 | 后续关联 |
|---|---|---|---|---|
| W00 契约 | D1–D5内部规则已写入原文档 | 契约中Replace源In对齐、锁策略等是项目选择，不自动等于Resolve | 需要逐条用20实测/带页码官方依据复核；未确认项保留不确定 | 八项对照表、B14–B16 |
| W01 Replace | 无参按钮→EditPage→view→新命令/宏可达 | mixedFPS/速度点/伙伴/冻结/流安全不足，不能判完成 | 本轮没有证明全部原生Replace定位语义一致 | B03/B04/B05/B06 |
| W02 蓝目的轨 | insert/overwrite/append及drop配对侧消费已存在 | 初始徽章与effective不符属U06；项目状态/碰撞仍有缺口 | 红target与蓝patch必须按本机规则和实际落轨共同验收，不能只测resolver | U06/U09、B09/B11 |
| W03 Keyframe Tray | 基础clip animated_params泳道、add/remove/move桥接存在 | 本轮修对齐/焦点/命中/关闭；Remove+Add仍丢完整key元数据；effect参数轨另有范围 | 基础UI完成不代表全部Resolve keyframe能力 | U10、B12；effect轨另估 |
| W04 Voiceover | toolbar能调用ADR/录音入口 | 返回类型错；完成链缺timeline落clip及timeline/session约束；设备未实测 | 不能把fake start/stop计数当正确Voiceover | B02/B10 |
| W05 Flag | clip字段、菜单请求、view与命令存在 | 混合no-op undo丢旧值；命令validator宽松 | 颜色按钮和单步历史不等于数据安全/全部原生旗标语义 | B01/B08 |
| W06 Position Lock | UI提交入口和service显式policy使用footprint逻辑 | UI异常边界本轮修；service错误契约及policy来源仍需完善；默认无policy不是自动会话锁 | 原生Position Lock允许矩阵需另验，不把内部矩阵当官方规则 | U08、B06/B14 |
| W07 展示保存 | UIState字段、host收集/恢复、view/panel导入导出存在；轨高已有layout链 | custom/坏值本轮修；未知字段/版本/跨project隔离未完成 | 偏好恢复是产品可靠性，不是仅视觉parity | U07/U09、B07/B09 |
| W08 电平订阅 | 原`update_audio_levels`和manager信号保留；重附/stop断订阅测试存在 | 真实stop/pause/EOF瞬时level清零及旧回调generation需验 | 不生成模拟电平；以真实音频生命周期为准 | B13 |
| W09 bind/unbind | 新请求集中绑定消费者、原8个请求连接计数测试存在 | 有消费者不代表回调类型、目标上下文、失败/取消、重复完成均正确；clear通知本轮补 | 生命周期与每个真实工作流需另验，不以generation字段出现判通过 | U09、B02/B06/B10/B14 |

## 6. 实施顺序与分界

**本轮交付路径**：完成U01–U11→真实Qt事件/截图验证→向用户交付可review UI与这份后端计划。遇到后端缺能力，使用明确能力反馈和可取消draft，保持模型不变；不通过UI临时造结果。

**后续后端批次**：

| 批次 | 范围 | 前置条件 | 完成门槛 |
|---|---|---|---|
| P1数据安全 | B01/B02/B03/B04/B05/B11/B12/B17/B18 | 先保留反例和决定Replace/录音契约；不合并无关重构 | 每项新增失败测试先成立，修后完整模型/采样/单步undo恢复；原反例全部关闭 |
| 提交与兼容 | B06/B07/B08/B09 | 确定policy来源、payload归属/版本 | UI/service错误一致；乐观锁先验；旧/未来payload往返；跨project隔离 |
| 录音与音频 | B10/B13 | 录音session与数据事务明确；设备测试环境 | fake完成/迟到/重复回调+真实设备smoke；正确落轨，停止level与真实状态一致 |
| 高级变速 | B14/B15/B17/B18 | 统一速度积分、相位、fps与linked策略；关键帧移动B12 | 完整draft不丢字段；支持矩阵明确；preview/export同源时钟；单步undo |
| 曲线淡变 | B16 | schema/registry/音视频消费者均设计完成 | duration/curve整链可用；实时与离线一致；旧包和默认曲线兼容 |

不可把所有问题都塞进一个“时间线优化”巨型提交。每批按最小可验证内容分拆，Review确认取消/失败/资源清理/兼容路径后再合并。遵循[`.clinerules`](../../.clinerules)和[Edit时间线架构](../architecture/edit-timeline.md)，不在UI线程增加GPU、同步解码或录音重活。

## 7. 复测矩阵与证据记录

| 工作流 | 关键场景 | 必须记录的证据 | 通过条件 |
|---|---|---|---|
| selection/link | 点击、框选、多选、linked开关、Option临时独立、永久unlink/undo | 真实事件、Selection IDs、两段截图 | 真选择与轮廓一致；无残余发光，模型链接和临时交互分开 |
| move/cross/copy | 不同轨高、snap、回原位/Esc、Alt overlap、单边音频blocker、跨轨组复制 | ghost帧/轨/高与提交模型、linked IDs、history | UI预览吻合当前结果；B11后组A/V共同落点或整体拒绝 |
| trim/fades | In/Out、T四区、roll、fade In/Out回原位/Esc、锁/源不足 | before/after完整模型、revision、历史及预览/导出frame/sample | 取消/无变化零写入；duration路径不回退；curve未知能力不假生效 |
| speed dialog/retime | Speed/FPS/Duration联动、全选项draft、cancel、unsupported、Retime菜单/关闭 | native20截图、Topos截图、settings payload、request/结果 | UI信息齐全；高级不支持显式反馈且模型不变；B14后全字段原子执行 |
| Replace | mixedFPS、speedpoints、反放、freeze、linked不同速度/长度、流不兼容 | 源窗与逐帧映射、音频源采样、undo/redo快照 | 源不足/不兼容零写入；有效路径完整采样而非尾帧clamp |
| Tray | zoom/scroll对齐、低ppf密集key、拖动取消、焦点/关闭、Bezier key移动 | 像素坐标与实际keyframe、完整Keyframe.to_dict、曲线采样 | 本轮UI命中和取消可靠；B12后除frame外key元数据完整 |
| Flag | 已蓝+未标记混选、颜色混合、清除、no-op、序列化undo、service非法hex | before/apply/undo/redo模型 | 不丢原旗标；读取未知兼容但新写合法 |
| lock/service | Delete/Cut/Paste、move/trim、pure add、宏失败、stale revision、AI policy | Qt异常、CommitResult、两层状态、history/revision | UI无异常离开事件边界；后端统一拒绝、无部分写入 |
| persistence | custom0/scroll、坏值、未来字段/version、A/B工程同timeline ID、旧包 | 真实host collect/save/load/export payload | 本轮字段恢复安全；B07/B09后兼容及归属一致 |
| voiceover/meters | 无轨/关闭蓝路由、设备拒绝/拔出、停止/EOF、切timeline/工程、迟到take、旧level | manager session IDs、clip落点、asset去重、真实dB | 不崩溃/不投错工程；B10后正确落轨；B13后停止值正确 |

复用fixture：[UI Review工程](../../artifacts/timeline_resolve_ui/resolve_ui_review.topos)、[Backend Check工程](../../artifacts/timeline_resolve_ui/resolve_ui_backend_check.topos)。预览工具：[preview_resolve_timeline.py](../../tools/preview_resolve_timeline.py)。独立预览窗口不是完整host，不能用它证明所有新请求已完成后端处理。

```bash
cd /Users/heng/Documents/vscode/topos-studio
.venv/bin/python tools/preview_resolve_timeline.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_resolve_timeline_interactions.py tests/edit/test_timeline_resolve_toolbar.py tests/edit/test_change_clip_speed_dialog.py tests/edit/test_edit_keyframe_tray.py
```

后端复现命令及JSON详见[独立审查报告](../audit/2026-11-08-timeline-review/backend_findings.md)；拖拽反例见[drag_audit_findings](../audit/2026-11-08-timeline-review/ui_evidence/drag_audit_findings.md)。运行观察探针时要读其输出，不能仅数pass。

## 8. 本轮收尾记录

- **UI实施：U01–U11已落盘。** 保留原配色和电平表；新增/修正的内容包括联动点击与框选、Option独立、clicked primary与属性/效果面板、Link Clips单一勾选入口、矢量图标、跨轨高度/取消、copy ghost、fade手柄与时长反馈、完整变速draft/Retime布局、Tray对齐/焦点/关闭/取消、路由徽章、展示恢复及clear通知。
- U01的鼠标选择与框选保存完整联动组；程序化`set_selection`继续尊重调用方明确给出的selection，保持既有脚本/诊断及“旗标不隐式传播”的API契约。选中互链二片时，属性/效果/标记/Replace/Tray识别为一个编辑单元并使用clicked primary。真正多个独立单元仍按多选处理。
- 新窗口Speed/FPS/Duration、冻结当前值和取消逻辑已完成；带动画、速度点、freeze/ripple的高级提交等待后端B14/B17，不静默忽略字段。比例拉伸的混fps/参数丢失属于B18，尚未修改。
- U08自动化验证了PositionLock下Delete：Qt事件无外泄异常、模型/history不变；键盘入口统一捕获该已知策略异常。未把每个宿主菜单或AI调用都声称为已通过，此类后端边界见B06。
- U11在独立真实素材原生窗口检查了工具层级、图标与Retime布局；变速窗口额外修复了暗色主题下checkbox/radio指示器不清晰的问题，复用了已有checkmark.svg。没有宣称全机型/多屏DPI或每个原生图标逐像素复刻。
- **最终组合回归：286 passed / 19.36s，0 failed、0 skipped。** 覆盖22个focused文件；清单与命令见[最终验证记录](../audit/2026-11-08-timeline-review/ui_evidence/verification.md)。组合之后补清晰的选项指示器QSS、本地化、快捷键回归及超大展示整数的安全钳制；相关专项45项通过（不与组合重复累加）。独立后端61 passed与原Qt19探针保留为不同阶段证据，不累加成虚高数字。
- 本机Topos真实UI：在隔离的真实素材窗口打开速度draft，键入200%后AX确认FPS从25到50，Duration从00:00:02:00到00:00:01:00；Cancel关闭窗口并返回原时间线。Retime布局与linked轮廓已截图观察。原用户Topos窗口未重启，未改动其未保存工程。
- 新增后端反例：[B17速度撤销](../audit/2026-11-08-timeline-review/backend_evidence/speed_undo_probe.py)、[输出](../audit/2026-11-08-timeline-review/backend_evidence/speed_undo_results.json)；[B18比例拉伸](../audit/2026-11-08-timeline-review/backend_evidence/rate_stretch_probe.py)、[输出](../audit/2026-11-08-timeline-review/backend_evidence/rate_stretch_results.json)。两者是已复现缺陷的观察探针，返回0不表示产品正确。
- 本机Resolve20直接操作的已观察项目仅限第1节清单；其他规则按官方来源或Topos代码实测标注，没有全部原生手势自动完成的声明。
- 后端B01–B19：本轮只列计划，不修业务代码；后续完成时须补新提交、反例转回归测试、预览/导出一致性证据后再逐项关闭。

### 用户截图追加回归

用户18:59截图暴露了先前独立TimelinePanel测试缺少真实属性宿主回调的覆盖空缺。新增U12已修复，包含真实macOS复现与完整host测试；不是原B01–B19后端任务。原报告的“UI修复完成”仅代表当时查实范围，此处补记后续发现与修复，不改写历史测试数字。见[追加修复记录](../audit/2026-11-08-timeline-review/ui_evidence/drag_focus_and_chrome_fix.md)。

### 片段手柄追加对齐

用户后续要求统一淡变/首尾/接缝操作，本轮U13已实施查实的样式、鼠标命中与键盘交互修正，保留电平表和原配色。另复现B19并加UI拒绝保护，后端共同delta事务仍由用户后续完成。新增图形是本地矢量重绘，文档区分原生观察、测试验证与未接后端的自由淡变曲线等能力，不将本轮交付描述为全部Resolve操作已经100%等同。

## 11. 播放头与变速条追加（U14）

根据后续要求新增平面播放头、顶部拖速、底部完整菜单入口，以及用户提醒的“打开后片段略增高”。本机Resolve确认顶部向上展开约24px且底边不动，Topos按临时布局投影实现；原轨高不写回。高级段落操作保留显式草稿，后端工作按B14/B15/B17/B18执行。详情与真实Qt/原生观察边界见U14证据。

本轮最终组合验证：**268 passed / 17.29s**，加编译、JSON与diff检查；[命令文件列表、原始输出及原生验证](../audit/2026-11-08-timeline-review/ui_evidence/playhead_retime_controls.md)。与前轮376/122等结果有重叠，不相加。该结果验证所列UI路径，不关闭19项后端任务。

## 12. 后端批次实施记录（B01–B19，2026-11-08 第二轮）

本节为第 4 节 19 项后端任务的完成记录。每项均先以审查反例转回归测试（旧代码上失败、修复后通过），再按批次提交；提交哈希见第 4 节状态列。

**已修复且反例关闭**：B01 旗标快照、B02 Voiceover 契约、B03/B04/B05 Replace 源窗/冻结/流、B06 service 错误契约、B07 未知字段/版本、B08 白名单、B09 跨工程隔离、B11 组复制共同计划、B12 整 key 移动、B13 静音广播、B17 动画快照 undo、B18 时基换算、B19 共同边界计划。

**已实现的新能力**：B15 `MoveSpeedPointCommand`（源坐标移动+互链+混fps积分）；B16 `fade_curve` 模型+命令+视频 render plan/实时/离线/Stem 四路统一消费（默认曲线逐帧/逐样本保留旧结果）；B10 Voiceover 完成会话（project/timeline/track/anchor/generation 快照，守卫不满足时素材保留不落 clip）。

**B14 分阶段**：stage 1（接收链：EditPage→EditController→`speed_settings` 契约/一致性/能力矩阵→`SetClipSpeedSettingsCommand` 单事务：speed/reverse/pitch/freeze/ripple/keyframe_timing 双模式）与 stage 3（`clear_point` 单点删除并入前段速度）已实现；stage 2/4/5（带曲线/动画片段的分段 stretch、freeze/reverse/rewind/ramp/reset 高级事务）仍走显式请求+能力诊断，零静默降级。

**验证**：新增回归文件 test_clip_flags、test_replace_edit_and_voiceover、test_alt_copy_group_plan、test_edit_keyframe_tray(B12)、test_timeline_clip_speed(B17)、test_rate_stretch(B18)、test_position_lock_policy(B06)、test_presentation_state_and_levels(B07/B09)、test_voiceover_completion(B10)、test_audio_playback(B13)、test_linked_numeric_trim(B19)、test_move_speed_point(B15)、test_speed_settings_reception(B14)、test_fade_curve(B16)；既有 focused 套件（flags/audio/DSP/rate-stretch/presentation/interactions/AI service 等）全部通过。

**全量回归**：`tests/edit` 最终 **12254 passed / 15 failed / 43 skipped**。15 项失败经 f1acf2b 基线 worktree 全量对照（基线 20 failed / 12073 passed）逐一归因：14 项在基线全量同样失败（n03_grade_degrade_gate×5、n03_layers_mask_preflight×3、fix_ui2_aud3、linked_ripple_revision_proxy_jobs、timeline_disable_shortcut 各 1，及顺序/负载敏感的 timeline_clip_affordance_gestures×2、timeline_pointer_and_chrome×1——像素阈值断言在全套件负载下抖动，隔离运行基线/当前均通过）；1 项 test_trim_dual_frame（线程时序）在基线全量与本批次第一轮全量均通过、仅最终一轮失败，属双向抖动。**本批次 0 个新增稳定失败**；基线反向 flake 的 import_builder×4、fix_wd13g69×2 在本批次代码上通过，佐证该套件本身存在环境敏感抖动。第一轮的另 10 项失败为本批次行为变更的旧契约测试（B14 接收链替换"永久不支持"消息、clear_point 转为已执行能力、面板绑定新增可选信号），已在 1380e8bf 更新为新契约并全部通过；B14 引入的 PyQt6 receivers 用法与 DummyPanel 兼容性同批修复。

**证据边界（不宣称的范围）**：B10 未做真实麦克风设备 smoke（用 RecordingManager 替身验证完成链，设备回归待用户环境执行）；B03/B05 的解码/导出后果以模型级源窗/流断言覆盖，未逐个启动真实解码器；B16 的 UI 曲线控件按第 4 节边界仍只发 draft 请求（模型与消费者已就绪后不再属于"画假曲线"）；B14 stage 2/4/5 见上。所有既有审查探针（speed_undo/rate_stretch/linked_numeric_trim）在修复后反例断言不再成立。
