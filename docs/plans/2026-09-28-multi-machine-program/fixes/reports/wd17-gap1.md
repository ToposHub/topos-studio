# 补测报告 wd17-gap1 — GAP-002 / GAP-010（批 D 最后的编辑侧 test-gap）

- 日期：2026-10-01
- 计划表：docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md（条目行 1711 / 1718）
- 证据报告：docs/testcoverage/2026-09-28/GAP_REPORT.md（#2 / #10）
- 新增测试（仅两个新文件，未改任何 src/ 与既有测试）：
  - `tests/app/test_gap002_edit_controller.py`（66 用例）
  - `tests/app/test_gap010_ai_edit_controller.py`（44 用例）
- 命令：`.venv/bin/python -m pytest tests/app/test_gap002_edit_controller.py tests/app/test_gap010_ai_edit_controller.py -q`
  → **110 passed**

## 逐条结论

| 编号 | 模块 | 结论 | 新增用例 | 覆盖提升（同一选择集口径，见 §3） |
|------|------|------|---------|--------------------------------|
| GAP-002 | `src/app/controllers/edit_controller.py` | **PARTIAL**（高风险路径已补，全文件仍大） | 66 | 11.4% → 22.3%（+559 语句，+11.0pt） |
| GAP-010 | `src/app/controllers/ai_edit_controller.py` | **PARTIAL**（高风险路径已补，全文件仍大） | 44 | 35.5% → 46.8%（+419 语句，+11.3pt） |

两条均按"补高风险路径断言、不凑行数"执行；32%/39% 的全仓口径见 §3 说明。因
两个文件各 10k/6.7k 行、269/200+ 方法，一次批次不可能补满，判 PARTIAL。

## 1. GAP-002 覆盖的主要风险路径（edit_controller）

纯函数边界/异常分支：

- `_fps_token`：Rational/字符串/float 归一，None/畸形输入降级（不抛）。
- `_preview_content_aspect`：视频模式优先显示尺寸；图像模式忽略渲染器残留的
  last-video 元数据；`img_source_full.shape` 回退；全非法 → None。
- `_crop_pixmap_to_aspect`：宽/高两侧去 matte、±1% 容差、null pixmap /
  无目标比例透传。
- `_remove_render_cache_file`：主文件 + `.tmp`/`.realtime.tmp` 旁车全删；空
  路径/缺失文件 no-op。
- `_render_cache_manifest_from_value`：dict/实例归一，垃圾值 → None（宽松 dict
  契约由 `from_dict` 兜底，已按实际行为断言）。

撤销栈/命令语义：

- `_execute_timeline_edit_command`：TimelineView 命令栈路由（单一撤销入口，
  直连 apply 不触发）；无视图宿主的回退路径含复合命令 tuple 解包 + 手动同步。
- `_CreateStampedColorStackCommand`：apply/undo/redo 间 stack_id 稳定；
  displaced 碰撞时 undo 恢复原栈；栈被他人替换时 undo 守卫 no-op。

状态机分支：

- `_cleanup_obsolete_render_cache_files`：clip/scene 过滤、rendering 默认保留、
  `include_rendering` 显式清理、valid+指纹保留矩阵（tmp 文件级断言）。
- `_prune_render_cache_variants`：>max 删旧留新 + 不可引用文件删除；宿主无
  prune API / prune 抛 TypeError 的降级。
- MG 空闲缓存：`_edit_session_is_quiet_for_mg_cache` 四分支（Edit 播放 /
  Color 播放 / 渲染器忙 / 探针异常降级）；`_maybe_start_idle_mg_cache` 的
  定时器缺失、不安静、串行守卫、in-flight 去重、非 Comp/MG 素材跳过、
  单片段入队；`_cancel_idle_mg_cache_renders` 的 worker cancel + inflight
  清理 + 工程失效级联。
- `_invalidate_motion_graphic_caches_for_timeline_change`：分辨率契约变化、
  纯 trim 早退、retiming 失效、无工程/无 previous 早退。
- smart 自动缓存：`_render_cache_auto_mode/_threshold` 的偏好读取与异常回退
  （off/6）；`_auto_evaluate_render_cache` 的 off 早退、无时间线早退、
  分数达标但普通素材不入队、MG 素材单配额入队（`_RENDER_CACHE_AUTO_BATCH=1`）。
- `_on_timeline_settings_requested`：伪对话框驱动完整状态机（fps 24→25 保持
  绝对时序 48→50 帧、ref/format_settings 回写、active 时间线重载、取消不变更、
  缺工程/缺 ref 早退）。

错误/异常路径：

- `_invalidate_clip_render_cache`：吞掉工程失效异常（缓存失效绝不打断编辑）；
  缺工程/缺 API 早退。
- 字幕命令提交：`_execute_subtitle_command` 无时间线/无视图降级、命令执行异常
  → 状态栏报错 + 返回 False；`_commit_subtitle_drag_or_restore` 失败回滚
  （SubtitleEditPolicy.restore_on_failure + 轨道重排）。
- ASR worker（同步调用 `run()`，不起线程）：provider 错误、任务 failed 终态、
  空转录、provider 异常四条 failed 分支 + 成功/进度上报。
- TTS worker：成功（SpeechCue 组装/fps/settings 透传）与 provider 异常。
- 音频路由错误收集：`_collect_audio_routing_errors` 三态（无页面/读取异常/
  正常元组）；`_warn_audio_routing_errors_on_save` >8 条折叠为 `+N`、无错
  不弹窗（monkeypatch QMessageBox）。

公开 API 契约：

- `_apply_project_change`：媒体池/TimelineView（含 project_changed emit）/
  预览渲染器全量同步；ADR take sheet 首次重建、同实例不重建。
- `_multicam_clip_at_playhead`（真实 multicam_group_from_asset）：命中/组过滤/
  片段外/非多机位。
- `_find_timeline_clip`、`_get_subtitle_clip_field/duration/start`（默认值契约）、
  `_resolve_subtitle_style`（异常 → None）、`_custom_subtitle_style_updates`
  （冻结生效样式，未提供字段不回退 dataclass 默认）。
- 调色剪贴板：`_get_grade_clipboard` 惰性单例；`_grade_clip_layers` v2 快照
  优先 / v1 平铺键回退（list→tuple、LUT、mixer、soft_clip）；`_on_grade_copy_requested`
  无栈拒绝 + 有栈分类捕获。
- ADR 上下文：`_adr_settings_dict`（面板缺失/异常/正常）、`_adr_timeline_context`
  （fps+播放头、非法帧号 → None）、`_adr_loop_window_seconds`（范围反转/缺失/
  非法 fps/非正 fps）。
- `_rational_for_timeline_fps`（23.976/29.97/59.94 drop-frame 精确映射）、
  `_format_settings_from_dialog`（白名单合并）、`_timeline_settings_for_dialog`。

## 2. GAP-010 覆盖的主要风险路径（ai_edit_controller）

生命周期契约（dispose 家族）：

- `dispose`：三类 worker 引用清空、`service.on_state_changed` 置空、自主执行
  全部账本/目标/检查点清空、`_pending_skill_continuations` 清空。
- dispose 后 `panel.request_submitted`/`cancel_requested` 不再路由（真实 Qt
  signal 断连验证，状态机不推进）。
- `_retire_worker`：None/无 isRunning no-op；运行中 → cancel 标志 + quit +
  有限 wait + 自定义信号全量断连；不可中断（wait 超时）→ `_DETACHED_WORKERS`
  detach；`wait=False`（Motion Graphic 浏览器后处理）不等待。
- `_detach_running_worker`：已结束线程立即回收（进程级集合不残留）。

worker 状态机：

- `_PlanWorker.run`（同步调用）：成功 finished_plan / 异常 failed + service
  record_error / LLM 返回后置 cancel → 丢弃结果 emit cancelled。
- `_PlanStateGate`：引用计数（嵌套挂起、过量 resume 不为负）。
- chat 三信号：`_on_chat_finished` 迟到 worker 身份守卫、正常回复记录 + 回
  IDLE、空回复占位文案、scene_edit 响应路由（模式复位）与 handler 异常转
  failed；`_on_chat_failed` 正常路径（错误消息 + 推荐卡兜底）与迟到守卫；
  `_on_chat_cancelled` 部分流式回复保留（preserve hook）、MG build 取消走
  兜底通知（空响应）。

上下文压缩（`_compress_chat_messages`）：

- 限内透传（标志复位）；超限 → 系统块前置 + 自动摘要 + 保最近 6 条，溢出
  记录 spill + checkpoint 写入；同签名 checkpoint 去重（只写一次）；极小预算
  下整条裁剪链收敛（淘汰旧消息 → 缩摘要 → 缩正文/多模态降级 → 缩系统块），
  发出请求不超预算（+64 容差）。

会话目录一致性：

- `_on_new_session`：create_session、澄清往返状态清空、内存历史 reset、压缩
  统计复位；无 store / 创建异常吞掉。
- `_switch_to_session`：空 id/无 store/归档拒绝；成功切换清内存后从目标
  session 回灌（内存-持久化一致）。
- `_on_clear_history`：store/run_trace/spill 清空 + 内存 reset + 面板标题清空；
  无 store no-op。
- `_on_conversation_title_changed`：空白归一、空标题忽略、持久化 + 面板回写。

多模态与抓帧：

- `_validate_chat_attachments`：无 profile 拒绝 + 警告；不支持输入类型阻断 +
  可读原因；支持时放行（monkeypatch 能力目录）。
- `_chat_profile_for_mode`：teaching 模式走 `active_profile_for_capability("language")`
  路由、异常回退默认 profile、registry 缺失 → None。
- `_on_frame_grab_requested`：无 viewer 提示、bridge 异常降级、抓帧失败提示、
  null 图静默、成功以 `source="frame_grab"` 入附件。
- `_chat_activity_label` 全分支（用户可见文案不泄漏 prompt 内部）。

公开 API / 持久化契约：

- `export_plugin_state`（无 registry → None；导出 schema_version/snapshot/
  checkpoint id）、`restore_plugin_state`（非 dict → None；合法载荷往返，不
  激活插件）。
- `export/restore_tool_operation_ledger`、`export/restore_context_spills`
  空快照与 None 快照契约。

run 事件与诊断：

- `_on_run_event`：事件入 run_trace_store；仅活动 chat run 的
  `run.state_changed` 触发跨线程信号。
- `_on_run_diagnostics_requested`（真实 HarnessRuntime 失败 run）：空 id/未知
  id 不弹窗；错误码 + 建议文案（AUDIO_CLIP_NOT_SELECTED → 选中音频重试；
  ASR_FAILED → 检查模型）；ASR 建议分支。

## 3. 覆盖率对比（可选手段，pytest-cov 可用）

口径：同一选择集前后对比 —— "tests/app 下 9 个 edit/ai 控制器相关既有套件"
（before）vs "同 9 个 + 本次 2 个新文件"（after）。GAP_REPORT 的 32%/39% 为
全仓口径（tests/edit、tests/ui 等亦覆盖这两个模块），无法在本批逐一复跑
（见 §5 环境性挂起），故以同口径增量为准：

| 模块 | before | after | Δ |
|------|--------|-------|---|
| `edit_controller.py` | 11.4%（580/5102） | 22.3%（1139/5102） | +559 语句 / +11.0pt |
| `ai_edit_controller.py` | 35.5%（1314/3703） | 46.8%（1733/3703） | +419 语句 / +11.3pt |

抽样核对的目标函数均已从 0 命中变为有效覆盖（如 `_maybe_start_idle_mg_cache`
34/56、`_cancel_idle_mg_cache_renders` 34/51、`_on_timeline_settings_requested`
37/96、`_invalidate_motion_graphic_caches_for_timeline_change` 37/110、
`_compress_chat_messages` 60/117、`dispose` 47/153、`_on_chat_finished` 33/97、
`_on_run_diagnostics_requested` 26/70 —— 分母含 docstring/不可执行行）。

## 4. 未覆盖路径及原因

- GAP-002：`_on_render_cache_clip_requested`（142 缺失行）与 `_start_render_cache_worker`
  的真实渲染入队（需真实 Comp/MG 渲染 worker 与编码会话）；`_on_timeline_selection_changed`
  的全量选中语义（依赖 TimelineView item 生态）；`_on_speech_to_text_completed` /
  `_on_subtitle_to_speech_completed` 的完整提交链（需 `_probe_audio_info` 真实
  WAV 与工程导入）；`_on_mixer_routing_committed` 等混音台 UI 对话框路径；ADR
  真录音（`_start_recording`/`_on_take_recorded` 需音频设备）。均属"真机 GPU/
  音频设备/编码器"依赖，按任务要求跳过。
- GAP-010：`_on_request_submitted` 的 Motion Graphic / Teaching / media 模式
  路由（需 feature module handler 注入与 v2 orchestrator，现有 tests/ui 与
  tests/ai 已覆盖大半）；`_start_chat`/`_on_chat_finished` 的 html_generate
  构建链（浏览器后处理）；V2 orchestrator 事件流（`tests/app/
  test_ai_controller_agent_runtime_v2.py` 已覆盖主干）。
- 复杂对话框真实 exec（TTS 设置、字幕轨道选择等）不可无头验证，仅覆盖其
  纯逻辑与降级分支。

## 5. 回归与环境备注

- 新测试 + 既有控制器套件：`.venv/bin/python -m pytest tests/app/test_gap002_…
  test_gap010_… -q` → 110 passed；同目录 9 个 edit/ai 控制器既有套件 →
  113 passed；合计运行 210 passed。
- `tests/edit/ai_editing + tests/ui` → 3969 passed / 2 failed：两例
  `tests/ui/test_p1_media_pool_selection.py` 单独复跑仍失败（与本次新增文件
  无关的预存失败，未收集入已知清单，建议主会话关注）。
- `tests/edit` 全量在本机被环境性挂起阻断（原生 libavcodec 解码等待）：主会话
  的遗留 pytest 进程（PID 2302，13:49 起）卡在 `test_scene_cut_false_positive_
  guards.py` 真实素材解码 >1 小时；本批 runs 在 tests/edit 92% 处同样卡于解码。
  逐段探测证明其余全部文件单独运行通过（含 GPU mask 测试），唯一失败为已知
  预存 `test_track_sync_lock`。本批新增测试与该挂起无因果（新文件不在
  tests/edit，且挂起先于本批 tests/edit 运行出现）。
- 中途清理过一次测试运行产生的未跟踪 `cache/scene_cut/`（运行期产物，git
  未跟踪，测试会自行重建）；如主会话的 scene-cut 缓存分析因此重算，属预期。

## 6. 疑似缺陷（记录，未修改源码）

- `edit_controller._cleanup_obsolete_render_cache_files`（src/app/controllers/
  edit_controller.py:5987-5996）：`referenced_elsewhere` 的构造条件把"abspath
  在目标集合内"的引用排除在外，而目标路径本身必然在目标集合内 →
  `os.path.abspath(path) not in referenced_elsewhere` 对目标恒真，"共享路径
  保护"分支永不生效；当另一 manifest（如别的 clip 的 valid 缓存）与目标指向
  同一路径字符串时，文件仍被删除、留下悬空 manifest。与 docstring"仅在无其他
  manifest 引用时删除"相悖。本批按实际行为断言并在测试内注释标注（
  `test_cleanup_obsolete_render_cache_files_keeps_shared_paths`），建议主会话
  评估是否立 AUD 条目。

## 7. 合规确认

- 未修改 `src/`（`git diff --stat src/` 为空）；未做任何 git 分支/commit/push
  操作；未修改 REMEDIATION_PLAN.md 复选框。
- UI 测试未开 OpenGL：Qt 对象经 `qapp` fixture；worker 以同步 `run()` 驱动，
  不启动真实线程；所有 except 均为具体类型；测试文件带类型注解。
