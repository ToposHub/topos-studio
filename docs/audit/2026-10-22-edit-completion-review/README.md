# 2026-10-22 剪辑完成项复核证据

主计划：[第七轮完成项复核及优化计划](../../edit_page_seventh_completion_review_optimization_plan_2026-10-22.md)。基线 `d11615bb85d057f92735dbfcbfdc0debb127fc13`。本目录仅包含审计工具、结果和截图，未改产品源码与已有测试。

## 如何阅读

- `records/003_transport_delivery.json`：真实尺规/Next/Prev/Home同步，Custom导出可建立；范围为排他end，`[0,49)`实际49帧是正确结果。
- `records/004_live_edit_in_deliver.json`：真实Ctrl+B/Delete后live/draft 50帧，preflight/controller仍100帧；`export_inspection.json`外部解码确认实际输出100帧。
- `records/005_nested_export.json`、`008_other_timeline.json`：嵌套resolver和非当前序列的基础主链路已可输出，不能据此推断音频正确。
- `records/export_inspection.json`：ffprobe完整stream数据、文件SHA256及独立FFmpeg解码AAC的peak/nonzero/rms。4份MP4全部静音；输出文件本体留在隔离临时目录，不放入Git。`records/export_manifests/`为生产manifest原件。
- `records/006_g16_animation.json`：真实属性输入100后，动画仍0；`014_reopen_actual_saved.json`确认其0/0关键帧确实写入磁盘并重开。
- `records/007_switch_sources.json`：九素材三轮，共27次open/seek，循环内无截图，3组GL1282/WRITING/帧释放告警。当前源名可能不是异步错误的唯一来源。
- `records/011_png_export.json`及`screenshots/round7_gui_png_*.png`：实际Deliver输出3张PNG。`probes/topos-round7-delivery-*`另测PNG/TIFF生产编码和目录、像素、安全边界。
- `records/queue_restore.json`：独立真实Qt Workspace＋真实领域模型/队列文件的恢复顺序问题，非完整MainWindow重开队列验收。
- `logs/slot_exceptions.log`：第二次可见应用打开工程时9次已删除QTreeWidgetItem异常；进程仍退出0，不代表出现了进程崩溃。
- `specialists/`为三个专项原报告。保留报告生成时的`/tmp/topos-round7-*`路径；同名脚本/JSON/日志已复制到`probes/`或`tests/`，`audio-data/results.json`对应`probes/audio_synthetic_results.json`。

## 测试与边界

`tests/`包含Deliver全目录229通过、音频45文件937通过/1跳过、核心99通过、图序/decoder/capability26通过、宿主80通过的日志。后两批与其他批次重叠；音频新增21例已包含在937中，不能累计。本轮未重跑整个Edit单进程套件。

QTest操作是真实生产窗口内的Qt事件；独立pytest/部分面板使用offscreen。主音频probe使用真实WAV或真实2K H.264；实时FX门控probe使用受控DC decoder替身；取消probe使用真实Qt线程和可控分析时长。各证据等级在主计划与专项中分别说明。未做真实声卡监听、外部剪辑软件导入、小时级性能验证。

两次可见应用会话均以正常生产入口启动/退出，退出码0，原关窗2000ms误报本次未再出现。实际临时工程是`/tmp/topos-round7-gui/Round7Audit20261022.topos`，实际输出位于`/tmp/topos-round7-gui/exports/`。

## 审计脚本纠正记录

`009_image_sequence_ui`误用了`_format_combo`，实际为`_container_combo`；`010`尚未切图序容器，找不到PNG属于预期过滤；`011`选择图序容器后正常输出。`012`猜测保存路径`round7-review.topos`，实际对话框保存为工程名，因此该步open返回False；`013`打印的也是猜测路径。之后`014`在新的真实进程中打开实际保存路径成功，`015`记录正确路径并正常关闭。以上脚本错误不计作产品缺陷；原始结果保留，不篡改为通过。

## 重放

从项目根运行，使用项目`.venv/bin/python`和现有`tests/test_videos`。独立probe的输入/输出均在各自`/tmp/topos-round7-*`或新建临时目录，且不会编辑业务代码。部分probe通过`runpy`复用本基线测试辅助函数，因此需保持对应测试文件存在。

```sh
.venv/bin/python docs/audit/2026-10-22-edit-completion-review/probes/topos-round7-core-probe.py
.venv/bin/python docs/audit/2026-10-22-edit-completion-review/probes/topos-round7-audio-probe.py
.venv/bin/python docs/audit/2026-10-22-edit-completion-review/probes/topos-round7-audio-real-probe.py
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-10-22-edit-completion-review/probes/topos-round7-audio-qt-probe.py
.venv/bin/python docs/audit/2026-10-22-edit-completion-review/probes/topos-round7-delivery-probe.py
.venv/bin/python docs/audit/2026-10-22-edit-completion-review/probes/topos-round7-delivery-e2e.py
.venv/bin/python docs/audit/2026-10-22-edit-completion-review/scripts/host_probes.py
```

可见窗口工具为`scripts/interactive_session.py`，逐步脚本归档在`scripts/steps/`。原始步骤依赖执行顺序，009/012故意保留历史失败，不是“一键全通过”脚本。重放时应新开隔离session目录，逐个投递完整步骤；不要同时投递依赖步骤或写入用户配置。该工具当前输出到本目录，保留原始证据时应先复制整个证据包再重放。

`scripts/inspect_exports.py`对实际GUI文件再做ffprobe和PCM测量。`scripts/collect_evidence.py`复制本轮临时证据、记录环境并更新`evidence_manifest.json`（相对路径、字节数、SHA256）；会校验主计划/README链接及脚本语法。专项目录保留原始临时路径只用于溯源，不应成为后续工程数据的持久依赖。
