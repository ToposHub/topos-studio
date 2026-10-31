# F0.4 ADR · 时间基、身份与迁移契约（冻结）

> 日期：2026-10-22；基线 commit：`ef91445f2`。性质：设计冻结 + 精确向量锁定（最小实现——现实现已满足契约，本 ADR 冻结语义并补向量测试；未发现需要改动的产品代码）。

## 1. 时间基契约

### 1.1 定义

| 概念 | 定义 | 载体 |
|---|---|---|
| 源帧（source frame） | 源媒体 0 基帧号；VFR 源以 PTS 索引（秒，`source_pts_index`） | `FrameMapRequest.source_start_frame` 等 |
| 时间线帧（timeline frame） | 编辑时间线 0 基绝对帧号 | `FrameMapRequest.timeline_frame` |
| 采样点（sample point） | 48kHz 音频样本位置；帧边界样本 = `Fraction(sample_rate * k * fps_den, fps_num)`（精确有理数） | 音频 retime/导出样本域计算 |
| 有理数时间基 | 全部中间量 `fractions.Fraction`；帧率一律 num/den（24/1、24000/1001…），禁止 float 累加 | `timeline_frame_mapper.py:20-21,150-163` |

### 1.2 区间开闭

- 时间线片段区间：**左闭右开** `[clip_timeline_start, clip_timeline_start + clip_timeline_duration)`；local ∈ [0, duration)。
- 源采样上界：**末帧含入（inclusive clamp）**——速度/混 fps 下四舍五入后不越过 `source_start + source_duration - 1`（`timeline_frame_mapper.py:171`）。
- NTSC 相邻样本边界：帧→样本换算只在整数样本处取边界，非整数处按 §1.3 舍入到样本并保持相邻帧映射单调（D06 向量锁定的对象）。

### 1.3 舍入规则（R07，已冻结）

`half-up`（对 Fraction 显式实现，规避 Python `round()` 的银行家舍入）：正数 `floor(x + 1/2)`。见 `timeline_frame_mapper.py:176-182`。

### 1.4 漂移保证

CFR/VFR 全路径 Fraction 算术 ⇒ 任意长度序列（含 29.97/59.94）**零累计漂移**——不是"误差 < ε"，而是精确相等。精确向量见 `tests/edit/test_timebase_precision_vectors.py`（本 ADR §5）。

## 2. 身份契约

| 实体 | 身份 | 稳定性规则 |
|---|---|---|
| MediaAsset | `asset_id`（UUID，经 `import_asset` 生成） | 路径可变（重连/代理），id 不变；虚拟路径 `topos://adjustment|sequence|solid/{id}` |
| ClipInstance | `clip_id` | 粘贴/复制必须生成新 id；`linked_clip_id` 双向 |
| TimelineSequence | `timeline_id` + 乐观锁 `revision`（统一提交入口单调 +1） | 跨页快照按 (timeline_id, revision) 对齐 |
| 调色栈 | `color_stack_id`（工程级），clip 经 `color_stack_id`+`color_shared` 引用 | UseSharedGradeCommand 切换不改 id |
| 跨页快照 | `audio_graph_snapshot`（revision+hash）、AI artifact（revision 冲突拒绝）、A 线 `trace_id`（schema v3） | 快照只读、派生态，不持久化 |
| 嵌套序列 | 子 `timeline_id` 经 `topos://sequence/{id}` 虚拟素材引用 | 防环 `would_create_cycle` |

新增身份（F3.3 采样编辑、F4.1 调色组、F4.2 版本、F8.2 评论、F8.3 take）**各自独立 schema 演进**，不复用/挤占上表现有 id 空间。

## 3. 迁移契约（每个新 schema 独立立项）

1. 版本化：每个子 schema（采样编辑位置、调色组、调色版本、评论锚点、候选 take）自带版本字段；迁移函数只消费自己版本的数据。
2. 未知字段保留（两层已实现并冻结）：
   - clip 级：`ClipInstance.from_dict` 把未知键收进 `_extra_fields` 并在 `to_dict` 回写（`timeline_v2.py:1495-1500`）；
   - 工程包级：`tests/project/test_project_module_preservation.py` 锁定未知模块文件整包保留。
3. 不提前写空字段：后续任务落地迁移时才增加字段；新增字段不保存 secret/机器绝对路径/大缓存（.clinerules）。
4. 降版不承诺无损：回退旧应用前保留备份（计划 §3）。

## 4. 帧映射精确向量（独立 oracle）

测试用独立 `Fraction` 重新计算期望值（不调用被测代码的内部函数），向量示例：

| 向量 | timeline fps | source fps | 输入 | 期望（oracle） |
|---|---|---|---|---|
| V-identity-29.97 | 30000/1001 | 同 | k ∈ {0,1,107892(≈1h),431568(≈2h)} | source == k（精确） |
| V-cross-24←NTSC | 24/1 | 30000/1001 | local=1 | `floor(1250/1001+1/2)`=1 |
| V-cross-24←NTSC | 24/1 | 30000/1001 | local=7 | `floor(8750/1001+1/2)`=9 |
| V-half-up | 任意混 fps | — | 舍入边界 | half-up 而非银行家舍入 |
| V-reverse | 24/1 | 24/1 | reverse | 末帧起反向精确 |
| V-speed | 24/1 | 24/1 | speed=1/2 | 精确半速 |
| V-audio-boundary | 30000/1001 | — | 48kHz | 帧 k 边界样本 = Fraction(48000·k·1001, 30000)；k=5 整数 8008 |
| V-extras | — | — | clip dict 带 `future_field` | `_extra_fields` 保留并 round-trip |

## 5. 验证与未验证

- 已验证（本 ADR 随附）：`tests/edit/test_timebase_precision_vectors.py` 全绿；既有 `test_mixed_fps_playback_cadence`、`test_multicam_nested_timecode_truepeak`、`test_project_module_preservation` 通过。
- 未验证/边界：VFR PTS 索引的 float 精度（`source_pts_index: tuple[float,...]` 为秒的 float 表示——PTS 源头即 float 秒，映射内部仍 Fraction 比较；VFR 长片的 PTS 表精度受容器时基限制，登记为已知边界，非本 ADR 引入）；子帧/采样级位置 schema 属 F3.3，本 ADR 只冻结整数帧契约。
