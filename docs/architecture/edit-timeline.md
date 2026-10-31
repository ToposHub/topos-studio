# Edit 时间线与命令系统架构

- 撰写日期：2026-09-28（计划04 · 架构文档 / Edit 时间线与命令系统）
- 范围：`src/features/edit/core/` 下的时间线数据模型、命令（undo/redo）系统、音频图、播放链路
- 信息来源：直接阅读下列源文件（版本为 dev 分支 2026-09-28 状态），并引用 2026-09-28 静态审查报告佐证（batch-162、batch-191，见文末）
- 阅读对象：需要修改 Edit 时间线、命令、混音或播放逻辑的开发者

---

## 1. 总览

Edit 页面的时间线子系统遵循"**不可变模型 + 命令模式 + 提交边界 bump revision**"的三层结构：

1. **模型层**（`timeline_v2.py` / `project.py`）：全部是 `@dataclass(frozen=True)` 的纯 Python 数据类，不依赖 PyQt/OpenGL。任何修改都通过 `with_*` 工厂方法或 `dataclasses.replace` 生成新实例，原实例永不改变。
2. **命令层**（`commands.py`）：所有编辑操作封装为命令对象（`EditCommand` Protocol），`apply()` 返回新时间线、`undo()` 逆向恢复；`CommandStack` 管理两条历史栈。项目级操作（素材箱/素材）走独立的 `ProjectCommandStack`。
3. **消费层**：渲染/音频/导出从模型或其投影（snapshot、signature）读取数据，与模型层解耦。AI 与脚本路径经 `EditProjectService.commit_command()` 统一提交，做乐观锁校验后 bump `revision`。

```
                    ┌────────────────────────────────────────────────────────────┐
                    │                        UI 层（Qt 主线程）                     │
                    │  TimelineView / TimelinePanel / 属性面板 / EditPage           │
                    │  （TimelineView 自持一个 CommandStack，见 timeline_view.py:614）│
                    └───────┬───────────────────────────────┬────────────────────┘
                            │ command.apply(timeline)       │ signature/snapshot 同步
                            ▼                               ▼
        ┌───────────────────────────────┐   ┌─────────────────────────────────────┐
        │ commands.py                    │   │ edit_page.py                        │
        │  EditCommand (Protocol)        │   │  _rebuild_audio_tracks()            │
        │  AddClip/Move/Trim/Split/...   │   │   ├─ build_audio_graph_snapshot()   │
        │  MacroCommand / RippleDelete   │   │   │   （路由校验门 P0-02）            │
        │  CommandStack（undo/redo 栈）   │   │   └─ _build_audio_timeline_signature│
        │  ProjectCommandStack（bin/asset）│  └──────────────┬──────────────────────┘
        └───────┬───────────────────────┘                   │ 音频线程重建轨/解码器
                │ apply/undo 产生新实例                        ▼
                ▼                                   ┌──────────────────────────┐
        ┌───────────────────────────────┐           │ audio_playback.py         │
        │ timeline_v2.py（不可变模型）     │           │  AudioMixer（QMutex 保护）  │
        │  TimelineSequence ─ revision   │◄──────────│  AudioTrackState/decoder  │
        │  EditTrack / ClipInstance      │  快照/签名  └──────────────────────────┘
        │  with_* / _rebuild 工厂         │
        └───────┬───────────────────────┘
                │ 同一模型被三条消费路径读取
                ├────────────► timeline_preview_renderer.py（GPU 预览，经 RealtimePlaybackEngine）
                ├────────────► timeline_export.py / stem_export.py（离线导出，经 AudioGraphRenderer）
                └────────────► ai_editing/edit_project_service.py（AI/脚本统一提交入口，
                                乐观锁 + with_revision_bumped + ref↔sequence 双层同步）

        播放链（视频）：
        PlaybackControllerTimer(QTimer,16ms) → EditPlaybackController.tick(monotonic)
          → frame_ready → TimelinePanel._on_timer_frame_ready
          → TimelinePreviewRenderer.request_render
          → RealtimePlaybackEngine.request_present（唯一热路径）
          → SourceResolver.resolve_layers → TimelineDecodeScheduler（预取窗口）
          → ClipDecodeStream + FrameRingBuffer（解码池化） → GpuFramePool / GPU 合成 → ProgramViewer
```

线程边界一览：

| 线程/上下文 | 职责 | 与模型的关系 |
|---|---|---|
| Qt 主线程 | UI、命令构造与执行、签名计算 | 唯一写方（经命令） |
| 音频线程（AudioMixer） | 实时混音 | 只读投影（AudioTrackState/签名重建），QMutex 保护 |
| 解码 worker 池 | ClipDecodeStream 解码作业 | 不触碰 TimelineSequence |
| GL/渲染线程 | GpuFramePool、GPU 合成 | 消费 FrameHandle，不回读模型 |

---

## 2. 模型层：timeline_v2.py / project.py

### 2.1 TimelineSequence 家族（timeline_v2.py，3,549 行）

模块自述设计原则（文件头）：不可变数据类（`frozen=True`）确保线程安全；完整类型注解；不依赖 PyQt；参数验证集中在 `__post_init__`；同轨剪辑不允许重叠。

核心实体：

| 类 | 职责 | 关键点 |
|---|---|---|
| `TimelineSequence` | 时间线序列 | 持 `video_tracks`/`audio_tracks`/`subtitle_tracks` 三组轨道元组、markers、caption graphic bindings、master 混音字段、`revision`（见 §3.4） |
| `EditTrack` | 编辑轨道 | 26 个字段：clips/subtitle_clips/transitions、mute/solo/lock/sync_lock、volume/pan/strip_eq/effect_chain、automation、Bus 路由（is_bus/output_bus_id/sends）、stem_group、caption_metadata、track_role（standard/graphics） |
| `ClipInstance` | 剪辑实例 | 双窗口时间映射（timeline_start+duration ↔ source_start+duration）、speed（float 或 SpeedParams 变速点）、transform/crop/opacity/effects、fade、freeze_source_frame、blend_mode、audio_gain_db、调色/合成/渲染缓存链接字段 |
| `SubtitleClip` / `TransitionInstance` / `TimelineMarker` | 字幕/转场/标记 | 各自独立 frozen dataclass |
| `Send`/`StripEQParams`/`EffectChainParams` 等 | 混音参数 | frozen 值对象 |

**帧映射**：`ClipInstance.timeline_to_source_frame()` 是播放/擦洗/导出三路共用的唯一换算入口，内部依次处理：冻结帧短路 → Motion Graphic 虚拟素材 span 映射 → 变速点（SpeedParams）→ 有理数精确映射（`timeline_frame_mapper.map_frame`，需 fps 元数据）→ 旧公式回退。

### 2.2 with_* 工厂模式的三种实现（重点）

修改 frozen 实例有三种并存方式，**这是改动时最容易踩坑的地方**：

1. **`dataclasses.replace`（安全，首选）**——`ClipInstance` 的全部 `with_*`（如 `with_timeline_start`，timeline_v2.py:501-502）和 `EditTrack.with_effects`（timeline_v2.py:894-907）都用 `replace()`。`with_effects` 的 docstring（FX-C01 修复注释）明确记录了教训：手工枚举字段的旧实现曾丢 `label_color`、fade、`color_stack_id`、FPS/PTS 映射字段等，"adding a blur could silently kill the fade or detach the color stack"。

2. **`_rebuild(**kwargs)`（中心化转发）**——`EditTrack._rebuild`（timeline_v2.py:2340-2401）与 `TimelineSequence._rebuild`（timeline_v2.py:3241-3281）是"内部不可变重建器"，把所有字段转发集中到一处，避免每个 `with_*` 手工复制时遗漏新增字段。注意其参数语义：
   - `None` 表示"不传该字段"（保留原值）；要显式清空 strip_eq/color/output_bus_id/effect_chain 等 Optional 字段必须走对应 `with_*`；
   - `automation=None` 表示不传，清空须传 `{}`；
   - `caption_metadata` 用字符串 `"ellipsis"` 哨兵区分"不传"与"显式置 None"；
   - `TimelineSequence._rebuild` 对 `range_start/range_end` 用 `...`（Ellipsis）哨兵——两种 `_rebuild` 的哨兵风格不一致（AUD-19108）。

3. **手工逐字段构造（危险，历史遗留）**——`EditTrack.with_strip_eq / with_effect_chain / with_name / with_color / with_output_bus` 五个方法绕过 `_rebuild` 直接 `EditTrack(...)` 构造（注释称是为了避开 None="不传"歧义），但手工枚举**漏掉了 `is_sync_locked` 与 `track_role` 两个字段**（AUD-19100）：用户关闭同步锁后任何重命名/改色/改 EQ 都会把它静默重置为 True；对 graphics 轨重命名会把 `track_role` 重置为 "standard"，使 `get_graphics_track()` 返回 None、字幕图形链路静默失效。**给 EditTrack/TimelineSequence 增加新字段时，务必同步更新 `_rebuild` 与全部手工构造点，并优先用 `dataclasses.replace`。**

`project.py` 的 `MediaAsset`（asset_id/path/metadata/proxy/日志字段）吸取了教训，文件头明确"所有 with_* 使用 dataclasses.replace 派生新实例"（project.py:153-155）；但 `ProjectBin` 的六个 `with_*`（project.py:501-565）仍是手工构造（当前只有 5 个字段，风险可控）。`EditProject` 持有 assets/bins/timelines(ref)/compositions/color_stacks/smart_bins 等 dict 字段与项目级 `revision`；`__post_init__` 中旧 schema 无 root_bin_id 时用 `object.__setattr__` 自动补建 Root bin（frozen 类内改字段的合法用法之一）。

### 2.3 校验与序列化

- `__post_init__` 全量校验：非空 ID、非负帧、正时长、speed ∈ [0.1, 8.0]、opacity/volume/pan/gain/EQ 各参数域、clip 与 subtitle clip 不重叠、字幕轨不变量（subtitle_clips 只能存在于 subtitle 轨、track_id 一致、序列内 cue id 全局唯一）、caption binding 唯一性。这是 O(n log n) 的有意代价（不可变模型在构造边界强制不变量）。
- `to_dict`/`from_dict`：`EditTrack.from_dict` 与 `TimelineSequence.from_dict` 对损坏的 automation 数据 `except Exception: pass` 静默丢弃（AUD-19102/19103，Major）——修改这两个加载器时应收窄异常并记日志。
- 已知契约裂缝：`__post_init__` 接受 int/float fps "for convenience"，但 `to_dict` 无条件访问 `fps.num/.den`，传 `fps=30` 的序列保存时会 AttributeError（AUD-19101，现行调用方均传 Rational，属潜伏问题）。

---

## 3. 命令系统：commands.py（16,251 行）

### 3.1 EditCommand Protocol（命令契约）

```python
class EditCommand(Protocol):            # commands.py:141
    command_id: str
    label: str
    def apply(self, timeline: TimelineSequence) -> TimelineSequence: ...
    def undo(self, timeline: TimelineSequence) -> TimelineSequence: ...
    def to_dict(self) -> dict[str, Any]: ...
```

实际命令类多为 `@dataclass(frozen=True)`，另提供 `create(...)` 静态工厂与 `from_dict(...)` 反序列化。命令是**纯数据操作**：不依赖 PyQt、不触碰 OpenGL、不发射信号（信号合并在上层 controller）；命令只持数据快照，无 GPU 资源引用。

`apply` 的返回值有**两种合法形态**，这是命令系统最核心、也最微妙的约定：

1. 返回 `TimelineSequence`（新时间线）；
2. 返回二元组 `(new_timeline, updated_command)`——apply 过程中捕获的旧值（old_volume、被删快照、生成的 clip_id 等）写进**新命令实例**，由栈保存供 undo 使用（如 `SetTrackMuteCommand.apply`，commands.py:6403-6410）。

此外还有第三种**约定外的**旧值保存方式：`SplitClipCommand.apply` 用 `object.__setattr__` 就地改写 frozen 命令自身（commands.py:2136-2138，全文件共 96 处，AUD-16215）。模块头声明 "All commands are immutable"，但这套写法使同一命令实例在 execute/redo 间被就地改写——单线程语义尚可，混用两套机制正是 AUD-16205 一族"旧值丢失"缺陷的温床。

### 3.2 undo 语义的三种实现机制

| 机制 | 代表命令 | 说明 |
|---|---|---|
| apply 返回 `(timeline, updated_cmd)` | SetTrackMute/Solo/Volume、DeleteClip（快照三重恢复）、SlipClip、ChangeClipSpeed、RippleTrim 等 | 正确模式；`CommandStack.execute` 检测到 tuple 就把 updated_cmd 压栈（commands.py:3710-3715） |
| `object.__setattr__` 回写 | SplitClip、MacroCommand 的 split 链接保持 | 就地改写 frozen 实例，绕过类型系统 |
| 工厂预存/不存旧值 | MoveClip、TrimClip、UpdateKeyframe、Relink、BatchRelink、UpdateAssetProxy 等 | apply 计算了旧值但只存局部变量 → undo 退化为 no-op / 抛 "old values not stored" / 片段被移到第 0 帧（AUD-16205，Major，7 类命令） |

**新增命令时必须二选一：要么返回 updated_cmd，要么 `object.__setattr__` 回写，并为 undo 写往返测试。**

### 3.3 撤销栈：CommandStack / ProjectCommandStack

`CommandStack`（commands.py:3668-3812）：

- `execute(command, timeline)` → apply（处理 tuple 返回）→ 压 undo 栈 → **清空 redo 栈** → 超过 `max_size=100` 截断最旧历史；
- `undo(timeline)` → pop → `command.undo(timeline)` → 压 redo 栈，返回新时间线（空栈返回 None）；
- `redo(timeline)` → pop redo → `command.apply`（同样处理 tuple 返回）→ 压回 undo 栈；
- 辅助：`can_undo/can_redo`、`get_undo_label/get_redo_label`、`clear()`、`get_history_size()`。

`ProjectCommandStack`（commands.py:6152-6267）结构相同，操作对象是 `EditProject`（素材箱/素材/时间线引用级命令），**不处理 tuple 返回**（项目命令均返回新 project）。

已知薄弱点：

- **undo 抛异常时丢命令**：`undo` 先 pop 再执行，失败（如快照缺失的 ValueError）时命令既不在 undo 栈也不在 redo 栈，操作从此不可撤销/重做，且栈与时间线状态错位（AUD-16204，Major；两处栈同构）。
- **无命令合并（coalesce）**：拖拽类操作每次 mouse-up 产生一条命令，快速消耗 100 条历史，最旧操作被静默挤掉（AUD-16219）。
- 栈本身无锁；当前仅 UI 线程调用是安全前提。

`MacroCommand`（commands.py:9902-10055）把多个原子命令组合为一个用户可见操作（RippleDelete、Shift-split、scene-cut 等大量使用）：

- `apply` 顺序执行子命令（逐个处理 tuple 返回），**任一子命令失败时逆序回滚已应用的子命令再抛出**（不可变模型使中间时间线天然可弃，不留半执行状态）；
- 附加逻辑：成对分割 A/V 链接时为两个右段重建 reciprocal link；
- `undo` 逆序回滚全部子命令；
- 已知缺陷：`from_dict` 只识别 Delete/Move/Add 三种子命令类型，其余**静默丢弃**（AUD-16210，Major）。

### 3.4 revision 与乐观锁（TAEP P0）

`TimelineSequence.revision`（timeline_v2.py:2959-2964）与 `EditProject.revision`（project.py:1001-1006）是**内容版本号**（不是 schema 版本），用于 AI plan 的 `base_revision` 一致性检查（TAEP §6.2）。规则：

- `_rebuild` / `with_*` **不 bump** revision——preview、模拟、diff 产生的新实例与 base 同 revision，便于乐观锁比较；
- 唯一 bump 点是提交边界：`with_revision_bumped()`（timeline_v2.py:3283-3290）只应在命令成功 apply 并落地后调用。

统一提交入口是 `EditProjectService.commit_command`（ai_editing/edit_project_service.py:156-261），流程：

```
assert_sequences_consistent（ref 层 key == sequence 层 key，把保存期校验提到运行时）
  → get_sequence（懒加载未物化的 sequence 时抛明确错误）
  → 乐观锁：base_revision != sequence.revision 时抛 RevisionConflict（不自动 last-write-wins）
  → command.apply（不可变模型天然在"副本"上模拟）→ 取 tuple[0]
  → validate_timeline（引用完整性 + transition 邻接；overlap 已在 apply 内强制）
  → committed = proposed.with_revision_bumped()          ← 唯一 bump 点
  → _sync_two_layers：sequence map 写回 + TimelineSequenceRef 反向同步
    （name/fps/width/height/sample_rate/format_settings）+ project revision bump
  → CommitResult（applied=False 时两层状态保持原样，无半套状态）
```

调用方：UI 直接交互路径仍主要走 `TimelineView` 自持的 `CommandStack` + `EditController._on_edit_timeline_changed` 同步两层；AI/脚本/plan 路径走 `transaction_service` → `commit_command`。**任何新的写入入口（脚本、导入、MCP host）都应走 `EditProjectService`，否则 revision 与 ref↔sequence 同步会失去保证。**

---

## 4. 音频图：snapshot、renderer 与音频线程的交接

音频侧有四个角色，注意它们的分工与演进状态（目标架构 §1.1/§5.1 的渐进实现）：

| 组件 | 位置 | 角色 | 状态 |
|---|---|---|---|
| `AudioMixer` | audio_playback.py:1380 | **实时**混音（音频线程），持 QMutex，内部维护每轨 EQ/FX/automation/LUFS、Bus 路由、master 限幅器与零分配 scratch 缓冲 | 生产路径 |
| `AudioGraphSnapshot` | audio_graph_snapshot.py:83 | 从 TimelineSequence 一次性构建的**不可变快照**（clip/track/master 节点 + 编译期校验），禁止播放线程读取 UI 对象 | 投影/校验用 |
| `AudioGraphRenderer` | audio_graph_renderer.py:95 | 实时/导出**共享节点处理**：与 `mix_samples` 严格一致的节点顺序与 DSP，供导出（timeline_export.py、stem_export.py）单线程使用 | 导出路径 |
| `SnapshotHistory` | audio_graph_snapshot.py:321 | revision 单调递增的快照序列，`needs_rebuild` 用 stable_hash 比较 | 未来原子换图的基础 |

**交接流程**（UI 线程 → 音频线程）：

1. 时间线变更后 `EditPage._rebuild_audio_tracks()`（edit_page.py:1040 起）在 UI 线程执行：
   - 先做嵌套序列音频展开（`flatten_nested_audio_tracks`）；
   - **路由校验门（P0-02）**：`build_audio_graph_snapshot(timeline)` 做编译期校验（环/未知 bus/重复 ID，复用 `validate_routing_graph`）。图非法时**不重建轨道**——保留上一张有效图继续播放，错误记入 `_audio_routing_errors`，但时间基准（fps/时长）仍同步；图恢复合法后自动追上；
   - 计算 `_build_audio_timeline_signature(...)`（结构 `(fps, master_volume, master_muted, tuple(track_entries))`）；签名与活跃轨 ID 都未变则只刷新 fps/duration 后返回（跳过重建）；
   - 签名变化：quiesce/pause → 移除全部旧轨 → 按 `_update_audio_track_state` 重建 track/decoder/状态，写回 `_audio_timeline_signature` 与 `_audio_graph_revision`（playback_trace 记 `audio.graph_committed`）。
2. 音频线程内部：`AudioMixer` 所有状态读写都包在 `QMutexLocker(self._mutex)` 内；`mix_samples` 的节点顺序为 clip fade → automation(mute gate + de-click) → clip FX → automation(volume/pan ramp) → volume → pan（按源声道数选 equal-power/balance）→ strip EQ → track FX → mute gate → 路由（output_bus/sends → Bus 拓扑序 → master → 软限幅）。`AudioGraphRenderer._apply_declick_edges` 等静态工具被实时路径复用（audio_playback.py:2083 等）。
3. 一致性保证：`AudioGraphSnapshot.stable_hash` 的字段覆盖必须是 `edit_page._build_audio_timeline_signature` 的严格超集（RMA-02/B1 注释），否则用 snapshot 替代 signature 会丢重建触发（曾漏 clip 的 asset_id/source_start 与 track 的 is_enabled）。

**改动注意**：给模型新增"会改变声音"的字段时，需要同时更新（1）`_build_audio_timeline_signature`（2）`AudioGraphSnapshot.stable_hash`（3）`_TrackNode`/`AudioMixer` 的消费逻辑，并补签名完整性测试。

---

## 5. 播放链：EditPlaybackController 与 realtime_playback

### 5.1 EditPlaybackController（调度层，edit_playback_controller.py，1,238 行）

基于 **monotonic clock** 的帧调度器（不做简单 `+=1`），不依赖具体渲染器：

- `tick(now_monotonic)`（:826）：elapsed × fps × speed 得目标帧 → 方向调整 → clamp 到播放范围（range 或全时间线）→ 边界处理（循环 re-anchor / 停止并发 `end_reached`）→ **模式分叉**：
  - `PREVIEW` 模式：落后超过 `DROP_THRESHOLD=3` 帧时跳帧追赶，每次最多 `MAX_DROPS_PER_TICK=5` 帧（丢帧计入 dropped_frames）；
  - `EXACT` 模式（pause/scrub）：绝不跳帧，每 tick 只前进/后退一帧，保证帧准确。
- JKL：L/J 键在 1x/2x/4x/8x 档位间循环（`PLAYBACK_SPEEDS`），K 停止，500ms 空闲自动复位；NTSC 分数帧率用精确 Rational 表（24000/1001 等）。
- 信号：`frame_changed`/`state_changed`/`speed_changed`/`direction_changed`/`end_reached`，全部走 Qt 主线程。
- `PlaybackControllerTimer`（:1121）：QTimer 包装（默认 16ms，`Qt.TimerType.PreciseTimer`——默认 Coarse ±5% 容差在 40ms 周期下有可见抖动），interval 按 `1000/fps` clamp 到 [16,100]ms。

**视频时钟是唯一 master**：`EditPage._setup_audio_video_sync` 刻意断开 audio `frame_changed → seek` 的连接，播放期间音频帧回调只用于计算 A/V drift（`AVSyncController.evaluate`，drift ≥150ms 记 warning），避免音频回调与视频时钟互相抢帧（edit_page.py:1812-1898）。

### 5.2 RealtimePlaybackEngine（realtime_playback/，唯一热路径）

目标（包 docstring + engine.py 头注释）：把 Edit 播放收敛为**单一热路径**，"新架构写了但真实播放仍走老路径"是被明确修复的反模式：

```
播放 tick → RealtimePlaybackEngine.request_present(frame, deadline)
         → RealtimePlaybackPolicy.decide_sources(frame)      # source 决策
         → TimelineDecodeScheduler.get_or_prepare_bundle(frame)
         → ClipDecodeStream / FrameRingBuffer 返回 ready frames
         → GpuFramePool 确保 GPU-resident frame
         → TimelineGpuCompositor 合成 → ProgramViewer present
```

关键事实：

- **装配**：`TimelinePreviewRenderer.__init__` 创建 engine 并 `configure(renderer=self, delegate_render=self._engine_delegate_render, delegate_scrub_render=..., source_resolver=..., layer_query_provider=...)`（timeline_preview_renderer.py:5868-5899）；渲染请求统一路由 `request_present_via_engine`（:7696）。engine 是编排层，实际渲染委托回 renderer（渐进式集成，复杂效果仍走 `_try_gpu_render` 任务组装）。
- **request_present**（engine.py:683）每帧动作：generation 守卫 → 预算 back-pressure（`PlaybackBudgetController.before_render`，多轨超预算 drop 不阻塞 UI tick）→ `next_frame_trace_id()` 分配统一 trace id → `SourceResolver.resolve_layers` 产 `SourceDecision`（只影响 telemetry 与后台 proxy 触发，**不阻塞当前帧**——设计权衡见 engine.py:15-27 注释）→ `_schedule_decode_window` 预取 → 委托渲染 → telemetry（`PlaybackFrameTelemetry`）→ miss 处理。
- **miss/rebuffer 状态机**：连续 miss ≥ 3 进 `REBUFFERING`，present 成功回 `PLAYING`；miss 归因由 worker 通过 `note_playback_miss()` 上报真实原因（GPU drop、解码失败），取代 delegate 布尔值的模糊分类。状态机：`IDLE → WARMING → READY → PLAYING ⇄ REBUFFERING → PAUSED → SHUTDOWN`。
- **scrub 通道独立**：`request_scrub_present` 刻意不走 budget/rebuffer——scrub 的 miss 语义是"显示最近帧、永不冻结"。
- **自适应质量（P1-10）**：`AdaptiveQualityController` 按负载观测（miss 归因 + 背压）调档，经 `quality_pressure_changed` 信号让 renderer 把档位映射为 decode scale 系数。

### 5.3 解码池化（解码不进热路径）

- `ClipDecodeStream`（clip_decode_stream.py）：**一个 timeline clip 一条解码流**（`StreamKey` 含 clip_id，同素材不同 clip 不共享 cursor——规格书 §10.1 不变量）；顺序播放优先 `read_next_frame` 避免逐帧 seek；大跳变 flush cursor，旧 generation 结果丢弃；`open_async` 在非热路径打开。
- `FrameRingBuffer`（frame_ring_buffer.py）：每流固定容量（默认 48 帧）的 `OrderedDict` LRU 环形缓冲，decode worker 写、scheduler/engine 读（各自加 `threading.RLock`）；窗口淘汰（`evict_outside_window`）+ hit/miss/evict 统计；淘汰帧若携带 GPU payload 用 `safe_release_gpu_frame` 释放，防泄漏。
- `TimelineDecodeScheduler`（timeline_decode_scheduler.py）：按 render plan 的 `LayerSpec` 计算各 layer source frame，预取窗口默认 `[frame-2, frame+8]`，JKL 变速按倍率扩展 lookahead（上界 32）；pending 队列不堆积旧帧；线程模型为 UI 线程 / scheduler 线程 / decode worker 池（`ThreadPoolExecutor`）/ GL 线程。
- `GpuFramePool`：保证已解码帧 GPU-resident，合成前不上传。

---

## 6. 关键调用链汇总

1. **UI 编辑 → 时间线变更**：
   `TimelineView`(交互) → `Command.create(...)` → `CommandStack.execute(cmd, timeline)` → `cmd.apply` 返回新 `TimelineSequence`（或 `(new, updated_cmd)`） → `EditController._on_edit_timeline_changed` 同步 `TimelineSequenceRef` + sequence map → 下游（preview renderer / edit_page 音频签名 / 波形等）响应。
2. **撤销/重做**：`CommandStack.undo/redo(timeline)` → 逆 apply → 新时间线写回 controller。
3. **AI/脚本提交**：`plan → transaction_service → EditProjectService.commit_command(state, timeline_id, cmd, base_revision)` → 乐观锁 → apply → validate → bump → 双层同步（UI 路径的 CommandStack 与此入口的协调见 ai_edit_controller.py:6330 注释——避免绕过 CommandStack 的路径分歧）。
4. **播放（视频）**：`PlaybackControllerTimer._on_tick` → `EditPlaybackController.tick(monotonic)` → `frame_ready` → `TimelinePanel._on_timer_frame_ready`（必要时 `reanchor_to_frame` 回锚）→ `TimelinePreviewRenderer.request_render` → `RealtimePlaybackEngine.request_present` → SourceResolver/Scheduler/delegate → GPU 合成 → `frame_presented`。
5. **播放（音频）**：时间线变更 → `EditPage._rebuild_audio_tracks`（快照校验门 + 签名 diff）→ `AudioManager` 重建轨/decoder → 音频线程 `AudioMixer.mix_samples`（QMutex）→ 音频回调仅测 drift 不抢帧。
6. **导出（音频）**：`TimelineExporter` → `AudioGraphRenderer.configure_from_sequence(sequence)` → 逐帧 `render_track_block / render_bus_stage / apply_master`（与实时同节点序，parity 测试锁定）。

## 7. 改动注意事项（含已知薄弱点）

**模型层**

- 给 `EditTrack`/`TimelineSequence` 加字段：必须同步 `_rebuild` 的参数表与转发体，并排查所有**手工构造**点（模型内 5 个 with_*、commands.py 内约 8 处 `EditTrack(...)`/`TimelineSequence(...)` 手工构造——转场三命令的手工构造已确认丢 14+ 字段，AUD-16201）。首选 `dataclasses.replace` 或补齐 `_rebuild` 通道。
- `with_*` 丢字段是**家族性历史缺陷**：FX-C01（ClipInstance with_effects 曾丢 8 类字段）、AUD-19100（EditTrack 五个 with_* 仍丢 is_sync_locked/track_role）、AUD-16200（InsertLinkedAssetCommand.undo 手工重建 TimelineSequence 丢 subtitle/master 等新字段）。改动后应回归断言：新旧实例除目标字段外逐字段相等。
- automation/effect params 等可变 dict 内嵌于 frozen 模型，快照间存在引用别名（AUD-19104）；不要在命令/渲染层原位修改这些 dict。

**命令层**

- 新命令必须实现 apply/undo 互逆且持久化旧值（返回 updated_cmd 或 `object.__setattr__`），补 undo 往返测试；参考正确样板：DeleteClipCommand、SlipClipCommand、RippleTrimCommand、SetTrackMute/Solo。
- 已知 undo 缺陷清单（改动相关命令前先读 batch-162）：BatchDelete(ripple) undo 误调 `move_cmd.undo`（AUD-16202）、OverwriteClip undo 启发式误删同轨无关片段（AUD-16203）、栈 pop-后-执行丢命令（AUD-16204）、7 类命令旧值不持久化（AUD-16205）、RemoveBus 不恢复 sends（AUD-16206）、AddEffect undo 按"最后一个同类型"回退查找（AUD-16211）等。
- `MacroCommand.from_dict` 只认 3 种子命令（AUD-16210）；新增可序列化命令进入宏时必须同步注册。

**音频图**

- 新增声音相关字段需同时更新 signature、stable_hash、AudioMixer/AudioGraphRenderer 两端消费（三处缺一会导致"改了不重建"或"预览与导出发声不一致"）。
- 路由校验门是激活门：非法图冻结轨道重建但保留旧图播放，不要在门内抛异常打断播放。

**播放链**

- 一切播放 tick 必须过 `engine.request_present`（scrub 走 `request_scrub_present`），不要新增绕过 engine 的旁路渲染路径。
- 热路径禁令：实时 tick 内禁止 `av.open`、磁盘代理生成、同步 master fallback；miss 必须可观测（telemetry/trace），不允许静默 CPU fallback。
- 视频时钟是 master；不要重新把音频帧回调接到 seek。
- fps 变更要同时考虑 `EditPlaybackController.set_fps_rational`、timer interval、automation ramp 的帧→样本换算（P1-20：显式传 fps，避免回退 24fps 兜底）。

---

## 8. 依据的源文件

本文全部内容基于以下源文件（路径相对仓库根 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/`）：

模型层：
- `src/features/edit/core/timeline_v2.py`（3,549 行；TimelineSequence/EditTrack/ClipInstance、with_*/_rebuild、revision）
- `src/features/edit/core/project.py`（5,392 行；MediaAsset/ProjectBin/EditProject、import_asset/remove_bin 等）

命令层：
- `src/features/edit/core/commands.py`（16,251 行；EditCommand Protocol、CommandStack、ProjectCommandStack、MacroCommand、全部具体命令）
- `src/features/edit/ai_editing/edit_project_service.py`（统一提交入口、RevisionConflict、CommitResult、ref↔sequence 双层同步）

音频图：
- `src/features/edit/core/audio_graph_snapshot.py`（AudioGraphSnapshot、build_audio_graph_snapshot、SnapshotHistory、stable_hash）
- `src/features/edit/core/audio_graph_renderer.py`（AudioGraphRenderer、_TrackNode、节点顺序与 de-click）
- `src/features/edit/core/audio_playback.py`（AudioMixer 实时路径、QMutex、签名重建消费端）
- `src/features/edit/ui/edit_page.py`（_rebuild_audio_tracks、路由校验门、_build_audio_timeline_signature、_setup_audio_video_sync、A/V drift）

播放链：
- `src/features/edit/core/edit_playback_controller.py`（EditPlaybackController、tick、JKL、PlaybackControllerTimer）
- `src/features/edit/core/realtime_playback/engine.py`（RealtimePlaybackEngine、request_present、miss/rebuffer 状态机）
- `src/features/edit/core/realtime_playback/timeline_decode_scheduler.py`（LayerSpec、预取窗口、线程模型）
- `src/features/edit/core/realtime_playback/clip_decode_stream.py`（StreamKey、cursor 隔离不变量）
- `src/features/edit/core/realtime_playback/frame_ring_buffer.py`（环形缓冲、GPU payload 释放）
- `src/features/edit/core/realtime_playback/__init__.py`（内核包结构与目标管线）
- `src/features/edit/core/timeline_preview_renderer.py`（engine 装配与委托、request_present_via_engine）
- `src/features/edit/ui/timeline_panel.py`（PlaybackControllerTimer 装配、frame_ready 处理）

佐证审查报告（docs/audit/2026-09-28/batches/）：
- `batch-162-edit-core-commands.md`（commands.py 全文审查：AUD-16200～16219）
- `batch-191-features-edit-core.md`（timeline_v2.py 全文审查：AUD-19100～19109）
