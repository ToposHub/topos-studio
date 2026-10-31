# F3.3 · 采样级音频编辑与迁移 — 验证台账

- 日期：2026-10-24
- 计划条目：docs/feature_plan_F_davinci_2026-10-22.md §F3.3
- 测试：`tests/edit/test_sample_time_audio.py`（16）＋回归 138 绿
  （audio transitions / clip speed fade / preserve-pitch×2 / round7 audio
  output / timestretch / multicam production / 序列化×3）

## 1. 交付范围与语义

**核心换算层** `core/sample_time.py`：
- `TimelineSampleClock`：帧↔样本全 `Fraction`（29.97@48k 帧长 = 8008/5
  精确有理数）；`block_bounds` 相邻边界差闭合（无孔洞/重叠，D06 同型）；
  `position_samples(frame, offset)` = (帧, 整数偏移) 二元组精确表示任意
  子帧位置——**非整数帧率下不做帧进位归一化**（任何进位引入有理数残差
  错位 ≤0.6 样本，这是有意的设计决策）。
- `SampleSkipGate`：子帧偏移的块消费门（实时 decoder 与导出/Stem 循环
  同一实现——预览/成片同刻）。

**模型字段**（ClipInstance，F3.3）：
- `audio_sample_offset`（时间线侧子帧延迟入点，int 样本 @48k 参考率）
- `source_sample_offset`（源侧子帧修剪：入点后跳过 N 源样本）
- `fade_in_samples` / `fade_out_samples`（采样域淡化，设置时覆盖帧域）

**命令**：`UpdateClipSampleFieldsCommand`（nudge/slip/fade 公共底座，
before/after 快照原子事务、单步 Undo、宏注册表可序列化）。

**消费链**（三路同语义）：
- 实时：`AudioDecoder.set_fade` 扩展采样域参数；`_apply_skip_gates`
  在两条解码路径（变调/保调）末端串联消费；`mix_samples` 淡化调用透传
  采样域覆盖与偏移；`add_track`→`edit_page` 接线。
- 成片：`timeline_export` clip_fade dict 带采样域键 + 块循环门。
- Stem：`decode_clip` 三条返回路径（pp/retime/1x）统一包 `_apply_sample_gate`。

## 2. 验收 → 证据

| 计划验收 | 证据 |
|---|---|
| 48kHz 可移动/修剪一个 sample | `test_move_by_one_sample_undo`（nudge 1 样本+Undo）、`test_trim_in_by_one_sample`（源侧 1 样本）、`test_skip_gate_one_sample_move`（渲染端 1 样本位移）、`test_fade_boundary_exact_sample`（淡化第 N 样本包络精确） |
| 非整数 fps 长序列无累积漂移 | `test_ntsc_frame_length_exact_rational`、`test_long_sequence_no_accumulated_drift`（10 万帧有理闭合精确 vs float32 逐步累积漂移 ≥1 样本反例）、`test_block_bounds_close_exactly` |
| 旧工程迁移、Undo、重开精确保留 | `test_defaults_and_legacy_roundtrip`（无字段→默认；序列化不写新键=字节兼容）、`test_exact_roundtrip_cross_platform`（整数样本精确往返）、`test_invalid_payloads_fall_back_not_silently_rounded`（负值/浮点载荷防御回退默认，可检测不静默）、命令 Undo/宏序列化 4 项 |
| Stem/成片一致 | 三路消费链同一 `SampleSkipGate`/淡化键（代码路径）；回归套件全绿 |

## 3. 整数帧假设盘点（计划"盘点…逐个兼容迁移"）

| 站点 | 假设 | 兼容性 |
|---|---|---|
| ripple/波纹（InsertClip 等） | timeline_start/duration int 帧 | **兼容**：子帧偏移挂在 clip 上随片段移动，波纹操作不动 offset |
| linked A/V 对 | 双轨同 timeline_start | **兼容**：视频侧保持帧对齐（计划要求）；音频子帧偏移不传染视频 |
| trim/split | int 帧边界 | **本片语义**：帧级 trim 保留 offset（split 继承左片 offset；登记后续片：split 处 offset 归属策略可配） |
| markers | 帧号 | **兼容**（marker 不涉子帧） |
| 速度/retime | 帧映射 | **本片边界**：保调/变调路径的子帧偏移在解码块端消费（skip gate），retime 数学不感知 offset——速度片段的子帧对齐登记后续片 |
| 保存/交换 | to_dict 键 | **兼容**：非默认才写新键（旧工程字节不变）；未知字段经 _extra_fields 保留（R02） |
| 转场/交叉淡化 | 帧区间 | **登记后续片**：crossfade 边界仍帧级；子帧 crossfade 需转场模型升级 |

## 4. 登记的后续片（不阻塞验收）

1. 子帧 crossfade（转场模型帧级边界升级）。
2. retime/保调片段的子帧精确对齐（当前 gate 在解码端，速度数学不感知）。
3. split 时 offset 归属策略（当前继承左片；可选右片/按比例）。
4. UI：波形/属性面板显示与拖拽采样级偏移（模型/命令已就绪）。
5. 工程级 `audio_sample_rate` 声明字段（当前固定 48k 参考率，渲染端
   有理换算已预留）。

## 5. 诚实边界

- 子帧位置表示为 (帧, 整数偏移) 二元组：绝对样本位置为有理数；渲染
  块端消费为整数样本跳过（48k 下精确；非 48k 块率时有理换算后执行，
  误差 <1 样本，不静默丢——gate pending 记账）。
- 本片不改变时间线主体帧量化（ripple/转场等仍帧域）——完整采样域
  时间线边界移除属后续演进，语义边界已在 §3 声明。
