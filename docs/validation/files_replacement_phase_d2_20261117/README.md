# Files 替代 D2 批次验证记录（回收站 + 集合写入入口）

日期：2026-11-17。对应[总计划](../../plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) D 表第 6 项。

## 交付物

| 层 | 内容 |
| --- | --- |
| catalog | `rename_collection` / `remove_from_collection` / `delete_collection`（成员随 FK 级联清除，不触及资产本体） |
| 服务 | `FilesCollectionsService.rename_collection/remove_members/delete_collection`；`FilesTaskService.list_trash`（B4 manifest 投影） |
| transport | `load_trash`（trash_loaded/trash_failed）；`create/rename/add_members/remove_members/delete_collection`（collection_changed + 自动刷新清单）；恢复/彻底删除复用 `run_task` 的逐项结果 |
| UI | `native_files_trash.TrashDialog`（清单/恢复/改名恢复/彻底删除带确认；DEST_EXISTS 显式指引改用改名恢复）；面板工具栏「回收站」；集合菜单：从所选新建/打开/加入所选/改名/删除；集合视图右键「从集合移除」；空集合提示不再指向完整 Files |
| 接线 | FilesPanel：`_open_trash`（任务结果→对话框+回刷）、`_on_collection_changed`（成功刷新清单与当前视图） |

## 验证

- services：`tests/files/test_files_domain_services.py` +2（改名/成员/删除级联/重名 CONFLICT；list_trash）。
- transport：`test_native_files_transport.py` +4（回收站往返/恢复冲突→改名恢复并存/彻底删除/集合写全往返）。
- UI：`test_files_trash_and_collections_ui.py` 10 项（对话框意图+确认拒绝、面板入口意图、
  集合菜单结构、反馈文案）。
- 受影响面：**347 passed / 1 skipped + 65 passed**（含 D1 全部、面板/插件/元数据等 8 套）。

## 诚实边界

- 「锁定原因/解锁」未实现：新系统尚无目录锁服务（`FilesErrorCode.LOCKED`
  仅保留码位；旧锁为旧系统能力，是否保留属矩阵 §2 待决定项）——本项因此
  记**部分**，不冒充完成。
- TrashDialog 的任务结果反馈依赖 `task_finished` 广播（restore/purge 过滤），
  多开对话框会同时刷新——列表内容一致无害；逐对话框任务归属账本属 D 表
  第 5 项（原生任务列表）。
- 集合新建的归属库按首个成员解析；空集合归属空库（列表可见、可改名删除）。

## 隐私

仅源码/测试/Markdown；合成素材与临时目录。
