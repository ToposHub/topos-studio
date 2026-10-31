# EF-19 复查（GLM5.3）

- 日期：2026-10-01 ｜ 复查会话：GLM5.3（只读复核）
- 提交：`7ff275826`（`[fix-E ef-19] ... 24条 Suggestion 全修 ... 2个既有测试随异步行为适配已披露 (24)`）
- 白名单（e_flash_batches.json modules 键）：`src/app`（根）、`src/features/composite/evaluators`、`src/features/teaching_animation/ui`、`src/shared/project`、`src/app/ui`
- 执行报告：`fixes/reports/ef-19.md`（结论 24/24 FIXED）

## §6 八项逐项判定

| # | 检查项 | 判定 | 依据 |
|---|--------|------|------|
| 1 | 越界 | **通过** | `git show --stat 7ff275826`：22 个文件 = 18 源（全部落在 5 个白名单目录）+ 3 测试（`tests/app/test_ef19_aud.py` 新建；`tests/app/test_gap027_main_window.py`、`tests/composite/test_disk_cache.py` 为既有测试适配，属 tests/ 允许范围）+ 1 报告（`fixes/reports/ef-19.md`）。报告"为适配行为变更而更新的既有测试"节披露的 2 个文件与 diff 完全一致，且 diff 内容仅为等待后台线程回填（`_wait_for_audio_snapshot` / 有界等待写手落盘），无断言弱化：`assert mw._audio_capabilities is caps`、roundtrip 数值断言均保留。未触碰 REMEDIATION_PLAN.md。 |
| 2 | 真实修复（逐条） | **通过**（24/24 CONFIRMED，见下表） | 逐 ID：grep 计划行 → 读报告结论 → HEAD 源码 file:line 独立核实 |
| 3 | GLSL/HDR 红线 | **通过** | diff 新增行无 GLSL 改动（本批不含着色器）；无 HDR 钳制、无 601/709 相关行 |
| 4 | 并发 | **通过** | 详见下方"重点核实"。音频探测/缓存统计/torch 探测/npz 写盘均为真后台 daemon 线程 + 信号回 UI 线程；GL readback 明确留在渲染线程（AUD-5923 修复方式符合"GL 状态线程绑定"红线）；`_stop_html_preview` 有界 `wait(5000)` |
| 5 | 资源 | **通过** | AUD-6010 直建 FBO/纹理就地 `release()`（GL 与 Metal 两路径）；AUD-6012 `_quad_vbo` 持引用随 `release()` 释放；AUD-36809 mkstemp 临时文件失败路径清理 |
| 6 | 异常 | **通过** | diff 新增 except 全部带具体类型且留痕（`_abort_startup`→`logger.exception`；音频探测失败→`logger.debug`；版本兜底→`logger.warning`；磁盘写失败→`logger.warning`；welcome 三处→warning/error+弹窗）。无裸 `except:`。两处保留的窄吞（`except OSError: pass` in `_recursive_search`、torch `except ImportError: pass`）均为预存代码非本批引入（已对 diff 核实），后者是代码搬迁进 `_probe_runtime_diagnostics` 静态方法 |
| 7 | 测试质量 | **通过** | `tests/app/test_ef19_aud.py` 63 用例逐 ID 正例+回退敏感断言；异步类测试断言 `probe_threads` 中无 MainThread（真异步验证，见下）；fake module 注入 torch/onnxruntime、monkeypatch 弹窗、`test_mode=True`，无 GL/模态 |
| 8 | 复查产出 | 本文件 | 结论 **PASS** |

### 重点核实（复查指令指定）

- **EXR/音频探测/torch 探测移后台线程（真异步）**：
  - 音频（AUD-3513）：`src/app/main_window.py:833-840` `threading.Thread(target=_probe, name="audio-backend-diagnostics", daemon=True).start()`；`_apply_audio_capabilities` 为 UI 线程槽（`audio_capabilities_probed.connect` 于 `__init__`，`main_window.py:335`）；窗口销毁 emit `RuntimeError` 留痕丢弃。测试 `test_probe_runs_off_main_thread_and_applies_on_ui_thread` 断言探测线程名 ≠ MainThread——同步实现会直接失败。**真异步确认**。
  - torch/onnxruntime（AUD-03912）：`src/app/ui/preferences_dialog.py:1576-1656`——保存对话框先在 UI 线程弹出，`WaitCursor` 期间 `_probe_runtime_diagnostics`/`_write_diagnostics_report` 在 daemon 线程执行，经 `diagnostics_export_finished` 信号回 `_on_diagnostics_export_finished`（UI 线程弹结果框）。**真异步确认**。
  - npz 写盘（AUD-5923）：`src/features/composite/evaluators/cache_evaluator.py:33-77`——模块级 `queue.Queue(maxsize=2)` + 单 daemon 写手线程；GL readback（`_readback_result_array`，:166-177）**保留在渲染线程**（符合"GL 状态线程绑定"，未把 GL 调用移入 worker——方向正确）；写手忙时 `put_nowait`→`queue.Full`→debug 留痕跳过。测试断言 store 调用发生在名为 `disk-cache-store-writer` 的线程。
- **evaluator FBO-VBO 就地释放**：`histogram_manager.py:531-535`（直建 FBO 用毕 `release()`）、:418-425（GL 路径无 pool 时 `ds_tex.release()`）、:478-481（Metal 路径同型）；`_get_quad_vao` 记录 `self._quad_vbo`（:560-562），`release()` 中随 VAO 一并释放（:834-841）。

## 逐 ID 核实表（24/24）

| ID | 计划指控（摘要） | HEAD 证据 | 判定 |
|----|------------------|-----------|------|
| AUD-3200 | 兼容包装层无调用方且每次重建 flags | `src/app/feature_flags.py:23-37`：`get_enabled_features` 加 `@lru_cache(maxsize=1)`、`is_feature_enabled` 加 `@lru_cache(maxsize=None)`；`DEFAULT_FEATURES` 别名保留 | CONFIRMED |
| AUD-3507 | 引擎初始化失败无降级、裸崩 | `src/app/main_window.py:390-399`（Database/ImageEngine）、:529-534（create_panels）外层 try + `_abort_startup`（:801-829：`logger.exception` + test_mode 跳过的 QMessageBox + `raise RuntimeError ... from exc`） | CONFIRMED |
| AUD-3513 | 音频诊断 UI 线程同步执行 | 见"重点核实"：daemon 线程 + `audio_capabilities_probed` 信号 + UI 槽 `_apply_audio_capabilities`（:842-844） | CONFIRMED |
| AUD-42002 | 版本兜底字面量人工同步、冻结环境漂移 | `src/app/update_checker.py:17-37`：`_DIST_NAMES=("topos","topos-color")` 逐个 `importlib.metadata.version`；字面量兜底前 `logger.warning` 留痕 | CONFIRMED |
| AUD-42004 | 注释写 GitHub 实为 Gitee | `update_checker.py:60`（"Gitee API v5 endpoint"）、:78（docstring "queries the Gitee Releases API (Gitee API v5)"）；URL 未变 | CONFIRMED |
| AUD-57001 | docstring 引用不存在路径 | `evaluators/__init__.py:23-25` 指向真实存在的 `src/features/composite/evaluators/graph_evaluator_mixin.py`（已验证文件存在） | CONFIRMED |
| AUD-57004 | docstring 引用不存在路径 | `evaluators/ai_evaluator.py:31-32` 指向真实存在的 `src/features/composite/render/ai_compute_manager.py` | CONFIRMED |
| AUD-5923 | 磁盘缓存同步压缩阻塞渲染线程 | 见"重点核实"：压缩+IO 移后台、GL readback 留渲染线程、队列限 2 帧 | CONFIRMED |
| AUD-6010 | `_downsample` 直建路径泄漏纹理/FBO | :531-535 FBO release；:418-425 GL 路径、:478-481 Metal 路径 ds_tex release | CONFIRMED |
| AUD-6012 | release 未释放 quad VBO；get_stats 无锁穿透私有结构 | :560-562 `_quad_vbo` 记录；:834-841 release；`HistogramCache.__len__`（:145-148 持锁）；get_stats `'cache_entries': len(self._cache)`（:863） | CONFIRMED |
| AUD-30802 | 行索引线性扫描 O(n²) | `mg_keyframes_panel.py:139` `_row_index_by_id` 字典、:461-465 `_rebuild` 维护、:505-508 `_row_by_id` O(1)、:1225-1232 `_hit_keyframe` 把 `_row_center` 提到点循环外、:1849-1857 marquee O(1) 索引 | CONFIRMED |
| AUD-30905 | 陈旧 pending 被重新启动 | `motion_graphic_editor_window.py:2956-2960` 直启前 `self._html_preview_pending = None`（含代次失效原因注释） | CONFIRMED |
| AUD-30906 | UI 线程无限期 wait | :93 `_HTML_PREVIEW_STOP_TIMEOUT_MS = 5000`；:3017-3029 `thread.wait(timeout)` 超时 `logger.warning` 后继续关闭，`deleteLater` 兜底 | CONFIRMED |
| AUD-31002 | `_updating` 死锁标志 | `page_navigation.py`：标志已删除，`__init__`/`set_frame` 注释说明 `clicked` 连接 + 程序化 `setChecked` 不回声的防护来源 | CONFIRMED |
| AUD-31003 | `_link_slider` 互连靠数值收敛 | `page_timing_panel.py:426-456`：成对 `blockSignals(True/False)` + try/finally 恢复，回声源头截断（与 SceneInspector 同 idiom） | CONFIRMED |
| AUD-36809 | restore 仅捕 OSError，BadZipFile 泄漏 mkstemp 临时文件 | `library.py:344-353`：`except (OSError, zipfile.BadZipFile)` + `os.remove(temporary_name)`（失败仅 debug） | CONFIRMED |
| AUD-36817 | auto_search 同步递归扫家目录阻塞 | `offline_asset_manager.py:461-545`：`deadline`（monotonic 预算）逐 entry 检查；`auto_search_for_missing` 新增 `budget_seconds`/`cancelled` 可选参数（不传行为与旧版一致），:583-590 取消留痕 | CONFIRMED |
| AUD-36922 | 恒假死条件 + 重复路径解析 | `save_router.py:1219-1240`：顶部重复 `get_current_file_path` 解析移除，仅 save 兜底分支按需补读 | CONFIRMED |
| AUD-37010 | 先发信号再无条件 save（双重保存） | `topos_project_controller.py:607-613`：`receivers(self.auto_save_triggered) > 0` 时只发信号，否则兜底 `save()`，二选一 | CONFIRMED |
| AUD-37012 | save_page 空操作死代码 + 文档不符 | :507-513 死代码删除；:495-502 docstring 如实描述全量保存契约及未来页面级回调契约 | CONFIRMED |
| AUD-03908 | 缓存统计 UI 线程同步扫盘且低估 | `preferences_dialog.py:1800-1831`：`_update_cache_size_label` 起 daemon 线程；`_compute_cache_bytes` 用 `os.walk` 递归；`_cache_scan_in_progress` 防重入；信号回填 | CONFIRMED |
| AUD-03912 | 诊断导出 UI 线程 import torch | 见"重点核实" | CONFIRMED |
| AUD-4103 | `_updating_table` 死标志 | `shortcut_dialog.py:111-116`、:668-670：标志删除 + 注释说明无 itemChanged 连接无回声 | CONFIRMED |
| AUD-4112 | 三处宽泛 except 静默吞错 | `welcome_dialog.py:214-219`（recent 过滤→warning）、:257-261（模式探测→warning）、:472-489（重启前保存失败→error+QMessageBox.warning） | CONFIRMED |

## 红线发现

- 无 Critical。新增行 0 个裸 `except:`、0 个无留痕吞错、0 个 UI 线程 moderngl/GL 调用、0 个 HDR 钳制、0 个 601/709 混用。
- 备注（不计问题）：`_probe_runtime_diagnostics` 中 torch `except ImportError: pass` 不记录"not installed"（onnxruntime 同路径会记录）——预存代码搬迁，非本批引入；可在后续批次统一。

## 回退验证建议（抽 2 条）

1. **AUD-6012**
   - 文件：`src/features/composite/evaluators/histogram_manager.py:863`
   - 当前原文（old，逐字）：
     ```python
                'cache_entries': len(self._cache),
     ```
   - 回退为（new）：
     ```python
                'cache_entries': len(self._cache._cache),
     ```
   - 应失败节点：`tests/app/test_ef19_aud.py::TestAud6012QuadVboAndStats::test_get_stats_uses_public_len_not_private_dict`
2. **AUD-3513**
   - 文件：`src/app/main_window.py:853-855`
   - 当前原文（old，逐字）：
     ```python
        threading.Thread(
            target=_probe, name="audio-backend-diagnostics", daemon=True,
        ).start()
     ```
   - 回退为（new，同步执行旧行为）：
     ```python
        _probe()
     ```
   - 应失败节点：`tests/app/test_ef19_aud.py::TestAud3513AudioDiagnosticsOffUiThread::test_probe_runs_off_main_thread_and_applies_on_ui_thread`（断言 `all(name != "MainThread" ...)` 失败）

## 最终结论

**PASS**。24/24 修复真实、边界干净、2 个既有测试适配与披露一致、红线零违规。允许 `--mark`（commit 填 `7ff275826`）。
