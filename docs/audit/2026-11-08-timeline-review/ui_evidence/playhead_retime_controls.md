# 播放头与变速控件（2026-11-08 追加）

用户范围：直接实现 UI/交互；领域模型、速度积分、渲染/音频以及高级接线只更新计划。保留时间线配色与现有电平表。没有关闭或重启用户原来的 Topos 窗口。

## 本机 Resolve Studio 20 观察

参考窗口：`Topos KF Parity Audit 2026-11-07` / Timeline 3。只在独立参考项目观察，速度拖拽后 Cmd+Z 恢复。

- 播放头：扁平珊瑚红五边形指针，窄细竖线贯穿轨道；没有高光渐变与悬浮时间码泡，时间码位于标尺左侧。
- 选中 V2 素材后 Cmd+R：顶部 `× Speed Change` 标题，下方方向箭头，底部 `100%` 和向下三角。
- 原生顶部右端从 x1378 拖到 x1450：片段变长，百分比显示 84%，链接音频时长同步；撤销恢复 100%。这是确认的右端拖动，不能据此认定所有内部速度点把手都已验证。
- 根据用户追加提醒再次开关控件：V2 上边从约 y951 上移至 y927，底边约 y1061 不变；标题展开增加约 24 像素，下方 V1/A1/A2 坐标保持。关闭后恢复。
- 本次 CUA 对原生 Resolve 底部菜单的点击未捕获到展开菜单；菜单条目使用下述官方手册作为补充依据，不能宣称原生菜单每个动作都已执行。

官方补充：[Resolve 18 Editor's Guide，PDF 第311页](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-18-Editors-Guide.pdf)，以及 [Resolve 15 Definitive Guide，第179页](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-15-Definitive-Guide.pdf)。两者支持 retime 百分比菜单与 speed-change bar 工作方式，属于旧版书面参照，并非本机20所有选项的逐像素验收。

## 本轮实现

| 区域 | 实现与验证边界 |
|---|---|
| 播放头 | 14×12 逻辑像素、平面珊瑚红指针，1px cosmetic 竖线；去除拖动浮泡；继续使用既有 ruler scrubbing、corner timecode 与 scene foreground 覆盖层 |
| 顶部变速拖动 | 标题与箭头带独立 `retime_stretch` 命中，排除左上关闭按钮；实时改变本地宽度与百分比；松手通过现有速度命令一次提交，源 In/源长度保持；按 Esc/关闭/换工具等取消恢复 |
| 混 fps 与参数保留 | UI 适配器使用 `old_speed × old_timeline_duration / requested_timeline_duration`，保留 reverse/interpolation/pitch；48fps 源200帧、24fps 序列100帧拉到50帧得到2x，而非旧 rate-stretch 的4x。没有更改旧工具中的后端计算，B18继续待修 |
| 高度展开 | 仅控件已展开的 clip 高24px，同轨其他 clip 底部对齐并保持原高；同轨开多个不会累加轨高；关闭最后一个后恢复。仅投影临时 layout state，不写入工程高度、不产生 undo |
| 几何稳定性 | clip、track header 和 roll handle 的高度变化先调用 `prepareGeometryChange()`，防止场景索引保留旧矩形；追加的共享轨测试曾稳定触发 Qt 重绘崩溃，修正后通过 |
| 底部百分比 | 采用文本加本地矢量三角，显示整数百分比；整个底部信息带可点击；在 release 后打开菜单，避免开菜单的同次 mouse release 将其立刻关闭；菜单使用 view 作为 parent，退出后 deleteLater |
| 接缝命中 | roll hit shape 避开 retime 顶部带与底部菜单带；同轨未展开片段的 fade 区也受保护，保持原有正常滚动修剪 |
| 分组菜单 | Add/Clear Speed Point、Change Speed（10/25/50/75/100/110/150/200/400/800% 与自定义）、Reset to100%、Freeze Frame、Reverse Segment、Rewind、Speed Ramp、Reset Clip、Pitch Correction；中英本地化。单速度段 reverse/pitch/reset 重用现有命令；百分比/点与平滑功能沿既有信号 |
| 高级能力 | clear 单点、freeze、rewind、0速 ramp，以及曲线/冻结/带动画成员的顶部 stretch，发出明确高级草稿请求与“不支持、片段未改变”提示；不会伪造已生效。后端依赖 B14/B15/B17/B18 |

## Topos 原生窗口检查

通过 `tools/preview_resolve_timeline.py` 启动独立 app bundle `com.topos.timeline-review`，加载 tests/test_videos 中真实素材，不保存或改写原项目。

- 新播放头外观在预览窗口可见。
- Cmd+R 显示控件；拖动顶部中部片段从100%变为约124%，画面宽度缩短，链接音频松手后同步缩短。
- 展开后 V1 片段顶部约从 y309 到 y285，底边约y389不变；同轨其余片段维持原高，上方轨道移出所需空间，下方音频保持。
- 最终代码再次检查 V2 独立片段：顶部右端从 x506 拖到 x547，显示整数77%，方向箭头变为金黄色；A2链接音频松手同步延长；Cmd+Z恢复原速/宽度，点击左上 `×` 收起，片段与轨道恢复原高。
- 点击左上 `×` 关闭，片段和轨道恢复原高。
- CUA 的 Topos popup 截图/AX同样未捕获到展开后的菜单。本轮补充真实 Qt popup 测试：点击 viewport 的底部带，确认 `QApplication.activePopupWidget()` 为实际 `retimeSegmentMenu`，在真实 QAction 矩形点击 Reverse，核对模型和一次历史提交。该用例没有替换 QMenu.exec。

## 后端接线要求

见新计划 [B14 高级速度契约](../../../plans/timeline_resolve_parity_review_2026-11-08.md)。本轮没有创建 MoveSpeedPointCommand、没有改变 schema 或 renderer，也没有把 Rewind 简化成倒放、把清单点简化成清全部点。linked 音频在顶部 stretch 拖动中的实时投影仍需与最终后端 timing plan 统一；本轮原生验收仅确认松手后的联动。

## 最终验证

- `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` 加下列14个测试文件：**268 passed / 17.29s**。含本轮17个 retime 事件测试、2个播放头测试，以及原有拖拽、淡变/修剪、roll、锁、速度、布局、字幕与真实隐藏属性面板回归。
- [最终原始输出](playhead_retime_verification.txt)。测试素材缺失日志来自已有故意使用不存在路径的隔离fixture，不是用户素材加载失败。
- `compileall`、中英locale JSON解析与 `git diff --check` 均通过。
- 组合回归中发现的光标失败来自 `test_initial_timeline_load_resets_after_hidden_view_is_shown` 未把已显示窗口登记到qtbot清理；已登记，原悬停断言保留，没有放宽产品的失焦取消策略。
- 音频绘制额外复制 `option.exposedRect` 为自有QRectF，防止借用SIP对象在嵌套重绘/缓存场景中失效；此前组合回归捕获到该对象已删除异常，修正后的整组运行通过。

测试文件：

```text
tests/edit/test_timeline_retime_controls.py
tests/edit/test_timeline_playhead_style.py
tests/edit/test_timeline_clip_affordance_gestures.py
tests/edit/test_timeline_view_layout.py
tests/edit/test_resolve_timeline_interactions.py
tests/edit/test_timeline_host_drag_regression.py
tests/edit/test_timeline_pointer_and_chrome.py
tests/edit/test_change_clip_speed_dialog.py
tests/edit/test_timeline_resolve_ui_review.py
tests/edit/test_trim_professional.py
tests/edit/test_round4_linked_roll.py
tests/edit/test_round3_a_roll_focus.py
tests/edit/test_subtitle_layout_persistence.py
tests/edit/test_timeline_clip_speed.py
```
