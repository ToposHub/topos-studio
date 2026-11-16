# Files 替代计划完成度复核与后续整改

日期：2026-11-17。审查起点：`fix-ui @ 7f025973`；本轮局部修复为该提交之上的工作区变更。
对应[原计划](2026-11-16-files-full-replacement-review-plan.md)。本文件记录核查结果及留给后续实施的工作，不代表下列任务已经实施。

**结论：NR-00～05 已有大量实现和局部自动化，但各批次的完整退出门槛尚未满足；NR-06～10 尚未实施。当前不能删除 `external/file_explorer/`。** 尤其 FR-01 的 P0 安全问题仍能在目标被替换的注入交错中复现。不能把“测试通过”或原表的勾号当作整批验收完成。

本轮按用户要求仅直接修局部问题；安全文件事务、工程事务、共享 runtime、GPU 归属与后端迁移保留为后续计划。没有搬移旧扩展、迁移真实数据库或改写真实素材。

## 1. 批次完成情况

| 原批次 | 本轮结论 | 已有交付 | 尚缺的退出条件 |
| --- | --- | --- | --- |
| NR-00 | 部分完成 | FR 编号对应的回归套件、隔离夹具、历史复现证据存在 | 部分断言与真实入口有覆盖盲区；真实库完整备份与恢复演练没有本轮证据 |
| NR-01 | 部分完成，P0 未关闭 | 进程内目标预约、文件分块复制、临时替换、关系迁移 helper、v2 回收站、任务池 | 真正原子 no-replace、所有改名/恢复入口贯通、完整关系恢复、目录/跨卷/退出路径 |
| NR-02 | 部分完成 | 虚拟位置索引/写入保护、防抖、标签快照、能力门控、多任务轮询 | 目录/搜索/集合统一世代；原生下一页；原生任务列表/取消。FR-10 不只是缺 10000 条测试，而是第 201 条起没有 UI 加载入口 |
| NR-03 | 部分完成 | 宏失败后的新增素材补偿、复用素材保护 | 构造阶段异常也要补偿；assets+bins+timeline 的统一提交与 Undo/Redo |
| NR-04 | 部分完成 | 窗口会话、后台解码、单图/显示像素限制；本轮补预览请求身份与模板快照 | 运行中解码任务保活/完成回收、非阻塞退出、跨窗口总预算；改名计划与执行版本绑定 |
| NR-05 | 部分完成 | 尾帧索引不再重复、Topos 时基公式、等比解码/SAR、尺寸快照 | 解码完成与呈现完成仍混用；GPU 仍在 GUI 线程；真实格式/设备矩阵 |
| NR-06 | 未实施 | 现有 DTO/import_job 可复用 | repository/service/runtime、同服务的 HTTP/直连适配、数据位置独立 |
| NR-07 | 未实施 | 有删除和任务 API，具备部分原生预览基础 | 回收站列表/恢复/解锁、任务取消 UI、独立音频与必要格式维护闭环 |
| NR-08 | 未实施 | 共享播放核心已经迁入 src | 原生 Files 仍创建 WebEngine、启动旧服务；跨域工具和 `.toos` 仍反向依赖扩展 |
| NR-09 | 未实施 | 旧 DB 快照接口可作为基础 | 完整迁移/恢复演练、物理无扩展 checkout 与冻结包验收 |
| NR-10 | 未实施 | 原计划有退役清单 | 必须先完成 NR-09，当前不具备物理退役条件 |

## 2. 本轮已经直接修复的局部问题

| 修复 | 变更和验证 |
| --- | --- |
| 批改名执行回包方法被覆盖 | 删除 transport 重复的 `_finish_batch_rename`，保留执行会话 ID；HTTP/网络失败、无效或未确认的响应不能报成功；补真实 `execute → reply.finished → result` 的假网络链路测试 |
| 旧预览确认新模板 | 批改名每次预览独立 token，并保存请求时模板；输入修改、重复预览和乱序失败不能把旧计划确认给当前输入；执行仍使用稳定的窗口会话 ID，宿主接线签名不变 |
| 同路径重开图片的旧 fetch | 读取、长度检查、完成回调验证 reply/state 当前身份；旧 fetch 不再被面板接收为新一代解码；补旧回包先到/后到两种顺序 |
| 能力禁用原因缺失 | 未选择素材时也显示服务的禁用原因，保留 Capability DTO 的 reason；能力恢复后仍须有选择才能启用编辑；修正旧测试的过时启用断言 |
| 文件并发测试会误绿 | 移除 `assert ... or outputs` 非空字典兜底；分别断言 error/overwrite/rename 的输出数量、获胜内容和源文件保留规则，修正全部失败分支的反向断言 |
| 原计划状态和链接 | 标明历史状态与本次复核的区别，修正“全部待整改/待实施”的时间口径，以及机器用户名导致的失效链接 |

相关入口：[批改名窗口](../../src/app/ui/native_files_batch_rename.py)、[transport](../../src/app/ui/native_files_transport.py)、[能力控件](../../src/app/ui/native_files_panel.py)。这些小修不表示 FR-13 的后端计划绑定、FR-14/17 的运行期资源回收已完成。

## 3. 留待后续实施的大问题

证据分为 **R：临时文件/SQLite/假会话隔离复现；C：源码调用链确认；V：仍待真实设备/平台验收**。注入交错不冒充真实跨卷或跨进程验收。

### FUP-01：文件安全事务与任务生命周期（P0，FR-01/02/05/07）

**已确认（R/C）**：`error` move 创建 O_EXCL 占位后仍调用可覆盖的 `os.rename`；注入占位被其他写入者替换，第三方内容被覆盖且源消失。目录复制第二项取消后第一项仍留在目标中。目录 rename 误走文件占位流程并报 `NotADirectoryError`。overwrite move 注入 EXDEV 后直接失败，没有跨卷实现，源保留。线程数有界，但提交队列未限制；未查到宿主退出调用该任务服务 shutdown 的接线。

入口：[file_tasks.py](../../external/file_explorer/file_tasks.py) 的 `_move_file_payload/_do_copy/_do_move/_do_rename/shutdown`；[routes.py](../../external/file_explorer/routes.py) 的 `api_tasks_start`；[main.py](../../external/file_explorer/main.py) 的退出接线。

后续工作：

1. 定义源身份、链接、目录树、跨卷和各冲突策略；所有真实读写对象统一在领域入口校验锁/权限，不只校验 HTTP 请求。
2. 使用平台真实 no-replace 原语或可证明的等效安全策略；占位后盲目 rename 不作为完成条件。父目录换链和源身份变化纳入边界。
3. 目录树/跨卷采用可恢复 staging，失败与取消不留下伪完成树；覆盖已有目录时明确原内容保护与恢复方式。
4. 有界提交、活动记录、退出等待/取消、重启后未完成任务诊断接入统一 runtime。

验收：同目标 copy/move/rename 的三策略 × 文件/目录 × 同卷/跨卷；占位/目标/父目录/源换链；源重叠/父子树任务；复制到中途取消、磁盘满、权限失败、退出与恢复。所有未授权覆盖为零，成功/失败后的源和目标、任务状态均可解释。

### FUP-02：所有改名/移动入口统一关系迁移（P1，FR-03）

**已确认（R/C）**：`_move_meta_rows` 本身已修列映射和 DB 事务，但旧单改名、批改名 API 仍各自更新部分表并吞下关系迁移异常。真实 Flask test client 返回成功后，`file_meta` 指向新路径，`markers.file_path` 仍是旧路径。原生批改名仍调用该 API，因此 helper 测试通过不足以证明入口完成。

入口：[routes.py](../../external/file_explorer/routes.py) 的 `api_rename`（约386）、`api_batch_rename`（约852）、`_move_meta_rows`。

后续工作：单改名、批改名、任务 move/rename 共用同一领域变更入口；覆盖所有关系、目录子树、缓存和 FTS；文件成功而 DB 失败时记录可恢复状态/补偿，不能仅记日志再返回成功。

验收：分别从真实 API/原生 transport 调用，核对 OCR、markers、标签、集合、堆叠、FTS、未知字段；逐表注入失败，不出现部分提交伪成功。

### FUP-03：回收站完整快照与安全恢复（P1，FR-04）

**已确认（R/C）**：成功删恢复后，集合 `sort_order/added_at` 从 `17/123` 变 `0/null`，堆叠 `created_at` 从 `456` 变 `null`。快照/关系清理异常仍有 warning 后继续的路径；恢复仍是 exists 后 `shutil.move`，未进入安全目标策略。

入口：[trash.py](../../external/file_explorer/trash.py) 的快照、删关系与 `restore_items`。

后续工作：版本化清单完整保存关系字段与未知字段；必须快照成功才允许删除。恢复冲突统一采用 FUP-01 安全写入策略，实体、DB、manifest 作为可恢复事务处理。

验收：完整字段逐项相等，包含排序和时间；同名占位、子目录、重复删除/恢复、损坏清单、DB失败、跨卷/磁盘满；冲突副本不串组织关系，失败也能重试。

### FUP-04：工程导入、落轨与 Undo/Redo 原子性（P1，FR-06）

**已确认（R/C）**：补偿覆盖部分提前返回和宏执行失败路径，未覆盖构造阶段抛异常。真实方法的 AST 夹具注入片段构造异常后，新增 asset 留下1个、工程已发布1次、无失败提示。暂存轨道 `apply`、无有效片段但存在新增轨道命令、部分素材构造失败也需完整验证。导入仍在 Undo 宏外，原计划已明确这一点。

入口：[timeline_view.py](../../src/features/edit/ui/timeline_view.py) 的 `_finish_external_import`（约16930起）；[现有补偿测试](../../tests/edit/test_external_import_compensation.py)。

后续工作：先暂存导入/轨道/片段，使用工程级 command/transaction 一次提交；将所有构造、取消、切工程/删轨、提交失败路径归入同一回滚边界；反馈与实际提交结果一致。

验收：失败/取消无新 asset、bin或轨道残留；一次 Undo/Redo 同时覆盖素材和落轨；project/revision/dirty/preview/通知次数一致，复用的原素材不受误删。

### FUP-05：浏览请求上下文、分页和任务原生入口（P1/P2，FR-08/09/10/16）

**已确认（R/C）**：搜索与集合各只取消同类请求。先搜索后集合、集合先回包时，仍收到 `collection → search`，旧搜索未取消。清空输入、首个结果前退出全库也没有统一失效。搜索入口均固定 `limit=200, offset=0`，没有下一页 UI。多任务轮询已存在，但 `cancel_task/active_task_jobs` 没有原生任务列表/取消调用方。

入口：[transport](../../src/app/ui/native_files_transport.py) 的 `search_library/load_collection/load_directory/cancel`；[panel](../../src/app/ui/native_files_panel.py) 的 `_filter_changed/_emit_debounced_search/refresh` 及文件任务反馈。

后续工作：

1. 目录、搜索、集合、清空、返回、退出共享不可变 location/request_id/generation；在发起意图时立即作废旧上下文，不等新结果返回。
2. 接入完整分页/加载更多或虚拟列表；服务排序/total/next_cursor 与前端追加、过滤、选择和缩略图索引一致。
3. 增加轻量任务列表：逐任务进度、失败详情、取消、超时/重连；切换位置不丢归属。

验收：目录↔搜索↔集合所有乱序组合、清空/返回/关闭、输入防抖期间切位置；201/2001/10000条无漏重且尾页终止；双任务交错完成/取消/失败均有原生反馈。

### FUP-06：解码 runtime、保活、异步退出和总预算（P1，FR-14/15/17）

**已确认（C）**：`PreviewDecoder.cancel` 将 job 放入 `_retired`，完成时没有移除，job 的压缩 bytes 留到 shutdown。shutdown 同步等2秒，忽略 `waitForDone` 结果后清空引用。Compare teardown 无视 `tryTake` 是否成功即清空 job 引用；带父对象的线程池随窗口销毁可能阻塞 GUI。当前只有单图/单 pixmap 预算，没有跨窗口总预算。上述是调用链风险，本轮未声称已实机复现崩溃。

入口：[preview.py:94](../../src/app/ui/native_files_preview.py#L94)、[compare.py:147](../../src/app/ui/native_files_compare.py#L147)、[窗口销毁接线](../../src/app/ui/files_panel.py)。

后续工作：runtime 独立持有运行中的 job/pool，完成后回收压缩与解码数据；取消队列与取消在途分别处理；关闭先作废结果，再异步退出/延迟回收，禁止清引用假装线程结束；加入总像素、总字节和并发预算。

验收：解码超过2秒时关闭/销毁、双窗口取消、100次切源/开关；关闭后无UI回调，无长时间主线程阻塞，线程和压缩/解码内存收敛，运行任务包装不被提前释放。

### FUP-07：播放完成状态与 GPU 任务队列（P1，FR-18/21；FR-19/20补验收）

**已确认（R/C）**：尾帧重复修复后，producer 在帧仍排队时就将 `_playing=False` 并触发 EOF；Topos 同时暂停音频。2帧假会话中，EOF时 `shown_index=-1`，队列仍为 `[0,1]`。Topos 的无音频消费分支在 `_playing=False` 时仍能显示排队帧。因此现有“索引正确、EOF一次”测试不能证明暂停、尾帧显示和音频收尾正确。GPU画布仍在 GUI `initializeGL/paintGL` 创建/上传/绘制，尺寸快照不等于 GPU 归属整改完成。

入口：[base_player.py:622](../../src/shared/playback/base_player.py#L622)、[Topos EOF/消费](../../src/shared/playback/topos_player.py)、[Libav EOF/时钟](../../src/shared/playback/libav_player.py)、[GL画布](../../src/shared/playback/gl_video_canvas.py)。

后续工作：分离 producer耗尽、呈现完成、用户暂停和seek；尾帧/音频消费完成才发布播放完成，所有UI和设备状态在所属线程提交；GPU转换/上传/绘制/释放接既有渲染任务队列与延迟资源回收。

验收：0/1/2/长片、有声/无声、ring排队、暂停、seek片尾、重播，核对最终展示帧/音频/时钟/图标；真GPU线程断言、resize/DPR/context丢失/关闭100次。FR-19时基公式与FR-20基本比例/SAR已有自动化，仍补24/25/30000÷1001、旋转、奇数尺寸、RGB/YUV、HDR与设备seek矩阵。

### FUP-08：批改名确认计划与执行身份（P1，FR-13）

本轮已修模板/回包/窗口身份问题。**仍缺（C）**：服务 preview 与 execute 分别运行 `_plan_renames`，没有绑定已确认的 files/template/plan/revision；中间文件或冲突变化会产生另一份执行计划，且数据传播仍受 FUP-02 影响。

后续工作：服务返回可核验的计划ID/版本/输入身份，execute校验并按已确认计划提交；计划失效明确要求重预览；逐项失败和完成刷新真实受影响位置。

验收：预览后外部改名/创建冲突/源被替换、模板变化、双窗口、部分失败与DB失败；最终文件名与确认计划一致，或明确拒绝执行。

### FUP-09：服务独立、数据迁移与最终退役（P1，FD-01～06 / NR-06～10）

**已确认（C/R）**：`src/features/files/` 仍只有 contracts/import_job；原生 transport 仍调旧 HTTP。Files宿主顶层导入旧 adapter/WebEngine，原生模式仍创建浏览器并启动旧服务。模拟旧 `main.py` 不存在，干净子进程仍报 `RuntimeError: Cannot locate external/file_explorer from adapter.py`。这只是缺入口注入，本轮没有物理移除扩展。

其他入口：[旧DB/config定位](../../external/file_explorer/db.py)、[CWD默认库](../../src/shared/db/database.py)、[工具反向依赖](../../src/shared/diagnostics/optional_dependencies.py)、[.toos provider反向依赖](../../src/shared/media/thumbnail_provider.py)、[备份接口](../../external/file_explorer/routes.py)。

后续顺序沿原计划，不另复制一套后端：

1. **NR-06**：复用既有 repository/provider 抽同一 query/command/runtime，HTTP降为薄适配；统一用户数据目录、库定位器和schema版本。
2. **NR-07**：原生回收站/恢复、取消/解锁、音频和必要格式入口，写操作依赖 FUP-01～03。
3. **NR-08**：独立原生宿主，拆旧adapter/server/browser/sys.path；工具定位与 `.toos` provider 迁共享域，并逐项处理旧插件去向。
4. **NR-09**：旧库/config/XMP/回收站实体/组织关系/未知字段幂等迁移；完整备份清单/校验和/恢复演练；物理无扩展 checkout 与干净冻结包矩阵。
5. **NR-10**：原计划§7清单全部核验后再退役。其他页面合法使用的 WebEngine/Flask 保留独立依赖，不随 Files 清理误删。

验收：完整“查找→预览→投放→Undo/Redo→保存重开”，冷/热大库与任务并行、断网/可选依赖缺失、macOS/Windows/目标Linux、大小写/Unicode/UNC、发布包升级和恢复。均须附真实service/UI/平台证据。

## 4. FR逐项口径

| 原ID | 截至本轮的完成边界 | 后续归属 |
| --- | --- | --- |
| FR-01 | 进程内并发用例通过；跨写入者目标替换仍可覆盖，P0未关 | FUP-01 |
| FR-02 | 目标末级软/硬链接保护已有回归；父路径/源身份/完整政策未验 | FUP-01 |
| FR-03 | helper事务通过；真实单改名/批改名关系遗漏已复现 | FUP-02 |
| FR-04 | v2快照存在；完整关系字段、失败停止、安全恢复未完成 | FUP-03 |
| FR-05 | 原报告mkdir parent等绕过用例已挡；领域统一校验和时间窗口未闭环 | FUP-01 |
| FR-06 | 宏失败补偿通过；构造失败和全工程Undo/Redo未完成 | FUP-04 |
| FR-07 | 单文件取消/线程池通过；目录取消、退出/恢复未完成 | FUP-01/06 |
| FR-08 | 位置索引与虚拟目录写保护通过；完整导航交错待统一 | FUP-05 |
| FR-09 | 防抖和目录取消已做；搜索/集合/清空跨上下文不完整 | FUP-05 |
| FR-10 | 后端稳定分页已有回归；UI仍只有第一页 | FUP-05 |
| FR-11 | replace/clear及快照链路已有回归；重开/XMP/部分失败仍需验收矩阵 | NR-06/09联调 |
| FR-12 | 能力与选择共同门控、提交复核已有回归；本轮补原因提示 | NR-06联调 |
| FR-13 | 本轮修复执行回包/预览乱序与模板；服务计划版本绑定仍缺 | FUP-08/02 |
| FR-14 | fetch+decode身份链局部通过；取消后的资源回收未闭环 | FUP-06 |
| FR-15 | 后台解码/fit/单图预算存在；生命周期/总预算未完成 | FUP-06 |
| FR-16 | 多job轮询通过；原生取消入口未交付 | FUP-05 |
| FR-17 | 部分abort/closed guard通过；任务保活/非阻塞退出未完成 | FUP-01/06 |
| FR-18 | 不再重复解尾帧；producer EOF与呈现/音频完成仍混用 | FUP-07 |
| FR-19 | 时基公式假会话回归通过；真实格式/音视频seek待验 | FUP-07 |
| FR-20 | 比例/SAR/偶数尺寸合成帧通过；旋转/格式完整矩阵待验 | FUP-07 |
| FR-21 | 尺寸快照已修；GPU任务队列尚未接入 | FUP-07 |

## 5. 执行优先级与证据

建议先 **FUP-01 → FUP-02/03**，确保素材和组织数据安全；FUP-04/05/06/07可按边界分别推进，FUP-08依赖FUP-01/02；随后按FUP-09逐阶段迁移与退役。每项交付后都更新实现、自动化、实机和无旧扩展四种证据，不按代码量估算统一百分比。

本轮测试命令、报告、隔离复现输出与范围边界见[验证记录](../validation/files_replacement_review_20261117/README.md)。所有高风险问题留在上述后续计划，本轮没有实施对应结构性改动。

## 6. 用户提供真实素材后的补充（2026-11-17）

随后用户提供照片和视频目录，完成只读CPU解码补测，结果见[真实媒体记录](../validation/files_eagle_layout_20261117/real_media_notes.md)。这补齐了部分真实媒体读取证据，不等于GPU、HDR显示、声卡、连续播放或旧扩展退役验收。

| 范围 | 结果 | 后续工作 |
| --- | --- | --- |
| 123张JPEG原图 + 14张PNG | 137张全部解码/比例通过；120张照片为竖幅 | 四种浏览布局使用真实画幅；图像解码继续在后台 |
| 34个常规MOV/MP4 | RGB/YUV抽帧、前后seek及比例通过，覆盖H.264/H.265/ProRes/DNxHR、2K/4K与8/10/12-bit | 保留为FUP-07真实读取子集；还需播放/设备验收 |
| 12个Topos MOV | 解码/seek通过，但RGB回退在320×240等非16:9盒中全部拉伸 | **FUP-07/FR-20范围扩展**：`ToposMovieSession._frame_rgb_locked`仍直接按box缩放；与Libav共用等比fit语义，补Topos RGB/YUV双路径真实回归。本轮UI调整不改播放核心 |
| 8K ProRes RAW HQ相机MOV | 元数据打开约0.55秒，首帧RGB超过35秒未返回；矩阵40秒deadline超时 | **FUP-07/FD-05**：RAW能力诊断、后台解码超时/取消、释放与失败提示；不得把常规ProRes通过推广为ProRes RAW可播 |
| BRAW/R3D各1个 | 当前能力表缺SDK，未计通过 | FD-05 provider/依赖诊断，避免静默等待或黑屏 |
| 原先缺素材的YUV回归 | 用本次视频目录在独立测试进程替换素材定位，9 passed / 0 skipped | 原3项skip已补证；该测试不执行GPU上传/绘制 |

被测源文件size/mtime前后未变；没有写原素材、原缓存或用户库。5个`.topos`文件为只含`project.json`的工程侧车，未当作视频解码；隐藏缓存204张webp未重复计为原图。
