# 剪辑页面实施后复查与新优化计划表

日期：2026-10-14。复查版本：`833dcd382d40f3ff75cb31bc3e3374814efa2b1b`；对照实施前版本：`55351a5f45d5a34ff2fc91a3cb9eee53581a889b`。

项目根目录：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`。

本轮重新检查了已实施批次、命令与模型组合、真实素材的可见窗口交互，以及工程保存和导出。结果是：基础路径有明显改善，但还不能把上一轮全部问题关闭。合并同一根因后，记录 **31 组当前可复现问题（24 组 P1、7 组 P2）**；另列 **4 项明确尚未实施的工程事务工作、16 项工作流补齐任务、1 项测试维护任务**。条目内的多个复现场景不重复计数。未验证的硬件、长时播放和外部软件兼容性单列验收门禁。

本次仅新增审计文档、复现脚本和证据，没有修改生产代码、原测试、旧计划或用户素材。旧计划保留作历史对照：[上一轮计划](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/edit_page_full_audit_optimization_plan_2026-10-14.md>)。

# 0. 实施状态索引（2026-10-14 复查批次 A–F）

本节由实施方追加；正文原审计内容保持不变。提交映射：A=`e29ddc77d`、B=`9b36b71e0`、C=`42a358a72`、D=`2e12d04c7`、F(部分)=本次提交。

| 条目 | 状态 | 说明 |
|---|---|---|
| R01 锁轨守卫 | ✅ 已修 | merged helper 补 is_locked；Move/Trim/Delete/Insert/Overwrite/RippleTrim/UpdateClipParam 七类命令统一守卫（拒绝先于快照回写） |
| R02 反向右修 | ✅ 已修 | new_source_start 单独生效；右修保持 timeline_start；undo 还原 source_start |
| R03 跨轨 delta | ✅ 已修 | ripple offset 走 timeline_duration_for_source |
| R04 转场重锚 | ✅ 已修 | roll 单次原子重建 + 转场快照；Macro 复活通道（最终仍相接的转场不丢，真分离仍删） |
| R05 fade 钳制 | ✅ 已修 | 显式左/右修、with_speed_params、ChangeSpeed 单步构造；Trim/Speed 快照 fade+转场 undo 还原并随 JSON 往返 |
| R06 曲线积分 | ✅ 已修 | smooth 委托曲线积分×fps 比；变速点正/逆映射按 ratio 折算 |
| R07 统一切分 | ◑ 部分 | mapper 改 half-up；split_source_partition 统一 Split/Insert/OW/Roll/legacy-trim。24 矩阵 21/24 逐帧保留（基线 4/24）；余 3 组切点落 .5 分数相位，需分数相位 schema（DOMAIN-01），测试固化 ±1 帧现状 |
| R08 目标轨同移 | ✅ 已修 | move/delete/insert/ripple_trim 的 keys+scoped marker 共同位移 + undo 快照 |
| R09 快照 JSON | ✅ 已修 | Move/RippleDelete/BatchDelete/Slide/Split 新快照字段 to_dict/from_dict 往返 |
| R10 seam marker | ✅ 已修 | 半开区间归右半 |
| R11 逆映射 FPS | ✅ 已修 | ratio 折算，正反映射可逆 |
| R12 no-op 历史 | ✅ 已修 | CommandStack 同一性判定：不 push、不清 Redo |
| R13 Roll 取消 | ✅ 已修 | ROLLING 会话 + handle.cancel_drag（ungrab 不发 finished）+ 迟到 release 丢弃 + 素材界钳制护栏 |
| R14 取消恢复 | ✅ 已修 | _preview_affected_item_ids 登记与模型重投影（linked/slide/ripple）；primary 回退护栏 |
| R15 Properties Speed | ✅ 已修 | 改道 ChangeClipSpeedCommand；UpdateClipParam speed 分支同公式兜底 |
| R16 多选原子 | ◑ 部分 | 批量参数/Reset 单宏 + 锁预检整组拒绝 + 每片原值派生；mixed 控件显示（未改字段标记）未做 |
| R17 disabled 投影 | ✅ 已修 | full/incremental 传 is_enabled；轨道态不再覆盖 clip 自身 |
| R18 预览源域 | ✅ 已修 | 预览与 release 共用边缘映射（含 linked 按自身时基） |
| R19 锚定精度 | ◑ 部分 | 连续 double 帧 + round 补偿：全部 ppf ≤0.90px、无方向性累积（基线 -22.73 帧）；整数滚动条 ≤1px 量化为平台精度，亚像素原点（布局/viewport 机制）待后续 |
| R20 字幕轨 FPS | ✅ 已修 | subtitle_tracks 参与重建；word_timings 字段存在时同域缩放 |
| R21 Bezier handle | ✅ 已修 | 绝对 handle.x 同倍率缩放 |
| R22 源 A/V | ✅ 已修 | InsertLinkedAssetRippleCommand（同步切分+波纹+一次跨轨位移+双轨快照）；OW 音频侧覆盖命令；Append 共同落点；Overwrite/Append 补 fps |
| R23 multicam 时基 | ◑ 部分 | 切角 fps 换装 + 源窗口按新 fps 保持；组公共 rational 时基/网格映射/重同步迁移（F08）未做 |
| R24 Nest Bus | ✅ 已修 | Bus 依赖闭包（含递归）拷入 child 并重映射 |
| R25 revision 单调 | ◑ 部分 | ProjectCommandStack execute/undo/redo 均推进；统一 commit adapter（file_controller 边界/媒体池 UI 接线）待做 |
| R26 proxy 代次 | ✅ 已修 | 会话代次 + set/clear_project 注销 + 二阶段探测 source 校验 |
| R27 真峰边界 | ✅ 已修 | 镜像上下文 + 扫描延迟 + finalize；任意分块=整段精确一致；progress 取消 |
| R28 extras | ✅ 已修 | Track/Sequence _extra_fields（含 _rebuild 保留）；兼容 JSON 补 revision |
| R29 逐像素 gate | ✅ 已修 | 深压缩与 dark_mask 相交（批内无关像素零影响） |
| R30 LUT 安全门 | ✅ 已修 | u8 域 7³ 探针验证 ≤1 LSB，跨 RGB 组合拒走快路 |
| R31 导出 shader | ◑ 部分 | GL/Metal 补暗部保护块 + hue 后重算 gray；跨后端 golden 基线与 HDR hue 语义（clamp 策略）待后续冻结 |
| U01 设置事务 | ◑ 部分 | UpdateTimelineSettingsCommand 领域事务（单调 revision）；控制器命令栈接线随 R25 adapter |
| U02/U03 工程事务 | ❌ 未做 | multicam/Nest 创建的 project command 化（依赖 R25 基建） |
| U04 Replace probe | ✅ 已修 | 提交前探测类型/元数据随新源（同步探测与导入同口径）；usage-index 兼容预检/用户处置策略未做 |
| T01 测试维护 | ✅ 已修 | no_deblock fixture、sync-lock emit 契约、Metal scratch 键迁移+复用断言 |
| F01–F16 工作流 | ❌ 未做 | 16 项功能建设（见 §5）；R22 已覆盖 F01 的基础 stream patch 部分 |
| 验收门 G01–G07 | ❌ 待验收 | 可见窗口/真实设备验收未执行（本批全部为 Q/M/T 级证据） |

回归测试：新增 `tests/edit/test_recheck_*.py` 共 10 文件 96 项；相关 edit 测试 225 文件 sweep 通过（3 个历史失败均为本轮 T01 已修复或环境项）；tests/color 全量 2419 过。R19 的 ≤1px 量化与 R07 的 3 组分数相位为本批声明的能力边界，非隐藏放宽。

## 1. 复查方法与证据边界

| 标记 | 本轮实际做了什么 | 可以支持的结论 |
|---|---|---|
| G | macOS 上显示完整 MainWindow，使用 Qt QTest 鼠标、键盘、拖动事件和真实按钮；隔离配置、审计专用临时工程 | 可见窗口操作、实际槽函数、模型提交和截图；是自动交互测试，不称为人工操作 |
| Q | offscreen 的真实 TimelineView、ClipItem、属性控件和命令栈事件测试 | 交互状态和模型变化；不证明可见 GPU 像素或操作系统失焦行为 |
| M | 真实模型、命令、序列化和实际 handler；部分 handler 使用轻量宿主或服务 spy | 可确定领域逻辑错误；不冒充完整窗口端到端操作 |
| T | 现有 pytest，包括实际 OpenGL/Metal 导出测试 | 指定测试路径的结果；失败需区分生产问题与过时断言 |
| S | 当前源码、差异、调用链与能力注册表 | 实现缺口和原因；没有动态复现的性能/取消问题不称为实测故障 |

环境：Python 3.12.13、PyQt6 6.10.2、Qt **运行时 6.10.2**、Qt 编译版本 6.10.0、`cocoa`。可见窗口为 1440×1000。使用用户提供的 `tests/test_videos/2K_h264.mp4`、`2K_prores422Proxy.mov`、`2K_topos_422_Proxy.mov`，正常导入三份真实素材；主要连续操作使用 H.264。合成 FPS/速度/锁轨边界 fixture 另标 M/Q，没有把占位路径当成真实视频解码。

可见窗口驱动为保留后续证据安装了审计专用 `sys.excepthook`，记录未捕获 Qt 槽异常。因此“Source 覆盖抛异常”有真实调用链证据，但本轮没有据此宣称未经该钩子的生产进程一定崩溃。取消矩阵的 Alt 场景只在鼠标移动派发期间提供受控全局 modifier 快照，真实桌面持续 Alt 仍需补验收。

全部证据在 [本轮审计目录](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/README.md>)。详细原因、精确文件行、完整输出及原问题状态矩阵分别见：[核心复查](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/reports/topos_recheck_core.md>)、[交互复查](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/reports/topos_recheck_interaction.md>)、[功能与工程复查](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/reports/topos_recheck_features.md>)、[颜色保真复查](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/reports/topos_recheck_render.md>)。

## 2. 已改善的功能与实际测试结果

| 范围 | 本轮结果 | 仍需区分的边界 |
|---|---|---|
| Source 预览与播放 | G：Source 标签真正可见、有实际视频像素，`paint_failed=false`、纹理有效；播放帧 100→126。截图 09/10 | 验收了 CPU 上传纹理路径；未证明 GPU shared texture 的所有首帧场景 |
| Source I/O | G/Q：Clear 后三个编辑按钮禁用，I=O 单帧合法且按钮启用，close/range_changed 契约正确 | 多流编辑和混合 FPS 仍有 R22；范围修复不能替代编辑事务验收 |
| 时间线基础取消/工具 | G：普通 Move 按 Esc 不提交、Hand 只滚动；Q：波纹单击、Blade 右键/锁轨、普通/多选/linked Move 等取消通过 | Roll seam、linked trim、Slide 仍有 R13/R14 |
| 快捷键、字幕导航、选择 | Q/S：清空 B 不再回退刀片，N 名称修正，Shift 框选 ID 去重，字幕 Fit/边界跳转/删除询问通过 | 多选属性仅标题/上下文修好，实际写入和原子 Reset 未完成 |
| 核心原始例子 | M/T：基本 Roll/Insert/Slide live Undo、反放首帧、常速 mixed FPS、部分转场/marker/fade 示例改善 | 复杂曲线、分数采样、整组转场、JSON 命令载荷仍不能关闭 |
| Program 播放 | G：播放头 0→46、44 次有效 frame_ready，截图 20 显示实际画面 | 未做耳听、长时 A/V 漂移或 4K60 验收 |
| 工程 ZIP 保存/重载 | G：两条时间线、三素材成功保存并读取，project revision 3→3 | 一处非法 reciprocal link 被加载规范化清除；见 R22。不是全 payload 原样通过，也不是普通 ZIP 保存普遍丢 revision |
| 短片导出 | 两条真实素材时间线各导出 49 帧、640×360 H.264，服务返回 success、audio_omitted=false | 为短片服务烟测；不是全部交付设置、无损色彩或硬件编码路径验收 |

测试统计按独立运行记录，不把不同分工重叠的测试累加成“总覆盖数”：

- 新增修复测试文件：**107 passed、1 skipped**。跳过的是 `ShortcutManager unavailable` fixture；清空 B 的实际 Qt probe 另已通过。
- 既有失败范围重跑：**168 passed、9 failed**。其中 **4 个数值失败对应 R30/R31 两个生产根因**；另外 **5 个失败是三类测试契约/fixture 问题**，见 T01。R29 为额外生产数学探针确认的问题。
- 核心补充运行：150 passed；另一范围 91 passed。功能分工三组为 46、72、66 passed。均保留各自记录，存在重叠，不能相加作为独立用例数。
- 取消矩阵：18 场景×Esc/切工具/FocusOut＝54 组合；42 组合干净，12 组合存在提交或预览残留，其中 2 组合实际提交；0 个未捕获异常。具体失败为 R13/R14。

## 3. 当前问题总表

优先级：P1＝可能错误修改内容、打断正常工作流、撤销不完整、结果保真或异步隔离错误，应优先修；P2＝交互一致性、精度、兼容与效率问题。所有条目状态均为待修复；“新增回归”只用于差异和复现支持的场景，渲染既存问题不归因于本轮修改。

| ID | 优先级 | 问题及实际现象 | 相对上轮状态 / 证据 | 详细追踪 |
|---|---|---|---|---|
| R01 | P1 | 锁轨守卫不完整：属性 X 55→77 仍能改锁轨；累计波纹删除把锁定音轨 follower 100→80；多类领域命令绕锁 | 部分修复；累计 helper 新回归；G/Q/M | K01 + RI-03 |
| R02 | P1 | 反放右修剪忽略新源入点；首帧 199→189，显式成功路径 Undo 仍不恢复 source_start | 部分修复，新 factory 接线缺口；M/S | K02 |
| R03 | P1 | 30fps 源插入 24fps 序列：实际时长 24，跨轨 ripple delta 仍 30，followers 124/130 错位 | 新时长修复的组合回归；M | K03 |
| R04 | P1 | Roll/整组 Move 的中间态重锚删除合法 seam 转场，转场 1→0→Undo 0 | 新重锚策略回归；M | K04 |
| R05 | P1 | 显式左修剪/5x 变速带 fade 抛 ValueError；普通修剪 Undo 不能恢复被钳制的 fade/转场长度 | 部分修复，新入口未覆盖；M/S | K05 |
| R06 | P1 | 有 speed_points 时忽略 FPS；smooth 曲线 helper 算 90 帧、真实积分 102 帧 | 部分修复/新时长路径分叉；M | K06 |
| R07 | P1 | 24 组 FPS×方向×速度切分中 20 组保留源帧变化；Insert/Overwrite/Roll/左修剪仍有旧公式 | 统一 slice 尚未完成；M | K07 |
| R08 | P1 | 目标轨 follower 移动时动画 key/marker 未共同迁移，opacity 改变、marker 跑到 owner 外 | 部分修复，仅 sync helper 完善；M | K08 |
| R09 | P1 | 新增 Undo 快照未完整写入 command JSON；载荷往返后丢转场、Slide 几何或 marker owner | 部分修复，新增快照契约漏项；M | K09 |
| R10 | P2 | marker 恰在 Split seam 时归左半，违反 `[start,end)` | 部分修复；M | K10 |
| R11 | P1 | source→timeline 逆映射漏 FPS，源位置 160 应偏移 48，实际 60；字幕/源标记定位错误 | 时间域契约未闭合；M/S | K11 |
| R12 | P2 | Undo 后执行原位 Move 等 no-op，仍写历史并清空 Redo | APP-01 部分完成；M | K12 |
| R13 | P1 | 真正 Roll seam 拖动后 Esc/失焦再释放仍提交；可见窗口 cut 149→188 | 普通取消已修，Roll 残留；G/Q | RI-01 |
| R14 | P1 | linked 左/右修剪、Slide 取消只恢复 primary，伙伴/邻片保留错误源区间和几何 | 取消覆盖不完整；Q | RI-02 |
| R15 | P1 | Properties Speed 仍旧公式：120src/30fps→24fps 的 2x 得 V60 而非48，linked A96@1x | 菜单已改善，属性入口未统一；Q/M | RI-04 |
| R16 | P2 | 多选标题是2片，但 X/Opacity 只改首片；Transform Reset 两条历史，一次 Undo 只还一片 | 多选上下文部分修复；G/Q | RI-05 |
| R17 | P2 | 首次加载 disabled clip 被投影为 enabled；菜单写 Disable，点击反而启用 | 菜单字段修复未补加载投影；Q | RI-06 |
| R18 | P2 | 2x 修剪拖中 preview 按1x源域，释放时 source190→180 跳变 | 提交已修，预览未修；Q | RI-07 |
| R19 | P2 | 缩放锚定重复漂移：ppf0.1 五次累计 -22.73 帧，非边界 scrollbar clamp | 新精度实现不足；Q | RI-08 |
| R20 | P1 | 改 FPS 没重建 subtitle_tracks，cue/word24–48 在24→48后仍24–48 | FE-B01 部分修复；M/S | F-R01 |
| R21 | P1 | FPS 缩放 key.frame 却不缩绝对 Bezier handle.x；同秒曲线值 .03399→.09686 | FE-B01 部分修复；M | F-R02 |
| R22 | P1 | Source 覆盖已有 A/V 抛异常；内部 Insert 退化新素材无音频且旧链接不互逆；Append 不同轨尾错位；混合 FPS 时长错误 | 新 A/V 分支回归；G/M/S | F-R03 |
| R23 | P1 | 多机位切不同 FPS 角度仍沿用旧源时基；网格与 Program 映射分裂；重同步未迁移既有片段 | 部分修复，重同步范围已声明；M/S | F-R04 |
| R24 | P1 | Nest 只复制选中有片段音轨，output/send Bus 引用仍指父轨 ID，子路由不自包含 | FE-B08 部分修复；M | F-R05 |
| R25 | P1 | 媒体池 ProjectCommandStack execute/undo/redo 不推进 project revision，换源后仍7 | helper 已修，提交边界漏接；M/S | F-R06 |
| R26 | P1 | 切工程后旧 proxy token 仍有效；resolution 第二阶段回调不校验项目/任务代次，可写新工程/缓存 | 单 asset 注销已修，全生命周期未修；M/S | F-R07 |
| R27 | P1 | True Peak 文件尾漏峰、首块结果随分块大小变化；响度扫描无取消接口 | 新估算器边界回归 M；取消缺口 S | F-R08 |
| R28 | P2 | Clip extras 已保留，Track/Sequence extras 仍丢；兼容 JSON project revision7→0 | typed 保留策略部分完成；M | F-R09 |
| R29 | P1 | CPU GradingMath 结果依赖同批其他像素：加入无关黑像素，同一 HDR 像素红255→36 | 既存问题，本轮新发现；M | RENDER-03 |
| R30 | P1 | CPU 独立通道 LUT 不能表达跨 RGB 暗部保护；5427/25344 元素不同，最大4 code | 既存问题，本轮确认；T/M | RENDER-01 |
| R31 | P1 | 导出 GL/Metal primary grade 落后 Color 保护/HDR 契约；GPU u8差179、u16差45936 | 既存问题，本轮确认；T/M/S | RENDER-02 |

### 3.1 核心模型与命令：逐项实现及验收

本节文件简称相对项目根；详细报告给出对应绝对路径和完整行号。`commands.py`、`timeline_ops.py`、`timeline_v2.py` 均位于 `src/features/edit/core/`。

| ID | 原因与定位 | 具体实施方式 | 关闭条件 |
|---|---|---|---|
| R01 | `timeline_ops.py:722` 新 merged helper 漏旧 helper 的 is_locked；`commands.py:3057` UpdateClipParam 无 guard；其他写命令保护不完整 | 建统一 affected-tracks 预检，覆盖主轨、伙伴、旧链接伙伴、sync follower、批量参数。明确锁 sync 轨跳过或整组拒绝政策。UI 共用 allowed/reason，领域层仍防绕过。Reset 预检后一个事务；Undo 不因后来锁轨失效 | Move/Trim/Delete/Insert/Overwrite/Ripple/Slip/Roll/Slide/参数/效果/API 全入口锁组合无被锁内容变化；拒绝不新增历史/revision；G 属性不能写锁轨；锁音轨100保持100 |
| R02 | `commands.py:1794` 右 trim 要求 new_timeline_start 与 source_start 同时有值，factory `:1971` 只传后者；undo `:1872` 漏源入点 | 右缘明确保持原 timeline_start，独立应用新 source_start；采用完整 before/after clip，不逆推 source 区间 | reverse source[100,200) 右缩90得到[110,200)，保留帧不变；factory/UI/API 与 Undo/Redo 一致 |
| R03 | `commands.py:12815` ripple offset 仍 source_duration/speed，领域 helper 已算 FPS 时长 | placement plan 生成一次真实 timeline_duration，目标轨和各 sync 轨只使用同一 delta；Undo/Redo 重用同一计划 | 30→24 followers124/124；分数 FPS、0.5/1.25/2x、最短单帧各轨一致 |
| R04 | `timeline_v2.py:2573/:2603` 每更新一片立即重锚，暂时 gap 被当删除；Roll/Macro 顺序写入 | 先生成全体 clip 最终几何，在最终快照重锚一次；保存 clips、transitions 及参数完整前后快照；Macro 延迟关联清理与最终校验 | Roll±、Slide、整组 Move 最终合法转场 ID/长度不丢；三循环 Undo/Redo、JSON 往返完整相等 |
| R05 | `commands.py:1778/:1799` raw replace 绕 fade clamp；`timeline_v2.py:694` Speed 中间实例先非法；trim Undo 漏 fade/转场 | 所有缩短路径统一 slice/fade envelope 策略，直接构造合法最终实例；before/after 包含 envelope、转场、源窗口；禁止在 undo 从钳制后的状态推回 | fade40 缩片到20/Speed5合法不崩；Undo 回40、转场长度回40；Redo 不累计钳制 |
| R06 | `timeline_v2.py:406` curve 分支绕 FPS mapper；`timeline_ops.py:905` duration 积分只取 constant segments | 单一 time-map 处理 rational FPS、step/smooth 积分、正反映射；曲线分段保持精确值，仅最终帧边界舍入；duration 从同一积分求解 | smooth fixture102帧；mixedFPS 曲线覆盖正确源尾；preview/export 与 model 映射一致 |
| R07 | `commands.py:2323` 整数 slice 丢采样 phase/曲线域；Insert/Overwrite/Roll/左trim仍旧公式 | 建统一 slice planner：保留原 time-map window、分数 phase、曲线控制域、方向、源界；全编辑入口委托。必要 schema 字段带版本迁移，避免各入口复制公式 | 当前24矩阵全部逐帧保留；新增曲线点前/后/段内与重复采样切点；只验证 duration 不算关闭 |
| R08 | `timeline_ops.py:1620` 目标 follower 仅 with_timeline_start；新 sync 修复没覆盖本轨动画/marker | 对移动集合共同 transform clip、effect keys、scoped markers、transition；slice 同时改 owner ID；使用共享局部值保持不变量 | Insert/RippleDelete/Batch/RippleTrim/Slide 后局部曲线相同，marker合法；validator空，Undo与保存重载一致 |
| R09 | `commands.py:632/:11105/:11746/:11388/:2583` 新 transition/clip/marker snapshots 漏 to_dict/from_dict | 序列化全部新增 before/after 字段，区分 None 与空；深拷贝嵌套 payload；旧载荷显式迁移/fallback，不静默退回错误逆操作 | live 与真实 JSON 往返后的 apply/undo/redo payload 相同；empty track、嵌套 Macro、marker/link/curve/transition均覆盖。此项只指公开命令载荷，不声称工程保存完整 Undo 栈 |
| R10 | `commands.py:2467` seam marker 用 <= 归左 | 改为 frame<split 归左、seam归右；所有 slice 使用半开区间政策，保存原归属 | seam-1/seam/seam+1、连续 split/Undo 均合法 owner |
| R11 | `timeline_v2.py:464` inverse 只除 speed，调用字幕 placement、Source marker、preview | 正反共用 time-map；明确定义多对一采样的逆定位策略，处理 reverse/ramp/FPS/PTS | source160→offset48；字幕/日志/marker实际秒误差≤1目标帧，正反符合声明策略 |
| R12 | `commands.py:4129` execute 无变化仍 push/clearredo | command stack 先获得有效 proposal 并判 no-op，再改变历史；失败/拒绝保持栈；内容变化 Macro 一次提交 | 原位 Move、同状态 Toggle、空 Macro、原边界 trim 不清 Redo、不增历史/revision；正常编辑仍正确 |

### 3.2 交互：逐项实现及验收

| ID | 原因与定位 | 具体实施方式 | 关闭条件 |
|---|---|---|---|
| R13 | `ui/timeline_view.py:8932` Roll seam 未 begin session；`:9880` cancel 没处理 RollHandle；`timeline_items.py:9625` release直接提交delta | RollHandle 成为 session owner，登记双片与 token；cancel 原子恢复两片投影、handle original_cut/current_delta/grab、monitor/snap/preview，release 校验活 token | Esc/工具/失焦/拖回0/单击无提交，timeline payload/历史/revision不变；两片几何/源/fade一致、state IDLE，下一次 Roll 正常 |
| R14 | view`:8283/:8469` 改 linked Item，`:9198/:9211`改 Slide 邻片；cancel`:9919`只恢复 primary | press 时记录全部 affected objects 完整 preview 或统一从 immutable model 重投影；preview 经同一 session，恢复源区间/几何/fade/cache/renderer状态 | linked左/右、Slide有gap/fade时三种取消全部干净；之后操作伙伴不会把残留当original提交 |
| R15 | `commands.py:3076` Properties Speed 第二套 source/speed 公式；`edit_properties_panel.py:1668`路由此命令 | Speed 属性委托统一 retime planner/ChangeClipSpeedCommand；明确 reverse/pitch/interpolation 参数各自是否改变时长，联动与锁策略共用 | Properties 与菜单同输入 payload 一致；120src30→24的2x V/A均48，一步Undo；反放/曲线/锁伙伴一致 |
| R16 | Properties`:2063/:1573`取 primary；Reset`:1720`只部分字段发全部IDs；controller`:3872`逐条提交 | SelectionContext保存 stable IDs/兼容scope；mixed 控件显示混合状态，编辑单字段对各片原值派生；批量修改/Reset一个 Macro、预检后原子提交 | G两片输入X55应两片55；Transform/Crop/Opacity/Speed混合值正确；一次Undo恢复各自值，锁/异类scope明确 |
| R17 | view`:1847/:6409` full/incremental创建没传clip.is_enabled；`:12718`混用track effective状态 | 首次加载/增量/重载统一完整状态投影；clip own enable与track mute/visibility分开；菜单读canonical clip，绘制读effective | disabled重载显示Enable；启用一次模型变true且文案变Disable；Mute/Undo不污染clip状态 |
| R18 | view`:8285/:8461` preview用timeline delta直接改source，release已经用helper | preview与commit共用同一 source slice proposal，各linked片按自身FPS/方向；统一上下界和snap政策 | 2x/0.5x/reverse/mixedFPS/ramp逐步preview源区间等最终proposal；release不跳、cancel全恢复 |
| R19 | view`:2860/:4766/:2876` anchor与scroll重复截断 | 连续 double anchor_frame；round补偿并保留小数残差；低ppf支持subpixel origin/transform，不只integer scrollbar；wheel/工具/slider统一 | 0.1/0.2/0.5/1/3/10ppf正反20次无方向漂移；连续帧测误差，明确像素/帧容差，不暗中放宽原门禁 |

### 3.3 功能、工程和保真：逐项实现及验收

| ID | 原因与定位 | 具体实施方式 | 关闭条件 |
|---|---|---|---|
| R20 | `src/app/controllers/edit_controller.py:762` rebuild遗漏subtitle_tracks；word_timings未转换 | 时间基变换遍历 V/A/字幕三组，cue/word/caption动画按字段域转换；源窗口不改，保存metadata未知字段；经U01事务发布 | 24↔48/分数FPS的cue、word、graphics同秒；误差≤1目标帧；一步Undo全payload恢复 |
| R21 | `src/shared/keyframe.py:142`仅frame缩放；handle.x为绝对frame_delta | 绝对Bezier handle.x按倍率缩放，normalized旧格式不变；统一clip/effect/track/master；降采样重复key制定形状/合并政策 | 同秒采样曲线值保持；broken/smooth/auto、两种handle格式、多次FPS往返不失真 |
| R22 | `ui/timeline_view.py:16075/:16112/:16226/:16307`：旧时长、Insert video-only fallback、Overwrite音频Add、Append独立轨尾 | SourceEditService生成统一stream patch：一个record位置和实际时长；Insert同步slice+ripple，Overwrite同步覆盖V/A，Append共同落点；锁/碰撞/源界预检，一次提交；不把ValueError吞成无音频。重建互逆links并最终validate | 空轨/占用AV/音床/错轨尾/纯音频/无A轨/锁A/mixedFPS全矩阵；无未捕获槽异常，V/A源窗/落点一致；Undo完整。G内部插入当前新音频0个；右半旧link在重载被清除，这两项必须一起关闭 |
| R23 | `commands.py:2858/:2686`切角沿用旧FPS；`multicam.py:275`网格直接加帧；controller建组取raw帧min | group公共rational ticks/秒；每角保存FPS/offset域；公共位置映射目标source，更新metadata；grid/program共用mapper；重同步事务保持公共shot时间或固化版本 | 24/48混合角、speed/reverse、换reference、A→B→A公共时间一致；目标handles明确；组/片段/Undo恢复，网格与Program同帧 |
| R24 | `nested_sequence.py:382/:404`复制旧busID且没收集空Bus | 收集output/send依赖闭包含多级Bus及FX/automation/mute/solo；child track-ID映射重写routing；明确parent/child gain/master责任，避免重复gain | 独立child、不同父、多实例、两级Bus+send、保存重载无悬空路由，PCM等价。不把原父仍有Bus的全部播放误称静音 |
| R25 | `commands.py:6755`Project stack及`file_controller.py:1600`边界不bump | 统一project commit adapter覆盖媒体池、timeline、结构事务和AI；一次发布更新refs/cache/dirty/revision；Undo/Redo单调；衍生proxy状态单独政策 | rename/import/delete/relink/replace/Undo/Redo均使旧AI plan冲突；macro一次+1、拒绝/no-op不增，重载保留 |
| R26 | media pool`:5271/:5417`切工程未注销proxy；`:10634`二阶段仅proxy_path匹配 | job record携project/session generation、asset ID、source fingerprint、token；completion/error/resolution全链验证；switch/delete/replace/Undo取消旧任务并清pending；不误删新任务同路径产物 | 跨工程复用ID/path、同路径内容替换、delete→undo、新旧任务乱序的旧回调零model/service write |
| R27 | `loudness_analyzer.py:63/:88/:192`首块padding随chunk，尾guard不flush；scan无cancel | 固定stream start/end padding与delay，finalize drain尾；任意分区同结果；注明估算倍率/方法；progress/cancel贯穿前后两次分析和报告发布 | 尾[0,1,1,0]真实估计峰约1.2379不能报0dBTP；DC/头中尾脉冲whole/任意chunk一致；长PCM中取消有可测延迟、无结果发布 |
| R28 | timeline模型Track/Sequence无extras；`serialization.py:251/:544`兼容JSON漏revision | 版本化extension策略覆盖typed层、copy-on-write和rebuild保留extras；兼容JSON补revision迁移；未知类型安全诊断，不执行未知payload | future_sequence/track/clip在load→edit→undo→save/reload均保留；JSONrevision不归0。主要ZIP已有revision，不作普遍保存损坏结论 |
| R29 | `the_decompiler.py:193`np.any(dark_mask)外层批判定，deep_mask没与逐pixel dark_mask交集 | deep gate逐像素相交，stage顺序与ColorGL/MSL一致；保留HDR负值/>1，不靠clamp掩盖；先冻结正确参考再改其他后端 | 同像素单独/合批/拆batch/tile/顺序/加无关像素结果相同；dark/nondark/deep/HDR逐stage跨后端一致 |
| R30 | `timeline_export.py:9337/:9375`把已跨RGB保护当可分离1D LUT | 先关闭不能证明可分离的grade快路；或仅用LUT做可分离前缀，真实RGB保护另stage；审查saturation组合缓存契约 | 原exactness不放宽；合法wheel near-black网格fast/float一致；u8/u16/alpha/cache开关有明确误差预算 |
| R31 | GPU exporter`:190/:228`、Metal`:120/:155`缺暗部保护/HDR hue语义不同 | primary grade共享权威IR或生成GLSL/MSL片段，统一保护/HDR hue/gray顺序及版本；在既有worker/GPU队列执行。先完成R29 reference | CPU/ColorGL/ColorMSL/exportGL/exportMetal比较，含合法边界wheel、HDR负/>1、hue+saturation、u8/u16、多层；不能仅GL≈Metal。旧超范围fixture制定迁移政策，不能删测试掩盖合法参数差异 |

## 4. 四项明确未实施的工程工作

这四项在旧计划实施状态中已经声明未完成，本次没有把它们改称新增回归。其中已有可复现副作用，需要与 P1 缺陷同批处理。

| ID / 优先级 | 当前缺口 | 具体实现步骤 | 验收 |
|---|---|---|---|
| U01 / P1 | Timeline 设置直接写 cache/ref，无设置 Undo；controller`:828` | ①构建 UpdateTimelineSettings project transaction，before/after涵盖设置、三个轨组、keys/Bezier/word/markers/refs；②复用R06/R20/R21时间基transform；③统一commit更新audio/render/cache/UI；④整体历史，不与旧时基命令错序 | FPS改动一步Undo恢复全部时间域；旧编辑Undo不会混合timebase；dirty/revision单调、旧AI plan失效 |
| U02 / P1 | Multicam建组先stamp资产再锁检查；同步metadata直写，无Undo/dirty/generation | ①先preflight轨锁、角度时基和源界；②Create/UpdateMulticam命令一次提交assets/group/clips；③sync worker带project/group revision/token；④重同步迁移政策与R23统一 | 无可用轨/锁轨失败零stamp；建组/同步一步Undo完整；切工程旧worker不落新工程，revision推进 |
| U03 / P1 | Nest创建直写assets/ref/childcache/parent，Undo及revision缺失 | ①规划child及依赖闭包；②跨project+父/子seq transaction，保存稳定IDs、refcount、active导航；③一次commit；④Undo移除无引用child或保留有其他实例的child，Redo复用IDs | 三层Nest、多实例、创建后编辑再Undo/Redo无孤儿；refs/cache/asset一致，父子revision单调 |
| U04 / P1 | Replace Media未probe，video→jpg仍video/旧fps | ①后台probe类型/FPS/PTS/时长/流/色彩；②usage index检查全部source windows与reverse/ramp；③生成兼容、裁窗或拒绝处置；④token/fingerprint校验后ReplaceMediaCommand原子提交并invalidate旧decode/proxy；⑤完整asset Undo | 短源/跨类型/同路径换内容metadata正确；不得悄悄越界；Undo原asset全恢复，旧回调无写入 |

## 5. 当前仍需补齐的16项工作流

已有基础能力均保留；本表只描述剩余闭环。F15/F16是时间线效率缺口。F12/F13等规模较大的能力依据当前注册表、入口和旧计划未实施声明排期，未伪装成全部外部实机已测。

| ID / 优先级 | 当前剩余功能 | 具体实现步骤与依赖 | 验收条件 |
|---|---|---|---|
| F01 / P1 | Source高级编辑：完整stream patch、单端/全源默认、3/4点、Replace Edit、Match Frame | 先修R22并统一SourceEditService；分别解析source/record I/O，3点求缺端、4点明确fit或保调策略；Replace保留目标时长/FX并计算源窗；Match Frame通过R11定位正确asset/stream/source帧；操作使用command而非UI直接改model | V/A/纯音频、焦点切换、单端range、不同FPS/速度/锁轨、Source↔Program往返；每项一步Undo且无源越界 |
| F02 / P2 | Trim Monitor真实双/四画面，普通/ripple/slip/slide比较 | 现有TrimMonitorDialog只有TC QLabel/Δ/headroom，不是双画面。加入原/新in/out帧槽；后台decode由session generation协调，低清代理和缓存去重；全部tool共享preview proposal，cancel收尾；数值trim保留 | 两侧画面与源mapper一致、拖动反馈连续、取消无晚帧；普通/roll/ripple/slip/slide及reverse/ramp均显示正确 |
| F03 / P2 | Edit关键帧Dope Sheet/Curve完整UI | 复用shared keyframe model/commands；Properties入口绑定clip/effect context，dock实现多点框选、frame/value、hold/linear/ease/Bezier手柄、复制粘贴；一次拖动一个command；明确absolute timeline域/局部显示转换，依赖R08/R21 | 多片段/效果切换、mixed值、曲线手柄、Undo/Redo、FPS变更与preview/export一致；锁轨无法编辑 |
| F04 / P1 | 实时preserve_pitch与离线保调一致 | 现实时是变调resample、离线已有WSOLA。建立streaming WSOLA缓冲、hop/latency和seek reset；接公共time-map/audio graph、rate cache和pitch policy；算法工作在线程，不阻塞UI；不支持范围明确fallback | 0.5/1.25/2x语音与tone音高对照，seek/loop/ramp不卡断；preview/export差异和延迟有测量 |
| F05 / P3 | 可选optical_flow高质插帧 | 模型字段已有、UI隐藏且后端未完整。注册可选backend+依赖探测；相邻帧flow/interpolation、遮挡/scene cut回退、缓存预算、取消；能力不足隐藏或明确降级，交付复用同算法 | 硬切不跨场景合成、遮挡无明显拖影；preview/export一致；backend缺失与显存不足可恢复 |
| F06 / P1–P2 | LUFS/峰值Normalize与Auto Duck完整操作 | 先修R27；分析worker基于asset/window/revision缓存，Normalize提出目标gain并一个command提交；Auto Duck注册sidechain FX与routing/UI，参数attack/release/threshold/depth，经audio graph调用现有DSP；分析取消不提交 | 标准PCM目标LUFS/峰值与gain正确；对话期间音乐按设置下降；Preview/Export/Stem一致、Undo完整 |
| F07 / P1–P2 | Nest成熟化：retime音频、子转场、导航与Unnest | 先U03/R24；递归time-map把子audio windows/automation/transitions映射父，支持reverse/ramp/pitch，多实例独立cache；breadcrumb保存父返回点；Unnest按引用与时间窗展开并预检冲突，不静默丢字幕 | 三层、多实例、倍速/反放、Bus/子audio transitions，帧和PCM一致；Nest/Unnest一步Undo无孤儿 |
| F08 / P1–P2 | Multicam动态网格、TC/marker同步、音频切角和重同步管理 | 先U02/R23；统一group时基；增加TC/marker对齐策略；proxy动态网格按可见角度调度限帧decode；audio-follow-video/固定audio策略明确；group管理更新既有shots或固定sync版本 | 混合FPS长片角度切换不中断，网格/program对齐；TC/marker/波形同步有偏差报告；音频策略和Undo一致 |
| F09 / P2 | 字幕表格可编辑Text/Speaker/Status/时间、词级split、批译 | 当前表NoEditTriggers、split中点。添加delegate校验并走caption commands；按playhead/word边界split/merge；批量样式与翻译worker携revision，结果以可审查batch提交；源字词证据和graphics绑定保留 | 表格修改可Undo，错误时间/锁cue拒绝；词边界不丢字；切工程/旧翻译回调拒绝；先关闭R20 |
| F10 / P2 | 代理集中队列、可见进度、cancel/retry、容量和迁移 | 先R26/U04；job manager保存worker、token、状态与progress；queue限并发、cancel安全收尾、retry新代次；缓存按source fingerprint和policy索引，迁移原子更新且不删使用中资源 | 批量取消/重试/磁盘满/切工程/同路径竞态；状态与真实产物一致，旧任务无回写 |
| F11 / P2 | 多版本备份、历史预览、恢复副本和损坏恢复 | 已有固定bak/autosave。新增版本manifest含timestamp/schema/checksum；异步immutable snapshot、临时写+原子rename；retention按时间/数量/容量；历史预览和恢复副本不覆盖原工程；校验损坏模块给诊断 | 中断写入/磁盘满/损坏最新版本可恢复前一版本；未知module保留；恢复副本资产引用有效 |
| F12 / P2–P3 | 交换保真矩阵与降级报告；可选AAF/EDL | 已有FCPXML/OTIO受控子集。以公共time-map列各格式clip/retime/transitions/FX/routing支持矩阵；unsupported输出结构化报告与可选render替代；AAF/EDL独立optional adapter，缺依赖不影响已有功能 | 外部软件实际roundtrip比较源窗/帧/PCM；复杂FX明确降级、不静默丢字段；导入先预览再原子提交 |
| F13 / P2 | 人工逐词文本剪辑：选词剪、lift/extract、停顿编辑 | 已有ASR/transcript/AI口播服务。新增text selection→word evidence→Source window映射；Lift保留gap、Extract共享ripple planner、停顿阈值可预览；linked/sync/markers/caption一起事务；低置信度边界可修正 | 选词对应正确源音画，撤销恢复全部对象；跨cue/锁轨/FPS/ramp不误剪；文本与字幕证据可追踪 |
| F14 / P2 | 能力/帮助/AI声明与实际入口、后端统一 | Stem AVAILABLE已修。将capability绑定action/backend/optional dependency及范围，不只notes字符串；生成帮助与AI manifest；对菜单、快捷键、service做契约测试；局部降级不让整个Edit失效 | AVAILABLE有实际入口与后端；PARTIAL能说清范围；缺依赖时UI/帮助/AI一致，True Peak算法说明与R27结果同步 |
| F15 / P2 | 拖动边缘自动滚屏/卷轨 | 当前播放follow不等于drag autoscroll。InteractionSession指针进左右24–48px启动约16ms timer，按距离加速限速；垂直对应V/A/字幕区域bar；固定scene offset重算preview/snap；release/cancel/drop/close停timer | 长时间线多选/linked/Alt-copy可以拖到原不可见位置；取消即停、不换错轨、不额外提交 |
| F16 / P2 | A/V片段逐帧Nudge与精确Move | Alt箭头现仅字幕nudge，A/V箭头移playhead。注册可改快捷键±1/±10 actions与timecode/relative frame Move对话框；领域plan取全组合法delta，预检锁/碰撞/边界，一次Macro保持相对关系 | 低zoom下AV多轨组精确1帧移动；碰撞/0边界明确反馈；一步Undo、caption绑定/keys/marker同步 |

## 6. 测试维护任务 T01（P2，不计入31组生产问题）

| 子项 | 本轮失败原因 | 应实施的测试改进 |
|---|---|---|
| T01-a，3例 | `test_ef01_aud_part2.py::TestAud16506PrepositionStartFailureCleanup` 的 SimpleNamespace 缺 `no_deblock`，在目标清理行为前就失败 | fixture补真实构造所需配置，覆盖preposition启动失败的线程/decoder清理；不能把fixture AttributeError写成已测生产泄漏 |
| T01-b，1例 | `test_track_sync_lock.py:216`要求孤立header立即自改状态；生产早已emit后等command成功回写 | 孤立header断言请求signal且自身不先改；加真实TimelineView+command+Undo集成。当前集成True→False→UndoTrue已通过 |
| T01-c，1例 | Metal blur像素≤2已过，旧断言要求`stack_effect_horizontal`，当前物化链用`stack_effect_chain_horizontal` | 不强迫生产建旧key；验证两layer/两frame实际scratch对象复用/分配计数、扩容及release，同时保留数值门禁 |

GPU旧`GRADE_TYPICAL`含超UI范围wheel参数，名称/fixture需整理；但合法边界wheel同样能复现差异，因此不能把R31降为单纯测试漂移。禁止放宽LUT exactness或只比GL↔Metal来“修绿”测试。

## 7. 实施顺序与统一架构约束

| 批次 | 包含条目 | 先做什么 / 本批产物 | 完成门禁 |
|---|---|---|---|
| A：防错误提交 | R01/R02/R05/R13/R14/R17/R22 | 固化失败fixture；统一锁preflight、取消所有affected对象、源A/V完整patch、完整clip快照 | 当前可见失败操作重跑；取消零model变化；源覆盖无异常；锁轨无写入 |
| B：时间域与slice | R03/R06/R07/R08/R10/R11/R15/R18/R20/R21/U01 | 建唯一time-map/slice/transform服务；明确source帧、sequence帧、秒、PTS、phase、smooth语义；全入口委托，版本迁移一次完成 | 逐帧24矩阵+曲线/Bezier/word保真；混合FPS V/A一致；设置Undo正确 |
| C：原子历史与结构 | R04/R09/R12/R16/R23/R24/R25/U02/U03 | transaction保存全部before/after、最终态validate一次；公共commit adapter覆盖project/sequence；真实多选批量写入 | 转场/动画/link/marker/Bus全payload Undo；no-op不清Redo；project revision单调 |
| D：异步和持久化 | R26/R28/U04/F10/F11 | 全链generation+fingerprint；probe兼容预检；typed extension统一与atomic IO | 旧callback零写入，跨类型换源正确，future字段保留，故障恢复 |
| E：分析与交付保真 | R27/R29/R30/R31/T01 | 先正确逐像素reference，再共享shader和LUT安全门；truepeak start/end/finalize/cancel；更新过时测试 | 真峰任意chunk一致；CPU/Color/GL/Metal合法参数golden；不放宽实质保真断言 |
| F：效率与高级功能 | R19/F01–F09/F12–F16 | 以上基础闭环后逐工作流接UI；优先F15/F16/F02/F03和实际用户高频入口 | 对每工作流做真实素材操作、Undo、保存/重载、preview/export；可选能力明确降级 |

关键约束：遵循 `.clinerules` 的 immutable model、command/service/facade 边界、信号防递归和GPU队列；不在UI新增直接GPU计算。事务先算全体最终态，再校验/发布/更新历史与revision，不能让顺序apply的中间态触发不可逆清理。拒绝、取消、失败和no-op无内容与历史副作用；Undo恢复内容，但发布revision保持单调。预览只展示proposal，不把Item残留反写model。

每批先将本轮失败输入变为有效回归fixture，按相关范围测试，随后可见窗口复跑。全批通过前不要仅因107个原修复测试绿色就关闭对应旧条目。上轮状态可按本表逐项关联更新，但本轮保留原文，不覆盖历史证据。

## 8. 未完成验收门禁与不能下结论的记录

| Gate | 当前状态 | 后续应执行 |
|---|---|---|
| G01：Source GPU共享纹理 | CPU Source有效像素/播放已通过；shared texture尚未验收 | 真实GPU decoder首帧、切source、close/reopen、失败降级，测实际像素及线程/context约束 |
| G02：普通Trim可见命中 | 本轮多次可见right-edge尝试没有进入drag mode；offscreen真实trim可进入且发现R18。当前不足以断定新的普通Trim生产缺陷 | 在干净session记录grabber、hit routing、Tool/focus和press handler；人工鼠标复核。不能把无提交当成功，也不能把QTest命中不确定性算确认bug |
| G03：操作系统输入 | Q失焦是FocusOut事件，Alt为受控modifier | 真实窗口切换、持续Alt、触控板pixel/angle、不同DPI；按R13/R19条件验收 |
| G04：性能与声音 | 短播放/短导出已过，无耳听/长时压力测量 | 4K/长GOP/HDR/VFR长片，多轨+FX+proxy，连续seek/play/loop，实际音频设备A/V漂移、内存/队列和cancel延迟 |
| G05：外部/可选能力 | 模型与注册表核查，不等于外部软件/AI/录音设备实测 | FCPXML/OTIO目标软件、可选provider/录音设备/硬件编码、脱网与依赖缺失；记录支持与降级矩阵 |
| G06：跨时间线Inspector与布局 | 截图中切新时间线后Properties仍显示先前值；未完成目标ID/提交scope复核，不计确认缺陷 | 检查选区清空、旧clip上下文、控件是否应禁用；小屏/初始dock布局和timeline hit测试一起核查 |
| G07：正常退出 | 专用审计窗口close停在QMessageBox确认，单次自动响应未完成全部弹窗；采样未证明线程死锁，之后仅终止审计专用进程 | 重复响应各确认，验证正常保存/丢弃/取消的close路径和资源收尾；本轮不写正常退出已通过 |

原始记录保留了早期 Program 标签仍激活的“Source”截图、一次已变回单选的属性输入，以及无效普通trim尝试；这些没有被当作 Source 有效像素、多选失败或普通trim成功证据。结论使用后续明确Source可见截图09/10、两ID上下文记录15、锁轨记录16、Source操作17–19与Program播放20。

本计划关闭标准是每项的失败复现消失、相关有效回归测试通过、受影响真实工作流可见操作通过，并记录验证版本和证据。未完成门禁保持待验收，不能改写为“全面通过”。
