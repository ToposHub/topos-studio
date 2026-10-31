# 批次报告：batch-259 — src/features/scripting//

编号区间：AUD-25900–AUD-25999。审查日期：2026-09-28。
文件：`__init__.py`(1) / `api.py`(1516) / `composite_api.py`(177) / `engine.py`(381)，共 4 个文件 2,075 行，全部通读。
交叉核对过：`src/shared/ui/widgets/curves_widget.py`、`hsl_curves_widget.py`、`src/features/color/core/history.py`、`image_engine.py`、`layer.py`、`preset_meta.py`、`src/shared/paths/paths.py`、`src/app/controllers/layer_controller.py`、仓库 `luts/` 目录布局。

---

## src/features/scripting/__init__.py (行数: 1)

### 发现

（无）

### 通过项
- 通过（无逻辑）：文件仅含一行注释 `# Topos Color Scripting System`，无代码、无导出符号，无审查维度适用。

---

## src/features/scripting/api.py (行数: 1516)

### 发现

- [AUD-25900] [Critical] RGB 曲线 y 轴方向与 UI/持久化约定相反，曲线经 UI 回读后整体反相
  - 位置: src/features/scripting/api.py:119
  - 证据:
    ```python
    if len(xs) == 2:
        slope = (ys[1] - ys[0]) / (xs[1] - xs[0]) if xs[1] != xs[0] else 0
        lut = (ys[0] + slope * (np.arange(256, dtype=np.float32) - xs[0])) / 255.0
        return np.clip(lut, 0.0, 1.0)
    ```
    而 UI 侧 `src/shared/ui/widgets/curves_widget.py:457-458` 为：
    ```python
    curve_y = self.calculate_curve(self.curves[ch])
    self._lut_cache[ch] = (255.0 - curve_y) / 255.0
    ```
    且本文件 set_curves 未提供通道时的默认值（api.py:684）`curves_data['M'] = [(0.0, 255.0), (255.0, 0.0)]` 恰是 UI 约定下的恒等线。
  - 影响: `_interpolate_curve_255` 把 y 直接当输出（y/255），UI/`layer.curves_data` 持久化约定却是 (255-y)/255（y=0 为白）。脚本 `set_curves(master=[(0,0),(255,255)])` 立即生效为恒等曲线，但写入 `curves_data` 的同一组点被 `layer_controller.update_from_layer`（layer_controller.py:44-45）载入 CurvesWidget 后显示为反向对角线；用户一旦拖动任意控制点，节流信号发出的 LUT 即按 (255-y)/255 计算，图像瞬间翻转为相反调色（近似负片）。docstring 声称 "matches UI widget coordinates"（api.py:655）与实现矛盾。渲染错误级。
  - 建议: 在 `_interpolate_curve_255` 中与 UI 统一：先按排序点计算 y，再以 `(255.0 - y) / 255.0` 生成 LUT（或等价地在入口反转输入 y），并同步修订 docstring 与示例；补一条脚本→UI→脚本的往返回归测试。
  - 置信度: 高

- [AUD-25901] [Critical] batch() 在 output_dir=None 时原地覆盖原始文件，源图不可恢复
  - 位置: src/features/scripting/api.py:1136
  - 证据:
    ```python
                if output_dir:
                    name, _ = os.path.splitext(os.path.basename(file_path))
                    ...
                    out_path = os.path.join(output_dir, name + ext)
                else:
                    out_path = file_path

                result = self.save(out_path, format=format, quality=quality)
    ```
    结合 save()（api.py:459-461）`ext = fmt_ext.get(format, ...)`，对 `.jpg` 输入与默认 `format="JPEG"`，路径保持不变，直接覆写原文件。
  - 影响: 最常见的批量场景（对目录内 jpg 做批处理且未传 output_dir）会把 8-bit JPEG q95 的调色结果无确认、无备份地覆盖原始 jpg——原始素材数据丢失。docstring 完全未提示该行为。
  - 建议: output_dir 缺省时要么强制要求显式参数、要么默认写到子目录（如 `graded/`），至少在 docstring 与运行前输出中明确“将覆盖原文件”；save() 可对“目标 == 当前源文件路径”做保护提示。
  - 置信度: 中（可能是有意的就地调色行为，但无文档、无确认、不可撤销）

- [AUD-25902] [Major] apply_lut 相对路径解析到 src/luts/（回溯 3 层），与全项目 luts 根目录不符
  - 位置: src/features/scripting/api.py:781
  - 证据:
    ```python
        def _do():
            if not os.path.isabs(path):
                project_root = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
                path_resolved = os.path.join(project_root, "luts", path)
    ```
    api.py 位于 `src/features/scripting/`，回溯 3 层得到 `src/`。仓库实际 `.cube` 文件在根目录 `luts/`（cinematic.cube 等），`src/luts/` 为空；统一路径工具 `src/shared/paths/paths.py:39` 也以项目根 `luts/` 为准。
  - 影响: `topos.apply_lut("cinematic.cube")` 查找 `src/luts/cinematic.cube` → FileNotFoundError，随附 LUT 相对名全部不可用；与 UI/quick_export 的 LUT 目录解析不一致。
  - 建议: 改用 `src.shared.paths.paths.get_lut_dir()`（注意该函数自身的回溯层级历史问题，见其 docstring），或在冻结/源码两种形态下统一解析到真正的项目根。
  - 置信度: 高

- [AUD-25903] [Major] 曲线两点路径 clip(0,1) 与多点/PCHIP/Widget 的线性外推不裁切行为不一致
  - 位置: src/features/scripting/api.py:122
  - 证据:
    ```python
    if len(xs) == 2:
        slope = (ys[1] - ys[0]) / (xs[1] - xs[0]) if xs[1] != xs[0] else 0
        lut = (ys[0] + slope * (np.arange(256, dtype=np.float32) - xs[0])) / 255.0
        return np.clip(lut, 0.0, 1.0)
    ```
    而同函数 PCHIP 路径（api.py:133-153）`res[mask_mid] = pchip(...)` 后仅 `return res / 255.0`，无 clip；widget `_compute_lut` 的注释明确“使用线性外推处理超出范围的值，避免裁切色彩信息”（curves_widget.py:446-453）。
  - 影响: 同一调色意图用 2 个控制点与 3 个控制点会得到不同 LUT（2 点时黑/白点外推被 clamp 到 [0,1]，HDR 高光/阴影信息被无理由裁切）；也违反“HDR 内部数据不得无理由 clamp(0.0,1.0)”铁律（curves LUT 属调色语义数据，外推段是设计行为）。
  - 建议: 去掉两点路径的 `np.clip`，与 PCHIP 路径及 CurvesWidget 保持一致（仅最终采样语义需要 0-1 时不在此处裁）。
  - 置信度: 高

- [AUD-25904] [Major] 脚本级调色不使视频缓存渲染参数失效，视频播放期间脚本修改可能被缓存覆盖
  - 位置: src/features/scripting/api.py:597
  - 证据:
    ```python
            if gain is not None:
                gp["gain"] = tuple(gain)
            self._engine.render_pipeline()
        self._bridge.call_sync(_do)
    ```
    UI 的每个调色入口在渲染前都调用 `self._invalidate_video_render_params_if_playing()`（layer_controller.py:1652 等 10+ 处），脚本路径（grade/set_curves/set_hsl_curve/set_qualifier 等）只调 `render_pipeline()`。
  - 影响: 视频已加载/播放时执行脚本调色，S5.C 的视频渲染参数缓存不失效，后续帧可能继续用旧参数渲染——脚本的调色不生效或被下一帧覆盖，属于功能缺陷（对图像模式无影响）。
  - 建议: 在脚本 API 的各渲染入口（或统一的 `_commit()` 辅助）里，经 bridge 同步调用与 UI 等价的缓存失效逻辑（`video_controller.invalidate_cached_render_params`）。
  - 置信度: 中（依赖视频缓存消费时机，但 UI 侧密集的失效调用表明其为必要步骤）

- [AUD-25905] [Suggestion] PchipInterpolator 对重复 x 直接抛 ValueError，无 widget 版的兜底，环境间行为不一致
  - 位置: src/features/scripting/api.py:131
  - 证据:
    ```python
    try:
        from scipy.interpolate import PchipInterpolator
    except ModuleNotFoundError:
        return np.interp(xi, xs, ys).astype(np.float32) / 255.0

    pchip = PchipInterpolator(xs, ys)
    ```
    `_interpolate_hsl_curve_255`（api.py:232）同样；而 widget 的 `calculate_curve` 外层有 `except: return np.interp(...)`（curves_widget.py:336-337）。
  - 影响: 控制点 x 重复（用户脚本常见笔误）时：有 scipy → 向脚本抛 scipy 内部 "x must be strictly increasing"；无 scipy → np.interp 静默容忍。错误信息不友好且跨环境不一致。
  - 建议: 构造前按 x 去重/排序验证并给出明确错误，或像 widget 一样兜底 np.interp。
  - 置信度: 高（机制确定）

- [AUD-25906] [Suggestion] 多处 implicit-Optional 类型注解与缺失返回注解
  - 位置: src/features/scripting/api.py:444
  - 证据:
    ```python
    def save(self, path: str, format: str = None, quality: int = 95) -> str:
    ```
    同类：`add_layer(self, name: str = None)`(488)、`reset_layer(self, index: int = None)`(536)、`batch(..., output_dir: str = None...)`(1095)、`input_number(..., min_val: float = None, ...)`(1296)、`get_image_size(self) -> tuple`(1193) 裸泛型、`open`/`crop` 等无返回注解。
  - 影响: 与强类型规范不符；`str = None` 在严格模式（PEP 484/661）下属类型错误，误导调用方。
  - 建议: 改为 `str | None` / `int | None`，`-> tuple[int, int]`，补齐公开方法返回注解。
  - 置信度: 高

- [AUD-25907] [Suggestion] capture_still/apply_preset 的名称参数未验证，可写出 Presets 目录之外
  - 位置: src/features/scripting/api.py:1413
  - 证据:
    ```python
            current_folder = mw.presets_panel.get_current_folder()
            preset_folder = os.path.join(current_folder, preset_name)
            os.makedirs(preset_folder, exist_ok=True)
    ```
    `preset_name = name`（api.py:1399）未过滤；apply_preset 同样直接 `os.path.join(presets_root, folder, preset_name, "grade.json")`（api.py:1465）。
  - 影响: 名称含 `/`、`..` 等时目录穿越，仍会写盘/读盘（受信任脚本场景下风险低，但违反“外部数据须验证”约束）；重名时静默覆盖已有 preset。
  - 建议: 校验名称只含安全字符且 basename 不变；已存在时拒绝或明确覆盖语义。
  - 置信度: 高（代码事实），影响评级低

- [AUD-25908] [Suggestion] _find_main_window 回退分支缓存任意可见顶层控件，后续引擎调用可能 AttributeError
  - 位置: src/features/scripting/api.py:1374
  - 证据:
    ```python
        # Fallback: any top-level widget (prevents dialogs from hiding behind)
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible():
                self._main_window = widget
                return widget
    ```
  - 影响: 回退命中非 MainWindow（如独立的对话框/浮窗）时被永久缓存（无失效机制），随后 `open()` 里 `mw.update_canvas(...)`、`mw.layer_panel`（api.py:431-440）抛 AttributeError；主窗口销毁重建后同样持陈旧引用。
  - 建议: 回退分支不缓存（仅对话框 parenting 用）；缓存按窗口标题/objectName 校验或加弱引用。
  - 置信度: 中

### 通过项
- 线程安全：全部 ImageEngine/MainWindow 访问均封装在 `call_sync` 闭包内于主线程执行（open/save/grade/curves/qualifier/dialogs 等）；`_find_main_window` 的 QApplication 遍历也都发生在主线程闭包内。
- 信号防递归：脚本路径直接改 layer 字段 + render_pipeline，不经过 UI 控件信号，无递归触发面。
- GPU 资源生命周期：脚本 API 不直接创建 ModernGL 对象；apply_curves/apply_hsl_curve 经 engine.renderer.submit 上传纹理（image_engine.py:1362-1387），无泄漏点。
- HSL 曲线正确性：`_interpolate_hsl_curve_255` 的 `(255-y)/127.5`、`(127.5-y)/255` 与 HSLCurvesWidget（hsl_curves_widget.py:673-705）完全一致，往返一致。
- 状态提交一致性：grading_params 键名（shadow/highlights/temp 等）与 layer.py 默认结构一致（layer.py:372-378）；begin_undo 经 history.push_state 深序列化图层（history.py:44-50），_auto_undo_snapshot 失败时降级不阻断脚本。
- 异常与取消路径：无活动层抛 RuntimeError、文件不存在抛 FileNotFoundError 均明确；list_clips/clip_grade/grade_snapshot 失败降级返回空并 logger.warning，不吞领域错误。
- 输入验证（部分）：open/save 路径存在性检查、set_mixer 数值 clamp [-100,100]、apply_lut 绝对存在性检查、API 版本声明校验（主版本拒绝/次版本警告逻辑正确）。
- 性能模式：curves LUT 向量化 numpy 计算，scipy 缺失有 np.interp 回退；`parse_api_requirement` 只扫描前 40 行避免全文正则。
- undo 事务：_undo_started 标志防止重复快照；flip 经 engine.flip_image 自带 push_state。

---

## src/features/scripting/composite_api.py (行数: 177)

### 发现

- [AUD-25914] [Suggestion] 兼容回退路径调用页面私有方法，且 _apply_plan 重复解析 page
  - 位置: src/features/scripting/composite_api.py:170
  - 证据:
    ```python
        # 兼容尚未接入新 hook 的旧页面；至少触发同一套评估/持久化信号。
        if hasattr(page, "_emit_user_edit_once"):
            page._emit_user_edit_once()
        if hasattr(page, "_evaluate_graph_now"):
            page._evaluate_graph_now()
    ```
    另外 `_apply_plan`（api 侧 154-160）先 `page = self._page_getter()` 再调 `self._service()`，后者内部再次 `page_getter()`，一次 apply 做两次主线程页面查找。
  - 影响: 跨模块访问下划线私有方法，页面内部重构会静默绕过该兼容分支（hasattr 掩盖）；重复查找属轻微冗余。
  - 建议: 将回退逻辑改为页面公开 hook（或经 page 提供的公共接口触发评估/持久化）；`_service()` 接受可选 page 参数复用查找结果。
  - 置信度: 高（代码事实），影响低

### 通过项
- 线程安全：snapshot/validate/apply 及全部便捷方法最终经 `bridge.call_sync` 在主线程执行；page_getter（内含 `_find_main_window`）只在 call_sync 闭包内求值。
- 协议一致性：与 AI/MCP 共用 `CompositeEditService` 的同名操作、值域与校验（构建 CompositeEditPlan），单一代码路径。
- 异常路径：Composite page 未激活/无 node_graph 时抛 RuntimeError("Composite page is not active")，错误明确。
- 状态提交一致性：`_notify_page_changed` 优先 `record_external_composite_edit`（UI/工程保存/预览/undo 同一份变更），设计正确。
- 输入验证：委托 `CompositeEditService.validate/apply` 统一校验，API 层不重复实现。

---

## src/features/scripting/engine.py (行数: 381)

### 发现

- [AUD-25909] [Critical] 脚本抛出 SystemExit/KeyboardInterrupt 逃出 run() 重写：script_finished 不发射，且可能触发 qFatal 整进程崩溃
  - 位置: src/features/scripting/engine.py:347
  - 证据:
    ```python
            with _StdoutRedirect(stdout_stream, stderr_stream):
                try:
                    # Auto-save undo snapshot if script doesn't call begin_undo manually
                    api._auto_undo_snapshot()
                    compiled = compile(code, path or "<script>", "exec")
                    exec(compiled, namespace)
                    self.script_finished.emit(True, "Script completed successfully.")
                except ScriptAbortError:
                    ...
                except Exception:
    ```
    `_run_action`（296-319）同样只捕 `ScriptAbortError`/`Exception`。
  - 影响: 用户脚本常见写法 `sys.exit(0)` / `raise SystemExit`（BaseException，不被 `except Exception` 捕获）会逃出被重写的 `QThread.run`（engine.py:250 `self._thread.run = lambda: ...`）。PyQt6 中逃出虚方法重实现的未处理异常按 qFatal 处理 → abort 整个应用（崩溃）；即便仅打印，`script_finished` 也永不发射，UI 一直显示脚本运行中。
  - 建议: 在 `_run_script`/`_run_action` 增加 `except BaseException`（或至少 `SystemExit`/`KeyboardInterrupt`）分支：正常 flush、emit `script_finished(False, ...)` 后（对 SystemExit(0) 可视为完成）再决定是否重抛；绝不让异常逃出 run 重写。
  - 置信度: 中（信号缺失部分确定；qFatal 崩溃依赖 PyQt6 虚方法异常处理行为，未在本仓库内实测）

- [AUD-25910] [Major] call_sync 文档化的 timeout 实际不可达：BlockingQueuedConnection 的 emit 先阻塞至槽完成，等待/超时循环形同虚设
  - 位置: src/features/scripting/engine.py:62
  - 证据:
    ```python
        self._execute_in_main.connect(
            self._do_execute, Qt.ConnectionType.BlockingQueuedConnection
        )
    ```
    ```python
        self._execute_in_main.emit(func)

        self._mutex.lock()
        deadline = time.monotonic() + timeout / 1000.0
        while True:
            # Accept result only from *this* call (prevents stale wakeups)
            if self._done and self._result_seq == expected_seq:
                break
    ```
  - 影响: BlockingQueuedConnection 下 `emit()` 在主线程执行完 `_do_execute` 前不返回，返回时 `_done` 已为 True，循环首轮即 break——`TimeoutError` 分支（含 docstring 宣称的可按调用覆盖 timeout）永远无法触发；主线程卡死时脚本线程在 `emit` 内部无限挂起，超时保护失效（且 wait 循环里的 abort 检查同样不可达，`request_abort` 的 wakeAll 唤不醒不在 wait 中的线程）。
  - 建议: 二选一：改用 `Qt.ConnectionType.QueuedConnection` 让 timeout/abort 机制真正生效（配合 AUD-25911 的 seq 修正）；或保留 BlockingQueuedConnection 并删除/改文档化 timeout 参数，另以主线程心跳监控实现挂起检测。
  - 置信度: 高

- [AUD-25911] [Suggestion] 结果序号按"执行时"的 _seq 标记而非"发起时"，若等待路径被启用存在陈旧结果错配隐患
  - 位置: src/features/scripting/engine.py:98
  - 证据:
    ```python
            result = func()
            self._mutex.lock()
            self._result = result
            self._result_seq = self._seq
            self._done = True
    ```
  - 影响: `_do_execute` 读取的是执行时刻的 `self._seq`。当前因阻塞连接该路径不可达（见 AUD-25910），但一旦改为队列连接/超时生效：超时后的旧调用晚到执行会把结果记到最新一次调用的 seq 上，下一次 `call_sync` 直接返回上一个（超时）调用的结果——张冠李戴。
  - 建议: emit 前把 `expected_seq` 随 payload 传递（闭包捕获），`_do_execute` 用发起时的 seq 写 `_result_seq`。
  - 置信度: 中（当前不可触发，属防御性设计缺陷）

- [AUD-25912] [Suggestion] reset_abort/is_aborted 无锁读写共享 _abort 标志
  - 位置: src/features/scripting/engine.py:117
  - 证据:
    ```python
        def reset_abort(self):
            self._abort = False

        def is_aborted(self) -> bool:
            """Return whether an abort has been requested."""
            return self._abort
    ```
  - 影响: call_sync/request_abort 全程持 `_mutex` 访问 `_abort`，这两个入口却裸读写（主线程 reset 与脚本线程读并发）。bool 赋值近似原子，实际风险低，但破坏了既定同步契约。
  - 建议: 统一在 `_mutex` 保护下读写 `_abort`。
  - 置信度: 高（不一致事实确定，后果轻微）

- [AUD-25913] [Suggestion] stop() 仅协作式中止，纯 Python 循环中的脚本无法停止
  - 位置: src/features/scripting/engine.py:51
  - 证据:
    ```python
        if self._abort:
            raise ScriptAbortError("Script aborted")
    ```
    （call_sync 入口是唯一实际可达的 abort 检查点；`stop()` 只调用 `request_abort()`。）
  - 影响: 脚本若在长时间纯 Python 计算/死循环中不经过任何 bridge 调用（如 `while True: pass`），"停止"按钮毫无作用，只能强杀进程；用户对批处理大图脚本的中断预期落空。
  - 建议: 至少在 docstring 标明中止为协作式；可选增强：注册 `sys.settrace`/周期心跳检查，或 UI 提供线程终止兜底。
  - 置信度: 高（机制确定）

### 通过项
- 信号跨线程安全：`output_ready`/`error_output_ready`/`script_finished` 等在脚本线程 emit，接收方（ScriptEngine 属主为主线程）经 Qt 自动队列连接跨线程传递，无 UI 线程直接操作。
- 输出重定向生命周期：`_EmittingStream` 按换行/4096 字符批量回调，`finally` 中 `flush()` 保证脚本尾部残留输出不丢失；`_StdoutRedirect.__exit__` 恢复原始 stdout/stderr。
- 线程生命周期：`execute`/`run_registered_action` 以 `_is_running` 防重入；`_on_thread_finished` 复位状态并 `deleteLater()`，无 QThread 泄漏。
- 行号归因：异常行号优先匹配用户脚本帧（`co_filename == script_name`），回退最内层帧，归因合理。
- API 版本门禁：执行前 `parse_api_requirement` + `check_api_compatibility`，主版本不匹配拒绝执行并给迁移提示，次版本超前仅警告，逻辑正确。
- 输入验证：脚本路径不存在由 open() 抛 FileNotFoundError；`parse_api_requirement` 只扫描前 40 行，正则锚定行首防误报。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 3 | AUD-25900, AUD-25901, AUD-25909 |
| Major | 4 | AUD-25902, AUD-25903, AUD-25904, AUD-25910 |
| Suggestion | 8 | AUD-25905, AUD-25906, AUD-25907, AUD-25908, AUD-25911, AUD-25912, AUD-25913, AUD-25914 |
