# 合成页面 UI / 图编辑 / app 集成审查与优化计划（2026-10-14）

## 问题索引

| ID | 优先级 | 问题 | 证据级别 | 预计工时 / 依赖 |
|---|---|---|---|---|
| U-01 | P1 | 图层/图层栈没有同步回 EditProject | 生产方法独立复现 | 4–8 h；依赖保存契约 U-11 |
| U-02 | P1 | 切合成后共享旧撤销历史 | 真实 page/stack 独立复现 | 8–16 h；与 U-05/09/13 共用 session reset |
| U-03 | P1 | 批量参数防抖混用原值，undo 写坏另一参数 | 真实 page/stack 独立复现 | 4–8 h；与 U-04 共用事务边界 |
| U-04 | P2 | 待提交参数还未入历史时 Ctrl+Z 撤错步骤 | 生产方法即时顺序独立复现，实机时序未测 | 4–8 h；依赖 U-03，计入统一事务修复 |
| U-05 | P1 | 空图载入保留上一合成节点和格式 | 真实 loader 独立复现 | 4–8 h；与 U-02 共用 loader/session reset |
| U-06 | P2 | 保存的 Backdrop/便签载入后没有视觉对象 | 真实 loader 独立复现 | 4–8 h；与 U-05/09/13 共用恢复入口 |
| U-07 | P2 | 节点旁路/启用禁用不能撤销 | 真实 action/stack 独立复现 | 4–8 h；独立命令，复用 UndoStack |
| U-08 | P2 | Project Settings 修改不能撤销 | 真实 slot/stack 独立复现 | 8–16 h；依赖格式元数据提交契约 |
| U-09 | P2 | 新合成继承上一合成节点锁 | 真实 editor/loader 独立复现 | 2–4 h；可并入 U-02/06，勿重复计时 |
| U-10 | P2 | 选择上游/下游只遍历一跳 | 4 节点真实图独立复现 | 1–3 h；独立，可先修 |
| U-11 | P1 | 合成页保存覆盖来源/身份/扩展元信息 | 真实 SaveRouter，捕获落盘前对象 | 8–16 h；第一优先，包含持久化集成回归 |
| U-12 | P1 | 保存查看器状态覆盖节点编辑器/输出目标状态 | 同上 | 4–8 h；依赖 U-11，分 schema 迁移另估 |
| U-13 | P1 | 旧分组视图过滤让新合成节点全部隐藏 | 真实 group view/loader 独立复现 | 2–4 h；可并入 U-02/06，勿重复计时 |
| U-14 | P1 | 替换单输入连线后 undo 不恢复旧线 | 真实 action/graph/stack 独立复现 | 8–16 h；连线事务基础，先于 U-15/16 |
| U-15 | P1 | 多输入断线误删其他视觉线，产生幽灵线 | 真实 Scene3D 多输入独立复现 | 8–16 h；依赖 U-14，包含多输入精确删除 |
| U-16 | P2 | 粘贴已有连接图生成多余的 Connect 历史 | 真实 clipboard/action/stack 独立复现 | 4–8 h；依赖连线事务 U-14 |

工时为单人实现+focused回归+review的粗估；多个项目共享修复时应合并排期，不能把表中数字机械相加。全部属于已证实生产处理路径问题；实机鼠标/窗口、真实落盘及GPU性能限制见下文。

## 审查范围、证据与限制

项目目录：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02`。已阅读 `.clinerules`，本次仅审查和制作复现证据，未修改产品代码。主要覆盖 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/composite_controller.py`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py`、`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/project/save_router.py`。

以下 16 项全部有独立 Python 复现；其中 U-15 包含两个变体。复现使用项目 `.venv/bin/python`、PyQt6 offscreen、真实 NodeGraph / CompositePage / NodeEditorWidget / UndoStack，屏蔽 GPU 求值、素材扫描和重型外部服务。保存问题使用真实 SaveRouter 的更新路径，最后落盘函数替换为返回成功，以避免生成/覆盖用户工程；已证实传给落盘层的对象错误，但未在这个子审查中操作用户真实 `.topos`。

本报告未进行操作系统原生窗口的逐按钮实机点击、真实显示器色彩观感、GPU/视频播放性能测试。某些重现直接调用与 UI 信号相同的 slot/action；本文把“该生产处理路径确定失败”和“用户实际鼠标/快捷键时序已实机验证”明确区分。根任务负责更大范围的自动化测试和测试视频性能基准。历史计划中声称的缺陷没有作为本次证据使用。

### 复现与输出文件

| 文件 | 覆盖项 | 运行方式 |
|---|---|---|
| `/tmp/topos_ui_audit_repro_20261014.py` | U-01～05 | 项目根目录 `.venv/bin/python /tmp/topos_ui_audit_repro_20261014.py` |
| `/tmp/topos_ui_audit_more_20261014.py` | U-06～13 | 同上，换脚本路径 |
| `/tmp/topos_ui_audit_connections_20261014.py` | U-14～16 | 同上，换脚本路径 |

复现脚本不会写用户媒体或工程；offscreen 下无需创建 OpenGL context。日志中的字体/locale 提示是环境提示，不能算产品崩溃或性能结论。

## 功能覆盖清单

| 用户功能域 | 已检查的入口/数据流 | 本次结论 |
|---|---|---|
| 进入、退出、重新打开合成 | page controller → composite controller → load/context/on_enter/on_leave | 查到空图载入污染、历史串图、锁/分组视图串图；真实切页手势仍需实机验收 |
| 节点新增/删除/移动 | actions → page slot → NodeGraph → undo/render | 阅读新增/删除及资源前置清理；本子任务不声称这些所有交互都已通过 |
| 节点属性、批量属性 | debounce、参数提交、批量修改、undo | 证实批量归属错误与 Ctrl+Z 未刷新 pending |
| 节点启停/旁路 | H shortcut、toolbar/property enabled → graph disabled | 状态能改，但没有对应 undo |
| 连线、替换、断开、反拖、多输入 | action/connection signal → page connect/disconnect → model/visual/undo | 证实旧线恢复丢失、多输入模型/视觉不一致 |
| 复制、剪切、粘贴、重复 | clipboard → paste/connect → grouped undo | 证实连线重复进入历史；普通剪切/重复的完整用户旅程仍需实机验收 |
| Backdrop、便签、分组 | model persistence → visual reconstruction、group filter | 证实装饰载入后不显示、旧组过滤隐藏新图 |
| AE 图层视图、图层顺序/可见性/父级 | LayerStackController → NodeGraph serialization → EditProject sync | 证实 live sync 字段白名单遗漏图层数据 |
| 关键帧、曲线、表达式、time range | panel signal → page slot → keyframe/expression model、undo | 阅读绑定与轨道恢复路径；根任务统一跑已有覆盖测试，不能把单元测试等同全部实机功能 |
| 播放头、播放范围、I/O、Edit transport | 独立 comp timer / transport delegate、双向 range sync | 阅读范围、播放/seek 接线；实际帧率/音画同步由根任务性能检查 |
| Project Settings | fps/duration/background/output/Scene3D/Constant 更新 | 证实整项设置没有 undo |
| 工程保存/加载、查看器状态 | runtime serialization → SaveRouter → EditProject/topos | 证实 composition identity/source/扩展元信息被覆盖、viewer 子状态被覆盖 |
| 素材依赖、重链、收集 | panel → AssetRelink/Collect service → undo/publish | 阅读服务入口及错误回报；本报告没有实际收集大文件或覆盖路径 |
| 3D 场景、材质、Outliner、动画信息 | selected Model3D → diagnostics/panels → params | 阅读选择与状态接线；只用 Scene3D 多输入复现连线问题，没有声称所有 3D 工具已验收 |
| 节点预览、Viewer、A/B、ROI、通道、HDR显示 | selected node/output target → RenderRequest；viewer persisted state | 阅读显示/请求路径，发现 UI 状态保存覆盖；像素和性能由其他审查负责 |
| 节点搜索、预设、导航、缩略图、minimap、Perf | registry/catalog/actions/navigation/thumbnail policy | 证实上下游导航只遍历一跳；其他入口作为后续实机 QA 清单 |

## 优先级与实施顺序

- **P1：优先修复**，会造成工程信息丢失、跨合成修改、图连接损坏、画布不可编辑、模型/视觉不一致。
- **P2：随后修复**，撤销行为/导航/恢复体验不完整，仍影响编辑可靠性。
- 排期建议先完成保存与会话边界（U-11/12/01/02/05/09/13），再修连线事务（U-14/15/16），再完成参数及设置撤销（U-03/04/07/08），最后补 UI 恢复/导航（U-06/10）。优先级是对风险的判断，不是承诺工期。


## 详细修复计划

### U-01：图层与图层栈未同步回 EditProject

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/composite_controller.py:375`、`:393`；页面序列化和加载参见 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2244`、`:2327`。
- **触发/复现**：打开一个 Comp Clip，给页面图修改 `layers` 和 `layer_stacks`，调用 `_sync_composite_graph_to_edit_project`。仅这两项改变时，比较逻辑直接返回。复现输出 `layer sync result: None True`，即工程仍是原对象，根本没有获得新层数据。
- **用户影响**：图层顺序、可见性、solo/lock、父子级、名称等不能成为 EditProject 的可靠保存来源。含图连接变动的图层操作可能同步了节点/边，但仍漏 layer metadata，重开页面可退化为无层或旧层。显式合成 Ctrl+S 可能临时保存层，但遇到 U-11 另一类信息损失。
- **根因**：live sync 使用手写 graph_keys 白名单，没有 `layers` / `layer_stacks`，比较和 merge 都漏掉。实现层已经序列化这些字段，不是 LayerStack 不能保存。
- **解决步骤**：1）把 graph-owned payload 字段定义为可共享且有版本的契约，追加层字段；2）merge 中明确支持删除最后一个 layer/stack（不能以字段缺失表示删除，否则旧层永远留下；保存空 dict 或明确 tombstone）；3）让页面首次图层编辑和元信息编辑均通过统一持久化/revision提交入口；4）同步后使 Edit/Deliver frame-provider 指向新工程并失效 comp cache。
- **验收**：真实 LayerStackController 新增两层，排序、隐藏、设置父级、删除到 0 层，离开到 Edit 保存，重开后逐字段一致；只改层 metadata 必须使 dirty/revision变化；undo/redo 和最终落盘的 layer/node topology一致。

### U-02：撤销历史跨合成使用

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2133`、`:1975`、`:829`、`:7518`。
- **触发/复现**：A 图中 node_id=`shared` 的 rotation从 0改为20并提交；load B，B 同 ID 的 rotation=70；Ctrl+Z 对应 `page.undo()`。实测 B被改成0。不同 node_id时，旧 add/remove/connect action也可能操作旧 ID、重加旧节点或成为无效记录。
- **根因**：改变 composite_id只重置 one-shot edit 标志；load、reset_to_empty_workspace、源图切换没有清空或切换 UndoStack，也没有隔离 pending param/batch/nav/interaction。旧 lambda从 `self._node_graph` 解析当前图，跨图使用。
- **解决步骤**：1）新增 composition session切换入口，先提交/取消本图的 pending transactions并停止相关 timer；2）最小修复是新图标识变化清空当前 stack，长期方案按 composite_id维护独立 undo上下文；3）load validation成功后才原子切换 session，失败保持原图/原history；4）scratch reset也清历史、pending、选择、预览、group/lock transient state；5）避免同一身份的重复 load把当前编辑历史无谓删除，定义 reload语义。
- **验收**：A/B含相同和不同 node IDs分别测试；B的Ctrl+Z不能修改A或恢复A节点；回A后按既定策略恢复A历史/清空，行为明确；80ms内切图不会把A pending参数写入B；load失败原图历史可用。

### U-03：批量参数防抖写坏其他参数

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:7063`、`:7091`、`:7149`。单参数已有归属校验 `:6916`，批量分支未采用。
- **触发/复现**：两 Transform节点 rotation原0、scale原1，在一次80ms窗口先批量rotation→10，接着批量scale→2，flush后undo。实测两节点 rotation仍10且scale变0；预期两个可独立撤销步骤，scale第一次undo回1、第二次rotation回0。
- **根因**：已有 pending条目时总保留 orig_old，却把 node_ids/param_name替换为最新操作，导致旧参数的原值套到新参数。切换多选集合也会借用另一集合的 old_values。
- **解决步骤**：1）以 `(tuple(sorted(node_ids)), param_name, interaction/session id)`识别批量事务，归属变化先flush旧项；2）原值从live model写入前捕获、深拷贝并限定在该事务；3）batch/single pending互斥或有统一提交顺序；4）支持 `None`/不存在参数的精确恢复，不用 `old_val is not None`静默跳过；5）绑定真实drag begin/end减少单靠时间窗口合并。
- **验收**：上述rotation/scale复现，重叠/完全不同node集合，单/批切换，颜色tuple/普通float分别覆盖；一次完整拖动是一步，多次不同参数是多步，每个undo精确恢复参数及关键帧状态。

### U-04：Ctrl+Z未先提交待防抖修改

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:7518`、`:7525`、`:6985`、`:7149`。
- **触发/复现**：先完成rotation修改，再修改scale但不等80ms，立即undo。实测undo撤掉rotation，scale仍保留，之后timer/手动flush又给scale创建一个新历史分支。实机快捷键时序未操作，但同一生产入口顺序已复现。
- **根因**：undo()/redo()直接操作stack，pending param/batch没有flush，timer可在撤销后继续提交新action。
- **解决步骤**：1）所有undo/redo入口（快捷键、toolbar、history tree、外部脚本）集中至page事务边界；2）在undo前停止debounce timer并按提交顺序flush param/batch/interaction/nav；3）若仍拖动，定义结束并提交或取消，不让拖动后续tick复活已撤销动作；4）redo前pending新的真实编辑必须明确使redo分支失效。
- **验收**：0/20/79/81ms延迟的快捷undo；当前参数应先恢复；flush后无意外历史条目；redo正确；跨图、关闭页面和拖动中Undo同样覆盖。

### U-05：载入空图保留上一合成内容

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2132`、`:2146`。
- **触发/复现**：先load有节点、24fps、48帧的A，再load `{'composite_id':'empty','nodes':[],'fps':30,'duration_frames':2}`。实测节点仍为A及后加节点，fps仍24、duration仍48，但当前身份已是empty。
- **用户影响**：新/空/旧版本空合成显示上一合成内容，之后graph_changed/保存可能把旧节点写到新的composition ID。即使常规empty Comp Clip factory有默认Constant+Output，合法空图/旧工程/损坏数据仍会到这个入口。
- **根因**：先切身份再检查nodes，空nodes提前return，清图、元数据设置与visual重建都跳过。
- **解决步骤**：1）区分缺失nodes字段（格式无效）与nodes=[]（合法空图）；2）统一validate→construct→commit顺序，合法空图载入空模型或约定Output-only空工作区；3）fps/duration/view状态独立于节点数量恢复；4）失败不更换当前身份，显示诊断；5）last texture/preview清空，避免画面缓存残留。
- **验收**：有图→空图之后无旧节点/层/边，fps/duration为新值；空图save/reload不复活旧图；缺字段/非法结构保持原图；page ID、display、EditProject三方一致。

### U-06：Backdrop/便签只载入模型，没有生成视觉对象

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2274`（backdrops/sticky_notes赋值）；正确重建入口 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:866`、`:880`。
- **触发/复现**：工程payload带1个Backdrop和1条便签，调用load。实测model计数1/1，editor `_backdrop_items`、`_sticky_note_items`均0。
- **根因**：page loader手工逐个add_node_visual，最后赋decorations，未调用已有build_from_graph或相应add_*_visual；存储层有数据但恢复UI漏步骤。
- **解决步骤**：1）先完整恢复graph元数据，再统一 `build_from_graph`，复用批量模式、group/locks/navigation恢复；2）不要同时手工重建nodes/connections再build导致重复；3）thumbnail callback、node state、selection再绑定放在统一恢复完成后；4）保留backdrop包含节点、note颜色/位置/字号和层级。
- **验收**：saved/backdrop/sticky model和visual数量/内容/位置一致；move/resize/edit/delete均有undo；多次load无重复对象；含未知节点或损坏装饰仍有可读诊断，不让整体工程无法打开。

### U-07：旁路/启停没有撤销记录

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_actions.py:1068`、`:1094`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:3625`、`:6414`。
- **触发/复现**：清空undo，actions.toggle_bypass('t')，undo。实测disabled仍True、can_undo=False。属性section的enabled handler同样直接改状态，没有push action。
- **根因**：bypass只更新model/visual/evaluate，未采用page/command统一状态提交入口，批量H也未成组记录。
- **解决步骤**：1）统一SetNodeEnabled/Disabled提交命令，捕获每节点旧值；2）H、菜单、属性标题、脚本等调用同一命令；3）多选旁路一条group undo；4）恢复时同步header、properties、layer row、预览/cache；5）锁节点统一拒绝，不在action已写后slot再拦截。
- **验收**：单节点H、属性启用checkbox、多选混合disabled分别undo/redo；只影响选中且未锁节点；一键H是一条记录；保存重开状态一致。

### U-08：Project Settings不支持撤销

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2824`。
- **触发/复现**：改Output到640×360、fps30、duration5秒→150帧，再undo。实测设置保持640/30/150且history为空。background、Scene3D/Constant分辨率跟随也是直接写。
- **根因**：一项设置同时写page metadata、NodeGraph metadata和若干node参数，没有before/after快照或command。
- **解决步骤**：1）提交前捕获fps/duration/output size、需要跟随的Scene3D/Constant、marked background color与resolution lock状态；2）形成单个ProjectSettings命令；3）拖size/duration按真实交互事务合并；4）undo/redo同步Settings面板、frame range、playhead clamp、output/cache；5）timeline-bound合成继续尊重Edit拥有的格式，拒绝被禁用控件的间接修改。
- **验收**：分辨率/fps/duration/background逐项及组合更改undo/redo准确；用户自建Constant颜色不受背景改色；0-duration及非整数fps边界；保存重开后与视图值一致；修改duration后的关键帧/工作区边界有明确定义。

### U-09：节点锁跨合成残留

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:845`、`:498`；page loader `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2250`、`:2269`。
- **触发/复现**：A节点t上锁，load无viewport_state的B，B同ID节点t仍is_node_locked=True。B没有显式保存锁，预期应可编辑。
- **根因**：clear_all不清 `_locked_node_ids`，page loader又只在非空viewport_state时调用set_viewport_state。正确build_from_graph有按当前graph过滤/初始化锁的逻辑，但这里绕过了。
- **解决步骤**：1）切身份清transient editor锁/selection；2）恢复当前图时使用build_from_graph的规范化逻辑；3）viewport字段缺失/空值恢复默认，不保留上一图；4）将持久化locks归属于composition UI schema。
- **验收**：A/B相同ID、不同ID、B空state、B显式锁集合、非法ID分别测试；B锁只来自B payload；properties read-only与viewer handles一致；同图重建不会意外丢锁。

### U-10：上下游选择只到直接相邻节点

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:3870`、`:3888`；shortcut注册 `:2167`、`:2168`；actions使用 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_actions.py:1378`、`:1400`。
- **触发/复现**：Constant a→Grade b→Blur c→Output d。调用_get_upstream_nodes(d)只有c；_get_downstream_nodes(a)只有b。UI Ctrl+Up/Ctrl+Down调用这些方法，所以“所有上/下游”的能力不完整。
- **根因**：父层在递归前把邻节点加入visited；递归入口发现它已visited立即return，无法继续遍历。
- **解决步骤**：1）删除递归前的预标记，由每层入口标记，或采用deque/set的迭代BFS/DFS；2）统一根节点是否包含、多个起点是否保留当前选择的用户语义；3）仅返回仍在graph中的节点，避免坏边虚假选择；4）深图采用迭代避免Python recursion limit。
- **验收**：4节点链得到3个节点；分叉/汇合/多输入得到完整闭包且无重复；多起点与孤点；伪循环防护；1000级链不递归溢出。

### U-11：合成页保存覆盖composition的身份、来源和扩展字段

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/project/save_router.py:921`、`:928`；graph-only来源 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py:214`；fallback `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/composite_controller.py:915`。
- **触发/复现**：项目已有comp composition，内含comp_clip_id、comp_asset_id、composite_id、source_kind、name、width/height，以及timeline_id/clip_id/source_clip_id与custom_extension；把真实NodeGraph.to_dict的payload送进 `_save_composite_to_edit_project`。捕获落盘函数参数后这些字段全部缺失。保存路径成功返回，更新后的工程已同步callback。
- **用户影响**：Ctrl+S可能失去对应Edit clip/timeline context，重开后进入默认/非timeline路径；名称/格式 fallback变化，未知扩展字段也丢。`comp_clips`容器本身虽然保留，不能代表composition内的来源信息被保留。现有 `test_comp_clip_save_integrity.py`测试保留的是外层comp_clips，并没有覆盖同一composition被graph-only数据替换的情形。
- **根因**：router把新graph整体赋值给compositions[id]，没有merge已有记录；live sync controller已经知道要merge，显式save未复用同一契约。
- **解决步骤**：1）按现有composition为base，覆盖graph-owned字段，保留source/identity/unknown extension；2）composite_id强制使用当前key；3）统一live sync与save所用merge service，避免两处白名单再漂移；4）旧缺信息工程以canonical CompClip/Asset补全，timeline来源不能猜测；5）出错在内存提交前/落盘原子替换前失败，避免“写失败但内存已被改坏”。
- **验收**：timeline-backed/empty/image comp各创建真实工程→合成修改→Ctrl+S→关闭→重开，来源、clip范围、名称、格式、ID一致；注入未知字段往返保留；多个compositions互不覆盖；save失败工程原对象完整、dirty保留。

### U-12：Viewer保存覆盖节点画布和输出目标状态

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/project/save_router.py:922`、`:924`；Viewer state键集合 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py:11055`；editor合并逻辑 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:5180`；output目标保存 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2344`。
- **触发/复现**：graph.viewer_state有selected_node_id、asset_search_roots、output_target_name、output_target_node_id；保存传入独立viewer state `{'zoom_level':1.5}`。最终composition.viewer_state仅这个zoom字段，其他全部丢。
- **根因**：graph UI与display UI共用viewer_state键，SaveRouter所谓“合并”实际是整字典替换。runtime graph获取也直接node_graph.to_dict，未走page.get_composite_data补充当前output target。
- **解决步骤**：1）短期将 `{**existing_graph_viewer_state, **display_viewer_state}`深拷贝合并并指定冲突键所有权；2）调用page的规范序列化入口来捕获output目标；3）长期拆成graph_editor_state / composite_viewer_state并增加schema迁移，读取旧混合字典；4）确保selection仅恢复存在的node，search roots遵守工程可移植路径规范。
- **验收**：设置选中Model3D、Matte/AOV输出目标、资产search roots、viewer exposure/ROI/channel/zoom各项，save/reopen逐项恢复；旧混合state迁移；无Viewer时仅保存graph state，不把其清空；unknown state字段保留。

### U-13：上一合成的group过滤让新图隐藏

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:324`、`:509`、`:845`、`:3648`；page loader `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2178`。
- **触发/复现**：A进入包含b/c的组g，load C仅有node `new-only`。实测group_view_stack仍['g']，new-only visual.isVisible=False，新画布是空的且旧group breadcrumb仍可残留。
- **根因**：clear_all有意保留group filter用于同图undo重建，page却在跨图load同样调用；没有跨身份session reset；逐add视觉执行旧过滤。
- **解决步骤**：1）区别同图rebuild与跨图load：跨图重置group stack/filter/branch isolation/dependency highlights/preview；2）新图如果需恢复group view只从该图persisted state获得并验证group仍存在；3）清breadcrumb并在load结束后同步；4）与U-02共享session reset，避免分别补零散状态。
- **验收**：在A深层组中打开B全图正常可见、breadcrumb root；B也含同名g但不同成员只按B过滤；同图undo/redo保持已进入组；删除当前group/成员有明确返回root策略。

### U-14：替换单输入连线后undo丢失旧连线

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2381`、`:7386`、`:7401`；底层替换 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py:1251`。
- **触发/复现**：a.result→o.image已经连上；把b.result接入同一个o.image。图正确改成b→o；undo后edges=[]，不是原a→o。
- **根因**：NodeGraph.connect自动替换旧input边，page仅记录“添加新边”，没有捕获被替换的边；undo只disconnect新边。
- **解决步骤**：1）connect提交前捕获目标input旧边与visual数据；2）把replace/reconnect定义为一个原子命令，包含removed_edges和added_edges；3）验证新边合法/无环/数据类型后才移旧边，失败保留原状态；4）undo按准确quad恢复旧边，redo准确替换；5）单输入/多输入分别定义，不能多输入每次全清。
- **验收**：a→o替换b→o，undo恢复a→o、redo恢复b→o，model/visual各一条；非法类型/环/锁端点拒绝后原线保留；重复连接不生成历史；连续replace三次逐步还原。

### U-15：多输入断线误删其他视觉连接

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2400`、`:7401`、`:7411`、`:7449`、`:7472`；视觉删除 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py:741`；action `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_actions.py:641`。
- **触发/复现A**：Geo3D g1.display和g2.display同时连Scene3D s.in（max_connections=8）；删g2→s。model剩g1→s，visual却剩g2→s，出现幽灵线且正常线视觉丢失。
- **触发/复现B**：相同两条线，对s.in调用disconnect_by_dst（输入口反拖入口用它）。action先删第一条visual，然后page slot再次按dst删除下一条visual，model剩g2→s，visual为空。
- **根因**：page和undo统一使用“按目标端口删第一条”，不按src/dst完整quad；disconnect_by_dst还违反“page独占visual删除”的信号契约。redo connect/undo disconnect也无条件删目标端口一条，影响无辜multi-input线。
- **解决步骤**：1）新增/复用remove_connection_visual_exact(src_id,src_port,dst_id,dst_port)；2）所有disconnect和undo/redo使用quad；3）disconnect_by_dst只选择要断的具体连接并emit，让page执行唯一删除；4）反拖多输入在有多条线时明确选择哪条，避免静默断错；5）连续事务后model连接集合和visual集合做debug契约检查。
- **验收**：Scene3D 2～8条线，任意顺序断其中一条、undo、redo、反拖分别保持其他边；visual/source port计数精确；到8条满载拒绝第9条且无变化；幽灵线清理不能删除其他线。

### U-16：粘贴图的内部连接提前/重复进入undo历史

- **位置**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_actions.py:907`、`:912`、`:916`；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:2704`、`:2710`。
- **触发/复现**：复制一对已有连接的Constant+Output，paste后历史顶是Paste nodes。一次undo删除新节点，下一条history仍是“Connect result → image”，关联已删除的新ID，成为多余无效步骤。含N条边会留下N个类似步骤。
- **根因**：paste通过actions.connect时page逐边已push_connect_undo；nodes_pasted随后才begin_group，又读取内部connections把它们录一次。新节点和连线未在开始时形成单个事务。
- **解决步骤**：1）paste开始前开启page/command事务，nodes与internal edges统一提交；2）采用已存在create_3d_scene_with_children那类“静默创建+一次汇总”的契约，保证内部connect没有单独记录；3）汇总只记录一次before/after或一次node/edge列表；4）部分失败原子回滚且不留下history；5）load_preset检查相同connect→nodes_pasted顺序并共享修复。
- **验收**：带0/1/20条内部边的粘贴各一条history；一次undo全撤，一次redo全恢复，model/visual无重复边；graph unknown node/受保护节点策略明确；preset load同样一条事务；回滚不留下孤节点、幽灵线或无效undo。

## 仍需实机/集成验收的不足（不当成已测出的新bug）

1. **完整操作旅程验证不足**：现有许多测试侧重数据对象、单slot或mock。例如CompClip保存完整性测试只测外层对象保留，不能捕获U-11。建议新建5条保存/编辑/切页/undo/reopen旅程；步骤要走真实controller和SaveRouter，而不是直接提供已经完整的composition payload。
2. **会话状态边界需要统一**：U-02/05/09/13都来自页面、editor与graph分别保留状态。应小步建立session契约并补focused tests，不做未经测量的大规模重写。
3. **关键帧/图层两种编辑模式的一致性需要实机QA**：selection、row filters、lock、auto-key、expression diagnostics、vector component、Bezier拖动、帧范围、合成/来源时间映射、I/O播放范围及undo group逐项走鼠标/键盘；不以已有unit tests数目替代验收结论。
4. **素材操作的UI响应待测量**：素材依赖扫描/collect在page slot同步调用service，应测大工程/大文件下event-loop阻塞和取消可用性。当前报告未测拷贝GB文件，因此只列验证任务，不宣称具体卡顿时长。
5. **大型节点图恢复耗时待量化**：page loader逐节点add视觉，而build_from_graph已有batch模式；修U-06时复用batch可降低重复包含关系/preview visibility扫描。需分别测50/200/1000节点载入、graph serialization、selection/repaint，给p50/p95和UI heartbeat延迟；不要凭复杂度估计写成实测速度。
6. **本子审查未验证真实显示**：HDR/alpha/checkerboard/AOV/ROI/A-B对比、不同DPI与dock尺寸、输入焦点/快捷键冲突、国际化文本和实际GPU性能纳入根计划的后续人工/集成验收清单。

## 建议回归层次

- 首层：基于3个复现脚本转换为focused test，不重复实现函数本身，断言用户预期最终状态。
- 第二层：model连接/visual连接/undo history/serialization四者一致性检查，真实page+controller+router integration。
- 第三层：Edit→Composite→Edit→save→restart的真实工程旅程，含失败/取消/旧schema/未知字段。
- 第四层：用户提供素材的GPU/视频性能与像素一致性；与本报告headless UI逻辑测试结果分开记录。

所有修复都应按 `.clinerules`：graph/project变更走已有model+command/service，GPU仅通过渲染线程任务队列，schema结构调整带迁移，未知字段保留。完成一项后复跑对应复现与关联测试，再做独立review。
