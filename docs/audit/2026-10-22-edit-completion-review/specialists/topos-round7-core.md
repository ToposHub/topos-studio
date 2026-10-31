# 第七轮复核：核心编辑、属性动画、字幕

基线 HEAD `d11615bb85d057f92735dbfcbfdc0debb127fc13`，审查 `e1b9e49b8..HEAD`。遵循 `.clinerules`，只执行诊断，未修改产品代码和既有测试。

## 已执行与通过

命令：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/edit/test_fix_round6_phase1c_commands.py tests/edit/test_fix_round6_phase2.py tests/edit/test_edit_properties_panel_layout.py tests/edit/test_interchange.py -q`。

结果 **99 passed in 4.24s**；日志 `/tmp/topos-round7-core-pytest.log`。这是 focused pytest，不是完整应用窗口验收。

通过点：C02新/已有轨主命令动画平移及Undo；C03单边迁移、双边移除并Undo恢复；C04链接音频转场快照80帧恢复；C05 marker与尾转场迁移、Undo；C06新增9命令锁轨拒绝；D08带Format/无Format的标准ASS逗号及SSA往返；D09显式tickRate、显式frameRateMultiplier、嵌套begin、dur、拒绝smpte；C01新建参数负值及180度插值/保存重读；U05信号和控制器定位；G16六个◆注册；Q03能力表与测试同步。U07十语言改动已静态核对，没有发现本次文案错误。

## 确证未闭环项

所有诊断可由 `/tmp/topos-round7-core-probe.py` 重放，结果 `/tmp/topos-round7-core-probe.json`，stderr仅有Qt locale/font告警。Probe前一版把TimelineSequence位置参数顺序写错，已改为具名；该脚本错误不计产品问题。

### K01 / P1：G16只增加◆，改属性值仍写静态值，无法完成两个不同值的普通Transform动画

生产模型＋真实独立Qt属性面板验证（offscreen，非完整MainWindow）：X=0先在frame0加关键帧，frame50把X静态改100后，模型静态100、渲染求值0；再点◆，面板发出的新关键帧值仍0。即常见“第一帧打点→移动播放头→改X→再打点”不能生成0→100动画。现有参数编辑handler始终创建UpdateClipParamCommand，命令只替换TransformParams/opacity，不改animated_params；◆在发射时又强行采样旧动画覆盖控件编辑值。

源码：`src/app/controllers/edit_controller.py:2467`，`src/features/edit/core/commands.py:3464`（Transform替换），`src/features/edit/ui/edit_properties_panel.py:1975`（◆重新采样旧动画）；实际渲染读动画位于 `src/features/edit/core/timeline_render_plan.py:910`。

验收：真实窗口从0→50帧分别输入0/100，两个关键帧值为0/100、中点50，当前关键帧编辑/非关键帧自动插点策略明确，Undo/Redo/保存重开、预览/导出一致；Opacity同验。旧G16只能记“◆入口已加，完整编辑工作流待补”。

### K02 / P2：播放头移动时Inspector数值不随动画求值，和◆及实际画面不一致

独立真实QtPanel持有模型0帧X=0、50帧X=100，set_current_frame(50)后◆缓存100、模型采样100，但X框仍0。set_current_frame只更新按钮，不更新数值；构建/刷新仍来自静态TransformParams。

源码：`src/features/edit/ui/edit_properties_panel.py:2391`、`:3272`、`:2533`。与K01相关但修复点不同，可归同一个动画完整性工作包。

验收：拖播放头或播放经过关键帧，Transform/Opacity及手柄和实际画面同步显示求值；程序刷新不反写模型，不把所有帧变关键帧。

### K03 / P2：C01既有工程仍保留错误动画值域，新加关键帧也继续被0..100截断

用上一版本真实序列模式创建X raw=-250/min0/max100的关键帧，to_dict→from_dict模拟旧工程加载，再由当前AddKeyframeCommand新增frame50=-500。输出raw值[-250,-500]仍保留，但KeyframeTrack范围仍[0,100]，frame0/50求值都0。新命令查询的权威range只在首次创建AnimatableParam时应用，已有param分支直接add_keyframe。

源码：`src/features/edit/core/commands.py:3853`、`src/features/edit/core/timeline_v2.py:1081`、`src/features/edit/core/effect_instance.py:137`。

验收：旧工程负translate/rotation、旧scale值域经明确schema迁移/修复策略恢复；不影响效果参数自己的schema；新/旧工程添加、移除Undo、保存重开均正确。不能只用当前版本新建参数的保存往返证明兼容已修。

### K04 / P2：D09 TTML声明frameRate但省略tickRate时默认时钟错误

TTML `ttp:frameRate="30"`，p begin=30t/end=60t，目标24fps：期望start24/duration24，实际start720/duration720。实现无条件default tick_rate=1，没有按显式effectiveFrameRate×subFrameRate推导。

源码：`src/features/edit/core/captions/importer.py:208`、`:215`、`:246`。规范依据：[W3C TTML1 §6.2.10](https://www.w3.org/TR/ttml1/Overview.html#parameter-attribute-tickRate)，未显式tickRate而有frameRate时，ticks按effectiveFrameRate×subFrameRate解释（2026-10-22核查）。

验收：显式frameRate、frameRateMultiplier、subFrameRate与缺省组合覆盖，源到目标帧率转换正确。

### K05 / P2：TTML失败输入与时间容器范围仍可生成错误长字幕

同一导入器相关未闭环边界，可与K04合并为字幕时间语义工作包，建议保留三条验收向量：

1. `tickRate="0"`被max(...,1e-9)接受，begin10t/end20t产生start=240000000000/duration=240000000000，可能把时间线扩为极大时长；应该明确拒绝无效非正/非有限时基。
2. timeBase=media、frameRate30、begin=`00:00:01:00`/end=`2s`（标准media帧式clock-time），begin不被识别后静默`or0`，实际start0/dur48而不是24/24；两端都frame-clock时只报无timed paragraphs。支持合法语法或明确拒绝，不能把解析失败当0。
3. div begin1s/end3s内p begin1s/end9s，实际start48/dur192延长到10s，未受父容器3s结束裁切；预期可见区间2–3s即start48/dur24。原D09只修父begin加法，没有父end裁切。

源码：`src/features/edit/core/captions/importer.py:227`、`:259`、`:331`、`:351`；官方语法与时序定义：[W3C TTML1 timeExpression](https://www.w3.org/TR/ttml1/Overview.html#timing-value-timeExpression)、[TTML Timing](https://www.w3.org/TR/ttml1/Overview.html#timing)。这些向量部分属于历史未闭环，不声称全部本次新引入。

验收：以上三个向量正确或受控拒绝，invalid token不静默写0，父范围裁切、timeContainer=seq若不支持要明确报错。

### K06 / P2（低用户触达，API/反序列化）：旧跨轨命令Undo默认回0帧

`MoveClipToExistingTrackCommand.create('v1','v2','c',300)`（old_start为可选），片段原frame100，apply到300→undo回0。apply局部old_start从未写入self.old_timeline_start，undo缺失时默认0。本次C02新增测试使用原50但只断言恢复的关键帧50/99，没断言clip位置，因此通过却留下clip0/keyframes50的不一致。静态rg确认当前生产UI没有直接调用旧命令，仍在COMMAND_REGISTRY中支持重放，所以应P2维护/API边界，不夸大成主UI拖动必现。

源码：`src/features/edit/core/commands.py:1450`、`:1525`、`:1578`、`:19073`。

验收：create/constructor/from_dict缺old_start时apply捕获，正/负夹起始、markers、关键帧、转场完整恢复；已有目标轨和重复Undo/Redo正确。

## 本次未计为已确证缺陷

- Freeze后视频左段50帧仍linked到100帧音频，右侧定格段unlink：probe展示模型状态但validate无错；视频冻结保持原音频可能是既定语义，本轮不据此单独报错。
- AddKeyframeCommand对相同帧覆盖再Undo的潜在状态丢失、超范围8192/多圈旋转限制，未作为本轮新缺陷定级；若进一步做API完整性专项可扩展。
- C05反向/变速/混帧率冻结的用户完整流程、所有语言窗口显示未逐项重做，不能把本报告当100%验收。
