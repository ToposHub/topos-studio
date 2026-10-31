# EF-10 复查（GLM5.3）—— src/features/composite/render 16 条

- 批次：EF-10 ｜ 提交：`97b2a7ca066e5e35668e6cfce011b267ee31f36b`（dev）
- 白名单：`src/features/composite/render/` ∪ `tests/` ∪ `fixes/reports/ef-10.md`
- 复查日期：2026-10-01 ｜ 只读复查（未运行 pytest——中心已复跑全绿；本会话对 22 个改动源文件 + 2 个测试文件重跑 py_compile 全通过）

## §6 八项检查结论

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **通过** | `git show --stat`：10 个源文件全在 `src/features/composite/render/` + `tests/composite/test_ef10_aud.py` + 报告自身。未触碰 REMEDIATION_PLAN.md 及其他批次在途目录（render3d/nodes/color 等） |
| 2 | 真实修复 | **通过（16/16 CONFIRMED）** | 重点三项均核实为真算法/真护栏改动（详下）；2 处合理的"根因已修+残余最小修复"决策（7903、8419 的 json/md5 部分）有证据支撑且诚实披露 |
| 3 | GLSL/HDR 红线 | **通过** | 无 GLSL 改动；diff 无 HDR 钳制、无 601/709 触碰；8203 的 f4 读回 + RGB2BGR 转换为既有路径 |
| 4 | 并发 | **通过** | 8313 属主线程护栏反而**减少**了非渲染线程 GL 调用；8403/8404 fence 删除在既有渲染线程节拍内；7916/8304/8416/8206 加锁方向正确（锁内无重活——7916 锁内仅属性读，无死锁面） |
| 5 | 资源 | **通过** | 8203 thumb_tex finally 归还（acquire 前置 None 哨兵，release 自身异常 warning 留痕不吞原异常）；8403/8404 fence 删除杜绝 GLsync 泄漏；80020 遥测有界 |
| 6 | 异常 | **通过（1 条 note）** | 新增 except 均为具体类型（JSONDecodeError/IOError、OSError、TypeError/ValueError）或留痕的 `except Exception`（8203 release 兜底 warning）；`get_disk_cache_store()` 中 `except Exception: pass` 为**预存代码随双检锁重排搬移**（diff 上下文可见旧块删除/新锁块加入），非新增吞错 |
| 7 | 测试质量 | **通过** | 45 用例全纯逻辑（GL 边界 monkeypatch stub）；抽查 8403/8313/7920 断言真实可证伪；报告的回退敏感性抽验（8303、8403 实测变红）方法正确 |
| 8 | 综合 | **PASS** | 见最终结论 |

## 逐 ID 核实表（全部 16 条）

| ID | 判定 | 当前 HEAD 证据 |
|----|------|---------------|
| AUD-7903 | CONFIRMED | ai_cache_manager.py:78-105 `_read_disk_manifest_locked`：读失败 `os.replace(manifest, manifest+".corrupt")` 备份 + warning（备份再失败二次 warning）；:68-75 `_load_manifest` 补 warning。原子落盘部分（tmp+os.replace）核实确已被 AUD-7900/7901 前置修复——报告披露准确，残余修复（不再静默清空）真实在位 |
| AUD-7916 | CONFIRMED | ai_compute_manager.py:1280-1285：`get_preview_result` 的 `_workers.get`/`_preview_results.get` 整体移入 `with self._lock`（锁内仅属性读，与其余访问方式一致，无回调无死锁面） |
| AUD-7918 | CONFIRMED | ai_compute_manager.py:1076-1082、:1146-1152、:1172-1179：三处全部 `np.random.default_rng(seed)`/`default_rng()` 线程局部生成器；`grep np.random.seed` 零命中——进程级全局 RNG 污染消除 |
| AUD-7920 | CONFIRMED | cache_manager.py:654-668 `_eviction_candidates()`（类目优先级序 × `dict.items()` 插入序，单趟快照）+ :670-694 `_evict_to_memory_budget`（total 一次汇总逐项扣减）+ :696-714 `_evict_to_entry_count`（单趟）。**真算法改动**：逐轮全量重扫与每轮 O(n) estimated_memory_mb 汇总均已消除；`self._cache.get(key) is not entry` 防御失效候选。测试用 `estimated_memory_mb` 间谍断言零重扫调用 |
| AUD-80011 | CONFIRMED | composite_frame_renderer.py:163-165（无效 graph_data）、:178-180（snapshot 失败）、:252-255（异常兜底）三条 return None 路径统一 `self.last_frame_trace = None`（异常路径同时清 last_diagnostics） |
| AUD-80020 | CONFIRMED | composite_graph_render_service.py:244-257：`_TELEMETRY_MAX_ENTRIES=100` 类常量 + append 后 `del self._telemetry[:-100]`，与 `get_telemetry()` 末 100 条读取口径一致 |
| AUD-8203 | CONFIRMED | composite_renderer.py:1624-1673：acquire 前置 `thumb_tex = None` 哨兵，`finally` 中 `if thumb_tex is not None: release_texture`；release 自身异常 `except Exception as release_exc: logger.warning(...)` 留痕不吞 |
| AUD-8206 | CONFIRMED | render_scheduler.py:1389-1396 `@property latest_request_id`（持 `_id_lock` 读取）；composite_renderer.py:1676-1689 `_handle_ai_result_ready` 两处（含 AUD-8201 附近的另一处）改走访问器，grep 无 `._latest_request_id` 直访残留 |
| AUD-8303 | CONFIRMED | disk_cache_store.py:299-309：store() 在 manifest 提交成功后按最终 `payload_path` 判向 `_safe_unlink` 兄弟格式（.toos↔.npz）。**选"提交后"而非审计建议的"写 manifest 前"的决策正确**——写前删除在 manifest 写失败时会留下指向已删旧格式的条目（has() 误报/load() 错解码），报告的关键决策说明 #2 论证成立且有反例测试锁定 |
| AUD-8304 | CONFIRMED | disk_cache_store.py:99（`_stats_lock`）+ :310/:326/:354/:357/:441（hits/misses/writes 与 stats() 读侧全部入锁）+ :498-517 `get_disk_cache_store()` 改 `_STORE_INIT_LOCK` 双检锁懒初始化（单例双实例竞争消除） |
| AUD-8313 | CONFIRMED | gpu_resource_pool.py:88-90（`_owner_thread_id`）+ :101-107（initialize 记录 `threading.get_ident()`）+ :159-165 query_available_vram_kb / :212 附近 query_total_vram_kb：非属主线程直接返回 `_vram_available_kb` 缓存快照、**绝不发 glGetIntegerv**；四个公共预算 API docstring 标注渲染线程契约（:1903/:1947/:1992）。测试把 `ctypes.CDLL` 换成"触达即 AssertionError"哨兵验证护栏——**真护栏非纯注释** |
| AUD-8316 | CONFIRMED | gpu_resource_pool.py:1387-1424 `_evict_to_free`：成本路径改为一次性 `_rank_cost_candidates()`（:1427-1448）降序遍历，`glo not in _pool_lru` 跳过已失效/已延迟释放候选——O(n²)→O(n log n)，坏候选不再死循环（有限列表遍历天然终止）；小池（≤3）保留原 LRU + max_iterations 安全限 |
| AUD-8403 | CONFIRMED | pbo_readback.py:186-196：enqueue 覆写槽位前对 prev（无论 data 是否已收集）`_delete_fence(prev.fence)` + 置 None——消费方停收 + 生产方持续 enqueue 的每圈 GLsync 泄漏堵住；已收集条目 fence=None 纯 no-op（测试三态覆盖：覆写/空槽/已收集） |
| AUD-8404 | CONFIRMED | pbo_readback.py:328-336：enqueue_sync 覆写槽位前同样删除被顶替条目 fence（结果丢弃属既有语义），混用 enqueue/enqueue_sync 的 fence 泄漏堵住 |
| AUD-8416 | CONFIRMED | render_scheduler.py:1430-1450 `is_stale_request` 三字段比较整体 `with self._id_lock`；:1398-1417 `mark_current` 的 `_latest_graph_generation/_latest_color_revision` 写入移入既有 `_id_lock` 块（request_id 取 max 防回退）——"新 id 旧 generation"撕裂中间态消除 |
| AUD-8419 | CONFIRMED | render_scheduler.py:1140-1176 `_create_temp_node` 按 (param_revision + parameters 同一性/相等) 备忘录化；:596 graph 代际回退 clear、:983-987 节点删除剪枝。json.dumps+md5 半项核实确已被 AUD-7502 覆盖（报告 DUPLICATE 判定诚实） |

## 红线扫描发现

- 新增行无裸 `except:`（零命中）。
- 唯一 `except Exception: pass` 出现在 `get_disk_cache_store()` 双检锁块内——diff 上下文显示为**预存代码搬移**（旧的非线程安全块被删除、同一逻辑移入锁内），非新增吞错。**note 级**：该预存吞错本批未顺手收窄（越权范围外，可理解）。
- 8203 的 `except Exception as release_exc: logger.warning` 在 finally 中且不吞原始异常路径（原异常已在 except 分支记录）——合规。
- 无 UI 线程 GL 调用新增；无 HDR 值域钳制；无 601/709 矩阵触碰。
- Note（非 ISSUE）：AUD-8419 备忘录返回**共享可变 Node 实例**，安全性依赖"ROI 规划只读实例"假设（docstring 已声明）。若未来某节点类型在 ROI 计算路径产生内部状态突变，两遍共享可能引入隐蔽行为差异——建议后续在该处加一行断言性注释或只读测试锁定。

## 测试质量抽查

- `TestAud8403EnqueueFenceLeak::test_enqueue_deletes_superseded_fence`（tests/composite/test_ef10_aud.py:806）：`assert deleted == [sentinel]`——删除 `_delete_fence` 调用立即失败；三态（覆写/空槽/已收集 fence=None no-op）全覆盖。
- `TestAud8313VramThreadContract::test_offthread_query_returns_cached_without_gl`（:649 附近）：`ctypes.CDLL` 哨兵"触达即 AssertionError"+ 断言返回缓存值——删除属主线程守卫立即失败（raw GL 触达哨兵）。
- `TestAud7920EvictionComplexity::test_memory_eviction_priority_and_no_rescan`（:206）：`estimated_memory_mb` 间谍断言零调用 + PREFETCH 先于 EFFECT 驱逐 + pinned 存活——回退到逐轮重扫立即失败。
- 全文件无 GL 上下文/Qt 实例化/模态对话框；并发用例 Barrier + setswitchinterval 还原。

## 回退验证建议（抽 2 条）

1. **AUD-8403** — 文件 `src/features/composite/render/pbo_readback.py`
   - 当前原文（old）：
     ```python
            # AUD-8403: the entry being overwritten here must not keep its
            # GLsync alive — consumers that stopped collecting (e.g. preview
            # hidden) while the producer keeps enqueueing would otherwise
            # leak one driver fence object per ring rotation. Collected
            # entries already carry fence=None, so this is a no-op for them.
            if prev is not None:
                self._delete_fence(prev.fence)
                prev.fence = None
     ```
   - 回退代码（new）：整块删除（含上面注释）
     - 应失败：`tests/composite/test_ef10_aud.py::TestAud8403EnqueueFenceLeak::test_enqueue_deletes_superseded_fence`
2. **AUD-8313** — 文件 `src/features/composite/render/gpu_resource_pool.py`
   - 当前原文（old）：
     ```python
            # AUD-8313: never issue raw GL from a thread without the context —
            # that is undefined behaviour on macOS and violates the
            # "UI/planning threads must not touch OpenGL" rule. Return the
            # cached snapshot instead.
            if (self._owner_thread_id is not None
                    and threading.get_ident() != self._owner_thread_id):
                return self._vram_available_kb
     ```
   - 回退代码（new）：整块删除（守卫移除后 off-thread 查询会触达被测试哨兵化的 `ctypes.CDLL`）
     - 应失败：`tests/composite/test_ef10_aud.py::TestAud8313VramThreadContract::test_offthread_query_returns_cached_without_gl`

## 最终结论

**PASS**（0 Critical / 0 ISSUE / 2 note）

- 16/16 真实修复；两处"根因已修+残余最小修复"（7903、8419 半项）披露准确、证据核实成立；AUD-8303 偏离审计建议的工程决策（manifest 提交后清理）论证正确且有反例测试锁定。
- Note 1：`get_disk_cache_store()` 预存 `except Exception: pass` 随双检锁搬移保留（非本批新增，未越权收窄）。
- Note 2：AUD-8419 共享可变 Node 实例依赖"只读"假设，建议后续加守护测试。
- 建议中心执行 `check_fixes.py --mark`（16 条全 FIXED）。
