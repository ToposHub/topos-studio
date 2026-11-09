# Viewport 复查后续计划（I01–I07）验收记录

日期：2026-11-09。执行：本仓库 `bugfix` 分支。对应计划：
`docs/plans/2026-11-09-viewport-integration-review-followup.md`（ChatGPT
复查清单的修复批次）。

- **基线 commit**：`0db1b1be`（前端 Viewport 合流）。
- **本批终点 commit**：见 §1（7 个实现提交）。
- **平台**：macOS 15.3 offscreen（`QT_QPA_PLATFORM=offscreen`），
  Python 3.12.14，PyQt6 6.11.0 / Qt 6.11.2。
- **状态口径**（计划 I07-3）：每项拆分"域件完成／宿主接线完成／媒体
  验收完成"三栏；未执行的真机项如实标注，不填通过。

## 1. 提交清单与状态

| 任务 | Commit | 域件 | 宿主接线 | 媒体验收 |
|---|---|---|---|---|
| I01 全入口播放仲裁/真实反馈/页面生命周期 | `9ebd8ba6` | ✅ authorize_play/上下文同步 | ✅ 真实入口门+Deliver/页面/关闭钩子 | ⚠️ 按键手感/双声听感属真机 |
| I02 Source 时间域/校验/换源状态 | `5e68d625` | ✅（B04 既有）指纹/域接入 | ✅ open/restore/校验全链 | ✅ 冒烟（见 §3）＋⚠️ VFR 索引真机 |
| I03 capability/Match Frame 宿主 | `8177cf00` | ✅ 注册表政策修正 | ✅ 菜单/按钮回填+双向 Match Frame | ⚠️ 菜单视觉/手感真机 |
| I04 Source 拖出生产端/身份/事务 | `0a868c2a` | ✅ 指纹字段+校验 | ✅ 画面/图标拖出+insert 意图+回滚 | ✅ 拖拽阈值像素级属真机，语义已测 |
| I05 工程关闭 Source 释放边界 | `53bf31ab` | ✅ release_for_project_close | ✅ _close_current_project/媒体会话分支 | ✅ widget 级全测；整机 Cmd+W 真机 |
| I06 双视图预算执行 | `8e64c4ab` | ✅（B03 既有） | ✅ 执行器接真实质量档/结构核验/诊断 | ⚠️ 4K/HDR 吞吐与 10 分钟稳定性真机 |
| I07 整页验收/记录闭环 | 本记录 + 计划记录 | — | ✅ 组合宿主集成测试 | ✅ 用户素材冒烟（offscreen 子集） |

## 2. 各任务落地要点（对照复查清单原文）

**I01**：`ViewportPlaybackArbiter.authorize_play`（门控+暂停对侧、不发
Start——入口链保留 `_sync_playback_duration`/JKL 档位/scrub 恢复等准备
步骤）；`set_source_asset_id` 决策点上下文同步。协调器 sink 核验真实
效果（静默暂停/起播失败 → 拒绝并记日志，不虚报）；反馈路径
（`state_changed`＋新 `playback_state_changed`）广播快照；
`sync_initial_state` 安装即对齐（素材/两路播放/布局/激活/页面）。入口门
安装在 SourceViewerWidget（toggle/scrub 恢复/reverse）与 TimelinePanel
（toggle/start_playback/J/L 起播）；Deliver 借还与页面 enter/leave、
shutdown 管线全部驱动仲裁器；拒绝原因回填状态栏。暂停永不受门控。

**I02**：`media_fingerprint_for_path`；`_build_time_domain`（显式
rational fps 优先，否则 float→Rational；start_timecode/DF；VFR 索引只经
`set_source_time_index` 异步注入）；open/restore（指纹+帧数校验、恢复区
间再校验、generation 提升后单次 `_load_frame`）；close_source 保存；
In/Out setter 统一走时间域校验（负值/越界/反置零写入 + `range_rejected`
回因；单帧合法；无素材拒绝）；`_frame_to_timecode` 起始 TC 偏移 + DF。

**I03**：注册表修正——`match_frame.source_to_timeline` 需素材+时间线；
`source.loop` 需素材；Deliver 不再豁免 source_to_timeline。EditViewport
新增能力动作协议（`capability_action_requested` /
`capabilities_context_requested` / `refresh_capabilities`；菜单+底栏同键
共享回填，默认禁用不虚亮；reverse 本地消化）。宿主
`viewport_capability_host`：真实上下文（页面模式取仲裁器快照单一事实
源）→ 注册表 → 回填；Timeline→Source 复用
`resolve_match_frame_at_playhead`＋既有 `_on_clip_open_in_source`；
Source→Timeline 经 `timeline_to_source_frame` 反查精确帧（2x/冻结准
确），多处使用弹候选、取消不动播放头；无 handler 的键不注册。F 键维持
既有"适配窗口"绑定（产品决定，未改为 Match Frame）。

**I04**：P1——`insert_clip(stream_intent=...)` 覆盖 patch 路由（video ⇒
音频侧关 / audio ⇒ 视频侧关，统一配对/单流/拒绝判定；显式音频目标不再
被蓝徽章重定向）。P2——载荷先经可信 metadata 校验（指纹/区间/流）再建
轨；建轨后插入失败回滚建轨命令（零孤立轨/零 Undo 垃圾）。载荷新增可选
`media_fingerprint`（跨工程同名 ID 拒绝）；忽略载荷自报 fps（换算全部用
素材 metadata）。生产端：`start_source_clip_drag`（全段政策/当前窗口/
流不存在拒绝/域指纹+Rational fps）；画面 10px 左键阈值拖出（平移仍中
键）；Video Only／Audio Only 图标为真实拖出源。

**I05**：`release_for_project_close`（会话级：停播/关解码器/清
asset/In-Out/监看存储/归还显示 slot；非永久 token，widget 可复用）；
`_close_current_project` 在保存确认通过后调用并同步仲裁器快照；无工程
媒体会话分支同样释放；应用退出仍走 `prepare_for_shutdown` 永久 token。

**I06**：`ViewportBudgetExecutor`——真实输入（激活/可见/两路播放/pane
尺寸）→ B03 政策 → 应用：Timeline 渲染器**既有**
`set_preview_quality` 档位（不超 cap 的最近档；未布局不喂假尺寸）；
Source cap-1/hidden 门控为结构性已达成（单槽 CPU worker＋单在飞 GPU＋
B02 调度），执行器核验记录；I01 快照变化驱动重评；`diagnostics()` 出
政策+实际生效+真实计数器（JSON 可序列化），应用失败记 warning。
CPU 提交路径补齐 `decode_submitted` 计数（与 GPU 路径同口径）。

**I07**：组合宿主集成测试（真实 widget/panel + 与 dock 相同的安装函数：
协调器→门→能力宿主→预算执行器；互斥/快照/能力/预算/失败路径/关闭释放
跨层一致）；用户素材冒烟（§3）；本记录与计划文档状态更新。

## 3. 用户素材证据（tests/test_videos，offscreen 真实子集）

`tests/edit/test_viewport_real_media_smoke.py`（标记 `real_media`，素材
缺失按 `real_media` 语义 skip）：

- 真实 `2K_prores422.mov`（487 帧@25fps，冻结清单）经 CPU 解码 worker
  （FFmpeg 线程）：open → seek(24) → 帧到达（`_last_good_frame`）→
  `decode_submitted ≥ 1`；
- 播放时钟真实推进 ≥1 帧 → pause；`release_for_project_close` 在真实
  解码会话上关闭解码器/清缓存无异常；
- In/Out 设置/反置拒绝/25fps TC 显示（帧 50 → 00:00:02:00）。

**未覆盖（真机证据边界，如实不填通过）**：跨页/全屏/浮动/单双/工程
切换/关闭的整机循环与音频听感；4K/HDR 双视图吞吐（呈现率/p95/10 分钟
内存）；拖拽阈值像素级手感；HDR 监看一致性；Source Tape/反向音频/源
marker/monitor bypass/Source 外部监看（显式不实现，注册表带原因禁用）。

## 4. 测试证据（offscreen）

新增/修改用例（全部先红后绿或对既有行为断言）：

```
tests/edit/test_viewport_playback_arbiter.py        +6（authorize/上下文）
tests/edit/test_viewport_playback_coordinator.py    +8（诚实 sink/反馈/同步）
tests/edit/test_viewport_playback_entry_gates.py    +7（真实入口集成）★新
tests/edit/test_source_time_domain_integration.py   +11（域接入）★新
tests/edit/test_viewer_capabilities.py              +2（政策）/1 项改判
tests/app/test_viewport_capability_host.py          +8（宿主）★新
tests/edit/test_edit_viewport.py                    +1（能力分发）
tests/edit/test_source_drag_drop.py                 +10（意图/回滚/身份）★新段
tests/edit/test_source_drag_producer.py             +9（生产端）★新
tests/edit/test_source_shutdown_lifecycle.py        +3（I05）★新段
tests/edit/test_viewport_budget_executor.py         +7（执行器）★新
tests/app/test_viewport_full_host_integration.py    +4（整页组合）★新
tests/edit/test_viewport_real_media_smoke.py        +3（用户素材）★新
tests/edit/test_track_lock_guard_and_source_range.py / test_source_viewer_
scrub_preview.py                                    2 项按新校验更新夹具
```

聚焦命令（全部通过）：

```
QT_QPA_PLATFORM=offscreen pytest tests/edit/test_viewport_playback_arbiter.py \
  tests/edit/test_viewport_playback_coordinator.py \
  tests/edit/test_viewport_playback_entry_gates.py \
  tests/edit/test_viewport_budget_policy.py \
  tests/edit/test_viewport_budget_executor.py \
  tests/edit/test_viewport_preferences.py tests/edit/test_viewport_icons.py \
  tests/edit/test_viewport_scrubber.py tests/edit/test_edit_viewport.py \
  tests/edit/test_source_time_domain.py \
  tests/edit/test_source_time_domain_mapping.py \
  tests/edit/test_source_time_domain_integration.py \
  tests/edit/test_source_drag_payload.py tests/edit/test_source_drag_drop.py \
  tests/edit/test_source_drag_producer.py \
  tests/app/test_viewport_capability_host.py \
  tests/app/test_viewport_full_host_integration.py -q   → 317 passed
QT_QPA_PLATFORM=offscreen pytest tests/edit/test_source_viewer_public_state.py \
  tests/edit/test_source_viewer_playback_chain.py \
  tests/edit/test_source_viewer_playback_backpressure.py \
  tests/edit/test_source_viewer_scrub_preview.py \
  tests/edit/test_source_hidden_scheduling.py \
  tests/edit/test_source_shutdown_lifecycle.py \
  tests/edit/test_source_transport_adaptation.py \
  tests/edit/test_source_present_gate.py \
  tests/edit/test_match_frame.py tests/edit/test_match_frame_view.py -q
                                                        → 97 passed
QT_QPA_PLATFORM=offscreen pytest tests/edit/test_viewport_real_media_smoke.py -q
                                           → 3 passed（真实解码）
```

全量回归（`tests/edit + tests/app + tests/deliver` 组合，offscreen，同
进程约 26 分钟）做了**两轮完整对照**——修复批次 HEAD 与不含本批任何
改动的基线 `0db1b1be`（独立 worktree）：

| 轮次 | 结果 |
|---|---|
| 基线 0db1b1be（无本批改动） | 24 failed / 14775 passed / 119 skipped |
| 本批 HEAD（首跑） | 18 failed / 14945 passed / 43 skipped |

逐项 diff 归属：

- **HEAD 独有失败 1 个**：`test_project_save_routing` —— I05 的
  `_release_edit_source_viewer` 在 SimpleNamespace 测试宿主上直接调用
  抛 AttributeError。**本批 bug，已修复**（改经 `getattr` 守卫调用，
  其套件 76 passed 复验）。
- **其余 17 个 HEAD 失败全部在基线同配置下同样失败**（n03_grade×5、
  n03_mask×3、ui2_aud3、linked_ripple、disable_shortcut、affordance×2、
  pointer×1 共 14 个为上一批已归属的稳定基线；另有 3 个组合配置下的
  顺序/加载敏感项——`poster`（`tests/app/test_deliver_composite_frame`
  把 `tests/` 插到 `sys.path[0]` 遮蔽 `src/shared`，**基线可复现，本批
  已顺手修复**）、`ef19_aud`、`quick_export`（单跑两代都通过））。
- 基线独有失败 7 个（ai_editing×4、wd13g69×2、affordance 参数化×1）
  为基线侧的加载敏感 flake，HEAD 轮未出现。

结论：**修复批次对组合全量配置零新增稳定失败**；组合配置本身暴露的
先存问题（path 遮蔽、加载敏感 flake）已修复一项并逐项归属，其余与
本批无关。

## 5. 已知边界与后续

1. 本批未把 Composite/Color 侧直接驱动共享控制器的旧路径（B8 类）改经
   仲裁门——Edit 离页暂停已保证互斥，反馈路径保持快照 truthful；这些
   页面自己的播放政策由各自计划（Color：CV-B01–B11）处理。
2. GPU 可用性探针仍未全局化（capability 的 fullscreen 门保持默认可用，
   等 B02 计数接入宿主信号后由真实状态驱动）。
3. `tests/benchmark` 的真机驱动脚本（drive_real_app_*）可复用于后续
   4K/HDR 专项验收；本批未执行。
