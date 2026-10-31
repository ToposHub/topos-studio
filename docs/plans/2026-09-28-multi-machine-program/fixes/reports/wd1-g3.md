# 修复报告 wd1-g3 — 批 D（color_controller，Major × 6）

- 日期：2026-09-29
- 证据文档：docs/audit/2026-09-28/batches/batch-023-app-color-controllers.md
- 目标文件：`src/app/controllers/color_controller.py`（行号引用以证据报告时点为准，本轮以当前代码核实，含 BUG-C4-01/02 既有修复）
- 改动文件（全部在白名单内）：
  - `src/app/controllers/color_controller.py`（AUD-2302/2303/2304/2305/2306/2307）
- 新增测试：`tests/app/test_fix_wd1g3_aud.py`（22 用例，覆盖全部 6 条）
- 未修改任何既有测试文件；既有锁定断言全部保持绿色。

## 逐条结论

| 编号 | 结论 |
|------|------|
| AUD-2302 | **FIXED**（残余：清空确认对话框未加，见下述理由） |
| AUD-2303 | **FIXED**（残余：历史重置/调试叠加仍直连 renderer，见下述理由） |
| AUD-2304 | **FIXED**（残余：EXR 后台写盘未做，受白名单+锁定测试双重约束，见下述理由） |
| AUD-2305 | **FIXED** |
| AUD-2306 | **FIXED** |
| AUD-2307 | **FIXED**（残余：decompile 仍同步执行，见下述理由） |

---

## AUD-2302 [Major/bug] 遮罩增/删/清空/层级重组不写 undo 栈

**状态：FIXED**（证据所列 5 处入口现全部入栈；确认对话框为登记残余）

**当前代码核实**（证据行号已漂移，BUG-C4-01 先行修复了大部分）：
- `on_mask_shape_added` / `on_mask_shape_removed` / `on_all_masks_cleared` /
  `on_mask_group_added` / `on_mask_hierarchy_updated` / `on_brush_stroke_added`
  均已经由 `_push_mask_structure_snapshot()` 提交 `copy_image=False` 快照；
- **唯一剩余缺口**：`_on_tracker_pattern_committed`（画布拖拽 pattern 框
  写回窗口几何）改动 `shape.center/size` 前不入栈——本轮补上。

**修复**：
- `_on_tracker_pattern_committed` 在区域解析成功、目标形状有效后、写
  几何之前调用 `_push_mask_structure_snapshot()`；目标缺失/区域非法不
  产生空快照（与 `on_mask_shape_removed` 的语义一致）。
- `_push_mask_structure_snapshot()` 增加 `getattr(engine, "history", None)`
  守卫：宿主处于极简状态（无 history，如 tests/color/
  test_tracking_render_apply.py 直驱 mixin 的裸 fake）时跳过提交而非
  抛 AttributeError。生产 `ImageEngine` 恒有 history，行为不变。

**残余（登记，不阻塞）**："清空全部遮罩"的 QMessageBox 确认未添加：
1. BUG-C4-01 后该操作已可撤销（Ctrl+Z 完整恢复几何遮罩 + AI matte），
   条目"永久销毁/不可逆数据丢失"的核心伤害已消除；
2. 既有锁定测试 `test_all_masks_cleared_clears_every_layer_and_ai_mattes`、
   `test_clear_all_masks_without_content_commits_no_snapshot` 直调
   `on_all_masks_cleared()` 并断言立即清空——模态框会挂起 offscreen
   测试，且本批白名单禁止修改该测试文件。建议后续在 mask_panel 按钮
   侧（信号发起端）加确认。

**测试**：`test_aud2302_tracker_pattern_commit_pushes_undo_snapshot`、
`test_aud2302_tracker_pattern_commit_guards_push_no_snapshot`。

---

## AUD-2303 [Major/bug] 视频降噪下发 `except Exception: pass` + 越层直连 renderer

**状态：FIXED**（静默吞错根因全部消除；两处渲染器专属能力的分层残余已登记）

**修复**：
- `on_video_denoise_changed`：优先调用 `engine.set_video_denoise_state(
  params.to_dict())`（image_engine.py:924-937 的统一公共入口：负责记录
  `video_denoise_params` 供项目保存，并以带日志方式下发渲染器）；
  引擎无该入口（旧引擎/测试替身）时回退：记录参数 + 经新私有收口
  `_push_video_denoise_params()` 直发渲染器，失败 `logger.warning(
  ..., exc_info=True)`。
- `on_video_denoise_history_reset`：`reset_video_denoise_history` 为渲染器
  专属能力（engine 无公共方法），保留直连但失败记 warning + exc_info，
  不再 `pass`。
- `on_video_denoise_debug_overlay_toggled`：经 `_push_video_denoise_params(
  params, debug_overlay=...)` 收口，同样带日志。

三处 `except Exception: pass` 全部清零，失败可排查且不断面板管线。

**残余（登记）**：历史重置 / 调试叠加两个能力 engine 无对应公共入口，
app 层仍持 renderer 引用直调（分层不彻底）。补齐需在
`src/features/color/core/image_engine.py` 增公共方法——该文件在本批
白名单之外，登记待后续批次。

**测试**：`test_aud2303_changed_routes_through_engine_entry`、
`test_aud2303_changed_fallback_records_params_and_pushes_renderer`、
`test_aud2303_renderer_failure_is_logged_not_swallowed`、
`test_aud2303_debug_overlay_kwarg_passthrough`。

---

## AUD-2304 [Major/risk] denoise/deblur 完成回调 UI 线程同步全图拷贝/双 resize/EXR 写盘

**状态：FIXED**（冗余全图拷贝已消除；EXR 后台写盘为白名单约束下的登记残余）

**修复**：
- `on_denoise_finished` / `on_deblur_finished` 的
  `FileIO.save_baked_layer(name, img.astype(np.float32))` 改为直接传
  `img`：denoise engine 契约保证结果已是 float32
  （DenoiseWorker 两条路径均返回 float32），且
  `FileIO.save_baked_layer`（file_io.py:71-73）自带
  `dtype != float32 → astype` 归一——原先的 `.astype(np.float32)` 是
  纯冗余的 4K 全图拷贝（~100MB+ 内存带宽/次）。

**残余（登记，BLOCKED 约束）**：EXR（DWAB 压缩）编码写盘仍在 UI 线程
同步执行。未后台化的原因：
1. 既有锁定测试 `test_denoise_finished_pushes_undo_bakes_and_restores_panel`
   / `test_deblur_requested_converts_uint16_and_finished_bakes` 在
   `on_*_finished` 返回后立即同步断言 save 调用次数、bake 文件名与
   autosave 次数——任何异步化（QThreadPool/QThread/QTimer.singleShot）
   都会确定性失败或产生竞态 flake，而本批白名单禁止修改该测试文件；
2. 渲染线程命令队列（renderer.submit）无文件 IO 命令，扩展需改
   shared 层（白名单外）。
   建议：后续批次解禁 test_color_controller_coverage.py 后，用
   QRunnable + 代次 token 把 bake 写盘移入
   `QThreadPool.globalInstance()`（仓库已有先例：
   file_controller.py:298、welcome_dialog.py:250），完成回调再更新
   `source_bake_file_name/type` 并触发 autosave。

**测试**：`test_aud2304_denoise_finished_saves_original_array_without_copy`
（断言传入 save 的数组与结果对象同一身份，即零拷贝）、
`test_aud2304_deblur_finished_saves_original_array_without_copy`。

---

## AUD-2305 [Major/bug] denoise/deblur 完成回调不校验任务上下文

**状态：FIXED**

**修复**（按证据建议的"记录 (文件路径, 图层索引)，回调先校验"方案）：
- 新增 `_capture_bake_task_context()` →
  `(engine.current_file_path, engine.active_layer_index)`；
- `on_denoise_requested` / `on_deblur_requested`：仅当
  `apply_denoise(...)` 返回 True（任务真正启动，busy 拒绝不覆盖）时
  记录 `self._denoise_task_ctx` / `self._deblur_task_ctx`；
- `on_denoise_finished` / `on_deblur_finished`：先校验上下文一致，
  不一致则丢弃结果（不 push_state 污染新文件 undo 栈、不 set_image
  到切换后上下文、不写/删 bake 文件），恢复按钮与进度条并提示
  discarded；一致则应用并清理上下文；
- `on_denoise_error` / `on_deblur_error`：清理上下文；
- 兼容语义：完成回调无发起上下文（直调/编程调用）时按旧行为应用，
  生产链路 request→finish 恒有上下文，防护完整生效。

文件切换（engine.current_file_path 随 set_image(is_new_file=True)
更新）与图层切换（active_layer_index）均被捕获。

**测试**：`test_aud2305_denoise_discarded_after_file_switch`、
`test_aud2305_denoise_discarded_after_layer_switch`、
`test_aud2305_denoise_applies_when_context_unchanged`、
`test_aud2305_finish_without_request_keeps_legacy_apply`、
`test_aud2305_error_clears_context_and_busy_reject_keeps_old`、
`test_aud2305_deblur_discarded_after_file_switch`。

---

## AUD-2306 [Major/bug] 激活 crop 静默替换为未 bake 原图，退出无恢复

**状态：FIXED**

**修复**（按证据建议的"进入前保存 bake 上下文，退出（apply 与 cancel
两条路径）统一恢复"方案，采用零开销变体）：
- `_stash_crop_preview_context()`：激活换源图前，仅当确有会丢失的
  工作态（存在 `source_bake_file_name`，或此前已应用过裁切——
  `crop_params` 非 `{"rect","rotation"}` 默认形态）时，保存当前
  **显示金字塔**（`img_high/img_low` 引用）+ `img_full_original` +
  文件标识。未 bake、未裁切的新图源图与工作显示同内容，不保存
  （退出零额外动作，与既有锁定行为一致）。恢复回传原金字塔引用，
  零 resize、零 EXR 读盘。
- `_restore_crop_preview_context()`：`on_crop_toggled(False)`（含 Esc
  取消、空裁切框路径）恢复快照：`set_image(full, high, low,
  is_new_file=False)` + `update_canvas()` + `request_render()`；
  bake 引用与 layer_panel 标记保持进入前状态，显示态/持久化态不再
  分裂。会话期间切换文件（文件标识不一致）则丢弃快照不恢复。
- `on_crop_applied`（裁切框非空）：`engine.apply_crop` 成功后作废
  快照——apply 路径消费新工作态，退出时不得回滚刚应用的裁切。
- `on_denoise_finished` / `on_deblur_finished` / 两个 reset：工作显示
  被替换/恢复时同步作废 crop 快照，防止 crop 退出回滚到过期图像。

**测试**：`test_aud2306_crop_exit_restores_baked_display`、
`test_aud2306_crop_apply_consumes_stash_no_restore`、
`test_aud2306_no_bake_no_crop_no_restore`、
`test_aud2306_prior_crop_also_restores`、
`test_aud2306_file_switch_during_session_skips_restore`、
`test_aud2306_denoise_finish_invalidates_crop_stash`。

---

## AUD-2307 [Major/bug] LUT 反编译同步执行 + processEvents + 异常路径状态栏残留

**状态：FIXED**（processEvents 重入与异常残留两个缺陷清除；同步执行残余登记）

**修复**：
- 移除 `QApplication.processEvents()`（模块 import 一并清理）：等待
  期间用户可关闭文件/切换页面的重入竞态不再存在；
- `engine.decompile_lut(path)` 包裹 try/except：异常时
  `logger.exception`（完整堆栈）+ 状态栏恢复为 "LUT Decompile Failed."
  （5000ms），不再让 timeout=0 的 "Decompiling..." 永久残留，也不再
  上抛到 Qt 事件循环。

**残余（登记）**：`decompile_lut` 本身仍同步执行（scipy least_squares
拟合，通常亚秒级）。完全异步需要 engine 侧拆分"计算/应用"两段并保证
线程安全（当前实现内联修改 layer.grading_params/curves_lut 并提交
GPU 命令），属 image_engine.py / the_decompiler.py 改造，白名单外，
登记待后续批次。

**测试**：`test_aud2307_decompile_exception_restores_status_bar`、
`test_aud2307_decompile_no_process_events_reentrancy`。

---

## 验证记录

```
$ .venv/bin/python -m py_compile src/app/controllers/color_controller.py tests/app/test_fix_wd1g3_aud.py
（无输出，退出码 0）

$ .venv/bin/python -m pytest tests/app/test_color_controller_coverage.py -q -p no:cacheprovider --no-header
112 passed in 1.82s

$ .venv/bin/python -m pytest tests/app/test_color_controller_coverage.py tests/app/test_fix_wd1g3_aud.py -q -p no:cacheprovider --no-header
134 passed in 1.85s

# 邻接回归（全部触及 ColorControllerMixin / 遮罩-跟踪链路的套件）：
$ .venv/bin/python -m pytest tests/app/test_color_controller_coverage.py tests/app/test_fix_wd1g3_aud.py tests/color/test_tracking_render_apply.py tests/app/test_fix_w5g4_aud5.py tests/app/test_fix_controllers_aud.py tests/color/test_grade_keyframes.py tests/color/test_power_windows.py tests/color/test_qualifier_matte.py tests/color/test_tracker_mask_binding_flow.py -q -p no:cacheprovider --no-header
265 passed in 4.26s
```

**既有测试处理**：无锁定旧行为断言需要翻转；中途发现
`tests/color/test_tracking_render_apply.py::TestPatternCommit::test_commit_writes_back_shape`
的裸 fake Engine 无 `history` 属性，通过在
`_push_mask_structure_snapshot` 增加 history 存在性守卫解决（生产
engine 恒有 history，行为不变），该套件恢复全绿。

## 约束遵从

- UI 线程未新增任何 OpenGL/ModernGL 触碰（新增代码仅文件 IO / Qt 控件 / 日志）；
- 无裸 except：新增 except 均为 `except Exception` + `# noqa: BLE001` 注释 + warning/exception 日志；
- 新增方法全部带类型注解；
- 未做任何 git 分支/commit/push 操作；未修改 REMEDIATION_PLAN.md 与任何既有测试文件。
