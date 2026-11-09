# Color Gallery 前端验证（2026-11-09）

参考本机 DaVinci Resolve Studio 20 Color Gallery。实际查看了缩略图、相册侧栏、搜索展开与列表模式；结束后恢复参考应用的缩略图／侧栏收起／搜索收起状态。

## 已实现的外观与交互

- 28px 单行工具栏，20px code-native vector 图标；相册、缩略图尺寸、排序、Grid／List、Search、Gallery View 和更多菜单。
- 缩放slider仅设置透明widget背景，轨道／橙色圆形handle／悬停和按下态继承全局`DARK_THEME`；不再单独画灰色滑块或背景块。
- `#28282e` 静帧背景、16:9 缩略图、图片外居中 caption、细黑框与珊瑚红选中描边；去掉旧方卡、渐变与效果彩点。
- 缩略图宽度 108–256px；普通 resize 只改变列数和留白，窄面板放不下时按可用宽度收缩，保持16:9。列表使用20px表头和27px行。
- 可折叠／拖宽的相册侧栏；当前 User 库为 Stills 1、System 库保留。PowerGrade／Timelines／Memories 没有模型，明确禁用，没有冒充跨工程集合。
- 搜索按需展开50px行、复用既有名称／来源metadata过滤；Escape清除并收起，Find只在Gallery内生效。排序复用真实文件名。
- Grid／List复用同一个QListWidget模型、item路径角色和selection；单击只选择，双击／Enter／中键复用原应用信号；多选删除仍走原确认。
- 保留抓帧、应用调色、分类应用、参考划像、Color Match、rename/delete和thumbnail export。导入完整still／grade bundle没有服务，禁用。
- 新绘制路径不再打开`.topos` zip读取效果圆点，保留原256项缩略图LRU；扫描／文件写入仍由原owner负责。
- 右侧滚动条按内容需要出现，空库、搜索无结果和内容放得下时隐藏；网格排布预估溢出，避免滚动条出现后反复换列或丢掉一列。

## 划像／分屏 UI

Gallery右键可直接加载参考划像；Gallery更多菜单与Viewport更多菜单可打开划像选择窗口，确认后发原`reference_requested(str)`，走既有高精参考链。

Viewport分屏图标和Gallery分屏菜单打开同一设置窗口：可选参考静帧，提供并排／2×2布局及缩略图布局示意。**Current Image格是示意占位，未加载处理后帧；此图不是实际分屏渲染证据。** 多图presenter未实现，Start Comparison保持禁用并给出原因，未让UI checked冒充画面结果。后端详细接线见[Gallery计划G13](../../plans/2026-11-09-color-gallery-backend-plan.md)。

## 图片

当前图片使用真实控件、应用`DARK_THEME`，从`tests/test_videos/2K_prores422.mov`读取第0／25／50／75／100／125／150／175帧，临时写入缩略图和来源metadata；`.topos`仍为临时fixture。离线QA脚本使用CPU解码，产品UI没有新增同步解码。未读取／改写用户真实Gallery，未创建GPU renderer；缺该测试视频时脚本明确失败，不用别的图伪装。

| 图片 | 内容 |
|---|---|
| [504px Grid](gallery_504x440_grid.png) | 与参考Gallery约同宽，默认108×61缩略图 |
| [相册 Grid](gallery_700x500_grid_albums.png) | 侧栏与真实目录入口 |
| [相册 List](gallery_700x500_list_albums.png) | 三列表头、27px行；缺少Label／Timeline metadata的列留空 |
| [Search](gallery_504x440_grid_search.png) | 50px搜索展开行 |
| [280px Grid](gallery_280x360_grid.png) | 窄面板保留核心入口，可选工具收进更多菜单 |
| [空画廊](gallery_empty_504x440.png) | 新建的空库，右侧没有常驻滚动条；搜索无匹配的路径另有行为测试 |
| [划像选择](comparison_wipe.png) | 单选真实still路径，原reference信号 |
| [分屏设置](comparison_split.png) | 布局示意、参考选择和待接通状态 |

重建图片：

```sh
.venv/bin/python docs/validation/color_gallery_20261109/render_preview.py
```

## 自动检查

```sh
.venv/bin/python -m pytest \
  tests/color/test_gallery_chrome.py \
  tests/color/test_presets_gallery.py \
  tests/ui/test_column_grid_panels.py \
  tests/color/test_ab_reference_wipe.py \
  tests/app/test_color_viewport_capability_host.py \
  tests/color/test_color_viewport_chrome.py \
  tests/shared/test_ef17_aud.py \
  tests/color/test_fix_wd4g23_aud.py \
  -q --disable-warnings --maxfail=3
```

结果：最新 **230 passed，12.45s**。覆盖宽窄几何、Grid／List选择保留、搜索取消、空库／过滤／缩窗的滚动条显示、全局slider样式、双击单次应用、无paint zip I/O、比较设置与原信号、HiDPI图标、原文件操作兼容及参考wipe回归。

`git diff --check`与模块编译检查通过。原生macOS启动检查退出码**0**，使用`/tmp/topos-gallery-ui-startup/config.json`隔离配置，未覆盖用户配置；日志仍有已有缩略图FFmpeg路径未找到及可选模型缺失提示。未将离屏图片作为真实HDR／4K／分屏管线通过证据。系统字体、DPI、原生窗口边框与参考应用的像素差分尚未验证为零。
