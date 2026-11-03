# Effects / LUT UI 交付与验证（2026-11-03）

本轮只完成 UI、交互和只读资源目录展示，不接入新的效果/LUT项目写入或渲染。后续统一施工入口为 [详细后端计划](../../../color_resources_backend_implementation_plan.md)：R00–R12为资源接线任务，N01–N17覆盖旧节点计划及最新实测仍未完成的事项。

## 已实现

- Effects 库/设置、分类折叠、搜索、收藏、列表/图块，拖到指定节点、双击/右键/按钮添加到选中节点。
- 按资源参数 schema 构建设置；实例启用、排序、重置、删除和参数编辑保留在该节点的 UI 草稿中。
- LUT 文件夹树、搜索、收藏、列表/缩略图、缩放、导入选择/会话库、刷新、目标节点、当前 LUT 与强度/重置/移除。
- MIME仅传版本、资源种类和ID；节点落点高亮，Source/Output/Mixer/Compound外壳/锁定节点拒绝。Timeline未实现时禁用资源投放。
- Gallery/LUT/Effects默认左侧标签组，Nodes独立；一次迁移保留已有浮动，之后不覆盖用户布局；Reset Layout恢复默认组。
- 两面板在360px浮动宽度检查正常，使用项目全局颜色/滑条/开关。旧兼容控件被放在隐藏容器，避免内部show调用弹出旧窗口。

参考本机 DaVinci Resolve Studio 20 的效果库/设置和分类条目结构。Topos保留浮动与标签组工作方式。本轮没有声称Resolve所有OFX算法、实时LUT悬停预览或全界面逐像素等价。

## 自动化

`regression.log`：**170 passed / 9.01s / EXIT 0**。覆盖新Effects/LUT/草稿/真实Qt拖放、节点选择与编辑、旧布局迁移/浮动恢复、图命令/持久化和已有宿主回归。`git diff --check`通过。

可复跑的完整命令见新计划§7，另包含 `tests/ui/test_color_resource_dock_layout.py` 验证一次迁移后自定义位置不被改回、已有浮动被保留、强制重置时归位。

## 原生应用

```bash
TOPOS_COLOR_GRAPH_RENDER=1 .venv/bin/python scripts/validate_color_node_media.py \
  tests/test_videos/测试素材01.mov --resources --workflow \
  --output docs/validation/color_resources/2026-11-03
```

最终 `media_preview_report.json`：**passed**，1920×1080 / 25fps / 124帧，源缩略图512×288、RGB std=66.625；从已有目录读取41个Effects与5个LUT。验证了左侧资源标签、Nodes独立、Qt drop、每节点草稿、Effects/LUT浮动，以及草稿阶段工程graph与engine插件未修改。随后已有节点后端的undo/redo、primary调节、断线重连、.topos保存重读和9种scope模式冒烟通过。

- `effects_library_floating.png`：360px浮动效果库。
- `effects_settings.png`：从真实插件metadata构建的设置。
- `lut_browser.png`：360px浮动LUT目录与强度。
- `lut_browser_docked.png` / `resources_studio.png`：停靠状态与整体布局。
- 其他素材、节点和scope截图保留在同目录。

**边界**：画面没有应用这些UI草稿；LUT图块仍为明确的“暂无预览”；节点缩略图仍是源素材fallback。插件编译、LUT导入持久化、候选/逐节点预览、正式undo/保存和全出口执行均依新计划实施。可选AI模型/外部FFmpeg相关启动告警不作为AI能力通过证据。持续4K压力与跨平台矩阵仍待执行。
