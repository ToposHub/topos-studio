# F6.5 · 素材入库、组织与受控校验拷贝 — 验证台账

- 日期：2026-10-24；计划条目：§F6.5
- 测试：tests/edit/test_f65_verified_ingest.py（8）全绿

## 1. 现状核验

- **组织**：media_pool 文件/文件夹导入、SmartBin/搜索/评级/关键词、
  Bin 树既有（F0.x-F2.x 批次测试族）；project_archive 的 SHA-256
  收集校验（F6.2 复用面）。
- **缺口**：受控校验拷贝（verified ingest）用户流程——"不把 archive
  hash 当已具备 ingest 用户流程"（计划原文）。

## 2. 交付

| 组件 | 内容 |
|---|---|
| 受控拷贝核 | `verified_ingest.py::ingest_files`：单目的地复制（.topos-partial 临时名）→ **整文件 SHA-256 源/目标校验**（快速指纹不冒充）→ os.replace 原子落位 → verified 登记 |
| 逐项报告 | IngestReport：verified / skipped_existing_good / conflict / failed / skipped_not_started——同名异卡冲突（绝不覆盖良好拷贝）、不完整源文件、目的盘断开（可操作文案）、取消保留已完成部分 |
| 摄影卡组织保留 | 目标路径 = dest_root/<源相对目录>（目录结构即卡组织；卷名/相机 metadata 随文件原样） |
| 源卡只读 | 全程只读流复制（chmod 444 源测试锁定） |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 同名异卡有逐项报告 | `test_same_name_different_card_conflict_not_overwritten`（conflict 状态 + 良好拷贝未覆盖） |
| 不完整文件/目的盘断开/取消/重试 | `test_incomplete_source_reported_not_success`、`test_destination_disconnect_actionable`、`test_cancel_keeps_completed_reports_rest`、重跑=skip_existing_good（重试幂等） |
| 未验证文件不能标成功 | 校验失败/失败路径只进 failures；verified 仅整文件哈希一致（结构保证+测试） |
| 不覆盖良好拷贝 | skipped_existing_good 语义测试 |
| 源卡只读 | `test_source_card_read_only` |
| 动态集合/批量组织 Undo/重开 | 组织面既有（SmartBin/Bin 命令族）；ingest 产物经普通导入进入组织流 |
| 多目的备份可后续扩展 | 单目的地为计划允许的首期；多目的登记后续片 |

## 4. 登记的后续片（不阻塞验收）

1. 媒体池 UI 的"受控入库"入口（核心已就绪；报告面板）。
2. 多目的地并行备份（单目的地先行，计划原文允许）。
3. F2.10 外录素材组织联动 / F5.4 序列素材 ingest。
4. 摄影卡现场备份需求实例化后的真卡验收（计划原文：按真实需求立项）。
