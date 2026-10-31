# EF-02 子运行 1/2 修复报告 —— src/shared/media Suggestion 级（23 条）

- 日期：2026-10-01
- 分支：dev（未做任何 git 操作）
- 白名单：仅 `src/shared/media/` 下文件
- 结论：**23/23 全部 FIXED**（0 INVALID / 0 DUPLICATE / 0 BLOCKED）

## 逐条结论

| ID | 结论 | 位置（文件:行） | 一句说明 |
|----|------|----------------|---------|
| AUD-34304 | FIXED | src/shared/media/adapter.py:405-470 | `_detect_actual_bit_depth` 改为注册表 → plane_infos → metadata 一次解析，`max()` 仅兜底（暗帧不再误判、不再逐帧全量扫描） |
| AUD-34313 | FIXED | src/shared/media/asset_proxy_manager.py:209-226 | `ensure_proxy_async` 未 configure 分支无条件返回 MISSING 占位 entry（key 可用时带真实 key），不再落入 `assert self._manifest is not None` |
| AUD-34314 | FIXED | src/shared/media/asset_proxy_manager.py:317,427,480,499-511 | 4 处 `proxy_state_changed.emit` 改为 `dataclasses.replace` 快照；`_mark_failed` 以新对象表示状态迁移，不再原地改 entry |
| AUD-34318 | FIXED | src/shared/media/cache.py:58-61,390-425,428-465 | `get_missing_frames` 校验区间（start>end 告警、负起点截断、超 5 万帧告警），锁内仅做键快照、成员判断移到锁外；`prefetch_hint` 过滤负帧号 |
| AUD-34422 | FIXED | src/shared/media/color_metadata.py:690-698,758-759,810-811 | 三个 alias 函数未知分支按 docstring 契约返回 'unknown'；透传仅限映射表内全部标准值（smpte428 等合法名不回归） |
| AUD-34502 | FIXED | src/shared/media/decode_queue.py:341-477 | 抽取公共实现 `_take_entry` + 新增 `_scan_decoded_queue_for` 原地摘除（Queue.mutex 下重建 deque），期望帧命中不再打乱队列次序；不匹配语义写入 docstring |
| AUD-34505 | FIXED | src/shared/media/decode_queue.py:452-470,656-666 | `_prefetch_loop` 与取帧路径的全部 `_stats` 写入统一置于 `_request_lock` 内，与 `get_stats` 加锁读取配对 |
| AUD-34510 | FIXED | src/shared/media/disk_proxy_service.py:467-517 | GENERATING 置位前在锁内复验缓存，竞态落败方走既有分支（挂回调/直接交付）不入队；回调挂接抽取为 `_attach_pending_caller_callbacks` |
| AUD-34514 | FIXED | src/shared/media/disk_proxy_service.py:317-323,717-735 | `get_proxy_stats` 的计数读取整体移入 `with self._lock`；磁盘发现路径不再累计 `_total_bytes_saved`（口径只含实际生成，消除 cleanup 后重复累计） |
| AUD-34603 | FIXED | src/shared/media/factory.py:393-399 | `_select_backend` 对 'ffmpeg'/'topos'/'gstreamer'/'auto' 之外的名称按 create() docstring 契约抛 ValueError（已核实全部生产调用方只传合法值） |
| AUD-34612 | FIXED | src/shared/media/factory.py:162-167,274-277,525-534 | 新增 `_REGISTRY_LOCK`（RLock，注册链内 check_* 回调可重入）+ `_REGISTRY_INITIALIZED` 双检；`register_backend` 写入与延迟注册持锁，命中路径无锁快读 |
| AUD-34709 | FIXED | src/shared/media/ffmpeg_source.py:1155-1160,1568-1571 | `close()` 以 2s 超时 join GOP 索引后台线程并清引用；后台 `_build` 写回前检查 `_is_open`，关闭后不再写 `_gop_index`/落盘 |
| AUD-34711 | FIXED | src/shared/media/ffmpeg_source.py:1583-1599 | `warm_gop_index` 捕获 IO/解析异常，记 warning 后按 docstring 契约返回 None |
| AUD-34807 | FIXED | src/shared/media/frame_cache.py:510-526,542-558 | docstring/注释统一为 LRU 访问序语义；`_evict_one` 改取首个满足空闲条件的条目（条目序即 last_used_time 升序），免去 O(n) 最小值全表扫描 |
| AUD-34813 | FIXED | src/shared/media/gpu_frame.py:16-19,330-336,364-370 | `GpuDecodedFrame.__post_init__` 把 planes 归一为不可变 tuple；模块设计原则改述为"planes 构造期归一 tuple"（不 frozen：async_renderer 惰性 VT 绑定需整体替换字段，白名单外） |
| AUD-34814 | FIXED | src/shared/media/gpu_frame.py:238,251-262 | `GpuFrameRelease.__post_init__` 对未实现的 `destructor_policy="enqueue"` 构造期抛 ValueError（全库无使用方，已核实） |
| AUD-34909 | FIXED | src/shared/media/hwaccel.py:163-236,645-681 | 探测缓存 `_DETECT_LOCK`（RLock）双检，命中无锁快读；`HardwareDecodeConfig.__post_init__` 不再构造期触发子进程探测，可用性告警惰性推迟到首次 `get_decoder_for_codec` 且仅一次 |
| AUD-35001 | FIXED | src/shared/media/macos_vt/capability.py:69-72,437-461 | 探测（最长 30s 子进程）移入独立 `_capability_probe_lock` 单飞；`_capability_lock` 只保护缓存读写——invalidate/缓存命中不再被探测串行阻塞（保持防重复 probe 语义） |
| AUD-35010 | FIXED | src/shared/media/macos_vt/gpu_export_bridge.py:84-90,119,131-141 | `close()` 的 `_closed` 检查-置位移入实例 `_close_lock`；`write_audio_samples` 注解 `Any → np.ndarray`（TYPE_CHECKING 导入） |
| AUD-35011 | FIXED | src/shared/media/macos_vt/hardware_capability.py:57-59,80-94,265-282,334-339 | 持久化 verdict 的 load→probe→store 整体纳入 `_hw_lock`（并发探测不再覆写丢更新）；`_machine_fingerprint` 用专用 `_FINGERPRINT_LOCK` 懒初始化（固定顺序 _hw_lock→_FINGERPRINT_LOCK，clear 同锁置空） |
| AUD-35103 | FIXED | src/shared/media/macos_vt/packet_demux.py:203-215 | 采纳审计"刻意归一"选项：docstring 注明 10=bt2020c 刻意映射为 bt2020nc 及理由（VT 输出 NV12/P010 为 NCL 型、下游 VALID_COLOR_MATRICES 不含 bt2020c）；映射行为不变 |
| AUD-35204 | FIXED | src/shared/media/macos_vt/topos_surface.py:183-193 | `unmap_surface` 仅在 IOSurfaceUnlock 成功后清 `mapped_buffers`；失败时保留映射视图（memoryview 保活即 CPU 锁仍在），lease 停留 MAPPED 可重试，不再卡死 |
| AUD-35209 | FIXED | src/shared/media/macos_vt/vt_compression.py:471-500 | `_set_profile_level` 配置 CFRelease 签名并在 finally 中释放 key/value 两个 CFStringRef（每会话原生堆泄漏消除） |

## 测试

### 新建 tests/media/test_ef02_aud_part1.py（59 用例）

- 每条 ID 至少一个正例；行为可回退验证的条目附反例（回退即失败）：
  - 行为级：AUD-34304（注册表优先于 max 扫描）、34313（不再 AssertionError）、34314（旧引用不被改写）、34318（负帧号/非法区间）、34422（unknown + 合法名回归守卫）、34502（原地摘除保序 + 过期代丢弃）、34510（竞态不入队）、34514（发现不累计 bytes_saved）、34603（ValueError）、34612（并发单飞注册）、34709（close 阻塞于 join）、34711（异常返回 None）、34807（LRU 序 + 驱逐选最久未用）、34813（planes 归一 tuple）、34814（enqueue 拒绝）、34909（并发探测单飞 + 构造不探测 + 惰性告警一次）、35001（探测锁持有时缓存命中即时返回）、35010（并发 close 链只跑一次 + 注解）、35011（verdict 落盘/内存双缓存 + 指纹锁）
  - 源码哨兵（结构性修复）：34505（stats 写入持锁）、34514（return 在锁内）、34709（写回前 _is_open 检查）、35001（compute 持探测锁）、35011（_store_hw_cache 在锁内）、35103（docstring 注明归一理由）、35204（先校验后清缓冲）、35209（finally CFRelease）
- 无真实媒体解码（合成 DecodedFrame/ndarray/mock）、无模态对话框（未创建 QApplication）、总耗时 ~1.4s

### 回退敏感性验证（抽 2 条）

1. AUD-34422：`_normalize_space_alias` 临时还原为 `return name_lower` →
   `test_unknown_space_returns_unknown` **FAILED** ✓ → 还原修复 → PASS
2. AUD-34814：`GpuFrameRelease.__post_init__` 临时跳过 enqueue 检查 →
   `test_enqueue_policy_raises` **FAILED (DID NOT RAISE)** ✓ → 还原修复 → PASS

## 验证关卡输出摘要

1. **py_compile**：17 个改动源文件 + 测试文件全部
   `PY_COMPILE_ALL_OK`
2. **新测试**：`.venv/bin/python -m pytest tests/media/test_ef02_aud_part1.py -q -p no:cacheprovider`
   → `59 passed in 1.35s`
3. **定向回归**（未整目录跑、未跑 tests/edit，无新增失败）：
   - `tests/media/test_gpu_frame.py tests/media/test_color_metadata_mapping.py` → 212 passed (0.89s)
   - `tests/media/test_disk_proxy.py tests/media/test_v3_hwaccel_policy.py` → 74 passed (0.95s)
   - `tests/media/test_media_source_factory.py tests/media/test_gop_index.py` → 29 passed (6.0s)
   - `tests/shared/test_fix_wd16g89_aud.py tests/media/test_proxy_manifest.py` → 86 passed (0.87s)
   - `tests/media/test_disk_proxy_integration.py tests/media/test_hwaccel_vendor_routing.py` → 47 passed, 1 skipped (0.63s)
4. 已知预存失败类别（GPU 位精确/x265 10-bit/VT 探针）本次均未触发。

## 改动文件清单

源码（17）：
- src/shared/media/adapter.py
- src/shared/media/asset_proxy_manager.py
- src/shared/media/cache.py
- src/shared/media/color_metadata.py
- src/shared/media/decode_queue.py
- src/shared/media/disk_proxy_service.py
- src/shared/media/factory.py
- src/shared/media/ffmpeg_source.py
- src/shared/media/frame_cache.py
- src/shared/media/gpu_frame.py
- src/shared/media/hwaccel.py
- src/shared/media/macos_vt/capability.py
- src/shared/media/macos_vt/gpu_export_bridge.py
- src/shared/media/macos_vt/hardware_capability.py
- src/shared/media/macos_vt/packet_demux.py
- src/shared/media/macos_vt/topos_surface.py
- src/shared/media/macos_vt/vt_compression.py

测试（1）：
- tests/media/test_ef02_aud_part1.py（新建）

未触碰白名单外文件；未执行任何 git 命令；REMEDIATION_PLAN.md 未改动。
