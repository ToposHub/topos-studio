# Files 整体复查、问题整改与 file_explorer 完全替代计划

日期：2026-11-16。审查基线：`dev @ b08e60f0`（包含 `abd41b9a` 原生功能扩展、`9db4aaac` 播放核心迁移、`b08e60f0` GPU预览）。本计划记录**复查发现及下一步工作**，不是已修复清单。

## 历史实施记录（2026-11-16 整改批次，fix-ui 分支）

> **2026-11-17 复核更新**：下表保留为历史交付记录，不代表整批退出门槛已满足。本轮发现 NR-00～05 仍为部分完成（含 FR-01 P0 未关闭、真实改名入口未贯通、UI 分页未实现等），NR-06～10 未实施；已直接修复局部问题。当前状态、后续任务和验证证据以[新复核与后续计划](2026-11-17-files-replacement-followup-plan.md)为准。

以下§1～8保留最初审查内容与阶段设计，其中“本轮”指2026-11-16审查，不指2026-11-17复核。

按 §8 拆分的证据层级（实现存在 / 自动化链路通过 / 实机验收通过 / 无旧扩展通过）如实记录：

| 批次 | 范围 | 状态与证据 |
| --- | --- | --- |
| NR-00 | 复现转回归 | ✅ 自动化通过：`tests/files/test_file_safety_regressions.py`、`tests/ui/test_playback_semantics_regressions.py` 等 5 个回归套件，问题编号（FR-xx）与用例一一对应；全部 tmp/隔离 SQLite 夹具 |
| NR-01 | FR-01~05、FR-07 | ✅ 实现存在 + 自动化通过（24 用例：并发矩阵/TOCTOU 注入/链接保护/列映射注入/回收站关系/锁绕过/取消清理/有界池）。❗跨进程竞态与真实坏盘/磁盘满为 V 项待实机注入 |
| NR-02 | FR-08~12、FR-16 | ✅ 实现存在 + 自动化通过（17 用例：位置身份/防抖/分页无漏重/标签快照/能力门控/多任务轮询）。❗UI 虚拟化长列表（FR-10 的 10000 条矩阵）未做 |
| NR-03 | FR-06 | ⚠️ 以**补偿回滚**实现（宏失败→移除本次新导入素材+如实播报；复用素材不动）；单一 Undo 覆盖 assets+timeline 仍待 ImportMediaAssetCommand 入宏（后续）。自动化通过（3 用例） |
| NR-04 | FR-13~15、FR-17 | ✅ 实现存在 + 自动化通过（9 用例：会话隔离/模板快照/世代身份/像素预算/关闭守卫/abort 清理） |
| NR-05 | FR-18~21 | ✅ FR-18/19/20 实现存在 + 自动化通过（15 用例，合成帧/假会话）。⚠️ FR-21 部分：尺寸快照已修、误导注释已删；**GL 仍在 GUI 线程执行，未接渲染任务队列——代码内已如实标注偏差，需真 GPU 验证（V 项）** |
| NR-06~10 | FD-01~06 | ⛔ 未开始：服务抽取/数据独立/去跨域依赖/恢复演练/物理退役仍为待实施，**不得据此宣称可删除 external/file_explorer** |

回归基线：`tests/files/ + tests/ui/（files/播放相关）+ tests/edit/test_external_import_compensation.py` 全绿（218 passed, 3 skipped）。

**结论：现阶段完成了原生界面和部分播放迁移，尚未实现对 file_explorer 的独立替代。不能直接删除 `external/file_explorer/`。** 先解决文件安全与数据一致性问题，再把服务、数据、运行时迁入项目，最后以“旧扩展物理不存在”的测试作为退役门槛。

本文件作为旧《Files 原生 PyQt6 界面迁移与后端实施计划》的后续整改计划。旧计划中的“已实施/通过”只证明当时列出的代码和测试存在，不等于本计划的退役条件已满足。继续遵守用户之前的产品方向：现代、紧凑、实用，保留优点，不把所有旧工具照搬进新页面。

## 1. 本轮范围、方法和可信边界

| 项目 | 结果 / 边界 |
| --- | --- |
| 检查范围 | Files宿主、原生界面/transport/预览/比较/批改名/配置、Files DTO/投放协调、旧服务新增文件任务/路径迁移/回收站/搜索、共享播放器与GPU预览、跨域依赖与现有退役记录 |
| 自动化基线 | Files领域、全部原生Files UI测试、播放宿主/Topos音频/YUV、既有集成契约：**172 passed**；JUnit见验证目录 |
| 独立复现 | UI状态/晚到HTTP/批量标签/并发任务/路径迁移/回收站/软锁/分页/时间线回滚/播放语义，共5个隔离脚本；已实际执行并保存输出 |
| 数据保护 | 文件与SQLite实验只使用`/tmp`夹具；没有对真实素材或用户数据库进行写实验；未修改业务源码 |
| 缺失扩展实验 | 模拟`external/file_explorer/main.py`不存在，导入Files宿主即抛`RuntimeError: Cannot locate external/file_explorer from adapter.py`；未移动真实扩展目录 |
| 未完成实机项 | 全格式真实播放/HDR显示/音频设备/真实鼠标拖拽/长期压力/Windows与Linux原生设备/无源码发布包测试，均不因单元测试通过而标完成 |
| 当前提交保护 | 开始审查时dev已有3个本地提交；本轮保留其内容，只新增计划与审查证据 |

证据类型：**R=隔离复现已确认；C=调用链/源码已确认；V=待补实机验证。** 运行记录、复现脚本与基线报告位于[验证目录](../../docs/validation/files_replacement_audit_20261116/README.md)。R证明具体触发路径，不代表所有环境都会同样表现。

优先级：P0=可造成不可恢复数据丢失；P1=核心行为错误、数据/事务失真、项目架构硬约束违例或退役阻塞；P2=局部交互、一致性、资源或证据缺口。以下保留2026-11-16审查基线的问题描述；后续整改并非全部未做，当前完成边界以顶部链接的新复核计划为准。

## 2. 问题总表与整改验收

### 2.1 文件安全、数据与工程事务

| ID | 级别 / 证据 | 发现和后果 | 修复任务 | 必须补的验收 |
| --- | --- | --- | --- | --- |
| FR-01 | **P0 / R** | 每个文件任务独立线程；目标检查与实际移动没有原子预留。两份同名文件并发移动到同目录、策略`error`，两任务都成功、两源都消失，目标只剩其中一份 | 同路径读写协调、目标预留与“不覆盖”原子语义；不同卷也不能依赖先exists后move；`rename`策略同样处理竞态 | Barrier固定合法交错；同目标copy/move/rename并发，`error/rename/overwrite`分别验收，未授权覆盖必须为零 |
| FR-02 | P1 / R | `copy + overwrite`使用`shutil.copy2`，若目标已是符号链接，会跟随链接覆盖实际指向文件，超出用户看到的目标路径 | 明确符号链接/硬链接政策；校验实际目标和源身份、锁/权限；通过安全临时文件和原子替换完成目标写入，不能先检查再盲写 | 目标链接指向锁目录/其他目录/源自身，断链、硬链接、检查后换链；保护文件内容不变 |
| FR-03 | P1 / R+C | `_move_meta_rows`对`ocr_text/markers`使用不存在的`path`列，实际列为`file_path`；异常吞掉后继续commit，任务仍成功 | 修正schema映射；所有关系重映射纳入单事务；失败必须反馈，文件已移动时提供补偿/待修复状态；旧单改名、批改名、move均复用同入口 | OCR/标记/标签/集合/堆叠/FTS及子目录全关系断言；任一表失败注入，不得只完成部分DB修改 |
| FR-04 | P1 / R+C | 回收站只快照少数`file_meta`字段；删除后恢复遇同名时恢复为副本，技术字段/stack_id丢失，集合成员及堆叠封面仍指原位置，可能指向另一份素材 | 版本化恢复清单保存完整用户/组织关系；重建字段与不可重建字段明确区分；恢复副本时全部关系按最终身份重映射；保留未知字段 | 同名恢复、目录子树、重复删恢复、DB失败/跨卷；原文件与新占位文件不能串组织关系 |
| FR-05 | P1 / R | `/api/tasks/start`锁检查只看targets与destination；mkdir实际写`options.parent`，可通过无锁targets向锁目录建文件夹 | 在领域命令解析后的真实读写目标上统一做锁/权限校验；不能信任无关targets；校验类型及源/目标/恢复目录 | 无锁targets+锁parent必须拒绝且磁盘无变化；copy/move/rename/restore同策略检查 |
| FR-06 | P1 / R+C | 外部文件落轨先发布导入后的工程，再建立时间线和执行宏；宏失败后仍多出asset并发出project_changed，提示却说“已回滚”；Undo宏也不撤回已发布的导入 | 先暂存导入、轨道与片段，再由工程级事务统一提交；或把导入与落轨明确拆为两个诚实的操作，但不得宣称整批原子回滚 | 宏失败、时间线设置取消、无有效片段、AV失败、切工程/删轨、Undo/Redo；project/assets/bins/revision/dirty/preview一起断言 |
| FR-07 | P1 / C | 文件任务是无界daemon线程、仅文件之间检查取消，无统一stop/join；超过历史上限可丢弃仍运行任务的查询记录；单个大文件拷贝无取消/字节进度 | 有界任务池、可取消分块拷贝、临时文件与失败清理；保留活动任务；统一runtime关闭和任务恢复策略 | 大文件半途取消、关窗/退出/服务重启、200+任务、磁盘满、跨卷中断；无未知后台写入、无半成品伪成功 |

定位依据：

- FR-01/02/07：[file_tasks.py:120](../../external/file_explorer/file_tasks.py:120)、[目标冲突处理:224](../../external/file_explorer/file_tasks.py:224)、[复制/移动:262](../../external/file_explorer/file_tasks.py:262)、[历史淘汰:149](../../external/file_explorer/file_tasks.py:149)。
- FR-03：[路径传播:3712](../../external/file_explorer/routes.py:3712)，尤其3746–3767的列映射与吞异常；schema在`db.py`的ocr_text/markers表定义。
- FR-04：[回收站快照:103](../../external/file_explorer/trash.py:103)、[恢复:251](../../external/file_explorer/trash.py:251)。
- FR-05：[任务入口:3791](../../external/file_explorer/routes.py:3791)。
- FR-06：[落轨提交:16928](../../src/features/edit/ui/timeline_view.py:16928)，16979先发布，17035后执行宏，17039提示全回滚。

已排除的猜测：同源overwrite copy/rename会拒绝、原地move为no-op、move目录到自身后代会报错且源保留；这些隔离用例没有复现直接删源，不把它们列为已发现缺陷。

### 2.2 浏览上下文、查询与标记一致性

| ID | 级别 / 证据 | 发现和后果 | 修复任务 | 必须补的验收 |
| --- | --- | --- | --- | --- |
| FR-08 | P1 / R | 搜索/集合只替换model，没重建`_entry_rows/_entry_paths`、封面状态与当前位置；新素材封面被丢弃，旧路径lookup可返回新行，mkdir/paste仍落到上一个真实目录 | 所有位置经统一`Location + QueryResult`提交；更新选择/索引/封面/preview身份；虚拟位置不允许隐式目录写入；refresh按当前位置重放 | A目录→B搜索→集合→目录；每阶段核对封面/预览/复制/粘贴/新建/刷新/回退，虚拟视图绝不写A目录 |
| FR-09 | P1 / R+C | 搜索/集合响应没有共享generation、cancel不取消这些请求，旧搜索能覆盖已导航的新目录；每次输入立即查询 | 目录/搜索/集合统一request_id与generation，按位置取消与丢弃；输入防抖；清空/退出全库也失效旧结果 | 延迟交错、连续输入、清空、切目录/集合、关闭；模型/路径/位置身份始终一致 |
| FR-10 | P2 / R+C | 全库服务多词AND命中又被前端整句substring过滤；UI仅第一页200条；后端先截到2000再按offset分页，2001条命中时offset2000返回空但total仍2001 | 只保留一份查询语义；在全匹配集合上稳定排序分页，返回cursor/total；UI加载下一页/虚拟化；计数与过滤明确 | 名称sunset+标签aerial跨字段命中；201/2001/10000条；分页无漏/重、末页终止、过滤后总量正确 |
| FR-11 | P2 / R | 批量标签UI按替换/清空显示，服务却追加并忽略空值；API成功后UI显示与DB不同，刷新又“恢复”旧标签 | 显式add/remove/replace/clear语义；逐项返回最终UserMetadata，UI应用服务快照；旧API追加行为兼容迁移 | 混合/同值、删除最后标签、空值清空、部分失败、重开、XMP/结构化标签关系一致 |
| FR-12 | P2 / R | 能力清单禁用metadata后，选择/信息刷新又按“有选择”重新启用；提交入口不复核能力 | 启用条件合并能力、选择、离线/锁和提交状态；提交侧再次校验，缺能力的默认政策明确 | 禁用后切选择、封面回包、过滤、失败回填均不启用、不发写请求 |

定位依据：

- FR-08：[搜索结果提交:1610](../../src/app/ui/native_files_panel.py:1610)、[集合提交:1812](../../src/app/ui/native_files_panel.py:1812)、[lookup:1470](../../src/app/ui/native_files_panel.py:1470)、[操作目录:1522](../../src/app/ui/native_files_panel.py:1522)。
- FR-09/10：[搜索请求:862](../../src/app/ui/native_files_transport.py:862)、[取消:979](../../src/app/ui/native_files_transport.py:979)、[查询截断:4179](../../external/file_explorer/routes.py:4179)。
- FR-11：[标签提交:1251](../../src/app/ui/native_files_panel.py:1251)、[API追加:959](../../external/file_explorer/routes.py:959)。
- FR-12：[能力应用:1477](../../src/app/ui/native_files_panel.py:1477)、[回填重启用:1196](../../src/app/ui/native_files_panel.py:1196)。

### 2.3 对话框、预览与任务生命周期

| ID | 级别 / 证据 | 发现和后果 | 修复任务 | 必须补的验收 |
| --- | --- | --- | --- | --- |
| FR-13 | P1 / R+C | 批改名预览后改模板仍可执行新模板；响应无session ID，多个非模态窗口共享广播，会收到彼此结果 | 执行绑定已确认的files/template/plan/revision快照；编辑使预览失效；执行前再核冲突；会话隔离，完成即刷新受影响位置 | 未重预览不可执行；双窗口/乱序响应/执行中改输入/冲突变化/部分失败 |
| FR-14 | P2 / R | PreviewDecoder以path识别任务；同路径重开，旧任务先完成可删除新job并吞掉新结果 | fetch/decode都使用递增session/generation；按job身份回收；关闭、同名文件变化和重开统一失效 | 旧1px任务先到、新2px后到必须只显示新图；旧失败晚到、取消后同path重开 |
| FR-15 | P1 / C | 比较窗口GUI线程同步解码，无像素/总内存预算，最高4倍生成巨幅QPixmap；fit只看第一张宽且下限25%，大图不真正fit；多窗口共享队列、关闭不取消 | 后台解码/有界并发、单图与总像素预算、按viewport绘制缩放而非预生成巨图；独立session/取消；fit同时考虑所有图片与宽高 | 大尺寸高压缩图、4图、竖横混合、双窗口、关闭中下载/解码；主线程不被解码阻塞、内存收敛 |
| FR-16 | P2 / R+C | transport仅一个`_task_poll_job`，第二任务覆盖第一轮询，前任务无完成/失败反馈；取消任务无可达原生UI入口 | 按job_id跟踪全部活动任务或明确串行队列；任务列表/取消/失败详情可达，切位置不丢任务归属 | 两任务交错完成均显示，取消/部分失败/退出重进/超时恢复；任务状态不会相互覆盖 |
| FR-17 | P2 / C | 关闭清理不覆盖所有请求：metadata只清引用不abort，搜索/集合/扫描等无统一在途表；compare在设置closed前abort且完成回调无closed判断；decoder池shutdown不等运行任务结束 | runtime持有在途请求/任务/会话，先标关闭再取消，结果统一guard；可安全等待/延迟回收，不从GUI强杀线程 | 下载/解码/写入中销毁panel、切旧UI、关主窗；关闭后无UI回调/错误成功提示/运行线程析构崩溃 |

定位依据：

- FR-13：[批改名执行:70](../../src/app/ui/native_files_batch_rename.py:70)、[宿主接线:514](../../src/app/ui/files_panel.py:514)。
- FR-14：[PreviewDecoder:96](../../src/app/ui/native_files_preview.py:96)。
- FR-15：[比较解码:108](../../src/app/ui/native_files_compare.py:108)、[缩放:95](../../src/app/ui/native_files_compare.py:95)、[共享请求队列:738](../../src/app/ui/native_files_transport.py:738)。4张7K级RGBA图本体可达约784MB，放大4倍的像素面积增16倍；这是容量估算，**本轮没有故意分配巨量内存来压垮应用**。
- FR-16：[单任务轮询:478](../../src/app/ui/native_files_transport.py:478)。
- FR-17：[transport关闭:992](../../src/app/ui/native_files_transport.py:992)、[比较完成:762](../../src/app/ui/native_files_transport.py:762)、[解码取消/关闭:104](../../src/app/ui/native_files_preview.py:104)。这里是明确的清理不完整，不宣称已在实机复现崩溃；由失败注入测试进一步验证。

### 2.4 播放语义与GPU边界

| ID | 级别 / 证据 | 发现和后果 | 修复任务 | 必须补的验收 |
| --- | --- | --- | --- | --- |
| FR-18 | P1 / R | 最后一帧后仅当`target+1<frame_count`才推进索引，EOF分支永远到不了；2帧会话读成`0,1,1,1…`，EOF次数0 | 正确推进到结束哨兵并只处理一次EOF；区分播放完成、seek和暂停，避免提前丢最后展示帧 | 0/1/2/长视频，末帧、暂停seek片尾、从结束重播；不重复解码尾帧、不空转 |
| FR-19 | P1 / R | ToposMovieSession直接把MOV timescale当fps；timescale24000、240帧、每帧1000ticks应24fps/10秒，实际24000fps/0.01秒 | 复用ToposSource的精确时基/时长公式与Rational；按sample durations计算，避免第二套格式解释 | 24/25/30000÷1001、不同timescale、音视频seek和时长；与主项目source同值 |
| FR-20 | P1 / R | Libav将源帧直接缩成画布宽高，160×80的2:1视频在400×400窗口解码成1:1，之后画布无法恢复原几何 | 等比限制解码尺寸，保留源/可见尺寸、SAR和显示宽高比；letterbox由呈现层处理 | 竖屏/超宽/不同SAR/奇数尺寸/旋转，RGB与YUV一致；更改窗口不拉伸 |
| FR-21 | P1 / C | GLVideoCanvas在GUI线程initializeGL/paintGL创建并执行ModernGL；与.clinerules“GPU走既有渲染线程”冲突；解码线程还调用canvas.display_size读QWidget尺寸/DPR | 将转换/上传/渲染接既有GPU任务队列和资源释放机制；UI只接呈现结果。尺寸/DPR由GUI线程生成不可变快照再传worker；删除误称符合规则的注释 | 线程归属断言、渲染开销/播放干扰、context丢失/resize/关闭、100次开关资源计数；V：真GPU验证 |

定位依据：

- FR-18：[BasePlayer:646](../../src/shared/playback/base_player.py:646)、[尾帧索引:669](../../src/shared/playback/base_player.py:669)。
- FR-19：[Topos fps:87](../../src/shared/playback/topos_player.py:87)；可复用[ToposSource时基:1882](../../src/shared/media/topos_source.py:1882)。
- FR-20：[Libav缩放:436](../../src/shared/playback/libav_player.py:436)。
- FR-21：[GL创建:129](../../src/shared/playback/gl_video_canvas.py:129)、[GL呈现:150](../../src/shared/playback/gl_video_canvas.py:150)、[错误约束说明:19](../../src/shared/playback/gl_video_canvas.py:19)、[worker读尺寸:579](../../src/shared/playback/libav_player.py:579)。Qt容许某些QOpenGLWidget用法，不等于项目授权绕过本项目GPU线程约束。

## 3. 完全替代的现存阻塞

这些是独立于上表缺陷的迁移工作，不能以“保留旧工具入口”判为已完成。

| ID | 阻塞 / 证据 | 必须交付 |
| --- | --- | --- |
| FD-01 | 宿主硬依赖旧扩展：`files_panel.py`顶层导入LibraryAdapter，adapter导入时查找旧main.py，不存在直接RuntimeError；原生模式仍创建QWebEngineView、启动旧Flask并加载隐藏页面 | 原生Files独立宿主；默认不导入WebChannel/WebEngine/旧adapter；无扩展目录也能导入和启动；旧模式只能作为隔离迁移工具临时存在 |
| FD-02 | `src/features/files/`当前只有contracts/import_job，DTO不是业务facade；目录/树/搜索/metadata/任务/库/缩略图仍全部HTTP调旧routes | 抽出库repository、查询/命令/任务service与runtime；UI调用类型化异步facade；迁移期HTTP只作同service的薄适配器，不能复制两套业务 |
| FD-03 | 数据位置和生命周期未独立：旧db/config锚定扩展/仓库位置，shared Database默认`topos.db`依赖CWD；单纯搬目录可能分叉到另一个库 | 用户级数据目录/单一库定位器、版本化schema、只读旧库发现、全量迁移/幂等/回滚；Files与Library统一repository入口 |
| FD-04 | 跨域残余运行依赖：optional_dependencies从旧utils找工具，媒体池`.toos`缩略图从旧topos_image插件导入；main有旧sys.path注入、启动/退出/benchmark/preference插件配置仍引用旧路径 | 工具定位复用`src/shared/ffmpeg_locator.py`等已有能力；`.toos`provider迁入shared codec/media并登记；宿主组装、退出、preferences与benchmark逐条拆旧引用 |
| FD-05 | 原生恢复闭环缺失：已有Delete，回收站列表/恢复/冲突说明无原生入口；独立音频仍经旧分派，文本/专业图片解码并未全部原生覆盖；解锁提示要求去旧Files | 原生回收站/恢复、可达任务取消、音频预览、必要解锁/格式诊断；必要专业格式走现有provider而非只依赖Qt原始字节解码 |
| FD-06 | BX01只有备份端点，尚无完整恢复演练；DB快照不包含独立config、XMP、回收站实体与插件资源。发布spec也未提供“无旧扩展”验收 | 一致备份清单/恢复工具/校验和、实体引用检查；无扩展目录源码启动+冻结包运行、断网/可选依赖缺失矩阵；确认其他功能的WebEngine使用不受影响 |

关键证据：[adapter:18](../../src/app/integrations/file_explorer/adapter.py:18)、[Files启动:147](../../src/app/ui/files_panel.py:147)、[共享工具反向依赖:80](../../src/shared/diagnostics/optional_dependencies.py:80)、[.toos反向依赖:834](../../src/shared/media/thumbnail_provider.py:834)、[main_window注入:79](../../src/app/main_window.py:79)、[shared库默认CWD:40](../../src/shared/db/database.py:40)、[备份端点:3843](../../external/file_explorer/routes.py:3843)。

“完全替代”不要求全项目卸掉QtWebEngine或Flask：教学动画HTML预览、其他独立产品可能合法使用。门槛是**Files及其跨域消费者不再依赖旧file_explorer的代码、端点、数据路径或插件注册**，不能为清理Files而删掉其他页面需要的依赖。

## 4. 功能取舍与旧插件退役矩阵

所有条目都要选择“原生保留 / 独立工具接管 / 明确停用并保留数据”；不能用“旧版里还有”作为最终交付。以下为推荐执行范围，未获新的具体需求不扩大UI。

| 功能域 | 最终建议 | 替代交付 / 数据处理 |
| --- | --- | --- |
| Home/素材目录、网格/列表、搜索、过滤、信息、基本标记 | 原生保留 | 统一Location/facade，稳定身份、真实分页、封面优先级、现代紧凑布局；保留黑色1px分隔线/折叠搜索 |
| 图片/视频/音频预览、多选拖入媒体池/时间线 | 原生保留 | 修复FR-06/14/18–21；播放路由/时基/音频互斥统一，禁止回退旧WebView |
| 复制/粘贴/移动/改名/回收站恢复 | 原生保留 | 安全任务service、取消、逐项结果、进度、恢复冲突及工程引用处理；先完成FR-01–07 |
| 固定目录/最近访问、集合基本组织 | 原生保留实用部分 | 集合创建/改名/增删成员用轻量原生入口；引用操作明确区别物理移动；复杂规则暂不进入首版 |
| 智能文件夹/层级标签/堆叠 | 按需，先保留可读取数据 | 迁移原schema和关系，至少不丢数据；有频繁使用需求再补编辑UI，未支持能力明确说明 |
| XMP/元数据JSON/备份/失联重链 | 保留为库维护能力 | 可放原生“素材库维护”窗口或独立命令；能实际恢复和找回，不要求占主界面 |
| RAW/EXR/HDR/PSD/PDF及`.toos/.topos` | 按格式能力矩阵保留 | 共享decoder/provider负责，缺可选依赖明确诊断；原文件64MB上限与显示预览能力分开处理 |
| 模板批改名/图像比较/视频hover | 已有则修好，非卸载核心阻塞外的扩张目标 | FR-13/15整改；真实媒体性能达标后启用，不能以旧页面兜底 |
| GPS地图/OCR/相似重复/幻灯片BGM/水印导出 | 本期不扩原生UI | 若明确弃用，保留旧数据导出和用户可读说明；若保留，需独立service/工具，不保留整个旧宿主 |
| 定时关机/跑分/Monaco/Darkroom/格式工厂 | 从Files核心分离 | 已有主应用工具优先接管；没有替代且要保留的工具独立注册，不能靠sys.path导入旧plugins |

| 旧插件ID | 去向 / 退出条件 |
| --- | --- |
| `video_pro` | 共享播放核心已迁移；修复本计划播放问题、去旧JS导航/Toast兼容类依赖，核心通过后退出旧插件 |
| `audio_pro` | 新增原生独立音频预览；波形/截取只按实际需求选择，先确保播放/停止/切源不走旧端点 |
| `topos_image` | `.toos`元数据/缩略图provider迁shared并更新媒体池调用；删除扩展后测试仍过 |
| `ai_library_browser` | 接现有shared vault client/主应用AI素材入口，保持可选能力和离线说明 |
| `format_factory`、`image_factory` | 需要时接独立Qt工具action registry，任务归runtime管理；不要再启动旧Flask作为launcher |
| `decode_benchmark` | 迁诊断/开发工具入口，保留既有Edit service，生产Files不依赖它 |
| `code_editor`、`image_editor` | 原生Files不重写整套编辑器；主应用/独立工具接管或明确停用，保留文件与配置导出 |
| `shutdown_timer` | 不进入Files替代必需项，停止旧集成前说明停用去向 |

## 5. 目标结构与UI/后端分工

建议结构是职责目标，实施前优先复用现有repository、thumbnail provider、SourceManager、共享播放和工具定位器，不机械新增同名第二套。

```text
NativeFilesPanel / Preview / Compare / Dialogs
              │ UI意图 + 不可变请求上下文
              ▼
Files facade（异步query/command/capability/job事件）
    ├─ 库repository / schema迁移 / 元数据关系
    ├─ 目录与索引查询 / watcher / 分页
    ├─ 文件事务与可恢复任务 / 回收站
    ├─ 共享thumbnail / codec / 工具能力
    └─ Runtime（取消、任务归属、资源与退出）

工程导入/落轨 → 既有EditProject service + command/transaction
视频呈现     → 既有GPU渲染任务队列，UI仅呈现与输入
旧HTTP routes → 迁移期调用相同facade；通过退役门槛后移除
```

分工继续沿用用户此前约定：

| 责任方 | 交付 |
| --- | --- |
| Codex：UI/交互 | 浏览Location状态、查询反馈/分页、能力状态、预览和比较会话、批改名确认、任务/回收站/解锁入口、紧凑布局、焦点快捷键、i18n、接真实facade及验证截图 |
| 用户：后端/核心 | 安全文件事务、数据/路径迁移、库repository/facade、索引/查询、任务runtime、工程级导入事务、codec时基与GPU线程/播放、恢复与分发集成 |
| 联调 | 真实接口样例/错误/取消/版本定义、端到端回归、缺扩展启动、发布包验证与退役验收 |

### 5.1 先冻结的最小契约

1. **Location**：directory/search/collection/trash/drive，稳定key、显示名、真实路径或query、writable/locked/online；虚拟位置永不假装上一个目录。
2. **Query**：request_id/generation/location/filter/sort/cursor；返回entries/total/next_cursor/truncated/revision；退出即失效旧结果，不重建查询语义。
3. **Mutation**：明确目标及实际源/目的路径、预期revision、patch操作；标签区分replace/add/remove/clear；最终快照和逐项结果必须由服务返回。
4. **Job**：job_id/context、进度/取消/失败/可恢复状态；UI和runtime能跟踪多个作业；shutdown有明确等待/恢复规则。
5. **Preview**：session_id+source_version，不能仅用path；fetch/decode/render所有结果都验证身份，资源预算独立于压缩文件大小。
6. **Import**：工程/序列/轨道/revision/帧率/落点快照；一次提交的原子性和Undo边界可测试且提示诚实。
7. **Library state**：用户数据目录、数据库schema版本、库ID/素材ID与路径映射；迁移与备份包含未知字段和组织关系。

## 6. 下一步执行表

下表保留最初的阶段设计与退出门槛，当前状态见顶部复核更新。阶段完成必须附新测试和真实证据；不要把“旧代码挪位置”“新增DTO”“某组单测绿”单独当退役完成。

| 阶段 | 任务 | 对应问题 | 依赖 | 负责人 | 退出门槛 |
| --- | --- | --- | --- | --- | --- |
| NR-00 | 冻结基线、备份、复现转回归 | 全部 | 无 | 联调 | 复现脚本与问题编号可追踪；真实库备份可校验；不写真实素材做破坏实验 |
| NR-01 | 文件安全与数据补偿 | FR-01–05、FR-07 | NR-00 | 用户后端 | P0竞态/链接覆盖/锁绕过全部失败注入通过；完整回收站恢复；不得吞迁移错误 |
| NR-02 | 统一Location/查询/标记/任务状态 | FR-08–12、FR-16 | NR-00，可与NR-01独立推进 | Codex UI + 用户查询/任务 | 目录/搜索/集合全过程一致；多词/分页/多任务/标签写读一致 |
| NR-03 | 工程级导入与落轨事务 | FR-06 | NR-00，契约先冻结 | 用户核心 + Codex投放UI | 取消/失败/Undo/Redo对assets+timeline全域一致；媒体池和预览同步 |
| NR-04 | 预览/比较/批改名会话安全 | FR-13–15、FR-17 | NR-02身份契约 | Codex UI + 用户runtime | 同路径重开不串台；多个窗口独立；后台解码/预算/取消通过 |
| NR-05 | 播放正确性与GPU归属 | FR-18–21 | NR-00 | 用户播放/GPU + Codex呈现接线 | 时基/EOF/比例修复，GPU线程约束通过，真实媒体和设备矩阵补齐 |
| NR-06 | 抽共享数据与服务，替换UI transport | FD-02/03 | NR-01–04核心契约 | 用户后端 + Codex facade adapter | HTTP与直连适配调用同service；数据无双库；UI无SQL/扫描业务 |
| NR-07 | 补原生恢复、音频、格式与必要维护入口 | FD-05、功能矩阵 | NR-04–06 | Codex UI + 用户provider | 删除可恢复、任务可取消、音频可播、必要格式不靠旧WebView、维护工具可达 |
| NR-08 | 去跨域反向依赖与旧宿主启动 | FD-01/04，插件矩阵 | NR-05–07 | 用户集成 + Codex宿主 | Files不创建旧browser、不启动旧server；.toos/诊断/Library/偏好无扩展依赖 |
| NR-09 | 全库迁移/恢复演练、缺目录与打包矩阵 | FD-03/06 | NR-06/08 | 用户数据/分发 + 联调 | 物理无扩展目录的干净进程/安装包通过，备份可实际恢复，用户数据不丢 |
| NR-10 | 移除旧扩展与死代码，最终回归 | 全部 | NR-09全过 | 联调 | 下节退役清单全勾；仓库/安装包/运行时无file_explorer必需引用 |

执行优先级：**NR-00 → NR-01；NR-02/03/05可按独立模块推进；随后NR-04/06/07 → NR-08/09 → NR-10。** 不先做“删目录”，也不继续堆低频图库工具来掩盖替代链路尚未独立。

## 7. 最终退役验收清单

- [ ] FR-01 P0消除，所有其他P1修复并有失败路径测试；P2逐项完成或有明确暂缓范围/影响说明。
- [ ] 可在隔离checkout中物理移除`external/file_explorer/`后，启动Studio并打开Files、Library、媒体池、时间线及诊断/偏好，不抛导入错误。
- [ ] 除历史文档/数据迁移识别/明确独立工具兼容外，运行代码无`file_explorer`、裸`db/const/utils/plugins`导入或旧`sys.path`注入。
- [ ] Files正常使用不启动Flask/WSGI/旧WebChannel或隐藏WebEngine页面；其他合法HTML消费者独立保留。
- [ ] 查找→预览→多选→拖入Bin/时间线→Undo/Redo→保存重开真实流程通过；空工程、锁轨、切工程和坏文件给真实反馈。
- [ ] 文件copy/move/rename/trash/restore的并发、符号链接、跨卷、权限、磁盘满、取消与崩溃恢复无数据丢失。
- [ ] 真正的目录/search/collection/trash位置、分页、计数、选择、封面和操作目标一致。
- [ ] 旧库/配置/XMP/回收站实体/集合/标签/堆叠/未知字段迁移可幂等重试；恢复演练确认文件与关系均回来。
- [ ] `.toos/.topos`、常见图片/视频/音频及必要专业格式在新provider下可用；缺codec/ffmpeg时明确降级，无旧扩展兜底。
- [ ] GPU上传/转换/释放只在项目允许的渲染线程；EOF/时基/SAR/色彩语义/设备seek/退出经真实媒体验证。
- [ ] 至少100次打开/切源/关闭，任务中退出/重启，资源与线程不持续增长；慢网络不再是本地Files数据访问的必需条件。
- [ ] 100/1000/10000条真实索引+冷/热缩略图+播放同时运行的基准达标；原合成条目填充耗时仅作为UI基线。
- [ ] Windows/macOS及目标Linux、不同DPI/浮动/紧凑宽度测试；NTFS/大小写敏感卷/Unicode/UNC用实际平台补证。
- [ ] 干净安装/升级后的冻结包运行不需要源码目录、旧端口文件或仓库可写；依赖锁/许可说明和插件清单更新。
- [ ] 旧计划/退役审计/自动化报告统一标明最后一次验证基线，不能继续引用迁移前的“全部通过”作为最终结论。

## 8. 验证记录应怎样修正

原P0验收表A05–A08的“通过”多数是AST/领域/夹具测试，不等于实际鼠标投放、宏失败后的全工程回滚、原生音频播放或真实GPU/设备生命周期。当前复现证明这些条目存在覆盖盲区；后续报告应拆分：**实现存在 / 自动化链路通过 / 实机验收通过 / 无旧扩展通过**。

原性能记录明确不含真实缩略图、磁盘IO，平台记录主要是同机器不同QT_SCALE_FACTOR；保留其价值，但不据此宣称全平台和大库运行已验证。备份端点“有数据库快照”也不等于“完整素材库可恢复”。

每个NR批次交付：对应FR/FD编号、变更入口、focused回归及失败注入、真实service+UI接线证据、未覆盖项、资源/数据兼容影响。完成一批再更新状态，最后才能把旧扩展从默认运行和分发中移除。
