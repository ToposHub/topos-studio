# Files 替代 D4 批次验证记录（四布局状态保持）

日期：2026-11-17。对应[总计划](../../plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) D 表第 4 项。

## 现状审计与增量

四布局（adaptive/masonry/grid/list）的选择/身份/多选语义、横竖混合几何与
fit/fill 像素政策在既有批次已实现并有测试（`test_files_four_layouts.py`
`test_four_layouts_keep_selection_drag_urls_and_open_preview`：四模式共享
selection model/proxy，多选 URL/预览身份一致；
`test_fit_fill_changes_only_grid_pixels_and_never_file_geometry`：裁切政策
只动像素不动几何；A-wide/B-tall/D-square 合成条目覆盖横竖方混合）。

本批补上缺的两块验证：

| 缺口 | 补测 |
| --- | --- |
| 布局切换滚动锚点（`_change_layout` 捕获首个可见行→新视图 PositionAtTop） | `test_layout_switch_keeps_top_visible_anchor`（60 行、锚点行±1） |
| 缩放偏好持久化（密度滑杆→prefs.density→重开恢复 card_width） | `test_zoom_preference_survives_reopen` |

## 验证

- `tests/ui/test_files_four_layouts.py` **14 passed**、
  `test_files_gallery_layouts.py` **29 passed**（resize 锚点/范围选择等既有覆盖）。

## 边界

- 键鼠/窗口尺度/国际化实机验收属 D 表第 9 项，未在本批。
- masonry 布局下"首个可见行"与用户视线中心可能有 1-2 行包装差——锚点
  取的是可见首行（实现语义），测试按该语义断言。

## 隐私

仅源码/测试/Markdown；合成 PNG 与临时 QSettings。
