# Topos 独立应用

这里的每个子目录都是完整的小应用：既可以单独运行，也可以被素材库发现。复制、升级和维护时保留整个目录，不需要复制 Python 环境。

## 当前两个应用

从项目根目录单独启动：

```bash
.venv/bin/python extensions/topos_word/main.py
.venv/bin/python extensions/quick_3d_viewer/main.py
```

也可以在命令末尾传入一个文件路径。离开 Topos 项目后，使用已安装相应依赖的 Python 执行包内 `main.py`，不需要导入或安装 Topos。

在素材库中使用：打开插件中心并刷新，可看到 Topos Word 和 Topos 3D Viewer；双击对应文档或模型，或右键选择“打开方式”。第一次运行需要确认信任该本地应用，后续内容更新后重新确认。

## 以后升级

1. 关闭该应用窗口。
2. 替换对应目录中的应用代码与资源，保持 `plugin_id` 稳定，更新 `version`。
3. 保持 `main.py` 和 `topos_app_sdk.py` 的协议 v1 接口；如果只升级业务脚本，检查本包 `AGENTS.md` 中的适配钩子。
4. 回到素材插件中心刷新并打开，宿主自动检查新清单及运行依赖。

设置和恢复文件保存在用户数据目录。不要把素材、模型权重、`.venv`、秘密配置或运行缓存放进应用包。

各包 `requirements.txt` 是依赖清单，安装到实际用于启动应用的 Python 即可；升级公共依赖前检查与 Topos 及其他应用的兼容性。3D 的 FBX 功能使用 `assimp-py`，与 `pyassimp` 不同。

完整协议、消息和目录配置见 [独立应用协议](../docs/architecture/independent-apps.md)。
