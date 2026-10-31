# 第六轮计划执行进度（2026-10-21 会话）

基线：`439a8cc15`（fix-ui）。本文件记录
[第六轮审计计划表](edit_page_sixth_full_audit_optimization_plan_2026-10-21.md)
在本会话的逐项落地状态，供后续会话接续。

## 已完成（26 项优化事项中的 25 项 + G14/G16）

| 项 | 状态 | 提交 | 说明 |
|---|---|---|---|
| U01 | ✅ | fix-1A | preflight 读 `duration_frames`（缺失→None 不臆造 0） |
| U02 | ✅ | fix-1A | DeliverWorkspace.set_timeline_sequences + JobContext.timeline_sequences 贯通 Runner |
| U03 | ✅ | fix-1A | combo 解析完整目标序列；Ref 不进队列上下文/不冒充 |
| U04 | ✅ | fix-1A | 暂停导航 frame_changed 同步可见播放头（四端同帧） |
| Q02 | ✅ | fix-1A | 夹具改完整 TimelineSequence + 缺 resolver 降级用例 |
| D01 | ✅ | fix-1B | audio_retime 共享模块：canonical 映射 + 样本域重采样（2x→880Hz/reverse 时间反转锁定） |
| D02 | ✅ | fix-1B | AudioDecoder 保留真实声道布局；反相=同相响度；静音拒绝正增益 |
| D03 | ✅ | fix-1B | _NormalizeGainWorker 后台线程 + 取消标志 |
| D04 | ✅ | fix-1B | 动画 FX 逐帧求值（成片/Stem/实时 AudioMixer 三路一致） |
| D05 | ✅ | fix-1B | Stem 补 clip FX/冻结跳过/crossfade；Master stem≡成片（<1e-4） |
| D06 | ✅ | fix-1B | 精确相邻样本边界（成片+Stem）；NTSC 无零洞、480480 精确；修 stem float fps 截断 |
| D07 | ✅ | fix-1B | decoder 清理 try/finally；预解码循环响应取消 |
| C02 | ✅ | fix-1C | 跨轨移动 with_temporal_shift（新建轨/已有轨/旧命令三处 + undo 对称） |
| C03 | ✅ | fix-1C | 单边转场迁移重锚/双边丢弃可恢复；源/目标轨快照 |
| C04 | ✅ | fix-1C | 链接变速快照在 with_clip_updated 之前取 |
| C05 | ✅ | fix-1C | Freeze 复用 Split 事务（源窗口/转场归属/marker 迁移/整轨快照） |
| C06 | ✅ | fix-1C | 9 个关键帧/效果写命令 assert_track_editable |
| D08 | ✅ | fix-1C | ASS 按 Format 列数切分；逗号/标签/换行/SSA/往返锁定 |
| D09 | ✅ | fix-1C | TTML tickRate/frameRate(+multiplier)；smpte/clock 拒绝 |
| U06(关窗日志) | ✅ | fix-1C | 停止判定以线程真实状态为准（旧契约 stop() 返回 None 被误当失败） |
| C01 | ✅ | fix-2A | CLIP_PARAM_RANGES 权威值域；负平移/角度全链路保值 |
| U05 | ✅ | fix-2A | Find 命中 find_hit_selected → seek cue 开始（cue 内不重复） |
| U07 | ✅ | fix-2A | 10 语言"即将推出"→真实功能描述 |
| Q03 | ✅ | fix-2A | interchange 静态表 FCPXML 与 registry(PARTIAL) 对齐 |
| G16 | ✅ | fix-2A | Transform 每轴/Rotation/Opacity ◆ 入口（C01 值域生效） |
| Q01(门禁) | ✅ | fix-3A | faulthandler_timeout=300 + 真实素材关闭/切源压力回归 |
| D10 | ✅ | fix-D10 | enum.value 先于 str 提取；序列 codec 纳入无音频集合 |
| G14 | ✅ | feat-G14 | PNG/TIFF 标准序列编码器（8/16-bit/alpha/取消清理/端到端回读） |

新增回归：tests/edit/test_fix_round6_*.py、tests/deliver/test_fix_round6_*.py
（phase1a/1b/1c/phase2/g14/u04/u06/q01 + D10 追加），共 100+ 用例。

## 剩余

- **U06 切源 GL 1282 / lease 回收**：关窗误报已修；切源期 GL program
  归属需真实窗口 + 九素材循环复现定位（headless 无法复现），验收要求
  "切源无告警、长循环显存稳定"。入手点：Source 切源异步旧任务晚到时
  的 GL program generation/帧释放路径（file_controller SourceViewerDecodeWorker
  已有 generation 拒绝；疑点在 viewer 侧 program 重建窗口）。
- **Q01 根因**：单进程全量卡死（FFmpegSource.__del__→container.close 竞争）
  已有超时+栈输出门禁与压力回归；根因修复需在完整套件环境复现。
- **第二阶段能力建设未开工**：G01(Source Patching)、G02(三/四点)、
  G03(Trim Viewer/JKL)、G04(实时多机位)、G05(TC/marker 同步)、
  G06(实时保调——离线 D01 已修,实时仍变调,UI 已披露 export only)、
  G07(采样级剪辑)、G08(AAF)、G09(FCPXML 往返加固)、G13(混合值批量
  Properties——批量日志字段已有)、G17(嵌套子转场/父子音频重采样)。
  每项均为独立特性建设,按计划表 §6 验收标准逐项开做。
- **第三阶段**：G10(文稿剪辑)/G11(Audition)/G12(协作)/G15(VST3/AU 宿主)
  按计划表"以真实用户场景和性能预算决定投入"。

## 已知非缺陷

- `test_fix_wd11g53_aud` 在大 `-k` 批次中的失败为既有顺序 flake
  （干净树同样出现,单跑通过;属 Q01 类环境顺序风险,非本轮引入）。
