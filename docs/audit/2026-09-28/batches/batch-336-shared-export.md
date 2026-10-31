# 批次报告：batch-336 [P2] src/shared/export/

任务区间：AUD-33600–AUD-33699。审查文件 1 个：`src/shared/export/video_export_worker.py`（365 行，全文通读）。
交叉验证文件（只读）：`src/features/edit/core/timeline_export.py`（cancel/pause/resume、`_notify_progress`、`ExportOutcome`、`create_timeline_exporter`）、`src/app/controllers/deliver_controller.py`、`src/features/deliver/core/video_job_runner.py`、`tests/edit/test_export_outcome.py`、`tests/edit/ai_editing/test_export_postcheck.py`。

## src/shared/export/video_export_worker.py (行数: 365)

### 发现

- [AUD-33600] [Major] cleanup() 忽略 wait() 超时结果且不先请求取消，线程仍运行时 QThread 析构可致 qFatal 崩溃
  - 位置: src/shared/export/video_export_worker.py:332
  - 证据:
    ```python
    def cleanup(self) -> None:
        """清理 worker 线程资源（在 finished/error 之后调用）。"""
        try:
            self._worker_thread.quit()
            self._worker_thread.wait(3000)  # 最多等 3 秒
        except Exception as e:
            logger.warning(f"VideoExporter cleanup failed: {e}")
    ```
    且 line 247 `self._worker_thread = QThread()` 未设 parent（不随 VideoExporter 的 QObject 父链销毁，仅靠 Python 属性引用存活）。
  - 影响: 若 cleanup() 在 run() 仍在执行时被调用（导出暂停门 `timeline_export.py:3605-3609` 每 0.25s 轮询、或单帧渲染/编码超过 3 秒——4K 导出常态），`wait(3000)` 超时返回 False 被静默忽略，方法照常返回"成功"；线程继续渲染/写输出文件。之后 `VideoExporter` 被销毁时（`deliver_controller.py:934` 以 parent=self 挂在控制器下，控制器析构即级联）未停止的 QThread 被析构 → Qt qFatal "QThread: Destroyed while thread is still running"，进程中止。另核实 `deliver_controller.py:1889-1901` 关闭兜底只持久化批处理队列，窗口在导出中关闭时无人调 cleanup，同样落入此风险。
  - 建议: cleanup() 先调 `self._worker.request_cancel()` 再 quit/wait；检查 `wait()` 返回值，超时则 logger.error 并考虑 `requestInterruption()`/升级处理，绝不静默放行；为 `_worker_thread` 设 parent 或在 VideoExporter 析构前确保线程已停。
  - 置信度: 高（崩溃机制确定；正常路径两处调用方均在 finished/error 后清理，触发依赖关闭中退出/异常时序）

- [AUD-33601] [Suggestion] `_is_exporting` 仅由 finished/error 信号槽复位，信号丢失时永久卡 True，重复 export() 被静默吞掉且无调用方反馈
  - 位置: src/shared/export/video_export_worker.py:296
  - 证据:
    ```python
    if self._is_exporting:
        logger.warning("VideoExporter.export() called while already exporting")
        return
    ```
    复位仅在 `_on_worker_finished`（line 345）与 `_on_worker_error`（line 353）两个槽内。
  - 影响: 若 finished/error 信号未送达（worker 线程在 run() 排队执行前已被 cleanup 退出、C 层崩溃、对象析构竞态），`_is_exporting` 永久为 True，后续所有 export() 调用仅留 warning 日志即返回——调用方拿不到任何返回值/异常/信号，表现为"点导出无反应"。
  - 建议: export() 返回 bool 或抛异常告知拒绝；或加超时兜底（如 `QTimer`/时间戳校验 `_is_exporting` 陈旧性）；至少把 warning 升级为可观测的失败信号。
  - 置信度: 中（卡死需异常路径；静默吞请求为当前确定行为）

- [AUD-33602] [Suggestion] VideoExporter.export() 直接写 worker 私有属性，破坏封装且与库内既有约定相悖
  - 位置: src/shared/export/video_export_worker.py:303
  - 证据:
    ```python
    self._worker._cancel_requested = False
    self._worker._pause_requested = False
    self._worker._post_export = on_post_export
    # P1-02: 嵌套序列导出需要的全量时间线（经普通属性传递，run 槽签名不变）
    self._worker._timeline_sequences = timeline_sequences
    ```
    而 `video_job_runner.py:248-250` 明言 "we do NOT assign private attributes on the exporter (that would be overwritten by export() and have no effect)"——同库对同类越界已有明确禁例。
  - 影响: 协调器与 worker 的状态契约散落在两处，worker 内部字段重命名/加锁时此处会静默失效；`_post_export` 传递绕过 `export()` 参数签名，类型契约不可见。
  - 建议: 在 VideoExportWorker 上提供 `prepare(post_export, timeline_sequences)` / `reset_flags()` 之类公开方法（或扩展 run 槽参数），协调器只经公开接口注入。
  - 置信度: 高

- [AUD-33603] [Suggestion] 死状态：`_config` 只写不读，`last_outcome` 全库无读方且新一轮导出开始时不重置
  - 位置: src/shared/export/video_export_worker.py:66
  - 证据:
    ```python
    self._config = None          # line 66，run() line 95 赋值、finally line 189 清空，全程无读取
    ...
    self.last_outcome = None     # line 76，仅 line 175 成功路径赋值
    ```
    全库 grep 确认 `last_outcome` 在 src/ 与 tests/ 中无任何读取点。
  - 影响: `_config` 为纯死代码；`last_outcome` 是公开属性但只写不读——若未来调用方开始读取，将拿到上一轮成功导出的陈旧 outcome（新一轮失败后不清空），易误判；当前则纯属误导性 API。
  - 建议: 删除 `_config`；`last_outcome` 要么在 run() 入口重置为 None（并补读取方/测试），要么删除。
  - 置信度: 高

- [AUD-33604] [Suggestion] 关键公开方法缺类型注解，泛型 dict 未参数化
  - 位置: src/shared/export/video_export_worker.py:79
  - 证据:
    ```python
    @pyqtSlot(object, object, object)
    def run(self, project, sequence, config) -> None:
    ```
    `export(self, project, sequence, config, ...)`（line 271-281）同样无注解；line 69 `self._timeline_sequences: Optional[dict] = None` 未参数化；line 76 `self.last_outcome = None` 无注解；line 65 用注释式 `# type: Optional[object]` 而非变量注解。
  - 影响: project/sequence/config 实际为 EditProject/TimelineSequence/TimelineExportConfig（run 文档字符串已写明），缺注解导致 IDE/审查无法静态发现传参错误，与项目"强类型"约束不符。
  - 建议: 以字符串形式注解（`"EditProject"`）避免循环导入；`Optional[Dict[str, Any]]`；统一变量注解风格。
  - 置信度: 高

- [AUD-33605] [Suggestion] post-export 检查中 `_inspect.signature` 重复求值最多 3 次，run() 成功路径过长
  - 位置: src/shared/export/video_export_worker.py:129
  - 证据:
    ```python
    accepts_telemetry = (
        "stage_telemetry" in _inspect.signature(
            self._post_export).parameters
        or any(
            p.kind == _inspect.Parameter.VAR_KEYWORD
            for p in _inspect.signature(
                self._post_export).parameters.values()
        )
    )
    ```
  - 影响: 每次导出对同一回调重复构建 Signature（首次命中即短路则 1 次，否则 2-3 次），纯冗余；更重要的是 run() 从 line 79 到 186 近 110 行、嵌套 5 层，成功路径混排 telemetry 探测/post-check/audio_omitted/hw_fallback 多个关注点，可读性与可测性差（现有测试只能整段驱动 fake exporter）。
  - 建议: `params = _inspect.signature(self._post_export).parameters` 求值一次后复用；将 telemetry 探测与 post-check 判定拆为 `_run_post_export(config, outcome)` 私有方法。
  - 置信度: 高（冗余确定；影响为可维护性层面，非功能缺陷）

### 通过项

- 线程安全（执行模型）：run() 经 `QMetaObject.invokeMethod(..., QueuedConnection)`（line 312-319）在 worker 线程执行，project/sequence/config 跨线程以 object 传参，PyQt6 标准安全模式；GL/编码全部发生在 worker 线程，UI 线程不触碰 OpenGL。
- 线程安全（早到控制请求）：`_cancel_requested`/`_pause_requested` 在 GIL 下赋值原子；run() 在 `self._exporter = exporter`（line 108）后立即复查两标志（line 109-112），覆盖了 invokeMethod 与 exporter 构造之间的竞态窗口。
- 取消/暂停转发安全：`TimelineExporter.cancel_export/pause_export/resume_export`（timeline_export.py:3580-3603）为 bool 标志 + `threading.Event`，主线程直接跨线程调用安全；取消会 `_resume_event.set()` 唤醒暂停门，不会卡死在 pause 循环。
- 进度换算正确：`_notify_progress`（timeline_export.py:10854/10856）传 0–1 比例，worker `percent * 100.0`（line 100）转 0–100，与 `deliver_controller.py:938` `int(percent)`、`video_job_runner.py:243` `percent / 100.0` 两侧消费契约对齐。
- 信号防递归：`_is_exporting` 在调用用户回调前置 False（line 345/353 先复位再回调），回调内可安全发起下一次导出；三个转发槽对用户回调均有 try/except 保护（line 347-365），异常不会中断信号链。
- 异常与取消路径：run() 顶层 `except Exception`（line 184）为线程边界惯用法，`logger.exception` 保留完整栈并转结构化 error 信号，未吞领域错误（领域失败由 ExportOutcome 三态承载，line 118-182 区分成功/用户取消/失败，不再把失败误报为取消）；post-check 异常显式映射 EXPORT_POSTCHECK_FAILED（line 152-155）；finally 清理 `_exporter`/`_config` 引用。
- ExportOutcome 字段一致性：worker 使用的 success/cancelled/error_code/cause/audio_omitted/hw_fallback_reason/stage_telemetry 均在 dataclass 定义中存在（timeline_export.py:655-688），`getattr` 防御性访问与默认值一致；`create_timeline_exporter(..., timeline_sequences=...)` 签名匹配（timeline_export.py:10865-10870）。
- GPU 资源生命周期：本文件不直接创建/持有 ModernGL 对象；headless GL 上下文由 TimelineExporter 在 worker 线程惰性创建（文件头 docstring line 10-12），与 UI 线程上下文隔离，符合项目线程规则（该上下文的释放路径属 timeline_export.py 批次范围）。
- GLSL/HDR 正确性：本文件无着色器与 HDR 像素数据路径，不适用。
- 状态提交一致性：本文件无 undo/redo/revision 状态；last_path 持久化由调用方完成（deliver_controller.py:957-958），不适用。
- 输入验证：config 校验由调用方完成（deliver_controller.py:895 `config.validate()` 拦截错误后弹窗），shared 层内部分工可接受。
