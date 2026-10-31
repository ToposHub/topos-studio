"""Topos Vault — AI 影视素材库管理系统。

独立外挂应用：在电脑闲置时启动，批量给视频/音乐/图片素材做 AI 打标，
把所有标签/向量/指纹存储到独立的 vault.db，供 Topos Color 主项目检索调用。

核心设计：
- **指纹为主键**：数据身份靠内容指纹（不靠路径），素材移动后能重连
- **三类素材统一**：视频/音乐/图片同库不同表，共享身份层与画面理解流水线
- **可插拔 provider**：每个能力是 Protocol，大/小/stub 多实现，改配置即可换链路
- **与主项目共享模型**：模型权重放 ``models/ai_editing/``，复用 ModelPackManager

三个进程形态（共享同一套数据，靠文件系统通信）：
    进程 B（重）：``topos-vault index``     — 打标流水线，离线跑
    进程 C（轻）：``topos-vault serve``     — 只读查询服务
    进程 A：     Topos Color 主项目        — 通过 client.py 只读访问

详见 ``docs/`` 下的设计文档与方案。
"""

__version__ = "0.1.0"
