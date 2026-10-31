# EF-20 复查（GLM5.3）

- 日期：2026-10-01 ｜ 复查会话：GLM5.3（只读复核）
- 提交：`246373f8a`（`[fix-E ef-20] 散点8目录 24条 Suggestion 全修 ... (24)`）
- 白名单（e_flash_batches.json modules 键）：`src/features/color/tracking`、`src/features/ai_workflow/ui`、`src/features/composite/utils`、`src/features/deliver/core`、`src/features/shared/effects`、`src/features/teaching_animation/core`、`src/shared/tracking`、`src/features/ai_workflow/adapters`
- 执行报告：`fixes/reports/ef-20.md`（结论 24/24 FIXED）

## §6 八项逐项判定

| # | 检查项 | 判定 | 依据 |
|---|--------|------|------|
| 1 | 越界 | **通过** | `git show --stat 246373f8a`：24 个文件 = 22 源 + 1 新测试（`tests/shared/test_ef20_aud.py`）+ 1 报告。22 个源文件逐一对照 8 个白名单目录键全部命中（含 `src/features/shared/effects`）。无 tests/ 既有文件改动、无 REMEDIATION_PLAN.md 触碰。 |
| 2 | 真实修复（逐条） | **通过**（24/24 CONFIRMED，见下表） | 逐 ID：grep 计划行 → 读报告结论 → HEAD 源码 file:line 独立核实 |
| 3 | GLSL/HDR 红线 | **通过** | 无 GLSL 改动；diff 无 HDR 钳制行；唯一 `np.clip` 相关命中是 AUD-11601 旧注释文字，非数值钳制代码；无 601/709 行 |
| 4 | 并发 | **通过** | 详见"重点核实"：IncrementalSaver 全路径 RLock；comfyui `_bind_lock` 覆盖 bind/caps/generate/submit 快照；registry RLock；`_CancelToken` Event 化；`_zombie_workers` 保活防 QThread 析构崩溃 |
| 5 | 资源 | **通过** | AUD-26405 中途失败逆序 release（vao→vbo→prog）；AUD-24910 worker 引用保活（间接防 QThread 运行中被 GC 的进程 Abort） |
| 6 | 异常 | **通过** | 新增 except 全部窄类型 + 留痕：`except OSError`（getmtime 降级 warning）、`except OSError as cleanup_exc`（原子写清理 warning）、2 处 `except Exception: # noqa: BLE001 — best-effort cleanup`（AUD-26405 清理路径，外层已有 `logger.warning("...blit program failed")` 留痕，符合"留痕"要求）、`except Exception: # noqa: BLE001 - 无法探测时退化为纯文件身份`（探测超时路径，有 warning）。无裸 `except:`（grep 0 命中） |
| 7 | 测试质量 | **通过** | `tests/shared/test_ef20_aud.py` 48 用例覆盖全部 24 条，正例+反例；deepcopy 等价性有专门对照测试（`test_aud24902_copy_geometry_matches_deepcopy_semantics`）；并发正确性有真实多线程压测（`test_aud11611_concurrent_base_swap_never_torn`：3 writer + 1 saver × 50 轮）；fake moderngl ctx / 假 worker / 打桩探测，无真实 GL/模态 |
| 8 | 复查产出 | 本文件 | 结论 **PASS** |

### 重点核实（复查指令指定）

- **追踪热路径 deepcopy 消除（数值等价）**：`tracking_evaluator.py:96-110` `_copy_geometry`——dict/list 递归重建、不可变值原样共享；审计点名的每帧调用点（:139、:148、:167、:176、:218，对应计划 :123/:132/:150-151/:202）全部换用；保留的 2 处 deepcopy（:65 `capture_base_geometry` 捕获时一次、:288 `_evaluate_binding_frame` 烘焙路径）均**非播放热路径**，属审计范围外的合理保留。等价性依据：`capture_base_geometry` 产出的结构只含 dict/list/标量/str（无 tuple 内嵌可变容器），隔离语义与 deepcopy 一致；测试 `test_aud24902_copy_geometry_matches_deepcopy_semantics` + `test_aud24902_identity_returns_isolated_copy`（嵌套改写不穿透）+ `test_aud24902_failed_frame_fallback_is_isolated` 双向锁定。
- **增量保存 RLock + 原子快照**：`serialization_formats.py:292` `RLock`；set/clear/属性读/compute_delta 全持锁；`save_incremental`（:546-551）锁内 compute→serialize→advance 原子化（`with saver._lock:` 包住三步）；`load_incremental`（:585-592）锁内读 base 快照+generation 对，防撕裂。
- **导出队列 O(n) 移除**：`export_queue.py:365-377` `clear_completed`——按 `(PENDING, RUNNING, PAUSED)` 状态集过滤，一次遍历构造 keep/removed；`_by_id` 同步重建；`job_removed` 信号逐个发射。O(n) 且消除"字段全同作业被值相等误删"（原 `j not in keep` 语义）。

## 逐 ID 核实表（24/24）

| ID | 计划指控（摘要） | HEAD 证据 | 判定 |
|----|------------------|-----------|------|
| AUD-24902 | 每帧 deepcopy 基准几何 | 见"重点核实"；`tracking_evaluator.py:96-110` + 5 个热路径调用点 | CONFIRMED |
| AUD-24910 | `_drop_worker` gen 未用 + isRunning 竞态残留 | `tracking_service.py:311-319` 闭包绑定 worker 引用按身份移除；:392-408 `_drop_worker`（无 gen 参数）收尾中线程转 `_zombie_workers`（:335、:407-408）；`_prune_zombie_workers`（:341-345）回收 | CONFIRMED |
| AUD-24911 | get_binding O(n) 热路径 | `tracking_store.py:29` `_by_shape` 二级索引；:43 add、:50-51 remove、:59 get、:75 clear、:109 from_dict 全同步维护 | CONFIRMED |
| AUD-24913 | 裸布尔跨线程取消 | `tracking_worker.py` `_CancelToken`：`threading.Event` 背书，`set()/is_set()` 接口与 Tracker2D 协议兼容 | CONFIRMED |
| AUD-23924 | 文档承诺双击卡片缺失 | `workflow_gallery_page.py:89-91` property + installEventFilter；:114-125 `eventFilter` MouseButtonDblClick→emit `template_selected`（与按钮 :108-109 等效） | CONFIRMED |
| AUD-24001 | 过滤只匹配截断 48 字符 | `workflow_node_search.py:29-31` `DescriptionRole`；:85 存完整 desc；:100-101 `_filter_items` 匹配完整描述（:82 截断仅显示层） | CONFIRMED |
| AUD-24006 | `_updating` 装饰性锁 | `workflow_panel.py:778-785` `set_run_drawer` 重入早退（真实读取）+ try/finally；:787-795 `_on_artifact_clicked` 槽在 `_updating` 期间丢弃程序性信号（:282 连接改经槽） | CONFIRMED |
| AUD-11532 | 持久化写入非原子 | `preset_manager.py:42-61` `_atomic_write_json`（同目录 tmp + `os.replace` + 失败清理留痕）；5 个写入点 :202/:328/:396/:420/:450 全部改走 | CONFIRMED |
| AUD-11534 | 默认 presets 目录取 cwd | :99-103 改用 `src.shared.paths.get_project_root()` 锚定仓库根 | CONFIRMED |
| AUD-11611 | IncrementalSaver 无锁 | 见"重点核实"；`serialization_formats.py:292` RLock 全路径 + save/load 原子化 | CONFIRMED |
| AUD-11901 | getmtime 未捕 OSError | `custom_export_presets.py:156-164` try/except OSError → 0.0 降级 + warning 留痕，列表不炸 | CONFIRMED |
| AUD-11907 | clear_completed O(n²) 值相等 | 见"重点核实"；`export_queue.py:365-377` 状态集合 O(n) | CONFIRMED |
| AUD-12001 | docstring 幻影 container_override | `video_export_presets.py:55-57` 删除该字段声明，明确 container 恒由 `video_config.container` 决定 | CONFIRMED |
| AUD-23015 | _submitted_jobs 无界增长 | `comfyui_executor.py:79` `_MAX_TRACKED_JOBS=256`；:418-420 OrderedDict；:516-528 `_track_submitted`（move_to_end + popitem(last=False) LRU 淘汰）；:491-493 幂等命中 move_to_end | CONFIRMED |
| AUD-23019 | bind 与 worker 线程 generate 无同步 | :409 `_bind_lock`；:447-449 caps 写、:464-472 bind_workflow 四字段原子替换、:482-483 submit_job 锁内快照 caps、:726-731 generate 锁内一次性快照三字段 | CONFIRMED |
| AUD-26302 | default_uniform_values 文档失实 | `dctl_wrapper.py` docstring 更正为"测试/工具用途纯映射助手，生产不经过"，行为保持 | CONFIRMED |
| AUD-26312 | 持锁扫盘 + is_known "cheap" 失实 | `effect_catalog.py` `_ensure_built`：锁外 `_build_dctl_plugin_specs()` + 双重检查 `_built`（锁内二次检查防重复注册）；`is_known` docstring 补懒构建成本说明 | CONFIRMED |
| AUD-26405 | ensure program 中途失败泄漏 | `effect_pass_runner.py:1415-1432`（blit）prog/vbo/vao 逆序 release，成功路径 :1433-1435 不变；combine 同型（报告 :1387-1395 一带） | CONFIRMED |
| AUD-27644 | frozen Mapping 共享引用靠自律 | `model.py:141-148`（SceneElement）、:412-415（TeachingSceneDocument）类 docstring + :156-158 字段行注释声明只读约定与 replace+新 dict 纪律（doc-gap 类修复，行为不变符合预期） | CONFIRMED |
| AUD-27718 | SceneTemplateRegistry 无锁 | `template_registry.py:63` RLock；register/unregister/get/list/`__contains__`（:67/:74/:78/:82/:86）全部持锁 | CONFIRMED |
| AUD-27801 | 省略号宽度按截断前字号 | `text_layout.py:254-265`：截断预留按截断前最大字号（保守），合成省略号单元按截断后行自身字号 `fs` 重测宽度；行为空回退截断前字号 | CONFIRMED |
| AUD-37307 | mask 与 region 语义矛盾 | `feature_detector.py:80-86`：`mask = cv2.bitwise_and(mask, combined)` 取交集，region 外点不参与检测（不再被 clip 夹到矩形边）；docstring 同步；mask=None 路径不变 | CONFIRMED |
| AUD-37404 | 失败路径漏传 ref_template | `tracker_2d.py:523-537` `_fail_step(... ref_template=ref_template ...)`（含 AUD 注释），与其余失败路径一致；测试 `test_aud37404_all_fail_step_call_sites_pass_ref_template` 锁定全部调用点 | CONFIRMED |
| AUD-37411 | 容器探测无超时阻塞调用线程 | `tracking_cache.py:31` `_MEDIA_PROBE_TIMEOUT_S=3.0`；:180-196 限时 daemon 线程 + join 超时降级 `probe-failed` 纯文件指纹 + warning 留痕 | CONFIRMED |

## 红线发现

- 无 Critical、无 ISSUE。新增行 0 个裸 `except:`；2 处 `except Exception` 均带 noqa 理由注释且外层/后续有 logger 留痕，属"best-effort 清理/降级"合法形态；无 UI 线程 GL 调用（AUD-26405 release 位于渲染线程 ensure 路径；AUD-37411 探测线程不触 GL）；无 HDR/色彩矩阵改动。

## 回退验证建议（抽 2 条）

1. **AUD-24913**
   - 文件：`src/features/color/tracking/tracking_worker.py`（`_CancelToken`，约 :26-49）
   - 当前原文（old，逐字）：
     ```python
    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()
     ```
   - 回退为（new，裸布尔旧行为）：
     ```python
    def __init__(self) -> None:
        self._cancelled = False

    def set(self) -> None:
        self._cancelled = True

    def is_set(self) -> bool:
        return self._cancelled
     ```
   - 应失败节点：`tests/shared/test_ef20_aud.py::test_aud24913_cancel_token_backed_by_event`（`isinstance(token._event, threading.Event)` 断言失败）
2. **AUD-11901**
   - 文件：`src/features/deliver/core/custom_export_presets.py:159-163`
   - 当前原文（old，逐字）：
     ```python
        try:
            fallback_mtime = os.path.getmtime(path)
        except OSError as exc:
            logger.warning("Stale user export preset vanished %s: %s", path, exc)
            fallback_mtime = 0.0
     ```
   - 回退为（new，无保护旧行为）：
     ```python
        fallback_mtime = os.path.getmtime(path)
     ```
   - 应失败节点：`tests/shared/test_ef20_aud.py::test_aud11901_vanished_file_mtime_failure_tolerated`（monkeypatch getmtime 抛 OSError 后旧代码直接传播异常）

## 最终结论

**PASS**。24/24 修复真实（含 3 个重点方向逐一核实），边界零越界，红线零违规，测试具备真实回归锁定能力。允许 `--mark`（commit 填 `246373f8a`）。
