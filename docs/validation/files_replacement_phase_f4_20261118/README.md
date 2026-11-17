# F 阶段第四批（F4）：权威复核与调度的原子边界

日期：2026-11-18 · 分支：fix-ui · 范围：`src/shared/plugins/dispatch.py`
（新）+ `LegacyFilesCommandService.dispatch` 重写 + catalog 廉价探测 +
控制器启停走边界。

## 缺口

revalidate（worker）→dispatch（GUI）是开放窗口：停用/切库/撤权落在窗口
里时，dispatch 侧逐项复查仍可能与变更交错（复查通过→变更落地→提交
发出）。F3 用"dispatch 前再比对"收窄但非原子；DTO 票据（revalidated
标记/弱引用表）只证明"曾经复核过"，不证明"提交时仍然成立"。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `dispatch.py`（新） | `DispatchAuthority`：唯一权威锁 + 单调 epoch。**check_and_submit**——锁内 epoch 比对 + 业务复核 + 提交一气呵成（复核与提交之间插不进任何经边界的变更）；**mutate/invalidate**——停用/切库/撤权经同一把锁串行并推进 epoch。锁纪律：临界区只做判定与提交 |
| `plugin_launch_service.py` | ① `prepare` 冻结 `host_revision=authority.epoch`。② `dispatch` 尾段重写：静态拒绝后全部进入 `authority.check_and_submit`（锁内 = 库 revision 比对 + 廉价可启动探测 + transport 提交）——**票据不再单独放行**（删"已复核跳过复查"捷径）。③ `set_enabled_atomic`：启停经 `authority.mutate`。④ `_launchable_probe`：catalog 提供 `launchable_now` 则锁内零目录扫描；否则回退 launch_request 全查（正确性优先） |
| `file_explorer/plugins.py` | `launchable_now`：廉价探测（builtin 启动路由 + 停用配置；配置读不了保守拒绝）——GUI 线程锁内复用不触发目录扫描 |
| `files_plugins_controller.py` | `_set_enabled` legacy 分支改走 `set_enabled_atomic`（与在途提交原子串行） |

## 语义（顺序性证明）

经边界的变更与提交**串行**：要么整体在提交前（`AUTHORITY_EPOCH_MOVED`/
`COMMAND_NOT_APPLICABLE` 拒绝、零外发），要么整体在提交后（变更对下一
次调用生效）。绕过边界直改 catalog 的停用不推进 epoch，但锁内探测读
实时配置仍拦下（belt-and-braces，不靠"所有人都守规矩"的假设）。

## 验证

- `tests/plugins/test_dispatch_f4.py`（新，4 项）：提交持锁时 mutate 被
  挡（0.3s 内未完成）且顺序为 submit→exit→mutate；epoch 推进后拒绝且
  submit 零调用；复核异常（权威/非权威）都阻止提交；同 epoch 复核先
  于提交。
- `tests/files/test_plugin_launch_service.py` +4（共 32）：invalidate 后
  票据不放行（AUTHORITY_EPOCH_MOVED、零提交）；经边界停用后 dispatch
  拒绝；**绕过边界**直改状态仍被锁内探测拦下；在途提交（transport 阻
  塞模拟）期间 set_enabled_atomic 被挡、提交完整在前。
- 回归：tests/plugins + 全部 files 域（domain/facade/catalog/manifest/
  launch/transport/UI 插件）—— **677 通过**。

## 诚实边界

- 撤权（grant）路径尚未有真实变更源（F 表第 9 项 broker 批次）——
  `invalidate("revoke")` 的入口已就绪，等待第一个真实 grant 消费者。
- 库 revision 的源头（facade 写路径）不持有本边界锁：revision 漂移靠
  锁内重读比对（int 读原子）——提交持锁期间发生的 revision bump 会在
  **下一次**调用被拒；本次提交的快照语义以冻结值为准（与 F3 口径一致）。
- `launchable_now` 只覆盖"经边界的变更"（启停）；self_disabled/加载失
  救等慢事实仍由 revalidate 全量复核负责。

## 隐私

合成路径与假 catalog/transport/线程事件；无隐私路径。
