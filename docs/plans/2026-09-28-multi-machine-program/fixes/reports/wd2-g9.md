# 修复报告 wd2-g9 — 批 D（Major×15：AUD-3500~3504 / GAP-027 / AUD-3600~3601 / AUD-3701 / AUD-40013 / AUD-3804~3805 / AUD-3801~3803）

- 范围：`src/app/main_window.py`（AUD-3500~3503、GAP-027）、
  `src/app/ui/library_panel.py`（AUD-3504、AUD-3801~3803）、
  `src/app/menu/menu_builder.py`（AUD-3600~3601）、
  `src/app/module_loader.py`（AUD-3701）、
  `src/app/ui/script_panel.py`（AUD-40013，按条目行内位置）、
  `src/app/ui/files_panel.py`（AUD-3804）、
  `src/app/ui/info_panel.py`（AUD-3805）
- 证据：docs/audit/2026-09-28/batches/batch-035-app-main-window.md、
  batch-036-app-menu.md、batch-037-app.md、batch-038-app-ui.md、
  batch-040-app-ui.md、docs/testcoverage/2026-09-28/GAP_REPORT.md（GAP-027）
- 测试：新建 `tests/app/test_fix_wd2g9_aud.py`（46 用例，全绿）。
  未翻转任何既有用例；邻接既有套件原样通过（详见「验证记录」）。

## 结论总表

| 编号 | 结论 | 摘要 |
|------|------|------|
| AUD-3500 | **FIXED** | closeEvent 把可取消的 `_confirm_save_before_close()` 提前到一切后台服务停机之前：取消关闭时 Tangent/tracking/files_panel/回放管线零调用、`_pipeline_shutdown_done` 保持 False（可再次关闭）；确认通过后 FileOpsMixin 内的二次确认以实例级 bypass 放行（"丢弃"后脏标记仍在，否则会被问两次），finally 恢复类级方法解析 |
| AUD-3501 | **FIXED** | closeEvent 新增 UpdateChecker 停机块：`quit()` + `wait(10500)`（上限略高于 run() 内 urlopen 的 timeout=10s，保证线程必收；常规路径立即返回），超时仅 warning 不再留运行中的 QThread 到析构 |
| AUD-3502 | **FIXED** | closeEvent 在 FileOpsMixin 停 engine worker **之前**调用 `script_engine.stop()`（请求中止）并 `script_thread.wait(3000)`（读取引擎私有 `_thread` 句柄，getattr 判空），消除清理顺序倒置与 QThread 析构崩溃 |
| AUD-3503 | **FIXED** | closeEvent 对 `denoise_panel`/`deblur_panel` 的引擎调用现有 `cancel()`（中断请求 + 2s 有界等待 + terminate 兜底），worker 不再随窗口析构 |
| AUD-3504 | **FIXED** | `LibraryPanel.set_root_path` 加同路径守卫（`_last_synced_path`，abspath+normpath 归一比较）+ emit 期间状态锁 `_dir_syncing`（.clinerules 信号防递归铁律）：前端对程序化 loadPath 回发 directoryChanged 时，重入的同路径同步被吸收，双向互连不再可能无限互相触发目录重载；不同路径的正常导航/广播行为不变 |
| GAP-027 | **PARTIAL**（按计划策略推进） | 本批 46 个用例中 11 个直接覆盖 main_window.py 低覆盖区：closeEvent 全部新路径（取消/确认先行/mixin bypass 恢复/真实路径一次）、UpdateChecker·script_engine·denoise 停机块、`_shutdown_playback_pipeline` 幂等与五个停机分支、`_config_bool`。942 缺失行的补齐按 GAP_REPORT 分批策略继续，本批不宣告关闭 |
| AUD-3600 | **FIXED** | 未注册顶层菜单不再静默丢弃：`_resolve_menu` 持有 menu_bar 时自动补建顶层菜单（标题经 i18n，启用此前从未使用的 `_menu_bar` 成员），View/Timeline 的 5 个 action 真实出现；无 menu_bar（测试替身）时跳过日志从 DEBUG 升为 warning，配置错误生产可见 |
| AUD-3601 | **FIXED** | 中间子菜单标题经 i18n：`_translated_segment` 查找链 `menu.<seg>` → `panel.<seg>`（两者全部 10 个 locale 均有条目，如 panel.timeline=时间线/タイムライン/타임라인…）→ 英文原文回退；键名小写归一（menu.view/panel.timeline）。未改 locale 文件（不在白名单），中文界面 "Timeline" 由既有 panel.timeline 词条命中 |
| AUD-3701 | **FIXED** | `post_register_hook` 移出主 try 单独隔离：钩子（composite 的 discover_and_register_plugins）抛异常时模块保持「已注册」返回 `(True, "")`，仅降级为插件发现 warning——register_builtin_modules 不再把已注册模块记入 results=False/failures，main.py [R2-8] 提示与实际状态一致。附带修正 `register_module` 返回 False（不可用/重复）时如实上报失败（原无条件报成功，与 AUD-3701 同族的状态/报告矛盾） |
| AUD-40013 | **FIXED** | `_discover_builtin_examples` 目录从 `src/app/scripting/examples`（不存在）改定位 `src/features/scripting/examples`（本文件上两级跨包）；Scripts 菜单 Examples 恢复非空，backward-compat 入口 `_load_example` 同源恢复 |
| AUD-3804 | **FIXED** | `_show_error_page` 置 `_is_error_page` 标志（初始化移到 __init__ 首次错误页之前），`_on_page_loaded` 对错误页的 loadFinished(True) 一律忽略（不再置 `_page_loaded=True`/清零计数/短路健康探测）；`_load_url` 启动失败分支用独立计数 `_start_failures`（上限 5 次重试、不被错误页重置），超限停表保留错误页；服务恢复加载真实 URL 时标志复位 |
| AUD-3805 | **FIXED** | `update_metadata` 循环改 `label.setText(normalized.get(key, "-"))`：缺失字段重置 "-"，空 metadata 清空整面板（删除原 `if not metadata: return` 早退）；snake_case 回退键行为保持 |
| AUD-3801 | **FIXED** | paint 热路径三处去 IO：①评分走共享 `_RatingCache`（单连接整表读入 `{NFC path: rating}`，delegate/代理排序同源，写入点增量维护，directoryLoaded 刷新）——不再逐项 connect/execute/close；②`is_dir` 走 `QFileSystemModel.isDir`（节点树 O(1)，无 stat；无模型能力时回退）；③`.topos` 存在性按路径缓存（`_has_project`），directoryLoaded/rowsInserted/rowsRemoved 失效。放大器同步修：`on_thumbnail_ready` 重绘节流合并为每帧（16ms）至多一次，N 个缩略图到达不再触发 N 次整 viewport 重绘 |
| AUD-3802 | **FIXED** | `LibraryProxyModel.lessThan` 的 rating 分支查内存缓存；`set_sort_criteria("rating")` 时一次性 refresh（单连接批量）。回归锁：排序全程 `db.get_file_rating` 零调用（旧实现 O(N log N)×2 次 SQLite 连接） |
| AUD-3803 | **FIXED** | 重命名：文件同步迁移 `.topos` 边车（目标不存在时）+ `file_meta` 行（NFC 键，`UPDATE OR IGNORE`）；目录改写前缀（`substr` 前缀比较，避免 LIKE 通配符）。删除：询问是否连同删除 `.topos` 边车 + 清理 DB 行（目录按前缀）。DB 迁移经 `self.__dict__` 判存，兼容既有测试的 `__new__` 裸面板替身（未初始化 PyQt 实例的属性访问会抛 RuntimeError） |

无 INVALID / BLOCKED 条目。

## 1. AUD-3500 [Major/bug/M] — FIXED

证据复现（batch-035）：closeEvent 先后 shutdown Tangent Hub、tracking、
files_panel（含 Flask 服务）、`_shutdown_playback_pipeline()`（并置
`_pipeline_shutdown_done=True` 使其不可恢复），最后才调用
`FileOpsMixin.closeEvent`——其首行 `_confirm_save_before_close()` 弹保存
确认框；用户选「取消」后窗口不关闭，但跟踪/播放/预览渲染已全部失效。

修复（main_window.py closeEvent）：

1. 最前面执行 `confirm = getattr(self, "_confirm_save_before_close", None)`
   （getattr 判空兼容 test_mode 无工程路径）；返回 False → `event.ignore()`
   后直接 return——此时任何服务尚未停机，`_pipeline_shutdown_done` 仍为
   False，用户再次关闭仍走完整流程。
2. 确认流程自身抛异常不吞关闭（mixin 内还会再问一次），仅
   `logger.exception`。
3. 确认通过后置实例级 `self._confirm_save_before_close = lambda: True`
   （mixin 的二次确认直接放行；「丢弃」不清脏标记，否则用户被问两次），
   `finally` 中 `self.__dict__.pop("_confirm_save_before_close", None)`
   恢复类级解析（closeEvent 可能因后续 ignore 再次进入）。
4. `_user_discarded_changes`（AUD-2800 语义）由首轮确认设置，bypass 不
   触碰，close 尾部的 `_persist_active_project_before_close` 行为不变。

测试：`test_close_cancel_aborts_before_any_shutdown`（取消→零停机+事件
忽略+可复用）、`test_close_confirm_runs_before_all_shutdowns`（确认严格
先于 tangent/tracking/files/pipeline）、`test_close_confirmed_shuts_services_
and_accepts`、`test_close_confirmed_bypasses_second_prompt_in_mixin`（桩替换
FileOpsMixin.closeEvent 复现其首行确认，断言首轮仅一次 + 二次确认 True）、
`test_close_accept_path_runs_real_mixin_cleanup`（真实 mixin 路径一次，
test_mode 分支不抛异常且 bypass 恢复）。

## 2. AUD-3501 [Major/bug/M] — FIXED

证据复现：`UpdateChecker(QThread)` 在启动路径创建并 start（main_window.py
744-750），closeEvent 无对应清理；closeEvent docstring 自述的
「QThread 运行中被析构 → SIGSEGV/SIGABRT」同样适用于它。

修复：closeEvent 新增独立 try/except 块（与 tangent/tracking 并列）：
`checker.quit()`；`checker.wait(10500)` 返回 False 时 warning（不再等待）。
10500ms > run() 内 urlopen 的 timeout=10s，保证最坏情况下线程也必然
收敛，不存在「等不满导致析构崩溃」的窗口；常规路径（请求早已完成/
测试短路 no_update）wait 立即返回，不拖慢关闭。

测试：`test_close_stops_update_checker_thread`（quit 一次、wait(10500) 一次）。

## 3. AUD-3502 [Major/bug/M] — FIXED

证据复现：`script_engine = create_script_engine(...)`（1283-1288），引擎
在后台 QThread 执行用户脚本（engine.py:248）；存在 `stop()`（1704-1705
连接 stop_clicked）但关闭路径从不调用——脚本线程持有 `self.engine`
引用，FileOpsMixin 先停 engine worker 属清理顺序倒置。

修复：closeEvent 在 FileOpsMixin.closeEvent 之前调用
`script_engine.stop()`（桥接 request_abort），再经
`getattr(script_engine, "_thread", None)` 有界 `wait(3000)`（引擎侧无
公开等待 API，读取私有句柄并判空；无句柄=从未执行脚本，安全跳过），
异常隔离为 `logger.exception`。

测试：`test_close_stops_script_engine_before_engine_workers`（stop 一次 +
wait(3000) 一次）、`test_close_script_engine_without_thread_handle_is_safe`。

## 4. AUD-3503 [Major/risk/M] — FIXED

证据复现：`denoise_panel.engine.processing_finished.connect(...)` 等
（1719-1724）表明 DenoiseWorker（QThread）为异步执行；closeEvent 未处理，
AI ONNX 推理中关闭 → worker 存活时析构崩溃 + 迟到信号落已销毁 UI。

修复：closeEvent 遍历 `("denoise_panel", "deblur_panel")`，对
`panel.engine` 调用既有 `DenoiseEngine.cancel()`（Phase 5 协作取消：
requestInterruption → wait(2000) → terminate → wait(1000)），逐面板
try/except 隔离。

测试：`test_close_cancels_denoise_and_deblur_workers`（两引擎 cancel 各一次）。

## 5. AUD-3504 [Major/risk/M] — FIXED

证据复现：main_window.py 1509-1512 双向互连
`files_panel.directory_changed ↔ library_panel.set_root_path`；
Library 侧 `set_root_path` 无同路径守卫，且每次都 emit——前端对程序化
loadPath 回发 directoryChanged 时构成异步互触环（CPU 高占 + 反复重载）。

修复（library_panel.set_root_path）：

- `_last_synced_path`（str|None，__init__ 置 None）记录最近一次对外广播
  的目录；重入路径经 `abspath+normpath` 归一后相等 → 直接 return（同路径
  下视图/路径框/配置均无变化，断环无副作用）。首次调用（init_ui 的
  start_path）`_last_synced_path is None` → 正常执行，初始同步不受影响。
- `_dir_syncing` 状态锁：emit 期间置 True（direct 连接同步重入直接
  return），finally 复位。

测试：`test_directory_sync_loop_terminated`（FakeFilesPanel 模拟最坏
回发，bounces == [subdir] 恰一次）、`test_set_root_path_same_path_does_
not_reemit`（含尾分隔符等价路径）、`test_set_root_path_different_path_
still_emits`（正常导航回归锁）、`test_set_root_path_missing_path_ignored`。

## 6. GAP-027 [Major/test-gap/L] — PARTIAL（按证据推进）

GAP_REPORT：main_window.py 覆盖 19%、缺失 942 行（Top 30 第 27 位），
补测策略为按模块分批补测（每批=1 模块，用例跑到全绿）。本批伴随 AUD
修复新增 46 用例，其中直接覆盖 main_window.py：

- closeEvent 全部新路径（见 AUD-3500~3503 各测试）+ 真实 mixin 路径；
- `_shutdown_playback_pipeline`：幂等（重复调用零副作用）与五个停机
  分支（走带 pause/音频/视频控制器/时间线预览渲染器+置空/合成页
  cleanup）——`test_shutdown_playback_pipeline_idempotent`；
- `_config_bool` 的 None/False 语义——`test_config_bool_distinguishes_
  none_and_false`。

942 缺失行的整体补齐（非关闭路径的大量 UI 编排代码）按 GAP_REPORT
策略另行分批推进，本批不宣告关闭。

## 7. AUD-3600 [Major/bug/M] — FIXED

证据复现：menu_builder.build 对未注册 menu_path 仅 `logger.debug` +
continue；edit 模块注册 5 个 `menu_path="View/Timeline"` 的 action，调用
方只注册 File/Edit/Color/Composite/Deliver——整组被静默丢弃。

修复：

1. `_resolve_menu` 顶层菜单未注册且 `self._menu_bar` 非空时自动补建：
   `self._menu_bar.addMenu(I18N.tr(f"menu.{top_name.lower()}", top_name))`
   并入 `_menus` 缓存（此前构造参数 menu_bar 被存储但从未使用）——
   View/Timeline 的 5 个 action 恢复出现（顶层菜单追加在菜单栏末尾）。
2. 连 menu_bar 都没有（测试替身/异常装配）时维持返回 None，但 build
   的跳过日志从 DEBUG 升为 WARNING，生产日志可见。
3. 顺带消除 `_resolve_menu` 中不可达的 `if not parts` 死分支，改为对
   空 menu_path 的显式校验。

偏差说明：审计建议的「调用方补注册 View」涉及
menu_toolbar_controller.py / edit module（均不在白名单），以 builder 侧
自动补建达成同一用户可见结果。

测试：`test_unregistered_top_menu_auto_created`（menu_bar 自动补建 +
子菜单/真实挂载断言）、`test_unregistered_top_menu_without_menu_bar_warns`
（caplog 断言 warning 且 actions 为空）。

## 8. AUD-3601 [Major/bug/M] — FIXED

证据复现：中间子菜单 `current.addMenu(sub)` 直接使用英文段名
（"Edit/Timeline" → "Timeline"），未经 i18n，中文界面显示英文。

修复：`_translated_segment(segment)` 静态方法查找链
`menu.<seg>` → `panel.<seg>` → 原文段：

- locale 键一律小写，段名 `.lower()` 归一；
- `panel.timeline` 在全部 10 个 locale 文件中均有条目（zh_CN=时间线、
  ja_JP=タイムライン、ko_KR=타임라인、ru_RU=Таймлайн…），未命中
  `menu.timeline`（词条不存在）时由二级键命中，中文界面不再显示英文
  "Timeline"；
- 两级均未命中回退英文原文段（与旧行为一致，等待词条补齐；未改
  locale JSON——不在白名单，且规范要求 10 个 locale 同步加键）。

测试：`test_submenu_title_translated_via_fallback_chain`（桩化
I18N.tr_or_none 验证三级链）、`test_submenu_title_uses_translated_text`
（真实 addMenu 标题为译文）。

## 9. AUD-3701 [Major/bug/M] — FIXED

证据复现：`registry.register_module(module)` 成功后（模块已在
`_modules`、pages 已注册）执行 post_register_hook；钩子抛异常落入宽
except → `return (False, str(e))`——状态（已注册可用）与报告（加载失败）
矛盾，main.py [R2-8] 据此向用户误报该特性加载失败。

修复（module_loader._register_spec）：

1. 主 try 收敛为「导入 + 构造 + register_module + 日志」；钩子调用移出
   主 try，单独 try/except 隔离，失败仅
   `logger.warning("%s registered, but post-register hook %r failed: %s")`
   并保持 `return True, ""`。
2. `register_module` 返回 False（模块不可用/重复注册，此时模块不在
   registry 中）时如实 `return (False, "registration rejected ...")`
   ——原实现忽略返回值无条件报成功，与本条同属状态/报告矛盾（AUD-3704
   的核心点，随本修复一并落地于 `_register_spec` 路径）。
3. ImportError / 其他异常的既有隔离语义不变。

测试：`test_post_register_hook_failure_keeps_registered_true`（钩子抛
RuntimeError → (True, "") 且 registry 中确有模块）、`..._hook_success_
returns_true`、`test_register_module_rejected_reports_false`、
`test_import_error_reports_false`。

## 10. AUD-40013 [Major/bug/M] — FIXED

证据复现：`_discover_builtin_examples` 以
`os.path.join(dirname(__file__), "..", "scripting", "examples")` 拼出
`src/app/scripting/examples`——不存在（offscreen 实测返回 `{}`）；真实
示例在 `src/features/scripting/examples/`。

修复：目录改 `../../features/scripting/examples`（本文件 src/app/ui/
向上两级跨包），os.walk/排序/读取逻辑不变；Scripts 菜单 Examples 与
backward-compat `_load_example`（1538）同源恢复。

测试：`test_discover_builtin_examples_finds_real_examples`（非空、含
basic_grade.py、内容含 topos API）、`..._dir_is_features_scripting`
（目录存在 + 旧缺陷路径确认不存在）。与 AUD-40014 既有测试
（test_script_panel_project_root_aud40014.py）邻接通过。

## 11. AUD-3804 [Major/bug/M] — FIXED

证据复现：`_load_url` 启动失败分支每 4s 无条件重试；每次 setHtml 错误页
触发 `loadFinished(True)` → `_page_loaded=True`、`_load_retries=0`——
[CRASH-6] 有上限退避被完全绕过，且 `_page_loaded=True` 使 set_root_path
向错误页发 JS、健康探测被短路。

修复（files_panel.py）：

1. `_show_error_page` 置 `self._is_error_page = True`；三个状态字段
   （`_is_error_page`/`_start_failures` 与 [CRASH-6] 字段一起）的初始化
   移到 __init__ 首次 `_show_error_page()` **之前**（否则早于字段定义的
   首次调用会被后续复位覆盖）。
2. `_on_page_loaded` 首行 `if self._is_error_page: return`——错误页的
   成功回调不置 ready、不清计数、不消费 `_pending_path`。
3. `_load_url` 启动失败用独立计数 `_start_failures`：>5 次后 error 日志
   + 保留错误页不再调度；该计数不被错误页 loadFinished 重置。服务成功
   路径 `_start_failures=0` 复位并清除错误页标志后 setUrl。

测试：`test_load_url_startup_failure_retry_is_bounded`（QTimer.singleShot
桩化，10 次触发仅 5 次重试调度）、`test_error_page_load_finished_does_
not_reset_state`、`test_real_page_load_finished_still_works`（正常路径
回归锁）、`test_server_recovery_clears_error_page_flag`。

## 12. AUD-3805 [Major/bug/M] — FIXED

证据复现：`update_metadata` 仅更新新元数据中出现的键且空 dict 早退——
图 A（有 Lens/ISO）切图 B（无字段）后残留 A 的值。

修复（info_panel.update_metadata）：`normalized` 构造改为空 dict 起步
（metadata 为空/None 时直接全 "-"），渲染循环
`label.setText(normalized.get(key, "-"))`——缺失字段一律重置占位符。
`_KEY_MAP` 的 Title Case/snake_case 双兼容不变。

测试：`test_info_panel_missing_fields_reset`、`test_info_panel_empty_
metadata_clears_all`、`test_info_panel_snake_case_keys_accepted`。

## 13. AUD-3801 [Major/bug/M] — FIXED

证据复现：paint 热路径每项 `os.path.isdir` + `os.path.exists(.topos)`
两次 stat，评分 `db.get_file_rating` 每次新建 SQLite 连接
（database.py:110-116 connect/execute/close）；放大器：每个缩略图到达
触发整 viewport 重绘（34-36），N 文件 ≈ N 次全量 paint × 每项 DB 往返。

修复（library_panel.py）：

1. 新增 `_RatingCache`（GUI 线程专用）：单连接整表读入
   `{NFC path: rating}`（与 `Database._meta_key` 同键约定），懒加载 +
   `refresh()`（directoryLoaded/边车迁移/行删除后）+ `update()`（每个
   `set_file_rating` 写入点同步：delegate 双击清星/点星、panel
   `set_file_rating`/`refresh_file_rating`）。file_meta 仅评过级的文件
   有行，整表读取代价与素材规模同阶。
2. delegate 与 LibraryProxyModel 共享 panel 级同一实例
   （`set_rating_cache` 注入），绘制与排序同源。
3. `_is_dir`：穿透代理取源索引走 `QFileSystemModel.isDir`（节点树缓存，
   O(1) 无系统调用）；模型无该能力时回退 `os.path.isdir`。
4. `_has_project`：`.topos` 存在性按路径缓存（`_project_cache`），
   `directoryLoaded`/`rowsInserted`/`rowsRemoved` 失效（清 dict O(1)）。
5. `on_thumbnail_ready` 节流：16ms 合并窗口内多次到达只调度一次
   `_flush_thumbnail_repaint` → 一次 `viewport().update()`。

测试：`test_rating_cache_load_update_refresh`（含 NFD 查询键命中 NFC
存储键）、`test_delegate_paint_uses_rating_cache_not_per_item_db`（get_
file_rating 零调用 + delegate/panel 缓存同源）、`test_delegate_is_dir_
uses_model_without_stat`、`test_delegate_sidecar_cache_hit_avoids_stat`
（monkeypatch os.path.exists 计数为 0）、`test_delegate_thumbnail_
repaint_coalesced`（10 次到达 → 1 次重绘）、`test_directory_loaded_
refreshes_caches`。

## 14. AUD-3802 [Major/perf-opportunity/M] — FIXED

证据复现：rating 排序 `lessThan` 每次比较 2 次 `get_file_rating`
（各一连接）；N≈2000 → 4 万+ 次连接建立全部在 GUI 线程。

修复：`LibraryProxyModel` 持共享 `_RatingCache`；`set_sort_criteria`
切到 "rating" 时 `refresh()`（单连接批量），`lessThan` 查内存 dict。
set_sort_criteria 其余语义（name 升序/其他降序、invalidate）不变。

测试：`test_proxy_rating_sort_uses_cache_not_db_per_comparison`
（1<5 / 5<1 比较结果正确 + 全程 `db.get_file_rating` 零调用 + 缓存值
校验；评分经 DB 写入以验证 refresh 链路）。

## 15. AUD-3803 [Major/process/M] — FIXED

证据复现：rename/delete 只动本体文件；`.topos` 边车与 `file_meta`
评分行按路径关联（徽章 93-94、中键应用调色 292、评分 164/251）——
重命名后边车成孤儿、DB 行残留旧路径；删除时边车与 DB 行均不清理。

修复（library_panel.py）：

- 模块级迁移助手（均 NFC 键约定、`sqlite3.Error` 隔离为 warning）：
  `_migrate_file_meta_path`（`UPDATE OR IGNORE ... WHERE path = ?`）、
  `_migrate_file_meta_prefix`（`substr` 前缀比较改写目录前缀，不用
  LIKE 避免路径中 `%`/`_` 被当通配符）、`_delete_file_meta_rows`
  （文件按行/目录按前缀 DELETE）。
- `rename_item`：os.rename 成功后——文件：边车存在且目标边车不存在则
  同步 `os.rename`（失败仅 warning 提示，不影响已完成的主体重命名）+ 行
  迁移；目录：仅前缀改写（目录内文件与其边车一起搬移、相对关系保持）。
  AUD-3800 的重名冲突守卫保持在前。
- `delete_item`：有边车时追加询问「是否连同删除工程文件」；DB 行按
  文件/前缀清理；两次确认均为 Yes 时边车随删，本体失败仍走 critical
  提示且不清理 DB（return）。
- 兼容性：DB 句柄经 `self.__dict__.get("db")` 判存
  （`_file_meta_db_path`/`_refresh_rating_cache_safe`）——既有回归测试
  用 `LibraryPanel.__new__` 构造裸面板，未初始化 PyQt 实例的属性访问
  抛 `RuntimeError: super-class __init__() ... was never called`，
  `getattr` 默认值无法吸收（非 AttributeError，实测确认）。

测试：`test_rename_migrates_sidecar_and_db_row`、`test_rename_conflict_
keeps_target`（AUD-3800 守卫回归）、`test_rename_directory_migrates_db_
prefix`、`test_delete_file_removes_sidecar_when_confirmed_and_cleans_db`、
`test_delete_file_keeps_sidecar_when_declined`（用户拒删边车时保留但 DB
行仍清理）、`test_delete_directory_cleans_db_prefix`、
`test_file_meta_migration_helpers_nfc_keys`（NFD 旧键迁移命中 NFC 存储）。

## 验证记录

py_compile（.venv/bin/python -m py_compile，全部退出码 0，无输出）：

```
src/app/main_window.py src/app/menu/menu_builder.py src/app/module_loader.py \
src/app/ui/files_panel.py src/app/ui/info_panel.py src/app/ui/library_panel.py \
src/app/ui/script_panel.py
```

新建测试（46 用例全绿）：

```
$ .venv/bin/python -m pytest tests/app/test_fix_wd2g9_aud.py -q -p no:cacheprovider --no-header
46 passed in 3.10s
```

邻接既有套件（grep 改动文件名选出的相关测试）：

```
$ .venv/bin/python -m pytest tests/app/test_fix_layer_ai_library_aud.py \
    tests/app/test_fix_wd2g9_aud.py -q -p no:cacheprovider --no-header
57 passed in 3.22s                        # library_panel 重命名域（AUD-3800 既有锁）

$ .venv/bin/python -m pytest tests/app/test_fix_wd2g10_aud.py \
    tests/ui/test_column_grid_panels.py tests/scripting/test_script_api_versioning.py \
    -q -p no:cacheprovider --no-header
63 passed in 5.95s                        # script_panel（AUD-40013/40015 邻接）等

$ .venv/bin/python -m pytest tests/app/test_page_controller_coverage.py \
    tests/app/test_project_save_routing.py tests/app/test_composite_entry_source_selection.py \
    tests/app/test_edit_to_color_playhead_sync.py \
    tests/app/test_color_entry_playback_and_timeline_refresh.py -q ...
184 passed in 2.60s                       # main_window 页面/保存路由邻接

$ .venv/bin/python -m pytest tests/architecture/test_module_isolation.py \
    tests/architecture/test_ci_matrix.py tests/architecture/test_product_physical_missing.py \
    tests/architecture/test_feature_flags_registry.py \
    tests/architecture/test_file_explorer_integration.py \
    tests/app/test_script_panel_project_root_aud40014.py -q ...
1 failed, 120 passed, 1 xfailed            # 见下

$ .venv/bin/python -m pytest tests/app/test_fix_wd2g9_aud.py \
    tests/architecture/test_page_context.py tests/architecture/test_src_directory_boundaries.py \
    tests/app/test_fix_wd2g10_aud.py -q -p no:cacheprovider --no-header
109 passed in 4.78s
```

既有失败（按锁定旧行为协议处理，非本批引入）：

- `tests/architecture/test_module_isolation.py::TestColorOnlyPhysicalMissing::
  test_color_core_still_works` — ModuleNotFoundError: No module named
  'src.features.composite'。导入链为
  image_engine.py:57 → shader_compiler.py:14 → composition_uniforms.py:45 →
  effects/__init__.py:11 → effect_catalog.py:47（该测试物理移除 composite
  目录）。本批 7 个改动文件均不在该链上，且 git status 显示链上文件均无
  工作区改动（非并行批次所为亦非本批所为，属既有/提交内缺陷）；本批
  未改动其任何行为，不翻转该用例。

## 行为变化说明（供 review）

- MainWindow.closeEvent：保存确认先行（此前在最后）；新增三个停机块
  （UpdateChecker/script_engine/denoise·deblur）。取消关闭路径不再有
  任何服务停机副作用——这是审计要求的方向性修复。
- MenuBuilder：未注册顶层菜单自动补建（仅当构造时传入了 menu_bar——
  唯一生产调用方 menu_toolbar_controller 传入了 self.menuBar()）；跳过
  日志 DEBUG→WARNING。
- LibraryPanel.set_root_path：同路径重复调用不再重载/emit（防环语义）。
- LibraryPanel rename/delete：新增边车与 DB 行迁移/清理（含删除前边车
  确认对话框）。
- FilesPanel：错误页期间页面回调被忽略；启动失败重试 5 次封顶。
- InfoPanel：切换素材时缺失字段显示 "-"（此前残留旧值）。
