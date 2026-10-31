# EF-17 复查报告（GLM5.3）

- 批次：EF-17（src/features/color/ui 10 条 + src/shared/gpu 10 条，共 20 条）
- 提交哈希：`79e019e12`（79e019e120f27ffc34f2d4a4d194b7fe5244ef3f，`[fix-E ef-17] … (20)`）
- 白名单：src/features/color/ui、src/shared/gpu ∪ tests/ ∪ fixes/reports/
- 执行报告：fixes/reports/ef-17.md
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show / grep / HEAD 源码逐条核实，未运行 pytest、未做任何 git 写操作）

## §6 八项检查逐项判定

1. **越界检查：通过**。`git show --stat 79e019e12` 共 19 个文件：color/ui 10 个、shared/gpu 7 个（含 metal/ 4 个）、tests/shared/test_ef17_aud.py（新建）、fixes/reports/ef-17.md。全部 ⊆ 白名单 ∪ tests ∪ fixes/reports。未触碰 REMEDIATION_PLAN.md；无 i18n locale 文件。
2. **真实修复：CONFIRMED（20/20）**。重点项（GL query 池与异常释放、metal 计时锁、histogram 单趟数值等价、lease 违规上报）逐条独立核实，见下表。
3. **红线扫描：1 项轻微偏离（ISSUE-1，见"红线发现"）**；无裸 `except:`；无新增 moderngl/GL 上下文创建（gl_pass_timer 类 docstring 明示"只能在持有当前 GL context 的线程（渲染线程）使用"，本批未引入任何 UI 线程 GL 调用路径——canvas/gl_viewport 的改动均为信号/死代码层面，paintGL 仅保留 ctx.clear）；无 HDR 钳制、无 601/709 内容。
4. **测试质量：通过**。tests/shared/test_ef17_aud.py 37 用例逐 ID 正例+反例，断言均为修复语义断言：如 AUD-1418 `body.count("for (") == 1`（回退为双循环即失败）+ numpy 对拍 `cdf_reference` 逐值一致（数值等价验证）；AUD-25213 `emitted == []` + 状态锁再入守卫；AUD-1209 caplog 断言 warning 留痕。
5. **并发（重点核查）**：
   - AUD-1403：`_last_gpu_time_ms` 两写（context.py:505-506 置 None、:518-519 写测量值）两读（:606-607 property、:645-646 get_stats）全部 `with self._lock` 内；重活 `cb.waitUntilCompleted()` 在锁外，锁内仅 `_measure_gpu_time_ms`（:529-543 读两个时间戳属性 + float，轻量）——**锁内无重活成立**。
   - AUD-25213/25403/25316：信号防递归改为实例状态锁 + 子控件逐一 blockSignals（finally 复位），非面板自身 blockSignals。
   - AUD-1523：有界化剪枝在持锁回调内触发（submission_scheduler.py:113/:115/:139）。
6. **资源（重点核查）**：
   - AUD-1218：探测路径 try/finally release（gl_pass_timer.py:71-79）；测量路径 `__enter__` 抛出时对未入 ring 的 query release（:213-232，`_query_in_ring` 判重防双释放）。
   - AUD-1219：`_acquire_query`/`_recycle_query` 池化（POOL_LIMIT=RING_DEPTH*8），collect 读回后回池（:139 finally `_recycle_query`），`release()` 连带清池（:237-247）。
   - GPU 测试全部 fake/stub 驱动，未创建真实 GL/Metal 上下文（报告披露 + 测试源码核实）。
7. **异常**：AUD-1209 按要求改为具体异常类型（ImportError / PyOpenGL `Error`（覆盖 GLError/NullFunctionError）/ `ctypes.ArgumentError`）+ `logger.warning`（含 fence id 与泄漏提示），模块保持零硬依赖（绑定延迟解析）。
8. **回归有效性**：报告抽 AUD-1209、AUD-25403 回退验证可信；本复查另给出 2 条回退建议（见下）。

## 逐 ID 判定表（20/20）

| ID | 计划指控摘要 | 报告结论 | 复查判定 | HEAD 证据（file:line） |
|----|-------------|---------|---------|----------------------|
| AUD-25100 | 包 docstring 未列 ColorMappingPanel | FIXED | CONFIRMED | src/features/color/ui/__init__.py:6（面板清单含 ColorMappingPanel，与 `__all__` 对齐） |
| AUD-25213 | set_brush_params 无条件回发信号回声、无状态锁 | FIXED | CONFIRMED | canvas.py:6066-6088：外部驱动路径不再 emit（函数体无 emit）；`_brush_params_syncing` 状态锁（:6073 早退 / :6087 finally 复位）；内部路径仍显式 emit（:4519/:4525/:4531，快捷键/内部 UI 处理器） |
| AUD-25301 | 500ms 轮询无条件常驻 | FIXED | CONFIRMED | color_status_bar.py:141-167：`_update_poll_timer_active` 唯一裁决（provider 且可见才 start），showEvent/hideEvent 恢复/暂停 |
| AUD-25316 | setCurrentText 静默忽略非法值；blockSignals 只锁面板自身 | FIXED | CONFIRMED | deblur_panel.py:194-255：`_set_combo_text_validated`（findText 未命中 → 回退默认 + logger.warning 带候选清单）；子控件逐一 blockSignals（finally 复位） |
| AUD-25403 | 防递归锁挂在面板自身挡不住子控件信号 | FIXED | CONFIRMED | denoise_panel.py:506-558：`_restoring_params` 实例状态锁 + `on_mode_changed` 恢复窗口早退（:449-454）+ 子 combo blockSignals 双保险 + 恢复完成显式调一次 on_mode_changed（:558） |
| AUD-25413 | paintGL 父级遍历死代码 | FIXED | CONFIRMED | gl_viewport.py:121-131：遍历已删（注释说明 AUD-25413），保留 ctx.clear |
| AUD-25418 | `_on_hsl_curve_changed` 空转发死槽 | FIXED | CONFIRMED | hsl_curves_panel.py:74-78：空槽及连接已删，仅保留 hsl_picker_toggled 连接；注释说明实际链路在 main_window 直连 |
| AUD-25510 | 右键菜单每次 UI 线程同步重扫 | FIXED | CONFIRMED | plugin_panel.py:149-216：注册表空时惰性 scan 一次；显式 "Reload Effects" 动作走 reload_plugins()；`_reload_plugins` 失败 logger.warning + exc_info 留痕 |
| AUD-25517 | 扫描路径逐条 QPixmap 解码冗余；copy2 无保护 | FIXED | CONFIRMED | presets_panel.py:412-419（扫描路径 eager 解码+setIcon 已删，注释指向 delegate LRU 按需解码）、:687-705（copy2 包 try/except OSError → warning 弹窗 + 日志留痕）。按审计"或至少惰性化"降级路线处理，报告已如实披露 |
| AUD-25809 | 三重排序 O(n) 重复开销 | FIXED | CONFIRMED | tracker_panel.py:922-925：`keyframes = sorted(all_transforms)` 一次排序，series 循环与 set_markers 复用 |
| AUD-1209 | delete_fence 吞 glDeleteSync 失败无诊断 | FIXED | CONFIRMED | composite_gpu_frame_handle.py:252-283：ImportError / `(_OpenGLError, ctypes.ArgumentError)` 分别捕获，`logger.warning` 含 fence id + 泄漏提示；PyOpenGL 异常类延迟解析保持零硬依赖 |
| AUD-1217 | docstring 引用不存在的 last_composite_ms | FIXED | CONFIRMED | gl_pass_timer.py:25 `timer.last_ms("composite")`（真实 API） |
| AUD-1218 | query `__enter__` 抛出时对象不释放（两条路径） | FIXED | CONFIRMED | gl_pass_timer.py:71-79（探测路径 try/finally release）、:213-232（测量路径失败释放未入 ring query，`_query_in_ring` :235-241 判重） |
| AUD-1219 | 每 pass 每帧新建 query 无池化 | FIXED | CONFIRMED | gl_pass_timer.py:130-137（collect finally 回池）、:145-160（`_acquire_query`/`_recycle_query`，POOL_LIMIT=RING_DEPTH*8，满池或释放失败兜底销毁/留 debug 痕）、:237-247（release() 连带清池） |
| AUD-1403 | _last_gpu_time_ms submit 锁外读写竞态 | FIXED | CONFIRMED | metal/context.py:505-506、:518-519（写）、:606-607、:645-646（读）全部 `with self._lock`；waitUntilCompleted 在锁外、锁内仅轻量时间戳读取（:529-543）——锁内无重活 |
| AUD-1416 | smeta 布局 docstring 12×u32 与 13×u32 并存 | FIXED | CONFIRMED | metal/kernels_dec.py:22-25：统一 13×u32 并注明与 MSL 侧 `smeta + sid * 13u` 步长对齐 |
| AUD-1418 | histogram_cdf 每线程两次全通道 256 桶扫描 | FIXED | CONFIRMED | metal/kernels_histogram.py:71-87：单次循环同时累加 channel_total 与 `i <= bin` 的 cum；语义 = cdf[bin]=Σ(0..bin)/Σall，与原两次扫描数学等价（测试对拍 cdf_reference 逐值 1e-6 一致）；Metal 侧字面量全部带 u 后缀、循环边界常量 256u |
| AUD-1523 | _completed/_errors 无界增长 | FIXED | CONFIRMED | submission_scheduler.py:64-70（上限 max(64, max*16)）、:170-199（`_bound_completed_locked` 丢最旧+一次性告警、`_prune_errors_locked`）；completion 回调与 on_complete 失败路径触发（:113/:115/:139）；poll() 消费语义不变 |
| AUD-1606 | _reuse_violations 无递增路径，验收空转 | FIXED | CONFIRMED | present_frame.py:438-447（register_slot 顶替非 FREE lease 时 +1，伴随 state warning）、:594-608（公开 `report_reuse_violation(detail)` 上报路径 + warning）、:610-621（assert_no_reuse_violations 锁内读数、非零即 AssertionError） |
| AUD-1610 | acquire 失败热路径急切求值 | FIXED | CONFIRMED | present_frame.py:498-505：`logger.isEnabledFor(DEBUG)` 守卫 + `%` 惰性格式化，debug 关闭时不再构造 f-string/`_state_summary()` |

## 红线发现

**ISSUE-1（Suggestion 级，不否决）**：gl_pass_timer.py:244-246 `release()` 的池清理循环新增：

```python
for query in self._pool:
    try:
        query.release()
    except Exception:
        pass
```

`except Exception: pass` 无留痕，字面违反"吞错需具体类型 + 留痕"批不变式。缓解情节：(a) 位于 context 销毁前 teardown 路径，释放失败无可恢复动作；(b) 与同函数上方**预存**的 `_open` ring 循环（:229-234）逐字同构——系镜像既有模式而非引入新风格；(c) 同批在可恢复路径（`_recycle_query` :157-160、`__enter__` 失败 :225-230）均已按要求加 `logger.debug(..., exc_info=True)` 留痕。建议后续批补一行 `logger.debug` 对齐不变式，不构成本批否决理由。

其余扫描干净：无裸 `except:`；`except Exception` 均带留痕（plugin_panel `# noqa: BLE001` + warning+exc_info）；无 UI 线程 GL 调用新增；无 HDR/色彩矩阵改动。

## 回退验证建议（抽 2 条）

1. **AUD-1209**（src/shared/gpu/composite_gpu_frame_handle.py:278-283）
   - 当前原文（逐字）：
     ```python
    try:
        glDeleteSync(_int_to_sync(fence_ns))
    except (_OpenGLError, ctypes.ArgumentError) as exc:
     ```
     （except 体为 `logger.warning("delete_fence(0x%x): glDeleteSync failed (%r); GLsync leaked", fence_ns, exc)`）
   - 回退为：
     ```python
    try:
        glDeleteSync(_int_to_sync(fence_ns))
    except Exception:
        pass
     ```
   - 应失败节点：`tests/shared/test_ef17_aud.py::TestAud1209DeleteFenceDiagnostics::test_failure_is_logged_not_swallowed`（caplog 断言 warning 留痕，吞错后无日志 → 失败）
2. **AUD-1403**（src/shared/gpu/metal/context.py:504-506）
   - 当前原文（逐字）：
     ```python
            with self._lock:
                self._last_gpu_time_ms = None
     ```
   - 回退为：
     ```python
            self._last_gpu_time_ms = None
     ```
   - 应失败节点：`tests/shared/test_ef17_aud.py::TestAud1403GpuTimeDiagLocking::test_submit_and_readers_hold_lock`（锁探针断言 submit 路径持锁，去掉 with 后探针不触发 → 失败）

## 过程备注（不扣分）

- 执行报告自称"未做任何 git 操作"，提交 `79e019e12` 系中心统一收编；提交内容与报告文件清单一一对应，无越界。
- AUD-25517 采用审计给出的降级选项（惰性化而非后台线程化），报告如实披露，接受。

## 最终结论

**PASS-with-notes**。20/20 FIXED 独立核实为真实修复；重点项（GL 池化与异常释放、metal 计时锁内读写且锁内无重活、histogram 单趟扫描数值等价、lease 违规真实上报）全部成立。Critical 0 ｜ ISSUE 1（ISSUE-1，Suggestion 级：release() 池清理 `except Exception: pass` 无留痕，建议补 debug 日志）。允许 --mark。
