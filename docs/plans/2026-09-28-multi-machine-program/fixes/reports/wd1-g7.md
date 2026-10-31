# 修复报告 wd1-g7 — 批 D（Major ×13：AUD-2800~2805 / GAP-007 / AUD-2901~2903 / AUD-2920 / AUD-3000~3001）

- 范围：`src/app/controllers/file_controller.py`（AUD-2800~2805、GAP-007）、
  `src/app/controllers/layer_controller.py`（AUD-2901~2903）、
  `src/app/controllers/media_analysis_controller.py`（AUD-2920）、
  `src/app/controllers/menu_toolbar_controller.py`（AUD-3000~3001）
- 证据：docs/audit/2026-09-28/batches/batch-028-app-controllers.md、
  docs/audit/2026-09-28/batches/batch-029-app-controllers.md、
  docs/audit/2026-09-28/batches/batch-030-app-controllers.md、
  docs/testcoverage/2026-09-28/GAP_REPORT.md（GAP-007）
- 测试：新建 `tests/app/test_fix_wd1g7_aud.py`（41 用例，全绿）。
  未翻转任何既有用例；全部邻接既有套件原样通过。

## 结论总表

| 编号 | 结论 | 摘要 |
|------|------|------|
| AUD-2800 | **FIXED** | `_confirm_save_before_close` 的 Discard 分支置 `_user_discarded_changes` 标志；`_persist_active_project_before_close` 据此跳过 `save_router.save()`，「不保存并关闭」不再把脏状态写回主工程 |
| AUD-2801 | **FIXED** | `dropEvent` 改走 `_open_media_path(files[0])`，与菜单 Open Media 的视频/图像/邻接工程分发一致，不再一律 `load_image` |
| AUD-2802 | **FIXED**（白名单内根因） | 新增 `_make_gl_viewport_current`/`_release_gl_viewport_current` 所有权跟踪：三个回读路径（PBO 回读 / 直接回读 / viewport 抓取）的 finally 全部补 `doneCurrent()`，且嵌套调用（PBO 内进直接回读）不释放外层上下文；viewport 抓取路径补上此前完全缺失的 finally。渲染侧迁移见「偏差说明」 |
| AUD-2803 | **FIXED** | `_SourceViewerDecodeWorker._run` 显式跟踪已解码帧所有权：构造/提交中途抛异常时在 except 分支 `safe_release_gpu_frame` 释放；提交成功后所有权移交渲染队列（不释放） |
| AUD-2804 | **FIXED** | `load_video` 的面板重置、`save_router.clear_cache`、`_add_to_recent_files` 移到 `video_controller.load_video` 成功之后；失败分支按快照回滚 `current_file_path`/`current_file_is_video`（此前被清空且 `current_file_is_video` 残留 True） |
| AUD-2805 | **FIXED** | 新增 `_confirm_project_replacement_for_image`：`load_image` 在旧素材保存之后、新素材状态写入之前，对「替换异类工程」（时间线工程/不含该素材的工程）先走 `_close_current_project()` 保存确认，取消即中止加载；`_ensure_image_project` 自身保留同款兜底（返回 bool） |
| GAP-007 | **PARTIAL**（按计划策略推进） | 本批 13 条修复配套 41 个行为锁定用例，其中 file_controller 直接覆盖 537 条语句（单独运行 13%，此前全库基线 22%）；覆盖新增路径：closeEvent/Discard、dropEvent、DeckLink 三条回读、解码 worker 三条所有权路径、load_video 成败两会话、load_image 替换中止、_ensure_image_project。3,070 缺失行的补齐按 GAP_REPORT 的分批策略继续推进，本批不宣告关闭 |
| AUD-2901 | **FIXED** | `_composite_startup_fallback` 的 except 分支改用模块级 `logger.warning`（原引用未定义局部名 `_logger`，触发即 NameError 中断启动回退） |
| AUD-2902 | **FIXED** | `request_render` 的渲染管线异常改 `logger.exception`（完整堆栈）+ 状态栏提示「渲染失败」，不再单行无堆栈 `logger.error` 吞掉 |
| AUD-2903 | **FIXED** | 新增 `_push_layer_history`（`copy_image=False`，与 `on_layer_reset` 同款）：增/删/移/显隐/重命名/`on_adjustments_reset`/`on_reset_all_click` 统一在变更前压撤销栈；no-op（最后一层删除、显隐/重命名值未变）不产生空步骤 |
| AUD-2920 | **FIXED** | `get_asset_status` 契约对齐：瞬态 pending/running/failed 由 controller 生命周期缓存（`_emit_status` 记录，`on_project_changed` 清空）优先返回；service 报 ready 时经 `is_valid` + 从 `project_getter` 解析的当前指纹（size+mtime_ns，与 `_fingerprint_from_asset` 同口径）判 stale；docstring 重写为真实查询顺序 |
| AUD-3000 | **FIXED** | `QLabel` 补入 menu_toolbar_controller 的 QtWidgets 导入；GPU 标签装配失败改 `logger.debug` 留痕，不再 `except Exception: pass` 静默 |
| AUD-3001 | **FIXED** | 新增 `apply_panel_opacity`：停靠 dock 用逐 dock `QGraphicsOpacityEffect`（windowOpacity 对非顶层窗口无效），浮动 dock 用 `setWindowOpacity`；`opacity>=1.0` 移除全部效果保持零合成开销；`_wire_panel_opacity_reapply` 在 dock 浮动/停靠切换时重套正确机制；`setup_statusbar` 末尾统一应用持久化配置（docks 先于它创建，启动路径补齐） |

## 1. AUD-2800 [Major/bug/M] — FIXED

证据复现（batch-028）：`_confirm_save_before_close` 的 Discard 分支
`return True` 放行关闭后，closeEvent 尾部 `_persist_active_project_before_close()`
无条件 `save_router.save()`（与 Ctrl+S 同路由）——用户丢弃的未保存修改仍被
写入主工程文件。

修复（file_controller.py）：

1. `_confirm_save_before_close` 入口置 `self._user_discarded_changes = False`，
   Discard 分支返回 True 前置 True（Save/Cancel/无未保存修改均保持 False，
   重复调用语义稳定）。
2. `_persist_active_project_before_close` 在 composite flush 之后检查该标志：
   为 True 时 `logger.info` 记录并跳过 `save_router.save()`（composite flush
   仍执行——它只同步内存态与恢复快照，不写主工程）。

测试：`test_close_discard_skips_final_save`（Discard → 不落盘）、
`test_close_save_choice_still_persists`（Save → 照常落盘）、
`test_close_without_unsaved_changes_persists`（无未保存 → 不弹窗且照常落盘，
回归锁定）。既有 `test_close_persists_project_even_without_batch_queue_failure`
（SimpleNamespace 宿主，无该标志）经 `getattr(..., False)` 兼容，原样通过。

## 2. AUD-2801 [Major/bug/M] — FIXED

证据复现：`dropEvent` 对拖入的任意文件调 `load_image(files[0])`，拖入 mp4/mov
进入图像加载路径（`FileIO.load_image` 解不出帧 → 误导性「找不到文件」错误框）。

修复：`dropEvent` → `self._open_media_path(files[0])`，与 `open_file` 菜单同源：
视频 → `load_video`（含 Color/Library 页拒绝逻辑）、图像 → 邻接 .topos 工程优先、
否则 `load_image`。测试锁定 `test_drop_event_routes_through_open_media_path`
与 `test_open_media_path_routes_video_to_load_video`。

## 3. AUD-2802 [Major/risk/M] — FIXED（白名单内根因）

证据复现：`_on_color_monitor_frame_ready`/`_on_color_output_monitor_ready` 在
UI 线程触发三条回读路径，`makeCurrent()` 后从不 `doneCurrent()`（viewport
抓取路径连 finally 都没有），破坏 Qt 的 context-current 簿记；直接回读还会
被 PBO 回读嵌套调用。

修复（file_controller.py）：

1. `_make_gl_viewport_current(gl_viewport) -> bool`：比较
   `QOpenGLContext.currentContext() is gl_viewport.context()`——已 current
   （嵌套调用）返回 False 复用外层上下文；否则 `makeCurrent()` 返回 True。
2. `_release_gl_viewport_current(gl_viewport, made_current, *, source)`：
   仅所有权方 `doneCurrent()`（异常吞为 debug 日志，finally 永不抛）。
3. 三个调用点接入：`_submit_color_monitor_pbo_readback`（finally 补）、
   `_submit_color_monitor_direct_readback`（finally 补，嵌套时不释放外层）、
   `_grab_color_viewport_monitor_frame`（新增 finally）。恢复 GL 绑定状态
   （`_restore_gl_readback_state`）仍在 doneCurrent 之前执行。

偏差说明：审计首选建议「回读迁移到拥有 GL context 的渲染侧（engine/
AsyncRenderer 做 PBO 回读后投递 MonitorRGBAFrame）」需修改
`async_renderer.py`/`image_engine.py`，超出本批白名单（4 个控制器文件）。
按审计给出的下限建议（「至少在 finally 中补 gl_viewport.doneCurrent()」）
实施根因修复，并额外解决嵌套释放竞态（盲补 doneCurrent 会让嵌套的直接
回读把 PBO 路径仍在使用的上下文释放掉——所有权跟踪是必要条件而非装饰）。
渲染侧迁移作为后续架构项保留。

测试：`test_make_gl_viewport_current_ownership`（取得/复用两态）、
`test_grab_viewport_releases_context_on_render_failure`/`_on_success`
（抓取失败/成功都 doneCurrent）、`test_direct_readback_releases_context_on_success`
/`_on_gl_failure`（纯 Python 假 GL 模块驱动全流程——真实 PyOpenGL 在无当前
上下文时是 C 级段错误，不能用真实模块测失败路径）。

## 4. AUD-2803 [Major/risk/M] — FIXED

证据复现：`safe_release_gpu_frame(decoded_frame)` 只覆盖 `frame_type == 'cpu'`
与 `submit() == False` 两条路径；`create_edit_render_task`/`layer_data` 构造/
`submit()` 自身抛异常时，已解码帧（CVPixelBuffer/IOSurface/GPU 纹理引用）既不
释放也不提交，异常降为 debug 日志。

修复：`_run` 循环体入口 `decoded_frame = None`；cpu 兜底与 submit 拒绝后清引用；
submit 成功后清引用（所有权移交渲染队列）；except 分支对非 None 引用
`safe_release_gpu_frame` 后再上报 cpu_fallback。

测试（真实 worker 线程 + monkeypatch `gpu_frame.safe_release_gpu_frame` 与
`edit_facade.create_edit_render_task`）：构造抛异常 → 释放+兜底；
submit 拒绝 → 释放+兜底（回归锁定）；submit 成功 → 不释放、无兜底（所有权
移交锁定）。注：测试用 `Qt.ConnectionType.DirectConnection` 连接信号——
pytest 无事件循环，queued 连接永远收不到。

## 5. AUD-2804 [Major/bug/M] — FIXED

证据复现：面板 reset / `current_file_path = path` / `save_router.clear_cache()` /
`_add_to_recent_files` 全部发生在 `video_controller.load_video(path)` 之前；
加载失败（损坏文件/解码器错误）后旧视频会话被「卸载」，失败分支还把
`current_file_path` 清 None 且 `current_file_is_video` 残留 True。

修复：

- 快照 `prev_file_path`/`prev_is_video` → 赋新路径（保持加载过程中信号读到的
  路径语义不变）→ 加载成功分支内依次：面板重置、`clear_cache`、
  `_add_to_recent_files`（均在 `_load_video_project` 之前，面板先复位再由工程
  数据回填，保持原意图「防止旧视频的调色应用到新视频」）；失败分支回滚
  快照 + 状态栏错误。

偏差说明：审计建议「将面板重置与 save_router 缓存清理移到 load_video(path)
成功之后；失败分支保留旧会话状态并将 current_file_is_video 复位」——已实施；
「复位」按「恢复到快照值」实现（失败后旧会话的路径/标志原样保留，面板与
保存路由从未被改动）。`video_controller.load_video` 内部对旧视频的解码器级
卸载属 video_playback.py 行为，不在白名单内。

测试：`test_load_video_failure_preserves_old_session`（面板/缓存/路径/最近文件
全部未动 + 错误提示）、`test_load_video_success_applies_session_swaps_after_load`
（成功路径回归锁定）。

## 6. AUD-2805 [Major/bug/M] — FIXED

证据复现：视频/时间线模式下（`current_file_path` 为 None，时间线素材从未
加载进 Color），`load_image` 的旧工程保存（`if self.current_file_path`）被
跳过，`_ensure_image_project` 无确认地用新图像工程替换 `_current_edit_project`
并清空 `_current_timeline_sequences`——未保存的时间线剪辑静默丢失。

修复：

1. 新增 `_confirm_project_replacement_for_image(path) -> bool`：project 为
   None、或「无时间线且含该素材」（同一图像自己的工程）→ True 不打扰；
   否则（时间线工程 / 不含该素材的工程）→ `self._close_current_project()`
   走 Save/Discard/Cancel 保存确认。
2. `load_image` 在「旧素材保存之后、新素材任何状态写入之前」调用守卫——
   取消时文档与面板状态原样保留、加载就地中止（放在旧保存之后保证图像→图像
   切换时旧图调色先经 save_router 落盘，与原顺序兼容）。
3. `_ensure_image_project` 返回 bool 并内置同款兜底（保护潜在其它调用路径；
   正常流程 `load_image` 已先行确认，不会二次弹窗）；取消时不创建新工程。

时间线 clip 路径不受影响：`page_controller` 的 TIMELINE_CLIP 分支用
`create_standalone_session=False`，守卫被跳过；`_load_standalone_project`
先设好含该素材的工程再进 `load_image`，同样不触发确认。

测试：4 个守卫单测（关闭确认执行/取消中止/同素材跳过/无工程放行）+
`_ensure_image_project` 兜底取消 + `load_image` 取消后不解码。

## 7. GAP-007 [Major/test-gap/L] — PARTIAL（按计划策略推进）

GAP_REPORT 排序 #7：file_controller.py 22% 覆盖（3,935 语句缺 3,070）。其补测
策略为「每批 = 1 个模块……新增 coverage 测试实际跑到全绿」，即分批推进。

本批贡献：`tests/app/test_fix_wd1g7_aud.py` 41 用例中 24 个直接驱动
file_controller（单文件运行覆盖 537/3,935 语句 = 13%；media_analysis_controller
单文件 63%）。新覆盖路径（此前 0 覆盖）：`_confirm_save_before_close` /
`_persist_active_project_before_close` / `_has_unsaved_changes`、`dropEvent`、
`_open_media_path` 视频分支、`_make_gl_viewport_current`/`_release_gl_viewport_current`/
`_grab_color_viewport_monitor_frame`/`_submit_color_monitor_direct_readback`、
`_SourceViewerDecodeWorker._run` 三条所有权路径、`load_video` 成败两会话、
`load_image` 替换中止、`_confirm_project_replacement_for_image`/
`_ensure_image_project`。

不宣告关闭：22% → 目标覆盖率的补齐需要独立的分批投入（GAP_REPORT 明示
P2 首批 30 个模块的分批节奏），本批以修复配套行为锁定为主。

## 8. AUD-2901 [Major/bug/M] — FIXED

`_composite_startup_fallback` 的 except 分支引用 `_logger`——该名仅在
`_on_color_output_changed` 内部定义为局部变量（模块级是 `logger`），触发即
NameError 沿 QTimer 回调向上传播。改为模块级 `logger.warning`，保持
「记录并继续」容错语义。全文件复查：其余 `_logger` 使用点（8 处）均在
定义它的 `_on_color_output_changed` 作用域内，无同类问题。

测试：`test_composite_startup_fallback_tolerates_hook_failure`——
`enter_page_hook` 抛 ValueError 时不再 NameError、caplog 收到 WARNING、
回退流程继续（`_is_active` 仍被置位）。

## 9. AUD-2902 [Major/bug/M] — FIXED

`request_render` 的 `except Exception as e: logger.error(f"Error: {e}")` 把整个
渲染管线（引擎不变量破坏/提交失败）压成一行无堆栈日志。改为
`logger.exception`（含渲染原因标签）+ `status_bar.showMessage`（I18N.tr 键
`status.render_failed`）用户可见提示；状态栏本身不可用时降级 debug（不因
提示失败二次抛出）。异常仍不上抛（保持 request_render 所有调用点的既有契约）。

测试：`test_request_render_logs_pipeline_exception_with_traceback`——
`render_pipeline` 抛 RuntimeError：不向上抛、caplog ERROR 记录带 `exc_info`、
状态栏提示一次。

## 10. AUD-2903 [Major/process/M] — FIXED

`on_layer_added`/`on_layer_removed`/`on_layer_moved`/`on_layer_visibility_changed`/
`on_layer_renamed`/`on_adjustments_reset`/`on_reset_all_click` 此前不压 history
（engine 侧 `add_layer`/`remove_layer`/`discard_changes`/`reset_all_layers`
均无 push_state），Ctrl+Z 跳过结构变更回退更早快照，且 `_trigger_autosave`
已持久化不可撤销变更。

修复：新增 `_push_layer_history()`（真实 `HistoryManager.push_state`，
`copy_image=False`——结构/重置操作不触碰源像素，符合 history 模块
「源帧不可变」契约），七个入口在变更**前**统一压栈：

- 增/移：无条件压（必然变更）。
- 删：仅在 `len(layers) > 1 and 0 <= index < len(layers)`（引擎对最后一层
  静默拒绝，no-op 不产生空撤销步骤）。
- 显隐/重命名：仅值真正变化时压（程序化 no-op 不产生空步骤），渲染/自动
  保存仍无条件执行（保持既有行为）。
- 两个 reset：压栈后调用引擎（快照封存重置前参数，测试断言快照值）。

无图像会话（`img_full_original is None`）：显式跳过（push_state 同语义 no-op）。

测试：7 个用例，含真实 HistoryManager + 真实 Layer 序列化——断言撤销栈
快照封存的是**变更前**状态（层数/顺序/重置前 exposure 值）、no-op 不入栈、
无图像会话不抛不压。

## 11. AUD-2920 [Major/bug/M] — FIXED

原实现：docstring 声称「优先用 scheduler 的 pending/running」但 scheduler
从未被查询；`AnalysisService.get_status` 只返回 unknown/ready/pending，
running/failed 恒不可达；`STATUS_STALE` 分支不存在（is_valid 从未被调用）。

修复（media_analysis_controller.py）：

1. `_asset_status` 缓存（`_emit_status` 记录、`on_project_changed` 清空）：
   pending/running/failed 瞬态优先返回——它们只存在于请求生命周期信号，
   service 不跟踪。
2. service 报 ready 时 `_is_asset_stale`：`_current_fingerprint` 从
   `project_getter` 解析素材（`project.assets[asset_id].path`）按
   size+mtime_ns 构造 `AssetFingerprint`（与 ai_editing_mixin
   `_fingerprint_from_asset` 同口径），`service.is_valid` 不匹配 → STALE。
   指纹不可解析（无 getter/虚拟素材/文件缺失）/is_valid 异常一律
   「不误报」返回 False。
3. docstring 重写为真实查询顺序。

测试：瞬态三态可达（propose 公共入口 → pending；生命周期 → running/failed）、
真实 AnalysisService+ingest 造 ready 记录后 os.utime 改 mtime → STALE、
无 project_getter 不误报、工程切换清缓存回 service 派生。既有
`test_get_asset_status_reflects_service` 原样通过。

## 12. AUD-3000 [Major/bug/M] — FIXED

`QLabel` 未在 menu_toolbar_controller 导入（导入清单 15 个 QtWidgets 类中
无它），`setup_statusbar` 的 GPU 信息标签处 `QLabel(...)` 必然 NameError 且被
`except Exception: pass` 吞掉。修复：导入补 `QLabel`；装配失败改
`logger.debug("GPU info label unavailable", exc_info=True)`（onnx_env 不可用
等环境差异可排查，不再零痕迹）。

测试：`test_menu_toolbar_module_imports_qlabel`（`mtc.QLabel is QLabel` 锁定，
防回归删除导入）+ `test_gpu_info_label_no_longer_name_errors`
（`show_gpu_info=True` 时 setup_statusbar 完整执行到 Ready 消息）。

## 13. AUD-3001 [Major/bug/M] — FIXED

两层缺陷：(1) 偏好预览对停靠 dock 调 `setWindowOpacity`——停靠中的
QDockWidget 非顶层窗口，Qt windowOpacity 对其无效，预览大概率无视觉效果；
(2) `panel_opacity` 全库唯一调用点就是这个瞬时预览，保存的值重启后永不生效。

修复（menu_toolbar_controller.py）：

1. `apply_panel_opacity(opacity)`：浮动 dock → `setWindowOpacity` + 移除效果；
   停靠 dock → 逐 dock `QGraphicsOpacityEffect`（按审计建议）+ 复位 windowOpacity；
   `opacity >= 1.0` → 全部恢复无效果路径（默认配置零合成开销）。数值钳制
   [0,1]、类型防御、单 dock 失败降级 debug 不中断。
2. `_on_appearance_preview('panel_opacity')` → 复用上述方法（预览与启动同源）。
3. `_wire_panel_opacity_reapply`：`dock.topLevelChanged` → 重套正确机制
   （浮↔停切换后机制不同），幂等接线。
4. `setup_statusbar` 末尾读取持久化 `panel_opacity`（非数值回退 100）统一应用
   ——main_window 初始化顺序 `setup_docks()` → `setup_statusbar()`，docks
   已就绪（已核 main_window.py:541/554）。

偏差说明：效果安装仅在 `opacity < 1.0` 时发生（用户显式设置，滑杆范围
80–100），默认路径不引入合成开销；含 QOpenGLWidget 的 dock 在效果开启时
经 Qt 纹理拷贝合成，属该设置项的预期代价（该机制即审计建议方案）。

测试：5 个用例——停靠效果/浮动 windowOpacity（offscreen 平台 8bit 量化按
1/255 容差）、`opacity=1.0` 移除效果、偏好预览路由、浮↔停切换重套、
`setup_statusbar` 启动应用持久值（QMainWindow+Mixin 宿主实测）。

## 验证记录

py_compile（.venv/bin/python，Python 3.12.13）：

```
$ .venv/bin/python -m py_compile \
    src/app/controllers/file_controller.py \
    src/app/controllers/layer_controller.py \
    src/app/controllers/media_analysis_controller.py \
    src/app/controllers/menu_toolbar_controller.py \
    tests/app/test_fix_wd1g7_aud.py
PY_COMPILE_OK (all 5 files)
```

新建测试 + 邻接既有测试（全部通过）：

```
$ .venv/bin/python -m pytest tests/app/test_fix_wd1g7_aud.py -q
41 passed in 1.95s

# 直接相关邻接（file/layer/media_analysis/save 路由/控制器修复批）
$ .venv/bin/python -m pytest tests/app/test_fix_wd1g7_aud.py \
    tests/app/test_media_analysis_controller.py tests/app/test_project_save_routing.py \
    tests/app/test_fix_layer_ai_library_aud.py tests/app/test_fix_controllers_aud.py \
    tests/app/test_fix_w5g4_aud5.py -q
91 passed

$ .venv/bin/python -m pytest tests/app/test_color_stack_switch_persistence.py \
    tests/app/test_color_page_audio_bridge.py tests/app/test_color_scrub_settle_nonblocking.py \
    tests/app/test_color_timeline_clip_playback_mapping.py \
    tests/app/test_composite_per_image_isolation.py tests/app/test_project_manager_startup.py -q
93 passed

$ .venv/bin/python -m pytest tests/edit/test_media_pool_analysis_ui.py \
    tests/edit/test_media_pool_delete_routing.py tests/edit/test_media_pool_drop_to_bin.py \
    tests/edit/test_media_pool_finder_phase1.py tests/app/test_ai_editing_composition_root_r1.py -q
70 passed

$ .venv/bin/python -m pytest tests/app/test_fix_wd1g3_aud.py tests/app/test_fix_wd1g5_aud.py \
    tests/color/test_auto_color.py tests/color/test_grade_clipboard.py tests/color/test_grade_keyframes.py -q
136 passed

$ .venv/bin/python -m pytest tests/color/test_presets_gallery.py tests/color/test_scope_live_refresh.py \
    tests/color/test_scope_input_source.py tests/color/test_power_windows.py \
    tests/color/test_tracking_render_apply.py tests/color/test_ai_plugin_pixel_contract.py \
    tests/edit/test_timeline_preview_renderer.py tests/app/test_dock_layout_controller_coverage.py -q
300 passed

$ .venv/bin/python -m pytest tests/app/test_color_controller_coverage.py \
    tests/app/test_deliver_controller_coverage.py tests/edit/test_edit_project_initialization.py \
    tests/edit/test_interchange_ui.py tests/edit/test_playback_debug_trace.py -q
277 passed
```

合计：新建 41 + 邻接 967 全绿，无一既有用例需翻转。

已知无关失败（非本批引入，按锁定旧行为协议未处理）：
`tests/app/test_fix_wd1g2_aud.py` 10 个用例失败于
`make_composite_render_provider(...)` 返回值缺 `.provider` 属性——该函数位于
`src/app/controllers/ai_workflow_composite_render.py`（另一工作组的在途重构，
非本批白名单文件；本批四个文件均未被该测试引用，失败机理与本批改动无关）。

## 附注：工作树事故与恢复记录

验证过程中一次 `git stash push`（含未跟踪测试文件的 pathspec）失败后，紧随的
`git stash pop` 误弹了仓库中既有的他人 stash（"WIP on dev: 73f3a1f"），在
composite 目录制造了冲突。已完整恢复：冲突文件按 ours 侧还原、stash 残留文件
清除、索引 reset；恢复后 `git status` 与事故前完全一致（31 个修改文件 = 其它
工作组在途修改 + 本批 4 个文件；10 个未跟踪 = 其它工作组产物 + 本批测试文件），
既有 stash 条目原样保留，本批全部改动与测试经恢复后复跑确认无损（41+50 全绿）。
