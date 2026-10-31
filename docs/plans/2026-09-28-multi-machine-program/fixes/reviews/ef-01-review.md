# EF-01 复查报告（GLM5.3）— src/features/edit/core

- 批号：**EF-01**（e_flash_batches.json 第 1 批，modules = src/features/edit/core，67 条 AUD-ID）
- 提交：**70dd698e1**（`70dd698e150622973213cf8e94e39b01d1eb4c63`，批内唯一提交，3 个子运行合并）
- 白名单：src/features/edit/core/ ∪ tests/ ∪ fixes/reports/
- 执行报告：fixes/reports/ef-01-part1.md（24 条）、ef-01-part2.md（24 条）、ef-01-part3.md（19 条）
- 新测试：tests/edit/test_ef01_aud_part1.py（1249 行）、part2（1148 行）、part3（1396 行），报告合计 169 用例全绿
- 复查日期：2026-10-01 ｜ 复查方式：只读复核（未改任何 src/tests/计划文件，未跑 pytest，未执行 git 写操作）

---

## 一、§6 八项检查判定表

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界检查 | ✅ 通过 | `git show --stat 70dd698e1` 共 43 文件：3 报告（fixes/reports/）+ 37 源文件（全部 src/features/edit/core/，含 realtime_playback/、scene_cut/、captions/ 子目录）+ 3 测试（tests/edit/）。REMEDIATION_PLAN.md、FIXLOG.md、e_flash_batches.json、check_fixes.py 均未触碰 |
| 2 | 真实修复（67 条逐条） | ✅ 67/67 CONFIRMED | 见下方逐 ID 表。每条均在当前 HEAD 源码中独立核实修复存在且针对审计指控的根因；无 INVALID/DUPLICATE/BLOCKED/PARTIAL 条目需要反向验证（执行报告即无此类结论，AUD-17414 的"*"备注经核实成立：旧码 `self._service=None` 后本会重试，实际修的是无锁赋值竞态 + 显式失败不固化，见 source_resolver.py:155-180） |
| 3 | GLSL/HDR 红线 | ✅ 通过 | 全部改动为 Python；新增行无 `min(x,1.0)` 式 HDR 钳制（唯一 `np.clip` 命中为 uint8 预览混合路径既有代码在 try 块内的位置迁移，非新增语义）；bt709/limited 默认标签与旧码逐字一致（仅记忆化，无 601/709 混用） |
| 4 | 并发（UI 线程无 GL 等） | ✅ 通过 | AUD-17404/18323/18325/18909 的 GL 释放全部走"GL 线程执行 / 非 GL 线程停靠入队 / 线程退出后销毁"契约；diff 新增行中的 ModernGL 字样均在注释/docstring。锁内重活收敛（AUD-15505 close 出锁、AUD-17301 信号出锁、AUD-17413 锁内仅非阻塞接口） |
| 5 | 资源（泄漏/延迟释放） | ✅ 通过 | AUD-16506（start 失败三态回收）、AUD-15605（超时 worker 托管防 GC）、AUD-18909（comp GL context/surface 幂等销毁，stop 两条路径 + 外层兜底）均核实 |
| 6 | 异常（裸 except/吞错） | ✅ 通过 | diff 新增行 0 个裸 `except:`；grep 命中的唯一 `except Exception: pass` 是报告 markdown 里对旧代码的描述文字；实际改动把 AUD-16505 的 `except Exception: pass` 替换为 warning + playback_trace 留痕；AUD-18325/18326 的裸 `except: pass` 改 logger.debug |
| 7 | 测试质量 | ✅ 通过（有备注） | 三份文件头/中/尾抽查共 14 个测试类；断言体现修复语义（如 `sleeps == [5, 5]`、`calls["n"] == 0`、位级 `assert_array_equal` 对拍），doc-gap 类条目用源码/文档断言（符合修复本体）。无恒真断言发现。执行报告自述的回退敏感性抽验（15511/16317/16801/17407/18906/19208）与我核实一致 |
| 8 | 复查产出 | 本文件 | — |

**综合结论：PASS-with-notes**（0 Critical，0 ISSUE，4 条备注见 §四；推荐放行 `--mark`）。

---

## 二、逐 ID 复核表（67 行）

判定图例：CONFIRMED = 修复真实存在且针对根因。证据列为当前 HEAD 的 file:line（行号为 AUD 注释锚点，正文在其后数行内）。

| ID | 执行结论 | 复核判定 | 证据（file:line） | 备注 |
|----|----------|----------|-------------------|------|
| AUD-15200 | FIXED | CONFIRMED | edit/core/__init__.py:7-18, 100+ | docstring 清单与 `__all__` 对齐：edit_project_io/timeline_render_plan 已列，timeline_ops 已移除（全文 0 命中） |
| AUD-15204 | FIXED | CONFIRMED | audio_capabilities.py:223-234 | `current is None` 分支就地重探测，不向调用方返回 None；升级分支持 `_cache_lock` |
| AUD-15423 | FIXED | CONFIRMED | audio_health.py:74-119 | `max_seconds=5.0` 时间预算 + 超时 warning + 部分统计返回；裸 except 收敛为 OSError+logger |
| AUD-15505 | FIXED | CONFIRMED | audio_playback.py:1580-1612, 1636-1680 | add_track/remove_track 锁内仅摘字典，`decoder.close()` 在 `QMutexLocker` 块外执行（两处 for 循环） |
| AUD-15506 | FIXED | CONFIRMED | audio_playback.py:99-103, 1618-1631, 2147-2154 | 4 个热路径函数已模块级导入；solo 闭包失效式缓存（7 个失效入口 + mix_samples 持锁读 2147-2154）；_apply_declick 惰性缓存规避循环依赖 | 见备注 3：2939 行残留一处函数级 import（容错分支，非审计指控的 mix 热路径） |
| AUD-15511 | FIXED | CONFIRMED | audio_playback.py:3455-3470 | `msleep(5)` 位于日志守卫之外、每个反压轮次执行；测试断言 `sleeps == [5, 5]` |
| AUD-15514 | FIXED | CONFIRMED | audio_playback.py:4767-4789 | `wait(1000)` + 超时 warning + 收尾 wait 两段式 |
| AUD-15601 | FIXED | CONFIRMED | audio_waveform.py:207-231 | `__new__` 整体持类级 `_mutex`（lock/try/finally unlock，QMutex 无 with） |
| AUD-15605 | FIXED | CONFIRMED | audio_waveform.py:57-60, 916-917, 1170-1189 | `wait(WAVEFORM_CANCEL_WAIT_MS=1500)` + `_detached_workers` 托管保引用 |
| AUD-15606 | FIXED | CONFIRMED | audio_waveform.py:74-95 | WaveformData docstring/行内注释改为幅值包络语义（max(\|samples\|)），实现未动（文档路线） |
| AUD-15607 | FIXED | CONFIRMED | audio_waveform.py:919-922, 1010-1016, 1033-1041, 1108-1141 | 请求代际 `_request_generations`；失败处理器携带 generation，不匹配即保留新回调（1131-1137） |
| AUD-15610 | FIXED | CONFIRMED | automation_recorder.py:326-330, 348, 385, 420, 478 | `_lock` 串行化 start/record/stop/cancel 四方法 |
| AUD-15611 | FIXED | CONFIRMED | automation_recorder.py:15-22, 309-317 | 模块头显式声明 Touch release 未实现（文档路线，与审计"二选一"一致） |
| AUD-15801 | FIXED | CONFIRMED | captions/commands.py:716-731 | 兜底分支先 `with_track_removed(created_track_id)` 摘除首建空轨，`created_track_id = fallback_track_id` |
| AUD-15802 | FIXED | CONFIRMED | captions/commands.py:178-197 | `changed_fields` 为空跳过锁定 cue 校验；真实变更仍抛（测试双向覆盖） |
| AUD-15803 | FIXED | CONFIRMED | captions/commands.py:479-486, 511-520 | same-clip 拒绝 + gap 拒绝（"Cannot merge a clip with itself" / "gap"），相邻合并仍放行 |
| AUD-16111 | FIXED | CONFIRMED | chapter_export.py:9-16 | 模块头订正为 TIMEBASE=1/1000 毫秒语义 |
| AUD-16112 | FIXED | CONFIRMED | chapter_export.py:6-11, 83-90 | <3 章节时 `logger.warning`（仍输出），docstring 声明该行为 |
| AUD-16316 | FIXED | CONFIRMED | crop_cpu.py:33-35, 52-67, 83-90 | 4 元组合法、feather 默认 0.0（`values.append(0.0)`），与 crop_is_identity 契约统一 |
| AUD-16317 | FIXED | CONFIRMED | crop_render.py:235-240 | `out = frame_rgba.astype(np.float32)` 后无 `.copy()`；测试含 `out.base is not frame` 独立性断言 |
| AUD-16416 | FIXED | CONFIRMED | edit_frame_coordinator.py:615-618 | 取帧成功后 `self.frame_ready.emit(asset_id, frame_index, quality, frame)`；失败不发射 |
| AUD-16419 | FIXED | CONFIRMED | edit_frame_coordinator.py:86, 597-599, 836-866 | `last_requested_at` 单调时钟 + `_select_quality` 驻留 ≥`_quality_dwell_seconds`(0.5s) 升级 master；测试回拨时钟验证 |
| AUD-16422 | FIXED | CONFIRMED | edit_frame_coordinator.py:311-321, 777-822 | `_on_state_changed`/`on_pause`/`on_play` 写入持 `_lock`；`is_paused`/`current_quality` 属性同锁读；测试用持锁阻塞线程验证 |
| AUD-16504 | FIXED | CONFIRMED | edit_gpu_decode_manager.py:460-472 | 段注释订正为"仅遥测、不阻塞、无特殊 frame_type"；行为本就正确（doc-gap，源码断言 + 行为测试双覆盖） |
| AUD-16505 | FIXED | CONFIRMED | edit_gpu_decode_manager.py:373-391 | `except Exception: pass` → warning + `playback_trace("decode_manager.playback_prefetch_error")`；caplog 测试验证留痕 |
| AUD-16506 | FIXED | CONFIRMED | edit_gpu_decode_manager.py:841-849, 861-881 | `_closing` 早退与 `t.start()` 失败两路径均回收 busy 预约/done_event/线程注册表 |
| AUD-16715 | FIXED | CONFIRMED | edit_project_io.py:593, 649, 1029-1063, 1120-1230 | `save_project_as`/`quick_save` 复用 `_AsyncSaveWorker`（QThreadPool 后台 ZIP 写）+ `manual_save_failed` 信号；`save_project` 本体保持同步 | 见备注 1（排队语义 + 信号暂无接线方） |
| AUD-16716 | FIXED | CONFIRMED | edit_project_io.py:624, 831, 843, 1020-1028 | `_generator_preflight_lock` + 线程安全读取器 `get_last_generator_preflight()` |
| AUD-16718 | FIXED | CONFIRMED | edit_project_io.py:1810-1819 | `waitForDone(5000)` + 超时告警 + 清 `_manual_save_workers` |
| AUD-16801 | FIXED | CONFIRMED | effect_instance.py:33-38 | `@dataclass(frozen=True)`；测试断言直接赋值抛 FrozenInstanceError |
| AUD-16805 | FIXED | CONFIRMED | effect_registry.py:596-608 | 逐 spec try/except + warning 隔离，单条畸形 spec 不中断循环；catalog 获取失败仍整体降级 |
| AUD-16806 | FIXED | CONFIRMED | effect_registry.py:519-533 | 类 docstring 重写为实际目录（audio 8 mixer FX / generator 2 / catalog 驱动） |
| AUD-16807 | FIXED | CONFIRMED | effect_registry.py:33-42, 156-176, 740-756 | EffectInstance 顶层导入（:36，无环）+ `_registry_lock`/`_AUDIO_DEFAULT_PARAMS_LOCK` 双检锁 |
| AUD-17004 | FIXED | CONFIRMED | playback_debug.py:160-173 | seq 分配 + 行组装 + `print(flush=True)` 同一 `_LOCK` 临界区 |
| AUD-17301 | FIXED | CONFIRMED | engine.py:366-385 | `set_mode` 锁内判定/赋值，`_set_state`（含 state_changed 发射）锁外执行 |
| AUD-17305 | FIXED | CONFIRMED | engine.py:1143-1156 | `get_default_engine()` 双检锁（`_default_engine_lock`） |
| AUD-17404 | FIXED | CONFIRMED | gpu_frame_pool.py:69-81, 186-190, 308-335, 337-352, 354-405 | `_defer_or_release`：非 GL 线程入队 `_pending_releases`，GL 线程 `ensure_gpu_frame` 入口 drain；stats 新增 deferred/pending；未创建真实 GL 上下文 | 见备注 4（shutdown 后队列由下次 drain / context 销毁兜底，代码已注明） |
| AUD-17405 | FIXED | CONFIRMED | playback_budget.py:80-85, 157-171, 247-260; engine.py:797-815 | in-flight 超时自愈（默认 2s、下限 4×budget）+ `abort_render` API；engine 异常路径显式 abort 后上抛 |
| AUD-17407 | FIXED | CONFIRMED | playback_budget.py:120-124, 186-217 | after_render 帧号一致性校验（不一致丢弃 timing 仍清占位）+ start_ns 健全性回退 + `budget_ms` 持锁 |
| AUD-17408 | FIXED | CONFIRMED | policy.py:335-352 | `get_default_policy` 双检锁；"GIL 保证"错误注释删除 |
| AUD-17413 | FIXED | CONFIRMED | source_resolver.py:265-270, 346-371 | `_ensure_proxy_lock`：check+登记+触发原子；`invalidate_proxy_cache` 同锁；锁内仅非阻塞接口 |
| AUD-17414 | FIXED* | CONFIRMED | source_resolver.py:155-180 | 失败不写回 `self._service`（下次重试）+ `_service_lock` 双检锁。执行报告的 *（审计"不再重试"子主张与旧码不符）经读旧码核实成立——旧码置 None 后本会重试；本修复的实际增量是无锁赋值竞态消除 + 失败不固化语义显式化 |
| AUD-17418 | FIXED | CONFIRMED | telemetry.py:20, 199-210 | ts 快照入锁；`field` 导入已删（:20 仅 `dataclass`） |
| AUD-17420 | FIXED | CONFIRMED | timeline_decode_scheduler.py:553-555, 605-611, 757-761, 782-786 | hits/misses 持 `_lock` 自增；stats 的 prefetch 计数 `_prefetch_lock` 读；reset_stats 补清 prefetch 三计数 |
| AUD-17423 | FIXED | CONFIRMED | timeline_decode_scheduler.py:700-751 | `shutdown(timeout_s=2.0)`：cancel_futures + 分片 `Future.result` 有界等待 + 超时告警继续关 stream；无参调用兼容 |
| AUD-17505 | FIXED | CONFIRMED | timeline_gpu_compositor.py:149-155, 288-294, 447-470 | `_stats_lock` 统一计数器读写清零 |
| AUD-17606 | FIXED | CONFIRMED | recording_manager.py:276-287 | TakeInfo.status docstring 补 "pending"/"completed_partial"（与 :301, :1307 赋值一致） |
| AUD-17812 | FIXED | CONFIRMED | scene_cut/cache.py:612-618, 667-676, 716, 762, 824, 850, 879, 929-938 | `_lock`(RLock) 覆盖全部 8 个公开方法；并发测试验证记账不变量 |
| AUD-17813 | FIXED | CONFIRMED | scene_cut/cache.py:1135-1167 | 按 `os.path.abspath(project_cache_dir)` 键控的模块级双检锁单例；同目录复用一次构造/LRU |
| AUD-17911 | FIXED | CONFIRMED | scene_cut/filters.py:307-315, 346-349 | `_reset_diagnostics()` 在 process() 开头重建（与 `__init__` 共用 `_make_diagnostics`） |
| AUD-17913 | FIXED | CONFIRMED | scene_cut/filters.py:198-201, 224-242, 503-524 | 追加即按 ±5/±3 窗口裁剪头；`motion_frames_seen`/`black_frames_seen` 累计计数器保持 guard 门控旧语义（:505, :523） |
| AUD-18015 | FIXED | CONFIRMED | scene_cut/models.py:505-510 | docstring 公式改四项 0.35/0.30/0.20/0.10；与 metrics.py:47-50 权重常量逐一对上 |
| AUD-18210 | FIXED | CONFIRMED | serialization.py:563-580 | keywords/metadata isinstance 校验兜底；rating try/except (TypeError, ValueError) |
| AUD-18215 | FIXED | CONFIRMED | sidechain_ducker.py:192-252, 258-283 | 阈值穿越分段 + `np.multiply.accumulate` 向量化；release/无信号段与旧逐样本循环位级一致（`assert_array_equal` 对拍），attack ≤1 ulp（atol 1e-12 通过）；含逐样本参考实现对拍 + 结构断言测试 | 见备注 2（gain_db < target_duck 边界语义差异） |
| AUD-18323 | FIXED | CONFIRMED | timeline_clip_frame_provider.py:673-696, 2235-2252 | 首次进入固化 `_gl_thread_id`；线程变更时 warning + `_invalidate_gl_thread_state` 全量作废后重绑 |
| AUD-18325 | FIXED | CONFIRMED | timeline_clip_frame_provider.py:2153-2176, 2224-2233 | 非 GL 线程停靠 `_debug_cache_clear_pending`，渲染线程入口 drain；owned 纹理释放失败 logger.debug 留痕；方法名落差 docstring 显式声明 |
| AUD-18326 | FIXED | CONFIRMED | timeline_clip_frame_provider.py:1364-1376 | info=None 默认标签 ("bt709"×3,"limited") 写入 tags_cache 记忆化；探测异常 logger.debug |
| AUD-18328 | FIXED | CONFIRMED | timeline_clip_frame_provider.py:205-215, 1188-1213, 1217-1245, 1296-1301 | `_async_decode_generation` 世代号：stop 递增（:1301），worker 循环条件 + 解码后 + pending 回写前三重检查（:1212/1217/1232/1241） |
| AUD-18411 | FIXED | CONFIRMED | timeline_export.py:2862-2880 | flip 前清上一轮遗留 + OR 合并（`_cancel_requested or _cancel_pending_before_start`），两通道取消不丢 |
| AUD-18511 | FIXED | CONFIRMED | timeline_frame_mapper.py:104-112 | docstring 明确 clip-local 播放时间语义（不含 source_start、reverse 不翻转），指向 source_frame/fractional_source_frame（文档路线，行为不变） |
| AUD-18605 | FIXED | CONFIRMED | timeline_metal_export_renderer.py:3495-3531, 4696-4700 | `_should_materialize_effect_chain` 纯函数新增"单效果 + 非 Metal 源 + max(half_x,half_y)>1 → 物化"分支 |
| AUD-18809 | FIXED | CONFIRMED | timeline_ops.py:56-66, 350-365, 800-810, 1340-1355, 1578-1591 | `_translate_model_error` + move_clip 跨/同轨、add_transition、overwrite（及 insert 既有路径）ValueError → TimelineOperationError（`from exc` 链） |
| AUD-18816 | FIXED | CONFIRMED | timeline_ops.py:1330-1352 | 波移改单次 `_rebuild(clips=...)` 原子平移，最终一致性由 `with_clip_added` 统一校验；测试 spy 断言 `with_clip_updated` 调用数为 0 + 参考结果对拍 |
| AUD-18905 | FIXED | CONFIRMED | timeline_preview_renderer.py:2807-2813, 3088-3103 | `_composite_layers` 主体 try/finally：异常路径恢复/删除 `_current_render_timeline_frame` |
| AUD-18906 | FIXED | CONFIRMED | timeline_preview_renderer.py:4326-4339 | `next(iter(...))` 加 StopIteration 防御（空表跳过淘汰）；测试用 `_RaceDict` 注入竞态 |
| AUD-18909 | FIXED | CONFIRMED | timeline_preview_renderer.py:1307-1345, 1359-1362, 7086-7093 | `_teardown_comp_gl_resources`（doneCurrent→surface.destroy→置空，幂等）挂入 `_teardown_decode_resources`；外层 shutdown 在 worker 退出后兜底补调 |
| AUD-19208 | FIXED | CONFIRMED | transition_mask.py:41-118, 206-207, 235-243 | 坐标网格 (w,h) LRU(16) + interior 判定 (type,w,h,progress) LRU(64)，锁保护；返回 `inside.astype(np.float32)` 新拷贝（调用方改写不污染缓存，有测试）；位级一致 + 不同 progress 不命中均有测试 |

**汇总：FIXED 67 / CONFIRMED 67；INVALID 0；DUPLICATE 0；BLOCKED 0。**

---

## 三、红线扫描发现

对 `git show 70dd698e1` 全部 src/ 新增行扫描：

1. **裸 `except:`**：0 命中。
2. **`except Exception: pass` 吞错**：0 命中（唯一 grep 命中位于 ef-01-part2.md 报告正文，是对旧代码的描述；源码侧实为删除该模式并替换为 warning+trace）。
3. **UI 线程 GL 调用**：0 命中。新增行中 ModernGL/glDelete 字样全在注释与 docstring；实际 GL 释放路径均为"GL 线程执行 / 停靠 / 线程退出后销毁"三契约之一（gpu_frame_pool.py:308-352、timeline_clip_frame_provider.py:2153-2176、timeline_preview_renderer.py:1307-1345）。
4. **HDR 钳制**：0 命中。唯一 `np.clip` 新增行为 uint8 预览混合（0-255）既有语句随 try 块重构的位置迁移（diff 同 hunk 内一删一增，语义不变）。
5. **601/709 混用**：无矩阵改动。AUD-18326 的默认标签 ("bt709","bt709","bt709","limited") 与旧码逐字一致（`git show 70dd698e1^` 第 1319 行核对），仅增加记忆化。

**结论：红线零违例。**

---

## 四、备注（非 Critical / 非 ISSUE，供中心会话知悉）

1. **AUD-16715 排队语义与失败信号接线（跨批集成点）**：`save_project_as`/`quick_save` 返回值语义变为"是否成功排队"；唯一外部调用方 `src/app/controllers/edit_controller.py:6353` 在排队成功即清 dirty 状态/更新最近工程。后台写失败时仅有 `manual_save_failed(str, str)` 信号 + error 日志——**当前 src/app 与 src/features/edit/ui 中尚无任何该信号的订阅者**（全仓 grep 确认）。属白名单外的接线工作（EF-03/EF-08 范畴），建议中心会话在后续批次跟踪：连接 `manual_save_failed` 到 UI 提示并恢复 dirty 标记，否则极端场景（排队成功但磁盘写失败）用户可能误信已保存。本批实现本身与审计建议一致，不构成否决项。
2. **AUD-18215 边界语义差异（理论性）**：旧逐样本循环 attack 段有 `if gain_db > target_duck` 守卫——当 `gain_db < target_duck`（仅可通过 duck 进行中把 target_duck_db 幅度调小触达，如 -12 改 -6）时旧码保持增益不变，新向量化实现会立即向新 target attack。听感无差别且新行为更符合参数变更直觉，parity 测试（固定参数）不覆盖此分支。已知即可，无需动作。
3. **AUD-15506 残留**：audio_playback.py:2939（`_samples_to_output_bytes` 的声道适配容错分支）仍有一处函数级 `from ... import adapt_channels`。该分支仅在输出声道数与混音结果不一致的冗余保护路径执行，非审计指控的 mix 热路径（2013-2412 行段），可留待后续清理。
4. **AUD-17404 shutdown 语义**：非 GL 线程调用 `shutdown()` 时句柄只入队不实际释放，若 GL 线程不再进入 `ensure_gpu_frame`，底层纹理延迟到 context 销毁由驱动回收。代码注释已声明该取舍（符合审计要求的 deferred-release 契约），非缺陷。

---

## 五、回退验证建议（2 条）

以下两条修复局部、可精确撤销、各有专用新测试；中心会话按 old（当前原文，逐字）→ new（回退后）修改后，对应 pytest 节点**应失败**，随后还原。

### 回退 1 — AUD-15511（反压每轮退避）

- 文件：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/audio_playback.py`（当前 3459-3470 行）
- old（当前代码原文）：

```python
            if not self._write_blocked_logged:
                self._write_blocked_logged = True
                playback_trace(
                    "audio_thread.output_wait",
                    frame=current_frame,
                    bytes_free=bytes_free,
                    chunk_bytes=chunk_bytes,
                )
            self.msleep(5)
            return False
```

- new（回退 = 把 msleep 放回"仅首次"守卫内）：

```python
            if not self._write_blocked_logged:
                self._write_blocked_logged = True
                playback_trace(
                    "audio_thread.output_wait",
                    frame=current_frame,
                    bytes_free=bytes_free,
                    chunk_bytes=chunk_bytes,
                )
                self.msleep(5)
            return False
```

- 回退后应失败的 pytest 节点：
  - `tests/edit/test_ef01_aud_part1.py::TestAud15511::test_backpressure_sleeps_on_every_blocked_cycle`（断言 `sleeps == [5, 5]`，回退后为 `[5]`）

### 回退 2 — AUD-15803（同 clip 合并拒绝）

- 文件：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/captions/commands.py`（当前 479-486 行）
- old（当前代码原文）：

```python
        # AUD-15803: 同 clip 合并是调用方错误。旧实现靠宏内第二条 Delete 因
        # 片段已删而抛 "not found"，经宏回滚后用户看到的是删除失败而非
        # "不能合并自身"，报错误导。
        if self.first_clip_id == self.second_clip_id:
            raise ValueError(
                f"Cannot merge a clip with itself: {self.first_clip_id}"
            )
```

- new（回退 = 整段删除，含注释）：

```python
```

- 回退后应失败的 pytest 节点：
  - `tests/edit/test_ef01_aud_part1.py::TestAud15803::test_merge_rejects_same_clip`（`pytest.raises(ValueError, match="merge a clip with itself")` 不再命中——旧路径经宏回滚后抛的是 "not found" 类错误或行为不同，match 必然失配）

---

## 六、最终结论

**PASS-with-notes**

- Critical：0
- ISSUE：0
- 备注：4（见 §四；最重要的是备注 1 —— `manual_save_failed` 信号的 UI 接线需后续批次跟踪）
- 建议：允许对本批 67 条执行 `check_fixes.py --mark "..." --commit 70dd698e1`；中心会话可另行执行 §五 两条回退验证以完成回归有效性抽查（执行报告自测的 6 条回退敏感性记录与本次独立核实一致）。
