# Files 新数据目录、schema 与缓存边界（A 阶段交付）

日期：2026-11-17。基线 `a36b96cf`。本文是[总计划](2026-11-17-files-final-replacement-and-upgrade-plan.md) A 阶段第 3 项交付物：定义新系统的数据目录、schema、插件状态与缓存边界，并列出旧开发数据的可清空位置。按计划范围修正：**不建立旧库迁移桥，旧数据不作为新系统输入**。

实现归属：目录定位器与 schema 由 B 阶段落地（`FilesDataLocator` + repository）；本文是边界契约，B 偏离本文须先修订本文。

## 1. 旧落盘现状（事实，作为对照）

| 旧位置（相对仓库根） | 内容 | 性质 |
| --- | --- | --- |
| `topos.db`（+`-wal/-shm`） | 宿主 settings/layouts 与 FE 全部元数据共用库；FE 的 db.py:20 一旦存在恒用它 | 开发库数据 |
| `src/topos.db` | CWD 分叉历史产物（gitignore 显式列出） | 待清理遗留 |
| `external/file_explorer/metadata.db`（`.migrated-*`） | FE 回退库/合并留档（当前不存在） | 旧开发数据 |
| `external/file_explorer/config.json` | FE UI 状态 + libraries 注册 + meta_embed/ocr_enabled/disabled_plugins | 开发配置 |
| `external/file_explorer/.port_config.json` | 运行时端口 | 可再生 |
| `external/file_explorer/.trash/` | 应用内回收站文件体 | 30 天保留数据 |
| `external/file_explorer/.assetdeck/thumbnails/` | 全局缩略图兜底缓存 | 可再生 |
| `.topos/`（config.json、cache/、deliver_export_queue.json） | src 应用配置/缓存 | 开发配置（保留，新系统复用其根定位） |
| `cache/`、`.images_cache/`、`src/.images_cache/` | 运行时缓存 | 可再生 |
| （仓库外）`<素材库>/.cache/*.webp` + `.nomedia` | 跟随源目录的缩略图 | 可再生但**在用户卷上**，新系统不清理 |
| （仓库外）`<素材库>/*.xmp` | meta_sync 写回的用户标记 | **用户数据**，任何重置不得触碰 |

旧锚定缺陷（新设计必须消除）：FE 的 DB/config/trash 以 `external/file_explorer/` 自身 `__file__` 锚定（db.py:15-34、trash.py:32-35）；宿主 `src/shared/db/database.py:40` 默认 `db_path="topos.db"` 按进程 CWD 解析（无参实例化点：main_window.py:407、library_panel.py:143/490/566）；缩略图 worker 向**素材源目录**写 `.cache/`。

## 2. 新数据目录（唯一边界）

**定位器**：`FilesDataLocator`，根目录 = 现有应用配置根（`TOPOS_CONFIG_PATH` 环境变量 → `.topos/`，见 src/main.py:60-67、config_manager.py:320-329）之下的 `files-data/`。所有路径由定位器唯一解析，**禁止相对路径/CWD 解析**；仓库内开发与打包后行为一致。

```
<config_root>/files-data/
├── catalog.db                 # 唯一新库（多库共用，library_id 分键空间）
├── schema_version             # 人读 + 程序读双保险的版本钉
├── libraries.json             # 库注册表（id、根路径、显示名、创建时间）
├── trash/<library_id>/        # 每库回收站文件体（跨卷 send2trash 回退，如实标注）
├── cache/
│   ├── thumbnails/thumbnails-v1.db  # 各显示尺寸共用的缩略图 PNG；512 MiB 负载上限
│   ├── strips/                # 胶片条
│   └── probe/                 # 深度元数据探测缓存（按 mtime+size 失效）
├── plugins/
│   ├── install/               # 安装根（PR-01 所有权/事务规则，域前缀 files.*）
│   ├── state.json             # desired_enabled 等插件状态（Inventory 拥有）
│   └── logs/                  # 有界作业/贡献日志（U3 预算）
└── backups/                   # A-5 备份产物默认目录
```

**边界规则**：

1. `files-data/` 之外，新系统只写两类位置：用户显式文件操作的目标目录（经 B 安全事务）；用户开启 XMP 写回策略时的素材旁 `.xmp`（政策沿袭 sidecar/inplace/off，默认 off）。
2. 素材源目录**不写缓存**（行为变化声明见矩阵 §6.1）。
3. `catalog.db`/`libraries.json`/`state.json` 写入一律原子（temp + os.replace，沿用 save_config 的 P2-12 模式）；DB 用 WAL，退出检查点。
4. 缓存目录可整目录删除而系统自愈；`trash/`、`backups/` 不是缓存，重置命令单列确认。
5. 路径含 NFC 归一（沿用旧库经验）；库根切换 = 切 `library_id` 上下文，所有实体行携带库 id，杜绝跨库串写。

左侧「素材库」读取既有注册表。每个库的目录树按需读取真实子目录，登记重叠目录时，各树分支保持独立，按路径共享后台读取。子文件夹由 Files 文件任务创建与管理。新建素材库先在已有的存放位置创建单级文件夹，再登记根路径。右键库可按库执行 AI 搜索和分析。重命名更新显示名称并保留库 ID，移除解除注册。全库搜索和 AI 搜索均以当前注册表为范围。「磁盘」和 Home 继续提供直接浏览。

## 3. 新 schema（B 阶段实现的第一版边界）

表名与旧库**不同名**（防止旧工具误碰）；不迁移旧行。

| 表 | 关键列 | 说明 |
| --- | --- | --- |
| `assets` | (library_id, path) PK；用户列 tags 冗余串**取消**，rating/color/flag/comment；技术列 mtime/size/date_taken/width/height/duration/codec/camera/lens/iso/aperture/shutter/focal/gps/dom_color/missing/indexed_at | 唯一资产身份 = (library_id, NFC path)。**不预置 phash/dom_bucket 索引列**（相似/主色属 U4，届时独立扩展表，不绑架扫描成本） |
| `tags` / `asset_tags` | (id, name, color, parent_id) / (library_id, path, tag_id) | 层级保留 parent_id（矩阵 §2 层级标签树待决定，schema 先不关门）；防环由服务层保证 |
| `collections` / `collection_items` | (id, library_id, name, color, created_at) / (collection_id, asset_id, sort_order) | 成员引用资产身份而非裸 path |
| `saved_queries` | (id, library_id, name, rules JSON, created_at) | 承接智能文件夹（若用户决定保留） |
| `stacks` | (id, library_id, cover_asset, created_at) + 成员表 | 同上待决定，schema 预留 |
| `trash_items` | (id, library_id, original_path, item_name, is_dir, trashed_name, deleted_at, expire_at, **manifest JSON**（全列快照+标签+集合成员关系+关系重映射记录）) | 版本化完整清单；快照失败禁止删源（B 验收） |
| `job_log` | (id, library_id, op, args JSON, idempotency_key, state, items JSON, created_at, updated_at) | B 的持久操作日志：文件+DB 非原子时的补偿/待恢复依据；有界保留 |
| `settings` | (library_id, key, value) | 新 Files 偏好库内部分（UI 偏好仍归应用 QSettings/新偏好层，F 阶段接线） |
| `assets_fts` | FTS5(name, comment, tag_names, ocr) | trigram 沿用；ocr 列预留（U4 决定后启用） |

**初始化规则**：首次创建建全套表 + `schema_version`；重复启动幂等（版本不匹配即拒绝打开并给出明确诊断，不做隐式迁移——开发阶段破坏性升级授权下，升版本走"备份→重置→重建"）。

**库切换/多根**：`libraries.json` 注册多根；浏览任何未注册目录不隐式建库（只读浏览可用，写入组织数据要求先固定为库——与现有"固定库"语义对齐）。

### AI 素材索引

AI 素材扫描读取已注册的目录，原件留在各自磁盘上。普通目录浏览仍按磁盘内容工作。AI 索引随 `catalog.db` 备份，独立表的版本记在 `settings.ai_library_schema`；首次使用在事务中追加表和 FTS 索引，已有 Files 资料、目录表和 catalog 主版本均保留。

- `ai_library_assets` 按媒体类型及文件内容识别素材。重新索引内容相同的文件时复用素材 ID。
- `ai_library_locations` 保存每个素材已登记的路径、文件状态和离线信息。搜索可以返回离线匹配，只有签名仍一致的路径才标为可用。
- `ai_library_evidence` 保存模型版本对应的文字、标签与源时间范围，通过 FTS 检索；人工备注和标签仍归 `assets`、`asset_tags`。取消或推理失败会保留已完成的分析，可重新扫描补齐。
- 基础索引登记文件信息，安装 CPU 文字识别组件时也识别图片文字。模型分析可分别为图片、视频抽帧和音频转录选用已配置的模型。向本机以外的服务发送素材前，模型配置需允许上传，且用户须确认具体目录和接收服务。授权保存在 `settings`，保存及发送前均核对目录身份和模型绑定。
- 本地识别、视觉和语音转文字分别记录分析版本。停用模型或更改目录的分析方式后，已生成的证据继续可搜索。重新启用相同模型时可复用上一轮已完成的结果。
- AI 素材索引 v2 按路径绑定每种分析的快照。同一个素材 ID 的多个副本可在不同目录保留各自的模型结果。同一扫描范围的相同内容和模型版本可复用快照，强制重扫只更新选定路径。升级 v1 时，只有来源路径唯一的旧 AI 证据会重新关联，人工备注和标签继续照常检索。
- 根目录和真实子目录可分别设置分析模型、素材类型和递归范围。子目录的外部服务授权同时绑定该目录和登记的库根。局部扫描只更新实际可遍历范围中的文件状态。待分析筛选包含未入库的文件，文件签名匹配且至少有一项已完成的视觉、音频或语音转写结果时显示 AI 标记。
- AI 素材索引 v3 新增 `ai_library_evidence_vectors`。证据 ID 与向量模型配置指纹共同标识一条向量。已完成的描述、标签和转录按批补建，可取消并继续。候选按注册范围、素材类型和有效分析快照筛选，再融合关键词与余弦相似度排名；更换模型可独立补建向量。源结果随数据库备份，旧向量属于可重建的派生数据。
- 素材库和剪辑助手共用混合检索。首选项的默认向量模型负责文本向量，云端接收的内容限于已授权范围的分析文本与语义搜索词。目录和模型配置变更会使原授权失效；剪辑助手跨库查询仅使用有对应库根授权的云端范围。模型缺失、向量暂未生成或服务失败时继续关键词检索。
- 剪辑项目的真实媒体池、时间线和 AI 分析证据继续随 `.topos` 包保存。`<config_root>/cache/project-ai/` 中的 SQLite 库是按项目和归档分支生成的查询索引，只保存媒体池 ID、源媒体指纹、帧率和可用帧范围。助手展示本项目可用的素材 ID，库外候选由用户从助手面板加入媒体池，带源时间范围的候选保留入出点。项目重开后先装配新 AI 服务，再恢复包内证据。

## 4. 插件状态与安装边界

- 安装根 `files-data/plugins/install/`，所有权/事务/SemVer/来源 sidecar 规则 = PR-01 现行实现（`src/shared/plugins/installer.py`），不重开设计。
- 插件启停状态 `state.json` 由新 Inventory 独占写（PR-03 单锁快照），UI/偏好/命令只读快照；旧 `config.json.disabled_plugins` 与 QSettings 插件键**不迁移**。
- 内建十插件来源 = builtin（宿主政策，PR-03 `builtin_ids`），不受安装根管理；G 阶段逐插件接管后其能力归属见矩阵 §3。
- 插件代码对 `files-data/` 的访问一律经 F 阶段 broker 授权；声明权限 ≠ 已强制执行（矩阵 §6.5）。

## 5. 旧开发数据可清空位置（H 阶段重置命令的目标清单）

**原则**：重置只处理下列受管位置；绝不触碰用户源素材目录、`<素材库>/*.xmp`、`<素材库>/.cache/`（旧缓存留在用户卷上不清理）、仓库外任何路径、`external/file_explorer/` 之外的历史文档。

| 位置 | 处置 | 说明 |
| --- | --- | --- |
| `<config_root>/files-data/` | 列出后按显式确认清空重建 | 新系统自身受管目录（含 trash/backups 单列确认） |
| `topos.db`、`topos.db-wal/-shm`（仓库根） | 可清空（开发库） | 内含宿主 settings/layouts——重置命令须先导出/确认，属 H 演练范围 |
| `src/topos.db` | 可清空（分叉遗留） | 无引用后即删 |
| `external/file_explorer/metadata.db*` | 可清空 | 当前不存在 |
| `external/file_explorer/config.json`、`.port_config.json` | 可清空 | I 阶段随旧目录退役 |
| `external/file_explorer/.trash/` | 可清空（30 天保留数据，开发期） | 若含用户误删文件须先确认 |
| `external/file_explorer/.assetdeck/thumbnails/` | 可清空（可再生缓存） | — |
| `cache/`、`.images_cache/`、`src/.images_cache/` | 可清空（可再生缓存） | — |
| QSettings（org ToposColor / app FilesPanel） | **不清**，仅停止读取 | 用户偏好，留着无害；新偏好另起键空间 |

重置命令设计（`topos-files dev-reset --list/--apply`，H 阶段实现演练）：先 `--list` 打印逐项与体积；`--apply` 需 `--yes` 显式确认；不追随符号链接；操作前后对受管目录做摘要核对；源素材摘要不变为通过条件。

## 6. 与既有共享层的衔接

- `src/shared/db/database.py` 的 CWD 默认路径问题：B 阶段把宿主 settings/layouts 库一并改为显式路径注入（沿用定位器根），无参默认值保留但生产代码禁止使用（以测试守卫）。
- FE 旧 db.py/config/trash 锚定缺陷不修——旧系统冻结现状（A-4 基线），随 I 阶段整体退役。
