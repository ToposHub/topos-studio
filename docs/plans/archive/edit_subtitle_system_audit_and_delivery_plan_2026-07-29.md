# Edit 页面字幕系统审计与交付优化计划（2026-07-29）

## 1. 结论与范围

当前实现已经具备「手工建立字幕轨、创建/移动/修剪单个字幕、基础样式、节目监视器叠加、烧录到成片、SRT 导出」的最小闭环，但还不具备专业非编（NLE）中可用于真实节目交付的字幕工作流。最大风险并非单一渲染性能，而是字幕编辑绕过统一时间线状态提交流程、变换拖拽的撤销语义错误，以及属性编辑的高频全量重建。

本文件记录代码可验证的问题、可落地的实施计划，以及截至 2026-07-30 在 `fix-ui` 分支的复核状态。这里的「字幕」包含普通字幕、开放式字幕（burn-in/open captions）、可交换的文本轨和无障碍字幕；电视规范的闭合字幕（CEA-608/708）单列为后续能力，不能与普通 SRT 混为一谈。

### 审计范围

| 层面 | 已检查入口 |
|---|---|
| 数据/序列化 | `timeline_v2.py` 的 `SubtitleClip`、`EditTrack`、`TimelineSequence` |
| 命令与撤销 | `commands.py` 的 Add/Update/DeleteSubtitleClipCommand，`edit_controller.py` 的字幕处理器 |
| 时间线交互 | `subtitle_clip_item.py`、`timeline_view.py`、`timeline_layout.py`、`timeline_panel.py` |
| 预览与手柄 | `subtitle_renderer.py`、`program_viewer.py`、`subtitle_handle_controller.py`、`edit_page.py` |
| 属性编辑 | `edit_properties_panel.py` |
| 输出与交换 | `timeline_export.py`、`srt_exporter.py`、`deliver_controller.py` |
| 自动化验证 | 字幕相关测试、时间线回归测试、布局测试的收集状态 |

### 当前已验证的基础能力

| 能力 | 状态 | 证据/限制 |
|---|---|---|
| 独立字幕轨和不重叠的 cue | 有 | `EditTrack.subtitle_clips` 和 `with_subtitle_clip_*` |
| 单 cue 文本、字体、对齐、描边、阴影、2D 变换 | 有 | `SubtitleClip` + 字幕属性面板 |
| 时间线上的移动、左右修剪、删除 | 有基础实现 | 失败回滚、吸附、批量操作尚未健全 |
| 当前帧叠加预览 | 有 | Program Viewer QPainter overlay；已完成紧凑字形缓存优化 |
| 成片烧录 | 有 | `TimelineExporter._burn_in_subtitles()`；仍是 CPU 全画布合成 |
| SRT 导出 | 有 | 仅单向、扁平化的 `.srt` 导出 |
| SRT/VTT/TTML/ASS 导入、转写、质检、协作 | 无 | 全局搜索无解析器、导入入口、转写或质量规则 |

## 2. 审计证据与验证基线

1. 初始审计时运行 `tests/edit/test_timeline_command_regressions.py` 与 `tests/media/test_timeline.py`：**101 passed**；最新 `fix-ui` 复核结果见 2.1。
2. `tests/edit/test_timeline_view_layout.py` 在收集阶段被环境中缺失的可选依赖 `rawpy` 阻塞（`thumbnail_provider -> FileIO -> rawpy`），不是已执行后的布局断言失败；必须在 CI 测试环境补齐依赖或隔离可选 RAW 导入依赖。
3. 现有测试覆盖主要集中在「普通轨道变更不丢字幕」及少量布局断言。没有发现覆盖以下关键场景的专项测试：字幕命令到项目持久化、字幕 undo/redo、字幕导出正确性、字幕导入、手柄矩阵、错误回滚、批量编辑、可访问性与 4K 导出性能。

### 2.1 `fix-ui` 最新复核状态（2026-07-30）

截至 2026-07-30 本轮复核，验证命令覆盖字幕命令、拖拽、预览签名、增量时间线、面板 debounce、吸附、布局、字体预检、公式级矩阵一致性、Caption 数据模型/编辑模式/Workspace，共 **286 passed**（含 1000 cue Workspace 基准 **6 passed**）；时间线命令、媒体时间线与布局回归另有 **148 passed**，组合共 **434 passed**。`rawpy` 已改为可选依赖，不再因缺少 RAW 图片支持阻塞字幕/时间线 UI 测试收集。

完整 `tests/edit` 本轮收集 **3931** 项并运行到约 30% 时，在未改动的 `audio_waveform.py -> edit_page._set_thumbnail_playback()` Qt/原生路径发生 segmentation fault；这是进程级崩溃而非 Python 断言失败，不属于本轮字幕改动路径。字幕、Caption、时间线命令、媒体时间线和布局的上述定向组合已全部通过，但完整 Edit 套件仍需在稳定的 Qt/音频测试进程中分片复跑。

| 计划范围 | 状态 | 已交付实现 | 尚存边界 |
|---|---|---|---|
| Phase 0（SUB-P0-01~05） | 主体完成 | 统一命令提交、变换 session 的可逆 Undo/Redo、模型不变量和 patch 白名单、拖拽失败恢复/live clamp、完整预览签名。本轮补齐快速 move→release 竞态、字幕轨误装媒体 clip、sequence 轨道分组不变量和 cue/轨道锁命令保护。 | 保存/重开与 renderer 的端到端测试仍应扩展；不能仅凭命令单测标记 100% 完成。 |
| Phase 1.1/1.2/1.3/1.4/1.5/1.6 | 主体完成 | 字幕增量 diff、文本/颜色 transaction 与 debounce、可持久化 lane/滚动、snap/nudge/edit mode、60 Hz 拖拽合并、仿射公式 parity 测试。本轮补齐实际 drag/trim 对 cue 自身 snap 点的排除，以及多选 nudge 的预校验、单 macro 撤销和碰撞原子拒绝。 | preview/export 尚未真正消费同一个矩阵对象或 golden pixel；4K 实机 input-to-photon 仍需带 GL 环境验收。 |
| 字体可靠性（SUB-P1-04、P2-11 的首批） | 部分完成 | `FontResolver`、样式解析和 Deliver 字体 preflight 已接入并有测试。 | 尚未实现字体嵌入/打包、跨机器 glyph golden image 与交付 manifest。 |
| 导出性能（SUB-P1-03） | 未完成 | 预览已使用紧凑字形缓存；已建立公式级 transform parity。 | 4K burn-in 仍需 GPU texture pass 或 CPU 紧凑 ROI compositor；尚无 1/3/8 活动 cue 的吞吐/峰值内存门槛。 |
| 字幕轨语义（SUB-P1-09） | 部分完成 | 元数据支持语言/角色；本轮已从字幕 header 与属性面板移除无意义 Mute/Solo，统一为 Visible/Lock。 | `export_enabled`、Deliver 选择及 burn-in/sidecar 状态矩阵尚未实现。 |
| Phase 2A（P2-01/03/06/10/12） | 迁移基础/首版 | CaptionStyle、CaptionTrackMetadata、CaptionCueMetadata 迁移桥、schema migration、样式继承/override、只读 Caption Workspace、查找替换、split/merge 样式和 metadata 保留。本轮修复同 cue 多次 Replace All 的偏移错位。 | 尚无独立 CaptionCue/CaptionTrack 实体；Workspace 缺 inline 时码/文本/元数据编辑与样式预设集成，因此不能视为完整三层编辑模型。 |
| 原 Phase 2B/2C | 已重筛 | 仅 SRT、基础 QC、Deliver manifest、字体可复现性和按需导出优化保留。 | TTML/IMSC/ASS、转写/翻译、词级时间与复杂排版已移出当前计划。 |
| 原 Phase 3 | 已移出当前计划 | 无。 | CEA-608/708、OTT mux、动态模板和协作仅在明确客户需求下另行立项。 |

### 2.2 当前执行范围重筛（必须项与已移出项）

本轮不再以“追平所有专业 NLE 功能”为目标。以下清单只保留对现有编辑器的可靠字幕交付有直接价值、且不依赖特定客户/平台的工作；其余能力不再进入当前开发排期。

| 分类 | 必须保留的工作 | 为什么必须 | 完成判定 |
|---|---|---|---|
| 交付可靠性 | SRT 导入、按语言/轨道导出、固定的帧率与 rounding 规则 | 客户字幕资产必须能进入项目，并能可靠回交；这是日常编辑闭环的最低要求。 | SRT 导入→编辑→导出时码、文本、语言轨不丢失；23.976/29.97 边界正确。 |
| 基础质量控制 | 空文本、时长、重叠/间隙、CPS、每行字数/行数、title-safe、缺字体 | 这些规则直接避免不可读或不能交付的字幕，且无需复杂 AI/平台集成。 | 可在 Workspace 定位问题；阻断级问题无法绕过 Deliver。 |
| 多语言交付选择 | track 的 language/role/export_enabled 与 burn-in/sidecar 选择；最小 manifest | 已有多轨模型，若交付端不明确选择轨道就容易烧错语言或漏导出。 | 两语言三轨项目可预测地选择 burn-in 与 sidecar，并记录输出内容。 |
| 字体可复现性 | FontResolver preflight 补齐字体 hash/替代记录；项目字体打包作为可选开关 | 无字体预检/记录会使换机后的换行和画面位置漂移。 | 缺字体阻断或明确警告；交付报告列出解析字体和 hash。 |
| 性能兜底 | 先测量 4K burn-in；仅在不达吞吐门槛时实现 CPU ROI compositor，GPU pass 后置 | 不能仅因“专业软件都有 GPU pass”而扩大渲染架构；但不能让长节目导出不可用。 | 1/3/8 活动 cue 有基准；CPU ROI 达标则不开发 GPU atlas。 |
| 小型语义收尾 | 删除/隐藏字幕轨无意义的 mute/solo，统一 visible/locked/export_enabled 语义 | 这是低风险修复，能消除 UI 与预览/导出的歧义。 | Timeline、Program Viewer、Deliver 对轨道状态完全一致。 |

| 已移出当前计划（不开发） | 原因 | 重新纳入条件 |
|---|---|---|
| WebVTT、TTML/IMSC、ASS 与广播/OTT/CEA-608/708 | 没有当前平台交付需求，规范兼容和验证成本远大于日常 SRT 价值。 | 获得目标平台、客户样本和验收验证器。 |
| 自动转写、翻译、说话人识别、词级时间 | 是独立的 AI/隐私/作业系统，不是解决现有字幕轨可靠性的前置条件。 | 有明确语言、离线/云端、成本和隐私方案。 |
| Rich text、竖排/复杂 BiDi shaping、动态模板/逐词特效 | 现有样式与 Workspace 已覆盖普通字幕；这些是创意/特定语种功能而非通用阻塞。 | 有 RTL/复杂脚本文字素材，或包装模板产品需求。 |
| 多人协作、vendor package、审校冲突解决 | 当前项目已有 cue metadata，完整协作还需要账号、权限和后端策略。 | 进入多人审校或字幕供应商协作流程。 |

## 3. 已发现问题清单（按风险排序）

严重性定义：P0 = 数据丢失/撤销错误/交付错误；P1 = 主工作流受阻或性能明显退化；P2 = 专业效率、质量或兼容性不足；P3 = 增强能力。

| ID | 级别 | 发现的问题与代码证据 | 用户/交付影响 | 可落地修复方法 | 验收与自动化测试 |
|---|---:|---|---|---|---|
| SUB-P0-01 | P0 | `_execute_subtitle_command()` 直接赋值 `edit_page._current_timeline` 并调用 `timeline_view.load_timeline()`；没有走 `_on_edit_timeline_changed()`。后者才更新 `_current_timeline_sequences`、项目引用、渲染器 sequence 与统一预览请求。 | 字幕编辑可能只停留在 UI 当前对象；保存、重新打开、渲染器和其他页面看到的 sequence 有机会不同步。 | 删除字幕专用旁路。字幕命令一律交给 `TimelineView.execute_command()` 或新增 `EditPage.commit_timeline_command()`；该入口必须原子完成 command stack、timeline_changed、project sequence cache、renderer、autosave dirty state、增量 UI 更新。 | 新增 `test_subtitle_command_commit_contract.py`：Add/Update/Delete 后断言 EditPage、TimelineView、project sequence cache、renderer 获得同一对象；保存/重开内容字节等价。 |
| SUB-P0-02 | P0 | 手柄拖拽期间直接 apply 新 timeline；结束时又以「已经是最终值」创建命令。`UpdateSubtitleClipCommand.apply()` 此时保存的 `_previous` 是最终值，Undo 无法回到拖拽前。 | 位置/缩放/旋转的 Undo 表面可点击但实际无效，属于剪辑数据可靠性问题。 | 引入 `SubtitleTransformSession`：pointer-down 只记录 immutable `before`；pointer-move 仅更新 transient preview（不写 command stack）；pointer-up 只提交一个 `UpdateSubtitleClipCommand(before, after)`。必要时 preview state 单独传给 viewer。 | 单元与 UI 合约：移动/缩放/旋转/自定义 pivot 各执行一次，Undo 回到逐字段 before，Redo 到 after；取消/失焦/切换选择不写命令。 |
| SUB-P0-03 | P0 | `UpdateSubtitleClipCommand` 对 `updates` 没有字段白名单；`with_params(**updates)` 可接受 `clip_id`、`track_id`、时间字段等。`EditTrack` 也未验证 subtitle clip 的 `track_id` 与 owner 一致，或仅 subtitle 轨才有 `subtitle_clips`。 | 可构造跨轨但 owner 不变、改 ID 后找不到、普通轨携带字幕等不一致数据；后续保存、选择、导出结果不可预测。 | 定义 `SubtitleCuePatch`（只包含允许编辑字段）和 `SubtitleTimingPatch`（开始/时长）；禁止 `clip_id/track_id` 由 Update 修改，移动轨道用专属 `MoveSubtitleCueCommand`。在 `EditTrack.__post_init__` 加强 track_type、成员类型、track_id 一致性、全 sequence cue-id 唯一性校验。 | 参数化负例：非法字段、错误 owner、重复 ID、普通轨的 subtitle、字幕轨的 media clip 全部 fail-fast；旧项目迁移给出可定位错误。 |
| SUB-P0-04 | P0 | 移动/修剪在鼠标释放后才由模型校验 overlap；异常被 controller 捕获后不恢复 `SubtitleClipItem` 的临时几何。 | 用户可看到片段停留在非法位置，模型实际未更新；下一次刷新才跳回，容易误判已修改。 | 将可放置区间计算下沉到 `SubtitleEditPolicy`，拖拽时 clamp/snap 到合法范围；release 失败时显式恢复 item 到原 start/duration 并显示原因。命令失败不能吞掉状态。 | 覆盖左/右邻 cue、锁轨、0 帧、跨越多个 cue、撤销后的拖拽；所有失败路径 item geometry 与模型一致。 |
| SUB-P0-05 | P0 | `EditPage._build_timeline_preview_signature()` 遍历 `subtitle_tracks` 时读取 `track.clips`，而真实数据在 `track.subtitle_clips`。 | 字幕变更不会被统一预览签名识别；优化后的「无变化不重渲染」路径会产生遗漏或状态不同步。 | 签名重写为 subtitle-specific serializer：track visibility/enabled/order + cue timing/text/style/transform/enabled。共享同一个 `SubtitleRenderSignature` 给 EditPage、renderer cache 和 autosave diff。 | 在只改 subtitle text、样式、可见性、时长、轨道顺序时断言 signature 改变；无关 audio 参数不改变 subtitle render signature。 |
| SUB-P1-01 | P1 | `textChanged` 每个字符发出 `subtitle_param_changed`；controller 每次调用 `_execute_subtitle_command()`，后者全量 `load_timeline()` 且清空/重建 scene。颜色选择器一次颜色选择也连续发四次字段命令。 | 输入卡顿、焦点/选择闪烁、撤销栈按字符膨胀；长字幕或大量 cue 时无法使用。 | 文本框采用 150–300 ms debounce 的 transient preview，`editingFinished`/focus-out 提交一个合并命令；颜色选择用一个 RGBA patch；数值拖动用 begin/update/end session。TimelineView 的增量差异模型加入 SubtitleCueChanges，禁止用全量 load 处理普通 cue 编辑。 | 性能测试：100 cue、4K sequence，连续输入 40 字符只产生 1 条撤销记录、最多 1 次 scene item rebuild；P95 每次 preview 更新 <16 ms。 |
| SUB-P1-02 | P1 | `TimelineChanges` 和 `_clip_change_detector()` 只收集 `track.clips`/普通 ClipInstance，不识别 `subtitle_clips`；增量更新同样不会创建/更新/删除 `SubtitleClipItem`。 | 无法复用现有时间线优化；一旦接入统一提交，字幕会丢 UI 更新或被迫全量 reload。 | 扩展 `TimelineChanges` 为明确的 `SubtitleChanges`（added/removed/modified/track-state），新增 `_create_subtitle_item()` 与 `_update_subtitle_item()`，所有 item map 用带类型的 ID namespace 或全局唯一 ID。 | 对 Add/Update/Delete/track lock/visibility 的差异断言只影响对应 item，不重建不相关视频/音频 item；1000 cue benchmark。 |
| SUB-P1-03 | P1 | 编辑器预览已用紧凑 glyph cache，但导出 `_burn_in_subtitles()` 对每个活跃 cue 仍生成全画布 float32 RGBA、CPU over 合成、BGR↔RGBA 全帧转换。 | 4K 长节目烧录字幕的导出吞吐会显著下降；多轨重叠字幕按 cue 数线性放大内存和 CPU。 | 抽出 `SubtitleRenderPlan`（紧凑纹理 + 仿射矩阵）；优先在 export compositor 加 GPU subtitle texture pass（纹理 atlas/缓存、premultiplied linear blend）。CPU fallback 只处理变换后的紧凑 ROI，而非整幅 canvas。 | 1080p/4K，1/3/8 活跃 cue 的 fps、峰值内存、像素差基准；GPU 与 CPU fallback 的 alpha/color 容差对比。 |
| SUB-P1-04 | P1 | 字体/字形 raster 使用 QPainter/QFontDatabase，预览与 export 依赖本机字体；没有字体嵌入、缺字 fallback 记录、版本锁定或项目字体资产。 | 换机器、渲染节点或交付机时字形/换行/位置可能变化，无法保证线上成片一致。 | 建立 `FontResolver`：项目 font assets、family/style/postscript-name、fallback chain、字体文件 hash；项目打开与导出前 preflight。最终交付可选择嵌入/打包字体或转为受控 glyph atlas。 | 两台不同字体环境的 golden image；缺字体显示明确 warning 与替代字体；导出 manifest 记录 font hash。 |
| SUB-P1-05 | P1 | 实时手柄 update 每个 mouse event 都创建不可变 timeline 和调用 overlay 更新；没有明确的 tick 节流、取消、事件合并或渲染预算。 | 高 DPI 鼠标、复杂字幕、多个 overlay 时仍可能掉帧；与播放/seek 竞争 UI 线程。 | 手柄的模型 preview 使用 60 Hz `QTimer`/frame-coalescer，最新值覆盖旧值；仅 release 入栈；播放时降低 overlay quality 或走 GPU。 | 10 秒合成拖拽，更新频率 ≤显示刷新率、P99 input-to-photon <33 ms、命令数=1；取消后模型无变更。 |
| SUB-P1-06 | P1 | 多个字幕轨的 `subtitle_scroll_offset` 只有 layout state，没有对应滚动条/滚轮路由；subtitle splitter 在事件入口被明确拒绝，`subtitle_area_ratio` 实际不控制区域；所有轨高度被强制 40 px，预设里的 28/36/50 无效。 | 多语言/多层字幕轨无法稳定浏览；UI 暴露的分隔条/预设语义与实际不一致。 | 选择一种一致设计：A) 固定单行 lane + 独立 S 轨滚动条和可折叠轨；或 B) 允许高度/分区 resize。移除无效 splitter API，或完整接线并持久化 layout。 | 8 条字幕轨、不同 viewport 高度：每轨可达、header/clip 不重叠、滚动保持，重开项目 layout 恢复；所有 preset 断言生效。 |
| SUB-P1-07 | P1 | `AddSubtitleClipCommand` 遇 overlap 静默把 cue 推到已有片段之后；无 insert/overwrite/ripple/extend 策略，也不选择新 cue。 | 插入点与实际落点不同，容易破坏对话同步；用户无法在切点建立 cue。 | 新建 `CaptionEditMode`（overwrite/insert/ripple/replace/allow-overlap-by-lane）；默认使用可见 ghost preview + 明确策略。创建后自动选中并开始文本编辑。 | 每种 mode 的 timing、相邻 cue、undo/redo；播放头在 cue 中新增时 UI 显示最终位置而非静默移动。 |
| SUB-P1-08 | P1 | 字幕 item 拖动按像素四舍五入到帧，没有使用 timeline SnapManager、marker、in/out、剪辑边缘或 cue 边缘。 | 难以做到逐帧/切点精确对齐，低缩放下操作不可靠。 | 将 subtitle drag/trim 接入统一 SnapCandidate/SnapFeedback；支持 Ctrl 临时禁用、Shift/nudge、磁吸容差和可视指示。 | 24/23.976/29.97 fps 下对视频切点、marker、相邻 cue 边缘、range 断言落帧正确。 |
| SUB-P1-09 | P1 | 字幕 track 的 header 复用视频的 mute/solo/visibility 语义；`is_muted` 对字幕无意义，`is_solo` 未参与 `get_active_subtitle_clips()`。 | 操作标签误导，轨道状态在 UI/预览/导出间可能不一致。 | 为 CaptionTrack 定义 `visible`, `locked`, `export_enabled`, `language`, `role`, `mix_group`；移除 mute，明确 solo 应为「仅预览此语言/组」还是不提供。 | 所有轨道状态在 timeline、program monitor、burn-in、sidecar export 的一致性矩阵。 |
| SUB-P1-10 | P1 | 预览和 exporter 对字幕使用不同实现路径：preview QPainter transform，export CPU warp/composite；缓存版曾出现自定义 pivot 坐标混用，已修复一处但没有统一矩阵/黄金测试。 | 预览看起来对，导出可能有 1 px～明显的 position/rotation/alpha 差异。 | `SubtitleTransform` 只产生一次规范化 3×3 matrix；QPainter/GPU/CPU exporter 都消费同一 matrix 与同一 premultiplied color contract。建立 golden frames。 | center/custom pivot、负 scale、rotate、outline/shadow、多 DPR、不同 canvas 分辨率的像素比对。 |
| SUB-P2-01 | P2 | 数据模型只有单个 `SubtitleClip` 样式字段，没有 cue UID 外的角色、语言、speaker、置信度、review status、comment、source reference、style reference、行级时间。 | 不支持审校、翻译、听障标注、多人协作或从转写结果追溯。 | 引入 `CaptionCue` + `CaptionTrackMetadata` + `CaptionStyle` 三层；cue 只存文本/时码/metadata/style override，样式可继承 track/sequence preset。保留旧 SubtitleClip 迁移器。 | JSON schema version bump，旧项目无损迁移；track language/role/style 和 cue speaker/status roundtrip。 |
| SUB-P2-02 | P2 | 不支持 SRT/VTT/TTML/IMSC/ASS 导入；只支持 SRT 导出，且将所有轨扁平为无样式 cue。 | 无法接收客户、字幕公司、流媒体平台的字幕资产；无法保留样式/语言轨。 | 建立 format adapter registry：SRT、WebVTT 先行；TTML/IMSC、ASS 作为第二批。导入 wizard 处理 fps、offset、overlap policy、encoding、语言/role、样式映射；导出按轨/语言，不隐式合并。 | 标准 fixture roundtrip、UTF-8/UTF-8 BOM/UTF-16、CRLF、重叠 cue、24/23.976/29.97；保留未知字段 warning。 |
| SUB-P2-03 | P2 | 没有专业字幕编辑器：无 cue 列表、timecode in/out 字段、下一/上一 cue、播放头跟随、批量选择、查找替换、分割/合并、自动延长、复制样式。 | 只能通过窄时间线和属性面板逐条编辑，效率远低于 Resolve/Premiere/Avid/FCP 类工作流。 | 建立 `CaptionWorkspaceDock`：表格（序号、in、out、duration、text、speaker、status、QC），与 timeline/monitor 双向选择；快捷键编辑和批量命令。 | 1000 cue 虚拟化列表；表格改时码/文本立即同步 timeline；multi-select 的 replace style、split/merge、undo macro。 |
| SUB-P2-04 | P2 | 无字幕质量控制（读速、每行字符数、行数、最短/最长时长、flash、cut proximity、重叠、空 cue、对比度/安全框）。 | 容易生成不可读或不符合播出/平台规范的字幕；人工审校成本高。 | `CaptionQcEngine` 以 profile 驱动：Netflix-like、broadcast、social、custom；规则返回 cue/range/severity/fix suggestion。先实现 CPS、CPL、lines、duration、gap/overlap、safe title、空文本、缺字。 | fixture 覆盖中英文/RTL/emoji/多行；QC 结果可点选定位、ignore/waiver 可持久化，导出前可设阻断级别。 |
| SUB-P2-05 | P2 | 没有逐词/逐句转写、speaker diarization、翻译、词级时间或音频波形字幕编辑联动。 | 面对访谈、长节目、短视频批量生产时劳动密集。 | 将语音服务做成可选 provider：导入音频 -> background transcription job -> immutable word timings -> 编辑器生成 cue；翻译独立产生 link group，绝不覆盖原语种。先实现离线/本地 provider contract，再接云 provider。 | mock provider 和取消/重试、离线、长音频分段；手动编辑后保护用户文本，重新转写走版本分支。 |
| SUB-P2-06 | P2 | 仅有 font family/size/weight/italic、对齐、描边、阴影，缺少 style preset、背景/box、padding、opacity、边距/安全区 anchor、kerning/leading、文本方向、underline、run-level style、动画。 | 无法高效制作品牌化下三分之一、广播字幕或多语言排版；每条 cue 手工设样式且不一致。 | 第一阶段 style preset + track 默认 style + cue override + safe-area anchor；第二阶段 rich text runs/markup、background box、per-word emphasis、keyframe/模板。 | 预设修改可选择 propagate；cue override 不被覆盖；4:3/16:9/9:16 safe area anchor 保持正确。 |
| SUB-P2-07 | P2 | `SubtitleClip` 的文本布局基于本机 QPainter，未见 Unicode BiDi/脚本 shaping/vertical writing/区域框与换行策略的独立契约。 | 阿拉伯语、希伯来语、CJK、复杂 emoji/组合字、不同 fallback font 下行断行不可控。 | 采用 HarfBuzz/Qt text layout 的受控 shaping layer，保存语言、direction、script、fallback list 和 line-break policy；以 glyph run 输出给 raster/GPU。 | 中英日韩阿希混排、RTL 数字、emoji、combining mark 的 golden image 和导出一致性。 |
| SUB-P2-08 | P2 | 无画面可读性工具。虽其他页面已有 safe-area guides，但 Edit Program Viewer 未将其与字幕 anchor/QC/遮罩或背景自适应关联。 | 文字可能落在 title-safe 外或与高亮背景低对比，移动端/电视端可读性差。 | Program Viewer 加 action/title-safe、center guide、字幕 baseline/box overlay；QC 读取最终字形 bbox；可选 auto-contrast/backplate 只作为建议，不能静默改变创意。 | 不同 aspect/zoom/DPR 下 guide 与字幕 bbox 重合；字幕超安全区/低对比被 QC 标红。 |
| SUB-P2-09 | P2 | SRT exporter 使用帧到毫秒换算并写 BOM；没有 drop-frame display timecode、reel/source timecode、offset、frame rounding policy、sidecar naming/manifest 或多轨选择。 | 23.976/29.97 长节目及客户回交时可能有累计/边界约定问题；交付文件不可追溯。 | `TimebaseConverter` 统一 Rational fps 与 rounding（start floor/end ceil）；导出 wizard 显示 track/language、offset、range、BOM/line-ending、timecode mode；输出 JSON manifest。 | 1 小时 23.976/29.97/59.94 fixture，首尾/相邻 cue 不倒置、不重叠；对外工具可读的 golden SRT。 |
| SUB-P2-10 | P2 | 无项目级字幕 migration/version strategy；旧模型字段直接散在主序列 JSON。 | 后续新增样式、语言、QC、词级 timing 时难以兼容已有项目和外部 interchange。 | 单独 `captions_schema_version`、migration registry、unknown-field preservation、import provenance。任何 schema 升级必须 reversible backup。 | v1→vN fixture、缺字段、未来字段、损坏数据恢复提示及无损保存。 |
| SUB-P2-11 | P2 | SRT export 只跳过 disabled/hidden track 或 disabled cue；没有明确「导出 sidecar」与「burn-in」的策略矩阵，也没有导出前字体/QC preflight。 | 用户无法确信最终文件含哪些语言/轨道；可能输出错误字幕或漏字幕。 | Deliver 页增加 Caption Deliverables：burn-in track selection、sidecar format per language、force/SDH metadata、QC gate、font preflight、manifest。 | 2 语言/3 轨 sequence 的 burn-in 与 sidecar 组合矩阵；隐藏轨、export disabled、range export 行为可预测。 |
| SUB-P2-12 | P2 | 没有 cue 与媒体/脚本/marker/版本之间的关联，也没有 comment/review/lock-at-cue 级别。 | 不能支持译审、客户反馈、锁定已批准字幕或后期回改追踪。 | cue metadata 添加 `status`, `assignee`, `note`, `source_span`, `revision`, `locked`; collaboration adapter 后接入。 | 受锁 cue 不可改；评论不影响可见文本；版本比较显示 timing/text/style diff。 |
| SUB-P3-01 | P3 | 无 CEA-608/708、SCC/MCC、Teletext、IAB/IMSC profile、HLS/DASH embedded subtitle mux 等广播/流媒体闭合字幕链路。 | 不能承诺电视台或 OTT 的闭合字幕交付；SRT 仅适合部分 sidecar 场景。 | 在普通 caption 稳定后，以独立 `ClosedCaptionService` 实现，不把 608/708 control codes 塞入普通文本模型；先支持 IMSC/TTML，再根据客户需求支持 SCC/MCC/608/708。 | 使用官方/客户 conformance samples，mux 后在目标播放器/验证器通过。 |
| SUB-P3-02 | P3 | 无动态模板、MOGRT 类参数化字幕、逐词 karaoke、高亮、数据驱动 lower-third、动态避障。 | 短视频、新闻和品牌包装需要另行在合成页制作，编辑页生产率不足。 | CaptionTemplate 引用 Composite graph，暴露受控字段与 keyframe；普通字幕和模板字幕共享 timing/QC/export policy。 | 模板升级不破坏已存在实例；逐词 timing 在 preview/export 对齐。 |
| SUB-P3-03 | P3 | 无多人协作、字幕 vendor package、审校变更比较、字幕锁定和冲突解决。 | 长片/剧集难以进入真实制作管线。 | 建立 CaptionPackage（track/cue/style/font/QC/provenance）与 revision diff；后续接审校任务/云端。 | 离线 package roundtrip，三方文本冲突明确标记且不静默覆盖。 |

## 4. 与专业影视制作软件的差距矩阵

下表对照的是专业 NLE 普遍提供的能力类别，而不是对某一个产品的逐项模仿。

| 能力域 | 当前水平 | 专业制作的最低预期 | 差距与优先策略 |
|---|---|---|---|
| 编辑基础 | 可创建单 cue、移动、trim、删、改文本 | 键盘驱动的 split/merge/nudge/ripple/overwrite、吸附、批量编辑、稳定 undo | 先解决 SUB-P0-01~04、SUB-P1-01/02/07/08，再增加 CaptionWorkspace。 |
| 时间线可扩展性 | 单条固定高度 chip、每轨不重叠 | 数百～数千 cue、可折叠/过滤/多语言轨/可靠滚动与虚拟化 | 字幕自己的增量 diff、虚拟化 cue 列表、独立 S-track navigation。 |
| 文本排版 | 基础单样式、QPainter 本机字体 | project style、字体管理、safe area、可控换行、复杂语言、模板 | 先 style inheritance/font preflight，再 shaping/rich text/template。 |
| 预览/渲染一致性 | CPU/QPainter 两条路径 | 同一文本布局/变换/色彩语义，GPU 实时预览与可靠离线渲染 | 统一 transform/layout contract 与 golden frames；随后 GPU ROI/atlas pass。 |
| 字幕编辑器 | 属性面板 + 双击对话框 | cue list、timecode editor、波形/脚本联动、查找替换、批量 QC | P2 CaptionWorkspace 与 QC engine 是生产效率分水岭。 |
| 导入/交换 | 仅 SRT export | SRT/VTT/TTML/IMSC/ASS，编码/FPS/offset、按语言导出、保留元数据 | 先 SRT+VTT 双向，再 TTML/IMSC；ASS 作为风格映射选项。 |
| 质量与无障碍 | 无 | CPS/CPL、时长、间隙、safe area、可读性、SDH/语言角色、导出 gate | CaptionQcEngine + Deliver preflight。 |
| 自动化 | 无 | 语音转写、speaker、翻译、词级 timing、人审保护 | provider contract 优先，模型/云端实现后置。 |
| 广播/OTT 交付 | burn-in + SRT | 多语言 sidecar、IMSC/TTML、608/708 或客户格式、manifest/conformance | 与普通字幕模型隔离，按客户需求分阶段投入。 |
| 协作与审校 | 无 | 版本、评论、状态、锁定、vendor package | 基础 cue metadata 完成后再做 package/diff。 |

## 5. 目标架构（实施前先定契约）

```text
CaptionTrackMetadata (language / role / visible / export policy / base_style)
             │ 1..N
             ▼
CaptionCue (id / in-out / text/runs / style override / speaker / QC/revision)
             │
             ├── CaptionEditService ── commands ── unified timeline commit ── persistence/autosave
             ├── CaptionRenderPlan ── preview GPU/QPainter fallback ── export compositor
             ├── CaptionInterchangeService ── SRT/VTT/TTML/ASS adapters
             └── CaptionQcEngine ── workspace / Deliver preflight
```

### 不可违反的架构约束

1. **一个提交入口**：所有字幕变更（面板、timeline、workspace、导入、自动转写）都必须经过同一个 command + timeline commit 入口；不得直接改 `EditPage._current_timeline`。
2. **模型与预览分离**：拖拽/输入期间只维护 transient preview；完成、失焦或显式确认时才提交可撤销命令。
3. **单一布局/变换规范**：文本 shaping、bbox、pivot、matrix、alpha mode、color space 只能定义一次；preview、CPU fallback、GPU export 均消费相同 RenderPlan。
4. **样式可继承而非复制**：sequence/track style preset → cue override，避免每个 cue 冗余持有一整套样式字段。
5. **普通字幕与闭合字幕隔离**：开放式文本轨、sidecar captions、608/708 control data 使用不同模型与 exporter，不以 bool 开关混合。
6. **任何格式导入均有 provenance**：记录 source filename、format、timebase、offset、导入时间与警告；不静默“修复”时码或重叠。

## 6. 分阶段实施计划表

### Phase 0：先恢复数据一致性与可撤销性（P0，建议 3–5 个小 PR）

| 顺序 | 工作包 | 涉及文件/模块 | 详细实施步骤 | 依赖 | 验收门槛 |
|---:|---|---|---|---|---|
| 0.1 | 统一字幕提交入口 | `edit_controller.py`、`edit_page.py`、`timeline_view.py` | 提供 `commit_timeline_command(command, source)`；调用 command stack 后由 `timeline_changed` 驱动 `_on_edit_timeline_changed`；消除 `_execute_subtitle_command` 对私有字段的赋值与直接 load。 | 无 | 新增/编辑/删除字幕后 project cache、renderer、view、save 使用同一 sequence revision。 |
| 0.2 | 修复 transform session | `subtitle_handle_controller.py`、`program_viewer.py`、controller、commands | 新建 `SubtitleTransformSession(before, preview, active_handle)`；preview 仅送 viewer；release 构造一次 before/after command；Esc、focus loss、selection change 调 cancel。 | 0.1 | 每种变换 Undo/Redo 完全可逆；无拖动/取消不产生 undo entry。 |
| 0.3 | 模型不变量与 patch whitelist | `timeline_v2.py`、`commands.py` | 引入不可变 patch dataclass；禁止改 id/owner；校验 track type/track id/unique ID；迁移函数对旧数据给错误上下文。 | 无 | 所有非法模型从构造/反序列化/命令入口拒绝。 |
| 0.4 | 拖拽失败回滚和 collision policy | `subtitle_clip_item.py`、`timeline_view.py`、新 `caption_edit_policy.py` | 预先计算合法 range；实时显示 snap/forbidden；release 失败 restore；定义默认 overwrite/insert 策略并显示结果。 | 0.3 | 失败后不出现 UI/模型分叉；所有相邻/重叠/锁轨场景有测试。 |
| 0.5 | 修复 preview signature | `edit_page.py`、renderer signature helper | 用 `subtitle_clips` 和所有视觉字段构建显式 signature，补 track enabled/visible/order。 | 0.1 | 改字幕触发一次正确 request，改无关字段不触发。 |

### Phase 1：实时编辑性能与可靠时间线（P1，建议 4–6 个小 PR）

| 顺序 | 工作包 | 涉及文件/模块 | 详细实施步骤 | 依赖 | 验收门槛 |
|---:|---|---|---|---|---|
| 1.1 | SubtitleChanges 增量管线 | `timeline_view.py`、`subtitle_clip_item.py` | 为 subtitle 单列 map/diff/create/update/remove；track lock/visible/height 也进入 diff；禁止文字编辑全量重建。 | 0.1, 0.3 | 100 cue 中改 1 条只更新一个 QGraphicsItem。 |
| 1.2 | 面板编辑 transaction/debounce | `edit_properties_panel.py`、controller | Text 预览 debounce，commit on focus-out；RGBA 一次 patch；DragValue begin/update/end；相同字段去重。 | 0.1, 1.1 | 40 次 keypress=1 undo、0 scene full reload，输入焦点稳定。 |
| 1.3 | 字幕时间线导航与布局修复 | `timeline_layout.py`、`timeline_view.py`、`timeline_panel.py` | 选择固定 lane+独立字幕滚动条/折叠轨，或可 resize lane；移除死 splitter/无效 preset 分支；持久化 layout state。 | 1.1 | 8 轨可访问、可滚动、重开一致；控件语义真实。 |
| 1.4 | Snap/nudge 与编辑模式 | `timeline_interaction.py`、SnapManager、caption policy | cue edge、video edit、marker、range 参与 snap；快捷键 nudge/split/merge；明确 insert/overwrite/ripple。 | 0.4 | 逐帧准确、Ctrl bypass、可见 feedback、undo macro。 |
| 1.5 | 预览帧预算与字体缓存 | `program_viewer.py`、renderer、`FontResolver` 雏形 | mouse updates coalesce 到 display tick；glyph/QImage LRU 按 font hash + style + text；统计 cache hit/miss/UI ms。 | 0.2 | 4K、100 cue、单活动 cue时 P95 <16 ms；拖拽 P99 <33 ms。 |
| 1.6 | 预览/导出 matrix parity | renderer、handle、export | 集中 `SubtitleTransform`/RenderPlan；golden images。 | 0.2 | preview/CPU/GPU（引入后）对齐且 custom pivot 无回归。 |

### Phase 2：可靠字幕交付收尾（仅保留必须项）

| 顺序 | 工作包 | 详细实施步骤 | 输出物 | 验收门槛 |
|---:|---|---|---|---|
| 2.1 | SRT 交换与时基 | 实现 SRT importer；统一 Rational fps 的 start floor/end ceil；导入时选择目标语言轨和 offset；导出按单轨/语言生成 sidecar。 | `caption_srt.py`、导入入口、SRT fixture。 | SRT 导入→编辑→导出不丢文本/时码；24/23.976/29.97 相邻 cue 不倒置。 |
| 2.2 | 基础 QC | 实现 empty、duration、gap/overlap、CPS、CPL/lines、title-safe、缺字体规则；Workspace 展示并定位；Deliver 可阻断 error。 | `caption_qc.py`、QC 面板/列表、可持久化 waiver。 | 每条规则有 fixture；error 无法静默导出，warning 可记录豁免。 |
| 2.3 | Deliver 选择与 manifest | 将 language/role/export_enabled 接入 burn-in/sidecar 选择，输出最小 JSON manifest（轨道、范围、字体、QC）。 | Deliver captions section、manifest writer。 | 两语言/三轨项目的输出矩阵稳定，隐藏或禁用轨绝不混入。 |
| 2.4 | 字体可复现性 | 将 FontResolver 结果写入 Deliver report；记录 font file hash 和 fallback；项目字体打包只作为显式选项。 | preflight report、font manifest。 | 换机缺字体有可操作提示；交付记录可追溯。 |
| 2.5 | 4K 导出基准与按需优化 | 先测 1080p/4K、1/3/8 cue 的吞吐/RSS；若 CPU 基线不足，先做紧凑 ROI 合成；只有 ROI 仍不达标才设计 GPU atlas。 | benchmark report；必要时 CPU ROI compositor。 | 达到项目确定的吞吐目标；preview/export 像素容差保持现有 parity。 |
| 2.6 | 轨道语义收尾 | 让字幕 header 只显示 visible/locked/export_enabled；清理无意义 mute/solo 或映射为明确的 preview filter。 | header/UI state mapping、状态矩阵测试。 | 同一状态在 timeline、viewer、burn-in、sidecar 中行为一致。 |

### 明确不排期的增强项

TTML/IMSC/ASS/WebVTT、CEA-608/708/OTT mux、自动转写/翻译、复杂 BiDi 与 rich-text、动态字幕模板及协作审校，均已移出本计划；仅在 2.2 的“重新纳入条件”满足后单独立项，不再作为本字幕轨优化的尾项。

## 7. 建议的测试与性能门槛

| 测试层 | 必须新增的测试 | 通过标准 |
|---|---|---|
| Model/property | track/cue 不变量、patch whitelist、overlap mode、ID/owner 唯一性、schema migration | 随机操作 10k 次后仍满足所有不变量。 |
| Commands | Add/Move/Trim/Split/Merge/Batch/Undo/Redo/cancel | 每条命令 after→undo→redo 等价；command failure 原子回滚。 |
| UI contract | panel debounce、selection、drag/snap、scroll、track visible/export state | 不依赖 OpenGL 的 headless Qt 测试；禁止全量 reload 的计数断言。 |
| Render parity | glyph bbox、matrix、center/custom pivot、outline/shadow、DPR、negative scale | preview、CPU fallback、GPU 输出使用 golden PNG/alpha diff。 |
| Interchange | SRT fixture、encoding、fps/offset、language-track mapping | import→export→import 的 cue/timecode/language 不变量。 |
| QC | 中英/RTL、多行、长/短、safe area、CPS/CPL、waiver | 规则输出稳定的 code/severity/fix。 |
| Performance | 100/1000 cue timeline，1080p/4K preview/export，1/3/8 active cue | CI 记录 median/P95/peak RSS/cache hit；回归阈值需版本化。 |
| E2E | 创建→编辑→保存→重开→导出 burn-in + sidecar | timeline、project、renderer、输出一致；替换字体/缺字体必须 preflight。 |

### 推荐的首批新增文件

```text
src/features/edit/core/captions/model.py
src/features/edit/core/captions/commands.py
src/features/edit/core/captions/edit_policy.py
src/features/edit/core/captions/render_plan.py
src/features/edit/core/captions/qc.py
src/features/edit/core/captions/caption_srt.py
src/features/edit/ui/caption_workspace.py
tests/edit/test_caption_interchange_srt.py
tests/edit/test_caption_qc.py
tests/benchmark/test_caption_export_performance.py
```

## 8. 实施决策、风险与非目标

| 决策/风险 | 建议 |
|---|---|
| 是否立即重写全部字幕模型 | 否。先以适配层让 `SubtitleClip` 进入统一 commit/command/render contract，再做可版本化迁移；避免将 P0 修复与 P2 功能大重构绑在同一 PR。 |
| CPU 与 GPU 字幕渲染 | GPU 是 4K/多轨交付目标，CPU ROI fallback 是可靠性要求；在 GPU pass 未稳定前不要删除 CPU baseline。 |
| 允许同一轨 overlap 吗 | 不要把「不允许」写死为唯一规则。普通单语字幕默认不重叠；翻译/图文/模板使用独立 lane 或显式 overlap policy。 |
| 自动转写上线顺序 | 先定义可取消、可离线、可审计的 provider contract 和手动修改保护；模型供应商/云服务作为实现替换，不得把业务模型绑定到某 API。 |
| CEA-608/708 是否纳入基础版本 | 否。它是规范化闭合字幕数据，不是“更复杂的 SRT”。在 Phase 2 交付稳定并有真实客户需求时启动。 |
| 依赖 `rawpy` 的测试收集失败 | 在 CI 通过 extras 安装，或让 `thumbnail_provider` 对 RAW 支持延迟导入；不能让无关可选依赖阻断字幕/时间线 UI 测试。 |

## 9. 推荐执行顺序与 Definition of Done

Phase 0、Phase 1 已完成主体能力但仍有上表所列验收缺口；Phase 2A 是可用的迁移基础和只读 Workspace 首版，不再标记为完整交付。本轮已收完 cue 自身吸附排除和多选 nudge 原子性；后续先补真实 render golden，再严格按 **2.1 SRT → 2.2 基础 QC → 2.3 Deliver 选择/manifest → 2.4 字体可复现性 → 2.5 先测后优化导出 → 2.6 轨道语义收尾** 执行。不要把转写、TTML/广播格式、动态模板或协作插入该链路。

一个字幕功能只有同时满足以下条件才能标记完成：

1. 模型不变量、保存重开、Undo/Redo、失败回滚均通过；
2. timeline、Caption Workspace、Program Viewer、export 使用同一个 sequence revision；
3. 预览、CPU fallback、GPU（如启用）有同源 RenderPlan 和 golden image；
4. 交付前能说明导出了哪些轨、语言、样式/字体、QC 状态，并留下 manifest；
5. 性能门槛在目标分辨率/活跃 cue 数量上可重复测量，而不是只在空项目中“感觉流畅”。
