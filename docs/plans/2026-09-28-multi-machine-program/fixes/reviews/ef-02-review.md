# EF-02 复查（GLM5.3）—— src/shared/media 45 条 Suggestion

- 复查日期：2026-10-01
- 批次提交：`ffc8d4f3899b0796fcef88bdb48890cb1a67e7fd`（[fix-E ef-02] … (45)）
- 白名单（e_flash_batches.json modules 键）：`src/shared/media`
- 执行报告：fixes/reports/ef-02-part1.md（23 条）、ef-02-part2.md（22 条）
- 复查方式：只读。git show/log/diff + 当前 HEAD 源码逐条独立核实 + 测试文件抽查。未运行 pytest（中心统一跑），未执行任何 git 写操作。

## §6 八项检查结论

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **通过** | `git show --stat ffc8d4f38` 共 36 文件：33 个源文件全在 `src/shared/media/`（含 macos_vt/、windows_hw/ 子目录）+ 2 个报告（fixes/reports/ef-02-part{1,2}.md）+ 2 个新测试（tests/media/）。未碰 REMEDIATION_PLAN.md、e_flash_batches.json、FIXLOG.md |
| 2 | 真实修复（逐条） | **通过** | 45/45 条均以报告 file:line 在当前 HEAD 独立核实到实现证据（逐 ID 表见下）。45 条全部 FIXED，报告无 INVALID/DUPLICATE/BLOCKED |
| 3 | GLSL/HDR 红线 | **通过** | diff 新增行零裸 `except:`、零 `except Exception: pass`。本批不涉及 GLSL/着色器。packet_demux.py 的 bt2020c→bt2020nc 归一是 AUD-35103 的既有行为 docstring 化（下游 `gpu_frame.VALID_COLOR_MATRICES` 确认不含 'bt2020c'，理由成立），非 601/709 混用，无 HDR 钳制 |
| 4 | 并发 | **通过** | UI 线程 GL 调用：无新增。锁纪律修复本身经核实：`_REGISTRY_LOCK`(RLock)、`_DETECT_LOCK` 双检、`_capability_probe_lock` 单飞、`_FINGERPRINT_LOCK` 固定顺序（_hw_lock→_FINGERPRINT_LOCK）、d3d11 releaser 锁外释放、proxy_manager 慢 IO 出锁。`_availability_checked` 惰性标志无锁但幂等（最坏重复告警一次），可接受 |
| 5 | 资源 | **通过** | CFRelease finally 释放（vt_compression）、close() join 后台线程（ffmpeg_source）、_cv_locks finally 清理（thumbnail_provider）、V9 spare 槽 close 释放（topos_source）、submit 失败冲销 pending+释放 lease（d3d11_pipeline）。异常路径无泄漏 |
| 6 | 异常 | **通过（附注）** | 新增 `except Exception` 均带 logger.warning/debug 留痕且符合 docstring 契约（warm_gop_index 返回 None、gop 后台构建 debug+exc_info）。附注：proxy_manager.get_stats 中 `except OSError: pass` 为既有代码随锁范围平移（diff 中删除后原样重建），非本批引入，未留痕但属统计场景良性；不构成否决 |
| 7 | 测试质量 | **通过（附注）** | 新测试 101 用例（part1 59 + part2 42）。抽查 AUD-34304/34502/35605/35904 等行为级断言均体现修复语义、回退即失败。附注：约 12 条为"源码哨兵"（inspect.getsource/缩进检查），报告已如实披露，用于并发锁纪律等难以稳定行为化的场景；哨兵断言弱于行为断言，属已知折衷 |
| 8 | 回退有效性抽查 | **通过** | 见"回退验证建议"节 2 条；flash 报告亦自带 2+2 条回退敏感性验证记录 |

## 逐 ID 核实表（45/45，全部 FIXED → 判定 CONFIRMED）

| ID | 报告结论 | HEAD 证据（file:line） | 复查判定 |
|----|---------|----------------------|---------|
| AUD-34304 | FIXED | adapter.py:405-470 `_detect_actual_bit_depth` 注册表→plane_infos→metadata→max() 兜底四级顺序 | CONFIRMED |
| AUD-34313 | FIXED | asset_proxy_manager.py:209-227 未配置分支无条件返回占位 entry；:250 的 assert 在早退后已不可达（仅类型收窄） | CONFIRMED |
| AUD-34314 | FIXED | asset_proxy_manager.py:319,428,481,510 emit 均 `_dataclass_replace` 快照；:489-510 `_mark_failed` 以 replace 新对象 upsert | CONFIRMED |
| AUD-34318 | FIXED | cache.py:397-420 区间校验（start>end 告警/负起点截断/>5 万帧告警）+ 锁内仅键快照；:431-447 prefetch_hint 负帧号过滤 | CONFIRMED |
| AUD-34422 | FIXED | color_metadata.py:694-703（transfer）/758-765（primaries）/810-817（space）未识别返回 'unknown'，透传仅限映射表标准值 | CONFIRMED |
| AUD-34502 | FIXED | decode_queue.py:380-433 `_take_entry` + :435-472 `_scan_decoded_queue_for` Queue.mutex 下原地摘除保序；不匹配语义入 docstring | CONFIRMED |
| AUD-34505 | FIXED | decode_queue.py 全部 10 处 `_stats` 写入（295/322/336/421/433/471/484/600/659/667）逐一核实均在 `with self._request_lock` 块内 | CONFIRMED |
| AUD-34510 | FIXED | disk_proxy_service.py:462-483 锁内复验缓存，竞态落败方走挂回调/直接交付分支不入队；:507 `_attach_pending_caller_callbacks` | CONFIRMED |
| AUD-34514 | FIXED | disk_proxy_service.py:696-723 get_proxy_stats 计数读取在 `with self._lock` 内；:312 磁盘发现不累计 bytes_saved（仅 :767 实际生成累计） | CONFIRMED |
| AUD-34603 | FIXED | factory.py:395-400 未识别 backend 抛 ValueError | CONFIRMED |
| AUD-34612 | FIXED | factory.py:162-166 `_REGISTRY_LOCK`(RLock)+`_REGISTRY_INITIALIZED`；:527-533 命中无锁快读+锁内双检；:619 锁内置位 | CONFIRMED |
| AUD-34709 | FIXED | ffmpeg_source.py:1155-1162 close() join(timeout=2.0)+清引用；:1570-1572 后台 `_build` 写回前检查 `self._is_open` | CONFIRMED |
| AUD-34711 | FIXED | ffmpeg_source.py:1592-1596 IO/解析异常记 warning 返回 None（有留痕，非吞错） | CONFIRMED |
| AUD-34807 | FIXED | frame_cache.py:510-525 docstring 改 LRU 访问序；:544-556 `_evict_one` 取首个满足条件条目，免 O(n) 全表 min 扫描 | CONFIRMED |
| AUD-34813 | FIXED | gpu_frame.py:362-366 `__post_init__` planes 归一 tuple；:18 模块设计原则同步改述（不 frozen 的理由：白名单外 async_renderer 惰性 VT 绑定需整体替换字段，已在报告中披露） | CONFIRMED |
| AUD-34814 | FIXED | gpu_frame.py:251-262 enqueue 构造期 ValueError；全仓 `destructor_policy` 仅 warn_only 两处使用（d3d11_gl_bridge.py:126、topos_source.py:1201），无破坏 | CONFIRMED |
| AUD-34909 | FIXED | hwaccel.py:159-185 `_DETECT_LOCK`(RLock) 双检；:640-676 构造期不探测，告警惰性至首次 get_decoder_for_codec 且仅一次 | CONFIRMED |
| AUD-35001 | FIXED | macos_vt/capability.py:69-72 `_capability_probe_lock`；:439-460 缓存命中仅短持 `_capability_lock`，probe 全程在探测锁上单飞 | CONFIRMED |
| AUD-35010 | FIXED | macos_vt/gpu_export_bridge.py:90-91,131-141 `_close_lock` 检查-置位；:119 samples 注解 Any→np.ndarray | CONFIRMED |
| AUD-35011 | FIXED | macos_vt/hardware_capability.py:265-282 load→probe→store 整体在 `_hw_lock`；:57-59,82-94,334-339 `_FINGERPRINT_LOCK` 懒初始化/清空同锁 | CONFIRMED |
| AUD-35103 | FIXED | macos_vt/packet_demux.py:203-215 docstring 注明 10=bt2020c 刻意归一为 bt2020nc 及理由；映射行为未变（:210 `10: 'bt2020nc'`）。理由独立核实：gpu_frame.py:47 VALID_COLOR_MATRICES 确不含 'bt2020c' | CONFIRMED |
| AUD-35204 | FIXED | macos_vt/topos_surface.py:183-193 仅 IOSurfaceUnlock 成功（int(status)==0）后才清 `slot.mapped_buffers`，失败 raise 保留映射 | CONFIRMED |
| AUD-35209 | FIXED | macos_vt/vt_compression.py:482-499 finally 中 CFRelease key/value 两 CFStringRef | CONFIRMED |
| AUD-35309 | FIXED | macos_vt/vt_session.py:385-389,459-471,526-536 计数 `+=` 与 property 读全部持 `self._lock` | CONFIRMED |
| AUD-35319 | FIXED | macos_vt/vt_zero_copy_probe.py:590-598 `frames_rendered_count == 0` 首帧守卫 | CONFIRMED |
| AUD-35407 | FIXED | performance_monitor.py:869-877 死 elif 分支删除（留注释说明）；docstring（:831-848）改为逐帧超预算计数口径；822/824 的 frame_budget_ms 属另一活代码方法，未误删 | CONFIRMED |
| AUD-35409 | FIXED | performance_monitor.py:992-998+ 各类数据 `_calculate_stats(list(...))` 仅一次拷贝排序 | CONFIRMED |
| AUD-35502 | FIXED | pixel_format.py:65-66（YUV_410 垂直 1/2）、:278（1.5×8 算式注释）、:688-692,706（ProRes 4444 无 alpha 表述） | CONFIRMED |
| AUD-35605 | FIXED | proxy_generator.py:204-213 generate() 不再重置 `_cancelled`（构造函数 :171 初始化） | CONFIRMED |
| AUD-35611 | FIXED | proxy_manager.py:196,285-288,307 `_prune_terminal_tasks_locked()` 有界保留终态任务 | CONFIRMED |
| AUD-35612 | FIXED | proxy_manager.py:463-480 校验与取值整体 `with self._lock`（RLock 重入） | CONFIRMED |
| AUD-35613 | FIXED | proxy_manager.py:430-441（is_proxy_valid）、:529-541（get_stats）、:555-560（cleanup）锁内快照→锁外 stat/remove→锁内校验写回 | CONFIRMED |
| AUD-35617 | FIXED | proxy_manifest.py:263-297 get_entry 返回 replace 拷贝、upsert_entry 存储 replace 拷贝 | CONFIRMED |
| AUD-35618 | FIXED | proxy_manifest.py:243-249 ensure_loaded double-checked locking | CONFIRMED |
| AUD-35704 | FIXED | seek.py:110-115 docstring 示例改为 100100（100 × fps.den） | CONFIRMED |
| AUD-35714 | FIXED | thumbnail_provider.py:924-929,1658-1660,2011-2013 三个提取路径 finally 调用 `_cleanup_file_lock` | CONFIRMED |
| AUD-35717 | FIXED | thumbnail_provider.py:194-201 `_get_bridge` 全程持 `_bridge_rebuild_lock` | CONFIRMED |
| AUD-35718 | FIXED | thumbnail_provider.py:516-528 `_instance_lock` 双检（ClipThumbnailCache）；:957-969 instance() 加锁+GUI 线程断言（无 QApplication 放行） | CONFIRMED |
| AUD-35810 | FIXED | topos_image_sequence.py:524-529 槽位回写纳入 `with self._prefetch_lock` | CONFIRMED |
| AUD-35811 | FIXED | topos_image_sequence.py:177-184 PlaneCache.put 入口先过滤 None 平面再记账/冻结 | CONFIRMED |
| AUD-35904 | FIXED | topos_source.py:607-610,852-854,950-965,985-988 `_v9_spare_planes` 复用槽（getrefcount<=2 判定）+ 交付前 `flags.writeable = False` + close 释放 | CONFIRMED |
| AUD-36006 | FIXED | windows_hw/capability.py:50-62 取值 ∈ {auto,off,force} 校验，未知记 warning 归 auto；模块文档注明 force 语义 | CONFIRMED |
| AUD-36103 | FIXED | windows_hw/d3d11_pipeline.py:147-165 submit() 捕获 executor shutdown 的 RuntimeError：冲销 pending、释放 lease、`_finish_failed` 归还槽位 | CONFIRMED |
| AUD-36111 | FIXED | windows_hw/d3d11_texture_pool.py:389-410 锁内只改状态收集指针，锁外 `self._releaser(...)` | CONFIRMED |
| AUD-36115 | FIXED | windows_hw/d3d11_gl_bridge.py:62-79 `_count_failure` 写入与计数同锁；:72-77 负 frame_index 拒绝；:144-156 三个诊断 property 读持锁。报告对"`%` 产生负槽键"机制描述的更正（Python `%` 恒非负）属实 | CONFIRMED |

## 红线发现

- **Critical：0**。
- 次要观察（不否决）：
  1. proxy_manager.py get_stats 的 `except OSError: pass`（:538-539）为既有代码随 AUD-35613 锁范围平移，非新增；建议后续批补一行 debug 日志。
  2. 测试中约 12 条源码哨兵断言（缩进/字符串包含）强度低于行为断言，报告已披露；行为可回退验证的条目均已配行为反例。

## 回退验证建议（供中心抽检，回退后对应 pytest 节点应失败）

1. **AUD-34422** — 文件 `src/shared/media/color_metadata.py` 当前 ：697-703：
   ```python
       for std_name in COLOR_TRANSFER_MAP.values():
           if name_lower == std_name.lower().replace('-', '_').replace(' ', '_'):
               return std_name

       return 'unknown'
   ```
   回退为末行 `return name`（旧行为：未知串透传原值）。应失败节点：
   `tests/media/test_ef02_aud_part1.py::TestAud34422AliasContract::test_unknown_transfer_returns_unknown`
2. **AUD-35605** — 文件 `src/shared/media/proxy_generator.py` 当前 ：203-213（generate() 入口仅保留 `_pause_event.clear()`）。回退为在 `start_time = time.time()` 之后重新插入一行 `self._cancelled = False`。应失败节点：
   `tests/media/test_ef02_aud_part2.py::TestAUD35605::test_cancel_before_generate_survives`
   （同类 `::test_generate_does_not_reset_flag` 为源码哨兵，也会失败）

## 附加验证

- py_compile：EF-02 + EF-13 全部 44 个改动源文件 `PY_COMPILE_OK`。

## 最终结论

**PASS**。45/45 FIXED 全部独立核实为真实修复；零越界、零红线违规；测试 101 项含行为级回归锁定。附注两条（既有 OSError 静默平移、少量源码哨兵断言）不构成扣分。建议中心执行 `check_fixes.py --mark`（45 条，commit ffc8d4f38）并统一跑 `tests/media/test_ef02_aud_part{1,2}.py` 确认。
