# EF-01 子运行 1/3 修复报告 — src/features/edit/core（24 条 Suggestion）

- 日期：2026-10-01
- 分支：dev（未做任何 git add/commit/branch/push）
- 白名单：仅 `src/features/edit/core/` 下文件
- 测试文件：`tests/edit/test_ef01_aud_part1.py`（54 用例全绿）

## 一、逐条结论

| ID | 结论 | 位置（文件:行，修复后） | 说明 |
|----|------|------------------------|------|
| AUD-15200 | FIXED | src/features/edit/core/__init__.py:7-18 | 模块 docstring 职责清单与 `__all__` 对齐：补 edit_project_io/timeline_render_plan，删除未导出的 timeline_ops |
| AUD-15204 | FIXED | src/features/edit/core/audio_capabilities.py:223-234 | 升级分支读到 `current is None`（get/reset 竞态窗口）时就地补探测，杜绝向调用方返回 None |
| AUD-15423 | FIXED | src/features/edit/core/audio_health.py:73-119 | probe_dir_size_mb 增加 `max_seconds` 时间预算（默认 5s），超时告警并返回部分统计；窄化裸 except 为 OSError + logger 留痕 |
| AUD-15505 | FIXED | src/features/edit/core/audio_playback.py:1580-1605, 1636-1674 | add_track/remove_track 锁内只摘除字典，`decoder.close()`（文件 IO）移到锁外执行 |
| AUD-15506 | FIXED | src/features/edit/core/audio_playback.py:94-106, 1510-1521, 2058-2072 等 | mix 热路径 4 处函数内 import 提升为模块级（adapt_channels/pan_gains_for_channels/apply_clip_fade/balance_gains）；AudioGraphRenderer._apply_declick_edges 因反向依赖改为 mixer 实例惰性缓存（_apply_declick，3 个调用点收敛）；solo 可听闭包按失效式缓存（7 个变更入口 `_invalidate_solo_closure`，mix_samples 持同锁读取） |
| AUD-15511 | FIXED | src/features/edit/core/audio_playback.py:3452-3467 | `msleep(5)` 移出"仅首次"日志守卫，每个反压轮次都退避；日志去重保留（与 written==0 分支同构） |
| AUD-15514 | FIXED | src/features/edit/core/audio_playback.py:4775-4789 | manager.stop() 改为 wait(1000) + 超时告警 + 收尾 wait 的两段式（与 quiesce_for_timeline_rebuild 一致） |
| AUD-15601 | FIXED | src/features/edit/core/audio_waveform.py:207-226 | WaveformCache.__new__ 的 check-then-create 整体持类级 `_mutex`（QMutex lock/unlock，不支持 with），并发首建只产生单实例 |
| AUD-15605 | FIXED | src/features/edit/core/audio_waveform.py:57-60, 916-917, 1168-1185 | cancel_all 改为 wait(WAVEFORM_CANCEL_WAIT_MS=1500)；超时 worker 转入 `_detached_workers` 托管（保引用防 GC 崩溃），finished 统一回收 + deleteLater |
| AUD-15606 | FIXED | src/features/edit/core/audio_waveform.py:78-95 | WaveformData 文档/行内注释改为幅值包络语义（max(|samples|)，非正半波），与 _PeakAggregator 实现一致（实现不变） |
| AUD-15607 | FIXED | src/features/edit/core/audio_waveform.py:919-922, 1010-1016, 1032-1041, 1112-1141, 1157-1160 | 每个 asset 引入请求代际：request_waveform 新请求代际 +1 并绑定到失败处理器；_on_extraction_failed 仅当代际匹配才清回调，迟到的旧代际失败保留新回调 |
| AUD-15610 | FIXED | src/features/edit/core/automation_recorder.py:311-315, 326-330, 384-401, 417-428, 461-468 | AutomationRecorder 增加 threading.Lock；record 的读-改-写与 stop 的原子取走/cancel/start 全部串行化，消除样本表重绑竞态 |
| AUD-15611 | FIXED | src/features/edit/core/automation_recorder.py:19-23, 397-398 | 按审计建议采文档路线：模块头显式声明 Touch release 未实现（松手即停写、回归由既有曲线插值产生，属调用方/后续任务），record 内同步注明 |
| AUD-15801 | FIXED | src/features/edit/core/captions/commands.py:714-733 | promote 兜底分支先 `with_track_removed` 摘除首建空图形轨再建兜底轨，`_created_track_id` 只记录兜底轨——undo 后无泄漏空轨 |
| AUD-15802 | FIXED | src/features/edit/core/captions/commands.py:178-197 | `changed_fields` 为空（no-op 样式提交）时跳过锁定 cue 校验；真实变更仍拒绝 |
| AUD-15803 | FIXED | src/features/edit/core/captions/commands.py:479-485, 511-520 | _build_macro 增加 same-clip 拒绝（"Cannot merge a clip with itself"）与间隙拒绝（`second.timeline_start > first.timeline_end` 报 "gap"），与 docstring 声明一致 |
| AUD-16111 | FIXED | src/features/edit/core/chapter_export.py:9-14 | 模块头 docstring 改为 TIMEBASE=1/1000 毫秒语义，与实现及函数 docstring 一致 |
| AUD-16112 | FIXED | src/features/edit/core/chapter_export.py:68-77, 85-90 | format_youtube_chapters 在章节数 <3 时 logger.warning（仍输出，调用方可感知）；函数 docstring 声明该行为 |
| AUD-16316 | FIXED | src/features/edit/core/crop_cpu.py:40-68, 75-83 | 契约统一：4 元组（历史形态）合法、feather 默认 0.0；apply_crop_cpu/_crop_rect_from_tuple 类型放宽为 Tuple[float, ...] |
| AUD-16317 | FIXED | src/features/edit/core/crop_render.py:235-240 | 删除 astype(np.float32) 后冗余 `.copy()`（astype 默认拷贝，4K RGBA 每帧省 ~132MB 分配） |
| AUD-16416 | FIXED | src/features/edit/core/edit_frame_coordinator.py:615-619 | request_frame 成功取帧后 emit frame_ready(asset_id, frame_index, quality, frame)；失败（None）不发射 |
| AUD-16419 | FIXED | src/features/edit/core/edit_frame_coordinator.py:86, 291-293, 600-602, 836-866 | 实现"播放态停帧驻留升级"：ClipHandle 新增 last_requested_at（单调时钟），_select_quality 在同帧驻留 >= `_quality_dwell_seconds`(0.5s) 时升级 master；docstring 同步为已实现规则 |
| AUD-16422 | FIXED | src/features/edit/core/edit_frame_coordinator.py:313-321, 775-820 | _on_state_changed/on_pause/on_play 的 `_is_paused`/`_current_quality` 写入纳入 `_lock`；is_paused/current_quality 属性同锁读取，与 request_frame 锁内读统一纪律 |
| AUD-16504 | FIXED | src/features/edit/core/edit_gpu_decode_manager.py:457-467, 484-492 | 段注释与 docstring 的"拒绝并返回 placeholder"旧契约描述删除，改为"仅遥测、不阻塞、无特殊 frame_type"；行为本就正确（未改逻辑） |

无 INVALID / DUPLICATE / BLOCKED 条目（24/24 FIXED；根因全部在白名单内）。

## 二、验证关卡输出摘要

1. **py_compile（全部改动源文件 + 测试）**：`ALL_COMPILE_OK`
   覆盖 12 个源文件 + tests/edit/test_ef01_aud_part1.py。
2. **新测试**：`.venv/bin/python -m pytest tests/edit/test_ef01_aud_part1.py -q -p no:cacheprovider`
   → **54 passed in 2.94s**（全绿，无挂起）。
3. **定向既有测试（均无新增失败、未触发已知预存失败清单）**：
   - test_audio_playback.py — 100 passed (1.94s)
   - test_edit_gpu_decode_manager.py — 49 passed
   - test_fix_wd9g56_aud.py（edit_frame_coordinator 回归）— 36 passed
   - test_crop_render_stage.py — 20 passed
   - test_chapter_markers.py — 19 passed
   - test_automation_recorder.py — 18 passed
   - test_audio_capabilities.py — 10 passed
   - test_audio_health_report.py — 15 passed
   - test_caption_split_merge_style.py — 7 passed
   - test_caption_global_style.py — 6 passed
   - test_caption_lock_enforcement.py — 13 passed
   - test_caption_graphics.py — 19 passed
   - test_audio_waveform_lifecycle.py — 2 passed
4. **回退敏感性验证（随机抽 2 条）**：
   - AUD-15511：临时把 `msleep(5)` 放回日志守卫内 → `TestAud15511::test_backpressure_sleeps_on_every_blocked_cycle` **FAILED**（sleeps==[5] 缺第二次退避）→ 还原修复。
   - AUD-16317：临时还原 `.astype(np.float32).copy()` → `TestAud16317::test_no_redundant_copy_after_astype` **FAILED** → 还原修复。
   - 还原后新测试文件复跑 54 passed。

## 三、改动文件清单（全部在白名单内）

源文件（12）：
- src/features/edit/core/__init__.py
- src/features/edit/core/audio_capabilities.py
- src/features/edit/core/audio_health.py
- src/features/edit/core/audio_playback.py
- src/features/edit/core/audio_waveform.py
- src/features/edit/core/automation_recorder.py
- src/features/edit/core/captions/commands.py
- src/features/edit/core/chapter_export.py
- src/features/edit/core/crop_cpu.py
- src/features/edit/core/crop_render.py
- src/features/edit/core/edit_frame_coordinator.py
- src/features/edit/core/edit_gpu_decode_manager.py

测试文件（1，新建）：
- tests/edit/test_ef01_aud_part1.py

## 四、备注

- 未触碰白名单外目录（其他批次的 ai_workflow/composite 等改动与本报告无关）。
- .clinerules 约束自查：无 UI 线程 GL 调用、无裸 except（顺带把 audio_health 的
  `except OSError: pass` / `except Exception: return None` 收敛为带 logger 的
  OSError 处理）、新增性/改签名处均有类型注解、无 HDR 数值钳制、无矩阵混用。
- AUD-15506 的 AudioGraphRenderer 惰性缓存是为规避
  audio_graph_renderer → audio_playback 的模块级循环依赖（无法按审计建议直接
  提升为模块级 import），语义与建议一致（热路径零逐块 import）。
