# 剪辑与调色 Viewport 第二轮复查 R01–R06 验收记录

日期：2026-11-09。执行：本仓库 `bugfix` 分支。对应计划：
`docs/plans/2026-11-09-edit-color-viewport-audit-followup-plan.md`。

- **批次起点**：`9c0284ef`（§2 小修批次终点）。
- **批次终点**：见 §1 提交表（R01–R05 五个实现提交＋本记录）。
- **平台**：macOS 15.3 offscreen（`QT_QPA_PLATFORM=offscreen`），
  Python 3.12.14，PyQt6 6.11.0 / Qt 6.11.2，PyAV 18.1.0。
- **素材**：`tests/test_videos`（真实 DNxHR/ProRes/H.264/H.265）；
  真机素材冒烟走 `tests/real_media.py`（缺失按 `real_media` 语义 skip）。

## 1. 提交清单

| 任务 | Commit | 内容 |
|---|---|---|
| R01 | `000cf733` | 静帧覆盖多文件保存事务（staging→备份→安装→manifest 日志；崩溃恢复；抓帧播放恢复 generation 守卫） |
| R02 | `35656d15` | Source drop 单宏命令（确定性建轨＋插入；失败零 revision/零历史改动、Redo 保留；成功单步 undo/redo 全覆盖） |
| R03 | `b297cf7a` | 监看域预算与用户预览质量分离（renderer `set_budget_size_cap` 两轴约束 full/scrub 全档；实际像素诊断；resize/show/hide watcher；关门/离页解除；Source 无通道约束如实标记未执行） |
| R04 | `e42f9e9e` | VFR PTS 实际播放路径（打开即消费 pts_index＋清洗；tick 正/倒放走秒域；音频起播/seek/回卷/预览同秒域；迟到索引身份校验；反查 O(log n) 秒列缓存；真实 VFR 容器端到端） |
| R05 | `f05f007d` | Color 快照按 presentation_path 取权威控制器与域（clip-local 映射不再混源全域帧数；TimelinePreview/Composite 用共享时间线控制器；源控制器不再决定时间线域可播） |
| R06 | 本提交 | 可离屏执行子集验收（§3）＋真机证据边界（§4）＋计划表收尾 |

## 2. 各任务落地要点（对照计划细则）

**R01**：`src/features/color/core/still_capture_transaction.py`——
`StillCaptureTransaction`（目标目录内隐藏 staging，同卷单文件
`os.replace` 原子；manifest 逐条落盘 journal：prepared→backing→
installing→committed；异常即时回滚，进程退出遗留由下一次事务构造时
`recover_still_transactions` 按 manifest 收敛）。`on_capture_still`：
SaveRouter 把 `.topos` 写进 staging 与 sidecar 同一提交点；JPEG 写
失败放弃；EXR 按 M09.2 降级（过期旧 EXR/声明按 drop 条目移除、回滚
恢复）；meta 写失败不留旧帧过期 meta；抓帧播放恢复收束到单一 finally
回调（token=video_controller 身份＋源路径＋clip id；`_pipeline_
shutdown_done`/仲裁器 `is_shutdown()` 双关门）。**覆盖验收矩阵**：
新建/覆盖 × 成功/SaveRouter False/抛异常/JPEG 写失败/取消/提交中断
（backing 与 installing 中段各一）——失败后旧文件集合逐字节不变、
零孤儿；成功时全文件同帧同 grade。

**R02**：`_handle_source_clip_drop` 重写为批量收集＋单
`MacroCommand` 提交。新轨走确定性 `track_id`：AddTrackCommand 先应用
到**本地暂存时间线**（纯对象图副本，不动视图/历史）供插入 planner
读到建轨后状态，宏提交时对真实时间线再应用同一命令。全部前置验证
（身份/范围/目标/锁轨）先于任何命令；宏 apply 失败由 MacroCommand
内部逆序回滚（含建轨），CommandStack 不入栈、Redo 不清。旧实现的
"失败→公开 undo()"（revision+2、Undo 清空、Redo 残留可复活空轨、
清掉用户既有 Redo）全部消除。

**R03**：`TimelinePreviewRenderer`/`PreviewRenderWorker` 新增监看域
预算面 `set_budget_size_cap`（两轴"不高于"、None 解除、同值 no-op），
模块级 `_budget_constrained_scale/_size` 同时约束 auto/smooth、显式
档、暂停 FULL 刷新与 scrub 独立档；0.125 档位下限保留（无法满足的
cap 由 `budget_size_cap_status` 如实报 unsatisfied）。执行器：预算
只在有效 cap 或渲染器实例变化时触碰；pane resize/show/hide 事件
watcher＋60ms 合并防抖；真实可见性并入 hidden（页面切走/借出后
隐藏；离屏保留逻辑值）；`mark_page_left`（离 Edit hook）与
`mark_shutdown`（关闭管线 3.7）解除预算；Source 的 request_size_cap/
priority 无请求通道——`enforced: False` 如实标记，计数器用真实
gpu_slot_stats。诊断记录渲染器报告的实际目标像素 vs cap 与未触碰的
用户偏好值，不再是 setter 调用次数。

**R04**：`SourceFrameTimeIndex.from_pts_seconds`（NaN/junk 丢弃、
重复/回退 PTS 保留首个、<2 点→None 显式 CFR 回退）＋
`seconds_column()` 懒缓存秒列；`_build_time_domain` 打开即消费导入器
`metadata["pts_index"]`；`set_source_time_index` 校验 asset_id＋指纹
（迟到 A 索引不注入 B）；`_frame_after_elapsed` 正/倒放经 PTS 秒域
求目标帧（CFR 数学不变）；`_audio_position_frame` 把起播/seek/循环
回卷/scrub 预览统一换算到音频帧钟（round(seconds×fps)）；
`source_time_mode` 如实报告 vfr_pts/cfr。Color `vfr_frame_for_seconds`
改用缓存秒列（原实现每次查询遍历全部 points 构造秒列）。**真实
VFR 证据**：PyAV 生成非均匀 PTS 的 MP4（1ms 时间基，帧距
40/70/90ms 混合）→ `extract_pts_index_seconds` 真实 demux → 判 VFR →
逐点帧↔秒往返＋音频位置一致。**已知边界**：Color 页
EnhancedVideoPlaybackEngine 的播放节奏仍走 coordinator CFR 帧钟（代
理解码管线自有时钟，接 PTS 需管线侧专项）；Color 的 VFR 帧映射生产
消费点仍是 clip `_source_pts_index`（插入链路）与共享索引方法。

**R05**：`_gather_state` 按呈现路径取唯一权威——Source-GL 有 clip：
位置/总长经 `_color_source_frame_to_local_frame`/
`_color_timeline_clip_total_frames`（生产有理数映射；几何近似兜底）
声明 clip_local；独立源显式 source_frame；TimelinePreview/Composite：
playing/loop/位置/总长取共享 Edit 时间线控制器（缺席时回落 bar 且如
实为 0，不假装）；`video_controller_available` 同样按路径判定——
只有源控制器时时间线域 transport 不再被误启。计划复现数字（源
120/1000、局部 20/50）有专项用例钉死。

## 3. 测试证据（offscreen 可执行子集）

```
# 上一轮 67 套件回归锚点 ＋ 本批五个新套件（一次合并跑）
QT_QPA_PLATFORM=offscreen pytest -q --disable-warnings --maxfail=4 \
  $(cat docs/validation/viewport_second_review_20261109_tests.txt) \
  tests/edit/test_source_vfr_playback.py tests/edit/test_source_drop_transaction.py \
  tests/edit/test_viewport_budget_executor.py \
  tests/color/test_still_capture_transaction.py \
  tests/app/test_color_viewport_capability_host.py
→ 1352 passed（0 failed / 0 skipped）

# 真机素材冒烟（素材在场，真实 FFmpeg demux/解码/探针）
QT_QPA_PLATFORM=offscreen pytest tests/edit/test_real_media_edit_smoke.py \
  tests/edit/test_viewport_real_media_smoke.py \
  tests/color/test_color_real_media_smoke.py \
  tests/edit/ai_editing/test_real_media_probe_matrix.py -q
→ 19 passed

# R06 循环稳定性（本批改动面 ×30 轮）
tests/app/test_r06_cycle_stability.py → 3 passed
（预算执行器 30 轮模式/借还循环：诊断收敛、用户质量选择器零写入；
 仲裁器 30 轮交替：revision 单调、单侧播放成立；
 Color 宿主 30 轮独立源↔clip↔preview：每轮快照收敛到该路径域，
 旧域事实零泄漏）

# tests/edit 全量（6 分块执行，规避单进程累积慢）
→ 10,536 passed / 18 failed / 38 skipped
其中 18 failed：11 个经基线 worktree（9c0284ef）复现为先存失败
（n03 keyer/mask 门、ui2 retime 菜单、ripple revision、plain-D 快捷键
等，与本批无关）；其余 7 个为分块驱动环境 PATH 缺 rg 的工具性失败
（test_present_protocol_cleanup / test_legacy_hotpath_isolation_gates
的 grep 用例，直跑全过）。本批零新增失败。

# 原生启动检查（隔离配置）
TOPOS_CONFIG_PATH=/tmp/... TOPOS_TEST_MODE=1 \
  .venv/bin/python -m src.main --product studio --test-startup \
  --no-startup-dialog → exit 0，无 traceback
（日志含预算执行器"渲染器未懒建"的一次性 warning，属如实记录非回归。）

# 新用例分布
R01 27 / R02 18 / R03 23（含 I06 契约更新）/ R04 21 / R05 8 / R06 3
```

过程修复（本批自伤，均已随批修回）：mock QMessageBox 的
`StandardButton.Yes` 需透传真枚举（自动 child mock 与真枚举永不相等，
固定名覆盖测试被误判取消）；未 show 的 QWidget resize 不发事件（watcher
测试先 show）；未初始化 QObject shell 上 `getattr` 走 Qt 元对象抛
RuntimeError（预算查询改 `__dict__.get` 惯用法）。

## 4. 真机证据边界（如实未执行，不填通过）

- 4K60/HDR 单/双视图呈现率、p95、队列水位与 10 分钟内存基线。
- Source/Timeline 音频互斥与取消静音的**听感**验证。
- 整窗 50 次切 clip/工程与 30 次 Edit↔Color↔Composite↔Deliver/浮动/
  全屏整机循环（`tests/benchmark/drive_real_app_*` 可作后续专项驱动；
  本批仅覆盖改动部件的 30 轮离屏循环，见 §3）。
- 外接 HDR 监看一致性。
- Color Enhanced 播放引擎的 PTS 节奏接入（见 R04 边界）。

## 5. 已知未实施产品范围

维持计划 §4 原样：比较会话 grid presenter、Proxy/Resolution/Bypass
服务、Unmix、独立 Grade Freeze、Alpha 显示路径、viewer-only 全屏、
外观偏好持久化；Edit 的 Source Tape/源 markers/倒放音频/外部监看。
菜单门 `set_menu_open` 仍无生产 feed。保持禁用＋真实原因。
