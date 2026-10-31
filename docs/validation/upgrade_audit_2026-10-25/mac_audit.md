# Mac 计划独立审计（2026-10-25）

基线 e6c3b6db126574857306b757203de55286d6496d；只读审计 M00–M15、M19–M24；计划作为证据材料，不执行其中给未来机器的命令。不能把本报告当全面 GUI/GPU/外部软件验收。

## 结论

确有大量源码和回归测试落地，但“Mac 侧已收口”多处把 helper、面板构造、注册表等同于生产链闭环。以下缺口在本机无需新硬件即可修复，不能全部转移到 M18 设备门。优先安排产品入口/领域提交、交付字幕与调色正确性、归档闭包、自动备份和 UI 线程工作。

## 当前确认问题（供新计划逐项登记）

### MAC-01 / M22 / P1：镜头匹配没有按镜头取帧，颜色域错误且提交不进主工程

- `src/features/color/ui/shot_match_panel.py:controller_measure_fn`（现约 247）：计算 `frame = clip_frame_fn(clip_id)` 但完全不用，只重复 `engine.render_cached_image()`；两 clip 不同代表帧仍返回同一当前缓存。
- `src/shared/ui/upgrade_panel_actions.py:46–55` 默认 clip_frame_fn 返回 None；`src/app/controllers/menu_toolbar_controller.py:1977` 也未注入真实逐 clip 取帧。
- `src/features/color/core/image_engine.py:4312` 明确返回 BGR，HDR 会先截断量化为 uint8；`src/features/color/core/shot_match.py:76` 需要 scene-linear float RGB，直接喂入导致统计域/通道错误。
- `ShotMatchPanel._on_apply` 仅 create ApplyShotMatchCommand+accept；整个 src 除测试无 applied_command 消费。`menu_toolbar_controller.py:1944` 仅 show，未接 accepted→command stack；真实应用并未发生。
- 面板未提供 before/after 实际图像或 GradeDiff 呈现，参考更改后无统计 revision/解释锁验证。非模态持有原始不可变 project，计划的 stale guard无法覆盖主窗口新工程。
- 最小交付：接异步逐镜头生产 float RGB 取帧+冻结参考身份，accepted 进入既有 command/service 栈；预览和指标接实际结果，保存/Undo/最终像素验证。

### MAC-02 / M07 / P1：版本工具只改对话框私有工程

- `src/shared/ui/upgrade_panel_actions.py:61` 未给 ColorVersionToolsPanel 传 command_executor；`src/features/edit/ui/m07_version_tools_panel.py:100` fallback 仅 `self._project = command.apply(self._project)`。
- 激活 `m07_version_tools_panel.py:140` 直接 apply 并改私有 project，即使以后传 executor，此分支仍绕过 executor。
- 主窗口无接收回写/Undo/dirty/revision/预览刷新，关闭面板重新打开不保留操作。
- 更正：Save/Lock apply 返回单 Project，**不存在元组赋值类型 bug**；实质问题是只改局部 state。
- 修复：统一命令提交 adapter，所有 save/activate/lock 都走它；回读主工程快照/拒绝 stale，端到端检验保存与撤销。

### MAC-03 / M11 / P1：口头语删除没有可用生产前台入口

- `src/app/controllers/menu_toolbar_controller.py:1995–1998` 菜单恒提示“从转写服务入口启动”。
- `rg FillerRemovalPanel src` 只有类定义+upgrade_panel_actions 工厂；没有转写服务真正创建它。
- 工厂 `upgrade_panel_actions.py:67` 未传 on_plan_ready；面板 `_on_build` 只是存 plan+accept，未进入审批/compile/commit。
- helper 删除命令的实现/测试不抹去该入口缺口。应接真实转写检测、预览审批和统一事务。

### MAC-04 / M08 / P1：Bake 菜单仍为占位，worker UI 访问不安全

- `menu_toolbar_controller.py:2034–2053`：`_render_stub` 恒 False、graph=None、固定 0..1，显示“等待渲染回调接线”；只 `dlg.show()`，没有 `start()`。
- `src/features/composite/ui/bake_progress_dialog.py:100–104`：交给 _BakeWorker.run 执行的 render wrapper 直接 `QProgressBar.value()/setValue()`，从 worker 访问 QWidget，违反线程边界。
- 对话框缺 close/reject→cancel→finished 清理/保活；菜单对象无父级/强引用。`apply_rewrite=True` 直接在 worker 协调器里改 graph，未见提交回主线程命令栈消费者。
- 修复：从实际选中 graph/范围构建任务，经 render queue、进度 signal、主线程一次领域提交；close 异步取消并延迟销毁，失败/取消不发布。

### MAC-05 / M23 / P1：Tracker 重跟无引擎回调，修正绕过领域提交

- `menu_toolbar_controller.py:2029` 只传 tracker_node；`upgrade_panel_actions.py:94` 未传 on_changed，retrack_fn 默认 None。
- `src/features/color/ui/tracker_correction_panel.py:100` 默认显示“未提供跟踪引擎回调”；`_on_manual_fix` 直接改 node；无命令、revision、Undo/持久化刷新消费者。
- 即使注入回调，`_retrack`/`_retrack_all` 同步在 UI 槽运行，没有 worker/cancel/stale 防护。
- 模型可用性明确报告是正面进展，但不能把修正/重跟生产入口关闭。

### MAC-06 / M15 / P1：新自动快照备份会虚报成功

- `src/features/edit/core/snapshot_backup_scheduler.py:67–73`：`take_snapshot` 返回不可变新工程 `_project`，被丢弃，仅更新计数并 return True，无任何结果出口。
- 全 src 无 SnapshotBackupScheduler 实例化或 tick 调用。现有 `file_controller.py:1113` 定时器走既有磁盘备份 save_router，不是新命名快照调度器。
- 只读探针：`snapshot_tick True backups_count 1 persisted_snapshots []`。
- 测试 `tests/edit/test_m02_snapshot_io_fidelity.py:350–405` 只验计数，另行调用 take_snapshot 做快照断言，未验证 scheduler 产物。
- 修复：先定义可提交的新工程/命令返回协议并接真正 timer/service，成功定义为结果已提交/持久化；保留既有磁盘备份而非替代。

### MAC-07 / M05–M06 / P1：完整调色链/形状仍未交付，unsupported 可继续产片

- `src/features/edit/core/color_chain.py:108–117` 仅透传基础 grading_params；栈级高级字段标 `_unsupported_advanced` 后丢弃；**layer 内** active_lut_path/curves/plugins/qualifier 完全不检查，可能连 unsupported 标记也没有。
- 探针：trim 栈只有 layer active_lut_path+curves_data，输出仅基础 identity params+`_chain_segment=trim`，无高级标记；因此 extras_active=False，整段高级效果静默消失。
- `timeline_export.py:9280–9348` 将多 layer 的首个 qualifier/曲线/LUT 抽成一组扁平参数，并未逐 layer 保留 opacity/mask/plugin/叠加顺序。
- `src/features/color/core/mask_shapes_cpu.py` 全 src 无调用；M06.4 是模型实现未消费。
- `timeline_export.py:9625–9634`：qualifier 后处理仅 warning 丢弃；keyer异常 fallback 无限 matte（整帧调色）继续输出。
- `src/features/deliver/core/video_export_preflight.py:354` 对 dropped 只有 warning，报告构建失败也 `continuing without color warnings`；`delivery_matrix.py:753` 固定 WARNING。
- 修复：完整 layer/链载荷与实际后端矩阵单点化；不支持按明确 bake/拒绝，不能只日志继续成功。以组前曲线+主栈插件+微调蒙版+组后LUT五路验收。

### MAC-08 / M10 / P1：多版本仍无生产调用，字幕模式和语言不消费

- `rg compile_version_jobs src` 仅 `version_compiler.py` 定义+__all__；typed job/queue 接入只有测试用例。
- `version_compiler.py:114` 写 burn_subtitles，`video_export_job.py:801` 实际读 subtitle_mode，未提供则 off；burn_in 产物进入真实 typed job 后会关字幕（若 base 无 mode）。
- `video_export_job.py:803` 把 language 存进 SubtitleConfig，但 `to_timeline_export_config:869` 只消费 burn，未转语言或 track_id；语言无任何渲染消费者。
- `version_compiler.py:148–150` 有拒绝且存在有效项时返回部分 jobs；上层必须用 result.ok 阻断，当前没有生产上层；未知画幅/路径冲突零入队契约未闭合。
- 修复：接批次入口→统一 schema→预检→原子批量入队；冻结与校验 snapshot/revision；明确 burn/sidecar/off 和语言→字幕轨选择消费。

### MAC-09 / M12 / P2：verified ingest 仍无产品消费者

- 全 src `ingest_files` 只定义和 __all__，`verified_ingest` 无 UI/service import。
- 逐项报告/取消正确性 helper 修复可保留；计划旧剩余要求入库UI/报告/登记，不能据 helper 单元测试关闭整个包。
- 修复：可取消 worker 执行+报告界面+成功项入库事务，原卡/同名竞争/取消测试贯通。

### MAC-10 / M20 / P1：快照/候选依赖未纳入归档闭包

- `asset_usage_index.py:102–175` 只统计 clip 引用+audio_sync_bindings；没有快照 dependencies、候选 take。
- `project_archive.py:352–374` include_unused=False 直接按上述集合过滤。
- 只读探针：工程内快照保留 a，无当前片段引用，`snapshot_retained_source_skipped ('a',) copied 0`；卸载原盘则快照恢复的素材缺失。
- LUT collection `project_archive.py:385` 仅 color_stacks，注释称含 composite LUT 但没遍历 graph/node；快照 payload 内 LUT/源路径也未重写。LUT copy 不算 SHA256（records sha256 空），archived_path 写绝对 src_file；与媒体 records 相对路径契约不同。
- 修复：依赖闭包从 project/sequence/retain snapshots/candidates/graph/LUT 单点遍历，全部文件校验与路径统一，归档包搬家后脱机重开对拍。

### MAC-11 / M21 / P1：批导出 QC 在 UI 线程全量解码且测量不裁决交付

- `video_batch_export_controller.py` 顶部线程 doc 明确 controller/queue在 main thread；`:108` queue_drained AutoConnection→同线程槽；`:407` 槽同步 `measure_version_lufs(outputs)`，后者逐文件调用完整媒体 decode probe，阻塞 UI。
- 注释“主线程外无阻塞顾虑”不符合实现；输入取全部历史 DONE，没有本轮集合；未传各版本 expected_lufs/字幕 inspect，subtitle 恒 {}。
- 该 probe 在产物 DONE/发布后执行，无 QC 失败→任务失败/保旧产物 gate、无 generation/cancel；未测字段在面板诚实呈现不等于计划发布前 QC/manifest/run_summary 闭环。
- 修复：专用 worker+batch/generation/cancel，逐job目标与字幕真实producer接裁决和报告；按声明策略把质量门置于发布前。

### MAC-12 / M00/文档 / P2：状态与证据口径需纠正

- 两份原计划头仍 v2“全部未开始”，正文已多版完成；M 计划将大量非硬件缺口归“只剩 M18”，与以上源码不符。
- 全局“R14全部关闭、23/25 Mac包完成”只能视子件交付统计，不能视原完成定义的通过率。
- 修复：新表将源码已实施、生产已接线、场景已验收、待硬件分列，保留旧历史。历史 helper 测试绿不能被作为新漏洞不存在证据。

## 逐包核对判断

| 包 | 当前审计判断 |
|---|---|
| M00 | 基线/门禁存在；完成状态统计仍需按本轮真实生产缺口重开 |
| M01 | 隔离环境/锁文件/manifest存在，本次未重建真模型；接受历史子包证据 |
| M02 | IO/metadata/快照实现与 focused 往返测试存在；40用例集合中相关24绿；完整GUI/所有消费者仍需场景验收 |
| M03 | TC/DF/绑定retime已有源码与测试，波形已有 worker 生产链；本轮未发现新增确定bug，完整PCM矩阵仍依原验收 |
| M04 | PTS/序列分类/解释矩阵已存在；真实长VFR+B、几何未应用范围须按声明待验，不能扩宣支持 |
| M05 | 重开：链顺序修复属实，完整载荷/层语义和 fail-closed 未完成（MAC-07） |
| M06 | 重开：基础 qualifier实现属实，CPU mask仅 helper、后处理丢弃/失败继续（MAC-07） |
| M07 | 重开：manifest/模型存在，前台写入只私有project（MAC-02） |
| M08 | 重开：协调器/签名存在；菜单stub+线程/生命周期缺口（MAC-04） |
| M09 | EXR/float/alpha声明实现存在；真实画面/抓帧人工验收仍待，未新增确定bug |
| M10 | 重开：typed字段helper存在，编译器无生产调用且字幕模式/语言未消费（MAC-08） |
| M11 | 重开：区间命令存在，删除面板无真实入口或审批提交（MAC-03） |
| M12 | 部分实施：proxy/relink生产链存在，verified ingest无消费者（MAC-09） |
| M13 | 多项模型/页面接线存在，真实UI/成片/多屏体验仍需原定验收；未把helper测试推广为全包验收 |
| M14 | sample字段/嵌套/Stem有实现测试；未新增确定bug，录音/设备/Windows释放仍依门禁 |
| M15 | 重开：比较面板可用，auto snapshot丢结果且无消费者（MAC-06）；评论/Audition helper存在 |
| M19 | provider/上下文/Scope声明有实现；本轮未复测完整三页像素/设备对拍，接受软件子件不等于实机最终色彩 |
| M20 | 重开：绑定WAV/LUT部分闭包属实，快照/候选/Composite依赖及路径/校验未闭合（MAC-10） |
| M21 | 重开：事务/producer/面板存在，运行QC线程与发布裁决缺口（MAC-11） |
| M22 | 重开：面板/算法helper存在，生产帧与提交均不闭合（MAC-01） |
| M23 | 重开：修正模型/可用性存在，生产回调/领域命令缺失（MAC-05） |
| M24 | 有理时基/metadata映射存在；活动grade metadata保留不等于外部软件可渲染对应效果；外部接收端仍待，不以字段往返关闭交接 |

## 本轮验证

执行：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/edit/test_m07_version_tools_panel.py tests/edit/test_m15_snapshot_compare_panel.py tests/composite/test_m23_tracker_panel.py tests/color/test_m22_shot_match_panel.py tests/edit/test_m02_snapshot_io_fidelity.py -q --no-header -p no:cacheprovider`

结果：**40 passed / 2.66s**。这些测试全部绿仍与以上接线缺口并存，M07测试只验 panel内部project，M22只验 create的command，M15 scheduler只验计数，M23注入假回调，均未经过真实主窗口产品入口。

只读探针输出（未改产品）：
```
snapshot_tick True backups_count 1 persisted_snapshots []
different_clips_same_frame True
delivery_issue_result [('color.delivery.dropped.qualifier_postprocess', '限定器后处理 (Qual. Clean/Blur/Denoise) — 丢失：时间线视频导出为 CPU 管线，该效果不会被应用')]
snapshot_retained_source_skipped ('a',) copied 0
layer_advanced_chain [{...基础恒等参数..., '_chain_segment': 'trim'}]
```

## 对父任务小修 Review

已阅读 git diff（ShotMatchPanel+两个回归用例）：新分析开头清空 report/reference/selection/command/list并禁用提交，能阻断失败复分析继续提交旧建议；低置信可勾选但默认 unchecked，恢复显式确认，符合约束。无新的阻断问题。根任务应以 focused 测试最终结果为准；参考切换即时失效/主工程revision更新仍属于 MAC-01 较大集成包，不因这次小修声称全部修完。

## 补充快速核对

- **MAC-13 / P2 国际化缺口**：上述八个新面板（shot_match_panel、tracker_correction_panel、m07_version_tools_panel、filler_removal_panel、snapshot_compare_panel、proxy_cleanup_panel、bake_progress_dialog、m21_multi_version_qc_panel）中 `rg 'I18N|i18n|\.tr\('` 均无匹配，大量标题/状态/错误/确认文字硬编码中文。菜单外壳的 I18N 不能覆盖面板内部。应补已有 i18n key/fallback 并在英文 locale 验证；不要求本轮大改翻译。
- **M13 patch 矩阵准确口径**：不是完全未接线。`src/features/edit/ui/edit_page.py:2731–2749` 已创建 `SourcePatchDialog.run`，该 dialog `:229` 真正调用 `channel_matrix`，显示表格；但 `:141` 设置 `NoEditTriggers`，这是随既有预设变化的只读布线矩阵视图，不能宣称任意单元格布线编辑器；主属性panel无 channel_matrix消费不构成bug。若原计划“完整patch矩阵”包含任意映射编辑，需作为 UI 能力差量另列，不能等同当前只读视图。
- M14 @48k 微调 tooltip 明确时基，未以此单独报 bug。
- M24 `note topos:grade` 属专有metadata保留，未声称 FCP 实际重建 Topos调色，可保留该实现；真实外部软件接收效果与不可表达项诊断仍待门禁。
