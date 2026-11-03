# Effects / LUT 节点后端接线与节点遗留任务实施计划

更新：2026-11-03。本轮交付范围：**UI 与交互**。下列 R / N 任务均是待实施项，不能因面板能操作而打勾。项目中前几轮的后端修复予以保留，本轮未新增效果执行器、工程命令或 LUT 渲染实现。

本文件是后续统一施工入口。原设计见 [节点原计划](color_nodes_backend_implementation_plan.md)，实施事实见 [旧实施台账](validation/color_nodes/implementation_ledger.md)、[前后端复查](validation/color_nodes/integration_2026-11-03/README.md)、[Resolve 对照矩阵](validation/color_nodes/resolve_parity_2026-11-03/README.md)、[发布矩阵](validation/color_nodes/release_acceptance.md)。较新的实测修复覆盖旧台账中同一问题的历史状态；没有证据的项目继续保留待完成，不能把 B00–B16 全部推倒重做，也不能将它们视作全部通过。

## 1. 本轮已经存在的 UI 与明确边界

| 文件 | 已实现 UI / 接口 | 还没有做的事 |
|---|---|---|
| `src/features/color/ui/effects_browser_panel.py` | `ColorEffectsBrowser`，库/设置、搜索、分类折叠、收藏、列表/图块、拖动、双击/右键/按钮添加、按实例启用/排序/重置/删除；按 schema 生成 slider/bool/enum/color 控件 | 不编译插件、不提交参数、不改变画面 |
| `src/features/color/ui/lut_browser_panel.py` | `ColorLutBrowser`，目录树、搜索、收藏、列表/缩略图、缩放、显示/隐藏目录、导入选择、刷新、双击/右键应用、当前 LUT/强度/重置/移除 | 不解析/安装 LUT，不生成当前帧 LUT 预览 |
| `src/features/color/ui/node_resource_mime.py` | 版本化拖动数据；仅含 `version=1, kind=effect\|lut, resource_id`，拒绝坏 JSON/错误类型/超长数据 | 不以拖动文本或文件路径直接运行资源 |
| `src/features/color/ui/node_editor_view.py` | `resource_drop_requested(node_id, token)`；可编辑 Corrector 拖入高亮、离开清除、落点命中；Source/Output/Mixer/锁定节点拒绝 | 不调用图编辑 `apply_edit` 提交资源；Compound 外壳不接受，进入内部后可投放 |
| `src/app/controllers/color_resource_preview.py` | `ColorResourcePreview`；只读现有插件注册元数据与旧 LUT 列表，按节点保留 UI 草稿；`resource_intent` 统一导出交互意图 | **不是后端适配器**；不保存草稿、不发渲染、不写 engine/Layer/project |
| `src/app/controllers/dock_layout_controller.py` | Gallery/LUT/Effects 默认左侧标签组，Nodes 独立；旧布局一次迁移；后续保留自定义/浮动安排 | 不强制禁止用户自行合并 Nodes；合并后仍可双击添加至选中节点 |
| `src/app/main_window.py` | 新浏览器放进原 `dockEffects` / `dockLut`；旧 `plugin_panel` / `lut_panel` 隐藏保留，供旧控制器读写兼容 | 隐藏旧控件不是完成迁移；旧菜单/脚本/快捷键写入入口需 R10 全部审计 |

面板始终注明“交互预览 · 尚未应用到画面”。当前草稿添加不应点亮真实节点的 OFX/LUT 已应用标记。LUT 未提供后端缩略图时显示“暂无预览”，不能复制源图冒充已套 LUT 的结果。参考本机 Resolve 的 Library/Settings、分类条目和节点操作结构，颜色、滑条、开关使用项目全局样式；浮动布局是 Topos 自有能力，不照搬 Resolve 固定停靠限制。

### 1.1 现有 UI 数据契约

```python
# Effects catalog entry
{"id": "stable-effect-id", "kind": "effect", "name": "…", "category": "…",
 "description": "…", "available": True,
 "parameters": [{"var_name": "amount", "label": "Amount", "type": "slider",
                 "default": 0.2, "min": 0.0, "max": 1.0}]}
# Effect instance projection
{"instance_id": "persistent-instance-id", "resource_id": "stable-effect-id",
 "name": "…", "enabled": True, "params": {"amount": 0.2}, "parameters": [...]}
# LUT catalog entry: thumbnail is optional; only GUI-thread QIcon/QPixmap
{"id": "stable-lut-id", "kind": "lut", "name": "…", "category": "Film/Print",
 "description": "…", "thumbnail": optional_gui_image}
```

- Effects 调用：`set_resources(entries)`、`set_target(node_id,label,editable,reason)`、`set_instances(instances)`。
- LUT 调用：`set_resources(entries)`、`set_target(...)`、`set_current_lut(resource_id,strength)`。
- `resource_activated(entry)`：双击/右键/按钮只表示用户意图；目标是点击当时选中的正确节点。
- Effects `draft_edit_requested(action,payload)`：`set_parameter {instance_id,name,value,phase:change|commit}`、`set_enabled {instance_id,enabled}`、`reset_instance`、`remove_instance`、`move_instance {instance_id,index}`。
- LUT 同名信号：`set_lut_strength {strength}`、`reset_lut_strength {strength:1.0}`、`remove_lut {}`；`import_requested(paths)`、`refresh_requested()`另行处理。
- 现 `resource_intent` 载荷：`{ui_preview_only:True, target:tuple, action, payload}`。tuple 顺序是 `(session_id,generation,scope_owner_id,stack_id,scope,compound_path,node_id)`。它只是 UI 草稿诊断接口，**不得直接当项目协议持久化**。
- 正式后端应在意图接收瞬间建立 typed target / expected revision / intent_id；不在异步完成后重新读取 active node。正式 adapter 的文件与类型是待新建，下面“拟新增”均非现有 API。

## 2. 实施顺序与每次提交的完成条件

依赖顺序：`R00 → R01/R02 → R03 → R04/R05 → R06 → R07/R08 → R09 → R10 → R11/R12`。N 任务按照各自前置条件插入。R01/R02 可分别施工，但首次真实写入必须等 R03 的目标、事务和拒绝协议完成。

每步提交附：变更文件、测试命令、实际结果、尚未支持项、可回退点；待办只在代码、自动验证和必要的原生验证全部通过后勾选。新增测试文件先创建再运行，不把计划里的文件名描述成已经存在。

### R00 — 固定基线与写入口清单　[ ]

**修改/检查**：主窗口、`color_controller.py`、旧 PluginPanel/LutPanel、`color_graph_controller.py`、`color_graph_service.py`、EffectCatalog 和项目 IO。

1. 记录 git SHA、工作区差异、平台/GPU/开关；先保存现有测试结果，不覆盖前轮改动。
2. `rg -n 'add_plugin|remove_plugin|plugins|apply_lut_from_path|active_lut_path|lut_intensity' src/app src/features/color src/features/edit`，逐项登记触发入口、目标来源、是否写 Layer/graph、是否入历史、是否保存/导出。
3. 用同一素材、2 Corrector、1 分支、1 Compound、2 clip 建立基线工程；已有插件/LUT/未知参数都留样。
4. 运行本文件 §7 的 UI 与图回归。保存图 JSON、实例顺序、截图及已知错误，不先更换 golden。

**验证/出口**：每个写点归为“正式新命令/旧兼容/废弃待删”，待审计数为 0；纯 UI 会话不改项目/engine 的断言仍通过。失败时回退本步新增审计代码，不改用户工程。

### R01 — 统一 Effects 资源目录与能力状态　[x]（2026-11-03，证据 docs/validation/color_resources/r01_catalog/；EffectsResourceCatalog 只读投影 + 后台预热，GUI 冷路径 7ms）

**位置**：`src/features/shared/effects/effect_catalog.py`、`effect_spec.py`、`plugin_identity.py`、`src/features/color/core/plugin_manager.py`；拟新增 app catalog presenter。

1. 以现有 EffectCatalog/Registry 为权威，向 UI 投影稳定 ID、版本、schema、分类、作者、能力与缺失原因；把当前 `ColorResourcePreview._existing_catalog()` 直接读 PluginManager 的临时展示路径替换为此投影。
2. 编译/扫描/模型探测在既有后台队列完成，UI 仅接不可变 metadata；不得每次搜索扫描文件、编译 shader 或加载 AI 模型。
3. 复用 plugin identity 的 content/schema hash；同名不同内容必须区分。分类/名称翻译只改变显示，不能改变资源 ID。
4. 不支持当前 backend、模型未安装、版本不匹配的条目提供明确 disabled reason；支持离线 bake 的条目标明能力，不假装实时。

**验证**：拟新增 `tests/app/test_color_resource_catalog.py`：重复名字、Unicode、热刷新、删除/更新插件、未知 schema、缺 AI pack；断言选择/收藏身份不漂移、search 不触发扫描、缺失条目不能发真实添加命令。冷/热目录加载分别计时。

### R02 — LUT 资源注册、导入、迁移与重定位　[x]（2026-11-03，证据 docs/validation/color_resources/r02_lut_registry/；19 项注册矩阵 + 包往返/重定位全过）

**位置**：`src/shared/color/lut_loader.py`、`src/features/edit/core/edit_project_io.py`、`src/shared/project/color_stack.py`；复用项目现有资源容器，拟新增 LUT catalog adapter。

1. 后台验证 `.cube/.3dl/.look` 实际支持程度、维度、数据长度、DOMAIN、非有限值、颜色空间说明；未支持格式应给错误，不因文件对话框允许选择就宣称支持。
2. 采用内容 hash + 资源 ID 注册；同内容多路径去重、同名不同内容保留。UI 当前本机绝对路径 hash 只是会话标识，正式 ID 不得依赖机器路径。
3. 工程只保存 resource reference / content hash /格式元数据；导入写项目资源目录应使用现有原子写入与失败清理机制；复制中断、磁盘满时不产生可应用条目。
4. 旧 `active_lut_path` 迁为相对资源引用；旧载荷和未知字段保留；找不到文件提供“缺失/重定位”，不得静默移除。
5. UI 导入开始/成功/失败/取消反馈通过 presenter 回传。完成后重投影目录树；当前 `_lut_entry/import_preview` 退出生产写路径。

**验证**：扩展 `tests/color/test_lut_loader.py`，拟新增资源导入/迁移测试；2³ identity、非 identity、1D、域不为0–1、损坏数据、非 ASCII 名、重名、读失败、取消；移动整个工程后重开、同工程移至另一台机器，引用和结果一致。失败不发布半资源。

### R03 — typed target、命令与 ack/reject 主干　[x]（2026-11-03，证据 docs/validation/color_resources/r03_controller/；9 资源 op + 契约 + 互斥装配，15 项宿主矩阵）

**位置**：`src/app/controllers/color_graph_controller.py`、`src/features/color/core/color_graph_service.py`、`src/shared/project/color_graph.py`，沿用 `ApplyColorGraphCommand` 与现有项目 command 栈；拟新增 `color_resource_controller.py`。

1. 建立资源操作枚举和参数 schema；登记新增资源 action，不复用无语义的 `set_grade` 任意字典。
2. 定义目标 `(session_id,generation,scope,scope_owner_id,stack_id,compound_path,node_id)`，加 `intent_id/expected_graph_revision/expected_project_revision`。
3. 添加、删除、排序、启用、参数、LUT 替换/强度分别调用 service 原子操作；验证目标 Corrector、锁、资源存在、参数类型/范围、实例归属。
4. 用 command 前后不可变快照进入现有 undo/redo；reject 不入历史、不递增 revision、不留下 UI 成功状态。UI 等 ack 后用权威 projection 水合。
5. 替换 `ColorResourcePreview` 的生产装配；保留显式开发 UI preview 模式，禁止同一信号同时写草稿和正式 service。
6. 双击目标在激活瞬间捕获；拖动目标用 drop 传入 ID。多选时明确只操作 active/落点节点；批量操作未来另设命令，不能悄悄全部应用。

**验证**：拟新增 service/command/resource adapter 测试：锁定、Source/Mixer、无目标、删除节点后晚到结果、跨 clip/scope/Compound、revision 冲突、重复 intent；拒绝图字节不变，成功一次操作一次 undo。使用真实项目栈与宿主测试，不只 fake presenter。

### R04 — Effects 实例与参数编辑事务　[x]（2026-11-03，证据 docs/validation/color_resources/r04_gestures/；100 拖动=1 undo、取消=0、离散=1，异步守卫就绪）

1. 实例永久 ID 与资源 ID 分开；同资源重复添加允许多个实例。默认值由版本化 schema 构造，不复制其他节点当前值。
2. 添加/删除/重置/启用/重排更新正确节点的插件域与完整 legacy projection；不直接修改活动 Layer 列表作为权威。
3. 增加 `phase:start/change/commit/cancel` 或显式 gesture ID；当前 UI slider 只有 change/commit，接真实后端时须补开始/取消，鼠标抬起/失焦/切 clip 有确定结束策略。
4. 一次连续拖动一个 undo，期间 transient 预览保持图/当前节点一致；下拉、bool、确认颜色分别一次 command；取消拾色不入史。
5. 旧项目未知插件/未知参数原样保留，控件只编辑已知字段；重置只重置该实例，删除只删指定实例；重排保持其他身份。
6. AI 异步任务绑定目标和 source revision，取消/撤销/节点删除后丢弃晚到结果；bake 资源生命周期随 command/工程管理，不能原地覆盖源素材。

**验证**：拟新增 effect instance roundtrip/gesture tests；重复实例、100 次 valueChange=1 undo、取消=0 undo、删后撤销恢复同 ID/顺序；两 clip 相同 node_id 不串参；失效 AI 输出不能写新目标。执行前后保存/重开一致。

### R05 — 节点 LUT 语义、替换与强度　[x]（2026-11-03，证据 docs/validation/color_resources/r05_lut_semantics/；顺序规格 v1 + 三发射器强度一致 + HDR 恒等缺陷修正 + golden 重归档）

1. 定义 Corrector LUT 处理顺序，与 primary/curves/plugins/key 的顺序形成版本化规格；使用非交换操作测试顺序，不以 identity 通过代替顺序正确。
2. 节点持有 LUT resource reference + intensity；再次应用替换该节点现 LUT，不新增隐形全局 LUT。不作用于其他选中节点或其他 scope。
3. 强度 0/1/中间值使用指定处理空间混合；插值、DOMAIN 外值与 HDR >1/负值策略明确，不能为省事全局 clamp。
4. reset strength 恢复1，remove 只移除 LUT，保留其他调色；节点右键 LUT 菜单与浏览器共用命令/R02 导入入口。
5. ack 后 `set_current_lut` 回显与真实工具标记同步，reject 恢复权威旧值。

**验证**：2³ identity、通道交换、非线性/非单位 DOMAIN LUT；强度0=未套LUT、1=完整、0.42按规格；重复替换/移除/撤销/重做、嵌套与共享实例；图 JSON、Viewer 与保存重开一致。

### R06 — GL / CPU / Metal / bake 能力与输出一致　[x]（2026-11-04，证据 docs/validation/color_resources/r06_capability/；资源指纹进缓存键 + 预检 + bake 协议，执行器主体为前轮 B16.5）

**位置**：`corrector_render_service.py`、`color_graph_snapshot.py`、`color_graph_compiler.py`、`color_graph_executor.py`、`color_graph_cpu.py`、`color_graph_metal.py`、shared EffectCatalog/pass runner。

1. 冻结 snapshot 包括实例顺序、启用、参数、resource identity；缓存键覆盖资源内容/schema/参数/分辨率/输入 revision。
2. GL 使用既有渲染线程/任务队列创建编译与 LUT texture；绑定资源命名隔离，顺序执行、取消失败无半帧发布。
3. 旁路仍保留配置；删除/更新/换图释放纹理与缓存按 deferred release/lease，不能在 UI 线程 gl delete。
4. 按 effect+参数+图结构预检完整能力；CPU/Metal 未实现的插件/HSL/LUT/空间效果继续明确拒绝或选择完整 fallback，禁止跳过某一实例。
5. 离线 AI/bake 建立显式异步结果节点/引用协议，source/graph/version 变更取消；同一规格供预览和全分辨率输出使用。

**验证**：拓展现有 GL execution/cancel/capability/CPU parity 测试；2个非交换FX、LUT+FX、重复插件、bypass、missing shader、取消、resize、旧 revision、资源更新；有能力的每后端对照固定输入与误差预算，无能力的必须 fail-visible。

### R07 — LUT 缩略图与悬停预览后台任务　[x]（2026-11-04，证据 docs/validation/color_resources/r07_thumbnails/；内容寻址缓存键+世代守卫+预算 LRU；悬停预览为计划可选项，随 R12 评估）

1. 拟新增 thumbnail request DTO：source/frame revision、节点输入端 identity、LUT hash、显示变换、尺寸、session generation；不得只用 filename 缓存。
2. 在既有解码/渲染队列评估低分辨率节点输入→候选 LUT→显示变换；只处理可见资源，滚动/搜索/关面板取消，做有上限 LRU。
3. GUI 线程接收 QImage 并转 QPixmap；过期任务不能覆盖当前 clip/文件夹。错误保留“预览失败”提示，无图时保持占位。
4. 若做 Viewer 悬停预览，使用独立 transient token，鼠标离开/Escape/切页恢复权威输出，不保存、不入历史；不能重置用户当前节点。

**验证**：快速滚动/搜索、两 clip 同名 LUT、同帧不同源、资源热更、浮动关窗、100个可见/10000个总资源；断言任务上限/缓存上限、无 UI 阻塞、无旧图回写。对比候选应用后的真实输出与缩略图像素，明确显示变换。

### R08 — 逐节点已处理缩略图　[x]（2026-11-04，证据 docs/validation/color_resources/r08_node_thumbnails/；原生 per_node_rendering=evaluated_node_thumbnails）

1. 现 `ColorNodeSourcePreview` 只供应源缩略图。拟以 node-output demand 向 graph plan 请求可见 Corrector/Compound 输出，不能把 R07 候选 LUT 图混作节点输出。
2. 利用编译器依赖锥和执行器有效输出 lease；保持输出到 GUI 图像的既有异步通道、限频/尺寸预算；不逐帧同步 readback 每个4K节点。
3. key/mixer/Compound 内外含义标注清楚；锁/旁路、缩略图开关、缓存失效与 seek 同步；未评估时明确 fallback。

**验证**：节点01加非 identity LUT、02加明显FX，两个缩略图和源均可区分；改01→02依赖结果更新，独立分支不无效重算；删节点/切clip/关闭面板期间无旧回调；测缓存和峰值内存。R08完成后才可替换“source_thumbnail_only”验收状态。

### R09 — 保存、版本、still、共享与跨工程资源闭包　[x]（2026-11-04，证据 docs/validation/color_resources/r09_closure/；闭包收集+保存期自动迁移+缺失可修复；引用计数回收归 N06/R12）

1. 以 graph 为权威收集资源引用闭包，覆盖 Compound 子图/共享定义/效果实例/LUT；资源 manifest 有版本和迁移，保留未知字段。
2. 保存、另存、autosave、版本切换、Gallery still、copy/paste、跨工程粘贴均携带完整定义与资源；缺资源条目保留可修复状态。
3. Shared 明确“共享定义参数”与“实例覆盖”关系，跨栈 fanout 在一个事务中更新；锁必须后端校验，不能只禁控件。
4. 文件资源回收以项目/历史/版本/still所有引用计数为准；撤销删除仍能恢复，不提前删除历史引用内容。

**验证**：扩展 `test_color_graph_persistence.py`、stills/versions/compound_shared；在无原绝对路径的临时工程位置重开；共享跨2clip、子图粘贴、缺资源重定位、旧schema、新schema未知字段；redo快照相同、所有出口效果不丢失。

### R10 — 清理旧 UI/backend 写入与用户库偏好　[x]（2026-11-04，证据 docs/validation/color_resources/r10_routing/；A1–A5 图模式路由+库偏好持久化+D1 清除；D2 旧控件实例删除随 N03 typed handle 批次收口）

1. 完成 R00 全表路由：旧顶栏 Effects 菜单、`add_plugin_to_current_layer`、`apply_lut_from_path`、脚本/preset、Tracker/Qualifier 编辑都通过正式目标入口，或明确只属旧模式并禁在图模式调用。
2. 将隐藏旧 `plugin_panel`/`lut_panel` 的参数读取迁为 projection/provider，逐个撤掉后才删除旧控件实例；不要先删导致 controllers 访问空属性。
3. 搜索/分类折叠/收藏/列表模式/缩略图大小存用户配置；不进入工程 undo。资源 ID 重命名/缺失时收藏可诊断；配置不保存 LUT 像素缓存。
4. 保持新版左侧默认组；迁移仅一次，已浮动用户窗口不强行归位，Reset Layout 重新建立默认；同标签组仍保留双击添加替代拖放。

**验证**：R00每个入口逐一触发，assert command计数=1、engine无双写；2次启动/自定义浮动保存/切页/重置布局；中文/英文/窄宽/200%缩放。完成表中“废弃待删”数为0。

### R11 — 图标、诊断与真实状态投影　[x]（2026-11-04，证据 docs/validation/color_resources/r11_projection/；权威域标记+实例状态+生产横幅+拒绝诊断）

1. 由权威节点有效参数派生OFX/LUT标记，沿用 `node_tool_indicators.py`；仅浏览库/打开设置/悬停未提交不应显示已应用标记。
2. 添加等待/失败/缺资源/能力不足状态，ack/reject区分；真实后端模式才移除“交互预览”。
3. 诊断关联intent/target/resource/revision和耗时；错误不冒充成功，不逐帧刷屏；UI只接稳定的可翻译诊断码。

**验证**：成功后有标记、reset/remove/undo后正确更新、disabled实例策略固定；插件缺失仍保留配置并显示原因；失败后画面/参数/projection保持一致。沿用工具标记现有测试再添加资源命令场景。

### R12 — 完整发布与实机矩阵　[ ]

前置：R01–R11、下面 N01/N02/N03/N07/N09 的出口完成。

1. 同一帧同一图比较Color Viewer、Edit、Composite、视频/图片/full-res导出、scope与node thumbnail；选中节点改变不应改变最终输出。
2. 测`测试素材01.mov`、4K视频、HDR负值/高亮float输入、非单位DOMAIN LUT、8/32节点、分支、Compound、共享、2个插件+LUT。
3. 录制暂停/播放/seek/拖参/增删/undo/切clip/切页/浮动/resize/多屏；保存GPU指纹、日志、p50/p95/p99、掉帧、内存、显存、取消队列长度。
4. 默认生产图flag保持现状直至完整门槛通过；明确不支持后端的错误，不以CPU基础算子单测代替全域支持。

**验证**：UI事件p95目标<50ms；4K60预算16.67ms分解报告真实值，重AI不作虚假保证；连续播放/反复操作30分钟无持续增长、无黑scope/旧帧覆盖；按平台逐项PASS/FAIL/NOT RUN。回退可关闭新后端开关，UI仍能显示权威只读状态并明确说明。

## 3. 上一份节点计划的剩余事项（统一迁入，不遗漏）

以下只列尚未完成或未全量验收的内容。前轮已修复的信号参数、Compound事务、primary回显、图形上下文示波器呈现、Mixer槽身份、完整节点复制和工具标记**不重新列成待开发**，但保留回归。

| ID / 原任务 / 优先级 | 当前缺口与实施方法 | 验证方法 / 完成门槛 | 依赖 |
|---|---|---|---|
| N01 / B13、B16.5 / P0 | front/color output图执行flag默认关闭，full-res仍走旧出口；复杂图显式拒绝。将完整graph plan交给生产预览与full-res/视频/Edit/Composite路径，统一source覆盖、尺寸和f4精度，避免display transform双执行。先完成各出口语义，再考虑默认开启。 | 原生flag=0/1仅有基础冒烟证据；新增非串行+FX+LUT各出口同帧golden、取消/失败不发布半帧；性能/资源矩阵通过才默认开启。 | R06、R09、N09 |
| N02 / B07、B10、key / P0 | 图key边仍有白matte限制，Qualifier GPU keyer/窗口/bitmap与blur/clean/gain后处理未完整图域化，Invert Key Input未消费。将key依赖显式编进snapshot/plan，GL/CPU分别定义取反及范围、组合顺序，移除无依据的常白替代。 | 梯度key、窗口、限定器、透明bitmap、Outside、KeyMixer、反相互补、blur/clean/gain组合；alpha输出和source相关性验证；unsupported继续禁用。 | 图编译/执行器；N03目标绑定 |
| N03 / B06、E10、E27 / P0 | Tracker/Qualifier及非primary工具仍有legacy层读写，复杂图/嵌套选择未统一node-ID。逐面板改 typed handle读取、command提交和事务；取色/跟踪任务绑定node+frame+session；不按root active_index猜节点。 | 两节点不同曲线/Window/Qualifier、Compound内部、共享、切clip时异步跟踪、撤销、保存重开；所有工具参数实际落正确node；无隐藏旧层写入。 | R03、R10 |
| N04✅ / 节点LUT菜单 / P1 | 菜单现禁用；复用R02/R05新资源注册与节点命令，保持菜单与浏览器显示同一选择。不要再接旧全局LUT。 | 菜单应用与拖入产生相同图载荷、像素、undo；缺失/不支持资源有诊断。 | R02–R06 |
| N05 / Node Color Space、Gamma / P1 | 目前禁用、无真实节点级变换。定义输入/输出/工作空间变换位置与版本化枚举，独立node_options字段，避免与primary gamma元组冲突。由后端实际消费。 | 多空间色卡、Gamma roundtrip、HDR、LUT顺序，CPU/GL/Metal按能力验证；关选项=原结果；菜单勾选与保存一致。 | N01、R06 |
| N06 / Node Cache Auto/On/Off / P1 | 菜单禁用，已有plan LRU不等于节点渲染缓存策略。定义Auto判定、On强制缓存及Off禁止、成本/大小预算；key包含输入与资源revision；状态仅实际命中后显示。 | 改上游/自身/资源/尺寸后失效，下游选择不误失效；Off无缓存；越预算可解释；删除/取消释放；不得以蓝条代替实际缓存命中。 | R06、R08 |
| N07 / Timeline/组链 / B14 E27 / P0 | Timeline真实栈模型未接通；Group pre/clip/post owner虽已接真实身份，完整图语义输出链仍需专项。为Timeline定义真实owner栈/迁移/命令/保存，再统一pre→clip→post→timeline顺序。 | 两clip共享组、不同组、Timeline最终效果；Color/Edit/导出一致；切scope不写错stack；锁/撤销/资源闭包；无实现则继续只读并提示。 | R03、N01、R09 |
| N08✅ / Shared跨clip / B05/B14 / P0 | 跨clip粘贴共享定义、参数fanout与实例覆盖未完整认证。解析定义依赖闭包，冲突重命名/去重规则稳定；跨栈原子fanout，Make Local保留当时结果。 | A/B clip同时引用改参数，两边更新且1undo；跨工程同ID不同内容不覆盖；锁/删/解共享、Compound共享闭包。 | R03、R09 |
| N09 / S07、B16 / P0 | 持续播放/4K/浮动resize压力、性能资源录制未完整。前轮9种scope原生截图和短素材冒烟已通过，不等于压力完成。按R12执行完整矩阵，保留NOT RUN平台。 | scopes/s00_s01_record协议，30分钟播放/seek/拖参/切页/多模式/浮动；无texture_not_shared/FBO错误、过期输出和持续泄漏；真实p95预算报告。 | R06、N01 |
| N10 / keyframes / P1 | 节点图编辑上下文关键帧暂缓，已有底栏图标只识别元数据。定义节点/实例/参数路径、插值/时间base、剪贴板/版本归属；命令与播放评估读取同域。 | 插入/删/移动/插值、效果排序后身份不丢、trim/retime/Compound/shared、seek随机帧与导出一致，撤销一次手势一次command。 | R04、N03、N07 |
| N11✅ / E12、global history / P0 | standalone历史仍旧适配器；不经图宿主的全局undo/redo对engine同步存在缺口。让项目发布观察者统一重投影、刷新面板/资源/渲染，避免只有图菜单能undo正确。 | 菜单/键盘/全局undo和redo、剪贴板资源、切clip、standalone分别验证；图JSON/engine/Viewer/设置同时回到一致revision。 | R03、N03 |
| N12✅ / B04 B14 剪贴板 / P1 | 完整节点复制本轮已补；选择性paste_grade_domains、跨工程still/clipboard子图资源闭包仍需补。定义选择域语义，复用R09资源闭包，递归重建本地身份。 | 仅粘曲线不改LUT/Window，跨Compound/跨clip/跨工程、共享定义、缺插件保留；多次粘贴不共用legacy Layer ID；undo完整恢复。 | R09、N08 |
| N13✅ / E24 LUT导出 / P1 | 既有LUT fast path尚未完整接导出调用点。统一graph pointwise可表达性预检，接正式export_lut；空间/时序/依赖窗口图拒绝并解释。 | 3D色立方golden，串行pointwise+LUT顺序，含Glow/NR/Window/AI的图不能导出“成功但少效果”的cube；重载导出LUT对照。 | R05、R06、N01 |
| N14 / B12 E22跨后端 / P1 | CPU/Metal只支持基础点态域，fragment全域Metal Corrector未接，插件/HSL/LUT/warper/fx/非Normal等仍fail-closed。逐域实现与能力声明一起提交，不能一口气取消限制。 | 同一fixture跨GL/CPU/Metal、对应平台原生；未知/缺实现继续明确拒绝；Linux/Windows/Apple Silicon/Intel按机器列NOT RUN。 | R06、R12 |
| N15 / Resolve Serial+Window / P2 | CPW/LPW/PPW/PCW一键组合缺失。先实机核对各窗口含义，再在service原子创建Serial+相应Window默认形状/关联，菜单/快捷键走同一命令。 | 与Resolve实际新建结果对照；1undo删除组合，默认中性、形状/跟踪身份正确，不以单独Window可用冒充组合完成。 | N03、N11 |
| N16 / 基线与资源治理 / P1 | F06/F07时序/bake fixture生成器仅行为用例；grading_extreme/checker既有位级失配；测试GC/QThread积累有历史债务。补确定性fixture、按ADR核验重归档，逐所有者治理timer/thread/GL lease。 | 不能删断言/放宽误差掩盖失配；记录GPU指纹与逐像素差；重复套件无崩溃和活动QThread残留；DeferredDelete规则保留。 | R00、R06 |
| N17✅ / 可选依赖诊断 / P2 | 启动存在ThumbEngine FFmpeg路径和AI模型未安装告警。统一探测来源与可用性状态，区分Qt FFmpeg可用与外部工具缺失；未安装模型不能作为AI通过证据。 | 无FFmpeg/模型环境仍可浏览库、条目提示准确、实际调用明确拒绝或使用已声明替代；日志不泄露无关内容。 | R01、R11 |

## 4. 关键规格先固定，避免施工返工

1. **处理顺序**：每个Corrector primary/curves/FX/LUT/key顺序、全局RAW/NR/grain/display位置必须写成版本化规格；用非交换算子证明，不重复执行全局步骤。
2. **目标与历史**：UI选中/搜索/收藏/换库标签不入工程历史；插件/LUT/参数/排序必须入项目历史；布局用既有布局存储，不能污染渲染revision。
3. **临时预览与提交**：正式后端设计 transient token、commit/cancel；切clip/删node后的旧手势无效。不要把本轮会话草稿自动批量写进新工程。
4. **未知与缺失**：保留插件identity/未知字段/参数；能力不匹配明确显示；任何输出不得跳过实例再报成功。
5. **共享/Compound**：资源操作必须带完整路径；外壳不接受drop，先进入内部Corrector；未来若支持外壳effects须先定义算法位置与schema，不能猜测。
6. **性能与线程**：UI不扫描大库/解码/编译/同步GPU readback；渲染输出lease、取消、延迟释放沿用现有机制；缩略图与计划缓存都有预算。

## 5. 建议提交划分与回退方法

| 提交 | 允许启用的真实能力 | 回退 |
|---|---|---|
| R00–R02 | 只读正式catalog、受控资源导入；尚不应用 | 保留旧资源manifest与迁移备份；撤adapter，不删除原文件 |
| R03–R05 | 单节点命令/参数/LUT；仅能力预检通过的图 | 关闭正式resource adapter，保留工程新字段并只读诊断；UI preview显式标识 |
| R06 | 图内执行与资源生命周期 | 新执行flag关闭；不支持旧出口的复杂图明确拒绝 |
| R07–R08 | 真缩略图/候选预览 | 取消预览任务，回到明确占位/源fallback，不伪装已处理 |
| R09–R11 | 保存/跨clip/完整入口迁移 | 保留schema未知字段读取，停止写新版；不要降级保存丢字段 |
| R12/N验收 | 生产默认图、全出口、已验证平台 | 按实际平台保留开关与能力禁用；记录退化原因 |

## 6. 交互与原生人工验收表（每行留证据）

| 场景 | 预期 | 必存证据 |
|---|---|---|
| 左侧标签组 | Gallery/LUT/Effects切换不隐藏Nodes | 三种标签全窗截图；Nodes可见 |
| 浮动 | Effects/LUT拖浮、缩为360px、改尺寸、关闭重开仍可操作 | 浮动截图和录制；设置可滚动、无横向截断 |
| 添加 | drag具体node、双击/右键选中node，目标底栏准确 | 操作前后target/命令日志；非目标节点不改 |
| 拒绝 | Source/Output/Mixer/Compound外壳/锁/无目标不应用 | 禁用原因；graph revision不变 |
| 资源/参数 | 同FX重复2次、启用、重排、重置、删、undo/redo；LUT替换/强度/移除 | 实例ID、顺序、图JSON、实际画面 |
| 会话 | 拖动/AI/预览未完时切clip/scope/Compound/版本 | 旧ack不改新目标；取消日志 |
| 真实显示 | LUT预览、逐节点缩略图、最终Viewer、scope均同revision | 原图/候选/节点/最终图及像素对照 |
| 保存 | .topos另存、重开、跨路径、版本、still、copy/paste | 资源闭包manifest、输出hash/误差 |

## 7. 可直接执行的当前验证命令与后续补测

当前已经存在的测试文件：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/ui/test_effects_browser_panel.py tests/ui/test_lut_browser_panel.py \
  tests/app/test_color_resource_preview.py tests/app/test_color_resource_catalog.py \
  tests/ui/test_color_resource_dock_layout.py \
  tests/ui/test_color_node_editor_view.py tests/ui/test_color_node_editor_panel.py \
  tests/ui/test_factory_default_layout.py -q

QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_color_graph_ui_contract.py tests/color/test_color_graph_commands.py \
  tests/color/test_color_graph_service.py tests/color/test_color_graph_persistence.py \
  tests/app/test_color_graph_app_host.py tests/app/test_color_graph_parameter_feedback.py -q

TOPOS_COLOR_GRAPH_RENDER=1 .venv/bin/python scripts/validate_color_node_media.py \
  tests/test_videos/测试素材01.mov --resources --workflow --output /tmp/topos-color-resources

git diff --check
```

- `--resources`验证真实app加载目录、左侧停靠组、Qt MIME投放、每节点草稿、浮动及**engine/project未被UI草稿修改**。没有真实LUT时仅在隔离临时配置目录生成UI identity fixture；这不证明LUT渲染完成。
- `--workflow`验证前轮已有节点后端/源缩略图/scope基础冒烟；不能代替新资源命令或复杂图导出验证。
- R01–R12中“拟新增”测试需随实现创建，并分别补成功、拒绝、取消、旧会话和保存重开案例。纯 UI 测试保持，用于保护backend更换时的界面契约。
- 当前截图/报告见 `docs/validation/color_resources/2026-11-03/`。原生素材、目录计数和退出状态以JSON与日志为准。未实现项不因截图存在变成PASS。

## 8. 最终完成检查表

- [ ] R00–R12都有实现提交、自动测试、必要原生证据。
- [ ] N01–N17逐项完成或明确保留能力禁用与NOT RUN；没有用“旧计划已完成”笼统关闭。
- [ ] 所有真实入口都经typed target + service/command + ack projection；不存在草稿成功、实际无效的状态。
- [ ] 画面、scope、逐节点预览、导出和保存重开使用一致图与资源。
- [ ] Undo/redo、clip/scope/Compound/共享/版本/取消/关窗资源无遗漏。
- [ ] 新schema可迁移，缺资源可修复，未知插件与字段不丢失。
- [ ] 实机性能与平台矩阵如实记录，默认flag只在门槛通过后切换。
