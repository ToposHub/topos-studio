# Files 替代 D1 批次验证记录（transport 换轨）

日期：2026-11-17。对应[总计划](../../plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) D 阶段第一刀。
提交：`e436dfee`（facade 扩展）、`48108e08`（transport 换轨 + 测试）。

## 这一批做了什么

`NativeFilesTransport` 的**信号契约与公共方法签名一字未动**，内部换轨：
数据面不再依赖旧 HTTP 服务，走 C2 `FilesFacade`（同进程有界 worker、
代际作废）与本地文件直读。`FilesPanel`/`FilesPluginsController` 零改动。

## 路由映射（25 条）

| 路由 | 新后端 | 说明 |
| --- | --- | --- |
| `/api/list` | `facade.list_directory`（按路径解析归属库叠加） | loaded 载荷为 Entry DTO（面板已按新契约消费） |
| `/api/tree` | `facade.tree` | 在途集合封顶 MAX_TREE_QUEUE；幽灵目录=空列表（如实） |
| `/api/search` | `facade.search(library_id="")` 全库 | 无固定库时明确指引（不再空结果冒充） |
| `/api/collections`、`/api/collections/{id}/items` | `facade.collection_members` | 归属库内部解析；消失成员保留 OFFLINE 不静默丢行 |
| `/api/update_meta`、`/api/batch/meta` | `facade.patch_metadata`（按库分组一次语义） | 路径不在任何固定库→逐条失败+指引；快照回显 |
| `/api/tasks/start`/`status`/`cancel` | `facade.start_task`（job_id 预生成+逐项进度） | 无轮询：进度回调→task_started；终态 task_finished 逐项 |
| `/api/library/list`/`add`/`unpin` | `FilesLibraryService`（新注册表） | 破坏性升级：旧常用位置配置不是输入 |
| `/api/scan/status`/`start`/`cancel` | `facade.scan_libraries` | 无固定库→scan_failed 指引；进度/取消/完成如实 |
| `/api/capabilities` | 静态清单（本进程服务自证） | metadata.patch/files.task/library.search |
| `/api/batch/rename/preview`、`/api/batch/rename` | `facade.batch_rename_preview/execute`（B6） | plan_id 按会话暂存；STALE_PLAN/指纹校验兜底 |
| `/api/file`（预览/比较） | `read_file_bytes` 本地直读 | 64MB 上限；超限/缺失/无权限结构化拒绝；比较会话独立 key |
| `/api/thumb`、`/api/thumb/retry`、`/api/thumb/strip` | **保留 HTTP 桥** | 等 E 阶段媒体 provider 接管 |
| `/api/plugin/{id}/launch` | **保留 HTTP 桥** | 等 F/G 阶段插件宿主接管 |

## 关键语义决定

- **导航代际**：`cancel()` 同时自增 `_nav_generation`——迟到 facade 结果
  在投递门丢弃，与旧 HTTP abort 语义等价（FR-09/14/17 回归保持）。
- **同路径预览重开**：facade `read:{path}` 键作废 + `_preview_seq` 双保险。
- **比较会话**：`compare:{session}:{seq}` 独立键——两个窗口含同一文件互不顶掉。
- **扫描进度**：`FilesScanService.progress` 回调（chunk=200）驱动
  `scan_status_loaded`；取消为协作式（目录边界生效）。
- **任务失败载荷**：`failures` 逐项带 `error`+`code`；`cancelled` 由
  CANCELLED 条目汇总；不宣布假成功。
- **错误文案**：不再出现"打开完整 Files"回旧页指引（D 失败策略要求）。
- `{seq:N}` 模板宽度、tags 逗号分隔文本 + `clear`、mkdir `name` 选项、
  集合 `item_count`/`library_of` 为配套小口（tests/files 全绿）。

## 验证结果

- `tests/ui/test_native_files_transport.py` **38 passed**：数据面用真实
  catalog+合成素材；`_NoNetwork` 哨兵证明数据路由零 HTTP 依赖；
  缩略图桥保留本地 HTTP 协议夹具（旧 12 项语义原样）。
- FR-09/13/14/15/16/17 回归（`test_files_location_regressions.py` 12、
  `test_files_session_regressions.py` 14）按**同不变量**换 facade 口径。
- 受影响面合计：**396 passed / 1 skipped**（native UI 13 个文件 + tests/files
  全量含冻结基线）+ 布局/播放器 44 passed。
- `tests/files` 244 passed（D1a 扩展：tree/read_file/collection_members/
  batch_rename/scan_libraries/run + 服务补口）。

## 诚实边界

- **D-1（FilesPanel 独立宿主）未完成**：面板仍默认启动旧 server/建隐藏
  browser/保留模式开关——缩略图/胶片条桥仍需旧服务在线。数据面已就绪，
  等 E 的媒体 provider 后即可摘除启动链（本批为该刀切好了伤口）。
- 旧 server 未运行时：原生模式浏览/搜索/集合/元数据/任务/扫描/批改名
  全部可用；缩略图退化为占位（`thumbnail_failed` 如实）、胶片条不可用。
- 用户元数据只对**已固定（新注册表）且已扫描**的库生效——破坏性升级
  授权：旧 DB/旧常用位置配置不是输入；失败文案给出固定+索引指引。
- 任务为同步执行（B/C 语义）：进度按条目推进，无跨进程任务表落盘
  （B3 journal 落盘，重启恢复属 D 后续任务列表项）。
- 实机键鼠验收未做（D 验收要求整段完成后统一补）。

## 隐私

仅源码/测试/Markdown；合成素材与临时目录，无真实媒体路径。
