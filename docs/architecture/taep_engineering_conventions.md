# TAEP 工程约定（Phase 0 固化）

> 本文档把 `docs/ai_editing_protocol_and_implementation_plan_2026-07-31.md` 第 2.1、4、6
> 章的工程级硬约束固化为可引用的实现规约。后续 phase 的所有代码、测试与文档都以此为准。

## 1. 时间与坐标合同（最高优先级）

这是协议中最容易造成长片累计误差的部分。**任何**写入时间线的代码（command、AI plan
compiler、脚本、XML 导入）都必须遵守：

1. **内部位置一律整帧**。所有写入时间线的时刻是非负 `int` 帧号。
   - `timeline_start` / `source_start` ≥ 0。
   - `duration` 必须是正整数（>0）。
   - `end` 永远由 `start + duration` 推导，**不允许**同时传一个可能矛盾的 `end`。

2. **区间采用左闭右开 `[start_frame, end_frame)`**。
   - clip 的 `timeline_end = timeline_start + timeline_duration`，下一 clip 的 start
     可以等于上一 clip 的 end（相邻不重叠）。
   - overlap 在 `EditTrack.__post_init__` 与各 `command.apply` 内强制报错。

3. **帧率保存为有理数 `Rational(num, den)`**，例如 `24000/1001`，**不是** `23.976` 浮点数。
   - `TimelineSequence.fps` 是 `Rational`（精确）。
   - `TimelineSequenceRef.fps` 是 `float`（ref 层降级，仅供 UI/池显示）。
   - TAEP manifest 的 `accepted_fps` 使用字符串形式 `"24000/1001"`。

4. **秒/时码 → 帧的转换只在边界层做**，且策略固定：开始点 `floor`、结束点 `ceil`，
   并把转换结果与 warning 返回。内部不存浮点秒位置。

5. **29.97/59.94 的 drop-frame 只影响显示和某些交换格式的 timecode**，不改变内部
   帧位置。

### 必须拒绝的情况（domain 校验）

- fps ≤ 0、分母 ≤ 0；
- 负帧、零时长；
- source 范围越过已知素材时长；
- 同一非叠加视频轨的重叠 clip；
- 转场引用不存在的 clip、跨轨 clip 或不相邻 clip；
- 目标 track 类型与 clip 类型不匹配；
- 采用不支持的变速/倒放/复合效果却没有声明降级策略；
- 源素材离线而操作要求探帧、预览或导出。

## 2. revision 与乐观锁

- `EditProject.revision` 与 `TimelineSequence.revision` 都是**单调递增的逻辑版本号**
  （`int`，默认 0），**不是** schema 版本。
- **bump 只发生在提交边界**：`EditProjectService.commit_command` 成功落地一次原子
  变更后，调用 `sequence.with_revision_bumped()`（timeline +1）并 bump project revision。
- preview / dry-run / diff / 模拟**不 bump**，因此 proposed timeline 的 revision 与
  base 相等，便于随后用 `base_revision` 做乐观锁比较。
- 用户手工修改、脚本修改、AI 提交、外部 XML 导入**都必须走同一个提交入口**才能保证
  revision 正确。
- 冲突**不自动 last-write-wins**：`base_revision != current_revision` 抛
  `RevisionConflict`（诊断码 `TAEP_REVISION_CONFLICT`）；AI 重新读取 snapshot、重新规划。

## 3. 唯一提交入口

`EditProjectService`（`src/features/edit/ai_editing/edit_project_service.py`）是无 UI、
无 Qt、无 GPU 的纯逻辑服务，是 UI / AI / 脚本 / 交换导入的统一写入入口。

- **输入**：`ProjectSequences`（两层状态：`project` 持 ref、`sequences` map 持完整
  `TimelineSequence`）+ `EditCommand`（含 `MacroCommand`）。
- **流程**（TAEP §6.1 两阶段提交的内核）：
  1. key 一致性检查（`assert_sequences_consistent`，对应 `TAEP_TIMELINE_NOT_FOUND`）。
  2. 乐观锁比较（`base_revision == current revision`）。
  3. 在副本上 `command.apply`（不可变模型天然在“副本”上操作）。
  4. domain 校验（`validate_timeline`：引用完整性 + transition 邻接；overlap 已在
     apply 内强制）。
  5. bump revision（提交边界）。
  6. 同步 ref↔sequence 两层状态（name/fps/wh/sr/format_settings 漂移时重建 ref）。
  7. 返回 `CommitResult`；任一步失败都**不产生半套 timeline**。
- 它**不**做：UI 刷新、GPU/预览刷新、Qt 信号、文件保存、模型调用、XML 解析。调用方
  拿到 `CommitResult` 后自行刷新。

## 4. ref↔sequence 两层状态

`EditProject.timelines` 只存轻量 `TimelineSequenceRef`；完整的可编辑 `TimelineSequence`
由 controller 以 `dict[timeline_id, TimelineSequence]` 形式单独维护。两层 key 必须始终
一致：

- 保存时 `EditProjectIO.save_project` 做 key 一致性检查（`edit_project_io.py:483`）。
- 运行时 `EditProjectService.assert_sequences_consistent` 在提交边界做同样的检查。
- **禁止**只修改 `EditProject.timelines` 或只替换 controller sequence map。任何写入都
  必须经 `EditProjectService`，由它负责两层同步。

## 5. 线程与 GPU 边界

- **UI 线程不得调用 ModernGL**；AI 提交操作只更新编辑状态（纯数据），预览与 GPU 工作
  仍沿现有异步渲染路径执行。
- `ai_editing` 包是纯数据/逻辑层：**不实例化 Qt widget，不触碰 ModernGL**，可在任意
  线程被调用。涉及 UI 刷新的部分由 controller 在主线程订阅结果后执行。
- 长任务（模型调用、分析、预览、导出）复用现有 export queue / job API，可取消、可恢复，
  绝不阻塞 Qt UI 线程。

## 6. 导出独立性

导出**不可**依赖当前打开的 UI 页面、当前 viewer texture 或脚本上下文；必须从工程状态
与 timeline 重新构建（`TimelineExporter` 从 `TimelineSequence` + `TimelineExportConfig`
出发）。Preflight（audio/video-effects/fonts）在任何“看似成功”的文件写出前强制运行。
