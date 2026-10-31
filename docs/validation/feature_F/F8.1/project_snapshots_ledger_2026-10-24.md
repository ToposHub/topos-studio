# F8.1 · 命名工程快照与版本比较 — 验证台账

- 日期：2026-10-24；计划条目：§F8.1
- 测试：tests/edit/test_f81_project_snapshots.py（14）全绿

## 1. 交付

| 组件 | 内容 |
|---|---|
| 工程快照模型 | `project_snapshots.py`：`take_snapshot`（命名版本/自动备份区分；重名拒绝/auto 可覆盖）——payload=工程逻辑数据（时间线+栈+组表+版本表，**内容寻址** SHA256 哈希即身份）；`source_revision` + `created_at` + 依赖清单（素材路径+LUT） |
| 隐私守卫 | `_assert_no_secrets`：payload 键名含 token/secret/password/api_key/credential 即拒绝（不存 secret） |
| 恢复 | `restore_snapshot`：payload 哈希校验（篡改拒绝）→ 模型级替换（**不污染当前 Undo**——命令层包裹语义）；**依赖缺失进报告不阻断**（原片移动/插件缺失仍保留版本元数据）；快照表随工程走（可再回退/复制分支=恢复后重存新名） |
| 只读比较 | `compare_snapshots`：结构化 diff——clip_moved/trimmed/replaced/added/removed、effect_changed、grade_changed、audio_changed、timeline_added/removed；summary 计数；**不做三方合并**（计划原文初版边界） |
| 存储 | `EditProject.project_snapshots` 字段（settings 键模式；旧工程空表）——不复制媒体/代理/缓存（依赖外置清单） |

## 2. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 命名版本可重开、复制分支和回退 | restore 往返 + 快照表随工程走（再回退）；复制分支=恢复后以新名重存（模型支持） |
| 原片移动/插件缺失仍保留版本元数据并报告依赖 | `test_missing_dependency_reported_not_fatal`（移盘后恢复：缺失清单进报告，快照元数据保留） |
| 比较结果与真实差异一致 | 9 项 diff 测试（移动/修剪/换源/效果/调色/音频/增删/恒等空 diff/时间线增删） |
| 不复制巨大临时缓存或 secret | payload=工程逻辑数据（媒体字节外置）+ secret 键拒绝测试 |
| 不污染当前 Undo | 恢复是模型级替换（快照/恢复不经命令栈内嵌——undo 语义由调用方命令包裹，与 F4.2 同模式） |
| 区分自动备份/命名版本/调色版本 | auto_backup 标记 + F4.2 栈级版本（三层各归其位） |

## 3. 登记的后续片（不阻塞验收）

1. 工程管理 UI：快照列表/比较面板/恢复入口（模型已就绪）。
2. 自动备份的定时策略（auto_backup 标记已有，调度归 UI 片）。
3. 三方合并（计划明文"不急做"）。
4. IO 序列化接线（快照表随工程保存/重开——字段已挂，settings 键接线一片）。
