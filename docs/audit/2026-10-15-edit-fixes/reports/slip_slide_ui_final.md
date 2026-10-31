# Slip / Slide UI 最终修复

日期：2026-10-16。范围限定：第四轮审计剩余 Slip / Slide 交互闭环。

## 完成行为

- `TimelineView` 的 Slip / Slide 在同一个不可变时间线快照上预检整组。默认 linked selection 下，视频与互惠链接音频共同约束，整组采用同一偏移；任一轨锁定或时间线在交互期间变化时整组拒绝。
- Slip 对所有链接源窗口求共同 source-frame 范围。Slide 复用领域 `get_slide_bounds` / `slide_clip`，按邻片自身 speed、reverse、FPS 和 source limit 预演，包含全部 6 个受影响 Item 的几何和源区间；保留原 gap。
- 不具备相同时间线边界的链接片段、邻片链接或边界无法一一对应的 linked Slide 明确拒绝。linked Slip 的 source FPS 不一致时明确拒绝，避免把不同长度的源帧当作同一偏移。关闭 linked selection 后允许独立编辑，即使伙伴轨锁定。
- mouseMove 仅投影 Item 的临时窗口；release 恢复预览投影，再由既有 command 提交。链接组只产生一个 Macro / 一次 Undo；零偏移、拖回原点、仅点击都不产生历史。未增加绕开 command 的模型或播放状态写入口。
- Esc、切工具、失焦、切 linked selection 会恢复全部伙伴及邻片，清空预览集合并结束交互。取消后的迟到 release 不提交。
- 修正 ClipItem Slip 在同步 finished 信号返回后把已提交 `source_start` 再覆盖成旧值的问题；共同边界由 view 统一拥有，Item 不再先行用单片边界约束。
- SlideClipCommand 透传前后邻片 source limit；before/after marker 快照随 JSON 往返，Undo / Redo 与整轨 clip / transition 快照一致。
- Slip 源窗口平移复用既有 `copy_clip_with`；保留曲线 control domain、phase，并以正反片段逐帧验证固定源位移。

## 验证

真实 Qt 鼠标回归：`tests/edit/test_round4_linked_slip_slide.py`，36 passed。覆盖 preview → commit → 一步 Undo、两类工具整组锁轨预检、途中锁轨、四类取消及迟到 release、linked 关闭独立编辑、no-op、错配拒绝、正反方向与不同 speed / source 边界共同钳制、Slip smooth 曲线 domain / phase 及逐帧源位移。

`python -m compileall` 对三个修改模块通过；`git diff --check` 通过。

相关既有回归及领域修复联合验证：201 passed / 1 skipped（12.43s）。包含上述真实 Qt 用例、`test_round4_slide_domain.py`、`test_timeline_interaction_stopgaps.py`、`test_timeline_tool_acceptance.py`、`test_timeline_ux_stopgaps.py`、`test_timeline_linked_av_regressions.py`、`test_timeline_command_regressions.py` 与 `test_fix_w6g8_aud6.py`。

联合验证曾检出 Slide 选中片段在 `.with_temporal_shift` 后丢动态扩展属性；领域修复已保留原片段动态载荷并合并位移动画，既有扩展字段契约回归现通过。

## 边界

此项不修改 Roll 方法、structural history 桥或 GPU 线程。预览不发布临时不可变模型；播放器仍通过既有正式 command 提交后的 timeline_changed 刷新。未做 git commit。
