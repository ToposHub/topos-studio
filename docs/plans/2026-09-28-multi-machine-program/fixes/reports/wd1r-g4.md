# 修复报告 wd1r-g4 — 批 D（composite_controller，Major × 7，重派轮）

- 日期：2026-09-29
- 证据文档：docs/audit/2026-09-28/batches/batch-024-app-composite-controller.md
- 目标文件：`src/app/controllers/composite_controller.py`（重派基线为干净版本，
  行号引用以证据报告时点为准；本轮以当前代码核实，证据行号整体漂移约 +40）
- 改动文件（全部在白名单内）：
  - `src/app/controllers/composite_controller.py`（AUD-2401～2407 全部 7 条）
- 新增测试：`tests/app/test_fix_wd1g4_aud.py`（26 用例，覆盖全部 7 条）
- 未修改任何既有测试文件；未做 git 分支/commit/push 操作。

## 逐条结论

| 编号 | 结论 |
|------|------|
| AUD-2401 | **FIXED**（控制器侧信号桥 + 去 processEvents 重入 + 图片路径忙碌态；残余见下述） |
| AUD-2402 | **FIXED** |
| AUD-2403 | **FIXED** |
| AUD-2404 | **FIXED**（缓存失效/ICC 两处保留 best-effort `except Exception` 但不再静默，见下述） |
| AUD-2405 | **FIXED**（防御性修复；当前 CompositePage 初始化为 `{}`，崩溃为潜在态，见下述） |
| AUD-2406 | **FIXED** |
| AUD-2407 | **FIXED**（能力表驱动；对话框现状下 hdr 场景不可达，见下述） |

---

## AUD-2401 [Major/risk] 视频/序列渲染回调闭包直接操作 Qt 控件

**状态：FIXED**

**当前代码核实**：证据所述回调闭包（原 1215-1270）存在；`on_progress` 内
逐帧 `QApplication.processEvents()`、派发前 processEvents、图片路径渲染前
processEvents 三处重入窗口均在。

**修复**：
1. 新增模块级工厂 `_make_render_callback_bridge()`（与既有
   `_make_render_cancel_filter` 同款惰性 PyQt 导入约定）：progress/
   finished/error 三路 Qt 信号。`_run_composite_video_render` 把 UI 更新
   闭包只挂在桥信号上，交给 exporter 的是 `bridge.*.emit`——跨线程发射
   时 AutoConnection 自动退化为 QueuedConnection，UI 槽恒在 UI 线程执行，
   线程安全性不再依赖 exporter 实现（exporter 现状确实经自身信号回到主
   线程，见 composite_render_worker.py `_on_worker_*`，但那是本控制器
   之外的实现细节）。
2. 去掉 `on_progress` 内逐帧 `processEvents()` 与视频路径派发前的
   `processEvents()`（异步派发后控制权自然回到事件循环，进度条随事件
   循环绘制）。
3. 同步图片路径保留唯一一次渲染前 `processEvents()`（同步渲染冻结主
   线程前让进度条有机会绘制，去掉反而退化为无反馈冻结），但新增
   `_composite_image_rendering` 忙碌标志 + try/finally 复位，把该
   processEvents 重入窗口内再次触发的渲染直接拒绝（"渲染期间用忙碌态
   屏蔽重复触发"）。

**残余（登记，不阻塞）**：同步图片渲染本身仍占住主线程（既有架构，
改异步超出本批白名单）；`QApplication.processEvents` 在图片路径保留
一次（理由如上，重入已由忙碌态闭合）。

**测试**：`test_aud2401_exporter_receives_signal_bridge_emitters`、
`test_aud2401_bridge_marshals_cross_thread_emit_to_ui_thread`（真实
跨线程 emit → 断言不在工作线程同步执行、经 processEvents 在 UI 线程
投递）、`test_aud2401_video_render_never_calls_process_events`、
`test_aud2401_image_render_busy_guard_blocks_reentrant_trigger`
（processEvents 重入窗口内再触发被拒绝）。

---

## AUD-2402 [Major/bug] 重复启动渲染静默覆盖 `_composite_render_exporter`

**状态：FIXED**

**当前代码核实**：原 1209/1240/1262/1278-1297 对应现文件
`_run_composite_video_render` / `_cleanup_composite_render_exporter`；
启动前确无 `is_exporting` 守卫，回调闭包无条件清理共享引用，问题存在。

**修复**（三层）：
1. `_run_composite_video_render` 入口检查既有
   `self._composite_render_exporter.is_exporting`——正在导出时弹
   warning 对话框（新增 i18n key `composite.render_output.already_rendering`）
   并直接 return，不覆盖引用、不装配 UI。
2. 非导出态的残留 exporter（历史任务未走清理回调）在装配新 UI 前先
   `self._cleanup_composite_render_exporter()` 原地清掉，保证引用与
   过滤器/光标/tooltip 从零装配、不被静默覆盖。
3. 回调闭包捕获本次 `exporter` 实例：`on_finished`/`on_error` 先比对
   `self._composite_render_exporter is exporter`，不一致（新渲染已接管）
   直接返回；`_cleanup_composite_render_exporter(exporter)` 增加可选
   参数做同样比对——旧任务的回调不再可能拆掉新任务的取消过滤器与
   引用。既有调用方（无参）语义不变。

**测试**：`test_aud2402_busy_exporter_rejects_second_render`、
`test_aud2402_finish_cleans_own_exporter_and_ui`、
`test_aud2402_stale_callbacks_cannot_touch_newer_render`、
`test_aud2402_stale_idle_exporter_cleaned_before_new_render`。

---

## AUD-2403 [Major/bug] `exporter.export()` 同步抛异常时 UI 状态全部残留

**状态：FIXED**

**当前代码核实**：原 1264-1270 对应现文件 exporter 构造 + `export()`
调用，无 try/except；此前已执行 progress_bar.show/setCursor/
installEventFilter，异常时全部残留，且图像路径的 finally（原 1115-1116）
在视频路径无等价物。

**修复**：构造 + 派发包入 try/except；异常时 `logger.exception` 后调用
同一 `on_error(str(e))`——隐藏进度条、状态栏"渲染失败"、critical 对话框、
`_cleanup_composite_render_exporter(exporter)`（摘过滤器、复位光标/
tooltip、清理 exporter、清引用），与正常错误回调完全一致的状态复位。
构造失败（exporter 仍为 None）时 `on_error` 的实例比对自动跳过、照常
复位。

**测试**：`test_aud2403_export_sync_exception_resets_ui`（export 抛
ValueError：进度条隐藏、过滤器摘除、tooltip 清空、光标复位、引用清空、
exporter.cleaned、critical 弹窗、状态栏失败消息）、
`test_aud2403_exporter_constructor_exception_resets_ui`（构造抛
RuntimeError 的等价复位）。

---

## AUD-2404 [Major/bug] 多处宽泛 except 吞错

**状态：FIXED**（证据所列 4 处 + teardown 全部处理；其中两处保留
best-effort `except Exception` 但不再静默，理由如下）

**当前代码核实与逐处修复**（证据行号 → 现行号）：
1. 原 178-181 → 现 `_on_composite_graph_changed`：
   `invalidate_comp_clip_cache` 失败由 `except Exception: pass` 改为
   `logger.warning(..., exc_info=True)`。保留 Exception 是因为
   `timeline_preview_renderer` 属跨模块 API、异常面未知；但不再静默——
   正是"循环播放命中旧帧"bug 复现时会留下完整诊断。
2. 原 203-207 → 现 `_flush_composite_to_edit_before_leave`：切页序列化
   失败由 debug + 静默 return 改为 `logger.warning(..., exc_info=True)` +
   状态栏告知用户（新增 i18n key `composite.leave_flush.serialize_failed`，
   `hasattr(self, "status_bar")` 守卫兼容极简宿主）。用户编辑面临丢失时
   不再无感。
3. 原 415-435 → 现 `_remember_active_comp_clip`：`get_comp_clip_by_graph_id`
   的 `except Exception + debug` 收窄为
   `except (AttributeError, KeyError, TypeError, ValueError) + warning`，
   与 `_prepare_composite_data_for_open` 的异常面及
   `CompClipData.from_dict`（全 `.get` 带默认值）的现实失败面一致。
4. 原 1095-1098 → 现 `_run_composite_image_render`：`embed_icc` 失败由
   `except Exception: pass` 改为 `logger.warning(..., exc_info=True)`——
   ICC 嵌入是保存后的增强步骤，失败不推翻已成功的保存，但必须留痕。
5. 原 1283-1290 → 现 `_cleanup_composite_render_exporter` teardown：两处
   `except Exception: pass`（noqa BLE001）收窄为 `except RuntimeError +
   warning`（Qt wrapped-C++ 对象失效是这两处唯一现实异常），`exporter.cleanup()`
   的 `except Exception + warning` 原本已留痕，维持。

**测试**：`test_aud2404_cache_invalidation_failure_is_logged`（含
exc_info 断言）、`test_aud2404_leave_flush_serialize_failure_notifies_user`
（warning + 状态栏消息）、`test_aud2404_remember_active_comp_clip_narrowed_exceptions`、
`test_aud2404_icc_embed_failure_warns_but_save_succeeds`（失败不掩盖
已成功的保存与打开导出目录）。

---

## AUD-2405 [Major/bug] `_last_metadata` 存在但为 None 时渲染输出 AttributeError

**状态：FIXED**（防御性修复；潜在态说明如下）

**当前代码核实**：证据所述 `getattr(self.composite_page, "_last_metadata",
{}).get("aov", {})` 原样存在（现 `_on_composite_render_output`）。核实
`composite_page.py:217` 当前初始化为 `{}`，5881 行赋 `meta`（metadata_ready
载荷）、1377/832/9147 行重置为 `{}`——即当前代码路径下崩溃为**潜在态**
（防御形态错误，一旦上游把初始/重置值改为 None 即触发）。条目所指代码
模式属实，按防御性根因修复处理。

**修复**：抽取 `_collect_available_render_passes(graph_data) -> set[str]`
（渲染对话框 passes 的唯一来源），内部先
`getattr(self.composite_page, "_last_metadata", None) or {}` 归一 None
再取 aov；顺带使该逻辑可测。

**测试**：`test_aud2405_none_last_metadata_does_not_crash`、
`test_aud2405_missing_last_metadata_attribute`（属性整体缺失）、
`test_aud2405_aov_metadata_unions_scene3d_extras`（AOV 与 scene3d
附加 pass 合并语义不回归）。

---

## AUD-2406 [Major/risk] 工程文件图数据数值字段未校验即 int() 强转

**状态：FIXED**

**当前代码核实**：证据所列三组 `int(... or ...)` 裸转换原样存在：
`_setup_timeline_clip_context` 的 graph width/height 与 duration_frames、
`_resolve_output_resolution` 的 Output 节点 params width/height。

**修复**：新增 `_safe_int(value, default, minimum=0)` 静态工具（完整类型
注解）：坏值（字符串垃圾/None/0）回落 `default`，转换结果按 `minimum`
钳制非法负数；`or` 回退语义逐点保持（0/None → 默认值，与旧行为一致）。
替换点：
- `_setup_timeline_clip_context`：graph_data 的 width/height
  （minimum=1）与 duration_frames；
- 同函数内 timeline 的 width/height getattr（证据未点名但为同型崩溃面，
  时间线同样源自工程文件，语义零变化）；
- `_resolve_output_resolution`：Output 节点 params width/height
  （minimum=1）。

`_sync_composite_graph_to_edit_project` 内 `int(ow)` 已有 isinstance 守卫，
不涉及。

**测试**：`test_aud2406_safe_int_units`（垃圾字符串/None/0/空串/合法
数字串/float 截断/负值钳制）、
`test_aud2406_resolve_output_resolution_survives_corrupt_data`（"3840px"→
默认、"3840"→3840、负值→1）、
`test_aud2406_setup_context_survives_corrupt_graph_dimensions` /
`test_aud2406_setup_context_accepts_numeric_strings`（经假 facade 直驱
`_setup_timeline_clip_context`，损坏图数据不再 ValueError，页面切换
路径不崩）。

---

## AUD-2407 [Major/bug] `keep_linear=(ext == "exr")` 单点比较破坏浮点 HDR 线性语义

**状态：FIXED**（能力表驱动；可达性核实见下）

**当前代码核实**：`keep_linear=(ext == "exr")` 原样存在
（`_run_composite_image_render`）。核实可达性：
`composite_render_dialog._IMAGE_FORMATS = png/jpeg/tiff/exr/webp`——
当前对话框**不提供** hdr 等其他浮点格式；且 `FileIO.save_image` 对 .hdr
落入 uint8 转换分支（写侧不支持浮点 hdr），tiff 在本控制器 dtype 表与
FileIO 中均为 uint16 显示编码。即证据举例的 .hdr 场景在当前 UI 与
写盘链路下不可达，**崩溃面为"新增浮点格式时单点比较静默漏登记"的
结构性风险**（同类真实先例：quick-export 路径的 .toos 浮点档即因该
模式出过 bug，后由 `FileIO.toos_export_keeps_linear` 修复）。

**修复**：按证据建议改为格式能力表驱动：模块级
`_LINEAR_IMAGE_FORMATS = frozenset({"exr"})`，`keep_linear` 改为
`ext in _LINEAR_IMAGE_FORMATS`。表内登记与渲染对话框格式列表及
FileIO 写侧能力对齐（exr 是当前唯一端到端浮点线性格式）；后续新增
浮点 HDR 格式（hdr、toos-float 档等）只需在此一处登记，注释中写明
对照先例。故按根因（结构性单点比较）判 FIXED，而非因"当前不可触发"
判 INVALID——被点名的代码形态确实存在且证据明确。

**测试**：`test_aud2407_linear_format_capability_table`（exr 在表内；
png/jpeg/tiff/webp 与表不相交）、
`test_aud2407_keep_linear_driven_by_table`（exr→True、png/tiff/webp→False，
经直驱 `_run_composite_image_render` 断言传给
`_render_composite_full_res` 的 keep_linear 实参）。

---

## 验证记录（本机 venv，Python 3.12.13 / PyQt6 6.10.2）

1. `python3 -m py_compile src/app/controllers/composite_controller.py
   tests/app/test_fix_wd1g4_aud.py` → **通过**（py_compile OK）。
2. 新增测试 `tests/app/test_fix_wd1g4_aud.py`：**26 passed**。
3. 邻接既有测试（grep tests/ 下 composite_controller 相关全部文件）：
   `test_composite_leave_flush.py`、`test_composite_graph_open_context.py`、
   `test_composite_entry_source_selection.py`、
   `test_no_pending_comp_clip_context_in_page_switch.py`、
   `test_project_save_routing.py`、`tests/edit/test_remember_active_comp_clip.py`、
   `tests/composite/test_no_sync_readback_on_page_switch.py`、
   `test_fix_wd1g2_aud.py`、`test_fix_wd1g5_aud.py`、
   `test_deliver_controller_coverage.py` → 与新增测试合计
   **297 passed, 0 failed**。
4. 邻接范围外的既有失败（**与本批无关，已按协议核实**）：
   `tests/app/test_page_controller_coverage.py::test_update_color_monitor_probes_and_backfills_metadata`
   失败（primaries/transfer/range 回填为 None）。该测试与
   `src/app/controllers/page_controller.py` 均带有**其他并行批次的
   未提交改动**（AUD-3102 色彩 tag 探测，工作区同时存在
   `tests/app/test_fix_wd1g8_aud.py` 等他批产物），不在本批白名单内。
   经 git stash 往返验证：干净基线上该测试通过，恢复工作区后仍失败——
   失败由他批在制改动引入，本批未触碰 page_controller。已原样保留，
   不代为修复（越权），移交对应批次处理。

## 行为兼容性说明

- `_cleanup_composite_render_exporter` 新参数带默认值，既有调用方零改动。
- `_safe_int` 在合法输入下与旧 `int(x or default)` 逐点等值（含 0/None
  回退），仅坏值/负值路径行为变化（回落默认/钳制，均为修复目标）。
- 视频渲染 UI 装配顺序、取消过滤器行为、`cancel_composite_render` 语义
  不变；并发第二次启动由"静默覆盖"变为"拒绝并提示"（修复目标行为）。
- i18n：新增 2 个 key（already_rendering / serialize_failed），均带中文
  默认文案，走既有 `I18N.tr` 机制。
