# Color Keyframes 后端缺口与整改计划

2026-11-07 · 配合 [原实施计划](IMPLEMENTATION_PLAN.md)、[Resolve 量测协议](KF00_RESOLVE_FIXTURES.md)、[工具覆盖清单](TOOL_COVERAGE.md) 和 [发布清单](KF07_RELEASE_CHECKLIST.md) 使用。本表记录本轮复核得到的新证据。已有单测通过代表相应的测试路径可用，不能替代真实工程、各工具和各后端的对标验收。

Resolve 实测使用用户原工程当时打开的片段，在独立工程 **Topos KF Parity Audit 2026-11-07** 操作后切回原工程。下表时间码均为当时关键帧面板的 clip-local 显示。参考为 **DaVinci Resolve Studio 20** 调色页及同版本手册第 148 章。About 的完整 build、工程帧率、色彩管理和自定义动态权重尚未采集，仍是对标证据的待补项。Topos 本轮初始回归为 **207 项通过**，修复后，面板、宿主、图命令、持久化、导出与播放坐标相关测试合计 370 项通过。 [Blackmagic Design Colorist Guide](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf)

本轮已修复启动后宿主接线、真实工作区使用演示数据、活动 Corrector 同步、轨道分组和删除末键后空轨丢失。普通 1× 同速率片段接入源帧偏移，复杂映射及非 Clip 调色范围显示只读。时间码使用时间线名义帧率，失效上下文清理投影。另已修正嵌套关键帧载荷的 Mapping 解冻及内部静态键的精确帧边界。动态属性对话框使用合法的 0–3 值域和 Topos 兼容权重曲线。松手会经正式 seek 结算精确帧，取消手势只结束 scrub。轨道右键可左右微移一帧，Master 菜单可整批删除当前剪辑范围内可见键。锁定键存在时禁用整批删除，裁切范围外的源键保留。下面任务保留仍需完成的范围。

**R 批次整改（2026-11-07 第二轮，提交 2c51b69…0a7edcf）已消解 R2/R3/R5a/R5b/R6 全部代码项与 R1/R4 的可代码化部分**：显式 Dynamics 经 KF-01 编解码贯通求值域（0→1 Start=0/End=1 的 25% = 0.109375，legacy 帧值逐位不变）；批编辑无损化（只触及目标轨、未知字段保留、触及不可解析整批拒）；播放任务过期帧按实际解码帧覆盖（动画不停旧值）；Edit/Deliver CPU 回退按权威图逐节点合成（run_graph_frame_cpu 生产 oracle，能力不足默认拒绝）；Qualifier/Window 工具手势接入自动录制；手工 Add 采样当前求值结果；复杂映射只读矩阵 + DF 时间码（Timecode Rational 接口）；图宿主绑定描述 + A→B→A 迟到请求失效。回归扩至关键帧/导出/预览/录制相关 300+ 项通过。

**复审批次（同日第三轮）追加修复四处**：① 节点缓存键并入任务逐帧身份（R5.3"静态上游对变化视频纹理误命中"——播放期缓存参数携带冻结快照，静态节点跨解码帧命中旧输出；`_graph_cache_context` 并入 RENDER_VIDEO_FRAME 任务边界写入的实际解码帧，暂停态缓存行为不变）；② 图 CPU 路径运行期异常策略——Deliver fail-fast（带 clip 诊断，不静默缺效果）、Edit 预览告警降级扁平（成片路径不受影响）；③ 图执行分支的调色链段 timeline_id 与扁平路径同源（`_sequence`，此前误用不存在的 `_timeline` 属性会导致时间线域链段丢失）；④ compound 成员的手势录制——录制身份含 compound 路径，flush 按路径分组下行（此前成员批命令不带路径被 node_not_found 整批拒、录制静默丢失）。复审判据核对另确认一项既有范围缺口：关键帧面板投影只遍历顶层节点（compound 成员不进面板树——成员级手工编辑不可达；工具手势录制经路径分组已闭环，面板投影随 R7 槽位域一并登记）。下面各任务保留仍需实机/量测/后续版本的范围。

## Topos 实机记录

在 macOS Cocoa 启动正式 MainWindow，导入用户提供的 [测试素材01.mov](../../../tests/test_videos/测试素材01.mov)（H.264、1920×1080、25 fps、124 帧），插入临时时间线并添加 Corrector。通过正式停靠面板和 Qt 鼠标、键盘操作提交真实工程图命令。普通片段在 local 24 写入 source 24。经 Edit 的 TrimClipCommand 裁掉开头 12 帧后，在 local 24 写入 source 36。两次均看到静态键回显、local 47 动态键拖到 53 后保持选中、锁定时 Delete 无效、解锁后删除成功且 24 帧静态键保留。Resolve 参考窗口显示 Apple ProRes 4444，因此本轮对比操作和轨道结果，未作两个文件的像素等价判定。

工程模型的独立复核已用真实 `.topos` 文件验证 legacy 末键删除后保存重开不复活，A/B 版本隔离及 Undo/Redo 通过。完整播放/导出覆盖见下方验收状态。另做离屏 20 Corrector、200 叶轨、10,000 键测试，550×380 面板首次投影加绘制约 227 ms，60 次播放头回填并抓图的 P95 为适应缩放 1.44 ms、最大缩放 0.53 ms。该测量仅覆盖面板，完整 4K 播放的 V08 仍待验收。

## 优先级与交付边界

| 优先级 | 任务 | 前置与合并条件 |
| --- | --- | --- |
| P0 | R1 复杂帧坐标、R2 动态求值、R3 无损写入、R4 会话身份 | 可分别修，均须加入能复现当前问题的测试。R1、R4 完成后再通过真实工作区开放复杂变速或跨会话写入。 |
| P0 | R5 多节点求值、Edit 监看与导出一致性 | 依赖 R1、R2 的逐帧接口。对不支持路径给出能力诊断，在所有目标后端通过验收后声明支持。 |
| P1 | R6 工具录制与 auto 关闭态行为 | 使用 R1、R4 的帧和身份契约、R3 的批事务。先补已登记的 Qualifier/Window，再逐工具扩展。 |
| P1 | R7 Sizing、FX、EXT MATTE 和剩余工具 | 依赖 R1–R6。按实际工具能力分批开放，走同一工程、预览、导出和历史契约。 |
| 发布门禁 | R8 实机对标及旧入口退出 | OP-01–OP-16 和 V01–V08 按适用场景附证据。保留未实测项及能力限制的明确标记，不能以示例操作代替完整验收。 |

## 后端任务

### R1 · 统一 panel-local、source 和 timeline 坐标

**旧路径已复现，普通裁切已修；复杂映射显式只读（R 批次补齐只读矩阵与 DF 时间码，完整适配器待 R7 后续版本）。** 测试片段 `timeline_start=240`、`source_start=100`。在面板 local 12 添加键，旧路径存为 source 12，实际渲染帧应为 source 112，时间线为 252。自动录制使用源帧，手工键与录制键因此可能落入两个位置。本轮将 1× 正向且同速率的投影、手工写、复制/粘贴和移动接到源帧偏移，复杂 retime 显式只读。跨 clip 剪贴板、变速和不单射的往返仍须独立验收。

1. 定义单一坐标适配器，把剪辑、有效源起点、时间线帧率/素材帧率、retime 与当前范围冻结到会话绑定。UI 和拖拽偏移使用 clip-local，轨道保持历史 source 域。Timeline 范围以及无单一源资产的槽位登记独立时间线域。复用 [FileController](/Users/heng/Documents/vscode/topos-studio/src/app/controllers/file_controller.py) 的现有 seek/scrub 协议及 [ClipInstance](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/timeline_v2.py) 的映射。单独核查有效源偏移与 retime 同时存在的分支。
2. 改 [ColorKeyframesHost](/Users/heng/Documents/vscode/topos-studio/src/app/controllers/color_keyframes_host.py) 的投影和写请求翻译。读轨道时保留领域帧身份，按可见范围投影。trim 外的点过滤出视图而保存在工程。添加、删除、转换、动态属性、复制、粘贴均使用同一适配器。Move 先求每个 local 目的帧，再分别映射源帧。跨片段复制捕获值和源 clip-local 间距，粘贴时按目标 clip 映射。禁止把 clip-local 偏移直接加到 source frame。
3. 慢速、冻结和插帧可能令多个 local 帧共享一个源帧。确定显示与编辑策略，必要时让 marker carry 领域 identity。若两个移动目标映射成同一轨道同一源帧，提交前整批拒绝并说明。DF/NDF 用 [Timecode](/Users/heng/Documents/vscode/topos-studio/src/shared/media/timecode.py) 的 Rational 接口，不用 nominal fps 计算真实映射。对已被错误写入的工程，只能在能确定旧写入版本及 clip 绑定时提供检查和显式修复，不能假设所有小帧号都是错误数据。
4. 在 `tests/app/test_color_keyframes_host.py` 增加真实 ClipInstance 场景，记录 local 12 → source 112/global 252 的写入、投影和 seek，移动到 18 后保存应为 118。对照 `tests/edit/test_effect_keyframe_coordinate_contract.py`，补 2×、0.5×、倒放、冻结、速度曲线、滑移、前后裁切、有效源偏移、23.976 和 29.97 DF。实际解码帧、读取、导航、重开与导出一致才通过 V04。

### R2 · 统一显式 Dynamics 求值，修正 Static 中间键边界

**已闭环（R 批次，提交 2c51b69）。** provider 经 from_dict 重建、value_at 统一语义（精确帧左键值/静态右保持/legacy 等价键旧路径逐位一致/显式动态读 A.start、B.end）；原文备查： [图参数提供者](/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_param_source.py) 重建 `GradeKeyframe` 时只传 frame/value/interpolation，显式 kind/start/end 在持久化与 UI 中存在，但被求值丢掉。对于 0→1、起点 Start=0、终点 End=1 的区间，25% 处当前提供者得到 **0.25**，仓库已定义的 Hermite 兼容权重为 **0.109375**。原 [GradeKeyframeTrack.value_at](/Users/heng/Documents/vscode/topos-studio/src/features/color/core/grade_keyframes.py) 以闭区间扫描段，三个 hold 键在 f0/f10/f20 值为 0/10/20 时，f9/f10/f11 得到 **0/0/10**。本轮改用左闭右开，修复的预期值为 **0/10/10**，统一求值时保留此边界行为。

1. 让 [keyframe_tracks.py](/Users/heng/Documents/vscode/topos-studio/src/features/color/core/keyframe_tracks.py) 的 decode 和段权重成为共同入口。优先查找精确键，其他时间使用左闭右开的段。A→B 使用 A 的 Start 和 B 的 End，静态 A 决定保持到 B 之前，动态 A 允许插值到静态 B。无显式参数的旧 hold/linear/ease 保持既有非边界数值，静态精确帧按键值修正。保留向量分量及限定器 hue 最短弧规则。
2. 从不可变 payload 构造已验证轨道，接 Color graph provider、Layer/旧工程、Edit CPU/GPU 监看、Deliver CPU/GPU 及适用的 Metal 入口。保留原始序列化字段和解析诊断。渲染快照可以按 graph revision 与轨道 ID 缓存解码结果，逐帧只求值。清点 `GradeKeyframe.from_dict` 与手写构造器调用点。
3. 在 `tests/color/test_grade_keyframes.py` 和 `tests/color/test_keyframe_tracks.py` 加 f9/f10/f11、静态→动态、动态→静态、默认权重、显式非对称权重以及端点的数值测试。再用真实 graph provider、保存重开和导出夹具验证同一段 25%/50%/75% 一致。0.109375 仅是 Topos 兼容函数的验收值。完成 Resolve Start/End 档位实测后再改权重函数，不能把这组值标成 Resolve 实测。

### R3 · 批量编辑只改目标轨，保留不透明数据

**已闭环（R 批次，提交 0e59a10）。** 单键/批命令共用无损更新核心，未知字段保留、触及不可解析整批拒；原文备查：[ColorGraphService.op_batch_edit_keyframes](/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_service.py) 解析节点全部轨道，再按 `{"keyframes": ...}` 重组。改 exposure 一条轨就可能丢同节点未编辑轨的元数据或未来 schema；编辑轨里未改的键也可能经 decode/encode 丢字段。已有工程往返测试通过，并未覆盖真实批改后往返。

1. 从不可变图数据深拷贝原载荷。只对 `touch_names` 解析并更新已知字段。未触及轨的 Mapping、未知域、无键的未来格式、墓碑扩展和不认识的键级字段原样保留。move 只改帧号，dynamics 只改键型/动态属性，set 已存在时合并已知字段，删除只移除目标键。无法安全解析被触及的未来格式时整批拒绝并指明轨道。
2. 通过实际触及的数据差异判定空动作。一批先验证再一次提交。删空 legacy 轨时保留同名墓碑，新增该轨只恢复本次显式写入的数据。兼容脚本的单键命令复用同一无损更新逻辑。带 `MappingProxyType` 的嵌套数据在 DTO/渲染边界深解冻，对正常未知字段的保留独立验收。
3. 在 `tests/color/test_color_graph_keyframe_batch.py` 增加两条轨及未来字段夹具，分别改一条已有键的值、位置、类型并断言另一条轨与未动的键深度等价。加入未知格式、墓碑、no-op、拒绝和多节点事务。接 `tests/color/test_color_graph_keyframe_persistence.py` 走一次真实保存、重开、撤销、重做。原工程历史每次只增加一条。

### R4 · 权威图、旧项目绑定与跨会话隔离

**读投影/失效清理/绑定描述（binding_description）/A→B→A 失效已补（R 批次，提交 0a7edcf）；剩余为实机会话矩阵验收。** [ColorGraphAppHost._bind_and_hydrate](/Users/heng/Documents/vscode/topos-studio/src/app/controllers/color_graph_controller.py) 可为无图 legacy 栈创建迁移投影。原先面板只读取持久化图，会把仍有可读轨道的旧栈当空。现已有迁移投影回退。仍需核对真实写入所使用的已绑定 stack、grade 版本、scope owner、compound path、generation 和 expected revision，尤其是 Timeline/Group 与 A→B→A 的迟到请求。

1. 让 graph host 对外提供一份明确绑定描述，含只读迁移视图或权威图、是否允许写、scope/owner、实际 stack、图 revision、已生效的会话 generation。旧图首次写入复用既有 graph 命令建图的事务，不能由界面直接改变项目。若读投影与写入口暂时未对齐，先只读。
2. 拖拽开始、复制和工具手势绑定上述身份，提交重新比较。播放帧回填也带 generation。换片、换版本、换 scope、退出页面、卸载工程及 hydration 失败立即失效待处理手势和录制；旧结果只丢弃。切回相同 ID 时 generation 仍须不同。只有 project publish、graph/selection 变化和 transport 回填触发各自所需更新。
3. 建 A/B 同素材不同裁切、同一 clip 的两个 grade 版本、一个没有 `color_graph` 的旧栈和一个复合节点。每个会话执行 add/move/seek，注入旧 revision、旧选择、晚到 seek 和 A→B→A。检查工程内容、图绑定、首次写入/Undo、节目画面以及实际后端命令数。用 [现有宿主测试](/Users/heng/Documents/vscode/topos-studio/tests/app/test_color_keyframes_host.py) 增加真实项目命令案例，再用原生窗口检查初始化与切换。

### R5 · Edit 与 Deliver 按执行图消费所有 Corrector

**CPU 回退已按权威图逐节点合成（R 批次，提交 a4e5e03：run_graph_frame_cpu 生产 oracle + 能力诊断默认拒绝；复审批次补节点缓存逐帧身份与异常策略）；Edit GPU 监看、GPU 导出图执行与 Metal 入口仍待接（GPU 监看仍消费扁平 grade）。原文备查：** [TimelineExporter](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/timeline_export.py) 的 `_grade_from_stack` 仍提取活动层或首个有轨层作为扁平 `grade_keyframes`，窗口 matte 路径还会只看活动层并用该扁平轨道。复杂图通过能力硬门拒绝。 [TimelinePreviewRenderer](/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/timeline_preview_renderer.py) 的 `_get_preview_color_grade` 也取第一个有轨层。这些回退路径与图快照能力不同，单独修复 mappingproxy 能让输入通过，但无法恢复被压平的节点及动画。

**已修复（R 批次，提交 dcc2784：任务边界按实际解码帧覆盖 color_graph_source_frame）。原文备查：** 当缓存参数携带 `color_graph_source_frame=112` 而后续解码帧为 113 时，`RENDER_VIDEO_FRAME` 的 graph provider 仍用 112。播放画面上的动画可停在旧值，即使图节点缓存已按时域标记绕过。图执行还受 TOPOS_COLOR_GRAPH_RENDER 开关控制，默认走旧 Layer 路径。测试时分别记录开关关闭与开启的路径。

1. 对项目栈、scope 顺序、图快照、源帧与时间线帧建立共同求值计划。可投影串行图仍按图执行顺序保留每节点的 base、轨道、曲线、遮罩、opacity 与启停。CPU fallback 必须逐节点合成，无法等价执行的图返回能力诊断。复杂图走既有编译、执行及预检机制。保留用户显式允许的可诊断降级入口。
2. 拆分 [VideoPlayback](/Users/heng/Documents/vscode/topos-studio/src/shared/video/video_playback.py) 的可缓存图/静态参数和任务逐帧身份。生产 `RENDER_VIDEO_FRAME` 时携带实际解码帧的 source frame 与 generation。在 [AsyncRenderer](/Users/heng/Documents/vscode/topos-studio/src/features/color/core/async_renderer.py) 的任务边界按该值覆盖任务私有快照的帧字段，让 graph provider 读取真实帧。用一份静态参数提交解码帧 112、113、114，断言三次 provider 看到对应帧，任务之间原缓存对象不变，并补丢帧、seek、loop 和跨 clip。
3. 接 [CPU 图执行](/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_cpu.py) 和 [Metal 图执行](/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_metal.py) 的冻结快照与现有任务队列。依赖节点随时域标志、帧、图 revision 和资源签名失效。缓存键还须包含真实输入纹理代际或源帧内容身份，防止静态上游对变化的视频纹理误命中。时域或形状数据在渲染线程处理。刷新仅回填新会话对应的输出。
4. 测试图包含两条动态 Corrector、一条保持恒定且依赖 legacy 参数的节点、一条带动画的 Window 和跨帧 Qualifier。C1/C2 使用不同参数、不同关键帧和非交换组合，切换活动层后同一帧输出不变。关闭某一能力后应明确阻断或按用户允许降级。并行比较 Color 暂停、连续播放、Edit 零拷贝/GPU 与 CPU、Deliver CPU/GPU 和支持的 Metal，用同一输入及色彩管理留存逐帧数值和像素差。现有 `tests/edit/test_export_mask_keyframes.py`、`test_m06_qualifier_export.py` 与 `test_color_stack_preview_grade.py` 只作基线。

### R6 · 给 Qualifier、Window 与后续工具补全真实录制

**已闭环（R 批次，提交 3a147a1）。** submit_qualifier_change/submit_window_shape_update 在 patch 成功后、flush 前收集真实变更字段；手工 Add 采样当前求值结果；auto 关闭态锚点行为仍待 OP-15。原文备查：** [KeyframeAutoRecorder](/Users/heng/Documents/vscode/topos-studio/src/app/controllers/color_keyframe_recorder.py) 支持已登记参数名，但实际 observe 走 grade 适配器。 `submit_qualifier_change` 与 `submit_window_shape_update` 直接缓冲图命令，点亮的 auto 状态无法覆盖这两种真实工具手势。auto 关闭当前跳过录制，对已有动画的区间/锚点缺少 Resolve 行为。

1. 让 graph host 在验证工具 patch 成功后、事务 flush 前统一收集真实变更字段。Qualifier 使用 `qualifier.<field>`，Window 使用 `mask.<shape_uuid>.<field>`。身份包含 scope/owner、路径、节点与帧。实际开关以行的能力与父子级别合成，Corrector 开关作用于各可用子域，单个子轨开关只影响本轨。已锁、只读和未登记工具给出明确状态。
2. 区分真实手势、播放、回填、加载、撤销和脚本来源。一次手势锁定首个有效帧，汇总最后值，base patch 与批关键帧只入一次历史。切片、换版本、改变目标或取消时按实际手势生命周期处理。手工 Add 采样当前求值结果，不直接复制不随时间变的 base。auto 关时按当前键和相邻锚点选择已有键，补充首末、段中、混合键型的实测。无轨的真实手势仍走原参数修改路径。
3. 在 `tests/app/test_color_keyframe_recorder.py` 使用真实 `submit_qualifier_change`、`submit_window_shape_update` 和工具信号测试，仅模拟 playback/宿主硬件边界。分别在两个时刻调 luminance 和拖独立 shape，检查值、帧、图记录、可见标记、Undo 与播放输出。auto 关时有轨改锚点、无轨改 base。覆盖程序化回填零写、失效目标整批拒、原位 no-op 和跨 scope 不串轨。

### R7 · Sizing、FX、EXT MATTE 及工具能力覆盖

**尚未可用。** 手工图命令当前覆盖的是登记过的 grade/qualifier/window 字段。Sizing、独立或附着 FX、EXT MATTE 与未登记的 Color 工具仍缺写入和跨后端求值。当前 [工具覆盖清单](TOOL_COVERAGE.md) 是注册表的静态能力表；具体工具录制、动态档位和回退路径以此轮复测缺口为准。

1. Sizing 按 Input / Node / Timeline Output 设计不同槽位、坐标域和处理顺序。Resolve 的 Output Sizing 在 Timeline 图范围，立体工具按实际能力登记。核对 Edit Transform、Color Sizing 与稳定器的组合并避免重复处理。实现后用两个不同帧的变换矩阵、关键帧间帧与最终像素对比。
2. FX 使用 Catalog/Registry 参数 schema、稳定实例 ID 与版本。附着 FX 在 Corrector 组内，独立 FX 为单独轨道。对不具备逐帧后端的插件或字段显示原因，资源与离散选择单独处理。用两个同名实例、插件参数升级、缺失资源和不同后端测试。
3. EXT MATTE 由权威图是否包含外部遮罩决定，登记时间偏移与 Sizing 实际字段。验证该模式只改目标外部遮罩，Color 模式只改活动 Corrector，All 写所有适用叶轨。All 遇不支持目标时须可见地告知已支持范围，锁定与能力缺失遵循量测后的确定规则。
4. 按色轮、RAW、曲线/Soft Clip、Mixer/Warper、Window/Tracker、Qualifier/Key、Defocus/NR、FX/LUT、Sizing 逐个登记实际可动画字段及连续/离散/资源类型。更新 `DOMAIN_CAPABILITIES` 后用既有生成函数重建覆盖清单，每条记录都绑定真实命令、录制、预览、导出和复制证据。无法验证的能力先保持不可用。

### R8 · 发布证据与旧入口

按下面两个表补齐缺项。把能够稳定复现的操作从手工记录转为测试，并保留实机录屏/截图、帧值或媒体输出。对同时出现的色轮旧录制按钮和新面板，继续明示写权限归属。等真实工程事务、旧项目迁移、所有已开放工具和导出验收完成，再统一退役旧写入口与重复快捷键，统一 Node Badge、Viewer ruler、Clip Strip 和工具重置的读写来源。

## OP-01–OP-16 对标记录

| 编号 | Resolve Studio 20 本轮 | Topos 当前证据及剩余验证 |
| --- | --- | --- |
| OP-01 · 静态添加 | **部分实测。** Local `00:00:00:16` 出现圆点。 | 测试覆盖添加与事务。用轨道右键和播放头分离位置，核对写入当前帧及真实像素的静态跃迁。R1/R2。 |
| OP-02 · 动态添加 | **部分实测。** All 在 `00:00:01:16` 的 Corrector、Sizing、Master 显示菱形。 | 支持已登记轨的动态标记。默认 Start/End 与区间数值、Sizing 实写待查。R2/R7。 |
| OP-03 · 静态转动态 | **待实测。** | 批命令可保存转换。补真实播放、值保持、默认权重与 Undo。R2。 |
| OP-04 · 动态转静态 | **待实测。** | 现有测试覆盖归一为 hold，内部静态键精确帧晚一帧已修。仍需对比转型后逐帧画面与两侧区间。R2。 |
| OP-05 · 同帧改值 | **待实测。** | 现有批命令支持同帧替换。要用工具手势复核实际锚点与 base 事务。R3/R6。 |
| OP-06 · 删除单键 | **仅菜单实测。** Mark 菜单可见删除入口，单键删除后区间未量测。 | 有删除与拒绝测试。补真实区间、选择和键盘入口。R2/R3。 |
| OP-07 · 删除末键 | **仅菜单实测。** 可见 Delete All，轨道和基值结果待查。 | 墓碑保存、重开、撤销已有用例。补旧数据兼容、实机行保留及再添加。R3/R4。 |
| OP-08 · 拖动 | **部分实测。** C2 从 `00:00:02:15` 移到 `00:00:03:04`，下一关键帧导航随后跳到新位置。碰撞未测。 | 现有批命令碰撞整批拒；需确认 Resolve 覆盖规则与裁切/retime 中的目的帧。R1/R3。 |
| OP-09 · 框选多帧 | **待实测。** | 多节点移动已有事务测试；用真实窗体验证框选范围、多轨同帧、锁和碰撞。R1/R3。 |
| OP-10 · 拖父轨 | **部分实测。** C2 父轨标记可拖，子轨逐条的结果未逐一记录。 | 复核折叠树父轨只迁移同帧子键、Master 汇总与撤销。R1/R3。 |
| OP-11 · 复制/粘贴 | **待实测。** | 捕获值、删源后粘贴、跨 context 和分量冲突有测试。补剪辑时间映射、目标类型、Resolve 命令范围及工程回放。R1/R4/R7。 |
| OP-12 · 缩放/变速映射 | **待实测。** | 普通裁切帧偏移已修，复杂映射只读。确认缩放不变键值，补所有变速映射、重复源帧与冻结。R1。 |
| OP-13 · 节点启停 | **部分实测。** C1 的白圆启停能旁路节点。 | 补 Topos 旁路前后真实渲染、轨道保留、Undo 以及各后端对比。R5。 |
| OP-14 · 节点锁 | **部分实测。** C2 变暗，面板与节点出现锁标识；半锁定范围待查。 | 批命令锁定拒绝已有测试。补真实菜单、只读状态、半锁 All 和晚到命令。R3/R4。 |
| OP-15 · Auto | **部分实测。** C1 开启后，在 `00:00:03:16` 改 Temp 0→400，只新增 C1/Master 的键。关闭后锚点行为待测。 | 色轮录制有事务测试，Qualifier/Window 实际 observe 缺失，auto-off 尚未实现。R6。 |
| OP-16 · 写入范围 | **部分实测。** All 影响 Corrector/Sizing/Master。选中 C2 后 Color 在 `00:00:02:15` 只影响 C2/Master。Sizing 在 `00:00:04:04` 只影响 Sizing/Master。EXT MATTE 待测。 | 活动节点、父轨、真实 Sizing、FX/Matte 和组范围按 R4/R7 复查。Mark 菜单存在新增、全删与 nudge，实际快捷键优先级待验收。 |

## V01–V08 发布验收状态

| 用例 | 当前状态 | 完成条件 |
| --- | --- | --- |
| V01 写入范围 | **部分。** Resolve 已观察 All/Color/Sizing，Topos 只有已登记域可写。 | R4/R7 后以两 Corrector、独立 FX、EXT MATTE、Sizing 做实际工程读回，对照选择与轨道集合。 |
| V02 静态/动态 | **代码侧已闭环。** 显式 Dynamics 贯通求值域（数值/保存重开/导出一致）；Resolve 动态曲线档位仍待实测。 | R2 数值、界面、保存重开、各后端一致——代码侧已过；Resolve 档位量测后定稿。 |
| V03 手势与锁 | **部分。** 两边已测父轨拖动和锁。Topos 实媒体验证了锁定禁止删除和解锁后删除。 | 真实 UI 手势和工程历史，包括跨轨框选、跨上下文失效、锁定规则、取消和撤销重做。 |
| V04 时间映射 | **部分。** 普通裁切已修，复杂映射只读。 | R1 的剪辑与帧率矩阵全部通过。 |
| V05 旧工程与版本 | **代码侧已闭环。** 无损批编辑（未知字段经保存/重开/undo/redo 保留）已有测试；真实工程验收待实机。 | 用带未知字段、旧图和多个版本的真实工程完成编辑、保存、重开、撤销、再编辑。 |
| V06 渲染与导出 | **代码侧主链已闭环。** 过期帧已修；CPU 导出/预览按图逐节点合成；GPU 导出图执行与真机媒体对比待验收。 | 输出逐帧数据与媒体对比，含多节点、缺能力、取消和快进（真机）。 |
| V07 UI 与集中入口 | **部分。** 原生与面板修复随本轮独立验收，工具自动录制和旧入口退役待做。 | 同一视频夹具用真实工具、快捷键、旁路、锁、切片/版本及重开；没有双录且操作只提交一次。 |
| V08 性能 | **部分。** 已测离屏面板 10,000 键，4K 播放尚待实测。 | 同机 4K 测空轨、常用轨与 20 Corrector/200 轨/10,000 键，记录 60 秒播放掉帧、P95 界面帧耗时、seek 延时、缓存和内存；有可见回退先定位再放行。 |

优先把每个确认的缺陷写为会在当前路径失败的测试。修复后运行对应模块、现有 Keyframes 回归和相邻工具测试。最后在各实际后端补做 V01–V08 并把证据填回 [发布清单](KF07_RELEASE_CHECKLIST.md)。
