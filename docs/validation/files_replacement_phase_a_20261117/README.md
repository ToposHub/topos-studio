# Files 替代 A 阶段冻结基线验证记录

日期：2026-11-17。基线 `a36b96cf`。对应[总计划](../../plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) A 阶段第 4 项：以临时库和合成素材冻结回归。

## 冻结范围与测试映射

总计划要求冻结七项流程；五项本轮新增隔离冻结，两项复用既有冻结测试（Qt 侧，不重复）：

| 流程 | 冻结测试 | 口径 |
| --- | --- | --- |
| 浏览 | `tests/files/test_replacement_baseline_freeze.py::test_freeze_browse_lists_synthetic_library`、`test_freeze_tree_lists_child_directories`、`test_freeze_scan_indexes_files_with_size` | 真实 Flask 应用 + 临时 SQLite（重定向 `_topos_db_path`/`CONFIG_FILE`/`get_trash_dir`）+ 合成 PNG/JPG/TXT |
| 写 metadata | `test_freeze_update_meta_persists_user_fields_and_fts`、`test_freeze_batch_meta_only_touches_patched_fields`、`test_freeze_update_meta_sidecar_mode_writes_xmp`（exiftool 缺失时 skip——旧系统静默跳过写回，依赖已登记矩阵 §1 行 15） | DB 行 + FTS 检索 + 字段级 patch + XMP 旁车 |
| 改名/移动 | `test_freeze_rename_moves_file_and_metadata_together`、`test_freeze_move_into_subdirectory_keeps_identity` | 文件系统 + DB 身份随行 + 旧 path 行清除 |
| 删/恢复 | `test_freeze_trash_then_restore_roundtrip`（含 meta 快照往返）、`test_freeze_purge_removes_permanently` | 真实 `.trash` 目录 + trash 表快照 |
| 任务通道语义 | `test_freeze_task_rejects_unknown_operation`、`test_freeze_capabilities_surface_stable` | 词表与能力面 |
| 工程导入 | 既有 `tests/files/test_import_job.py`（10 项：探测/取消/部分成功/去重/复用资产） | 既有冻结，A 阶段引用不重写 |
| 播放器及关闭 | 既有 `tests/ui/test_native_player_host.py`、`tests/ui/test_playback_semantics_regressions.py`（16 项）、`tests/ui/test_native_files_*`（含图片工厂关闭） | 既有冻结，A 阶段引用不重写 |

## 验证结果

- `tests/files/test_replacement_baseline_freeze.py`：**12 收集 / 11 passed / 1 skipped，1.36s**（skip=sidecar·exiftool 不在本机）。
- 隔离性：DB/config/回收站/合成素材全部位于 pytest 临时目录；不读写仓库 `topos.db` 与用户素材；`DIR_CACHE` 每用例前后清理。
- 其余 A 阶段交付（清点+矩阵+数据边界+备份设计）为文档，验证方式为关键锚点人工复核（`db.py:15-34`、`database.py:40`、manifest `write_semantics`、任务选项 `new_name`/`destination`、回收站 id/原路径双入口均已逐一读码确认）。

## 四种证据对照（总计划 §3）

- 实现与生产调用证据：[能力矩阵](../../plans/files-replacement-capability-matrix-20261117.md)（108 路由/66 能力/10 插件/7 桥接的调用方与去向）。
- 自动化证据：本文件上表。
- 实机证据：不适用——A 阶段无设备/平台条目；真实 GPU/声卡属 E/I 待验。
- 无旧扩展证据：I 阶段汇总；A 阶段不产生该证据。

## 待决定项（阻塞，等待用户范围决定）

矩阵 §2 共 24 行非必须保留能力中，标"待决定"的包括：时间线视图、递归聚合、智能文件夹、层级标签树 UI、堆叠、批量旋转、拖拽上传、软锁、导入向导、导出水印、重复/相似/主色、OCR、地图、幻灯片+BGM、截帧。未决定前不实现、不删除。

## 隐私

本阶段只提交 Markdown 与测试代码；合成素材由测试即时生成于临时目录；无图片/视频/截图/用户路径入库。
