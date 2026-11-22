# 独立应用接入协议 v1

独立应用包保留自己的界面、文件读写和启动入口。Topos 只负责发现清单、选择文件处理器、启动会话以及接收保存结果；应用包不得导入 `src` 或依赖宿主 UI、数据库和 controller。独立使用与素材库使用共用同一份业务实现。

本次接入范围为 `extensions/topos_word/` 与 `extensions/quick_3d_viewer/`。目录拷贝、更新以及共享 Python 环境是源码运行方式；打包发布时仍需提供可启动这些脚本的 Python 运行环境。

## 应用包与维护边界

```text
extensions/
  topos_word/
    topos.plugin.json
    main.py
    topos_word.py
    topos_app_sdk.py
    requirements.txt
    AGENTS.md
  quick_3d_viewer/
    topos.plugin.json
    main.py
    quick_3d_viewer.py
    quick_3d_parse_worker.py
    topos_app_sdk.py
    requirements.txt
    AGENTS.md
```

每个目录都是可以单独交给维护者或 AI 的应用。应用更新通常只改本包；公共协议改变时同步调整 SDK 与协议版本。插件包不携带 `.venv`，依赖声明保留在包内；Topos 使用配置的 Python 启动独立进程，复用该环境现有的第三方库。依赖缺失时应报告模块名与安装提示，不在发现期间导入依赖，也不自动升级宿主公共环境。

复制整个目录到 `extensions/` 下一层后刷新插件清单。宿主只读取声明，不执行 `main.py` 或模块顶层代码；没有 `topos.plugin.json` 的普通目录忽略。更新前先关闭应用窗口，再替换包目录。设置和文档恢复数据应放在用户数据目录，避免跟随包替换丢失。

## 声明格式

沿用共享 `PluginManifest` 的身份、SemVer、宿主 API、平台与能力校验。在 `metadata.independent_app` 声明应用入口和 Python 运行依赖：

```json
{
  "plugin_id": "apps.topos_word",
  "display_name": "Topos Word",
  "version": "1.0.0",
  "host_api": [1, 1],
  "capabilities": [
    {
      "capability_id": "open",
      "kind": "command",
      "selection_mode": "single",
      "write_semantics": "write",
      "run_location": "process"
    }
  ],
  "metadata": {
    "independent_app": {
      "protocol": 1,
      "entrypoint": "main.py",
      "extensions": [".docx"],
      "required_modules": {"PyQt6": "PyQt6", "docx": "python-docx"},
      "format_modules": {}
    }
  }
}
```

`protocol` 必须为整数 `1`，不接受布尔值或字符串。入口必须为包内存在的相对 `.py` 文件；禁止绝对路径、`..` 和符号链接。`extensions` 为带点扩展名，比较时规范为小写。`required_modules` 的键是 Python 导入名，值是安装包提示，不能用插件依赖字段代替 Python 模块依赖。特定格式额外依赖放在 `format_modules`，例如 `".fbx": {"assimp_py": "assimp-py"}`；只有打开相应格式时才需要该依赖。

协议 v1 面向单文件交互式应用，一个包的宿主会话内复用一个应用实例。再次打开文件必须遵循应用现有的未保存确认逻辑；取消打开不能回报成功。缩略图、批量转换和 Inspector 并非因为声明了扩展名就自动具备，未来需要独立贡献入口。

## 来源、信任与兼容

宿主把固定应用根中的包标记为 `local_app`，显示“本地独立应用”。清单自报 `source` 不参与信任。发现默认启用仅表示允许成为候选，不等于允许执行；首次启动由宿主确认内容摘要信任，应用内容发生变化后重新确认。既有 installed/local_dev 第三方执行门禁保持独立。

内容摘要覆盖包内文件名、代码、资源与声明，按相对路径及文件字节确定，不依赖机器绝对路径或目录修改时间。只排除 `.git`、`.DS_Store` 及 `__pycache__` 中标准命名的 Python 编译缓存；`cache/`、`.cache/` 中的源码和资源仍计入摘要。运行时使用私有 `pycache_prefix` 并禁止写入字节码，避免执行包内未计入摘要的编译缓存。应用不得把 `.git` 或编译缓存当作业务源码；缓存和用户数据放到包外。包内符号链接和特殊文件拒绝。单包限制为 10,000 个目录条目、128 MiB，防止清单刷新无界遍历。执行前重新验证清单及摘要，过期的确认不能用于已更新的内容。应用进程运行期间不得把更新后的新声明当作旧进程的版本。

被信任的本地应用以普通本地进程权限运行，共享依赖并不构成沙箱，也不授予写入宿主元数据或直接操作数据库的接口。

## 会话消息

宿主与应用使用 UTF-8 JSONL，每条消息不超过 65,536 字节。固定 `protocol: 1` 与 `session_id` 绑定会话；宿主命令携带 `request_id`，应用对打开请求返回对应标识。标准输出属于协议，运行日志使用标准错误输出。

宿主命令为 `open`、`activate`、`close`、`detach`。应用事件为 `ready`、`opened`、`dirty`、`saved`、`exported`、`error`、`closed`。`open`、`opened`、`saved` 使用 `path`；`exported` 使用 `paths`，可选 `source_path` 记录来源文件。

创建进程成功只表示进程已启动；收到 `ready` 才表示协议已就绪；收到 `opened` 才表示具体文件打开成功。应用无法打开、用户取消未保存确认、模块缺失等情况必须通过错误或明确失败回应，不得伪装已打开。

后台线程负责管道读写和排队，Qt 主线程处理消息与窗口交互。消息和日志有界；未知协议、无效会话、非法路径和超大消息拒绝。宿主退出使用 `detach`，断开后应用仍可独立编辑和保存，通知失败不影响业务保存。应用停用阻止新调用，不能通过强杀进程丢弃尚未保存的内容。

宿主后台线程负责管道读写，不在 Qt 槽内执行阻塞写入。打开超时仅结束宿主等待，迟到结果不再终结其他请求；应用自己的菜单操作使用空 `request_id`，不会冒充宿主调用。断开宿主后 SDK 把标准输出/错误切到空输出，避免宿主进程退出影响后续业务操作。

保存或导出成功后，宿主校验会话与实际文件，再经 Files 服务更新索引和视图。协议事件不能直接改数据库字段、评分、标签或收藏。缩略图缓存失效和搜索索引更新应覆盖受影响文件，不依靠应用直接操作宿主缓存。

## 首批应用边界

- Topos Word：优先验证 `.docx` 编辑；`.doc/.rtf` 需要外部转换组件，打开与另存行为需如实呈现。复杂 Word 排版的往返保真属于应用自身能力，接入协议不作保证。恢复文件必须按文档或实例分开。
- 3D 查看器：复用支持的模型格式；FBX 需要 `assimp_py` 及完整的解析 worker，不能把 `pyassimp` 当作等价依赖。OBJ/glTF 的材质、纹理和外部缓冲文件仍由模型加载器解析。

本协议不声明自动提供嵌入式 3D 面板、文档首屏缩略图、市场分发或跨平台打包；这些能力可继续沿现有贡献接口扩展。

## 启动与配置

源码模式默认扫描仓库根目录的 `extensions/`；额外目录由 `TOPOS_APPS_DIR` 指定，多目录使用操作系统路径分隔符。直接复制一个应用包到这些目录下一层，然后在素材插件中心刷新即可。应用与其他来源身份冲突时显示诊断，不按最后扫描的目录覆盖。

默认使用宿主的 `sys.executable`，也可用 `TOPOS_APPS_PYTHON` 指定 Python 的绝对路径；冻结打包版必须显式提供这个运行环境。缺依赖只报错，不自动安装；开发者可使用相应包内 `requirements.txt` 安装或检查兼容版本。Word 的 DOC/RTF 转换仍需系统组件，不随 Python 模块检查自动提供。

首次点击启动或双击关联文件时，宿主会显示应用目录及普通进程权限，确认后按内容摘要保存本机信任；清单刷新本身不弹窗、不启动应用。右键“打开方式”可选择工具或修改该扩展名的默认处理器。只有一个匹配工具时双击直接进入该工具的启动流程；多个匹配工具且未设置默认时显示选择器。选择“使用原有预览方式”可取消该类型的应用接管。

关闭 Topos 后仍保留的应用已经断开回库通知；在独立窗口保存的文件可通过素材库刷新读取。再次启动 Topos 会建立新会话，不接管先前已经独立运行的窗口。

## 验证记录（2026-11-23）

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/plugins/test_independent_app_manifest.py \
  tests/plugins/test_independent_app_sdk.py \
  tests/ui/test_independent_app_runtime.py \
  tests/ui/test_files_native_host.py \
  tests/files/test_external_file_refresh.py -q
```

107 项通过。覆盖静态发现零执行、清单/路径/兼容诊断、内容变化撤销信任、中文空格路径、真实子进程会话、管道背压、过期请求、关闭宿主后存活、DOCX 编辑保存回读、未保存取消、局部回库保留用户标记、基础 OBJ/FBX 解析与 worker 数据往返。额外真实应用烟测：Word 打开合成 DOCX（offscreen），3D 查看器在 macOS Cocoa/OpenGL 窗口打开合成 OBJ；均收到 ready/opened/closed，退出码为 0。

相关插件、Files 领域与原生界面回归：`tests/plugins`、`tests/files`、独立应用运行时、真实 Files 宿主组合、原生插件中心、偏好、面板、操作和 transport 测试，共 1053 项通过、1 项跳过。`git diff --check` 通过。

这些验证不代表复杂 Word 排版完全保真，也未穷举大型模型、复杂 FBX 动画、材质组合及跨平台打包。
