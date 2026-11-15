# Deliver UI 对照验证

2026-11-16，参照已打开的 DaVinci Resolve Deliver 页面调整设置栏、队列与工作区比例。
使用项目全局配色和选择框，保留既有导出草稿、预检、队列控制及共享 Viewer/Timeline。

- `deliver_zh.png`：1580 × 1020 中文完整工作区。
- `deliver_en.png`：1580 × 1020 英文完整工作区。
- `deliver_zh_compact.png`：1240 × 620，展开高级/字幕设置，参数滚动且底栏固定。
- `audio_zh.png`、`file_zh.png`：1240 × 720 音频与文件页签。
- `deliver_boundaries_zh.png`、`deliver_boundaries_compact_zh.png`：后续面板边界修复，
  分别为 1580 × 1020 和 1240 × 620；面板底色直接贴合 1px 黑线。

截图使用仓库真实 H.264/HEVC 素材、真实共享面板和待渲染作业。静态预览帧提前解码，
该预览工具没有连接完整播放控制器，不能用于验证连续播放或导出性能。
配置、队列及输出位置使用临时目录，关闭后清理，不修改编辑器工程和用户队列。

```bash
.venv/bin/python tools/preview_deliver_ui.py
.venv/bin/python tools/preview_deliver_ui.py --locale zh_CN --screenshot /tmp/deliver.png
.venv/bin/python tools/preview_deliver_ui.py --locale zh_CN --size 1240x620 --advanced
```

相关回归 86 项通过，覆盖设置加载/构建、多预设切换、自定义预设重载、固定码率估算、
队列取消/完成操作、浮动归位，以及 Edit/Deliver 面板往返；编译和 `git diff --check` 通过。

边界修复后合并运行共享 divider/splitter 与上述 Deliver 回归：121 passed、1 skipped。
新增异色 pane/header 的 DPR 1/2 像素验证、横纵 T/X 交点、原生边缘拖动与缩放检查。
跳过项为既有 macOS tabified-dock 跨平台样式重抛光测试，保留原跳过原因。
