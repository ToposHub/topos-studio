# Edit Viewport 后端接入计划

日期：2026-11-09。状态：后端批次 VP-B01–VP-B12 已于 2026-11-09 实施完毕（见文末"VP-B01–B12 实施记录"）；前端容器接线仍待本文件所述前端提交线合流。本文件不表示前端已完成合流。

**同日合流复查补充**：上述“实施完毕”是原后端批次交付记录。当前单／双 Viewport 外壳已接，原播放控件、Source Loop／Stop 和版本化偏好已接；播放仲裁／时间域／预算／完整拖出等仍缺生产接线。明确的大问题、复现与后续验收以[新复查计划](2026-11-09-viewport-integration-review-followup.md)为准，不把原域件单测等同于整页完成。

## 1. 目标、参考和本轮边界

将剪辑页的 Program、Source 两个独立停靠面板合并成一个 Viewport；面板内部以单视图／双视图切换管理源素材和时间线监看。交互参考本机已打开的 DaVinci Resolve 20 Edit 页：右上角 Dual Viewer Mode、双视图的 Source 左／Timeline 右、各自的标题／时间码／播放控制，以及单视图的监看对象切换。本轮依据实机窗口观察，不将参考应用的按钮外观视作 Topos 已具备对应后端能力的证据。

本轮前端约定：

- 新容器为 `src/features/edit/ui/edit_viewport.py` 的 `EditViewport`，接收现有 `ProgramViewerWidget`、`SourceViewerWidget`，不另造渲染器。
- 单视图默认显示 Timeline；双视图 Source 左、Timeline 右。容器入口包括 `show_source`、`show_program`、`set_dual_view`、`restore_program_viewer`、`set_deliver_mode`。
- header/footer 复用原 toolbutton、时间码 QLabel、时间线 selector，维持原 setter 和信号的对象关联；不要为新 UI 建立另一份播放头或时间线。
- 精细化前端已完成：30px header 的标题全行居中，34px transport，单入口切换模式和 24px 自绘 scrubber。按最新用户要求，主播放区恢复原首帧／上一帧／橙色播放暂停／下一帧／末帧图标、15/18px glyph、28/32px 按钮与 3px 间距；Program 保留原 Loop，Source 保留原五键。停止与反向等扩展动作保留在菜单。原 SourceSlider 保持同一对象；直接点击／拖动及禁用、隐藏、鼠标捕获丢失时的手势收尾已在 UI 层接通。
- Program scrubber 发出 `program_seek_requested`，前端桥接已有 `controller.seek`，已有 `frame_changed` 回填 UI。此项属于旧能力接线，不是新解码／播放后端。
- 保留 `dockProgramViewer` 的 Qt objectName，标题显示 Viewport；不再创建 Source dock，`dock_source_viewer` 为 `None`。媒体池、时间线片段双击、Space 焦点入口和 PanelRegistry／预设随前端同步调整。
- Deliver 仍借用同一个 Program widget，attach 不再将 Viewport dock 作为 Program 的直接宿主传入；返回时由 `restore_program_viewer` 放回原容器。
- 显示偏好现接版本化 `edit_viewport_preferences` 段，兼容迁移旧 `edit_viewport_dual_view` 与 `edit_viewport_splitter_sizes`；保存活动监视器、单／双和合法分栏比例，经 300ms debounce，Deliver 临时状态不保存。LayoutStore 默认面板和旧 `edit.program_viewer`／`edit.source_viewer` 可见面板 ID 已合并映射到 `edit.viewport`，保留原 Qt state。
- 旧布局只显示 Source、隐藏 Program 时，新 Viewport 在该产品／工作模式首次进入 Edit 时显式展示一次并保存迁移标记；之后尊重用户关闭面板的选择，不清空其他停靠布局。
- 尚未接通的菜单和按钮保持 disabled 并给出原因。启用控件须以实际 capability 和成功回填为依据，不能只切换 checked 样式。

前端开发和本文并行时，以上是接口约定；最终调用签名以本轮提交中的实际代码为准。下文标为“拟新增”的接口／数据类型均未预设为已有实现。

## 2. 当前真实链路与可复用能力

| 路径／入口 | 已有能力 | 本次后端工作的边界 |
|---|---|---|
| `src/features/edit/ui/edit_page.py`：`set_program_viewer`、`_connect_transport_to_timeline` | Program 是被动显示端；transport 信号已接 TimelinePanel，播放头、播放状态和速度回填 Program；字幕／变换／Guides 已有信号链 | 保留这些连接和原 viewer 对象，新增监视器仲裁，不复制时间线控制器 |
| `src/features/edit/core/edit_playback_controller.py`：`seek`、`pause`、`stop`、`play_with_speed`、`jkl_*`、`set_loop`、`set_playback_range` | 时间线已有 seek、正反向／JKL、Loop 和播放范围；`stop()` 当前会归零 | 为 Viewport 做语义适配；不得为 Stop 按钮直接调用归零接口却宣称是原位停止 |
| `src/features/edit/core/timeline_preview_renderer.py` | 已协调时间线精确帧、scrub、实时预览；Program 帧由原链路接收 | 增加可见性和优先级策略时沿用现有 generation／取消机制 |
| `src/features/edit/core/edit_gpu_preview_renderer.py`：`EditRenderTask`、`ack_present_slot` | 后台共享 GL 渲染、generation fence、输出槽位及 deferred retirement 已存在 | 补隐藏／切换／退役情况下的槽位回收与双路预算验证，不在 UI 线程操作 GL |
| `src/features/edit/ui/source_viewer.py`：`open_asset`、`seek`、`play/pause/stop`、`set_in_point/set_out_point/clear_in_out` | Source 已有异步 GPU／CPU fallback、单调时钟正向播放、scrub 音频、In/Out、背压、generation；`stop()` 也会归零；目前播放至素材末尾暂停 | Source 反向、Loop、范围播放、公开状态快照和时间域完善是新增；不要重做现有 seek／解码 |
| `src/app/controllers/file_controller.py`：`_setup_source_gpu_provider`、`_on_source_present_frame_ready`、`_on_source_gpu_frame_ready` | Decode worker → Source GPU renderer → frame_ready → SourceWidget；present 以 frame+generation 匹配 slot；widget ack 回 renderer | 策略丢弃帧时也必须完成 slot 回收；不可用 `if not visible: return` 截断 ack |
| `src/features/edit/ui/edit_page.py`：`_get_source_edit_params`、`_on_timeline_source_insert/overwrite/append` | Source In/Out 已被 TimelinePanel 的插入／覆盖／追加消费，`range_changed` 同步按钮能力 | Viewport 新入口复用现有编辑链，保留命令栈／undo／revision |
| `src/features/edit/ui/timeline_view.py`：`_source_edit_plan`、`_apply_source_edit_plan`、`resolve_source_destination_track_id` | Source patch 目标轨道、源帧率、编辑计划和命令提交已有实现 | 视频／音频单独拖拽扩展必须进入同一 planner，不从 UI 直接增删 clips |
| `src/shared/media/timecode.py`：`Timecode`、`TimecodeType` | 共享有理数帧率及 NDF／DF 时间码模型 | Source UI 的简化时间码不应继续另立近似计算方式 |
| `src/features/edit/core/scene_cut/models.py`：`SourceFrameTimeIndex` | 场景切割已有 VFR 源帧与 PTS 索引模型 | 评估抽取共享时间索引接口；不要让普通播放依赖运行场景切割分析 |
| `src/features/deliver/ui/deliver_workspace.py`：`attach_edit_panels`、`detach_edit_panels` | 原 Program／Timeline 实例可跨页借用，连接和播放头继续存在 | 验证新父子关系、显示策略和状态恢复，禁止返回时覆盖整个 Viewport |

## 3. 后端任务总表

人日均为单名熟悉项目的开发者的粗估，含 focused test，不含等待硬件、素材补齐与跨平台问题处理。必须先测量再确定性能方案；数字不是交付承诺。

| ID | 优先级 | 依赖 | 范围／交付物 | 估算人日 |
|---|---|---|---|---:|
| VP-B01 | P0 | 前端容器接口稳定 | 活动监视器状态与播放互斥适配服务 | 1.5–2.5 |
| VP-B02 | P0 | B01 | 隐藏视图调度、generation、present ack 生命周期 | 2–3 |
| VP-B03 | P0 | B02 | 双路解码／GPU／内存预算与诊断 | 2–4 |
| VP-B04 | P1 | B01 | Source 时间域、VFR、源起始时间码和 In/Out 校验 | 2–3 |
| VP-B05 | P1 | B01、B04 | Timeline／Source transport 能力适配 | 2–3 |
| VP-B06 | P1 | B04、B05 | Match Frame、marker、时间线／源范围操作 | 1.5–2.5 |
| VP-B07 | P1 | B03、B05 | Viewer capability 与 Proxy／Resolution／Bypass／Source Tape | 2–4；Source Tape 另计 |
| VP-B08 | P1 | B04、B06 | Source 视频／音频／A+V 拖拽与命令提交 | 2–3 |
| VP-B09 | P1 | B01、前端 dock 合并 | 用户显示偏好持久化与旧布局兼容 | 1–1.5 |
| VP-B10 | P0 | B01、B02、前端 Deliver 适配 | Deliver／Edit 往返和全屏父级恢复 | 1–2 |
| VP-B11 | P0 | B02、B03、B05 | 关闭／取消／GPU 超时／fallback／音频释放 | 1.5–2.5 |
| VP-B12 | P0 交付门槛 | B01–B11 已启用部分 | HDR／真实素材／性能／跨页验收报告 | 2–3 |

以上合计约 20.5–34 人日，存在任务交叉，实施后应重新估算。Source Tape 的素材集合和虚拟时间域是独立功能：先保留禁用入口，完成技术拆分后另立工期，不将其压入普通 Source 切换任务。

## 4. 逐项实施和验证门槛

### VP-B01：活动监视器状态与播放互斥

**已有**：前端容器知道单／双模式；Space 已按焦点区分 Source 和 Timeline。两条播放链及音频链各自存在，没有统一的 Viewport 仲裁状态。

**输入 → 输出**：UI 激活／切模式／素材打开／时间线操作／跨页事件 → 一个权威监视器快照，以及对原控制器的 pause／play 请求。拟新增快照字段：`layout_mode`、`active_monitor`、`source_asset_id`、`page_mode`、两路播放状态、`request_revision`；只含可序列化状态和稳定 ID，不放 QWidget／texture／decoder。

**实现步骤**：

1. 在 edit 领域内建立轻量仲裁器，由 app 适配现有控制器；UI 通过信号请求状态变更，处理完成后回填。`EditViewport` 的可见性不承担领域播放状态。
2. 固定策略：单／双布局切换仅改变显示，双视图点击一侧只改 active；任一侧开始连续播放前先暂停另一侧音频和播放时钟，保留对方播放头；切到其他页按页面政策暂停 Source。显示隐藏的策略由 B02 承接。
3. Source 空素材、无 Timeline、输入框焦点、菜单展开时，分别拒绝不适用的 transport；拒绝必须回填原因，不显示正在播放。
4. 媒体池单击只更新 Source 内容，保持现有“不抢焦点”行为；双击／时间线 Open in Source 明确激活 Source；点击时间线或执行其播放动作激活 Timeline。所有入口走同一方法。
5. 信号回填使用状态锁／QSignalBlocker；同一 revision 的反馈不重复发播放请求，不在状态回调中反向递归切换 active。

**门槛**：Source→Timeline→Source 连续切换 50 次始终最多一路连续出声；单／双模式来回不改变两路播放头和 In/Out；无素材 Space 不抛错；文本框 Space 不启动播放；一次点击只产生一次领域请求。

**风险／失败路径**：pause 失败、旧回调迟到时先保持目标端未播放并诊断；关闭事件撤销待执行激活；不得以 boolean 默认值掩盖控制器失效。

### VP-B02：隐藏视图调度、generation 与槽位回收

**已有**：Source seek／换素材会推进 `gpu_generation`；CPU 回调校验 generation；GPU provider 的 worker 和 renderer 已接 ack。GPU renderer 已有过期 generation fence 和未 ack 不复用的 slot 规则。

**输入 → 输出**：monitor visible／active／page mode、待解码／待呈现帧、generation → 调度政策及每个已分配 slot 的唯一结束路径。

**实现步骤**：

1. 明确三种状态：可见且活动、可见但非活动、隐藏。隐藏端停止周期性预览请求和无意义 readback；保留最后已呈现画面／播放头。精确一次性 seek、导出监看等请求不得被通用 hidden 判断误杀。
2. 隐藏或换源时取消可取消的旧请求并推进对应会话 generation；可见性 revision 与媒体 generation 分开，不能因反复 show/hide 误将当前素材清空。
3. 审核 `file_controller._on_source_present_frame_ready/_on_source_gpu_frame_ready` 配对方式：被丢弃的 pending present、收到帧时 widget 已关闭、frame+generation 不匹配、迟到旧 renderer 回调均须通过原 owner 回收 slot。若调整协议，优先把 present descriptor 作为一个原子结果传递，保留兼容入口直至迁移完毕。
4. `SourceViewerWidget.set_gpu_frame`、Program `set_frame` 的“不显示”路径也需显式 ack／release，不能仅停止 `paintGL` 后永久占用 lease。
5. 再次可见时复用缓存或只请求当前帧一次；记录 decode submit、discard、ack、retire 计数，便于追踪饥饿和泄漏。

**门槛**：在 GPU 解码中隐藏 Source、快速换源、关闭 dock、Edit→Deliver 时，所有 slot 最终可重用／退役；200 次模式切换无持续增长的占用池；过期帧永不覆盖新素材；恢复后一个有效请求能出画面。

**风险／失败路径**：ack 必须发回创建 slot 的 renderer，不能只凭全局 slot_id 发给新实例；双 ack、未满足 fence 提前释放均需测试。超时退役沿用现有 deferred garbage 规则，UI 不调用 GL delete。

### VP-B03：双路预算、背压与诊断

**已有**：Source 有 `_should_defer_playback_tick` 和时钟 reanchor；实时预览已有 policy、priority、decode metrics 和 frame pool。本任务先测当前双视图，保留有效机制。

**范围**：`core/realtime_playback/{engine,policy,task_priority,playback_budget,gpu_frame_pool}.py`、TimelinePreviewRenderer、Source provider 的共享预算接入。

**输入 → 输出**：两侧尺寸、目标 fps、活动端、decode／GPU耗时、池压力 → 每侧请求尺寸／优先级／队列上限／背压决策和诊断快照。

**实现步骤**：

1. 记录单视图基线：同一素材、分辨率、效果、时间线和硬件，统计首次出帧、seek p95、呈现 fps、掉帧、音画差、UI响应、GPU池／CPU缓存峰值。
2. 双视图优先保障正在播放的 Timeline；非活动 Source 仅保持静帧。Source 活动播放时仍遵循 B01 单路连续播放政策，避免用“双视图”默认启动两套音频。
3. 依据实际 viewport 尺寸设置预览目标，设请求／内存上限；复用后台 decode manager 和现有队列，不额外创建无界线程池；4K 原始画幅不因一个小 Source 窗口重复全分辨率 CPU 转换。
4. 队列满时合并同会话的旧预览请求，保留最新一次精确 seek；Program 和 Source 不共用可变的 frame/generation 属性。
5. 在双视图压力下验证自动预览质量；显示的质量和真实政策一致，导出参数不受预览降质影响。

**门槛**：4K 60fps 是评测目标，不无条件宣称所有硬件达标。双视图开启但 Source 静止时，Program 呈现率相对同机单视图基线下降建议不超过 5%，UI p95 输入响应建议 <100ms；达不到须附瓶颈和降质政策。持续 10 分钟不发生无界队列／内存增长或 decode 饥饿。

**风险／失败路径**：硬解会话不足、GPU 不可用和高成本效果必须有可解释诊断；降质不改变时间线内容、源解释或 HDR 色彩语义。

### VP-B04：Source 时间域、VFR 和 In/Out

**已有**：Source 当前维护独立 frame/fps；已有 In/Out、`range_changed` 及时间线插入链；共享 Timecode 支持 Rational/DF/NDF。场景切割存在 `SourceFrameTimeIndex`，不等于普通 Source 播放已经完整支持 VFR。

**输入 → 输出**：媒体 metadata、源起始时间码、帧索引／PTS、用户输入 TC／范围 → 精确源位置与可消费的源区间。

**实现步骤**：

1. 区分 Source 帧索引、Source PTS、Timeline frame、显示 TC；在边界明确转换，不用 Timeline fps 解释 Source 帧。以 `Timecode`／Rational 替代新的 float 格式化和 `round(fps)` 近似。
2. 评估将 `SourceFrameTimeIndex` 的可复用协议提到共享 media 层，或用已有 decoder 时间索引适配；不要求用户先运行 Scene Cut，也不在 UI 同步扫描全文件。
3. 定义 UI Mark Out 与编辑 planner 区间的关系。UI 可继续显示包含 Out 帧的标记，提交前按现有 command/planner 的真实边界统一转换；一帧范围不得变成零长度。
4. 校验空范围、In>Out、负值、超末帧、单帧图像、只音频素材；按现有可编辑流决定按钮能力，不用 `None` 和帧 0 混淆。
5. 源素材状态按 asset ID／有效媒体版本保存，换源恢复前验证媒体变更和帧数；恢复不能覆盖用户当前时间线范围。

**门槛**：23.976/25/29.97DF/59.94DF、非零源 TC、VFR、倒放／变速片段均能定位预期帧；DF 非法时间码被明确拒绝；In=Out 插入一帧；Source Range 与导出／时间线消费一致。

**风险／失败路径**：离线媒体、未知时长、过期索引不回填假总帧数；异步建立索引允许取消，资产换版使旧索引失效。

### VP-B05：Reverse／Stop／Loop 和 transport 适配

**已有**：Timeline controller 的 JKL／reverse／loop／range 完整入口可复用；Source 只有正向 timer、play/pause/seek 和归零 stop。两侧现有 toolbar 的 Home/End／逐帧等已接通。

**输入 → 输出**：monitor ID、动作、方向／速度／范围 → 原控制器命令或 capability 不支持理由，回填实际播放状态／方向／速度。

| 操作 | Timeline 的复用 | Source 新增工作／政策 |
|---|---|---|
| Play／Pause | 现有 `play/pause` 与 TimelinePanel 入口 | 保留原连续音频和背压，接入仲裁与公开状态 |
| Reverse／JKL | 复用 `play_with_speed`、`jkl_backward_press/jkl_forward_press/jkl_stop` | 提供反向 clock／seek 窗口及 bounded cache；音频反向未支持时 capability 明示，不假装正常反向音频 |
| Stop | viewer“停止于当前位置”应适配 `pause` + JKL reset | 原 `stop()` 会 seek(0)，UI Stop 若要求原位停止须另设语义；回起点仍由 Home 单独负责 |
| Loop | `set_loop`、`set_playback_range` | 新增素材全段／有效 InOut 循环及边界音频重定位，避免末尾永远暂停 |
| 逐帧／Home／End | 复用已有 seek 及 playback range | 保留原 handler，边界 clamp，VFR 按帧索引推进 |
| Previous/Next Edit | 在 Timeline 的剪辑边界导航入口适配 | Source 无“时间线剪辑点”；采用明示的起末／标记导航，不能把逐帧按钮改名假称剪辑点 |

**实现步骤**：建立动作 capability → adapter → 原命令 → 状态回填链；输入框和菜单焦点拦截按现有快捷键规范；重复 keypress 不产生多个播放 timer；drag scrub 结束后按开始前状态决定是否恢复播放。

**门槛**：每种动作按钮与快捷键行为一致；Stop 保留当前位置、Home 归起点；Loop 在源末尾和一帧范围不死循环；JKL 加速／换向后 UI 状态准确；不影响已有 Timeline 音画同步。

**风险／失败路径**：反向长 GOP 需要实测缓存和 seek 开销，初版可限制速度并声明能力；不得通过 UI 线程同步逐帧解码凑反向播放。

### VP-B06：Match Frame、marker 和范围

**已有**：时间线双击 clip 已有 `_on_clip_open_in_source`，会解析 asset、Source Patch、绑定音频并 seek；Timeline marker 已有 `marker_commands.py` 和 TimelineView 的 marker 操作；Source In/Out 与 Timeline 源插入已连通。

**输入 → 输出**：活动 Timeline／clip／播放头或 Source asset／frame → 精确目标 Source／Timeline 位置；marker／范围操作走既有 command/service 并回填。

**实现步骤**：

1. 将 Match Frame 接入现有 clip→source 映射／打开链，不能简单用 `timeline_frame - clip_start`：需覆盖 source_start、trim、retime、reverse、嵌套和 transition 选中侧。
2. 定义多轨时目标：显式选中片段优先，未选中时使用既有命中／可见轨道规则；无可解析媒体、Motion Graphic、合成片段等返回 capability 原因。
3. Timeline In/Out 复用时间线范围权威模型；Source In/Out 保持源域，不复用同一 pair 字段。源 marker 若模型未支持，先不启用源 M 快捷键；需要工程保存时另做 schema 版本／迁移并保留未知 payload。
4. 如提供反向 Match Frame（Source→Timeline）需处理同素材多次使用的候选列表，用户选择目标 clip；不能任意跳到第一处。

**门槛**：裁切／变速／反向片段在 Match 后显示正确原始帧；绑定音频和 channel map 沿用已有解析；marker 增删改可 undo/redo 并持久化；无目标时不移动任何播放头。

**风险／失败路径**：嵌套循环、离线资产、transition 双侧歧义显式报因；保持用户原选中对象和 Source 未提交范围。

### VP-B07：Viewer capability 与菜单真实接线

**已有**：Program 有 `preview_quality_changed` 和质量档；Guides、Zoom、Transform、timeline selector 各自已有信号。代理源解析／调色和特效渲染已有领域实现，不能从新增 UI 直接跳过 pipeline。

**输入 → 输出**：当前 monitor／asset／page／renderer 能力 → 菜单 enabled/checked/reason 快照；有效请求 → 服务应用结果和真实 UI 回填。

| 菜单 | 复用／新增任务 | 验收／不可用行为 |
|---|---|---|
| Proxy Handling | 对接现有 source resolver／代理可用状态；区分“优先代理”与“强制代理” | 缺代理时给原因；不能只改菜单 checked；Timeline 与 Source 各自请求域清晰 |
| Preview/Timeline Resolution | 原 Program quality 控件和 `preview_quality_changed` 可复用；预览缩放与工程时间线格式分开 | 改预览不改成片分辨率；真正改 Timeline 格式走 command、revision、undo 和预览失效 |
| Bypass Color/Fusion/Effects | 通过已有效果栈／预览 service 建临时监看 bypass，不删模型 effects | 仅监看 bypass 与工程编辑 bypass 明确区分；导出继续使用工程权威状态，缓存 key 含 bypass 状态 |
| Source Tape | 当前普通 Source 是单 asset，不能靠改标题获得 tape | 新立素材有序集合、段偏移映射、跨段 seek／TC／InOut 和换源预取规格；完成前 disabled 并说明 |
| Guides/Transform/Zoom | 保留原 widget 和参数信号，不复制画面变换 | 活动侧正确；Source 不支持的 clip transform 不启用；UI 缩放不改变 clip 参数 |
| Fullscreen／外部监看 | 复用 Program fullscreen 与 monitor_frame_ready／monitor_gpu_frame_ready | 聚焦目标与输出目标明确；Source 不具备同能力时禁用 |

**实现步骤**：以 feature capability／registry 为单一事实源；拟新增 capability DTO 只表达动作与原因；服务成功后更新菜单，失败则恢复原 checked 状态。不要预先声明所有菜单项支持。

**门槛**：所有 enabled 菜单至少有一条真实调用和结果测试；代理缺失、无 timeline、交付中、GPU 不可用时状态准确；preview/export 分离有显式测试。

**风险／失败路径**：代理已删除、格式重建失败、effect capability 不匹配均有可诊断结果，不静默丢效果或回退成错误色彩。

### VP-B08：从 Source 拖拽视频／音频／A+V

**已有**：Media Pool `build_media_pool_drag_mime`、内部 asset MIME 和 TimelineView drop／source edit planner 已存在；Source patch 可选择目标视频／音频轨道。普通 asset MIME 只表达 asset 列表，不能假设它已携带 viewer 当前源区间和只音频意图。

**输入 → 输出**：Source asset ID、有效 In/Out、stream intent、源 fps／时间域、目标时间线／轨道／drop frame → 单次既有编辑计划和 command 提交。

**实现步骤**：

1. 在共享合同位置扩展有版本的内部 MIME payload，明确 `asset_id`、源范围及边界语义、`stream_intent=av/video/audio`；不把机器绝对路径当资产身份，不信任拖拽文本。
2. 源画面／视频图标／音频图标分别产生同一结构不同 intent；拖拽阈值后才启动，不把点击激活误判为编辑。
3. Timeline drop 校验 asset 是否属于当前项目、源范围／stream 可用、目标轨道锁定／类型／patch；复用 `_source_edit_plan` 和 `_apply_source_edit_plan`，统一音频映射与绑定偏移。
4. 含源范围的 drop 必须比通用 Media Pool MIME 优先解析；URI fallback 不得导致内部素材被重复导入。
5. 成功一次只产生一次 command／revision；取消拖拽没有项目变更。按已有 linked selection／A/V link 语义决定 clip 关联。

**门槛**：三种 intent 在 24→25fps、单帧范围、只音频、无音频、锁轨、source patch 禁用流时均有明确结果；undo/redo 还原轨道、clip 与范围；连续撤销后预览及工程序列化一致。

**风险／失败路径**：损坏／未来版本 MIME、拖拽期间资产删除、drop 到另一项目、超时素材拒绝且不留半条 clip；错误不能只显示一帧假成功。

### VP-B09：用户配置和旧 dock 布局兼容

**已有**：QMainWindow 的 page layouts、LayoutStore、出厂 default layout 及内置工作区预设负责停靠状态；本轮保留 `dockProgramViewer`，移除 `dockSourceViewer`。单／双模式和 splitter 尺寸已有本地配置、类型校验及延迟写盘；旧可见面板 ID 已做读取兼容映射。后续不重复实现这些基本接线。

**输入 → 输出**：旧／新布局、用户 viewport 偏好 → 单一 Viewport 停靠状态和有效显示模式；不修改工程媒体数据。

**实现步骤**：

1. 如需扩展用户配置，建立独立 versioned viewport 段，并迁移本轮两个已有 key；补充单视图目标及媒体会话恢复政策。实际播放中状态、texture、绝对路径和解码缓存不保存。
2. 加载时校验类型／枚举／有限比例；未知值回到单视图 Timeline，并记录配置诊断；程序回填不触发用户保存回路。配置写入合并／延迟，不能拖 splitter 每像素写盘。
3. 除旧 Qt dock state 忽略已删除 dock 外，检查旧 Source 曾单独 floating、Program 曾 hidden/tabified 的场景，确保恢复后 Viewport 可被找到和打开。
4. 对当前 work mode／page profile 的进一步修复做最小迁移并标版本；核验本轮已同步的 Editing 预设、PanelRegistry、默认 LayoutStore 面板和旧 ID 映射。不得清空全部用户自定义布局解决一个 Source dock 问题。
5. 内置 Audio／Captions／Review 预设是否强制 Timeline 单视图须先定规则；如需临时切换，恢复原显示偏好而不覆盖持久化用户选择。

**门槛**：旧布局 Program 隐藏＋Source 浮动、默认 tabbed、损坏配置、新安装、重启和不同 work mode 均显示单一面板入口；重启恢复合法显示偏好；工程保存内容不因单／双切换发生业务变化。

**风险／失败路径**：restoreState 失败使用现有局部默认布局途径并保留其他页面；layout DB 写入失败不阻塞播放。

### VP-B10：Deliver 往返、全屏和外部监看

**已有**：Deliver 移动真实 Program／Timeline 实例，没有复制控制器。本轮前端将 Program 借出后由容器恢复，保留原信号；这仍需完整运行验证。

**输入 → 输出**：Edit→Deliver→Edit、fullscreen、页面关闭 → 同一 Program 对象在正确宿主和正确显示模式，时间线状态连续。

**实现步骤**：

1. 为借出／归还建立幂等边界：重复 attach／detach 不重复连接；不再假设 `program_dock.widget() is program_viewer`。Viewport dock 的 widget 始终为容器。
2. 进入 Deliver 暂停 Source 并释放无用请求；Deliver 强制 Timeline 监看，隐藏 Source 侧和切换入口；返回恢复此前单／双布局及 active，保留真实播放头。
3. 检查 Program fullscreen 的父级记录：借出时全屏、全屏中切页、Esc 返回均归还有效布局；浮动 Viewport 不生成悬空 parent。
4. 外部监看保留 Program 输出策略，不因 active Source 把非时间线画面误发给现有 Program monitoring 消费者。

**门槛**：往返 30 次 viewer/renderer/controller 对象身份、连接数和时间线一致；Viewport dock 未被 Program 替换；单／双、浮动、全屏各组合无黑屏／重复 transport；关闭 Deliver 时旧 callback 不访问已删除 widget。

**风险／失败路径**：借出过程中取消切页或窗口关闭，恢复操作必须可重入；找不到原 host 时使用存活的安全宿主并诊断，不能 setWidget 覆盖其他面板。

### VP-B11：关闭、取消、超时和 fallback

**已有**：Source CPU fallback worker、GPU watchdog、解码 generation 校验和音频 cleanup 已存在；renderer 的 deferred release／pool retirement 已有规则。

**输入 → 输出**：关闭面板／关闭主窗／换工程／取消 pending seek／GPU timeout／decoder error → 停止请求、释放音频／worker／slot，或有正确状态的 CPU fallback。

**实现步骤**：

1. 画出 shutdown 顺序：仲裁器拒绝新请求 → 停 timer／音频 → 撤销会话 generation → 断开／屏蔽回调 → 在原线程释放 renderer／decoder → widget 销毁。优先沿用项目既有 shutdown 入口。
2. Source fallback 不能因隐藏又被 watchdog 自动拉起新 decode；已在途 CPU 请求仍校验 asset、generation 和关闭 token。
3. GPU 与 CPU 同帧竞争时只有有效最新结果可呈现，另一结果走 release；GPU失败不允许常驻无界 CPU cache 或主线程同步解码。
4. 关窗／换工程不等待无界 thread join；复用现有可取消 worker 和超时诊断。音频停止必须验证设备侧已安静，不只改播放图标。

**门槛**：gpu provider rejected／timeout、CPU decode error、离线素材、解码中关闭、切工程、反复开关 dock 均无崩溃／悬挂／残留音频；失败状态可见，恢复新素材后可播放；slot 最终回收。

**风险／失败路径**：旧 renderer owner 被释放后的 ack、decoder 无响应、Qt 删除后回调、重复 delete 均用有边界的日志和测试覆盖，不用宽泛 except 静默吞掉领域失败。

### VP-B12：HDR、真实素材和性能 smoke 门槛

**已有**：Program／Source 各自已有 GL 合约测试、Source 播放／背压测试、HDR GPU 合约和媒体时间码测试。UI 合并本身不等于通过 4K 或 HDR 验收。

**交付物**：在 `docs/validation/` 下保存一份实测报告，记录 commit、平台、GPU、媒体元数据、测试命令、pass/skip/fail、性能基线和已知限制。缺硬件的 GPU 测试明确 skip，不能统计为通过。

| 验证组 | 复用的现有测试／素材 | 新增验收内容 |
|---|---|---|
| UI／状态／配置 | `tests/edit/test_workspace_presets.py`、`tests/app/test_dock_layout_controller_coverage.py`、`tests/edit/test_program_viewer_toolbar.py` | 单一面板、单／双、active、空素材、焦点、菜单 capability、旧配置迁移 |
| 跨页和父级 | `tests/deliver/test_deliver_workspace_layout.py` | Program 从 Viewport 内借出／归还、容器身份、全屏、重复往返 |
| GPU／ack | `tests/edit/test_source_viewer_gl_contract.py`、`tests/edit/test_program_viewer_shared_texture.py` | 隐藏时丢弃帧 ack、旧 generation、旧 owner、晚到回调、pool 上限 |
| Source 连续播放 | `tests/edit/test_source_viewer_playback_chain.py`、`tests/edit/test_source_viewer_playback_backpressure.py`、`tests/edit/test_source_viewer_scrub_preview.py` | 双视图预算、互斥音频、Reverse／Loop、scrub 后恢复状态 |
| TC／VFR／范围 | `tests/media/test_timecode.py`、`tests/media/test_f54_vfr_alignment.py`、`tests/fixtures/scene_cut/vfr_cut_24fps.mp4` | DF/NDF 源 TC、源／时间线不同 fps、单帧范围、VFR Match Frame |
| HDR／色彩 | `tests/render/test_hdr_gpu_contract.py` | 10-bit PQ/HLG、SDR、透明图、CPU fallback；Source 与 Program 的既有解释／ODT 不被新容器重复套用，内部值不被无理由 clamp 到 0–1 |
| 编辑提交 | source patch／编辑 commands 现有相关测试 | 拖拽只视频／只音频／A+V，InOut 的真实插入、undo/redo、revision 和重新打开工程一致 |
| 真实机 smoke | 同一组 4K60、4K 长 GOP、VFR、只音频、图片、HDR、缺失素材 | 10 分钟稳定性、50 次激活切换、200 次显示切换、30 次 Deliver 往返、关窗／换工程 |

Focused test 可在项目虚拟环境运行；Qt 逻辑使用 offscreen，真实 GPU 及可视质量必须另做实际环境测试：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/edit/test_workspace_presets.py tests/edit/test_program_viewer_toolbar.py tests/deliver/test_deliver_workspace_layout.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/edit/test_source_viewer_playback_chain.py tests/edit/test_source_viewer_playback_backpressure.py tests/edit/test_source_viewer_scrub_preview.py -q
.venv/bin/python -m pytest tests/media/test_timecode.py tests/media/test_f54_vfr_alignment.py tests/render/test_hdr_gpu_contract.py -q
```

新增测试按实际提交路径补到命令中；不要将以上命令当作已运行结果。`tests/edit/conftest.py` 的 Qt shelter、GC 生命周期和 fallback worker 清理已有约定，测试应沿用，避免制造测试期悬空 QWidget。

**交付门槛**：已启用功能的 focused test、真实机 smoke 和失败路径检查通过；每个 skipped GPU 项有原因和补测设备；没有可复现的 frame 串源、音频双播、未 ack 饥饿、关窗崩溃和工程命令状态不一致。性能未达目标时提供实测数据、降质策略和限制，不能以 UI 完整替代后端验收。

## 5. 建议执行批次

1. **稳定容器后的最小安全批次**：B01 → B02，穿插 B10、B11。先完成互斥、槽位和跨页生命周期，保留尚未实现的功能禁用状态。
2. **时间和 transport 批次**：B04 → B05 → B06，优先复用 Program 控制器与现有 Source 编辑链；Source Reverse 可独立审查和测量。
3. **交互与策略批次**：B03 与 B09，随后 B07／B08。Source Tape 另立设计，不阻塞单／双模式交付。
4. **逐批验收**：每批做 B12 对应部分；最后执行全部已启用能力与真实素材 smoke。涉及渲染核心前按 AGENTS.md 先分析方案、审查、再实现；修改后的资源／取消路径必须复审。

每批结束记录：已启用 capability、仍禁用项、真实测试结果、需要下一批处理的问题。不要以一个“Viewport 后端完成”标记覆盖未实现的 Source Tape、反向音频或硬件受限项目。

---

## VP-B01–B12 实施记录（2026-11-09，后端批次）

基线 `283224a5` → 终点 `88dbba85`（13 个提交）。逐项：

| ID | Commit | 落地内容 | 新测试 |
|---|---|---|---|
| B01 | `9c856ce4`/`b8cda235` | `viewport_playback_arbiter`（纯域：可序列化快照/起播前先停对侧且保留播放头/拒绝回稳定原因键/单请求 revision/shutdown 拒新请求）+ `viewport_playback_coordinator`（QObject 适配层，"program"↔"timeline" 映射，`install_viewport_playback_coordinator` 单入口接线）+ `SourceViewerWidget.is_playing` 公开态 | 16+6+1 |
| B02 | `5aadf5ec` | `SourcePresentGate`（被顶掉/不匹配/宿主亡/清仓四类丢弃全部显式 ack + 计数）；file_controller 配对改走 gate、widget-None 与 RuntimeError 关窗竞态归还 slot；widget hideEvent/showEvent——隐藏零周期解码与 readback、可见性 revision 与媒体代数分离、watchdog 隐藏即停、200 次切换 slot 收支闭环、destroyed 归还显示中 slot | 9+8 |
| B10 | `332c5f9a` | deliver_workspace：借出前退出 Program 全屏（防空壳+父级悬空）；原 dock 被占不覆盖（留存活宿主 + `edit_panels_returned(False)` 诊断）；`edit_panels_borrowed/returned` 信号钩子供 B01 策略与 Viewport 模式接线；30 次往返对象身份/单连接/占位符断言 | 8 |
| B11 | `4173301d` | `SourceViewerWidget.prepare_for_shutdown()`（幂等关闭序列：token 拒一切新请求 → 撤销会话 generation → CPU 解码器/显示 slot/scrub 音频线程按序释放）；`closeEvent` 先于 decode worker.stop 调用（原顺序会让 QThread 随窗口析构） | 3 |
| B04 | `08086581` | `SourceTimeDomain`（float→Rational 帧率映射、非零源起始 TC 偏移、DF 非法时间码显式拒、VFR 走 `SourceFrameTimeIndex` PTS、In/Out Out-包含语义与空/In>Out/负值/超末帧/无帧稳定原因）+ `SourceMonitorStateStore`（指纹+帧数版本校验，换版作废） | 17 |
| B05 | `eeccea7a` | `EditPlaybackController.stop_in_place()`（pause+JKL 复位，播放头保留；legacy `stop()` 归零语义不变）；Source `stop_in_place`/`set_loop`（素材全段或有效 In/Out 循环、边界音频重定位、一帧范围每 tick 一次有界回卷）/`play_reverse`（仅视频 1x，`source_audio_reverse_supported=False` 如实声明）/slider 释放按按下前播放态恢复 | 13 |
| B06 | `bd3998e1` | `match_frame`（映射一律委托权威 `timeline_to_source_frame`，覆盖 trim/retime/reverse/freeze/mixed fps；选中侧/覆盖片段策略；motion graphic 等虚拟素材回 `no_source_media`）+ 双击"在 Source 打开"改走权威映射（2x 片段播放头 25 → 开帧 50，朴素公式给 25）+ `find_source_frame_uses` 同素材候选只列不跳 | 13+2 |
| B03 | `a609db13` | `DualViewBudgetPolicy`（播放侧 PLAYHEAD+周期请求、非活动/空闲静帧侧禁周期、隐藏侧最低优先；请求尺寸帽 min(视口,原生) 不放大；队列帽对齐渲染队列/latest-only）+ `collect_dual_view_diagnostics` 可序列化诊断 | 6 |
| B09 | `61eb3419` | `parse_viewport_preferences`（版本化段 + 前端现行两键迁移 + 逐项校验回落 + 诊断；未来版本整体回落单视图 Timeline；缺失键静默默认） | 7 |
| B07 | `276149fd` | `evaluate_viewer_capabilities`（菜单单一事实源：代理三态矩阵、已落地能力如实 True、Source Tape/反向音频/源 marker/monitor bypass/Source 外部监显式 False+原因；Deliver 页强制 Timeline 监看） | 7 |
| B08 | `4b9dd8b9` | `application/x-topos-source-clip` 有版本载荷（asset_id 身份而非路径、Out-包含源窗、av/video/audio 流意图、可选源帧率；解码永不抛错，QByteArray 鸭子兼容）；timeline_view 优先于通用 asset MIME 解析并按项目校验；锁轨/坏目标零写入回因；insert 模式直连 `insert_clip(in,out)`，overwrite 走 `_add_asset_as_clip(source_range, stream_intent)` 复用 subclip 跨帧率换算与 F2.1 流闸（24→25fps 窗口 48→50） | 7+7 |
| B12 | `88dbba85` | 验收报告 `docs/validation/edit_viewport_20261109/VP-B01-B12_backend_validation.md`（含 §3 前端合流接线点清单与 §5 证据边界） | — |

**新增测试合计 130，全部先红后绿**（新模块 ImportError 红 / 接线类行为
断言红 → 实现转绿）。focused 命令组（计划 §VP-B12 指定）：30+41+63
passed；`tests/render/test_hdr_gpu_contract.py` 2 passed；`tests/deliver`
全目录 306 passed。

**全量回归**：`QT_QPA_PLATFORM=offscreen pytest -q tests/edit` →
**12585 passed / 14 failed / 43 skipped（1582s）**。14 个失败逐项归属
基线已知集合：n03_grade×5、n03_mask×3、ui2_aud3、
linked_ripple_revision_proxy、timeline_disable_shortcut（稳定），
affordance×2、pointer×1（加载敏感型像素比对）——相对基线
（12458/14）**零新增稳定失败**，净增 327 通过。

**仍禁用/未实现（显式声明，不冒充）**：Source Tape（素材集合+虚拟时
间域未设计）、反向音频（能力位 False）、源 marker（工程 schema 未定，
B06 推迟）、monitor bypass（预览 service 未接）、Source 外部监看——均
在 B07 注册表带原因禁用。

**证据边界**：真机 4K60 双视图性能、HDR 可视一致性、手动操作手感、
`tests/test_videos` 真机 smoke 属前端容器合流后的整页联测（验收报告
§5）；§3 所列"前端合流时接线点"在容器提交线合入前无运行调用方，契约
由假件测试钉死（信号名与前端现行实现一致）。本计划文件属前端提交线，
本实施记录随该线一并入库。
