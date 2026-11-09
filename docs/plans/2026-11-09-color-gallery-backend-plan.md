# Color Gallery 后端接入计划

日期：2026-11-09。状态：**G01–G13 已实施**（2026-11-10 收尾）。用户本轮授权前端重做；本文不表示下列新增后端已完成，也不授权前端重排顺带迁移／删除用户素材。

## 实施记录（2026-11-10 G01–G13 收尾）

G01–G13 全部实施并分批提交（`744cef41`→`b7ab0bf4`，15 个实现提交）。
离屏可执行子集验收（199 个新测试＋G12 计划矩阵 393 全绿＋跨域锚点
1286 通过＋1k/10k 查询基线＋原生启动 exit 0）与真机边界（GPU 分屏
presenter／滚动帧率／跨机器导入未执行；split 如实拒绝不假启用；
PowerGrade/Timelines 的 UI 导航待 chrome 面板拆分 WIP 合入后接线）
见[验收记录](../validation/color_gallery_backend_g01_g13_20261110.md)。

## 1. 前端目标与本轮边界

参考本机 DaVinci Resolve Color Gallery：单行约28px工具条，可折叠侧栏，Stills／PowerGrade／Timelines入口，16:9缩略图及画面外caption，Grid/List切换，列表三列Still ID／Label／Timeline Name，搜索／排序，抓帧、应用调色、参考图动作。Memories没有现成模型／宿主，保持禁用并给出原因。

本轮前端可以复用现有数据做显示、过滤、排序和选择同步，保留原抓帧／应用／参考／Color Match信号。不能仅把User目录改名为Stills、System目录改名为PowerGrade，就宣称已有“工程专属静帧”“跨工程PowerGrade”“按时间线分组”等后端语义。侧栏的真实目录映射或禁用状态须在UI中诚实体现；集合／工程模型由后续任务定义。

后端尚无Still ID／独立Label／Timeline Name时，UI使用明确的空值或临时显示标识；不得把文件排序行号当永久ID、把源素材display_name当Timeline Name、把文件重命名当独立Label编辑。最终前端控件名字／行高以本轮实际提交为准；本文中的新增服务／DTO均标为拟新增。

### 1.1 必须保持的现有接口

| 接口／路径 | 当前真实用途 | 本轮前端约束 |
|---|---|---|
| `src/features/color/ui/presets_panel.py`：`PresetsPanel` | 原Presets升级的Gallery；类名仍被宿主和脚本引用 | 保留类名；外观变化不另造第二个Gallery实例 |
| `grade_applied(str)` | `.topos`路径→应用完整调色 | 保留payload和原宿主handler，不在delegate直接改engine |
| `capture_requested()` | 抓当前画面为静帧／grade preset | 保留无参信号；后端升级后再引入有上下文请求 |
| `apply_categories_requested(str)` | 分类应用，宿主可弹GradePasteDialog | 保留路径和取消语义 |
| `reference_requested(str)` | 加载最佳精度参考并开启wipe | 保留与“应用调色”不同的动作 |
| `match_requested(str)` | Color Match增量建议 | 保留建议／确认／撤销链，不直接套用参考像素 |
| `get_current_folder()`、`scan_presets()` | LayerController抓帧目的地与成功刷新；脚本也使用 | 返回真实可写目录；虚拟集合未有写入路由时禁用Capture，不返回虚构目录 |
| `list_widget`、`search_edit`、`zoom_slider`、`folder_combo`、`_column_grid`、`delegate` | 既有测试／UI操作入口；folder_combo可作为隐藏兼容适配 | Grid/List共用稳定身份和单一选择；隐藏兼容控件也需正确同步 |
| ItemData `UserRole`／`UserRole+1` | `.topos`路径／`PresetMeta`对象 | 原handler／delegate仍用这些role；增加新role不能改变旧含义 |
| `dockPresets` | `dock_layout_controller.py`中Gallery的持久化objectName | 保留停靠身份、浮动／Dock菜单和已有布局 |

`src/app/main_window.py`已将五个信号接到`src/app/controllers/layer_controller.py`相应handler；不应在前端再接一套相同回调，导致一次点击两次抓帧／应用。

## 2. 当前存储、动作链和已知边界

### 2.1 存储与元数据

- 根目录由`_resolve_presets_root()`基于Qt AppDataLocation解析，空值回退`~/.topos-color/Presets`；已有`User`、`System`子目录。它们是现有目录，不等于已定义的project／global album scope。
- `_migrate_legacy_cwd_presets()`仅在新子目录为空时复制旧`<cwd>/Presets/{User,System}`，保留来源，不删除旧数据。目录失败已有日志／部分UI反馈；后续迁移不能用启动时自动移动取代它。
- `scan_presets()`只扫描当前目录中的平铺`.topos`，按名字排序，不递归；读取可选`.meta.json`到`PresetMeta`，不为每条提前解码thumbnail。目录导航combo当前只列User/System及各自一层子目录。
- `.topos`是既有grade工程包。`_get_preset_meta()`会在delegate绘制时读取zip中的`project.json`，按路径缓存；这份数据与Gallery的`.meta.json`不是同一种metadata。
- `PresetMeta`现有字段包括display_name、clip_id、asset_id、timeline_frame、source_frame、source transfer/primaries/range、created_at、still_precision、working_space、display_transform。**当前没有**独立still_id、label、album_id、project_id、timeline_id／timeline_name。
- `save_preset_meta()`当前落盘`schema:1`；代码注释中的“schema 2 HDR”不等于真实写盘版本已是2。新迁移必须按磁盘事实识别，不能仅凭注释跳版本。

### 2.2 文件bundle

| 文件 | 当前用途／精度 |
|---|---|
| `{name}.topos` | grade工程包，原应用／分类应用入口 |
| `{name}.thumb.jpg` | 512长边缩略图，UI显示和导出thumbnail |
| `{name}.still.jpg` | 1920长边参考JPEG，旧库和降级回退 |
| `{name}.meta.json` | Gallery来源与精度metadata，可缺失 |
| `{name}.still.exr` | float32高精参考，保留超白／负值 |
| `{name}.still.exr.json` | EXR alpha声明：channels／precision／alpha_semantics |

`preset_meta.py`已实现`write_still_images`、`write_still_exr`、`load_still_best`、`still_alpha_declaration`和`align_alpha_domain`。EXR读写含现有PIZ编码及问题压缩格式的隔离解码路径，不能用新的UI图片加载器绕过。

现有`PresetsPanel._SIDECAR_SUFFIXES`没有包含`.still.exr.json`；rename/delete和capture失败清理尚未统一管理完整bundle。该不一致列入G03后端事务任务，前端不扩大文件操作范围冒充已经修好。

### 2.3 宿主动作

| 真实入口 | 已有能力 | 后续工作 |
|---|---|---|
| `LayerController.on_capture_still` | 视频先准备指定源帧；读取调色结果；保存JPEG／可选HDR EXR；`SaveRouter._save_single_asset_project`保存grade；写来源meta；成功`scan_presets`；已有保存失败清理 | 统一bundle事务、异步取消／上下文校验、补完整sidecar和metadata |
| `on_apply_grade_from_preset` | `SaveRouter.load_single_asset_data`读取；`_prepare_preset_layers`处理适用层；应用旧完整调色链 | 维护现有语义，核对timeline目标是否完整走command/revision，不只改engine显示 |
| `on_apply_preset_categories_requested` | 读取grade＋临时GradeClipboard；分类选择；timeline目标可路由`_paste_grade_to_clip`，standalone走engine history | 保留分类取消和单步undo；统一原始payload／目标上下文与版本校验 |
| `on_preset_reference_requested` | `load_still_best`优先EXR；`engine.load_reference_still`；同步Canvas参考菜单 | alpha／色彩域和降级原因闭环，避免只有缩略图却声称HDR参考 |
| `on_match_to_still_requested` | 最佳精度参考、FrameStats、增量建议／用户选择／应用 | 异步分析、上下文token、能力及失败回填 |
| `PresetDelegate.editorEvent` | 中键释放应用grade；右键菜单提供五类原动作 | 新Grid/List保持入口可达；不可把普通单选变成不可撤销自动套用 |
| `_export_thumbnail` | 只复制`.thumb.jpg`到用户选定文件 | 当前没有完整grade bundle导入／导出，更没有Resolve DRX兼容声明 |
| `src/features/scripting/api.py:capture_still` | 旧子目录格式`thumbnail.jpg＋grade.json`，不同于平铺`.topos`扫描 | 单列兼容任务；不能让新UI继续产生脚本可见但Gallery不可见的静帧 |

## 3. 任务总表与建议批次

人日为熟悉项目的一名开发者粗估，含focusedtest，不含真机设备等待和遗留库人工修复；不是交付承诺。Memories／DRX互操作不包含在该估算中。

| ID | 优先级 | 依赖 | 后端交付物 | 估算人日 |
|---|---|---|---|---:|
| G01 | P0 | 前端接口冻结 | ✅ 已实施（`744cef41`）：Gallery DTO／查询契约、宿主adapter、capability与上下文token | 1.5–2.5 |
| G02 | P1 | G01 | ✅ 已实施（`7b113f58`）：Still稳定身份、独立Label、Timeline来源metadata迁移 | 2–3 |
| G03 | P0 | G01、G02 | ✅ 已实施（`90d47c11`）：bundle manifest与抓取／rename／move／delete文件事务 | 2.5–4 |
| G04 | P1 | G01–G03 | ✅ 已实施（`c5a59620`）：集合scope规格／PowerGrade真实存储／虚拟Timelines（UI 导航待 chrome 接线） | 2.5–4 |
| G05 | P1 | G01–G03 | ✅ 已实施（`8d1e21d0`）：mtime 索引／代际守卫异步 runner／面板索引化扫描 | 2–3.5 |
| G06 | P0 | G01、G03 | ✅ 已实施（`9bfc37ac`）：抓帧会话一致性／取消令牌／诚实刷新 | 2–3 |
| G07 | P0 | G01、G02 | ✅ 已实施（`0619d719`）：timeline 全量应用统一权威命令通道／守卫恢复 | 2–3 |
| G08 | P1 | G03、G04 | ✅ 已实施（`ef933542`）：.toposbundle 导入导出／安全限制／身份冲突政策 | 2.5–4 |
| G09 | P1 | G02、G03 | ✅ 已实施（`16938a5b`）：precision 如实记录＋参考 alpha 合同（ODT 单次应用维持既有引擎口径） | 1.5–2.5 |
| G10 | P1 | G02、G03、G06 | ✅ 已实施（`546cc415`）：旧脚本格式可恢复迁移＋脚本 API 同一资源 | 1.5–2.5 |
| G11 | P1 | G01、G04、G05 | ✅ 已实施（`d39bb2d2`）：版本化偏好／mtime 门控外部同步／重扫保选中 | 1–1.5 |
| G12 | P0 交付门槛 | 已启用G01–G11 | ✅ 离屏可执行子集已验收（`docs/validation/color_gallery_backend_g01_g13_20261110.md`）；GPU/真机边界如实未执行 | 2–3 |
| G13 | P1 | G01、G02、G07、G09 | ◐ 可离屏子集已实施（`b7ab0bf4`）：wipe 会话 provenance＋split 协议如实拒绝；**分屏 presenter 未实施**（见验收 §4） | 3–5 |

总粗估26–41.5人日（含用户追加的划像／分屏G13）。建议先交付G01/G02/G03/G06/G07最小安全链，再做集合／索引、导入导出和G13；每批都执行G12对应部分。如一次性推进全部模型，需先独立评审schema和存储迁移。

## 4. 拟新增数据合同

以下是后端设计输入，不是已经存在的Python类型。应放在color领域的无Qt合同层，由app/UI适配；不得把QWidget、QPixmap、ModernGL对象、线程或机器绝对路径写进工程DTO。

| 拟新增DTO | 最小字段／语义 |
|---|---|
| `GalleryAlbum` | `album_id`、`scope`（project/global/legacy-folder）、`kind`（stills/powergrade/timeline）、`label`、parent_id、只读／可写、project/timeline ID、revision |
| `GalleryStill` | 稳定`still_id`、album_id、独立label、展示Still ID、bundle引用、source provenance、created_at、content revision、precision／alpha／display context、capabilities |
| `GalleryQuery` | album/scope、search text、sort key/direction、page/cursor、request revision；稳定次排序用still_id，避免重排跳项 |
| `GalleryCaptureRequest` | request ID、目标album、project/clip/asset/timeline ID、source/local/global具名帧、grade revision、color context、expected target revision、cancel token |
| `GalleryOperationResult` | request ID、status（success/rejected/cancelled/partial）、受影响still IDs、实际revision、warnings、稳定reason code、可恢复事务信息 |
| `GalleryBundleManifest` | bundle/schema version、still ID、每个文件的角色／相对路径／必要性／hash、依赖、grade snapshot身份、精度／alpha声明，未知字段保留 |
| `GalleryCapabilities` | 每动作supported/checked/reason；区分可预览、可应用grade、可reference、可match、可写Label、可Capture、可导入导出、可rename/delete |

路径解析由本地repository/service负责；旧信号仍可暂时传`.topos`路径，内部先解析稳定身份再执行。不要以文件名、列表row、缩略图caption为领域身份。

## 5. 逐项实施与验收

### G01：查询、上下文与capability宿主

**输入→输出**：当前工程／媒体／timeline上下文、选中still、album可写性和服务能力→DTO列表／选择快照／动作可用性；用户动作→request/result。

**步骤**：提取当前`scan_presets`的读取结果为查询adapter，先保持User/System平铺行为；MainWindow继续只安装一次动作handler；前端Grid/List共用stable selection。给capture/apply/reference等定义上下文revision，延迟任务完成时校验。无媒体、无grade、只读album、损坏bundle等返回具体原因；Memories、DRX、未实现集合不设置假enabled。

**已有能力不重做**：五个旧信号与LayerController handler保留；搜索／目录改变不触发应用、抓帧和工程dirty。

**门槛**：无素材Capture禁用；仅JPEG且无有效grade时可reference但不可Apply；图片／timeline目标正确；一动作一请求一回填；UI拒绝不改模型；异常记录具体上下文。

### G02：稳定Still ID、Label与Timeline来源

**输入→输出**：既有`.topos＋meta`、PageContext、用户label→稳定条目身份与可迁移metadata。

**步骤**：

1. 确定新metadata schema版本，真实旧writer输出为1；对现有HDR扩展字段兼容读取。新增still_id、label、album/project/timeline ID和timeline_name_snapshot，保留source display_name原语义。
2. Still ID首次分配后跨rename／move不变；显示编号与主键分开。若使用album内序号，规定并发分配／复制冲突和删除后不复用政策，不能靠当前排序重算。
3. Label单独存metadata，不默认rename文件；三列视图按ID/Label/Timeline Name呈现。旧条目无来源时显示未知／空，不能推断成当前timeline。
4. 迁移保留未知字段／旧grade payload；缺meta的旧preset可只读展示，首次写入时原子补齐。跨库导入冲突需保留外部origin ID并分配本库ID或明确拒绝。

**门槛**：扫描排序不改ID；文件rename后label和timeline不丢；缺损／未知schema有明确降级；工程和timeline同名不靠名字串联；metadata更新失败不让UI假保存。

### G03：完整bundle事务与危险文件操作边界

**已有**：rename有sidecar失败回滚；delete有确认，但直接os.remove且个别失败被忽略；导出仅thumbnail。新增统一bundle service，不在delegate中逐文件操作。

**输入→输出**：stable still ID、expected revision、rename/move/delete/capture意图→完整bundle的原子或可恢复结果。

**步骤**：

1. 统一manifest枚举，至少包含`.topos/.thumb.jpg/.still.jpg/.meta.json/.still.exr/.still.exr.json`；原`_sibling_paths`、capture失败清理、未来import/export共同使用，不能各自维护不同suffix清单。
2. 验证repository root真实路径、symlink／父目录归属、目标同名topos和孤立sidecar冲突；不能仅`startswith(root)`检查。名字校验复用／完善`_is_safe_preset_name`，保持Windows／大小写不敏感文件系统兼容。
3. 同卷先staging再原子提交；跨卷move按copy→校验→提交→清理的可恢复事务。rename任一文件失败不覆盖已有目标sidecar，rollback失败输出清单和恢复入口。
4. 删除优先可恢复Trash／内部tombstone策略，清楚界定它不是工程Undo；永久清除须由明确动作确认。保留现有确认，前端刷新／切album绝不触发删除。
5. Capture覆盖已有名称必须用同一事务；失败只清本次staging，不能把用户原来的still sidecar清掉。磁盘满／权限／冲突返回结构化结果，不静默吞OSError后显示成功。

**门槛**：逐个文件点注入失败，原bundle可继续读取或有明确可恢复状态；alpha声明随rename/move/delete；取消不留半包；同名不同大小写／孤立目标sidecar／只读卷／symlink越界均测试；删除后刷新不假称全部成功。

### G04：Stills／PowerGrade／Timelines集合语义

**输入→输出**：工程ID、timeline ID、现有目录、album操作→有明确scope的集合查询／Capture目的地／跨工程资源引用。

**步骤**：

1. 先写scope规格：Stills可属于工程album，PowerGrade为用户全局可复用grade集合，Timelines是按来源timeline过滤的虚拟集合。只有真实存储服务完成后才启用对应增删／拖入／Capture。
2. 旧User/System映射成明确legacy-folder album，不能自动搬走或把System设成PowerGrade来伪造scope；已有数据原路径继续可读。若选择逐步升级，提供可审阅迁移结果和回退，不启动自动删除。
3. 虚拟Timelines集合的Capture应要求明确可写目标album，而不是从timeline label拼目录。timeline重命名时ID不变，旧snapshot可展示；timeline删除后来源标为离线／已删除，still仍可独立使用。
4. 工程关闭／切换刷新scope和capabilities，异步旧query结果不得回填到新工程；全局PowerGrade独立于工程关闭，project album不泄漏到其他project视图。
5. 文件夹树可扩展递归读取但要限制深度／循环、lazy加载；album目录rename和数据库identity分离。

**门槛**：两工程各含同名timeline／album仍不串项；工程关闭PowerGrade保留且project still不可误写；旧User/System路径／Capture兼容；虚拟组只读能力准确。Memories另立模型和工期，保持禁用。

### G05：异步查询、索引与缩略图性能

**已有**：扫描已避免eager QPixmap解码；delegate LRU上限256，含missing负缓存，rescan清缓存；搜索对现有条目过滤；ColumnGridController封装flush-right、scrollbar及deferred retile。

**输入→输出**：GalleryQuery、文件变化、viewport可见范围→带generation的增量查询结果／QImage缩略图数据和可失效索引。

**步骤**：目录扫描、meta/zip读取、thumbnail decode放后台；QPixmap仅GUI线程创建；不要在paint继续打开zip获取grade小圆点。索引key含identity与mtime/size/content revision，坏包单项诊断，不让一个错误拖垮整库。搜索排序分页复用cache，caption／Label／Timeline Name缺失的排序固定。本轮专用GalleryGridController保持108–256px缩略图宽度、16:9和图外caption，resize只换列；List模式27px行／20px表头，共用同一QListWidget模型。不要改其他面板ColumnGrid默认行为。

**门槛**：1000／10000条库首次显示、搜索p95、滚动帧率、CPU／内存基线有实测；resize不重扫磁盘，thumbnail每revision不重复decode；快速切album／关闭panel不回填旧结果；caption长中文／emoji／空图不破布局。用户列数、排序稳定，不因vertical scrollbar出现产生retile循环。

### G06：抓帧会话一致性与取消

**已有**：`on_capture_still`已准备视频源帧、render、SaveRouter保存和失败清理；不可重写成截图或UI线程直接操作GPU。

**输入→输出**：GalleryCaptureRequest→完整grade+frame+metadata bundle，或cancelled/rejected结果。

**步骤**：锁定project/clip/source/timeline帧与grade revision；沿既有渲染线程任务队列取完成帧，再后台写staging。timeline clip、source video、standalone image路径分别明确，不从当前预览label推断source帧。原播放暂停／恢复使用request token和“用户之后未改变播放状态”的条件，不用无条件100ms timer在用户已pause／离页后重新起播。取消与工程关闭使token失效，已读回结果清理staging不覆盖原bundle。完成后仅增量刷新相应album，若用户已换album不抢回选择。

**门槛**：播放时换clip／调grade／切工程／取消／关闭窗口后，不抓到上一帧或错误grade；结果metadata与像素有一致provenance；拒绝无文件；失败保留原still；图像模式Space平移和视频播放不被Gallery焦点干扰。

### G07：应用、分类应用、参考与Color Match

**输入→输出**：选中still ID、目标上下文revision、动作/类别→目标服务单次提交或纯监看会话，返回真实结果。

**步骤**：复用SaveRouter读取与`_prepare_preset_layers`，声明AI／资源／plugin可移植范围，缺插件不静默丢功能。timeline clip的完整和分类应用核对并统一到权威`_paste_grade_to_clip`／现有command入口；standalone保留history单步。不能只更新engine.layers而让project payload、revision、预览和export分叉。分类选择取消零写入；用户Copy Grade的全局clipboard不被临时Gallery应用覆盖。

参考走`load_still_best→engine.load_reference_still→Canvas`；它不修改工程grade。Color Match仍先给建议，异步统计完成后复核原目标，用户选择再提交。多选apply若启用，先列出明确目标／作用范围，使用事务或逐项结果，不悄悄应用到所有timeline clip。

**门槛**：timeline apply后undo/redo、重新打开工程、export与当前预览一致；分类只改所选域；reference不dirty工程；Match取消不变更；目标已删／revision变化拒绝而不改新目标；中键／按钮／菜单同动作同结果。

### G08：导入／导出与依赖可移植性

**已有**：只有Export Thumbnail；`.topos`读写／资源解析已有基础，不能给thumbnail copy改标签就宣称Export Still或Export Grade已实现。

**输入→输出**：用户选文件／格式／目标album，或选中still及导出profile→经校验的bundle导入结果／可移植包／明确图像导出文件。

**步骤**：分清“导入图像为参考”“导入grade bundle”“导出参考图”“导出完整grade bundle”“导出thumbnail”。复用`src/shared/project/package_spec.py`、`save_router.py`、`topos_project_read_service.py`、`resource_resolver.py`和`dependency_audit.py`检查schema／资源；不携带secret和不可移植机器绝对路径。zip读取有文件数／解压大小限制、路径归属校验、拒绝目录穿越和symlink逃逸；不执行内含脚本／插件。

选择覆盖／重名分配必须具体可审阅；复制保留来源身份并处理本库stable ID冲突。导入staging通过检查后一次提交；导出不修改源bundle。Resolve DRX／DPX兼容须有独立格式适配和fixture，未完成前禁用且说明，不猜第三方格式。

**门槛**：跨目录／跨机器重新导入能恢复grade和参考；缺依赖逐项诊断；恶意／损坏／未来schema包拒绝不落散文件；HDR EXR和alpha声明完整；只JPEG导出不称HDR；取消不创建半目标。

### G09：HDR、alpha与显示上下文

**输入→输出**：float still／EXR声明、source/working/display tag、thumbnail显示目标→有明确精度／alpha／ODT来源的参考和预览。

**步骤**：复用已有float32 EXR、PIZ编码和`load_still_best`优先级；缩略图可为显示编码JPEG，但不作为比较master。保存工作空间／显示变换与frame provenance，读参考时明确统一比较域，避免双ODT。四通道使用`still_alpha_declaration/align_alpha_domain`，undeclared不能默认猜straight或premultiplied。元数据的still_precision应与文件实际成功状态一致，而非根据“本次尝试过EXR”标HDR。

**门槛**：超1.0、负值、PQ/HLG、SDR、透明边缘／alpha0像素与已有HDR合约一致；EXR失败以JPEG回退必须可见；rename／export／import后alpha声明和precision不丢。禁止为Gallery预览无理由clamp内部HDR值。

### G10：旧库与脚本格式统一

**输入→输出**：旧cwd目录／AppData User/System平铺库／脚本子目录格式→可读取legacy entry与可恢复迁移结果。

**步骤**：保留当前复制不删来源的迁移原则；新service识别`.topos`及`grade.json＋thumbnail.jpg`来源并区分能力，不把旧脚本目录误当普通album后一直空列表。脚本`capture_still`改调用同一capture service，保持返回值兼容或显式API版本；`apply_preset`核对其路径协议与新身份解析。迁移前盘点／冲突预览，写transaction journal／mapping，确认成功后再切索引；不自动删除原始文件。

**门槛**：未知payload／plugins保留；断电／取消可重跑且无重复；旧库不因新sidebar消失；脚本抓帧后Gallery与脚本apply都能找到同一资源；只读来源可查询不能误写。

### G11：显示偏好与外部变化

**输入→输出**：grid/list、列数、sort/search、sidebar展开／宽度、album／selection稳定ID、外部rename/capture→用户级版本化偏好和一致视图。

**步骤**：为新像素缩略图宽度制定版本化用户偏好段；旧`presets_columns`只能迁移为合理默认，不能把列数2读成缩略图2px。保存size/grid-list/sidebar宽度及sort，写盘debounce；不把QPixmap／缓存／绝对机器路径放进工程。Grid/List切换只换presentation，当前选中／过滤保持；外部capture、脚本、文件变化按revision失效索引，优先局部更新，不每帧轮询磁盘。保留`dockPresets`和Float/Dock／旧布局，窄屏sidebar可折叠但选中album不丢。

**门槛**：重启／浮动／grid-list往返不换资源身份；非法偏好回退不清其他面板配置；外部删除时selection清理且旧QAction不能再操作错误文件；当前读到的文件版本变更时提示刷新／拒绝陈旧写入。

### G12：验收矩阵与证据边界

交付报告保存到`docs/validation/`，写明commit、平台、GPU、测试命令、pass/skip/fail、库规模、真实素材和限制；没有真机HDR／4K的项明确未测，不用offscreen结果替代。

| 验证组 | 复用已有测试／真实路径 | 新增门槛 |
|---|---|---|
| 元数据／sidecar | `tests/color/test_presets_gallery.py` | 新schema旧库读取、still ID稳定、Label/Timeline独立、unknown字段保留、alpha声明bundle |
| Grid／List | `tests/ui/test_column_grid_panels.py` | 16:9+caption取代旧square断言；列数flush-right、list暂停grid、selection角色兼容、窄屏和HiDPI |
| 原宿主动作 | `tests/color/test_presets_gallery.py`、`tests/project/test_save_router.py` | capture失败／取消、目标revision、全量及分类apply的工程/undo/export一致 |
| Reference／HDR | `tests/color/test_ab_reference_wipe.py`、`tests/render/test_hdr_gpu_contract.py` | EXR优先、JPEG降级可见、透明alpha声明、ODT只应用一次 |
| 脚本／兼容 | `tests/color/test_color_tools_scripting_api.py` | capture/apply同一bundle、旧目录适配、参数名不允许路径注入 |
| 包／事务 | 后续新增repository/service focusedtest | 逐sidecar失败／rollback、只读／磁盘满、冲突、symlink/zip越界、未来schema、取消 |
| 大库／生命周期 | 后续真实机smoke | 1000/10000条查询／滚动、快速切scope、并发capture、关闭／换工程、内存上限和无UI阻塞 |

基础命令示例（不是已运行结果）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/color/test_presets_gallery.py tests/ui/test_column_grid_panels.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/project/test_save_router.py tests/color/test_ab_reference_wipe.py tests/color/test_color_tools_scripting_api.py -q
.venv/bin/python -m pytest tests/render/test_hdr_gpu_contract.py -q
```

测试必须用tmp root／隔离QSettings，不扫描或删除真实用户Gallery。文件写入失败、取消和兼容路径是事务正确性测试，不能只断言方法被调用。前端视觉验收另做28px工具条、可折叠sidebar、16:9缩略图／caption、三列表、hover/selection、搜索排序和浮动窄屏截图。

### G13：画廊静帧与视图划像／分屏联动（用户追加）

**本轮UI已提供**：Gallery右键“加载为划像参考”、更多菜单“划像／分屏”；Color Viewport的分屏图标打开GalleryComparison设置，更多菜单可选择Gallery划像参考。设置窗口复用真实Gallery条目，划像为单选、分屏为多选，提供并排／2×2布局与缩略图布局示意。Current Image为示意占位，**不是实际处理后的画面**。开始分屏保持禁用，原因可见；划像确认发原`reference_requested(str)`，走既有`on_preset_reference_requested`，不新增解码或GPU调用。选择／切布局不修改grade、不dirty工程。

**现有可复用链**：`PresetsPanel.reference_requested → LayerOpsMixin.on_preset_reference_requested → load_still_best → ImageEngine.load_reference_still → Canvas._sync_reference_menu_states`，保持EXR优先和JPEG精度诊断；现有split是单张参考wipe，不能当多图分屏。`src/features/color/core/compare_session.py`已提供CompareItem／ColorCompareSession的身份、缓存／释放域件，但**没有网格presenter或真实renderer消费者**。

**拟新增合同**：`GalleryCompareRequest{request_id, context_revision, mode(wipe/split), current_source_context, reference_still_ids, layout, fit, compare_color_context}`；`GalleryCompareResult{accepted, actual_mode, resolved_references, precision, reason, generation}`。统一View和Gallery两入口；GUI条目传路径的兼容adapter先解析稳定still身份，不能永久以row或当前文件名作键。

**实施步骤**：

1. 参考会话记录still ID／bundle revision／实际精度，load失败保留上一张有效参考，不先改checked。画廊切选择仅改selection；显式Use as Reference才改监看，成功回填来源caption、当前模式和退出入口。失联／被删静帧有明确处理，避免保留已复用路径的旧图。
2. 划像复用现有引擎分割位置与拖动事件；定义横／竖方向、swap当前／参考、reset位置和A/B切换政策。新动作要有真实服务才启用，不把仅按钮checked冒充画面变化。点击划像toggle仍保持原快捷键／引擎连接。
3. 分屏按当前画面＋1参考=并排、当前画面＋最多3参考=2×2；多选超过上限、布局不兼容、缺失图像先拒绝且无资源泄漏。当前画面的source/local/timeline帧与grade revision一次锁定，切clip／换工程使旧请求失效。
4. 经现有render线程／队列为每格生成真实快照／纹理，接独立grid presenter；禁止UI paint做读回／上传，不以JPEG thumbnail冒充比较master。首版参考静帧不启动额外视频时钟；每格分辨率依据pane和DPR确定，总VRAM／cache有上限，超预算有诊断而非静默换图。
5. 用ColorCompareSession管理grid生命周期与invalidations；保存进入前wipe/reference/position/zoom状态，退出split原样恢复。关闭、取消、晚到、替换布局、删除still、关工程时释放每格资源，走原deferred-release，不能在GUI线程直接release GL。
6. 统一工作色域／ODT比较域，EXR/HDR值保留；旧JPEG允许明示SDR低精度，不重复ODT，不通过clamp让参考“看着一致”。有alpha须遵循现有声明／premultiply域。
7. 完成真实handler与状态回填后才启用设置窗口Start Comparison；`split_requested`现为预留UI协议，宿主需连接、接受／拒绝并回填，不能只把disabled改True。同步Color capability registry到真实presenter in-flight/ready/failed状态，处理两入口同时操作。

**门槛**：Gallery与Viewport入口选择同一组still得到相同结果；current+1／current+3、混合宽高比／HDR／JPEG／alpha、布局切换、wipe↔split↔A/B往返不改工程grade或Undo；快速换clip／删still／换工程／关闭过程中旧结果不覆盖当前画面；原wipe退出恢复精确位置／参考；实测CPU/GPU队列、VRAM和释放计数。测试只有布局示意时必须标“UI”，不能记录真实分屏通过。

## 6. 后端完成定义

逐批标记“域件完成／宿主真实接线完成／媒体与文件验收完成”；所有enabled动作有实际handler和真实结果回填。保留旧入口不意味着新scope／import/export能力完成。Memories、未设计DRX兼容或未执行真机项继续明确禁用／未测。

遵循`.clinerules`：工程变更经command/service；UI不直接解码或操作GPU；GPU结果按原线程／deferred-release规则清理；新函数完整类型注解；程序回填锁；新schema有迁移并保留未知数据；不以UI单层改动掩盖工程／预览／导出不一致。
