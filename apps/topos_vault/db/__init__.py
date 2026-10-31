"""vault 数据库层。

三类素材（视频/音乐/图片）同库不同表，共用 ``media_source`` 身份层（指纹主键）。
各类型专属表挂在 ``fingerprint`` 外键下，互不干扰。

设计要点：
- **指纹为主键**，不是路径——素材移动后数据不丢
- **路径只是线索**（path_original / path_current / path_relative）
- **vector_index** 统一管理所有 FAISS 向量到业务表的映射
"""
