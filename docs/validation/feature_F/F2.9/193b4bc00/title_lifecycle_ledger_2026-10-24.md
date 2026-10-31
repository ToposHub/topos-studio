# F2.9 手工标题、动态图形与模板生命周期 — 证据台账

- 日期：2026-10-24
- 结论：**核心缺口已补（守卫 + 模板生命周期接线）**；六个后续片登记
  （§5），其中滚动片尾真实 roll 动画与跨平台最终像素验收为最大剩余量。
- 提交：`193b4bc00`（feat/f-plan，已 ff-merge fix-ui 并推送）。

## 1. 核验结论摘要（Explore 2026-10-24）

已满足（只记录覆盖）：六类标题入口 + 双击创建；Caption Effects 8 模板
完整 in/out 动画且跟随时长拉伸；轨默认样式 + global/custom scope（改一条
即推全轨）；promote/detach/demote 右键生产入口；cue 侧编辑/undo 对称同步
图形；display_mode=graphic 抑制原生渲染防重复文字；caption_graphics
roundtrip 重开持久化；画布相对布局 + QTextLayout 断行 + 8% title-safe
底边距；缺字体 preflight（warning=建议替代+受影响 cue，error=阻断烧录）。

## 2. 本次修复/接线（按验收点）

| 验收点 | 落地 | 证据 |
|---|---|---|
| 不重复文字/不残留样式（split 维度） | **守卫**：绑定字幕 cue 的图形 clip 禁止刀片 split（此前产生无绑定孤儿半段，同 caption-graphic 资产 id，文本/动画关系断裂）——显式 ValueError 指引切字幕 cue 或先转回 | test_title_lifecycle_guards.py::test_split_bound_graphic_clip_rejected |
| trim 失步守卫 | **守卫**：sync_timing 绑定下图形 clip 禁止 trim（cue 是时序事实源）；detach（快照语义）后允许 | ::test_trim_synced_graphic_clip_rejected / ::test_trim_after_detach_allowed |
| 保存为模板并用于第二条时间线 | **接线**：cue 右键"Save as Title Template…"（样式取 override 或轨默认，五类下拉，title_templates 原子写、同名拒绝）；应用侧本就作用于任意活动时间线——端到端锁定测试（保存→列出→第二时间线 Add+Update→单 Undo 回无覆盖） | ::test_saved_template_applies_to_second_timeline |
| 模板复制/改名/迁移 | **API+UI**：rename_title_template（样式/类别/描述不变换文件，同名拒绝零半更新）+ Titles 树用户模板右键重命名/删除 + 切页刷新（此前保存后须重启面板才可见） | ::test_rename_keeps_style_and_old_removed / ::test_rename_duplicate_rejected |
| 普通片段不受守卫影响 | 回归保护：无绑定 clip split/trim 原样 | ::test_regular_clip_unaffected + 104 项护栏 |

## 3. 关键决策

1. **守卫而非实现双向同步**：图形 clip 的 split/trim 正确实现需要"半段
   补绑定/反向同步 cue"的完整事务设计；在缺失前，显式拒绝 + 指引（改
   cue / 先转回字幕 / detach）比静默产生孤儿半段诚实。cue 侧 split/trim
   的同步链路本就完整（UpdateSubtitleClipCommand → sync_caption_graphic）。
2. **模板 schema 不新增**：存储复用 CaptionStylePreset（title_templates
   原有原子写），rename 走 save-new + delete-old 两步原子组合。
3. **保存入口放 cue 右键**：标题的"当前样式"事实源是 cue（override 或
   轨默认），右键即所见即所存。

## 4. 测试账本

- 新增 tests/edit/test_title_lifecycle_guards.py：7 项。
- 护栏：caption_graphics/split_merge/global_style/title_templates/
  graphic_effects/edit_commands/lift_extract 104 项全绿。

## 5. 登记的后续片（不阻塞核心验收）

1. **滚动片尾真实 roll 动画**：credit_roll 现为静态样式（无爬升/滚动），
   需图形动画域的 offset-over-time 支持。
2. **原生标题 cue 级入出场动画**：`cue_animation_envelope`（graphics.py
   :786-868，含 AUD-15900 修复）是零消费死代码；不 promote 的标题无动画。
3. **restore/同步 flag UI**：`SyncCaptionGraphicCommand` 零接线；detach 后
   无法重新挂接（只能 undo）；sync flag 三开关无 UI。
4. **9:16/1:1 时间线预设 + 绝对字号画幅自适应**（font_size=200 不随 9:16
   缩放）+ 跨画幅/跨平台（Qt vs PIL 断行）最终像素锁定测试。
5. **缺字体替代的用户明确选择 UI**（现在 preflight 报告 + 自动替换）与
   工程字体依赖清单（衔接 F6.2）。
6. **批选批量样式 / 预设变更回推**：build_batch_style_override 与
   propagate_preset_change 零生产调用。

## 6. B 依赖核对

无 Plan B 依赖（命令守卫 + 存储 + Qt UI，跨平台路径）。
