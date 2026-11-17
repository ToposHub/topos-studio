# Files 最终替代与后续升级总计划

日期：2026-11-17。复核起点：`4223ede5`；本文件为下一轮实施清单，不表示下列工作已经完成。

目标：完成本计划 A～I 阶段并满足退出门槛后，原生 Files 可以独立承担约定能力，物理退役 `external/file_explorer/`。
“再做一轮”指完整执行依赖链和验收，不指一次提交或只把表格勾完。缺少真实宿主、恢复、无旧目录或发布包证据时，不宣称已经完全替代。

**范围修正：项目仍在开发，用户已明确授权按最优结构进行破坏性升级，不要求兼容旧项目/旧工程。** 不为旧工程schema、旧DB/config、插件状态、旧接口或旧id建立迁移桥。新Files服务、新库结构和原生宿主是唯一生产实现；开发数据按显式重置流程处理。真实源素材不属于可重置开发数据。

本计划统一承接 [Files 复核 FUP-01～09](2026-11-17-files-replacement-followup-plan.md)、[原始 NR-00～10](2026-11-16-files-full-replacement-review-plan.md)、[插件重构](2026-11-17-files-plugin-system-rebuild-plan.md)及[插件复核 PR-01～07](2026-11-17-files-plugin-completion-review-plan.md)。旧表保留历史证据；后续实施以本表的生产闭环和退出条件为准。

## 1. 本次复核后的基线

已有：原生四布局、插件中心与启动器、四个旧启动接口、共享 manifest/Inventory/Installer/Lifecycle/服务契约及较完整的隔离回归。
PR-01～05 第二轮修复改善了共享契约；这些类存在不等于 Files 已使用它们。当前真实 Files 仍经旧 HTTP、配置和运行时工作；下一轮按本计划直接替换并移除这些旧调用，不做双实现过渡。

本次审查同时直接修复了一批独立问题；完整门禁 **817 passed / 0 failed / 0 skipped，36.34s**，新增66项参数化回归。
真实Topos视频12段、36次RGB比例检查全部通过，源文件size/mtime未变、未输出图片；这不代替GPU/声卡/平台实机验收。详见[本轮验证记录](../validation/files_final_replacement_review_20261117/README.md)。
已处理范围包括：诊断工具定位改共享 locator；Topos RGB 等比输出；manifest 畸形值/schema/递归边界；启动调用真实性检查和 queued/HTTP ACK 阶段；安装锁、来源 sidecar、回滚和遗留版本处理。
上述修复不重复列为待办；后续仍需真实宿主、并发替换、跨平台和持久恢复的完整验收。

| 目前阻塞 | 已确认的生产入口 | 本计划归属 |
| --- | --- | --- |
| 无旧目录不能启动 | `adapter.py` 按旧 `main.py` 定位根；`files_panel.py` 顶层 adapter/WebEngine | D、I |
| 原生仍启动旧宿主 | `FilesPanel.__init__` 无条件建 browser、启动 server，再切原生模式 | D |
| DTO 尚非业务服务 | `NativeFilesTransport` 的目录/任务/metadata/搜索等仍调旧 routes | B、C |
| 新插件类未接生产 | 中心与偏好读旧 catalog/config；实际工具还由旧 manager/Flask 启动 | F、G |
| 数据位置未独立 | 旧 DB/config 锚定扩展/仓库；共享 Database 默认路径依赖 CWD | B、H：新结构、空库与开发数据重置 |
| 素材事务与恢复不完整 | no-replace、路径关系迁移、回收站、改名确认计划及退出恢复 | A、B、H |
| 真实格式与生命周期未闭环 | 预览/比较任务保活、播放结束、GPU 所属线程与格式诊断 | E |
| 发布与退出证据不足 | 旧架构测试仍要求旧路径/Flask 存在；缺无旧目录冻结包验收 | I |

## 2. 范围与能力承诺

本轮必须保留：目录树、常用位置、四布局、搜索/筛选、选择/拖放、标签/评分/信息、集合基本组织、图片/视频/音频预览、文件任务、回收站恢复、必要库维护和实际使用的内建工具。
四布局继续共用 model、selection、location 和命令服务；不可各建一套数据与选择逻辑。
旧插件能力需要迁入独立工具或领域服务；仅移动名称、manifest、图标或保留旧页面入口不计完成。

| 旧能力类别 | 本轮处理要求 |
| --- | --- |
| 层级标签、智能文件夹、堆叠 | 按新模型设计关系和查询/维护入口；保留所需功能，不承担旧数据导入兼容 |
| XMP/metadata JSON、失联重链、备份恢复 | 新系统维护能力，采用新格式/协议；必须可实际恢复，不要求占主界面 |
| RAW/EXR/HDR/PSD/PDF、`.toos/.topos` | 每种格式声明真实 probe/metadata/thumbnail/preview/import/export 能力和依赖；不能统一冒充可播放 |
| OCR、相似/重复、地图、幻灯片/BGM、水印等旧工具 | A 阶段逐项登记现状与去向；需要保留的接服务/独立工具；不保留的先取得明确范围决定，不因打破兼容而无声删功能 |
| Monaco/Darkroom、转码/图片工厂、关机/跑分 | 按 G 阶段逐插件接管；不把删除工具当成完成迁移 |

禁止通过关闭按钮、隐藏错误、默认空结果、吞异常或把所有第三方声明成 builtin 来制造“替代完成”。

## 3. 执行与证据规则

依赖顺序：**A → B → C → D → F → G → H → I**；E 在 B 的任务/资源契约稳定后推进，D/G/I 的媒体验收依赖 E。
可以在边界确定后并行实现独立模块，但上游未过门槛不能标记下游完成。
沿用 `products → features → shared → app`；UI 只发意图和显示状态，业务服务不依赖 QWidget、旧 Flask globals 或旧 JS 状态。

每个阶段统一提交四种证据：

- [ ] 实现与生产调用证据：列真实入口、服务、持久化和关闭调用方。
- [ ] 自动化证据：成功、拒绝、部分失败、取消、重试、重开；说明隔离夹具与真实服务范围。
- [ ] 实机证据：需要设备/平台的条目真实执行，写清平台、依赖和能力限制。
- [ ] 无旧扩展证据：最终在 I 阶段汇总；只 mock 路径、假服务或 AST 夹具不算通过。

证据记录提交号、命令、结果、环境和未覆盖项；不以通过数量替代某条验收。
不适用项写原因和产品范围，不能用 skip 当通过；真实 GPU/声卡/发布包尚未验收则保留待验。

## A. 冻结能力、数据和可恢复基线（NR-00）

依赖：无。范围：确认本轮替代对象，建立可对照的功能/数据/故障矩阵。

- [x] 建立“旧入口 → 能力 → 新服务 → 新UI → 数据表/实体 → 测试 → 去向”的清单，覆盖旧 routes、JS handlers、10 插件和主窗桥接。（[能力矩阵](files-replacement-capability-matrix-20261117.md)：108 路由含 29 条原生仍调用与 5 条死代码、66 项前端能力、10 插件事实、7 项主窗桥接；关键锚点人工复核）
- [x] 对业务必需、独立工具、可选后端、需范围决定项分别写实际行为；没有决定的能力保留为阻塞，不静默删除。（矩阵 §1 必须保留 19 行映射阶段；§2 非必须 24 行分类，其中待决定 16 项保持阻塞并列默认建议；§3 十插件登记实际副作用与 G 阶段必须修正点）
- [x] 定义全新schema、数据目录、插件状态和缓存边界；列出旧开发数据的可清空位置，不导入或双写旧库。（[数据边界](files-replacement-data-boundary-20261117.md)：`files-data/` 唯一边界、新表清单、插件状态由 Inventory 独占、旧位置处置清单与“永不触碰”清单）
- [x] 以临时库和合成素材冻结回归：浏览、写 metadata、改名/移动、删恢复、工程导入、播放器及关闭。（`tests/files/test_replacement_baseline_freeze.py` 11 passed/1 skipped；工程导入与播放器关闭复用既有冻结测试；[验证记录](../validation/files_replacement_phase_a_20261117/README.md)）
- [x] 设计新系统完整备份/恢复命令与隔离样本；明确开发数据重置仅处理受管目录，H 阶段演练。（[备份/恢复与重置设计](files-replacement-backup-restore-20261117.md)：范围/格式/崩溃语义/合成样本定义/H 阶段归属）

生产调用：清单应从主应用菜单、快捷键、Files、Library、媒体池/时间线和工具窗口追踪，不能仅分析共享模块。
验收：旧能力都有明确接管项或待决定项；新数据契约与可重置开发数据边界清楚。
失败策略：未确认归属的目录不能自动清空；不得重置用户源素材、现有工作文件或无关项目。
完成证据：提交能力矩阵、数据清单、隔离基线结果及安全恢复方案。

## B. 安全文件事务、新数据模型与独立 repository（FUP-01～03/08、NR-01/06）

依赖：A。范围：把文件与组织数据的正确性放到同一领域边界，再迁移调用方。

- [x] 实现真实原子 no-replace 或可证明等价策略；末级/父目录/源身份与软硬链接政策一致，不以占位后无条件 rename 作为保证。（B2 `transactions/ops.py`：文件同卷 link+unlink（内核 EEXIST）、跨卷 staging+fsync+link；inode 核验防检查后源替换；目录如实标注检查+rename+核验非内核原子；源/目标符号链接一律拒绝穿过。测试 `test_file_transactions.py` 22 项）
- [x] copy/move/rename 的 error/rename/overwrite 覆盖文件、目录、同卷/跨卷；staging、取消、权限/磁盘满和中断有可恢复状态。（策略矩阵+目录覆盖拒绝+块边界协作取消+errno→结构化 code+retriable；中断恢复见 B3 journal。**跨卷经注入 `same_volume=False` 全路径演练，真实 EXDEV 块设备未接**——如实保留）
- [x] 单改名、批改名、任务 move/rename、恢复共用安全领域入口；真实解析后的读写目标统一锁/权限验证。（B5 `FileOperationService` 唯一写入口 + B6 批改名 + B4 恢复全经此；目标 claim 锁防并发同目标。生产 UI 调用方接线属 C/D，未接前不得声称闭环）
- [x] 文件与 DB 不能组成单一原子事务时使用持久操作日志、幂等键、补偿/待恢复状态；不得日志报错后仍宣称完整成功。（B3 `journal.py`：job_log 事件流、幂等键重放不重复执行、证据分类恢复（completed/清扫可重试/pending_recovery 不猜测）、失败只能 finish('failed')。测试 12 项）
- [x] 新系统中的改名/移动完整处理 metadata、OCR、markers、标签、集合排序/时间、堆叠封面/时间、FTS和子树；新扩展字段按新schema规则保存。（B5：用户/技术列、标签、集合成员（sort_order/added_at 随行迁移）、堆叠成员+封面、FTS 键、目录子树全量改挂；OCR 列 schema 预留待 U4 决定；markers 为无调用方死代码不迁移（矩阵 §4.3））
- [x] 回收站使用版本化完整清单；快照失败禁止删除。恢复冲突、最终身份、实体/关系/清单更新均可重试。（B4 `trash_service.py` version=1 清单先行、快照失败禁删、error 冲突保留可重试、rename 冲突关系挂最终身份、expire_at 过期清理。测试 9 项）
- [x] 批改名 preview 返回计划 id、版本和源身份；execute 核验已确认计划，变化则要求重预览，不重新计算另一计划偷偷执行。（B6 `batch_plan.py`：(size,mtime_ns) 指纹、STALE_PLAN 整体拒绝、PLAN_CONSUMED/PLAN_HAS_CONFLICTS 拒绝。测试 8 项）
- [x] 建立独立 Files repository 和库定位器；Files/Library 改用同一新模型、键空间与事务政策，消除 CWD 双库。（B1 `repository/`：FilesDataLocator + FilesCatalog schema v1；宿主 settings/layouts 库改显式 `<config_root>/host.db`（4 调用点+AST 守卫测试）。**Files/Library UI 数据源切到新模型属 C/D 接线**）
- [x] 用户数据目录、库id、schema版本和初始化规则明确；停止旧路径自动发现/合库，旧 `topos.db/metadata.db/config.json` 不作为新系统输入。（B1：schema_version 双钉+版本不符拒绝打开、libraries.json 原子注册；新代码零读取旧库/旧 config——旧系统行为冻结不动随 I 退役）
- [x] 有界任务提交/执行/历史；活动任务不被淘汰，取消确认后才终态，退出/重启能够识别未完成操作。（journal.trim 有界+未终态永不淘汰；协作取消只有真实停止才报 cancelled；pending()+recover_one 识别未完成。测试 `test_operation_journal.py` 12 项）

**B 阶段边界（诚实）**：以上为**契约测试口径**（隔离夹具 745 passed/1 skipped）；生产 UI/transport 仍走旧 HTTP（C/D 替换）；跨卷为注入模拟非真实块设备；C 阶段完成前 FileOperationService 无生产调用方。

生产调用：原生、插件和主应用的所有实际改名/删除/恢复入口只调用新服务；旧routes与旧任务实现直接退出，不维持薄HTTP适配。
验收：并发同目标、父源目标换链、目录重叠、跨卷、故障注入；源/目标字节、全部关系、任务状态逐项断言。
失败策略：无授权覆盖为零；未完成操作保留可恢复日志和逐项结果，不自动丢弃源文件或清空组织数据。
完成证据：真实 service/API 入口矩阵、恢复日志样例、失败后重启演练，不仅 helper 测试。

## C. 真实 Files facade 与工程提交（FUP-04/09、NR-03/06、PR-05）

依赖：B。范围：把DTO/Protocol变成可被UI、插件和新自动化入口调用的真实服务。

- [ ] 实现 query/tree/search/collections/metadata/library/scan/thumbnail-preview/file-task/import-export/jobs/events 服务，复用可用核心并统一新repository/provider。（**部分**：C1 已落 query/tree/search/collections/metadata/library/scan/file-task + C3 events，`src/features/files/services/`，`tests/files/test_files_domain_services.py` 13 项；thumbnail-preview/import-export/jobs 属 E/后续批次）
- [ ] UI 调用类型化异步 facade；阻塞扫描、探测、解码和 I/O 进入有界 worker，结果附库/位置/请求/版本身份。（**部分**：C2 `FilesFacade` 有界 worker+代际丢弃+错误通道；D1 `48108e08` 起 `NativeFilesTransport` 数据面 14 路由生产走 facade+本地直读、面板信号接线原样——缩略图/胶片条/插件仍走 HTTP 桥，等 E/F-G）
- [ ] 明确 command 输入、错误码、revision、幂等、锁/权限、截止时间和取消；原生/插件/自动化入口使用同一实现，不保留旧HTTP协议。（**部分**：输入/错误码/revision/幂等键/协作取消在 C1 服务+B3 journal 落地；截止时间与"入口同一实现"待 D/F 换轨——旧 HTTP 仍在运行）
- [ ] 受理、排队、服务确认、窗口 ready、作业完成分开；所有异步消息按 invocation/job id 关联，迟到消息不能覆盖新调用。（**部分**：facade 代际丢弃+request_id 关联已测；queued→completed 全阶段词表在共享层，UI 侧展示属 D）
- [x] 事件订阅返回可注销句柄，带库作用域/版本；selection/progress 合并与背压，关闭/切库撤销旧订阅。（C3 `FilesEventBus`：幂等注销、库过滤+revision、progress last-wins 合并、有界 drop-oldest 计数、close_all；`tests/files/test_files_event_bus.py` 6 项）
- [x] 工程导入先暂存 assets/bins/轨道/片段，再由既有 command/事务一次提交；构造、取消、切工程/删轨和提交失败统一回滚。（既有 B08 异步投放路径经本轮复核确认：stale job/取消/切工程/revision 变化/删轨/锁定/宏失败七类守卫与补偿；`tests/edit/test_external_import_compensation.py` 3 项 + `tests/edit/test_timeline_external_file_drop.py` 8 项）
- [x] 一次 Undo/Redo 覆盖新增素材与落轨；复用旧素材不误删，revision/dirty/preview/通知与实际提交一致。（C5：`ImportDropMacroCommand` 携带新导入素材的 undo/redo 回调，仍被引用素材跳过、按原 asset_id 复位；`MacroCommand` 重建改 `dataclasses.replace` 保子类；`tests/edit/test_import_undo_covers_assets.py` 4 项。revision/dirty 由既有 bump 机制；preview 一致性属 D/实机）

**C 阶段边界（诚实）**：服务/facade/事件均为**契约测试口径**，无生产 UI 调用方（D 换轨后闭环）；thumbnail/import-export/jobs 服务未做；tests/edit 全量 13601 passed/25 failed 中 25 个失败经干净树复跑归因为先于本轮的顺序/环境脆弱（channel_layout/disable_shortcut/pointer_chrome 等逐一确认与本轮改动无关），定向命令/宏/撤销/投放批次 154 passed。

生产调用：Native Files、Library、插件和实际需要的新API注入同一service；feature不回调旧routes/globals。没有明确用途不新增HTTP层。
验收：同一命令由各实际入口调用，产生相同最终实体/DB/revision/错误；失败与取消无半提交工程。
失败策略：拒绝过期/跨库上下文，反馈实际已提交与待恢复内容，不用空列表掩盖服务异常。
完成证据：真实 facade 集成、持久化重开和工程 Undo/Redo 测试，标明可选服务缺席行为。

## D. 原生 Files 完整交互与独立宿主（FUP-05、NR-02/07/08）

依赖：B/C；媒体部分依赖 E。范围：接真实 facade，移除旧页面作为功能兜底。

- [ ] 独立 FilesPanel 默认只组装原生组件和 runtime；不导入旧 adapter，不建隐藏 browser，不启动旧 server/health/port。（**部分**：D1 `48108e08` transport 数据面换轨完成——浏览/搜索/集合/元数据/任务/扫描/批改名/预览取字节对旧服务启停无感（网络哨兵测试自证）；面板仍默认启动旧 server+隐藏 browser 供缩略图/胶片条桥与模式切换，待 E 媒体 provider 后摘除，见 [D1 验证记录](../validation/files_replacement_phase_d1_20261117/README.md)）
- [x] 目录、树、搜索、集合、清空、返回/刷新共用不可变 location/request/generation；发出新意图即作废旧上下文。（FR-08 统一 Location + D1 facade 代际/同键作废为既有主干；本批补三块：树请求独立于内容导航（导航 cancel 不作废已展开树）、连续切换集合旧响应丢弃（同 key 作废实证）、清空/收起搜索=放弃全库检索（退出虚拟视图回目录并重放，不留旧结果冒充当前视图）；45+327 项回归全绿）
- [ ] 201/2001/10000 项真实分页或虚拟列表，排序/total/cursor/选择/缩略图身份一致；输入防抖不保留旧结果写权。（**部分**：D3 已交付目录+全库搜索真实分页（offset 续页、total/truncated 一致、模型增量插入保选择/滚动、排序同 proxy、防抖三层作废）；虚拟列表未做（或关系已满足）；offset 页间漂移如实存在，1 万项实机验收留待第 9 项，见 [D3 验证记录](../validation/files_replacement_phase_d3_20261117/README.md)）
- [x] 四布局切换保持选择、滚动锚点、缩放偏好和多选命令语义，横竖混合素材无错位/裁切政策混淆。（选择/身份/多选与 fit/fill 像素政策为既有覆盖；本批补布局切换滚动锚点与缩放偏好持久化验证，14+29 项布局测试全绿，见 [D4 验证记录](../validation/files_replacement_phase_d4_20261117/README.md)；实机键鼠验收归第 9 项）
- [x] 原生任务列表展示逐项进度/错误、取消、重试、恢复；切位置/关窗口不丢任务归属。（D5：常驻台账+列表对话框——逐项进度/错误、协作式取消、失败项按原参数重试、提交失败匿名可见、切目录/关窗不丢归属；追加批次补「恢复」：facade 默认持久 journal（此前生产路径零记录的 B3 落地缺口）、意图目的地入账、启动即装载遗留未终态任务、按磁盘证据分类恢复（completed/可重试/证据不足保持待恢复并展示原因），见 [D5 验证记录](../validation/files_replacement_phase_d5_20261117/README.md)；跨进程任务历史浏览未做（job_log 有界，可选后续））
- [ ] 回收站列表/恢复/冲突、锁定原因/解锁、集合创建改名增删成员均有可达入口，写入经 B/C。（**部分**：D2 已交付回收站对话框（清单/恢复/DEST_EXISTS→改名恢复/彻底删除带确认）与集合全套写入口（从所选新建/改名/增删成员/删除集合），写入全经 B/C；「锁定原因/解锁」未做——新系统无目录锁服务，属矩阵 §2 待决定能力，见 [D2 验证记录](../validation/files_replacement_phase_d2_20261117/README.md)）
- [x] 标签/评分/信息编辑只使用服务最终快照；能力/选择/离线/锁/忙状态合并判断，提交侧再次核验。（快照回显=FR-11 既有不变量 + D1 transport metadata_snapshot 生产链路；能力门控禁用优先/提交复核=FR-12；离线门控预览/打开（Availability.OFFLINE）；忙状态在途显式可见、确认/失败都清位（本批补测）；「锁」无目录锁服务——矩阵 §2 待决定能力，与第 6 项同边界）
- [ ] 音频、必要格式、维护工具由原生或独立宿主接管；移除“完整Files/更多工具”回旧页的隐式路径。
- [ ] 焦点、键盘导航、文本输入快捷键、隐藏 Files、窗口尺度、国际化和失败/重试状态验收。

生产调用：所有按钮/右键/快捷键通过同一 command/facade；同一次选择在不同入口得到相同上下文。
验收：真实服务的乱序导航、分页、多任务和删恢复；四布局真实图片/视频只读查看，截图使用合成素材。
失败策略：离线/锁定/格式缺失可解释，不能为了响应点击转回旧页面；删除旧模式开关与自动fallback，不保留旧宿主作过渡工具。
完成证据：真实 UI→service→持久化→刷新链路和键鼠实机记录。

## E. 解码、预览、播放与 GPU 所属线程（FUP-06/07、NR-04/05）

依赖：B 的任务/资源契约；与 C/D 可按稳定接口推进。范围：共享 runtime 和既有渲染队列。

- [ ] runtime 持有在途 decode job/pool；完成即释放压缩/解码数据，取消排队和取消在途分开，关闭先作废回包。（**部分**：E1 `52ab6d90` 图片预览/比较换共享 `ImageDecodeRuntime`——单一无父对象线程池、完成即释放压缩字节/归预算、排队即丢与在途作废分层、关闭先作废回包且零同步等待；视频播放器仍各 widget 独立解码线程（有界泄漏关停纪律已在），共享化归 E3/E5 批次，见 [E1 验证记录](../validation/files_replacement_phase_e1_20261117/README.md)）
- [x] 跨预览/比较窗口统一并发、总字节/像素预算；tryTake 失败不丢运行任务引用，GUI 不同步等两秒伪装退出完成。（E1/E2：预览+比较窗口共用同一池与总字节/像素预算，超限立即回绝可解释；Compare/ImportProbe 两处 `tryTake`-后-清引用 的 GC 风险点改为引用保留到真实完成；Preview/Compare shutdown 不再 `waitForDone`——慢解码>2s 时关闭<0.5s 且迟到结果不回 GUI；100 次开关循环内存收敛，见 [E1 验证记录](../validation/files_replacement_phase_e1_20261117/README.md)）
- [ ] 分离 producer 耗尽、最后帧呈现、音频排空、用户暂停和 seek；只有实际呈现完成才发布 EOF。（**部分**：E3 `569ffc69` 三引擎语义换轨——哨兵只置 producer 耗尽并泊车；`_maybe_publish_eof` 判定"末帧已实际呈现 + 队列已清 + 音频排空（音源读完+待写清空+sink 放完）"才 `_on_playback_finished` 恰好一次；截断 StopIteration 与自然 EOF 分流（`_decode_stopped_sig`）；UltimatePlayer 以 EndOfMedia 区分自然播完/用户停止（结尾重播 vs 原地续播）；seek/重播重置发布资格。假会话+fake sink 全绿（31 项新/改），见 [E3 验证记录](../validation/files_replacement_phase_e3_20261118/README.md)；0/1/2/长片、有声无声、片尾 seek/重播的真机矩阵未跑，与 E7 真实格式/设备补证合并）
- [ ] 复用共享 decoder/时基，RGB/YUV 的比例、SAR、旋转、奇数尺寸、色彩/HDR 一致；保留本轮 Topos RGB 等比回归。（**部分**：E4 `f0adf475` 显示旋转补齐——DISPLAYMATRIX 探取（镜像/任意角不支持且如实返回 0）、RGB 路径落地（逐像素=源帧顺时针 90°）、旋转感知等比拟合（框转置）、有旋转不走 GPU YUV；SAR/奇数尺寸/24/25 时基/BT.2020+PQ 元数据矩阵补证（ffmpeg 合成素材 20 项新测）；Topos RGB 等比回归保留。边界：HDR 仍元数据级无色调映射、旋转仅 RGB 路径、真机竖拍素材未验（归 E7）、视频会话跨引擎合并属 E5，见 [E4 验证记录](../validation/files_replacement_phase_e4_20261118/README.md)）
- [ ] GPU 转换/上传/绘制/释放接既有任务队列与 deferred release；UI 只提交尺寸/DPR快照和呈现结果。（**部分**：E5 `da1157dd` 播放器 GL 画布换渲染线程路径——专用 QThread 共享上下文承担 YUV 上传/转换/letterbox 绘制，latest-wins 命令队列 + 双缓冲前台帧 + GLResourceRegistry deferred release（所属线程释放/失败留账）；GUI paintGL 只呈现（raw bind 零拷贝或 CPU 回读 QImage，按共享组自动择路），worker 失败回退原 GUI 直绘（行为不变）。真 GPU（RX 6900 XT/GL4.1）6 项验证：线程断言/letterbox 像素/latest-wins/呈现端到端/台账清零/回退。边界：无共享组平台 CPU 回读（本机未触发该路径）、initializeGL/paintGL 仍 GUI 线程（Qt 硬约束，整改的是内容 GPU 工作归属）、finish 同步非 fence，见 [E5 验证记录](../validation/files_replacement_phase_e5_20261118/README.md)）
- [ ] 独立音频共享设备/时钟，切源和播放互斥；波形/截取有预算、取消和安全输出。
- [ ] ProRes RAW/BRAW/R3D 等按真实 backend/SDK 报能力；超时/缺后端/损坏明确诊断，禁止无限等待或黑屏假成功。

生产调用：Files、独立播放器和插件 provider 共享播放/解码能力，不新增第二套播放器或直接从 UI 操作 GPU。
验收：0/1/2/长片、有声无声、片尾暂停/seek/重播、慢解码关闭、双窗口和100次开关；真实 GPU/音频设备补证。
失败策略：后端超时可取消和回收；仅允许明确错误类别回退，权限/锁定错误不得通过另一后端绕过。
完成证据：线程断言、资源收敛和真实格式/设备矩阵；CPU抽帧通过不冒充HDR显示或GPU播放通过。

## F. 生产插件宿主、权限和安装闭环（PR-01～06、PL-01～08）

依赖：C/D；格式贡献依赖 E。范围：接共享层已有实现，补其真实运行边界。

- [ ] 单一 Inventory 汇总builtin/已安装/显式开发源；安装根与发现根闭环，真实来源、声明/运行版本、重复身份和诊断进入 UI。
- [ ] 按新ID/状态模型重建固定项、最近项、快捷键和启停偏好；偏好/中心/命令读同一快照，无需旧id别名或旧偏好迁移。
- [ ] command 为唯一启动入口；菜单/工具栏/右键/快捷键统一参数、权限、选择、库 revision 和 deadline 复核。
- [ ] 权威复核与调度在同一持有revision/grant/token的边界内原子判断；停用/切库/撤权发生在worker复核与GUI提交之间时拒绝过期调用。DTO票据防伪不能代替该原子边界。
- [ ] 实现 window owner/singleton/context 更新，贡献注册 token、事件/菜单/快捷键注销；真实激活半失败完整回滚。
- [ ] 生命周期停新调用→按owner取消→确认终止→清理贡献；GUI使用异步推进，不 sleep；失败清理可重试。
- [ ] 补作业历史/日志预算和新 generation 重激活 API；晚到旧代消息不能污染新窗口/作业，长期运行不无界积累。
- [ ] provider owner/优先级/冲突解释/缓存签名/可回退错误类型真实执行；Inspector 异步装配、重试和世代失效。
- [ ] Files 的文件/网络/外部程序经实际 broker；配置命名空间、secret代理、库切换撤授权，声明权限不等于已强制执行。
- [ ] 不可信/重任务采用受限进程及有界RPC；平台实际 isolation/sandbox 如实报告。未完成时仅开放可信builtin，禁用未保护的第三方执行入口。
- [ ] 安装/更新/卸载接生命周期和 Inventory；新增权限重新确认，卸载代码/数据/凭据分开，不触及素材。
- [ ] 安装目标抵抗并发路径替换，证明可信root及祖先边界；不能只做 Path 检查后再使用。源/目标所有权一并验收。
- [ ] 按实际目标平台实现安装锁；若支持Windows则补无fcntl的等价实现，不能将拒绝安装当作支持完成；各支持平台的跨进程并发/异常退出语义一致。
- [ ] 安装事务补持久日志、fsync/目录同步和崩溃恢复；中断后current、payload、provenance和新配置一致，不承担旧安装目录兼容。

生产调用：安装/卸载→发现→Inventory→激活→command→窗口/provider/job→停用清理，每步可从真实UI追踪。
验收：A/B互不影响、激活失败、更新期间在途、重启/切库、复核后撤权、拒权、跨进程并发、路径替换、worker crash、超量输出；10插件由G逐项证明。
失败策略：本次更新前的新系统插件版本可恢复或明确禁用；不把未结束任务标cancelled，不因manifest自称builtin提升信任，不绕过不可用沙箱。
完成证据：真实宿主集成和跨平台故障矩阵，单独标明共享契约测试与生产闭环。

## G. 十个内建插件逐项接管（PR-06）

依赖：F；媒体/格式依赖 E。保留所需实际功能，按新API重写接管；无需兼容旧插件包、旧配置或旧入口。以下所有行未验收前不能将“10插件接管”标完成。

| 旧 id | 新接管方式与真实能力 | 专门退出验收 |
| --- | --- | --- |
| `format_factory` | 独立Qt工具 command/window，单输入与多选政策明确；安全转码/封装服务 | 输入快照、输出计划/冲突、进度/取消/失败恢复；不走旧 `/launch` |
| `image_factory` | 批处理command，传真实选中数组和输出计划 | 色彩/HDR、逐项结果、取消/关闭，生成结果同一索引入口回库 |
| `shutdown_timer` | 独立utility window，single instance/owner | 启动只开窗口；系统动作有明确意图，取消/退出无遗留倒计时 |
| `decode_benchmark` | 诊断command直接接既有Edit host service | 来源/时间线上下文、预算、取消、零素材写入；不借旧bridge |
| `code_editor` | Monaco独立受控宿主或等价编辑工具/provider | 读取/编码/外部变更/安全保存/恢复，新建与多文件入口；不依赖旧routes/JS globals |
| `image_editor` | 既有Color/Effect Catalog或独立图片编辑command | 预览/导出一致、undo、默认安全输出与写入事务，不能照搬直接写原图接口 |
| `ai_library_browser` | 查询provider复用shared vault/既有AI服务 | search/status/stats、离线/模型缺失、取消/切库、凭据边界 |
| `audio_pro` | 原生audio viewer/inspector及必要波形/截取 | 播放/停止/seek/切源/设备释放，截取安全输出；无旧JS播放器 |
| `video_pro` | shared playback + `.topos`格式/缩略图/metadata provider | 播放/封面/metadata/关闭语义完整；替换window_ready hook，无旧包导入 |
| `topos_image` | shared `.toos` provider与惰性codec加载 | TPIM/TPIC内容识别、整数/浮点/HALF、linear显示编码、缺codec诊断；媒体池不导旧模块 |

- [ ] 每行记录新入口、服务、新状态模型、关闭路径、自动化和实机结果；仅窗口成功打开不等于任务能力通过。
- [ ] 使用新command id直连；旧 `plugins.*`、Flask Blueprint、HTML/onclick注入、旧context及兼容别名不留生产调用。
- [ ] 调试工具/关机工具从Files核心分离仍保留可发现入口；复用已迁shared播放器，删除旧目录重导出壳。

失败策略：新配置可恢复且不可用原因明确；没有等价接管的能力保持未完成，不把用户源文件或无关配置随卸载删除。
完成证据：逐插件至少一条真实成功链路及失败/取消/关闭链路，无旧宿主运行记录。

## H. 新库初始化、开发数据重置与恢复演练（NR-09、FD-03/06）

依赖：B～G。范围：以全新临时/空库验收新结构；为新系统自身提供备份和恢复。无需读取、合并或迁移旧工程/旧库。

- [ ] 新库首次创建、表/索引/默认状态初始化与重复启动幂等；Files/Library/插件使用同一定位器，不自动接旧DB/config。
- [ ] 编写开发数据重置命令，先列受管目标和作用域，再按显式调用清空/重建；不追随链接、不扫描清理素材目录。
- [ ] 新系统备份含DB/schema/config、组织关系、回收站实体/清单及插件状态；缓存与可重建数据单列，实体引用可校验。
- [ ] 新备份格式有版本、校验和与一致性点；秘密/素材引用按自身政策保存，不把机器私有状态写入通用工程payload。
- [ ] 从本轮新系统生成的备份恢复到空目录，核对记录数、完整关系、实体摘要和实际打开能力；NFC/NFD/大小写/盘符/UNC与失联重链测试。
- [ ] 初始化/重置/新系统备份恢复注入崩溃、磁盘满、损坏/缺实体；重复执行可解释，保留可恢复副本，不要求旧schema兼容。

生产调用：新启动路径直接初始化新库；开发重置与运行时恢复各有显式命令，不触发旧库扫描和双写。
验收：空库启动→真实功能写入→保存重开→新备份恢复；开发重置重复执行正确且源素材摘要不变。
失败策略：不覆盖唯一可用的新备份，不删除源素材或无关目录；失败保留阶段和恢复指引。
完成证据：空库/重置报告、恢复命令、校验结果和失败恢复记录，明确没有执行旧工程迁移。

## I. 无旧目录、冻结包与物理退役（NR-08～10）

依赖：A～H退出条件全部满足。范围：验证独立性，随后删除旧运行时代码。

- [ ] 独立checkout中真正不含 `external/file_explorer/`，干净子进程从任意CWD导入/启动真实Files与主应用；不移动用户正在使用的目录做实验。
- [ ] 禁止旧包导入并监控实际调用：浏览/搜索/metadata/任务/恢复/插件/播放/退出全流程无旧目录资源、server/port/bridge/sys.path依赖。
- [ ] 重写旧“扩展路径/Flask必须存在”和旧协议测试为新架构门禁；保留有效行为断言，删除失效兼容断言，不维护旧测试运行时。
- [ ] 清理主窗旧path注入、benchmark桥、preferences旧config、退出worker调用、library_adapter重导出及UI回旧页代码。
- [ ] PyInstaller各目标产品按真实资源收集manifest、provider、codec/工具/本地库和i18n；无源码/无旧目录的干净机器启动验收。
- [ ] 明确首发目标与支持矩阵；每个宣称支持的平台分别验路径/锁/原子文件语义与可选后端。未测Windows/Linux标待验证，不伪称通过；断网/缺SDK不拖垮浏览。
- [ ] 完整实机流程：四布局浏览→万项搜索→集合/标签→复制改名移动→删恢复→预览→媒体池/落轨→Undo/Redo→保存重开。
- [ ] 冷/热大库与后台任务并行测量；100次开关/启停后线程、子进程、句柄、压缩/解码/GPU资源收敛。
- [ ] 删除旧兼容桥/自动旧库发现器；历史文档或DTO格式说明可保留，不以文字清零代替生产调用与资源依赖为零。
- [ ] 最后删除旧扩展及死资源，重跑上述门禁；其他页面合法使用的WebEngine/Flask独立保留，不误删其功能。

失败策略：任一门槛失败即保留退役未完成；恢复到可用版本或明确禁用问题能力，不重新偷偷启动旧宿主。
完成证据：无旧目录源码运行、干净冻结包、实际支持平台/设备矩阵、新系统备份恢复和最终生产调用清单全部可审阅；待实机项如实保留。

## 4. 退役后升级路线（PR-07、PL-09/10）

以下与“取代file_explorer”分开交付；不得用市场功能完成代替A～I，也不必等远程市场建成才退役。

| 升级批次 | 范围 | 验收 |
| --- | --- | --- |
| U1 作者工具 | command/window/background/format/Inspector最小模板；typed SDK、模拟上下文、显式开发启动、manifest校验/确定性打包 | 新作者完成只读分析→结果窗口→取消/关闭；无私有模块/机器路径依赖 |
| U2 分发与更新 | 签名索引/发行者信任/吊销/离线策略、权限差异、版本固定/回滚；市场schema与缓存 | 恶意/过期/未来索引、断网、来源下线和更新失败可诊断；已安装页始终可用 |
| U3 诊断与性能 | 按插件trace/耗时/峰值内存/贡献归属、脱敏诊断包；1000项清单检索、4K播放并行预算 | 测量后冻结预算，回归可比较；日志不含token、原始媒体或完整私人路径 |
| U4 可选生产力 | 按实际需求扩展组织规则、相似/OCR/地图/批处理与自动化适配 | 共用既有服务、权限、取消和revision，不重新造业务/媒体引擎 |

第三方安装/执行能力只有在F的真实隔离与权限验收完成后开放；hash仅证明内容一致，不冒充作者签名。

## 5. 保存、隐私与最终交付

- [ ] 测试优先临时目录、隔离SQLite和合成素材；私人图片/视频只读解码，不修改原文件、原缓存或真实库做故障注入。
- [ ] UI截图、性能样本和可提交夹具使用合成/公开授权素材；包含用户媒体或私人路径的原始报告只留本机，不进Git。
- [ ] 提交仅显式暂存代码、测试、文字计划和脱敏记录；检查无图片/视频/缩略图/截图、token、用户DB/config和绝对私人路径。
- [ ] 每阶段提交清楚注明实现、验证和限制；收尾报告提交号、工作区状态、未覆盖平台/设备，不写笼统“全部完成”。
- [ ] 只有A～I证据全部齐备，才把结论改为“原生Files已独立替代，旧扩展已退役”；U1～U4保持后续升级清单。
