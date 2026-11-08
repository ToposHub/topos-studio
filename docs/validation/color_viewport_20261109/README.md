# Color Viewport 前端外观复核（2026-11-09）

本目录以本机 DaVinci Resolve Color 页的实际窗口为布局参考：30 px 顶栏、
20 px 灰色进度尺、36 px 底部操作栏，画布与 letterbox 背景为 `#191919`。
按用户要求保留 Topos 原有大播放控件：首／上一帧／下一帧／末／循环按钮
28×28 px、图标 15 px；橙色播放按钮 32×28 px、图标 18 px。

## 可复现预览

在项目的 Python 环境运行：

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/color_viewport_20261109/render_preview.py
```

脚本从自身位置寻找项目，输出文件写回本目录，不包含机器绝对路径。使用
应用真实的 `DARK_THEME`、真实 `ViewportMenuBar` / `VideoControlBar` 及
`ColorViewportChrome`，画布采用 `tests/color/test_color_viewport_chrome.py`
中的无 GL `QGraphicsView` 替身。不会创建渲染器、解码器或载入素材。

| 文件 | 检查范围 |
|---|---|
| `color_viewport_1000x600.png` | 完整顶栏、居中时间线名称、全套可见工具与原播放控件 |
| `color_viewport_700x480.png` | 中等宽度、两侧工具与中心名称不重叠 |
| `color_viewport_500x350.png` | 窄窗口折叠额外工具，保留菜单入口与原播放控件 |
| `chrome_detail.png` | 原尺寸顶栏与进度尺／底栏细条，未放大或重采样 |

本轮细节修正：Zoom 文字右侧细线箭头；Guides／Proxy／Resolution 矢量
下拉箭头；清除全局主题造成的时间线名称双箭头；Wipe／Highlight 切换后
继续保留细线矢量图标；底栏分组显式透明，避免全局样式引入灰色方块。
未接后端的按钮保持禁用并说明“此版本暂不支持”，原 View、Guides、
Workspace、Capture、A/B、缩放及快捷键操作仍可达。

## 背景与画面边界

Canvas 的 Qt 背景刷、时间线 QPainter 背景和 `GLCanvasViewport` 的已有
clear 背景常量同步为 `#191919`。仅改变素材外侧的画布／letterbox 外观，
未改动 GL 调用顺序、任务队列、纹理内容、HDR 数值或调色／输出管线。
视频 Fit 会避开覆盖在底部的 56 px 进度尺与播放栏；图片模式不预留播放栏。

## 测试与证据边界

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_color_viewport_chrome.py \
  tests/color/test_video_control_bar_scrub.py -q
```

结果：**20 passed**。覆盖原控件身份与信号、Color／Fusion／图片模式往返、
实时拖动时码、Zoom 更新、浮动面板宿主、状态栏避让、完整画面 Fit、
背景色一致性、工具切换后的图标保持，以及全局主题下单个短时间线名称的
可见性与精确居中（1000／700／500 px）。

这些图片是**空画布 UI 预览**，不是实际素材播放或 HDR/GPU 验收截图。
未证明真实 4K 播放性能、音画同步、色彩准确性或与 Resolve 的逐像素完全
一致；系统字体、DPI 与 Qt 平台绘制仍可能影响最终显示。保留较大的橙色
播放按钮是明确的产品选择，其尺寸与 Resolve 原版存在有意差异。
