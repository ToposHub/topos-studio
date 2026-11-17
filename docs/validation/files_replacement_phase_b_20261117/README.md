# Files 替代 B 阶段验证记录

日期：2026-11-17。基线 `a36b96cf` → B 阶段提交 `194d1f2f`…`5a74f165` + 本批。对应[总计划](../../plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) B 阶段。

## 交付物

| 批次 | 提交 | 模块 | 测试 |
| --- | --- | --- | --- |
| B1 定位器/新库/CWD 双库消除 | `194d1f2f` | `src/features/files/repository/{locator,catalog}.py`、`src/shared/paths/paths.py`、宿主 4 调用点 | `tests/files/test_files_repository.py` 19 |
| B2 安全文件事务核心 | `14002229` | `src/features/files/transactions/ops.py` | `tests/files/test_file_transactions.py` 21（B7 补目录重叠后 22） |
| B3 持久操作日志 | `619a478e` | `transactions/journal.py` | `tests/files/test_operation_journal.py` 12 |
| B4 回收站版本化清单 | `1a8dafce` | `transactions/trash_service.py` + catalog 关系助手 | `tests/files/test_trash_service.py` 9 |
| B5 统一入口+关系随行 | `b48f22db` | `transactions/service.py` + `catalog.move_subtree` | `tests/files/test_file_operation_service.py` 6 |
| B6 批改名计划协议 | `5a74f165` | `transactions/batch_plan.py` | `tests/files/test_batch_rename_plan.py` 8 |
| B7 目录重叠防护+整批验证 | 本批 | `ops.py` 重叠拒绝 | `test_file_transactions.py` +1 |

## 验证结果

- B 阶段新增测试合计 **77 项，全部通过**（隔离夹具：pytest 临时目录 + 临时 catalog.db + 合成文件）。
- 整批范围（tests/plugins + tests/files + tests/ui/test_native_files_* + 播放语义/播放器宿主 + 启动回归）：**745 passed / 0 failed / 1 skipped，33.24s**（skip=A 阶段 exiftool 依赖项）。
- A 阶段冻结基线（旧系统行为）在 B 阶段全部改动后仍通过——新代码零触碰旧系统运行路径。
- `src/app` 启动回归（test_main_startup 12 项）通过：宿主库显式路径切换无回归。

## 关键不变量与证据测试

| 不变量 | 证据 |
| --- | --- |
| 文件 no-replace 内核级（link EEXIST；inode 核验） | `test_move_never_replaces_existing_file`、`test_same_volume_move_preserves_inode_and_content` |
| 目录无内核原子被如实标注（检查+rename+核验+journal 补偿） | `ops.py` 模块 docstring + `_transfer_dir`；`test_directory_move_conflict_and_success` |
| 符号链接：源拒绝解引用/目标拒绝穿过/目录拷贝跳过 | `test_symlink_source_is_rejected`、`test_symlink_destination_is_never_traversed`、`test_copy_skips_symlink_children_in_directories` |
| 取消不留半成品（块边界+staging 清理） | `test_cancel_mid_copy_leaves_no_destination` |
| 权限/磁盘满结构化分类不伪装成功 | `test_permission_error_maps_to_code`、`test_disk_full_maps_to_code` |
| 目录重叠（目标在源子树内/自引用）拒绝 | `test_directory_move_into_own_subtree_is_rejected` |
| 快照失败禁止删除 | `test_manifest_failure_forbids_deletion` |
| 恢复冲突：error 保持可重试 / rename 挂最终身份 | `test_restore_conflict_error_policy_stays_retryable`、`test_restore_conflict_rename_policy_relinks_to_final_identity` |
| 批改名 STALE_PLAN 整体拒绝不偷跑 | `test_execute_rejects_stale_sources` |
| 幂等重放不重复执行 | `test_duplicate_submission_is_idempotent`、`test_idempotency_key_replays_existing_job` |
| 子树全量随行且前缀相似项不受影响 | `test_directory_move_remaps_whole_subtree` |
| journal 有界+未终态永不淘汰 | `test_trim_keeps_bound_and_never_drops_active` |
| 恢复只认磁盘证据，模糊保持 pending_recovery | `test_recovery_ambiguous_stays_pending` |
| 生产代码禁止 CWD 默认库（AST 守卫） | `test_production_code_has_no_cwd_default_database` |

## 诚实边界

- **契约测试口径**：FileOperationService 尚无生产调用方；原生 UI/transport 仍走旧 HTTP，C 阶段替换后才构成真实闭环。
- 跨卷路径经 `same_volume=False` 注入全真演练（staging+link 发布与清理均为真实文件系统操作），**未在真实双块设备上验证 EXDEV**；I 阶段平台矩阵补。
- 目录 rename 的原子性是检查+核验+journal 口径，非内核原子（文件才是内核级 no-replace）。
- Windows（无 os.link 语义差异/无 fcntl）未实现未验证；总计划 F 阶段安装锁条目同批处理。
- OCR（schema 预留）与 markers（无调用方死代码）按矩阵决定不迁移/延后 U4。

## 隐私

仅提交 Python 源码、测试与 Markdown；全部夹具为合成字节/临时目录；无图片、媒体、截图或用户路径。
