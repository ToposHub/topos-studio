# 剪辑／调色 Viewport 合流复查验证

日期：2026-11-09。参考：用户附件及本机 DaVinci Resolve Studio 20，最终只读复核 Color 页。用户要求播放区保留 Topos 原版较大的首末／逐帧按钮和橙色 Play/Pause，这是相对于 Resolve 外观的有意差异。

## 外观交付

- Edit：单个 Viewport dock，Source 左／Timeline 右的双视图，右上单入口切换；标题、信息栏、进度尺和底栏分别定位，原播放控件保持对象与信号链。
- Color：30px header、20px 灰尺／白播放头、36px footer，原大播放控件；Wipe／Highlight／Guides、时间线标题、时间码与右侧菜单重排，矢量图标／细线箭头统一。
- Color 的 Qt brush、timeline QPainter 背景与既有 GL clear 常量同步为 `#191919`，只调整留黑背景。未更改图像颜色、HDR 处理或渲染任务顺序。
- 原 View／Workspace／A/B／Capture 等操作从菜单继续可达；未接服务的新增动作禁用。Color／Fusion／image 模式切换恢复原控件与尺寸，状态栏避让播放栏，Fit 画面避开底部 overlay。

可复现截图和脚本：

- [Edit 布局记录](edit_viewport_20261109/README.md)
- [Color 布局记录](color_viewport_20261109/README.md)
- [Color 原尺寸控件细节](color_viewport_20261109/chrome_detail.png)

截图由真实 Qt 控件＋空画布替身生成，使用应用 `DARK_THEME`。没有用样例图片冒充媒体播放。Color 全局主题下的短标题隐藏／双箭头也经最终渲染修正。

## 小问题修复

1. Source 正播→倒放暂停既有正向音频；倒放 scrub 释放恢复原方向。
2. Loop 的包含式 Out 帧先呈现再回卷，一帧循环不在每个 fast tick 重定位音频。
3. hidden Source 不因未呈现 GPU pending 阻塞时钟，不再由迟到 fallback／watchdog 拉起周期 CPU 解码，show 仅刷新一次；显式 seek 保留。
4. Deliver 借用前检查实际 Program canvas 的全屏状态；真实 wrapper 类型回归覆盖原 stub 漏测。
5. Stop 接真实原位停止；Source Loop 菜单接既有 policy，状态回填且无素材禁用。
6. 版本化 Viewport 偏好接入本地配置，旧键迁移、活动对象保存、300ms debounce；Deliver 临时状态及单视图零分栏尺寸不污染偏好；拒绝版本 0／负数。
7. Source MIME codec 对 version／frame／fps 做严格类型与正数校验，拒绝 bool／float／坏分母／不完整 fps，不在 builder 中隐式截断或对坏结构抛异常。
8. SourceTimeDomain 范围校验仅接受非 bool 整数，拒绝浮点／NaN／字符串等；Source loop unsupported 提供明确原因。

## 自动测试

本轮合并回归：**955 passed，40.10s，无 skip／fail**。覆盖以下套件：

```sh
.venv/bin/python -m pytest \
  tests/edit/test_edit_viewport.py \
  tests/edit/test_program_viewer_toolbar.py \
  tests/edit/test_workspace_presets.py \
  tests/architecture/test_layout_store.py \
  tests/app/test_edit_viewport_integration.py \
  tests/app/test_edit_viewport_preferences_host.py \
  tests/app/test_dock_layout_controller_coverage.py \
  tests/app/test_color_timeline_clip_playback_mapping.py \
  tests/app/test_page_controller_coverage.py \
  tests/deliver/test_deliver_workspace_layout.py \
  tests/deliver/test_viewport_deliver_roundtrip.py \
  tests/edit/test_viewport_icons.py \
  tests/edit/test_viewport_scrubber.py \
  tests/edit/test_source_viewer_transform_contract.py \
  tests/edit/test_program_viewer_transform_contract.py \
  tests/edit/test_source_viewer_playback_chain.py \
  tests/edit/test_source_viewer_playback_backpressure.py \
  tests/edit/test_source_viewer_scrub_preview.py \
  tests/edit/test_source_viewer_gl_contract.py \
  tests/edit/test_edit_page_playback_preview.py \
  tests/color/test_color_viewport_chrome.py \
  tests/color/test_video_control_bar_scrub.py \
  tests/color/test_fix_wd4g23_aud.py \
  tests/color/test_fix_wd3g22_aud.py \
  tests/color/test_color_monitor_status_bar.py \
  tests/color/test_ab_reference_wipe.py \
  tests/color/test_canvas_clip_switch_preview.py \
  tests/composite/test_composite_viewport_safe_area.py \
  tests/app/test_color_page_audio_bridge.py \
  tests/app/test_color_entry_playback_and_timeline_refresh.py \
  tests/edit/test_source_transport_adaptation.py \
  tests/edit/test_source_hidden_scheduling.py \
  tests/edit/test_source_shutdown_lifecycle.py \
  tests/edit/test_source_present_gate.py \
  tests/edit/test_viewport_playback_arbiter.py \
  tests/edit/test_viewport_playback_coordinator.py \
  tests/edit/test_source_time_domain_mapping.py \
  tests/edit/test_source_time_domain.py \
  tests/edit/test_viewport_budget_policy.py \
  tests/edit/test_viewport_preferences.py \
  tests/edit/test_viewer_capabilities.py \
  tests/edit/test_source_drag_payload.py \
  tests/edit/test_source_drag_drop.py \
  tests/edit/test_match_frame.py \
  tests/edit/test_match_frame_view.py \
  -q --disable-warnings --maxfail=4
```

综合回归后的最终小修分别验证，不把重复运行算成额外独立覆盖：

- `test_source_time_domain.py`＋`test_viewer_capabilities.py`：**37 passed**，覆盖新增严格类型校验／unsupported reason。
- `test_color_viewport_chrome.py`＋`test_video_control_bar_scrub.py`：**20 passed**，覆盖最后的主题／短标题／图标／背景细节。

原生 macOS 启动检查：退出码 **0**。使用隔离配置，未覆盖用户配置：

```sh
TOPOS_CONFIG_PATH=/tmp/topos-viewport-review-startup/config.json \
TOPOS_TEST_MODE=1 .venv/bin/python -m src.main \
  --product studio --test-startup --no-startup-dialog
```

`git diff --check` 通过。启动日志中存在可选 AI 模型未安装等环境提示，不作为 Viewport 播放验收。

## 大问题与证据边界

剩余较大事项记录在[新复查计划](../plans/2026-11-09-viewport-integration-review-followup.md)，包括全入口播放仲裁、时间域／预算／capability 的真实消费者、Source 拖出生产端与工程关闭生命周期。

额外只读复现确认：Insert 模式忽略 stream intent（video 插出 video＋audio，audio 插出两条 audio clip）；先新建轨道再插入失败会留下空轨及 Undo entry。这些需统一编辑事务，未以局部 UI 补偿修改模型。

Color 后端尚未实施，继续按[原 Color 计划](../plans/2026-11-09-color-viewport-backend-plan.md)推进。

以上测试证明本轮控件行为、接线兼容和特定领域边界，**不证明实际媒体的整页互斥音频、4K/HDR 性能或 GPU 生命周期全部验收通过**。尚未获得与 Resolve 整窗逐像素差分为零的证据，系统字体／DPI 与原大播放控件的产品差异仍存在。
