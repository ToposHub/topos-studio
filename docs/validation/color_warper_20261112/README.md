# Color Warper UI 验证 · 2026-11-12

参考：用户提供的877×425 Hue–Saturation截图，以及本机已打开的DaVinci Resolve Studio 20三种模式。仅实现UI、画布和局部交互；没有修改渲染器、shader、图层模型或工程格式。

## 已完成范围

- 三种模式和独立工具页：Hue–Saturation六角网格；Chroma–Luma双网格/轴角度；Chroma Warp笔画与固定点。
- 右侧选择/取消选择、绘制选择、Pin、Pull/Push、falloff、反选、选点转Pin、列/环/行/全选、清选择/固定点。
- Range双手柄、Auto Lock、1/2 Points、CL自动固定方向、数值混合态、逐分量Smooth/Reset。平滑Hue不会改变Sat，反之亦然。
- Chroma Warp普通/点到点笔画、起点/终点/整条笔画拖动、固定点、删除、每对象参数、Esc取消。
- 底部独立分辨率与Link、HSV/HSL/HSP、展开/收起、窄面板Axis Angle换行、中英文。背景、按钮、下拉和数值框复用Topos样式和控件。

877×425英文基准几何：标题栏35px，Tools列253px，左侧画布624×343px，底栏47px；六角网格水平顶点朝向与参考一致。中文列会按文字所需宽度扩展。全局控件的圆角、橙色激活态、数值框、开关和字体与Resolve存在有意差异。

## 测试结果

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_warper_resolve_ui.py \
  tests/color/test_color_mapping_panel.py \
  tests/color/test_fix_wd4g23_aud.py::TestAud25814WarperPanelSetParamsResetGuard \
  tests/shared/test_ef14_aud.py::TestAud38209Padding -q
```

**22 passed in 6.99s**。Python3.12.14、PyQt6 6.11.0、Qt6.11.2、offscreen。另通过compileall与git diff --check；独立Review完成两轮，发现的信号隔离、平滑分量、Pin参数、取消恢复、手势粒度、全局重置及大画布缩放问题已修复。此验证不包含完整主窗口端到端操作或图像处理质量验收。

## 视觉复核与复现

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/color_warper_20261112/render_ui.py
```

脚本使用真实Qt控件生成截图；`*_selected.png`中的点/笔画是明确的UI交互夹具，不是视频调色结果。

| 截图 | 检查内容 |
| --- | --- |
| [Hue–Saturation](hue_saturation.png) | 六角网格、底部工具栏、Tools、空选择禁用状态 |
| [HS选中状态](hue_saturation_selected.png) | 多选、Pin、混合数值 |
| [Chroma–Luma](chroma_luma.png) | 并列网格、Grid1/2、Axis Angle、方向按钮 |
| [640px窄面板](chroma_luma_narrow.png) | Axis Angle第二行，控件保持可见 |
| [Chroma Warp](chroma_warp.png) | 空状态、笔画工具和Pin参数 |
| [CW选中状态](chroma_warp_selected.png) | 笔画端点/箭头、固定点、启用的对象参数 |
| [展开状态](expanded.png) | 同一个控件树重挂到1122×648浮窗，状态保留 |
| [中文](hue_saturation_zh.png) | 翻译、中文列宽、共享输入控件 |

已逐图检查布局。未宣称逐像素相同：图标为自行绘制的矢量图标；色场为展示背景，未经HSP/色度科学标定；未实测的菜单/数值域/衰减算法须按后端计划CW-B00再冻结。

## 用户指定素材

`render_ui.py`通过PyAV逐个打开并解码首帧。实际结果归档于 [media_probe.json](media_probe.json)：

| 素材（tests/test_videos） | 解码器 | 实际分辨率 | 解码像素格式 |
| --- | --- | --- | --- |
| 2K_h264.mp4 | h264 | 1920×1080 | yuv420p |
| 2K_prores422.mov | prores | 1920×1080 | yuv422p10le |
| 2K_DNxHR HQX 10bit.mov | dnxhd | 1920×1080 | yuv422p10le |

此项验证素材可用，不代表新模式已处理视频；文件名“2K”不作为4K性能或HDR证据。

## 后端边界

宿主回填legacy参数（含None）时显示HSV；只有当前HSV方阵的Hue/Sat编辑继续走现有信号。HSP/HSL、非方阵、HS Luma、CL、CW只保存当前UI会话状态并发独立展示通知，尚无新图像处理/持久化/导出。切换到新模式不会把坐标误交给旧HSV后端。全局reset_all保留旧调用方的清除效果语义。

没有伪造视频密度云；画布预留 `set_density_image` 接收真实异步采样图。真实云图、viewer采色、颜色空间转换、求解、权威模型、undo、持久化、GPU和导出均在 [后端实施计划](../../plans/2026-11-12-color-warper-backend-plan.md)，每个阶段含具体步骤与验证方法。
