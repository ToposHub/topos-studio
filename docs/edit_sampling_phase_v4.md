# 编辑内容 v4：源帧采样相位

日期：2026-10-15。包格式版本不变，`edit_content_schema_version` 从 3 升到 4。

## 解决的问题

整数 `source_start/source_duration` 无法单独表达子帧切点。慢放重复帧、混合帧率及 smooth 曲线切分后，新子段从整数源帧重启时钟，会改变本来应保留的画面。v4 明确保存采样时钟；画面保留以逐帧相等为验收条件。

## 持久化字段与迁移

- Clip 的 `source_frame_phase_num/source_frame_phase_den` 保存有理数源帧相位，缺省 `0/1`。分母必须大于零；CFR mapper 在最终量化前叠加相位。反放时相位表示从当前窗口高端开始消耗的源帧数。
- SpeedParams 的 `curve_time_offset` 保存原始全曲线域中的连续时间起点，单位为不含 FPS 比例的 speed 域帧。缺省 `null` 延续旧版本从整数 `curve_offset` 求原点的语义；新窗口切片显式写入连续原点。
- v3→v4 的 `clip_sampling_phase` 迁移只补缺失默认值，保留原窗口、未知字段和其他模块内容。v1/v2 经既有链到达 v4。当前及未来版本的处理仍经现有迁移管线。
- 已被旧版本整数切分而丢失的相位没有足够信息可逆推；迁移保留已保存的窗口语义。新的编辑动作从 v4 起准确记录相位。

## 操作契约

`ClipInstance.source_window_mapping_updates(source_start, source_duration, timeline_offset)` 接受新源窗口和相对原片段的裁头时间线帧数，返回应写入新片段的映射字段。该函数不改变几何、revision 或历史。纯平移及 Slip 不应重新积分速度曲线。

Split、Trim、Ripple、Insert、Overwrite、Roll、Slide 共用窗口映射。慢放切点两边可能引用同一个源帧；时间线窗口仍严格相接。源窗口是否相邻不能替代画面保留的正确性检查。

`ClipInstance.timeline_to_source_frame_pair(absolute_timeline_frame)` 返回 `(source_relative_a, source_relative_b, b_weight)`。它统一处理 FPS、相位、反放和冻结；分数入点允许采样相邻源 handle，因此相对帧号可为 -1。预览及导出不能把实际时间线帧直接交给 SpeedParams 的无 FPS 局部 API。

`roll_trim_delta_bounds(left, right, left_max_source_frames, right_max_source_frames)` 返回合法整数 delta 区间。交互预览与 command 提交使用同一边界。已知素材总帧数时两侧头尾都校验，未知素材尾部不伪造上限。

曲线裁窗保留原 `speed_points/curve_length`，以 `curve_time_offset` 表示连续原点。向左扩展允许负 clock，曲线域外按首尾速度延伸，不在扩展时重建 smooth 曲线或重新积分已有片段。修改基速、速度点或 easing 时，以当前窗口起点的连续源坐标重新求新曲线原点，保持分数采样相位。

Trim/Roll/Ripple 删除的内容所带 scoped marker 同步删除；Ripple 裁头后保留的 marker 随内容左移。Undo 从完整快照恢复曲线、相位、marker、转场，命令 JSON 保留这些快照。

## 验证

`tests/edit/test_round4_core_time_windows.py` 覆盖正放/反放、同帧率/30→24/25→24、慢放/1.25x/step/smooth、首尾切点/连续切分、工程 JSON、Trim JSON Undo/Redo、Insert/Overwrite/Ripple、marker 所有权、Roll 双侧素材边界与 frame blend。旧的 24 组切分测试现全部要求逐帧一致，已移除 ±1 容差。
