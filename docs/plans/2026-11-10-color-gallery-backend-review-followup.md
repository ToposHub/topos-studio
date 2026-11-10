# 剪辑／调色 Viewport 与 Gallery 后端复查整改计划

复查日期：2026-11-10。基线：`bugfix` / `c0cb9af2`。

本轮核对了 Gallery G01–G13 的实现、此前 Viewport R01–R06 的接线与验收记录，并执行相关回归。结论：主体接线已经落地，但**还不能标为端到端全部完成**。文件事务的异常恢复仍存在数据丢失风险；抓帧上下文、大库查询、分屏和部分导航／导入导出入口尚未完整闭环。

本表集中收录本轮发现的较大问题，以及旧验收记录明确保留的范围。后者单独标明，避免误认成新回归。未发现需要重写 Source 拖放宏命令、Source PTS 播放、Color 呈现路径快照的证据；这些已通过本轮相关回归。

## 1. 已直接修复的局部问题

以下修改与回归用例已在本轮工作区完成，不再排入待实施任务。

| 问题 | 修复 | 主要位置 |
|---|---|---|
| bundle 源名缺少验证，永久删除可接受 `../../Outside` | move/delete/export 入口校验源名；trash 恢复校验记录名；非字符串名安全拒绝 | `gallery_contracts.py`、`gallery_bundle_service.py`、`gallery_transfer_service.py` |
| 导入清单字段类型错误会抛异常；清单可以引用不存在的成员、重复角色或省略 hash | 校验 schema、字段类型、成员实际存在、名字／角色唯一、角色与后缀一致、SHA-256 格式；拒绝重复 ZIP 条目及控制字符；已超限的归档不再解压清单 | `gallery_transfer_service.py` |
| `limit=1` 时 `total_matched` 错报为 1 | repository 与 index 均先统计完整匹配数，再截取返回条目 | `gallery_repository.py`、`gallery_query_service.py` |
| 侧栏可进入深层目录，但 repository 仅识别一层相册，显示空列表 | 按既有目录动态解析深层 User/System 相册；库外 symlink 查询拒绝 | `gallery_repository.py` |
| grade 探测时 ZIP 在检查后损坏可能中断查询 | 捕获 `BadZipFile`，按不可加载降级 | `gallery_repository.py` |

局部修复没有改动现有未提交的面板分隔条／布局工作，也没有启用尚缺执行端的按钮。

## 2. 复查证据与边界

- 92 个相关测试文件：**1702 passed / 3 skipped，57.70 秒**。清单见 `docs/validation/gallery_backend_review_20261110_tests.txt`。覆盖 Gallery、截图事务、旧 67 套件回归锚点、Source VFR／拖放、循环稳定性、保存路由、脚本 API 和真实视频探针。
- 三项跳过：一项要求大小写敏感文件系统；两项曲线 LUT 测试在既有代码中以 `skipif(True)` 跳过。后两项不能记作本机缺失 cv2 的证明。
- 隔离配置的应用启动检查：`--test-startup --no-startup-dialog` 退出 0，无 traceback。使用 offscreen，**不代表 GPU 呈现、听感或像素一致性验收**。
- 故障注入脚本：`docs/validation/gallery_backend_review_20261110_repro.py`。只操作临时目录，不读取／修改用户画廊。当前复现下表 9 个场景。

| 复现场景 | 当前结果 | 对应任务 |
|---|---|---|
| 导入第二个成员安装失败 | rejected，但留下 `Partial.meta.json` | BR02 |
| 目标仅有孤立同名 thumbnail | 导入 success，旧 thumbnail 被覆盖 | BR02 |
| rename 安装后、日志更新前模拟进程退出 | 旧名未恢复，新名存在，日志被清掉 | BR01 |
| move 恢复过程中磁盘不可用 | staging 中唯一副本与日志均被删除 | BR01 |
| 静帧覆盖：备份移走后、日志更新前模拟退出 | 旧文件和事务目录均未保留 | BR01 |
| 静帧覆盖：回滚恢复文件时失败 | 旧备份与事务目录均被删除 | BR01 |
| prepare 后切换相册，再调用真实 persist | 请求目标 User，实际提交目标 System | BR03 |
| grade_revision 从 1 变为 2 | 抓帧会话仍返回 success | BR03 |
| 原地修改 meta，不改目录条目 | 磁盘 label 已更新，query 仍返回旧 label | BR04 |

执行方式：

```bash
.venv/bin/python docs/validation/gallery_backend_review_20261110_repro.py
# 对应任务修复后，要求全部 finding=false：
.venv/bin/python docs/validation/gallery_backend_review_20261110_repro.py --strict
```

KeyboardInterrupt 用来模拟文件操作已完成、下一次日志写入尚未发生的进程退出窗口；这属于确定性故障注入，不声称做过真实断电实验。各 `finding=true` 是已知缺陷证据，不属于上面 1702 个通过的默认测试。

## 3. 实施总表

优先级：P1 为数据完整性或错上下文写入；P2 为功能闭环、性能和验收缺口。

| 编号 | 优先级 | 工作项 | 依赖 | 状态 |
|---|---|---|---|---|
| BR01 | P1 | 统一截图／bundle／恢复操作的持久化事务与故障保全 | 无 | 已完成（7f48a62e） |
| BR02 | P1 | 导入完整原子性、全成员冲突与身份提交 | BR01 | 已完成（a811d69e） |
| BR03 | P1 | 抓帧请求锁定目标、帧、grade 和工程上下文 | BR01 | 已完成（ce534ab9） |
| BR04 | P2 | 查询失效协议、真实异步 UI、分页与队列背压 | 可并行设计 | 已完成核心（36a60ee6；分页/可见区模型见范围声明） |
| BR05 | P2 | 所有动作统一执行门、服务入口与集合导航闭环 | BR02、BR04 | 已完成核心（693838a6；PowerGrade/Timelines/Memories 仍如实禁用） |
| BR06 | P2 | Gallery 分屏 presenter 与划像会话生命周期 | BR04、BR05 | 部分完成（19621bea；分屏 presenter 未实现，见范围声明） |
| BR07 | P2 | 导出依赖可移植性与缺失依赖反馈 | BR02、BR05 | 已完成（8b948047） |
| BR08 | P2 | 旧 Viewport 计划明确保留的生产执行端 | 与 Gallery 主链独立 | 部分完成（f10e29f0；菜单门已接生产 feed，其余见范围声明） |
| BR09 | P2 | 故障／并发／真实 GPU 验收与文档收尾 | 对应实现完成后 | 本轮完成故障回归与文档（真实 GPU 验收见范围声明） |

建议顺序：先 BR01→BR02／BR03，消除数据风险；再完成 BR04→BR05；最后接 BR06／BR07，并执行 BR09。BR08 可作为独立批次，但完成前不能将旧 Viewport 的完整产品范围标为全部完成。

## 4. BR01：事务恢复必须保留唯一副本

**定位**

- `src/features/color/core/still_capture_transaction.py`：`_commit_backing`、`_commit_installing`、`rollback`、`recover_still_transactions`。
- `src/features/color/core/gallery_bundle_service.py`：`_MoveTransaction`、`_CopyMoveTransaction`、`_restore_move_entries`、`recover_gallery_transactions`、`restore_from_trash`。
- 同步核查 `gallery_legacy_migration.py` 的恢复与清理策略，避免第三套相似逻辑再次分叉。

**问题**

1. 截图备份路径在 `os.replace(final, backup)` **之后**才写入日志。此窗口退出，恢复看不到 backup 归属，却删除整个事务目录，丢掉唯一旧副本。新建文件安装也有 installed 标记滞后窗口。
2. 截图 `rollback()` 在 finally 中无条件 cleanup；恢复备份失败依然删备份。
3. bundle 安装也先 replace 后写 installed。恢复函数吞掉单项 OSError，外层依旧删除日志／staging，并记为 recovered。**即时回滚有保留日志的分支，但下次自动恢复会破坏这份保全。**
4. JSON 缺失／损坏时直接删事务目录；不能据此证明目标未被触碰。
5. copy-move 复用 move 的日志 kind／恢复逻辑，跨卷回滚不能靠 `os.replace(dst, src)`。源删除进度也没有逐条恢复协议。
6. trash 恢复逐成员直接移动，失败可能半恢复；再次恢复又被已恢复的同名成员挡住。不能与 rename/import 采用不同的成功定义。
7. 日志记录绝对路径，恢复时未完整校验 schema、状态、成员归属和 realpath；坏日志不应驱动库外文件操作。

**实施步骤**

1. 提炼共享事务协议，明确 create／replace／rename／copy-move／trash-restore 的所有权与收敛策略。每项登记 `src/staged/final/backup`、原内容身份和预期后置条件。
2. **先落盘意图，后操作文件**；使用原子写日志、文件 flush/fsync 与相关目录 fsync。恢复以意图＋实际文件状态＋内容校验判定，不只依赖 installed 布尔值。
3. 恢复返回逐项结果：recovered／pending／conflict／quarantined。仅在全部后置条件验证成功后清理。失败、未知状态、坏 JSON、日志版本不支持时保留目录与唯一副本，给出可重试原因和诊断位置。
4. 引入目标／bundle 互斥与事务所有者标识。自动恢复不得清扫同进程或其他进程仍在执行的事务；并发 capture/import/rename 不得互相恢复或覆盖。
5. copy-move 独立恢复分支：提交前失败移除事务拥有的目标副本，保留源；提交后按日志继续清源，不用跨卷 rename 充当回滚。
6. trash restore 进入相同协议，记录已恢复进度，允许幂等重试；冲突不覆盖新产生的文件。
7. 旧 journal 兼容：可确认的旧格式迁移；无法确认的一律保全隔离，不能猜测后删除。

**验收**

- 在每个文件 replace 前后、每次日志落盘前后强制退出子进程；重启后只能得到完整旧集合或完整新集合。
- 覆盖新建／覆盖／删除／同卷／模拟 EXDEV／restore，每一成员注入 ENOSPC、EACCES、设备失联；失败至少保留一份可验证的完整旧内容及恢复记录。
- 重复恢复三次结果幂等；坏 JSON／库外路径／symlink／未知 schema 不修改未归属资产。
- 两次同目标抓帧和 capture 与 rename 同时触发时，明确排队或拒绝，不误清对方 staging。
- 将本轮脚本中四项事务恢复场景转成正式回归；`--strict` 对应项全部消失。

## 5. BR02：导入提交、冲突和身份必须同一事务

**定位**：`gallery_transfer_service.py` 的 `import_bundle`、`_unoccupied_stem`、`_rollback_import`、`_recover_import_txns`、`_identity_conflict_action`、`_reallocate_imported_identity`。

**问题**

- 当前安装循环维护的是局部 `finals`，只有全部安装完成才写进 journal。第 N 个成员失败时，内存和磁盘 journal 都不知道前 N−1 个已安装目标，回滚漏删。
- 名称冲突只检查 `.topos`，会覆盖同名孤立 sidecar，也未统一大小写等价冲突规则。
- still_id 冲突后的 meta 换发在文件提交**之后**发生；写失败仍 success，留下未解决的身份冲突。同 ID／同 grade hash 的去重也没有比较参考图、alpha、meta 的差异；身份查重只覆盖目标文件夹，须明确库级还是 album 级身份。
- 导入任务缺少实际取消／进度协议。清单结构的局部校验本轮已修，但不能代替安装事务。

**实施步骤**

1. inspection 产出完整、唯一的角色到目标映射，再校验全成员占用、大小写等价与 symlink。沿用 G03 的冲突规则，分配出的名字在提交时再次校验并锁定。
2. 解压、hash 校验、meta ID 换发与 origin 记录**全部在 staging 完成**；拒绝未知／不兼容内容，明确 EXR 与声明文件配对政策。
3. 用 BR01 逐项意图日志安装；中断恢复只处理本事务拥有的文件，不能按最终文件名盲删。
4. 定义 duplicate 的完整内容指纹，至少包含 grade、参考图、精度／alpha；统一全库 still_id 冲突检查或明确变更身份作用域合同。
5. 身份保存失败导致整笔失败／回滚；成功结果返回实际 still_id 与文件名，区分 identity 和 stem。
6. 接取消令牌、进度和稳定错误码；commit 之前取消零落地，commit 之后如实返回已完成状态。

**验收**：六角色任意第 N 项安装失败无残文件；孤立 sidecar／大小写冲突不覆盖；ID 换发写失败不报 success；不同 album 中同 ID、同 grade 不同参考图、重复导入与进程退出均有用例。

## 6. BR03：抓帧必须使用不可变上下文

**定位**：`layer_controller.py` 的 `_build_gallery_capture_request`、`_probe_gallery_capture_facts`、`_persist_still_capture`、`_commit_still_capture`、`_collect_still_capture_meta`，以及 `gallery_capture_service.py`。

**问题**

- request 虽有 album_id，persist 却重新读取 `presets_panel.get_current_folder()`，没有使用锁定目的地。现有测试只在提交后切相册，漏掉 prepare 与 persist 之间的变化。
- `grade_revision` 字段既未由生产 probe 填入，也不在阻断比较字段中；工程身份和独立视频的权威帧位置也不完整。
- 截图、EXR、grade 保存和 meta 收集读 live 状态；prepare 与 persist 或覆盖确认对话框之间变化，可能把旧 JPEG 与新 grade／帧信息混装。
- expected_target_revision 未参与提交判断；persist 结果重新分配 request_id，无法完整追踪原请求。

**实施步骤**

1. 请求包含不可变目标路径／album_id、工程身份、权威 clip／asset／时间域／帧、grade revision 和色彩上下文；路径由 repository 解析，不能让 UI 当前选择决定提交位置。
2. 在同一呈现 generation 取得 JPEG、EXR 和序列化 grade；保存与 meta 都消费该快照，避免二次读取 engine/current context。
3. 建立真正的 grade revision 与独立源帧 probe；换工程、帧、源、grade、目标失效均在提交前复核。覆盖对话框返回后必须再复核取消令牌与上下文。
4. BR01 提交前校验 album revision／可写性／目标冲突。用户切相册允许继续写原相册，完成后仅刷新仍在场的目标；目标被移除则拒绝。
5. 全阶段保留同一 request_id；把 UI、脚本抓帧统一为同一服务，渲染调度和后台落盘沿用现有管线。

**验收**：prepare 后换相册写回原相册；换 grade／工程／独立源帧按政策取消或保存完整旧快照；覆盖框期间关闭／换源无落盘；JPEG、EXR、grade 与 meta 同帧同 revision，返回 request_id 不变。

## 7. BR04：索引正确性、异步消费和大库

**定位**：`gallery_query_service.py`、`gallery_repository.py`、`presets_panel.py::scan_presets`、`gallery_chrome.py::after_scan`、`preset_meta.py::matches_query`。

**问题与边界**

- 目录 mtime 能检测增加／删除／rename，不能检测原地修改 meta／JPEG／EXR／grade。手动 scan 同样命中缓存；扫描进行中 invalidate 之后，旧结果还能重新写回缓存。
- `AsyncGalleryQueryRunner` 存在，但面板仍同步 query 并全量重建 QListWidget；原报告 10k 首扫约 685ms，是查询代理基线，不能当作真实 UI 流畅度通过。
- runner 只有结果丢弃，缺少有界队列、旧任务取消／跳过；shutdown 与 submit 存在窗口，后台 queued tasks 仍可能继续工作。GUI 回填适配器尚未接入。
- selection 以路径恢复，rename 后丢失选择；`matches_query` 仍主要搜索旧来源字段，新增 label／still_id／timeline_name 没有完整消费。
- 深层目录的单相册查询本轮已修，但 scope 聚合仍由一层 `list_albums()` 枚举，虚拟 Timelines 对深层内容的完整性还需统一验证。

**实施步骤**

1. 定义库／album／bundle revision，内部变更显式 invalidate；手动刷新强制重建；外部变化采用事件或分层文件签名，覆盖原地写和 reference sidecar。
2. cache 发布检查 invalidation generation；失败扫描不能永久缓存成完整空库。缩略图缓存键包含内容 revision。
3. 正式安装 runner 和 queued GUI 回填适配器：按 album、工程、request_revision 复核；同类 pending 请求合并为最新，有界队列，关闭取消未执行任务并禁止迟到 QObject 访问。
4. 实现分页／可见区模型或增量批次插入；不要每次搜索／显示变化重建完整树和所有条目。选择跟随 still_id，旧条目没有 ID 时使用明确的迁移回退。
5. 扩展新元数据搜索，统一 repository、index 和 UI 口径；集合枚举按真实层级去重，忽略库外 symlink。

**验收**：原地修改后刷新可见；invalidate 与慢扫描交错不回填旧缓存；快速切 100 次相册／搜索队列有界；关闭无回调触碰已销毁 UI；1k／10k 真条目测首屏、搜索 p95、内存和滚动，不只测纯 repository。

## 8. BR05：动作执行门与真实入口

**定位**：`gallery_capability_host.py`、`gallery_contracts.py`、`gallery_chrome.py::apply_item`、`presets_panel.py` 的菜单／delegate、集合与传输服务。

**已确认缺口**

- 菜单由宿主门控，但双击／Enter 和中键直接 emit `grade_applied`，只检查文件存在；没有统一按目标 still、当前上下文和 shutdown 复核。
- 宿主按菜单显示文本匹配动作，当前选中项与右击对象可能不同；grade probe 只判断 ZIP 中有 `project.json`，不能证明结构可应用。实际 handler 仍有失败处理，不能把这项描述成已复现任意应用成功。
- import／export／Label、PowerGrade 和 Timelines 等服务到 UI 的完整接线尚未交付，能力事实没有反映可用服务。PowerGrade／Timelines 导航仍明确禁用，这是旧记录已披露的范围。
- project-scope album 没有存储服务；Memories 和 DRX 没有实现。它们属于原范围保留项，不能只改 enabled 状态交差。

**实施步骤**

1. 按稳定 action_key 提供 request/evaluate/execute 单一入口；菜单、快捷键、双击和中键都传目标 still_id／revision，在执行前再查权威上下文。
2. 能力判断使用被操作对象而非碰巧选中项；grade 内容探测复用生产解析／轻量校验，坏包返回稳定原因。
3. 将传输／集合／Label 服务真正接到 handler、任务结果和列表刷新；成功后选中新条目，失败不改变原选择，read-only 与多选限制一致。
4. PowerGrade 与 Timelines 导航启用前补深层枚举、全局／工程切换与 offline 策略；项目相册先实现存储、工程迁移和关闭行为。
5. Memories／DRX 单独定义格式与服务后再启用；若暂缓，保留真实禁用原因并在完成报告中排除。

**验收**：同一坏包／无媒体／只读／关闭态下，各操作入口结果一致；右击未选中项作用于正确对象；切工程时旧菜单动作不能落到新工程；启用入口都有成功、失败和取消回填。

## 9. BR06：分屏与划像会话的完整闭环

**定位**：`gallery_compare.py`、`compare_session.py`、`layer_controller.py::_begin_gallery_compare_session/on_split_compare_requested`、现有监看渲染宿主。

**现状**：划像已有真实 reference load 和精度／alpha 合同；split 仍是布局设置图与显式拒绝，Start Comparison 禁用。G13 完成了协议和 provenance 的一部分，**没有真实分屏 presenter**，旧验收记录对此如实注明。

**还需解决**

- wipe provenance 的 revision 仅取 `.topos` mtime，参考图／alpha sidecar 改变不会进入身份；color_context 当前是 fidelity 字符串，不是工作空间／输出变换的稳定快照。
- session 的 release 回调仅记诊断列表，未形成渲染资源所有权。删除／改名参考、工程切换、色彩设置变化与窗口关闭的失效政策和信号链不完整；不能据此直接断言目前已有 GPU 泄漏。
- split 缺少多参考实际呈现、预算、取消、失败回退及结束时视口状态恢复。

**实施步骤**

1. 以 still_id＋全 bundle revision＋实际加载精度／alpha＋色彩上下文生成参考身份；明确“继续持有已加载快照”和“源变化即失效”各操作政策。
2. 通过现有渲染队列和监看宿主消费 reference，接 ColorCompareSession 的 release 到真实资源；不在 UI 新建独立 GL 渲染体系。
3. 实现当前画面＋1 参考左右分屏，以及当前画面＋最多 3 参考的 2×2；区域内尺寸／色彩变换一致，HDR 不落到 QPixmap/JPEG 作为最终比较输出。
4. 统一绑定参考变更／删除／工程关闭／色彩上下文变化／关闭管线。异步 reference 迟到按 generation 丢弃，失败保留上一份有效比较。
5. 开始和结束均恢复原 zoom、overlay、transport、scope、预算状态；能力只有 presenter 安装成功后才启用。

**验收**：真 EXR／JPEG／alpha 参考，逐项验证左右和网格的像素区域、精度声明、色彩一致性；快速换参考／删参考／切工程无旧结果回填；预算、纹理与回调在结束后收敛。

## 10. BR07：导出包的依赖可移植性

**已确认**：export 收集六类 bundle 文件，并在 manifest 统计 LUT 引用数，但 grade 中 `active_lut_path` 原样保留，也没有把外部 LUT 一起打包。现有测试用 `/luts/a.cube`，往返只是证明引用字节还在，不能证明换机器后可还原相同调色。

**实施步骤**

1. 遍历 grade 的外部依赖，按支持格式分类；记录相对资源路径、内容 hash、版本和缺失原因。
2. 选择明确产品模式：完整便携包需打包依赖并重写引用，导入安装到受管资源目录；引用型包必须在 UI 和结果里明确告知不包含哪些依赖，不能称完整便携还原。
3. 引入依赖专用归档布局与独立限额，升级 manifest schema；兼容现有 schema 1，不得为支持子目录而放松路径穿越检查。
4. 缺 LUT／插件／版本不兼容的预检、继续／取消和降级结果统一返回，应用失败不半修改工程；资源生命周期与 BR02 事务一致。

**验收**：导出后移走原 LUT，在另一临时库／不同根路径导入，仍解析到相同 LUT hash；缺失依赖不能静默称完整成功；拒绝越界资源条目。

## 11. BR08：旧 Viewport 范围仍未完成的部分

以下来自 R01–R06 验收文档的明确保留项，本轮相关现有实现回归通过，不把它们误报为新增缺陷。

| 子项 | 当前边界 | 下一步与验收 |
|---|---|---|
| Color 视频 VFR 播放节奏 | Source 已消费 PTS；Color Enhanced 播放仍有 CFR 帧钟路径 | 沿现有解码管线接 PTS，统一 frame↔seconds 与音频位置；真实非均匀 PTS 素材测 seek、循环和播放节奏 |
| Source 预算执行通道 | 预算决策存在，但 Source 尺寸／优先级无真实请求通道，诊断诚实标记未执行 | 接实际解码／上传请求；实测隐藏源不抢监看预算、实际像素不超决策上限，保留用户质量偏好 |
| 菜单期间 transport 门 | `set_menu_open` 尚无完整生产 feed；本轮未复现按键穿透 | 接真实原生／Qt 菜单生命周期，验证打开菜单时 Space/J/K/L 不触发底层播放；先提供证据再定级 |
| 其他禁用服务 | Proxy/Resolution/Bypass、Unmix、独立 Grade Freeze、Alpha 显示、viewer-only 全屏等仍有旧范围保留；Edit 另有 Source Tape／源 markers／倒放音频／外部监看 | 各自补执行端、结果回填、关闭行为；无执行端继续禁用，完成报告逐项声明范围 |

## 12. BR09：完成标准与回归

1. 先将复现脚本中的故障点转成正式测试；进程退出使用子进程强制终止，再由新进程恢复，避免只测同一对象的内存日志。
2. 保留本轮 92 个文件的回归清单；按新增实现加入专门测试。3 个既有 skip 逐项记录，不能计为通过。
3. 真实素材继续使用 `tests/test_videos`；补 4K60／HDR／alpha／VFR 和外置卷，记录设备、素材、呈现路径及失败回退。
4. 真机 GPU 检查：划像／分屏、10 分钟播放、50 次切 clip／工程、30 次 Edit↔Color↔Composite↔Deliver／浮动／全屏；测 p95、队列水位、内存／纹理回收和音频互斥听感。
5. 对每项输出“请求入口→服务→执行端→成功／失败／取消回填”的证据。分屏 disabled、同步查询或缺依赖声明仍在时，不能用领域 DTO／单元测试代替功能完成。
6. 更新旧 G01–G13／R01–R06 完成记录，保留历史证据，链接本表实际修复提交；BR01–BR09 逐项验收后再更改状态。

本轮仅完成第 1 节的小修和复查证据整理，BR01–BR09 尚未实施。

## 13. 实施记录（2026-11-10）

本轮（基线 `c0cb9af2` 之后）逐项实施 BR01–BR09，批次提交如下：

| 项 | 提交 | 交付 |
|---|---|---|
| BR01 | `7f48a62e` | `gallery_txn_core`（原子 journal/fsync/内容身份/库外防护/目录互斥）；still-capture 意图先行备份＋实际状态恢复＋回滚失败保全；bundle copy-move 独立恢复分支、坏日志/越界路径隔离、trash 恢复幂等；`test_br01_gallery_txn_recovery` 21 例 |
| BR02 | `a811d69e` | 导入意图 journal 覆盖全部 finals（第 N 成员失败零残留）；全成员占用冲突（孤立 sidecar/大小写）；staging 内身份换发（失败整笔拒绝）；完整 bundle 指纹去重；取消/进度；`test_br02_import_atomicity` 15 例 |
| BR03 | `ce534ab9` | 目的地由请求锁定（切相册写回原相册）；grade_revision/project_id 真实 probe＋阻断；prepare 同代际快照（persist 不二次渲染）；对话框返回后复核；稳定 request_id；`test_br03_capture_context` 10 例 |
| BR04 | `36a60ee6` | 分层文件签名（原地改写可检测）；缓存发布代际守卫＋失败不缓存；runner 同 album 合并＋有界队列＋shutdown 丢弃；面板异步回填（≥2000 条）＋队列化桥；选择跟随 still_id；搜索补 label/still_id/timeline_name；`test_br04_gallery_async_query` 13 例 |
| BR05 | `693838a6` | 统一执行门（菜单/双击/Enter/中键按被操作对象复核）；import/export bundle 真接线；Label 服务；成功选中新条目；关闭态与未安装区分；`test_br05_action_gate` 9 例 |
| BR06 | `19621bea` | bundle 全成员 revision（sidecar 原地改写可检测）；color_context 打包精度+alpha；删除/改名失效联动＋引擎参考释放；快照政策显式声明；`test_br06_compare_lifecycle` 9 例 |
| BR07 | `8b948047` | LUT 随包携带（schema 2 dependencies）；导入安装受管 `LUTs/`＋引用重写（跨库同内容 hash）；缺失依赖如实 portable=false；越界/坏 hash 拒绝；schema 1 向下兼容；`test_br07_export_dependencies` 5 例 |
| BR08 | `f10e29f0` | 菜单期间 transport 门生产 feed（app 级事件过滤→set_menu_open）；`test_br08_menu_transport_gate` 4 例 |
| BR09 | 本提交 | 故障回归与文档收尾（见下） |

### 验收记录

- 复现脚本 `docs/validation/gallery_backend_review_20261110_repro.py --strict`：**9/9 场景 finding=false，退出码 0**（事务恢复 4 项、导入 2 项、抓帧上下文 2 项、原地写缓存 1 项全部收敛）。
- 复现脚本的 9 个故障点已全部转为正式回归（`test_br01/02/03` + `test_br04` 的 in-place 失效用例），不再只依赖诊断脚本。
- 92 文件回归清单 + 全部新增 `test_br0*` 套件：**1787 passed / 4 skipped**，无新增失败。
- 全量 `tests/color + tests/app + tests/ui`：**8471 passed / 8 skipped**，除下列**既有失败**（均在本轮基线 `c0cb9af2` 前引入、与本轮改动无关）：
  - `tests/color/test_curves_soft_clip_model.py::...test_legacy_v2_layer_dict_loads_identity_and_resaves_stable`（在干净 `c0cb9af2` worktree 复现失败）
  - `tests/app/test_fix_wd1g7_aud.py::...test_load_video_success_applies_session_swaps_after_load`（`_refresh_color_timecode_context` 由 2026-11-09 `74178007` CV-B02 引入，测试的假宿主无该属性）
- 隔离启动检查：`--test-startup --no-startup-dialog`（offscreen）退出 0、无 traceback——不代表 GPU 呈现/听感验收。
- 未做真实 GPU 呈现/听感/像素一致性验收（划像/分屏、10 分钟播放、50 次切 clip/工程等 §12.4 清单）——offscreen 回归不代表它们。

### 剩余范围（如实声明，未实现项保持真实禁用）

1. **BR06 分屏 presenter**：多参考左右分屏/2×2 的真实呈现执行端未实现——`compare.split` 能力位仍如实禁用，Start Comparison 入口明确拒绝并说明；本轮交付的是其生命周期/失效/资源释放基座（`ColorCompareSession` 接真实引擎参考释放）。
2. **BR04 分页/可见区模型**：查询层已有 `limit`/`total_matched`；QListWidget 全量重建未改为增量插入——异步回填＋代际守卫已消掉 UI 卡顿主因，1k/10k 真实条目的 p95/内存实测仍属设备验收。
3. **BR05 集合导航**：PowerGrade/Timelines 深层枚举与 project-scope album 存储服务、Memories/DRX 未实现——菜单项保留真实禁用原因。
4. **BR08 其余子项**：Color Enhanced VFR PTS 帧钟（现状仍是 wall-clock×fps 的 CFR 路径）、Source 预算执行通道、Proxy/Resolution/Bypass/Unmix/Grade Freeze/Alpha 显示/viewer-only 全屏等禁用服务的执行端——均未实施；macOS 原生菜单栏下拉不经 QWidget，菜单门覆盖 Qt 菜单/右键菜单（PyQt6 绑定暴露原生事件后自动生效）。
5. **BR09 设备验收**：§12.3–12.4 的真实素材（4K60/HDR/alpha/VFR/外置卷）与真机 GPU 检查未执行。
