# 第四轮核心改动二次审查

日期：2026-10-15；工作区基线 HEAD `8330e42df`，审查共享未提交改动。

审查范围：`clip_params.py`、`timeline_v2.py`、`timeline_frame_mapper.py`、`timeline_ops.py`、`commands.py`，以及编辑内容 schema 4 迁移。遵循 AGENTS.md 与 .clinerules；未提交 Git。

## 本次审查发现并修复

1. **frame_blend 切分残片丢采样 handle。** 30→24fps、0.5x、第三帧切分时，左残片最后一帧原采样 `(124,125,0.25)` 变成 `(124,124,0.25)`；反放也发生。`with_right_edge_source_values` 现在保留最后一帧实际使用的两源采样，`split_source_partition` 统一复用该窗口计算。新增六种切点、正反向断言；额外探索覆盖三种帧率、四种速度形态及五种切点，逐帧采样对与权重无差异。
2. **合法 Roll 被旧 base-speed 校验二次拒绝。** 原基速 8x、当前切片处于 0.5x 段，右片段 source_start=2 时，shared bounds 允许 -1，但旧检查计算 -8 并报越界。去掉旧重复判断，统一按已验证的实际曲线源窗口提交；新增保留帧与 Undo 断言。
3. **多机位跨 FPS 切角使用了错误的源帧单位。** `SwitchMulticamAngleCommand` 复用 `multicam.map_angle_source_frame`，先扣活动角度偏移，再按目标/活动源 FPS 换算，最后加目标角度偏移。
4. **切角 JSON/Undo/Redo 不完整。** 原 JSON 丢 active_sync_offset 和目标 FPS；内部切分 Undo 会丢掉重锚转场与已迁移 marker 所有者；Redo 随机产生新右片段 ID。现在持久化前整轨与 marker 快照、完整同步/FPS 参数，Undo 原子还原，Redo 复用右片段 ID。边界及内部切换均验证 JSON 往返后精确 Undo/Redo。旧命令缺快照的兼容回退保留。
5. **未指定目标 FPS 的换源缩短混合 FPS 窗口。** `UpdateClipSourceCommand` 默认沿用原片段的完整 source/timeline FPS 比；30→24 示例 source_duration=120 不再误变为 96。
6. **同 FPS 曲线换源重积分原曲线。** 原 smooth、source_duration=120、timeline_duration=96，换源把曲线源域改为96，导致42个保留帧变化、最大22源帧。曲线换源现在保留源窗口/完整曲线域，新增 linear/smooth、正反向同 FPS 验证。

## 其他确认

- Source Overwrite 的 `OW V + OW A + Link` 宏保留新旧左右 A/V 互链，Undo/Redo 和随后的 Linked Insert 均通过；validator 无悬空链接/marker。
- Trim/Ripple/Roll 的完整 marker/transition/clip 快照可经 JSON 往返恢复。
- schema 3→4 迁移缺省补相位/曲线时间原点，未知字段保留且二次迁移幂等；没有可逆恢复旧工程已丢失相位的声明。
- 保留画面测试要求逐帧相等，没有恢复旧 ±1 容差。

## 明确拒绝的未支持组合

跨 FPS 切角若原片段含 speed_points，整数曲线控制域不能靠 round 无损换算。依主代理协调结果，UpdateClipSource 和 SwitchMulticamAngle 两入口均在任何 split、模型写入及 Undo 快照之前 preflight ValueError，给出同源 FPS 或去掉曲线的可行操作。新增直接换源、边界切角、内部切角三种零变更拒绝测试；UI 信号反馈由交互审查代理接入。该组合不声明为已实现，未来需正式有理数曲线域方案。

## 验证

Focused 交叉命令包含：`test_round4_core_time_windows.py`、`test_multicam.py`、`test_multicam_nested_timecode_truepeak.py`、`test_recheck_c_structure.py`、`test_recheck_b_slice_planner.py`。主代理另行运行全部 Edit 回归。

新增项目事务类 `TimelineProjectTransactionCommand` 已登记 Macro JSON restore；公开 JSON roundtrip 测试验证 assets/ref/child payload、prepare/undo revision 及 Macro restore 类型。UI 层明确拒绝把该事务包进 Macro 执行，项目事务通过直接提交入口原子提交，避免 Macro 只写序列丢项目 payload。

最终核心 focused：`test_round4_core_time_windows.py`（87项）+ `test_fix_wd9g55_aud.py`（55项）共 **142 passed / 10.48s**，见 `../core-review-junit.xml`。之前包含旧 multicam/structure/slice 的交叉范围 **143 passed / 11.93s**（该次尚未加入后三种拒绝测试及项目事务 JSON 测试）。

## 全量分片回归后的补查（2026-10-16）

- 保持旧 duck-typed FrameMapRequest 客户端兼容：新增 source_frame_phase 是可选字段，CFR 映射与 cadence 模板复制均通过 getattr 缺省到 Fraction(0)，不要求历史适配器同时升级。原 source-time 三项语义测试继续保持原断言。
- 冻结帧修剪边界是真实产品回归，已修：rich fixture 的 freeze_source_frame=42 使新 shrink helper 将 source_window 错缩成 [0,43)，而不是潜在播放窗口。冻结片段现在按未冻结播放时钟计算源窗口，继续保留 frozen sample=42；23.976→25fps、100裁到80时间线帧的 source_duration 仍是81。没有放宽旧测试断言，也未删除 FPS/PTS 载荷。
- 公共 get_roll_trim_constraints 现直接复用 roll_trim_delta_bounds，同时验证两片段同轨、相接及轨道可编辑；常速 mixedFPS/反放、smooth 曲线源窗口的 numeric 范围与命令提交一致。未用旧 source_delta 当 timeline_delta。新增公共 helper 正反曲线及 gap 拒绝验证。

补查范围结果见 `../core-review-followup-junit.xml`。

## 混合 FPS 整数窗口测试契约澄清

`test_source_time_domain_mapping::test_trim_mixed_fps_ratio_applied` 的旧断言用 Python `round(10×30/24)=12` 规定整数 source_start。真实 CFR mapper 已采用统一 half-up，因此原片段 timeline10 的采样是13。v4 裁头写 source_start=13、phase=-1/2，未量化的绝对位置仍为12.5；它和 source_start=12、phase=+1/2 在像素域等价。

没有为旧断言更改真实画面采样。测试现在验证实际 Trim 提交、精确有理相位、全部70个保留时间线帧的源样本完全相等、双源 frame_blend 索引和权重完全相等，以及命令 JSON Undo 精确还原。没有容差或仅将12改成13的弱断言。结果见 `../core-review-pixel-domain-junit.xml`。
