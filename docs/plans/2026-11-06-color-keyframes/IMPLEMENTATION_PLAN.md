# Color Keyframes 后端接线与验收计划

2026-11-06 · 范围为 Topos 调色页。当前交付包含面板 UI、界面交互和独立演示数据。以下各项是后续实施任务，验收完成后再把真实编辑入口集中到此面板。

当前从调色页 **窗口 → 面板 → 调色 → 关键帧** 打开。标题旁显示“示例”，面板中的播放头、关键帧和轨道开关仅操作示例。当前可用的色轮录制入口暂时保留。`set_playhead(frame)` 可独立回填播放头，`set_snapshot()` 用于切换上下文或替换轨道投影；二者都不会反向发出 seek 或 edit。

## 进度标记（2026-11-07 交接）

| 编号 | 状态 | 交付与证据 |
| --- | --- | --- |
| KF-00 | **模板就绪，待实测** | [KF00_RESOLVE_FIXTURES.md](KF00_RESOLVE_FIXTURES.md)——量测协议（OP-01..OP-16 + 动态权重表）；实测须在装有 Resolve Studio 20 的机器上人工执行，未量测语义在代码与覆盖清单标 `PENDING-KF00` |
| KF-01 | **完成** | `src/features/color/core/keyframe_tracks.py`（轨道三域身份/键型+动态属性编解码/统一权重函数/能力注册表）；[TOOL_COVERAGE.md](TOOL_COVERAGE.md) 由注册表生成（测试强制同步）；`tests/color/test_keyframe_tracks.py` 48 项。提交 19e4f4c |
| KF-02 | **完成** | `src/app/controllers/color_keyframes_host.py` 只读宿主（权威投影回填 + 播放头双向同步 + 身份核对）；`ColorGraphAppHost.bound_graph()`；面板 scrub 手势边界信号。13 项测试。提交 905c14b |
| KF-03 | **完成** | `op_batch_edit_keyframes` 批处理领域命令（一次事务一个历史条目；move 先删后插；冲突/锁定整批拒；no-op 不入史）；面板 EditRequest → 领域语言翻译 + 真实写入口；grade/qualifier 登记字段全量常驻投影。25 项测试（含真实命令栈全链路）。提交 21962fe |
| KF-04 | **完成** | `KeyframeAutoRecorder`（origin 隔离/手势绑定有效帧/Corrector 级开关作用后代）；手势 flush 前并入同一事务（base+关键帧一条历史）。9 项测试。提交 208fcb5 |
| KF-05 | **完成** | 墓碑删除语义（params 显式空覆盖压制 legacy 复活，跨保存/重开/undo）；批处理懒导入 legacy 起源轨道；求值基线保护（播放中保存不固化插值临时值；删轨道恢复基线）；未知字段往返保留。6 项测试。提交 ac345e4 |
| KF-06 | **完成（CPU 域）** | 导出侧窗口动画逐帧求值（`evaluate_mask_tracks_payload` + N03 matte 消费点）；uint8 fast-path 静默丢窗口 matte 的预存 bug 修复；`graph_node_layer_provider(frame)` 无动画节点返回完整 base（此前 None 回退 plan params 丢 legacy 域）；window 域 deliver ✗→✓。5 项新测试。提交 f26fe8c。Metal/AsyncRenderer 时域标志遗留已由复检批次闭环（见下） |
| KF-07 | **部分完成** | 过渡期写权限策略（E09 单路径路由 + 合并读）与发布清单见 [KF07_RELEASE_CHECKLIST.md](KF07_RELEASE_CHECKLIST.md)（含 8 项不等价项与切换门禁）。旧写入口退役待：KF-00 实测 + 真机验收（真实工程写入→保存→重开→全管线导出） |

## 复检批次（2026-11-07）

对上表逐项复检发现并直接修复四项（不等价项 1/2/7 就此消解）。
提交 d65b8f3：

1. **时域标志盲区（KF-06 遗留）**：`key_frame_animated` 只看
   `node.params.grade_keyframes` 且无消费者——legacy 载荷起源的动画
   节点及其下游不绕过节点缓存，静像暂停 seek 会命中陈旧纹理。修：
   编译器判据覆盖 legacy 载荷并沿拓扑传播下游；GL 执行器缓存绕过
   消费该标志。`tests/color/test_color_graph_node_cache.py`。
2. **qualifier 写入口从未生效（真 bug）**：宿主翻译发裸字段名，被批
   命令白名单整批拒（`illegal_param_key`），既有测试零 qualifier 覆
   盖故未暴露。修：命令域一律 canonical 全名（`qualifier.<field>`）。
3. **正式跨片段剪贴板（计划 §edit_requested 后半段）**：复制时宿主
   捕获不可变值/动态属性/能力签名；粘贴优先捕获载荷（源删改/源片
   段离开不失效），跨 context 按轨道名重映射到活动 Corrector，分量
   数不匹配整批拒。
4. **窗口轨道图域写入（KF-03 登记后续）**：`track_name_is_writable`
   谓词准入 + 形状存在性 fail-closed；预览经 provider 帧模式逐帧补
   写形状（与导出同函数）；面板窗口叶轨按「形状 × 登记字段」常驻。
   TOOL_COVERAGE window 域 write ✗→✓。

回归基线：2026-11-06 的 153 项基线保持全绿；关键帧相关回归随复检批次扩至
447 项全绿（2026-11-07，offscreen Qt：基线 11 文件 + 关键帧各套件 + 节点缓存/
full outlets/m06/管线声明/Scope 出口 + 接线相邻 app 套件）。

## 参考行为与验收基线

- 已在本机 Resolve Studio 20 调色页核对右下角面板、左侧轨道树、右侧时间线、Master / Corrector / Sizing 层级、子轨披露、缩放控件和红色播放头。
- 随 Resolve 安装的 *DaVinci Resolve Reference Manual* 第 148 章（印刷页 3382–3392）补足标记和命令语义。圆点表示静态关键帧，菱形表示动态关键帧，Master 汇总折叠轨道里的关键帧。白圆按钮是节点启停，锁对应节点编辑权限，自动关键帧按钮控制动态录制。All / Color / Sizing / EXT MATTE 是**写入范围**，轨道可继续显示。Color 定向当前节点，EXT MATTE 依赖图内外部遮罩，独立 FX 节点拥有自己的汇总轨。框选可一次移动多帧，拖父轨标记可移动同帧子标记。动态属性 Start 改右侧区间的出端，End 改左侧区间的入端。指南的 Sizing 示例可用作复测。 [Blackmagic Design《The Colorist Guide to DaVinci Resolve 20》pp. 356–359](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf)
- 手册还明确了当前播放头是右键添加的位置，静态→动态之间保持，动态→静态之间插值。自动录制需实际改值才创建关键帧。Corrector 的录制开关作用于全部子轨，具体写入落在本次改动的子轨。上一/下一关键帧可用方括号导航。节点徽标在产生实际关键帧调色变化后才显示。
- 在独立 Resolve 测试工程记录实际版本、素材帧率、裁切、当前图范围和前后参数值。待量测项目包括混合键型时的端点取值、Dissolve 滑杆档位与权重、首帧锚点删移规则、碰撞覆盖、锁定子集、跨节点或跨片段粘贴。完成量测前保留兼容语义并在发布门禁标记待核对项。

## 现有链路与实施顺序

| 编号 | 任务与建议改动点 | 验收方法与完成条件 |
| --- | --- | --- |
| KF-00 | 在 Resolve 独立项目建立参考夹具，保存操作序列和测量值。逐个在两条 Corrector、一个 FX 节点、一个外部遮罩与 Sizing 上执行添加、转换、改值、删除、移动、复制、缩放、启停与锁。记录菜单启用条件和实际产生的叶轨。 | 保存每种操作的起始图、目标帧、前/当前/后帧值、菜单截图或录屏、Resolve 版本和色彩管理设置。动态属性额外保存 25%、50%、75% 权重。用本节末尾的验收用例编号关联证据。 |
| KF-01 | 从 [grade_keyframes.py](../../../src/features/color/core/grade_keyframes.py) 与 [color_graph_service.py](../../../src/features/color/core/color_graph_service.py) 提取共同的轨道描述与参数编解码。定义节点、窗口、Sizing、FX 实例的稳定身份，以及坐标域、键型、入出动态属性和参数类型。按下文的工具覆盖表逐项登记读写与求值。 | 生成可审查的工具覆盖清单。每行至少有一个真实字段成功往返，有一个无能力或非法值被拒的反例。为每个未支持项提供可见原因。相同显示名、改名、重排和窗口同形状实例均不会重绑轨道。 |
| KF-02 | 复用 [ColorGraphController](../../../src/app/controllers/color_graph_controller.py) 的会话投影、[ColorSession](../../../src/features/color/core/color_session.py) 的来源身份和 [FileController](../../../src/app/controllers/file_controller.py) 的播放入口。接入只读快照、节点选择和播放头双向同步。此步先关闭真实写入口。 | 用同素材的两个裁切片段与两个 grade 版本验证。切换范围、片段、活动节点或版本后同步更新树、锁、标记、帧范围和播放头。源帧反映到面板，拖面板播放头只走一次已有 seek，迟到回调只会被丢弃。 |
| KF-03 | 将添加、转换、移动、删除、动态属性、启停、锁、自动录制和粘贴转换成领域命令。补齐现有 N10 单参数命令不覆盖的批量、复合路径及非节点轨道。复用 [GraphProjectBridge / GraphTransactionBuffer](../../../src/app/controllers/color_graph_controller.py) 和 [Color Commands](../../../src/features/edit/core/commands/color_commands.py)；同一用户动作的 base 参数与关键帧提交为一个历史条目。 | 两个节点选中多个帧移动或粘贴一次后，工程历史增加一条。Undo/Redo 恢复所有目标和输出像素。制造一个锁定目标、过期 revision 或目标帧冲突，确认事务无部分写入，面板从权威快照恢复。 |
| KF-04 | 给现有调色工具参数提交链加统一自动录制路由。按手势变化记录字段集合，手势结束取最后值与有效帧，通过同一命令提交。播放器回填和程序化控件更新由明确 origin 和状态锁隔离。 | 调整曝光、改曲线、拖两个不同窗口、改插件参数分别验证每次手势只生成一条 Undo。关自动录制后按 KF-00 量测规则更新已受控区间；反复 seek、播放、换片、Undo、加载及只读投影均不产生关键帧。 |
| KF-05 | 在 [ColorStack / ColorGraph](../../../src/shared/project/color_stack.py) 的现有工程快照内版本化。改造 [Legacy Migration](../../../src/features/color/core/color_graph_migration.py) 与 [Hydrator](../../../src/features/color/adapters/color_stack_hydrator.py) 保存边界，完成一次性导入、删除屏蔽、未知字段保留和较新版本兼容诊断。 | 老项目打开前后同帧像素一致；删除迁移来的最后一帧后，保存、重新打开、换版本、撤销、重做都按预期处理。含未知字段和高级图结构的工程往返比对保留语义。 |
| KF-06 | 将各轨道的逐帧求值统一投影到预览、回放、Edit 监看和 Deliver。接入 [AsyncRenderer](../../../src/features/color/core/async_renderer.py) 现有任务队列与不可变图快照，覆盖 GPU、CPU 及 Metal。完善 [TimelineExporter](../../../src/features/edit/core/timeline_export.py) 的多节点完整消费和能力预检。 | 两个 Corrector 同时动画并含窗口、限定器、Sizing 和 FX。逐帧比对暂停、播放、Edit、导出，确保没有只消费首个动画节点、错误取全局帧或沿用上一帧缓存。记录逐帧参数、输出差异、能力诊断和 4K 性能对照。 |
| KF-07 | 所有当前可用工具通过上面验收后，切换到唯一真实面板入口。迁移色轮旧按钮和各处直接写入，复用统一服务保留工具值编辑、键盘命令、兼容脚本及节点徽标。移出演示数据和旧快捷键重复入口。 | 用菜单、面板、键盘与支持的控制表面各操作一次，同一操作只提交一次。验证老工程、项目恢复、剪贴板、调色复制、旁路、锁和导出诊断。将测试证据和剩余不等价项写入发布清单。 |

## UI 与宿主契约

现有 [视图 DTO](../../../src/features/color/ui/color_keyframes_view_state.py) 是显示与操作请求边界，宿主按 [面板](../../../src/features/color/ui/color_keyframes_panel.py) 的最终接口注入和接收。生产路径应由宿主创建实际快照，演示 fixture 只用于 UI 手势检查。

| 界面接口 | 宿主实施 |
| --- | --- |
| `set_snapshot(Snapshot)` | 把当前 `session_id`、grade 范围所有者、`stack_id`、版本和 generation 绑定为不透明 `context`，另存期望的 `graph_revision`。投影 frame_count、当前帧、帧率、当前 Corrector 及树。Track ID 要从稳定实体和工具域合成。Master 与父轨由叶轨按帧汇总，混合键型和锁状态明确规则。共享定义跟实例分开，分支按图可见顺序显示。只有一个视频帧或无视频上下文时，按 Resolve 基线与产品模式给出空状态或只读原因。 |
| `seek_requested(int)` | UI 入参是 clip-local 帧，复用 `on_video_seek` 或已存在的 Color 拖动协议。宿主负责合并频繁 move 并保证 release 的最后一帧送达。播放帧回写只刷新播放头，避免重新构建轨道树或触发 seek 回路。更换片段或页面时丢弃旧 drag、旧请求与订阅。 |
| `edit_requested(EditRequest)` | 解析 `context`、动作和 `(track_id, local_frame)`。先通过当前快照检查会话、权限、目标和冲突，再展开至领域目标，提交并重新投影。当前 UI 的粘贴请求可携带 `sources` 与 `source_context`。正式跨片段剪贴板需在复制时向宿主提交来源请求，立即解析并保存不可变值、动态属性、相对帧偏移和能力签名。后续粘贴用已捕获的载荷，避免读取已经删除、切换或改值的源。 |
| `expand_requested(bool)` | 现有工作区已监听此信号来调整面板宽度和还原尺寸。后续继续复用 Color 页面 dock 注册与布局存档。对旧布局、浮动窗口、页面切换和窄宽分别验证。展开、缩放和树折叠属于界面状态。 |

回填只在宿主确认当前会话后执行。切片段、节点范围、版本、项目、Undo/Redo、工具或图命令提交成功都应刷新对应版本的投影。接现有 `ColorGraphAppHost.on_external_project_change`、`on_clip_stack_loaded` 与项目 publish 路由，避免新增定时轮询。版本激活同时核对 [ClipVersionRequestHost](../../../src/app/controllers/clip_version_host.py) 与播放器当前 clip，首帧出来前可读目标的静态树，允许编辑前必须确认绑定完成。

## 轨道、坐标与命令规则

1. **稳定身份。** 领域键包含 scope/owner、stack、compound path、节点或 Sizing 槽、工具域和参数 ID。窗口键加 `shape_id`，FX 加实例 ID、参数 ID 与 schema 版本。Corrector 编号、节点标题、翻译后的工具名、树行号只用于显示。Master 和父行通过子轨得到时间上的并集。写操作解析到叶轨集合，并对同一参数和帧去重。默认锚点可标注为虚拟标记，只有必要时才物化真实数据。
2. **独立坐标。** 面板显示 clip-local 整帧，合法范围 `0..frame_count-1`。播放头命令使用时间线坐标映射，现有图/Layer 轨道继续以 source frame 为兼容域。使用 [Timeline v2](../../../src/features/edit/core/timeline_v2.py) 的 `clip_local_to_source_frame` 等映射。校验 `effective_source_start` 偏移分支与反放、变速、冻结和非整数帧率的组合。反向投影时裁切之外的点保留在数据中，显示时过滤，避免多个离屏点被 clamp 到首尾。Timeline grade 等没有单一源资产的轨道使用明确的时间线域与原点。给 Sizing 每个槽定义所属坐标，再接命令。
3. **非一一映射。** 慢速、混合帧、冻结与速度曲线可能使多个 local 帧映射同一 source 帧，或使源帧从可见范围内跳过。保留 key identity 并明确显示重复映射规则。移动时先按 local 偏移生成全部目标再转换，发现不同选中键落到同一领域帧时整批拒绝或执行 KF-00 证实的显式覆盖策略。禁止用 `source_start + local` 作为通用捷径。时间码走 [Timecode](../../../src/shared/media/timecode.py) 的 Rational 与 DF/NDF，UI 整数帧率字段不足时扩展投影。
4. **操作范围和权限。** 全局添加命令根据 All / Color / Sizing / EXT MATTE 解算叶轨，右键单行添加定向该行和播放头位置，父轨操作展开指定帧的子键，框选移动针对所选范围。应用级删除当前帧、删除全部和右键删除所选应为不同命令。调色复制按当前模式筛选 Color/Sizing 内容。节点锁、栈锁、轨道能力与临时只读是不同状态。轨道白圆对应启用，保持其关键帧数据和节点拓扑可恢复。已锁定节点的解锁走已有锁策略。对于 scope 内部分锁定，采用 KF-00 实测的跳过或拒绝规则，宿主返回具体目标与原因。任何可提交批次一律原子化。
5. **命令与手势。** 现有 `op_set_node_keyframe`、`op_move_node_keyframe`、`op_remove_node_keyframe` 可作为子能力，新增单个批处理 operation 统一校验后生成一个不可变候选并 commit 一次，避免循环提交子命令导致 revision 多跳。开始拖动存目标集合和 revision，移动中只做临时投影，release 只提交一次。Esc、失焦、切页、版本更换取消草稿。批量 move 先从候选移除所有源帧再插入目标帧，避免相邻帧交换时覆盖自己。空动作、重复置值和拖回原位不推进历史。
6. **键型和动态属性。** 建议持久化显式 `kind`、`start`、`end` 与已迁移的旧插值模式。旧 `hold`、`linear`、`ease` 在首次打开的帧值必须保持一致。新数据按前键决定区间语义，静态点右侧保持，动态点右侧默认线性插值，即使后一键为静态。求 `A→B` 时读取 A 的 Start、B 的 End，使用统一权重函数。已有 [keyframe_curves.py](../../../src/features/color/core/keyframe_curves.py) 的 Bezier 求解可以作为工具，Resolve 滑杆数值到曲线的映射需先验证。转换键型保留原值和帧号，明确取消旧 dynamic attributes 或恢复默认的规则。端点值、时间单调、有限值和类型能力必须可测试。
7. **录制与取样。** 开启 auto 时依据当前真实帧和参数差值建立动态帧，同一手势绑定 clip、node、起始 revision 与目标帧。Corrector 开关一次修改后代录制状态，真正写入只发生在用户改动的参数。分组添加静态/动态键时，从当前帧一致地采样整组真实参数并一次写入。关闭 auto 时对已动画轨道按参考行为修改当前或邻近已有锚点，静态区间由前键控制。按手册核对工具回显的锚点值与渲染的插值结果，允许两者采用不同读法。录制从初始化、工具回填和 render 求值路径隔离，程序化更新加 origin/状态锁。
8. **剪贴板。** 对复制的值、类型、动态属性、工具域、参数版本和相对时间生成深拷贝。粘贴前验证目标类型、权限、帧边界和目标冲突，并支持源片段离开后粘贴。显示 ID 和 frame 只用于第一次取样。节点/版本之间无法无损转换时清晰拒绝，整批工程历史只增加一条。Resolve 的可操作命令以 Color 页第 148 章和 KF-00 记录为准，避免套用 Edit 页曲线编辑器语义。

## 工具覆盖表

先从实际启用的 Color 面板、图节点类型和 Effect Catalog 生成全量清单，再按下表归并到 Resolve 对应的轨道。每个字段登记 `read`、`validate`、`write`、`evaluate`、`preview`、`deliver` 与 `copy` 能力。类型包括连续标量/向量、角度、颜色、离散枚举、拓扑对象和资源引用。只对明确可动画的字段开放操作。

| Resolve 轨道与 Topos 来源 | 后端落点 | 关键验收 |
| --- | --- | --- |
| **Color Corrector**。Camera RAW、色轮、Log/HDR 对应工具、色调饱和度、各类曲线、Soft Clip、通道混合、色彩映射/Warper，以及归入该轨的色彩工具。 | 从 [LayerController](../../../src/app/controllers/layer_controller.py) 的 grade/mixer/curves 入口映射到图参数域。复用既有 grade 白名单，规范 `temp/temperature` 等别名。曲线持久化控制点，按求值结果生成 LUT，同拓扑按稳定点身份插值，拓扑变化采用实测分段策略。RAW 字段先验证逐帧解码能力与节点归属。 | 用非默认曝光和色轮向量验证端点、区间和连续两条节点。加删曲线控制点后比对参考行为。未编辑的 Mixer 字段及其他曲线保持原值。 |
| **Window 子轨与 Tracker**。线性、圆形、多边形、自由曲线、渐变以及该节点实际可用的 Window 实例。 | 把 `mask.<shape_id>.<field>` 接到节点实例。独立保留 [TrackingStore](../../../src/features/color/tracking/tracking_store.py) 数据，明确跟踪结果、人工修正与关键帧的合成顺序。形状删除、复制和轨道随同一事务处理。 | 同形状的两个实例均能独立动画。跟踪完成、手工修正、移动关键帧、关闭窗口、重开工程和导出后，逐帧核对形状坐标、遮罩和合成输出。 |
| **Qualifier 与 Misc Isolation**。限定器、适用隔离工具和外部遮罩。 | 复用 `qualifier.<field>`，补齐不止现有的 H/S/L 字段，连续阈值守区间约束，启用/反相/模式作离散。外部遮罩的 Freeform Isolation 轨单独登记时间偏移与 Sizing 能力。 | 用连续阈值与离散反相验证同帧 matte、最终像素及 Alpha 输出。外部遮罩缺失或无法导出时返回具体诊断。 |
| **Defocus / NR**。Defocus 对应模糊、锐化和 Key 参数，NR 对应空间/时域降噪与 Motion Effects，另对照 Topos 去模糊和动态效果。 | Key 链路在同帧消费遮罩、增益、反相与实际节点不透明度。从工具命令映射到 Corrector/FX 或 clip 级域。时域参数在同帧快照内变化，时域历史窗口包含版本、源帧与参数依赖。 | 对两处关键帧之间暂停/前进/反向 seek，检查效果不会沿用前一个 revision。比较播放与随机定位所得帧及 Alpha。 |
| **Open FX 与独立 FX**。正确器附着效果、独立效果节点、自定义插件和在 Topos 开放的效果实例。 | 使用 Effect Catalog/Registry 的参数 schema 和实例 ID，读取实际 animatable 能力。插件缺失或升级时保留未知载荷和诊断。文件路径及模型选择仅在明确支持时以工程资源 ID 处理。 | 同类型两实例互不覆盖。版本升级、参数删除、重新加载或取消导出时状态一致。只有预览支持的参数在导出预检可见。 |
| **Node Format / Sizing**。节点或片段的变换、裁切、Input Sizing、立体模式的 Convergence、Float Window、Auto Align，以及 Timeline 模式的 Output Sizing。 | 先登记 Topos 实际支持，明确 Input/Output/Node/Edit Transform 各自的处理阶段和坐标系，防止同一变换应用两次。像素稳定矩阵沿用现有管线。 | 在同一片段同时启用 Edit Transform、Color Sizing 和稳定后检查空间顺序。切 Timeline 模式再验证 Output Sizing，检查 DF/NDF、裁切、冻结和工具能力。 |
| **其余调色功能**。LUT、颗粒、依赖型 AI 与未映射工具。 | LUT 强度可按数值能力登记，LUT 资源切换、AI 模型和工程设置以离散能力与资源闭包评估。静态操作也应在能力表明确归属和原因。 | 按 Color 页工具按钮逐项检查覆盖记录。每个可关键帧工具至少有一段能复现的参数变化和输出证据。 |

## 持久化、渲染和旧入口退出

- **迁移与删除。** Layer 载荷和节点 `params.grade_keyframes` 并存时，当前 [参数投影](../../../src/features/color/core/color_graph_param_source.py) 仅在非空情况下覆盖旧轨道。若删除的是最后一个点，旧 Layer 载荷里的同名轨道可能再出现。首次水合时将可识别轨道迁至权威域，保留来源和未知键；迁移前若必须共存，以显式空覆盖或墓碑解决删除语义。迁移幂等，取样无副作用。检查 `reconcile_projectable_graph` 的旧写通道回收，不允许序列化活动播放帧插值后的临时值覆盖 base。锁、旁路和 FX schema 版本随既有持久化设计处理，自动录制与布局状态的持久范围按 KF-00 核验。
- **导出能力。** 当前 `TimelineExporter._get_clip_color_grade` 只允许可投影的串行图走 legacy 路径，复杂图会显式拒绝。`_grade_from_stack` 还只取活动层或首个有轨的 Layer。接真实数据前给多个节点提供按原执行顺序的完整快照求值，走现有图编译和执行能力预检。需要新增时域支持的后端继续给出明确阻断原因，禁止生成缺效果的成片。保留 [Graph LUT Export](../../../src/features/color/core/graph_lut_export.py) 的时域诊断；静态 LUT 无法表达运动，若未来要导出某一帧，需明确作为采样功能验收。
- **帧快照与缓存。** Render worker 读取 `ColorGraphSnapshot` 与明确的源帧/时间线帧映射，通用 evaluator 返回新的参数覆盖。检查 `graph_node_layer_provider(frame=...)` 带动画与无动画节点的混合案例，未参与求值的节点仍须得到完整 base 参数。通过 [编译](../../../src/features/color/core/color_graph_compiler.py)、[OpenGL 执行](../../../src/features/color/core/color_graph_executor.py)、[CPU](../../../src/features/color/core/color_graph_cpu.py) 和 [Metal](../../../src/features/color/core/color_graph_metal.py) 共享时域标志。时域输入及依赖的下游节点按 revision、帧和资源指纹取缓存或显式绕过。播放只在画面有效时发布，不让旧 clip/grade 版本的任务覆写新帧。GPU 创建、释放与取消走已有队列。
- **旧入口。** [色轮](../../../src/features/color/ui/color_wheel.py) 的录制、快照、删除按钮依赖 [MainWindow](../../../src/app/main_window.py) 和 LayerController 的接线。Window 的录制依赖 ColorController 读取色轮录制状态。等统一命令、数据迁移和真实播放验收通过后，一次切换这些写入口，保留过去工程的可读性。查看 Viewer ruler、节点徽标、Clip Strip、工具重置、脚本 API 和调色复制的消费者，改为读同一投影或写同一服务。对旧 UI 与新 UI 同时出现的过渡版本，明确哪个入口有真实写权限，防止双录。

## 可复现的验收清单

| 用例 | 夹具与操作 | 核对结果 |
| --- | --- | --- |
| V01 · 写入范围 | 24 fps 的 120 帧素材建两条 Corrector、一条 Sizing、一条外部遮罩与独立 FX。在 12、36、60、84 帧切换四种 scope，从面板与应用级命令添加键。 | 展开正确的叶轨，Master 始终汇总可见与折叠轨。按同一帧回读工程，确认实际改动目标和 Resolve 记录一致。 |
| V02 · 动态/静态 | 为曝光或 Sizing 设置两端，分别测静态、动态、混合边界和自定义 Start/End。在关键点前、关键点上、关键点后、1/4、1/2、3/4 取样。 | 静态区间保持约定端点。默认动态在正确的帧域呈线性。非对称 Start/End 分别控制两侧，转换与 Undo 保留端点值。标记、对话框图形和逐帧值一致。 |
| V03 · 手势与锁 | 父轨移动同帧多叶轨，框选两个节点的多个关键帧拖动，复测碰撞、边界、锁、Esc 和切片段。再粘贴到兼容与不兼容目标。 | 单手势单提交、无部分写、Undo/Redo 后再打开工程结果一致。锁/能力原因对用户可见。取消、过期和无动作提交不改工程。 |
| V04 · 时间映射 | 同一素材设两个片段，普通片段 `timeline_start=240`、`source_start=100`，local 12 对应 global 252/source 112。另测前后裁切、滑移、2×、0.5×、倒放、冻结、23.976 与 29.97 DF。 | 面板 seek、播放、实际 decode、轨道读取和导出都使用正确帧域。离屏源帧仍保存，移动映射冲突可检测，过 DF 分钟边界的时间码正确。 |
| V05 · 旧工程与版本 | 在迁移夹具放 Layer grade、qualifier、mask 轨道，另加节点权威同名轨道和未知字段。删除最后一个旧轨道点，切 A/B 版本后保存，再重启。 | 迁移前后同帧参数和画面一致，删除不复活、版本隔离、未知字段保留。重复保存没有漂移或空 revision。 |
| V06 · 渲染与导出 | 两条动画节点、一条恒定节点、一条遮罩、Sizing 与 FX。逐帧记录 Color 暂停、顺序播放、随机 seek、Edit 监看与 Deliver 无损输出。分别启用适用的 OpenGL、Metal、CPU；再关闭某后端能力。 | 参数值精确匹配；像素比较使用仓库后端测试的容差与相同色彩管理。记录不同后端的最大误差、平均误差和异常帧。缺能力时预检指明轨道与原因。缓存、取消和切片段不产出错帧。 |
| V07 · UI 回归与集中入口 | 用旧布局和默认布局启动，中英文、宽/窄工作区、浮动与切页。打开真实面板，用工具调值、快捷键、工具重置、节点启停与锁、切换版本。 | 控件互相回显且每次只提交一次；旧调色动画可读、其它页面布局正常。演示 fixture 不进入生产数据或工程。 |
| V08 · 性能 | 以同一台机器、相同媒体与播放路径比较接线前后 4K。测空轨道、常见数量轨道与重场景（例如 20 Corrector、200 可见组/子轨、10,000 关键帧）。观察打开/关闭面板、滚动和 60 秒播放。 | 记录 UI 帧耗时 P95、播放掉帧、seek 延迟、缓存命中及内存；无轨时走快路径，播放回填只更新必要区域。渲染提交仍为现有线程模型。对有显著退化的场景补 profiling 后再发布。 |

从仓库根目录运行现有回归。新用例补到相应测试层，应用集成测试使用现有项目/播放夹具，后端能力条件跳过的用例仍须在对应硬件上实跑并保留记录。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/color/test_grade_keyframes.py \
  tests/color/test_color_graph_keyframes.py \
  tests/color/test_color_graph_qualifier_tracks.py \
  tests/color/test_tracking_keyframe_bake.py \
  tests/color/test_color_graph_migration.py \
  tests/color/test_color_graph_cpu_parity.py \
  tests/app/test_color_graph_clip_switch.py \
  tests/app/test_color_graph_save_reconcile.py \
  tests/app/test_color_graph_export_preview.py \
  tests/app/test_color_graph_production_switch.py \
  tests/edit/test_color_stack_preview_grade.py
```

2026-11-06 在 macOS 以 offscreen Qt 运行上述基线，153 项通过。这组测试作为后续接线前后的回归基准，V01–V08 是新增验收。

交接时按 KF-00 至 KF-07 标记进度。每项附代码入口、命令执行结果、实际操作录屏或截图及待处理差异。旧入口退出以真实工程写入、保存重开和全管线导出验收为前置条件。
