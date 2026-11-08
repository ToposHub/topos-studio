# 时间线 UI 复核工程

收尾日期：2026-11-08。

`resolve_ui_review.topos` 包含仓库内两份真实素材、四条 A/V 轨道及 20 个片段，用于复核缩略图、波形、轨道头、选择、修剪、缩放和菜单。两处素材索引均保存相对路径，保持仓库结构即可打开。

独立预览：

```bash
.venv/bin/python tools/preview_resolve_timeline.py
```

重新生成工程：

```bash
.venv/bin/python tools/preview_resolve_timeline.py --save-project artifacts/timeline_resolve_ui/resolve_ui_review.topos --project-only
```

此工具使用独立 review 工程，不修改运行中的编辑器工程，不伪造电平数据。预览显示当前源代码；已经运行的主程序需重启后加载新 UI。

验证结果：相关回归 334 通过、1 跳过；最终新增 UI/交互测试 31 通过；编译及 diff 检查通过；工程重开后全部真实素材可解析。

新增 Replace、源目的轨路由、Keyframe Tray、Voiceover、Flag 为 UI 请求入口，待宿主消费。Position Lock 当前是部分鼠标手势保护。详细状态、实施步骤和验收条件见 [后端接线计划](../../docs/plans/timeline_resolve_backend_wiring_2026-11-07.md)。
