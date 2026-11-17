# F 阶段第十四批（F14）：安装事务持久日志、fsync/目录同步与崩溃恢复

日期：2026-11-18 · 分支：fix-ui · 范围：`installer.py` 的
`TransactionJournal` + 恢复钩子 + 目录同步。PL-08 事务已有锁/回滚/清扫，
但崩溃后只有目录残留清扫——没有**持久账本**与按账本的一致性收尾。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `installer.py` | ① **`TransactionJournal`**：append-only JSONL（`.transactions.log`），**每条 fsync**（崩溃后日志可信）；begin{token, plugin_id, previous_version}/committed/aborted{reason}/recovered{outcome}；截断尾行容忍（下一条 begin 重新记账）；`pending_tokens` 找无收尾事务。② **`fsync_directory`**（POSIX `O_DIRECTORY+fsync`；Windows 无等价如实注释）：committed 前同步插件目录——指针落盘后事务才算完成；恢复前滚/回滚路径同样同步。③ **崩溃恢复**（挂 `_sweep_transactions`，下一事务启动时执行）：按**磁盘事实**判定（不按日志自称）——current==previous → `aborted:before-swap`（交换从未发生）；current 有效且 **provenance 必在**（安装器总在交换前写 sidecar——无 sidecar 的版本不可能出自完整事务）且摘要一致 → 前滚 `committed:<v>`；不符 → 回指 previous_version（内容完整保留）/ 无前版则**隔离坏 payload**（`.quarantined-<token>`）并摘指针；回滚失败如实留账可重试。④ 失败路径即时 `aborted` 记账（非崩溃失败不进恢复队列） |

## 验证（tests/plugins/test_install_journal_f14.py，9 项）

- 全程记账（两事务各 begin+committed、令牌唯一、pending 空）+ 校验失败
  即时 aborted；
- **崩溃截断尾行容忍**（半行 JSON 后 pending 仍可判）；
- **交换后崩溃**（真实安装后抹掉收尾行模拟）→ 恢复前滚
  `committed:2.0.0`，随后安装正常；
- **交换前崩溃**（手工挂起 begin）→ `aborted:before-swap`，安装根干净；
- **payload 位腐**（改字节 + 抹收尾）→ `rolled_back:1.0.0`，旧版完整、
  后续安装成功；
- **首装崩溃 + 坏指针无前版** → 坏 payload 隔离可查、指针摘除；
- fsync 目录不抛错（含缺失目录）；committed 后新实例（=新进程视角）
  读到一致 current/payload/provenance。

回归：tests/plugins + tests/files 全量 —— **750 通过**。

## 诚实边界

- 崩溃注入为"真实落盘后抹掉收尾行/改字节"的确定性模拟（等价于进程在
  对应阶段死亡）；真断电/kill -9 中途的模糊测试未做。
- Windows 无目录 fsync 等价（注释说明）——日志逐条 fsync + 恢复按磁盘
  事实判定兜底。
- 恢复只处理本安装器事务；**不承担旧安装目录兼容**（无 sidecar 的目录
  一律按不可信处理，符合授权）。

## 隐私

合成包/随机字节载荷/临时目录；无隐私路径。
