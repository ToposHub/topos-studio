# Critical 复核报告 — color/core（AUD-0200 / AUD-0310 / AUD-0501）

- 复核日期: 2026-09-28
- 复核方: 独立复核子智能体（不预设原结论正确，全部结论基于本次独立 Read/grep 取证）
- 复核对象:
  - [AUD-0200] batch-002-color-core-async-renderer.md — `_do_bake_layer` 引用未定义变量 `render_request_id`
  - [AUD-0310] batch-003-color-core-cache-mgr.md — CacheWarmer 持锁调用 stop_warmup 死锁
  - [AUD-0501] batch-005-color-core-grade-history.md — 历史快照与活动图层共享可变对象

---

## [AUD-0200] `_do_bake_layer` 引用未定义变量 `render_request_id`，AI Bake 每次必然抛 NameError

- 结论: **成立，维持 Critical**
- 独立证据:
  1. 全文 grep `render_request_id` 仅 6 处：115/116（信号注释）、2163（`_do_render_full_resolution` 内，且变量名为 `request_id`）、2319（绑定，位于 `_do_render_impl`（2240 行起）方法体内）、2720（`_do_render_impl` 内使用）、6428（`_do_bake_layer` 内使用）。无模块级 global、无 `self.render_request_id` 属性、无嵌套闭包可提供该名字。
  2. 通读 `_do_bake_layer` 全函数（async_renderer.py:6255–6502）：6278–6283 行从 `data` 仅提取 `layer` / `layer_id` / `source_size` / `full_res_image` / `prefix_layers` 五个键，函数体内不存在任何 `render_request_id` 赋值。6428 行 `request_id=render_request_id` 在 Python 名字解析（local → enclosing → global → builtins）下必然 NameError。
  3. 可达性：6428 位于 `if baked_tex is not None:` 的 `else` 分支（未冻结层必走）。bake 目标层本身保证未冻结——`image_engine.bake_layer`（image_engine.py:4387）对 `layer.is_baked` 早退，故可见的 bake 目标层 100% 进入该分支。
  4. 异常路径：NameError 在 `try`（6339）内抛出 → `finally`（6470）正常释放 VRAM → 异常经 `_handle_command`（async_renderer.py:953）传到 `run()` 主循环，被 520–521 行 `except Exception: traceback.print_exc()` 吞掉（仅 stderr），循环继续。
  5. 信号不发射：`layer_baked_ready` 全文件唯一 emit 点是 6502 行（try/finally 之后的正常路径）；唯一消费方是 image_engine.py:610 `connect(self._on_layer_baked)`。异常路径下 emit 被跳过，AI Bake 完成链（保存 EXR、标记冻结）整体死亡。
  6. 功能路径真实：layer_panel.py bake 按钮 → layer_controller.py:992 `engine.bake_layer(index)` → image_engine.py:4415 `renderer.submit(RenderCommand.BAKE_LAYER, {...})`。且提交的 payload 本身也不含 `render_request_id` 键（即便含，`_do_bake_layer` 也不读取）。
- 与原报告的出入: 无。行号、绑定位置、异常吞噬、信号失效、功能路径全部复核一致。补充一点：修复时仅在 payload 加键无效，必须在 `_do_bake_layer` 函数体内显式提取或传 `request_id=None`。
- 修复建议确认: 成立。在 `_do_bake_layer` 内从 `data` 提取或显式传 `None`，并为 bake 路径补端到端测试。

---

## [AUD-0310] CacheWarmer.start_warmup 持不可重入锁时同步调用 stop_warmup，必然死锁

- 结论: **成立（缺陷确凿、比原报告更易触发），建议由 Critical 降级为 Major**（理由见下）
- 独立证据:
  1. cache_manager.py:1393 `self._lock: threading.Lock = threading.Lock()` — 不可重入锁，确认。
  2. 同线程同步调用链确认（非后台线程间竞争）：`start_warmup` 1425 行 `with self._lock:` 进入临界区，1426 行 `if self._is_running:` 为真时，1428 行在**持有锁的同一调用栈内**直接 `self.stop_warmup()`；`stop_warmup` 1452 行再次 `with self._lock:` — `threading.Lock` 同线程二次 acquire 永久阻塞。纯结构决定，无时序假设。
  3. 触发条件比原报告更宽：`_is_running` 仅在两处置位/复位——1430 行置 True、1454 行（stop_warmup 内）置 False。`_warmup_loop`（1461–1508）自然跑完后**既不清 `_is_running` 也不 set `_stop_event`**。因此不是"预热运行中重启才死锁"，而是**首次 start 之后只要没显式调过 stop_warmup，第二次 start_warmup 必死锁**（无论首次预热是否早已自然结束）。
  4. 配置不设防：`enable_warmup: bool = True`（358 行），1421 行的开关检查默认放行。
  5. 全库调用方核实：grep src/ 与 tests/，`CacheWarmer`/`start_warmup`/`stop_warmup` 在 cache_manager.py 之外仅出现于 tests/color/test_cache_manager.py:314–330——该测试只 start 一次并最后 stop（从不二次 start），不触发死锁分支，测试通过。生产代码零调用方（composite 侧使用独立的 `src/features/composite/render/cache_manager.py`）。
- 严重级评估: 死锁机制本身是 Critical 级（一旦接线，调用线程确定性永久挂起）；但该类当前无任何生产调用路径，现行版本实际风险为零，属"接线即触发"型潜伏缺陷。按"当前实际影响"标准建议降为 Major，由主审按审计规范裁决；若规范将"确定性死锁"无条件定为 Critical，则维持亦可接受。
- 与原报告的出入: 两点。(1) 原报告称触发条件为"预热运行中再次调用 start_warmup"——实际更宽：首次自然结束后再次 start 同样死锁（`_is_running` 从不自动复位），原报告低估了触发面；(2) 原报告对"全仓库无外部调用方"的判断经独立 grep 证实成立（唯一引用是单次 start 的测试）。
- 修复建议确认: 成立（改 `RLock` 或拆出锁内无锁版本 `_stop_locked()`；`_warmup_thread` 读写纳入锁保护）。建议追加：`_warmup_loop` 结束时在锁内复位 `_is_running`，否则即使换成 RLock，"首次自然结束→再 start"仍会走多余的 stop 路径并 join 旧线程。

---

## [AUD-0501] 历史快照与活动图层共享可变对象，undo/redo 静默失效

- 结论: **成立，维持 Critical**（核心机制与至少 4 条真实污染路径独立证实；但原报告引用的 canvas 证据不成立，见"出入"）
- 独立证据:
  1. 快照侧无拷贝：history.py:124–137 `_serialize_layers` 直接 `serialized.append(layer.to_dict(assets_dir=None))`；文件顶部 `import copy`（第 1 行）从未被使用——本应 deepcopy 的意图有痕迹、无实现。
  2. `Layer.to_dict`（layer.py:921–993）返回活动引用：938 `"grading_params": self.grading_params`、943 `"soft_clip_params": self.soft_clip_params`、944 `"qualifier_params": self.qualifier_params`、953 `data["curves_data"] = self.curves_data`、967 `data["plugins"] = self.plugins`，均不拷贝。`MaskShape.to_dict`（131–185）155/156/158/159/165 行的 `center`/`size`/`points`/`grad_points`/`brush_strokes` 同为直接引用。
  3. 真实就地变更路径（每条都足以污染已压栈快照）：
     - image_engine.py:1421 `layer.qualifier_params.update(params)`（`set_qualifier_params`）——qualifier 面板滑杆直连（main_window.py:1592 `qualifier_panel.params_changed.connect(self.engine.set_qualifier_params)`），调色页高频主路径；
     - image_engine.py:1415 `layer.soft_clip_params.update(params)`（`set_soft_clip_params`，main_window.py:1677 接线）；
     - image_engine.py:1326 `layer.grading_params.update(results['grading'])`（`decompile_lut`）；
     - layer.py:699 / 712 / 715（`apply_grade_patch` 的 grading / qualifier / soft_clip 分支就地写）；
     - layer.py:910 `plugin["enabled"] = enabled`（`set_plugin_enabled`，经 plugin_panel.py:610 `_toggle_plugin` 调用且无 push）。
  4. undo 消费读污染数据：image_engine.py:4176 `undo()` → 4178 `history.undo(...)` → 4182 `_restore_state` → 4211 `HistoryManager.deserialize_layers` → `Layer.from_dict` 在 1022 行读 `data["grading_params"]`、1039 行 `.update(data["qualifier_params"])`——读到的即是栈内被就地改写后的同一 dict，撤销后这些字段保持现值。
  5. 时序成立性：压栈点遍布各操作（color_controller.py:118/165、layer_controller.py:270/407/712/952/1546/1583/1845、image_engine.py:1031/4232/4252 等），任一压栈后发生的上述就地变更都会改写栈内快照；undo 恢复的是变更后的值，对该字段撤销静默失效。
- 与原报告的出入: 一处实质性错误、一处弱化：
  1. **canvas 证据不成立**。canvas.py:1822 `self.bezier_points[idx]['pos'] = pos` 改的是画布自建显示列表（990 行初始化、1575–1577 重建），并非 MaskShape 的 points；同步回模型走 `_sync_back`（2055–2085）——它构造**全新**的 norm_points 列表后 emit `mask_shape_changed`，接收端 `process_mask_update`（color_controller.py:1352–1358）以 `s.center = list(v)`、`s.size = list(v)`、`s.points = v` **重绑**。因此蒙版几何（center/size/points）的历史快照不会被画布拖动污染，该子论据应从报告中移除。
  2. edit_controller.py:7144 的 `layer.grading_params[k] = ...` 位于 `_grade_clip_layers` 构造**新建** Layer 的初始化路径，不构成对已压栈快照的污染证据。
  3. 另注：`apply_grade_patch` 的 mixer(702)/effects(705)/curves(718)/plugins(747) 分支是 deepcopy 后**重绑**，经该函数不污染快照——原报告未区分，但 grading/qualifier/soft_clip 三个就地分支足以支撑结论。
- 修复建议确认: 成立。`_serialize_layers` 对 `to_dict` 产物整体 `copy.deepcopy` 即可切断全部别名（`assets_dir=None` 时 to_dict 不产生文件副作用，deepcopy 安全；warper 已 tolist、plugins 为纯 dict/list，deepcopy 无障碍）。若后续在意压栈开销，可仅对已知可变嵌套键定点深拷。
- 维持 Critical 的理由: undo 是核心用户功能；污染字段（qualifier/soft_clip/grading/plugins）覆盖调色页主工作流，失效方式完全静默（无异常、无日志），且反向污染（恢复出的 Layer 与栈内条目共享 nested 结构）同样存在，属历史栈数据完整性破坏。

---

## 复核结论汇总

| 编号 | 原级别 | 复核结论 | 建议级别 |
|------|--------|----------|----------|
| AUD-0200 | Critical | 成立（全部细节吻合，AI Bake 100% 失效、异常被吞、信号不发射） | 维持 Critical |
| AUD-0310 | Critical | 成立（确定性死锁，触发面比原报告更宽；但无生产调用方，仅测试单次 start） | 建议降为 Major（潜伏型，接线即触发） |
| AUD-0501 | Critical | 成立（快照别名 + qualifier/soft_clip/grading/plugins 就地污染路径独立证实；canvas 子证据不成立，应剔除） | 维持 Critical |
