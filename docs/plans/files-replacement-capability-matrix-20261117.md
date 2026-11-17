# Files 替代能力矩阵与去向（A 阶段冻结基线）

日期：2026-11-17。基线提交：`a36b96cf`。本文件是[最终替代与升级总计划](2026-11-17-files-final-replacement-and-upgrade-plan.md) A 阶段第 1、2 项的交付物：旧入口 → 能力 → 新服务 → 新UI → 数据实体 → 测试 → 去向。

清点方法：通读 `external/file_explorer/routes.py`（96 条路由）、`plugins/`（12 条 Blueprint 路由 + 无路由插件）、`templates/` + `static/`（旧 Web 前端能力）、`src/` 生产调用方（`native_files_transport.py`、`files_panel.py`、adapter、preferences、`src/main.py`）。行号以 `a36b96cf` 为准。关键锚点（DB/CWD、manifest 声明、任务选项）已人工复核。

**口径**：本矩阵登记现状与去向，不表示右侧任何"新"列已实现。去向指向总计划的阶段（B/C/D/E/F/G/H/I/U）；标 **待决定** 的条目在用户给出范围决定前保持阻塞，不得静默删除，也不得默认实现。

---

## 1. 必须保留能力（总计划 §2 承诺清单）→ 阶段映射

"原生现状"指 `a36b96cf` 时原生 Files（`src/app/ui/native_files_panel.py` + transport）相对旧 Web UI 的等价度：**已有** / **部分**（缺什么写在缺口列）/ **无**。

| # | 能力 | 旧入口 | 原生现状（缺口） | 新服务 | 新UI | 数据实体（新schema） | 冻结/现有测试 | 去向 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 目录树/导航/面包屑 | `/api/tree`、`/api/list`、`/api/drives` | 部分（缺同级目录下拉、盘符列表） | FileQueryService + TreeService（C） | D 面板导航 | assets/library 注册表 | `test_replacement_baseline_freeze.py`（浏览） | C/D |
| 2 | 四布局浏览（自适应/瀑布流/网格/列表）+ 密度 | 前端 view_mode | 已有 | FileQueryService（C） | 已有，补递归聚合开关（见 §4） | 同上 | 既有 `tests/ui/test_native_files_*` | C/D |
| 3 | 搜索：目录内过滤 + 全库 FTS（名/标签/备注/OCR） | 前端过滤 + `/api/search` | 部分（缺历史下拉、存为智能文件夹） | SearchService（C） | D 搜索栏增强 | assets + FTS | freeze（scan→search）+ 既有 query 回归 | C/D |
| 4 | 筛选：类型/旗标/评分/颜色/日期/时长 | 前端 + `/api/list` 字段 | 部分（有类型/旗标；缺评分/颜色/日期/时长筛选） | FileQueryService 谓词（C） | D 筛选器 | assets 列 | freeze（list 技术字段） | C/D |
| 5 | 排序（名/日期/大小/评分/扩展名/升降） | `/api/list` 参数 | 部分（缺扩展名项、图标视图无升降切换） | FileQueryService（C） | D 排序控件 | — | 既有 query 回归 | C/D |
| 6 | 选择/多选/拖放（DragOnly）+ 复制粘贴 | 前端 + `/api/action/*` | 已有（tasks copy） | FileTaskService（B） | 已有 | job_log + assets | freeze（任务通道） | B |
| 7 | 标签（平铺 chips + 补全） | `/api/meta/tags`、`/api/tags` | 部分（单行逗号编辑；缺补全） | OrganizationService（B/C） | D 检查器增强 | tags + asset_tags | freeze（update_meta） | B/C/D |
| 8 | 评分/颜色/旗标/备注编辑 | `/api/update_meta` | 部分（旗标只能筛不能改） | MetadataService（C） | D 检查器补旗标 | assets 用户列 | freeze（update_meta+DB 断言） | C/D |
| 9 | 集合基本组织（建/改名/删/增删成员/浏览） | `/api/collections*` | 部分（只读浏览） | CollectionsService（C） | D 集合面板 | collections + collection_items | freeze（collections 读）+ B 新写路径测试 | B/C/D |
| 10 | 图片/视频/音频预览（灯箱/播放器/胶片条/波形回落） | `/api/file`、`/api/thumb*`、`/api/play_native` | 部分（缺 Markdown 渲染、图片缩放平移、悬停波形） | PreviewService + 共享播放 runtime（E） | D 预览层 | 缩略图缓存（新缓存目录） | 既有 player host/播放语义回归 | D/E |
| 11 | 文件任务：mkdir/rename/copy/move + 批改名（模板模式） | `/api/tasks/start`、`/api/batch/rename*` | 已有（模板批改名；旧另有"替换/序号"两模式） | FileTaskService 安全事务（B） | D 任务列表（逐项进度/取消/重试） | job_log | freeze（rename/move/trash/restore）；B 补失败矩阵 | B/D |
| 12 | 回收站：入站/浏览/恢复/清空（30 天） | `/api/trash*`、tasks trash/restore/purge | 部分（仅"移入回收站"入口；无浏览/恢复/清空 UI） | TrashService 版本化清单（B） | D 回收站视图 | trash_items + trash/<lib>/ 实体 | freeze（删→恢复含元数据快照） | B/D |
| 13 | 索引扫描（手动/自动/状态） | `/api/scan/*` | 已有 | ScanService（C） | 已有 | assets 技术列 | freeze（scan→file_meta 行） | C |
| 14 | 库固定/取消固定（多根） | `/api/library/*` | 已有 | LibraryRegistry（B） | 已有 | libraries 注册表 | 既有 native files 回归 | B |
| 15 | XMP 写回（sidecar/inplace/off 策略） | `/api/config(meta_embed)` + meta_sync | 隐式（随 update_meta 生效；无模式切换 UI） | MetadataService（C，策略显式化） | D 偏好项 | 素材旁 .xmp（用户数据，不进新库目录） | freeze（sidecar 模式断言） | C |
| 16 | 深度元数据（EXIF 相机块/ffprobe 编码块） | `/api/metadata/detail` | 部分（检查器只有 list 技术字段） | MetadataService（C）+ 格式 provider（E/F） | D 信息面板 | assets 技术列 + 按需探测缓存 | freeze（list 技术字段） | C/E |
| 17 | 缩略图（三档/胶片条/失败重试） | `/api/thumb*` | 已有（重试有入口；缺缓存统计/清理 UI） | ThumbnailService（C）+ provider（E） | D 维护入口 | 新缓存目录（可再生，不入素材目录） | 既有 thumb 相关回归 | C/E |
| 18 | 必要库维护（缓存清理/失联诊断） | `/api/cache/*`、missing 标记 | 部分 | MaintenanceService（C） | D 维护面板 | — | freeze（missing 标记） | C/D |
| 19 | 内建工具实际能力（见 §3 十插件表） | `/api/plugin/*/launch` + 兼容入口 | 部分（四工具经 launcher；六个兼容入口切回旧页） | G 阶段逐插件接管 | G | 插件状态/安装根（F） | 既有 launch service 回归 | F/G |

## 2. 旧有但**不在必须保留清单**的能力（登记去向，多数待决定）

总计划 §2 的必须保留清单未包含以下能力。按计划规则：需要保留的接服务/独立工具；不保留的须用户明确决定。默认建议已给出，**未获决定前一律阻塞**。

| 旧能力 | 旧入口 | 实际行为 | 分类 | 默认建议（待用户确认） |
| --- | --- | --- | --- | --- |
| 时间线视图（按月分组） | 前端 ren:246 | 纯呈现层 | 呈现扩展 | 待决定；若保留归 D |
| 子目录递归聚合浏览 depth=2 | `/api/list?recursive=1` | 聚合子目录条目并标来源 | 呈现扩展 | 待决定；若保留归 C/D |
| 智能文件夹（规则构建器 CRUD） | `/api/smart_folders*` | DB smart_folders + 规则查询 | 组织扩展 | 待决定；建议以"保存的查询"进 B/C 新模型（与集合区分） |
| 层级标签树（拖拽父子/防环） | `/api/tags*` PUT | tags.parent_id + legacy 串重算 | 组织扩展 | 待决定；新 tags 表保留 parent_id（A-3 已含），UI 归 D 可选 |
| 堆叠 stacks | `/api/stacks*` | DB stacks + 封面/角标 | 组织扩展 | 待决定；若保留归 B/C/D |
| 批量旋转 90°（重编码原图） | `/api/batch/rotate` | PIL 重写源文件 | 破坏性工具 | 待决定；若保留须走 B 安全事务+新文件确认 |
| 批改名"替换/序号"两模式 | `/api/batch/rename` | FS+DB 批量 | 必须保留的扩展 | 建议并入 D 批改名（模式补齐），随 B 预览/执行协议 |
| 拖拽上传到当前目录 | `/api/upload` | FS 写 + after_upload 钩子 | 输入通道 | 待决定；若保留归 D（经 C 服务校验） |
| 会话撤销 Ctrl+Z（改名/删除） | 前端 undo 栈 | 前端记录 + 逆向调用 | 安全扩展 | 建议由 B 的 job_log/恢复能力在 D 提供统一"最近操作"入口 |
| 文件夹软锁（PBKDF2） | `/api/locks*` | DB locks + 会话解锁 | 安全功能 | **待决定**（保留/放弃均需明确） |
| XMP 反向导入 | `/api/meta/import_xmp` | 读文件 XMP 回填 DB | 元数据工程 | 建议并入 C MetadataService（与 15 对称） |
| 元数据 JSON 导出/导入 | `/api/meta/export`、`/api/meta/import` | 全量 JSON 附件 | 备份通道 | **由 A-5 新备份/恢复替代**（版本化、含校验），旧 JSON 格式不迁移 |
| 失联元数据重链 | `/api/meta/scan_orphans`、`/api/meta/relink` | 同名唯一候选改写 path | 元数据工程 | 建议并入 B（回收站/移动共用身份重映射）+ C 维护面板 |
| 导入向导（日期归档/模板/查重/删源） | `/api/import/run` | copy2×N + DB + 可选 send2trash | 独立工具 | 待决定；若保留归 C/D 新服务（删源必须走回收站语义） |
| 导出图片+水印+预设 | `/api/export/*` | PIL 生成新图 + 预设 DB | 独立工具 | 待决定；若保留与 G image_factory 合并设计 |
| 重复文件查找（体积→MD5） | `/api/duplicates/scan` | 只读扫描 + 手动入回收站 | 可选后端 | 待决定；计划 §4 归 U4 |
| 相似查找（pHash）+ 主色桶筛选 | `/api/similar`、dom_bucket | 索引期算 pHash/dom_color 回写 | 可选后端 | 待决定；扫描成本高，归 U4；新 schema 不预置（见 A-3） |
| OCR（单图+索引期自动） | `/api/ocr/*` | ocr_engine 写 ocr_text+FTS | 可选后端 | 待决定；归 U4；`ocr_enabled` 默认关 |
| 地图视图（Leaflet+GPS） | `/api/map/files` | 前端 Leaflet | 可选后端 | 待决定；WebEngine 依赖重，归 U4 |
| 幻灯片+BGM | 前端 toggleSlideshow | 间隔切换 + 循环播首音频 | 呈现扩展 | 待决定；若保留归 D |
| 库统计仪表盘 | `/api/stats` | 聚合读 | 维护 | 建议并入 C MaintenanceService/D 维护面板 |
| 视频打点 markers / 片段导出 | `/api/markers*` | **无任何调用方**（前后端均未接 UI） | 死代码 | 不迁移；如需要属 U4 |
| 主题切换（FE 内部） | `/api/config(theme)` | 旧页内主题 | 已被应用全局样式替代 | 不迁移 |
| 播放器截帧为封面 | `/api/action/snapshot` | bridge 信号触发宿主截图 | 呈现扩展 | 待决定；若保留归 E/D |

## 3. 十个内建插件（G 阶段接管对象；事实登记）

接管方式以总计划 G 表为准；本节登记 `a36b96cf` 的实际行为事实（G 重写时必须对齐/修正）：

| 插件 | 实际能力（事实） | 关键副作用 | 依赖 | 新实现必须修正的点 |
| --- | --- | --- | --- | --- |
| format_factory | ProRes 批转码（videotoolbox hwaccel）+ 封装/编码转换 + 字幕轨提取；拖入目录递归探测 | 写转码产物；可选把源文件移入废纸篓（osascript） | ffmpeg/ffprobe、psutil | manifest 谎报 `write_semantics:"read"`（实际写盘）——G 新 manifest 必须如实声明；单选 payload 与多文件队列不匹配 |
| image_factory | 目录/文件批转换（PIL，8 格式，质量/边长缩放，ProcessPoolExecutor）+ 扁平化归档 | 写产物；勾选时 **os.remove 永久删源**（非回收站）；rmdir 空目录 | Pillow | 同上 manifest 谎报；永久删源必须改回收站/明确确认语义 |
| shutdown_timer | 倒计时关机（osascript 提权 / shutdown / pkexec）+ 托盘 + 到期补发 | **系统级关机命令** | OS 命令 | G：系统动作须显式意图+取消无遗留（现已有宿主退出兜底取消） |
| decode_benchmark | 用 SourceManager 真实解码测速（master/proxy/seek 定步长）；edit 模式经宿主 Edit host service | 只读媒体；导出报告文件 | PyAV、宿主渲染器 | G 表要求直连 Edit host service——现 `main_window.py:1290-1306` 注册服务模式可沿用 |
| code_editor | Monaco 多标签编辑 13 种扩展名 | 读写**任意路径**文本（无软锁校验） | 内置 Monaco（113 文件） | G：受控宿主/等价编辑工具，保存须走安全写入 |
| image_editor | Filerobot iframe 编辑（裁剪/调色） | **覆写原图**（base64 直写） | filerobot 库 | G：默认安全输出+写入事务，不能照搬覆写原图 |
| ai_library_browser | 转发 search/status/stats 到 vault 服务(:8288) | 无本地写 | topos-vault | G：复用 shared vault client，不经旧插件 HTTP |
| audio_pro | WaveSurfer 波形/频谱/选区循环；截取经宿主 `/api/audio/trim` | 截取在源旁写 `*_trim_*` 新文件 | WaveSurfer.js、宿主 ffmpeg | G：原生 audio viewer/inspector；截取安全输出；Edit DSP 已吸收其算法（audio_dsp.py:3070） |
| video_pro | 无路由；注册 .topos 缩略图/元数据 provider；真身已在 `src/shared/playback/` | 写缩略图缓存 | Qt Multimedia、PyAV、libtopos_codec | G：替换 window_ready hook；播放核心已共享，主要是 provider 注册进 F |
| topos_image | 无路由；.toos 解码/元数据/三档 WEBP 缩略图（ctypes→libtopos_codec） | 写缩略图缓存 | libtopos_codec、numpy/PIL | G：provider 进 F；懒加载 codec 模式可沿用（无 dylib 不拖垮启动） |

## 4. 路由级去向

### 4.1 原生仍调用的 25 条 routes.py 路由（替代期间的生命线，B/C/D 替换后删除）

`/api/list`、`/api/tree`、`/api/thumb`、`/api/thumb/strip`、`/api/thumb/retry`、`/api/file`、`/api/search`、`/api/capabilities`、`/api/health`、`/api/scan/start|cancel|status`、`/api/tasks/start|status|cancel`、`/api/batch/rename|rename/preview`、`/api/batch/meta`、`/api/update_meta`、`/api/collections`、`/api/collections/<cid>/items`、`/api/library/list|add|unpin`、`/api/cache/invalidate`（files_panel:678）+ 4 条插件 launch。去向：逐条映射到 §1 表的新服务；`/api/cache/invalidate` 的宿主评分联动（files_panel→FE）由 C 的事件/revision 机制替代。

### 4.2 仅旧 Web UI 使用的 71 条

按 §1/§2 的能力行映射，无独立路由级去向。其中写副作用组（rename/delete/mkdir/trash 三件套/batch move|rotate/upload/audio trim/import run/export run/meta import/relink/locks/tags CRUD/smart_folders CRUD/stacks/collections 写/meta tags）在新系统一律收敛到 B/C 的领域服务，不再恢复同名 HTTP 端点。

### 4.3 无任何调用方的 5 条（死代码，不迁移）

`/api/markers`、`/api/markers/export`（routes.py:3594 自述"打点 UI 待接入"）、`/api/tasks/list`、`/api/library/backup`、`/api/plugin/decode_benchmark/capabilities`。

## 5. 主窗桥接（Python 级，非 HTTP）

| 桥接 | 位置 | 作用 | 去向 |
| --- | --- | --- | --- |
| adapter sys.path 注入 + `_find_project_root` | `src/app/integrations/file_explorer/adapter.py:18-38` | 定位旧扩展根；无旧目录不能启动 | D/I：FilesPanel 独立组装后删除 |
| `ensure_runtime()` 起全套旧服务 | adapter.py:296-303 → bridge.py:183 | 端口探测、插件加载、init_db、缩略图 worker、trash purge、autoscan、watcher | D：原生默认不启动；I 删除 |
| `bridge` 信号 9 条 | bridge.py:132-140 | 宿主只消费 add_library/open_folder_dialog/play_video 三条（files_panel:170-172） | C/D：同名意图改走 facade/事件 |
| `register_edit_benchmark_service` | bridge.py:150；main_window.py:1298 | 向旧扩展注入 Edit 跑分服务 | G：decode_benchmark 直连 host service 后删除 |
| `shutdown_thumbnail_worker` | main.py:41-47；src/main.py:156 | 退出时停缩略图 worker | C：新 ThumbnailService 自管生命周期 |
| `load_config/save_config/plugin_manager` 直用 | files_panel.py:21-22、526；preferences_dialog.py:1701 | 启停插件/读写旧 config | F：新 Inventory + 新偏好；I 删除 |
| `.port_config.json` 读写 | adapter.py:159/236-242 | 记录旧服务端口 | I 删除 |

## 6. 新系统明确声明的行为变化（替代后与旧系统的差异，验收时按此口径）

1. **缩略图/缓存不再写进素材源目录**：旧 thumb_engine 优先写 `<素材目录>/.cache/*.webp`（thumb_engine.py:135-137）。新系统只写自有缓存目录（A-3）；源目录保持只读（用户显式操作除外）。已有 `.cache/` 不清理、不迁移。
2. **永久删除收紧**：image_factory 的 `os.remove` 删源、audio trim 覆盖式输出等破坏性路径，新实现一律默认安全输出/回收站语义（G）。
3. **旧 JSON 元数据导出格式退役**：备份/恢复改用 A-5 的版本化格式；不提供旧 JSON 导入兼容。
4. **旧库/config 不作为输入**：topos.db（FE 表）、metadata.db、旧 config.json、QSettings FilesPanel 旧键不读取、不合并（H 阶段显式重置）。
5. **manifest 声明必须与实际写语义一致**：G/F 阶段所有新 manifest 按实际行为声明 write_semantics，安装侧（PR-01 已有 provenance）不再接受自我声明提升信任。
6. **无调用方能力不复活**：§4.3 死代码与 §2 中用户未决定保留的能力，不因代码存在而自动进入新系统。
