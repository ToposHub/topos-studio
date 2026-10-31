# EF-02 子运行 2/2 修复报告 —— src/shared/media Suggestion 级（22 条）

- 日期：2026-10-01
- 分支：dev（未做任何 git 操作）
- 白名单：仅 `src/shared/media/` 下文件
- 结论：**22/22 全部 FIXED**（0 INVALID / 0 DUPLICATE / 0 BLOCKED）
- 与 part1 改动共存于工作树，未提交（由主会话统一提交）

## 逐条结论

| ID | 结论 | 位置（文件:行） | 一句说明 |
|----|------|----------------|---------|
| AUD-35309 | FIXED | src/shared/media/macos_vt/vt_session.py:385-391,459-471,527-536 | `_lock` 不再是死代码：`_total_count`/`_dropped_count` 的 `+=`（VT 回调线程）与 property 读全部持锁，消除读-改-写竞态与"已加锁"错觉 |
| AUD-35319 | FIXED | src/shared/media/macos_vt/vt_zero_copy_probe.py:590-600 | `verify_color` 的 CPU reference 读取（Lock/Unlock + 裸指针读）加 `frames_rendered_count == 0` 首帧守卫——结果只在首帧渲染后消费一次，其余帧不再白读并污染计时口径 |
| AUD-35407 | FIXED | src/shared/media/performance_monitor.py:831-876 | 删除永不可达的 `elif current_index == -1` 分支与死变量 `frame_budget_ms`；docstring 改为与实现一致（逐帧超预算计数驱动、total_p95 仅进 reason 日志） |
| AUD-35409 | FIXED | src/shared/media/performance_monitor.py:992-1002,1024-1096 | decode/upload/plane_prepare/texture_write/yuv_render/queue/render_queue/held 各类数据只拷贝排序一次（每秒锁内聚合路径的重复 3-4 倍开销消除），结果值不变 |
| AUD-35502 | FIXED | src/shared/media/pixel_format.py:59-68,277,686-691,703 | 三处注释修正：YUV_410 垂直子采样 1/2（与 chroma_height_divisor 实现一致）、chroma_bits 算式改为 1.5×8、ProRes 4444 注明注册表 yuv444p10 无 alpha（实际解码 yuva444p10le，见 AUD-35501） |
| AUD-35605 | FIXED | src/shared/media/proxy_generator.py:204-213 | 删除 generate() 入口的无条件 `self._cancelled = False`（生成器单次使用，构造函数已初始化）——启动窗口内的 cancel 不再被吞、状态不再被覆盖为 complete |
| AUD-35611 | FIXED | src/shared/media/proxy_manager.py:199-201,288-292,303-341 | 新增 `_prune_terminal_tasks_locked()`（按 completed_at 有界保留 32 条终态任务），入队时淘汰——`_tasks` 不再无界增长，get_task_status 对近期任务仍可查询 |
| AUD-35612 | FIXED | src/shared/media/proxy_manager.py:475-481 | `get_valid_proxy_path` 校验与取值整体纳入 `with self._lock`（RLock 可重入），消除失效窗口、统一加锁口径 |
| AUD-35613 | FIXED | src/shared/media/proxy_manager.py:425-468,526-546,557-596 | is_proxy_valid/get_stats/cleanup_expired_proxies/cleanup_all_proxies 改为"锁内字典快照 → 锁外 stat/remove → 锁内校验写回"，慢速盘 IO 不再阻塞全部查询；失效写回校验映射未变，不误删新条目 |
| AUD-35617 | FIXED | src/shared/media/proxy_manifest.py:249-270,277-291 | `get_entry` 返回 `dataclasses.replace` 拷贝、`upsert_entry` 存储拷贝——调用方无法绕锁改写内部 entry（与 AUD-34314 的 replace 语义一致） |
| AUD-35618 | FIXED | src/shared/media/proxy_manifest.py:240-250 | `ensure_loaded` 改 double-checked locking（快路径无锁、慢路径锁内复检），并发首访不再重复全量 load、upsert 不再被并发 load 清掉 |
| AUD-35704 | FIXED | src/shared/media/seek.py:110-115 | frame_to_pts docstring 示例改为真实输出 `100100`（100 × fps.den） |
| AUD-35714 | FIXED | src/shared/media/thumbnail_provider.py:938-942,1672-1674,2025-2027 | `_cleanup_file_lock` 在三个提取路径（海报帧/单帧/clip 批量）的池归还 finally 中调用，覆盖全部退出路径；`_cv_locks` 不再只增不减 |
| AUD-35717 | FIXED | src/shared/media/thumbnail_provider.py:194-214 | `_get_bridge` 重建全程持模块级 `_bridge_rebuild_lock`——并发 worker 不再各建 bridge 导致 provider 已连接 bridge 被脱离（结果静默丢失） |
| AUD-35718 | FIXED | src/shared/media/thumbnail_provider.py:28-29,529-537,969-993 | `ThumbnailProvider.instance()` 加锁 + GUI 线程断言（无 QApplication 环境不设限，保持测试可用性）；`ClipThumbnailCache.__new__` 加 `_instance_lock` 双检 |
| AUD-35810 | FIXED | src/shared/media/topos_image_sequence.py:524-533 | `_read_slot` 的槽位刷新回写 `_by_frame` 纳入 `_prefetch_lock` 临界区，消除本文件唯一绕过锁纪律的共享可变状态写 |
| AUD-35811 | FIXED | src/shared/media/topos_image_sequence.py:177-200 | `PlaneCache.put` 统一 None 契约：入口先过滤 None 平面再记账/冻结，防御一致（不再 p.nbytes 先行崩溃） |
| AUD-35904 | FIXED | src/shared/media/topos_source.py:607-611,852-855,943-962,985-989 | V9 读路径新增 `_v9_spare_planes` 复用槽（refcount 判定消费方已释放才复用，与 `_entry_free` 同口径；native 每帧全量重写与池路径同语义），且交付前 `flags.writeable = False` 对齐 read-only 契约；close 时释放槽 |
| AUD-36006 | FIXED | src/shared/media/windows_hw/capability.py:5-12,50-62 | `TOPOS_VIDEO_ZERO_COPY` 显式校验 ∈ {auto,off,force}，未知值记 warning 后按 auto；模块文档注明 Windows 上 force 仅按 auto（探测决定，no-force 哲学） |
| AUD-36103 | FIXED | src/shared/media/windows_hw/d3d11_pipeline.py:147-163 | `submit()` 捕获 executor 已 shutdown 的 RuntimeError（与 close() 的 TOCTOU）：冲销 pending、释放 frame lease、`_finish_failed` 归还槽位并落定失败 future，不再向 run() 泄漏异常 |
| AUD-36111 | FIXED | src/shared/media/windows_hw/d3d11_texture_pool.py:389-411 | `_return_texture` 与 close() 对齐：锁内只改状态并收集待释放指针，锁外调用 releaser——关闭后在途归还不再持池锁执行 COM Release |
| AUD-36115 | FIXED | src/shared/media/windows_hw/d3d11_gl_bridge.py:62-79,144-155 | `_count_failure` 的 reason 写入与计数同锁；`is_initialized`/`last_fallback_reason`/`fail_count` 读持锁；`frame_index` 非负校验（负值拒绝并计入诊断口径）。备注：审计"负 frame_index 产生负槽键"的机制描述在 Python 语义下不成立（`%` 结果恒非负），但"未验证"属实，校验仍有效 |

## 测试

### 新建 tests/media/test_ef02_aud_part2.py（42 用例，~1.1s）

- 每条 ID 至少一个正例；可回退验证的条目附反例（回退即失败）：
  - 行为级：35309（计数器并发读）、35407（-1 降级仍工作）、35409（decode 数据仅统计 1 次 + 结果不变）、35605（cancel 后 generate 仍 is_cancelled）、35611（超保留额度淘汰最旧/活跃任务不动）、35612（有效/无效路径）、35613（is_proxy_valid/get_stats/cleanup 的 stat 时刻锁深度为 0，插桩 RLock depth 判定）、35617（get/upsert 双向拷贝隔离）、35618（并发首访 load 恰一次）、35714（清理函数生效 + 调用点哨兵）、35717（4 线程并发取同一 bridge）、35718（非 GUI 线程 RuntimeError / 无 app 放行 / ClipThumbnailCache 并发单例）、35811（None 平面不崩溃且记账只计非 None）、36006（非法值归 auto、合法值透传，importlib.reload）、36103（shutdown 后 submit 返回失败 future、frame 释放、槽位归还）、36111（关闭后归还仍释放 + 计数一致）、36115（负 frame_index 拒绝 + reason 文本）
  - 源码/AST 哨兵（结构性/文档性修复）：35309（增量与属性读持锁）、35319（首帧守卫行）、35407（死分支/死变量/docstring 移除）、35605（无 `self._cancelled = False`）、35612（with self._lock 包裹）、35704（docstring 100100）、35717（`_bridge_rebuild_lock`）、35718（`_instance_lock` + QApplication/QThread 守卫）、35810（回写在 `with self._prefetch_lock` 内）、35904（V9 冻结 + spare 槽 + getrefcount）、36111（AST：`with self._condition` 块内无 `_releaser` 调用）、36115（AST：`_last_fallback_reason` 赋值在 with 内 + 属性读持锁）
- 无真实媒体解码（合成 ndarray / FakeLease / 注入假工厂）、无模态对话框、pytest.ini 正常加载

### 回退敏感性验证（抽 2 条）

1. AUD-35605：`generate()` 临时恢复 `self._cancelled = False` →
   `TestAUD35605` 2 个用例 **FAILED** ✓（标志被吞 + 源码哨兵命中）→ 还原修复 → 42 全绿
2. AUD-36111：`_return_texture` 临时还原为持锁调用 `_releaser` →
   `test_releaser_called_outside_lock` **FAILED（持锁调用 _releaser）** ✓ → 还原修复 → 42 全绿

## 验证关卡

1. `py_compile` 全部 15 个改动源文件：**通过**（PY_COMPILE_OK）
2. `pytest tests/media/test_ef02_aud_part2.py -q`：**42 passed in 1.1s**
3. 定向既有测试（均无新增失败）：
   - tests/media/test_disk_proxy.py + test_proxy_manifest.py + test_performance_monitor.py + test_pixel_format_registry.py → **252 passed**
   - tests/media/test_windows_hw_d3d11_device_pool.py + test_windows_hw_pipeline_policy.py + tests/shared/test_fix_wd17g92_aud.py + tests/media/test_topos_image_sequence.py → **106 passed, 4 skipped**（skip 为既有 Windows-only 用例）
   - tests/media/test_thumbnail_provider_thread_safety.py + tests/shared/test_fix_wd16g89_aud.py → **66 passed**
4. 已知预存失败（GPU 位精确/x265 10-bit/VT 探针）未触及、未修改

## 改动文件清单

源码（15）：
- src/shared/media/macos_vt/vt_session.py
- src/shared/media/macos_vt/vt_zero_copy_probe.py
- src/shared/media/performance_monitor.py
- src/shared/media/pixel_format.py
- src/shared/media/proxy_generator.py
- src/shared/media/proxy_manager.py
- src/shared/media/proxy_manifest.py
- src/shared/media/seek.py
- src/shared/media/thumbnail_provider.py
- src/shared/media/topos_image_sequence.py
- src/shared/media/topos_source.py
- src/shared/media/windows_hw/capability.py
- src/shared/media/windows_hw/d3d11_pipeline.py
- src/shared/media/windows_hw/d3d11_texture_pool.py
- src/shared/media/windows_hw/d3d11_gl_bridge.py

测试（1）：
- tests/media/test_ef02_aud_part2.py（新建）

未触碰：REMEDIATION_PLAN.md、src/features/edit/**（其他批次文件）、git 操作零次。
