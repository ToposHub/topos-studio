# Edit Viewport 前端验证记录

日期：2026-11-09。参考：本机 DaVinci Resolve Studio 20 的 Edit 页，实际观察单／双视图切换并恢复原单视图状态。

## 本轮完成

- Program、Source 合并为一个 Viewport 停靠面板；保留 `dockProgramViewer` 兼容旧 Qt 布局。
- 右上单／双视图按钮；单视图 Source Clip／Timeline 切换，双视图左 Source、右 Timeline，可拖动分栏。
- 30px 顶部信息栏，标题独立按整个 monitor 宽度居中；Source Clip／Source Tape／Timeline、缩放、时长、当前时间码与右侧显示工具分组排列。
- 右上沿用达芬奇的单个模式入口：当前单视图时显示 Dual Viewer Mode，双视图时显示 Single Viewer Mode；保留原两个动作接口。
- 34px 底部控制栏，中央控制独立按整行居中，左右工具不会推偏播放区。按用户最新要求恢复原首帧／上一帧／橙色播放暂停／下一帧／末帧按钮：播放为 32×28px、18px 图标，其余为 28×28px、15px 图标，间距 3px；Program 另保留原 Loop。扩展的 Stop／Reverse 不占用原五键位置。
- 24px 自绘进度条，细暗轨道、红色空心旗形播放头、灰色起末／In/Out 手柄；不将已播放区域整段涂亮。Source 复用原 slider，并保留 pressed→value→released 生命周期。
- 显示与比较工具使用 code-native QIconEngine 图标，20×20 设计网格、1.1px 轮廓，按设备 DPR 绘制，统一常态／悬停／禁用／选中颜色。主播放按钮明确使用原实心图标与橙色 Play/Pause；矢量工具已抽到 shared UI，供调色页复用。
- 沿用原按钮、QLabel、时间线 selector 和预览组件；单／双切换不重新创建或搬移 OpenGL 画布。
- Source In/Out、Timeline Loop、缩放、参考线、变换、已有播放控制保留原信号链。Program scrubber 接原播放控制器 seek／frame_changed，回填不产生重复 seek。
- 素材双击／时间线双击打开 Source；点击时间线激活 Timeline；空格按当前焦点路由。Timeline 切换后，Program 的画布快捷键仍获得焦点。
- 单／双模式、活动监视器和分栏尺寸通过版本化配置保存，300ms 合并写盘并兼容旧键；Deliver 临时布局不写偏好；旧可见面板 ID 合并映射；旧 Source-only 布局首次展示 Viewport 后尊重用户关闭操作。
- Deliver 借用并归还原 Program 实例，返回时不替换整个 Viewport 容器；交付页隐藏视图模式按钮。
- Source Loop 菜单已接真实策略，Stop 接原位停止；Reverse、Match Frame、Timeline Mark In/Out、Proxy Handling、Bypass、Source Tape 等尚未完整接线的入口保持禁用。后端域件状态见[原计划](../../plans/2026-11-09-edit-viewport-backend-plan.md)，生产调用链剩余缺口见[新复查计划](../../plans/2026-11-09-viewport-integration-review-followup.md)。

## 自动检查

初轮以下聚焦回归合计 **540 passed**，无 skip／fail；最新剪辑／调色／后端复查的合并结果见[复查验证记录](../viewport_review_20261109.md)。

```bash
.venv/bin/python -m pytest \
  tests/edit/test_edit_viewport.py \
  tests/edit/test_program_viewer_toolbar.py \
  tests/edit/test_workspace_presets.py \
  tests/architecture/test_layout_store.py \
  tests/app/test_edit_viewport_integration.py \
  tests/app/test_dock_layout_controller_coverage.py \
  tests/app/test_color_timeline_clip_playback_mapping.py \
  tests/app/test_page_controller_coverage.py \
  tests/deliver/test_deliver_workspace_layout.py \
  tests/edit/test_viewport_icons.py \
  tests/edit/test_viewport_scrubber.py \
  tests/edit/test_source_viewer_transform_contract.py \
  tests/edit/test_program_viewer_transform_contract.py \
  tests/edit/test_source_viewer_playback_chain.py \
  tests/edit/test_source_viewer_playback_backpressure.py \
  tests/edit/test_source_viewer_scrub_preview.py \
  tests/edit/test_source_viewer_gl_contract.py \
  tests/edit/test_edit_page_playback_preview.py \
  -q --disable-warnings --maxfail=3
```

原生 macOS Qt 启动检查退出码 **0**，使用独立临时配置：

```bash
TOPOS_CONFIG_PATH=/tmp/topos-viewport-refined-startup/config.json \
TOPOS_TEST_MODE=1 .venv/bin/python -m src.main \
  --product studio --test-startup --no-startup-dialog
```

该环境日志另有缩略图 FFmpeg 路径未找到、可选 AI 模型包未安装提示；本轮未检查这些既有能力。完整应用在 offscreen 平台不能正常创建 OpenGL／QtWebEngine 窗口，故没有将其当作 GPU 通过证据；原生启动检查和离屏界面布局检查分开执行。

`git diff --check` 通过。

## 布局图

以下图片来自**真实新 header/footer、原 toolbar 与 transport 控件**，画布是空白测试替身，名称／时间码是固定示例数据。它们验证前端布局，不证明媒体解码、HDR 或播放性能。

[控件细节预览（省略画面区域）](chrome_preview.png)。

| 尺寸 | 单视图 | 双视图 |
|---|---|---|
| 1364×840（与用户附件相同） | [单视图](1364x840_single.png) | [双视图](1364x840_dual.png) |
| 1280×720 | [单视图](1280x720_single.png) | [双视图](1280x720_dual.png) |
| 800×480 | [单视图](800x480_single.png) | [双视图](800x480_dual.png) |
| 窄屏 | [320×300 单视图](320x300_single.png) | [640×400 双视图](640x400_dual.png) |

全部实际渲染尺寸与请求尺寸相同；使用应用自己的 `DARK_THEME` 检查容器透明样式。窄屏不将主窗口强行撑宽，模式按钮、时间码和必要播放按钮保持可见。可选显示工具收进更多菜单。

几何测试逐项检查标题中心、播放区中心、单一模式按钮、280／300／320／460／700px pane 的工具不重叠，以及换源后名称／时间码／缩放回填。禁用、隐藏和丢失鼠标捕获的取消测试同时覆盖 Program scrubber 与原 SourceSlider。

本记录验证前端几何约束与真实 Qt 控件渲染，没有将不同系统字体、DPI 和参考应用的整窗截图差分声明为零。

重建图片：

```bash
.venv/bin/python docs/validation/edit_viewport_20261109/render_preview.py
```

## 未完成的验收范围

电脑控制工具未获准操作 Topos 的 Python 宿主窗口，实际窗口的点击验收未完成。本轮完成原生启动、Qt 控件行为测试和离屏布局检查；没有宣称通过真实媒体、4K/HDR、双路音频仲裁、隐藏视图 GPU 槽位调度或所有跨平台交互验收。这些工作及失败／取消／关闭路径已在后端计划中单列。
