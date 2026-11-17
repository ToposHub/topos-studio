# Files 插件系统完成情况复核与后续计划

日期：2026-11-17。复核基线：`c76724e3`，覆盖 `d1b0cc63` 的共享层与 `af1dbc13` 的 Files 接线。
对应原计划：[Files 插件系统重构计划](2026-11-17-files-plugin-system-rebuild-plan.md)。

## 复核结论

原生插件 UI 与四个旧启动接口已落地；共享 manifest、安装、状态、生命周期、服务协议也已有代码和测试。**完整插件系统重构尚未完成**：部分代码仍未接入生产宿主，且安装边界、状态一致性、生命周期和 schema 兼容存在已复现缺陷。

本轮只修复独立边界问题，结构性工作按下列任务保留给后续实施。回归 **697 passed / 0 failed / 0 skipped**，并不代表下列未完成任务已验收。[验证范围与复现结果](../validation/files_plugin_completion_review_20261117/README.md)。

| 原任务 | 复核状态 | 实际完成范围 |
| --- | --- | --- |
| UI-01～10 | 已验证（既有 UI 范围） | 原生中心/启动器、检索分类、快捷键、启停重启提示、四个启动接口、其他工具兼容入口 |
| PL-01 | 部分 | 有声明解析/兼容检查；完整字段 schema、深冻结、未知字段往返与未来 schema 策略未通过 |
| PL-02 | 部分 | 有独立状态模型；并发快照、重复身份和真实来源存在问题，UI 尚未接入 Inventory |
| PL-03 | 部分 | 有上下文/贡献句柄/取消模型；任务归属、清理失败恢复和实际宿主贡献接线未完成 |
| PL-04/05 | 部分 | 10 个 manifest 已声明；统一窗口、菜单所有权、provider 仲裁、Inspector 仍未闭环 |
| PL-06 | 部分 | 有共享 broker/作业协议；Files 的实际权限执行、隔离 worker、OS 沙箱未完成 |
| PL-07 | 部分 | 四个启动入口走同一服务；多数 Files 域服务仍走旧 HTTP，执行前授权/状态复核未闭环 |
| PL-08 | 部分 | 有本地安装/卸载核心；安装根所有权、并发事务、版本语义和恢复验证未通过 |
| PL-09 | 未开始 | 远程签名索引、市场服务及其安装策略没有生产实现 |
| PL-10 | 部分 | 有确定性打包/摘要校验；模板、SDK、调试入口未完成 |
| 旧宿主退役 | 未开始 | 旧 Python/Flask/Web 插件仍提供实际能力，不能移除 |

## 本轮已经修复

- 安装/打包在读取、复制源树前拒绝文件、目录、悬空及 manifest 符号链接；复制时不静默解引用。此修复不等于完成安装目标目录的所有权防护。
- 空/非法插件 id 不再允许卸载安装根目录；要求删除插件数据但未提供策略时，在移除插件之前拒绝。
- 打包固定 ZIP 时间戳与规范权限，修改源文件 mtime 不改变包字节；空、非法或非 UTF-8 摘要 sidecar 返回失败。
- 旧清单字符串诊断转换为结构化诊断，保留旧声明解析错误，不再因 `.severity` 访问字符串而崩溃。
- 作业首次进入 `finished` / `cancelled` 后，不被迟到的完成或取消确认改写。
- `PreparedInvocation.payload` 递归冻结，传输时生成独立副本；拒绝把一个路径字符串当字符列表。启动服务限制四个已验证接口，拒绝取消、过期或身份不匹配的调用；UI 同步拒绝后释放 busy。
- catalog 按完整 API 区间判断兼容，声明元数据优先；拒绝跨目录 manifest 链接；声明不能凭空创建旧 HTTP 路由或改变旧接口的 payload 规则。
- 畸形 manifest 文件的字段转换异常变成诊断，避免逃出发现 worker。完整字段校验仍属于下列 PR-04。

本轮新增 **47 项回归**。测试素材和配置使用临时夹具；提交不含私人图片、视频、缩略图或截图。

## PR-01 / P1：安装根所有权、事务与版本身份

入口：[installer.py](../../src/shared/plugins/installer.py) 的 `_plugin_root/current_version/installed_manifest/install/uninstall`、[pack.py](../../src/shared/plugins/pack.py)。

**已复现**：预先令 `install_root/files.probe/versions` 链接到临时安装根之外，`install()` 仍把 `1.0.0` 写到外部目录。源包符号链接检查无法保护安装目标。`current` 也只读取目标名字，没有证明它属于该插件的版本目录。`1.0.0-beta → 1.0.0` 被 `_version_tuple` 错误判为非升级。

- [ ] 建立安装根、插件根、staging、versions、current 的统一所有权规则；逐层拒绝越界链接、错误类型与异常指针，处理检查和使用之间的替换。
- [ ] 为安装/更新/卸载建立同一锁和事务状态；临时指针名称不能跨事务冲突；崩溃/磁盘满/指针失败可恢复，清理失败保留诊断。
- [ ] 使用完整 SemVer 顺序；同版本不同内容摘要必须诊断冲突，不能仅按版本字符串返回 unchanged。
- [ ] 校验目录来源和 ZIP 来源使用同一大小/条目预算；明确数据删除失败时的卸载结果与重试协议。
- [ ] 来源、签名、摘要与包声明的 `source` 分开；包不能自称 builtin 获得信任。

验收：目标每一层被替换、跨插件 current、并发双更新/卸载、各阶段失败、同版不同内容、预发布转正式均有隔离测试；不得写出受管根或删除其他插件/用户素材。

## PR-02 / P1：生命周期按插件归属、可重试清理

入口：[lifecycle.py](../../src/shared/plugins/lifecycle.py) 的 `ContributionSet.rollback`、`PluginLifecycleController.deactivate`、`PluginJobs`。

**已复现**：同一 controller 激活 A、B，启动 B 的作业后停用 A，`cancel_all()` 会取消 B 的作业；一个 `release()` 抛错便中止剩余释放。`deactivate()` 先 pop 贡献集合，若关闭窗口/解绑/释放抛错，重试已丢失所有权。任务仍在终止时也会释放贡献。

- [ ] 作业、窗口、订阅和贡献句柄均记录 plugin owner；停用只处理该插件资源。
- [ ] 清理步骤有状态，失败聚合诊断且继续处理独立资源；失败句柄仍可重试，不提前移除注册所有权。
- [ ] 禁新调用后等待任务确认终止，再释放其仍依赖的贡献；超时报告 terminating，不能谎报 stopped。
- [ ] 不在 GUI 线程 sleep 等待作业；真实 Blueprint/JS/provider 注册使用可注销句柄，激活半失败可完整回滚。

验收：A/B 并行、每一步清理抛错后重试、重复停用、迟到回调、任务未退出时关闭均可测；不跨插件取消，不泄漏句柄，不销毁运行中资源。

## PR-03 / P1：清单一致性、重复身份与真实来源

入口：[inventory.py](../../src/shared/plugins/inventory.py)、[Files catalog](../../src/app/integrations/file_explorer/plugins.py)、[controller](../../src/app/ui/files_plugins_controller.py)。

**已复现**：在 `snapshot()` 读取 revision 后暂停，另一线程 refresh，可得到旧 revision 与新 records。`refresh()` 先以字典覆盖重复 id，后续校验已看不到重复身份。旧适配仍从 Python 常量重建 manifest，未接入 `topos.plugin.json`；未知旧目录可能被标为 builtin。

- [ ] 使用同一锁或不可变整快照指针，使 revision/records 原子一致；set_desired/mark_runtime/refresh 不丢失并发状态。
- [ ] 合并之前检测重复 id、legacy alias 和来源冲突，拒绝静默最后胜出。
- [ ] 来源由宿主管理，未知第三方默认停用；静态声明与真实运行版本分开投影，不能混淆安装版本与已加载版本。
- [ ] 将 UI/偏好/启动服务逐步接到同一 Inventory，保留现有 id、固定项和快捷键迁移；安装目录与发现目录也需闭环。

验收：用屏障确定性制造刷新交错；同 id 多来源、多版本、目录移除、第三方重启后状态一致；清单刷新不执行插件。

## PR-04 / P1：完整 schema、深不可变和未知字段往返

入口：[manifest.py](../../src/shared/plugins/manifest.py)、[InvocationContext](../../src/shared/plugins/lifecycle.py)。

**已复现**：`parse({future: ...}) → to_dict() → parse()` 丢失未知字段；修改输入 schema 的嵌套字典会同步改变解析后的 manifest；未来 `schema_version='999'` 只有 warning，兼容检查不阻断。文件读取边界本轮已防止转换异常逃逸，直接 mapping 解析和完整字段类型校验仍需完善。

- [ ] 统一冻结/解冻策略，覆盖 capability schema、resource_policy、metadata、unknown_fields、context.params；序列化仍输出可迁移 JSON。
- [ ] 对每层字段类型、枚举、尺寸、范围、长度和引用提供带字段路径的诊断；不以强制类型转换或静默默认值冒充合法声明。
- [ ] 明确未来 schema 的保存与执行策略：可保留未知声明，但不支持的版本不得安装为可用/启用。
- [ ] 未知字段往返不改变结构，迁移有 schema 版本；身份校验采用严格完整匹配。

验收：嵌套输入/输出 mutation、未知字段重复往返、所有字段畸形矩阵、超深/超大输入、未来 schema 经发现/打包/安装得到一致结果。

## PR-05 / P1：执行前复核与一致的受理结果

入口：[plugin_launch_service.py](../../src/features/files/plugin_launch_service.py)、[services.py](../../src/shared/plugins/services.py)、Files transport。

现阶段 prepare 冻结和取消/过期/身份拒绝已验证；dispatch 前仍未复核插件启停、源 entry revision 和授权。transport 若同步拒绝，`dispatch()` 仍可返回 accepted；多数领域服务仍只有 Protocol。

- [ ] 将状态/权限复核与执行调度放在同一权威服务；prepare 后停用、切库、源替换时拒绝过期调用，不能在 GUI 线程补同步扫描。
- [ ] 分开 queued、HTTP accepted、window ready 和 job completed；以 invocation id 关联同步拒绝、异步成功/失败与取消。
- [ ] Metadata/Collections/FileTask/Import 等接到真实领域实现，锁目录、revision、幂等和错误码一致，保留旧接口兼容。
- [ ] Files 的真实文件/网络 I/O 经过 broker；进程隔离与 OS 沙箱分别报告实际执行状态。

验收：prepare/dispatch 间变更状态、并发多次调用、网络不可用与迟到 ACK；任何入口不绕过授权，不把受理显示为处理完成。

## PR-06 / P1：实际插件贡献与宿主迁移

- [ ] 10 个内建插件逐项接到 command/window/provider/Inspector 宿主，复用既有媒体/渲染/效果服务。
- [ ] 实现 provider owner、优先级、冲突解释、缓存签名与允许回退的错误类别；声明能力不等于后端可用。
- [ ] 旧 Web 工具的有效能力和数据操作有等价新入口或明确兼容方案，之后才评估旧宿主退役。

沿用原计划逐插件迁移表与媒体/关闭验收；不要只迁移名称、图标或 manifest。

## PR-07 / P2：生态、分发和作者工具

- [ ] 远程签名索引、权限差异、更新回滚、离线策略和版本固定。
- [ ] command/window/background/format/inspector 模板、SDK、显式开发启动与诊断工具。
- [ ] 实测冷/热启动器、1000 项清单搜索、4K 播放期间后台作业和连续启停资源收敛，再冻结性能预算。

建议先完成 PR-01/02/03/04 的不变量与失败矩阵，再推进 PR-05/06 的生产接线，最后做 PR-07。每次验收注明“契约测试”还是“真实宿主闭环”，避免继续用局部测试通过代替整体完成。
