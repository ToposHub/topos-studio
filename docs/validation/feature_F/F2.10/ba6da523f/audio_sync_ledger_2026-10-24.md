# F2.10 摄影机视频与独立录音的源级同步 — 证据台账

- 日期：2026-10-24
- 结论：**已完成（源级绑定核心链）**。双源混音（keep_camera_audio=True
  同时出声）、波形法后台 worker 化、时间线波形图取绑定源登记后续片。
- 提交：`ba6da523f`。

## 1. 验收对照（计划 §F2.10）

| 验收点 | 落地 | 证据 |
|---|---|---|
| 外录 WAV 提前开录，预听/插入/修剪/保存重开/成片同一关系 | `resolve_audio_source` 单一解析口径 + 四路消费（播放 source_start 折算 / 成片导出 / Stem / 源监视器预听）；绑定入 project 载荷重开恢复 | test_audio_sync_binding.py::TestExportConsumption（真实 WAV：未绑 300Hz→绑定 700Hz 听感切换）+ ::TestBindingModel |
| 无共同开头音频可用 TC/手动 | TC：metadata["timecode"] 优先 + BWF bext TimeReference 兜底（`wav_timecode_seconds`）；手动：offset 秒直接输入；波形：`compute_audio_binding_alignment`（300s 大搜索窗） | ::TestWavTimecode（3 项）+ 控制器三法入口 |
| 低置信/错 TC/采样率不同不误绑 | 波形法沿用 F2.6 全套拒绝门槛（低置信/反相/歧义/出界/漂移）；TC 缺失/解析失败 → 拒绝并建议他法；采样率不同经**显式重采样归一**（修复 44.1k 按 48k 计数的既有缺陷；归一失败 → sample_rate_mismatch 拒绝）；命令 apply 校验 WAV 存在且为 audio | ::test_uncorrelated_rejected / ::test_cross_sample_rate_aligned_after_resample / ::test_non_audio_or_missing_wav_rejected |
| 取消零写入 | 确认对话框任一步取消 → 不执行命令（零写入）；波形不可靠 → 状态栏原因 + 不提交 | 控制器 `_on_bind_external_audio` 流程 |
| 原视频/WAV 不被破坏性改写 | 全程只写 `project.audio_sync_bindings`（dict 覆盖层） | 模型设计 + 测试 |
| 多声道与 offset 的身份/持久化 | 绑定含 audio_channel_map（复用 SourcePatch select_channels 消费链）+ method/confidence 审计字段；settings 落盘/读回（未知载荷保留） | ::test_persistence_roundtrip_and_unknown_payload |
| 与普通链接 A/V、多机位切角、播放时钟同步区分 | 独立绑定模型（非 MulticamAngle.sync_offset / 非 linked_clip_id / 不碰 AudioClock） | 模型 docstring + 域区分测试 |
| 单次领域事务 | UpdateAudioSyncBindingCommand 项目域单步撤销 | ::test_apply_undo_roundtrip |

## 2. 关键决策

1. **消费切换策略**：keep_camera_audio=False 时四路消费切 WAV；
   =True 时绑定留存但不切（双源混音属混音图域，登记后续片）。
2. **retime/保调片段回退机内声**：ClipRetimeReader/PreservedPitchReader
   的位置数学按视频源窗，注入偏移会错位——绑定在变速片段上显式跳过并
   留警告（诚实降级而非错位出声）。
3. **偏移折算口径**：`bound_audio_frame = source_frame + offset×fps`（解码
   器按 frame/fps 定位秒，WAV 内容在 t+offset 秒处）——播放（source_start
   基偏移）与导出（逐帧折算）同一公式。
4. **采样率归一是顺带修复**：`_decode_mono_segment` 此前不重采样
   （decode_next_samples 无重采样路径），44.1k 源按 48k 计数 → 多机位
   同步 lag 秒数差 ~8.8% 的既有隐患一并修复。
5. **波形法同步执行**（30s×2 段秒级解码）；后台 worker 化登记后续片。

## 3. 测试账本

新增 tests/edit/test_audio_sync_binding.py：14 项（模型/命令/持久化 7 +
真实 WAV 对齐 3[含跨采样率] + TC 3 + 导出听感 1）。护栏：source_patch
消费/multicam sync/guards/bwf/audio_playback/interpretation/phase1b/
round7/stem 91 项绿（含一次 NameError 回归的发现与修复——导出块引用
未导入名被外层吞成静音，护栏测试即刻捕获）。

## 4. B 依赖核对

无 Plan B 依赖（纯 Python DSP + 既有解码器 + Qt UI）。

## 5. 登记的后续片

1. keep_camera_audio=True 的双源混音（混音图域）。
2. 波形法后台 worker（复用 MulticamSyncWorker 模式）+ 分析中途取消。
3. 时间线波形图取绑定源（timeline_items → audio_waveform 路径）。
4. retime/保调片段的绑定支持（需 reader 位置数学接受基偏移）。
5. 长录制漂移矫正（条件依赖 F3.3，与 F2.6 共享）。
