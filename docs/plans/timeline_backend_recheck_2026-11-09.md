# 时间线后端收尾复审与后续完善计划

日期：2026-11-09（审查从11月8日晚延续）。基准：`f1acf2b2..7dee505c`；保留原 [B01–B19实施记录](timeline_resolve_parity_review_2026-11-08.md)，不覆盖其提交与全量归因。

本轮按用户要求：小范围问题直接修；需要重新定义领域事务、时基、模型兼容或整组提交的大问题列在本表。以下“已复现”是独立模型/真实UI适配器探针；不等于完成真实麦克风或全流程音视频导出验证。

## 基线结论与证据边界

| 运行 | 结果 | 使用方式 |
|---|---|---|
| 用户已完成的后端全量 | 12254 passed / 15 failed | 用户收尾记录；本轮没有重复跑另一遍全量替代它 |
| 用户基线 `/tmp/topos-baseline3`，f1acf2b | 12073 passed / 20 failed / 117 skipped，1506.78s | 已读取缓冲日志的最终输出；未停止、改动或重跑该worktree |
| 用户完成的逐项归因 | 14项基线同样失败；trim_dual_frame线程时序双向抖动；本批次0新增稳定失败 | 保留归因结论，不把未覆盖的内容缺陷归咎于这15个测试失败 |
| 本轮独立复审 | 六项内容/事务风险见R01–R06；另列能力待完善R07–R10 | 现有测试通过与完整功能正确是两种证据；新反例需加入后续回归 |

用户报告的预存项：n03调色门/掩码×8、ui2_aud3、ripple proxy、disable shortcut、affordance手势×2、hover；trim双帧线程为双向抖动。基线另外出现import_builder×4、wd13g69×2。仅保留本次运行事实；没有将同名失败一概解释为同一根因或全部产品无害。

## 本轮已经直接修复

| ID | 问题、改动 | 验证 |
|---|---|---|
| F01 | 只改fade_curve时，实时音频同步签名与两级视频预览签名未改变。为EditPage音频/节目图像、TimelinePreviewRenderer补曲线字段；页面视频签名也补淡入淡出时长 | 先复现两项失败，再补签名回归；linear→smooth刷新，恢复原值签名恢复。没有启动GL或改变DSP算法 |
| F02 | Renderer把SpeedParams转float失败后固定当1.0，仅倒放状态变化不使签名失效；冻结帧字段也缺失。签名保留完整速度表示及freeze_source_frame，页面同步补freeze | 参数变化的纯签名测试及现有页面预览专项；不声明已经覆盖所有未来影响像素的字段 |
| F03 | 三个新命令已导出但未登记宏反序列化。补MoveSpeedPointCommand、SetClipSpeedSettingsCommand、SetFadeCurveCommand的既有registry | 三个真实MacroCommand序列化往返，修前均unknown command_type、修后通过 |
| F04 | SetFadeCurveCommand未执行就序列化，恢复时被误判为已执行，undo会清除别人的旧曲线。新增显式_had_value；兼容旧字典用_previous键推断 | 修前4个roundtrip反例失败；修后未执行命令undo明确拒绝，已执行且原值None可恢复 |
| F05 | 切片curve_offset未扣除，速度菜单显示边界40但模型API需要本地16；初载/重载也未投影easing，Smooth Ramp勾选错误 | 统一ClipItem.set_retime_presentation在初载、增量和更新时投影；点40/窗口24→本地16，真实菜单清单点及undo；smooth勾选重载保持。只修UI窗口坐标，不重写smooth积分 |
| F06 | clear_point提交的链接伙伴锁拒绝会抛出Qt槽；现在捕获已知ValueError/RuntimeError并发status，模型/history保持 | 主片未锁、伙伴锁定，清点返回错误且无状态写入；不是放宽锁规则 |
| F07 | 父TimelinePanel的WindowShortcut可抢走活动roll的J/K/L，独立view测试不能代表宿主。窗口快捷键先转交当前动态修剪，再走transport | 父面板真实Qt测试；隔离原生ProgramViewer窗口L后Δ+1、J回Δ0，播放头保持00:00:04:00，Esc收起。常规文本焦点/外部窗口守卫保留 |

播放头共用设备像素中心、TrimMonitor嵌入节目画布、随全屏画布移动及销毁/隐藏清理等此前UI修复已在基准f1acf2b内；本次没有回退。原配色、电平表保留。

## 新的后续计划表（未实施）

估时为熟悉项目的单人净工作量，包含focused tests，不含设备与评审排期。

| ID/优先级 | 具体问题与证据 | 影响 | 后续实施步骤 | 必须通过的验收 | 依赖/估时 |
|---|---|---|---|---|---|
| **R01 P1** 链接组能力与参数预检 | `speed_settings.plan_speed_settings_edit/check_capability`只检查主片；主片恒速、互链伙伴带点(50,2x)时plan仍executable，SetClipSpeedSettingsCommand提交后伙伴点1→0。见S01 | 能力矩阵外数据被静默覆盖；伙伴freeze/不同速度/不同源span也需验证 | ①统一解析互链编辑成员；②逐成员检查曲线、冻结、动画、锁、源fps/phase；③保留每成员非目标字段；④对组内不支持状态在任何apply前拒绝；⑤UI/服务共享能力判断 | 两种主片方向、伙伴curve/freeze、不同fps/span/动画、单向坏链、锁；拒绝后模型/revision/history不变；支持组apply+undo字段逐一相等 | B14/B18/B15；1–2日 |
| **R02 P1** 速度事务撤销遗漏转场快照 | SetClipSpeedSettingsCommand：clip100/转场80→2x后转场50，undo clip100但转场50。MoveSpeedPointCommand：曲线clip82/转场70→21，undo转场仍21。见S02a/b | Undo不能还原工程内容；新命令的clip快照并不覆盖轨道重建副作用 | ①列出with_clip_updated/_rebuild所有隐式变更；②apply前保存主轨、伙伴轨及ripple涉及轨的transitions；③原子恢复clip、动画、转场与followers；④异常不残留半状态；⑤序列化往返与宏undo同验 | 转场字段、时长、对齐、ID、参数全等；两命令、两个linked成员、轨道锁、连续redo/undo、ripple后继转场、no-op/失败均覆盖；不能只断言clip时长 | R01；1–2日 |
| **R03 P1** Voiceover预卷/延迟时码落点 | `_place_voiceover_clip`固定timeline_start=anchor、source_start=0。RecordingManager实际将pre-roll PCM写入take，并按首样本计算time_reference_samples。24fps anchor240、1s预卷时首样本应216；当前录音锚点标记落264，晚24帧。见VO_PREROLL_ALIGNMENT | 默认预卷就可能使录音内容错位；设备延迟校准也未消费 | ①决定保留预卷还是裁掉源In的产品契约；②以实际首样本时码、采样率、latency、实际已收集预卷计算clip锚/源In；③处理零点之前、有符号时码、post-roll；④使用既有事务提交并保留录音素材；⑤真设备smoke | PCM放置已知impulse：无预卷、1s/不足1s预卷、±延迟、anchor近0、23.976/24/29.97fps、48/96kHz，锚点样本误差在约定范围；undo删除clip不误删原录音；设备失败/迟到take不投错项目 | B10；1–3日，另加真机验证 |
| **R04 P2** 拉伸用已舍入旧时长推速 | `_on_rate_stretch_finished`和retime顶部槽采用old_speed×old_duration/new_duration。源5帧、3x、时间线2帧，拉到10帧得到0.6x，最终只有8帧。见S05 | 鼠标预览边缘与松手结果跳变；此前混fps整比例样例通过仍漏量化误差 | ①用权威源时基/fps/phase与完整积分反解目标时长；②统一rate-stretch和retime顶部的planner；③返回实际可实现时长与clamp原因用于preview；④linked成员共计划 | 5帧/3x例得到目标10帧；短源、高倍率、0.1/8边界、混fps、尾部phase、曲线与动画；取消/no-op零history；松手图形边缘=提交边缘 | R01/R02；1–2日 |
| **R05 P1** 未知fade_curve加载保存丢失 | ClipInstance.from_dict将future_exp置None；extras虽保留原值，to_dict先写None再setdefault，输出仍None。见future_curve_roundtrip | 新版工程在旧程序保存后丢曲线定义；当前字段新增缺乏完整向前兼容 | ①定义字段版本/迁移与未知值保留策略；②区分有效已知值、未支持原始值、用户显式重置；③未编辑时原样回写未知原值；④渲染降级反馈，不冒充已支持；⑤undo与未知extras一致 | future字符串/未来结构的打开→保存→重开原值不丢；用户明确改为已知曲线可覆盖；旧None默认消费不变；脏值诊断明确，真实工程包IO验证 | B16 schema兼容；1日 |
| **R06 P2** 组复制同时自动创建V/A轨失败 | 真实自动目标`__new_video_track_above__`与`__new_audio_track_below__`：planner两副本都220帧，commit却单一new_track_clips+最后pseudoID装进一条audio轨，重叠而拒绝、无status。见LINKED_COPY_TWO_AUTO_TRACKS；**基线已有** | Option复制到新增轨边界无法完成，预览计划与提交不同；不会因B11共同delta已实现而自动修复 | ①按目标伪轨ID/类型分桶；②同一宏创建所需V/A轨并各自放片；③统一link新ID与共同delta；④全部验证后提交；⑤拒绝要发UI诊断并恢复ghost | 1V/1A/2clip→2V/2A/4clip/history1；A/V同frame并互链，undo两轨与副本一起恢复；一轨冲突/锁/类型错整次拒绝；全程preview=commit | B11组复制planner；1–2日 |
| **R07 P1/P2** Trim边界帧的显示契约 | 当前只roll调用双帧；UI `_display_boundary`比例round，对reverse/曲线返回None；CPU RGB24是原素材帧；同次拖动共用generation，旧位置结果仍可能匹配新位置。属于静态确认的能力/身份缺口，本轮未用真HDR导出验证 | 可能读到旧位置边界；当前预览不能承诺节目调色/合成/HDR一致；单边trim/slip/slide无同等监视器体验 | ①明确单边/双边/四格模式与邻片空位；②权威源时钟覆盖reverse/curve/freeze/混fps/嵌套/生成器；③request_id+generation+side+clip+sourceFrame联合身份；④复用既有渲染/色彩路径；⑤失败/不支持明确占位；⑥结束按实际playhead恢复节目图像 | 慢A请求后快B到达，A不能覆盖B；切序列/隐藏/销毁迟到帧不污染；每模式边界与导出采样一致；图像色彩与缩放按契约一致；真host节目图像恢复验证 | R04与preview渲染边界；2–4日 |
| **R08 P2** MoveSpeedPoint领域命令未接鼠标 | B15命令与测试存在，但timeline_items/view未引用MoveSpeedPointCommand；当前点标识没有独立拖动状态 | 不能宣称达到Resolve速度点把手拖动体验 | R02修好undo后：①按源窗口投影边界；②独立上/下把手命中与拖动状态；③源帧/时间线坐标转换走R04 planner；④本地预览、阈值、snap、返回原位/Esc/换工具取消；⑤松手单命令；⑥不支持状态禁用并提示 | 初载/切片/倒放/多点/混fps的上/下把手分别验；linked预览与落库一致；邻点不能交叉；一次undo恢复clip+转场+动画；真实viewport手势，而非仅直接调用命令 | R01/R02/R04；1–3日 |
| **R09 P2** 独立淡入/淡出曲线UI与模型 | B16目前是一个`fade_curve`两端共用，且没有SetFadeCurveCommand的UI消费者；不是已完成Resolve两端独立曲线 | 后端可用形状不能从UI完整操作；不能用单字段伪装两端独立 | ①定义两端独立曲线及旧字段迁移；②消费者预览/实时/导出一致；③端点菜单/控制柄命中与曲线投影；④命令保留未编辑端与linked policy；⑤默认旧工程声音/画面不变；⑥自由Bezier如无模型应明确不支持 | In smooth/Out linear可共存；单端修改不改另一端；多选/锁轨/取消/保存/undo；音频与视频按各自默认曲线契约；曲线形状与实际包络采样同验 | R05、B16消费者；2–4日 |
| **R10 P2** B14 stage2/4/5剩余事务 | 用户原收尾已明确：带速度点/动画的分段stretch、freeze/reverse/rewind/ramp/reset仍未实现 | 菜单存在不能代表内容能力完成 | 继续原B14分阶段契约；复用R01组预检、R02快照、R04积分；Rewind不能只当整片倒放、0%坡不能被MIN_SPEED=0.1静默钳制；UI能力状态要有最终成功/失败回写 | 保留所有意图字段；不支持时零写入并诊断；支持时真实节目采样/音频/关键帧/持久化一致；每项原生Resolve参考与边界规则写清 | R01/R02/R04；按功能拆分估时 |

建议顺序：先R01/R02/R03/R05数据安全，再R04/R06操作一致性，之后R07–R10完整交互。R08/R09作为领域能力与UI的后续联合工作，不能仅更新“command存在”的状态关闭。

## 基线失败的后续清理（独立于上表内容问题）

| 分类 | 工作 | 验收 |
|---|---|---|
| 调色/掩码与既有功能失败 | 按用户既有归因另开小范围复现，保存环境、依赖、精确trace；不因“预存”直接skip | 每项失败有确定原因与修复，功能断言保留 |
| hover/fade像素手势负载敏感 | 查测试窗清理、激活/hover事件已送达、异步缩略图/设备cache、字体/DPI和主题；等待应围绕可观察条件 | 原像素/源状态断言不放宽；单跑/原前缀/整组对比；真实UI行为也验证 |
| Trim线程时序 | 用事件/屏障区分已开始与排队请求，明确latest-wins的“尚未开始请求”边界；每例stop并确认解码器最终关闭 | 不以加大sleep或无限timeout掩盖race；负载下也能清理并稳定断言 |

## 本轮验证与复现

- 新反例先失败后修：缓存/菜单状态两项；三个宏反序列化与未执行undo四项。修复后的focused结果见[审查证据目录](../audit/2026-11-08-backend-recheck/)。
- **645 passed / 25.41s**：34个后端与UI文件组合，包含本轮小修、速度/淡变/Replace/录音/复制/锁/路由/真实隐藏属性面板与嵌入monitor回归。随后补倒放/冻结缓存字段，相关**29项通过**（与前组重叠，不相加）。本轮未重跑全量12k测试。
- `git diff --check`、修改文件compileall通过。最终命令文件列表和原始输出见证据目录的`verification.md`。
- 本机Resolve20再次观察速率条标题/箭头/底部百分比以及开启增加高度；没有改速度或保存参考项目。CUA未捕获百分比菜单实际展开，不将其逐项动作标为原生验收完成。
- 独立Topos真实素材窗口：内嵌monitor双画面可见，L→Δ+1、J→Δ0，playhead保持00:00:04:00，Esc收起。测试用入口只在预览工具，不加入产品UI。用户原Topos窗口和未保存工程未关闭/重启。

观察探针（exit0只表示完成采集，不表示功能正确）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-08-backend-recheck/speed_review_probe.py
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-08-backend-recheck/fade_review_probe.py
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-08-backend-recheck/transaction_review_probe.py
```

这些探针重跑会反映当前工作树。UI小修对应观察可能不再复现，原始提交问题、回归断言与实际当前输出应分别读取。没有提交、部署或改写用户基线worktree；本轮修改留在当前工作树供review。
