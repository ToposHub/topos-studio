# MyWebGallery (Pro)
**MyWebGallery** 是一个高性能、可扩展的混合桌面文件管理系统，专为影视后期、图形设计及多媒体从业者打造。
它结合了 **Python Flask** 的后端处理能力与 **PyQt6 (QtWebEngine)** 的桌面渲染能力，提供类似 macOS Finder 的流畅体验，并集成了专业的媒体播放与格式预览引擎。

## ✨ 核心特性
* **全格式预览引擎**:
* **视频**: 支持 ProRes, DNxHD, MXF, RAW Video (R3D, ARI) 等专业格式（基于 FFmpeg）。
* **图像**: 支持 OpenEXR, DPX, PSD, AI, HEIC, RAW Photo 等（基于 macOS QuickLook 与 Pillow）。Topos `.toos` 由 `topos_image` 插件直接调用原生解码器生成缩略图，并在灯箱中复用预览缓存。
* **文档**: 支持 Office (Docx/Xlsx), PDF, Markdown, 代码文件预览。

* **混合桌面交互**:
* 支持从软件内部直接**拖拽文件到桌面**或其他应用（如 PR/AE）。
* 支持系统级剪贴板交互（复制/粘贴进度监控）。
* 支持调用系统原生应用打开文件或在 Finder 中显示。

* **原生级媒体播放**: 内置基于 Qt Multimedia（自带 FFmpeg 后端）的浮窗播放器，支持 4K 高码率视频流畅播放、截图、片段截取。
* **多视图布局**: 瀑布流、自适应网格、标准网格、详细列表。
* **强大的插件系统**: 支持通过 Python + HTML/JS 扩展功能（如编辑器、转码器、自动化工具）。

---
## 🛠 技术栈

* **Core**: Python 3.10+, Flask
* **GUI**: PyQt6, QtWebEngine
* **Media**: Qt Multimedia (PyQt6 自带), FFmpeg, qlmanage (macOS)
* **Frontend**: HTML5, ES6+, Bootstrap 5
* **System**: macOS (Primary optimization), Windows (Compatible)

---

## 🔌 插件开发指南 (Plugin Development Guide)

本系统采用 **非侵入式** 插件架构。AI 或开发者无需修改主程序代码，只需在 `plugins/` 目录下创建文件夹即可扩展功能。

### 1. 插件目录结构

每个插件必须是一个独立的文件夹，放置于 `plugins/` 目录下：

```text
plugins/
└── my_cool_plugin/          # 插件唯一标识 ID (文件夹名)
    ├── __init__.py          # [必须] 插件入口 (Python)
    ├── config.json          # [可选] 插件元数据 (名称, 版本)
    ├── static/              # [可选] 前端资源 (JS/CSS)
    │   └── plugin.js
    └── templates/           # [可选] HTML 模板
        └── widget.html

```

### 2. Python 入口 (`__init__.py`)

`__init__.py` 必须包含 `setup(manager)` 函数。

#### 核心 API (`manager` 对象)

在 `setup(manager)` 中，你可以调用以下方法将插件注册到主程序：

| 方法名 | 描述 | 参数示例 |
| --- | --- | --- |
| `manager.register_js(path)` | 注入前端 JS 文件 | `'/plugin/my_id/static/main.js'` |
| `manager.register_file_handler(exts, js_func)` | 接管特定格式文件的打开动作 | `['.md'], 'MyPlugin.open'` |
| `manager.register_hook(event, callback)` | 监听系统事件 | `'after_upload', my_callback_func'` |
| `manager.register_ui_component(slot, html)` | 在界面指定位置插入 HTML | `'sidebar_bottom', '<div>...</div>'` |
| `app.register_blueprint(bp)` | 注册 Flask 路由 (后端 API) | `url_prefix='/plugin/my_id'` |

#### 代码示例

```python
from flask import Blueprint, jsonify

# 1. 定义蓝图 (用于提供插件自己的 API)
bp = Blueprint('my_plugin', __name__, static_folder='static', template_folder='templates')

# 2. 定义初始化函数
def setup(manager):
    # 注册前端脚本
    manager.register_js('/plugin/my_cool_plugin/static/plugin.js')
    
    # 注册 UI 插槽：在侧边栏底部加一个按钮
    btn_html = '<button onclick="MyPlugin.hello()">点击我</button>'
    manager.register_ui_component('sidebar_bottom', btn_html)
    
    # 注册文件处理器：接管 .log 文件
    manager.register_file_handler(['.log', '.txt'], 'MyPlugin.openEditor')
    
    # 注册钩子：监听文件上传完成
    manager.register_hook('after_upload', on_file_uploaded)

# 3. 钩子回调
def on_file_uploaded(file_path):
    print(f"插件监测到新文件: {file_path}")
    # 可以在这里做自动压缩、水印等处理

# 4. 插件 API
@bp.route('/api/do_something')
def api_action():
    return jsonify({'status': 'ok'})

```

### 3. 支持的事件钩子 (Hooks)

| 事件名 (`event_name`) | 触发时机 | 回调参数 (`kwargs`) |
| --- | --- | --- |
| `after_upload` | 文件上传并保存成功后 | `file_path` (str): 文件的绝对路径 |

### 4. 支持的 UI 插槽 (UI Slots)

| 插槽名 (`slot_name`) | 位置描述 | 用途建议 |
| --- | --- | --- |
| `sidebar_bottom` | 左侧侧边栏的最底部 | 放置全局工具、状态监控面板 |
| `top_menu` | 顶部菜单栏“插件”下拉菜单中 | 放置“新建文件”、“全局设置”等功能入口 |

### 5. 前端交互协议 (JS)

插件的前端代码 (`static/plugin.js`) 运行在主窗口的全局作用域中。

* **全局状态访问**:
* `STATE.currentPath`: 当前浏览的文件夹路径。
* `STATE.selected`: 当前选中的文件集合 (Set)。
* `STATE.allFiles`: 当前目录下的文件列表数据。


* **常用全局函数**:
* `reloadFiles()`: 刷新当前文件列表。
* `Toastify({text: "msg"}).showToast()`: 显示通知气泡。



#### 文件处理器示例 (JS)

如果在 Python 中注册了 `MyPlugin.openEditor`，则需要在 JS 中实现它：

```javascript
window.MyPlugin = {
    openEditor: function(filePath) {
        console.log("正在通过插件打开文件:", filePath);
        // 这里可以弹出一个模态框 iframe，指向插件的 Flask 路由
        // 或者直接 fetch 插件 API 获取内容显示
        fetch('/plugin/my_cool_plugin/api/read?p=' + encodeURIComponent(filePath))
            .then(res => res.json())
            .then(data => alert(data.content));
    },
    
    hello: function() {
        alert("Hello from Plugin!");
    }
};

```

---

## 📂 项目结构概览

```
file_explorer/
├── main.py              # [核心] Flask服务器与PyQt窗口，包含 update_copy_progress 等主逻辑
├── plugin_system.py     # [核心] 插件管理器：加载插件、分发事件、管理UI插槽
├── thumb_engine.py      # [核心] 缩略图引擎：调用 FFmpeg/QuickLook 生成预览
├── player.py            # [组件] Qt Multimedia 播放器组件
├── utils.py             # [工具] 辅助函数
├── plugins/             # [扩展] 插件存放目录
│   └── ...
├── static/              # 前端资源
│   ├── assets/
│   │   ├── js/script.js # 主前端逻辑 (包含 STATE 定义)
│   │   └── css/style.css
│   └── vendor/          # 第三方库
└── templates/
    └── index.html       # 主界面 (包含 {{ ui_slots }} 渲染点)

```

---

## 🚀 运行与安装

1. **环境准备**:
* Python 3.10+
* 系统需安装 `ffmpeg` 和 `ffprobe` (必须)。


2. **安装依赖**:
```bash
pip install Flask PyQt6 PyQt6-WebEngine Pillow opencv-python

```


3. **启动**:
```bash
python main.py

```



---

## 📝 贡献与扩展

欢迎编写插件！如果你发现主程序缺少某个 `Hook` 或 `UI Slot` 导致插件无法实现功能，请在 `plugin_system.py` 中添加相应的注册逻辑，并在 `main.py` 的对应位置触发 `fire_event`。
