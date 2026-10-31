# F2.5 · 同钟实时多机位与音频切换策略 — 验证台账

- 日期：2026-10-24
- 提交：片1 `20864f8a4`（音频策略三模式）＋ 片2 `00db7c172`（实时四路网格 + 插入侧成对落轨）
- 计划条目：docs/feature_plan_F_davinci_2026-10-22.md §F2.5
- 测试命令：`QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 .venv/bin/python -m pytest tests/edit/test_multicam_production.py tests/edit/test_multicam_sync_guards.py tests/edit/test_multicam_audio_policy.py tests/edit/test_multicam_grid_stream.py tests/edit/test_multicam_panel_grid.py tests/edit/test_multicam_grid_real_media.py tests/edit/test_playback_budget.py tests/edit/test_adaptive_playback_quality.py -q`
- 结果：**115 passed**（multicam 全护栏 + 播放预算/自适应质量无回归）

## 1. 验收标准 → 证据映射

| 计划验收 | 实现 | 证据 |
|---|---|---|
| 各格显示同一时间线时刻 | 控制器 `_on_multicam_playhead_frame` 把主时钟帧号透传 `panel.request_angle_frames(..., timeline_frame)`；播放中直达 `MulticamGridStream.push`（同批同刻、帧映射与切角命令同源 `map_angle_source_frame`），投递帧携带 timeline_frame+generation 双校验 | test_four_angles_same_timeline_instant（四角度同刻投递、逐角度映射一致） |
| 由一个主时钟驱动 | playhead_changed（PlaybackControllerTimer 主时钟）→ 面板 → 流；播放起停/切时间线经 signal_hub 驱动会话（dock_layout_controller 接线） | test_playback_routes_to_stream_bypassing_debounce；multicam_production 控制器护栏 |
| 独立有界解码/缩放预算 | 单工作线程、≤9 解码器、target_size=(170,96/64,64/85,48) 直入 FFmpegMediaSource（缩放不解全分辨率）、DECODE_BUDGET_MS=1000/24 单帧预算 | test_degrade_ladder_and_summary（降档 target_size 断言） |
| 优先保证 Program 输出 | 网格流为独立 CPU 小尺寸解码，不占 Program 的 VT 硬解会话池/调度优先级；暂停即 end_session 回落静帧路径 | 设计声明（multicam_grid_stream.py docstring）＋ test_pause_falls_back_to_static_path |
| 降质/掉监看帧可解释 | 每角度统计 decoded/superseded(被新请求追上)/skipped(抽稀)/decode_ms_p50/level → session_summary 信号 + playback_trace `multicam_grid.session_summary` / `multicam_grid.degrade`（摘要档词表已注册） | test_latest_wins_superseded_counted、test_degrade_ladder_and_summary、test_session_summary_shape |
| 明确固定主音轨/仅切视频/音频跟随三模式 | 片1：MULTICAM_AUDIO_POLICIES + SetMulticamAudioPolicyCommand 受限事务 + 切角 follow 联动宏；片2 补插入侧：非 video_only 且主角度带音频 → InsertLinkedAssetRippleCommand 链接 A/V 对（fixed_main 的"主音轨"= 初始角度音频），重建组沿用既有策略 | test_multicam_audio_policy.py 12 项（片1 7 + 片2 5） |
| 切角点准确 | SwitchMulticamAngleCommand（split+换源+fps 换装+FE-B06 无偏移累计）；片2 成对落轨后 follow 宏切 A/V 同刻同源 | test_av_pair_switches_together_single_undo、test_follow_angle_pair_export_truth |
| 音频策略可撤销且导出一致 | 策略命令 apply/undo 对称、冲突拒绝不半写；导出读时间线最终音频片段（音频轨即真相） | test_apply_undo_roundtrip、test_conflicting_change_refused_no_half_write |
| 暂停/seek/速率/缺路/最小化恢复正确 | 暂停=会话结束+静帧回落（test_pause_falls_back_to_static_path）；seek/速率=latest-wins+generation 取消（test_generation_cancel_on_seek、test_latest_wins_superseded_counted）；缺路=退避节流重开、恢复即 live（test_missing_angle_backoff_and_recovery，退避期 0 次 hammer）；最小化/隐藏=可见性门控停发、恢复续流（test_hidden_panel_gates_stream_push、test_visibility_gate_no_decode_when_inactive） | 上述 7 项流级 + 5 项面板级 |
| 不能以缩略图冒充实时四路 | 播放路径不经 ThumbnailProvider（测试断言 provider 零调用、去抖定时器未启动）；流级用真实解码契约（decoder_factory 注入真实 `_open_cpu_decode_source` 同路径） | test_playback_routes_to_stream_bypassing_debounce、test_multicam_grid_real_media.py |
| 记录同机负载与掉帧 | 真实素材四路 vs 基线对拍（下 §3）；掉帧计数进统计/trace | test_four_way_vs_baseline_load（1 passed，数字入台账） |

## 2. 实现清单

- `src/features/edit/ui/multicam_grid_stream.py`（新）：MulticamGridStream——单工作线程、每角度 latest-wins 槽（`_AnglePending.seq`）、同请求去重（`last_seq`，静置不重解、空闲不空转 `_idle` 协议）、generation 取消（begin/end_session）、降质阶梯 DEGRADE_LADDER（1×96 → 2×96 → 2×64 → 3×48；42ms 预算、4 次滞回恢复、档位换装关旧句柄）、缺路退避（MISSING_RETRY_EVERY=8 按请求数，成功即 failures=0 复位）、≤9 解码器有界（超限逐出最久未用路径）、统计快照线程安全。
- `src/features/edit/ui/multicam_panel.py`：双路径路由（播放中 `push` 直达流绕开 250ms 去抖；暂停/隐藏回 ThumbnailProvider 去抖静帧）；tile 模式徽标（LIVE / LIVE·Lx 降质 / 静帧 / 缺路，随帧与 angle_state 刷新）；`grid_stream_factory` 注入；showEvent/hideEvent 门控 + push 时 `window().isMinimized()` 检查；组清空 end_session；destroyed→shutdown。
- `src/app/controllers/dock_layout_controller.py`：playback_started/stopped → `set_playback_active`；timeline_switched → 结束会话。
- `src/app/controllers/edit_controller.py`：timeline_frame 透传；创建组流程消费端契约适配（`_inserted_clip_id`/`_video_clip_id` 双取）。
- `src/features/edit/core/multicam.py`：`plan_multicam_insert` 插入侧成对落轨（非 video_only 且主角度带音频 → InsertLinkedAssetRippleCommand；video_only/无声 → 原 video-only）；重建同组素材沿用既有 audio_policy。
- `src/features/edit/core/playback_debug.py`：`multicam_grid.session_summary` / `multicam_grid.degrade` 入 _SUMMARY_STAGES。
- 测试：tests/edit/test_multicam_grid_stream.py（7）、test_multicam_panel_grid.py（11→17）、test_multicam_audio_policy.py（7→12）、test_multicam_grid_real_media.py（1，真实素材）。

## 3. 同机负载证据（test_multicam_grid_real_media.py，本机 darwin 24.3.0）

素材 tests/test_videos/2K_h264.mp4（同码流 ×4 解码器实例），96px target_size，
单线程轮转（与网格流 worker 同形态），每角度 12 帧：

| 通道 | p50 | p95 | 总耗时 |
|---|---|---|---|
| 基线（单角度连续） | 51.4 ms | 63.7 ms | 579 ms / 12 帧 |
| 角度0 | 42.0 ms | 57.8 ms | — |
| 角度1 | 44.9 ms | 61.4 ms | — |
| 角度2 | 48.7 ms | 59.9 ms | — |
| 角度3 | 44.4 ms | 61.3 ms | — |
| 四路合计 | 45.2 ms | 61.9 ms | 2069 ms / 48 帧 |

结论：单帧 p50≈45ms 略超 42ms 预算 → 降质阶梯按设计介入（L1 抽稀标记）；
四路在单线程下总吞吐 ~22 帧/秒，即每角度 ~5.5Hz 刷新（96px、CPU、不占
Program VT 池）。48/48 帧全部解码成功。此为"记录同机负载与掉帧"的本机
样本；绝对数字随机器漂移，不设硬性性能门槛（断言只做正确性）。

## 4. 九路扩展评估（按四路证据，计划允许"再评估"而非本期实现）

- 外推：9 角度 × 单线程 45ms → 每角度 ~2.4Hz（L0）不达监看体验；需 L2/L3
  档（64/48px + 抽稀）或每角度 fps 减半策略，或代理强制。
- 资源面：网格流 CPU 小尺寸解码与 Program 的 VT 硬解池天然隔离；九路不
  增加 VT 会话压力，压力在 CPU 单线程吞吐 → 阶梯已覆盖。
- 结论：四路全档可用；九路按 L2 起步可行但体验降档，**登记为后续片**
  （不阻塞 F2.5 验收）。

## 5. 登记的后续片（不阻塞本期验收）

1. 九路网格实测 + 每角度 fps 减半/代理强制策略（§4 评估落地）。
2. In-app 四路播放 + Program 同跑的 VT session_pool 驱逐实测（kVT 重试
   已有 hardware_capability.py:216；网格用 CPU 不触 VT，但整包负载曲线
   需真机 GUI 跑一次记录）。
3. GUI 端到端联验（真实播放中四格可见、切角、缺路徽标目验）——headless
   已覆盖组件契约与路由，交互目验待真机。
4. seek 过程中的网格静帧补齐（当前暂停回落静帧路径已覆盖；播放中大跳
   依赖 latest-wins，可再加"seek 结束补一帧全角度"优化）。
5. F2.5 九宫评估的 per-angle 独立线程池（若单线程吞吐成瓶颈再评估）。

## 6. 诚实边界

- 网格是监看流：解码各角度源帧用于显示，不参与 Program 合成与成片渲染。
- "掉监看帧"在流级计数（superseded/skipped）；未测 Qt present 层丢帧
  （tile QLabel setPixmap 即显，无 present 管线）。
- 真实负载数字来自测试进程（无 GUI 竞争），in-app 数字待后续片 2 实测。
