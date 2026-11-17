# G 阶段第五批（G5）：code_editor 接管——等价编辑工具

日期：2026-11-18 · 分支：fix-ui · 范围：`code_editor_service.py`（新，域
层文件服务）+ `code_editor_window.py`（新，多标签编辑窗口）+ 控制器原生
命令与**原生可用性后处理**。G 节行验收：**读取/编码、外部变更、安全
保存、恢复、新建与多文件入口；不依赖旧 routes/JS globals**。

## 新入口 / 新服务 / 关闭路径

| 部件 | 路径 |
| --- | --- |
| 新入口 | `launch("code_editor")` → 原生命令三段（F4 边界）→ F5 singleton → `CodeEditorWindow`（携带选中文件为多文件入口）。**原生可用性后处理**：native 接管的工具（无 legacy launch_path）在投影中 `launch_path="native://<tool>"`——中心/启动器可展示启动（停用状态仍不可启动） |
| 新服务 | `code_editor_service`：**读取/编码**——BOM 声明优先（utf-8-sig/utf-16-le/be/utf-32）、UTF-8 严格解码、降级 latin-1 如实标注（原字节域写回不静默换码）；**外部变更**——载入快照（mtime_ns+size），保存前比对，外部已变 `EXTERNAL_CHANGE` 拒绝（他人改动保留）；**安全保存**——`.part`+fsync+`os.replace` 原子交付（失败清理 .part 原文件不动）；**恢复**——`reload_file` 重锚磁盘真值 |
| 新窗口 | QPlainTextEdit + QTabWidget 等价编辑工具（纯 Qt，零旧 routes/JS globals）：新建（未绑定路径保存如实拒绝）、多文件标签、保存/从磁盘重载、meta 标注编码（降级标注） |

## 验证

- `tests/ui/test_code_editor_g5.py`（10 项）：UTF-8/三种 BOM 变体（含
  utf-16 正文去 BOM）；latin-1 降级 roundtrip 不损坏；NOT_FOUND；
  原子保存快照更新无 .part；**外部变更拒绝**（他人改动保留）；目录
  只读 IO 失败原文件不动；重载恢复磁盘真值；窗口新建/多文件/保存/
  外部变更-重载流/未绑定路径拒绝。
- `tests/ui/test_native_files_plugins.py` +1：控制器端到端——
  `native://code_editor` 可用性、零 HTTP、singleton、停用后可用性消失
  （重启生效语义不破）。

回归：G1-G5 + plugins + files —— **823 通过**（一次 plugin_batch2 顺序
抖动为仓库既有类，隔离绿）。

## 诚实边界

- 等价编辑工具 = QPlainTextEdit（无 Monaco 语法高亮/LSP）——"或等价
  编辑工具"口径；Monaco 宿主可作为后续增强替换呈现层（域服务不变）。
- 另存为对话框未做（新建缓冲提示用另存——当前窗口按已有路径保存）。
- 大文件无分块加载（QPlainTextEdit 自身有段落优化）；崩溃恢复（未保存
  内容的草稿盘）未做——保存路径本身崩溃安全（.part 原子）。

## 隐私

合成文本/BOM 字节；无隐私路径。
