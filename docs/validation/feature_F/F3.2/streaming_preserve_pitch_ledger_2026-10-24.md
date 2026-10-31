# F3.2 · 流式实时保调变速 — 验证台账

- 日期：2026-10-24
- 计划条目：docs/feature_plan_F_davinci_2026-10-22.md §F3.2
- 测试：`tests/edit/test_streaming_wsola.py`（16）+ `tests/edit/test_preserve_pitch_realtime.py`（8）+
  回归 `test_audio_timestretch / test_clip_speed_fade_realtime / test_preserve_pitch_ui /
  test_capability_registry / test_fix_round7_audio_output / test_audio_transitions` = **合计 143 绿**

## 1. 实现选择与声明（计划"选择可流式的伸缩实现，明确延迟/窗口/声道/速度范围和许可证"）

- 算法：WSOLA 流式状态机（`StreamingWsola`），与离线 `wsola_time_stretch` 同参数
  ——25ms 分析帧 / 7.5ms 对齐搜索 / 50% Hann 重叠 / 多声道共用对齐偏移（声像不漂）。
- 延迟：算法延迟 ≈ (delta + 2×hop)/sr ≈ 32ms @48k，由首块读前余量补偿
  （`algorithmic_latency_samples`，推 n×speed+margin 后立即拉满 n——测试冻结）。
- 窗口/声道/速度：帧 1200 / 搜索 ±360 样本 @48k；声道共用偏移；速度 0.1–8.0
  （与 SpeedParams 一致；越界 ValueError，decoder 侧超界回退变调路径）。
- 许可证：自研 numpy 实现，无第三方许可约束。
- 回调边界：push/pull 内零解码、零无界分配；内部 FIFO 有界
  （不变式：pending ≤ margin + delta + 一个 push 块，测试冻结）。

## 2. 验收标准 → 证据

| 计划验收 | 证据 |
|---|---|
| 0.5/1.25/2 倍语音与已知频率向量，时长/音高落在冻结容差 | 冻结容差：音高 ±3%、时长 ≤ frame+delta+hop（≈45ms）。`test_sine_pitch_and_duration`（440Hz 正弦三档：440/440/439Hz）、`test_speech_like_preserves_f0`（220Hz 谐波簇自相关基频 ±3%）、decoder 级 `test_pitch_preserved_and_exact_block_length`（真实 WAV 三档，输出块恰好 2000 采样） |
| 预听/导出策略一致 | `test_streaming_matches_offline`：同参数流式 vs 离线互相关 ≥0.95（实测 0.9967/0.9967/0.9999）、长度差 ≤ frame；同一 `preserve_pitch_request` 口径解析（倒放/速度曲线实时同样回退变调并 logger 留痕，与导出回退策略一致——edit_page 接线） |
| 开关提示准确 | capability_registry `edit.audio.preserve_pitch` PARTIAL→AVAILABLE（新证据+参数/延迟/范围/许可 notes）；UI 标签"Preserve Pitch (export only)"→"Preserve Pitch"，tooltip 更新为"预览与导出同参数 WSOLA；倒放/曲线回退变调"；`test_capability_reports_available` + `test_capability_registry` 更新 |
| 切换无异常脉冲/爆音并有真实监听记录 | 冻结指标：切换/块界一阶差分 ≤ 10×全流 p95。`test_speed_switch_no_click`（纯流 1→2→0.5）+ `test_speed_switch_keeps_streaming`（decoder 级）。监听记录：`listening/` 下 2s 类语音源与 0.5/1.25/2× 流式输出 WAV（客观指标：f0 自相关归一 1.000，max_diff/p95_diff ≈ 4.7×，2s→3.975s/1.600s/1.012s） |
| seek、loop、暂停和倍率突变清理时域状态，与视频时钟补偿协同 | seek/loop/暂停：seek 分支 stretcher.reset()+余量重置（`test_seek_resets_stretcher_state`）；倍率突变：见 §3 语义偏离。视频时钟协同：伸缩在解码块内完成、输出块恰好 n——混音块节奏/音视频对齐机制不变，读前余量保证首块即刻可用（不引入额外输出延迟） |

## 3. 登记的语义偏离（有意为之，可复查）

1. **倍率突变保留对齐模板**（计划原文"倍率突变清理时域状态"）：实测清模板使下一帧
   失去对齐搜索（k=0 任意相位），OLA 重叠区产生 ~0.7 振幅脉冲，直接违背"切换无异常
   脉冲"验收；保留模板 + 只更新步进实测完全平滑（max_diff = 自然信号斜率）。
   seek/loop/暂停仍全清。已在 `set_speed` docstring 与本台账声明。
2. **1x 也走伸缩器**：1x 旁路会让 1x↔变速切换出现路径切换缝（新伸缩器读前余量跳过
   ~32ms 内容）。preserve_pitch 开启的 clip 全速经同一状态机（1x 时 WSOLA 近直通）。
3. **播放中 UI 开关 preserve_pitch**：属性面板修改经命令→音频重同步→decoder 重建，
   不会出现运行中 toggle 半块混路径；直接调用 set_preserve_pitch 运行中切换会重置
   伸缩器（接受一次状态重启，无崩溃路径）。

## 4. 登记的后续片（不阻塞验收）

1. 真机人工监听确认（headless 无法执行；listening/ WAV 已备，客观指标先行）。
2. 速度曲线（speed_points）的实时保调（分段常量 WSOLA + 曲线插值），当前与导出
   一致地回退变调并留痕。
3. J/K/L 全局倍率（transport speed，非 clip speed）的保调——transport 层目前不涉
   clip 伸缩语义，另立条目评估。

## 5. 变更清单

- `src/features/edit/core/audio_timestretch.py`：`StreamingWsola`（push/pull/flush/
  reset/set_speed，有界 FIFO，`algorithmic_latency_samples`）。
- `src/features/edit/core/audio_playback.py`：`AudioDecoder.set_preserve_pitch` +
  `_decode_preserve_pitch`（读 n×speed+margin → 流式 WSOLA → 恰好 n 输出；EOF flush；
  seek 清态；独立小数累积器）；`add_track(preserve_pitch=)` 透传。
- `src/features/edit/ui/edit_page.py`：`preserve_pitch_request` 同口径解析 + 不支持
  组合回退留痕。
- `src/features/edit/core/capability_registry.py`：条目 AVAILABLE + 完整声明。
- `src/features/edit/ui/edit_properties_panel.py`：标签/tooltip 更新。
- 测试：test_streaming_wsola.py（16）、test_preserve_pitch_realtime.py（8）、
  test_capability_registry.py 断言更新。
- 监听工件：docs/validation/feature_F/F3.2/listening/*.wav。
