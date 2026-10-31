# batch-093 — src/features/composite（P1）

- 审查范围：`src/features/composite/runtime.py`（240 行，已通读）
- 编号区间：AUD-9300–AUD-9399
- 结论摘要：0 Critical / 2 Major / 4 Suggestion

## src/features/composite/runtime.py (行数: 240)

### 发现

- [AUD-9300] [Major] 六处 `except Exception` 以 debug 级别吞掉可选面板的构建失败，违反“不得宽泛 except 掩盖领域错误”
  - 位置: src/features/composite/runtime.py:107（同型模式见 120、134、151、165、180）
  - 证据:
    ```python
    try:
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        self.keyframes_panel = KeyframesPanel(parent)
        ...
    except ImportError:
        logger.debug("KeyframesPanel unavailable, skipping")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("KeyframesPanel creation failed: %s", exc)
    ```
  - 影响: ImportError 分支是合理的特性探测，但 `except Exception` 把构造函数内的真实缺陷（TypeError、状态初始化失败等）也静默吞掉，且仅记 debug——默认 INFO 级别下完全不可见，面板悄无声息缺失，无从排查。同一模式覆盖 Keyframes/Spline/ImportReport/AssetDependency/SceneOutliner/Material 六个面板（另 185 行 `except Exception` 同理）。
  - 建议: 非导入类异常至少 `logger.warning(..., exc_info=True)`；或收窄捕获类型，将“模块缺失”与“构建失败”区分处理，后者应上抛或显著告警。
  - 置信度: 高

- [AUD-9301] [Major] cleanup() 只委托 page.cleanup()，运行时自建面板未被清理，SplinePanel 播放定时器可在页面拆卸后继续触发
  - 位置: src/features/composite/runtime.py:237-240（关联 src/features/composite/ui/spline_panel.py:198-199、540-544）
  - 证据:
    ```python
    def cleanup(self) -> None:
        """Clean up composite resources."""
        if self.page:
            self.page.cleanup()
    ```
    而 SplinePanel 持有 `self._play_timer = QTimer(self)`（spline_panel.py:198，`timeout` 连接 `_on_internal_play_tick`），该 tick 会 `seek_to_frame(next_frame)`（540-544 行）；page.cleanup() 只显式 dispose 了 keyframes_panel（composite_page.py:1894-1900）。
  - 影响: 若退出/清理时 Spline 播放处于运行态，`_play_timer` 未被任何清理路径停止，将继续以设定帧率触发 seek，向渲染器已停止（composite_page.py:1901-1909）的页面持续发信号；scope/perf/material 等其余自建面板同样无停用钩子，清理职责不完整。
  - 建议: `CompositeRuntime.cleanup()` 遍历自建面板调用 `dispose()/stop()`（可为 SplinePanel 增加 `dispose()` 停止 `_play_timer`），或由 page.cleanup() 统一处理所有已注入面板。
  - 置信度: 中（触发条件为拆卸时播放中；面板挂靠主窗口，最终随窗口销毁，风险窗口在 cleanup 之后到窗口销毁之间）

- [AUD-9302] [Suggestion] create() 不可重入且异常路径留下半初始化状态，与“失败返回 False”的契约不符
  - 位置: src/features/composite/runtime.py:74-86
  - 证据:
    ```python
    if existing_page is not None:
        self.page = existing_page
    else:
        self.page = CompositePage(parent=parent)
    self.viewer = CompositeViewer(parent=parent)
    ...
    self.page.set_viewer(self.viewer)
    self.page.properties_panel = self.properties_panel
    self._available = True
    ```
    （无 `if self._available` 重入守卫；构造序列未包异常处理）
  - 影响: 若 74-85 行之间抛出非导入异常，`page/viewer/scope_panel/properties_panel` 已赋值而 `_available` 仍为 False；调用方（dock_layout_controller.py:846 仅处理 False 与 ImportError，945 行）既拿不到 False 也无清理，留下孤儿控件；再次调用 create() 会重复创建 viewer 与全部面板。
  - 建议: 入口加幂等守卫（已可用直接返回 True）；构造序列包 try/except，失败时回滚已建控件并统一返回 False。
  - 置信度: 高（代码事实确凿，触发为条件性）

- [AUD-9303] [Suggestion] 通过私有属性向页面注入面板，绕过公共 API
  - 位置: src/features/composite/runtime.py:91、104、117、130、148、162、176
  - 证据:
    ```python
    self.page._perf_panel = self.perf_panel
    ...
    if hasattr(self.page, 'set_keyframes_panel'):
        self.page.set_keyframes_panel(self.keyframes_panel)
    else:
        self.page._keyframes_panel = self.keyframes_panel
    ```
  - 影响: 直接写页面私有字段耦合内部实现；页面侧重命名字段后注入静默失效（hasattr 回退进一步掩盖问题）。
  - 建议: 为 CompositePage 补齐 `set_perf_panel` 等公共 setter（set_viewer/set_spline_panel 等已存在，composite_page.py:3117-8502），运行时只走 setter 并在缺失时告警。
  - 置信度: 高

- [AUD-9304] [Suggestion] 类型注解薄弱：字段全为 Any、Dict 未参数化、on_enter/load_composite_data 参数无注解
  - 位置: src/features/composite/runtime.py:24-45、199、219、231
  - 证据:
    ```python
    self.page: Any = None
    self.viewer: Any = None
    ...
    def get_graph_data(self) -> Optional[Dict]:
    ...
    def on_enter(self, color_texture_id=None, color_width=0, color_height=0, color_revision=0) -> None:
    ...
    def load_composite_data(self, data: dict) -> None:
    ```
  - 影响: 作为 MainWindow 与 Composite 的解耦边界，接口契约全靠 Any 传递，静态检查无法发现页面接口不匹配（如 update_color_output 签名漂移）。
  - 建议: TYPE_CHECKING 块引入 CompositePage/CompositeViewer 等具体类型；`Optional[Dict[str, Any]]`；为 on_enter 四个参数与 data 补注解。
  - 置信度: 高

- [AUD-9305] [Suggestion] 类文档声称由 CompositeModule.create_runtime() 创建，实际该路径零调用且一旦启用会重复建页
  - 位置: src/features/composite/runtime.py:19-20（关联 src/features/composite/module.py:193-210）
  - 证据:
    ```python
    Created by CompositeModule.create_runtime() and consumed by
    MainWindow through MainWindowAdapter.
    ```
    而 module.py:206 调 `runtime.create(main_window)` 未传 existing_page（即使 register_pages 已建 `self._page`）；全库 grep 显示 create_runtime 无任何调用点，实际创建路径是 MainWindowAdapter.__init__（main_window_adapter.py:38，dock_layout_controller.py:845-846 实例化）。
  - 影响: 文档误导维护者；死路径本身逻辑与 adapter 的去重逻辑（复用 `composite_module._page`）不一致，若被启用会创建第二个 CompositePage/CompositeViewer。
  - 建议: 修正 docstring 为 MainWindowAdapter 路径；module.create_runtime 要么复用 `self._page` 要么删除。
  - 置信度: 高

### 通过项

- 线程安全：所有入口（on_color_output/on_enter/on_leave）均由 UI 线程 Qt 信号链调用（main_window.py:1483 → layer_controller.py:1220 → main_window_adapter.py:261/305），本文件不跨线程共享可变状态。
- GPU 资源生命周期：仅透传 texture_id/width/height/revision 整数句柄，不持有、不释放 GL 对象，无 UI 线程 OpenGL 调用。
- GLSL·HDR 正确性：文件无着色器代码，不适用。
- 信号防递归：本文件不连接任何信号（连线在 adapter._wire_signals），无递归风险。
- 状态提交一致性（保存路径）：get_graph_data 先 `save_ui_state()` 再 `to_dict()`；`save_ui_state` 存在（node_editor_widget.py:4992），`node_graph` 属性存在（composite_page.py:6199），且 203 行对 node_editor 有真值守卫，序列化路径正确。
- 输入验证：existing_page 取自模块内部状态并由 hasattr/属性访问约束，data 委托页面侧处理，未见外部未验证数据直接触发危险操作。
- 性能模式：所有面板导入均为函数内惰性导入，符合启动性能预期，无热路径重复导入。
