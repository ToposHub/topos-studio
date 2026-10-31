# Motion Graphic 关键帧时间线（2026-08-22）

## 结论

Motion Graphic 编辑器的底部时间线已从"18px 播放滑块"升级为**真正的关键帧时间线面板**，采用合成页 Keyframes 的交互模式（左侧元素/属性行树 + 右侧关键帧轨道 + 标尺/播放头），而非剪辑软件的片段轨道时间线。Motion Graphic 的核心是"元素随时间变化"，不是片段排列。

## 布局

```
Motion Graphic Editor
├─ Toolbar（不变）
├─ Preview | Parameters（Layers 仍在 Parameters 内负责结构管理）
└─ 底部 QFrame（垂直 splitter 可拖高度，View 菜单 "Keyframes Timeline" 开关）
   ├─ PageNavigationBar + SceneTimeline   ← 保留：播放/循环/翻页/标记/时码/时长
   └─ MgKeyframesPanel
      ├─ 工具栏：◆播放头关键帧切换 / 删除 / 缩放(−/Fit/+) / 播放头居中 / 全部展开/折叠 / 属性过滤
      ├─ 左：元素树（z 序与 Layers 一致，顶层在前）+ 属性行（可展开折叠）
      └─ 右：标尺（自适应刻度+标记刻度+红色播放头）+ 关键帧菱形轨道
```

HTML 图层显示为一条**只读总轨**，按 `html_page_deck` 页时序分段（P1/P2…），可点击选中（联动 HTML inspector），第一阶段不可加关键帧。

## 交互（移植自合成页已验证契约）

- 属性行左侧菱形：播放头处关键帧切换（◆有帧/◇有轨/空白）
- 关键帧：拖动（validate-all-then-apply，冲突/越界整组拒绝）、Alt 拖拽复制、
  双击空轨加帧、双击已有关键帧编辑数值、Delete 删除、Ctrl+C/V 复制粘贴（相对帧锚定播放头）、
  marquee 框选（4px 阈值，低于阈值=seek+清选）、右键菜单含 6 种插值
- 标尺拖动 seek（16ms 节流+松手强制），与 transport 滑块双向同步
- Ctrl+滚轮缩放（光标锚定）、中键平移、Shift+滚轮横向、普通滚轮纵向（两侧同步）

## 数据与撤销

- 轨道存储不变：`scene.animations`（`SceneAnimation.track` 即共享的
  `src/shared/keyframe.py::KeyframeTrack`）；元素 `local_animation_tracks`
  作为"预置轨"显示（带 preset 徽标），编辑在其上创建 scene 级轨道——
  与 evaluator 的优先级一致（scene 覆盖 local），保证面板编辑永远生效
- 一次拖拽/粘贴 = 一次 `tracks_changed` 批量 = 一个 `CompositeCommand` = 一条 undo
- 新增 `RemoveAnimationCommand`（删最后一帧→整轨移除，可撤销）；
  controller 新增 `apply_track_edits(batch)` 批量入口
- 可动画属性：变换 `x,y,width,height,scale_x,scale_y,rotation,opacity` +
  按类型过滤的 `font_size / fill_opacity / stroke_width / blur`（范围对齐 evaluator）

## 三向同步

- 面板元素行点击 → `_set_selection(source="keyframes")` → Layers/Inspector/画布/浏览器
- 画布/Layers 选择 → `keyframes_panel.set_selection`（高亮无回声）
- 播放头：SceneTimeline 滑块、面板标尺、Space 播放共用 `_frame_changed` 单一路径
- 任意场景变化 → `_refresh_scene` → `set_scene` 重建（保留展开集合、缩放、点选）

## 代码

| 文件 | 职责 |
|---|---|
| `ui/mg_keyframes_model.py` | Qt-free 行模型 + 轨道编辑纯函数 + FrameMapper |
| `ui/mg_keyframes_panel.py` | 面板（左树/右轨道自绘，30px 标尺 + 24px 行栅格共享） |
| `core/commands.py` | +`RemoveAnimationCommand` |
| `ui/editor_controller.py` | +`apply_track_edits`（批量单撤销） |
| `ui/motion_graphic_editor_window.py` | 底部布局、开关、三向同步接线、编辑落地 |
| `locales/{en_US,zh_CN}.json` | `motion_graphic.keyframes.*` + `view.keyframes` |

## 统一时间线布局（2026-08-22）

- 完整关键帧面板打开时，旧的 `SceneTimeline` 不再单独占用一条播放条；它保留播放、循环、Marker、时长和页面步进状态，并将控件停靠到关键帧工具栏。
- 左侧元素表头显示当前时间（默认秒，如 `1.04 s`），右侧同一水平带绘制秒刻度标尺；内部仍使用整数帧保证关键帧和拖动精度。
- 原生 Motion Graphic 场景不再显示独立 `Page` 行；HTML 多页面场景保留页面按钮作为语义化页面跳转，播放控制仍归统一时间线工具栏。
- 用户关闭完整关键帧面板时，才恢复紧凑播放条作为可用的降级控制面板。

测试：`tests/ui/test_mg_keyframes_panel.py` + `tests/ui/test_mg_keyframes_integration.py`（共 37）；
回归：phase3 / manual_controls / layer_tree / autokey / animation_inspector / user_journey /
multiselect / html_preview / asset_library / edit 侧三件套 / commands 引用方全部通过。

## 第一阶段边界（后续工作）

- HTML DOM 子元素轨道（当前仅只读总轨）
- 值列内联编辑（当前已支持关键帧双击/右键弹窗编辑；仍未做表格内联值列）
- 曲线编辑器（Graph Editor）、贝塞尔手柄编辑（当前仅整段插值模式）
- 表达式编辑（evaluator 已支持 SceneExpression，面板未暴露）

## 第二轮审查修复（同日）

复查发现并修复的问题（均有回归测试）：

1. **预置轨全删是静默 no-op**（功能缺陷）：删除 `local_animation_tracks` 预置轨的
   全部关键帧时，scene 级 `None` 无法遮蔽内嵌轨——`apply_track_edits` 现在会通过
   `UpdateElementCommand` 移除内嵌轨本体（可撤销）；部分删除仍走 scene 轨覆盖。
2. **双击加帧后选择被清空**：Qt 的 press/release/press/dblclick/release 序列中，
   第二次 press 打开的 marquee 会让尾随 release 把新帧的选中清掉——dblclick 处理
   中现在显式取消 marquee。
3. **拖动释放后关键帧丢失选中**：拖动/A lt 复制的目标帧现在在 emit 前写入
   `_point_selection`，重建后保持高亮（AE 行为）。
4. **播放期每帧全量重建行**（性能）：SVG 场景 `_frame_changed → _refresh_scene`
   每帧推同一个不可变 scene 对象——`set_scene` 增加同对象+同选择+同时长的
   快速路径，直接跳过重建。
5. **HTML 行无选中高亮**：`build_keyframe_rows` 现在为 html 行设置 `selected`，
   滚动定位也覆盖它。
6. **Alt 拖拽视觉误导**：复制拖拽过程中原图帧消失（看起来像移动）——现在
   拖拽时同时绘制原位帧与移动副本。
7. **补全：方向键帧步进**：面板聚焦时 Left/Right ±1 帧（Shift ±10）、Home/End
   跳首/末帧，走同一 `frame_changed` 路径。
8. **补全：右侧轨道区选中属性行淡高亮**，与左侧树高亮呼应。

## 第三轮审查修复（2026-08-22）

1. **关键帧曲线元数据不丢失**：移动、Alt 复制和粘贴统一复制完整
   `Keyframe`，保留 `bezier_in/out`、`tangent_mode` 和左右句柄；编辑数值只改值，
   不重置插值设置。
2. **补全关键帧数值编辑**：已有关键帧支持双击或右键“编辑数值…”，输入框使用轨道
   的合法范围，编辑通过原有批量轨道命令进入撤销栈。
3. **补全属性过滤**：工具栏可在“全部”和“有动画”之间切换，后者只显示含关键帧的
   属性行，元素行、HTML 总轨和现有点选状态仍保持同步。
4. **场景差异完整性**：`layout`、`template_id`、`local_animation_tracks` 现在会进入
   `SceneDiff`，局部预置轨变化也会正确标记 `animation_changed`。
5. **面板高度恢复**：隐藏时间线后再打开会恢复用户上一次拖出的垂直高度，避免每次
   切换都回到固定高度。
