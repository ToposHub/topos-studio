# Files U01 原生浏览验证

日期：2026-11-16。范围：原生素材库现代布局、真实缩略图 UI transport、兼容宿主接线。按用户补充，重新组织内容和操作，不复刻旧 Files 布局。

紧凑布局/Home目录树更新：76 passed。包括22项原生UI、29项HTTP transport、1项宿主切换、6项既有Files契约、18项共享分隔控件回归。此前47项记录对应现代卡片首版。

新增覆盖：可见素材惰性请求、4并发/128等待、2MiB响应限制、非法图片和占位响应、关闭/旧版本/旧目录丢弃、缓存淘汰后重取、129张可见图片重复绘制收敛、真实小图解码、冷缓存就绪自动重试、目录切换取消重试、480px窄布局、侧栏/信息栏开关保持选择。

```sh
PYTHONPATH=/tmp/topos-native-files-test-deps .venv/bin/python -m pytest \
  tests/ui/test_native_files_panel.py \
  tests/ui/test_native_files_transport.py \
  tests/ui/test_files_native_host.py \
  tests/architecture/test_file_explorer_integration.py \
  tests/ui/test_panel_splitter.py -q
```

当前 `.venv` 缺少 pytest/pytest-qt，因此仅将测试工具安装到 `/tmp/topos-native-files-test-deps` 后用项目 Python 3.13 执行，没有修改项目依赖。旧 Python 3.10 备份环境不能解析既有 `external/file_explorer/window.py` 的新式 f-string，未用它冒充有效测试环境。

截图使用 `capture_ui.py` 的明确展示夹具，不是真实磁盘、媒体解码或后端处理结果：

- `native_files_grid.png`：1120×650 逻辑尺寸，内容卡片、选中边框、信息栏。
- `native_files_search.png`：同尺寸，搜索图标展开路径栏内输入框。
- `native_files_list.png`：同尺寸详细列表。
- `native_files_narrow.png`：680×520，自动收起两侧辅助栏、简化工具栏。
- `native_files_compact.png`：480×560，保留主要检索/视图/导航；排序和选择目录仍可从更多菜单进入。

使用 `PYTHONPATH=. .venv/bin/python docs/validation/files_native_u01/capture_ui.py` 可重建截图。使用 Topos 主题与 ChevronComboBox；卡片采用统一封面区域、格式徽标、名称与辅助信息，信息栏结构化展示；选中以边框和轻底色表达，旧工具收进更多菜单。截图夹具未连接服务，明确展示分类占位；不把它冒充素材照片。

紧凑版删除了独立标题/搜索行，搜索默认折叠，点击放大镜在路径栏展开，Esc清空并收起；窄窗口展开时暂时让出路径与侧栏开关的空间，关闭恢复。使用共享PanelSplitter和C.PANEL_DIVIDER分隔功能区。左侧以Home为根惰性加载目录，沿用旧API的隐藏文件规则，并在UI中过滤准确的系统路径、Home/Library、Home/Applications及.app包；外接素材盘的同名普通目录保留。刷新同时重读当前树节点和右侧目录。新增搜索/黑线像素/Home过滤/树加载与失败重试/定向刷新测试。

真实运行时已接既有 `/api/thumb`。读取/解码失败显示分类图标；202/Logo占位单独视为生成中，仅当前可见素材按0.5/1.5/3/6/12秒延迟最多追加5次请求，切目录取消重试，超出次数后允许刷新。缓存以128张为基础动态覆盖可见集，淘汰非可见项，完成后只更新该条目，避免大窗口循环重取。全库完成事件、更多格式和实机性能仍待验收，不能把HTTP协议测试当真实素材全覆盖。

未验收：真实 QDrag 手势跨入媒体池/时间线、批次 Undo/Redo、真实播放器、Windows 路径及不同 DPI、万级目录性能。HTTP 测试用隔离的本地协议服务，验证鉴权头、字面 `+/%20/#/Unicode/换行` 路径、锁/错误/无效响应和晚到请求，不代替完整 Files 服务实机验收。

后端业务本轮未改。核心界面仍为预览入口；高级管理和预览通过完整 Files 保留。
