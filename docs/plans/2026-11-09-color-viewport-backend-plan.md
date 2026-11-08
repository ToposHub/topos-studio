# Color Viewport 后端接入与验收计划

日期：2026-11-09。状态：后端实施计划，供维护者后续完成；不表示下表能力已经实现。本轮只调整 Color Viewport 前端和保留旧交互，新能力未接通时保持禁用并说明真实原因。

## 1. 本轮前端约定与已有链路

界面参考本机 DaVinci Resolve Color 页，实施以实际观察为依据。前端约定为 30px header、20px ruler、36px transport，恢复项目原大橙色 Play/Pause 与首末／逐帧图标；保留 `Canvas.viewport_menu`、`Canvas.video_control_bar` 和原 controls 对象。Wipe、Mask、Guides、A/B、Workspace、静帧入口继续可达。

`VideoControlBar` 仍为 Canvas 的子控件、位于底部 overlay，不因重排重新挂载整个 bar。Color ruler 已在原 `_InOutSlider` 上增加纯 UI 绘制，不新建第二份播放头模型；旧 scrub 信号链不变。header 时间码通过本轮已新增并消费的 `timecode_changed(str)` 镜像原显示值；这不是时间域后端升级。菜单状态同步使用既有 `_canvas` owner，避免 reparent 后误用 `parent()`。共享矢量图标归 `src/shared/ui/viewport_icons.py`，Color 不依赖 Edit UI。外观与控件兼容验证见 [Color 前端记录](../validation/color_viewport_20261109/README.md)。

本计划中“拟新增”均为后续工作；既有接口继续复用，不把已存在功能列为从零开发。

| 真实路径／接口 | 当前能力 | 后续接入边界 |
|---|---|---|
| `src/features/color/ui/canvas.py`：`Canvas` | QGraphicsView＋GL viewport；UI overlay、图片平移／缩放、split 拖线、窗口／跟踪交互；存在 source GL 和 timeline preview pixmap 两种显示路径 | 外壳重排不改 GPU 管线，不让 paint 触发新 render，不混画旧 front texture |
| 同文件 `ViewportMenuBar` | View、Guides、timeline selector、Mask、Split/Wipe、A/B、Reference、Capture、Auto Color、Workspace | 保留 QAction、属性、信号与快捷键；新菜单须有 capability，再启用 |
| 同文件 `VideoControlBar` | 两个同步 slider、scrub、首末／逐帧／播放、Loop、TC 和 frame label；Color `show_for_video()`，Composite `show_for_video_with_io()` | Color 前端布局变化与 Composite Fusion ruler/I/O 模式隔离 |
| `src/app/controllers/dock_layout_controller.py` | `viewport_container` 按菜单＋Canvas 组装；`dockViewport` 是持久化身份 | 不改 dock 身份；浮动／页面切换仍经现有布局服务 |
| `src/app/controllers/file_controller.py`：`_connect_video_control_bar` | 清理旧连接后接 `on_video_seek/scrub_*/play_toggled/first/last/next/prev_frame/loop_toggled` | UI 不重复直连 controller；host 决定 source 或 timeline 路径 |
| 同文件 `_color_local_frame_to_source_frame`、`_color_source_frame_to_local_frame` | 时间线片段的 local/source 映射；clip trim／retime、effective source start 和音频桥已参与 | 新 TC／导航只能复用并明确这些域，不能将 slider 值直接当全局帧 |
| 同文件 `_activate_color_timeline_preview`、`_color_timeline_playback_controller` | 可使用 Edit 时钟和私有 TimelinePreviewRenderer；正常跨片段，Loop 时选中 clip 范围；具体激活由现有页面策略决定 | 不因 UI 重排强制切换显示路径／启用另一条 renderer |
| `src/features/color/core/image_engine.py`：`set_split_view_mode/set_compare_mode` | `split` 是单图与原图／参考 still 的 wipe，另有 `difference`；不是多图网格 Split Screen | 多源／多版本比较另立显示方案，保留原 wipe 语义和快捷键 |
| 同文件 `set_mask_preview_mode` | `show_mask_mode`＋`mask_preview_style=highlight/black/matte` | 这是 qualifier/matte 监看，不等于窗口线框开关，也不能直接冒充输入图像 Alpha 通道 |
| `src/app/controllers/color_controller.py`：`_on_matte_preview_requested` | Qualifier 可切 highlight／black／matte，回填面板 | 新 Highlight 控件优先适配已有能力，而非新增一套 matte renderer |
| `Canvas.set_overlay_visible/update_mask_overlay` | Power Window／mask 图形 overlay 可见性已有机制；选中窗口、tracker、brush 各有状态 | 复用显示开关，区分 overlay、最终 mask、tracker 交互和业务 enable |
| `src/app/controllers/layer_controller.py`：`on_capture_still` | 抓当前视频源帧后渲染、保存 .topos grade preset、thumb／全分辨率参考 sidecar、HDR EXR 与元数据，恢复播放；已有失败反馈 | 保留保存服务，补异步／generation／取消和“抓到哪一帧”的验收，不用截图替代 |
| `ImageEngine.capture_reference_still/load_reference_still/clear_reference_still/toggle_ab_view` | 当前图／Gallery 参考加载、wipe／A/B、清理；纯显示状态 | Grade Freeze 需先定义独立语义，不将 reference still 或 `freeze_graph` 当作已有冻结按钮 |
| `src/app/controllers/clip_version_host.py`、`src/features/edit/core/color_version_domains.py` | clip 版本读取／事务、共享／本地域、不可恢复诊断和 UI 回填已存在 | 比较版本读取快照，不通过反复激活版本污染工程 |
| `src/features/color/ui/color_status_bar.py` | SRC／WORK／OUT、provider fallback、decode path 等监看诊断 | 保留并避开 ruler／transport，不能为视觉精简删掉 HDR/fallback 信息 |

## 2. 后端任务总表

所有人日为一名熟悉项目的开发者估算，包含 focused test，不含等待设备和补素材；不是交付承诺。多图实时比较涉及新渲染成本，须先测量再修订。

| ID | 优先级 | 依赖 | 任务／范围 | 估算人日 |
|---|---|---|---|---:|
| CV-B01 | P0 | 前端接口稳定 | Color Viewer 状态／能力快照与模式路由 | 1–2 |
| CV-B02 | P0 | B01 | local/source/global 时间域、源 TC、DF/NDF、VFR | 2–3 |
| CV-B03 | P0 | B01、B02 | 保留播放／Loop／跨 clip／scrub 与音频政策 | 1–2 |
| CV-B04 | P1 | B01、B02 | Wipe 与多图／多版本 Split Screen 的后端隔离 | 3–5 |
| CV-B05 | P1 | B01 | Highlight／Matte／Alpha 与 Power Window overlay 能力 | 1.5–2.5 |
| CV-B06 | P1 | B01、B03 | Proxy／Resolution／Bypass／Unmix／Mute 真实接入 | 2–4 |
| CV-B07 | P1 | B01–B03 | Grade Freeze／Gallery still／画面抓取的会话一致性 | 2–3 |
| CV-B08 | P1 | B01、前端布局稳定 | Enhanced Viewer／浮动／全屏／用户偏好与作用域 | 1.5–2.5 |
| CV-B09 | P0 | 前端布局稳定 | Color/Fusion 共享控制栏、图片模式与诊断安全区 | 1–1.5 |
| CV-B10 | P0 | B03、B04、B07 | 关闭／取消／晚到结果／资源释放 | 1.5–2.5 |
| CV-B11 | P0 交付门槛 | 已启用的 B01–B10 | HDR／真实机性能／失败 smoke 与报告 | 2–3 |

总粗估 18.5–31 人日，可按下列批次逐步交付。B04 初版按受限数量的静态参考／版本快照估算，任意多视频同步播放、远程参考和多窗口硬件监看不包含在内；要支持须另立资源预算和工期。

## 3. 任务实施细则

### CV-B01：状态与 capability 的单一事实来源

**已有／新增**：复用 Canvas 状态、现有 PageContext、video_controller／timeline controller 和当前 clip 身份。新增轻量展示状态／capability 聚合适配，不创建另一个工程或播放时钟。

**输入 → 输出**：page/work mode、asset/clip/timeline ID、controller／renderer 能力、操作中状态 → header/footer/menu 的状态快照和 `{enabled, checked, reason}` 能力。拟新增状态须明确 `presentation_path`（source GL／timeline preview／image）、`time_domain`、`context_revision`；不要存 QWidget、GL 对象和 secret。

**步骤**：

1. 由 app host 聚合事实，UI 发请求、成功后回填；每次切 clip／换工程推进 context revision，旧回填不覆盖新状态。
2. 枚举 image、timeline clip source grading、timeline preview、Composite/Fusion、无素材模式；只向当前模式暴露确实可执行的能力。
3. 旧 View/Wipe/Mask/Guides/Workspace 行为继续调用原信号；不存在的 Unmix、多图等保持禁用并解释。
4. 快捷键、菜单、按钮汇入相同 action handler，程序更新使用 QSignalBlocker／状态锁，避免重复 toggle 和 undo 项。

**门槛**：切页／切片段不串状态；无素材控件诚实禁用；相同操作一次点击只发一次请求；菜单 checked 仅表示后端实际生效状态。

**失败路径**：控制器缺席、renderer 重启、上下文丢失时回填不可用原因，不使用默认 True 掩盖失败；对旧版本配置保留安全回退。

### CV-B02：时间域、源起始 TC 与精确时间码

**已有／新增**：clip-local slider 和 source 映射已有；共享 `src/shared/media/timecode.py` 支持 Rational、DF/NDF。当前 VideoControlBar `_update_timecode` 使用 float／round fps 的近似，header 镜像不会解决这个问题。

**输入 → 输出**：slider local frame、clip retime/trim/source_start、源 PTS／帧率／起始 TC、timeline frame/rate → 具名域的位置和准确显示／定位请求。

**步骤**：

1. 给显示值和 seek 请求标明域：`clip_local`、`source_frame`、`timeline_frame`、`source_pts`。header 如提供 Source TC／Timeline TC 选择，必须真正转换后显示，不能仅换 label。
2. 继续使用 `_color_local_frame_to_source_frame/_color_source_frame_to_local_frame` 和 clip 映射；处理反向、速度、trim、effective source start；不新写简单 `start + local` 替代现有逻辑。
3. 源 TC 读取 metadata；缺失时明确相对 TC，不伪造片头绝对码。用共享 Timecode/Rational 处理 30000/1001 和 60000/1001 DF/NDF，非法 DF 字符串拒绝且保持原播放头。
4. VFR 借用可复用的媒体时间索引协议；`SourceFrameTimeIndex` 当前在 `src/features/edit/core/scene_cut/models.py`，需评估共享适配，不要求预先执行 Scene Cut，也不在 UI 同步扫描全片。
5. 保留总帧边界和单帧素材规则；镜像 `timecode_changed` 只通知最终格式化结果，不反向 seek；播放头回填不制造第二次 seek。

**门槛**：23.976/25/29.97DF/59.94DF、非零 TC、VFR、反向／变速 clip 的 local/source/global 映射有 exact assertions；拖拽时 header 同步；输入无效码不移动播放头；跨 clip 后显示的域和片段一致。

**失败路径**：索引取消／过期、metadata 无 fps、媒体换版、超界帧均显式诊断。TC 显示格式升级不改变工程中的帧域和现有编辑／导出语义。

### CV-B03：播放、Loop、跨 clip 与音频

**已有／新增**：`_connect_video_control_bar`、`on_video_*` 已有；Color 的普通播放跨 clip，显式 Loop 才循环当前选中 clip，Composite 另有默认 ON 的 range loop。保留这些政策，新增的是新菜单的适配和验证。

**输入 → 输出**：Play/Pause／首末逐帧／Loop／scrub 请求和当前路径 → 原 host 命令及实际状态回填。

**步骤**：

1. 不直接将 Color 首末按钮绑定 Edit 的 timeline 0／duration；继续经 `on_video_first_frame/on_video_last_frame`，保持 clip-local 端点。
2. Loop OFF 使用真实内容范围／显式 timeline in-out，排除 TimelinePanel 的空尾；Loop ON 使用选中 clip 范围；切 clip 按现有规则复位 Loop，不能从 Composite 共享默认值。
3. scrub 保留 `started→moved→finished→seek` 协议、松手非阻塞精确帧与 generation 保护；不在绘制 ruler 时触发 seek。
4. source GL 分支与 timeline preview 分支各沿现有路由；复用 Color timeline audio bridge、scrub 音频和 reanchor，不另启动第二路声音。
5. 如要加 Reverse／Stop，先声明 capability：Timeline controller 已有 reverse/JKL，可按实际模式适配；源 video_controller 是否支持及声音政策需核实。停止原位与归零语义分开，不把 pause 按钮改成归零 stop。

**门槛**：跨两个同源连续 clip 及不同源 clip 无重复 toggle／旧片闪帧；首末仍是当前显示 clip；Loop 不播放空尾；slider/display clip/音频同域；拖后精确帧及用户播放状态正确。

**失败路径**：某 clip 离线、播放期间换上下文、scrub 中离页时取消旧请求／停止音频，不把旧片源结果呈现到新 clip。

### CV-B04：Wipe 和 Split Screen 多图／版本比较

**已有／新增**：`ImageEngine.set_split_view_mode` 的 split 是 wipe；`set_compare_mode` 支持 split/difference；`load_reference_still/toggle_ab_view` 已存在。真正多图／多版本网格比较是新增能力，不能给原 split 改名就算完成。

**输入 → 输出**：参考项 ID（Gallery／clip version／original）、比较布局／数量、目标帧和色彩上下文 → 独立比较会话、只读渲染快照及可释放的呈现结果。

**步骤**：

1. 保持 Wipe API／快捷键／原图回退和 A/B 端点行为；新增比较会话与 wipe 分离，切换时可恢复原参考和分割位置。
2. 首版限制参考数量，支持静态 still／同帧版本比较；不同帧／不同尺寸素材的 fit、同步／锁帧规则必须明确，不能混用当前 clip 的 source frame。
3. 版本列表通过 `clip_version_host.py`／`color_version_domains.py` 读取权威域；用已有快照编译／渲染能力评估只读版本，不通过反复 Load Version 改 live grade、undo 和 revision。
4. 固定输入／工作色彩空间和显示输出，参考项记录 provenance；缩略图 JPEG 只用于列表，不冒充 HDR 比较 master。Gallery 可优先读取既有 EXR／高精参考 sidecar。
5. 后台渲染分配 bounded cache／队列，缓存键包含 project/asset/version/revision/source frame/size/color context；取消或上下文切换释放所有在途／完成纹理。

**门槛**：开启比较不改变工程、active grade 或 undo 栈；原 wipe/difference/A-B 仍通过旧测试；删除版本／切工程不会显示旧图；多个尺寸保持明确 fit；HDR 参考值和 ODT 只处理一次。

**失败路径**：版本 plugin 不可恢复、EXR 读取失败、参考已删除、显存不足逐项标明 unavailable，不能用当前画面冒充失败的参考项。多视频同步播放另立计划，不隐含支持。

### CV-B05：Highlight、Matte、Alpha 与 Power Window overlay

**已有／新增**：现有 `set_mask_preview_mode(enabled, style)` 与 `_on_matte_preview_requested` 已支持 highlight/black/matte。`set_overlay_visible` 控制窗口图形；二者是独立状态。

**输入 → 输出**：当前选择节点／层／窗口、preview style、overlay policy、是否有 input alpha → 有能力的预览请求与菜单状态。

**步骤**：

1. 保留旧 Mask 控件可达及其引擎事实来源 `show_mask_mode`；若新菜单称 Highlight，明确其是当前 qualifier/matte 的 highlight，不默认为输入 Alpha。
2. 若提供 Alpha Channel，检查输入／合成输出 alpha 能力；没有 alpha 时禁用并说明，不能把 qualifier matte 灰度图当输入 alpha。
3. Power Window overlay 复用 `Canvas.set_overlay_visible/update_mask_overlay`，设计 Off／Selected／All 时按真实支持逐项启用；隐藏图形不修改窗口 enabled/mask 结果、跟踪数据或项目参数。
4. 区分 tracker pattern/search/current overlay、brush cursor、Guide、Power Window 控件；不要一个通用 hideChildren 把所有交互隐藏或损坏。
5. 双向同步 qualifier、viewport menu、快捷键时使用状态锁；选择节点变化后刷新能力和有效 matte，不沿用前一节点状态。

**门槛**：关闭窗口线框不改变最终画面 mask；Highlight 切换与 qualifier 面板一致；程序回填不触发第二次 render／业务提交；不存在 alpha 不显示伪灰度；tracker 交互保留。

**失败路径**：没有 active layer／节点不产 matte、选择已删除窗口、renderer 不可用时拒绝并保留原状态；不以 broad except 吞掉错误后显示 checked。

### CV-B06：Proxy、Resolution、Bypass、Unmix 和 Mute

**已有／新增**：Program／Color 已有预览管线、master/proxy 决策和图层／效果栈；`file_controller.load_video` 已按管线决策启用 dual path。新 menu 不能直接修改 UI 或跳过 pipeline。

| 功能 | 真实接入范围／步骤 | 验收与失败路径 |
|---|---|---|
| Proxy Handling | 通过已有 source resolver／video pipeline 决策请求代理策略；将预览代理与静态精确 master 分开，保留 Color settle 的 `allow_proxy_fallback=False` 政策 | 没代理／代理过期时明确 reason；源色彩解释、TC 和播放头不变；UI checked 回填真实策略 |
| Preview Resolution | 在 renderer／decoder 的预览目标尺寸政策层适配，不改 timeline 格式和原图像数据；高质量暂停帧仍可精确刷新 | 4K缩放与 zoom 百分比独立；预览降质不改导出；内存不足可解释降级 |
| Bypass Grade/Effects | 临时监看 bypass 进入快照／render plan／缓存键；若是工程业务 bypass，走既有命令并保留 undo/revision，菜单明确区分 | 不删除节点／layers，不静默丢 plugin；导出继续使用工程权威状态；恢复后缓存与状态一致 |
| Unmix | 新功能先定义为 Color 监看临时隔离当前 clip 的 transition／叠加贡献，检查 source GL 与 timeline composited 路径是否具备真实入口 | 当前仅显示 source 时不得声称可“关闭时间线转场”；不能通过删除 transition/clip 实现；无能力保持禁用 |
| Mute | 对接 `file_controller.py` 的 Color timeline 音频桥和现有 audio manager 输出政策，采用本页监看静音且保留先前状态；不得改片段 volume 代替 mute | 静音后设备实际无声，继续推进视频；恢复不把用户本来静音的轨道打开；跨页按约定恢复 |

**输入 → 输出**：当前 presentation path／pipeline capability、用户动作 → 服务应用结果＋实际菜单状态、可诊断回退。

**门槛**：每个 enabled 动作有真实 service 请求与成功／拒绝回填测试；快速重复 toggle 不叠加、无效上下文不更改模型；export/preview 语义和 HDR 路径一致。

**风险／失败路径**：代理文件被删、硬件 decoder 重建失败、插件不支持、音频设备切换须显式反馈；保持播放头与原策略可恢复，不默认假成功。

### CV-B07：Grade Freeze、stills 与抓帧

**已有／新增**：`on_capture_still` 已保存 grade `.topos`、缩略图／高精参考／HDR EXR 和来源 metadata；`grab_viewport_image()` 兼容转 `render_cached_image`；reference still 是显示层。`freeze_graph` 是不可变渲染快照机制，不等于用户 Grade Freeze 功能。

**输入 → 输出**：capture/freeze 请求、当前 project/clip/source frame、grade revision、color context → 有 provenance 的静帧／冻结会话或可解释失败。

**步骤**：

1. 沿用原 Gallery／SaveRouter／preset_meta，不新建另一套 still 库和目录结构；新入口统一发原 capture signal，避免截取 header/footer UI 当画面抓取。
2. 明确 Freeze 含义：若冻结显示帧，继续保留后台工程更新但 viewer 锁定 frame+grade；若冻结 grade 后继续播放，则需独立 immutable grade snapshot。两种模式不混用一个 checked 状态，未实现者禁用。
3. 抓帧使用请求时的 context/frame/revision，异步等待完整结果后保存；不要保存显示缓存的上一 clip。原 capture 可能暂停／恢复 source video，需核对 timeline 分支和用户之后手动 pause 时的恢复权限。
4. 保留 HDR float EXR sidecar、grade metadata 和失败反馈；JPEG 降级需可见，不能声称高精参考已保存。新增异步 readback 沿既有渲染线程队列，不在 UI 直接操作 GL。
5. 冻结／参考内存设上限，离页／清参考／关闭释放；保存业务仍经原工程服务，未知 payload 保留。

**门槛**：在播放、切 clip、改变 grade、取消捕获四种情况下，输出 frame／grade／metadata 一致；原 Gallery apply／Wipe 可用；失败不留下半份 preset；用户暂停后迟到 capture 不擅自恢复播放。

**失败路径**：无媒体、无法 render master、EXR writer 不可用、保存失败、context 变化、GPU readback 取消均有明确结果；保留原 still 不覆盖成空图。

### CV-B08：Enhanced Viewer、浮动与全屏作用域

**已有／新增**：`dockViewport` 可浮动、已有 focus mode；`ViewportMenuBar._toggle_fullscreen()` 当前控制 `self.window()`，浮动时它可能是 dock window，不能假定总是主窗。Enhanced Viewer／独立 viewer-only 全屏如需新作用域，应由宿主协调。

**输入 → 输出**：增强视图／focus／fullscreen／floating 请求、原宿主／屏幕／布局 → 明确作用域的显示模式以及可恢复的原布局。

**步骤**：

1. 为 Main Window Fullscreen、Viewer Focus、Viewer-only Fullscreen、Floating Dock 定义不同 action 和 capability；当前 F11 作用域在完成迁移前保持原行为，不能仅改名。
2. 使用现有 `viewport_container`、dock identity、layout store，保持同一 Canvas 和 GL viewport 对象；避免新建 renderer 导致双重上下文／输出所有权。
3. 进入增强视图暂存被隐藏面板／header/footer 状态，退出只恢复自己改变的内容；Esc、切页、窗口关闭、浮动 dock 归位、屏幕移除均考虑。
4. 用户配置可保存 viewer 外观偏好及合法屏幕位置，不保存 GL texture、播放中状态、临时冻结像素；恢复时校验屏幕范围和版本，不清空其他页面布局。

**门槛**：docked/floating 各做 enter→Esc→切页往返，Canvas 对象和连接不变；显示于可用屏幕；不把原 viewer 塞回错误 dock；主窗标题栏／菜单恢复；图片缩放与视频播放位置保留。

**失败路径**：宿主销毁／屏幕消失／restoreState失败时回到存活主窗的有效布局并诊断；晚到 fullscreen 回调不可访问已删除 QWidget。

### CV-B09：共享 VideoControlBar、图片模式和安全区

**已有／新增**：同一类被 Color 和 Composite 复用，实例各自独立。Color `show_for_video` 隐藏 I/O；Composite `show_for_video_with_io` 显示 Fusion ruler/I/O。Canvas image Space 是平移，video Space 按 bar 可见性播放。本任务以回归验证和最小适配为主。

**输入 → 输出**：页面／work mode、bar 模式、widget尺寸、诊断状态 → 正确可见性与不会覆盖画面工具的布局。

**步骤**：

1. Color header/ruler/transport 的布局在显式 Color 分支；不得重设 Composite ruler height、I/O、keyframe markers 和默认 Loop。
2. 保留 `label_time is timecode_label`、slider/ruler_slider、原控件 setter／signal；同步值时维持 blockSignals，不能产生播放头回声。
3. `hide_for_image` 保留禁用／reset／清 range 语义；图片模式 Space 继续平移。若空 footer 常驻，需要独立显示外壳，不用假 visible 的 video bar 破坏键盘路由。
4. Color status bar 按实际 visible footer/ruler 高度定位，留在可读的画面安全区；狭窄时折叠文字到 tooltip，保留 provider/HDR诊断，不覆盖 scrub thumb／transport 或窗口手柄。
5. overlay bar 仍为 Canvas child；FPS owner、`update_video_control_bar_position`、resize/show 路径和 timeline selector owner 正确，重排不改变 host lookup。

**门槛**：Color／Composite／image 连续切换，ruler模式／高度／I/O／Loop不串；无视频 Space可平移；provider警示和ruler可同时点击／阅读；窄窗与HiDPI不重叠；header TC镜像不发seek。

**失败路径**：宿主未show、尺寸极小、配置缺失、窗口处于浮动状态都不能出现负几何／永远隐藏控件；不因布局失败触发新解码。

### CV-B10：关闭、取消和资源生命周期

**已有／新增**：Color renderer／视频 worker、reference纹理命令和页面离开清理已有入口。新比较／冻结／capture 会话必须加入原生命周期，不能依赖 Python GC 碰运气。

**输入 → 输出**：离页／关窗／换工程／取消capture/compare／GPU错误 → 停止新请求、撤销会话、回收在途结果和资源或安全fallback。

**步骤**：先使 context token 失效并拒绝新请求；暂停相关音频／timer；断开旧回调；取消可取消worker任务；按创建线程和既有deferred-release模式回收texture/buffer/FBO；最后销毁widget。

所有完成回调验证 context、asset、frame、grade revision 与关闭token；抛弃的结果同样释放。reference still／冻结缓存／比较grid设内存上限。GPU timeout 和 CPU fallback 不能在关窗后自动发新渲染；退出等待有边界，不无限 join。

**门槛**：解码／抓帧／多图比较中关闭、切工程、切Color/Fusion，无崩溃、残音、僵尸worker和持续增长缓存；失败恢复新素材可继续显示；原图／参考清理不会释放仍被present使用的纹理。

**失败路径**：context已销毁、旧owner资源、decoder无响应、两条显示路径晚到结果必须有明确诊断；不从UI或脚本线程调用GL释放，不宽泛吞错后维持假playing。

### CV-B11：HDR、性能和真实机 smoke

**交付物**：`docs/validation/` 下实测报告，含commit、平台/GPU、模式、素材、完整命令、pass/skip/fail、性能基线和限制。未具备GPU／外屏的测试明确skip，不能统计为通过。

| 验证组 | 现有真实测试路径 | 新增门槛 |
|---|---|---|
| bar/UI隔离 | `tests/color/test_video_control_bar_scrub.py`、`tests/color/test_fix_wd4g23_aud.py` | Color30/20/36与Fusion分支、对象身份、scrub顺序、header镜像、imageSpace |
| Mask/Highlight | `tests/color/test_fix_wd3g22_aud.py` | owner reparent后sync、no recursion、matte/alpha/overlay分离 |
| Wipe/Gallery/版本 | `tests/color/test_ab_reference_wipe.py`、`tests/app/test_clip_version_host.py`、`tests/app/test_clip_version_project_sync.py` | Wipe不变、多版本只读比较不改工程、capture frame provenance |
| 播放域 | `tests/app/test_color_timeline_clip_playback_mapping.py`、`tests/app/test_color_page_audio_bridge.py`、`tests/app/test_color_entry_playback_and_timeline_refresh.py` | 不同fps/trim/retime/反向、Loop、跨clip、原位停止、声音静音政策 |
| 呈现边界 | `tests/color/test_canvas_clip_switch_preview.py`、`tests/composite/test_composite_viewport_safe_area.py` | 旧纹理不闪、浮动/全屏父级、status安全区、晚到结果 |
| 时间/HDR | `tests/media/test_timecode.py`、`tests/media/test_f54_vfr_alignment.py`、`tests/render/test_hdr_gpu_contract.py` | DF/源TC/VFR，PQ/HLG/SDR参考比较、无重复ODT、无不必要0–1 clamp |
| 诊断 | `tests/color/test_color_monitor_status_bar.py`、`tests/color/test_quality_control.py` | provider fallback可见，缺alpha/Unmix/proxy能力诚实禁用 |

真实机素材至少覆盖4K60、长GOP、VFR、23.976/29.97DF、10-bit PQ/HLG、透明图、普通图片、只音频、离线素材；4K60为测量目标，不能无条件承诺全部硬件达标。

建议同机记录单路旧UI基线，对比新UI的fps、seek p95、首次出帧、UI响应、GPU/CPU缓存峰值和音画偏移。仅前端重排不应显著影响呈现率；建议静态参考比较关闭时fps相对基线下降<5%、UI输入响应p95<100ms，达不到写明瓶颈。多图模式设单独数量／分辨率预算，不以后台无界渲染获得表面响应。

至少执行50次clip/模式切换、30次Color↔Composite/浮动/全屏往返、10分钟播放／比较、解码中关窗／取消／换工程。通过标准为无可复现旧帧串源、残音、悬空回调、未释放资源增长、项目状态污染和HDR解释变化。

Focused tests 示例（不是已执行结果）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/color/test_video_control_bar_scrub.py tests/color/test_ab_reference_wipe.py tests/color/test_color_monitor_status_bar.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/app/test_color_timeline_clip_playback_mapping.py tests/app/test_color_page_audio_bridge.py tests/app/test_color_entry_playback_and_timeline_refresh.py -q
.venv/bin/python -m pytest tests/media/test_timecode.py tests/media/test_f54_vfr_alignment.py tests/render/test_hdr_gpu_contract.py -q
```

新增focusedtest按实际提交路径补入；GPU／真实画质另在可用设备验证。测试不应为了避开native crash而把原回调、显示路径或失败检查全部mock掉。

## 4. 建议实施批次和完成定义

1. **安全接入批次**：B01、B09，随后B02/B03/B10的现有能力回归；保留新菜单禁用。先确保新外壳不改变时间域、Loop、图片平移和Fusion控件。
2. **现有能力扩展批次**：B05、B06、B07，逐项启用有真实后端和失败回填的能力。Unmix、Alpha和Freeze不得借现有相似功能名称假装支持。
3. **比较／增强批次**：B04、B08；先受限静态参考／版本，后独立评估多视频。每批执行B11对应测试，并复审取消／资源路径。
4. **最终报告**：列清已实现、仍禁用、硬件未测、性能受限项；完成的定义是“已启用功能通过正确性和失败门槛”，不是所有Resolve图标都可点击。

遵循`.clinerules`：新函数完整类型注解、外部数据验证、信号回填锁、工程变更走command/service、GPU任务走既有线程队列、临时预览不污染导出语义。修改核心渲染前先分析调用方和数据流，实施后审查资源、取消及兼容路径。
