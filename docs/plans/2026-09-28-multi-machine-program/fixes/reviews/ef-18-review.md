# EF-18 复查报告（GLM5.3）

- 批次：EF-18（src/features/teaching_animation/render 9 条 + src/shared/codec 6 条 + src/shared/export 6 条，共 21 条）
- 提交哈希：`cbda06cd1`（cbda06cd12fb01b79c656d63ed6cf991d90938c0，`[fix-E ef-18] … (21)`）
- 白名单：src/features/teaching_animation/render、src/shared/codec、src/shared/export ∪ tests/ ∪ fixes/reports/
- 执行报告：fixes/reports/ef-18.md
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show / git log -L / grep / HEAD 源码逐条核实，未运行 pytest、未做任何 git 写操作）

## §6 八项检查逐项判定

1. **越界检查：通过**。`git show --stat cbda06cd1` 共 18 个文件：teaching/render 7 个、shared/codec 3 个、shared/export 5 个、tests/teaching_animation/__init__.py（新建空文件，对齐 tests/shared 惯例）+ test_ef18_aud.py（新建 60 用例）+ fixes/reports/ef-18.md。全部 ⊆ 白名单 ∪ tests ∪ fixes/reports。**注意**：AUD-29804 对应的 asset_resolver.py 不在提交中——与报告 INVALID 结论自洽（未改该文件）。未触碰 REMEDIATION_PLAN.md；无 i18n locale 文件。
2. **真实修复：CONFIRMED（21/21）**。20 条 FIXED 逐条独立核实；1 条 INVALID（AUD-29804）经 git -L 历史独立验证成立（见下）。重点项（worker 工厂失败回收、Qt 捕获主线程 fail-closed、原子写目录 fsync）全部核实。
3. **红线扫描：通过**。新增行无裸 `except:`；无 `except Exception: pass`（新增异常全部具体类型：OSError×5、(OSError, ValueError)、(TypeError, ValueError)、queue.Full、BaseException-清理后-reraise）；无 GL/moderngl 改动（gpu_encode_surface 为 lease 所有权契约层）；无 HDR 钳制（max(0, …) 仅用于帧数计数，非像素值域）、无 601/709 内容。
4. **测试质量：通过**。test_ef18_aud.py 60 用例逐 ID ≥2（正例+反例/回归守护），断言体现修复语义：如 AUD-30217 工作线程 capture → `CaptureFailureCode.UNSUPPORTED` + "main thread"（回退守卫即失败）；AUD-32205 `set_calls == [1, 8]`（exit 不恢复即失败）；AUD-32302 fsync spy 断言目录 fd 被 fsync（durable=False 反例不 fsync）；AUD-30407 monkeypatch 计数 `_effect_filter` 每元素每帧恰 1 次。
5. **并发**：AUD-30310 `_handles` 三处读写全入 `_handles_lock`（submit/done 回调/shutdown）；AUD-33405 `_domain_stats_lock` + `_claim_warn` 原子 check-and-set（8×500 并发恰 4000/恰 16 次抽检测试在案）；AUD-32304 `_LUT_LOCK` 双检锁。无锁内重活。
6. **资源（重点核查）**：
   - AUD-30134：构造期工厂循环失败 → 逐个 `_close_quietly` 已建 worker 后 re-raise（html_renderer_service.py:88-95，浏览器后端不泄漏）。
   - AUD-32303：`os.open` 后整段构造并入同一 try（topos_image_binding.py:789-837），构造期异常（MemoryError/TypeError）统一关 fd + unlink tmp 再抛；测试注入 MemoryError 验证 fd 全关、tmp 不残留。
   - AUD-33403：哨兵重投 + join(2.0)×8 轮，工人全退出后才清空共享队列/cond；仍存活则保留引用（daemon 自灭）。
7. **异常**：`except BaseException:`（html_renderer_service 工厂回收）清理后原样 re-raise，不吞错；`except queue.Full: pass` 为重试循环控制流（下一轮继续投递），非错误掩盖；目录 fsync 失败 `logger.warning` 留痕不回滚成功写入。
8. **回归有效性**：报告抽 AUD-29804 守护、AUD-32205 回退验证可信；本复查另给出 2 条回退建议（见下）。邻接 3 个 VT 硬编探针失败经报告短路对照证实为环境预存（本机无 VT 硬编桥），非本批引入。

## 逐 ID 判定表（21/21）

| ID | 计划指控摘要 | 报告结论 | 复查判定 | HEAD 证据（file:line） |
|----|-------------|---------|---------|----------------------|
| AUD-29804 | data_uri 的 read_bytes() 无 OSError 处理 | INVALID（已被修） | **INVALID 成立** | asset_resolver.py:122-128 现状 `stat()`+`read_bytes()` 已包 `try/except OSError → None`（docstring 声明 unreadable→None）；`git log -L 115,135` 证实该守卫由批 D 提交 77f3588c9（`[fix-D w17] … data-uri size/MIME caps`）引入，早于 EF-18——计划行 :104 为旧行号。本批补回归守护测试 `test_vanished_asset_degrades_to_none`（TestAud29804DataUriOSErrorGuard），处置正确 |
| AUD-29809 | video.stat() TOCTOU 突破 Never-raises 契约 | FIXED | CONFIRMED | bitrate_probe.py:140-149 `_fallback_size()` 整体 `try/except OSError → 0`，注释指明 is_file/stat 两syscall 间隙 |
| AUD-30133 | backend_build 无超时阻塞获取 worker | FIXED | CONFIRMED | html_renderer_service.py:82-96：`__init__` 从首个 worker 缓存 `_backend_build`；`backend_build` property（:118-120 起）读缓存不再 acquire()（池忙时旧实现无界阻塞） |
| AUD-30134 | __init__ 工厂中途失败已建 worker 泄漏 | FIXED | CONFIRMED | html_renderer_service.py:86-95：`except BaseException:` 逐个 `_close_quietly(workers)` 后 re-raise（不吞错）；正/反测试在案 |
| AUD-30217 | 调用线程创建 QApplication/WebEngine 无主线程约束 | FIXED | CONFIRMED | qt_capture_backend.py:88-103（类 docstring 声明主线程约束）+ :165-173：任何 Qt 导入**之前** `threading.current_thread() is threading.main_thread()` 检查，非主线程 fail-closed 返回 UNSUPPORTED；主线程正例守卫不误伤（空 html → INVALID_INPUT） |
| AUD-30302 | render() 每帧 inspect.signature 反射 | FIXED | CONFIRMED | html_scene_renderer_v2.py:213-215（__init__ 探测缓存 `_service_accepts_allow_uniform`）+ :354（渲染路径查 flag，无反射）；测试断言 init 恰探测 1 次 + render 源无 inspect.signature |
| AUD-30307 | 冻结 PerfBudget.limits 持可变 Mapping | FIXED | CONFIRMED | perf_slo.py:34-40：`__post_init__` 拷贝后包 `MappingProxyType`（`object.__setattr__` 适配 frozen）；外部 dict 事后突变不影响预算、直接写抛 TypeError |
| AUD-30310 | _handles 无锁；render_sync 回调注解与 submit 不一致 | FIXED | CONFIRMED | render_worker.py:61-62（`_handles_lock`）、:75-76/:84-88（submit/done 回调持锁）、:97-104（shutdown 持锁快照+清空）；render_sync 注解 `Callable[[RenderProgress], bool \| None]` 与 submit 一致（:90、:107） |
| AUD-30407 | _filter_fragment 每元素每帧重复构建 | FIXED | CONFIRMED | svg_renderer.py:166-195：`_effect_defs` 返回三元组含 `filter_ids: dict[str, str \| None]`；两条绘制循环查表（:595 取 map、:624 `effect_filter_ids.get(...)`）；测试计数每元素每帧恰 1 次（旧实现 2 次） |
| AUD-32106 | acquire(capacity) 永久钉低进程级单例 | FIXED | CONFIRMED | decode_worker_budget.py:118-130：仅 `candidate > self._capacity` 增大方向采纳（monotonic），释放不回退；瞬时低值不再缩容 |
| AUD-32205 | ParallelSequentialDecoder 死代码 + 全局线程池副作用不恢复 | FIXED | CONFIRMED | topos_binding.py:4386-4390（`_prev_slice_threads` 记录，仅主动 set 过才在 `__exit__` :4408-4414 恢复；threads=None 全程不动全局旋钮）；PREFETCH/_owns_codec 全仓 grep 零命中；测试 `set_calls == [1]→[1,8]` / threads=None `set_calls == []` |
| AUD-32208 | encode() 文档引用不存在方法；decode() 死变量 widths | FIXED | CONFIRMED | topos_binding.py:1396 docstring 指 `encode_frame`（:1509 真实存在，`encode_with_stats` 零命中）；decode 源无 widths 变量（测试固化） |
| AUD-32302 | durable=True 未 fsync 父目录 | FIXED | CONFIRMED | topos_image_binding.py:909-922：atomic 替换后 `os.open(parent, O_RDONLY)` + `os.fsync(dir_fd)`（finally close）；不支持目录 fsync 的 OSError → `logger.warning` 留痕、不使成功写入回滚 |
| AUD-32303 | os.open 与首个 try 间无保护间隙泄漏 fd/tmp | FIXED | CONFIRMED | topos_image_binding.py:787-838：os.open 之后整段（回调定义/_CIo/create_string_buffer/derive_idsc）并入同一 `try/except Exception` → 统一 `os.close(fd)` + unlink tmp 再抛 |
| AUD-32304 | HALF LUT 惰性初始化无锁重复建表 | FIXED | CONFIRMED | topos_image_binding.py:101-142：`_LUT_LOCK` + 双检锁（与 get_image_codec 风格一致）；8 线程并发首建一致性测试在案 |
| AUD-33115 | save_batch_queue(settings=None) 抹掉已存 settings | FIXED | CONFIRMED | batch_queue_store.py:134-150：None 时锁内内联读旧文件合并回 payload（避免同模块非重入锁自锁）；旧文件损坏 `except (OSError, ValueError)` + warning 不阻塞 |
| AUD-33119 | ExportConfig 未冻结、framerate 弱类型、帧数可为负 | FIXED | CONFIRMED | encoder.py:407 `@dataclass(frozen=True)`；:431 `framerate: Optional['Rational']`（TYPE_CHECKING 导入防环，:23-25）；:447-448 `get_total_frames` 返回 `max(0, …)` |
| AUD-33208 | submit() 回调返回 False 不自动 release | FIXED | CONFIRMED | gpu_encode_surface.py:115-139：falsy 返回值自动 `self.release()`（幂等，调用方再 release 无害）并原样返回该值；异常路径同样释放；docstring 同步 |
| AUD-33212 | get_dnxhr_profile_for_resolution 的 width/height 未使用 | FIXED | CONFIRMED | presets.py:511-531：`max(width, height) >= 3840` 且请求 LB/SQ/HQ → 升 HQX；显式 HQX/444 保持；只升不降。竖屏 UHD 同样覆盖（max 语义） |
| AUD-33403 | _pipeline_discard join 超时后置空共享对象与存活 worker 竞争 | FIXED | CONFIRMED | topos_encoder.py:121-125（`_PIPELINE_DISCARD_JOIN_ROUNDS=8`）+ :1602-1636：哨兵重投 + join(2.0) 循环，全部线程退出后才置 None `_converted_queue/_converted_map/_convert_cond`；仍有工人存活保留引用（daemon 自灭，零 AttributeError） |
| AUD-33405 | _frames_domain_checked 与告警标志并发无同步 | FIXED | CONFIRMED | topos_encoder.py:926 `_domain_stats_lock`；:2682-2694 `_should_full_domain_check` 计数器读改写 + `_claim_warn` 原子 check-and-set；7 处一次性告警标志（:3148/:3153/:3440/:3448/:3515/:3575/:3590/:3597）全经锁；8×500 并发恰 4000、抽检恰 16 次测试在案 |

## 红线发现

无 Critical。逐项：
- 裸 `except:`：0。
- `except Exception: pass` 吞错：0。`except queue.Full: pass`（topos_encoder.py:1611-1613）为哨兵重投循环的控制流（队列满 → 下一轮重试），具体异常类型，非错误掩盖；`except BaseException:`（html_renderer_service.py:90-95）清理后 re-raise。
- UI 线程 GL：本批无 GL 调用改动；Qt 捕获后端（AUD-30217）恰是反向强化——非主线程 fail-closed 且检查先于任何 Qt 导入。
- HDR 钳制：无。`max(0, …)`（encoder 帧数、测试名中的 clamp 为 worker 容量）均非像素值域操作。
- 601/709：无涉及。

## 回退验证建议（抽 2 条）

1. **AUD-30217**（src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:168-172）
   - 当前原文（逐字）：
     ```python
        if threading.current_thread() is not threading.main_thread():
            return self._failure(
                CaptureFailureCode.UNSUPPORTED,
                "qt webengine capture must run on the main thread (AUD-30217)",
                None)
     ```
   - 回退为：删除上述 5 行（恢复无主线程守卫）
   - 应失败节点：`tests/teaching_animation/test_ef18_aud.py::TestAud30217MainThreadConstraint::test_capture_off_main_thread_fails_closed`（工作线程 capture 将继续走 Qt 导入路径，不再返回 UNSUPPORTED+“main thread” → 断言失败）
2. **AUD-32205**（src/shared/codec/topos_binding.py:4408-4414）
   - 当前原文（逐字）：
     ```python
        if self._prev_slice_threads is not None:
            self._codec.set_slice_threads(self._prev_slice_threads)
            self._prev_slice_threads = None
     ```
   - 回退为：
     ```python
        pass
     ```
   - 应失败节点：`tests/teaching_animation/test_ef18_aud.py::TestAud32205ParallelSequentialDecoder::test_exit_restores_slice_threads_when_instance_set_it`（`set_calls` 停留在 `[1]` ≠ `[1, 8]` → 失败）

## 过程备注（不扣分）

- 执行报告自称"未做任何 git 操作"，提交 `cbda06cd1` 系中心统一收编；提交内容与报告清单一致（含 asset_resolver.py 不在提交中这一 INVALID 自洽性）。
- codec/export 测试按"方案 A"集中于 tests/teaching_animation/test_ef18_aud.py（未建 tests/media/test_ef18_aud.py），报告已披露，路径仍在 tests/ 白名单内，接受。
- 邻接测试 3 个 VT 硬编桥失败为环境预存（本机无 VT 硬编桥），报告已做短路对照（失败集与本批改动无关）并按已知预存失败忽略，处置正确。

## 最终结论

**PASS**。20 条 FIXED 独立核实为真实根因修复；1 条 INVALID（AUD-29804）经 git 历史独立验证成立且补了回归守护测试。重点项（worker 工厂失败回收、Qt 捕获主线程 fail-closed 且检查先于 Qt 导入、原子写目录 fsync + 失败留痕不回滚）全部确认。Critical 0 ｜ ISSUE 0。允许 --mark（含 AUD-29804 的 `--note "invalid:已被 AUD-29803/w17 修复,补守卫测试"` 标注）。
