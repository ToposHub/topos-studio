# 第四轮 UI 与历史提交审查

复查基线：8330e42df；共享工作区上的第四轮修复。复查与追加修复延续至 2026-10-16。
范围：EditController、TimelineView / RollHandleItem、Inspector、DragValue、TimelineSettingsCommand，以及现有 Multicam / Nest 创建的项目和时间线联合事务。

## 复查时确定的问题与处理

| 问题 | 触发与影响 | 已完成修复与验证 |
|---|---|---|
| 真实 Roll release 提交顺序错误 | View 在 scene 投递 release 之前结束 interaction，RollHandle 的 finished 回调因此拒绝真实手势 | 先投递 scene release，再 finally 收尾；普通、慢速、2 倍速、反向 Roll 的 preview / commit 源区间一致；零位移点击关闭监视器且不留历史 |
| Roll 显示监视器后再激活外窗，取消保护失效 | 某些平台上 Tool 窗口已经让 View 失焦；忽略自身监视器的第一次 FocusOut 后，第二次真正外窗激活不再向 View 发 FocusOut，迟到 release 仍能提交 | 监视器添加 WindowDoesNotAcceptFocus；保留精确自身监视器例外，同时观察 QApplication.focusWindowChanged 和 applicationStateChanged。真正其他窗口激活 / 应用失活取消交互并恢复双片；迟到 release 不入栈。真实外窗激活测试与应用失活信号测试通过 |
| Undo 后 Inspector 快照落后于 canonical 序列 | X 修改→Undo→Y 修改，可把旧 X 再写回 | 正常提交、Undo / Redo 后替换 Inspector 的 immutable snapshots，并刷新控件；后续单字段编辑沿用恢复后的值 |
| 多选 mixed 显示及显式相同值提交 | 值不同的控件显示首片值；再次输入与首片相同的值可能不发提交 | Numeric 显示“—”，checkbox 使用 partial state、combo 使用 placeholder；显式输入相同主值也提交给每片，其他字段分别保留；程序化刷新不发提交 |
| 多时间线 / 新工程历史混用 | 共用单栈的旧时间线命令会进入另一时间线或新工程 | 按 timeline_id 隔离历史；切换工程清理所有历史；外部替换时间线内容使旧历史失效；切回原序列可继续 Undo |
| 设置与片段编辑不在同一历史 | 设置改变帧率后，旧帧域命令可能以新帧域 Undo；工程整快照撤销可能覆盖后续片段编辑 | TimelineSettingsCommand 在同一序列历史内保存完整 before / after；设置→片段编辑→连续 Undo / Redo 保持原顺序、帧域与项目引用一致；未知 payload 保留 |
| 降帧率压缩相邻一帧编辑导致未捕获 ValueError | 48 fps 的相邻两个 1 帧片段改为 24 fps，量化后重叠，EditTrack 构建拒绝；原错误直接逃逸 Qt 槽 | 在任何发布 / 切换 / 入栈之前整体拒绝设置提案，状态栏给出不可保留编辑的原因；工程、序列、revision、历史保持原状。保留片段需要的最低帧分辨率不足时不静默挪动剪辑 |
| 设置对话框嵌套事件循环使用陈旧快照 | 对话框打开期间后台操作发布了序列，接受设置会从打开时的旧序列重建，覆盖后台提交；期间切换工程还可能写入旧工程设置 | 接受后核对工程与目标仍存在，并重取最新 canonical 序列，再生成设置变换与 Undo 快照。期间发布的 X=99 在设置及设置 Undo 后均保留；切换工程后丢弃旧对话框结果 |

上述前六项同时核查了本轮此前已落地的修复；外窗取消保护、降帧率拒绝和设置对话框陈旧快照是在本次审查中追加闭合的路径。

## H05：现有结构操作的 Undo / Redo

新增纯域模块 `src/features/edit/core/timeline_project_transaction.py`：

- `TimelineProjectTransactionCommand.create(before_sequence, after_sequence, *, before_project, after_project, before_child_sequences=None, after_child_sequences=None, label=...)`。
- `apply / undo` 只处理 active sequence；`prepare_project(project, sequences, undo=False)` 预检并合并项目 assets、timeline refs 与显式 child sequences 的变化 key。
- 快照 deep copy，序列对比忽略 publication revision；项目其他 key 保留。工程身份、active sequence 内容、被触及资产 / ref / child 的内容不匹配时明确拒绝。
- 删除本次创建的 nested asset / child 前，扫描其他序列与 nested assets 的引用；被后续其他操作使用的对象不得静默删除。
- TimelineView 在 CommandStack 触碰 Undo / Redo 栈之前执行完整预检；失败反馈状态栏且保持历史、序列和工程原样。
- combined signal 一次发布 merged project、active sequence、child delta。Controller 同步缓存 / Inspector / media pool / renderer / dirty，之后才刷新 TimelinePanel 内容，避免时长信号提前请求旧渲染状态；project 与 sequence 每次有效 execute / Undo / Redo 各推进一次 revision。
- Multicam / Nest 创建使用此入口后，普通片段编辑继续在同一时间线历史中排序。Undo 创建恢复旧组 metadata，移除本次新增 nested 资产 / ref / child；Redo 使用原 ID。
- 显式拒绝把此命令嵌套在 Macro 中执行，避免只恢复序列却漏提交工程 payload；JSON / Macro 反序列化识别该类型。这个桥针对现有创建操作，不隐含全工程所有独立历史栈已经合并。

`tests/edit/test_round4_project_transactions.py` 的 10 项真实 View / Controller 桥测试覆盖：创建→普通编辑→逐步 Undo / Redo、revision 单次递增、ID 稳定、无关项目修改保留、资产 / child 冲突零写入、外部引用保护、Macro 明确拒绝、JSON 往返与快照隔离、no-op 保留 Redo / revision / dirty、序列 payload ID 校验与完整发布后才刷新 Panel。

Multicam / Nest 构造、独立同步命令及相应纯域测试由媒体复查分支负责，保存在同一共享工作区；本报告的 10 项测试单独验证 UI 提交桥。

## 验证结果

执行：

```text
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
  tests/edit/test_round4_project_transactions.py
  tests/edit/test_round4_settings_history.py
  tests/edit/test_round4_interaction_commit.py
  tests/edit/test_round3_a_roll_focus.py
  tests/edit/test_round3_a_interaction.py
  tests/edit/test_recheck_c_multiselect.py
  tests/edit/test_edit_properties_panel_layout.py
  tests/composite/test_drag_value_widget.py
  tests/composite/test_drag_value_math.py
  tests/app/test_edit_controller_timeline_management.py
  tests/app/test_edit_controller_timeline_metadata.py
  tests/edit/test_revision_and_source_av_routing.py
  tests/edit/test_timeline_ux_stopgaps.py
```

结果：**122 passed，1 skipped / 5.64s**。完整输出保存在同目录的 `ui-regression.log`。第四轮 UI / 设置 / 结构桥三个 focused 文件包含 **33 项**；它们包含在上述结果中，不相加。全量分片发现的两个旧 fixture 已更新：revision 测试使用真实 CommandStack，set_clips 测试使用已初始化的真实 Qt widget。

`git diff --check` 通过。测试使用真实 Qt widget / graphics scene 的鼠标 press / move / release；本报告不把 offscreen 检查冒充完整 MainWindow 原生视觉测试。MainWindow 实际素材和关联音视频 Roll 交互由主复查继续验证。
