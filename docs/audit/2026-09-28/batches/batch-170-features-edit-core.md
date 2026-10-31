# batch-170 审查报告：src/features/edit/core

- 批次：batch-170 [P1]
- 编号区间：AUD-17000–AUD-17099
- 审查文件：`src/features/edit/core/placeholder_frame.py` (93 行)、`src/features/edit/core/playback_debug.py` (179 行)
- 日期：2026-09-28

---

## src/features/edit/core/placeholder_frame.py (行数: 93)

### 发现

- [AUD-17000] [Major] 占位素材元数据宽高未经验证直接 `int()`，畸形外部数据可在渲染路径抛 ValueError/TypeError
  - 位置: src/features/edit/core/placeholder_frame.py:91
  - 证据:
    ```python
    metadata = getattr(asset, "metadata", None) or {}
    meta_w = int(metadata.get("width", 0) or 0)
    meta_h = int(metadata.get("height", 0) or 0)
    return render_placeholder_frame(meta_w or width, meta_h or height)
    ```
  - 影响: `metadata` 是 `dict[str, Any]`，随工程保存/加载持久化（`project.py:5358-5367` 写入 `int`，但 JSON round-trip 后类型不保证）。若值为非数字字符串（如 `"1920px"`）、dict/list，`int()` 抛 ValueError/TypeError。两个调用点（timeline_preview_renderer.py:3556-3567、timeline_export.py:9235-9246）的 `except (ImportError, AttributeError)` 均不捕获该异常，直接逃逸进逐帧渲染/导出路径，导致预览或导出中断。违反"外部数据须验证"铁律。
  - 建议: 用安全的数值提取（先 `isinstance(v, (int, float))` 或 try/except `(TypeError, ValueError)` 后回退到传入的 `width/height`），并在本函数内兜底而非依赖调用点。
  - 置信度: 中（正常路径 `create_placeholder_asset` 写入 int；触发需要手工编辑/损坏/旧版工程文件，但异常逃逸路径已核实）

- [AUD-17001] [Major] 确定性静态帧无缓存，预览/导出逐帧重新生成，且 int64 meshgrid + 冗余 `.copy()` 造成每次调用约百 MB 级临时分配
  - 位置: src/features/edit/core/placeholder_frame.py:74-79
  - 证据:
    ```python
    frame = np.broadcast_to(row[None, :, :], (height, width, 3)).copy()

    # 45° 斜纹：((x + y) // period) % 2 == 0 的位置叠加暗化
    xx, yy = np.meshgrid(np.arange(width), np.arange(height))
    stripe_mask = (((xx + yy) // _STRIPE_PERIOD) % 2 == 0)[..., None]
    frame = np.where(stripe_mask, frame * 0.65, frame)
    ```
  - 影响: 输出是纯确定性的（同 width/height 结果恒定），但两个调用点（timeline_preview_renderer.py:3567、timeline_export.py:9246）在逐帧渲染路径中直接调用、无任何缓存——导出一段占位片段会对每个输出帧重复计算。4K 下单次调用分配两个 int64 meshgrid（约 66MB）+ 布尔掩码 + 多个 float32 中间数组，合计约 170MB 瞬时内存churn；line 74 的 `.copy()` 也多余（`np.where` 恒返回新数组，广播只读视图可直接作为输入）。与 4K 实时基准冲突。
  - 建议: 按 `(width, height)` 做 `functools.lru_cache` 或调用点级缓存（输出只读分发即可）；掩码可用单行/单列广播或 `np.add.outer` 且用 `np.int32` 累加，去掉 `.copy()`。
  - 置信度: 高（函数纯确定性、调用点无缓存均已核实）

- [AUD-17002] [Suggestion] 类型注解风格不一致：`tuple`/`Tuple` 混用、返回类型用字符串字面量
  - 位置: src/features/edit/core/placeholder_frame.py:27
  - 证据:
    ```python
    _SMPTE_COLORS: tuple[Tuple[float, float, float], ...] = (
    ```
    以及 `def render_placeholder_frame(width: int, height: int) -> "np.ndarray":`（line 57，`np` 已 import，无需字符串引号）。
  - 影响: 同一文件混用内建泛型与 `typing.Tuple`，返回注解不必要的字符串化，降低一致性；`__all__`（line 19-23）也未包含实际被两个外部模块引用的 `render_placeholder_frame_for_asset`。
  - 建议: 统一为 `tuple[tuple[float, float, float], ...]`（文件已有 `from __future__ import annotations`），返回注解直接写 `np.ndarray`，并将 `render_placeholder_frame_for_asset` 补入 `__all__`。
  - 置信度: 高

### 通过项
- 线程安全：纯函数、无共享可变状态，多线程调用安全
- GPU 资源生命周期：不涉及任何 OpenGL/ModernGL 资源
- GLSL·HDR 正确性：无 GLSL；uint8 BGR 输出与模块契约一致（占位底图本就不属于 HDR 数据通路）
- 信号防递归：无 Qt 控件/信号
- 状态提交一致性：无 undo/redo/revision 逻辑
- 输入验证（尺寸部分）：`max(2, int(width/height))` 对尺寸做了下限钳制，`None`/falsy 元数据值经 `or` 回退到传入尺寸
- 类型注解：所有公开函数均有参数与返回注解（仅风格问题见 AUD-17002）

---

## src/features/edit/core/playback_debug.py (行数: 179)

### 发现

- [AUD-17003] [Major] 模块导入期用 `float()` 解析外部环境变量且无容错，坏值导致 import 失败并拖垮全部依赖模块（trace 关闭时也执行）
  - 位置: src/features/edit/core/playback_debug.py:20
  - 证据:
    ```python
    _SLOW_THRESHOLD_MS = float(os.environ.get("TOPOS_EDIT_PLAYBACK_SLOW_MS", "40"))
    ```
  - 影响: 环境变量是外部输入。设为空串或非数字（如 `TOPOS_EDIT_PLAYBACK_SLOW_MS=4O`）时 `float("")/float("4O")` 抛 ValueError，且发生在模块导入期、不受 `_ENABLED` 开关保护（trace 关着也会炸）。本模块被 timeline_preview_renderer（161 处引用）、audio_playback、edit_page 等核心链路导入，一处坏值即应用无法启动。违反"外部数据须验证"铁律。
  - 建议: 用 try/except `(TypeError, ValueError)` 包裹并回退默认 40.0，或写一个 `_parse_float_env(name, default)` 辅助函数。
  - 置信度: 高（失败机制确定；触发需要误设环境变量，但该解析无条件执行）

- [AUD-17004] [Suggestion] `print` 在 `_LOCK` 之外执行，多线程并发 trace 可产生交错撕裂的输出行，且 seq 与实际打印顺序可能颠倒
  - 位置: src/features/edit/core/playback_debug.py:149
  - 证据:
    ```python
    with _LOCK:
        seq = next(_SEQ)
        elapsed_ms = (now - _T0) * 1000.0

    parts = [
        f"[EDIT_PLAYBACK_TRACE seq={seq:05d} +{elapsed_ms:9.3f}ms",
        ...
    print(" ".join(parts), flush=True)
    ```
  - 影响: 锁只保护 seq/elapsed 的读取，格式化与 `print` 在锁外。`print` 对 stdout 是两次写（正文 + 换行），音频线程、渲染 worker、UI 线程并发调用时可出现半行交错/串行错乱，恰好损害该模块的核心用途（多线程播放问题的"路由/背压证据"采集）；另外先取到 seq 的线程可能后打印，日志中 seq 顺序不可靠。
  - 建议: 将 `parts` 组装与 `print` 一并移入 `with _LOCK:` 块内（print 耗时短，可接受）；如担心锁内 IO，可改用 `logging` 单行 emit 或队列+独立输出线程。
  - 置信度: 高（机制确定；仅在启用 trace 时显现，故仅评 Suggestion）

### 通过项
- 线程安全（共享状态部分）：`_SEQ`/`_T0` 的读取与 `reset_playback_trace` 的重赋值均在 `_LOCK` 内完成；`reset_playback_trace` 在锁释放后才调用 `playback_trace`，无重入死锁
- GPU 资源生命周期：不涉及 GPU 资源
- GLSL·HDR 正确性：无渲染数据通路
- 信号防递归：无 Qt 信号/控件
- 状态提交一致性：无 undo/redo/持久化逻辑
- 异常与取消路径：无宽泛 `except` 掩盖领域错误；禁用时所有入口提前返回，零开销
- 输入验证（trace 开关）：`_TRACE_MODE` 的 on/off 词表解析大小写不敏感、默认关闭，逻辑正确
- 性能模式：禁用/摘要模式下 `_should_print_summary` 为纯字典与子串判断，开销可忽略
- 类型注解：全部函数含参数与返回注解，`_format_value` 对超长值截断、含空白值加 repr 防注入格式

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-17000, AUD-17001（placeholder_frame.py）、AUD-17003（playback_debug.py） |
| Suggestion | 2 | AUD-17002（placeholder_frame.py）、AUD-17004（playback_debug.py） |

合计 5 项发现。
