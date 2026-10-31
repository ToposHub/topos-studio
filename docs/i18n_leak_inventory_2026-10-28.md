# i18n 硬编码泄漏清单（2026-10-28 全量 AST 扫描）

> 扫描范围：src/（排除 tests），AST 覆盖 setText/setWindowTitle/setToolTip/
setPlaceholderText/setTitle/setTabText/setStatusTip/setWhatsThis/setLabelText/
setSuffix + QPushButton/QLabel/QCheckBox/QRadioButton/QGroupBox/QMenu/QAction/
QToolButton/QListWidgetItem/QTableWidgetItem/QTreeWidgetItem 构造器首参 + addAction。
> 豁免：空串、纯数字/符号/时间格式、占位符。修复口径见计划 P3。

## src/shared/color/color_picker.py（52 处）

- L897 `QLabel()`: "对比度检查 (WCAG)"
- L903 `QPushButton()`: "白底"
- L923 `QLabel()`: "AAA"
- L934 `QLabel()`: "Aa 文字预览"
- L941 `QPushButton()`: "修复"
- L948 `setToolTip`: "自动调整亮度以满足 AA 标准 (4.5:1)"
- L1016 `QLabel()`: "代码格式导出"
- L1060 `QPushButton()`: "复制"
- L1167 `QPushButton()`: "取消"
- L1182 `QPushButton()`: "确定"
- L1252 `QPushButton()`: "取消"
- L1267 `QPushButton()`: "确定"
- L1366 `setWindowTitle`: "Color Inspector - 设置面板"
- L1385 `QLabel()`: "色彩空间:"
- L1401 `QLabel()`: "⚠ 溢色警告"
- L1423 `QPushButton()`: "恢复默认设置"
- L1433 `QPushButton()`: "关闭"
- L1590 `setToolTip`: "拖拽调整顺序"
- L1600 `setToolTip`: "双击修改名称"
- L1646 `QAction()`: "编辑颜色"
- L1651 `QAction()`: "复制HEX值"
- L1656 `QAction()`: "清除"
- L1805 `setText`: "#FF0000"
- L1838 `setToolTip`: "拖拽颜色到色块预设中保存"
- L1846 `setToolTip`: "屏幕取色"
- L1854 `setToolTip`: "切换颜色选择工具"
- L1882 `QPushButton()`: "添加到自定义"
- L1891 `QPushButton()`: "添加新行"
- L1900 `QPushButton()`: "重置"
- L1909 `QPushButton()`: "导出"
- L1918 `QPushButton()`: "导入"
- L1927 `QPushButton()`: "ASE"
- L1938 `QPushButton()`: "确定"
- L2261 `setToolTip`: "拖拽颜色到自定义色块中保存"
- L2275 `setToolTip`: "切换颜色选择工具"
- L2374 `QLabel()`: "历史"
- L2389 `QPushButton()`: "添加到自定义"
- L2396 `QPushButton()`: "确定"
- L2454 `QLabel()`: "自定义"
- L2629 `setWindowTitle`: "Color Inspector"
- L2742 `QAction()`: "拾色器"
- L2747 `QAction()`: "色板"
- L2754 `QAction()`: "设置面板..."
- L2761 `QAction()`: "色轮 (待开发)"
- L2765 `QAction()`: "亮度立方体 (待开发)"
- L964 `setText`: "黑底"
- L967 `setText`: "白底"
- L1800 `QLabel()`: "HEX:"
- L2342 `QLabel()`: "HEX:"
- L2358 `QLabel()`: "A:"
- L1319 `setToolTip`: "历史色块"
- L1348 `setToolTip`: "空"

## src/features/composite/ui/node_properties_panel.py（49 处）

- L232 `QLabel()`: "In"
- L233 `QLabel()`: "Out"
- L834 `setToolTip`: "Change node color"
- L871 `setToolTip`: "Save preset"
- L882 `setToolTip`: "Pin section (keep visible)"
- L893 `setToolTip`: "Lock parameters"
- L904 `setToolTip`: "Reset all parameters"
- L1688 `QLabel()`: "Properties"
- L1698 `setToolTip`: "Toggle search (Ctrl+F)"
- L1708 `setToolTip`: "Settings"
- L1724 `setPlaceholderText`: "Search parameters..."
- L2071 `addAction`: "Project Settings"
- L2072 `addAction`: "3D Scene Settings"
- L2165 `QGroupBox()`: "Common Parameters"
- L2867 `setToolTip`: "左键：切换关键帧\nAlt+左键：编辑表达式\n右键：曲线编辑器"
- L3087 `setToolTip`: "右键打开表达式/维度菜单"
- L3200 `setPlaceholderText`: "输入表达式；留空后点击“清除”可移除表达式"
- L3295 `setToolTip`: "右键打开表达式/维度菜单"
- L3503 `setToolTip`: "右键打开表达式/维度菜单"
- L3629 `setToolTip`: "右键打开表达式菜单"
- L3730 `setToolTip`: "左键：切换三轴关键帧\nAlt+左键：编辑 X 表达式\n右键：曲线编辑器"
- L4531 `QLabel()`: "Click to add | Drag to move | Double-click to delete"
- L5869 `setToolTip`: "左键：切换 X/Y 关键帧\nAlt+左键：编辑 X 表达式\n右键：曲线编辑器"
- L5940 `setToolTip`: "左键：切换多通道关键帧\nAlt+左键：编辑表达式\n右键：曲线编辑器"
- L6513 `setToolTip`: "Screen Colour — click to pick"
- L6576 `setToolTip`: "Pick screen colour from screen"
- L7045 `setPlaceholderText`: "Enter text... (Shift+Enter for newline)"
- L7224 `QPushButton()`: "2D"
- L7225 `QPushButton()`: "3D"
- L8125 `setToolTip`: "Pick color from screen"
- L860 `setToolTip`: "Activate camera view"
- L1998 `setText`: "Properties"
- L2057 `setText`: "Properties"
- L2203 `QLabel()`: "(mixed)"
- L2531 `QGroupBox()`: "Parameters"
- L2562 `QGroupBox()`: "Parameters"
- L3228 `setText`: "表达式语法有效"
- L5731 `addAction`: "复制参数值 (Copy Value)"
- L5738 `addAction`: "粘贴参数值 (Paste Value)"
- L5744 `addAction`: "参数链接 (Pick Whip)…"
- L5811 `QLabel()`: "选择源节点与参数（目标 = 当前参数）:"
- L7571 `setToolTip`: "Swap primary and secondary colors"
- L1928 `setText`: "Properties"
- L4935 `addAction`: "Dock"
- L4937 `addAction`: "Float"
- L5715 `addAction`: "提升到组 (Promote to Group)"
- L5761 `addAction`: "合并 X / Y  (Combine)"
- L5765 `addAction`: "分离 X / Y  (Separate Dimensions)"
- L5759 `addAction`: "没有可编辑的表达式"

## src/features/composite/ui/node_editor_widget.py（47 处）

- L1775 `addAction`: "Save as Preset..."
- L1801 `addAction`: "Import Preset..."
- L1820 `addAction`: "Auto Layout"
- L1825 `addAction`: "Left"
- L1826 `addAction`: "Right"
- L1827 `addAction`: "Top"
- L1828 `addAction`: "Bottom"
- L1829 `addAction`: "Center Horizontal"
- L1830 `addAction`: "Center Vertical"
- L1834 `addAction`: "Horizontal"
- L1835 `addAction`: "Vertical"
- L1838 `addAction`: "Cleanup Overlaps"
- L2052 `addAction`: "Add Backdrop"
- L2053 `addAction`: "Add Sticky Note"
- L2055 `addAction`: "Set All Reads to Latest Version"
- L3989 `setWindowTitle`: "Save as Preset"
- L3996 `QLabel()`: "Preset Name:"
- L3999 `setPlaceholderText`: "Enter preset name..."
- L4003 `QLabel()`: "Description (optional):"
- L4007 `setPlaceholderText`: "Describe this preset..."
- L4016 `QPushButton()`: "Save"
- L4018 `QPushButton()`: "Cancel"
- L4080 `setWindowTitle`: "Manage Presets"
- L4087 `QLabel()`: "Saved Presets:"
- L4106 `QPushButton()`: "Load"
- L4110 `QPushButton()`: "Rename"
- L4114 `QPushButton()`: "Delete"
- L4118 `QPushButton()`: "Export"
- L4122 `QPushButton()`: "Close"
- L5088 `addAction`: "Back"
- L5090 `addAction`: "Forward"
- L5093 `addAction`: "Add View Bookmark..."
- L5124 `addAction`: "Isolate Selected Branch"
- L5125 `addAction`: "Highlight Dependencies"
- L5126 `addAction`: "Clear Graph Focus"
- L1797 `addAction`: "(No presets)"
- L2031 `addAction`: "Delete Connection"
- L2042 `addAction`: "Edit Backdrop"
- L2043 `addAction`: "Change Backdrop Color"
- L2044 `addAction`: "Send Backdrop to Back"
- L2045 `addAction`: "Delete Backdrop"
- L2101 `addAction`: "Rename Node"
- L5101 `addAction`: "(No bookmarks)"
- L5110 `addAction`: "(No bookmarks)"
- L1792 `addAction`: "Manage Presets..."
- L1794 `addAction`: "(No presets saved)"
- L2112 `addAction`: "Disconnect Node"

## src/features/composite/ui/composite_viewport.py（36 处）

- L285 `setText`: "RGB"
- L302 `setText`: "Beauty"
- L325 `QLabel()`: "Exp: 0.0"
- L516 `addAction`: "Set 3D Cursor to View Target"
- L519 `addAction`: "Set 3D Cursor to Selected Object"
- L526 `addAction`: "+1 Stop"
- L527 `addAction`: "+0.5 Stop"
- L528 `addAction`: "+0.1 Stop"
- L530 `addAction`: "-0.1 Stop"
- L531 `addAction`: "-0.5 Stop"
- L532 `addAction`: "-1 Stop"
- L534 `addAction`: "Reset Exposure"
- L540 `addAction`: "1.0 (Linear)"
- L542 `addAction`: "2.2 (sRGB)"
- L544 `addAction`: "Reset Gamma"
- L553 `addAction`: "Reset"
- L559 `addAction`: "Reset"
- L565 `addAction`: "Reset"
- L574 `addAction`: "sRGB (Standard)"
- L579 `addAction`: "Linear (Raw)"
- L617 `addAction`: "Show BBox Overlay"
- L621 `addAction`: "Show Format Overlay"
- L652 `addAction`: "Store Snapshot to A	Ctrl+Shift+A"
- L655 `addAction`: "Store Snapshot to B	Ctrl+Shift+B"
- L674 `addAction`: "Clear A/B Snapshots"
- L681 `addAction`: "Enable Magnifier (Z)"
- L876 `QLabel()`: "x: —  y: —"
- L880 `QLabel()`: "R: —  G: —  B: —  A: —"
- L3670 `addAction`: "+1 Stop"
- L3671 `addAction`: "-1 Stop"
- L3672 `addAction`: "Reset"
- L3676 `addAction`: "Show BBox"
- L3681 `addAction`: "Show Format"
- L3686 `addAction`: "Show Pixel Inspector"
- L937 `setText`: "x: —  y: —"
- L938 `setText`: "R: —  G: —  B: —  A: —"

## src/app/ui/script_panel.py（29 处）

- L738 `setToolTip`: "Close search bar (Esc)"
- L755 `setToolTip`: "Find previous (Shift+Enter)"
- L762 `setToolTip`: "Find next (Enter)"
- L785 `setToolTip`: "Replace current match"
- L792 `setToolTip`: "Replace all matches"
- L799 `setToolTip`: "Toggle replace mode"
- L980 `QAction()`: "Float"
- L984 `QAction()`: "Dock"
- L991 `QAction()`: "Copy"
- L996 `QAction()`: "Paste"
- L1001 `QAction()`: "Cut"
- L1008 `QAction()`: "Select All"
- L1019 `QAction()`: "Copy"
- L1024 `QAction()`: "Select All"
- L1029 `QAction()`: "Clear"
- L1069 `setToolTip`: "New script (Ctrl+N)"
- L1077 `setToolTip`: "Open script (Ctrl+O)"
- L1085 `setToolTip`: "Save script"
- L1101 `setToolTip`: "Browse and manage scripts"
- L1119 `setToolTip`: "Run script (F5)"
- L1128 `setToolTip`: "Stop running script"
- L1222 `setPlaceholderText`: "Find in output..."
- L1259 `addAction`: "Examples"
- L1277 `addAction`: "My Scripts"
- L1295 `addAction`: "Save to Scripts..."
- L1299 `addAction`: "Recent Files"
- L1271 `addAction`: "(No examples)"
- L1289 `addAction`: "(Empty — Save scripts here)"
- L1308 `addAction`: "(No recent files)"

## src/features/edit/ui/timeline_items.py（22 处）

- L8170 `addAction`: "Rename Track..."
- L8173 `addAction`: "Delete Track"
- L8202 `addAction`: "Move Track Up"
- L8203 `addAction`: "Move Track Down"
- L8210 `addAction`: "Small"
- L8211 `addAction`: "Medium"
- L8212 `addAction`: "Large"
- L8213 `addAction`: "Default"
- L8219 `addAction`: "Set as Target Track"
- L9106 `addAction`: "Show Frames"
- L9107 `addAction`: "Show Audio Samples"
- L6387 `setToolTip`: "Transition (Track Locked)"
- L6395 `setToolTip`: "Trim transition duration (drag to adjust)"
- L8147 `QMenu()`: "Caption Graphic Template"
- L8148 `addAction`: "Fade"
- L8149 `addAction`: "Typewriter"
- L8150 `addAction`: "Scale Pop"
- L8151 `addAction`: "Remove Template"
- L4668 `addAction`: "Add Transition (Cross Dissolve)"
- L4669 `addAction`: "Add Transition (Dip To Color)"
- L4673 `addAction`: "Add Transition Before..."
- L6399 `setToolTip`: "Trim transition duration (drag to adjust)"

## src/features/edit/ui/caption_workspace.py（15 处）

- L363 `setPlaceholderText`: "Find…"
- L365 `setPlaceholderText`: "Replace…"
- L366 `QPushButton()`: "Find"
- L367 `QPushButton()`: "Replace All"
- L368 `QPushButton()`: "Split"
- L369 `QPushButton()`: "Merge"
- L563 `setText`: "Merge requested."
- L373 `QLabel()`: "Find:"
- L375 `QLabel()`: "Repl:"
- L491 `setText`: "Empty search."
- L517 `setText`: "Empty search."
- L536 `setText`: "Select exactly one cue to split."
- L541 `setText`: "Cue too short to split."
- L550 `setText`: "Select exactly two cues to merge."
- L571 `setText`: "No cues."

## src/features/composite/ui/viewer3d_settings_section.py（15 处）

- L399 `QCheckBox()`: "Opaque"
- L423 `QCheckBox()`: "Grid"
- L425 `QCheckBox()`: "Axes"
- L496 `QCheckBox()`: "Region"
- L499 `QCheckBox()`: "Mask"
- L502 `QCheckBox()`: "Border"
- L592 `QCheckBox()`: "Depth"
- L593 `QCheckBox()`: "Normal"
- L594 `QCheckBox()`: "Position"
- L595 `QCheckBox()`: "UV"
- L596 `QCheckBox()`: "Material ID"
- L597 `QCheckBox()`: "Motion"
- L598 `QCheckBox()`: "Object ID"
- L633 `setToolTip`: "拖动视图时自动降质（关 MSAA/IBL）保流畅；停止后恢复高质量"
- L678 `setToolTip`: "导航时对非活动正交视图降频（按下方 FPS 刷新，保留上一帧），保活动视图流畅"

## src/features/composite/ui/asset_dependency_panel.py（13 处）

- L71 `QLabel()`: "资产依赖"
- L76 `QPushButton()`: "刷新"
- L77 `setToolTip`: "重新扫描当前合成图中的显式资产路径"
- L81 `QPushButton()`: "收集到目录…"
- L82 `setToolTip`: "复制已解析依赖并将图中的路径写回为可移植路径"
- L86 `QPushButton()`: "搜索目录…"
- L87 `setToolTip`: "添加离线素材搜索目录并保存到当前合成图"
- L93 `QLabel()`: "搜索目录：未设置"
- L119 `QLabel()`: "尚未扫描资产依赖"
- L188 `QPushButton()`: "选择替换"
- L191 `setToolTip`: "从文件系统选择路径；页面会再次执行候选校验和撤销记录"
- L134 `setText`: "尚未扫描资产依赖"
- L156 `setText`: "搜索目录：未设置"

## src/features/composite/ui/sticky_note_item.py（13 处）

- L459 `addAction`: "Edit Sticky Note"
- L460 `addAction`: "Change Color"
- L472 `addAction`: "Delete Sticky Note"
- L473 `addAction`: "Bring to Front"
- L474 `addAction`: "Send to Back"
- L501 `setWindowTitle`: "Edit Sticky Note"
- L515 `QLabel()`: "Opacity:"
- L541 `QLabel()`: "Font size:"
- L571 `QPushButton()`: "OK"
- L572 `QPushButton()`: "Cancel"
- L627 `setWindowTitle`: "Choose Sticky Note Color"
- L507 `QLabel()`: "Note:"
- L537 `setText`: "Aa"

## src/features/edit/ui/ai_preferences_page.py（12 处）

- L622 `QGroupBox()`: "连接设置"
- L649 `setPlaceholderText`: "My OpenAI"
- L680 `setPlaceholderText`: "Base URL 或完整接口地址"
- L681 `setToolTip`: "可填写 Base URL（例如 /v4）或完整接口地址（例如 /v4/chat/completions）；程序不会重复拼接路径"
- L690 `setPlaceholderText`: "gpt-4o"
- L699 `setSuffix`: "k"
- L775 `QCheckBox()`: "允许将参考素材上传到此 Profile"
- L777 `setToolTip`: "只声明数据策略；每次云端分析/生成仍需独立用户确认。"
- L785 `QGroupBox()`: "服务能力（用于分类和路由）"
- L1365 `setPlaceholderText`: "Base URL 或完整接口地址"
- L1366 `setPlaceholderText`: "model-name"
- L430 `QListWidgetItem()`: "（MCP 网关不可用）"

## src/features/composite/ui/keyframes_tree_view.py（11 处）

- L386 `setToolTip`: "Keyframe frame"
- L388 `QLabel()`: "Value"
- L407 `setToolTip`: "Interpolation"
- L410 `QPushButton()`: "Apply"
- L414 `setToolTip`: "Remove keyframe"
- L417 `setToolTip`: "Close keyframe editor"
- L381 `QLabel()`: "KF"
- L1532 `addAction`: "Add to Active Layer"
- L1533 `addAction`: "Promote to Layer"
- L1534 `addAction`: "Create Matte Layer"
- L1546 `setToolTip`: "Edit this property's expression"

## src/features/color/ui/tracker_correction_panel.py（11 处）

- L87 `setWindowTitle`: "跟踪修正与增量重跟"
- L92 `QPushButton()`: "在当前帧手工修正…"
- L99 `QPushButton()`: "重跟选中段"
- L102 `QPushButton()`: "取消重跟"
- L107 `QPushButton()`: "全部重跟"
- L196 `setText`: "已请求取消（当前帧完成后中止，部分结果不合并）…"
- L96 `QLabel()`: "失败段（增量重跟——只跑失败帧）："
- L178 `setText`: "重跟进行中——先取消或等待完成"
- L181 `setText`: "未提供跟踪引擎回调——无法重跟"
- L207 `setText`: "重跟无结果（段未变化）"
- L239 `setText`: "重跟仍在途——完成后关闭（防半途销毁）"

## src/features/composite/ui/backdrop_item.py（10 处）

- L621 `setWindowTitle`: "Edit Backdrop"
- L641 `QLabel()`: "Opacity:"
- L667 `QLabel()`: "Title font:"
- L697 `QLabel()`: "Note font:"
- L727 `QPushButton()`: "OK"
- L728 `QPushButton()`: "Cancel"
- L794 `setWindowTitle`: "Choose Backdrop Color"
- L628 `QLabel()`: "Title:"
- L633 `QLabel()`: "Note:"
- L663 `setText`: "Aa"

## src/shared/ui/audio_mixer/track_mixer_widget.py（9 处）

- L251 `setText`: "FX"
- L253 `setText`: "DY"
- L255 `setText`: "EQ"
- L336 `setText`: "M"
- L452 `QLabel()`: "dB"
- L487 `QLabel()`: "L"
- L499 `QLabel()`: "R"
- L346 `setText`: "S"
- L368 `setText`: "R"

## src/features/composite/ui/timeline_expression_editor.py（9 处）

- L138 `setPlaceholderText`: "Expression — e.g. value * 2 + sin(time)"
- L154 `QPushButton()`: "Cancel"
- L156 `QPushButton()`: "Apply"
- L266 `setText`: "Expression disabled — value uses the keyframe/static base."
- L281 `setText`: "Enter an expression. Use value, time, frame, comp.width…"
- L284 `setText`: "Apply to validate."
- L293 `setText`: "Enter an expression. Use value, time, frame, comp.width…"
- L308 `setText`: "New expression."
- L311 `setText`: "Disabled — value uses the keyframe/static base."

## src/features/color/ui/timeline_clip_strip.py（9 处）

- L1100 `QLabel()`: "No timeline clips"
- L1274 `addAction`: "Clip Name"
- L1279 `addAction`: "Video Codec"
- L1329 `addAction`: "Float Panel"
- L1330 `addAction`: "Close Panel"
- L1265 `addAction`: "Enable Previews (Large Timeline)"
- L1266 `setToolTip`: "Previews are disabled for performance. Click to enable."
- L1309 `addAction`: "Copy Grade"
- L1313 `addAction`: "Paste Grade Attributes..."

## src/shared/ui/audio_mixer/audio_level_meter.py（8 处）

- L386 `QAction()`: "Solid Fill"
- L391 `QAction()`: "Gradient Fill"
- L396 `QAction()`: "Show Block Lines"
- L401 `QAction()`: "Colorful Peak Indicator"
- L406 `QAction()`: "Monochrome Peak Indicator"
- L412 `QAction()`: "Meter Mode: Peak (dBFS)"
- L417 `QAction()`: "Meter Mode: RMS (dBFS)"
- L422 `QAction()`: "Meter Mode: LUFS (EBU R128)"

## src/features/edit/ui/edit_effects_panel.py（8 处）

- L1028 `setToolTip`: "Search effects"
- L1135 `setSuffix`: " f"
- L1136 `setToolTip`: "Frames used by the selected subtitle effect to enter."
- L1147 `setSuffix`: " f"
- L1148 `setToolTip`: "Frames used by the selected subtitle effect to exit; 0 holds to the end."
- L1160 `setToolTip`: "Travel distance for slide effects, measured relative to the subtitle size."
- L1408 `setToolTip`: "Hide effect search"
- L1418 `setToolTip`: "Search effects"

## src/features/composite/ui/composite_page.py（8 处）

- L519 `QLabel()`: "Node Graph"
- L529 `QPushButton()`: "⬅ Back"
- L532 `setToolTip`: "Exit the current group edit view (also Esc)"
- L560 `setToolTip`: "Settings"
- L6117 `addAction`: "Grid Snap"
- L6123 `addAction`: "Minimap"
- L6152 `addAction`: "Preview Quality"
- L6177 `addAction`: "Output Target"

## src/features/composite/ui/bake_progress_dialog.py（8 处）

- L101 `QLabel()`: "准备…"
- L111 `QPushButton()`: "取消（保留旧产物）"
- L148 `setText`: "正在取消（当前帧完成后中止，产物不发布）…"
- L174 `setText`: "关闭"
- L196 `setText`: "正在取消并等待当前帧完成…"
- L207 `setText`: "任务仍在途（取消等待超时），完成后再关闭——防半途销毁"
- L164 `setText`: "烘焙完成（图已改写）"
- L166 `setText`: "烘焙完成（产物已发布，图未改写）"

## src/features/color/ui/mask_panel.py（8 处）

- L80 `setToolTip`: "Invert Mask"
- L106 `setToolTip`: "Delete Mask Window"
- L297 `setToolTip`: "Reset All Mask Parameters"
- L374 `setToolTip`: "Auto Remove Background (BiRefNet)"
- L382 `setToolTip`: "Interactive Segmentation (SAM)"
- L392 `setToolTip`: "Draw mask with brush (B to toggle, [ ] to resize, E for eraser)"
- L402 `setToolTip`: "Toggle Eraser Mode (E) - Subtract from mask"
- L409 `setToolTip`: "Send selected window to Tracker / 用跟踪器跟踪当前窗口"

## src/features/edit/ui/timeline_view.py（7 处）

- L4150 `addAction`: "Detach Caption Graphic"
- L4151 `addAction`: "Convert Back to Subtitle"
- L11998 `setSuffix`: " dB"
- L4154 `addAction`: "Open in Composite"
- L4139 `addAction`: "Convert to Caption Graphic"
- L12013 `setText`: "无法定位素材文件，无法分析"
- L3519 `addAction`: "Open Motion Graphic Editor"

## src/features/edit/ui/snapshot_compare_panel.py（7 处）

- L40 `setWindowTitle`: "工程快照比较"
- L59 `QPushButton()`: "比较"
- L63 `QLabel()`: "选择两个快照后点击「比较」"
- L45 `QLabel()`: "从："
- L51 `QLabel()`: "到："
- L78 `setText`: "需要两个不同的快照"
- L92 `setText`: "两快照无差异"

## src/features/composite/ui/material_panel.py（7 处）

- L66 `QLabel()`: "材质 / Materials"
- L75 `QLabel()`: "未选择导入材质"
- L154 `QPushButton()`: "还原材质"
- L103 `QLabel()`: "贴图槽 / Texture Slots"
- L115 `setPlaceholderText`: "无贴图或输入替换路径"
- L123 `QPushButton()`: "选择"
- L237 `setText`: "未选择导入材质"

## src/features/composite/ui/scene_outliner_panel.py（7 处）

- L52 `QLabel()`: "场景 Outliner"
- L58 `setPlaceholderText`: "搜索对象…"
- L67 `QPushButton()`: "展开"
- L71 `QPushButton()`: "收起"
- L97 `QLabel()`: "无模型层级"
- L114 `setText`: "无模型层级"
- L132 `setText`: "无模型层级"

## src/features/composite/ui/keyframe_editor.py（7 处）

- L627 `QCheckBox()`: "Animated"
- L628 `setToolTip`: "Enable keyframe animation for this parameter"
- L636 `setToolTip`: "Interpolation type for selected keyframe"
- L666 `setToolTip`: "Keyframe frame number"
- L676 `setToolTip`: "Keyframe value"
- L669 `QLabel()`: "Frame:"
- L679 `QLabel()`: "Value:"

## src/features/color/ui/plugin_panel.py（7 处）

- L252 `setText`: "EFFECT CONTROLS"
- L325 `QLabel()`: "Enabled"
- L335 `setToolTip`: "Reset parameters"
- L344 `setToolTip`: "Remove effect"
- L390 `QLabel()`: "Enabled"
- L403 `setToolTip`: "Remove effect"
- L282 `QGroupBox()`: "Missing Plugin"

## src/features/color/ui/layer_panel.py（7 处）

- L89 `setToolTip`: "Layer has grading adjustments applied"
- L97 `setToolTip`: "Layer has effects applied"
- L105 `setToolTip`: "Layer has active masks"
- L113 `setToolTip`: "AI source bake active"
- L131 `setToolTip`: "Reset Layer Adjustments"
- L139 `setToolTip`: "Remove Layer"
- L370 `QLabel()`: "⚠️"

## src/features/color/ui/canvas.py（7 处）

- L2806 `setText`: "I"
- L2813 `setText`: "O"
- L4801 `addAction`: "Convert to Smooth"
- L4802 `setToolTip`: "Shortcut: Double-click"
- L4805 `addAction`: "Convert to Corner"
- L4806 `setToolTip`: "Shortcut: Double-click or Alt+Drag handle"
- L4794 `addAction`: "Delete Point"

## src/features/edit/ui/relink_media_dialog.py（6 处）

- L245 `setToolTip`: "Select a new file for the selected asset"
- L250 `setToolTip`: "Select a directory to search for all missing files"
- L257 `setToolTip`: "Automatically search for files with matching names"
- L300 `QTableWidgetItem()`: "Offline"
- L599 `setText`: "Found"
- L602 `setText`: "Missing"

## src/features/edit/ui/proxy_cleanup_panel.py（6 处）

- L53 `setWindowTitle`: "代理与渲染缓存管理"
- L59 `QCheckBox()`: "只清源已不存在的孤儿代理（推荐——跨项目引用保护）"
- L64 `QCheckBox()`: "同时清理失败（FAILED）与过期（STALE）代理"
- L68 `QPushButton()`: "清理…"
- L81 `setText`: "代理服务不可用（无报告入口）"
- L87 `setText`: "使用状态读取失败（见日志）"

## src/features/composite/ui/graph_node_search_popup.py（6 处）

- L100 `setWindowTitle`: "Search in Graph"
- L119 `setPlaceholderText`: "Search nodes by name, type, or parameters... (e.g., 'blur', 'grade.gain', 'output')"
- L139 `QLabel()`: "0 results"
- L147 `QPushButton()`: "Prev"
- L154 `QPushButton()`: "Next"
- L164 `QLabel()`: "↑↓ Navigate  •  Enter Jump  •  Shift+Enter Jump & Close  •  Esc Close"

## src/features/deliver/ui/deliver_page.py（6 处）

- L255 `setSuffix`: " frames"
- L301 `setSuffix`: " px"
- L306 `setSuffix`: " px"
- L338 `setSuffix`: " fps"
- L400 `setSuffix`: " colors"
- L401 `QLabel()`: "GIF colors"

## src/features/color/ui/still_lightbox.py（6 处）

- L94 `setWindowTitle`: "Still Lightbox——参考比较"
- L112 `QPushButton()`: "差异模式"
- L116 `QPushButton()`: "关闭"
- L167 `setText`: "差异视图：|参考−当前|×4（显示域）"
- L171 `setText`: "并排视图（显示域，HDR 值已 clip——精度徽标为准）"
- L163 `setText`: "差异不可用（图像缺失或形状不一致）"

## src/app/controllers/menu_toolbar_controller.py（6 处）

- L407 `QAction()`: "Toggle Color Inspector"
- L1152 `setText`: "Untitled Project"
- L1413 `QAction()`: "Undo"
- L1415 `QAction()`: "Redo"
- L1527 `setToolTip`: "Reset All Layer Adjustments"
- L1580 `setToolTip`: "Select Workspace Layout"

## src/features/edit/ui/m07_version_tools_panel.py（5 处）

- L53 `QPushButton()`: "保存当前为命名版本…"
- L56 `QPushButton()`: "激活选中版本"
- L59 `QPushButton()`: "比较选中两版本"
- L62 `QPushButton()`: "切换共享锁"
- L113 `setText`: "提交被拒（工程已切换或命令失败）——本面板未写入"

## src/features/edit/ui/filler_removal_panel.py（5 处）

- L57 `setWindowTitle`: "删除口头语"
- L80 `QPushButton()`: "预览删除区间（差异）"
- L90 `setText`: "生成删除计划（进入审批）"
- L104 `setText`: "未勾选任何命中——无计划"
- L123 `setText`: "计划为空（命中未映射到真实片段？）"

## src/features/composite/ui/node_search_popup.py（5 处）

- L149 `setWindowTitle`: "Add Node"
- L172 `setPlaceholderText`: "Search nodes... (type to filter)"
- L231 `QLabel()`: "↑↓ Navigate  •  Enter Add  •  Tab Switch  •  Esc Close"
- L349 `QListWidgetItem()`: "  No favorites yet"
- L370 `QListWidgetItem()`: "  No recent nodes"

## src/features/composite/ui/perf_panel.py（5 处）

- L58 `QLabel()`: "Performance"
- L75 `QPushButton()`: "Copy"
- L86 `QPushButton()`: "Export JSON"
- L243 `addAction`: "Dock Panel"
- L245 `addAction`: "Float Panel"

## src/features/deliver/ui/video_export_settings_panel.py（5 处）

- L994 `QLabel()`: "W"
- L1011 `QLabel()`: "H"
- L934 `QLabel()`: "In"
- L936 `QLabel()`: "Out"
- L2326 `setSuffix`: " kbps"

## src/features/color/ui/shot_match_panel.py（5 处）

- L83 `setWindowTitle`: "批量镜头匹配"
- L94 `QPushButton()`: "分析"
- L99 `QLabel()`: "选择参考镜头后点击「分析」"
- L108 `setText`: "提交匹配"
- L87 `QLabel()`: "参考镜头："

## src/features/ai_workflow/ui/workflow_panel.py（5 处）

- L173 `QLabel()`: "状态: 未装配"
- L180 `QPushButton()`: "预览计划"
- L184 `QPushButton()`: "请求审批"
- L188 `QPushButton()`: "应用已批准计划"
- L195 `QLabel()`: "暂无计划"

## src/shared/export/export_panel.py（4 处）

- L1120 `QPushButton()`: "Cancel Export"
- L1196 `setText`: "img_"
- L1197 `setText`: "_exported"
- L1200 `setText`: "{name}_{n:03d}"

## src/shared/ui/audio_mixer/routing_matrix_dialog.py（4 处）

- L107 `QLabel()`: "■ Output"
- L109 `QLabel()`: "■ Send"
- L187 `setText`: "OUT"
- L189 `setText`: "SND"

## src/features/composite/ui/import_report_panel.py（4 处）

- L99 `QLabel()`: "导入报告"
- L107 `QLabel()`: "未开始"
- L423 `setText`: "—（无导入报告）"
- L382 `setText`: "无"

## src/features/color/ui/tracker_panel.py（4 处）

- L407 `QPushButton()`: "◀◀"
- L414 `QPushButton()`: "◀|"
- L426 `QPushButton()`: "|▶"
- L434 `QPushButton()`: "▶▶"

## src/features/edit/ui/scene_cut_preview_widget.py（3 处）

- L219 `QLabel()`: "Frame: --"
- L307 `setText`: "Frame: --"
- L287 `setText`: "Frame: --"

## src/features/edit/ui/source_viewer.py（3 处）

- L259 `setText`: "I"
- L266 `setText`: "O"
- L326 `setText`: "V/A"

## src/features/composite/ui/spline_panel.py（3 处）

- L315 `QLabel()`: "0 curves"
- L439 `setText`: "F: —"
- L440 `setText`: "V: —"

## src/features/composite/ui/project_settings_widget.py（3 处）

- L125 `QLabel()`: "W"
- L141 `QLabel()`: "H"
- L179 `QLabel()`: "s"

## src/app/ui/library_panel.py（3 处）

- L800 `addAction`: "Rename"
- L801 `addAction`: "Delete"
- L802 `addAction`: "New Folder"

## src/shared/export/export_dialog.py（2 处）

- L100 `setSuffix`: " px"
- L249 `setSuffix`: " px"

## src/shared/ui/audio_mixer/preset_dialog.py（2 处）

- L246 `QPushButton()`: "A"
- L258 `QPushButton()`: "B"

## src/features/color/preferences.py（2 处）

- L214 `setPlaceholderText`: "config.ocio"
- L541 `setPlaceholderText`: "Select AI Model Directory"

## src/features/composite/preferences.py（2 处）

- L222 `setSuffix`: " GB"
- L190 `setToolTip`: "Install PyOpenColorIO (pip install opencolorio) to enable."

## src/features/edit/preferences.py（2 处）

- L238 `setPlaceholderText`: "Auto-select when only one DeckLink device is installed"
- L246 `setPlaceholderText`: "/usr/local/bin/ffmpeg"

## src/features/edit/ui/adr_take_sheet_panel.py（2 处）

- L363 `setSuffix`: " s"
- L370 `setSuffix`: " s"

## src/features/edit/ui/new_timeline_dialog.py（2 处）

- L233 `QLabel()`: "W"
- L247 `QLabel()`: "H"

## src/features/edit/ui/new_project_dialog.py（2 处）

- L135 `setSuffix`: " px"
- L141 `setSuffix`: " px"

## src/features/edit/ui/quick_project_dialog.py（2 处）

- L493 `setSuffix`: " px"
- L504 `setSuffix`: " px"

## src/features/edit/ui/edit_properties_panel.py（2 处）

- L3834 `setToolTip`: "Pick color from screen"
- L3874 `setToolTip`: "Swap primary and secondary colors"

## src/features/teaching_animation/ui/motion_graphic_editor_window.py（2 处）

- L2799 `setWindowTitle`: "Apply Template"
- L2816 `QPushButton()`: "Apply (default params)"

## src/features/deliver/ui/m21_multi_version_qc_panel.py（2 处）

- L54 `setWindowTitle`: "多版本交付 QC"
- L65 `QPushButton()`: "关闭"

## src/features/color/ui/crop_panel.py（2 处）

- L295 `QGroupBox()`: "ROTATION"
- L314 `setToolTip`: "重置旋转角度为 0 (快捷键：R)"

## src/features/color/ui/blur_sharpen_panel.py（2 处）

- L98 `setToolTip`: "Reset All Sharpen Parameters"
- L199 `setToolTip`: "Reset All Blur Parameters"

## src/features/ai_workflow/ui/workflow_inspector.py（2 处）

- L60 `QLabel()`: "<b>(no selection)</b>"
- L105 `setText`: "<b>(no selection)</b>"

## src/features/ai_workflow/ui/workflow_node_search.py（2 处）

- L46 `setWindowTitle`: "Add Node"
- L63 `setPlaceholderText`: "Search nodes…"

## src/app/main_window.py（1 处）

- L357 `setWindowTitle`: "Topos"

## src/shared/ui/playback_hud_integration.py（1 处）

- L221 `QAction()`: "显示播放 HUD"

## src/shared/ui/video_control_panel.py（1 处）

- L122 `QLabel()`: "帧: 0 / 0"

## src/shared/export/quick_export_dialog.py（1 处）

- L784 `setSuffix`: " 色"

## src/shared/ui/widgets/realtime_fps_indicator.py（1 处）

- L36 `setToolTip`: "Realtime preview FPS / 实时预览帧率"

## src/features/edit/ui/comp_clip_settings_dialog.py（1 处）

- L173 `setPlaceholderText`: "Comp Clip"

## src/features/edit/ui/stem_export_dialog.py（1 处）

- L258 `QLabel()`: "WAV · 48 kHz · 24 bit · 立体声"

## src/features/edit/ui/trim_numeric_dialog.py（1 处）

- L65 `setSuffix`: " f"

## src/features/edit/ui/timeline_panel.py（1 处）

- L2697 `setPlaceholderText`: "00:00:10:00 or 240"

## src/features/edit/ui/placeholder_dialog.py（1 处）

- L53 `setSuffix`: " s"

## src/features/composite/ui/minimap_widget.py（1 处）

- L63 `setToolTip`: "收起/展开小地图"

## src/features/composite/ui/keyframes_panel.py（1 处）

- L1224 `addAction`: "往复播放 (Ping-Pong)"

## src/features/teaching_animation/ui/page_timing_panel.py（1 处）

- L81 `setPlaceholderText`: "#111827 or CSS color"

## src/features/teaching_animation/ui/scene_timeline.py（1 处）

- L82 `QLabel()`: "0.00 s"

## src/features/color/ui/soft_clip_panel.py（1 处）

- L246 `setToolTip`: "Reset All Parameters"

## src/features/color/ui/raw_panel.py（1 处）

- L69 `setToolTip`: "Reset All RAW Parameters"

## src/features/color/ui/deblur_panel.py（1 处）

- L66 `setToolTip`: "Reset All Deblur Parameters"

## src/features/color/ui/denoise_panel.py（1 处）

- L91 `setToolTip`: "Reset All Denoise Parameters"

## src/features/color/ui/warper_panel.py（1 处）

- L40 `setToolTip`: "Reset Grid"

## src/app/ui/welcome_dialog.py（1 处）

- L95 `QLabel()`: "Topos"

## src/app/ui/project_manager_dialog.py（1 处）

- L216 `QLabel()`: "Topos"

## src/app/controllers/edit_controller.py（1 处）

- L7300 `setWindowTitle`: "Topos"

## src/app/controllers/file_controller.py（1 处）

- L715 `setWindowTitle`: "Topos"

## 漏网补充清单（2026-10-28 二轮扫描：addMenu/getText/getItem/_Q 别名构造器）

> 首轮 AST 只覆盖 5 setter+11 构造器+addAction，本节为二轮补扫结果，
> 归 P3 后续批次（当前未修复，登记在案）。

### src/features/composite/ui/composite_viewport.py（14 处）

- L499 `addMenu`: "3D Orbit Center"
- L528 `addMenu`: "Exposure"
- L547 `addMenu`: "Gamma"
- L562 `addMenu`: "Viewer Gain"
- L569 `addMenu`: "Viewer Lift"
- L576 `addMenu`: "Viewer Offset"
- L588 `addMenu`: "Display Transform"
- L611 `addMenu`: "Resolution"
- L649 `addMenu`: "A/B Compare"
- L678 `addMenu`: "Compare Source"
- L701 `addMenu`: "Magnifier"
- L729 `addMenu`: "Background"
- L3684 `addMenu`: "Channel"
- L3692 `addMenu`: "Exposure"

### src/features/composite/ui/node_editor_widget.py（9 处）

- L1776 `addMenu`: "Presets"
- L1786 `addMenu`: "Load Preset"
- L1827 `addMenu`: "Arrange"
- L1834 `addMenu`: "Align"
- L1849 `addMenu`: "Distribute"
- L5119 `addMenu`: "View Navigation"
- L5128 `addMenu`: "Bookmarks"
- L5138 `addMenu`: "Remove Bookmark"
- L5158 `addMenu`: "Graph Focus"

### src/features/color/ui/timeline_clip_strip.py（2 处）

- L1255 `addMenu`: "Preview Frame"
- L1276 `addMenu`: "Bottom Label"

### src/shared/ui/audio_mixer/audio_level_meter.py（1 处）

- L488 `addMenu`: "Meter Mode"

### src/features/edit/ui/timeline_view.py（1 处）

- L4145 `addMenu`: "Caption Graphic"

### src/features/edit/ui/timeline_items.py（1 处）

- L8228 `alias _QMenu=QMenu`: "Track Height Preset"

### src/features/composite/ui/sticky_note_item.py（1 处）

- L465 `addMenu`: "Preset Colors"

### src/features/composite/ui/keyframes_panel.py（1 处）

- L1215 `addMenu`: "播放速度"

### src/app/ui/library_panel.py（1 处）

- L798 `addMenu`: "Set Rating"
