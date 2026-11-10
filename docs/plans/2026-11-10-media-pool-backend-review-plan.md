# 媒体池后端完成情况复核与整改计划

日期：2026-11-10。项目：topos-studio。基线：`eb029dde` 及检查开始时的工作区。

对照原交接表 `docs/media_pool_backend_parity_plan_2026-11-09.md`，检查新增服务、实际 UI/controller 调用链、命令/撤销、序列化、取消和工程切换。原有后端专项测试 **121 项通过**，但服务单测通过还不能代表生产入口已接通。本轮直接修复局部问题，保留跨模块、数据兼容及输出事务问题，供下一轮集中整改。

## 结果与优先顺序

- **直接修复 9 类局部问题**，另修正 1 处随机 ID 导致的测试断言不稳定；见下表。
- **15 项较大整改项**：4 项 P0、11 项 P1，全部仍未完成；本轮没有代为实施结构调整。
- Cloud / Power Bins 的协议设计已完成；真实跨工程/云服务仍明确不可用，作为单独延期项记录，不按新增回归缺陷计算。
- 优先处理 R01–R04，随后统一 R05/R06/R15 的执行身份与取消链，再处理范围兼容、引用索引和功能接线。

## 已直接修复

| 项目 | 修复与边界 | 回归依据 |
|---|---|---|
| 仅收集已用素材的空间估算 | 删除错误的 `del sequences`，保留调用方提供的时间线迭代器；原先会触发 `UnboundLocalError` | used/unused 两个不同大小文件，验证只统计已用文件 |
| 整理进度 | 使用 `ConsolidateProgress.completed_segments/total_segments`；原先读取不存在的字段，进度恒为 0 | 验证 1/4 与 4/4 原样转发 |
| CSV 无效表头 | 统一 warnings 为 tuple 后再追加提示；原先 list + tuple 抛 `TypeError` | 无可导入列、无匹配列均返回带警告的空计划 |
| 长素材多机位漂移采样 | 优先读取标准 `duration_seconds`，兼容旧 `duration` | 600/720 秒标准 metadata 实际进入首/中/末三个采样区间 |
| 单独设置 In 或 Out | `get_insert_range` 分别补齐缺失端点，避免只设置一个端点时整个范围被重置 | In=10 → 10..99；Out=49 → 0..49 |
| 素材标记校验 | 不再将布尔值/小数/字符串强制转成整数帧；颜色必须为六位十六进制 | 拒绝 True、1.8、"3" 和非法颜色 |
| Smart Bin 规则 | 关键词 `in` 按集合中任一关键词匹配且忽略大小写；数值/时间戳拒绝非数值、布尔值、NaN、Infinity 和倒序区间 | 关键词正反例、数值非法输入与正常区间 |
| 转录协议边界校验 | segment 时间戳与范围 fps 拒绝非有限值/布尔值；非 dict segment 不再静默丢弃 | 非法时间戳、fps、混合损坏载荷均明确报错；未更改端点兼容语义 |
| AI pending 取消计数 | 取消素材不再同时被 errors 和独立 canceled 计数两次 | 两素材仅取消第一个时，请求仍等待第二个结束，完成回调只执行一次 |
| 测试稳定性（非运行时缺陷） | 同路径组本身按 ID 排序，断言同步使用排序后的随机 ID | `test_backend_p1_usage_index.py` |

代码集中在 `media_management_worker.py`、`metadata_interchange.py`、`asset_source_range.py`、`smart_bin_rules.py`、`analysis_service.py`、`transcription_job.py` 和 `edit_controller.py`。新增回归：`tests/edit/test_media_pool_backend_review_20261110.py`（10 个用例）。没有更改工程 schema、原始媒体或全局 UI 样式。

## 较大问题整改表

行号以本次检查时源码为准；可通过函数名定位。所有复现都使用内存工程、临时合成文件或 mock provider/process，不改用户素材。

| 完成 | 编号 / 优先级 | 问题、触发方式与依据 | 建议修复范围 | 必须通过的验收 |
|---|---|---|---|---|
| [ ] | **R01 / P0** | **归档工程保存遗漏可编辑时间线。** `media_pool_panel.py:6127,6245` 只把 project 传给 `EditProjectIO.save_project`，没有传 `timeline_sequences`；整理路径还丢弃 `consolidate_project` 返回的 `_new_seqs`。实际保存返回 True，重开有 timeline refs，但 sequences 为空。 | 收集时保存冻结快照中的所有序列；整理时保存重映射后的新序列；复用完整工程保存入口，明确携带 comp/其他模块 payload，验证所有引用闭包。不要仅补一个成功提示。 | 至少两条时间线、嵌套序列、comp、音视频、handles；隔离原媒体/缓存后重开归档，轨道/片段/时长/源范围完整，整理后的片段只引用新来源；保存失败不得报告完成。 |
| [ ] | **R02 / P0** | **收集可覆盖目标目录已有文件。** `project_archive.py` 的 `_unique_dest_name` 仅防本轮名称重复，`collect_project` 使用 copy2/copytree 写目标；已有 `media/source.mov` 被直接覆盖。LUT 和 dependencies manifest 也需纳入同一所有权策略。 | 目标目录预检、输出 manifest、唯一 staging、占用/冲突策略、成功后事务发布；明确拒绝覆盖或要求可审阅的冲突决策；失败只删除本任务创建的产物。验证源/目标相同、路径别名和目录嵌套。 | 已有同名文件、已有 LUT/manifest、同名不同源、失败/磁盘满/取消、重试；所有既有文件及源文件字节保持不变，失败不留下伪完整归档。 |
| [ ] | **R03 / P0** | **真实代理入口仍绕过新服务，输出可能误删。** `media_pool_panel.py:11309` 直接创建旧 `ProxyGeneratorWorker`；新 DiskProxyService 的预检/队列/重试没有覆盖此路径。`proxy_generator.py:418` 用 `-y` 直写固定目标，`:439` 失败删除目标，PyAV `:549` 同样直接写。已复现失败后既有 `shot_proxy.mp4` 消失。 | 菜单接到统一代理服务；按项目/素材/源身份管理队列、并发、取消、重试；私有 staging 与原子发布，产物所有权与 unlink 规则分离；真取消编码进程/worker。 | 失败保全既有同名文件；相同 basename/不同源不串用；空间不足提前失败；切工程/删素材/换源终止任务且旧回调零写入；队列取消/重试及代理类型准确。 |
| [ ] | **R04 / P0** | **Media Management 快照、任务身份和输出恢复不完整。** `media_pool_panel.py:5978–6250` 的 queued work 运行时读取 `self._project`，却使用发起时采集的 sequences；复现为新工程与旧时间线混用。回调没有完整 request/project generation 守卫。空间预检按源文件大小，目录只计 inode，转码并非源大小上界。取消可能已经产生文件但只返回 cancelled。 | 发起时捕获不可变 project+sequences+模块状态；任务带身份、源签名和目标所有权；关闭/切工程取消；完成与取消 exactly-once；按复制/序列/LUT/sidecar/转码策略估算并持续检查空间，记录可恢复 manifest。 | 排队期间切工程、关闭、换源；取消前/复制中/编码中/保存中；目录序列和大于源文件的转码；无跨工程内容混合、无晚到成功提示、无误删，残留输出可说明/清理/恢复。 |
| [ ] | **R05 / P1** | **Relink 的工程身份、命令和重探测缺口。** `media_pool_panel.py:_on_relink_asset/_on_relink_batch` 在模态对话框返回后直接改当前 project；`project.py:relink_asset` 主要改 path/offline。已复现对话框期间切到含相同 asset_id 的另一工程后，新路径写入另一工程。旧 technical metadata 与来源身份未统一重建。Replace 已有较完整守卫，不应另造一套。 | 复用 Replace 的身份/探测/兼容服务；模态前后检查 project generation 与 asset source revision；单/批量重连通过命令事务，统一使代理、缩略图、解码和使用引用失效。 | 单步 Undo/Redo；同 ID 跨工程、素材删除/换源、错文件/时长不足/缺帧、批量歧义；失败无部分提交；成功后的预览/时间线/导出/重开均一致。 |
| [ ] | **R06 / P1** | **AI running 取消没有阻止结果发布。** `analysis_service.py:_execute_via_scheduler` 外层 job 启动内层 request；provider 执行后 ingest 缺少外层取消和 source/project 身份守卫。已复现 outer=CANCELLED、summary=SUCCEEDED，且结果已入库。`media_analysis_controller.py` 切工程清映射不足以阻止旧完成回调。 | request/asset/provider 共用取消令牌和执行身份；发布前检查 project generation、asset、source revision/fingerprint；取消后禁止 ingest/ready；区分成功、失败、取消和过期，保持 summary 与单项一致。 | pending/running、多素材混合完成、切工程、删除、换源、清除后重试；旧任务不恢复已清结果，不显示 ready，不写入新工程；provider 不支持中断时也拒绝发布。 |
| [ ] | **R07 / P1** | **子片段 `source_end` 同名字段有两种端点语义。** `scene_cut/models.py:1135` 返回 start+duration（不含尾）；`scene_cut/subclips.py:168` 写入 source_end；`asset_source_range.py` 按含尾端点解释。start=20、duration=10 的场景子片段被读成 20..30，共 11 帧；手工子片段 20..29 为 10 帧。单帧子片段/转录范围也存在禁止 start=end 与“含端点”说明不一致。 | 先定义规范范围域，添加版本/来源迁移；统一 scene cut、手工子片段、源 In/Out、markers、转录范围与 timeline duration 换算。不能全局盲目减 1，破坏已有含尾工程。 | 两类旧工程、单帧、首/末帧、非整数 fps、全长、截断、handles；创建→插入→保存重开→导出帧数完全一致，父素材时长不被误改。 |
| [ ] | **R08 / P1** | **素材范围/标记命令未贯通源监视器和时间线入口。** `source_viewer.py:2027,2402–2440` 主要维护本地 monitor 范围；`get_insert_range` 没有生产调用方，`timeline_view.py:_add_asset_as_clip` 等入口仍有从 0/全长插入路径。core 命令往返测试无法证明真实剪辑动作使用该范围。 | 在 R07 确定语义后，源监视器编辑通过现有命令提交；监视器/媒体池/拖拽/Insert/Overwrite/追加等入口共用范围服务；明确素材级与 monitor 临时范围的优先级。 | UI 设置 In/Out/marker→所有插入入口→Undo/Redo→切素材→重开；子片段海报、源音频与 linked A/V 同步；仅刷新/选择不写工程。 |
| [ ] | **R09 / P1** | **Interpret Media 的消费者仍不一致。** `interpretation_matrix.py` 将 rotation/SAR/field_order 的 preview/export 标为 declared_unapplied；`source_viewer.py:_build_time_domain` 与 `timeline_view.py:_timeline_fps_for_first_asset` 仍读取原始帧率，其他插入分支已用 effective fps。 | 补齐解释属性在源监视器、初始时间线、插入/覆盖、代理和导出的统一消费；暂不支持的维度通过能力声明清晰限制，不把持久化成功当渲染支持。 | 每项属性 Undo/Redo，23.976/29.97、90°、非方像素、场序、声道映射；源/时间线/代理/导出结果一致，原始探测 metadata 保留。 |
| [ ] | **R10 / P1** | **十万素材查询没有完成生产分页与过期保护。** `media_pool_panel.py:8142` 默认请求全量；`:7621,8059,8184` 仍在 UI 构建/排序全量项，Smart Bin 路径绕过异步查询。切 Bin/过滤指纹时保留旧 `_query_asset_ids`，新标题下可短暂出现旧结果；结果接受没有完整的当前 revision 校验。 | UI 实际使用分页/窗口化模型；普通/Smart Bin 共用可取消查询；结果携带 request、project generation、revision、bin/filter fingerprint；加载期间旧项不能作为新范围操作目标；选择按 ID 恢复。 | 100k 素材性能实测及 UI 帧延迟记录；快速搜索/切 Bin/修改项目/切工程/删除期间只接受最新匹配结果；排序分页稳定，选择、Delete、AI 当前箱范围准确。 |
| [ ] | **R11 / P1** | **Usage 扩展没有统一到删除/定位/依赖闭包。** `asset_usage_index.py` 新增 `usage_check_for_assets`、`same_path_asset_groups` 没有生产调用方；Composite 依赖仍明确排除；媒体池删除/Replace 等路径没有共同消费扩展索引。 | 统一 timeline、嵌套、comp、同路径身份与 source/derived dependency 的索引契约；Usage 展示、跳转、删除确认、Replace 和归档共用，避免多份近似统计。 | 嵌套循环防护、同路径父/子片段、Comp 引用、代理与真实来源；统计和点击定位一致，删除检查不漏引用，归档只排除真正未用内容。 |
| [ ] | **R12 / P1** | **专业容器的探测结果未修正生产导入类型。** probe 可识别纯音频 MOV，`MediaImportWorker` 及同步导入仍先按扩展名定 video，未在 probe 后统一为 audio；源监视器可能尝试视频解码。独立声道探测与核心测试通过不等于 UI 消费正确。 | 由统一 probe/capability 决定媒体类型与可用流；异步/同步导入共用契约；按流身份定义分离声道/多流选择与持久化，继续保留图像序列分组和缺帧诊断。 | 纯音频 MOV/MXF、视频无音频、多音轨、分离声道、图像序列缺帧；UI 图标/源播放/时间线轨道/导出一致，无同步重探测阻塞。 |
| [ ] | **R13 / P1** | **CSV 匹配与撤销不安全且没有完整生产入口。** `metadata_interchange.py:247–253` 同路径多素材只取第一个且无歧义警告；计划不绑定 project/revision；`ImportMetadataCommand` 撤销恢复计划生成时旧值，可丢失计划生成后发生的编辑。keywords 用分号拼接/拆分，`night;exterior` 往返变两个关键词。plan/export/command 未从 UI 调用。 | 稳定身份优先，路径多匹配必须预览/冲突处理；计划带工程与来源版本，提交时重验或重建 diff；命令保存真实提交前状态；制定可逆且兼容旧 CSV 的关键词编码；接差异预览和明确空值策略。 | 同路径父/子片段、跨工程计划、预览后并发编辑、Unicode/分隔符/换行/空值、失败原子性、Undo/Redo、导出再导入字段等价；无静默选错素材。 |
| [ ] | **R14 / P1** | **Smart Bin 规则编辑器仍未消费新能力。** `media_pool_panel.py:6308–6364` 仍是名称/关键词/最低评分的旧表单；能力清单和 `UpdateSmartBinRulesCommand` 没有生产接线。引擎新增嵌套规则与此次关键词修复不代表完整编辑体验已实现。 | 按 capability 渲染分组/操作符/字段控件，使用已有更新命令；保留未知字段与 schema，校验枚举与字段域；接 R10 的统一查询。 | 嵌套 AND/OR、日期区间、关键词、颜色/旗标、技术属性；创建/编辑/撤销/删除/重开等价，不支持字段诚实显示，不错误匹配无该属性的媒体。 |
| [ ] | **R15 / P1** | **转录独立 job 协议尚无生产消费者。** 全 src 对 `TranscriptionJobRequest/Result` 的使用仍局限于协议定义；现有媒体池 recipe/clip worker 不消费 language、range、speaker、revision 等统一契约。值对象往返测试只证明协议可构造。 | UI 意图→controller/scheduler→provider options→结果存储/字幕消费全链路接到同一协议，复用 R06 身份/取消策略和 R07 范围域；不要并存第三套推理服务。 | 真实 UI 请求携带语言/范围/隐私/speaker；未授权不推理；范围裁剪与时间戳精确；取消/重试/切工程/换源拒绝旧结果；字幕及证据保存重开一致。 |

## 延期范围（不计入上面 15 项缺陷）

| 完成 | 编号 | 当前结论 | 后续要求 |
|---|---|---|---|
| [ ] | D01 / P2：Cloud / Power Bins | `docs/architecture/media-cloud-sharing-protocol.md` 已提供设计；`capability_registry.py` 明确 UNAVAILABLE，没有假成功入口。原计划的“协议设计”可认定完成，真实共享服务尚未实现。 | 需要时另开实现任务，覆盖身份/权限、版本冲突、撤权、离线缓存、配额、跨工程引用和数据迁移；服务可用前保持不可用声明。 |

## 原计划 17 项逐项对应

“本轮未发现阻断”只代表本次检查范围，不代表所有格式/平台/实际 provider 已认证。

| 原编号 | 能力 | 本轮结论 / 后续行 |
|---|---|---|
| P0-1 | Clip 元数据与来源时间码 | probe/规范化及专项测试通过；本轮未发现新的阻断。消费一致性另见 R09/R12。 |
| P0-2 | 素材 In/Out、标记、子片段 | 局部校验已修；schema 与生产消费未闭环，R07/R08。 |
| P0-3 | Clip Attributes / Interpret | 命令、覆盖值、能力矩阵存在；渲染和时间域消费未齐，R09。 |
| P0-4 | 大数据查询 | 异步查询服务存在；生产分页/过期结果保护不完整，R10。 |
| P0-5 | 素材箱语义与事务 | 普通移动、Alt 引用拷贝、Master 未归类及命令测试通过，本轮未发现新的阻断。 |
| P0-6 | Relink / Replace | Replace 守卫较完整；Relink 仍有跨工程与源核验问题，R05。 |
| P0-7 | 代理/优化媒体 | 新服务存在但真实菜单未统一；输出所有权有数据丢失风险，R03。 |
| P0-8 | AI 生命周期 | pending 计数已修；running 与晚到结果仍需整改，R06。 |
| P1-1 | Smart Bins | 引擎存在；完整生产编辑/查询未齐，R10/R14。 |
| P1-2 | 自动同步/多机位 | 长素材采样键已修；生产同步、确认与命令已有实现，专项测试通过。 |
| P1-3 | 转录/说话人 | 协议值对象与校验存在，缺全链路消费者，R15。 |
| P1-4 | 标记/旗标/颜色语义 | 当前 schema 的能力声明及不支持值 fail-loud 测试通过；不等同已实现 Resolve 所有多旗标能力。素材标记生产接线见 R08。 |
| P1-5 | Usage/定位 | 扩展索引存在，引用闭包与生产共用未齐，R11。 |
| P1-6 | 专业容器/声道/序列 | probe 层通过；真实导入分类/流消费需整改，R12。 |
| P1-7 | 元数据导入导出 | API/命令存在，匹配/原子性/编码/入口待整改，R13。 |
| P2-1 | Cloud/Power Bins | 设计完成，真实服务明确延期，D01。 |
| P2-2 | Media Management | 后台化与基础预检存在，保存闭包/输出事务/生命周期仍有 P0，R01/R02/R04。 |

## 复现与验证记录

复现脚本位于 `docs/validation/`，可在项目根目录使用 `.venv/bin/python` 运行；它们只在临时目录生成合成媒体文件，不启动真实推理或真实编码，不打开或改写用户工程。

| 脚本 / 输出 | 关键观测 |
|---|---|
| `media_pool_backend_review_20261110_archive_repro.py` / `..._archive_results.txt` | `existing_target_overwritten=true`；保存成功但 `saved_timeline_sequences=[]`；queued work 使用替换工程及旧 sequence。 |
| `media_pool_backend_review_20261110_jobs_repro.py` / `..._jobs_results.txt` | running outer=cancelled，但 summary=succeeded 且 evidence_stored=true；失败代理目标不再存在；模态 Relink 写入 different-project。也包含已修 pending/协议校验的正向结果。 |
| `media_pool_backend_review_20261110_query_csv_repro.py` / `..._query_csv_results.txt` | 同路径歧义无警告；并发 comment=`concurrent`，CSV Apply 后 Undo 变空；含分号关键词往返被拆分；已修关键词匹配与数值校验正常。 |

代理复现使用 mock 失败进程；AI 复现使用 mock provider，证明取消/发布边界缺口，不代表已在真实模型上测量中断延迟。脚本中的 Undo 先前重命名对照案例保留了重连路径，**不是**撤销缺陷；未将该猜想列入计划。

最终回归 **334 passed, 11 skipped（345 项，20.13 秒）**，命令与完整结果见 `docs/validation/media_pool_backend_review_20261110_tests.txt`。范围包括原后端专项、新增回归、multicam、usage、archive、consolidate、interpretation、source-range、AI request/scheduler/progress/service、proxy tasks 和 media-analysis controller。11 个跳过项来自缺少 ffmpeg CLI 的 9 项编码集成，以及仅 Windows 运行的 2 项归档测试；这些路径不能视为已验证通过。本轮没有运行真实云端/ASR 服务，也未做十万素材 UI 性能认证。

工作区原有 Color Gallery、全局样式及其他未提交修改保持原状；本次没有替用户提交 Git。
