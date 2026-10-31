"""vault 查询服务（只读 Flask 应用）。

进程 C：``topos-vault serve`` 启动。只读 vault.db + 懒加载 FAISS，
为 file_explorer 插件和主项目提供毫秒级检索。

设计要点：
- **只读**：不写 vault.db（写入由打标进程 B 负责）
- **粗筛优先**：先 SQL 过滤缩小候选集，再小范围向量检索（端侧性能核心）
- **返回格式对齐 file_explorer**：``{files: [{name, path, type, ...}]}``，
  这样插件前端能直接塞进 STATE.allFiles 复用瀑布流渲染
"""
