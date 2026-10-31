# 剪辑页面第三轮审计证据

HEAD：`63c0bdeaeb5edcee59363479cd8c10b947c3ddb1`，对照`833dcd382`。主文件：[第三轮新优化计划表](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/edit_page_third_audit_optimization_plan_2026-10-14.md>)。

本目录只记录本次审计；旧证据、原测试、业务代码及用户素材未修改。脚本会写专用临时配置和审计专用工程，不打开用户已有工程。

## 自动测试

| 记录 | 本轮实际结果 |
|---|---|
| evidence/pytest-round2-fixes.log/xml | 新增13个test_recheck文件，121 passed，9.98s |
| evidence/pytest-render-and-contracts.log/xml | 上轮9失败所在范围（LUT/GL/Metal/SyncLock/preposition），177 passed，32.64s |
| evidence/probes/topos_round3_core_pytest.out | 独立核心范围110 passed；与主范围重叠，不相加 |
| evidence/probes/topos_round3_features_tests.log | 功能范围30 passed；与主范围重叠 |

现有测试绿与新增probe失败同时成立：前者没有覆盖缓存第二次调用、网格外合法wheel、真实word字段、batch连续字段、完整facade信号接线、proxy第二阶段同路径重入等输入。新计划区分有效复现和fixture错误，不用测试数量替代全功能验收。

## 完整窗口测试

[interactive_driver.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-third-review/interactive_driver.py>)显示完整MainWindow，隔离配置和项目，QTest发送鼠标/键盘/拖动/按钮事件；真实素材来自用户tests/test_videos。它记录Qt槽异常以继续采证，不是生产修复。部分选区/锁轨fixture通过已有selection/command接口建立，然后实际编辑可见控件；不把fixture准备描述为人工操作。

`evidence/gui-session1`、`gui-session2`、`gui-session3`各自保留截图、command脚本、interaction_records.json、full-app.log。Session2已成功创建时间线、Append/Overwrite/Insert；早期command03缺timeline_id是脚本错误。Session3改用真实接口重跑，最后正常关闭专用窗口，`normal_audit_close.window_visible=false`且进程exit0。

前两个专用进程核对PID/完整命令后以SIGTERM结束，没有终止用户进程。早期modal自动输入观察不完整，不能据此列NewTimelineDialog阻塞bug；后续实际创建已成功。重启记录保留以免误将中断试次当完整验收。

主证据在gui-session3：

| 记录/文件 | 用途 |
|---|---|
| source_visible/source_playback，05-source-visible.png | Source真实可见有效CPU纹理，播放100→123，paint_failed=false |
| source_overwrite_occupied/source_insert_internal，17–19截图 | 原覆盖overlap已修，内部Insert有新A；同时观察旧残片链接异常 |
| source_validation | 两条links to non-existent clip，直接验证真实GUI提交后的模型 |
| locked_property_real，25-lock-property-error.png，uncaught-qt-exceptions.log | 锁属性控件enabled，输入77抛真实Qt槽ValueError；model/history不变 |
| multi_xy_real，26-multi-xy-overwrite.png | 两ID context与view selection保持；X55成功，接Y66两片X回0；一次UndoY回55 |
| roll_normal_press/move/release，23/24截图 | 正常Roll在press之后自取消，没有有效preview/提交 |
| roll_hit/roll_signal_trace，28-roll-signal-trace.png | hit=RollHandleItem；started state=ROLLING，finished delta2时已IDLE、model/revision不变。与Q focus事件证据共同定位自TrimMonitor抢焦 |
| save_reload、timeline_snapshot.json、timeline_after_reload.json、save-reload-diff.json | ZIP成功保存/读取，差异仅清掉两条Overwrite留下的非法link |
| export-smoke-results.json、smoke-*.mp4 | 两时间线各49帧640×360 H.264均成功，audio_omitted=false |
| normal_audit_close | 最终使用重复定时响应专用工程确认，正常关闭窗口并走_orderly_shutdown |

早期session3 command03错误访问_sectioned_panel、command04错误导入validator，均为审计脚本错误。有效替代为command05/06/07，不计入生产缺陷。截图18/19文件名沿旧脚本而保留，名称中的failure/fallback不代表本轮仍抛原错误；以对应模型和异常记录为准。

驱动固定SESSION为`/tmp/topos-edit-third-review-s3-20261014`、OUT为gui-session3。重跑前复制本目录并修改SESSION/OUT，避免覆写历史。投递动作先写.tmp再rename.py。rescue_timer仅在显式投递rescue.py时在UI线程执行诊断；只有一次只读modal盘点，不是绕过生产接口的功能实现。

## 模型/交互/功能探针

`reports/`有核心C3-01–08、交互T3-01–08、功能RF01–08及结构revision追加、render RG01–02完整报告；新主计划统一映射N01–N27。`evidence/probes/`归档各分工实际脚本和JSON/log/out/err，临时前缀topos_round3独立于旧topos_recheck。

M探针是真实模型/command/实际handler，部分host/dialog/service替身，不等同完整窗口。Q为真实offscreen Qt事件；Alt仅dispatch期间受控modifier，失焦事件不等同OS窗口切换。取消54矩阵必须读为51个有效非Roll取消+3个Roll提前自取消，不能写54个通过。

功能probe的1–3字节mp4/jpg只是元数据路径/生命周期占位物，未复制为媒体交付，也没有当解码成功证据。真正坏MP4 Replace case是验证探测失败被错误提交；不得据此说所有合法codec失败。

## 导出LUT补充探针

`evidence/render_probe.py/json`：真实helper首次成功、第二/三次命中缓存抛ValueError；JSON文件也保留真实_apply_color_grade日志。

`evidence/render_real_path.json`：真实_apply_color_grade连续帧失败及合法wheel快路错色。`lut_safety_probe.py/json`：seed7713在30组合法wheel中20组被接受，3组实际网格外输入最大差8/2/4code。无需新增脚本GPU调用；旧177范围已实际执行GL/Metal测试。

`export_smoke.py`从本次保存的专用工程调用既有导出服务。成功短片没有调色快路压力，不能反证N25/N26，也没有证明耳听/长时/硬件编码/所有交付设置通过。

`manifest.json`保存本目录各交付文件大小与SHA-256及计划文件hash。重跑只生成新的证据版本，原记录不应覆写。
