# Files 插件系统完成情况复核与后续计划

日期：2026-11-17。复核基线：`c76724e3`，覆盖 `d1b0cc63` 的共享层与 `af1dbc13` 的 Files 接线。
对应原计划：[Files 插件系统重构计划](2026-11-17-files-plugin-system-rebuild-plan.md)。

**第三轮复核更新**：PR-01～05 又修复了安装回滚/锁、清理重试/并发、旧快照覆盖、畸形声明与复核标记等边界；完整回归 **817 passed**。本表保留前两轮历史口径，不能把下文“全部已复现缺陷已修复”解释为这些模块已穷尽正确或生产接线完成。当前残留大工程与后续升级统一以[最终替代总计划](2026-11-17-files-final-replacement-and-upgrade-plan.md)为准；细节见[第三轮验证记录](../validation/files_final_replacement_review_20261117/README.md)。用户已授权不考虑旧工程/旧接口兼容，新计划使用新服务与新宿主单一路径。

## 复核结论

原生插件 UI 与四个旧启动接口已落地；共享 manifest、安装、状态、生命周期、服务协议也已有代码和测试。**完整插件系统重构尚未完成**：部分代码仍未接入生产宿主。

第一轮复核只修复独立边界问题（回归 697 passed，见[验证记录](../validation/files_plugin_completion_review_20261117/README.md)）。
**第二轮（同日，`886dfaa1`→`8628fc6b` 共 5 个提交）已修复 PR-01～PR-05 的全部已复现缺陷**，下列各节复选框按实际完成勾选并注明测试证据；PR-06/PR-07 的结构性工作仍未开始。任何回归数字都不代表未勾选任务已验收。

| 原任务 | 复核状态 | 实际完成范围 |
| --- | --- | --- |
| UI-01～10 | 已验证（既有 UI 范围） | 原生中心/启动器、检索分类、快捷键、启停重启提示、四个启动接口、其他工具兼容入口 |
| PL-01 | 部分→PR-04 已修 | 深冻结、未知字段往返、未来 schema 阻断、严格身份匹配、字段路径诊断均有隔离测试；schema 矩阵非穷尽 |
| PL-02 | 部分→PR-03 已修 | 单锁原子快照、重复身份/legacy 冲突显式诊断、声明式 manifest 优先、宿主来源政策、声明/运行版本分槽 |
| PL-03 | 部分→PR-02 已修 | 任务归属插件、回滚失败不扩散、可重试停用状态机、诚实 terminating 报告 |
| PL-04/05 | 部分 | 10 个 manifest 已声明；统一窗口、菜单所有权、provider 仲裁、Inspector 仍未闭环（PR-06） |
| PL-06 | 部分 | 有共享 broker/作业协议；Files 的实际权限执行、隔离 worker、OS 沙箱未完成（PR-06） |
| PL-07 | 部分→PR-05 已修 | prepare→revalidate（worker 内权威复核）→dispatch；同步拒绝不再误报受理；域服务仍走旧 HTTP |
| PL-08 | 部分→PR-01 已修 | 逐层所有权、严格指针、单事务+清扫、完整 SemVer、同版本内容冲突、卸载失败协议、安装器自记来源 |
| PL-09 | 未开始 | 远程签名索引、市场服务及其安装策略没有生产实现（PR-07） |
| PL-10 | 部分 | 有确定性打包/摘要校验；模板、SDK、调试入口未完成（PR-07） |
| 旧宿主退役 | 未开始 | 旧 Python/Flask/Web 插件仍提供实际能力，不能移除 |

## 第一轮已经修复（`1d18cf21`）

- 安装/打包在读取、复制源树前拒绝文件、目录、悬空及 manifest 符号链接；复制时不静默解引用。此修复不等于完成安装目标目录的所有权防护。
- 空/非法插件 id 不再允许卸载安装根目录；要求删除插件数据但未提供策略时，在移除插件之前拒绝。
- 打包固定 ZIP 时间戳与规范权限，修改源文件 mtime 不改变包字节；空、非法或非 UTF-8 摘要 sidecar 返回失败。
- 旧清单字符串诊断转换为结构化诊断，保留旧声明解析错误，不再因 `.severity` 访问字符串而崩溃。
- 作业首次进入 `finished` / `cancelled` 后，不被迟到的完成或取消确认改写。
- `PreparedInvocation.payload` 递归冻结，传输时生成独立副本；拒绝把一个路径字符串当字符列表。启动服务限制四个已验证接口，拒绝取消、过期或身份不匹配的调用；UI 同步拒绝后释放 busy。
- catalog 按完整 API 区间判断兼容，声明元数据优先；拒绝跨目录 manifest 链接；声明不能凭空创建旧 HTTP 路由或改变旧接口的 payload 规则。
- 畸形 manifest 文件的字段转换异常变成诊断，避免逃出发现 worker。完整字段校验仍属于下列 PR-04。

第一轮新增 **47 项回归**；第二轮 PR-01～05 再新增 **33 个测试函数**（参数化展开后 tests/plugins 收集 327 项、启动服务 20 项；各节注明证据测试）。测试素材和配置使用临时夹具；提交不含私人图片、视频、缩略图或截图。

## 第二轮修复索引（PR-01～05，2026-11-17）

| 提交 | 范围 | 关键回归 |
| --- | --- | --- |
| `886dfaa1` | PR-01 安装所有权/事务/版本身份 | `tests/plugins/test_package_installer.py`（越界 versions 链接、跨插件 current、SemVer 预发布、同版本内容冲突、预算对齐、卸载失败协议、来源 sidecar、残留清扫、并发双更新） |
| `0197c314` | PR-02 生命周期归属与可重试清理 | `tests/plugins/test_shared_lifecycle.py`（停 A 不取消 B、单句柄失败不扩散、terminating 不释放、清理失败聚合重试、脏回滚保留所有权、按归属取消） |
| `e6e8bf59` | PR-03 清单一致性与真实来源 | `tests/plugins/test_shared_inventory.py`（成对采样原子性、重复 id 诊断、legacy 冲突、声明式优先、身份不符拒绝、宿主来源政策） |
| `618aa197` | PR-04 深冻结/往返/schema 策略 | `tests/plugins/test_shared_manifest.py`（深不可变、顶层与能力级未知字段往返、未来 schema 阻断、严格完整匹配、字段路径矩阵） |
| `8628fc6b` | PR-05 执行前复核与受理语义 | `tests/files/test_plugin_launch_service.py`（prepare→停用→dispatch 拒绝、revalidate 过期拒绝、复核后 dispatch 不重扫、同步拒绝≠受理、阶段词表） |

## PR-01 / P1：安装根所有权、事务与版本身份

入口：[installer.py](../../src/shared/plugins/installer.py) 的 `_plugin_root/current_version/installed_manifest/install/uninstall`、[pack.py](../../src/shared/plugins/pack.py)。

**已复现（均已修复，`886dfaa1`）**：预先令 `install_root/files.probe/versions` 链接到临时安装根之外，`install()` 仍把 `1.0.0` 写到外部目录。源包符号链接检查无法保护安装目标。`current` 也只读取目标名字，没有证明它属于该插件的版本目录。`1.0.0-beta → 1.0.0` 被 `_version_tuple` 错误判为非升级。

- [x] 建立安装根、插件根、staging、versions、current 的统一所有权规则；逐层拒绝越界链接、错误类型与异常指针，处理检查和使用之间的替换。（`_install_root_dir`/`_ensure_real_dir`/`_read_pointer`；测试：`test_versions_dir_linked_outside_never_receives_files`、`test_symlinked_install_root_is_refused_before_any_write`、`test_foreign_or_abnormal_current_pointer_is_refused`、`test_current_pointer_that_is_a_regular_file_is_refused`）
- [x] 为安装/更新/卸载建立同一锁和事务状态；临时指针名称不能跨事务冲突；崩溃/磁盘满/指针失败可恢复，清理失败保留诊断。（线程 RLock + flock `_Transaction`；事务令牌指针名；`_sweep_transactions` 诊断入 `last_cleanup_notes`；测试：`test_crashed_transaction_leftovers_are_swept_with_diagnostics`、`test_concurrent_double_update_is_serialized`、`test_failure_between_move_and_swap_restores_previous_state`）
- [x] 使用完整 SemVer 顺序；同版本不同内容摘要必须诊断冲突，不能仅按版本字符串返回 unchanged。（`_semver_key` 预发布排序 + `_payload_digest`/`VERSION_CONTENT_CONFLICT`；测试：`test_prerelease_to_release_is_an_upgrade`、`test_same_version_different_content_is_a_conflict`）
- [x] 校验目录来源和 ZIP 来源使用同一大小/条目预算；明确数据删除失败时的卸载结果与重试协议。（`_measure_source` 复制前后各查一次；`UNINSTALL_IO_ERROR` 可重试幂等 / `DATA_DELETE_FAILED` 明示半完成态；测试：`test_folder_sources_use_the_same_size_budget_as_zip`、`test_uninstall_data_deletion_failure_reports_partial_state`）
- [x] 来源、签名、摘要与包声明的 `source` 分开；包不能自称 builtin 获得信任。（`versions/<v>.install.json` sidecar 由安装器自写 `local_package`+内容摘要；测试：`test_provenance_is_installer_owned_not_package_declared`）

验收（契约测试口径）：目标每一层被替换、跨插件 current、并发双更新、各阶段失败、同版不同内容、预发布转正式均有隔离测试；不写出受管根或删除其他插件/用户素材。**未达"真实宿主闭环"**：生产安装根的位置与生命周期由宿主接线决定，尚无生产调用方。

## PR-02 / P1：生命周期按插件归属、可重试清理

入口：[lifecycle.py](../../src/shared/plugins/lifecycle.py) 的 `ContributionSet.rollback`、`PluginLifecycleController.deactivate`、`PluginJobs`。

**已复现（均已修复，`0197c314`）**：同一 controller 激活 A、B，启动 B 的作业后停用 A，`cancel_all()` 会取消 B 的作业；一个 `release()` 抛错便中止剩余释放。`deactivate()` 先 pop 贡献集合，若关闭窗口/解绑/释放抛错，重试已丢失所有权。任务仍在终止时也会释放贡献。

- [x] 作业、窗口、订阅和贡献句柄均记录 plugin owner；停用只处理该插件资源。（`PluginJobs.start(plugin_id=...)`/`cancel_owned`；窗口/解绑回调本就按 plugin_id 调用；测试：`test_stopping_plugin_a_never_cancels_plugin_b_jobs`、`test_jobs_owner_scoped_cancellation`）
- [x] 清理步骤有状态，失败聚合诊断且继续处理独立资源；失败句柄仍可重试，不提前移除注册所有权。（停用状态机 active→stopping→stopped，`cleanup_failures` 聚合、`retry_failed_releases` 重试、贡献集停完前保留在 controller；测试：`test_rollback_continues_when_one_release_raises`、`test_cleanup_step_failure_is_aggregated_and_retryable`、`test_activation_failure_with_dirty_rollback_stays_retryable`）
- [x] 禁新调用后等待任务确认终止，再释放其仍依赖的贡献；超时报告 terminating，不能谎报 stopped。（任务未确认终止时不关窗/不释放；测试：`test_deactivate_keeps_ownership_until_jobs_confirm_and_cleanup_succeeds`、`test_deactivate_reports_jobs_still_terminating_honestly`）
- [ ] 不在 GUI 线程 sleep 等待作业；真实 Blueprint/JS/provider 注册使用可注销句柄，激活半失败可完整回滚。（`wait_jobs=False` 轮询重入已提供，但旧 Blueprint/JS/provider 仍无真实贡献句柄接线——属 PR-06）

验收（契约测试口径）：A/B 并行、逐步清理失败重试、重复停用、迟到回调、任务未退出时关闭均可测；不跨插件取消，不泄漏句柄，不销毁运行中资源。**未达"真实宿主闭环"**：Files 旧插件停用仍是重启制，无真实窗口/事件句柄。

## PR-03 / P1：清单一致性、重复身份与真实来源

入口：[inventory.py](../../src/shared/plugins/inventory.py)、[Files catalog](../../src/app/integrations/file_explorer/plugins.py)、[controller](../../src/app/ui/files_plugins_controller.py)。

**已复现（均已修复，`e6e8bf59`）**：在 `snapshot()` 读取 revision 后暂停，另一线程 refresh，可得到旧 revision 与新 records。`refresh()` 先以字典覆盖重复 id，后续校验已看不到重复身份。旧适配仍从 Python 常量重建 manifest，未接入 `topos.plugin.json`；未知旧目录可能被标为 builtin。

- [x] 使用同一锁或不可变整快照指针，使 revision/records 原子一致；set_desired/mark_runtime/refresh 不丢失并发状态。（全部操作收进单一 RLock；测试：`test_snapshot_revision_and_records_never_mix_refresh_halves` 成对采样 400 次）
- [x] 合并之前检测重复 id、legacy alias 和来源冲突，拒绝静默最后胜出。（保留首个 + `PLUGIN_ID_DUPLICATE`/`LEGACY_ID_CONFLICT` 阻断诊断；测试：`test_duplicate_ids_are_flagged_not_last_write_wins`、`test_legacy_alias_conflict_across_records_is_flagged`）
- [x] 来源由宿主管理，未知第三方默认停用；静态声明与真实运行版本分开投影，不能混淆安装版本与已加载版本。（`legacy_files_records(builtin_ids=...)` 宿主政策、`PluginRecord.runtime_version` 分槽、适配优先读 `topos.plugin.json` 且身份不符拒绝；测试：`test_legacy_source_is_host_policy_not_declaration`、`test_legacy_adapter_prefers_declarative_manifest_with_matching_identity`、`test_legacy_adapter_rejects_foreign_declared_identity`）
- [ ] 将 UI/偏好/启动服务逐步接到同一 Inventory，保留现有 id、固定项和快捷键迁移；安装目录与发现目录也需闭环。（插件中心 UI/偏好仍读旧 catalog 投影；Inventory 尚无生产数据源接线）

验收（契约测试口径）：并发刷新成对一致、同 id 多来源显式诊断、第三方默认停用；清单刷新不执行插件。**未达"真实宿主闭环"**：UI 数据源切换与安装/发现目录闭环留给后续。

## PR-04 / P1：完整 schema、深不可变和未知字段往返

入口：[manifest.py](../../src/shared/plugins/manifest.py)、[InvocationContext](../../src/shared/plugins/lifecycle.py)。

**已复现（均已修复，`618aa197`）**：`parse({future: ...}) → to_dict() → parse()` 丢失未知字段；修改输入 schema 的嵌套字典会同步改变解析后的 manifest；未来 `schema_version='999'` 只有 warning，兼容检查不阻断。

- [x] 统一冻结/解冻策略，覆盖 capability schema、resource_policy、metadata、unknown_fields、context.params；序列化仍输出可迁移 JSON。（`freeze.deep_freeze/deep_thaw` 单一实现，manifest/lifecycle/services 共用；测试：`test_manifest_is_deeply_immutable`）
- [x] 对每层字段类型、枚举、尺寸、范围、长度和引用提供带字段路径的诊断；不以强制类型转换或静默默认值冒充合法声明。（窗口/provider/服务/能力/平台字段矩阵；条目级错误拒条目、整段结构错误拒 manifest、平台损坏拒绝而非默认无限制；测试：`test_malformed_top_level_fields_reject_manifest`、`test_malformed_items_are_dropped_but_manifest_is_kept`、`test_window_fields_validate_with_paths`）
- [x] 明确未来 schema 的保存与执行策略：可保留未知声明，但不支持的版本不得安装为可用/启用。（`SCHEMA_VERSION_UNSUPPORTED` 阻断级诊断，安装/Inventory/打包路径一致拒绝；测试：`test_future_schema_version_is_preserved_but_blocked`）
- [x] 未知字段往返不改变结构，迁移有 schema 版本；身份校验采用严格完整匹配。（顶层与能力级未知字段 `to_dict` 原样回填；`fullmatch` 拒绝尾部换行/空白；测试：`test_unknown_fields_survive_round_trip`、`test_capability_unknown_fields_survive_round_trip`、`test_plugin_id_is_strict_full_match`）

验收（契约测试口径）：嵌套 mutation、未知字段重复往返、字段畸形矩阵、未来 schema 全链路一致。**未达"穷尽"**：字段矩阵覆盖计划内字段与常见畸形，不声明覆盖所有可能输入。

## PR-05 / P1：执行前复核与一致的受理结果

入口：[plugin_launch_service.py](../../src/features/files/plugin_launch_service.py)、[services.py](../../src/shared/plugins/services.py)、Files transport。

**已复现（均已修复，`8628fc6b`）**：dispatch 前未复核插件启停/接口状态；transport 同步拒绝时 `dispatch()` 仍返回 accepted。

- [x] 将状态/权限复核与执行调度放在同一权威服务；prepare 后停用、切库、源替换时拒绝过期调用，不能在 GUI 线程补同步扫描。（`revalidate` 在 worker 线程重跑同一权威；控制器 GUI 线程只提交；未经复核直接 dispatch 由服务内补做——任何入口绕不过授权；测试：`test_dispatch_rejects_when_disabled_between_prepare_and_dispatch`、`test_revalidate_rejects_stale_state_and_marks_prepared`、`test_revalidated_dispatch_submits_without_rescanning`）
- [x] 分开 queued、HTTP accepted、window ready 和 job completed；以 invocation id 关联同步拒绝、异步成功/失败与取消。（STAGES 增 `queued`；dispatched=HTTP 受理≠窗口、completed=插件确认、同步拒绝 `LAUNCH_REJECTED` 可重试；测试：`test_transport_sync_rejection_is_never_reported_as_accepted`、`test_stage_vocabulary_separates_queued_dispatched_and_completed`）
- [ ] Metadata/Collections/FileTask/Import 等接到真实领域实现，锁目录、revision、幂等和错误码一致，保留旧接口兼容。（仍是共享 Protocol；域服务抽取属 NR-06/FUP-09）
- [ ] Files 的真实文件/网络 I/O 经过 broker；进程隔离与 OS 沙箱分别报告实际执行状态。（属 PR-06）

验收（契约测试口径）：prepare/dispatch 间变更状态、同步拒绝、迟到 ACK 均不误报。**未达"真实宿主闭环"**：四个启动接口之外域服务仍走旧 HTTP。

## PR-06 / P1：实际插件贡献与宿主迁移（本轮未动）

- [ ] 10 个内建插件逐项接到 command/window/provider/Inspector 宿主，复用既有媒体/渲染/效果服务。
- [ ] 实现 provider owner、优先级、冲突解释、缓存签名与允许回退的错误类别；声明能力不等于后端可用。
- [ ] 旧 Web 工具的有效能力和数据操作有等价新入口或明确兼容方案，之后才评估旧宿主退役。

沿用原计划逐插件迁移表与媒体/关闭验收；不要只迁移名称、图标或 manifest。

## PR-07 / P2：生态、分发和作者工具（本轮未动）

- [ ] 远程签名索引、权限差异、更新回滚、离线策略和版本固定。
- [ ] command/window/background/format/inspector 模板、SDK、显式开发启动与诊断工具。
- [ ] 实测冷/热启动器、1000 项清单搜索、4K 播放期间后台作业和连续启停资源收敛，再冻结性能预算。

PR-01/02/03/04 的不变量与失败矩阵已完成（各节注明契约测试口径）；PR-05 的启动路径复核已闭环，其余域服务与 PR-06/07 按上表保留。后续推进仍须注明“契约测试”还是“真实宿主闭环”，避免用局部测试通过代替整体完成。
