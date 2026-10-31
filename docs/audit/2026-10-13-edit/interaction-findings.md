# 剪辑交互代码审查与 focused Qt 事件验证（子任务）

日期：2026-10-13（Asia/Shanghai）。项目根路径：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。

已读 `.clinerules`，仅做只读审查、offscreen Qt 事件/集成验证；未修改生产代码、未启动可见 GUI。不能将本报告的 offscreen 结果称为人工可见窗口亲测。根 agent 另做可见窗口交互测试。测试素材用无媒体文件的 immutable TimelineSequence/ClipInstance/SubtitleClip fixture，足以检验事件路由与模型提交，不据此承诺画面、音频和 GPU 输出正确。

证据：`/tmp/topos_interaction_probe{,2,3,4,5}.py` 和对应 `.json`；代码行号为本次工作区读取版本。建议最终报告把脚本和 JSON 移入项目 audit 附件，保留复现能力。

## 已验证问题清单

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

## 每项详细实施方案及验收

### INT-01 波纹单击修剪

定位：`src/features/edit/ui/timeline_items.py:819-820` 构造时将 `_final_new_start/_final_new_end` 设0；`:4488` press保存原值时未初始化final；`:5580-5607` release仅比较final与original，没有was_dragging/threshold条件。

复现：建立源余量足够且timeline_start非0的clip；R后边缘按下马上松开。首个左单击可能使clip向0延长；右单击把end0发给命令，存在invalid command异常路径。之后普通边缘单击还可复用上次drag的stale final。

实施：每次press将final_start设为当前start，final_end设为当前end；release仅在was_dragging且有效preview与original有差异时发finished。最短时长/源范围在domain builder中验证，不依赖QGraphicsItem值。无变化release清理preview/trim cache，不推命令；失败恢复主/linked/follower geometry并显示诊断。

验收：左右边缘单击/移动阈值内/拖回原位无命令、无revision变更；完成一次drag后再单击无旧值重放；invalid拖动不会抛出未捕获Qt槽异常；单次有效操作只有一个undo。

### INT-02 取消操作的状态机没有接上片段事件

定位：`timeline_view.py:4416` Esc仅非IDLE才cancel；`:3897`换工具同样检查；`:4493-4577`所有begin只处理区域交互；媒体item press/move未begin。`:9457-9516`现有cancel覆盖不全：不重置pending/fades/roll/subtitle copy ghosts，fallback依赖primary_clip_id但未设置。

实施：ClipItem/SubtitleClipItem/TransitionItem/RollHandle统一发interaction_started、preview、finished、cancelled信号；在view的InteractionSession保存完整immutable snapshot和所有affected IDs（主、linked、multi、ripple/sync-lock followers、scoped markers）。Esc/工具切换/失焦/窗口关闭都走单一cancel。cancel先取消item grab与pending状态，再从模型恢复所有preview，隐藏snap/ghost/trim monitor，恢复cache和cursor。release遇到已取消session必须无提交。不要依靠调用_private字段的散落恢复逻辑。

验收：Move、Alt-copy、trim左右、ripple、roll、slip、slide、fade、subtitle/transition拖动分别按Esc；scene=immutable model，undo长度不变；之后释放鼠标不提交；A/B/T换工具、失焦后等价恢复。

### INT-03 手型/中键平移

定位：`timeline_view.py:3903-3904`设ScrollHandDrag，但`:4576-4584`空白左按总先框选；clip按下给item，`:4465+` item无HAND防守；全文件无MiddleButton处理。

实施：在全局press路由中先处理HAND+左键、中键临时平移，避免进入clip/edit handlers。记录viewport pointer与scroll origin；move按像素delta更新horizontal scroll，垂直按照光标所在V/A/字幕区域独立bar；release统一恢复cursor。焦点窗口变化解除pan；header按钮、ruler scrub等允许明确优先级。HAND不应改变selection或模型。可复用QGraphicsView pan但必须拦截item编辑路由。

验收：空白/clip body/字幕/转场上HAND拖动都只平移；临时中键任意编辑工具平移后保持工具；锁轨一样可浏览；scroll有限制无越界；撤销栈不变。

### INT-04 刀片按键与锁轨

定位：`timeline_view.py:4534-4542`按active_tool调用split而无左键判断；`:5199-5246`未检track.is_locked；Shift全轨路径`:5278`已滤lock，局部路径却没滤。domain SplitClipCommand apply也无通用锁守卫。

实施：仅左按钮消费blade；右按钮保留上下文菜单、middle路由平移。所有局部split从同一service检查editable，包括轨道锁、媒体边界、字幕/嵌套/生成器适用性。track resolution失败应诊断或不操作，不能fallback误切别轨。核心命令另加统一锁检查，防菜单/快捷键绕过。

验收：锁轨局部split无变化；未锁正常split精确frame；B右击菜单无模型改变；Shift-B/Shift-click split-all尊重lock；undo恢复links/transitions/markers。

### INT-05 锁轨模型防线缺失

定位：`timeline_items.py:4910-4936`enable/link菜单均不disable；`:4964-4980`speed菜单不disable；`timeline_view.py:10377-10391`toggle、`:10450-10489`links、`:10491-10495`speed无锁；core `commands.py:15964`Toggle、`:16049`SetLinked、`:16206+`Speed apply也不拒绝锁轨。

实施：统一editability resolver返回allowed/reason/affected_track_ids，UI menu/panel/canvas据此setEnabled+tooltip；命令apply对所有直接/linked/重链接旧partner/同步跟随目标实行同一轨锁策略。不把lock检查只放view，否则属性、AI、脚本等仍可绕过。拒绝时整个macro无revision/undo，保留原状态并给可理解的提示。

验收：锁轨Move/Trim/Blade/Enable/Speed/Link/Unlink/Fade/Effect/Crop/Transform所有入口一致；主未锁partner锁时不暗改partner；unlock后工作；菜单灰显与命令拒绝一致。范围由核心审查agent继续补齐。

### INT-06 滚动/缩放

定位：`timeline_view.py:2743-2762`只用angleDelta.y，zoom分支0走zoom_out；未用pixelDelta/x轴；`:2320-2444`set_zoom无viewport pointer anchor。

实施：输入归一化先使用pixelDelta，回退angleDelta/120；无Ctrl水平x优先，垂直y映射timeline水平符合现有习惯；Shift按所在区域垂直；Ctrl只对非零有效zoom delta处理，小delta指数平滑。保存mouse下timeline frame，set_zoom后调scroll使该帧仍在同一屏幕x；zero delta/start/end phase只accept无变化；考虑系统natural-scroll/inverted语义一次，不重复反转。避免增大显示率时每tick重建所有clip。wheel超界留父容器或accept按明确约定。

验收：Mac触控板横/竖/惯性、小delta；鼠标滚轮；Ctrl+zero不zoom；anchor frame误差≤1frame；无意外跳屏；Shift只滚对应stack；125%/200%DPR同效。

### INT-07 空快捷键与可定制范围

定位：`timeline_shortcuts.py:108-117`if not binding退default，无区分“不注册”和“注册但显式清空”；`timeline_view.py:4379+`标记与IO仍有hard-coded fallbacks；`timeline_shortcuts.py:62`N注册为Enable Next Clip，真实handler`:timeline_view.py:300`切Snap。

实施：manager.get_action不存在才用fallback，action存在且binding空直接False；只允许完全key+modifier匹配，不把QKeySequence多步chord每步都当独立shortcut；对M/I/O等移除不必要hard-coded回退，统一action scope，保留未初始化manager场景明确default。Naction label/id迁移为Toggle Snap（兼容旧custom key映射）。公开可定制键应与真正消费一致。

验收：清空/恢复默认/改键/冲突/启动时导入preset，旧键均按预期；未分配键不误触；UI显示N用于Snap；两段chord不会只按任一步就触发。

### INT-08 禁用菜单文案

定位：`timeline_items.py:4910`读未存在的_clip_enabled退True；模型update使用_is_enabled。

实施：使用唯一`is_enabled`状态或公开getter，menu checked/text由当前model/item刷新；enable/disable action与keyboard D共用command。锁轨禁用动作按INT-05灰显。

验收：启用显示Disable、禁用显示Enable；操作后重新菜单立即正确；undo/redo更新；选中多片段用明确Enable Selected/Disable Selected或显示混合状态。

### INT-09 选中去重

定位：`timeline_view.py:5166`Shift框选采用list(existing)+list(new)；`selection.py:116`selected_clip_ids保留duplicates。

实施：TimelineSelection建立identity `(item_type,item_id,timeline_id)`去重不变量，所有factory/merge/from_dict统一；Shift为稳定union、Ctrl为对称差；primary必须存在于items；box新对象排序按track/time。命令builder再防重复IDs，避免恢复不一致。selection indicator数量按unique对象。

验收：重复Shift框选10次仍唯一c；Shift新增和Ctrl切换符合结果；Ctrl+D重复一个对象只产生一个副本；Delete/Copy/Macro无重复项；跨字幕/clip/transition ID冲突也正确。

### INT-10 字幕统一对象定位

定位：`timeline_view.py:12923-12931`Fit只get_clip_by_id；`:12630-12694`边界只track.clips。相比`:12823`content end已遍历subtitle_clips，显示实现不一致。

实施：core/facade公开`iter_timeline_objects(types=...)`与`get_time_range_for_selection`，统一媒体、字幕、caption graphic binding去重、转场等。Fit Selection用统一范围；上下键明确“当前目标轨优先/全部可见轨”策略，字幕边界纳入；marker独立导航不混淆。宽度0/最小zoom做健壮处理。

验收：字幕-only/混选/字幕+caption graphics/locked/hidden tracks的fit和navigation定义一致；字幕100-200，50按Down到100、再到200；上下键不跳无关空白。

### INT-11 轨道删除内容统计

定位：`timeline_view.py:12024`只`len(track.clips)>0`；subtitle_clips/轨转场/绑定未统计。

实施：复用track content summary，对所有可删除内容一致确认；显示媒体/字幕/转场数量与名称；locked track提供解锁后删除的明确流程。删除后caption graphic binding、审阅状态等保留unknown字段/undo由domain command原子恢复。

验收：有cue的轨在取消确认后不变；确认才删除；空轨可免确认；undo恢复cue及关联；A/V/subtitle一致。

### INT-12 变速鼠标修剪

定位：`timeline_items.py:5510-5537`source_delta=timeline_delta；`timeline_view.py:8064`preview同；`:8290`snap后重新直接加delta；`:8478`right同；`:8490`上限只看source_duration忽略source_start。

实施：统一time-map trim builder：时间线frame delta先映射素材fps/timebase、speed、reverse、speed-points/easing。左右新源区间由域模型计算，联动音频按自身time-map；preview与finished都消费相同builder结果，bounds验证source_start+source_duration≤source_total。不能在item复制速度算式。命令中储完整old/new ranges，undo精准恢复；源区间未知用保守边界/用户提示。

验收：speed 0.5/1/2/8、reverse、ramp、mixed FPS、linked音频；2x左裁10timeline帧对应20source帧；preview和提交一致；trim不改变原有剩余画面/速度；导出与preview同最后一帧；不越源尾。

### INT-13 多选属性

定位：`edit_properties_panel.py:2049`暂存所有selected clips，`:2061`set_clip(first)又重置list为[clip]（`:1979`）；`:1721`批量reset用被截短list。

实施：Inspector独立SelectionContext保留完整IDs+primary，set_clipUI只构建primary但不覆盖context。每属性compute common/mixed；混合值显示“多个值”；输入绝对值或相对增量（scale/position尤其需明确）；command层为每个editable clip基于自己old dataclass创建新值，Macro一次undo；异构media类型仅显示共有兼容字段，跳过锁对象并解释数量。

验收：两不同translate/opacity片段，显示mixed；用户设50%同时变两clip、旧参数独立保留；Reset同时复位两个；取消/undo恢复；锁轨对象遵循统一策略；无UI rebuild导致失焦或写错对象。

## 体验摩擦 / 目前缺少的交互能力（需排期）

| ID | 优先级 | 缺少/不完整的功能 | 当前依据 | 详细实现方向 | 验收 |
|---|---|---|---|---|---|
| UI-GAP-01 | P2 | 拖动到viewport边缘自动滚屏及跨区域自动卷轨 | timeline_view/items中没有edge auto-scroll计时器/边界路由；只存在播放时follow | InteractionSession pointer进入左右24-48px边缘带时启动16ms timer；速度按离边缘距离加速，有上限；按固定scene frame/鼠标offset重算preview，保留snap与目标track；V/A/字幕区域各自上下边缘滚对应bar；所有release/cancel/drop/close停timer。无需触发command。 | 按住片段拖到右边缘可以连续移动到原来不可见区域；linked/multi/Alt-copy也不掉；停止/取消即停；无超原帧/误轨道。 |
| UI-GAP-02 | P2 | A/V选中片段键盘逐帧微移与精确Move对话框 | Alt+Left/Right仅_nudge_selected_subtitle_clips；A/V方向键移playhead，鼠标精确移动依赖zoom/snap | action注册NudgeSelected ±1/±10，作用clip/字幕统一selection，domain builder算组的合法delta/locked/overlap/linked；数值Move弹窗可输入绝对timecode或相对frame；一次Macro保留多轨间相对关系；不会误拖或强制缩放。 | 选中A/V多轨组在10%zoom也能1帧移动；碰撞/0边界明确反馈；1undo；快捷键可改；caption binding同步。 |
| UI-GAP-03 | P2 | Slip/Slide的同步画面反馈和四画面Trim Monitor | _on_clip_slip:8613仅logger且注释“could emit Program Viewer”;现有roll monitor但slip/slide缺少可见源in/out对照 | 复用现有trim preview service，通过queued request/session+generation发滑移前后两端源帧、前后邻片段边界，worker解码缓存；节流30/60Hz合并请求，UI只设图像；显示源In/Out、变化量与可用head/tail；cancel立即恢复；preview禁止改undo模型。 | slip鼠标移动时能确认源镜头变化与两端余量，不仅数字；slide可见前后连接；cache命中流畅、无UI decode、过期帧不覆盖新session。 |
| UI-GAP-04 | P2 | 多选属性编辑/混合值/批量效果参数 | INT-13已验证；只有blend_mode controller特判多选 | 依INT-13构建SelectionContext+mixed widget；效果批量须按catalog identity/version/schema兼容匹配，禁止按index错套；新增Paste Attributes勾选Transform/Crop/Speed/Opacity/Effects/Audio/Color，将asset grade/clip grade区别明确；批量Macro。 | 两片段参数独立old值、一次改全；不同effect schema明确跳过；undo、autosave、保存重开一致。 |
| UI-GAP-05 | P3 | 统一交互说明与快捷键提示 | N登记名称Enable Next Clip而执行Snap；timeline class文档承诺middle pan实际无效；硬编码键多于settings可定制 | ActionRegistry同时供应toolbar tooltip/menu/settings/help；显示当前键而非写死字母；按操作context给状态栏可行动提示（Shift暂时禁吸附、Alt复制、锁轨拒绝原因）；统一中英文I18N，包括裸English菜单。 | 改键后菜单tooltip实时一致；无未支持操作宣称；首次尝试用户可通过现有提示完成移动、修剪、snap、copy。 |
| UI-GAP-06 | P3 | 交互对象和焦点的统一命令上下文 | viewer/timeline都有I/O和zoom单键，JKL为window范围；已有owner window guard只保证其他top-level窗不触发 | 保持已有focus-local I/O机制，建立EditCommandContext(source/timeline/program/text-input/active-modal)，toolbar/action读统一context；输入数值/字幕/搜索期间字符键不shuttle；序列切换/嵌套导航恢复selection/playhead/source范围与undo上下文。应主窗口实测后确定具体遗漏。 | source I/O只改source；timeline只改sequence；字幕键入J/K/L/I/O正常输入；浮动面板/工具窗不偷键；当前快捷键与状态可见。 |

UI-GAP-06 属于待真实窗口验证的改进项，不能据代码称JKL一定偷走文本：Qt输入控件可自行接受ShortcutOverride。

## 覆盖与局限

本子任务运行现有7组focused测试：

`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_clip_item_trim_preview.py tests/edit/test_timeline_shortcuts_migration.py tests/edit/test_timeline_context_menu_guards.py tests/edit/test_selection_diagnostics.py tests/edit/test_timeline_disable_shortcut.py tests/edit/test_timeline_transport_shortcuts.py tests/edit/test_trim_professional.py`

结果：57 passed（3.75s pytest执行），PyQt6 6.10.2/Qt 6.10.2。说明常规测试覆盖了基础功能，但未覆盖本次找到的鼠标按下/移动/取消/释放真实链、空绑定、锁轨菜单命令等边界。新增回归应使用QTest事件与真实CommandStack，避免只测试helper与MagicMock误认为交互已完整。

未对真实媒体CPU/GPU画面解码、预览/导出一致性、实际声音、网络/AI服务成功失败、多机位切换、系统DPR/多显示器或外部桌面鼠标人工体验作亲测。源/节目监视器的wheel支持pixelDelta和pointer anchor，时间线却没有，形成可验证的输入设备一致性差异。

## 根 agent 的真实窗口测试新增：Source Viewer 致命崩溃（INT-14，P0）

根 agent 2026-10-13 报告：在真实 cocoa MainWindow 中双击素材打开 Source，`paintGL -> _render_texture -> source_viewer.py:833` 抛 `AttributeError: '_GLWrapper' object has no attribute 'glUniform4f'`，进程退出。真实崩溃日志由根 agent 保存。其后仅在审计harness设置excepthook继续其余交互，生产代码未变，绕过崩溃后的画面测试必须注明该限制。

静态根因确认：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ui/source_viewer.py:85` 的 `OpenGL.GL` import只有`glUniform1i`；`:108-149` `_GLWrapper`暴露相同函数集；`:833`新加入的静态quad shader显示矩形路径调用`glUniform4f`，`:836`Y-flip又调用`glUniform1f`。两函数均未import、未加入wrapper。`initializeGL:571-578` GLSL已声明并消费`u_display_rect/u_flip_y`，uniform location缓存也有`:595-598`，唯独Python桥接遗漏。只修4f会下一行再次崩溃。

证据补充：对Source/Program AST扫描全部`_gl.xxx`使用与`_GLWrapper` class声明，Source缺`['glUniform1f','glUniform4f']`，Program缺项为空。结果`/tmp/topos_source_gl_wrapper_audit.json`。Source的wrapper固定PyOpenGL，与主渲染后端Metal选择不是同一接口；已在默认Metal环境实际触发，但不能写成仅Metal错误。CPU上传/GPU共享只要有效纹理和shader均会走该共同路径。

详细实现：在OpenGL import和wrapper同步补两个API，或复用一个受约束的GL facade避免人工列表漂移。启动时做wrapper能力契约检查并在renderer能力诊断页暴露错误；保留静态VBO+uniform优化，不回退每帧VBO重传。paintGL失败须终止当前绘制、进入明确错误占位/日志并继续可用操作，不能通过宽泛except继续使用部分GL状态，也不能让未捕获Qt槽异常杀死进程。清理/错误不修改共享texture ownership或thread/slot ack契约。

验收：AST/API契约测试调用到的所有_gl属性存在；mock_GL render_texture验证4f/1f参数，两个uniform location=-1也安全；真实QOpenGLContext Source素材首帧、CPU QImage和GPU共享texture、play/scrub/新素材切换、resize/anchor zoom、纵向flip与清理，各不崩、首帧非黑且方向正确。至少macOS默认Metal+Source OpenGL桥接，以及Windows OpenGL各一次smoke。图片、纯音频（无video texture）和失效texture错误占位另测。此P0应先于所有交互体验优化排期。
