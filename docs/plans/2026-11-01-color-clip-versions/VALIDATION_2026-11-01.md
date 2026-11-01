# 片段版本功能复核与修复记录

2026-11-01，按 [实施计划](IMPLEMENTATION_PLAN.md) 对当前生产接线复核。此次先跑的 167 项测试通过，但补充真实工程结构、真实控制器调用链和桌面应用验证后发现以下遗漏，均已修复。

## 修复的问题

| 问题与触发方式 | 修复与验证 |
|---|---|
| 保存干净版本，之后加图层、RAW、降噪或曲线设置，再加载干净版本，旧设置残留 | 版本恢复从快照构造完整内容，仅保留当前栈身份；省略字段恢复 schema 默认值。覆盖加载、删除全部、Undo/Redo，版本诊断元数据不进入调色栈。 |
| 版本管理会丢未知表字段；新建空域后撤销留下空版本表键 | 保留完整未知数据，并记录原始键的存在状态；Undo 精确恢复缺键、空字典或原始表，不只恢复标准化后的四个字段。 |
| 安装/卸载插件后仍显示保存时的旧诊断 | 每次读取重新核对注册表；历史诊断和插件完整载荷原样保留；注册表不可用时明确未核对。 |
| 测试把完整序列放在 project.timelines，生产实际是 TimelineSequenceRef，菜单误报片段不存在 | 宿主用 controller 的 `_current_timeline_sequences` 作只读解析投影，不把完整序列写进工程引用表；补真实 refs 测试。 |
| 空域创建/跨域复制先单独建栈，后续重名失败留下栈与历史 | 建栈、初始化、保存或复制通过同一个项目宏事务提交；失败零遗留，单次撤销完整恢复。 |
| 跨域加载分属时间线与项目两条历史，单次撤销不能恢复引用与调色 | 新增 `LoadClipVersionCommand`，激活和片段引用变更在一个项目历史项中提交；复用 `ProjectTransactionResult.sequence_map` 发布。 |
| 用整个旧序列恢复绑定会覆盖后来修剪/移动；目标被删除或改绑仍能撤销 | `SequenceColorBindingUpdate` 仅更新目标片段两个绑定字段，保留修剪、移动和其他编辑；真实命令栈在 execute/undo/redo 前验证当前序列，冲突不弹历史、不提交。 |
| 跨域加载只 hydrate，没有重绑保存回调、ColorSession、PageContext、播放上下文 | 新增 `_sync_active_color_version_stack()`，在 hydrate 前同步这些状态，使用防递归锁；保存进入新栈，保留当前帧与源帧偏移。 |
| 修改共享版本时，当前查看的另一个共享片段不更新；非使用域操作可能改当前画面 | 根据当前会话实际绑定栈判影响，刷新共享视图与所有依赖缩略图；非使用域的变更不覆盖本地视图；更新 Composite 输入失效状态。 |
| 元数据修改经真实 File 发布入口仍会清帧缓存、重新预热解码 | 新增 renderer 的 `update_project_metadata()`；只更新版本表时保留预热，真实像素变化走颜色栈更新；重复发布同一个不可变工程幂等。 |
| 调色页全局撤销走引擎，无法撤销版本；直接抢项目历史又会跳过新图层 | 为成功版本操作记录会话与引擎历史边界，只在边界匹配时路由项目 Undo/Redo；独立引擎新操作优先；Redo 同时核对两侧历史；会话失效释放边界引用。补回引擎 Redo 的回显路径。 |
| 分割后未激活片段仍引用原片段私有栈，右键加载改变原片段 | 本地域解析校验非空 clip/timeline 属主；保留旧无盖章数据兼容。无独立栈时显示空域，创建独立版本后加载才切绑定；已有自己栈时加载使用原子引用事务。 |
| 立即创建版本时，最后 16ms 内的滑块/色轮值还没进入完整 layers 快照 | `trigger_save()` 先刷新色轮与滑块待发信号，再调用保存回调；真实桌面验证曝光 0.0 ↔ 1.25 的保存与加载，以及跨域后继续调色保存。 |

## 自动化验收

环境：项目 `.venv`，Python 3.12.14，PyQt6 6.11.0 / Qt 6.11.2，macOS。以下完整聚焦回归 **466 passed，32.87 秒**。其后只补一个引擎 Redo 回显用例，单独重跑 `test_clip_version_project_sync.py` **11 passed，2.09 秒**。本轮覆盖的不同用例合计 467 项；没有把重跑次数累计成用例数。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/app/test_clip_version_project_sync.py \
  tests/app/test_clip_version_renderer_sync.py \
  tests/app/test_clip_version_host.py \
  tests/app/test_clip_version_scale.py \
  tests/edit/test_color_version_command_stack.py \
  tests/edit/test_color_version_domains.py \
  tests/edit/test_color_versions.py \
  tests/edit/test_m07_version_tools_panel.py \
  tests/edit/test_m07_share_autolock.py \
  tests/edit/test_m02_snapshot_io_fidelity.py \
  tests/edit/test_timeline_preview_renderer.py \
  tests/color/test_primary_wheel_ui.py \
  tests/color/test_clip_version_ui.py \
  tests/color/test_clip_strip_presentation.py \
  tests/color/test_timeline_clip_strip_lazy_thumbnails.py \
  tests/color/test_timeline_clip_strip_signal_dedup.py \
  tests/color/test_clip_navigation.py \
  tests/app/test_color_clip_strip_preview_ownership.py \
  tests/app/test_color_clip_strip_idle_refresh.py \
  tests/app/test_color_stack_switch_persistence.py
```

覆盖真实命令栈、IO 往返、旧数据、未知字段、锁与插件诊断、共享域、失败原子性、并发、缩略图归属、版本 UI、真实发布链和 200/500/1000 片段规模。测试中 `/m/*.mov`、`/x.mov` 等为有意构造的媒体占位路径，相关 OpenCV 提示不代表真实素材解码失败。独立复查与 `git diff --check` 均通过。

## 真实桌面应用验收

使用完整 MainWindow、真实生产信号和媒体池命令栈，导入用户提供的 `tests/test_videos/2K_h264.mp4`，构造两个同源不同源范围的片段。使用独立临时配置与内存工程，没有覆盖用户工程或视频源文件。

通过的操作链：

1. 本地创建 Audit A；立即把曝光滑块改为 1.25 并创建 Audit Bright，验证引擎与完整快照收到最后手势。
2. 加载 Audit A 后曝光恢复 0.0，加载 Audit Bright 恢复 1.25。
3. 复制指定 Audit A 快照、重命名副本、复制到远程域并加载。
4. 核对片段引用、ColorSession、PageContext、GradingPanel.current_stack_id 与保存回调一致。
5. 从真实全局入口 Undo/Redo，片段域引用、会话和保存回调一起恢复。
6. 远程加载后继续修改曝光为 0.75、创建版本，验证实际写入新远程栈。
7. 正常关闭窗口，执行 Qt deferred delete，最终驱动进程退出码 **0**。

证据：[最终日志](validation/real-app.log)、[真实窗口截图](validation/real-app.png)。可复用驱动：[drive_real_app_clip_versions.py](../../../tests/benchmark/drive_real_app_clip_versions.py)。此驱动从既有真实应用片段驱动提取初始化流程，并补上可清理的退出路径；测试时自动取消启动项目选择框，仅对其自建内存测试工程跳过保存确认。

```bash
.venv/bin/python tests/benchmark/drive_real_app_clip_versions.py \
  tests/test_videos/2K_h264.mp4
```

边界：此次桌面验收核对真实视频显示、操作结果、调色参数与身份，不宣称像素级 GPU/导出等价。4K/60FPS 持续播放测量、真实导出逐帧对照及所有缺失插件组合未执行；已有相关自动化/解析测试通过不等于这些桌面测量通过。计划中的这些验收项继续保留其验证边界。
