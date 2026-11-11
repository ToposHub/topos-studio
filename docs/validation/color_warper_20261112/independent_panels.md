# Warper / Mixer 独立面板 · 2026-11-12

用户确认将两工具拆开后，移除了Color Mapping组合控件。主窗口继续创建原有WarperPanel和MixerPanel实例、复用原有调色与reset信号，仅改变UI停靠结构。

| 面板 | Registry ID | 主窗口属性 | Qt持久化名称 |
| --- | --- | --- | --- |
| Warper | color.warper | dock_warper | dockColorMapping（有意保留旧名称以精确保留布局） |
| Mixer | color.mixer | dock_mixer | dockMixer |

两者均出现在调色面板菜单，图片/视频模式均可访问；可以独立显示、关闭、移动、浮动或同其他面板组合。默认布局将两者作为独立dock页签放在调色工具区。离开Color页时隐藏，返回时按已保存状态恢复。Warper展开浮窗也会在离开Color页时自动收回dock，保留模式、网格和选择状态。

## 布局兼容策略

- Warper直接使用原Color Mapping的持久化名称，因此启动、切页、命名布局、出厂布局都通过Qt原生restoreState继承原区域、浮动、大小和隐藏状态，不改写Qt二进制数据。保留名称不代表保留组合容器。
- 布局迁移键`color_mapping_dock_layout_version`从1升至2。首次升级只安置新增的Mixer，Warper、Curves和Effects不被重新布局或强制显示；之后尊重用户对两个独立面板的设置。
- Warper处于浮窗时，Mixer默认放底部独立dock，避免破坏原浮窗。
- LayoutStore及自定义工作区中的旧`color.mapping`可见性展开为`color.warper`与`color.mixer`。原容器的floating元数据归Warper，已有Warper元数据优先，不复制为两个重叠浮窗。
- 内置Secondary工作区和Color/Studio默认面板列表已更新。已有独立Warper/Mixer工作区只打开指定工具，不再顺带打开另一工具。
- Mixer既有处理、Warper既有HSV处理和新模式UI-only边界均不变；后端计划仍全部待实施。
- 命名布局恢复完成后重新应用当前页面的可见性约束；旧Color-only布局在Edit/Composite加载时不会暴露两个调色面板，也不会覆盖其保存的Color页面快照。

## 回归结果

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_color_mapping_panel.py \
  tests/color/test_workspace_presets.py \
  tests/color/test_warper_resolve_ui.py \
  tests/architecture/test_layout_store.py \
  tests/app/test_dock_layout_controller_coverage.py \
  tests/app/test_page_controller_coverage.py \
  tests/ui/test_factory_default_layout.py \
  tests/shared/test_ef17_aud.py::TestAud25100PackageDocstring -q
```

**315 passed in 12.84s**（Qt offscreen）。覆盖独立注册/工作模式、旧profile和preset迁移、Qt浮窗隐藏状态保留、首次迁移/二次不重排、跨页往返、出厂/命名布局、Edit/Composite加载Color-only旧布局、展开Warper跨页自动收回且保留选择、菜单、标题栏、布局保存及Warper交互。没有运行完整MainWindow端到端或新调色后端验收。
