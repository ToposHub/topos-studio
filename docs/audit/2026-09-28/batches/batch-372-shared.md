# batch-372 [P2] — src/shared/ 审查报告

任务区间：AUD-37200–AUD-37299。审查文件：`src/shared/render_diagnostics.py`（265 行，全文通读）。
交叉验证：grep 全库确认了写入方（`src/features/color/core/shader_compiler.py:332,409`、`src/features/shared/effects/effect_pass_runner.py:707,733`）、读取方（`src/features/edit/ai_editing/tool_facade.py:1009-1027`）及测试（`tests/edit/ai_editing/test_render_diagnostics.py`）的实际调用形态。

## src/shared/render_diagnostics.py (行数: 265)

### 发现

- [AUD-37200] [Suggestion] `_VALID_SOURCES` 定义后从未使用，`record()` 对 `source`/`severity` 不做任何枚举校验
  - 位置: src/shared/render_diagnostics.py:45-47（定义）、src/shared/render_diagnostics.py:171-172（未校验的写入）
  - 证据:
    ```python
    _VALID_SOURCES = frozenset({
        SOURCE_COLOR_PIPELINE, SOURCE_COMPOSITION, SOURCE_EFFECT_PASS,
    })
    ...
    entry = CompileDiagnostic(
        timestamp=time.time(),
        source=str(source),
        severity=str(severity),
    ```
  - 影响: 全库 grep 确认 `_VALID_SOURCES` 仅在此处定义、无任何引用（死常量）。写入方若传错来源标签（如拼写错误）不会报错，条目照常入缓冲，但 AI 工具 `topos.get_render_diagnostics` 按 schema enum（tool_facade.py:364-368，用 `SOURCE_*` 常量另行构造）过滤时将静默返回空——诊断能力无声失效且无日志可查；`severity` 同样接受任意字符串（如显式传 `None` 会存成 `"None"`）。
  - 建议: 在 `record()` 中用 `_VALID_SOURCES` 校验 `source`（非法值回退默认并记 warning，或直接保留原值但打日志），`severity` 同理；或删除死常量并在注释中指明权威 enum 在 tool schema。
  - 置信度: 高（未使用是 grep 确证的事实；影响评估为低概率、 diagnosability 损失）

- [AUD-37201] [Suggestion] `subjects` 只做 str 化去重，未走 `_normalize_context_identifier`，与其余 ID 字段的防路径规整不对称
  - 位置: src/shared/render_diagnostics.py:163-167（subjects 处理）、src/shared/render_diagnostics.py:173-178（其他字段均已规整）
  - 证据:
    ```python
    for s in subject_values:
        sid = str(s)
        if sid and sid not in seen:
            seen.add(sid)
            subs.append(sid)
    ...
    code=_normalize_context_identifier(code) or CODE_SHADER_COMPILE_FAILED,
    subjects=tuple(subs),
    request_id=_normalize_context_identifier(request_id),
    layer_id=_normalize_context_identifier(layer_id),
    plugin_ids=_normalize_context_identifiers(plugin_ids),
    ```
  - 影响: 模块头注红线要求"不存储绝对路径"，`_normalize_context_identifier` 的存在目的就是剥离路径语法（`re.sub(r"[^A-Za-z0-9_.:-]+", "_", ...)`），却唯独没用在最核心的 `subjects` 上——该不变量目前完全依赖写入方自律（shader_compiler 传 `binding.spec.name`，是展示名而非路径，当前安全）加上读取侧 `sanitize_untrusted` 的标记；未来新增写入方一旦传入含路径的值，路径会原样进入 AI 可读 payload（虽被标记为不可信文本，但"不含路径"的模块承诺已破）。
  - 建议: `record()` 中对每个 subject 追加 `sid = _normalize_context_identifier(s)`（测试 `test_subjects_dedup_and_coerce` 中 `"Film_Halation"`/`"42"` 等现有值经该正则不变，兼容）；或在头注明确说明 subjects 的防路径责任归属写入方与读取侧 sanitize。
  - 置信度: 中（tests 显示现状是既定约定，非 bug；但纵深防御缺口真实存在）

- [AUD-37202] [Suggestion] `message` 未强制 `str()` 转换，非字符串真值会破坏 `message: str` 类型契约或直接抛 TypeError
  - 位置: src/shared/render_diagnostics.py:168
  - 证据:
    ```python
    msg = (message or "")[:_MAX_MESSAGE_LEN]
    ```
  - 影响: `message or ""` 只挡住 falsy 值。若传入 truthy 非 str：`bytes` 切片返回 `bytes` 被存入声明为 `str` 的字段，`to_dict()` 随之返回 bytes，破坏读取侧 JSON 序列化契约；`int`/异常对象等不可切片对象则 `[:4000]` 直接抛 TypeError——经 `record_compile_failure()` 调用会被吞掉（仅 debug 日志），但直接调用 `.record()` 的调用方（如测试里 `sink.record(...)`）会中招且与"record 是普通方法、可抛参数错误"的预期不一致。相比之下同函数里 `source`/`severity` 都显式 `str()` 了，唯独 message 没有。
  - 建议: 改为 `msg = str(message or "")[:_MAX_MESSAGE_LEN]`，与 `source`/`severity` 的处理保持一致。
  - 置信度: 中（现有三个写入方都传 `str(e)`/f-string，当前不触发；属防御性缺口）

- [AUD-37203] [Suggestion] 全局诊断单例从不 `clear()`，跨工程/会话的陈旧编译失败仍会返回给 AI 工具
  - 位置: src/shared/render_diagnostics.py:200-202（clear 无调用方）、src/shared/render_diagnostics.py:215-226（全局单例）
  - 证据:
    ```python
    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
    ...
    _SINK: RenderDiagnosticSink | None = None
    ```
  - 影响: 全库 grep 确认 `RenderDiagnosticSink.clear()` 在 src/ 下零调用（仅测试重置单例用 `set_render_diagnostic_sink`）。环形缓冲是进程级全局，条目只含 `layer_id`/`request_id`，无工程标识；用户关闭/切换工程后，旧工程最近 50 条编译失败仍会被 `topos.get_render_diagnostics` 一并返回，AI 排查新工程"效果为何不生效"时可能被陈旧记录误导（layer_id 恰好撞名时更甚）。
  - 建议: 在工程关闭/切换的生命周期钩子里调用 `get_render_diagnostic_sink().clear()`；或为条目增加轻量的 session/project epoch 并在 `recent()` 过滤。
  - 置信度: 中（行为确证；实际误导概率取决于使用模式）

### 通过项

- 线程安全：所有 `_entries` 读写均在 `self._lock` 内；`record()` 在锁外构建 entry、锁内 append+裁剪（`del self._entries[:overflow]` 环形裁剪正确），锁持有时间最小化；`recent()` 锁内快照、锁外过滤/反转，返回的 frozen dataclass 不可变，跨线程共享安全。
- 单例并发：`get_render_diagnostic_sink()` 双重检查锁在 CPython(GIL) 语义下安全——外层读要么见 None 要么见构造完成后才赋值的完整对象。
- GPU 资源生命周期：本模块不持有任何 ModernGL/纹理/缓冲对象，纯内存数据结构，无泄漏面。
- GLSL·HDR 正确性：不涉及 shader 生成或像素数据，无 clamp/字面量问题。
- 信号防递归：无 Qt 控件与信号，不适用。
- 状态提交一致性：属瞬态诊断缓冲而非可 undo 的文档状态，定位正确，未混入 undo/redo/revision 体系。
- 异常与取消路径：`record_compile_failure` 的"永不抛异常"契约成立（宽泛 `except Exception` 带 `# noqa: BLE001`、`exc_info=True` 记日志且有明确理由——诊断不得拖垮渲染，符合 .clinerules 对宽泛 except 需有正当理由的要求；测试 `test_never_raises_on_bad_args` 覆盖了 `source=None, message=None, subjects=123` 等坏输入）。
- 容量与截断：`_MAX_MESSAGE_LEN`/`_MAX_IDENTIFIER_LEN` 截断逻辑正确，`capacity = max(1, int(capacity))` 防御了非法容量；`recent()` 的 limit 钳制（`max(0, min(...))`）正确处理 0/负数/超容量。
- 性能模式：热路径（编译失败，低频）O(1) 追加 + 摊销裁剪，快照仅复制 ≤50 个引用，`count()` O(n≤50) 在锁内可忽略；无每帧开销。
- 类型注解：全模块函数/方法/字段注解完整（含 `from __future__ import annotations` 的 `str | None` 用法），无缺失。
- 隐私红线（部分通过）：不读文件、不存绝对路径、不保留 shader 源码的总体设计成立——message 为运行时 driver 报错且读取侧统一 `sanitize_untrusted` 标记（subjects 的纵深防御缺口见 AUD-37201）。
