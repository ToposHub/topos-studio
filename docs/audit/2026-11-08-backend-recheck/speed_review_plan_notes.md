# 速度后端复审条目建议

基线：`1380e8bf`（16 commits after `f1acf2b2`）。这里列的是新反例及后续修复建议，**没有修产品后端**。JSON/脚本运行成功只表示观察已采集，不等于这些行为通过验收。

证据：[可重复探针](speed_review_probe.py)、[精简观察值](speed_review_results.json)。执行：`QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-08-backend-recheck/speed_review_probe.py`。不加载用户项目，不控制原生应用，不跑全量。S03/S04是本轮交给主代理直接修复的UI问题，若重跑已修工作树，观察值可以改变；原始JSON归属上方HEAD及采集时工作树。

| 建议问题组 | 复现与风险 | 后端修复步骤 | 验收标准 |
|---|---|---|---|
| **P1 伙伴能力预检（S01，B14剩余）** | 主片恒速，互链音频带`SpeedPoint(50,2x)`；`plan_speed_settings_edit`返回可执行；`SetClipSpeedSettingsCommand`将主片new_params复制到伙伴，音频点从1个变0个，没有不支持诊断。`check_capability`只看主片。 | 1. 规划器解析完整互链编辑单元；2. 每成员检查curve/freeze、fps、source phase、时长及keyframe timing能力；3. 先完整规划后一次提交；4. 命令本身保留领域守卫，防止直接命令/AI绕过规划器；5. 明确哪些设置全组传播、哪些参数每成员保留。 | 主片/伙伴交换测试；伙伴带curve/freeze或不同源fps时不静默抹掉字段；不支持零模型/history/revision；锁成员失败原子；支持情况每成员采样、持续时间、undo/redo和保存重开一致。 |
| **P1 转场完整撤销（S02a/S02b，B14/B15/B17交界）** | Settings：100帧片+80帧尾转场，2x后转场50，undo仍50；MovePoint：82帧片+70帧尾转场，速度点80→10使片21帧，undo转场仍21。两新命令没存完整transitions，而轨道重建会钳制/删除/重锚转场。 | 1. apply前为所有受影响轨（主、伙伴、ripple followers）保存完整转场快照；2. undo按轨一次恢复clips+transitions，避免中间几何自动推导再次丢内容；3. 若持久化历史需支持完整快照版本；4. 不以逆向点移动重算代替完整撤销。 | 尾/头/双边转场，缩短钳制、间隙删除、ripple移动、linked不同轨全部覆盖；比较to_dict全字段（不能用只比transition_id的相等）；apply→undo精确还原，redo一致；含动画/淡变与转场参数、对齐。 |
| **P2 拉伸目标时长反解（S05，B18剩余）** | 24fps源5帧，3x，当前时间线2帧（5/3取整）；从2拖到10帧，UI比例算0.6x，现命令重算5/0.6=8帧，提交边缘回跳2帧。旧率伸缩与顶部retime均用相同公式。 | 1. 用源fps、timeline fps、source phase与速度积分反解目标时间线时长；2. 统一preview/commit timing plan；3. 明确MIN/MAX_SPEED钳制与整数端点策略；4. 两个UI入口消费同计划，保留reverse/pitch/interpolation及linked预检。 | 包含短片源5帧/3x→目标10，混fps、分数fps、非零phase、最小/最大速率、倒放、linked；目标边缘与最终一致或明确显示能力钳制；零变化/取消零历史；undo完整。 |

附：B15当前`MoveSpeedPointCommand`具备领域实现与直接模型测试，但在`timeline_items.py`/`timeline_view.py`搜索没有找到其引用或鼠标拖动提交入口。因此“领域命令完成”不能写成“用户已可拖动速度点”。这项属于UI接线完成度，交给主代理处理/记录。

S03：新增`clear_point`分支提交在try外，链接伙伴锁定时ValueError可离开Qt事件边界。S04：ClipItem直接投影曲线全域source_offset，菜单API却要求窗口坐标；curve_offset=24/点40时应投影16，实际投影40导致删除查找64而失败。两项由主代理按UI范围直接修复，不重复列后端工作。
