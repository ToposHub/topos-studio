# 用户截图回归：拖不动、光标闪烁、刻度与工具栏

2026-11-08，基于用户18:59截图追加验证。以下是UI缺陷，已直接修复；不添加后端修复任务。

## 根因与修复

| 问题 | 已确认原因 | 修复 |
|---|---|---|
| 首次按下片段却拖不动 | selection_changed同步重建属性面板。`_make_numeric_slider`创建无parent的ToposClickableSlider，在放入layout之前`setVisible(True)`；macOS将其显示成顶层窗口并抢焦点，TimelineView按正常失焦保护取消刚开始的拖动 | 数值工厂构造时指定`parent=self`并移除提前show；效果参数工厂指定`parent=row`，XY工厂同样明确归属。保留真正FocusOut/ApplicationInactive取消保护 |
| 箭头与抓手反复切换 | view每次mouseMove设Arrow，随后scene item设置hand/trim，view与item争抢光标 | clip/header/subtitle/transition及任何`hasCursor()`的scene item管理自己的光标；view只处理真正的播放头、splitter、轨高边界与空白 |
| 时间刻度像重复叠画 | 25fps常见缩放下，minor=10、medium=62、major=125，步长不整除，另有minor/medium重复draw同一位置 | 子刻度必须整除主刻度；medium是minor整数倍。每个tick按优先级绘制一次，保留原时间码格式与主网格 |
| 缩放滑块浅色底块 | 独立预览曾只使用timeline_style，未覆盖真实宿主DARK_THEME中的全部slider子控件 | slider本体透明；明确groove、handle、add-page、sub-page样式；预览工具也加载真实全局主题 |
| 工具间粗分隔线 | 9px原生VLine在全局主题下绘出宽/立体线 | 改为无原生frame的1px低对比分隔线 |
| 横向滚动条浅色区 | 宿主样式/原生page子控件未被局部覆盖 | 横向bar、handle、page与line子控件统一深色/透明，避免浅色或默认纹理 |

原生捕获到的取消调用链（修复前）：

```text
ClipItem.mousePress -> TimelineView._on_clip_clicked
 -> set_selection -> selection_changed
 -> EditControllerMixin._on_timeline_selection_changed
 -> EditPropertiesPanel.set_clips -> set_clip
 -> _build_transform_section -> _build_xy_param_row
 -> _make_numeric_slider -> slider.setVisible(True)
 -> TimelineView.focusOutEvent -> _cancel_current_interaction
```

此时应用仍为ApplicationActive，activeWindow却变成ToposClickableSlider。不是轨道锁，也不是要放宽真正切窗的取消保护。最初offscreen探针也遇到ApplicationInactive；那是另一条真实失活保护，未将其当作产品缺陷修复。最终以macOS原生窗口调用栈确认上述factory问题。

## 实际验证

在隔离的QMainWindow中接入真实隐藏Properties/Info docks、真实controller选择回调与用户`2K_h264.mp4`素材，用原生鼠标拖动：

- 修复前：选择第二段后仍停在550帧；日志出现属性滑块顶层激活、FocusOut、CANCEL。
- 修复后：第二段视频/链接音频550→610，首段保持0；日志只有一次COMMIT，没有属性窗口抢焦点。
- Cmd+Z后内容恢复：第二段550，第三段900，其他源窗/内容由host回归完整比较；revision按正常规则递增。
- 原生窗口检查确认清晰单列刻度、细分隔线、透明滑块本体与深色横向滚动条。电平表保留。
- 运行中的用户原工程曾作一次可撤销移动检查，随后已Cmd+Z还原内容；没有保存或重启该主程序。修复需保存工程后重启加载。

组合回归：**169 passed / 15.07s**。涵盖host拖动、属性布局、光标/刻度/主题、联动、trim、fade、Esc、真实切窗取消、缩放和Tray。之后增加效果工厂顶层Show观测，并补scene `hasCursor()`通用归属；最终专项 **28 passed / 6.70s**，命令如下（与组合重叠，不相加）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_timeline_host_drag_regression.py tests/edit/test_timeline_pointer_and_chrome.py
```

产品改动位于UI层：`edit_properties_panel.py`、`timeline_view.py`、`timeline_items.py`、`timeline_panel.py`。不改command、内容schema、渲染或音频逻辑。
