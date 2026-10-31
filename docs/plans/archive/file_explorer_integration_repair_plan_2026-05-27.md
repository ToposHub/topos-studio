# File Explorer 集成修复与优化计划（2026-05-27）

> 背景：当前项目经过 `src` 模块化拆分后，`external/file_explorer` 已经无法在 `Files` 面板访问。  
> 本计划先恢复可用性，再把集成方式整理成稳定的 app adapter/facade，避免后续拆 `MainWindow` 或禁用功能时再次破坏素材管理。

---

## 0. 当前结论

### 0.1 位置判断

`external/file_explorer` 本质是应用级素材管理能力，不属于单独的 `color`、`edit`、`composite` 或 `deliver`。

因此它集成在全局 `Files` 面板是正确的：

```text
src/app/ui/files_panel.py
  -> 嵌入 external/file_explorer 的 Web UI
  -> 与 LibraryPanel 做目录、星级、打开素材联动
```

不建议把它迁到：

```text
src/features/edit/
src/features/color/
src/features/composite/
```

否则素材管理会被误认为某个单独功能的附属能力，未来禁用/删除某个 feature 时会制造新的依赖混乱。

### 0.2 当前不可访问的直接原因

对比 `dev` 原始版本后，根因基本确定：

`dev` 原始文件：

```text
src/ui/library_adapter.py
```

当前拆分后文件：

```text
src/app/ui/library_adapter.py
```

但是 `library_adapter.py` 里的项目根目录推导逻辑仍然沿用旧层级。

当前代码大意：

```python
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(CURRENT_DIR)
ROOT = os.path.dirname(SRC_DIR)
external_path = os.path.join(ROOT, "external")
file_explorer_path = os.path.join(external_path, "file_explorer")
```

在旧路径 `src/ui/library_adapter.py` 中：

```text
CURRENT_DIR = <repo>/src/ui
SRC_DIR     = <repo>/src
ROOT        = <repo>
external    = <repo>/external
```

这是正确的。

在当前路径 `src/app/ui/library_adapter.py` 中：

```text
CURRENT_DIR = <repo>/src/app/ui
SRC_DIR     = <repo>/src/app
ROOT        = <repo>/src
external    = <repo>/src/external
```

这是错误的。

实际项目里存在的是：

```text
<repo>/external/file_explorer
```

当前代码会去找：

```text
<repo>/src/external/file_explorer
```

所以导入失败。

验证命令：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
p = Path("src/app/ui/library_adapter.py").resolve()
current = p.parent
src_dir = current.parent
root = src_dir.parent
print("computed_root=", root)
print("computed_external_exists=", (root / "external").exists())
print("actual_external_exists=", (Path.cwd() / "external").exists())
import src.app.ui.library_adapter as la
print("file_explorer_app=", la.file_explorer_app)
PY
```

当前结果要点：

```text
computed_root= .../topos-color02/src
computed_external_exists= False
actual_external_exists= True
file_explorer_app= None
```

### 0.3 当前代码的第二个风险

为了让拆分后的项目不在导入时崩溃，当前版本把 `file_explorer` 导入改成了 optional：

```python
try:
    from file_explorer.main import app as file_explorer_app, find_available_port
except ImportError:
    file_explorer_app = None
    find_available_port = None
```

这个做法本身可以作为降级机制，但现在它掩盖了路径错误。

结果是：

```text
FilesPanel 能创建
LibraryAdapter 也能创建
但 file_explorer_app = None
start_server() 直接 return
QWebEngineView 加载不到真正的素材浏览页面
```

所以用户看到的是 file_explorer 完全不可访问，而不是一个明确的路径错误。

---

## 1. 设计目标

### 1.1 必须恢复的功能

修复后必须恢复以下能力：

1. `Files` 面板能正常打开 `external/file_explorer` 页面。
2. Flask server 能在后台启动。
3. `QWebEngineView` 加载的 URL 可访问。
4. `Files -> Library` 目录同步正常。
5. `Library -> Files` 目录同步正常。
6. `Files -> Library` 星级同步正常。
7. `Library -> Files` 星级同步正常。
8. Files 面板中打开图片仍然进入主调色加载流程。
9. Files 面板中播放视频仍然调用主窗口的 `play_video_in_panel()`。
10. 添加素材库、打开目录对话框仍然可用。

### 1.2 独立软件 + 嵌入扩展双模式目标

`file_explorer` 后续应按“可独立发布的软件”来设计，而不是 Topos Color 内部的一组散文件。

目标形态：

```text
file_explorer/
  独立运行：素材管理软件
  嵌入运行：Topos Color 的 Files 扩展
```

独立模式：

```bash
python external/file_explorer/main.py
```

预期行为：

```text
启动自己的窗口
使用自己的配置、数据库、缓存目录
可以作为独立素材管理软件开发、测试、发布
```

嵌入模式：

```text
Topos Color
  -> FileExplorerAdapter
  -> external/file_explorer
  -> QWebEngineView
```

预期行为：

```text
Topos Color 只负责嵌入 file_explorer
Topos Color 只接收 file_explorer 发出的素材事件
Topos Color 不深入依赖 file_explorer 内部实现
```

最终升级方式应该是：

```text
1. 单独升级/开发 file_explorer
2. 替换 Topos Color 的 external/file_explorer 文件夹
3. 跑集成测试
4. 打开 Files 面板验收
```

也就是说，未来升级不应该需要在 Topos Color 里到处改 import 或改 UI 逻辑。

### 1.3 架构目标

最终结构建议：

```text
src/
  app/
    integrations/
      file_explorer/
        __init__.py
        adapter.py              # 唯一负责 external/file_explorer 的导入、路径、server 生命周期
        bridge.py               # 统一封装 file_explorer bridge 信号
        contract.py             # 对 FilesPanel 暴露的接口/状态对象
    ui/
      files_panel.py            # 只负责 WebView、UI 生命周期、对外信号
      library_panel.py          # 保持当前素材库面板，不直接碰 external/file_explorer
```

如果暂时不想新增太多文件，也可以先做最小版本：

```text
src/app/ui/library_adapter.py   # 先修路径，恢复访问
src/app/ui/files_panel.py       # 暂时保留现状
```

但长期建议迁到：

```text
src/app/integrations/file_explorer/adapter.py
```

因为 `library_adapter.py` 这个名字已经不准确，它实际管理的是 `external/file_explorer` server。

---

## 2. file_explorer 独立发布契约

### 2.1 核心原则

为了实现“拷贝出来就是独立素材管理软件，放进 `external` 就是 Topos Color 扩展”，必须给 `file_explorer` 建立稳定的对外契约。

Topos Color 不应该直接依赖：

```python
from file_explorer.main import bridge, load_config, save_config, app
```

因为这些都是内部实现细节。`main.py` 更适合做独立启动入口，不适合作为宿主集成 API。

建议 `file_explorer` 后续提供一个稳定入口：

```text
external/file_explorer/integration.py
```

Topos Color 只允许依赖：

```python
from file_explorer.integration import create_embedded_runtime
```

### 2.2 建议的 file_explorer 内部结构

长期建议把 `external/file_explorer` 整理成：

```text
file_explorer/
  __init__.py
  main.py                  # 独立软件入口，只负责 standalone 启动
  integration.py           # 宿主软件嵌入入口，稳定 API
  app_factory.py           # 创建 Flask app
  bridge.py                # AppBridge / Qt bridge 信号
  config_api.py            # load_config/save_config
  runtime.py               # EmbeddedRuntime / StandaloneRuntime
  player.py
  plugin_system.py
  thumb_engine.py
  utils.py
  static/
  templates/
  plugins/
```

第一轮不一定要全部拆完，但最终目标应该是：

```text
main.py 不再是 Topos Color 的集成入口
integration.py 才是 Topos Color 的唯一稳定入口
```

### 2.3 建议的嵌入 API

建议 `integration.py` 暴露：

```python
INTEGRATION_API_VERSION = "1.0"


def create_embedded_runtime(
    *,
    host_app: str,
    project_root,
    config_dir=None,
    db_path=None,
    cache_dir=None,
):
    ...
```

返回对象建议：

```python
class EmbeddedRuntime:
    app: object
    bridge: object

    def start_server(self, host: str = "127.0.0.1", port: int = 8188) -> str: ...
    def stop_server(self) -> None: ...
    def get_url(self) -> str: ...
    def load_config(self) -> dict: ...
    def save_config(self, config: dict) -> None: ...
    def is_available(self) -> bool: ...
```

Topos Color 侧 adapter 只依赖这个对象，不依赖 `main.py` 内部变量。

### 2.4 独立模式与嵌入模式的资源边界

必须明确区分以下路径：

```text
独立模式：
  config_dir = file_explorer 自己的用户配置目录
  db_path    = file_explorer 自己的素材数据库
  cache_dir  = file_explorer 自己的缩略图缓存

嵌入模式：
  config_dir = Topos Color 传入
  db_path    = Topos Color 传入，或显式共享 metadata/topos db
  cache_dir  = Topos Color 传入，避免写进 src
```

不要在 `file_explorer` 内部硬编码：

```text
Topos Color 项目根目录
Topos Color 的 config.json
Topos Color 的 metadata.db/topos.db
```

嵌入时需要共享什么，都由 Topos Color 的 `FileExplorerAdapter` 明确传入。

### 2.5 可替换升级验收标准

当 `file_explorer` 作为可替换子应用后，升级流程应是：

```text
1. 备份 external/file_explorer
2. 替换为新版 file_explorer 文件夹
3. 运行 Topos Color 的 file_explorer 集成测试
4. 打开 Topos Color 的 Files 面板手动验收
```

必须通过：

```bash
.venv/bin/python -m pytest tests/test_file_explorer_integration.py -q
```

并且手动确认：

```text
Files 面板能打开
目录同步正常
星级同步正常
打开图片正常
播放视频正常
添加素材库正常
```

### 2.6 不允许的耦合

完成这轮优化后，长期不允许出现：

```text
src/features/* -> file_explorer
src/shared/* -> file_explorer
src/app/ui/files_panel.py -> file_explorer.main
src/app/main_window.py -> file_explorer.main
```

唯一允许：

```text
src/app/integrations/file_explorer/adapter.py -> file_explorer.integration
```

过渡期可以短暂允许：

```text
src/app/integrations/file_explorer/adapter.py -> file_explorer.main
```

但必须在计划中标记为兼容层，不作为最终形态。

---

## 3. 不允许破坏的现有联动接口

当前 `FilesPanel` 与 `LibraryPanel` 的联动发生在 `src/app/main_window.py::setup_connections()`。

现有联动：

```python
self.files_panel.bridge.imageOpened.connect(self.load_image)
self.files_panel.bridge.directoryChanged.connect(self.library_panel.set_root_path)
self.library_panel.directory_changed.connect(self.files_panel.set_root_path)
self.files_panel.bridge.ratingChanged.connect(self.library_panel.refresh_file_rating)
self.library_panel.rating_changed.connect(self.files_panel.refresh_file_rating)
```

优化时必须保留等价能力。

更推荐的目标接口：

```python
self.files_panel.image_opened.connect(self.load_image)
self.files_panel.directory_changed.connect(self.library_panel.set_root_path)
self.library_panel.directory_changed.connect(self.files_panel.set_root_path)
self.files_panel.rating_changed.connect(self.library_panel.refresh_file_rating)
self.library_panel.rating_changed.connect(self.files_panel.refresh_file_rating)
```

也就是说：

```text
MainWindow 不应该再知道 files_panel.bridge
MainWindow 只应该知道 FilesPanel 自己暴露的公开信号
```

但这个可以作为第二阶段做。第一阶段先恢复访问，不要顺手大改联动。

---

## 4. Phase A：立即恢复 file_explorer 可访问

### 目标

只做最小修复，让当前拆分后的项目重新能访问 `external/file_explorer`。

### 修改文件

```text
src/app/ui/library_adapter.py
```

### 修复方案

不要再通过固定 `dirname(dirname(...))` 推导项目根目录。改成从当前文件向上查找含有 `external/file_explorer/main.py` 的目录。

建议实现：

```python
from pathlib import Path


def _find_project_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "external" / "file_explorer" / "main.py").exists():
            return candidate
    raise RuntimeError("Cannot locate external/file_explorer from library_adapter.py")


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
external_path = PROJECT_ROOT / "external"
file_explorer_path = external_path / "file_explorer"
```

然后把 `sys.path` 注入改成字符串：

```python
for path in (external_path, file_explorer_path):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
```

### 为什么这样修

这样即使未来文件从：

```text
src/app/ui/library_adapter.py
```

移动到：

```text
src/app/integrations/file_explorer/adapter.py
```

也不会再次因为目录层级变化而找不到 `external/file_explorer`。

### ImportError 处理建议

当前 optional import 可以保留，但日志必须更明确。

建议从：

```python
logger.warning("file_explorer or werkzeug not available, cannot start server")
```

增强为：

```python
logger.exception(
    "file_explorer integration unavailable. expected_path=%s sys_path_head=%s",
    file_explorer_path,
    sys.path[:5],
)
```

如果担心日志太吵，可以在 import 阶段保存错误对象：

```python
_file_explorer_import_error = None
try:
    from file_explorer.main import app as file_explorer_app, find_available_port
except ImportError as exc:
    file_explorer_app = None
    find_available_port = None
    _file_explorer_import_error = exc
```

`start_server()` 里打印：

```python
if file_explorer_app is None or make_server is None:
    logger.warning(
        "Cannot start file_explorer server: app=%s make_server=%s import_error=%r path=%s",
        file_explorer_app,
        make_server,
        _file_explorer_import_error,
        file_explorer_path,
    )
    return False
```

同时建议让 `start_server()` 返回 `bool`：

```python
def start_server(self) -> bool:
    ...
    return True
```

### 验证命令

```bash
.venv/bin/python - <<'PY'
import src.app.ui.library_adapter as la
print("external_path=", la.external_path)
print("file_explorer_path=", la.file_explorer_path)
print("file_explorer_app_is_none=", la.file_explorer_app is None)
print("find_available_port_is_none=", la.find_available_port is None)
print("make_server_is_none=", la.make_server is None)
assert la.file_explorer_app is not None
assert la.find_available_port is not None
assert la.make_server is not None
PY
```

预期：

```text
file_explorer_app_is_none= False
find_available_port_is_none= False
make_server_is_none= False
```

### UI 验证

启动应用后检查：

1. Files 面板不再空白。
2. Files 面板能显示 file_explorer 首页。
3. 控制台没有 `file_explorer or werkzeug not available`。
4. 切换页面后 Files 面板仍能刷新布局。
5. 关闭应用时 Flask worker 能正常停止。

### 完成标准

满足以下条件才算 Phase A 完成：

```text
LibraryAdapter 能定位 <repo>/external/file_explorer
file_explorer_app is not None
FilesPanel 能加载 http://127.0.0.1:<port>
FilesPanel 不再显示空白/灰块
```

---

## 5. Phase B：补一组最小自动化测试，防止再次搬家后失效

### 目标

把这次路径错误变成测试可防止的问题。

### 新增测试文件

```text
tests/test_file_explorer_integration.py
```

### 测试 1：adapter 能找到真实 external/file_explorer

建议测试：

```python
def test_file_explorer_adapter_paths_exist():
    from src.app.ui import library_adapter

    assert library_adapter.external_path.exists()
    assert library_adapter.file_explorer_path.exists()
    assert (library_adapter.file_explorer_path / "main.py").exists()
```

如果暂时还是字符串路径，则：

```python
from pathlib import Path

assert Path(library_adapter.external_path).exists()
assert Path(library_adapter.file_explorer_path, "main.py").exists()
```

### 测试 2：导入 file_explorer Flask app 成功

```python
def test_file_explorer_imports_flask_app():
    from src.app.ui import library_adapter

    assert library_adapter.file_explorer_app is not None
    assert library_adapter.find_available_port is not None
```

### 测试 3：FilesPanel 公开同步接口存在

这个测试不需要真的启动 WebEngine，只检查类接口。

```python
def test_files_panel_public_sync_contract():
    from src.app.ui.files_panel import FilesPanel

    assert hasattr(FilesPanel, "set_root_path")
    assert hasattr(FilesPanel, "refresh_file_rating")
```

如果后续给 `FilesPanel` 增加公开信号：

```python
assert hasattr(panel, "directory_changed")
assert hasattr(panel, "image_opened")
assert hasattr(panel, "rating_changed")
```

### 验证命令

```bash
.venv/bin/python -m pytest tests/test_file_explorer_integration.py -q
```

预期：

```text
passed
```

### 完成标准

未来无论 `library_adapter.py` 移到哪里，只要找不到 `external/file_explorer/main.py`，测试必须失败。

---

## 6. Phase C：把 file_explorer 集成层从 `ui` 中拆出来

### 目标

让 `FilesPanel` 不直接依赖 `file_explorer.main` 的内部对象。

当前不理想依赖：

```python
from file_explorer.main import bridge as fe_bridge, load_config, save_config
```

目标：

```python
from src.app.integrations.file_explorer.adapter import FileExplorerAdapter
```

### 建议新增目录

```text
src/app/integrations/
  __init__.py
  file_explorer/
    __init__.py
    adapter.py
```

### `adapter.py` 职责

`FileExplorerAdapter` 负责：

1. 定位 `external/file_explorer`。
2. 注入必要 `sys.path`。
3. 导入 Flask app、bridge、load_config、save_config、find_available_port。
4. 管理 Flask worker 生命周期。
5. 暴露稳定方法：

```python
class FileExplorerAdapter(QObject):
    add_library_requested = pyqtSignal()
    open_folder_requested = pyqtSignal()
    play_video_requested = pyqtSignal(str)

    @classmethod
    def instance(cls) -> "FileExplorerAdapter": ...

    def start_server(self) -> bool: ...
    def stop_server(self) -> None: ...
    def get_server_url(self) -> str: ...
    def is_available(self) -> bool: ...
    def load_config(self) -> dict: ...
    def save_config(self, config: dict) -> None: ...
```

### `FilesPanel` 目标职责

`FilesPanel` 只保留：

1. 创建 `QWebEngineView`。
2. 注册 `SyncBridge`。
3. 加载 adapter 提供的 URL。
4. 把 JS 回调转成 Qt 信号。
5. 处理 UI 对话框。
6. 暴露 `set_root_path()` 和 `refresh_file_rating()`。

`FilesPanel` 不再直接：

```python
from file_explorer.main import ...
```

### 迁移步骤

#### C1. 复制现有 `LibraryAdapter` 为新 adapter

新增：

```text
src/app/integrations/file_explorer/adapter.py
```

先从 `src/app/ui/library_adapter.py` 平移逻辑。

#### C2. 保留兼容 wrapper

为了降低风险，暂时保留：

```text
src/app/ui/library_adapter.py
```

内容改成：

```python
from src.app.integrations.file_explorer.adapter import FileExplorerAdapter

LibraryAdapter = FileExplorerAdapter
```

这样旧 import 不会立刻断。

#### C3. 修改 FilesPanel import

从：

```python
from src.app.ui.library_adapter import LibraryAdapter
```

改为：

```python
from src.app.integrations.file_explorer.adapter import FileExplorerAdapter
```

#### C4. 移除 FilesPanel 对 `file_explorer.main` 的直接 import

从：

```python
from file_explorer.main import bridge as fe_bridge, load_config, save_config
```

改为通过 adapter：

```python
self.adapter = FileExplorerAdapter.instance()
self.adapter.add_library_requested.connect(self._handle_add_library)
self.adapter.open_folder_requested.connect(self._handle_open_folder_dialog)
self.adapter.play_video_requested.connect(self._handle_play_video)
```

`_handle_add_library()` 里：

```python
conf = self.adapter.load_config()
...
self.adapter.save_config({"libraries": libs})
```

### 验证命令

```bash
.venv/bin/python -m compileall -q src/app/ui src/app/integrations
.venv/bin/python -m pytest tests/test_file_explorer_integration.py -q
.venv/bin/python -m pytest tests/test_page_context.py tests/test_feature_flags.py -q
```

### UI 验证

1. 打开 Files 面板。
2. 点击 file_explorer 里的添加素材库按钮。
3. 弹出原生目录选择框。
4. 添加成功后前端素材库列表刷新。
5. 打开目录按钮仍能跳转。
6. 视频播放按钮仍能调用 `MainWindow.play_video_in_panel()`。

### 完成标准

```text
src/app/ui/files_panel.py 不再出现 "from file_explorer.main import"
src/app/integrations/file_explorer/adapter.py 是唯一直接依赖 external/file_explorer 的 app 代码
```

---

## 7. Phase D：整理 FilesPanel 的公开信号，保护 Library 联动

### 目标

`MainWindow` 不再访问 `files_panel.bridge`。

当前：

```python
self.files_panel.bridge.imageOpened.connect(self.load_image)
self.files_panel.bridge.directoryChanged.connect(self.library_panel.set_root_path)
self.files_panel.bridge.ratingChanged.connect(self.library_panel.refresh_file_rating)
```

目标：

```python
self.files_panel.image_opened.connect(self.load_image)
self.files_panel.directory_changed.connect(self.library_panel.set_root_path)
self.files_panel.rating_changed.connect(self.library_panel.refresh_file_rating)
```

### 修改文件

```text
src/app/ui/files_panel.py
src/app/main_window.py
```

### FilesPanel 新增公开信号

```python
class FilesPanel(QWidget):
    directory_changed = pyqtSignal(str)
    image_opened = pyqtSignal(str)
    video_opened = pyqtSignal(str)
    rating_changed = pyqtSignal(str, int)
```

在 `__init__` 中桥接：

```python
self.bridge.directoryChanged.connect(self.directory_changed)
self.bridge.imageOpened.connect(self.image_opened)
self.bridge.videoOpened.connect(self.video_opened)
self.bridge.ratingChanged.connect(self.rating_changed)
self.video_opened.connect(self._handle_play_video)
```

### MainWindow 修改

从：

```python
self.files_panel.bridge.imageOpened.connect(self.load_image)
self.files_panel.bridge.directoryChanged.connect(self.library_panel.set_root_path)
self.files_panel.bridge.ratingChanged.connect(self.library_panel.refresh_file_rating)
```

改为：

```python
self.files_panel.image_opened.connect(self.load_image)
self.files_panel.directory_changed.connect(self.library_panel.set_root_path)
self.files_panel.rating_changed.connect(self.library_panel.refresh_file_rating)
```

### 验证命令

```bash
.venv/bin/python -m pytest tests/test_page_context.py -q
.venv/bin/python -m pytest tests/test_file_explorer_integration.py -q
```

### 手动验证

1. 在 Files 面板进入某个文件夹。
2. Library 面板路径同步变化。
3. 在 Library 面板进入另一个文件夹。
4. Files 面板路径同步变化。
5. 在 Files 面板修改星级。
6. Library 面板对应素材星级刷新。
7. 在 Library 面板修改星级。
8. Files 面板对应素材星级刷新。

### 完成标准

```bash
rg -n "files_panel\\.bridge" src/app/main_window.py
```

预期无结果。

---

## 8. Phase E：处理启动失败时的用户可见降级

### 目标

如果 file_explorer 依赖缺失、端口失败、server 启动失败，不要只显示空白 WebView。

### 建议行为

`FilesPanel` 中如果：

```python
started = self.adapter.start_server()
```

返回 `False`，则显示一个轻量错误页或 QLabel：

```text
素材浏览器启动失败
请检查 external/file_explorer 是否存在，以及 Flask / werkzeug / PyQtWebEngine 是否安装。
```

不要让用户只看到空白或灰块。

### 实现方式

可以先简单使用 `QWebEngineView.setHtml()`：

```python
self.browser.setHtml("""
<html>
  <body style="background:#121212;color:#ddd;font-family:sans-serif;padding:16px;">
    <h3>素材浏览器启动失败</h3>
    <p>external/file_explorer 未能加载，请查看日志。</p>
  </body>
</html>
""")
```

### 验证

临时把 `external/file_explorer` 改名或用 monkeypatch 让 adapter unavailable，确认 Files 面板显示错误页，而不是空白。

自动化测试可以后续补，不作为第一轮必须项。

---

## 9. Phase F：更新文档和架构说明

### 修改文档

建议更新：

```text
docs/post_ai_completion_remaining_plan_2026-05-27.md
docs/src_directory_simplification_plan_2026-05-26.md
```

新增说明：

```text
external/file_explorer 是 app-level integration，不属于 features/*
FilesPanel 是 UI 容器
FileExplorerAdapter 是 external 集成边界
LibraryPanel 是原生素材库视图
MainWindow 只负责连接公开信号
```

### 建议加入边界规则

未来边界目标：

```text
允许：
src/app/integrations/file_explorer/* -> external/file_explorer

不允许：
src/app/ui/files_panel.py -> file_explorer.main
src/features/* -> external/file_explorer
src/shared/* -> external/file_explorer
```

可以后续加入测试：

```text
tests/test_file_explorer_boundaries.py
```

规则：

```python
def test_only_adapter_imports_file_explorer():
    ...
```

---

## 10. Phase G：把 file_explorer 整理成可替换子应用

### 目标

实现用户期望的最终形态：

```text
file_explorer 文件夹拷贝出来就是独立素材管理软件
file_explorer 放进 external 目录就是 Topos Color 的一个扩展
升级 file_explorer 时可以直接替换 external/file_explorer
```

### G1. 在 file_explorer 内新增稳定嵌入入口

新增：

```text
external/file_explorer/integration.py
```

第一版可以先做兼容封装，不大拆内部：

```python
INTEGRATION_API_VERSION = "1.0"


def create_embedded_runtime(**kwargs):
    from .main import app, bridge, load_config, save_config, find_available_port
    return EmbeddedRuntime(
        app=app,
        bridge=bridge,
        load_config=load_config,
        save_config=save_config,
        find_available_port=find_available_port,
        options=kwargs,
    )
```

注意：这只是过渡版本。最终要把 `main.py` 中的 app/bridge/config 初始化拆到 `app_factory.py`、`bridge.py`、`config_api.py`。

### G2. Topos Color adapter 改为依赖 integration.py

修改：

```text
src/app/integrations/file_explorer/adapter.py
```

从：

```python
from file_explorer.main import app, bridge, load_config, save_config
```

改为：

```python
from file_explorer.integration import create_embedded_runtime
```

然后：

```python
self.runtime = create_embedded_runtime(
    host_app="topos_color",
    project_root=PROJECT_ROOT,
    config_dir=...,
    db_path=...,
    cache_dir=...,
)
```

### G3. 保留独立启动入口

`external/file_explorer/main.py` 必须仍能独立运行：

```bash
.venv/bin/python external/file_explorer/main.py
```

如果独立运行依赖系统 GUI，可以至少保证以下命令不因 import 失败退出：

```bash
.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "external/file_explorer")
import main
print("standalone main import ok")
PY
```

### G4. 加入 API 版本检查

Topos Color adapter 应检查：

```python
INTEGRATION_API_VERSION
```

建议规则：

```text
Topos Color 支持 file_explorer integration API 1.x
如果主版本不匹配，FilesPanel 显示明确错误页
```

这样以后替换新版 `file_explorer` 时，如果 API 不兼容，不会悄悄空白。

### G5. 替换升级测试

新增或扩展：

```text
tests/test_file_explorer_integration.py
```

测试内容：

```python
def test_file_explorer_exposes_stable_integration_api():
    import sys
    from src.app.integrations.file_explorer import adapter

    assert adapter.file_explorer_path.exists()
    sys.path.insert(0, str(adapter.file_explorer_path.parent))

    from file_explorer.integration import INTEGRATION_API_VERSION, create_embedded_runtime

    assert INTEGRATION_API_VERSION.startswith("1.")
    assert callable(create_embedded_runtime)
```

### G6. 文档要求

在 `external/file_explorer/README.md` 中新增：

```text
## Standalone / Embedded Modes

Standalone:
  python main.py

Embedded:
  from file_explorer.integration import create_embedded_runtime

Integration API version:
  1.0
```

### 完成标准

- [ ] `external/file_explorer/integration.py` 存在。
- [ ] `integration.py` 暴露 `INTEGRATION_API_VERSION`。
- [ ] `integration.py` 暴露 `create_embedded_runtime()`。
- [ ] Topos Color adapter 不再从 `file_explorer.main` 读取内部变量。
- [ ] `main.py` 仍能作为独立软件入口。
- [ ] 替换 `external/file_explorer` 后，只要 integration API 兼容，Topos Color 无需修改代码。

---

## 11. 推荐执行顺序

### Commit 1：恢复访问

只改：

```text
src/app/ui/library_adapter.py
tests/test_file_explorer_integration.py
```

目标：

```text
file_explorer_app 不再是 None
Files 面板能加载
```

验证：

```bash
.venv/bin/python -m pytest tests/test_file_explorer_integration.py -q
.venv/bin/python -m compileall -q src/app/ui
```

### Commit 2：抽 adapter

新增：

```text
src/app/integrations/file_explorer/adapter.py
src/app/integrations/file_explorer/__init__.py
src/app/integrations/__init__.py
```

修改：

```text
src/app/ui/library_adapter.py
src/app/ui/files_panel.py
```

验证：

```bash
.venv/bin/python -m compileall -q src/app
.venv/bin/python -m pytest tests/test_file_explorer_integration.py -q
```

### Commit 3：公开 FilesPanel 信号

修改：

```text
src/app/ui/files_panel.py
src/app/main_window.py
```

验证：

```bash
.venv/bin/python -m pytest tests/test_page_context.py tests/test_file_explorer_integration.py -q
```

手动验证 Files/Library 双向目录与星级同步。

### Commit 4：失败降级与文档

修改：

```text
src/app/ui/files_panel.py
docs/*
```

验证：

```bash
.venv/bin/python -m compileall -q src/app
```

### Commit 5：建立 file_explorer 独立/嵌入双模式契约

新增或修改：

```text
external/file_explorer/integration.py
external/file_explorer/README.md
src/app/integrations/file_explorer/adapter.py
tests/test_file_explorer_integration.py
```

验证：

```bash
.venv/bin/python -m pytest tests/test_file_explorer_integration.py -q
.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "external")
from file_explorer.integration import INTEGRATION_API_VERSION, create_embedded_runtime
print(INTEGRATION_API_VERSION, callable(create_embedded_runtime))
PY
```

---

## 12. 给下一个 AI 的执行提示

可以把下面这段直接给下一个 AI：

```text
请先修复 external/file_explorer 在模块化拆分后无法访问的问题。

项目：Topos Color，PyQt6 + QWebEngine + Flask。
当前分支：fix-ui。

已知根因：
src/app/ui/library_adapter.py 从 dev 的 src/ui/library_adapter.py 迁移后，仍使用旧的 dirname 层级计算 ROOT。
现在 ROOT 被算成 <repo>/src，导致 external_path 变成 <repo>/src/external，真实路径应该是 <repo>/external。

要求：
1. 不要大改 MainWindow。
2. 第一阶段只修复 LibraryAdapter 路径定位，让 file_explorer_app 不再是 None。
3. 用 pathlib 从 __file__ 向上查找 external/file_explorer/main.py，不要写死 dirname 层级。
4. 添加 tests/test_file_explorer_integration.py，验证 adapter 能找到 external/file_explorer，且 file_explorer_app/find_available_port 可导入。
5. 保持 FilesPanel 与 LibraryPanel 的目录同步、星级同步接口不变。
6. 通过后再考虑把 src/app/ui/library_adapter.py 迁到 src/app/integrations/file_explorer/adapter.py。
7. 长期目标是让 external/file_explorer 成为可独立运行、可直接替换升级的素材管理子应用。
8. 后续应在 external/file_explorer/integration.py 暴露 INTEGRATION_API_VERSION 和 create_embedded_runtime()，Topos Color 只依赖这个稳定入口，不直接依赖 file_explorer.main。

验证命令：
.venv/bin/python -m pytest tests/test_file_explorer_integration.py -q
.venv/bin/python -m compileall -q src/app/ui
```

---

## 13. 最终验收清单

全部完成后逐项确认：

- [ ] `file_explorer_app is not None`
- [ ] `find_available_port is not None`
- [ ] `make_server is not None`
- [ ] `external_path` 指向 `<repo>/external`
- [ ] `file_explorer_path` 指向 `<repo>/external/file_explorer`
- [ ] Files 面板能加载素材浏览页面
- [ ] Files 面板切目录后 Library 面板同步
- [ ] Library 面板切目录后 Files 面板同步
- [ ] Files 面板改星级后 Library 面板刷新
- [ ] Library 面板改星级后 Files 面板刷新
- [ ] Files 面板打开图片仍调用 `MainWindow.load_image`
- [ ] Files 面板播放视频仍调用 `MainWindow.play_video_in_panel`
- [ ] `src/app/ui/files_panel.py` 不再直接 import `file_explorer.main`（Phase C 完成后）
- [ ] `src/app/main_window.py` 不再访问 `files_panel.bridge`（Phase D 完成后）
- [ ] 有测试覆盖 adapter 路径定位，防止未来搬文件再次失效
- [ ] `external/file_explorer` 拷贝出来后仍可作为独立素材管理软件运行
- [ ] `external/file_explorer/integration.py` 提供稳定嵌入 API
- [ ] Topos Color 只通过 `file_explorer.integration` 集成 file_explorer
- [ ] 替换 `external/file_explorer` 后，只要 API 版本兼容，Topos Color 不需要改代码
