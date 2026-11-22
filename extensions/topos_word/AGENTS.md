# Topos Word 独立应用维护

本目录是完整应用，可单独拷走；无需读取或导入 Topos 主工程。

- 入口：`python main.py [文件路径]`；依赖见 requirements.txt。
- 宿主入口：`python main.py --topos-session ID`，经 stdin/stdout 使用 JSONL 协议。
- 编辑业务保留在 topos_word.py；接入与会话逻辑位于 main.py；可复制 SDK 位于 topos_app_sdk.py。
- 协议版本固定为 1。每行 UTF-8，含换行不超过 65536 字节；所有消息携带 protocol=1、session_id、request_id。普通日志必须写 stderr。
- 宿主命令：open（path）、activate、close、detach。应用事件：ready、opened（path）、dirty（dirty）、saved（path）、exported（paths、source_path）、error（code、message）、closed。
- 窗口和桥接连接完成后才能发 ready；成功载入/写入后才能发 opened/saved/exported；关闭取消不得发 closed；EOF/detach 后保留应用和未保存文档。
- main.py 不得 import src。插件清单声明依赖，不携带虚拟环境，不在程序运行时自动 pip install，不写死机器路径。
- DOC/RTF 经转换载入，保存走另存为；不能宣称完整 Word 格式保真。
- 自动恢复按文档区分并用锁保护，多窗口不得覆盖对方的备份；升级代码时不能删除用户恢复数据。
- 业务升级尽量只改 topos_word.py，保持 main.py 的适配方法兼容。修改导出逻辑后保留成功路径中的 `_notify_export` 通知。
- 协议变更须同时升级清单与适配层；保持旧接口兼容，未验证的能力不要在清单中声明。
