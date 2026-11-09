# Viewport 后端复查与剩余接线计划

日期：2026-11-09。范围：Edit VP-B01–B12 提交（`9c856ce4` 至 `88dbba85`）与当前剪辑／调色 Viewport 前端合流后的生产调用链。

本表是新一轮复查结果，不覆盖原实施记录。用户确认可能仅完成了剪辑计划；**Color 后端尚未实施**，继续执行 [Color 后端计划](2026-11-09-color-viewport-backend-plan.md)。调色前端重排、大播放按钮恢复和已有信号兼容，不代表 Color 新后端能力已接通。

## 结论与状态边界

Edit 已交付一批可复用的领域组件、Source 播放／资源修正及 Timeline MIME 消费端；其中部分尚无生产调用方。原 [后端验证报告](../validation/edit_viewport_20261109/VP-B01-B12_backend_validation.md) §3 已说明“域件交付、前端合流待接线”，此边界应保留。不能仅凭假控制器单测通过，把整页互斥播放、预算生效或完整拖拽标记为验收完成。

| 组件 | 本次核对的实际状态 | 后续批次 |
|---|---|---|
| `viewport_playback_coordinator.py` | installer 未被 app 调用；按钮、Space、JKL、scrub 恢复仍经旧链直接起播 | I01 |
| `source_time_domain.py` | `SourceTimeDomain`／`SourceMonitorStateStore` 未被 Source widget 消费 | I02 |
| `viewport_playback_budget.py` | 领域预算有测试，Source／Timeline 调度执行端未消费政策 | I06 |
| `viewer_capabilities.py` | registry 未接菜单；不得因领域标注 supported 就启用没有 handler 的控件 | I03 |
| `source_drag_payload.py` | codec 与 Timeline 接收存在；Source 画面／流图标没有拖出生产端 | I04 |
| `match_frame.py` | Timeline 双击已有权威映射；Viewport 菜单、F、反向候选选择尚缺 | I03 |
| `viewport_preferences.py` | 本次小修接入版本段、活动对象和旧键迁移 | 已修 |
| Deliver 借用／归还信号 | 已声明／发射，尚未连接播放仲裁器 | I01 |

## 本次直接修复的小问题

| 问题 | 修复边界 | 回归证据 |
|---|---|---|
| 正放切倒放仍有正向连续音频 | `SourceViewerWidget.play_reverse` 先暂停既有音频；反向仍遵守 video-only 政策 | Source transport 转场测试 |
| Source Loop 包含式 Out 从未显示 | 越过 Out 后才回卷；一帧循环至少保持一源帧时长 | Out／单帧范围测试 |
| 倒放拖动释放后错误恢复正播 | scrub 记录并恢复原方向 | reverse scrub resume 测试 |
| 隐藏时 pending GPU 帧阻塞逻辑时钟 | 隐藏时绕过显示背压；显示时仅刷新当前位置 | hidden pending 测试 |
| 隐藏后迟到 GPU 失败仍提交 CPU fallback | 隐藏时抑制异步 fallback／watchdog；保留显式 seek 路径 | late fallback／show refresh 测试 |
| Deliver 在 wrapper 查找 fullscreen 状态 | 先取得真实 Program canvas，再退出全屏后借出 wrapper | 真实 ProgramViewerWidget 回归 |
| Stop 通过播放按钮模拟 | Source 直连 `stop_in_place`；Program 发信号接 controller `stop_in_place`，不归零／不误起播 | UI 路由与宿主测试 |
| Source Loop 菜单一直禁用 | 接现有 `set_loop`；菜单打开时回填 checked，无素材禁用 | UI menu policy 测试 |
| 偏好只保存旧扁平字段 | 版本化读写／迁移、保存活动监视器、保留合法双视图比例；Deliver 临时状态不写偏好 | 偏好／dock 测试 |

后续补充的小型输入校验修复与最终测试结果记入本轮 [复查验证记录](../validation/viewport_review_20261109.md)。以上修复没有把新后端能力全面启用。

## 实施任务表

优先级表示剩余工作的实施顺序。I01 是启用新 Viewport 播放能力前的阻断项，不能只添加一行 install 即关闭。

| ID | 优先级 | 目标 | 依赖 | 预计工作量 |
|---|---|---|---|---|
| I01 | P0 | 全入口播放仲裁、真实反馈与页面生命周期 | 无 | 2–4 人日 |
| I02 | P1 | Source 时间域、范围校验和换源状态真正接入 | I01 状态契约 | 2–4 人日 |
| I03 | P1 | capability／Match Frame 宿主与菜单回填 | I01、I02 | 2–3 人日 |
| I04 | P1 | Source 拖出生产端、身份校验与事务一致性 | I02 | 2–4 人日 |
| I05 | P1 | 关闭／切换工程的可复用 Source 释放边界 | I01 | 1–2 人日 |
| I06 | P1 | 双视图预算执行与真实性能验收 | I01、I02 | 2–4 人日 |
| I07 | P1 | 整页验收、失败路径和实施记录闭环 | I01–I06 | 1–2 人日 |

合计约 12–23 人日，需按实际媒体与现有服务能力调整。禁止用此估算反推“模块已有，所以只剩几行 UI”。

### I01：全入口播放仲裁

**真实接线位置**：`src/app/controllers/dock_layout_controller.py`、`src/app/controllers/file_controller.py`、`src/app/main_window.py`、`src/app/controllers/page_controller.py`、`src/features/edit/ui/edit_page.py`、`src/features/edit/ui/timeline_panel.py`、`src/features/edit/ui/source_viewer.py`、`src/features/edit/ui/viewport_playback_coordinator.py`。

1. 建立一次性的宿主 adapter 与公开状态反馈，安装时同步已打开 Source asset ID、timeline availability、两路实际 playing、layout／active monitor、当前页面。不要依赖“稍后用户再点一次”初始化。
2. 所有起播入口经过同一仲裁：Source 原按钮／Space／程序 play／scrub resume；Timeline 原按钮／Space／J／L／程序播放；素材与时间线打开；跨页与 Deliver 借还。暂停对方成功后才能开始本侧，保留双方播放头。
3. 保留原链中的准备步骤：Timeline `_sync_playback_duration`、音频桥、JKL 档位／方向、动态 trim，不把旧按钮简单改接裸 `controller.play()` 而绕过这些行为。scrub 音频试听与连续播放的声音政策须单独定义。
4. Source 增加明确的只读状态／状态变化信号，涵盖 play／pause／到尾／loop／scrub／close／shutdown。coordinator 目前只订阅 Timeline `state_changed`，Source 自然停止会导致快照过期。
5. 修正 coordinator 反馈后不 emit `snapshot_changed`、命令 sink 未核对静默拒绝的缺陷；暂停或起播返回后以控制器真实状态判定 accepted。异常记录明确原因，避免宽泛 catch 后失去诊断。
6. 连接 Deliver borrowed／returned、page enter／leave、shutdown；临时强制 Timeline 不覆盖用户单／双偏好，归还恢复原布局。连接清理和重复 install 要有明确所有权。

**验收**：真实 QWidget host 按钮／Space／JKL 交错播放，至多一条连续音频；切 active／单双只改变显示；Source 到尾后再次 Play 可起播；无素材／shutdown／pause失败不虚报成功；Edit→Color→Deliver→Edit 不遗留源播放、不丢 Timeline 范围与 JKL 状态。假 arbiter 测试作为补充，不能代替入口集成测试。

### I02：时间域与源状态

**证据**：`SourceViewerWidget.open_asset` 仍取 `asset.fps or 24.0`，换源重置 frame／In／Out；`_frame_to_timecode` 未消费 SourceTimeDomain 的 start TC／VFR；setter 未统一调用 `validate_range`。

1. 明确 metadata 中 Rational fps、起始 TC、drop-frame 类型、VFR time index 的权威来源；VFR 索引异步取得，UI 不探测／解码阻塞。
2. 接入 SourceTimeDomain；显示 TC、seek、In／Out、loop、拖出范围使用同一域。区分源帧、源秒、Timeline 帧，不把 VFR 帧号按 CFR 硬换。
3. 建立稳定 asset fingerprint／revision；换源前保存、内容换版后失效、打开后恢复，避免仅用跨项目可重名的 asset_id 命中旧状态。
4. 校验负值、反置／越界、一帧范围、未知时长；只有加载新源完成后才能启用相关动作。pending decode generation 与状态恢复顺序必须一致。

**验收**：23.976／29.97 DF／不同 start TC／VFR、A→B→A、素材换版／丢失／重连，回到准确画面；In／Out 与拖出长度包含式语义一致；不出现旧资产帧。

### I03：菜单能力与 Match Frame

1. 由宿主收集真实上下文供 registry；UI 可用性还必须有实际 handler。Proxy ready 不代表切代理服务已经存在，Match Frame 计算器存在不代表候选 UI 已完成。
2. 核实 registry 前置条件：Source→Timeline 需打开的 Source 和 Timeline；Deliver 中涉及 Source 的动作统一禁止；无素材 Source loop／reverse 不启用。当前 `match_frame.source_to_timeline` 被 Deliver 特意排除于禁用条件外，需按真实政策修正。
3. Program F／菜单复用当前 Timeline／selection 的 `resolve_match_frame` 与已有 EditPage open-source 链；Source→Timeline 同素材多处使用须展示候选，不能默认第一处。
4. Proxy／bypass／source tape／markers 等服务缺失项保持禁用，返回具体原因；失败恢复 checked；操作回填不重复发送命令。

**验收**：空项目／仅 Source／仅 Timeline／Deliver／GPU fallback 的菜单状态可信；retime／trim／反向 clip 与双击／F 结果一致；多用途候选取消不改变播放头。

### I04：完整 Source 拖拽与事务

**已复现的问题**：P1：Insert 模式把 video intent 插成 video＋audio，把 audio intent 插出两条 audio clip；根因是 `insert_clip` 不接收 stream intent，继续按 asset 的流创建配对。P2：先建轨后插入失败，drop 被拒但 video tracks 从 1 变 2，且留下 1 个 Undo entry。以下任务必须同时解决这些一致性问题，不能只补 QDrag 发起端。

1. 在 Source 画面及 Video Only／Audio Only 图标实现 drag threshold、QDrag 与版本 MIME 生产；定义无 In／Out 时的全段政策和不存在流时的原因。
2. 在当前项目服务中验证 project identity／asset fingerprint，不能只按字符串 asset_id 找到同名资产就接受。使用可信 metadata 校验源范围、时长和 stream availability；忽略载荷对源 fps 的任意覆盖。
3. 对照 `_handle_source_clip_drop` 的 insert 与 overwrite 两条分支，确保 av／video／audio 三种 intent 均经过一致 planner，不因 insert 直接调用旧 `insert_clip` 丢掉 stream intent。
4. 新建轨道、插入 clip／配对音频、项目／selection／undo 的变更作为一个可回滚事务；失败、锁轨、资源缺失时零写入，不能先建轨再留空轨。禁止只在 UI 内部补偿部分模型。
5. 损坏内部 MIME 的拒绝／通用 MIME 回退政策与实际接收端统一说明，不产生 URI 二次导入；drop 后无论成功／失败均清理 drag state。

**验收**：三种 intent × insert／overwrite × 同／跨 fps × 锁轨／新轨；单次撤销恢复全部模型；失败无孤立轨／半条音频；跨项目同名 ID 拒绝；越界范围、坏 fps／未知版本拒绝且不崩溃。

### I05：工程切换的 Source 生命周期

**证据**：正常 MainWindow close 已接 `prepare_shutdown`，但 `src/app/controllers/edit_controller.py:_close_current_project` 没有可复用的 Source close 链。仅卸载 Color video 不会释放 Edit Source 状态。

1. 保存确认成功且工程确实关闭后，暂停 Source／音频，调用可复用 `close_source`，清 asset／marks／state snapshot／pending requests，配对归还 GPU slot；取消保存／取消关闭不清当前工程。
2. 换工程不能调用永久标记的 `prepare_shutdown` 后直接复用同一个 widget；应用退出才使用永久 shutdown token。
3. 关闭失败、Deliver 借用中、浮动／全屏中都要经原资源所有者清理，不能在 UI 线程释放 GL 对象。

**验收**：Source 播放时关闭／换工程无旧声／旧帧／slot 残留；新项目再次打开和播放正常；取消关闭仍可继续；pending GPU／CPU 结果迟到不污染新工程。

### I06：预算真正作用到调度器

1. 确定宿主何时生成 DualViewBudgetPolicy 输入（active、visibility、playback、精确 seek、proxy readiness），以及输入变化后如何更新。
2. 把 queue cap／priority／preview size cap 作用到 Source worker、Timeline decoder／renderer 的既有任务队列。只输出诊断 DTO 不算实施完成。
3. hidden／inactive 连续播放降低显示成本，仍保持时钟／音频政策；显式精确 seek／恢复显示允许一次请求。禁止饿死前台帧或用预算丢弃 slot ack。
4. 将预算选择、actual applied limits、fallback／drop／latency 记录到可查询诊断，避免“政策说受限，执行端仍原样跑”。

**验收**：真实 4K HDR 单／双、Source／Timeline 轮流 active、hidden／show／seek，队列上限可观测；记录 CPU／GPU／VRAM／decode latency／帧丢弃及音画同步，对比单视图基线；无 busy loop、阻塞与泄漏。

### I07：最终整页验收与交付

1. 新增真实宿主集成测试，覆盖 I01–I06 的入口与失败路径；保留低成本纯领域测试，但用例标题不能声称已跑真实播放。
2. 使用用户媒体执行跨页／全屏／浮动／单双／工程切换／关闭循环；分别记录 CPU fallback、GPU slot、HDR 和音频证据。
3. 更新旧计划任务状态，拆开“域件完成／宿主接线完成／媒体验收完成”，附提交与测试命令；真实 4K/HDR 未跑不得填通过。
4. Color 继续按单独 CV-B01–B11 实施。新的共用能力放 shared／facade，避免 Color 直接依赖 Edit UI 内部控件或复制一套播放时钟。

**退出条件**：每个启用的动作都能从 UI 追踪到真实服务和成功／失败回填；无 handler 的动作保持禁用；本表每项有可复现证据后才关闭。

## I01–I07 实施记录（2026-11-09，复查修复批次）

全部 7 项已实施并按批提交；详细验收（含提交表、状态三栏口径、用户素材
证据与聚焦测试命令）见
[validation/viewport_followup_i01_i07_20261109.md](../validation/viewport_followup_i01_i07_20261109.md)。

| 任务 | Commit | 状态 |
|---|---|---|
| I01 全入口播放仲裁/真实反馈/页面生命周期 | `9ebd8ba6` | 域件+宿主接线完成；真机手感边界如实 |
| I02 Source 时间域/范围校验/换源状态 | `5e68d625` | 域件+宿主接线完成；含真实素材冒烟 |
| I03 capability/Match Frame 宿主与菜单 | `8177cf00` | 域件+宿主接线完成；F 键维持既有绑定 |
| I04 Source 拖出生产端/身份校验/事务（P1/P2） | `0a868c2a` | P1/P2 修复有复现用例；三意图×两模式×跨帧率×锁轨/新轨矩阵 |
| I05 工程关闭 Source 释放边界 | `53bf31ab` | 可复用边界全测；整机 Cmd+W 真机复核 |
| I06 双视图预算执行 | `8e64c4ab` | 执行器接真实质量档+结构核验+诊断；4K/HDR 吞吐真机边界 |
| I07 整页验收与记录闭环 | 见 validation 记录 | 组合宿主集成+用户素材冒烟+本记录 |

要点：

- 每个启用的动作均可从 UI 入口追踪到真实服务与成功/失败回填（入口门→
  仲裁器→真实控制器→状态信号→快照/状态栏/菜单回填）；无 handler 的
  动作（Proxy 切换/bypass/Source Tape/markers/timeline mark）保持禁用
  并带注册表原因。
- 复查复现的 P1（insert 丢流意图）与 P2（先建轨后失败留孤立轨+Undo）
  均已修复并有针对性用例。
- 全量回归（tests/edit + tests/app + tests/deliver，offscreen）最终
  数字与失败归属见
  [validation 记录 §4](../validation/viewport_followup_i01_i07_20261109.md)。
- 真机证据边界（不填通过）：跨页/全屏/浮动/单双/工程切换整机循环与
  音频听感、4K/HDR 双视图吞吐与 10 分钟内存、拖拽阈值手感、HDR 监看
  一致性；`tests/benchmark/drive_real_app_*` 可作后续专项驱动。
- Color 后端维持单独 CV-B01–B11 计划推进（本批未实施 Color 侧）。
