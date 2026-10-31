# Motion Graphic 场景页面（画板）— 设计与实现

日期：2026-08-22 · 分支：fix-ui

## 结论

Page（画板）成为一级实体：`TeachingSceneDocument.pages` 持有有序的
`ScenePage(page_id, name, duration_frames, background)`，每个
`SceneElement.page_id` 指向所属页面。HTML 层保留为渲染后端；其检测出的
每个 section 映射成一个 Page。Layers 与 Timeline 共用同一棵页面树。

## 数据模型（core/model.py）

- **增量 schema，不做版本号提升**（沿用 `metadata["hierarchy"]` 先例）：
  旧版本打开新文件时未知键被丢弃但不报错；新版本解析旧文件走容错默认。
- 构造期归一化（`__post_init__`）保证不变量：
  1. `pages` 非空且 page_id 去重；为空时 → 单页 `page_1`（时长=场景时长）；
     有 html_layer 且无 pages（旧 HTML 存档）→ 从分节派生。
  2. 每个元素的 page_id 必须指向存在的页面，未知/空 → 第一页。
- `duration_frames` 仍是总时长权威；页起点按页时长累计
  （`page_start_frames`），末页吸收尾差（`page_index_for_frame`）。
  页时长命令把总时长重算为页时长之和（`AddPage/RemovePage/UpdatePage`）。
- 视图：`elements_for_page` / `limited_to_page`（仅编辑器画布/命中测试用；
  导出与渲染服务继续用完整文档）。

## HTML 同步（core/pages.py）

`sync_pages_with_html_layer(scene)`：`detect_page_count` + `page_timing_for`
→ 页数与每页帧长（相邻起点差，末页到总时长）。页数不变时 page_id 与
命名保留（重命名、元素归属跨时序编辑存活）；页数缩减时，被删页的元素
移到最后一页。接入点：`SetHtmlLayerCommand`、`SetHtmlPageTimingCommand`、
`ReplaceHtmlSourceCommand`、`ApplyHtmlPatchCommand`（apply 同步 + undo
还原 pages/elements 快照）与 `module.py` 两条 AI 生成构建路径。

## 命令（core/commands.py）

| 命令 | 行为 |
|---|---|
| `AddPageCommand` | 插入页面，总时长=页时长之和；undo 还原 |
| `RemovePageCommand` | 删除页，元素并入相邻页（优先前一页）；快照还原 |
| `UpdatePageCommand` | 改名/时长/背景；改时长时后续页起点整体后移，总时长重算 |
| `ReorderPageCommand` | 页排序（时间区间随顺序重排） |
| `MoveElementsToPageCommand` | 批量改 page_id；沿 `hierarchy.parents` 展开后代，Frame 连子元素一起移动，单条 undo |

## 画布与窗口

- 画布按页过滤：`scene_view.set_scene(limited_to_page)`（渲染帧 + 命中
  测试同源）；SVG 同步渲染与 HTML 捕获线程都传页视图；浏览器原生
  overlay 只注入当前页元素，页切换时重建（`_refresh_native_overlay`）。
- 播放头/播放跨页自动切换（`page_index_for_frame`），浏览器 deck 同步
  `set_page`（`_syncing_browser_page` 防回环）；浏览器键盘翻页经
  `page_start_frames` 反查帧。
- 新建元素（菜单/绘制手势/预设插入）一律落在
  `controller.current_page_id`（由当前帧推导，无独立状态）。

## 图层树（layer_tree_model/panel）

- ≥2 页时按页分组：页头行（`__page__:{page_id}`，加粗、▶ 标记活动页、
  显示元素数），不可选/不可拖；点击 → `pageActivated` → 播放头跳页起点。
- 拖放：落在其它页的区块 → `pageMoveRequested` → `MoveElementsToPageCommand`
  （一条 undo）；同页 → 原 z 重排。重排/上下移按钮在"纯元素序"里计算
  （页头与 HTML 行不占位）。单页场景显示与旧版完全一致。

## 时间线（mg_keyframes_model/panel）

- HTML 主轨行移除，由页面行取代：≥2 页时每页一行（kind="page"），右侧
  画该页持续区间条（活动页高亮 + "P{n}" 标签），左侧页名（▶ 活动标记），
  点击页头 → 播放头跳页起点。元素行归入其页下方（z 降序），属性行深度+1。
- 单页场景无页面行 → 现有关键帧测试与行为不变。

## page_navigation

改为镜像 `scene.pages`（原生场景也有分页导航）；≥2 页才显示。页按钮点击
跳页起点；"+" 添加页；页按钮右键菜单：重命名 / 时长 / 左移 / 右移 / 删除
（意图信号，窗口执行命令与对话框）。

## 边界（第一阶段未含）

- 值列内联编辑、曲线编辑器（Graph Editor）、贝塞尔手柄编辑 —— 已有边界。
- 页面背景 `background` 已入模型与序列化，未暴露 UI。
- DOM 子元素级 HTML 轨道仍不拆分（html_layer 整体仍是渲染后端）。
- 预设片段插入时若关键帧超出时长仍自动 `SetDuration`（页时长不随之
  调整，由末页吸收尾差）。

## 测试

- `tests/core/test_scene_pages.py`（26）：归一化/迁移/往返、页助手、
  with_page_*、五个命令（含 Frame 带子元素 + undo）、HTML 同步
  （含旧存档派生、页数缩减重指派、时序命令页长更新）。
- `tests/ui/test_mg_scene_pages_ui.py`（13）：图层树页行/点击跳页/
  跨页拖放=重指派/同页拖放=重排/上下移元素空间、导航条原生多页显示、
  窗口加页/创建落活动页/重指派单条 undo/改名与时长处理器。
- 更新：`test_mg_keyframes_panel.py`（HTML 主轨行测试 → 页面行测试）、
  `test_mg_keyframes_integration.py`（分组 + 页切换画布断言）、
  `test_motion_graphic_manual_controls.py`（场景构建走 SetHtmlLayerCommand）。
- 回归：phase1 7、html 命令 11+12、渲染服务 17、manual_controls 34、
  journey 11、keyframes 集成 6、layer_tree 18、autokey 5、html_preview 17、
  multiselect 16+18、constraints 20 全绿。
  （注：`test_mg_keyframes_panel.py` 全文件批量跑存在先于本次改动的
  offscreen 段错误——三个 timecode 新测试与其它测试同进程运行时崩溃，
  单跑均通过；与本特性无关。）
