# F2.3 真双画面 Trim Viewer 与 JKL 动态修剪 — 证据台账

- 日期：2026-10-24
- 结论：**已完成（步进形态 JKL + 双画面真实边界帧）**。连续速度形态
  JKL（按住 L 以播放速度连续修 cut、预/后卷循环播放）登记后续片（§5），
  与音频预听的连续播放态集成相关，风险面更大。
- 提交：`21767814a`（feat/f-plan，已 ff-merge fix-ui 并推送）。
- 前置调研：[explore map](../research/explore_map_2026-10-23.md)。

## 1. 验收对照（计划 §F2.3）

| 验收点 | 落地 | 证据 |
|---|---|---|
| 双画面对应真实保留边界 | 左=新 cut 前一帧、右=新 cut 处，按权威 `source_frames_per_timeline_frame` 比例直接取素材源帧（与命令提交后的源窗数学同源）；反向/变速点片段（非常量比例）诚实退占位不显示错帧 | test_trim_dual_frame.py::TestRollDualFrameWiring::test_roll_requests_real_boundary_frames |
| 单帧修剪与最终输出一致 | 帧值与拖拽预览/提交命令的源窗一致（同比例数学）；提交路径复用 RollTrimCommand（未改） | 同上 + test_round3_a_roll_focus 全绿 |
| 鼠标/键盘取消不留 revision | 鼠标取消走既有 InteractionSession（未改，回归绿）；键盘 Esc 同路径；Enter 提交 | ::TestRollJklKeyboard::test_esc_cancels_without_revision |
| 提交一次 Undo | Enter → `_on_roll_trim_finished` → 单 RollTrimCommand | ::test_jkl_steps_cut_and_enter_commits_once（断言 undo 栈深 1） |
| 快速拖动/切源/关闭不显示旧 generation | provider latest-wins 合并请求（忙碌期间三连发只解码最后一条）+ generation 随帧投递 + UI 侧 generation 校验丢弃迟到帧 + hide/切源 bump + invalidate | ::TestTrimDualFrameProvider::test_latest_wins_merges_requests / ::test_generation_tagged_on_delivery / ::TestRollDualFrameWiring::test_hide_monitor_bumps_generation / ::test_stale_generation_frame_not_displayed / ::test_load_timeline_closes_decoders |
| 不泄漏纹理 | 双帧走 QImage CPU 路径（无 GPU 句柄）；解码器仅工作线程内创建/关闭（AUD-20910 同型），stop/destroyed/close_decoders 三路收口（worker 常驻轮询不随视图泄漏） | ::test_stop_ends_worker_and_closes_decoders / ::test_close_decoders_disposes_cached |
| 工具窗不抢焦点 | TrimMonitorDialog 保留 Tool+DoesNotAcceptFocus+WA_ShowWithoutActivating 三件套与 N15 失焦白名单（未动） | test_round3_a_roll_focus 回归绿 |
| 两侧时间码升级为真实切点帧 | TC 行保留既有 cut 两侧 SMPTE 时间码（frames_to_timecode）；帧区为其画面化 | ::TestTrimMonitorDualFrames |
| JKL | ROLLING 态 L/J 步进 ±1（Shift ×10）、K 暂停、Enter 提交；会话外不消费透传传输层 JKL | ::TestRollJklKeyboard（3 项） |
| 预听走现有音频时钟 | 未新建播放源（约束满足——本次未引入任何音频路径） | — |

## 2. 关键设计决策

1. **帧直接取素材源而非时间线合成**：合成帧不是"左右片各自的真实保留
   边界帧"；per-asset 解码器（≤2 缓存）复用 `_open_cpu_decode_source`
   工厂（Topos 私有编码经 factory 路由）。
2. **单线程双面顺序解码 + latest-wins**：请求在解码期间到达即被合并，
   拖拽不堆积；左右各解码一次投递（generation 同值）。
3. **显示边界 vs 源窗手柄**：`with_*_edge_source_values` 语义是源窗
   （handles），显示边界用 `source_start + offset × ratio`（与命令数学
   同源）；反向/变速点片段边界非常量比例，退占位而非给错帧。
4. **scrub 管线收尾**：新增 `trim_frame_preview_finished` → edit_page
   转发 `timeline_scrub_finished` → `finish_scrub`，消除"trim 复用
   scrub 管线后滞留量化态"缺口（调研备忘 item 7）。
5. **JKL 步进形态**：复用 ROLLING 会话状态机与单命令提交路径，键盘只
   是另一驱动源——与鼠标路径完全同构（同一 clamped 预览、同一提交/取消）。

## 3. 测试账本

- tests/edit/test_trim_dual_frame.py：provider 4 + 监视器 3 + 接线 4 +
  JKL 3 + finish 1 = 15 项。
- 护栏：trim/roll/interaction/JKL 相关 193 项全绿；1 项
  （test_pbo_rgba_to_qimage_bad_buffer_is_none）在干净基线上同样失败
  （git stash 验证），与本改动无关，属既有基线问题。

## 4. B 依赖核对

无 Plan B 依赖（UI + 后台线程 + 既有解码工厂，跨平台路径）。

## 5. 登记的后续片

1. **连续速度形态 JKL**：按住 L/J 以播放速度连续修 cut + 音频预听
  （`preview_scrub_audio` 同源）——需要 trim 会话与播放态集成。
2. **预/后卷循环**：cut±N range + `set_playback_range + set_loop` 组合
  （控制器 API 齐备，缺会话级入口与退出恢复）。
3. **普通/波纹 trim 的左半帧**：当前 update_trim 清右半；左半显示被修
  片边界帧可复用同一 provider（entry 未接）。
