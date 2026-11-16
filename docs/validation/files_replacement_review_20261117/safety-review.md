# Files 安全与工程事务复核证据

日期：2026-11-17；读取基线：`7f025973`。本文件是审查记录，不是实施指令。
只新增本目录证据文件；实验写入均为 `TemporaryDirectory` 和隔离 SQLite，未操作真实素材/用户库。

## 已执行

```sh
.venv/bin/python -m pytest -q tests/files/test_file_safety_regressions.py tests/files/test_file_tasks.py tests/edit/test_external_import_compensation.py
# 33 passed in 2.21s（审查时现有测试）
.venv/bin/python docs/validation/files_replacement_review_20261117/repro_safety.py > docs/validation/files_replacement_review_20261117/repro_safety.json
```

`repro_safety.py` 可重跑；实际 JSON 输出见同目录 `repro_safety.json`。
注入 EXDEV 与目标替换只验证相应代码分支，不冒充真实跨卷/跨进程/坏盘验收。

## 结论

| 范围 | 状态 | 证据 |
| --- | --- | --- |
| FR-01 | 部分，P0 尚未关闭 | `file_tasks.py:550–555` 的 O_EXCL 占位后仍用可覆盖的 rename；注入占位被另一写入者替换时，error 策略仍覆盖第三方内容并删除源。仅进程内目标预约不等于跨进程 no-replace，也未协调所有源/目录树读写。 |
| FR-02 | 目标末级链接已改善，未完整验收 | overwrite copy 使用同目录临时文件 + replace，可保护目标末级 symlink/hardlink 指向的内容，现有测试通过；父目录换链与 source 身份变化无完整覆盖。顶层 copy symlink 仍走文件复制并跟随源，与树内仅复制链接的政策不一致。 |
| FR-03 | helper 已改，实际入口未贯通 | `_move_meta_rows` 修正列映射、单事务回滚；但 `routes.py:386` 单改名与 `:852` 批改名仍各自修改部分表并吞 DB 错误。通过真实 Flask test client 执行，成功响应后 file_meta 跟到新路径，markers 留旧路径。 |
| FR-04 | v2 快照部分可用，完整关系恢复未完成 | 成功 delete/restore 后 collection_items 的 sort_order/added_at 从 17/123 变 0/null，stacks.created_at 从 456 变 null；`trash.py:146,151,369,373` 只保存部分关系字段。`:130–167` 快照失败仅 warning 仍继续，`:187–200` 部分关系清理失败也继续；`:411–425` restore 仍检查 exists 后 shutil.move，未进入安全目标预约。 |
| FR-05 | 已复现的 mkdir/rename/restore 锁绕过已修 | 3 个真实 Flask/临时 SQLite 锁用例通过；策略仍在 HTTP 层，domain service 无统一锁校验，锁检查至任务执行之间存在时间窗口，完整目标类型/链接/权限矩阵未验。 |
| FR-06 | 补偿已实现但覆盖不足 | 3 个测试验证宏失败和复用资产；AST 调用真实方法，将 clip 构造改为抛 RuntimeError 后异常外溢，新增 asset 遗留 1、工程已发布 1 次且无失败提示。`timeline_view.py:17049–17061` 只有 finally；`:17031` 暂存轨道 apply 异常同样未补偿。导入不在 Undo 宏内仍未解决。 |
| FR-07 | 池与文件分块已有，runtime/树任务不完整 | 池上限、单文件中途取消、活动任务不淘汰、显式 shutdown 测试通过；目录第二项注入取消后首项残留。`file_tasks.py:528–533,593–599` 只移除空目录。未查到实际宿主退出调用本服务 shutdown；ThreadPoolExecutor 仅限制执行线程，未限制提交队列，未实现重启恢复。 |

另外：`file_tasks.py:616–619` 的目录 rename 误走文件占位路径，临时目录实测 `NotADirectoryError`，源保留；`:546–548` overwrite move 无跨卷回退，注入 EXDEV 直接失败，源保留。两项应随安全任务服务整体修正，避免只修入口而扩大未验证的目录写入行为。

## 验收建议

安全任务服务需形成统一读写/源身份/目录树/跨卷政策，并使用真正的原子 no-replace 与可恢复 staging；所有改名和 restore 入口复用同一领域实现。回收站必须先完整快照成功，失败停止删除；保存完整关系字段和未知字段，恢复冲突纳入同一安全目标策略。工程导入应通过真正的工程级 command/transaction 验证所有构造/取消/宏失败/Undo/Redo 分支，以及 assets/bins/revision/dirty/preview 一致性。
