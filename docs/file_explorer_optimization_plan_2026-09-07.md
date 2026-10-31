# File Explorer 素材管理能力优化计划（对标专业工具差距分析）

> 文档日期：2026-09-07
> 基线代码：`bugfix` 分支 @ `1feaef75`（U1–U10 升级批已全部落地）
> 对标工具：**Eagle 4.x**（设计素材管理）、**Adobe Bridge**（Adobe 生态全类型编目）、**Lightroom Classic**（摄影工作流）、**digiKam 9**（开源摄影管理）、**ACDSee**、**Kyno / Catalyst Browse**（视频粗剪与日志）
> 性质：差距分析 + 实施方案。本文档只做规划，不含已实施记录；每完成一批在「版本历史」追加记录。

---

## 一、现状盘点（已有能力，作为对比基线）

| 能力域 | 已有 | 证据 |
|---|---|---|
| 浏览 | 网格/瀑布/列表/自适应/时间线(按 mtime 月分组) 5 种视图、面包屑、目录树、多磁盘 | `templates/index.html:82-86`、`fe-render.js:181-209` |
| 组织 | 评分 0-5、颜色标记 1-5、旗标(选用/拒收)、结构化标签(tags/file_tags 双轨+legacy 串)、智能文件夹(smart://) | `db.py:264-342`、`routes.py:1550-1845` |
| 搜索 | 全库 FTS5 trigram 搜索(q/tag/type/rating/color/flag)、搜索历史、保存为智能文件夹 | `routes.py:2011`、`fe-actions.js:976-1041` |
| 安全 | 应用内回收站(trash://)+恢复+彻底删除、Ctrl+Z 撤销栈、元数据备份导出/导入/re-link | `trash.py`、`routes.py:779-1029` |
| 预览 | 灯箱(缩放/平移)、对比视图(2-4 张同步缩放)、视频胶片条 scrub、音频 WaveSurfer 波形、PDF 预览、RAW/EXR/PSD 缩略图 | `fe-render.js:303-330`、`thumb_engine.py:29-38` |
| 播放 | video_pro 三引擎(Qt Multimedia / PyAV / Topos 原生)、audio_pro 插件 | `player_host.py:36-48` |
| 批量 | 批量重命名(replace/append)、批量元数据、批量移动、批量旋转、批量删除 | `routes.py:459/568/2248/2344` |
| 工具 | 重复文件查找(MD5)、格式工厂转码、图片工厂批量处理、Darkroom 调色、代码编辑器 | `routes.py:2174`、`plugins/` |
| 基础设施 | 缩略图三档管线+失败缓存、DIR_CACHE 60s、增量分批渲染+无限滚动、轮询推送 | `thumbs.py`、`fe-render.js:177-216` |

**核心局限（一句话版）**：这是一个"目录浏览器 + 侧车数据库"，而专业工具是"全量编目的素材库"——没有索引器、技术元数据不落库、元数据不与文件互通、没有静态集合/层级标签/堆叠、查找手段只有文本。

---

## 二、对标功能矩阵

图例：✅ 完整支持　🟡 部分支持　❌ 缺失

| 功能维度 | Eagle | Bridge | LR Classic | digiKam | ACDSee | Kyno | **本项目** |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 全量后台编目/索引 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌（仅浏览过的文件有 meta 行） |
| 文件夹变更自动监视 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌（`bridge.py:168` 空桩） |
| 结构化技术元数据(EXIF/时长) 落库+筛选 | 🟡 | ✅ | ✅ | ✅ | ✅ | ✅ | ❌（摘要串 `cached_info`，图片 EXIF 不落库） |
| 元数据写回文件 (XMP/IPTC) | 🟡(library.xml) | ✅ | ✅ | ✅ | ✅ | ✅ | ❌（仅 SQLite） |
| 静态集合/相册 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌（只有动态智能文件夹） |
| 智能集合 | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| 层级标签/关键词树 | ✅ 标签组 | ✅ | ✅ | ✅ | ✅ | — | ❌（`tags.parent_id` 列已预留无 UI） |
| 堆叠/版本分组 (stack) | 🟡 | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| 视觉相似搜索 (pHash) | ✅ | ❌ | ❌ | ✅ | 🟡 | — | ❌（重复查找仅 MD5 精确匹配） |
| 按颜色筛选素材 | ✅ 招牌功能 | ❌ | ❌ | ❌ | 🟡 | — | ❌ |
| 批量重命名模板变量 | 🟡 | ✅ 强 | ✅ 强 | ✅ | ✅ | 🟡 | ❌（仅查找替换/前缀+序号） |
| 相机/存储卡导入模块 | ❌ | ✅ | ✅ 强 | ✅ | ✅ | ✅ | ❌ |
| 地图/地理浏览 | ❌ | ❌ | ✅ | ✅ | ✅ | — | ❌（GPS 不读取） |
| OCR 图片文字搜索 | 🟡 | ❌ | ❌ | ✅ | 🟡 | — | ❌ |
| AI 自动打标/人脸识别 | 🟡 去背 | 🟡 | ✅ 人脸 | ✅ 人脸+场景 | ✅ | — | ❌（宿主有 vault 服务未接） |
| 子文件夹内容聚合显示 | ✅ | 🟡 | 🟡 | 🟡 | 🟡 | — | ❌ |
| 库统计仪表盘 | 🟡 | ❌ | 🟡 | ✅ | ✅ | — | ❌ |
| 缩略图/缓存管理工具 | 🟡 | ✅ | ✅ | ✅ | ✅ | — | ❌（只增不减） |
| 导出预设(尺寸/格式/水印) | ❌ | ✅ | ✅ 强 | ✅ | ✅ | ✅ | 🟡（format_factory 手动逐任务） |
| 视频 in/out 标记/子片段 | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 招牌 | ❌ |
| 相同点 | 评分/颜色/旗标/标签/回收站/重复清理/灯箱/幻灯片/拖拽收集 —— 双方均有 | | | | | | ✅ |

---

## 三、差距总览与优先级

| 编号 | 功能 | 优先级 | 工作量 | 批次 |
|---|---|---|---|---|
| GAP-01 | 全库后台索引器（Scanner） | **P0** | L | V1 |
| GAP-02 | 技术元数据结构化落库（EXIF/视频字段列） | **P0** | M | V1（随 GAP-01 建列） |
| GAP-03 | 文件夹监视（自动发现外部变更） | **P0** | M | V1 |
| GAP-04 | 元数据写回 XMP（评级/标签互通） | **P0** | M | V2 |
| GAP-05 | 静态集合 Collections | **P0** | M | V2 |
| GAP-06 | 层级标签树 | P1 | M | V2 |
| GAP-07 | 堆叠 Stack（连拍/多版本归组） | P1 | M | V2 |
| GAP-08 | 视觉相似搜索（pHash） | P1 | M | V3 |
| GAP-09 | 按颜色筛选（主色提取） | P1 | S | V3 |
| GAP-10 | 批量重命名模板引擎 | P1 | M | V3 |
| GAP-11 | 子文件夹内容聚合显示 | P1 | S | V3 |
| GAP-12 | 导入模块（相机/下载目录 → 库） | P2 | M | V3 |
| GAP-13 | 库统计仪表盘 | P2 | S | V3 |
| GAP-14 | 缓存管理（缩略图 LRU/清理工具） | P2 | S | V3 |
| GAP-15 | 查询性能索引补齐 | P2 | S | V1（顺手） |
| GAP-16 | 地理位置提取 + 地图视图 | P2 | M/L | V4 |
| GAP-17 | OCR 文字入库与搜索 | P2 | M | V4 |
| GAP-18 | 导出预设 + 水印 | P2 | M | V4 |
| GAP-19 | 视频 in/out 标记与子片段 | P3 | M | V4 |
| GAP-20 | 幻灯片增强（音频/转场） | P3 | S | V4 |
| GAP-21 | AI 自动打标 / 人脸识别 | P3 | L | 远期 |
| GAP-22 | 文件夹密码保护 | P3 | S | 远期 |

优先级判据：P0 = 缺失后不能称为"素材库"（数据不可发现/不可迁移）；P1 = 专业工作流明显缺口，实现成本可控；P2 = 显著增值；P3 = 重依赖或小众。

---

## 四、逐项实现方案

### GAP-01 全库后台索引器（P0，批次 V1）

**现状证据**：无 `scanner.py`；`/api/search`（`routes.py:2011`）与智能文件夹（`routes.py:1550`）只查 `file_meta` 有行记录的文件——没被打过标/评过分的文件对搜索和智能文件夹**不可见**。新文件发现完全依赖目录导航实时 `os.scandir`（`routes.py:193`）+ 60s DIR_CACHE。

**实现方案**：

1. 新建 `external/file_explorer/scanner.py`：
   ```python
   class LibraryScanner(QObject):          # QThread 亲和主线程信号
       progress = pyqtSignal(dict)          # {library, scanned, total, phase}
       finished_scan = pyqtSignal(dict)     # {added, updated, removed, elapsed}
   ```
   - 后台 QThread（参考 `thumbs.py:21` 的双 worker 模式，单线程即可）遍历 `config.json` 的 libraries 列表，`os.walk` 跳过 `.trash/`、`.cache/`、隐藏目录。
   - 每个文件 UPSERT：`INSERT INTO file_meta(path,...) VALUES(...) ON CONFLICT(path) DO NOTHING`——**绝不覆盖**用户已有的 rating/tags/comments/color/flag；已存在的行仅刷新技术列（mtime/size/GAP-02 各列）。
   - 增量判定：`file_meta.mtime` + `size` 与磁盘一致则跳过（首次全量后二次扫描为秒级）。
   - 孤儿清理：库内已不存在且不在回收站的行，标记 `missing=1`（新增列）而非删除——搜索默认排除 missing，UI 提供"仅看失联"。
   - 节流：每处理 200 个文件 `time.sleep(0.05)` 让出 CPU；扫描中产生缩略图任务走现有 `thumbs.py` 队列（上限 500 已有界）。
2. API（routes.py 新增）：
   - `POST /api/scan/start`（body 可选 `{"library": path}`，缺省全库；已在扫描返回 409）
   - `GET /api/scan/status`（返回 `{running, phase, scanned, total, last_result}`；前端并入现有 1.5s 轮询 `fe-actions.js:523-577`）
3. 前端：
   - 侧边栏"我的素材库"每个库下方加进度行（`已编目 12,340 / 13,001`）+ 手动"重建索引"右键项。
   - 首次开启素材库时提示"后台正在建立索引"，不打断浏览。
4. 触发时机：`webapp` 启动 5s 后自动增量扫描一次；之后每 30 分钟（可配置 `config.json: scan_interval_min`，0=关闭）。

**验收**：库内放入 100 个从未浏览过的文件 → 不做任何浏览操作，全库搜索 `/api/search?q=` 能命中；智能文件夹"全部视频"计数 = 磁盘实际视频数；二次扫描 < 10s（万文件级）。

**依赖**：GAP-02 的列扩展（同批落地）。

---

### GAP-02 技术元数据结构化落库（P0，批次 V1）

**现状证据**：图片 EXIF 每次打开属性面板实时重新解析（`utils.py:75 get_pro_metadata` → exiftool/Pillow），**不落库**；视频 ffprobe 结果只拼成人类可读摘要串存 `cached_info`（`thumbs.py:53-58`）；**没有 duration 列**——视频管理最基本字段缺失；前端时间线引用 `data['Date']`（`fe-render.js:620`）但后端从不产出该键，实际全部用 mtime。

**实现方案**：

1. DB 迁移（`db.py init_db` 追加 ALTER，模式与现有 `color/flag` 列一致，`db.py:326-329`）：
   ```sql
   ALTER TABLE file_meta ADD COLUMN mtime REAL DEFAULT 0;
   ALTER TABLE file_meta ADD COLUMN size INTEGER DEFAULT 0;
   ALTER TABLE file_meta ADD COLUMN date_taken REAL;      -- 拍摄/创建时间（EXIF DateTimeOriginal，视频 startDate，缺省 mtime）
   ALTER TABLE file_meta ADD COLUMN width INTEGER;
   ALTER TABLE file_meta ADD COLUMN height INTEGER;
   ALTER TABLE file_meta ADD COLUMN duration REAL;        -- 秒
   ALTER TABLE file_meta ADD COLUMN codec TEXT;
   ALTER TABLE file_meta ADD COLUMN camera TEXT;          -- "Make Model"
   ALTER TABLE file_meta ADD COLUMN lens TEXT;
   ALTER TABLE file_meta ADD COLUMN iso INTEGER;
   ALTER TABLE file_meta ADD COLUMN aperture REAL;
   ALTER TABLE file_meta ADD COLUMN shutter TEXT;         -- 如 "1/250"，比值不适合数值列
   ALTER TABLE file_meta ADD COLUMN focal REAL;           -- 等效焦距 mm
   ALTER TABLE file_meta ADD COLUMN gps_lat REAL;
   ALTER TABLE file_meta ADD COLUMN gps_lon REAL;
   ALTER TABLE file_meta ADD COLUMN indexed_at REAL;
   ```
2. 提取管线：扩展 `utils.get_pro_metadata()` 返回结构化 dict（现返回扁平字符串表，加一个并行函数 `get_tech_meta(path) -> dict` 复用其 ffprobe/exiftool 子进程调用）；`thumbs.py` 缩略图生成路径已有 ffprobe 异步队列（`thumbs.py:43`），把 `UPDATE file_meta SET duration=..., width=...` 并入同一队列，GAP-01 扫描器直接调用。
3. API 扩展：
   - `/api/search` 与智能文件夹规则引擎新增过滤键：`min_duration/max_duration`、`camera`（LIKE）、`date_from/date_to`、`has_gps`、`min_width/min_height`。
   - `/api/list` 响应 file 对象附带 `duration/date_taken/size`（已在一次 DB JOIN 内，零额外查询）。
4. 前端：
   - 列表视图加"时长/大小/拍摄日期"列（`fe-render.js` list 分支）；视频卡片角标显示 `mm:ss`。
   - 筛选下拉（`index.html:242-274`）加"拍摄日期范围"（原生 `<input type="date">` x2）与"时长 ≥"。
   - 时间线分组增加切换：`修改时间 | 拍摄时间`（`fe-render.js:181-209`，date_taken 空则回退 mtime——平滑降级）。

**验收**：导入 GoPro 素材后列表能按"时长 ≥ 10 分钟"筛出长镜头；EXIF 拍摄日期与 Finder 显示一致；旧库升级后原有行不丢（新列默认 NULL）。

---

### GAP-03 文件夹监视（P0，批次 V1）

**现状证据**：`bridge.py:168` `onDirectoryChanged` 是空 `pass` 桩；外部程序（相机导入、下载、其他编辑器保存）产生的变更要等 60s DIR_CACHE 过期或手动刷新。

**实现方案**：

1. `QFileSystemWatcher` 填桩（不引入 watchdog 新依赖；macOS/Linux/Windows 均支持，`addPath` 递归受限则按一级子目录注册，子目录创建时动态补注册——这是 QFileSystemWatcher 的标准用法）。
2. 事件去抖：收到 `directoryChanged` 后对该路径 2s 合并（`QTimer.singleShot` 重置），然后：
   - `DIR_CACHE` 失效该 key（复用 `/api/cache/invalidate` 的内部函数，`routes.py:76`）；
   - 若 GAP-01 索引器开启，投递该目录的增量扫描任务（单目录 `os.scandir` + UPSERT）。
3. 前端刷新：当前目录正是变更目录时，轮询 `/api/list` 发现 `t_ver/entries` 变化自动重渲染（现有 1.5s 轮询已拉数据，只需在响应变化时刷新，避免整页 reload）。
4. 边界：`.trash`、`.cache` 目录不监视；库被拔出（卷离线）时 `removePath` 容错。

**验收**：在库目录用外部程序保存一张新图，≤5s 网格自动出现；外部删除文件 ≤5s 网格消失且回收站不产生记录（外部删除无法进应用回收站，属正常）。

---

### GAP-04 元数据写回 XMP（P0，批次 V2）

**现状证据**：全代码无 pyexiv2/piexif/XMP（grep 无命中）；评分/标签/备注/颜色只写本机 SQLite——**换工具、换机器、共享素材给同事，标记全部丢失**。Lightroom/digiKam/Bridge 均支持 XMP 互通。

**实现方案**：

1. 复用已有依赖 **exiftool**（`utils.py` 已在用其读取），写入走子进程：
   ```
   exiftool -overwrite_original -P \
     -XMP:Rating=3 -IPTC:Rating=3 \
     -XMP-dc:Description="备注" \
     -XMP:HierarchicalSubject<a/b -XMP-lr:HierarchicalSubject<a/b \
     -sep ", " -IPTC:Keywords="标签1,标签2"  <file>
   ```
   - exiftool 可写几乎所有格式（含视频 MOV/MP4 的 XMP），不支持的格式返回非零 → 记日志跳过。
2. 新建 `meta_sync.py`：单线程串行队列（QThread），`/api/update_meta`、`/api/batch/meta`、`/api/meta/tags` 成功后投递异步任务（不阻塞 API 响应）。失败重试 1 次，仍失败 toast 提示"未能写入文件元数据（只读或格式不支持）"。
3. 模式配置（config.json `meta_embed`）：
   - `sidecar`（默认）：对图片生成/更新同名 `.xmp`；对视频直接内嵌（视频 sidecar 支持面窄）。
   - `inplace`：直接写文件内（改 mtime → 需同步刷新缩略图 `.v` 与 file_meta.mtime，复用 `routes.py:2248` 批量旋转已验证的 mtime 一致性处理）。
   - `off`。
   - `.xmp` 文件纳入 GAP-01 扫描忽略清单；纳入回收站删除时同步删除（改 `trash.py` 删除/恢复两条链）。
4. **反向导入**（Lightroom "从文件读取元数据"）：设置页按钮"从文件导入标记"→ 扫描库内文件读 XMP Rating/Keywords，DB 为默认值时采用文件值（冲突不覆盖 DB，报告冲突数）。
5. 元数据导出/导入（`/api/meta/export` v2）追加 `xmp_available` 提示，让用户知道还有第二恢复通道。

**验收**：打 3 星 + 标签"客户A"后，终端 `exiftool -XMP:all file.jpg` 能看到 Rating=3 与 Keywords；把文件拷到另一台机器重新入库，星级标签自动恢复；只读卷上操作不报错只提示。

---

### GAP-05 静态集合 Collections（P0，批次 V2）

**现状证据**：db.py 无 collection/album 表（grep 无命中）；仅有 smart:// 动态查询。**无法手工圈选一批素材组成"客户 A 初选/某项目素材包"**——这是 Bridge/LR/Eagle 共有的最基本组织单元。

**实现方案**：

1. DB：
   ```sql
   CREATE TABLE IF NOT EXISTS collections(
     id INTEGER PRIMARY KEY AUTOINCREMENT,
     name TEXT NOT NULL, parent_id INTEGER, created_at REAL);
   CREATE TABLE IF NOT EXISTS collection_items(
     collection_id INTEGER NOT NULL, file_path TEXT NOT NULL,
     sort_order INTEGER DEFAULT 0, added_at REAL,
     PRIMARY KEY(collection_id, file_path));
   CREATE INDEX IF NOT EXISTS idx_coll_items_path ON collection_items(file_path);
   ```
2. API：`/api/collections` GET/POST/PUT/DELETE + `/api/collections/<id>/items` POST（action: add/remove/reorder，批量路径）。响应附带成员计数。
3. 前端：
   - 侧边栏智能文件夹区块上方新增"集合"区块（`index.html:137-146` 同款交互：右键重命名/删除/改色）。
   - 拖入：卡片 dragstart 已携带路径（`fe-render.js:139-142`），侧栏集合项加 drop 目标；右键菜单"添加到集合 ▸"子菜单。
   - 虚拟视图 `collection://<id>`，完全复用 search:// 的渲染管道（`fe-actions.js` 加一个 openCollection 分支）。
   - 集合内支持手动拖拽排序（sort_order，HTML5 DnD reorder）。
4. 路径随迁：把 `collection_items` 加进 rename（`routes.py:312-320` 目录后代平移）与 batch_move（`routes.py:2344`）的 UPDATE 目标表清单，文件移动后集合不散架。
5. 回收站：成员被删进回收站时集合行保留，collection:// 视图内标记"已入回收站"灰态；彻底删除时清理行。

**验收**：建"客户A初选"拖入 12 张图 → 重启仍存在；把其中 3 张移动到子目录，集合仍指向新路径；导出/导入元数据后集合完整恢复（v3 导出格式追加 collections 数组）。

---

### GAP-06 层级标签树（P1，批次 V2）

**现状证据**：`tags.parent_id INTEGER` 已存在（`db.py:332-337`）但无任何 UI 与递归查询——标签面板是平铺列表（`index.html:148-157`）。Eagle 的"标签组"、LR 的关键词树、Bridge 的层级关键词都是核心组织手段。

**实现方案**：

1. API：`GET /api/tags` 已返回全量（`routes.py:1847`）附带 parent_id；`PUT /api/tags/<id>` 增加可选 `parent_id`（防环校验：沿 parent 链上溯不许遇到自身）。
2. 查询语义（关键决策，对齐 Lightroom）：**筛选父标签 = 包含所有子孙标签的文件**，打父标签不自动打子标签。SQL 用递归 CTE：
   ```sql
   WITH RECURSIVE tag_tree(id) AS (
     SELECT ? UNION SELECT t.id FROM tags t JOIN tag_tree tt ON t.parent_id = tt.id)
   SELECT DISTINCT file_path FROM file_tags WHERE tag_id IN tag_tree;
   ```
   挂进 `/api/search` 的 tag 参数与智能文件夹规则引擎（`routes.py:1550`）。
3. 前端标签面板：缩进树 + 折叠箭头 + 子标签计数气泡（含子孙聚合）；拖拽标签到另一标签上设父子。标签输入 datalist 补全（`fe-render.js:512-566`）显示为 `父/子` 路径形式。
4. XMP 互通：GAP-04 写 HierarchicalSubject 用 `/` 连接路径，读取反向展开——与 LR 互认。

**验收**：建两级"项目/客户A"，给文件只打"客户A"，点"项目"能筛到它；标签树拖拽改父子后计数正确；导出 XMP 在 Lightroom 中看到相同层级。

---

### GAP-07 堆叠 Stack（P1，批次 V2）

**现状证据**：无 stack 概念（grep 无命中）。连拍 10 张、HDR 多曝光、同一素材的多版本转码挤占网格，无归组手段。

**实现方案**：

1. DB：`file_meta` 加 `stack_id INTEGER`；`stacks(id, cover_path, created_at)`。组内 cover 显示，其余折叠。
2. API：`POST /api/stacks`（paths[]，可选 cover）、`POST /api/stacks/<id>/unstack`、`PUT /api/stacks/<id>`（换 cover/加成员/移除成员）。
3. 前端：网格卡片渲染时（`fe-render.js` 网格分支）同 stack 只渲染 cover，右下角角标 `×N`；点击展开临时行内平铺（不跳转）；右键菜单"堆叠所选 / 取消堆叠"。撤销栈 pushUnstack。
4. 自动建议（P2 增强）：date_taken 秒级相同且 camera 相同的 ≥3 张 → 侧栏提示"发现 8 组疑似连拍，是否堆叠"。
5. 排序/搜索语义：堆叠成员参与搜索时整组以 cover 代表（LR 行为），设置可关。

**验收**：堆叠 5 张连拍网格只占 1 格；展开后可单张评级；cover 移动路径后堆叠关系保持（stack 迁移并入 rename/batch_move UPDATE 清单）。

---

### GAP-08 视觉相似搜索 pHash（P1，批次 V3）

**现状证据**：`/api/duplicates/scan`（`routes.py:2174`）只做同尺寸→MD5 **完全相同**检测；digiKam 相似度搜索/Eagle"查找相似"覆盖"同图不同压缩/尺寸/微调色"场景。

**实现方案**：

1. 感知哈希：自实现 DCT pHash 64bit（Pillow 缩 32x32 灰度 → numpy DCT → 取左上 8x8 均值阈值位图），不引入 imagehash 新依赖（numpy 宿主必有）。视频取 cover 帧（缩略图已生成则直接读缩略图文件，零额外解码）。
2. 落库：`file_meta.phash TEXT`（16 位 hex），GAP-01 扫描与缩略图生成管线顺带计算。
3. 查询 `GET /api/similar?path=<p>&threshold=8`：读该文件 phash，全表取 phash 列（10 万行 < 1MB），Python 侧 `int 异或 → bin(x).count('1')` 排序取距离 ≤ 阈值的前 50。结果页复用 search:// 虚拟视图 `similar://<path>`。
4. 入口：卡片右键"查找相似"；duplicates 面板加第二 tab"相似（非完全相同）"，分组展示距离。
5. 与 GAP-12 导入联动：导入时命中 phash 距离 ≤4 → 提示"疑似重复，跳过/保留两者"。

**验收**：同一张照片的 JPG 与重压缩 WEBP（尺寸不同）互相命中；500 张库全表相似查询 < 500ms。

---

### GAP-09 按颜色筛选（P1，批次 V3，Eagle 招牌功能）

**现状证据**：颜色标记(1-5)是**用户手工打标**（`routes.py:344`），无"图片自动主色"概念。Eagle 的自动颜色筛选是设计师找素材高频功能。

**实现方案**：

1. 提取：缩略图生成时（`thumb_engine.py` small 档 300px 已在磁盘）Pillow 打开 → 缩到 16x16 → 转 HSV → 直方图取主桶 → 存 `file_meta.dom_color INTEGER`（0xRRGGBB）。饱和度/明度极低的（黑白灰）存灰阶标记。
2. 筛选 `?color=<hex>&tolerance=2`：HLS 距离（SQL 做不了，Python 过滤 or 12 色桶预计算列 `dom_bucket INTEGER 0-11`，SQL 直接等值——**采用桶方案**，零运行时开销）。
3. 前端：筛选区色板（复用现有色板 UI 样式）加一行"自动颜色"12 色桶条；点击后网格按该色桶过滤，卡片角落显示提取的主色小圆点（悬停放大）。
4. 与手工颜色标记并存：手工标记优先展示，自动主色仅参与筛选。

**验收**：红墙背景照片被"红色"桶筛出；黑白照片落入灰桶互不干扰；旧素材经重建索引后可用。

---

### GAP-10 批量重命名模板引擎（P1，批次 V3）

**现状证据**：`/api/batch/rename`（`routes.py:459`）仅 replace / append(start+digits) 两种；Bridge/LR 支持 `{日期}{EXIF}{序号}{大小写}` 组合模板——摄影工作流刚需（`IMG_1234` → `20260901_婚礼_001`）。

**实现方案**：

1. 模板语法（Jinja 风格简化版，自实现 ~80 行解析器，无新依赖）：
   - `{name}` 原名（无扩展名）　`{ext}` 扩展名　`{seq}` 序号（`{seq:3}` 补零 3 位，`{seq:start=10}`）
   - `{date:mtime|taken:%Y%m%d}`（strftime 格式，缺 taken 回退 mtime——依赖 GAP-02 落库）
   - `{camera}` `{lens}` `{iso}` `{aperture}` `{shutter}` `{focal}` `{w}` `{h}` `{duration}`（GAP-02 列）
   - `{parent}` 父目录名　`{parent2}` 上上级
   - 修饰符 `{name:upper|lower}`；`[:30]` 截断
2. 后端：`/api/batch/rename` 增加 `mode:"template"`；新增 `POST /api/batch/rename/preview`（dry-run 返回前后对照与冲突检测，复用现有 `_1` 后缀策略）。
3. 前端：批量面板重命名 tab（`fe-render.js:634-665` 已有实时预览骨架）加变量插入按钮条（点击插入占位符）+ 预览表（旧名 → 新名，冲突红色行）。
4. 安全：模板结果过文件名非法字符清洗（`\/:*?"<>|`）；空结果/全同名拒绝；undo 栈照旧支持反向 rename。

**验收**：`{date:taken:%Y%m%d}_{seq:3}` 重命名 100 张照片全部成功且可 Ctrl+Z 整批撤销；冲突预览准确。

---

### GAP-11 子文件夹内容聚合显示（P1，批次 V3）

**现状证据**：`/api/list` 单层 `os.scandir`（`routes.py:193`），子目录只显示为文件夹卡片。Eagle 的"子文件夹内容显示"（官方博客重点功能）让素材扁平化浏览不破坏磁盘组织。

**实现方案**：

1. API：`/api/list?recursive=1&depth=N`（默认 1 层、上限 3 层、总量护栏 2000 复用现有截断语义）；文件路径带相对前缀渲染。虚拟视图（smart/search/collection）天然聚合，无需处理。
2. 前端：目录头部工具条开关"显示子文件夹内容"（状态入 localStorage）；聚合模式下卡片左下角显示所属子目录 chip，点击跳转真实目录；文件夹卡片与文件混排时文件夹置顶。
3. DIR_CACHE：key 加入 recursive 维度避免缓存互踩。

**验收**：开启后父目录可见全部子目录文件；评级/打标操作在聚合视图内正常（路径已是全路径）。

---

### GAP-12 导入模块（P2，批次 V3）

**现状证据**：只有"上传到当前目录"（`routes.py:1182`，浏览器文件选择器）和 Qt 粘贴（CopyWorker）。没有从相机/SD 卡/下载目录批量导入并自动整理的能力（LR/Bridge/digiKam 标配）。

**实现方案**：

1. 前端"导入"向导模态（3 步）：
   - 选源：Qt 目录/文件多选对话框（复用 `/api/action/open_folder_dialog` 机制，`routes.py:1306`），左侧支持"设备/卷"快捷入口（复用 `/api/drives`，`routes.py:1049`）。
   - 规则：目标目录（默认库根/按日期子目录 `{YYYY}/{YYYY-MM-DD}`）+ 重命名模板（复用 GAP-10 引擎）+ 选项（跳过重复[MD5/phash，GAP-08 联动] / 导入后删除源[走应用回收站协议，源在库外则 send2trash] / 转码 HEIC→JPG 可选）。
   - 执行：复用 CopyWorker（`window.py:148-176` 已有进度/速度/取消），完成后续做 rename+入库。
2. 导入历史：config.json `import_history`（最近 20 条规则，一键复用）。
3. 边界：同名冲突沿用现有 `(1)` 策略；导入中断的半成品由 CopyWorker 现有清理逻辑兜底。

**验收**：从 SD 卡导入 50 张 → 自动按拍摄日期归入 `2026/2026-09-07/` 并按模板重命名；重复卡内文件二次导入全部跳过并报告。

---

### GAP-13 库统计仪表盘（P2，批次 V3，S）

**实现方案**：`GET /api/stats` 单条 SQL 聚合 file_meta（按 ext 分组计数/大小、评分分布、颜色分布、标签 TOP20、最近 7 天新增、缺失文件数、库总数与总体积）；前端"库统计"模态纯 CSS 条形图 + 一键导出 PNG（canvas 截图）。duplicates/相似清理建议入口（体积 TOP 重复组）。**验收**：数字与 SQL 手工核对一致；10 万行 < 300ms（配合 GAP-15 索引）。

---

### GAP-14 缓存管理（P2，批次 V3，S）

**现状证据**：缩略图磁盘缓存只增不减（本地 `.cache` + 全局 md5 兜底双份），无任何清理工具——数年后可达数十 GB。

**实现方案**：设置页"存储"区块：显示两处缓存体积（`os.walk` 聚合，后台线程算）；操作按钮——"清理失败缓存残留 / 清理 90 天未访问（用缩略图 atime，APFS 注意 noatime 挂载降级 mtime）/ 清理全部分辨率 > large 的旧档"；上限水位（默认 5GB）后台线程超额时 LRU 淘汰。**注意**：`.v` 有效性 sidecar 与缩略图同名，删除需成对。**验收**：清理后浏览不重新生成（未删的仍命中）；体积下降符合预期。

---

### GAP-15 查询性能索引补齐（P2，批次 V1 顺手，S）

**现状证据**：`init_db` 中**没有任何显式 CREATE INDEX**（探索确认）；`file_tags(tag_id)` 无单列索引——按标签查文件全表扫；GAP-02 落库后 rating/date_taken/duration 也将成为高频过滤列。

**实现方案**：
```sql
CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_meta_rating ON file_meta(rating);
CREATE INDEX IF NOT EXISTS idx_meta_date_taken ON file_meta(date_taken);
CREATE INDEX IF NOT EXISTS idx_meta_duration ON file_meta(duration);
CREATE INDEX IF NOT EXISTS idx_meta_stack ON file_meta(stack_id) WHERE stack_id IS NOT NULL;
```
+ `PRAGMA journal_mode=WAL`（多线程 waitress 4 workers 下的读写并发，`webapp` 启动时设一次）。**验收**：EXPLAIN QUERY PLAN 不再出现 SCAN file_tags；10 万行标签筛选 < 50ms。

---

### GAP-16 地理位置提取 + 地图视图（P2，批次 V4）

**实现方案**：
1. 提取（先行，成本低）：GAP-02 扫描时读 exiftool GPSLatitude/GPSLongitude（含 E/W/N/S 符号处理）落 gps_lat/gps_lon；`/api/search?has_gps=1` 与智能文件夹"有位置"条件；属性面板显示坐标 + "在地图中查看"（跳系统地图 `https://maps.apple.com/?ll=`，零依赖）。
2. 地图视图（后做，依赖大）：QWebEngineView 内 Leaflet（vendor 进 `static/vendor/leaflet/`，同 wavesurfer 模式）+ 在线瓦片（默认 OSM，设置可换高德——国内可达性）；pin 聚合（markercluster）；点击 pin 筛选文件列表侧栏；拖拽文件到地图设置 GPS（exiftool 写回，复用 GAP-04 管线）。
**验收**：iPhone 照片落点正确；断网时已有瓦片的区域仍可浏览；无 GPS 素材不报错。

---

### GAP-17 OCR 文字入库与搜索（P2，批次 V4）

**实现方案**：
1. 引擎选型：macOS 优先 `Vision` 框架（pyobjc，系统自带中文识别，零模型下载）；跨平台兜底 RapidOCR（onnxruntime，模型 ~10MB vendor 进插件目录）。封装为 `ocr_engine.py`，运行时探测可用性。
2. 管线：GAP-01 扫描器对图片类（EXT_IMAGE_NORMAL + 截图常见尺寸）投递 OCR 任务（独立单线程队列，CPU 限速）；结果存新表 `ocr_text(file_path PK, text, lang, ocr_at)`。
3. 搜索：FTS5 表加列需迁移——重建 `file_meta_fts(path, name, tags, comments, ocr)`（init_db 检测旧表结构自动 DROP+回填，回填幂等标记复用 `fe_markers` 机制 `db.py:381`）；`/api/search` 把 ocr 列纳入 MATCH。触发器 fts_ai/au 同步扩展。
4. 前端：属性面板"图内文字"折叠区显示提取结果；设置开关（默认关，标注隐私提示）。
**验收**：白板照片里的"会议纪要 9/7"能被全库搜索命中；关闭开关后不再产生 OCR 任务。

---

### GAP-18 导出预设 + 水印（P2，批次 V4）

**现状证据**：format_factory（转码）/image_factory（批量处理）都是手动逐任务配置，无"预设"概念；无水印。Bridge/LR 的导出面板（预设：尺寸/格式/质量/水印/命名）是交付工作流终点。

**实现方案**：
1. 预设存储：DB 新表 `export_presets(id, name, kind[image|video], config JSON)`；UI 在两个工厂插件的配置面板加"保存为预设/应用预设/管理预设"（下拉 + CRUD 模态）。
2. 前端一键导出：卡片多选 → 右键"导出 ▸ [预设列表]"→ 后端 `POST /api/export/run`（新路由，编排调用 image_factory/format_factory 的核心处理函数——两个插件已有可复用的纯函数层）→ 输出到用户选择目录，复用 CopyWorker 进度 UI。
3. 水印（图片路径）：Pillow `ImageDraw`（文字水印：内容/字体/大小/颜色/透明度/位置 9 宫格/平铺；图片水印：PNG 带 alpha 叠加）；视频水印 ffmpeg `overlay`/`drawtext` 滤镜（format_factory 任务参数扩展）。
4. 导出命名：复用 GAP-10 模板引擎（`{name}_导出` 默认）。
**验收**：选中 20 张 → "导出 ▸ Web 1280px 水印"一键完成，产物命名与水印符合预设。

---

### GAP-19 视频 in/out 标记与子片段（P3，批次 V4）

**实现方案**：DB 新表 `markers(file_path, t_in REAL, t_out REAL, label, color, created_at)`；video_pro 播放器加 I/O 快捷键（`fe-actions.js` 快捷键区 + 播放器 keyPressEvent 转发）；卡片角标显示标记数；右键"导出子片段"→ 复用 `/api/audio/trim` 的 ffmpeg `-ss -to -c copy` 模式（`routes.py:1337` 已验证）扩展到视频；灯箱胶片条上渲染标记色块。对标 Kyno 的核心日志能力。**验收**：标记的片段能无损导出（copy 编码可切时）。

---

### GAP-20 幻灯片增强（P3，批次 V4，S）

**实现方案**：现有幻灯片（`fe-actions.js:1339-1370`，仅图片定时轮播）扩展：背景音乐（audio_pro 引擎播放单曲/列表）、淡入淡出 CSS 过渡、视频素材支持（静音自动播放时长即间隔）、全屏 API；"导出为视频"远期（ffmpeg concat 图片序列，依赖 GAP-18 编排层）。

---

### GAP-21 AI 自动打标 / 人脸识别（P3，远期）

**现状证据**：宿主已有语义检索服务（ai_library_browser 插件对接 topos-vault :8288，`ai_library_browser/__init__.py:24`），但仅查不写；本地无人脸/场景模型。

**实现方案**（分两步走，控依赖）：
1. **接宿主 AI**（第一步，零新模型）：扫描器把图片送 topos-vault 语义索引（已有服务），返回的自动标签前缀 `ai:` 写入标签体系（独立命名空间，不污染人工标签）；搜索直接可用。人脸不做。
2. **本地模型**（第二步，可选大件）：MobileNetV3/CLIP-Lowbit 做场景打标 + RetinaFace 做人脸检测，模型文件用户手动放置 `models/`（不随仓库分发）；人脸区裁剪图 phash 聚类 → 人工命名（digiKam 累积确认模式）；隐私默认全关。
**验收（第一步）**：开启后新入库图片 5 分钟内出现 `ai: mountain` 类标签且可搜索。

---

### GAP-22 文件夹密码保护（P3，远期，S）

**实现方案**（对标 Eagle 文件夹加密）：DB `folder_locks(path, pwd_hash, hint)`（PBKDF2）；`/api/list`/`/api/tree`/缩略图对锁定目录返回锁形占位；解锁后会话级缓存口令（内存，不落盘）；`/api/file` 下载同样校验。**明确边界**：这是 UI 级软锁（文件本体在磁盘未加密），文档需如实说明，不宣称安全防护。

---

## 五、批次划分与执行序

```
V1 数据地基（P0 核心，约 4-6 天）
   GAP-15 索引补齐（半天热身，收益立现）
   GAP-02 技术元数据落库（列迁移 + 提取管线）
   GAP-01 全库索引器（依赖 02 的列）
   GAP-03 文件夹监视（依赖 01 的增量通道）
   交付物：搜索/智能文件夹从"碰过的文件"变为"全库可用"；时长/拍摄日期可筛可排

V2 组织与互通（P0 收尾，约 4-5 天）
   GAP-04 XMP 回写（exiftool 复用，独立可并行）
   GAP-05 静态集合
   GAP-06 层级标签
   GAP-07 堆叠
   交付物：素材标记跨工具/跨机器不丢；手工圈选/连拍归组/标签树齐备

V3 查找与工作流（P1 全清 + P2 快赢，约 5-7 天）
   GAP-08 pHash 相似搜索
   GAP-09 自动颜色筛选
   GAP-10 重命名模板引擎
   GAP-11 子文件夹聚合
   GAP-12 导入模块（依赖 08/10）
   GAP-13 统计仪表盘、GAP-14 缓存管理（S 项穿插）
   交付物：找素材的手段对齐 Eagle/digiKam；导入-整理-导出闭环

V4 增强与视频（P2/P3，按需排期）
   GAP-16 地理（提取先行，地图后置）
   GAP-17 OCR
   GAP-18 导出预设+水印、GAP-19 视频标记、GAP-20 幻灯片
   远期：GAP-21 AI、GAP-22 软锁
```

每批沿用既有交付纪律：配套 `test_vN_*.py` 验收脚本（隔离 DB/tempdir 模式）、静态前端断言、计划文档批次记录 + 版本历史追加。

---

## 六、风险与依赖

| 风险 | 影响 | 缓解 |
|---|---|---|
| 全库首扫大库（10 万+）耗时/占用磁盘 IO | V1 体验 | 节流+可暂停+增量优先；首扫只做文件枚举落 path/mtime，元数据二阶段补 |
| exiftool 写入改 mtime | 缩略图/增量误判 | 写后回写 file_meta.mtime 与 `.v` sidecar（批量旋转链已有成熟处理） |
| SQLite 写并发（waitress 4 线程 + 扫描器写） | 锁冲突 | WAL 模式（GAP-15）+ 扫描器批量事务（每 500 行 commit）+ busy_timeout |
| FTS 表加列迁移（GAP-17） | 旧库升级 | DROP+重建+fe_markers 幂等回填，升级路径先在测试库演练 |
| QFileSystemWatcher 递归限制 | 监视不全 | 子目录动态补注册；30 分钟兜底全量增量扫 |
| numpy/pyobjc 等运行时依赖 | 分发体积 | pHash 自实现仅依赖 numpy（宿主必有）；OCR 引擎运行时探测、优雅降级 |
| XMP 写坏文件 | 数据损失 | exiftool `-P` 保护+写入前复制到 `.trash` 旁路一份（或先 sidecar 模式灰度） |

---

## 七、明确不做（Scope Out）

| 项 | 理由 |
|---|---|
| 素材内嵌真加密（磁盘级） | 超出素材管理边界，软锁如实标注即可 |
| 云同步/协作（多用户库） | 单机工具定位；元数据导出+XMP 已提供迁移通道 |
| 网页版/移动端 | QWebEngineView 桌面架构决定 |
| 视频剪辑时间线 | 宿主 Topos Color 本体就是编辑器，管理器只做粗编（in/out 标记） |
| 缩略图人脸马赛克等破坏性批处理 | image_factory 已有处理管线，不重复建设 |

---

## 八、版本历史

- 2026-09-07：初版。基于 U1–U10 全量落地后的代码基线（`1feaef75`），完成与 Eagle/Bridge/Lightroom/digiKam/ACDSee/Kyno 的差距分析，产出 22 项 GAP（P0×5 / P1×6 / P2×5 / P3×6）与 V1–V4 批次规划。
- 2026-09-07：**V1 数据地基批完成**（GAP-15/02/01/03）。验收 `test_v1_foundation.py` 51 项断言全绿 + U5/U6/U7/U9/U10/consistency/library_optimizations/thumb_fix 全量回归无破坏。要点：
  - GAP-15：`db.py` 补 6 个显式索引（file_tags.tag_id / rating / date_taken / duration / missing / stack 部分索引）+ WAL（事务内切换会被 SQLite 静默拒绝——先 commit 再 PRAGMA，且 PRAGMA 返回行必须消费）。
  - GAP-02：file_meta 扩 20 个技术列（mtime/size/date_taken/宽高/时长/codec/camera/lens/iso/aperture/shutter/focal/GPS + 预置 dom_color/phash/stack_id/missing/indexed_at）；`utils.get_tech_meta()` 结构化提取（ffprobe 视频 / exiftool 图片含 GPS 十进制 / Pillow 兜底）；thumbs 元数据队列顺带回写；date_taken 全链路统一 UTC 语义（exiftool 原串按 UTC 读入显示，timegm 与前端 Date.parse 一致）。
  - GAP-01：新模块 `scanner.py`——全库增量索引（ON CONFLICT 只 touch 技术列，用户资产列永不覆盖；mtime/size 变更检测；失联标 missing=1 不删行；枚举 20 万护栏；技术元数据内联提取节流；缩略图走既有有界队列）；`/api/scan/start|status`；ensure_runtime 启动 5s 后首扫 + 按 `scan_interval_min`（默认 30，0 关）周期；`/api/list`、`/api/search`、智能文件夹全部下发/支持 duration·date_taken·camera·GPS 过滤。
  - GAP-03：新模块 `watcher.py`——QFileSystemWatcher 逐级注册（上限 2000）+ 2s 去抖 → 缓存失效 + `scanner.scan_directory` 单目录增量（纯枚举+DB，GUI 线程安全）；无 Qt 事件循环自动停用；独立模式 MainWindow 补挂（ensure_runtime 先于 QApplication）；30 分钟周期扫描兜底。
  - 前端：视频时长角标（卡片+meta 行）、时间线基准切换（修改时间|拍摄时间）、筛选菜单新增拍摄日期范围与最短时长、侧栏索引状态块（3s 轮询 + 手动重建 + 扫描完成自动刷新目录）。
- 2026-09-07：**V2 组织与互通批完成**（GAP-04/05/06/07）。验收 `test_v2_organization.py` 49 项断言全绿 + V1/U 系全量回归无破坏。要点：
  - GAP-04：新模块 `meta_sync.py`——XMP 写回串行队列（Rating/Subject/HierarchicalSubject 层级路径/digiKam ColorLabel 映射/lr:Flag），sidecar（默认，exiftool -o）| inplace | off 三模式；update_meta/batch_meta/meta_tags 三处入队；`/api/meta/import_xmp` 反向导入（DB 空值才采用）；.xmp 旁车对列表与扫描器不可见。**依赖 exiftool**（utils.get_tool_path 探测，缺失静默跳过并留日志）。
  - GAP-05：collections/collection_items 两表 + 全套 CRUD/成员 API；`collection://<id>` 虚拟视图；拖拽文件到侧栏集合即添加（选区联动）；rename/batch_move/目录 rename 三条路径迁移链同步平移集合成员；成员失联但仍在回收站时行保留（恢复即复活）。
  - GAP-06：tags.parent_id 启用——`_tag_and_descendant_ids` 递归 CTE，父标签筛选=含全部子孙（/api/search 与智能文件夹 tag 规则同语义，不存在标签返回空而非全库）；PUT /api/tags/<id> 支持 parent_id（防环上溯校验）；侧栏标签树缩进渲染 + 拖拽改父。
  - GAP-07：stacks 表 + file_meta.stack_id；创建/调整封面/加删成员/解除 API；/api/list 下发 stack_id/cover/size，前端网格折叠只显示封面（×N 角标点击展开，封面被筛掉时全员可见降级）；右键"堆叠所选/取消堆叠"。
  - 附带修复：`INSERT OR REPLACE INTO file_meta` 的隐式 DELETE 会把未列出列重置 NULL——update_meta 与 meta_import 改为 `ON CONFLICT DO UPDATE`，改评分不再清空 V1 技术列；堆叠创建时为无行成员补基础行。
- 2026-09-07：**V3 查找工作流批完成**（GAP-08~14）。验收 `test_v3_workflow.py` 52 项断言全绿 + V1/V2/U 系全量回归无破坏。要点：
  - GAP-08：新模块 `media_features.py`——自实现 numpy DCT pHash（64bit hex）+ 汉明距离；`/api/similar?path=&threshold=`（目标无 phash 时按需现算并回写）；缩略图管线 small 档生成后顺带计算特征；右键"查找相似" → `similar://` 虚拟视图。
  - GAP-09：主色桶（均值色 HSV → 12 色相桶 + 灰阶 -1；**桶边界偏移 15° 让正红居桶中央**——否则 ±2 的 b/g JPEG 噪声会把 0.3° 环绕成 359.7° 掉进品红桶）；`dom_bucket` 搜索/智能文件夹规则/列表下发 + 筛选菜单 12+1 色点条。
  - GAP-10：新模块 `rename_template.py`——`{name}/{seq:3}/{date:taken:%Y%m%d}/{camera}/{iso}/{aperture}/{w}x{h}/{parent}` + `:upper/:lower`，未知字段原样保留、非法字符清洗、模板未含 `{ext}` 自动保留扩展名；`_plan_renames` 统一执行与 dry-run（`POST /api/batch/rename/preview` 冲突标记）；批量面板模板模式 + 变量插入按钮 + 实时预览。
  - GAP-11：`/api/list?recursive=1&depth=N`（上限 3 层/2000 条，条目带 subdir 来源；缓存 key 含模式防互踩）；工具条"显示子文件夹内容"开关（localStorage 持久），卡片 meta 行显示 `[子目录]` chip。
  - GAP-12：`/api/import/run`——源目录→库目录，按拍摄日期（DB→mtime 回退）YYYY/YYYY-MM-DD 归档 + 模板重命名 + 同名同体积查重跳过 + 冲突自动 `(n)` 后缀 + 可选删源（send2trash）+ 入库后投递元数据/缩略图队列；导入向导模态。
  - GAP-13：`/api/stats`（总量/体积/时长/扩展名分布/评分分布/标签 TOP/失联/近 7 天）+ 库统计模态（纯 CSS 条形图）；扩展名聚合在 Python 侧完成（SQLite 无 reverse()）。
  - GAP-14：`/api/cache/size`（全局 .assetdeck + 各库 .cache 体积）与 `/api/cache/cleanup`（older_days 按龄清/全清，清理后清空 DIR_CACHE）；缓存管理模态。
- 2026-09-07：**V4 增强批完成**（GAP-16/17/18/19/20/21①/22）。验收 `test_v4_enhancements.py` 50 项断言全绿 + V1/V2/V3/U 系共 11 套全量回归无破坏。要点：
  - GAP-16：`/api/map/files`（GPS pin，上限 5000）；地图视图模态（Leaflet CDN 懒加载，离线优雅降级）；属性面板 GPS 坐标 + "在地图中查看"外链。
  - GAP-17：新模块 `ocr_engine.py`——macOS Vision（pyobjc）→ RapidOCR 运行时探测降级；`ocr_text` 表 + FTS 升级重建（新增 ocr 列，DROP+等价回填）；`/api/ocr/status|run|text`；config `ocr_enabled`（默认关）扫描器自动投递（≤800/次）+ 属性面板手动"识别图内文字"；全库搜索直接命中图内文字。
  - GAP-18：`export_presets` 表 + 预设 CRUD + `/api/export/run`（EXIF 转置/缩放/JPEG·PNG·WebP/质量/文字水印九宫格+平铺/重名自动后缀，上限 2000）；导出模态（选中工具栏入口）+ 保存/应用预设。视频水印经 format_factory 转码覆盖（不重复建设）。
  - GAP-19：`markers` 表 + CRUD + `/api/markers/export`（ffmpeg -ss/-to -c copy 无损优先）。播放器内 I/O 打点 UI 需 video_pro Qt 深改，**待人工接入**（API 已可用的；属性面板/右键可后续消费）。
  - GAP-20：幻灯片背景音乐（取当前视图首个音频循环播放，随放映启停）+ 灯箱淡入过渡。
  - GAP-21（第一步）：新模块 `ai_tagging.py`——对接宿主 topos-vault（config `ai_vault_url`，缺省关闭），标签 `ai:` 前缀独立命名空间、不污染人工标签；扫描器每次 ≤200 张；人脸识别（本地模型）保持远期。
  - GAP-22：文件夹软锁（PBKDF2 口令 + 会话级解锁内存态）；`/api/list` 423、`/api/thumb` 403、`/api/file` 423 三端守卫；右键"设置密码保护" + 进入锁定目录弹解锁流程（3 次重试）；文档如实标注 UI 级软锁非加密。
  - 实施期修复：markers GET 列索引错位、`_apply_watermark` 缺 PIL 导入、`compare_digest`（hmac）/`pbkdf2_hmac`（hashlib）模块归属。
- 2026-09-07：**V5 复查批完成**（V1–V4 全量交叉审查 + 修复）。三路并行审查（后端路由/后端模块/前端 JS）共发现并修复 30+ 项，验收 `test_v5_review.py` 53 项断言全绿 + 全部 13 套回归无破坏。要点：
  - P1×5：前端 `sizeParam` 块级作用域 ReferenceError（图片/视频网格整体瘫痪）；scanner `scan_directory` 把子目录深层文件误标 missing=1（LIKE 后缀匹配 vs 直接子文件集）；全量扫描从不复位 missing（卷恢复不自愈）；枚举截断（>20 万文件）后仍判失联；`meta_sync.import_from_files` 达 cap 提前返回丢提交（全部采纳回滚）。
  - P1 数据丢失×1：集合失联清理实现在回收站/离线卷/NFD 卷三场景下误删成员行（docstring 承诺的回收站保留分支从未实现）——补 trash 表查询 + `_volume_available` + NFD 探测三重防御。
  - P2×9：FTS 触发器不带 ocr 列（任何 tags 更新抹掉 OCR 索引，改为从 ocr_text 取权威值 + DROP 重建升级）；`ai:` 前缀 `lstrip` 字符集剥离（'animal'→'nimal'）；XMP 清空值不写删除参数（删掉的标签反向导入复活）；`_local_cache_dirs` 剪枝剪掉 `.cache`（本地缓存体积恒 0）；批量重命名 replace/append 产物路径逃逸；`{ext:upper}` 识别不到致扩展名双拼；NFD 卷 meta_map 键不匹配（整目录元数据显示空）；递归视图缓存 key 永不失效；batch/meta rating 无值域校验。
  - P2 前端×3：堆叠角标 `data-stack-id` 不在委托协议（点击永久无效，改 data-args）；`runOcrPanel` 签名与委托传参错配（OCR 按钮无效）；collection://、similar:// 视图漏虚拟视图守卫五处（新建/粘贴/拖拽上传/系统打开/重复查找/路径回车，统一 `isVirtualView` 五 scheme）。
  - 软锁硬化：写 API 全量接 `_lock_guard`（rename/delete/move/rotate/upload/mkdir/paste/trim/import/export/ocr/markers/update_meta 等 423）；读侧搜索/智能文件夹/地图/OCR 文本/深度元数据排除锁定目录；重锁使旧解锁凭据失效。
  - P3 若干：AI vault 不可达熔断（不再 200×6s 空等）、watcher `_watched` 按 Qt 实际监视集修剪（重建目录恢复监视）+ GUI 线程校验、OCR 先建 file_meta 行消重复 FTS 行、rename_template 修饰符对 date/seq 生效 + strftime 冒号保留、导出单轴缩放/PNG→JPEG RGB 转换/位图字体 anchor 降级、导入 mtime 兜底改本地日期、stats/map/search 排除 missing=1 幽灵行、markers/export 区间校验、导出后缀路径校验、空堆叠清理、地图 marker layerGroup 防叠加、智能文件夹缓存未命中重拉、`pendingSelection` 虚拟视图清滞留、导出快照过期刷新、meta_map NFD 键统一。
