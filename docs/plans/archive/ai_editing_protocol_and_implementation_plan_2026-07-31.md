# Topos AI-Native 剪辑工程协议与实施计划

> 状态：设计基线，尚未实现。
>
> 日期：2026-07-31。
>
> 目标：让大模型可以在**不阅读 Topos 源码**的前提下，通过受控接口完成素材理解、创作规划、多方案剪辑、校验、预览和导出；同时为 FCPXML 和后续交换格式预留稳定边界。

## 1. 先给出结论

项目不应把“LLM 生成一份 XML，然后软件直接导出”作为核心架构。正确的核心是：

```text
创作 Brief / 自然语言需求
        │
        ▼
素材事实与语义索引（转录、镜头、人物、标签、用户标注）
        │
        ▼
故事结构 / 候选片段 / 多个 Edit Variant
        │             ▲
        │             └── 用户反馈、品牌规则、节奏/平台约束
        ▼
Topos AI Edit Tools（查询能力 + 声明式编辑操作）
        │             ▲
        │             └── capability manifest、素材分析、校验错误
        ▼
AI Edit Plan（可审计、可重放、可预览的操作清单）
        │
        ▼
现有 EditCommand / MacroCommand → TimelineSequence（唯一事实源）
        │                                      │
        │                                      ├── .topos 工程包
        │                                      ├── Preview / Playback
        │                                      └── TimelineExporter / Deliver
        ▼
FCPXML / OTIO / 其他格式适配器（导入和导出，不参与核心渲染）
```

因此：

1. **内部剪辑模型**应继续以现有 `EditProject` + `TimelineSequence` 为唯一事实源，不能再创建第二套“AI 时间线”。但它上方必须增加可版本化的 Brief、语义素材、故事结构、方案与决策层；否则 AI 只能做“按指令搬动 clip”。
2. AI 对外获得的是一个稳定的、版本化的**声明式协议**（下面称为 **TAEP，Topos AI Edit Protocol**），而不是 Python 对象、UI 点击序列、GLSL 或任意代码执行权限。
3. XML 是互操作格式，不是内部数据模型。不存在真正通用且无损的“XML 剪辑表”：FCPXML、旧 Final Cut Pro 7 XML、Premiere XML、Resolve XML 的语义各不相同。
4. 最先实现的 XML 适配器应是 **FCPXML**；它覆盖常见的 NLE 交换需求，且本项目 `interchange.py` 已明确将其列为下一优先级。OTIO 可作为第二交换入口，但它不是 XML，适合在 Python/管线工具间交换时间线结构。
5. 脚本系统应成为受信任的自动化入口；LLM 线上操作应优先走工具/API。不要让模型生成的任意 Python 自动获得本机文件、网络、`os` 或 UI 权限。
6. AI 的主要产物不是单一成片，而是**带证据、带约束、可比较、可回滚的多个剪辑方案**。人类可以批准方案、拒绝某个决策、锁定片段或重新规划，而无需从零剪。

## 2. 当前工程的事实基线

本计划基于当前代码，不假设一个不存在的“空白剪辑器”。

| 现有能力 | 代码位置 | 对 AI 剪辑的意义 |
|---|---|---|
| 项目/素材模型 | `src/features/edit/core/project.py` | `EditProject`、`MediaAsset`、素材箱、时间线引用、离线素材、代理、色彩/合成引用已存在。 |
| 标准时间线 | `src/features/edit/core/timeline_v2.py` | `TimelineSequence`、`EditTrack`、`ClipInstance`、`TransitionInstance`、`SubtitleClip`、marker 均为不可变模型并可序列化。 |
| 编辑命令与撤销 | `src/features/edit/core/commands.py` | 添加、移动、裁切、分割、转场、效果、字幕等操作已走 Command/MacroCommand；AI 必须复用这条路径。 |
| 工程持久化 | `src/features/edit/core/edit_project_io.py`、`src/shared/project/` | `.topos` 是工程包；AI 元数据应作为可选工程 payload，不应旁路项目保存。 |
| 播放/预览 | `timeline_preview_renderer.py`、`realtime_playback/`、`edit_gpu_preview_renderer.py` | 预览是既有模型的消费者；AI 不触碰 OpenGL 或 UI。 |
| 成片导出 | `src/features/edit/core/timeline_export.py` | `TimelineExportConfig`、preflight 与 `TimelineExporter` 已提供离线导出基础。 |
| 效果目录 | `src/features/edit/core/effect_registry.py` | 可从注册表生成给模型的受限效果能力表，禁止模型随意发明效果参数。 |
| 字幕模型 | `timeline_v2.py` 的 `SubtitleClip` 与 `core/captions/` | 可作为 AI 转写/剪辑结果的第一类时间线对象。 |
| 场景切分 | `src/features/edit/core/scene_cut/` | 是构建镜头级素材分析结果的天然起点。 |
| 现有脚本面板 | `src/app/ui/script_panel.py`、`src/features/scripting/` | 已有后台 `ScriptEngine` 和 `ToposAPI`，但 API 当前主要面向调色，不可直接作为 AI 剪辑接口。 |
| 交换能力评估 | `src/features/edit/core/interchange.py` | 已有 BWF、CSV cue sheet、stems；FCPXML 被列为未实现但推荐的下一步。 |

还有一个必须正视的现状：完整时间线目前不是完全收在 `EditProject` 内。`EditProject.timelines` 保存的是轻量 `TimelineSequenceRef`；完整的 `dict[timeline_id, TimelineSequence]` 由应用/controller 生命周期维护，并在工程包中以 `timeline_sequences` 保存。保存前已有两张表 key 一致性的检查。AI、脚本和交换导入必须通过同一个应用服务**原子同步**这两层；只修改 `EditProject.timelines` 或只替换 controller 中的 sequence map 都是错误的。

### 2.1 必须保留的工程约束

- `TimelineSequence` 和其 clip/track 是不可变数据。编辑的结果必须由命令返回新实例，绝不在 AI 层原地修改列表。
- 当前不少命令由 `TimelineView` 内的 command stack 发起，随后由 `EditController` 同步到完整 sequence map、媒体池和 renderer snapshot。P0 必须先抽出无 UI 的 service 提交入口；AI 绝不实例化 widget 或模拟拖拽。
- 时间线所有位置均使用**整帧**；片段区间采用 `[start_frame, end_frame)` 的左闭右开规则。对外协议也必须如此。
- 帧率必须保存为有理数，例如 `24000/1001`，而不是只以 `23.976` 浮点数计算时间。当前模型已有 `Rational` 基础，协议不得退化它。
- UI 线程不得调用 ModernGL；AI 提交操作只是更新编辑状态，预览与 GPU 工作仍沿现有异步路径执行。
- 导出不可依赖当前打开的 UI 页面、当前 viewer texture 或脚本上下文；必须从工程状态与 timeline 重新构建。
- 没有通过校验的计划不得偷偷“尽力渲染”；允许修复建议，但提交和导出需要明确的状态门禁。

## 3. 名称、边界与版本策略

### 3.1 协议名称

采用：**TAEP — Topos AI Edit Protocol**。

建议的首版版本号：`taep/1.0`。版本号只描述外部 AI 合同，不等同于 `.topos` 工程格式版本、FCPXML 版本或软件版本。

### 3.2 三个层次，不可混用

| 层次 | 表示 | 读者 | 是否是项目真相 |
|---|---|---|---|
| Canonical domain | `EditProject`、`TimelineSequence`、`EditCommand` | Topos 内核 | 是 |
| AI contract | TAEP capability、snapshot、plan、operation、diagnostic | LLM、MCP/API 客户端、脚本 | 否；必须编译到 canonical domain |
| Interchange | FCPXML、OTIO、CSV/SRT 等 | 外部 NLE/工具 | 否；通过 adapter 导入/导出 |

所有未来字段都分为：

- `core`：Topos 已保证可渲染、可保存、可 round-trip 的字段；
- `extension`：Topos 特有能力，例如 ColorStack、Composite Graph、AI provenance；
- `unsupported`：导入时无法解释但应保留原始信息/发出诊断的字段。

**禁止**让 FCPXML 的结构直接渗入 `TimelineSequence`，也禁止让 AI plan 成为第二种工程文件。

### 3.3 AI-native 的四类创作状态

AI 剪辑不能只保存“最终怎么剪”，还必须保存“为什么这么剪”。在 canonical timeline 之上新增以下**可选工程 payload**；它们不参与逐帧渲染真相，却必须能引用到确定的 `asset_id`、source range 和 `clip_id`。

| 状态层 | 一等对象 | 回答的问题 | 写入权限 |
|---|---|---|---|
| 创作意图 | `EditorialBrief` | 这条片面向谁、讲什么、要多长、什么不能做？ | 用户为主，AI 只能提议 patch |
| 素材事实 | `AssetAnalysis`、`SemanticSegment` | 素材里何时出现什么内容、证据和置信度是什么？ | provider/用户写入；AI 只读 |
| 方案与决策 | `StoryOutline`、`EditVariant`、`EditDecision` | 哪些素材为什么被选择/排除；哪个版本更符合 brief？ | AI 可提议，用户可锁定/否决 |
| 确定性交付 | `EditPlan`、`TimelineSequence`、`ExportRequest` | 当前版本逐帧如何呈现和导出？ | 仅经 validate/approval/command 提交 |

四层必须单向落地：事实和用户锁定内容不能被规划器覆盖；`EditVariant` 通过 `EditPlan` 编译为 timeline；timeline 的人工改动反向生成 `EditDecision`/override，而不是被下一次 AI 规划抹掉。

### 3.4 ID 和引用规则

- `project_id`、`timeline_id`、`track_id`、`asset_id`、`clip_id`、`operation_id`、`brief_id`、`variant_id`、`decision_id` 均为稳定 UUID 字符串。
- LLM 可以请求由 Topos 创建 ID，但不能以显示名称作为唯一引用。名称可能重复或被用户修改。
- 人类可读别名只能做查询过滤，例如 `{"name_contains":"采访"}`；返回结果必须附带稳定 ID。
- 外部导入 ID 与内部 ID 保存在 `external_ids`/provenance 中，不能覆盖 Topos UUID。
- 任何 patch 都必须携带 `base_revision`，防止 AI 把过期的时间线覆盖掉用户刚做的修改。

## 4. 时间与坐标合同

这是协议中最容易造成长片累计误差的部分，必须优先实现和测试。

### 4.1 规范时间表达

```json
{
  "timebase": {"fps": {"num": 24000, "den": 1001}, "drop_frame": false},
  "frame_range": {"start": 240, "duration": 72}
}
```

- 所有写入时间线的时刻是非负 `int` 帧号。
- `duration` 必须是正整数。
- `end` 永远由 `start + duration` 推导，不允许同时传一个可能矛盾的 `end`。
- UI 或 LLM 可输入秒数/时码，但 API gateway 必须在边界层按明确策略转帧：开始点 `floor`、结束点 `ceil`，并把转换结果和 warning 返回。
- 视频源范围仍使用源帧范围；音频可以携带 source sample metadata，但 placement 仍以 timeline frame 为准。
- 29.97/59.94 的 drop-frame 只影响显示和某些交换格式的 timecode，不改变内部帧位置。

### 4.2 必须拒绝的情况

- fps `<= 0`、分母 `<= 0`；
- 负帧、零时长、source 范围越过已知素材时长；
- 同一非叠加视频轨的重叠 clip；
- 转场引用不存在的 clip、跨轨 clip 或不相邻 clip；
- 目标 track 类型与 clip 类型不匹配；
- 采用不支持的变速/倒放/复合效果，却没有声明降级策略；
- 源素材离线而操作要求探帧、预览或导出。

## 5. TAEP 数据合同

TAEP 首版应使用 JSON。JSON 对 LLM 工具调用、JSON Schema 校验、diff、审计和测试最友好。XML adapter 可以从同一 canonical model 生成，不要求模型手写 XML。

### 5.1 Capability Manifest：模型唯一需要背的“说明书”

模型每个会话先调用 `get_capabilities()`，获得当前软件真正支持的能力、值域和限制；不要把一份静态长 prompt 当成真相。

```json
{
  "protocol": "taep/1.0",
  "project_revision": "sha256:...",
  "timebase_support": {
    "frame_only_writes": true,
    "accepted_fps": ["24/1", "24000/1001", "25/1", "30000/1001", "30/1", "60000/1001", "60/1"]
  },
  "track_types": ["video", "audio", "subtitle"],
  "operations": ["add_clip", "trim_clip", "move_clip", "split_clip", "add_transition", "add_subtitle", "set_effect", "set_marker"],
  "effects": [
    {
      "effect_type": "gaussian_blur",
      "target": "video_clip",
      "parameters": {"radius": {"type": "float", "min": 0.0, "max": 100.0, "default": 0.0}}
    }
  ],
  "export": {"codecs": ["h264", "h265", "prores_422"], "requires_confirmation": true},
  "limits": {"max_operations_per_plan": 500, "max_preview_frames": 300}
}
```

manifest 必须从实际 registry 与 feature flags 生成，不能手工复制一份效果表。这样未实现的参数不会被模型调用。

### 5.2 Project Snapshot：给模型的只读世界状态

snapshot 不是 `.topos` 原文件，也不能包含所有帧/缩略图。应按请求返回：

- 项目格式、revision、当前活动时间线；
- 素材 ID、路径的**脱敏显示名**、媒体类型、时长/帧率/分辨率、离线/代理状态、reel/scene/shot/take；
- 轨道与 clip 的必要时间线结构；
- marker、字幕摘要、当前选择区间；
- 选配：转写、场景切分、镜头标签、语音人声、响度等“分析资产”；
- 不返回本机绝对路径、用户隐私 metadata、未授权脚本、原始二进制或无关项目数据。

```json
{
  "protocol": "taep/1.0",
  "project_id": "p-uuid",
  "revision": 42,
  "active_timeline": {
    "timeline_id": "t-main",
    "format": {"width": 1920, "height": 1080, "fps": {"num": 25, "den": 1}, "sample_rate": 48000},
    "tracks": [
      {"track_id": "v1", "kind": "video", "index": 0, "locked": false, "clips": [
        {"clip_id": "c-01", "asset_id": "a-interview", "timeline": {"start": 0, "duration": 250}, "source": {"start": 1200, "duration": 250}}
      ]}
    ]
  },
  "assets": [{"asset_id": "a-interview", "name": "采访 A", "kind": "video", "duration_frames": 18500, "analysis_status": "ready"}]
}
```

### 5.3 Editorial Brief：让 AI 先理解“要做什么”

`EditorialBrief` 是 AI-native 剪辑的核心，不是一个可有可无的 prompt 字符串。它是用户可编辑、可版本化、可验证的创作合同；没有它，模型只能按“好看”这一主观且不可复现的默认目标工作。

```json
{
  "brief_id": "brief-launch-30s",
  "revision": 6,
  "objective": {
    "primary_goal": "在 30 秒内说清新品的核心价值并引导点击",
    "audience": ["首次接触产品的短视频用户"],
    "platform": "vertical_social",
    "target_duration_frames": {"ideal": 750, "min": 675, "max": 825},
    "language": "zh-CN"
  },
  "narrative": {
    "required_beats": ["hook", "problem", "product_value", "call_to_action"],
    "priority_order": ["clarity", "pace", "brand_safety"],
    "tone": ["confident", "warm", "fast_paced"]
  },
  "constraints": {
    "locked_source_ranges": [{"asset_id": "a-hero", "start": 240, "duration": 100}],
    "forbidden_labels": ["competitor_logo", "uncleared_music"],
    "must_include_claim_ids": ["claim-fast-charge"],
    "subtitle_policy": "burn_in_zh_cn",
    "safe_area_profile": "vertical_social",
    "approval_required_for": ["copyright_risk", "generated_media", "destructive_edit"]
  },
  "deliverables": [
    {"id": "main", "aspect_ratio": "9:16", "duration_target": 750, "export_preset": "social-vertical"},
    {"id": "teaser", "aspect_ratio": "9:16", "duration_target": 375, "export_preset": "social-teaser"}
  ]
}
```

设计规则：

- 约束分为 `hard`（违反即拒绝）和 `soft`（作为排序/解释依据）；不能把二者混成一个 prompt。
- 用户可锁定 source range、故事 beat、字幕文案、音乐、品牌素材和已批准 timeline；锁定对象永远优先于 AI 重规划。
- brief 的每次变更会产生新 revision，并使受影响的 variant 标为 `needs_replan`，但不会删除旧方案。
- 成功度量应可观察：时长、必要 beat 覆盖、字幕可读性、品牌/版权规则、用户选择，而不是把“模型自评分”当成事实。

### 5.4 Analysis Manifest 与 Semantic Segment：让 AI “看懂素材”，不让 AI 猜素材

素材分析是独立于剪辑计划的缓存数据。建议为每个 `asset_id` 建立版本化 `AssetAnalysis`，并把不同 provider 的结果归一为可查询的 `SemanticSegment`：

```json
{
  "asset_id": "a-interview",
  "asset_fingerprint": {"size": 32109876, "mtime_ns": 0, "content_hash": "optional"},
  "timebase": {"fps": {"num": 25, "den": 1}},
  "scene_cuts": [{"frame": 0, "confidence": 1.0}, {"frame": 375, "confidence": 0.92}],
  "transcript": {"language": "zh-CN", "segments": [{"start_frame": 42, "end_frame": 169, "text": "...", "speaker": "S1"}]},
  "tags": [{"start_frame": 375, "end_frame": 525, "labels": ["产品", "特写"]}],
  "audio": {"integrated_lufs": -18.2, "speech_ranges": [[42, 169]]},
  "generator": {"provider": "local", "model": "...", "created_at": "..."}
}
```

`SemanticSegment` 的最小字段为：`segment_id`、`asset_id`、`source_range`、`kind`（transcript/shot/tag/ocr/audio/user_note）、`payload`、`confidence`、`evidence_ref`、`provenance`、`user_verified`。它允许 AI 精确说出“选择 a-interview 的 1250–1425 帧，因为这段覆盖 claim-fast-charge，且用户已确认转录”。

规则：分析结果只能提出剪辑候选，不能直接改变工程；fingerprint 不匹配即失效；用户手工标注与 AI 推断分层保存；模型、提示词和转录文本都可能属于敏感数据，保存前需用户选择。

### 5.5 Story Outline、Edit Variant 与 Edit Decision：让 AI 产出可比较的编辑思路

`StoryOutline` 不是时间线，它描述叙事槽位及证据：例如 Hook 应使用哪一类镜头、产品价值如何说明、CTA 必须在何处出现。`EditVariant` 是一个可命名的方案，包含 outline、候选操作、评分解释、preview artifact 和与 brief/analysis 的版本关系。它可以有多个并存版本，例如 `A_口播优先`、`B_B-roll 优先`、`C_节奏更快`。

```json
{
  "variant_id": "variant-b",
  "brief_id": "brief-launch-30s",
  "brief_revision": 6,
  "analysis_snapshot_hash": "sha256:...",
  "status": "proposed",
  "outline": [
    {"beat_id": "hook", "target_range": {"start": 0, "duration": 75}, "evidence": ["seg-reaction-01"]},
    {"beat_id": "product_value", "target_range": {"start": 75, "duration": 475}, "evidence": ["seg-claim-fast-charge"]}
  ],
  "decisions": [
    {
      "decision_id": "d-01",
      "action": "select_source_range",
      "subject": {"asset_id": "a-interview", "source": {"start": 1250, "duration": 175}},
      "supports": ["product_value", "claim-fast-charge"],
      "evidence_refs": ["seg-claim-fast-charge"],
      "alternatives_considered": ["seg-claim-fast-charge-take2"],
      "confidence": 0.86,
      "explanation": "表述完整且镜头稳定；另一条有口误。",
      "state": "proposed"
    }
  ],
  "scores": {
    "hard_constraints_passed": true,
    "duration_fit": 0.96,
    "beat_coverage": 1.0,
    "pace_estimate": 0.81,
    "explanation": "仅作为排序依据，不替代人工判断。"
  },
  "edit_plan_ref": "plan-uuid"
}
```

关键规则：

- AI 分数只能帮助排序；它必须同时返回证据与备选项，用户不能被迫相信单一分数。
- `EditDecision` 有 `proposed / accepted / rejected / user_override / superseded` 状态。用户拒绝“删口误”后，该拒绝应成为下一轮规划的约束。
- 人工在时间线上的修改必须能以最小粒度映射为 `user_override`，并标出原 AI decision 是否仍有效。
- variant 是不可变 snapshot；接受一个 variant 会产生一个新的 canonical timeline revision，而不是把其他备选方案覆写掉。

### 5.6 Edit Plan：从方案到确定性时间线的声明式落地


AI 的写入单位是 `EditPlan`，不是一次 API 调用一个像素级动作。它应支持预览、显示 diff、原子提交、撤销与审计。

```json
{
  "protocol": "taep/1.0",
  "plan_id": "plan-uuid",
  "base_revision": 42,
  "target": {"project_id": "p-uuid", "timeline_id": "t-main"},
  "mode": "preview",
  "title": "提取采访中的产品亮点并生成 30 秒竖版初剪",
  "rationale": "只使用标记为产品亮点且语义完整的片段。",
  "operations": [
    {
      "op_id": "op-01",
      "kind": "ensure_track",
      "track": {"track_id": "v2", "track_type": "video", "name": "B-roll"}
    },
    {
      "op_id": "op-02",
      "kind": "add_clip",
      "track_id": "v1",
      "asset_id": "a-interview",
      "source": {"start": 1250, "duration": 175},
      "timeline": {"start": 0, "duration": 175},
      "intent": {"reason": "产品核心卖点"}
    },
    {
      "op_id": "op-03",
      "kind": "add_subtitle",
      "track_id": "s1",
      "timeline": {"start": 0, "duration": 175},
      "text": "核心卖点",
      "style_preset_id": "caption-social-default"
    }
  ],
  "requested_outputs": [{"kind": "preview", "range": {"start": 0, "duration": 175}}]
}
```

`mode` 只能是：`preview`、`commit`、`dry_run_export`。外部 LLM 初始权限只能提交 `preview`；`commit` 和最终 `export` 必须由 UI 或受信任自动化策略批准。

### 5.7 首版 operation 白名单

首版只交付已存在且容易验证的 NLE 基本操作，不承诺“AI 能做一切”。

| 分类 | `kind` | 编译目标/备注 |
|---|---|---|
| 查询 | `get_snapshot`、`find_assets`、`get_analysis`、`get_capabilities` | 只读工具，非 EditPlan operation。 |
| 结构 | `create_timeline`、`ensure_track`、`rename_track`、`set_track_state` | 对应项目/时间线模型；禁止删除包含未确认内容的轨。 |
| 基础剪辑 | `add_clip`、`move_clip`、`trim_clip`、`split_clip`、`delete_clip`、`replace_clip` | 编译到现有 `EditCommand`，执行 overlap/源范围检查。 |
| 转场/淡化 | `add_transition`、`remove_transition`、`set_clip_fade` | 只开放 registry/模型当前支持的 cross dissolve、dip、wipe。 |
| 图像处理 | `set_transform`、`set_crop`、`set_opacity`、`set_speed`、`set_effect`、`remove_effect` | value/range 从 `EffectRegistry` 读取；不存在的 effect 必须报错。 |
| 音频 | `set_clip_gain`、`set_track_mute`、`set_ducking_rule` | 以当前音频图正式 API 为准；未建立 command 前不放入 manifest。 |
| 字幕 | `ensure_subtitle_track`、`add_subtitle`、`update_subtitle`、`delete_subtitle` | 复用字幕锁、样式和 provenance 规则；不得绕过 caption command。 |
| 标记/审核 | `set_marker`、`set_range`、`add_review_note` | 适合让模型标记候选剪辑，先交人审。 |
| 导出请求 | `request_export` | 只创建 `TimelineExportConfig` 提议，先走 preflight 和权限确认。 |

`ripple_delete`、多机位、嵌套时间线、完整关键帧曲线、生成式视频、复杂混音、合成图编辑、多人协作合并均应在 manifest 中显示为未支持，直到各自有明确的 domain command 与测试。

### 5.8 返回格式：诊断必须给机器和人都能读的答案

```json
{
  "plan_id": "plan-uuid",
  "status": "rejected",
  "base_revision": 42,
  "current_revision": 43,
  "diagnostics": [
    {
      "severity": "error",
      "code": "TAEP_REVISION_CONFLICT",
      "op_id": null,
      "path": "/base_revision",
      "message": "项目已被用户修改；请重新读取快照后再生成计划。",
      "suggested_action": "refresh_snapshot"
    }
  ]
}
```

诊断 code 一经发布应稳定。建议至少定义：

- `TAEP_SCHEMA_INVALID`、`TAEP_UNSUPPORTED_OPERATION`、`TAEP_PERMISSION_DENIED`；
- `TAEP_REVISION_CONFLICT`、`TAEP_TIMELINE_NOT_FOUND`、`TAEP_TRACK_NOT_FOUND`；
- `TAEP_ASSET_OFFLINE`、`TAEP_SOURCE_RANGE_INVALID`、`TAEP_TRACK_OVERLAP`；
- `TAEP_EFFECT_INVALID`、`TAEP_EFFECT_PARAMETER_INVALID`；
- `TAEP_EXPORT_PREFLIGHT_FAILED`、`TAEP_INTERCHANGE_LOSSY`。

## 6. 事务、审批、撤销和审计

### 6.1 两阶段提交

每个写计划必须经历：

1. **解析**：JSON Schema + protocol version + operation 白名单。
2. **读取一致性检查**：`base_revision == current_revision`。
3. **编译**：每个 operation 转为现有 `EditCommand`，禁止直接改 `TimelineSequence` 私有状态。
4. **纯数据模拟**：在副本上用 `MacroCommand` 应用，得到 proposed sequence。
5. **domain 校验**：引用完整性、轨道语义、重叠、素材范围、转场、字幕锁、颜色/合成引用、效果参数。
6. **呈现 diff**：新增/删除/移动片段、时长变化、受影响轨、风险与导出预估。
7. **用户/策略审批**：首次 `commit`、覆盖大量片段、删除、渲染、文件写入均需显示确认。
8. **原子提交**：在 revision 未变化时一次提交为单个可命名 `MacroCommand`，只产生一个 undo 单位。
9. **保存/可选预览**：沿现有 project IO、preview pipeline 运行。

任一步错误都不应留下半套时间线。运行时异常必须返回计划级失败和明确的 `op_id`。

### 6.2 Revision 的最低设计

- `EditProject`/活跃时间线每次成功 commit 递增逻辑 revision。
- 可以先存递增整数，后续再加 canonical JSON hash 作为 `content_hash`。
- 用户手工修改、脚本修改、AI 提交、外部 XML 导入都必须走同一个 revision 更新入口。
- 冲突不自动 last-write-wins。AI 重新读取 snapshot、重新规划；必要时支持 explicit rebase，但仅对不冲突操作。

### 6.3 审计记录

项目可选保存 `ai/audit/<plan_id>.json`，包含：协议版本、模型/客户端标识、base/new revision、批准人、operation 摘要、诊断、时间与产物 hash。

默认不保存原始对话、API key、本机绝对路径或素材内容。需要可复现时，由用户选择保存 prompt、模型版本、分析结果 hash 和 export manifest。

## 7. 面向大模型的工具面，而不是“告诉它写 XML”

若采用 tool calling/MCP/HTTP，本地 host 应暴露下面的语义工具；工具的输入输出使用 TAEP JSON Schema。

| 工具 | 权限 | 输入 | 输出 |
|---|---|---|---|
| `topos.get_capabilities` | read | 无/feature scope | 动态 manifest |
| `topos.get_project_snapshot` | read | 范围、timeline、detail level | 脱敏 snapshot + revision |
| `topos.get_editorial_brief` / `propose_brief_patch` | read / proposal | brief ID、用户意图 | 版本化 brief 或待批准 patch |
| `topos.search_assets` | read | 文本、标签、时段、媒体类型 | ID 列表与摘要 |
| `topos.get_asset_analysis` | read | `asset_id`、分析类型 | scene/transcript/tag/audio 数据 |
| `topos.create_edit_variants` | proposal | brief、证据范围、variant 策略 | StoryOutline、EditDecision、候选 EditPlan |
| `topos.compare_variants` | read | variant IDs、比较维度 | 时长、约束覆盖、证据、差异、preview 引用 |
| `topos.accept_decision` / `reject_decision` | approval | decision ID、可选反馈 | 可持久化人类偏好/锁定规则 |
| `topos.validate_edit_plan` | preview | EditPlan | diff、diagnostics、render cost estimate |
| `topos.apply_edit_plan` | write + approval | 已验证 plan、approval token | new revision、undo group、diff |
| `topos.render_preview` | expensive + approval policy | timeline/range/preset | job id、缩略图/视频地址 |
| `topos.request_export` | file write + confirmation | ExportRequest | preflight、计划 job；不立即覆盖文件 |
| `topos.get_job_status` / `cancel_job` | scoped | job id | 状态、进度、产物 |
| `topos.import_interchange` / `export_interchange` | file read/write + confirmation | adapter、路径/安全 token、策略 | report、diagnostics、产物 |

模型的典型循环是：`capabilities → brief → snapshot/analysis → outline + variants + decisions → 用户选择/锁定 → validate_edit_plan → apply → preview → export preflight → export`。

它不应能调用“鼠标点击时间线”“执行 Python 字符串”“读取任意路径”“调用 shell”。

## 8. Script 功能的定位与安全改造

现有 `ScriptEngine` 在后台 `QThread` 中 `exec()` 用户提供的 Python，`ToposAPI` 又有文件与 UI 相关功能。它对本机用户的受信任脚本是合理的，但不能直接暴露给远程模型。

建议分成两套表面：

1. `ToposEditAPI`：给本地、已签名或用户确认的 Python 脚本。它是 TAEP operation 的 Python facade，不准绕开 command compiler。
2. `Topos AI Tools`：给 LLM 的受限工具接口。默认无 Python、无 shell、无任意 filesystem、无网络权限。

首版建议的脚本用法：

```python
from topos.edit import EditSession

session = EditSession.current()
plan = session.load_plan("/approved/rough_cut.taep.json")
report = session.validate(plan)
if report.has_errors:
    raise RuntimeError(report.format())
session.apply(plan, approval="local-user")
session.export_from_preset("social-vertical")
```

脚本 API 的规则：

- 所有编辑仍经 `validate → compile → MacroCommand → commit`；
- 每次脚本执行默认生成一个 undo group；
- 提供 `dry_run`，不提供任意对象内部引用；
- 文件路径仅能由用户选取或 capability token 授权；输出不允许静默覆盖；
- 长任务复用 export queue/job API，并可取消；
- 禁止 UI 线程内 GPU 操作，脚本只能提交任务/等待状态；
- 对 AI 生成但由用户运行的脚本，在编辑器中清楚标记为“未受信任”，显示权限清单与 diff。

## 9. FCPXML 与其他交换格式的兼容设计

### 9.1 不要实现“通用 XML”

没有一个 XML 格式可同时无损表达所有 NLE 的效果、混音、调色、字幕、嵌套、速度曲线和媒体重连规则。正确做法是：

```text
Topos canonical timeline
       ├── FCPXML adapter（首批 XML）
       ├── OTIO adapter（第二批，非 XML）
       ├── CSV/BWF/stems/SRT adapters（已有/渐进）
       └── future AAF adapter（最后，复杂且二进制）
```

FCPXML 也不能与“旧 Final Cut Pro XML”混为一谈；两者独立建 adapter 和 fixture，接口使用明确名称，例如 `fcpxml`、`fcp7_xml`，不能统称 `xml`。

### 9.2 adapter 合同

每个 adapter 必须实现概念上如下接口：

```python
class InterchangeAdapter(Protocol):
    format_id: str
    def inspect(self, source: InterchangeSource) -> InterchangeInspection: ...
    def import_to_canonical(self, source: InterchangeSource, options: ImportOptions) -> ImportResult: ...
    def export_from_canonical(self, context: ExportContext, options: ExportOptions) -> ExportResult: ...
```

`ImportResult` 至少含：候选 `EditProject`/`TimelineSequence`、asset relink 请求、warning/error、unsupported element 清单、provenance。导入同样必须以 preview/confirmation 的方式 commit。

### 9.3 FCPXML 首版范围

**必须支持**：

- project/library/event 中的基本 timeline；
- format（尺寸、帧率）、资源 asset、asset clip、gap；
- video/audio track 顺序与 clip placement；
- source in/out、名称、role、基础启用状态；
- 简单 cross dissolve（能精确映射的才支持）；
- marker；
- 基础字幕的可映射文本和时长；
- 媒体 URL/相对路径、reel/时码等可用元数据；
- 导入/导出的 version、原始文件名、警告和 media mapping report。

**首版明确降级或拒绝**：

- 任意第三方 effect、复杂速度曲线、audition、multicam；
- compound clip 的任意嵌套（可在第二阶段映射为 nested sequence）；
- 完整 color correction、LUT/ColorStack、Composite Graph；
- 精细音频 routing、bus、sidechain、automation；
- 尚无双向字符/复杂样式映射保证的字幕；
- generator、标题模板、素材内 effect 参数。

对于 Topos 特有数据，可在可选 `topos:` XML namespace 中输出扩展，包含 stable ID、ColorStack/Composite reference 或 audit reference。默认导出应关闭该扩展以提高其他软件兼容性；启用后报告“外部软件可能忽略此信息”。

### 9.4 资产重连与失真报告

- 导出时使用 URI/path escaping，路径可配置为绝对、相对、copy-to-package；绝不悄悄改变 source range。
- 导入时先生成 asset mapping 表：external resource → Topos `MediaAsset`；同路径/同 fingerprint 自动匹配，歧义交给用户。
- 每次导入/导出输出 `InterchangeReport`：`preserved`、`converted`、`dropped`、`requires_user_action` 四类条目。
- “无损”只能在明确的功能子集里承诺。每个 adapter 以 round-trip fixture 定义自己支持的子集。

### 9.5 OTIO 的位置

OTIO 适合自动化 pipeline 的 timeline interchange。它应作为**可选适配器**：用来和其他 Python/DCC 工具交换 clip/track/marker/transition，不替代 FCPXML 的 NLE 交接，不把 OTIO 的 schema 作为 Topos 内部模型。

## 10. 推荐代码结构（目标态）

为避免污染现有渲染与 UI，建议新增以下模块；名称可在实现前统一，但职责不要改变。

```text
src/features/edit/
├── ai_editing/
│   ├── __init__.py
│   ├── models.py              # TAEP dataclass / typed request-response
│   ├── schema.py              # JSON Schema、protocol migration
│   ├── capability_manifest.py # 从 registry/feature flag 构建能力表
│   ├── snapshot_service.py    # 脱敏、分页、按 detail level 读取工程
│   ├── editorial_models.py    # EditorialBrief、StoryOutline、EditVariant、EditDecision
│   ├── brief_service.py       # brief patch、硬/软约束、用户锁定与版本关系
│   ├── semantic_index.py      # SemanticSegment 查询；不直接依赖某个模型 provider
│   ├── variant_planner.py     # 证据 → 多方案 + 可解释评分；输出计划但不提交
│   ├── decision_service.py    # 接受/拒绝/用户 override，回灌规划约束
│   ├── analysis_models.py     # AssetAnalysis、fingerprint、provenance
│   ├── analysis_service.py    # scene cut/transcript/provider job 契约
│   ├── plan_validator.py      # schema + domain preflight + diagnostics
│   ├── plan_compiler.py       # TAEP operation → EditCommand/MacroCommand
│   ├── transaction_service.py # revision、preview、commit、undo/audit
│   ├── tool_facade.py         # App/IPC 对外稳定 facade，不能放 UI
│   ├── jobs.py                # preview/export 的 job handle 和取消
│   └── audit_store.py         # 可选 .topos AI payload
├── adapters/
│   ├── fcpxml_adapter.py      # FCPXML adapter（不要改写现有 interchange.py）
│   ├── otio_adapter.py        # 可选第二阶段
│   └── interchange_types.py   # Import/Export/Report 共用类型
└── core/
    ├── project.py             # 仅增加 revision/provenance 的最小字段
    ├── timeline_v2.py         # 仅在领域能力已成熟时增加模型字段
    └── commands.py            # 新增 operation 对应的纯数据 Command
```

现有 `src/features/edit/core/interchange.py` 目前以音频交换能力和格式评估为主。第一阶段不建议把它大规模改名为 package；只从新 `adapters/` 调用它的 format registry 或渐进迁移，避免无关重构。

`tool_facade.py` 是唯一允许被未来 MCP/HTTP/WebSocket/本地插件调用的入口。`edit_controller.py` 与 UI 只订阅结果、触发确认和刷新，不包含协议解析、XML parser 或 renderer 逻辑。

## 11. AI 编辑助手：产品、UI 与实施规格

### 11.1 产品定位与边界

AI 编辑助手是 Edit 产品内的一个**可停靠工作区**，不是普通聊天机器人，也不是自动点击 UI 的宏录制器。它负责把自然语言需求变成可审查的 `EditorialBrief → EditVariant → EditPlan`；时间线、媒体池、预览器和 Deliver 仍是确定性执行和人工精修界面。

首版将它注册为 Edit/Studio 可用的右侧 `QDockWidget`，而不是新建独立产品页。这样用户可以同时看到媒体池、时间线、Program Viewer 与 AI 的方案/证据。只有在功能成熟且需要沉浸式评审时，才增加全屏 AI Workspace 页面。

助手必须做到：

- 可用自然语言表达需求，但所有写入均转换为结构化对象；
- 只查询用户当前项目和已授权的媒体/音乐库；
- 至少给出两个可比较的方案，而非无解释地改一条时间线；
- 显示每个关键选择的证据、备选项、约束和影响范围；
- 只在用户批准后写入时间线，且写入是一个可撤销的 `MacroCommand`；
- 允许用户在 timeline 手工精修，并把精修保留为下一轮 AI 的 `UserOverride`；
- 长时间模型、分析、预览和导出任务均异步、可取消、可恢复，绝不阻塞 Qt UI 线程。

助手明确**不做**：读取项目源码、执行模型生成的 Python、任意访问文件系统、从未知网站下载音乐、生成/注入 GLSL，或未经确认覆盖时间线和导出文件。

### 11.2 UI 信息架构与面板布局

默认 dock 建议宽度为 420–520 px，可浮动/可标签化；最小宽度 360 px。窄宽度时，Brief、Variants、Decisions 和 Jobs 改为内部 tab，不隐藏审批状态或错误。

```text
┌──────────────────────── AI 编辑助手 ────────────────────────┐
│ [模型 Profile ▾] [本地/云端状态] [隐私]              [⚙ 设置] │
├──────────────────────────────────────────────────────────────┤
│ Brief                                                         │
│  30 秒新品竖版；快速、有活力；必须包含快充卖点。               │
│  [编辑 Brief]  平台: 9:16  时长: 27–33 秒  约束: 3             │
├──────────────────────────────────────────────────────────────┤
│ 对话与任务                                                     │
│  用户：做一个适合抖音的新品宣传片                              │
│  助手：找到 14 个候选镜头；缺少“快充”素材确认。                 │
│  [输入需求或反馈…                              ] [发送/停止] │
├──────────────────────────────────────────────────────────────┤
│ 上下文摘要  素材 18 | 已分析 12 | 音乐 4 | 已锁定 2             │
│ [查看候选素材] [重新分析] [选择素材范围]                       │
├──────────────────────────────────────────────────────────────┤
│ 方案 A 口播优先      29.8 s     约束 ✓     [预览] [比较]       │
│ 方案 B 产品镜头优先  30.1 s     约束 ✓     [预览] [采用…]      │
├──────────────────────────────────────────────────────────────┤
│ 决策检查器                                                     │
│ ✓ Hook：产品反应镜头（证据：seg-12） [锁定] [替换]             │
│ ? 音乐：Fast Pop 01（已授权）             [试听] [拒绝]        │
│ ! 字幕：第 4 句阅读速度偏快                 [修复建议]          │
├──────────────────────────────────────────────────────────────┤
│ [查看 Timeline Diff]  [应用方案…]  [生成 10 秒预览]            │
│ Job: 空闲 / 进度 / 可取消；导出请求只在此处预检，不直接写文件   │
└──────────────────────────────────────────────────────────────┘
```

面板的对话区是“对意图和决策的可读记录”，不是直接控制层：每一条 assistant 消息必须关联 `brief_patch`、`analysis_ref`、`variant_id`、`decision_id` 或 `job_id` 中至少一个。纯文本不能自行触发 commit。

### 11.3 面板子组件与职责

| 子组件 | 用户行为 | 只读/写入边界 | 关键状态 |
|---|---|---|---|
| `AiAssistantHeader` | 切换模型 profile、查看连接/隐私状态、打开设置 | profile 切换只影响后续任务；不修改项目 | active profile、online/local、capabilities hash |
| `EditorialBriefEditor` | 填写目标、时长、平台、受众、语气、硬约束、锁定内容 | 产生待批准 brief patch；批准后版本化保存 | draft/dirty/valid/approved |
| `AiConversationView` | 输入需求、追问、取消当前请求、查看结构化回答 | 发出 `AssistantRequest`；禁止直接调用 timeline | session id、message refs、stream state |
| `ContextSummaryPanel` | 查看/选择素材、分析、音乐、用户锁定范围 | 写“上下文选择”与 brief，不写 timeline | snapshot revision、analysis freshness |
| `VariantListPanel` | 比较 A/B/C 方案、设为候选、请求重规划 | 只更新 variant/decision 状态 | proposed/selected/stale/failed |
| `DecisionInspector` | 查看证据、听音频、锁定/拒绝/替换建议 | 写 `EditDecision` 或 `UserOverride`，不直接 edit clip | proposed/accepted/rejected/locked |
| `TimelineDiffPanel` | 查看新增/删除/移动、素材和风险 | 只读 proposed diff；“应用”交由 controller 的审批流程 | base/current revision、validation report |
| `AiJobPanel` | 查看模型、分析、预览、导出任务，取消/重试 | 只经 job service 控制任务 | queued/running/cancelling/succeeded/failed |

每个 widget 只能发出细粒度 PyQt signal，例如 `brief_patch_requested(BriefPatch)`、`variant_selected(str)`、`apply_requested(str)`。它们不得导入 `TimelineSequence` renderer、`TimelineExporter`、controller 私有字段或 ModernGL 对象；状态回填时使用 `_updating` 锁，避免信号递归。

### 11.4 模型 Profile 与凭据设计

模型设置不应散在聊天面板、工程文件或脚本中。新增“Preferences → AI Editing”设置页，面板仅选择当前 profile。

`AiProviderProfile` 建议字段：

```json
{
  "profile_id": "local-qwen-video",
  "display_name": "本地剪辑模型",
  "provider_kind": "local_http",
  "endpoint": "http://127.0.0.1:...",
  "model_id": "...",
  "credential_ref": "keychain:topos:local-qwen-video",
  "modalities": ["text", "tool_call"],
  "data_policy": {"execution": "local", "allow_media_upload": false, "retain_prompts": false},
  "limits": {"max_context_tokens": 32000, "max_parallel_jobs": 1, "monthly_cost_alert": null}
}
```

实施规则：

- API key/令牌只保存到系统 keychain/credential store；工程与 audit 只保存 `credential_ref` 或 profile ID，绝不保存 secret。
- profile 配置完成后必须运行“测试连接 + 拉取 capability”任务；失败不影响编辑器本身可用。
- 仅当 profile 声明了对应 modality 且用户同意数据策略时，才允许传输转录、缩略图或代理媒体。首版只传递结构化文本/metadata，不上传原始媒体。
- 每个模型请求记录 profile/model/version、输入摘要 hash、工具调用和成本/令牌估算；默认不保存完整 prompt 和回包文本。
- Provider client 必须是可替换 adapter，UI 只依赖 `AiAssistantService`，不得依赖任何具体 SDK。

### 11.5 会话状态机与并发规则

一个 project 同一时刻只允许一个会影响同一 timeline 的**提交会话**，但可以有只读分析/预览 job。面板须显式呈现状态，不能用一个永久旋转的“思考中”。

```text
IDLE
  → GATHERING_CONTEXT → AWAITING_BRIEF_APPROVAL → PLANNING
  → VARIANTS_READY → AWAITING_VARIANT_APPROVAL → VALIDATING
  → AWAITING_APPLY_APPROVAL → APPLYING → PREVIEW_RENDERING → IDLE
                  ↘ STALE / FAILED / CANCELLED
```

- `GATHERING_CONTEXT`：读取 capability、brief、snapshot 和已授权分析；任何读取失败给可定位 warning。
- `PLANNING`：模型只可调用只读 tools 与 proposal tools；取消后丢弃不完整 stream，保留已完成的诊断。
- `VARIANTS_READY`：用户可选择、比较、锁定、拒绝或要求“更短/更轻快”等 brief patch；不得自动 commit。
- `VALIDATING`：固定 base revision，编译并模拟 plan；期间用户改变 timeline 则转为 `STALE`。
- `APPLYING`：获取一次性 approval token，原子 commit；成功后刷新 snapshot/renderer，失败则无状态变化。
- `PREVIEW_RENDERING`：仅运行短范围、低质量 preset 的 job；不影响当前可继续编辑的 timeline。
- `FAILED`：保留错误 code、可重试输入和建议，但不会把失败回复变成可执行 plan。

### 11.6 从用户需求到自动剪辑的逐步流程

以下是必须实现的端到端流程；每一步都应在 UI 显示明确结果，不能暗中跳过。

1. **打开或创建会话**：读取当前 `project_id`、active timeline、revision、selected media/range、active profile 和已有 brief；若项目为空，显示“先导入素材”，不调用模型。
2. **提交需求**：用户输入自然语言，例如“做 30 秒快节奏竖版新品宣传”；面板创建不可变 `AssistantRequest`，包含输入文本、用户选择的素材范围、brief revision、profile ID，且不含绝对路径/secret。
3. **澄清并产生 Brief Patch**：服务优先把需求转为结构化 brief patch；若缺少会改变结果的关键信息（时长、平台、必须包含内容、音乐授权），以最多 3 个明确问题或合理默认值呈现。用户确认后才保存新的 brief revision。
4. **收集上下文**：`ContextSummaryPanel` 调用 snapshot/semantic index，显示候选素材、转录覆盖、镜头、已锁定内容和可用音乐；用户可排除素材或手动加到“必须考虑”。
5. **分析缺口**：若没有转录/切分/音乐 metadata，提示用户启动相应分析 job；首版允许用户继续用现有 metadata 规划，不能假装“已理解视频”。
6. **生成故事结构**：planner 先返回 `StoryOutline`：Hook、核心信息、B-roll、CTA 等 beat 的目标时长与 evidence，供用户先审查；此时尚无 timeline 改动。
7. **生成多个 Variant**：模型/规则引擎根据 outline 生成至少两个 variant；每个都附时长、hard constraint 状态、软评分、关键 decision、备选素材和预估操作数。
8. **处理音乐、字幕、转场和效果**：
   - 音乐只能从媒体池内 `music` bin 或已授权 catalog 的 `MediaAsset` 中选择；必须显示授权/来源、时长、BPM（若有）和将要使用的范围。
   - AI 只能提出 `MusicDecision`、audio gain/ducking proposal；未实现正式 audio command 的能力必须显示“不支持”，不能伪造已添加音乐。
   - 字幕由已存在的 transcript/用户文本生成 `SubtitleClip` proposal；文字、语言和样式 preset 可编辑，未验证的转录须标记。
   - 转场、滤镜和调色只允许引用 capability manifest 中的 `TransitionType`、`EffectDefinition` 或受版本控制的 preset；禁止自由生成 effect 名和 GLSL。
9. **比较与人工反馈**：用户在 Variant/Decision 面板试听音乐、预览 source range、锁定某段、拒绝建议或输入反馈。反馈转为 `UserOverride`/BriefPatch 后，旧 variant 标记为 stale，重新规划必须保留锁定。
10. **校验并显示 Timeline Diff**：对用户选定方案运行 `validate_edit_plan`；面板按轨展示新增 clip、裁切、字幕、音乐、转场、效果、总时长、离线素材、预检警告和将删除内容。存在 error 时禁用“应用方案”。
11. **显式应用**：用户点“应用方案”，确认对话框显示影响摘要与撤销说明；controller 向 transaction service 提供一次性 token。服务通过 `MacroCommand` 提交，更新 project/timeline 两层状态并刷新 viewer；成功后记录 audit。
12. **预览与迭代**：用户点“生成 10 秒预览”或选择区间，创建 preview job；完成后可在 Program Viewer/面板播放。用户输入“开头再快一点”时，只为当前 variant 创建新的 brief patch 和后继 variant，旧方案仍可比较。
13. **导出请求**：用户明确选择 export preset 与输出位置；助手展示 `TimelineExportConfig`、预检和覆盖风险。只有 final confirmation 后才进入现有 export queue。

### 11.7 音乐、文字、转场与滤镜的专属策略

这些功能很吸引人，但也是最容易做出“看似自动、实际不可交付”的部分。首版的策略必须保守。

| 类别 | 首版可做 | 必须显示/校验 | 后续再做 |
|---|---|---|---|
| 音乐 | 从已导入且已授权的本地音乐素材中提议 1–3 首，并创建音频 clip proposal | 授权/来源、时长、BPM/情绪 metadata（缺失则说明）、响度和覆盖范围 | 云曲库搜索、自动生成音乐、复杂 beat matching |
| 语音/ducking | 提议目标响度和 ducking rule | 是否已有对应 audio command/renderer 支持；不支持则只输出建议 | sidechain 自动化、逐句包络 |
| 字幕 | 由转录或用户文本产生可编辑 `SubtitleClip` proposal 与现有 style preset | 语言、未确认转录、阅读速度、字体/导出 preflight | 翻译、多语种多版本、逐词字幕 |
| 转场 | 仅推荐当前 registry 支持的转场 | 两 clip 邻接性、时长、与视频 track 的兼容性 | 内容感知转场、生成式过渡 |
| 滤镜/调色 | 只引用已注册 effect 或命名 preset；必要时在 diff 展示参数 | effect capability、参数范围、export preflight、HDR 约束 | 模型自由调参、生成 GLSL、复杂调色自动匹配 |

### 11.8 文件级集成点

建议新增而非向现有巨大 UI/controller 类继续堆逻辑：

```text
src/features/edit/ai_editing/
├── assistant_service.py        # 会话状态机、request 编排、provider/tool 调用
├── provider_profiles.py        # 非 secret profile；credential store adapter 接口
├── assistant_events.py         # typed UI event/result/state，不传 Qt object
├── assistant_session_store.py  # 可选 project-local brief/variant/decision 持久化
└── music_policy.py             # 授权素材筛选与 AI 可用音乐清单

src/features/edit/ui/
├── ai_edit_assistant_panel.py  # 主 dock，QSplitter/tab、只发 signal
├── ai_brief_editor.py          # Brief 表单与硬/软约束编辑
├── ai_context_summary.py       # 素材/分析/音乐 context
├── ai_variant_list.py          # variant card 与对比
├── ai_decision_inspector.py    # evidence、试听、锁定/拒绝
├── ai_timeline_diff.py         # 非编辑性 diff 视图与审批入口
└── ai_job_panel.py             # job 进度、取消、重试

src/app/
├── controllers/ai_edit_controller.py  # 把 UI signal 映射到 service；同步既有 EditController
└── ui/preferences_dialog.py            # 挂载 AI Editing preferences page；不保存 secret
```

需要修改的现有接线点：

- `src/features/edit/module.py`：注册 `PanelDescriptor(panel_id="edit.ai_assistant", dock_attr="dock_ai_edit_assistant")`，仅 Edit/Studio feature enabled 时可见。
- `src/app/main_window.py`：创建 `dock_ai_edit_assistant`，设置默认右侧停靠与持久化布局；只负责装配，不持有 AI session 业务状态。
- `src/app/controllers/edit_controller.py`：暴露明确的 service bridge，用于提交成功后同步 `_current_timeline_sequences`、媒体池和 renderer snapshot；不要在这里解析模型输出。
- `src/features/edit/commands_facade.py`：仅在实际需要时增加 AI application service 的轻量 facade，避免 app 层对 core/AI internals 深度导入。
- `src/features/edit/locales/zh_CN.json` 与 `en_US.json`：新增完整 i18n key；不可把中文字符串散写在 widget 内。

### 11.9 AI 编辑助手的交付阶段

这些步骤属于 UI/产品实施流，可与第 12 章的底层协议 phase 并行，但每一步都依赖前一项验收。

#### UI-0：空壳、注册与状态契约

1. 建立 `assistant_events.py` 的强类型状态/事件；禁止 `dict[str, Any]` 在 UI 与 service 间无限传播。
2. 建立 `AiEditAssistantPanel`、QDockWidget、PanelDescriptor 和 Window 菜单可见性。
3. 使用 fake `AiAssistantService` 显示 idle/loading/error/empty project 状态，不接任何模型。
4. 为 dock layout、feature flag、i18n、禁用 Edit 产品场景写 UI 测试。

**验收**：Topos Edit/Studio 可显示、隐藏、浮动和重启恢复面板布局；Color/Composite 产品不出现无效入口；无项目时提示导入素材。

#### UI-1：模型 Profile 与隐私设置

1. 在 Preferences 新增 AI Editing 页面和 `AiProviderProfile` 列表：新建、编辑、删除、启用、测试连接。
2. 接入 credential-store abstraction；测试用 fake store，生产使用系统 keychain；界面永不回显 token。
3. Header 显示 profile、local/cloud、连接状态、是否允许上传分析数据；未配置 profile 时只允许编辑 Brief 和查看 mock/demo。
4. 加入连接超时、不可用、额度/成本警告、离线 fallback 的明确文案。

**验收**：profile 变更不会写入 `.topos` secret；测试连接失败不会造成 UI 卡死；用户能在请求前准确看到数据是否离开本机。

#### UI-2：Brief 与上下文选择

1. 实现 `EditorialBriefEditor`：目标、平台 preset、目标/最小/最大时长、受众、语气、必要 beat、禁用内容、可用音乐策略。
2. 将自然语言首条需求先变为 draft brief patch，用户一键批准/编辑后生成 brief revision。
3. 实现 `ContextSummaryPanel`：素材数量、分析新鲜度、选中/排除/锁定 source range、音乐可用性。
4. 提供“缺少分析”任务入口，但不强制云分析；所有手动选择都转为 brief/override。

**验收**：用户能完全不接模型地创建 Brief、选择素材与锁定镜头；修改 Brief 只让 variant 过期，不破坏已提交 timeline。

#### UI-3：对话、规划与方案比较

1. `AiConversationView` 将用户输入封装为 `AssistantRequest`，显示状态、取消和结构化 message reference。
2. 接入 `assistant_service → tool_facade → variant_planner`；先使用 deterministic fake planner 建 E2E 测试，再接真实 provider。
3. 实现 `VariantListPanel`：最多先显示 3 个方案，比较时长、beat coverage、约束、音乐/字幕/效果摘要和关键差异。
4. 实现 `DecisionInspector`：点击 evidence 可定位 source viewer/timeline range；接受、拒绝、锁定、替换都写入 decision service。

**验收**：同一 brief 至少能生成 A/B 两个方案；每个关键选段能回到素材证据；锁定/拒绝后重规划不会重新选回被拒绝素材。

#### UI-4：Diff、审批和时间线提交

1. 实现 `AiTimelineDiffPanel`，按视频/音频/字幕轨显示 operation；特殊高风险项（delete、替换、音乐版权、降级 effect）置顶。
2. “应用方案”前强制 `validate_edit_plan`，并在 base revision 改变时切换到 stale，禁止提交。
3. 确认 dialog 显示 undo group 名、影响 clip 数、删除数、范围、预检 warning；用户确认后传一次性 approval token。
4. controller 仅订阅 transaction result，复用现有 Edit timeline 更新/预览刷新路径；失败则保留原 timeline 不变。

**验收**：AI 方案不会在未确认时改变时间线；提交后一次 undo 恢复；用户在 review 期间手动改 timeline 后，旧方案无法误提交。

#### UI-5：音乐、字幕、预览与导出请求

1. 增加授权音乐素材卡、试听、拒绝与替换；未授权/未知来源音乐没有“应用”按钮。
2. 字幕卡展示语言、来源、阅读速度、字体与可编辑文本；使用既有 caption command 和 export preflight。
3. 增加短 preview preset，job 进度、取消、失败和 artifact 打开入口；预览请求不与最终 export 混用。
4. 导出面板只创建 export request，复用 Deliver preflight/queue；最终输出路径和覆盖操作必须再次确认。

**验收**：选定音乐、字幕、转场、effect 都出现在 diff 和 timeline；预览/导出异常可取消且 UI 仍响应；没有 approval 无法写出视频。

#### UI-6：可靠性、可访问性与产品化

1. 实现会话恢复：保存 brief、variant 摘要、decision、audit refs；默认不保存秘密和完整云对话。
2. 完善键盘导航、焦点顺序、屏幕阅读器 label、高对比状态、长文本/中文换行、错误可复制。
3. 建立 telemetry 的 opt-in 设计，只记录匿名成功/失败/耗时；不上传素材、转录、prompt 或路径。
4. 用真实项目 fixture 压测长转录、1000 clip snapshot、反复 cancel/replan、项目切换/关闭时 job 清理。

**验收**：断网、provider 崩溃、项目关闭、任务取消和恢复均无死锁、无未释放 worker、无错误提交；用户能清楚解释“AI 做了什么、依据什么、还没做什么”。

## 12. 详细分阶段实施计划

下面的顺序刻意先完成“可验证的 AI 创作闭环”，再接模型、复杂分析和 XML。每个 phase 都遵守同一条单向链路：**事实/用户约束 → 方案/决策 → 确定性时间线 → 预览与交付**。不要让任一 AI provider 直接跨层写时间线。

### Phase 0 — 架构封口与基线测试（P0）

**目标**：在不接入 AI 的情况下，明确唯一时间线提交入口与可测试的导出基线。

- 盘点并记录：`TimelineView` command stack、`EditController._current_timeline_sequences`、`EditPage/TimelinePanel`、脚本、导入、场景切分各自是如何提交 `TimelineSequence` 的。
- 提取/统一一个无 UI 的应用服务级 `EditProjectService.commit_timeline_change(...)`；后续 UI、AI、脚本、导入均从此路径进入。该服务必须同时更新轻量 `EditProject.timelines` ref、完整 sequence map、dirty/revision、媒体池和 renderer snapshot。
- 将 `timeline_ops.validate_timeline(sequence)` 纳入提交前的底层校验，再叠加协议层的引用、权限和能力校验。
- 给 `EditProject`/timeline 引入单调 revision（可先放到 project payload），保存、重开、undo/redo 的 revision 语义写成测试。
- 确认 `TimelineExporter` 可在没有打开 Edit/Color/Composite UI 的条件下执行当前支持的普通 timeline 导出。
- 在 `docs/` 固化本文件里的时间单位、半开区间与线程边界约定。

**涉及文件**：`project.py`、`timeline_v2.py`、`timeline_ops.py`、`commands.py`、`ui/timeline_view.py`、`edit_controller.py`、`edit_project_io.py`、`timeline_export.py`。

**验收**：同一条 UI 命令、测试命令和服务提交路径生成相同 sequence；一次 atomic change 只产生一次 undo；脏状态、保存、重开一致。

### Phase 1 — AI-native 创作模型、TAEP 只读合同与校验器（P1）

**目标**：先让“创作意图、素材事实、方案决策、时间线交付”成为明确对象；让一个 JSON plan 在内存中可解析、可诊断、可模拟，但不修改实际工程。

- 新建 `ai_editing/editorial_models.py`：带完整 type hints 的 `EditorialBrief`、`HardConstraint`、`SoftPreference`、`StoryOutline`、`EditVariant`、`EditDecision`、`UserOverride` 与 `SemanticSegment`。其中所有 source/timeline range 必须复用本文件第 4 章的整帧合同。
- 新建 `brief_service.py`：支持 revision、锁定内容、硬/软约束、平台 deliverable；用户修改 brief 只能使 variant 过期，不能静默改掉 timeline。
- 新建 `ai_editing/models.py`：带完整 type hints 的 `EditPlan`、operation union、`Diagnostic`、`ValidationReport`、`ProjectSnapshot`、`CapabilityManifest`，并在 plan 中加入 `brief_id`、`variant_id`、`decision_refs`、`analysis_snapshot_hash`。
- 新建 `schema.py`：JSON Schema 文件/生成器、version negotiation、future/unknown field policy（默认拒绝写操作中的未知 core 字段）。
- 新建 `plan_validator.py`：先 schema，后引用、范围、类型、重叠、effect registry、字幕锁、revision 等 domain validation。
- 新建 `capability_manifest.py`，从实际 `EffectRegistry`、transition 定义、feature flags、export codec 构造 manifest。
- 新建 `snapshot_service.py` 与 `semantic_index.py`，实现 detail level、分页、路径脱敏、project revision、基于用户标签/已有 scene-cut 的 source-range 查询；P1 不依赖云模型。
- 给诊断码建立枚举、文案模板和中英文翻译入口。

**禁止**：这个阶段不要接 LLM SDK，不要写 UI，不要生成 XML，不要调用 GPU；也不要把“模型偏好分数”写入 canonical timeline。

**验收 fixture**：合法 brief、互相冲突的 hard constraint、用户锁定片段、variant 失效、合法单 clip、track overlap、离线素材、source 越界、未知 effect、过期 revision、锁定字幕、24/23.976/29.97 的帧边界。

### Phase 2 — 多方案规划、Plan compiler、事务与撤销（P2）

**目标**：让 AI 或规则引擎能基于 brief 和证据产生多个可比较方案，并将被选方案无损编译到现有 command 系统。

- 新建 `variant_planner.py`：输入 `EditorialBrief + SemanticSegment + 用户锁定`，输出多个 `StoryOutline + EditDecision + EditPlan`；它只生成 proposal，不能 commit。
- 新建 `decision_service.py`：用户可接受/拒绝某条 decision，或将 timeline 手改记录为 `UserOverride`；下一轮规划必须读取这些状态。
- 定义透明评分：hard constraints 只能 pass/fail；duration fit、beat coverage、pace 等只用于排序，并必须保留 evidence 与 alternatives。
- 在 `plan_compiler.py` 为每个白名单 operation 创建相应 `EditCommand`；缺少的领域命令先补纯数据 command，再开放 operation。
- 使用 `MacroCommand` 把整个 plan 变成一个命名 undo group；模拟和正式提交必须复用同一 compiler。
- `transaction_service.py` 实现 `validate_preview`、`commit`、revision compare-and-swap、rollback、audit record。
- 输出结构化 `TimelineDiff`：新增/删除/移动、变更前后时间、影响轨、总时长、warning，以及 `decision → clip/source range` 的可追溯映射。
- 加入操作数、总影响 clip、总时长、递归深度等配额，防止模型一次生成破坏性大计划。

**验收**：至少两个 variant 能被逐项比较；用户拒绝/锁定会影响下次规划；preview 的 proposed timeline 与 commit 后结果 byte/canonical-equal；任一中间 operation 无效不会有部分提交；undo/redo 往返恢复；并发用户修改正确触发 conflict。

### Phase 3 — 应用服务与本地脚本 API（P3）

**目标**：先让人类脚本和内部测试安全使用协议，再暴露外部模型工具。

- 新建 `ToposEditAPI`/`EditSession`，只暴露 snapshot、validate、apply、job 查询等高层 API。
- 将 `src/features/scripting/api.py` 的编辑相关能力与现有只面向调色的 `ToposAPI` 分开；不在已有 API 中塞大量时间线内部对象。
- 确保所有跨线程调用通过现有 `BlockingBridge` 或明确服务任务；禁止从脚本直接触摸 Qt widget/GL。
- Scripts UI 增加“加载 TAEP plan”“预览 diff”“本地批准执行”的路径，但先不支持在线模型。
- 制定路径授权和覆盖输出的确认 token 机制。

**验收**：受信任 Python 示例可以加载 plan、dry-run、显示诊断、commit、undo、生成导出请求；恶意/意外 plan 无法访问任意文件或 `exec` 新代码。

### Phase 4 — 预览/导出 Job 与 Deliver 门禁（P4）

**目标**：AI 能安全地看到结果和请求成片，但绝不阻塞 UI 或绕过 preflight。

- 为 `TimelineExporter` 封装 job contract：job ID、进度、日志、取消、result manifest、错误 code。
- `request_export` 将 TAEP export request 映射到 `TimelineExportConfig`，调用已有 audio/video/font preflight。
- 预览使用短范围、代理/低质量 preset；最终 deliver 使用明确 preset，且只在确认后写文件。
- 导出前展示：范围、分辨率、fps、codec、音频、字幕、素材离线、effect degradation、预计文件大小和覆盖风险。
- 产物记录到 audit/export manifest，包含 project/timeline revision 和 asset fingerprint 摘要。

**验收**：用户取消、编码器失败、effect preflight 失败、离线素材、字体缺失均不会产出“看似成功”的文件；UI 在任务运行时可继续响应。

### Phase 5 — 素材分析和“AI 辅助而非 AI 猜测”（P5）

**目标**：让模型获得足够语义上下文去剪，而不读取视频文件或源码。

- 复用 `scene_cut` 输出为镜头候选，建立以 fingerprint 为 key 的 `AssetAnalysis` 存储。
- 定义可插拔 `AnalysisProvider`：本地/云转写、镜头标注、OCR、人声/静音、响度、面部/物体标签。provider 不可直接写 timeline。
- 建立 async job、取消、重试、缓存失效与隐私策略；云 provider 上传必须单独用户确认。
- 将 transcript 的原始/修订文本、speaker、置信度、语言、来源和用户修订明确区分。
- 提供 `search_assets`、`get_asset_analysis` 的分页与范围查询，避免把数小时转录一次塞进模型上下文。
- 将分析 provider 输出转换为 `SemanticSegment` 后再进入 variant planner；provider 的原始标签、模型版本和置信度保留在 provenance 中，不能被规划器伪装成用户事实。

**验收**：素材变更后分析失效；同一转录可以产生带 evidence 的候选 variant/plan 但不自动 commit；用户修订不被重新分析覆盖；无网络环境不会阻塞本地剪辑。

### Phase 6 — AI 工具 host 与人机交互（P6）

**目标**：将 P1–P5 的本地服务封装给一个或多个模型供应商，而不是绑定某一家 SDK。

- 用 `tool_facade.py` 提供 transport-neutral 工具定义；可再加 MCP/HTTP adapter，但它们只负责认证和序列化。
- UI 增加 AI 编辑工作区：Brief 编辑、素材/分析引用、故事结构、variant 对比卡、每条 decision 的证据/备选项、timeline diff、影响范围、预览、批准/拒绝/锁定后重新规划。传统 timeline 保留为精修与最终裁决界面。
- 对 destructive operation 分级：添加/标记可较低风险；删除、批量替换、导出、云上传、覆盖文件必须确认。
- 对 prompt injection 做数据隔离：素材文件名、字幕文本、OCR/网页内容均视为不可信数据，不能改变工具权限或系统指令。
- 支持 audit/replay：用相同 snapshot、manifest、plan 重放到副本工程，便于 bug report。

**验收**：模型只凭 manifest、brief、snapshot 与分析可完成“按转录生成至少两个初剪方案 → 用户选择并锁定/拒绝决策 → commit → 10 秒预览 → 导出请求”；无法要求或获得源代码、shell、文件系统扩大权限。

### Phase 7 — FCPXML Import/Export（P7）

**目标**：在可控子集内完成外部 NLE 往返，不阻塞 TAEP 主线。

- 定义 `InterchangeAdapter` 共用类型与 `InterchangeReport`，新增 `fcpxml_adapter.py`。
- 先完成 FCPXML `inspect`：schema/version、资源列表、timeline、媒体路径、所有不支持特性清单；不直接导入。
- 再完成受支持子集到 canonical model 的 import preview，以及从 canonical model export。
- 通过 XML fixture（而非依赖用户机器上的 Final Cut）测试：24/25/23.976、gap、分轨、source offset、transition、marker、字幕、路径转义、离线重连。
- 输出 loss report；每个 fallback 都要求用户显式选“drop / bake / cancel”。

**验收**：定义的受支持子集可 `Topos → FCPXML → Topos` 且 clip placement/时长/引用一致；不支持项目不会静默丢信息。

### Phase 8 — OTIO、嵌套与高级交换（P8，按需）

**目标**：服务 pipeline 工作流，而不是抢在 FCPXML 前扩大范围。

- 在确认依赖和许可后添加可选 OTIO adapter；先做 read/write 时间线、track、clip、marker、transition 的共有子集。
- 完成 nested sequence 映射策略，明确 flatten/retain/reference 三种选择与可逆性。
- 评估 AAF；只有音频回交和用户需求足够明确时才投入。OMF 默认不实现。

### Phase 9 — 高级 AI 剪辑能力（P9，最后）

在 P0–P7 的可靠性通过后，再逐项开放：

- 基于文字的 edit、silence removal、filler-word proposal；
- 多版本/多平台自动重构图与 safe-area；
- 逐镜头 B-roll 建议与版权/素材来源标记；
- 自动字幕草稿、翻译分支、speaker diarization；
- 音频 ducking、响度目标与音乐节拍建议；
- 多机位选择、compound/nested sequence；
- ColorStack/Composite 的受限 preset 引用（先引用预设，不让模型生成任意 shader/node graph）。

每项都必须先扩展 canonical command/model、capability manifest、校验器、diff、审计和测试，最后才对模型开放。

## 13. 测试矩阵与质量门

### 13.1 单元测试

建议新增：

```text
tests/edit/ai_editing/
├── test_taep_schema.py
├── test_capability_manifest.py
├── test_snapshot_redaction.py
├── test_editorial_brief_service.py
├── test_semantic_index.py
├── test_variant_planner.py
├── test_decision_user_override.py
├── test_ai_provider_profiles.py
├── test_ai_assistant_state_machine.py
├── test_timebase_conversion.py
├── test_plan_validator.py
├── test_plan_compiler.py
├── test_transaction_atomicity.py
├── test_revision_conflict.py
├── test_ai_audit_store.py
├── test_export_request_preflight.py
├── test_fcpxml_inspect.py
├── test_fcpxml_import.py
├── test_fcpxml_export.py
└── fixtures/

tests/ui/
├── test_ai_edit_assistant_dock.py
├── test_ai_brief_editor.py
├── test_ai_variant_comparison.py
├── test_ai_decision_inspector.py
├── test_ai_timeline_diff_approval.py
└── test_ai_job_panel.py
```

### 13.2 必须覆盖的场景

| 类别 | 关键场景 |
|---|---|
| 时间 | 24、25、24000/1001、30000/1001；一小时边界；相邻 clip；floor/ceil；drop-frame 显示。 |
| 编辑 | 单轨/多轨、linked audio、track lock、ripple 未来实现前被拒绝、transition 相邻约束、单 MacroCommand undo。 |
| 素材 | 离线、路径重连、VFR/PTR index、短于请求范围、proxy 与原素材切换。 |
| 效果 | registry 存在/缺失、参数边界、不可导出 effect、未知字段。 |
| 字幕 | 字幕轨锁、cue 锁、Unicode/CJK、文本来源、导出/预览状态一致。 |
| 并发 | AI preview 后用户手动修改、两个 AI plan、脚本与 UI 同时提交。 |
| 安全 | 绝对路径脱敏、越权 tool 请求、超过配额、外部文本 prompt injection、无确认 export。 |
| 助手 UI | 空项目、无 profile、profile 断线、Brief draft、流式取消、variant stale、拒绝/锁定、审批 dialog、dock 浮动/恢复、中文长文本和键盘导航。 |
| 音乐 | 未授权素材不可应用、授权状态变更后 variant stale、试听失败、BPM 缺失只降级而不编造数据。 |
| 交换 | 合法 FCPXML、未知元素、资源缺失、转场、gap、路径空格/Unicode、loss report、round trip 子集。 |
| 运行 | 预览/导出 job 取消、编码失败、UI 不阻塞、GPU 操作不在 UI thread。 |

### 13.3 回归与黄金输出

- 对 canonical JSON 使用稳定排序/规范化 serializer，便于 diff；不依赖 UUID 随机值比较。
- 对少量短 fixture 导出视频做帧级/感知 hash 验证；字幕和转场应有 golden image。
- 对 FCPXML 使用 XML canonicalization 后比较，并额外用独立 parser 验证重读。
- 为 AI plan 建立“无模型”fixture：测试永远不依赖线上模型输出。
- 性能基准至少记录：100/500 operations 的 validate/compile 时间、1000 clip snapshot 大小、4K preview/export job 对 UI 响应的影响。

## 14. 风险、决策与不做事项

| 风险 | 后果 | 处理方式 |
|---|---|---|
| 建第二套 AI 时间线 | 预览、保存、导出和 XML 互相漂移 | 只把 TAEP 编译为 `TimelineSequence` + commands。 |
| 直接让 LLM 写 XML | 时间基、ID、素材路径、未知语义出错且难调试 | LLM 写 TAEP；XML 交给受测 adapter。 |
| 直接暴露 Python `exec` | 本机文件/网络/命令执行风险 | AI tools 与 trusted scripts 分离，最小权限与审批。 |
| 浮点秒做位置 | 23.976/29.97 长片累积漂移 | 内部 position 一律整帧 + Rational fps。 |
| UI/GPU 与 AI 逻辑耦合 | 卡 UI、线程问题、不可复现 | AI 层只有纯数据/任务；GPU 仍由 renderer 异步消费。 |
| 静默降级 XML | 外部交接错误却不易发现 | `InterchangeReport` 强制列出保留/转换/丢失/待处理。 |
| 一开始追求全自动成片 | 用户失去可控性、评估困难 | 先做候选计划、diff、预览与批准；自动 commit 是后续可配置策略。 |
| 将 AI 分数当真相 | 模型偏见或错误会变成不可质疑的剪辑规则 | hard constraint 与 soft score 分离；每个方案附 evidence、备选项和用户否决状态。 |
| 人工修改被重规划覆盖 | 编辑师失去控制并不再信任 AI | 将手工修改持久化为 `UserOverride`/锁定约束，重规划默认保留。 |

本计划**不包含**：让 AI 生成/注入 GLSL；替代现有播放架构；重写 `.topos` 格式；立即实现 AAF/OMF；把云模型账户/API key 写进工程；以 UI 录制宏代替 domain command。

## 15. 首个可交付版本定义（MVP）

MVP 不是“AI 自动剪整部片”，而是下面这个可靠、可解释的 AI-native 闭环：

1. 用户导入几段已完成 metadata/场景切分的素材，并填写一个可编辑 `EditorialBrief`（时长、平台、必要信息、风格与禁区）。
2. 模型读取 capability manifest、brief、简化 snapshot 与转录/镜头分析。
3. 模型先生成带 evidence 的 `StoryOutline`，再生成至少两个 `EditVariant`；每个方案都含显式 `EditDecision`、备选片段和仅用于排序的评分。
4. 用户在方案层选择版本，并可锁定片段、接受/拒绝决策；这些反馈成为下一轮规划的硬约束。
5. Topos 将所选 variant 的 TAEP plan 校验为 100% 可解释的 timeline diff，不改工程。
6. 用户点击批准，plan 作为一个 MacroCommand 提交，可一次 undo；手工精修回写为 `UserOverride`。
7. 用户获得低质量短预览；批准后按现有 `TimelineExportConfig` 执行最终导出。
8. brief、证据、方案、决策、timeline revision、导出预检和产物 manifest 可被审计。

这时，LLM 已完全不需要阅读项目源代码；它只需阅读动态工具定义、TAEP schema、brief、当前工程 snapshot 和素材分析结果。之后你再逐项扩充 canonical command、语义分析和 capability manifest，模型自然获得更强操作能力，而不会破坏工程架构或掩盖人工编辑意图。

## 16. 实施前的决策清单

开始 P0/P1 前，产品侧只需明确以下选择：

1. 首个 AI 目标是否锁定为“访谈/口播的初剪 + 字幕草稿”，而不是通用影视自动剪辑。
2. AI 运行位置：仅本地模型、可选云模型，还是两者；这决定分析数据与项目审计的隐私默认值。
3. 外部交接首选：FCPXML first；是否明确不支持旧 FCP7 XML，避免“XML”名称误导。
4. 自动化权限默认：建议所有 commit/export 都需要用户批准；以后再为本地受信任工作流增加 policy。
5. 项目内是否保存 AI 对话/提示词：建议默认关闭，仅保存 plan、revision、诊断和可复现必要的 hash。
