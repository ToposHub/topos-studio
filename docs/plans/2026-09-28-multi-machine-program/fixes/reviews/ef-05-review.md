# EF-05 复查报告（GLM5.3）

- 批号：**EF-05**（src/shared/video，24 条 Suggestion 级）
- 提交哈希：主体 `c866a5a367f20f3bce5df7ea06e617519f2c2f2e`；closeout `53503171ae86c4a4b2c04c9bae9bf98b3216ce56`（一并纳入复核）
- 白名单：`src/shared/video/`
- 执行报告：`fixes/reports/ef-05.md`（含 2026-10-02 补充披露节）
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show / 源码逐条核对 / py_compile / moderngl 运行时属性检查），未运行 pytest（测试由中心会话统一执行）

---

## §6 复查清单逐项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **PASS（主体）/ PASS-with-note（closeout）** | 主体 `c866a5a36` 共 19 文件：16 源文件全部在 `src/shared/video/`（含 denoise/ 子目录）+ `tests/video/test_ef05_aud.py`（新建）+ `tests/video/test_frame_trace_schema.py`（1 个既有用例按 AUD-39304 新契约更新，已披露）+ 报告。⊆ 白名单 ∪ tests/ ∪ fixes/reports/。**closeout `53503171a` 改了白名单外文件 `src/shared/media/ffmpeg_source.py`（+7/-2）**——但已充分披露（见下"closeout 专项"），按复查规则（越界且未披露→Critical）不构成 Critical |
| 2 | 真实修复（逐条） | **PASS** | 21 FIXED 全部独立核实 CONFIRMED；1 INVALID（AUD-39402）与 1 BLOCKED（AUD-39512）的证据链独立复核成立；1 个既有断言更新（AUD-39304）披露属实（见逐 ID 表） |
| 3 | GLSL/HDR 红线 | **PASS** | 无 GLSL 改动；`_get_texture_format` 收敛为单 `return 'f2'`（AUD-39513）是删除死分支、无行为变化，且 docstring 完整披露 f2 精度策略——**不是**新增 HDR 钳制（内部管线 float32 未动）；601/709 矩阵零触碰（diff 中 `bt709` 唯一出现在测试 fixture `tests/video/test_ef05_aud.py:845`，生产默认值未改） |
| 4 | 并发 | **PASS** | UI 线程零新增 GL 调用（AUD-39811 守卫反而**阻止**非渲染线程触碰 GL）；锁内无重活：decode_worker 的 `request_prefetch`/日志在锁外（:904-1023 区域）；39514 的 queue.mutex 锁内仅无副作用 peek；39610 全部 lock/unlock 改 try/finally（9 处逐一目检：request_seek 三段/stop/get_queue_depth/_check_seek_request/_get_generation/_has_frame/_add_frame/_remove_frame/_maybe_drop_old_frames + 既有 get_ready_frame） |
| 5 | 资源 | **PASS** | GPU 对象：39715 纹理即时登记（异常路径可被 `_release_textures()` 释放）；39810 三处局部 IBO 成员化并随 `release()` 释放（VAO 先于 IBO）；39204 的 O(n) 重建保留 `_defer_release_gpu_frame` 渲染线程延迟释放契约；39402 无资源触碰 |
| 6 | 异常 | **PASS-with-note** | 零裸 `except:`；**但新增 3 处 `except Exception: pass`**（yuv_gpu_upload.py release() 的 IBO 释放路径，见红线发现）——与同方法 13 处既有同款块风格一致，但报告"无吞错"的自查声明与事实不符，记 S 级问题 |
| 7 | 测试质量 | **PASS** | 见"测试质量评估" |
| 8 | 报告/勾选纪律 | **PASS** | 报告逐条状态+验证输出完整；REMEDIATION_PLAN.md 未触碰；无 git 写操作痕迹 |

### closeout 专项（53503171a 越界披露核实）

- 事实链：`_gop_index_thread` 成员本身源自 P2-05 功能提交 `1f710878d`；EF-02 批（`ffc8d4f38`，AUD-34709）在 `FFmpegMediaSource.close()` 新增了对该属性的**直接读取**，引入部分构造边界回归——`__init__` 的 output_format 校验在 :508-509 提前 raise 时，成员初始化（:1576 之前）尚未执行，子类（WindowsHwPlanarSource）部分构造对象经 `__del__`→`close()` 抛 AttributeError（`tests/shared/test_fix_wd17g94_aud.py::TestAud36201` 2 例失败，efr-small-s5 验证时暴露）。
- 修复：`close()` 改 `getattr(self, "_gop_index_thread", None)` 防御读取（ffmpeg_source.py:1154-1164），正常路径行为不变，注释完整。改动最小且正确（复查者独立核实 __init__ raise 位置 :508-509 早于成员初始化）。
- 披露充分性：ef-05.md"补充披露（2026-10-02）"节内容与 diff 逐点吻合（回归来源、触发条件、失败测试、修复方式）。**判定：披露充分，不构成 Critical。**
- 会计学备注（供中心注意，非扣分项）：该回归**源自 EF-02 批的 AUD-34709 修复**，却在 ef-05 的 closeout 提交修复、披露挂在 ef-05.md。建议中心在 EF-02 的复查/mark 记录中交叉引用此回归-修复对，避免审计链断点。

---

## 逐 ID 表（24/24，全部独立核实于当前 HEAD）

| ID | 计划指控（摘要） | 报告结论 | 复查判定 | 当前 HEAD 证据（file:line） |
|----|----------------|---------|---------|---------------------------|
| AUD-38803 | DecodeWorkerStats 跨线程读写无锁 | FIXED | **CONFIRMED** | decode_worker.py:136（`_stats_lock`）、:146-181（六个 update_* 持锁）、:180-183（`to_dict`→`_to_dict_locked` 锁内快照） |
| AUD-38804 | 三跨线程 int 字段无同步 | FIXED | **CONFIRMED** | decode_worker.py:294（`_decode_position_lock`）、:324-329（属性读/setter 写）、:962/:1245（两解码路径走 setter）、:896（`_clock_state_lock`）、:992/:1010（update_clock_position/set_prefetch_lookahead 持锁） |
| AUD-38902 | TemporalFrameWindowCache 无消费者零验证 | FIXED（状态注记分支） | **CONFIRMED** | denoise/frame_types.py:76-90（docstring 注记"计划内 M2.2 预留、无生产消费者"+不可变契约；行为零改动——符合审计建议的 docstring 分支） |
| AUD-38907 | docstring 称 1920 实际 2560 | FIXED | **CONFIRMED** | denoise/window_provider.py:5-8（改为 2560 并注明 AUD-38907）与 :31（`_MAX_NEIGHBOR_WIDTH = 2560`）一致 |
| AUD-39006 | 模块示例 log_fallback(event) 照抄即炸 | FIXED | **CONFIRMED** | fallback_types.py:17-27（示例改为字段参数调用 `log_fallback(reason=..., exception=RuntimeError(...))`，与真实签名一致） |
| AUD-39108 | 全局统计计数无锁递增丢更新 | FIXED | **CONFIRMED** | frame_provider_service.py:695（`_stats_lock`）、:722/:730（served/hit_rate 持锁读）、:881/:1011（open_clip 两处）、:1415（closed）、:1519/:1548/:1575/:1578（request_frame 快路径五处）、:2412-2413（get_statistics 同锁快照） |
| AUD-39111 | prefetch 同步执行+范围缓存无界增长 | FIXED | **CONFIRMED** | frame_provider_service.py:1810+（docstring 明示"调用线程同步执行"）、:1568（request_frame 解码临界区内 `session.evict_old_frames(frame_index, ...)` 滚动淘汰）、:1883（prefetch 循环内同款）——缓存增量受 max_*cache 上界约束 |
| AUD-39204 | 过期帧清除 list.remove O(n²) | FIXED | **CONFIRMED** | frame_queue.py:399-423（`_drop_stale_frames` 单遍重建 kept/stale 两列表）、:756+（`dequeue_latest_before_or_at` 同型）；GPU 帧仍走 `_defer_release_gpu_frame` 延迟释放 |
| AUD-39214 | `_dropped_frames` 非原子自增 | FIXED | **CONFIRMED** | monitor_output.py:689-700（`_drop_counter_lock`+属性持锁读）、:770-779（Ffmpeg 满队/put 失败）、:988-989（Native 同款）、:1120-1122/:1158-1160（Native 排空/queue.Full） |
| AUD-39304 | 负数字节静默忽略与 docstring 不符 | FIXED | **CONFIRMED** | pixel_copy_counters.py:58-83（`nbytes < 0` 抛 ValueError fail-closed；`nbytes == 0` 保持静默跳过；未知 kind 仍抛；docstring 同步）。既有断言更新披露属实：tests/video/test_frame_trace_schema.py 旧用例 `test_zero_and_negative_bytes_ignored` → `test_zero_bytes_skipped_negative_rejected`（锁定的正是审计要求移除的静默忽略行为，更新正当且已披露） |
| AUD-39311 | fps=0 除零崩溃 | FIXED | **CONFIRMED** | playback_clock.py:1027-1028（`_safe_fps` 属性：fps<=0 回退 25.0，与构造器 0.04s 口径一致）、:1093/:1142/:1268/:1298（四处除法改用） |
| AUD-39317 | export_json 两次独立加锁快照不一致 | FIXED | **CONFIRMED** | playback_frame_trace.py:571-583（单次持 RLock 期间调用 get_traces+get_summary，写入方同需该锁无法插入） |
| AUD-39402 | `_session_lock` 在阻塞操作全程被持有 | **INVALID**（已被 AUD-39400 修复） | **CONFIRMED（INVALID 判定成立）** | 独立核实：playback_state.py:386-396（`_detach_session`/`_peek_session`——持锁仅取引用）；:422/:478（load/unload）、:645+651 区域（blocking seek：`_peek_session()` 后锁外调 `session.seek_to_frame`）、resume/next 同模式；AUD-39400 注记遍布命令方法。守卫测试真实有效：test_ef05_aud.py:549-589（stub session 的 seek/unload 期间对 `_session_lock.acquire(blocking=False)` 必须成功——回退持锁实现即失败） |
| AUD-39403 | tick 回调死代码 + 回调字段写入不持锁 | FIXED | **CONFIRMED** | playback_state.py:253-256（死代码三元组删除，注释说明）、:256（`_callback_lock`）、:943/:954/:986/:1025（set_on_frame_ready/set_on_state_change/_on_session_state_change 快照后锁外调用/cleanup 锁内清空）；全库 grep 无 set_tick_callback 残留调用方（复查者验证：仅测试断言其不存在） |
| AUD-39408 | `_notify_state_change` docstring 与调用点矛盾 | FIXED | **CONFIRMED** | session.py:1382-1394（docstring 改为"MUST be called OUTSIDE _state_lock（V-P0-11）"并列明全部调用点） |
| AUD-39412 | load() 文档与配置默认值矛盾 | FIXED | **CONFIRMED** | session.py:399-410（Note 重写为"默认【不】使用共享帧服务，V-P1-4"）、:427-428（"共享帧服务模式（默认【关闭】，经配置开启）"）——与 :241-247 的 `value if value is not None else False` 对齐 |
| AUD-39512 | 每帧 ctx.finish() 可用 fence 替代 | **BLOCKED** | **CONFIRMED（BLOCKED 理由成立）** | 复查者独立验证证据链：① `.venv` 运行时检查 moderngl 5.12.0 的 Context **无任何 fence/sync 属性**（`dir()` 扫描为空）；② `frame_ready` 信号消费方在 `src/features/color/*`、`src/features/composite/*`（白名单外）；③ `ctx.finish()`（video_async_renderer.py:1160）未被本批改动。跨上下文纹理交接下贸然弱化确有撕裂风险，BLOCKED 判定正当 |
| AUD-39513 | `_get_texture_format` 死分支+精度未言明 | FIXED | **CONFIRMED** | video_async_renderer.py:619-643（五恒等分支收敛为单 `return 'f2'`；docstring 完整披露精度策略：内部管线 float32、仅落纹理降 f2、条带代价、未来显式 'f4' 开关） |
| AUD-39514 | 命令合并裸 peek 队列内部 deque | FIXED | **CONFIRMED** | video_async_renderer.py:262-281（经 `command_queue.mutex` 持锁无副作用 peek，确认后公共 API `get_nowait()`+`task_done()` 配对；本线程唯一消费者的论证写在注释） |
| AUD-39515 | 调用线程无锁读 `current_frame_index` | FIXED | **CONFIRMED** | video_async_renderer.py:126-127（`_submit_frame_index_lock`+`_submitted_frame_index`）、:403-412（自动帧号持锁自增；显式 frame_index>=0 原样使用并单调推进计数——混合调用语义保持） |
| AUD-39610 | ProxyPrefetchWorker 裸 lock/unlock 无 try/finally | FIXED | **CONFIRMED** | video_playback.py:555-700 区域 9 个方法全部 try/finally（复查者逐方法目检，见 §6 第 4 项）；`_has_frame` 消除循环内手写提前 unlock。嵌套重入（BUG-C2-02）系既有独立缺陷，测试 workaround 在案（tests/video/test_video_playback_coverage.py:305-312），本批未触碰——披露属实 |
| AUD-39715 | upload_planes 中途异常纹理泄漏 | FIXED | **CONFIRMED** | video_upload.py:403-421（每创建一张立即 `self._textures['y'/'u'/'v']` 登记，注释明示 AUD-39715；`_release_textures()` :622+ 可正常回收） |
| AUD-39810 | 三处局部 IBO 未跟踪 GL 泄漏 | FIXED | **CONFIRMED** | yuv_gpu_upload.py:730-732（三成员声明）、:2325/:2484/:2647（三处创建点赋值成员）、:3257-3317（release() 依次释放三个 IBO，VAO 先于 IBO）——与既有 `_nv12_quad_ibo`（AUD-39806）模式一致 |
| AUD-39811 | enable_pbo 无线程契约守卫 | FIXED | **CONFIRMED** | yuv_gpu_upload.py:774-802（docstring 线程契约 + 守卫：渲染线程已注册且当前线程非渲染线程→拒绝执行仅告警不触碰 GL）；依赖 frame_queue.py:103-111 新增 `gpu_frame_release_thread_id()` getter（白名单内，无循环导入）；未注册环境行为不变 |

统计：**21 FIXED 全 CONFIRMED；1 INVALID（AUD-39402，判定成立）；1 BLOCKED（AUD-39512，理由成立）；0 ISSUE。**

---

## 红线发现

**零 Critical。1 项 S 级发现：**

1. **[S] yuv_gpu_upload.py release() 新增 3 处 `except Exception: pass`**（`_r16ui_quad_ibo`/`_nv12_r16ui_ibo`/`_quad_ibo` 释放块，:3251/:3283/:3316 一带）。
   - 事实：与同方法内 13 处**既有**同款块（program/VAO/纹理释放）风格完全一致，teardown 尽力而为路径，无功能性风险。
   - 但按计划 §1"吞错必留痕"的字面要求构成违规；且执行报告约束自查声称"无吞错"——**该声明与事实不符**（报告准确性瑕疵）。
   - 建议：中心会话安排一行级后续（各加 `logger.debug("IBO release failed", exc_info=True)` 或接受为文件既有惯例并在 FIXLOG 注记）；不否定本批修复本身。
2. 裸 `except:`：零命中（扫描唯一命中为报告 markdown 自述）。
3. HDR 钳制：零（`_get_texture_format` 为死分支删除，非值域钳制；float 透传未动）。
4. 601/709：生产代码零触碰；`bt709` 唯一出现在测试 fixture（test_ef05_aud.py:845）。
5. UI 线程 GL：零新增（AUD-39811 守卫为反向防护）。

---

## 测试质量评估

- 规模：`tests/video/test_ef05_aud.py` 56 用例 / 1011 行，23 个测试类与 ID 一一对应（AUD-39512 BLOCKED 无测试，合理）；不真实解码视频（全 mock/合成数据），QMutex 相关用例带 qapp fixture。
- 行为断言为主、结构断言为辅（文件头自述"结构断言+行为断言双重锚定"）：
  - 行为断言样本（质量高）：AUD-39402 的 `acquire(blocking=False)` 探针守卫；AUD-39610 的 `tryLock()` 失败检测（`test_add_frame_releases_mutex_when_min_raises`：畸形元组触发 min() TypeError 后锁必须可用）；AUD-39304 的负数 ValueError 断言。
  - 结构断言（`inspect.getsource` 共 7 处）用于 doc-gap 类 ID（38902/38907/39006/39408/39412/39513/39810 的成员存在性）——对 doc/结构类修复这是合理语义；对代码类 ID（39514/39515）结构断言之外均伴随行为断言，非唯一锚点。可接受。
- 既有断言更新（AUD-39304 契约）披露完整、更新正当。
- 执行报告自述的回退敏感性抽查（39610、39515）与断言设计一致可信。
- 可疑弱断言：**未发现**。

py_compile：主体 16 个源文件 + closeout 的 ffmpeg_source.py 在当前 HEAD 全部编译通过（复查者独立执行）。

---

## 回退验证建议（供中心会话执行；回退后对应测试必须 FAILED）

### 候选 1：AUD-39304（pixel_copy_counters.py）

文件：`src/shared/video/pixel_copy_counters.py`（当前 :70-75）
当前（old）：
```python
    nbytes = int(nbytes)
    if nbytes < 0:
        raise ValueError(
            f"negative copy bytes rejected (kind={kind!r}, nbytes={nbytes})"
        )
    if nbytes == 0:
        return
```
回退为（new，恢复静默忽略）：
```python
    nbytes = int(nbytes)
    if nbytes <= 0:
        return
```
应失败的 pytest 节点：
- `tests/video/test_ef05_aud.py::TestAud39304NegativeBytesRejected::test_negative_bytes_raise`
- `tests/video/test_ef05_aud.py::TestAud39304NegativeBytesRejected::test_negative_bytes_do_not_corrupt_counters`
- 既有契约测试：`tests/video/test_frame_trace_schema.py`（更新后的 `test_zero_bytes_skipped_negative_rejected`）

### 候选 2：AUD-39610（video_playback.py `_add_frame`）

文件：`src/shared/video/video_playback.py`（当前 :645-665）
当前（old，节选锁结构）：
```python
        self._ready_frames_mutex.lock()
        try:
            # 如果队列满，丢弃最旧的帧
            if len(self._ready_frames) >= self._max_queue_size:
                min_idx = min(self._ready_frames, key=lambda x: x[0])[0]
                self._remove_frame(min_idx)
                logger.debug(f"Dropped frame {min_idx} from queue")

            self._ready_frames.append((frame_index, frame_array, generation))
        finally:
            self._ready_frames_mutex.unlock()
```
回退为（new，裸 lock/unlock）：
```python
        self._ready_frames_mutex.lock()
        # 如果队列满，丢弃最旧的帧
        if len(self._ready_frames) >= self._max_queue_size:
            min_idx = min(self._ready_frames, key=lambda x: x[0])[0]
            self._remove_frame(min_idx)
            logger.debug(f"Dropped frame {min_idx} from queue")

        self._ready_frames.append((frame_index, frame_array, generation))
        self._ready_frames_mutex.unlock()
```
应失败的 pytest 节点：
- `tests/video/test_ef05_aud.py::TestAud39610MutexReleasedOnException::test_add_frame_releases_mutex_when_min_raises`（畸形元组触发 min() TypeError 后 `tryLock()` 必须成功——回退后锁仍被持有，断言失败）

### 候选 3（closeout）：AUD-34709 回归修复（ffmpeg_source.py）

文件：`src/shared/media/ffmpeg_source.py`（当前 :1154-1164）
当前（old）：
```python
        gop_thread = getattr(self, "_gop_index_thread", None)
```
回退为（new，直接读取）：
```python
        gop_thread = self._gop_index_thread
```
应失败的 pytest 节点：
- `tests/shared/test_fix_wd17g94_aud.py::TestAud36201::test_open_failure_closes_container_and_resets_state`
- `tests/shared/test_fix_wd17g94_aud.py::TestAud36201::test_hwaccel_reopen_failure_also_cleans_up`
（`__init__` output_format 校验提前 raise → 部分构造对象 `__del__`→close() AttributeError。）

---

## 最终结论

**PASS-with-notes。**

- 21 FIXED 全部真实修复（独立核实）；INVALID（AUD-39402）与 BLOCKED（AUD-39512）判定证据链均独立复核成立；既有断言更新披露属实；测试断言有效；主体提交零越界。
- Notes（不阻塞 mark，但需中心知悉/跟进）：
  1. **[S] 3 处新增 `except Exception: pass`**（yuv_gpu_upload.py release() IBO 释放块）——与文件既有惯例一致但违反"吞错必留痕"字面要求，且报告自查声明失实；建议一行级留痕后续或注记接受。
  2. **closeout 越界已披露**（ef-05.md 补充披露节，内容与 diff 吻合），不构成 Critical；但该回归源自 **EF-02 的 AUD-34709 修复**，披露与提交却挂在 ef-05 名下——建议中心在 EF-02 复查记录交叉引用，mark 会计对齐。
  3. AUD-39512 确认为合理 BLOCKED（moderngl 5.12 无 fence API——复查者运行时验证；消费方在白名单外），留待后续原生 GL 扩展方案。
- 建议：本批可 `check_fixes.py --mark` 勾选（commit=c866a5a36；AUD-39402 以 INVALID+note 勾选、AUD-39512 以 BLOCKED+note 勾选）。
