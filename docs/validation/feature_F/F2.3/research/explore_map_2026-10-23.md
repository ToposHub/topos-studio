# F2.3 真双画面 Trim Viewer — 前置调研备忘（Explore 产出，未实施）

供续作直接开工。核心结论 + 可复用/需改造/需新建清单：

## 现状
- TrimMonitorDialog（trim_numeric_dialog.py:157-222）= 纯 QLabel×2 文本 TC 窗，无帧画面/步进/commit-cancel/JKL/预后卷。Tool 窗不抢焦点三件套（Tool|DoesNotAcceptFocus + WA_ShowWithoutActivating + N15 失焦白名单 timeline_view.py:4896）已就绪且有测试。
- 帧预览现状：timeline_view trim_frame_preview_requested(int)（:739，roll 开始 :9590/拖拽 :9648 emit）→ edit_page:419 → timeline_scrub_frame_requested → request_scrub_preview → Program Viewer 单画面合成帧。缺 trim 结束信号（renderer 滞留 scrub 态）。

## 可复用（零/低改动）
1. 拖拽会话状态机（ROLLING/preview-only/Esc-cancel/单命令提交）+ _execute_command 单 Undo。
2. 命令路径：build_numeric_trim_command（timeline_ops.py:3939）/ build_linked_roll_command（linked_roll.py:57）/ RollTrimCommand.create（锁轨/钳制/linked Macro）。
3. EditPlaybackController set_playback_range+set_loop+play_with_speed（预/后卷=range+loop）。
4. AudioPlaybackManager.preview_scrub_audio（:4613，自带 generation 弃旧）——预听同源不新建。
5. generation 取消：_invalidate_render_generation_for_scrub（timeline_preview_renderer.py:8380）/ worker stale-drop / set_generation_id / GPU 同步。

## 需改造
6. TrimMonitorDialog QLabel×2 → 双帧控件（改自 scene_cut_preview_widget.py:333 PreviewImageWidget，QPainter 等比缩放现成）。
7. 新双帧信号（left/right 的 asset_path+source_frame）+ trim 结束信号（finish_scrub 对齐）。
8. 双画面帧供应：两个 per-asset latest-wins worker（复用 _SourceViewerDecodeWorker file_controller.py:35 / _CpuFallbackDecodeWorker source_viewer.py:1408 模式，直接取素材帧=真实保留边界，与最终输出一致；勿走时间线合成 renderer）。GPU 路径必须沿用 AUD-2803 所有权跟踪 + safe_release_gpu_frame + deferred-close（scene_cut_dialog.py:1489）。

## 需新建
9. 双帧 latest-only 调度（左右各一 latest、共一 UI generation token、cancel bump 两 worker）。
10. Trim 会话 JKL/预后卷状态机（最小=ROLLING/TRIMMING 态 timeline_view keyPressEvent 拦 J/K/L 为 cut±1 步进/暂停，仿 ,/. 步进消费模式 :4580/:4748，会话结束一次 RollTrimCommand commit）。
11. 验收测试：双帧=命令后 clip source 窗一致；拖拽 generation 无旧帧；取消 CommandStack 深度不变；提交 undo 恢复双片；工具窗期间 JKL/焦点行为。

## 既有测试锚点
tests/edit/test_trim_professional.py（TestTrimMonitorDialog:452 现只测文本）、test_round3_a_roll_focus.py（N15 焦点）、test_round4_interaction_commit.py、test_round4_linked_roll.py、test_fix_wd13g69_aud.py（AUD-21500 状态锁）。
