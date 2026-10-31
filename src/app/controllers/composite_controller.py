"""CompositeControllerMixin - extracted from MainWindow.

Contains methods related to composite frame handling, node editor,
composite page switching, and composite graph/state data retrieval.

职责（Responsibilities）:
    - 合成页（Composite/Fusion 页）控制层：Edit → Composite 带上下文
      切换（PageContext / TimelineClipFrameProvider 装配）、合成图数据
      与 EditProject 的双向同步（``_sync_composite_graph_to_edit_project``）、
      Comp Clip 身份元数据恢复与激活记忆、节点选择 → Node Properties
      面板联动，以及 Output 节点 / 菜单的渲染输出（同步图片渲染与
      后台视频/序列渲染、进度条点击取消）。
    - 本 Mixin 不持有状态模型：所有 ``self.*`` 属性引用解析到宿主
      MainWindow 实例（composite_page、composite_viewer、
      _current_edit_project 等）。

与引擎/页面的边界（Boundary）:
    - 图求值与渲染归 composite feature（facade / CompositePage 内部
      renderer）与 ``engine``：本层只负责把 graph_data 在页面与
      EditProject 之间搬运/合并，经 ``timeline_preview_renderer`` 失效
      缓存后触发预览刷新，不直接操作 OpenGL。
    - 节点编辑 UI 归 CompositePage/NodeGraph：本层通过
      ``load_composite_data``/``get_composite_data``/``set_node`` 等
      页面 API 读写，不触碰节点内部结构。

信号协议（Signal protocol）:
    - 入站：合成渲染器的 frame_ready/metadata_ready/status 信号、
      节点编辑器选中信号、合成图变更信号（graph_changed → 同步到
      工程并失效 comp_clip 帧缓存）等，在 MainWindow 中连接到本
      Mixin 的 ``_on_*`` 槽。
    - 出站：无自定义 Qt 信号；经 ``switch_to_page(_with_context)``、
      ``request_render``、状态栏消息和 exporter 回调驱动 UI。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import replace

logger = logging.getLogger(__name__)

# AUD-2407: 单帧导出"保持场景线性"由格式能力表驱动，而非单点
# ``ext == "exr"`` 字符串比较。当前渲染对话框（composite_render_dialog
# 的 _IMAGE_FORMATS：png/jpeg/tiff/exr/webp）中仅 EXR 走浮点线性写盘
# （TIFF 在 FileIO.save_image 与本控制器的 dtype 表里都是 uint16 显示
# 编码）。新增浮点 HDR 格式（hdr、toos-float 档等）时必须在此登记，
# 否则场景线性数据会被错误地做 sRGB 显示编码后存成浮点文件（同类
# 先例：FileIO.toos_export_keeps_linear 的 .toos 修复）。
_LINEAR_IMAGE_FORMATS = frozenset({"exr"})


def _make_render_cancel_filter(on_cancel):
    """Progress-bar click-to-cancel filter (PyQt is imported lazily here)."""
    from PyQt6.QtCore import QEvent, QObject

    class _CancelFilter(QObject):
        """事件过滤器：进度条鼠标释放时触发取消回调。"""

        def eventFilter(self, obj, event) -> bool:
            """鼠标释放事件 → 调用 on_cancel 并吞掉该事件。"""
            if event.type() == QEvent.Type.MouseButtonRelease:
                on_cancel()
                return True
            return False

    return _CancelFilter()


def _make_render_callback_bridge():
    """Qt 信号桥：把 exporter 回调封送回 UI 线程执行（AUD-2401）。

    ``CompositeRenderExporter.export()`` 接收的是裸 Python 回调而非 Qt
    信号。本桥把回调绑定到 Qt 信号上：发射方与桥不在同一线程时
    AutoConnection 自动退化为 QueuedConnection，UI 侧槽恒在 UI 线程
    执行——跨线程安全性不再依赖 exporter 的内部实现细节。
    （PyQt 与 _make_render_cancel_filter 一致按需惰性导入。）
    """
    from PyQt6.QtCore import QObject, pyqtSignal

    class _RenderCallbackBridge(QObject):
        """progress/finished/error 三路信号（参数以值拷贝跨线程）。"""

        progress = pyqtSignal(int, int)
        finished = pyqtSignal(str)
        error = pyqtSignal(str)

    return _RenderCallbackBridge()


class CompositeControllerMixin:
    """Mixin providing composite controller methods.

    Intended to be mixed into MainWindow so that all ``self.*`` attribute
    references resolve against the MainWindow instance.
    """

    def _prepare_composite_data_for_open(
        self, composite_id: str, composite_data: object
    ) -> object:
        """Restore identity metadata before loading a Media Pool graph.

        Older project files stored the graph under ``compositions[graph_id]``
        but did not repeat ``composite_id`` inside the graph dictionary.  The
        node data can still be loaded, but CompositePage then cannot mark the
        workspace active and shows the empty-state overlay over the graph.
        Comp Clip metadata is the authoritative fallback for the remaining
        identity fields as well.
        """
        if not isinstance(composite_data, dict):
            return composite_data

        needs_identity = not composite_data.get("composite_id")
        project = getattr(self, "_current_edit_project", None)
        comp_clip = None
        comp_asset = None
        if project is not None:
            try:
                comp_clip = project.get_comp_clip_by_graph_id(composite_id)
            except (AttributeError, TypeError, ValueError):
                comp_clip = None
            for asset in getattr(project, "assets", {}).values():
                metadata = getattr(asset, "metadata", {}) or {}
                if (
                    getattr(asset, "is_comp_clip", False)
                    and metadata.get("graph_id") == composite_id
                ):
                    comp_asset = asset
                    break

        needs_identity = needs_identity or any(
            not composite_data.get(key)
            for key in ("comp_clip_id", "comp_asset_id", "source_kind")
        )
        if not needs_identity:
            return composite_data

        prepared = dict(composite_data)
        prepared.setdefault("composite_id", composite_id)
        canonical_comp_clip_id = getattr(comp_clip, "comp_clip_id", None)
        if comp_clip is not None:
            # The dictionary key is the canonical ID for old projects where
            # CompClipData.create_empty() generated a second internal ID.
            for key, value in getattr(project, "comp_clips", {}).items():
                if value is comp_clip:
                    canonical_comp_clip_id = key
                    break
                if isinstance(value, dict) and value.get("graph_id") == composite_id:
                    canonical_comp_clip_id = key
                    break
        asset_metadata = getattr(comp_asset, "metadata", {}) or {}
        fallback_fields = {
            "comp_clip_id": (
                canonical_comp_clip_id
                or asset_metadata.get("comp_clip_id")
            ),
            "comp_asset_id": (
                getattr(comp_clip, "asset_id", None)
                or getattr(comp_asset, "asset_id", None)
            ),
            "source_kind": getattr(comp_clip, "source_kind", None),
            "fps": getattr(comp_clip, "fps", None),
            "duration_frames": getattr(comp_clip, "duration_frames", None),
        }
        if not fallback_fields["source_kind"]:
            fallback_fields["source_kind"] = asset_metadata.get("source_kind")
        for key, value in fallback_fields.items():
            if value is not None and not prepared.get(key):
                prepared[key] = value
        return prepared

    # ------------------------------------------------------------------
    # Composite frame / metadata / status callbacks
    # ------------------------------------------------------------------

    def _on_composite_frame_ready(self, request_id: int, tex_id: int, width: int, height: int) -> None:
        """Handle composite frame ready — update CompositeViewer."""
        if self._composite_adapter:
            self._composite_adapter.handle_frame_ready(request_id, tex_id, width, height)
        elif self.current_page == "Composite" and self.composite_viewer:
            # R-7: Prefer shared texture path — no sync readback on UI thread.
            if tex_id > 0:
                self.composite_viewer.set_texture(tex_id, width, height)
            elif self.composite_page:
                # Fallback: try async preview image
                preview_img = getattr(self.composite_page, 'get_preview_image', lambda: None)()
                if preview_img is not None:
                    self.composite_viewer.set_image(preview_img)
                else:
                    # No sync readback — submit async readback to render thread instead.
                    # Sync readback blocks UI thread and causes page switch stutter.
                    if hasattr(self.composite_page, '_renderer') and self.composite_page._renderer:
                        from src.features.composite import facade as composite_facade
                        CompositeCommand = composite_facade.get_composite_command_class()
                        if CompositeCommand is not None:
                            self.composite_page._renderer.submit(
                                CompositeCommand.READBACK_PRESENT_ASYNC,
                                {"request_id": request_id},
                            )
                    else:
                        logger.debug(
                            "Composite frame ready but no texture/preview available (request_id=%s)",
                            request_id,
                        )

    def _on_composite_metadata_ready(self, meta: dict) -> None:
        """Handle composite metadata — forward format/bbox to viewer overlay."""
        if self._composite_adapter:
            self._composite_adapter.handle_metadata_ready(meta)
            return

        if not self.composite_viewer or not self._composite_available:
            return

        # Use composite facade to avoid deep import from core.format
        from src.features.composite.facade import build_overlay_objects
        fmt, bbox = build_overlay_objects(meta)
        fmt_data = meta.get("format")
        bbox_data = meta.get("bbox")
        if fmt or bbox:
            self.composite_viewer.set_overlay_data(
                image_format=fmt if fmt_data else {},
                bbox=bbox if bbox_data else {},
            )

    def _on_composite_status_message(self, message: str) -> None:
        """Handle composite status message — show in status bar."""
        self.status_bar.showMessage(message, 5000)  # Show for 5 seconds

    # ------------------------------------------------------------------
    # Comp Clip promotion on first user edit
    # ------------------------------------------------------------------

    def _on_composite_graph_changed(self, composite_id: str, graph_data: dict) -> None:
        """Persist live Composite graph edits into the in-memory EditProject."""
        self._sync_composite_graph_to_edit_project(composite_id, graph_data)
        # 合成图变化时清除 comp_clip 帧缓存，否则循环播放会命中旧帧（内容/色彩跳变）。
        if hasattr(self, "timeline_preview_renderer") and self.timeline_preview_renderer:
            try:
                self.timeline_preview_renderer.invalidate_comp_clip_cache(composite_id)
            except Exception:
                # AUD-2404: 缓存失效失败会让"循环播放命中旧帧"的 bug 无诊断地
                # 复现，必须留下 warning 级痕迹（含堆栈），不允许裸 pass 吞错。
                logger.warning(
                    "[CompClip] Failed to invalidate comp-clip frame cache for %s "
                    "(looped playback may hit stale frames)",
                    composite_id, exc_info=True,
                )

    def _flush_composite_to_edit_before_leave(self) -> None:
        """Force-persist the active Composite graph before returning to Edit.

        Most graph edits emit signals while they happen, but a plain page-tab
        switch (Composite -> Edit) must not depend on those signals having fired
        in the right order.  This method is the last-chance bridge: serialize the
        live graph, promote a temporary timeline composition if it was edited,
        and mark the Edit preview for refresh.
        """
        page = getattr(self, "composite_page", None)
        if page is None:
            return

        composite_id = (
            getattr(page, "_current_composite_id", "")
            or getattr(self, "_current_composite_id", "")
        )
        if not composite_id:
            return

        graph_data = None
        try:
            graph_data = page.get_composite_data()
        except Exception as e:
            # AUD-2404: 序列化失败意味着用户合成图编辑无法在切页前持久化
            # （数据丢失级后果），必须 warning 级日志 + 状态栏告知用户，
            # 不允许 debug 级记录后静默 return。
            logger.warning(
                "[CompClip] Cannot serialize composite %s on leave: %s",
                composite_id, e, exc_info=True)
            if hasattr(self, "status_bar"):
                from src.shared.i18n.i18n import I18N
                self.status_bar.showMessage(
                    I18N.tr(
                        "composite.leave_flush.serialize_failed",
                        "合成图序列化失败，本次编辑未能同步到工程",
                    ),
                    5000,
                )

        if not graph_data:
            return

        if self._current_edit_project:
            self._sync_composite_graph_to_edit_project(composite_id, graph_data)
            self._remember_active_comp_clip(composite_id)
        else:
            # Standalone image (no EditProject): persist the composite graph to
            # the image's own .topos project before leaving, so the in-memory
            # graph isn't lost when the next image is loaded.
            try:
                self.save_router._save_from_composite()
            except Exception as e:
                logger.warning(f"[Composite] standalone composite save on leave failed: {e}")

        # R-4/R-5: Legacy temporary composition promotion disabled.
        # Comp Clips must be created explicitly via Create Comp Clip.
        # The old _pending_comp_clip_context lazy promote path is removed.

    def _on_composite_user_edit_started(self, composite_id: str) -> None:
        """
        Handle the first user edit in a composite session.

        When the user makes a real edit (add/remove/connect nodes, change
        parameters) in a temporary composition opened from a timeline clip,
        this handler promotes the composition to a proper Comp Clip in the
        media pool.

        Promotion happens at most once per composite session.
        """
        logger.info(f"[CompClip] User edit detected for composite: {composite_id}")

        if not self._current_edit_project:
            logger.warning("No edit project for comp clip promotion")
            return

        # (a) Check if current composite is already a comp_clip -- skip
        comp_data = self._current_edit_project.get_composition_by_id(composite_id)
        if comp_data is None:
            logger.warning(f"Composition not found for promotion: {composite_id}")
            return

        # Check if already promoted (has comp_clip_id set)
        from src.features.edit import facade as edit_facade
        if isinstance(comp_data, dict):
            comp_graph = edit_facade.parse_composite_graph_data(comp_data)
        else:
            comp_graph = comp_data

        if comp_graph.comp_clip_id is not None:
            logger.debug(f"[CompClip] Already promoted: {composite_id}")
            return

        # R-4/R-5: Legacy lazy promotion disabled.
        # Comp Clips must be created explicitly. Return early.
        logger.warning(
            f"[CompClip] Skipping legacy auto-promotion for {composite_id}. "
            "Use explicit Create Comp Clip instead."
        )
        return

    def _sync_composite_graph_to_edit_project(
        self,
        composite_id: str,
        graph_data: dict | None = None,
    ) -> None:
        """Merge the live NodeGraph payload into ``EditProject.compositions``.

        ``CompositePage`` serializes only graph/UI fields, while the project
        composition record also owns source clip, asset, comp clip, and format
        metadata.  Merge instead of replacing so Edit playback/export can still
        resolve the Comp Clip source correctly.
        """
        if not composite_id or not self._current_edit_project:
            return
        if graph_data is None and self.composite_page:
            graph_data = self.composite_page.get_composite_data()
        if not graph_data:
            return

        existing = self._current_edit_project.compositions.get(composite_id)
        if existing is None:
            logger.debug(f"[CompClip] Cannot sync graph; composition missing: {composite_id}")
            return

        # U-01/U-11：与 SaveRouter 共用同一 merge 契约（含 layers/
        # layer_stacks），避免两处白名单漂移导致图层编辑不落盘。
        from src.shared.project.composition_merge import merge_composition_record
        merged = merge_composition_record(existing, graph_data, composite_id)

        # 只比较业务字段；modified_at 由 merge 自动刷新，不参与 dirty 判定。
        comparison_keys = (
            set(merged.keys()) | set(existing.keys())) - {"modified_at"}
        if all(merged.get(key) == existing.get(key)
               for key in comparison_keys):
            return

        new_compositions = dict(self._current_edit_project.compositions)
        if new_compositions.get(composite_id) == merged:
            return
        new_compositions[composite_id] = merged
        self._current_edit_project = replace(
            self._current_edit_project,
            compositions=new_compositions,
            modified_at=time.time(),
        )
        self.edit_project_dirty = True
        self._refresh_edit_preview_after_composite_change()

    def _remember_active_comp_clip(self, composite_id: str) -> None:
        """Persist the currently open composition as ``active_comp_clip_id``.

        Resolves the Comp Clip backing the live graph and records it on the
        EditProject so reopening the project can restore the composition the
        user had open when leaving/closing the app.
        """
        if not composite_id or not self._current_edit_project:
            return
        try:
            cc = self._current_edit_project.get_comp_clip_by_graph_id(composite_id)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # AUD-2404: 工程数据损坏（comp_clips 字段非法）导致解析失败时
            # 收窄异常类型并提升到 warning（原为 Exception + debug 静默），
            # 与 _prepare_composite_data_for_open 的异常面保持一致。
            logger.warning(
                "[CompClip] Cannot resolve comp clip for %s: %s",
                composite_id, exc)
            return
        comp_clip_id = getattr(cc, "comp_clip_id", "") if cc is not None else ""
        if not comp_clip_id:
            return
        if self._current_edit_project.active_comp_clip_id == comp_clip_id:
            return
        try:
            self._current_edit_project = self._current_edit_project.set_active_comp_clip(
                comp_clip_id)
        except Exception as exc:
            logger.warning(f"[CompClip] Failed to remember active comp clip: {exc}")
            return
        self.edit_project_dirty = True

    def _refresh_edit_preview_after_composite_change(self) -> None:
        """Refresh Edit-side project references and invalidate preview cache."""
        if getattr(self, "current_page", None) != "Edit":
            self._edit_preview_needs_composite_refresh = True
            return

        current_timeline = None
        try:
            if hasattr(self, "edit_page") and self.edit_page:
                current_timeline = getattr(self.edit_page, "_current_timeline", None)
            if current_timeline is None and self._current_edit_project:
                active_tid = getattr(self._current_edit_project, "active_timeline_id", None)
                if active_tid and hasattr(self, "_current_timeline_sequences"):
                    current_timeline = self._current_timeline_sequences.get(active_tid)
        except Exception as e:
            logger.debug(f"[CompClip] Resolve current timeline failed: {e}")

        try:
            if hasattr(self, "timeline_preview_renderer") and self.timeline_preview_renderer:
                self.timeline_preview_renderer.set_project(self._current_edit_project)
                if current_timeline is not None:
                    self.timeline_preview_renderer.set_sequence(current_timeline)
                if hasattr(self, "_request_current_timeline_preview_frame"):
                    self._request_current_timeline_preview_frame()
        except Exception as e:
            logger.debug(f"[CompClip] Timeline preview refresh failed: {e}")

        try:
            if hasattr(self, "timeline_panel") and self.timeline_panel:
                self.timeline_panel.set_project(self._current_edit_project)
                timeline_view = self.timeline_panel.get_timeline_view()
                if current_timeline is not None and timeline_view:
                    timeline_view.load_timeline(current_timeline)
        except Exception as e:
            logger.debug(f"[CompClip] Timeline panel project refresh failed: {e}")

        self._edit_preview_needs_composite_refresh = False

    # ------------------------------------------------------------------
    # Node editor
    # ------------------------------------------------------------------

    def _on_node_editor_selected(self, node_id: str) -> None:
        """Handle node selection from the node editor — update Properties panel."""
        if not self._composite_available or not self.composite_page:
            return
        node = self.composite_page.node_graph.get_node(node_id)
        if not node:
            return

        # Get parameter options and definitions from node's PARAM_DEFS
        param_options = {}
        param_defs = {}
        if hasattr(node, 'get_param_options'):
            param_options = node.get_param_options()
        if hasattr(node, 'get_param_defs'):
            param_defs = node.get_param_defs()

        self._node_properties_panel.set_node(
            node_id=node.id,
            node_type=node.node_type,
            node_title=node.node_title,
            # AUD-2410: 传副本而非节点实时 dict。面板侧 set_nodes 本就会
            # ``dict(parameters)`` 拷贝（且 _block_signals 防递归锁在
            # AUD-10702 已建），这里再拷一层是本层的契约保障：即使面板
            # 实现变化，编辑面板也绝不会就地改动节点状态、绕过命令路径。
            parameters=dict(node.parameters),
            param_options=param_options,
            param_defs=param_defs,
            enabled=not getattr(node, 'disabled', False),
            node_color=getattr(node, 'header_color', '#666666'),
        )

        # Auto-raise Composite's own Node Properties dock so it's visible.
        # (Previously this raised Edit's shared dock_properties, which does
        # not exist in Image mode.)
        dock_np = getattr(self, 'dock_node_properties', None)
        if dock_np is not None:
            if not dock_np.isVisible():
                dock_np.show()
            dock_np.raise_()

    # ------------------------------------------------------------------
    # Enter composite with context
    # ------------------------------------------------------------------

    def _handle_enter_composite_with_context(self, context) -> None:
        """
        处理进入 Composite 页面的上下文逻辑。

        功能：
        1. 如果来自 Edit 且有 composite_id，加载对应合成
        2. 如果无 composite_id，可选创建新合成
        3. 将 Color 输出作为合成输入

        Args:
            context: PageContext 实例
        """
        # Import here to avoid circular dependency at module level
        from src.app.main_window import PageType

        if context.source_page != PageType.EDIT:
            return

        if self.composite_page and hasattr(self.composite_page, "set_edit_project"):
            self.composite_page.set_edit_project(getattr(self, "_current_edit_project", None))

        clip_id = context.clip_id
        if not clip_id:
            logger.warning("Edit → Composite switch without clip_id")
            return

        # P9.2: 从 EditProject 加载合成数据
        composite_id = context.composite_id
        color_stack_id = context.color_stack_id
        clip = None

        # 如果 context 没有提供 composite_id，尝试从 clip 获取
        if not composite_id and clip_id and self.edit_page is not None and self.edit_page._current_timeline:
            timeline = self.edit_page._current_timeline
            for track in timeline.all_tracks:
                c = track.get_clip_by_id(clip_id)
                if c:
                    clip = c
                    break

            if clip:
                composite_id = clip.composite_id
                color_stack_id = color_stack_id or getattr(clip, 'color_stack_id', None)

        # 如果有 composite_id，从 EditProject 加载合成数据到 CompositePage
        # 注意：页面已经通过 switch_to_page_with_context 切换，无需再次切换
        if composite_id:
            # Set _current_composite_id so SaveRouter can use it
            self._current_composite_id = composite_id

        loaded_composite_data = False
        if composite_id and self._current_edit_project:
            composite_data = self._current_edit_project.get_composition_by_id(composite_id)
            if composite_data and self.composite_page:
                composite_data = self._prepare_composite_data_for_open(
                    composite_id, composite_data)
                self.composite_page.load_composite_data(composite_data)
                loaded_composite_data = True
                # Always ensure Topos Color node exists in the graph
                self.composite_page.ensure_topos_color_node(color_stack_id or "")
                logger.info(f"[P9.2] Loaded composite {composite_id} for clip {clip_id}")
            elif not composite_data:
                logger.warning(f"Composite data not found: {composite_id}")

        if not loaded_composite_data:
            logger.debug(f"No composite_id for clip: {clip_id}")
            if self.composite_page:
                # A timeline clip without a Comp Clip is not an active
                # composition.  Keep the page available, but leave its node
                # workspace in the disabled empty state until a composition is
                # explicitly created or opened.
                if not composite_id and hasattr(
                    self.composite_page, "set_composition_active"
                ):
                    self.composite_page.set_composition_active(False)
                if not self.composite_page.has_timeline_clip_input():
                    self.composite_page.switch_to_timeline_input_graph(color_stack_id or "")
                else:
                    # Always ensure Topos Color node exists in the graph
                    self.composite_page.ensure_topos_color_node(color_stack_id or "")

        # 确保 Color 输出有效，作为 Composite 的输入
        # 仅当图中包含旧版 COLOR 节点时才需要 ColorOutput；
        # ToposColor 图直接读取 ColorStackData，不依赖 ColorOutput。
        if self.composite_page and self.composite_page.requires_color_output_input():
            if not self.engine.color_output.is_valid:
                self.engine.request_color_output()

        # Set up timeline clip context for TimelineClipInputNode
        self._setup_timeline_clip_context(context, composite_id, clip_id)

    # ------------------------------------------------------------------
    # Timeline clip context setup
    # ------------------------------------------------------------------

    def _setup_timeline_clip_context(self, context, composite_id: str,
                                      clip_id: str) -> None:
        """Set up TimelineClipFrameProvider and runtime context on CompositePage.

        Called during Edit -> Composite page transition so that
        TimelineClipInputNode can access the correct frame data
        during graph evaluation.

        Args:
            context: PageContext with source page info.
            composite_id: The composite graph ID (may be empty).
            clip_id: The timeline clip ID.
        """
        if not self.composite_page:
            return

        # Build CompositeRuntimeContext
        from src.features.composite import facade as composite_facade
        timeline_id = context.timeline_id or ""
        timeline_frame = context.timeline_frame or 0
        source_frame = context.source_frame or 0
        clip_local_frame = context.clip_local_frame or 0
        asset_id = context.asset_id
        fps = 24.0
        width = 1920
        height = 1080
        graph_data = None
        if composite_id and self._current_edit_project:
            graph_data = self._current_edit_project.get_composition_by_id(
                composite_id
            )
            if isinstance(graph_data, dict):
                # AUD-2406: 工程文件来源的数值字段走 _safe_int——旧版/手改
                # 工程的 "3840px" 之类字符串会让裸 int() 在页面切换路径抛
                # ValueError。
                width = self._safe_int(graph_data.get("width"), width, minimum=1)
                height = self._safe_int(graph_data.get("height"), height, minimum=1)

        # Resolve timeline and clip info
        timeline = None
        if timeline_id and hasattr(self, '_current_timeline_sequences'):
            timeline = self._current_timeline_sequences.get(timeline_id)
        if timeline is None and self.edit_page and self.edit_page._current_timeline:
            timeline = self.edit_page._current_timeline
            timeline_id = timeline_id or getattr(timeline, 'timeline_id', '')

        if timeline is not None:
            raw_fps = getattr(timeline, "fps", 24.0) or 24.0
            if hasattr(raw_fps, "to_fps"):
                raw_fps = raw_fps.to_fps()
            elif hasattr(raw_fps, "to_float"):
                raw_fps = raw_fps.to_float()
            elif hasattr(raw_fps, "num") and hasattr(raw_fps, "den"):
                raw_fps = raw_fps.num / (raw_fps.den or 1)
            fps = float(raw_fps)
            width = self._safe_int(getattr(timeline, "width", None), width, minimum=1)
            height = self._safe_int(getattr(timeline, "height", None), height, minimum=1)

            for track in timeline.all_tracks:
                c = track.get_clip_by_id(clip_id)
                if c:
                    asset_id = asset_id or getattr(c, 'asset_id', None)
                    if not context.source_frame:
                        source_frame = getattr(c, 'source_start', 0)
                    # ClipInstance does not own the composition canvas; the
                    # Edit timeline is the authoritative resolution.
                    break

        # clip 的实际时长和起始位置（用于 transport bar 帧数显示和全局帧↔本地帧映射）。
        # duration 优先取 graph_data，回退到 clip；timeline_start 始终从 clip 查（graph_data 不存此值）。
        clip_duration = 0
        clip_timeline_start = 0
        if timeline is not None:
            for track in timeline.all_tracks:
                c = track.get_clip_by_id(clip_id)
                if c:
                    # The current Edit clip may have been trimmed after the
                    # composite graph was created. Its live segment length,
                    # not stale graph metadata, drives playback/keyframes.
                    clip_duration = getattr(c, 'timeline_duration', 0) or 0
                    clip_timeline_start = getattr(c, 'timeline_start', 0) or 0
                    break
        if clip_duration <= 0 and isinstance(graph_data, dict):
            clip_duration = self._safe_int(graph_data.get('duration_frames'), 0)

        runtime_ctx = composite_facade.make_composite_runtime_context(
            project_id=getattr(self, '_current_project_id', ''),
            graph_id=composite_id or '',
            timeline_id=timeline_id,
            clip_id=clip_id,
            timeline_frame=timeline_frame,
            clip_local_frame=clip_local_frame,
            source_frame=source_frame,
            asset_id=asset_id,
            fps=fps,
            width=width,
            height=height,
            comp_clip_id=composite_id,
            duration_frames=clip_duration,
            clip_timeline_start=clip_timeline_start,
        )

        # Create or reuse TimelineClipFrameProvider
        from src.features.edit import facade as edit_facade
        if not hasattr(self, '_timeline_clip_frame_provider') or \
           self._timeline_clip_frame_provider is None:
            self._timeline_clip_frame_provider = edit_facade.create_timeline_clip_frame_provider()
        provider = self._timeline_clip_frame_provider

        # Wire provider dependencies
        provider.set_engine(self.engine)
        provider.set_allow_external_gpu_frames(False)
        # 跳转异步解码（持帧预览）完成回调：复用合成页 Batch H 的帧就绪
        # 信号（跨线程队列连接回 UI 线程 → 16ms 防抖重求值）。
        frame_ready_signal = getattr(self.composite_page, '_video_node_frame_ready', None)
        if frame_ready_signal is not None:
            provider.set_frame_ready_callback(frame_ready_signal.emit)
        if self._current_edit_project:
            provider.set_project(self._current_edit_project)
        if hasattr(self, '_current_timeline_sequences'):
            provider.set_timeline_sequences(self._current_timeline_sequences)

        # Pass to composite page
        self.composite_page.set_timeline_clip_context(
            runtime_context=runtime_ctx,
            gpu_provider=provider,
        )

        logger.info(
            f"[CompController] Set timeline clip context: "
            f"clip={clip_id[:8]}, composite={composite_id[:8] if composite_id else 'N/A'}"
        )

    # ------------------------------------------------------------------
    # Switch to composite page
    # ------------------------------------------------------------------

    def _switch_to_composite_page(self, composite_id: str) -> None:
        """
        Switch to Composite page and load specified composite graph (P9.2).

        Args:
            composite_id: Composite graph ID to load
        """
        logger.info(f"[MainWindow P9.2] Switching to Composite page: {composite_id}")

        if not self._composite_available:
            logger.warning("Composite module not available")
            return
        if not self._current_edit_project:
            logger.warning("No active project for Composite page switch")
            return

        # Get composite data
        composite_data = self._current_edit_project.get_composition_by_id(composite_id)
        if composite_data is None:
            logger.warning(f"Composite not found: {composite_id}")
            return

        composite_data = self._prepare_composite_data_for_open(
            composite_id, composite_data)

        # Remember this composition so reopening the project restores it.
        self._remember_active_comp_clip(composite_id)

        # Timeline-backed Comp Clips must enter through PageContext so
        # TimelineClipInput resolves the graph's own clip instead of whatever
        # clip happens to be selected in the Edit timeline.
        context = self._context_for_composite_data(composite_id, composite_data)
        if context is not None:
            self._current_composite_id = composite_id
            self.switch_to_page_with_context(context)
            return

        # Empty/non-timeline Comp Clips have no timeline input context. Suppress
        # the Composite enter hook's selected-clip auto-bind for this one switch.
        self._current_composite_id = composite_id
        self._suppress_composite_auto_bind_once = True

        # Switch to Composite page
        self.switch_to_page("Composite")

        # The suppress flag must not outlive this switch: when already on
        # the Composite page switch_to_page early-returns without running
        # the enter hook, and a stale True would block workspace restore
        # on the next tab return.
        self._suppress_composite_auto_bind_once = False

        # Load composite data into CompositePage
        if self.composite_page:
            if hasattr(self.composite_page, "set_timeline_clip_context"):
                self.composite_page.set_timeline_clip_context(None, None)
            self.composite_page.load_composite_data(composite_data)
            # load_composite_data sets _is_comp_clip / _composite_duration_frames,
            # which is_comp_clip_playback_active() depends on. The enter hook ran
            # before this load (during switch_to_page), so re-configure the comp-
            # clip transport controls now that the metadata is available — this
            # shows the bottom bar with I/O range controls for the new comp clip.
            try:
                if hasattr(self, "_configure_composite_comp_clip_controls"):
                    self._configure_composite_comp_clip_controls()
            except Exception as exc:
                logger.warning(f"_switch_to_composite_page: comp-clip controls setup failed: {exc}")

        logger.info(f"[P9.2] Switched to Composite page with {composite_id}")

    def _context_for_composite_data(self, composite_id: str, composite_data) -> object | None:
        """Build an Edit -> Composite PageContext for a timeline-backed graph."""
        if not self._current_edit_project:
            return None

        try:
            from src.features.edit import facade as edit_facade

            graph = (
                edit_facade.parse_composite_graph_data(composite_data)
                if isinstance(composite_data, dict)
                else composite_data
            )
        except Exception as exc:
            logger.warning(f"[Composite] Cannot parse graph context: {exc}")
            return None

        timeline_id = getattr(graph, "timeline_id", None) or ""
        clip_id = getattr(graph, "clip_id", None) or getattr(graph, "source_clip_id", None) or ""
        if not timeline_id or not clip_id:
            return None

        timeline = None
        if hasattr(self, "_current_timeline_sequences"):
            timeline = self._current_timeline_sequences.get(timeline_id)
        if timeline is None and self.edit_page is not None:
            candidate = getattr(self.edit_page, "_current_timeline", None)
            if getattr(candidate, "timeline_id", None) == timeline_id:
                timeline = candidate
        if timeline is None:
            logger.warning(f"[Composite] Cannot build context; timeline not loaded: {timeline_id}")
            return None

        clip = None
        for track in timeline.all_tracks:
            clip = track.get_clip_by_id(clip_id)
            if clip is not None:
                break
        if clip is None:
            logger.warning(f"[Composite] Cannot build context; clip not found: {clip_id}")
            return None

        try:
            timeline_frame = self.timeline_panel.get_playhead_frame()
        except (AttributeError, TypeError):
            timeline_frame = clip.timeline_start
        if timeline_frame < clip.timeline_start or timeline_frame >= clip.timeline_end:
            timeline_frame = clip.timeline_start

        try:
            source_frame = clip.timeline_to_source_frame(timeline_frame)
        except (ValueError, AttributeError):
            source_frame = getattr(clip, "source_start", 0)
        clip_local_frame = timeline_frame - clip.timeline_start

        return self._page_context_manager.create_context_for_edit_to_composite(
            clip_id=clip.clip_id,
            composite_id=composite_id,
            asset_id=clip.asset_id,
            timeline_id=timeline_id,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
            color_stack_id=getattr(clip, "color_stack_id", None),
        )

    # ------------------------------------------------------------------
    # Composite graph / state data retrieval
    # ------------------------------------------------------------------

    def _get_composite_graph_data(self):
        """Get serialized composite graph data if available."""
        if self._composite_adapter:
            return self._composite_adapter._get_graph_data()
        if self._composite_available and self.composite_page:
            # U-11：使用页面规范序列化入口（含 output target 与
            # fps/duration），而不是 graph-only 的 to_dict()。
            get_data = getattr(self.composite_page, "get_composite_data", None)
            if callable(get_data):
                return get_data()
            if hasattr(self.composite_page, 'node_editor') and self.composite_page.node_editor:
                self.composite_page.node_editor.save_ui_state()
            return self.composite_page.node_graph.to_dict()
        return None

    def _get_composite_state_data(self):
        """Get serialized composite viewer state if available."""
        if self._composite_adapter:
            return self._composite_adapter._get_viewer_state()
        if self._composite_available and self.composite_viewer:
            return self.composite_viewer.get_state()
        return None

    # ------------------------------------------------------------------
    # Render Output (Output node "Render" button → export to disk)
    # ------------------------------------------------------------------

    def _collect_available_render_passes(self, graph_data: dict) -> set[str]:
        """Render passes offered by the dialog: metadata AOVs + scene3d extras.

        AUD-2405: ``getattr(page, "_last_metadata", {}).get(...)`` 的默认值
        只在属性缺失时生效；属性存在但为 None（未收到首帧元数据的常见
        初始态/重置态）时会在 None 上取属性抛 AttributeError。先做
        None 归一再读 aov。
        """
        available: set[str] = {"beauty"}
        last_meta = getattr(self.composite_page, "_last_metadata", None) or {}
        last_aovs = last_meta.get("aov", {})
        if isinstance(last_aovs, dict):
            available.update(str(name) for name in last_aovs)
        if any(isinstance(node, dict) and node.get("type") == "scene3d"
               for node in (graph_data.get("nodes") or [])):
            available.update({"depth", "object_id"})
        return available

    def _on_composite_render_output(self, node_id: str) -> None:
        # M19.2：三页统一工程色彩上下文——Composite 渲染输出前确保
        # 工程级 color.* 覆盖层已注入（与 Edit 加载路径同一入口），使
        # descriptor_from_config 三页读出同一工作/显示配置。
        try:
            from src.shared.color.color_space_descriptor import (
                apply_project_color_context,
            )
            project = getattr(self, "current_edit_project", None) \
                or getattr(self, "_current_edit_project", None)
            settings = (getattr(project, "render_cache_settings", None)
                        or {})
            # 工程色彩管理载荷在 EditProjectIO 加载时经 converter 注入
            # 进程层；此处幂等重放（空载荷=清空回全局默认）
            color_mgmt = None
            getter = getattr(self, "get_project_color_management", None)
            if callable(getter):
                color_mgmt = getter()
            apply_project_color_context(color_mgmt or {})
        except Exception:  # noqa: BLE001 — 注入失败回全局默认（已有日志路径）
            pass

        """Handle the Output node "Render Output" button.

        Detects the composite mode (image/video), opens the dedicated render
        dialog with sensible defaults, then dispatches to either the
        synchronous image path (main thread) or the background video /
        image-sequence path (worker thread).
        """
        if not self._composite_available or not self.composite_page:
            return

        from src.features.composite.render.composite_render_output import (
            CompositeMode,
            detect_composite_mode,
            resolve_video_range,
        )
        from src.features.composite.ui.composite_render_dialog import (
            CompositeRenderDialog,
        )
        from src.shared.i18n.i18n import I18N

        # Snapshot the current graph data BEFORE opening the dialog so the
        # render works on a stable copy even if the user keeps editing.
        graph_data = self.composite_page.get_composite_data()
        if not graph_data or not graph_data.get("nodes"):
            QMessageBox = self._import_qmessagebox()
            QMessageBox.information(
                self,
                I18N.tr("composite.render_output.title", "渲染输出 / Render Output"),
                I18N.tr(
                    "composite.render_output.empty_graph",
                    "当前合成图为空，无法渲染。",
                ),
            )
            return

        mode = detect_composite_mode(graph_data)

        # Resolve resolution from the Output node (authoritative).
        width, height = self._resolve_output_resolution(graph_data)

        # Resolve comp clip + video range (video mode only needs these, but we
        # compute them here so the dialog can pre-fill defaults).
        comp_clip = self._resolve_current_comp_clip(graph_data)
        start_frame, end_frame, fps = resolve_video_range(graph_data, comp_clip)

        # The render dialog only offers passes backed by the current Scene3D
        # contract. Metadata from the last preview is authoritative when it
        # exists; the node-type fallback keeps the option discoverable before
        # the first frame has arrived.
        available_render_passes = self._collect_available_render_passes(graph_data)

        # Default output directory / name
        default_dir = (
            self.config_manager.get("default_export_path")
            or self.config_manager.get("last_path")
            or os.path.expanduser("~")
        )
        default_name = "composite"
        composite_id = getattr(self.composite_page, "_current_composite_id", "") or ""
        if composite_id:
            default_name = composite_name_for_file(composite_id)

        dialog = CompositeRenderDialog(
            parent=self,
            mode=mode,
            default_dir=default_dir,
            default_name=default_name,
            resolution=(width, height),
            frame_range=(start_frame, end_frame),
            fps=fps,
            available_render_passes=tuple(available_render_passes),
            parallel_frames_default=self._resolve_export_parallel_default(),
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return

        settings = dialog.get_settings()

        # P0-03: preflight the export graph before dispatching. Hard errors
        # (cycle / Output without input / missing media) block the final
        # render; placeholder nodes in the render tree require explicit
        # confirmation (P0-06: 占位节点默认不进最终渲染).
        from src.features.composite.core.graph_preflight import (
            MODE_EXPORT,
            run_preflight_data,
        )
        report = run_preflight_data(graph_data, mode=MODE_EXPORT)
        # Keep the report actionable in the graph editor as well as in the
        # modal export message.  Node badges are view-only and do not mutate
        # the graph or its undo history.
        self.composite_page.apply_preflight_diagnostics(report)
        if not report.ok:
            QMessageBox = self._import_qmessagebox()
            hard_errors = [i for i in report.errors
                           if i.code != "node.placeholder"]
            placeholder_errors = [i for i in report.errors
                                  if i.code == "node.placeholder"]
            if hard_errors:
                QMessageBox.critical(
                    self,
                    I18N.tr("composite.render_output.preflight_title",
                            "渲染预检未通过"),
                    I18N.tr(
                        "composite.render_output.preflight_blocked",
                        "以下问题会阻止最终渲染：\n\n{issues}").format(
                            issues=report.format_summary()),
                )
                return
            if placeholder_errors:
                ret = QMessageBox.question(
                    self,
                    I18N.tr("composite.render_output.preflight_title",
                            "渲染预检未通过"),
                    I18N.tr(
                        "composite.render_output.preflight_placeholder",
                        "渲染树中包含尚未实现的占位节点，它们不会产生"
                        "真实效果：\n\n{issues}\n\n仍要继续渲染吗？").format(
                            issues="\n".join(
                                f"  {i.format()}" for i in placeholder_errors)),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return

        # Dispatch by mode.
        if mode == CompositeMode.IMAGE:
            self._run_composite_image_render(settings)
        else:
            self._run_composite_video_render(settings, graph_data, comp_clip)

    def on_render_composite(self) -> None:
        """Menu callback for the "Render Composite" action.

        Resolves the current graph's Output node (or the first one found) and
        delegates to _on_composite_render_output so the menu and the Output
        node's button share one entry point.
        """
        if not self._composite_available or not self.composite_page:
            return
        graph = getattr(self.composite_page, "node_graph", None)
        node_id = ""
        if graph is not None:
            for nid, node in getattr(graph, "nodes", {}).items():
                if getattr(node, "node_type", "") == "output":
                    node_id = nid
                    break
        self._on_composite_render_output(node_id)

    # ── Image render (synchronous, main thread) ──────────────────────

    def _run_composite_image_render(self, settings: dict) -> None:
        """Render a single composite image to disk on the main thread.

        Reuses the existing _render_composite_full_res() path (which uploads
        the live Color-page texture and evaluates the graph), then saves via
        FileIO + ICC embedding, mirroring perform_quick_export().
        """
        import numpy as np
        from PyQt6.QtWidgets import QApplication
        from src.shared.i18n.i18n import I18N
        from src.shared.io.file_io import FileIO

        # AUD-2401: 忙碌态屏蔽重复触发。下方唯一的 processEvents()（用于
        # 让进度条先绘制出来）构成重入窗口，窗口内再次发起的同步渲染会
        # 嵌套执行并互相覆盖状态栏/进度条。
        if getattr(self, "_composite_image_rendering", False):
            return
        self._composite_image_rendering = True

        path = settings["output_path"]
        self.status_bar.showMessage(
            I18N.tr(
                "composite.render_output.rendering_image",
                "正在渲染合成图片...",
            ),
            0,
        )
        self.progress_bar.setRange(0, 0)  # indeterminate
        self.progress_bar.show()
        QApplication.processEvents()

        try:
            # Render via the live Color-texture path.
            color_img = self.engine.get_current_image(force_full_res=True)
            if color_img is None:
                self._fail_image_render(path, "无法获取调色输出")
                return
            # AUD-2407: 线性保持由格式能力表 _LINEAR_IMAGE_FORMATS 驱动；
            # 其余显示格式在 _render_composite_full_res 内做 linear→sRGB
            # 编码，使导出与预览一致。
            ext = settings.get("image_format", "png").lower()
            export_img = self._render_composite_full_res(
                color_img, keep_linear=(ext in _LINEAR_IMAGE_FORMATS),
                render_pass=settings.get("render_pass", "beauty"),
            )
            if export_img is None:
                self._fail_image_render(path, "合成渲染失败")
                return

            # _render_composite_full_res already encodes linear→sRGB (the
            # composite readback is scene-linear; that central encode covers
            # every caller). convert_for_export only does ICC profile mapping
            # (a no-op for an sRGB source) — it is NOT a transfer-function
            # encoder, so we do NOT re-encode here (that would double-encode
            # and make the image too bright).
            source_space = getattr(self, "_source_color_space", None)
            export_img = self.color_manager.convert_for_export(
                export_img, source_space=source_space
            )

            # Dtype conversion per format.
            if export_img.dtype == np.float32:
                if ext in ("jpg", "jpeg", "png", "bmp", "webp"):
                    export_img = (np.clip(export_img, 0.0, 1.0) * 255.0).astype(np.uint8)
                elif ext in ("tif", "tiff"):
                    export_img = (np.clip(export_img, 0.0, 1.0) * 65535.0).astype(np.uint16)

            options = settings.get("options") or {}
            options.setdefault("preserve_metadata", True)
            if FileIO.save_image(path, export_img, options=options):
                if self._config_bool(self.config_manager.get("embed_icc_profile"), default=True):
                    try:
                        self.color_manager.embed_icc(path)
                    except Exception as e:
                        # AUD-2404: ICC 嵌入失败不影响已保存的图片，但必须
                        # 留下 warning 级痕迹（含堆栈），不允许裸 pass 吞错。
                        logger.warning(
                            "ICC profile embedding failed for %s: %s",
                            path, e, exc_info=True)
                self.status_bar.showMessage(
                    I18N.tr(
                        "composite.render_output.image_success",
                        "渲染成功: {filename}",
                        filename=os.path.basename(path),
                    ),
                    5000,
                )
                self.config_manager.set("last_path", os.path.dirname(path))
                self.config_manager.save_config()
                self._open_export_folder(path)
            else:
                self._fail_image_render(path, "保存图片失败")
        except Exception as e:
            logger.exception("Composite image render failed")
            self._fail_image_render(path, str(e))
        finally:
            self._composite_image_rendering = False
            self.progress_bar.hide()

    def _fail_image_render(self, path: str, detail: str) -> None:
        """图片渲染失败：状态栏提示 + 弹出严重错误对话框。

        Args:
            path: 渲染目标输出路径。
            detail: 具体失败原因描述。
        """
        from PyQt6.QtWidgets import QMessageBox
        from src.shared.i18n.i18n import I18N
        self.status_bar.showMessage(
            I18N.tr("composite.render_output.failed", "渲染失败"), 5000
        )
        QMessageBox.critical(
            self,
            I18N.tr("composite.render_output.title", "渲染输出 / Render Output"),
            f"{detail}\n{path}",
        )

    # ── Video / image-sequence render (background worker thread) ─────

    def _run_composite_video_render(
        self, settings: dict, graph_data: dict, comp_clip
    ) -> None:
        """Launch a background composite video / image-sequence render."""
        from src.features.composite.render.composite_render_worker import (
            CompositeRenderExporter,
        )
        from src.shared.i18n.i18n import I18N

        # AUD-2402: 渲染进行中拒绝再次启动。旧行为静默覆盖
        # ``_composite_render_exporter``：旧任务的完成回调随后清掉的是
        # 新任务的取消过滤器与 exporter 引用（新渲染失去取消入口，
        # 两个导出任务并发竞争输出路径/进度条）。
        busy_exporter = getattr(self, "_composite_render_exporter", None)
        if busy_exporter is not None and getattr(busy_exporter, "is_exporting", False):
            QMessageBox = self._import_qmessagebox()
            QMessageBox.warning(
                self,
                I18N.tr("composite.render_output.title", "渲染输出 / Render Output"),
                I18N.tr(
                    "composite.render_output.already_rendering",
                    "已有渲染任务正在进行，请等待完成或点击进度条取消后再试。",
                ),
            )
            return
        # AUD-2402/AUD-2403: 残留的非导出态 exporter（历史任务未走清理回调）
        # 先原地清掉，保证下方 UI 状态（过滤器/光标/tooltip）从零装配、
        # 引用不被静默覆盖。
        self._cleanup_composite_render_exporter()

        # Pre-flight: empty frame range.
        start = settings.get("start_frame", 0)
        end = settings.get("end_frame", 0)
        if end < start:
            QMessageBox = self._import_qmessagebox()
            QMessageBox.warning(
                self,
                I18N.tr("composite.render_output.title", "渲染输出 / Render Output"),
                I18N.tr(
                    "composite.render_output.invalid_range",
                    "结束帧不能小于起始帧。",
                ),
            )
            return

        # Resolve the output color space from preferences (timeline→Rec.709
        # by default; user can change in Color & Processing prefs). The
        # descriptor drives both the frame encode and the stream metadata.
        from src.shared.color.color_space_descriptor import descriptor_from_config
        is_video = bool(graph_data.get("nodes")) and any(
            isinstance(n, dict) and n.get("type") in
            ("video_frame", "timeline_clip_input")
            for n in (graph_data.get("nodes") or [])
        )
        output_descriptor = descriptor_from_config(
            self.config_manager, scope="output", kind="timeline", is_video=is_video
        )

        context = {
            "graph_data": graph_data,
            "edit_project": getattr(self, "_current_edit_project", None),
            "timeline_sequences": getattr(self, "_current_timeline_sequences", None),
            "comp_clip": comp_clip,
            "output_descriptor": output_descriptor,
            # config_manager so the worker/render path selects the user's
            # chosen color engine (builtin vs OCIO) instead of always defaulting
            # to OCIO (P1 fix: export paths must honour composite.color_engine).
            "config_manager": self.config_manager,
        }

        self.status_bar.showMessage(
            I18N.tr(
                "composite.render_output.rendering_video",
                "正在渲染合成视频...",
            ),
            0,
        )
        total = max(1, end - start + 1)
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        # 此前长渲染没有任何取消入口（cancel_composite_render 零调用方）。
        # 渲染期间点击进度条即可取消；过滤器在 exporter 清理时统一移除。
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QCursor

        self.progress_bar.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.progress_bar.setToolTip(
            I18N.tr("composite.render_output.click_to_cancel", "点击进度条取消渲染")
        )
        self._render_cancel_filter = _make_render_cancel_filter(
            self.cancel_composite_render,
        )
        self.progress_bar.installEventFilter(self._render_cancel_filter)
        # AUD-2401: 不再在派发前 processEvents()——export() 经队列调用派发
        # 到 worker 线程后立即返回，控制权回到事件循环，进度条随下一次
        # 事件循环自然绘制；这里的 processEvents 只会打开重入窗口（用户
        # 可在 exporter 建立前再次触发渲染/关闭窗口）。

        kind_label = (
            "video" if settings.get("kind") == "video_file" else "sequence"
        )

        # AUD-2401: UI 更新闭包只挂在 Qt 信号桥上，由（跨线程时自动退化为
        # QueuedConnection 的）连接封送回 UI 线程执行——回调的线程安全性
        # 不再依赖 exporter 的内部实现（export() 收到的是裸 Python 回调）。
        bridge = _make_render_callback_bridge()

        exporter: CompositeRenderExporter | None = None

        def on_progress(current: int, tot: int) -> None:
            self.progress_bar.setRange(0, max(1, tot))
            self.progress_bar.setValue(current)
            msg = I18N.tr(
                "composite.render_output.progress",
                "渲染中: {current}/{total} 帧",
                current=current,
                total=tot,
            )
            self.status_bar.showMessage(msg, 0)
            # AUD-2401: 去掉逐帧 processEvents()——它构成重入窗口（用户可在
            # 渲染期间再次触发菜单/关闭窗口）；队列连接本身即经事件循环投递，
            # 无需手动泵事件。

        def on_finished(output_path: str) -> None:
            if getattr(self, "_composite_render_exporter", None) is not exporter:
                return  # AUD-2402: 新渲染已接管 UI 状态，旧回调不得拆台。
            self.progress_bar.hide()
            self.status_bar.showMessage(
                I18N.tr(
                    "composite.render_output.video_success",
                    "渲染成功: {path}",
                    path=os.path.basename(output_path),
                ),
                5000,
            )
            self.config_manager.set("last_path", os.path.dirname(output_path))
            self.config_manager.save_config()
            self._open_export_folder(output_path)
            self._cleanup_composite_render_exporter(exporter)

        def on_error(error_msg: str) -> None:
            if exporter is not None and getattr(
                self, "_composite_render_exporter", None
            ) is not exporter:
                return  # AUD-2402: 新渲染已接管 UI 状态，旧回调不得拆台。
            self.progress_bar.hide()
            if error_msg == "EXPORT_CANCELLED":
                self.status_bar.showMessage(
                    I18N.tr(
                        "composite.render_output.cancelled",
                        "渲染已取消",
                    ),
                    5000,
                )
            else:
                self.status_bar.showMessage(
                    I18N.tr("composite.render_output.failed", "渲染失败"), 5000
                )
                QMessageBox = self._import_qmessagebox()
                QMessageBox.critical(
                    self,
                    I18N.tr("composite.render_output.title", "渲染输出 / Render Output"),
                    f"{kind_label} render failed:\n{error_msg}",
                )
            self._cleanup_composite_render_exporter(exporter)

        bridge.progress.connect(on_progress)
        bridge.finished.connect(on_finished)
        bridge.error.connect(on_error)
        self._render_callback_bridge = bridge

        # Hold a reference to avoid GC; cleared in cleanup.
        # AUD-2403: 构造/派发同步抛异常时走与 on_error 相同的状态复位
        # （隐藏进度条、错误提示、摘除取消过滤器），不再让 UI 停留在
        # "正在渲染..." + 手形光标 + 点击即取消的残留态。
        try:
            exporter = CompositeRenderExporter(parent=self)
            self._composite_render_exporter = exporter
            exporter.export(
                settings=settings,
                context=context,
                on_finished=bridge.finished.emit,
                on_error=bridge.error.emit,
                on_progress=bridge.progress.emit,
            )
        except Exception as e:
            logger.exception("Composite video/sequence render dispatch failed")
            on_error(str(e))

    def cancel_composite_render(self) -> None:
        """Cancel an in-progress composite video/sequence render."""
        exporter = getattr(self, "_composite_render_exporter", None)
        if exporter is not None and exporter.is_exporting:
            exporter.cancel()

    def _cleanup_composite_render_exporter(
        self, exporter: object | None = None
    ) -> None:
        """渲染结束/取消后的清理：移除进度条取消过滤器并释放 exporter。

        Args:
            exporter: 触发清理的回调所属的 exporter 实例（AUD-2402）。
                给定时，若 ``_composite_render_exporter`` 已被更新的渲染
                占据，本次清理整体跳过——旧任务的回调不得拆掉新任务的
                取消过滤器与 exporter 引用。None 或与当前一致的实例按
                原语义清理。
        """
        current = getattr(self, "_composite_render_exporter", None)
        if exporter is not None and current is not None and current is not exporter:
            return  # A newer render owns the UI state now.
        cancel_filter = getattr(self, "_render_cancel_filter", None)
        if cancel_filter is not None:
            try:
                self.progress_bar.removeEventFilter(cancel_filter)
            except RuntimeError as e:
                # AUD-2404: 收窄为 Qt wrapped-C++ 对象失效异常并留痕
                # （原为 except Exception + 裸 pass）。
                logger.warning(f"Remove render cancel filter failed: {e}")
            self._render_cancel_filter = None
        try:
            self.progress_bar.setToolTip("")
            self.progress_bar.unsetCursor()
        except RuntimeError as e:
            logger.warning(f"Reset progress bar cursor/tooltip failed: {e}")
        bridge = getattr(self, "_render_callback_bridge", None)
        if bridge is not None:
            self._render_callback_bridge = None
            bridge.deleteLater()
        if current is not None:
            try:
                current.cleanup()
            except Exception as e:
                logger.warning(f"Composite render exporter cleanup failed: {e}")
            self._composite_render_exporter = None

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _safe_int(value: object, default: int, minimum: int = 0) -> int:
        """把工程文件来源的数值字段安全转为 int（AUD-2406）。

        ``int(graph_data.get("width", w) or w)`` 只挡得住 None/0，挡不住
        字符串（旧版/手改/损坏工程里的 ``width: "3840px"`` 会让裸 int()
        在页面切换（_setup_timeline_clip_context）或渲染输出
        （_resolve_output_resolution）路径抛 ValueError）。坏值回落
        ``default``，结果按 ``minimum`` 钳制非法负数。
        """
        if not value:
            return default
        try:
            result = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, result)

    def _resolve_output_resolution(self, graph_data: dict):
        """Read (width, height) from the Output node, falling back to 1920x1080."""
        for node in graph_data.get("nodes") or []:
            if not isinstance(node, dict) or node.get("type") != "output":
                continue
            params = node.get("parameters") or {}
            if params.get("format_mode") in ("custom", "project"):
                # AUD-2406: Output 节点参数随图数据持久化，同样可能损坏。
                w = self._safe_int(params.get("width"), 1920, minimum=1)
                h = self._safe_int(params.get("height"), 1080, minimum=1)
                return w, h
        return 1920, 1080

    def _resolve_export_parallel_default(self) -> int:
        """Seed the render dialog's parallel-frames spin (CP-004).

        Mirrors CompositeRenderOutput._resolve_parallel_frames' preference
        fallback (composite.export_parallel_frames, default 2) so the dialog
        opens on the value an export without explicit UI input would use.
        """
        try:
            from src.features.composite.preferences import (
                EXPORT_PARALLEL_FRAMES_DEFAULT,
            )
            default = EXPORT_PARALLEL_FRAMES_DEFAULT
        except Exception:
            default = 2
        try:
            return max(1, int(
                self.config_manager.get("composite.export_parallel_frames")
                or default))
        except Exception:
            return default

    def _resolve_current_comp_clip(self, graph_data: dict):
        """Return the backing CompClipData dict for the current composite, or None.

        Looks up the project's comp_clips via the composite's comp_clip_id
        (stored on the graph data), falling back to fps/duration on the graph
        data itself.
        """
        project = getattr(self, "_current_edit_project", None)
        if project is None:
            return None
        comp_clip_id = graph_data.get("comp_clip_id")
        if not comp_clip_id:
            return None
        comp_clips = getattr(project, "comp_clips", None) or {}
        clip = comp_clips.get(comp_clip_id)
        if isinstance(clip, dict):
            return clip
        return None

    @staticmethod
    def _import_qmessagebox():
        """惰性导入并返回 QMessageBox 类（避免模块级 PyQt 依赖）。"""
        from PyQt6.QtWidgets import QMessageBox
        return QMessageBox

    @staticmethod
    def _config_bool(value, default: bool = False) -> bool:
        """把配置项宽松地解析为 bool（None → default，字符串按 1/true/yes/on）。"""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes", "on")


def composite_name_for_file(composite_id: str) -> str:
    """Derive a filesystem-safe default file name from a composite id."""
    import re
    # Use the trailing 8 hex chars of a UUID-ish id; sanitize the rest.
    tail = composite_id.replace("-", "")[-8:] if composite_id else "composite"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", tail)
    return f"composite_{safe}" if safe else "composite"
