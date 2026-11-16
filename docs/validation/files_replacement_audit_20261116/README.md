# Files 完全替代复查证据

日期：2026-11-16。源码基线：`b08e60f0`，分支dev。对应[新整改与替代计划](/Users/hengshi/Documents/vscode/topos-studio/docs/plans/2026-11-16-files-full-replacement-review-plan.md)。

本轮只新增文档、复现与测试输出，不修改业务实现；所有文件/SQLite实验都在`/tmp`隔离夹具中执行。复现脚本不是修复，也不是正常用户操作流程，不应拿真实素材替换夹具路径。

## 自动化基线

```sh
PYTHONPATH=/tmp/topos-native-files-test-deps .venv/bin/python -m pytest \
  tests/files tests/ui/test_native_files_*.py \
  tests/ui/test_files_native_host.py tests/ui/test_native_player_host.py \
  tests/ui/test_topos_audio.py tests/ui/test_gpu_yuv_frame.py \
  tests/architecture/test_file_explorer_integration.py -q \
  --junitxml=/tmp/topos-files-replacement-audit-20261116/baseline.xml
```

结果：**172 passed，16.10秒**。已归档`baseline.xml`。这组测试覆盖当前实现已有断言，不能否定额外复现出的缺陷；当前GPU YUV测试也不要求真实OpenGL context。

本机项目环境没有pytest，沿用先前临时目录里的pytest/pytest-qt，未修改依赖文件。审查时不进行网络搜索或外部服务调用，HTTP复现只使用本机临时服务。

## 隔离复现与实际结果

从仓库根运行：

```sh
.venv/bin/python docs/validation/files_replacement_audit_20261116/repro/native_files_review_repro.py
.venv/bin/python docs/validation/files_replacement_audit_20261116/repro/native_files_review_transport_repro.py
.venv/bin/python docs/validation/files_replacement_audit_20261116/repro/native_files_review_tags_repro.py
.venv/bin/python docs/validation/files_replacement_audit_20261116/repro/topos_files_backend_audit_20261116.py
.venv/bin/python docs/validation/files_replacement_audit_20261116/repro/playback_semantics.py
.venv/bin/python docs/validation/files_replacement_audit_20261116/repro/without_extension_import.py
```

| 输出文件 | 已确认结果 | 对应问题 |
| --- | --- | --- |
| `native_files_review_repro.txt` | 搜索model变成B而索引/操作目录仍A；AND命中被二次过滤；改模板未预览即可执行；同path预览仅发布旧1px结果 | FR-08/10/13/14 |
| `native_files_review_transport_repro.txt` | 延迟搜索覆盖新目录；j1/j2都开始但仅j2完成反馈；能力禁用后信息刷新又启用 | FR-09/12/16 |
| `native_files_review_tags_repro.txt` | API成功但new标签只是追加；空串成功但DB不清空 | FR-11 |
| `topos_files_backend_audit_20261116.txt` | 同名并发移动丢一份；copy跟随目标symlink覆盖指向文件；OCR/markers旧路径；冲突恢复丢字段/错关系；mkdir绕过parent锁；2001条末页丢失；宏失败仍发布新asset | FR-01–06/10 |
| `playback_semantics.txt` | 尾帧重复且EOF=0；24000timescale被当fps，10秒算成0.01秒；2:1帧被缩成1:1 | FR-18–20 |
| `without_extension_import.txt` | `RuntimeError: Cannot locate external/file_explorer from adapter.py` | FD-01 |
| `legacy_import_inventory.json` | src下直接导入旧扩展的AST清单；不含注释/文档中的纯文本引用 | FD-01/04 |

补充边界：

- UI状态复现第一份脚本的`CAP_BEFORE/CAP_AFTER_INFO`没有保持有效选区，因此均为False；能力回归以第二份transport脚本的有效选区结果`False→True`为准。
- 后端并发移动用Barrier固定“两个任务都检查目标不存在后再执行”的合法交错，修改仅限临时service实例的方法；实际执行仍使用当前移动实现。输出剩A或B取决于线程顺序，两源消失且只剩一份才是判据。
- 后端脚本的same-source与move-to-descendant用例均保留源，未把它们判成新的删源问题。
- 后端路径迁移/软锁/恢复实验使用当前routes/trash函数与临时DB；时间线回滚复现使用真实不可变EditProject和当前提交方法，通过假命令失败注入固定失败分支。
- 播放语义实验不打开真实视频文件、不创建GPU context；Topos绑定用合法时基描述的假会话，验证计算公式，Libav比例用实际PyAV VideoFrame。
- 缺扩展import用Path.exists故障注入，不实际删除/移动旧目录。物理无扩展的干净checkout与发布包仍列入NR-09验收。
- 比较窗口内存问题通过源码和预算推导确认，没有在本机故意创建数GB图像。FR-17关闭崩溃为待失败注入验证的风险，没有声称已发生实机崩溃。

所有输出中的成功/失败只表达复现结果。整改后应把这些用例转换成正常focused tests，以“问题不再复现”的断言验收，保留旧输出作为历史基线。
