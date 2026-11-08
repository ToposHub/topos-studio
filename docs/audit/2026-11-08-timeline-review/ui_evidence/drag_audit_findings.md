# 时间线真实 Qt 手势审查证据

日期：2026-11-08，工作树基于 `720b875`，记录于本轮 UI 修复前。后续修复可能改变行号和探针输出。

- 探针：[drag_audit_probe.py](drag_audit_probe.py)
- 完整输出：[drag_audit_output.txt](drag_audit_output.txt)
- 执行：`QT_QPA_PLATFORM=offscreen PYTHONPATH=. .venv/bin/python -m pytest docs/audit/2026-11-08-timeline-review/ui_evidence/drag_audit_probe.py -q -s --tb=short`
- 结果：19 个探针执行完成。前 7 个移动/复制探针输出模型和预览值；后 12 个 trim/fade 探针断言取消/返回原位零写入。**19 passed 不代表前 7 项没有问题。**

手势通过 QTest.mousePress / mouseMove / mouseRelease 投递到真实 TimelineView.viewport，不直接调用拖动 handler。离屏 QTest.mouseMove 不保留物理 Alt/Shift 状态，因此仅固定 QApplication.keyboardModifiers 返回对应的持续按键状态；此处没有模拟命令、布局、复制或拖动处理逻辑。

夹具：V1/A1 互链，起点100帧、长度100帧；空V2/A2。缩放3px/帧，V1高82px、V2高140px。复制场景另可在A1放置200..300帧 blocker。

## 已证实、后端计划高优先级

**组复制按成员独立避让，造成互链音画错位。** Alt 拖动到220帧时，视频副本落220帧，音频副本因A1的blocker落300帧，两者仍保留相互 linked_clip_id，偏差80帧。原件保留。见输出 `AUDIT copy_audio_blocked`。

根源：`src/features/edit/ui/timeline_view.py:8962` 在每个成员循环内单独调用 `_resolve_copy_start`，`:8966` 单独写 timeline_start，`:8970` 仍重映射为互链。应由组复制计划统一计算共同 delta/冲突策略，完成原子预检和单步提交；本阶段未修改后端。

## 已证实、交给本轮 UI 修复

1. **复制 ghost 与实际落点不一致。** 原件100..200帧，Alt指向150帧；ghost显示150，释放后副本落200。见 `AUDIT copy_overlap`。预览在 `timeline_view.py:8090` 直接使用 member.new_start，提交在`:8962`才执行避让。UI应显示既有最终避让结果，不自行改变后端冲突策略。
2. **跨不同高度轨道时预览高度不匹配。** V1→V2目标轨140px，移动或复制预览仍82px，释放后才重建为140px。见 `AUDIT cross`、`AUDIT copy_cross`。`timeline_view.py:8099-8104`仅设置目标Y和位置，未投影目标轨高。UI应在预览中同步目标轨高，取消时还原原轨高。

## 通过的范围

- 正常同轨移动：起点100→120，A/V同步，单次历史。
- 正常跨轨移动：V1→V2带A1→A2，预览目标与提交目标一致。
- 无碰撞的同轨/跨轨Alt复制：保留原件，复制A/V互链，目标轨正确。
- Trim In/Out、Ripple In/Out、Fade In/Out各自拖出再返回原位：模型完全一致、undo0、interaction idle。
- 上述6类手势各自Esc取消后再释放：同样零写入、undo0、interaction idle。

## 需要现场或官方对照，不判定为缺陷

Shift对角拖动当前同时改变时间和轨道；`timeline_view.py:7882`只把Shift用于暂时禁用吸附。探针证实当前没有轴锁，但没有据此宣称Resolve应使用Shift轴锁。保持现状，等待现场/官方规则确认。
