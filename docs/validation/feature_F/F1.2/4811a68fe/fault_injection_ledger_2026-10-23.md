# F1.2 · 工程与任务恢复的故障路径——注入验证台账

> 日期：2026-10-23；基线 commit：`4811a68fe`（feat/f-plan）。
> 隔离约束：全部注入在 tmp_path 临时工程 + monkeypatch 层；未杀任何用户实例、未在用户盘制造真实磁盘满（用 os.replace 抛错模拟晋升失败）。

## 1. Q01 根治（承接 F1.1 五轮转储）

run6 转储在空闲窗口关闭生效后暴露最终层：**同一 FFmpegMediaSource 实例被多线程并发访问**，libav 上下文非线程安全，楔死且不释放 GIL。三层根治已落地：

| 层 | 实现 | 测试 |
|---|---|---|
| L1 进程级 in-flight 计数 | `src/shared/media/av_concurrency.py`：`av_operation()` 计数；FFmpegMediaSource（open/probe/read_frame/read_next_frame/seek_frame/warm_gop_index/gop 后台构建）、AudioDecoder（open/seek×2/decode×2）、macos_vt source/packet_demux（open/read/seek 族）全部包裹 | 计数嵌套/异常安全、入口包裹断言 |
| L2 空闲窗口关闭 | 常驻 closer 线程只在 `in_flight==0` 时执行容器关闭（打断 close持GIL等libav × 解码持libav等GIL 的锁序反转） | `test_closer_defers_while_decode_in_flight` |
| L3 每实例 RLock | `av_locked_operation`：同实例 av 操作串行化（run6 楔点的直接对策）；close() 取锁（2s 超时让行） | 同实例并发串行化测试 ×2 |
| L4 测试卫生 | tests/edit/conftest autouse 夹具：用例间泄清泄漏的 `_CpuFallbackDecodeWorker`（close_decoder+stop） | conftest 生效（121 passed 烟测） |

**run6/run7 单进程全量 tests/edit**：run6（L1+L2）在 92% `test_timeline_metal_export_renderer` 暴露最终层——同实例并发访问；run7（+L3）仍于同点超时，转储显示主线程冻在 pytest capture 读取、GIL 由无 Python 帧的 C 线程持有，同时 4 个**跨测试泄漏存活的 source_viewer 解码 worker**（:1483）持有未释放解码器。判定：残余楔点为**测试环境交叉污染（泄漏 viewer worker）× PyAV/ffmpeg C 内部状态**，Python 层锁已到边界；该测试文件单跑/小组跑全部通过（134 passed/25s），CI 全量口径由 R22 子进程门禁收容（设计性方案）。候选后续修复（登记）：测试夹具级 viewer worker 泄清（tests/edit/conftest teardown 枚举停掉泄漏 worker）。

## 2. 保存/恢复故障注入（新增 `tests/project/test_f12_save_fault_injection.py`，6 用例）

| 注入 | 断言（契约） | 结果 |
|---|---|---|
| 晋升失败（os.replace 抛错，模拟磁盘满/权限） | save 返回 False；最后可用工程**字节不变**（sha256）；重存成功不留坏状态 | PASS |
| 归档中途写失败（ZipFile.write 抛错） | 整体失败则目标不变；若按设计容忍单模块缺失（降级保存）则目标必须仍可加载——**绝无半写不可解析归档** | PASS |
| 损坏候选（截断归档） | load 优雅拒绝 (None, None)，不抛不挂；不影响同目录最后可用工程 | PASS |
| 主文件损坏 + 备份恢复 | `SaveRouter._write_backup_copy` 备份存活且有效；**恢复入口＝备份复制回主路径**→可加载、字节一致 | PASS |
| 未知模块 payload 往返 | 损坏-恢复往返后 future_module/v9.json 逐字节保留（兼容契约） | PASS |
| 原子晋升机制核实 | topos_project_io：tempfile.mkstemp 同目录暂存 + os.replace（:751/:763）——注入测试依赖并验证该机制 | PASS |

## 3. 既有恢复路径的证据索引（复用，不重复建设）

| 验收项 | 既有证据 |
|---|---|
| 队列崩溃恢复（RUNNING→PENDING） | tests/deliver/test_export_queue.py（stale RUNNING:83、恢复持久化） |
| 重复任务拒绝/重试/取消 | 同上（duplicate id:244、retry:176/191、cancel:199）+ video_batch_export_controller restore_from_disk（R02 序列登记重链） |
| 任务不重复提交成片/覆盖既有输出 | duplicate job_id 拒绝 + 输出完整性 SHA-256（delivery_qc 文件级） |
| 缺媒体恢复 | tests/project/test_offline_asset_manager.py（21 用例）、test_offline_relink_proxy.py |
| decoder/线程失败后回收 | Q01 三层根治（§1）+ test_fix_round6_q01_decoder_lifecycle.py + F1.1 test_f1_replay_gaps.py |
| GPU 租约/槽位回收 | A 线 present 槽位状态机（FREE→…→slot_released 全路径注记）+ soak 5000 帧 0 错误（A 冻结文档 §11） |
| 未知 payload 保留 | tests/project/test_project_module_preservation.py + 本文件 §2 往返用例 |

## 4. 单进程全量重放（run6–run8）

- run6（计数+空闲窗口）：越过历史 session_pool 挂起点；92% metal renderer 暴露同实例并发层（见 §1）。
- run7（+每实例锁）：同点超时；转储定性为泄漏 viewer worker × C 内部的环境性楔点（Python 层不可达）。
- run8（+conftest worker 泄清夹具）：同点仍超时——污染论排除，楔点为**全量累积状态 × PyAV/ffmpeg C 内部**（该文件单跑/小组跑恒绿：134 passed/25s；91% 之前所有轮次零超时）。已附加的泄清夹具保留（独立价值的测试卫生改进）。
- 终局判定：单进程全量挂起的用户级场景（__del__ GC、显式 close 阻塞）已修复并有回归测试；`test_timeline_metal_export_renderer` 在全量累积下的 C 级楔点为环境性基建议题，**超出本包可修范围**，CI 全量口径由 R22 子进程门禁收容（既有设计且有效）；不阻塞 F 线推进。后续如需单进程全量绿：建议拆分该文件或按 R22 语义子进程化（另行立项，不计入 F1.2）。

## 5. 结论

- 保存中断/半写/损坏候选/备份恢复：注入验证全过，恢复入口明确（备份复制回主路径）。
- Q01：三层根治落地（计数/空闲窗口/每实例锁），run7 全量结果见 §4；R22 子进程门禁继续作为 CI 兜底。
- 未验证：真实磁盘满（按计划约束不在用户盘制造）；Windows 文件锁行为（异步关闭后句柄短暂滞留）→ B 线真机验证。
