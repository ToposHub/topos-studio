# Files 替代 C 阶段验证记录

日期：2026-11-17。对应[总计划](../../plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) C 阶段。

## 交付物

| 批次 | 提交 | 模块 | 测试 |
| --- | --- | --- | --- |
| C1 领域服务 | `a4bd6dce` | `src/features/files/services/{query,domain}.py` + catalog revision | `tests/files/test_files_domain_services.py` 13 |
| C2 异步 facade | `c16599c2` | `services/facade.py` | `tests/files/test_files_facade.py` 6 |
| C3 事件总线 | `06086721` | `services/events.py` | `tests/files/test_files_event_bus.py` 6 |
| C4 工程导入原子性复核 | 本批 | （既有 B08 路径，复核+回归确认） | `test_external_import_compensation.py` 3 + `test_timeline_external_file_drop.py` 8 |
| C5 Undo/Redo 覆盖新增素材 | 本批 | `commands/asset_commands.py::ImportDropMacroCommand`、`commands/__init__.py`（replace 保子类）、`timeline_view.py`（投放宏接线+`_undo/_redo_imported_assets`） | `tests/edit/test_import_undo_covers_assets.py` 4 |

## 验证结果

- C 阶段新增 **29 项测试全部通过**。
- 定向回归（命令/宏/撤销/投放/补偿/资源池）：**154 passed**。
- `tests/edit` 全量：**13601 passed / 25 failed / 65 skipped，28:26**。25 个失败已归因：
  - `test_channel_layout_import.py::…channel_layout_71`、`test_timeline_disable_shortcut.py::…plain_d…`、`test_timeline_pointer_and_chrome.py`（全量跑时）三个单独复跑均**在干净树（stash 本轮改动）同样失败**——先于本轮，与本轮无关；
  - 其余为全量顺序/环境脆弱（该现象在既往批次已有记录），定向复跑通过。
- `MacroCommand.apply` 重建改 `dataclasses.replace`：对裸宏等价（同字段拷贝），对子类保字段——`test_macro_subclass_survives_subcommand_self_update` 直接验证。

## C4 事实复核（既有实现，非本轮新写）

`_finish_external_import`（timeline_view）七类守卫：stale job_id 丢弃、取消不落轨、工程切换（身份+project_id）作废、时间线 revision 变化作废、目标轨道删除/锁定作废、宏失败逆序回滚时间线+补偿新导入素材、复用素材（is_new=False）永不删除。提示与实际状态一致（FR-06）。

## C5 语义

- 撤销：宏子命令逆序回滚时间线后，移除本次新导入素材；素材仍被其他片段引用时跳过（不产生悬挂引用）且不发布空变更。
- 重做：子命令重放后按**原 asset_id** 复位素材（`import_asset` 会换 UUID 破坏 clip 引用，故直接对象复位+root bin 成员关系），幂等 ensure 兼容首次执行。
- 子命令自我更新路径：宏重建经 `replace(self, …)` 保留子类回调（防退化为裸宏丢补偿）。

## 诚实边界

- C1/C2/C3 为**契约测试口径**：facade/服务/事件均无生产 UI 调用方；D 阶段把 FilesPanel/transport 切到 facade 且删除旧 HTTP 依赖后才构成真实闭环。
- thumbnail-preview / import-export / jobs 服务未实现（依赖 E 的 provider/任务框架）。
- C5 的 revision/dirty 一致性由既有 `_bump_revision_at_commit` 机制承担；preview/通知一致性未做实机验收。
- 25 个全量失败清单未逐条在干净树复跑（样本 3 条已确认），如需最终封口按既往方法论做 worktree 全量对照。

## 隐私

仅源码/测试/Markdown；合成素材与临时目录。
