# batch-028 — src/app/controllers// [P1]

任务书：docs/audit/2026-09-28/assignments/batch-028.md（编号区间 AUD-2800–AUD-2899）

审查范围：`src/app/controllers/file_controller.py`（7,313 行，全文通读）。

---

## src/app/controllers/file_controller.py (行数: 7313)

### 发现

- [AUD-2800] [Major] 用户在关闭确认中选择「Discard」后，closeEvent 仍无条件把脏状态写入工程文件
  - 位置: src/app/controllers/file_controller.py:6738-6740, 6861, 6911-6916
  - 证据:
    ```python
        if not self._confirm_save_before_close():
            event.ignore()
            return
    ```
    （Discard 分支在 `_confirm_save_before_close` 中 `return True` 放行关闭）随后：
    ```python
        self._persist_active_project_before_close()
    ```
    ```python
        try:
            self.save_router.save()
        except Exception as exc:  # noqa: BLE001
            # Shutdown continues so resources and a valid recovery file can
            # still be handled deterministically; the failure remains logged.
            logger.exception("closeEvent: project save failed: %s", exc)
    ```
  - 影响: `_persist_active_project_before_close` 调用的是与 Ctrl+S 完全相同的 `save_router.save()`（注释明确"所有保存都写入当前 .topos 工程包"）。用户明确选择「不保存并关闭」后，内存中的未保存修改仍被持久化到主工程文件，「Discard」按钮语义失效；若用户随后依赖"上一次保存的版本"，会得到被覆盖的内容。
  - 建议: 在 `_confirm_save_before_close` 返回 Discard 时设置标志（如 `self._user_discarded_changes = True`），`_persist_active_project_before_close` 据此跳过主文件保存；如目的是崩溃恢复，应写入独立 recovery/autosave 快照而非主工程路径。
  - 置信度: 高（代码路径确定；仅"是否故意作为退出兜底"存在设计意图歧义）

- [AUD-2801] [Major] dropEvent 对拖入的视频/任意文件一律调用 load_image，绕过 _open_media_path 路由
  - 位置: src/app/controllers/file_controller.py:6924-6927（对照 574-593）
  - 证据:
    ```python
    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        if files:
            self.load_image(files[0])
    ```
    而菜单打开走的是：
    ```python
    def _open_media_path(self, path: str) -> None:
        """Open media, reusing its own project when one already exists."""
        if _is_video_file(path):
            ...
            self.load_video(path)
            return
    ```
  - 影响: 拖入 mp4/mov 等视频文件时进入图像加载路径：`FileIO.load_image` 解不出帧 → `full is None` → 弹出误导性的"找不到文件"错误框（文件明明存在），或误入离线缓存分支；拖入非媒体文件（文件夹、txt）同样报"找不到文件"。多文件拖入时静默忽略除第一个外的全部文件。
  - 建议: `dropEvent` 改为 `self._open_media_path(files[0])` 复用视频/图像/工程路由；对不可识别类型给出明确提示。
  - 置信度: 高

- [AUD-2802] [Major] DeckLink 监视输出路径在 UI 线程同步 makeCurrent + glReadPixels/glGenBuffers 等 GL 操作，且从不 doneCurrent
  - 位置: src/app/controllers/file_controller.py:2896-3017（PBO 回读）, 3149-3264（直接回读）, 3026-3071（PBO/FBO 分配）, 3320-3342（viewport 抓取）
  - 证据:
    ```python
        gl_state = None
        try:
            gl_viewport.makeCurrent()
            from OpenGL import GL as gl

            gl_state = self._capture_gl_readback_state(gl)
    ```
    ```python
            gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, pbo)
            gl.glReadPixels(
                0,
                0,
                width,
                height,
                gl.GL_RGBA,
                gl.GL_UNSIGNED_BYTE,
                ctypes.c_void_p(0),
            )
    ```
    收尾只恢复绑定、从不释放上下文：
    ```python
        finally:
            try:
                from OpenGL import GL as gl

                self._restore_gl_readback_state(gl, gl_state)
            except Exception:
                pass
    ```
  - 影响: 这些方法由 `_on_color_monitor_frame_ready` / `_on_color_output_monitor_ready` 等渲染信号槽在 UI 线程逐帧触发（监视输出启用时），与铁律"UI 线程不得操作 OpenGL"冲突：与渲染线程共享的 context 做 makeCurrent 会与 QOpenGLWidget 的 paintGL 竞争；4K 帧的直接 `glReadPixels`（direct fallback 首帧必走）在 UI 线程同步回读可造成明显卡顿；makeCurrent 后未 doneCurrent 破坏 Qt 的 context current 簿记。已做 FBO/PBO 绑定保存恢复说明作者意识到风险，但未消除线程归属问题。
  - 建议: 将监视输出回读迁移到拥有 GL context 的渲染侧（engine/渲染线程内做 PBO 回读后以 `MonitorRGBAFrame` 投递），UI 线程只消费；至少在 finally 中补 `gl_viewport.doneCurrent()`。
  - 置信度: 中（机制与代码确定；实际影响受"DeckLink 监视输出启用"配置门控，默认关闭）

- [AUD-2803] [Major] _SourceViewerDecodeWorker 异常路径泄漏已解码的 GPU 帧
  - 位置: src/app/controllers/file_controller.py:167-184
  - 证据:
    ```python
                task = edit_facade.create_edit_render_task(
                    frame=frame,
                    generation_id=generation,
                    layers=[layer_data],
                    ...
                )
                if not self._renderer.submit(task):
                    safe_release_gpu_frame(decoded_frame)
                    self.signals.cpu_fallback_requested.emit(int(frame))
            except Exception as e:
                logger.debug(f"SourceViewerDecodeWorker error: {e}")
                self.signals.cpu_fallback_requested.emit(int(frame))
    ```
  - 影响: `safe_release_gpu_frame(decoded_frame)` 只在两条路径调用：`frame_type == 'cpu'`（144 行）与 `submit()` 返回 False（180 行）。若 `create_edit_render_task`、`layer_data` 构造或 `submit()` 本身抛异常（如 IOSurface/VT 对象在搬运中失效），已解码帧（可能持有 CVPixelBuffer/IOSurface/GPU 纹理引用）既不释放也不提交，异常被降为 debug 日志——长时间源监视播放中反复触发会累积泄漏 GPU/IOSurface 资源，最终表现为解码或渲染失败。
  - 建议: 在 except 分支中，若 `decoded_frame` 已产出且尚未成功提交，调用 `safe_release_gpu_frame` 释放（可在 try 内先置 `decoded_frame = None`，提交成功后清引用，except 中对非 None 引用释放）。
  - 置信度: 高

- [AUD-2804] [Major] load_video 在尝试加载前即重置全部调色面板，失败后旧视频会话状态被清空且无恢复
  - 位置: src/app/controllers/file_controller.py:4710-4729, 4785-4787
  - 证据:
    ```python
        # 重置所有调色相关面板的参数（防止旧视频的调色应用到新视频）
        self.grading_panel.reset_all()
        if hasattr(self.hsl_curves_panel, 'reset_all_curves'):
            self.hsl_curves_panel.reset_all_curves()
        ...
        self.current_file_path = path
        self.current_file_is_video = True
        # 切换到新视频时清除 save_router 的路径缓存（同 load_image）。
        self.save_router.clear_cache()
    ```
    失败分支：
    ```python
        else:
            self.current_file_path = None  # 加载失败，清除路径
            self.status_bar.showMessage(f"加载视频失败: {os.path.basename(path)}", 5000)
    ```
  - 影响: 面板 reset 与 `save_router.clear_cache()`、`current_file_path = path` 都发生在 `video_controller.load_video(path)` 尝试之前。新视频加载失败（损坏文件/解码器错误）后：旧视频的 `current_file_path` 被清空、路径缓存被清、面板已被重置，旧视频会话实际上被"卸载"，未保存的面板级调整丢失，且没有任何恢复逻辑或回滚；错误路径下状态不一致（`current_file_is_video` 仍为 True）。
  - 建议: 将面板重置与 save_router 缓存清理移到 `load_video(path)` 成功之后；失败分支保留旧会话状态（不清 `current_file_path`）并将 `current_file_is_video` 复位。
  - 置信度: 中（代码路径确定；实际丢失幅度取决于 autosave 落盘时机）

- [AUD-2805] [Major] _ensure_image_project 无确认地用新图像工程替换打开中的时间线工程，未保存的 timeline 编辑可能丢失
  - 位置: src/app/controllers/file_controller.py:541-549, 666-667
  - 证据:
    ```python
        project = getattr(self, "_current_edit_project", None)
        asset = project.get_asset_by_path(path) if project is not None else None
        if project is None or project.timelines or asset is None:
            from src.features.edit.core.project import EditProject

            project = EditProject.create_image_project(
                name=os.path.splitext(os.path.basename(path))[0] or "Untitled Project",
                image_path=path,
            )
            self._current_edit_project = project
            self._current_timeline_sequences = {}
    ```
    load_image 仅在 `current_file_path` 非空时保存：
    ```python
        if self.current_file_path:
            self.save_router.save()
    ```
  - 影响: 在视频模式下打开一个无相邻 .topos 的图像（open_file 对话框含图像过滤器；或拖拽，见 AUD-2801）时，若当前时间线工程自建立后从未把素材加载进 Color（`current_file_path` 为 None），未保存的时间线剪辑/工程修改既不触发保存也不弹关闭确认，直接被新图像工程覆盖，`_current_timeline_sequences` 清空——仅剩 autosave 恢复快照兜底。
  - 建议: `_ensure_image_project` 在 `project.timelines` 非空（即替换的是异类工程）时先走 `close_project()` 的保存确认流程；load_image 的旧工程保存不应以 `current_file_path` 为唯一条件，还应检查 `edit_project_dirty`。
  - 置信度: 中（覆盖路径确定；`current_file_path` 为 None 的前置状态依赖具体操作序列）

- [AUD-2806] [Suggestion] _perform_periodic_save 在 QTimer 回调中于 UI 线程同步执行全量保存
  - 位置: src/app/controllers/file_controller.py:972-975
  - 证据:
    ```python
    def _perform_periodic_save(self):
        """Periodic full-save callback (separate from 2s debounce autosave)."""
        if (self.current_file_path or self._current_edit_project_filepath) and self.engine:
            self.save_router.save()
    ```
  - 影响: 周期性定时器静默触发同步 `save()`（.topos 打包写盘）。大工程（多素材 + 时间线 + 合成图）在用户操作中任意时刻都可能被磁盘写入阻塞 UI；与之对照 `_perform_auto_backup` 用的是 `backup_project_async`。
  - 建议: 周期保存改走异步（复用 backup_project_async 的线程池模式），或至少在保存前检查 `edit_project_dirty` 跳过无变化周期。
  - 置信度: 中

- [AUD-2807] [Suggestion] unload_video 的断连清单与 _connect_video_control_bar 不对称（漏掉 loop_toggled 与 Composite 侧桥接）
  - 位置: src/app/controllers/file_controller.py:6664-6692（对照 4856-4915）
  - 证据:
    ```python
                self.canvas.video_control_bar.scrub_finished.disconnect(self.on_video_scrub_finished)
                self.canvas.video_control_bar.play_toggled.disconnect(self.on_video_play_toggled)
                self.canvas.video_control_bar.next_frame.disconnect(self.on_video_next_frame)
                self.canvas.video_control_bar.prev_frame.disconnect(self.on_video_prev_frame)
                self.canvas.video_control_bar.first_frame.disconnect(self.on_video_first_frame)
                self.canvas.video_control_bar.last_frame.disconnect(self.on_video_last_frame)
    ```
    而连接侧还包括：
    ```python
            (bar.loop_toggled, self.on_video_loop_toggled),
            ...
            (comp_bar.mark_in_requested, self.on_composite_mark_in),
            (comp_bar.mark_out_requested, self.on_composite_mark_out),
            (comp_bar.clear_in_out_requested, self.on_composite_clear_in_out),
            ...
            (self.video_controller.frame_changed, self._on_video_frame_changed_for_composite),
            (self.video_controller.play_state_changed, self._on_video_play_state_for_composite),
    ```
  - 影响: 卸载后 `loop_toggled`、Composite 控制栏的 mark in/out 以及 `video_controller → Composite` 桥接仍保持连接。当前因控制栏 `hide_for_image()` 隐藏而休眠，但任何程序化 emit（或后续代码改动）都会在已清空的状态上触发处理器，属于潜在的双重行为/递归隐患。
  - 建议: 用与 `_connect_video_control_bar` 相同的连接元组表驱动断连（单一事实来源），确保两侧永远对称。
  - 置信度: 高

- [AUD-2808] [Suggestion] 大量跨模块私有 API 访问（save_router._get_project_path_for_file、edit_project_io._autosave_filepath、composite_page._node_graph 等）
  - 位置: src/app/controllers/file_controller.py:473-475, 586, 676, 775, 812-815, 4075, 6598
  - 证据:
    ```python
        default_path = current_path or self.save_router._get_project_path_for_file(
            self.current_file_path
        )
    ```
    ```python
        if load_filepath != original_filepath:
            self.edit_project_io._autosave_filepath = load_filepath
    ```
    ```python
                            restored_graph = NodeGraph.from_dict(composite_graph_data)
                            self.composite_page._node_graph = restored_graph
                            self.composite_page._node_editor.build_from_graph(restored_graph)
                            if hasattr(self.composite_page, '_undo_stack'):
                                self.composite_page._undo_stack.clear()
    ```
  - 影响: 控制器直接改写 SaveRouter/EditProjectIO/CompositePage 的私有字段，绕过它们的封装不变量（例如直接赋值 `_autosave_filepath` 可能与 IO 内部状态失同步；直接替换 `_node_graph` 绕过页面的 setter 逻辑）。模块边界漂移，重构任一侧都可能静默破坏。
  - 建议: 为 SaveRouter 提供 `get_project_path_for(file)`、为 EditProjectIO 提供 `adopt_recovery_path()`、为 CompositePage 提供 `restore_graph(graph)` 公共接口，控制器只走公共面。
  - 置信度: 高

- [AUD-2809] [Suggestion] _restore_edit_ui_state 对工程文件中的 view_mode 字符串直接枚举索引，未验证可抛 KeyError
  - 位置: src/app/controllers/file_controller.py:4155-4158
  - 证据:
    ```python
            else:
                mode = ViewMode[ui_state.view_mode.upper()] if isinstance(ui_state.view_mode, str) else ui_state.view_mode
                self.media_pool_panel.set_view_mode(mode)
    ```
  - 影响: `ui_state.view_mode` 来自反序列化的 .topos 工程（外部数据）。try 块只包住 import；手工编辑、旧 schema 或未来枚举值会令 `ViewMode[...]` 抛 KeyError，在 `_load_edit_project`（4099 行调用、无 try）中途中断加载——此时 `_current_edit_project` 等状态已赋值，窗口停留在半初始化状态。
  - 建议: 用 `ViewMode.__members__.get(...)` + 回退默认值，并把整个 restore 包进 try/except 记日志，保证加载路径完整。
  - 置信度: 高（机制确定，触发概率低）

- [AUD-2810] [Suggestion] RAW 扩展名判定含 .cr3，但打开对话框过滤器未包含 *.cr3
  - 位置: src/app/controllers/file_controller.py:569（对照 872）
  - 证据:
    ```python
        filters = f"All Supported (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.arw *.cr2 *.nef *.dng *.orf *.raf *.psd *.exr *.hdr{video_formats});;..."
    ```
    ```python
        is_raw = os.path.splitext(path)[1].lower() in ['.arw', '.cr2', '.cr3', '.nef', '.dng', '.orf', '.raf']
    ```
  - 影响: CR3（佳能新格式）被按 RAW 处理但无法从文件对话框直接选择，只能切到 "All Files" 打开；用户会以为不支持。
  - 建议: 在两处过滤器中补 `*.cr3`，或改为从单一常量列表生成过滤器与 RAW 判定，消除双份清单。
  - 置信度: 高

- [AUD-2811] [Suggestion] 未使用的导入（sys、uuid、QGraphicsScene、MaskShape、MaskGroup、Rational）
  - 位置: src/app/controllers/file_controller.py:7, 10, 21, 24, 216
  - 证据:
    ```python
    import sys
    import uuid
    ...
    from PyQt6.QtWidgets import (QFileDialog, QMessageBox, QInputDialog,
                                 QGraphicsScene, QDialog)
    from src.features.color.core.layer import Layer, MaskShape, MaskGroup
    ...
        from src.shared.media.metadata import Rational
    ```
  - 影响: 全文检索无任何使用点（`Layer` 有使用，其余没有）。增加启动导入开销与阅读噪音；`Rational` 仅在 try/except 分支赋值后闲置。
  - 建议: 删除未用导入；如 `Rational` 是给外部消费者的 re-export，显式写入 `__all__` 并加注释。
  - 置信度: 高

- [AUD-2812] [Suggestion] 大量硬编码中文状态消息绕过 I18N.tr
  - 位置: src/app/controllers/file_controller.py:4365, 4372-4375, 4382-4385, 4389-4392, 4542, 4595, 4599, 4768, 6486, 6631-6634
  - 证据:
    ```python
                        self.status_bar.showMessage(
                            f"项目已创建，请手动切换到 Edit 页面",
                            10000
                        )
    ```
    ```python
                self.status_bar.showMessage(
                    f"已加载视频: {os.path.basename(path)} ({info['width']}x{info['height']}, {info['fps']:.2f}fps, {info['total_frames']}帧)",
                    5000
                )
    ```
  - 影响: Quick Project、播放器、视频加载/失败、复合页求值等路径的用户可见文案为硬编码中文，英文/其他语言环境下与其余 I18N 文案混杂，违反项目国际化约束。
  - 建议: 统一改为 `I18N.tr(key, default).format(...)`（tr 已支持 format kwargs，见 src/shared/i18n/i18n.py:132）。
  - 置信度: 高

- [AUD-2813] [Suggestion] _init_gpu_pipeline 在 GPU 渲染线程 start() 之后、装配完成之前发生异常时泄漏已启动线程
  - 位置: src/app/controllers/file_controller.py:2191-2216
  - 证据:
    ```python
            # Start the GPU render thread
            gpu_renderer.start()

            # Pass to TimelinePreviewRenderer (which also connects frame_ready signal)
            self.timeline_preview_renderer.set_gpu_renderer(
                gpu_decode_manager=gpu_decode_manager,
                gpu_renderer=gpu_renderer,
            )
    ```
    ```python
        except Exception as e:
            logger.warning(f"GPU pipeline initialization failed, using CPU fallback: {e}")
            # CPU path continues to work — no action needed
    ```
  - 影响: 若 `set_gpu_renderer` / `set_comp_gl_context` / 后续装配抛异常，已 start 的渲染线程没有任何引用（仅 Qt parent 保命不被 GC abort），也无人负责 stop——进程内多出一条持续运行的 GL 线程，closeEvent 也不会停它（它不在 `_source_gpu_renderer`/`timeline_preview_renderer` 的关停清单里，除非装配已成功）。
  - 建议: 把局部 `gpu_renderer` 先存到 `self`（如 `self._program_gpu_renderer`），except 分支中对已 start 的渲染器调用 `_stop_gpu_renderer_deferred`，成功路径再移交所有权。
  - 置信度: 中

- [AUD-2814] [Suggestion] 可选特性 payload 收集/恢复大量 `except Exception: logger.debug(...)` 吞错，默认不可见
  - 位置: src/app/controllers/file_controller.py:1089-1090, 1270-1272, 1324-1325, 1351-1353, 1388-1389
  - 证据:
    ```python
        except Exception:  # noqa: BLE001
            logger.debug("Teaching Animation payload collection failed", exc_info=True)
            return None
    ```
    ```python
        except Exception:  # noqa: BLE001
            logger.debug("AI Workflow payload restore failed", exc_info=True)
    ```
  - 影响: 这些路径决定"教学动画/AI 工作流数据是否随工程保存/恢复"。真实 bug（schema 漂移、序列化异常）只留 debug 级日志，默认日志级别下完全不可见——用户会发现场景/工作流图悄悄不保存或不恢复而无从排查。宽泛 except 掩盖领域错误与铁律相悖（虽已带 exc_info）。
  - 建议: 至少升为 `logger.warning`（带一次性去重/节流），或将"特性存在但序列化失败"与"特性不存在（ImportError）"区分处理。
  - 置信度: 中

- [AUD-2815] [Suggestion] _frame_to_timecode 未防御 fps<=0（来自外部工程数据）导致除零
  - 位置: src/app/controllers/file_controller.py:3729-3746（调用点 2510-2513）
  - 证据:
    ```python
        total_seconds = frame / fps
        hours = int(total_seconds // 3600)
        ...
        frames = int(frame % fps)
    ```
    调用：
    ```python
        if self._current_edit_project and hasattr(self._current_edit_project, 'fps'):
            fps = self._current_edit_project.fps
            timecode = self._frame_to_timecode(frame, fps)
    ```
  - 影响: `fps` 来自 .topos 工程文件反序列化结果，畸形数据（0/None 数值）会在 `frame_ready` 信号槽里抛 ZeroDivisionError/TypeError，中断该帧的 timecode 更新链路。
  - 建议: 函数入口 `fps = float(fps) or 1.0` 钳底，或调用处验证 `fps > 0`。
  - 置信度: 低（触发需畸形工程文件；机制确定）

- [AUD-2816] [Suggestion] 多个公共处理器与回调缺少类型注解
  - 位置: src/app/controllers/file_controller.py:564 (`open_file`), 896 (`save_file`), 981 (`update_canvas`), 1074 (`perform_idle_render`), 4534 (`play_video_in_panel`), 6463 (`on_video_frame_ready`), 6638 (`unload_video`), 6703 (`closeEvent`), 6918/6924/6929（drag/drop/resize）等
  - 证据:
    ```python
    def open_file(self):
        last_path = (self.config_manager.get("default_open_directory")
    ```
    ```python
    def unload_video(self, *, save_before_unload: bool = True):
        """卸载当前视频, optionally saving the active project first."""
    ```
  - 影响: 与 `.clinerules` 的强类型约束不符；`load_image` 的 `path` 参数同样无注解（`create_standalone_session`/`project_path` 有），混用降低可读性与 IDE 检查覆盖。
  - 建议: 按现有带注解方法（如 `load_video`、`open_project`）的风格补齐参数/返回注解。
  - 置信度: 高

- [AUD-2817] [Suggestion] _on_video_frame_changed_for_composite 每次图求值打 INFO 级日志（最高约 30fps）
  - 位置: src/app/controllers/file_controller.py:6552-6557
  - 证据:
    ```python
        eval_start = _time.monotonic()
        has_ctx = self.composite_page._runtime_context is not None
        _logger.info(
            f"[COMP-PLAY] eval graph: frame={frame}, has_ctx={has_ctx}, "
            f"renderer={self.composite_page._renderer is not None}"
        )
    ```
  - 影响: 播放期间按自适应节流（10–30fps）持续输出 INFO 日志，生产环境产生大量日志噪音与格式化开销；且该函数每次调用重新 `getLogger` 而非复用模块级 `logger`。
  - 建议: 降为 `logger.debug` 或加节流；直接使用模块级 `logger`。
  - 置信度: 高

### 通过项
- 线程安全（后台解码/元数据加载）：`_MetadataWorker` 经 QRunnable + 队列信号回 UI 线程；`_SourceViewerDecodeWorker` 的 latest-wins 合并、`stop()` 带 2s join 超时且避免自 join，均正确。
- GPU 资源生命周期（退出路径）：closeEvent 先断信号再停线程（decode worker → source GPU renderer → timeline preview renderer → color timeline preview），`_stop_gpu_renderer_deferred` 对超时线程挂入 `_LEAKED_GPU_OBJECTS` 推迟销毁，避免 QThread 存活期析构 Abort；`_ensure_color_monitor_pbos` 重建前先删旧 PBO/FBO。
- 信号防递归：程序化更新控制栏走 `VideoControlBar.on_frame_changed`/`set_current_frame`，内部 `blockSignals` 保护（src/features/color/ui/canvas.py:2876-2880）；`_connect_video_control_bar` 每次先 disconnect-再 connect，避免叠加连接；`_maybe_preadvance_same_source_clip` 等跨 clip 推进用 `_cross_clip_advancing` 闩锁防重入。
- 状态提交一致性（时间线缓存）：`_sync_asset_references_after_project_change` 的删除快照/undo 恢复（含批次 token 去重消费）、`_sync_timeline_sequences_after_project_change` 的快照回填、`_get_timeline_sequences` 对活动编辑器快照的兜底，逻辑核对无越权修改。
- GLSL/HDR 正确性：本文件无 GLSL 代码；HDR float 帧仅在监视器输出（SDR 目标）路径做 8bit 量化，理由成立；bake 恢复链路 cv2 操作保持 dtype，无无理由 clamp。
- 异常与取消路径（导入/导出）：FCPXML/OTIO/字幕导出全路径有异常捕获并给用户对话框反馈（7168-7180, 7284-7297）；autosave 选择"No"时显式 `discard_autosave` 防反复弹窗。
- 输入验证（工程打开）：`_read_project_mode_from_manifest` 容错返回 None、`_check_project_matches_work_mode` 工作模式门控、`_load_standalone_project` 在改状态前先完成校验。
- 泄漏防御（播放器）：`play_video_in_panel` 持久复用三引擎实例（[CRASH-5] 注释），`stop_other_players` 互斥停机，避免每次播放新建泄漏。
- 编辑项目 IO 状态：`_load_edit_project` 恢复 autosave 后保留原始路径为活路径、绑定 MainWindow 级 IO 管理器（Quick Project 一次性写入后 `set_current_filepath`），恢复链完整。
