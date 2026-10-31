# Batch 035 — src/app/main_window.py 静态审查

- 日期: 2026-09-28
- 审查对象: `src/app/main_window.py`（2,008 行，全部 Read 核对）
- 交叉验证（只读 grep/sed，用于避免误报）: `src/app/controllers/file_controller.py`、`src/app/update_checker.py`、`src/features/color/core/denoise_engine.py`、`src/features/scripting/engine.py`、`src/features/color/ui/crop_panel.py`、`src/app/ui/library_panel.py`、`src/app/ui/files_panel.py`、`src/app/controllers/menu_toolbar_controller.py`
- 严重度统计: Critical 0 / Major 5 / Suggestion 10

## src/app/main_window.py (行数: 2008)

### 发现

- [AUD-3500] [Major] closeEvent 在弹出"保存确认"之前就关闭全部后台服务，用户取消关闭后应用处于半瘫痪状态
  - 位置: src/app/main_window.py:1911-1946（交叉验证 src/app/controllers/file_controller.py:6738-6740）
  - 证据:
    ```python
    try:
        tangent_bridge = getattr(self, "tangent_color_bridge", None)
        if tangent_bridge is not None:
            tangent_bridge.shutdown()
    except Exception:
        logger.exception("MainWindow: Tangent Hub shutdown failed")
    ...
    try:
        self._shutdown_playback_pipeline()
    except Exception:
        logger.exception("MainWindow: playback pipeline shutdown failed")
    # QMainWindow 在本类 MRO 中排在所有 controller mixin 之前，...
    FileOpsMixin.closeEvent(self, event)
    ```
    而 FileOpsMixin.closeEvent 开头（file_controller.py:6738-6740）：
    ```python
    if not self._confirm_save_before_close():
        event.ignore()
        return
    ```
  - 影响: 有未保存更改时点击关闭 → 保存确认对话框弹出前，Tangent Hub、tracking_service、files_panel（含 Flask 服务）、整条回放管线（走带/音频/时间线预览渲染/合成渲染线程）已被 shutdown，且 `_pipeline_shutdown_done = True`（1950-1952 行）使其不可恢复。用户在对话框中选择"取消"后窗口不关闭，但跟踪、Tangent 硬件、播放、预览渲染全部失效，只能重启应用。
  - 建议: 把 `_confirm_save_before_close()`（可取消路径）提前到 MainWindow.closeEvent 最前，或在 FileOpsMixin 确认通过后再回调 MainWindow 执行服务停止；至少为 `_shutdown_playback_pipeline` 提供"取消关闭后重新拉起"的恢复路径。
  - 置信度: 高

- [AUD-3501] [Major] UpdateChecker 为 QThread，closeEvent 关闭路径从不停止/等待它
  - 位置: src/app/main_window.py:744-750（创建与 start）；src/app/main_window.py:1903-1946（closeEvent 无对应清理）
  - 证据:
    ```python
    if self.config_manager.get("check_updates_on_startup"):
        self._update_checker = UpdateChecker(self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.no_update.connect(self._on_no_update)
        self._update_checker.error.connect(self._on_update_error)
        self._update_checker.start()
    ```
    交叉验证 src/app/update_checker.py:53 `class UpdateChecker(QThread):`，run() 内 `urllib.request.urlopen(req, timeout=10)`。
  - 影响: 启动后 10 秒内（网络请求挂起时最长 10s）关闭窗口，QThread 仍在运行。MainWindow.closeEvent 的 docstring（1904-1909 行）自己声明了该崩溃模式：macOS 上 QThread 在运行中被析构会 "QThread: Destroyed while thread is still running" → SIGSEGV/SIGABRT。回放管线按此修复了，update_checker 漏掉了。
  - 建议: 在 closeEvent 中对 `_update_checker` 做 `quit()/wait(超时)`（或 `isRunning()` 时 `wait()`）；与 tangent/tracking 同样的 try/except 块并列。
  - 置信度: 高

- [AUD-3502] [Major] script_engine 在后台 QThread 运行用户脚本，关闭路径未调用其 stop API
  - 位置: src/app/main_window.py:1283-1288（创建）；src/app/main_window.py:1704-1705（存在 stop API）；src/app/main_window.py:1903-1946 与 file_controller.py closeEvent（均无停止调用）
  - 证据:
    ```python
    from src.features.scripting.module import create_script_engine
    from src.app.ui.script_panel import ScriptPanel
    self.script_engine = create_script_engine(self.engine)
    ...
    self.script_panel.run_clicked.connect(self.script_engine.execute)
    self.script_panel.stop_clicked.connect(self.script_engine.stop)
    ```
    交叉验证 src/features/scripting/engine.py:175 "Executes user Python scripts in a background QThread."（engine.py:248 `self._thread = QThread()`）。
  - 影响: 用户脚本（如长循环）运行中关闭窗口：脚本线程继续持有 `self.engine` 引用并在退出阶段触碰引擎/Qt 对象；线程存活时被析构同样触发 docstring 描述的 QThread 析构崩溃。FileOpsMixin.closeEvent 停了 engine 的 worker 线程（file_controller.py:6889-6894），却没有停正在使用 engine 的脚本线程，存在清理顺序倒置。
  - 建议: closeEvent（或 FileOpsMixin.closeEvent 停 engine 之前）调用 `self.script_engine.stop()` 并等待线程结束。
  - 置信度: 高

- [AUD-3503] [Major] denoise/deblur 处理线程（DenoiseWorker 为 QThread）无关闭停止路径
  - 位置: src/app/main_window.py:1719-1724（信号连接显示其为异步引擎）；src/app/main_window.py:1903-1946（closeEvent 未处理）
  - 证据:
    ```python
    self.denoise_panel.engine.processing_finished.connect(self.on_denoise_finished)
    self.denoise_panel.engine.error_occurred.connect(self.on_denoise_error)
    self.deblur_panel.deblur_requested.connect(self.on_deblur_requested)
    ...
    self.deblur_panel.engine.processing_finished.connect(self.on_deblur_finished)
    self.deblur_panel.engine.error_occurred.connect(self.on_deblur_error)
    ```
    交叉验证 src/features/color/core/denoise_engine.py:70 `class DenoiseWorker(QThread):`；DenoiseEngine.apply_denoise 中 `self.worker = DenoiseWorker(...); self.worker.start()`。
  - 影响: 去噪/去模糊任务（AI ONNX 推理可长达数秒以上）进行中关闭窗口：worker 线程存活时对象析构 → 崩溃风险；任务完成后 `processing_finished` 信号还可能落到已销毁的 UI 槽。与已修复的 playback 崩溃同类。
  - 建议: closeEvent 中对 `denoise_panel.engine` / `deblur_panel.engine` 的当前 worker 做 stop/wait（DenoiseEngine 无 shutdown API，需要补一个或在引擎层加）。
  - 置信度: 中

- [AUD-3504] [Major] Library ↔ Files 面板 directory_changed 双向互连，Library 侧 set_root_path 无同路径守卫，存在目录重载死循环风险
  - 位置: src/app/main_window.py:1509-1512（交叉验证 src/app/ui/library_panel.py:539-546、src/app/ui/files_panel.py:104,306-314）
  - 证据:
    ```python
    self.files_panel.directory_changed.connect(self.library_panel.set_root_path)
    ...
    self.library_panel.directory_changed.connect(self.files_panel.set_root_path)
    ```
    library_panel.py:539-546（无 `if path == current: return` 之类的守卫）:
    ```python
    def set_root_path(self, path):
        if os.path.exists(path):
            self.list_view.setRootIndex(...)
            self.path_edit.setText(path); ...
            self.directory_changed.emit(path)
    ```
    files_panel.py:104 `self.bridge.directoryChanged.connect(self.directory_changed)`。
  - 影响: 环路：Library.set_root_path → directory_changed → Files.set_root_path → JS loadPath → 前端 bridge.directoryChanged 回发 → Files.directory_changed → Library.set_root_path → 再 emit……只要前端对程序化 loadPath 也回发 directoryChanged，两个面板就会无限互相触发目录重载（异步环，表现为 CPU 占用高、文件列表反复刷新，而非栈溢出）。
  - 建议: 在 main_window 的两个连接处或 set_root_path 内加"路径相同则不 emit/不转发"守卫（或引入 `_syncing` 状态锁），这是 .clinerules 信号防递归的标准要求。
  - 置信度: 中（环的最后一环取决于 file_explorer 前端 loadPath 是否回发 bridge.directoryChanged，需实测确认）

- [AUD-3505] [Suggestion] 类型注解引用未导入的名称 TimelinePreviewRenderer
  - 位置: src/app/main_window.py:429
  - 证据:
    ```python
    # Timeline Preview Renderer (P2.1)
    self.timeline_preview_renderer: Optional[TimelinePreviewRenderer] = None
    ```
    全文件及 import 区（1-98 行）无 `TimelinePreviewRenderer` 导入（grep 全仓库仅此一处引用该裸名）。
  - 影响: 因第 1 行 `from __future__ import annotations` 注解惰性求值，运行时不报错；但 mypy/IDE 会报 undefined name，且误导维护者以为此处有导入。
  - 建议: 补 `from src.features.edit... import TimelinePreviewRenderer`（仅类型用途可放 TYPE_CHECKING 块）或改为字符串注解。
  - 置信度: 高

- [AUD-3506] [Suggestion] 启动路径大量宽泛 `except Exception` 静默降级，吞掉配置/特性开关错误
  - 位置: src/app/main_window.py:504-508、586-590、598-602、615-619、727-731、754-759、798-815（部分）；典型如 615-619
  - 证据:
    ```python
    try:
        from src.shared.feature_flags import get_work_mode
        active_mode = get_work_mode(self.config_manager)
    except Exception:
        active_mode = "video"
    ```
    以及 504-508：
    ```python
    try:
        from src.shared.feature_flags import get_effective_layout_mode
        elm = get_effective_layout_mode(self.config_manager)
    except Exception:
        elm = "studio"
    ```
  - 影响: feature_flags 或 config 数据损坏引发的任何异常（不止 ImportError）都被静默吞掉，用户被错误地放进错误的工作模式/页面且无日志（部分分支连 logger 都没有），违反 .clinerules "不得宽泛 except 掩盖错误"。
  - 建议: 至少收窄为预期异常类型并 `logger.warning(..., exc_info=True)`；同一 try/except 的 get_effective_layout_mode 块在文件内重复 2 次、get_work_mode 块重复 3 次，可提取为一个 `_safe_work_mode(default)` 辅助函数。
  - 置信度: 高

- [AUD-3507] [Suggestion] GPU/引擎初始化失败无降级处理，__init__ 直接崩溃
  - 位置: src/app/main_window.py:385-386、525-526
  - 证据:
    ```python
    self.db = Database()
    self.engine = ImageEngine()
    self.engine.history.max_steps = self.config_manager.get("undo_history_limit") or 50
    ...
    if not self._test_mode:
        self.create_panels()
    ```
  - 影响: 与同文件对 Edit 模块的优雅降级（58-76 行 EDIT_MODULE_AVAILABLE 模式）形成对比：ImageEngine/Database 构造或 create_panels 抛异常（如 GPU 驱动不可用、模型目录缺失）时进程直接以 traceback 退出，没有"GPU 不可用→软件模式/错误对话框"路径。
  - 建议: 对 engine/关键面板创建增加顶层捕获，向用户展示明确错误（QMessageBox + 日志）后选择降级或安全退出，避免半初始化窗口。
  - 置信度: 中（ImageEngine 内部是否可能在此处抛异常取决于其实现，本批次未审）

- [AUD-3508] [Suggestion] 数值型配置一律用 `or` 兜底，显式 0 值被静默覆盖
  - 位置: src/app/main_window.py:386、392、1274-1277
  - 证据:
    ```python
    self.engine.history.max_steps = self.config_manager.get("undo_history_limit") or 50
    ...
    autosave_interval=self.config_manager.get("auto_save_interval") or 5,
    ...
    self.export_panel.set_default_export_settings(
        self.config_manager.get("default_export_format") or "JPEG",
        self.config_manager.get("default_export_quality") or 85
    )
    ```
  - 影响: 用户显式配置 `undo_history_limit: 0`（或导出质量 0、自动保存间隔 0）会被 `or` 替换为默认值 50/5/85。文件自己定义了 `_config_bool`（37-42 行）来区分 None 与 False，说明作者知道该坑，但数值配置未获得同等处理。
  - 建议: 用 `value if value is not None else default` 或仿照 `_config_bool` 增加 `_config_int/_config_str` 辅助函数。
  - 置信度: 高

- [AUD-3509] [Suggestion] _create_pages_from_registry 中途失败时，已成功创建的页面 widget 成为孤儿
  - 位置: src/app/main_window.py:1145-1152
  - 证据:
    ```python
    for page_desc in self.feature_registry.get_all_pages():
        try:
            page_widget = page_desc.create_page(ctx)
            created_pages[page_desc.page_id] = page_widget
            logger.debug(f"Created page '{page_desc.page_id}' from registry")
        except Exception as e:
            logger.error(f"Failed to create page '{page_desc.page_id}': {e}")
            return False
    ```
  - 影响: 假设 composite 创建成功而 deliver 失败：return False 后走 fallback 重建 Edit/Deliver/Composite，先前创建成功的 widget 未被引用保存也未 deleteLater，作为无 parent 的 QWidget 泄漏（其内部资源如信号、定时器随对象存活）。
  - 建议: 失败分支对 `created_pages` 中已创建的 widget 调用 `deleteLater()`，或在赋值到 self 属性后再判定失败。
  - 置信度: 高

- [AUD-3510] [Suggestion] __init__ 签名注解与 None 默认值不符
  - 位置: src/app/main_window.py:334-335
  - 证据:
    ```python
    def __init__(self, feature_flags: FeatureFlags = None, feature_registry: FeatureRegistry = None, test_mode: bool = False,
                 startup_project: str = None, startup_no_dialog: bool = False):
    ```
  - 影响: `feature_flags`、`feature_registry`、`startup_project` 默认 None 但注解为非 Optional，静态检查报错，与文件内其他处的 Optional 风格不一致。
  - 建议: 改为 `Optional[FeatureFlags] = None` 等（文件已导入 Optional）。
  - 置信度: 高

- [AUD-3511] [Suggestion] 快捷键处理中跨对象读取多个私有属性，破坏封装
  - 位置: src/app/main_window.py:981、1047、1068-1073
  - 证据:
    ```python
    bar.set_playing(page._comp_playing)
    ...
    bar.set_current_frame(page._composite_current_frame)
    ...
    start = getattr(controller, "_playback_range_start", None)
    controller.seek(start if start is not None else 0)
    elif key == Qt.Key.Key_End:
        end = getattr(controller, "_playback_range_end", None)
    ```
  - 影响: 主窗口依赖 CompositePage/播放控制器的内部属性名，任何内部重构都会静默破坏快捷键（getattr 默认 None 掩盖错误），且无法从公开接口获得类型保证。
  - 建议: 在 CompositePage/播放控制器上提供公开访问器（如 `comp_is_playing()`、`playback_range`），主窗口只走公开 API。
  - 置信度: 高

- [AUD-3512] [Suggestion] 启动期 singleShot 定时器在窗口关闭后仍会触发，操作半关闭 UI
  - 位置: src/app/main_window.py:681、693、735、737、741、756、764
  - 证据:
    ```python
    QTimer.singleShot(2500, self._composite_startup_fallback)
    ...
    QTimer.singleShot(2000, self._composite_startup_fallback)
    ...
    QTimer.singleShot(800, self._prompt_restore_queue)
    ...
    QTimer.singleShot(600, self._run_audio_backend_diagnostics)
    ```
  - 影响: 用户在启动后 0.6–2.5 秒内关闭窗口时，这些回调仍会执行：在已关闭的窗口上弹 Project Manager/Welcome/恢复队列对话框、执行 composite viewer 换绑、向已隐藏的 status_bar 加永久 widget，出现"幽灵对话框"或对半析构控件的 RuntimeError。
  - 建议: 回调开头统一检查 `self.isVisible()`/关闭标志（或用可 stop 的成员 QTimer 替代 singleShot 并在 closeEvent 停止）。
  - 置信度: 中

- [AUD-3513] [Suggestion] 音频后端诊断在 UI 线程同步执行，可能阻塞启动后的交互
  - 位置: src/app/main_window.py:764、798-806
  - 证据:
    ```python
    QTimer.singleShot(600, self._run_audio_backend_diagnostics)
    ...
    reset_audio_capabilities_cache()
    caps = get_audio_capabilities(check_output_device=True)
    self._audio_capabilities = caps
    ```
  - 影响: `get_audio_capabilities(check_output_device=True)` 在主线程同步探测 PyAV/SciPy/输出设备（首次探测还包含重模块导入），慢设备或冷缓存时 UI 冻结数百毫秒以上，违反"不得阻塞 UI 线程"。
  - 建议: 将探测移到后台线程，完成后经信号回主线程更新 `_audio_capabilities` 与状态栏。
  - 置信度: 中

- [AUD-3514] [Suggestion] _show_audio_backend_warning 的 caps 参数未使用；告警构建失败被静默吞掉
  - 位置: src/app/main_window.py:817-838
  - 证据:
    ```python
    def _show_audio_backend_warning(self, caps, disabled: list) -> None:
        try:
            ...
            self._audio_backend_warning_label = label
        except Exception:  # pragma: no cover
            pass
    ```
  - 影响: `caps` 参数从未被函数体引用（只用 disabled）；裸 `except Exception: pass` 使告警 UI 构建失败完全不可见，音频降级状态可能既无 widget 也无日志。
  - 建议: 删除 caps 参数或用其补充 tooltip 细节；except 分支至少 `logger.debug(..., exc_info=True)`。
  - 置信度: 高

### 通过项

- 关闭路径顺序设计（部分）：closeEvent 显式调用 `FileOpsMixin.closeEvent(self, event)` 修复 MRO 跳过导致的清理丢失（1941-1946），且 docstring 记录了动机；`_shutdown_playback_pipeline` 用 `_pipeline_shutdown_done` 做幂等（1950-1952）；GPU 渲染线程最终在 file_controller.closeEvent 中 `_stop_gpu_renderer_deferred` + `shutdown_worker_threads(timeout_ms=3000)` 停止并等待（交叉验证）。
- 线程安全（GL 初始化重入）：启动恢复文件的加载用 `Qt.ConnectionType.QueuedConnection` 连接 `gl_initialized`，避免在 initializeGL 内同步 load_image/load_video 重入（654-658，并有注释说明 DirectConnection 的危害）。
- 线程安全（Tangent 硬件）：TangentColorBridge 的 socket worker 不在硬件回调里直写渲染状态，而是复用 grading controller（1230-1234 注释与实现意图一致）；tracking_service 的任务信号经默认 AutoConnection 队列回主线程（1649-1653）。
- 信号防递归（部分）：标题栏更新有 `_updating_titlebars` 标志 + 50ms 防抖定时器（456-460）；`crop_rotation_changed → crop_panel.set_params` 路径在面板侧有 `blockSignals` 守卫（交叉验证 crop_panel.py:487-489）；mask overlay 的 viewport_menu → mask_overlay_act 单向同步在值相等时 Qt 不再发射 toggled，不构成环（544-546 + menu_toolbar_controller.py:850-855 交叉验证）。
- 输入验证：自定义缓存目录经 `os.path.isdir` 校验后才应用（472-474）；启动恢复文件经 `os.path.exists` 校验（606）；恢复的 current_page 在对应 feature 禁用时回退 Color 并记录 warning（514-523）。
- 可选模块降级：Edit 模块与 UltimatePlayer 的导入失败均被捕获为可用性标志而非崩溃（58-76、90-98），Composite/Deliver/Edit 可用性三重判定（481-499、1390-1399）。
- 启动副作用控制：`sys.path.append`（而非 insert(0)）避免 file_explorer 通用模块名遮蔽，有详细注释论证（82-88）。
- 工具函数 `_config_bool` 正确区分 None 与显式 False（37-42），show_tooltips 判断用 `is False` 与之配合（365）。
