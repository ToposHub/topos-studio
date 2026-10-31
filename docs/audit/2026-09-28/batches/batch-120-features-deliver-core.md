# batch-120 [P1] — src/features/deliver/core（视频导出预设 + 作业运行器）

编号区间：AUD-12000–AUD-12099。审查日期：2026-09-28。
文件：video_export_presets.py (368 行)、video_job_runner.py (363 行)。
交叉核对依赖（只读）：export_queue.py、video_export_job.py、src/shared/export/video_export_worker.py、video_batch_export_controller.py、video_export_preflight.py、video_export_settings_panel.py、src/app/main_window.py。

## src/features/deliver/core/video_export_presets.py (行数: 368)

### 发现

- [AUD-12000] [Major] 文件名模板仅消毒替换值，模板字面量中的路径分隔符原样放行，破坏"basename only"契约
  - 位置: src/features/deliver/core/video_export_presets.py:279-287（辅证 :249-252、:331-333）
  - 证据:
    ```python
    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in values:
            # Sanitise: no path separators / illegal chars in filenames.
            v = values[key]
            return re.sub(r'[\\/:*?"<>|]', "_", v)
        return match.group(0)  # leave unknown placeholders intact

    return re.sub(r"\{(\w+)\}", _replace, template)
    ```
    docstring 声明 "The result is a *basename only*"（:250-251），随后 `output_path = os.path.join(output_dir, filename) if output_dir else filename`（:333）。
  - 影响: 消毒只作用于 `{var}` 的替换值；模板本身的字面文本未过滤。UI 侧模板来自用户可编辑文本框（video_export_settings_panel.py:1602 `template = self._filename_edit.text().strip() or ...`），输入 `../../x` 或 `/abs/path/{project}` 时 `os.path.join` 会逃出所选输出目录（绝对路径时直接覆盖 output_dir）。preflight 只检查最终父目录存在/可写（video_export_preflight.py:498-515），不会拦截；配合 OverwritePolicy.OVERWRITE 可在目录预期之外静默覆盖同名文件。违反 .clinerules"外部数据须验证"。
  - 建议: 对整个展开结果（而非仅替换值）做 `r'[\\/:*?"<>|]'` 消毒，或校验展开后 basename 不含分隔符且 `os.path.basename(base) == base`，再交给 join。
  - 置信度: 高（机制确定；实际触发需用户在模板中键入分隔符或队列文件被外部构造）

- [AUD-12001] [Suggestion] VideoPreset 文档声明 `container_override` 字段但 dataclass 未定义、构建逻辑也未消费
  - 位置: src/features/deliver/core/video_export_presets.py:59-60（对照 :64-72、:316-331）
  - 证据:
    ```python
    container_override: when set, forces the container regardless of
        ``video_config.container`` (useful when a codec allows several).
    ```
    而字段列表为：
    ```python
    preset_id: str
    title_key: str
    ...
    order: int = 100
    ```
  - 影响: 文档与实现不一致；使用者按文档设置该属性会遇到 frozen dataclass 的赋值错误或静默忽略，多容器 codec 无法按文档覆盖容器。
  - 建议: 要么补上字段并在 `build_job_from_preset` 中生效（覆盖 `video_cfg.container` 后再取扩展名），要么删除文档条目。
  - 置信度: 高

- [AUD-12002] [Suggestion] 模板展开结果可为空串，生成 ".mp4" 式隐藏文件名且能通过 validate()
  - 位置: src/features/deliver/core/video_export_presets.py:259-287、:331-333
  - 证据:
    ```python
    res_str = ""
    ...
    fps_str = f"{fps:g}" if fps else ""
    ...
    "fps": fps_str,
    ...
    return re.sub(r"\{(\w+)\}", _replace, template)
    ```
    用户模板 `"{fps}"` + master 预设（fps=None）→ base 为空 → `filename = "" + ".mp4"`。
  - 影响: 输出为隐藏点文件 `.mp4`；`VideoExportJob.validate()` 只检查非空与扩展名白名单（video_export_job.py:515-572），`.mp4` 合法，错误文件名一路放行到导出。
  - 建议: 展开后检查 `base.strip()` 非空（为空时回落默认模板），或对全由空值变量组成的模板报校验错误。
  - 置信度: 高（机制确定，触发概率低）

- [AUD-12003] [Suggestion] `_container_extension` 对未知容器静默回落 ".mov"，可能产生扩展名与容器不符的输出
  - 位置: src/features/deliver/core/video_export_presets.py:355-368
  - 证据:
    ```python
    container = container.lower()
    return {
        "mp4": ".mp4",
        "mov": ".mov",
        ...
    }.get(container, ".mov")
    ```
  - 影响: 容器字段拼写错误（如 `"mp4 "`）不报错，文件以 `.mov` 命名而配置仍是错误容器；校验层只看扩展名白名单，无法发现不一致，用户得到命名错误的交付文件。
  - 建议: 未知容器时抛 `ValueError`（或记 warning 并回落），并把映射表与 validate() 的扩展名白名单统一维护。
  - 置信度: 高

### 通过项
- 线程安全：纯数据模块、无可变共享状态；DEFAULT_VIDEO_PRESETS 为元组常量，`build_job_from_preset` 对三个 config 全量 deepcopy（:316-318），面板只读模板（video_export_settings_panel.py:2613-2632 仅读取 preset.video_config 做差异比较）
- GPU 资源生命周期：不涉及 OpenGL/ModernGL
- GLSL·HDR 正确性：不涉及着色器；预设位深/像素格式组合（ProRes422 yuv422p10le、4444 yuv444p10le+alpha、HEVC main/yuv420p）与 video_export_job.py 的 `_hevc_pixel_format_override` 语义一致
- 信号防递归：无 Qt 信号
- 状态提交一致性：预设不承载项目状态；job 快照经 to_legacy_job/队列持久化，本模块仅负责构造
- 异常与取消路径：无 I/O 与外部调用，不存在隐藏异常路径；无宽泛 except
- 类型注解：公开 API 注解完整（Tuple/Optional/返回类型齐全）
- 性能：`get_preset_by_id` 线性扫描 6 项常量，无影响；无逐帧/热循环代码

## src/features/deliver/core/video_job_runner.py (行数: 363)

### 发现

- [AUD-12010] [Major] 每个作业的 VideoExporter 以 runner 为父对象创建但从不删除，长队列会话中子对象/线程句柄无界累积
  - 位置: src/features/deliver/core/video_job_runner.py:353（辅证 :194、:200、:263-272；video_export_worker.py:244-245、:332-338）
  - 证据:
    ```python
    exporter = VideoExporter(parent=self)
    thread = getattr(exporter, "_worker_thread", None)
    return exporter, thread
    ```
    作业结束时：
    ```python
    finally:
        try:
            exporter.cleanup()
        except Exception:  # noqa: BLE001
            logger.exception("Worker cleanup raised; continuing")
        self._current_exporter = None
        self._current_job_id = None
        self._loop = None
    ```
    `VideoExporter.cleanup()`（video_export_worker.py:332-338）仅 `quit()+wait(3000)`，不 deleteLater、不解除父子关系。
  - 影响: 每个 `run_job` 都新建一个 parent=self 的 VideoExporter，QObject 父子关系使其（连同 VideoExportWorker、QThread 包装、`_on_finished/_on_error/_on_progress` 闭包及其捕获的 legacy_job/video_job 引用）在作业结束后仍被 runner 持有，直到 runner 销毁。批队列是长生命周期组件（控制器常驻），连续导出 N 个作业即累积 N 组对象；同时 `wait(3000)` 超时返回值被忽略（worker 侧），线程可能仍在跑。属资源泄漏，违反"资源不得泄漏"铁律。
  - 建议: teardown 时 `exporter.deleteLater()`（或创建时不设 parent、cleanup 后显式 deleteLater），并至少记录 `wait()` 超时；确认回调引用随对象一并释放。
  - 置信度: 高

- [AUD-12011] [Major] 嵌套 QEventLoop 退出而无结果时无任何护栏：退出即把作业误判为失败并继续调度下一作业，应用退出时会连锁空跑整个队列
  - 位置: src/features/deliver/core/video_job_runner.py:251-282（辅证 :212；export_queue.py:418-425）
  - 证据:
    ```python
    try:
        exporter.export(
            project=context.project,
            ...
        )
        self._loop.exec()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"The video export worker crashed: {e}") from e
    finally:
        ...
    if self._cancelled:
        return False
    if not self._result_success and self._result_error:
        raise RuntimeError(self._result_error)
    return self._result_success
    ```
    未收到任何回调时 `_result_success=False`、`_result_error=""` → 裸 `return False`。
  - 影响: 嵌套循环会处理所有主线程事件。应用退出/`QApplication.exit()` 会中断所有事件循环（含嵌套），`loop.exec()` 立即返回而 worker 未发任何信号：当前作业被标 FAILED（队列写入泛化错误"export runner returned failure"），随后 `_process_next()` 递归继续下一个 PENDING 作业——关机路径上每个剩余作业都会再创建一个 VideoExporter+QThread（真实 OS 线程）再立刻 teardown。已核对 src/app/main_window.py closeEvent 无 deliver 队列停止/取消接线，该场景无外部缓解；解释器退出阶段线程仍在运行有 "QThread: Destroyed while thread is still running" 崩溃风险。
  - 建议: 循环退出后若 `_result_success/_cancelled/_result_error` 全空则视为"未完成"（直接返回 False 并阻止队列继续，或检测 `QApplication.quit`/aboutToQuit 停止调度）；导出期间不要让嵌套循环响应 Quit 事件（必要时 `QEventLoop.ExcludeUserInputEvents` 之外补退出标志检查）。
  - 置信度: 中（机制由 Qt 嵌套循环语义推得；关机时的具体连锁表现未实测）

- [AUD-12012] [Major] request_cancel 在 exporter 尚未就绪的窗口内被静默丢弃——runner 没有与 pause 对称的 pending-cancel 标志，而文档宣称任意线程可调
  - 位置: src/features/deliver/core/video_job_runner.py:286-300（辅证 :166-177、:200-206）
  - 证据:
    ```python
    job_id = self._current_job_id
    exporter = self._current_exporter
    if exporter is not None:
        try:
            exporter.cancel()
        except Exception as e:  # noqa: BLE001
            logger.warning("cancel() failed on current exporter: %e", e)
    if job_id is not None:
        self.cancel_requested.emit(job_id)
    ```
    `run_job` 开头（:166-171）先设 `_current_job_id`，`_current_exporter` 直到 :200 才赋值；期间 context 解析、config 构建、worker 构造均为同步步骤。pause 有 pending 机制（:201-205），cancel 没有。
  - 影响: docstring 声称 "Safe to call from any thread"。主线程在该窗口内不可重入，但按文档从其他线程调用（或在 `job_started` 直接连接的槽中触发链路）时：取消被丢弃，导出继续跑完整段 4K 编码；而队列侧 `_cancelled_current=True`（控制器 :196-198 先调 request_cancel 再 cancel_current）使作业最终标 CANCELLED——状态与"文件已完整写出"不符。跨线程读取 `_current_exporter/_current_job_id` 也无同步原语。
  - 建议: 仿照 pause 增加 `self._cancel_requested` 标志，在 exporter 就绪后（:200 后）补发 `exporter.cancel()`；或在 `export()` 前后各检查一次取消；同时把"任意线程可调"的承诺收紧为主线程/加锁。
  - 置信度: 中（窗口小、需非主线程或重入触发；但 API 明确邀请任意线程使用，缺陷结构确定）

- [AUD-12013] [Suggestion] 日志占位符 `%e` 配异常参数，格式化必然 TypeError，告警信息丢失
  - 位置: src/features/deliver/core/video_job_runner.py:298
  - 证据:
    ```python
    exporter.cancel()
    except Exception as e:  # noqa: BLE001
        logger.warning("cancel() failed on current exporter: %e", e)
    ```
  - 影响: `%e` 只接受数值，格式化异常对象抛 TypeError，logging 的 handleError 只向 stderr 打印内部回溯，真正的"取消失败"内容丢失，排障线索缺失。
  - 建议: 改为 `%s`（同文件 :312、:327 已正确使用）。
  - 置信度: 高

- [AUD-12014] [Suggestion] 暂停计时不自洽：跨作业的 pending pause 时间戳被 run_job 清空，export_fps 把暂停时长计入耗时导致速率被低估
  - 位置: src/features/deliver/core/video_job_runner.py:213-215、:302-306、:314-321、:232-237（`_progress_started_at` 在 :139 初始化后从未在作业结束时置回 None）
  - 证据:
    ```python
    def request_pause(self) -> None:
        self._pause_requested = True
        if self._progress_started_at is not None and self._progress_pause_started_at is None:
            self._progress_pause_started_at = time.perf_counter()
    ```
    而新作业开始时：
    ```python
    self._progress_started_at = time.perf_counter()
    self._progress_pause_started_at = None
    self._progress_paused_total = 0.0
    ```
  - 影响: 队列空闲期间 `request_pause` 因上一作业残留的 `_progress_started_at` 非空而记录暂停起点；下一个 `run_job` 把它清成 None 且不并入 `_progress_paused_total`，该作业带着 pending pause 启动，恢复时 `if self._progress_pause_started_at is not None` 不成立，暂停墙钟时间全部计入 elapsed → `legacy_job.export_fps` 系统性偏低（该值写入队列持久化并展示在卡片 UI）。
  - 建议: 在 run_job 重置前先把未结算的 `_progress_pause_started_at` 并入；或空闲（无 in-flight 作业）时禁止记录暂停起点；作业结束时将 `_progress_started_at` 置回 None。
  - 置信度: 高

- [AUD-12015] [Suggestion] 早期失败路径（context 解析/config 映射/worker 创建失败）直接 raise，跳过状态清理，`_current_job_id` 残留过期值
  - 位置: src/features/deliver/core/video_job_runner.py:167、:177、:183-189、:198（`finally` 仅覆盖 :251 起的 try）
  - 证据:
    ```python
    self._current_job_id = video_job.job_id
    ...
    context = self._safe_resolve_context(video_job)
    if not context.is_resolvable:
        raise RuntimeError(context.reason or "Cannot resolve export context")
    ```
  - 影响: 这些 raise 路径不清 `_current_job_id`（清理只在 :271 的 finally）；作业间隙调用 `request_cancel` 会向已失败作业的 id 发 `cancel_requested` 信号，下游监听者拿到过期 job_id 产生误动作/噪音。
  - 建议: 把状态初始化包进 try/finally，或在 raise 前 `self._current_job_id = None`。
  - 置信度: 高

- [AUD-12016] [Suggestion] `_create_worker` 用 getattr 反射私有属性并返回从未被使用的 thread；方法缺返回类型注解
  - 位置: src/features/deliver/core/video_job_runner.py:338-355（辅证 :194）
  - 证据:
    ```python
    def _create_worker(
        self,
        project: Any,
        sequence: Any,
        export_config: Any,
    ):
        ...
        exporter = VideoExporter(parent=self)
        thread = getattr(exporter, "_worker_thread", None)
        return exporter, thread
    ```
    调用侧 `exporter, thread = self._create_worker(...)` 之后 `thread` 再未使用。
  - 影响: 死代码 + 对 worker 私有属性 `_worker_thread` 的脆弱耦合（属性改名静默变 None）；`run_job` 里的 `thread` 变量误导读者以为 runner 管理线程生命周期；缺返回注解违反项目"所有函数有类型注解"约束。
  - 建议: 删除 thread 返回值与 getattr，方法签名补 `-> Tuple[Any, Any]` 或精确类型。
  - 置信度: 高

### 通过项
- 线程安全（主体）：run_job 在主线程阻塞于嵌套事件循环；worker 信号经队列连接回到主线程槽（video_export_worker.py:252-254）再调 runner 回调，`_quit_loop` 的 QTimer.singleShot 亦在主线程执行，退出路径无跨线程定时器问题；worker 的取消/暂停标志为单写单读的 GIL 原子布尔
- GPU 资源生命周期：本模块不直接触碰 OpenGL；headless GL 在 worker 线程内创建/回落（video_export_worker.py 头注），符合"UI 线程不得操作 OpenGL"
- GLSL·HDR 正确性：不涉及着色器与像素处理
- 信号防递归：job_started/cancel_requested 为一次性通知信号，无递归回路；进度回调异常被捕获并记日志（:241-245），不会杀作业
- 异常与取消路径（主体）：失败时 raise RuntimeError 让队列记录真实错误（与 export_queue.py:409-412 吻合）；"EXPORT_CANCELLED" 哨兵与 worker 发射值严格一致（video_export_worker.py:178）；取消→False 由队列标记 CANCELLED；宽泛 except 均带 noqa 注释、保留 `from e` 链且不吞领域错误类型
- 状态提交一致性：进度换算（worker 0-100 ↔ 队列 0-1）与 TimelineExporter 0..1 语义核对一致（timeline_export.py:10856-10857 ×100，runner :243 ÷100）；export_fps/progress 写回 legacy_job 由队列统一持久化；from_legacy_job 恢复的 job_id 与队列一致
- 输入验证（主体）：context 不可解析即失败并携带 reason；`to_timeline_export_config` 的 ImportError/ValueError/KeyError 被显式翻译为可读 RuntimeError；`_safe_resolve_context` 防御 provider 抛错
- 性能模式：无热循环；速率统计用 perf_counter；帧间进度只做常数运算
