# Color 资源浏览器 UI 对照（2026-11-05）

本页记录本次直接对照 DaVinci Resolve Studio 20 Color 页后的界面约定。面板继续使用 Topos 全局控件样式、主题色和面板底色；资源身份、缩略图计算及节点命令仍由既有 catalog/controller/service 提供。

## Effects

- 默认显示 `Library / Settings` 页签、右侧搜索和更多按钮。搜索框由按钮展开，默认不占一行。
- Library 使用按分类折叠的列表。分类行、单效果描边行、左侧独立图标格和行距按 Resolve 的紧凑排布绘制。条目可以拖到节点；双击和右键仍走既有资源激活信号。
- Settings 采用无外框效果组：启用开关、名称、重置和删除置顶，参数排列在下方。效果上下移动保留在组的右键菜单，发出原有 `move_instance` 意图。
- 旧版 `grid=True` 偏好读取时迁移为列表；效果库不再默认显示分类下拉、加号按钮、描述区和目标说明区。其节点参数和信号接口保留。

## LUT

- 顶栏为文件夹树开关、后退/前进、当前文件夹名、缩略图尺寸、排序、缩略图/列表切换、搜索和更多按钮。
- 树中含 LUTs 根目录、嵌套分类和 Favorites。根目录将文件夹与 LUT 按名称混排；双击文件夹进入，树选择及前进/后退同步导航。搜索框按需展开。
- 缩略图视图显示文件夹瓦片和 LUT 预览；列表视图显示文件名、类型、相对路径、文件路径列。文件夹瓦片不会参与节点拖放或缩略图请求。
- 已应用节点 LUT 的强度与移除操作在“更多 → Current node LUT”中打开；原有 `draft_edit_requested` 信号和控件值保持兼容。缩略图到达时由 `QImage → QPixmap → QIcon` 更新可见瓦片。

## 验证与边界

- `tests/ui/test_effects_browser_panel.py`、`tests/ui/test_lut_browser_panel.py` 覆盖搜索、分类/文件夹导航、视图切换、节点意图和缩略图替换。
- `scripts/validate_color_node_media.py tests/test_videos/测试素材01.mov --resources --workflow` 在隔离配置和工程中通过，包含资源拖放、浮动布局、节点编辑及媒体工作流。
- UI 的图标按 Topos 效果元数据绘制，无法借用 Resolve 私有的逐效果图标。LUT 预览必须由现有 `LutThumbnailService` 返回真实图像；无图像时仍显示明确占位，不能伪造已应用效果。
- 本次隔离应用的资源截图中，临时 `UI Identity` LUT 仍显示 `No preview`；单元测试已验证真实 `QImage` 到达后能替换根目录瓦片。后续需在节点源缩略图生成后重新请求 LUT 预览，并确认服务的 Qt 像素对象只在 GUI 线程创建。验证方式：用测试素材导入临时 `.cube`，等待缩略图信号，断言瓦片不含占位文字且像素与源图不同；切片、切节点、取消与失败路径分别确认不会显示过期预览。
