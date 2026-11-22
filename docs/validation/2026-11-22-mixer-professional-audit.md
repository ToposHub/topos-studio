# 2026-11-22 混音全面审计与UI实施证据

本报告对应[完整实施计划](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-22-professional-film-mixing-plan.md)。生产后端保持原状；本轮修改生产UI、显示快照与旧路由UI的模型访问/窗口归属，继续使用原有路由命令。上轮Properties改动保留。

## 已复现的待修后端问题

[诊断脚本](/Users/heng/Documents/vscode/topos-studio/docs/validation/2026-11-22-mixer-professional-audit.py)实际调用生产代码，只对确定性DSP fixture和无处理栈的测试环境注入。输出写入[JSON](/Users/heng/Documents/vscode/topos-studio/docs/validation/2026-11-22-mixer-professional-audit.json)。

| 检查 | 实测 | 应有行为 | 计划 |
| --- | --- | --- | --- |
| 串联Bus与直达PDC | sample100输入得到[100,500]两脉冲，声明延迟200 | sample400对齐，合成幅度2 | B02 |
| Trim离线消费 | 输入0.5、trim0.5仍输出0.5 | 输出0.25 | B03 |
| 交付QC缺峰值 | 只有−23 LUFS字段仍passed=true | 缺测量阻断 | B04 |
| 声音签名 | 单改Send.tap/muted、Trim、VCA仍未变 | 声音变化使快照/同步失效 | B01 |
| VCA叠加Read曲线 | 静态输出0.25；加恒1音量曲线变0.5 | 两者均0.25 | B03 |

运行命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/2026-11-22-mixer-professional-audit.py
```

退出成功只表示诊断收集完成；`contract_satisfied=false`明确代表尚未修复。这些结果没有被伪装成通过的验收。另有代码检查项：既有routing preview/rollback/commit固定传is_bus=False，以及output+sends逐条命令而非整体原子事务，已纳入B01/B02待办；本轮未修后端。

## UI验证

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/mixer tests/app/test_audio_mixer_initial_sync.py \
  tests/edit/test_record_arm_wiring.py tests/edit/test_routing_ui.py \
  tests/edit/test_track_color_ui.py -q --tb=short
# 413 passed / 154.66 s（收尾前整组）

QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/mixer/test_console_navigation.py tests/shared/test_audio_mixer_compact_ui.py \
  tests/mixer/test_mixer_properties.py tests/app/test_audio_mixer_initial_sync.py -q --tb=short
# 49 passed / 20.37 s（最终导航/表计/布局/清工程修正后）

QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/mixer/test_project_workspace.py::test_routing_overview_opens_owned_editor_and_shares_timeline_history \
  tests/edit/test_routing_ui.py tests/app/test_fix_wd17w2_aud.py \
  tests/app/test_ef08_aud.py -q --tb=short
# 88 passed / 12.50 s（最终实际路由入口/窗口parent/模型读取修正后）
```

各组重叠，不应加成独立测试数量。测试运行在macOS、Python3.12.14、PyQt6/Qt6.11、offscreen；没有使用测试通过声称物理设备/外部格式认证。

新增13项导航用例覆盖搜索/快捷键、类别/solo/arm筛选、隐藏与声学状态隔离、重排/删除/恢复、峰值即时复位、路由总览/上游/只读元数据、关工程清理、200/220/320/560px布局。新增生产端到端路由用例从总览打开已有RoutingDialog，修改输出后验证时间线模型和总览，关闭后撤销恢复。实际运行发现并修复旧UI读取不存在的`TimelineView.timeline`及把`window`方法传作parent的问题；没有改路由DSP或命令算法。

## 计划与算法示例验证

- 只读核心审计回归85项通过：monitor、VCA、Reconform、delivery、dialogue、PDC、surround bus、graph snapshot；不覆盖上述全部组合反例。
- `.venv/bin/python -m pytest docs/plans/examples -q --tb=short`：18项通过。
- 主计划内B10–B20共11段独立Python例码实际运行通过；B06空间声像与B24Wiener教学例运行通过。
- 89个本地文件链接均存在；新生产Python AST、翻译JSON、审计脚本编译和`git diff --check`检查通过。
- Explore/方案/Review按AGENTS工作流完成。Review指出的Armed/Solo刷新、Trim单位、tap顺序、静音QC、ADM UID复用等已修正文档或UI。

## 视觉证据

下图由真实生产Qt控件、合成通道元数据与项目深色主题离屏渲染。电平处于静音；它们证明界面布局，不能证明声音引擎或硬件试听。

![中文生产混音台](/Users/heng/Documents/vscode/topos-studio/docs/audit/2026-11-22-mixer-professional-audit/console-zh.png)

![200px窄面板](/Users/heng/Documents/vscode/topos-studio/docs/audit/2026-11-22-mixer-professional-audit/console-narrow-zh.png)

![中文实时路由总览](/Users/heng/Documents/vscode/topos-studio/docs/audit/2026-11-22-mixer-professional-audit/routing-zh.png)

待后续实施：所有B00–B24后端任务、真实监听、Windows声卡、AAF接收端、原生多声道硬件、外部插件及90分钟性能验收。当前仅完成计划、代码审计、指定UI与对应自动化验证。
