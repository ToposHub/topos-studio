# EF-12 复查报告（GLM5.3，只读复核）

- 批次：EF-12（src/features/composite/nodes，15 条）
- 提交哈希：`87600a28c`（[fix-E ef-12] composite/nodes 15条 Suggestion 全修）
- 执行报告：`fixes/reports/ef-12.md`
- 复查日期：2026-10-01 ｜ 复查基线：当前 HEAD `9984d5939`
- 白名单：`src/features/composite/nodes/` ∪ `tests/` ∪ `fixes/reports/`

## §6 八项检查逐项判定

### 1. 越界检查 — PASS（1 处既有测试更新，披露与 diff 一致）

`git show --stat 87600a28c`：16 个文件：
- `fixes/reports/ef-12.md`（+77）✓
- 13 个源文件全部在 `src/features/composite/nodes/` 下 ✓
- `tests/composite/test_ef12_aud.py`（新建，+775）✓
- `tests/composite/test_scene3d_node_coverage.py`（+22/-4）——**报告已披露**的既有测试更新。

披露核对：diff 仅改 `TestRedrawWithView::test_valid_params_coerced_and_result_returned` 一处断言（`_armed_node()` 真实节点 → `Scene3DNode("__viewer3d_scene__")` 虚拟节点，原断言"导航值写入真实节点参数"正是 AUD-7312 认定的缺陷行为）+ 新增 `test_real_node_params_not_polluted_by_nav_values` 锁定新契约。与报告"既有测试更新（1 处，审计驱动的契约变更）"章节逐点吻合，无未披露改动。文件在 tests/ 白名单内。**披露一致，不构成越界。**

### 2. 真实修复（逐条独立核实）— 15/15 CONFIRMED

重点条目深查：

**AUD-6312（AI 生成注入锁内成组替换）**：`_TRANSPORT_LOCK`（threading.Lock）下三元组一次赋值（ai_gen_expand_node.py:81-85）；`_generation_stack()` 持锁快照（:96-100）；全部读点改走快照——`_build_job` :463、`_do_evaluate` :626，模块内无其他直接读全局处（grep 证实：除 setter/快照外零引用）。渲染线程不再可能观察到"新 transport + 旧 key_provider"。

**AUD-6802（帧插值回读缓存）**：`_read_input_cached`（frame_interpolate_node.py:116-145）按 (texture_id, w, h, revision) 键缓存，FIFO 上限 2 条（当前 a/b 对，4K RGBA32F ~264MB 封顶），revision 变化天然失效；模型路径 :374-375 两处读点改走缓存；cleanup :415 清空。上游内容变化经 revision 失效——正确性契约成立。

**AUD-7704（volume 512MB 预算降采样）**：`_MAX_SAMPLE_GATHER_BYTES = 512MB` + `budget_output_size`（volume3d_node.py:33-56）按等比 sqrt 缩放输出分辨率，保守上界用 steps（实际 n_samples ≤ steps，注释说明）；超预算时 logger.warning + `error_message` 写入降采样说明（:208-256）。**HDR 值域核实：无任何数值钳制——只降空间分辨率，密度/颜色/alpha 计算路径不变；文件内 `np.clip` 仅存在于既有的采样坐标索引钳制（ys/xs，:271-274，7c62bbdf0~1 前即存在），与 HDR 值域无关。** 快路径 TextureResult 尺寸同步用有效尺寸。

### 3. 红线扫描 — PASS（无 Critical）

`git show 87600a28c` 全部新增行扫描：
- 裸 `except:`：0；`except Exception:`：0（新增行）。
- `pass` 新增 1 处：frame_interpolate_node.py:136 `except KeyError: pass`——dict 刷新最近性的防御分支（get 已成功，del 竞态极窄），窄类型，可接受。
- scene3d_node.py:1194-1201 将旧 `(KeyError, Exception)` 冗余元组**收窄**为 `except KeyError`（AUD-7310 的一部分，属红线正向改进）。
- 7310 新增两处 `except Exception` + `logger.debug` + `# noqa: BLE001`（读 ctx.blend_func 探测 / best-effort 恢复）——均留痕，符合"吞错必留痕"。
- UI 线程 moderngl：无（改动均在求值/渲染线程路径）。
- HDR 钳制：无（invert_node 反而把"不钳制、负值是设计行为"写进契约）。
- 601/709：新增行零命中。

### 4. 测试质量 — PASS

`tests/composite/test_ef12_aud.py`（43 用例，与报告一致，py_compile 通过）：
- 锁类验证（AUD-6312/7606）用 `_GateLock`（门闩锁）+ Event + 超时断言：验证"写者持锁期间读者必须阻塞"（test_replacement_is_atomic_under_lock）、"第二次簿记必须被锁串行化"（test_bookkeeping_runs_under_cache_lock）——这是真正体现锁语义的正向断言，而非仅检查属性存在。
- AUD-7704 有 `test_evaluate_downsamples_instead_of_memory_error`（极端参数下断言降采样生效而非 MemoryError）与预算内不动路径的反例。
- AUD-6802 有"同一对邻帧重复求值每帧只回读一次"（计数断言）+ revision 失效 + 上限/清空用例。
- 锁验证全部用 Event+超时，无线程悬挂风险。

### 5. 回退验证建议（抽查 2 条）

| 条目 | 文件:行（当前 HEAD） | 回退操作 | 应失败的 pytest 节点 |
|------|---------------------|---------|---------------------|
| AUD-7200 | `src/features/composite/nodes/ramp_node.py:155-160` | 删除 `if ramp_type not in RAMP_TYPES:` 整个守卫块（6 行：if + logger.warning 3 行 + 回落赋值），恢复直读 `ramp_type = str(self.parameters.get("ramp_type", "linear"))` 后直接使用 | `tests/composite/test_ef12_aud.py::TestAud7200RampWhitelist::test_invalid_ramp_type_falls_back` |
| AUD-7312 | `src/features/composite/nodes/scene3d_node.py:3555-3556` | 将 `if is_virtual_viewer:` / `self.parameters[key] = value` 两行改回无条件 `self.parameters[key] = value`（删除门卫） | `tests/composite/test_ef12_aud.py::TestAud7312RedrawWithView::test_real_node_parameters_not_written` |

### 6. 综合结论 — **PASS**

## 逐 ID 核实表（15/15）

| ID | 计划指控 | 报告结论 | HEAD 独立核实（file:line 证据） | 判定 |
|----|---------|---------|-------------------------------|------|
| AUD-6312 | 三个进程级全局无锁非原子成组替换 | FIXED | ai_gen_expand_node.py:51 `_TRANSPORT_LOCK`；:75-99 setter 持锁赋值 + `_generation_stack()` 持锁快照；读点 :463/:626 全走快照（grep 证实无其他直接读点） | CONFIRMED |
| AUD-6706 | `_DETECTOR` 无锁可变共享 | FIXED（契约+快照访问器） | face_track_node.py:23-31 线程契约注释（仅初始化期设置）；:47-55 `current_face_detector()`；读点 :146/:174 全走访问器 | CONFIRMED |
| AUD-6802 | RIFE 路径每帧双全帧回读+上传 | FIXED（中期缓存） | frame_interpolate_node.py:72-75 声明；:116-145 实现键 (id,w,h,revision)、上限 2；:374-375 读点；:415 cleanup 清空 | CONFIRMED |
| AUD-6909 | HDR 反相语义未文档化 | FIXED（文档路线） | invert_node.py:6-15 模块 docstring + :40-48 HELP_TEXT：pivot 固定 1.0、无钳制、c>1 反相为负值是设计行为、下游自行定义负值处理。渲染零变更（报告披露测试锁定 channels 标记） | CONFIRMED |
| AUD-7000 | HELP_TEXT 参数名与 PARAM_DEFS 不一致 | FIXED | luma_keyer_node.py:30-41 参数名 black_point/white_point/softness/invert/output_mode 与 PARAM_DEFS 一致，不存在 low/high_threshold | CONFIRMED |
| AUD-7200 | ramp_type/extend_mode 无白名单校验 | FIXED | ramp_node.py:153-168 白名单校验 + logger.warning 回落 linear/clamp；模块 docstring 补 diamond 与 EXTEND_MODES | CONFIRMED |
| AUD-7209 | docstring 过度承诺光流/帧混合 | FIXED（如实重写） | retime_node.py:7-19 明示源端时间重映射+直通、use_optical_flow 为预留元数据不生效 | CONFIRMED |
| AUD-7309 | vbo_cache_hit 永不为 False / model_cache_hit 恒 None | FIXED（三态） | scene3d_node.py:57-80 `merge_vbo_cache_hit`/`model_cache_hit_from_status` 助手；循环体聚合 :837-838；:5202-5205 从 model_cache_status 换算；死引用 `_perf_model_cache_hit` 删除（grep 零命中） | CONFIRMED |
| AUD-7310 | GL 状态泄漏 + 宽泛 except | FIXED | scene3d_node.py:1836-1842 进入读 blend_func（失败 debug 留痕）；:1945-1953 finally 恢复（best-effort + debug 留痕）；:1194-1201 except 收窄为 KeyError | CONFIRMED |
| AUD-7312 | redraw_with_view 渲染线程直写真实节点 parameters | FIXED（虚拟节点门卫） | scene3d_node.py:3512-3525 契约 docstring；:3545-3557 `is_virtual_viewer = (self.id == "__viewer3d_scene__")` 门卫，真实节点不写入。审计建议后半（局部变量穿透 2000 行函数）明确披露未实施——Suggestion 级最小修复边界合理 | CONFIRMED |
| AUD-7401 | HELP_TEXT `constant_value` 与实际参数 `value` 不符 | FIXED | set_alpha_node.py:29-43 参数名改 `value` | CONFIRMED |
| AUD-7413 | caption 覆盖直写共享 parameters["text"] | FIXED（覆盖通道） | text_node.py:282-302 `_text_override` + `_effective_text()`；:403 哈希感知 override；5 处读点（:542/:701/:1344/:1532/:1651）全走访问器；:1142-1153 求值期 scoped override，结束置 None | CONFIRMED |
| AUD-7509 | docstring 承诺 warning 实际无日志 | FIXED（补行为） | unknown_placeholder_node.py:47 once 标志；:60-69 首次求值 logger.warning 含 original_type 与 node id | CONFIRMED |
| AUD-7606 | _vt_cpu_ok/_vt_cpu_miss 无锁读写 | FIXED（入锁） | video_frame_node.py:822-851 `_record_vt_cpu_gate` 持 `_cache_lock`；worker 写点 :977；读点 :1447 与 :1924 均在 `with self._cache_lock:` 块内（核实 :1442-1447 / :1922-1926）；死锁排查属实（_decode_frame 调用点在锁外） | CONFIRMED |
| AUD-7704 | 8192²×128 gather 无内存预算 | FIXED（512MB 预算降采样） | volume3d_node.py:33-56 预算函数；:203-222 降采样 + warning + error_message；**无 HDR 值域钳制**（见上）；快路径尺寸同步 | CONFIRMED |

## 红线发现

无 Critical。备注 2 条（非阻塞）：
1. scene3d_node.py:1838-1841 / :1949-1952 两处 `except Exception` + `logger.debug`（# noqa: BLE001）——已留痕且为探测/best-effort 恢复路径，符合"吞错必留痕"的宽限解释；如后续从严可提级为 warning。
2. AUD-7312 只做了审计建议的前半（虚拟节点门卫），后半（cam 覆盖值局部变量化）因 2000 行函数级重构明确披露未实施——真实节点写参数路径已关闭，残余风险（虚拟节点参数字典仍被写）属既有设计模式，可接受。

## 最终结论

**PASS**。15/15 真实修复；唯一既有测试更新已如实披露且与 diff 一致（断言更新正是缺陷行为本身）；无越界、无红线违规；锁类测试用门闩锁真实验证串行化语义。允许 `check_fixes.py --mark`（commit 填 `87600a28c`）。
