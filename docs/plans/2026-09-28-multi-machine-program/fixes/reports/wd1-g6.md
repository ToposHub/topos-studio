# 修复报告 wd1-g6 — 批 D（Major × 5 AUD）+ GAP-002

- 日期：2026-09-29
- 证据报告：docs/audit/2026-09-28/batches/batch-027-app-controllers.md（AUD-2704..2708）；docs/testcoverage/2026-09-28/GAP_REPORT.md（GAP-002）
- 改动文件（生产代码全部在白名单内）：
  - `src/app/controllers/edit_controller.py`（AUD-2704/2705/2706/2707/2708）
  - `tests/app/test_fix_wd1g6_aud.py`（新建，20 例：五条修复回归 + GAP-002 补口）
  - 既有测试 harness 适配（锁定旧行为协议，见下）：`tests/edit/test_edit_effect_param_no_rebuild.py`、`tests/edit/test_effect_edit_cache_invalidation_wiring.py`、`tests/edit/test_render_cache_job_lifecycle.py`
- 未改 REMEDIATION_PLAN.md；无 git 分支操作/commit/push。

## 逐条结论

| 编号 | 结论 |
|------|------|
| AUD-2704 | **FIXED** |
| AUD-2705 | **FIXED** |
| AUD-2706 | **FIXED** |
| AUD-2707 | **FIXED**（11 处站点 + 新统一提交入口；顺带修复 1 处同族未解包缺陷） |
| AUD-2708 | **FIXED**（3 个入口全部原子化 + 新 `_CreateStampedColorStackCommand`） |
| GAP-002 | **FIXED**（edit_controller.py 补测：新建 20 例回归，覆盖 5 条修复的空层/回退/竞态/undo 路径） |

注：批 D 条目原始行号（1759-1806 / 8575-8577 / 7867-7876 / 2130-2136 等）因本文件已修 AUD-2700/2701/2702/2703 发生漂移；全部按当前代码核实后实施，6 条均仍存在、无一 INVALID。

---

## AUD-2704 [Major/bug] `_on_grading_changed_for_color_stack` 空层 UnboundLocalError

**状态：FIXED**

**根因**：`curves_data`…`raw_params` 等快照字段在 `if self.engine.layers:` 前统一预初始化为
None，唯独 `trackings`/`video_denoise` 只在 if 块内赋值（P0-COLOR-02/P1-COLOR-13 后加的
两段）。引擎无活动层（源未加载/已卸载）时提交调色，`UpdateColorStackCommand.create(
trackings=trackings, …)` 处直接 UnboundLocalError，本次调色修改丢失。

**修复**：在 if 块前补 `trackings = None`、`video_denoise = None`（与其他快照字段同一处
预初始化，附 AUD-2704 注释）。空层提交全链路正常走完：命令进栈、`edit_project_dirty`
置位、渲染管线触发。

**测试**：`test_grading_commit_with_empty_layers_does_not_raise`（空层不崩 +
trackings/video_denoise=None + 提交完成）、
`test_grading_commit_with_layers_collects_tracking_and_denoise`（有层时 tracking_store/
video_denoise_params 正常采集，回归不回归化）。

---

## AUD-2705 [Major/bug] 标题模板 fallback 未解包 MacroCommand 二元组

**状态：FIXED**

**根因**：`_on_title_template_selected` 的无 TimelineView 分支
`new_timeline = macro.apply(timeline)`——`MacroCommand.apply`（commands.py:9913-9934）
按复合命令契约返回 `(new_timeline, updated_command)` 二元组。tuple 被存进
`edit_page._current_timeline`，后续任何 `get_clip_by_id` 即
`AttributeError: 'tuple' object has no attribute 'get_clip_by_id'`。同文件
`_on_edit_effect_params_changed`（4046 区）与 `on_subtitle_interaction_updated` 等处均有
该契约的防御注释/解包，此处遗漏。`UpdateSubtitleClipCommand.apply` 本身返回时间线
（非元组，已核实），不需处理。

**修复**：fallback 改为 `result = macro.apply(timeline); new_timeline = result[0] if
isinstance(result, tuple) else result`，与 4046 区参考实现一致。

**测试**：`test_title_template_fallback_stores_timeline_not_tuple`——用替身 MacroCommand
（apply 返回真契约二元组）走完整 `_on_title_template_selected`，断言
`_current_timeline` 是解包后的时间线对象且非 tuple。

---

## AUD-2706 [Major/risk] RenderCacheWorker 运行中被析构的崩溃竞态

**状态：FIXED**

**根因**：facade 创建 worker 不传 parent（`RenderCacheWorker(parent=None)`），字典条目是
其唯一硬引用；`finished_ok`/`failed` 在 `run()` 内 emit、经队列投递主线程，回调执行时
工作线程可能尚未完全退出——此刻无条件 `del self._render_cache_workers[cache_id]` 触发
C++ QThread 析构 → "QThread: Destroyed while thread is still running" 硬崩溃。同文件
MulticamSyncWorker（6567 区）已用 `worker.finished.connect(worker.deleteLater)` 安全模式。

**修复**（两层防护，均在 `_start_render_cache_worker` / 两个完成回调内）：

1. 注册时连接 `worker.finished.connect(worker.deleteLater)`：连接持有 bound-method 引用
   使 wrapper 在字典删除后仍存活，C++ 析构推迟到线程真正退出后的主线程事件循环
   （canonical 自管理 QThread 模式）。
2. 完成回调的无条件 `del` 改为 `_release_render_cache_worker_slot(cache_id)`：
   仅当 `isFinished()` 为真才丢条目（audit 建议的 isFinished 确认）；未退出时条目保留，
   由新增的 `finished` 驱动 sweep `_on_render_cache_worker_thread_finished`（QObject
   宿主经队列连接到主线程，按 sender 身份移除条目）兜底，避免悬空 wrapper 残留
   （8118 区 idle 调度对已析构 wrapper 的 `getattr(worker, "_manifest")` 会 RuntimeError）。

**测试**：5 例——启动连接 deleteLater + 登记、isFinished 假/真两态的 guarded release、
finished sweep 按 sender 身份清理、`_on_render_cache_failed` 经真实处理函数在未退出时
保留条目、`_on_render_cache_finished` 在已退出时移除条目。

---

## AUD-2707 [Major/bug] 属性面板命令绕过 undo 栈（11 处 hasattr 站点）

**状态：FIXED**

**根因**：`EditPage` 全文件无 `_command_stack`（命令栈在 TimelineView 上），11 处
`hasattr(self.edit_page, '_command_stack')` 分支恒为假，else 直接 `command.apply` 落到
`edit_page._current_timeline`：剪辑参数/效果增删改/启停/重排/关键帧/转场参数等属性面板
编辑全部不可撤销，`old_value` 快照形同虚设，且 `TimelineView._timeline`/
`_current_timeline_sequences`（保存取值源）与渲染器各持旧副本（部分站点连
`_sync_timeline_to_views` 都没有，视图彻底陈旧）。审计列 12 处：11 处现存站点 + 1 处
（transform 拖拽提交）已被 AUD-2703 修复吸收。

**修复**：

1. 新增统一提交入口 `_execute_timeline_edit_command(command, timeline)`：
   - 有 TimelineView（且 `_timeline` 非空）→ `view.execute_command(command)`，返回
     `view._timeline`（命令栈/增量视图更新/项目缓存/自动保存单一入口，经
     `timeline_changed` 信号链同步 `edit_page._current_timeline`、序列缓存、渲染器——
     与 AUD-2703 修复同一模式）；视图存在但无时间线时**回退**直接 apply，命令不丢。
   - 无视图（headless/测试壳）→ 直接 apply + 复合命令 `(timeline, command)` 元组解包 +
     `edit_page._current_timeline` 发布 + `_sync_timeline_to_views`。
2. 11 处站点全部改走该入口：`_on_edit_clip_param_changed`（clip param）、
   `_on_edit_reset_transform`（多选循环）、`_on_transition_param_changed`（转场）、
   `_on_edit_effect_param_changed`、`_on_edit_effect_params_changed`（macro）、
   `_on_edit_effect_keyframe_toggled`、`_on_edit_effect_enabled_changed`、
   `_on_edit_effect_remove_requested`、`_on_edit_effect_reorder_requested`、
   `_on_edit_clip_effect_params_committed`（音频效果）、`_on_edit_effect_add_requested`。
   各站点冗余的尾部 `load_timeline`/`_sync_timeline_to_views` 同步移除（视图路径由信号链
   增量同步，避免全量重建闪烁；回退路径由入口内 `_sync` 承担）；FX-H02 缓存失效、
   属性面板刷新、音频重同步、silent 渲染器热替换等站点特有后处理全部保留。
3. **同族顺带修复**：`_on_edit_clip_effect_params_committed` 的旧 fallback 对
   MacroCommand 二元组从不解包（AUD-2705 同缺陷家族）——入口统一解包后自然修复。
   `_resync_audio_after_effect_change` 的过时注释（"不经过 timeline_view 命令栈"）按新
   路径订正（signature 去重保证两路径下二次调用无副作用）。

**测试**：7 例——视图路径经 execute_command 提交且不重复同步、无视图回退 apply+发布+
同步、复合命令元组解包、视图无时间线不丢命令、站点级（effect reorder）经视图命令栈
提交。

---

## AUD-2708 [Major/bug] 调色栈创建与绑定/盖章非原子，undo 后悬空引用

**状态：FIXED**

**根因**（三个入口同一病灶）：

- `_ensure_clip_has_color_stack`：`CreateColorStackCommand` 进媒体池命令栈（可撤销），
  随后 ownership 盖章是裸 `replace(project, color_stacks=…)`（无栈记录），clip 绑定
  `UpdateClipColorStackCommand.apply` 直接改时间线（时间线命令栈无记录）。媒体池
  Ctrl+Z 撤销建栈后 clip 的 `color_stack_id` 悬空；redo 更糟——
  `CreateColorStackCommand.apply` 每次重放生成**全新 stack_id**
  （`ColorStackData.create` → uuid4），redo 重建的栈与仍在的绑定必然失配且盖章丢失。
- `_on_use_shared_grade_requested`：同款"命令栈建栈 + 裸 replace 盖 asset_default 章"。
- `_on_make_local_grade_copy_requested`：`project.create_color_stack` 干脆完全不经命令栈
  （整个本地化操作不可撤销），快照复制也是裸 replace。

**修复**（commands.py 不在白名单，命令定义在本文件内）：

1. 新增模块级 `_CreateStampedColorStackCommand`（项目域命令，EditProject）：
   `apply` 一次完成建栈 + `stamp_fields` 盖章（含本地副本的 v2 快照字段），**缓存
   `ColorStackData` 对象**使 undo/redo 间 stack_id 稳定（重放插回同一对象）；`undo`
   整栈移除，同 id 栈被他人替换时记录 displaced 并恢复、不破坏他人状态。
2. 三个入口的建栈+盖章（+快照复制）合并为**单个该命令事务**，经
   `media_pool_panel.get_command_stack()` 提交——单步 undo 整栈移除、单步 redo 同 id +
   完整盖章恢复。本地副本路径的 P1-COLOR-16 v2 快照字段清单（grading/curves/hsl/
   mixer/soft_clip/lut/layers/active_layer_index/trackings/raw_params）逐字段保留。
3. clip 绑定改走时间线命令路径：`_ensure_clip_has_color_stack` 的
   `UpdateClipColorStackCommand` 与 `_apply_clip_color_stack_change`（两个 grade 右键
   入口共用）统一经 AUD-2707 的 `_execute_timeline_edit_command` 提交——绑定成为有
   undo 记录的时间线命令（`_on_use_shared_grade_requested` docstring 宣称的"切换本身
   可经时间线命令撤销"首次真正成立）。

**测试**：9 例——命令单元（apply/undo/redo 单元性 + redo 同 id 同对象、displaced 恢复、
他人替换时不破坏）、`_ensure_clip_has_color_stack`（盖章字段 + 绑定发布 + 单步 undo/
redo）、shared 入口（asset_default 盖章 + shared 绑定 + 单步 undo/redo）、local 入口
（源栈保留 + v2 快照复制 + 绑定 + 单步 undo 移除本地栈且源栈无恙 + redo 快照完整）。

---

## GAP-002 [Major/test-gap] edit_controller.py 低覆盖补测

**状态：FIXED（本批范围内的补口）**

GAP_REPORT 记录 edit_controller.py 覆盖 32%（缺失 3398 行）。按证据文档补测策略
（正常/边界/异常/取消 + mock 外部 IO/GPU），新建 `tests/app/test_fix_wd1g6_aud.py`
20 例，针对本批 5 条 Major 修复补上此前零覆盖的高风险路径：空引擎层提交、无
TimelineView 宿主的标题模板回退、渲染缓存 worker 生命周期竞态、统一命令提交入口的
四条路径、调色栈三入口的 undo/redo 原子性。全部最小 mixin 宿主 + 替身，offscreen，
不碰 GPU。模块整体覆盖率的进一步提升（3398 缺失行不可能一批补完）留待 GAP 专项
批次，本批以"修复即回归"方式封闭与本批缺陷对应的缺口。

---

## 既有测试适配（锁定旧行为协议）

3 个既有测试失败，均为 **SimpleNamespace 壳宿主绑定真实方法、逐项 stub 协作者**的
harness 形状锁定（测试自身的契约断言——面板不重建/缓存失效/manifest 发布——全部保持
不变，未改任何断言）：

1. `tests/edit/test_edit_effect_param_no_rebuild.py`：壳缺新协作者
   `_execute_timeline_edit_command` → 按该文件既有的 `__get__` 绑定模式补绑真实方法。
2. `tests/edit/test_effect_edit_cache_invalidation_wiring.py`：同上。
3. `tests/edit/test_render_cache_job_lifecycle.py`：
   `test_cache_completion_publishes_manifest_to_preview_renderer` 壳缺
   `_release_render_cache_worker_slot` → 补绑真实方法（Mock worker 的
   `isFinished()` → True，条目按旧 del 语义移除）。

## 验证记录

- `py_compile`（.venv/bin/python -m py_compile）全部通过：edit_controller.py、
  test_fix_wd1g6_aud.py 及 3 个适配的既有测试文件。
- 新建测试：`tests/app/test_fix_wd1g6_aud.py` — **20 passed**。
- 邻接既有测试（.venv/bin/python -m pytest）：
  - 合并运行（tests/app edit_controller 相关 11 文件 + tests/color/test_grade_clipboard
    + tests/edit 的 caption_graphics/title_templates/mg_idle_cache/render_cache_lifecycle/
    effect_param/cache_invalidation/reset_transform/crop_param 等）— **203 passed**。
  - 全量 `tests/edit/` — **8522 passed, 18 skipped**（含标题模板、转场、渲染缓存、
    timeline_view、序列生产全量）。
  - `tests/color/test_timeline_clip_strip_lazy_thumbnails.py`、`tests/ui/` 相关
    （p0 wiring × 2、p1_controller_deps）— **73 passed**。
  - 全量 `tests/app/` — 1027 passed，3 failed：`test_fix_wd1g4_aud.py` 的
    AUD-2401/2402 用例（CompositeControllerMixin 导出回调编组域）。该文件**零**
    edit_controller 引用（grep 核实），失败位于另一批工作者改动域
    （composite_controller.py 等本会话未触碰），属并发批次/环境性既有失败，与本批
    改动无关联。
- 因本批修复而失败的既有测试：0（上述 3 个 harness 形状适配后全部通过，断言未动）。
