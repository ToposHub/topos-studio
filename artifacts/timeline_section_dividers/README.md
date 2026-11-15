# 时间线功能区与固定滚动条

2026-11-16，参照用户提供的 DaVinci Resolve 时间线截图。

- `timeline.png`：1420 × 660，真实共享 TimelinePanel 与仓库 H.264/HEVC 素材。
- `timeline_compact.png`：1100 × 520，相同面板的窄窗口审阅。
- `timeline_scrollbars_fixed.png`：1420 × 660，视频/音频各增加 4 条空轨，验证溢出滑条。

工具栏、时间码/标尺、轨道头和音视频分隔条使用 `C.PANEL_DIVIDER`。
逐轨黑线仅出现在轨道头，右侧编辑区保持连续背景；保留片段选中轮廓与播放头。
溢出滑条是 view 的子控件，与 viewport 同级，避免 Qt 滚动画布时一起平移。
8px 中性滑条映射并锁定在 viewport 右缘及对应分区内。
未改变轨高、编辑命令或工程数据；审阅工具使用临时配置目录。

```bash
.venv/bin/python tools/preview_resolve_timeline.py --screenshot /tmp/timeline.png
.venv/bin/python tools/preview_resolve_timeline.py --size 1100x520 --screenshot /tmp/timeline-compact.png
.venv/bin/python tools/preview_resolve_timeline.py --extra-tracks 4 --screenshot /tmp/timeline-overflow.png
```

186 项 focused 回归通过：10 项 DPR 1/2 合成像素检查，包含满高片段、
选中边框、播放头、字幕/锁定轨、横向与各域滚动、轨高/窗口变化及空时间线；
另有 5 项固定滑条回归，覆盖原生水平滚动、缩放、分区比例、宿主移动/缩放、
三域独立滑块拖动及无溢出隐藏。
其余覆盖已有工具栏、时间码、鼠标编辑、布局、窄条重绘和 Edit/Deliver 往返。
编译与 `git diff --check` 通过。本次未测导出性能或完整实时播放性能。
