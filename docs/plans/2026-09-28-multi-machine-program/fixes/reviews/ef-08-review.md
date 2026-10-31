# EF-08 复查（GLM5.3）—— src/app/controllers 18 条

- 批次：EF-08 ｜ 提交：`1e8ecd2d6f5c4fc2cda9996a9b94462e33431da6`（dev）
- 白名单：`src/app/controllers/` ∪ `tests/` ∪ `fixes/reports/ef-08.md`
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show / 当前 HEAD 源码独立核实 / grep），未运行 pytest（中心已统一复跑全绿）

## §6 八项检查结论

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **通过** | `git show --stat`：12 个 src/app/controllers 源文件 + 3 个测试文件（tests/app/test_ef08_aud.py 新增、tests/app/test_page_controller_coverage.py、tests/color/test_ai_plugin_pixel_contract.py）+ 报告自身，全部 ⊆ 白名单。未触碰 REMEDIATION_PLAN.md |
| 2 | 真实修复 | **通过（18/18 CONFIRMED）** | 逐条见下表，均在当前 HEAD 独立核实 file:line 证据 |
| 3 | GLSL/HDR 红线 | **通过** | 本批无 GLSL 改动；diff 新增行无 HDR 钳制、无 601/709 矩阵触碰 |
| 4 | 并发 | **通过** | 重点三处均为真异步（见下 AUD-2909/3107/3010 详述）；新增 worker 信号对象挂主线程 QObject（auto→queued），回调主线程执行；ASR 中继（AUD-2108）同理 |
| 5 | 资源 | **通过** | AUD-2813 异常路径停渲染线程走 `_stop_gpu_renderer_deferred`（延迟销毁，不在 UI 线程直接 GL 释放）；AUD-2717 补 deleteLater；QRunnable 持引用防 GC |
| 6 | 异常 | **通过（1 条 note）** | diff 新增 7 处 `except Exception`，6 处带 logger 留痕（debug/warning，含 exc_info）；1 处（`_footer_history_signature`，ai_edit_controller.py:6141）`return None` 无日志——但 None 语义是"签名未知→强制全量重建"（保守回退到旧行为，非吞错伪装成功），docstring 已注明，记 note 不记 ISSUE |
| 7 | 测试质量 | **通过** | 53 个用例覆盖 18 条（每条正例+反例）；抽查 19003/2909/3107 断言真实可证伪（详下）。2 个既有测试改动与报告披露逐字一致 |
| 8 | 综合 | **PASS** | 见最终结论 |

## 逐 ID 核实表（全部 18 条）

| ID | 判定 | 当前 HEAD 证据 |
|----|------|---------------|
| AUD-19003 | CONFIRMED | agent_v2_chat_worker.py:91-101：分发只看 `classify_chat_result(result)` 真实类别（`if category == "cancelled"`，不再 `or self.cancel_requested`）；`finished_result` 与终结信号成对保留。修复前代码 `if category == "cancelled" or self.cancel_requested:` 已从父提交核实 |
| AUD-2014 | CONFIRMED | ai_edit_controller.py:6134-6166：`_footer_history_signature`（计数+末条 message_id+草稿）+ `_refresh_context_token_estimate`（签名相同即复用 `_chat_context_tokens` 跳过全量重建；签名 None → 强制重建，service 异常安全回退） |
| AUD-2015 | CONFIRMED | ai_edit_controller.py:2925-2933：`persist_v2_state` 写 `state_path.name + ".tmp"` 后 `os.replace(tmp_path, state_path)` 原子替换；失败路径 logger.debug + exc_info |
| AUD-2108 | CONFIRMED | ai_edit_host_bridge_impl.py:61-76 `_AiTranscriptionRelay`（主线程亲和 QObject，四信号中继 auto→queued）；:1013-1031 实际接线，worker 信号连中继、注入回调连中继信号——回调确定在主线程事件循环执行 |
| AUD-2110 | CONFIRMED | ai_edit_host_bridge_impl.py:1372-1385：bbox 测量与品牌元素物化移入 `_post_export`（编码 worker 质量门内执行，`_measure_export_quality_contracts`），测量异常按质量门失败返回 False；:1290 对话框摘要改轻量计数（"待测量 N 条"）；profile/inset 轻量预检保留（快速失败语义） |
| AUD-2111 | CONFIRMED | ai_edit_host_bridge_impl.py:53-59（TTL=30s 常量）+ :350-385 `_probe_credential_available`：本地 provider（ollama/local_http/openai_compat）直通；缓存键 (kind, credential_ref, id(store))；keyring 异常 logger.warning 降级为不可用 |
| AUD-2205 | CONFIRMED | ai_editing_mixin.py:31-52 `_comfy_probe_timeout` 上下文管理器（30s→10s，finally 恢复；桩无数值 `_timeout` 时空操作）；:1725/:1757 两个探测方法实际使用；线程契约写入 docstring |
| AUD-2214 | CONFIRMED | ai_workflow_composite_render.py:43-97 `_prune_render_cache`（2GB/64 文件上限，mtime 旧→新删除，1h 保护窗口，只碰 `composite-*`，失败仅 debug 留痕）；:369 渲染落盘后调用（worker 线程节拍内） |
| AUD-2410 | CONFIRMED | composite_controller.py:524 `parameters=dict(node.parameters)` 副本（`_on_node_editor_selected` 内），报告核实面板侧 `set_nodes` 已有 try/finally 信号锁（AUD-10702）属实——本层补契约防御 |
| AUD-2610 | CONFIRMED（部分，与披露一致） | dock_layout_controller.py:2231-2235 `getattr(self,'all_docks',None) or findChildren(...)` 惰性回退；"停靠两次写盘"半项核实 `_on_dock_floating_changed` 停靠分支现为 `pass`（AUD-2600 已修，:2127-2134），披露准确 |
| AUD-2713 | CONFIRMED | edit_controller.py:347-372 `_AdrCalibrationWorker(QThread)`（失败 emit None + warning）；:3176-3190 实际接线：worker.completion→`_on_adr_calibration_ready` queued 回主线程，测试核实防重入（test_aud2713_reentrant_click_ignored） |
| AUD-2717 | CONFIRMED | edit_controller.py:3571-3580：`existing.close()` 后补 `existing.deleteLater()`；原裸 `except Exception: pass` 收窄为 `except RuntimeError`（C++ 已销毁场景）+ debug 留痕 |
| AUD-2806 | CONFIRMED | file_controller.py:1034-1056：edit 工程上下文下 `edit_project_dirty/color_session_dirty/composite_dirty/deliver_dirty` 任一置位或工程未命名才落盘，否则 debug 短路；Library 模式保持旧行为。报告披露"计时器当前未接线"的附注诚实（本修复使重新接线时天然安全） |
| AUD-2813 | CONFIRMED | file_controller.py:2239（局部 `gpu_renderer` 引用）+ :2295-2302：装配异常分支对已 start() 的渲染线程调 `_stop_gpu_renderer_deferred(gpu_renderer, 1000)`（项目统一延迟停止模式，GPU 释放延迟到渲染线程） |
| AUD-2904 | CONFIRMED | layer_controller.py：全文件 `grep "^\s*print("` 零命中（仅注释提及）；6 处 `[COMPOSITE-DEBUG]` 从 info 降为 `logger.debug`（惰性格式化，:1400-1420），启动回退路径 print 同步移除 |
| AUD-2909 | CONFIRMED | layer_controller.py:54-95 两个 QRunnable worker；:1977-2075 实装——EXR 读盘 + BGR→RGB 转换在线程池，`one_ready(object,object,object)` 信号（挂主线程 QObject → queued）逐条回主线程写 `ai_plugin_result_cache`（BGR 契约）+ `renderer.submit(UPLOAD_AI_PLUGIN_RESULT, RGB)`（P0-COL-03 保留）+ dirty；工程切换迟到结果按图层 identity 丢弃；`_ai_plugin_restore_inflight` 防重入。**真异步判定成立**：测试断言 `load_threads[0] is not main_thread`，且 BGR→RGB 上传契约逐通道断言 |
| AUD-3010 | CONFIRMED | menu_toolbar_controller.py:35-67 `_DependencyReportWorker(QRunnable)`；:1707-1731 实装——文件系统扫描在 `QThreadPool.globalInstance()`，`ready` queued 信号回 UI 弹对话框；防重入 + 状态栏提示；失败 QMessageBox.warning + 日志 |
| AUD-3107 | CONFIRMED | page_controller.py:530-541：`makeCurrent()` 调用已删（仅存解释性注释），show/_update_lod/update/invalidate 保留触发重绘。修复前父提交核实存在 `self.canvas.gl_viewport.makeCurrent()` 无 doneCurrent 配对 |

## 红线扫描发现

- 新增行无裸 `except:`（零命中）。
- 新增 7 处 `except Exception`：6 处 logger 留痕；1 处（AUD-2014 签名失败 `return None`）无日志但为保守回退语义（None→全量重建），**note 级**，不构成吞错。
- file_controller 既有 `except Exception: pass  # Default to trying GPU`（pipeline_decision 块）为预存代码，本批 diff 未触碰（已核对该文件 diff 无 except 增删）。
- diff 新增行无 UI 线程 moderngl/GL 调用（唯一 makeCurrent 字样在删除说明注释里）；无 HDR 钳制；无 601/709 触碰。

## 测试质量抽查

- `test_aud2909_exr_load_off_thread_submit_on_main`（tests/app/test_ef08_aud.py:1311）：断言加载线程非主线程 + (layer_id, instance_id) 缓存命中 + dirty + submit payload + 上传数组 R/B 通道互换（BGR 磁盘→RGB 上传契约逐通道验证）——回退到同步加载立即失败。
- `test_aud3107_viewport_restore_skips_makecurrent`（:1528）：`"makeCurrent" not in calls` + show/lod/update/invalidate 仍在——修复回退立即失败。
- `test_aud19003_late_cancel_keeps_complete_reply`（:107）：晚到 cancel_requested 下 reply 完整、cancelled==0、finished_result 仍成对——回退立即失败。
- 既有测试改动核对：test_page_controller_coverage.py 仅 `makeCurrent.assert_called_once()` → `assert_not_called()`（与缺陷行为锁定一致的方向性翻转，其余断言未动）；test_ai_plugin_pixel_contract.py 仅补事件泵送等待，BGR/RGB 原断言原样保留。**与报告披露逐字一致**。

## 回退验证建议（抽 2 条）

1. **AUD-19003** — 文件 `src/app/controllers/agent_v2_chat_worker.py`
   - 当前原文（old）：
     ```python
        category = classify_chat_result(result)
        if category == "cancelled":
            self.cancelled.emit()
     ```
   - 回退代码（new）：
     ```python
        category = classify_chat_result(result)
        if category == "cancelled" or self.cancel_requested:
            self.cancelled.emit()
     ```
   - 应失败：`tests/app/test_ef08_aud.py::test_aud19003_late_cancel_keeps_complete_reply`
2. **AUD-3107** — 文件 `src/app/controllers/page_controller.py`
   - 当前原文（old）：
     ```python
            if hasattr(self.canvas, 'gl_viewport') and self.canvas.gl_viewport and self.canvas.gl_viewport.isVisible():
                self.canvas.gl_viewport.show()

                # Trigger LOD update
     ```
   - 回退代码（new）：
     ```python
            if hasattr(self.canvas, 'gl_viewport') and self.canvas.gl_viewport and self.canvas.gl_viewport.isVisible():
                self.canvas.gl_viewport.show()
                self.canvas.gl_viewport.makeCurrent()

                # Trigger LOD update
     ```
   - 应失败：`tests/app/test_ef08_aud.py::test_aud3107_viewport_restore_skips_makecurrent`

## 最终结论

**PASS**（0 Critical / 0 ISSUE / 1 note）

- Note：AUD-2014 `_footer_history_signature` 的 `except Exception: return None` 建议后续补一行 debug 日志（非阻塞——回退语义安全且 docstring 已注明）。
- 18/18 真实修复；越界零；披露与 diff 一致；建议中心执行 `check_fixes.py --mark`（18 条全 FIXED）。
