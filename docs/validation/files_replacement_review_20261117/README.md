# Files 替代计划第二轮复核证据

> 文中提及的部分旧计划或测试附件已清理，正文保留当时的记录。

日期：2026-11-17。读取基线：`fix-ui @ 7f025973`；修复后报告对应本轮工作区。
结论和后续任务见[新计划](../../plans/2026-11-17-files-replacement-followup-plan.md)。

本轮查阅原计划、实际调用方和回归测试，直接修局部UI/transport和测试断言；文件安全、工程事务、runtime、GPU归属和独立替代只记录后续工作。文件/SQLite实验全部使用临时夹具，没有迁移真实库、删除扩展或启动真实播放/GPU。

## 自动化验证

修复前第一组（Files + 原生Files UI + 播放语义 + 导入补偿）共216项：**215 passed, 1 failed**。失败为 `test_capabilities_disable_metadata_controls_with_reason`，未选择素材时禁用原因为空；其后“能力恢复立即启用”的断言也与必须有选区的规则冲突。本轮修复提示并修正选区断言。原始JUnit。

额外播放宿主/Topos音频/YUV验证：15 passed, 3 skipped。最终采用下列明确集合一次复跑，不能将不同测试集合的计数直接当作完成率。

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/files tests/ui/test_native_files_*.py \
  tests/ui/test_files_native_host.py tests/ui/test_files_location_regressions.py \
  tests/ui/test_files_session_regressions.py tests/ui/test_playback_semantics_regressions.py \
  tests/ui/test_native_player_host.py tests/ui/test_topos_audio.py tests/ui/test_gpu_yuv_frame.py \
  tests/edit/test_external_import_compensation.py \
  tests/architecture/test_file_explorer_integration.py \
  --junitxml=docs/validation/files_replacement_review_20261117/regression.xml
```

**结果：247 passed, 3 skipped（14.08秒）**。[完整输出](regression.txt)、JUnit。3项跳过是 `test_gpu_yuv_frame.py` 中本机缺失的真实媒体夹具；其余该文件测试不要求真实GL context。测试通过不等于GPU、声卡、平台、完整替代或恢复演练验收。

局部新增回归覆盖：批改名执行的会话回包/HTTP错误/网络错误/无效与未确认响应、在途改模板/重复预览乱序、同路径fetch两种回包顺序、Capability DTO原因与无选择门控。并收紧原有文件并发断言。子审查独立复核过3组UI测试，随后最终集合覆盖了进一步补充的响应/DTO用例。

## 隔离复现

从仓库根运行；这些脚本报告观察结果，不把输出中的成功当作完整验收。安全脚本复用既有临时SQLite与AST工程夹具。

```sh
.venv/bin/python docs/validation/files_replacement_review_20261117/repro_safety.py
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/files_replacement_review_20261117/ui_context_repro.py
.venv/bin/python docs/validation/files_replacement_review_20261117/playback_boundary_repro.py
.venv/bin/python docs/validation/files_replacement_audit_20261116/repro/without_extension_import.py
```

| 文件 | 观察与边界 |
| --- | --- |
| [repro_safety.json](repro_safety.json) / [源码与分析](safety-review.md) | 目标替换注入仍覆盖第三方；目录取消残留；目录改名失败；EXDEV注入无回退；真实API改名后markers留旧路径；恢复丢关系字段；片段构造异常遗留asset。没有把注入冒充真实跨卷/跨进程/坏盘验收 |
| [ui_context_repro_after.txt](ui_context_repro_after.txt) | 本轮修复后旧批改名预览不能确认新模板（null/false）；未修改的查询漏洞仍输出collection→search、旧搜索未abort。这是修复后输出，不是修复前基线 |
| [playback_boundary_repro.txt](playback_boundary_repro.txt) | 假2帧会话EOF时尚未展示任何帧、队列仍有0/1且Topos已暂停音频；暂停状态下Topos消费分支仍能取帧。无真实视频、音频设备、GPU操作 |
| [without_extension_import.txt](without_extension_import.txt) | 模拟旧main.py缺失后仍抛RuntimeError。仅patch Path.exists；没有物理删除/移动旧扩展，也不能替代无旧扩展发布包验收 |

Compare/Preview运行任务强引用、总预算、shutdown等剩余事项为源码确认，未在本机故意制造崩溃/大量内存分配。完整实机压力、10000条UI分页、跨平台、冻结包、旧库迁移/恢复均尚未验收。

## 轻量检查

`git diff --check`通过。当前项目venv没有安装ruff，因此未报告lint通过；本轮不为审查修改依赖环境。
