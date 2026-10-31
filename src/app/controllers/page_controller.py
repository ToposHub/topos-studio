"""PageManagementMixin — extracted from MainWindow.

Handles page switching, page context coordination, and viewport restore logic.

职责（Responsibilities）:
    - 页面（Media/Edit/Color/Composite/Deliver）切换核心：leave/enter
      钩子、页面布局保存/恢复（LayoutStore 优先，legacy saveState 回退）、
      页面专属 dock 可见性强制与布局迁移（QTimer.singleShot 延迟修复）。
    - 带上下文切换（PageContext）：Edit→Color（跟随播放头打开时间线
      剪辑、静态缩略图预览、ColorStack 水合）、Edit→Composite（Comp Clip
      工作区恢复/自动绑定/时间线播放控件）、Edit→Deliver。
    - Color 时间线剪辑播放上下文维护（帧换算、子剪辑 source frame
      归一化、剪辑条同步）与 Keyframes 面板播放适配器。
    - 本 Mixin 不持有状态模型：所有 ``self.*`` 属性引用解析到宿主
      MainWindow 实例（page_layouts、canvas、engine、timeline_panel 等）。

与引擎/页面的边界（Boundary）:
    - 页面切换只搬运 dock/widget 归属与布局字节码，不触碰 GL 上下文；
      视口恢复经 ``_restore_viewport_after_page_switch`` 延迟触发
      ``request_render``，像素求值始终归 ``engine``/renderer。
    - Color 剪辑数据归 EditProject/ColorSession：本层经
      ``_run_color_stack_hydration_step`` 等薄封装读取并装配上下文，
      不直接改写剪辑/资产结构。

信号协议（Signal protocol）:
    - 入站：页面按钮点击、时间线双击/上下文菜单（open_timeline_clip_
      in_color）、播放控制条信号（经 _configure_composite_* 内的
      闭包槽连接）。
    - 出站：Mixin 本体无自定义 Qt 信号；通过 switch_to_page(_with_context)
      的钩子链（leave_page_hook/enter_page_hook）和状态栏消息通知
      其他控制器。后台色彩 tag 探测（AUD-3102）经模块级
      _ColorTagProbeSignals.probe_finished 队列回到 UI 线程。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional, Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from src.shared.diagnostics import flash_diag as _flash_diag

logger = logging.getLogger(__name__)
_COLOR_CLIP_DEBUG_ENABLED = os.environ.get("TOPOS_COLOR_CLIP_DEBUG", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
# [AUD-3101] 静态预览重试的硬上限：主重试链 0-7（50ms 间隔）+ 精确
# seek 后的 legacy 控制器重试最多到 20（约 1.2s @100ms）。任何控制器
# 持续返回未渲染（解码器卡死/损坏文件）时到此为止，不再形成无上限
# 的 100ms 强制 seek 风暴。
_COLOR_STATIC_PREVIEW_MAX_RETRIES = 20


class _ColorTagProbeSignals(QObject):
    """[AUD-3102] 后台色彩 tag 探测完成 → UI 线程回填的线程边界信号。

    ``probe_finished`` 从 daemon 工作线程 emit，Qt 自动队列投递到本对象
    所属线程（UI 线程）的槽，避免在非 GUI 线程触碰 canvas 控件。
    """

    probe_finished = pyqtSignal(int, object, object)  # generation, asset, tags|None


def _apply_color_tag_probe_result(
    host: Any, generation: int, asset: Any, tags: Optional[dict],
) -> None:
    """[AUD-3102] 探测完成回填（模块级：非 QObject 宿主同样可用）。

    在 UI 线程执行：清理在途集合 → 代际校验（过期结果丢弃）→ 写回
    ``asset.metadata`` → 刷新色彩监看。
    """
    path = getattr(asset, "path", None)
    inflight = getattr(host, "_color_tag_probes_inflight", None)
    if isinstance(inflight, set) and isinstance(path, str):
        inflight.discard(path)
    if int(getattr(host, "_color_tag_probe_generation", -1) or -1) != int(
        generation
    ):
        return  # 期间又打开了别的 clip：过期结果不覆盖当前监看
    if not tags:
        return
    metadata = getattr(asset, "metadata", None)
    if isinstance(metadata, dict):
        for key, value in tags.items():
            if value is not None:
                metadata[key] = value
    canvas = getattr(host, "canvas", None)
    update = getattr(canvas, "update_color_monitor", None)
    if not callable(update):
        return
    try:
        from src.shared.color.color_transform_provider import \
            get_color_provider_diagnostics
        diag = get_color_provider_diagnostics(
            getattr(host, "config_manager", None))
        update(
            source={
                "transfer": tags.get("color_transfer"),
                "primaries": tags.get("color_primaries"),
                "range": tags.get("color_range"),
            },
            working="scene-linear",
            display="sRGB",
            provider_diag=diag,
        )
    except Exception:
        logger.debug("color monitor update failed", exc_info=True)


def _probe_color_tags_sync_for_host(host: Any, asset: Any) -> None:
    """[AUD-3102] 无信号基础设施宿主的同步探测回退（保持旧语义）。

    探测、写回与监看刷新都在调用线程一次完成；结果经与异步路径相同
    的完成回填逻辑处理，两条路径行为一致。
    """
    path = getattr(asset, "path", None)
    if not isinstance(path, str) or not path:
        return
    generation = int(getattr(host, "_color_tag_probe_generation", 0) or 0) + 1
    host._color_tag_probe_generation = generation
    tags: dict[str, Any] | None = None
    try:
        from src.shared.media.factory import MediaSourceFactory
        meta = MediaSourceFactory.probe_video_metadata(path)
        if meta is not None:
            tags = {
                "color_transfer": getattr(meta, "color_transfer", None),
                "color_primaries": getattr(meta, "color_primaries", None),
                "color_range": getattr(meta, "color_range", None),
            }
    except Exception:
        logger.debug(
            "synchronous color-tag probe failed for %s", path, exc_info=True)
    _apply_color_tag_probe_result(host, generation, asset, tags)


def _launch_color_tag_probe_for_host(host: Any, asset: Any) -> None:
    """[AUD-3102] 在后台线程探测资产的色彩 tag（transfer/primaries/range）。

    去重：同一路径探测在途时不重复派发。完成后经
    ``_ColorTagProbeSignals.probe_finished`` 队列投递回 UI 线程。
    宿主销毁后 emit 会抛 RuntimeError，吞掉即可（daemon 线程收尾）。
    非 QObject 宿主没有线程边界信号，退回同步探测（此时后台线程也
    无法安全回填 canvas；生产宿主 MainWindow 始终走异步路径）。
    """
    path = getattr(asset, "path", None)
    if not isinstance(path, str) or not path:
        return
    inflight = getattr(host, "_color_tag_probes_inflight", None)
    if inflight is None:
        inflight = set()
        host._color_tag_probes_inflight = inflight
    if path in inflight:
        return
    signals = getattr(host, "_color_tag_probe_signals", None)
    if signals is None:
        try:
            signals = _ColorTagProbeSignals(parent=host)
        except TypeError:
            logger.debug(
                "color tag probe dispatcher unavailable; probing "
                "synchronously")
            _probe_color_tags_sync_for_host(host, asset)
            return
        signals.probe_finished.connect(host._on_color_tag_probe_finished)
        host._color_tag_probe_signals = signals
    generation = int(getattr(host, "_color_tag_probe_generation", 0) or 0) + 1
    host._color_tag_probe_generation = generation
    inflight.add(path)

    def probe_worker() -> None:
        tags: dict[str, Any] | None = None
        try:
            from src.shared.media.factory import MediaSourceFactory
            meta = MediaSourceFactory.probe_video_metadata(path)
            if meta is not None:
                tags = {
                    "color_transfer": getattr(meta, "color_transfer", None),
                    "color_primaries": getattr(meta, "color_primaries", None),
                    "color_range": getattr(meta, "color_range", None),
                }
        except Exception:
            logger.debug(
                "background color-tag probe failed for %s", path,
                exc_info=True)
        try:
            signals.probe_finished.emit(generation, asset, tags)
        except RuntimeError:
            pass  # 宿主已销毁：无需回填

    spawn = getattr(host, "_color_tag_probe_spawn", None)
    if callable(spawn):
        # 测试替身可注入同步执行器以获得确定性时序。
        spawn(probe_worker)
    else:
        thread = threading.Thread(
            target=probe_worker,
            name=f"color-tag-probe-{generation}",
            daemon=True,
        )
        thread.start()


class _KeyframesTimelineTransport:
    """Keyframes 面板播放按钮的适配器：复用 viewport 播放条同款引擎。

    时间线联动的合成里，viewport 播放条由 EditPlaybackController 驱动；
    Keyframes 面板的播放/暂停必须作用在同一个控制器上，否则两边播放头
    各自为政（面板播自己的 comp 定时器，viewport 条纹丝不动）。

    Delegate protocol: ``play()`` / ``pause()`` / ``is_playing() -> bool`` /
    ``seek(clip_local_frame)``。
    """

    def __init__(self, controller, clip_start: int, clip_total: int) -> None:
        """绑定底层播放控制器并记录 clip 的全局帧区间。

        Args:
            controller: EditPlaybackController（或同协议对象）。
            clip_start: clip 在时间线上的全局起始帧。
            clip_total: clip 总帧数（用于推导区间终点）。
        """
        self._controller = controller
        self._range_start = int(clip_start)
        self._range_end = int(clip_start) + max(1, int(clip_total)) - 1
        # Keyframes 面板工作区（I/O 范围），全局帧；None = 整个 clip。
        self._play_in: int | None = None
        self._play_out: int | None = None

    def is_playing(self) -> bool:
        """底层控制器是否正在播放。"""
        return bool(getattr(self._controller, "is_playing", False))

    def play(self) -> None:
        """开始播放：先把播放范围约束到 clip 区间（或面板工作区）。"""
        # 与 viewport 播放条的 _on_play 一致：先把播放范围约束到 clip 区间
        # （全局帧坐标），否则 tick() 会按整个时间线推进，一播放就跳到末尾。
        # 面板设置了工作区时优先用工作区（AE 式范围播放）；否则保留控制器
        # 上已有的合法范围（如播放条 I/O 标记），不能无条件重置为整个 clip。
        if hasattr(self._controller, "set_playback_range"):
            if self._play_in is not None and self._play_out is not None:
                self._controller.set_playback_range(
                    self._play_in, self._play_out)
            else:
                existing = getattr(self._controller, "playback_range", None)
                keep = (
                    isinstance(existing, (tuple, list))
                    and len(existing) == 2
                    and existing[0] is not None
                    and existing[1] is not None
                    and self._range_start <= int(existing[0])
                    and int(existing[1]) <= self._range_end
                    and int(existing[0]) <= int(existing[1]))
                if not keep:
                    self._controller.set_playback_range(
                        self._range_start, self._range_end)
        self._controller.play()

    def pause(self) -> None:
        """暂停播放（透传给底层控制器）。"""
        self._controller.pause()

    def set_play_range(self, clip_local_in: int, clip_local_out: int) -> None:
        """Keyframes 面板工作区（I/O）→ 时间线播放范围（全局帧）。

        在播放中修改也立即生效（EditPlaybackController 每次 tick 都读当前
        范围）；同时把播放头约束到范围内由控制器自身处理。
        """
        self._play_in = int(clip_local_in) + self._range_start
        self._play_out = int(clip_local_out) + self._range_start
        if hasattr(self._controller, "set_playback_range"):
            self._controller.set_playback_range(self._play_in, self._play_out)

    def seek(self, clip_local_frame: int) -> None:
        """Keyframes/Spline 面板播放头拖拽 → 时间线 seek。

        与 viewport 播放条的 scrub 语义一致：播放中拖动先暂停；
        clip 本地帧 + clip 起点 → 时间线全局帧。
        """
        if getattr(self._controller, "is_playing", False):
            self._controller.pause()
        self._controller.seek(int(clip_local_frame) + self._range_start)


def _color_clip_debug_enabled() -> bool:
    """Color 剪辑切换调试日志是否开启（环境变量或 DEBUG 级别）。"""
    return _COLOR_CLIP_DEBUG_ENABLED or logger.isEnabledFor(logging.DEBUG)


def _emit_color_clip_debug(message: str, *args: object) -> None:
    """调试开启时输出一条 Color 剪切换调试日志（否则空操作）。"""
    if not _color_clip_debug_enabled():
        return
    logger.debug(message, *args)


class PageManagementMixin:
    """Mixin that provides page switching and context coordination methods."""

    # ------------------------------------------------------------------
    # Page availability
    # ------------------------------------------------------------------

    def _is_page_available(self, page_name: str) -> bool:
        """Check if a page is available based on feature flags."""
        page = page_name.lower()
        if page == "edit":
            return self._edit_available
        if page == "composite":
            return self._composite_available
        if page == "deliver":
            return self._deliver_available
        return page in ("library", "color")

    # ------------------------------------------------------------------
    # Core page switching
    # ------------------------------------------------------------------

    def switch_to_page(self, page_name, force: bool = False) -> None:
        """切换到目标页面：重置工具态 → leave 钩子 → 布局恢复 → enter 钩子。

        流程要点：不可用页面回落到 Color；已在目标页且布局已知时提前
        返回（``force=True`` 用于启动时首激活）；普通 Edit→Color 切换
        跟随播放头重定向到剪辑路径；Deliver 与 Edit 复用同一 viewer/
        timeline widget，切换时搬运归属；恢复后按页面调度布局迁移
        （QTimer 延迟），最后延迟 50ms 恢复视口可见性并触发重绘。

        Args:
            page_name: 目标页面名（Library/Color/Edit/Composite/Deliver）。
            force: 已在目标页时也强制执行（首次激活/布局恢复）。
        """
        t0 = time.monotonic()
        from_page = self.current_page
        # The plain Edit→Color playhead route only applies to switches that
        # actually asked for Color; the unavailable-page fallback below reuses
        # "Color" as its target and must keep the legacy behaviour.
        requested_page = page_name
        # Composite is also a standalone workspace.  A normal Edit clip may
        # have no Comp Clip binding yet, but that must not prevent the user
        # from opening the page.  The enter hook resets the page to a scratch
        # graph in that case; creating a persistent Comp Clip remains an
        # explicit action from the Media Pool or the timeline context menu.
        # Reject unavailable pages and fallback to Color
        if not self._is_page_available(page_name):
            logger.warning(f"Page '{page_name}' is not available, falling back to Color")
            page_name = "Color"

        # Skip if already on the target page AND its layout is known — UNLESS
        # `force=True`. The force flag is used on first startup when
        # current_page already equals the target (e.g. app opens directly on
        # Composite) but the page has never been activated: apply_default_layout
        # hides all docks and relies on this call to restore visibility and run
        # enter_page_hook. Without force, the early return skips dock
        # restoration + on_enter(), so the Properties panel stays empty and
        # the Composite page is uninitialized until the user manually switches
        # away and back.
        if page_name == self.current_page and page_name in self.page_layouts and not force:
            return

        # A plain Edit→Color switch (tab click, shortcut) follows the Edit
        # playhead: the Color page must open the clip under the playhead at
        # that frame instead of restoring whatever clip it showed when it
        # was last visible. Contextual entries re-enter switch_to_page via
        # switch_to_page_with_context with the guard flag set and must not
        # route through the clip path a second time.
        if (
            from_page == "Edit"
            and requested_page == "Color"
            and not force
            and not getattr(self, "_contextual_page_switch_in_progress", False)
            and self._switch_to_color_following_edit_playhead()
        ):
            return

        # Task 3: 安全重置所有工具状态（防止切换页面后残留绘制状态）
        self.canvas.cancel_active_tools()

        # Auto-disable SAM mode when switching pages (prevents residual control points)
        self.canvas.set_sam_mode(False)

        # Clear SAM points and reset engine state
        self.canvas.clear_sam_points()
        if hasattr(self.engine, 'reset_sam_points'):
            self.engine.reset_sam_points()

        # Auto-disable brush tool when switching pages
        self.mask_panel.btn_brush.setChecked(False)

        # Auto-disable picker when switching pages
        self.qualifier_panel.set_picker_active(False)

        # Hooks for leaving page
        self.leave_page_hook(self.current_page)

        # Save current state (both legacy and LayoutStore)
        self.page_layouts[self.current_page] = self.saveState()
        if hasattr(self, '_save_current_layout_to_store'):
            self._save_current_layout_to_store(self.current_page)

        # Deliver reuses the real Edit Program Viewer and TimelinePanel. Move
        # them back before restoring another page's dock state, otherwise the
        # Edit docks would restore without their widgets.
        if from_page == "Deliver" and page_name != "Deliver":
            restore_deliver_panels = getattr(
                self, "_restore_edit_panels_from_deliver", None
            )
            if callable(restore_deliver_panels):
                restore_deliver_panels()

        # Composite owns its Node Properties dock (widget bound at creation),
        # so no panel swap is needed when entering Composite. Edit still uses
        # the shared dock_properties with its own panel.
        if page_name != "Composite":
            if self.dock_properties is not None and self.edit_properties_panel is not None:
                self.dock_properties.setWidget(self.edit_properties_panel)

        # Update buttons UI
        for name, btn in self.page_buttons.items():
            btn.setChecked(name == page_name)

        # Restore new state - try LayoutStore first, then fall back to legacy
        restored_from_store = False
        if hasattr(self, '_restore_layout_from_store'):
            restored_from_store = self._restore_layout_from_store(page_name)

        if not restored_from_store and page_name in self.page_layouts:
            self.restoreState(self.page_layouts[page_name])
        elif not restored_from_store:
            # New page without saved layout - save current state as its layout
            self.page_layouts[page_name] = self.saveState()

        # After the Deliver layout is restored, move the actual Edit viewer
        # and timeline widgets into its centre column. They remain the same
        # instances, so playback and playhead signals continue to work.
        if page_name == "Deliver":
            attach_deliver_panels = getattr(
                self, "_attach_edit_panels_to_deliver", None
            )
            if callable(attach_deliver_panels):
                attach_deliver_panels()

        self.current_page = page_name
        self.status_bar.showMessage(f"Mode: {page_name}", 2000)

        # Post-restore: enforce page-specific dock visibility.
        # Saved layouts may have docks visible that don't belong on this page
        # (e.g. dock_properties from Edit showing on Color page).
        if hasattr(self, '_enforce_page_dock_visibility'):
            self._enforce_page_dock_visibility()

        # Repair only legacy saved Edit layouts after their Qt state has been
        # restored.  The migration is versioned and never constrains later
        # user-driven splitter resizing.
        if page_name == "Edit" and hasattr(self, "_migrate_edit_properties_dock_width"):
            QTimer.singleShot(0, self._migrate_edit_properties_dock_width)
        if page_name == "Edit" and hasattr(self, "_migrate_edit_info_dock_visibility"):
            QTimer.singleShot(0, self._migrate_edit_info_dock_visibility)
        if page_name == "Edit" and hasattr(self, "_migrate_ai_assistant_dock_layout"):
            QTimer.singleShot(0, self._migrate_ai_assistant_dock_layout)
        if page_name == "Edit" and hasattr(self, "_migrate_edit_timeline_dock_height"):
            # Layout restoration completes after the current event turn.  A
            # small delay lets Timeline report its actual restored height.
            QTimer.singleShot(50, self._migrate_edit_timeline_dock_height)
        if page_name == "Color" and hasattr(self, "_migrate_color_grading_dock_height"):
            # The Color Wheel dock needs the restored splitter geometry to
            # settle before the legacy height can be repaired.
            QTimer.singleShot(50, self._migrate_color_grading_dock_height)
        if page_name == "Color" and hasattr(self, "_migrate_color_mapping_dock_layout"):
            QTimer.singleShot(0, self._migrate_color_mapping_dock_layout)
        if page_name == "Composite" and hasattr(self, "_migrate_composite_scopes_dock_visibility"):
            # Scopes was available before it entered the panel registry. Add
            # it to existing Composite layouts once, after restoreState().
            QTimer.singleShot(0, self._migrate_composite_scopes_dock_visibility)
        if page_name == "Deliver" and hasattr(self, "_migrate_deliver_layout"):
            # Old Deliver layouts had a separate queue dock and could restore
            # the settings dock at a collapsed width.  The queue now lives
            # beside the settings inside DeliverWorkspace.
            QTimer.singleShot(50, self._migrate_deliver_layout)

        # Note: We do NOT call _restore_docked_sizes here because restoreState()
        # already restores the page-specific dock proportions correctly.
        # _restore_docked_sizes uses global dock sizes from closeEvent which would
        # override the correct page-specific sizes.

        # Hooks for entering page
        self._page_switch_from_page = from_page
        self.enter_page_hook(page_name)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(f"[PAGE-SWITCH] {from_page} -> {page_name} completed in {elapsed_ms:.1f}ms")

        # Update dock titlebars (tabs might have changed)
        self.update_all_dock_titlebars()

        # Fix for FilesPanel gray block issue: refresh FilesPanel layout after page switch
        if hasattr(self, 'files_panel') and self.files_panel:
            QTimer.singleShot(150, self.files_panel.refresh_layout)

        # Fix black screen on cross-page switch: ensure viewport is visible and trigger render
        # When switching between pages with different dock configurations, the GL context
        # may become invalid. We need to ensure viewport is shown and trigger a render.
        QTimer.singleShot(50, self._restore_viewport_after_page_switch)

    # ------------------------------------------------------------------
    # Viewport restore helpers
    # ------------------------------------------------------------------

    def _restore_viewport_after_page_switch(self) -> None:
        """Restore viewport visibility and trigger render after page switch."""
        # For Composite page, update the CompositeViewer
        if self.current_page == "Composite":
            if self.composite_viewer:
                self.composite_viewer.update()
            return

        try:
            # Ensure floating viewport is visible and raised (respect user's floating preference)
            if hasattr(self, 'dock_viewport'):
                if self.dock_viewport.isVisible():
                    self.dock_viewport.raise_()

            # Ensure the GL viewport widget is visible. AUD-3107: 此前的显式
            # ``makeCurrent()`` 在 UI 线程保持 GL 上下文 current，其后无任何
            # GL 调用、也没有配对 doneCurrent()——与"UI 线程不得操作
            # OpenGL"铁律相悖（QOpenGLWidget 的 paint 事件会自行
            # makeCurrent），且与异步渲染线程共享上下文存在争用风险。
            # 可见性 + 下面的 update/invalidate 已足以触发重绘，删除该调用。
            if hasattr(self.canvas, 'gl_viewport') and self.canvas.gl_viewport and self.canvas.gl_viewport.isVisible():
                self.canvas.gl_viewport.show()

                # Trigger LOD update
                self.canvas._update_lod()

                # Force viewport update and invalidate scene
                self.canvas.gl_viewport.update()
                from PyQt6.QtWidgets import QGraphicsScene
                self.canvas.scene.invalidate(self.canvas.scene.sceneRect(), QGraphicsScene.SceneLayer.BackgroundLayer)

            # Trigger a new render
            if self.engine and hasattr(self.engine, 'render_pipeline'):
                self.engine.render_pipeline(render_reason='viewport_refresh')

            # Schedule final update
            QTimer.singleShot(50, self._finalize_viewport_restore)
        except Exception as e:
            logger.error(f"restoring viewport after page switch: {e}")

    def _finalize_viewport_restore(self) -> None:
        """Finalize viewport restore with a second render pass."""
        try:
            if self.current_page == "Composite":
                return
            self.canvas.viewport().update()
            if self.engine and hasattr(self.engine, 'render_pipeline'):
                self.engine.render_pipeline(render_reason='viewport_refresh')
        except Exception as e:
            logger.error(f"finalizing viewport restore: {e}")

    # ------------------------------------------------------------------
    # Page hooks (leave / enter)
    # ------------------------------------------------------------------

    def leave_page_hook(self, page_name) -> None:
        """Interface for future development."""
        # R-005: Notify FeatureRegistry before page-specific hooks
        page_id = page_name.lower()
        if self.feature_registry:
            self.feature_registry.on_page_leave(page_id)

        # Edit page leave hook (P1 placeholder)
        if page_name == "Edit":
            if self._edit_available and self.edit_page is not None:
                self.edit_page.on_leave()
            return

        # Deliver temporarily hosts the same Program Viewer and TimelinePanel
        # as Edit, so keep their preview lifecycle explicit while those
        # widgets are visible in Deliver.
        if page_name == "Deliver":
            if self._edit_available and self.edit_page is not None:
                self.edit_page.on_leave()
            return

        # The Color page owns a separate source-video playback session.  It
        # must be idle once the page is hidden, otherwise its decode workers and
        # color render commands compete with the Edit or Composite page.
        if page_name == "Color":
            controller = getattr(self, "video_controller", None)
            if controller is not None and getattr(controller, "is_playing", False):
                try:
                    controller.stop_playback()
                except (AttributeError, RuntimeError) as exc:
                    logger.warning("leave_page_hook: failed to stop Color playback: %s", exc)
            # The Color timeline preview has its own render worker.  Stop it
            # before the viewport is reparented to another page so no queued
            # frame can paint into a disappearing native OpenGL widget.
            deactivate_preview = getattr(self, "_deactivate_color_timeline_preview", None)
            if callable(deactivate_preview):
                deactivate_preview()
            return

        if page_name == "Composite" and self._composite_available and self.composite_page is not None:
            # The auto-bind suppress flag is only meaningful for the enter
            # hook of a single switch INTO Composite. All set-sites raise it
            # right before switch_to_page("Composite"), so by the time we
            # leave the page any True is stale and must be cleared — it
            # would block workspace restore on the next tab return.
            self._suppress_composite_auto_bind_once = False
            self._set_composite_playback_frame_sync(False)
            self._configure_composite_timeline_playback_controls(False)
            # 清除为 clip 区间设置的 playback_range，恢复时间线全局范围。
            try:
                controller = None
                if self.timeline_panel is not None:
                    controller = self.timeline_panel.get_playback_controller()
                if controller is not None and hasattr(controller, "clear_playback_range"):
                    controller.clear_playback_range()
            except (AttributeError, TypeError):
                pass
            # Stop the comp-clip playback timer when leaving Composite.
            try:
                if hasattr(self.composite_page, "comp_pause"):
                    self.composite_page.comp_pause()
            except Exception:
                pass
            try:
                if hasattr(self, "_flush_composite_to_edit_before_leave"):
                    self._flush_composite_to_edit_before_leave()
            except Exception as e:
                logger.warning(f"leave_page_hook: composite flush failed: {e}")

            if self._composite_adapter:
                self._composite_adapter.on_leave_page()
            else:
                self.composite_page.on_leave()
                if self.viewport_container and self.dock_viewport:
                    self.dock_viewport.setWidget(self.viewport_container)
                if self.composite_viewer:
                    self.composite_viewer.hide()
                if self.engine.renderer:
                    self.engine.render_pipeline(render_reason='viewport_refresh')

    def enter_page_hook(self, page_name) -> None:
        """Interface for future development."""
        # R-005: Notify FeatureRegistry before page-specific hooks
        page_id = page_name.lower()
        if self.feature_registry:
            self.feature_registry.on_page_enter(page_id)

        # Edit page enter hook (P1 placeholder)
        if page_name == "Edit":
            if self._edit_available and self.edit_page is not None:
                if self.dock_properties is not None:
                    self.dock_properties.setWidget(self.edit_properties_panel)
                try:
                    self.edit_page.on_enter()
                except Exception as e:
                    logger.warning(f"enter_page_hook: edit_page.on_enter failed: {e}")
                try:
                    if self._current_edit_project and not self.timeline_preview_renderer:
                        self._init_timeline_preview_renderer()
                except Exception as e:
                    logger.warning(f"enter_page_hook: _init_timeline_preview_renderer failed: {e}")
                if getattr(self, "_edit_preview_needs_composite_refresh", False):
                    try:
                        self._refresh_edit_preview_after_composite_change()
                    except Exception as e:
                        logger.warning(f"enter_page_hook: composite preview refresh failed: {e}")
            return

        if page_name == "Composite" and self._composite_available and self.composite_page is not None:
            if hasattr(self.composite_page, "set_edit_project"):
                self.composite_page.set_edit_project(getattr(self, "_current_edit_project", None))
            # Image Color mode: a studio "image" work-mode preference turns the
            # Composite page into a static-effects workspace — keyframes,
            # playback and time-driven nodes are disabled. Set the flag before
            # any binding so panels/buttons are built in the right state.
            try:
                from src.shared.feature_flags import get_work_mode
                work_mode = get_work_mode(getattr(self, "config_manager", None))
                if hasattr(self.composite_page, "set_image_color_mode"):
                    self.composite_page.set_image_color_mode(work_mode == "image")
            except Exception:
                pass
            composite_context_applied = False
            current_context = self._page_context_manager.get_current_context()
            is_contextual_composite_entry = (
                getattr(self, '_contextual_page_switch_in_progress', False)
                and current_context is not None
                and getattr(current_context.target_page, "value", None) == "Composite"
                and getattr(current_context.source_page, "value", None) == "Edit"
                and bool(current_context.clip_id)
            )
            if is_contextual_composite_entry:
                self._handle_enter_composite_with_context(current_context)
                composite_context_applied = True
            elif getattr(self, "_page_switch_from_page", "") == "Edit":
                composite_context_applied = self._auto_bind_composite_to_selected_clip()
                if not composite_context_applied:
                    # No bindable selection — restore the composition left
                    # open before falling back to the scratch workspace.
                    composite_context_applied = (
                        self._restore_current_composite_workspace()
                    )
                if not composite_context_applied:
                    self._prepare_empty_composite_workspace()
            else:
                # Returning to the page should restore the composition that
                # was left open before trying to infer a new source from the
                # current Color/Edit selection. Otherwise a tab switch with
                # no selected clip falls through to the empty workspace and
                # makes the open composition appear to have been closed.
                composite_context_applied = (
                    self._restore_current_composite_workspace()
                )
                if not composite_context_applied:
                    color_session_applied = self._auto_bind_composite_to_color_session()
                    if color_session_applied:
                        composite_context_applied = True
                    else:
                        # Check standalone image before falling back to the Edit
                        # timeline. Standalone images use ColorOutput; timeline
                        # videos must stay bound to their clip context.
                        standalone_applied = self._try_standalone_image_composite()
                        if standalone_applied:
                            composite_context_applied = True
                        else:
                            composite_context_applied = self._auto_bind_composite_to_selected_clip()
                            if not composite_context_applied:
                                self._prepare_empty_composite_workspace()

            # A timeline-backed Composite graph owns the Edit playback clock.
            # Stop any legacy Color-page video player before it can decode the
            # same file concurrently and race the Composite source context.
            if getattr(self.composite_page, "_runtime_context", None) is not None:
                legacy_video_controller = getattr(self, "video_controller", None)
                if legacy_video_controller is not None and getattr(
                    legacy_video_controller, "is_playing", False
                ):
                    try:
                        legacy_video_controller.stop_playback()
                    except (AttributeError, RuntimeError) as exc:
                        logger.warning(
                            "Failed to stop legacy video playback for Composite: %s",
                            exc,
                        )
            self._set_composite_playback_frame_sync(True)
            self._configure_composite_timeline_playback_controls(
                getattr(self.composite_page, "_runtime_context", None) is not None
            )
            # Comp-clip path: an independent composition drives its own playback
            # (no timeline/video controller). Wire + show the transport bar.
            self._configure_composite_comp_clip_controls()

            if self._composite_adapter:
                co = self.engine.color_output
                needs_color_output = self.composite_page.requires_color_output_input()
                tex_id = co.texture_id if needs_color_output and co.is_valid else None
                co_w = co.width if needs_color_output and co.is_valid else 0
                co_h = co.height if needs_color_output and co.is_valid else 0
                co_rev = co.revision if needs_color_output and co.is_valid else 0
                if needs_color_output and (not co.is_valid or co.is_dirty):
                    self.engine.request_color_output()
                self._composite_adapter.on_enter_page(tex_id, co_w, co_h, co_rev)
                self._composite_context_applied_during_enter = composite_context_applied
                return

            # Fallback: legacy direct path. Node Properties dock already
            # owns its widget, so no setWidget is needed here.
            if self.dock_node_properties is not None and not self.dock_node_properties.isVisible():
                self.dock_node_properties.show()

            if self.composite_viewer and self.dock_viewport:
                self.dock_viewport.setWidget(self.composite_viewer)

            co = self.engine.color_output
            needs_color_output = self.composite_page.requires_color_output_input()
            tex_id = co.texture_id if needs_color_output and co.is_valid else None
            co_w = co.width if needs_color_output and co.is_valid else 0
            co_h = co.height if needs_color_output and co.is_valid else 0
            co_rev = co.revision if needs_color_output and co.is_valid else 0

            if needs_color_output and (not co.is_valid or co.is_dirty):
                self.engine.request_color_output()

            # Inject the real source size (full-res) before on_enter runs the
            # Output-node sync, so Project Settings shows the true dimensions.
            img = getattr(self.engine, "img_full_original", None)
            if img is not None and hasattr(self.composite_page, "set_source_image_size"):
                try:
                    ih, iw = img.shape[:2]
                    if iw > 0 and ih > 0:
                        self.composite_page.set_source_image_size(iw, ih)
                except (AttributeError, IndexError, TypeError):
                    pass

            self.composite_page.on_enter(tex_id, co_w, co_h, co_rev)
            self._composite_context_applied_during_enter = composite_context_applied

            # Apply the preview color space (viewer ODT) from preferences +
            # smart default. A timeline/video source → Rec.709 transfer;
            # a standalone image → sRGB transfer. The user can override in
            # Color & Processing preferences.
            self._apply_composite_preview_color_space()

            if self.composite_viewer and self.composite_page._last_texture_id:
                self.composite_viewer.set_texture(
                    self.composite_page._last_texture_id,
                    self.composite_page._last_texture_width,
                    self.composite_page._last_texture_height
                )
                if self.composite_page._last_metadata:
                    self._on_composite_metadata_ready(self.composite_page._last_metadata)
            # Restore the video control bar visibility when entering Composite
            # while a video is loaded. load_video() may have run before this
            # viewer was placed in the dock (or the page was entered later), so
            # the show_for_video_with_io() call did not take effect. Re-sync now.
            if self.composite_viewer and self.video_controller:
                try:
                    has_video = (
                        getattr(self.video_controller, "decoder", None) is not None
                        or getattr(self.video_controller, "is_playing", False)
                    )
                    if has_video:
                        self.composite_viewer.video_control_bar.show_for_video_with_io()
                        self.composite_viewer.update_video_control_bar_position()
                except Exception as e:
                    logger.warning(f"enter_page_hook: composite video bar sync failed: {e}")
            return

        # Color page enter hook: refresh the timeline clip strip when entering
        # the Color page. Kept here (in the page-switch handler) rather than in
        # _apply_composite_preview_color_space() because it depends on the
        # local page_name variable which that helper does not receive.
        if page_name == "Color":
            sync_color_timeline_selector = getattr(
                self, "_sync_color_timeline_selector", None
            )
            if callable(sync_color_timeline_selector):
                sync_color_timeline_selector()
            try:
                self.refresh_color_timeline_clip_strip()
            except Exception as e:
                logger.warning(f"enter_page_hook: refresh_color_timeline_clip_strip failed: {e}")
            return

        # Deliver page enter hook (P9.3)
        if page_name == "Deliver":
            if self._deliver_available and self.deliver_page is not None:
                # Set project and sequence references for timeline export
                self.deliver_page.set_project(self._current_edit_project)

                # Get active timeline sequence
                if self._current_edit_project and self._current_edit_project.active_timeline_id:
                    sequence = self._current_timeline_sequences.get(self._current_edit_project.active_timeline_id)
                    self.deliver_page.set_sequence(sequence)
                else:
                    self.deliver_page.set_sequence(None)

                # P1-02: 嵌套序列导出需要全量时间线做递归解析
                set_seqs = getattr(self.deliver_page, "set_timeline_sequences", None)
                if callable(set_seqs):
                    set_seqs(self._current_timeline_sequences)

                # The centre of Deliver contains the live Edit viewer and
                # timeline, so enable the existing preview coordinator while
                # those widgets are hosted here.
                if self._edit_available and self.edit_page is not None:
                    self.edit_page.on_enter()

                # For Color Image source, set current image path
                if self.engine.img_full_original is not None:
                    h, w = self.engine.img_full_original.shape[:2]
                    self.export_panel.set_original_size(w, h)
                    self.export_panel.set_default_path(self.current_file_path)
            return

    def _prepare_empty_composite_workspace(self) -> None:
        """Prepare an unbound Composite workspace for direct page entry.

        Direct navigation from Edit/Color is intentionally non-persistent when
        there is no existing Comp Clip.  Clearing the previous graph here is
        important: otherwise a graph from a previously opened Comp Clip could
        remain visible and subsequent edits could be mistaken for edits to
        that old composition.
        """
        page = getattr(self, "composite_page", None)
        reset = getattr(page, "reset_to_empty_workspace", None)
        if callable(reset):
            reset()
        else:
            # Keep this fallback safe for lightweight test doubles and older
            # CompositePage implementations.
            if page is not None:
                page._current_composite_id = ""
                page.set_timeline_clip_context(None, None)
        # Save/leave routing also consults the controller-level id, so clear it
        # together with the page-level id when entering the scratch workspace.
        self._current_composite_id = ""
        self._suppress_composite_auto_bind_once = False
        logger.debug("[Composite] Entering unbound empty workspace")

    def _restore_current_composite_workspace(self) -> bool:
        """Restore the last opened persistent Composite workspace.

        Leaving Composite intentionally keeps the live graph in memory and
        flushes it to ``EditProject``. A later page-tab return must restore
        that identity before trying to infer a composition from the current
        Edit selection; otherwise a page switch with no selected clip falls
        through to ``_prepare_empty_composite_workspace`` and clears the
        user's open composition.

        Explicit graph opens use a suppression/context flag and continue
        through their dedicated loading path instead of being mistaken for a
        tab-return restore.
        """
        if (getattr(self, "_suppress_composite_auto_bind_once", False)
                or getattr(self, "_contextual_page_switch_in_progress", False)):
            return False

        page = getattr(self, "composite_page", None)
        project = getattr(self, "_current_edit_project", None)
        if page is None or project is None:
            return False

        composite_id = (
            getattr(self, "_current_composite_id", "")
            or getattr(page, "_current_composite_id", "")
        )
        if not composite_id or composite_id == "standalone_composite":
            # After an app restart the in-memory identity is empty; fall back
            # to the persisted active Comp Clip recorded when the app closed.
            composite_id = self._resolve_remembered_comp_clip_composite_id(project)
        if not composite_id or composite_id == "standalone_composite":
            # Standalone images are restored by the existing Color-session
            # path, which also verifies the current image path.
            return False

        try:
            composite_data = project.get_composition_by_id(composite_id)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning(
                "[Composite] Cannot restore composition %s: %s",
                composite_id, exc,
            )
            return False
        if not composite_data:
            logger.warning(
                "[Composite] Remembered composition not found: %s",
                composite_id,
            )
            return False

        prepare = getattr(self, "_prepare_composite_data_for_open", None)
        if callable(prepare):
            composite_data = prepare(composite_id, composite_data)

        # Timeline-backed graphs need a fresh runtime context when returning
        # from another page; this also loads the graph through the same
        # authoritative Edit -> Composite path used for explicit opens.
        build_context = getattr(self, "_context_for_composite_data", None)
        context = None
        if callable(build_context):
            try:
                context = build_context(composite_id, composite_data)
            except Exception as exc:
                logger.warning(
                    "[Composite] Cannot rebuild context for %s: %s",
                    composite_id, exc,
                )

        try:
            if context is not None:
                context_manager = getattr(self, "_page_context_manager", None)
                if context_manager is not None:
                    context_manager.set_current_context(context)
                self._handle_enter_composite_with_context(context)
            else:
                page.load_composite_data(composite_data)
                if hasattr(page, "set_timeline_clip_context"):
                    page.set_timeline_clip_context(None, None)
                if hasattr(page, "set_composition_active"):
                    page.set_composition_active(True)
        except Exception as exc:
            logger.exception(
                "[Composite] Failed to restore composition %s: %s",
                composite_id, exc,
            )
            return False

        self._current_composite_id = str(composite_id)
        logger.info("[Composite] Restored remembered composition: %s",
                    composite_id)
        return True

    def _resolve_remembered_comp_clip_composite_id(self, project) -> str:
        """Resolve the persisted ``active_comp_clip_id`` back to its graph id.

        Comp Clip records map a comp clip to its CompositeGraphData via
        ``graph_id``; the restore path needs the graph/composition id.
        """
        try:
            active_cc = getattr(project, "active_comp_clip_id", None)
            if not active_cc:
                return ""
            cc = project.comp_clips.get(active_cc)
            graph_id = cc.get("graph_id", "") if isinstance(cc, dict) else getattr(cc, "graph_id", "")
            return str(graph_id or "")
        except Exception as exc:
            logger.warning("[Composite] Cannot resolve remembered comp clip: %s", exc)
            return ""

    def _apply_composite_preview_color_space(self) -> None:
        """Apply the preview (viewer ODT) color space from preferences.

        Picks the timeline (Rec.709) or image (sRGB) transfer based on whether
        the composite is a timeline-clip (video) source, then sets the
        CompositeViewer's display transform. Driven by color.preview.* prefs
        and the color.smart_default flag. For HDR sources, applies the
        configured HDR→SDR tonemap so HDR content previews correctly on a
        normal (SDR) display.
        """
        if not self._composite_available or not self.composite_viewer:
            return
        try:
            from src.shared.color.color_space_descriptor import (
                descriptor_from_config,
            )
            from src.shared.color.types import ColorTransfer  # noqa: F401 — types 模块可用性探测（见下方 except 注释）
            # A timeline-clip composite (runtime_context set) is video;
            # otherwise it's a still-image composite.
            is_video = (
                self.composite_page is not None
                and getattr(self.composite_page, "_runtime_context", None) is not None
            )
            kind = "timeline" if is_video else "image"
            desc = descriptor_from_config(
                self.config_manager, scope="preview", kind=kind, is_video=is_video
            )
            # Detect HDR content in the graph (PQ/HLG-tagged source nodes) so
            # we can tonemap it to SDR for the preview display.
            is_hdr_source = False
            if self.composite_page is not None:
                ng = getattr(self.composite_page, "node_graph", None)
                if ng is not None:
                    for node in getattr(ng, "nodes", {}).values():
                        ntype = getattr(node, "node_type", "")
                        if ntype == "video_frame":
                            # video_frame nodes carry probed VideoMetadata on
                            # the instance; its color_transfer reveals HDR.
                            meta = getattr(node, "_video_metadata", None)
                            tf = (getattr(meta, "color_transfer", "") or "").lower()
                            if tf in ("smpte2084", "smpte2084-10", "arib-std-b67"):
                                is_hdr_source = True
                                break
                        elif ntype == "timeline_clip_input":
                            # The timeline provider stamps the detected transfer
                            # into the node's last-evaluated source color tag
                            # (set from clip metadata during evaluation). Fall
                            # back to a parameter hint when not yet evaluated.
                            cs = getattr(node, "_last_source_color_tag", None) or ""
                            if cs in ("pq", "smpte2084", "hlg", "arib-std-b67"):
                                is_hdr_source = True
                                break
            tonemap = 0
            target_peak = 100.0
            if is_hdr_source:
                tonemap = int(self.config_manager.get("color.preview.hdr_tonemap", 1) or 0)
                target_peak = float(self.config_manager.get("color.preview.hdr_target_peak", 100) or 100)
            # set_display_space drives the full ODT: transfer + gamut +
            # tonemap. Falls back gracefully if the viewer is an older build.
            if hasattr(self.composite_viewer, "set_display_space"):
                try:
                    self.composite_viewer.set_display_space(
                        desc, tonemap=tonemap, target_peak=target_peak,
                    )
                except Exception:
                    # [AUD-3104] viewer 拒绝参数（领域错误）不得被静默
                    # 降为 debug：记录 warning + 堆栈，并回退 transfer-only
                    # 路径，预览仍可用（至少 transfer 正确）。
                    logger.warning(
                        "set_display_space failed, falling back to "
                        "transfer-only preview ODT",
                        exc_info=True,
                    )
                    self._apply_legacy_display_transfer(desc)
            else:
                # Older viewer: transfer only.
                self._apply_legacy_display_transfer(desc)
        except ImportError as e:
            # 旧版组件缺失（descriptor/types 未提供）属预期回退，保持 debug。
            logger.debug(
                "preview color-space apply skipped (legacy components): %s", e)
        except Exception as e:
            # [AUD-3104] 旧实现把整条预览 ODT 链（descriptor 构建、HDR
            # 源探测、tonemap 配置）的任何领域错误都降为 debug 级，Viewer
            # 以错误 transfer/gamut 显示 HDR 内容且无任何用户可见诊断。
            # 现在至少 warning 带堆栈，故障可排查。
            logger.warning(
                "preview color-space apply failed (non-fatal): %s", e,
                exc_info=True)

    def _apply_legacy_display_transfer(self, desc: Any) -> None:
        """[AUD-3104] 旧版 viewer 回退：仅设置 display transfer 枚举。"""
        from src.shared.color.types import ColorTransfer
        transfer_map = {
            ColorTransfer.LINEAR: 0, ColorTransfer.SRGB: 1,
            ColorTransfer.BT709: 2, ColorTransfer.PQ: 3,
        }
        self.composite_viewer.set_display_transfer(
            transfer_map.get(desc.transfer, 1)
        )

    def _auto_bind_composite_to_selected_clip(self) -> bool:
        """Bind direct Composite page entry to the selected Edit timeline clip.

        Returns True if a clip was successfully bound, False otherwise.
        """
        if getattr(self, "_suppress_composite_auto_bind_once", False):
            self._suppress_composite_auto_bind_once = False
            logger.debug("[Composite] Auto-bind skipped for direct graph open")
            return False
        if getattr(self, '_contextual_page_switch_in_progress', False):
            return False
        if not self._edit_available or self.timeline_panel is None:
            return False
        if self.composite_page is None:
            return False

        timeline = self.edit_page._current_timeline if self.edit_page else None
        if timeline is None:
            return False

        contextual_clip_id = None
        contextual_color_stack_id = None
        current_context = self._page_context_manager.get_current_context()
        if current_context and current_context.clip_id:
            contextual_clip_id = current_context.clip_id
            contextual_color_stack_id = current_context.color_stack_id

        try:
            clip_ids = self.timeline_panel.get_selected_clip_ids()
        except (AttributeError, TypeError):
            clip_ids = []

        try:
            timeline_frame = self.timeline_panel.get_playhead_frame()
        except (AttributeError, TypeError):
            timeline_frame = 0

        clip = None
        track = None
        if clip_ids:
            selected_clip_id = clip_ids[0]
            for candidate_track in timeline.all_tracks:
                candidate_clip = candidate_track.get_clip_by_id(selected_clip_id)
                if candidate_clip is not None:
                    track = candidate_track
                    clip = candidate_clip
                    if timeline_frame < clip.timeline_start or timeline_frame >= clip.timeline_end:
                        timeline_frame = clip.timeline_start
                    break

        if clip is None:
            for candidate_track in tuple(timeline.video_tracks) + tuple(timeline.audio_tracks):
                candidate_clip = candidate_track.get_clip_at(timeline_frame)
                if candidate_clip is not None:
                    track = candidate_track
                    clip = candidate_clip
                    break

        if clip is None and contextual_clip_id:
            for candidate_track in timeline.all_tracks:
                candidate_clip = candidate_track.get_clip_by_id(contextual_clip_id)
                if candidate_clip is not None:
                    track = candidate_track
                    clip = candidate_clip
                    break

        if clip is None or track is None:
            logger.info("[Composite] Direct page entry skipped: no selected or playhead clip")
            return False

        clip_id = clip.clip_id
        timeline_id = getattr(timeline, 'timeline_id', None)

        if timeline_frame < clip.timeline_start or timeline_frame >= clip.timeline_end:
            timeline_frame = clip.timeline_start
        self._sync_timeline_playhead_for_composite_entry(timeline_frame)

        color_stack_id = clip.color_stack_id or contextual_color_stack_id
        resolve_composite_id = getattr(
            self, "_resolve_composite_id_for_clip", None
        )
        composite_id = (
            resolve_composite_id(clip)
            if callable(resolve_composite_id)
            else getattr(clip, "composite_id", None)
        )
        if not composite_id:
            logger.info(
                "[Composite] Direct page entry skipped: clip has no Comp Clip; "
                "use Create Comp Clip first"
            )
            return False

        clip_local_frame = timeline_frame - clip.timeline_start
        try:
            source_frame = clip.timeline_to_source_frame(timeline_frame)
        except (ValueError, AttributeError):
            source_frame = clip.source_start

        context = self._page_context_manager.create_context_for_edit_to_composite(
            clip_id=clip_id,
            composite_id=composite_id,
            asset_id=clip.asset_id,
            timeline_id=timeline_id,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
            color_stack_id=color_stack_id,
        )
        self._page_context_manager.set_current_context(context)
        self._handle_enter_composite_with_context(context)
        return True

    def _auto_bind_composite_to_color_session(self) -> bool:
        """Bind Color → Composite entry to the active timeline ColorSession."""
        if self.composite_page is None:
            return False
        session = None
        if hasattr(self, "_color_session_manager"):
            session = self._color_session_manager.get_current_session()
        if session is None or not getattr(session, "is_timeline_clip", lambda: False)():
            return False

        clip_id = getattr(session, "clip_id", None)
        timeline_id = getattr(session, "timeline_id", None)
        if not clip_id or not timeline_id:
            return False

        timeline = None
        if hasattr(self, "_current_timeline_sequences"):
            timeline = self._current_timeline_sequences.get(timeline_id)
        if timeline is None and self.edit_page is not None:
            timeline = getattr(self.edit_page, "_current_timeline", None)
            timeline_id = timeline_id or getattr(timeline, "timeline_id", "")
        if timeline is None:
            return False

        clip = None
        for track in timeline.all_tracks:
            candidate = track.get_clip_by_id(clip_id)
            if candidate is not None:
                clip = candidate
                break
        if clip is None:
            return False

        timeline_frame = getattr(session, "timeline_frame", None)
        source_frame = getattr(session, "source_frame", None)

        # If Color is showing a video frame, prefer the controller's current
        # source frame for the initial Composite preview.
        video_controller = getattr(self, "video_controller", None)
        if getattr(self, "current_file_is_video", False) and video_controller is not None:
            try:
                source_frame = int(getattr(video_controller, "current_frame", source_frame or 0))
            except (TypeError, ValueError):
                pass

        if timeline_frame is None:
            if source_frame is not None:
                source_start = getattr(clip, "source_start", 0) or 0
                timeline_frame = getattr(clip, "timeline_start", 0) + max(0, int(source_frame) - int(source_start))
            else:
                timeline_frame = getattr(clip, "timeline_start", 0)

        frame_was_clamped = False
        if timeline_frame < clip.timeline_start or timeline_frame >= clip.timeline_end:
            timeline_frame = clip.timeline_start
            frame_was_clamped = True

        try:
            mapped_source_frame = clip.timeline_to_source_frame(timeline_frame)
        except (ValueError, AttributeError):
            mapped_source_frame = getattr(clip, "source_start", 0)
        if source_frame is None or frame_was_clamped:
            source_frame = mapped_source_frame

        clip_local_frame = timeline_frame - clip.timeline_start
        color_stack_id = (
            getattr(session, "color_stack_id", None)
            or getattr(clip, "color_stack_id", None)
        )
        resolve_composite_id = getattr(
            self, "_resolve_composite_id_for_clip", None
        )
        composite_id = (
            resolve_composite_id(clip)
            if callable(resolve_composite_id)
            else getattr(clip, "composite_id", None)
        )
        if not composite_id:
            logger.info(
                "[Composite] Color entry skipped: clip has no Comp Clip; "
                "use Create Comp Clip first"
            )
            return False

        if not color_stack_id:
            color_stack_id = self._ensure_clip_has_color_stack(clip_id, clip.asset_id)

        self._sync_timeline_playhead_for_composite_entry(timeline_frame)

        context = self._page_context_manager.create_context_for_edit_to_composite(
            clip_id=clip_id,
            composite_id=composite_id,
            asset_id=getattr(session, "asset_id", None) or clip.asset_id,
            timeline_id=timeline_id,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
            color_stack_id=color_stack_id,
        )
        self._page_context_manager.set_current_context(context)
        self._handle_enter_composite_with_context(context)
        return True

    def _try_standalone_image_composite(self) -> bool:
        """Switch Composite graph to ColorOutput mode for standalone images.

        When the Color page has a standalone image loaded (not a timeline
        clip) and the user navigates to Composite, this method uses the
        Color page's already-rendered output as the Composite source.

        Returns True if the graph was switched, False if not applicable.
        """
        # Guard: must have a current file that is NOT a video
        file_path = getattr(self, 'current_file_path', None)
        if not file_path:
            return False
        if getattr(self, 'current_file_is_video', False):
            return False
        if self.composite_page is None:
            return False

        # Guard: only for standalone image sessions
        session = None
        if hasattr(self, '_color_session_manager'):
            session = self._color_session_manager.get_current_session()
        if session is not None and not session.is_standalone_image():
            return False

        # Bind a stable composite id so the leave-flush persists this image's
        # graph. Without it, standalone images have no composite id and the
        # leave-flush no-op'd — so edits were lost when switching images.
        self.composite_page._current_composite_id = "standalone_composite"
        if hasattr(self.composite_page, "set_composition_active"):
            self.composite_page.set_composition_active(True)
        # 同步到 file_controller（同一 self），让 save_router 的
        # get_current_composite_id 回调读到正确 id —— 否则 Color/Library 页
        # 图片工程始终使用稳定的合成 ID，避免切换图片时串用上一张图的图层。
        # "standalone_composite" 写入，读取端虽能命中但 id 不一致会埋隐患。
        self._current_composite_id = "standalone_composite"

        # Preserve the in-memory graph only if it already belongs to this same
        # image (load_image already established it for the current file).
        # Otherwise the graph is a previous image's leftover — replace it with a
        # fresh default. Each image owns its own composite.
        # NOTE: do NOT guard on requires_color_output_input() — that returns True
        # for ANY ColorOutput graph (the COLORNode's node_type is "color_input"),
        # so once the first image is loaded it would short-circuit here forever
        # and image B would silently keep image A's nodes.
        current_file = getattr(self, 'current_file_path', None)
        if getattr(self, "_composite_graph_image_path", None) == current_file:
            return True

        self.composite_page.switch_to_color_output_graph()
        self._composite_graph_image_path = current_file
        return True

    def _sync_timeline_playhead_for_composite_entry(self, timeline_frame: int) -> None:
        """Keep the Edit playback controller aligned with the Composite entry frame."""
        if self.timeline_panel is None:
            return
        try:
            current_frame = self.timeline_panel.get_playhead_frame()
        except (AttributeError, TypeError):
            current_frame = None
        if current_frame == timeline_frame:
            return
        try:
            self.timeline_panel.set_playhead_frame(timeline_frame)
        except Exception as exc:
            logger.warning(f"[Composite] Failed to align playhead for entry: {exc}")

    def _set_composite_playback_frame_sync(self, enabled: bool) -> None:
        """Connect Edit playback ticks to the active timeline-backed Composite input."""
        controller = None
        if self.timeline_panel is not None:
            try:
                controller = self.timeline_panel.get_playback_controller()
            except (AttributeError, TypeError):
                controller = None
        if controller is None or not hasattr(controller, "frame_changed"):
            return

        connected = bool(getattr(self, "_composite_playback_frame_sync_connected", False))
        if enabled and not connected:
            try:
                controller.frame_changed.connect(self._on_composite_playback_frame_changed)
                self._composite_playback_frame_sync_connected = True
                logger.debug("[Composite] Playback frame sync connected")
            except Exception as exc:
                logger.warning(f"[Composite] Failed to connect playback frame sync: {exc}")
        elif not enabled and connected:
            try:
                controller.frame_changed.disconnect(self._on_composite_playback_frame_changed)
            except (TypeError, RuntimeError):
                pass
            self._composite_playback_frame_sync_connected = False
            logger.debug("[Composite] Playback frame sync disconnected")

    def _configure_composite_timeline_playback_controls(self, enabled: bool) -> None:
        """Wire Composite viewer transport controls to Edit timeline playback."""
        # Image Color mode has no timeline, so there is nothing to wire.
        if getattr(getattr(self, "composite_page", None), "_is_image_color_mode", False):
            return
        viewer = getattr(self, "composite_viewer", None)
        bar = getattr(viewer, "video_control_bar", None)
        controller = None
        if self.timeline_panel is not None:
            try:
                controller = self.timeline_panel.get_playback_controller()
            except (AttributeError, TypeError):
                controller = None

        prev_controller = getattr(self, "_composite_controls_controller", None)
        frame_slot = getattr(self, "_composite_controls_frame_slot", None)
        state_slot = getattr(self, "_composite_controls_state_slot", None)
        if prev_controller is not None:
            if frame_slot is not None:
                try:
                    prev_controller.frame_changed.disconnect(frame_slot)
                except (TypeError, RuntimeError):
                    pass
            if state_slot is not None:
                try:
                    prev_controller.state_changed.disconnect(state_slot)
                except (TypeError, RuntimeError):
                    pass
        self._composite_controls_controller = None
        self._composite_controls_frame_slot = None
        self._composite_controls_state_slot = None
        # 拆除旧的 Keyframes 面板播放委托（可能挂在旧 controller 上）。
        kf_slot = getattr(self, "_kf_transport_state_slot", None)
        if kf_slot is not None and prev_controller is not None:
            try:
                prev_controller.state_changed.disconnect(kf_slot)
            except (TypeError, RuntimeError):
                pass
        self._kf_transport_state_slot = None
        # 拆除上一次连接的传输条槽闭包（BUG-C3-01）：13 个 bar 信号槽是
        # 每次调用新建的闭包，捕获当时的 controller/clip 区间。不先断开，
        # 重复进入 Composite 页会累积 N 份旧闭包（重复 seek/播放头被旧
        # clip 坐标系拽回）。与 _comp_clip_bar_slots 的对称写法一致。
        prev_bar_slots = getattr(self, "_composite_timeline_bar_slots", None)
        if prev_bar_slots is not None:
            prev_bar, prev_connections = prev_bar_slots
            for signal_name, prev_slot in prev_connections:
                signal = getattr(prev_bar, signal_name, None)
                while signal is not None:
                    try:
                        signal.disconnect(prev_slot)
                    except TypeError:
                        break
            self._composite_timeline_bar_slots = None
        # [AUD-3100] 时间线/comp-clip 两条路径共用同一个 viewport 播放条
        # 且激活条件互斥（is_comp_clip_playback_active ⟺ _runtime_context
        # 为 None）。本路径接管（enabled 且有 controller）前先拆掉另一条
        # 路径可能残留的 comp-clip 闭包，否则 bar 信号会同时驱动
        # controller.seek 与 page.comp_seek 双重触发。
        self._teardown_comp_clip_bar_slots()

        if bar is None or not enabled or controller is None:
            # 没有驱动 viewport 播放条的时间线控制器：让 Keyframes 面板回退
            # 到 comp-clip 播放引擎（或面板内部定时器）。
            page = getattr(self, "composite_page", None)
            if page is not None and hasattr(page, "set_keyframes_transport"):
                page.set_keyframes_transport(None)
            return

        page = getattr(self, "composite_page", None)
        # Comp clip 在时间线上的起始帧；transport bar 使用 clip 本地帧（0-based），
        # 而 controller 使用时间线全局帧。两者通过此偏移相互转换。
        clip_start = int(getattr(page, "_comp_clip_timeline_start", 0) or 0)
        # BUG-C3-02：comp_clip_total_frames() 返回 None/非法值时（轻量页面
        # 替身/旧实现），不能让 TypeError 打断整个装配——与 else 分支一致
        # 回退 controller.timeline_duration。
        clip_total = 0
        total_frames = getattr(page, "comp_clip_total_frames", None)
        if callable(total_frames):
            try:
                clip_total = int(total_frames() or 0)
            except (TypeError, ValueError):
                clip_total = 0
        if clip_total < 1:
            clip_total = max(1, int(getattr(controller, "timeline_duration", 1) or 1))

        def _to_local(timeline_frame: int) -> int:
            """时间线全局帧 → clip 本地帧（clamp 到 [0, clip_total-1]）。"""
            local = int(timeline_frame) - clip_start
            return max(0, min(local, clip_total - 1))

        def _on_seek(local_frame: int) -> None:
            target = int(local_frame) + clip_start
            seek = getattr(controller, "seek", None)
            if callable(seek):
                seek(target)
            else:
                callback = getattr(self, "on_video_seek", None)
                if callable(callback):
                    callback(target)

        def _on_scrub_started(local_frame: int) -> None:
            target = int(local_frame) + clip_start
            seek = getattr(controller, "seek", None)
            if callable(seek):
                seek(target)
            callback = getattr(self, "on_video_scrub_started", None)
            if callable(callback):
                callback(target)

        def _on_scrub_moved(local_frame: int) -> None:
            target = int(local_frame) + clip_start
            seek = getattr(controller, "seek", None)
            if callable(seek):
                seek(target)
            callback = getattr(self, "on_video_scrub_moved", None)
            if callable(callback):
                callback(target)

        def _on_scrub_finished(_local_frame: int) -> None:
            callback = getattr(self, "on_video_scrub_finished", None)
            if callable(callback):
                callback(int(_local_frame) + clip_start)

        def _on_play() -> None:
            # EditPlaybackController 没有 toggle_play，只有 play()/pause()。
            if getattr(controller, "is_playing", False):
                pause = getattr(controller, "pause", None)
                if callable(pause):
                    pause()
                else:
                    callback = getattr(self, "on_video_play_toggled", None)
                    if callable(callback):
                        callback()
            else:
                # 约束播放范围为 clip 区间（全局帧坐标），否则 tick() 会按整个
                # 时间线范围推进，出界后被 _clamp 钳到最后一帧，表现为一播放就跳到末尾。
                # 但已有 I/O 工作区（Keyframes 面板/播放条 I 标记）且在 clip
                # 区间内时保留它，不能重置为整个 clip，否则范围循环失效。
                if hasattr(controller, "set_playback_range"):
                    lo = clip_start
                    hi = clip_start + clip_total - 1
                    existing = getattr(controller, "playback_range", None)
                    keep = (
                        isinstance(existing, (tuple, list))
                        and len(existing) == 2
                        and existing[0] is not None
                        and existing[1] is not None
                        and lo <= int(existing[0])
                        and int(existing[1]) <= hi
                        and int(existing[0]) <= int(existing[1]))
                    if not keep:
                        controller.set_playback_range(lo, hi)
                play = getattr(controller, "play", None)
                if callable(play):
                    play()
                else:
                    callback = getattr(self, "on_video_play_toggled", None)
                    if callable(callback):
                        callback()

        def _on_next() -> None:
            target = int(getattr(controller, "current_frame", 0)) + 1
            seek = getattr(controller, "seek", None)
            if callable(seek):
                seek(target)
            else:
                callback = getattr(self, "on_video_next_frame", None)
                if callable(callback):
                    callback()

        def _on_prev() -> None:
            target = int(getattr(controller, "current_frame", 0)) - 1
            seek = getattr(controller, "seek", None)
            if callable(seek):
                seek(target)
            else:
                callback = getattr(self, "on_video_prev_frame", None)
                if callable(callback):
                    callback()

        def _on_first() -> None:
            seek = getattr(controller, "seek", None)
            if callable(seek):
                seek(clip_start)
            else:
                callback = getattr(self, "on_video_first_frame", None)
                if callable(callback):
                    callback()

        def _on_last() -> None:
            target = clip_start + clip_total - 1
            seek = getattr(controller, "seek", None)
            if callable(seek):
                seek(target)
            else:
                callback = getattr(self, "on_video_last_frame", None)
                if callable(callback):
                    callback()

        def _on_mark_in() -> None:
            cur = int(getattr(controller, "current_frame", 0) or 0)
            out_tl = getattr(controller, "_playback_range_end", None)
            if getattr(self, "_composite_default_playback_range", False):
                out_tl = None
            if hasattr(controller, "set_playback_range"):
                controller.set_playback_range(cur, out_tl)
            self._composite_default_playback_range = False
            bar.set_in_out_points(_to_local(cur), _to_local(out_tl) if out_tl is not None else None)

        def _on_mark_out() -> None:
            cur = int(getattr(controller, "current_frame", 0) or 0)
            in_tl = getattr(controller, "_playback_range_start", None)
            if in_tl is None:
                in_tl = clip_start
            if hasattr(controller, "set_playback_range"):
                controller.set_playback_range(in_tl, cur)
            self._composite_default_playback_range = False
            bar.set_in_out_points(_to_local(in_tl), _to_local(cur))

        def _on_clear_io() -> None:
            if hasattr(controller, "clear_playback_range"):
                controller.clear_playback_range()
            elif hasattr(controller, "set_playback_range"):
                controller.set_playback_range(None, None)
            bar.set_in_out_points(None, None)

        def _on_loop(enabled: bool) -> None:
            if hasattr(controller, "set_loop"):
                controller.set_loop(enabled)

        control_connections = (
            ("seek_requested", _on_seek),
            ("scrub_started", _on_scrub_started),
            ("scrub_moved", _on_scrub_moved),
            ("scrub_finished", _on_scrub_finished),
            ("play_toggled", _on_play),
            ("next_frame", _on_next),
            ("prev_frame", _on_prev),
            ("first_frame", _on_first),
            ("last_frame", _on_last),
            ("mark_in_requested", _on_mark_in),
            ("mark_out_requested", _on_mark_out),
            ("clear_in_out_requested", _on_clear_io),
            ("loop_toggled", _on_loop),
        )
        for signal_name, slot in control_connections:
            signal = getattr(bar, signal_name, None)
            if signal is None or slot is None:
                continue
            # 旧槽已在函数开头统一拆卸（BUG-C3-01），这里只连接新闭包。
            signal.connect(slot)
        # 记录本次连接的 (bar, 槽列表)，供下次进入页面时拆卸。
        self._composite_timeline_bar_slots = (bar, list(control_connections))

        try:
            bar.show_for_video_with_io()
            bar.set_total_frames(clip_total)
            # 进入合成页面时把播放头重置到片段开头（clip 本地帧 0）。
            seek = getattr(controller, "seek", None)
            if callable(seek):
                seek(clip_start)
                bar.on_frame_changed(0)
            else:
                bar.on_frame_changed(_to_local(getattr(controller, "current_frame", 0)))
            bar.set_frame_rate(float(getattr(controller, "fps", 24.0) or 24.0))
            bar.on_play_state_changed(bool(getattr(controller, "is_playing", False)))
            # 约束播放范围到 clip 区间 + 同步 loop 状态（默认循环播放）。
            if hasattr(controller, "set_playback_range"):
                controller.set_playback_range(clip_start, clip_start + clip_total - 1)
                self._composite_default_playback_range = True
            if hasattr(controller, "set_loop"):
                controller.set_loop(bar.btn_loop.isChecked())
            playback_range = getattr(controller, "playback_range", None)
            if playback_range:
                bar.set_in_out_points(
                    _to_local(playback_range[0]),
                    _to_local(playback_range[1]) if playback_range[1] is not None else None,
                )
        except (AttributeError, TypeError, ValueError):
            pass

        def frame_slot(timeline_frame) -> None:
            bar.on_frame_changed(_to_local(int(timeline_frame)))

        def state_slot(state: str) -> None:
            bar.on_play_state_changed(state == "playing")

        controller.frame_changed.connect(frame_slot)
        controller.state_changed.connect(state_slot)
        self._composite_controls_controller = controller
        self._composite_controls_frame_slot = frame_slot
        self._composite_controls_state_slot = state_slot

        # Keyframes 面板播放按钮 → 同一个 EditPlaybackController（与 viewport
        # 播放条同款引擎），双向同步播放状态，两边播放头保持一致。
        if hasattr(page, "set_keyframes_transport"):
            page.set_keyframes_transport(
                _KeyframesTimelineTransport(controller, clip_start, clip_total))
        if hasattr(page, "notify_external_play_state"):
            def kf_state_slot(state: str) -> None:
                page.notify_external_play_state(state == "playing")
            controller.state_changed.connect(kf_state_slot)
            self._kf_transport_state_slot = kf_state_slot

    def _teardown_comp_clip_bar_slots(self) -> None:
        """[AUD-3100] 拆除上一次 comp-clip 路径连接的传输条槽与帧刷新槽。

        与 ``_configure_composite_timeline_playback_controls`` 开头的对称
        拆卸互补：时间线/comp-clip 两条路径共用同一个 viewport 播放条。
        旧实现的拆卸位于 ``_configure_composite_comp_clip_controls`` 的
        早退判定之后，comp-clip 不再激活（回到时间线路径）或 Image
        Color 模式时 13 个旧闭包残留在 bar 上，与新接的时间线槽并存。
        拆卸使用存储的 (bar, slots)，宿主 viewer 变更后也能正确断开。
        """
        prev = getattr(self, "_comp_clip_bar_slots", None)
        if prev is not None:
            prev_bar, prev_slots = prev
            for signal_name, prev_slot in prev_slots:
                signal = getattr(prev_bar, signal_name, None)
                while signal is not None:
                    try:
                        signal.disconnect(prev_slot)
                    except TypeError:
                        break
            self._comp_clip_bar_slots = None
        prev_frame_wiring = getattr(self, "_comp_clip_frame_wiring", None)
        if prev_frame_wiring is not None:
            prev_signal, prev_slot = prev_frame_wiring
            while prev_signal is not None:
                try:
                    prev_signal.disconnect(prev_slot)
                except TypeError:
                    break
            self._comp_clip_frame_wiring = None

    def _configure_composite_comp_clip_controls(self) -> None:
        """Wire the Composite viewer transport bar to the comp-clip playback.

        The third playback path: an independent comp clip (created via
        "New Comp Clip") has no timeline/video controller, so CompositePage
        drives the playhead itself (comp_* methods + a QTimer). This connects
        the bottom VideoControlBar's transport + I/O signals to that engine,
        shows the bar, and seeds total-frames/current-frame.

        Safe to call repeatedly: disconnects prior comp-clip slots first.
        """
        # [AUD-3100] 拆卸必须在任何早退判定之前：comp-clip 不再激活 /
        # Image Color 模式 / viewer 缺失时，上一轮连接的 13 个闭包与
        # 帧刷新槽也要拆掉，否则与时间线路径的槽在同一 bar 上并存。
        self._teardown_comp_clip_bar_slots()
        # Image Color mode disables comp-clip playback entirely.
        if getattr(self.composite_page, "_is_image_color_mode", False):
            return
        viewer = getattr(self, "composite_viewer", None)
        bar = getattr(viewer, "video_control_bar", None)
        page = self.composite_page
        if bar is None or page is None or viewer is None:
            return
        # Only the comp-clip path uses this wiring.
        if not page.is_comp_clip_playback_active():
            return

        total = page.comp_clip_total_frames()
        cur = getattr(page, "_composite_current_frame", 0)

        # Transport slots bound to the comp-clip engine.
        def _on_seek(frame) -> None:
            page.comp_seek(int(frame))
            bar.set_current_frame(page._composite_current_frame)
            bar.set_time_info(self._format_comp_time(
                page._composite_current_frame,
                page.comp_clip_total_frames(),
                page.comp_clip_fps(),
            ))

        def _on_scrub_started(frame) -> None:
            if getattr(page, "_comp_playing", False):
                page.comp_toggle_play()
                bar.set_playing(False)
            _on_scrub_moved(frame)

        def _on_scrub_moved(frame) -> None:
            page.comp_seek(int(frame))
            bar.set_current_frame(page._composite_current_frame)
            bar.set_time_info(self._format_comp_time(
                page._composite_current_frame,
                page.comp_clip_total_frames(),
                page.comp_clip_fps(),
            ))

        def _on_scrub_finished(_frame):
            pass

        def _on_play() -> None:
            page.comp_toggle_play()
            bar.set_playing(page._comp_playing)

        def _on_next() -> None:
            page.comp_next_frame()
            bar.set_current_frame(page._composite_current_frame)

        def _on_prev() -> None:
            page.comp_prev_frame()
            bar.set_current_frame(page._composite_current_frame)

        def _on_first() -> None:
            page.comp_first_frame()
            bar.set_current_frame(page._composite_current_frame)

        def _on_last() -> None:
            page.comp_last_frame()
            bar.set_current_frame(page._composite_current_frame)

        def _on_mark_in() -> None:
            page.set_comp_clip_in_out(
                page._composite_current_frame, page._comp_out_frame)
            bar.set_in_out_points(page._comp_in_frame, page._comp_out_frame)

        def _on_mark_out() -> None:
            page.set_comp_clip_in_out(
                page._comp_in_frame, page._composite_current_frame)
            bar.set_in_out_points(page._comp_in_frame, page._comp_out_frame)

        def _on_clear_io() -> None:
            page.clear_comp_clip_in_out()
            bar.set_in_out_points(None, None)

        def _on_loop(enabled):
            # B2: bind the ⟳ button to comp-clip looping (was hardcoded before).
            page.set_comp_loop(bool(enabled))

        slots = [
            ("seek_requested", _on_seek),
            ("scrub_started", _on_scrub_started),
            ("scrub_moved", _on_scrub_moved),
            ("scrub_finished", _on_scrub_finished),
            ("play_toggled", _on_play),
            ("next_frame", _on_next),
            ("prev_frame", _on_prev),
            ("first_frame", _on_first),
            ("last_frame", _on_last),
            ("mark_in_requested", _on_mark_in),
            ("mark_out_requested", _on_mark_out),
            ("clear_in_out_requested", _on_clear_io),
            ("loop_toggled", _on_loop),
        ]
        # [AUD-3100] 连接前由函数开头的 _teardown_comp_clip_bar_slots 统一
        # 拆掉旧闭包（含另一条路径接管时残留的），这里只连接新槽。
        for signal_name, slot in slots:
            signal = getattr(bar, signal_name, None)
            if signal is None:
                continue
            signal.connect(slot)
        # 记录 (bar, 槽列表) 与帧刷新槽的信号来源，供下次（任意路径）
        # 进入页面时拆卸。
        self._comp_clip_bar_slots = (bar, list(slots))

        try:
            bar.show_for_video_with_io()
            bar.set_total_frames(max(1, total))
            bar.set_current_frame(int(cur))
            bar.set_frame_rate(page.comp_clip_fps())
            bar.set_playing(False)
            # Seed an fps-based time display.
            fps = page.comp_clip_fps()
            bar.set_time_info(self._format_comp_time(int(cur), total, fps))
            # B2: sync the page's loop flag to the bar's actual ⟳ button state
            # so they start in agreement (bar defaults to checked).
            if hasattr(bar, "btn_loop") and hasattr(page, "set_comp_loop"):
                page.set_comp_loop(bar.btn_loop.isChecked())
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            viewer.update_video_control_bar_position()
        except AttributeError:
            pass

        # Refresh the time/frame display as the playhead moves.
        def _refresh_display(frame) -> None:
            bar.set_current_frame(int(frame))
            bar.set_time_info(self._format_comp_time(
                int(frame), page.comp_clip_total_frames(), page.comp_clip_fps()))
        # [AUD-3100] 旧帧刷新槽已在函数开头统一拆卸（连信号源一起存储，
        # 早退路径也能拆），这里只连接新槽。
        fps_signal = getattr(page, "composite_frame_changed", None)
        if fps_signal is not None:
            fps_signal.connect(_refresh_display)
            self._comp_clip_frame_wiring = (fps_signal, _refresh_display)

    @staticmethod
    def _format_comp_time(frame: int, total: int, fps: float) -> str:
        """Format a simple HH:MM:SS / total timecode for the transport bar."""
        fps = max(1.0, float(fps))
        def _tc(f):
            f = max(0, int(f))
            total_s = f / fps
            h = int(total_s // 3600)
            m = int((total_s % 3600) // 60)
            s = total_s - h * 3600 - m * 60
            return f"{h:02d}:{m:02d}:{s:05.2f}"
        return f"{_tc(frame)} / {_tc(total)}"

    def _on_composite_playback_frame_changed(self, timeline_frame: int) -> None:
        """Advance Composite's timeline clip input when the Edit playhead moves."""
        if self.current_page != "Composite" or self.composite_page is None:
            return

        current_context = self._page_context_manager.get_current_context()
        clip_id = getattr(current_context, "clip_id", None)
        if not clip_id:
            return

        timeline_frame = int(timeline_frame)
        runtime_context = getattr(self.composite_page, "_runtime_context", None)
        if (
            runtime_context is not None
            and int(getattr(runtime_context, "timeline_frame", -1)) == timeline_frame
        ):
            return

        timeline = None
        timeline_id = getattr(current_context, "timeline_id", None)
        if timeline_id and hasattr(self, "_current_timeline_sequences"):
            timeline = self._current_timeline_sequences.get(timeline_id)
        if timeline is None and self.edit_page is not None:
            timeline = getattr(self.edit_page, "_current_timeline", None)
            timeline_id = getattr(timeline, "timeline_id", timeline_id)
        if timeline is None:
            return

        track = None
        clip = None
        for candidate_track in timeline.all_tracks:
            candidate_clip = candidate_track.get_clip_by_id(clip_id)
            if candidate_clip is not None:
                track = candidate_track
                clip = candidate_clip
                break
        if clip is None or track is None:
            return

        followed_clip = False
        if timeline_frame < clip.timeline_start or timeline_frame >= clip.timeline_end:
            # 播放推进到段边界时保持原"边界停止"行为；用户拖拽播放头（无论
            # 是否在播放）则跟随进入其下方的 clip——刀片切开的相邻段共享素材
            # 解码会话，切换零成本。此前每次越界都 pause+seek 回段边界，播放
            # 头被反复拽回、画面钉死在边界帧，切开多段后拖拽预览完全不动。
            if (
                self._composite_playback_is_transport_playing()
                and not self._composite_playhead_is_scrubbing()
            ):
                self._clamp_composite_playback_to_clip_range(timeline_frame, clip)
                return
            follow_clip = self._find_clip_under_composite_playhead(timeline, timeline_frame)
            if follow_clip is None:
                # 播放头下方是间隙：保持上一帧画面，不回弹播放头。
                return
            clip = follow_clip
            followed_clip = True

        try:
            source_frame = clip.timeline_to_source_frame(timeline_frame)
        except (ValueError, AttributeError):
            source_frame = getattr(clip, "source_start", 0)
        clip_local_frame = timeline_frame - clip.timeline_start
        if followed_clip:
            # 跟随到其它 clip 时不继承原绑定 clip 的调色栈/合成图/素材，
            # 否则会把 A 段的 comp graph 应用到 B 段画面上。
            color_stack_id = getattr(clip, "color_stack_id", None)
            composite_id = getattr(clip, "composite_id", None)
            asset_id = getattr(clip, "asset_id", None)
        else:
            color_stack_id = getattr(clip, "color_stack_id", None) or getattr(current_context, "color_stack_id", None)
            composite_id = getattr(clip, "composite_id", None) or getattr(current_context, "composite_id", None)
            asset_id = getattr(clip, "asset_id", None) or getattr(current_context, "asset_id", None)

        context = self._page_context_manager.create_context_for_edit_to_composite(
            clip_id=clip.clip_id,
            composite_id=composite_id,
            asset_id=asset_id,
            timeline_id=timeline_id,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
            color_stack_id=color_stack_id,
        )
        self._page_context_manager.set_current_context(context)
        if hasattr(self, "_setup_timeline_clip_context"):
            self._setup_timeline_clip_context(context, composite_id or "", clip.clip_id)

    def _composite_playback_is_transport_playing(self) -> bool:
        """Return whether the shared timeline transport is currently playing."""
        panel = getattr(self, "timeline_panel", None)
        if panel is None:
            return False
        try:
            controller = panel.get_playback_controller()
        except (AttributeError, TypeError):
            return False
        return bool(getattr(controller, "is_playing", False))

    def _composite_playhead_is_scrubbing(self) -> bool:
        """Return whether the user is currently dragging the shared playhead."""
        panel = getattr(self, "timeline_panel", None)
        return bool(getattr(panel, "_scrubbing_playhead", False))

    @staticmethod
    def _topmost_enabled_video_clip_at(timeline: Any, timeline_frame: int) -> Any:
        """播放头处最顶层可见视频 clip（统一分层约定，AUD-3103）。

        与 edit preview 的分层一致：video track ``index`` 最大（最上层）
        优先命中；``is_enabled=False`` 的 clip 视为不存在（gap）。
        Edit→Color 播放头跟随（``_switch_to_color_following_edit_playhead``）
        与 Composite 播放跟随（``_find_clip_under_composite_playhead``）两条
        路径共用本工具，消除"同一帧经不同入口打开不同 clip"的矛盾。
        """
        video_tracks = sorted(
            getattr(timeline, "video_tracks", ()) or (),
            key=lambda t: getattr(t, "index", 0),
            reverse=True,
        )
        for track in video_tracks:
            clip = track.get_clip_at(int(timeline_frame))
            if clip is not None and getattr(clip, "is_enabled", True):
                return clip
        return None

    def _find_clip_under_composite_playhead(self, timeline: Any, timeline_frame: int) -> Any:
        """Return the topmost enabled video clip covering ``timeline_frame``.

        Mirrors the edit preview's layering: the highest video-track index
        wins; disabled clips are treated as absent (gap).
        """
        return self._topmost_enabled_video_clip_at(timeline, timeline_frame)

    def _clamp_composite_playback_to_clip_range(self, timeline_frame: int, clip: Any) -> None:
        """Stop clip-level Composite playback at the source clip boundary."""
        controller = None
        if self.timeline_panel is not None:
            try:
                controller = self.timeline_panel.get_playback_controller()
            except (AttributeError, TypeError):
                controller = None
        if controller is None:
            return

        target = clip.timeline_start
        if timeline_frame >= clip.timeline_end:
            target = max(clip.timeline_start, clip.timeline_end - 1)

        try:
            if getattr(controller, "is_playing", False):
                controller.pause()
            if getattr(controller, "current_frame", None) != target:
                controller.seek(target)
        except Exception as exc:
            logger.warning(f"[Composite] Failed to clamp playback to clip range: {exc}")

    def _ensure_temporary_composition_for_clip(
        self,
        timeline,
        track,
        clip,
        color_stack_id: str | None = None,
    ) -> str | None:
        """DEPRECATED: Temporary composition auto-creation is disabled (Phase 1).

        This function is kept as a stub to prevent import errors but will
        always return None for normal clips. Only returns composite_id
        for clips that already have one.
        """
        if getattr(clip, 'composite_id', None):
            return clip.composite_id

        # Phase 1: do NOT auto-create temporary compositions
        logger.warning(
            "[Phase1] _ensure_temporary_composition_for_clip called but "
            "auto-creation is disabled. Use explicit Create Comp Clip instead."
        )
        return None

    def _resolve_composite_id_for_clip(self, clip) -> str | None:
        """Return an existing graph id without mutating the Edit project."""
        composite_id = getattr(clip, "composite_id", None)
        if composite_id:
            return str(composite_id)
        project = getattr(self, "_current_edit_project", None)
        if project is None:
            return None
        try:
            asset = project.get_asset_by_id(getattr(clip, "asset_id", ""))
        except (AttributeError, TypeError):
            asset = None
        if asset is not None and getattr(asset, "is_comp_clip", False):
            metadata = getattr(asset, "metadata", {}) or {}
            graph_id = metadata.get("graph_id")
            return str(graph_id) if graph_id else None
        return None

    def _should_block_implicit_composite_entry(self, from_page: str) -> bool:
        """Whether a direct page switch targets a normal, unbound clip."""
        clip = None
        if from_page == "Edit":
            timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
            if timeline is None or getattr(self, "timeline_panel", None) is None:
                return False
            try:
                selected_ids = self.timeline_panel.get_selected_clip_ids()
            except (AttributeError, TypeError):
                selected_ids = []
            if selected_ids:
                for track in timeline.all_tracks:
                    clip = track.get_clip_by_id(selected_ids[0])
                    if clip is not None:
                        break
            if clip is None:
                try:
                    frame = self.timeline_panel.get_playhead_frame()
                except (AttributeError, TypeError):
                    frame = 0
                for track in timeline.all_tracks:
                    clip = track.get_clip_at(frame)
                    if clip is not None:
                        break
        elif from_page == "Color":
            session = None
            manager = getattr(self, "_color_session_manager", None)
            if manager is not None:
                session = manager.get_current_session()
            if session is None or not getattr(
                session, "is_timeline_clip", lambda: False
            )():
                return False
            clip_id = getattr(session, "clip_id", None)
            timeline_id = getattr(session, "timeline_id", None)
            timeline = None
            sequences = getattr(self, "_current_timeline_sequences", {}) or {}
            if timeline_id:
                timeline = sequences.get(timeline_id)
            if timeline is None:
                timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
            if timeline is not None and clip_id:
                for track in timeline.all_tracks:
                    clip = track.get_clip_by_id(clip_id)
                    if clip is not None:
                        break
        return clip is not None and self._resolve_composite_id_for_clip(clip) is None

    # ================================================================
    # Page Context Coordination (P9)
    # ================================================================

    def switch_to_page_with_context(self, context) -> None:
        """
        带上下文的页面切换。

        根据上下文中的信息，执行相应的数据传递和状态初始化。

        Args:
            context: PageContext 实例，包含页面切换所需的所有信息
        """
        # Import PageType locally to avoid circular imports at module level

        target_page = context.target_page.value

        # 更新上下文管理器
        self._page_context_manager.set_current_context(context)

        # 先执行基础页面切换
        self._contextual_page_switch_in_progress = True
        try:
            self.switch_to_page(target_page)
        finally:
            self._contextual_page_switch_in_progress = False

        # 根据目标页面执行特定逻辑
        if target_page == "Color":
            self._handle_enter_color_with_context(context)
        elif target_page == "Composite":
            if getattr(self, '_composite_context_applied_during_enter', False):
                self._composite_context_applied_during_enter = False
            else:
                self._handle_enter_composite_with_context(context)
        elif target_page == "Deliver":
            self._handle_enter_deliver_with_context(context)
        elif target_page == "Edit":
            self._handle_enter_edit_with_context(context)

        logger.debug(f"Switched to {target_page} with context: {context.to_dict()}")

    def _handle_enter_color_with_context(self, context) -> None:
        """
        处理进入 Color 页面的上下文逻辑。

        支持 three modes:

        1. TIMELINE_CLIP + video:
           - load_video(for_timeline_clip=True), seek to source_frame
           - NO standalone ColorSession
        2. TIMELINE_CLIP + image:
           - load_image(create_standalone_session=False)
           - NO standalone image session
        3. Standalone image (SINGLE_ASSET or no clip_id):
           - load_image() with default create_standalone_session=True
           - Existing standalone behavior preserved

        After loading media:
        - Load color stack to grading panel
        - Set up grading save callback
        - Create ColorSession via ColorSessionManager

        Args:
            context: PageContext 实例
        """
        from src.app.main_window import PageType

        if context.source_page != PageType.EDIT:
            return

        asset_id = context.asset_id
        clip_id = context.clip_id
        if not asset_id:
            logger.warning("Edit → Color switch without asset_id")
            return

        # 从 EditProject 获取 asset 信息
        if not self._current_edit_project:
            logger.warning("No current EditProject for Edit → Color switch")
            return

        asset = self._current_edit_project.assets.get(asset_id)
        if not asset:
            logger.warning(f"Asset not found: {asset_id}")
            return
        asset_metadata = getattr(asset, "metadata", None) or {}
        self._update_color_monitor_for_asset(asset, asset_metadata)
        _emit_color_clip_debug(
            "[COLOR_CLIP_DEBUG] enter.context clip=%s timeline=%s asset=%s "
            "context_timeline_frame=%s context_source_frame=%s asset_path=%s "
            "asset_meta_is_subclip=%s asset_meta_source_start=%s asset_meta_source_duration=%s",
            clip_id,
            context.timeline_id,
            asset_id,
            getattr(context, "timeline_frame", None),
            getattr(context, "source_frame", None),
            getattr(asset, "path", None),
            asset_metadata.get("is_subclip") if isinstance(asset_metadata, dict) else getattr(asset_metadata, "is_subclip", None),
            asset_metadata.get("source_start") if isinstance(asset_metadata, dict) else getattr(asset_metadata, "source_start", None),
            asset_metadata.get("source_duration") if isinstance(asset_metadata, dict) else getattr(asset_metadata, "source_duration", None),
        )

        # Determine mode: TIMELINE_CLIP vs standalone
        is_timeline_clip = bool(clip_id and context.timeline_id)

        # Arm the flash guard BEFORE the flush: flush/fit/hydration submit
        # grading and viewport RENDER_FRAME commands, and without the guard
        # those execute against the departing clip's video planes (the
        # previous shot flashes for one frame; a grading drag started right
        # after selecting the clip lands inside that window).  The guard
        # discards queued refresh lanes, bumps the video generation so any
        # command already in flight is rejected by the stale-generation
        # gate, and arms the pending gate.
        self._color_clip_switch_guard_armed = False
        if asset.is_video and is_timeline_clip:
            video_controller = getattr(self, "video_controller", None)
            arm_guard = getattr(video_controller, "begin_clip_switch_preview_guard", None)
            if callable(arm_guard):
                try:
                    arm_guard()
                    self._color_clip_switch_guard_armed = True
                except Exception:
                    logger.debug("Failed to arm Color clip switch preview guard", exc_info=True)

        # A grading drag is coalesced on a short UI timer. Flush the active
        # clip before a new asset load can replace its controls or engine
        # layers. The explicit target ID is available for normal Edit/Color
        # navigation and avoids saving when simply reopening the same clip.
        flush_active_stack = getattr(self, '_flush_active_grading_stack_before_switch', None)
        if callable(flush_active_stack):
            flush_active_stack(context.color_stack_id)
        timeline_display_size: tuple[int, int] | None = None
        timeline_clip = None
        if is_timeline_clip:
            timeline = self._current_timeline_sequences.get(context.timeline_id)
            if timeline is not None:
                timeline_display_size = (int(timeline.width), int(timeline.height))
                try:
                    timeline_clip = timeline.get_clip_by_id(clip_id)
                except AttributeError:
                    timeline_clip = None
        if timeline_clip is not None:
            _emit_color_clip_debug(
                "[COLOR_CLIP_DEBUG] enter.timeline_clip clip=%s timeline_start=%s "
                "timeline_duration=%s source_start=%s source_duration=%s",
                clip_id,
                getattr(timeline_clip, "timeline_start", None),
                getattr(timeline_clip, "duration", None),
                getattr(timeline_clip, "source_start", None),
                getattr(timeline_clip, "source_duration", None),
            )

        # Color grades the source clip, not the Edit timeline composite.  A
        # timeline can use a delivery/canvas aspect that differs from its
        # media; making that size the Color viewer's scene squeezed every
        # decoded frame into that unrelated aspect during playback.  Keep the
        # timeline only for clip/frame mapping and let the Color canvas take
        # its presentation aspect from the decoded media, just like Program.
        if hasattr(self.canvas, "set_display_scene_size_override"):
            self.canvas.set_display_scene_size_override(None)
        if self.video_controller and hasattr(self.video_controller, "clear_display_scene_size"):
            self.video_controller.clear_display_scene_size()

        # ── Load media into Color engine ──────────────────────────────
        if asset.is_video:
            if not is_timeline_clip:
                logger.warning(
                    "Edit → Color video rejected without timeline clip context: "
                    f"asset_id={asset_id}, clip_id={clip_id}, timeline_id={context.timeline_id}"
                )
                return
            # TIMELINE_CLIP + video: seek to source_frame (the frame in
            # the source media file), NOT timeline_frame (position on
            # the timeline).
            source_frame = int(context.source_frame or 0)
            if timeline_clip is not None:
                source_frame = self._normalize_color_clip_source_frame(
                    timeline_clip,
                    asset,
                    source_frame,
                )
                context.source_frame = source_frame
                _emit_color_clip_debug(
                    "[COLOR_CLIP_DEBUG] enter.normalized clip=%s normalized_source_frame=%s",
                    clip_id,
                    source_frame,
                )
            if timeline_clip is not None and hasattr(self, "_set_color_timeline_clip_playback_context"):
                effective_source_start = self._normalize_color_clip_source_frame(
                    timeline_clip,
                    asset,
                    int(getattr(timeline_clip, "source_start", 0) or 0),
                )
                self._set_color_timeline_clip_playback_context(
                    context.timeline_id,
                    timeline_clip,
                    effective_source_start=effective_source_start,
                )
                _emit_color_clip_debug(
                    "[COLOR_CLIP_DEBUG] enter.playback_context clip=%s effective_source_start=%s",
                    clip_id,
                    effective_source_start,
                )
                self._fit_color_timeline_clip_viewport(
                    context,
                    stage="before-immediate-preview",
                )
            controller_ready = bool(
                self.video_controller
                and self.video_controller.is_video_loaded()
            )
            controller_path = getattr(self.video_controller, "current_path", None)
            should_load_video = (
                self.video_controller is None
                or self.current_file_path != asset.path
                or (not controller_ready and controller_path != asset.path)
            )
            _emit_color_clip_debug(
                "[COLOR_CLIP_DEBUG] enter.load_decision clip=%s should_load=%s "
                "controller_ready=%s current_file=%s controller_path=%s asset_path=%s",
                clip_id,
                should_load_video,
                controller_ready,
                self.current_file_path,
                controller_path,
                asset.path,
            )
            if should_load_video:
                _emit_color_clip_debug(
                    "[COLOR_CLIP_DEBUG] enter.before_load clip=%s source_frame=%s path=%s",
                    clip_id,
                    source_frame,
                    asset.path,
                )
                self._run_color_stack_hydration_step(
                    lambda: self.load_video(
                        asset.path,
                        for_timeline_clip=True,
                        display_size=None,
                    ),
                )
                _emit_color_clip_debug(
                    "[COLOR_CLIP_DEBUG] enter.after_load clip=%s is_loaded=%s "
                    "vc_current=%s controller_path=%s",
                    clip_id,
                    bool(
                        self.video_controller
                        and self.video_controller.is_video_loaded()
                    ),
                    getattr(self.video_controller, "current_frame", None),
                    getattr(self.video_controller, "current_path", None),
                )
            if hasattr(self, "_sync_color_timeline_clip_video_controls"):
                self._sync_color_timeline_clip_video_controls()
            # Color grading must flow through the ImageEngine GLSL pipeline
            # (Canvas.set_image -> engine.render_pipeline -> AsyncRenderer ->
            # drawBackground blit), which fuses the full grade stack (curves,
            # LUT3D, mixer, HSL, soft_clip, grain).  The shared CPU
            # TimelinePreviewRenderer only applies the primary-grade subset and
            # had no pre-buffering, so driving playback through it made every
            # param adjustment invisible and stalled at each clip boundary.
            # Keep transport on the graded video_controller path instead; the
            # crash guards in timeline_view/timeline_panel still protect the
            # playhead sync that _sync_color_timeline_playhead drives.
            # activate_timeline_preview = getattr(self, "_activate_color_timeline_preview", None)
            # if callable(activate_timeline_preview):
            #     activate_timeline_preview(context)

        elif asset.is_image:
            if hasattr(self, "_clear_color_timeline_clip_playback_context"):
                self._clear_color_timeline_clip_playback_context()
            if is_timeline_clip:
                # TIMELINE_CLIP + image: do NOT create standalone session
                if self.current_file_path != asset.path:
                    self._run_color_stack_hydration_step(
                        lambda: self.load_image(
                            asset.path,
                            create_standalone_session=False,
                        ),
                    )
            else:
                # Standalone image (SINGLE_ASSET / no clip_id):
                # keep existing behavior with default create_standalone_session=True
                if self.current_file_path != asset.path:
                    self._run_color_stack_hydration_step(
                        lambda: self.load_image(asset.path),
                    )

        if is_timeline_clip and timeline_display_size is not None:
            self._fit_color_timeline_clip_viewport(
                context,
                stage="after-media-load",
            )

        # ── Color stack handling ──────────────────────────────────────
        color_stack_id = context.color_stack_id

        # If no color_stack_id provided, try to get from clip
        if not color_stack_id and clip_id and self.edit_page is not None and self.edit_page._current_timeline:
            timeline = self.edit_page._current_timeline
            clip = None
            for track in timeline.all_tracks:
                c = track.get_clip_by_id(clip_id)
                if c:
                    clip = c
                    break

            if clip:
                color_stack_id = clip.color_stack_id

        # Auto-create color stack if clip has no stack (P9.1)
        if clip_id and not color_stack_id:
            color_stack_id = self._ensure_clip_has_color_stack(clip_id, asset_id)

        # Seek the new clip's first frame BEFORE hydrating the grading panel.
        # Hydration triggers grading-panel signals that submit RENDER_FRAME
        # commands; if the new frame's RENDER_VIDEO_FRAME has not been queued
        # yet, those RENDER_FRAME commands reuse the departing clip's video
        # texture (the renderer's current_video_texture) and briefly flash the
        # previous shot on the Canvas and DeckLink monitor.  Seeking first
        # ensures the worker processes the new frame before any hydration-
        # triggered RENDER_FRAME, so the latter reads the correct texture.
        if asset.is_video and is_timeline_clip:
            self._show_color_timeline_clip_static_preview(context)

        # Load color stack to grading panel
        if color_stack_id:
            # Bind the target callback before changing panel values. During
            # hydration, suppress all write-backs so a delayed UI signal can
            # neither overwrite the departing clip nor save defaults into the
            # target stack.
            if callable(flush_active_stack):
                flush_active_stack(color_stack_id)
            self._setup_grading_save_callback(color_stack_id)
            self._color_stack_hydration_in_progress = True
            try:
                self._load_color_stack_to_grading_panel(color_stack_id)
            finally:
                self._color_stack_hydration_in_progress = False

        # ── Create ColorSession for save routing ─────────────────────
        project_id = self._current_edit_project.project_id
        source_path = asset.path

        self._color_session_manager.create_from_page_context(
            context=context,
            project_id=project_id,
            color_stack_id=color_stack_id,
            source_path=source_path,
        )

        # Update the timeline clip strip's active clip highlight
        if clip_id and hasattr(self, 'color_timeline_clip_strip'):
            self.color_timeline_clip_strip.set_active_clip(clip_id)

        logger.info(f"ColorSession created: {self._color_session_manager.get_current_session()}")

    def _show_color_timeline_clip_static_preview(
        self,
        context,
        *,
        retry_count: int = 0,
        token: int | None = None,
    ) -> None:
        """Force the Color viewer's paused preview to the active clip frame.

        ColorStack hydration can submit a viewport refresh using the previous
        video texture.  Re-seeking after hydration makes the final submitted
        frame match the newly selected Timeline Clip instead of the prior shot.
        """
        if token is None:
            token = int(getattr(self, "_color_static_preview_token", 0) or 0) + 1
            self._color_static_preview_token = token
        elif token != getattr(self, "_color_static_preview_token", None):
            return

        video_controller = getattr(self, "video_controller", None)
        if video_controller is None:
            self._disarm_color_clip_switch_guard(video_controller)
            return
        # Playback supersedes the paused static preview.  The retry chain and
        # its retry-exhausted exact seek call stop_playback(); without this
        # bail-out, a Space press inside the async master-decode window starts
        # playback and the late preview transaction kills it, which reads as
        # "the page is not activated" until the user clicks and retries.
        if bool(getattr(video_controller, "is_playing", False)):
            self._disarm_color_clip_switch_guard(video_controller)
            return
        is_loaded = getattr(video_controller, "is_video_loaded", None)
        if callable(is_loaded):
            try:
                if not is_loaded():
                    self._disarm_color_clip_switch_guard(video_controller)
                    return
            except Exception:
                self._disarm_color_clip_switch_guard(video_controller)
                return
        source_frame = int(getattr(context, "source_frame", 0) or 0)
        preview_key = (int(token), source_frame)
        _emit_color_clip_debug(
            "[COLOR_CLIP_PREVIEW] request clip=%s asset=%s source_frame=%s retry=%s",
            getattr(context, "clip_id", None),
            getattr(context, "asset_id", None),
            source_frame,
            retry_count,
        )
        if getattr(self, "_color_static_preview_rendered_key", None) == preview_key:
            if hasattr(self, "_sync_color_timeline_clip_video_controls"):
                self._sync_color_timeline_clip_video_controls()
            _emit_color_clip_debug(
                "[COLOR_CLIP_DEBUG] preview.skip_already_rendered clip=%s "
                "asset=%s source_frame=%s token=%s",
                getattr(context, "clip_id", None),
                getattr(context, "asset_id", None),
                source_frame,
                token,
            )
            return

        # Fast completion for same-clip reselection: the clip-switch guard
        # armed pending and withdrew the front texture, but the requested
        # frame is already the presented one.  No master frame will arrive
        # for a request the coordinator already served, so restore the
        # presentation instead of waiting out the retry window.
        shows_frame = getattr(video_controller, "static_preview_shows_frame", None)
        if (
            callable(shows_frame)
            and bool(getattr(video_controller, "_last_static_preview_delivered", False))
            and bool(shows_frame(source_frame))
        ):
            withdraw = getattr(video_controller, "withdraw_static_preview_pending", None)
            if callable(withdraw):
                try:
                    withdraw()
                except Exception:
                    logger.debug("Failed to withdraw Color clip switch guard", exc_info=True)
            self._color_clip_switch_guard_armed = False
            self._color_static_preview_rendered_key = preview_key
            if hasattr(self, "_sync_color_timeline_clip_video_controls"):
                self._sync_color_timeline_clip_video_controls()
            _emit_color_clip_debug(
                "[COLOR_CLIP_DEBUG] preview.skip_already_displayed clip=%s "
                "asset=%s source_frame=%s token=%s",
                getattr(context, "clip_id", None),
                getattr(context, "asset_id", None),
                source_frame,
                token,
            )
            return

        rendered = False
        preview = getattr(video_controller, "request_static_preview_frame", None)
        if callable(preview):
            rendered = bool(preview(source_frame))
        elif not (
            getattr(video_controller, "_use_dual_path", False)
            and getattr(video_controller, "_coordinator", None) is not None
        ):
            try:
                video_controller.seek_to_frame(source_frame)
                rendered = True
            except Exception:
                rendered = False
        if hasattr(self, "_sync_color_timeline_clip_video_controls"):
            self._sync_color_timeline_clip_video_controls()
        if rendered:
            # The replacement frame is queued; its RENDER_VIDEO_FRAME upload
            # clears the switch guard. Remember the delivered key so a
            # same-clip reselection can fast-complete without another seek.
            self._color_static_preview_rendered_key = preview_key
            self._color_clip_switch_guard_armed = False
        _emit_color_clip_debug(
            "[COLOR_CLIP_DEBUG] preview.requested clip=%s asset=%s source_frame=%s "
            "rendered=%s vc_current_frame=%s",
            getattr(context, "clip_id", None),
            getattr(context, "asset_id", None),
            source_frame,
            rendered,
            getattr(video_controller, "current_frame", None),
        )
        if _color_clip_debug_enabled():
            QTimer.singleShot(
                0,
                lambda: self._log_color_viewport_debug(context, "after-preview-0ms"),
            )
            QTimer.singleShot(
                120,
                lambda: self._log_color_viewport_debug(context, "after-preview-120ms"),
            )
            QTimer.singleShot(
                350,
                lambda: self._log_color_viewport_debug(context, "after-preview-350ms"),
            )

        if not rendered and retry_count < 8:
            QTimer.singleShot(
                50,
                lambda: self._show_color_timeline_clip_static_preview(
                    context,
                    retry_count=retry_count + 1,
                    token=token,
                ),
            )
        elif not rendered:
            # Color is a paused, precision-first workflow.  If the async
            # master worker has not returned promptly, use the controller's
            # bounded exact seek rather than leaving the previous clip visible
            # indefinitely or substituting a thumbnail cache.  This fetches
            # the requested source frame at original quality.
            exact_seek = getattr(video_controller, "seek_to_frame", None)
            exact_seek_ok = False
            if callable(exact_seek):
                try:
                    exact_seek(source_frame, force_display=True)
                    exact_seek_ok = True
                except TypeError:
                    try:
                        exact_seek(source_frame)
                        exact_seek_ok = True
                    except Exception as exc:
                        logger.debug("Color clip exact static seek failed: %s", exc)
                except Exception as exc:
                    logger.debug("Color clip exact static seek failed: %s", exc)

            # [AUD-3101] 旧实现对所有控制器无差别地以相同 retry_count
            # 重新调度自身，重试耗尽后进入不设上限的 100ms 无限重试环
            # （每 100ms 一次 force_display seek 风暴）。现在：实现了
            # exact-seek 接口的控制器在上述一次 force_display 后就此
            # 止步（帧上传由渲染管线回填）；仅真 legacy（无 seek_to_frame
            # 接口）控制器保留重试，且同样受硬上限约束；彻底放弃时
            # 撤防 clip-switch guard（视口不再永久黑屏）并记 warning。
            legacy_controller = not callable(exact_seek)
            if legacy_controller and retry_count < _COLOR_STATIC_PREVIEW_MAX_RETRIES:
                QTimer.singleShot(
                    100,
                    lambda: self._show_color_timeline_clip_static_preview(
                        context,
                        retry_count=retry_count + 1,
                        token=token,
                    ),
                )
            elif not exact_seek_ok:
                logger.warning(
                    "[COLOR_CLIP_PREVIEW] static preview gave up after %d "
                    "retries (clip=%s asset=%s source_frame=%s); disarming "
                    "clip switch guard",
                    retry_count,
                    getattr(context, "clip_id", None),
                    getattr(context, "asset_id", None),
                    source_frame,
                )
                self._disarm_color_clip_switch_guard(video_controller)

    def _disarm_color_clip_switch_guard(self, video_controller) -> None:
        """Withdraw the clip-switch flash guard when no replacement frame comes.

        The guard arms ``video_texture_pending`` and withdraws the front
        texture at transaction start.  If the static-preview path then
        aborts (no controller, media not loaded), nothing would ever clear
        it and the viewport would stay blank; disarm it explicitly.
        """
        if not bool(getattr(self, "_color_clip_switch_guard_armed", False)):
            return
        self._color_clip_switch_guard_armed = False
        withdraw = getattr(video_controller, "withdraw_static_preview_pending", None)
        if callable(withdraw):
            try:
                withdraw()
            except Exception:
                logger.debug("Failed to disarm Color clip switch guard", exc_info=True)

    def _fit_color_timeline_clip_viewport(self, context, *, stage: str) -> bool:
        """Reset Color timeline clip preview to a whole-frame fitted viewport."""
        canvas = getattr(self, "canvas", None)
        fit_to_window = getattr(canvas, "fit_to_window", None)
        if not callable(fit_to_window):
            return False
        try:
            fit_to_window()
        except Exception as exc:
            logger.debug("Color timeline clip fit-to-window failed: %s", exc)
            return False
        if _color_clip_debug_enabled():
            self._log_color_viewport_debug(context, f"fit-{stage}")
        return True

    def _show_color_timeline_clip_immediate_thumbnail_preview(
        self,
        context,
        *,
        source_frame: int,
    ) -> bool:
        """Paint a fast clip-frame hint before any slow video reload/hydration."""
        token = int(getattr(self, "_color_static_preview_token", 0) or 0) + 1
        self._color_static_preview_token = token
        rendered = self._request_color_clip_thumbnail_static_preview(
            context,
            source_frame=int(source_frame),
            token=token,
        )
        fallback_rendered = False
        if not rendered:
            video_controller = getattr(self, "video_controller", None)
            if video_controller is not None:
                preview = getattr(video_controller, "request_static_preview_frame", None)
                if callable(preview):
                    fallback_rendered = bool(preview(int(source_frame)))
                elif not (
                    getattr(video_controller, "_use_dual_path", False)
                    and getattr(video_controller, "_coordinator", None) is not None
                ):
                    seek = getattr(video_controller, "seek_to_frame", None)
                    if callable(seek):
                        try:
                            seek(int(source_frame))
                            fallback_rendered = True
                        except Exception:
                            fallback_rendered = False
            rendered = rendered or fallback_rendered
        _emit_color_clip_debug(
            "[COLOR_CLIP_DEBUG] preview.immediate_thumbnail clip=%s asset=%s "
            "source_frame=%s rendered=%s fallback_rendered=%s token=%s",
            getattr(context, "clip_id", None),
            getattr(context, "asset_id", None),
            source_frame,
            rendered,
            fallback_rendered,
            token,
        )
        if _color_clip_debug_enabled():
            QTimer.singleShot(
                0,
                lambda: self._log_color_viewport_debug(context, "after-immediate-thumb-0ms"),
            )
        return rendered

    def _request_color_clip_thumbnail_static_preview(
        self,
        context,
        *,
        source_frame: int,
        token: int,
    ) -> bool:
        """Show the clicked clip's first frame using the thumbnail provider path.

        TimelineClipCard thumbnails already use this provider with
        ``clip.source_start``.  Reusing it here gives Color's viewport a
        deterministic first-frame response even while the playback coordinator is
        still seeking or warming its decode cache.
        """
        asset_id = getattr(context, "asset_id", None)
        if not asset_id or not getattr(self, "_current_edit_project", None):
            return False

        asset = self._current_edit_project.assets.get(asset_id)
        asset_path = getattr(asset, "path", None)
        if not asset_path:
            return False
        preview_key = (int(token), int(source_frame))
        if getattr(self, "_color_static_preview_rendered_key", None) == preview_key:
            return True

        provider = None
        try:
            from src.shared.media.thumbnail_provider import ThumbnailProvider

            provider = ThumbnailProvider.instance()
        except Exception as exc:
            logger.debug("Color clip thumbnail preview provider unavailable: %s", exc)
            return False

        # Disconnect the previous one-shot thumbnail listener so rapid clip
        # clicks cannot leave stale callbacks that repaint the wrong clip later.
        previous = getattr(self, "_color_static_preview_thumb_subscription", None)
        if previous:
            previous_provider, previous_slot = previous
            try:
                previous_provider.frame_thumbnail_ready.disconnect(previous_slot)
            except (TypeError, RuntimeError):
                pass
            self._color_static_preview_thumb_subscription = None

        rendered = False
        rendered_cache_height = None
        for target_height in (720, 512, 144):
            try:
                cached = provider.get_cached_frame_pixmap(
                    asset_id,
                    int(source_frame),
                    target_height,
                    path=asset_path,
                )
            except Exception:
                cached = None
            if cached:
                rendered = self._submit_color_clip_thumbnail_pixmap(
                    cached,
                    source_frame=source_frame,
                    token=token,
                )
                if rendered:
                    _emit_color_clip_debug(
                        "[COLOR_CLIP_PREVIEW] thumbnail-cache clip=%s frame=%s height=%s",
                        getattr(context, "clip_id", None),
                        source_frame,
                        target_height,
                    )
                    rendered_cache_height = int(target_height)
                    break
        if rendered and rendered_cache_height == 720:
            return True

        total_frames = self._color_clip_preview_total_frames(context, source_frame)

        def _on_frame_thumbnail_ready(path: str, frame_index: int, pixmap) -> None:
            if token != getattr(self, "_color_static_preview_token", None):
                return
            if path != asset_path or int(frame_index) != int(source_frame):
                return
            try:
                provider.frame_thumbnail_ready.disconnect(_on_frame_thumbnail_ready)
            except (TypeError, RuntimeError):
                pass
            self._color_static_preview_thumb_subscription = None
            if self._submit_color_clip_thumbnail_pixmap(
                pixmap,
                source_frame=source_frame,
                token=token,
            ):
                _emit_color_clip_debug(
                    "[COLOR_CLIP_PREVIEW] thumbnail-submit clip=%s frame=%s",
                    getattr(context, "clip_id", None),
                    source_frame,
                )

        try:
            provider.frame_thumbnail_ready.connect(_on_frame_thumbnail_ready)
            self._color_static_preview_thumb_subscription = (
                provider,
                _on_frame_thumbnail_ready,
            )
            provider.request_frame_pixmap_async(
                asset_id,
                asset_path,
                int(source_frame),
                total_frames,
                target_height=720,
            )
        except Exception as exc:
            logger.debug("Color clip thumbnail preview request failed: %s", exc)

        return rendered

    def _color_clip_preview_total_frames(self, context, source_frame: int) -> int:
        """估算 Color 剪辑静态预览的总帧数（元数据优先，回退 clip 时长）。"""
        asset_id = getattr(context, "asset_id", None)
        asset = None
        if asset_id and getattr(self, "_current_edit_project", None):
            asset = self._current_edit_project.assets.get(asset_id)
        metadata = getattr(asset, "metadata", None) or {}
        if isinstance(metadata, dict):
            total = metadata.get("total_frames") or metadata.get("frame_count")
        else:
            total = (
                getattr(metadata, "total_frames", None)
                or getattr(metadata, "frame_count", None)
            )
        try:
            return max(1, int(total))
        except (TypeError, ValueError):
            pass
        clip_duration = 0
        context_clip = getattr(self, "_color_timeline_clip_playback_context", None)
        if context_clip:
            clip = context_clip.get("clip")
            try:
                clip_duration = int(getattr(clip, "source_duration", 0) or 0)
            except (TypeError, ValueError):
                clip_duration = 0
        return max(1, int(source_frame) + max(1, clip_duration))

    def _submit_color_clip_thumbnail_pixmap(
        self,
        pixmap,
        *,
        source_frame: int,
        token: int,
    ) -> bool:
        """把缩略图 pixmap 提交为 Color 页静态预览帧。

        token 与当前 ``_color_static_preview_token`` 不匹配（切换已被
        新请求取代）或转换失败时返回 False；优先走 video_controller 的
        submit_static_preview_array，测试/旧控制器回退到 engine.set_image
        + 视口重绘。

        Args:
            pixmap: 缩略图 QPixmap。
            source_frame: 该帧在源媒体中的帧号。
            token: 请求令牌，用于丢弃过期预览。
        """
        if token != getattr(self, "_color_static_preview_token", None):
            return False
        if pixmap is None:
            return False
        frame = self._pixmap_to_bgr_array(pixmap)
        if frame is None:
            return False

        video_controller = getattr(self, "video_controller", None)
        submit = getattr(video_controller, "submit_static_preview_array", None)
        if callable(submit) and submit(frame, int(source_frame)):
            self._color_static_preview_rendered_key = (int(token), int(source_frame))
            return True

        # Fallback for tests or legacy controllers: route through the Color
        # engine as a video frame, then repaint the viewport.
        engine = getattr(self, "engine", None)
        if engine is not None:
            try:
                engine.set_image(
                    frame,
                    frame,
                    frame,
                    is_new_file=False,
                    is_video=True,
                    video_path=getattr(self, "current_file_path", None),
                )
            except Exception as exc:
                logger.debug("Color thumbnail preview engine fallback failed: %s", exc)
                return False
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            try:
                canvas.viewport().update()
            except Exception:
                pass
        self._color_static_preview_rendered_key = (int(token), int(source_frame))
        return True

    @staticmethod
    def _pixmap_to_bgr_array(pixmap):
        """QPixmap → BGR uint8 ndarray（RGB888 转换并反转通道），失败返回 None。"""
        try:
            import numpy as np
            from PyQt6.QtGui import QImage

            image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
            width = image.width()
            height = image.height()
            if width <= 0 or height <= 0:
                return None
            ptr = image.bits()
            ptr.setsize(image.sizeInBytes())
            arr = np.frombuffer(ptr, dtype=np.uint8).reshape((height, image.bytesPerLine()))
            rgb = arr[:, : width * 3].reshape((height, width, 3)).copy()
            return np.ascontiguousarray(rgb[:, :, ::-1])
        except Exception as exc:
            logger.debug("Failed to convert thumbnail pixmap to BGR frame: %s", exc)
            return None

    def _log_color_viewport_debug(self, context, stage: str) -> None:
        """输出 Color 视口状态调试快照（播放器/渲染器/引擎/画布缩放）。

        Args:
            context: PageContext（取 clip_id/source_frame）。
            stage: 调用阶段标签（如 thumbnail/static_preview）。
        """
        video_controller = getattr(self, "video_controller", None)
        engine = getattr(self, "engine", None)
        renderer = getattr(engine, "renderer", None)
        canvas = getattr(self, "canvas", None)
        zoom = None
        try:
            transform = canvas.transform() if canvas is not None else None
            zoom = transform.m11() if transform is not None else None
        except Exception:
            zoom = None
        playback_context = getattr(self, "_color_timeline_clip_playback_context", None) or {}
        _emit_color_clip_debug(
            "[COLOR_CLIP_DEBUG] viewport.stage=%s clip=%s context_source_frame=%s "
            "vc_current_frame=%s vc_last_displayed=%s renderer_current_video=%s "
            "renderer_last_video=%s renderer_command=%s renderer_reason=%s "
            "front_tex=%s front_size=%s engine_visible_rect=%s engine_scene_size=%s "
            "engine_viewport_size=%s canvas_zoom=%s playback_effective_start=%s",
            stage,
            getattr(context, "clip_id", None),
            getattr(context, "source_frame", None),
            getattr(video_controller, "current_frame", None),
            getattr(video_controller, "_last_displayed_frame_index", None),
            getattr(renderer, "current_video_frame_index", None),
            getattr(renderer, "debug_last_rendered_video_frame_index", None),
            getattr(renderer, "debug_last_render_command", None),
            getattr(renderer, "debug_last_render_reason", None),
            getattr(renderer, "current_front_tex_id", None),
            getattr(renderer, "current_front_size", None),
            getattr(engine, "visible_rect", None),
            getattr(engine, "scene_size", None),
            getattr(engine, "viewport_size", None),
            zoom,
            playback_context.get("effective_source_start"),
        )

    def _run_color_stack_hydration_step(self, operation: Any) -> Any:
        """Run a media/UI reset without persisting its temporary defaults.

        ``load_video`` deliberately resets grading controls before opening a
        different source. Those resets emit ``grading_changed``. During a
        Timeline Clips switch the panel still belongs to the departing clip,
        so letting that signal through would overwrite its saved stack with
        identity values before the target stack is loaded.
        """
        previous_state = getattr(self, '_color_stack_hydration_in_progress', False)
        self._color_stack_hydration_in_progress = True
        try:
            return operation()
        finally:
            self._color_stack_hydration_in_progress = previous_state

    def _create_standalone_image_color_session(
        self,
        path: str,
        frame: int | None = None,
        *,
        project_path: str | None = None,
    ) -> None:
        """
        创建单素材工程的 ColorSession（仅限图片）。

        用于 Library 打开的单张图片。视频文件不能作为 standalone 调色，
        必须通过 Edit timeline clip 流程进入 Color。

        Args:
            path: 源文件路径
            frame: 当前帧号（保留参数，图片场景下为 None）

        Raises:
            ValueError: 如果传入的是视频文件路径
        """
        # Guard: reject video files — videos must go through timeline clip flow
        from src.shared.media.format_registry import is_video as _is_video_file
        if _is_video_file(path):
            logger.warning(f"_create_standalone_image_color_session: rejected video file: {path}")
            raise ValueError(
                "Video files cannot be graded as standalone assets. "
                "Use the Edit timeline clip flow instead."
            )

        project = getattr(self, "_current_edit_project", None)
        asset = project.get_asset_by_path(path) if project is not None else None
        stack = project.get_color_stack_for_asset(asset.asset_id) if asset else None
        if project is None or asset is None or stack is None:
            raise RuntimeError(
                "Standalone image session requires the canonical image project"
            )

        project_id = project.project_id
        color_stack_id = stack.stack_id

        # 创建 ColorSession
        self._color_session_manager.create_single_asset_session(
            project_id=project_id,
            color_stack_id=color_stack_id,
            source_path=path,
            frame=frame,
        )

        logger.info(f"Single asset ColorSession created: {self._color_session_manager.get_current_session()}")

    def _load_color_stack_to_grading_panel(self, stack_id: str) -> None:
        """
        加载调色栈到 GradingPanel + 所有调色 UI 面板 (P9.1).

        通过 ColorStackHydrator 将调色栈数据统一同步到:
        - GradingPanel (色轮 + 滑块)
        - CurvesWidget (RGB 曲线) + CurvesPanel 侧栏 (四参数柔化裁切/强度)
        - HSLCurvesPanel (HSL 曲线)
        - MixerPanel (颜色混合器)
        - LutPanel (3D LUT)
        - engine.layers (渲染引擎)

        Args:
            stack_id: 调色栈 ID
        """
        if not self._current_edit_project:
            logger.warning("No EditProject for color stack loading")
            return

        stack = self._current_edit_project.color_stacks.get(stack_id)
        if not stack:
            logger.warning(f"Color stack not found: {stack_id}")
            return

        from src.features.color.adapters.color_stack_hydrator import ColorStackHydrator

        hydrator = ColorStackHydrator()
        panels = self._collect_color_panels()
        hydrator.hydrate(stack, self.engine, panels)
        self._refresh_video_preview_after_color_stack_hydration()

        logger.debug(f"Loaded color stack {stack_id} to all panels via hydrator")

    def _refresh_video_preview_after_color_stack_hydration(self) -> None:
        """Rebuild the playback frame after applying a saved ColorStack.

        The realtime video player intentionally caches its render parameters.
        Hydrating a clip stack updates ``engine.layers`` directly, so no user
        signal is emitted to invalidate that cache. Without this explicit
        refresh, Timeline Clips can correctly restore their saved controls
        while the viewer continues drawing the prior/default grade.
        """
        if not getattr(self, 'current_file_is_video', False):
            return

        invalidate = getattr(self, '_invalidate_video_render_params_if_playing', None)
        if callable(invalidate):
            invalidate()

        video_controller = getattr(self, 'video_controller', None)
        refresh_current_frame = getattr(video_controller, 'refresh_current_frame_view', None)
        is_loaded = getattr(video_controller, 'is_video_loaded', None)
        if not callable(refresh_current_frame):
            return

        if callable(is_loaded):
            try:
                if not is_loaded():
                    return
            except Exception:
                return

        try:
            if refresh_current_frame():
                logger.debug('Refreshed video frame after ColorStack hydration')
        except Exception:
            logger.debug('Video frame refresh after ColorStack hydration failed', exc_info=True)

    def _collect_color_panels(self) -> dict[str, Any]:
        """Collect all color UI panel references into a dict for hydrator use."""
        panels: dict[str, Any] = {}
        if hasattr(self, 'grading_panel') and self.grading_panel:
            panels["grading_panel"] = self.grading_panel
        if hasattr(self, 'curves_widget') and self.curves_widget:
            panels["curves_panel"] = self.curves_widget
            # 曲线面板侧栏（四参数柔化裁切 + Edit 强度）的水合入口
            if getattr(self, "curves_panel", None):
                panels["curves_palette"] = self.curves_panel
        if hasattr(self, 'hsl_curves_panel') and self.hsl_curves_panel:
            panels["hsl_panel"] = self.hsl_curves_panel
        if hasattr(self, 'mixer_panel') and self.mixer_panel:
            panels["mixer_panel"] = self.mixer_panel
        if hasattr(self, 'lut_panel') and self.lut_panel:
            panels["lut_panel"] = self.lut_panel
        if hasattr(self, 'denoise_panel') and self.denoise_panel:
            panels["denoise_panel"] = self.denoise_panel
        return panels

    def _handle_enter_deliver_with_context(self, context) -> None:
        """
        处理进入 Deliver 页面的上下文逻辑。

        功能：
        1. 根据 deliver_source 设置导出来源
        2. 如果来自 timeline，设置时间线导出参数

        Args:
            context: PageContext 实例
        """
        source = context.deliver_source

        # 更新导出面板的来源设置
        if hasattr(self.export_panel, 'set_export_source'):
            self.export_panel.set_export_source(source)

        if source == "timeline":
            timeline_id = context.timeline_id
            if timeline_id and hasattr(self.export_panel, 'set_timeline_id'):
                self.export_panel.set_timeline_id(timeline_id)
            logger.debug(f"Deliver source: timeline ({timeline_id})")

        elif source == "composite":
            composite_id = context.composite_id
            logger.debug(f"Deliver source: composite ({composite_id})")

        elif source == "color":
            # 默认导出当前 Color 输出
            if self.engine.img_full_original is not None:
                h, w = self.engine.img_full_original.shape[:2]
                self.export_panel.set_original_size(w, h)
            self.export_panel.set_default_path(self.current_file_path)
            logger.debug("Deliver source: color (current image/video)")

    def _handle_enter_edit_with_context(self, context) -> None:
        """
        处理进入 Edit 页面的上下文逻辑。

        功能：
        1. 如果来自 Color，带回调色后的 clip
        2. 如果来自 Composite，带回合成后的 clip

        Args:
            context: PageContext 实例
        """
        clip_id = context.clip_id
        if not clip_id:
            return

        # 如果有 color_stack_id 或 composite_id，
        # 在 Properties Panel 中显示对应信息
        if context.color_stack_id:
            logger.debug(f"Edit → back with color_stack_id: {context.color_stack_id}")
            # P9.1: 在 edit_properties_panel 中显示调色状态
            if hasattr(self, 'edit_properties_panel') and self.edit_properties_panel:
                self.edit_properties_panel.update_color_status(context.color_stack_id)

        if context.composite_id:
            logger.debug(f"Edit → back with composite_id: {context.composite_id}")
            # P9.2: 在 edit_properties_panel 中显示合成状态
            if hasattr(self, 'edit_properties_panel') and self.edit_properties_panel:
                self.edit_properties_panel.update_composite_status(context.composite_id)

    def get_page_context_manager(self):
        """获取页面上下文管理器。"""
        return self._page_context_manager

    # ------------------------------------------------------------------
    # Convenience page-switch helpers
    # ------------------------------------------------------------------

    def switch_from_edit_to_color(self, clip_id: str = None) -> None:
        """
        从 Edit 页面切换到 Color 页面。

        自动获取当前选中的 clip 信息，创建上下文并切换。

        Args:
            clip_id: 可选的 clip ID，如果不提供则使用当前选中
        """
        from src.shared.i18n.i18n import I18N

        if not clip_id:
            # 获取当前选中的 clip
            clip_ids = self.timeline_panel.get_selected_clip_ids()
            if not clip_ids:
                self.status_bar.showMessage(
                    I18N.tr("edit.no_clip_selected", "No clip selected"), 3000
                )
                return
            clip_id = clip_ids[0]

        # 从 timeline 获取 clip 信息
        clip = self._get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        timeline_id = None
        if self._current_edit_project is not None:
            timeline_id = self._current_edit_project.active_timeline_id
        if not timeline_id:
            logger.warning("switch_from_edit_to_color: no active timeline")
            return

        current_frame = None
        try:
            current_frame = self.timeline_panel.get_playhead_frame()
        except (AttributeError, TypeError):
            current_frame = getattr(self.edit_page, '_current_frame', None)

        # Keep every Edit -> Color entry on the same explicit timeline clip path.
        self.open_timeline_clip_in_color(timeline_id, clip.clip_id, current_frame)

    def _switch_to_color_following_edit_playhead(self) -> bool:
        """Route a plain Edit→Color page switch through the playhead's clip.

        Reads the Edit playhead, resolves the clip it covers and delegates to
        open_timeline_clip_in_color so the Color page lands on that clip at
        that frame. Returns True when the switch was handled and the caller
        must not run the plain switch; False when there is nothing meaningful
        to open (no project/timeline, no video clip under the playhead) or the
        clip entry bailed, so the previous keep-last-state behaviour stands.
        """
        project = self._current_edit_project
        timeline_id = project.active_timeline_id if project is not None else None
        timeline = (
            self._current_timeline_sequences.get(timeline_id)
            if timeline_id is not None
            else None
        )
        if timeline is None:
            return False

        try:
            playhead = int(self.timeline_panel.get_playhead_frame())
        except (AttributeError, TypeError, RuntimeError):
            playhead = int(getattr(self.edit_page, "_current_frame", 0) or 0)

        # [AUD-3103] 统一分层约定：与 Composite 播放跟随
        # （_find_clip_under_composite_playhead）共用 topmost-enabled-clip
        # 解析——最上层可见视频轨优先，disabled clip 视为 gap。旧实现按
        # video_tracks 自然顺序取第一个命中且不过滤 is_enabled，与另一
        # 条路径在多层视频轨重叠时打开不同 clip。
        clip = self._topmost_enabled_video_clip_at(timeline, playhead)
        if clip is None:
            return False

        asset = project.assets.get(clip.asset_id)
        if asset is None or not (asset.is_video or asset.is_image):
            return False

        return bool(
            self.open_timeline_clip_in_color(timeline_id, clip.clip_id, playhead)
        )

    def _update_color_monitor_for_asset(self, asset, asset_metadata: dict) -> None:
        """P1-COLOR-22: 打开 clip 时刷新色彩监看状态栏。

        源 tag 优先读 asset 缓存 metadata，缺失时探测一次并写回缓存
        （避免每次切镜重复 ffprobe）。provider 诊断直接读注册表。

        [AUD-3102] 首次未命中缓存的容器探测是磁盘/网络 I/O（PyAV
        demux open），不得在 UI 线程同步执行——4K/网络媒体可卡顿数百
        毫秒到数秒。现在先以已知（可能为默认）的 tag 立即刷新监看，
        探测在后台 daemon 线程执行，完成后经队列信号回到 UI 线程回填
        ``asset.metadata`` 并再次刷新监看（带代际校验，过期结果不覆盖
        新打开的 clip）。
        """
        canvas = getattr(self, "canvas", None)
        if canvas is None or not hasattr(canvas, "update_color_monitor"):
            return
        try:
            transfer = asset_metadata.get("color_transfer")
            primaries = asset_metadata.get("color_primaries")
            range_tag = asset_metadata.get("color_range")
            from src.shared.color.color_transform_provider import \
                get_color_provider_diagnostics
            diag = get_color_provider_diagnostics(
                getattr(self, "config_manager", None))
            # 先以已知（可能为默认/未知）的 tag 刷新监看，保证页面切换
            # 立即完成；后台探测完成后再做带探测结果的第二次刷新。
            canvas.update_color_monitor(
                source={"transfer": transfer, "primaries": primaries,
                        "range": range_tag},
                working="scene-linear",
                display="sRGB",
                provider_diag=diag,
            )
            if not (transfer or primaries):
                # 模块级入口（宿主可以是测试替身等非 QObject 对象）。
                _launch_color_tag_probe_for_host(self, asset)
        except Exception:
            logger.debug("color monitor update failed", exc_info=True)

    def _launch_color_tag_probe(self, asset: Any) -> None:
        """[AUD-3102] 见模块级 ``_launch_color_tag_probe_for_host``。"""
        _launch_color_tag_probe_for_host(self, asset)

    def _on_color_tag_probe_finished(
        self, generation: int, asset: Any, tags: Optional[dict],
    ) -> None:
        """[AUD-3102] 后台探测完成（UI 线程）：回填 asset.metadata 并刷新监看。"""
        _apply_color_tag_probe_result(self, generation, asset, tags)

    def open_timeline_clip_in_color(
        self,
        timeline_id: str,
        clip_id: str,
        timeline_frame: int | None = None,
        source_frame_override: int | None = None,
    ) -> bool:
        """
        Open a specific timeline clip in the Color page.

        Unlike switch_from_edit_to_color which operates on the currently
        selected clip, this method takes explicit timeline/clip identifiers
        and computes precise frame mappings for accurate round-tripping.

        Args:
            timeline_id: Timeline sequence ID
            clip_id: Clip ID within the timeline
            timeline_frame: Frame position on the timeline. If None, uses
                the playhead position.
            source_frame_override: Optional source frame supplied by the
                Timeline Clips card itself. This avoids re-deriving the first
                frame from stale playhead/timeline state when clicking cards
                inside the Color page.

        Returns:
            True when the context was created and the page entry initiated;
            False when a precondition failed and nothing was switched. The
            Edit→Color playhead route relies on this to fall back to the
            plain page switch instead of leaving the tab click dead.
        """
        from src.shared.i18n.i18n import I18N

        _flash_diag.event(
            'open_clip_in_color', timeline_id=timeline_id, clip_id=clip_id,
            timeline_frame=timeline_frame, page=getattr(self, 'current_page', None),
        )

        requested_timeline_frame = timeline_frame

        # --- a. Project ---------------------------------------------------
        project = self._current_edit_project
        if not project:
            logger.warning("open_timeline_clip_in_color: no current EditProject")
            return False

        # --- b. Timeline --------------------------------------------------
        timeline = self._current_timeline_sequences.get(timeline_id)
        if not timeline:
            logger.warning(f"open_timeline_clip_in_color: timeline not found: {timeline_id}")
            return False

        # --- c. Clip ------------------------------------------------------
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"open_timeline_clip_in_color: clip not found: {clip_id}")
            return False

        # --- d. Asset -----------------------------------------------------
        asset = project.assets.get(clip.asset_id)
        if not asset:
            logger.warning(f"open_timeline_clip_in_color: asset not found: {clip.asset_id}")
            return False

        # --- e. Validate asset type ---------------------------------------
        if not (asset.is_video or asset.is_image):
            self.status_bar.showMessage(
                I18N.tr("edit.clip_not_video_or_image", "Clip is not video or image"), 3000
            )
            return False

        # --- f. Resolve timeline_frame ------------------------------------
        if timeline_frame is None:
            if getattr(self, "current_page", None) == "Color":
                # TimelineClipStrip clicks identify the target clip, not the
                # Edit playhead. Using the old playhead here made Color clip
                # selection feel stuck on the first clip when the playhead was
                # outside the clicked card's range.
                timeline_frame = clip.timeline_start
            else:
                try:
                    timeline_frame = self.timeline_panel.get_playhead_frame()
                except (AttributeError, TypeError):
                    timeline_frame = clip.timeline_start

        # --- g. Clamp to clip range ---------------------------------------
        if timeline_frame < clip.timeline_start:
            timeline_frame = clip.timeline_start
        elif timeline_frame >= clip.timeline_end:
            timeline_frame = clip.timeline_start

        # A Color clip card is a timeline navigation action. Keep Edit's
        # playhead as the canonical timeline position; Color maps it to the
        # graded source frame and exposes this clip as its transport range.
        self._sync_timeline_playhead_for_color_entry(timeline_frame)

        # --- h. Compute frame mappings ------------------------------------
        clip_local_frame = timeline_frame - clip.timeline_start
        source_frame = self._resolve_color_clip_source_frame(
            clip,
            asset,
            timeline_frame,
        )
        if source_frame_override is not None:
            source_frame = self._normalize_color_clip_source_frame(
                clip,
                asset,
                int(source_frame_override),
            )
        asset_metadata = getattr(asset, "metadata", None) or {}
        _emit_color_clip_debug(
            "[COLOR_CLIP_DEBUG] open.request timeline=%s clip=%s "
            "arg_timeline_frame=%s arg_source_override=%s resolved_timeline_frame=%s "
            "clip_local_frame=%s resolved_source_frame=%s clip_timeline_start=%s "
            "clip_timeline_end=%s clip_source_start=%s clip_source_duration=%s "
            "asset=%s asset_path=%s asset_meta_is_subclip=%s "
            "asset_meta_source_start=%s asset_meta_source_duration=%s",
            timeline_id,
            clip_id,
            requested_timeline_frame,
            source_frame_override,
            timeline_frame,
            clip_local_frame,
            source_frame,
            getattr(clip, "timeline_start", None),
            getattr(clip, "timeline_end", None),
            getattr(clip, "source_start", None),
            getattr(clip, "source_duration", None),
            getattr(clip, "asset_id", None),
            getattr(asset, "path", None),
            asset_metadata.get("is_subclip") if isinstance(asset_metadata, dict) else getattr(asset_metadata, "is_subclip", None),
            asset_metadata.get("source_start") if isinstance(asset_metadata, dict) else getattr(asset_metadata, "source_start", None),
            asset_metadata.get("source_duration") if isinstance(asset_metadata, dict) else getattr(asset_metadata, "source_duration", None),
        )

        # --- i. Ensure clip has an owned color_stack_id -------------------
        # Scene cuts preserve the source clip's local stack reference.  That
        # is safe for rendering history, but a newly created segment must get
        # its own stack before it can be edited independently.  Shared asset
        # grades remain shared by design.
        color_stack_id = clip.color_stack_id
        stack = (
            self._current_edit_project.color_stacks.get(color_stack_id)
            if color_stack_id
            else None
        )
        inherited_local_stack = bool(
            color_stack_id
            and not getattr(clip, "color_shared", False)
            and getattr(stack, "scope", "clip_local") == "clip_local"
            and getattr(stack, "clip_id", None)
            and getattr(stack, "clip_id", None) != clip_id
        )
        if not color_stack_id or inherited_local_stack:
            color_stack_id = self._ensure_clip_has_color_stack(clip_id, clip.asset_id)

        # --- j. Refresh clip data after color stack creation ---------------
        # The _ensure_clip_has_color_stack call may replace the timeline in
        # _current_timeline_sequences (immutable dataclass pattern), so
        # re-fetch the clip to get the updated color_stack_id.
        if not color_stack_id or inherited_local_stack:
            # Re-read from the (possibly updated) timeline.  The helper uses
            # immutable timeline snapshots, so the original ClipInstance may
            # still carry the inherited stack reference.
            timeline = self._current_timeline_sequences.get(timeline_id, timeline)
            clip = timeline.get_clip_by_id(clip_id) or clip
            color_stack_id = clip.color_stack_id

        # --- k. Create PageContext ----------------------------------------
        context = self._page_context_manager.create_context_for_edit_to_color(
            clip_id=clip_id,
            asset_id=clip.asset_id,
            timeline_id=timeline_id,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
            color_stack_id=color_stack_id,
        )

        # --- l. Switch to Color page --------------------------------------
        self._pause_playback_before_color_clip_switch()
        if getattr(self, "current_page", None) == "Color":
            self._page_context_manager.set_current_context(context)
            self._handle_enter_color_with_context(context)
        else:
            self.switch_to_page_with_context(context)
        logger.info(
            f"open_timeline_clip_in_color: clip={clip_id} "
            f"timeline_frame={timeline_frame} source_frame={source_frame} "
            f"clip_local_frame={clip_local_frame} stack={color_stack_id}"
        )
        return True

    @staticmethod
    def _metadata_int(metadata: object, key: str, default: int = 0) -> int:
        """从 dict 或对象的 metadata 中宽松读取 int 值（失败回退 default）。"""
        try:
            if isinstance(metadata, dict):
                value = metadata.get(key, default)
            else:
                value = getattr(metadata, key, default)
            return int(value if value is not None else default)
        except (TypeError, ValueError):
            return int(default)

    def _asset_subclip_source_start(self, asset: object | None) -> int:
        """虚拟子剪辑资产在父媒体中的起始帧（非子剪辑返回 0）。"""
        metadata = getattr(asset, "metadata", None) or {}
        is_subclip = (
            bool(metadata.get("is_subclip", False))
            if isinstance(metadata, dict)
            else bool(getattr(metadata, "is_subclip", False))
        )
        if not is_subclip:
            return 0
        return self._metadata_int(metadata, "source_start", 0)

    def _normalize_color_clip_source_frame(
        self,
        clip: object,
        asset: object | None,
        source_frame: int,
    ) -> int:
        """Normalize source frames for virtual subclip assets.

        Scene-cut subclips share the parent media path.  Some imported/older
        timelines store clip.source_start as a local offset inside the subclip,
        while the true parent-media offset lives in asset.metadata["source_start"].
        Color must decode from the parent media frame domain, so convert local
        subclip frames to absolute parent-media frames here.
        """
        asset_base = self._asset_subclip_source_start(asset)
        if asset_base <= 0:
            return int(source_frame)
        if int(source_frame) >= asset_base:
            return int(source_frame)
        clip_start = self._metadata_int(clip, "source_start", 0)
        if clip_start >= asset_base:
            return int(source_frame)
        local = max(0, int(source_frame) - clip_start)
        return asset_base + local

    def _resolve_color_clip_source_frame(
        self,
        clip: object,
        asset: object | None,
        timeline_frame: int,
    ) -> int:
        """时间线帧 → Color 解码用源媒体帧（经子剪辑归一化）。

        优先 ``clip.timeline_to_source_frame``，异常时回退 clip 元数据
        的 source_start，再交给 :meth:`_normalize_color_clip_source_frame`
        处理虚拟子剪辑的父媒体帧域换算。
        """
        try:
            source_frame = clip.timeline_to_source_frame(timeline_frame)
        except (ValueError, AttributeError):
            source_frame = self._metadata_int(clip, "source_start", 0)
        return self._normalize_color_clip_source_frame(clip, asset, int(source_frame))

    def _pause_playback_before_color_clip_switch(self) -> None:
        """Stop active playback before changing the Color page clip source."""
        video_controller = getattr(self, "video_controller", None)
        if video_controller is not None and getattr(video_controller, "is_playing", False):
            try:
                video_controller.stop_playback()
            except Exception:
                logger.debug("Failed to stop Color video before clip switch", exc_info=True)

        timeline_panel = getattr(self, "timeline_panel", None)
        if timeline_panel is None:
            return
        try:
            controller = timeline_panel.get_playback_controller()
        except (AttributeError, TypeError):
            controller = None
        if controller is not None and getattr(controller, "is_playing", False):
            try:
                controller.pause()
            except Exception:
                logger.debug("Failed to pause Edit playback before Color clip switch", exc_info=True)

    def _sync_timeline_playhead_for_color_entry(self, timeline_frame: int) -> None:
        """Align the shared Edit timeline playhead before opening a Color clip."""
        timeline_panel = getattr(self, "timeline_panel", None)
        if timeline_panel is None:
            return
        try:
            current_frame = timeline_panel.get_playhead_frame()
        except (AttributeError, TypeError):
            current_frame = None
        if current_frame == int(timeline_frame):
            return
        try:
            timeline_panel.set_playhead_frame(int(timeline_frame))
        except (AttributeError, TypeError, RuntimeError) as exc:
            logger.debug("Failed to align timeline playhead for Color entry: %s", exc)

    def refresh_color_timeline_clip_strip(self) -> None:
        """
        Refresh the TimelineClipStrip with current project/timeline data.

        Should be called when:
        - Switching to Color page
        - Active timeline changes
        - Timeline clips are modified

        The strip is only visible on the Color page (registered with
        page_ids=("Color",)).
        """
        project = self._current_edit_project
        sequence = None
        if project and project.active_timeline_id:
            sequence = self._current_timeline_sequences.get(project.active_timeline_id)
        if hasattr(self, 'color_timeline_clip_strip'):
            self.color_timeline_clip_strip.set_project_and_timeline(project, sequence)

    # ------------------------------------------------------------------
    # Debounced clip-strip refresh (P2: Edit-Color real-time sync)
    # ------------------------------------------------------------------

    def _setup_clip_strip_sync(self) -> None:
        """
        Connect Edit timeline change signals to a debounced clip-strip refresh.

        Should be called from ``setup_connections`` after ``timeline_panel``
        and ``edit_page`` are initialized.  The debounce timer collapses
        rapid bursts of timeline_changed signals (e.g. during drag-move or
        multi-clip operations) into a single refresh.

        Signal lifecycle: the timer is parented to *self* (MainWindow) and
        is destroyed together with it.  Signal connections are severed via
        ``_cleanup_clip_strip_sync`` when the window is destroyed.
        """
        from PyQt6.QtCore import QTimer

        # Single-shot debounce timer — 80 ms gives good UX without wasted
        # work during rapid edits.
        self._clip_strip_refresh_timer = QTimer(self)
        self._clip_strip_refresh_timer.setSingleShot(True)
        self._clip_strip_refresh_timer.setInterval(80)
        self._clip_strip_refresh_timer.timeout.connect(
            self.refresh_color_timeline_clip_strip,
        )

        # Connect timeline content changes (clip add/remove/trim/move).
        if self._edit_available and self.timeline_panel is not None:
            self.timeline_panel.timeline_changed.connect(
                self._on_clip_strip_timeline_changed,
            )

        # Connect timeline switch events.
        if self._edit_available and self.edit_page is not None:
            self.edit_page.signal_hub.timeline_switched.connect(
                self._on_clip_strip_timeline_switched,
            )

    def _on_clip_strip_timeline_changed(self, _timeline: object) -> None:
        """Restart debounce timer on every timeline content change."""
        if hasattr(self, '_clip_strip_refresh_timer'):
            self._clip_strip_refresh_timer.start()

    def _on_clip_strip_timeline_switched(self, _timeline_id: str) -> None:
        """Restart debounce timer on timeline switch."""
        if hasattr(self, '_clip_strip_refresh_timer'):
            self._clip_strip_refresh_timer.start()

    def _cleanup_clip_strip_sync(self) -> None:
        """Disconnect clip-strip sync signals to prevent leaks on destruction."""
        if not hasattr(self, '_clip_strip_refresh_timer'):
            return

        self._clip_strip_refresh_timer.stop()

        try:
            if self._edit_available and self.timeline_panel is not None:
                self.timeline_panel.timeline_changed.disconnect(
                    self._on_clip_strip_timeline_changed,
                )
        except (TypeError, RuntimeError, AttributeError):
            pass

        try:
            if self._edit_available and self.edit_page is not None:
                self.edit_page.signal_hub.timeline_switched.disconnect(
                    self._on_clip_strip_timeline_switched,
                )
        except (TypeError, RuntimeError, AttributeError):
            pass

    def switch_from_edit_to_composite(self, clip_id: str = None, create_new: bool = False) -> None:
        """
        从 Edit 页面切换到 Composite 页面。

        Args:
            clip_id: 可选的 clip ID
            create_new: 是否创建新合成
        """
        if not clip_id:
            clip_ids = self.timeline_panel.get_selected_clip_ids()
            if not clip_ids:
                # Composite can be opened as an independent workspace even
                # when Edit has no selected timeline clip.
                self._suppress_composite_auto_bind_once = True
                self.switch_to_page("Composite")
                return
            clip_id = clip_ids[0]

        clip = self._get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        # P9.2: 从 clip 获取 composite_id
        composite_id = self._resolve_composite_id_for_clip(clip)
        timeline_id = None
        timeline = None
        for tid, seq in self._current_timeline_sequences.items():
            for candidate_track in seq.all_tracks:
                if candidate_track.get_clip_by_id(clip_id):
                    timeline = seq
                    timeline_id = tid
                    break
            if timeline is not None:
                break

        if not composite_id:
            logger.info(
                "[Composite] Opening unbound workspace for normal clip: %s",
                clip_id,
            )
            self._suppress_composite_auto_bind_once = True
            self.switch_to_page("Composite")
            return

        try:
            timeline_frame = self.timeline_panel.get_playhead_frame()
        except (AttributeError, TypeError):
            timeline_frame = getattr(clip, 'timeline_start', 0)

        if timeline_frame < clip.timeline_start or timeline_frame >= clip.timeline_end:
            timeline_frame = clip.timeline_start

        clip_local_frame = timeline_frame - clip.timeline_start
        try:
            source_frame = clip.timeline_to_source_frame(timeline_frame)
        except (ValueError, AttributeError):
            source_frame = clip.source_start

        context = self._page_context_manager.create_context_for_edit_to_composite(
            clip_id=clip_id,
            composite_id=composite_id,
            create_new=False,
            asset_id=clip.asset_id,
            timeline_id=timeline_id,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
            color_stack_id=clip.color_stack_id,
        )
        self.switch_to_page_with_context(context)

    def switch_to_deliver(self, source: str = "color", timeline_id: str = None) -> None:
        """
        切换到 Deliver 页面。

        Args:
            source: 导出来源（color, composite, timeline）
            timeline_id: 时间线 ID（当 source=timeline 时）
        """
        context = self._page_context_manager.create_context_for_to_deliver(
            source=source,
            timeline_id=timeline_id,
        )
        self.switch_to_page_with_context(context)

    def _get_clip_by_id(self, clip_id: str) -> Optional[Any]:
        """
        根据 clip_id 获取 ClipInstance。

        Args:
            clip_id: Clip ID

        Returns:
            ClipInstance 或 None
        """
        if not self._current_edit_project:
            return None

        # 遍历所有 timeline sequences 查找 clip
        for timeline_id, timeline in self._current_timeline_sequences.items():
            for track in timeline.all_tracks:
                for clip in track.clips:
                    if clip.clip_id == clip_id:
                        return clip
        return None

    def _safe_switch_to_page(self, page_name: str) -> None:
        """安全切换页面，隔离 Quick Project 后续 UI 初始化失败。"""
        try:
            self.switch_to_page(page_name)
            logger.info(f"Quick Project: successfully switched to {page_name} page")
        except Exception as e:
            logger.error(f"Quick Project: switch_to_page({page_name}) failed: {e}")
            self.status_bar.showMessage(
                "项目创建成功，请手动切换到 Edit 页面",
                10000
            )
