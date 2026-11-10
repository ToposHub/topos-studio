# 剪辑／调色 Gallery 后端第二轮复查计划

日期：2026-11-10。复查基线：`bugfix` / `eb029dde`。
对应上一轮：`2026-11-10-color-gallery-backend-review-followup.md` 的 BR01–BR09。

上一轮的 9 个故障场景已全部收敛，`gallery_backend_review_20261110_repro.py --strict` 退出 0。本轮发现的是新增实现之间的交叉路径与未完成范围，不能据此否定已经通过的修复，也不能因为 9 项通过就将整个后端标为完成。

本轮小修已经落在工作区；以下 SR01–SR07 为仍需实施的集中清单。没有修改既有未提交的面板分隔条／布局工作。

## 1. 本轮已直接修复

| 问题 | 已完成修改 | 回归 |
|---|---|---|
| dependencies 为整数／布尔等时抛异常，空 dict／null 被当作合法列表 | 严格校验依赖容器类型；不接受非法安装名和坏 hash 格式 | `tests/color/test_gallery_review_round2.py` |
| journal schema 0／负数被接受，entries／finals 类型错误可抛异常或被过滤成空集合 | 共享 reader 拒绝未知 schema 和非法成员列表，调用方保留日志 | 同上 |
| 包的 manifest 有身份、但没有 meta 成员时，换发的新 ID 只写在 staging，最终被清掉 | 新建 meta 加入同一安装集合与意图日志 | 同上 |
| 导入恢复日志可指向库外文件，未知 state 也会执行删除 | 恢复入口拒绝不属于目标相册的路径和未知状态，保留现场 | 同上 |
| 库内 `LUTs` 是库外 symlink 时仍可成功写出库外 | 安装前验证资源目录／目标 realpath 归属，越界明确拒绝 | 同上 |
| 覆盖确认框期间相册被删除或 revision 改变，返回后仍提交 | 确认后重新校验目标相册；拒绝时不进入提交事务 | `tests/app/test_gallery_review_round2_capture.py` |

上述两份新套件共 **25 项**。结构与路径校验只是局部防护，**不代表已完成下文的事务所有权与依赖原子性**。

## 2. 可复现证据

新增脚本：`docs/validation/gallery_backend_second_review_20261110_repro.py`。
全部文件操作在 `TemporaryDirectory` 内；异步用例只使用脚本自己创建的工作线程，不操作用户工程或画廊。

```bash
.venv/bin/python docs/validation/gallery_backend_second_review_20261110_repro.py
# SR01–SR05 修复后要求全部 finding=false：
.venv/bin/python docs/validation/gallery_backend_second_review_20261110_repro.py --strict
```

| 场景 | 小修后实际结果 | 状态 |
|---|---|---|
| 同一线程创建第二个截图事务 | 第一笔尚在使用的 staging 被自动恢复清掉 | SR01，未修 |
| 导入恢复日志指向库外文件 | 文件与日志保留 | 本轮已修 |
| 恢复日志指向相册内同大小、不同归属文件 | 文件仍被删除，日志被清掉 | SR02，未修 |
| `LUTs` symlink 指向库外 | rejected，库外无写入 | 本轮已修 |
| LUT 安装后取消导入 | cancelled、bundle 文件清空，但遗留 1 个 LUT | SR03，未修 |
| 整个库目录移动，LUT 随库移动 | grade 仍引用旧绝对路径；新位置 LUT 存在，原引用失效 | SR04，未修 |
| A 的旧任务排队期间切代际，再提交 A 最新请求 | 最新请求被旧代际任务消费并丢弃，没有回填 | SR05，未修 |

同大小文件用例构造的是恢复日志与磁盘内容不匹配的现场；它证明当前仅靠 path＋size 无法确认归属，不声称已复现某种特定进程产生这个现场的概率。

## 3. 实施总表

| 编号 | 优先级 | 内容 | 依赖 | 状态 |
|---|---|---|---|---|
| SR01 | P1 | 活跃事务身份与同线程／跨进程互斥 | 无 | **已完成**（84e34ac0） |
| SR02 | P1 | 恢复与回滚的内容归属、完整性和旧日志保全 | SR01 | **已完成**（4f2f9de7） |
| SR03 | P2 | LUT 与 bundle 的统一事务、取消与资源管理 | SR01、SR02 | **已完成**（4f2f9de7） |
| SR04 | P2 | 受管资源稳定引用与库迁移 | SR03 | **已完成**（4f2f9de7，UI 警告透出同批） |
| SR05 | P2 | 异步请求的代际归属、回填与大库 UI | 可并行 | **部分完成**（1443ae0b：代际随请求／原子调度／同步共用 token／待选回填；分页模型与传输异步化见 §13） |
| SR06 | P2 | 真正的抓帧不可变快照与脚本生命周期 | SR01、SR02 | **部分完成**（bd2f2ade：grade 载荷冻结＋脚本 coordinator 登记；独立视频权威帧 probe 等见 §13） |
| SR07 | P2 | 原计划保留功能与设备验收 | 对应实现完成后 | 保留项维持诚实禁用；设备验收未做（§10/§13） |

建议优先 SR01→SR02；其余项目按依赖实施。每项关闭须附生产入口与执行端证据，不能只提交领域辅助类或把按钮改为 enabled。

## 4. SR01：活跃事务身份和锁

**定位**：`gallery_txn_core.py::_DirLockTable/acquire_directories/try_recover_lock/other_process_alive`、`StillCaptureTransaction.__init__`，以及 bundle/import 的恢复入口。

**已确认问题**

1. 目录锁使用 `threading.RLock`。同线程第二个事务可重复获取锁，再调用自动恢复；`other_process_alive` 对本 PID 恒返回 False，导致第一笔活跃事务被当成遗留事务清理。最小复现无需线程竞争。
2. 相同机制也允许恢复与业务事务在同一线程互相穿透；模态窗口、回调或脚本重入不能靠“主线程串行”排除。
3. 跨进程 pid 存活检查只能避免清扫某些日志，不是写入互斥。两个进程可能同时通过名称预检，然后各自覆盖目标。PID 复用、同一存活进程中已经结束的请求也没有请求级判断。
4. 初始化持锁后失败、回滚冲突异常与对象销毁的释放路径仍依赖细节；需要请求所有者明确管理，不能用宽泛忽略 release 错误代替平衡校验。

**实施**

1. 注册每笔活跃 txn_id／owner_token／目录集合；区分“本事务内部重入”和“新事务请求”，新请求不得复用另一个所有者的重入深度。
2. 自动恢复仅处理确定无活跃所有者的事务；日志中本 PID 不等于“可恢复”。同线程重入返回 busy 或进入既有任务队列。
3. 提供 OS 级跨进程锁／租约及启动身份，覆盖 capture、import、rename、move、restore 和资源目录；按固定次序获取多目录锁。
4. constructor／commit／rollback／取消／关闭统一 finally 释放，错误路径保留 journal；锁表不无限保留已无使用者的目录对象。

**验收**：两笔同线程 capture、capture 与 import／rename 的嵌套回调、两个独立进程同目标写入，均不得删除对方 staging 或覆盖对方资产；第一个请求的字节和日志始终可追踪。初始化任意步骤失败后新请求可正常获取锁。

## 5. SR02：内容归属与恢复后置条件

**定位**：`gallery_transfer_service.py::_rollback_import/_recover_import_txns`、`gallery_bundle_service.py::_rollback_copy_entries/_recover_committed_txn`、`still_capture_transaction.py::_restore_entry_to_old_set`、旧迁移恢复模块。

**已确认与代码核查**

- 导入只保存 final path＋size，同大小的其他文件会被当成本事务产物删除。本轮已挡住库外路径，仍不能确认相册内文件属于谁。
- committed copy-move 只检查目标“存在”，随后继续删源；没有核对目标内容是否完整、源是否已被用户重建。copy 回滚同样主要检查 size。
- 截图恢复依赖 size／mtime，旧日志缺少身份时 `identity_matches(None)` 直接通过；部分冲突分支在覆盖目标之后才验证备份身份。
- 本轮 reader 已拒绝错误容器，但缺字段、空 entries、重复／相互重叠路径等语义仍需各 kind 校验。日志是恢复意图，不能视为删除授权的全部证据。
- 恢复目录自身的 symlink／伪装事务目录，以及旧迁移模块的路径和状态验证，需纳入统一协议复核。

**实施**

1. 每成员登记预期内容 hash、稳定文件身份、staging 归属、目标原内容与动作意图；文件移入目标前日志必须已持久化。
2. 恢复删除／覆盖前先核对内容和所有权；不符则 conflict／quarantined，并保留原文件、备份和日志。先验验证备份，不先覆盖再发现不匹配。
3. committed 清源之前验证全部目标内容；源发生变化不删除，返回明确冲突。每次恢复必须幂等，不能只凭文件名存在就丢备份。
4. 各 kind 独立校验完整 journal schema、路径层级、角色、状态转换和成员唯一性；检查事务目录本身的真实归属。
5. 旧日志证据不足时保全隔离，允许人工确认／恢复；不能把缺字段自动降级成“匹配”。取消时回滚失败也必须保留日志并返回 recoverable 结果。
6. 明确 fsync 失败的合同；需要持久化保证的场景不能把文件 fsync 的 I/O 错误全部当作平台不支持。

**验收**：同大小不同内容／保留 mtime 的替换、目标损坏、源被重建、错误旧日志、伪装事务 symlink、恢复磁盘失联及取消回滚失败；任何不明归属文件都不被删除。新进程在每个文件动作前后强制退出，恢复仍得完整旧／新集合或有证据的保全态。

## 6. SR03：LUT 与 bundle 的事务边界

**定位**：`gallery_transfer_service.py::_install_dependencies/_import_bundle_locked/_collect_grade_dependencies`。

**问题**

- `_install_dependencies` 在 bundle journal 安装前直接写入最终 `LUTs/`；取消、后续依赖 hash 失败、身份保存失败或 bundle 安装失败，不撤回已经写入的资源。
- 资源安装被定义为“附加性写入”，与上一轮的取消零落地、统一事务目标不一致。即便采用受管缓存政策，也缺少索引、引用计数、孤儿回收和可观察结果。
- 最终资源名仅采用 hash 前 8 位加原名，若名字碰撞且内容不同，会覆盖既有目标；资源目录也不在目前的相册锁集合内。
- 导出读取／hash／打包没有同一内容快照；并发修改可能使 manifest hash 与包中字节不一致。导出依赖数量也未与导入 `IMPORT_MAX_FILES` 上限统一。
- inspect 与真正解压分两次打开归档；需要防止两次之间包内容被替换，避免只验证先前 ZipInfo 的大小限制。

**实施**

1. 首先校验全部 bundle 和依赖，再安装任何最终资源；将新建资源也登记在 BR01 共享事务中。
2. 若产品选择 content-addressed 共享缓存，改用完整 hash／稳定资源 ID，建立事务引用和 GC：取消不能留下无记录资源，不能删除其他 still 正在引用的对象。
3. final 安装采用不覆盖外来内容的策略；碰撞明确拒绝或分配新资源位置，进程间锁保护资源索引和目录。
4. 取消／失败／重启统一恢复资源与 bundle 引用。分块复制提供取消与进度，不只在整个 128MB 文件读完后检查。
5. 导出构建不可变快照并验证最终归档；导出和导入共享数量／单项／总量限制及格式规则，超限时明确拒绝，不生成本服务自己无法导入的成功包。
6. 归档校验与消费使用同一稳定句柄／快照，处理 CRC、解压异常与大小变化，清理／保全语义一致。

**验收**：依赖第 N 项坏 hash／安装失败、依赖成功后取消、身份保存失败、bundle 第 N 项失败，全程无未登记的最终资源；模拟 hash 前缀碰撞不得覆盖既有 LUT；导出边界数量可往返，并发改变源文件时要么完整成功要么明确拒绝。

## 7. SR04：受管资源引用不能绑定本机绝对路径

**证据**：当前 `_rewrite_staged_grade_references` 把 `active_lut_path` 写成 `<old-root>/LUTs/...`。整个 Presets 目录移动后，LUT 虽随库存在，grade 仍指向旧路径。本轮脚本验证了这一点。

**实施**

1. 持久化使用受管 resource ID／库相对路径；运行时统一 resolver 转为本机路径，不把一次导入的绝对位置当作长期身份。
2. 明确 Gallery grade 移到工程、工程导出、跨机器与重新导出时的资源所有权与打包行为。
3. 升级／迁移已导入的绝对引用：只重写能确认内容 hash 与库归属的记录，外部用户引用不猜测。
4. schema 1 或缺依赖包在导入结果、导出成功提示及应用预检中呈现缺失依赖；UI 目前只显示简短成功／失败信息，不能把 service warnings 静默丢掉。

**验收**：库改目录、工程换根、跨目录／用户根导入后，同一资源 hash 仍可解析；缺依赖明示降级，取消不改工程，重新导出不依赖旧库路径。

## 8. SR05：异步代际与 UI 完整接线

**确定复现**：旧 A 任务排队 → begin_generation → 新 A 请求合并进 pending → 旧 A 工作项读到新请求，但仍携带旧 generation → 最新结果被当作 stale 丢弃。

**同时需要处理**

- generation 绑定在 executor 提交而非实际消费的请求上；`submit` 解锁后再 executor.submit，仍与 shutdown 存在竞态。
- 合并 pending 不等于全局有界队列；不同 album 可继续产生工作项，过时任务缺少执行前取消协议。
- `scan_presets` 先在 GUI 线程全量 stat 生成签名，之后才决定是否异步；回填仍全量创建 QListWidgetItem、重建侧栏。不能宣称大库 UI 卡顿已验收消除。
- 同步扫描未统一递增 `_scan_generation`，可能与迟到异步结果交错；异步切相册期间旧条目仍在场，模型需声明加载状态／归属并控制动作。
- import／rename 后立即 `_select_items_by_stems`，大库重扫尚未回填时选不到新条目。
- Import／Export 真实服务仍从 UI 直接同步调用，较大的 bundle／LUT 复制压缩会阻塞窗口；需要任务取消和进度接线。

**实施**

1. pending 项保存 `(generation, request_revision, query)`；worker 消费的是该请求的代际，不能沿用旧工作项的代际。执行前和 GUI 提交前分别复核。
2. 统一 submit／shutdown 的原子调度；丢弃未执行旧任务，设置固定队列上限或 latest-wins 调度，不只合并相册字典。
3. 同步／异步扫描共用单调 request token；切相册立即更新模型归属和加载态，旧结果不得覆盖新状态。
4. 移走 GUI 全量磁盘 stat，增量／分页模型及可见区解码；成功后选中请求随 scan token 回填，失败保留原相册有效选择。
5. 传输走现有 job／service 调度，GUI 只发请求、收进度和结果；关闭与取消保证无迟到 QObject 访问。

**验收**：本轮最新请求丢失场景必须回填 revision=2；排队期间切 100 次相册／搜索结果收敛到最后请求；异步与同步切换、关闭竞争、导入／改名后的选择、1k／10k 首屏与搜索 p95、256MB 包操作期间窗口响应均有证据。

## 9. SR06：抓帧快照与脚本生命周期仍需收尾

本轮已修“覆盖框期间目标变化”，不再将它列作待实施。目的地锁定、grade_revision 与工程身份的阶段守卫也已经生效。

但 `CaptureFrameSnapshot` 当前只包含图像／HDR 帧；`_commit_still_capture` 保存 grade 仍把 live engine 传给 SaveRouter，meta 也再次读取 live 上下文。`_probe_gallery_capture_facts` 的帧信息仍主要取 PageContext，独立视频缺少权威控制器帧回退；脚本 session 没有与 UI 路径相同的 coordinator register/finish 取消登记。

**实施**

1. 请求／prepare 一次性冻结 grade 序列化数据、来源帧域、来源 meta 和色彩上下文；落盘只消费快照，不在图像落盘后再读取 live engine。
2. 为独立源视频提供权威帧 probe，clip-local／source／timeline 映射显式；不同呈现路径不共享模糊帧号。
3. UI 与脚本都登记到同一个 coordinator，关闭／取消能中断所有尚未 commit 的请求；跨线程渲染沿用现有队列。
4. 目标 revision 与所有权在持锁提交点检查，避免对话框后的再次 TOCTOU；旧兼容入口要么显式补齐快照，要么明确拒绝缺少事实的保存。

**验收**：独立视频 seek、prepare 后换 grade／工程、脚本捕获期间关闭、保存过程中上下文变化，不出现 JPEG／EXR／grade／meta 跨代际混装。此项当前主要是代码合同未闭环，尚未做真实 GPU 混装复现，不将其描述为已测得的显示错误。

## 10. SR07：原范围保留项与最终验收

上一轮实施记录已经明示下列范围未交付；它们继续保留在本表，避免反复将“部分基座完成”误作整项完成。

| 范围 | 当前状态 | 关闭条件 |
|---|---|---|
| 真分屏 presenter | 未实现，Start Comparison 禁用 | 现有渲染队列消费 Gallery 参考；当前图＋1 参考左右、当前图＋最多 3 参考 2×2；HDR／alpha／色彩／资源预算和结束恢复通过 |
| PowerGrade／Timelines 导航、项目相册 | 服务／导航不完整，部分入口禁用 | 深层枚举、跨工程／offline、项目存储生命周期和 UI 成功失败回填 |
| Memories／DRX | 未实现 | 格式与服务兼容合同＋真实执行端，不能只启用按钮 |
| Color Enhanced VFR／Source 预算通道 | 仍有既有范围保留 | 真实 PTS 节奏和实际解码／上传预算执行，不以诊断字段替代 |
| 其他旧禁用 Viewport 服务 | Proxy 等仍无完整执行端 | 按上一轮清单逐项实施并验收，或明确从完成范围排除 |
| macOS 原生菜单门 | Qt 菜单已接；原生绑定事件不可用的用例仍 skip | 真机菜单下 Space/J/K/L 验证，不能用 skip 视为已覆盖 |
| 真机 GPU／音频／性能 | 本轮离屏检查不能覆盖 | 4K60／HDR／VFR／alpha、外置卷、10 分钟播放、50 次换 clip／工程、30 次跨页／浮动／全屏循环；记录 p95、水位、内存纹理和音频听感 |

完成前更新 BR01–BR09 的“部分交付／待设备验证”状态，不删除历史验收记录。每项需附入口→服务→执行端→成功／失败／取消的证据和对应提交。

## 11. 本轮验证记录

- 回归文件清单：`docs/validation/gallery_backend_second_review_20261110_tests.txt`（102 个文件，含原 92 个、8 个 BR 新套件及本轮 2 个套件）。
- 上一轮复现脚本 `--strict`：9/9 finding=false。
- 新脚本：7 个场景中 2 个已修，5 个仍复现，对应 SR01–SR05；不把它们计为通过测试。
- 新增 25 个回归用例：21 项在修复前失败，4 项是既有拒绝行为的对照；修复后全部通过。
- 最终合并回归：**1812 passed / 4 skipped，65.78 秒**，退出码 0。跳过项为大小写敏感文件系统要求 1 项、既有固定跳过的曲线 LUT 测试 2 项、PyQt6 未暴露原生菜单事件 1 项；不计作通过。
- 隔离配置启动：`--test-startup --no-startup-dialog`，offscreen，退出码 0、无 traceback；不代表 GPU 呈现或音频听感验收。

回归重跑（仓库根目录）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
from pathlib import Path
import pytest
paths = Path('docs/validation/gallery_backend_second_review_20261110_tests.txt').read_text().splitlines()
raise SystemExit(pytest.main(['-q', '-rs', '--disable-warnings', *paths]))
PY
```

本表 SR01–SR07 尚未实施，本轮没有自动提交。

## 12. 实施记录（2026-11-10 SR 整改批次）

| 提交 | 内容 |
|---|---|
| f8506686 | 复查计划与复现脚本入库（本计划＋媒体池复核计划及验证产物） |
| 68a3d333 | §1 已直接修复的 color 侧小修与 25 项回归（复查过程产出） |
| 02d3e4b9 | §"已直接修复"的媒体池 9 类局部修复与 14 项回归（复查过程产出） |
| 84e34ac0 | SR01：`ActiveTxnRegistry` 进程内活跃登记（三套事务统一登记/注销）；flock 跨进程目录租约（进程内引用计数共享，锁文件在系统临时目录）；锁表引用计数清零回收；构造失败 finally 平衡释放；fsync 合同收紧（EINVAL/ENOTSUP/ENOSYS 之外上抛）。回归 `tests/color/test_sr01_active_txn_identity.py`（10 项，含真实双进程租约用例） |
| 4f2f9de7 | SR02：import finals/resources 带 sha256（删除授权=内容证据；无证据旧日志保全）；copy-move 条目记录源/副本 sha（回滚与 committed 清源先验全部目标内容＋源身份，源被重建→conflict）；截图备份先验身份、existed 无身份证据→conflict；重复条目/symlink 事务目录/资源越界全部隔离；取消回滚失败保留日志＋recoverable。SR03：依赖在事务 staging 解压校验，`resources` 段与 finals 同一 journal 先落盘；安装名 sha 前缀顺延（8→64），全 hash 碰撞明确拒绝，同内容幂等复用；单一归档句柄贯穿检查/解压；分块解压（块间取消＋字节数核对）；导出后验证最终归档（CRC＋成员 hash==manifest）并共享导入限额。SR04：受管引用 `topos-lut://LUTs/<shaN>_<name>`（`gallery_resource_refs.py`）；导入重写为受管引用；应用边界（`_prepare_preset_layers`）resolver 换算当前库根；`migrate_album_lut_references` 按内容证据升级绝对引用（扫描入口每相册一次接线）；导入/导出交互层透出缺依赖警告。回归 `tests/color/test_sr02_import_ownership.py`（9）、`test_sr03_lut_transaction.py`（7）、`test_sr04_managed_refs.py`（7）、`tests/app/test_sr04_managed_ref_apply.py`（4） |
| 1443ae0b | SR05：pending 项携带 `(generation, query)`——工作项消费**请求自己的**代际（复现修复）；submit/shutdown 锁内原子调度（executor 提交不再落在锁外）；pending 相册 FIFO 上限 128（`dropped_overflow`）；执行前取消计入 `dropped_stale`；面板同步/异步共用单调扫描 token（迟到异步结果不覆盖同步状态）；导入/改名待选随 token 在异步回填后生效。回归 `tests/ui/test_sr05_query_generation.py`（7） |
| bd2f2ade | SR06（可落地核心）：`CaptureFrameSnapshot` 冻结 grade 载荷（layers/raw_params/active index/tracking/stabilization 只读视图）＋合成图数据＋源素材路径，commit 全程不读 live engine（无快照的兼容入口回退 live 读，已声明）；脚本抓帧路径与 UI 路径登记同一 coordinator（register/finish 配对）。回归 `tests/app/test_sr06_capture_snapshot_freeze.py`（7） |

**验收记录**

- 本轮复现脚本 `--strict`：**7/7 finding=false，退出码 0**（修复前 5 项 finding=true）。
- 上一轮复现脚本 `--strict`：9/9 finding=false（未回归）。
- 合并回归清单（109 文件，本轮新增 7 个 SR 套件）：**1866 passed / 4 skipped，63.7 秒**；跳过项＝cv2 缺失 2、大小写敏感文件系统 1、PyQt6 未暴露原生菜单事件 1，均不计作通过。deselect 的 `test_curves_soft_clip_model::test_legacy_v2_layer_dict_loads_identity_and_resaves_stable` 为上一轮已溯源的**既有失败**（基线 c0cb9af2 干净树同样失败），非本轮引入。
- 全树分目录回归：`tests/color + tests/app`＝**6245 passed / 6 skipped / 2 failed**（仅上一轮已溯源的两项既有失败：curves soft-clip v2 用例、`test_fix_wd1g7_aud`，见上一轮实施记录）；`tests/ui`＝**2305 passed / 2 skipped**。全树单进程连跑（color+app+ui 一次 pytest）在 macOS Qt 6.10 存在与本轮改动无关的偶发段错误（`test_panel_dividers` 的既有 skip 理由同源），故以分目录结果为准。
- 隔离启动检查：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m src.main --test-startup --no-startup-dialog` 退出码 0、无 traceback。
- 有意行为变更并已更新对应测试：导入依赖引用由绝对路径改为受管引用（`test_br07_export_dependencies`）；伪造/旧格式恢复日志由"按 size 删除"改为"无内容证据保全"（`test_br01` copy 用例组）；在场事务不被自动恢复（`test_still_capture_transaction` prepared 用例模拟所有者消亡）。

**剩余范围（诚实声明，未交付）**

1. SR05 大库 UI：增量/分页/可见区解码模型未实施（当前仍是全量 QListWidgetItem 回填）；Import/Export 仍由 GUI 线程同步调用（大包会阻塞窗口，无取消/进度接线）；1k/10k 首屏、搜索 p95、256MB 包操作响应性未实测——不宣称大库 UI 卡顿已验收消除。
2. SR06 剩余：独立源视频的权威帧 probe（clip-local/source/timeline 映射）未实施；meta 采集仍含 live 上下文读取（grade 载荷已冻结）；未做真实 GPU 混装复现（合同闭环以单测证据为准）。
3. SR04 剩余：Gallery grade 移入工程/工程导出/跨机器场景的资源所有权与打包行为未定义实施（当前覆盖：库内迁移/导入导出/应用解析）。
4. §10 全部保留项（真分屏 presenter、PowerGrade/Timelines/Memories/DRX、VFR PTS、Source 预算、Proxy 等）维持诚实禁用；真机 GPU/音频/性能验收未做。
5. 媒体池复核计划（`2026-11-10-media-pool-backend-review-plan.md`）的 R01–R15 未在本批实施，另属独立整改批次。
