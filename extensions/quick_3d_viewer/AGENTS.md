# Topos 3D Viewer 独立应用维护

本目录是完整应用，可单独拷走；无需读取或导入 Topos 主工程。

- 入口：`python main.py [模型路径]`；依赖见 requirements.txt；FBX 额外需要 assimp-py。
- 宿主入口：`python main.py --topos-session ID`，经 stdin/stdout 使用 JSONL 协议。
- 原业务位于 quick_3d_viewer.py；适配位于 main.py；SDK 位于 topos_app_sdk.py；FBX 子进程入口 quick_3d_parse_worker.py 必须一起交付。
- 协议版本固定为 1。每行 UTF-8，含换行不超过 65536 字节；消息携带 protocol=1、session_id、request_id，日志写 stderr。
- 宿主命令：open（path）、activate、close、detach。事件：ready、opened（path）、dirty（dirty）、saved（path）、exported（paths、source_path）、error（code、message）、closed。
- ready 表示窗口与桥接完成，opened 必须等异步模型解析和场景提交成功；过期任务不得误报成功。
- EOF/detach 只解除宿主连接，不关闭查看器；截图实际写入成功才能发 exported。
- 保留独立 OpenGL 上下文配置；解析线程仅处理 CPU 数据，不访问 GPU。FBX 使用自带 worker，不能偷偷回退到阻塞 GUI 的原生解析。
- main.py、worker、业务脚本不得 import src，不写死用户机器路径，不在启动时自动安装依赖。
- 升级时保持 main.py 包装的方法兼容，并保留截图成功路径的 `_notify_export` 回调。
- 业务应用升级不要求修改 Topos；清单中的格式与依赖必须与真实能力一致。
