# 2026-10-21 Edit 第六轮审计证据

主计划：[edit_page_sixth_full_audit_optimization_plan_2026-10-21.md](../../edit_page_sixth_full_audit_optimization_plan_2026-10-21.md)。

基线与环境见 [records/environment.json](records/environment.json)。目录只保存审计资料，没有产品修复。用户原始测试视频只读；隔离配置、临时字幕/音频、生成工程与成片位于 `/tmp/topos-edit-audit-20261021/` 及探针各自声明的 `/tmp` 路径。成片二进制没有额外复制入库，保存了 ffprobe、manifest 和解码画面；临时文件可能被系统清理，重放时应重新生成。

## 文件组织

| 路径 | 内容 |
|---|---|
| `scripts/interactive_session.py` | 启动真正的 `src.main.main()`，正常Qt/渲染线程，无GPU替身；GUI线程轮询本地步骤文件 |
| `scripts/steps/` | 本次交互命令原文，按序号执行，依赖同一会话前文状态；不是每个文件均可独立运行，也不是整理后的通过型自动测试 |
| `records/0xx_*.json` | 每步结果。`ok`表示驱动代码完成，不表示产品无缺陷；failed导出等也可能是`ok:true`的观察结果 |
| `screenshots/` | 实际窗口/控件截图；`export_frame_01/02/03.png`是实际成片0/150/299帧 |
| `probes/` | 11个生产模型/命令、真实PCM、合成信号、受控decoder探针；证据类型见专项报告 |
| `specialists/` | 核心、音频字幕交付、专业对标三份专项原始报告；保留原始编号和路径，已复制的探针见本目录 |
| `regression/summary.json` | 619测试文件→31批次的精确分配、命令、退出码、XML统计；每文件一次 |
| `regression/batch_*.xml/.log` | 各独立进程的JUnit与原始日志；累计10,672 passed、20 skipped、0 failures/errors |
| `records/skipped_tests.json` | 全部20个跳过项及原因 |
| `logs/` | 应用日志、两次未完成的单进程全量日志、原生采样栈、Deliver专项失败日志 |

## 最重要的交互证据

- [015：锁轨效果◆仍提交](records/015_effect_keyframe_locked.json)。轨锁准备使用生产handler，◆为真实鼠标点击。
- [020：SRT/ASS实际拖入，ASS文本丢失](records/020_import_captions.json)。
- [025：真实音频分析阻塞UI心跳](records/025_normalize_ui_responsiveness.json)。调用生产分析方法于实际GUI线程，不是替换解码器。
- [029：300帧序列的Custom范围误报0帧](records/029_delivery_context.json)。
- [030：真实队列普通导出成功](records/030_export_run.json)、[成片ffprobe](records/gui_export_ffprobe.json)、[manifest](records/gui_export_manifest.json)。
- [032：真实嵌套导出失败](records/032_nested_delivery.json)。
- [033：非当前序列导出失败](records/033_other_timeline_export.json)。
- [037：尺规/控制器同步后，Next/Home破坏同步](records/037_transport_fresh_timeline.json)。
- [038：无截图的18次切源复验仍有GPU错误/释放告警](records/038_source_switch_no_capture.json)。
- [042：真实场景检测完成](records/042_scene_results.json)。
- [043：生产保存重开、12素材/3时间线一致](records/043_save_reopen.json)。
- [044：真实运行中的队列任务取消，未生成成功manifest](records/044_export_cancel.json)。不等同底层decoder释放正确。
- [045：保存后正常关窗](records/045_close_requested.json)、[进程退出码0](records/process_exit.json)。应用日志末尾有GPU线程2000ms未退出的错误，清理路径仍需优化，归入U06。

## 证据解释和驱动修正

以下属于审计脚本/投递问题，不计产品缺陷：

- 002/004：早期直接写入命令`.py`时，轮询可能读到半文件，结果为null。后续均先写`.tmp`再原子重命名。
- 003：002未完成准备导致无project；006/008猜错MediaAsset属性名，之后核实实际为`path/display_name`。
- 007：依赖Source尚未建立；011：误用`tp.transport_bar`，正确为ProgramViewerWidget的`get_transport_bar()`。
- 019：诊断输出对浮点speed误调`to_dict`；023：媒体池信号名误写，024使用正确`create_multicam_group_requested`。
- 028：Ref没有`duration_frames`，驱动打印报错；029改用安全类型诊断，才作为产品上下文证据。
- 034：Next按钮名误写；035/037使用正确`btn_next_frame`独立复验。
- 039：查找检测按钮文本未匹配“自动场景检测”，故040无分析。041点正确按钮，042才是成功检测证据。
- 最初播放观察器在011失败前已连接，012又连接一次，frame_ready计数翻倍；报告没有将118次发射当作118个独立呈现帧。
- 早期程序化seek不会推进可见播放头，导致部分分割准备位置与实际位置不同；最终用尺规实际点击建立一致起点，037确认U04。没有把准备动作的失败当作分割算法单独错误。
- 首次标记列表可见性观察不稳定；037复验浮动列表确实显示，不列为产品问题。
- `logs/topos-gui-stack-20261021.txt`采样时主GUI处于正常事件循环，未据此报告GUI挂死。Q01的关闭等待栈来自两次pytest单进程；实际关窗的GPU线程超时有独立日志，不混为同一已证实根因。

## 重放

从项目根目录使用当前项目解释器，所有目录名称含空格时保留引用。

```bash
# 重放全部Edit测试；进程有超时和诊断，不改测试源码。
.venv/bin/python docs/audit/2026-10-21-edit-full-review/scripts/run_partitioned_regression.py

# 独立生产模型/命令探针。
PYTHONPATH=. .venv/bin/python docs/audit/2026-10-21-edit-full-review/probes/topos_edit_core_probes_20261021.py
PYTHONPATH=. .venv/bin/python docs/audit/2026-10-21-edit-full-review/probes/topos_caption_probe_20261021.py

# 真实音频PCM对照（需要tests/test_videos/2K_h264.mp4）。
PYTHONPATH=. .venv/bin/python docs/audit/2026-10-21-edit-full-review/probes/topos_delivery_real_audio_20261021.py
PYTHONPATH=. .venv/bin/python docs/audit/2026-10-21-edit-full-review/probes/topos_stem_real_parity_20261021.py

# 归一化合成声道与恒速正弦探针；仅在/tmp生成补充WAV。
PYTHONPATH=. .venv/bin/python docs/audit/2026-10-21-edit-full-review/probes/topos_normalize_probe_20261021.py
PYTHONPATH=. .venv/bin/python docs/audit/2026-10-21-edit-full-review/probes/topos_audio_speed_sine_20261021.py
```

可见GUI调试可另启动：

```bash
TOPOS_AUDIT_SESSION=/tmp/topos-edit-audit-replay .venv/bin/python \
  docs/audit/2026-10-21-edit-full-review/scripts/interactive_session.py
```

它会使用新目录隔离配置。向其`commands/`按序投递Python步骤：先写`.tmp`，再重命名为编号`.py`，每次等待记录生成后继续。原始步骤有前文变量、时间线ID和历史栈依赖，需按目标复现流程读取/准备，不能无脑并发全投；审计失败尝试也不应加入通过型测试集。每个结果写入本证据目录，重放前应复制目录保存本次原始证据。

主计划中标记未测的硬件/外部软件/RAW/长时/广播/云模型组合，没有被这些脚本补充成“全部通过”。
