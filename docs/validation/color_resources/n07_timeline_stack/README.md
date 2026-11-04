# N07 — Timeline/组链真实栈模型（2026-11-04）

## 交付（提交点见 git log N07）

- **Timeline 栈 owner 模型**：`EditProject.timeline_grade_stacks`
  （{timeline_id: stack_id}；栈本体入 `color_stacks`——保存/R09 闭包/
  graph_locked 锁/undo 全部免费复用）；`SetTimelineGradeStackCommand`
  受限事务（before 冲突拒绝、undo 对称）；IO settings 键
  `timeline_grade_stacks`（容错反序列化）；ColorStackRef 序列化把
  `timeline` 映射到既有合法域 `timeline_adjustment`。
- **scope 菜单接线**：timeline scope 懒建中性栈（含迁移初始图，
  一命令一 undo）后绑定——编辑落 timeline 栈；切回 clip/组重绑不串写
  （session/generation 守卫原样）；未登记路径保持只读解绑（fail-visible）。
- **统一链序 pre→main→trim→post→timeline**：`CLIP_STACK_CHAIN` 契约
  +5 段；`chain_extra_params` 追加 `_chain_segment="timeline"`（4 个
  消费端 Edit CPU/GPU 预览、导出 CPU/GPU 按段标签自动落位）；无登记
  退化 4 段（`resolve_timeline_stack` 单点查询）。
- **Composite 共享栈跨组归属歧义**：`chain_for_stack` 多 clip 引用且
  (timeline, group) 键不一致时 fail-visible warning（此前静默取首个）。

## 验证

- tests/color/test_timeline_grade_stack.py（5）：命令 apply/undo/冲突、
  链段 5 段/退化、IO roundtrip、宿主懒建+绑定+编辑落栈+clip 不串写、
  undo 恢复。
- tests/app/test_color_graph_app_host.py 更新 timeline scope 语义断言
  （绑定/幂等/回 clip 不串写）；既有组/链/闭包/缓存回归 120+ 全绿。
- 原生验证（本目录）：`TOPOS_COLOR_GRAPH_RENDER=1 --workflow` passed。

## 已知边界（如实登记）

- Color 页单栈视图不含 timeline 段（输出链语义；E27 统一 plan 后续）。
- Resolve 实机对照（N15 同款）：timeline 栈创建/编辑的实机 UI 行为
  待人工核验（脚本 API 不覆盖 scope 菜单）。
- tests/edit 全量套件含单进程原生崩溃债务（N16 登记，clean HEAD 复现）。
